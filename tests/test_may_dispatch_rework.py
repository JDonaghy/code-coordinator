"""Unit tests for ``coord.drive_queue.may_dispatch_rework`` (#4133) and its
wiring into the auto-loop's own fix dispatchers (#4134), the board-doctor
dispatch sites (#4135), and conflict-fix/ci-fix/review dispatch (#4136).

#4133 is slice 1 of 5 of #4121 (the quadraui#2825 incident: a merged PR
re-dispatched a fix leg about 22 times because nothing checked the live
PR/row state before firing). The tests above the ``#4134`` marker pin the
guard itself; #4134 is slice 2, wiring it into
``coord.auto_loop._dispatch_fix`` and ``coord.auto_loop.
_dispatch_fix_for_review`` — the tests below that marker cover the wiring,
not the predicate's own logic (already exhaustive above). #4135 is slice 3,
wiring the same guard into ``coord.board_doctor._dispatch_base_sync_fix``
and ``coord.board_doctor_permissions.sweep_dispatch_fixit_leg`` — the tests
below the ``#4135`` marker cover those two sites. #4136 is slice 4, wiring
the same guard into ``coord.conflict_fix.dispatch_conflict_fix``,
``coord.ci_fix.dispatch_ci_fix`` and ``coord.review.dispatch_review`` — the
tests below the ``#4136`` marker cover those three sites, the last of the
parent issue's seven declared call sites. #4137 (sibling, out of scope
here) fixes an unrelated ``queue_diagnose`` binding.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from coord.drive_queue import (
    NON_CONVERGENCE_LATCH_MARKER,
    NON_CONVERGENCE_LATCH_THRESHOLD,
    STATE_DONE,
    STATE_WAITING,
    is_permanent_block_reason,
    may_dispatch_rework,
)
from coord.config import Config
from coord.models import Assignment, Board, Repo
from coord.review import ReviewFindings

REPO = "quadraui"
ISSUE = 2825


def _assignment(**overrides) -> Assignment:
    defaults = dict(
        machine_name="m",
        repo_name=REPO,
        issue_number=ISSUE,
        issue_title="t",
        assignment_id="a1",
        type="work",
        status="done",
        branch="issue-2825-fix",
        dispatched_at=100.0,
        review_iteration=0,
    )
    defaults.update(overrides)
    return Assignment(**defaults)


def _board(*assignments: Assignment) -> Board:
    return Board(
        repos=list(_config().repos), machines=[], active=[],
        completed=list(assignments),
    )


def _config() -> Config:
    return Config(
        repos=[Repo(name=REPO, github="org/quadraui", default_branch="main")],
        machines=[],
    )


# ── 1. drive-queue row already `done` ────────────────────────────────────────


def test_refuses_when_drive_queue_row_is_done():
    with patch(
        "coord.state.get_drive_queue_entry",
        return_value={"repo_name": REPO, "issue_number": ISSUE, "state": STATE_DONE},
    ):
        ok, reason = may_dispatch_rework(REPO, ISSUE, "fix")
    assert ok is False
    assert "done" in reason


def test_does_not_refuse_on_a_waiting_row_with_no_work_history():
    with patch(
        "coord.state.get_drive_queue_entry",
        return_value={"repo_name": REPO, "issue_number": ISSUE, "state": STATE_WAITING},
    ), patch("coord.state.build_board", return_value=_board()), patch(
        "coord.config.load", side_effect=RuntimeError("no config in test")
    ):
        ok, reason = may_dispatch_rework(REPO, ISSUE, "fix")
    assert ok is True
    assert "no work-like assignment" in reason


def test_no_row_at_all_and_no_history_reads_as_nothing_to_block():
    with patch("coord.state.get_drive_queue_entry", return_value=None), patch(
        "coord.state.build_board", return_value=_board()
    ), patch("coord.config.load", side_effect=RuntimeError("no config in test")):
        ok, _ = may_dispatch_rework(REPO, ISSUE, "fix")
    assert ok is True


def test_no_row_and_no_board_history_still_refuses_when_the_issue_is_closed_live():
    """#4133 review: `work_rows` comes from `build_board()` alone, which
    never sees a leg `coord housekeeping` has archived — proving the LIVE
    check still fires even when nothing on the board names this issue at
    all is the thing the permissive test above cannot show."""
    with patch("coord.state.get_drive_queue_entry", return_value=None), patch(
        "coord.state.build_board", return_value=_board()
    ), patch("coord.config.load", return_value=_config()), patch(
        "coord.claim.list_matching_remote_branches", return_value=[]
    ), patch("coord.github_ops.work_is_terminal", return_value=True):
        ok, reason = may_dispatch_rework(REPO, ISSUE, "fix")
    assert ok is False
    assert "closed" in reason


def test_no_row_and_no_board_history_checks_a_live_matching_branch():
    """Same shape, but the live `issue-{N}-*` lookup (`coord.claim`) finds a
    branch the board itself never recorded for this issue."""
    with patch("coord.state.get_drive_queue_entry", return_value=None), patch(
        "coord.state.build_board", return_value=_board()
    ), patch("coord.config.load", return_value=_config()), patch(
        "coord.claim.list_matching_remote_branches",
        return_value=["issue-2825-fix"],
    ), patch("coord.github_ops.work_is_terminal", return_value=True) as terminal:
        ok, reason = may_dispatch_rework(REPO, ISSUE, "fix")
    assert ok is False
    assert "merged or closed" in reason
    terminal.assert_called_with(
        "org/quadraui", ISSUE, "issue-2825-fix", trust_issue_closed=True
    )


# ── 2. PR merged or closed (live) ────────────────────────────────────────────


def test_refuses_when_the_live_pr_is_merged():
    work = _assignment()
    with patch("coord.state.get_drive_queue_entry", return_value=None), patch(
        "coord.state.build_board", return_value=_board(work)
    ), patch("coord.config.load", return_value=_config()), patch(
        "coord.github_ops.work_is_terminal", return_value=True
    ):
        ok, reason = may_dispatch_rework(REPO, ISSUE, "fix")
    assert ok is False
    assert "merged or closed" in reason


def test_refuses_when_the_live_pr_is_closed_even_if_the_row_is_still_live():
    """Mirrors the #2825 shape: the drive-queue row has not caught up to
    `done` yet, but the PR itself is already live-terminal."""
    work = _assignment()
    with patch(
        "coord.state.get_drive_queue_entry",
        return_value={"repo_name": REPO, "issue_number": ISSUE, "state": STATE_WAITING},
    ), patch("coord.state.build_board", return_value=_board(work)), patch(
        "coord.config.load", return_value=_config()
    ), patch("coord.github_ops.work_is_terminal", return_value=True):
        ok, reason = may_dispatch_rework(REPO, ISSUE, "conflict_fix")
    assert ok is False
    assert "merged or closed" in reason


# ── 3. branch head is an ancestor of base ────────────────────────────────────


def test_refuses_when_branch_head_is_an_ancestor_of_base_with_real_prior_history():
    work = _assignment(status="done")
    with patch("coord.state.get_drive_queue_entry", return_value=None), patch(
        "coord.state.build_board", return_value=_board(work)
    ), patch("coord.config.load", return_value=_config()), patch(
        "coord.github_ops.work_is_terminal", return_value=False
    ), patch(
        "coord.branch_model.resolve_base_branch_for_issue_number", return_value="main"
    ), patch("coord.github_ops.branch_commits_ahead", return_value=0):
        ok, reason = may_dispatch_rework(REPO, ISSUE, "fix")
    assert ok is False
    assert "ancestor" in reason


def test_does_not_trust_ancestry_alone_on_a_branch_with_no_terminal_history():
    """#3376/quadraui#2539: a freshly-cut, never-advanced branch also reads
    `ahead_by == 0` — that must NOT be confused with "already landed"."""
    work = _assignment(status="running")
    with patch("coord.state.get_drive_queue_entry", return_value=None), patch(
        "coord.state.build_board", return_value=_board(work)
    ), patch("coord.config.load", return_value=_config()), patch(
        "coord.github_ops.work_is_terminal", return_value=False
    ), patch(
        "coord.branch_model.resolve_base_branch_for_issue_number", return_value="main"
    ), patch("coord.github_ops.branch_commits_ahead", return_value=0) as ahead:
        ok, _ = may_dispatch_rework(REPO, ISSUE, "fix")
    assert ok is True
    ahead.assert_not_called()


# ── 4. refused_premise / already-done, identical state ──────────────────────


def test_refuses_when_the_latest_attempt_already_reported_already_done():
    work = _assignment(
        branch=None,
        status="refused_premise",
        completion_summary="### Summary\nWORK_OUTCOME: already-done — found at coord/x.py\n",
    )
    with patch("coord.state.get_drive_queue_entry", return_value=None), patch(
        "coord.state.build_board", return_value=_board(work)
    ), patch("coord.config.load", side_effect=RuntimeError("no config in test")):
        ok, reason = may_dispatch_rework(REPO, ISSUE, "fix")
    assert ok is False
    assert "already-done" in reason


def test_a_newer_dispatch_clears_the_already_done_refusal():
    old = _assignment(
        assignment_id="a1",
        branch=None,
        status="refused_premise",
        dispatched_at=100.0,
        completion_summary="### Summary\nWORK_OUTCOME: already-done — found at coord/x.py\n",
    )
    newer = _assignment(
        assignment_id="a2", branch=None, status="done", dispatched_at=200.0,
    )
    with patch("coord.state.get_drive_queue_entry", return_value=None), patch(
        "coord.state.build_board", return_value=_board(old, newer)
    ), patch("coord.config.load", side_effect=RuntimeError("no config in test")):
        ok, _ = may_dispatch_rework(REPO, ISSUE, "fix")
    assert ok is True


def test_a_reopened_issue_waives_the_already_done_refusal():
    """#4133 review: `IssueFacts.reopened` exists precisely because a human
    reopening the issue is the statement that an "already done" verdict no
    longer holds — and reopening is the only lever left once this refusal
    is the one standing between every rework dispatch and a permanent
    block."""
    work = _assignment(
        branch=None,
        status="refused_premise",
        completion_summary="### Summary\nWORK_OUTCOME: already-done — found at coord/x.py\n",
    )
    with patch("coord.state.get_drive_queue_entry", return_value=None), patch(
        "coord.state.build_board", return_value=_board(work)
    ), patch("coord.config.load", return_value=_config()), patch(
        "coord.claim.list_matching_remote_branches", return_value=[]
    ), patch("coord.github_ops.work_is_terminal", return_value=False), patch(
        "coord.drive_queue._issue_reopened_live", return_value=True
    ):
        ok, reason = may_dispatch_rework(REPO, ISSUE, "fix")
    assert ok is True
    assert "already-done" not in reason


def test_already_done_refusal_checks_reopened_live_and_stays_refused_when_not():
    work = _assignment(
        branch=None,
        status="refused_premise",
        completion_summary="### Summary\nWORK_OUTCOME: already-done — found at coord/x.py\n",
    )
    with patch("coord.state.get_drive_queue_entry", return_value=None), patch(
        "coord.state.build_board", return_value=_board(work)
    ), patch("coord.config.load", return_value=_config()), patch(
        "coord.claim.list_matching_remote_branches", return_value=[]
    ), patch("coord.github_ops.work_is_terminal", return_value=False), patch(
        "coord.drive_queue._issue_reopened_live", return_value=False
    ) as reopened_check:
        ok, reason = may_dispatch_rework(REPO, ISSUE, "fix")
    assert ok is False
    assert "already-done" in reason
    reopened_check.assert_called_once_with("org/quadraui", ISSUE)


# ── non-convergence latch ────────────────────────────────────────────────────


def _row(legs_at_enqueue: int = 0, state: str = STATE_WAITING) -> dict:
    return {
        "repo_name": REPO,
        "issue_number": ISSUE,
        "state": state,
        "legs_at_enqueue": legs_at_enqueue,
    }


def test_latches_once_legs_dispatched_since_the_row_was_declared_reach_the_threshold():
    with patch(
        "coord.state.get_drive_queue_entry", return_value=_row(legs_at_enqueue=0)
    ), patch("coord.state.build_board", return_value=_board()), patch(
        "coord.config.load", side_effect=RuntimeError("no config in test")
    ), patch(
        "coord.state.leg_counts",
        return_value={f"{REPO}#{ISSUE}": {"work": NON_CONVERGENCE_LATCH_THRESHOLD}},
    ):
        ok, reason = may_dispatch_rework(REPO, ISSUE, "fix")
    assert ok is False
    assert NON_CONVERGENCE_LATCH_MARKER in reason
    # #4133: the marker makes the module's own permanent-block predicate
    # recognise this as a human-only clear, the same convention every other
    # `_PERMANENT_BLOCK_MARKERS` entry already uses.
    assert is_permanent_block_reason(reason) is True


def test_does_not_latch_below_the_threshold():
    with patch(
        "coord.state.get_drive_queue_entry", return_value=_row(legs_at_enqueue=0)
    ), patch("coord.state.build_board", return_value=_board()), patch(
        "coord.config.load", side_effect=RuntimeError("no config in test")
    ), patch(
        "coord.state.leg_counts",
        return_value={
            f"{REPO}#{ISSUE}": {"work": NON_CONVERGENCE_LATCH_THRESHOLD - 1}
        },
    ):
        ok, reason = may_dispatch_rework(REPO, ISSUE, "fix")
    assert ok is True
    assert NON_CONVERGENCE_LATCH_MARKER not in reason


def test_latch_resets_after_an_operator_remove_and_add():
    """#4133 review: `drive_queue_remove` deletes the row outright and `add`
    inserts a fresh one with `legs_at_enqueue` restamped at that moment — so
    the identical lifetime leg count that just latched reads back as zero
    rounds spent against the NEW row's own baseline. This is the clear path
    the latch's refusal reason actually promises, unlike a per-branch
    `review_iteration` scan off board history, which the remove/add cycle
    never touches at all."""
    with patch(
        "coord.state.get_drive_queue_entry",
        return_value=_row(legs_at_enqueue=NON_CONVERGENCE_LATCH_THRESHOLD + 3),
    ), patch("coord.state.build_board", return_value=_board()), patch(
        "coord.config.load", side_effect=RuntimeError("no config in test")
    ), patch(
        "coord.state.leg_counts",
        return_value={
            f"{REPO}#{ISSUE}": {"work": NON_CONVERGENCE_LATCH_THRESHOLD + 3}
        },
    ):
        ok, reason = may_dispatch_rework(REPO, ISSUE, "fix")
    assert ok is True
    assert NON_CONVERGENCE_LATCH_MARKER not in reason


def test_latch_does_not_relatch_on_a_reused_branch_name_after_a_fresh_add():
    """#4133 review: work branches are deterministic
    (`existing_branch or f"issue-{N}-{slug}"` in `coord.agent`), so a
    post-merge rework dispatch for the same issue+title lands on the exact
    SAME branch name as the exhausted one. The latch must not re-trip just
    because the branch name was reused — it is keyed to the row's own
    baseline, never to a per-branch history scan."""
    work = _assignment(branch="issue-2825-fix", status="done")
    with patch(
        "coord.state.get_drive_queue_entry", return_value=_row(legs_at_enqueue=1)
    ), patch("coord.state.build_board", return_value=_board(work)), patch(
        "coord.config.load", side_effect=RuntimeError("no config in test")
    ), patch(
        "coord.state.leg_counts", return_value={f"{REPO}#{ISSUE}": {"work": 1}}
    ):
        ok, reason = may_dispatch_rework(REPO, ISSUE, "fix")
    assert ok is True
    assert NON_CONVERGENCE_LATCH_MARKER not in reason


def test_latch_is_a_no_op_with_no_drive_queue_row_at_all():
    """No row means no `remove && add` clear path exists either, so this
    latch — unlike the other refusals above, which are pure live GitHub
    reads — has nothing to attach to and must not fire."""
    work = _assignment(review_iteration=NON_CONVERGENCE_LATCH_THRESHOLD + 10)
    with patch("coord.state.get_drive_queue_entry", return_value=None), patch(
        "coord.state.build_board", return_value=_board(work)
    ), patch("coord.config.load", side_effect=RuntimeError("no config in test")):
        ok, reason = may_dispatch_rework(REPO, ISSUE, "fix")
    assert ok is True
    assert NON_CONVERGENCE_LATCH_MARKER not in reason


# ── fail-open on lookup trouble ───────────────────────────────────────────────


def test_fails_open_when_config_load_raises():
    work = _assignment()
    with patch("coord.state.get_drive_queue_entry", return_value=None), patch(
        "coord.state.build_board", return_value=_board(work)
    ), patch("coord.config.load", side_effect=RuntimeError("boom")):
        ok, _ = may_dispatch_rework(REPO, ISSUE, "fix")
    assert ok is True


def test_fails_open_when_repo_is_unknown_to_config():
    work = _assignment()
    empty_config = Config(repos=[], machines=[])
    with patch("coord.state.get_drive_queue_entry", return_value=None), patch(
        "coord.state.build_board", return_value=_board(work)
    ), patch("coord.config.load", return_value=empty_config):
        ok, _ = may_dispatch_rework(REPO, ISSUE, "fix")
    assert ok is True


def test_returns_true_with_a_reason_when_nothing_matches():
    work = _assignment(status="running", branch=None)
    with patch("coord.state.get_drive_queue_entry", return_value=None), patch(
        "coord.state.build_board", return_value=_board(work)
    ), patch("coord.config.load", side_effect=RuntimeError("no config in test")):
        ok, reason = may_dispatch_rework(REPO, ISSUE, "fix")
    assert ok is True
    assert reason


def test_accepts_a_caller_supplied_board_instead_of_building_its_own():
    work = _assignment(status="running", branch=None)
    with patch("coord.state.get_drive_queue_entry", return_value=None), patch(
        "coord.state.build_board"
    ) as build_board_mock, patch(
        "coord.config.load", side_effect=RuntimeError("no config in test")
    ):
        ok, _ = may_dispatch_rework(REPO, ISSUE, "fix", board=_board(work))
    assert ok is True
    build_board_mock.assert_not_called()


# ── #4134: wiring into the auto-loop's own fix dispatchers ──────────────────
#
# The tests above pin `may_dispatch_rework` itself; these pin that
# `coord.auto_loop._dispatch_fix` and `coord.auto_loop._dispatch_fix_for_review`
# actually CALL it and honor a refusal, rather than re-deriving the
# predicate's own logic (already exhaustive above).


def _work_row(**overrides) -> Assignment:
    """The reviewed WORK row — ``_assignment()`` plus the fields the fix
    dispatchers actually read: a briefing to re-send, a ``review_state`` for
    the verdict to propagate onto, and a PR for the guard to refuse against.
    """
    return _assignment(**{
        "assignment_id": "work-2825",
        "issue_title": "Fix the thing",
        "briefing": "Original briefing text.",
        "pr_url": "https://github.com/org/quadraui/pull/999",
        "finished_at": 101.0,
        "review_state": "dispatched",
        **overrides,
    })


def _review_row(**overrides) -> Assignment:
    """The completed REVIEW row pointing at :func:`_work_row`'s work."""
    return _assignment(**{
        "assignment_id": "review-2825",
        "issue_title": "[review] Fix the thing",
        "type": "review",
        "dispatched_at": 102.0,
        "finished_at": 103.0,
        "review_of_assignment_id": "work-2825",
        **overrides,
    })


