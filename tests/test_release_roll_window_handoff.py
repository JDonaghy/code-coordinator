"""#2587: roll at the drive queue's natural inter-drive gap, never a drain.

An attended manual roll on 2026-08-22 ran `coord release nightly-window` for
its full 60-minute deadline, drained nothing, and rolled nothing — with
`coord-drive-queue.timer` (and therefore ALL reconciliation and ALL new
dispatch) stopped the entire time. The fleet-wide quiescent window that
mechanism waited for essentially never arrives on a continuously-busy
queue. This issue replaces "stop a timer and poll for a window" with "set a
marker; let the tick that is already running notice the moment it goes
quiet".

This file is the acceptance bar for the whole handoff, spanning the three
places involved:

* `coord.drive_queue` — the pure decision half: `RollPending`'s own bound,
  and `plan_tick`'s `roll_pending_reason` parameter (launch nothing,
  reconcile normally, exactly like a #2101 release cordon or #2110
  `--reconcile-only`).
* `coord.commands.drive_queue` — the I/O shell: the marker store
  (`read_roll_pending`/`write_roll_pending`/`clear_roll_pending`), and
  `drive_queue_tick`'s handling of it (force reconcile-only posture, fire
  `systemctl --user start --no-block coord-release-window.service` the
  instant `TickPlan.occupied` reaches 0, bump/self-clear the bound).
* `coord.commands.release` — `coord release propagate`/`nightly-window` set
  the marker instead of draining.
* `coord.notify` — dispatches no NEW leg while the marker is live.

Four sections, matching the issue's own acceptance list:

1. `TestPlanTickRollPending` — the pure decision half.
2. `TestTickFiresAtTheInterDriveGap` — a continuously-busy queue, driven
   through the real CLI, proves the roll fires the instant the queue empties
   out, with NO timer ever stopped, and reconciliation running throughout.
3. `TestRollPendingSelfClears` — the TTL and deferral-ceiling bound.
4. `TestNightlyWindowHandsOffToTheTick` — `coord release nightly-window`
   sets the marker for real; `coord drive-queue tick` picks it up.
5. `TestNotifyDispatchesNoNewLegsWhilePending` — the `coord notify` gap the
   2026-08-22 incident also exposed (a review dispatched a minute into what
   was supposed to be quiescent).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from coord import state
from coord.cli import main
from coord.commands import drive_queue as dq_cmd
from coord.config import Config
from coord.drive_queue import (
    ROLL_PENDING_DEFAULT_MAX_DEFERRALS,
    ROLL_PENDING_DEFAULT_TTL_SECONDS,
    STATE_RUNNING,
    STATE_WAITING,
    BoardView,
    IssueFacts,
    QueueEntry,
    RollPending,
    entry_key,
    plan_tick,
)
from coord.models import Machine, Repo
from tests import backends

REPO = "claude-coordinator"
NOW = 1_800_000_000.0


def entry(issue: int, **kw) -> QueueEntry:
    base: dict = {"repo": REPO, "issue": issue, "position": issue}
    base.update(kw)
    return QueueEntry(**base)


def board(*, merged: tuple[int, ...] = (), open_: tuple[int, ...] = (),
         sessions: tuple[int, ...] = ()) -> BoardView:
    facts: dict[str, IssueFacts] = {}
    for issue in {*merged, *open_}:
        facts[entry_key(REPO, issue)] = IssueFacts(
            known=True,
            issue_state="open" if issue in open_ else "closed",
            merged=issue in merged,
        )
    return BoardView(issues=facts, live_sessions=frozenset(entry_key(REPO, i) for i in sessions))


# ── CLI-level fixtures (mirrors tests/test_cli_drive_queue.py's own) ──────

_CONFIG_YAML = f"""\
repos:
  - name: {REPO}
    github: john/claude-coordinator
    default_branch: main
machines:
  - name: dellserver
    host: dellserver
    repos: [{REPO}]
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "coordinator.yml"
    path.write_text(_CONFIG_YAML)
    return path


@pytest.fixture
def cli(config_file: Path):
    """Invoke `coord drive-queue <args...>` with the seeded config."""

    def run(*args: str):
        return CliRunner().invoke(main, ["drive-queue", *args, "--config", str(config_file)])

    return run


@pytest.fixture
def seed(coord_db):
    """Write `issues`/`assignments` rows the tick will actually read back —
    same shape as test_cli_drive_queue.py's own `seed` fixture."""

    def _seed(
        *, issues: dict[int, str] | None = None,
        assignments: list[dict[str, Any]] | None = None,
    ) -> None:
        for number, issue_state in (issues or {}).items():
            backends.upsert_issue(
                coord_db, repo_name=REPO, number=number, title=f"issue {number}",
                state=issue_state,
            )
        for index, row in enumerate(assignments or []):
            coord_db.execute(
                "INSERT INTO assignments "
                "(assignment_id, repo_name, issue_number, issue_title, "
                " machine_name, type, status, dispatched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row.get("assignment_id", f"a-{index}"),
                    REPO,
                    row["issue_number"],
                    f"issue {row['issue_number']}",
                    "dellserver",
                    row.get("type", "work"),
                    row["status"],
                    100.0 + index,
                ),
            )
        coord_db.commit()

    return _seed


