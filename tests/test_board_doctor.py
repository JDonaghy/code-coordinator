"""Black-box coverage for the board-doctor sweep harness (#3758).

Drives :func:`coord.board_doctor.sweep` the same way the CLI's production
entry point (:func:`coord.board_doctor.run_sweep`) would — real
:mod:`coord.state` persistence, fake dispatcher callables standing in for
C3/C4 (not yet built) — and asserts on its externally-observable behavior:
every row gets classified, the matching dispatcher is called exactly once,
every decision is logged with cause + provenance, a row stuck on the same
cause past the convergence limit escalates exactly once and stops being
re-acted on, and ``dry_run=True`` mutates nothing.
"""

from __future__ import annotations

from coord import state
from coord.board_doctor import (
    TIER_ALERT,
    TIER_AUTO,
    StuckRow,
    sweep,
)


def _row(key: str, cause: str, queue: str = "drive-queue") -> StuckRow:
    return StuckRow(
        queue=queue,
        key=key,
        state="stuck",
        cause=cause,
        confidence="high",
        evidence=(f"cause={cause}",),
    )


class _Recorder:
    """Fake dispatcher that counts calls and records what it saw."""

    def __init__(self) -> None:
        self.calls: list[StuckRow] = []

    def __call__(self, row: StuckRow) -> None:
        self.calls.append(row)

    @property
    def count(self) -> int:
        return len(self.calls)


def _always(tier: str):
    return lambda cause: tier  # noqa: ARG005


class TestSweepClassifiesAndDispatches:
    def test_two_rows_different_causes_each_dispatched_exactly_once(self) -> None:
        auto_row = _row("repo#1", "no_pr")
        alert_row = _row("repo#2", "merge_conflict")

        auto_dispatch = _Recorder()
        alert_dispatch = _Recorder()

        def tier_of(cause: str) -> str:
            return TIER_AUTO if cause == "no_pr" else TIER_ALERT

        report = sweep(
            [auto_row, alert_row],
            tier_of=tier_of,
            auto_dispatchers={"no_pr": auto_dispatch},
            alert_dispatcher=alert_dispatch,
            sweep_id="sweep-test-1",
        )

        assert len(report.decisions) == 2
        assert auto_dispatch.count == 1
        assert auto_dispatch.calls[0].key == "repo#1"
        assert alert_dispatch.count == 1
        assert alert_dispatch.calls[0].key == "repo#2"

        decisions_by_key = {d.key: d for d in report.decisions}
        assert decisions_by_key["repo#1"].action == "auto-dispatch"
        assert decisions_by_key["repo#1"].cause == "no_pr"
        assert decisions_by_key["repo#2"].action == "alert"
        assert decisions_by_key["repo#2"].cause == "merge_conflict"

        logged = state.board_doctor_decisions()
        assert len(logged) == 2
        logged_by_key = {entry["key"]: entry for entry in logged}
        assert logged_by_key["repo#1"]["cause"] == "no_pr"
        assert logged_by_key["repo#1"]["sweep_id"] == "sweep-test-1"
        assert logged_by_key["repo#1"]["evidence"] == ["cause=no_pr"]
        assert logged_by_key["repo#2"]["cause"] == "merge_conflict"
        assert logged_by_key["repo#2"]["action"] == "alert"


class TestConvergence:
    def test_same_cause_past_limit_escalates_exactly_once_and_stops_re_acting(
        self,
    ) -> None:
        dispatch = _Recorder()
        alert = _Recorder()
        key = "repo#9"
        cause = "ci_red"

        for _ in range(5):
            sweep(
                [_row(key, cause)],
                tier_of=_always(TIER_AUTO),
                auto_dispatchers={cause: dispatch},
                alert_dispatcher=alert,
                convergence_limit=3,
            )

        # Sweeps 1-2 auto-dispatch (count < 3); sweep 3 is the first AT the
        # limit so it falls back to alert once; sweeps 4-5 must neither
        # re-dispatch the auto action nor re-alert.
        assert dispatch.count == 2
        assert alert.count == 1

        row_state = state.board_doctor_row_state("drive-queue", key)
        assert row_state["escalated"] is True
        assert row_state["cause"] == cause
        assert row_state["count"] == 5

    def test_cause_change_resets_convergence_counter(self) -> None:
        dispatch = _Recorder()
        alert = _Recorder()
        key = "repo#10"

        for _ in range(3):
            sweep(
                [_row(key, "ci_red")],
                tier_of=_always(TIER_AUTO),
                auto_dispatchers={"ci_red": dispatch},
                alert_dispatcher=alert,
                convergence_limit=3,
            )
        # A new cause on this same row is a new problem, not a continuation.
        sweep(
            [_row(key, "no_pr")],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={"no_pr": dispatch},
            alert_dispatcher=alert,
            convergence_limit=3,
        )

        row_state = state.board_doctor_row_state("drive-queue", key)
        assert row_state["cause"] == "no_pr"
        assert row_state["count"] == 1
        assert row_state["escalated"] is False


class TestDryRun:
    def test_dry_run_dispatches_nothing_and_persists_no_counter(self) -> None:
        dispatch = _Recorder()
        alert = _Recorder()
        key = "repo#42"

        before = state.board_doctor_row_state("drive-queue", key)
        assert before == {}

        report = sweep(
            [_row(key, "no_pr")],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={"no_pr": dispatch},
            alert_dispatcher=alert,
            dry_run=True,
        )

        assert dispatch.count == 0
        assert alert.count == 0
        assert report.dry_run is True
        assert report.decisions[0].dry_run is True
        # classified and logged in the report, but nothing persisted
        assert report.decisions[0].cause == "no_pr"

        after = state.board_doctor_row_state("drive-queue", key)
        assert after == {}
        assert state.board_doctor_decisions() == []

    def test_dry_run_after_real_sweeps_does_not_advance_the_counter(self) -> None:
        dispatch = _Recorder()
        alert = _Recorder()
        key = "repo#43"

        sweep(
            [_row(key, "no_pr")],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={"no_pr": dispatch},
            alert_dispatcher=alert,
        )
        before = state.board_doctor_row_state("drive-queue", key)
        assert before["count"] == 1

        sweep(
            [_row(key, "no_pr")],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={"no_pr": dispatch},
            alert_dispatcher=alert,
            dry_run=True,
        )

        after = state.board_doctor_row_state("drive-queue", key)
        assert after == before
        assert dispatch.count == 1  # only the real sweep dispatched
