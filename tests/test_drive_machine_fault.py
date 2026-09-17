"""#3367: `coord drive`'s review/work failed-retry arms must not charge a
machine fault to the issue's retry budget.

Reuses `tests/test_drive.py`'s `state`/`step`/`work_tested`/`done_work`
helpers and fixtures directly — this is deliberately a sibling module, not
inline in that (already 7,600+ line) file, so the #3367 behaviour has its
own compact, greppable home.

The repro this closes: precision's dead OAuth credential produced four
identical review-worker deaths — `'Failed to authenticate: OAuth session
expired and could not be refreshed'`, 0.1s, 1 turn, $0.00 — and every one
was charged to `review_retries`, which is the exact counter `_die()` reports
in `review <aid> failed N retr(ies) in: ...` once the (unrelated) budget for
GENUINE work failures runs out.
"""

from __future__ import annotations

from coord import machine_fault, machine_pause
from coord.drive import (
    EXIT_TERMINAL_FAILURE,
    RUN,
    DriveCounters,
    DriveOptions,
)
from tests.test_drive import done_work, state, step, work_tested

AUTH_FAILURE_TEXT = (
    "Failed to authenticate: OAuth session expired and could not be refreshed"
)


# ═══════════════════════════════════════════════════════════════════════════
# review-side
# ═══════════════════════════════════════════════════════════════════════════


def test_an_auth_failed_review_redispatches_without_spending_review_retries():
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_work_retries=1)
    s = work_tested(
        review_aid="r1",
        review_status="failed",
        review_machine="precision",
        review_failure_reason=AUTH_FAILURE_TEXT,
        review_num_turns=1,
        review_cost_usd=0.0,
    )
    action = step(s, opts, counters=counters)
    assert action.kind == RUN
    assert action.command == ("review", "w1")
    # The whole point: the issue's own review-retry budget is untouched.
    assert counters.review_retries == 0
    assert counters.review_machine_fault_retries == 1
    assert any("machine fault" in w for w in action.warnings)
    assert any("precision" in w for w in action.warnings)


def test_an_instant_zero_cost_review_failure_is_also_a_machine_fault():
    """No auth text at all — the generic "1 turn / $0" shape alone must be
    enough, per #3367's own framing ("four identical instances is not
    ambiguity... regardless of the error text")."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_work_retries=1)
    s = work_tested(
        review_aid="r1",
        review_status="failed",
        review_machine="precision",
        review_failure_reason="worker process exited",
        review_num_turns=0,
        review_cost_usd=0.0,
    )
    action = step(s, opts, counters=counters)
    assert action.kind == RUN
    assert counters.review_retries == 0
    assert counters.review_machine_fault_retries == 1


def test_a_genuine_review_failure_still_spends_the_ordinary_budget():
    """Regression guard: a review that made a real attempt (turns/cost look
    like an actual run) must NOT be reclassified as a machine fault — the
    pre-#3367 bounded retry (and eventual `_die()`) still applies."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_work_retries=1)
    s = work_tested(
        review_aid="r1",
        review_status="failed",
        review_machine="precision",
        review_failure_reason="529 Overloaded",
        review_num_turns=12,
        review_cost_usd=0.35,
    )
    action = step(s, opts, counters=counters)
    assert action.kind == RUN
    assert action.command == ("review", "w1")
    assert counters.review_retries == 1
    assert counters.review_machine_fault_retries == 0