def _done_row_entry() -> dict:
    """The drive-queue row from the incident: already ``done``."""
    return {"repo_name": REPO, "issue_number": ISSUE, "state": STATE_DONE}


def test_2825_shape_merged_pr_done_row_zero_dispatches_from_fix_dispatchers():
    """The #2825 incident shape — a merged PR, a drive-queue row that
    already reads `done`, and (per the daemon log) a block_log episode the
    board-doctor sweep kept re-classifying `integration-red` — must
    produce ZERO fix dispatches from either auto-loop dispatcher.
    `block_log` itself is not consulted by `may_dispatch_rework` or by
    either dispatcher below (that binding is #4135/#4137's own scope), so
    it is not part of what these two assertions exercise; the `done` row
    and the live-merged PR are, and either alone already refuses."""
    from coord.auto_loop import _dispatch_fix, _dispatch_fix_for_review

    work = _work_row()
    review = _review_row()
    board = _board(work, review)
    config = _config()
    mock_http = MagicMock()

    # `config.machines=[]` alone already makes `select_fix_machine` return
    # no candidate, so `_dispatch_fix` would return `None` even with the
    # guard removed — that would make this arm pass vacuously. Patch
    # `select_fix_machine` itself (imported inside `_dispatch_fix` at call
    # time, so patching the `coord.dispatch` module attribute is seen) and
    # assert it is never even reached: that is the fact that can only hold
    # with the `may_dispatch_rework` guard in place.
    with patch(
        "coord.state.get_drive_queue_entry", return_value=_done_row_entry(),
    ), patch("coord.github_ops.work_is_terminal", return_value=True), patch(
        "coord.dispatch.select_fix_machine"
    ) as mock_select_fix_machine:
        fix_result = _dispatch_fix(
            work, "fix it", board, config, 1, http_client=mock_http,
        )

        findings = ReviewFindings(
            verdict="request-changes", body="## Blocking\n- 1. fix it\n"
        )
        actions = _dispatch_fix_for_review(
            review, findings, board, config, http_client=mock_http,
        )

    assert fix_result is None
    mock_select_fix_machine.assert_not_called()
    assert len(actions) == 1
    assert actions[0].kind == "rework_refused"
    assert "done" in actions[0].detail
    mock_http.post.assert_not_called()


