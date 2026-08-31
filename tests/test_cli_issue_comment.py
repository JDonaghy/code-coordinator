"""Black-box tests for `coord issue comment` (#2643).

Mirrors ``tests/test_cli_issue_reopen.py``'s coverage of `coord issue reopen`
for the state-free comment write:

- state.comment_on_issue / state._comment_on_issue_local: daemon routing +
  local gh call
- daemon endpoint POST /issue-comment
- CLI `coord issue comment`

Two behaviours this issue calls out as easy to get wrong, both pinned here
against a stubbed forge (only the ``gh`` subprocess boundary is mocked, so
the real ``github_ops.post_issue_comment`` -> capture-at-write path runs):

1. The comment lands in the durable ``issue_comments`` mirror, not just on
   the (stubbed) forge — asserted via a subsequent ``list_issue_comments()``.
2. The issue's state is untouched — open stays open, closed stays closed.
"""

from __future__ import annotations

import sqlite3 as _sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from starlette.testclient import TestClient

from coord.cli import main

# ── shared config ────────────────────────────────────────────────────────


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


# ── state.comment_on_issue routing ──────────────────────────────────────────


class TestCommentOnIssueRouting:
    def test_routes_to_daemon_when_service_set(self, coord_db, monkeypatch) -> None:
        from coord import client as cc
        from coord import state

        monkeypatch.setattr(
            cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
        )
        captured: dict = {}
        monkeypatch.setattr(
            cc,
            "request_resource",
            lambda svc, method, path, payload=None, **kw: captured.update(
                method=method, path=path, payload=payload
            ) or {"ok": True, "action": "post"},
        )

        def _boom(*a, **k):
            raise AssertionError("backend write must run on the daemon, not the client")

        monkeypatch.setattr("coord.github_ops.post_issue_comment", _boom)

        state.comment_on_issue("api", 42, "correction: X was wrong", repo_github="acme/api")
        # #1946: was POST /issue-comment.
        assert (captured["method"], captured["path"]) == (
            "POST", "/issue/api/42/comments",
        )
        assert captured["payload"]["action"] == "post"
        assert captured["payload"]["body"] == "correction: X was wrong"
        assert captured["payload"]["repo_github"] == "acme/api"

    def test_local_path_calls_github_ops(self, coord_db, monkeypatch) -> None:
        from coord import state

        calls: list = []
        monkeypatch.setattr(
            "coord.github_ops.post_issue_comment",
            lambda repo, issue, body: calls.append((repo, issue, body)),
        )

        state.comment_on_issue("api", 42, "hello", repo_github="acme/api")

        assert calls == [("acme/api", 42, "hello")]

    def test_local_path_defaults_slug_to_repo_name(self, coord_db, monkeypatch) -> None:
        from coord import state

        calls: list = []
        monkeypatch.setattr(
            "coord.github_ops.post_issue_comment",
            lambda repo, issue, body: calls.append((repo, issue, body)),
        )

        state.comment_on_issue("acme/api", 42, "hello")

        assert calls == [("acme/api", 42, "hello")]


# ── daemon endpoint ──────────────────────────────────────────────────────────


def _make_file_db(path: Path) -> None:
    from coord.db import _ensure_schema

    conn = _sqlite3.connect(str(path))
    conn.row_factory = _sqlite3.Row
    _ensure_schema(conn)
    conn.execute(
        "INSERT OR REPLACE INTO board_meta (key, value) VALUES ('round_number', '1')"
    )
    conn.execute(
        "INSERT OR REPLACE INTO board_meta (key, value) VALUES ('board_initialized', '1')"
    )
    conn.commit()
    conn.close()


@pytest.fixture
def file_db(tmp_path: Path) -> Path:
    p = tmp_path / "coord.db"
    _make_file_db(p)
    return p


