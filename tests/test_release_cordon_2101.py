"""Release cordons: create a propagation window instead of waiting for one
(#2101).

The bug this closes is not subtle and it is not rare: on 2026-08-10 the fleet
sat eleven releases behind PyPI for a day, with elitebook idle and rollable
the entire time, because `coord release propagate` waits for a quiescence
window that a working drive queue never produces. The fix stops waiting and
manufactures the window — cordon, drain, roll, uncordon.

Every test here is written against one of #2101's acceptance criteria, and
each one fails against the pre-fix code (`plan_cordons` and the cordon store
did not exist; `plan_tick` had no `cordons` parameter at all — its 2121 lines
contained zero references to pause of any kind, which is precisely why the
queue walked straight through a cordon).

The traps are deadlocks, not polish, so they get tests of their own:

* **A** — a cordon and an operator pause must not be one flag. If they were,
  the post-roll uncordon would silently clear a deliberate `coord pause`, and
  an operator's `coord unpause` would un-cordon a host mid-drain.
* **B** — a run killed between cordon and roll must not cordon the fleet
  forever. The cordon lives in daemon state and the daemon is restarted by the
  very roll it gates.
* **C** — a host that never drains must escalate loudly rather than wait
  forever, and the escalation must be a MESSAGE, not an internal flag.
* **D** — the `launch_host` attribution that charged every drive to the timer
  host, which is the daemon host, which defers the whole fleet.
* **E** — a cordon nobody can see is a queue that mysteriously stopped.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from coord import machine_pause as mp
from coord import release_cordon as rc
from coord import release_propagate as rp
from coord.cli import main
from coord.commands import release as release_cmd
from coord.drive_queue import (
    STATE_RUNNING,
    STATE_WAITING,
    QueueEntry,
    build_board_view,
    plan_tick,
    render_plan,
)


@pytest.fixture()
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the pause/cordon store — it lives at $HOME/.coord/."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".coord").mkdir()
    return tmp_path


# ══════════════════════════════════════════════════════════════════════════
# Acceptance 2 / trap A: a release cordon and an operator pause are two
# different things, and each clears only its own — in BOTH orders.
# ══════════════════════════════════════════════════════════════════════════


def test_an_operator_unpause_does_not_lift_a_release_cordon(tmp_home):
    """Pause first, then cordon, then unpause.

    If both wrote one flag, `coord unpause` here would un-cordon a host in the
    middle of draining for a release — the fleet would start routing work onto
    a machine that is about to have its agent restarted, which is the
    in-flight-worker massacre the whole quiescence design exists to prevent.
    """
    mp.local_pause("server")
    mp.local_set_cordon("server", target_version="0.5.31")

    outcome = mp.local_unpause_effective("server")

    assert outcome.changed and outcome.kind == "resumed"
    assert "server" not in mp._explicit_paused_set()
    # The cordon survives, and the machine is still not dispatchable.
    assert "server" in mp.cordoned_names()
    assert "server" in mp.local_paused_set()


def test_clearing_a_release_cordon_does_not_lift_an_operator_pause(tmp_home):
    """Cordon first, then pause, then uncordon.

    The mirror image: after the roll lands, propagation uncordons the host —
    and must not thereby resume a machine the operator deliberately took out
    of rotation.
    """
    mp.local_set_cordon("server", target_version="0.5.31")
    mp.local_pause("server")

    assert mp.local_clear_cordon("server") is True

    assert mp.cordoned_names() == set()
    # The hand pause is untouched: still paused, still not dispatchable.
    assert "server" in mp._explicit_paused_set()
    assert "server" in mp.local_paused_set()


def test_a_cordon_does_not_read_as_a_hand_pause(tmp_home):
    """Trap E, at the display layer.

    A cordoned machine IS in `paused_set()` — that is how every dispatcher in
    the fleet honours it without learning a second concept — so a renderer
    that only looks at that set says "PAUSED" for a machine nobody paused and
    no `coord unpause` will free.
    """
    from coord.models import Machine

    machine = Machine(name="server", host="server.tailnet", repos=["api"])
    mp.local_set_cordon("server", target_version="0.5.31")
    paused = mp.local_paused_set([machine])

    state = mp.describe_pause_state(machine, paused, cordons=mp.local_cordons())

    assert state is not None
    assert state.kind == "cordon"
    assert state.detail == "cordoned: draining for v0.5.31"
    # ...and without the cordon map it is indistinguishable from a hand pause,
    # which is exactly why the map is threaded through every surface.
    assert mp.describe_pause_state(machine, paused).kind == "hand"


def test_the_two_stores_survive_each_other_s_writes(tmp_home):
    """The single line that makes trap A true rather than merely intended:
    `_save_state` preserves whichever axes the caller didn't pass."""
    mp.local_pause("laptop")
    mp.local_set_cordon("server", target_version="0.5.31")
    mp.local_unpause_effective("laptop")
    mp.local_pause("server")
    mp.local_set_cordon("laptop", target_version="0.5.31")

    raw = json.loads((tmp_home / ".coord" / "paused_machines.json").read_text())
    assert raw["paused"] == ["server"]
    assert sorted(raw["release_cordons"]) == ["laptop", "server"]