def test_rework_refusal_persists_the_propagated_verdict_to_the_db(tmp_path):
    """A refusal is reached only AFTER `propagate_review_verdict` has
    already written `review.review_verdict` and `work.review_state` onto
    the in-memory board, and `process_review_completion` leaves persistence
    to its caller. So `rework_refused` has to be one of the kinds that
    caller writes the board back for (`PERSIST_ACTION_KINDS`) — otherwise
    the mutation is silently dropped, the review row's `review_verdict`
    stays NULL in the DB, and every later `coord notify` pass re-detects
    the row as "review done, no verdict ever captured" and can escalate to
    deleting and re-dispatching the review: a fresh metered review leg
    against an already-merged PR, the very symptom this guard exists to
    stop."""
    from coord.auto_loop import run_for_review_transition
    from coord.state import load_board, save_board

    review_log = tmp_path / "review.log"
    review_log.write_text(
        "REVIEW_VERDICT: request-changes\n"
        "REVIEW_BODY:\n"
        "## Blocking findings\n"
        "- A real problem, from this reviewer's own vantage.\n"
        "END_REVIEW\n"
    )
    save_board(_board(_work_row(), _review_row()))

    record = {"type": "review", "review_of_assignment_id": "work-2825"}
    entry = {"log_path": str(review_log)}

    with patch(
        "coord.state.get_drive_queue_entry", return_value=_done_row_entry(),
    ), patch("coord.github_ops.work_is_terminal", return_value=True):
        actions = run_for_review_transition(
            "review-2825", record, entry, _config(),
        )

    assert [a.kind for a in actions] == ["rework_refused"]

    loaded = load_board()
    assert loaded is not None
    review_loaded = loaded.find_by_id("review-2825")
    assert review_loaded is not None
    assert review_loaded.review_verdict == "request-changes", (
        "the verdict must be durably recorded, not dropped — a later stall "
        "sweep must never read this row as 'no verdict ever captured'"
    )
    work_loaded = loaded.find_by_id("work-2825")
    assert work_loaded is not None
    assert work_loaded.review_state == "done"


