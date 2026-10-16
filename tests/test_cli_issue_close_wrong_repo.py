"""Black-box tests for `coord issue close-wrong-repo` (#3711).

A worker that discovers mid-investigation that the root cause lives in a
different repo previously had no recourse but to say so in its final message
and stop — the queue kept dispatching fresh workers at the same wrong-repo
issue in the meantime. `coord issue close-wrong-repo` lets it file the
replacement issue in the correct repo and close the current one with a
`coord:event=wrong_repo` marker comment in one atomic step.

Coverage targets:
- Happy path: files the new issue in --target, closes REPO#ISSUE with a
  `coord:event=wrong_repo` marker comment, and writes a pinned
  `issue_context` entry on both sides linking them to each other.
- --body / --body-file are mutually exclusive.
- Missing --body and --body-file is an error.
- --body-file reads from a file (and '-' reads from stdin).
- A failure filing the new issue must never close the source issue.
- A failure closing the source issue (after the new issue was already
  created) surfaces as a non-zero exit that names the already-created issue.
- --target accepts a raw owner/repo slug as well as a coordinator.yml name.
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
  - name: quadraui
    github: acme/quadraui
    default_branch: main
  - name: vimcode
    github: acme/vimcode
    default_branch: main
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [quadraui, vimcode]
    repo_paths:
      quadraui: /tmp/quadraui
      vimcode: /tmp/vimcode
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


class TestIssueCloseWrongRepo:
    def test_happy_path_files_closes_and_links(self, config_file: Path) -> None:
        with patch(
            "coord.github_ops.create_issue",
            return_value={"number": 99, "url": "https://github.com/acme/vimcode/issues/99"},
        ) as mock_create, patch(
            "coord.github_ops.close_issue",
        ) as mock_close:
            result = CliRunner().invoke(
                main,
                [
                    "issue", "close-wrong-repo", "quadraui", "1554",
                    "--target", "vimcode",
                    "--title", "Fix dialog sizing in dialogs.rs",
                    "--body", "Root cause is in vimcode's src/app/dialogs.rs.",
                    "--reason", "quadraui never owned this dialog code.",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        assert "#99" in result.output
        assert "closed as wrong-repo" in result.output

        mock_create.assert_called_once_with(
            "acme/vimcode",
            "Fix dialog sizing in dialogs.rs",
            "Root cause is in vimcode's src/app/dialogs.rs.",
            labels=[],
        )

        mock_close.assert_called_once()
        close_args, close_kwargs = mock_close.call_args
        assert close_args[0] == "acme/quadraui"
        assert close_args[1] == 1554
        assert close_kwargs["force"] is False
        comment = close_kwargs["comment"]
        assert "acme/vimcode#99" in comment
        assert "quadraui never owned this dialog code." in comment
        assert "<!-- coord:event=wrong_repo target=acme/vimcode issue=99 -->" in comment

        rows = state_mod.get_connection().execute(
            "SELECT repo_name, issue_number, pinned, source, body FROM issue_context"
            " ORDER BY id"
        ).fetchall()
        assert len(rows) == 2
        source_row, target_row = rows
        assert source_row["repo_name"] == "quadraui"
        assert source_row["issue_number"] == 1554
        assert source_row["pinned"] == 1
        assert source_row["source"] == "wrong-repo"
        assert "vimcode#99" in source_row["body"]
        assert target_row["repo_name"] == "vimcode"
        assert target_row["issue_number"] == 99
        assert target_row["pinned"] == 1
        assert "quadraui#1554" in target_row["body"]

    def test_target_as_raw_slug(self, config_file: Path) -> None:
        with patch(
            "coord.github_ops.create_issue",
            return_value={"number": 7, "url": "https://github.com/other/repo/issues/7"},
        ), patch(
            "coord.github_ops.close_issue",
        ) as mock_close:
            result = CliRunner().invoke(
                main,
                [
                    "issue", "close-wrong-repo", "quadraui", "10",
                    "--target", "other/repo",
                    "--title", "Belongs elsewhere",
                    "--body", "body",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        assert "#7 (other/repo) created" in result.output
        mock_close.assert_called_once()

    def test_body_and_body_file_mutually_exclusive(
        self, config_file: Path, tmp_path: Path
    ) -> None:
        body_file = tmp_path / "body.md"
        body_file.write_text("from file")
        result = CliRunner().invoke(
            main,
            [
                "issue", "close-wrong-repo", "quadraui", "1",
                "--target", "vimcode",
                "--title", "t",
                "--body", "inline",
                "--body-file", str(body_file),
                "--config", str(config_file),
            ],
        )
        assert result.exit_code == 2
        assert "mutually exclusive" in result.output.lower()

    def test_missing_body_is_an_error(self, config_file: Path) -> None:
        result = CliRunner().invoke(
            main,
            [
                "issue", "close-wrong-repo", "quadraui", "1",
                "--target", "vimcode",
                "--title", "t",
                "--config", str(config_file),
            ],
        )
        assert result.exit_code == 2
        assert "error" in result.output.lower()

    def test_body_file_is_read(self, config_file: Path, tmp_path: Path) -> None:
        body_file = tmp_path / "body.md"
        body_file.write_text("root cause analysis from file")
        with patch(
            "coord.github_ops.create_issue",
            return_value={"number": 12, "url": "https://github.com/acme/vimcode/issues/12"},
        ) as mock_create, patch(
            "coord.github_ops.close_issue",
        ):
            result = CliRunner().invoke(
                main,
                [
                    "issue", "close-wrong-repo", "quadraui", "2",
                    "--target", "vimcode",
                    "--title", "t",
                    "--body-file", str(body_file),
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        assert mock_create.call_args[0][2] == "root cause analysis from file"

    def test_create_failure_does_not_close_source(self, config_file: Path) -> None:
        with patch(
            "coord.github_ops.create_issue",
            side_effect=RuntimeError("gh: network error"),
        ), patch(
            "coord.github_ops.close_issue",
        ) as mock_close:
            result = CliRunner().invoke(
                main,
                [
                    "issue", "close-wrong-repo", "quadraui", "1554",
                    "--target", "vimcode",
                    "--title", "t",
                    "--body", "body",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 1
        assert "error" in result.output.lower()
        mock_close.assert_not_called()

    def test_close_failure_after_create_mentions_new_issue(
        self, config_file: Path
    ) -> None:
        with patch(
            "coord.github_ops.create_issue",
            return_value={"number": 55, "url": "https://github.com/acme/vimcode/issues/55"},
        ), patch(
            "coord.github_ops.close_issue",
            side_effect=RuntimeError("gh: permission denied"),
        ):
            result = CliRunner().invoke(
                main,
                [
                    "issue", "close-wrong-repo", "quadraui", "1554",
                    "--target", "vimcode",
                    "--title", "t",
                    "--body", "body",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 1
        assert "#55" in result.output
        assert "error" in result.output.lower()
