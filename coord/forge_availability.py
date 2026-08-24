"""Forge/CI availability instrumentation — Phase 0 of the Forge Independence
program (#1896).

Phases 3-6 of that program (issue-store split, parentage, message-bus
cutover) are 20-40 issues of work whose entire justification is "our forge
is unreliable enough that leaving it pays for itself". Whether that premise
is even true has never been measured — every data point that exists lives
in a chat transcript about one bad day (2026-08-06's 7+-hour GitHub Actions
outage). This module turns that into durable data, cheaply, by observing
three seams coord already touches on every relevant call, so nothing here
adds a single extra network round trip:

1. **CI reads** — :func:`record_ci_check_fetch`, called from
   :meth:`coord.ci_github.GitHubCi._fetch` on every live (non-cached)
   ``gh pr checks`` read.
2. **Forge API calls** — :func:`record_gh_call`, called from
   :func:`coord.github_ops._gh`, the seam the vast majority of that module's
   public functions funnel through (#1483). A handful of call sites
   (``edit_issue``, ``close_issue``, ``reopen_issue``, ``rerun_workflow_run``,
   ``rerun_workflow_run_failed``, plus ``get_pr_checks`` which is covered
   separately via seam 1 above) shell out to ``gh`` directly instead of
   through ``_gh`` — each for a documented reason (idempotent-on-a-specific-
   stderr-message semantics, or a "never raise, return False" contract that
   ``_gh``'s raise-only contract can't express) — and each calls
   :func:`record_gh_call` inline at its own call site instead, so none of
   them is a silent gap in this measurement (#1896 review).
3. **Merge-gate refusals** — :func:`record_merge_gate_refusal`, called from
   :func:`coord.merge_queue.process` for each live (never dry-run)
   ``checks_failed``/``checks_pending``/``checks_stale`` :class:`~coord.
   merge_queue.MergeEvent`.

Storage deliberately follows the audit-trail grain (#1041/milestone 33)
rather than inventing a parallel log/table: every observation is one row in
the existing ``audit_log`` (via :func:`coord.audit.record_audit`), tagged
``category="forge_availability"``. :func:`availability_report` is the read
side — ``coord diagnose --forge-availability``.

**#2654: ``outcome="ok"`` observations are rolled up, not written one row
each.** In practice ~99% of observations are "ok" — measured at 159,601 of
162,039 rows (98.5%) on dellserver, 80% of the *entire* audit trail — and
they are pure denominator for the uptime-% math, not signal. Every
``app_error``/``transient``/``unreachable`` observation (the 1.5% that
actually says something about forge/CI availability) is still written
per-observation exactly as before; ``ok`` observations instead accumulate
in-process (see :class:`_OkAggregate`) and flush as a single aggregate row
per bucket (:data:`_OK_BUCKET_S`, per ``argv0`` for ``gh_call`` / per
``(repo, number)`` for ``ci_check_fetch``) carrying ``count``/
``duration_s_total``/``first_ts``/``last_ts`` (plus a summed check-level
``conclusions`` distribution for ``ci_check_fetch``). Flushed on bucket
roll, on process exit (``atexit``), and immediately before any interesting
outcome is recorded — the last of those guarantees an aggregate never lands
*after* an event it chronologically precedes, which is what
:func:`availability_report`'s ordered-observation math depends on.
:func:`availability_report` reads ``details["count"]`` to weight aggregate
rows correctly; :data:`AvailabilityReport.longest_unavailable_stretch_s` is
unaffected because it only ever measures contiguous runs of *unavailable*
observations, which are never aggregated.

**Best-effort, unconditionally.** Every ``record_*`` function here is a thin
wrapper that can never raise, retry, or delay its caller — ``record_audit``
itself already swallows all exceptions (see its docstring), and each
function below adds its own belt-and-suspenders ``try/except`` on top so
that guarantee holds even if this module's own bookkeeping (the periodic
prune sweep) misbehaves. Measurement must never become a new way for the
forge's actual unreliability to take coord down with it.

This is measurement only — see the issue for why acting on the data
(pausing the drive queue when the forge is degraded) is explicitly a
different, sibling issue (#1893).
"""

