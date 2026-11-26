"""#2275: reconcile a running board row the agent has no record of.

The passive reconcile (#625, on #1616's 30s daemon clock) used to conflate two
cases with opposite correct handling — "still running on the agent" and "the
agent has no record of this at all" — behind one `continue`, and so left both.
Because `_COMPLETED_HISTORY_CAP` is 25, the second case is a *certainty* rather
than a race: a leg that dies needs only 25 further completions on its machine
before its id is gone from `/status` forever.

claude-coordinator#2208 is the worked example: a `[smoke]` Test leg died, its
board row stayed `status=running` for 11 hours, the drive burned both attempts
against a 240m deadline on a branch that was already green, and a human running
`coord status` cleared it — merge-READY 14 minutes later.

**This arm reaps**, so the exclusion tests below matter more than the happy
path.  #1658 (reaping live headless workers) is what getting it wrong costs.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from coord.config import Config
from coord.models import Assignment, Board, Machine, Repo
from coord.reconcile import (
    NO_AGENT_RECORD_REASON,
    agent_has_no_record,
    is_attended_session,
    reconcile_completed_assignments,
)


@pytest.fixture(autouse=True)
def _no_real_gh_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Safety net for #2553's branch check: every test in this module either
    doesn't reach `_reconcile_no_agent_record`'s branch check at all, or
    passes its own `commits_ahead_fn` stub. If a future test reaches it
    without stubbing, this fails loudly instead of silently shelling out to
    a real `gh` for a repo (`acme/cc`) that doesn't exist."""

    def _boom(*_a, **_kw):  # pragma: no cover - only runs on regression
        raise AssertionError(
            "a test reached the real github_ops.branch_commits_ahead_for_"
            "assignment default — pass commits_ahead_fn= explicitly instead "
            "of hitting the network from a unit test"
        )

    monkeypatch.setattr(
        "coord.github_ops.branch_commits_ahead_for_assignment", _boom
    )


def _config() -> Config:
    return Config(
        repos=[Repo(name="cc", github="acme/cc")],
        machines=[Machine(name="dellserver", host="dellserver", repos=["cc"])],
    )


def _running(
    aid: str = "w1",
    *,
    atype: str = "work",
    branch: str | None = "issue-2208-x",
    provider_name: str | None = None,
    dispatched_at: float | None = None,
    review_of: str | None = None,
) -> Assignment:
    """A `running` board row, dispatched long enough ago to clear the
    `_NO_RECORD_GRACE_SECONDS` window unless a test says otherwise."""
    return Assignment(
        machine_name="dellserver", repo_name="cc",
        issue_number=2208, issue_title="t",
        status="running", assignment_id=aid, type=atype, branch=branch,
        provider_name=provider_name,
        review_of_assignment_id=review_of,
        dispatched_at=(
            time.time() - 3600.0 if dispatched_at is None else dispatched_at
        ),
    )


def _board(*assignments: Assignment) -> Board:
    return Board(
        repos=[Repo(name="cc", github="acme/cc")], machines=[],
        active=list(assignments),
    )


class _Recorder:
    """Stand-in for issue_store._update_local_state."""

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
    """A well-formed `/status` payload — BOTH keys, like a real agent."""
    return {"active": list(active or []), "completed": list(completed or [])}


def _zero_ahead(*_a, **_kw) -> int:
    """Stub `commits_ahead_fn` (#2553): "nothing pushed" — the pre-#2553
    default. Passed explicitly by every test below that doesn't itself
    exercise the branch-has-work half, so the no-record arm never shells out
    to a real `gh` for a repo (`acme/cc`) that doesn't exist."""
    return 0


# ── the fix ────────────────────────────────────────────────────────────────


