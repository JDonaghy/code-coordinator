"""#3366: `coord status` surfaces a non-default supervisor (launchd) up
front, before it matters — `_escalate_restart`'s launchd fallback and the
`supervisor:` remediation strings are the incident-time fix, but "this
machine has no systemd" used to be invisible until an escalation actually
needed it. The `/health` self-report (freshest) wins over the static
`coordinator.yml` `supervisor:` override, which in turn wins over unknown —
`coord.restart_cmd.resolve_supervisor`'s precedence, exercised here through
`coord status`'s own printed output.
"""

from __future__ import annotations

from click.testing import CliRunner

import coord.network as network_mod
from coord.commands.status import status as status_cmd
from coord.network import MachineStatus, StatusResult


def _run_status(valid_config_path, monkeypatch, *, health: dict | None, machine_supervisor=None):
    from coord import config as config_mod

    cfg = config_mod.load(valid_config_path)
    laptop = next(m for m in cfg.machines if m.name == "laptop")
    server = next(m for m in cfg.machines if m.name == "server")
    if machine_supervisor is not None:
        laptop.supervisor = machine_supervisor

    def fake_check_all(machines, timeout=3.0, max_workers=None):
        return [
            MachineStatus(machine=laptop, state="online", latency_ms=1.0, health=health),
            MachineStatus(machine=server, state="offline", reason="connection refused"),
        ]

    def fake_fetch_status(machine, timeout=3.0):
        return StatusResult(data={"active": [], "completed": []})

    monkeypatch.setattr(network_mod, "check_all", fake_check_all)
    monkeypatch.setattr(network_mod, "fetch_status", fake_fetch_status)

    runner = CliRunner()
    result = runner.invoke(
        status_cmd, ["--config", str(valid_config_path)], catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    return result.output


def _host_line(out: str, host: str) -> str:
    """The single line reading `    host: <host>  repos: ...` — the
    supervisor note, when present, is appended to this exact line (see
    `coord/commands/status.py`), so isolating it is more robust than
    slicing the whole output between machine names (`laptop` recurs inside
    `laptop.tailnet` itself, which breaks a naive split)."""
    line = next(ln for ln in out.splitlines() if ln.strip().startswith(f"host: {host}"))
    return line


def test_a_live_launchd_self_report_is_surfaced(valid_config_path, monkeypatch) -> None:
    out = _run_status(valid_config_path, monkeypatch, health={"supervisor": "launchd"})
    assert "supervisor: launchd" in _host_line(out, "laptop.tailnet")


def test_a_systemd_host_shows_no_supervisor_line(valid_config_path, monkeypatch) -> None:
    """Silent for the overwhelmingly common case — naming systemd on every
    machine would just be noise on every `coord status`."""
    out = _run_status(valid_config_path, monkeypatch, health={"supervisor": "systemd"})
    assert "supervisor:" not in _host_line(out, "laptop.tailnet")


def test_static_config_fallback_is_used_when_health_says_nothing(
    valid_config_path, monkeypatch
) -> None:
    """An old agent build that predates the `/health` `supervisor` field —
    the operator's `coordinator.yml` `supervisor: launchd` override must
    still surface."""
    out = _run_status(
        valid_config_path, monkeypatch, health={}, machine_supervisor="launchd",
    )
    assert "supervisor: launchd" in _host_line(out, "laptop.tailnet")


def test_live_health_wins_over_a_stale_config_override(valid_config_path, monkeypatch) -> None:
    """The live self-report is the freshest truth and must win — a config
    override left over from before a machine was reimaged onto systemd must
    not keep claiming launchd forever."""
    out = _run_status(
        valid_config_path, monkeypatch,
        health={"supervisor": "systemd"}, machine_supervisor="launchd",
    )
    assert "supervisor:" not in _host_line(out, "laptop.tailnet")