@pytest.fixture(autouse=True)
def no_tmux(monkeypatch):
    """No live drive sessions unless a test says otherwise."""
    monkeypatch.setattr("coord.drive.list_drive_sessions", lambda *a, **k: [])


@pytest.fixture(autouse=True)
def _default_pipeline_labels(monkeypatch):
    """#2839: `add` now also projects `coord`/`status:ready` onto the issue
    via `coord.state.apply_issue_labels` — default that to an inert no-op
    here. Without it, `add`'s real label write falls through to
    `github_ops._gh` and, once a test also mocks `subprocess.run` (the
    `launches` fixture below), lands in that SAME capture — `subprocess.run`
    is one singleton module attribute — polluting the captured argv this
    file asserts on with spurious `gh issue view`/`gh issue edit` entries.
    See the identical fixture (and its longer rationale) in
    `tests/test_cli_drive_queue.py`.
    """
    monkeypatch.setattr(
        "coord.state.apply_issue_labels", lambda *a, **k: ([], False)
    )


@pytest.fixture
def live_sessions(monkeypatch):
    def _set(*issues: int) -> None:
        monkeypatch.setattr(
            "coord.drive.list_drive_sessions",
            lambda *a, **k: [{"repo": REPO, "issue": n} for n in issues],
        )

    return _set


@pytest.fixture(autouse=True)
def tick_lock(monkeypatch, tmp_path) -> Path:
    """Give every test its own tick lock — see test_cli_drive_queue.py's
    identical fixture for why this must never resolve under the real
    `~/.coord`."""
    path = tmp_path / "drive-queue.lock"
    monkeypatch.setattr("coord.filelock.drive_queue_lock_path", lambda: path)
    return path


@pytest.fixture(autouse=True)
def block_log(monkeypatch, tmp_path) -> Path:
    monkeypatch.setenv("COORD_BLOCK_LOG", str(tmp_path / "queue-block-log.jsonl"))


class _Launches(list):
    """Captured subprocess argvs from `coord.commands.drive_queue`'s ONE
    `subprocess.run` seam — both `coord drive --tmux` launches AND #2587's
    `systemctl ... coord-release-window.service` fire attempt go through it,
    so a test tells them apart by argv content, never by a second mock."""