from __future__ import annotations

import atexit
import logging
import threading
import time
from typing import Any

from coord.audit import MAX_LIMIT as _AUDIT_MAX_LIMIT
from coord.audit import query_audit_log, record_audit

_log = logging.getLogger(__name__)

CATEGORY = "forge_availability"

EVENT_GH_CALL = "gh_call"
EVENT_CI_CHECK_FETCH = "ci_check_fetch"
EVENT_MERGE_GATE_REFUSAL = "merge_gate_refusal"

# gh_call / ci_check_fetch outcomes that count as the forge being reachable.
# "app_error" is gh running fine and reporting a normal application-level
# failure (e.g. "label not found") -- that is NOT a forge-availability
# problem, it is business as usual, so it counts as available. "transient"
# (an error string matching github_ops._is_transient_error -- auth,
# rate-limit, network) and "unreachable" (gh missing / timed out / OSError,
# or a raised read failure on the CI seam) are the two outcomes that
# actually say something about forge/CI availability.
_AVAILABLE_OUTCOMES = frozenset({"ok", "app_error"})

# Refusal reasons this issue asks to be tracked (#1896 scope: "checks_failed
# / checks_pending / checks_stale are already distinct MergeEvent kinds ...
# persist the counts"). Deliberately narrower than every MergeEvent kind
# that can block a merge (e.g. checks_absent, checks_unreadable, conflict)
# -- those are real refusal reasons too, but out of THIS issue's stated
# scope; widening the set later is additive, not a migration.
MERGE_GATE_REFUSAL_KINDS = frozenset({"checks_failed", "checks_pending", "checks_stale"})

# How often (seconds, per-process) the opportunistic retention sweep runs
# after a write. A DELETE scan on every single observation -- the whole
# point of this module is to be cheap enough to fire on every `gh` call --
# would defeat that; observations are cheap to lose track of for an hour
# without threatening the "does not grow unboundedly" acceptance bar.
_PRUNE_INTERVAL_S = 3600.0

# Retention window (days). ">= 90 days" per the issue's acceptance bar.
RETENTION_DAYS = 90.0

_last_prune_at = 0.0


def record_gh_call(
    argv: tuple[str, ...], *, outcome: str, duration_s: float, detail: str = "",
) -> None:
    """Best-effort: one row per :func:`coord.github_ops._gh` invocation --
    except ``"ok"`` outcomes, which accumulate into a per-bucket aggregate
    instead (#2654; see the module docstring).

    ``outcome`` is one of ``"ok"`` (exit 0), ``"app_error"`` (non-zero exit,
    not an auth/network/rate-limit failure -- an ordinary application-level
    error), ``"transient"`` (non-zero exit matching
    ``github_ops._is_transient_error`` -- auth, rate-limit, network), or
    ``"unreachable"`` (the ``gh`` binary was missing, the call timed out, or
    raised some other ``OSError`` before it could even run).
    """
    argv0 = argv[0] if argv else ""
    if outcome == "ok":
        _record_ok(EVENT_GH_CALL, argv0, duration_s=duration_s)
        return
    _flush_all_ok_aggregates()
    _safe_record(
        event_type=EVENT_GH_CALL,
        summary=f"gh {argv0 or '(no args)'}: {outcome}",
        details={
            "argv0": argv0,
            "outcome": outcome,
            "duration_s": round(duration_s, 3),
            "detail": detail[:200] if detail else "",
        },
    )


