"""Usage tracking: parse token/cost data from worker stream-json logs.

Provides per-assignment and per-model cost breakdowns, session burn rate,
and summary helpers for ``coord usage`` and the burn-rate warning in
``coord status``.

Usage data is collected from two sources:

* **Local logs** — ``~/.coord/logs/<assignment_id>.log`` (stream-json).
  These exist when the agent ran on the same machine as the coordinator.
* **Remote agent status** — HTTP ``/status`` on agent servers.
  The agent already reports ``cost_so_far`` / ``total_cost_usd`` in its
  ``list_assignments()`` response; we use those when a local log is absent.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from coord.models import Assignment

# Re-export COORD_DIR so callers don't need to import state directly.
from coord.state import COORD_DIR

if TYPE_CHECKING:
    from coord.config import Config
    from coord.providers.base import Provider

_log = logging.getLogger(__name__)

LOGS_DIR = COORD_DIR / "logs"

# Burn rate threshold ($/hr) above which coord status shows a warning line.
HIGH_BURN_RATE_USD_PER_HOUR = 2.0


# ── Data classes ─────────────────────────────────────────────────────────────


@dataclass
class AssignmentUsage:
    """Cost/usage data for a single assignment."""

    assignment_id: str
    repo_name: str
    issue_number: int
    issue_title: str
    status: str  # pending | running | done | failed
    model: str | None = None
    total_cost_usd: float = 0.0
    num_turns: int = 0
    duration_ms: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    # #2128: True when this assignment's cost is genuinely *unknown* — no
    # local log (e.g. it ran on another machine) and no remote data was
    # supplied to fill the gap — as opposed to a real, captured $0.00. Only
    # set for providers that actually report cost (see
    # ``capabilities().cost_reporting``); a provider that never reports cost
    # (subscription-billed ``claude-pty``) is legitimately silent, matching
    # ``parse_usage_from_log``'s own #1710 signal. Lets
    # ``format_usage_report`` warn that the total undercounts instead of
    # silently rendering these as free.
    cost_unknown: bool = False

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def duration_str(self) -> str:
        if self.duration_ms is None:
            return "?"
        s = self.duration_ms / 1000.0
        if s < 60:
            return f"{s:.0f}s"
        m, sec = divmod(int(s), 60)
        if m < 60:
            return f"{m}m {sec}s"
        h, m = divmod(m, 60)
        return f"{h}h {m}m"


@dataclass
class SessionUsage:
    """Aggregated usage across all assignments in the current session."""

    started_at: float | None = None  # Unix timestamp from session.json
    assignments: list[AssignmentUsage] = field(default_factory=list)

    @property
    def total_cost_usd(self) -> float:
        return sum(a.total_cost_usd for a in self.assignments)

    @property
    def total_input_tokens(self) -> int:
        return sum(a.input_tokens for a in self.assignments)

    @property
    def total_output_tokens(self) -> int:
        return sum(a.output_tokens for a in self.assignments)

    @property
    def elapsed_hours(self) -> float | None:
        """Hours since session start; None if no session timestamp."""
        if self.started_at is None:
            return None
        elapsed_sec = time.time() - self.started_at
        # Guard against negative/zero to avoid division weirdness.
        return max(elapsed_sec, 60.0) / 3600.0

    def burn_rate_usd_per_hour(self) -> float | None:
        """$/hr based on total cost and elapsed session time.

        Returns None if the session start time is unknown.
        """
        hours = self.elapsed_hours
        if hours is None:
            return None
        if self.total_cost_usd == 0.0:
            return 0.0
        return self.total_cost_usd / hours

    def cost_by_model(self) -> dict[str, float]:
        """Map model name → total cost across all assignments."""
        result: dict[str, float] = {}
        for a in self.assignments:
            key = a.model or "(unknown)"
            result[key] = result.get(key, 0.0) + a.total_cost_usd
        return result

    def count_by_model(self) -> dict[str, int]:
        """Map model name → number of assignments."""
        result: dict[str, int] = {}
        for a in self.assignments:
            key = a.model or "(unknown)"
            result[key] = result.get(key, 0) + 1
        return result

    def uncosted_count(self) -> int:
        """Number of assignments whose cost is unknown rather than $0.00
        (#2128) — no local log and no remote data covered them."""
        return sum(1 for a in self.assignments if a.cost_unknown)


# ── Log parsing ───────────────────────────────────────────────────────────────


def parse_usage_from_log(
    log_path: Path,
    *,
    provider_name: str | None = None,
    provider: "Provider | None" = None,
    cfg: "Config | None" = None,
) -> AssignmentUsage | None:
    """Parse an :class:`AssignmentUsage` from a worker log file.

    Returns ``None`` if the file doesn't exist, isn't stream-json, or can't
    be parsed.  The returned object has placeholder values for fields that
    aren't available from the log alone (``assignment_id``, ``repo_name``,
    etc.) — callers must fill those in from the board.

    #1710: cost/token parsing is routed through the assignment's resolved
    :class:`~coord.providers.base.Provider` (``provider.parse_log()``)
    instead of assuming :mod:`coord.worker_events`'s claude-shaped parser.
    *provider_name* (typically ``Assignment.provider_name``) resolves via
    :func:`coord.providers.get_provider`; ``None`` defaults to
    :class:`~coord.providers.claude.ClaudeProvider`, matching pre-#1710
    behaviour byte-for-byte for every caller that doesn't pass it. *provider*
    is an escape hatch to pass an already-constructed provider directly
    (tests; bypasses name resolution).

    *cfg* (#2305): the coordinator's :class:`~coord.config.Config`, passed
    straight through to :func:`coord.providers.get_provider` so a
    **tier-named** ``providers.definitions`` entry (e.g. ``oc-mid``) resolves
    to its real backend instead of silently degrading to
    :class:`~coord.providers.claude.ClaudeProvider` — *provider_name* alone
    only ever matches the built-in type names. ``None`` (the default)
    preserves pre-#2305 behaviour.
    """
    from coord.worker_events import is_stream_json

    if not log_path.exists():
        return None
    if provider is None:
        from coord.providers import get_provider  # noqa: PLC0415
        provider = get_provider(provider_name, cfg=cfg)
    if not is_stream_json(log_path):
        # #1710: a provider that claims it reports cost but whose log isn't
        # stream-json shaped gets NO cost/token data here — that mismatch is
        # worth a loud signal rather than a silent, indistinguishable-from-
        # "no cost yet" None. A provider that legitimately never reports cost
        # (e.g. claude-pty, subscription-billed) sets
        # capabilities().cost_reporting=False and stays silent, as before.
        if provider.capabilities().cost_reporting:
            _log.warning(
                "parse_usage_from_log: %s is not stream-json but provider "
                "%r reports capabilities().cost_reporting=True — no cost/"
                "token data will be captured for this assignment (#1710)",
                log_path, provider_name or "claude",
            )
        return None
    try:
        summary = provider.parse_log(log_path, tail_bytes=0)
    except OSError:
        return None
    return AssignmentUsage(
        assignment_id="",  # caller fills in
        repo_name="",  # caller fills in
        issue_number=0,  # caller fills in
        issue_title="",  # caller fills in
        status="",  # caller fills in
        model=summary.model_used,
        total_cost_usd=summary.total_cost_usd,
        num_turns=summary.num_turns,
        duration_ms=summary.duration_ms,
        input_tokens=summary.input_tokens,
        output_tokens=summary.output_tokens,
        cache_creation_tokens=summary.cache_creation_tokens,
        cache_read_tokens=summary.cache_read_tokens,
    )


def _assignment_to_usage(
    a: Assignment,
    *,
    logs_dir: Path | None = None,
    remote_data: dict | None = None,
    cfg: "Config | None" = None,
) -> AssignmentUsage:
    """Build an :class:`AssignmentUsage` for *a*.

    Priority: local log file > *remote_data* dict (from agent HTTP) >
    Assignment.model field as a final model fallback.

    *remote_data* is a dict from the agent's ``list_assignments()`` response
    (e.g. ``{"cost_so_far": 0.12, "model_used": "claude-sonnet-4-6", ...}``).

    *cfg* (#2305): threaded through to both the local-log parse and the
    "neither local nor remote" ``get_provider`` fallback below, so a
    tier-named provider (e.g. ``oc-mid``) resolves to its real backend
    rather than :class:`~coord.providers.claude.ClaudeProvider`. ``None``
    (the default) preserves pre-#2305 behaviour.
    """
    _logs_dir = logs_dir if logs_dir is not None else LOGS_DIR
    from coord.models import effective_issue_number  # noqa: PLC0415

    usage = AssignmentUsage(
        assignment_id=a.assignment_id or "",
        repo_name=a.repo_name,
        # #1553: per-issue spend must land on the issue the work was really
        # for. An oracle-loop acceptance slice carries the milestone's
        # tracking issue in `issue_number`, so booking cost against it
        # attributed every child's authoring spend to the epic.
        issue_number=effective_issue_number(a),
        issue_title=a.issue_title,
        status=a.status,
        model=a.model,
    )

    # Try local log first.
    if a.assignment_id:
        log_path = _logs_dir / f"{a.assignment_id}.log"
        # #1710: thread the assignment's resolved provider name through so
        # the parse uses the right provider's parse_log() rather than always
        # assuming claude.
        parsed = parse_usage_from_log(log_path, provider_name=a.provider_name, cfg=cfg)
        if parsed is not None:
            usage.model = parsed.model or a.model
            usage.total_cost_usd = parsed.total_cost_usd
            usage.num_turns = parsed.num_turns
            usage.duration_ms = parsed.duration_ms
            usage.input_tokens = parsed.input_tokens
            usage.output_tokens = parsed.output_tokens
            usage.cache_creation_tokens = parsed.cache_creation_tokens
            usage.cache_read_tokens = parsed.cache_read_tokens
            return usage

    # Fall back to remote agent data if available.
    if remote_data:
        cost = remote_data.get("total_cost_usd") or remote_data.get("cost_so_far") or 0.0
        usage.total_cost_usd = float(cost)
        model_r = remote_data.get("model_used") or remote_data.get("model")
        if model_r:
            usage.model = str(model_r)
        turns = remote_data.get("num_turns") or remote_data.get("turns")
        if isinstance(turns, int):
            usage.num_turns = turns
        return usage

    # #2128: neither a local log nor remote data covered this assignment —
    # its $0.00 is a placeholder, not a captured fact. Flag it as such when
    # the assignment's provider actually reports cost data (a provider with
    # cost_reporting=False is genuinely free/uncosted and stays silent).
    if a.assignment_id:
        from coord.providers import get_provider  # noqa: PLC0415

        provider = get_provider(a.provider_name, cfg=cfg)
        if provider.capabilities().cost_reporting:
            usage.cost_unknown = True

    return usage


# ── Session collection ────────────────────────────────────────────────────────


def collect_usage(
    board_assignments: list[Assignment],
    *,
    logs_dir: Path | None = None,
    remote_by_id: dict[str, dict] | None = None,
    cfg: "Config | None" = None,
) -> list[AssignmentUsage]:
    """Collect :class:`AssignmentUsage` for every assignment on the board.

    *remote_by_id* maps ``assignment_id → agent_status_dict`` for assignments
    whose logs live on a remote machine.  Pass ``None`` (the default) to skip
    remote lookups entirely — the result will still be correct for any
    assignment whose log is available locally.

    *cfg* (#2305): the coordinator's :class:`~coord.config.Config`, when the
    caller has one — threaded to :func:`_assignment_to_usage` so a
    tier-named ``providers.definitions`` entry resolves correctly rather
    than degrading to :class:`~coord.providers.claude.ClaudeProvider`.
    ``None`` (the default) preserves pre-#2305 behaviour.
    """
    result: list[AssignmentUsage] = []
    for a in board_assignments:
        if not a.assignment_id:
            continue
        remote = (remote_by_id or {}).get(a.assignment_id)
        result.append(
            _assignment_to_usage(a, logs_dir=logs_dir, remote_data=remote, cfg=cfg)
        )
    return result


def build_session_usage(
    board_assignments: list[Assignment],
    *,
    logs_dir: Path | None = None,
    remote_by_id: dict[str, dict] | None = None,
    started_at: float | None = None,
    cfg: "Config | None" = None,
) -> SessionUsage:
    """Build a :class:`SessionUsage` from the current board.

    *started_at* should come from ``session.json["started_at"]`` (parsed to
    a Unix timestamp).  If not provided we fall back to the oldest
    ``dispatched_at`` among the assignments.

    *cfg* (#2305): passed straight through to :func:`collect_usage` — see
    its docstring.
    """
    if started_at is None:
        # Derive from oldest dispatch time on the board.
        times = [
            a.dispatched_at
            for a in board_assignments
            if a.dispatched_at is not None
        ]
        started_at = min(times) if times else None

    assignments = collect_usage(
        board_assignments,
        logs_dir=logs_dir,
        remote_by_id=remote_by_id,
        cfg=cfg,
    )
    return SessionUsage(started_at=started_at, assignments=assignments)


def filter_assignments_in_window(assignments: list[Assignment], window) -> list[Assignment]:
    """Filter *assignments* to those in-window (#1119 review finding #1).

    An assignment is in-window if its ``dispatched_at`` **or** ``finished_at``
    falls inside *window* (a :class:`~coord.usage_rollup.TimeWindow`/
    ``Window``) — the same semantics :func:`coord.usage_rollup.leg_in_window`
    applies to daemon-row dicts, applied here directly to ``Assignment``
    timestamps (already Unix floats, no ISO parsing needed). This lets the
    legacy ``coord usage`` view (no ``--by``/``--by-time``/``--by-issue``/
    ``--issue``) actually honor ``--today``/``--week``/``--month``/``--since``
    instead of silently ignoring them.
    """
    return [
        a for a in assignments
        if window.contains(a.dispatched_at) or window.contains(a.finished_at)
    ]


# ── Rollup fetch (#1118) ──────────────────────────────────────────────────────

# #3313: hard ceiling on a fully unbounded fetch (no since/until at all) —
# a windowed call (--today/--week/--month/--since, the common case) is bounded
# by the window itself and practically never hits this. Exists only so an
# unbounded "all history" fetch on a very old fleet can't grow forever; if it
# ever DOES trip, the caller must be told the total undercounts rather than
# silently rendering a partial read as if it were complete.
USAGE_ROWS_MAX_ROWS = 50_000

# #4139: an unbounded `/usage-rows` round-trip makes the daemon read, decode
# and serialize every row in `assignments` + `assignments_archive`, which on a
# live fleet with real history routinely takes longer than a plain httpx
# default (5s) — short enough that `coord usage --issue N` with no window flag
# fails with a read timeout instead of an answer. This is the default for both
# `fetch_usage_rows` and `fetch_usage_rows_from_daemon`; callers that know
# their fetch is cheap (a tightly windowed/issue-filtered read) may still pass
# a smaller `timeout` explicitly.
USAGE_ROWS_DEFAULT_TIMEOUT = 30.0


def fetch_usage_rows(
    *,
    since: float | None = None,
    until: float | None = None,
    issue: int | None = None,
    flag_url: str | None = None,
    flag_token: str | None = None,
    timeout: float = USAGE_ROWS_DEFAULT_TIMEOUT,
) -> list[dict]:
    """Fetch board assignment rows as plain dicts for :mod:`coord.usage_rollup`.

    This is the thin *fetch* caller the pure aggregator's docstring promises:
    it mirrors :func:`coord.board_service.read_board`'s local-vs-remote
    branch, but returns raw wire-shape ``dict`` rows rather than
    :class:`~coord.models.Assignment` instances — the aggregator consumes the
    daemon ``/board`` ``assignments`` shape directly, including
    ``is_interactive``, which is a real DB/wire column but (deliberately,
    #748/#632 — see ``coord.board_schema.INTEGER_BACKED_BOOLEANS`` and
    ``tests/test_board_schema.py``) not an ``Assignment`` dataclass field, so
    converting through ``Assignment`` would silently drop it.

    #3313: neither branch reads ``/board`` any more. ``/board`` is capped
    (#762) to active + pipeline-referenced + ``COORD_BOARD_RETENTION_DAYS``
    (default 14) of terminal rows — a usage rollup wants full history, so
    reading it from a thin client made ``coord usage`` under-report spend by
    ~8x and made a *wider* ``--since`` window sometimes report *less* than a
    narrower one (whichever slice of history happened to still be resident on
    ``/board``). Remote now GETs the daemon's dedicated ``/usage-rows``
    endpoint (own read path, like ``/leg-counts``/``/audit``); local reads
    ``assignments`` + ``assignments_archive`` directly via
    :func:`_local_usage_rows` — ``SqliteStore.list_assignments()`` alone
    would miss any row ``coord housekeeping`` already archived.

    *since*/*until* (Unix-epoch floats, optional) push the resolved
    ``--today``/``--week``/``--month``/``--since`` window down into the fetch
    itself, rather than relying on :func:`coord.usage_rollup.aggregate` to
    filter an already-truncated set — the same window semantics as
    :func:`coord.usage_rollup.leg_in_window` (in-window if ``dispatched_at``
    **or** ``finished_at`` falls in ``[since, until)``). Leaving both ``None``
    (the default) fetches unbounded all-time history, matching every
    call site's pre-#3313 behaviour.

    *issue* (#4139, optional) narrows the fetch to legs attributed to one
    GitHub issue (:func:`coord.usage_rollup.row_issue_number` semantics —
    ``for_issue_number`` when set, else ``issue_number``), pushed down the
    same way *since*/*until* are: the daemon branch sends it as a query
    param so a capable daemon filters before serializing the response (the
    whole point — on a live fleet, serializing every history row is what
    blows the httpx timeout, not the SQLite scan), and the local branch
    filters before truncation. An older daemon that doesn't recognize the
    param simply ignores it and returns the window-filtered set, which the
    caller (e.g. ``_usage_issue_drill``) must still filter client-side —
    this is a narrowing optimization, never the only filter applied.

    *timeout* defaults to :data:`USAGE_ROWS_DEFAULT_TIMEOUT`, not the
    generic httpx default — see that constant's docstring for why.
    """
    from coord.client import fetch_usage_rows_from_daemon, resolve_board_service  # noqa: PLC0415

    svc = resolve_board_service(flag_url, flag_token)
    if svc is not None:
        result = fetch_usage_rows_from_daemon(
            svc, since=since, until=until, issue=issue, timeout=timeout
        )
        rows = list(result.get("rows") or [])
        if result.get("truncated"):
            _log.warning(
                "fetch_usage_rows: daemon /usage-rows read was truncated at "
                "%d rows (#3313) — the rendered total UNDERCOUNTS actual spend.",
                len(rows),
            )
        return rows

    rows, truncated = _local_usage_rows(since=since, until=until, issue=issue)
    if truncated:
        _log.warning(
            "fetch_usage_rows: local usage-rows read was truncated at %d rows "
            "(#3313) — the rendered total UNDERCOUNTS actual spend.",
            len(rows),
        )
    return rows


def _local_usage_rows(
    *,
    since: float | None = None,
    until: float | None = None,
    issue: int | None = None,
) -> tuple[list[dict], bool]:
    """Full-history local read spanning ``assignments`` + ``assignments_archive``
    (#3313) — backs both :func:`fetch_usage_rows`'s local branch and the
    daemon's ``GET /usage-rows`` handler (``coord/serve_app.py``).

    Mirrors :func:`coord.state._leg_counts_local`'s span: ``coord
    housekeeping`` MOVES (never deletes) terminal assignments older than
    ``COORD_ARCHIVE_RETENTION_DAYS`` (default 30) out of the live
    ``assignments`` table into ``assignments_archive``, so a read of
    ``assignments`` alone silently loses the tail of any window reaching past
    that boundary — exactly the "full history, not a retention-capped slice"
    promise this module's docstring already made and ``/board`` couldn't keep.

    *issue* (#4139, optional) filters to legs attributed to one GitHub issue
    (:func:`coord.usage_rollup.row_issue_number`) before the row-count
    ceiling below is applied, so a single issue's drill-down never gets
    truncated by unrelated history sharing the table.

    Returns ``(rows, truncated)`` — ``truncated`` is only ``True`` if
    :data:`USAGE_ROWS_MAX_ROWS` was hit, newest-dispatched rows kept. Callers
    must surface that rather than silently rendering a partial total as
    complete (#3313's "a rollup that silently drops rows must say so").
    """
    from coord import sql  # noqa: PLC0415
    from coord.board_schema import decode_row  # noqa: PLC0415
    from coord.db import get_connection, rollback_after_driver_error  # noqa: PLC0415
    from coord.usage_rollup import TimeWindow, leg_in_window, row_issue_number  # noqa: PLC0415

    conn = get_connection()
    rows: list[dict] = []
    for table in ("assignments", "assignments_archive"):
        try:
            result = sql.execute(conn, f"SELECT * FROM {table}").fetchall()  # noqa: S608 — literal table name
        except sql.driver_errors() as exc:
            # #2983/#2784: continue on the SAME connection rather than raise —
            # a missing assignments_archive (housekeeping never ran yet) must
            # not abort the (Postgres) transaction and poison the sibling
            # `assignments` read that hasn't happened yet in this loop.
            rollback_after_driver_error(conn, exc)
            continue  # assignments_archive may not exist yet (housekeeping never ran)
        # `assignments_archive` is a dumb, unregistered column-mirror of
        # `assignments` (see coord.housekeeping._ensure_archive_mirror) — it
        # has no entry of its own in `board_schema.BOARD_PROJECTIONS`, so
        # decoding it under its own table name would skip DTO projection
        # entirely (`decode_row` falls back to `dict(row)` for an unknown
        # table) and ship every raw column, including `briefing` — exactly
        # the ~8MB-per-row wire bloat #1849 slimmed the live table to avoid.
        # Decode both tables under the "assignments" key so archived rows get
        # the identical slim projection live rows do.
        rows.extend(decode_row("assignments", r) for r in result)

    if issue is not None:
        rows = [r for r in rows if row_issue_number(r) == issue]

    if since is not None or until is not None:
        window = TimeWindow(start=since, end=until)
        rows = [r for r in rows if leg_in_window(r, window)]

    rows.sort(key=lambda r: r.get("dispatched_at") or 0.0, reverse=True)
    truncated = len(rows) > USAGE_ROWS_MAX_ROWS
    if truncated:
        rows = rows[:USAGE_ROWS_MAX_ROWS]
    return rows, truncated


# ── Per-repo p90 attention threshold (#4142) ─────────────────────────────────
#
# `coord.config.PipelineConfig.attention_threshold_for`'s fixed "work" default
# (45m) is a fleet-wide guess that doesn't fit every repo — quadraui's own
# first-work legs normally run 45-69m, so the fixed default fired a false
# "needs attention" alert on nearly every one of them. This section learns a
# per-(repo, assignment type) threshold from that repo's own history instead,
# so attention_threshold_for can replace the guess with "what normal actually
# looks like here" once there's enough history to trust, and otherwise keep
# the fixed default unchanged.

#: Terminal leg statuses that count as a *successful* run, for the purpose of
#: learning a threshold. Deliberately narrower than
#: `coord.notifier.baseline.SAMPLE_STATUSES` (which also keeps "failed"/
#: "advisory" — that module is answering "how long does this kind of leg
#: take", not "how long does it take to succeed"): a failed leg's duration
#: says nothing about how long a normal, converging run of this type takes,
#: and mixing it in would pull the threshold toward whatever makes a leg fail
#: (a timeout, a crash loop) rather than toward what a healthy one looks like.
SUCCESSFUL_LEG_STATUSES = frozenset({"done", "merged"})

#: Minimum successful-leg samples before a repo/type's learned p90 is trusted
#: over the fixed default — mirrors `coord.notifier.baseline.MIN_SAMPLES`'s
#: "never fire off a population of one" rule.
P90_ATTENTION_MIN_SAMPLES = 5

#: How long the learned-threshold index is cached before being refetched.
#: `attention_signal` is evaluated for every running assignment on every
#: `coord notify` sweep, so an unbounded `fetch_usage_rows()` call per check
#: would turn one sweep into dozens of full-history scans; this amortizes it
#: to one fetch per TTL window regardless of how many assignments are active.
P90_ATTENTION_CACHE_TTL = 300.0

#: Only legs dispatched within this trailing window feed the learned p90 — a
#: repo's recent normal is what matters for "is THIS leg running long", not a
#: multi-year history the fleet's hardware/model mix has long since outgrown.
#: Also keeps the underlying fetch bounded rather than scanning full history
#: (#4139's exact timeout failure mode) on every cache refresh.
P90_ATTENTION_HISTORY_WINDOW_SECS = 30 * 24 * 3600.0

# (cached-at monotonic time, {(repo_name, assignment_type): [duration_secs]})
# — module-level memo, mirrors `coord.config._TS_SELF_DNS_MEMO`'s shape.
_SUCCESSFUL_DURATIONS_CACHE: tuple[float, dict[tuple[str, str], list[float]]] | None = None


def reset_p90_attention_cache() -> None:
    """Forget the memoized successful-leg-duration index (tests only)."""
    global _SUCCESSFUL_DURATIONS_CACHE
    _SUCCESSFUL_DURATIONS_CACHE = None


def _successful_durations_index(
    *, now: float | None = None,
) -> dict[tuple[str, str], list[float]]:
    """The cached ``(repo_name, assignment_type) -> [duration_secs, ...]``
    index of successful-leg durations, refetched at most once per
    :data:`P90_ATTENTION_CACHE_TTL`.
    """
    global _SUCCESSFUL_DURATIONS_CACHE

    now = time.monotonic() if now is None else now
    cached = _SUCCESSFUL_DURATIONS_CACHE
    if cached is not None:
        cached_at, index = cached
        if now - cached_at < P90_ATTENTION_CACHE_TTL:
            return index

    from coord.usage_rollup import leg_duration  # noqa: PLC0415

    since = time.time() - P90_ATTENTION_HISTORY_WINDOW_SECS
    rows = fetch_usage_rows(since=since)
    index: dict[tuple[str, str], list[float]] = {}
    for row in rows:
        if str(row.get("status") or "") not in SUCCESSFUL_LEG_STATUSES:
            continue
        secs, is_open = leg_duration(row)
        if is_open or secs <= 0:
            continue
        key = (str(row.get("repo_name") or ""), str(row.get("type") or "work"))
        index.setdefault(key, []).append(secs)

    _SUCCESSFUL_DURATIONS_CACHE = (now, index)
    return index


def p90_attention_threshold(
    repo_name: str,
    assignment_type: str,
    *,
    min_samples: int = P90_ATTENTION_MIN_SAMPLES,
    now: float | None = None,
) -> float | None:
    """P90 duration (seconds) of *repo_name*'s successful *assignment_type*
    legs over the trailing history window, or ``None`` when there are fewer
    than *min_samples* qualifying legs — the caller's signal to fall back to
    the fixed default instead of trusting a thin population.

    Best-effort: any read failure (daemon unreachable, DB error, ...) is
    swallowed and reported as ``None``, exactly like a cold/empty population.
    This feeds an attention-threshold *default*, not a gate — a transient
    read failure must fall back to the fixed default, never raise and break
    the `coord notify` needs-attention sweep.
    """
    from coord.notifier.baseline import percentile  # noqa: PLC0415

    try:
        index = _successful_durations_index(now=now)
    except Exception:  # noqa: BLE001
        _log.warning(
            "p90_attention_threshold(%r, %r): usage fetch failed, falling "
            "back to the fixed default", repo_name, assignment_type,
            exc_info=True,
        )
        return None
    durations = index.get((repo_name, assignment_type))
    if not durations or len(durations) < min_samples:
        return None
    return percentile(durations, 90.0)


# ── Heal/stall signals for `coord stats waste` (#4144) ──────────────────────
#
# The #4122 heal/stall/cost tranche (#4139-#4143) each landed a fix with no
# durable before/after number attached — this section is the read side that
# turns the audit trail those fixes already write into something `coord
# stats waste` can print, so a later efficiency change against any of these
# four shapes can be scored against a real baseline instead of an anecdote.
# Pure functions over *already-fetched* ``fetch_usage_rows`` rows and
# ``coord.state.list_audit_log`` entries — exactly the two read seams
# ``coord.commands.stats.waste`` already composes for
# :func:`coord.leg_waste.build_waste_report`. `repo_attention_thresholds` is
# the one exception: it delegates to `config.pipeline.attention_threshold_for`,
# which can itself fetch usage rows to compute a learned p90 (bounded,
# cached) — see its own docstring below.

#: `coord.notify._sweep_phantom_rows` audit event for one row #2536's sweep
#: auto-healed. `coord.diagnose.sweep_dead_running_rows` computes which rows
#: are dead; `_sweep_phantom_rows` is what actually calls `record_audit`.
PHANTOM_ROW_HEALED_EVENT = "phantom_row_auto_healed"

#: `coord.notify._sweep_stuck_test_state` audit event for one row #2803's
#: watchdog cleared for a fresh Test-stage dispatch.
STUCK_TEST_STATE_HEALED_EVENT = "stuck_test_state_auto_healed"

#: `coord.diagnose.sweep_dead_running_rows`'s heal ``detail`` text always
#: names how many CONSECUTIVE sweep passes agreed the row was dead before
#: healing it (#4141's own two-miss confirmation gate — see
#: ``coord.diagnose.PHANTOM_DEAD_CONFIRM_PASSES``). Parsed back out here
#: rather than duplicating the gate's own threshold, so a heal event
#: recorded by an OLDER coordinator build (pre-#4141, no such phrase at all)
#: reads as "not miss-confirmed" rather than silently miscounted as one —
#: the exact distinction that makes this number meaningful: it should read
#: 0 for history before #4141 landed and equal the plain heal count after.
_PHANTOM_HEAL_MISS_COUNT_RE = re.compile(r"across (\d+) consecutive sweep passes")


@dataclass
class HealCounts:
    """How many of each #4122 auto-heal kind fired in the reported window —
    see the module section docstring above for the audit events each counts.
    """

    phantom_row_healed: int = 0
    phantom_row_miss_confirmed: int = 0
    stuck_test_state_healed: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "phantom_row_healed": self.phantom_row_healed,
            "phantom_row_miss_confirmed": self.phantom_row_miss_confirmed,
            "stuck_test_state_healed": self.stuck_test_state_healed,
        }


