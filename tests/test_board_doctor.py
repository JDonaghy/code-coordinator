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
    ALERT_SKIPPED,
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
    """Fake dispatcher that counts calls and records what it saw.

    Returns ``True`` so it also works as an *alert_dispatcher* under
    `sweep`'s confirmed-delivery contract — a fake standing in for a real
    dispatcher must report success the same way a real one would.
    """

    def __init__(self) -> None:
        self.calls: list[StuckRow] = []

    def __call__(self, row: StuckRow) -> bool:
        self.calls.append(row)
        return True

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


class TestAlertDeliveryIsConfirmed:
    """The failure half of the confirmed-delivery contract.

    `sweep` may only mark a row escalated — which permanently suppresses
    further alerts for that cause — when the dispatcher CONFIRMS delivery.
    These drive dispatchers that fail (falsy return), blow up (raise) and
    deliberately decline (`ALERT_SKIPPED`), and assert the sweep keeps
    retrying instead of recording a human as notified.
    """

    def test_falsy_return_reports_alert_failed_and_leaves_the_row_unescalated(
        self,
    ) -> None:
        key = "repo#201"

        report = sweep(
            [_row(key, "ci_red")],
            tier_of=_always(TIER_ALERT),
            alert_dispatcher=lambda row: False,  # noqa: ARG005
            sweep_id="sweep-failed-1",
        )

        decision = report.decisions[0]
        assert decision.action == "alert-failed"
        assert decision.escalated is False
        assert state.board_doctor_row_state("drive-queue", key)["escalated"] is False

        logged = state.board_doctor_decisions()
        assert [entry["action"] for entry in logged] == ["alert-failed"]

    def test_raising_dispatcher_reports_alert_failed_rather_than_crashing(self) -> None:
        key = "repo#202"

        def boom(row: StuckRow) -> bool:
            raise RuntimeError("database is locked")

        report = sweep(
            [_row(key, "ci_red")],
            tier_of=_always(TIER_ALERT),
            alert_dispatcher=boom,
        )

        decision = report.decisions[0]
        assert decision.action == "alert-failed"
        assert decision.escalated is False
        assert state.board_doctor_row_state("drive-queue", key)["escalated"] is False

    def test_a_failed_alert_is_re_attempted_by_the_next_sweep(self) -> None:
        """The whole point of not marking a failed alert escalated."""
        key = "repo#203"
        cause = "ci_red"
        attempts: list[StuckRow] = []
        delivers = False

        def flaky(row: StuckRow) -> bool:
            attempts.append(row)
            return delivers

        first = sweep(
            [_row(key, cause)],
            tier_of=_always(TIER_ALERT),
            alert_dispatcher=flaky,
        )
        assert first.decisions[0].action == "alert-failed"
        assert len(attempts) == 1

        delivers = True
        second = sweep(
            [_row(key, cause)],
            tier_of=_always(TIER_ALERT),
            alert_dispatcher=flaky,
        )
        assert len(attempts) == 2  # re-invoked, not suppressed
        assert second.decisions[0].action == "alert"
        assert second.decisions[0].escalated is True
        assert state.board_doctor_row_state("drive-queue", key)["escalated"] is True

        # Now that delivery IS confirmed, the "escalate exactly once"
        # contract takes over again.
        sweep(
            [_row(key, cause)],
            tier_of=_always(TIER_ALERT),
            alert_dispatcher=flaky,
        )
        assert len(attempts) == 2

    def test_a_failed_alert_on_the_convergence_fallback_also_retries(self) -> None:
        """The auto-tier path at the convergence limit uses the same seam."""
        key = "repo#204"
        cause = "ci_red"
        dispatch = _Recorder()
        attempts: list[StuckRow] = []

        def failing(row: StuckRow) -> bool:
            attempts.append(row)
            return False

        for _ in range(5):
            sweep(
                [_row(key, cause)],
                tier_of=_always(TIER_AUTO),
                auto_dispatchers={cause: dispatch},
                alert_dispatcher=failing,
                convergence_limit=3,
            )

        # Sweeps 1-2 auto-dispatch; sweeps 3-5 are at/past the limit and
        # each re-attempt the alert, because none of them was confirmed.
        assert dispatch.count == 2
        assert len(attempts) == 3
        assert state.board_doctor_row_state("drive-queue", key)["escalated"] is False

    def test_a_skipped_alert_is_labelled_distinctly_and_still_retried(self) -> None:
        """A dispatcher declining to file is not a delivered alert.

        `coord.board_doctor_policy.escalation_alert_dispatcher` returns
        this when a more specific escalation already covers the issue —
        nothing board-doctor filed, so nothing may be suppressed.
        """
        key = "repo#205"
        cause = "ci_red"
        attempts: list[StuckRow] = []

        def declining(row: StuckRow) -> str:
            attempts.append(row)
            return ALERT_SKIPPED

        first = sweep(
            [_row(key, cause)],
            tier_of=_always(TIER_ALERT),
            alert_dispatcher=declining,
        )
        assert first.decisions[0].action == "alert-skipped"
        assert first.decisions[0].escalated is False
        assert state.board_doctor_row_state("drive-queue", key)["escalated"] is False

        sweep(
            [_row(key, cause)],
            tier_of=_always(TIER_ALERT),
            alert_dispatcher=declining,
        )
        assert len(attempts) == 2


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

    def test_row_absent_from_a_later_sweep_clears_its_counter(self) -> None:
        """A row that drops out of the walk (resolved) is no longer tracked.

        Without clearing, a later recurrence on the SAME cause would silently
        resume the stale count/escalation instead of starting over.
        """
        dispatch = _Recorder()
        alert = _Recorder()
        key = "repo#11"
        cause = "ci_red"

        for _ in range(3):
            sweep(
                [_row(key, cause)],
                tier_of=_always(TIER_AUTO),
                auto_dispatchers={cause: dispatch},
                alert_dispatcher=alert,
                convergence_limit=3,
            )
        assert state.board_doctor_row_state("drive-queue", key)["count"] == 3

        # The row resolves — it no longer appears in the walk at all.
        sweep(
            [],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=alert,
            convergence_limit=3,
        )
        assert state.board_doctor_row_state("drive-queue", key) == {}

    def test_resolved_then_recurring_row_restarts_convergence_from_scratch(
        self,
    ) -> None:
        dispatch = _Recorder()
        alert = _Recorder()
        key = "repo#12"
        cause = "ci_red"

        # Escalate past the limit.
        for _ in range(4):
            sweep(
                [_row(key, cause)],
                tier_of=_always(TIER_AUTO),
                auto_dispatchers={cause: dispatch},
                alert_dispatcher=alert,
                convergence_limit=3,
            )
        assert state.board_doctor_row_state("drive-queue", key)["escalated"] is True

        # Resolves for one sweep (absent from the walk) ...
        sweep(
            [],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=alert,
            convergence_limit=3,
        )

        # ... then recurs on the SAME cause. Without the clear-on-absence
        # mechanism this would resume at count=5/escalated=True and never
        # alert again; it must instead look exactly like a fresh row.
        alert.calls.clear()
        sweep(
            [_row(key, cause)],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=alert,
            convergence_limit=3,
        )

        row_state = state.board_doctor_row_state("drive-queue", key)
        assert row_state["count"] == 1
        assert row_state["escalated"] is False

    def test_row_absent_from_a_later_sweep_notifies_the_escalation_resolver(
        self,
    ) -> None:
        """A row dropping out of the walk gets its escalation dismissed.

        Mirrors `test_row_absent_from_a_later_sweep_clears_its_counter` but
        asserts on the ``escalation_resolver`` callback itself, since that
        is the hook `coord.board_doctor_policy.dismiss_resolved_escalation`
        plugs into.
        """
        dispatch = _Recorder()
        alert = _Recorder()
        resolved: list[tuple[str, str]] = []
        key = "repo#14"
        cause = "ci_red"

        sweep(
            [_row(key, cause)],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=alert,
            escalation_resolver=lambda queue, k: resolved.append((queue, k)),
        )
        assert resolved == []

        sweep(
            [],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=alert,
            escalation_resolver=lambda queue, k: resolved.append((queue, k)),
        )
        assert resolved == [("drive-queue", key)]

    def test_a_raising_escalation_resolver_does_not_abort_the_clear_walk(self) -> None:
        """One row's cleanup blowing up must not strand the others."""
        dispatch = _Recorder()
        alert = _Recorder()
        keys = ("repo#15", "repo#16")
        cause = "ci_red"
        seen: list[tuple[str, str]] = []

        def exploding(queue: str, key: str) -> None:
            seen.append((queue, key))
            raise RuntimeError("escalation store unreachable")

        sweep(
            [_row(key, cause) for key in keys],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=alert,
        )

        sweep(
            [],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=alert,
            escalation_resolver=exploding,
        )

        assert sorted(seen) == [("drive-queue", k) for k in keys]
        for key in keys:
            assert state.board_doctor_row_state("drive-queue", key) == {}

    def test_clear_absent_rows_false_leaves_a_partial_walks_counters_alone(
        self,
    ) -> None:
        """A caller intentionally driving a partial row subset opts out."""
        dispatch = _Recorder()
        alert = _Recorder()
        key = "repo#13"
        cause = "ci_red"

        sweep(
            [_row(key, cause)],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=alert,
        )
        assert state.board_doctor_row_state("drive-queue", key)["count"] == 1

        # A deliberately empty/partial walk with clear_absent_rows=False must
        # not treat this row as resolved.
        sweep(
            [],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=alert,
            clear_absent_rows=False,
        )
        assert state.board_doctor_row_state("drive-queue", key)["count"] == 1


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
