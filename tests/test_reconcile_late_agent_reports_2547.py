"""#2547: let a late-arriving, authoritative agent completion correct a stale
``_reconcile_no_agent_record`` GUESS.

Reproduces coord-portal#129's assignment ``c2120f7206ec`` shape: the
no-record arm (``_reconcile_no_agent_record``, #2275/#2553) caught the row
mid-``AgentServer._reap`` — in neither the agent's ``active`` nor
``completed`` list yet — and guessed a terminal status from weaker evidence
than the reap's own verdict. Once guessed, ``reconcile_completed_assignments``
never looks at the row again (it only scans ``status == "running"`` rows), so
the board is stuck on the wrong answer even after the agent's own, complete
completion record shows up. ``reconcile_late_agent_reports`` is the follow-up
pass that finds exactly those rows and corrects them once the real record
arrives — never overwriting anything else.
"""

from __future__ import annotations

import time

from coord.config import Config
from coord.models import Assignment, Board, Machine, Repo
from coord.reconcile import (
    _LATE_REPORT_CORRECTION_WINDOW_SECONDS,
    NO_AGENT_RECORD_REASON,
    _no_agent_record_branch_reason,
    reconcile_late_agent_reports,
)


def _config() -> Config:
    return Config(
        repos=[Repo(name="cc", github="acme/cc")],
        machines=[Machine(name="precision", host="precision", repos=["cc"])],
    )


def _board(*assignments: Assignment) -> Board:
    return Board(
        repos=[Repo(name="cc", github="acme/cc")], machines=[], completed=list(assignments)
    )


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(
        self, *, assignment_id, terminal_status, branch, review_state,
        failure_reason=None, exit_code=None,
    ) -> None:
        self.calls.append(
            {
                "assignment_id": assignment_id,
                "terminal_status": terminal_status,
                "branch": branch,
                "review_state": review_state,
                "failure_reason": failure_reason,
                "exit_code": exit_code,
            }
        )


def _status(*, active: list[dict] | None = None, completed: list[dict] | None = None) -> dict:
    return {"active": list(active or []), "completed": list(completed or [])}


def _guessed_failed(
    aid: str = "c2120f7206ec",
    *,
    atype: str = "test-author",
    branch: str | None = "issue-129-jit-slice",
    finished_at: float | None = None,
) -> Assignment:
    """A row `_reconcile_no_agent_record` already flipped to `failed` with
    the pinned reason — i.e. no branch was known to have commits ahead at
    guess time (the pre-#2553 half, or #2553's own "unknown" fail-closed)."""
    return Assignment(
        machine_name="precision", repo_name="cc",
        issue_number=129, issue_title="t",
        status="failed", assignment_id=aid, type=atype, branch=branch,
        failure_reason=NO_AGENT_RECORD_REASON,
        finished_at=(time.time() - 300.0) if finished_at is None else finished_at,
    )


def _guessed_advisory(
    aid: str = "c2120f7206ec", *, branch: str = "issue-129-jit-slice", ahead: int = 3,
) -> Assignment:
    """A row `_reconcile_no_agent_record` flipped to `advisory` via #2553's
    branch-has-commits half."""
    return Assignment(
        machine_name="precision", repo_name="cc",
        issue_number=129, issue_title="t",
        status="advisory", assignment_id=aid, type="test-author", branch=branch,
        failure_reason=_no_agent_record_branch_reason(branch, ahead),
        finished_at=time.time() - 300.0,
    )


# ── the fix ────────────────────────────────────────────────────────────────


def test_late_advisory_report_corrects_a_guessed_failed() -> None:
    """Acceptance 1: the exact coord-portal#129 shape — guessed `failed`,
    agent's own completed record (arrived late) says `advisory` → corrected."""
    rec = _Recorder()
    out = reconcile_late_agent_reports(
        _config(),
        board=_board(_guessed_failed()),
        agent_status_fn=lambda host: _status(
            completed=[{"id": "c2120f7206ec", "status": "advisory"}]
        ),
        update_state_fn=rec,
    )

    assert len(rec.calls) == 1
    assert rec.calls[0]["terminal_status"] == "advisory"
    assert rec.calls[0]["branch"] == "issue-129-jit-slice"
    assert "advisory" in rec.calls[0]["failure_reason"]
    assert "#2547" in rec.calls[0]["failure_reason"]
    assert out[0]["from_status"] == "failed"
    assert out[0]["to_status"] == "advisory"


