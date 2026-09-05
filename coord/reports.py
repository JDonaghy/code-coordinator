"""Report engine (#1742) — a registry of named, parameterised reports folded
out of the coordinator's own history.

The point is to stop paying an Opus coordinator session to hand-roll the same
aggregation every morning.  "What did the fleet do overnight, and where did it
all end up?" is pure deterministic arithmetic over the audit trail (#1036 /
#1037) — this module makes it a `coord report run` away, and reproducible.

Three layers, deliberately separated so the interesting one is testable:

1. :func:`fold_issue_activity` — **pure**.  Takes already-fetched audit
   entries plus an explicit ``(start, end)`` window and returns a
   :class:`ReportResult`.  No daemon, no DB, no clock (``generated_at``
   defaults to the window end).  This is where every derivation lives, and
   it unit-tests against fixture events.
2. :func:`fetch_audit_window` — pagination.  The audit read path hard-caps a
   single call at 500 rows (``coord.audit.MAX_LIMIT``); that is a *page
   size*, not a window bound, so this walks the keyset cursor until the
   window is covered and reports ``truncated=True`` if it genuinely could
   not finish.  Never silently drops the tail (#1742: "no silent caps").
3. :data:`REPORTS` + :func:`run_report` — the registry and its parameter
   validation.  Seven entries: ``issue-activity``; ``completed`` (#2454 /
   #2472), one row per issue that FINISHED in a window rather than one row
   per audit event, joined with each issue's lifetime legs/tokens/cost;
   ``drive-queue-status`` (#1805), a **live snapshot** of ``drive_queue`` (no
   window, no audit trail, no clock beyond ``generated_at``) rather than a
   fold over history; ``decisions`` (#2369), option-based cards folded from
   the SAME live snapshot plus :func:`coord.state.list_drive_escalations` —
   "why is this stuck, and what do I run" per root cause, with a downstream
   `after=` cascade collapsed into its root's card rather than shown as N
   separate problems; ``usage`` (#1763), a cost/token fold over board
   assignment rows that delegates every number to :mod:`coord.usage_rollup`
   priced with the daemon's own loaded ``pricing:`` config — the report that
   replaced coord-tui's ``panel:usage`` and its hardcoded pricing snapshot;
   ``queue-outcomes`` (#2270), the one number the morning report is for —
   *what fraction of the queue got over the line without a human* — folded
   from #2235's per-host block log rather than from the audit trail, and the
   only report here that refuses to answer at all when its input file is not
   on this host; and ``trend`` (#2826), the one **bucketed** fold — a row per
   fixed-width time bucket rather than per issue or per episode, pairing
   merge throughput with a trailing-window mean cost/legs per merged issue so
   an empty bucket reads as a gap, never a false cost collapse to `$0`. It
   reuses ``completed``'s own merged-issue rows rather than a second cost
   calculator, and reuses its exact definition of "merged" so the two can
   never silently disagree on what counts.

The :class:`ReportResult` field names are the **wire contract** the coord-tui
Reports panel (#1741) renders against, and the CLI's ``--json`` and the
daemon's ``GET /report/{id}`` both emit exactly this shape — treat them as
public.

Read-only by construction: every query here is a ``SELECT``.  Running a
report must never touch the board (this repo has a recurring
"``reconcile()`` accretes behaviour" problem; reports do not join it).
"""

from __future__ import annotations

import csv
import io
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from coord.models import WORK_LIKE_TYPES

__all__ = [
    "ReportError",
    "UnknownReportError",
    "ReportParam",
    "ReportDef",
    "RowIdentity",
    "ColumnMeta",
    "ChartSeries",
    "ChartSpec",
    "CHART_KINDS",
    "ReportResult",
    "REPORTS",
    "catalogue",
    "resolve_params",
    "run_report",
    "fetch_audit_window",
    "detect_prior_activity",
    "fold_issue_activity",
    "run_issue_activity",
    "COMPLETED_COLUMNS",
    "COMPLETED_COLUMN_META",
    "fold_completed",
    "run_completed",
    "fold_drive_queue_status",
    "run_drive_queue_status",
    "fold_decisions",
    "run_decisions",
    "find_decision",
    "resolve_usage_window",
    "fold_usage",
    "run_usage",
    "QUEUE_OUTCOMES_COLUMNS",
    "QUEUE_OUTCOMES_COLUMN_META",
    "QUEUE_OUTCOMES_WINDOW_CHOICES",
    "resolve_queue_outcomes_window",
    "fold_queue_outcomes",
    "queue_outcomes_chart",
    "run_queue_outcomes",
    "TREND_COLUMNS",
    "TREND_COLUMN_META",
    "TREND_RANGE_CHOICES",
    "TREND_TRAILING_BUCKETS",
    "resolve_trend_range",
    "fold_trend",
    "run_trend",
    "parse_duration",
    "result_to_csv",
    "csv_filename",
]


class ReportError(ValueError):
    """A bad request against the report engine — unknown parameter, bad value.

    Callers (the CLI, the daemon) turn this into a clean message + non-zero
    exit / 400, never a traceback.
    """


class UnknownReportError(ReportError):
    """The requested ``report_id`` is not in :data:`REPORTS` (daemon: 404)."""


# ── parameter / definition / result shapes ─────────────────────────────────


@dataclass(frozen=True)
class ReportParam:
    """One parameter of a report, described richly enough that a client can
    build its input form from the catalogue alone (#1741 must NOT hardcode
    the param list).

    ``kind`` is ``"choice"`` (render a picker over ``choices``) or ``"text"``
    (render a free-text field).  ``free_form`` marks a ``choice`` param whose
    ``choices`` are *presets* rather than a whitelist — ``since`` is one:
    ``13h`` is a perfectly good window that nobody wants in a five-item
    picker.  ``validate`` is the server-side check, and is the authority; a
    client's form is a convenience on top of it.
    """

    id: str
    label: str
    kind: str = "text"
    choices: tuple[str, ...] = ()
    default: str = ""
    help: str = ""
    free_form: bool = False
    # Not part of the wire shape — the server-side validator. Raises
    # ReportError (message names the allowed values) on a bad value.
    validate: Callable[[str], None] | None = field(default=None, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "choices": list(self.choices),
            "default": self.default,
            "help": self.help,
            "free_form": self.free_form,
        }


@dataclass(frozen=True)
class RowIdentity:
    """#2454: which two ``columns`` of a report's rows name the ``(repo,
    issue)`` that row is *about*.

    Purely declarative, and deliberately **not** a rendering hint like
    :class:`ColumnMeta`.  A client that knows how to navigate to an issue —
    coord-tui's Reports panel, which offers a right-click "View on Board" on
    a result row — needs the row's *identity*, never its *content*.  Saying
    it here, once per report, is what lets that client stay generic: it reads
    an optional field off the catalogue entry instead of carrying a
    ``match`` on report ids (the exact coupling #2405 declined to introduce).

    ``repo_column`` names the column holding the **coord-local** repo name
    (matches ``coordinator.yml``, i.e. what ``select_issue`` and every
    ``coord`` verb take), ``issue_column`` the one holding the issue number.

    **Optional in both directions.**  Reports whose rows have no single
    ``(repo, issue)`` — ``usage`` grouped ``by=repo``, ``decisions``' cards,
    ``queue-outcomes``' per-period aggregates whose ``issues`` column is a
    *list* — simply declare none, and a client offers no per-row navigation
    for them.  A client that predates the field ignores it and renders
    exactly as before.
    """

    repo_column: str
    issue_column: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_column": self.repo_column,
            "issue_column": self.issue_column,
        }


@dataclass(frozen=True)
class ReportDef:
    """A named report.  ``run(**params)`` returns a :class:`ReportResult`."""

    id: str
    title: str
    description: str
    params: tuple[ReportParam, ...]
    run: Callable[..., "ReportResult"] = field(compare=False)
    #: #2454 — optional per-row ``(repo, issue)`` identity.  See
    #: :class:`RowIdentity`; ``None`` means "this report's rows are not
    #: about one issue".
    row_identity: RowIdentity | None = None

    def to_dict(self) -> dict[str, Any]:
        """Catalogue entry — everything a client needs except the callable."""
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "params": [p.to_dict() for p in self.params],
            # #2454: additive, and `null` for most reports. A client that
            # predates it ignores the key; one that understands it gets
            # per-row navigation without knowing which report it is looking at.
            "row_identity": (
                None if self.row_identity is None else self.row_identity.to_dict()
            ),
        }


@dataclass(frozen=True)
class ColumnMeta:
    """Display metadata for one entry of ``ReportResult.columns`` (#1760).

    Additive, not a retype: ``columns`` stays a bare ``list[str]`` (the
    already-shipped #1741 panel deserialises it as ``Vec<String>`` and must
    keep working unchanged), and row values stay raw — a ``started_at`` cell
    is still an epoch float, a ``machines`` cell is still a list.  This is
    only the hint a generic renderer needs to turn that raw value into a
    reasonable cell: ``kind`` says how to format it, ``align``/``weight``
    say how to lay out the column.  ``id`` matches the corresponding
    ``columns[]`` entry (and order matches too), so a client can zip them.
    """

    id: str
    label: str
    # Open vocabulary — a client that meets a `kind` it predates must fall
    # back to plain stringification, never fail to parse:
    # "text" | "int" | "timestamp" | "list" | "enum" | "duration" | "money"
    kind: str
    align: str = "left"  # "left" | "right"
    weight: float = 1.0  # relative column width hint

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "align": self.align,
            "weight": self.weight,
        }


# ── chart declaration (#2271) ──────────────────────────────────────────────
#
# A report says "this table also reads as a chart"; it does NOT ship a second
# copy of the numbers.  Every series names a `columns[]` id and the renderer
# reads the same `rows` the table renders, so there is exactly one source of
# truth, the table stays the fallback rendering, and `result_to_csv` (#1765)
# needs no change at all — it is driven by `columns`/`rows`/`totals` and never
# looks at this block.
#
# THE COMPATIBILITY RULE, same as `ColumnMeta.kind`'s (#1760): a client that
# does not understand this block, or meets a `kind` it predates, **renders the
# table and ignores the chart**.  It must never fail to parse and must never
# leave a hole where the chart would have gone.  That matters more than usual
# here because coord-tui ships as a per-host locally-built binary, outside
# propagation's reach, so the fleet routinely runs mixed versions.

#: Open vocabulary — the kinds a client is *expected* to know today.  A newer
#: daemon may name one that is not here; see the compatibility rule above.
CHART_KINDS = ("bar", "line", "sparkline")


@dataclass(frozen=True)
class ChartSeries:
    """One series of a :class:`ChartSpec`, derived from an existing column.

    ``column`` is a ``ReportResult.columns`` id whose per-row value supplies
    the y-values; ``label`` is what the legend shows.  ``color`` is an
    optional ``"#rrggbb"`` hint — when :attr:`ChartSpec.group_by` is set the
    series are generated per group and the backend palette picks the colours
    instead, because one declared colour cannot describe N groups.
    """

    label: str
    column: str
    color: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "column": self.column, "color": self.color}


@dataclass(frozen=True)
class ChartSpec:
    """An optional chart rendering of a :class:`ReportResult`'s own rows.

    Two shapes, and which one you get depends on ``group_by``:

    * **``group_by is None`` — one data point per row, in the report's
      canonical row order.**  ``x`` names the column supplying each point's
      category/time label; each :class:`ChartSeries` reads its own column
      straight off the row.  This is the "one bar per category" shape.
    * **``group_by`` set — a pivot.**  The x-axis is the *distinct* values of
      ``x`` in first-appearance order, and one output series is produced per
      distinct ``group_by`` value.  Rows landing in the same ``(group, x)``
      cell are **summed**, and an empty cell is ``0`` — so this shape is for
      magnitudes (counts, totals), not for averages or rates.  This is the
      "one trendline per bucket" shape that a long-form result needs.

    ``stacked`` is bar-only and ignored by every other kind.  Rendering a
    multi-series bar chart at all needs quadraui#584; a client whose pinned
    build predates it must degrade the section to a table with a stated
    reason rather than draw a chart that silently omits every series but the
    first.
    """

    kind: str
    series: tuple[ChartSeries, ...]
    x: str | None = None
    group_by: str | None = None
    stacked: bool = False
    title: str = ""
    y_label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "series": [s.to_dict() for s in self.series],
            "x": self.x,
            "group_by": self.group_by,
            "stacked": self.stacked,
            "title": self.title,
            "y_label": self.y_label,
        }


@dataclass
class ReportResult:
    """The wire contract (#1741 renders against these exact field names).

    ``columns`` is the ordered list of row keys worth putting in a table;
    ``rows`` may carry extra keys beyond it (``started_before_window``,
    ``last_event_at``, ...) for clients that want the detail.  ``notes``
    holds derived anomalies and caveats, rendered under the table.
    ``column_meta`` is additive display metadata, one entry per ``columns``
    entry in the same order (#1760) — a client that ignores it entirely
    still gets byte-identical ``columns``/``rows``.

    ``totals`` (#1763) is an optional grand-total row for reports that are a
    *fold* with a meaningful sum (``usage``), keyed by the same column ids as
    ``rows``.  It is **additive and defaults to ``None``**: reports that have
    no meaningful total (``issue-activity``, ``drive-queue-status``) leave it
    unset, and a client that ignores the key renders exactly as it did
    before.  Identity columns are deliberately *absent* from the dict rather
    than filled with a placeholder — a renderer that wants a ``Σ`` marker
    picks one itself, and one that doesn't leaves the cell blank.

    ``chart`` (#2271) is an optional declaration that this result also reads
    as a chart, derived from the very columns the table renders — see
    :class:`ChartSpec`.  **Additive and defaulting to ``None``**, and a
    client that ignores the key (or meets a ``kind`` it predates) renders the
    table exactly as it did before.
    """

    report_id: str
    generated_at: float
    window: tuple[float, float]
    columns: list[str]
    rows: list[dict]
    notes: list[str]
    column_meta: list[ColumnMeta] = field(default_factory=list)
    totals: dict[str, Any] | None = None
    chart: ChartSpec | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "generated_at": self.generated_at,
            "window": [self.window[0], self.window[1]],
            "columns": list(self.columns),
            "column_meta": [m.to_dict() for m in self.column_meta],
            "rows": list(self.rows),
            "notes": list(self.notes),
            "totals": None if self.totals is None else dict(self.totals),
            "chart": None if self.chart is None else self.chart.to_dict(),
        }


# ── time helpers ───────────────────────────────────────────────────────────

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw])\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0, "w": 604800.0}


def parse_duration(raw: str) -> float:
    """``"13h"`` → ``46800.0``.  Units: s, m, h, d, w.  Raises ReportError."""
    match = _DURATION_RE.match(raw or "")
    if match is None:
        raise ReportError(
            f"not a duration: {raw!r} — expected e.g. '90m', '13h', '3d' "
            "(units: s, m, h, d, w)"
        )
    return float(match.group(1)) * _UNIT_SECONDS[match.group(2).lower()]


def parse_timestamp(raw: str) -> float:
    """Epoch seconds or ISO-8601 → float.  Mirrors ``coord audit``'s parsing
    so ``--param until=...`` and ``coord audit --until`` agree."""
    try:
        return float(raw)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except ValueError as exc:
        raise ReportError(
            f"not an epoch number or ISO-8601 timestamp: {raw!r}"
        ) from exc


def _iso(ts: float | None) -> str:
    if ts is None:
        return "?"
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%SZ"
    )


# ── parameter resolution ───────────────────────────────────────────────────


def resolve_params(report: ReportDef, raw: Mapping[str, Any] | None) -> dict[str, str]:
    """Validate ``raw`` against ``report.params`` and fill in defaults.

    Unknown keys and bad values raise :class:`ReportError` with a message
    that names what *was* allowed — the CLI and the daemon both surface it
    verbatim, so it has to read well on its own.
    """
    raw = dict(raw or {})
    known = {p.id: p for p in report.params}
    for key in raw:
        if key not in known:
            raise ReportError(
                f"unknown parameter {key!r} for report {report.id!r} — "
                f"known parameters: {', '.join(sorted(known)) or '(none)'}"
            )
    resolved: dict[str, str] = {}
    for param in report.params:
        value = raw.get(param.id)
        value = param.default if value is None or value == "" else str(value)
        _validate_param(param, value)
        resolved[param.id] = value
    return resolved


def _validate_param(param: ReportParam, value: str) -> None:
    if param.validate is not None:
        param.validate(value)
        return
    if param.kind == "choice" and param.choices and not param.free_form:
        if value not in param.choices:
            raise ReportError(
                f"invalid value for {param.id!r}: {value!r} — "
                f"allowed values: {', '.join(param.choices)}"
            )


# ── audit fetch + pagination ───────────────────────────────────────────────

# 100 pages x 500 rows = 50k events. Far past any real window; a backstop
# against an infinite cursor walk, not a coverage limit — hitting it sets
# truncated=True and the report says so in `notes`.
MAX_PAGES = 100


def _default_fetch(**kwargs: Any) -> dict:
    from coord.audit import query_audit_log  # noqa: PLC0415

    return query_audit_log(**kwargs)


def fetch_audit_window(
    *,
    since: float,
    until: float,
    repo: str | None = None,
    fetch: Callable[..., Mapping[str, Any]] | None = None,
    page_limit: int | None = None,
    max_pages: int = MAX_PAGES,
    category: str | None = None,
    event_type: str | None = None,
) -> tuple[list[dict], bool]:
    """Walk the keyset cursor until the whole ``[since, until]`` window is
    covered.  Returns ``(entries, truncated)``.

    ``truncated`` is True only when the walk gave up with rows still
    outstanding (page cap hit, or a page claimed ``has_more`` but handed
    back no cursor) — the caller turns that into an explicit note rather
    than shipping a silently short answer.

    ``category``/``event_type`` push the filter down into
    :func:`coord.audit.query_audit_log` rather than filtering the pages
    afterwards — a four-week window (``queue-outcomes``) is exactly where
    reading every row to keep a handful of ``merged`` ones would hit the page
    cap and report itself truncated for no reason.  They are passed to
    ``fetch`` **only when set**, so an injected fetch that predates them keeps
    receiving byte-identical kwargs.
    """
    if fetch is None:
        fetch = _default_fetch
    if page_limit is None:
        from coord.audit import MAX_LIMIT  # noqa: PLC0415

        page_limit = MAX_LIMIT

    filters: dict[str, Any] = {}
    if category:
        filters["category"] = category
    if event_type:
        filters["event_type"] = event_type

    entries: list[dict] = []
    cursor: str | None = None
    truncated = True  # flipped to False the moment a page says "that's all"
    for _ in range(max(1, int(max_pages))):
        page = fetch(
            since=since,
            until=until,
            repo=repo or None,
            limit=page_limit,
            cursor=cursor,
            **filters,
        ) or {}
        entries.extend(page.get("entries") or [])
        if not page.get("has_more"):
            truncated = False
            break
        cursor = page.get("next_cursor")
        if not cursor:
            # has_more with no cursor — can't advance; stop rather than loop.
            break
    return entries, truncated


# ── issue-activity: the fold ───────────────────────────────────────────────

ISSUE_ACTIVITY_COLUMNS = [
    "repo",
    "issue",
    "title",
    "started_at",
    "machines",
    "fix_iterations",
    "test_verdicts",
    "review_verdicts",
    "merged_at",
    "drive_exit",
    "outcome",
]

