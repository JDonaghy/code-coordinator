"""Unit tests for ``coord.drive_queue.may_dispatch_rework`` (#4133).

#4133 is slice 1 of 5 of #4121 (the quadraui#2825 incident: a merged PR
re-dispatched a fix leg about 22 times because nothing checked the live
PR/row state before firing). This file pins the guard itself; the sibling
slices (#4134-#4137) wire it into the actual dispatch sites, which are out
of scope here.
"""

from __future__ import annotations

from unittest.mock import patch

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
    return Board(active=[], completed=list(assignments))


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