def heal_counts_from_audit(audit_entries: "list[dict] | None") -> HealCounts:
    """Tally #2536/#2803 auto-heal audit events — see :class:`HealCounts`.

    Best-effort over whatever ``audit_entries`` the caller already fetched
    (``coord.state.list_audit_log``'s shape); never raises, and an empty/
    ``None`` list reads as all-zero rather than an error, mirroring
    :func:`coord.leg_waste.audit_leg_kind_overrides`'s own posture toward
    the same read seam.
    """
    from coord.diagnose import PHANTOM_DEAD_CONFIRM_PASSES  # noqa: PLC0415

    counts = HealCounts()
    for entry in audit_entries or ():
        event_type = entry.get("event_type")
        if event_type == PHANTOM_ROW_HEALED_EVENT:
            counts.phantom_row_healed += 1
            detail = str((entry.get("details") or {}).get("detail") or "")
            m = _PHANTOM_HEAL_MISS_COUNT_RE.search(detail)
            if m and int(m.group(1)) >= PHANTOM_DEAD_CONFIRM_PASSES:
                counts.phantom_row_miss_confirmed += 1
        elif event_type == STUCK_TEST_STATE_HEALED_EVENT:
            counts.stuck_test_state_healed += 1
    return counts


@dataclass
class RepoThreshold:
    """The wall-clock ``attention_threshold_for`` value actually applied to
    one ``(repo_name, assignment_type)`` pair, and whether that value came
    from #4142's per-repo learned p90 rather than the fleet-wide fixed
    default/explicit ``coordinator.yml`` override — see
    :func:`repo_attention_thresholds`.
    """

    repo_name: str
    assignment_type: str
    threshold_secs: float
    is_learned: bool

    def to_dict(self) -> dict[str, "float | str | bool"]:
        return {
            "repo_name": self.repo_name,
            "assignment_type": self.assignment_type,
            "threshold_secs": self.threshold_secs,
            "is_learned": self.is_learned,
        }