@pytest.fixture
def launches(monkeypatch) -> _Launches:
    captured = _Launches()

    class _Result:
        def __init__(self, returncode: int = 0, stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = ""
            self.stderr = stderr

    def fake_run(argv, **_kw):
        captured.append(list(argv))
        return _Result()

    monkeypatch.setattr("coord.commands.drive_queue.subprocess.run", fake_run)
    return captured


def queued(issue: int) -> dict | None:
    return state._get_drive_queue_entry_local(REPO, issue)


# ── 1. the pure decision half ─────────────────────────────────────────────


class TestRollPendingBound:
    def test_not_expired_well_within_its_ttl(self):
        p = RollPending(target_version="1.2.3", set_at=NOW - 10.0)
        assert p.expired(NOW) is False

    def test_expires_once_its_ttl_elapses(self):
        p = RollPending(
            target_version="1.2.3", set_at=NOW - ROLL_PENDING_DEFAULT_TTL_SECONDS - 1.0,
        )
        assert p.expired(NOW) is True

    def test_expires_at_the_deferral_ceiling_even_within_ttl(self):
        """The second, independent bound (#2587's own comment: a wedged clock
        must not defeat the TTL half alone)."""
        p = RollPending(
            target_version="1.2.3", set_at=NOW - 1.0,
            ttl_seconds=999_999.0, max_deferrals=ROLL_PENDING_DEFAULT_MAX_DEFERRALS,
            deferrals=ROLL_PENDING_DEFAULT_MAX_DEFERRALS,
        )
        assert p.expired(NOW) is True

    def test_zero_disables_a_bound_the_same_way_max_parallel_per_repo_does(self):
        p = RollPending(
            target_version="1.2.3", set_at=NOW - 999_999.0,
            ttl_seconds=0.0, max_deferrals=0, deferrals=999,
        )
        assert p.expired(NOW) is False

    def test_to_dict_from_dict_round_trip(self):
        p = RollPending(
            target_version="0.5.230", set_at=1000.0, reason="nightly-window",
            ttl_seconds=3600.0, max_deferrals=20, deferrals=3,
        )
        assert RollPending.from_dict(p.to_dict()) == p

    def test_from_dict_is_tolerant_of_a_hand_edited_missing_bound(self):
        """A marker file a human hand-edited (or an older schema) is not a
        reason to crash the tick — see `RollPending.from_dict`'s docstring."""
        restored = RollPending.from_dict({"target_version": "0.5.230", "set_at": 1000.0})
        assert restored.ttl_seconds == ROLL_PENDING_DEFAULT_TTL_SECONDS
        assert restored.max_deferrals == ROLL_PENDING_DEFAULT_MAX_DEFERRALS
        assert restored.deferrals == 0


class TestPlanTickRollPending:
    def test_reconciliation_still_runs_while_launch_is_refused(self):
        """The whole mechanism in one assertion: a `running` entry whose
        issue actually landed reconciles to `done` — occupied drops to 0 —
        even though `roll_pending_reason` blocks the launch that would
        otherwise immediately follow."""
        running = entry(1650, state=STATE_RUNNING)
        waiting = entry(1654, state=STATE_WAITING, position=1)
        b = board(merged=(1650,), open_=(1654,))

        plan = plan_tick(
            [running, waiting], b, capacity=5, now=NOW,
            roll_pending_reason="roll pending: v9.9.9 (nightly-window)",
        )

        assert plan.roll_pending_reason == "roll pending: v9.9.9 (nightly-window)"
        assert plan.launch is None
        assert plan.occupied == 0  # reconciliation ran: 1650 no longer occupies a slot
        done_keys = {r.key for r in plan.reconciles if r.outcome == "done"}
        assert entry_key(REPO, 1650) in done_keys

    def test_still_occupied_reads_correctly_while_roll_pending(self):
        """The other half: an entry genuinely still running is reported as
        occupying its slot — not silently dropped just because launching is
        refused. This is the `occupied` reading the tick shell (section 2)
        uses to decide whether the inter-drive gap has arrived yet."""
        running = entry(1650, state=STATE_RUNNING)
        b = board(open_=(1650,), sessions=(1650,))

        plan = plan_tick(
            [running], b, capacity=5, now=NOW, roll_pending_reason="roll pending: v9.9.9",
        )
        assert plan.launch is None
        assert plan.occupied == 1

    def test_blocks_a_launch_that_would_otherwise_happen_right_now(self):
        """Isolates roll-pending as the actual cause: capacity is wide open
        and the entry is fully eligible — WITHOUT the marker it launches."""
        waiting = entry(1650, state=STATE_WAITING)
        b = board(open_=(1650,))

        blocked = plan_tick(
            [waiting], b, capacity=5, now=NOW, roll_pending_reason="roll pending: v9.9.9",
        )
        assert blocked.launch is None

        baseline = plan_tick([waiting], b, capacity=5, now=NOW)
        assert baseline.launch is not None
        assert baseline.launch.issue == 1650

    def test_no_alert_is_raised_for_a_roll_pending_hold(self):
        """#2587: a deliberately-held queue must never read as broken — no
        `QueueAlert`, unlike a cordon (which DOES raise one)."""
        waiting = entry(1650, state=STATE_WAITING)
        b = board(open_=(1650,))
        plan = plan_tick(
            [waiting], b, capacity=5, now=NOW, roll_pending_reason="roll pending: v9.9.9",
        )
        assert plan.alert is None


# ── 2. the real tick, driven through the CLI ──────────────────────────────


def _pending(**overrides) -> RollPending:
    kwargs = {"target_version": "9.9.9", "set_at": time.time(), "reason": "nightly-window"}
    kwargs.update(overrides)
    return RollPending(**kwargs)


class TestTickFiresAtTheInterDriveGap:
    """The issue's own acceptance test: a continuously-busy queue, driven
    through the REAL `coord drive-queue tick` CLI (not `plan_tick` directly)
    — proving the SHELL's marker handling end to end: force reconcile-only
    posture, fire the roll via `systemctl` (never a `coord drive --tmux`
    launch, never a stopped timer) the instant the queue empties out, and
    keep reconciling throughout."""

    def test_busy_then_the_gap_arrives_and_the_roll_fires_with_no_timer_stopped(
        self, cli, seed, launches, live_sessions
    ):
        seed(issues={1650: "open", 1654: "open"})
        cli("add", REPO, "1650")
        cli("add", REPO, "1654")
        state._update_drive_queue_entry_local(REPO, 1650, state="running")
        live_sessions(1650)
        dq_cmd.write_roll_pending(_pending())

        # ── tick 1: the queue is genuinely busy — 1650's session is alive.
        first = cli("tick")
        assert first.exit_code == 0, first.output
        assert queued(1650)["state"] == "running"  # reconciliation ran and found it alive
        assert queued(1654)["state"] == "waiting"  # never launched
        assert launches == []  # no coord-drive launch, no fire attempt yet
        assert dq_cmd.read_roll_pending() is not None
        assert "roll pending: v9.9.9" in first.output

        # ── the drive finishes: session ends, its issue lands. This is the
        #    inter-drive gap — nothing else changes about the queue.
        live_sessions()  # no more live sessions
        seed(issues={1650: "closed"}, assignments=[{"issue_number": 1650, "status": "merged"}])

        second = cli("tick")
        assert second.exit_code == 0, second.output
        # Reconciliation kept running throughout the pending window: 1650
        # actually moved to `done` this tick.
        assert queued(1650)["state"] == "done"
        # The gap arrived (occupied -> 0) and yet 1654 — fully eligible,
        # capacity wide open — still did NOT launch. That is the whole
        # mechanism: the marker, not capacity, is what governs this.
        assert queued(1654)["state"] == "waiting"

        # Exactly one subprocess call this tick, and it is the roll firing —
        # never a `coord drive --tmux` launch.
        assert len(launches) == 1, launches
        fired_argv = launches[0]
        assert "systemctl" in fired_argv[0] or "systemctl" in fired_argv
        assert "coord-release-window.service" in fired_argv
        assert "drive" not in fired_argv

        # And — the acceptance test's own words — no timer was EVER stopped:
        # neither `coord-drive-queue.timer` nor any other unit appears as
        # the target of a stop/start, across either tick.
        joined = " ".join(" ".join(argv) for argv in launches)
        assert "coord-drive-queue.timer" not in joined
        assert "stop" not in joined

        # #2587 review: the tick must NEVER clear the marker itself on a
        # fire — `_fire_pending_roll` returning True only means `systemctl
        # --user start --no-block` was ACCEPTED, not that a roll happened.
        # Clearing it here, in the tick's own process, raced the freshly
        # spawned `coord-release-window.service` out from under it on every
        # real invocation (see `RollPending`'s own docstring): that process
        # re-resolves its target from a PyPI lookup + a fleet health gather
        # before it ever reads this same marker, by which point the tick
        # would already have deleted it. So the marker must survive,
        # unchanged, for that process to find.
        survived = dq_cmd.read_roll_pending()
        assert survived is not None, (
            "the tick cleared the roll-pending marker itself — the spawned "
            "coord-release-window.service will never see it"
        )
        assert survived.target_version == "9.9.9"
        # 1, from tick 1's "still busy" deferral — the fire attempt itself
        # (this tick) spends no ADDITIONAL deferral; only "still busy" ticks
        # bump the counter.
        assert survived.deferrals == 1
        assert "roll fired" not in second.output  # no longer claims confirmed success
        assert "requested roll" in second.output
        assert "coord-release-window.service" in second.output

        # ── a third tick, while the spawned service is (from this test's
        #    perspective) still mid-run: still nothing to launch, so the gap
        #    still reads as arrived. Re-firing costs nothing — `systemctl
        #    start` against an already-active `Type=oneshot` unit is
        #    systemd's own no-op — and the marker still isn't this tick's to
        #    clear.
        third = cli("tick")
        assert third.exit_code == 0, third.output
        assert len(launches) == 2, launches
        assert "coord-release-window.service" in launches[1]
        still_pending = dq_cmd.read_roll_pending()
        assert still_pending is not None
        assert still_pending.target_version == "9.9.9"
        # Still 1 — a repeated fire attempt (this tick too saw the gap) is
        # not a "still busy" deferral either.
        assert still_pending.deferrals == 1

    def test_status_shows_the_marker_while_it_is_live(self, cli, seed):
        """#2587's own non-negotiable: a deliberately held queue must never
        read as broken — `coord drive-queue status` names it."""
        seed(issues={1650: "open"})
        cli("add", REPO, "1650")
        dq_cmd.write_roll_pending(_pending(target_version="0.5.230"))

        result = cli("status")
        assert result.exit_code == 0, result.output
        assert "roll pending: v0.5.230" in result.output
        assert "deferrals" in result.output

        as_json = cli("status", "--json")
        payload = __import__("json").loads(as_json.output)
        assert payload["roll_pending"]["target_version"] == "0.5.230"


class TestTickFiresOnTheBetweenLegsSettleWindowReading:
    """#2870 part 2: apply #2854's between-legs/settle-window reading to the
    tick's own fire condition, not just `coord release propagate`'s.

    The 2026-08-28 incident: a drive-queue row stayed `running` in the
    queue (Work and Test legs landed, Review not yet dispatched) with its
    last-known host `elitebook` — no LIVE assignment right now, and long
    past the #2854 settle window. `coord release propagate`, run directly,
    read this as `quiescent — nothing in flight` (see `test_release_roll_
    between_legs_2854.py`'s own coverage of `assess_quiescence` for that
    half). But the tick's own reconciliation, asked from `dellserver`
    (#1870: a local tmux read proves nothing about a row launched
    elsewhere), read the SAME row as `unknown` — still occupying a slot —
    and the pre-#2870 fire condition (`plan.occupied == 0`) never fired the
    pending roll at all: the queue froze on a strictly stricter bar than
    the one that actually mattered.
    """

    def _seed_between_legs_row(
        self, coord_db, issue: int, *, finished_ago: float, host: str = "elitebook"
    ) -> None:
        """A `running` queue row whose last leg finished on *host*
        *finished_ago* seconds before "now" — no live assignment for it
        right now (the row is between legs), matching the shape
        `test_release_roll_between_legs_2854.py` builds directly against
        `assess_quiescence`, here seeded through the real DB so the whole
        `coord drive-queue tick` CLI path exercises it end to end."""
        finished_at = time.time() - finished_ago
        state._update_drive_queue_entry_local(
            REPO, issue, state="running", launch_host=host,
            launched_at=finished_at - 5000.0,
        )
        coord_db.execute(
            "INSERT INTO assignments "
            "(assignment_id, repo_name, issue_number, issue_title, "
            " machine_name, type, status, dispatched_at, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"a-{issue}-work", REPO, issue, f"issue {issue}", host, "work",
             "done", finished_at - 500.0, finished_at),
        )
        coord_db.commit()

    def test_a_between_legs_row_still_fires_the_roll_even_though_occupied_is_nonzero(
        self, cli, seed, launches, coord_db, monkeypatch
    ):
        seed(issues={2862: "open"})
        cli("add", REPO, "2862")
        # Launched on a DIFFERENT host than the tick below runs on — #1870's
        # `unknown` verdict, still occupying a slot from THIS tick's own
        # reconciliation, with no local tmux session to disprove it.
        self._seed_between_legs_row(coord_db, 2862, finished_ago=1000.0)
        dq_cmd.write_roll_pending(_pending(target_version="9.9.9"))

        monkeypatch.setattr("socket.gethostname", lambda: "dellserver")
        result = cli("tick")

        assert result.exit_code == 0, result.output
        # The tick's OWN reconciliation still reads this as occupied — #1870
        # never disproves a foreign `launch_host`.
        entry = queued(2862)
        assert entry["state"] == "running"

        # And yet — the whole point — the roll still fires: #2854's
        # settle-window reading, re-derived for this exact row, overrides
        # the strict `occupied == 0` bar.
        assert len(launches) == 1, launches
        assert "coord-release-window.service" in launches[0]

        survived = dq_cmd.read_roll_pending()
        assert survived is not None, "the tick must never clear the marker itself"
        assert survived.target_version == "9.9.9"

    def test_a_between_legs_row_still_within_the_settle_window_does_not_fire(
        self, cli, seed, launches, coord_db, monkeypatch
    ):
        """The mirror case: the same shape, but the gap is only 5s old — too
        fresh to trust (#2854's own debounce). Must NOT fire."""
        seed(issues={2863: "open"})
        cli("add", REPO, "2863")
        self._seed_between_legs_row(coord_db, 2863, finished_ago=5.0)
        dq_cmd.write_roll_pending(_pending(target_version="9.9.9"))

        monkeypatch.setattr("socket.gethostname", lambda: "dellserver")
        result = cli("tick")

        assert result.exit_code == 0, result.output
        assert launches == [], (
            "a gap too fresh to trust must not fire the roll — #2854's own "
            "debounce"
        )
        assert dq_cmd.read_roll_pending() is not None


