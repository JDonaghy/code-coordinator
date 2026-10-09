"""#3661: `coord status` surfaces the latest nightly real-platform smoke
verdict per repo+artifact — green, red (with issue links), INFRA, and
stale. Classification itself is unit-tested in isolation in
tests/test_nightly_status.py; this only pins that `coord status` actually
reads the real store and renders one of the four states.
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
  - name: vimcode
    github: acme/vimcode
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [vimcode]
    repo_paths:
      vimcode: /tmp/vimcode
release_gate:
  vimcode:
    nightly: required
    nightly_artifacts: [macos-dmg]
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


@pytest.fixture(autouse=True)
def coord_dir_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COORD_DIR", str(tmp_path / "coord_dir"))


def _one_online_machine() -> list[network.MachineStatus]:
    st = network.MachineStatus(
        machine=MagicMock(host="laptop.tailnet", repos=["vimcode"]),
        state=network.ONLINE, latency_ms=12.0,
        health={"machine": "laptop", "capabilities": [], "repos": ["vimcode"], "active": 0, "completed": 0},
    )
    st.machine.name = "laptop"
    st.machine.host = "laptop.tailnet"
    st.machine.repos = ["vimcode"]
    st.machine.quiet_hours = None
    return [st]


def _invoke(config_file: Path):
    with patch("coord.network.check_all", return_value=_one_online_machine()), \
         patch(
             "coord.network.fetch_status",
             return_value=network.StatusResult(data={"active": [], "completed": []}),
         ), \
         patch("coord.state.list_drive_queue", return_value=[]):
        return CliRunner().invoke(main, ["status", "--config", str(config_file)])


def _record(**kwargs) -> None:
    import time

    from coord.nightly_store import NightlyResultRecord, record_nightly_result

    defaults = dict(
        repo="vimcode", artifact="macos-dmg", sha="deadbeef", passed=True,
        checked_at=time.time(), spec="install.yaml", step="launch",
        run_id="run-1", steps_total=1,
    )
    defaults.update(kwargs)
    record_nightly_result(NightlyResultRecord(**defaults))


class TestNightlyStatusSection:
    def test_green_result_renders_green(self, config_file: Path) -> None:
        _record(passed=True)
        result = _invoke(config_file)
        assert result.exit_code == 0, result.output
        assert "nightly real-platform smoke" in result.output
        assert "green" in result.output
        assert "vimcode" in result.output and "macos-dmg" in result.output

    def test_red_result_renders_red_with_issue_link(self, config_file: Path) -> None:
        import time

        from coord.nightly_store import set_nightly_issue_number

        _record(passed=False, detail="crashed", checked_at=time.time())
        set_nightly_issue_number(
            repo="vimcode", run_id="run-1", spec="install.yaml", step="launch",
            issue_number=77,
        )
        result = _invoke(config_file)
        assert result.exit_code == 0, result.output
        assert "RED" in result.output
        assert "#77" in result.output

    def test_infra_result_renders_infra_with_host(self, config_file: Path) -> None:
        import time

        _record(
            passed=False, unavailable=True, detail="screen locked",
            host="elitebook", checked_at=time.time(),
        )
        result = _invoke(config_file)
        assert result.exit_code == 0, result.output
        assert "INFRA" in result.output
        assert "elitebook" in result.output
        assert "screen locked" in result.output

    def test_no_run_recorded_renders_stale(self, config_file: Path) -> None:
        result = _invoke(config_file)
        assert result.exit_code == 0, result.output
        assert "STALE" in result.output

    def test_an_old_run_renders_stale_not_green(self, config_file: Path) -> None:
        _record(passed=True, checked_at=1.0)
        result = _invoke(config_file)
        assert result.exit_code == 0, result.output
        assert "STALE" in result.output

    def test_no_release_gate_repos_prints_no_section(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator-plain.yml"
        p.write_text(
            "repos:\n  - name: vimcode\n    github: acme/vimcode\n"
            "machines:\n  - name: laptop\n    host: laptop.tailnet\n"
            "    repos: [vimcode]\n    repo_paths:\n      vimcode: /tmp/vimcode\n"
        )
        result = _invoke(p)
        assert result.exit_code == 0, result.output
        assert "nightly real-platform smoke" not in result.output

    def test_machine_filter_skips_the_section(self, config_file: Path) -> None:
        _record(passed=True)
        with patch("coord.network.check_all", return_value=_one_online_machine()), \
             patch(
                 "coord.network.fetch_status",
                 return_value=network.StatusResult(data={"active": [], "completed": []}),
             ), \
             patch("coord.state.list_drive_queue", return_value=[]):
            result = CliRunner().invoke(
                main, ["status", "--config", str(config_file), "--machine", "laptop"],
            )
        assert result.exit_code == 0, result.output
        assert "nightly real-platform smoke" not in result.output