def repo_attention_thresholds(
    repo_names: Iterable[str],
    *,
    assignment_type: str = "work",
    config: "Config",
) -> list[RepoThreshold]:
    """Per-repo #4142 learned-threshold override in effect for
    *assignment_type*, one :class:`RepoThreshold` per name in *repo_names*
    (de-duplicated, order preserved).

    Reuses ``config.pipeline.attention_threshold_for`` — the one function
    that already resolves explicit-override-vs-learned-vs-fixed-default —
    rather than re-deriving that order here (#2096 "one question, one
    answer"): comparing the *repo-scoped* call against the exact same call
    with no ``repo_name`` isolates whether #4142's own per-repo learned
    step is what's in effect, since that step is the ONLY thing
    ``repo_name`` changes about the result.

    Unlike the rest of this module, this is not pure: a learned threshold
    reaches ``p90_attention_threshold``, which does its own (cached,
    bounded-window) ``fetch_usage_rows`` call.
    """
    generic = config.pipeline.attention_threshold_for(assignment_type)
    seen: set[str] = set()
    out: list[RepoThreshold] = []
    for repo_name in repo_names:
        if not repo_name or repo_name in seen:
            continue
        seen.add(repo_name)
        effective = config.pipeline.attention_threshold_for(
            assignment_type, repo_name=repo_name,
        )
        out.append(RepoThreshold(
            repo_name=repo_name,
            assignment_type=assignment_type,
            threshold_secs=effective,
            is_learned=effective != generic,
        ))
    return out