class TestRollPendingDoesNotBlockDirectMerges:
    """#2587 review (non-blocking): forcing the tick's reconcile-only
    capacity posture while a roll is pending must not also suppress the
    #2350 direct-merge fast path (`_run_merge_only_candidates`) — merging an
    already-fully-approved entry is not "launching new work", and skipping
    it would leave that entry queued, unmerged, for the marker's entire
    span, for no reason connected to the roll. An EXPLICIT
    `--reconcile-only`/`--max-parallel 0` request is unaffected: THAT flag's
    own contract still promises to touch nothing external."""

    def test_merge_only_still_runs_while_a_roll_is_pending(self, cli, seed, monkeypatch):
        calls = []
        monkeypatch.setattr(
            dq_cmd, "_run_merge_only_candidates",
            lambda plan, config_path: calls.append(plan),
        )
        seed(issues={1650: "open"})
        cli("add", REPO, "1650")
        dq_cmd.write_roll_pending(_pending())

        result = cli("tick")
        assert result.exit_code == 0, result.output
        assert len(calls) == 1, "roll-pending must not skip the merge-only fast path"

    def test_merge_only_is_still_skipped_under_an_explicit_reconcile_only(
        self, cli, seed, monkeypatch
    ):
        calls = []
        monkeypatch.setattr(
            dq_cmd, "_run_merge_only_candidates",
            lambda plan, config_path: calls.append(plan),
        )
        seed(issues={1650: "open"})
        cli("add", REPO, "1650")

        result = cli("tick", "--reconcile-only")
        assert result.exit_code == 0, result.output
        assert calls == [], "an explicit --reconcile-only must still skip it"


