"""Triage-first waste report (#4099): legs, cost, and failure->green time
per merged issue, computed purely from board + audit history.

"The pipeline wastes legs off the happy path" was an anecdote before this
module — this is the number. Like :mod:`coord.scorecard` and
:mod:`coord.usage_rollup`, it is a **pure** aggregator: plain board
assignment rows (the daemon ``/board`` wire shape, same rows
``coord.usage.fetch_usage_rows`` returns) and, optionally, plain audit-log
entries (``coord.state.list_audit_log``'s shape) go in; a
:class:`WasteReport` comes out. No I/O, no daemon calls — the *fetch* side
lives in :mod:`coord.commands.stats`.

## Leg taxonomy

Every assignment row resolves to exactly one of :data:`LEG_KINDS`:

- ``"work"`` — an issue's first root dispatch (``coord.scorecard.is_root_work``
  — the single existing answer to "is this the root work leg", reused here
  rather than re-deriving a second one, #2096).
- ``"retry"`` — a SECOND (or later) root-shaped dispatch for the same issue
  (``coord retry`` redispatching a fresh, unlinked root after a failure —
  the same shape :attr:`coord.scorecard.IssueScorecard.multiple_roots`
  flags per-issue, resolved here per-row instead).
- ``"fix"`` — a follow-up row in the work/fix chain (``review_of_assignment_id``
  set) — covers both a human ``--fix-of``/``--rework-of`` dispatch and
  ``auto_loop``'s headless review-bounce fix, since neither changes the
  board's waste picture.
- ``"test"`` — a Test-stage child (``type="smoke"``).
- ``"review"`` — a review assignment (``type="review"``).
- ``"mechanic"`` — an automated, non-agentic conflict repair
  (``type="conflict-fix"``, or a fix-chain row the audit log tags
  ``conflict_fix_dispatched``/``conflict_fix_redispatched``).
- ``"investigator"`` — a board-doctor investigate-tier leg
  (audit ``fixit_leg_dispatched``).
- ``"finisher"`` — a board-doctor finisher-tier leg
  (audit ``finisher_leg_dispatched``).
- ``"other"`` — everything else (plan, chat, audit, uat, acceptance, ...) —
  reported for transparency, never silently dropped.

``investigator``/``finisher``/the fix-chain half of ``mechanic`` all
dispatch as a plain ``type="work"`` follow-up row (see
``coord.auto_loop._dispatch_fix``'s docstring) — the ONLY durable signal
that distinguishes them from an ordinary fix is the audit-log event
recorded at dispatch time (``coord.board_doctor_permissions``). Call
:func:`build_waste_report` with *audit_entries* to get that split; without
it, every one of those three kinds collapses into the plain ``"fix"``
bucket — never silently miscounted as something else, just less specific.

## Avoidable legs

Three shapes, all best-effort over whatever data is available:

1. **Compile-error retries** — a ``"retry"``-kind row whose immediately
   preceding row (any kind, same issue) failed and that failure's own
   persisted reason text (``test_reason``/``smoke_test_reason``/
   ``failure_reason``) signals a compile error
   (``coord.failure_evidence.extract_failure_evidence``). Re-running the
   identical broken commit never fixes a compile error on its own.
2. **Zero-commit fix rounds** — a ``"fix"``-kind row that landed
   ``status="advisory"`` — the board's own zero-commit-outcome marker
   (``coord.worker_events.classify_zero_commit_outcome`` / the ``advisory``
   status value), i.e. the worker looked and changed nothing.
3. **Test reruns on an already-broken SHA** — a ``"test"``-kind row whose
   TARGET (``review_of_assignment_id`` — the work/fix round it is testing,
   i.e. the exact same branch/SHA) has a prior audit test-verdict event
   (``test_failed``, #1340's single write choke point
   ``coord.state._record_test_verdict_local``) with a captured
   ``test_reason`` that signals a compile error, and no intervening
   ``test_passed``/``test_skipped`` for that SAME target before this row's
   own dispatch — i.e. this exact round's SHA was already known broken when
   this Test leg ran. Scoped to the SAME target (never just "the issue has
   failed at some point") so an ordinary fail -> real fix -> retest cycle
   (a NEW round, new target) never false-positives. This one NEEDS
   *audit_entries* (the board row's own ``test_reason`` gets cleared before
   each new Test round dispatches — see ``coord.state.reset_work_test_state``
   — so only the audit trail still carries the superseded verdict); it is
   always 0 without them.

## Failure -> green time

Per issue: the elapsed wall time from the FIRST Test or CI failure
(audit ``test_failed``, or ``ci_fix_dispatched`` — CI found a real break and
a fix leg was dispatched for it) to the next passing verdict
(``test_passed``/``test_skipped``, or the issue's own ``merged`` event).
Needs *audit_entries*; ``None`` with no audit data, also ``None`` (distinct
from 0) when a failure happened but never resolved before the window's end.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from coord.config import PricingConfig
from coord.failure_evidence import extract_failure_evidence
from coord.scorecard import is_root_work
from coord.usage_rollup import (
    TimeWindow,
    leg_cost,
    leg_duration,
    leg_in_window,
    parse_timestamp,
    row_issue_number,
)

__all__ = [
    "LEG_KINDS",
    "AVOIDABLE_KINDS",
    "LegKindCounts",
    "AvoidableLegs",
    "IssueWaste",
    "WasteReport",
    "classify_leg_kind",
    "audit_leg_kind_overrides",
    "build_waste_report",
    "waste_report_to_dict",
]

LEG_KINDS = (
    "work",
    "fix",
    "test",
    "retry",
    "review",
    "mechanic",
    "investigator",
    "finisher",
    "other",
)

AVOIDABLE_KINDS = (
    "compile_error_retries",
    "zero_commit_fix_rounds",
    "test_reruns_on_known_broken_sha",
)

# Internal placeholder for a root-shaped row (type="work"/"test-author"/
# "mock-author" with no review_of_assignment_id, review_iteration 0) before
# per-issue ordering resolves it to "work" (first) or "retry" (any later
# one) — see _resolve_root_kinds.
_ROOT_PLACEHOLDER = "_root"

_TEST_VERDICT_EVENTS = ("test_failed", "test_passed", "test_skipped")


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _row_failure_text(row: Mapping[str, Any]) -> str:
    parts = [row.get("test_reason"), row.get("smoke_test_reason"), row.get("failure_reason")]
    return "\n".join(str(p) for p in parts if p)


def _is_compile_error_text(text: str) -> bool:
    if not text.strip():
        return False
    return extract_failure_evidence(text).signals.compile_error


def audit_leg_kind_overrides(audit_entries: Iterable[Mapping[str, Any]] | None) -> dict[str, str]:
    """Map ``assignment_id`` -> ``"investigator"``/``"finisher"``/``"mechanic"``
    from the audit events ``coord.board_doctor_permissions`` records at the
    moment it dispatches one of those tiers (each names the NEW leg's own
    ``assignment_id`` directly, so no issue/timestamp correlation is needed).
    Empty dict for ``None``/no matching entries — callers degrade to the
    plain ``"fix"`` bucket for everything, never raise.
    """
    overrides: dict[str, str] = {}
    for entry in audit_entries or ():
        aid = entry.get("assignment_id")
        if not aid:
            continue
        event_type = entry.get("event_type")
        if event_type == "fixit_leg_dispatched":
            overrides[str(aid)] = "investigator"
        elif event_type == "finisher_leg_dispatched":
            overrides[str(aid)] = "finisher"
        elif event_type in ("conflict_fix_dispatched", "conflict_fix_redispatched"):
            overrides[str(aid)] = "mechanic"
    return overrides


def classify_leg_kind(row: Mapping[str, Any], overrides: Mapping[str, str] | None = None) -> str:
    """Classify one assignment row — see the module docstring's taxonomy.

    Returns :data:`_ROOT_PLACEHOLDER` for a root-shaped row; resolve that to
    ``"work"``/``"retry"`` at the per-issue group level via
    :func:`_resolve_root_kinds` before reporting — a single row can't know
    whether it's the first root for its issue in isolation.
    """
    overrides = overrides or {}
    rtype = str(row.get("type") or "work")
    if rtype == "conflict-fix":
        return "mechanic"
    if rtype == "smoke":
        return "test"
    if rtype == "review":
        return "review"
    if is_root_work(row):
        return _ROOT_PLACEHOLDER
    if row.get("review_of_assignment_id"):
        aid = row.get("assignment_id")
        if aid and str(aid) in overrides:
            return overrides[str(aid)]
        return "fix"
    return "other"


def _resolve_root_kinds(rows: list[dict], kinds: list[str]) -> None:
    """Mutate *kinds* in place: the chronologically-first ``_ROOT_PLACEHOLDER``
    row becomes ``"work"``, every later one becomes ``"retry"``."""
    root_indices = [i for i, k in enumerate(kinds) if k == _ROOT_PLACEHOLDER]
    root_indices.sort(key=lambda i: parse_timestamp(rows[i].get("dispatched_at")) or 0.0)
    for pos, i in enumerate(root_indices):
        kinds[i] = "work" if pos == 0 else "retry"


@dataclass
class LegKindCounts:
    """How many legs of each kind — see :data:`LEG_KINDS`."""

    work: int = 0
    fix: int = 0
    test: int = 0
    retry: int = 0
    review: int = 0
    mechanic: int = 0
    investigator: int = 0
    finisher: int = 0
    other: int = 0

    def add(self, kind: str) -> None:
        setattr(self, kind, getattr(self, kind) + 1)

    @property
    def total(self) -> int:
        return sum(getattr(self, k) for k in LEG_KINDS)

    def to_dict(self) -> dict[str, int]:
        return {k: getattr(self, k) for k in LEG_KINDS}


@dataclass
class AvoidableLegs:
    """Counts for the three avoidable-leg shapes — see the module docstring."""

    compile_error_retries: int = 0
    zero_commit_fix_rounds: int = 0
    test_reruns_on_known_broken_sha: int = 0

    @property
    def total(self) -> int:
        return sum(getattr(self, k) for k in AVOIDABLE_KINDS)

    def to_dict(self) -> dict[str, int]:
        return {k: getattr(self, k) for k in AVOIDABLE_KINDS}


@dataclass
class IssueWaste:
    """Everything this module computes for one merged issue.

    ``unknown_model_legs`` is how many of this issue's legs carried tokens
    but priced to $0 because *pricing* has no rate for that leg's model
    (:func:`coord.usage_rollup.leg_cost`'s ``unknown_model`` return) — a
    cost no captured/estimated figure here ever represents, so a caller
    that only read ``cost_total`` would otherwise read "cheap" where the
    true answer is "unknown".
    """

    repo_name: str
    issue_number: int
    legs: LegKindCounts
    avoidable: AvoidableLegs
    cost_captured: float
    cost_est: float
    duration_secs: float
    cost_by_stage: dict[str, dict]
    failure_to_green_secs: float | None
    had_unresolved_failure: bool
    has_audit_data: bool
    unknown_model_legs: int = 0

    @property
    def cost_total(self) -> float:
        return self.cost_captured + self.cost_est

    def to_dict(self) -> dict:
        return {
            "repo_name": self.repo_name,
            "issue_number": self.issue_number,
            "legs": self.legs.to_dict(),
            "legs_total": self.legs.total,
            "avoidable": self.avoidable.to_dict(),
            "avoidable_total": self.avoidable.total,
            "cost_captured": self.cost_captured,
            "cost_est": self.cost_est,
            "cost_total": self.cost_total,
            "duration_secs": self.duration_secs,
            "cost_by_stage": self.cost_by_stage,
            "failure_to_green_secs": self.failure_to_green_secs,
            "had_unresolved_failure": self.had_unresolved_failure,
            "has_audit_data": self.has_audit_data,
            "unknown_model_legs": self.unknown_model_legs,
        }


@dataclass
class WasteReport:
    """The full report: one row per merged issue in-window, plus rollups."""

    window: TimeWindow
    repo_filter: str | None
    issues: list[IssueWaste] = field(default_factory=list)
    totals: dict = field(default_factory=dict)
    by_repo: dict[str, dict] = field(default_factory=dict)


def _stage_cost_rollup(rows: Iterable[Mapping[str, Any]], pricing: PricingConfig) -> dict[str, dict]:
    by_stage: dict[str, dict] = {}
    for row in rows:
        stage = str(row.get("type") or "work")
        g = by_stage.setdefault(
            stage,
            {"legs": 0, "cost_captured": 0.0, "cost_est": 0.0, "duration_secs": 0.0},
        )
        captured, est, _unknown_model = leg_cost(row, pricing)
        duration, _is_open = leg_duration(row)
        g["legs"] += 1
        g["cost_captured"] += captured
        g["cost_est"] += est
        g["duration_secs"] += duration
    for g in by_stage.values():
        g["cost_total"] = g["cost_captured"] + g["cost_est"]
    return by_stage


def _merge_stage_rollups(dst: dict[str, dict], src: Mapping[str, dict]) -> None:
    for stage, g in src.items():
        acc = dst.setdefault(
            stage,
            {"legs": 0, "cost_captured": 0.0, "cost_est": 0.0, "duration_secs": 0.0, "cost_total": 0.0},
        )
        for key in ("legs", "cost_captured", "cost_est", "duration_secs", "cost_total"):
            acc[key] += g.get(key, 0)


def _test_verdict_events_by_target(
    audit_entries: Iterable[Mapping[str, Any]],
) -> dict[str, list[tuple[float, str, bool]]]:
    """``target_assignment_id -> [(ts, event_type, compile_error)]`` for every
    test-verdict audit event, sorted ascending per target. ``target`` is the
    event's own ``assignment_id`` — the work/fix round the verdict was
    recorded ON (``coord.state.record_test_verdict``'s ``assignment_id``
    param), i.e. the exact round/SHA the verdict describes.
    ``compile_error`` reflects that event's OWN ``details.test_reason`` —
    the historical reason text, captured at write time, which a current
    board-row read can no longer see once a later round clears it (see
    ``coord.state.reset_work_test_state``)."""
    by_target: dict[str, list[tuple[float, str, bool]]] = {}
    for entry in audit_entries:
        if entry.get("category") != "test":
            continue
        event_type = entry.get("event_type")
        if event_type not in _TEST_VERDICT_EVENTS:
            continue
        ts = parse_timestamp(entry.get("ts"))
        if ts is None:
            continue
        target = entry.get("assignment_id")
        if not target:
            continue
        details = entry.get("details") or {}
        reason = str(details.get("test_reason") or "")
        compile_error = event_type == "test_failed" and _is_compile_error_text(reason)
        by_target.setdefault(str(target), []).append((ts, event_type, compile_error))
    for events in by_target.values():
        events.sort(key=lambda e: e[0])
    return by_target


def _failure_to_green(audit_entries_for_issue: list[Mapping[str, Any]]) -> tuple[float | None, bool, bool]:
    """Returns ``(seconds, had_unresolved_failure, has_data)``.

    ``seconds`` is ``None`` when there was no failure, or a failure never
    resolved before the data ran out (``had_unresolved_failure=True`` in
    that second case, distinguishing "nothing broke" from "broke and we
    don't know when/if it got fixed"). ``has_data`` is ``False`` only when
    there is no relevant audit event for this issue at all.
    """
    events: list[tuple[float, str]] = []
    for entry in audit_entries_for_issue:
        ts = parse_timestamp(entry.get("ts"))
        if ts is None:
            continue
        category = entry.get("category")
        event_type = entry.get("event_type")
        if category == "test" and event_type in _TEST_VERDICT_EVENTS:
            events.append((ts, event_type))
        elif category == "merge" and event_type in ("merged", "ci_fix_dispatched"):
            events.append((ts, event_type))
    events.sort(key=lambda e: e[0])
    if not events:
        return None, False, False

    first_failure_ts: float | None = None
    for ts, event_type in events:
        if event_type in ("test_failed", "ci_fix_dispatched"):
            first_failure_ts = ts
            break
    if first_failure_ts is None:
        return None, False, True

    for ts, event_type in events:
        if ts <= first_failure_ts:
            continue
        if event_type in ("test_passed", "test_skipped", "merged"):
            return ts - first_failure_ts, False, True
    return None, True, True


def _score_one_issue(
    *,
    repo_name: str,
    issue_number: int,
    rows: list[dict],
    audit_entries_for_issue: list[Mapping[str, Any]],
    overrides: Mapping[str, str],
    pricing: PricingConfig,
) -> IssueWaste:
    rows_sorted = sorted(rows, key=lambda r: parse_timestamp(r.get("dispatched_at")) or 0.0)
    kinds = [classify_leg_kind(r, overrides) for r in rows_sorted]
    _resolve_root_kinds(rows_sorted, kinds)

    counts = LegKindCounts()
    for kind in kinds:
        counts.add(kind)

    avoidable = AvoidableLegs()

    # 1. Compile-error retries: a "retry" row whose immediately preceding
    # row (any kind) failed with a compile-error signal.
    for i, kind in enumerate(kinds):
        if kind != "retry" or i == 0:
            continue
        prev = rows_sorted[i - 1]
        if _is_compile_error_text(_row_failure_text(prev)):
            avoidable.compile_error_retries += 1

    # 2. Zero-commit fix rounds: a "fix" row that landed status="advisory".
    for row, kind in zip(rows_sorted, kinds):
        if kind == "fix" and str(row.get("status") or "") == "advisory":
            avoidable.zero_commit_fix_rounds += 1

    # 3. Test reruns on an already-known-broken SHA: a "test" row whose
    # TARGET (the round/SHA it tests) has a prior test_failed compile-error
    # verdict with nothing passing in between — see module docstring.
    verdict_events_by_target = _test_verdict_events_by_target(audit_entries_for_issue)
    for row, kind in zip(rows_sorted, kinds):
        if kind != "test":
            continue
        dispatched = parse_timestamp(row.get("dispatched_at"))
        target = row.get("review_of_assignment_id")
        if dispatched is None or not target:
            continue
        most_recent: tuple[float, str, bool] | None = None
        for ts, event_type, compile_error in verdict_events_by_target.get(str(target), ()):
            if ts >= dispatched:
                break
            most_recent = (ts, event_type, compile_error)
        if most_recent is not None and most_recent[1] == "test_failed" and most_recent[2]:
            avoidable.test_reruns_on_known_broken_sha += 1

    cost_captured = 0.0
    cost_est = 0.0
    duration_secs = 0.0
    unknown_model_legs = 0
    for row in rows_sorted:
        captured, est, unknown_model = leg_cost(row, pricing)
        duration, _is_open = leg_duration(row)
        cost_captured += captured
        cost_est += est
        duration_secs += duration
        if unknown_model:
            unknown_model_legs += 1
    cost_by_stage = _stage_cost_rollup(rows_sorted, pricing)

    failure_to_green_secs, had_unresolved_failure, has_audit_data = _failure_to_green(
        audit_entries_for_issue
    )

    return IssueWaste(
        repo_name=repo_name,
        issue_number=issue_number,
        legs=counts,
        avoidable=avoidable,
        cost_captured=cost_captured,
        cost_est=cost_est,
        duration_secs=duration_secs,
        cost_by_stage=cost_by_stage,
        failure_to_green_secs=failure_to_green_secs,
        had_unresolved_failure=had_unresolved_failure,
        has_audit_data=has_audit_data,
        unknown_model_legs=unknown_model_legs,
    )


def _issue_is_merged(rows: list[dict], audited_merged: bool) -> bool:
    if audited_merged:
        return True
    return any(str(r.get("status") or "") == "merged" for r in rows)


def _empty_totals() -> dict:
    return {
        "issue_count": 0,
        "legs": LegKindCounts().to_dict(),
        "legs_total": 0,
        "avoidable": AvoidableLegs().to_dict(),
        "avoidable_total": 0,
        "cost_captured": 0.0,
        "cost_est": 0.0,
        "cost_total": 0.0,
        "duration_secs": 0.0,
        "cost_by_stage": {},
        "unknown_model_legs": 0,
        "failure_to_green": {
            "issues_with_data": 0,
            "issues_resolved": 0,
            "issues_unresolved": 0,
            "avg_secs": None,
            "max_secs": None,
        },
    }


def _fold_issue_into_totals(totals: dict, issue: IssueWaste, resolved_secs: list[float]) -> None:
    totals["issue_count"] += 1
    for kind in LEG_KINDS:
        totals["legs"][kind] += getattr(issue.legs, kind)
    totals["legs_total"] += issue.legs.total
    for kind in AVOIDABLE_KINDS:
        totals["avoidable"][kind] += getattr(issue.avoidable, kind)
    totals["avoidable_total"] += issue.avoidable.total
    totals["cost_captured"] += issue.cost_captured
    totals["cost_est"] += issue.cost_est
    totals["cost_total"] += issue.cost_total
    totals["duration_secs"] += issue.duration_secs
    totals["unknown_model_legs"] += issue.unknown_model_legs
    _merge_stage_rollups(totals["cost_by_stage"], issue.cost_by_stage)

    ftg = totals["failure_to_green"]
    if issue.has_audit_data:
        ftg["issues_with_data"] += 1
    if issue.failure_to_green_secs is not None:
        ftg["issues_resolved"] += 1
        resolved_secs.append(issue.failure_to_green_secs)
    elif issue.had_unresolved_failure:
        ftg["issues_unresolved"] += 1


def build_waste_report(
    assignment_rows: Iterable[dict],
    *,
    window: TimeWindow,
    repo: str | None = None,
    audit_entries: Iterable[Mapping[str, Any]] | None = None,
    pricing: PricingConfig | None = None,
) -> WasteReport:
    """Build the full waste report.

    *assignment_rows* — board rows, any repo (filtered to *repo* here if
    given, matching :mod:`coord.scorecard`'s own fetch-then-filter
    convention). *audit_entries* is optional and best-effort (#4099: legs
    classification, cost, and leg counts all work without it; only the
    investigator/finisher/mechanic split, failure->green time, and the
    test-rerun avoidable count need it — see the module docstring). *window*
    bounds every row/event by ``dispatched_at``/``finished_at`` (rows) or
    ``ts`` (audit entries); pass ``TimeWindow()`` (unbounded) for "all time".
    """
    if pricing is None:
        pricing = PricingConfig()
    audit_entries = list(audit_entries or ())
    overrides = audit_leg_kind_overrides(audit_entries)

    rows = [
        r
        for r in assignment_rows
        if (repo is None or str(r.get("repo_name") or "") == repo) and leg_in_window(r, window)
    ]

    def _audit_in_window(entry: Mapping[str, Any]) -> bool:
        ts = parse_timestamp(entry.get("ts"))
        return window.contains(ts)

    audit_entries_in_window = [e for e in audit_entries if _audit_in_window(e)]

    audited_merged: set[tuple[str, int]] = set()
    for entry in audit_entries:
        if entry.get("event_type") != "merged":
            continue
        entry_repo = str(entry.get("repo") or "")
        entry_issue = entry.get("issue")
        if entry_repo and entry_issue is not None:
            audited_merged.add((entry_repo, _to_int(entry_issue)))

    by_issue: dict[tuple[str, int], list[dict]] = {}
    for row in rows:
        key = (str(row.get("repo_name") or ""), row_issue_number(row))
        by_issue.setdefault(key, []).append(row)

    audit_by_issue: dict[tuple[str, int], list[Mapping[str, Any]]] = {}
    for entry in audit_entries_in_window:
        entry_repo = str(entry.get("repo") or "")
        entry_issue = entry.get("issue")
        if not entry_repo or entry_issue is None:
            continue
        audit_by_issue.setdefault((entry_repo, _to_int(entry_issue)), []).append(entry)

    issues: list[IssueWaste] = []
    for (issue_repo, issue_number), issue_rows in by_issue.items():
        if not _issue_is_merged(issue_rows, (issue_repo, issue_number) in audited_merged):
            continue
        issues.append(
            _score_one_issue(
                repo_name=issue_repo,
                issue_number=issue_number,
                rows=issue_rows,
                audit_entries_for_issue=audit_by_issue.get((issue_repo, issue_number), []),
                overrides=overrides,
                pricing=pricing,
            )
        )

    issues.sort(key=lambda c: (c.repo_name, c.issue_number))

    totals = _empty_totals()
    by_repo: dict[str, dict] = {}
    resolved_secs: list[float] = []
    resolved_secs_by_repo: dict[str, list[float]] = {}
    for issue in issues:
        _fold_issue_into_totals(totals, issue, resolved_secs)
        repo_totals = by_repo.setdefault(issue.repo_name, _empty_totals())
        _fold_issue_into_totals(repo_totals, issue, resolved_secs_by_repo.setdefault(issue.repo_name, []))

    if resolved_secs:
        totals["failure_to_green"]["avg_secs"] = sum(resolved_secs) / len(resolved_secs)
        totals["failure_to_green"]["max_secs"] = max(resolved_secs)
    for repo_name, secs in resolved_secs_by_repo.items():
        ftg = by_repo[repo_name]["failure_to_green"]
        if secs:
            ftg["avg_secs"] = sum(secs) / len(secs)
            ftg["max_secs"] = max(secs)

    return WasteReport(window=window, repo_filter=repo, issues=issues, totals=totals, by_repo=by_repo)


def waste_report_to_dict(report: WasteReport) -> dict:
    """Plain-dict rendering for JSON output."""
    return {
        "window": {"start": report.window.start, "end": report.window.end, "label": report.window.label},
        "repo_filter": report.repo_filter,
        "issues": [i.to_dict() for i in report.issues],
        "totals": report.totals,
        "by_repo": report.by_repo,
    }
