"""Black-box tests for the #2583 min-releases-behind auto-roll gate.

`coord release propagate` and `coord release nightly-window` both attempt a
roll on any delta at all today. This gate holds either command below a
configurable ``propagation.min_releases_behind`` (or per-invocation
``--min-behind``) — a REPORTED no-op, not a silent one, and one that must
never cordon a host or set the #2587 roll-pending marker while holding
(that would be "touching" the fleet, exactly what the operator disabled the
auto-roll lane over — see the issue's own evidence section).

Covers:

* config parsing (`propagation:` block; `PropagationConfig` defaults);
* the default (unset threshold) never even queries the delta — byte-identical
  to today, no second PyPI read;
* below threshold: `coord release propagate` holds without cordoning ANY
  host, and `coord release nightly-window` holds without touching the
  roll-pending marker (setting one, clearing one, or firing one);
* at/above threshold: both commands proceed exactly as before this gate
  existed;
* `--min-behind` overrides `coordinator.yml`;
* an unresolvable delta (unparseable version, unreachable index) gates OPEN
  — never holds on missing data.

Nothing here touches a real fleet or the real PyPI index: `_releases_behind_
count`, `coord.release_verify.gather`/`.verify`, `_fetch_board`, and the
per-lane executors are all seams, same as the sibling
`test_cli_release_propagate.py` / `test_cli_release_window.py` modules this
one deliberately mirrors.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from coord import release_propagate as rp
from coord import release_window as rw
from coord.cli import main
from coord.commands import drive_queue as dq_cmd
from coord.commands import release as release_cmd
from coord.config import ConfigError, parse_mapping
from coord.config import PropagationConfig
from coord.drive_queue import RollPending

from .conftest import VALID_CONFIG


# ── config parsing ──────────────────────────────────────────────────────────


def _minimal_raw(**propagation_overrides):
    raw = {
        "repos": [{"name": "r", "github": "acme/r"}],
        "machines": [{"name": "m", "host": "m.tailnet"}],
    }
    if propagation_overrides:
        raw["propagation"] = propagation_overrides
    return raw


def test_absent_propagation_block_defaults_to_one():
    """No `propagation:` block == `min_releases_behind=1` == today's
    behaviour: any delta at all is enough."""
    config = parse_mapping(_minimal_raw())
    assert config.propagation == PropagationConfig()
    assert config.propagation.min_releases_behind == 1


def test_propagation_block_is_parsed():
    config = parse_mapping(_minimal_raw(min_releases_behind=10))
    assert config.propagation.min_releases_behind == 10


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, "10", None])
def test_invalid_min_releases_behind_is_rejected(bad):
    with pytest.raises(ConfigError):
        parse_mapping(_minimal_raw(min_releases_behind=bad))


def test_unknown_propagation_key_is_rejected():
    with pytest.raises(ConfigError):
        parse_mapping({
            "repos": [{"name": "r", "github": "acme/r"}],
            "machines": [{"name": "m", "host": "m.tailnet"}],
            "propagation": {"not_a_real_field": 1},
        })


def test_propagation_block_must_be_a_mapping():
    with pytest.raises(ConfigError):
        parse_mapping({
            "repos": [{"name": "r", "github": "acme/r"}],
            "machines": [{"name": "m", "host": "m.tailnet"}],
            "propagation": ["not", "a", "mapping"],
        })


# ── _resolve_min_behind: flag > config > default ────────────────────────────


def test_resolve_min_behind_flag_wins_over_config():
    config = parse_mapping(_minimal_raw(min_releases_behind=10))
    assert release_cmd._resolve_min_behind(3, config) == 3


def test_resolve_min_behind_falls_back_to_config():
    config = parse_mapping(_minimal_raw(min_releases_behind=10))
    assert release_cmd._resolve_min_behind(None, config) == 10


def test_resolve_min_behind_defaults_to_one():
    config = parse_mapping(_minimal_raw())
    assert release_cmd._resolve_min_behind(None, config) == 1


# ── shared fixtures, mirroring test_cli_release_propagate.py /
#    test_cli_release_window.py ───────────────────────────────────────────


@pytest.fixture(autouse=True)
def _own_pause_store(tmp_path, monkeypatch):
    """Per-test `$HOME` — see the sibling modules' identical fixture for the
    full #2170 write-up on why this matters even for tests that never call
    `coord pause`: the cordon/roll-pending stores this gate must NOT touch
    live under `$HOME`, and a shared home would let one test's (non-)state
    leak into the next."""
    home = tmp_path / "home"
    (home / ".coord").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state"
    d.mkdir()
    monkeypatch.setattr(release_cmd, "_state_dir", lambda: d)
    return d


@pytest.fixture()
def no_network(monkeypatch):
    monkeypatch.setattr(release_cmd, "_fetch_board", lambda: ({}, None))
    monkeypatch.setattr(
        release_cmd, "_post",
        lambda *a, **k: pytest.fail("no test should POST without saying so"),
    )
    monkeypatch.setattr(release_cmd, "_interactive_session_busy", lambda config: [])


def _serve_health(host: str) -> dict:
    return {
        "version": "0.5.31",
        "health": {"schema": 1, "results": [
            {"check_id": "spawned_coord", "subject": "coord-serve",
             "severity": "ok", "values": {"unit": "coord-serve", "pid": 1,
                                          "version": "0.5.31"}},
        ]},
    }


def _stub_verify(monkeypatch, *, versions: dict[str, list], daemon: str = "server"):
    """Same seam `test_cli_release_propagate.py::_stub_verify` uses,
    trimmed to what this module needs (no findings/severity knobs)."""
    from coord import release_verify as rv

    lanes = [
        rv.Lane(host=host, lane="~/.coord-venv", version=v)
        for host, vs in versions.items()
        for v in vs
    ]
    machine_health = {daemon: _serve_health(daemon)} if daemon else {}
    monkeypatch.setattr(rv, "gather",
                        lambda *a, **k: (machine_health, {}, None, daemon or "daemon"))
    monkeypatch.setattr(
        rv, "verify",
        lambda **kwargs: rv.VerifyReport(
            expected=kwargs.get("expected"), lanes=lanes, findings=[]
        ),
    )


def _stub_behind(monkeypatch, *, behind: int | None, warning: str | None = None):
    calls = []

    def _fake(current_version, **kwargs):
        calls.append(current_version)
        return behind, warning

    monkeypatch.setattr(release_cmd, "_releases_behind_count", _fake)
    return calls


def _records_propagate(state_dir):
    return rp.read_records(state_dir)


def _records_window(state_dir):
    return rw.read_records(state_dir)


# ── `coord release propagate` ───────────────────────────────────────────────


def test_default_threshold_never_queries_the_delta(valid_config_path, state_dir,
                                                    no_network, monkeypatch):
    """#2583 acceptance: an absent `propagation:` block (and no `--min-behind`)
    must be byte-identical to today — including never spending the extra
    PyPI read this gate needs above threshold 1."""
    monkeypatch.setattr(
        release_cmd, "_releases_behind_count",
        lambda *a, **k: pytest.fail("the gate must not run at the default threshold"),
    )
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.111"], "server": ["0.4.111"]})
    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111"],
    )
    assert result.exit_code == 0, result.output
    record = _records_propagate(state_dir)[0]
    assert record["status"] == rp.STATUS_UP_TO_DATE
    assert record["min_releases_behind"] == 1
    assert record["releases_behind"] is None


def test_below_threshold_holds_without_cordoning_any_host(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """The explicit #2583 acceptance test: a held run cordons NOTHING, even
    though both hosts here are genuinely behind the target and would
    otherwise be cordoned to drain (#2101)."""
    cordon_calls = []
    monkeypatch.setattr(
        release_cmd, "_apply_cordons",
        lambda **kwargs: cordon_calls.append(kwargs) or {},
    )
    behind_calls = _stub_behind(monkeypatch, behind=4)
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.100"], "server": ["0.4.100"]})

    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--daemon-host", "server", "--min-behind", "10"],
    )
    assert result.exit_code == 0, result.output
    assert cordon_calls == []
    assert behind_calls == ["0.4.100"]
    assert "holding: 4 behind, threshold 10" in result.output

    record = _records_propagate(state_dir)[0]
    assert record["status"] == rp.STATUS_HOLDING
    assert record["status"] in rp.NO_OP_STATUSES
    assert record["releases_behind"] == 4
    assert record["min_releases_behind"] == 10
    assert record["cordons"] == {}


