"""#3340: `coord status` must surface a "retried" verdict, not just the final
state — a machine that flips from a slow-but-connected /health reply into a
clean ONLINE only after `check_machine`'s one retry is meaningfully
different from one that answered promptly the first time, and an operator
silently discounting an intermittently-flaky machine is exactly the failure
mode #3340 reports.

End-to-end regression: drive the actual `status` Click command (not just
`coord.network.check_machine` in isolation) so the fix is verified at the
layer an operator actually reads.
"""

from __future__ import annotations

import coord.network as network_mod
from click.testing import CliRunner

from coord.network import MachineStatus


def _run_status(valid_config_path, monkeypatch, *, statuses):
    from coord.commands.status import status as status_cmd
    from coord.network import StatusResult

    def fake_check_all(machines, timeout=3.0, max_workers=None):
        return statuses

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


def test_retried_online_machine_is_flagged_in_status(valid_config_path, monkeypatch) -> None:
    from coord import config as config_mod

    cfg = config_mod.load(valid_config_path)
    laptop = next(m for m in cfg.machines if m.name == "laptop")
    server = next(m for m in cfg.machines if m.name == "server")

    output = _run_status(
        valid_config_path, monkeypatch,
        statuses=[
            MachineStatus(machine=laptop, state="online", latency_ms=2500.0, retried=True),
            MachineStatus(machine=server, state="online", latency_ms=5.0, retried=False),
        ],
    )

    laptop_line = next(line for line in output.splitlines() if "laptop" in line)
    server_line = next(line for line in output.splitlines() if "server" in line)
    assert "retried" in laptop_line
    assert "retried" not in server_line


def test_non_retried_timeout_unaffected(valid_config_path, monkeypatch) -> None:
    """A plain (non-retried) timeout must render exactly as before — no
    "retried" text where there was none."""
    from coord import config as config_mod

    cfg = config_mod.load(valid_config_path)
    laptop = next(m for m in cfg.machines if m.name == "laptop")
    server = next(m for m in cfg.machines if m.name == "server")

    output = _run_status(
        valid_config_path, monkeypatch,
        statuses=[
            MachineStatus(machine=laptop, state="timeout", reason="timed out"),
            MachineStatus(machine=server, state="online", latency_ms=5.0),
        ],
    )

    laptop_line = next(line for line in output.splitlines() if "laptop" in line)
    assert "timed out" in laptop_line
    assert "retried" not in laptop_line