# ══════════════════════════════════════════════════════════════════════════
# Acceptance 3 / trap B: a run killed between cordon and roll must not leave
# a permanent cordon.
# ══════════════════════════════════════════════════════════════════════════


def test_a_cordon_expires_on_its_own_with_nothing_running(tmp_home):
    """Nothing has to run for a cordon to lapse.

    This is the whole design: cleanup is not an ACTION (which a killed process
    would skip) but an absence — the read side simply ignores an expired
    record. A propagate run that dies mid-drain therefore cannot cordon the
    fleet forever, and the daemon being restarted by the very roll it gates
    cannot either.
    """
    written = mp.local_set_cordon("server", target_version="0.5.31", ttl_seconds=600)
    assert "server" in mp.cordoned_names(now=written.renewed_at + 60)

    later = written.expires_at + 1
    assert mp.cordoned_names(now=later) == set()
    assert mp.local_paused_set(now=later) == set()


def test_a_propagate_run_killed_after_cordoning_leaves_an_expiring_cordon(
    tmp_home, valid_config_path, monkeypatch, tmp_path
):
    """Kill the run between cordon and roll, then look at what it left.

    The failure mode being tested for is a fleet that looks quiet — no work
    anywhere, every readout green — because a dead process left every machine
    refusing work. #2082 in a new costume.
    """
    _stub_state_dir(monkeypatch, tmp_path)
    _stub_board(monkeypatch, drive_queue=[], assignments=[])
    _stub_verify(monkeypatch, versions={"laptop": ["0.5.31"], "server": ["0.5.26"]})

    def _die(*_a, **_k):
        raise KeyboardInterrupt("the propagate run was killed mid-roll")

    monkeypatch.setattr(release_cmd, "_roll_python", _die)

    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.5.31", "--no-verify"],
    )
    assert result.exit_code != 0, "the run really did die before uncordoning"

    cordons = mp.local_cordons(include_expired=True)
    assert "server" in cordons, "the run really did cordon before dying"
    stranded = cordons["server"]
    assert stranded.expires_at > stranded.renewed_at, "a cordon with no expiry is a wedge"
    # The fleet frees itself without anyone noticing the run died.
    assert mp.cordoned_names(now=stranded.expires_at + 1) == set()


# ══════════════════════════════════════════════════════════════════════════
# Acceptance 1: the drive-queue tick refuses to launch onto a cordoned host.
# This is the hole that existed today — `coord/drive_queue.py` had zero pause
# awareness, and `coord/drive.py` checks pause only when routing a WORKER.
# ══════════════════════════════════════════════════════════════════════════


def _entry(repo="api", issue=1, position=0, machine="", state=STATE_WAITING, **kw):
    return QueueEntry(
        repo=repo, issue=issue, position=position, machine=machine, state=state, **kw
    )


def _board(**kw):
    return build_board_view({"assignments": [], "issues": [], **kw})


def test_the_tick_refuses_to_launch_when_this_host_is_cordoned():
    plan = plan_tick(
        [_entry(issue=7)],
        _board(),
        capacity=1,
        local_host="dellserver",
        cordons={"dellserver": "cordoned: draining for v0.5.31"},
    )

    assert plan.launch is None
    assert plan.cordon_reason == "cordoned: draining for v0.5.31"
    # Trap E: the queue says WHY it stopped, in the alert an operator reads.
    assert plan.alert is not None
    assert "draining for v0.5.31" in plan.alert.reason
    assert plan.alert.command == "coord release cordon --clear dellserver"
    assert any("draining for v0.5.31" in line for line in render_plan(plan))


def test_a_cordoned_tick_still_reconciles_so_the_host_can_actually_drain():
    """A cordon that also froze the queue's view of reality would re-create
    the #2110 deadlock with its own fix: the finished drive's `running` row
    would never move to `done`, and that stale row alone pins propagation
    indefinitely. A cordoned tick is exactly `--reconcile-only`.
    """
    board = build_board_view(
        {
            "assignments": [],
            "issues": [{"repo_name": "api", "number": 7, "state": "closed"}],
        }
    )
    plan = plan_tick(
        [_entry(issue=7, state=STATE_RUNNING, launch_host="dellserver")],
        board,
        capacity=1,
        local_host="dellserver",
        cordons={"dellserver": "cordoned: draining for v0.5.31"},
    )

    assert plan.launch is None
    assert [r.outcome for r in plan.reconciles] == ["done"]
    assert plan.writes(), "the drained row must still be written back"


def test_the_cordon_matches_a_host_named_with_a_domain_suffix():
    """`coordinator.yml` says `dellserver`; `socket.gethostname()` may say
    `dellserver.local`. A cordon that fails to match on that is a cordon that
    silently does nothing — #1563's failure class, reintroduced."""
    plan = plan_tick(
        [_entry()],
        _board(),
        capacity=1,
        local_host="dellserver.local",
        cordons={"DellServer": "cordoned: draining for v0.5.31"},
    )
    assert plan.launch is None and plan.cordon_reason


