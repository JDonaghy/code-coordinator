"""Black-box tests for `coord issue view` and `coord issue list` (#2484).

The read-side counterpart to `coord issue create`/`edit`/`close`/`reopen`/
`label` — closes the gap where an interactive/coordinator session had no
seam-covered way to read issue state and fell back to raw `gh issue view` /
`gh issue list --search`.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from coord.cli import main


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


# ── coord issue view ─────────────────────────────────────────────────────


class TestIssueView:
    def test_view_prints_issue_and_comments(self, config_file: Path) -> None:
        issue_data = {
            "number": 42,
            "title": "Fix the thing",
            "state": "OPEN",
            "body": "Detailed description here.",
            "labels": [{"name": "bug"}],
            "milestone": {"title": "M1"},
        }
        comments = [
            {"author": {"login": "alice"}, "createdAt": "2026-01-01T00:00:00Z", "body": "first comment"},
        ]
        with patch("coord.github_ops.get_issue", return_value=issue_data) as mock_get, \
             patch("coord.github_ops.get_issue_comments", return_value=comments) as mock_comments:
            result = CliRunner().invoke(
                main,
                ["issue", "view", "api", "42", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        assert "#42" in result.output
        assert "acme/api" in result.output
        assert "Fix the thing" in result.output
        assert "bug" in result.output
        assert "M1" in result.output
        assert "Detailed description here." in result.output
        assert "alice" in result.output
        assert "first comment" in result.output
        mock_get.assert_called_once_with("acme/api", 42)
        mock_comments.assert_called_once_with("acme/api", 42)

    def test_view_no_comments_flag_skips_comment_fetch(self, config_file: Path) -> None:
        issue_data = {
            "number": 7, "title": "Quiet issue", "state": "OPEN",
            "body": "body", "labels": [], "milestone": None,
        }
        with patch("coord.github_ops.get_issue", return_value=issue_data), \
             patch("coord.github_ops.get_issue_comments") as mock_comments:
            result = CliRunner().invoke(
                main,
                ["issue", "view", "api", "7", "--no-comments", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        mock_comments.assert_not_called()
        assert "comment(s)" not in result.output

    def test_view_json_output(self, config_file: Path) -> None:
        issue_data = {"number": 5, "title": "T", "state": "OPEN", "body": "b", "labels": []}
        with patch("coord.github_ops.get_issue", return_value=issue_data), \
             patch("coord.github_ops.get_issue_comments", return_value=[]):
            result = CliRunner().invoke(
                main,
                ["issue", "view", "api", "5", "--json", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        import json as _json

        parsed = _json.loads(result.output)
        assert parsed["number"] == 5
        assert parsed["comments"] == []

    def test_view_not_found_exits_nonzero(self, config_file: Path) -> None:
        with patch("coord.github_ops.get_issue", return_value={}):
            result = CliRunner().invoke(
                main,
                ["issue", "view", "api", "999", "--config", str(config_file)],
            )
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_view_gh_failure_exits_nonzero(self, config_file: Path) -> None:
        with patch("coord.github_ops.get_issue", side_effect=RuntimeError("gh: network error")):
            result = CliRunner().invoke(
                main,
                ["issue", "view", "api", "1", "--config", str(config_file)],
            )
        assert result.exit_code == 1
        assert "error" in result.output.lower()

    def test_view_unknown_repo_errors_cleanly_without_reaching_gh(
        self, config_file: Path
    ) -> None:
        """#2655: an unresolvable local name must never fall through to
        `gh` — it must fail with the same clean seam-level error
        `coord plans` already uses, naming the bad input and coordinator.yml,
        and the forge backend must never be invoked."""
        with patch("coord.github_ops.get_issue") as mock_get:
            result = CliRunner().invoke(
                main,
                ["issue", "view", "code-coordinator", "42", "--config", str(config_file)],
            )
        assert result.exit_code != 0
        assert "unknown repo 'code-coordinator'" in result.output
        assert "coordinator.yml" in result.output
        mock_get.assert_not_called()

    def test_view_raw_slug_fallback_still_works(self, config_file: Path) -> None:
        """A value that already looks like an OWNER/REPO slug (contains
        '/') is a deliberate escape hatch for a repo not tracked in
        coordinator.yml at all — #2655 must not remove it."""
        issue_data = {
            "number": 9, "title": "T", "state": "OPEN", "body": "b", "labels": [],
        }
        with patch("coord.github_ops.get_issue", return_value=issue_data) as mock_get, \
             patch("coord.github_ops.get_issue_comments", return_value=[]):
            result = CliRunner().invoke(
                main,
                ["issue", "view", "JDonaghy/code-coordinator", "9", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        mock_get.assert_called_once_with("JDonaghy/code-coordinator", 9)


# ── coord issue list ─────────────────────────────────────────────────────


class TestIssueList:
    def test_list_default_open_state(self, config_file: Path) -> None:
        issues = [
            {"number": 1, "title": "A", "state": "OPEN", "labels": [{"name": "bug"}]},
            {"number": 2, "title": "B", "state": "OPEN", "labels": []},
        ]
        with patch("coord.github_ops.search_issues", return_value=issues) as mock_search:
            result = CliRunner().invoke(
                main,
                ["issue", "list", "api", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        assert "#1" in result.output
        assert "#2" in result.output
        assert "bug" in result.output
        mock_search.assert_called_once_with(
            "acme/api", state="open", search=None, milestone=None, label=None, limit=100,
        )

    def test_list_forwards_filters(self, config_file: Path) -> None:
        with patch("coord.github_ops.search_issues", return_value=[]) as mock_search:
            result = CliRunner().invoke(
                main,
                [
                    "issue", "list", "api",
                    "--state", "all",
                    "--search", "flaky test",
                    "--milestone", "M1",
                    "--label", "bug",
                    "--limit", "25",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        mock_search.assert_called_once_with(
            "acme/api", state="all", search="flaky test", milestone="M1", label="bug", limit=25,
        )

    def test_list_empty_result(self, config_file: Path) -> None:
        with patch("coord.github_ops.search_issues", return_value=[]):
            result = CliRunner().invoke(
                main,
                ["issue", "list", "api", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        assert "no issues" in result.output.lower()

    def test_list_json_output(self, config_file: Path) -> None:
        issues = [{"number": 3, "title": "C", "state": "OPEN", "labels": []}]
        with patch("coord.github_ops.search_issues", return_value=issues):
            result = CliRunner().invoke(
                main,
                ["issue", "list", "api", "--json", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        import json as _json

        parsed = _json.loads(result.output)
        assert parsed == issues

    def test_list_gh_failure_exits_nonzero(self, config_file: Path) -> None:
        with patch("coord.github_ops.search_issues", side_effect=RuntimeError("gh: rate limited")):
            result = CliRunner().invoke(
                main,
                ["issue", "list", "api", "--config", str(config_file)],
            )
        assert result.exit_code == 1
        assert "error" in result.output.lower()

    def test_list_unknown_repo_errors_cleanly_without_reaching_gh(
        self, config_file: Path
    ) -> None:
        """#2655 repro: `coord issue list code-coordinator` against a
        coordinator.yml whose repo is named 'api' (here) / 'claude-coordinator'
        (real fleet) must not leak a raw gh invocation error — it must name
        the bad input and coordinator.yml, and never call the backend."""
        with patch("coord.github_ops.search_issues") as mock_search:
            result = CliRunner().invoke(
                main,
                ["issue", "list", "code-coordinator", "--config", str(config_file)],
            )
        assert result.exit_code != 0
        assert "unknown repo 'code-coordinator'" in result.output
        assert "coordinator.yml" in result.output
        mock_search.assert_not_called()

    def test_list_raw_slug_fallback_still_works(self, config_file: Path) -> None:
        with patch("coord.github_ops.search_issues", return_value=[]) as mock_search:
            result = CliRunner().invoke(
                main,
                ["issue", "list", "JDonaghy/code-coordinator", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        mock_search.assert_called_once_with(
            "JDonaghy/code-coordinator",
            state="open", search=None, milestone=None, label=None, limit=100,
        )
