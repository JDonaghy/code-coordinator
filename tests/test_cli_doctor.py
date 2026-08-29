"""End-to-end tests for `coord doctor` (#1570 E) — the Click command, driven
the same way tests/test_cli_status_merge_queue.py drives `status`: mock
`coord.network.check_all` so the test is hermetic, then assert on the
command's actual output and exit code.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

import coord.network as network_mod
from coord.commands.status import doctor
from coord.network import ONLINE, OFFLINE, MachineStatus


def _run_doctor(config_path, monkeypatch, statuses, *, extra_args=None, ts_map=None):
    monkeypatch.setattr(network_mod, "check_all", lambda *a, **k: statuses)
    # #2912: default the new host-resolution check to "tailscale not
    # available" (matches=None, silent) so every PRE-EXISTING test here
    # stays hermetic and unaffected — tests that actually exercise the
    # check pass their own `ts_map`.
    monkeypatch.setattr(network_mod, "tailscale_ip_map", lambda *a, **k: ts_map)
    runner = CliRunner()
    # #2082: doctor resolves --expected from PyPI by default now. Default
    # these tests to --no-pypi so they stay hermetic (no real network call);
    # a caller wanting the pypi-resolution path passes --pypi in extra_args,
    # which — as the LAST occurrence of the --pypi/--no-pypi flag pair on
    # the line — wins.
    result = runner.invoke(
        doctor, ["--config", str(config_path), "--no-pypi", *(extra_args or [])],
        catch_exceptions=False,
    )
    return result


def _health(tool_versions: dict, machine=None) -> dict:
    """#1712: a realistic `/health` echoes back what the machine declares.
    A stub that always published `capabilities: []` now fires doctor's
    declared-but-unpublished CRIT in every test here, drowning out what each
    one is actually asserting — so pass the machine and echo it."""
    return {
        "machine": getattr(machine, "name", "x"),
        "capabilities": list(getattr(machine, "capabilities", None) or []),
        "repos": list(getattr(machine, "repos", None) or []),
        "tool_versions": tool_versions,
    }


def _ok_probe(capability: str | None = None) -> dict:
    return {
        "found": True, "version": "9.9.9", "min_version": None,
        "meets_floor": None, "capability": capability, "ok": True,
    }


def _missing_probe(capability: str | None = None) -> dict:
    return {
        "found": False, "version": None, "min_version": None,
        "meets_floor": None, "capability": capability, "ok": False,
    }


def test_doctor_exits_zero_when_everything_checks_out(
    valid_config_path, monkeypatch,
) -> None:
    from coord.config import load

    cfg = load(valid_config_path)
    machines = cfg.machines  # laptop [python], server [python, docker]

    statuses = [
        MachineStatus(
            machine=m, state=ONLINE, latency_ms=5.0,
            health=_health({
                "git": _ok_probe(), "gh": _ok_probe(),
                "python3": _ok_probe("python"),
            }, m),
        )
        for m in machines
    ]
    result = _run_doctor(valid_config_path, monkeypatch, statuses)
    assert result.exit_code == 0, result.output
    assert "✓ git" in result.output
    assert "✓ python3" in result.output
    # "docker" has no registered prereq — must not be reported at all.
    assert "docker" not in result.output


def test_doctor_flags_unreachable_machine(valid_config_path, monkeypatch) -> None:
    from coord.config import load

    cfg = load(valid_config_path)
    statuses = [
        MachineStatus(machine=cfg.machines[0], state=OFFLINE, reason="connection refused"),
        MachineStatus(
            machine=cfg.machines[1], state=ONLINE,
            health=_health({"git": _ok_probe(), "gh": _ok_probe()}, cfg.machines[1]),
        ),
    ]
    result = _run_doctor(valid_config_path, monkeypatch, statuses)
    assert result.exit_code == 1
    assert "unreachable" in result.output
    assert "connection refused" in result.output


def test_doctor_names_lan_dns_shadow_even_when_unreachable(
    valid_config_path, monkeypatch,
) -> None:
    """#2912: `laptop`'s `host: laptop.tailnet` resolving to something other
    than laptop's own tailnet address must be named EVEN on the branch that
    reports "unreachable" — that's the whole failure mode: a DNS shadow
    makes a healthy agent look dead with no clue why."""
    from coord.config import load

    cfg = load(valid_config_path)
    statuses = [
        MachineStatus(machine=cfg.machines[0], state=network_mod.TIMEOUT, reason="timed out"),
        MachineStatus(
            machine=cfg.machines[1], state=ONLINE,
            health=_health({"git": _ok_probe(), "gh": _ok_probe()}, cfg.machines[1]),
        ),
    ]
    ts_map = {"laptop": ("100.64.0.1", "laptop.tailf46ef8.ts.net")}
    monkeypatch.setattr(
        network_mod, "resolve_host_ip", lambda host, **k: "192.168.1.183"
    )
    result = _run_doctor(valid_config_path, monkeypatch, statuses, ts_map=ts_map)
    assert result.exit_code == 1
    assert "machines.host_resolves_offtailnet" in result.output
    assert "100.64.0.1" in result.output
    assert "laptop.tailf46ef8.ts.net" in result.output
    assert "unreachable" in result.output


def test_doctor_is_silent_when_host_matches_tailnet_address(
    valid_config_path, monkeypatch,
) -> None:
    from coord.config import load

    cfg = load(valid_config_path)
    statuses = [
        MachineStatus(
            machine=m, state=ONLINE,
            health=_health({"git": _ok_probe(), "gh": _ok_probe()}, m),
        )
        for m in cfg.machines
    ]
    ts_map = {
        "laptop": ("100.64.0.1", "laptop.tailf46ef8.ts.net"),
        "server": ("100.64.0.2", "server.tailf46ef8.ts.net"),
    }
    monkeypatch.setattr(
        network_mod, "resolve_host_ip",
        lambda host, **k: {"laptop.tailnet": "100.64.0.1", "server.tailnet": "100.64.0.2"}[host],
    )
    result = _run_doctor(valid_config_path, monkeypatch, statuses, ts_map=ts_map)
    assert result.exit_code == 0, result.output
    assert "host_resolves_offtailnet" not in result.output


def test_doctor_flags_missing_baseline_tool(valid_config_path, monkeypatch) -> None:
    from coord.config import load

    cfg = load(valid_config_path)
    statuses = [
        MachineStatus(
            machine=m, state=ONLINE,
            health=_health({"git": _ok_probe(), "gh": _missing_probe()}, m),
        )
        for m in cfg.machines
    ]
    result = _run_doctor(valid_config_path, monkeypatch, statuses)
    assert result.exit_code == 1
    assert "✗ gh: not found" in result.output


def test_doctor_flags_claimed_capability_the_probe_contradicts(
    valid_config_path, monkeypatch,
) -> None:
    """`server` claims `docker` isn't registered (skipped) but claims
    nothing that maps to a failing probe in this fixture — use `python`
    instead: laptop claims `python` but its own probe says python3 is
    missing, which must surface as an unmet-capability line, not just a
    generic tool-missing line."""
    from coord.config import load

    cfg = load(valid_config_path)
    statuses = [
        MachineStatus(
            machine=m, state=ONLINE,
            health=_health({
                "git": _ok_probe(), "gh": _ok_probe(),
                "python3": _missing_probe("python"),
            }, m),
        )
        for m in cfg.machines
    ]
    result = _run_doctor(valid_config_path, monkeypatch, statuses)
    assert result.exit_code == 1
    assert "capability 'python' claimed but unmet" in result.output


def test_doctor_flags_agent_predating_tool_versions(
    valid_config_path, monkeypatch,
) -> None:
    """An agent that hasn't upgraded to #1570 B yet omits `tool_versions`
    from /health entirely — doctor must call this out distinctly rather
    than crash or silently pass it."""
    from coord.config import load

    cfg = load(valid_config_path)
    statuses = [
        MachineStatus(machine=m, state=ONLINE, health={"machine": m.name})
        for m in cfg.machines
    ]
    result = _run_doctor(valid_config_path, monkeypatch, statuses)
    assert result.exit_code == 1
    assert "predates #1570 B" in result.output


def test_doctor_machine_filter_narrows_to_one(valid_config_path, monkeypatch) -> None:
    from coord.config import load

    cfg = load(valid_config_path)
    laptop = next(m for m in cfg.machines if m.name == "laptop")
    statuses = [
        MachineStatus(
            machine=laptop, state=ONLINE,
            health=_health(
                {"git": _ok_probe(), "gh": _ok_probe(), "python3": _ok_probe("python")},
                laptop,
            ),
        ),
    ]
    result = _run_doctor(
        valid_config_path, monkeypatch, statuses, extra_args=["--machine", "laptop"],
    )
    assert result.exit_code == 0, result.output
    assert "laptop" in result.output
    assert "server" not in result.output


def test_doctor_unknown_machine_filter_errors(valid_config_path, monkeypatch) -> None:
    result = _run_doctor(
        valid_config_path, monkeypatch, [], extra_args=["--machine", "nope"],
    )
    assert result.exit_code == 2


# ── #1711: provider:opencode availability — declared vs. probed-and-met ────

OPENCODE_CONFIG = """\
repos:
  - name: api
    github: acme/api
    provider: opencode