def test_an_entry_pinned_to_a_cordoned_machine_defers_and_the_next_one_launches():
    """Per-host, not fleet-wide: one machine draining must not stop the queue
    from launching work bound for a machine that is fine."""
    plan = plan_tick(
        [
            _entry(repo="api", issue=1, position=0, machine="server"),
            _entry(repo="web", issue=2, position=1, machine="laptop"),
        ],
        _board(),
        capacity=1,
        local_host="dellserver",
        cordons={"server": "cordoned: draining for v0.5.31"},
    )

    assert plan.launch is not None and plan.launch.key == "web#2"
    deferred = [d for d in plan.deferrals if d.key == "api#1"]
    assert deferred and deferred[0].cordoned
    assert "draining for a release" in deferred[0].reason


def test_a_cordon_deferral_does_not_raise_the_stalled_queue_alert():
    """A drain lasts minutes and ends by itself. Escalating it on every tick
    is how an alert channel gets muted — the same posture #1972's per-repo
    limit takes."""
    plan = plan_tick(
        [_entry(machine="server")],
        _board(),
        capacity=1,
        local_host="dellserver",
        cordons={"server": "cordoned: draining for v0.5.31"},
    )
    assert plan.launch is None
    assert plan.alert is None
    assert any("release cordon" in line for line in render_plan(plan))


def test_an_uncordoned_tick_launches_exactly_as_before():
    """The whole mechanism is a no-op on a fleet with no cordons."""
    plan = plan_tick([_entry(issue=7)], _board(), capacity=1, local_host="dellserver")
    assert plan.launch is not None and plan.cordon_reason == ""


def test_the_tick_command_launches_nothing_while_this_host_is_cordoned(
    tmp_home, monkeypatch, valid_config_path
):
    """Black box, through the real `coord drive-queue tick`: no subprocess is
    spawned at all while the host is cordoned."""
    from coord.commands import drive_queue as dq_cmd

    monkeypatch.setattr(dq_cmd, "_local_host_id", lambda: "dellserver")
    monkeypatch.setattr(dq_cmd, "_fetch_board_view", lambda: _board())
    monkeypatch.setattr(
        dq_cmd, "list_drive_queue", lambda *a, **k: [], raising=False
    )
    monkeypatch.setattr(
        "coord.state.list_drive_queue",
        lambda *a, **k: [
            {"repo_name": "api", "issue_number": 7, "position": 0,
             "state": STATE_WAITING},
        ],
    )
    monkeypatch.setattr(
        dq_cmd.subprocess, "run",
        lambda *a, **k: pytest.fail("a cordoned host must launch NOTHING"),
    )
    mp.local_set_cordon("dellserver", target_version="0.5.31")

    result = CliRunner().invoke(
        main, ["drive-queue", "tick", "--config", str(valid_config_path)]
    )

    assert result.exit_code == 0, result.output
    assert "cordoned: draining for v0.5.31" in result.output


@pytest.fixture()
def daemon_db(tmp_path: Path) -> Path:
    """An empty on-disk board DB for the daemon under test."""
    import sqlite3

    from coord.db import _ensure_schema

    path = tmp_path / "coord.db"
    conn = sqlite3.connect(str(path))
    try:
        _ensure_schema(conn)
        conn.commit()
    finally:
        conn.close()
    return path