def record_ci_check_fetch(
    repo: str,
    number: int,
    *,
    outcome: str,
    duration_s: float,
    conclusions: dict[str, int] | None = None,
    detail: str = "",
) -> None:
    """Best-effort: one row per live (cache-miss) ``gh pr checks`` read --
    except ``"ok"`` outcomes, which accumulate into a per-bucket aggregate
    instead (#2654; see the module docstring).

    ``outcome`` is ``"ok"`` or ``"unreachable"``. ``conclusions`` is the
    check-level conclusion distribution (e.g. ``{"success": 3, "failure":
    1}``) when ``outcome == "ok"`` -- the "check-level conclusion
    distribution" the issue asks for, alongside reachability. ``ok``
    aggregates bucket per ``(repo, number)`` (unlike ``gh_call``'s per-
    ``argv0`` bucketing) and sum ``conclusions`` across the bucket, so a
    single-read bucket reports byte-for-byte the same distribution it always
    did.
    """
    if outcome == "ok":
        _record_ok(
            EVENT_CI_CHECK_FETCH, f"{repo}#{number}", duration_s=duration_s,
            repo=repo, issue=number, conclusions=conclusions,
        )
        return
    _flush_all_ok_aggregates()
    _safe_record(
        event_type=EVENT_CI_CHECK_FETCH,
        summary=f"{repo}#{number}: CI checks {outcome}",
        repo=repo,
        issue=number,
        details={
            "outcome": outcome,
            "duration_s": round(duration_s, 3),
            "conclusions": conclusions or {},
            "detail": detail[:200] if detail else "",
        },
    )


def record_merge_gate_refusal(
    *, repo: str, issue: int | None, reason: str, message: str,
) -> None:
    """Best-effort: one row per live merge-gate CI refusal.

    Only ``reason in MERGE_GATE_REFUSAL_KINDS`` (see that constant) should be
    passed here -- callers filter before calling, this function does not
    re-filter, so it stays a plain unconditional recorder like its siblings.
    """
    _safe_record(
        event_type=EVENT_MERGE_GATE_REFUSAL,
        summary=f"{repo}#{issue}: merge blocked ({reason})",
        repo=repo,
        issue=issue,
        details={"reason": reason, "message": message[:300] if message else ""},
    )


def _safe_record(
    *,
    event_type: str,
    summary: str,
    details: dict[str, Any],
    repo: str | None = None,
    issue: int | None = None,
    ts: float | None = None,
) -> None:
    try:
        record_audit(
            tier="operational",
            category=CATEGORY,
            event_type=event_type,
            actor="system",
            summary=summary,
            repo=repo,
            issue=issue,
            details=details,
            ts=ts,
        )
        _maybe_prune()
    except Exception as exc:  # noqa: BLE001 -- measurement must never affect the caller
        _log.debug("forge_availability: best-effort record failed: %s", exc)


# ── #2654: in-process rollup of "ok" observations ───────────────────────────
#
# ~99% of gh_call/ci_check_fetch observations are "ok" -- pure denominator
# for uptime%, not signal (see module docstring). Rather than one audit_log
# row per "ok" observation, this buffers them in memory and flushes one
# aggregate row per (event_type, key) bucket -- `key` is `argv0` for
# `gh_call`, `"{repo}#{number}"` (one bucket per PR) for `ci_check_fetch`.

# Bucket width. Suggested by the issue; not exposed as a config knob -- like
# _PRUNE_INTERVAL_S below, this is an implementation cheapness knob, not a
# behavior operators need to tune.
_OK_BUCKET_S = 60.0


