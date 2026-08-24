"""Tests for `coord uat` — the pre-merge UAT-gate verdict command (#2687)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.config import load
from coord.models import Assignment, Board
from coord.state import save_board


@pytest.fixture
def config_file_with_uat(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n"
        "    github: acme/api\n"
        "    uat_preview: 'https://{pr_branch_slug}.example.pages.dev/'\n"
        "machines:\n"
        "  - name: testbox\n    host: testbox.tailnet\n    repos: [api]\n"
    )
    return p


@pytest.fixture
def config_file_without_uat(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: acme/api\n"
        "machines:\n"
        "  - name: testbox\n    host: testbox.tailnet\n    repos: [api]\n"
    )
    return p


@pytest.fixture
def board_with_done(coord_db) -> Board:
    board = Board(completed=[
        Assignment(
            machine_name="testbox",
            repo_name="api",
            issue_number=42,
            issue_title="Fix chart colors",
            assignment_id="abc123",
            status="done",
            branch="issue-42-fix-chart-colors",
            finished_at=1000.0,
        ),
    ])
    save_board(board)
    return board


class TestRepoUatPreviewConfig:
    def test_uat_preview_parsed(self, config_file_with_uat: Path) -> None:
        cfg = load(config_file_with_uat)
        assert cfg.repo("api").uat_preview == (
            "https://{pr_branch_slug}.example.pages.dev/"
        )

    def test_uat_preview_optional(self, config_file_without_uat: Path) -> None:
        cfg = load(config_file_without_uat)
        assert cfg.repo("api").uat_preview is None


class TestUatVerdict:
    def test_passed_records_on_board_and_prints_preview(
        self, config_file_with_uat: Path, board_with_done: Board,
    ) -> None:
        result = CliRunner().invoke(main, [
            "uat", "abc123", "--passed",
            "--config", str(config_file_with_uat),
        ])
        assert result.exit_code == 0, result.output
        assert "PASSED" in result.output
        assert "preview: https://issue-42-fix-chart-colors.example.pages.dev/" in result.output

        from coord.state import load_board
        board = load_board()
        assert board.completed[0].uat_state == "passed"

    def test_failed_records_note_and_issue_context(
        self, config_file_with_uat: Path, board_with_done: Board,
    ) -> None:
        result = CliRunner().invoke(main, [
            "uat", "abc123", "--failed", "--note", "logo is cropped on mobile",
            "--config", str(config_file_with_uat),
        ])
        assert result.exit_code == 0, result.output
        assert "FAILED" in result.output
        assert "logo is cropped on mobile" in result.output

        from coord.state import load_board
        board = load_board()
        assert board.completed[0].uat_state == "failed"
        assert board.completed[0].uat_reason == "logo is cropped on mobile"

    def test_requires_a_verdict_flag(
        self, config_file_with_uat: Path, board_with_done: Board,
    ) -> None:
        result = CliRunner().invoke(main, [
            "uat", "abc123",
            "--config", str(config_file_with_uat),
        ])
        assert result.exit_code != 0
        assert "--passed or --failed" in result.output

    def test_unknown_assignment_errors(self, config_file_with_uat: Path, coord_db) -> None:
        result = CliRunner().invoke(main, [
            "uat", "nope", "--passed",
            "--config", str(config_file_with_uat),
        ])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_warns_when_repo_has_not_opted_in(
        self, config_file_without_uat: Path, board_with_done: Board,
    ) -> None:
        # Recording still succeeds — an operator can stamp a verdict on
        # record even before the repo opts in — but the merge gate won't
        # enforce it, and the command says so.
        result = CliRunner().invoke(main, [
            "uat", "abc123", "--passed",
            "--config", str(config_file_without_uat),
        ])
        assert result.exit_code == 0, result.output
        assert "has no uat_preview configured" in result.output

        from coord.state import load_board
        assert load_board().completed[0].uat_state == "passed"


class TestUatVerdictMergeGateIntegration:
    def test_failed_verdict_blocks_merge_gate(
        self, config_file_with_uat: Path, board_with_done: Board,
    ) -> None:
        from coord import merge_queue as mq
        from coord.config import load as load_cfg
        from coord.state import load_board

        cfg = load_cfg(config_file_with_uat)
        cfg.pipeline.default_gates = ["uat", "merge"]

        entry = mq.QueuedMerge(
            assignment_id="abc123", repo_name="api", repo_github="acme/api",
            branch="issue-42-fix-chart-colors", target_branch="main",
            issue_number=42, issue_title="t",
        )
        board = load_board()
        assert mq.passes_merge_gates(entry, cfg, board) is False

        CliRunner().invoke(main, [
            "uat", "abc123", "--passed", "--config", str(config_file_with_uat),
        ])
        board = load_board()
        assert mq.passes_merge_gates(entry, cfg, board) is True