def test_notify_classifies_a_rework_refusal_as_a_board_mutation():
    """The stalled-pipeline sweep reaches the same dispatcher through
    `coord.notify.dispatch_stalled_pipeline_action`, which decides whether
    the sweep's `board_dirty` gets set from the action kind alone. A kind
    that mutates the board but dispatches nothing has to classify as
    `review_transition_applied` there, not `no_action`: `no_action` drops
    the write while the one-shot `notified` ledger still marks the row
    handled, so the transition is lost for good."""
    from coord import notify as notify_mod
    from coord.auto_loop import LoopAction

    assert (
        "rework_refused" in notify_mod._MUTATING_REVIEW_COMPLETION_KINDS
    ), "rework_refused mutates the board — notify must persist it"

    work = _work_row(review_state=None, test_state="passed")
    review = _review_row(review_verdict="request-changes", review_posted_at=104.0)
    board = _board(work, review)
    config = _config()
    config.pipeline.auto_dispatch_stalled = True

    detections = notify_mod.detect_stalled_pipeline(
        config, board=board, merge_queue_items=[],
    )
    assert detections, "fixture must produce the review_request_changes_no_fix shape"
    detection, detected_work = detections[0]
    assert detection.reason == "review_request_changes_no_fix"

    with patch(
        "coord.auto_loop.process_review_completion",
        return_value=[LoopAction(
            kind="rework_refused", assignment_id="review-2825",
            detail="drive-queue row is already 'done' — refusing fix dispatch",
        )],
    ):
        action = notify_mod.dispatch_stalled_pipeline_action(
            detection, detected_work, board, config,
        )

    assert action.kind == "review_transition_applied"
    assert action.kind in notify_mod._STALLED_DISPATCH_KINDS


