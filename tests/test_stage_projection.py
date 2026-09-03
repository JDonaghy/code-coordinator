"""Tests for coord/stage_projection.py (#550).

Truth-table cases mirror the Rust behaviour documented in
``tui/src/app/pipeline.rs`` (``stage_status_for``, ``merge_stage_status_for``,
``test_stage_status_for``, ``issue_has_any_approved_review``) so both sides
encode the same expected outcomes for the same inputs.
"""

from __future__ import annotations

from coord import stage_projection as sp
from coord.merge_queue import QueuedMerge
from coord.models import Assignment, Repo


class _StubConfig:
    """Minimal ``config.repo(name)`` duck-type for the #2951 UAT-projection
    tests — ``stage_projection.py`` never touches anything else on
    ``config``, matching ``coord.merge_queue._uat_repo_for``'s own contract.
    """

    def __init__(self, repos: list[Repo]) -> None:
        self._by_name = {r.name: r for r in repos}

    def repo(self, name):
        return self._by_name.get(name)


def _work(**kw) -> Assignment:
    base = dict(
        machine_name="laptop",
        repo_name="api",
        issue_number=1,
        issue_title="t",
        type="work",
        status="done",
    )
    base.update(kw)
    return Assignment(**base)


def _review(**kw) -> Assignment:
    base = dict(
        machine_name="laptop",
        repo_name="api",
        issue_number=1,
        issue_title="t",
        type="review",
        status="done",
    )
    base.update(kw)
    return Assignment(**base)


def _entry(**kw) -> QueuedMerge:
    base = dict(
        assignment_id="w1",
        repo_name="api",
        repo_github="acme/api",
        branch="issue-1-impl",
        target_branch="main",
        issue_number=1,
        issue_title="t",
    )
    base.update(kw)
    return QueuedMerge(**base)


# ── stage_status_for: generic stage ─────────────────────────────────────────


def test_stage_status_running_is_active():
    a = [_review(status="running", dispatched_at=1.0)]
    assert sp.stage_status_for(a, "review", stage_names=["work", "review"], is_closed=False, require_plan=False) == sp.ACTIVE


def test_stage_status_review_approve_is_done():
    a = [_review(status="done", review_verdict="approve", dispatched_at=1.0)]
    assert sp.stage_status_for(a, "review", stage_names=["work", "review"], is_closed=False, require_plan=False) == sp.DONE


def test_stage_status_review_request_changes_is_failed():
    a = [_review(status="done", review_verdict="request-changes", dispatched_at=1.0)]
    assert sp.stage_status_for(a, "review", stage_names=["work", "review"], is_closed=False, require_plan=False) == sp.FAILED


def test_stage_status_review_no_verdict_is_failed():
    """#812: a terminal done row with no verdict is a dead end, not in-progress."""
    a = [_review(status="done", review_verdict=None, dispatched_at=1.0)]
    assert sp.stage_status_for(a, "review", stage_names=["work", "review"], is_closed=False, require_plan=False) == sp.FAILED


def test_stage_status_review_finalizing_is_active_not_failed():
    """#1566: a review agent that finished but hasn't had its verdict parsed
    + posted yet (`coord notify`'s separate, slower step) must render as
    still in-progress — NOT as the #812 "terminal done, no verdict" dead
    end, which is indistinguishable from a genuinely dropped verdict."""
    a = [_review(status="finalizing", review_verdict=None, dispatched_at=1.0)]
    assert sp.stage_status_for(a, "review", stage_names=["work", "review"], is_closed=False, require_plan=False) == sp.ACTIVE


def test_stage_status_no_assignment_open_issue_is_pending():
    assert sp.stage_status_for([], "review", stage_names=["work", "review"], is_closed=False, require_plan=False) == sp.PENDING


def test_stage_status_no_assignment_closed_issue_is_skipped():
    assert sp.stage_status_for([], "review", stage_names=["work", "review"], is_closed=True, require_plan=False) == sp.SKIPPED


def test_stage_status_stale_when_upstream_redispatched():
    """#193: a Done verdict against an older revision renders Stale."""
    a = [
        _work(status="done", dispatched_at=1.0),
        _review(status="done", review_verdict="approve", dispatched_at=2.0),
        _work(status="running", dispatched_at=5.0),  # re-dispatched after review
    ]
    # "work" is upstream of "review" in stage_names.
    assert sp.stage_status_for(
        a, "review", stage_names=["work", "review"], is_closed=False, require_plan=False
    ) == sp.STALE


# ── pipeline_stage_names ─────────────────────────────────────────────────────


def test_pipeline_stage_names_includes_merge_for_default_gate_order():
    """#1429: restore "merge" to the per-issue stage-name ordering as a
    read-only observation badge — #738 retired the per-issue *box* with its
    Go/dispatch affordance, but the name ordering itself should still surface
    where an issue sits, including a merged/queued/failed Merge stage."""
    assert sp.pipeline_stage_names(["test", "review", "merge"]) == [
        "work",
        "test",
        "review",
        "merge",
    ]


def test_pipeline_stage_names_excludes_work_and_plan_from_gate_list():
    """"work"/"plan" are prepended separately, so a duplicate in
    ``default_gates`` must not double up."""
    assert sp.pipeline_stage_names(["plan", "work", "review", "merge"]) == [
        "work",
        "review",
        "merge",
    ]


def test_merged_issue_projects_merge_stage_as_done():
    """A merged issue's Pipeline row must show the Merge badge as Done, not
    silently omit it — that's the whole point of #1429: an issue stalled
    after review (no merge) must be visually distinguishable from one that
    actually merged."""
    a = [_work(assignment_id="w1", status="done", test_state="passed", dispatched_at=1.0)]
    entry = _entry(state="merged")
    out = sp.compute_issue_projection(
        a,
        entry,
        is_closed=False,
        require_plan=False,
        default_gates=["test", "review", "merge"],
    )
    assert out["stages"]["merge"] == sp.DONE
    assert list(out["stages"].keys())[:4] == ["work", "test", "review", "merge"]


# ── merge_stage_status_for ──────────────────────────────────────────────────


