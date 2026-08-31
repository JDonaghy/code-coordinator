"""Tests for session lifecycle hooks — config, execution, and CLI integration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from click.testing import CliRunner

from coord.cli import main
from coord.config import Config, ConfigError, HooksConfig, load
from coord.hooks import is_round_complete, run_hooks, _summary_report
from coord.models import Assignment, Board, Machine, Repo
from coord.state import save_board


# ── Config parsing ──────────────────────────────────────────────────────────


class TestHooksConfig:
    def test_hooks_parsed_from_yaml(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: a/a\n"
            "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
            "hooks:\n"
            "  on_round_complete:\n"
            "    - close_merged_issues\n"
            "    - summary_report\n"
            "  on_session_end:\n"
            "    - summary_report\n"
        )
        cfg = load(p)
        assert cfg.hooks.on_round_complete == ["close_merged_issues", "summary_report"]
        assert cfg.hooks.on_session_end == ["summary_report"]

    def test_no_hooks_section_gives_empty(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: a/a\n"
            "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
        )
        cfg = load(p)
        assert cfg.hooks.on_round_complete == []
        assert cfg.hooks.on_session_end == []

    def test_unknown_hook_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: a/a\n"
            "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
            "hooks:\n"
            "  on_round_complete:\n"
            "    - nonexistent_hook\n"
        )
        import pytest
        with pytest.raises(ConfigError, match="unknown hooks"):
            load(p)

    @pytest.mark.skipif(
        not (Path(__file__).resolve().parents[1] / "coordinator.yml").exists(),
        reason="coordinator.yml is gitignored",
    )
    def test_example_config_parses_hooks(self) -> None:
        cfg = load(Path(__file__).resolve().parents[1] / "coordinator.yml")
        assert "summary_report" in cfg.hooks.on_round_complete


# ── Round completion detection ──────────────────────────────────────────────


class TestRoundCompletion:
    def test_complete_when_no_active_but_has_completed(self) -> None:
        board = Board(completed=[
            Assignment(machine_name="m", repo_name="r", issue_number=1,
                       issue_title="t", status="done"),
        ])
        assert is_round_complete(board)

    def test_not_complete_when_active_remain(self) -> None:
        board = Board(
            active=[
                Assignment(machine_name="m", repo_name="r", issue_number=1,
                           issue_title="t", status="running"),
            ],
            completed=[
                Assignment(machine_name="m2", repo_name="r", issue_number=2,
                           issue_title="t2", status="done"),
            ],
        )
        assert not is_round_complete(board)

    def test_not_complete_when_empty_board(self) -> None:
        assert not is_round_complete(Board())


# ── Hook execution ──────────────────────────────────────────────────────────


class TestRunHooks:
    def test_summary_report_hook(self) -> None:
        config = Config(
            repos=[Repo(name="api", github="a/a")],
            machines=[Machine(name="m", host="h")],
            hooks=HooksConfig(on_session_end=["summary_report"]),
        )
        board = Board(
            round_number=3,
            completed=[
                Assignment(machine_name="m", repo_name="api", issue_number=1,
                           issue_title="Fix auth", status="done"),
                Assignment(machine_name="m", repo_name="api", issue_number=2,
                           issue_title="Add logging", status="failed"),
            ],
        )
        results = run_hooks("on_session_end", config, board)
        assert len(results) == 1
        assert results[0].ok
        assert "Round 3" in results[0].message
        assert "1 assignment(s)" in results[0].message  # completed
        assert "Fix auth" in results[0].message
        assert "Add logging" in results[0].message

    @patch("coord.hooks.github_ops.close_issue")
    def test_close_merged_issues_hook(self, mock_close: MagicMock) -> None:
        config = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(name="m", host="h")],
            hooks=HooksConfig(on_round_complete=["close_merged_issues"]),
        )
        board = Board(completed=[
            Assignment(machine_name="m", repo_name="api", issue_number=42,
                       issue_title="Fix auth", status="done", assignment_id="abc"),
        ])
        results = run_hooks("on_round_complete", config, board)
        assert len(results) == 1
        assert results[0].ok
        assert "closed 1" in results[0].message
        mock_close.assert_called_once()
        args, kwargs = mock_close.call_args
        assert args == ("acme/api", 42)
        assert "abc" in kwargs["comment"]

    @patch("coord.hooks.github_ops.close_issue")
    def test_close_merged_issues_skips_non_closes_type(self, mock_close: MagicMock) -> None:
        # #1196 hole 3: a "mock-author"/"test-author"/"audit" assignment's
        # issue_number is the milestone's tracking issue (often an epic) —
        # not something it resolved. The hook must not close it regardless
        # of `status == "done"`, mirroring CLOSES_ISSUE_TYPES elsewhere.
        config = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(name="m", host="h")],
            hooks=HooksConfig(on_round_complete=["close_merged_issues"]),
        )
        board = Board(completed=[
            Assignment(machine_name="m", repo_name="api", issue_number=1041,
                       issue_title="Epic", status="done", assignment_id="ga",
                       type="mock-author"),
        ])
        results = run_hooks("on_round_complete", config, board)
        assert len(results) == 1
        assert results[0].ok
        assert "no issues to close" in results[0].message
        mock_close.assert_not_called()

    def test_close_merged_issues_refuses_epic_with_open_children(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # #1196 hole 3 + the chokepoint: even a "work"-type assignment whose
        # issue_number happens to be an epic with open children must not be
        # closed — this exercises the real github_ops.close_issue guard
        # (#1196 hole 1), not a mocked stand-in.
        config = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(name="m", host="h")],
            hooks=HooksConfig(on_round_complete=["close_merged_issues"]),
        )
        board = Board(completed=[
            Assignment(machine_name="m", repo_name="api", issue_number=1041,
                       issue_title="Epic", status="done", assignment_id="w1"),
        ])

        def fake_gh(*args: str, **_kwargs) -> str:
            if args[:2] == ("issue", "view"):
                return (
                    '{"number": 1041, "body": "## Sub-issues\\n- [ ] #1039\\n'
                    '- [x] #1040\\n", "title": "Epic", "state": "open", '
                    '"milestone": null, "labels": []}'
                )
            if args[:2] == ("api", "graphql"):
                # #1354: the guard also does a live batch state lookup; no
                # live answer here, so it falls back to the checkbox above.
                raise RuntimeError("gh api graphql: not available in this test")
            raise AssertionError(f"unexpected gh call: {args}")

        monkeypatch.setattr("coord.github_ops._gh", fake_gh)
        results = run_hooks("on_round_complete", config, board)
        assert len(results) == 1
        assert results[0].ok
        assert "no issues to close" in results[0].message

    def test_no_hooks_configured_returns_empty(self) -> None:
        config = Config(
            repos=[], machines=[],
            hooks=HooksConfig(),
        )
        results = run_hooks("on_round_complete", config, Board())
        assert results == []

    def test_hook_failure_captured(self) -> None:
        config = Config(
            repos=[Repo(name="api", github="a/a")],
            machines=[],
            hooks=HooksConfig(on_round_complete=["close_merged_issues"]),
        )
        board = Board(completed=[
            Assignment(machine_name="m", repo_name="api", issue_number=1,
                       issue_title="t", status="done", assignment_id="x"),
        ])
        with patch("coord.hooks.github_ops._gh", side_effect=RuntimeError("gh failed")):
            results = run_hooks("on_round_complete", config, board)
        assert len(results) == 1
        assert results[0].ok
        assert "no issues to close" in results[0].message


# ── Summary report ──────────────────────────────────────────────────────────


class TestSummaryReport:
    def test_summary_includes_counts(self) -> None:
        config = Config(repos=[], machines=[])
        board = Board(
            round_number=5,
            active=[
                Assignment(machine_name="m", repo_name="r", issue_number=3,
                           issue_title="Running", status="running"),
            ],
            completed=[
                Assignment(machine_name="m", repo_name="r", issue_number=1,
                           issue_title="Done", status="done"),
                Assignment(machine_name="m", repo_name="r", issue_number=2,
                           issue_title="Failed", status="failed"),
            ],
        )
        report = _summary_report(config, board)
        assert "Round 5" in report
        assert "1 assignment(s)" in report  # done
        assert "1 assignment(s)" in report  # failed
        assert "1 assignment(s) still running" in report


# ── CLI commands ────────────────────────────────────────────────────────────


class TestCoordDone:
    def test_done_shows_summary(self, tmp_path: Path, coord_db) -> None:
        config_file = tmp_path / "coordinator.yml"
        config_file.write_text(
            "repos:\n  - name: api\n    github: a/a\n"
            "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
        )
        board = Board(
            round_number=2,
            completed=[
                Assignment(machine_name="m", repo_name="api", issue_number=1,
                           issue_title="Fix auth", status="done", finished_at=1.0),
            ],
        )
        save_board(board)

        runner = CliRunner()
        result = runner.invoke(main, ["done", "--config", str(config_file)])

        assert result.exit_code == 0
        assert "Round 2" in result.output
        assert "Session ended" in result.output

    def test_done_with_hooks(self, tmp_path: Path, coord_db) -> None:
        config_file = tmp_path / "coordinator.yml"
        config_file.write_text(
            "repos:\n  - name: api\n    github: a/a\n"
            "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
            "hooks:\n  on_session_end:\n    - summary_report\n"
        )
        board = Board(round_number=1)
        save_board(board)

        runner = CliRunner()
        result = runner.invoke(main, ["done", "--config", str(config_file)])

        assert result.exit_code == 0
        assert "session-end hooks" in result.output
        assert "summary_report" in result.output

    def test_done_warns_about_active(self, tmp_path: Path, coord_db) -> None:
        config_file = tmp_path / "coordinator.yml"
        config_file.write_text(
            "repos:\n  - name: api\n    github: a/a\n"
            "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
        )
        board = Board(active=[
            Assignment(machine_name="m", repo_name="api", issue_number=1,
                       issue_title="Still going", status="running"),
        ])
        save_board(board)

        runner = CliRunner()
        result = runner.invoke(main, ["done", "--config", str(config_file)])

        assert result.exit_code == 0
        assert "still active" in result.output


class TestHelpText:
    def test_done_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["done", "--help"])
        assert result.exit_code == 0
        assert "session" in result.output.lower() or "housekeeping" in result.output.lower()