def test_a_fix_round_asks_the_live_rework_question_exactly_once():
    """`_dispatch_fix_for_review` consults the guard and then calls
    `_dispatch_fix`, which guards itself for every other caller. Each
    evaluation makes live GitHub reads (PR state per candidate branch, a
    base/head compare, a remote branch listing), so one fix round must ask
    once — the allow it already has cannot have changed in between."""
    from coord.auto_loop import _dispatch_fix_for_review

    work = _work_row()
    review = _review_row()
    board = _board(work, review)
    findings = ReviewFindings(
        verdict="request-changes", body="## Blocking\n- 1. fix it\n"
    )

    guard = MagicMock(return_value=(True, "no refusal condition matched"))
    with patch("coord.drive_queue.may_dispatch_rework", guard), patch(
        "coord.auto_loop._work_is_terminal", return_value=False,
    ), patch(
        "coord.drive_queue.fix_round_ceiling_blocked_for_issue",
        return_value=False,
    ), patch("coord.dispatch.select_fix_machine") as select_machine:
        select_machine.return_value = MagicMock(machine=None)
        actions = _dispatch_fix_for_review(
            review, findings, board, _config(), http_client=MagicMock(),
        )

    # The dispatch itself fails (no capable machine) — what this pins is the
    # guard call count on the way there.
    assert [a.kind for a in actions] == ["no_work_found"]
    assert guard.call_count == 1, (
        f"expected one live rework check per fix round, got "
        f"{guard.call_count}"
    )