@dataclass
class FanoutFoldLatency:
    """How long #3182's fold (``coord.smoke.finalize_smoke_fanout``) took to
    write the parent's aggregate verdict after the LAST of its fan-out legs
    actually finished — see :func:`fanout_fold_latencies`.
    """

    assignment_id: str
    repo_name: str
    issue_number: int
    latency_secs: float

    def to_dict(self) -> dict[str, "str | int | float"]:
        return {
            "assignment_id": self.assignment_id,
            "repo_name": self.repo_name,
            "issue_number": self.issue_number,
            "latency_secs": self.latency_secs,
        }


def fanout_fold_latencies(
    assignment_rows: list[dict],
    audit_entries: "list[dict] | None",
) -> list[FanoutFoldLatency]:
    """Elapsed time, per #3182 fan-out round, between the LAST fan-out leg
    finishing and :func:`coord.smoke.finalize_smoke_fanout` writing the
    parent's own aggregate ``test_{state}`` verdict — i.e. how long the
    fold itself took to catch up once every leg had reported in. #4143
    fixed the two ``coord.reconcile`` call sites that let this wedge for
    199-220m instead of folding promptly; this is the number that would
    have caught that regression instead of 16 separate stuck-row sweeps
    noticing it after the fact.

    A parent's ``test_{state}`` audit event (recorded by
    ``coord.state._record_test_verdict_local`` on EVERY terminal verdict
    write, fan-out or not) carries that write's own ``test_reason`` in its
    ``details`` — :func:`coord.smoke._parse_fanout_manifest` reads the
    ``[[smoke-fanout:...]]`` manifest :func:`finalize_smoke_fanout`'s own
    write always carries at the front of that text, which is how a
    fan-out fold is told apart from an ordinary single-leg verdict here:
    only a parsed, non-empty manifest counts. Each leg named in it is then
    looked up by id in *assignment_rows* for its own ``finished_at``; the
    round's latency is the fold event's own timestamp minus the LATEST of
    those (the leg still being waited on when the fold finally ran).
    A round with no resolvable leg ``finished_at`` (incomplete usage-row
    history) contributes nothing rather than guessing.
    """
    from coord.smoke import TEST_STATE_BLOCKED  # noqa: PLC0415
    from coord.smoke_tags import _parse_fanout_manifest  # noqa: PLC0415
    from coord.usage_rollup import parse_timestamp  # noqa: PLC0415

    # Every terminal state `finalize_smoke_fanout` can write for the parent
    # row, prefixed the way `_record_test_verdict_local` names the event
    # (`event_type=f"test_{test_state}"`) — "passed", "failed", "skipped",
    # or TEST_STATE_BLOCKED. A round that folds to blocked is the one this
    # function exists to catch, so it must not be filtered out here.
    fold_event_types = (
        "test_passed",
        "test_failed",
        "test_skipped",
        f"test_{TEST_STATE_BLOCKED}",
    )

    rows_by_id = {
        str(row.get("assignment_id")): row
        for row in assignment_rows or ()
        if row.get("assignment_id")
    }

    out: list[FanoutFoldLatency] = []
    for entry in audit_entries or ():
        event_type = entry.get("event_type")
        if event_type not in fold_event_types:
            continue
        fold_ts = parse_timestamp(entry.get("ts"))
        if fold_ts is None:
            continue
        details = entry.get("details") or {}
        legs = _parse_fanout_manifest(details.get("test_reason"))
        if not legs:
            continue  # an ordinary (non-fan-out) verdict write

        leg_finished: list[float] = []
        for leg_id, _caps, _command in legs:
            leg_row = rows_by_id.get(str(leg_id))
            if leg_row is None:
                continue
            finished = parse_timestamp(leg_row.get("finished_at"))
            if finished is not None:
                leg_finished.append(finished)
        if not leg_finished:
            continue

        latency_secs = fold_ts - max(leg_finished)
        if latency_secs < 0:
            # Clock skew or a `finished_at` written after the fold — a
            # fabricated zero would still feed avg/max, so skip the round
            # rather than guess.
            continue

        assignment_id = str(entry.get("assignment_id") or "")
        out.append(FanoutFoldLatency(
            assignment_id=assignment_id,
            repo_name=str(entry.get("repo") or ""),
            issue_number=int(entry.get("issue") or 0),
            latency_secs=latency_secs,
        ))
    return out