machines:
  - name: laptop
    host: laptop.tailnet
    capabilities: ["provider:opencode"]
    repos: [api]
providers:
  definitions:
    opencode:
      type: opencode
"""


@pytest.fixture
def opencode_config_path(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(OPENCODE_CONFIG)
    return p


def test_doctor_reports_provider_declared_and_probed_met(
    opencode_config_path, monkeypatch,
) -> None:
    """A machine that DECLARES `provider:opencode` AND whose probe found
    the binary reads green — same shape as any other capability."""
    from coord.config import load

    cfg = load(opencode_config_path)
    statuses = [
        MachineStatus(
            machine=m, state=ONLINE,
            health=_health({
                "git": _ok_probe(), "gh": _ok_probe(),
                "opencode": _ok_probe("provider:opencode"),
            }, m),
        )
        for m in cfg.machines
    ]
    result = _run_doctor(opencode_config_path, monkeypatch, statuses)
    assert result.exit_code == 0, result.output
    assert "✓ opencode" in result.output


def test_doctor_flags_declared_provider_the_probe_contradicts(
    opencode_config_path, monkeypatch,
) -> None:
    """DECLARED (`provider:opencode` in capabilities) but PROBED-AND-UNMET
    (the opencode binary isn't actually on that machine) must surface as an
    unmet-capability line — the same "claimed but unmet" shape #1570 D
    already gives rust/gtk/browser, now covering provider availability too."""
    from coord.config import load

    cfg = load(opencode_config_path)
    statuses = [
        MachineStatus(
            machine=m, state=ONLINE,
            health=_health({
                "git": _ok_probe(), "gh": _ok_probe(),
                "opencode": _missing_probe("provider:opencode"),
            }, m),
        )
        for m in cfg.machines
    ]
    result = _run_doctor(opencode_config_path, monkeypatch, statuses)
    assert result.exit_code == 1
    assert "✗ opencode: not found" in result.output
    assert "capability 'provider:opencode' claimed but unmet" in result.output


def test_doctor_does_not_probe_undeclared_provider_capability(
    valid_config_path, monkeypatch,
) -> None:
    """A machine that never declared `provider:opencode` gets no opencode
    row at all — matches the existing "docker has no registered prereq /
    unclaimed capability isn't probed" posture for every other capability."""
    from coord.config import load

    cfg = load(valid_config_path)  # laptop/server declare only python/docker
    statuses = [
        MachineStatus(
            machine=m, state=ONLINE,
            health=_health({"git": _ok_probe(), "gh": _ok_probe()}, m),
        )
        for m in cfg.machines
    ]
    result = _run_doctor(valid_config_path, monkeypatch, statuses)
    assert result.exit_code == 0, result.output
    assert "opencode" not in result.output


# ── #1862: quiet-hours starvation warning ───────────────────────────────────

QUIET_ONLY_GPU_CONFIG = """
repos:
  - name: api
    github: acme/api
machines:
  - name: quiet1
    host: quiet1.tailnet
    capabilities: ["gpu"]
    repos: [api]
    quiet_hours:
      start: "23:00"
      end: "08:00"
      tz: UTC
  - name: server
    host: server.tailnet
    capabilities: ["python"]
    repos: [api]
"""

QUIET_BUT_COVERED_CONFIG = """
repos:
  - name: api
    github: acme/api
machines:
  - name: quiet1
    host: quiet1.tailnet
    capabilities: ["gpu"]
    repos: [api]
    quiet_hours:
      start: "23:00"
      end: "08:00"
      tz: UTC
  - name: awake1
    host: awake1.tailnet
    capabilities: ["gpu"]
    repos: [api]
"""


@pytest.fixture
def quiet_only_gpu_config_path(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(QUIET_ONLY_GPU_CONFIG)
    return p


@pytest.fixture
def quiet_but_covered_config_path(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(QUIET_BUT_COVERED_CONFIG)
    return p


def test_doctor_warns_when_only_quiet_hours_machines_offer_a_capability(
    quiet_only_gpu_config_path, monkeypatch,
) -> None:
    """#1862 "Starvation" section: a quiet window that removes the only
    machine with a capability would make matching work silently
    unroutable (dispatch_smoke's #1678 failure shape) — `coord doctor`
    must at least log it."""
    from coord.config import load

    cfg = load(quiet_only_gpu_config_path)
    statuses = [
        MachineStatus(
            machine=m, state=ONLINE,
            health=_health({"git": _ok_probe(), "gh": _ok_probe()}, m),
        )
        for m in cfg.machines
    ]
    result = _run_doctor(quiet_only_gpu_config_path, monkeypatch, statuses)
    assert result.exit_code == 1, result.output
    assert "capability 'gpu' is only ever offered by machine(s) with quiet_hours" in result.output
    assert "quiet1" in result.output


def test_doctor_does_not_warn_when_a_capability_has_an_always_awake_machine(
    quiet_but_covered_config_path, monkeypatch,
) -> None:
    """Same capability, but a second machine offers it with no
    `quiet_hours:` block — always coverable, no warning."""
    from coord.config import load

    cfg = load(quiet_but_covered_config_path)
    statuses = [
        MachineStatus(
            machine=m, state=ONLINE,
            health=_health({"git": _ok_probe(), "gh": _ok_probe()}, m),
        )
        for m in cfg.machines
    ]
    result = _run_doctor(quiet_but_covered_config_path, monkeypatch, statuses)
    assert result.exit_code == 0, result.output
    assert "only ever offered by machine(s) with quiet_hours" not in result.output


# ── unit_drift (#1831) ────────────────────────────────────────────────────
#
# `coord doctor` projects the machine's own `unit_drift` H-1 result
# (coord/health/checks/unit_drift.py) out of `/health["health"]["results"]` —
# see `_unit_drift_lines` in coord/commands/status.py. These drive it
# end-to-end through the real `doctor` command rather than unit-testing that
# helper directly, matching this file's own convention.


def _health_with_unit_drift(tool_versions: dict, machine, *, unit_results: list[dict]) -> dict:
    h = _health(tool_versions, machine)
    h["health"] = {"results": unit_results}
    return h


def test_doctor_reports_a_stale_unit(valid_config_path, monkeypatch) -> None:
    """The acceptance-criteria "stale unit -> reported" half, through `coord
    doctor` itself."""
    from coord.config import load

    cfg = load(valid_config_path)
    m = cfg.machines[0]
    statuses = [
        MachineStatus(
            machine=m, state=ONLINE,
            health=_health_with_unit_drift(
                {"git": _ok_probe(), "gh": _ok_probe()}, m,
                unit_results=[{
                    "check_id": "unit_drift",
                    "subject": "coord-serve.service",
                    "severity": "warn",
                    "headroom": "stale — installed 504.0h ago, 3 line(s) differ from deploy/coord-serve.service",
                    "detail": "cp deploy/coord-serve.service ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user restart coord-serve",
                }],
            ),
        ),
        *[
            MachineStatus(
                machine=other, state=ONLINE,
                health=_health({"git": _ok_probe(), "gh": _ok_probe()}, other),
            )
            for other in cfg.machines[1:]
        ],
    ]
    result = _run_doctor(valid_config_path, monkeypatch, statuses)
    assert result.exit_code == 1, result.output
    assert "unit drift coord-serve.service" in result.output
    assert "stale" in result.output
    assert "fix:" in result.output


def test_doctor_is_silent_about_a_matching_unit(valid_config_path, monkeypatch) -> None:
    """The acceptance-criteria "matching unit -> silent" half — a machine
    whose installed unit matches deploy/ must not fire `coord doctor`, and
    must not even print a line for it."""
    from coord.config import load

    cfg = load(valid_config_path)
    machines = cfg.machines
    statuses = [
        MachineStatus(
            machine=m, state=ONLINE,
            health=_health_with_unit_drift(
                {"git": _ok_probe(), "gh": _ok_probe()}, m,
                unit_results=[{
                    "check_id": "unit_drift",
                    "subject": "coord-serve.service",
                    "severity": "ok",
                    "headroom": "matches deploy/",
                }],
            ),
        )
        for m in machines
    ]
    result = _run_doctor(valid_config_path, monkeypatch, statuses)
    assert result.exit_code == 0, result.output
    assert "unit drift" not in result.output


def test_doctor_reports_a_path_shadow_risk_as_crit(valid_config_path, monkeypatch) -> None:
    from coord.config import load

    cfg = load(valid_config_path)
    m = cfg.machines[0]
    statuses = [
        MachineStatus(
            machine=m, state=ONLINE,
            health=_health_with_unit_drift(
                {"git": _ok_probe(), "gh": _ok_probe()}, m,
                unit_results=[{
                    "check_id": "unit_drift",
                    "subject": "coord-serve.service",
                    "severity": "crit",
                    "headroom": "PATH shadow risk (504.0h since install)",
                    "detail": "editable checkout shadows the release",
                }],
            ),
        ),
        *[
            MachineStatus(
                machine=other, state=ONLINE,
                health=_health({"git": _ok_probe(), "gh": _ok_probe()}, other),
            )
            for other in cfg.machines[1:]
        ],
    ]
    result = _run_doctor(valid_config_path, monkeypatch, statuses)
    assert result.exit_code == 1, result.output
    assert "CRIT unit drift coord-serve.service" in result.output
    assert "PATH shadow risk" in result.output


# ── #2082: `coord release verify` wired into `coord doctor` ───────────────
#
# `coord release verify` already computes whether the fleet's running
# version matches the released one, and already returns CRIT — nothing
# routine called it, which is #2082's whole complaint. These drive that
# wiring end-to-end through `coord doctor` itself, the same way the
# unit_drift tests above do for #1831.


def _health_with_agent_venv(tool_versions: dict, machine, *, version: str) -> dict:
    h = _health(tool_versions, machine)
    h["health"] = {
        "results": [{
            "check_id": "agent_venv",
            "severity": "ok",
            "headroom": f"~/.coord-venv is {version}",
            "values": {"version": version, "editable": False},
        }],
    }
    return h


def test_doctor_flags_a_fleet_uniformly_behind_the_released_version(
    valid_config_path, monkeypatch,
) -> None:
    """#2082's own evidence, reproduced: every lane agrees with every other
    lane (no internal skew for #2052's trap to catch), but PyPI has moved
    on. This is the test that FAILS against the pre-fix `doctor`: it never
    called `coord release verify` at all, so this exact fleet state
    (uniformly 0.5.15 while PyPI has 0.5.26) rendered as a clean exit 0.
    """
    from coord.config import load
    from coord.health.pypi import parse_version

    cfg = load(valid_config_path)
    statuses = [
        MachineStatus(
            machine=m, state=ONLINE,
            health=_health_with_agent_venv(
                {"git": _ok_probe(), "gh": _ok_probe()}, m, version="0.5.15",
            ),
        )
        for m in cfg.machines
    ]
    monkeypatch.setattr(
        "coord.health.pypi.latest_release_any",
        lambda *a, **k: ("claude-coordinator", parse_version("0.5.26"), []),
    )
    result = _run_doctor(valid_config_path, monkeypatch, statuses, extra_args=["--pypi"])
    assert result.exit_code == 1, result.output
    assert "release version" in result.output
    assert "expected 0.5.26" in result.output


def test_doctor_is_silent_about_release_lag_on_a_current_fleet(
    valid_config_path, monkeypatch,
) -> None:
    from coord.config import load
    from coord.health.pypi import parse_version

    cfg = load(valid_config_path)
    statuses = [
        MachineStatus(
            machine=m, state=ONLINE,
            health=_health_with_agent_venv(
                {"git": _ok_probe(), "gh": _ok_probe()}, m, version="0.5.26",
            ),
        )
        for m in cfg.machines
    ]
    monkeypatch.setattr(
        "coord.health.pypi.latest_release_any",
        lambda *a, **k: ("claude-coordinator", parse_version("0.5.26"), []),
    )
    result = _run_doctor(valid_config_path, monkeypatch, statuses, extra_args=["--pypi"])
    assert result.exit_code == 0, result.output
    assert "release version" not in result.output


def test_doctor_defaults_to_pypi_resolution_without_a_flag(
    valid_config_path, monkeypatch,
) -> None:
    """`--pypi` is the DEFAULT (#2082) — this is the one test in the file
    that does not pass `--no-pypi` (see `_run_doctor`'s comment), pinning
    that the flag really does default on rather than merely being
    available."""
    from coord.config import load
    from coord.health.pypi import parse_version

    cfg = load(valid_config_path)
    statuses = [
        MachineStatus(
            machine=m, state=ONLINE,
            health=_health_with_agent_venv(
                {"git": _ok_probe(), "gh": _ok_probe()}, m, version="0.5.15",
            ),
        )
        for m in cfg.machines
    ]
    monkeypatch.setattr(
        "coord.health.pypi.latest_release_any",
        lambda *a, **k: ("claude-coordinator", parse_version("0.5.26"), []),
    )
    monkeypatch.setattr(network_mod, "check_all", lambda *a, **k: statuses)
    result = CliRunner().invoke(
        doctor, ["--config", str(valid_config_path)], catch_exceptions=False,
    )
    assert result.exit_code == 1, result.output
    assert "expected 0.5.26" in result.output


# ── unit_enablement (#2098) ─────────────────────────────────────────────────
#
# `coord doctor` projects the machine's own `unit_enablement` H-1 result
# (coord/health/checks/unit_enablement.py) out of
# `/health["health"]["results"]` — see `_unit_enablement_lines` in
# coord/commands/status.py, which mirrors `_unit_drift_lines` immediately
# above. Same convention: drive it end-to-end through the real `doctor`
# command rather than unit-testing the helper directly.
#
# `_health_with_unit_drift` just wraps whatever `unit_results` it's given
# into `health["health"]["results"]` regardless of `check_id` — reused here
# rather than duplicated for a second check id.


def test_doctor_reports_a_disabled_unit(valid_config_path, monkeypatch) -> None:
    """The state that hid coord-release-propagate.timer for a day: an
    installed, manifest-listed unit reporting anything other than enabled.
    `coord doctor`'s own printed report — the thing docs/AGENT_OPERATIONS.md
    points operators at — must name the unit and how to fix it, not just
    roll the WARN into the aggregate FLEET severity."""
    from coord.config import load

    cfg = load(valid_config_path)
    m = cfg.machines[0]
    statuses = [
        MachineStatus(
            machine=m, state=ONLINE,
            health=_health_with_unit_drift(
                {"git": _ok_probe(), "gh": _ok_probe()}, m,
                unit_results=[{
                    "check_id": "unit_enablement",
                    "subject": "coord-release-propagate.timer",
                    "severity": "warn",
                    "headroom": "installed but disabled — a disabled unit and a working one produce identical evidence until something needed it (#2098)",
                    "detail": "systemctl --user daemon-reload && systemctl --user enable --now coord-release-propagate.timer",
                }],
            ),
        ),
        *[
            MachineStatus(
                machine=other, state=ONLINE,
                health=_health({"git": _ok_probe(), "gh": _ok_probe()}, other),
            )
            for other in cfg.machines[1:]
        ],
    ]
    result = _run_doctor(valid_config_path, monkeypatch, statuses)
    assert result.exit_code == 1, result.output
    assert "unit enablement coord-release-propagate.timer" in result.output
    assert "installed but disabled" in result.output
    assert "fix:" in result.output
    assert "enable --now coord-release-propagate.timer" in result.output


def test_doctor_is_silent_about_an_enabled_unit(valid_config_path, monkeypatch) -> None:
    """A unit reporting `enabled` must not fire `coord doctor`, and must not
    even print a line for it (mirrors test_doctor_is_silent_about_a_matching_unit
    for unit_drift)."""
    from coord.config import load

    cfg = load(valid_config_path)
    machines = cfg.machines
    statuses = [
        MachineStatus(
            machine=m, state=ONLINE,
            health=_health_with_unit_drift(
                {"git": _ok_probe(), "gh": _ok_probe()}, m,
                unit_results=[{
                    "check_id": "unit_enablement",
                    "subject": "coord-serve.service",
                    "severity": "ok",
                    "headroom": "enabled",
                }],
            ),
        )
        for m in machines
    ]
    result = _run_doctor(valid_config_path, monkeypatch, statuses)
    assert result.exit_code == 0, result.output
    assert "unit enablement" not in result.output
