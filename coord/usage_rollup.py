"""Usage Core (#1118): pure cost/token/duration aggregation over board rows.

This module is a **pure** aggregator — every function here takes plain data
in (board assignment rows as ``dict``s, matching the daemon ``/board``
``assignments`` wire shape) and returns plain data out. No I/O, no daemon
calls, no filesystem reads. That separation is deliberate: it's the anchor of
the Gate-A acceptance contract (``tests/acceptance/ms-37/contract.md``) —
seeded rows in, an exact rollup out — and it's what lets CLI-1 (#1115),
CLI-2 (#1119), and the TUI view (#1116) all share one aggregation path
instead of three reimplementations. The *fetch* side (reading the daemon
board via ``coord.board_service`` / ``resolve_board_service``, the same read
path ``coord status`` uses) is a thin, separate caller — see
``coord.usage.fetch_usage_rows`` — deliberately kept out of this module.

Row fields consumed (all optional except where noted; missing/None fields
degrade gracefully rather than raising): ``issue_number``, ``issue_title``,
``repo_name``, ``type``, ``model``, ``is_interactive``, ``status``,
``cost_usd``, ``input_tokens``, ``output_tokens``, ``cache_read_tokens``,
``cache_creation_tokens``, ``num_turns`` (#2786), ``dispatched_at``,
``finished_at``.

Grouping dimensions (``group_by=``): ``"issue"`` (keyed by repo+issue number
in the internal :func:`rollup` path — see :func:`_agg_key` for how the
sealed :func:`aggregate` entry point differs), ``"repo"``, ``"day"``,
``"week"`` (ISO week), ``"month"``, and ``"stage"`` (keyed by ``type`` — the
per-stage-type sub-rollup for the time view, see :func:`rollup_by_stage`).

Time window: a resolved half-open ``[start, end)`` interval (see
:class:`TimeWindow`). ``today``/``week``/``month``/``since=<ISO|Nd|Nh>`` are
*presets* that compute one; :class:`TimeWindow` itself is the general case
(explicit ``start``/``end``) so callers like the TUI's arbitrary range picker
(#1116) aren't limited to the presets. A leg is in-window if its
``dispatched_at`` **or** ``finished_at`` falls in the interval — a leg that
started before the window but finished inside it (or vice versa) still
counts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, NamedTuple

from coord.config import ModelRates, PricingConfig

# ── Model normalization ──────────────────────────────────────────────────────

# Canonical keys this module recognizes out of the box. Anything else is
# "(unknown)" — never guessed, never silently priced at $0.
_KNOWN_CANONICAL = ("sonnet", "opus", "haiku", "fable")

UNKNOWN_MODEL = "(unknown)"


def normalize_model(model: str | None) -> str:
    """Normalize a raw ``model`` field to a canonical pricing key.

    Handles bare aliases (``"sonnet"``, ``"opus"``, ``"haiku"``, ``"fable"``),
    versioned ids (``"claude-sonnet-4-6"``, ``"claude-opus-4-7"``,
    ``"claude-haiku-4-5"``, ``"claude-fable-5"``, and future dated variants —
    matched by substring so a new date suffix doesn't need a code change),
    and the empty/``None``/``"(unknown)"`` cases. Anything that doesn't match
    one of the four known tiers returns ``"(unknown)"`` — the estimator
    treats that as "no rate available" and flags it, rather than defaulting
    to a tier that might be wrong.
    """
    if not model:
        return UNKNOWN_MODEL
    text = str(model).strip().lower()
    if not text or text == UNKNOWN_MODEL:
        return UNKNOWN_MODEL
    if text in _KNOWN_CANONICAL:
        return text
    for canonical in _KNOWN_CANONICAL:
        if canonical in text:
            return canonical
    return UNKNOWN_MODEL


# ── Timestamp parsing ─────────────────────────────────────────────────────────


def parse_timestamp(value: Any) -> float | None:
    """Parse a row timestamp field to a Unix float.

    Accepts a Unix float/int directly, an ISO-8601 string (``datetime.
    fromisoformat``, tolerating a trailing ``Z``), or a numeric string.
    Returns ``None`` for ``None``, empty strings, and unparseable values —
    callers treat "no timestamp" as "not in any window" / "not orderable"
    rather than raising.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            pass
        try:
            iso = text[:-1] + "+00:00" if text.endswith("Z") else text
            return datetime.fromisoformat(iso).timestamp()
        except ValueError:
            return None
    return None


def _to_int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ── Time window ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TimeWindow:
    """A resolved half-open ``[start, end)`` interval, in Unix time.

    This is the general case: pass explicit ``start``/``end`` (either may be
    ``None`` for an unbounded side) for an arbitrary range — e.g. the TUI
    range picker (#1116), or a CLI ``--since/--until`` pair. The
    ``today``/``week``/``month``/``since`` module-level functions below are
    *presets* that compute one of these; they are not separate modes.

    ``label`` is a human-readable rendering of the resolved range (e.g.
    ``"today"`` or ``"2026-07-01 00:00 -> 2026-07-08 00:00"``) for the
    surfaces that print a window header — purely descriptive, not consumed
    by any predicate here.
    """

    start: float | None = None
    end: float | None = None
    label: str = ""

    def contains(self, ts: float | None) -> bool:
        """Whether *ts* falls in ``[start, end)``. ``None`` is never in-window."""
        if ts is None:
            return False
        if self.start is not None and ts < self.start:
            return False
        if self.end is not None and ts >= self.end:
            return False
        return True


def _local_day_bounds(dt: datetime) -> tuple[datetime, datetime]:
    start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def _resolve_now(now: float | None) -> datetime:
    return datetime.fromtimestamp(now) if now is not None else datetime.now()


def window_today(now: float | None = None) -> TimeWindow:
    """Preset: local calendar day containing *now* (or the real "now")."""
    start, end = _local_day_bounds(_resolve_now(now))
    return TimeWindow(start=start.timestamp(), end=end.timestamp(), label="today")


def window_week(now: float | None = None) -> TimeWindow:
    """Preset: current ISO week (Monday 00:00 local -> next Monday 00:00)."""
    day_start, _ = _local_day_bounds(_resolve_now(now))
    monday = day_start - timedelta(days=day_start.weekday())
    return TimeWindow(
        start=monday.timestamp(), end=(monday + timedelta(days=7)).timestamp(), label="week"
    )


def window_month(now: float | None = None) -> TimeWindow:
    """Preset: current calendar month (1st 00:00 local -> 1st of next month)."""
    dt = _resolve_now(now)
    start = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return TimeWindow(start=start.timestamp(), end=end.timestamp(), label="month")


_SINCE_RELATIVE_RE = re.compile(r"^(\d+)\s*([dh])$", re.IGNORECASE)


def window_since(spec: str, now: float | None = None) -> TimeWindow:
    """Preset: open-ended window starting at *spec* (``<ISO>``, ``Nd``, or ``Nh``).

    ``end`` is left unbounded (``None``) — everything from ``start`` onward
    is in-window, including legs that haven't finished yet.
    """
    now_dt = _resolve_now(now)
    match = _SINCE_RELATIVE_RE.match(spec.strip())
    if match:
        amount = int(match.group(1))
        unit = match.group(2).lower()
        delta = timedelta(days=amount) if unit == "d" else timedelta(hours=amount)
        start_dt = now_dt - delta
    else:
        parsed = parse_timestamp(spec)
        if parsed is None:
            raise ValueError(f"invalid 'since' spec: {spec!r} (expected ISO date, 'Nd', or 'Nh')")
        start_dt = datetime.fromtimestamp(parsed)
    return TimeWindow(start=start_dt.timestamp(), end=None, label=f"since {spec}")


def leg_in_window(row: dict, window: TimeWindow) -> bool:
    """Whether *row* is in-window: ``dispatched_at`` OR ``finished_at`` in range."""
    dispatched = parse_timestamp(row.get("dispatched_at"))
    finished = parse_timestamp(row.get("finished_at"))
    return window.contains(dispatched) or window.contains(finished)


# ── Per-leg cost + duration ──────────────────────────────────────────────────


def leg_cost(row: dict, pricing: PricingConfig) -> tuple[float, float, bool]:
    """Compute ``(cost_captured, cost_est, unknown_model)`` for one leg.

    A leg with a real captured ``cost_usd`` (non-null, non-zero) keeps it
    verbatim as ``cost_captured`` and never also gets an estimate (no
    double-counting). A leg with ``cost_usd`` in ``{None, 0}`` **and** any
    tokens gets an estimate from *pricing*, keyed by the leg's normalized
    model — unless the model doesn't map to a priced tier, in which case no
    estimate is produced and ``unknown_model`` is ``True`` (never a silent
    $0). A leg with no tokens and no captured cost is simply zero everywhere.
    """
    raw_cost = row.get("cost_usd")
    try:
        captured = float(raw_cost) if raw_cost not in (None, "") else 0.0
    except (TypeError, ValueError):
        captured = 0.0
    if captured:
        return captured, 0.0, False

    input_tokens = _to_int(row.get("input_tokens"))
    output_tokens = _to_int(row.get("output_tokens"))
    cache_read_tokens = _to_int(row.get("cache_read_tokens"))
    cache_creation_tokens = _to_int(row.get("cache_creation_tokens"))
    total_tokens = input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens
    if total_tokens <= 0:
        return 0.0, 0.0, False

    canonical = normalize_model(row.get("model"))
    rates: ModelRates | None = pricing.rates_for(canonical)
    if rates is None:
        return 0.0, 0.0, True

    est = (
        input_tokens * rates.input
        + output_tokens * rates.output
        + cache_read_tokens * rates.cache_read
        + cache_creation_tokens * rates.cache_creation
    ) / 1_000_000.0
    return 0.0, est, False


def leg_duration(row: dict) -> tuple[float, bool]:
    """Compute ``(duration_secs, is_open)`` for one leg.

    ``is_open`` is ``True`` when there's no ``finished_at`` (still running):
    duration contributes ``0`` and the leg is counted separately so the time
    view can note "N in progress." Otherwise duration is
    ``max(0, finished_at - dispatched_at)`` — clamped so a clock skew or bad
    data pair never goes negative.
    """
    finished = parse_timestamp(row.get("finished_at"))
    if finished is None:
        return 0.0, True
    dispatched = parse_timestamp(row.get("dispatched_at"))
    if dispatched is None:
        return 0.0, False
    return max(0.0, finished - dispatched), False


# ── Grouping ──────────────────────────────────────────────────────────────────


class IssueKey(NamedTuple):
    """Group key for ``group_by="issue"`` — an issue is scoped to its repo."""

    repo_name: str
    issue_number: int


_VALID_GROUP_BY = ("issue", "repo", "day", "week", "month", "stage")


def _leg_group_timestamp(row: dict) -> float | None:
    """Timestamp used to bucket a leg into a day/week/month group.

    Prefers ``dispatched_at`` (when the work started) and falls back to
    ``finished_at`` so a leg with only one of the two timestamps still
    lands somewhere instead of being silently dropped from time-based
    grouping.
    """
    ts = parse_timestamp(row.get("dispatched_at"))
    if ts is None:
        ts = parse_timestamp(row.get("finished_at"))
    return ts


def row_issue_number(row: dict) -> int:
    """The issue a leg's cost is attributed to (#1553).

    ``for_issue_number`` when the row carries one (an oracle-loop acceptance
    slice and everything derived from it — its ``issue_number`` is the
    milestone's *tracking* issue, so grouping on that booked every child's
    authoring spend to the epic), else ``issue_number``. Rows without the
    key — including the sealed ms-37 fixtures — are unaffected.

    Mirrors :func:`coord.models.effective_issue_number`, kept local so this
    module stays a pure dict-in/dict-out aggregator with no model import.
    """
    for key in ("for_issue_number", "issue_number"):
        raw = row.get(key)
        if raw is None or raw == "":
            continue
        return _to_int(raw)
    return 0


def _group_key_for(row: dict, group_by: str) -> Any:
    if group_by == "issue":
        return IssueKey(
            repo_name=str(row.get("repo_name") or ""),
            issue_number=row_issue_number(row),
        )
    if group_by == "repo":
        return str(row.get("repo_name") or "")
    if group_by == "stage":
        return str(row.get("type") or "work")

    ts = _leg_group_timestamp(row)
    if ts is None:
        return None
    dt = datetime.fromtimestamp(ts)
    if group_by == "day":
        return dt.date().isoformat()
    if group_by == "week":
        iso_year, iso_week, _ = dt.isocalendar()
        return f"{iso_year:04d}-W{iso_week:02d}"
    if group_by == "month":
        return f"{dt.year:04d}-{dt.month:02d}"
    raise ValueError(f"unknown group_by: {group_by!r} (expected one of {_VALID_GROUP_BY})")


# ── Rollup result ─────────────────────────────────────────────────────────────


@dataclass
class TokenTotals:
    """Token sums for one group or the overall total."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_creation: int = 0


@dataclass
class GroupRollup:
    """Aggregated cost/token/duration numbers for one group (or the grand total)."""

    key: Any
    legs: int = 0
    cost_captured: float = 0.0
    cost_est: float = 0.0
    tokens: TokenTotals = field(default_factory=TokenTotals)
    duration_secs: float = 0.0
    open_legs: int = 0
    unknown_model_legs: int = 0
    # #2786: sum of `num_turns` across every leg in the group — the other
    # half of "context size vs turn count" (cache-read tokens are the
    # numerator, turns is the denominator). 0 for rows predating the
    # `assignments.num_turns` column, same convention as the token totals.
    turns: int = 0
    # Retained per-leg rows for drill-down (e.g. CLI-1's `coord usage --issue N`
    # per-stage breakdown). Rows appear in the order they were accumulated.
    leg_rows: list[dict] = field(default_factory=list)

    @property
    def cost_total(self) -> float:
        """Captured + estimated cost — never double-counts a leg (see :func:`leg_cost`)."""
        return self.cost_captured + self.cost_est

    @property
    def has_unknown_model(self) -> bool:
        return self.unknown_model_legs > 0


@dataclass
class RollupResult:
    """The full result of :func:`rollup`: the resolved window, dimension, and groups."""

    window: TimeWindow
    group_by: str
    groups: dict[Any, GroupRollup]
    total: GroupRollup


def _accumulate(group: GroupRollup, row: dict, pricing: PricingConfig) -> None:
    captured, est, unknown_model = leg_cost(row, pricing)
    duration, is_open = leg_duration(row)

    group.legs += 1
    group.cost_captured += captured
    group.cost_est += est
    group.tokens.input += _to_int(row.get("input_tokens"))
    group.tokens.output += _to_int(row.get("output_tokens"))
    group.tokens.cache_read += _to_int(row.get("cache_read_tokens"))
    group.tokens.cache_creation += _to_int(row.get("cache_creation_tokens"))
    group.turns += _to_int(row.get("num_turns"))
    group.duration_secs += duration
    if is_open:
        group.open_legs += 1
    if unknown_model:
        group.unknown_model_legs += 1
    group.leg_rows.append(row)


def rollup(
    rows: Iterable[dict],
    *,
    group_by: str,
    window: TimeWindow,
    pricing: PricingConfig | None = None,
) -> RollupResult:
    """Aggregate *rows* into per-group cost/token/duration rollups.

    Only rows in-window (see :func:`leg_in_window`) are counted at all — an
    out-of-window leg contributes to nothing, including the grand total.
    *pricing* defaults to the built-in :class:`~coord.config.PricingConfig`
    defaults when omitted (callers that loaded ``coordinator.yml`` should
    pass ``config.pricing`` instead).
    """
    if group_by not in _VALID_GROUP_BY:
        raise ValueError(f"unknown group_by: {group_by!r} (expected one of {_VALID_GROUP_BY})")
    if pricing is None:
        pricing = PricingConfig()

    groups: dict[Any, GroupRollup] = {}
    total = GroupRollup(key=None)

    for row in rows:
        if not leg_in_window(row, window):
            continue
        key = _group_key_for(row, group_by)
        if key is None:
            # No orderable timestamp for a day/week/month bucket — excluded
            # rather than silently lumped into a bogus "unknown" bucket.
            continue
        group = groups.setdefault(key, GroupRollup(key=key))
        _accumulate(group, row, pricing)
        _accumulate(total, row, pricing)

    return RollupResult(window=window, group_by=group_by, groups=groups, total=total)


def rollup_by_stage(
    rows: Iterable[dict],
    window: TimeWindow,
    pricing: PricingConfig | None = None,
) -> RollupResult:
    """Per-stage-type (work/smoke/review/conflict-fix/chat/test-author) rollup.

    This is the "where is time spent" answer for the time view — a thin,
    named alias for ``rollup(rows, group_by="stage", ...)`` so callers don't
    need to know the dimension's string key.
    """
    return rollup(rows, group_by="stage", window=window, pricing=pricing)


# ── Public contract API (Gate-A / ms-37 acceptance surface) ───────────────────
# Stable, sealed names consumed by tests/acceptance/**, CLI-1 (#1115),
# CLI-2 (#1119), and TUI (#1116).  The internal names above (TimeWindow,
# leg_cost, normalize_model, leg_in_window) remain unchanged so existing
# callers don't break.

canonical_model = normalize_model
"""Alias for :func:`normalize_model` — public Gate-A name."""

in_window = leg_in_window
"""Alias for :func:`leg_in_window` — public Gate-A name."""


@dataclass(frozen=True)
class Window(TimeWindow):
    """Half-open ``[start, end)`` interval — public API alias for :class:`TimeWindow`.

    ``Window(start, end)`` constructs a plain interval.  The two class methods
    below add preset constructors with *bounded* semantics — both ``start`` and
    ``end`` are always set, which is why ``Window.since("2d")`` differs from the
    module-level :func:`window_since` (which leaves ``end=None``).
    """

    @classmethod
    def since(cls, spec: str, now: float | None = None) -> "Window":
        """Half-open ``[start, now)`` window.

        *spec* is a relative duration (``Nd`` / ``Nh``) or an ISO-8601 instant
        for ``start``; ``end`` is always anchored to *now* (unlike the
        module-level :func:`window_since` which leaves ``end`` unbounded).
        """
        dt_now = _resolve_now(now)
        now_ts = dt_now.timestamp()
        match = _SINCE_RELATIVE_RE.match(spec.strip())
        if match:
            amount = int(match.group(1))
            unit = match.group(2).lower()
            delta = timedelta(days=amount) if unit == "d" else timedelta(hours=amount)
            start_ts = (dt_now - delta).timestamp()
        else:
            parsed = parse_timestamp(spec)
            if parsed is None:
                raise ValueError(
                    f"invalid 'since' spec: {spec!r} (expected ISO date, 'Nd', or 'Nh')"
                )
            start_ts = parsed
        return cls(start=start_ts, end=now_ts, label=f"since {spec}")

    @classmethod
    def today(cls, now: float | None = None) -> "Window":
        """Preset: current local calendar day ``[midnight, midnight+1d)``."""
        base = window_today(now)
        return cls(start=base.start, end=base.end, label=base.label)


def estimate_leg_cost(row: dict, pricing: dict) -> float | None:
    """Estimate the token-based cost of one leg.

    *pricing* is a plain ``dict`` keyed by canonical model name, e.g.::

        {"sonnet": {"input": 3.00, "output": 15.00,
                    "cache_read": 0.30, "cache_creation": 3.75}, ...}

    Rates are per 1 M tokens.  Returns the estimated cost as a ``float``
    (possibly ``0.0`` for a mapped model with zero tokens), or ``None`` when
    the leg's model is not a key in *pricing* — never a silent ``$0`` for an
    unmapped model.  Captured ``cost_usd`` is **not** consulted here; the
    caller decides whether to use captured or estimated cost.
    """
    key = canonical_model(row.get("model"))
    rates = pricing.get(key)
    if rates is None:
        return None
    return (
        _to_int(row.get("input_tokens")) * rates["input"]
        + _to_int(row.get("output_tokens")) * rates["output"]
        + _to_int(row.get("cache_read_tokens")) * rates["cache_read"]
        + _to_int(row.get("cache_creation_tokens")) * rates["cache_creation"]
    ) / 1_000_000.0


# ── aggregate() helpers ───────────────────────────────────────────────────────


def _agg_key(row: dict, by: str) -> Any:
    """Group key for :func:`aggregate`.

    For ``by="issue"`` the key is the bare ``issue_number`` integer (unlike
    the internal :func:`rollup` which uses an :class:`IssueKey` named-tuple so
    it can distinguish the same number across repos).  All other dimensions
    delegate to :func:`_group_key_for`.

    KNOWN LIMITATION (#1118 review, tracked, not fixed here): because the key
    is a bare int, two different repos' issue ``#N`` collide into one
    ``"by issue"`` group — GitHub issue numbers are per-repo, not globally
    unique, and ``coordinator.yml`` is explicitly multi-repo. This matches
    the sealed Gate-A acceptance contract's mock output shape
    (``tests/acceptance/ms-37/contract.md``, a single ``issue`` column) and
    the fixture's issue numbers (501/502) happen not to collide, so it isn't
    caught by the sealed suite. Each group's ``"rows"`` list still carries
    each row's real ``repo_name``, so a caller (CLI-1/#1115, CLI-2/#1119,
    TUI/#1116) that cares about repo scoping for ``by="issue"`` can recover
    it from there today; a real fix would need a repo-qualified key (e.g.
    ``f"{repo_name}#{issue_number}"``) *and* a matching sealed-contract
    update, which is out of scope for this PR.
    """
    if by == "issue":
        # #1553: attributed issue, not the raw column — see row_issue_number.
        return row_issue_number(row)
    return _group_key_for(row, by)


def _empty_agg_group(key: Any) -> dict:
    return {
        "key": key,
        "legs": 0,
        "cost_captured": 0.0,
        "cost_est": 0.0,
        "cost_total": 0.0,
        "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0},
        "duration_secs": 0.0,
        "open_legs": 0,
        "unknown_models": 0,
        "rows": [],
    }


def _accumulate_agg(group: dict, row: dict, pricing: dict) -> None:
    """Accumulate one *row* into *group* and update all numeric fields."""
    raw_cost = row.get("cost_usd")
    try:
        captured = float(raw_cost) if raw_cost not in (None, "") else 0.0
    except (TypeError, ValueError):
        captured = 0.0

    if captured:
        group["cost_captured"] += captured
    else:
        est = estimate_leg_cost(row, pricing)
        if est is not None:
            group["cost_est"] += est
        else:
            # Unmapped model + tokens → flag; neither captured nor estimated.
            total_tok = (
                _to_int(row.get("input_tokens"))
                + _to_int(row.get("output_tokens"))
                + _to_int(row.get("cache_read_tokens"))
                + _to_int(row.get("cache_creation_tokens"))
            )
            if total_tok > 0:
                group["unknown_models"] += 1

    # Token sums include ALL in-window legs regardless of model mapping.
    group["tokens"]["input"] += _to_int(row.get("input_tokens"))
    group["tokens"]["output"] += _to_int(row.get("output_tokens"))
    group["tokens"]["cache_read"] += _to_int(row.get("cache_read_tokens"))
    group["tokens"]["cache_creation"] += _to_int(row.get("cache_creation_tokens"))

    dur, is_open = leg_duration(row)
    group["duration_secs"] += dur
    if is_open:
        group["open_legs"] += 1

    group["legs"] += 1
    group["rows"].append(row)


def aggregate(
    rows: Iterable[dict],
    *,
    by: str,
    window: TimeWindow,
    pricing: dict,
) -> dict:
    """Aggregate *rows* into a plain rollup dict — the Gate-A public surface.

    Parameters
    ----------
    rows:
        Iterable of assignment row dicts (daemon ``/board`` wire format).
    by:
        Grouping dimension: ``"issue"``, ``"repo"``, ``"day"``, ``"week"``,
        ``"month"``, or ``"stage"``.
    window:
        Half-open ``[start, end)`` interval.  Any :class:`TimeWindow` (or
        :class:`Window`) is accepted.  Only rows whose ``dispatched_at``
        **or** ``finished_at`` falls inside are counted.
    pricing:
        Plain ``dict`` keyed by canonical model name with per-1M-token rates,
        e.g. ``{"sonnet": {"input": 3.00, "output": 15.00,
        "cache_read": 0.30, "cache_creation": 3.75}}``.

    Returns
    -------
    dict
        ``"by"``:  the *by* dimension string.
        ``"groups"``:  list of group dicts sorted **descending** by
        ``cost_total`` (``cost_captured + cost_est``).  Each group dict has
        keys ``key``, ``legs``, ``cost_captured``, ``cost_est``,
        ``cost_total``, ``tokens`` (dict), ``duration_secs``, ``open_legs``,
        ``unknown_models``, ``rows``.
        ``"totals"``:  a single dict with the same numeric keys summed across
        all in-window legs.
    """
    if by not in _VALID_GROUP_BY:
        raise ValueError(f"unknown 'by': {by!r} (expected one of {_VALID_GROUP_BY})")

    groups: dict[Any, dict] = {}
    totals = _empty_agg_group(None)

    for row in rows:
        if not in_window(row, window):
            continue
        key = _agg_key(row, by)
        if key is None:
            continue
        if key not in groups:
            groups[key] = _empty_agg_group(key)
        _accumulate_agg(groups[key], row, pricing)
        _accumulate_agg(totals, row, pricing)

    # Sort groups descending by total cost and materialise the derived field.
    sorted_groups = sorted(
        groups.values(),
        key=lambda g: g["cost_captured"] + g["cost_est"],
        reverse=True,
    )
    for g in sorted_groups:
        g["cost_total"] = g["cost_captured"] + g["cost_est"]
    totals["cost_total"] = totals["cost_captured"] + totals["cost_est"]

    return {"by": by, "groups": sorted_groups, "totals": totals}