class _OkAggregate:
    """In-memory accumulator for one bucket's worth of ``outcome="ok"``
    observations.

    ``repo``/``issue`` are carried through for ``ci_check_fetch`` aggregates
    (bucketed per-PR — see ``_record_ok``'s ``key`` for ``EVENT_CI_CHECK_
    FETCH`` — so every observation folded into one aggregate shares the same
    repo/issue, unlike ``gh_call``'s per-``argv0`` bucketing which spans
    whatever repo each call happened to target). ``conclusions_total`` sums
    the check-level conclusion distribution across the bucket -- for a
    single-observation bucket this is byte-for-byte the pre-#2654 per-call
    distribution.
    """

    __slots__ = (
        "conclusions_total", "count", "duration_s_total", "first_ts",
        "issue", "last_ts", "repo",
    )

    def __init__(
        self,
        ts: float,
        duration_s: float,
        *,
        repo: str | None = None,
        issue: int | None = None,
        conclusions: dict[str, int] | None = None,
    ) -> None:
        self.count = 1
        self.duration_s_total = duration_s
        self.first_ts = ts
        self.last_ts = ts
        self.repo = repo
        self.issue = issue
        self.conclusions_total: dict[str, int] = dict(conclusions) if conclusions else {}

    def add(
        self, ts: float, duration_s: float, *, conclusions: dict[str, int] | None = None,
    ) -> None:
        self.count += 1
        self.duration_s_total += duration_s
        self.first_ts = min(self.first_ts, ts)
        self.last_ts = max(self.last_ts, ts)
        for k, v in (conclusions or {}).items():
            self.conclusions_total[k] = self.conclusions_total.get(k, 0) + v

    def to_details(self, *, event_type: str, key: str) -> dict[str, Any]:
        details: dict[str, Any] = {
            "outcome": "ok",
            "count": self.count,
            "duration_s_total": round(self.duration_s_total, 3),
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
        }
        if event_type == EVENT_GH_CALL:
            details["argv0"] = key
        elif event_type == EVENT_CI_CHECK_FETCH:
            details["conclusions"] = self.conclusions_total
        return details


_ok_aggregates: dict[tuple[str, str], _OkAggregate] = {}
_ok_aggregates_lock = threading.Lock()
# A *strong reference* to the connection pending aggregates belong to --
# not id(conn). id() is a memory address; once the previous connection is
# closed and garbage-collected, CPython is free to hand that exact address
# to the very next object allocated (observed in practice: the next test's
# fresh sqlite3.connect(":memory:") landing at the just-freed address), which
# would make an id()-only check silently see "unchanged" across a real
# connection swap. Holding the object itself keeps it alive so identity
# comparison (`is`) stays meaningful for as long as we still need it.
_ok_aggregates_conn: Any = None
_atexit_flush_registered = False


def _register_atexit_flush() -> None:
    global _atexit_flush_registered
    if _atexit_flush_registered:
        return
    _atexit_flush_registered = True
    atexit.register(_flush_all_ok_aggregates)


def _drop_ok_aggregates_if_conn_changed() -> None:
    """Discard (never flush) pending ``ok`` aggregates if the underlying DB
    connection has changed since they started accumulating.

    In production :func:`coord.db.get_connection` returns the same
    singleton for the whole process, so this is a no-op identity check on
    every call. It exists for the one place identity legitimately changes
    mid-process: tests swap in a fresh ``:memory:`` connection per test
    (``coord.db.override_connection``, via the autouse ``coord_db`` fixture
    in ``tests/conftest.py``). Without this, an aggregate accumulated in one
    test would flush into a *later, unrelated* test's database on the next
    bucket roll/atexit -- corrupting that test's audit trail with rows it
    never asked for. Discarding rather than flushing is deliberate: the
    connection that data belonged to is already gone.
    """
    global _ok_aggregates_conn
    try:
        from coord.db import get_connection  # noqa: PLC0415

        conn = get_connection()
    except Exception:  # noqa: BLE001 -- best-effort; assume unchanged on failure
        return
    if _ok_aggregates_conn is not None and conn is not _ok_aggregates_conn:
        with _ok_aggregates_lock:
            _ok_aggregates.clear()
    _ok_aggregates_conn = conn


