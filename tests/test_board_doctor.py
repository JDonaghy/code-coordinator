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

import pytest

from coord import queue_diagnose, state
from coord.board_doctor import (
    ALERT_SKIPPED,
    CAUSE_BASELINE_RED,
    CAUSE_CEILING_BLOCKED_MERGEABLE,
    CAUSE_CHECKS_STALE_RACE,
    CAUSE_SMOKE_NO_VERDICT,
    TIER_ALERT,
    TIER_AUTO,
    StuckRow,
    _reclassify_ceiling_blocked,
    _reclassify_checks_stale_race,
    _refresh_ceiling_blocked_settled_rows,
    baseline_red_rows,
    drive_queue_rows,
    no_progress_rows,
    smoke_no_verdict_rows,
    sweep,
)
from coord.config import BoardDoctorConfig, Config, ConfigError, _parse_board_doctor
from coord.drive_queue import BoardView, IssueFacts, QueueEntry
from coord.models import Assignment, Board, BoardDoctorRepoConfig, Repo
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


@pytest.fixture(autouse=True)
def _isolated_block_log(tmp_path, monkeypatch):
    """#3889: `sweep`'s outcome-backfill now also reads `coord.block_log`
    (:func:`coord.board_doctor._board_doctor_human_resolutions`) for
    attribution evidence whenever a decision is still pending a verdict.
    Isolate it for every test in this module so none of them can read —
    or be made flaky by — whatever happens to be in the real operator log
    at ``~/.coord/queue-block-log.jsonl``.
    """
    monkeypatch.setenv("COORD_BLOCK_LOG", str(tmp_path / "block-log.jsonl"))


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


_STALE_RACE_GIVEUP_REASON = (
    "merge attempted 3 times without landing.\n"
    "   Last board state: status='CONFLICT' reason='none'\n"
    "   Last `coord merge --only` diagnostic:\n"
    "     CI stale: checks predate the current base — rebase onto main"
)


def _seed_stale_race_blocked_entry(repo: str, issue: int) -> None:
    """A drive-queue row whose owning drive died burning its whole
    merge-attempt budget against a `checks_stale`-rooted refusal (#3859,
    quadraui#1961's shape)."""
    state.enqueue_drive_queue(repo, issue, after=[])
    state.update_drive_queue_entry(
        repo, issue, state="blocked", last_reason=_STALE_RACE_GIVEUP_REASON,
    )


def _stale_race_config(repo: str, *, opted_in: bool = True) -> Config:
    """A `Config` whose fleet + per-repo `stale_race` opt-in are both set to
    *opted_in* — the same two-gate shape `_stale_race_tier_enabled` reads."""
    return Config(
        repos=[
            Repo(
                name=repo, github=repo,
                board_doctor=BoardDoctorRepoConfig(stale_race=opted_in),
            )
        ],
        machines=[],
        board_doctor=BoardDoctorConfig(stale_race=opted_in),
    )


class TestReclassifyChecksStaleRace:
    """#3859: relabel a checks-stale-race-but-live-clear row."""

    def test_nothing_blocking_row_is_reclassified(self) -> None:
        _seed_stale_race_blocked_entry("acme/widgets", 201)
        row = _row("acme/widgets#201", queue_diagnose.CAUSE_NOTHING_BLOCKING, fresh=True)

        [out] = _reclassify_checks_stale_race(
            [row], config=_stale_race_config("acme/widgets")
        )

        assert out.cause == CAUSE_CHECKS_STALE_RACE
        assert out.evidence[:-1] == row.evidence
        assert "#3859" in out.evidence[-1]

    def test_repo_that_has_not_opted_in_is_left_alone(self) -> None:
        # Gating (#3859 review): the classification half of this feature
        # must not be live for a repo that never opted into the
        # `stale_race` tier, even though the row would otherwise qualify.
        _seed_stale_race_blocked_entry("acme/widgets", 299)
        row = _row("acme/widgets#299", queue_diagnose.CAUSE_NOTHING_BLOCKING, fresh=True)

        [out] = _reclassify_checks_stale_race(
            [row], config=_stale_race_config("acme/widgets", opted_in=False)
        )

        assert out == row

    def test_stale_row_is_left_alone(self) -> None:
        _seed_stale_race_blocked_entry("acme/widgets", 202)
        row = _row("acme/widgets#202", queue_diagnose.CAUSE_NOTHING_BLOCKING, fresh=False)

        [out] = _reclassify_checks_stale_race(
            [row], config=_stale_race_config("acme/widgets")
        )

        assert out == row

    def test_low_confidence_row_is_left_alone(self) -> None:
        _seed_stale_race_blocked_entry("acme/widgets", 203)
        row = StuckRow(
            queue="drive-queue", key="acme/widgets#203", state="stuck",
            cause=queue_diagnose.CAUSE_NOTHING_BLOCKING,
            confidence=queue_diagnose.CONFIDENCE_LOW,
            evidence=(f"cause={queue_diagnose.CAUSE_NOTHING_BLOCKING}",),
            fresh=True,
        )

        [out] = _reclassify_checks_stale_race(
            [row], config=_stale_race_config("acme/widgets")
        )

        assert out == row

    def test_a_row_blocked_for_a_different_reason_is_left_alone(self) -> None:
        # Exhausted merge attempts, but the last diagnostic names a real
        # conflict, not CI staleness — must not relabel a genuine block.
        state.enqueue_drive_queue("acme/widgets", 204, after=[])
        state.update_drive_queue_entry(
            "acme/widgets", 204, state="blocked",
            last_reason=(
                "merge attempted 3 times without landing.\n"
                "   Last `coord merge --only` diagnostic:\n"
                "     merge conflict in foo.py"
            ),
        )
        row = _row("acme/widgets#204", queue_diagnose.CAUSE_NOTHING_BLOCKING, fresh=True)

        [out] = _reclassify_checks_stale_race(
            [row], config=_stale_race_config("acme/widgets")
        )

        assert out == row

    def test_ceiling_blocked_row_is_reclassified_as_ceiling_not_stale_race(self) -> None:
        # The two reclassifiers key on disjoint marker text — a row that IS
        # #2972-ceiling-blocked (no "checks_stale" marker) must not also
        # match the stale-race predicate.
        _seed_ceiling_blocked_entry("acme/widgets", 205)
        row = _row("acme/widgets#205", queue_diagnose.CAUSE_NOTHING_BLOCKING, fresh=True)

        [out] = _reclassify_checks_stale_race(
            [row], config=_stale_race_config("acme/widgets")
        )

        assert out == row

    def test_merge_queue_rows_are_never_reclassified(self) -> None:
        _seed_stale_race_blocked_entry("acme/widgets", 206)
        row = _row(
            "acme/widgets#206", queue_diagnose.CAUSE_NOTHING_BLOCKING,
            queue="merge-queue", fresh=True,
        )

        [out] = _reclassify_checks_stale_race(
            [row], config=_stale_race_config("acme/widgets")
        )

        assert out == row

    def test_unparseable_key_is_left_alone(self) -> None:
        row = _row("no-hash-separator", queue_diagnose.CAUSE_NOTHING_BLOCKING, fresh=True)

        [out] = _reclassify_checks_stale_race(
            [row], config=_stale_race_config("acme/widgets")
        )

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

    def test_escalated_row_realerts_on_exponential_backoff_not_forever_silent(
        self,
    ) -> None:
        """A row that never gets better must not go quiet after its one
        alert — it keeps getting re-flagged, on a backoff that doubles
        each time it fires, rather than converging to permanent silence.
        """
        from datetime import datetime, timedelta, timezone

        dispatch = _Recorder()
        alert = _Recorder()
        key = "repo#13"
        cause = "ci_red"
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)

        # Sweeps 1-2 dispatch; sweep 3 hits the limit and alerts once.
        for i in range(3):
            sweep(
                [_row(key, cause)],
                tier_of=_always(TIER_AUTO),
                auto_dispatchers={cause: dispatch},
                alert_dispatcher=alert,
                convergence_limit=3,
                now=lambda i=i: base + timedelta(minutes=i),
            )
        assert alert.count == 1

        # Still inside the 1h initial backoff window: no re-alert yet.
        sweep(
            [_row(key, cause)],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=alert,
            convergence_limit=3,
            now=lambda: base + timedelta(minutes=30),
        )
        assert alert.count == 1

        # Past the 1h window (last alert fired at base+2min): re-alerts,
        # and the wait doubles to 2h.
        sweep(
            [_row(key, cause)],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=alert,
            convergence_limit=3,
            now=lambda: base + timedelta(hours=1, minutes=5),
        )
        assert alert.count == 2
        assert state.board_doctor_row_state("drive-queue", key)["backoff_hours"] == 2.0

        # Inside the NEW 2h window (last alert now at base+1h5min): no
        # re-alert yet.
        sweep(
            [_row(key, cause)],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=alert,
            convergence_limit=3,
            now=lambda: base + timedelta(hours=2, minutes=35),
        )
        assert alert.count == 2

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


