"""Black-box tests for `coord release propagate` / `history` (#1835, PKG-7).

These drive the *running command* through Click and assert on what it
printed and what it wrote to the journal — the CLAUDE.md bar for a
behaviour-changing PR. The pure judgement is covered in
`tests/test_release_propagate.py`; what is tested here is the wiring that
only exists in the shell, and the two behaviours a timer depends on:

* **A busy fleet is exit 0, with a record.** This command runs unattended
  every 20 minutes and defers most of the time. If a deferral exited
  non-zero, systemd would mark the unit failed and an operator would learn
  to ignore it — and the one night it genuinely broke would look identical.

* **Every attempt is journalled.** #1835: "a silent success is
  indistinguishable from a silent no-op, which is precisely how 2026-08-04
  stayed invisible."

Nothing here touches a real fleet: the board fetch, the PyPI lookup and the
per-host HTTP calls are all seams.
"""

from __future__ import annotations

import inspect
import json

import pytest
from click.testing import CliRunner

from coord import machine_pause as mp
from coord import release_propagate as rp
from coord.cli import main
from coord.commands import drive_queue as dq_cmd
from coord.commands import release as release_cmd
from coord.config import load as load_config
from coord.drive_queue import HOLD_FIRED, STATE_RUNNING

# Captured at import time, before any fixture has a chance to monkeypatch
# `release_cmd._interactive_session_busy` (the `no_network` fixture below
# stubs it to `lambda config: []` for every test by default) — the one test
# that needs the REAL function (`test_a_failing_session_probe_does_not_defer
# _the_fleet`) restores this reference rather than re-importing, which would
# just re-read whatever monkeypatch currently has installed.
_REAL_INTERACTIVE_SESSION_BUSY = release_cmd._interactive_session_busy


@pytest.fixture(autouse=True)
def _own_pause_store(tmp_path, monkeypatch):
    """Give every test in this module its own pause store (#2174).

    `coord release propagate` both READS the pause store (#2174's
    `_paused_machine_busy`) and WRITES it (#2101's cordons), and several
    tests below seed it with `mp.local_pause()` / `mp.local_set_cordon()`.
    That store is per-`$HOME`, not per-test — so without this, one test's
    pause leaks into every test that runs after it in the same session.

    `conftest._no_real_pause_store` does not cover this: it redirects only
    when the resolved path lands under the REAL home, on the assumption
    that a redirected `$HOME` was redirected BY THE TEST, per test. That
    assumption is false under `scripts/run_tests_in_populated_home.sh`
    (#2170), where `$HOME` is one throwaway thin-client directory shared by
    the WHOLE run: the guard stands down and the store becomes session
    state. That is exactly how this module went green on `ubuntu-latest`
    and red on the `populated-home` job — a phantom `machine paused:
    server` in tests that never paused anything, deferring rolls that
    should have proceeded.

    Redirecting `$HOME` (rather than patching `_state_path`) is what every
    other pause-touching module here already does — `test_machine_pause`,
    `test_quiet_hours`, `test_release_cordon_2101`,
    `test_release_cordon_deadlock_2240` — and it also pins the thin-client
    decision: a tmp `$HOME` has no `client.toml`, so this module resolves
    board-service the same way in both CI jobs instead of inheriting
    whichever shape the ambient home happens to have.
    """
    home = tmp_path / "home"
    (home / ".coord").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    """Point the propagation journal at a tmp dir, never the real ~/.coord."""
    d = tmp_path / "state"
    d.mkdir()
    monkeypatch.setattr(release_cmd, "_state_dir", lambda: d)
    return d


@pytest.fixture()
def no_network(monkeypatch):
    """No PyPI lookup, no /board read, no agent POST, no tmux/ssh session
    probe unless a test says so.

    #2228: `_interactive_session_busy` is real network I/O (an ssh probe
    per configured machine) — `valid_config_path` names hosts
    (`laptop.tailnet`/`server.tailnet`) that don't exist, so without this
    every test below would pay a real (if fast-failing) ssh attempt per
    machine.  Tests that actually exercise the seam override it back.
    """
    monkeypatch.setattr(release_cmd, "_fetch_board", lambda: ({}, None))
    monkeypatch.setattr(
        release_cmd, "_post",
        lambda *a, **k: pytest.fail("no test should POST without saying so"),
    )
    monkeypatch.setattr(release_cmd, "_interactive_session_busy", lambda config: [])


def _records(state_dir):
    return rp.read_records(state_dir)


# ── deferral: the common case, and the one a timer depends on ────────────


def test_a_busy_fleet_defers_at_exit_zero(valid_config_path, state_dir, no_network,
                                          monkeypatch):
    monkeypatch.setattr(
        release_cmd, "_fetch_board",
        lambda: ({"drive_queue": [{"repo_name": "api", "issue_number": 7,
                                   "state": STATE_RUNNING}],
                  "assignments": []}, None),
    )
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111"],
    )
    assert result.exit_code == 0, result.output
    assert "deferred" in result.output
    # The reason names the entry — a deferral nobody can explain is
    # indistinguishable from a wedged timer.
    assert "api#7" in result.output


def test_a_deferral_is_journalled(valid_config_path, state_dir, no_network, monkeypatch):
    """#2067: a deferral is per-host now — a single busy host no longer
    defers the whole run (see the tests below), so this exercises the case
    that genuinely must still defer everything: EVERY configured host
    (`laptop` and `server`, per `valid_config_path`) is occupied."""
    monkeypatch.setattr(
        release_cmd, "_fetch_board",
        lambda: ({"assignments": [{"machine_name": "laptop", "issue_number": 9,
                                   "status": "RUNNING"},
                                  {"machine_name": "server", "issue_number": 10,
                                   "status": "RUNNING"}]}, None),
    )
    CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "v0.4.111"],
    )
    records = _records(state_dir)
    assert len(records) == 1
    assert records[0]["status"] == rp.STATUS_DEFERRED
    assert records[0]["target_version"] == "0.4.111"  # leading v normalised
    assert not records[0]["quiescence"]["quiescent"]


def test_an_unreadable_board_defers_rather_than_crashing(valid_config_path, state_dir,
                                                         no_network, monkeypatch):
    """The safe move when we cannot prove the fleet is idle is to do nothing
    and say so — never to assume idle and start restarting agents."""
    monkeypatch.setattr(
        release_cmd, "_fetch_board", lambda: ({}, "ConnectError: refused")
    )
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111"],
    )
    assert result.exit_code == 0, result.output
    assert "board unreadable" in result.output


def test_no_resolvable_target_fails_loudly(valid_config_path, state_dir, no_network,
                                           monkeypatch):
    monkeypatch.setattr(
        release_cmd, "_resolve_expected", lambda *a, **k: (None, "PyPI unreachable")
    )
    result = CliRunner().invoke(
        main, ["release", "propagate", "--config", str(valid_config_path)]
    )
    assert result.exit_code == 1
    assert _records(state_dir)[0]["status"] == rp.STATUS_FAILED


# ── dry run: the plan, without touching a host ───────────────────────────


def test_a_dry_run_prints_the_plan_and_writes_nothing(valid_config_path, state_dir,
                                                      no_network, monkeypatch):
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]})
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "would roll" in result.output
    assert "[dry-run]" in result.output
    # A dry run must not append to the journal — otherwise a rehearsal is
    # indistinguishable from the real thing in the history.
    assert _records(state_dir) == []


def test_a_dry_run_puts_the_daemon_host_first(valid_config_path, state_dir, no_network,
                                              monkeypatch):
    """The 405 invariant, visible end to end: `server` is the daemon host, so
    its python lane must be the first thing the plan names."""
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]})
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--dry-run", "--daemon-host", "server",
         "--lane", "python"],
    )
    lines = [l for l in result.output.splitlines() if "would roll" in l]
    assert lines
    assert "server" in lines[0]


def test_hosts_already_on_the_target_are_reported_not_rolled(valid_config_path,
                                                             state_dir, no_network,
                                                             monkeypatch):
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.111"], "server": ["0.4.110"]})
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--dry-run", "--lane", "python"],
    )
    assert "already on v0.4.111" in result.output
    assert "laptop" not in "\n".join(
        l for l in result.output.splitlines() if "would roll" in l
    )


def test_a_fleet_already_on_the_target_is_up_to_date(valid_config_path, state_dir,
                                                     no_network, monkeypatch):
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.111"], "server": ["0.4.111"]})
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111"],
    )
    assert result.exit_code == 0, result.output
    assert rp.STATUS_UP_TO_DATE in result.output


# ── #2110: a stale `running` row must not defer the roll ──────────────────
#
# The exact 2026-08-10 incident, reproduced through the real CLI: a
# drive-queue row still reads `running` for an issue that has since closed
# and whose PR has merged — the reconciler that would normally have caught
# this lives inside `coord drive-queue tick`, and the timer can be stopped
# (that is the whole scenario `docs/AGENT_OPERATIONS.md` documents). Before
# #2110 this deferred every run, forever, on a row describing work that
# ended hours ago. It must not anymore.


def test_a_stale_running_row_does_not_defer_and_is_surfaced(
    valid_config_path, state_dir, no_network, monkeypatch
):
    monkeypatch.setattr(
        release_cmd, "_fetch_board",
        lambda: (
            {
                "drive_queue": [
                    {"repo_name": "api", "issue_number": 7,
                     "state": STATE_RUNNING, "launch_host": "server"},
                ],
                "assignments": [
                    {"repo_name": "api", "issue_number": 7, "type": "work",
                     "status": "merged"},
                ],
                "issues": [
                    {"repo_name": "api", "number": 7, "state": "closed"},
                ],
            },
            None,
        ),
    )
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.111"], "server": ["0.4.111"]})
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111"],
    )
    assert result.exit_code == 0, result.output
    assert rp.STATUS_DEFERRED not in result.output
    assert rp.STATUS_UP_TO_DATE in result.output
    # Surfaced, not silently dropped — the operator can see the fleet
    # self-corrected a stale row instead of it just quietly not blocking.
    assert "stale" in result.output
    assert "api#7" in result.output
    records = _records(state_dir)
    assert records[-1]["quiescence"]["stale"] == ["api#7"]


# ── history ──────────────────────────────────────────────────────────────


def test_history_of_an_empty_journal_names_the_timer(state_dir):
    result = CliRunner().invoke(main, ["release", "history"])
    assert result.exit_code == 0
    assert "no propagation attempts recorded" in result.output


def test_history_renders_what_propagate_wrote(valid_config_path, state_dir, no_network,
                                              monkeypatch):
    monkeypatch.setattr(
        release_cmd, "_fetch_board",
        lambda: ({"drive_queue": [{"repo_name": "api", "issue_number": 7,
                                   "state": STATE_RUNNING}]}, None),
    )
    CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111"],
    )
    result = CliRunner().invoke(main, ["release", "history"])
    assert result.exit_code == 0
    assert "api#7" in result.output


def test_history_json_is_machine_readable(valid_config_path, state_dir, no_network,
                                          monkeypatch):
    monkeypatch.setattr(release_cmd, "_fetch_board",
                        lambda: ({}, "ConnectError: refused"))
    CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111"],
    )
    result = CliRunner().invoke(main, ["release", "history", "--json"])
    payload = json.loads(result.output)
    assert payload[0]["status"] == rp.STATUS_DEFERRED


def test_propagate_json_output_is_the_record(valid_config_path, state_dir, no_network,
                                             monkeypatch):
    monkeypatch.setattr(release_cmd, "_fetch_board",
                        lambda: ({}, "ConnectError: refused"))
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--json"],
    )
    payload = json.loads(result.output)
    assert payload["target_version"] == "0.4.111"
    assert payload["status"] == rp.STATUS_DEFERRED


# ── a fired deploy gate is a window, not a blocker ───────────────────────


def test_a_fired_deploy_gate_does_not_defer(valid_config_path, state_dir, no_network,
                                            monkeypatch):
    """#1757's gate stops the queue waiting for a deploy; propagation IS that
    deploy. If this deferred, the fleet would deadlock."""
    monkeypatch.setattr(
        release_cmd, "_fetch_board",
        lambda: ({"drive_queue": [{"repo_name": "api", "issue_number": 1543,
                                   "state": "done", "hold_state": HOLD_FIRED}]}, None),
    )
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.111"], "server": ["0.4.111"]})
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111"],
    )
    assert result.exit_code == 0, result.output
    assert rp.STATUS_DEFERRED not in result.output
    assert "waiting on exactly this deploy" in result.output


# ──────────────────────────────────────────────────────────────────────────
# #2067: quiescence is per host — a busy host defers on its own, and does
# not hold the rest of the fleet hostage
# ──────────────────────────────────────────────────────────────────────────


