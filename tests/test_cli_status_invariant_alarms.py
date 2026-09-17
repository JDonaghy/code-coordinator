"""#3376 deliverable B: `coord status` surfaces the invariant alarms it has
enough local data to compute without a live daemon tick or new persisted
cross-tick history — machines busy while the drive queue is empty (#1), a
review/smoke row that reached `done` without ever recording the verdict its
own gate requires (#3), and a terminal assignment with 0 turns / $0.00
cost, invisible to spend accounting (#5). See `coord.invariant_alarms` for
the underlying pure checks (unit-tested there in isolation) — this only
pins that `coord status` actually calls them.
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
                # #3376 review round 1: type="work" here — a "review" row
                # with no `review_verdict` would ALSO trip alarm 3 (#3375's
                # loop condition, below), which is correct but not what
                # THIS test is pinning; keep the two alarms' fixtures
                # independent.
                status="done", type="work",
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
                # #3376 review round 1: type="work" (not "review") — a
                # "review" row with no `review_verdict` would trip alarm 3
                # independently of this test's alarm-5 fixture.
                status="done", type="work",
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


class TestGateDoneWithoutVerdictAlarm:
    """#3376 review round 1: alarm 3 (#3375's own loop condition) — a
    review/smoke row that reached `status="done"` without ever recording
    the verdict its own gate requires. Previously `check_gate_done_
    without_verdict` had zero callers anywhere in the tree; these pin that
    `coord status` now calls it for the two row/verdict-field pairs that
    need no per-repo gate-configuration resolution to check correctly
    (review_verdict on a "review" row, smoke_test on a "smoke" row).
    """

    def test_fires_on_a_done_review_with_no_verdict(
        self, config_file: Path, coord_dir: Path,
    ) -> None:
        from coord.models import Assignment, Board
        from coord.state import save_board

        save_board(Board(completed=[
            Assignment(
                machine_name="laptop", repo_name="api", issue_number=7,
                issue_title="Reviewed", assignment_id="rev1",
                status="done", type="review", review_verdict=None,
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
        assert "rev1" in result.output
        assert "without ever recording the verdict" in result.output

    def test_fires_on_a_done_smoke_with_no_verdict(
        self, config_file: Path, coord_dir: Path,
    ) -> None:
        from coord.models import Assignment, Board
        from coord.state import save_board

        save_board(Board(completed=[
            Assignment(
                machine_name="laptop", repo_name="api", issue_number=7,
                issue_title="Smoked", assignment_id="smoke1",
                status="done", type="smoke", smoke_test=None,
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
        assert "smoke1" in result.output
        assert "without ever recording the verdict" in result.output

    def test_silent_on_a_done_review_with_a_recorded_verdict(
        self, config_file: Path, coord_dir: Path,
    ) -> None:
        from coord.models import Assignment, Board
        from coord.state import save_board
        from coord.usage import AssignmentUsage

        save_board(Board(completed=[
            Assignment(
                machine_name="laptop", repo_name="api", issue_number=7,
                issue_title="Reviewed", assignment_id="rev2",
                status="done", type="review", review_verdict="approve",
            ),
        ]))

        # Real captured usage too (mirrors
        # test_silent_on_a_done_row_with_real_captured_usage) — otherwise
        # alarm 5 (zero turns / $0 cost) would independently fire on this
        # same fixture, and this test is only pinning alarm 3.
        real_usage = AssignmentUsage(
            assignment_id="rev2", repo_name="api", issue_number=7,
            issue_title="Reviewed", status="done",
            total_cost_usd=0.42, num_turns=6,
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