def test_every_auto_loop_fix_dispatch_site_calls_may_dispatch_rework():
    """Grep-based: every function in `coord/auto_loop.py` that dispatches a
    rework leg must call `may_dispatch_rework` BEFORE it dispatches — both
    that the call exists and that it precedes the dispatch it guards, so a
    guard moved below the POST fails here. Covers the two sites #4134 wires
    (`_dispatch_fix`, `_dispatch_fix_for_review`); the sibling slices extend
    this same shape of assertion for their own dispatch sites rather than
    inventing a second way to ask the same question (#2096/#2085)."""
    import inspect

    from coord import auto_loop

    # Each site paired with the text of the dispatch it must guard: the
    # `/assign` POST for the function that fires it, and the call to that
    # function for the one that delegates.
    sites = {
        "_dispatch_fix": "/assign",
        "_dispatch_fix_for_review": "_dispatch_fix(",
    }
    for name, dispatch_marker in sites.items():
        source = inspect.getsource(getattr(auto_loop, name))
        guard_at = source.find("may_dispatch_rework(")
        assert guard_at != -1, (
            f"coord.auto_loop.{name} must call may_dispatch_rework() before "
            "dispatching a rework leg (#4134)"
        )
        dispatch_at = source.find(dispatch_marker)
        assert dispatch_at != -1, (
            f"coord.auto_loop.{name} no longer dispatches via "
            f"{dispatch_marker!r} — update this test's site table"
        )
        assert guard_at < dispatch_at, (
            f"coord.auto_loop.{name} calls may_dispatch_rework() only AFTER "
            f"{dispatch_marker!r} — a guard below the dispatch guards nothing"
        )