def test_a_busy_non_daemon_host_defers_alone_while_the_daemon_rolls(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """The regression, end to end: `laptop` has a live assignment; `server`
    (the daemon) is free. Under the old fleet-wide reading this deferred
    everything, forever, on a fleet whose drive queue never goes idle.
    `server` must roll and verify while `laptop`'s lanes are recorded as a
    per-host deferral, not attempted."""
    monkeypatch.setattr(
        release_cmd, "_fetch_board",
        lambda: ({"assignments": [{"machine_name": "laptop", "issue_number": 9,
                                   "status": "RUNNING"}]}, None),
    )
    calls = _stub_lanes(monkeypatch)
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
                 daemon="server")
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111"],
    )
    assert result.exit_code == 0, result.output
    # `server`'s lanes were attempted; `laptop`'s never were.
    assert any(host == "server" for _lane, host in calls)
    assert not any(host == "laptop" for _lane, host in calls)

    record = _records(state_dir)[0]
    assert record["status"] == rp.STATUS_VERIFIED
    laptop_lane = next(l for l in record["lanes"] if l["host"] == "laptop")
    assert laptop_lane["lane"] == "-"
    assert laptop_lane["ok"] is None
    assert "deferred" in laptop_lane["detail"]
    assert "laptop:9" in laptop_lane["detail"]


def test_a_busy_daemon_host_defers_the_whole_run(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """The one case per-host quiescence still has to defer everything: the
    DAEMON is occupied. Rolling `laptop` ahead of an unrolled `server` would
    put a caller on a newer `coord` than the daemon it talks to — the
    documented 405 — so nothing may roll until `server` itself is free."""
    monkeypatch.setattr(
        release_cmd, "_fetch_board",
        lambda: ({"assignments": [{"machine_name": "server", "issue_number": 9,
                                   "status": "RUNNING"}]}, None),
    )
    monkeypatch.setattr(
        release_cmd, "_roll_python",
        lambda *a, **k: pytest.fail("a busy daemon must roll nothing, anywhere"),
    )
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
                 daemon="server")
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111"],
    )
    assert result.exit_code == 0, result.output
    record = _records(state_dir)[0]
    assert record["status"] == rp.STATUS_DEFERRED
    assert record["lanes"] == []

    # #2587 review: this is the ONE place `coord release propagate` itself
    # hits the daemon-busy deadlock `coord release nightly-window` exists to
    # route around (`_ensure_roll_pending_marker`) — a plain, periodic
    # `coord-release-propagate.timer` run must arm the SAME marker the
    # drive-queue tick watches, not just defer silently and rely on an
    # operator to separately reach for `nightly-window`.
    pending = dq_cmd.read_roll_pending()
    assert pending is not None, (
        "a daemon-busy deferral in `coord release propagate` must arm the "
        "#2587 roll-pending marker, not just record a deferral"
    )
    assert pending.target_version == "0.4.111"
    assert pending.reason == "propagate"


def test_a_busy_daemon_deferral_stamps_this_runs_own_threshold_onto_the_marker(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """#2870: the marker `_ensure_roll_pending_marker` arms here must carry
    THIS run's own already-passed `--min-behind`/`propagation.
    min_releases_behind` threshold (`RollPending.min_releases_behind`) — so
    whatever eventually discharges it (`coord release nightly-window`'s
    belt-and-braces `coord release propagate` call, or the drive-queue
    tick's spawned `coord-release-window.service`) is gated at the SAME
    threshold that armed it, not a threshold re-resolved from scratch."""
    monkeypatch.setattr(
        release_cmd, "_fetch_board",
        lambda: ({"assignments": [{"machine_name": "server", "issue_number": 9,
                                   "status": "RUNNING"}]}, None),
    )
    monkeypatch.setattr(
        release_cmd, "_releases_behind_count", lambda *a, **k: (7, None),
    )
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
                 daemon="server")
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--min-behind", "5"],
    )
    assert result.exit_code == 0, result.output
    pending = dq_cmd.read_roll_pending()
    assert pending is not None
    assert pending.min_releases_behind == 5


def test_a_busy_daemon_dry_run_defers_without_writing_the_marker(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """#2869: `--dry-run` promises to "print the window verdict and the roll
    plan; change nothing" — but the daemon-busy defer branch above called
    `_ensure_roll_pending_marker` unconditionally, so a purely read-only
    `coord release propagate --dry-run` against a busy daemon armed the
    REAL #2587 roll-pending marker, freezing the whole drive queue (capacity
    forced to 0) even though nothing was supposed to change. This asserts
    the marker file stays absent and the output names what would have been
    set instead, matching `_fire_pending_roll`'s and `_apply_cordons`'s
    existing dry-run wording conventions."""
    monkeypatch.setattr(
        release_cmd, "_fetch_board",
        lambda: ({"assignments": [{"machine_name": "server", "issue_number": 9,
                                   "status": "RUNNING"}]}, None),
    )
    monkeypatch.setattr(
        release_cmd, "_roll_python",
        lambda *a, **k: pytest.fail("a busy daemon must roll nothing, anywhere"),
    )
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
                 daemon="server")
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert dq_cmd.read_roll_pending() is None, (
        "--dry-run must never write the real #2587 roll-pending marker"
    )
    assert "would set a roll-pending marker" in result.output
    assert "0.4.111" in result.output


def test_a_second_busy_daemon_deferral_does_not_re_arm_the_marker(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """`_ensure_roll_pending_marker`'s own contract: never reset an
    already-live marker's clock for the SAME target. A periodic
    `coord-release-propagate.timer` firing every ~20 minutes while the fleet
    stays busy must not keep re-arming `set_at`/`deferrals` every run — that
    would make the #2587 TTL bound unreachable in practice, exactly the
    "never actually bounded" failure the bound exists to prevent."""
    monkeypatch.setattr(
        release_cmd, "_fetch_board",
        lambda: ({"assignments": [{"machine_name": "server", "issue_number": 9,
                                   "status": "RUNNING"}]}, None),
    )
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
                 daemon="server")

    def _run() -> None:
        result = CliRunner().invoke(
            main,
            ["release", "propagate", "--config", str(valid_config_path),
             "--target", "0.4.111"],
        )
        assert result.exit_code == 0, result.output

    _run()
    first = dq_cmd.read_roll_pending()
    assert first is not None
    assert first.target_version == "0.4.111"

    _run()
    second = dq_cmd.read_roll_pending()
    assert second is not None
    assert second.target_version == "0.4.111"
    # Untouched — same marker, not a fresh one.
    assert second.set_at == first.set_at
    assert second.deferrals == first.deferrals


def test_force_rolls_over_per_host_busyness_too(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """`--force` already killed in-flight work fleet-wide before #2067; it
    must still roll every host, busy or not, rather than defer any of them."""
    monkeypatch.setattr(
        release_cmd, "_fetch_board",
        lambda: ({"assignments": [{"machine_name": "laptop", "issue_number": 9,
                                   "status": "RUNNING"},
                                  {"machine_name": "server", "issue_number": 10,
                                   "status": "RUNNING"}]}, None),
    )
    calls = _stub_lanes(monkeypatch)
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
                 daemon="server")
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--force"],
    )
    assert result.exit_code == 0, result.output
    assert "--force" in result.output  # the kill warning
    assert {host for _lane, host in calls} == {"laptop", "server"}
    assert _records(state_dir)[0]["status"] == rp.STATUS_VERIFIED


def test_a_busy_host_is_visible_in_a_dry_run_plan(
    valid_config_path, state_dir, no_network, monkeypatch
):
    monkeypatch.setattr(
        release_cmd, "_fetch_board",
        lambda: ({"assignments": [{"machine_name": "laptop", "issue_number": 9,
                                   "status": "RUNNING"}]}, None),
    )
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
                 daemon="server")
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "would roll" in result.output
    assert "server" in "\n".join(
        l for l in result.output.splitlines() if "would roll" in l
    )
    assert "laptop" not in "\n".join(
        l for l in result.output.splitlines() if "would roll" in l
    )
    assert "laptop:9" in result.output


# ──────────────────────────────────────────────────────────────────────────
# #2228: a live interactive session is host-local activity the board cannot
# see (`coord assign --interactive` never POSTs `/assign`) — it must defer a
# roll exactly like a live headless assignment does.
# ──────────────────────────────────────────────────────────────────────────


def test_a_live_interactive_session_defers_its_host_alone(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """`laptop` has a live interactive session; `server` (the daemon) does
    not. `server` must roll and verify while `laptop`'s lane is recorded as
    a per-host deferral naming the session, not attempted — the same shape
    as a live headless assignment (#2067)."""
    monkeypatch.setattr(
        release_cmd, "_interactive_session_busy",
        lambda config: [
            rp.Busy(kind="interactive session", subject="laptop:coord-abc123",
                    host="laptop")
        ],
    )
    calls = _stub_lanes(monkeypatch)
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
                 daemon="server")
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111"],
    )
    assert result.exit_code == 0, result.output
    assert any(host == "server" for _lane, host in calls)
    assert not any(host == "laptop" for _lane, host in calls)

    record = _records(state_dir)[0]
    assert record["status"] == rp.STATUS_VERIFIED
    laptop_lane = next(l for l in record["lanes"] if l["host"] == "laptop")
    assert laptop_lane["lane"] == "-"
    assert laptop_lane["ok"] is None
    assert "deferred" in laptop_lane["detail"]
    assert "interactive session" in laptop_lane["detail"]
    assert "laptop:coord-abc123" in laptop_lane["detail"]


def test_a_live_interactive_session_is_visible_in_a_dry_run_plan(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """Acceptance: `coord release propagate --dry-run` reports the session's
    host as not rollable, naming the session as the reason."""
    monkeypatch.setattr(
        release_cmd, "_interactive_session_busy",
        lambda config: [
            rp.Busy(kind="interactive session", subject="laptop:coord-abc123",
                    host="laptop")
        ],
    )
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
                 daemon="server")
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    would_roll_lines = "\n".join(
        l for l in result.output.splitlines() if "would roll" in l
    )
    assert "server" in would_roll_lines
    assert "laptop" not in would_roll_lines
    assert "laptop:coord-abc123" in result.output


def test_a_live_interactive_session_on_the_daemon_host_defers_the_whole_run(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """The daemon-first invariant applies here too: a session on `server`
    (the daemon) must defer the whole run, not just that one host — rolling
    `laptop` ahead of an unrolled daemon is the documented 405."""
    monkeypatch.setattr(
        release_cmd, "_interactive_session_busy",
        lambda config: [
            rp.Busy(kind="interactive session", subject="server:coord-def456",
                    host="server")
        ],
    )
    monkeypatch.setattr(
        release_cmd, "_roll_python",
        lambda *a, **k: pytest.fail("a busy daemon must roll nothing, anywhere"),
    )
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
                 daemon="server")
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111"],
    )
    assert result.exit_code == 0, result.output
    record = _records(state_dir)[0]
    assert record["status"] == rp.STATUS_DEFERRED
    assert record["lanes"] == []


def test_no_live_session_rolls_exactly_as_before(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """No interactive session anywhere -> no new deferral; both hosts roll.
    (The `no_network` fixture's default `_interactive_session_busy` stub
    already returns `[]` — this test pins that "no signal, no change"
    behaviour explicitly.)"""
    calls = _stub_lanes(monkeypatch)
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
                 daemon="server")
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111"],
    )
    assert result.exit_code == 0, result.output
    assert {host for _lane, host in calls} == {"laptop", "server"}
    assert _records(state_dir)[0]["status"] == rp.STATUS_VERIFIED


def test_a_failing_session_probe_does_not_defer_the_fleet(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """#2228 acceptance: a failing/unavailable session probe must fail
    OPEN — never synthesise an unpinned `Busy` (which would read as
    `fleet_wide_busy` and defer everything forever). Exercises the REAL
    `_interactive_session_busy`, with the underlying tmux/ssh sweep
    stubbed to raise, rather than the `no_network` fixture's blanket stub."""
    monkeypatch.setattr(
        release_cmd, "_interactive_session_busy", _REAL_INTERACTIVE_SESSION_BUSY,
    )
    monkeypatch.setattr(
        "coord.interactive.gather_fleet_tmux_sessions",
        lambda config: (_ for _ in ()).throw(RuntimeError("ssh sweep blew up")),
    )
    calls = _stub_lanes(monkeypatch)
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
                 daemon="server")
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111"],
    )
    assert result.exit_code == 0, result.output
    assert "warning: could not probe interactive sessions" in result.output
    assert {host for _lane, host in calls} == {"laptop", "server"}
    assert _records(state_dir)[0]["status"] == rp.STATUS_VERIFIED


# ──────────────────────────────────────────────────────────────────────────
# #2174: `coord pause <machine>` says "leave this box alone" — the board has
# no `paused` column on any row, so it has to reach `assess_quiescence`
# through the same `extra_busy` seam as a live interactive session.
# ──────────────────────────────────────────────────────────────────────────


