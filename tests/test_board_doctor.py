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
from coord import board_doctor_followthrough as ft
from coord.board_doctor import (
    ALERT_SKIPPED,
    CAUSE_BASELINE_RED,
    CAUSE_CEILING_BLOCKED_MERGEABLE,
    CAUSE_CHECKS_STALE_RACE,
    CAUSE_MERGE_NEVER_ATTEMPTED,
    CAUSE_MERGED_PARTIAL,
    CAUSE_SMOKE_NO_VERDICT,
    CAUSE_STRANDED_PREREQ,
    CAUSE_STRANDED_PREREQ_BLOCKED,
    MERGE_PENDING_STALE_AFTER_SECONDS,
    STRANDED_PREREQ_OPT_OUT_LABEL,
    TIER_ALERT,
    TIER_AUTO,
    StuckRow,
    _reclassify_ceiling_blocked,
    _reclassify_checks_stale_race,
    _reclassify_merge_never_attempted,
    _refresh_ceiling_blocked_settled_rows,
    baseline_red_rows,
    dispatch_plan_leg,
    drive_queue_rows,
    merged_partial_rows,
    no_progress_rows,
    run_followthrough_sweep,
    smoke_no_verdict_rows,
    stranded_prereq_dep,
    stranded_prereq_guard,
    stranded_prereq_rows,
    sweep,
)
from coord.config import (
    BoardDoctorConfig,
    Config,
    ConfigError,
    PreflightGuardConfig,
    PreflightGuardRepoConfig,
    _parse_board_doctor,
)
from coord.drive_queue import BoardView, IssueFacts, QueueEntry, STATE_MERGED_PARTIAL, STATE_WAITING
from coord.models import Assignment, Board, BoardDoctorRepoConfig, Machine, Repo
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
    (:func:`coord.board_doctor._board_doctor_other_resolutions`) for
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


def _widgets_config() -> Config:
    repo = Repo(
        name="acme/widgets", github="acme/widgets",
        default_branch="main", test_command="pytest",
    )
    return Config(
        repos=[repo],
        machines=[
            Machine(
                name="laptop", host="laptop.tail",
                repos=["acme/widgets"], repo_paths={"acme/widgets": "/work"},
            ),
        ],
    )


class TestReclassifyMergeNeverAttempted:
    """#3929 (#3918 gap 1, second half), quadraui#2160: a merge-queue entry
    that has NEVER been attempted at all (``last_attempt is None``) is
    invisible to both :func:`coord.board_doctor.merge_queue_rows` and
    :data:`coord.board_doctor._MERGE_QUEUE_STUCK_CAUSE` — a no-progress
    drive-queue row stuck only because `coord merge` never got to it must
    be promoted to :data:`CAUSE_MERGE_NEVER_ATTEMPTED` instead of idling or
    escalating toward the deadline, but ONLY once a live re-check confirms
    every gate ahead of the merge attempt itself actually reads clear.
    """

    @staticmethod
    def _merge_entry(**overrides):
        from coord import merge_queue as mq

        base = dict(
            assignment_id="w2160", repo_name="acme/widgets", repo_github="acme/widgets",
            branch="issue-2160-fix", target_branch="main", issue_number=2160,
            issue_title="Fix the thing", state=mq.PENDING, last_attempt=None,
            # No open PR and an explicit, gate-free required_gates list:
            # keeps `entry_gate_status` hermetic (no `gh` call at all), same
            # trick `tests/test_board_doctor_policy.py::
            # TestConfirmMergePendingStuck` uses — these tests exercise the
            # LIVE-CONFIRM CONTROL FLOW, not a live network read.
            pr_number=None, required_gates=["merge"], error="",
        )
        base.update(overrides)
        return mq.QueuedMerge(**base)

    def test_an_unknown_no_progress_row_with_a_never_attempted_merge_row_is_promoted(
        self,
    ) -> None:
        entry = self._merge_entry()
        row = _row("acme/widgets#2160", queue_diagnose.CAUSE_UNKNOWN, fresh=True)

        [out] = _reclassify_merge_never_attempted(
            [row], [entry], board=Board(), config=_widgets_config(),
        )

        assert out.cause == CAUSE_MERGE_NEVER_ATTEMPTED
        assert out.evidence[:-1] == row.evidence
        assert "last_attempt=None" in out.evidence[-1]

    def test_a_no_pr_row_with_a_never_attempted_merge_row_is_also_promoted(self) -> None:
        # `CAUSE_NO_PR` is the OTHER incoming cause this reclassifier may
        # promote away from (`_AFTER_PREREQS_LANDED_SAFE_CAUSES`) — a merge
        # never attempted at all usually means `coord merge` never even
        # created the PR yet.
        entry = self._merge_entry()
        row = _row("acme/widgets#2160", queue_diagnose.CAUSE_NO_PR, fresh=True)

        [out] = _reclassify_merge_never_attempted(
            [row], [entry], board=Board(), config=_widgets_config(),
        )

        assert out.cause == CAUSE_MERGE_NEVER_ATTEMPTED

    def test_an_attempted_and_refused_merge_row_keeps_todays_behaviour(self) -> None:
        """Acceptance item 3: a row whose merge WAS attempted (and is
        refused/pending its ordinary turn) must be left exactly as the
        caller already classified it — `last_attempt` set at all disables
        this reclassifier entirely, regardless of what the live gate would
        say."""
        entry = self._merge_entry(last_attempt=1000.0, error="review not approved")
        row = _row("acme/widgets#2160", queue_diagnose.CAUSE_UNKNOWN, fresh=True)

        [out] = _reclassify_merge_never_attempted(
            [row], [entry], board=Board(), config=_widgets_config(),
        )

        assert out == row

    def test_a_row_whose_gates_are_not_actually_clear_is_left_alone(self) -> None:
        # The merge gate itself still refuses (review required) — a
        # never-attempted merge row is not, on its own, proof the row is
        # ready for a merge attempt.
        entry = self._merge_entry(required_gates=["review", "merge"])
        row = _row("acme/widgets#2160", queue_diagnose.CAUSE_UNKNOWN, fresh=True)

        [out] = _reclassify_merge_never_attempted(
            [row], [entry], board=Board(), config=_widgets_config(),
        )

        assert out == row

    def test_drops_the_row_when_no_matching_merge_entry_is_found(self) -> None:
        row = _row("acme/widgets#2160", queue_diagnose.CAUSE_UNKNOWN, fresh=True)

        [out] = _reclassify_merge_never_attempted(
            [row], [], board=Board(), config=_widgets_config(),
        )

        assert out == row

    def test_a_row_whose_live_confirm_raises_is_left_alone(self, monkeypatch) -> None:
        def _boom(*a, **k):
            raise RuntimeError("gh hiccup")

        monkeypatch.setattr("coord.merge_queue.entry_gate_status", _boom)

        entry = self._merge_entry()
        row = _row("acme/widgets#2160", queue_diagnose.CAUSE_UNKNOWN, fresh=True)

        [out] = _reclassify_merge_never_attempted(
            [row], [entry], board=Board(), config=_widgets_config(),
        )

        assert out == row

    def test_leaves_other_causes_and_queues_untouched(self) -> None:
        other = _row("acme/widgets#1", queue_diagnose.CAUSE_PR_MERGED, queue="drive-queue")

        [out] = _reclassify_merge_never_attempted(
            [other], [], board=Board(), config=_widgets_config(),
        )

        assert out == other

    def test_merge_queue_rows_are_never_promoted(self) -> None:
        entry = self._merge_entry()
        row = _row(
            "acme/widgets#2160", queue_diagnose.CAUSE_UNKNOWN,
            queue="merge-queue", fresh=True,
        )

        [out] = _reclassify_merge_never_attempted(
            [row], [entry], board=Board(), config=_widgets_config(),
        )

        assert out == row

    def test_a_stale_episode_reconstruction_is_never_promoted(self) -> None:
        """Non-blocking review finding: `row.fresh=False` — a row rebuilt by
        `_episode_stuck_row` from an arbitrarily old stored `cause=unknown`
        — carries no live evidence of its own, same guard
        `_reclassify_after_prereqs_landed`/`_reclassify_ceiling_blocked`/
        `_reclassify_checks_stale_race` already apply."""
        entry = self._merge_entry()
        row = _row("acme/widgets#2160", queue_diagnose.CAUSE_UNKNOWN, fresh=False)

        [out] = _reclassify_merge_never_attempted(
            [row], [entry], board=Board(), config=_widgets_config(),
        )

        assert out == row

    def test_a_clear_gates_nothing_blocking_row_with_a_pr_is_also_promoted(self) -> None:
        """Non-blocking review finding: quadraui#2160's own shape is an open
        PR with every check green, review approved and `mergeable` true —
        `queue_diagnose._rule_nothing_blocking` reads that as `nothing-
        blocking`, not `unknown`/`no-pr`, so this cause must be eligible
        too."""
        entry = self._merge_entry()
        row = _row("acme/widgets#2160", queue_diagnose.CAUSE_NOTHING_BLOCKING, fresh=True)

        [out] = _reclassify_merge_never_attempted(
            [row], [entry], board=Board(), config=_widgets_config(),
        )

        assert out.cause == CAUSE_MERGE_NEVER_ATTEMPTED

    def test_a_freshly_enqueued_entry_is_not_raced_by_this_sweep(self) -> None:
        """Non-blocking review finding: a `PENDING`/`last_attempt is None`
        entry enqueued only moments ago is, per `merge_queue_rows`'s own
        reasoning for the STALE-pending shape, "very likely still alive and
        polling" under the ordinary auto-drain tick — this sweep must not
        race it with its own merge attempt before it has had a chance to
        age past `MERGE_PENDING_STALE_AFTER_SECONDS`."""
        now = 10_000.0
        entry = self._merge_entry(enqueued_at=now - 60.0)
        row = _row("acme/widgets#2160", queue_diagnose.CAUSE_UNKNOWN, fresh=True)

        [out] = _reclassify_merge_never_attempted(
            [row], [entry], board=Board(), config=_widgets_config(), now=now,
        )

        assert out == row

    def test_an_aged_past_the_bound_entry_is_still_promoted(self) -> None:
        now = 10_000.0
        entry = self._merge_entry(enqueued_at=now - MERGE_PENDING_STALE_AFTER_SECONDS - 1.0)
        row = _row("acme/widgets#2160", queue_diagnose.CAUSE_UNKNOWN, fresh=True)

        [out] = _reclassify_merge_never_attempted(
            [row], [entry], board=Board(), config=_widgets_config(), now=now,
        )

        assert out.cause == CAUSE_MERGE_NEVER_ATTEMPTED