def test_the_daemon_serves_cordons_over_the_same_pause_endpoint(
    daemon_db, valid_config_path, monkeypatch, tmp_path
):
    """A cordon set on a thin client has to reach the daemon that actually
    governs dispatch — the whole reason #1563 made pause daemon-backed, and
    the reason this is buildable at all. Same endpoint, different owner."""
    from starlette.testclient import TestClient

    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    monkeypatch.setenv("HOME", str(tmp_path / "daemon_home"))
    (tmp_path / "daemon_home" / ".coord").mkdir(parents=True)
    app = build_app(SqliteStore(daemon_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        posted = cli.post(
            "/pause",
            json={"machine": "server", "action": "cordon",
                  "target_version": "0.5.31", "ttl_seconds": 600},
        )
        assert posted.status_code == 200
        assert posted.json()["cordoned"] == ["server"]
        # A cordon IS a routing pause — that is how every dispatcher honours
        # it — while staying separately identifiable.
        assert posted.json()["paused"] == ["server"]

        body = cli.get("/pause").json()
        assert body["cordoned"] == ["server"]
        assert body["cordons"][0]["target_version"] == "0.5.31"

        # ...and an operator's `coord unpause` does not lift it.
        cli.post("/pause", json={"machine": "server", "action": "unpause"})
        assert cli.get("/pause").json()["cordoned"] == ["server"]

        cleared = cli.post("/pause", json={"machine": "server", "action": "uncordon"})
        assert cleared.json() == {**cleared.json(), "changed": True}
        assert cli.get("/pause").json()["cordoned"] == []


# ══════════════════════════════════════════════════════════════════════════
# Acceptance 4 / trap C: a host that will not drain escalates VISIBLY.
# ══════════════════════════════════════════════════════════════════════════


def test_a_host_that_does_not_drain_escalates_with_a_message_naming_the_override():
    now = 10_000.0
    stuck = rc.Cordon(
        machine="dellserver", target_version="0.5.31",
        created_at=now - 7200, renewed_at=now - 60, expires_at=now + 600,
    )
    plan = rc.plan_cordons(
        target_version="0.5.31",
        host_versions={"dellserver": "0.5.26"},
        existing={"dellserver": stuck},
        now=now,
        drain_deadline=5400,
        busy_reasons={"dellserver": "live RUNNING assignment: dellserver:2085"},
    )

    assert len(plan.escalations) == 1
    message = plan.escalations[0].message
    assert "DRAIN OVERDUE" in message
    assert "dellserver" in message and "v0.5.31" in message
    # Assert on the SURFACED message, per acceptance 4 — including what is
    # holding it and the documented override.
    assert "live RUNNING assignment: dellserver:2085" in message
    assert "coord release cordon --clear dellserver" in message
    # Escalating does not silently un-cordon: the host really is behind.
    assert [c.machine for c in plan.cordon] == ["dellserver"]


def test_a_renewal_does_not_reset_the_drain_deadline():
    """Otherwise a wedged host postpones its own escalation forever, once
    every 20 minutes, and the deadline never fires."""
    now = 10_000.0
    first = rc.plan_cordons(
        target_version="0.5.31", host_versions={"h": "0.5.26"},
        existing={}, now=now - 7200,
    ).cordon[0]
    # Still live at `now` (the timer renewed it every 20 minutes all along).
    live = rc.Cordon(**{**first.to_dict(), "expires_at": now + 600})
    renewed = rc.plan_cordons(
        target_version="0.5.31", host_versions={"h": "0.5.26"},
        existing={"h": live}, now=now,
    ).cordon[0]

    assert renewed.created_at == first.created_at
    assert renewed.renewed_at == now
    assert renewed.overdue(now, 5400)


def test_the_drain_escalation_reaches_stderr_and_the_escalation_table(
    tmp_home, valid_config_path, monkeypatch, tmp_path
):
    _stub_state_dir(monkeypatch, tmp_path)
    _stub_board(
        monkeypatch,
        assignments=[{"machine_name": "server", "issue_number": 9, "status": "RUNNING"},
                     {"machine_name": "laptop", "issue_number": 8, "status": "RUNNING"}],
    )
    _stub_verify(monkeypatch, versions={"laptop": ["0.5.31"], "server": ["0.5.26"]})
    recorded: list[dict] = []
    monkeypatch.setattr(
        "coord.state.record_drive_escalation",
        lambda repo, issue, **kw: recorded.append({"repo": repo, **kw}),
    )
    # #2373: `_apply_cordons` now POSTs to the escalated host's own agent
    # before escalating — stub it so this test stays hermetic (no real DNS
    # lookup against "server.tailnet") and asserts nothing about it; the
    # dedicated #2373 test below covers that call itself.
    monkeypatch.setattr(release_cmd, "_post", lambda *a, **k: (None, {}, "unreachable (test)"))
    # A cordon set two hours ago that has never drained.
    import time as _time

    mp.local_set_cordon(
        "server", target_version="0.5.31",
        created_at=_time.time() - 7200, ttl_seconds=3600,
    )

    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.5.31"],
    )

    assert "DRAIN OVERDUE" in result.output
    assert recorded and "DRAIN OVERDUE" in recorded[0]["reason"]
    assert recorded[0]["proposed_command"] == "coord release cordon --clear server"


# ══════════════════════════════════════════════════════════════════════════
# Trap D: the `launch_host` attribution that charged every drive to the
# timer host — which is the daemon host, which defers the whole fleet.
# ══════════════════════════════════════════════════════════════════════════


def test_a_drive_pinned_elsewhere_no_longer_pins_the_timer_host(tmp_home):
    """Measured on 2026-08-10: `launch_host='dellserver'` for a drive whose
    worker ran on precision, so dellserver — the daemon host, which nothing
    may roll ahead of — read as busy for the life of every drive on the
    fleet. Any drive anywhere pinned the entire fleet from rolling."""
    q = rp.assess_quiescence(
        queue_entries=[
            {"repo_name": "coord-portal", "issue_number": 53, "state": STATE_RUNNING,
             "machine": "precision", "launch_host": "dellserver"},
        ],
    )
    assert q.rollable_hosts(["dellserver", "elitebook"]) == ["dellserver", "elitebook"]
    assert q.busy_hosts() == {"precision"}


# ══════════════════════════════════════════════════════════════════════════
# Trap F: the trigger is coupled to release frequency, so it is a knob.
# ══════════════════════════════════════════════════════════════════════════


def test_any_drift_cordons_by_default():
    plan = rc.plan_cordons(
        target_version="0.5.31", host_versions={"h": "0.5.30"}, now=0.0
    )
    assert [c.machine for c in plan.cordon] == ["h"]


def test_a_raised_threshold_tolerates_a_small_drift():
    plan = rc.plan_cordons(
        target_version="0.5.31", host_versions={"h": "0.5.30"}, now=0.0, threshold=3
    )
    assert plan.cordon == ()


def test_a_host_whose_version_cannot_be_read_is_never_cordoned():
    """Cordoning stops real work. Doing that on a guess is the failure this
    fleet keeps repeating — and an unreadable lane is emphatically not
    evidence of agreement either (#1834), so an EXISTING cordon on such a
    host is left exactly as it is rather than cleared."""
    stuck = rc.Cordon(machine="h", created_at=0.0, expires_at=1e12)
    plan = rc.plan_cordons(
        target_version="0.5.31",
        host_versions={"h": None},
        existing={"h": stuck},
        now=1.0,
    )
    assert plan.cordon == () and plan.uncordon == ()
    assert plan.unknown == ("h",)


def test_turning_the_mechanism_off_releases_the_fleet():
    """`--no-cordon` must not freeze the fleet in whatever state the last run
    left behind."""
    plan = rc.plan_cordons(
        target_version="0.5.31",
        host_versions={"h": "0.5.26"},
        existing={"h": rc.Cordon(machine="h", expires_at=1e12)},
        now=1.0,
        enabled=False,
    )
    assert plan.cordon == () and plan.uncordon == ("h",)


def test_a_host_proven_current_is_uncordoned():
    plan = rc.plan_cordons(
        target_version="0.5.31",
        host_versions={"h": "0.5.31"},
        existing={"h": rc.Cordon(machine="h", expires_at=1e12)},
        now=1.0,
    )
    assert plan.uncordon == ("h",)


# ══════════════════════════════════════════════════════════════════════════
# Acceptance 5: end to end — a drive running and a newer version on the
# index; the fleet cordons, the drive finishes, the host rolls, uncordons,
# and the queue resumes launching.
# ══════════════════════════════════════════════════════════════════════════


def _stub_state_dir(monkeypatch, tmp_path):
    d = tmp_path / "propagation-state"
    d.mkdir(exist_ok=True)
    monkeypatch.setattr(release_cmd, "_state_dir", lambda: d)
    return d


def _stub_board(monkeypatch, *, drive_queue=(), assignments=(), issues=()):
    monkeypatch.setattr(
        release_cmd,
        "_fetch_board",
        lambda: (
            {
                "drive_queue": list(drive_queue),
                "assignments": list(assignments),
                "issues": list(issues),
            },
            None,
        ),
    )


def _serve_health(name):
    """A ``/health`` body whose ``spawned_coord`` rows name a live coord-serve
    — how the daemon host is DERIVED rather than guessed (#2052 fault 2)."""
    return {
        "version": "0.5.31",
        "health": {"schema": 1, "results": [
            {"check_id": "spawned_coord", "subject": "coord-serve",
             "severity": "ok",
             "values": {"unit": "coord-serve", "pid": 1, "version": "0.5.31"}},
        ]},
    }


def _stub_verify(monkeypatch, *, versions, daemon="server", findings=None):
    from coord import release_verify as rv

    lanes = [
        rv.Lane(host=host, lane="~/.coord-venv", version=v)
        for host, vs in versions.items()
        for v in vs
    ]
    machine_health = {daemon: _serve_health(daemon)} if daemon else {}
    monkeypatch.setattr(
        rv, "gather", lambda *a, **k: (machine_health, {}, None, daemon or "daemon")
    )
    monkeypatch.setattr(
        rv,
        "verify",
        lambda **kwargs: rv.VerifyReport(
            expected=kwargs.get("expected"), lanes=lanes, findings=list(findings or [])
        ),
    )


def test_end_to_end_cordon_drain_roll_uncordon_resume(
    tmp_home, valid_config_path, monkeypatch, tmp_path
):
    """The loop, in one test.

    `server` is the daemon host AND the host running the drive, which is the
    exact shape that used to defer the whole fleet forever: nothing may roll
    ahead of the daemon (the documented 405), and the daemon is never idle
    because the queue relaunches every three minutes.
    """
    state_dir = _stub_state_dir(monkeypatch, tmp_path)
    rolled: list[str] = []
    monkeypatch.setattr(
        release_cmd,
        "_roll_python",
        lambda machine, **kw: (rolled.append(machine.name), (True, "rolled", True))[1],
    )

    # ── tick 1: a drive is running on `server`; `server` is behind ───────
    _stub_board(
        monkeypatch,
        drive_queue=[{"repo_name": "api", "issue_number": 7,
                      "state": STATE_RUNNING, "machine": "server",
                      "launch_host": "server"}],
    )
    _stub_verify(monkeypatch, versions={"laptop": ["0.5.31"], "server": ["0.5.26"]})

    first = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.5.31"],
    )
    assert first.exit_code == 0, first.output
    assert rolled == [], "nothing may roll while the daemon host is busy"
    assert "server" in mp.cordoned_names(), "...but the drain must have STARTED"
    assert "cordon server" in first.output

    # ── the queue on that host now refuses to start anything new ─────────
    stopped = plan_tick(
        [_entry(issue=8)],
        _board(),
        capacity=1,
        local_host="server",
        cordons={n: c.describe() for n, c in mp.local_cordons().items()},
    )
    assert stopped.launch is None and stopped.cordon_reason

    # ── tick 2: the drive finished, so the host has drained ──────────────
    _stub_board(monkeypatch, drive_queue=[])
    second = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.5.31", "--no-verify"],
    )
    assert second.exit_code == 0, second.output
    assert "server" in rolled, "the drained host must roll"
    # ...and be released the moment it does.
    assert mp.cordoned_names() == set()
    assert "uncordon server" in second.output

    # ── the queue resumes ────────────────────────────────────────────────
    resumed = plan_tick(
        [_entry(issue=8)],
        _board(),
        capacity=1,
        local_host="server",
        cordons={n: c.describe() for n, c in mp.local_cordons().items()},
    )
    assert resumed.launch is not None and resumed.launch.key == "api#8"

    # The whole sequence is readable after the fact, which is what makes a
    # quiet night distinguishable from a wedged one (#1835's fourth criterion).
    records = rp.read_records(state_dir)
    assert records[0]["cordons"]["cordoned"] == ["server"]
    assert records[-1]["cordons"]["uncordoned"] == ["server"]