def test_merge_stage_active_conflict_fix_wins():
    a = [Assignment(machine_name="m", repo_name="api", issue_number=1, issue_title="t", type="conflict-fix", status="running")]
    entry = _entry(state="failed")
    assert sp.merge_stage_status_for(a, entry, is_closed=False) == sp.ACTIVE


def test_merge_stage_merged_entry_is_done():
    entry = _entry(state="merged")
    assert sp.merge_stage_status_for([], entry, is_closed=False) == sp.DONE


def test_merge_stage_open_entry_is_active():
    entry = _entry(state="open")
    assert sp.merge_stage_status_for([], entry, is_closed=False) == sp.ACTIVE


def test_merge_stage_human_required_is_failed():
    entry = _entry(state="human_required")
    assert sp.merge_stage_status_for([], entry, is_closed=False) == sp.FAILED


def test_merge_stage_conflict_is_failed():
    """#919 review: a real GitHub merge conflict is a genuine
    resting/terminal merge_queue state (coord/merge_queue.py). Before this
    fix `conflict` fell through to the CI-check / pending fallback below —
    and a conflicting PR reliably reports zero CI checks (merge_queue.py
    #1877), so the Merge stage box rendered Pending with a lit one-click
    [Go] for an item that could never actually merge."""
    entry = _entry(state="conflict")
    assert sp.merge_stage_status_for([], entry, is_closed=False) == sp.FAILED


def test_merge_stage_skipped_is_failed_for_open_issue():
    """#919 review (round 2): `skipped` is the third state
    ``_state_to_plan_status`` folds into PLAN_NEEDS_ATTENTION, and
    ``_RESOLVE_TERMINAL_STATES`` treats it as terminal alongside "merged" —
    a superseded row on a still-open issue never self-resolves. Reporting
    PENDING lit the one-click [Go] on the Merge box for an item
    ``pipeline_merge_state()`` already refuses to dispatch."""
    entry = _entry(state="skipped")
    assert sp.merge_stage_status_for([], entry, is_closed=False) == sp.FAILED


def test_merge_stage_skipped_row_does_not_mask_merged_work_assignment():
    """The flip side: a superseded ("skipped") row routinely survives next
    to the *successful* attempt for the same issue, whose own row the
    reconcile tick then prunes (#775). The merged work assignment must still
    win — a finished issue must not be painted red."""
    a = [_work(status="merged")]
    entry = _entry(state="skipped")
    assert sp.merge_stage_status_for(a, entry, is_closed=False) == sp.DONE


def test_merge_stage_skipped_row_on_closed_issue_is_skipped():
    """A closed issue with only a superseded row reads SKIPPED (settled),
    not FAILED — there is no remedial action left to take."""
    entry = _entry(state="skipped")
    assert sp.merge_stage_status_for([], entry, is_closed=True) == sp.SKIPPED


def test_merge_stage_pruned_entry_falls_back_to_merged_work_assignment():
    """#775: the queue row can be pruned after the work assignment flips to
    status='merged' — that's still sufficient evidence Merge is Done."""
    a = [_work(status="merged")]
    assert sp.merge_stage_status_for(a, None, is_closed=False) == sp.DONE


def test_merge_stage_merged_pr_helper_for_tracking_issue_is_not_done():
    """#1142: a merged `coord pr` helper (type="pr-helper", #1142) tied to a
    milestone tracking issue must NOT be mistaken for that issue's own
    merged work — regression for epic #1117 showing "Done" prematurely from
    a merged test-author/mock-author PR-opening helper whose issue_number is
    the tracking issue, not something it resolves."""
    a = [
        Assignment(
            machine_name="m",
            repo_name="api",
            issue_number=1117,
            issue_title="[test-author] ms-37 acceptance suite",
            type="pr-helper",
            status="merged",
            branch="issue-1117-test-author-ms-37-acceptance-suite",
        )
    ]
    assert sp.merge_stage_status_for(a, None, is_closed=False) == sp.PENDING


def test_merge_stage_no_entry_open_issue_is_pending():
    assert sp.merge_stage_status_for([], None, is_closed=False) == sp.PENDING


def test_merge_stage_no_entry_closed_issue_is_skipped():
    assert sp.merge_stage_status_for([], None, is_closed=True) == sp.SKIPPED


# ── test_stage_status_for ───────────────────────────────────────────────────


def test_test_stage_work_not_done_is_pending():
    a = [_work(status="running")]
    assert sp.test_stage_status_for(a, is_closed=False, require_plan=False) == sp.PENDING


def test_test_stage_passed_verdict_is_done():
    a = [_work(status="done", test_state="passed")]
    assert sp.test_stage_status_for(a, is_closed=False, require_plan=False) == sp.DONE


def test_test_stage_failed_verdict_is_failed():
    a = [_work(status="done", test_state="failed")]
    assert sp.test_stage_status_for(a, is_closed=False, require_plan=False) == sp.FAILED


def test_test_stage_contested_verdict_is_failed():
    """#2579: "contested" (coord.notify.TEST_STATE_CONTESTED) — an
    independent #2464 re-run refuted a pass claim whose review had already
    approved it. Deliberately distinct from the literal "failed" string so
    no automatic fix-dispatch door mistakes it for an ordinary Test-stage
    failure, but by this module's own #1672 rule it IS a statement about the
    branch, so it must render as a red Failed badge here — not fall through
    to PENDING, which would look like nothing had happened yet."""
    a = [_work(status="done", test_state="contested")]
    assert sp.test_stage_status_for(a, is_closed=False, require_plan=False) == sp.FAILED


def test_test_stage_active_smoke_session_overrides_prior_pass():
    """#585: an in-flight manual smoke session keeps Test Active even over a
    prior passed verdict."""
    a = [
        _work(status="done", test_state="passed", dispatched_at=1.0),
        Assignment(machine_name="m", repo_name="api", issue_number=1, issue_title="t", type="smoke", status="running"),
    ]
    assert sp.test_stage_status_for(a, is_closed=False, require_plan=False) == sp.ACTIVE


def test_test_stage_running_verdict_is_active():
    """#1395: an unattended driver (scripts/drive-issue.sh) that runs the
    suite locally has no `type="smoke"` assignment for
    `_has_active_smoke_session` to catch — it sets `test_state="running"`
    directly on the work row instead, and this must read Active (not
    Pending, indistinguishable from "nothing happening yet")."""
    a = [_work(status="done", test_state="running")]
    assert sp.test_stage_status_for(a, is_closed=False, require_plan=False) == sp.ACTIVE


