"""Black-box tests for `coord issue reopen` (#1078).

Mirrors ``tests/test_cli_milestone_remove_and_issue_close.py``'s coverage of
`coord issue close` for the complement operation:

- state.reopen_issue / state._reopen_issue_local: daemon routing + local gh call
- daemon endpoint POST /issue-reopen
- CLI `coord issue reopen`

github_ops.reopen_issue itself (the `gh` call shape + idempotency-on-already-open
behavior) is unit-tested directly in tests/test_github_ops.py::TestReopenIssue.
"""

from __future__ import annotations

import sqlite3 as _sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from starlette.testclient import TestClient

from coord.cli import main
from tests.backends import set_board_meta


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


# ── state.reopen_issue routing ─────────────────────────────────────────────────


class TestReopenIssueRouting:
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
            ) or {"updated": True},
        )

        def _boom(*a, **k):
            raise AssertionError("backend write must run on the daemon, not the client")

        monkeypatch.setattr("coord.github_ops.reopen_issue", _boom)

        state.reopen_issue("api", 42, comment="my bad", repo_github="acme/api")
        # #1946: was POST /issue-reopen.
        assert (captured["method"], captured["path"]) == ("PATCH", "/issue/api/42")
        assert captured["payload"]["state"] == "open"
        assert captured["payload"]["comment"] == "my bad"
        assert captured["payload"]["repo_github"] == "acme/api"

    def test_local_path_calls_github_ops(self, coord_db, monkeypatch) -> None:
        from coord import state

        calls: list = []
        monkeypatch.setattr(
            "coord.github_ops.reopen_issue",
            lambda repo, issue, comment=None: calls.append((repo, issue, comment)),
        )

        state.reopen_issue("api", 42, comment="reopening", repo_github="acme/api")

        assert calls == [("acme/api", 42, "reopening")]

    def test_local_path_defaults_slug_to_repo_name(self, coord_db, monkeypatch) -> None:
        from coord import state

        calls: list = []
        monkeypatch.setattr(
            "coord.github_ops.reopen_issue",
            lambda repo, issue, comment=None: calls.append((repo, issue, comment)),
        )

        state.reopen_issue("acme/api", 42)

        assert calls == [("acme/api", 42, None)]


# ── daemon endpoint ──────────────────────────────────────────────────────────


def _make_file_db(path: Path) -> None:
    from coord.db import _ensure_schema

    conn = _sqlite3.connect(str(path))
    conn.row_factory = _sqlite3.Row
    _ensure_schema(conn)
    set_board_meta(conn, "round_number", "1")
    set_board_meta(conn, "board_initialized", "1")
    conn.commit()
    conn.close()


@pytest.fixture
def file_db(tmp_path: Path) -> Path:
    p = tmp_path / "coord.db"
    _make_file_db(p)
    return p


def test_serve_issue_reopen_calls_github_ops(file_db: Path, tmp_path: Path) -> None:
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    calls: list = []
    with patch(
        "coord.github_ops.reopen_issue",
        lambda repo, issue, comment=None: calls.append((repo, issue, comment)),
    ):
        app = build_app(SqliteStore(file_db), load_config(p))
        with TestClient(app) as cli:
            resp = cli.post(
                "/issue-reopen",
                json={
                    "repo_name": "api",
                    "issue_number": 42,
                    "comment": "reopening",
                    "repo_github": "acme/api",
                },
            )
    assert resp.status_code == 200, resp.json()
    assert resp.json() == {"updated": True}
    assert calls == [("acme/api", 42, "reopening")]


def test_serve_issue_reopen_missing_field_400(file_db: Path, tmp_path: Path) -> None:
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    app = build_app(SqliteStore(file_db), load_config(p))
    with TestClient(app) as cli:
        resp = cli.post("/issue-reopen", json={"repo_name": "api"})
    assert resp.status_code == 400


def test_serve_issue_reopen_gh_failure_503(file_db: Path, tmp_path: Path) -> None:
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    with patch(
        "coord.github_ops.reopen_issue",
        side_effect=RuntimeError("gh: not found"),
    ):
        app = build_app(SqliteStore(file_db), load_config(p))
        with TestClient(app) as cli:
            resp = cli.post(
                "/issue-reopen", json={"repo_name": "api", "issue_number": 42}
            )
    assert resp.status_code == 503


# ── CLI `coord issue reopen` ────────────────────────────────────────────────


class TestIssueReopenCli:
    def test_reopens_and_echoes_summary(self, config_file: Path) -> None:
        with patch("coord.state.reopen_issue") as mock_reopen:
            result = CliRunner().invoke(
                main,
                ["issue", "reopen", "api", "42", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        assert "#42" in result.output
        mock_reopen.assert_called_once_with(
            "api", 42, comment=None, repo_github="acme/api"
        )

    def test_reopens_with_comment(self, config_file: Path) -> None:
        with patch("coord.state.reopen_issue") as mock_reopen:
            result = CliRunner().invoke(
                main,
                [
                    "issue", "reopen", "api", "42",
                    "--comment", "reopening, closed by mistake",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        mock_reopen.assert_called_once_with(
            "api", 42, comment="reopening, closed by mistake", repo_github="acme/api"
        )

    def test_gh_failure_exits_1(self, config_file: Path) -> None:
        with patch(
            "coord.state.reopen_issue",
            side_effect=RuntimeError("gh: not found"),
        ):
            result = CliRunner().invoke(
                main,
                ["issue", "reopen", "api", "42", "--config", str(config_file)],
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
        with patch("coord.state.reopen_issue") as mock_reopen:
            result = CliRunner().invoke(
                main,
                ["issue", "reopen", "nope", "42", "--config", str(config_file)],
            )
        assert result.exit_code != 0
        assert "unknown repo 'nope'" in result.output
        assert "coordinator.yml" in result.output
        mock_reopen.assert_not_called()

    def test_unknown_repo_raw_slug_still_falls_through(
        self, config_file: Path
    ) -> None:
        # The deliberate escape hatch: a value that already looks like an
        # OWNER/REPO slug (contains '/') is accepted as-is even though it's
        # not a coordinator.yml-local name.
        with patch("coord.state.reopen_issue") as mock_reopen:
            result = CliRunner().invoke(
                main,
                ["issue", "reopen", "someone/nope", "42", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        mock_reopen.assert_called_once_with(
            "someone/nope", 42, comment=None, repo_github="someone/nope"
        )