class TestOutcomeTracking:
    """A logged `auto-dispatch`/`alert` decision gets its own ``outcome``
    filled in once the row's state at a LATER sweep answers whether it
    actually worked — never left permanently ``None``.
    """

    def test_resolved_row_is_recorded_as_progressed(self) -> None:
        dispatch = _Recorder()
        key = "repo#20"
        cause = "ci_red"

        sweep(
            [_row(key, cause)],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=_Recorder(),
            convergence_limit=3,
            sweep_id="sweep-1",
        )

        # The row resolves — absent from the next sweep's walk entirely.
        sweep(
            [],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=_Recorder(),
            convergence_limit=3,
            sweep_id="sweep-2",
        )

        logged = [
            d for d in state.board_doctor_decisions()
            if d["sweep_id"] == "sweep-1" and d["key"] == key
        ]
        assert logged[0]["outcome"] == "progressed"

    def test_still_stuck_on_the_same_cause_is_recorded_as_unchanged(self) -> None:
        dispatch = _Recorder()
        key = "repo#21"
        cause = "ci_red"

        sweep(
            [_row(key, cause)],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=_Recorder(),
            convergence_limit=3,
            sweep_id="sweep-1",
        )

        # Still stuck on the exact same cause next sweep.
        sweep(
            [_row(key, cause)],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=_Recorder(),
            convergence_limit=3,
            sweep_id="sweep-2",
        )

        logged = [
            d for d in state.board_doctor_decisions()
            if d["sweep_id"] == "sweep-1" and d["key"] == key
        ]
        assert logged[0]["outcome"] == "unchanged"

    def test_freshly_escalating_after_an_action_is_recorded_as_regressed(self) -> None:
        dispatch = _Recorder()
        alert = _Recorder()
        key = "repo#22"
        cause = "ci_red"

        # First sweep: under the convergence limit (2), so it
        # auto-dispatches rather than alerting.
        report = sweep(
            [_row(key, cause)],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=alert,
            convergence_limit=2,
            sweep_id="sweep-1",
        )
        assert report.decisions[0].action == "auto-dispatch"

        # Second sweep: still the same cause, now AT the limit, so it
        # escalates — the auto action from the first sweep did not help.
        sweep(
            [_row(key, cause)],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=alert,
            convergence_limit=2,
            sweep_id="sweep-2",
        )

        logged = [
            d for d in state.board_doctor_decisions()
            if d["sweep_id"] == "sweep-1" and d["key"] == key
        ]
        assert logged[0]["outcome"] == "regressed"

    def test_only_actionable_decisions_ever_get_an_outcome_key(self) -> None:
        """A `no-dispatcher`/`converged-noop`/deferred row was never
        actually acted on, so its decision must never claim a pending
        verdict that will never arrive.
        """
        key = "repo#23"
        cause = "unmapped-cause"

        sweep(
            [_row(key, cause)],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={},
            alert_dispatcher=_Recorder(),
            convergence_limit=3,
        )

        logged = [d for d in state.board_doctor_decisions() if d["key"] == key]
        assert logged[0]["action"] == "no-dispatcher"
        assert "outcome" not in logged[0]


