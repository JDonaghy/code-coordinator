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

from dataclasses import replace

from coord import queue_diagnose, state
from coord.board_doctor import (
    ALERT_SKIPPED,
    CAUSE_BASELINE_RED,
    CAUSE_CEILING_BLOCKED_MERGEABLE,
    CAUSE_SMOKE_NO_VERDICT,
    TIER_ALERT,
    TIER_AUTO,
    StuckRow,
    _reclassify_ceiling_blocked,
    _refresh_ceiling_blocked_settled_rows,
    baseline_red_rows,
    smoke_no_verdict_rows,
    sweep,
)
from coord.drive_queue import QueueEntry
from coord.models import Assignment, Board
from coord.queue_diagnose import LiveState
from coord.smoke import (
    ABANDONED_BACKGROUND_SMOKE_MARKER,
    ENVIRONMENTAL_SMOKE_MARKER,
    MUTE_SMOKE_LEG_BUDGET,
    TEST_STATE_BLOCKED,
    mute_smoke_tally,
)


def _row(key: str, cause: str, queue: str = "drive-queue", *, fresh: bool = False) -> StuckRow:
    return StuckRow(
        queue=queue,
        key=key,
        state="stuck",
        cause=cause,
        confidence="high",
        evidence=(f"cause={cause}",),
        fresh=fresh,
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


def _seed_ceiling_blocked_entry(repo: str, issue: int) -> None:
    """A drive-queue row that already gave up on #2972's fix-round ceiling."""
    state.enqueue_drive_queue(repo, issue, after=[])
    state.update_drive_queue_entry(
        repo,
        issue,
        state="blocked",
        last_reason="giving up after 2 fix rounds (#2972)",
    )


class _FakeProbe:
    """A :class:`coord.queue_diagnose.LiveProbe` double keyed by ``repo#issue``."""

    def __init__(self, live_by_key: dict[str, LiveState | Exception]) -> None:
        self._live_by_key = live_by_key
        #: every key this probe was actually called with, in order — lets a
        #: test assert the probe was never reached (``calls == []``) rather
        #: than relying on an unconfigured key's ``AssertionError`` as a
        #: proxy, since production's ``except Exception`` would also catch
        #: that error and mask the short-circuit silently passing.
        self.calls: list[str] = []

    def probe(self, entry: QueueEntry) -> LiveState:
        key = f"{entry.repo}#{entry.issue}"
        self.calls.append(key)
        result = self._live_by_key.get(key)
        if isinstance(result, Exception):
            raise result
        if result is None:
            raise AssertionError(f"no fake live state configured for {key}")
        return result


class TestRefreshCeilingBlockedSettledRows:
    """#3777: one more live probe for a settled, ceiling-blocked episode."""

    def test_ceiling_blocked_episode_is_refreshed_to_pr_merged(self) -> None:
        _seed_ceiling_blocked_entry("acme/widgets", 101)
        entry = QueueEntry(repo="acme/widgets", issue=101)
        episode = {
            "key": "acme/widgets#101",
            "state": "stuck",
            "diagnosed_cause": queue_diagnose.CAUSE_MERGE_CONFLICT,
            "diagnosis_confidence": queue_diagnose.CONFIDENCE_HIGH,
            "diagnosis_evidence": ("pr: mergeable=False",),
        }
        probe = _FakeProbe({"acme/widgets#101": LiveState(pr_state="MERGED")})

        refreshed, remaining = _refresh_ceiling_blocked_settled_rows(
            [episode],
            entries_by_key={entry.key: entry},
            probe=probe,
            trigger="board-doctor-sweep",
        )

        assert remaining == []
        [row] = refreshed
        assert row.key == "acme/widgets#101"
        assert row.cause == queue_diagnose.CAUSE_PR_MERGED
        assert row.confidence == queue_diagnose.CONFIDENCE_HIGH
        assert row.fresh is True

    def test_episode_not_ceiling_blocked_is_left_in_remaining(self) -> None:
        # A drive-queue row IS seeded here, but it's still "running" — not
        # ceiling-blocked — so the ceiling predicate reads False and this
        # refresh path must never even reach the probe for it. (A prior
        # version of this test seeded no row at all, which only proved the
        # "no matching entry" branch, already covered separately below.)
        state.enqueue_drive_queue("acme/widgets", 102, after=[])
        entry = QueueEntry(repo="acme/widgets", issue=102)
        episode = {
            "key": "acme/widgets#102",
            "state": "stuck",
            "diagnosed_cause": queue_diagnose.CAUSE_MERGE_CONFLICT,
            "diagnosis_confidence": queue_diagnose.CONFIDENCE_HIGH,
        }
        probe = _FakeProbe({})

        refreshed, remaining = _refresh_ceiling_blocked_settled_rows(
            [episode],
            entries_by_key={entry.key: entry},
            probe=probe,
            trigger="board-doctor-sweep",
        )

        assert refreshed == []
        assert remaining == [episode]
        assert probe.calls == []

    def test_episode_with_no_matching_drive_queue_entry_is_left_in_remaining(self) -> None:
        episode = {
            "key": "acme/widgets#103",
            "state": "stuck",
            "diagnosed_cause": queue_diagnose.CAUSE_MERGE_CONFLICT,
            "diagnosis_confidence": queue_diagnose.CONFIDENCE_HIGH,
        }
        probe = _FakeProbe({})

        refreshed, remaining = _refresh_ceiling_blocked_settled_rows(
            [episode],
            entries_by_key={},
            probe=probe,
            trigger="board-doctor-sweep",
        )

        assert refreshed == []
        assert remaining == [episode]
        assert probe.calls == []

    def test_a_probe_failure_falls_back_to_remaining_instead_of_crashing(self) -> None:
        _seed_ceiling_blocked_entry("acme/widgets", 104)
        entry = QueueEntry(repo="acme/widgets", issue=104)
        episode = {
            "key": "acme/widgets#104",
            "state": "stuck",
            "diagnosed_cause": queue_diagnose.CAUSE_MERGE_CONFLICT,
            "diagnosis_confidence": queue_diagnose.CONFIDENCE_HIGH,
        }
        probe = _FakeProbe({"acme/widgets#104": RuntimeError("gh timed out")})

        refreshed, remaining = _refresh_ceiling_blocked_settled_rows(
            [episode],
            entries_by_key={entry.key: entry},
            probe=probe,
            trigger="board-doctor-sweep",
        )

        assert refreshed == []
        assert remaining == [episode]

    def test_ceiling_blocked_episode_still_blocked_keeps_its_cause(self) -> None:
        # The live probe confirms the row is still mid-conflict — the
        # refreshed diagnosis just re-derives the same cause from fresh
        # evidence rather than leaving a stale answer in place.
        _seed_ceiling_blocked_entry("acme/widgets", 105)
        entry = QueueEntry(repo="acme/widgets", issue=105)
        episode = {
            "key": "acme/widgets#105",
            "state": "stuck",
            "diagnosed_cause": queue_diagnose.CAUSE_MERGE_CONFLICT,
            "diagnosis_confidence": queue_diagnose.CONFIDENCE_HIGH,
        }
        probe = _FakeProbe({"acme/widgets#105": LiveState(mergeable=False)})

        refreshed, remaining = _refresh_ceiling_blocked_settled_rows(
            [episode],
            entries_by_key={entry.key: entry},
            probe=probe,
            trigger="board-doctor-sweep",
        )

        assert remaining == []
        [row] = refreshed
        assert row.cause == queue_diagnose.CAUSE_MERGE_CONFLICT
        assert row.fresh is True

    def test_inconclusive_refresh_keeps_the_prior_stored_cause_in_remaining(self) -> None:
        # A refresh probe that comes back empty must not downgrade a
        # previously settled, actionable stored cause (CAUSE_PR_MERGED
        # here) to CAUSE_UNKNOWN — the episode stays in `remaining` so
        # `_episode_stuck_row` reconstructs it from `block_log` exactly as
        # it would have without this refresh ever running.
        _seed_ceiling_blocked_entry("acme/widgets", 106)
        entry = QueueEntry(repo="acme/widgets", issue=106)
        episode = {
            "key": "acme/widgets#106",
            "state": "stuck",
            "diagnosed_cause": queue_diagnose.CAUSE_PR_MERGED,
            "diagnosis_confidence": queue_diagnose.CONFIDENCE_HIGH,
        }
        probe = _FakeProbe({"acme/widgets#106": LiveState()})

        refreshed, remaining = _refresh_ceiling_blocked_settled_rows(
            [episode],
            entries_by_key={entry.key: entry},
            probe=probe,
            trigger="board-doctor-sweep",
        )

        assert refreshed == []
        assert remaining == [episode]


class TestReclassifyCeilingBlocked:
    """#3760 Action 1: relabel a ceiling-blocked-but-live-clear row."""

    def test_nothing_blocking_row_is_reclassified(self) -> None:
        _seed_ceiling_blocked_entry("acme/widgets", 101)
        row = _row("acme/widgets#101", queue_diagnose.CAUSE_NOTHING_BLOCKING, fresh=True)

        [out] = _reclassify_ceiling_blocked([row])

        assert out.cause == CAUSE_CEILING_BLOCKED_MERGEABLE
        assert out.evidence[:-1] == row.evidence
        assert "#2972" in out.evidence[-1]

    def test_stale_nothing_blocking_row_is_left_alone(self) -> None:
        # #3806: a row rebuilt from `block_log`'s *stored* diagnosis
        # (fresh=False, e.g. `_episode_stuck_row`'s reconstruction of a
        # settled episode) must never grant a fresh #2972 fix-round budget
        # — that stored "nothing blocking" verdict can be arbitrarily old,
        # taken before the PR went red or conflicted, and nothing
        # re-confirms it just because this sweep is replaying it.
        _seed_ceiling_blocked_entry("acme/widgets", 108)
        row = _row("acme/widgets#108", queue_diagnose.CAUSE_NOTHING_BLOCKING, fresh=False)

        [out] = _reclassify_ceiling_blocked([row])

        assert out == row

    def test_ci_green_row_is_left_alone(self) -> None:
        # queue_diagnose._rule_ci_green fires on green checks alone and is
        # the LAST rule in _RULES order — reached precisely when the
        # stronger _rule_nothing_blocking (gate_ready + all green +
        # mergeable) did NOT fire. It is not proof the row is actually
        # clear, so it must not be promoted to auto-tier here.
        _seed_ceiling_blocked_entry("acme/widgets", 102)
        row = _row("acme/widgets#102", queue_diagnose.CAUSE_CI_GREEN, fresh=True)

        [out] = _reclassify_ceiling_blocked([row])

        assert out == row

    def test_ceiling_blocked_row_with_low_confidence_is_left_alone(self) -> None:
        # A CONFIDENCE_LOW verdict must never be promoted to auto-tier,
        # even when the cause itself is in _LIVE_CLEAR_CAUSES.
        _seed_ceiling_blocked_entry("acme/widgets", 107)
        row = StuckRow(
            queue="drive-queue",
            key="acme/widgets#107",
            state="stuck",
            cause=queue_diagnose.CAUSE_NOTHING_BLOCKING,
            confidence=queue_diagnose.CONFIDENCE_LOW,
            evidence=(f"cause={queue_diagnose.CAUSE_NOTHING_BLOCKING}",),
            fresh=True,
        )

        [out] = _reclassify_ceiling_blocked([row])

        assert out == row

    def test_live_clear_row_that_is_not_ceiling_blocked_is_left_alone(self) -> None:
        # No drive-queue row at all for this issue — "nothing to block on".
        row = _row("acme/widgets#103", queue_diagnose.CAUSE_NOTHING_BLOCKING, fresh=True)

        [out] = _reclassify_ceiling_blocked([row])

        assert out == row

    def test_a_row_blocked_for_a_different_reason_is_left_alone(self) -> None:
        state.enqueue_drive_queue("acme/widgets", 104, after=[])
        state.update_drive_queue_entry(
            "acme/widgets",
            104,
            state="blocked",
            last_reason="merge conflict, no path forward",
        )
        row = _row("acme/widgets#104", queue_diagnose.CAUSE_NOTHING_BLOCKING, fresh=True)

        [out] = _reclassify_ceiling_blocked([row])

        assert out == row

    def test_an_unrelated_cause_on_a_ceiling_blocked_row_is_left_alone(self) -> None:
        # The row IS ceiling-blocked, but this row's cause is not one of
        # the two "live reads completely clear" causes — must not relabel
        # every cause a ceiling-blocked row happens to carry.
        _seed_ceiling_blocked_entry("acme/widgets", 105)
        row = _row("acme/widgets#105", "merge_conflict", fresh=True)

        [out] = _reclassify_ceiling_blocked([row])

        assert out == row

    def test_merge_queue_rows_are_never_reclassified(self) -> None:
        _seed_ceiling_blocked_entry("acme/widgets", 106)
        row = _row(
            "acme/widgets#106",
            queue_diagnose.CAUSE_NOTHING_BLOCKING,
            queue="merge-queue",
            fresh=True,
        )

        [out] = _reclassify_ceiling_blocked([row])

        assert out == row

    def test_unparseable_key_is_left_alone(self) -> None:
        row = _row("no-hash-separator", queue_diagnose.CAUSE_NOTHING_BLOCKING, fresh=True)

        [out] = _reclassify_ceiling_blocked([row])

        assert out == row

    def test_negative_issue_number_is_left_alone(self) -> None:
        # #3807: "repo#-5" must be rejected here exactly as it is by every
        # other row-key reader in this module and in board_doctor_policy —
        # all now share coord.drive_queue.parse_key, which requires a bare
        # digit tail and so never parses a leading "-".
        _seed_ceiling_blocked_entry("acme/widgets", 5)
        row = _row("acme/widgets#-5", queue_diagnose.CAUSE_NOTHING_BLOCKING, fresh=True)

        [out] = _reclassify_ceiling_blocked([row])

        assert out == row


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

    def test_a_falsy_dispatcher_return_is_logged_as_a_noop_not_a_success(self) -> None:
        # An auto-dispatcher reporting "nothing to do" (a falsy return, not
        # an exception) must not be recorded as a confirmed "auto-dispatch"
        # — that would falsely claim the action actually happened.
        noop_row = _row("repo#3", "no_pr")

        report = sweep(
            [noop_row],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={"no_pr": lambda row: False},
            alert_dispatcher=_Recorder(),
            sweep_id="sweep-test-noop",
        )

        [decision] = report.decisions
        assert decision.action == "auto-dispatch-noop"

        logged = state.board_doctor_decisions()
        [entry] = [e for e in logged if e["key"] == "repo#3"]
        assert entry["action"] == "auto-dispatch-noop"


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


class TestBaselineRedRows:
    """#3761: a red base poisons every row for the repo, collapsed to one."""

    def test_one_row_per_blocked_repo_regardless_of_streak_value(self) -> None:
        rows = baseline_red_rows({"acme/widgets": 3, "acme/gadgets": 9})

        assert {row.key for row in rows} == {"acme/widgets#0", "acme/gadgets#0"}
        for row in rows:
            assert row.queue == "baseline-red"
            assert row.cause == CAUSE_BASELINE_RED

    def test_evidence_carries_the_streak_count_not_the_repo_name(self) -> None:
        [row] = baseline_red_rows({"acme/widgets": 7})

        assert row.evidence == ("baseline_red_streak=7",)

    def test_empty_mapping_yields_no_rows(self) -> None:
        assert baseline_red_rows({}) == []

    def test_a_row_never_collides_with_a_real_per_issue_row(self) -> None:
        # Issue numbers on GitHub start at 1 — the synthetic row's issue
        # number is always 0, so no real per-issue row for the same repo
        # can ever share its key in escalation/convergence bookkeeping,
        # regardless of which issue number that real row happens to carry.
        [row] = baseline_red_rows({"acme/widgets": 3})

        repo_name, _, issue_text = row.key.rpartition("#")
        assert repo_name == "acme/widgets"
        assert int(issue_text) == 0


class TestBaselineRedConvergesLikeAnyOtherAlertRow:
    """#3761 acceptance (a)+(d): one alert, never a second for the same cause."""

    def test_baseline_red_row_escalates_once_and_a_resweep_stays_quiet(self) -> None:
        # #3761 review: `policy_tier_of` (the real C2 policy, not a stubbed
        # `_always(TIER_ALERT)`) is what actually decides this — it is not
        # registered on `ALLOWLIST`, so this is load-bearing evidence that
        # a red base has no safe auto-fix, rather than a tautology.
        from coord.board_doctor_policy import policy_tier_of

        alert = _Recorder()
        auto = _Recorder()
        row = baseline_red_rows({"acme/widgets": 3})[0]

        for _ in range(4):
            sweep(
                [row],
                tier_of=policy_tier_of,
                auto_dispatchers={CAUSE_BASELINE_RED: auto},
                alert_dispatcher=alert,
            )

        # Alert-tier: never routed to an auto-action, no matter how many
        # sweeps see it — a red base has no safe auto-fix.
        assert auto.count == 0
        assert alert.count == 1


class TestSuppressedRowsStayPresentForClearAbsentRows:
    """#3761 review (blocking finding #1).

    `collect_rows` folds a red-base repo's real rows into one collapsed
    baseline-red alert by marking each real row ``suppressed=True`` rather
    than dropping it from the row set. This class is the direct regression
    test for the bug that would reappear if a future change went back to
    dropping them: ``clear_absent_rows`` (default on) must not mistake a
    merely-*suppressed* row for a genuinely-absent one — it should neither
    clear its convergence counter nor notify ``escalation_resolver`` — while
    a row that is truly gone from the sweep must still get both.
    """

    def test_a_suppressed_row_keeps_its_counter_and_is_never_cleared(self) -> None:
        dispatch = _Recorder()
        alert = _Recorder()
        resolved: list[tuple[str, str]] = []
        key = "repo#21"
        cause = "ci_red"

        # Build up a counter as an ordinary auto-tier row first.
        sweep(
            [_row(key, cause)],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=alert,
        )
        assert dispatch.count == 1
        assert state.board_doctor_row_state("drive-queue", key)["count"] == 1

        # The repo's base goes red: the row is now folded into the
        # baseline-red alert and shows up here only as `suppressed=True`.
        suppressed_row = replace(_row(key, cause), suppressed=True)
        for _ in range(3):
            sweep(
                [suppressed_row],
                tier_of=_always(TIER_AUTO),
                auto_dispatchers={cause: dispatch},
                alert_dispatcher=alert,
                escalation_resolver=lambda queue, k: resolved.append((queue, k)),
            )

        # Neither dispatch path runs for a suppressed row, no matter how
        # many sweeps see it, and its prior counter is left untouched —
        # `clear_absent_rows` must treat it as present, not resolved.
        assert dispatch.count == 1
        assert alert.count == 0
        assert resolved == []
        assert state.board_doctor_row_state("drive-queue", key)["count"] == 1

        # Contrast: once the row is genuinely gone (not merely suppressed —
        # e.g. the base went green and the row itself also resolved), the
        # ordinary clear-on-absence path still fires exactly as before.
        sweep(
            [],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=alert,
            escalation_resolver=lambda queue, k: resolved.append((queue, k)),
        )
        assert resolved == [("drive-queue", key)]
        assert state.board_doctor_row_state("drive-queue", key) == {}

    def test_a_suppressed_row_is_logged_but_never_escalates(self) -> None:
        alert = _Recorder()
        key = "repo#22"
        cause = "ci_red"
        suppressed_row = replace(_row(key, cause), suppressed=True)

        report = None
        for _ in range(5):
            report = sweep(
                [suppressed_row],
                tier_of=_always(TIER_ALERT),
                alert_dispatcher=alert,
            )

        # Never alerted, however many sweeps see it past the convergence
        # limit — the collapsed baseline-red row is what alerts instead.
        assert alert.count == 0
        assert report is not None
        [decision] = report.decisions
        assert decision.action == "suppressed-baseline-red"
        assert decision.escalated is False


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


def _parked_assignment(
    *,
    assignment_id: str,
    repo: str,
    issue: int,
    type: str = "work",  # noqa: A002
    test_reason: str | None = None,
) -> Assignment:
    return Assignment(
        machine_name="m1",
        repo_name=repo,
        issue_number=issue,
        issue_title=f"issue-{issue}-x",
        assignment_id=assignment_id,
        status="done",
        branch=f"issue-{issue}-x",
        type=type,
        test_state=TEST_STATE_BLOCKED,
        test_reason=test_reason if test_reason is not None else mute_smoke_tally(MUTE_SMOKE_LEG_BUDGET),
    )


class TestSmokeNoVerdictRows:
    """#3760 Action 5 / #2244: `smoke_no_verdict_rows` is the detector half
    of `coord.board_doctor_permissions.sweep_smoke_requeue` — it must only
    surface a row the handler can actually clear.
    """

    def test_a_row_parked_at_the_mute_budget_is_selected(self) -> None:
        board = Board(
            completed=[
                _parked_assignment(assignment_id="aid-x", repo="acme/widgets", issue=1),
            ]
        )

        [row] = smoke_no_verdict_rows(board)

        assert row.queue == "smoke-no-verdict"
        assert row.key == "acme/widgets#1"
        assert row.cause == CAUSE_SMOKE_NO_VERDICT
        assert row.evidence == (f"mute_smoke_legs={MUTE_SMOKE_LEG_BUDGET}",)

    def test_a_row_below_the_mute_budget_is_not_selected(self) -> None:
        board = Board(
            completed=[
                _parked_assignment(
                    assignment_id="aid-x",
                    repo="acme/widgets",
                    issue=2,
                    test_reason=mute_smoke_tally(MUTE_SMOKE_LEG_BUDGET - 1),
                ),
            ]
        )

        assert smoke_no_verdict_rows(board) == []

    def test_a_row_not_parked_at_all_is_not_selected(self) -> None:
        board = Board(
            completed=[
                Assignment(
                    machine_name="m1",
                    repo_name="acme/widgets",
                    issue_number=3,
                    issue_title="issue-3-x",
                    assignment_id="aid-x",
                    status="done",
                    test_state="passed",
                ),
            ]
        )

        assert smoke_no_verdict_rows(board) == []

    def test_a_3315_environmental_retry_park_is_not_picked_up(self) -> None:
        # Its own tally marker text does not match `mute_smoke_legs`'s
        # regex, so this cause never fires for it.
        board = Board(
            completed=[
                _parked_assignment(
                    assignment_id="aid-x",
                    repo="acme/widgets",
                    issue=4,
                    test_reason=ENVIRONMENTAL_SMOKE_MARKER,
                ),
            ]
        )

        assert smoke_no_verdict_rows(board) == []

    def test_a_3737_abandoned_background_park_is_not_picked_up(self) -> None:
        board = Board(
            completed=[
                _parked_assignment(
                    assignment_id="aid-x",
                    repo="acme/widgets",
                    issue=5,
                    test_reason=ABANDONED_BACKGROUND_SMOKE_MARKER,
                ),
            ]
        )

        assert smoke_no_verdict_rows(board) == []

    def test_a_test_author_row_at_the_mute_budget_is_not_selected(self) -> None:
        # `coord.state.reset_work_test_state`/`test_reset_candidates` can
        # only clear this type with an `assignment_id` this detector has no
        # row to carry (`StuckRow` has no such field) — surfacing it here
        # would mint a row `sweep_smoke_requeue` could never actually clear.
        board = Board(
            completed=[
                _parked_assignment(
                    assignment_id="aid-x",
                    repo="acme/widgets",
                    issue=6,
                    type="test-author",
                ),
            ]
        )

        assert smoke_no_verdict_rows(board) == []

    def test_two_parked_rows_on_the_same_issue_collapse_to_one(self) -> None:
        board = Board(
            completed=[
                _parked_assignment(assignment_id="aid-a", repo="acme/widgets", issue=7),
                _parked_assignment(assignment_id="aid-b", repo="acme/widgets", issue=7),
            ]
        )

        rows = smoke_no_verdict_rows(board)

        assert [row.key for row in rows] == ["acme/widgets#7"]

    def test_rows_on_different_issues_both_surface(self) -> None:
        board = Board(
            completed=[
                _parked_assignment(assignment_id="aid-a", repo="acme/widgets", issue=8),
                _parked_assignment(assignment_id="aid-b", repo="acme/gadgets", issue=9),
            ]
        )

        rows = smoke_no_verdict_rows(board)

        assert {row.key for row in rows} == {"acme/widgets#8", "acme/gadgets#9"}

    def test_active_assignments_are_also_scanned(self) -> None:
        board = Board(
            active=[
                _parked_assignment(assignment_id="aid-x", repo="acme/widgets", issue=10),
            ]
        )

        [row] = smoke_no_verdict_rows(board)

        assert row.key == "acme/widgets#10"
