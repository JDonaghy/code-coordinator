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

import dataclasses
from typing import Any, Protocol, runtime_checkable

from coord.models import CLOSES_ISSUE_TYPES, WORK_LIKE_TYPES, effective_issue_number

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
# #919 review (round 2): "skipped" is the third state `_state_to_plan_status`
# folds into PLAN_NEEDS_ATTENTION, and `_RESOLVE_TERMINAL_STATES` treats it as
# terminal alongside "merged" — it will not self-resolve, so it is just as
# much "not ready to merge" as "conflict". It is kept *out* of
# `_FAILED_MERGE_STATES` on purpose: unlike conflict/human_required, a
# superseded ("skipped") row routinely survives next to the *successful*
# attempt for the same issue whose own row the reconcile tick then prunes
# (#775), so matching it before the merged-work-assignment / closed-issue
# fallbacks would paint a finished issue red. Checked last instead — see
# `merge_stage_status_for`.
_SUPERSEDED_MERGE_STATES = frozenset({"skipped"})


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
    uat_state: str | None
    uat_reason: str | None


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


def _leg_count_for_stage(
    assignments_for_issue: list,
    stage: str,
    *,
    require_plan: bool,
) -> int:
    """How many separate dispatched attempts ("legs") this issue has had at
    *stage* (#3013) — "attempts a human would recognise as separate", not a
    raw row count. Each retry/redispatch is already its own board row (a
    retry dispatches a fresh assignment id rather than mutating the failed
    one, see ``coord.reconcile._reassign``), so counting rows already gives
    the right number for stages backed by their own dedicated assignment
    ``type`` — this only needs to special-case the stages that aren't:

    * ``"work"`` is widened from a literal ``type == "work"`` (plus the
      existing plan-fold) to the full :data:`WORK_LIKE_TYPES` — an
      oracle-loop acceptance slice's repeated ``test-author``/``mock-author``
      legs (#3013's coord-portal#164 example: 4 ``test-author`` legs, 5
      ``review`` legs on one branch) are exactly the kind of repetition this
      field exists to surface, and a strict ``type == "work"`` match would
      never see them.
    * ``"test"`` has no ``type="test"`` assignment — the Test-stage's
      dispatched worker is ``type="smoke"`` (``coord.smoke``).
    * ``"merge"`` has no ``type="merge"`` assignment either — repeated
      landing attempts show up as ``type="conflict-fix"`` legs (#241). Note
      the counting convention this implies differs from every other stage:
      a clean single merge with no conflict reports ``0`` (no *retry* was
      needed), whereas one ordinary ``"work"``/``"review"``/``"test"`` leg
      reports ``1`` (one attempt happened). A client applying one uniform
      "suppress the number at <= 1" rule across every stage — as the
      ``/board`` schema description implies — will suppress a single
      conflict-fix leg (``count == 1``, arguably the most useful case to
      surface: "this needed a rebase") exactly like "no problem at all". No
      code path here can distinguish the two without a wire-schema change,
      so this is flagged for the client rather than fixed on this side.
    * ``"acceptance"`` (review fix, #3013) has no dedicated assignment type
      either — like ``test_state``/``uat_state``, ``acceptance_state`` is
      stamped directly onto the ``type="work"`` row a
      ``coord acceptance record`` verdict belongs to (see
      :func:`acceptance_stage_status_for`, which reads the same rows), so a
      generic ``assignments_for_stage(..., "acceptance", ...)`` scan would
      always return zero.

    Every other stage (``"plan"``, ``"review"``, ``"uat"``, ...) already has
    a 1:1 assignment ``type``, so :func:`assignments_for_stage` alone gives
    the right count.
    """
    if stage == "work":
        matching = assignments_for_stage(assignments_for_issue, "work", require_plan=require_plan)
        seen = {id(a) for a in matching}
        extra = [
            a
            for a in assignments_for_issue
            if id(a) not in seen and (a.type or "work") in WORK_LIKE_TYPES
        ]
        return len(matching) + len(extra)
    if stage == "test":
        return sum(1 for a in assignments_for_issue if (a.type or "work") == "smoke")
    if stage == "merge":
        return sum(1 for a in assignments_for_issue if (a.type or "work") == "conflict-fix")
    if stage == "acceptance":
        return sum(
            1
            for a in assignments_for_issue
            if (a.type or "work") == "work" and (a.acceptance_state or "") != ""
        )
    return len(assignments_for_stage(assignments_for_issue, stage, require_plan=require_plan))