def test_a_paused_non_daemon_host_defers_alone_while_the_daemon_rolls(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """`laptop` is paused; `server` (the daemon) is not. `server` must roll
    and verify while `laptop`'s lane is recorded as a per-host deferral
    naming the pause, not attempted — the same shape as a live headless
    assignment (#2067) or a live interactive session (#2228). This is the
    exact regression #2174 reports: before the fix, a paused machine read as
    idle and `coord release propagate` rolled it anyway."""
    mp.local_pause("laptop")
    calls = _stub_lanes(monkeypatch)
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
                 daemon="server")
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111"],
    )
    assert result.exit_code == 0, result.output
    assert any(host == "server" for _lane, host in calls)
    assert not any(host == "laptop" for _lane, host in calls)

    record = _records(state_dir)[0]
    assert record["status"] == rp.STATUS_VERIFIED
    laptop_lane = next(l for l in record["lanes"] if l["host"] == "laptop")
    assert laptop_lane["lane"] == "-"
    assert laptop_lane["ok"] is None
    assert "deferred" in laptop_lane["detail"]
    assert "machine paused" in laptop_lane["detail"]
    assert "laptop" in laptop_lane["detail"]


def test_a_paused_host_is_visible_in_a_dry_run_plan(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """Acceptance: `coord release propagate --dry-run` against a board
    where one machine is paused reports that machine as busy and excludes
    it from the rollable set, while other hosts remain rollable."""
    mp.local_pause("laptop")
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
                 daemon="server")
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    would_roll_lines = "\n".join(
        l for l in result.output.splitlines() if "would roll" in l
    )
    assert "server" in would_roll_lines
    assert "laptop" not in would_roll_lines
    assert "machine paused" in result.output
    assert "explicit `coord pause`" in result.output


def test_a_paused_daemon_host_defers_the_whole_run(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """#2174 item 2: pausing the DAEMON host defers the whole run, not just
    that host's own lane — the daemon-leads invariant already defers
    everything when the daemon is busy and behind, and a pause is one more
    way for it to be busy. The recorded reason must name the pause, not a
    generic 'busy' string."""
    mp.local_pause("server")
    monkeypatch.setattr(
        release_cmd, "_roll_python",
        lambda *a, **k: pytest.fail("a paused daemon must roll nothing, anywhere"),
    )
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
                 daemon="server")
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111"],
    )
    assert result.exit_code == 0, result.output
    record = _records(state_dir)[0]
    assert record["status"] == rp.STATUS_DEFERRED
    assert record["lanes"] == []
    assert "machine paused" in record["quiescence"]["reason"]
    assert "server" in record["quiescence"]["reason"]


def test_an_unpaused_fleet_is_unaffected_by_the_pause_seam(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """Acceptance: an unpaused fleet gets no new busy signal from #2174 —
    `quiescent` and the roll outcome are unchanged from before the fix."""
    calls = _stub_lanes(monkeypatch)
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
                 daemon="server")
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111"],
    )
    assert result.exit_code == 0, result.output
    assert {host for _lane, host in calls} == {"laptop", "server"}
    record = _records(state_dir)[0]
    assert record["status"] == rp.STATUS_VERIFIED
    assert record["quiescence"]["quiescent"] is True


def test_a_release_cordon_alone_is_not_read_as_an_operator_pause(valid_config_path):
    """A #2101 release cordon is this command's OWN drain mechanism, not a
    sign a host is already quiescent — feeding it back in through
    `_paused_machine_busy` would make a cordon defer the very roll it exists
    to unblock. Cordoning `laptop` (with no explicit `coord pause` and no
    quiet-hours window) must not, on its own, produce a `machine paused`
    busy signal."""
    mp.local_set_cordon("laptop", target_version="0.4.110")
    config = load_config(str(valid_config_path))
    assert release_cmd._paused_machine_busy(config) == []


# ── the roll, the final gate, and the rollback on red ────────────────────


def _stub_lanes(monkeypatch, *, python_ok=True, serve_unit_ok=None, calls=None,
                tui_local=None):
    """Replace the three per-lane executors with recorders.

    *python_ok* is either a single bool applied to every host, or a
    ``{host: bool}`` mapping for tests that need one host's python lane to
    fail while another's succeeds (e.g. the daemon-leads invariant).

    *serve_unit_ok* (#2095 review) mirrors *python_ok*'s shape but answers
    the narrower question `_roll_python` now reports separately: whether
    THIS host's own coord-serve is confirmed on target_version, independent
    of whether some OTHER sibling (coord-web, coord-drive-queue) also failed
    to restart — see `_roll_python`'s docstring. Left as ``None`` it tracks
    *python_ok* 1:1 host-for-host — "the lane failed because coord-serve
    itself failed", the historical undifferentiated shape — so tests that
    don't care about the distinction don't have to know about it.
    """
    log = calls if calls is not None else []

    def _mapped(mapping, host: str, default: bool) -> bool:
        if isinstance(mapping, dict):
            return mapping.get(host, default)
        if mapping is None:
            return default
        return mapping

    def _ok_for(host: str) -> bool:
        return _mapped(python_ok, host, True)

    def _python(machine, **kwargs):
        log.append(("python", machine.name))
        ok = _ok_for(machine.name)
        s_ok = _mapped(serve_unit_ok, machine.name, ok)
        return ok, ("now v0.4.111" if ok else "pip failed"), s_ok

    def _units(machine, **kwargs):
        log.append(("units", machine.name))
        return True, "1 unit(s) refreshed; daemon-reload ok"

    def _tui(machine, **kwargs):
        log.append(("tui", machine.name))
        if tui_local is not None and machine.name != tui_local:
            # #2052: no channel for this lane here — `ok=None`, never False.
            return None, "coord-tui is a per-host binary with no remote install path"
        return True, "coord-tui now v0.4.111"

    monkeypatch.setattr(release_cmd, "_roll_python", _python)
    monkeypatch.setattr(release_cmd, "_roll_units", _units)
    monkeypatch.setattr(release_cmd, "_roll_tui", _tui)
    return log


def test_a_green_roll_is_verified_and_journalled(valid_config_path, state_dir,
                                                 no_network, monkeypatch):
    calls = _stub_lanes(monkeypatch)
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]})
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--daemon-host", "server"],
    )
    assert result.exit_code == 0, result.output
    record = _records(state_dir)[0]
    assert record["status"] == rp.STATUS_VERIFIED
    # The daemon leads — the 405 invariant, end to end.
    assert calls[0] == ("python", "server")
    # Every lane and host is in the record: #1835's observability gate is
    # "when each lane rolled", not "something happened".
    assert {(l["lane"], l["host"]) for l in record["lanes"]} >= {
        ("python", "server"), ("python", "laptop"),
        ("units", "server"), ("tui", "laptop"),
    }
    assert record["verification"]["severity"] == "ok"


def test_a_waiting_between_legs_entry_rolls_its_host_normally(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """#2403, end to end: `laptop`'s only queue entry is `waiting` — deferred
    by an unrelated repo-capacity cap (#1972), exactly `claude-coordinator
    #2005`'s live shape on 2026-08-18 — with a terminal assignment recording
    `laptop` as its last worker. #2240's "last known host" fallback exists
    for a `running` between-legs row; it must not reach a `waiting` one. A
    genuinely idle `laptop` has to roll and verify like any other free host,
    not sit deferred and cordoned on the strength of a queue row that never
    launched anything."""
    monkeypatch.setattr(
        release_cmd, "_fetch_board",
        lambda: (
            {
                "drive_queue": [
                    {"repo_name": "api", "issue_number": 2005,
                     "state": "waiting", "deferrals": 3, "attempts": 2},
                ],
                "assignments": [
                    {"repo_name": "api", "issue_number": 2005,
                     "machine_name": "laptop", "status": "COMPLETED",
                     "dispatched_at": 100.0},
                ],
            },
            None,
        ),
    )
    calls = _stub_lanes(monkeypatch)
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
                 daemon="server")
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111"],
    )
    assert result.exit_code == 0, result.output
    assert rp.STATUS_DEFERRED not in result.output
    assert any(host == "laptop" for _lane, host in calls), (
        "a waiting queue row must never defer the host it names as its "
        "last known worker"
    )
    record = _records(state_dir)[0]
    assert record["status"] == rp.STATUS_VERIFIED
    assert not any(
        l["host"] == "laptop" and l["lane"] == "-" for l in record["lanes"]
    )
    # Nothing is left cordoned behind a row that was never actually running.
    assert mp.cordoned_names() == set()


def test_a_red_verification_rolls_every_updated_host_back(valid_config_path, state_dir,
                                                          no_network, monkeypatch):
    """#1835: 'a red post-deploy verification must roll back, not just
    report.' Exit 2 so the timer's failure is distinguishable from a
    deferral (0) and from a plain failure (1)."""
    _stub_lanes(monkeypatch)
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
                 severity="crit")
    rolled_back: list[str] = []
    monkeypatch.setattr(
        release_cmd, "_rollback_host",
        lambda machine, **k: (rolled_back.append(machine.name), (True, "rolling back"))[1],
    )
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--daemon-host", "server"],
    )
    assert result.exit_code == 2, result.output
    assert sorted(rolled_back) == ["laptop", "server"]
    assert _records(state_dir)[0]["status"] == rp.STATUS_ROLLED_BACK


def test_a_host_whose_python_lane_failed_is_not_rolled_back(valid_config_path,
                                                            state_dir, no_network,
                                                            monkeypatch):
    """Rolling back a host this run never successfully updated would undo
    somebody else's deliberate state."""
    _stub_lanes(monkeypatch, python_ok=False)
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
                 severity="crit")
    rolled_back: list[str] = []
    monkeypatch.setattr(
        release_cmd, "_rollback_host",
        lambda machine, **k: (rolled_back.append(machine.name), (True, "x"))[1],
    )
    CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--daemon-host", "server"],
    )
    assert rolled_back == []


def test_a_failed_daemon_python_roll_skips_other_hosts_python_lane(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """#1835 review: plan_lanes() puts the daemon host's python lane first so
    that 'a caller must never reach an endpoint its daemon predates' holds —
    but that invariant is only real if a failure there actually stops the
    rest of the python lane. If `server` (the daemon) fails its own python
    roll, `laptop` must never be advanced to target_version anyway; doing so
    would reproduce the documented 405 skew for the rest of this run."""
    calls = _stub_lanes(monkeypatch, python_ok={"server": False, "laptop": True})
    # The stubbed verify gate reports whatever `versions` says regardless of
    # what the roll loop actually did — it is not the seam under test here.
    # What's under test is that the loop itself never advances `laptop` past
    # the daemon, independent of what the final gate later decides.
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]})
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--daemon-host", "server"],
    )
    assert result.exit_code == 0, result.output
    # The daemon's own python lane was attempted and failed...
    assert ("python", "server") in calls
    # ...but laptop's python lane was never attempted at all — not
    # attempted and failed, simply skipped outright.
    assert ("python", "laptop") not in calls

    record = _records(state_dir)[0]
    laptop_python = next(
        l for l in record["lanes"] if l["lane"] == "python" and l["host"] == "laptop"
    )
    # "not attempted" is recorded as ok=None, distinct from ok=False (a real
    # failure) — a re-run should resume this host, not treat it as needing
    # a rollback.
    assert laptop_python["ok"] is None
    assert "not attempted" in laptop_python["detail"]
    # No lane record claims laptop's python roll succeeded.
    assert not any(
        l["lane"] == "python" and l["host"] == "laptop" and l["ok"] is True
        for l in record["lanes"]
    )


def test_a_daemon_coord_web_only_failure_does_not_skip_other_hosts_python_lane(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """#2095 review: `daemon_python_failed` used to be set straight from the
    python lane's own aggregate `ok` — which #2095 correctly made `False`
    whenever ANY restarted sibling failed, coord-web included. But the
    cascading skip this flag drives exists to stop other hosts calling into
    a daemon whose own coord-serve hasn't reached target_version — it has
    nothing to do with coord-web. Reusing the aggregate for that decision
    meant a coord-web-only failure on the daemon host (coord-serve itself
    restarts and reports target_version fine) ALSO halted every other
    host's python lane for the rest of the run: a materially larger blast
    radius than before #2095, and precisely the shape of the 2026-08-10
    incident this issue is about (dellserver's coord-serve was fine;
    coord-web was what failed). The daemon's own lane must still fail here
    (no `✓` over a real outage) — but `laptop` must still be allowed to
    roll forward, because coord-serve on the daemon is fine."""
    calls = _stub_lanes(
        monkeypatch,
        python_ok={"server": False, "laptop": True},
        serve_unit_ok={"server": True},
    )
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]})
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--daemon-host", "server"],
    )
    assert result.exit_code == 0, result.output
    # The daemon's own python lane was attempted and failed...
    assert ("python", "server") in calls
    # ...but unlike a coord-serve failure, laptop's python lane WAS still
    # attempted — coord-serve on the daemon is confirmed fine, so nothing
    # here should have blocked it.
    assert ("python", "laptop") in calls

    record = _records(state_dir)[0]
    server_python = next(
        l for l in record["lanes"] if l["lane"] == "python" and l["host"] == "server"
    )
    assert server_python["ok"] is False, "the lane itself still failed — never a ✓"
    laptop_python = next(
        l for l in record["lanes"] if l["lane"] == "python" and l["host"] == "laptop"
    )
    assert laptop_python["ok"] is True, (
        "a coord-web-only failure on the daemon must not cascade into "
        "skipping every other host's python lane"
    )