def test_test_stage_running_verdict_never_satisfies_merge_or_review_gates():
    """#1395: "running" must fail closed everywhere a terminal verdict is
    required — it is emphatically not one."""
    from coord.merge_queue import has_smoke_verdict
    from coord.models import Board

    work = _work(status="done", test_state="running", assignment_id="w1")
    entry = _entry(assignment_id="w1")
    board = Board(completed=[work])
    assert has_smoke_verdict(entry, board) is False


def test_test_stage_bounce_fix_work_inherits_prior_passed_verdict():
    """#310: a bounce-created fix-work assignment with empty test_state
    doesn't strand Test at Pending — the most recent assignment *carrying* a
    verdict wins."""
    a = [
        _work(status="done", test_state="passed", dispatched_at=1.0),
        _work(status="done", test_state=None, dispatched_at=2.0, assignment_id="fix1"),
    ]
    assert sp.test_stage_status_for(a, is_closed=False, require_plan=False) == sp.DONE


def test_test_stage_no_verdict_no_work_yet_running_is_pending_not_skipped():
    assert sp.test_stage_status_for([], is_closed=False, require_plan=False) == sp.PENDING


def test_test_stage_no_work_closed_issue_is_skipped():
    assert sp.test_stage_status_for([], is_closed=True, require_plan=False) == sp.SKIPPED


# ── acceptance_stage_status_for / acceptance_progress_for (#932) ────────────


def test_acceptance_stage_no_verdict_yet_is_skipped_not_pending():
    """Unlike Test, an issue with no acceptance suite authored yet (no
    `acceptance record` has ever run) reads SKIPPED — it isn't a gate every
    issue must clear, only oracle-loop milestones' issues."""
    a = [_work(status="done")]
    assert sp.acceptance_stage_status_for(a) == sp.SKIPPED
    assert sp.acceptance_progress_for(a) is None


def test_acceptance_stage_passed_verdict_is_done():
    a = [_work(status="done", acceptance_state="passed")]
    assert sp.acceptance_stage_status_for(a) == sp.DONE


def test_acceptance_stage_failed_verdict_is_failed():
    a = [_work(status="done", acceptance_state="failed")]
    assert sp.acceptance_stage_status_for(a) == sp.FAILED


def test_acceptance_stage_latest_by_dispatch_wins():
    a = [
        _work(status="done", acceptance_state="failed", dispatched_at=1.0),
        _work(status="done", acceptance_state="passed", dispatched_at=2.0, assignment_id="fix1"),
    ]
    assert sp.acceptance_stage_status_for(a) == sp.DONE


def test_acceptance_progress_reports_partial_green():
    """The illustrative example from the issue: '3/7 acceptance green' is
    reporting, not a fail verdict — the box itself is DONE only when this
    issue's own scoped slice is fully green (build_verdict's `green`), the
    fractional count is separate context surfaced alongside it."""
    a = [_work(status="done", acceptance_state="failed", acceptance_total=7, acceptance_passed=3)]
    assert sp.acceptance_progress_for(a) == {"passed": 3, "total": 7}
    assert sp.acceptance_stage_status_for(a) == sp.FAILED


def test_acceptance_progress_none_when_counts_predate_932():
    a = [_work(status="done", acceptance_state="passed")]
    assert sp.acceptance_progress_for(a) is None


def test_compute_issue_projection_includes_acceptance_box():
    a = [_work(status="done", acceptance_state="passed", acceptance_total=5, acceptance_passed=5)]
    out = sp.compute_issue_projection(
        a, None, is_closed=False, require_plan=False, default_gates=["test", "review", "merge"],
    )
    assert out["stages"]["acceptance"] == sp.DONE
    assert out["acceptance_progress"] == {"passed": 5, "total": 5}


def test_compute_issue_projection_stage_counts_has_same_keys_as_stages():
    """Review fix (#3013): `stage_counts` must carry every key `stages`
    does — including `acceptance`, which is added to `stages`
    unconditionally outside `names`/`default_gates` and was previously
    dropped from `stage_counts` entirely, contradicting the `/board` schema
    description ('Same key set as `stages`') and risking a `KeyError` for
    any client indexing `stage_counts` by every key in `stages`."""
    a = [_work(status="done", acceptance_state="passed", acceptance_total=5, acceptance_passed=5)]
    out = sp.compute_issue_projection(
        a, None, is_closed=False, require_plan=False, default_gates=["test", "review", "merge"],
    )
    assert set(out["stage_counts"]) == set(out["stages"])
    assert out["stage_counts"]["acceptance"] == 1


def test_compute_issue_projection_stage_counts_acceptance_counts_verdict_legs():
    """Each `type="work"` row carrying a recorded `acceptance_state` is a
    separate acceptance-verdict leg — mirrors the `test`/`merge` special
    cases: no dedicated assignment type exists to count directly."""
    a = [
        _work(assignment_id="w1", status="done", acceptance_state="failed", dispatched_at=1.0),
        _work(assignment_id="w2", status="done", acceptance_state="passed", dispatched_at=2.0),
        _work(assignment_id="w3", status="done", dispatched_at=3.0),  # no verdict yet
    ]
    out = sp.compute_issue_projection(
        a, None, is_closed=False, require_plan=False, default_gates=["test", "review", "merge"],
    )
    assert out["stage_counts"]["acceptance"] == 2


# ── compute_issue_projection: stage_counts (#3013) ──────────────────────────


def test_compute_issue_projection_stage_counts_counts_separate_legs():
    """A stage re-dispatched N times must report N in stage_counts, not
    fold into the single status string the way `stages` does."""
    a = [
        _work(assignment_id="w1", status="failed", dispatched_at=1.0),
        _work(assignment_id="w2", status="failed", dispatched_at=2.0),
        _work(assignment_id="w3", status="done", test_state="passed", dispatched_at=3.0),
        _review(
            assignment_id="r1", review_of_assignment_id="w3",
            review_verdict="request-changes", dispatched_at=4.0,
        ),
        _review(
            assignment_id="r2", review_of_assignment_id="w3",
            review_verdict="approve", dispatched_at=5.0,
        ),
    ]
    out = sp.compute_issue_projection(
        a, None, is_closed=False, require_plan=False, default_gates=["test", "review", "merge"],
    )
    assert out["stage_counts"]["work"] == 3
    assert out["stage_counts"]["review"] == 2