# ── 3. the marker self-clears at its bound ────────────────────────────────


class TestRollPendingSelfClears:
    """#2587's non-negotiable: a pending or failed roll must never hold the
    queue down indefinitely. Both independent bounds — see
    `TestRollPendingBound` above for the pure predicate this exercises end
    to end through the real tick."""

    def test_an_expired_marker_self_clears_and_launching_resumes_the_same_tick(
        self, cli, seed, launches
    ):
        seed(issues={1650: "open"})
        cli("add", REPO, "1650")
        dq_cmd.write_roll_pending(
            _pending(set_at=time.time() - ROLL_PENDING_DEFAULT_TTL_SECONDS - 60.0)
        )

        result = cli("tick")
        assert result.exit_code == 0, result.output

        assert dq_cmd.read_roll_pending() is None  # self-cleared
        # Launching resumed in THIS SAME tick — never "cleared, but wait one
        # more interval before anything launches".
        assert queued(1650)["state"] == "running"
        assert any("drive" in argv for argv in launches)

        # Loud, not silent (#2587's own "never silently held" requirement) —
        # a dedicated escalation record, separate from the ordinary queue
        # alert (which this tick's own successful launch already clears).
        escalation = state._get_drive_escalation_local(
            dq_cmd.ROLL_PENDING_ALERT_REPO, dq_cmd.ROLL_PENDING_ALERT_ISSUE,
        )
        assert escalation is not None
        assert "9.9.9" in escalation["reason"]
        assert "expired" in escalation["reason"]

    def test_the_deferral_ceiling_alone_also_self_clears(self, cli, seed, launches):
        """The clock-independent half of the bound: a marker whose `set_at`
        is recent still expires once its deferral count reaches the
        ceiling — see `RollPending.expired`'s two-bound docstring."""
        seed(issues={1650: "open"})
        cli("add", REPO, "1650")
        dq_cmd.write_roll_pending(
            _pending(
                set_at=time.time() - 5.0,
                max_deferrals=ROLL_PENDING_DEFAULT_MAX_DEFERRALS,
                deferrals=ROLL_PENDING_DEFAULT_MAX_DEFERRALS,
            )
        )

        result = cli("tick")
        assert result.exit_code == 0, result.output
        assert dq_cmd.read_roll_pending() is None
        assert queued(1650)["state"] == "running"

    def test_a_still_busy_marker_bumps_its_deferral_count_each_tick(
        self, cli, seed, launches, live_sessions
    ):
        """The counter the ceiling above actually measures: each tick the
        fleet is still busy adds exactly one deferral, never more."""
        seed(issues={1650: "open"})
        cli("add", REPO, "1650")
        state._update_drive_queue_entry_local(REPO, 1650, state="running")
        live_sessions(1650)
        dq_cmd.write_roll_pending(_pending(deferrals=0))

        cli("tick")
        first = dq_cmd.read_roll_pending()
        assert first is not None
        assert first.deferrals == 1

        cli("tick")
        second = dq_cmd.read_roll_pending()
        assert second is not None
        assert second.deferrals == 2
        # `set_at` stays frozen at the ORIGINAL time — see
        # `_bump_roll_pending_deferral`'s docstring for why the TTL must
        # measure real age, not "time since last bumped".
        assert second.set_at == first.set_at