def test_no_rollback_on_red_reports_instead(valid_config_path, state_dir, no_network,
                                            monkeypatch):
    _stub_lanes(monkeypatch)
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
                 severity="crit")
    monkeypatch.setattr(
        release_cmd, "_rollback_host",
        lambda *a, **k: pytest.fail("--no-rollback-on-red must not roll back"),
    )
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--no-rollback-on-red"],
    )
    assert result.exit_code == 1
    assert _records(state_dir)[0]["status"] == rp.STATUS_FAILED


def test_a_verified_roll_releases_the_deploy_gate_that_was_waiting(valid_config_path,
                                                                   state_dir,
                                                                   no_network,
                                                                   monkeypatch):
    """The loop closes: the gate stops the queue for the deploy, propagation
    performs the deploy, propagation restarts the queue."""
    _stub_lanes(monkeypatch)
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]})
    monkeypatch.setattr(
        release_cmd, "_fetch_board",
        lambda: ({"drive_queue": [{"repo_name": "api", "issue_number": 1543,
                                   "state": "done", "hold_state": HOLD_FIRED}]}, None),
    )
    released: list[str] = []
    monkeypatch.setattr(
        release_cmd, "_release_hold",
        lambda key: (released.append(key), (True, "queue released"))[1],
    )
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111"],
    )
    assert result.exit_code == 0, result.output
    assert released == ["api#1543"]
    assert _records(state_dir)[0]["released_holds"] == ["api#1543"]


def test_a_rolled_back_run_leaves_the_deploy_gate_held(valid_config_path, state_dir,
                                                       no_network, monkeypatch):
    """Releasing the gate on a rolled-back roll would restart the overnight
    queue into the exact 'merged is not live' trap the gate exists for."""
    _stub_lanes(monkeypatch)
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
                 severity="crit")
    monkeypatch.setattr(release_cmd, "_rollback_host", lambda *a, **k: (True, "x"))
    monkeypatch.setattr(
        release_cmd, "_release_hold",
        lambda key: pytest.fail("a rolled-back run must never release the gate"),
    )
    monkeypatch.setattr(
        release_cmd, "_fetch_board",
        lambda: ({"drive_queue": [{"repo_name": "api", "issue_number": 1543,
                                   "state": "done", "hold_state": HOLD_FIRED}]}, None),
    )
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111"],
    )
    assert result.exit_code == 2


def test_no_verify_stops_before_the_gate(valid_config_path, state_dir, no_network,
                                         monkeypatch):
    _stub_lanes(monkeypatch)
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]})
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--no-verify"],
    )
    assert result.exit_code == 0
    record = _records(state_dir)[0]
    assert record["status"] == rp.STATUS_ROLLED
    assert record["verification"] is None


# ── rollback is one command (#1560) ──────────────────────────────────────


def test_release_rollback_hits_every_machine(valid_config_path, monkeypatch):
    hit: list[str] = []

    def _fake_post(url, payload, *, timeout):
        hit.append(url)
        return 202, {}, ""

    monkeypatch.setattr(release_cmd, "_post", _fake_post)
    monkeypatch.setattr(release_cmd, "_get",
                        lambda url, *, timeout: (200, {"version": "0.4.110"}))
    result = CliRunner().invoke(
        main, ["release", "rollback", "--config", str(valid_config_path), "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert len(hit) == 2
    assert all(u.endswith("/rollback") for u in hit)
    # #2052 fault 1: "rolling back" is a statement about the request. The
    # outcome is whether the service is serving again.
    assert "serving again" in result.output


def test_a_rollback_that_leaves_the_agent_dead_says_so(valid_config_path, monkeypatch):
    """#2052 fault 1: precision's coord-agent went `inactive (dead)` at the
    moment of the rollback and was never restarted — recovery needed a human.
    A rollback that stops a service and does not restore it leaves the fleet
    WORSE off than the failed roll did, so it must escalate, and then shout."""
    monkeypatch.setattr(release_cmd, "_post", lambda *a, **k: (202, {}, ""))
    monkeypatch.setattr(release_cmd, "_get", lambda url, *, timeout: (None, {}))
    escalated: list[str] = []
    monkeypatch.setattr(
        "coord.commands.agent_ops._escalate_restart",
        lambda machine: (escalated.append(machine.name), False)[1],
    )
    result = CliRunner().invoke(
        main, ["release", "rollback", "--config", str(valid_config_path), "--yes",
               "--wait", "1"]
    )
    assert result.exit_code == 1, result.output
    assert "DOWN" in result.output
    # The documented systemd-stall fix is APPLIED, not merely suggested.
    assert escalated, "a dead agent must be restarted, not just reported"


def test_a_rollback_rescued_by_the_ssh_restart_is_a_success(valid_config_path,
                                                            monkeypatch):
    """#404/#1568: `os.execv` does not always take under systemd. The
    documented fix is an SSH `systemctl --user restart coord-agent` — and a
    host that came back that way is genuinely back."""
    monkeypatch.setattr(release_cmd, "_post", lambda *a, **k: (202, {}, ""))
    answers = iter([(None, {})] * 200)
    revived = {"yes": False}

    def _fake_get(url, *, timeout):
        if revived["yes"]:
            return 200, {"version": "0.4.110"}
        return next(answers)

    monkeypatch.setattr(release_cmd, "_get", _fake_get)
    monkeypatch.setattr(
        "coord.commands.agent_ops._escalate_restart",
        lambda machine: revived.__setitem__("yes", True) or True,
    )
    result = CliRunner().invoke(
        main, ["release", "rollback", "--config", str(valid_config_path), "--yes",
               "--wait", "1"]
    )
    assert result.exit_code == 0, result.output
    assert "systemctl --user restart" in result.output


def test_release_rollback_reports_a_host_with_no_previous_generation(valid_config_path,
                                                                     monkeypatch):
    monkeypatch.setattr(release_cmd, "_post", lambda *a, **k: (404, {}, ""))
    result = CliRunner().invoke(
        main, ["release", "rollback", "--config", str(valid_config_path), "--yes"]
    )
    assert result.exit_code == 1
    assert "no previous generation" in result.output


# ──────────────────────────────────────────────────────────────────────────
# #2052: the gate cannot fail for reasons propagation cannot influence
# ──────────────────────────────────────────────────────────────────────────


def test_2026_08_09_a_good_roll_is_not_reverted_by_lanes_it_cannot_roll(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """The regression, end to end. #2052: every lane propagation *can* roll,
    rolled — three python lanes, three unit lanes, the one coord-tui it could
    reach. Verification then came back crit on `~/.coord-cli-venv` (a lane
    this command has no model of) and on the remote `coord-tui` binary (which
    it reports itself has NO remote install path), plus a stale `webapp
    bundle` (SHA-versioned off its own timer, never pip-versioned at all) —
    and `--rollback-on-red` reverted the lot. Not a transient failure: it
    would have happened on every run, forever.

    #2069 closed the fourth lane this incident actually hit — `coord-serve
    process` — by having the python lane restart coord-serve itself; see
    `tests/test_release_propagate.py::
    test_a_sibling_unit_finding_blocks_when_its_host_python_lane_rolled` for
    that lane now correctly blocking instead of being advisory forever."""
    from coord import release_verify as rv

    # `server` is the host this command runs on, so it is the only host whose
    # coord-tui binary has any install path at all — exactly the shape of the
    # real run, where 1 of 3 tui lanes could roll.
    _stub_lanes(monkeypatch, tui_local="server")
    _stub_verify(
        monkeypatch,
        versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
        findings=[
            rv.Finding(severity="crit", host="laptop",
                       lane="~/.coord-cli-venv (laptop)",
                       summary="on 0.4.104, expected 0.4.111"),
            rv.Finding(severity="warn", host="server",
                       lane="webapp bundle",
                       summary="webapp bundle is stale"),
            rv.Finding(severity="warn", host="laptop", lane="coord-tui",
                       summary="tui binary is stale"),
        ],
    )
    monkeypatch.setattr(
        release_cmd, "_rollback_host",
        lambda *a, **k: pytest.fail(
            "reverting a good python roll because a per-host binary could "
            "not be installed remotely is a category error"
        ),
    )
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--daemon-host", "server"],
    )
    assert result.exit_code == 0, result.output
    record = _records(state_dir)[0]
    assert record["status"] == rp.STATUS_VERIFIED
    # The full report is still journalled verbatim — scoping the gate must
    # never shrink the record.
    assert record["verification"]["severity"] == "crit"
    assert len(record["verification"]["findings"]) == 3
    # ...and the scoping itself is legible, so a gate that stopped gating
    # would be visible rather than silent.
    assert record["gate"]["severity"] == "ok"
    assert len(record["gate"]["advisory"]) == 3
    assert "advisory" in result.output


def test_2981_an_empty_tui_channel_does_not_roll_back_the_python_lanes(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """THE #2981 regression, end to end.

    `JDonaghy/coord-tui` had zero releases/tags, so `coord tui update` 404s
    from `/releases/latest` on every run, forever, until someone cuts a
    first release. `_roll_tui` on the one host that actually has an install
    path (`dellserver` in the real incident, `server` here) used to read
    that 404 exactly like a genuine install failure and report `ok=False` —
    a BLOCKING result. That put `(tui, server)` inside this run's attempted
    scope, so a merely-advisory "coord-tui is stale" verify finding on that
    same host turned blocking, `gate.red` went true, and
    `--rollback-on-red` reverted the two good python rolls right alongside
    the tui lane that was never actually broken — a fleet rolled forward
    and straight back for nothing, twice in a row, per the issue.

    `coord tui update` now exits `EXIT_EMPTY_CHANNEL` for exactly this case
    (`EmptyReleaseChannelError`, raised at the source in
    `fetch_latest_release_tag`), and `_roll_tui` reads that exit code back
    into `ok=None` — the same "no channel to roll" treatment already given
    to a remote host with no install path at all. `ok=None` excludes the
    lane from `attempted_scope`, so the verify finding below stays
    advisory, the gate stays green, and nothing gets rolled back."""
    from coord import release_verify as rv

    calls = _stub_lanes(monkeypatch)

    def _tui(machine, **kwargs):
        calls.append(("tui", machine.name))
        if machine.name == "server":
            # What `_roll_tui` itself now returns when `coord tui update`
            # exits `EXIT_EMPTY_CHANNEL` — see its #2981 docstring section.
            return None, (
                f"{rp.CHANNEL_TUI} channel has no published release yet — "
                "nothing to install, not a roll failure (#2981): "
                "404 Not Found"
            )
        return None, "coord-tui is a per-host binary with no remote install path"

    monkeypatch.setattr(release_cmd, "_roll_tui", _tui)
    _stub_verify(
        monkeypatch,
        versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
        findings=[
            # Even a CRIT finding, on the very host that attempted this
            # lane, must not turn the gate red once the attempt itself is
            # `ok=None` — that is the whole point of #2052's scoping.
            rv.Finding(severity="crit", host="server", lane="coord-tui",
                       summary="coord-tui is 40h older than source"),
        ],
    )
    monkeypatch.setattr(
        release_cmd, "_rollback_host",
        lambda *a, **k: pytest.fail(
            "an empty coord-tui channel is not a failed roll — it must "
            "never trigger --rollback-on-red"
        ),
    )
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--daemon-host", "server"],
    )
    assert result.exit_code == 0, result.output

    record = _records(state_dir)[0]
    # The run completed and verified — it was never reverted.
    assert record["status"] == rp.STATUS_VERIFIED
    # The python lanes for BOTH hosts stayed rolled.
    for host in ("server", "laptop"):
        python_lane = next(
            l for l in record["lanes"] if l["lane"] == "python" and l["host"] == host
        )
        assert python_lane["ok"] is True, f"{host}'s good python roll must survive"

    # The tui lane itself is recorded as unrollable, not failed.
    server_tui = next(
        l for l in record["lanes"] if l["lane"] == "tui" and l["host"] == "server"
    )
    assert server_tui["ok"] is None
    assert server_tui["unrollable"] is True

    # The verify finding is still journalled in full...
    assert record["verification"]["severity"] == "crit"
    assert len(record["verification"]["findings"]) == 1
    # ...but the gate that `--rollback-on-red` actually acts on is clean.
    assert record["gate"]["severity"] == "ok"
    assert len(record["gate"]["blocking"]) == 0
    assert len(record["gate"]["advisory"]) == 1
    assert "advisory" in result.output


def test_the_outside_reach_message_names_the_manual_remedy(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """#2403: elitebook sat behind for the length of a cordon's lifetime with
    only `"outside propagation's reach, fix by hand"` as its signal — no
    remedy, just an instruction to invent one under time pressure. A
    finding that names a host must name the two commands that actually fix
    it: `coord agent update --machine <host>` and `coord release cordon
    --clear <host>`."""
    from coord import release_verify as rv

    # `laptop` has a live assignment, so its python lane is deferred (never
    # attempted) this run — exactly why its own version-mismatch finding
    # lands as advisory rather than blocking (#2052's `attempted_scope`).
    monkeypatch.setattr(
        release_cmd, "_fetch_board",
        lambda: ({"assignments": [{"machine_name": "laptop", "issue_number": 9,
                                   "status": "RUNNING"}]}, None),
    )
    _stub_lanes(monkeypatch)
    _stub_verify(
        monkeypatch,
        versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
        daemon="server",
        findings=[
            rv.Finding(severity="crit", host="laptop",
                       lane="~/.coord-venv (laptop)",
                       summary="on 0.4.104, expected 0.4.111"),
        ],
    )
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111"],
    )
    assert result.exit_code == 0, result.output
    assert "outside propagation's reach, fix by hand" in result.output
    assert "coord agent update --machine laptop" in result.output
    assert "coord release cordon --clear laptop" in result.output


def test_a_stuck_busy_host_with_nothing_else_to_roll_still_reports_reach(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """#3048: dell64 sat cordoned and idle, behind the target, while this
    run had nothing else left to attempt — `server` was already on target
    and `laptop` (the only host still behind) was busy, so `rolls` came
    back empty and the run used to finish via the `if not rolls:` branch
    *before* it ever reached the post-roll gate that knows how to print
    the outside-reach message. The remedy used to surface only if an
    operator happened to run `coord release verify` separately. It must
    now print here too, on the very tick that found nothing to roll."""
    from coord import release_verify as rv

    monkeypatch.setattr(
        release_cmd, "_fetch_board",
        lambda: ({"assignments": [{"machine_name": "laptop", "issue_number": 9,
                                   "status": "RUNNING"}]}, None),
    )
    calls = _stub_lanes(monkeypatch)
    _stub_verify(
        monkeypatch,
        versions={"laptop": ["0.4.104"], "server": ["0.4.111"]},
        daemon="server",
        findings=[
            rv.Finding(severity="crit", host="laptop",
                       lane="~/.coord-venv (laptop)",
                       summary="on 0.4.104, expected 0.4.111"),
        ],
    )
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111"],
    )
    assert result.exit_code == 0, result.output
    assert not calls  # nothing was attempted this run — `rolls` was empty
    record = _records(state_dir)[0]
    assert record["status"] == rp.STATUS_DEFERRED
    assert "outside propagation's reach, fix by hand" in result.output
    assert "coord agent update --machine laptop" in result.output
    assert "coord release cordon --clear laptop" in result.output
    assert record["verification"]["severity"] == "crit"
    assert record["gate"]["advisory"]
    assert record["gate"]["severity"] == "ok"  # advisory never triggers red


def test_a_stale_unit_advisory_names_the_units_remedy_not_agent_update(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """#2963: a `unit ...` lane finding (`coord.health.checks.unit_drift`,
    #1831/#1927) got the exact same fixed remedy as every other advisory —
    `coord agent update --machine <host>` — even though that command only
    swaps the venv and never touches `~/.config/systemd/user/`. Followed
    literally it does nothing for a stale unit, which is how #2938's
    `Restart=always` fix (shipped four releases ago) reached zero hosts'
    live systemd: every `--lane python` run reported '✓ verified' with the
    units gap present only as a silenced advisory pointing at the wrong fix.

    The health check itself already computes the correct, host-specific
    remedy into `Finding.detail` (see `unit_drift.py`'s `cp ... &&
    systemctl --user restart ...` / templated-`sed` remedies) — this must be
    surfaced instead of a fabricated one."""
    from coord import release_verify as rv

    _stub_lanes(monkeypatch)
    _stub_verify(
        monkeypatch,
        versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
        daemon="server",
        findings=[
            rv.Finding(
                severity="warn",
                host="laptop",
                lane="unit coord-agent.service",
                summary="stale — installed 240h ago, 39 line(s) differ",
                detail=(
                    "cp /deploy/coord-agent.service "
                    "~/.config/systemd/user/coord-agent.service && "
                    "systemctl --user daemon-reload && systemctl --user "
                    "restart coord-agent   # reference: packaged coord 0.4.111"
                ),
            ),
        ],
    )
    # #1831: units are rolled by their OWN lane (`POST /deploy-units`), never
    # by the python lane — `--lane python` here reproduces the exact
    # invocation from #2963's repro, which is also this fleet's normal
    # manual-release habit (the auto timer is disabled, per
    # docs/AGENT_OPERATIONS.md). The units lane is therefore never part of
    # this run's `attempted_scope`, so the finding lands as advisory.
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--lane", "python"],
    )
    assert result.exit_code == 0, result.output
    assert "advisory" in result.output
    assert "coord agent update --machine laptop" not in result.output
    assert "systemctl --user restart coord-agent" in result.output


def test_a_crit_on_a_lane_this_run_rolled_still_reverts(valid_config_path, state_dir,
                                                        no_network, monkeypatch):
    """Scoping the gate is not removing it."""
    from coord import release_verify as rv

    _stub_lanes(monkeypatch)
    _stub_verify(
        monkeypatch,
        versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
        findings=[rv.Finding(severity="crit", host="laptop",
                             lane="~/.coord-venv (laptop)",
                             summary="on 0.4.110, expected 0.4.111")],
    )
    rolled_back: list[str] = []
    monkeypatch.setattr(
        release_cmd, "_rollback_host",
        lambda machine, **k: (rolled_back.append(machine.name), (True, "back up"))[1],
    )
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--daemon-host", "server"],
    )
    assert result.exit_code == 2, result.output
    assert sorted(rolled_back) == ["laptop", "server"]


def test_the_remote_tui_lane_is_recorded_as_unrollable_not_failed(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """`coord-tui` is a per-host binary with no remote install path — the
    command says so in its own failure message. A lane that reports it cannot
    be rolled from here must not also count as this run going wrong."""
    _stub_lanes(monkeypatch, tui_local="server")
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]})
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--daemon-host", "server"],
    )
    assert result.exit_code == 0, result.output
    record = _records(state_dir)[0]
    laptop_tui = next(
        l for l in record["lanes"] if l["lane"] == "tui" and l["host"] == "laptop"
    )
    assert laptop_tui["ok"] is None
    assert laptop_tui["unrollable"] is True
    assert "tui@laptop" in record["gate"]["unrollable"]