# ══════════════════════════════════════════════════════════════════════════
# #2373: a launch-host-only liveness ambiguity must not wedge the drain
# forever — the escalated host's own agent is asked to resolve it first.
# ══════════════════════════════════════════════════════════════════════════
#
# Live incident, 2026-08-18 (claude-coordinator#2360): a drive-queue entry
# launched on elitebook — a non-daemon host that runs only
# `coord-agent.service`, no `coord-drive-queue.timer` — sat `running` for
# ~17h. Every tick from the daemon host (dellserver) correctly refused to
# declare it dead (#1870's cross-host guard, `local_host != launch_host`),
# so it never left `running`, the cordon elitebook picked up for being
# behind kept renewing on every propagate tick that found it "still busy",
# and the drain-deadline escalation fired over and over with nothing ever
# resolving the underlying ambiguity — because the only host that COULD
# resolve it never ticks its own queue. Running `coord drive-queue tick
# --reconcile-only` locally ON elitebook resolved it in one call (the entry
# moved to `parked`, waiting on a CI re-check, #1891 — not actually dead).


def test_a_behind_hosts_agent_is_asked_to_reconcile_before_the_drain_escalates(
    tmp_home, valid_config_path, monkeypatch, tmp_path
):
    """The exact incident shape: `laptop` is behind, not the daemon, and is
    where the wedged `running` entry actually lives. Asserts `coord release
    propagate` POSTs to `laptop`'s own agent — not the daemon's, not
    anywhere else — before the loud DRAIN OVERDUE message goes out, and
    folds the self-heal's own outcome into that same message/record rather
    than requiring a human to SSH in and run the command by hand.
    """
    _stub_state_dir(monkeypatch, tmp_path)
    _stub_board(
        monkeypatch,
        drive_queue=[{"repo_name": "api", "issue_number": 42,
                      "state": STATE_RUNNING, "machine": "laptop",
                      "launch_host": "laptop"}],
    )
    # `server` is the daemon and already current; `laptop` is behind and is
    # where the wedged entry actually launched — the shape #1870's guard
    # exists for: only `laptop`'s OWN tick can tell this entry's true state.
    _stub_verify(monkeypatch, versions={"laptop": ["0.5.26"], "server": ["0.5.31"]})

    posts: list[str] = []

    def fake_post(url, payload, *, timeout):
        posts.append(url)
        if url.endswith("/drive-queue-reconcile"):
            return 200, {"ok": True, "detail": "moved to parked (#1891)"}, ""
        return 200, {}, ""

    monkeypatch.setattr(release_cmd, "_post", fake_post)

    recorded: list[dict] = []
    monkeypatch.setattr(
        "coord.state.record_drive_escalation",
        lambda repo, issue, **kw: recorded.append({"repo": repo, **kw}),
    )

    import time as _time

    mp.local_set_cordon(
        "laptop", target_version="0.5.31",
        created_at=_time.time() - 7200, ttl_seconds=3600,
    )

    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.5.31"],
    )

    assert "DRAIN OVERDUE" in result.output
    # Reached laptop's own agent specifically — the #1870 guard's contract
    # is that only the launch host itself can resolve this, so asking any
    # other machine would have been pointless.
    reconcile_calls = [u for u in posts if u.endswith("/drive-queue-reconcile")]
    assert reconcile_calls == ["http://laptop.tailnet:7433/drive-queue-reconcile"]
    # The outcome is folded into the SAME escalation surfaced to stderr...
    assert (
        "asked laptop's own agent to run a local reconcile-only tick first "
        "(#2373): ok (moved to parked (#1891))" in result.output
    )
    # ...and into the escalation table's own record — `coord drive
    # escalations` shows the self-heal attempt with no second alert to
    # correlate it against.
    assert recorded and "reconcile-only tick" in recorded[0]["reason"]
    assert "remote_reconcile=ok" in recorded[0]["gate_readings"]