def _record_ok(
    event_type: str,
    key: str,
    *,
    duration_s: float,
    repo: str | None = None,
    issue: int | None = None,
    conclusions: dict[str, int] | None = None,
) -> None:
    """Accumulate one ``outcome="ok"`` observation into its bucket.

    Best-effort like every other entry point in this module: bucket
    bookkeeping is a handful of dict/lock operations, but a caller here must
    never see an exception regardless.
    """
    try:
        _register_atexit_flush()
        _drop_ok_aggregates_if_conn_changed()
        now = time.time()
        rolled: tuple[tuple[str, str], _OkAggregate] | None = None
        bucket_key = (event_type, key)
        with _ok_aggregates_lock:
            agg = _ok_aggregates.get(bucket_key)
            if agg is None:
                _ok_aggregates[bucket_key] = _OkAggregate(
                    now, duration_s, repo=repo, issue=issue, conclusions=conclusions,
                )
            elif now - agg.first_ts >= _OK_BUCKET_S:
                rolled = (bucket_key, agg)
                _ok_aggregates[bucket_key] = _OkAggregate(
                    now, duration_s, repo=repo, issue=issue, conclusions=conclusions,
                )
            else:
                agg.add(now, duration_s, conclusions=conclusions)
        if rolled is not None:
            _flush_ok_aggregate(*rolled)
    except Exception as exc:  # noqa: BLE001 -- measurement must never affect the caller
        _log.debug("forge_availability: ok-aggregate bookkeeping failed: %s", exc)


def _flush_ok_aggregate(bucket_key: tuple[str, str], agg: _OkAggregate) -> None:
    event_type, key = bucket_key
    details = agg.to_details(event_type=event_type, key=key)
    if event_type == EVENT_GH_CALL:
        summary = f"gh {key or '(no args)'}: ok x{agg.count}"
    else:
        summary = f"{agg.repo}#{agg.issue}: CI checks ok x{agg.count}"
    _safe_record(
        event_type=event_type, summary=summary, details=details, ts=agg.last_ts,
        repo=agg.repo, issue=agg.issue,
    )


def _flush_all_ok_aggregates() -> None:
    """Flush every pending ``ok`` aggregate right now.

    Called on bucket roll (per-bucket, above), at process exit, and before
    every non-``ok`` observation is recorded -- the last of those is what
    guarantees an aggregate row never lands, chronologically, after an event
    it actually precedes (module docstring).
    """
    try:
        _drop_ok_aggregates_if_conn_changed()
        with _ok_aggregates_lock:
            pending = list(_ok_aggregates.items())
            _ok_aggregates.clear()
    except Exception as exc:  # noqa: BLE001 -- measurement must never affect the caller
        _log.debug("forge_availability: ok-aggregate flush-all failed: %s", exc)
        return
    for bucket_key, agg in pending:
        _flush_ok_aggregate(bucket_key, agg)