def test_an_agent_without_deploy_units_is_a_next_run_fact_not_a_red_gate(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """Bootstrap: an agent that predates /deploy-units gets the endpoint once
    the python lane lands. That is a fact about the next run, and it must not
    revert this one."""
    _stub_lanes(monkeypatch)
    monkeypatch.setattr(
        release_cmd, "_roll_units",
        lambda machine, **k: (None, "agent has no /deploy-units yet"),
    )
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]})
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--daemon-host", "server"],
    )
    assert result.exit_code == 0, result.output
    record = _records(state_dir)[0]
    assert all(
        l["unrollable"] is True
        for l in record["lanes"] if l["lane"] == "units"
    )


# ──────────────────────────────────────────────────────────────────────────
# #2052 fault 2: the daemon host is derived, or the run refuses
# ──────────────────────────────────────────────────────────────────────────


def test_the_daemon_host_is_derived_from_the_fleets_own_health(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """No --daemon-host flag, and it still leads: `server` is the machine
    whose /health reports a running coord-serve."""
    calls = _stub_lanes(monkeypatch)
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
                 daemon="server")
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111"],
    )
    assert result.exit_code == 0, result.output
    assert calls[0] == ("python", "server")


def test_an_unidentifiable_daemon_host_refuses_instead_of_guessing(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """#2052 fault 2: this used to warn and roll in coordinator.yml order,
    which during a partial revert briefly left the daemon host BEHIND both
    its callers — the documented 405 hazard the warning itself named.
    Ordering is the one thing protecting against that."""
    _stub_lanes(
        monkeypatch,
        calls=None,
    )
    monkeypatch.setattr(
        release_cmd, "_roll_python",
        lambda *a, **k: pytest.fail("an unorderable run must not roll anything"),
    )
    monkeypatch.setattr(release_cmd, "_daemon_machine_name", lambda *a, **k: None)
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
                 daemon=None)
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111"],
    )
    assert result.exit_code == 1, result.output
    record = _records(state_dir)[0]
    assert record["status"] == rp.STATUS_FAILED
    assert "REFUSING" in record["error"]
    assert "405" in record["error"]


# ── helpers ──────────────────────────────────────────────────────────────


def _serve_health(host: str) -> dict:
    """A ``/health`` body whose ``spawned_coord`` rows name a live coord-serve.

    #2052 fault 2: this is how the daemon host is *derived* rather than
    guessed. Stubbing `gather` with an empty machine_health used to leave
    propagation unable to name the daemon at all, which is precisely the
    state that let it roll in coordinator.yml order and briefly put the
    daemon behind both its callers.
    """
    return {
        "version": "0.4.111",
        "health": {"schema": 1, "results": [
            {"check_id": "spawned_coord", "subject": "coord-serve",
             "severity": "ok", "values": {"unit": "coord-serve", "pid": 1,
                                          "version": "0.4.111"}},
        ]},
    }


def _stub_verify(monkeypatch, *, versions: dict[str, list[str]], severity: str = "ok",
                 daemon: str | None = "server", findings=None):
    """Replace `coord.release_verify`'s fleet sweep with a canned lane set.

    *daemon* is the machine whose ``/health`` reports a running coord-serve —
    the fact `_daemon_machine_name` derives the roll order from. Pass None to
    model a fleet nothing can name a daemon for (which now REFUSES to roll).
    """
    from coord import release_verify as rv

    lanes = [
        rv.Lane(host=host, lane="~/.coord-venv", version=v)
        for host, vs in versions.items()
        for v in vs
    ]
    if findings is None:
        findings = (
            # #2052: a crit the gate can actually attribute to this run. A
            # finding on a lane propagation cannot roll is advisory, and
            # tests that want THAT say so explicitly.
            [rv.Finding(severity="crit", host=host, lane=f"~/.coord-venv ({host})",
                        summary="stubbed")
             for host in sorted(versions)]
            if severity == "crit"
            else []
        )
    machine_health = {daemon: _serve_health(daemon)} if daemon else {}
    monkeypatch.setattr(rv, "gather",
                        lambda *a, **k: (machine_health, {}, None, daemon or "daemon"))
    monkeypatch.setattr(
        rv, "verify",
        lambda **kwargs: rv.VerifyReport(
            expected=kwargs.get("expected"), lanes=lanes, findings=findings
        ),
    )


# ──────────────────────────────────────────────────────────────────────────
# #2069: the python lane restarts coord-serve/coord-web/coord-drive-queue,
# not just coord-agent
# ──────────────────────────────────────────────────────────────────────────


def _machine(name="server", host="server.tailnet"):
    from coord.models import Machine

    return Machine(name=name, host=host)


def _stub_agent_update_ok(monkeypatch, *, target="0.4.111"):
    """Make `_roll_python`'s own `/update` half succeed without a real agent."""
    monkeypatch.setattr(release_cmd, "_post",
                        lambda url, payload, *, timeout: (202, {}, ""))
    monkeypatch.setattr(
        "coord.commands.agent_ops._fetch_pre_started_at", lambda machines: {}
    )
    monkeypatch.setattr(
        "coord.commands.agent_ops._wait_agents_updated",
        lambda machines, *, target_version, timeout, pre_started_at: {
            m.name: {"matched": True} for m in machines
        },
    )


def test_roll_python_restarts_sibling_services_after_the_venv_swap(monkeypatch):
    """The concrete cost this issue names: v0.5.13 carried a fix inside
    coord-serve, but only coord-agent got restarted, so the daemon kept
    serving v0.5.8's code under a v0.5.13 label. `_roll_python` must now call
    `/restart-services` right after `/update` reports success."""
    posts: list[tuple[str, dict]] = []

    def _fake_post(url, payload, *, timeout):
        posts.append((url, payload))
        if url.endswith("/update"):
            return 202, {}, ""
        if url.endswith("/restart-services"):
            return 200, {"units": {
                "coord-serve": {"restarted": True, "detail": "active"},
                "coord-web": {"restarted": None, "detail": "not running on this host"},
                "coord-drive-queue": {"restarted": None, "detail": "not running on this host"},
            }}, ""
        raise AssertionError(f"unexpected POST {url}")

    monkeypatch.setattr(release_cmd, "_post", _fake_post)
    monkeypatch.setattr(
        "coord.commands.agent_ops._fetch_pre_started_at", lambda machines: {}
    )
    monkeypatch.setattr(
        "coord.commands.agent_ops._wait_agents_updated",
        lambda machines, *, target_version, timeout, pre_started_at: {
            m.name: {"matched": True} for m in machines
        },
    )

    ok, detail, serve_unit_ok = release_cmd._roll_python(
        _machine(), target_version="0.4.111", agent_port=7433, timeout=5.0, force=False
    )
    assert ok
    assert "now v0.4.111" in detail
    assert "restarted coord-serve" in detail
    assert serve_unit_ok is True
    urls = [u for u, _ in posts]
    assert urls == [
        "http://server.tailnet:7433/update",
        "http://server.tailnet:7433/restart-services",
    ], "restart-services must be called AFTER update, on the same host"