def test_at_threshold_rolls_exactly_as_before(valid_config_path, state_dir,
                                               no_network, monkeypatch):
    """At/above the threshold, behaviour is unchanged: verify + rollback-on-
    red still apply, and the roll proceeds."""
    calls = []

    def _python(machine, **kwargs):
        calls.append(("python", machine.name))
        return True, "now v0.4.111", True

    def _units(machine, **kwargs):
        calls.append(("units", machine.name))
        return True, "1 unit(s) refreshed"

    def _tui(machine, **kwargs):
        calls.append(("tui", machine.name))
        return True, "coord-tui now v0.4.111"

    monkeypatch.setattr(release_cmd, "_roll_python", _python)
    monkeypatch.setattr(release_cmd, "_roll_units", _units)
    monkeypatch.setattr(release_cmd, "_roll_tui", _tui)
    _stub_behind(monkeypatch, behind=10)
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.100"], "server": ["0.4.100"]})

    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--daemon-host", "server", "--min-behind", "10"],
    )
    assert result.exit_code == 0, result.output
    record = _records_propagate(state_dir)[0]
    assert record["status"] == rp.STATUS_VERIFIED
    assert record["releases_behind"] == 10
    assert record["min_releases_behind"] == 10
    assert ("python", "server") in calls


def test_min_behind_flag_overrides_a_higher_config_value(tmp_path, state_dir,
                                                          no_network, monkeypatch):
    config_path = tmp_path / "coordinator.yml"
    config_path.write_text(VALID_CONFIG + "\npropagation:\n  min_releases_behind: 20\n")
    behind_calls = _stub_behind(monkeypatch, behind=4)
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.111"], "server": ["0.4.111"]})

    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(config_path),
         "--target", "0.4.111", "--min-behind", "1"],
    )
    assert result.exit_code == 0, result.output
    # threshold 1 -> the gate never even runs, exactly like the unset default.
    assert behind_calls == []
    record = _records_propagate(state_dir)[0]
    assert record["min_releases_behind"] == 1
    assert record["status"] != rp.STATUS_HOLDING


