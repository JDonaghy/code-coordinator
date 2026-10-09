"""Tests for `coord smoke nightly` (#3660) — the CLI wiring over
coord.nightly_runner.run_nightly_smoke. The orchestration itself is tested
end-to-end in tests/test_nightly_runner.py; this file only checks the CLI
seam: config/board loading, exit codes, and --dry-run/--json rendering.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from coord.config import Config
from coord.models import Board, Machine, Repo
from coord.nightly_runner import (
    NightlyRunnerError,
    NightlyRunPlan,
    NightlyRunReport,
)
from coord.nightly_smoke import NightlyStepObservation, StepVerdict, StepVerdictKind
from coord.nightly_smoke import NightlyStepOutcome


def _plan(*, infra_blocked: bool = False, infra_reason: str = "") -> NightlyRunPlan:
    return NightlyRunPlan(
        repo="vimcode", artifact="macos-dmg", spec="tests/smoke-spec/install.yaml",
        driver_kind="cli-pytest", source="build", ref="integration",
        detail="build it", host=None, host_rationale="",
        infra_blocked=infra_blocked, infra_reason=infra_reason,
    )


def _outcome(*, action: str, step: str = "launch") -> NightlyStepOutcome:
    obs = NightlyStepObservation(
        repo="vimcode", spec="tests/smoke-spec/install.yaml", step=step,
        sha="deadbeef", passed=action == "none", checked_at=1.0,
    )
    kind = StepVerdictKind.GREEN_CLEAN if action == "none" else StepVerdictKind.RED_NEEDS_FILING
    verdict = StepVerdict(observation=obs, known_bug=None, kind=kind)
    return NightlyStepOutcome(verdict=verdict, action=action, issue_number=99 if action == "filed" else None)


@pytest.fixture(autouse=True)
def _coord_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COORD_DIR", str(tmp_path / "coord_dir"))


def _config() -> Config:
    return Config(
        repos=[Repo(name="vimcode", github="acme/vimcode")],
        machines=[Machine(name="m1", host="m1.tail", repos=["vimcode"])],
    )


class TestSmokeNightlyCli:
    def test_unknown_repo_exits_2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from coord.cli import main

        monkeypatch.setattr("coord.commands.smoke._load_config", lambda path: _config())
        result = CliRunner().invoke(
            main, ["smoke", "nightly", "--repo", "nope", "--artifact", "macos-dmg"],
        )
        assert result.exit_code == 2
        assert "not in coordinator.yml" in result.output

    def test_runner_error_exits_2_with_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from coord.cli import main

        monkeypatch.setattr("coord.commands.smoke._load_config", lambda path: _config())
        monkeypatch.setattr("coord.board_service.read_board", lambda: Board())
        monkeypatch.setattr("coord.github_ops.get_open_issues", lambda slug: [])
        monkeypatch.setattr("coord.commands.smoke._fetch_closed_issues", lambda slug: [])

        def _boom(**kwargs):
            raise NightlyRunnerError("no acceptance driver configured")

        monkeypatch.setattr("coord.nightly_runner.run_nightly_smoke", _boom)
        result = CliRunner().invoke(
            main, ["smoke", "nightly", "--repo", "vimcode", "--artifact", "macos-dmg"],
        )
        assert result.exit_code == 2
        assert "no acceptance driver configured" in result.output

    def test_infra_blocked_plan_exits_2_and_prints_reason(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from coord.cli import main

        monkeypatch.setattr("coord.commands.smoke._load_config", lambda path: _config())
        monkeypatch.setattr("coord.board_service.read_board", lambda: Board())
        monkeypatch.setattr("coord.github_ops.get_open_issues", lambda slug: [])
        monkeypatch.setattr("coord.commands.smoke._fetch_closed_issues", lambda slug: [])
        report = NightlyRunReport(
            plan=_plan(infra_blocked=True, infra_reason="screen is locked"), ran=False,
        )
        monkeypatch.setattr(
            "coord.nightly_runner.run_nightly_smoke", lambda **kwargs: report,
        )
        result = CliRunner().invoke(
            main, ["smoke", "nightly", "--repo", "vimcode", "--artifact", "macos-dmg"],
        )
        assert result.exit_code == 2
        assert "INFRA BLOCKED" in result.output
        assert "screen is locked" in result.output

    def test_open_issues_fetch_failure_exits_2_not_a_traceback(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#3660 review non-blocking item: a GitHub hiccup fetching open
        issues (the dedupe decision's primary input) must exit 2 like
        every other failure mode, never an unguarded traceback."""
        from coord.cli import main

        monkeypatch.setattr("coord.commands.smoke._load_config", lambda path: _config())
        monkeypatch.setattr("coord.board_service.read_board", lambda: Board())

        def _boom(slug: str) -> list[dict]:
            raise RuntimeError("gh: connection reset")

        monkeypatch.setattr("coord.github_ops.get_open_issues", _boom)
        monkeypatch.setattr("coord.commands.smoke._fetch_closed_issues", lambda slug: [])
        result = CliRunner().invoke(
            main, ["smoke", "nightly", "--repo", "vimcode", "--artifact", "macos-dmg"],
        )
        assert result.exit_code == 2
        assert "could not fetch open issues" in result.output

    def test_dry_run_prints_plan_and_exits_0(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from coord.cli import main

        monkeypatch.setattr("coord.commands.smoke._load_config", lambda path: _config())
        monkeypatch.setattr("coord.board_service.read_board", lambda: Board())
        report = NightlyRunReport(plan=_plan(), ran=False)
        captured = {}

        def _fake_run(**kwargs):
            captured.update(kwargs)
            return report

        monkeypatch.setattr("coord.nightly_runner.run_nightly_smoke", _fake_run)
        result = CliRunner().invoke(
            main,
            ["smoke", "nightly", "--repo", "vimcode", "--artifact", "macos-dmg", "--dry-run"],
        )
        assert result.exit_code == 0, result.output
        assert "nightly smoke plan" in result.output
        assert captured["dry_run"] is True

    def test_successful_run_exits_0_and_lists_outcomes(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from coord.cli import main

        monkeypatch.setattr("coord.commands.smoke._load_config", lambda path: _config())
        monkeypatch.setattr("coord.board_service.read_board", lambda: Board())
        monkeypatch.setattr("coord.github_ops.get_open_issues", lambda slug: [])
        monkeypatch.setattr("coord.commands.smoke._fetch_closed_issues", lambda slug: [])
        report = NightlyRunReport(
            plan=_plan(), ran=True, sha="deadbeef",
            outcomes=(_outcome(action="filed"),),
        )
        monkeypatch.setattr(
            "coord.nightly_runner.run_nightly_smoke", lambda **kwargs: report,
        )
        result = CliRunner().invoke(
            main, ["smoke", "nightly", "--repo", "vimcode", "--artifact", "macos-dmg"],
        )
        assert result.exit_code == 0, result.output
        assert "filed" in result.output
        assert "#99" in result.output

    def test_dropped_outcome_exits_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from coord.cli import main

        monkeypatch.setattr("coord.commands.smoke._load_config", lambda path: _config())
        monkeypatch.setattr("coord.board_service.read_board", lambda: Board())
        monkeypatch.setattr("coord.github_ops.get_open_issues", lambda slug: [])
        monkeypatch.setattr("coord.commands.smoke._fetch_closed_issues", lambda slug: [])
        report = NightlyRunReport(
            plan=_plan(), ran=True, sha="deadbeef",
            outcomes=(_outcome(action="not-filed"),),
        )
        monkeypatch.setattr(
            "coord.nightly_runner.run_nightly_smoke", lambda **kwargs: report,
        )
        result = CliRunner().invoke(
            main, ["smoke", "nightly", "--repo", "vimcode", "--artifact", "macos-dmg"],
        )
        assert result.exit_code == 1, result.output

    def test_json_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from coord.cli import main

        monkeypatch.setattr("coord.commands.smoke._load_config", lambda path: _config())
        monkeypatch.setattr("coord.board_service.read_board", lambda: Board())
        report = NightlyRunReport(plan=_plan(), ran=False)
        monkeypatch.setattr(
            "coord.nightly_runner.run_nightly_smoke", lambda **kwargs: report,
        )
        result = CliRunner().invoke(
            main,
            ["smoke", "nightly", "--repo", "vimcode", "--artifact", "macos-dmg",
             "--dry-run", "--json"],
        )
        assert result.exit_code == 0, result.output
        import json

        payload = json.loads(result.output)
        assert payload["repo"] == "vimcode"
        assert payload["ran"] is False