def _maybe_prune(*, force: bool = False) -> None:
    """Delete ``forge_availability`` rows older than :data:`RETENTION_DAYS`.

    Throttled to once per :data:`_PRUNE_INTERVAL_S` per process (``force``
    bypasses the throttle, for tests) -- see the module docstring for why a
    DELETE scan on every single write would defeat the point of this module
    being cheap enough to fire on every ``gh`` call.
    """
    global _last_prune_at
    now = time.time()
    if not force and (now - _last_prune_at) < _PRUNE_INTERVAL_S:
        return
    _last_prune_at = now
    try:
        from coord.db import get_connection  # noqa: PLC0415

        cutoff = now - RETENTION_DAYS * 86400.0
        conn = get_connection()
        conn.execute(
            "DELETE FROM audit_log WHERE category = ? AND ts < ?",
            (CATEGORY, cutoff),
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001 -- best-effort, never blocks a caller
        _log.debug("forge_availability: prune sweep failed: %s", exc)


# ── Read side: the `coord diagnose --forge-availability` read-out ──────────


class AvailabilityReport:
    """Summary of forge/CI availability over a trailing window.

    ``uptime_pct`` and ``longest_unavailable_stretch_s`` are computed over
    the merged, time-ordered sequence of ``gh_call`` + ``ci_check_fetch``
    observations -- this is an *observation-based* signal, not a continuous
    heartbeat: a stretch with no forge calls at all (nights, a quiet repo)
    is not a gap in availability, it's a gap in *observations*, and
    "longest unavailable stretch" only measures contiguous runs of
    observations that came back unavailable, not wall-clock silence.
    """

    def __init__(
        self,
        *,
        window_days: float,
        since: float,
        until: float,
        gh_calls: int,
        ci_fetches: int,
        available: int,
        unavailable: int,
        longest_unavailable_stretch_s: float,
        refusals_by_reason: dict[str, int],
        truncated: bool,
    ) -> None:
        self.window_days = window_days
        self.since = since
        self.until = until
        self.gh_calls = gh_calls
        self.ci_fetches = ci_fetches
        self.available = available
        self.unavailable = unavailable
        self.longest_unavailable_stretch_s = longest_unavailable_stretch_s
        self.refusals_by_reason = refusals_by_reason
        self.truncated = truncated

    @property
    def total_observations(self) -> int:
        return self.available + self.unavailable

    @property
    def uptime_pct(self) -> float | None:
        if self.total_observations == 0:
            return None
        return 100.0 * self.available / self.total_observations

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_days": self.window_days,
            "since": self.since,
            "until": self.until,
            "gh_calls": self.gh_calls,
            "ci_fetches": self.ci_fetches,
            "total_observations": self.total_observations,
            "available": self.available,
            "unavailable": self.unavailable,
            "uptime_pct": self.uptime_pct,
            "longest_unavailable_stretch_s": self.longest_unavailable_stretch_s,
            "refusals_by_reason": dict(self.refusals_by_reason),
            "truncated": self.truncated,
        }


# Safety cap on how many audit rows a single report will page through --
# bounds worst case cost the same way `coord.audit.query_audit_log`'s own
# MAX_LIMIT bounds a single page; a window with more observations than this
# reports `truncated=True` rather than paging forever.
_MAX_REPORT_ROWS = 20_000