def test_row_in_neither_list_is_reconciled_to_failed_with_pinned_reason() -> None:
    """Acceptance 1: in neither `active` nor `completed` on a REACHABLE agent
    → `failed`, with a reason naming the cause."""
    rec = _Recorder()
    out = reconcile_completed_assignments(
        _config(),
        board=_board(_running("ghost")),
        agent_status_fn=lambda host: _status(
            active=[{"id": "someone-else"}],
            completed=[{"id": "older", "status": "done"}],
        ),
        update_state_fn=rec,
        capture_plan=False,
        commits_ahead_fn=_zero_ahead,
    )

    assert len(rec.calls) == 1
    assert rec.calls[0] == {
        "assignment_id": "ghost",
        "terminal_status": "failed",
        "branch": "issue-2208-x",
        "review_state": None,
        "failure_reason": NO_AGENT_RECORD_REASON,
        "exit_code": None,
    }
    assert out == [
        {
            "assignment_id": "ghost",
            "issue_number": 2208,
            "repo": "cc",
            "type": "work",
            "to_status": "failed",
            "plan_captured": False,
            "reason": NO_AGENT_RECORD_REASON,
        }
    ]


def test_reason_string_is_pinned() -> None:
    """The reason is the operator-facing explanation for a terminal status
    reached with no worker verdict — it must keep naming the cause."""
    assert "no record of this assignment" in NO_AGENT_RECORD_REASON
    assert "#2275" in NO_AGENT_RECORD_REASON


def test_never_flips_to_done() -> None:
    """A silent flip to `done` on a leg that never reported a verdict would
    manufacture a pass, which is far worse than the stall it replaces."""
    rec = _Recorder()
    reconcile_completed_assignments(
        _config(),
        board=_board(
            _running("g1", atype="work"),
            _running("g2", atype="review"),
            _running("g3", atype="plan"),
        ),
        agent_status_fn=lambda host: _status(),
        update_state_fn=rec,
        capture_plan=False,
        commits_ahead_fn=_zero_ahead,
    )
    assert [c["terminal_status"] for c in rec.calls] == ["failed", "failed", "failed"]


def test_empty_agent_reconciles_every_row_on_that_machine() -> None:
    """The state-loss case (an agent whose state file was lost/corrupt, so
    `_load_state` recovered nothing): every row on that machine is in neither
    list, and the workers really are gone.  Reconciling them all is the
    deliberate decision, not an accident — a normal `coord agent update`
    restart does NOT land here, because `_load_state` rewrites in-flight rows
    to `failed` and serves them in `completed` (see the next test)."""
    rec = _Recorder()
    out = reconcile_completed_assignments(
        _config(),
        board=_board(_running("a1"), _running("a2"), _running("a3")),
        agent_status_fn=lambda host: _status(),
        update_state_fn=rec,
        capture_plan=False,
        commits_ahead_fn=_zero_ahead,
    )
    assert {c["assignment_id"] for c in rec.calls} == {"a1", "a2", "a3"}
    assert len(out) == 3


def test_restart_recovered_row_takes_the_ordinary_path_not_the_no_record_arm() -> None:
    """An agent restart with its state file intact reports the lost worker in
    `completed` with a real reason (`AgentServer._load_state` rewrites every
    pending/running assignment to FAILED before the first `/status` is
    served), so the ordinary #625 path handles it and the no-record arm never
    sees it."""
    rec = _Recorder()
    reconcile_completed_assignments(
        _config(),
        board=_board(_running("w1")),
        agent_status_fn=lambda host: _status(
            completed=[
                {
                    "id": "w1",
                    "status": "failed",
                    "error": "agent restarted; subprocess lost",
                    "exit_code": 1,
                }
            ],
        ),
        update_state_fn=rec,
        capture_plan=False,
    )
    assert rec.calls[0]["terminal_status"] == "failed"
    # NOT the #2275 reason — the agent had a record and gave a real one.
    assert rec.calls[0]["failure_reason"] != NO_AGENT_RECORD_REASON
    assert rec.calls[0]["exit_code"] == 1


# ── #2553: consult the row's own branch before guessing `failed` ───────────
#
# coord-portal#129, assignment c2120f7206ec: a `test-author` leg pushed 948
# lines to `test-author-ms-4-slice-129` and then the agent forgot about it.
# The no-record arm recorded `failed` without ever looking at the branch it
# was already holding the name of, stranding real work behind a sealed path
# nothing else would ever pick back up.


