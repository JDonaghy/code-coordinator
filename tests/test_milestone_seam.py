"""Black-box tests for the milestone write seam (#645 task 1):
`github_ops.create_milestone`/`edit_milestone`, `state.write_milestone`
routing, the daemon `POST /milestone-edit` endpoint, and the
`coord milestone create`/`edit` CLI.

Mirrors the #628/#802 issue-edit seam tests (tests/test_serve.py,
tests/test_cli_issue_create_label.py) — same shape, milestone flavour.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from starlette.testclient import TestClient

from coord.cli import main
from coord.config import load as load_config
from coord.dao import SqliteStore
from coord.serve_app import build_app
from tests.backends import set_board_meta


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


# ── github_ops ──────────────────────────────────────────────────────────────


class TestGithubOpsMilestone:
    def test_create_milestone_posts_expected_fields(self) -> None:
        fake_result = MagicMock(
            returncode=0,
            stdout=json.dumps({"number": 3, "title": "v1", "description": "goal"}),
            stderr="",
        )
        with patch("subprocess.run", return_value=fake_result) as mock_run:
            from coord import github_ops

            result = github_ops.create_milestone(
                "acme/api", "v1", description="goal", due_on="2026-08-01T00:00:00Z"
            )
        assert result == {"number": 3, "title": "v1", "description": "goal"}
        args = mock_run.call_args[0][0]
        assert args[:3] == ["gh", "api", "repos/acme/api/milestones"]
        assert "-f" in args and "title=v1" in args
        assert "description=goal" in args
        assert "due_on=2026-08-01T00:00:00Z" in args

    def test_edit_milestone_uses_patch(self) -> None:
        fake_result = MagicMock(
            returncode=0, stdout=json.dumps({"number": 3, "title": "v1.1"}), stderr=""
        )
        with patch("subprocess.run", return_value=fake_result) as mock_run:
            from coord import github_ops

            result = github_ops.edit_milestone("acme/api", 3, title="v1.1")
        assert result == {"number": 3, "title": "v1.1"}
        args = mock_run.call_args[0][0]
        assert args[:5] == ["gh", "api", "-X", "PATCH", "repos/acme/api/milestones/3"]
        assert "title=v1.1" in args

    def test_edit_milestone_noop_skips_gh_call(self) -> None:
        with patch("subprocess.run") as mock_run:
            from coord import github_ops

            assert github_ops.edit_milestone("acme/api", 3) == {}
        mock_run.assert_not_called()

    def test_create_milestone_raises_on_gh_failure(self) -> None:
        fake_result = MagicMock(returncode=1, stdout="", stderr="boom")
        with patch("subprocess.run", return_value=fake_result):
            from coord import github_ops

            with pytest.raises(RuntimeError, match="boom"):
                github_ops.create_milestone("acme/api", "v1")


# ── state.write_milestone routing ──────────────────────────────────────────


class TestWriteMilestoneRouting:
    def test_write_milestone_routes_to_daemon_when_service_set(
        self, coord_db, monkeypatch
    ) -> None:
        from coord import client as cc
        from coord import state

        monkeypatch.setattr(
            cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
        )
        captured: dict = {}
        monkeypatch.setattr(
            cc,
            "post_record",
            lambda svc, path, payload, **kw: captured.update(path=path, payload=payload)
            or {"number": 5, "title": "v2"},
        )

        def _boom(*a, **k):
            raise AssertionError("backend write must run on the daemon, not the client")

        monkeypatch.setattr("coord.github_ops.create_milestone", _boom)
        monkeypatch.setattr("coord.github_ops.edit_milestone", _boom)

        result = state.write_milestone("api", title="v2", repo_github="owner/api")
        assert result == {"number": 5, "title": "v2"}
        assert captured["path"] == "/milestone-edit"
        assert captured["payload"]["number"] is None
        assert captured["payload"]["title"] == "v2"
        assert captured["payload"]["repo_github"] == "owner/api"

    def test_write_milestone_local_create_calls_github_ops(self, coord_db, monkeypatch) -> None:
        from coord import state

        calls: list = []
        monkeypatch.setattr(
            "coord.github_ops.create_milestone",
            lambda repo, title, *, description=None, due_on=None: calls.append(
                (repo, title, description, due_on)
            )
            or {"number": 9, "title": title},
        )
        result = state.write_milestone(
            "api", title="v3", description="d", repo_github="owner/api"
        )
        assert result == {"number": 9, "title": "v3"}
        assert calls == [("owner/api", "v3", "d", None)]

    def test_write_milestone_local_edit_calls_github_ops(self, coord_db, monkeypatch) -> None:
        from coord import state

        calls: list = []
        monkeypatch.setattr(
            "coord.github_ops.edit_milestone",
            lambda repo, number, *, title=None, description=None, due_on=None: calls.append(
                (repo, number, title, description, due_on)
            )
            or {"number": number, "title": title},
        )
        result = state.write_milestone(
            "api", number=9, title="v3.1", repo_github="owner/api"
        )
        assert result == {"number": 9, "title": "v3.1"}
        assert calls == [("owner/api", 9, "v3.1", None, None)]

    def test_write_milestone_local_create_without_title_raises(self, coord_db) -> None:
        from coord import state

        with pytest.raises(ValueError, match="title"):
            state.write_milestone("api", repo_github="owner/api")


# ── daemon endpoint ─────────────────────────────────────────────────────────


def _make_file_db(path: Path) -> None:
    import sqlite3

    from coord.db import _ensure_schema

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
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


def test_serve_milestone_edit_creates_via_backend(
    file_db: Path, monkeypatch, tmp_path: Path
) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    calls: list = []
    monkeypatch.setattr(
        "coord.github_ops.create_milestone",
        lambda repo, title, *, description=None, due_on=None: calls.append(
            (repo, title, description, due_on)
        )
        or {"number": 11, "title": title, "description": description},
    )
    app = build_app(SqliteStore(file_db), load_config(p))
    with TestClient(app) as cli:
        resp = cli.post(
            "/milestone-edit",
            json={
                "repo_name": "api",
                "title": "v4",
                "description": "goal",
                "repo_github": "owner/api",
            },
        )
    assert resp.status_code == 200
    assert resp.json() == {"number": 11, "title": "v4", "description": "goal"}
    assert calls == [("owner/api", "v4", "goal", None)]


def test_serve_milestone_edit_edits_via_backend(
    file_db: Path, monkeypatch, tmp_path: Path
) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    calls: list = []
    monkeypatch.setattr(
        "coord.github_ops.edit_milestone",
        lambda repo, number, *, title=None, description=None, due_on=None: calls.append(
            (repo, number, title, description, due_on)
        )
        or {"number": number, "title": title},
    )
    app = build_app(SqliteStore(file_db), load_config(p))
    with TestClient(app) as cli:
        resp = cli.post(
            "/milestone-edit",
            json={
                "repo_name": "api",
                "number": 11,
                "title": "v4.1",
                "repo_github": "owner/api",
            },
        )
    assert resp.status_code == 200
    assert resp.json() == {"number": 11, "title": "v4.1"}
    assert calls == [("owner/api", 11, "v4.1", None, None)]


def test_serve_milestone_edit_create_without_title_400(
    file_db: Path, tmp_path: Path
) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    app = build_app(SqliteStore(file_db), load_config(p))
    with TestClient(app) as cli:
        resp = cli.post("/milestone-edit", json={"repo_name": "api"})
    assert resp.status_code == 400


def test_serve_milestone_edit_missing_repo_name_400(file_db: Path, tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    app = build_app(SqliteStore(file_db), load_config(p))
    with TestClient(app) as cli:
        resp = cli.post("/milestone-edit", json={"title": "v5"})
    assert resp.status_code == 400


# ── CLI ─────────────────────────────────────────────────────────────────────


class TestMilestoneCreateCli:
    def test_create_echoes_milestone_number(self, config_file: Path) -> None:
        with patch(
            "coord.github_ops.create_milestone",
            return_value={"number": 4, "title": "v1"},
        ) as mock_create:
            result = CliRunner().invoke(
                main,
                [
                    "milestone", "create", "api",
                    "--title", "v1",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        assert "#4" in result.output
        mock_create.assert_called_once_with(
            "acme/api", "v1", description=None, due_on=None
        )

    def test_create_thin_client_posts_to_seam(self, config_file: Path) -> None:
        fake_svc = MagicMock()
        fake_svc.url = "http://daemon:7435"
        fake_svc.token = None
        with patch("coord.state._board_service", return_value=fake_svc), patch(
            "coord.client.post_record",
            return_value={"number": 12, "title": "v9"},
        ) as mock_post:
            result = CliRunner().invoke(
                main,
                [
                    "milestone", "create", "api",
                    "--title", "v9",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        assert "#12" in result.output
        _svc, endpoint, payload = mock_post.call_args[0]
        assert endpoint == "/milestone-edit"
        assert payload["title"] == "v9"
        assert payload["number"] is None

    def test_create_requires_title(self, config_file: Path) -> None:
        result = CliRunner().invoke(
            main, ["milestone", "create", "api", "--config", str(config_file)]
        )
        assert result.exit_code != 0

    def test_create_unknown_repo_errors(self, config_file: Path) -> None:
        result = CliRunner().invoke(
            main,
            [
                "milestone", "create", "nope",
                "--title", "v1",
                "--config", str(config_file),
            ],
        )
        assert result.exit_code == 2


class TestMilestoneEditCli:
    def test_edit_calls_backend(self, config_file: Path) -> None:
        with patch(
            "coord.github_ops.edit_milestone",
            return_value={"number": 4, "title": "v1.1"},
        ) as mock_edit:
            result = CliRunner().invoke(
                main,
                [
                    "milestone", "edit", "api", "4",
                    "--title", "v1.1",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        assert "#4" in result.output
        mock_edit.assert_called_once_with(
            "acme/api", 4, title="v1.1", description=None, due_on=None
        )

    def test_edit_requires_a_field(self, config_file: Path) -> None:
        result = CliRunner().invoke(
            main, ["milestone", "edit", "api", "4", "--config", str(config_file)]
        )
        assert result.exit_code == 2

    def test_edit_unknown_repo_errors(self, config_file: Path) -> None:
        result = CliRunner().invoke(
            main,
            [
                "milestone", "edit", "nope", "4",
                "--title", "x",
                "--config", str(config_file),
            ],
        )
        assert result.exit_code == 2