def test_serve_issue_comment_calls_github_ops(file_db: Path, tmp_path: Path) -> None:
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    calls: list = []
    with patch(
        "coord.github_ops.post_issue_comment",
        lambda repo, issue, body: calls.append((repo, issue, body)),
    ):
        app = build_app(SqliteStore(file_db), load_config(p))
        with TestClient(app) as cli:
            resp = cli.post(
                "/issue-comment",
                json={
                    "repo_name": "api",
                    "issue_number": 42,
                    "body": "posting an update",
                    "repo_github": "acme/api",
                },
            )
    assert resp.status_code == 200, resp.json()
    assert resp.json() == {"updated": True}
    assert calls == [("acme/api", 42, "posting an update")]


def test_serve_issue_comment_missing_field_400(file_db: Path, tmp_path: Path) -> None:
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    app = build_app(SqliteStore(file_db), load_config(p))
    with TestClient(app) as cli:
        resp = cli.post("/issue-comment", json={"repo_name": "api", "issue_number": 42})
    assert resp.status_code == 400


def test_serve_issue_comment_gh_failure_503(file_db: Path, tmp_path: Path) -> None:
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    with patch(
        "coord.github_ops.post_issue_comment",
        side_effect=RuntimeError("gh: not found"),
    ):
        app = build_app(SqliteStore(file_db), load_config(p))
        with TestClient(app) as cli:
            resp = cli.post(
                "/issue-comment",
                json={"repo_name": "api", "issue_number": 42, "body": "x"},
            )
    assert resp.status_code == 503


# ── CLI `coord issue comment` ───────────────────────────────────────────────