def test_branch_with_pushed_commits_is_advisory_not_failed() -> None:
    """Acceptance half 1: agent forgot, but the branch has commits ahead of
    its base → `advisory`, not `failed` — the work must not be discarded."""
    rec = _Recorder()
    out = reconcile_completed_assignments(
        _config(),
        board=_board(_running("ta1", atype="test-author", branch="test-author-ms-4-slice-129")),
        agent_status_fn=lambda host: _status(),
        update_state_fn=rec,
        capture_plan=False,
        commits_ahead_fn=lambda a, cfg: 11,
    )

    assert len(rec.calls) == 1
    assert rec.calls[0]["terminal_status"] == "advisory"
    assert rec.calls[0]["branch"] == "test-author-ms-4-slice-129"
    assert rec.calls[0]["exit_code"] is None
    reason = rec.calls[0]["failure_reason"]
    assert reason != NO_AGENT_RECORD_REASON
    # The operator-facing reason must name the branch — that's the whole
    # acceptance bar: an operator reading `coord status` shouldn't have to
    # go spelunking for which branch carries the stranded work.
    assert "test-author-ms-4-slice-129" in reason
    assert "11" in reason
    assert "#2553" in reason
    assert out[0]["to_status"] == "advisory"
    assert out[0]["reason"] == reason


def test_branch_with_zero_commits_ahead_is_still_failed() -> None:
    """Acceptance half 2: agent forgot, and nothing was ever pushed → the
    pre-#2553 `failed` + pinned reason, unchanged."""
    rec = _Recorder()
    out = reconcile_completed_assignments(
        _config(),
        board=_board(_running("w1", atype="work")),
        agent_status_fn=lambda host: _status(),
        update_state_fn=rec,
        capture_plan=False,
        commits_ahead_fn=lambda a, cfg: 0,
    )

    assert rec.calls[0]["terminal_status"] == "failed"
    assert rec.calls[0]["failure_reason"] == NO_AGENT_RECORD_REASON
    assert out[0]["to_status"] == "failed"


def test_unknown_commits_ahead_fails_closed_to_failed() -> None:
    """An unconfirmable branch state (network hiccup, unknown repo, ...)
    must not be guessed as `advisory` — that would be manufacturing a
    "provenance unverified" verdict on no evidence at all. Fails closed to
    the pre-#2553 `failed`, matching this arm's existing polarity."""
    rec = _Recorder()
    reconcile_completed_assignments(
        _config(),
        board=_board(_running("w1", atype="work")),
        agent_status_fn=lambda host: _status(),
        update_state_fn=rec,
        capture_plan=False,
        commits_ahead_fn=lambda a, cfg: None,
    )
    assert rec.calls[0]["terminal_status"] == "failed"
    assert rec.calls[0]["failure_reason"] == NO_AGENT_RECORD_REASON