def test_roll_python_fails_the_lane_when_a_sibling_restart_fails(monkeypatch):
    """#2095: this used to stay `ok=True` — "the venv swap itself succeeded"
    bleeding into "the lane succeeded" — and printed a leading `✓` over a
    line that itself said `FAILED to restart: coord-serve`. That is what
    happened for real during the 2026-08-10 0.5.15 -> 0.5.26 roll (coord-web,
    not coord-serve, but the same code path): the phone dashboard went
    offline and propagation reported success.

    The venv swap is still named in the detail string — that part really did
    happen and is still worth recording — but a sibling this run took down
    and never brought back is a real outage, not a footnote under a `✓`. The
    old justification for staying green was "`coord release verify` will
    catch the resulting skew"; it cannot, because verify grades versions, not
    liveness, and carries no lane for these units at all — see
    `tests/test_release_propagate.py`'s coord-web-liveness-adjacent tests
    (there is deliberately no such lane to test)."""
    _stub_agent_update_ok(monkeypatch)

    def _fake_post(url, payload, *, timeout):
        if url.endswith("/update"):
            return 202, {}, ""
        # #2069: the real endpoint (agent_app.py's restart_services) returns HTTP
        # 500 — not 200 — whenever any unit fails to restart, with the same
        # {"units": {...}} body shape either way. Mocking 200 here would let a
        # since-fixed bug (the caller discarding per-unit detail on a real 500)
        # regress silently.
        return 500, {"units": {
            "coord-serve": {"restarted": False, "detail": "still activating 30s after restart"},
        }}, ""

    monkeypatch.setattr(release_cmd, "_post", _fake_post)
    ok, detail, serve_unit_ok = release_cmd._roll_python(
        _machine(), target_version="0.4.111", agent_port=7433, timeout=5.0, force=False
    )
    assert ok is False, (
        "a sibling this run took down and never brought back must not print "
        "a `✓` over the lane — see coord/commands/release.py's _roll_python"
    )
    assert "now v0.4.111" in detail, "the venv swap itself still happened and is still named"
    assert "FAILED to restart" in detail
    assert "coord-serve" in detail
    assert "verify" not in detail, (
        "must not claim `coord release verify` catches this — it has no "
        "liveness lane for these units at all (#2095)"
    )
    assert serve_unit_ok is False, (
        "coord-serve itself was the sibling that failed — the daemon's own "
        "API-serving unit is not confirmed on target_version"
    )


def test_roll_python_reports_serve_unit_ok_when_only_a_non_daemon_sibling_fails(
    monkeypatch,
):
    """#2095 review: a coord-web (or coord-drive-queue) restart failure must
    still fail the lane's own `ok` (no `✓` over a real outage) — but must
    NOT be indistinguishable from a coord-serve failure to a caller deciding
    whether it's safe to let other hosts' python lanes proceed. coord-serve
    itself restarted and reports target_version fine here; only coord-web
    failed. See coord/commands/release.py's main roll loop, which uses
    `serve_unit_ok` (not `ok`) to decide `daemon_python_failed`."""
    _stub_agent_update_ok(monkeypatch)

    def _fake_post(url, payload, *, timeout):
        if url.endswith("/update"):
            return 202, {}, ""
        return 500, {"units": {
            "coord-serve": {"restarted": True, "detail": "active"},
            "coord-web": {"restarted": False, "detail": "still deactivating 10s after restart"},
        }}, ""

    monkeypatch.setattr(release_cmd, "_post", _fake_post)
    ok, detail, serve_unit_ok = release_cmd._roll_python(
        _machine(), target_version="0.4.111", agent_port=7433, timeout=5.0, force=False
    )
    assert ok is False, "coord-web is still down — the lane itself must not print a ✓"
    assert "FAILED to restart" in detail
    assert "coord-web" in detail
    assert serve_unit_ok is True, (
        "coord-serve itself is fine — only coord-web failed, which must not "
        "block other hosts' python lanes from proceeding"
    )


def test_roll_python_tolerates_an_agent_that_predates_restart_services(monkeypatch):
    """A host on an old agent build has no /restart-services endpoint yet.
    That must not turn a successful venv swap into a failed python lane."""
    _stub_agent_update_ok(monkeypatch)
    monkeypatch.setattr(
        release_cmd, "_post",
        lambda url, payload, *, timeout: (
            (202, {}, "") if url.endswith("/update") else (404, {}, "")
        ),
    )
    ok, detail, serve_unit_ok = release_cmd._roll_python(
        _machine(), target_version="0.4.111", agent_port=7433, timeout=5.0, force=False
    )
    assert ok
    assert "now v0.4.111" in detail
    assert serve_unit_ok is True


def test_roll_python_posts_a_meaningful_initiator(monkeypatch):
    """#2121 item 2: `_roll_python`'s `/update` POST must carry a real
    `cli_initiator`-built string naming the fleet roll, not leave the
    target agent to fall back to its own generic peer/user-agent default."""
    posts: list[tuple[str, dict]] = []

    def _fake_post(url, payload, *, timeout):
        posts.append((url, payload))
        if url.endswith("/update"):
            return 202, {}, ""
        if url.endswith("/restart-services"):
            return 200, {"units": {}}, ""
        raise AssertionError(f"unexpected POST {url}")

    monkeypatch.setattr(release_cmd, "_post", _fake_post)
    monkeypatch.setattr(
        "coord.commands.agent_ops._fetch_pre_started_at", lambda machines: {}
    )
    monkeypatch.setattr(
        "coord.commands.agent_ops._wait_agents_updated",
        lambda machines, *, target_version, timeout, pre_started_at: {
            m.name: {"matched": True} for m in machines
        },
    )

    release_cmd._roll_python(
        _machine(), target_version="0.4.111", agent_port=7433, timeout=5.0, force=False
    )

    update_payload = next(p for u, p in posts if u.endswith("/update"))
    initiator = update_payload.get("initiator")
    assert isinstance(initiator, str)
    assert initiator.startswith("coord release propagate -> server python lane (")


# ──────────────────────────────────────────────────────────────────────────
# #2124: `_roll_units`'s output must name every timer whose state it
# changed — and, distinctly, every one it confirmed already enabled and
# deliberately left alone (the operator-stopped-it-on-purpose case) — so
# "the queue came back mid-roll" (or, after this fix, didn't) is legible in
# `coord release propagate`'s own printed output, not reconstructed
# afterwards from journal timestamps.
# ──────────────────────────────────────────────────────────────────────────


def test_roll_units_names_a_timer_it_started(monkeypatch):
    monkeypatch.setattr(
        release_cmd, "_post",
        lambda url, payload, *, timeout: (200, {
            "ok": True, "units": [], "reloaded": False,
            "timers_enabled": {
                "coord-agent.timer": {"ok": True, "changed": True, "detail": "enabled"},
            },
        }, ""),
    )
    ok, detail = release_cmd._roll_units(_machine(), agent_port=7433)
    assert ok
    assert "enabled timer(s): coord-agent.timer" in detail


def test_roll_units_names_a_timer_it_left_alone(monkeypatch):
    """#2124's acceptance item 3: the exact text an operator greps for after
    a roll to confirm the timer they stopped is still stopped."""
    monkeypatch.setattr(
        release_cmd, "_post",
        lambda url, payload, *, timeout: (200, {
            "ok": True, "units": [], "reloaded": False,
            "timers_enabled": {
                "coord-drive-queue.timer": {
                    "ok": True, "changed": False,
                    "detail": "already enabled (ActiveState=inactive) — left its "
                              "current run state alone",
                },
            },
        }, ""),
    )
    ok, detail = release_cmd._roll_units(_machine(), agent_port=7433)
    assert ok
    assert "left stopped as-is" in detail
    assert "coord-drive-queue.timer" in detail
    # And it must NOT also claim to have enabled it.
    assert "enabled timer(s)" not in detail


def test_roll_units_names_both_in_one_report(monkeypatch):
    """Started and held timers are independent per-unit outcomes — a run
    with one of each must name both, not collapse to whichever it saw last."""
    monkeypatch.setattr(
        release_cmd, "_post",
        lambda url, payload, *, timeout: (200, {
            "ok": True, "units": [], "reloaded": False,
            "timers_enabled": {
                "coord-release-propagate.timer": {
                    "ok": True, "changed": True, "detail": "enabled",
                },
                "coord-drive-queue.timer": {
                    "ok": True, "changed": False, "detail": "already enabled",
                },
            },
        }, ""),
    )
    ok, detail = release_cmd._roll_units(_machine(), agent_port=7433)
    assert ok
    assert "coord-release-propagate.timer" in detail
    assert "coord-drive-queue.timer" in detail


def test_roll_units_fails_the_lane_on_a_failed_timer(monkeypatch):
    """A 500 with a `units` body (agent_app.py's `deploy_units` returns
    exactly this shape on any timer failure) must still be parsed for
    per-timer detail, the same partial-failure convention `_roll_restart`
    already uses for `/restart-services` — never an opaque 'HTTP 500' that
    throws away which timer actually failed."""
    monkeypatch.setattr(
        release_cmd, "_post",
        lambda url, payload, *, timeout: (500, {
            "ok": False, "units": [], "reloaded": False,
            "timers_enabled": {
                "coord-agent.timer": {
                    "ok": False, "changed": False, "detail": "enable failed",
                },
            },
        }, ""),
    )
    ok, detail = release_cmd._roll_units(_machine(), agent_port=7433)
    assert ok is False
    assert "FAILED to enable timer(s)" in detail
    assert "coord-agent.timer" in detail
    assert "enable failed" in detail


def test_roll_units_never_reports_a_timer_it_never_asked_about(monkeypatch):
    """#2124 item 4: a run with no timers at all must not invent a report
    about one — `timers_enabled` empty means nothing about timers is said."""
    monkeypatch.setattr(
        release_cmd, "_post",
        lambda url, payload, *, timeout: (200, {
            "ok": True, "units": [], "reloaded": False, "timers_enabled": {},
        }, ""),
    )
    ok, detail = release_cmd._roll_units(_machine(), agent_port=7433)
    assert ok
    assert "timer" not in detail.lower()


def test_roll_units_holding_a_running_timer_is_not_reported_as_stopped(monkeypatch):
    """#2124 review: `held` (a timer `enable_timers` left alone because it
    was already enabled) covers two very different cases — the operator
    deliberately stopped it (ActiveState=inactive), and the overwhelmingly
    common case of a timer that is enabled and already running fine
    (ActiveState=active). Only the first may say "left stopped as-is" —
    saying it for the second reports a state ("stopped") this call never
    confirmed."""
    monkeypatch.setattr(
        release_cmd, "_post",
        lambda url, payload, *, timeout: (200, {
            "ok": True, "units": [], "reloaded": False,
            "timers_enabled": {
                "coord-agent.timer": {
                    "ok": True, "changed": False,
                    "detail": "already enabled (ActiveState=active) — left its "
                              "current run state alone",
                },
            },
        }, ""),
    )
    ok, detail = release_cmd._roll_units(_machine(), agent_port=7433)
    assert ok
    assert "left stopped as-is" not in detail
    assert "already enabled (unchanged): coord-agent.timer" in detail


def test_roll_units_names_a_masked_unit_left_alone(monkeypatch):
    """#2812: `install_units` reports a masked unit as its own action rather
    than silently overwriting it — `_roll_units` must surface that as a
    named finding, not fold it into "units already current" the way a
    plain unchanged unit is."""
    monkeypatch.setattr(
        release_cmd, "_post",
        lambda url, payload, *, timeout: (200, {
            "ok": True,
            "units": [
                {"name": "coord-release-window.service", "action": "masked",
                 "detail": "masked by an operator — left masked, content "
                           "not refreshed (#2812)"},
            ],
            "reloaded": False, "timers_enabled": {},
        }, ""),
    )
    ok, detail = release_cmd._roll_units(_machine(), agent_port=7433)
    assert ok
    assert "left masked, not refreshed" in detail
    assert "coord-release-window.service" in detail


