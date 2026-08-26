"""Server-side per-issue stage/gate projection (#550, generalizes #776/#778).

``coord-tui``'s ``tui/src/app/pipeline.rs`` independently re-derives every
stage/gate computation from the raw ``/board`` rows — ``stage_status_for``,
``merge_stage_status_for``, ``test_stage_status_for``, and
``issue_has_any_approved_review`` (which duplicates
``coord.merge_queue.has_approved_review``'s intent, but keyed by issue
number rather than branch).  This module computes the same *DB-derivable*
subset of that logic once, in Python, so it can be injected into ``/board``
(``coord/serve_app.py``) and consumed by the TUI instead of re-implemented.

Deliberately excluded — genuinely TUI-session-local state with no server
equivalent, so it stays a client-side overlay on top of this projection:

* the optimistic "merge just dispatched" flag (``pipeline_inflight_merges``)
  set the instant the Go button is pressed, before the DB round-trip lands;
* a locally-spawned Phase-1 build subprocess (``test_build_in_flight``);
* the CI-check cache the TUI itself polls via the GitHub API
  (``pipeline_ci_checks``) — this module uses the server's own ``CiStore``
  instead, which is a *different* (also valid) CI signal source already
  wired into ``coord.merge_queue``'s gate evaluation.

Because local-SQLite-mode coord-tui (no ``coord serve`` daemon configured)
has no server to ask, the Rust functions this mirrors are NOT deleted —
they remain the local-mode fallback. The daemon path prefers this
projection when present. See #550 for the full rationale.

Pure computation: every function here takes already-loaded data and returns
plain values — no I/O, no side effects.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from coord.models import CLOSES_ISSUE_TYPES

# ── Stage-status vocabulary — mirrors tui/src/app/pipeline.rs::StageStatus ──
PENDING = "pending"
ACTIVE = "active"
DONE = "done"
FAILED = "failed"
STALE = "stale"
SKIPPED = "skipped"

_MERGED_STATES = frozenset({"merged"})
_ACTIVE_MERGE_STATES = frozenset({"open", "queued"})
# #919 review: "conflict" is a genuine resting/terminal merge_queue state
# (coord/merge_queue.py) reached whenever GitHub reports a real merge
# conflict and no conflict-fix worker is currently resolving it. Without it
# here, a conflicting entry fell through to the "pending" default in
# `_merge_stage_status` below, projecting a lit one-click Merge for an item
# that cannot actually merge — the false green #919 exists to close.
_FAILED_MERGE_STATES = frozenset({"failed", "human_required", "conflict"})


@runtime_checkable
class _AssignmentLike(Protocol):
    assignment_id: str | None
    type: str
    status: str
    dispatched_at: float | None
    review_verdict: str | None
    review_of_assignment_id: str | None
    test_state: str | None
    repo_name: str
    issue_number: int
    acceptance_state: str | None
    acceptance_total: int | None
    acceptance_passed: int | None


# ── Helpers ──────────────────────────────────────────────────────────────


def _latest_by_dispatch(assignments: list) -> Any | None:
    """Return the assignment with the max ``dispatched_at`` (None sorts last,
    matching Rust's ``partial_cmp(...).unwrap_or(Equal)`` on an Option)."""
    if not assignments:
        return None
    return max(
        assignments,
        key=lambda a: (a.dispatched_at is not None, a.dispatched_at or 0.0),
    )


def _issue_has_plan_assignment(assignments_for_issue: list) -> bool:
    return any(a.type == "plan" for a in assignments_for_issue)


def _work_ids_for_issue(assignments_for_issue: list, seed_work_id: str | None = None) -> set[str]:
    """Every ``CLOSES_ISSUE_TYPES`` assignment id for this issue, plus
    *seed_work_id* when given (#292: a queue entry keyed to a work id whose
    row has since been pruned from the board)."""
    work_ids = {
        a.assignment_id
        for a in assignments_for_issue
        if a.type in CLOSES_ISSUE_TYPES and a.assignment_id
    }
    if seed_work_id:
        work_ids.add(seed_work_id)
    return work_ids


def _review_verdict_events(assignments_for_issue: list, work_ids: set[str]) -> list:
    """Every verdict-bearing event connected to *work_ids*: dedicated
    ``type="review"`` rows scoped by ``review_of_assignment_id``, AND (#331)
    a verdict stamped directly on a work row itself (self-approval /
    PR-comment-fallback, no separate reviewer dispatched).

    #2085 (non-blocking finding): shared by :func:`issue_has_any_approved_review`
    and the "review" stage branch of :func:`stage_status_for` so the two can
    never disagree about which events count. Before this was factored out,
    ``stage_status_for``'s ``assignments_for_stage(..., "review", ...)`` only
    ever matched ``type == "review"`` rows — a self-approval stamped directly
    on a work row (no dedicated review assignment at all) was invisible to
    it, so ``has_approved_review: True`` could sit next to
    ``stages["review"]`` reading ``PENDING``/``SKIPPED`` in the same
    projection — the same READY-vs-refused disagreement #2085 is about, just
    via a different path.
    """
    return [
        a
        for a in assignments_for_issue
        if a.review_verdict
        and (
            (a.type == "review" and a.review_of_assignment_id in work_ids)
            or a.assignment_id in work_ids
        )
    ]


def assignments_for_stage(
    assignments_for_issue: list,
    stage: str,
    *,
    require_plan: bool,
) -> list:
    """Mirrors ``pipeline.rs::assignments_for_stage``.

    When the pipeline has no Plan stage in this issue's strip (no global
    ``require_plan`` and no ``type="plan"`` assignment for this issue),
    plan-typed assignments fold into "work" so a ``--plan-only`` dispatch
    without ``require_plan`` doesn't disappear from the Work stage.
    """
    fold_plan_into_work = (
        stage == "work"
        and not require_plan
        and not _issue_has_plan_assignment(assignments_for_issue)
    )
    out = []
    for a in assignments_for_issue:
        t = a.type or "work"
        if fold_plan_into_work:
            if t in ("work", "plan"):
                out.append(a)
        elif t == stage:
            out.append(a)
    return out


def upstream_max_dispatched_at(
    assignments_for_issue: list,
    stage: str,
    stage_names: list[str],
    *,
    require_plan: bool,
) -> float | None:
    """Mirrors ``pipeline.rs::upstream_max_dispatched_at``."""
    if stage not in stage_names:
        return None
    idx = stage_names.index(stage)
    if idx == 0:
        return None
    best: float | None = None
    for s in stage_names[:idx]:
        for a in assignments_for_stage(assignments_for_issue, s, require_plan=require_plan):
            if a.dispatched_at is not None:
                best = a.dispatched_at if best is None else max(best, a.dispatched_at)
    return best


def _has_active_conflict_fix(assignments_for_issue: list) -> bool:
    """Mirrors ``pipeline.rs::has_active_conflict_fix`` (#241)."""
    return any(
        a.type == "conflict-fix" and a.status in ("running", "pending")
        for a in assignments_for_issue
    )


def _has_active_smoke_session(assignments_for_issue: list) -> bool:
    """Mirrors ``pipeline.rs::has_active_smoke_session`` (#585)."""
    return any(
        a.type in ("smoke", "test-chat") and a.status in ("running", "pending")
        for a in assignments_for_issue
    )


def _ci_failed_for_entry(merge_entry: Any | None, ci_store: Any | None) -> bool:
    """Mirrors ``pipeline.rs::ci_failed_for_entry``, sourced from the
    server's ``CiStore`` rather than the TUI's own poll cache."""
    if ci_store is None or not getattr(ci_store, "is_available", False):
        return False
    if merge_entry is None or getattr(merge_entry, "pr_number", None) is None:
        return False
    from coord.ci_store import failed_checks  # noqa: PLC0415

    checks = ci_store.list_checks_for_pr(merge_entry.repo_github, merge_entry.pr_number)
    return bool(failed_checks(checks))


# ── Per-stage status functions ──────────────────────────────────────────


def stage_status_for_internal_work(
    assignments_for_issue: list,
    *,
    is_closed: bool,
    require_plan: bool,
) -> str:
    """Mirrors ``pipeline.rs::stage_status_for_internal_work``."""
    matching = assignments_for_stage(assignments_for_issue, "work", require_plan=require_plan)
    if any(a.status == "running" for a in matching):
        return ACTIVE
    latest = _latest_by_dispatch(matching)
    if latest is not None:
        if latest.status == "done":
            return DONE
        if latest.status == "failed":
            return FAILED
    return SKIPPED if is_closed else PENDING


def test_stage_status_for(
    assignments_for_issue: list,
    *,
    is_closed: bool,
    require_plan: bool,
) -> str:
    """Mirrors ``pipeline.rs::test_stage_status_for`` (#200/#235/#310/#585/#1395).

    Excludes the #235 "Phase 1 build in flight" override — that's a locally
    spawned TUI subprocess with no server-side equivalent; the TUI overlays
    it on top of this value.
    """
    work_status = stage_status_for_internal_work(
        assignments_for_issue, is_closed=is_closed, require_plan=require_plan
    )
    if work_status != DONE:
        return SKIPPED if is_closed else PENDING

    if _has_active_smoke_session(assignments_for_issue):
        return ACTIVE

    work = assignments_for_stage(assignments_for_issue, "work", require_plan=require_plan)
    with_verdict = [a for a in work if (a.test_state or "") != ""]
    verdict_assignment = _latest_by_dispatch(with_verdict)
    verdict = verdict_assignment.test_state if verdict_assignment else None
    # #1395: "running" is a transient, non-verdict marker — an unattended
    # driver (scripts/drive-issue.sh) that bypasses dispatch_smoke and runs
    # the suite locally sets it right before the run and overwrites it with a
    # terminal passed/failed/skipped verdict when the run concludes. Without
    # this, the Test box reads Pending (indistinguishable from "not started")
    # for however long the local suite takes — the #1395 invisible-test-stage
    # bug. It must never be treated as a verdict by any gate; the merge gate
    # (`has_smoke_verdict`) and the review gate (`dispatch_pending_reviews`)
    # both key off `test_state in ("passed", "skipped")`, so "running" already
    # fails closed there without any change.
    if verdict == "running":
        return ACTIVE
    if verdict in ("passed", "skipped"):
        return DONE
    if verdict == "failed":
        return FAILED
    # #2579: ``"contested"`` (``coord.notify.TEST_STATE_CONTESTED``) is an
    # independent #2464 re-run REFUTING a pass claim whose review had already
    # rendered a terminal "approve" verdict — deliberately distinct from the
    # literal string "failed" so it is never mistaken by an automatic
    # fix-dispatch door for an ordinary Test-stage failure (see that
    # constant's docstring). But per this module's own #1672 rule right
    # below — PENDING is reserved for values that say NOTHING about the
    # branch — "contested" is very much a statement about the branch (a
    # re-run disagreed with the claimed pass), so it must render exactly
    # like an ordinary failure here: a red Failed badge, not a neutral
    # Pending one indistinguishable from "nothing has happened yet".
    if verdict == "contested":
        return FAILED
    # #1672: ``"blocked"`` (``coord.smoke.TEST_STATE_BLOCKED`` — no
    # capability-matched machine could run the Test stage) deliberately falls
    # through to PENDING here rather than mapping to FAILED. FAILED is a
    # statement about the BRANCH, and the TUI acts on it: it would flip
    # `can_redispatch_work_after_test_failure` on (offering to re-dispatch
    # Work that is perfectly fine) and `test_gate_actionable` off (taking away
    # the operator's `B`/verdict keys on the one row that needs them). The
    # blocked reason reaches the operator through `test_reason` instead —
    # rendered by `coord gates` and, since no smoke row exists in this case,
    # by the TUI Summary tab's `board_assignment_reason` on the work row.
    #
    # #2272 parks the same ``"blocked"`` for a second cause — N Test-stage
    # legs finished without printing a `SMOKE:` marker and the retry budget
    # ran out — and it maps here for the identical reason: a mute leg says
    # nothing about the branch, so FAILED would be a lie about the diff and
    # would offer the operator a Work re-dispatch that fixes nothing. The
    # count and the cause travel in `test_reason` (`coord.smoke.
    # mute_smoke_legs` reads them back), which is what keeps a row on its
    # last retry distinguishable from one whose Test stage never started —
    # the confusion that let five identical mute laps look like progress.
    return PENDING


def acceptance_stage_status_for(assignments_for_issue: list) -> str:
    """The Acceptance box's status (#932/#944) — reported and gated
    *separately* from the Test box (docs/ORACLE_LOOP.md), so this mirrors
    ``test_stage_status_for``'s shape but keys off
    ``Assignment.acceptance_state`` rather than a dedicated assignment
    type: ``coord acceptance record`` stamps the verdict directly onto the
    work assignment row (see ``coord/commands/acceptance.py``), it never
    spawns a separate ``type="acceptance"`` assignment.

    Distinct from Test in one more way: an issue with no acceptance suite
    authored yet (no manifest slice, so ``acceptance record`` was never run
    against it) has no signal at all — SKIPPED rather than PENDING, since
    the Acceptance box only applies to oracle-loop milestones, not every
    issue on the board.
    """
    work = [a for a in assignments_for_issue if (a.type or "work") == "work"]
    with_state = [a for a in work if (a.acceptance_state or "") != ""]
    if not with_state:
        return SKIPPED
    latest = _latest_by_dispatch(with_state)
    state = latest.acceptance_state
    if state == "passed":
        return DONE
    if state == "failed":
        return FAILED
    return PENDING


def acceptance_progress_for(assignments_for_issue: list) -> dict[str, int] | None:
    """``{"passed": p, "total": t}`` from the latest recorded acceptance
    verdict for this issue, or ``None`` when no verdict exists yet or it
    predates #932's per-test counts. Backs the Acceptance box's
    partial-green display (e.g. "3/7 acceptance green") — a growing suite
    is *expected* to read sub-100% until the feature completes
    (docs/ORACLE_LOOP.md), so this is reporting, not a pass/fail gate.
    """
    work = [a for a in assignments_for_issue if (a.type or "work") == "work"]
    with_state = [a for a in work if (a.acceptance_state or "") != ""]
    if not with_state:
        return None
    latest = _latest_by_dispatch(with_state)
    if latest.acceptance_total is None or latest.acceptance_passed is None:
        return None
    return {"passed": latest.acceptance_passed, "total": latest.acceptance_total}


def merge_stage_status_for(
    assignments_for_issue: list,
    merge_entry: Any | None,
    *,
    is_closed: bool,
    ci_store: Any | None = None,
) -> str:
    """Mirrors ``pipeline.rs::merge_stage_status_for`` (#241/#290/#775).

    Excludes the #290 "just dispatched, DB not yet caught up" optimistic
    flag — that's TUI-session-local; the TUI overlays it on top.
    """
    if _has_active_conflict_fix(assignments_for_issue):
        return ACTIVE

    if merge_entry is not None:
        state = merge_entry.state
        if state in _MERGED_STATES:
            return DONE
        if state in _ACTIVE_MERGE_STATES:
            return ACTIVE
        if state in _FAILED_MERGE_STATES:
            return FAILED

    if _ci_failed_for_entry(merge_entry, ci_store):
        return FAILED

    # #775: the daemon's merge-reconcile tick prunes the queue row after
    # flipping the work assignment to status="merged" — fall back to the
    # assignment itself as evidence the Merge stage is Done.
    # #1142: gate on CLOSES_ISSUE_TYPES, not a bare `type == "work"` — a
    # merged assignment whose type doesn't actually resolve this issue (e.g.
    # a `coord pr` PR-opening helper for a test-author/mock-author original,
    # whose issue_number is a milestone tracking issue) must not be read as
    # this issue's own work merging. See `coord.models.PR_HELPER_TYPE`.
    if any(a.type in CLOSES_ISSUE_TYPES and a.status == "merged" for a in assignments_for_issue):
        return DONE

    return SKIPPED if is_closed else PENDING


def stage_status_for(
    assignments_for_issue: list,
    stage: str,
    *,
    stage_names: list[str],
    is_closed: bool,
    require_plan: bool,
    merge_entry: Any | None = None,
    ci_store: Any | None = None,
) -> str:
    """Mirrors ``pipeline.rs::stage_status_for`` — the generic per-stage
    dispatcher, including the #193 "stale downstream verdict" check.

    #2085: the "review" stage branch deliberately DIVERGES from
    ``pipeline.rs`` in one respect — it also folds in a #331 self-approval
    verdict stamped directly on a work row (see the ``stage == "review"``
    block below). That closes a same-projection disagreement this server
    endpoint can produce (``has_approved_review: True`` next to
    ``stages["review"]`` reading ``PENDING``); local-mode ``coord-tui``
    (no server) still has the older, narrower behavior until the Rust side
    picks up the equivalent fix.
    """
    if stage == "merge":
        return merge_stage_status_for(
            assignments_for_issue, merge_entry, is_closed=is_closed, ci_store=ci_store
        )
    if stage == "test":
        return test_stage_status_for(
            assignments_for_issue, is_closed=is_closed, require_plan=require_plan
        )

    matching = assignments_for_stage(assignments_for_issue, stage, require_plan=require_plan)
    if stage == "review":
        # #2085 (non-blocking finding): `assignments_for_stage`'s strict
        # `type == "review"` filter misses a #331 self-approval verdict
        # stamped directly on a work row — fold in the SAME event set
        # `issue_has_any_approved_review` scans so the "review" stage badge
        # and the projection's `has_approved_review` field can't disagree.
        work_ids = _work_ids_for_issue(assignments_for_issue)
        seen = {id(a) for a in matching}
        matching = matching + [
            a for a in _review_verdict_events(assignments_for_issue, work_ids)
            if id(a) not in seen and a.type != "review"
        ]
    # #1566: "finalizing" is a review row whose agent finished but whose
    # verdict hasn't been parsed + persisted yet (`coord notify`'s slower,
    # separate step — see `coord.reconcile.reconcile_completed_assignments`).
    # Treat it exactly like "running" here: without this, the #473/#812
    # verdict-based mapping below would read a still-in-flight review as a
    # terminal "done" with no verdict and paint it FAILED — the same
    # dead-end misread #1566 was filed over, just in the board's colours
    # instead of `coord drive`'s exit.
    if any(a.status in ("running", "finalizing") for a in matching):
        return ACTIVE

    latest = _latest_by_dispatch(matching)
    if latest is not None:
        mapped: str | None = None
        if latest.status == "done" and stage == "review":
            # #473/#812: key off the verdict, not merely that review ran.
            mapped = DONE if latest.review_verdict == "approve" else FAILED
        elif latest.status == "done":
            mapped = DONE
        elif latest.status == "failed":
            mapped = FAILED

        if mapped is not None:
            if latest.dispatched_at is not None:
                upstream = upstream_max_dispatched_at(
                    assignments_for_issue, stage, stage_names, require_plan=require_plan
                )
                if upstream is not None and upstream > latest.dispatched_at:
                    return STALE
            return mapped

    return SKIPPED if is_closed else PENDING


def issue_has_any_approved_review(
    assignments_for_issue: list,
    seed_work_id: str | None = None,
) -> bool:
    """Mirrors ``pipeline.rs::issue_has_any_approved_review`` (#292/#331).

    Also the issue-scoped equivalent of
    ``coord.merge_queue.has_approved_review`` (which is branch-scoped, keyed
    off a single ``QueuedMerge`` entry) — this collects every work
    assignment for the *issue* so a bounce-created fix worker's approval is
    found even when a merge-queue entry is still keyed to the original work.

    #2085: before this fix, ANY historical ``approve`` anywhere in the
    issue's work chain counted, forever — including one superseded by a
    later ``request-changes`` on newer commits. That produced a board
    record with ``stages["review"] == "failed"`` (the general
    ``stage_status_for`` dispatcher already keys off the *latest* review by
    dispatch order) sitting next to ``has_approved_review: True`` in the
    same projection — self-contradictory on its face, and the exact
    daemon-vs-gate disagreement #2085 traces back to `coord drive` looping
    on a merge that could never land.
    ``coord.merge_queue.has_approved_review`` closes the equivalent gap with
    a live commit-SHA/patch-id bind, which this module deliberately has no
    I/O to perform (see module docstring). Dispatch order over the issue's
    own recorded verdict history is the best available DB-only proxy, and —
    crucially — it is the SAME ordering rule ``stage_status_for`` already
    applies to the "review" stage badge computed alongside this field in
    :func:`compute_issue_projection` (both now share
    :func:`_review_verdict_events`, #2085), so the two can no longer disagree
    the way the #2085 board record did — including the #331 self-approval
    case, where earlier the "review" stage badge alone missed the event this
    function already counted.
    """
    # #1142: CLOSES_ISSUE_TYPES, not a bare `type == "work"` — see the same
    # rationale in `merge_stage_status_for` above.
    work_ids = _work_ids_for_issue(assignments_for_issue, seed_work_id)
    if not work_ids:
        return False

    # The MOST RECENTLY DISPATCHED event wins — mirrors `stage_status_for`'s
    # own "latest by dispatch" rule for the "review" stage, so an earlier
    # `approve` superseded by a later `request-changes` (on newer commits)
    # no longer counts, matching what the merge gate's commit-bound check
    # would also refuse.
    events = _review_verdict_events(assignments_for_issue, work_ids)
    if not events:
        return False

    return _latest_by_dispatch(events).review_verdict == "approve"


# ── Board-level projection ──────────────────────────────────────────────


def pipeline_stage_names(default_gates: list[str]) -> list[str]:
    """Mirrors ``pipeline.rs::pipeline_stage_names`` (module-default, no
    per-issue plan-assignment prepend — see ``issue_stage_names``)."""
    stages = ["work"]
    for g in default_gates:
        # #1429: "merge" is restored to the per-issue stage-name ordering as
        # a read-only observation badge (#738 retired the per-issue *box*
        # with its Go/dispatch affordance — that reasoning covered the
        # affordance, not observation; merge is still initiated solely from
        # the Merge Queue panel). "work"/"plan" stay excluded here since
        # they're prepended explicitly above / by the caller.
        if g not in ("work", "plan"):
            stages.append(g)
    return stages


def issue_stage_names(assignments_for_issue: list, default_gates: list[str]) -> list[str]:
    """Mirrors ``pipeline.rs::pipeline_stage_names_for_issue``."""
    stages = pipeline_stage_names(default_gates)
    if stages[0] != "plan" and _issue_has_plan_assignment(assignments_for_issue):
        stages = ["plan", *stages]
    return stages


def compute_issue_projection(
    assignments_for_issue: list,
    merge_entry: Any | None,
    *,
    is_closed: bool,
    require_plan: bool,
    default_gates: list[str],
    ci_store: Any | None = None,
) -> dict[str, Any]:
    """Compute the full per-issue stage badge dict.

    ``stages`` covers every name in this issue's stage strip (``plan``?,
    ``work``, then the configured gates minus ``work``/``plan``) — including
    ``merge`` (#1429: restored as a read-only observation badge; ``stage_status_for``
    special-cases ``"merge"`` to delegate to ``merge_stage_status_for``, so the
    loop below already produces the correct value) — plus a redundant explicit
    ``merge`` assignment as a defensive fallback in case ``merge`` is ever
    absent from ``names`` again (e.g. a future ``default_gates`` without it).
    """
    names = issue_stage_names(assignments_for_issue, default_gates)
    stages: dict[str, str] = {}
    for name in names:
        stages[name] = stage_status_for(
            assignments_for_issue,
            name,
            stage_names=names,
            is_closed=is_closed,
            require_plan=require_plan,
            merge_entry=merge_entry,
            ci_store=ci_store,
        )
    stages.setdefault(
        "merge",
        merge_stage_status_for(
            assignments_for_issue, merge_entry, is_closed=is_closed, ci_store=ci_store
        ),
    )
    # #932: the Acceptance box, computed unconditionally like "merge" above
    # (own box, own verdict — reported separately from the Test stage) and
    # excluded from the per-issue stage-strip ordering that `default_gates`
    # drives, since it only applies to oracle-loop milestones.
    stages["acceptance"] = acceptance_stage_status_for(assignments_for_issue)
    return {
        "stages": stages,
        "acceptance_progress": acceptance_progress_for(assignments_for_issue),
        "has_approved_review": issue_has_any_approved_review(
            assignments_for_issue,
            seed_work_id=merge_entry.assignment_id if merge_entry is not None else None,
        ),
    }


def compute_board_stage_projection(
    *,
    issues: list[dict],
    assignments: list,
    merge_queue_items: list,
    default_gates: list[str],
    require_plan: bool = False,
    ci_store: Any | None = None,
) -> list[dict[str, Any]]:
    """Compute the per-issue stage projection for every issue that appears
    on the board — the payload injected into ``GET /board`` as
    ``issue_stage_projection``.

    Issue keys are ``(repo_name, issue_number)`` — the union of the
    ``issues`` table (open + recently-synced) and every assignment's
    ``(repo_name, issue_number)`` (so closed issues with assignment history
    still get a projection, matching what the TUI's Pipeline tab shows).
    """
    is_closed_by_key: dict[tuple[str, int], bool] = {
        (i["repo_name"], i["number"]): str(i.get("state", "")).lower() == "closed"
        for i in issues
    }
    issue_title_by_key: dict[tuple[str, int], str] = {
        (i["repo_name"], i["number"]): i.get("title", "") for i in issues
    }

    assignments_by_key: dict[tuple[str, int], list] = {}
    for a in assignments:
        if not a.repo_name or a.issue_number is None:
            continue
        assignments_by_key.setdefault((a.repo_name, a.issue_number), []).append(a)

    merge_by_key: dict[tuple[str, int], Any] = {}
    for m in merge_queue_items:
        assignment_type = getattr(m, "assignment_type", "work") or "work"
        if assignment_type not in CLOSES_ISSUE_TYPES:
            # This row's `issue_number` is the milestone's tracking issue, not
            # something this PR resolves (#1077/#1084) — it's the sealed
            # acceptance suite being delivered (docs/ORACLE_LOOP.md), not a
            # merge that closes anyone's work. #1203 tried re-attributing it
            # to the child issue named by the originating assignment's
            # `for_issue_number`, but that just moved the false green from
            # the epic to the child (claude-coordinator#1652) — the child's
            # own work still has to be tested, reviewed, and merged on its
            # own branch. There is no issue whose Merge box this row
            # legitimately describes, so skip it entirely rather than
            # attribute it anywhere.
            continue
        key = (m.repo_name, m.issue_number)
        # First-match-wins, mirroring `.find()` over the id-ordered list —
        # `load_queue()`/`board_projection()` both order by `id` ascending.
        merge_by_key.setdefault(key, m)

    keys = set(is_closed_by_key) | set(assignments_by_key)

    result: list[dict[str, Any]] = []
    for repo_name, issue_number in keys:
        key = (repo_name, issue_number)
        issue_assignments = assignments_by_key.get(key, [])
        entry = compute_issue_projection(
            issue_assignments,
            merge_by_key.get(key),
            is_closed=is_closed_by_key.get(key, False),
            require_plan=require_plan,
            default_gates=default_gates,
            ci_store=ci_store,
        )
        entry["repo_name"] = repo_name
        entry["issue_number"] = issue_number
        entry["issue_title"] = issue_title_by_key.get(key, "")
        result.append(entry)

    result.sort(key=lambda e: (e["repo_name"], e["issue_number"]))
    return result