class TestEvaluateBoardDoctorOutcomeAttribution:
    """#3889: :func:`coord.state.evaluate_board_doctor_outcome`'s
    ``attributed_to_other`` parameter — pure, so the attribution rule is
    testable without any block log or sweep machinery.
    """

    def test_resolved_with_no_attribution_evidence_is_progressed(self) -> None:
        assert state.evaluate_board_doctor_outcome(
            {"cause": "ci_red", "escalated": False}, None,
        ) == "progressed"

    def test_resolved_with_attribution_evidence_is_progressed_by_other(self) -> None:
        assert state.evaluate_board_doctor_outcome(
            {"cause": "ci_red", "escalated": False}, None,
            attributed_to_other=True,
        ) == "progressed_by_other"

    def test_a_changed_cause_with_attribution_evidence_is_also_progressed_by_other(
        self,
    ) -> None:
        decision = {"cause": "ci_red", "escalated": False}
        current = {"cause": "merge-conflict", "escalated": False}
        assert state.evaluate_board_doctor_outcome(
            decision, current, attributed_to_other=True,
        ) == "progressed_by_other"

    def test_attribution_evidence_never_changes_an_unchanged_verdict(self) -> None:
        same = {"cause": "ci_red", "escalated": False}
        assert state.evaluate_board_doctor_outcome(
            same, dict(same), attributed_to_other=True,
        ) == "unchanged"

    def test_attribution_evidence_never_changes_a_regressed_verdict(self) -> None:
        decision = {"cause": "ci_red", "escalated": False}
        current = {"cause": "ci_red", "escalated": True}
        assert state.evaluate_board_doctor_outcome(
            decision, current, attributed_to_other=True,
        ) == "regressed"


class TestSweepAttributesOutcomesHonestly:
    """#3889 end-to-end: the row that actually decides ``attributed_to_other``
    comes from :mod:`coord.block_log`, consulted by :func:`sweep`'s own
    outcome-backfill — a doctor action with no human evidence anywhere
    still reads as ``"progressed"`` (:class:`TestOutcomeTracking` already
    covers that); an operator action in the same window downgrades it.
    """

    def test_an_operator_remove_before_the_next_sweep_is_progressed_by_other(
        self,
    ) -> None:
        from datetime import datetime, timezone

        from coord.block_log import enter_event, operator_resolution_event, record
        from coord.drive_queue import QueueEntry as _QE

        key = "acme/widgets#302"
        cause = "ci_red"
        dispatch = _Recorder()
        t0 = 1_800_000_000.0

        sweep(
            [_row(key, cause)],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=_Recorder(),
            convergence_limit=3,
            sweep_id="sweep-1",
            now=lambda: datetime.fromtimestamp(t0, tz=timezone.utc),
        )
        assert dispatch.count == 1

        # An operator ran `coord drive-queue remove` on the stalled entry
        # AFTER the decision above was recorded.
        entry = _QE(
            repo="acme/widgets", issue=302, state="blocked", attempts=1,
            last_reason="ci red",
        )
        record(
            [
                enter_event(entry, state="blocked", reason="ci red", now=t0 - 10.0),
                operator_resolution_event(
                    entry, resolution="operator_removed", now=t0 + 60.0,
                ),
            ]
        )

        # The row is gone from the very next sweep's walk entirely.
        sweep(
            [],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=_Recorder(),
            convergence_limit=3,
            sweep_id="sweep-2",
            now=lambda: datetime.fromtimestamp(t0 + 120.0, tz=timezone.utc),
        )

        logged = [
            d for d in state.board_doctor_decisions()
            if d["sweep_id"] == "sweep-1" and d["key"] == key
        ]
        assert logged[0]["outcome"] == "progressed_by_other"

    def test_an_operator_remove_BEFORE_the_decision_does_not_count(self) -> None:
        """A `human_acted` episode that resolved BEFORE this decision was
        even recorded is unrelated to it — attribution only looks forward
        from the decision's own ``recorded_at``."""
        from datetime import datetime, timezone

        from coord.block_log import enter_event, operator_resolution_event, record
        from coord.drive_queue import QueueEntry as _QE

        key = "acme/widgets#303"
        cause = "ci_red"
        dispatch = _Recorder()
        t0 = 1_800_000_000.0

        entry = _QE(
            repo="acme/widgets", issue=303, state="blocked", attempts=1,
            last_reason="ci red (earlier incident)",
        )
        record(
            [
                enter_event(entry, state="blocked", reason="earlier", now=t0 - 120.0),
                operator_resolution_event(
                    entry, resolution="operator_removed", now=t0 - 60.0,
                ),
            ]
        )

        sweep(
            [_row(key, cause)],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=_Recorder(),
            convergence_limit=3,
            sweep_id="sweep-1",
            now=lambda: datetime.fromtimestamp(t0, tz=timezone.utc),
        )

        sweep(
            [],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=_Recorder(),
            convergence_limit=3,
            sweep_id="sweep-2",
            now=lambda: datetime.fromtimestamp(t0 + 120.0, tz=timezone.utc),
        )

        logged = [
            d for d in state.board_doctor_decisions()
            if d["sweep_id"] == "sweep-1" and d["key"] == key
        ]
        assert logged[0]["outcome"] == "progressed"