def test_config_driven_threshold_holds_without_a_flag(tmp_path, state_dir,
                                                       no_network, monkeypatch):
    config_path = tmp_path / "coordinator.yml"
    config_path.write_text(VALID_CONFIG + "\npropagation:\n  min_releases_behind: 10\n")
    cordon_calls = []
    monkeypatch.setattr(
        release_cmd, "_apply_cordons",
        lambda **kwargs: cordon_calls.append(kwargs) or {},
    )
    _stub_behind(monkeypatch, behind=3)
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.100"], "server": ["0.4.100"]})

    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(config_path),
         "--target", "0.4.111", "--daemon-host", "server"],
    )
    assert result.exit_code == 0, result.output
    assert cordon_calls == []
    record = _records_propagate(state_dir)[0]
    assert record["status"] == rp.STATUS_HOLDING
    assert "holding: 3 behind, threshold 10" in result.output


def test_an_unresolvable_delta_gates_open_not_held(valid_config_path, state_dir,
                                                    no_network, monkeypatch):
    """#1834's rule: missing data is not evidence of agreement. An index
    that cannot be read must not silently hold a fleet that genuinely needs
    a roll — it proceeds exactly as if the gate were not configured."""
    _stub_behind(monkeypatch, behind=None, warning="could not read the PyPI simple index")
    _stub_verify(monkeypatch, versions={"laptop": ["0.4.111"], "server": ["0.4.111"]})

    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.4.111", "--min-behind", "10"],
    )
    assert result.exit_code == 0, result.output
    assert "warning: could not read the PyPI simple index" in result.output
    assert rp.STATUS_HOLDING not in result.output
    # A fleet already on target still rolls nothing, but for the ordinary
    # up-to-date reason, not because the gate held it.
    record = _records_propagate(state_dir)[0]
    assert record["status"] == rp.STATUS_UP_TO_DATE


