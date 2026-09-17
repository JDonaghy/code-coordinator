"""#3376 deliverable B: five invariant alarms.

Three of the four #3376 source bugs (#3367, #3368, #3375) were found
because a human happened to notice something looked odd; in every case
`coord status` / `coord drive-queue status` said ``alert: (none)`` the
whole time. #3375 is the sharpest example: five agents ran a smoke stage
on a branch that had already merged to main, and because the drive queue
itself was legitimately drained, every existing status surface was
correctly green — the waste was invisible BY CONSTRUCTION, not by an
oversight in an existing check.

This module is the five checks that would have caught each incident,
written as pure, independently-testable functions — the SAME "one
function per question" shape `coord.dispatch_liveness` uses for deliverable
A, not one big fleet-wide scanner nobody can unit test in isolation. Each
takes exactly the facts it needs (already fetched by the caller — no `gh`
call, no DB access, no ambient clock) and returns an :class:`InvariantAlarm`
or `None`.  Wiring these into `coord status` / `coord drive-queue status` /
`coord notify` is the caller's job; this module only answers "does the
invariant hold right now", so a future 6th alarm has exactly one obvious
place to be added.

1. :func:`check_machines_busy_while_queue_empty` — #3375's own shape:
   machines showing live work while the drive queue itself has zero rows.
2. :func:`check_queue_stalled` — #3368: rows blocked, nothing running, no
   state change in N ticks in a row.
3. :func:`check_gate_done_without_verdict` — #3375's own loop condition: a
   stage reached a terminal-success status without ever recording the
   verdict its own gate requires. A HARD invariant violation, not a
   heuristic — always ``"critical"``.
4. :func:`check_host_staged_stale` — #3363: a host left `staged` (a binary
   swap performed but not yet restarted into) across more than one
   propagate run, six days silent.
5. :func:`check_zero_turn_zero_cost_terminal` — the accounting blind spot:
   an assignment recorded done/failed with 0 turns and $0.00 cost is
   invisible to `coord usage` by construction (turns=0 duration=? cost=$0
   contributes nothing to any total), so either it is real waste or the
   accounting itself is broken — both warrant surfacing. Reuses
   `coord.machine_fault.is_instant_zero_cost_failure` for the shape test
   (#2096: one question, one answer — this is the SAME "no measurable
   work product at all" reading #3367's module already made, just no
   longer confined to a `status == "failed"` row).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from coord.machine_fault import is_instant_zero_cost_failure

__all__ = [
    "InvariantAlarm",
    "ALARM_QUEUE_EMPTY_MACHINES_BUSY",
    "ALARM_QUEUE_STALLED",
    "ALARM_GATE_DONE_NO_VERDICT",
    "ALARM_HOST_STAGED_STALE",
    "ALARM_ZERO_COST_TERMINAL",
    "check_machines_busy_while_queue_empty",
    "check_queue_stalled",
    "check_gate_done_without_verdict",
    "check_host_staged_stale",
    "check_zero_turn_zero_cost_terminal",
]

ALARM_QUEUE_EMPTY_MACHINES_BUSY = "queue_empty_machines_busy"
ALARM_QUEUE_STALLED = "queue_stalled_blocked"
ALARM_GATE_DONE_NO_VERDICT = "gate_done_without_verdict"
ALARM_HOST_STAGED_STALE = "host_staged_stale"
ALARM_ZERO_COST_TERMINAL = "zero_turn_zero_cost_terminal"

# #3368's own evidence: a stall this module's caller reports on every tick
# would be noise for the ordinary first tick or two of a legitimate wait
# (a slow CI run, a human still reviewing). Three consecutive ticks with
# zero running and zero state change is the threshold #3368's own queue
# used once diagnosed — a queue that's been sitting on the same shape for
# three whole polls has very likely already stopped waiting on anything
# that's still moving.
DEFAULT_STALL_TICK_THRESHOLD = 3

# #3363: a host is expected to restart into a `staged` swap on the very
# next propagate run. Surviving past the run immediately after the one
# that staged it is already an anomaly worth a look.
DEFAULT_STAGED_RUN_THRESHOLD = 1

_DEFAULT_TERMINAL_SUCCESS_STATUSES = frozenset({"done", "merged"})
_DEFAULT_TERMINAL_STATUSES = frozenset({"done", "failed"})


@dataclass(frozen=True)
class InvariantAlarm:
    """One fired invariant — a hard fact about the board/queue/fleet that
    should never be silently true, surfaced with enough detail that a
    human (or `coord notify`) doesn't have to re-derive the "why"."""

    key: str
    severity: str  # "warning" | "critical"
    summary: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "severity": self.severity,
            "summary": self.summary,
            "details": self.details,
        }


def check_machines_busy_while_queue_empty(
    *, busy_machines: list[str], queue_row_count: int
) -> InvariantAlarm | None:
    """#3375: N machines running work while the drive queue itself has
    ZERO rows is exactly the shape that let five smoke agents run against
    a branch that had already merged — the queue was correctly empty
    (nothing left to do) and the busy machines were doing something the
    queue no longer had any row for at all. A legitimately drained queue
    with genuinely idle machines is fine; a drained queue with BUSY
    machines means something is running that no queue row accounts for,
    which is worth a look every time, not just when it happens to be
    noticed by eye.
    """
    if queue_row_count == 0 and busy_machines:
        names = ", ".join(sorted(busy_machines))
        return InvariantAlarm(
            key=ALARM_QUEUE_EMPTY_MACHINES_BUSY,
            severity="warning",
            summary=(
                f"{len(busy_machines)} machine(s) busy ({names}) while the "
                "drive queue is empty — verify each busy assignment still "
                "names a live issue/branch (#3376, caught #3375 by eye)"
            ),
            details={"busy_machines": sorted(busy_machines), "queue_row_count": queue_row_count},
        )
    return None