def test_the_remote_reconcile_failing_still_lets_the_drain_escalation_through(
    tmp_home, valid_config_path, monkeypatch, tmp_path
):
    """An unreachable agent must not turn a real drain escalation into
    silence — the loud message still goes out exactly as before #2373, with
    the failed self-heal attempt folded in rather than hidden."""
    _stub_state_dir(monkeypatch, tmp_path)
    _stub_board(
        monkeypatch,
        drive_queue=[{"repo_name": "api", "issue_number": 42,
                      "state": STATE_RUNNING, "machine": "laptop",
                      "launch_host": "laptop"}],
    )
    _stub_verify(monkeypatch, versions={"laptop": ["0.5.26"], "server": ["0.5.31"]})
    monkeypatch.setattr(
        release_cmd, "_post",
        lambda *a, **k: (None, {}, "ConnectError: [Errno 111] Connection refused"),
    )

    import time as _time

    mp.local_set_cordon(
        "laptop", target_version="0.5.31",
        created_at=_time.time() - 7200, ttl_seconds=3600,
    )

    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.5.31"],
    )

    assert "DRAIN OVERDUE" in result.output
    assert "reconcile-only tick first (#2373): failed" in result.output
    assert "Connection refused" in result.output


def test_reconcile_launch_host_forwards_a_remote_timeout_derived_from_its_own(monkeypatch):
    """Review fix (#2373): the POST body used to be `{}`, so the remote
    agent's own subprocess timeout defaulted to 120s
    (`AgentServer.reconcile_drive_queue`) while this call's own HTTP wait
    was only `DEFAULT_RECONCILE_TIMEOUT_SECONDS` (30s) — a tick that
    legitimately took, say, 45s would report here as a misleading
    `ok=False, detail="unreachable: ..."` even though it was still running
    and would resolve correctly on its own. The remote timeout must now be
    forwarded, and kept under this call's own HTTP timeout so the agent has
    room to marshal and return its response first."""
    posts = []

    def fake_post(url, payload, *, timeout):
        posts.append((url, payload, timeout))
        return 200, {"ok": True, "detail": "moved to parked"}, ""

    ok, detail = release_cmd._reconcile_launch_host(
        "laptop.tailnet", agent_port=7433, timeout=30.0, post=fake_post,
    )

    assert (ok, detail) == (True, "moved to parked")
    [(url, payload, http_timeout)] = posts
    assert url == "http://laptop.tailnet:7433/drive-queue-reconcile"
    assert http_timeout == 30.0
    assert payload["timeout"] < http_timeout
    assert payload["timeout"] == pytest.approx(
        30.0 - release_cmd._RECONCILE_TIMEOUT_MARGIN_SECONDS
    )