class TestMergeNeverAttemptedAndReviewTestStaleAreWiredToRealActions:
    """Acceptance (#3929): `CAUSE_REVIEW_STALE`/`CAUSE_TEST_STALE` dispatch
    a fresh review/test, and `CAUSE_MERGE_NEVER_ATTEMPTED` dispatches a
    merge attempt — never the generic investigate-tier fix-it leg. Asserts
    on `coord.board_doctor_policy`'s own closed allow-list (the same
    surface `coord.board_doctor.default_tier_of`/`sweep` consult), not a
    duplicate of that module's own dispatcher-level tests
    (`tests/test_board_doctor_policy.py`)."""

    def test_review_stale_and_test_stale_dispatch_a_restage_never_a_fixit_leg(self) -> None:
        import coord.board_doctor_policy as board_doctor_policy

        assert board_doctor_policy.ALLOWLIST[queue_diagnose.CAUSE_REVIEW_STALE] == "restage"
        assert board_doctor_policy.ALLOWLIST[queue_diagnose.CAUSE_TEST_STALE] == "restage"
        assert queue_diagnose.CAUSE_REVIEW_STALE not in board_doctor_policy.INVESTIGATE_CAUSES
        assert queue_diagnose.CAUSE_TEST_STALE not in board_doctor_policy.INVESTIGATE_CAUSES

    def test_merge_never_attempted_dispatches_a_merge_attempt(self) -> None:
        import coord.board_doctor_policy as board_doctor_policy

        assert board_doctor_policy.ALLOWLIST[CAUSE_MERGE_NEVER_ATTEMPTED] == "merge"
        assert CAUSE_MERGE_NEVER_ATTEMPTED not in board_doctor_policy.INVESTIGATE_CAUSES


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

    def test_clear_absent_rows_false_leaves_the_outcome_log_entry_open(self) -> None:
        """#3907 review (blocking): a partial walk is a signal the caller
        has explicitly declared untrustworthy — ``_grade_open_outcomes``
        must not write a terminal ``progressed`` verdict for a row that
        is merely absent from a ``clear_absent_rows=False`` pass, the
        same contract :func:`sweep` itself upholds for the row's
        counter."""
        dispatch = _Recorder()
        alert = _Recorder()
        key = "repo#14"
        cause = "ci_red"

        sweep(
            [_row(key, cause)],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=alert,
            sweep_id="sweep-1",
        )
        pending = [
            d for d in state.board_doctor_decisions()
            if d["sweep_id"] == "sweep-1" and d["key"] == key
        ]
        assert pending[0]["outcome"] is None

        # A deliberately empty/partial walk with clear_absent_rows=False
        # must not grade this decision as resolved — the row was never
        # actually observed this pass.
        sweep(
            [],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=alert,
            clear_absent_rows=False,
            sweep_id="sweep-2",
        )
        still_pending = [
            d for d in state.board_doctor_decisions()
            if d["sweep_id"] == "sweep-1" and d["key"] == key
        ]
        assert still_pending[0]["outcome"] is None


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
        """#3907: grading happens once the grace window has elapsed, not
        on the very next sweep — so this threads ``now=`` across a gap
        wider than the default `state.BOARD_DOCTOR_OUTCOME_GRACE_HOURS`.
        """
        from datetime import datetime, timezone

        dispatch = _Recorder()
        key = "repo#21"
        cause = "ci_red"
        t0 = 1_900_000_000.0

        sweep(
            [_row(key, cause)],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=_Recorder(),
            convergence_limit=3,
            sweep_id="sweep-1",
            now=lambda: datetime.fromtimestamp(t0, tz=timezone.utc),
        )

        # Still stuck on the exact same cause, well past the grace window.
        sweep(
            [_row(key, cause)],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=_Recorder(),
            convergence_limit=3,
            sweep_id="sweep-2",
            now=lambda: datetime.fromtimestamp(
                t0 + state.BOARD_DOCTOR_OUTCOME_GRACE_HOURS * 3600.0 + 60.0, tz=timezone.utc,
            ),
        )

        logged = [
            d for d in state.board_doctor_decisions()
            if d["sweep_id"] == "sweep-1" and d["key"] == key
        ]
        assert logged[0]["outcome"] == "unchanged"

    def test_escalation_moments_after_dispatch_is_deferred_not_regressed(self) -> None:
        """#3907: the doctor's OWN next-sweep ``alert`` for the exact same
        cause, minutes after its ``auto-dispatch``, is not graded at all
        yet — the leg that dispatch launched has barely had time to
        start, let alone land, so there's no honest verdict to give.
        """
        from datetime import datetime, timezone

        dispatch = _Recorder()
        alert = _Recorder()
        key = "repo#22"
        cause = "ci_red"
        t0 = 1_900_000_000.0

        # First sweep: under the convergence limit (2), so it
        # auto-dispatches rather than alerting.
        report = sweep(
            [_row(key, cause)],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=alert,
            convergence_limit=2,
            sweep_id="sweep-1",
            now=lambda: datetime.fromtimestamp(t0, tz=timezone.utc),
        )
        assert report.decisions[0].action == "auto-dispatch"

        # Ten minutes later: still the same cause, now AT the limit, so
        # the doctor escalates — its OWN action, taken while the leg the
        # first sweep launched is still almost certainly running.
        sweep(
            [_row(key, cause)],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=alert,
            convergence_limit=2,
            sweep_id="sweep-2",
            now=lambda: datetime.fromtimestamp(t0 + 600.0, tz=timezone.utc),
        )

        logged = [
            d for d in state.board_doctor_decisions()
            if d["sweep_id"] == "sweep-1" and d["key"] == key
        ]
        assert logged[0]["outcome"] is None

    def test_still_regressed_once_the_grace_window_has_genuinely_elapsed(self) -> None:
        """The deferral in the test above is not permanent silence: once
        `BOARD_DOCTOR_OUTCOME_GRACE_HOURS` has genuinely passed with no
        improvement, the decision DOES grade `regressed`."""
        from datetime import datetime, timezone

        dispatch = _Recorder()
        alert = _Recorder()
        key = "repo#22b"
        cause = "ci_red"
        t0 = 1_900_000_000.0

        sweep(
            [_row(key, cause)],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=alert,
            convergence_limit=2,
            sweep_id="sweep-1",
            now=lambda: datetime.fromtimestamp(t0, tz=timezone.utc),
        )
        sweep(
            [_row(key, cause)],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=alert,
            convergence_limit=2,
            sweep_id="sweep-2",
            now=lambda: datetime.fromtimestamp(t0 + 600.0, tz=timezone.utc),
        )
        # Well past the grace window, and the row is STILL stuck on the
        # exact same (now escalated) cause — this is genuinely bad news.
        sweep(
            [_row(key, cause)],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=alert,
            convergence_limit=2,
            sweep_id="sweep-3",
            now=lambda: datetime.fromtimestamp(
                t0 + state.BOARD_DOCTOR_OUTCOME_GRACE_HOURS * 3600.0 + 60.0, tz=timezone.utc,
            ),
        )

        logged = [
            d for d in state.board_doctor_decisions()
            if d["sweep_id"] == "sweep-1" and d["key"] == key
        ]
        assert logged[0]["outcome"] == "regressed"

    def test_regressed_decision_is_later_regraded_progressed_once_the_row_merges(
        self,
    ) -> None:
        """#3907 end to end: auto-dispatch, an alert the doctor raises on
        itself minutes later (deferred), still stuck once the grace
        window elapses (graded ``regressed``), and the row finally
        merges a couple of sweeps after that — the ORIGINAL decision's
        outcome flips from ``regressed`` to ``progressed`` rather than
        staying wrong forever."""
        from datetime import datetime, timezone

        dispatch = _Recorder()
        alert = _Recorder()
        key = "repo#22c"
        cause = "ci_red"
        t0 = 1_900_000_000.0

        sweep(
            [_row(key, cause)],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=alert,
            convergence_limit=2,
            sweep_id="sweep-1",
            now=lambda: datetime.fromtimestamp(t0, tz=timezone.utc),
        )
        sweep(
            [_row(key, cause)],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=alert,
            convergence_limit=2,
            sweep_id="sweep-2",
            now=lambda: datetime.fromtimestamp(t0 + 600.0, tz=timezone.utc),
        )
        sweep(
            [_row(key, cause)],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=alert,
            convergence_limit=2,
            sweep_id="sweep-3",
            now=lambda: datetime.fromtimestamp(
                t0 + state.BOARD_DOCTOR_OUTCOME_GRACE_HOURS * 3600.0 + 60.0, tz=timezone.utc,
            ),
        )
        logged_after_sweep_3 = [
            d for d in state.board_doctor_decisions()
            if d["sweep_id"] == "sweep-1" and d["key"] == key
        ]
        assert logged_after_sweep_3[0]["outcome"] == "regressed"

        # The fix leg finally lands — the row is gone from the walk.
        sweep(
            [],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=alert,
            convergence_limit=2,
            sweep_id="sweep-4",
            now=lambda: datetime.fromtimestamp(
                t0 + state.BOARD_DOCTOR_OUTCOME_GRACE_HOURS * 3600.0 + 7200.0, tz=timezone.utc,
            ),
        )

        logged_after_merge = [
            d for d in state.board_doctor_decisions()
            if d["sweep_id"] == "sweep-1" and d["key"] == key
        ]
        assert logged_after_merge[0]["outcome"] == "progressed"

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