def _widen_work_like_types(assignments: list) -> list:
    """Return *assignments* with every :data:`WORK_LIKE_TYPES` row (#3013:
    ``mock-author``/``test-author``, not just literal ``type="work"``)
    presented as ``type="work"`` to the generic per-stage dispatcher
    (:func:`stage_status_for`, :func:`assignments_for_stage`,
    :func:`acceptance_stage_status_for`, ...), all of which key strictly off
    ``a.type``.

    Used ONLY by :func:`compute_board_stage_projection`'s phantom-entry
    fallback (see that function's docstring) — without it, a slice whose
    only rows are ``test-author``/``mock-author`` legs got
    ``stage_counts["work"] == 4`` (via :func:`_leg_count_for_stage`, which
    already widens the same way) sitting next to ``stages["work"] ==
    "pending"`` forever, since the strict-``type`` dispatcher never
    recognised those rows as "work" activity. Every other field (status,
    dispatched_at, test_state, acceptance_state, ...) is preserved
    unchanged — this only relabels the ``type`` a downstream ``==`` check
    sees.
    """
    out = []
    for a in assignments:
        t = a.type or "work"
        widen = t in WORK_LIKE_TYPES and t != "work"
        out.append(dataclasses.replace(a, type="work") if widen else a)
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


def uat_stage_status_for(
    assignments_for_issue: list,
    *,
    is_closed: bool,
    require_plan: bool,
) -> str:
    """The UAT box's status (#2687/#2951) — modelled directly on
    :func:`test_stage_status_for`, which solves the identical "verdict is
    stamped onto the work row, not a dedicated ``type="uat"`` assignment"
    shape: ``coord uat <id> --passed|--failed`` writes ``uat_state``/
    ``uat_reason`` straight onto the work row exactly the way ``coord test``
    writes ``test_state``/``test_reason`` (see ``coord.state.
    record_uat_verdict``).

    Simpler than Test in two ways that both trace back to #2687's design
    (see ``coord.merge_queue.evaluate_uat_verdict``'s docstring): there is
    no ``"running"``/ACTIVE transient — ``coord uat`` records a human's
    one-shot judgment on a rendered preview, not a re-runnable driver — and
    no staleness re-check, since a UAT verdict isn't a measurement that a
    moved SHA could invalidate the way a Test/Review verdict can.

    Before this existed, ``stage_status_for``'s generic dispatcher matched
    on assignment *type*, and nothing ever creates a ``type="uat"``
    assignment — the badge sat PENDING forever regardless of the recorded
    verdict (#2951 cause 2).
    """
    work_status = stage_status_for_internal_work(
        assignments_for_issue, is_closed=is_closed, require_plan=require_plan
    )
    if work_status != DONE:
        return SKIPPED if is_closed else PENDING

    work = assignments_for_stage(assignments_for_issue, "work", require_plan=require_plan)
    with_verdict = [a for a in work if (a.uat_state or "") != ""]
    verdict_assignment = _latest_by_dispatch(with_verdict)
    verdict = verdict_assignment.uat_state if verdict_assignment else None
    if verdict == "passed":
        return DONE
    if verdict == "failed":
        return FAILED
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

    if is_closed:
        return SKIPPED

    # #919 review (round 2): a still-open issue whose only queue row is
    # `skipped` (superseded by a newer attempt that has since vanished) has
    # nothing in flight and nothing that will self-resolve — reporting
    # PENDING here lights the one-click [Go] on the Merge box for an item
    # `pipeline_merge_state()` would already refuse to dispatch. Same false
    # green as the `conflict` case, one state value over.
    if merge_entry is not None and merge_entry.state in _SUPERSEDED_MERGE_STATES:
        return FAILED

    return PENDING


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
    if stage == "uat":
        return uat_stage_status_for(
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


def _repo_for(repo_name: str, config: Any | None) -> Any | None:
    """Best-effort ``config.repo(repo_name)`` lookup, duck-typed the same
    fail-soft way as ``coord.merge_queue._uat_repo_for`` — an unknown repo
    name or a config stand-in without ``.repo()`` (a minimal test double)
    is "can't confirm this repo opted in", not a crash. ``config`` is
    already fully loaded by the caller (no file/network I/O happens here),
    so this stays inside the module's "pure computation" contract."""
    if config is None:
        return None
    try:
        return config.repo(repo_name)
    except Exception:  # noqa: BLE001 — unknown/malformed repo: no signal
        return None


def repo_has_uat_preview(repo_name: str, config: Any | None) -> bool:
    """The per-repo half of ``coord.merge_queue.requires_uat``'s two-part
    UAT opt-in (#2687) — the fleet-wide half (``"uat" in default_gates``) is
    threaded separately as ``uat_enabled`` (see :func:`pipeline_stage_names`).

    #2948: mirrors ``requires_uat``'s own per-repo check exactly — ``True``
    when *either* ``Repo.uat_preview`` (the override template) or
    ``Repo.uat_live_preview`` (the live GitHub-Deployment lookup) is set.
    Before this fix the badge only ever lit up for ``uat_preview``, so a
    repo that opted in via ``uat_live_preview`` alone — the shape this PR's
    own docs recommend for a project with no templatable preview host —
    read as "gate off" on the board while ``coord merge`` was actively
    blocking on it. Answering "has this repo opted in" any other way here
    than ``requires_uat`` does is exactly the split-brain #2948 exists to
    close.
    """
    repo = _repo_for(repo_name, config)
    if repo is None:
        return False
    return bool(getattr(repo, "uat_preview", None)) or bool(
        getattr(repo, "uat_live_preview", False)
    )


def uat_preview_url_for(
    repo_name: str,
    issue_number: int | None,
    merge_entry: Any | None,
    config: Any | None,
) -> str | None:
    """Best-effort rendered UAT preview URL for this issue's PR (#2951 item
    3) — mirrors the preview-resolution half of ``coord.merge_queue.
    evaluate_uat_verdict`` (minus its ``coord uat`` command text, which the
    caller can build itself from the assignment id it already has). Returns
    ``None`` when the repo isn't configured or hasn't opted in.

    #2948: this module is pure computation (see the module docstring — no
    I/O), so unlike ``evaluate_uat_verdict`` this can only ever render
    ``Repo.uat_preview``'s override template — it has no ``gh_ops`` to run
    the live GitHub-Deployment lookup ``Repo.uat_live_preview`` opts into.
    For a repo configured with ``uat_live_preview`` alone this therefore
    returns ``None`` even though :func:`repo_has_uat_preview` now reports
    the repo as opted in (both intentional — see that function's docstring):
    the caller (``compute_board_stage_projection``) still surfaces the "uat"
    badge and gate state from ``uat_enabled``, just without a clickable URL
    in this code path — a deliberately best-effort, template-only surface,
    not the #2948 bug class (a plausible but dead link) reappearing. A
    caller that wants the live URL server-side must call
    ``coord.merge_queue.evaluate_uat_verdict``/``_resolve_uat_preview_url``
    (with a real ``gh_ops``) instead."""
    repo = _repo_for(repo_name, config)
    if repo is None:
        return None
    return repo.resolve_uat_preview_url(
        branch=getattr(merge_entry, "branch", None),
        issue_number=issue_number,
        pr_number=getattr(merge_entry, "pr_number", None),
    )


def pipeline_stage_names(default_gates: list[str], *, uat_enabled: bool = False) -> list[str]:
    """Mirrors ``pipeline.rs::pipeline_stage_names`` (module-default, no
    per-issue plan-assignment prepend — see ``issue_stage_names``), plus the
    #2951 repo-awareness half ``pipeline.rs`` does not have yet (see that
    issue's "The Rust mirror" section — ``coord-tui`` isn't a dispatchable
    fleet repo, so this drifts from the Rust mirror on purpose until it is).

    ``"uat"`` is a two-part opt-in (``coord.merge_queue.requires_uat``):
    ``default_gates`` alone is only the FLEET-WIDE half. ``uat_enabled``
    carries the caller's already-resolved per-repo half (``bool(repo and
    repo.uat_preview)``) — this module has no config/repo lookup of its own
    (see module docstring: pure computation, no I/O) — so a repo that
    hasn't set ``uat_preview`` never gets the badge, no matter what
    ``default_gates`` says. Defaults to ``False`` (hidden) rather than
    ``True`` so a caller that forgets to resolve it fails toward "no repo
    opted in" — the common case — not toward a fleet-wide false badge.
    """
    stages = ["work"]
    for g in default_gates:
        # #1429: "merge" is restored to the per-issue stage-name ordering as
        # a read-only observation badge (#738 retired the per-issue *box*
        # with its Go/dispatch affordance — that reasoning covered the
        # affordance, not observation; merge is still initiated solely from
        # the Merge Queue panel). "work"/"plan" stay excluded here since
        # they're prepended explicitly above / by the caller.
        if g in ("work", "plan"):
            continue
        if g == "uat" and not uat_enabled:
            continue
        stages.append(g)
    return stages


def issue_stage_names(
    assignments_for_issue: list, default_gates: list[str], *, uat_enabled: bool = False
) -> list[str]:
    """Mirrors ``pipeline.rs::pipeline_stage_names_for_issue`` (see
    :func:`pipeline_stage_names` for the #2951 ``uat_enabled`` addendum)."""
    stages = pipeline_stage_names(default_gates, uat_enabled=uat_enabled)
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
    uat_enabled: bool = False,
    uat_preview_url: str | None = None,
    stage_count_assignments: list | None = None,
) -> dict[str, Any]:
    """Compute the full per-issue stage badge dict.

    ``stages`` covers every name in this issue's stage strip (``plan``?,
    ``work``, then the configured gates minus ``work``/``plan``) — including
    ``merge`` (#1429: restored as a read-only observation badge; ``stage_status_for``
    special-cases ``"merge"`` to delegate to ``merge_stage_status_for``, so the
    loop below already produces the correct value) — plus a redundant explicit
    ``merge`` assignment as a defensive fallback in case ``merge`` is ever
    absent from ``names`` again (e.g. a future ``default_gates`` without it).

    ``uat_enabled`` (#2951) is the caller's already-resolved per-repo half of
    ``requires_uat``'s two-part opt-in — see :func:`pipeline_stage_names` —
    so ``"uat"`` only appears in ``stages`` for a repo that actually
    configured ``Repo.uat_preview``. ``uat_preview_url`` is the rendered
    preview link for THIS issue's PR when available (``None`` otherwise);
    carried through unconditionally so a caller can show it next to the
    badge instead of sending the operator to ``coord merge``'s refusal
    message to find it (#2951 item 3).

    ``stage_count_assignments`` (#3013) is the assignment list
    :func:`_leg_count_for_stage` scans to fill ``stage_counts`` — a client
    can then render ``Work (2)`` / ``Review (5)`` next to the status badge
    and suppress the suffix at 1. Defaults to *assignments_for_issue* (the
    same list ``stages`` uses) when omitted, which is correct for an
    ordinary issue; :func:`compute_board_stage_projection` passes a
    separately-keyed list for the #1553 oracle-loop epic/slice case — see
    that function's docstring.
    """
    names = issue_stage_names(assignments_for_issue, default_gates, uat_enabled=uat_enabled)
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

    count_source = (
        assignments_for_issue if stage_count_assignments is None else stage_count_assignments
    )
    stage_counts: dict[str, int] = {
        name: _leg_count_for_stage(count_source, name, require_plan=require_plan) for name in names
    }
    stage_counts.setdefault(
        "merge", _leg_count_for_stage(count_source, "merge", require_plan=require_plan)
    )
    # Review fix (#3013): `stages` always carries "acceptance" (added
    # unconditionally above, outside `names`/`default_gates`), but
    # `stage_counts` was built only from `names` plus an explicit "merge"
    # fallback — so it silently dropped "acceptance", contradicting the
    # `/board` schema's documented "same key set as `stages`" guarantee
    # (coord/serve_app.py) and leaving a client that indexes `stage_counts`
    # by every key in `stages` with a plain `KeyError`.
    stage_counts.setdefault(
        "acceptance", _leg_count_for_stage(count_source, "acceptance", require_plan=require_plan)
    )
    return {
        "stages": stages,
        "stage_counts": stage_counts,
        "acceptance_progress": acceptance_progress_for(assignments_for_issue),
        "uat_preview_url": uat_preview_url if uat_enabled else None,
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
    config: Any | None = None,
) -> list[dict[str, Any]]:
    """Compute the per-issue stage projection for every issue that appears
    on the board — the payload injected into ``GET /board`` as
    ``issue_stage_projection``.

    Issue keys are ``(repo_name, issue_number)`` — the union of the
    ``issues`` table (open + recently-synced) and every assignment's
    ``(repo_name, issue_number)`` (so closed issues with assignment history
    still get a projection, matching what the TUI's Pipeline tab shows).

    ``config`` (#2951) is the ONLY place in this module that resolves the
    per-repo half of the UAT gate's two-part opt-in — every issue on a given
    repo shares the same ``repo_has_uat_preview`` answer, so it's resolved
    once here (not re-derived per-issue by lower functions, which take the
    already-resolved ``uat_enabled``/``uat_preview_url`` as plain values).
    ``None`` (the default — e.g. a caller with no config loaded) means no
    repo is treated as opted in, matching :func:`pipeline_stage_names`'s own
    fail-toward-hidden default.

    ``stage_counts`` (#3013) is deliberately built from a SECOND index keyed
    by ``coord.models.effective_issue_number`` rather than the raw
    ``assignments_by_key`` below: an oracle-loop acceptance slice's
    ``test-author``/``mock-author``/``review`` legs are booked
    (``issue_number``) to the milestone's *tracking* issue, with the child
    they're actually FOR named in ``for_issue_number`` (see
    ``coord gates <repo> <slice>``, which resolves the same way). Reusing
    ``assignments_by_key`` for the count would leave a slice's own
    ``stage_counts`` reading 0 forever — the same keying trap noted in the
    #3013 issue's "drive-queue sweep bug" reference. For an issue that ALSO
    has its own raw-keyed rows, this does NOT touch ``stages``: that stays
    keyed on the raw ``issue_number`` on purpose (#1652 — re-attributing
    status by ``for_issue_number`` moved a false "merged" green from the
    epic onto a child that already had its own, separately-tracked state; a
    leg *count* carries no such false-green risk, so the two fields are
    allowed to disagree on which issue owns which rows in that case).

    Review fix (#3013): a *phantom* entry — ANY key whose raw-keyed
    assignment list is empty, whether it exists in ``issues`` with no
    assignments of its own (the coord-portal#164 test below: the slice IS
    already synced, but every one of its legs is booked to the tracking
    issue) or exists ONLY via the effective-key union (never synced at all:
    not yet caught up by ``_sync_issues_tick``, or a closed slice pruned
    from ``issues`` after 7 days while assignment retention runs 14, see
    ``coord/state.py``) — has nothing raw-keyed to protect from the #1652
    clobber, because there IS no separately-tracked state for it to
    conflict with. Rendering ``stages`` from the empty raw list anyway
    produced a self-contradictory row: every stage PENDING (acceptance
    SKIPPED) right next to a ``stage_counts`` proving real legs ran. So for
    that case only, ``stages``/``acceptance``/``has_approved_review`` fall
    back to the same effective-keyed list ``stage_counts`` already uses,
    widened through :func:`_widen_work_like_types` so a `test-author`/
    `mock-author` leg registers as "work" activity to the generic
    per-stage dispatcher — see the ``projection_assignments`` fallback
    below. ``issue_title`` still reads ``""`` for a phantom entry that
    isn't itself in ``issues`` (genuinely unknown — not fabricated from the
    tracking issue's own title, which would be actively wrong).
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

    # #3013: effective-issue-keyed index — see docstring above. Used only to
    # fill `stage_counts`, never `stages`.
    assignments_by_effective_key: dict[tuple[str, int], list] = {}
    for a in assignments:
        if not a.repo_name:
            continue
        eff = effective_issue_number(a)
        if not eff:
            continue
        assignments_by_effective_key.setdefault((a.repo_name, eff), []).append(a)

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

    # #3013: also union in the effective-key index so an acceptance slice
    # whose only board rows are booked to the tracking issue (nothing keyed
    # to the slice's own issue_number, e.g. never separately synced) still
    # gets a projection row for stage_counts to land in.
    keys = set(is_closed_by_key) | set(assignments_by_key) | set(assignments_by_effective_key)

    uat_enabled_by_repo: dict[str, bool] = {}

    result: list[dict[str, Any]] = []
    for repo_name, issue_number in keys:
        key = (repo_name, issue_number)
        issue_assignments = assignments_by_key.get(key, [])
        effective_assignments = assignments_by_effective_key.get(key, [])
        # Review fix (#3013): a phantom entry (see docstring) has no
        # raw-keyed rows at all, so falling back to the effective-keyed list
        # here — instead of leaving `stages` computed from `[]` — keeps
        # `stages`/`acceptance`/`has_approved_review` consistent with the
        # non-zero `stage_counts` this same entry reports below. Widened
        # through `_widen_work_like_types` so a `test-author`/`mock-author`
        # leg (WORK_LIKE_TYPES, not literal `type="work"`) registers with
        # the generic per-stage dispatcher's strict `type` check the same
        # way `_leg_count_for_stage` already widens it for `stage_counts`.
        # A no-op for every ordinary issue: `issue_assignments or ...` only
        # reaches the fallback when the raw list is empty.
        projection_assignments = issue_assignments or _widen_work_like_types(effective_assignments)
        merge_entry = merge_by_key.get(key)
        if repo_name not in uat_enabled_by_repo:
            uat_enabled_by_repo[repo_name] = repo_has_uat_preview(repo_name, config)
        uat_enabled = uat_enabled_by_repo[repo_name]
        entry = compute_issue_projection(
            projection_assignments,
            merge_entry,
            is_closed=is_closed_by_key.get(key, False),
            require_plan=require_plan,
            default_gates=default_gates,
            ci_store=ci_store,
            uat_enabled=uat_enabled,
            uat_preview_url=(
                uat_preview_url_for(repo_name, issue_number, merge_entry, config)
                if uat_enabled
                else None
            ),
            stage_count_assignments=effective_assignments,
        )
        entry["repo_name"] = repo_name
        entry["issue_number"] = issue_number
        entry["issue_title"] = issue_title_by_key.get(key, "")
        result.append(entry)

    result.sort(key=lambda e: (e["repo_name"], e["issue_number"]))
    return result