# ══════════════════════════════════════════════════════════════════════════
# #2595: a cordoned host that is ALSO idle is a whole machine silently
# pulled from the fleet, not a normal in-progress drain (`precision`, 22
# releases behind and cordoned — found only by hand). `coord status`,
# `coord doctor` and the `release_cordon_idle` health check all render this
# off ONE pure decision, tested here directly.
# ══════════════════════════════════════════════════════════════════════════


def test_idle_overdue_cordons_fires_on_a_cordoned_idle_host_past_deadline():
    now = 10_000.0
    stuck = rc.Cordon(
        machine="precision", target_version="0.5.232",
        created_at=now - 7200, renewed_at=now - 60, expires_at=now + 600,
    )

    found = rc.idle_overdue_cordons(
        {"precision": stuck}, now=now, idle_hosts={"precision"},
        deadline=5400, host_versions={"precision": "0.5.210"},
    )

    assert len(found) == 1
    overdue = found[0]
    assert overdue.machine == "precision"
    assert overdue.drift == 22
    assert "precision" in overdue.message
    assert "cordoned and IDLE" in overdue.message
    assert "22 releases behind" in overdue.message
    assert "coord agent update --machine precision" in overdue.message
    assert "coord release cordon --clear precision" in overdue.message


def test_idle_overdue_cordons_ignores_a_host_still_busy():
    """A cordoned host with active work is draining normally — the whole
    point of #2595 is distinguishing that from a host with nothing left to
    drain at all."""
    now = 10_000.0
    stuck = rc.Cordon(
        machine="dellserver", target_version="0.5.232",
        created_at=now - 7200, renewed_at=now - 60, expires_at=now + 600,
    )

    assert rc.idle_overdue_cordons(
        {"dellserver": stuck}, now=now, idle_hosts=set(), deadline=5400,
    ) == ()


def test_idle_overdue_cordons_ignores_an_idle_host_within_deadline():
    """Idle + cordoned + NOT yet overdue is a normal drain in progress."""
    now = 10_000.0
    fresh = rc.Cordon(
        machine="precision", target_version="0.5.232",
        created_at=now - 60, renewed_at=now - 60, expires_at=now + 3540,
    )

    assert rc.idle_overdue_cordons(
        {"precision": fresh}, now=now, idle_hosts={"precision"}, deadline=5400,
    ) == ()


def test_idle_overdue_cordons_ignores_an_expired_cordon():
    now = 10_000.0
    expired = rc.Cordon(
        machine="precision", target_version="0.5.232",
        created_at=now - 7200, renewed_at=now - 7200, expires_at=now - 1,
    )

    assert rc.idle_overdue_cordons(
        {"precision": expired}, now=now, idle_hosts={"precision"}, deadline=5400,
    ) == ()


def test_idle_overdue_cordons_never_fabricates_a_drift_count():
    """No `host_versions` given -> `drift` stays `None`, never rendered as a
    misleading "0 releases behind"."""
    now = 10_000.0
    stuck = rc.Cordon(
        machine="precision", target_version="0.5.232",
        created_at=now - 7200, renewed_at=now - 60, expires_at=now + 600,
    )

    [overdue] = rc.idle_overdue_cordons(
        {"precision": stuck}, now=now, idle_hosts={"precision"}, deadline=5400,
    )

    assert overdue.drift is None
    assert "behind" not in overdue.message