def test_roll_units_names_a_masked_timer_left_alone(monkeypatch):
    """The timer-enablement half of #2812: `enable_timers`'s own masked
    branch must be named distinctly, not folded into "already enabled" —
    it was never enabled, that is the whole point of the mask."""
    monkeypatch.setattr(
        release_cmd, "_post",
        lambda url, payload, *, timeout: (200, {
            "ok": True, "units": [], "reloaded": False,
            "timers_enabled": {
                "coord-release-window.timer": {
                    "ok": True, "changed": False,
                    "detail": "masked (ActiveState=inactive) — left masked, "
                              "not overriding an operator's explicit mask "
                              "(#2812)",
                },
            },
        }, ""),
    )
    ok, detail = release_cmd._roll_units(_machine(), agent_port=7433)
    assert ok
    assert "left masked as-is (operator mask, #2812)" in detail
    assert "coord-release-window.timer" in detail
    # Must not be misreported under either #2124 bucket.
    assert "left stopped as-is" not in detail
    assert "already enabled (unchanged)" not in detail
    assert "enabled timer(s)" not in detail


def test_roll_units_fails_the_lane_on_a_failed_unit_install(monkeypatch):
    """#2124 review: before this fix, `_roll_units`'s `ok` was derived only
    from `failed_timers`, so a unit whose install itself failed
    (`action == "failed"`, e.g. an unreadable installed unit file) was
    silently reported as a green lane — `changed`/`new` are the only things
    counted for the narrative, and nothing inspected `units[i]["action"]`
    for `"failed"`. This is the exact HTTP-500-with-a-`units`-body shape
    `agent_app.py`'s `deploy_units` returns for that failure."""
    monkeypatch.setattr(
        release_cmd, "_post",
        lambda url, payload, *, timeout: (500, {
            "ok": False,
            "units": [
                {"name": "coord-drive-queue.timer", "action": "failed",
                 "detail": "unreadable installed unit: PermissionError"},
            ],
            "reloaded": False, "timers_enabled": {},
        }, ""),
    )
    ok, detail = release_cmd._roll_units(_machine(), agent_port=7433)
    assert ok is False
    assert "FAILED to install unit(s)" in detail
    assert "coord-drive-queue.timer" in detail
    assert "PermissionError" in detail


def test_roll_units_fails_the_lane_on_a_top_level_error(monkeypatch):
    """A `body["error"]` (e.g. `InstallReport.error` — the reference itself
    couldn't be read) must fail the lane even with an empty `units` list and
    no timer failures."""
    monkeypatch.setattr(
        release_cmd, "_post",
        lambda url, payload, *, timeout: (500, {
            "ok": False, "error": "no reference build found",
            "units": [], "reloaded": False, "timers_enabled": {},
        }, ""),
    )
    ok, detail = release_cmd._roll_units(_machine(), agent_port=7433)
    assert ok is False
    assert "no reference build found" in detail


def test_roll_units_fails_the_lane_on_a_failed_reload(monkeypatch):
    """A daemon-reload that was actually attempted (a unit's bytes changed)
    and failed must fail the lane, decoupled from any timer outcome —
    before this fix, `ok` was `not failed_timers` alone, so a reload
    failure with zero timer failures was reported as a green lane."""
    monkeypatch.setattr(
        release_cmd, "_post",
        lambda url, payload, *, timeout: (500, {
            "ok": False,
            "units": [{"name": "coord-agent.service", "action": "updated", "detail": ""}],
            "changed": True, "reloaded": False, "reload_detail": "reload timed out",
            "timers_enabled": {},
        }, ""),
    )
    ok, detail = release_cmd._roll_units(_machine(), agent_port=7433)
    assert ok is False
    assert "daemon-reload FAILED" in detail
    assert "reload timed out" in detail


def test_roll_units_reload_never_attempted_is_not_a_failure(monkeypatch):
    """`reloaded=False` with no `changed` units means a reload was never
    attempted — nothing to reload, not a failure. Guards against the
    reload-failure check above over-firing on the routine no-op case."""
    monkeypatch.setattr(
        release_cmd, "_post",
        lambda url, payload, *, timeout: (200, {
            "ok": True, "units": [], "changed": False, "reloaded": False,
            "timers_enabled": {},
        }, ""),
    )
    ok, detail = release_cmd._roll_units(_machine(), agent_port=7433)
    assert ok
    assert "daemon-reload FAILED" not in detail


def test_rollback_host_posts_the_caller_supplied_initiator(monkeypatch):
    """#2121: `_rollback_host` is the sibling of `_roll_python` above — same
    fleet-automation shape, same obligation to name itself on the target
    host's audit trail via the `initiator` field."""
    posts: list[tuple[str, dict]] = []

    def _fake_post(url, payload, *, timeout):
        posts.append((url, payload))
        return 202, {}, ""

    monkeypatch.setattr(release_cmd, "_post", _fake_post)
    monkeypatch.setattr(
        release_cmd, "_wait_agent_back", lambda *a, **k: (True, "0.4.110")
    )

    ok, detail = release_cmd._rollback_host(
        _machine(), agent_port=7433, timeout=5.0,
        initiator="coord release propagate -> server rollback (red gate) (john@laptop pid 1)",
    )

    assert ok
    assert "rolled back" in detail
    (url, payload), = posts
    assert url == "http://server.tailnet:7433/rollback"
    assert payload.get("initiator") == (
        "coord release propagate -> server rollback (red gate) (john@laptop pid 1)"
    )


def test_rollback_host_without_an_initiator_omits_the_field(monkeypatch):
    """No caller-supplied initiator must not send a falsy/empty value that
    would shadow the target agent's own peer/user-agent fallback — the key
    should simply be absent."""
    posts: list[tuple[str, dict]] = []

    def _fake_post(url, payload, *, timeout):
        posts.append((url, payload))
        return 202, {}, ""

    monkeypatch.setattr(release_cmd, "_post", _fake_post)
    monkeypatch.setattr(
        release_cmd, "_wait_agent_back", lambda *a, **k: (True, "0.4.110")
    )

    release_cmd._rollback_host(_machine(), agent_port=7433, timeout=5.0)

    (_url, payload), = posts
    assert "initiator" not in payload


def test_release_propagate_red_gate_rollback_names_itself(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """The red-gate rollback inside `coord release propagate` is one of the
    two live call sites of `_rollback_host` (the other is `coord release
    rollback`) — it must build a real initiator, not leave the field off
    and fall back to the generic per-agent default."""
    _stub_lanes(monkeypatch)
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
                 severity="crit")
    captured: list[dict] = []
    monkeypatch.setattr(
        release_cmd, "_rollback_host",
        lambda machine, *, agent_port, timeout, initiator=None: (
            captured.append({"machine": machine.name, "initiator": initiator}),
            (True, "back up"),
        )[1],
    )
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111"],
    )
    assert result.exit_code == 2, result.output
    assert captured, "the red gate must have rolled back at least one host"
    for row in captured:
        assert isinstance(row["initiator"], str)
        assert row["initiator"].startswith("coord release propagate -> ")


def test_release_rollback_cli_names_itself(valid_config_path, monkeypatch):
    """`coord release rollback` is the other live call site of
    `_rollback_host` — same obligation."""
    captured: list[dict] = []
    monkeypatch.setattr(
        release_cmd, "_rollback_host",
        lambda machine, *, agent_port, timeout, initiator=None: (
            captured.append({"machine": machine.name, "initiator": initiator}),
            (True, "back up"),
        )[1],
    )
    result = CliRunner().invoke(
        main,
        ["release", "rollback", "--config", str(valid_config_path), "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert captured
    for row in captured:
        assert isinstance(row["initiator"], str)
        assert row["initiator"].startswith("coord release rollback -> ")


def test_restart_sibling_services_reports_a_mix_of_outcomes(monkeypatch):
    calls = []

    def _fake_post(url, payload, *, timeout):
        calls.append(url)
        # #2069: a mixed outcome with any failed unit is a real HTTP 500 from the
        # endpoint (agent_app.py's restart_services), not a 200 — see the comment
        # in test_roll_python_fails_the_lane_when_a_sibling_restart_fails above.
        return 500, {"units": {
            "coord-serve": {"restarted": True, "detail": "active"},
            "coord-web": {"restarted": False, "detail": "still deactivating"},
            "coord-drive-queue": {"restarted": None, "detail": "not running on this host"},
        }}, ""

    monkeypatch.setattr(release_cmd, "_post", _fake_post)
    ok, detail, failed = release_cmd._restart_sibling_services(
        _machine(), agent_port=7433, timeout=5.0
    )
    assert not ok
    assert "restarted coord-serve" in detail
    assert "not running here: coord-drive-queue" in detail
    assert "FAILED to restart: coord-web" in detail
    assert calls == ["http://server.tailnet:7433/restart-services"]
    # #2095 review: the per-unit failed mapping is what `_roll_python` keys
    # its `serve_unit_ok` off of — coord-web failed, coord-serve did not.
    assert failed == {"coord-web": "still deactivating"}


def test_restart_sibling_services_tolerates_a_pre_2069_agent(monkeypatch):
    """#2095: HTTP 404 from `/restart-services` means this agent build
    predates the endpoint (#2069) — there was never a channel here to have
    restarted anything through, which is a different thing from a channel
    that existed and failed. Tri-state `None`, not `False`, is what tells
    `_roll_python` not to fail the lane over it (see
    test_roll_python_tolerates_an_agent_that_predates_restart_services)."""
    monkeypatch.setattr(
        release_cmd, "_post", lambda url, payload, *, timeout: (404, {}, "")
    )
    ok, detail, failed = release_cmd._restart_sibling_services(
        _machine(), agent_port=7433, timeout=5.0
    )
    assert ok is None
    assert "404" in detail
    assert failed == {}


# ──────────────────────────────────────────────────────────────────────────
# #2898: the tui lane rolls its OWN channel
# ──────────────────────────────────────────────────────────────────────────


def test_roll_tui_never_passes_a_coordinator_version(monkeypatch):
    """THE #2898 REGRESSION TEST for the roll half.

    This used to run `coord tui update --version <this run's target>`, correct
    only while one `v*` tag stamped both repos (#1242). After the split that
    argument names a tag in the COORDINATOR's channel, which coord-tui's
    Releases have never heard of — it would 404 on every run and report a
    failed tui lane for a fleet that is in fact perfectly current.
    """
    seen: list[list[str]] = []

    class _Proc:
        returncode = 0
        stdout = "Installed /home/u/.local/bin/coord-tui -- coord-tui reports 0.2.7."
        stderr = ""

    def _run(argv, **kwargs):
        seen.append(list(argv))
        return _Proc()

    monkeypatch.setattr("subprocess.run", _run)
    ok, detail = release_cmd._roll_tui(_machine(name="server"), local_name="server")

    assert ok is True
    assert len(seen) == 1
    assert seen[0][-2:] == ["tui", "update"], seen[0]
    assert "--version" not in seen[0], seen[0]
    # What actually landed is read back out, not assumed.
    assert "0.2.7" in detail
    assert rp.CHANNEL_TUI in detail


def test_roll_tui_reports_the_idempotent_path_version(monkeypatch):
    class _Proc:
        returncode = 0
        stdout = "coord-tui is already v0.2.7 at /home/u/.local/bin/coord-tui -- nothing to do (--force to reinstall)."
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda argv, **kw: _Proc())
    ok, detail = release_cmd._roll_tui(_machine(name="server"), local_name="server")
    assert ok is True
    assert "0.2.7" in detail


def test_roll_tui_still_succeeds_when_the_version_cannot_be_parsed(monkeypatch):
    """The roll is not a failure just because its output changed shape — the
    version is journalling detail, the exit code is the verdict."""
    class _Proc:
        returncode = 0
        stdout = "something entirely unexpected"
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda argv, **kw: _Proc())
    ok, detail = release_cmd._roll_tui(_machine(name="server"), local_name="server")
    assert ok is True
    assert rp.CHANNEL_TUI in detail


def test_roll_tui_on_a_remote_host_is_unrollable_not_failed(monkeypatch):
    """#2052: `ok=None`. A lane that reports "there is no remote install path"
    in its own message cannot also be evidence this run went wrong — that is
    what made `--rollback-on-red` revert three good python rolls. The advice
    it prints must not name a coordinator version either (#2898)."""
    def _boom(*a, **k):  # pragma: no cover - must never be reached
        raise AssertionError("a remote host must not shell out")

    monkeypatch.setattr("subprocess.run", _boom)
    ok, detail = release_cmd._roll_tui(_machine(name="macmini"), local_name="server")
    assert ok is None
    assert "no remote install path" in detail
    assert "--version" not in detail
    assert rp.CHANNEL_TUI in detail


def test_roll_tui_surfaces_a_failed_update(monkeypatch):
    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "error: could not resolve coord-tui's latest release"

    monkeypatch.setattr("subprocess.run", lambda argv, **kw: _Proc())
    ok, detail = release_cmd._roll_tui(_machine(name="server"), local_name="server")
    assert ok is False
    assert "could not resolve" in detail