def check_queue_stalled(
    *,
    blocked_rows: int,
    running_rows: int,
    ticks_without_change: int,
    stall_tick_threshold: int = DEFAULT_STALL_TICK_THRESHOLD,
) -> InvariantAlarm | None:
    """#3368: a queue with blocked rows, nothing running, and no state
    change for *stall_tick_threshold* consecutive ticks outlived its own
    cause — the pre-req issue had already merged and closed, and nothing
    re-checked. A queue that is blocked but actively churning (running_rows
    > 0, or its shape keeps changing tick to tick) is working as intended;
    this alarm is specifically the "nothing is happening AND nothing has
    changed in a while" combination.
    """
    if (
        blocked_rows > 0
        and running_rows == 0
        and ticks_without_change >= stall_tick_threshold
    ):
        return InvariantAlarm(
            key=ALARM_QUEUE_STALLED,
            severity="critical",
            summary=(
                f"{blocked_rows} row(s) blocked, 0 running, no state change "
                f"in {ticks_without_change} ticks — the block's own stated "
                "cause may already be resolved (#3376, caught #3368)"
            ),
            details={
                "blocked_rows": blocked_rows,
                "running_rows": running_rows,
                "ticks_without_change": ticks_without_change,
            },
        )
    return None


def check_gate_done_without_verdict(
    *,
    stage: str,
    status: str,
    has_required_verdict: bool,
    terminal_success_statuses: frozenset[str] = _DEFAULT_TERMINAL_SUCCESS_STATUSES,
) -> InvariantAlarm | None:
    """#3375's own loop condition: a stage reached a terminal-success
    status (``done``/``merged``) WITHOUT ever recording the verdict its
    own gate requires (a Test/Review/UAT/Smoke verdict field the merge
    gate reads). This is a HARD invariant violation, not a heuristic
    "looks suspicious" — a gate that can be satisfied by "nothing ever
    said no" instead of an actual recorded verdict is unreachable-failure
    by construction (#2096's "a gate must be able to fail"), which is
    exactly what let #3375's loop keep dispatching. Always ``"critical"``.
    """
    if status in terminal_success_statuses and not has_required_verdict:
        return InvariantAlarm(
            key=ALARM_GATE_DONE_NO_VERDICT,
            severity="critical",
            summary=(
                f"{stage!r} reached {status!r} without ever recording the "
                "verdict its own gate requires — hard invariant violation "
                "(#3376, the #3375 loop condition)"
            ),
            details={"stage": stage, "status": status},
        )
    return None


def check_host_staged_stale(
    *,
    host: str,
    staged_propagate_runs: int,
    stale_propagate_run_threshold: int = DEFAULT_STAGED_RUN_THRESHOLD,
) -> InvariantAlarm | None:
    """#3363: a host left `staged` (a new build swapped into place but the
    service not yet restarted onto it) across more than
    *stale_propagate_run_threshold* propagate runs went six days silent —
    every later run's own "already staged, nothing to do" reasoning read
    as success, because from the propagate run's own point of view nothing
    was wrong.
    """
    if staged_propagate_runs > stale_propagate_run_threshold:
        return InvariantAlarm(
            key=ALARM_HOST_STAGED_STALE,
            severity="warning",
            summary=(
                f"host {host!r} has been `staged` (swapped, not restarted) "
                f"across {staged_propagate_runs} propagate runs — "
                "(#3376, caught #3363 six days silent)"
            ),
            details={"host": host, "staged_propagate_runs": staged_propagate_runs},
        )
    return None


def check_zero_turn_zero_cost_terminal(
    *,
    assignment_id: str,
    status: str,
    num_turns: int | None,
    cost_usd: float | None,
    terminal_statuses: frozenset[str] = _DEFAULT_TERMINAL_STATUSES,
) -> InvariantAlarm | None:
    """The accounting blind spot: an assignment recorded ``done``/
    ``failed`` with 0 (or unmeasured) turns and $0.00 (or unmeasured) cost
    never reaches `coord usage` totals — a 0-duration, $0 row contributes
    nothing to any spend rollup, so this waste (or, alternatively, a
    broken measurement — either is worth surfacing) is invisible to cost
    accounting the same way it was invisible to the queue and to alerts.
    Deliberately not restricted to ``status == "failed"`` (unlike
    `coord.machine_fault`'s use of the same shape test): a `"done"` row
    with 0 turns/$0 is just as clearly invisible to spend.
    """
    if status in terminal_statuses and is_instant_zero_cost_failure(
        num_turns=num_turns, cost_usd=cost_usd,
    ):
        turns_str = "?" if num_turns is None else str(num_turns)
        cost_str = f"{cost_usd:.2f}" if cost_usd else "0.00"
        return InvariantAlarm(
            key=ALARM_ZERO_COST_TERMINAL,
            severity="warning",
            summary=(
                f"assignment {assignment_id} recorded {status!r} with "
                f"{turns_str} turn(s) and ${cost_str} — invisible to spend "
                "accounting (#3376)"
            ),
            details={
                "assignment_id": assignment_id,
                "status": status,
                "num_turns": num_turns,
                "cost_usd": cost_usd,
            },
        )
    return None