# ── #4135: wiring into the board-doctor dispatchers ─────────────────────────
#
# The tests above pin `may_dispatch_rework` itself (and #4134's auto-loop
# wiring); these pin that `coord.board_doctor._dispatch_base_sync_fix` and
# `coord.board_doctor_permissions.sweep_dispatch_fixit_leg` actually CALL it
# and honor a refusal, rather than re-deriving the predicate's own logic.


def test_every_board_doctor_rework_dispatch_site_calls_may_dispatch_rework():
    """Grep-based, same shape as `test_every_auto_loop_fix_dispatch_site_
    calls_may_dispatch_rework` above: every board-doctor function that
    dispatches a rework leg must call `may_dispatch_rework` BEFORE it
    dispatches."""
    import inspect

    from coord import board_doctor, board_doctor_permissions

    sites = {
        (board_doctor, "_dispatch_base_sync_fix"): "dispatch_conflict_fix(",
        (board_doctor_permissions, "sweep_dispatch_fixit_leg"): "_dispatch_fix(",
    }
    for (module, name), dispatch_marker in sites.items():
        source = inspect.getsource(getattr(module, name))
        guard_at = source.find("may_dispatch_rework(")
        assert guard_at != -1, (
            f"{module.__name__}.{name} must call may_dispatch_rework() "
            "before dispatching a rework leg (#4135)"
        )
        dispatch_at = source.find(dispatch_marker)
        assert dispatch_at != -1, (
            f"{module.__name__}.{name} no longer dispatches via "
            f"{dispatch_marker!r} — update this test's site table"
        )
        assert guard_at < dispatch_at, (
            f"{module.__name__}.{name} calls may_dispatch_rework() only "
            f"AFTER {dispatch_marker!r} — a guard below the dispatch "
            "guards nothing"
        )


def test_2825_shape_done_row_refuses_base_sync_fix_dispatch():
    """The #2825 incident shape against `_dispatch_base_sync_fix`: a
    drive-queue row already `done` must refuse the budget-free base-sync
    fix before `dispatch_conflict_fix` is ever reached."""
    from coord import merge_queue as mq
    from coord.board_doctor import _dispatch_base_sync_fix

    mq.save_queue([
        mq.QueuedMerge(
            assignment_id="w2825", repo_name=REPO, repo_github="org/quadraui",
            branch="issue-2825-fix", target_branch="main", issue_number=ISSUE,
            issue_title="t", state=mq.PENDING,
        ),
    ])

    with patch(
        "coord.state.get_drive_queue_entry", return_value=_done_row_entry(),
    ), patch("coord.conflict_fix.dispatch_conflict_fix") as mock_dispatch:
        result = _dispatch_base_sync_fix(REPO, ISSUE, config=_config())

    mock_dispatch.assert_not_called()
    assert result["dispatched"] is False
    assert "done" in result["detail"]


def test_2825_shape_done_row_refuses_fixit_leg_dispatch():
    """The #2825 incident shape against `sweep_dispatch_fixit_leg`: a
    drive-queue row already `done` must refuse the fix-it leg before
    `_dispatch_fix` (and so the agent `/assign` POST it would make) is
    ever reached."""
    from coord.board_doctor_permissions import sweep_dispatch_fixit_leg

    work = _work_row()
    board = _board(work)
    mock_http = MagicMock()

    with patch(
        "coord.state.get_drive_queue_entry", return_value=_done_row_entry(),
    ), patch("coord.auto_loop._dispatch_fix") as mock_dispatch_fix:
        result = sweep_dispatch_fixit_leg(
            REPO, ISSUE, board, _config(),
            cause="ci_rerun_exhausted", http_client=mock_http,
        )

    mock_dispatch_fix.assert_not_called()
    assert result["dispatched"] is False
    assert "done" in result["detail"]
    mock_http.post.assert_not_called()