def test_roll_tui_treats_an_empty_channel_as_unrollable_not_failed(monkeypatch):
    """#2981: `coord tui update` exits `EXIT_EMPTY_CHANNEL` (not the generic
    `1`) specifically when the channel has never published a release —
    `JDonaghy/coord-tui`'s actual state, which 404s from `/releases/latest`
    on every run forever. `_roll_tui` must read that exit code back into
    `ok=None`, exactly like the "no remote install path" case, so this
    lane never lands inside a run's attempted scope and can never be
    grounds for `--rollback-on-red` — see
    `test_2981_an_empty_tui_channel_does_not_roll_back_the_python_lanes` for
    the full propagate-level regression this unlocks."""
    from coord.commands.tui import EXIT_EMPTY_CHANNEL

    class _Proc:
        returncode = EXIT_EMPTY_CHANNEL
        stdout = ""
        stderr = (
            "error: JDonaghy/coord-tui has no published release to resolve "
            "a latest version from (GET .../releases/latest -> 404 — this "
            "repo has zero releases/tags)"
        )

    monkeypatch.setattr("subprocess.run", lambda argv, **kw: _Proc())
    ok, detail = release_cmd._roll_tui(_machine(name="server"), local_name="server")
    assert ok is None
    assert "not a roll failure" in detail
    assert "no published release" in detail


def test_roll_tui_a_real_failure_still_blocks_after_the_2981_fix(monkeypatch):
    """The other half of the #2981 acceptance bar: a GENUINE failure (a real
    release exists, but e.g. the asset/checksum/install step failed) must
    still exit the generic `1`, and `_roll_tui` must still report `ok=False`
    -- still blocking, still eligible to trigger a rollback."""
    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "error: checksum mismatch for coord-tui-x86_64-linux"

    monkeypatch.setattr("subprocess.run", lambda argv, **kw: _Proc())
    ok, detail = release_cmd._roll_tui(_machine(name="server"), local_name="server")
    assert ok is False
    assert "checksum mismatch" in detail


# ──────────────────────────────────────────────────────────────────────────
# #3047 part 2: `--drain` — a resident loop instead of an operator re-running
# `coord release propagate` by hand until a poll happens to land inside the
# (normally seconds-long) #2854 between-legs window.
# ──────────────────────────────────────────────────────────────────────────


def test_drain_rejects_dry_run(valid_config_path, state_dir, no_network, monkeypatch):
    """A dry run changes nothing on any host, so draining it would never
    converge — refuse the combination up front rather than spin forever."""
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--drain", "--dry-run"],
    )
    assert result.exit_code != 0
    assert "--dry-run" in result.output
    assert _records(state_dir) == []


def test_drain_converges_in_one_attempt_when_nothing_is_behind(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """The fleet is already on the target — `--drain` must recognise that on
    the very first attempt and exit without ever sleeping/polling again."""
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.111"], "server": ["0.4.111"]})
    slept: list[float] = []
    monkeypatch.setattr(release_cmd, "_sleep", lambda s: slept.append(s))
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--drain"],
    )
    assert result.exit_code == 0, result.output
    assert not slept, "already up to date — --drain must not poll a second time"
    assert len(_records(state_dir)) == 1
    assert "every host reached the target after 1 attempt" in result.output


def test_drain_gives_up_at_its_deadline_and_reports_stragglers(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """The daemon (`server`) is permanently busy, so every attempt defers —
    exactly the case a timer alone would retry forever. `--give-up-after 0` means
    the very next poll after the first attempt is already overdue, so this
    must stop after exactly one attempt, exit non-zero, and name the hosts
    still behind rather than hang."""
    monkeypatch.setattr(
        release_cmd, "_fetch_board",
        lambda: ({"assignments": [{"machine_name": "server", "issue_number": 9,
                                   "status": "RUNNING"}]}, None),
    )
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
                 daemon="server")
    slept: list[float] = []
    monkeypatch.setattr(release_cmd, "_sleep", lambda s: slept.append(s))
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--drain", "--give-up-after", "0", "--drain-interval", "1"],
    )
    assert result.exit_code == 1, result.output
    assert len(slept) == 1, "must poll once (after attempt 1) before giving up"
    records = _records(state_dir)
    assert len(records) == 1
    assert records[0]["status"] == rp.STATUS_DEFERRED
    assert "deadline" in result.output
    assert "laptop" in result.output and "server" in result.output


def test_drain_rolls_a_host_once_it_frees_up_on_a_later_poll(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """The scenario in #3047's own report: `laptop` is busy on the first
    poll (so only `server`, the free daemon, rolls) and free by the second
    (so `laptop` catches up too) — `--drain` must keep going after the first
    attempt, report the transition, and stop once nothing is left behind."""
    calls = {"n": 0}

    def _fetch_board():
        calls["n"] += 1
        if calls["n"] == 1:
            return (
                {"assignments": [{"machine_name": "laptop", "issue_number": 9,
                                  "status": "RUNNING"}]},
                None,
            )
        return ({"assignments": []}, None)

    monkeypatch.setattr(release_cmd, "_fetch_board", _fetch_board)
    _stub_lanes(monkeypatch)
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
                 daemon="server")
    slept: list[float] = []
    monkeypatch.setattr(release_cmd, "_sleep", lambda s: slept.append(s))
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--drain", "--give-up-after", "60"],
    )
    assert result.exit_code == 0, result.output
    assert len(slept) == 1, "must poll exactly once between the two attempts"
    records = _records(state_dir)
    assert len(records) == 2
    assert records[0]["status"] == rp.STATUS_VERIFIED
    laptop_lane_1 = next(l for l in records[0]["lanes"] if l["host"] == "laptop")
    assert laptop_lane_1["ok"] is None  # deferred, never attempted
    assert records[1]["status"] == rp.STATUS_VERIFIED
    assert any(l["host"] == "laptop" and l["ok"] for l in records[1]["lanes"])
    assert "laptop: reached the target" in result.output
    assert "every host reached the target after 2 attempt(s)" in result.output


def test_drain_no_cordon_stops_after_first_non_deferred_attempt(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """#3047 review: `--no-cordon --drain` is a distinct, previously-untested
    code path — and turned out to be genuinely broken. `_drain_remaining_
    hosts` reads `record.cordons`, which `_apply_cordons` never populates at
    all with `--no-cordon` (`plan_cordons(enabled=False, ...)` short-circuits
    before touching any of `cordoned`/`collateral_spared`/`stuck_in_cooldown`/
    `unknown`), so `remaining` is the empty set on EVERY `--no-cordon`
    attempt. An earlier version of `_run_drain` checked `not remaining`
    before the `--no-cordon` branch, so it always won: `laptop` here is still
    on the OLD version (busy, deferred) after `server` alone rolls, yet the
    loop reported "every host reached the target after 1 attempt(s)" and
    exited 0 — a false convergence claim. It must instead take the honest
    `--no-cordon`-specific exit that says it cannot confirm every host
    converged."""
    monkeypatch.setattr(
        release_cmd, "_fetch_board",
        lambda: ({"assignments": [{"machine_name": "laptop", "issue_number": 9,
                                   "status": "RUNNING"}]}, None),
    )
    _stub_lanes(monkeypatch)
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
                 daemon="server")
    slept: list[float] = []
    monkeypatch.setattr(release_cmd, "_sleep", lambda s: slept.append(s))
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--drain", "--no-cordon", "--give-up-after", "60"],
    )
    assert result.exit_code == 0, result.output
    assert not slept, "the honest --no-cordon exit must not poll again"
    assert "--no-cordon: stopping after the first non-deferred attempt" in result.output
    assert "every host reached the target" not in result.output, (
        "laptop never rolled — this must not claim full convergence"
    )
    records = _records(state_dir)
    assert len(records) == 1
    assert records[0]["status"] == rp.STATUS_VERIFIED


def test_drain_no_cordon_keeps_polling_while_fully_deferred(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """The other half of the same fix: a fully-deferred `--no-cordon` attempt
    (nothing rolled at all) must NOT be mistaken for convergence either — it
    must keep polling until something actually happens or --give-up-after
    passes, exactly like a cordoned drain does."""
    monkeypatch.setattr(
        release_cmd, "_fetch_board",
        lambda: ({"assignments": [{"machine_name": "laptop", "issue_number": 9,
                                   "status": "RUNNING"},
                                  {"machine_name": "server", "issue_number": 10,
                                   "status": "RUNNING"}]}, None),
    )
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
                 daemon="server")
    slept: list[float] = []
    monkeypatch.setattr(release_cmd, "_sleep", lambda s: slept.append(s))
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--drain", "--no-cordon",
         "--give-up-after", "0", "--drain-interval", "1"],
    )
    assert result.exit_code == 1, result.output
    assert len(slept) == 1, "a fully-deferred no-cordon attempt must poll again, not stop"
    assert "every host reached the target" not in result.output
    assert "--give-up-after deadline" in result.output


# ── #3047 review: `--drain --json` must be one parseable document ────────


def _stdout_only_runner() -> CliRunner:
    """A `CliRunner` whose `result.stdout` really is stdout ALONE.

    Click < 8.2 defaults to `mix_stderr=True`, which aliases `sys.stderr`
    onto `sys.stdout` for the duration of the invocation — `result.stdout`
    then holds BOTH streams merged, identically to `result.output`. That
    silently defeats the very thing the `--drain --json` tests below assert:
    `_run_drain`'s `[drain] ...` progress lines go to stderr (`_echo` passes
    `err=json_mode`) and would be spliced in front of the JSON document,
    making `json.loads` raise before any assertion ran.

    Click >= 8.2 always separates the two streams and REMOVED the parameter,
    so passing it unconditionally raises `TypeError` there. `pyproject.toml`
    only requires `click>=8.1`, so both are live installs in practice (the
    fleet venv has 8.1.6; a fresh `pip install -e '.[dev]'` resolves 8.5.0).
    Detect the parameter rather than pin a version.
    """
    if "mix_stderr" in inspect.signature(CliRunner.__init__).parameters:
        return CliRunner(mix_stderr=False)  # click < 8.2
    return CliRunner()  # click >= 8.2 — always separated


def test_stdout_only_runner_actually_separates_the_streams():
    """Guard the guard: if `_stdout_only_runner` ever silently stopped
    separating the streams (a future click bump renaming the parameter, say),
    the two `--drain --json` tests below would go green for the wrong reason
    — stderr would simply be merged in and `json.loads` would... still fail,
    but a laxer future assertion might not. Pin the property directly."""
    import click

    @click.command()
    def _cmd():
        click.echo("diagnostic line", err=True)
        click.echo('{"ok": true}')

    result = _stdout_only_runner().invoke(_cmd, [])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"ok": True}
    assert "diagnostic line" not in result.stdout


def test_drain_json_emits_a_single_aggregated_document(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """Before this fix, `--drain --json` echoed one full JSON record PER
    ATTEMPT (`_finish` unconditionally dumps `record.to_dict()` when
    `--json` is set) interleaved with the loop's own plain-text `[drain]
    ...` status lines on the SAME stdout — not a single parseable JSON
    stream for a script reading the output. `--drain --json` must instead
    produce exactly one JSON document: `_run_drain`'s own aggregated
    summary."""
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.111"], "server": ["0.4.111"]})
    result = _stdout_only_runner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--drain", "--json"],
    )
    assert result.exit_code == 0, result.output
    # Must parse as exactly one JSON document on STDOUT ALONE — `json.loads`
    # raises on trailing data, which is exactly what N interleaved documents
    # (or this attempt's own diagnostic lines) would produce. `result.stdout`
    # (not the merged `result.output`) is what a script piping this
    # command's stdout would actually see.
    payload = json.loads(result.stdout)
    assert payload["drain_status"] == "converged"
    assert payload["attempts"] == 1
    assert payload["remaining"] == []
    assert payload["last_attempt"]["target_version"] == "0.4.111"
    assert len(_records(state_dir)) == 1


def test_drain_json_across_multiple_attempts_still_emits_one_document(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """Same guarantee across more than one poll — the case that actually
    interleaved multiple JSON documents pre-fix (one per attempt, plus the
    transition lines between them)."""
    calls = {"n": 0}

    def _fetch_board():
        calls["n"] += 1
        if calls["n"] == 1:
            return (
                {"assignments": [{"machine_name": "laptop", "issue_number": 9,
                                  "status": "RUNNING"}]},
                None,
            )
        return ({"assignments": []}, None)

    monkeypatch.setattr(release_cmd, "_fetch_board", _fetch_board)
    _stub_lanes(monkeypatch)
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.110"], "server": ["0.4.110"]},
                 daemon="server")
    monkeypatch.setattr(release_cmd, "_sleep", lambda s: None)
    result = _stdout_only_runner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--drain", "--give-up-after", "60", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["drain_status"] == "converged"
    assert payload["attempts"] == 2
    assert payload["remaining"] == []
    assert len(_records(state_dir)) == 2