class TestIssueCommentCli:
    def test_comments_and_echoes_summary(self, config_file: Path) -> None:
        with patch("coord.state.comment_on_issue") as mock_comment:
            result = CliRunner().invoke(
                main,
                [
                    "issue", "comment", "api", "42",
                    "--body", "correction: the earlier claim was wrong",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        assert "#42" in result.output
        mock_comment.assert_called_once_with(
            "api", 42, "correction: the earlier claim was wrong", repo_github="acme/api"
        )

    def test_body_file_reads_from_file(self, config_file: Path, tmp_path: Path) -> None:
        body_path = tmp_path / "body.md"
        body_path.write_text("a long markdown correction\n\nwith paragraphs")
        with patch("coord.state.comment_on_issue") as mock_comment:
            result = CliRunner().invoke(
                main,
                [
                    "issue", "comment", "api", "42",
                    "--body-file", str(body_path),
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        mock_comment.assert_called_once_with(
            "api", 42, "a long markdown correction\n\nwith paragraphs", repo_github="acme/api"
        )

    def test_body_file_dash_reads_stdin(self, config_file: Path) -> None:
        with patch("coord.state.comment_on_issue") as mock_comment:
            result = CliRunner().invoke(
                main,
                [
                    "issue", "comment", "api", "42",
                    "--body-file", "-",
                    "--config", str(config_file),
                ],
                input="from stdin",
            )
        assert result.exit_code == 0, result.output
        mock_comment.assert_called_once_with(
            "api", 42, "from stdin", repo_github="acme/api"
        )

    def test_body_and_body_file_mutually_exclusive(self, config_file: Path) -> None:
        with patch("coord.state.comment_on_issue") as mock_comment:
            result = CliRunner().invoke(
                main,
                [
                    "issue", "comment", "api", "42",
                    "--body", "x", "--body-file", "-",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 2
        mock_comment.assert_not_called()

    def test_missing_body_errors_cleanly(self, config_file: Path) -> None:
        with patch("coord.state.comment_on_issue") as mock_comment:
            result = CliRunner().invoke(
                main,
                ["issue", "comment", "api", "42", "--config", str(config_file)],
            )
        assert result.exit_code == 2
        mock_comment.assert_not_called()

    def test_gh_failure_exits_1(self, config_file: Path) -> None:
        with patch(
            "coord.state.comment_on_issue",
            side_effect=RuntimeError("gh: not found"),
        ):
            result = CliRunner().invoke(
                main,
                [
                    "issue", "comment", "api", "42", "--body", "x",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 1
        assert "error" in result.output.lower()

    def test_unknown_repo_name_errors_cleanly_without_reaching_gh(
        self, config_file: Path
    ) -> None:
        # #2655: an unrecognized REPO that doesn't look like an OWNER/REPO
        # slug must fail with the clean seam-level error (naming the bad
        # input + coordinator.yml) instead of falling through to `gh`
        # verbatim and leaking a raw backend error.
        with patch("coord.state.comment_on_issue") as mock_comment:
            result = CliRunner().invoke(
                main,
                ["issue", "comment", "nope", "42", "--body", "x", "--config", str(config_file)],
            )
        assert result.exit_code != 0
        assert "unknown repo 'nope'" in result.output
        mock_comment.assert_not_called()


# ── black-box: mirror capture + state-untouched (issue #2643's two pins) ────


class _FakeCompletedProcess:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestCommentLandsInMirrorAndLeavesStateAlone:
    """Drives `coord issue comment` end-to-end against a stubbed forge (only
    the ``gh`` subprocess boundary is mocked) so the real capture-at-write
    path (#873) runs, then asserts on the durable mirror and on the local
    issues cache — the two behaviours the issue calls out as easy to get
    wrong."""

    def _stub_gh(self, comment_url: str):
        def _dispatch(*args: str, **_kwargs) -> str:
            if args[:2] == ("issue", "comment"):
                return comment_url
            if args[:2] == ("api", "user"):
                return "bot-login"
            raise AssertionError(f"unexpected gh call: {args}")

        return _dispatch

    def test_comment_on_open_issue_lands_in_mirror_and_stays_open(
        self, coord_db, config_file: Path
    ) -> None:
        from coord.state import list_issue_comments

        coord_db.execute(
            "INSERT INTO issues (repo_name, number, title, state) "
            "VALUES ('api', 42, 'Some open issue', 'open')"
        )
        coord_db.commit()

        with patch(
            "coord.github_ops._gh",
            self._stub_gh("https://github.com/acme/api/issues/42#issuecomment-555"),
        ):
            result = CliRunner().invoke(
                main,
                [
                    "issue", "comment", "api", "42",
                    "--body", "correction: retracting the earlier claim",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output

        # Pin 1: lands in the durable mirror, keyed under the GitHub slug
        # `github_ops.post_issue_comment` was actually called with (mirrors
        # the existing close_issue --comment capture convention).
        comments = list_issue_comments("acme/api", 42)
        assert len(comments) == 1
        assert comments[0]["body"] == "correction: retracting the earlier claim"

        # Pin 2: state-free — the cached issue row is untouched.
        row = coord_db.execute(
            "SELECT state FROM issues WHERE repo_name='api' AND number=42"
        ).fetchone()
        assert row["state"] == "open"

    def test_comment_on_closed_issue_stays_closed(
        self, coord_db, config_file: Path
    ) -> None:
        from coord.state import list_issue_comments

        coord_db.execute(
            "INSERT INTO issues (repo_name, number, title, state) "
            "VALUES ('api', 99, 'Some closed issue', 'closed')"
        )
        coord_db.commit()

        with patch(
            "coord.github_ops._gh",
            self._stub_gh("https://github.com/acme/api/issues/99#issuecomment-556"),
        ):
            result = CliRunner().invoke(
                main,
                [
                    "issue", "comment", "api", "99",
                    "--body", "one more note on this closed epic",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output

        comments = list_issue_comments("acme/api", 99)
        assert len(comments) == 1
        assert comments[0]["body"] == "one more note on this closed epic"

        row = coord_db.execute(
            "SELECT state FROM issues WHERE repo_name='api' AND number=99"
        ).fetchone()
        assert row["state"] == "closed"