# ── #4136: wiring into conflict-fix, ci-fix and review dispatch ────────────
#
# The tests above pin `may_dispatch_rework` itself and #4134/#4135's wiring
# into the auto-loop and board-doctor dispatchers; these pin that
# `coord.conflict_fix.dispatch_conflict_fix`, `coord.ci_fix.dispatch_ci_fix`
# and `coord.review.dispatch_review` — the last three of the parent issue's
# seven declared call sites — actually CALL the guard and honor a refusal,
# rather than re-deriving the predicate's own logic (already exhaustive
# above).


def _queued_merge_entry(**overrides):
    from coord import merge_queue as mq

    defaults = dict(
        assignment_id="w2825", repo_name=REPO, repo_github="org/quadraui",
        branch="issue-2825-fix", target_branch="main", issue_number=ISSUE,
        issue_title="t", state=mq.PENDING,
    )
    defaults.update(overrides)
    return mq.QueuedMerge(**defaults)


def test_every_conflict_ci_review_rework_dispatch_site_calls_may_dispatch_rework():
    """Grep-based, same shape as the #4134/#4135 site tests above: every
    function in `coord.conflict_fix`, `coord.ci_fix` and `coord.review` that
    dispatches a rework leg must call `may_dispatch_rework` BEFORE it
    dispatches — both that the call exists and that it precedes the
    dispatch it guards, so a guard moved below the POST/delegated call
    fails here. With this, all 7 of #4121's declared call sites are
    covered."""
    import inspect

    from coord import ci_fix, conflict_fix, review

    # Each site paired with the text of the live dispatch action it must
    # guard: the `/assign` POST itself for the two sites that build one
    # directly, or the call to the delegating primitive for the one that
    # doesn't.
    sites = {
        (conflict_fix, "dispatch_conflict_fix"): "client.post(url",
        (ci_fix, "dispatch_ci_fix"): "_dispatch_fix(",
        (review, "dispatch_review"): "client.post(url",
    }
    for (module, name), dispatch_marker in sites.items():
        source = inspect.getsource(getattr(module, name))
        guard_at = source.find("may_dispatch_rework(")
        assert guard_at != -1, (
            f"{module.__name__}.{name} must call may_dispatch_rework() "
            "before dispatching a rework leg (#4136)"
        )
        dispatch_at = source.find(dispatch_marker)
        assert dispatch_at != -1, (
            f"{module.__name__}.{name} no longer dispatches via "
            f"{dispatch_marker!r} — update this test's site table"
        )
        assert guard_at < dispatch_at, (
            f"{module.__name__}.{name} calls may_dispatch_rework() only "
            f"AFTER {dispatch_marker!r} — a guard below the dispatch "
            "guards nothing"
        )


def test_2825_shape_done_row_refuses_conflict_fix_dispatch():
    """The #2825 incident shape against `dispatch_conflict_fix`: a
    drive-queue row already `done` must refuse the conflict-fix before
    machine selection — and so the agent `/assign` POST it would make —
    is ever reached."""
    from coord.conflict_fix import dispatch_conflict_fix

    entry = _queued_merge_entry()
    board = _board()

    with patch(
        "coord.state.get_drive_queue_entry", return_value=_done_row_entry(),
    ), patch("coord.conflict_fix.select_conflict_fix_machine") as mock_select:
        result = dispatch_conflict_fix(entry, board, _config())

    mock_select.assert_not_called()
    assert result is None


def test_2825_shape_done_row_refuses_ci_fix_dispatch():
    """The #2825 incident shape against `dispatch_ci_fix`: a drive-queue
    row already `done` must refuse the ci-fix before the original work
    assignment is even looked up — and so before any infra rerun, mechanic
    leg, or ordinary worker dispatch is attempted."""
    from coord.ci_fix import dispatch_ci_fix

    entry = _queued_merge_entry()
    board = _board()

    with patch(
        "coord.state.get_drive_queue_entry", return_value=_done_row_entry(),
    ), patch("coord.ci_fix.dispatch_precheck") as mock_precheck:
        result = dispatch_ci_fix(entry, board, _config())

    mock_precheck.assert_not_called()
    assert result is None


def test_2825_shape_done_row_refuses_review_dispatch():
    """The #2825 incident shape against `dispatch_review`: a drive-queue
    row already `done` must refuse a fresh review before the #3670
    duplicate-review checks and the atomic dispatch claim — and so before
    the agent `/assign` POST it would make — are ever reached."""
    from coord.review import dispatch_review

    work = _work_row()
    board = _board(work)

    with patch(
        "coord.state.get_drive_queue_entry", return_value=_done_row_entry(),
    ), patch("coord.state.claim_review_dispatch") as mock_claim:
        result = dispatch_review(work, board, _config())

    mock_claim.assert_not_called()
    assert result is None
    assert "done" in (work.review_dispatch_reason or "")
