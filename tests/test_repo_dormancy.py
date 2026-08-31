"""Tests for coord.repo_dormancy: the per-repo dormancy skip for the
daemon's periodic GitHub sweeps (#2994).

State-file isolation comes from the autouse ``_no_real_repo_dormancy_store``
fixture in conftest.py (redirects ``$COORD_REPO_DORMANCY_STATE`` to a temp
path per test) — no explicit fixture needed here.
"""

from __future__ import annotations

import time

from coord import repo_dormancy
from coord.models import Assignment, Board


def _assignment(*, repo_name: str = "idle", status: str = "done") -> Assignment:
    return Assignment(
        machine_name="laptop",
        repo_name=repo_name,
        issue_number=1,
        issue_title="t",
        status=status,
        assignment_id="a1",
        type="work",
    )


def test_repo_with_no_activity_is_idle() -> None:
    board = Board(active=[], completed=[])
    assert repo_dormancy.repo_has_activity("idle", board) is False


def test_open_assignment_counts_as_activity() -> None:
    board = Board(active=[_assignment(status="running")], completed=[])
    assert repo_dormancy.repo_has_activity("idle", board) is True


def test_done_but_unmerged_assignment_counts_as_open_pr_activity() -> None:
    """A completed row still at status='done' is a coord-authored PR that
    (as far as the board knows) hasn't merged yet — activity, not idle."""
    board = Board(active=[], completed=[_assignment(status="done")])
    assert repo_dormancy.repo_has_activity("idle", board) is True


def test_merged_assignment_does_not_count_as_activity() -> None:
    """A row that already flipped to 'merged' is done being tracked — it
    must not keep a repo artificially 'active' forever."""
    board = Board(active=[], completed=[_assignment(status="merged")])
    assert repo_dormancy.repo_has_activity("idle", board) is False


def test_drive_queue_entry_counts_as_activity(monkeypatch) -> None:
    from coord import state

    monkeypatch.setattr(
        state, "list_drive_queue", lambda repo_name=None: [{"repo": "idle"}]
    )
    board = Board(active=[], completed=[])
    assert repo_dormancy.repo_has_activity("idle", board) is True


def test_activity_in_a_different_repo_does_not_count() -> None:
    board = Board(active=[_assignment(repo_name="other", status="running")], completed=[])
    assert repo_dormancy.repo_has_activity("idle", board) is False


def test_never_swept_repo_is_not_skipped() -> None:
    """A repo with no recorded sweep at all is due, not skippable — there is
    no prior sweep to protect."""
    board = Board(active=[], completed=[])
    assert repo_dormancy.should_skip_sweep("idle", board) is False


def test_dormant_repo_skipped_within_the_floor() -> None:
    board = Board(active=[], completed=[])
    now = time.time()
    repo_dormancy.record_swept("idle", now=now)
    assert repo_dormancy.should_skip_sweep("idle", board, now=now + 60.0) is True


def test_dormant_repo_swept_again_once_floor_expires() -> None:
    board = Board(active=[], completed=[])
    now = time.time()
    repo_dormancy.record_swept("idle", now=now)
    past_floor = now + repo_dormancy.DORMANT_SWEEP_FLOOR_S + 1.0
    assert repo_dormancy.should_skip_sweep("idle", board, now=past_floor) is False


def test_queuing_work_wakes_a_dormant_repo_before_the_floor_expires() -> None:
    """#2994 acceptance: waking is prompt — queuing work for a dormant repo
    puts it back on the normal cadence on the very next check, not after
    DORMANT_SWEEP_FLOOR_S. `should_skip_sweep` is computed live against
    board state on every call, so there is no separate 'un-skip' to miss."""
    board = Board(active=[], completed=[])
    now = time.time()
    repo_dormancy.record_swept("idle", now=now)
    # Still well inside the floor -- would be skipped if still idle.
    soon = now + 5.0
    assert repo_dormancy.should_skip_sweep("idle", board, now=soon) is True

    # Work gets queued for the repo -- board now shows an open assignment.
    board.active.append(_assignment(status="pending"))

    assert repo_dormancy.should_skip_sweep("idle", board, now=soon) is False