def test_commits_ahead_fn_raising_fails_closed_to_failed() -> None:
    """A network error from the branch check must not crash the passive
    daemon tick — caught and treated as unknown, same as a `None` return."""
    rec = _Recorder()
    reconcile_completed_assignments(
        _config(),
        board=_board(_running("w1", atype="work")),
        agent_status_fn=lambda host: _status(),
        update_state_fn=rec,
        capture_plan=False,
        commits_ahead_fn=lambda a, cfg: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert rec.calls[0]["terminal_status"] == "failed"
    assert rec.calls[0]["failure_reason"] == NO_AGENT_RECORD_REASON


def test_branch_check_is_scoped_to_work_like_types() -> None:
    """A `smoke`/`review` row's `branch` is inherited verbatim from the work
    it rides on (see `coord.smoke.dispatch_pending_smoke`), so it is almost
    always "ahead" for reasons that have nothing to do with what THIS leg
    produced. The branch check must not run for these types — checked here
    by making the stub explode if it's ever called for a non-WORK_LIKE_TYPES
    row, which would otherwise misroute the #2208 smoke path into
    `advisory` instead of its environmental `failed` clear."""

    def _boom(a, cfg):  # pragma: no cover - only runs on regression
        raise AssertionError(
            f"commits_ahead_fn was called for a.type={a.type!r}, which is "
            "not in WORK_LIKE_TYPES"
        )

    rec = _Recorder()
    out = reconcile_completed_assignments(
        _config(),
        board=_board(
            _running("smoke-1", atype="smoke", review_of="work-1"),
            _running("review-1", atype="review"),
        ),
        agent_status_fn=lambda host: _status(),
        update_state_fn=rec,
        capture_plan=False,
        commits_ahead_fn=_boom,
    )
    assert [c["terminal_status"] for c in rec.calls] == ["failed", "failed"]
    assert [o["to_status"] for o in out] == ["failed", "failed"]


def test_no_branch_recorded_is_still_failed() -> None:
    """A row with no branch at all has nothing to consult — straight to the
    pre-#2553 `failed` path without ever calling `commits_ahead_fn`."""

    def _boom(a, cfg):  # pragma: no cover - only runs on regression
        raise AssertionError("commits_ahead_fn was called with no branch")

    rec = _Recorder()
    reconcile_completed_assignments(
        _config(),
        board=_board(_running("w1", atype="work", branch=None)),
        agent_status_fn=lambda host: _status(),
        update_state_fn=rec,
        capture_plan=False,
        commits_ahead_fn=_boom,
    )
    assert rec.calls[0]["terminal_status"] == "failed"
    assert rec.calls[0]["failure_reason"] == NO_AGENT_RECORD_REASON


# ── guard rails ────────────────────────────────────────────────────────────


def test_row_in_active_is_left_untouched() -> None:
    """Acceptance 2: still running on the agent → leave it.  This is the case
    the old `continue` was RIGHT about, and the one this change must not
    break."""
    rec = _Recorder()
    out = reconcile_completed_assignments(
        _config(),
        board=_board(_running("live")),
        agent_status_fn=lambda host: _status(
            active=[{"id": "live", "status": "running"}],
        ),
        update_state_fn=rec,
        capture_plan=False,
    )
    assert rec.calls == []
    assert out == []


def test_attended_session_is_never_reaped() -> None:
    """Acceptance 3, PINNED HARDEST.  An interactive session is a tmux/PTY
    launch, not an agent subprocess: it appears in NEITHER list for its whole
    life.  Reaping one kills a human's live work — the #1658 failure mode."""
    rec = _Recorder()
    out = reconcile_completed_assignments(
        _config(),
        board=_board(
            # `--interactive` work/review/smoke/fix sessions: same `type` as
            # their headless counterparts, told apart only by provider_name.
            _running("i-work", atype="work", provider_name="claude-pty"),
            _running("i-review", atype="review", provider_name="claude-pty"),
            _running("i-smoke", atype="smoke", provider_name="claude-pty",
                     review_of="parent-1"),
            _running("i-merge", atype="conflict-fix", provider_name="claude-pty",
                     review_of="parent-2"),
            # the chat/troubleshoot family: no headless counterpart at all.
            _running("i-chat", atype="chat"),
            _running("i-trouble", atype="troubleshoot"),
            _running("i-audit", atype="audit"),
            _running("i-refine", atype="refinement"),
        ),
        agent_status_fn=lambda host: _status(),  # agent knows about none of them
        update_state_fn=rec,
        capture_plan=False,
    )
    assert rec.calls == [], (
        "an attended session was reaped by the no-record arm — this kills live "
        "human work (#1658)"
    )
    assert out == []


def test_is_attended_session_predicate() -> None:
    assert is_attended_session(_running("x", provider_name="claude-pty"))
    assert is_attended_session(_running("x", atype="chat"))
    assert is_attended_session(_running("x", atype="milestone-chat"))
    assert not is_attended_session(_running("x", atype="work"))
    assert not is_attended_session(_running("x", atype="smoke"))
    assert not is_attended_session(_running("x", atype="review"))


def test_unreachable_agent_changes_nothing() -> None:
    """Acceptance 4: no record and no answer are different things.  The
    fail-open `if not status` short-circuit must stay unweakened."""
    for unreachable in (None, {}):
        rec = _Recorder()
        out = reconcile_completed_assignments(
            _config(),
            board=_board(_running("w1")),
            agent_status_fn=lambda host, _u=unreachable: _u,
            update_state_fn=rec,
            capture_plan=False,
        )
        assert rec.calls == []
        assert out == []


def test_payload_without_an_active_key_changes_nothing() -> None:
    """Fail-open on a payload that can't support the disproof.  Without
    `active` we would be inferring from silence again — exactly the bug — so
    an older agent build degrades to pre-#2275 behaviour, not to a reap."""
    rec = _Recorder()
    out = reconcile_completed_assignments(
        _config(),
        board=_board(_running("w1")),
        agent_status_fn=lambda host: {"completed": []},
        update_state_fn=rec,
        capture_plan=False,
    )
    assert rec.calls == []
    assert out == []


def test_grace_window_defers_a_just_dispatched_row() -> None:
    """A row dispatched seconds ago is left for the next tick — belt to the
    positive disproof's braces, for the `AgentServer.assign()` window between
    worktree setup and the `_assignments` insert."""
    rec = _Recorder()
    out = reconcile_completed_assignments(
        _config(),
        board=_board(_running("fresh", dispatched_at=time.time() - 5.0)),
        agent_status_fn=lambda host: _status(),
        update_state_fn=rec,
        capture_plan=False,
        commits_ahead_fn=_zero_ahead,
    )
    assert rec.calls == []
    assert out == []

    # ...and the same row IS reconciled once it is past the window.
    rec2 = _Recorder()
    reconcile_completed_assignments(
        _config(),
        board=_board(_running("fresh", dispatched_at=time.time() - 600.0)),
        agent_status_fn=lambda host: _status(),
        update_state_fn=rec2,
        capture_plan=False,
        commits_ahead_fn=_zero_ahead,
    )
    assert [c["assignment_id"] for c in rec2.calls] == ["fresh"]


def test_row_with_no_dispatched_at_is_eligible() -> None:
    """The grace window guards a dispatch-time race; a row with no dispatch
    time recorded is not in one."""
    rec = _Recorder()
    reconcile_completed_assignments(
        _config(),
        board=_board(_running("nodate", dispatched_at=0.0)),
        agent_status_fn=lambda host: _status(),
        update_state_fn=rec,
        capture_plan=False,
        commits_ahead_fn=_zero_ahead,
    )
    assert [c["assignment_id"] for c in rec.calls] == ["nodate"]


def test_agent_has_no_record_predicate() -> None:
    payload = _status(active=[{"id": "a"}], completed=[{"id": "b"}])
    assert not agent_has_no_record(payload, "a")
    assert not agent_has_no_record(payload, "b")
    assert agent_has_no_record(payload, "c")
    # fail-open shapes
    assert not agent_has_no_record({"completed": []}, "c")
    assert not agent_has_no_record({"active": []}, "c")
    assert not agent_has_no_record({}, "c")


def test_arm_stays_passive_no_dispatch_no_github(monkeypatch) -> None:
    """#1616's contract: this function reflects state, it never advances the
    pipeline.  The no-record arm must not become the exception.

    #2553's branch check is a READ (`commits_ahead_fn`), not a mutation, and
    it only decides between two non-dispatching terminal statuses — it does
    not violate this contract, so it is stubbed here (like `agent_status_fn`
    always has been) rather than asserted against. What must still never
    happen is a dispatch (`_reassign`) or a posted comment (`httpx.post`)."""
    import coord.reconcile as rec_mod

    def _boom(*a, **kw):  # pragma: no cover - only runs on regression
        raise AssertionError("the passive reconcile dispatched something")

    monkeypatch.setattr(rec_mod, "_reassign", _boom)
    monkeypatch.setattr("httpx.post", _boom)

    rec = _Recorder()
    reconcile_completed_assignments(
        _config(),
        board=_board(_running("ghost")),
        agent_status_fn=lambda host: _status(),
        update_state_fn=rec,
        capture_plan=False,
        commits_ahead_fn=_zero_ahead,
    )
    assert rec.calls[0]["terminal_status"] == "failed"


# ── #2208's exact shape, end to end ────────────────────────────────────────


def test_2208_smoke_row_clears_without_a_human(monkeypatch) -> None:
    """Acceptance 6, the worked example: a `[smoke]` row `running` next to a
    work row reading `test_state=passed`, with the agent reporting neither.

    Asserts both halves clear: the smoke row goes terminal AND the parent
    work row's `test_state` is resolved — leaving the parent on the `running`
    non-verdict marker (#1395) would fix half of #2208 and strand the other
    half exactly as before.

    The parent's verdict is CLEARED (``test_state=None``), not recorded as a
    test failure: a vanished worker is the machine's fault, so
    `dispatch_pending_smoke` re-runs the Test stage on its next tick instead
    of `coord fix` burning a bounded retry round on a branch that is green.
    """
    verdicts: list[dict] = []
    monkeypatch.setattr(
        "coord.state.record_test_verdict",
        lambda **kw: verdicts.append(kw),
    )
    monkeypatch.setattr("coord.state.load_assignment_test_reason", lambda aid: None)

    work = Assignment(
        machine_name="dellserver", repo_name="cc",
        issue_number=2208, issue_title="green branch",
        status="done", assignment_id="work-1", type="work",
        branch="issue-2208-x", test_state="running",
        dispatched_at=time.time() - 43200.0,
    )
    smoke = _running(
        "smoke-1", atype="smoke", review_of="work-1",
        dispatched_at=time.time() - 39600.0,  # 11 hours, like #2208
    )

    rec = _Recorder()
    out = reconcile_completed_assignments(
        _config(),
        board=Board(
            repos=[Repo(name="cc", github="acme/cc")], machines=[],
            active=[smoke], completed=[work],
        ),
        # the agent has no record of the smoke leg — it died 11h ago and its
        # id rolled off the 25-entry history long since.
        agent_status_fn=lambda host: _status(
            completed=[{"id": f"other-{i}", "status": "done"} for i in range(25)],
        ),
        update_state_fn=rec,
        capture_plan=False,
    )

    assert [c["assignment_id"] for c in rec.calls] == ["smoke-1"]
    assert rec.calls[0]["terminal_status"] == "failed"
    assert rec.calls[0]["failure_reason"] == NO_AGENT_RECORD_REASON
    assert out[0]["to_status"] == "failed"

    # the parent work row is resolved, and cleared for automatic re-dispatch.
    assert len(verdicts) == 1
    assert verdicts[0]["assignment_id"] == "work-1"
    assert verdicts[0]["test_state"] is None, (
        "a vanished Test-stage worker must not be recorded as a work failure "
        "— that spends a `coord fix` round on a green branch (#2208)"
    )
    assert "no record of this assignment" in verdicts[0]["test_reason"]


def test_fanout_leg_with_no_agent_record_self_records_and_folds(monkeypatch) -> None:
    """#4143: a #3182 capability-partition fan-out leg the agent has no
    record of at all must still self-record onto ITS OWN row (never
    straight onto the shared parent — the #1797 racing-sibling-writes
    shape) and have `finalize_smoke_fanout` called against the shared
    parent immediately afterward, exactly like the ordinary `terminal ==
    "failed"` path does."""
    from coord.smoke import smoke_leg_issue_title  # noqa: PLC0415

    verdicts: list[dict] = []
    monkeypatch.setattr(
        "coord.state.record_test_verdict",
        lambda **kw: verdicts.append(kw),
    )
    monkeypatch.setattr("coord.state.load_assignment_test_reason", lambda aid: None)
    monkeypatch.setattr("coord.state.load_assignment_test_state", lambda aid: None)
    finalize_calls: list[str] = []
    monkeypatch.setattr(
        "coord.smoke.finalize_smoke_fanout",
        lambda work_parent_id: finalize_calls.append(work_parent_id),
    )

    work = Assignment(
        machine_name="dellserver", repo_name="cc",
        issue_number=2208, issue_title="green branch",
        status="done", assignment_id="work-1", type="work",
        branch="issue-2208-x", test_state="running",
        dispatched_at=time.time() - 43200.0,
    )
    leg = Assignment(
        machine_name="dellserver", repo_name="cc",
        issue_number=2208, issue_title=smoke_leg_issue_title("t", ("macos",)),
        status="running", assignment_id="leg-macos", type="smoke",
        branch="issue-2208-x", review_of_assignment_id="work-1",
        dispatched_at=time.time() - 39600.0,
    )

    rec = _Recorder()
    out = reconcile_completed_assignments(
        _config(),
        board=Board(
            repos=[Repo(name="cc", github="acme/cc")], machines=[],
            active=[leg], completed=[work],
        ),
        agent_status_fn=lambda host: _status(
            completed=[{"id": f"other-{i}", "status": "done"} for i in range(25)],
        ),
        update_state_fn=rec,
        capture_plan=False,
    )

    assert [c["assignment_id"] for c in rec.calls] == ["leg-macos"]
    assert out[0]["to_status"] == "failed"

    # The #3315 budget tracking write lands on the persistent parent; the
    # leg's OWN verdict is a separate write, never a direct overwrite of the
    # shared parent's test_state with this leg's result.
    leg_verdicts = [v for v in verdicts if v["assignment_id"] == "leg-macos"]
    assert len(leg_verdicts) == 1, (
        "the leg's own verdict must land on its own row, never straight "
        f"onto the shared parent — got {verdicts!r}"
    )
    assert leg_verdicts[0]["test_state"] is None
    assert finalize_calls == ["work-1"], (
        "finalize_smoke_fanout must be called against the shared parent "
        f"right after the leg's own verdict is recorded — got {finalize_calls!r}"
    )


# ── history eviction, driven through a real AgentServer ────────────────────


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for cmd in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "t@t.com"],
        ["git", "config", "user.name", "Test"],
    ):
        subprocess.run(cmd, cwd=str(path), check=True, capture_output=True)
    (path / "README").write_text("init\n")
    subprocess.run(["git", "add", "README"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"], cwd=str(path), check=True, capture_output=True,
    )
    return path


def test_history_eviction_end_to_end(tmp_path: Path) -> None:
    """Acceptance 5: seed a terminal assignment, push `_COMPLETED_HISTORY_CAP
    + 1` completions past it, assert the row still reconciles.

    Driven through a REAL `AgentServer` and its real `/status` payload rather
    than a hand-written dict, because the eviction — not the payload shape —
    is the mechanism that makes #2275 a certainty rather than a race.
    """
    from coord.agent import (
        _COMPLETED_HISTORY_CAP,
        DONE,
        AgentAssignment,
        AgentServer,
        AssignmentSpec,
    )

    repo = _init_repo(tmp_path / "repo")
    server = AgentServer(
        machine_name="dellserver",
        capabilities=["python"],
        repos=["cc"],
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/sh", "-c", "true"],
        repo_paths={"cc": str(repo)},
    )
    spec = AssignmentSpec(
        repo_name="cc", repo_path=str(repo), issue_number=2208,
        issue_title="t", briefing="b", branch="main",
    )

    # The leg that died, seeded as the OLDEST terminal entry.
    server._assignments["victim"] = AgentAssignment(
        id="victim", spec=spec, status=DONE,
        started_at=0.0, finished_at=0.0, exit_code=0,
    )
    assert any(e["id"] == "victim" for e in server.list_assignments()["completed"])

    # Now push cap+1 newer completions past it.
    for i in range(_COMPLETED_HISTORY_CAP + 1):
        server._assignments[f"newer{i:04d}"] = AgentAssignment(
            id=f"newer{i:04d}", spec=spec, status=DONE,
            started_at=float(i + 1), finished_at=float(i + 1), exit_code=0,
        )
    server._persist()

    payload = server.list_assignments()
    assert not any(e["id"] == "victim" for e in payload["completed"]), (
        "the seeded assignment should have been evicted by the history cap"
    )
    assert not any(e["id"] == "victim" for e in payload["active"])
    # ...and the persisted state file agrees (this is what survives a restart).
    state = json.loads(server.state_path.read_text())
    assert not any(a["id"] == "victim" for a in state["assignments"])

    rec = _Recorder()
    out = reconcile_completed_assignments(
        _config(),
        board=_board(_running("victim", atype="work")),
        agent_status_fn=lambda host: payload,
        update_state_fn=rec,
        capture_plan=False,
        commits_ahead_fn=_zero_ahead,
    )
    server.shutdown()

    assert [c["assignment_id"] for c in rec.calls] == ["victim"], (
        "a row whose completion rolled off the capped history was skipped "
        "forever — the #2275 hole"
    )
    assert rec.calls[0]["terminal_status"] == "failed"
    assert rec.calls[0]["failure_reason"] == NO_AGENT_RECORD_REASON
    assert out[0]["to_status"] == "failed"