class TestDecisionLogRetentionAndFilters:
    """A durable, filterable log — trimmed by AGE rather than
    a fixed entry count, and queryable by the same shape
    ``coord drive-queue doctor-log`` exposes.
    """

    def test_entries_older_than_the_retention_window_are_dropped(self) -> None:
        """#3889: retention is a dedicated prune (:func:`prune_board_doctor_log`),
        run once per sweep — not an implicit side effect of every single
        :func:`record_board_doctor_decision` call."""
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        old = now - timedelta(days=state.BOARD_DOCTOR_LOG_RETENTION_DAYS + 1)
        recent = now - timedelta(days=1)

        state.record_board_doctor_decision(
            {
                "sweep_id": "s-old", "queue": "drive-queue", "key": "acme/x#1",
                "cause": "ci_red", "tier": "auto", "action": "converged-noop",
                "count": 1, "escalated": False, "confidence": "high",
                "evidence": [], "recorded_at": old.isoformat(),
            }
        )
        state.record_board_doctor_decision(
            {
                "sweep_id": "s-recent", "queue": "drive-queue", "key": "acme/x#2",
                "cause": "ci_red", "tier": "auto", "action": "converged-noop",
                "count": 1, "escalated": False, "confidence": "high",
                "evidence": [], "recorded_at": recent.isoformat(),
            }
        )

        # Both present until the prune actually runs.
        keys = {d["key"] for d in state.board_doctor_decisions()}
        assert keys == {"acme/x#1", "acme/x#2"}

        deleted = state.prune_board_doctor_log(now=now)
        assert deleted == 1

        keys = {d["key"] for d in state.board_doctor_decisions()}
        assert keys == {"acme/x#2"}

    def test_prune_respects_a_custom_retention_days(self) -> None:
        """#3889 (#3875 ask C-adjacent): ``coordinator.yml``'s
        ``board_doctor.log_retention_days`` overrides the 90-day default —
        a 7-day window drops an 8-day-old entry the default would keep."""
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        eight_days_old = now - timedelta(days=8)

        state.record_board_doctor_decision(
            {
                "sweep_id": "s-1", "queue": "drive-queue", "key": "acme/x#1",
                "cause": "ci_red", "tier": "auto", "action": "converged-noop",
                "count": 1, "escalated": False, "confidence": "high",
                "evidence": [], "recorded_at": eight_days_old.isoformat(),
            }
        )

        # Default (90-day) retention keeps it.
        assert state.prune_board_doctor_log(now=now) == 0
        assert {d["key"] for d in state.board_doctor_decisions()} == {"acme/x#1"}

        # A 7-day retention window drops it.
        assert state.prune_board_doctor_log(retention_days=7, now=now) == 1
        assert state.board_doctor_decisions() == []

    def test_filters_by_repo_issue_and_action_and_since(self) -> None:
        from datetime import datetime, timedelta, timezone

        base = datetime.now(timezone.utc) - timedelta(days=1)
        for i, (key, action) in enumerate(
            [
                ("acme/x#1", "auto-dispatch"),
                ("acme/x#2", "alert"),
                ("acme/y#1", "auto-dispatch"),
            ]
        ):
            state.record_board_doctor_decision(
                {
                    "sweep_id": f"s-{i}", "queue": "drive-queue", "key": key,
                    "cause": "ci_red", "tier": "auto", "action": action,
                    "count": 1, "escalated": False, "confidence": "high",
                    "evidence": [], "recorded_at": (base + timedelta(minutes=i)).isoformat(),
                }
            )

        assert {d["key"] for d in state.board_doctor_decisions(repo="acme/x")} == {
            "acme/x#1", "acme/x#2",
        }
        assert {d["key"] for d in state.board_doctor_decisions(repo="acme/x", issue=1)} == {
            "acme/x#1",
        }
        assert {d["key"] for d in state.board_doctor_decisions(action="alert")} == {
            "acme/x#2",
        }
        since_ts = (base + timedelta(minutes=1)).timestamp()
        assert {d["key"] for d in state.board_doctor_decisions(since=since_ts)} == {
            "acme/x#2", "acme/y#1",
        }


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


class TestNeverDispatchedRowReachesTheSweep:
    """#3874: `coord drive-queue sweep` must list, classify and auto-fix a
    never-dispatched `blocked` row instead of silently dropping it the
    moment `queue_diagnose` would otherwise abstain forever.

    End-to-end through the real adapters the CLI's own `collect_rows` uses
    — `queue_diagnose.run_pass` -> :func:`coord.board_doctor.drive_queue_rows`
    -> `sweep`, with the real `policy_tier_of` (not a stubbed `_always`), so
    this is load-bearing evidence the cause is actually wired to the
    `drive_queue_requeue` auto-action, not a tautology.
    """

    def test_a_never_dispatched_row_is_classified_and_auto_fixed(self) -> None:
        from coord.board_doctor_policy import policy_tier_of
        from coord.drive_queue import STATE_BLOCKED

        entry = QueueEntry(
            repo="acme/widgets",
            issue=2035,
            state=STATE_BLOCKED,
            attempts=0,
            last_reason=(
                "acme/widgets#2035's merge gate could not be read this tick "
                "(no merge-queue row for this entry, even after the "
                "self-heal enqueue attempt)"
            ),
        )
        episode = {
            "key": entry.key,
            "state": STATE_BLOCKED,
            "diagnoses": 0,
            "diagnosed_cause": "",
        }

        class _NoBranchProbe:
            def probe(self, entry_):
                return LiveState(
                    probe_errors=(f"gh: no branch known for {entry_.key}",),
                    no_assignment_ever=True,
                )

        diagnoses = queue_diagnose.run_pass([entry], [episode], probe=_NoBranchProbe())
        rows = drive_queue_rows(diagnoses)

        [row] = rows
        assert row.key == entry.key
        assert row.cause == queue_diagnose.CAUSE_NEVER_DISPATCHED

        auto = _Recorder()
        alert = _Recorder()
        sweep(
            rows,
            tier_of=policy_tier_of,
            auto_dispatchers={queue_diagnose.CAUSE_NEVER_DISPATCHED: auto},
            alert_dispatcher=alert,
        )
        assert auto.count == 1
        assert alert.count == 0


class TestParseNoProgressMinutes:
    """#3888 review: `no_progress_minutes` validates the same way its
    sibling `followthrough_cap` already does — tested here since
    `_parse_board_doctor` had no coverage of this branch at all."""

    def test_default_is_sixty(self) -> None:
        assert _parse_board_doctor(None).no_progress_minutes == 60

    def test_custom_value_is_accepted(self) -> None:
        cfg = _parse_board_doctor({"no_progress_minutes": 30})
        assert cfg.no_progress_minutes == 30

    def test_zero_is_rejected(self) -> None:
        with pytest.raises(ConfigError):
            _parse_board_doctor({"no_progress_minutes": 0})

    def test_non_integer_is_rejected(self) -> None:
        with pytest.raises(ConfigError):
            _parse_board_doctor({"no_progress_minutes": "60"})