def test_idle_overdue_cordons_renders_cross_series_drift_as_major_version_behind():
    """`version_drift()` returns `CROSS_SERIES_DRIFT` (9999) for a cross-major/
    minor jump (e.g. 0.4.x -> 0.5.x, a transition this fleet's own version
    history has actually made) — the raw sentinel must never be rendered
    verbatim as "9999 releases behind" in the CRIT text (#2595 review)."""
    now = 10_000.0
    stuck = rc.Cordon(
        machine="precision", target_version="0.5.0",
        created_at=now - 7200, renewed_at=now - 60, expires_at=now + 600,
    )

    [overdue] = rc.idle_overdue_cordons(
        {"precision": stuck}, now=now, idle_hosts={"precision"}, deadline=5400,
        host_versions={"precision": "0.4.111"},
    )

    assert overdue.drift == rc.CROSS_SERIES_DRIFT
    assert "9999" not in overdue.message
    assert "major/minor version behind" in overdue.message


# ══════════════════════════════════════════════════════════════════════════
# #2595: `DRAIN OVERDUE` reaches the notifier, not just `coord release
# propagate`'s own stderr/journal — same live cordon store `coord status`/
# `coord doctor` read, same `DrainEscalation` wording `_escalate_drain`
# already prints.
# ══════════════════════════════════════════════════════════════════════════


def test_the_notifier_collector_receives_an_overdue_drain_escalation(tmp_home):
    import time as _time

    from coord.notifier import collect

    mp.local_set_cordon(
        "precision", target_version="0.5.232",
        created_at=_time.time() - 7200, ttl_seconds=3600,
    )

    found = collect.drain_overdue(now=_time.time())

    assert len(found) == 1
    halted = found[0]
    assert halted.repo == "(release-cordon:precision)"
    assert halted.urgent is True
    assert "DRAIN OVERDUE" in halted.reason
    assert "precision" in halted.reason
    assert "v0.5.232" in halted.reason


def test_the_notifier_collector_is_silent_with_no_cordon_at_all(tmp_home):
    from coord.notifier import collect

    assert collect.drain_overdue(now=10_000.0) == []


def test_the_notifier_collector_is_silent_for_a_cordon_within_its_deadline(tmp_home):
    import time as _time

    from coord.notifier import collect

    mp.local_set_cordon(
        "precision", target_version="0.5.232", created_at=_time.time(),
    )

    assert collect.drain_overdue(now=_time.time()) == []


def test_drain_overdue_flows_through_the_full_collector(tmp_home):
    """Not just the standalone helper — the exact `PipelineSnapshot` field
    `coord notifier` evaluates every tick."""
    import time as _time
    import types

    from coord.notifier import collect
    from coord.notifier.store import NotifierState

    mp.local_set_cordon(
        "precision", target_version="0.5.232",
        created_at=_time.time() - 7200, ttl_seconds=3600,
    )

    class _Cfg:
        machines: list = []
        notifications = types.SimpleNamespace(web_base_url=None)

    snapshot = collect.collect(_Cfg(), now=_time.time(), notifier_state=NotifierState())

    assert any(h.repo == "(release-cordon:precision)" for h in snapshot.halted)


# ══════════════════════════════════════════════════════════════════════════
# #2595: "outside propagation's reach" CRIT advisories (#2403's remedy line)
# reach nowhere but the timer's own stderr today — `rp.latest_crit_advisories`
# is the read side that lets `coord status` show the newest run's findings
# without re-deriving anything.
# ══════════════════════════════════════════════════════════════════════════


def _record(*, gate=None) -> dict:
    return {"started_at": 1.0, "target_version": "0.5.232", "gate": gate}


def test_latest_crit_advisories_reads_only_the_newest_record():
    older = _record(gate={"advisory": [
        {"host": "precision", "lane": "coord-agent spawns (precision)",
         "severity": "crit", "summary": "stale"},
    ]})
    newest = _record(gate={"advisory": [
        {"host": "dellserver", "lane": "~/.coord-venv (dellserver)",
         "severity": "crit", "summary": "on 0.5.210, expected 0.5.232"},
    ]})

    found = rp.latest_crit_advisories([older, newest])

    assert [f["host"] for f in found] == ["dellserver"]


def test_latest_crit_advisories_drops_warn_and_ok_findings():
    record = _record(gate={"advisory": [
        {"host": "a", "lane": "l", "severity": "warn", "summary": "s"},
        {"host": "b", "lane": "l", "severity": "crit", "summary": "s"},
        {"host": "c", "lane": "l", "severity": "ok", "summary": "s"},
    ]})

    found = rp.latest_crit_advisories([record])

    assert [f["host"] for f in found] == ["b"]


def test_latest_crit_advisories_degrades_to_empty_on_missing_or_malformed_data():
    assert rp.latest_crit_advisories([]) == []
    assert rp.latest_crit_advisories([_record(gate=None)]) == []
    assert rp.latest_crit_advisories([_record(gate={"advisory": "nonsense"})]) == []
    assert rp.latest_crit_advisories([{"started_at": 1.0}]) == []