def test_compute_issue_projection_stage_counts_single_leg_is_one():
    a = [_work(assignment_id="w1", status="done", dispatched_at=1.0)]
    out = sp.compute_issue_projection(
        a, None, is_closed=False, require_plan=False, default_gates=["test", "review", "merge"],
    )
    assert out["stage_counts"]["work"] == 1
    assert out["stage_counts"]["review"] == 0


def test_compute_issue_projection_stage_counts_test_counts_smoke_legs():
    """The Test stage has no `type="test"` assignment — its dispatched
    worker is `type="smoke"` (coord.smoke) — so the count must key off
    that, not a literal type match."""
    a = [
        _work(assignment_id="w1", status="done", dispatched_at=1.0),
        _work(assignment_id="s1", type="smoke", status="done", test_state="failed", dispatched_at=2.0),
        _work(assignment_id="s2", type="smoke", status="done", test_state="passed", dispatched_at=3.0),
    ]
    out = sp.compute_issue_projection(
        a, None, is_closed=False, require_plan=False, default_gates=["test", "review", "merge"],
    )
    assert out["stage_counts"]["test"] == 2


def test_compute_issue_projection_stage_counts_merge_counts_conflict_fix_legs():
    """Repeated landing attempts show up as `type="conflict-fix"` legs
    (#241) — there is no `type="merge"` assignment to count directly."""
    a = [
        _work(assignment_id="w1", status="done", dispatched_at=1.0),
        _work(assignment_id="cf1", type="conflict-fix", status="done", dispatched_at=2.0),
        _work(assignment_id="cf2", type="conflict-fix", status="failed", dispatched_at=3.0),
    ]
    out = sp.compute_issue_projection(
        a, None, is_closed=False, require_plan=False, default_gates=["test", "review", "merge"],
    )
    assert out["stage_counts"]["merge"] == 2


def test_compute_issue_projection_stage_counts_work_includes_test_author_legs():
    """#3013's coord-portal#164 example: an oracle-loop acceptance slice's
    repeated `test-author` legs must count as Work legs — a strict
    `type == "work"` match would never see them."""
    a = [
        _work(assignment_id="ta1", type="test-author", status="failed", dispatched_at=1.0),
        _work(assignment_id="ta2", type="test-author", status="failed", dispatched_at=2.0),
        _work(assignment_id="ta3", type="test-author", status="failed", dispatched_at=3.0),
        _work(assignment_id="ta4", type="test-author", status="done", dispatched_at=4.0),
    ]
    out = sp.compute_issue_projection(
        a, None, is_closed=False, require_plan=False, default_gates=["test", "review", "merge"],
    )
    assert out["stage_counts"]["work"] == 4


# ── issue_has_any_approved_review ───────────────────────────────────────────


def test_approved_review_linked_to_work_id():
    a = [
        _work(assignment_id="w1", status="done"),
        _review(review_of_assignment_id="w1", review_verdict="approve"),
    ]
    assert sp.issue_has_any_approved_review(a) is True


def test_approved_review_self_stamped_on_work():
    """#331: verdict stamped directly on the work row (no separate review worker)."""
    a = [_work(assignment_id="w1", status="done", review_verdict="approve")]
    assert sp.issue_has_any_approved_review(a) is True


def test_self_stamped_approval_is_also_reflected_in_review_stage_badge():
    """#2085 (non-blocking finding): before this fix, a #331 self-approval
    verdict (stamped directly on a work row, no separate ``type="review"``
    assignment dispatched) was counted by `issue_has_any_approved_review`
    but INVISIBLE to `stage_status_for`'s "review" stage — which only ever
    scans `type == "review"` rows — so a projection could read
    `has_approved_review: True` next to `stages["review"] == PENDING` at
    once, the same READY-vs-refused self-contradiction #2085 is about, via a
    different path. The two must agree: both now share
    `_review_verdict_events`.
    """
    a = [_work(assignment_id="w1", status="done", review_verdict="approve", dispatched_at=1.0)]
    assert sp.issue_has_any_approved_review(a) is True
    assert sp.stage_status_for(
        a, "review", stage_names=["work", "review"], is_closed=False, require_plan=False,
    ) == sp.DONE


def test_self_stamped_request_changes_is_also_reflected_in_review_stage_badge():
    """#2085 (non-blocking finding), the mirror case: a self-stamped
    ``request-changes`` must read FAILED on the "review" stage badge too,
    not just count against `issue_has_any_approved_review`."""
    a = [_work(assignment_id="w1", status="done", review_verdict="request-changes", dispatched_at=1.0)]
    assert sp.issue_has_any_approved_review(a) is False
    assert sp.stage_status_for(
        a, "review", stage_names=["work", "review"], is_closed=False, require_plan=False,
    ) == sp.FAILED


def test_approved_review_self_stamped_on_pr_helper_does_not_count():
    """#1142: a review verdict stamped on a `pr-helper`-type row (a `coord
    pr` helper for a non-closes-issue original) must not count as an
    approved review of the *tracking issue's own* work — same
    CLOSES_ISSUE_TYPES rationale as merge_stage_status_for above."""
    a = [
        Assignment(
            machine_name="m", repo_name="api", issue_number=1117, issue_title="t",
            assignment_id="pr-helper-1", type="pr-helper", status="done",
            review_verdict="approve",
        )
    ]
    assert sp.issue_has_any_approved_review(a) is False


def test_approved_review_seed_work_id_covers_pruned_row():
    """#292: entry is keyed to a work id whose row has been pruned from the
    board — seed_work_id still finds an approval linked to it."""
    a = [_review(review_of_assignment_id="pruned-w1", review_verdict="approve")]
    assert sp.issue_has_any_approved_review(a, seed_work_id="pruned-w1") is True


def test_no_approved_review_returns_false():
    a = [_work(assignment_id="w1", status="done")]
    assert sp.issue_has_any_approved_review(a) is False