class TestParseBoardDoctorLogAndBackoffKnobs:
    """#3889 (#3875 asks B/C): ``board_doctor.log_retention_days`` /
    ``backoff_initial_hours`` / ``backoff_cap_hours`` / ``fixit_leg_budget``
    parse the same way every other knob in this section does."""

    def test_defaults_match_the_hardcoded_fallbacks(self) -> None:
        cfg = _parse_board_doctor(None)
        assert cfg.log_retention_days == 90
        assert cfg.backoff_initial_hours == 1.0
        assert cfg.backoff_cap_hours == 24.0
        assert cfg.fixit_leg_budget == 1

    def test_custom_values_are_accepted(self) -> None:
        cfg = _parse_board_doctor(
            {
                "log_retention_days": 30,
                "backoff_initial_hours": 2.0,
                "backoff_cap_hours": 48.0,
                "fixit_leg_budget": 3,
            }
        )
        assert cfg.log_retention_days == 30
        assert cfg.backoff_initial_hours == 2.0
        assert cfg.backoff_cap_hours == 48.0
        assert cfg.fixit_leg_budget == 3

    def test_log_retention_days_rejects_non_positive(self) -> None:
        with pytest.raises(ConfigError, match="log_retention_days"):
            _parse_board_doctor({"log_retention_days": 0})

    def test_backoff_initial_hours_rejects_non_positive(self) -> None:
        with pytest.raises(ConfigError, match="backoff_initial_hours"):
            _parse_board_doctor({"backoff_initial_hours": 0})

    def test_backoff_cap_hours_below_initial_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="backoff_cap_hours"):
            _parse_board_doctor({"backoff_initial_hours": 10.0, "backoff_cap_hours": 5.0})

    def test_fixit_leg_budget_rejects_non_positive(self) -> None:
        with pytest.raises(ConfigError, match="fixit_leg_budget"):
            _parse_board_doctor({"fixit_leg_budget": 0})

    def test_fixit_leg_budget_rejects_non_integer(self) -> None:
        with pytest.raises(ConfigError, match="fixit_leg_budget"):
            _parse_board_doctor({"fixit_leg_budget": "3"})


class TestBoardDoctorLogKnobsTakeEffectWithoutRestart:
    """#3889 config acceptance: a changed knob affects the very next
    :func:`sweep`/:func:`prune_board_doctor_log` call — there is no cache
    to invalidate, no daemon to restart. Threading the value through a
    plain function argument (never a module-level constant baked in at
    import time) is what makes this true by construction."""

    def test_a_custom_retention_window_prunes_more_aggressively(self) -> None:
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        eight_days_old = now - timedelta(days=8)
        state.record_board_doctor_decision(
            {
                "sweep_id": "s-1", "queue": "drive-queue", "key": "acme/x#9",
                "cause": "ci_red", "tier": "auto", "action": "converged-noop",
                "count": 1, "escalated": False, "confidence": "high",
                "evidence": [], "recorded_at": eight_days_old.isoformat(),
            }
        )

        # The fleet default (90 days) keeps it...
        assert state.prune_board_doctor_log(now=now) == 0
        # ...but an operator who just tightened `board_doctor.log_retention_days`
        # to 7 sees it pruned on the very next call — no restart, no cache.
        assert state.prune_board_doctor_log(retention_days=7, now=now) == 1

    def test_a_custom_backoff_schedule_changes_when_the_next_alert_is_due(self) -> None:
        """A counter with no persisted ``backoff_hours`` (predating that
        field) falls back entirely to the caller-supplied
        *backoff_initial_hours* — the live config knob, once threaded
        through, decides the answer with no restart needed."""
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        legacy_counter = {
            "escalated": True,
            "last_alert_at": (now - timedelta(hours=3)).isoformat(),
        }

        # Under a 6h initial backoff, 3h elapsed is not due yet.
        assert state.board_doctor_alert_due(
            legacy_counter, now=now, backoff_initial_hours=6.0,
        ) is False
        # Under the OLD 1h default, the SAME 3h elapsed already IS due —
        # proof the custom schedule, not the elapsed time, decided it.
        assert state.board_doctor_alert_due(legacy_counter, now=now) is True

    def test_a_fresh_counter_starts_its_backoff_clock_at_the_custom_initial_value(self) -> None:
        counter = state.next_board_doctor_counter(
            None, "ci_red", backoff_initial_hours=6.0,
        )
        assert counter["backoff_hours"] == 6.0


