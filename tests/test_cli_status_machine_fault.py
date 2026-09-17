"""#3367: `coord status` surfaces a machine's recorded fault streak.

Before this, a host whose Claude credentials were dead rendered as a plain
`online • idle` — indistinguishable from a machine with nothing to do,
which is exactly how precision's four identical auth-failure dispatches
went unnoticed until a human paused it by hand. `coord.machine_fault.
describe()` is consulted per machine and, when non-`None`, renders as an
`⚠ machine fault: ...` line right alongside the existing `⚠ degraded: ...`
line this mirrors (see `coord/commands/status.py`).
"""

from __future__ import annotations

from click.testing import CliRunner

import coord.network as network_mod
from coord import machine_fault
from coord.commands.status import status as status_cmd
from coord.network import MachineStatus, StatusResult


def _run_status(valid_config_path, monkeypatch) -> str:
    from coord import config as config_mod

    cfg = config_mod.load(valid_config_path)
    laptop = next(m for m in cfg.machines if m.name == "laptop")
    server = next(m for m in cfg.machines if m.name == "server")

    def fake_check_all(machines, timeout=3.0, max_workers=None):
        return [
            MachineStatus(machine=laptop, state="online", latency_ms=1.0),
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


def test_status_surfaces_a_recorded_machine_fault(valid_config_path, monkeypatch) -> None:
    machine_fault.record_fault(
        "laptop",
        "machine fault (auth): the host's Claude OAuth credentials are dead "
        "— this is not a defect in the work (#3367)",
    )
    output = _run_status(valid_config_path, monkeypatch)
    assert "⚠ machine fault: 1 consecutive machine fault — last: " in output, output
    assert "OAuth credentials are dead" in output, output


def test_status_omits_the_line_for_a_healthy_machine(valid_config_path, monkeypatch) -> None:
    """The overwhelming majority case — no fault ever recorded — must not
    print anything extra."""
    output = _run_status(valid_config_path, monkeypatch)
    assert "machine fault" not in output, output


def test_status_reports_the_running_streak_not_just_one(
    valid_config_path, monkeypatch,
) -> None:
    for _ in range(3):
        machine_fault.record_fault("laptop", "machine fault (instant failure): 1 turn(s), $0.00")
    output = _run_status(valid_config_path, monkeypatch)
    assert "⚠ machine fault: 3 consecutive machine faults — last: " in output, output