def test_request_changes_is_not_approved():
    a = [
        _work(assignment_id="w1", status="done"),
        _review(review_of_assignment_id="w1", review_verdict="request-changes"),
    ]
    assert sp.issue_has_any_approved_review(a) is False


# ── uat_stage_status_for / UAT projection (#2951) ───────────────────────────


def test_uat_stage_work_not_done_is_pending():
    a = [_work(status="running")]
    assert sp.uat_stage_status_for(a, is_closed=False, require_plan=False) == sp.PENDING


def test_uat_stage_no_work_closed_issue_is_skipped():
    assert sp.uat_stage_status_for([], is_closed=True, require_plan=False) == sp.SKIPPED


def test_uat_stage_work_done_no_verdict_is_pending():
    a = [_work(status="done", dispatched_at=1.0)]
    assert sp.uat_stage_status_for(a, is_closed=False, require_plan=False) == sp.PENDING


def test_uat_stage_passed_verdict_is_done():
    a = [_work(status="done", dispatched_at=1.0, uat_state="passed")]
    assert sp.uat_stage_status_for(a, is_closed=False, require_plan=False) == sp.DONE


def test_uat_stage_failed_verdict_is_failed():
    a = [_work(status="done", dispatched_at=1.0, uat_state="failed", uat_reason="visual defect")]
    assert sp.uat_stage_status_for(a, is_closed=False, require_plan=False) == sp.FAILED


def test_uat_stage_latest_by_dispatch_wins():
    """A bounce/fix round's fresh work assignment resets the verdict,
    mirroring `test_stage_status_for`'s own latest-by-dispatch rule."""
    a = [
        _work(assignment_id="w1", status="done", dispatched_at=1.0, uat_state="failed"),
        _work(assignment_id="w2", status="done", dispatched_at=2.0, uat_state="passed"),
    ]
    assert sp.uat_stage_status_for(a, is_closed=False, require_plan=False) == sp.DONE


def test_stage_status_for_dispatches_uat_to_dedicated_function():
    """#2951 cause 2: before this branch existed, `stage_status_for` matched
    on `type == "uat"`, which no assignment is ever created with, so the
    generic path could never see a verdict stamped on the work row."""
    a = [_work(status="done", dispatched_at=1.0, uat_state="passed")]
    assert sp.stage_status_for(
        a, "uat", stage_names=["work", "uat"], is_closed=False, require_plan=False,
    ) == sp.DONE


# ── pipeline_stage_names / issue_stage_names: uat_enabled (#2951) ──────────


def test_pipeline_stage_names_hides_uat_by_default():
    """The two-part opt-in's fleet-wide half alone (`"uat" in default_gates`)
    is NOT sufficient — `uat_enabled` defaults to False so a caller that
    forgets to resolve the per-repo half fails toward "hidden", not toward
    a fleet-wide false badge (#2951 cause 1)."""
    assert sp.pipeline_stage_names(["test", "review", "uat", "merge"]) == [
        "work", "test", "review", "merge",
    ]


def test_pipeline_stage_names_shows_uat_when_enabled():
    assert sp.pipeline_stage_names(["test", "review", "uat", "merge"], uat_enabled=True) == [
        "work", "test", "review", "uat", "merge",
    ]


def test_pipeline_stage_names_hides_uat_when_not_in_default_gates_even_if_enabled():
    """The fleet-wide half still gates too — a repo with `uat_preview`
    configured must not show the badge if "uat" was never added to
    `pipeline.default_gates` at all."""
    assert sp.pipeline_stage_names(["test", "review", "merge"], uat_enabled=True) == [
        "work", "test", "review", "merge",
    ]


def test_issue_stage_names_threads_uat_enabled():
    assert sp.issue_stage_names([], ["test", "uat", "merge"], uat_enabled=True) == [
        "work", "test", "uat", "merge",
    ]
    assert sp.issue_stage_names([], ["test", "uat", "merge"], uat_enabled=False) == [
        "work", "test", "merge",
    ]


# ── compute_issue_projection: uat badge visibility (#2951) ─────────────────


def test_compute_issue_projection_hides_uat_badge_when_repo_not_opted_in():
    a = [_work(status="done", dispatched_at=1.0)]
    out = sp.compute_issue_projection(
        a, None, is_closed=False, require_plan=False,
        default_gates=["test", "review", "uat", "merge"],
        uat_enabled=False,
    )
    assert "uat" not in out["stages"]
    assert out["uat_preview_url"] is None


def test_compute_issue_projection_shows_uat_badge_states_when_enabled():
    default_gates = ["test", "review", "uat", "merge"]

    pending = sp.compute_issue_projection(
        [_work(status="done", dispatched_at=1.0)], None,
        is_closed=False, require_plan=False, default_gates=default_gates, uat_enabled=True,
    )
    assert pending["stages"]["uat"] == sp.PENDING

    done = sp.compute_issue_projection(
        [_work(status="done", dispatched_at=1.0, uat_state="passed")], None,
        is_closed=False, require_plan=False, default_gates=default_gates, uat_enabled=True,
    )
    assert done["stages"]["uat"] == sp.DONE

    failed = sp.compute_issue_projection(
        [_work(status="done", dispatched_at=1.0, uat_state="failed")], None,
        is_closed=False, require_plan=False, default_gates=default_gates, uat_enabled=True,
    )
    assert failed["stages"]["uat"] == sp.FAILED


def test_compute_issue_projection_carries_preview_url_only_when_enabled():
    a = [_work(status="done", dispatched_at=1.0)]
    enabled = sp.compute_issue_projection(
        a, None, is_closed=False, require_plan=False,
        default_gates=["test", "uat", "merge"], uat_enabled=True,
        uat_preview_url="https://issue-1.example.pages.dev/",
    )
    assert enabled["uat_preview_url"] == "https://issue-1.example.pages.dev/"

    disabled = sp.compute_issue_projection(
        a, None, is_closed=False, require_plan=False,
        default_gates=["test", "uat", "merge"], uat_enabled=False,
        uat_preview_url="https://issue-1.example.pages.dev/",
    )
    assert disabled["uat_preview_url"] is None


# ── compute_board_stage_projection: repo-scoped uat opt-in (#2951) ─────────