class TestNoProgressRows:
    """#3888: classify every no-progress drive-queue row, not just the
    subset `block_log`'s episode walk and :func:`merge_queue_rows` already
    cover. Replays the 2026-11-05 incident's three shapes (quadraui#2035 —
    a frozen `blocked` dependent; #2038 — a `running` row whose PR merged
    by hand; #2047 — a `waiting` row whose prereqs had all landed with a
    free repo slot sitting open) so all three reach the sweep's output.
    """

    @staticmethod
    def _config() -> Config:
        return Config(repos=[Repo(name="acme/widgets", github="acme/widgets")], machines=[])

    def test_running_no_session_no_activity_is_classified(self) -> None:
        # quadraui#2038 shape.
        entry = QueueEntry(
            repo="acme/widgets",
            issue=2038,
            state="running",
            session_name="drive-acme-widgets-2038",
            launched_at=0.0,
        )
        probe = _FakeProbe({"acme/widgets#2038": LiveState(pr_state="MERGED")})

        rows = no_progress_rows(
            [entry],
            covered_keys=frozenset(),
            live_sessions=frozenset(),
            board_view=BoardView(),
            probe=probe,
            config=self._config(),
            now=100_000.0,
        )

        [row] = rows
        assert row.key == entry.key
        assert row.cause == queue_diagnose.CAUSE_PR_MERGED
        assert "running-no-session-no-activity" in row.evidence[-1]

    def test_foreign_running_row_is_not_classified_dead(self) -> None:
        # #3888 review: a `running` row launched on another host must never
        # be judged dead off this (local) tmux read alone — the quadraui
        # cross-host shape the blocking review finding named.
        entry = QueueEntry(
            repo="acme/widgets",
            issue=2039,
            state="running",
            launch_host="otherhost",
            launched_at=0.0,
        )
        probe = _FakeProbe({})

        rows = no_progress_rows(
            [entry],
            covered_keys=frozenset(),
            live_sessions=frozenset(),
            board_view=BoardView(),
            probe=probe,
            config=self._config(),
            now=100_000.0,
            local_host="thishost",
        )

        assert rows == []
        assert probe.calls == []

    def test_blocked_row_with_no_episode_is_classified_unknown_and_alerts(self) -> None:
        # quadraui#2035 shape: no probe evidence at all survives to explain
        # it, so this must land on `unknown` rather than being dropped.
        entry = QueueEntry(
            repo="acme/widgets",
            issue=2035,
            state="blocked",
            last_reason="pre-req acme/widgets#2000 is queued but blocked",
            reason_at=0.0,
        )
        probe = _FakeProbe({"acme/widgets#2035": LiveState()})

        rows = no_progress_rows(
            [entry],
            covered_keys=frozenset(),
            live_sessions=frozenset(),
            board_view=BoardView(),
            probe=probe,
            config=self._config(),
            now=100_000.0,
        )

        [row] = rows
        assert row.key == entry.key
        assert row.cause == queue_diagnose.CAUSE_UNKNOWN
        assert row.confidence == queue_diagnose.CONFIDENCE_NONE
        assert "pre-req acme/widgets#2000 is queued but blocked" in row.evidence[-1]
        # #3888 review nit: a `blocked` row's evidence must not claim no
        # episode exists at all — one may well exist, just dropped for a
        # stored `unknown` cause.
        assert "dropped" in row.evidence[-1]

    def test_waiting_with_prereqs_landed_and_free_slot_is_classified(self) -> None:
        # quadraui#2047 shape.
        entry = QueueEntry(
            repo="acme/widgets",
            issue=2047,
            state="waiting",
            after=("acme/widgets#2000",),
            reason_at=0.0,
        )
        probe = _FakeProbe({"acme/widgets#2047": LiveState()})

        rows = no_progress_rows(
            [entry],
            covered_keys=frozenset(),
            live_sessions=frozenset(),
            board_view=BoardView(issues={"acme/widgets#2000": IssueFacts(issue_state="closed")}),
            probe=probe,
            config=self._config(),
            now=100_000.0,
        )

        [row] = rows
        assert row.key == entry.key
        assert row.cause == queue_diagnose.CAUSE_UNKNOWN
        assert "waiting-ready-not-launched" in row.evidence[-1]

    def test_waiting_with_prereq_done_in_queue_but_not_yet_landed_on_board(self) -> None:
        # #3888 review (non-blocking): `_resolve_prereqs` itself (#2715)
        # treats a dep as satisfied on EITHER `facts.landed` OR the dep's
        # own drive-queue row already reading `STATE_DONE_LIKE` — the
        # `issues` cache backing `landed` can run minutes behind a merge
        # the queue performed itself. This dep's row is `done`, not yet
        # reflected as `landed` on the board.
        from coord.drive_queue import STATE_DONE

        dep = QueueEntry(repo="acme/widgets", issue=2000, state=STATE_DONE)
        entry = QueueEntry(
            repo="acme/widgets",
            issue=2047,
            state="waiting",
            after=("acme/widgets#2000",),
            reason_at=0.0,
        )
        probe = _FakeProbe({"acme/widgets#2047": LiveState()})

        rows = no_progress_rows(
            [dep, entry],
            covered_keys=frozenset(),
            live_sessions=frozenset(),
            board_view=BoardView(),
            probe=probe,
            config=self._config(),
            now=100_000.0,
        )

        assert {row.key for row in rows} == {entry.key}

    def test_all_three_2026_11_05_shapes_reach_one_sweep_output(self) -> None:
        # Three different repos — each shape's own fixture is independently
        # the ONLY row that could occupy its own repo's capacity, so a
        # free-slot check on one shape can never accidentally exclude
        # another. See `test_same_repo_2026_11_05_shapes_reach_one_sweep_
        # output` below for the actual incident shape, all in one repo.
        running = QueueEntry(
            repo="acme/widgets",
            issue=2038,
            state="running",
            session_name="drive-acme-widgets-2038",
            launched_at=0.0,
        )
        blocked = QueueEntry(
            repo="acme/sprockets",
            issue=2035,
            state="blocked",
            last_reason="pre-req acme/sprockets#2000 is queued but blocked",
            reason_at=0.0,
        )
        waiting = QueueEntry(
            repo="acme/gadgets",
            issue=2047,
            state="waiting",
            after=("acme/gadgets#2000",),
            reason_at=0.0,
        )
        probe = _FakeProbe(
            {
                "acme/widgets#2038": LiveState(),
                "acme/sprockets#2035": LiveState(),
                "acme/gadgets#2047": LiveState(),
            }
        )

        rows = no_progress_rows(
            [running, blocked, waiting],
            covered_keys=frozenset(),
            live_sessions=frozenset(),
            board_view=BoardView(issues={"acme/gadgets#2000": IssueFacts(issue_state="closed")}),
            probe=probe,
            config=self._config(),
            now=100_000.0,
        )

        assert {row.key for row in rows} == {running.key, blocked.key, waiting.key}

    def test_same_repo_2026_11_05_shapes_reach_one_sweep_output(self) -> None:
        # #3888 review blocking finding: the real 2026-11-05 incident put
        # all three rows in ONE repo (`DEFAULT_MAX_PARALLEL_PER_REPO == 1`),
        # with the `running` row itself the phantom quadraui#2038 shape (PR
        # merged by hand). A raw `state == running` occupancy count would
        # keep counting that phantom row as occupying the repo's only slot
        # forever, permanently excluding the `waiting` row from
        # classification — reproducing the incident rather than replaying
        # it as a fixed regression test. Taking occupancy from
        # `compute_running_occupancy` (the scheduler's own verdict) instead
        # sees `facts.landed` on the phantom row and frees the slot.
        running = QueueEntry(
            repo="acme/widgets",
            issue=2038,
            state="running",
            session_name="drive-acme-widgets-2038",
            launched_at=0.0,
        )
        blocked = QueueEntry(
            repo="acme/widgets",
            issue=2035,
            state="blocked",
            last_reason="pre-req acme/widgets#2000 is queued but blocked",
            reason_at=0.0,
        )
        waiting = QueueEntry(
            repo="acme/widgets",
            issue=2047,
            state="waiting",
            after=("acme/widgets#2000",),
            reason_at=0.0,
        )
        probe = _FakeProbe(
            {
                "acme/widgets#2038": LiveState(pr_state="MERGED"),
                "acme/widgets#2035": LiveState(),
                "acme/widgets#2047": LiveState(),
            }
        )

        rows = no_progress_rows(
            [running, blocked, waiting],
            covered_keys=frozenset(),
            live_sessions=frozenset(),
            board_view=BoardView(
                issues={
                    # The phantom `running` row's own key reads `landed`
                    # (merged by hand) — this is what frees its repo slot.
                    "acme/widgets#2038": IssueFacts(issue_state="closed"),
                    "acme/widgets#2000": IssueFacts(issue_state="closed"),
                }
            ),
            probe=probe,
            config=self._config(),
            now=100_000.0,
        )

        assert {row.key for row in rows} == {running.key, blocked.key, waiting.key}

    def test_row_inside_the_no_progress_window_is_not_classified(self) -> None:
        entry = QueueEntry(repo="acme/widgets", issue=2036, state="blocked", reason_at=99_000.0)
        probe = _FakeProbe({})

        rows = no_progress_rows(
            [entry],
            covered_keys=frozenset(),
            live_sessions=frozenset(),
            board_view=BoardView(),
            probe=probe,
            config=self._config(),
            now=100_000.0,
        )

        assert rows == []
        assert probe.calls == []

    def test_already_covered_key_is_skipped(self) -> None:
        entry = QueueEntry(repo="acme/widgets", issue=2037, state="blocked", reason_at=0.0)
        probe = _FakeProbe({})

        rows = no_progress_rows(
            [entry],
            covered_keys=frozenset({entry.key}),
            live_sessions=frozenset(),
            board_view=BoardView(),
            probe=probe,
            config=self._config(),
            now=100_000.0,
        )

        assert rows == []
        assert probe.calls == []

    def test_waiting_row_with_unlanded_prereq_is_not_classified(self) -> None:
        entry = QueueEntry(
            repo="acme/widgets",
            issue=2048,
            state="waiting",
            after=("acme/widgets#2000",),
            reason_at=0.0,
        )
        probe = _FakeProbe({})

        rows = no_progress_rows(
            [entry],
            covered_keys=frozenset(),
            live_sessions=frozenset(),
            board_view=BoardView(issues={"acme/widgets#2000": IssueFacts(issue_state="open")}),
            probe=probe,
            config=self._config(),
            now=100_000.0,
        )

        assert rows == []
        assert probe.calls == []

    def test_waiting_row_with_no_free_repo_slot_is_not_classified(self) -> None:
        # Occupies the repo's only slot (default ceiling 1), but recently
        # enough itself (``launched_at`` 100s old) that it is not, on its
        # own, a no-progress row this pass would also report.
        running_entry = QueueEntry(
            repo="acme/widgets", issue=2049, state="running", launched_at=99_900.0,
        )
        waiting_entry = QueueEntry(
            repo="acme/widgets", issue=2050, state="waiting", after=(), reason_at=0.0,
        )
        probe = _FakeProbe({})

        rows = no_progress_rows(
            [running_entry, waiting_entry],
            covered_keys=frozenset(),
            live_sessions=frozenset(),
            board_view=BoardView(),
            probe=probe,
            config=self._config(),
            now=100_000.0,
        )

        assert rows == []
        assert probe.calls == []

    def test_running_row_with_live_session_is_not_classified(self) -> None:
        entry = QueueEntry(
            repo="acme/widgets",
            issue=2051,
            state="running",
            session_name="drive-acme-widgets-2051",
            launched_at=0.0,
        )
        probe = _FakeProbe({})

        rows = no_progress_rows(
            [entry],
            covered_keys=frozenset(),
            live_sessions=frozenset({"drive-acme-widgets-2051"}),
            board_view=BoardView(),
            probe=probe,
            config=self._config(),
            now=100_000.0,
        )

        assert rows == []
        assert probe.calls == []

    def test_running_row_with_active_assignment_is_not_classified(self) -> None:
        entry = QueueEntry(repo="acme/widgets", issue=2052, state="running", launched_at=0.0)
        probe = _FakeProbe({})

        rows = no_progress_rows(
            [entry],
            covered_keys=frozenset(),
            live_sessions=frozenset(),
            board_view=BoardView(issues={entry.key: IssueFacts(active_work=True)}),
            probe=probe,
            config=self._config(),
            now=100_000.0,
        )

        assert rows == []
        assert probe.calls == []

    def test_a_probe_failure_still_classifies_unknown_instead_of_dropping(self) -> None:
        entry = QueueEntry(repo="acme/widgets", issue=2053, state="blocked", reason_at=0.0)
        probe = _FakeProbe({"acme/widgets#2053": RuntimeError("gh timed out")})

        rows = no_progress_rows(
            [entry],
            covered_keys=frozenset(),
            live_sessions=frozenset(),
            board_view=BoardView(),
            probe=probe,
            config=self._config(),
            now=100_000.0,
        )

        [row] = rows
        assert row.cause == queue_diagnose.CAUSE_UNKNOWN

    def test_probe_budget_exhaustion_still_classifies_unknown_not_dropped(self) -> None:
        # #3888 review nit: once the per-sweep probe cap is spent, a
        # further matching row must still be returned (never silently
        # dropped) — just unprobed, as `unknown`, for the next sweep to
        # pick up.
        from coord.board_doctor import _NO_PROGRESS_PROBE_CAP

        entries = [
            QueueEntry(
                repo="acme/widgets", issue=3000 + i, state="blocked", reason_at=0.0,
            )
            for i in range(_NO_PROGRESS_PROBE_CAP + 1)
        ]
        probe = _FakeProbe(
            {e.key: LiveState() for e in entries}
        )

        rows = no_progress_rows(
            entries,
            covered_keys=frozenset(),
            live_sessions=frozenset(),
            board_view=BoardView(),
            probe=probe,
            config=self._config(),
            now=100_000.0,
        )

        assert len(rows) == len(entries)
        assert len(probe.calls) == _NO_PROGRESS_PROBE_CAP
        unprobed = [row for row in rows if row.key not in probe.calls]
        assert len(unprobed) == 1
        assert unprobed[0].cause == queue_diagnose.CAUSE_UNKNOWN

    def test_no_progress_blocked_row_is_reclassified_as_ceiling_blocked(self) -> None:
        # #3888 review blocking finding: `collect_rows` must run the SAME
        # #2972 fix-round-ceiling reclassifier over a no-progress row that
        # it already runs over an episode-sourced row. This composes the
        # two pure building blocks the way the fixed `collect_rows` now
        # orders them, standing in for that (otherwise untestable, fully
        # impure) wiring.
        _seed_ceiling_blocked_entry("acme/widgets", 2972)
        entry = QueueEntry(
            repo="acme/widgets",
            issue=2972,
            state="blocked",
            last_reason="giving up after 2 fix rounds (#2972)",
            reason_at=0.0,
        )
        live = LiveState(
            pr_number=1,
            pr_state="OPEN",
            mergeable=True,
            checks=(queue_diagnose.CheckReading("test", "success"),),
            gate_ready=True,
            agent_reachable=True,
        )
        probe = _FakeProbe({"acme/widgets#2972": live})

        rows = no_progress_rows(
            [entry],
            covered_keys=frozenset(),
            live_sessions=frozenset(),
            board_view=BoardView(),
            probe=probe,
            config=self._config(),
            now=100_000.0,
        )
        [row] = rows
        assert row.cause == queue_diagnose.CAUSE_NOTHING_BLOCKING
        assert row.fresh is True

        [reclassified] = _reclassify_ceiling_blocked(rows)
        assert reclassified.cause == CAUSE_CEILING_BLOCKED_MERGEABLE


