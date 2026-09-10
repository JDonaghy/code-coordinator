"""Pure aggregation for ``coord plans`` — the data backbone for the TUI
"Plans" panel (#975).

Every public function here is **pure / board-driven**: all inputs are plain
Python values (milestone dicts, issue bodies, a :class:`~coord.models.Board`,
sets of open/terminal issue numbers). No GitHub calls, no subprocess, no I/O.
That keeps the stats cheap to unit-test with seeded fixtures.

The CLI command (``coord/commands/plans.py``) fetches the raw inputs from
GitHub and calls :func:`aggregate_plan` for each milestone in each configured
repo.

Design decisions (#974):
- **tracking issue = "epic"-labelled issue under the milestone**.  This is the
  same convention established in :mod:`coord.milestone_order`
  (``TRACKING_ISSUE_LABEL = "epic"``).  An open milestone with no such issue is
  reported with ``has_work_order = False`` and a ``"no_work_order"``
  ``needs_you`` signal.
- **Tracking-epic lookup considers closed epics too.**  A milestone can stay
  open on GitHub after its tracking epic has been closed (e.g. all
  work-order nodes finished and someone tidied up the epic before
  remembering to close the milestone).  Reporting that as ``"no_work_order"``
  would be backwards — it would read as "nobody wrote a work order yet" when
  the real state is "this plan is done."  So :func:`aggregate_repo_plans`
  accepts an optional ``closed_tracking_issues`` list (closed, ``"epic"``-
  labelled issues, e.g. from :func:`coord.github_ops.get_closed_epics`) and
  merges it with ``open_issues`` before searching for the tracking issue —
  mirroring the open+closed scan :func:`coord.serve_app` already does for the
  same "find the epic" operation (the #795 Phase 3b per-milestone work-order
  projection).
- **Work order scope only**.  ``done``/``total`` counts count work-order
  nodes, not all issues under the milestone (the work order is the declared
  scope of automated dispatch).
- **No remote branch check during aggregation**.  The ``branch_lookup``
  parameter to :func:`~coord.milestone_order.ready_frontier` defaults to
  ``lambda *_: []`` here — a background aggregate over many milestones should
  not spawn N ``gh`` calls for branch checks.  ``in_flight`` is derived purely
  from the board's active assignments.
- **Unlabelled-epic lint (#3227)**.  :data:`EPIC_TITLE_RE` /
  :func:`find_unlabelled_epics` are a *separate* read-only lint, not part of
  the milestone-aggregation pipeline above: an issue whose title reads as an
  epic (``"Epic:"``, ``"EPIC:"``, a ``"[tag] Epic:"``/``"[epic]"`` bracket
  prefix) but whose cached ``labels`` don't carry ``TRACKING_ISSUE_LABEL`` is
  invisible to :func:`find_tracking_issue` (and, since #3132, to
  ``coord.drive_queue.dispatch_type_for_labels``) — it silently dispatches as
  plain ``type="work"`` instead of the epic-aware type. This lint only
  *flags* the mismatch; it never writes a label (out of scope per #3226).
  Pure, like the rest of this module: it takes already-cached issue dicts (the
  local ``issues`` table shape — ``labels`` as a plain ``list[str]``, not
  GitHub's ``[{"name": ...}]``) and does no I/O of its own. The CLI command
  (``coord plans --lint-epics``) is what reads the local cache.
- **Stale-tracker lint (#3228)**.  :func:`find_stale_epics` is the sibling of
  :func:`find_unlabelled_epics` above — same locally-cached ``issues`` shape,
  same read-only "flag, don't fix" posture (out of scope per #3226) — but it
  looks the other direction: instead of an epic missing its label, it flags an
  open, correctly ``"epic"``-labelled epic whose declared scope has already
  shipped (every child closed) or was never registered (zero children). It
  resolves children via :class:`coord.parentage.MarkdownParentage` against the
  epic's own cached ``body`` (no GitHub round trip) and cross-references each
  child's REAL state in the same cached ``issues`` rows rather than trusting
  the ``## Sub-issues``/``## Work order`` checklist's own ``[x]``/``[ ]`` box,
  which #1061 stopped keeping in sync with reality. The CLI command
  (``coord plans --lint-stale-epics``) is what reads the local cache.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from coord.issue_store import diff_audit_goals
from coord.milestone_order import (
    TRACKING_ISSUE_LABEL,
    WorkOrder,
    parse_work_order,
    ready_frontier,
)
from coord.models import Board
from coord.parentage import MarkdownParentage


__all__ = [
    "TRACKING_ISSUE_LABEL",
    "EPIC_TITLE_RE",
    "PlanEntry",
    "find_tracking_issue",
    "find_unlabelled_epics",
    "find_stale_epics",
    "aggregate_plan",
    "aggregate_repo_plans",
]


# ── Unlabelled-epic lint (#3227) ────────────────────────────────────────────

# Base pattern on the five real-world titles the issue cited:
#   "[platform] Epic: ..."                 (bracket tag + "Epic:")
#   "Epic: goal-driven autonomous planner"  (bare "Epic:")
#   "EPIC: coordinator<->vimcode integration" (bare, upper-case)
#   "Epic: pluggable CI/CD store"           (bare)
#   "Epic: pluggable issue store"           (bare)
# plus the "[epic]" bracket-tag-alone style the issue also calls out.
EPIC_TITLE_RE = re.compile(
    r"^(?:\[[^\]]*\]\s*)?epic\s*:|^\[epic\]",
    re.IGNORECASE,
)


# ── Data model ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PlanEntry:
    """Aggregated stats for one GitHub milestone in one repo.

    All counts are scoped to the ``## Work order`` block of the milestone's
    tracking epic.  When there is no work order the counts are all 0 and
    ``needs_you`` contains ``"no_work_order"``.

    Attributes:
        repo:              coord-local repo name (``coordinator.yml`` key).
        title:             Milestone title on GitHub.
        milestone_number:  GitHub milestone number.
        tracking_issue:    GitHub issue number of the epic/tracking issue, or
                           ``None`` when none exists under this milestone.
                           May be a closed issue when the caller supplies
                           ``closed_tracking_issues`` to
                           :func:`aggregate_repo_plans`.
        has_work_order:    ``True`` iff the tracking body carries a parsed
                           ``## Work order`` block with ≥1 node.
        ready_frontier:    Number of work-order nodes ready to dispatch now
                           (dependencies met, unclaimed, not terminal).
        blocked:           Number of nodes blocked by unmet dependencies or a
                           conflict (i.e. *not* claimed by an active assignment).
        in_flight:         Number of nodes that are claimed by an active board
                           assignment or a remote ``issue-N-*`` branch.
        done:              Number of terminal (closed) work-order nodes.
        total:             Total number of nodes in the work order.
        needs_you:         Ordered list of attention signals.  Possible values:

                           ``"no_work_order"``
                               Open milestone with no parseable work order.
                           ``"ready_waiting"``
                               ≥1 ready-frontier entry exists to dispatch.
                           ``"stalled"``
                               Open, has a work order, but nothing is ready *or*
                               in-flight and the milestone is not done.
                           ``"chat_pending"``
                               A ``type="milestone-chat"`` assignment is
                               currently active (running/pending) against the
                               tracking issue (#976) — an operator opened
                               ``coord milestone chat`` and hasn't finalized
                               it yet.  Orthogonal to the other three signals:
                               it can appear alongside any of them (or alone
                               on an otherwise "done" milestone).

        outcome_run_number, outcome_met, outcome_partial, outcome_gap,
        outcome_bottom_line, outcome_diff_summary:
                           #886 Phase 2 — the latest Milestone Outcome Audit
                           (``--audit-of``) verdict for this milestone's epic,
                           independent of the issue-closed counts above. All
                           ``None`` when no audit has ever run against this
                           epic. ``outcome_diff_summary`` is a pre-rendered
                           one-line delta vs the previous run (e.g. "closed:
                           tests.rs split; still open: #550"), ``None`` on the
                           first run (nothing to diff against yet).
    """

    repo: str
    title: str
    milestone_number: int
    tracking_issue: int | None
    has_work_order: bool
    ready_frontier: int
    blocked: int
    in_flight: int
    done: int
    total: int
    needs_you: list[str] = field(default_factory=list)
    outcome_run_number: int | None = None
    outcome_met: int | None = None
    outcome_partial: int | None = None
    outcome_gap: int | None = None
    outcome_bottom_line: str | None = None
    outcome_diff_summary: str | None = None

    def to_dict(self) -> dict:
        """Serialise to a plain dict suitable for ``json.dumps``."""
        return {
            "repo": self.repo,
            "title": self.title,
            "milestone_number": self.milestone_number,
            "tracking_issue": self.tracking_issue,
            "has_work_order": self.has_work_order,
            "ready_frontier": self.ready_frontier,
            "blocked": self.blocked,
            "in_flight": self.in_flight,
            "done": self.done,
            "total": self.total,
            "needs_you": list(self.needs_you),
            "outcome_run_number": self.outcome_run_number,
            "outcome_met": self.outcome_met,
            "outcome_partial": self.outcome_partial,
            "outcome_gap": self.outcome_gap,
            "outcome_bottom_line": self.outcome_bottom_line,
            "outcome_diff_summary": self.outcome_diff_summary,
        }


# ── Pure helpers ─────────────────────────────────────────────────────────────


def find_tracking_issue(
    milestone_number: int,
    issues: list[dict],
) -> dict | None:
    """Return the first issue under *milestone_number* labelled ``"epic"``.

    ``issues`` is a raw issue-dict list — each item has at least ``"number"``,
    ``"labels"`` (list of ``{"name": ...}`` dicts), and ``"milestone"``
    (``None`` or ``{"number": ...}``), matching the shape returned by
    :func:`coord.github_ops.get_open_issues` / :func:`~coord.github_ops.get_closed_epics`.
    This function does not care about issue state — callers that want a
    closed epic to still count as the tracking issue (see
    :func:`aggregate_repo_plans`) pass a list that already includes closed
    epics alongside the open issues.

    Returns ``None`` when no epic is found for this milestone.  The caller
    decides how to report a missing tracking issue.
    """
    for issue in issues:
        ms = issue.get("milestone") or {}
        if ms.get("number") != milestone_number:
            continue
        labels = [lbl.get("name", "") for lbl in (issue.get("labels") or [])]
        if TRACKING_ISSUE_LABEL in labels:
            return issue
    return None


def find_unlabelled_epics(issues: list[dict]) -> list[dict]:
    """Return open issues whose title reads like an epic but whose cached
    labels don't include ``TRACKING_ISSUE_LABEL`` (#3227).

    ``issues`` is expected in the **local cache** shape — each item has at
    least ``"number"``, ``"title"``, ``"state"``, and ``"labels"`` — matching
    the rows :func:`coord.dao.SqliteStore.list_issues` decodes from the
    ``issues`` table (``labels`` as a plain ``list[str]``). A GitHub-shaped
    ``labels`` list (``[{"name": ...}]``, as :func:`find_tracking_issue`
    above consumes) is also accepted for convenience — each entry is
    normalised to its name either way.

    Closed issues are skipped: a closed epic missing the label is no longer
    actionable the way an open one is. Order is preserved from the input.
    This is read-only — it never mutates anything; the caller decides what,
    if anything, to do with the hits (this lint deliberately does not
    auto-label, per #3226).
    """
    hits: list[dict] = []
    for issue in issues:
        state = (issue.get("state") or "open").lower()
        if state != "open":
            continue
        title = issue.get("title") or ""
        if not EPIC_TITLE_RE.match(title):
            continue
        raw_labels = issue.get("labels") or []
        label_names = {
            lbl["name"] if isinstance(lbl, dict) else str(lbl) for lbl in raw_labels
        }
        if TRACKING_ISSUE_LABEL in label_names:
            continue
        hits.append(issue)
    return hits


# ── Stale-tracker lint (#3228) ──────────────────────────────────────────────


def find_stale_epics(issues: list[dict]) -> list[dict]:
    """Return open, ``TRACKING_ISSUE_LABEL``-labelled issues whose declared
    scope has already shipped, or was never registered (#3228).

    An epic is flagged when its children — parsed from its OWN cached
    ``body`` via :class:`coord.parentage.MarkdownParentage.children`
    (``fallback_to_work_order=True``, so an epic with only a ``## Work
    order`` block and no separate ``## Sub-issues`` checklist isn't reported
    as childless just because it predates the #1008 checklist convention) —
    are either:

    * absent entirely (zero registered children), or
    * all resolved to ``"closed"``.

    Each child's real state is looked up by ``(repo_name, number)`` in
    *issues* itself — the SAME already-cached rows this function scans for
    epics — rather than trusted from the checklist's own ``[x]``/``[ ]`` box.
    That box is not a live signal: #1061 deliberately stopped keeping it in
    sync once the checkbox-free ``## Work order`` grammar shipped, so an
    unchecked box no longer means "still open" (see
    :mod:`coord.milestone_order`'s ``parse_work_order``/``render_work_order``
    docstrings). A child not present in *issues* at all (a different repo,
    or not yet synced into the local cache) is unresolvable and counted as
    ``"open"`` — the conservative default, so an incomplete cache under-flags
    rather than over-flags a still-live epic.

    ``issues`` is the same local-cache shape :func:`find_unlabelled_epics`
    consumes, PLUS a ``"body"`` field (:func:`coord.state.cached_open_issues`
    carries it since #3228) — the epic's own tracking-issue body, needed to
    parse its declared children. Closed epics are skipped — a closed tracker
    with no open children isn't drift, it's just done.

    Read-only, like :func:`find_unlabelled_epics`: this never mutates
    ``issues``, closes anything, or edits a checklist (out of scope per
    #3226). Each hit is the original issue dict with three extra keys
    layered in: ``child_total``, ``child_open``, ``child_closed``.
    """
    state_by_key: dict[tuple[str, int], str] = {}
    for issue in issues:
        number = issue.get("number")
        if number is None:
            continue
        state_by_key[(issue.get("repo_name", ""), int(number))] = (
            issue.get("state") or "open"
        ).lower()

    parentage = MarkdownParentage()
    hits: list[dict] = []
    for issue in issues:
        state = (issue.get("state") or "open").lower()
        if state != "open":
            continue
        raw_labels = issue.get("labels") or []
        label_names = {
            lbl["name"] if isinstance(lbl, dict) else str(lbl) for lbl in raw_labels
        }
        if TRACKING_ISSUE_LABEL not in label_names:
            continue
        number = issue.get("number")
        if number is None:
            continue

        repo_name = issue.get("repo_name", "")
        try:
            children = parentage.children(
                repo_name,
                int(number),
                body=issue.get("body") or "",
                fallback_to_work_order=True,
            )
        except Exception:  # noqa: BLE001 — malformed checklist: skip this epic
            # `children()` parses the epic's own hand-edited body via
            # `parse_sub_issues`/`parse_work_order` (`coord.milestone_order`),
            # which raise `WorkOrderError` for a realistic range of malformed
            # checklists (a duplicate `#N` entry, an unknown annotation key, a
            # malformed/undeclared `after:` target, a dependency cycle) — and
            # `int(number)` above is defensively covered too, though `number`
            # is a SQLite INTEGER column so that branch is unreachable in
            # practice. One bad epic body must never blank the whole
            # `--lint-stale-epics` scan (nor its `--lint-epics` sibling output
            # sharing this command invocation) — same posture as
            # `MarkdownParentage.parent()` above and
            # `milestone_work_order_membership` in `coord.milestone_order`.
            continue

        child_open = 0
        child_closed = 0
        for child in children:
            real_state = state_by_key.get((repo_name, child.number), "open")
            if real_state == "closed":
                child_closed += 1
            else:
                child_open += 1

        if not children or child_open == 0:
            hits.append(
                {
                    **issue,
                    "child_total": len(children),
                    "child_open": child_open,
                    "child_closed": child_closed,
                }
            )
    return hits


def _has_pending_chat(
    board: Board,
    repo_name: str,
    tracking_issue_number: int | None,
) -> bool:
    """True iff an active ``type="milestone-chat"`` assignment targets
    *tracking_issue_number* in *repo_name* (#976).

    ``coord milestone chat <repo> <tracking_issue>``
    (:func:`coord.milestone_chat.dispatch_milestone_chat`) records the
    session as a normal :class:`~coord.models.Assignment` with
    ``issue_number=tracking_issue_number`` — the same board state every
    other claim check reads, so this needs no new plumbing.  Mirrors the
    ``a.status == "failed"`` skip in :func:`coord.claim.find_work_claim`: a
    failed chat dispatch never actually opened, so it isn't "pending."
    ``None`` tracking issue (no epic yet) can't have a chat dispatched
    against it — always ``False``.
    """
    if tracking_issue_number is None:
        return False
    return any(
        a.type == "milestone-chat"
        and a.repo_name == repo_name
        and a.issue_number == tracking_issue_number
        and a.status != "failed"
        for a in board.active
    )


def _latest_audit_outcome(
    board: Board, repo_name: str, tracking_issue_number: int | None,
) -> dict | None:
    """The latest ``--audit-of`` verdict for this milestone's epic (#886
    Phase 2), or ``None`` when no audit has ever run against it.

    Scans ``board.completed`` for ``type="audit"`` rows keyed by
    ``(repo_name, tracking_issue_number)`` — the epic's own issue number
    doubles as the audit assignment's ``issue_number`` (see #885's
    ``_dispatch_audit_of``) — and picks the highest ``audit_run_number``.
    When a second-highest run also exists, pre-renders a short delta string
    (via :func:`coord.issue_store.diff_audit_goals`) so callers (the TUI)
    don't need to re-derive a diff from two raw JSON blobs.  Board-driven and
    pure, like the rest of this module — no DB access here even though the
    Board's assignment rows ultimately came from one.
    """
    if tracking_issue_number is None:
        return None
    runs = sorted(
        (
            a
            for a in board.completed
            if a.type == "audit"
            and a.repo_name == repo_name
            and a.issue_number == tracking_issue_number
            and a.audit_run_number is not None
        ),
        key=lambda a: a.audit_run_number,
    )
    if not runs:
        return None
    latest = runs[-1]
    try:
        latest_goals = json.loads(latest.audit_goals_json or "[]")
    except (TypeError, ValueError):
        latest_goals = []
    met = sum(1 for g in latest_goals if g.get("verdict") == "met")
    gap = sum(1 for g in latest_goals if g.get("verdict") == "gap")
    total = len(latest_goals)
    partial = total - met - gap

    diff_summary: str | None = None
    if len(runs) >= 2:
        prev = runs[-2]
        try:
            prev_goals = json.loads(prev.audit_goals_json or "[]")
        except (TypeError, ValueError):
            prev_goals = []
        diff = diff_audit_goals(prev_goals, latest_goals)
        parts = []
        if diff["closed"]:
            parts.append(f"closed: {', '.join(diff['closed'])}")
        if diff["regressed"]:
            parts.append(f"REGRESSED: {', '.join(diff['regressed'])}")
        if diff["still_open"]:
            parts.append(f"still open: {', '.join(diff['still_open'])}")
        if parts:
            diff_summary = (
                f"v{prev.audit_run_number}→v{latest.audit_run_number}: "
                + "; ".join(parts)
            )

    return {
        "run_number": latest.audit_run_number,
        "met": met,
        "partial": partial,
        "gap": gap,
        "total": total,
        "bottom_line": latest.audit_bottom_line or "",
        "diff_summary": diff_summary,
    }


def aggregate_plan(
    *,
    milestone_title: str,
    milestone_number: int,
    repo_name: str,
    repo_github: str,
    tracking_issue_number: int | None,
    tracking_body: str | None,
    board: Board,
    open_issue_numbers: set[int],
) -> PlanEntry:
    """Compute a :class:`PlanEntry` for one milestone.

    Parameters
    ----------
    milestone_title:
        GitHub milestone title.
    milestone_number:
        GitHub milestone number.
    repo_name:
        coord-local repo name.
    repo_github:
        GitHub slug (``owner/repo``).
    tracking_issue_number:
        Issue number of the epic/tracking issue, or ``None``.
    tracking_body:
        Body text of the tracking issue, or ``None`` when there is no
        tracking issue.  An empty string is treated the same as ``None``
        (no work order).
    board:
        Current board state, used to find active assignments for in-flight
        detection.
    open_issue_numbers:
        Set of GitHub issue numbers that are currently *open* under this
        milestone.  Any work-order node **not** in this set is assumed
        terminal (closed).

    Returns
    -------
    PlanEntry
        Aggregated stats.  When ``tracking_body`` is ``None`` / empty, or the
        body has no ``## Work order`` block, ``has_work_order`` is ``False``
        and all counts are 0.
    """
    chat_pending = _has_pending_chat(board, repo_name, tracking_issue_number)
    # #886 Phase 2: the audit outcome is independent of the work-order/issue
    # counts below (the whole point — completion judged by goals met, not
    # issues closed), so it's computed once and attached to EVERY return path,
    # including the has_work_order=False ones.
    outcome = _latest_audit_outcome(board, repo_name, tracking_issue_number)
    outcome_kwargs = (
        {
            "outcome_run_number": outcome["run_number"],
            "outcome_met": outcome["met"],
            "outcome_partial": outcome["partial"],
            "outcome_gap": outcome["gap"],
            "outcome_bottom_line": outcome["bottom_line"],
            "outcome_diff_summary": outcome["diff_summary"],
        }
        if outcome is not None
        else {}
    )

    _no_work_order_signals = ["no_work_order"]
    if chat_pending:
        _no_work_order_signals.append("chat_pending")
    _no_work_order = PlanEntry(
        repo=repo_name,
        title=milestone_title,
        milestone_number=milestone_number,
        tracking_issue=tracking_issue_number,
        has_work_order=False,
        ready_frontier=0,
        blocked=0,
        in_flight=0,
        done=0,
        total=0,
        needs_you=_no_work_order_signals,
        **outcome_kwargs,
    )

    if not tracking_body:
        return _no_work_order

    try:
        work_order: WorkOrder = parse_work_order(tracking_body)
    except Exception:  # noqa: BLE001 — malformed work order; treat as absent
        return _no_work_order

    if not work_order.nodes:
        return _no_work_order

    # Terminal = declared in the work order but not currently open.
    terminal_issues: set[int] = {
        n.issue_number
        for n in work_order.nodes
        if n.issue_number not in open_issue_numbers
    }

    total = len(work_order.nodes)
    done = len(terminal_issues)

    frontier = ready_frontier(
        work_order,
        board,
        repo_name=repo_name,
        repo_github=repo_github,
        terminal_issues=terminal_issues,
        # No remote branch check here: a bulk aggregate must not spawn N gh
        # calls for branch lookups.  In-flight is derived from the board only.
        branch_lookup=lambda *_: [],
    )

    ready_count = len(frontier.ready)
    in_flight_count = sum(1 for b in frontier.blocked if b.claim is not None)
    blocked_count = sum(1 for b in frontier.blocked if b.claim is None)

    needs_you: list[str] = []
    if ready_count > 0:
        needs_you.append("ready_waiting")
    elif done < total and in_flight_count == 0:
        needs_you.append("stalled")
    if chat_pending:
        needs_you.append("chat_pending")

    return PlanEntry(
        repo=repo_name,
        title=milestone_title,
        milestone_number=milestone_number,
        tracking_issue=tracking_issue_number,
        has_work_order=True,
        ready_frontier=ready_count,
        blocked=blocked_count,
        in_flight=in_flight_count,
        done=done,
        total=total,
        needs_you=needs_you,
        **outcome_kwargs,
    )


def aggregate_repo_plans(
    *,
    repo_name: str,
    repo_github: str,
    milestones: list[dict],
    open_issues: list[dict],
    board: Board,
    closed_tracking_issues: list[dict] | None = None,
    issue_body_fetcher: "Callable[[int], str | None] | None" = None,
) -> list[PlanEntry]:
    """Aggregate plans for all milestones in one repo.

    Parameters
    ----------
    repo_name:
        coord-local repo name.
    repo_github:
        GitHub slug (``owner/repo``).
    milestones:
        List of milestone dicts (``{number, title, ...}``), as returned by
        :func:`coord.github_ops.get_repo_milestones`.
    open_issues:
        List of all open issue dicts for the repo, as returned by
        :func:`coord.github_ops.get_open_issues`.
    board:
        Current board state.
    closed_tracking_issues:
        Optional list of *closed* ``"epic"``-labelled issue dicts (e.g. from
        :func:`coord.github_ops.get_closed_epics`), merged with
        ``open_issues`` when searching for each milestone's tracking issue.
        Without this, a milestone whose tracking epic was closed while the
        milestone stayed open would be reported as ``"no_work_order"``
        instead of reflecting its (likely done) work order. ``None``/``[]``
        preserves the open-only lookup.
    issue_body_fetcher:
        Optional callable ``(issue_number: int) -> str | None`` that fetches
        the body of a *single* issue by number.  Required only when the
        tracking epic's body is not already in ``open_issues`` /
        ``closed_tracking_issues`` (it always should be, but the hook lets
        callers supply a pre-fetched body map for testing).  When ``None``,
        body is read from the matching entry in those snapshots only.

    Returns
    -------
    list[PlanEntry]
        One entry per milestone, in the same order as *milestones*.
    """
    from typing import Callable  # noqa: PLC0415 — keep import cheap

    closed_tracking_issues = closed_tracking_issues or []

    # Candidates for tracking-issue lookup: open issues + any closed epics
    # supplied by the caller. Deliberately kept separate from open-issue-only
    # terminal detection below (a closed epic isn't a work-order node).
    tracking_candidates: list[dict] = open_issues + closed_tracking_issues

    # Build a quick index: issue_number → body from the open + closed-epic
    # snapshots.
    body_index: dict[int, str] = {
        i["number"]: (i.get("body") or "")
        for i in tracking_candidates
    }

    # Build open-issue-numbers per milestone.
    open_by_milestone: dict[int, set[int]] = {}
    for issue in open_issues:
        ms = issue.get("milestone") or {}
        ms_num = ms.get("number")
        if ms_num is not None:
            open_by_milestone.setdefault(ms_num, set()).add(issue["number"])

    entries: list[PlanEntry] = []
    for ms in milestones:
        ms_num: int = ms["number"]
        ms_title: str = ms.get("title", f"Milestone #{ms_num}")

        tracking = find_tracking_issue(ms_num, tracking_candidates)
        tracking_number: int | None = tracking["number"] if tracking is not None else None

        if tracking_number is not None:
            body: str | None = body_index.get(tracking_number)
            # Call the fetcher when the body is absent or empty — an empty
            # string from the snapshot means the issue was returned without
            # body text, so we fall back to a direct fetch if available.
            if not body and issue_body_fetcher is not None:
                body = issue_body_fetcher(tracking_number)
        else:
            body = None

        open_nums = open_by_milestone.get(ms_num, set())

        entry = aggregate_plan(
            milestone_title=ms_title,
            milestone_number=ms_num,
            repo_name=repo_name,
            repo_github=repo_github,
            tracking_issue_number=tracking_number,
            tracking_body=body,
            board=board,
            open_issue_numbers=open_nums,
        )
        entries.append(entry)

    return entries
