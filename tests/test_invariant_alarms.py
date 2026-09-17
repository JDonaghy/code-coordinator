"""#3376 deliverable B: unit tests for the five invariant alarms.

Each alarm gets both a FIRING case and a matching non-firing control — a
check that can only ever pass is not a check (#2096: "a gate must be able
to fail").
"""

from __future__ import annotations

from coord.invariant_alarms import (
    ALARM_GATE_DONE_NO_VERDICT,
    ALARM_HOST_STAGED_STALE,
    ALARM_QUEUE_EMPTY_MACHINES_BUSY,
    ALARM_QUEUE_STALLED,
    ALARM_ZERO_COST_TERMINAL,
    check_gate_done_without_verdict,
    check_host_staged_stale,
    check_machines_busy_while_queue_empty,
    check_queue_stalled,
    check_zero_turn_zero_cost_terminal,
)


class TestMachinesBusyWhileQueueEmpty:
    def test_fires_when_queue_empty_and_machines_busy(self) -> None:
        alarm = check_machines_busy_while_queue_empty(
            busy_machines=["laptop", "server"], queue_row_count=0,
        )
        assert alarm is not None
        assert alarm.key == ALARM_QUEUE_EMPTY_MACHINES_BUSY
        assert alarm.severity == "warning"
        assert "laptop" in alarm.summary and "server" in alarm.summary

    def test_silent_when_queue_has_rows(self) -> None:
        assert check_machines_busy_while_queue_empty(
            busy_machines=["laptop"], queue_row_count=3,
        ) is None

    def test_silent_when_no_machines_busy(self) -> None:
        assert check_machines_busy_while_queue_empty(
            busy_machines=[], queue_row_count=0,
        ) is None


class TestQueueStalled:
    def test_fires_on_blocked_idle_no_change(self) -> None:
        alarm = check_queue_stalled(
            blocked_rows=7, running_rows=0, ticks_without_change=5,
        )
        assert alarm is not None
        assert alarm.key == ALARM_QUEUE_STALLED
        assert alarm.severity == "critical"

    def test_silent_when_something_is_running(self) -> None:
        assert check_queue_stalled(
            blocked_rows=7, running_rows=1, ticks_without_change=10,
        ) is None

    def test_silent_when_nothing_blocked(self) -> None:
        assert check_queue_stalled(
            blocked_rows=0, running_rows=0, ticks_without_change=10,
        ) is None

    def test_silent_under_the_tick_threshold(self) -> None:
        assert check_queue_stalled(
            blocked_rows=7, running_rows=0, ticks_without_change=1,
        ) is None

    def test_custom_threshold_is_honoured(self) -> None:
        assert check_queue_stalled(
            blocked_rows=7, running_rows=0, ticks_without_change=2,
            stall_tick_threshold=10,
        ) is None
        alarm = check_queue_stalled(
            blocked_rows=7, running_rows=0, ticks_without_change=10,
            stall_tick_threshold=10,
        )
        assert alarm is not None


class TestGateDoneWithoutVerdict:
    def test_fires_when_done_with_no_verdict(self) -> None:
        alarm = check_gate_done_without_verdict(
            stage="smoke", status="done", has_required_verdict=False,
        )
        assert alarm is not None
        assert alarm.key == ALARM_GATE_DONE_NO_VERDICT
        assert alarm.severity == "critical"
        assert "smoke" in alarm.summary

    def test_silent_when_verdict_recorded(self) -> None:
        assert check_gate_done_without_verdict(
            stage="smoke", status="done", has_required_verdict=True,
        ) is None

    def test_silent_when_not_yet_terminal(self) -> None:
        assert check_gate_done_without_verdict(
            stage="smoke", status="running", has_required_verdict=False,
        ) is None

    def test_fires_on_merged_too(self) -> None:
        alarm = check_gate_done_without_verdict(
            stage="review", status="merged", has_required_verdict=False,
        )
        assert alarm is not None


class TestHostStagedStale:
    def test_fires_past_threshold(self) -> None:
        alarm = check_host_staged_stale(host="dellserver", staged_propagate_runs=2)
        assert alarm is not None
        assert alarm.key == ALARM_HOST_STAGED_STALE
        assert "dellserver" in alarm.summary

    def test_silent_at_or_under_threshold(self) -> None:
        assert check_host_staged_stale(host="dellserver", staged_propagate_runs=1) is None
        assert check_host_staged_stale(host="dellserver", staged_propagate_runs=0) is None

    def test_custom_threshold_is_honoured(self) -> None:
        assert check_host_staged_stale(
            host="h", staged_propagate_runs=3, stale_propagate_run_threshold=5,
        ) is None
        alarm = check_host_staged_stale(
            host="h", staged_propagate_runs=6, stale_propagate_run_threshold=5,
        )
        assert alarm is not None


class TestZeroTurnZeroCostTerminal:
    def test_fires_on_zero_turn_zero_cost_failed(self) -> None:
        alarm = check_zero_turn_zero_cost_terminal(
            assignment_id="a1", status="failed", num_turns=0, cost_usd=0.0,
        )
        assert alarm is not None
        assert alarm.key == ALARM_ZERO_COST_TERMINAL

    def test_fires_on_zero_turn_zero_cost_done(self) -> None:
        """Deliberately not restricted to 'failed' — a 'done' row with the
        same shape is just as invisible to spend accounting."""
        alarm = check_zero_turn_zero_cost_terminal(
            assignment_id="a2", status="done", num_turns=1, cost_usd=None,
        )
        assert alarm is not None

    def test_silent_on_real_work_product(self) -> None:
        assert check_zero_turn_zero_cost_terminal(
            assignment_id="a3", status="failed", num_turns=12, cost_usd=1.50,
        ) is None

    def test_silent_when_not_terminal(self) -> None:
        assert check_zero_turn_zero_cost_terminal(
            assignment_id="a4", status="running", num_turns=0, cost_usd=0.0,
        ) is None

    def test_silent_when_turns_never_measured(self) -> None:
        """`num_turns=None` is 'never measured', not 'zero' — mirrors
        `coord.machine_fault.is_instant_zero_cost_failure`'s own contract
        exactly (#2096: one question, one answer, reused here rather than
        re-implemented)."""
        assert check_zero_turn_zero_cost_terminal(
            assignment_id="a5", status="failed", num_turns=None, cost_usd=0.0,
        ) is None