def test_board_projection_shows_uat_badge_only_for_opted_in_repo():
    config = _StubConfig([
        Repo(name="api", github="acme/api", uat_preview="https://{branch}.example.dev/"),
        Repo(name="other", github="acme/other"),  # uat_preview unset — not opted in
    ])
    issues = [
        {"repo_name": "api", "number": 1, "title": "t", "state": "open"},
        {"repo_name": "other", "number": 2, "title": "t2", "state": "open"},
    ]
    assignments = [
        _work(repo_name="api", issue_number=1, status="done", dispatched_at=1.0, uat_state="passed"),
        _work(repo_name="other", issue_number=2, status="done", dispatched_at=1.0),
    ]
    out = sp.compute_board_stage_projection(
        issues=issues,
        assignments=assignments,
        merge_queue_items=[],
        default_gates=["test", "review", "uat", "merge"],
        config=config,
    )
    by_issue = {(e["repo_name"], e["issue_number"]): e for e in out}
    assert by_issue[("api", 1)]["stages"]["uat"] == sp.DONE
    assert "uat" not in by_issue[("other", 2)]["stages"]
    assert by_issue[("other", 2)]["uat_preview_url"] is None


def test_board_projection_hides_uat_badge_for_every_repo_without_config():
    """#2951: `config=None` (the default for any caller that hasn't wired
    it through) must not regress to the old fleet-wide behaviour — no repo
    is ever treated as opted in without a config to confirm it."""
    issues = [{"repo_name": "api", "number": 1, "title": "t", "state": "open"}]
    assignments = [_work(status="done", dispatched_at=1.0, uat_state="passed")]
    out = sp.compute_board_stage_projection(
        issues=issues,
        assignments=assignments,
        merge_queue_items=[],
        default_gates=["test", "review", "uat", "merge"],
    )
    assert "uat" not in out[0]["stages"]


def test_board_projection_hides_uat_when_gate_not_in_default_gates():
    """Guards the fleet-wide half: a repo WITH `uat_preview` configured
    still shows nothing when "uat" was never added to `default_gates`."""
    config = _StubConfig([
        Repo(name="api", github="acme/api", uat_preview="https://preview.example.dev/"),
    ])
    issues = [{"repo_name": "api", "number": 1, "title": "t", "state": "open"}]
    assignments = [_work(status="done", dispatched_at=1.0, uat_state="passed")]
    out = sp.compute_board_stage_projection(
        issues=issues,
        assignments=assignments,
        merge_queue_items=[],
        default_gates=["test", "review", "merge"],
        config=config,
    )
    assert "uat" not in out[0]["stages"]


def test_board_projection_resolves_preview_url_for_opted_in_issue():
    config = _StubConfig([
        Repo(name="api", github="acme/api", uat_preview="https://{branch}.example.dev/"),
    ])
    issues = [{"repo_name": "api", "number": 1, "title": "t", "state": "open"}]
    assignments = [_work(status="done", dispatched_at=1.0)]
    mq_items = [_entry(state="open", branch="issue-1-impl")]
    out = sp.compute_board_stage_projection(
        issues=issues,
        assignments=assignments,
        merge_queue_items=mq_items,
        default_gates=["test", "review", "uat", "merge"],
        config=config,
    )
    assert out[0]["uat_preview_url"] == "https://issue-1-impl.example.dev/"


def test_repo_has_uat_preview_true_for_uat_live_preview_only_repo():
    """#2948: `repo_has_uat_preview` must answer "opted in" the same way
    `coord.merge_queue.requires_uat` does — `uat_preview` OR
    `uat_live_preview`, either one alone sufficient. A repo with no
    templatable preview host (natal-chart's actual situation) opts in via
    `uat_live_preview` alone; before this fix the badge never lit up for
    it even though `coord merge` was enforcing the gate."""
    config = _StubConfig([
        Repo(name="api", github="acme/api", uat_live_preview=True),
    ])
    assert sp.repo_has_uat_preview("api", config) is True


def test_repo_has_uat_preview_false_when_neither_set():
    config = _StubConfig([Repo(name="api", github="acme/api")])
    assert sp.repo_has_uat_preview("api", config) is False


def test_uat_preview_url_for_returns_none_for_uat_live_preview_only_repo():
    """This module does no I/O (see module docstring), so it can't run the
    live GitHub-Deployment lookup `uat_live_preview` opts into — it can only
    render `uat_preview`'s override template. `None` here is the documented
    best-effort answer, not the #2948 bug (a rendered-but-dead URL)."""
    config = _StubConfig([
        Repo(name="api", github="acme/api", uat_live_preview=True),
    ])
    assert sp.uat_preview_url_for("api", 1, None, config) is None


def test_board_projection_shows_uat_badge_for_uat_live_preview_only_repo():
    """#2948: the board must surface the "uat" badge (and gate state) for a
    repo opted in via `uat_live_preview` alone, even though it can't also
    surface a clickable preview URL here (no I/O in this module) — an
    operator must see the gate is active, not a clean board that then
    silently refuses to merge."""
    config = _StubConfig([
        Repo(name="api", github="acme/api", uat_live_preview=True),
    ])
    issues = [{"repo_name": "api", "number": 1, "title": "t", "state": "open"}]
    assignments = [_work(status="done", dispatched_at=1.0, uat_state="passed")]
    out = sp.compute_board_stage_projection(
        issues=issues,
        assignments=assignments,
        merge_queue_items=[],
        default_gates=["test", "review", "uat", "merge"],
        config=config,
    )
    assert out[0]["stages"]["uat"] == sp.DONE
    assert out[0]["uat_preview_url"] is None


# ── compute_board_stage_projection ──────────────────────────────────────────


def test_compute_board_stage_projection_covers_issue_and_merge_state():
    issues = [{"repo_name": "api", "number": 1, "title": "t", "state": "open"}]
    assignments = [
        _work(assignment_id="w1", status="done", test_state="passed", dispatched_at=1.0),
        _review(review_of_assignment_id="w1", review_verdict="approve", dispatched_at=2.0),
    ]
    mq_items = [_entry(state="open")]
    out = sp.compute_board_stage_projection(
        issues=issues,
        assignments=assignments,
        merge_queue_items=mq_items,
        default_gates=["test", "review", "merge"],
    )
    assert len(out) == 1
    entry = out[0]
    assert entry["repo_name"] == "api"
    assert entry["issue_number"] == 1
    assert entry["has_approved_review"] is True
    assert entry["stages"]["work"] == sp.DONE
    assert entry["stages"]["test"] == sp.DONE
    assert entry["stages"]["review"] == sp.DONE
    assert entry["stages"]["merge"] == sp.ACTIVE