# ── `coord release nightly-window` ──────────────────────────────────────────


def _pending(**overrides):
    kwargs = {"target_version": "0.4.111", "set_at": 1000.0, "reason": "nightly-window"}
    kwargs.update(overrides)
    return RollPending(**kwargs)


def _stub_window_verify(monkeypatch, *, daemon_version: str | None, daemon: str = "server"):
    from coord import release_verify as rv

    lanes = [rv.Lane(host=daemon, lane="~/.coord-venv", version=daemon_version)]
    machine_health = {daemon: _serve_health(daemon)}
    monkeypatch.setattr(rv, "gather",
                        lambda *a, **k: (machine_health, {}, None, daemon))
    monkeypatch.setattr(
        rv, "verify",
        lambda **kwargs: rv.VerifyReport(expected=kwargs.get("expected"), lanes=lanes,
                                         findings=[]),
    )


def _stub_propagate(monkeypatch, *, status: str, exit_code: int, output: str = "ok"):
    calls = []

    def _fake(**kwargs):
        calls.append(kwargs)
        return status, exit_code, output, None

    monkeypatch.setattr(release_cmd, "_run_propagate", _fake)
    return calls


def test_window_default_threshold_never_queries_the_delta(
    valid_config_path, state_dir, no_network, monkeypatch
):
    monkeypatch.setattr(
        release_cmd, "_releases_behind_count",
        lambda *a, **k: pytest.fail("the gate must not run at the default threshold"),
    )
    _stub_window_verify(monkeypatch, daemon_version="0.4.111")

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.4.111", "--daemon-host", "server"],
    )
    assert result.exit_code == 0, result.output
    record = _records_window(state_dir)[0]
    assert record["status"] == rw.STATUS_UP_TO_DATE
    assert record["min_releases_behind"] == 1
    assert record["releases_behind"] is None


def test_window_below_threshold_holds_and_never_sets_a_marker(
    valid_config_path, state_dir, no_network, monkeypatch
):
    _stub_behind(monkeypatch, behind=4)
    _stub_window_verify(monkeypatch, daemon_version="0.4.100")
    prop_calls = _stub_propagate(monkeypatch, status=rp.STATUS_VERIFIED, exit_code=0)

    assert dq_cmd.read_roll_pending() is None
    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.4.111", "--daemon-host", "server", "--min-behind", "10"],
    )
    assert result.exit_code == 0, result.output
    assert prop_calls == []
    assert dq_cmd.read_roll_pending() is None
    assert "holding: 4 behind, threshold 10" in result.output

    record = _records_window(state_dir)[0]
    assert record["status"] == rw.STATUS_HOLDING
    assert record["status"] in rw.OK_STATUSES
    assert record["releases_behind"] == 4
    assert record["min_releases_behind"] == 10


def test_window_below_threshold_leaves_an_existing_marker_untouched(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """A marker set before this threshold existed (or by a lower one) must
    not be fired, cleared, or replaced by a holding run — it is left for a
    later, above-threshold run to pick back up."""
    _stub_behind(monkeypatch, behind=4)
    _stub_window_verify(monkeypatch, daemon_version="0.4.100")
    prop_calls = _stub_propagate(monkeypatch, status=rp.STATUS_VERIFIED, exit_code=0)
    dq_cmd.write_roll_pending(_pending())

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.4.111", "--daemon-host", "server", "--min-behind", "10"],
    )
    assert result.exit_code == 0, result.output
    assert prop_calls == []
    pending = dq_cmd.read_roll_pending()
    assert pending is not None
    assert pending.target_version == "0.4.111"
    assert pending.set_at == 1000.0  # untouched