# ── Formatting ────────────────────────────────────────────────────────────────


def _fmt_cost(usd: float) -> str:
    if usd < 0.001:
        return "$0.00"
    if usd < 0.01:
        return f"${usd:.4f}"
    return f"${usd:.2f}"


def _fmt_burn_rate(usd_per_hr: float) -> str:
    if usd_per_hr < 0.01:
        return f"${usd_per_hr:.4f}/hr"
    return f"${usd_per_hr:.2f}/hr"


def format_usage_report(session: SessionUsage, window_label: str | None = None) -> str:
    """Return the full multi-section usage report for ``coord usage``.

    *window_label* is set when the caller resolved one of ``--today``/
    ``--week``/``--month``/``--since`` and pre-filtered *session*'s
    assignments to that window (#1119 review finding #1) — when given, a
    ``USAGE — window: ...`` header and a ``Σ total`` grand-total footer are
    printed, mirroring the convention :func:`format_usage_by_group`/
    :func:`format_usage_by_time` already use, so the resolved window is
    visible even on the legacy (no ``--by``) view. Left as ``None`` (the
    default — no window flag was given), the report is byte-for-byte
    unchanged from before #1119.
    """
    lines: list[str] = []

    if window_label is not None:
        lines.append(f"USAGE — window: {window_label}")
        lines.append("")

    # ── Session header ────────────────────────────────────────────────────
    burn = session.burn_rate_usd_per_hour()
    burn_str = _fmt_burn_rate(burn) if burn is not None else "(no session time)"
    high_flag = " ⚠" if burn is not None and burn >= HIGH_BURN_RATE_USD_PER_HOUR else ""

    n_done = sum(1 for a in session.assignments if a.status == "done")
    n_running = sum(1 for a in session.assignments if a.status == "running")
    n_failed = sum(1 for a in session.assignments if a.status == "failed")
    counts: list[str] = []
    if n_done:
        counts.append(f"{n_done} done")
    if n_running:
        counts.append(f"{n_running} running")
    if n_failed:
        counts.append(f"{n_failed} failed")
    counts_str = ", ".join(counts) if counts else "0 assignments"

    total_str = _fmt_cost(session.total_cost_usd)
    lines.append(f"Session usage:  {total_str}  •  {counts_str}  •  burn rate: {burn_str}{high_flag}")

    # Budget remaining estimate: max(0, 5hr - elapsed) * burn_rate
    elapsed_hours = session.elapsed_hours
    if elapsed_hours is not None and burn is not None and burn > 0:
        budget_remaining_usd = max(0.0, 5.0 - elapsed_hours) * burn
        lines.append(
            f"Est. 5hr budget remaining: ~{_fmt_cost(budget_remaining_usd)} (based on current rate)"
        )

    lines.append("")

    # ── Per-assignment table ──────────────────────────────────────────────
    if not session.assignments:
        lines.append("No assignments found.")
        if window_label is not None:
            lines.append("")
            lines.append(f"Σ  total {total_str}  •  {counts_str}")
        return "\n".join(lines)

    lines.append("Per-assignment:")
    col_id_w = max(8, max(len(a.assignment_id[:8]) for a in session.assignments))
    col_repo_w = max(8, max(len(a.repo_name) for a in session.assignments))
    col_model_w = max(5, max(len(a.model or "(unknown)") for a in session.assignments))

    header = (
        f"  {'ID':<{col_id_w}}  {'STATUS':<7}  {'REPO':<{col_repo_w}}  "
        f"{'#':>5}  {'MODEL':<{col_model_w}}  {'TURNS':>5}  {'DUR':>7}  COST"
    )
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))

    for a in session.assignments:
        aid = (a.assignment_id or "")[:8]
        model = a.model or "(unknown)"
        dur = a.duration_str()
        cost = _fmt_cost(a.total_cost_usd)
        line = (
            f"  {aid:<{col_id_w}}  {a.status:<7}  {a.repo_name:<{col_repo_w}}  "
            f"#{a.issue_number:>4}  {model:<{col_model_w}}  {a.num_turns:>5}  "
            f"{dur:>7}  {cost}"
        )
        lines.append(line)

    # ── Token summary (only shown when any tokens were recorded) ──────────
    total_in = session.total_input_tokens
    total_out = session.total_output_tokens
    if total_in or total_out:
        lines.append("")
        lines.append(f"Token totals:  {total_in:,} input  •  {total_out:,} output")

    # ── Per-model breakdown ───────────────────────────────────────────────
    lines.append("")
    lines.append("Per-model:")
    cost_by = session.cost_by_model()
    count_by = session.count_by_model()
    total = session.total_cost_usd

    for model_name in sorted(cost_by, key=lambda m: cost_by[m], reverse=True):
        n = count_by[model_name]
        c = cost_by[model_name]
        pct = f"  ({100 * c / total:.0f}%)" if total > 0 else ""
        noun = "assignment" if n == 1 else "assignments"
        lines.append(f"  {model_name:<{col_model_w}}  {n} {noun:<12}  {_fmt_cost(c)}{pct}")

    if window_label is not None:
        lines.append("")
        lines.append(f"Σ  total {total_str}  •  {counts_str}")

    # #2128: this view derives cost by re-parsing local log files, so a leg
    # that ran on another machine (or whose remote data wasn't fetched)
    # renders as $0.00 — indistinguishable from "actually free" without this
    # line. Silence here is what turned a visible gap into a silent 2x
    # undercount.
    uncosted = session.uncosted_count()
    if uncosted:
        noun = "assignment" if uncosted == 1 else "assignments"
        lines.append("")
        lines.append(
            f"⚠ {uncosted} {noun} could not be costed (no local log — "
            f"likely ran on another machine). This total may undercount. "
            f"Try `coord usage --by-issue` or `--remote` for full fleet cost."
        )

    return "\n".join(lines)