def test_compute_board_stage_projection_includes_closed_issue_with_assignments_only():
    """An issue with assignment history but absent from the issues table
    (e.g. pruned/never synced) still gets a projection, treated as open."""
    assignments = [_work(assignment_id="w1", status="done")]
    out = sp.compute_board_stage_projection(
        issues=[],
        assignments=assignments,
        merge_queue_items=[],
        default_gates=["test", "review", "merge"],
    )
    assert len(out) == 1
    assert out[0]["issue_number"] == 1


def test_merged_test_author_entry_does_not_mark_tracking_issue_merge_done():
    """#1203: a merged `test-author` merge-queue row is keyed to the
    milestone's tracking issue (#1117-style) on `issue_number` — it must not
    make the tracking issue's own Pipeline card read `merge: done` while the
    epic itself is still open and untouched."""
    issues = [{"repo_name": "api", "number": 1117, "title": "epic", "state": "open"}]
    mq_items = [
        _entry(
            assignment_id="ta1",
            issue_number=1117,
            state="merged",
            assignment_type="test-author",
        )
    ]
    out = sp.compute_board_stage_projection(
        issues=issues,
        assignments=[],
        merge_queue_items=mq_items,
        default_gates=["test", "review", "merge"],
    )
    assert len(out) == 1
    assert out[0]["issue_number"] == 1117
    assert out[0]["stages"]["merge"] == sp.PENDING


def test_merged_test_author_entry_does_not_attribute_to_child_issue():
    """#1652: #1203 stopped a merged `test-author` merge-queue row from
    greening the *tracking* issue's Merge box, but did so by re-attributing
    the row to the child issue named by the originating assignment's
    `for_issue_number` — moving the false green rather than removing it. A
    test-author/mock-author slice PR closes nothing for anyone's work (it's
    the sealed acceptance suite, docs/ORACLE_LOOP.md); the child's own work
    still has to be tested, reviewed, and merged on its own branch, so
    neither issue's merge box should read done from this row."""
    issues = [
        {"repo_name": "api", "number": 1117, "title": "epic", "state": "open"},
        {"repo_name": "api", "number": 1039, "title": "slice", "state": "open"},
    ]
    assignments = [
        _work(
            assignment_id="ta1",
            issue_number=1117,
            type="test-author",
            status="done",
            for_issue_number=1039,
        ),
    ]
    mq_items = [
        _entry(
            assignment_id="ta1",
            issue_number=1117,
            state="merged",
            assignment_type="test-author",
        )
    ]
    out = sp.compute_board_stage_projection(
        issues=issues,
        assignments=assignments,
        merge_queue_items=mq_items,
        default_gates=["test", "review", "merge"],
    )
    by_issue = {e["issue_number"]: e for e in out}
    assert by_issue[1117]["stages"]["merge"] == sp.PENDING
    assert by_issue[1039]["stages"]["merge"] == sp.PENDING


def test_merged_test_author_entry_does_not_green_child_with_own_unmerged_work():
    """#1652 regression, mirroring the live shape seen on ms-38/#1120 →
    #1122: an open child issue has its own `status="done"` (but unmerged —
    no PR, no merge-queue row) work assignment, plus a merged `test-author`
    row on the tracking issue whose originating assignment's
    `for_issue_number` points at the child. The child's own work has not
    been tested, reviewed, or merged — its Merge box must read PENDING, not
    DONE from the acceptance slice's queue row."""
    issues = [
        {"repo_name": "api", "number": 1120, "title": "epic", "state": "open"},
        {"repo_name": "api", "number": 1122, "title": "child", "state": "open"},
    ]
    assignments = [
        _work(assignment_id="w1", issue_number=1122, status="done"),
        _work(
            assignment_id="ta1",
            issue_number=1120,
            type="test-author",
            status="done",
            for_issue_number=1122,
        ),
    ]
    mq_items = [
        _entry(
            assignment_id="ta1",
            issue_number=1120,
            state="merged",
            assignment_type="test-author",
        )
    ]
    out = sp.compute_board_stage_projection(
        issues=issues,
        assignments=assignments,
        merge_queue_items=mq_items,
        default_gates=["test", "review", "merge"],
    )
    by_issue = {e["issue_number"]: e for e in out}
    assert by_issue[1122]["stages"]["merge"] == sp.PENDING


# ── compute_board_stage_projection: stage_counts epic/slice keying (#3013) ──