def availability_report(
    *, window_days: float = 30.0, now: float | None = None,
) -> AvailabilityReport:
    """Summarize forge/CI availability over the trailing *window_days*.

    Read-only; queries the local ``audit_log`` via
    :func:`coord.audit.query_audit_log`, paginating until the window is
    exhausted or :data:`_MAX_REPORT_ROWS` is hit (``truncated=True`` in the
    latter case, reported rather than hidden).
    """
    now = now if now is not None else time.time()
    since = now - window_days * 86400.0

    entries: list[dict[str, Any]] = []
    cursor: str | None = None
    truncated = False
    while True:
        page = query_audit_log(
            since=since, until=now, category=CATEGORY,
            limit=_AUDIT_MAX_LIMIT, cursor=cursor,
        )
        entries.extend(page["entries"])
        if len(entries) >= _MAX_REPORT_ROWS:
            truncated = bool(page["has_more"])
            break
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]

    observations = [
        e for e in entries if e["event_type"] in (EVENT_GH_CALL, EVENT_CI_CHECK_FETCH)
    ]
    # query_audit_log returns newest-first; availability math wants
    # chronological order so "contiguous" means "contiguous in time".
    observations.sort(key=lambda e: (e["ts"], e["id"]))

    # #2654: an "ok" observation may be a rolled-up aggregate row standing in
    # for `count` individual observations (module docstring) rather than
    # one row each -- every sum below weights by `details["count"]`, which
    # defaults to 1 for the non-aggregated rows (every non-"ok" outcome,
    # plus any pre-#2654 "ok" row still inside the retention window).
    gh_calls = sum(
        (e.get("details") or {}).get("count", 1)
        for e in observations if e["event_type"] == EVENT_GH_CALL
    )
    ci_fetches = sum(
        (e.get("details") or {}).get("count", 1)
        for e in observations if e["event_type"] == EVENT_CI_CHECK_FETCH
    )

    available = 0
    unavailable = 0
    longest_stretch = 0.0
    run_start_ts: float | None = None
    run_end_ts: float | None = None
    for e in observations:
        details = e.get("details") or {}
        outcome = details.get("outcome")
        weight = details.get("count", 1)
        duration_s = details.get("duration_s") or 0.0
        is_available = outcome in _AVAILABLE_OUTCOMES
        if is_available:
            # Aggregate rows are always "ok" (never written for an
            # unavailable outcome), so this branch is the only one a
            # weight > 1 ever reaches -- the contiguous-unavailable-run
            # math below stays per-observation, unaffected by rollup.
            available += weight
            if run_start_ts is not None:
                longest_stretch = max(longest_stretch, (run_end_ts or run_start_ts) - run_start_ts)
            run_start_ts = None
            run_end_ts = None
        else:
            unavailable += weight
            if run_start_ts is None:
                run_start_ts = e["ts"]
            run_end_ts = e["ts"] + duration_s
    if run_start_ts is not None:
        longest_stretch = max(longest_stretch, (run_end_ts or run_start_ts) - run_start_ts)

    refusals_by_reason: dict[str, int] = {}
    for e in entries:
        if e["event_type"] != EVENT_MERGE_GATE_REFUSAL:
            continue
        reason = (e.get("details") or {}).get("reason", "unknown")
        refusals_by_reason[reason] = refusals_by_reason.get(reason, 0) + 1

    return AvailabilityReport(
        window_days=window_days,
        since=since,
        until=now,
        gh_calls=gh_calls,
        ci_fetches=ci_fetches,
        available=available,
        unavailable=unavailable,
        longest_unavailable_stretch_s=longest_stretch,
        refusals_by_reason=refusals_by_reason,
        truncated=truncated,
    )


def _format_duration(seconds: float) -> str:
    if seconds <= 0:
        return "0s"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def format_report_lines(report: AvailabilityReport) -> list[str]:
    """Human-readable report lines for ``coord diagnose --forge-availability``."""
    lines: list[str] = []
    uptime = report.uptime_pct
    if uptime is None:
        lines.append(
            f"no forge/CI observations in the trailing {report.window_days:.0f}d "
            "(nothing has called `gh` or read CI checks yet in this window)"
        )
    else:
        lines.append(
            f"uptime: {uptime:.2f}% ({report.available}/{report.total_observations} "
            f"observations available) over the trailing {report.window_days:.0f}d"
        )
        lines.append(
            f"observations: {report.gh_calls} gh call(s), {report.ci_fetches} CI check-fetch(es)"
        )
        lines.append(
            f"longest unavailable stretch: {_format_duration(report.longest_unavailable_stretch_s)}"
        )
    if report.refusals_by_reason:
        lines.append("merge-gate refusals by reason:")
        for reason in sorted(report.refusals_by_reason):
            lines.append(f"  {reason}: {report.refusals_by_reason[reason]}")
    else:
        lines.append("merge-gate refusals by reason: none")
    if report.truncated:
        lines.append(
            f"⚠ truncated at {_MAX_REPORT_ROWS} rows -- narrow --window-days for exact figures"
        )
    return lines


def summary_line(report: AvailabilityReport) -> str:
    """Machine-parseable trailer line, same family as ``GRAPH_HEALTH:``."""
    uptime = report.uptime_pct
    uptime_str = f"{uptime:.2f}" if uptime is not None else "n/a"
    refusals_total = sum(report.refusals_by_reason.values())
    return (
        f"FORGE_AVAILABILITY: window_days={report.window_days:.0f} "
        f"observations={report.total_observations} uptime_pct={uptime_str} "
        f"longest_outage_s={report.longest_unavailable_stretch_s:.0f} "
        f"refusals_total={refusals_total} truncated={report.truncated}"
    )
