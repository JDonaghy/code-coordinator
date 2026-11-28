"""#4212: the drive's Test-failure edge must dispatch the #4094 mechanic leg
even once the ordinary fix-round ceiling is already reached.

#4095 wired `coord.drive._decide_test` to route a `mechanical` triage verdict
to `coord mechanic-dispatch` (which, in turn, reaches
`coord.board_doctor_permissions.sweep_dispatch_mechanic_leg` via
`coord.board_doctor_policy.dispatch_mechanic_leg_for_triage`) — but only
BEFORE the fix-round ceiling check. A mechanical Test failure discovered on
the fix round that happens to hit `max_fix_rounds` still died outright
(claude-coordinator#4202), never getting the bounded, budget-free mechanic
leg a `mechanical` verdict is supposed to earn.

Reuses `tests/test_drive.py`'s `FakeVerifier`/`step`/`done_work` helpers
directly, the same sibling-module pattern `tests/test_drive_machine_fault.py`
already uses — this is deliberately not inlined into that (already very
large) file.
"""

from __future__ import annotations

import coord.failure_triage as failure_triage
from coord.drive import DriveCounters, DriveOptions
from tests.test_drive import done_work, step, FakeVerifier

MECHANICAL_VERDICT = failure_triage.TriageVerdict(
    bucket=failure_triage.BUCKET_MECHANICAL,
    diagnosis="ratchet off by one in tests/test_foo.py",
    repro_capability=None,
    proposed_scope=failure_triage.ProposedScope(
        files=("tests/test_foo.py",), estimated_line_delta=1,
    ),
    confidence=0.95,
    via="model",
)

SUBSTANTIVE_VERDICT = failure_triage.TriageVerdict(
    bucket=failure_triage.BUCKET_SUBSTANTIVE,
    diagnosis="a real design disagreement in the diff",
    repro_capability=None,
    proposed_scope=failure_triage.ProposedScope(),
    confidence=0.8,
    via="model",
)


def _at_ceiling_counters(sha: str, max_fix_rounds: int = 2) -> tuple[DriveCounters, DriveOptions]:
    """A #4202-shaped drive state: `max_fix_rounds` fix rounds already spent
    at a PRIOR sha, and the #3714 retry latch already spent at *sha* too —
    exactly the state a third, mechanical Test failure at a fresh sha would
    find waiting for it."""
    counters = DriveCounters(fix_rounds=max_fix_rounds)
    opts = DriveOptions(machine="precision", max_fix_rounds=max_fix_rounds)
    return counters, opts


def test_mechanical_verdict_dispatches_the_mechanic_leg_past_the_ceiling():
    """#4212 acceptance: a replay of the #4202 shape — the fix-round ceiling
    is already reached, the triage verdict is `mechanical` — dispatches one
    mechanic leg within this same tick instead of dying. The fix-round
    counter is left exactly where the ceiling put it."""
    sha = "ccc"
    counters, opts = _at_ceiling_counters(sha)
    v = FakeVerifier(head_sha=sha, triage_verdict=MECHANICAL_VERDICT)
    s = done_work(work_test_state="failed", work_test_reason="ratchet off by one")

    action = step(s, opts, counters=counters, verifier=v)

    assert not action.is_exit
    assert action.command[0] == "mechanic-dispatch"
    assert counters.fix_rounds == opts.max_fix_rounds
    assert counters.test_retry_sha == sha


def test_substantive_verdict_still_dies_at_the_ceiling():
    """A `substantive` verdict gets no ceiling bypass — the row still dies
    exactly as it did before #4212."""
    sha = "ccc"
    counters, opts = _at_ceiling_counters(sha)
    v = FakeVerifier(head_sha=sha, triage_verdict=SUBSTANTIVE_VERDICT)
    s = done_work(work_test_state="failed", work_test_reason="3 failed")

    action = step(s, opts, counters=counters, verifier=v)

    assert action.is_exit
    assert f"after {opts.max_fix_rounds} fix round(s)" in action.message


def test_no_verdict_still_dies_at_the_ceiling():
    """#4212 must not change the pre-existing ceiling behaviour for the
    degraded `None`-verdict case either — only `mechanical` bypasses it."""
    sha = "ccc"
    counters, opts = _at_ceiling_counters(sha)
    v = FakeVerifier(head_sha=sha, triage_bucket=None)
    s = done_work(work_test_state="failed", work_test_reason="3 failed")

    action = step(s, opts, counters=counters, verifier=v)

    assert action.is_exit
    assert f"after {opts.max_fix_rounds} fix round(s)" in action.message


def test_second_failure_at_the_same_sha_does_not_redispatch_a_mechanic_leg():
    """#4212 acceptance: a second Test failure at the SAME head sha, still
    at the ceiling, does not dispatch a second mechanic leg — it dies,
    exactly like any other exhausted-ceiling row, without re-running the
    triage classifier a second time for this sha."""
    sha = "ccc"
    counters, opts = _at_ceiling_counters(sha)
    v = FakeVerifier(head_sha=sha, triage_verdict=MECHANICAL_VERDICT)
    s = done_work(work_test_state="failed", work_test_reason="ratchet off by one")

    first = step(s, opts, counters=counters, verifier=v)
    assert first.command[0] == "mechanic-dispatch"

    # Simulate the drive warning on a decline (`on_error="warn"`) and
    # polling again — nothing on the board changed, so `test_state` is
    # still "failed" at the same sha.
    second = step(s, opts, counters=counters, verifier=v)

    assert second.is_exit
    assert f"after {opts.max_fix_rounds} fix round(s)" in second.message
    assert v.triage_test_failure_calls == 1
    assert counters.fix_rounds == opts.max_fix_rounds


def test_mechanical_verdict_below_the_ceiling_is_unaffected():
    """#4212 must not change the ALREADY-correct below-ceiling behaviour
    #4095 shipped: a `mechanical` verdict with fix rounds still available
    dispatches the mechanic leg exactly as before, spending neither the
    retry latch's sibling budget nor a fix round."""
    sha = "aaa"
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_fix_rounds=2)
    v = FakeVerifier(head_sha=sha, triage_verdict=MECHANICAL_VERDICT)
    s = done_work(work_test_state="failed", work_test_reason="ratchet off by one")

    action = step(s, opts, counters=counters, verifier=v)

    assert action.command[0] == "mechanic-dispatch"
    assert counters.fix_rounds == 0