class TestIntegrationRedClassification:
    """#3882: a `ci-red` row whose own branch Test verdict already read
    green must classify as `integration-red` — the merged-tree-only
    failure shape (quadraui#2028) a fix leg built from the branch alone
    can never reproduce. A row whose branch Test verdict is ALSO red (or
    simply unread) must stay plain `ci-red`: collapsing an unread verdict
    into "integration" would manufacture a confidently-wrong cause out of
    a probe gap."""

    @staticmethod
    def _entry() -> QueueEntry:
        from coord.drive_queue import STATE_BLOCKED

        return QueueEntry(
            repo="acme/widgets", issue=2028, state=STATE_BLOCKED, attempts=1,
            last_reason="checks failed: build (failure)",
        )

    def test_ci_red_with_green_branch_test_is_integration_red(self) -> None:
        from coord.queue_diagnose import CheckReading

        live = LiveState(
            checks=(CheckReading(name="build", conclusion="failure"),),
            branch_test_state="passed",
        )
        diagnosis = queue_diagnose.diagnose(self._entry(), live)
        assert diagnosis.cause == queue_diagnose.CAUSE_INTEGRATION_RED

    def test_ci_red_with_red_branch_test_stays_plain_ci_red(self) -> None:
        from coord.queue_diagnose import CheckReading

        live = LiveState(
            checks=(CheckReading(name="build", conclusion="failure"),),
            branch_test_state="failed",
        )
        diagnosis = queue_diagnose.diagnose(self._entry(), live)
        assert diagnosis.cause == queue_diagnose.CAUSE_CI_RED
        # The branch Test reading is visible in the record even though it
        # wasn't decisive — an operator reading a `ci-red` diagnosis can see
        # why it did NOT come back `integration-red`.
        assert any(
            "coord gates (test decision): branch Test verdict reads 'failed'" in line
            for line in diagnosis.evidence
        )

    def test_ci_red_with_unread_branch_test_stays_plain_ci_red(self) -> None:
        from coord.queue_diagnose import CheckReading

        live = LiveState(
            checks=(CheckReading(name="build", conclusion="failure"),),
            branch_test_state=None,
        )
        diagnosis = queue_diagnose.diagnose(self._entry(), live)
        assert diagnosis.cause == queue_diagnose.CAUSE_CI_RED

    def test_ci_red_with_skipped_branch_test_stays_plain_ci_red(self) -> None:
        """A `skipped` Test verdict is a structural claim about the SHAPE
        of the diff ("nothing to smoke-test"), never a claim the code
        behaved correctly at this SHA — there is no confirmed green half
        to split the CI failure away from, so this must NOT read as
        `integration-red`. A `baseline_red`-confirmed skip in particular
        means the BASE is what's broken; relabeling it `integration-red`
        would steer it away from `coord.board_doctor.CAUSE_BASELINE_RED`,
        the cause that actually names that shape."""
        from coord.queue_diagnose import CheckReading

        live = LiveState(
            checks=(CheckReading(name="build", conclusion="failure"),),
            branch_test_state="skipped",
        )
        diagnosis = queue_diagnose.diagnose(self._entry(), live)
        assert diagnosis.cause == queue_diagnose.CAUSE_CI_RED


