"""Black-box tests for `coord issue create` and `coord issue label` (#802).

Coverage targets:
- issue create: success path → echoes number, writes to DB cache
- issue create: thin-client path → POSTs to daemon seam
- issue label: add+remove success → cache updated
- issue label: no-op delta (labels already in desired state) → no gh call
- issue label: missing --add/--remove → error exit
- _apply_label_change refactor: lifecycle commands (coord ready/backlog)
  still work after routing through the seam
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord import state as state_mod


# ── shared config ─────────────────────────────────────────────────────────────


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


# ── coord issue create ─────────────────────────────────────────────────────────


class TestIssueCreate:
    def test_create_echoes_issue_number(self, config_file: Path) -> None:
        """coord issue create: success path prints #N and writes to issues cache."""
        with patch(
            "coord.github_ops.create_issue",
            return_value={"number": 42, "url": "https://github.com/acme/api/issues/42"},
        ) as mock_create:
            result = CliRunner().invoke(
                main,
                [
                    "issue", "create", "api",
                    "--title", "Fix the thing",
                    "--body", "Detailed description here.",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        assert "#42" in result.output
        assert "acme/api" in result.output
        mock_create.assert_called_once_with(
            "acme/api",
            "Fix the thing",
            "Detailed description here.",
            labels=[],
        )

    def test_create_with_labels(self, config_file: Path) -> None:
        """--label options are forwarded to github_ops.create_issue."""
        with patch(
            "coord.github_ops.create_issue",
            return_value={"number": 7, "url": "https://github.com/acme/api/issues/7"},
        ) as mock_create:
            result = CliRunner().invoke(
                main,
                [
                    "issue", "create", "api",
                    "--title", "Bug report",
                    "--label", "bug",
                    "--label", "priority:high",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        assert "#7" in result.output
        labels_kwarg = mock_create.call_args.kwargs.get("labels", [])
        assert set(labels_kwarg) == {"bug", "priority:high"}

    def test_create_inserts_into_issues_cache(self, config_file: Path) -> None:
        """After creation, the issue row is visible in the local DB cache."""
        with patch(
            "coord.github_ops.create_issue",
            return_value={"number": 55, "url": "https://github.com/acme/api/issues/55"},
        ):
            result = CliRunner().invoke(
                main,
                [
                    "issue", "create", "api",
                    "--title", "Cache test",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        conn = state_mod.get_connection()
        row = conn.execute(
            "SELECT title, state FROM issues WHERE repo_name='api' AND number=55"
        ).fetchone()
        assert row is not None, "issue row should be in cache after create"
        assert row["title"] == "Cache test"
        assert row["state"] == "open"

    def test_create_thin_client_posts_to_seam(self, config_file: Path) -> None:
        """On a thin client, create routes to the daemon via POST /issue-create."""
        fake_svc = MagicMock()
        fake_svc.url = "http://daemon:7435"
        fake_svc.token = None

        with patch("coord.state._board_service", return_value=fake_svc), \
             patch(
                 "coord.client.post_record",
                 return_value={"number": 99, "url": "https://github.com/acme/api/issues/99"},
             ) as mock_post:
            result = CliRunner().invoke(
                main,
                [
                    "issue", "create", "api",
                    "--title", "Remote create",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        assert "#99" in result.output
        mock_post.assert_called_once()
        _svc, endpoint, payload = mock_post.call_args[0]
        assert endpoint == "/issue-create"
        assert payload["title"] == "Remote create"
        assert payload["repo_name"] == "api"

    def test_create_requires_title(self, config_file: Path) -> None:
        """--title is required; omitting it exits with code 2."""
        result = CliRunner().invoke(
            main,
            ["issue", "create", "api", "--config", str(config_file)],
        )
        assert result.exit_code == 2
        assert "title" in result.output.lower() or "missing" in result.output.lower()

    def test_create_body_file(self, config_file: Path, tmp_path: Path) -> None:
        """--body-file reads the body from disk."""
        body_path = tmp_path / "body.md"
        body_path.write_text("# Long body\nContent here.")
        with patch(
            "coord.github_ops.create_issue",
            return_value={"number": 3, "url": "https://github.com/acme/api/issues/3"},
        ) as mock_create:
            result = CliRunner().invoke(
                main,
                [
                    "issue", "create", "api",
                    "--title", "With file body",
                    "--body-file", str(body_path),
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        called_body = mock_create.call_args[0][2]
        assert "Long body" in called_body

    def test_create_bug_report_renders_structured_body(self, config_file: Path) -> None:
        """#1964: --expected/--actual/--repro/--evidence render the four
        bug-lane intake fields as addressable sections via
        coord.bug_intake.format_bug_report, instead of prose."""
        with patch(
            "coord.github_ops.create_issue",
            return_value={"number": 8, "url": "https://github.com/acme/api/issues/8"},
        ) as mock_create:
            result = CliRunner().invoke(
                main,
                [
                    "issue", "create", "api",
                    "--title", "Help popup missing border",
                    "--expected", "full box border, title centred",
                    "--actual", "side-bars only, title demoted",
                    "--repro", "open the extensions panel help popup",
                    "--evidence", "screenshots of both, plus develop as reference",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        assert "#8" in result.output
        called_body = mock_create.call_args[0][2]
        assert "## Expected behaviour" in called_body
        assert "full box border, title centred" in called_body
        assert "## Evidence" in called_body
        assert "develop as reference" in called_body

    def test_create_bug_report_requires_all_four_fields(self, config_file: Path) -> None:
        result = CliRunner().invoke(
            main,
            [
                "issue", "create", "api",
                "--title", "Incomplete report",
                "--expected", "x",
                "--config", str(config_file),
            ],
        )
        assert result.exit_code == 2
        assert "must all be given together" in result.output
        assert "--actual" in result.output

    def test_create_bug_report_rejects_body_combo(self, config_file: Path) -> None:
        result = CliRunner().invoke(
            main,
            [
                "issue", "create", "api",
                "--title", "Conflicting flags",
                "--body", "freeform",
                "--expected", "x", "--actual", "y", "--repro", "z", "--evidence", "w",
                "--config", str(config_file),
            ],
        )
        assert result.exit_code == 2
        assert "mutually exclusive" in result.output

    def test_create_gh_failure_exits_nonzero(self, config_file: Path) -> None:
        """A gh RuntimeError surfaces as exit code 1 with an error message."""
        with patch(
            "coord.github_ops.create_issue",
            side_effect=RuntimeError("gh: network error"),
        ):
            result = CliRunner().invoke(
                main,
                [
                    "issue", "create", "api",
                    "--title", "Will fail",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 1
        assert "error" in result.output.lower()


# ── coord issue label ──────────────────────────────────────────────────────────


def _seed_issue(repo_name: str = "api", number: int = 10, labels: list[str] | None = None) -> None:
    """Insert a minimal issue row into the test DB."""
    import json, time
    conn = state_mod.get_connection()
    conn.execute(
        """
        INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at)
        VALUES (?, ?, 'Test issue', '', 'open', ?, ?)
        ON CONFLICT (repo_name, number) DO NOTHING
        """,
        (repo_name, number, json.dumps(labels or []), time.time()),
    )
    conn.commit()


class TestIssueLabel:
    def test_add_label_success(self, config_file: Path) -> None:
        """coord issue label --add: adds label, updates cache, prints summary."""
        _seed_issue(labels=["existing"])
        with patch(
            "coord.github_ops.change_issue_labels",
            return_value=(["bug", "existing"], True),
        ) as mock_change:
            result = CliRunner().invoke(
                main,
                [
                    "issue", "label", "api", "10",
                    "--add", "bug",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        assert "updated" in result.output
        mock_change.assert_called_once()
        # Verify cache was updated
        import json
        conn = state_mod.get_connection()
        row = conn.execute(
            "SELECT labels FROM issues WHERE repo_name='api' AND number=10"
        ).fetchone()
        assert row is not None
        assert "bug" in json.loads(row["labels"])

    def test_remove_label_success(self, config_file: Path) -> None:
        """coord issue label --remove: removes label, updates cache."""
        _seed_issue(labels=["bug", "status:ready"])
        with patch(
            "coord.github_ops.change_issue_labels",
            return_value=(["bug"], True),
        ):
            result = CliRunner().invoke(
                main,
                [
                    "issue", "label", "api", "10",
                    "--remove", "status:ready",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        assert "updated" in result.output

    def test_add_and_remove_in_one_call(self, config_file: Path) -> None:
        """--add and --remove can be combined in one invocation."""
        _seed_issue(labels=["old"])
        with patch(
            "coord.github_ops.change_issue_labels",
            return_value=(["new"], True),
        ) as mock_change:
            result = CliRunner().invoke(
                main,
                [
                    "issue", "label", "api", "10",
                    "--add", "new",
                    "--remove", "old",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        # change_issue_labels received the right sets
        call_kwargs = mock_change.call_args.kwargs
        assert "new" in call_kwargs["add"]
        assert "old" in call_kwargs["remove"]

    def test_noop_when_labels_already_in_desired_state(self, config_file: Path) -> None:
        """When no delta exists (label already present/absent), echoes 'unchanged'."""
        _seed_issue(labels=["bug"])
        with patch(
            "coord.github_ops.change_issue_labels",
            return_value=(["bug"], False),  # changed=False
        ):
            result = CliRunner().invoke(
                main,
                [
                    "issue", "label", "api", "10",
                    "--add", "bug",  # already present — no delta
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        assert "unchanged" in result.output

    def test_no_options_exits_with_error(self, config_file: Path) -> None:
        """Providing neither --add nor --remove is a usage error (exit 2)."""
        result = CliRunner().invoke(
            main,
            ["issue", "label", "api", "10", "--config", str(config_file)],
        )
        assert result.exit_code == 2
        assert "error" in result.output.lower()

    def test_thin_client_posts_to_seam(self, config_file: Path) -> None:
        """On a thin client, label routes to the daemon.

        #1946: via ``PATCH /issue/{repo}/{n}`` (``add_labels``), replacing the
        deprecated ``POST /issue-label``.
        """
        fake_svc = MagicMock()
        fake_svc.url = "http://daemon:7435"
        fake_svc.token = None

        with patch("coord.state._board_service", return_value=fake_svc), \
             patch(
                 "coord.client.request_resource",
                 return_value={
                     "updated": True, "applied": ["labels"],
                     "labels": ["bug"], "labels_changed": True,
                 },
             ) as mock_req:
            result = CliRunner().invoke(
                main,
                [
                    "issue", "label", "api", "10",
                    "--add", "bug",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        mock_req.assert_called_once()
        _svc, method, path, payload = mock_req.call_args[0]
        assert (method, path) == ("PATCH", "/issue/api/10")
        assert payload["add_labels"] == ["bug"]
        assert payload["remove_labels"] == []

    def test_gh_failure_exits_nonzero(self, config_file: Path) -> None:
        """A gh RuntimeError surfaces as exit code 1 with an error message."""
        with patch(
            "coord.github_ops.change_issue_labels",
            side_effect=RuntimeError("gh: label not found"),
        ):
            result = CliRunner().invoke(
                main,
                [
                    "issue", "label", "api", "10",
                    "--add", "nonexistent-label",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 1
        assert "error" in result.output.lower()


# ── lifecycle commands still work after _apply_label_change refactor ───────────


class TestLifecycleCommandsAfterRefactor:
    """coord ready / backlog still work after _apply_label_change was refactored
    to delegate to state.apply_issue_labels (#802)."""

    def test_coord_ready_sets_status_ready(self, config_file: Path) -> None:
        _seed_issue(labels=["coord"])
        with patch(
            "coord.github_ops.change_issue_labels",
            return_value=(["coord", "status:ready"], True),
        ) as mock_change:
            result = CliRunner().invoke(
                main,
                ["ready", "api", "10", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        assert "ready" in result.output.lower()
        # The seam received the right add/remove sets
        call_kwargs = mock_change.call_args.kwargs
        assert "status:ready" in call_kwargs["add"]
        assert "coord" in call_kwargs["add"], "coord ready must add the 'coord' label"
        assert "status:refining" in call_kwargs["remove"]

    def test_coord_ready_adds_coord_label_when_missing(self, config_file: Path) -> None:
        """#544: coord ready must add coord label even if issue doesn't have it."""
        _seed_issue(labels=["status:backlog"])
        with patch(
            "coord.github_ops.change_issue_labels",
            return_value=(["status:ready", "coord"], True),
        ) as mock_change:
            result = CliRunner().invoke(
                main,
                ["ready", "api", "10", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        assert "ready" in result.output.lower()
        # Verify both labels are in the add set
        call_kwargs = mock_change.call_args.kwargs
        assert call_kwargs["add"] == {"status:ready", "coord"}, (
            "coord ready must add both status:ready and coord labels"
        )

    def test_coord_backlog_reports_noop_when_already_backlog(
        self, config_file: Path
    ) -> None:
        """coord backlog echoes the no-op message when no status:* label is set."""
        _seed_issue(labels=["coord"])
        with patch(
            "coord.github_ops.change_issue_labels",
            return_value=(["coord"], False),  # changed=False → no-op
        ):
            result = CliRunner().invoke(
                main,
                ["backlog", "api", "10", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        # The no_op_message for backlog says "already in Backlog"
        assert "backlog" in result.output.lower()

    def test_coord_ready_gh_failure_exits_nonzero(self, config_file: Path) -> None:
        """gh failure in lifecycle command surfaces as exit 1."""
        with patch(
            "coord.github_ops.change_issue_labels",
            side_effect=RuntimeError("gh: api error"),
        ):
            result = CliRunner().invoke(
                main,
                ["ready", "api", "10", "--config", str(config_file)],
            )
        assert result.exit_code == 1
        assert "error" in result.output.lower()