# One entry per ISSUE_ACTIVITY_COLUMNS entry, same order (#1760) — the
# display metadata a generic renderer (CLI table, coord-tui panel) needs to
# format a raw row value without hardcoding per-report field knowledge.
ISSUE_ACTIVITY_COLUMN_META = [
    ColumnMeta(id="repo", label="Repo", kind="text"),
    ColumnMeta(id="issue", label="Issue", kind="int", align="right"),
    ColumnMeta(id="title", label="Title", kind="text", weight=3.0),
    ColumnMeta(id="started_at", label="Started", kind="timestamp"),
    ColumnMeta(id="machines", label="Machines", kind="list"),
    ColumnMeta(id="fix_iterations", label="Fixes", kind="int", align="right"),
    ColumnMeta(id="test_verdicts", label="Tests", kind="list"),
    ColumnMeta(id="review_verdicts", label="Reviews", kind="list"),
    ColumnMeta(id="merged_at", label="Merged", kind="timestamp"),
    ColumnMeta(id="drive_exit", label="Drive Exit", kind="text"),
    ColumnMeta(id="outcome", label="Outcome", kind="enum"),
]

_TEST_EVENTS = ("test_passed", "test_failed", "test_skipped")
_REVIEW_EVENTS = ("review_approve", "review_request-changes")

# An issue with no drive_exit and no event for this long by the end of the
# window is called `stalled` rather than `in-flight`. Two hours is well past
# any normal gate turnaround in this fleet.
STALL_QUIET_SECONDS = 2 * 3600.0

# A work-like dispatch is a real attempt at the issue; a review/smoke/plan
# dispatch is not, and must not count as a fix iteration.
#
# #3132 review: this used to be a locally-hardcoded frozenset independent of
# `coord.models.WORK_LIKE_TYPES`, which drifted once already (mock-author
# missing here, per #1141) and would have drifted again the moment
# `epic-decompose` was added there without a matching update here. Import
# the canonical set instead of re-declaring it.
_WORK_LIKE_TYPES = WORK_LIKE_TYPES


def fold_issue_activity(
    entries: Iterable[Mapping[str, Any]],
    window: tuple[float, float],
    *,
    titles: Mapping[tuple[str, int], str] | None = None,
    generated_at: float | None = None,
    truncated: bool = False,
    prior_activity: frozenset[tuple[str, int]] = frozenset(),
) -> ReportResult:
    """Fold audit entries into one row per ``(repo, issue)``.

    **Pure** — no DB, no daemon, no clock.  ``generated_at`` defaults to the
    window end so a frozen-clock test gets a deterministic result.

    ``entries`` may arrive in any order (the audit read path is newest-first);
    they are sorted ascending on ``(ts, id)`` here, which is what makes
    "first dispatch", "last merge" and the ordered verdict lists mean what
    they say.

    ``prior_activity`` (#1760) is the one fact this pure fold cannot derive
    for itself: the set of ``(repo, issue)`` keys that have *any* audit event
    before the window opened, as determined by the caller's bounded
    look-back (:func:`detect_prior_activity`).  Without it, an issue whose
    real start predates the window but which was re-dispatched inside it
    reads as "started here, zero fixes" — a real timestamp and a real count
    that are both wrong, with nothing in the row saying so.  With it, that
    row instead reports ``started_at=None``, ``started_before_window=True``
    and ``counts_partial=True``, and every in-window work dispatch counts as
    a fix (the issue was already running when the window opened, so each one
    is a re-dispatch).  Default is empty, so existing callers are unaffected
    in shape.
    """
    start, end = float(window[0]), float(window[1])
    title_map = dict(titles or {})

    usable: list[Mapping[str, Any]] = []
    orphans = 0
    for entry in entries:
        if entry.get("repo") and entry.get("issue") is not None:
            usable.append(entry)
        else:
            orphans += 1
    usable.sort(key=lambda e: (float(e.get("ts") or 0.0), int(e.get("id") or 0)))

    groups: dict[tuple[str, int], list[Mapping[str, Any]]] = {}
    for entry in usable:
        key = (str(entry["repo"]), int(entry["issue"]))
        groups.setdefault(key, []).append(entry)

    rows = [
        _fold_one_issue(
            repo,
            issue,
            evs,
            end,
            title_map.get((repo, issue)),
            had_prior_activity=(repo, issue) in prior_activity,
        )
        for (repo, issue), evs in groups.items()
    ]
    # Most-recently-active first: the morning question is "what moved", and
    # the thing that moved last is the thing still moving.
    rows.sort(key=lambda r: r["last_event_at"] or 0.0, reverse=True)

    notes: list[str] = []
    if truncated:
        notes.append(
            f"TRUNCATED: the window {_iso(start)} → {_iso(end)} could not be "
            f"fully fetched from the audit trail ({len(usable) + orphans} "
            "events read before the page cap). Rows below cover only part of "
            "the window — narrow it with a smaller `since` for a complete "
            "answer."
        )
    if orphans:
        notes.append(
            f"{orphans} event(s) in the window carry no repo/issue "
            "(fleet-level housekeeping) and are not represented in any row."
        )
    notes.extend(_derive_notes(rows))

    return ReportResult(
        report_id="issue-activity",
        generated_at=end if generated_at is None else float(generated_at),
        window=(start, end),
        columns=list(ISSUE_ACTIVITY_COLUMNS),
        column_meta=list(ISSUE_ACTIVITY_COLUMN_META),
        rows=rows,
        notes=notes,
    )


def _fold_one_issue(
    repo: str,
    issue: int,
    events: Sequence[Mapping[str, Any]],
    window_end: float,
    title: str | None,
    *,
    had_prior_activity: bool = False,
) -> dict[str, Any]:
    started_at: float | None = None
    machines: list[str] = []
    work_dispatches = 0
    test_verdicts: list[str] = []
    review_verdicts: list[str] = []
    merged_at: float | None = None
    drive_exit: dict[str, Any] | None = None

    for entry in events:
        category = entry.get("category")
        event_type = entry.get("event_type")
        ts = float(entry.get("ts") or 0.0)
        details = entry.get("details") or {}
        if not isinstance(details, Mapping):
            details = {}
        machine = entry.get("machine")
        if machine and machine not in machines:
            machines.append(machine)

        if category == "drive" and event_type == "drive_started":
            if started_at is None:
                started_at = ts
        elif category == "dispatch" and event_type == "dispatched":
            # `details.type` is absent on the oldest rows; "work" is the
            # assignment default, so that is the right assumption.
            if (details.get("type") or "work") in _WORK_LIKE_TYPES:
                work_dispatches += 1
                if started_at is None:
                    started_at = ts
        elif category == "test" and event_type in _TEST_EVENTS:
            test_verdicts.append(str(event_type)[len("test_"):])
        elif category == "review" and event_type in _REVIEW_EVENTS:
            review_verdicts.append(str(event_type)[len("review_"):])
        elif category == "merge" and event_type == "merged":
            merged_at = ts
        elif category == "drive" and event_type == "drive_exited":
            drive_exit = {
                "at": ts,
                "exit_code": details.get("exit_code"),
                "reason": details.get("reason") or details.get("error"),
            }

    if had_prior_activity:
        # The caller's look-back (#1760) found an event before the window
        # opened — this issue was already running. Report that plainly
        # rather than claiming a start the window cannot support: no start
        # time, and every in-window work dispatch is a re-dispatch (not
        # "first dispatch, zero fixes").
        started_at = None
        started_before_window = True
        fix_iterations = work_dispatches
    else:
        # "In-window activity, but no start event in it" — the issue began
        # before the window opened. Reported as started_at=None + this flag
        # rather than as a bogus start time taken from the first event we
        # happened to see.
        started_before_window = started_at is None
        # Every work dispatch after the *first* one is a fix iteration.
        fix_iterations = max(0, work_dispatches - 1)
    # counts_partial is narrower than started_before_window: the latter can
    # also fire from the plain "no start event in this window" inference
    # above, which doesn't know whether fix_iterations/test_verdicts are
    # complete or merely empty. Only a confirmed look-back hit means the
    # counts are a known lower bound.
    counts_partial = had_prior_activity

    first_event_at = float(events[0].get("ts") or 0.0) if events else None
    last_event_at = float(events[-1].get("ts") or 0.0) if events else None

    return {
        "repo": repo,
        "issue": issue,
        "title": title,
        "started_at": started_at,
        "started_before_window": started_before_window,
        "machines": machines,
        "fix_iterations": fix_iterations,
        "counts_partial": counts_partial,
        "test_verdicts": test_verdicts,
        "review_verdicts": review_verdicts,
        "merged_at": merged_at,
        "drive_exit": drive_exit,
        "outcome": _derive_outcome(merged_at, drive_exit, last_event_at, window_end),
        "first_event_at": first_event_at,
        "last_event_at": last_event_at,
        "event_count": len(events),
    }


def _nonzero_exit(drive_exit: Mapping[str, Any] | None) -> bool:
    """True when the driver did NOT exit clean.  A missing/None ``exit_code``
    counts — that shape is written by the crash path
    (``DriveRunner._drive_exit_summary``), which is exactly as unclean as a
    non-zero code."""
    return drive_exit is not None and drive_exit.get("exit_code") != 0


def _derive_outcome(
    merged_at: float | None,
    drive_exit: Mapping[str, Any] | None,
    last_event_at: float | None,
    window_end: float,
) -> str:
    if merged_at is not None:
        return "merged"
    if drive_exit is not None:
        # The driver is gone. Non-zero => it gave up loudly; clean exit with
        # nothing landed => it gave up quietly. Neither is in-flight.
        return "failed" if _nonzero_exit(drive_exit) else "stalled"
    if last_event_at is not None and (window_end - last_event_at) > STALL_QUIET_SECONDS:
        return "stalled"
    return "in-flight"


