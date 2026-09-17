"""#3376 deliverable B: `coord status` surfaces the two invariant alarms it
has enough local data to compute without a live daemon tick — machines
busy while the drive queue is empty (#1), and a terminal assignment with
0 turns / $0.00 cost, invisible to spend accounting (#5). See
`coord.invariant_alarms` for the underlying pure checks (unit-tested there
in isolation) — this only pins that `coord status` actually calls them.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from coord import network
from coord.cli import main

CONFIG_YAML = """\
repos:
  - name: api
    github: acme/api
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


@pytest.fixture
def coord_dir(tmp_path: Path, coord_db) -> Path:
    return tmp_path


def _online_health(machine_name: str = "laptop") -> dict:
    return {"machine": machine_name, "capabilities": [], "repos": ["api"], "active": 0, "completed": 0}


def _one_online_machine() -> list[network.MachineStatus]:
    st = network.MachineStatus(
        machine=MagicMock(host="laptop.tailnet", repos=["api"]),
        state=network.ONLINE, latency_ms=12.0, health=_online_health(),
    )
    st.machine.name = "laptop"
    st.machine.host = "laptop.tailnet"
    st.machine.repos = ["api"]
    st.machine.quiet_hours = None
    return [st]


class TestMachinesBusyWhileQueueEmptyAlarm:
    def test_fires_when_a_running_assignment_has_no_queue_row(
        self, config_file: Path, coord_dir: Path,
    ) -> None:
        from coord.models import Assignment, Board
        from coord.state import save_board

        save_board(Board(active=[
            Assignment(
                machine_name="laptop", repo_name="api", issue_number=42,
                issue_title="Fix auth", assignment_id="abc123", status="running",
                type="smoke",
            ),
        ]))

        with patch("coord.network.check_all", return_value=_one_online_machine()), \
             patch(
                 "coord.network.fetch_status",
                 return_value=network.StatusResult(data={"active": [], "completed": []}),
             ), \
             patch("coord.state.list_drive_queue", return_value=[]):
            result = CliRunner().invoke(main, ["status", "--config", str(config_file)])

        assert result.exit_code == 0, result.output
        assert "INVARIANT ALARMS" in result.output
        assert "queue is empty" in result.output
        assert "laptop" in result.output

    def test_silent_when_queue_has_a_matching_row(
        self, config_file: Path, coord_dir: Path,
    ) -> None:
        from coord.models import Assignment, Board
        from coord.state import save_board

        save_board(Board(active=[
            Assignment(
                machine_name="laptop", repo_name="api", issue_number=42,
                issue_title="Fix auth", assignment_id="abc123", status="running",
                type="smoke",
            ),
        ]))

        with patch("coord.network.check_all", return_value=_one_online_machine()), \
             patch(
                 "coord.network.fetch_status",
                 return_value=network.StatusResult(data={"active": [], "completed": []}),
             ), \
             patch("coord.state.list_drive_queue", return_value=[{"repo_name": "api", "issue_number": 42}]):
            result = CliRunner().invoke(main, ["status", "--config", str(config_file)])

        assert result.exit_code == 0, result.output
        assert "queue is empty" not in result.output


class TestZeroTurnZeroCostTerminalAlarm:
    def test_fires_on_a_done_row_with_no_measurable_work(
        self, config_file: Path, coord_dir: Path,
    ) -> None:
        """No local log file and no remote usage data (the default in this
        isolated test env) is exactly the "never captured" shape #3376's
        alarm 5 targets — a ghost dispatch that never actually ran."""
        from coord.models import Assignment, Board
        from coord.state import save_board

        save_board(Board(completed=[
            Assignment(
                machine_name="laptop", repo_name="api", issue_number=7,
                issue_title="Ghost dispatch", assignment_id="ghost1",
                status="done", type="review",
            ),
        ]))

        with patch("coord.network.check_all", return_value=_one_online_machine()), \
             patch(
                 "coord.network.fetch_status",
                 return_value=network.StatusResult(data={"active": [], "completed": []}),
             ), \
             patch("coord.state.list_drive_queue", return_value=[]):
            result = CliRunner().invoke(main, ["status", "--config", str(config_file)])

        assert result.exit_code == 0, result.output
        assert "INVARIANT ALARMS" in result.output
        assert "ghost1" in result.output
        assert "invisible to spend accounting" in result.output

    def test_silent_on_a_done_row_with_real_captured_usage(
        self, config_file: Path, coord_dir: Path,
    ) -> None:
        from coord.models import Assignment, Board
        from coord.state import save_board
        from coord.usage import AssignmentUsage

        save_board(Board(completed=[
            Assignment(
                machine_name="laptop", repo_name="api", issue_number=7,
                issue_title="Real work", assignment_id="real1",
                status="done", type="review",
            ),
        ]))

        real_usage = AssignmentUsage(
            assignment_id="real1", repo_name="api", issue_number=7,
            issue_title="Real work", status="done",
            total_cost_usd=0.85, num_turns=14,
        )
        with patch("coord.network.check_all", return_value=_one_online_machine()), \
             patch(
                 "coord.network.fetch_status",
                 return_value=network.StatusResult(data={"active": [], "completed": []}),
             ), \
             patch("coord.state.list_drive_queue", return_value=[]), \
             patch("coord.usage.parse_usage_from_log", return_value=real_usage):
            result = CliRunner().invoke(main, ["status", "--config", str(config_file)])

        assert result.exit_code == 0, result.output
        assert "INVARIANT ALARMS" not in result.output