def test_repeated_auth_failures_on_the_same_machine_auto_pause_it():
    """#3367's own evidence, mechanised: repeated identical auth failures on
    `precision` must pull it out of the routing pool no later than
    `machine_fault.AUTO_PAUSE_THRESHOLD` retries — not "never", which is
    what happened for real before this fix (four dispatches, no pause,
    stayed `online • idle`)."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_work_retries=1)
    s = work_tested(
        review_aid="r1",
        review_status="failed",
        review_machine="precision",
        review_failure_reason=AUTH_FAILURE_TEXT,
        review_num_turns=1,
        review_cost_usd=0.0,
    )
    assert "precision" not in machine_pause.local_paused_set()
    for _ in range(machine_fault.AUTO_PAUSE_THRESHOLD):
        action = step(s, opts, counters=counters)
        assert action.kind == RUN
    assert "precision" in machine_pause.local_paused_set()
    # The issue's own budget is STILL untouched after the auto-pause fired.
    assert counters.review_retries == 0


def test_a_machine_fault_that_outlives_its_own_local_budget_still_dies():
    """Worst case: the auto-pause never lands (thin-client transport blip,
    a race) and the SAME broken machine keeps getting picked. This must
    still terminate with a clear diagnosis rather than looping forever —
    bounded by `_MACHINE_FAULT_RETRY_BUDGET`, a counter independent of the
    issue's own `review_retries`."""
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_work_retries=1)
    s = work_tested(
        review_aid="r1",
        review_status="failed",
        review_machine="precision",
        review_failure_reason=AUTH_FAILURE_TEXT,
        review_num_turns=1,
        review_cost_usd=0.0,
    )
    action = None
    for _ in range(20):
        action = step(s, opts, counters=counters)
        if action.is_exit:
            break
    assert action.is_exit
    assert action.exit_code == EXIT_TERMINAL_FAILURE
    assert "machine-fault" in action.message
    # Still never spent the issue's own budget, even on the terminal path.
    assert counters.review_retries == 0


def test_a_completed_review_clears_a_stale_fault_streak():
    """A review that actually finished (worker ran to completion) is proof
    the machine works — a fault streak recorded against it earlier (e.g. a
    since-fixed credential) must not linger forever in `coord status`."""
    machine_fault.record_fault("precision", "machine fault (auth): dead OAuth")
    machine_fault.record_fault("precision", "machine fault (auth): dead OAuth")
    assert machine_fault.consecutive_faults("precision") == 2

    s = work_tested(
        review_aid="r1", review_status="done", review_verdict="approve",
        review_machine="precision",
    )
    step(s, DriveOptions(machine="precision"))
    assert machine_fault.consecutive_faults("precision") == 0


# ═══════════════════════════════════════════════════════════════════════════
# work-side
# ═══════════════════════════════════════════════════════════════════════════


def test_an_auth_failed_work_leg_redispatches_without_spending_work_retries():
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_work_retries=1)
    s = done_work(
        work_status="failed",
        work_machine="precision",
        work_failure_reason=AUTH_FAILURE_TEXT,
        work_num_turns=1,
        work_cost_usd=0.0,
    )
    action = step(s, opts, counters=counters)
    assert action.kind == RUN
    assert action.command == ("retry", "w1")
    assert counters.work_retries == 0
    assert counters.work_machine_fault_retries == 1
    assert any("machine fault" in w for w in action.warnings)


def test_a_genuine_work_failure_still_spends_the_ordinary_budget():
    counters = DriveCounters()
    opts = DriveOptions(machine="precision", max_work_retries=1)
    s = done_work(
        work_status="failed",
        work_machine="precision",
        work_failure_reason="AssertionError: expected 200, got 500",
        work_num_turns=8,
        work_cost_usd=0.12,
    )
    action = step(s, opts, counters=counters)
    assert action.kind == RUN
    assert action.command == ("retry", "w1")
    assert counters.work_retries == 1
    assert counters.work_machine_fault_retries == 0


def test_a_completed_work_leg_clears_a_stale_fault_streak():
    machine_fault.record_fault("precision", "machine fault (auth): dead OAuth")
    assert machine_fault.consecutive_faults("precision") == 1

    s = state(
        work_aid="w1", work_status="done", work_branch="issue-1392-x",
        work_machine="precision",
    )
    step(s, DriveOptions(machine="precision"))
    assert machine_fault.consecutive_faults("precision") == 0