class TestRollLedgerBoundsFreshArms:
    """#2889: the follow-up to `TestRollPendingSelfClears` above. A marker
    that reaches its own TTL and self-clears is not the end of the story —
    nothing bounded how often a FRESH marker (a brand new `set_at`, brand
    new `deferrals`) could follow it, and #2889's own incident saw the
    queue re-frozen ten times in ~15 hours, 49 ticks refused to launch,
    each individual marker perfectly well-behaved by its own bound. This is
    the issue's own acceptance list, bullet 1, driven through the REAL tick:
    "arm a marker ... let it expire, and assert a SECOND marker for the
    same target is refused (or rate-limited) rather than armed fresh."
    """

    def test_a_fresh_re_arm_right_after_expiry_is_refused_and_the_queue_keeps_launching(
        self, cli, seed, launches, monkeypatch,
    ):
        from coord.commands import release as release_cmd

        seed(issues={1650: "open"})
        cli("add", REPO, "1650")
        monkeypatch.setattr(time, "time", lambda: NOW)
        armed = release_cmd._ensure_roll_pending_marker("9.9.9", reason="propagate")
        assert armed is True

        # Force the marker past its own TTL — the SAME self-clear
        # `TestRollPendingSelfClears` exercises above: the tick escalates it
        # loudly, clears it, AND launches the queued entry in this SAME
        # tick (never "cleared, but wait one more interval").
        monkeypatch.setattr(
            time, "time", lambda: NOW + ROLL_PENDING_DEFAULT_TTL_SECONDS + 60.0,
        )
        result = cli("tick")
        assert result.exit_code == 0, result.output
        assert dq_cmd.read_roll_pending() is None  # self-cleared
        assert queued(1650)["state"] == "running"  # the queue kept launching

        # A SECOND, FRESH arm attempted immediately after — real wall-clock
        # time has not moved since the expiry above — must be REFUSED, not
        # armed fresh: the exact "ten fresh arms in 15h" pathology #2889
        # reports. Fails against unfixed `main`: no rate limit exists there,
        # so this would just succeed and write a new marker.
        refused = release_cmd._ensure_roll_pending_marker("9.9.9", reason="propagate")
        assert refused is False, "a fresh re-arm right after expiry must be rate-limited"
        assert dq_cmd.read_roll_pending() is None, (
            "a refused arm must write nothing at all — the queue is never "
            "re-frozen for a marker that was declined"
        )