def test_window_at_threshold_sets_the_marker_exactly_as_before(
    valid_config_path, state_dir, no_network, monkeypatch
):
    _stub_behind(monkeypatch, behind=10)
    _stub_window_verify(monkeypatch, daemon_version="0.4.100")
    prop_calls = _stub_propagate(monkeypatch, status=rp.STATUS_VERIFIED, exit_code=0)

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.4.111", "--daemon-host", "server", "--min-behind", "10"],
    )
    assert result.exit_code == 0, result.output
    assert prop_calls == []  # nothing pending yet -> set-and-return, same as always
    pending = dq_cmd.read_roll_pending()
    assert pending is not None
    assert pending.target_version == "0.4.111"

    record = _records_window(state_dir)[0]
    assert record["status"] == rw.STATUS_ROLL_PENDING
    assert record["releases_behind"] == 10
    assert record["min_releases_behind"] == 10
    # #2870: the marker itself carries the threshold it was armed at.
    assert pending.min_releases_behind == 10


def test_2870_a_marker_armed_below_the_fleet_default_still_discharges_at_its_own_threshold(
    tmp_path, state_dir, no_network, monkeypatch
):
    """#2870's own regression test: `nightly-window --min-behind 1` against a
    fleet configured `min_releases_behind: 5` must not produce a marker that
    can never clear. Before the fix, the marker carried no threshold at all,
    so every discharge attempt (this command's own belt-and-braces `coord
    release propagate` call, and every re-entry the drive-queue tick fires
    via `coord-release-window.service`) re-resolved the fleet default (5)
    and held forever at "N behind, threshold 5" — exactly the 2026-08-28
    incident (`v0.5.259`/`v0.5.260`, held ~40 minutes with two machines
    idle, `alert: (none)` throughout)."""
    config_path = tmp_path / "coordinator.yml"
    config_path.write_text(VALID_CONFIG + "\npropagation:\n  min_releases_behind: 5\n")

    # ── night 1: armed at --min-behind 1, well below the fleet default.
    _stub_behind(monkeypatch, behind=1)
    _stub_window_verify(monkeypatch, daemon_version="0.4.100")
    arm = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(config_path),
         "--target", "0.4.111", "--daemon-host", "server", "--min-behind", "1"],
    )
    assert arm.exit_code == 0, arm.output
    armed = dq_cmd.read_roll_pending()
    assert armed is not None
    assert armed.min_releases_behind == 1  # NOT the fleet default of 5

    # ── night 2 (or the tick's own `coord-release-window.service` re-entry —
    #    that unit's ExecStart carries the SAME operator-added `--min-behind
    #    1` every time it runs, exactly like `coord-release-window.service`
    #    in the issue's own evidence section): same marker, same target.
    #    Before #2870, the belt-and-braces `coord release propagate` call
    #    never passed `--min-behind` AT ALL regardless of what this run's own
    #    gate resolved, so the real subprocess fell back to the fleet default
    #    (5) and held. After the fix it is gated at the marker's own arm
    #    threshold (1).
    prop_calls = _stub_propagate(monkeypatch, status=rp.STATUS_VERIFIED, exit_code=0)
    fire = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(config_path),
         "--target", "0.4.111", "--daemon-host", "server", "--min-behind", "1"],
    )
    assert fire.exit_code == 0, fire.output
    assert len(prop_calls) == 1
    assert prop_calls[0]["min_behind"] == 1, (
        "the discharge call re-resolved the fleet default instead of the "
        "marker's own arm threshold — this is the exact #2870 freeze"
    )
    # And the marker actually clears — it does not survive the next
    # propagate the way it did in the incident.
    assert dq_cmd.read_roll_pending() is None
