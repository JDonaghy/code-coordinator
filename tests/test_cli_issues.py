"""Black-box tests for `coord issue create --milestone` (#3432).

Coverage targets (mirroring test_cli_issue_create_label.py /
test_cli_milestone_assign.py):
- --milestone by number: resolves title via github_ops.get_milestone, creates
  the issue, then assigns it through state.assign_issue_milestone.
- --milestone by title: resolves via github_ops.get_repo_milestones.
- Unresolvable milestone (bad number, unmatched/ambiguous title) is an error
  *before* the issue is created — github_ops.create_issue must never be
  called.
- --milestone composes with --label and with the bug-lane intake flags.
- A milestone-assign failure *after* a successful create still surfaces as a
  non-zero exit with the issue number in the message (the create already
  happened and is not rolled back).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord import state as state_mod


CONFIG_YAML = """\
repos:
  - name: api
    github: acme/api
    default_branch: main
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


class TestIssueCreateWithMilestone:
    def test_milestone_by_number_creates_and_assigns(self, config_file: Path) -> None:
        with patch(
            "coord.github_ops.get_milestone",
            return_value={"number": 7, "title": "v1.0"},
        ), patch(
            "coord.github_ops.create_issue",
            return_value={"number": 42, "url": "https://github.com/acme/api/issues/42"},
        ) as mock_create, patch(
            "coord.github_ops.assign_issue_milestone",
        ) as mock_assign:
            result = CliRunner().invoke(
                main,
                [
                    "issue", "create", "api",
                    "--title", "Needs a milestone",
                    "--body", "body text",
                    "--milestone", "7",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        assert "#42" in result.output
        assert "v1.0" in result.output
        mock_create.assert_called_once()
        mock_assign.assert_called_once_with("acme/api", 42, 7)

        # The daemon seam updates the local cache immediately (no `coord sync` wait).
        row = state_mod.get_connection().execute(
            "SELECT milestone_number, milestone_title FROM issues"
            " WHERE repo_name='api' AND number=42"
        ).fetchone()
        assert row is not None
        assert row["milestone_number"] == 7
        assert row["milestone_title"] == "v1.0"

    def test_milestone_by_title_resolves_number(self, config_file: Path) -> None:
        with patch(
            "coord.github_ops.get_repo_milestones",
            return_value=[
                {"number": 7, "title": "v1.0"},
                {"number": 8, "title": "v2.0"},
            ],
        ), patch(
            "coord.github_ops.create_issue",
            return_value={"number": 43, "url": "https://github.com/acme/api/issues/43"},
        ), patch(
            "coord.github_ops.assign_issue_milestone",
        ) as mock_assign:
            result = CliRunner().invoke(
                main,
                [
                    "issue", "create", "api",
                    "--title", "By title",
                    "--milestone", "v2.0",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        assert "#43" in result.output
        mock_assign.assert_called_once_with("acme/api", 43, 8)

    def test_unresolvable_milestone_number_blocks_creation(
        self, config_file: Path
    ) -> None:
        """An unfetchable numeric milestone must fail *before* the issue is
        created — create_issue must never be called (#3432)."""
        with patch(
            "coord.github_ops.get_milestone",
            side_effect=RuntimeError("HTTP 404"),
        ), patch(
            "coord.github_ops.create_issue",
        ) as mock_create:
            result = CliRunner().invoke(
                main,
                [
                    "issue", "create", "api",
                    "--title", "Doomed",
                    "--milestone", "999",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 1
        assert "error" in result.output.lower()
        mock_create.assert_not_called()

    def test_unmatched_title_blocks_creation(self, config_file: Path) -> None:
        with patch(
            "coord.github_ops.get_repo_milestones",
            return_value=[{"number": 7, "title": "v1.0"}],
        ), patch(
            "coord.github_ops.create_issue",
        ) as mock_create:
            result = CliRunner().invoke(
                main,
                [
                    "issue", "create", "api",
                    "--title", "Doomed",
                    "--milestone", "nonexistent",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 1
        assert "error" in result.output.lower()
        mock_create.assert_not_called()

    def test_ambiguous_title_blocks_creation(self, config_file: Path) -> None:
        with patch(
            "coord.github_ops.get_repo_milestones",
            return_value=[
                {"number": 3, "title": "dup"},
                {"number": 4, "title": "dup"},
            ],
        ), patch(
            "coord.github_ops.create_issue",
        ) as mock_create:
            result = CliRunner().invoke(
                main,
                [
                    "issue", "create", "api",
                    "--title", "Doomed",
                    "--milestone", "dup",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 1
        assert "multiple" in result.output.lower() or "error" in result.output.lower()
        mock_create.assert_not_called()

    def test_milestone_composes_with_label(self, config_file: Path) -> None:
        with patch(
            "coord.github_ops.get_milestone",
            return_value={"number": 7, "title": "v1.0"},
        ), patch(
            "coord.github_ops.create_issue",
            return_value={"number": 44, "url": "https://github.com/acme/api/issues/44"},
        ) as mock_create, patch(
            "coord.github_ops.assign_issue_milestone",
        ) as mock_assign:
            result = CliRunner().invoke(
                main,
                [
                    "issue", "create", "api",
                    "--title", "With label",
                    "--label", "bug",
                    "--milestone", "7",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        assert mock_create.call_args.kwargs.get("labels") == ["bug"]
        mock_assign.assert_called_once_with("acme/api", 44, 7)

    def test_milestone_composes_with_bug_lane_fields(self, config_file: Path) -> None:
        with patch(
            "coord.github_ops.get_milestone",
            return_value={"number": 7, "title": "v1.0"},
        ), patch(
            "coord.github_ops.create_issue",
            return_value={"number": 45, "url": "https://github.com/acme/api/issues/45"},
        ) as mock_create, patch(
            "coord.github_ops.assign_issue_milestone",
        ) as mock_assign:
            result = CliRunner().invoke(
                main,
                [
                    "issue", "create", "api",
                    "--title", "Bug with milestone",
                    "--expected", "x", "--actual", "y", "--repro", "z", "--evidence", "w",
                    "--milestone", "7",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        called_body = mock_create.call_args[0][2]
        assert "## Expected behaviour" in called_body
        mock_assign.assert_called_once_with("acme/api", 45, 7)

    def test_assign_failure_after_create_exits_nonzero_and_mentions_issue(
        self, config_file: Path
    ) -> None:
        """Assignment failing after a successful create is not silently
        swallowed — it must surface as a non-zero exit naming the issue that
        was created but left unattached (#3432)."""
        with patch(
            "coord.github_ops.get_milestone",
            return_value={"number": 7, "title": "v1.0"},
        ), patch(
            "coord.github_ops.create_issue",
            return_value={"number": 46, "url": "https://github.com/acme/api/issues/46"},
        ), patch(
            "coord.github_ops.assign_issue_milestone",
            side_effect=RuntimeError("gh: network error"),
        ):
            result = CliRunner().invoke(
                main,
                [
                    "issue", "create", "api",
                    "--title", "Assign fails",
                    "--milestone", "7",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 1
        assert "#46" in result.output
        assert "error" in result.output.lower()

    def test_no_milestone_option_unchanged_behaviour(self, config_file: Path) -> None:
        """Omitting --milestone entirely never touches the milestone seam."""
        with patch(
            "coord.github_ops.create_issue",
            return_value={"number": 47, "url": "https://github.com/acme/api/issues/47"},
        ), patch(
            "coord.github_ops.assign_issue_milestone",
        ) as mock_assign:
            result = CliRunner().invoke(
                main,
                [
                    "issue", "create", "api",
                    "--title", "No milestone",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        mock_assign.assert_not_called()