def _derive_notes(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """Anomalies worth a human's eye, derived from the folded rows.

    The load-bearing one is the first: a driver that exits non-zero on an
    issue that then merges anyway.  That is the real 2026-08-02 case
    (#1631 exited 1 with "merge attempted 3 times without landing" at 21:48;
    the merge landed at 22:01) — a driver giving up on a merge that was
    still converging, otherwise invisible in both the event stream and the
    final board state.
    """
    notes: list[str] = []
    for row in rows:
        ident = f"{row['repo']}#{row['issue']}"
        drive_exit = row.get("drive_exit")
        if drive_exit and _nonzero_exit(drive_exit) and row.get("merged_at") is not None:
            reason = drive_exit.get("reason")
            reason_part = f" ({reason})" if reason else ""
            notes.append(
                f"{ident}: driver exited exit_code="
                f"{drive_exit.get('exit_code')!r} at {_iso(drive_exit.get('at'))}"
                f"{reason_part}, but the merge landed at "
                f"{_iso(row['merged_at'])} — the driver gave up on a merge "
                "that was still converging."
            )
        if (
            row.get("merged_at") is not None
            and row.get("test_verdicts")
            and row["test_verdicts"][-1] == "failed"
        ):
            notes.append(
                f"{ident}: merged at {_iso(row['merged_at'])} with the last "
                "in-window Test-gate verdict still 'failed'."
            )
        if int(row.get("fix_iterations") or 0) >= 3:
            notes.append(
                f"{ident}: {row['fix_iterations']} fix iterations in this "
                "window — the work is not converging on its own."
            )
        if row.get("counts_partial"):
            # #1760: this issue was already running when the window opened
            # (the caller's look-back found an earlier event) — say so
            # explicitly rather than let a real-looking fix_iterations/
            # test_verdicts count pass as complete.
            notes.append(
                f"{ident}: started before this window — fix_iterations and "
                "test_verdicts are lower bounds, not the full count. Widen "
                "`since` to see the real start."
            )
        elif (
            "request-changes" in (row.get("review_verdicts") or [])
            and int(row.get("fix_iterations") or 0) == 0
        ):
            # #1760: a request-changes verdict implies at least one
            # re-dispatch happened. fix_iterations=0 with counts_partial
            # False (the elif) means the fold believes it saw the whole
            # window's activity — this combination should not be reachable,
            # and if it appears the row is self-contradictory.
            notes.append(
                f"{ident}: review verdict 'request-changes' with "
                "fix_iterations=0 — this combination should not happen; the "
                "row is internally inconsistent."
            )
    return notes


# ── issue-activity: the runner ─────────────────────────────────────────────


def _lookup_titles(
    keys: Iterable[tuple[str, int]],
) -> dict[tuple[str, int], str]:
    """Best-effort issue titles from the local DB.  Read-only, and failure is
    not an error — a missing title renders as ``None`` in the row, which is
    strictly better than failing the whole report over cosmetics."""
    keys = sorted(set(keys))
    if not keys:
        return {}
    out: dict[tuple[str, int], str] = {}
    try:
        from coord import sql  # noqa: PLC0415
        from coord.db import get_connection  # noqa: PLC0415

        conn = get_connection()
        for repo, number in keys:
            row = sql.execute(
                conn,
                "SELECT title FROM issues WHERE repo_name = ? AND number = ?",
                (repo, number),
            ).fetchone()
            if row is not None and row["title"]:
                out[(repo, number)] = row["title"]
                continue
            row = sql.execute(
                conn,
                "SELECT issue_title FROM assignments WHERE repo_name = ? "
                "AND issue_number = ? AND issue_title IS NOT NULL "
                "ORDER BY rowid DESC LIMIT 1",
                (repo, number),
            ).fetchone()
            if row is not None and row["issue_title"]:
                out[(repo, number)] = row["issue_title"]
    except Exception:  # noqa: BLE001 — titles are cosmetic; never fail a report
        return out
    return out


def detect_prior_activity(
    keys: Iterable[tuple[str, int]],
    *,
    until: float,
    fetch: Callable[..., Mapping[str, Any]],
) -> frozenset[tuple[str, int]]:
    """Bounded look-back (#1760): which ``(repo, issue)`` keys already have
    at least one audit event before ``until`` (the window start)?

    One query per issue in ``keys`` — not one per event, not an unbounded
    scan.  Same query path as the window fetch (``fetch`` is the same
    callable, real or injected), just ``until=window_start``, a per-issue
    filter, and ``limit=1`` newest-first: the fold only needs a yes/no per
    issue, not the events themselves.
    """
    prior: set[tuple[str, int]] = set()
    for key_repo, key_issue in sorted(set(keys)):
        page = fetch(
            since=None,
            until=until,
            repo=key_repo,
            issue=key_issue,
            limit=1,
            cursor=None,
        ) or {}
        if page.get("entries"):
            prior.add((key_repo, key_issue))
    return frozenset(prior)


def run_issue_activity(
    *,
    since: str = "24h",
    until: str = "",
    repo: str = "",
    now: float | None = None,
    fetch: Callable[..., Mapping[str, Any]] | None = None,
    title_lookup: Callable[..., Mapping[tuple[str, int], str]] | None = None,
) -> ReportResult:
    """Fetch the window (paginated) and fold it.  ``now``/``fetch``/
    ``title_lookup`` are test seams; the report's own parameters are
    ``since``/``until``/``repo``."""
    generated_at = time.time() if now is None else float(now)
    end = parse_timestamp(until) if until else generated_at
    start = end - parse_duration(since)

    fetch_fn = _default_fetch if fetch is None else fetch

    entries, truncated = fetch_audit_window(
        since=start, until=end, repo=repo or None, fetch=fetch_fn
    )
    keys = {
        (str(e["repo"]), int(e["issue"]))
        for e in entries
        if e.get("repo") and e.get("issue") is not None
    }
    lookup = _lookup_titles if title_lookup is None else title_lookup
    titles = lookup(keys)
    prior_activity = detect_prior_activity(keys, until=start, fetch=fetch_fn)
    return fold_issue_activity(
        entries,
        (start, end),
        titles=titles,
        generated_at=generated_at,
        truncated=truncated,
        prior_activity=prior_activity,
    )


# ── drive-queue-status: a live snapshot, not a fold ────────────────────────
#
# #1805: "what is queued, and is it moving?" without a CLI round-trip.  Unlike
# issue-activity this is not a fold over an audit-trail window — it is a
# point-in-time read of `drive_queue` via `coord.state.list_drive_queue`
# (daemon-or-local already handled there), so `window` is degenerate:
# `(generated_at, generated_at)`.  `drive_queue` has no `completed_at` and
# `coord/drive_queue.py` emits no audit events, so there is no data source
# for a queue *history* report — see this issue's "Out of scope".

DRIVE_QUEUE_STATUS_COLUMNS = [
    "position",
    "repo",
    "issue",
    "title",
    "state",
    "machine",
    "attempts",
    "deferrals",
    "last_reason",
    "reason_at",
    "enqueued_at",
    "launched_at",
    "hold_state",
    "after",
]

# One entry per DRIVE_QUEUE_STATUS_COLUMNS entry, same order (#1760).
DRIVE_QUEUE_STATUS_COLUMN_META = [
    ColumnMeta(id="position", label="Pos", kind="int", align="right"),
    ColumnMeta(id="repo", label="Repo", kind="text"),
    ColumnMeta(id="issue", label="Issue", kind="int", align="right"),
    ColumnMeta(id="title", label="Title", kind="text", weight=2.0),
    ColumnMeta(id="state", label="State", kind="enum"),
    ColumnMeta(id="machine", label="Machine", kind="text"),
    ColumnMeta(id="attempts", label="Attempts", kind="int", align="right"),
    ColumnMeta(id="deferrals", label="Deferrals", kind="int", align="right"),
    ColumnMeta(id="last_reason", label="Last Reason", kind="text", weight=3.0),
    # #2133: capture time of `last_reason` — a client renders it as an age
    # next to the reason so a stale snapshot never reads as current state.
    # `None`/absent for a row predating the migration.
    ColumnMeta(id="reason_at", label="Reason At", kind="timestamp"),
    ColumnMeta(id="enqueued_at", label="Enqueued", kind="timestamp"),
    ColumnMeta(id="launched_at", label="Launched", kind="timestamp"),
    ColumnMeta(id="hold_state", label="Hold", kind="enum"),
    ColumnMeta(id="after", label="After", kind="list"),
]

# The #1794 tell: an entry that has already burned at least one launch
# attempt is the thing an operator most wants shouted at them.
_RETRIED_ATTEMPTS_THRESHOLD = 1


def fold_drive_queue_status(
    entries: Iterable[Mapping[str, Any]],
    generated_at: float,
    *,
    titles: Mapping[tuple[str, int], str] | None = None,
    queue_escalation: Mapping[str, Any] | None = None,
) -> ReportResult:
    """Fold already-fetched ``drive_queue`` rows into a snapshot ``ReportResult``.

    **Pure** — no DB, no daemon, no clock: ``entries`` is whatever
    :func:`coord.state.list_drive_queue` returned (raw column names,
    ``after_json`` already decoded to a list) and ``generated_at`` is the
    caller's clock reading, reused verbatim for both ends of ``window`` since
    a live snapshot has no meaningful range.

    ``entries`` arrives pre-ordered (``list_drive_queue`` is
    ``ORDER BY position, id``) — this fold does not re-sort.
    """
    title_map = dict(titles or {})
    rows: list[dict[str, Any]] = []
    for entry in entries:
        repo = str(entry.get("repo_name") or "")
        issue = int(entry.get("issue_number") or 0)
        rows.append(
            {
                "position": int(entry.get("position") or 0),
                "repo": repo,
                "issue": issue,
                "title": title_map.get((repo, issue)),
                "state": entry.get("state") or "",
                "machine": entry.get("machine") or "",
                "attempts": int(entry.get("attempts") or 0),
                "deferrals": int(entry.get("deferrals") or 0),
                "last_reason": entry.get("last_reason") or "",
                "reason_at": entry.get("reason_at"),
                "enqueued_at": entry.get("enqueued_at"),
                "launched_at": entry.get("launched_at"),
                "hold_state": entry.get("hold_state") or "",
                "after": list(entry.get("after_json") or []),
                # Extra keys beyond `columns` — ReportResult's contract
                # explicitly allows this for clients that want the detail.
                "session_name": entry.get("session_name") or "",
                "hold_reason": entry.get("hold_reason") or "",
                "resume_when": entry.get("resume_when") or "",
                # #2186: without this, a report consumer (or `coord reports
                # drive-queue-status`) has the same blind spot the TUI had —
                # a fired gate with no way to tell "this entry alone is
                # held" from "the whole queue stopped". Fail-closed exactly
                # like `coord.state.list_drive_queue`'s own normalisation
                # (and `QueueEntry._normalize_hold_scope`): anything other
                # than the literal `"fleet"` reads as the narrower `"entry"`.
                "hold_scope": "fleet" if str(entry.get("hold_scope") or "") == "fleet" else "entry",
            }
        )

    notes: list[str] = []
    if not rows:
        notes.append("The drive queue is empty.")
    else:
        from coord.drive_queue import (  # noqa: PLC0415
            STATE_BLOCKED,
            STATE_DONE,
            STATE_FAILED,
            STATE_RUNNING,
            STATE_WAITING,
            TERMINAL_QUEUE_STATES,
        )

        counts: dict[str, int] = {}
        for r in rows:
            counts[r["state"]] = counts.get(r["state"], 0) + 1
        # The headline is entries the queue will still act on — `done` (and
        # any other terminal state) is run history, not queue depth, so it
        # is excluded here rather than folded into `len(rows)` (#1855).
        queued = sum(n for state, n in counts.items() if state not in TERMINAL_QUEUE_STATES)

        def _ordered(present: set[str], preferred: tuple[str, ...]) -> list[str]:
            # `preferred` is display polish only — any state absent from it
            # (a future addition to drive_queue.py's five, or one we simply
            # forgot to list) still surfaces, just alphabetically after the
            # known ones, so nothing can silently vanish the way `blocked`
            # did before this fix.
            return [s for s in preferred if s in present] + sorted(present - set(preferred))

        non_terminal_states = _ordered(
            {s for s in counts if s not in TERMINAL_QUEUE_STATES},
            (STATE_RUNNING, STATE_WAITING),
        )
        # `blocked`/`failed` are the states that need a human — call them
        # out ahead of the benign `done` count, not appended after it.
        terminal_states = _ordered(
            {s for s in counts if s in TERMINAL_QUEUE_STATES},
            (STATE_BLOCKED, STATE_FAILED, STATE_DONE),
        )

        breakdown = ", ".join(f"{counts[s]} {s}" for s in non_terminal_states)
        headline = f"{queued} entr{'y' if queued == 1 else 'ies'} queued"
        if breakdown:
            headline += f" ({breakdown})"
        terminal_parts = [f"{counts[s]} {s}" for s in terminal_states]
        if terminal_parts:
            headline += " · " + " · ".join(terminal_parts)
        notes.append(headline + ".")
        retried = [r for r in rows if r["attempts"] >= _RETRIED_ATTEMPTS_THRESHOLD]
        if retried:
            named = ", ".join(
                f"{r['repo']}#{r['issue']} (attempts={r['attempts']})" for r in retried
            )
            notes.append(f"attempts>=1: {named}.")
    if queue_escalation:
        reason = queue_escalation.get("reason") or "(no reason recorded)"
        stage = queue_escalation.get("stage") or "?"
        notes.append(
            f"standing queue-level escalation: stage={stage!r} — {reason}"
        )

    return ReportResult(
        report_id="drive-queue-status",
        generated_at=generated_at,
        window=(generated_at, generated_at),
        columns=list(DRIVE_QUEUE_STATUS_COLUMNS),
        column_meta=list(DRIVE_QUEUE_STATUS_COLUMN_META),
        rows=rows,
        notes=notes,
    )


def _default_list_drive_queue(repo: str | None) -> list[dict]:
    from coord.state import list_drive_queue  # noqa: PLC0415

    return list_drive_queue(repo)


def _default_queue_escalation() -> Mapping[str, Any] | None:
    """The standing queue-level escalation record (#1754's synthetic key),
    if one exists.  A plain read — never runs a tick — so it is safe to
    surface here; best-effort, mirroring :func:`_lookup_titles`."""
    try:
        from coord.drive_queue import (  # noqa: PLC0415
            QUEUE_ALERT_ISSUE,
            QUEUE_ALERT_REPO,
        )
        from coord.state import get_drive_escalation  # noqa: PLC0415

        return get_drive_escalation(QUEUE_ALERT_REPO, QUEUE_ALERT_ISSUE)
    except Exception:  # noqa: BLE001 — cosmetic; never fail the report over it
        return None


def run_drive_queue_status(
    *,
    repo: str = "",
    now: float | None = None,
    fetch: Callable[[str | None], Sequence[Mapping[str, Any]]] | None = None,
    title_lookup: Callable[..., Mapping[tuple[str, int], str]] | None = None,
    escalation_lookup: Callable[[], Mapping[str, Any] | None] | None = None,
) -> ReportResult:
    """Fetch the live queue and fold it.  ``now``/``fetch``/``title_lookup``/
    ``escalation_lookup`` are test seams (mirrors :func:`run_issue_activity`);
    the report's own parameter is ``repo``.

    Read-only and tick-free by construction: the only call here is
    ``list_drive_queue`` (or the injected ``fetch``) — never ``plan_tick``.
    """
    generated_at = time.time() if now is None else float(now)
    fetch_fn = _default_list_drive_queue if fetch is None else fetch
    entries = list(fetch_fn(repo or None) or [])

    keys = {
        (str(e["repo_name"]), int(e["issue_number"]))
        for e in entries
        if e.get("repo_name") and e.get("issue_number") is not None
    }
    lookup = _lookup_titles if title_lookup is None else title_lookup
    titles = lookup(keys)

    esc_lookup = _default_queue_escalation if escalation_lookup is None else escalation_lookup
    queue_escalation = esc_lookup()

    return fold_drive_queue_status(
        entries,
        generated_at,
        titles=titles,
        queue_escalation=queue_escalation,
    )


# ── decisions: escalations + blocked queue roots as option-based cards ────
#
# #2369.  "Why is the fleet stuck" today means opening `drive-queue list`,
# `merge --plan`, `gh pr checks` and sometimes a CI log, then translating
# dense infra text into something actionable by hand. The raw material
# already exists — `coord.state.list_drive_escalations()` (written by
# `coord/drive.py`'s `_escalate_merge`/`_escalate_dead_end`) already carries
# a structured `reason` + `proposed_command` per issue, and a `blocked`/
# `failed` `drive_queue` row's `last_reason` already embeds "inspect:" /
# "remedy:"-shaped lines a human reads by eye. This report folds BOTH
# sources into one card per root cause — reusing `proposed_command`
# verbatim and PARSING the embedded lines rather than re-deriving either.
#
# Read-only and additive, same posture as `drive-queue-status` above: no new
# escalation-detection logic, no new persistence, and no touch on `coord
# status`/`coord health`/`coord diagnose`.

DECISIONS_COLUMNS = [
    "repo",
    "issue",
    "title",
    "why",
    "options",
    "downstream_count",
    "downstream",
    "since",
    "source",
]

# One entry per DECISIONS_COLUMNS entry, same order (#1760).
DECISIONS_COLUMN_META = [
    ColumnMeta(id="repo", label="Repo", kind="text"),
    ColumnMeta(id="issue", label="Issue", kind="int", align="right"),
    ColumnMeta(id="title", label="Title", kind="text", weight=2.0),
    ColumnMeta(id="why", label="Why", kind="text", weight=3.0),
    ColumnMeta(id="options", label="Options", kind="list", weight=3.0),
    ColumnMeta(id="downstream_count", label="Downstream", kind="int", align="right"),
    ColumnMeta(id="downstream", label="Downstream Of This", kind="list"),
    ColumnMeta(id="since", label="Since", kind="timestamp"),
    ColumnMeta(id="source", label="Source", kind="enum"),
]

# `_resolve_prereqs`'s "queued but blocked/failed" verdict — the ONE
# unsatisfiable `after=` shape `coord/drive_queue.py`'s own blocked-row
# handling (and this fold) treats as "downstream of another card", never a
# novel root cause. `_is_unsatisfiable_prereq_reason` is the actual reused
# predicate (imported below) — reused conceptually, not mechanically, same
# as #2369's own docstring says of `coord/dead_end.py`.
# Imported lazily (mirrors every other `coord.drive_queue` import in this
# module) to keep this module importable from the thin client base install.
def _unsatisfiable_prereq_reason(text: str) -> bool:
    from coord.drive_queue import _is_unsatisfiable_prereq_reason  # noqa: PLC0415

    return _is_unsatisfiable_prereq_reason(text)


# A "Label: rest of the line" shape this fold recognises as an embedded
# option — deliberately narrow (an allowlist below), so a `gates: k=v | ...`
# or `last board state: ...` line (informational, not actionable) is never
# mistaken for one.
_LABELED_LINE_RE = re.compile(r"^(?P<label>[A-Za-z][A-Za-z '-]{1,40}?):\s*(?P<rest>\S.*)$")
_OR_RERUN_RE = re.compile(r"^or\s+re-run\s+(?P<rest>\S.*)$", re.IGNORECASE)

# `coord/drive_queue.py`'s `reason = f"{own_reason} — {explanation}"` (the
# #1844/#2019 "permanent" branch, and its `parking without spending an
# attempt` siblings) appends its rationale to `own_reason`'s LAST line with
# no newline in between — so when that last line is itself the "or re-run
# ..."/labeled-line an option is parsed from, the appended rationale reads
# as if it were part of the command (#2369 review: the #2283 dead-end
# fixture's `Re-run` option ends up with "... skip JIT authoring. — the
# board row is terminal and unactionable ..., blocking without spending an
# attempt" as its `command_or_action`). `" — "` (em dash, never a bare
# `--` flag) is the one join marker this repo uses for "reason + why", so
# stripping from its first occurrence recovers just the command.
_APPENDED_RATIONALE_RE = re.compile(r"\s+—\s+.*$")


def _strip_appended_rationale(text: str) -> str:
    return _APPENDED_RATIONALE_RE.sub("", text).strip()

# Every label `coord/drive.py`'s `_die`/`_escalate_*` calls are already
# known to embed (see #2283's "inspect:"/"Re-author by hand:"/"or re-run"
# shape and coord-portal#107's "inspect: coord merge --plan --repo ..."
# shape, both quoted in #2369) — reused verbatim, not reinvented.
_OPTION_LABEL_WHAT_HAPPENS = {
    "inspect": "Shows the raw log for the failed run.",
    "inspect the gates": "Shows the current merge/test/review gate state.",
    "remedy": "Applies the documented remedy for this block.",
    "recover": "Runs the recorded recovery command.",
    "recovery": "Runs the recorded recovery command.",
    "proposed": "Runs the proposed fix.",
    "re-author by hand": "Manually re-authors the slice from scratch.",
    "re-dispatch by hand": "Manually re-dispatches the failed step.",
    "resolve by hand": "Manually resolves the block, then clears the record.",
    "continue by hand": "Manually continues the work the automation stopped.",
    "re-run": "Re-runs the drive with the named flag.",
}


def _parse_reason_options(reason: str) -> list[dict[str, Any]]:
    """Pull the de facto options already embedded in a `last_reason` (#2283 /
    coord-portal#107's worked shapes) rather than inventing new ones.

    The first line is the headline ("why"); every line after it is a
    candidate. Recognises `"<label>: <command>"` lines whose label is in
    :data:`_OPTION_LABEL_WHAT_HAPPENS`, and the `"or re-run ..."` shape
    `coord/drive.py` writes with no colon. Anything else (a `gates:` summary,
    a diagnostic block line, plain prose) is silently skipped — it isn't an
    option, and #2369 explicitly scopes this to reformatting, not a new
    taxonomy of failure classes.
    """
    options: list[dict[str, Any]] = []
    lines = (reason or "").splitlines()[1:]
    for raw_line in lines:
        line = raw_line.strip().lstrip("-*•").strip()
        if not line:
            continue
        match = _LABELED_LINE_RE.match(line)
        if match:
            label = match.group("label").strip()
            key = label.lower()
            if key in _OPTION_LABEL_WHAT_HAPPENS:
                options.append(
                    {
                        "label": label[:1].upper() + label[1:],
                        "command_or_action": _strip_appended_rationale(
                            match.group("rest")
                        ),
                        "what_happens": _OPTION_LABEL_WHAT_HAPPENS[key],
                    }
                )
                continue
        rerun = _OR_RERUN_RE.match(line)
        if rerun:
            options.append(
                {
                    "label": "Re-run",
                    "command_or_action": _strip_appended_rationale(
                        rerun.group("rest")
                    ),
                    "what_happens": _OPTION_LABEL_WHAT_HAPPENS["re-run"],
                }
            )
    return options[:4]


def _generic_fallback_option(repo: str, row: Mapping[str, Any]) -> dict[str, Any]:
    """The card #2369 promises even a genuinely novel shape gets — never a
    silently dropped stuck item.  Merge-shaped text (mentions "merge"/"CI"/
    "check", the coord-portal#107 shape) points at `coord merge --plan`;
    anything else points at `coord drive-queue list --repo <repo>` — the one
    command guaranteed to resolve.  A `coord log <id>` fallback was tried
    first and dropped (#2369 review): a drive-queue row's `session_name` is
    the *tmux session name* `drive_session_name` writes for `tmux attach`,
    not a `coord log` `ASSIGNMENT_ID`, and `entry_key(repo, issue)`
    (`"repo#issue"`) isn't one either — both would hand the operator a
    command that resolves nothing."""
    reason = str(row.get("last_reason") or "").lower()
    if any(word in reason for word in ("merge", " ci ", "check", "ci)")):
        return {
            "label": "Inspect the merge plan",
            "command_or_action": f"coord merge --plan --repo {repo}",
            "what_happens": "Shows the current merge/test/review gate state for this repo.",
        }
    issue = int(row.get("issue_number") or 0)
    return {
        "label": "Inspect the queue row",
        "command_or_action": f"coord drive-queue list --repo {repo}",
        "what_happens": f"Shows this and every other queued entry for {repo} (look for #{issue}).",
    }


def _first_line(text: str) -> str:
    stripped = (text or "").strip()
    return stripped.splitlines()[0].strip() if stripped else ""


def _looks_stale_smoke(reason: str) -> bool:
    low = (reason or "").lower()
    return "stale" in low and "smoke" in low


def _escalation_why(reason: str) -> str:
    """The escalation's own `reason`, paired with what it means practically
    when the shape is recognised (#2369's #2360 worked example: a stale
    smoke verdict). A bare status string is not explanation enough on its
    own — anything NOT recognised still gets the raw reason verbatim rather
    than a guessed rephrasing (#2369: no new taxonomy of failure classes)."""
    text = (reason or "").strip() or "(no reason recorded)"
    if _looks_stale_smoke(text):
        return (
            f"{text.rstrip('.')}. Main moved forward past what was tested, "
            "so the system can't confirm the tested code is still safe to "
            "merge without a human saying so."
        )
    return text


def _decisions_notes(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """The headline note for a set of already-finished decision cards.

    A function of *rows* alone (never the unfiltered fold input) so it can
    be recomputed after `run_decisions` applies its `repo` filter (#2369
    review: computing this once, before the filter, left a `--repo`-scoped
    call reporting a global count next to a repo-scoped row list)."""
    if not rows:
        return [
            "Nothing needs a decision — no blocked/failed drive-queue "
            "entries and no open escalations."
        ]
    total_downstream = sum(r["downstream_count"] for r in rows)
    headline = f"{len(rows)} decision{'s' if len(rows) != 1 else ''} pending"
    if total_downstream:
        headline += (
            f" ({total_downstream} more entr"
            f"{'y' if total_downstream == 1 else 'ies'} waiting on them)"
        )
    return [headline + "."]


def fold_decisions(
    escalations: Iterable[Mapping[str, Any]],
    queue_entries: Iterable[Mapping[str, Any]],
    generated_at: float,
    *,
    titles: Mapping[tuple[str, int], str] | None = None,
) -> ReportResult:
    """Fold escalation-table rows + blocked/failed queue rows into cards.

    **Pure** — no DB, no daemon, no clock: *escalations* is whatever
    :func:`coord.state.list_drive_escalations` returned, *queue_entries* is
    whatever :func:`coord.state.list_drive_queue` returned (the FULL queue,
    every state — this needs `waiting`/`done` rows too, to resolve an
    `after=` chain's roots), and ``generated_at`` is the caller's clock
    reading.

    One card per root cause. An escalation-table row is always a root — it
    names its own gate divergence, never another queue entry. A `blocked`/
    `failed` queue row is a root UNLESS its `last_reason` is
    `_resolve_prereqs`'s "queued but blocked/failed — it will never satisfy"
    verdict AND its `after=` graph names another card's key — that row folds
    into the root's card as a `downstream` entry instead of getting its own
    (the #2283 cascade-collapse: "1 real problem, N waiting on it").
    """
    from coord.drive_queue import STATE_BLOCKED, STATE_FAILED, parse_key  # noqa: PLC0415

    title_map = dict(titles or {})

    escalated: dict[str, Mapping[str, Any]] = {}
    for esc in escalations:
        repo = esc.get("repo_name")
        issue = esc.get("issue_number")
        if not repo or issue is None:
            continue
        escalated[f"{repo}#{int(issue)}"] = esc

    blocked_or_failed: dict[str, Mapping[str, Any]] = {}
    for row in queue_entries:
        repo = row.get("repo_name")
        issue = row.get("issue_number")
        if not repo or issue is None:
            continue
        if row.get("state") not in (STATE_BLOCKED, STATE_FAILED):
            continue
        blocked_or_failed[f"{repo}#{int(issue)}"] = row

    universe = set(escalated) | set(blocked_or_failed)

    # Immediate parent for every queue row whose own block is nothing but
    # "one of my pre-reqs is itself stuck" — never for an escalation (an
    # escalation names its own gate divergence, not another entry's key).
    parent_of: dict[str, str] = {}
    for key, row in blocked_or_failed.items():
        if not _unsatisfiable_prereq_reason(row.get("last_reason") or ""):
            continue
        for dep_key in row.get("after_json") or []:
            if dep_key in universe and dep_key != key:
                parent_of[key] = dep_key
                break

    def _resolve_root(key: str) -> str:
        seen: set[str] = set()
        current = key
        while current in parent_of and current not in seen:
            seen.add(current)
            current = parent_of[current]
        return current

    downstream_of: dict[str, list[str]] = {}
    for key in parent_of:
        root = _resolve_root(key)
        downstream_of.setdefault(root, []).append(key)

    # Numeric-aware: sorting the raw `"repo#issue"` strings would put
    # `"api#10"` before `"api#2"` (#2369 review nit) — `parse_key` gives
    # `(repo, issue)` so the issue number sorts as a number, not text. A key
    # `parse_key` can't parse (shouldn't happen — `universe` is built from
    # `entry_key`-shaped keys) sorts last rather than raising.
    root_keys = sorted(
        (k for k in universe if k not in parent_of),
        key=lambda k: parse_key(k) or (k, 1 << 62),
    )

    rows: list[dict[str, Any]] = []
    for key in root_keys:
        parsed = parse_key(key)
        if parsed is None:
            continue
        repo, issue = parsed
        downstream = sorted(set(downstream_of.get(key, [])))

        if key in escalated:
            esc = escalated[key]
            reason = str(esc.get("reason") or "")
            proposed = str(esc.get("proposed_command") or "")
            options = [
                {
                    "label": "Recommended",
                    "command_or_action": proposed,
                    "what_happens": "Runs the fix the driver proposed when it escalated.",
                    "recommended": True,
                },
                {
                    "label": "Inspect",
                    "command_or_action": f"coord escalate list --repo {repo}",
                    "what_happens": "Shows the full escalation record, including gate readings.",
                    "recommended": False,
                },
            ]
            rows.append(
                {
                    "repo": repo,
                    "issue": issue,
                    "title": title_map.get((repo, issue)),
                    "why": _escalation_why(reason),
                    "options": options,
                    "downstream_count": len(downstream),
                    "downstream": downstream,
                    "since": esc.get("created_at"),
                    "source": "escalation",
                    # Extra keys beyond `columns` — ReportResult's contract
                    # allows this for a client that wants the detail.
                    "stage": esc.get("stage") or "",
                    "raw_reason": reason,
                }
            )
            continue

        row = blocked_or_failed[key]
        reason = str(row.get("last_reason") or "")
        parsed_options = _parse_reason_options(reason)
        if parsed_options:
            for i, opt in enumerate(parsed_options):
                opt["recommended"] = i == 0
            options = parsed_options
        else:
            fallback = _generic_fallback_option(repo, row)
            fallback["recommended"] = True
            options = [fallback]
        rows.append(
            {
                "repo": repo,
                "issue": issue,
                "title": title_map.get((repo, issue)),
                "why": _first_line(reason) or reason or "(no reason recorded)",
                "options": options,
                "downstream_count": len(downstream),
                "downstream": downstream,
                "since": row.get("reason_at") or row.get("enqueued_at"),
                "source": "queue",
                "stage": row.get("state") or "",
                "raw_reason": reason,
            }
        )

    return ReportResult(
        report_id="decisions",
        generated_at=generated_at,
        window=(generated_at, generated_at),
        columns=list(DECISIONS_COLUMNS),
        column_meta=list(DECISIONS_COLUMN_META),
        rows=rows,
        notes=_decisions_notes(rows),
    )


def _default_list_drive_escalations(repo: str | None) -> list[dict]:
    from coord.state import list_drive_escalations  # noqa: PLC0415

    return list_drive_escalations(repo)


def run_decisions(
    *,
    repo: str = "",
    now: float | None = None,
    escalations_fetch: Callable[[str | None], Sequence[Mapping[str, Any]]] | None = None,
    queue_fetch: Callable[[str | None], Sequence[Mapping[str, Any]]] | None = None,
    title_lookup: Callable[..., Mapping[tuple[str, int], str]] | None = None,
) -> ReportResult:
    """Fetch both sources and fold them.  ``now``/``*_fetch``/``title_lookup``
    are test seams; the report's own parameter is ``repo``.

    Fetches the FULL (unfiltered) queue and escalation table regardless of
    ``repo`` — mirrors `coord drive-queue list`'s #2183 fix: an `after=`
    chain can cross repos, so filtering before the cascade collapse would
    misdiagnose a cross-repo prereq as unrelated. ``repo`` is applied to the
    finished cards instead — and ``notes`` is recomputed from those same
    finished cards (#2369 review), so a ``--repo``-scoped call never reports
    a headline count that includes another repo's decisions.
    """
    generated_at = time.time() if now is None else float(now)
    esc_fn = _default_list_drive_escalations if escalations_fetch is None else escalations_fetch
    queue_fn = _default_list_drive_queue if queue_fetch is None else queue_fetch
    escalations = list(esc_fn(None) or [])
    queue_entries = list(queue_fn(None) or [])

    # Folded WITHOUT titles first, then the title lookup is scoped to just
    # the resulting cards' keys (#2369 review non-blocking finding): the
    # queue fetch above is intentionally the FULL, unfiltered queue (every
    # state, needed to resolve `after=` roots), so keying the lookup off its
    # raw rows would query titles for `waiting`/`done`/`running` entries
    # that never surface in this report — the exact "280+ historical rows"
    # overhead #2369 opens by naming as the problem this report exists to
    # avoid.
    result = fold_decisions(escalations, queue_entries, generated_at, titles=None)
    card_keys = {(r["repo"], r["issue"]) for r in result.rows}
    lookup = _lookup_titles if title_lookup is None else title_lookup
    titles = lookup(card_keys)
    for r in result.rows:
        r["title"] = titles.get((r["repo"], r["issue"]))

    if repo:
        result.rows = [r for r in result.rows if r["repo"] == repo]
    result.notes = _decisions_notes(result.rows)
    return result


def find_decision(
    repo: str,
    issue: int,
    *,
    now: float | None = None,
    escalations_fetch: Callable[[str | None], Sequence[Mapping[str, Any]]] | None = None,
    queue_fetch: Callable[[str | None], Sequence[Mapping[str, Any]]] | None = None,
) -> dict[str, Any] | None:
    """Look up the single `decisions` card for ``repo``#``issue``, fresh,
    every call — `coord decide` (#2370) is required to never re-derive or
    cache its own copy of "what the options are", so it calls this instead
    of hand-rolling a filtered fold.

    Folds the SAME full envelope :func:`run_decisions` would (escalation
    table + the FULL, unfiltered drive queue — an `after=` chain can cross
    repos, so scoping the queue fetch to ``repo`` before the cascade
    collapse would misdiagnose a cross-repo prereq, same #2183 reasoning
    `run_decisions` documents) but skips the title lookup entirely: unlike
    the report, `coord decide` never displays a title, so paying for
    `_lookup_titles` — which `run_decisions` already has to run for every
    OTHER repo's cards too, since it only learns which cards are "this
    repo's" after folding the whole fleet — would be pure waste for a
    single-issue call.

    Returns ``None`` if ``repo``#``issue`` has no open decision: no
    escalation record and no blocked/failed queue row, OR it exists as a
    queue row but folds as a `downstream` entry into some OTHER root's card
    (matching the report itself, which gives that row no card of its own —
    see `fold_decisions`'s cascade-collapse docstring).
    """
    generated_at = time.time() if now is None else float(now)
    esc_fn = _default_list_drive_escalations if escalations_fetch is None else escalations_fetch
    queue_fn = _default_list_drive_queue if queue_fetch is None else queue_fetch
    escalations = list(esc_fn(None) or [])
    queue_entries = list(queue_fn(None) or [])
    result = fold_decisions(escalations, queue_entries, generated_at, titles=None)
    for row in result.rows:
        if row["repo"] == repo and int(row["issue"]) == int(issue):
            return row
    return None


# ── usage: the per-issue / per-repo cost + token rollup ────────────────────
#
# #1763.  This is a **correctness fix**, not a consolidation.  `coord-tui`'s
# `panel:usage` was a Rust port of `coord/usage_rollup.py` carrying a
# hardcoded snapshot of `coord.config.PricingConfig`'s shipped defaults, so
# an operator who overrode `pricing:` in coordinator.yml changed what
# `coord usage` reported and left the panel confidently showing different
# numbers (the durable #1116 finding).  The daemon holds the config, so the
# daemon does the arithmetic: everything below *calls* `usage_rollup.rollup`
# / `rollup_by_stage` with the loaded `PricingConfig` and reimplements none
# of its window predicate, leg-cost rule or default sort.

USAGE_WINDOW_CHOICES = ("today", "week", "month", "7d", "30d")
USAGE_GROUP_BY_CHOICES = ("issue", "repo")

# Columns depend on `group_by`: a repo-grouped row IS the whole repo, so it
# carries no issue number and no title (same shape the retired panel used).
#
# #2786: `cache_read`/`cache_create`/`turns` were added alongside the
# existing `tokens_in`/`tokens_out` — never folded into them. `tokens_in`
# keeps meaning exactly what it always meant (raw uncached input, ~0.001% of
# `work`-leg spend); redefining it to a sum would silently change every
# historical number a dashboard already rendered. `cache_read` is the one
# that actually carries the money (~66% of `work` spend, #2786) and is
# listed right after the existing token columns so it isn't the column a
# narrow client truncates first.
USAGE_ISSUE_COLUMNS = [
    "issue",
    "repo",
    "title",
    "legs",
    "tokens_in",
    "tokens_out",
    "cache_read",
    "cache_create",
    "turns",
    "cost_captured",
    "cost_est",
    "cost_total",
]

USAGE_REPO_COLUMNS = [
    "repo",
    "legs",
    "tokens_in",
    "tokens_out",
    "cache_read",
    "cache_create",
    "turns",
    "cost_captured",
    "cost_est",
    "cost_total",
]

# #1760 display metadata, indexed by column id and emitted in `columns`
# order — a large `weight` on `title`, `int`/`right` for the counts, and
# `money`/`right` for the three dollar columns.  `money` is a *generic* kind
# (the vocabulary is open, see ColumnMeta): a client that predates it falls
# back to plain stringification and still shows the number.
_USAGE_COLUMN_META: dict[str, ColumnMeta] = {
    "issue": ColumnMeta(id="issue", label="Issue", kind="int", align="right", weight=0.8),
    "repo": ColumnMeta(id="repo", label="Repo", kind="text", weight=1.5),
    "title": ColumnMeta(id="title", label="Title", kind="text", weight=4.0),
    "legs": ColumnMeta(id="legs", label="Legs", kind="int", align="right", weight=0.6),
    # #2825: labelled "Raw In", not "Tok In" — sitting next to a full "Tok
    # Out", "Tok In" reads as its matched pair, and it is not: this is raw
    # UNCACHED input, ~0.001% of what a `work` leg actually sends (the other
    # ~98% is `cache_read`, below). The column `id` stays `tokens_in` so no
    # historical number or saved sort changes.
    "tokens_in": ColumnMeta(id="tokens_in", label="Raw In", kind="int", align="right"),
    "tokens_out": ColumnMeta(id="tokens_out", label="Tok Out", kind="int", align="right"),
    # #2786: the column that carries the money — see module comment above.
    "cache_read": ColumnMeta(id="cache_read", label="Cache Rd", kind="int", align="right"),
    "cache_create": ColumnMeta(id="cache_create", label="Cache Cr", kind="int", align="right"),
    "turns": ColumnMeta(id="turns", label="Turns", kind="int", align="right", weight=0.6),
    "cost_captured": ColumnMeta(
        id="cost_captured", label="Cost $", kind="money", align="right"
    ),
    "cost_est": ColumnMeta(id="cost_est", label="Est ~$", kind="money", align="right"),
    "cost_total": ColumnMeta(id="cost_total", label="Total $", kind="money", align="right"),
}

# Dollar figures are rounded before they go on the wire so a float artefact
# (2.8000000000000003) never reaches a generic renderer. Six places is far
# below any real per-leg cost and above any rounding that could change a
# reported cent.
_USAGE_COST_PLACES = 6


def usage_columns(group_by: str) -> list[str]:
    """The ``columns`` list for *group_by*.  Raises :class:`ReportError`."""
    if group_by == "issue":
        return list(USAGE_ISSUE_COLUMNS)
    if group_by == "repo":
        return list(USAGE_REPO_COLUMNS)
    raise ReportError(
        f"invalid value for 'group_by': {group_by!r} — "
        f"allowed values: {', '.join(USAGE_GROUP_BY_CHOICES)}"
    )


def resolve_usage_window(window: str, now: float | None = None):
    """Resolve a ``window`` parameter to a :class:`coord.usage_rollup.TimeWindow`.

    Every preset is *called* from :mod:`coord.usage_rollup`, never
    reimplemented — that module owns the calendar (this is precisely what the
    retired panel hand-rolled a civil calendar to duplicate).
    """
    from coord.usage_rollup import (  # noqa: PLC0415
        Window,
        window_month,
        window_today,
        window_week,
    )

    if window == "today":
        return window_today(now)
    if window == "week":
        return window_week(now)
    if window == "month":
        return window_month(now)
    if window in ("7d", "30d"):
        # Window.since is the *bounded* variant: [now - spec, now).
        return Window.since(window, now)
    raise ReportError(
        f"invalid value for 'window': {window!r} — "
        f"allowed values: {', '.join(USAGE_WINDOW_CHOICES)}"
    )


def _usage_row_title(leg_rows: Sequence[Mapping[str, Any]]) -> str | None:
    for row in leg_rows:
        title = row.get("issue_title")
        if title:
            return str(title)
    return None


def _usage_metrics(group: Any) -> dict[str, Any]:
    """The numeric half of a row (or of ``totals``) — identical for both."""
    turns = int(group.turns)
    cache_read = int(group.tokens.cache_read)
    cache_create = int(group.tokens.cache_creation)
    metrics = {
        "legs": int(group.legs),
        "tokens_in": int(group.tokens.input),
        "tokens_out": int(group.tokens.output),
        # #2786: cache_read is the column that carries the money (~66% of
        # `work`-leg spend) — see the module comment above USAGE_ISSUE_COLUMNS.
        "cache_read": cache_read,
        "cache_create": cache_create,
        "turns": turns,
        "cost_captured": round(float(group.cost_captured), _USAGE_COST_PLACES),
        "cost_est": round(float(group.cost_est), _USAGE_COST_PLACES),
        "cost_total": round(float(group.cost_total), _USAGE_COST_PLACES),
        "duration_secs": round(float(group.duration_secs), 3),
        "open_legs": int(group.open_legs),
        "unknown_model_legs": int(group.unknown_model_legs),
    }
    # #2786: tok/turn — beyond `columns` (the contract explicitly allows
    # extra row keys), a derived figure that makes "long context" vs "many
    # turns" directly readable without a client doing its own division. 0
    # (not a crash / not a bare token total) when there's no turn data to
    # divide by, e.g. every row predating the `num_turns` column.
    total_tokens = (
        metrics["tokens_in"] + metrics["tokens_out"] + cache_read + cache_create
    )
    metrics["tok_per_turn"] = round(total_tokens / turns) if turns > 0 else 0
    return metrics


def _usage_stage_breakdown(
    leg_rows: Sequence[Mapping[str, Any]], window: Any, pricing: Any
) -> list[dict[str, Any]]:
    """Per-stage sub-rollup for one group, as a list of plain dicts.

    The panel's only drill-down was "click a row → its per-stage legs"; that
    maps onto rows without needing a second request, so it ships inline as an
    extra row key rather than as a second report.
    """
    from coord.usage_rollup import rollup_by_stage  # noqa: PLC0415

    sub = rollup_by_stage(list(leg_rows), window, pricing)
    stages = [
        {"stage": str(key), **_usage_metrics(grp)} for key, grp in sub.groups.items()
    ]
    stages.sort(key=lambda s: s["cost_total"], reverse=True)
    return stages


def fold_usage(
    rows: Iterable[Mapping[str, Any]],
    window: Any,
    *,
    group_by: str = "issue",
    pricing: Any = None,
    generated_at: float | None = None,
    extra_notes: Sequence[str] = (),
) -> ReportResult:
    """Fold board assignment rows into a per-issue / per-repo cost rollup.

    **Pure** — no DB, no daemon, no clock: *rows* is whatever the caller
    fetched (daemon ``/board`` ``assignments`` wire shape), *window* is a
    resolved :class:`~coord.usage_rollup.TimeWindow`, and *pricing* is the
    :class:`~coord.config.PricingConfig` that was actually loaded.  Every
    number comes back out of :func:`coord.usage_rollup.rollup` — this
    function only shapes it into the report wire contract.

    *pricing* left at ``None`` falls through to ``usage_rollup``'s own
    built-in defaults, which is correct for a unit test and **not** what the
    runner does (see :func:`run_usage`, which loads ``coordinator.yml``).
    """
    from coord.usage_rollup import IssueKey, rollup  # noqa: PLC0415

    columns = usage_columns(group_by)
    rows = list(rows)
    result = rollup(rows, group_by=group_by, window=window, pricing=pricing)

    out_rows: list[dict[str, Any]] = []
    for key, group in result.groups.items():
        row: dict[str, Any] = {}
        if isinstance(key, IssueKey):
            row["issue"] = int(key.issue_number)
            row["repo"] = str(key.repo_name)
            row["title"] = _usage_row_title(group.leg_rows)
        else:
            row["repo"] = str(key)
        row.update(_usage_metrics(group))
        row["stages"] = _usage_stage_breakdown(group.leg_rows, window, pricing)
        out_rows.append(row)

    # Same default order as `coord usage` and the retired panel: biggest
    # spend first. `_ident` breaks ties deterministically so a frozen-clock
    # test isn't at the mercy of dict ordering.
    out_rows.sort(
        key=lambda r: (-r["cost_total"], str(r.get("repo") or ""), int(r.get("issue") or 0))
    )

    start = 0.0 if getattr(window, "start", None) is None else float(window.start)
    end = (
        float(generated_at if generated_at is not None else start)
        if getattr(window, "end", None) is None
        else float(window.end)
    )

    totals = _usage_metrics(result.total)

    notes: list[str] = list(extra_notes)
    if not out_rows:
        notes.append("No usage recorded in this window.")
    for row in out_rows:
        unknown = int(row.get("unknown_model_legs") or 0)
        if unknown:
            ident = (
                f"{row['repo']}#{row['issue']}" if "issue" in row else str(row["repo"])
            )
            notes.append(
                f"{ident}: {unknown} leg(s) ran a model with no entry in the "
                "loaded `pricing:` config — their tokens are counted but "
                "their spend is NOT in `cost_est` (never silently priced at "
                "$0). Add a rate for that model to coordinator.yml."
            )
    if totals["open_legs"]:
        notes.append(
            f"{totals['open_legs']} leg(s) in this window are still running — "
            "their duration counts as 0 and their cost is not final."
        )

    return ReportResult(
        report_id="usage",
        generated_at=end if generated_at is None else float(generated_at),
        window=(start, end),
        columns=columns,
        column_meta=[_USAGE_COLUMN_META[c] for c in columns],
        rows=out_rows,
        notes=notes,
        totals=totals,
    )


def _default_usage_rows(repo: str | None) -> list[dict]:  # noqa: ARG001
    """Board assignment rows from the local DB.

    Deliberately **not** :func:`coord.usage.fetch_usage_rows`: that helper
    branches to a ``GET /board`` when a board service is configured, and a
    report already runs *on* the daemon host (``coord.state.run_report``
    routes a thin client's request to ``GET /report/{id}``), so going through
    it would make the daemon HTTP-call itself.  This mirrors that helper's
    *local* branch exactly — ``list_assignments()`` rather than the
    retention-capped ``/board`` projection, because a usage rollup wants full
    history.
    """
    from coord.dao import SqliteStore  # noqa: PLC0415

    return SqliteStore().list_assignments()


def _load_pricing() -> tuple[Any, list[str]]:
    """The ``pricing:`` block from the loaded ``coordinator.yml``.

    Returns ``(PricingConfig, notes)``.  A config that cannot be loaded falls
    back to the built-in defaults **and says so in ``notes``** — silently
    falling back is exactly the failure mode #1763 exists to remove.
    """
    from coord.config import PricingConfig  # noqa: PLC0415

    try:
        from coord.config import load, resolve_config_path  # noqa: PLC0415

        return load(resolve_config_path()).pricing, []
    except Exception as exc:  # noqa: BLE001 — surfaced as a note, not a crash
        return (
            PricingConfig(),
            [
                "WARNING: coordinator.yml could not be loaded "
                f"({type(exc).__name__}: {exc}) — `cost_est` uses the built-in "
                "default rates, which may differ from this fleet's `pricing:` "
                "block."
            ],
        )


def run_usage(
    *,
    window: str = "today",
    group_by: str = "issue",
    repo: str = "",
    now: float | None = None,
    fetch: Callable[[str | None], Sequence[Mapping[str, Any]]] | None = None,
    pricing: Any = None,
) -> ReportResult:
    """Fetch board rows and fold them.  ``now``/``fetch``/``pricing`` are test
    seams; the report's own parameters are ``window``/``group_by``/``repo``."""
    generated_at = time.time() if now is None else float(now)
    resolved = resolve_usage_window(window, generated_at)

    fetch_fn = _default_usage_rows if fetch is None else fetch
    rows = list(fetch_fn(repo or None) or [])
    if repo:
        rows = [r for r in rows if str(r.get("repo_name") or "") == repo]

    extra_notes: list[str] = []
    if pricing is None:
        pricing, extra_notes = _load_pricing()

    return fold_usage(
        rows,
        resolved,
        group_by=group_by,
        pricing=pricing,
        generated_at=generated_at,
        extra_notes=extra_notes,
    )


# ── queue-outcomes: the morning number (#2270) ─────────────────────────────
#
# One question: **what fraction of the queue got over the line without me?**
# The operator's target is `(succeeded + auto_resolved_mechanism +
# auto_resolved_rescue) / total` trending to ~100%.
#
# This is the view over #2235's Phase-0 recorder (`coord.block_log`), which is
# the only durable record of a stall *and how it ended*.  `drive-queue-status`
# cannot answer it and says so in its own description ("a snapshot, not a
# history: `drive_queue` has no `completed_at`"), so nothing here reads the
# queue table.
#
# TWO SOURCES, and the seam between them is deliberate:
#
# * every bucket except `succeeded` folds out of block-log EPISODES, because a
#   stall is the only thing that log records; and
# * `succeeded` — merged with no stall at all — has no episode by
#   construction, so it is counted from `merged` audit events in the same
#   window, minus any key that already has an episode there (a stall that
#   later landed is auto_resolved, not succeeded, and must not be counted
#   twice).  The report says which number came from where in its notes; a
#   headline whose denominator is silently half-sourced is worse than none.

QUEUE_OUTCOMES_WINDOW_CHOICES = ("24h", "7d", "4w")

#: window -> (span, period).  The bar view is one period; the trend views are
#: 7 daily and 4 weekly points of the same arithmetic, so a client renders a
#: trendline by grouping rows on `period_start` and needs no second report.
_QUEUE_OUTCOMES_WINDOWS: dict[str, tuple[float, float]] = {
    "24h": (86400.0, 86400.0),
    "7d": (7 * 86400.0, 86400.0),
    "4w": (28 * 86400.0, 7 * 86400.0),
}

QUEUE_OUTCOMES_COLUMNS = [
    "period_start",
    "bucket",
    "category",
    "by_design",
    "count",
    "share_pct",
    "issues",
]

# One entry per QUEUE_OUTCOMES_COLUMNS entry, same order (#1760).
QUEUE_OUTCOMES_COLUMN_META = [
    ColumnMeta(id="period_start", label="Period", kind="timestamp"),
    ColumnMeta(id="bucket", label="Bucket", kind="enum", weight=1.6),
    ColumnMeta(id="category", label="Category", kind="text", weight=3.0),
    ColumnMeta(id="by_design", label="By Design", kind="text"),
    ColumnMeta(id="count", label="Count", kind="int", align="right", weight=0.6),
    ColumnMeta(id="share_pct", label="Share %", kind="text", align="right", weight=0.7),
    # Attributability (#2270 acceptance): every count drills to the exact
    # `(repo, issue)` list behind it. Truncated in a terminal table, whole in
    # --format json / csv.
    ColumnMeta(id="issues", label="Issues", kind="list", weight=3.0),
]

_MERGE_AUDIT_CATEGORY = "merge"
_MERGE_AUDIT_EVENT = "merged"


def resolve_queue_outcomes_window(
    window: str, end: float
) -> tuple[float, float, float]:
    """``(start, end, period_seconds)`` for a ``window`` preset.

    Periods are aligned to ``end``, not to the civil calendar: the report has
    to be reproducible from ``until`` alone, and a calendar alignment would
    make the same ``until`` produce different buckets in different timezones.
    """
    try:
        span, period = _QUEUE_OUTCOMES_WINDOWS[window]
    except KeyError:
        raise ReportError(
            f"invalid value for 'window': {window!r} — "
            f"allowed values: {', '.join(QUEUE_OUTCOMES_WINDOW_CHOICES)}"
        ) from None
    return float(end) - span, float(end), period


def _period_bounds(start: float, end: float, period: float) -> list[float]:
    """The start timestamp of each period in ``[start, end)``, ascending."""
    if period <= 0:
        return [start]
    count = max(1, int(round((end - start) / period)))
    return [start + i * period for i in range(count)]


def _period_index(ts: float, start: float, period: float, count: int) -> int:
    if period <= 0:
        return 0
    return min(count - 1, max(0, int((ts - start) // period)))


def _episode_period_ts(episode: Mapping[str, Any]) -> tuple[float, bool]:
    """``(the timestamp this episode is bucketed on, is_open)``.

    A resolved episode belongs to the period it *ended* in — that is when it
    got over the line (or didn't).  An open one has no such moment, so it
    belongs to the period it stalled in: "still stalled" is a fact about the
    day the queue stopped, not about today.
    """
    if episode.get("resolved"):
        return float(episode.get("resolved_at") or 0.0), False
    return float(episode.get("entered_at") or 0.0), True


def fold_queue_outcomes(
    episodes: Iterable[Mapping[str, Any]],
    window: tuple[float, float],
    *,
    period_seconds: float | None = None,
    merged: Iterable[tuple[str, float]] = (),
    generated_at: float | None = None,
    log_location: Mapping[str, Any] | None = None,
    log_starts_at: float | None = None,
    extra_notes: Sequence[str] = (),
) -> ReportResult:
    """Fold block-log episodes (+ merge events) into outcome buckets.

    **Pure** — no log read, no DB, no daemon, no clock: *episodes* is whatever
    :func:`coord.block_log.episodes` returned, *merged* is a sequence of
    ``(key, ts)`` pairs for issues that merged in the window, and
    ``generated_at`` defaults to the window end so a frozen-clock test is
    deterministic.

    Every category comes out of :func:`coord.block_log.episode_category`, an
    **open vocabulary** read from the data — a cause this build has never seen
    appears in the report as itself.  Every bucket comes out of
    :func:`coord.block_log.episode_bucket`, and the ``by_design`` split out of
    :func:`coord.block_log.is_by_design`; none of the three is re-derived here,
    so the report and ``coord drive-queue block-log`` cannot drift.

    An episode that entered before the window and is **still open** is folded
    into the first period rather than dropped.  Dropping it would be the
    single most flattering bug available to this report: the longest-running
    unresolved stalls are exactly the ones whose ``entered_at`` has fallen off
    the back of the window.
    """
    from coord.block_log import (  # noqa: PLC0415
        AUTO_BUCKETS,
        BUCKET_OPEN,
        BUCKET_SUCCEEDED,
        OUTCOME_BUCKETS,
        UNCLASSIFIED_CATEGORY,
        episode_bucket,
        episode_category,
        is_by_design,
    )

    start, end = float(window[0]), float(window[1])
    period = float(period_seconds) if period_seconds else max(1.0, end - start)
    period_starts = _period_bounds(start, end, period)
    n_periods = len(period_starts)

    # (period, bucket, category, by_design) -> [key, ...]
    tally: dict[tuple[int, str, str, bool], list[str]] = {}
    windowed_keys: set[str] = set()
    open_before_window = 0

    for episode in episodes:
        key = str(episode.get("key") or "")
        ts, is_open = _episode_period_ts(episode)
        if is_open:
            if ts >= end:
                continue  # stalled after this window closed
            if ts < start:
                open_before_window += 1
        elif not (start <= ts < end):
            continue
        idx = _period_index(max(ts, start), start, period, n_periods)
        cell = (
            idx,
            episode_bucket(episode),
            episode_category(episode),
            is_by_design(episode),
        )
        tally.setdefault(cell, []).append(key)
        windowed_keys.add(key)

    for key, ts in merged:
        key = str(key)
        ts = float(ts)
        if not (start <= ts < end):
            continue
        if key in windowed_keys:
            # It stalled first. That episode already counted it in an
            # auto_resolved/human bucket; counting the merge again would
            # inflate the numerator with the very entries that needed help.
            continue
        idx = _period_index(ts, start, period, n_periods)
        tally.setdefault((idx, BUCKET_SUCCEEDED, "merged", False), []).append(key)

    bucket_order = {name: i for i, name in enumerate(OUTCOME_BUCKETS)}
    per_period_total: dict[int, int] = {}
    for (idx, _bucket, _cat, _bd), keys in tally.items():
        per_period_total[idx] = per_period_total.get(idx, 0) + len(keys)

    rows: list[dict[str, Any]] = []
    for (idx, bucket, category, by_design), keys in tally.items():
        total = per_period_total.get(idx, 0)
        rows.append(
            {
                "period_start": period_starts[idx],
                "period_end": period_starts[idx] + period,
                "bucket": bucket,
                "category": category,
                "by_design": by_design,
                "count": len(keys),
                "share_pct": round(100.0 * len(keys) / total, 1) if total else 0.0,
                "issues": sorted(set(keys)),
            }
        )
    rows.sort(
        key=lambda r: (
            r["period_start"],
            bucket_order.get(r["bucket"], len(bucket_order)),
            -r["count"],
            r["category"],
        )
    )

    grand_total = sum(per_period_total.values())
    grand_auto = sum(r["count"] for r in rows if r["bucket"] in AUTO_BUCKETS)
    grand_by_design = sum(r["count"] for r in rows if r["by_design"])

    notes: list[str] = list(extra_notes)
    notes.extend(_queue_outcomes_location_notes(log_location))
    if not rows:
        notes.append(
            "No queue entry reached a terminal state in this window — neither "
            "a recorded stall nor a merge. That is an EMPTY result, not a "
            "100% score."
        )
    else:
        notes.append(
            "headline: "
            + _headline_note(grand_auto, grand_total, grand_by_design)
            + " over the whole window."
        )
        if n_periods > 1:
            for idx, period_start in enumerate(period_starts):
                total = per_period_total.get(idx, 0)
                auto = sum(
                    r["count"]
                    for r in rows
                    if r["period_start"] == period_start and r["bucket"] in AUTO_BUCKETS
                )
                by_design = sum(
                    r["count"]
                    for r in rows
                    if r["period_start"] == period_start and r["by_design"]
                )
                notes.append(
                    f"  {_iso(period_start)}: "
                    + _headline_note(auto, total, by_design)
                )
    notes.extend(
        _queue_outcomes_caveats(
            rows,
            open_before_window=open_before_window,
            unclassified_label=UNCLASSIFIED_CATEGORY,
            open_bucket=BUCKET_OPEN,
        )
    )
    if rows and log_starts_at is not None and log_starts_at > start:
        # The recorder (#2235) landed in v0.5.90 and the fleet was on v0.5.88
        # when this report was written, so EVERY early window will hit this.
        # It is the same failure as a missing log, one granularity down: with
        # no stall records but a complete merge history, a period scores 100%
        # because nothing was measured — the most flattering possible way to
        # read an instrument that was switched off.
        notes.append(
            "PARTIAL WINDOW: the block log's oldest record is "
            f"{_iso(log_starts_at)}, after this window opened at "
            f"{_iso(start)}. Every period before that has merges but NO stall "
            "records, so its score is unmeasured, not perfect — the recorder "
            "was not running yet. Trust the periods from "
            f"{_iso(log_starts_at)} onward."
        )

    totals = (
        {"count": grand_total, "share_pct": 100.0 if grand_total else 0.0}
        if rows
        else None
    )

    return ReportResult(
        report_id="queue-outcomes",
        generated_at=end if generated_at is None else float(generated_at),
        window=(start, end),
        columns=list(QUEUE_OUTCOMES_COLUMNS),
        column_meta=list(QUEUE_OUTCOMES_COLUMN_META),
        rows=rows,
        notes=notes,
        totals=totals,
        chart=queue_outcomes_chart(rows, n_periods),
    )


def queue_outcomes_chart(
    rows: Sequence[Mapping[str, Any]], n_periods: int
) -> ChartSpec | None:
    """The chart declaration for a ``queue-outcomes`` fold (#2271).

    Exactly the two views the report's own description already promises:
    ``24h`` is a single period, so it is **one stacked bar per bucket** over
    the categories in it; ``7d``/``4w`` are the same arithmetic in 7 daily /
    4 weekly points, so they are **one trendline per bucket** over
    ``period_start``.  Both derive from ``count`` — the column the table
    renders — so there is nothing to keep in sync.

    ``None`` when there is nothing to plot: an empty fold is an EMPTY result,
    and an axis with no marks on it reads as a zero score rather than as no
    measurement.
    """
    if not rows:
        return None
    if n_periods > 1:
        return ChartSpec(
            kind="line",
            series=(ChartSeries(label="Entries", column="count"),),
            x="period_start",
            group_by="bucket",
            title="Outcomes per period",
            y_label="Entries",
        )
    # `x` and `group_by` are deliberately the same column. A terminal chart
    # has a legend but no per-tick category text, so the legend is the only
    # place a bucket NAME can appear — grouping on the axis column is what
    # puts "succeeded / auto_resolved_* / human / open" on screen next to its
    # own colour, instead of five anonymous bars.
    return ChartSpec(
        kind="bar",
        series=(ChartSeries(label="Entries", column="count"),),
        x="bucket",
        group_by="bucket",
        stacked=True,
        title="Outcomes by bucket",
        y_label="Entries",
    )


def _headline_note(auto: int, total: int, by_design: int) -> str:
    """``(succeeded + auto_*) / total``, with the by-design-excluded variant.

    Both, always, because they answer different questions: the raw fraction is
    the operator's stated target, and the adjusted one is the only fraction
    that CAN reach 100% — a Gate-A sign-off and a policy refusal are supposed
    to stop for a human, so counting them as misses would make a working queue
    read as permanent failure (#2270).
    """
    if total <= 0:
        return "no terminal entries"
    pct = 100.0 * auto / total
    out = f"{pct:.1f}% got over the line without a human ({auto}/{total})"
    remaining = total - by_design
    if by_design:
        adjusted = (100.0 * auto / remaining) if remaining > 0 else 100.0
        out += (
            f" · excluding {by_design} that stop for a human BY DESIGN "
            f"(Gate A, policy): {adjusted:.1f}% ({auto}/{remaining})"
        )
    return out


def _queue_outcomes_location_notes(
    location: Mapping[str, Any] | None,
) -> list[str]:
    """Say where the log was read — and shout when it was not there (#1806).

    The block log is a per-host file and only the host that runs the tick
    writes one, so a reader that quietly reports zeros from the wrong machine
    has produced a *perfect score* out of a missing file.  That is the exact
    thin-client trap #1806 documents, and the one thing this report must never
    do silently.
    """
    if not location:
        return []
    path = location.get("path") or "?"
    host = location.get("host") or "?"
    if location.get("exists"):
        return [f"source: the block log on {host} ({path})."]
    return [
        f"NO BLOCK LOG ON THIS HOST: {path} does not exist on {host}, so this "
        "report has no input and the table above is EMPTY — not a clean "
        "sweep. The log is written by the drive-queue tick and is per-host "
        "(#2235), so run this on the tick host, or point a board_service "
        "thin client at that host's daemon and let it answer.",
    ]


def _queue_outcomes_caveats(
    rows: Sequence[Mapping[str, Any]],
    *,
    open_before_window: int,
    unclassified_label: str,
    open_bucket: str,
) -> list[str]:
    from coord.block_log import BUCKET_AUTO_RESCUE  # noqa: PLC0415

    notes: list[str] = []
    if not rows:
        return notes
    if not any(r["bucket"] == BUCKET_AUTO_RESCUE for r in rows):
        notes.append(
            f"`{BUCKET_AUTO_RESCUE}` is 0 because nothing writes it yet — the "
            "rescue agent (#2268) does not exist. It is modelled as its own "
            "series from day one so this report does not change shape when it "
            "lands, and so 'a deterministic arm fixed it' never quietly "
            "becomes 'an agent judged it'."
        )
    unclassified = sum(
        r["count"] for r in rows if r["category"] == unclassified_label
    )
    if unclassified:
        notes.append(
            f"{unclassified} episode(s) have no cause at all and are grouped "
            f"as '{unclassified_label}' — a stall nobody has diagnosed. Run "
            "`coord drive-queue diagnose` (#2276) to fill that column; until "
            "then the category breakdown under-reports every real cause."
        )
    still_open = sum(r["count"] for r in rows if r["bucket"] == open_bucket)
    if still_open:
        notes.append(
            f"{still_open} entr(y/ies) are still stalled. Read this beside the "
            "headline: a queue that stops needing interventions by leaving "
            "everything blocked forever scores well on `human` and badly here."
        )
    if open_before_window:
        notes.append(
            f"{open_before_window} of those stalled before this window opened "
            "and are folded into its first period — they are counted, not "
            "dropped, because the oldest unresolved stalls are exactly the "
            "ones a window would otherwise hide."
        )
    return notes


def _default_block_log_episodes() -> list[dict]:
    """Every episode in this host's block log, oldest first.

    The WHOLE log, never a windowed read: :func:`coord.block_log.read_events`
    filters raw records, and an episode whose `enter` fell before the window
    would arrive as an orphan `resolve` that
    :func:`coord.block_log.episodes` correctly drops — silently deleting
    exactly the long stalls this report exists to count.  Windowing happens on
    the paired episode, in :func:`fold_queue_outcomes`.  A whole-file parse is
    the shape that module already commits to (it rotates at 4 MiB for this
    reason).
    """
    from coord.block_log import episodes, read_events  # noqa: PLC0415

    return episodes(read_events())


def _fetch_merged_keys(
    *,
    since: float,
    until: float,
    repo: str | None,
    fetch: Callable[..., Mapping[str, Any]],
) -> tuple[list[tuple[str, float]], list[str]]:
    """``merged`` audit events in the window, as ``[(key, ts)]`` + notes.

    Fails **soft**: a report whose `succeeded` count is missing under-states
    the headline (never over-states it), so a broken audit read is worth a
    loud note rather than a dead report.
    """
    try:
        entries, truncated = fetch_audit_window(
            since=since,
            until=until,
            repo=repo,
            fetch=fetch,
            category=_MERGE_AUDIT_CATEGORY,
            event_type=_MERGE_AUDIT_EVENT,
        )
    except Exception as exc:  # noqa: BLE001 — surfaced as a note, not a crash
        return [], [
            "WARNING: the audit trail could not be read "
            f"({type(exc).__name__}: {exc}) — the `succeeded` bucket (merged "
            "with no stall) is MISSING from this run, so the headline is a "
            "lower bound, not the real number."
        ]

    out: list[tuple[str, float]] = []
    for entry in entries:
        if entry.get("event_type") != _MERGE_AUDIT_EVENT:
            continue
        repo_name, issue = entry.get("repo"), entry.get("issue")
        if not repo_name or issue is None:
            continue
        out.append((f"{repo_name}#{int(issue)}", float(entry.get("ts") or 0.0)))
    notes: list[str] = []
    if truncated:
        notes.append(
            "TRUNCATED: the audit trail's merge events could not be fully "
            "fetched for this window, so the `succeeded` bucket is a lower "
            "bound. Use a shorter window for a complete answer."
        )
    return out, notes


def run_queue_outcomes(
    *,
    window: str = "24h",
    until: str = "",
    repo: str = "",
    now: float | None = None,
    episode_source: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
    fetch: Callable[..., Mapping[str, Any]] | None = None,
    location: Mapping[str, Any] | None = None,
) -> ReportResult:
    """Read this host's block log (+ the merge events) and fold it.

    ``now``/``episode_source``/``fetch``/``location`` are test seams; the
    report's own parameters are ``window``/``until``/``repo``.

    Refuses to invent a score when the log is not here: on a host with no
    ``queue-block-log.jsonl`` this returns an EMPTY result — columns intact,
    zero rows — with a note naming the host and the path, rather than a table
    of zeros that reads as a perfect week (#1806, and this issue's own
    acceptance).
    """
    from coord.block_log import log_location as _log_location  # noqa: PLC0415

    generated_at = time.time() if now is None else float(now)
    end = parse_timestamp(until) if until else generated_at
    start, end, period = resolve_queue_outcomes_window(window, end)

    where = dict(_log_location() if location is None else location)
    if not where.get("exists"):
        return fold_queue_outcomes(
            (), (start, end), period_seconds=period,
            generated_at=generated_at, log_location=where,
        )

    source = _default_block_log_episodes if episode_source is None else episode_source
    episodes = list(source() or [])
    # Before the repo filter: "when did this log start recording?" is a fact
    # about the FILE, and a repo that happens to have stalled late must not
    # make the whole log look younger than it is.
    entered = [float(ep.get("entered_at") or 0.0) for ep in episodes]
    log_starts_at = min((t for t in entered if t > 0), default=None)
    if repo:
        episodes = [
            ep for ep in episodes
            if str(ep.get("key") or "").split("#")[0] == repo
        ]

    merged, notes = _fetch_merged_keys(
        since=start,
        until=end,
        repo=repo or None,
        fetch=_default_fetch if fetch is None else fetch,
    )

    return fold_queue_outcomes(
        episodes,
        (start, end),
        period_seconds=period,
        merged=merged,
        generated_at=generated_at,
        log_location=where,
        log_starts_at=log_starts_at,
        extra_notes=notes,
    )


# ── completed: what left the pipeline inside a window ──────────────────────
#
# #2454.  #2405 built exactly this table as a Pipeline-local detail tab
# (`tui/src/app/pipeline.rs`'s `completed_rows`) rather than a catalogue
# entry, because the interaction it wanted — row click opens that issue's
# Pipeline detail — could only be expressed as a per-report `match` inside
# `tui/src/app/reports.rs`, which is precisely the coupling that module
# exists to prevent.  #2454 changes the interaction (right-click → "View on
# Board", which needs row *identity* and not row *content*, declared
# generically by `RowIdentity` above), so the report can live here where it
# belongs: filterable by time range and repo, sortable, and exportable
# through the panel's existing CSV action, instead of only inside one tab.
#
# The fold is a port of `completed_rows`' rules, not a new definition of
# "done":
#
#   * an issue is completed when it is **closed** OR its `merge_queue` row
#     says `merged` (the PR closed it via `fixes #N` before the brain synced
#     the GitHub close — `pipeline_lifecycle_section`'s rule 1 and rule 3),
#   * ENDED is the merged `merge_queue` row's `last_attempt`, falling back to
#     the **max** `finished_at` across the issue's assignments (`issue_done_at`),
#   * STARTED is the **min** `dispatched_at` across them (`issue_started_at`),
#   * the window is applied to ENDED, and an issue with no ENDED at all is
#     dropped and *counted in a note* — it cannot be placed in any time range,
#     the same call `completed_rows` documents making.
#
# Deliberately NOT ported: the epic-aggregation branch (`epic_lifecycle_section`,
# #1253).  A tracking issue whose children are all done but which is itself
# still open reads as in-progress here.  That branch is a *sidebar bucketing*
# rule with no server-side counterpart, and inventing one is a behaviour
# change to "what is done", which this issue is not.  See the note this fold
# emits when it drops such rows for lack of an ENDED timestamp.

COMPLETED_COLUMNS = [
    "repo",
    "issue",
    "title",
    "started_at",
    "ended_at",
    # #2472's four, plus #2825's `cache_read`. #2472's own five (`repo`
    # through `ended_at` above) keep the indices they had — the client
    # addresses columns by INDEX for sorting (`reports_sort_by_column`), so
    # THAT boundary is never interleaved. `cost_total` shifting from index 8
    # to 9 here is accepted, not an oversight: it mirrors `usage`'s own
    # precedent (#2786 inserted `cache_read`/`cache_create`/`turns` ahead of
    # `cost_captured`/`cost_est`/`cost_total` there too), and grouping token
    # counts together beats preserving one more index.
    "legs",
    "tokens_in",
    "tokens_out",
    # #2825: `cache_read` was already on the wire as a row key (see
    # `_completed_spend`) — ~98% of a `work` leg's input, sitting next to a
    # `tokens_in` that is ~0.001% of it. Without this column, `Tok Out`
    # (uncapped) next to `tokens_in` (raw uncached input) reads as "output
    # dwarfs input", which is backwards by five orders of magnitude. Reuses
    # `_USAGE_COLUMN_META`'s `cache_read` verbatim.
    "cache_read",
    "cost_total",
]

# One entry per COMPLETED_COLUMNS entry, same order (#1760).
#
# `repo` is present even though #2405's grid had no repo column: that grid
# folded the repo into its `C#2345` issue ref, which works because
# `CoordApp::repo_tag` shortens against the repos actually on screen. A
# server-side report has no such screen context, and its `repo` param
# defaults to *all repos*, so the pair has to be two real columns — which is
# also what `issue-activity` already does, and what `RowIdentity` reads.
#
# #2472's spend columns reuse `_USAGE_COLUMN_META`'s labels/kinds verbatim
# (`Legs`, `Raw In`, `Tok Out`, `Cache Rd`, `Total $`) rather than inventing
# near-synonyms: an operator reading `completed` and `usage` side by side is
# looking at the same numbers out of the same rollup, and two spellings of
# the same column would imply otherwise.
#
# #2825: `tokens_in` is labelled "Raw In", not "Tok In" — see the comment on
# `_USAGE_COLUMN_META["tokens_in"]` for why. Applied here too so the two
# reports keep agreeing.
#
# ONE cost column, not `usage`'s `cost_captured`/`cost_est` split. `usage`
# splits because it is the dedicated cost report, where "some legs have no
# captured cost, don't silently price at $0" (#1763) is the headline; here the
# headline is still the time-range list, and a 5-column table growing to 11
# would push `title` off a narrow pane. Both halves still ship as extra ROW
# keys (see `_completed_spend`) — the wire contract allows row keys beyond
# `columns`, so a client that wants the split has it without another column,
# and `usage` remains the report to visit for it on screen. `cache_read` is
# the one exception (#2825): it isn't a split of an existing column, it's the
# ~98% of input spend that `tokens_in` alone cannot represent.
COMPLETED_COLUMN_META = [
    ColumnMeta(id="repo", label="Repo", kind="text"),
    ColumnMeta(id="issue", label="Issue", kind="int", align="right"),
    ColumnMeta(id="title", label="Title", kind="text", weight=3.0),
    ColumnMeta(id="started_at", label="Started", kind="timestamp"),
    ColumnMeta(id="ended_at", label="Ended", kind="timestamp"),
    ColumnMeta(id="legs", label="Legs", kind="int", align="right", weight=0.6),
    ColumnMeta(id="tokens_in", label="Raw In", kind="int", align="right"),
    ColumnMeta(id="tokens_out", label="Tok Out", kind="int", align="right"),
    ColumnMeta(id="cache_read", label="Cache Rd", kind="int", align="right"),
    ColumnMeta(id="cost_total", label="Total $", kind="money", align="right"),
]

def _completed_spend(
    assignment_rows: Sequence[Mapping[str, Any]], pricing: Any
) -> dict[tuple[str, int], dict[str, Any]]:
    """Per-issue ``legs``/tokens/cost, keyed ``(repo_name, issue_number)``.

    A thin adapter over :func:`coord.usage_rollup.rollup`, **not** a second
    cost calculator: the pricing rules, the captured-vs-estimated split and
    the "never silently priced at $0" unknown-model rule all stay in
    ``usage_rollup``, exactly as #1763 requires, and the numbers are shaped by
    the same :func:`_usage_metrics` that builds a ``usage`` row.  ``legs``
    therefore counts agent SESSIONS (every dispatch attempt and retry), which
    is what ``GroupRollup.legs`` already means — no new arithmetic here.

    One keying detail worth being explicit about: ``rollup``'s ``IssueKey``
    books a leg to :func:`~coord.usage_rollup.row_issue_number`, i.e.
    ``for_issue_number`` when the leg carries one (#1553's oracle-loop
    attribution), while this fold's own ``first_dispatch``/``last_finish`` key
    on the raw ``issue_number``.  That divergence is DELIBERATE: the whole
    point of #2472 is that ``completed``'s cost for an issue equals what
    ``usage`` reports for the same issue, so the cost columns must use
    ``usage``'s attribution rule.  The timestamps keep #2454's, which is a
    port of ``completed_rows``.
    """
    from coord.usage_rollup import IssueKey, TimeWindow, rollup  # noqa: PLC0415

    # THE WINDOW IS UNBOUNDED — the spend figures are the issue's *lifetime*
    # spend, not spend inside the report's own window.
    #
    # `completed`'s `since`/`until` filters on ENDED — "what finished in this
    # range". An issue's legs routinely ran before that window opened (a 24h
    # window over an issue dispatched a week ago is the normal case, not the
    # edge case), so windowing the cost the same way would answer "what did
    # this issue cost me *today*" under a column an operator reads as "what did
    # finishing this issue cost me" — and would report a smaller number every
    # time the window narrowed, for an issue whose true cost never changed.
    # `usage` is where a windowed spend question belongs; it has a `window`
    # param and this report does not.
    #
    # Consequence worth stating: an unbounded `TimeWindow` still drops a leg
    # with NEITHER `dispatched_at` NOR `finished_at` (`leg_in_window` needs one
    # of the two to be in range, and `contains(None)` is False). Such a row is
    # not a session that ran — it is a row with no evidence it ever started —
    # and `usage` excludes it on the same rule.
    result = rollup(
        [dict(a) for a in assignment_rows],
        group_by="issue",
        window=TimeWindow(),
        pricing=pricing,
    )
    spend: dict[tuple[str, int], dict[str, Any]] = {}
    for key, group in result.groups.items():
        if not isinstance(key, IssueKey) or not key.repo_name:
            continue
        spend[(str(key.repo_name), int(key.issue_number))] = _usage_metrics(group)
    return spend


#: What a row gets when the rollup saw no legs at all for its issue — a real
#: zero, not a missing key. An issue can be closed with no assignment ever
#: dispatched against it (closed by hand, or fixed as a drive-by in someone
#: else's PR), and that row's honest answer is "0 legs, $0", not a blank cell
#: a client would have to guess the meaning of.
_COMPLETED_NO_LEGS: dict[str, Any] = {
    "legs": 0,
    "tokens_in": 0,
    "tokens_out": 0,
    "cache_read": 0,
    "cache_create": 0,
    "turns": 0,
    "cost_captured": 0.0,
    "cost_est": 0.0,
    "cost_total": 0.0,
    "duration_secs": 0.0,
    "open_legs": 0,
    "unknown_model_legs": 0,
    "tok_per_turn": 0,
}


def fold_completed(
    issues: Iterable[Mapping[str, Any]],
    assignments: Iterable[Mapping[str, Any]],
    merge_queue: Iterable[Mapping[str, Any]],
    window: tuple[float, float],
    *,
    repo: str = "",
    generated_at: float | None = None,
    pricing: Any = None,
    extra_notes: Sequence[str] = (),
) -> ReportResult:
    """Fold the board's own tables into one row per issue that *finished*
    inside ``window``.

    Pure — every input is a plain sequence of mappings, so the whole rule set
    above is testable without a database (same posture as
    :func:`fold_issue_activity`).  Rows come back newest-ENDED first, which
    is #2405's default order; the client re-sorts by column head-click.

    *pricing* left at ``None`` falls through to ``usage_rollup``'s built-in
    default rates, which is correct for a unit test and **not** what the
    runner does — :func:`run_completed` loads ``coordinator.yml`` and passes
    the real ``pricing:`` block, the same seam :func:`fold_usage` uses and for
    the same #1763 reason.
    """
    start, end = window
    generated_at = time.time() if generated_at is None else float(generated_at)
    repo_filter = (repo or "").strip()

    issue_rows = list(issues)
    assignment_rows = list(assignments)
    merge_rows = list(merge_queue)

    # `merge_queue` may hold several rows for one issue (a re-queue after a
    # failed merge). Keep the newest MERGED one — `last_attempt` is when the
    # merge landed, which is exactly the ENDED value we want.
    merged_at: dict[tuple[str, int], float] = {}
    for m in merge_rows:
        if str(m.get("state") or "") != "merged":
            continue
        name = str(m.get("repo_name") or "")
        number = m.get("issue_number")
        stamp = m.get("last_attempt")
        if not name or number is None or stamp is None:
            continue
        try:
            key = (name, int(number))
            value = float(stamp)
        except (TypeError, ValueError):
            continue
        if value > merged_at.get(key, float("-inf")):
            merged_at[key] = value

    # One pass over assignments for both timestamps, keyed the same way
    # `issue_done_at`/`issue_started_at` key theirs: coord-LOCAL repo name
    # plus issue number, so repo-a#7 and repo-b#7 can never contribute to
    # each other.
    first_dispatch: dict[tuple[str, int], float] = {}
    last_finish: dict[tuple[str, int], float] = {}
    for a in assignment_rows:
        name = str(a.get("repo_name") or "")
        number = a.get("issue_number")
        if not name or number is None:
            continue
        try:
            key = (name, int(number))
        except (TypeError, ValueError):
            continue
        dispatched = a.get("dispatched_at")
        if dispatched is not None:
            try:
                value = float(dispatched)
            except (TypeError, ValueError):
                value = None
            if value is not None and value < first_dispatch.get(key, float("inf")):
                first_dispatch[key] = value
        finished = a.get("finished_at")
        if finished is not None:
            try:
                value = float(finished)
            except (TypeError, ValueError):
                value = None
            if value is not None and value > last_finish.get(key, float("-inf")):
                last_finish[key] = value

    spend = _completed_spend(assignment_rows, pricing)

    rows: list[dict] = []
    no_end_time = 0
    for issue in issue_rows:
        name = str(issue.get("repo_name") or "")
        number = issue.get("number")
        if not name or number is None:
            continue
        try:
            key = (name, int(number))
        except (TypeError, ValueError):
            continue
        is_closed = str(issue.get("state") or "open") == "closed"
        if not is_closed and key not in merged_at:
            continue
        if repo_filter and name != repo_filter:
            continue
        ended = merged_at.get(key, last_finish.get(key))
        if ended is None:
            # No END timestamp anywhere — it cannot be placed in *any* time
            # range, so it is dropped rather than silently windowed to now
            # (`completed_rows` makes the same call). Counted in a note below
            # so "my issue is missing" has a visible answer.
            no_end_time += 1
            continue
        if ended < start or ended > end:
            continue
        rows.append(
            {
                "repo": name,
                "issue": key[1],
                "title": str(issue.get("title") or "") or None,
                "started_at": first_dispatch.get(key),
                "ended_at": ended,
                # #2472. `**` copies the pairs into this row's own dict, so
                # the shared `_COMPLETED_NO_LEGS` constant can never end up
                # aliased by two zero-leg rows.
                **spend.get(key, _COMPLETED_NO_LEGS),
            }
        )

    # Newest-ended first, with `(repo, issue)` as a total secondary key so a
    # re-run can never reshuffle rows that tie on the same second (#2405's
    # `sort_completed_rows` takes the same precaution for the same reason).
    rows.sort(key=lambda r: (-float(r["ended_at"]), r["repo"], r["issue"]))

    notes: list[str] = list(extra_notes)
    if no_end_time:
        notes.append(
            f"{no_end_time} completed issue(s) have no end timestamp (no merged "
            "merge_queue row and no assignment finished_at) and are not shown — "
            "there is no time range that could contain them."
        )
    if repo_filter and not any(
        str(i.get("repo_name") or "") == repo_filter for i in issue_rows
    ):
        notes.append(
            f"No issue in this board belongs to repo {repo_filter!r} — check the "
            "coord-local repo name (the one in coordinator.yml), not the GitHub slug."
        )
    # #1763's rule is "never silently priced at $0", and `cost_total` here is
    # exactly the number that would go silently short. So it IS noted — but as
    # ONE aggregate line rather than `fold_usage`'s line-per-issue: this report
    # is a time-range list that can easily carry a hundred rows, and a hundred
    # near-identical notes would bury the two above that are about the list
    # itself. The per-issue breakdown is a question for `usage`, which the note
    # names.
    unpriced = sorted(
        f"{r['repo']}#{r['issue']}" for r in rows if r.get("unknown_model_legs")
    )
    if unpriced:
        shown = ", ".join(unpriced[:5])
        more = f" (and {len(unpriced) - 5} more)" if len(unpriced) > 5 else ""
        notes.append(
            f"{len(unpriced)} issue(s) ran leg(s) on a model with no entry in "
            f"the loaded `pricing:` config — {shown}{more}. Their tokens are "
            "counted but that spend is NOT in `Total $` (never silently priced "
            "at $0), so those rows read LOW. Add a rate for the model to "
            "coordinator.yml, or run the `usage` report for the per-issue "
            "breakdown."
        )

    return ReportResult(
        report_id="completed",
        generated_at=generated_at,
        window=(start, end),
        columns=list(COMPLETED_COLUMNS),
        column_meta=list(COMPLETED_COLUMN_META),
        rows=rows,
        notes=notes,
    )


def _default_completed_source() -> tuple[list[dict], list[dict], list[dict]]:
    """``(issues, assignments, merge_queue)`` straight off the local board DB.

    A plain read — three ``SELECT``s, no tick, no reconcile — mirroring
    :func:`_lookup_titles`' posture (and its "never fail the report over a DB
    that isn't there" degradation).  Like ``queue-outcomes``' block log this
    is a *host-local* source: run the report where the board lives, or let
    that host's daemon answer it over ``GET /report/completed``.
    """
    try:
        from coord import sql  # noqa: PLC0415
        from coord.db import get_connection  # noqa: PLC0415

        conn = get_connection()
        issues = [
            dict(r)
            for r in sql.execute(
                conn, "SELECT repo_name, number, title, state FROM issues"
            ).fetchall()
        ]
        # #2472 widens this one SELECT rather than adding a second source
        # function: the spend columns fold out of the SAME assignment rows the
        # timestamps do, so a second round trip would re-read the same table to
        # get columns this one could have carried. Everything past
        # `finished_at` is what `usage_rollup.rollup`/`leg_cost` read — token
        # counts, the captured `cost_usd`, the `model` its estimate is keyed
        # by, and `for_issue_number` for #1553's attribution. `type` is NOT
        # selected: it only feeds `usage`'s per-stage drill-down, which this
        # report does not emit.
        assignments = [
            dict(r)
            for r in sql.execute(
                conn,
                "SELECT repo_name, issue_number, for_issue_number, "
                "dispatched_at, finished_at, input_tokens, output_tokens, "
                "cache_read_tokens, cache_creation_tokens, cost_usd, model "
                "FROM assignments",
            ).fetchall()
        ]
        merge_queue = [
            dict(r)
            for r in sql.execute(
                conn, "SELECT repo_name, issue_number, state, last_attempt FROM merge_queue"
            ).fetchall()
        ]
    except Exception:  # noqa: BLE001 — an unreadable board is an empty report
        return [], [], []
    return issues, assignments, merge_queue


def run_completed(
    *,
    since: str = "24h",
    until: str = "",
    repo: str = "",
    now: float | None = None,
    source: Callable[[], tuple[
        Sequence[Mapping[str, Any]],
        Sequence[Mapping[str, Any]],
        Sequence[Mapping[str, Any]],
    ]] | None = None,
    pricing: Any = None,
) -> ReportResult:
    """Read the board and fold it.  ``now``/``source``/``pricing`` are test
    seams (mirrors :func:`run_issue_activity`); the report's own parameters are
    ``since``/``until``/``repo`` — the same three, with the same vocabulary
    and the same validators, that ``issue-activity`` uses."""
    generated_at = time.time() if now is None else float(now)
    end = parse_timestamp(until) if until else generated_at
    start = end - parse_duration(since)
    source_fn = _default_completed_source if source is None else source
    issues, assignments, merge_queue = source_fn()

    # Same seam, same reason as `run_usage`: the estimated half of `cost_total`
    # has to be priced off the fleet's OWN `pricing:` block, and a config that
    # could not be loaded says so in a note instead of silently falling back
    # (#1763).
    extra_notes: list[str] = []
    if pricing is None:
        pricing, extra_notes = _load_pricing()

    return fold_completed(
        issues,
        assignments,
        merge_queue,
        (start, end),
        repo=repo,
        generated_at=generated_at,
        pricing=pricing,
        extra_notes=extra_notes,
    )


# ── trend: time-bucketed merge throughput + cost efficiency (#2826) ────────
#
# One question: does throughput rise while cost per merged issue falls? None
# of the six reports above can show it — they are all point-in-time
# (`drive-queue-status`, `decisions`), per-issue (`issue-activity`,
# `completed`, `usage`), or per-episode (`queue-outcomes`), never
# per-time-bucket. This is the one BUCKETED fold: a row is a bucket, not an
# issue, not a period-of-outcomes.
#
# "MERGED" here means exactly what `completed` means by ENDED (#2472): the
# issue is closed, or its `merge_queue` row says `merged`; the timestamp a
# bucket is chosen by is the merge timestamp, falling back to the last
# assignment to finish. An issue with neither is placed in no bucket at all
# — same drop, same reason, as `completed`. Keeping this identical to
# `completed` matters more than usual here: a trend line whose denominator
# quietly differs from the `completed` panel's row count would be worse than
# no trend line.
#
# `cost_per_issue`/`legs_per_issue` are a TRAILING-WINDOW mean over the
# trailing `TREND_TRAILING_BUCKETS` buckets (this one plus the previous
# N-1), **not** a per-bucket mean. A bucket with zero merges has an
# UNDEFINED mean cost, and the chart widget this report exists to feed takes
# a plain evenly-spaced `Vec<f64>` with no way to express a gap — no NaN /
# `is_finite` guard anywhere in quadraui's `primitives/chart.rs` or
# `tui/chart.rs` — so an empty bucket emitted as `0.0` would draw a dramatic
# cost collapse that never happened, which is exactly the graph the
# hypothesis hopes to see, falsely. At `1d`'s hourly granularity most
# buckets genuinely are empty, so this is not an edge case. Where even the
# trailing window has zero merges, the column comes back `None` — never
# `0.0` — and a note says how many buckets that hit. A CUMULATIVE mean was
# considered and rejected: it is dominated by history and would hide exactly
# the recent improvement this report exists to show.
#
# Cost/legs are not a second calculator: `fold_completed` above already
# derives them, per issue, via `_completed_spend` -> `usage_rollup.rollup`
# (lifetime spend, not windowed — see that function's own comment for why).
# This fold reuses `fold_completed`'s own ROWS — fetched over a window
# widened backward far enough to give the *earliest* reported bucket the
# same trailing-window WIDTH every other bucket gets — rather than
# re-deriving anything.

TREND_RANGE_CHOICES = ("1d", "3d", "7d", "1m")

#: range -> (bucket_seconds, point_count). Fixed point counts (~24-30 per
#: range, per #2826) keep the x-axis readable at any pane width and keep a
#: client from having to resample.
_TREND_RANGES: dict[str, tuple[float, int]] = {
    "1d": (3600.0, 24),  # hourly
    "3d": (3 * 3600.0, 24),  # 3-hourly
    "7d": (6 * 3600.0, 28),  # 6-hourly
    "1m": (86400.0, 30),  # daily
}

#: What each range's bucket width is called in a note — kept as a table
#: rather than derived from the seconds so the wording never has to handle
#: an odd fraction of an hour.
_TREND_RANGE_BUCKET_LABEL: dict[str, str] = {
    "1d": "hourly",
    "3d": "3-hourly",
    "7d": "6-hourly",
    "1m": "daily",
}

#: Width of the trailing window `cost_per_issue`/`legs_per_issue` average
#: over, in BUCKETS rather than a fixed duration — a fixed duration would
#: mean a different number of buckets contribute at every range, which is
#: not what "~5 buckets" (#2826) means. 5 is wide enough that most windows
#: contain at least one merge even at `1d`'s hourly granularity, without
#: being so wide it smears away the recent-improvement signal the report
#: exists to show.
TREND_TRAILING_BUCKETS = 5

TREND_COLUMNS = ["bucket_start", "merged", "cost_per_issue", "legs_per_issue"]

# One entry per TREND_COLUMNS entry, same order (#1760). `cost_per_issue`
# reuses the `money` kind `usage`/`completed` already declare;
# `legs_per_issue` is a per-bucket MEAN, not a count, so it is `float`
# rather than their `int` `legs` — an open-vocabulary `kind` (same rule
# every other one here follows), so a client that predates it falls back to
# plain stringification, which still reads fine for a number.
TREND_COLUMN_META = [
    ColumnMeta(id="bucket_start", label="Bucket", kind="timestamp"),
    ColumnMeta(id="merged", label="Merged", kind="int", align="right", weight=0.6),
    ColumnMeta(id="cost_per_issue", label="$/Issue", kind="money", align="right"),
    ColumnMeta(
        id="legs_per_issue", label="Legs/Issue", kind="float", align="right", weight=0.8
    ),
]


def resolve_trend_range(value: str) -> tuple[float, int]:
    """``(bucket_seconds, point_count)`` for a ``range`` preset.  Raises
    :class:`ReportError` on an unknown value."""
    try:
        return _TREND_RANGES[value]
    except KeyError:
        raise ReportError(
            f"invalid value for 'range': {value!r} — "
            f"allowed values: {', '.join(TREND_RANGE_CHOICES)}"
        ) from None


def fold_trend(
    issues: Iterable[Mapping[str, Any]],
    assignments: Iterable[Mapping[str, Any]],
    merge_queue: Iterable[Mapping[str, Any]],
    window_end: float,
    *,
    range_: str = "7d",
    repo: str = "",
    generated_at: float | None = None,
    pricing: Any = None,
    extra_notes: Sequence[str] = (),
) -> ReportResult:
    """Bucket MERGED issues (see the #2826 section comment above for the
    exact definition) into fixed-width buckets ending at ``window_end``, one
    row per bucket.

    **Pure** — no DB, no daemon, no clock beyond the explicit ``window_end``
    (mirrors :func:`fold_completed`). Reuses :func:`fold_completed`'s own
    fold over the SAME ``issues``/``assignments``/``merge_queue`` inputs,
    fetched over a window widened backward by the trailing lookback, rather
    than re-deriving which issues are "done" or what they cost.
    ``generated_at`` defaults to ``window_end``, same convention as every
    other fold here.

    ``range_`` (not ``range``) — the report's own parameter IS named
    ``range``, but shadowing the builtin in a function that needs to call
    ``range()`` in the bucket loop below is a trap, not a style nit; see
    :func:`run_trend`, which owns the ``range`` name at the wire boundary.
    """
    bucket_seconds, point_count = resolve_trend_range(range_)
    window_end = float(window_end)
    generated_at = window_end if generated_at is None else float(generated_at)
    window_start = window_end - bucket_seconds * point_count
    # Widen the fetch so the FIRST reported bucket's trailing window is the
    # same width as every other bucket's — without this, bucket 0's
    # "trailing window" would silently mean "everything before it", i.e. a
    # narrower lookback than the rest of the series gets.
    fetch_start = window_start - bucket_seconds * (TREND_TRAILING_BUCKETS - 1)

    completed = fold_completed(
        issues,
        assignments,
        merge_queue,
        (fetch_start, window_end),
        repo=repo,
        generated_at=generated_at,
        pricing=pricing,
    )

    bucket_starts = _period_bounds(window_start, window_end, bucket_seconds)
    # bucket index -> [(cost_total, legs), ...] for issues merged in it.
    # Indices below 0 are merges in the trailing lookback ONLY, ahead of the
    # first reported bucket — they feed that bucket's trailing mean without
    # ever being reported as a (nonexistent) bucket of their own. `_period_bounds`
    # / `_period_index` (used by `queue-outcomes`) assume a single in-range
    # index, so the assignment is inlined here rather than reused.
    per_bucket: dict[int, list[tuple[float, float]]] = {}
    for row in completed.rows:
        ended = row.get("ended_at")
        if ended is None:
            continue
        idx = int((float(ended) - window_start) // bucket_seconds)
        idx = min(idx, point_count - 1)
        per_bucket.setdefault(idx, []).append(
            (float(row.get("cost_total") or 0.0), float(row.get("legs") or 0))
        )

    rows: list[dict[str, Any]] = []
    empty_trailing = 0
    for i, start in enumerate(bucket_starts):
        merged_here = per_bucket.get(i, ())
        trailing: list[tuple[float, float]] = []
        for j in range(i - TREND_TRAILING_BUCKETS + 1, i + 1):
            trailing.extend(per_bucket.get(j, ()))
        if trailing:
            cost_per_issue = round(
                sum(c for c, _ in trailing) / len(trailing), _USAGE_COST_PLACES
            )
            legs_per_issue = round(sum(legs for _, legs in trailing) / len(trailing), 2)
        else:
            cost_per_issue = None
            legs_per_issue = None
            empty_trailing += 1
        rows.append(
            {
                "bucket_start": start,
                "merged": len(merged_here),
                "cost_per_issue": cost_per_issue,
                "legs_per_issue": legs_per_issue,
            }
        )

    total_merged = sum(r["merged"] for r in rows)
    bucket_label = _TREND_RANGE_BUCKET_LABEL[range_]

    notes: list[str] = list(extra_notes) + list(completed.notes)
    notes.append(
        f"{total_merged} issue(s) merged across {point_count} {bucket_label} "
        "buckets. `cost_per_issue`/`legs_per_issue` are a TRAILING mean over "
        f"the {TREND_TRAILING_BUCKETS} most recent buckets (this one plus "
        f"the previous {TREND_TRAILING_BUCKETS - 1}), not a per-bucket mean "
        "— a bucket with zero merges has no defined mean cost of its own."
    )
    if empty_trailing:
        notes.append(
            f"{empty_trailing} of {point_count} bucket(s) show `null` for "
            "cost_per_issue/legs_per_issue — even their trailing window saw "
            "no merge at all. That is a real gap, not a zero cost; render it "
            "as one rather than a dip to $0."
        )
    if range_ in ("1d", "3d"):
        notes.append(
            f"`{range_}` buckets are small (often a handful of merges each) "
            "— expect a noisy line, not a smooth one. `7d`/`1m` are where a "
            "real throughput-up/cost-down trend is actually testable."
        )

    return ReportResult(
        report_id="trend",
        generated_at=generated_at,
        window=(window_start, window_end),
        columns=list(TREND_COLUMNS),
        column_meta=list(TREND_COLUMN_META),
        rows=rows,
        notes=notes,
    )


def run_trend(
    *,
    range: str = "7d",
    until: str = "",
    repo: str = "",
    now: float | None = None,
    source: Callable[[], tuple[
        Sequence[Mapping[str, Any]],
        Sequence[Mapping[str, Any]],
        Sequence[Mapping[str, Any]],
    ]] | None = None,
    pricing: Any = None,
) -> ReportResult:
    """Read the board and fold it.  ``now``/``source``/``pricing`` are test
    seams (mirrors :func:`run_completed`); the report's own parameters are
    ``range``/``until``/``repo``."""
    generated_at = time.time() if now is None else float(now)
    window_end = parse_timestamp(until) if until else generated_at
    # Same source as `completed` — the merged-issue fold this report buckets
    # is exactly `fold_completed`'s, over the same three tables.
    source_fn = _default_completed_source if source is None else source
    issues, assignments, merge_queue = source_fn()

    # Same seam, same reason as `run_completed`/`run_usage`: the estimated
    # half of `cost_per_issue` has to be priced off the fleet's OWN
    # `pricing:` block, and a config that could not be loaded says so in a
    # note instead of silently falling back (#1763).
    extra_notes: list[str] = []
    if pricing is None:
        pricing, extra_notes = _load_pricing()

    return fold_trend(
        issues,
        assignments,
        merge_queue,
        window_end,
        range_=range,
        repo=repo,
        generated_at=generated_at,
        pricing=pricing,
        extra_notes=extra_notes,
    )


# ── deprecated-routes: evidence for RPC retirement (#1945) ────────────────
#
# A live snapshot, same posture as `drive-queue-status`: no window, no
# `repo` param — a deprecated *route* is not scoped to one repo the way an
# issue is. Folds `coord.deprecation_telemetry`'s audit rows (category
# "deprecation", written by the daemon's `_DeprecatedRouteTelemetryMiddleware`
# on every call to a route in `coord.serve_app.RPC_SUPERSEDED_BY_RESOURCE`)
# into one row per deprecated route: last call, distinct calling
# client+version pairs, and a total count.
#
# The one thing this report exists to get right that a log grep would not:
# telling "zero calls" (a real, actionable signal — safe to consider for
# retirement) apart from "no data collected" (telemetry not running for
# THIS route or ANY route — an old daemon build, `audit.level: business`
# dropping the operational-tier rows, or the audit table's own retention
# trim — which must never be read as "safe").  The distinction is made at
# the corpus level: if literally zero deprecation-category rows exist
# anywhere (any route), collection cannot be confirmed live, so EVERY route
# reads `no_data` rather than the misleadingly reassuring `zero_calls`.

DEPRECATED_ROUTES_COLUMNS = [
    "route",
    "replacement",
    "status",
    "last_call",
    "call_count",
    "clients",
]

# One entry per DEPRECATED_ROUTES_COLUMNS entry, same order (#1760).
DEPRECATED_ROUTES_COLUMN_META = [
    ColumnMeta(id="route", label="Route", kind="text", weight=1.5),
    ColumnMeta(id="replacement", label="Replacement", kind="text", weight=2.5),
    ColumnMeta(id="status", label="Status", kind="enum"),
    ColumnMeta(id="last_call", label="Last Call", kind="timestamp"),
    ColumnMeta(id="call_count", label="Calls", kind="int", align="right"),
    ColumnMeta(id="clients", label="Clients", kind="list"),
]

# `status` values.  `no_data` and `zero_calls` are BOTH "nothing seen for
# this route" — they differ only in whether that absence is trustworthy —
# so a client rendering this column must not collapse them to one meaning.
DEPRECATED_ROUTE_NO_DATA = "no_data"
DEPRECATED_ROUTE_ZERO_CALLS = "zero_calls"
DEPRECATED_ROUTE_IN_USE = "in_use"


def fold_deprecated_routes(
    entries: Iterable[Mapping[str, Any]],
    generated_at: float,
    *,
    routes: Mapping[str, str] | None = None,
) -> ReportResult:
    """Fold already-fetched deprecation-telemetry audit rows into a
    per-route snapshot.  **Pure** — no DB, no daemon, no clock.

    ``entries`` is whatever ``coord.audit.query_audit_log(category=
    "deprecation")`` (paginated by the caller — see :func:`run_deprecated_routes`)
    returned, newest-first.  ``routes`` defaults to
    ``coord.serve_app.RPC_SUPERSEDED_BY_RESOURCE`` so the set of routes this
    report covers can never drift from the set the daemon actually stamps
    ``deprecated: true`` on in the served OpenAPI spec (#1944) — there is
    exactly one source of truth for "which routes are deprecated", and this
    report reads it rather than redeclaring it (the ``routes=`` override
    exists purely so this stays a pure function a test can call without
    importing the daemon module).
    """
    if routes is None:
        from coord.serve_app import RPC_SUPERSEDED_BY_RESOURCE  # noqa: PLC0415

        routes = RPC_SUPERSEDED_BY_RESOURCE

    entries = list(entries)
    any_data = bool(entries)

    by_route: dict[str, list[Mapping[str, Any]]] = {r: [] for r in routes}
    for entry in entries:
        details = entry.get("details") or {}
        route = details.get("route")
        if route in by_route:
            by_route[route].append(entry)

    rows: list[dict[str, Any]] = []
    for route in sorted(routes):
        calls = sorted(
            by_route.get(route, []), key=lambda e: e.get("ts") or 0, reverse=True
        )
        last_call = calls[0].get("ts") if calls else None
        clients: list[str] = []
        seen: set[tuple[str, str]] = set()
        for call in calls:
            details = call.get("details") or {}
            pair = (
                details.get("client") or "unknown",
                details.get("client_version") or "unknown",
            )
            if pair not in seen:
                seen.add(pair)
                clients.append(f"{pair[0]}@{pair[1]}")
        if calls:
            status = DEPRECATED_ROUTE_IN_USE
        elif any_data:
            status = DEPRECATED_ROUTE_ZERO_CALLS
        else:
            status = DEPRECATED_ROUTE_NO_DATA
        rows.append(
            {
                "route": route,
                "replacement": routes[route],
                "status": status,
                "last_call": last_call,
                "call_count": len(calls),
                "clients": clients,
            }
        )

    notes: list[str] = []
    if not any_data:
        notes.append(
            "No deprecation telemetry has been recorded at all, for any "
            "route — this may mean genuinely zero calls, or that capture "
            "is not running (an old daemon build predating #1945, "
            "`audit.level: business` dropping operational-tier rows, or "
            "the audit table's own retention trim). Every row above reads "
            "`no_data`, not `zero_calls` — treat it as UNKNOWN, never as "
            "evidence it is safe to retire."
        )

    return ReportResult(
        report_id="deprecated-routes",
        generated_at=generated_at,
        window=(generated_at, generated_at),
        columns=list(DEPRECATED_ROUTES_COLUMNS),
        column_meta=list(DEPRECATED_ROUTES_COLUMN_META),
        rows=rows,
        notes=notes,
    )


def _default_fetch_deprecation_entries(
    generated_at: float,
) -> tuple[list[dict], bool]:
    return fetch_audit_window(since=0.0, until=generated_at, category="deprecation")


def run_deprecated_routes(
    *,
    now: float | None = None,
    fetch: Callable[[float], tuple[Sequence[Mapping[str, Any]], bool]] | None = None,
    routes: Mapping[str, str] | None = None,
) -> ReportResult:
    """Fetch every recorded deprecated-RPC-route call and fold it (#1945).

    ``fetch``/``routes`` are test seams (mirrors every other ``run_*``'s
    ``fetch=`` seam). Production always walks the FULL audit history
    (``since=0``), never a recent window — "this route has not been called
    in months" is exactly the number #1945 exists to produce, and a
    windowed report would silently hide the very evidence retirement needs.
    ``query_audit_log`` orders newest-first, so ``last_call`` is accurate
    even if the walk is truncated by the page cap; only the full
    client/version set and total ``call_count`` could then be incomplete,
    which is called out in a note exactly like every other truncated fold
    in this module.
    """
    generated_at = time.time() if now is None else float(now)
    fetch_fn = _default_fetch_deprecation_entries if fetch is None else fetch
    entries, truncated = fetch_fn(generated_at)
    result = fold_deprecated_routes(entries, generated_at, routes=routes)
    if truncated:
        result.notes.append(
            "Audit history walk hit its page cap before covering the full "
            "history — `clients`/`call_count` may be missing older calls, "
            "though `last_call` (newest-first order) is still accurate."
        )
    return result


# ── the catalogue ──────────────────────────────────────────────────────────

SINCE_PRESETS = ("1h", "6h", "24h", "3d", "7d")


def _validate_since(value: str) -> None:
    if value in SINCE_PRESETS:
        return
    try:
        parse_duration(value)
    except ReportError as exc:
        raise ReportError(
            f"invalid value for 'since': {value!r} — allowed values: "
            f"{', '.join(SINCE_PRESETS)}, or any duration like '13h' "
            "(units: s, m, h, d, w)"
        ) from exc


def _validate_until(value: str) -> None:
    if not value:
        return
    try:
        parse_timestamp(value)
    except ReportError as exc:
        raise ReportError(
            f"invalid value for 'until': {value!r} — expected epoch seconds "
            "or an ISO-8601 timestamp (e.g. '2026-08-03T09:16:00Z'), or "
            "empty for 'now'"
        ) from exc


ISSUE_ACTIVITY = ReportDef(
    id="issue-activity",
    title="Issue Activity",
    description=(
        "What moved in this window and where did it end up — the audit trail "
        "folded into one row per issue: when it started, which machines "
        "touched it, how many fix iterations it took, its Test/Review "
        "verdicts in order, whether it merged, and how its driver exited."
    ),
    params=(
        ReportParam(
            id="since",
            label="Time range",
            kind="choice",
            choices=SINCE_PRESETS,
            default="24h",
            help="How far back the window reaches from `until`. Presets, or any duration (e.g. 13h).",
            free_form=True,
            validate=_validate_since,
        ),
        ReportParam(
            id="until",
            label="Window end",
            kind="text",
            default="",
            help="Epoch seconds or ISO-8601. Empty means now.",
            validate=_validate_until,
        ),
        ReportParam(
            id="repo",
            label="Repo",
            kind="text",
            default="",
            help="Restrict to one repo by name. Empty means all repos.",
        ),
    ),
    run=run_issue_activity,
    # #2454: one row per issue, with `repo`/`issue` naming it — so a client
    # can offer per-row navigation here for free.
    row_identity=RowIdentity(repo_column="repo", issue_column="issue"),
)


COMPLETED = ReportDef(
    id="completed",
    title="Completed",
    description=(
        "Everything that left the pipeline inside a time range — one row per "
        "issue, with when it first started and when it ended. An issue counts "
        "as completed when it is closed, or when its merge_queue row says "
        "merged (the PR closed it before the brain synced the close). ENDED is "
        "the merge timestamp, falling back to the last assignment to finish; "
        "STARTED is the first dispatch. Issues with no end timestamp at all "
        "cannot be placed in a time range and are counted in a note instead. "
        "Each row also carries what it cost: LEGS is how many agent sessions "
        "(dispatches and retries) ran against the issue, and the token and "
        "dollar figures are the issue's WHOLE-LIFE spend — not just the part "
        "inside this window — so narrowing the range never shrinks them. Use "
        "the `usage` report for windowed spend or the captured/estimated split."
    ),
    params=(
        # The same three params, the same vocabulary and the same validators
        # as `issue-activity` above (#2270's rule: follow the existing
        # convention rather than inventing one) — which is also exactly the
        # control set #2405's Pipeline-local grid settled on.
        ReportParam(
            id="since",
            label="Time range",
            kind="choice",
            choices=SINCE_PRESETS,
            default="24h",
            help="How far back the window reaches from `until`. Presets, or any duration (e.g. 13h).",
            free_form=True,
            validate=_validate_since,
        ),
        ReportParam(
            id="until",
            label="Window end",
            kind="text",
            default="",
            help="Epoch seconds or ISO-8601. Empty means now.",
            validate=_validate_until,
        ),
        ReportParam(
            id="repo",
            label="Repo",
            kind="text",
            default="",
            help="Restrict to one repo by name. Empty means all repos.",
        ),
    ),
    run=run_completed,
    row_identity=RowIdentity(repo_column="repo", issue_column="issue"),
)


DRIVE_QUEUE_STATUS = ReportDef(
    id="drive-queue-status",
    title="Drive Queue Status",
    description=(
        "A live snapshot of the drive queue — one row per queued entry in "
        "run order, with its state, machine pin, attempts/deferrals and the "
        "tick's own last_reason. A snapshot, not a history: `drive_queue` "
        "has no `completed_at`, so this shows what is queued now, not what "
        "the queue has processed."
    ),
    params=(
        ReportParam(
            id="repo",
            label="Repo",
            kind="text",
            default="",
            help="Restrict to one repo by name. Empty means all repos.",
        ),
    ),
    run=run_drive_queue_status,
)


DECISIONS = ReportDef(
    id="decisions",
    title="Decisions",
    description=(
        "Why is the fleet stuck, as option-based cards instead of raw infra "
        "text — one card per root cause, folding `coord escalate list`'s "
        "structured merge-gate escalations and `drive-queue list`'s "
        "blocked/failed rows into a plain-language `why`, 2-4 ready-to-run "
        "options (one recommended), and a downstream count for anything "
        "stuck only because this one is."
    ),
    params=(
        ReportParam(
            id="repo",
            label="Repo",
            kind="text",
            default="",
            help="Restrict to one repo by name. Empty means all repos.",
        ),
    ),
    run=run_decisions,
)


USAGE = ReportDef(
    id="usage",
    title="Usage",
    description=(
        "Cost and token spend for a time window, one row per issue (or per "
        "repo): legs, tokens in/out, captured $, estimated ~$ for legs with "
        "no captured cost, and the total. Estimates use the daemon's own "
        "loaded `pricing:` block, so they agree with `coord usage` by "
        "construction."
    ),
    params=(
        ReportParam(
            id="window",
            label="Time window",
            kind="choice",
            choices=USAGE_WINDOW_CHOICES,
            default="today",
            help=(
                "today/week/month are local calendar periods; 7d/30d are "
                "rolling windows ending now."
            ),
        ),
        ReportParam(
            id="group_by",
            label="Group by",
            kind="choice",
            choices=USAGE_GROUP_BY_CHOICES,
            default="issue",
            help="One row per issue, or one row per repo.",
        ),
        ReportParam(
            id="repo",
            label="Repo",
            kind="text",
            default="",
            help="Restrict to one repo by name. Empty means all repos.",
        ),
    ),
    run=run_usage,
)


QUEUE_OUTCOMES = ReportDef(
    id="queue-outcomes",
    title="Queue Outcomes",
    description=(
        "What fraction of the queue got over the line without a human. Every "
        "entry that reached a terminal state in the window, bucketed as "
        "succeeded / auto_resolved_mechanism / auto_resolved_rescue / human / "
        "open, with the human bucket broken down by cause and split again by "
        "`by_design` (a Gate-A sign-off and a policy refusal are SUPPOSED to "
        "stop for a person). Folded from the drive-queue block log (#2235), "
        "which is per-host — run it where the tick runs, or let that host's "
        "daemon answer. `24h` is one bar per category; `7d`/`4w` are the same "
        "arithmetic in 7 daily / 4 weekly periods, so a client trends it by "
        "grouping rows on `period_start`."
    ),
    params=(
        ReportParam(
            id="window",
            label="Window",
            kind="choice",
            choices=QUEUE_OUTCOMES_WINDOW_CHOICES,
            default="24h",
            help=(
                "Span and bucket size together: 24h is a single period, 7d is "
                "7 daily periods, 4w is 4 weekly ones. Periods are aligned to "
                "`until`, not to the civil calendar."
            ),
        ),
        # Same name, same semantics and the same validator as
        # `issue-activity`'s (#2270: follow the existing convention rather
        # than inventing one). `since` is deliberately absent — `window` sets
        # the span, and two ways to say it would let them disagree.
        ReportParam(
            id="until",
            label="Window end",
            kind="text",
            default="",
            help="Epoch seconds or ISO-8601. Empty means now.",
            validate=_validate_until,
        ),
        ReportParam(
            id="repo",
            label="Repo",
            kind="text",
            default="",
            help="Restrict to one repo by name. Empty means all repos.",
        ),
    ),
    run=run_queue_outcomes,
)


TREND = ReportDef(
    id="trend",
    title="Trend",
    description=(
        "Time-bucketed merge throughput + cost efficiency — one row per "
        "bucket, not per issue: MERGED (issues that finished in that bucket, "
        "the same ENDED definition `completed` uses) and a TRAILING-window "
        "mean cost/legs per merged issue (~5 buckets), never a per-bucket "
        "mean — an empty bucket's cost is undefined, not zero. Answers the "
        "one question none of the other reports can: does throughput rise "
        "while cost per merged issue falls? `1d`/`3d`/`7d`/`1m` trade bucket "
        "width for range, always ~24-30 points so the x-axis stays readable."
    ),
    params=(
        ReportParam(
            id="range",
            label="Range",
            kind="choice",
            choices=TREND_RANGE_CHOICES,
            default="7d",
            help=(
                "1d=hourly, 3d=3-hourly, 7d=6-hourly, 1m=daily buckets — "
                "~24-30 points either way."
            ),
        ),
        # Same name, same semantics and the same validator as
        # `queue-outcomes`'s `until` (#2270's rule: follow the existing
        # convention). `since` is deliberately absent — `range` sets the
        # span, and two ways to say it would let them disagree.
        ReportParam(
            id="until",
            label="Window end",
            kind="text",
            default="",
            help="Epoch seconds or ISO-8601. Empty means now.",
            validate=_validate_until,
        ),
        ReportParam(
            id="repo",
            label="Repo",
            kind="text",
            default="",
            help="Restrict to one repo by name. Empty means all repos.",
        ),
    ),
    run=run_trend,
)


DEPRECATED_ROUTES = ReportDef(
    id="deprecated-routes",
    title="Deprecated RPC Routes",
    description=(
        "Per deprecated RPC route (the #1944 superseded-by-resource table): "
        "last call, distinct calling client+version pairs, and a total "
        "count — evidence for retirement instead of belief (#1945). "
        "`status` distinguishes `in_use` from `zero_calls` (a real, "
        "actionable signal) from `no_data` (telemetry not confirmed live — "
        "never safe to read as `zero_calls`)."
    ),
    params=(),
    run=run_deprecated_routes,
)


# ── CSV serialisation (#1765) ──────────────────────────────────────────────
#
# One serializer, server-side, for every surface: `coord report run --format
# csv`, `GET /report/{id}?format=csv`, and the coord-tui Reports panel's
# Export action (which fetches the route rather than formatting anything
# itself).  Doing it here is not incidental — the values on the wire are
# **raw** (`started_at` is an epoch float, `machines` is a list), and every
# renderer turns those into display strings (`13h ago`, `dellserver,
# precision`).  A client-side CSV would therefore export the *formatting*,
# not the data: an epoch would become a relative string no spreadsheet can
# sort, and the bytes would silently depend on when Export was clicked.
#
# Line terminator is `\n`, not RFC 4180's `\r\n`: this is a Unix tool whose
# output is piped and redirected, `csv.reader` accepts either, and every
# spreadsheet we care about does too.  Fixing it (rather than taking
# `csv.writer`'s platform-ish default) is what makes CLI and daemon bytes
# identical.
_CSV_LINE_TERMINATOR = "\n"


def _csv_scalar(value: Any) -> str:
    """One *raw* value → its CSV text.  Never a display string."""
    if value is None:
        return ""
    if isinstance(value, bool):
        # Before the int check — bool is an int in Python, and `true`/`false`
        # is what every consumer of this file expects to see.
        return "true" if value else "false"
    return str(value)


def format_option_cell(option: Mapping[str, Any]) -> str:
    """One ``{label, command_or_action, what_happens, recommended}`` option
    dict (the ``decisions`` report's ``options`` column shape, #2369) → its
    single-line display text.

    Shared by the CLI human table (:mod:`coord.commands.report`), this
    module's own CSV export, and the coord-tui Reports panel's Rust port of
    this same rule — so a ``kind: list`` column that happens to hold option
    dicts, rather than the far more common list of scalar strings every
    other report uses, renders as ``"<label>: <command>"`` on all three
    surfaces instead of a raw Python/JSON dict blob (#2369 review: every
    existing ``kind: list`` renderer only knew how to flatten a list of
    scalars). Falls back to ``key=value`` pairs for a dict that doesn't
    match the ``{label, command_or_action}`` shape, so an unrelated
    dict-valued list column — should one ever exist — still renders
    *something* readable rather than nothing.
    """
    label = option.get("label")
    command = option.get("command_or_action")
    if label and command:
        mark = "★ " if option.get("recommended") else ""
        return f"{mark}{label}: {command}"
    return ", ".join(f"{k}={v}" for k, v in option.items())


def _csv_cell(value: Any) -> str:
    """One row value → one CSV field.

    Composite values collapse into a single field rather than spilling into
    extra columns: lists (``machines``, ``test_verdicts``) join with ``"; "``,
    and dicts (``drive_exit``) render as ``key=value`` pairs joined the same
    way.  ``drive_exit.reason`` is embedded **verbatim** — commas, quotes and
    newlines and all — because `csv.writer` quotes and escapes it, and a
    round-trip through `csv.reader` has to return the original text (#1631's
    multi-line driver-exit reason is the regression fixture).  JSON-encoding
    the dict would have escaped that newline into a literal ``\\n`` and lost
    the round-trip.

    A list item that is itself a dict (the ``decisions`` report's
    ``options`` column, #2369) renders through :func:`format_option_cell`
    rather than ``_csv_scalar``'s ``str(value)`` fallback, which would emit
    a Python dict repr.
    """
    if isinstance(value, (list, tuple)):
        return "; ".join(
            format_option_cell(v) if isinstance(v, Mapping) else _csv_scalar(v)
            for v in value
        )
    if isinstance(value, Mapping):
        return "; ".join(f"{k}={_csv_scalar(v)}" for k, v in value.items())
    return _csv_scalar(value)


def _csv_comment(text: str) -> list[str]:
    """A note → its ``#``-prefixed line(s).  A note that itself spans lines
    gets one ``#`` per physical line, so no fragment can escape into the
    data and be parsed as a row."""
    lines = str(text).splitlines() or [""]
    return [f"# {line}" if line else "#" for line in lines]


def result_to_csv(result: "ReportResult | Mapping[str, Any]") -> str:
    """Serialise a :class:`ReportResult` (or its ``to_dict()`` form) as CSV.

    Shape:

    * leading ``#``-prefixed comment lines — the report id, the window, and
      **every** ``notes`` entry.  Notes are the derived anomalies and are the
      most valuable part of ``issue-activity``; they are not rows, and they
      must never silently vanish, so they ride along as comments that keep
      the file self-describing and still let it parse once ``#`` lines are
      skipped.
    * a header row, labelled from ``column_meta[].label`` (#1760) when
      present and from the raw column key otherwise.
    * one row per ``rows`` entry, raw values only.
    * ``totals`` (#1763), when the report has one, as a final row — flagged
      in the comments so nobody mistakes it for another data row.  Reports
      without a meaningful sum emit no such row and are unaffected.
    """
    data = result.to_dict() if isinstance(result, ReportResult) else dict(result)

    columns = [str(c) for c in (data.get("columns") or [])]
    labels = {
        str(m.get("id")): str(m.get("label") or m.get("id"))
        for m in (data.get("column_meta") or [])
        if isinstance(m, Mapping)
    }
    window = data.get("window") or [None, None]

    comments: list[str] = [
        f"# report: {data.get('report_id')}",
        f"# window: {_iso(window[0])} to {_iso(window[1])}",
        f"# generated: {_iso(data.get('generated_at'))}",
    ]
    rows = list(data.get("rows") or [])
    comments.append(f"# rows: {len(rows)}")
    # The export is the report's own canonical row order — never a client's
    # transient sort (#1762), which is view state over one result set.
    comments.append("# order: the report's canonical row order")
    totals = data.get("totals")
    if isinstance(totals, Mapping):
        comments.append(
            "# totals: the final row is the grand total, not a data row "
            "(identity columns are blank)"
        )
    for note in data.get("notes") or []:
        comments.extend(_csv_comment(note))

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator=_CSV_LINE_TERMINATOR)
    writer.writerow([labels.get(c, c) for c in columns])
    for row in rows:
        row = row if isinstance(row, Mapping) else {}
        writer.writerow([_csv_cell(row.get(c)) for c in columns])
    if isinstance(totals, Mapping):
        writer.writerow([_csv_cell(totals.get(c)) for c in columns])

    header = "".join(line + _CSV_LINE_TERMINATOR for line in comments)
    return header + buf.getvalue()


def csv_filename(result: "ReportResult | Mapping[str, Any]") -> str:
    """``issue-activity-20260804-1130.csv`` — the suggested download name.

    Derived from the *result* (its window end), not from the wall clock, so
    the daemon's ``Content-Disposition`` and the panel's save-dialog
    suggestion agree for the same run.
    """
    data = result.to_dict() if isinstance(result, ReportResult) else dict(result)
    window = data.get("window") or [None, None]
    stamp_at = window[1] if window[1] is not None else data.get("generated_at")
    try:
        stamp = datetime.fromtimestamp(float(stamp_at), tz=timezone.utc).strftime(
            "%Y%m%d-%H%M"
        )
    except (TypeError, ValueError):
        stamp = "unknown"
    report_id = re.sub(r"[^A-Za-z0-9._-]+", "-", str(data.get("report_id") or "report"))
    return f"{report_id}-{stamp}.csv"


REPORTS: dict[str, ReportDef] = {
    ISSUE_ACTIVITY.id: ISSUE_ACTIVITY,
    COMPLETED.id: COMPLETED,
    DRIVE_QUEUE_STATUS.id: DRIVE_QUEUE_STATUS,
    DECISIONS.id: DECISIONS,
    USAGE.id: USAGE,
    QUEUE_OUTCOMES.id: QUEUE_OUTCOMES,
    TREND.id: TREND,
    DEPRECATED_ROUTES.id: DEPRECATED_ROUTES,
}


def catalogue() -> dict[str, Any]:
    """The wire shape of ``GET /report`` — everything #1741 needs to build a
    report picker and its parameter form without hardcoding anything."""
    return {"reports": [REPORTS[rid].to_dict() for rid in sorted(REPORTS)]}


def run_report(
    report_id: str,
    params: Mapping[str, Any] | None = None,
    **injected: Any,
) -> ReportResult:
    """Look up, validate, run.  Raises :class:`UnknownReportError` /
    :class:`ReportError` — never a traceback for a bad request."""
    report = REPORTS.get(report_id)
    if report is None:
        raise UnknownReportError(
            f"unknown report {report_id!r} — known reports: "
            f"{', '.join(sorted(REPORTS))}"
        )
    resolved = resolve_params(report, params)
    return report.run(**resolved, **injected)