class TestIntegrationRedRowReachesTheInvestigateTier:
    """End-to-end through the real adapters, the same shape as
    `TestNeverDispatchedRowReachesTheSweep` — load-bearing evidence the
    cause is actually wired to the `drive_queue_requeue`-style fixit-leg
    auto-dispatcher via the real `policy_tier_of`, not a tautology."""

    def test_an_integration_red_row_is_classified_and_investigated(self) -> None:
        from coord.board_doctor_policy import policy_tier_of
        from coord.drive_queue import STATE_BLOCKED
        from coord.queue_diagnose import CheckReading

        entry = QueueEntry(
            repo="acme/widgets", issue=2028, state=STATE_BLOCKED, attempts=1,
            last_reason="checks failed: build (failure)",
        )
        episode = {
            "key": entry.key, "state": STATE_BLOCKED, "diagnoses": 0,
            "diagnosed_cause": "",
        }

        class _Probe:
            def probe(self, entry_):
                return LiveState(
                    checks=(CheckReading(name="build", conclusion="failure"),),
                    branch_test_state="passed",
                )

        diagnoses = queue_diagnose.run_pass([entry], [episode], probe=_Probe())
        rows = drive_queue_rows(diagnoses)

        [row] = rows
        assert row.key == entry.key
        assert row.cause == queue_diagnose.CAUSE_INTEGRATION_RED

        auto = _Recorder()
        alert = _Recorder()
        sweep(
            rows,
            tier_of=policy_tier_of,
            auto_dispatchers={queue_diagnose.CAUSE_INTEGRATION_RED: auto},
            alert_dispatcher=alert,
        )
        assert auto.count == 1
        assert alert.count == 0


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