# ── 4. `coord release nightly-window` -> `coord drive-queue tick` ─────────


class TestNightlyWindowHandsOffToTheTick:
    """The literal handoff this file is named for: `coord release
    nightly-window` sets the marker for real (no stubbed `write_roll_pending`
    call — driven through the actual command), and a later `coord
    drive-queue tick` picks it up with no further coordination between the
    two commands beyond the one shared file."""

    def test_nightly_window_sets_it_and_the_next_tick_fires_it(
        self, cli, seed, launches, config_file, tmp_path, monkeypatch
    ):
        from coord import release_verify as rv
        from coord.commands import release as release_cmd

        # `coord release nightly-window`'s own hazards, mirrored from
        # tests/test_cli_release_window.py: never let it touch the real
        # `~/.coord`, and stub the fleet sweep + PyPI/board reads so this
        # test is hermetic.
        home = tmp_path / "home"
        (home / ".coord").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        window_state_dir = tmp_path / "window-state"
        window_state_dir.mkdir()
        monkeypatch.setattr(release_cmd, "_state_dir", lambda: window_state_dir)
        monkeypatch.setattr(release_cmd, "_fetch_board", lambda: ({}, None))

        lanes = [rv.Lane(host="dellserver", lane="~/.coord-venv", version="0.5.30")]
        machine_health = {
            "dellserver": {"version": "0.5.31", "health": {"schema": 1, "results": []}},
        }
        monkeypatch.setattr(rv, "gather", lambda *a, **k: (machine_health, {}, None, "dellserver"))
        monkeypatch.setattr(
            rv, "verify",
            lambda **kwargs: rv.VerifyReport(
                expected=kwargs.get("expected"), lanes=lanes, findings=[],
            ),
        )

        window_result = CliRunner().invoke(
            main,
            ["release", "nightly-window", "--config", str(config_file),
             "--target", "9.9.9", "--daemon-host", "dellserver"],
        )
        assert window_result.exit_code == 0, window_result.output
        assert "roll pending" in window_result.output

        pending = dq_cmd.read_roll_pending()
        assert pending is not None
        assert pending.target_version == "9.9.9"
        assert pending.reason == "nightly-window"

        # Now the drive-queue side, with an eligible, currently-IDLE entry —
        # so the "gap" is already there the moment the marker exists.
        seed(issues={1650: "open"})
        cli("add", REPO, "1650")

        tick_result = cli("tick")
        assert tick_result.exit_code == 0, tick_result.output
        assert "roll pending: v9.9.9" in tick_result.output
        # Never launched via the drive-queue's own mechanism — the marker
        # governed it, not capacity.
        assert queued(1650)["state"] == "waiting"
        # Exactly the roll-firing subprocess, never a `coord drive` launch.
        assert len(launches) == 1, launches
        assert "coord-release-window.service" in launches[0]

        # #2587 review: the tick's fire must NOT have cleared this marker —
        # `systemctl --user start --no-block` returning 0 only means the
        # start request was accepted, before the spawned process has even
        # begun running. See `TestTickFiresAtTheInterDriveGap`'s own
        # regression test for the isolated version of this assertion.
        assert dq_cmd.read_roll_pending() is not None, (
            "the tick cleared the marker before the spawned "
            "coord-release-window.service could ever read it"
        )

        # Close the loop for real: a THIRD invocation of `coord release
        # nightly-window`, run the same way the real
        # `deploy/coord-release-window.service` unit's `ExecStart=` runs it —
        # no `--target` at all, so it re-resolves "latest" from scratch
        # (stubbed here to land on the SAME version already pending, exactly
        # what happens in production since nothing changed on PyPI between
        # the tick's fire and this run). This is what the freshly spawned
        # process actually does; it must find the still-live marker from
        # above and drive it to completion via `coord release propagate`.
        from coord import release_propagate as rp

        monkeypatch.setattr(release_cmd, "_resolve_expected", lambda *a, **k: ("9.9.9", None))
        prop_calls: list[dict] = []

        def _fake_run_propagate(**kwargs):
            prop_calls.append(kwargs)
            return rp.STATUS_VERIFIED, 0, "verified", None

        monkeypatch.setattr(release_cmd, "_run_propagate", _fake_run_propagate)

        spawned_result = CliRunner().invoke(
            main,
            ["release", "nightly-window", "--config", str(config_file),
             "--daemon-host", "dellserver"],
        )
        assert spawned_result.exit_code == 0, spawned_result.output
        # It found the marker and attempted the real fire — not the "no
        # marker pending -> set a fresh one" branch the pre-fix race always
        # took.
        assert len(prop_calls) == 1, prop_calls
        assert prop_calls[0]["target_version"] == "9.9.9"
        assert dq_cmd.read_roll_pending() is None  # NOW it is actually cleared


# ── 5. `coord notify` dispatches no NEW leg while the marker is set ───────


@pytest.fixture
def notify_config() -> Config:
    return Config(
        repos=[Repo(name="api", github="acme/api")],
        machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"])],
    )