def test_compute_board_stage_projection_stage_counts_attributes_slice_legs_to_child():
    """#3013's coord-portal#164 example: an oracle-loop acceptance slice's
    test-author/review legs are booked (`issue_number`) to the milestone's
    tracking issue but FOR the child (`for_issue_number`) — `coord gates
    coord-portal 164` resolves them the same way. stage_counts must land on
    the CHILD's own projection entry, not the epic's — and must not
    double-count the same legs on both."""
    issues = [
        {"repo_name": "api", "number": 160, "title": "epic", "state": "open"},
        {"repo_name": "api", "number": 164, "title": "slice", "state": "open"},
    ]
    assignments = [
        _work(
            assignment_id="ta1", issue_number=160, type="test-author",
            status="failed", for_issue_number=164, dispatched_at=1.0,
        ),
        _work(
            assignment_id="ta2", issue_number=160, type="test-author",
            status="failed", for_issue_number=164, dispatched_at=2.0,
        ),
        _work(
            assignment_id="ta3", issue_number=160, type="test-author",
            status="failed", for_issue_number=164, dispatched_at=3.0,
        ),
        _work(
            assignment_id="ta4", issue_number=160, type="test-author",
            status="done", for_issue_number=164, dispatched_at=4.0,
        ),
        _review(
            assignment_id="r1", issue_number=160, review_of_assignment_id="ta4",
            for_issue_number=164, review_verdict="request-changes", dispatched_at=5.0,
        ),
        _review(
            assignment_id="r2", issue_number=160, review_of_assignment_id="ta4",
            for_issue_number=164, review_verdict="request-changes", dispatched_at=6.0,
        ),
        _review(
            assignment_id="r3", issue_number=160, review_of_assignment_id="ta4",
            for_issue_number=164, review_verdict="request-changes", dispatched_at=7.0,
        ),
        _review(
            assignment_id="r4", issue_number=160, review_of_assignment_id="ta4",
            for_issue_number=164, review_verdict="request-changes", dispatched_at=8.0,
        ),
        _review(
            assignment_id="r5", issue_number=160, review_of_assignment_id="ta4",
            for_issue_number=164, review_verdict="approve", dispatched_at=9.0,
        ),
    ]
    out = sp.compute_board_stage_projection(
        issues=issues,
        assignments=assignments,
        merge_queue_items=[],
        default_gates=["test", "review", "merge"],
    )
    by_issue = {e["issue_number"]: e for e in out}
    assert by_issue[164]["stage_counts"]["work"] == 4
    assert by_issue[164]["stage_counts"]["review"] == 5
    # The epic's own count must not also carry the slice's legs — they
    # belong on exactly one entry, not both.
    assert by_issue[160]["stage_counts"]["work"] == 0
    assert by_issue[160]["stage_counts"]["review"] == 0


def test_compute_board_stage_projection_phantom_slice_stages_match_stage_counts():
    """Review fix (#3013 blocking finding): a slice/child issue whose ONLY
    board rows are booked to the tracking issue — and which is NOT itself in
    the `issues` table yet (unlike the sibling test above, where both #160
    and #164 are already synced) — must not render a self-contradictory
    board row: every stage PENDING/SKIPPED right next to a `stage_counts`
    proving 4 work legs and 5 review legs ran. `stages` now falls back to
    the same effective-keyed rows `stage_counts` uses whenever an issue has
    zero rows keyed to its own raw `issue_number` (nothing separately
    tracked for #1652's false-green concern to clobber)."""
    issues = [
        {"repo_name": "api", "number": 160, "title": "epic", "state": "open"},
        # #164 deliberately absent — the phantom case: it only appears via
        # the effective-key union below.
    ]
    assignments = [
        _work(
            assignment_id="ta1", issue_number=160, type="test-author",
            status="failed", for_issue_number=164, dispatched_at=1.0,
        ),
        _work(
            assignment_id="ta2", issue_number=160, type="test-author",
            status="failed", for_issue_number=164, dispatched_at=2.0,
        ),
        _work(
            assignment_id="ta3", issue_number=160, type="test-author",
            status="failed", for_issue_number=164, dispatched_at=3.0,
        ),
        _work(
            assignment_id="ta4", issue_number=160, type="test-author",
            status="done", for_issue_number=164, dispatched_at=4.0,
        ),
        _review(
            assignment_id="r1", issue_number=160, review_of_assignment_id="ta4",
            for_issue_number=164, review_verdict="request-changes", dispatched_at=5.0,
        ),
        _review(
            assignment_id="r2", issue_number=160, review_of_assignment_id="ta4",
            for_issue_number=164, review_verdict="request-changes", dispatched_at=6.0,
        ),
        _review(
            assignment_id="r3", issue_number=160, review_of_assignment_id="ta4",
            for_issue_number=164, review_verdict="request-changes", dispatched_at=7.0,
        ),
        _review(
            assignment_id="r4", issue_number=160, review_of_assignment_id="ta4",
            for_issue_number=164, review_verdict="request-changes", dispatched_at=8.0,
        ),
        _review(
            assignment_id="r5", issue_number=160, review_of_assignment_id="ta4",
            for_issue_number=164, review_verdict="approve", dispatched_at=9.0,
        ),
    ]
    out = sp.compute_board_stage_projection(
        issues=issues,
        assignments=assignments,
        merge_queue_items=[],
        default_gates=["test", "review", "merge"],
    )
    by_issue = {e["issue_number"]: e for e in out}
    slice_entry = by_issue[164]

    # The counts are unaffected by this fix — same as the synced-sibling case.
    assert slice_entry["stage_counts"]["work"] == 4
    assert slice_entry["stage_counts"]["review"] == 5

    # The core fix: `stages` must no longer be a blanket PENDING default —
    # it reflects the SAME rows the counts are drawn from.
    assert slice_entry["stages"]["work"] == sp.DONE
    assert slice_entry["stages"]["review"] == sp.DONE
    assert slice_entry["has_approved_review"] is True
    # No merge signal exists for a phantom entry (no merge-queue row keys to
    # it) — PENDING here is honest (matches stage_counts["merge"] == 0), not
    # a fabricated green.
    assert slice_entry["stages"]["merge"] == sp.PENDING
    assert slice_entry["stage_counts"]["merge"] == 0

    # Genuinely unknown, not fabricated from the tracking issue's own title.
    assert slice_entry["issue_title"] == ""

    # The epic's own entry is completely unaffected (it has real raw-keyed
    # rows, so the phantom fallback never applies to it).
    assert by_issue[160]["stage_counts"]["work"] == 0
    assert by_issue[160]["stage_counts"]["review"] == 0


def test_compute_board_stage_projection_stage_counts_ordinary_issue_uses_own_key():
    """An ordinary (non-oracle-loop) issue has no `for_issue_number` — its
    stage_counts must come from its own rows, unaffected by the #3013
    effective-key indirection added for the slice case above."""
    issues = [{"repo_name": "api", "number": 1, "title": "t", "state": "open"}]
    assignments = [
        _work(assignment_id="w1", status="failed", dispatched_at=1.0),
        _work(assignment_id="w2", status="done", dispatched_at=2.0),
        _review(assignment_id="r1", review_of_assignment_id="w2", review_verdict="approve", dispatched_at=3.0),
    ]
    out = sp.compute_board_stage_projection(
        issues=issues,
        assignments=assignments,
        merge_queue_items=[],
        default_gates=["test", "review", "merge"],
    )
    entry = out[0]
    assert entry["stage_counts"]["work"] == 2
    assert entry["stage_counts"]["review"] == 1