# ── Per-issue rollup rendering (#1115 CLI-1) ─────────────────────────────────
#
# Consumes coord.usage_rollup.aggregate() (#1118 Core) — this module only
# renders the plain dict it returns. Distinct 4-decimal cost formatting and
# compact token/duration formatting are used here (vs. the 2-decimal
# _fmt_cost/duration_str above) to match the sealed contract mocks exactly
# (tests/acceptance/ms-37/contract.md, Mocks 1 & 2).


def pricing_dict_from_config(pricing) -> dict:
    """Convert a :class:`~coord.config.PricingConfig` to the plain
    ``{model: {"input": ..., "output": ..., "cache_read": ..., "cache_creation": ...}}``
    dict :func:`coord.usage_rollup.aggregate` expects."""
    return {
        model: {
            "input": rates.input,
            "output": rates.output,
            "cache_read": rates.cache_read,
            "cache_creation": rates.cache_creation,
        }
        for model, rates in pricing.models.items()
    }


def _fmt_cost4(usd: float) -> str:
    """Captured-cost formatting for the rollup views: always 4 decimals."""
    return f"${usd:.4f}"


def _fmt_est4(usd: float) -> str:
    """Estimated-cost formatting: ``~$`` prefix, always visually distinct
    from a captured figure (see :func:`_fmt_cost4`)."""
    return f"~${usd:.4f}"