def _agent_completed(assignment_id: str, status: str, **overrides) -> dict:
    """Same shape `tests/test_notify.py`'s own helper builds — the fields
    `notify_mod._agent_status`'s real payload carries."""
    base = {
        "id": assignment_id,
        "status": status,
        "exit_code": 0 if status == "done" else 1,
        "started_at": 1000.0,
        "finished_at": 1004.0,
        "log_path": f"/var/log/{assignment_id}.log",
        "error": None,
    }
    base.update(overrides)
    return base


class TestNotifyDispatchesNoNewLegsWhilePending:
    """The other gap the 2026-08-22 incident exposed: a review dispatch for
    #2540 and a work dispatch for #2541 both landed within the drain's
    first minute, because nothing had ever told `coord notify` a drain was
    in progress. `_agent_status` is patched to report nothing active/
    completed unless a test says otherwise — this is purely about whether
    the NEW-dispatch calls are even reached, not about what they would have
    found."""

    def test_no_marker_dispatches_normally(self, notify_config, coord_db):
        from coord import notify as notify_mod

        assert dq_cmd.read_roll_pending() is None
        with patch.object(notify_mod, "_dispatch_board_pending_smoke") as smoke, \
             patch.object(notify_mod, "_dispatch_board_pending_reviews") as review, \
             patch.object(notify_mod, "_sweep_stalled_pipeline", return_value=[]) as stalled, \
             patch.object(notify_mod, "_agent_status", return_value={"active": [], "completed": []}):
            notify_mod.run(notify_config)
        # #2975: `run()` takes a head-start dispatch pass before its own
        # transition-detection loop (so a slow repo's out-of-band test
        # confirmation can never queue another repo's Test/Review dispatch
        # behind it), then repeats the same two calls in their usual place —
        # two calls per un-gated run, not one.
        assert smoke.call_count == 2
        assert review.call_count == 2
        stalled.assert_called_once()

    def test_a_live_marker_blocks_every_new_dispatch_call(self, notify_config, coord_db):
        from coord import notify as notify_mod

        dq_cmd.write_roll_pending(_pending())
        with patch.object(notify_mod, "_dispatch_board_pending_smoke") as smoke, \
             patch.object(notify_mod, "_dispatch_board_pending_reviews") as review, \
             patch.object(notify_mod, "_sweep_stalled_pipeline", return_value=[]) as stalled, \
             patch.object(notify_mod, "_agent_status", return_value={"active": [], "completed": []}):
            notify_mod.run(notify_config)
        smoke.assert_not_called()
        review.assert_not_called()
        stalled.assert_not_called()

    def test_an_expired_marker_does_not_block_dispatch(self, notify_config, coord_db):
        """Read-only on the marker (see `_roll_pending_blocks_new_dispatch`'s
        docstring) — an EXPIRED one still reads as "not pending" for
        `coord notify`'s own purposes, even though only the drive-queue tick
        actually clears it."""
        from coord import notify as notify_mod

        dq_cmd.write_roll_pending(
            _pending(set_at=time.time() - ROLL_PENDING_DEFAULT_TTL_SECONDS - 60.0)
        )
        with patch.object(notify_mod, "_dispatch_board_pending_smoke") as smoke, \
             patch.object(notify_mod, "_dispatch_board_pending_reviews") as review, \
             patch.object(notify_mod, "_sweep_stalled_pipeline", return_value=[]) as stalled, \
             patch.object(notify_mod, "_agent_status", return_value={"active": [], "completed": []}):
            notify_mod.run(notify_config)
        # #2975: see the head-start comment in test_no_marker_dispatches_normally
        # above — two calls per un-gated run, not one.
        assert smoke.call_count == 2
        assert review.call_count == 2
        stalled.assert_called_once()
        # And read-only, as promised — notify never touches the file itself.
        assert dq_cmd.read_roll_pending() is not None

    def test_completion_detection_and_posting_is_unaffected(self, notify_config, coord_db):
        """Advancing an EXISTING leg (posting a completion comment for work
        already in flight) must keep happening — #2587 only blocks NEW
        dispatch. Reuses the exact `_record`/`_agent_completed` shape
        `tests/test_notify.py::TestRun` already pins."""
        from coord import notify as notify_mod
        from coord.models import Proposal

        proposal = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=42, issue_title="t", rationale="r",
            files_likely=["src/a.py"], briefing="b",
        )
        state.record_dispatched(assignment_id="abc", proposal=proposal, repo_github="acme/api")

        agent_status = {"active": [], "completed": [_agent_completed("abc", "done")]}
        dq_cmd.write_roll_pending(_pending())
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment") as mock_post, \
             patch.object(notify_mod, "_dispatch_board_pending_smoke"), \
             patch.object(notify_mod, "_dispatch_board_pending_reviews"), \
             patch.object(notify_mod, "_sweep_stalled_pipeline", return_value=[]):
            posted, *_ = notify_mod.run(notify_config)
        assert len(posted) == 1
        mock_post.assert_called_once()