class TestRecordBoardDoctorOutcomeValidatesVocabulary:
    """#3889 review nit: :data:`coord.state.BOARD_DOCTOR_OUTCOME_VALUES` is
    a declared vocabulary — :func:`coord.state.record_board_doctor_outcome`
    must actually enforce it rather than silently accepting any string."""

    def test_a_value_outside_the_frozenset_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="BOARD_DOCTOR_OUTCOME_VALUES"):
            state.record_board_doctor_outcome("sweep-1", "drive-queue", "repo#1", "bogus")

    def test_every_declared_value_is_accepted(self) -> None:
        for outcome in state.BOARD_DOCTOR_OUTCOME_VALUES:
            # No matching row exists — this is a no-op, not an error; the
            # point is that a VALID outcome never raises.
            state.record_board_doctor_outcome("sweep-x", "drive-queue", "repo#9", outcome)


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

    def test_an_operator_requeue_add_before_the_next_sweep_is_progressed_by_other(
        self,
    ) -> None:
        """#3889 review: `coord drive-queue add` re-queuing a stalled row is
        the SAME operator intervention `remove` already logs (see
        :func:`coord.commands.drive_queue._record_operator_release_entry`)
        — it must downgrade the verdict exactly like a `remove` does, not
        silently keep crediting the doctor."""
        from datetime import datetime, timezone

        from coord.block_log import enter_event, operator_resolution_event, record
        from coord.drive_queue import QueueEntry as _QE

        key = "acme/widgets#304"
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

        # An operator ran `coord drive-queue add` over the stalled entry
        # (the #2972 "bump --max-fix-rounds and re-add" remedy) AFTER the
        # decision above was recorded — `_record_operator_release_entry`
        # logs this with `resolution="operator_requeued"`, not
        # `"operator_removed"`.
        entry = _QE(
            repo="acme/widgets", issue=304, state="blocked", attempts=1,
            last_reason="ci red",
        )
        record(
            [
                enter_event(entry, state="blocked", reason="ci red", now=t0 - 10.0),
                operator_resolution_event(
                    entry, resolution="operator_requeued", now=t0 + 60.0,
                ),
            ]
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
        assert logged[0]["outcome"] == "progressed_by_other"

    def test_a_hand_merge_landed_resolution_before_the_next_sweep_is_progressed_by_other(
        self,
    ) -> None:
        """#3889 review: a hand merge / unrelated branch landing while the
        row sat blocked is `coord.block_log`'s ``_LANDED_CAUSE`` —
        ``human_acted=False`` but ``resolution == "landed"``. That is still
        "something other than the doctor's own confirmed action" (see
        `_LANDED_CAUSE`'s own docstring), so it must downgrade the verdict
        too, not just the `human_acted=True` releases."""
        from datetime import datetime, timezone

        from coord.block_log import enter_event, plan_events, record
        from coord.drive_queue import (
            QueueEntry as _QE,
            Reconcile,
            STATE_DONE,
            TickPlan,
        )

        key = "acme/widgets#305"
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

        # Something else (a hand merge, an unrelated branch) landed the
        # work while the entry sat `blocked` — the tick's own `done`
        # reconcile, recorded AFTER the decision above.
        reason = "done — merged by hand while blocked"
        entry = _QE(
            repo="acme/widgets", issue=305, state="blocked", attempts=1,
            last_reason="ci red",
        )
        plan = TickPlan(
            reconciles=(
                Reconcile(key, "done", reason,
                           updates={"state": STATE_DONE, "last_reason": reason}),
            ),
        )
        events = plan_events([entry], plan, now=t0 + 60.0)
        assert [e["resolution"] for e in events] == ["landed"]
        record(
            [enter_event(entry, state="blocked", reason="ci red", now=t0 - 10.0), *events]
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
        assert logged[0]["outcome"] == "progressed_by_other"

    def test_an_override_human_required_before_the_next_sweep_is_progressed_by_other(
        self, monkeypatch,
    ) -> None:
        """`coord merge --override-human-required` releasing a latched
        merge-queue row is the SAME operator intervention `remove`/
        `add` already log, via :func:`coord.commands.drive_queue.
        _record_operator_release_entry` — it must downgrade the verdict
        exactly like those do, not leave the doctor crediting itself for a
        human's override.

        Drives the real helper (``synthesize_enter=True``, the merge
        queue's own ``human_required`` as the single latched state) rather
        than hand-writing an ``enter_event`` call directly — no production
        path ever calls ``enter_event(state="human_required", ...)`` itself,
        so a hand-written one could assert a log shape the command under
        test can never actually produce (#3898 review)."""
        from datetime import datetime, timezone

        from coord.commands.drive_queue import _record_operator_release_entry
        from coord.drive_queue import QueueEntry as _QE

        key = "acme/widgets#306"
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

        # An operator ran `coord merge --only --override-human-required`
        # on the latched entry AFTER the decision above was recorded — the
        # synthetic `enter` and the `resolve` land ten seconds apart, both
        # after `t0`, the same ordering `coord.commands.merge` produces.
        entry = _QE(
            repo="acme/widgets", issue=306, state="human_required",
            last_reason="gh pr merge ... not mergeable",
        )
        times = iter([t0 + 50.0, t0 + 60.0])
        monkeypatch.setattr("coord.block_log.time.time", lambda: next(times))
        _record_operator_release_entry(
            entry,
            resolution="operator_override",
            latched_states=frozenset({"human_required"}),
            synthesize_enter=True,
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
        assert logged[0]["outcome"] == "progressed_by_other"


class TestQuadrauiAutoDispatchReplayAndBackfill:
    """#3907: replays the exact shape of the 2026-11-07 quadraui incident —
    ten ``auto-dispatch`` decisions that all read ``outcome=regressed``
    despite most of their rows later reaching a done-like drive-queue
    state (``done``/``merged``/``issue closed``). One parametrized sweep
    sequence — auto-dispatch, an alert the doctor raises on itself at the
    very next sweep, then a merge two sweeps later — replayed against each
    of the six quadraui keys the issue names, asserting every one ends
    ``progressed`` rather than permanently ``regressed``.
    """

    @pytest.mark.parametrize(
        "issue_number",
        [1960, 1965, 1974, 1975, 2035, 2103],
    )
    def test_quadraui_key_ends_progressed_after_the_merge(self, issue_number: int) -> None:
        from datetime import datetime, timezone

        dispatch = _Recorder()
        alert = _Recorder()
        key = f"quadraui#{issue_number}"
        cause = "ci_red"
        t0 = 1_900_000_000.0

        sweep(
            [_row(key, cause)],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=alert,
            convergence_limit=2,
            sweep_id="sweep-1",
            now=lambda: datetime.fromtimestamp(t0, tz=timezone.utc),
        )

        # The doctor's own next-sweep alert — a Rust work/test/review
        # cycle on quadraui routinely runs past an hour, so the leg the
        # first sweep dispatched is almost certainly still running here.
        sweep(
            [_row(key, cause)],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=alert,
            convergence_limit=2,
            sweep_id="sweep-2",
            now=lambda: datetime.fromtimestamp(t0 + 3600.0, tz=timezone.utc),
        )
        pending_after_alert = [
            d for d in state.board_doctor_decisions()
            if d["sweep_id"] == "sweep-1" and d["key"] == key
        ]
        assert pending_after_alert[0]["outcome"] is None

        # Two sweeps after the dispatch, the fix leg's PR merges — the row
        # is gone from the walk entirely.
        sweep(
            [],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=alert,
            convergence_limit=2,
            sweep_id="sweep-3",
            now=lambda: datetime.fromtimestamp(t0 + 7200.0, tz=timezone.utc),
        )

        logged = [
            d for d in state.board_doctor_decisions()
            if d["sweep_id"] == "sweep-1" and d["key"] == key
        ]
        assert logged[0]["outcome"] == "progressed"

    def test_backfill_corrects_an_already_regressed_entry_once_the_row_resolves(
        self,
    ) -> None:
        """#3907 point 5: the one-shot
        :func:`coord.board_doctor.backfill_board_doctor_outcomes` re-grades
        an EXISTING ``regressed`` entry against the doctor's current
        per-row state, without needing another :func:`sweep` call at all.
        """
        from datetime import datetime, timezone

        from coord.board_doctor import backfill_board_doctor_outcomes

        dispatch = _Recorder()
        alert = _Recorder()
        key = "quadraui#2035"
        cause = "ci_red"
        t0 = 1_900_000_000.0

        sweep(
            [_row(key, cause)],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=alert,
            convergence_limit=2,
            sweep_id="sweep-1",
            now=lambda: datetime.fromtimestamp(t0, tz=timezone.utc),
            outcome_grace_hours=0.0,
        )
        # Force the ORIGINAL bug's exact shape: graded `regressed` on the
        # very next sweep (grace disabled) while the row is still stuck on
        # the same cause, now escalated.
        sweep(
            [_row(key, cause)],
            tier_of=_always(TIER_AUTO),
            auto_dispatchers={cause: dispatch},
            alert_dispatcher=alert,
            convergence_limit=2,
            sweep_id="sweep-2",
            now=lambda: datetime.fromtimestamp(t0 + 600.0, tz=timezone.utc),
            outcome_grace_hours=0.0,
        )
        regressed = [
            d for d in state.board_doctor_decisions()
            if d["sweep_id"] == "sweep-1" and d["key"] == key
        ]
        assert regressed[0]["outcome"] == "regressed"

        # The row resolves with no further sweep ever walking past it
        # again — `clear_board_doctor_row` has already dropped its
        # counter, so the key is no longer `tracked` at all.
        state.clear_board_doctor_row("drive-queue", key)

        changed = backfill_board_doctor_outcomes()
        assert changed >= 1

        regraded = [
            d for d in state.board_doctor_decisions()
            if d["sweep_id"] == "sweep-1" and d["key"] == key
        ]
        assert regraded[0]["outcome"] == "progressed"

    def test_backfill_is_a_noop_when_nothing_is_open_for_grading(self) -> None:
        """A terminal entry (already ``progressed``) is seeded first so a
        ``0`` result means "correctly found nothing open", not "never
        looked" — an empty DB cannot distinguish the two."""
        from datetime import datetime, timezone

        from coord.board_doctor import backfill_board_doctor_outcomes

        state.record_board_doctor_decision(
            {
                "sweep_id": "s-1", "queue": "drive-queue", "key": "acme/x#1",
                "cause": "ci_red", "tier": "auto", "action": "auto-dispatch",
                "count": 1, "escalated": False, "confidence": "high",
                "evidence": [], "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        state.record_board_doctor_outcome("s-1", "drive-queue", "acme/x#1", "progressed")

        assert backfill_board_doctor_outcomes() == 0

        unchanged = [
            d for d in state.board_doctor_decisions()
            if d["sweep_id"] == "s-1" and d["key"] == "acme/x#1"
        ]
        assert unchanged[0]["outcome"] == "progressed"


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
    — plus #3907's ``outcome_grace_hours`` — parse the same way every other
    knob in this section does."""

    def test_defaults_match_the_hardcoded_fallbacks(self) -> None:
        cfg = _parse_board_doctor(None)
        assert cfg.log_retention_days == 90
        assert cfg.backoff_initial_hours == 1.0
        assert cfg.backoff_cap_hours == 24.0
        assert cfg.fixit_leg_budget == 1
        assert cfg.outcome_grace_hours == 3.0

    def test_custom_values_are_accepted(self) -> None:
        cfg = _parse_board_doctor(
            {
                "log_retention_days": 30,
                "backoff_initial_hours": 2.0,
                "backoff_cap_hours": 48.0,
                "fixit_leg_budget": 3,
                "outcome_grace_hours": 6.0,
            }
        )
        assert cfg.log_retention_days == 30
        assert cfg.backoff_initial_hours == 2.0
        assert cfg.backoff_cap_hours == 48.0
        assert cfg.fixit_leg_budget == 3
        assert cfg.outcome_grace_hours == 6.0

    def test_outcome_grace_hours_rejects_non_positive(self) -> None:
        with pytest.raises(ConfigError, match="outcome_grace_hours"):
            _parse_board_doctor({"outcome_grace_hours": 0})

    def test_outcome_grace_hours_rejects_non_number(self) -> None:
        with pytest.raises(ConfigError, match="outcome_grace_hours"):
            _parse_board_doctor({"outcome_grace_hours": "6"})

    def test_log_retention_days_rejects_non_positive(self) -> None:
        with pytest.raises(ConfigError, match="log_retention_days"):
            _parse_board_doctor({"log_retention_days": 0})

    def test_backoff_initial_hours_rejects_non_positive(self) -> None:
        with pytest.raises(ConfigError, match="backoff_initial_hours"):
            _parse_board_doctor({"backoff_initial_hours": 0})

    def test_backoff_cap_hours_below_initial_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="backoff_cap_hours"):
            _parse_board_doctor({"backoff_initial_hours": 10.0, "backoff_cap_hours": 5.0})

    def test_a_config_setting_only_initial_above_the_default_cap_names_the_default(
        self,
    ) -> None:
        """#3889 review nit: a config that sets ONLY
        ``backoff_initial_hours`` above the untouched ``backoff_cap_hours``
        default must say the cap is the UNTOUCHED side, not just restate
        the invariant and leave an operator guessing which knob to fix."""
        with pytest.raises(ConfigError, match=r"defaults to 24\.0"):
            _parse_board_doctor({"backoff_initial_hours": 48.0})

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

    def test_run_sweep_threads_config_knobs_into_sweep(self, monkeypatch) -> None:
        """#3889 review non-blocking: the above prove the PARAMETERS work —
        nothing proved :func:`coord.board_doctor.run_sweep` actually reads
        them off ``config.board_doctor`` and passes them down. Stub
        ``collect_rows``/``sweep`` (both module-level names `run_sweep`
        resolves fresh on every call, so monkeypatching the module wins)
        and assert the knobs the production entry point owns —
        ``log_retention_days``/``backoff_initial_hours``/
        ``backoff_cap_hours``/#3907's ``outcome_grace_hours`` plus the
        already-threaded ``capacity_check`` — arrive at `sweep` unchanged."""
        import coord.board_doctor as board_doctor_mod
        from coord.config import BoardDoctorConfig, Config

        captured: dict = {}
        sentinel_report = board_doctor_mod.SweepReport(sweep_id="sentinel-report")

        def _fake_sweep(rows, **kwargs):
            captured.update(kwargs)
            captured["rows"] = rows
            return sentinel_report

        monkeypatch.setattr(board_doctor_mod, "collect_rows", lambda *, config: [])
        monkeypatch.setattr(board_doctor_mod, "sweep", _fake_sweep)

        cfg = Config(
            repos=[],
            machines=[],
            board_doctor=BoardDoctorConfig(
                log_retention_days=7,
                backoff_initial_hours=6.0,
                backoff_cap_hours=12.0,
                fixit_leg_budget=2,
                outcome_grace_hours=9.0,
            ),
        )

        result = board_doctor_mod.run_sweep(config=cfg, capacity_check=None)

        # #3945: `run_sweep` now returns the SAME report object `sweep()`
        # built, only stamping `rows_observed` onto it (no merged-partial
        # rows here, so `followthrough` stays empty) — no longer a bare
        # passthrough, hence identity rather than equality to a sentinel
        # string.
        assert result is sentinel_report
        assert result.rows_observed == 0
        assert result.followthrough == []
        assert captured["log_retention_days"] == 7
        assert captured["backoff_initial_hours"] == 6.0
        assert captured["backoff_cap_hours"] == 12.0
        assert captured["outcome_grace_hours"] == 9.0
        assert captured["capacity_check"] is None


class TestNoProgressAgeSeconds:
    """Direct unit coverage of `_no_progress_age_seconds`'s stall-nudge
    branch, including the edge the black-box `no_progress_rows` tests
    cannot reach: a malformed `stalled_for` value.
    """

    def test_unparseable_stalled_for_falls_back_to_launched_at(self) -> None:
        from coord.board_doctor import _no_progress_age_seconds

        entry = QueueEntry(
            repo="acme/widgets", issue=1, state="running", launched_at=100.0,
        )
        age = _no_progress_age_seconds(
            entry, now=500.0, stall_nudge={"stalled_for": "not-a-number"},
        )
        assert age == 400.0

    def test_stall_nudge_with_stalled_for_none_falls_back_to_launched_at(self) -> None:
        from coord.board_doctor import _no_progress_age_seconds

        entry = QueueEntry(
            repo="acme/widgets", issue=1, state="running", launched_at=100.0,
        )
        age = _no_progress_age_seconds(
            entry, now=500.0, stall_nudge={"stalled_for": None},
        )
        assert age == 400.0


class TestFreshStallNudge:
    """Direct unit coverage of `_fresh_stall_nudge`: the #3918 review's
    recency/launch bound on a notifier stall-nudge record, isolated from
    the rest of `no_progress_rows`'s plumbing.
    """

    def test_none_nudge_stays_none(self) -> None:
        from coord.board_doctor import _fresh_stall_nudge

        assert _fresh_stall_nudge(
            None, now=100.0, launched_at=0.0, ttl_seconds=60.0,
        ) is None

    def test_missing_at_is_rejected(self) -> None:
        from coord.board_doctor import _fresh_stall_nudge

        assert _fresh_stall_nudge(
            {"stalled_for": 5.0}, now=100.0, launched_at=0.0, ttl_seconds=60.0,
        ) is None

    def test_unparseable_at_is_rejected(self) -> None:
        from coord.board_doctor import _fresh_stall_nudge

        assert _fresh_stall_nudge(
            {"at": "not-a-number", "stalled_for": 5.0},
            now=100.0, launched_at=0.0, ttl_seconds=60.0,
        ) is None

    def test_at_before_launched_at_is_rejected(self) -> None:
        from coord.board_doctor import _fresh_stall_nudge

        assert _fresh_stall_nudge(
            {"at": 50.0, "stalled_for": 5.0},
            now=100.0, launched_at=60.0, ttl_seconds=60.0,
        ) is None

    def test_at_past_ttl_is_rejected(self) -> None:
        from coord.board_doctor import _fresh_stall_nudge

        assert _fresh_stall_nudge(
            {"at": 0.0, "stalled_for": 5.0},
            now=200.0, launched_at=None, ttl_seconds=60.0,
        ) is None

    def test_fresh_nudge_within_bounds_passes_through(self) -> None:
        from coord.board_doctor import _fresh_stall_nudge

        nudge = {"at": 90.0, "stalled_for": 5.0}
        assert _fresh_stall_nudge(
            nudge, now=100.0, launched_at=60.0, ttl_seconds=60.0,
        ) is nudge

    def test_no_launched_at_known_skips_the_launch_bound(self) -> None:
        from coord.board_doctor import _fresh_stall_nudge

        nudge = {"at": 90.0, "stalled_for": 5.0}
        assert _fresh_stall_nudge(
            nudge, now=100.0, launched_at=None, ttl_seconds=60.0,
        ) is nudge


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

    def test_running_live_but_idle_is_classified(self) -> None:
        # #3918 / quadraui#2160 shape: the tmux session IS alive (so the
        # dead-leg check never matches), but `coord drive`'s own stall
        # nudge reports no state change for well past the no-progress
        # threshold. Must still reach this sweep's output instead of
        # being silently invisible just because a live session exists.
        from coord.notifier.store import record_nudge

        record_nudge("acme/widgets", 2160, at=100_000.0, stalled_for=9_000.0)

        entry = QueueEntry(
            repo="acme/widgets",
            issue=2160,
            state="running",
            session_name="drive-acme-widgets-2160",
            launched_at=100_000.0,
        )
        probe = _FakeProbe({"acme/widgets#2160": LiveState()})

        rows = no_progress_rows(
            [entry],
            covered_keys=frozenset(),
            live_sessions=frozenset({"drive-acme-widgets-2160"}),
            board_view=BoardView(),
            probe=probe,
            config=self._config(),
            now=100_000.0,
        )

        [row] = rows
        assert row.key == entry.key
        assert row.cause == queue_diagnose.CAUSE_UNKNOWN
        assert "running-live-no-state-change" in row.evidence[-1]
        assert "150m" in row.evidence[-1]

    def test_running_live_but_idle_with_active_work_is_not_classified(self) -> None:
        # #3918 review (blocking): the issue's own condition is a `running`
        # entry with "nothing active" — a stall nudge alone must not
        # override a live, non-terminal work-like assignment the board
        # still has recorded for this row. The ordinary case of a live
        # session making real progress carries `active_work=True` for
        # almost its entire run, so this guard is what keeps that case
        # from reading as idle just because a nudge happens to be on file
        # from an earlier, now-resolved stall.
        from coord.notifier.store import record_nudge

        record_nudge("acme/widgets", 2163, at=100_000.0, stalled_for=9_000.0)

        entry = QueueEntry(
            repo="acme/widgets",
            issue=2163,
            state="running",
            session_name="drive-acme-widgets-2163",
            launched_at=100_000.0,
        )
        probe = _FakeProbe({})

        rows = no_progress_rows(
            [entry],
            covered_keys=frozenset(),
            live_sessions=frozenset({"drive-acme-widgets-2163"}),
            board_view=BoardView(
                issues={"acme/widgets#2163": IssueFacts(active_work=True)},
            ),
            probe=probe,
            config=self._config(),
            now=100_000.0,
        )

        assert rows == []

    def test_running_live_but_idle_under_threshold_is_not_classified(self) -> None:
        # A stall nudge that hasn't aged past `no_progress_minutes` yet is
        # not a no-progress row — same grace window every other shape gets.
        from coord.notifier.store import record_nudge

        record_nudge("acme/widgets", 2161, at=100_000.0, stalled_for=60.0)

        entry = QueueEntry(
            repo="acme/widgets",
            issue=2161,
            state="running",
            session_name="drive-acme-widgets-2161",
            launched_at=100_000.0,
        )
        probe = _FakeProbe({})

        rows = no_progress_rows(
            [entry],
            covered_keys=frozenset(),
            live_sessions=frozenset({"drive-acme-widgets-2161"}),
            board_view=BoardView(),
            probe=probe,
            config=self._config(),
            now=100_000.0,
        )

        assert rows == []

    def test_running_live_with_no_stall_nudge_is_not_classified(self) -> None:
        # A live session with no published nudge at all is the ordinary
        # "making progress" case — never flagged from liveness alone.
        entry = QueueEntry(
            repo="acme/widgets",
            issue=2162,
            state="running",
            session_name="drive-acme-widgets-2162",
            launched_at=100_000.0,
        )
        probe = _FakeProbe({})

        rows = no_progress_rows(
            [entry],
            covered_keys=frozenset(),
            live_sessions=frozenset({"drive-acme-widgets-2162"}),
            board_view=BoardView(),
            probe=probe,
            config=self._config(),
            now=100_000.0,
        )

        assert rows == []

    def test_running_live_but_idle_nudge_predating_launch_is_not_classified(self) -> None:
        # #3918 review: a nudge dated BEFORE this session's own
        # `launched_at` describes an earlier (possibly already-ended)
        # session for the same (repo, issue) — it must not be read as
        # evidence about the current one just because the key matches.
        from coord.notifier.store import record_nudge

        record_nudge("acme/widgets", 2164, at=50_000.0, stalled_for=9_000.0)

        entry = QueueEntry(
            repo="acme/widgets",
            issue=2164,
            state="running",
            session_name="drive-acme-widgets-2164",
            launched_at=100_000.0,
        )
        probe = _FakeProbe({})

        rows = no_progress_rows(
            [entry],
            covered_keys=frozenset(),
            live_sessions=frozenset({"drive-acme-widgets-2164"}),
            board_view=BoardView(),
            probe=probe,
            config=self._config(),
            now=100_000.0,
        )

        assert rows == []

    def test_running_live_but_idle_nudge_past_ttl_is_not_classified(self) -> None:
        # #3918 review: `load_state` applies no expiry of its own, so a
        # record that has outlived the notifier's own `NUDGE_TTL_SECS`
        # must not be read as live evidence either, even when it postdates
        # `launched_at`.
        from coord.notifier.store import NUDGE_TTL_SECS, record_nudge

        launched_at = 100_000.0
        nudge_at = launched_at + 10.0
        now = nudge_at + NUDGE_TTL_SECS + 1.0
        record_nudge("acme/widgets", 2165, at=nudge_at, stalled_for=9_000.0)

        entry = QueueEntry(
            repo="acme/widgets",
            issue=2165,
            state="running",
            session_name="drive-acme-widgets-2165",
            launched_at=launched_at,
        )
        probe = _FakeProbe({})

        rows = no_progress_rows(
            [entry],
            covered_keys=frozenset(),
            live_sessions=frozenset({"drive-acme-widgets-2165"}),
            board_view=BoardView(),
            probe=probe,
            config=self._config(),
            now=now,
        )

        assert rows == []

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

    def test_waiting_follows_a_live_prereq_truth_over_a_stale_blocked_dep_row(
        self,
    ) -> None:
        """#3936, vimcode#2140's shape replayed for the no-progress sweep:
        the dep's own drive-queue row still reads `blocked` (a latched
        conclusion — the OLD check here, `dep_states.get(dep) in
        STATE_DONE_LIKE`, would read this dep as unsatisfied forever) and
        the board's own issue cache has not caught up either (`facts.landed`
        is also `False`) — but a LIVE `EntryTruth` supplied for this dep via
        `prereq_truth` says `next_action=mark_done` (the dep's feature
        shipped upstream even though its row never got rewritten). The
        actor must follow the live truth, not either persisted/stale
        reading, and classify the row `waiting-ready-not-launched`."""
        from coord.entry_truth import EntryTruth, NEXT_ACTION_MARK_DONE

        dep = QueueEntry(repo="acme/widgets", issue=2000, state="blocked")
        entry = QueueEntry(
            repo="acme/widgets",
            issue=2047,
            state="waiting",
            after=("acme/widgets#2000",),
            reason_at=0.0,
        )
        probe = _FakeProbe({"acme/widgets#2047": LiveState()})
        dep_truth = EntryTruth(
            repo_name="acme/widgets",
            issue_number=2000,
            next_action=NEXT_ACTION_MARK_DONE,
            next_action_reason="PR merged; issue should close",
        )

        rows = no_progress_rows(
            [dep, entry],
            covered_keys=frozenset(),
            live_sessions=frozenset(),
            board_view=BoardView(),
            probe=probe,
            config=self._config(),
            now=100_000.0,
            prereq_truth={"acme/widgets#2000": dep_truth},
        )

        [row] = [r for r in rows if r.key == entry.key]
        assert "waiting-ready-not-launched" in row.evidence[-1]

    def test_waiting_still_reads_a_stale_blocked_dep_as_unsatisfied_with_no_truth_supplied(
        self,
    ) -> None:
        """Same stale-`blocked` dep row as above, but with no `prereq_truth`
        supplied at all (today's default) — the pre-#3936 board/queue-state
        reasoning must still apply unchanged, so the row is NOT classified."""
        dep = QueueEntry(repo="acme/widgets", issue=2000, state="blocked")
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

        assert entry.key not in {r.key for r in rows}

    def test_waiting_probe_failure_escalate_truth_falls_through_to_board_facts(
        self,
    ) -> None:
        """Review fix (#3936): a dep's live `EntryTruth` reading `escalate`
        with `escalate_probe_failure=True` (a transient `gh` read failure,
        `coord.entry_truth._derive_next_action`) is INCONCLUSIVE, exactly
        like :func:`coord.drive_queue._prereq_truth_verdict` treats it — not
        a confirmed "not landed". The dep's own board facts already read
        `merged`, so the edge must still release."""
        from coord.entry_truth import EntryTruth, NEXT_ACTION_ESCALATE

        dep = QueueEntry(repo="acme/widgets", issue=2000, state="running")
        entry = QueueEntry(
            repo="acme/widgets",
            issue=2047,
            state="waiting",
            after=("acme/widgets#2000",),
            reason_at=0.0,
        )
        probe = _FakeProbe({"acme/widgets#2047": LiveState()})
        dep_truth = EntryTruth(
            repo_name="acme/widgets",
            issue_number=2000,
            next_action=NEXT_ACTION_ESCALATE,
            next_action_reason="could not confirm branch head",
            escalate_probe_failure=True,
        )

        rows = no_progress_rows(
            [dep, entry],
            covered_keys=frozenset(),
            live_sessions=frozenset(),
            board_view=BoardView(issues={"acme/widgets#2000": IssueFacts(merged=True)}),
            probe=probe,
            config=self._config(),
            now=100_000.0,
            prereq_truth={"acme/widgets#2000": dep_truth},
        )

        [row] = [r for r in rows if r.key == entry.key]
        assert "waiting-ready-not-launched" in row.evidence[-1]

    def test_waiting_done_dep_row_outranks_a_non_mark_done_live_truth(self) -> None:
        """Review fix (#3936): a dep whose OWN drive-queue row already
        reads `done` this tick must not be shadowed by a live truth whose
        verdict is anything other than `mark_done` (e.g. `merge`, read off a
        slightly-behind PR state) — the same ordering
        :func:`coord.drive_queue._resolve_prereqs` enforces (#2715 before
        #3934's own truth check)."""
        from coord.drive_queue import STATE_DONE
        from coord.entry_truth import EntryTruth, NEXT_ACTION_MERGE

        dep = QueueEntry(repo="acme/widgets", issue=2000, state=STATE_DONE)
        entry = QueueEntry(
            repo="acme/widgets",
            issue=2047,
            state="waiting",
            after=("acme/widgets#2000",),
            reason_at=0.0,
        )
        probe = _FakeProbe({"acme/widgets#2047": LiveState()})
        dep_truth = EntryTruth(
            repo_name="acme/widgets",
            issue_number=2000,
            next_action=NEXT_ACTION_MERGE,
            next_action_reason="PR is mergeable, not yet merged",
        )

        rows = no_progress_rows(
            [dep, entry],
            covered_keys=frozenset(),
            live_sessions=frozenset(),
            board_view=BoardView(),
            probe=probe,
            config=self._config(),
            now=100_000.0,
            prereq_truth={"acme/widgets#2000": dep_truth},
        )

        [row] = [r for r in rows if r.key == entry.key]
        assert "waiting-ready-not-launched" in row.evidence[-1]

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

    def test_repo_ceiling_honours_systemd_max_parallel_per_repo_override(self) -> None:
        # A `--max-parallel-per-repo` baked into the daemon host's
        # installed systemd unit outranks `coordinator.yml` for the REAL
        # tick (`resolve_max_parallel_per_repo`'s own contract) —
        # `_repo_parallel_ceiling` must read the same override, not only
        # `coordinator.yml`, so a fleet running a lower systemd override
        # never has the doctor compute a HIGHER ceiling than the tick ever
        # enforces.
        from coord.board_doctor import _repo_parallel_ceiling

        config = self._config()
        config.pipeline.max_parallel_per_repo = 5

        # No systemd override known (default posture, e.g. a non-daemon
        # host): `coordinator.yml`'s own value wins, same as #3888.
        assert _repo_parallel_ceiling(
            "acme/widgets", config=config, systemd_flags={}
        ) == 5
        # The daemon host's installed unit hardcodes a lower ceiling: that
        # number must win, exactly like `resolve_concurrency_report` (the
        # SAME resolution the real tick uses) already honours.
        assert _repo_parallel_ceiling(
            "acme/widgets",
            config=config,
            systemd_flags={"max_parallel_per_repo": 2},
        ) == 2

    def test_systemd_flags_read_sees_a_dropin_override(self, tmp_path) -> None:
        # The packaged `coord-drive-queue.service` deliberately omits
        # `--max-parallel-per-repo` from its own `ExecStart=` line — a host
        # that carries that flag at all does so via a
        # `coord-drive-queue.service.d/*.conf` drop-in, systemd's own
        # mechanism for overriding one unit without copying its whole
        # `ExecStart=`. A reader that only opens the main unit file can
        # never see that value.
        from coord.drive_queue import read_systemd_max_parallel_flags

        unit_path = tmp_path / "coord-drive-queue.service"
        unit_path.write_text(
            "[Service]\n"
            "ExecStart=/h/.coord-venv/bin/coord drive-queue tick --config x\n"
        )
        dropin_dir = tmp_path / "coord-drive-queue.service.d"
        dropin_dir.mkdir()
        (dropin_dir / "override.conf").write_text(
            "[Service]\n"
            "ExecStart=\n"
            "ExecStart=/h/.coord-venv/bin/coord drive-queue tick --config x "
            "--max-parallel-per-repo 2\n"
        )

        assert read_systemd_max_parallel_flags(unit_path) == {
            "max_parallel_per_repo": 2
        }

    def test_systemd_flags_read_ignores_dropin_without_main_unit(self, tmp_path) -> None:
        # A drop-in with no unit to override means nothing to systemd
        # either — this must read as "no override known" (`{}`), the same
        # as an absent unit today, never crash or invent a flag.
        from coord.drive_queue import read_systemd_max_parallel_flags

        unit_path = tmp_path / "coord-drive-queue.service"
        dropin_dir = tmp_path / "coord-drive-queue.service.d"
        dropin_dir.mkdir()
        (dropin_dir / "override.conf").write_text(
            "[Service]\nExecStart=/x --max-parallel-per-repo 2\n"
        )

        assert read_systemd_max_parallel_flags(unit_path) == {}

    def _systemd_override_fixture(self):
        """The shared N(=ceiling) `running` + aged-out `waiting` fixture
        both systemd-override tests below replay — one `running` row
        launched on a FOREIGN host, plus a `waiting` row whose `after=`
        deps have all landed. Returns ``(config, entries, board_view,
        live_sessions, probe)`` so each test only has to vary the
        ``systemd_flags`` it passes into :func:`no_progress_rows`.
        """
        config = self._config()
        config.pipeline.max_parallel_per_repo = 5
        # `ConcurrencyConfig.max_workers` defaults to 2 — raised here so
        # the FLEET-wide gate this fixture is not exercising never binds
        # first and masks the repo-ceiling behaviour both tests below are
        # actually checking.
        config.concurrency.max_workers = 10

        running_local = QueueEntry(
            repo="acme/widgets",
            issue=2137,
            state="running",
            session_name="drive-acme-widgets-2137",
            launched_at=0.0,
        )
        running_foreign = QueueEntry(
            repo="acme/widgets",
            issue=2138,
            state="running",
            launch_host="otherhost",
            launched_at=0.0,
        )
        waiting = QueueEntry(
            repo="acme/widgets",
            issue=2139,
            state="waiting",
            after=("acme/widgets#2000",),
            reason_at=0.0,
        )
        board_view = BoardView(
            issues={"acme/widgets#2000": IssueFacts(issue_state="closed")},
            # `BoardView.live_sessions` keys on `repo#issue`, the SAME
            # occupancy verdict `_reconcile_running` reads — distinct from
            # the top-level `live_sessions` collection below (local tmux
            # session NAMES, used only by the `running` no-progress
            # shape's own liveness check).
            live_sessions=frozenset({running_local.key}),
        )
        return (
            config,
            [running_local, running_foreign, waiting],
            board_view,
            frozenset({"drive-acme-widgets-2137"}),
            _FakeProbe({}),
        )

    def test_waiting_row_deferred_by_systemd_override_is_not_classified(self) -> None:
        # Regression: the repo's effective ceiling is the daemon host's
        # systemd-unit override (2), lower than `coordinator.yml`'s
        # `pipeline.max_parallel_per_repo` (5) — the exact
        # 2026-11-07T18:26Z quadraui#2137-et-al. shape: the tick's own
        # capacity read (2/2, deferring) must be the SAME number the
        # doctor reads, so this produces ZERO no-progress rows and ZERO
        # escalations for the repo, never `waiting-ready-not-launched`.
        config, entries, board_view, live_sessions, probe = self._systemd_override_fixture()

        rows = no_progress_rows(
            entries,
            covered_keys=frozenset(),
            live_sessions=live_sessions,
            board_view=board_view,
            probe=probe,
            config=config,
            now=100_000.0,
            local_host="thishost",
            systemd_flags={"max_parallel_per_repo": 2},
        )

        assert rows == []
        assert probe.calls == []

    def test_waiting_row_without_override_still_classifies_at_ceiling_five(self) -> None:
        # Companion to the regression above: the IDENTICAL fixture (2
        # `running` rows, ceiling-eligible `waiting` row) with NO systemd
        # override known. `coordinator.yml`'s own ceiling (5) then has
        # room for the repo's 2 occupied slots, so the row IS classified —
        # proving the override test above passes BECAUSE of the override,
        # not because this fixture was never classifiable at all.
        config, entries, board_view, live_sessions, probe = self._systemd_override_fixture()

        rows = no_progress_rows(
            entries,
            covered_keys=frozenset(),
            live_sessions=live_sessions,
            board_view=board_view,
            probe=probe,
            config=config,
            now=100_000.0,
            local_host="thishost",
            systemd_flags={},
        )

        [row] = rows
        assert row.key == "acme/widgets#2139"
        assert "waiting-ready-not-launched" in row.evidence[-1]

    def test_waiting_row_deferred_by_fleet_capacity_is_not_classified(self) -> None:
        # A `waiting` row with room under its OWN repo's ceiling must
        # still not be classified no-progress while the FLEET-wide
        # `--max-parallel` ceiling is full — the identical "queue working
        # as intended" shape as the repo-ceiling case above, one level up.
        # `plan_tick`'s own global early return only refreshes a `waiting`
        # row's prereq text (`counted=False`) and never writes a capacity
        # reason, so a row deferred only at this level reads a
        # `last_reason` that names no capacity at all.
        config = Config(
            repos=[
                Repo(name="acme/widgets", github="acme/widgets", max_parallel=3),
                Repo(name="acme/other", github="acme/other", max_parallel=3),
            ],
            machines=[],
        )
        config.pipeline.max_parallel = 2

        running_a = QueueEntry(
            repo="acme/other", issue=3001, state="running",
            session_name="drive-acme-other-3001", launched_at=0.0,
        )
        running_b = QueueEntry(
            repo="acme/other", issue=3002, state="running",
            session_name="drive-acme-other-3002", launched_at=0.0,
        )
        waiting = QueueEntry(
            repo="acme/widgets",
            issue=3003,
            state="waiting",
            after=("acme/widgets#2000",),
            reason_at=0.0,
        )
        probe = _FakeProbe({})

        rows = no_progress_rows(
            [running_a, running_b, waiting],
            covered_keys=frozenset(),
            live_sessions=frozenset(
                {"drive-acme-other-3001", "drive-acme-other-3002"}
            ),
            board_view=BoardView(
                issues={"acme/widgets#2000": IssueFacts(issue_state="closed")},
                live_sessions=frozenset({running_a.key, running_b.key}),
            ),
            probe=probe,
            config=config,
            now=100_000.0,
            local_host="thishost",
        )

        assert rows == []
        assert probe.calls == []


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


class TestStrandedPrereqDep:
    """#3944: :func:`stranded_prereq_dep` is the one place outside
    `coord.drive_queue` that knows `_resolve_prereqs`'s "open, not queued"
    verdict text."""

    def test_matches_the_exact_verdict_text(self) -> None:
        assert (
            stranded_prereq_dep("waiting on quadraui#2172 (open, not queued)")
            == "quadraui#2172"
        )

    def test_none_for_other_waiting_reasons(self) -> None:
        assert stranded_prereq_dep("waiting on quadraui#2172 (queued, waiting)") is None
        assert stranded_prereq_dep("waiting on quadraui#2172 (work in flight, not queued)") is None
        assert stranded_prereq_dep("") is None
        assert stranded_prereq_dep(None) is None

    def test_none_for_trailing_garbage(self) -> None:
        # The real verdict never has anything after the closing paren; a
        # reason that merely CONTAINS the marker must not match.
        assert stranded_prereq_dep("waiting on quadraui#2172 (open, not queued) — retrying") is None


class TestStrandedPrereqGuard:
    """#3944's "Must NOT auto-queue" list: epic, operator opt-out label,
    operator-removed history, and an unresolvable repo must all refuse —
    and fail CLOSED (refuse) on a `gh` read it could not complete."""

    @staticmethod
    def _config() -> Config:
        return Config(
            repos=[Repo(name="quadraui", github="acme/quadraui")], machines=[],
        )

    def test_open_plain_issue_may_be_queued(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "coord.github_ops.get_issue", lambda *a, **k: {"labels": []},
        )
        guard = stranded_prereq_guard("quadraui#2172", self._config())
        assert guard.may_queue is True

    def test_unparseable_key_is_refused(self) -> None:
        guard = stranded_prereq_guard("not-a-key", self._config())
        assert guard.may_queue is False

    def test_unknown_repo_is_refused(self) -> None:
        guard = stranded_prereq_guard("vimcode#50", self._config())
        assert guard.may_queue is False
        assert "coordinator.yml" in guard.reason

    def test_epic_is_refused(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "coord.github_ops.get_issue",
            lambda *a, **k: {"labels": [{"name": "epic"}]},
        )
        guard = stranded_prereq_guard("quadraui#2283", self._config())
        assert guard.may_queue is False
        assert "epic" in guard.reason

    def test_opt_out_label_is_refused(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "coord.github_ops.get_issue",
            lambda *a, **k: {"labels": [{"name": STRANDED_PREREQ_OPT_OUT_LABEL}]},
        )
        guard = stranded_prereq_guard("quadraui#2283", self._config())
        assert guard.may_queue is False
        assert STRANDED_PREREQ_OPT_OUT_LABEL in guard.reason

    def test_operator_removed_is_refused_without_even_reading_github(
        self, monkeypatch
    ) -> None:
        # The real production write: `coord drive-queue remove` (CLI's own
        # `operator=True` default) against a `waiting` row — the exact
        # shape `block_log`'s `STALL_STATES` gate could never record, since
        # `waiting` isn't `blocked`/`parked`. Reads back through the shared
        # `audit_log`, not the per-host `block_log`.
        def _boom(*a, **k):
            raise AssertionError("must not read GitHub once operator-removed is confirmed")

        monkeypatch.setattr("coord.github_ops.get_issue", _boom)
        state.enqueue_drive_queue("quadraui", 2172)
        state.dequeue_drive_queue("quadraui", 2172)

        guard = stranded_prereq_guard("quadraui#2172", self._config())
        assert guard.may_queue is False
        assert "operator" in guard.reason

    def test_a_later_automated_removal_does_not_inherit_the_operator_flag(
        self, monkeypatch
    ) -> None:
        # The MOST RECENT removal decides: an operator's `remove`, followed
        # by the row getting re-added and later removed again by an
        # AUTOMATED path (`operator=False` — e.g. `board_doctor_
        # permissions`'s own gated dequeue, or `upstream_gaps`'s remove-
        # then-re-add), must not still read as a standing human veto.
        state.enqueue_drive_queue("quadraui", 2172)
        state.dequeue_drive_queue("quadraui", 2172)
        state.enqueue_drive_queue("quadraui", 2172)
        state.dequeue_drive_queue("quadraui", 2172, operator=False)

        monkeypatch.setattr("coord.github_ops.get_issue", lambda *a, **k: {"labels": []})
        guard = stranded_prereq_guard("quadraui#2172", self._config())
        assert guard.may_queue is True

    def test_no_removal_history_is_not_treated_as_removed(self) -> None:
        # Absence of a record is not evidence of a human decision — a key
        # that was simply never queued, or queued and left alone, must not
        # refuse on this check alone.
        from coord.board_doctor import _dep_last_operator_removed

        assert _dep_last_operator_removed("quadraui", 2172) is False

    def test_gh_read_failure_is_refused_not_assumed_safe(self, monkeypatch) -> None:
        def _boom(*a, **k):
            raise RuntimeError("gh: rate limited")

        monkeypatch.setattr("coord.github_ops.get_issue", _boom)
        guard = stranded_prereq_guard("quadraui#2172", self._config())
        assert guard.may_queue is False
        assert "could not read" in guard.reason

    def test_empty_label_read_is_refused_not_assumed_safe(self, monkeypatch) -> None:
        # `github_ops.get_issue` fails OPEN to `{}` on an exit-0 `gh` that
        # emitted empty/malformed stdout — distinct from the raising case
        # above, and from a genuinely label-less issue (`{"labels": []}`,
        # which `test_open_plain_issue_may_be_queued` already covers as
        # auto-queueable). Both must refuse, never fall through to "no
        # labels, so no epic/opt-out" by default.
        monkeypatch.setattr("coord.github_ops.get_issue", lambda *a, **k: {})
        guard = stranded_prereq_guard("quadraui#2172", self._config())
        assert guard.may_queue is False
        assert "could not read" in guard.reason


class TestStrandedPrereqRows:
    """#3944: the classifier that walks every current `waiting` entry and
    produces :data:`CAUSE_STRANDED_PREREQ` / :data:`CAUSE_STRANDED_PREREQ_
    BLOCKED` rows — the shape neither the episode walk nor
    :func:`no_progress_rows` ever sees (quadraui#2174/#2178, 960+
    deferrals)."""

    @staticmethod
    def _config() -> Config:
        return Config(
            repos=[Repo(name="quadraui", github="acme/quadraui")], machines=[],
        )

    def test_a_stranded_waiting_row_is_flagged_auto_queueable(self, monkeypatch) -> None:
        monkeypatch.setattr("coord.github_ops.get_issue", lambda *a, **k: {"labels": []})
        entry = QueueEntry(
            repo="quadraui", issue=2174, state="waiting",
            after=("quadraui#2172",),
            last_reason="waiting on quadraui#2172 (open, not queued)",
            deferrals=960,
        )

        [row] = stranded_prereq_rows([entry], config=self._config())

        assert row.key == "quadraui#2174"
        assert row.cause == CAUSE_STRANDED_PREREQ
        assert "quadraui#2172" in row.evidence[0]
        assert row.fresh is True

    def test_an_epic_dep_is_flagged_blocked_not_auto_queueable(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "coord.github_ops.get_issue",
            lambda *a, **k: {"labels": [{"name": "epic"}]},
        )
        entry = QueueEntry(
            repo="vimcode", issue=2141, state="waiting",
            after=("quadraui#2283",),
            last_reason="waiting on quadraui#2283 (open, not queued)",
        )

        [row] = stranded_prereq_rows(
            [entry],
            config=Config(
                repos=[
                    Repo(name="quadraui", github="acme/quadraui"),
                    Repo(name="vimcode", github="acme/vimcode"),
                ],
                machines=[],
            ),
        )

        assert row.key == "vimcode#2141"
        assert row.cause == CAUSE_STRANDED_PREREQ_BLOCKED
        assert "epic" in row.evidence[0]

    def test_a_non_stranded_waiting_row_is_not_flagged(self) -> None:
        entry = QueueEntry(
            repo="quadraui", issue=1, state="waiting",
            after=("quadraui#2",),
            last_reason="waiting on quadraui#2 (queued, waiting)",
        )
        assert stranded_prereq_rows([entry], config=self._config()) == []

    def test_a_non_waiting_row_is_never_flagged_even_with_matching_text(self) -> None:
        # Defensive: the shape is only ever meaningful for `waiting` —
        # `_resolve_prereqs` never writes this text for any other state.
        entry = QueueEntry(
            repo="quadraui", issue=2174, state="blocked",
            after=("quadraui#2172",),
            last_reason="waiting on quadraui#2172 (open, not queued)",
        )
        assert stranded_prereq_rows([entry], config=self._config()) == []

    def test_declined_cause_routes_to_the_alert_tier(self) -> None:
        from coord.board_doctor_policy import policy_tier_of

        assert policy_tier_of(CAUSE_STRANDED_PREREQ_BLOCKED) == TIER_ALERT


class TestQueueStrandedPrereqDispatcher:
    """#3944: end to end through the REAL `coord.board_doctor_policy`
    dispatcher (not a fake) — settle window, idempotency, and same-repo vs.
    cross-repo placement, the load-bearing evidence this cause is actually
    wired up rather than a tautology.
    """

    @staticmethod
    def _config() -> Config:
        return Config(
            repos=[
                Repo(name="quadraui", github="acme/quadraui"),
                Repo(name="vimcode", github="acme/vimcode"),
            ],
            machines=[],
        )

    @pytest.fixture(autouse=True)
    def _wire_seams(self, monkeypatch):
        """Every test in this class hits the same two external seams: the
        epic/opt-out label read (`coord.github_ops.get_issue`) and the
        audit comment (`coord.github_ops.post_issue_comment`) — stubbed
        here so no test needs to re-declare them, and so a comment attempt
        is recorded (``self.comments``) instead of reaching the real `gh`
        seam `_wire_config` cannot stub (#1484)."""
        self.comments: list[tuple[str, int, str]] = []
        monkeypatch.setattr("coord.github_ops.get_issue", lambda *a, **k: {"labels": []})
        monkeypatch.setattr(
            "coord.github_ops.post_issue_comment",
            lambda repo, issue, body, **k: self.comments.append((repo, issue, body)),
        )
        self._monkeypatch = monkeypatch

    def _wire_config(self, config) -> None:
        import coord.commands._common as common

        self._monkeypatch.setattr(common, "resolve_and_load_config", lambda *a, **k: config)

    def _sweep(self, config, *, sweep_id: str):
        from coord.board_doctor_policy import AUTO_DISPATCHERS, policy_tier_of

        rows = stranded_prereq_rows(
            [QueueEntry.from_row(r) for r in state.list_drive_queue()], config=config,
        )
        return sweep(
            rows,
            tier_of=policy_tier_of,
            auto_dispatchers=AUTO_DISPATCHERS,
            alert_dispatcher=_Recorder(),
            sweep_id=sweep_id,
        )

    def test_not_enqueued_before_the_settle_window(self) -> None:
        config = self._config()
        self._wire_config(config)

        state.enqueue_drive_queue("quadraui", 2174, after=["quadraui#2172"])
        state.update_drive_queue_entry(
            "quadraui", 2174, state="waiting",
            last_reason="waiting on quadraui#2172 (open, not queued)",
        )

        report = self._sweep(config, sweep_id="sweep-1")

        [decision] = [d for d in report.decisions if d.key == "quadraui#2174"]
        assert decision.cause == CAUSE_STRANDED_PREREQ
        assert decision.action == "auto-dispatch-noop"
        assert state.get_drive_queue_entry("quadraui", 2172) is None
        assert self.comments == []

    def test_second_sweep_enqueues_at_the_dependents_position(self) -> None:
        config = self._config()
        self._wire_config(config)

        # Another row ahead of the dependent, so the dependent's own
        # position is NOT 0 — the dep must land exactly there, pushing the
        # dependent back by one, not at the front of the whole repo queue.
        state.enqueue_drive_queue("quadraui", 999)
        state.enqueue_drive_queue("quadraui", 2174, after=["quadraui#2172"])
        state.update_drive_queue_entry(
            "quadraui", 2174, state="waiting",
            last_reason="waiting on quadraui#2172 (open, not queued)",
        )
        dependent_position_before = state.get_drive_queue_entry("quadraui", 2174)["position"]

        self._sweep(config, sweep_id="sweep-1")
        report = self._sweep(config, sweep_id="sweep-2")

        [decision] = [d for d in report.decisions if d.key == "quadraui#2174"]
        assert decision.action == "auto-dispatch"

        dep_row = state.get_drive_queue_entry("quadraui", 2172)
        assert dep_row is not None
        assert dep_row["state"] == "waiting"
        assert dep_row["machine"] in ("", None)
        assert dep_row["position"] == dependent_position_before

        # `coord drive-queue list`'s own order is by position — the dep is
        # now ahead of the dependent it was stranding.
        ordered = [r["issue_number"] for r in state.list_drive_queue("quadraui")]
        assert ordered.index(2172) < ordered.index(2174)

        # The audit comment on the dependent names both issues (#3944).
        [(repo, issue, body)] = self.comments
        assert (repo, issue) == ("acme/quadraui", 2174)
        assert "quadraui#2172" in body and "quadraui#2174" in body

    def test_third_sweep_is_idempotent(self) -> None:
        config = self._config()
        self._wire_config(config)

        state.enqueue_drive_queue(
            "quadraui", 2174, after=["quadraui#2172"], machine="laptop",
        )
        state.update_drive_queue_entry(
            "quadraui", 2174, state="waiting",
            last_reason="waiting on quadraui#2172 (open, not queued)",
        )

        self._sweep(config, sweep_id="sweep-1")
        self._sweep(config, sweep_id="sweep-2")
        dep_after_first_enqueue = state.get_drive_queue_entry("quadraui", 2172)
        dependent_after_first_enqueue = state.get_drive_queue_entry("quadraui", 2174)

        # A third sweep still classifies the dependent as stranded (its
        # OWN `after=` text hasn't changed — nothing in this harness runs
        # the real tick that would flip it) — the dep is already queued,
        # so `queue_prereq` must report "left as-is", never resetting its
        # machine/after/position, and the dependent's own declared fields
        # (machine="laptop") must never be rewritten either.
        self._sweep(config, sweep_id="sweep-3")

        assert state.get_drive_queue_entry("quadraui", 2172) == dep_after_first_enqueue
        dependent_now = state.get_drive_queue_entry("quadraui", 2174)
        assert dependent_now["machine"] == "laptop"
        assert dependent_now["after_json"] == dependent_after_first_enqueue["after_json"]
        # Only the ONE enqueue (sweep-2) ever wrote a comment.
        assert len(self.comments) == 1

    def test_cross_repo_dep_lands_at_the_front_of_its_own_repo_queue(self) -> None:
        config = self._config()
        self._wire_config(config)

        # An existing backlog in the pre-req's repo — the pre-req must
        # still jump to the front, not sink behind it (same rule
        # `_queue_upstream` already applies to a blocker).
        state.enqueue_drive_queue("quadraui", 601)
        state.enqueue_drive_queue("quadraui", 602)
        state.enqueue_drive_queue("vimcode", 2141, after=["quadraui#2172"])
        state.update_drive_queue_entry(
            "vimcode", 2141, state="waiting",
            last_reason="waiting on quadraui#2172 (open, not queued)",
        )

        self._sweep(config, sweep_id="sweep-1")
        self._sweep(config, sweep_id="sweep-2")

        ordered = [r["issue_number"] for r in state.list_drive_queue("quadraui")]
        assert ordered == [2172, 601, 602]

    def test_an_epic_dep_is_never_enqueued_even_past_the_settle_window(self) -> None:
        config = self._config()
        self._wire_config(config)
        self._monkeypatch.setattr(
            "coord.github_ops.get_issue",
            lambda *a, **k: {"labels": [{"name": "epic"}]},
        )

        state.enqueue_drive_queue("vimcode", 2141, after=["quadraui#2283"])
        state.update_drive_queue_entry(
            "vimcode", 2141, state="waiting",
            last_reason="waiting on quadraui#2283 (open, not queued)",
        )

        self._sweep(config, sweep_id="sweep-1")
        report = self._sweep(config, sweep_id="sweep-2")

        assert state.get_drive_queue_entry("quadraui", 2283) is None
        [decision] = [d for d in report.decisions if d.key == "vimcode#2141"]
        assert decision.cause == CAUSE_STRANDED_PREREQ_BLOCKED
        assert decision.tier == TIER_ALERT
        assert self.comments == []


# ── Merged-partial follow-through wiring (#3945) ────────────────────────────


class TestMergedPartialRows:
    """:func:`merged_partial_rows` — the `StuckRow` source that feeds
    :func:`run_followthrough_sweep`."""

    def test_a_merged_partial_entry_is_flagged(self) -> None:
        entry = QueueEntry(
            repo="quadraui", issue=2180, state=STATE_MERGED_PARTIAL,
            last_reason="merged, ISSUE_RESOLUTION: partial",
        )
        [row] = merged_partial_rows([entry])
        assert row.key == "quadraui#2180"
        assert row.queue == "drive-queue"
        assert row.cause == CAUSE_MERGED_PARTIAL
        assert row.fresh is True
        assert "last_reason=merged, ISSUE_RESOLUTION: partial" in row.evidence

    def test_a_done_or_waiting_entry_is_not_flagged(self) -> None:
        done = QueueEntry(repo="quadraui", issue=1, state="done")
        waiting = QueueEntry(repo="quadraui", issue=2, state=STATE_WAITING)
        assert merged_partial_rows([done, waiting]) == []

    def test_collect_rows_feeds_merged_partial_rows(self, monkeypatch) -> None:
        """#3945: a `merged-partial` row carries no `block_log` episode and
        no merge-queue entry, so without this wiring it is invisible to
        :func:`coord.board_doctor.collect_rows` entirely."""
        import coord.board_doctor as board_doctor_mod

        entry = QueueEntry(repo="quadraui", issue=2180, state=STATE_MERGED_PARTIAL)
        monkeypatch.setattr(board_doctor_mod, "merge_queue_rows", lambda *a, **k: [])
        monkeypatch.setattr(board_doctor_mod, "merged_partial_rows", lambda entries: (
            merged_partial_rows(entries)
        ))
        monkeypatch.setattr(board_doctor_mod, "no_progress_rows", lambda *a, **k: [])
        monkeypatch.setattr(board_doctor_mod, "smoke_no_verdict_rows", lambda *a, **k: [])
        monkeypatch.setattr(board_doctor_mod, "stranded_prereq_rows", lambda *a, **k: [])
        monkeypatch.setattr(board_doctor_mod.queue_diagnose, "run_pass", lambda *a, **k: [])
        monkeypatch.setattr(
            board_doctor_mod, "_refresh_ceiling_blocked_settled_rows",
            lambda *a, **k: ([], []),
        )
        monkeypatch.setattr("coord.block_log.episodes", lambda *a, **k: [])
        monkeypatch.setattr("coord.block_log.read_events", lambda *a, **k: [])
        monkeypatch.setattr(
            "coord.commands.drive_queue._fetch_board_view_with_retry",
            lambda: (_ for _ in ()).throw(RuntimeError("no board view in this test")),
        )
        monkeypatch.setattr("coord.commands.drive_queue._local_host_id", lambda: "host")
        monkeypatch.setattr("coord.drive.list_drive_sessions", lambda: [])
        monkeypatch.setattr(
            "coord.health.aggregate.local_fleet_health_block", lambda *a, **k: {},
        )
        monkeypatch.setattr("coord.merge_queue.load_queue", lambda: [])
        monkeypatch.setattr("coord.state.baseline_red_merge_blocked", lambda *a, **k: False)
        monkeypatch.setattr("coord.state.baseline_red_streaks", lambda: {})
        monkeypatch.setattr("coord.state.build_board", lambda: Board())
        monkeypatch.setattr("coord.state.list_drive_queue", lambda: [entry.__dict__])
        monkeypatch.setattr(
            "coord.drive_queue.entries_from_rows", lambda rows: [entry],
        )
        monkeypatch.setattr(
            "coord.drive_queue.read_systemd_max_parallel_flags", lambda: {},
        )

        config = Config(repos=[Repo(name="quadraui", github="acme/quadraui")], machines=[])
        rows = board_doctor_mod.collect_rows(config=config)

        assert any(r.key == "quadraui#2180" and r.cause == CAUSE_MERGED_PARTIAL for r in rows)


class TestBuildPartialMerge:
    """:func:`build_partial_merge` — the real (best-effort)
    :class:`~coord.board_doctor_followthrough.PartialMerge` builder."""

    def test_pulls_issue_epic_and_resolution_text(self, monkeypatch) -> None:
        import coord.board_doctor as board_doctor_mod

        def fake_get_issue(repo, number):
            if number == 101:
                return {
                    "number": 101, "title": "Do A and B", "body": "Do A. Do B.",
                    "milestone": {"title": "M1"},
                }
            if number == 50:
                return {"number": 50, "title": "Epic", "body": "## Work order\n- #101\n"}
            raise AssertionError(f"unexpected issue number {number}")

        monkeypatch.setattr("coord.github_ops.get_issue", fake_get_issue)
        monkeypatch.setattr(
            "coord.github_ops.get_issue_parent", lambda repo, number: {"number": 50},
        )
        monkeypatch.setattr(
            "coord.github_ops.get_repo_file", lambda repo, path, branch="develop": "rules",
        )

        board = Board(completed=[
            Assignment(
                machine_name="m", repo_name="quadraui", issue_number=101,
                issue_title="Do A and B", type="work", status="done",
                completion_summary="ISSUE_RESOLUTION: partial — B remains",
            ),
        ])

        partial = board_doctor_mod.build_partial_merge(
            "quadraui", 101, config=Config(repos=[], machines=[]), board=board,
        )

        assert partial.repo_name == "quadraui" and partial.issue_number == 101
        assert partial.title == "Do A and B"
        assert partial.epic_number == 50
        assert "## Work order" in partial.epic_body
        assert partial.resolution_text == "ISSUE_RESOLUTION: partial — B remains"
        assert partial.milestone == "M1"
        assert partial.claude_md == "rules"

    def test_degrades_gracefully_without_epic_or_claude_md(self, monkeypatch) -> None:
        import coord.board_doctor as board_doctor_mod

        monkeypatch.setattr(
            "coord.github_ops.get_issue",
            lambda repo, number: {"number": number, "title": "T", "body": "B"},
        )
        monkeypatch.setattr(
            "coord.github_ops.get_issue_parent", lambda repo, number: None,
        )
        monkeypatch.setattr(
            "coord.github_ops.get_repo_file",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no CLAUDE.md")),
        )

        partial = board_doctor_mod.build_partial_merge(
            "quadraui", 42, config=Config(repos=[], machines=[]), board=Board(),
        )

        assert partial.epic_number is None and partial.epic_body == ""
        assert partial.claude_md == "" and partial.resolution_text == ""


class _FakeFollowThroughWriter:
    def __init__(self) -> None:
        self.closed: list[int] = []

    def create_issue(self, repo_name, *, title, body, milestone):
        return 999

    def add_child(self, repo_name, *, epic, child, after):
        pass

    def get_issue_body(self, repo_name, number):
        return ""

    def set_issue_body(self, repo_name, number, body):
        pass

    def close_issue(self, repo_name, number, *, comment):
        self.closed.append(number)


class TestRunFollowthroughSweep:
    """:func:`run_followthrough_sweep` — the production wiring between a
    `merged-partial` :class:`StuckRow` and
    :func:`coord.board_doctor_followthrough.run_followthrough`."""

    @staticmethod
    def _config(*, opted_in: bool = True) -> Config:
        repo = Repo(
            name="quadraui", github="acme/quadraui",
            board_doctor=BoardDoctorRepoConfig(followthrough=opted_in),
        )
        return Config(repos=[repo], machines=[])

    @staticmethod
    def _fake_plan_leg(plan: dict):
        import json as _json

        def leg(prompt, *, partial, config):
            return ft.PlanLegResult(
                assignment_id="plan-1",
                output="```json\n" + _json.dumps(plan) + "\n```",
            )
        return leg

    ALL_LANDED_PLAN = {
        "items": [{"item": "A", "verified_still_missing": False, "evidence": "a.py:1"}],
        "follow_ups": [],
    }

    def _row(self, issue: int = 2180) -> StuckRow:
        return StuckRow(
            queue="drive-queue", key=f"quadraui#{issue}", state=STATE_MERGED_PARTIAL,
            cause=CAUSE_MERGED_PARTIAL, confidence="high",
        )

    def _stub_build_partial_merge(self, monkeypatch, *, epic_body: str = "") -> None:
        monkeypatch.setattr(
            "coord.board_doctor.build_partial_merge",
            lambda repo_name, issue_number, *, config, board: ft.PartialMerge(
                repo_name=repo_name, issue_number=issue_number, title="T", body="B",
                epic_body=epic_body,
            ),
        )

    def test_eligible_row_runs_followthrough_and_closes(self, monkeypatch) -> None:
        self._stub_build_partial_merge(monkeypatch)
        monkeypatch.setattr("coord.state.build_board", lambda: Board())
        writer = _FakeFollowThroughWriter()

        dispositions = run_followthrough_sweep(
            [self._row()],
            config=self._config(),
            plan_leg_dispatcher=self._fake_plan_leg(self.ALL_LANDED_PLAN),
            writer=writer,
            issue_open=lambda repo, number: True,
        )

        assert len(dispositions) == 1
        assert dispositions[0].action == ft.ACTION_ALL_LANDED
        assert writer.closed == [2180]

    def test_dry_run_writes_nothing(self, monkeypatch) -> None:
        self._stub_build_partial_merge(monkeypatch)
        monkeypatch.setattr("coord.state.build_board", lambda: Board())
        writer = _FakeFollowThroughWriter()

        dispositions = run_followthrough_sweep(
            [self._row()],
            config=self._config(),
            dry_run=True,
            plan_leg_dispatcher=self._fake_plan_leg(self.ALL_LANDED_PLAN),
            writer=writer,
            issue_open=lambda repo, number: True,
        )

        assert len(dispositions) == 1
        assert dispositions[0].dry_run is True
        assert writer.closed == []

    def test_a_row_not_cause_merged_partial_is_skipped(self, monkeypatch) -> None:
        other_row = StuckRow(
            queue="drive-queue", key="quadraui#9", state="blocked",
            cause="merge-conflict", confidence="high",
        )
        dispositions = run_followthrough_sweep(
            [other_row], config=self._config(), issue_open=lambda r, n: True,
        )
        assert dispositions == []

    def test_a_suppressed_row_is_skipped(self, monkeypatch) -> None:
        row = replace(self._row(), suppressed=True)
        dispositions = run_followthrough_sweep(
            [row], config=self._config(), issue_open=lambda r, n: True,
        )
        assert dispositions == []

    def test_a_repo_without_opt_in_is_skipped(self, monkeypatch) -> None:
        dispositions = run_followthrough_sweep(
            [self._row()], config=self._config(opted_in=False),
            issue_open=lambda r, n: True,
        )
        assert dispositions == []

    def test_an_already_closed_issue_is_skipped(self, monkeypatch) -> None:
        dispositions = run_followthrough_sweep(
            [self._row()], config=self._config(), issue_open=lambda r, n: False,
        )
        assert dispositions == []

    def test_a_flaky_open_check_does_not_sink_the_sweep(self, monkeypatch) -> None:
        def boom(repo, number):
            raise RuntimeError("gh timeout")

        dispositions = run_followthrough_sweep(
            [self._row()], config=self._config(), issue_open=boom,
        )
        assert dispositions == []

    def test_max_rows_bounds_dispatches_per_sweep(self, monkeypatch) -> None:
        """#3945 review: without a ceiling, N eligible rows in one sweep
        means N serialized 20-minute plan legs. A row past *max_rows* is
        simply left for the next sweep — no disposition, no alert."""
        self._stub_build_partial_merge(monkeypatch)
        monkeypatch.setattr("coord.state.build_board", lambda: Board())
        writer = _FakeFollowThroughWriter()
        rows = [self._row(2180), self._row(2181), self._row(2182)]

        dispositions = run_followthrough_sweep(
            rows, config=self._config(),
            plan_leg_dispatcher=self._fake_plan_leg(self.ALL_LANDED_PLAN),
            writer=writer, issue_open=lambda repo, number: True,
            max_rows=2,
        )

        assert len(dispositions) == 2
        assert {d.issue_number for d in dispositions} == {2180, 2181}

    # ── #3945 review: the #3864 acceptance cases through the WIRED path,
    # exercising `run_followthrough_sweep`'s own `cap`/`backends`/
    # `epic_exists`/`milestone_exists` arguments rather than only
    # `validate_plan`/`run_followthrough` directly — this is exactly where
    # the `epic_exists` arity bug hid (a plan setting `fu.epic` never ran
    # through this function before).

    def test_over_cap_plan_rejects_through_the_wired_path(self, monkeypatch) -> None:
        self._stub_build_partial_merge(monkeypatch)
        monkeypatch.setattr("coord.state.build_board", lambda: Board())
        writer = _FakeFollowThroughWriter()
        config = replace(
            self._config(), board_doctor=BoardDoctorConfig(followthrough_cap=1),
        )
        plan = {
            "items": [{"item": "B", "verified_still_missing": True}],
            "follow_ups": [
                {"key": "a", "title": "t1", "body": ""},
                {"key": "b", "title": "t2", "body": ""},
            ],
        }

        dispositions = run_followthrough_sweep(
            [self._row()], config=config,
            plan_leg_dispatcher=self._fake_plan_leg(plan),
            writer=writer, issue_open=lambda repo, number: True,
        )

        assert len(dispositions) == 1
        assert dispositions[0].action == ft.ACTION_REJECTED
        assert any("over the per-issue cap of 1" in r for r in dispositions[0].reasons)
        assert writer.closed == []

    def test_cross_backend_plan_rejects_through_the_wired_path(self, monkeypatch) -> None:
        self._stub_build_partial_merge(monkeypatch)
        monkeypatch.setattr("coord.state.build_board", lambda: Board())
        writer = _FakeFollowThroughWriter()
        config = replace(
            self._config(),
            preflight_guard=PreflightGuardConfig(
                repos={
                    "quadraui": PreflightGuardRepoConfig(backends=("gtk", "tui", "cocoa")),
                },
            ),
        )
        plan = {
            "items": [{"item": "B", "verified_still_missing": True}],
            "follow_ups": [{"key": "x", "title": "Port B to gtk and tui", "body": "both"}],
        }

        dispositions = run_followthrough_sweep(
            [self._row()], config=config,
            plan_leg_dispatcher=self._fake_plan_leg(plan),
            writer=writer, issue_open=lambda repo, number: True,
        )

        assert len(dispositions) == 1
        assert dispositions[0].action == ft.ACTION_REJECTED
        assert any("crosses backends (gtk, tui)" in r for r in dispositions[0].reasons)
        assert writer.closed == []

    def test_epic_missing_rejects_through_the_wired_path(self, monkeypatch) -> None:
        """#3945 review: `epic_exists` is wired with the fixed arity
        (`epic_exists(fu.epic)`, one arg) and addressed by the repo's
        GitHub slug, never the local repo name."""
        self._stub_build_partial_merge(monkeypatch)
        monkeypatch.setattr("coord.state.build_board", lambda: Board())
        writer = _FakeFollowThroughWriter()
        seen_repos: list[str] = []

        def fake_get_issue(repo_name, issue_number):
            seen_repos.append(repo_name)
            return {} if issue_number == 999 else {"number": issue_number}

        monkeypatch.setattr("coord.github_ops.get_issue", fake_get_issue)
        plan = {
            "items": [{"item": "B", "verified_still_missing": True}],
            "follow_ups": [
                {"key": "a", "title": "t1", "body": "", "epic": 999},
            ],
        }

        dispositions = run_followthrough_sweep(
            [self._row()], config=self._config(),
            plan_leg_dispatcher=self._fake_plan_leg(plan),
            writer=writer, issue_open=lambda repo, number: True,
        )

        assert len(dispositions) == 1
        assert dispositions[0].action == ft.ACTION_REJECTED
        assert any("epic #999 does not exist" in r for r in dispositions[0].reasons)
        assert writer.closed == []
        # Addressed GitHub by the SLUG (`acme/quadraui`), never the local
        # `coordinator.yml` name (`quadraui`).
        assert seen_repos and all(r == "acme/quadraui" for r in seen_repos)

    def test_milestone_missing_rejects_through_the_wired_path(self, monkeypatch) -> None:
        self._stub_build_partial_merge(monkeypatch)
        monkeypatch.setattr("coord.state.build_board", lambda: Board())
        writer = _FakeFollowThroughWriter()
        seen_repos: list[str] = []

        def fake_get_repo_milestones(repo_name, state="open"):
            seen_repos.append(repo_name)
            return [{"title": "M1"}]

        monkeypatch.setattr("coord.github_ops.get_repo_milestones", fake_get_repo_milestones)
        plan = {
            "items": [{"item": "B", "verified_still_missing": True}],
            "follow_ups": [
                {"key": "a", "title": "t1", "body": "", "milestone": "nope"},
            ],
        }

        dispositions = run_followthrough_sweep(
            [self._row()], config=self._config(),
            plan_leg_dispatcher=self._fake_plan_leg(plan),
            writer=writer, issue_open=lambda repo, number: True,
        )

        assert len(dispositions) == 1
        assert dispositions[0].action == ft.ACTION_REJECTED
        assert any("milestone 'nope' does not exist" in r for r in dispositions[0].reasons)
        assert writer.closed == []
        assert seen_repos and all(r == "acme/quadraui" for r in seen_repos)

    def test_depth_limit_pages_through_the_wired_path(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "coord.board_doctor.build_partial_merge",
            lambda repo_name, issue_number, *, config, board: ft.PartialMerge(
                repo_name=repo_name, issue_number=issue_number, title="T",
                body=f"B\n{ft.FOLLOWTHROUGH_MARKER.format(issue=1)}",
            ),
        )
        monkeypatch.setattr("coord.state.build_board", lambda: Board())
        writer = _FakeFollowThroughWriter()
        paged: list[tuple[str, str]] = []
        monkeypatch.setattr(
            "coord.board_doctor._followthrough_notify",
            lambda repo, number, stage, subject, body: paged.append((stage, subject)),
        )

        def boom_plan_leg(prompt, *, partial, config):
            raise AssertionError("must not dispatch a plan leg for a depth-limited row")

        dispositions = run_followthrough_sweep(
            [self._row()], config=self._config(),
            plan_leg_dispatcher=boom_plan_leg,
            writer=writer, issue_open=lambda repo, number: True,
        )

        assert len(dispositions) == 1
        assert dispositions[0].action == ft.ACTION_DEPTH_PAGED
        assert writer.closed == []
        assert len(paged) == 1 and "followthrough-page" in paged[0][0]


class TestRunSweepFollowthroughWiring:
    """:func:`run_sweep`'s own split: `merged-partial` rows never reach
    :func:`sweep`'s tier loop — they're resolved by *followthrough_sweep*
    instead and reported separately."""

    def test_merged_partial_rows_bypass_the_tier_loop(self, monkeypatch) -> None:
        import coord.board_doctor as board_doctor_mod

        rows = [
            _row("quadraui#1", "merge-conflict"),
            self_row := StuckRow(
                queue="drive-queue", key="quadraui#2180", state=STATE_MERGED_PARTIAL,
                cause=CAUSE_MERGED_PARTIAL, confidence="high",
            ),
        ]
        monkeypatch.setattr(
            board_doctor_mod, "_collect_rows_with_board",
            lambda *, config: (rows, Board()),
        )

        captured_followthrough_rows: list = []
        captured_board = []

        def _fake_followthrough_sweep(ft_rows, *, config, board, dry_run):
            captured_followthrough_rows.extend(ft_rows)
            captured_board.append(board)
            return ["sentinel-disposition"]

        config = Config(repos=[], machines=[])
        report = board_doctor_mod.run_sweep(
            config=config,
            tier_of=lambda cause: TIER_ALERT,
            alert_dispatcher=lambda row: True,
            followthrough_sweep=_fake_followthrough_sweep,
        )

        # Only the non-merged-partial row went through the tier loop.
        assert [d.key for d in report.decisions] == ["quadraui#1"]
        # The merged-partial row was handed to the dedicated pass instead.
        assert captured_followthrough_rows == [self_row]
        assert report.followthrough == ["sentinel-disposition"]
        # Still counted for the "nothing stuck" honesty check (#3758).
        assert report.rows_observed == 2
        # The SAME Board `_collect_rows_with_board` already built is handed
        # to the followthrough pass — never a second `build_board` read.
        assert len(captured_board) == 1 and isinstance(captured_board[0], Board)

    def test_followthrough_false_disarms_the_pass(self, monkeypatch) -> None:
        import coord.board_doctor as board_doctor_mod

        row = StuckRow(
            queue="drive-queue", key="quadraui#2180", state=STATE_MERGED_PARTIAL,
            cause=CAUSE_MERGED_PARTIAL, confidence="high",
        )
        monkeypatch.setattr(
            board_doctor_mod, "_collect_rows_with_board",
            lambda *, config: ([row], Board()),
        )
        called = []

        report = board_doctor_mod.run_sweep(
            config=Config(repos=[], machines=[]),
            followthrough=False,
            followthrough_sweep=lambda *a, **k: called.append(1) or [],
        )

        assert called == []
        assert report.followthrough == []


class TestDriveQueueSweepCliFollowthroughOutput:
    """CLI-level black-box coverage (#3864's open AC item, closed by
    #3945): ``coord drive-queue sweep --dry-run`` prints
    :func:`~coord.board_doctor_followthrough.render_plan`'s rendering of a
    `merged-partial` follow-through and writes nothing."""

    @staticmethod
    def _invoke(monkeypatch, *, dry_run: bool, followthrough_report):
        from click.testing import CliRunner

        from coord.commands.drive_queue import drive_queue_sweep

        monkeypatch.setattr("coord.board_service.is_remote", lambda: False)
        monkeypatch.setattr(
            "coord.commands._common._load_config",
            lambda path, **k: Config(repos=[], machines=[]),
        )
        monkeypatch.setattr(
            "coord.board_doctor.run_sweep", lambda **k: followthrough_report,
        )

        runner = CliRunner()
        args = ["--dry-run"] if dry_run else []
        return runner.invoke(drive_queue_sweep, args)

    def test_no_followthrough_flag_reaches_run_sweep(self, monkeypatch) -> None:
        """#3945 review: `run_sweep`'s own `followthrough` kwarg must be
        reachable from the CLI, not just the env var."""
        from click.testing import CliRunner

        from coord.commands.drive_queue import drive_queue_sweep

        captured_kwargs: dict = {}

        monkeypatch.setattr("coord.board_service.is_remote", lambda: False)
        monkeypatch.setattr(
            "coord.commands._common._load_config",
            lambda path, **k: Config(repos=[], machines=[]),
        )

        def fake_run_sweep(**kwargs):
            captured_kwargs.update(kwargs)
            import coord.board_doctor as board_doctor_mod

            return board_doctor_mod.SweepReport(sweep_id="s1", rows_observed=0)

        monkeypatch.setattr("coord.board_doctor.run_sweep", fake_run_sweep)

        runner = CliRunner()
        result = runner.invoke(drive_queue_sweep, ["--no-followthrough"])

        assert result.exit_code == 0, result.output
        assert captured_kwargs["followthrough"] is False

    def test_dry_run_prints_the_rendered_plan_and_writes_nothing(self, monkeypatch) -> None:
        import coord.board_doctor as board_doctor_mod

        disposition = ft.Disposition(
            repo_name="quadraui", issue_number=2180, action=ft.ACTION_FILED,
            dry_run=True, follow_ups=[],
            rendered_plan=(
                "Remaining items:\n  - [still missing] B\n"
                "Proposed follow-ups:\n  - [a] Implement B core"
            ),
        )
        report = board_doctor_mod.SweepReport(
            sweep_id="s1", dry_run=True, decisions=[], rows_observed=1,
            followthrough=[disposition],
        )

        result = self._invoke(monkeypatch, dry_run=True, followthrough_report=report)

        assert result.exit_code == 0, result.output
        assert "quadraui#2180 merged-partial follow-through" in result.output
        assert "[still missing] B" in result.output
        assert "Implement B core" in result.output
        # The printed promise must not claim nothing was dispatched — a
        # merged-partial row's own read-only plan leg IS a real dispatch
        # even under --dry-run (#3945 review).
        assert "nothing was dispatched" not in result.output
        assert "still dispatched a real, read-only plan leg" in result.output
        # Dry-run action is still ACTION_FILED (what WOULD be filed) but no
        # issue numbers were actually created, so the "filed:" line (which
        # only fires for a real, non-dry-run file) must not appear.
        assert "filed:" not in result.output

    def test_real_run_reports_what_was_filed(self, monkeypatch) -> None:
        import coord.board_doctor as board_doctor_mod

        disposition = ft.Disposition(
            repo_name="quadraui", issue_number=2180, action=ft.ACTION_FILED,
            dry_run=False, follow_ups=[300, 301],
            rendered_plan="Remaining items:\n  - [still missing] B",
        )
        report = board_doctor_mod.SweepReport(
            sweep_id="s1", dry_run=False, decisions=[], rows_observed=1,
            followthrough=[disposition],
        )

        result = self._invoke(monkeypatch, dry_run=False, followthrough_report=report)

        assert result.exit_code == 0, result.output
        assert "filed: #300, #301" in result.output


class TestDispatchPlanLeg:
    """:func:`dispatch_plan_leg` — the real `plan_leg` seam: dispatches a
    headless `type="plan"` leg through the agent, polls `/status` until
    terminal, and reads the raw transcript back."""

    @staticmethod
    def _config() -> Config:
        return Config(
            repos=[Repo(name="quadraui", github="acme/quadraui")],
            machines=[
                Machine(
                    name="box", host="box.tailnet", repos=["quadraui"],
                    repo_paths={"quadraui": "/src/quadraui"},
                ),
            ],
        )

    @staticmethod
    def _partial() -> ft.PartialMerge:
        return ft.PartialMerge(
            repo_name="quadraui", issue_number=2180, title="T", body="B",
            github_repo="acme/quadraui",
        )

    def test_happy_path_polls_until_done_and_returns_the_transcript(self, monkeypatch) -> None:
        from coord.network import StatusResult

        # The first call is `select_fix_machine`'s own pre-dispatch
        # reachability probe (discarded — it predates the assignment even
        # existing); the real poll loop starts from the second.
        statuses = iter(["running", "running", "done"])
        sleeps: list[float] = []

        def fake_dispatch(proposal, config, **kwargs):
            assert proposal.type == "plan"
            assert proposal.system_prompt == "PROMPT"
            assert proposal.repo_name == "quadraui"
            return {"id": "assign-1"}

        def fake_status_fetcher(machine):
            return StatusResult(
                data={"active": [{"id": "assign-1", "status": next(statuses)}], "completed": []},
            )

        monkeypatch.setattr("coord.dispatch.dispatch", fake_dispatch)
        recorded = []
        monkeypatch.setattr(
            "coord.state.record_dispatched_assignment",
            lambda *, assignment, repo_github: recorded.append((assignment, repo_github)),
        )

        result = dispatch_plan_leg(
            "PROMPT",
            partial=self._partial(),
            config=self._config(),
            status_fetcher=fake_status_fetcher,
            text_fetcher=lambda host, aid: "the plan text",
            sleep=sleeps.append,
            now=iter([0.0, 1.0, 2.0, 3.0]).__next__,
        )

        assert result.assignment_id == "assign-1"
        assert result.output == "the plan text"
        assert len(sleeps) == 1  # one non-terminal poll before "done"
        # #3945 review: the leg must be visible to `coord status`/cost
        # attribution for the up-to-20-minutes it occupies a slot, same as
        # every other non-CLI dispatcher's `dispatch* -> record_dispatched_
        # assignment` pattern.
        assert len(recorded) == 1
        assignment, repo_github = recorded[0]
        assert assignment.assignment_id == "assign-1"
        assert assignment.machine_name == "box"
        assert repo_github == "acme/quadraui"

    def test_no_reachable_machine_raises(self, monkeypatch) -> None:
        from coord.network import StatusResult

        monkeypatch.setattr(
            "coord.dispatch.dispatch",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not dispatch")),
        )

        with pytest.raises(RuntimeError, match="no machine reachable"):
            dispatch_plan_leg(
                "PROMPT",
                partial=self._partial(),
                config=Config(repos=[Repo(name="quadraui", github="acme/quadraui")], machines=[]),
                status_fetcher=lambda m: StatusResult(error="unreachable"),
            )

    def test_timeout_raises_with_the_last_observed_status(self, monkeypatch) -> None:
        from coord.network import StatusResult

        monkeypatch.setattr(
            "coord.dispatch.dispatch", lambda proposal, config, **kwargs: {"id": "assign-1"},
        )
        clock = iter([0.0, 5.0, 25.0])

        def fake_status_fetcher(machine):
            return StatusResult(
                data={"active": [{"id": "assign-1", "status": "running"}], "completed": []},
            )

        with pytest.raises(RuntimeError, match="did not reach a terminal state"):
            dispatch_plan_leg(
                "PROMPT",
                partial=self._partial(),
                config=self._config(),
                status_fetcher=fake_status_fetcher,
                sleep=lambda s: None,
                now=clock.__next__,
                timeout_seconds=20.0,
            )