def test_still_no_record_leaves_the_guess_alone() -> None:
    """The common case: the agent still has no record (leg genuinely gone,
    or hasn't finished reaping yet) — nothing to correct with, so nothing
    changes."""
    rec = _Recorder()
    out = reconcile_late_agent_reports(
        _config(),
        board=_board(_guessed_failed()),
        agent_status_fn=lambda host: _status(),
        update_state_fn=rec,
    )
    assert rec.calls == []
    assert out == []


def test_agreeing_report_is_a_no_op() -> None:
    """The agent's real record agrees with the guess (#2553 already got it
    right) — no redundant write."""
    rec = _Recorder()
    out = reconcile_late_agent_reports(
        _config(),
        board=_board(_guessed_failed()),
        agent_status_fn=lambda host: _status(
            completed=[{"id": "c2120f7206ec", "status": "failed"}]
        ),
        update_state_fn=rec,
    )
    assert rec.calls == []
    assert out == []


def test_never_corrects_into_done() -> None:
    """`done` is deliberately excluded — promoting straight to `done` here
    would skip the #1616 notify drain and every side effect a real
    completion goes through. The row stays on its safer guess."""
    rec = _Recorder()
    out = reconcile_late_agent_reports(
        _config(),
        board=_board(_guessed_failed()),
        agent_status_fn=lambda host: _status(
            completed=[{"id": "c2120f7206ec", "status": "done"}]
        ),
        update_state_fn=rec,
    )
    assert rec.calls == []
    assert out == []


def test_row_not_terminal_via_the_guess_arm_is_never_touched() -> None:
    """A row that reached `failed`/`advisory` for a real reason (no
    no-agent-record marker in `failure_reason`) must never be touched —
    this pass only corrects its own sibling arm's guesses."""
    rec = _Recorder()
    real_failure = Assignment(
        machine_name="precision", repo_name="cc",
        issue_number=129, issue_title="t",
        status="failed", assignment_id="w2", type="work",
        failure_reason="worker crashed: OOM",
        finished_at=time.time() - 300.0,
    )
    out = reconcile_late_agent_reports(
        _config(),
        board=_board(real_failure),
        agent_status_fn=lambda host: _status(
            completed=[{"id": "w2", "status": "advisory"}]
        ),
        update_state_fn=rec,
    )
    assert rec.calls == []
    assert out == []


def test_a_human_reset_row_is_never_touched() -> None:
    """Once a human (or `coord diagnose --reset`) has moved the row off
    `failed`/`advisory`, this pass must never touch it again even if the
    stale reason text is still sitting in `failure_reason`."""
    rec = _Recorder()
    reset_row = _guessed_failed()
    reset_row.status = "pending"
    out = reconcile_late_agent_reports(
        _config(),
        board=_board(reset_row),
        agent_status_fn=lambda host: _status(
            completed=[{"id": "c2120f7206ec", "status": "advisory"}]
        ),
        update_state_fn=rec,
    )
    assert rec.calls == []
    assert out == []


def test_past_the_correction_window_is_left_alone() -> None:
    """Bounded, not a standing full-history rescan: a guess old enough is
    never revisited again, even if the real record would still resolve it."""
    rec = _Recorder()
    old_row = _guessed_failed(
        finished_at=time.time() - (_LATE_REPORT_CORRECTION_WINDOW_SECONDS + 60.0)
    )
    out = reconcile_late_agent_reports(
        _config(),
        board=_board(old_row),
        agent_status_fn=lambda host: _status(
            completed=[{"id": "c2120f7206ec", "status": "advisory"}]
        ),
        update_state_fn=rec,
    )
    assert rec.calls == []
    assert out == []


def test_unreachable_agent_changes_nothing() -> None:
    rec = _Recorder()
    out = reconcile_late_agent_reports(
        _config(),
        board=_board(_guessed_failed()),
        agent_status_fn=lambda host: None,
        update_state_fn=rec,
    )
    assert rec.calls == []
    assert out == []


def test_guessed_advisory_can_still_be_corrected_to_failed() -> None:
    """The correction is bidirectional within the safe-target set: a #2553
    advisory guess (branch had commits) can still be corrected to `failed`
    if the agent's own real verdict says so."""
    rec = _Recorder()
    out = reconcile_late_agent_reports(
        _config(),
        board=_board(_guessed_advisory()),
        agent_status_fn=lambda host: _status(
            completed=[{"id": "c2120f7206ec", "status": "failed"}]
        ),
        update_state_fn=rec,
    )
    assert len(rec.calls) == 1
    assert rec.calls[0]["terminal_status"] == "failed"
    assert out[0]["from_status"] == "advisory"
    assert out[0]["to_status"] == "failed"