def _fmt_tokens_compact(n: int) -> str:
    """Compact token count: ``k`` below 1M, one-decimal ``M`` at/above."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{round(n / 1_000)}k"
    return str(n)


def _fmt_duration_hms(secs: float | None, *, is_open: bool) -> str:
    """``NmSSs`` duration (e.g. ``20m00s``); ``—`` for an open/unknown leg."""
    if is_open or secs is None:
        return "—"
    total = int(round(secs))
    m, s = divmod(total, 60)
    return f"{m}m{s:02d}s"


def format_usage_by_issue(result: dict, window_label: str) -> str:
    """Render the ``coord usage --by-issue`` view (contract Mock 1) from an
    :func:`coord.usage_rollup.aggregate` result (``by="issue"``).

    *window_label* is the resolved :class:`~coord.usage_rollup.Window`'s
    ``label`` (e.g. ``"today"``) — printed in the header, not consumed by
    the aggregator itself.
    """
    lines = [f"USAGE — by issue — window: {window_label}"]
    lines.append(
        f"{'issue':<8}{'repo':<10}{'legs':>4}   {'cost':<10} {'est(~)':<11} "
        f"{'out / cache':<20}{'time':<10}note"
    )
    for g in result["groups"]:
        issue_no = g["key"]
        repo = g["rows"][0].get("repo_name", "") if g["rows"] else ""
        cost_str = _fmt_cost4(g["cost_captured"]) if g["cost_captured"] > 0 else "—"
        est_str = _fmt_est4(g["cost_est"]) if g["cost_est"] > 0 else "—"
        out_str = _fmt_tokens_compact(g["tokens"]["output"])
        cache_str = _fmt_tokens_compact(g["tokens"]["cache_read"])
        tok_str = f"{out_str} / {cache_str}"
        dur_str = _fmt_duration_hms(g["duration_secs"], is_open=False)
        note = f"⚠ unknown-model:{g['unknown_models']}" if g["unknown_models"] else ""
        lines.append(
            f"#{issue_no:<7}{repo:<10}{g['legs']:>4}   {cost_str:<10} {est_str:<11} "
            f"{tok_str:<20}{dur_str:<10}{note}"
        )

    lines.append("─" * 80)
    t = result["totals"]
    total_out = _fmt_tokens_compact(t["tokens"]["output"])
    total_cache = _fmt_tokens_compact(t["tokens"]["cache_read"])
    total_dur = _fmt_duration_hms(t["duration_secs"], is_open=False)
    progress = f" · {t['open_legs']} in progress" if t["open_legs"] else ""
    lines.append(
        f"Σ  captured {_fmt_cost4(t['cost_captured'])} · est {_fmt_est4(t['cost_est'])} · "
        f"total {_fmt_cost4(t['cost_total'])} · {total_out} out / {total_cache} cache · "
        f"{total_dur}{progress}"
    )
    return "\n".join(lines)


def format_usage_issue_drill(rows: list[dict], issue_number: int, pricing) -> str:
    """Render the ``coord usage --issue N`` per-stage drill (contract Mock 2).

    *rows* are the raw board-row dicts for this one issue (any window
    filtering already applied by the caller); *pricing* is a
    :class:`~coord.config.PricingConfig`. Rows are rendered oldest-first by
    ``dispatched_at`` (falling back to ``finished_at``).

    #3672: the ``turns``/``ctx(~)`` columns surface the two numbers the
    7-day cost analysis found drove the 300+-turn-leg cost cliff (31% of
    work spend at a $10.36 mean, vs. $1.07 under 100 turns) — ``turns`` is
    the exact worker-reported ``num_turns``; ``ctx(~)`` is
    :func:`coord.usage_rollup.leg_context_footprint`'s best-effort proxy
    (see its own docstring for why it's a proxy, not a persisted true
    peak).
    """
    from coord.usage_rollup import (
        leg_context_footprint,
        leg_cost,
        leg_duration,
        parse_timestamp,
    )

    if not rows:
        return f"No usage data for issue #{issue_number}."

    def _sort_ts(row: dict) -> float:
        ts = parse_timestamp(row.get("dispatched_at"))
        if ts is None:
            ts = parse_timestamp(row.get("finished_at"))
        return ts if ts is not None else float("inf")

    ordered = sorted(rows, key=_sort_ts)
    repo = rows[0].get("repo_name", "")

    total_captured = 0.0
    total_est = 0.0
    for row in rows:
        captured, est, _unknown = leg_cost(row, pricing)
        total_captured += captured
        total_est += est

    lines = [
        f"#{issue_number}  {repo}   {_fmt_cost4(total_captured)} captured  +  "
        f"{_fmt_est4(total_est)} est"
    ]
    lines.append(
        f"{'stage':<9}{'model':<11}{'int':<5}{'cost':<11}{'est(~)':<11}"
        f"{'out':<7}{'cache':<8}{'turns':>6}  {'ctx(~)':<8}{'time':<10}status"
    )
    for row in ordered:
        captured, est, unknown_model = leg_cost(row, pricing)
        duration, is_open = leg_duration(row)
        stage = str(row.get("type") or "")
        model = str(row.get("model") or "(unknown)")
        interactive = "I" if row.get("is_interactive") else "-"
        cost_col = _fmt_cost4(captured) if captured > 0 else "—"
        if est > 0:
            est_col = _fmt_est4(est)
        elif unknown_model:
            est_col = "n/a*"
        else:
            est_col = "—"
        out_col = _fmt_tokens_compact(int(row.get("output_tokens") or 0))
        cache_col = _fmt_tokens_compact(int(row.get("cache_read_tokens") or 0))
        turns = row.get("num_turns")
        turns_col = str(int(turns)) if turns else "—"
        ctx_col = _fmt_tokens_compact(leg_context_footprint(row))
        time_col = _fmt_duration_hms(duration, is_open=is_open)
        status = str(row.get("status") or "")
        note = "  *unknown model" if unknown_model else ""
        lines.append(
            f"{stage:<9}{model:<11}{interactive:<5}{cost_col:<11}{est_col:<11}"
            f"{out_col:<7}{cache_col:<8}{turns_col:>6}  {ctx_col:<8}{time_col:<10}"
            f"{status}{note}"
        )
    return "\n".join(lines)


# ── Cross-repo + time-bucketed rollup rendering (#1119 CLI-2) ────────────────
#
# Consumes coord.usage_rollup.aggregate() exactly like the #1115 renderers
# above — same ``~$``/4-decimal conventions, same plain-dict input. Two new
# views: a cross-cut rollup by repo/week/month (contract Mock 3) and a
# time-spent ranking by stage-type or issue (contract Mock 4).


def format_usage_by_group(result: dict, window_label: str, dim: str) -> str:
    """Render ``coord usage --by repo|week|month`` (contract Mock 3 for
    ``dim="repo"``) from an :func:`coord.usage_rollup.aggregate` result.

    Each row is one group (a repo name, or a week/month bucket string) with
    its issue count (distinct ``issue_number`` across the group's rows),
    legs, captured/estimated/total cost, tokens, and duration. Groups are
    rendered in *result*'s given order — callers sort beforehand (see
    ``coord/commands/status.py``'s ``_usage_sort_key``).
    """
    lines = [f"USAGE — by {dim} — window: {window_label}"]
    lines.append(
        f"{dim:<8}{'issues':>7}{'legs':>6}   {'cost':<10} {'est(~)':<11} "
        f"{'total':<11} {'out / cache':<20}time"
    )
    for g in result["groups"]:
        key_str = str(g["key"])
        issues = len({row.get("issue_number") for row in g["rows"]})
        cost_str = _fmt_cost4(g["cost_captured"]) if g["cost_captured"] > 0 else "—"
        est_str = _fmt_est4(g["cost_est"]) if g["cost_est"] > 0 else "—"
        total_str = _fmt_cost4(g["cost_total"])
        out_str = _fmt_tokens_compact(g["tokens"]["output"])
        cache_str = _fmt_tokens_compact(g["tokens"]["cache_read"])
        tok_str = f"{out_str} / {cache_str}"
        dur_str = _fmt_duration_hms(g["duration_secs"], is_open=False)
        lines.append(
            f"{key_str:<8}{issues:>7}{g['legs']:>6}   {cost_str:<10} {est_str:<11} "
            f"{total_str:<11} {tok_str:<20}{dur_str}"
        )

    lines.append("─" * 80)
    t = result["totals"]
    total_out = _fmt_tokens_compact(t["tokens"]["output"])
    total_cache = _fmt_tokens_compact(t["tokens"]["cache_read"])
    total_dur = _fmt_duration_hms(t["duration_secs"], is_open=False)
    progress = f" · {t['open_legs']} in progress" if t["open_legs"] else ""
    lines.append(
        f"Σ  total {_fmt_cost4(t['cost_total'])} · {total_out} out / {total_cache} cache · "
        f"{total_dur}{progress}"
    )
    return "\n".join(lines)


def format_usage_by_time(result: dict, window_label: str, dim: str) -> str:
    """Render ``coord usage --by-time`` (contract Mock 4) from an
    :func:`coord.usage_rollup.aggregate` result with ``by="stage"`` (default,
    ``dim="stage"``) or ``by="issue"`` (``dim="issue"``, via ``--by issue``).

    Ranks groups by share of total in-window active duration — "where is
    wall-clock going." An open leg (no ``finished_at``) contributes 0
    duration but is called out via the group's ``(N in progress)`` note.
    """
    lines = [f"USAGE — time by {dim} — window: {window_label}"]
    key_header = "issue" if dim == "issue" else "stage"
    lines.append(f"{key_header:<14}{'legs':>4}   {'time':<10}share")

    total_dur = result["totals"]["duration_secs"]
    for g in result["groups"]:
        key_str = f"#{g['key']}" if dim == "issue" else str(g["key"])
        dur_str = _fmt_duration_hms(g["duration_secs"], is_open=False)
        pct = f"{(g['duration_secs'] / total_dur * 100):.1f}%" if total_dur > 0 else "—"
        note = f"   ({g['open_legs']} in progress)" if g["open_legs"] else ""
        lines.append(f"{key_str:<14}{g['legs']:>4}   {dur_str:<10}{pct}{note}")

    lines.append(f"── total active {_fmt_duration_hms(total_dur, is_open=False)} ──")
    if dim == "stage":
        lines.append("(also available: --by-time --by issue → per-issue duration ranking)")
    else:
        lines.append("(also available: --by-time → time by stage)")
    return "\n".join(lines)


def format_burn_rate_line(session: SessionUsage) -> str | None:
    """One-line burn-rate summary for ``coord status``.

    Returns ``None`` when the burn rate is below the high threshold or
    can't be computed (no session time).
    """
    burn = session.burn_rate_usd_per_hour()
    if burn is None or burn < HIGH_BURN_RATE_USD_PER_HOUR:
        return None
    total_str = _fmt_cost(session.total_cost_usd)
    burn_str = _fmt_burn_rate(burn)
    n = len(session.assignments)
    noun = "assignment" if n == 1 else "assignments"
    return f"Usage: {total_str} this session  •  burn rate: {burn_str} ⚠  ({n} {noun})"
