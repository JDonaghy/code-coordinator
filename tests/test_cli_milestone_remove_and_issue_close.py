"""Black-box tests for `coord milestone remove` + `coord issue close` (#1003).

Two small backend gaps identified by #1003 ("Plans panel: right-click CRUD
for milestones/epics") as prerequisites for the Plans-panel context menu's
"Remove issue from milestone…" and "Close / archive plan" actions:

- github_ops.unassign_issue_milestone: correct gh call shape (mirrors
  test_cli_milestone_assign.py's coverage of assign_issue_milestone)
- state.unassign_issue_milestone / state._unassign_issue_milestone_local:
  daemon routing + local gh call + cache clear
- state.close_issue / state._close_issue_local: daemon routing + local gh call
- daemon endpoints POST /issue-milestone-remove, POST /issue-close
- CLI `coord milestone remove`, `coord issue close`
"""

from __future__ import annotations

import json
import sqlite3 as _sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from starlette.testclient import TestClient

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


def _seed_issue(
    repo_name: str = "api",
    number: int = 42,
    milestone_number: int | None = 7,
    milestone_title: str | None = "v1.0",
) -> None:
    conn = state_mod.get_connection()
    conn.execute(
        """
        INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at,
                            milestone_number, milestone_title)
        VALUES (?, ?, 'Test issue', '', 'open', '[]', ?, ?, ?)
        ON CONFLICT (repo_name, number) DO NOTHING
        """,
        (repo_name, number, time.time(), milestone_number, milestone_title),
    )
    conn.commit()


# ── github_ops.unassign_issue_milestone ───────────────────────────────────────


class TestGithubOpsUnassignMilestone:
    def test_sends_patch_with_null_milestone(self) -> None:
        fake_result = MagicMock(returncode=0, stdout="{}", stderr="")
        with patch("subprocess.run", return_value=fake_result) as mock_run:
            from coord import github_ops

            github_ops.unassign_issue_milestone("acme/api", 42)

        args = mock_run.call_args[0][0]
        assert args[:5] == ["gh", "api", "-X", "PATCH", "repos/acme/api/issues/42"]
        assert "-F" in args
        assert "milestone=null" in args

    def test_raises_on_gh_failure(self) -> None:
        fake_result = MagicMock(returncode=1, stdout="", stderr="not found")
        with patch("subprocess.run", return_value=fake_result):
            from coord import github_ops

            with pytest.raises(RuntimeError, match="not found"):
                github_ops.unassign_issue_milestone("acme/api", 42)


# ── state.unassign_issue_milestone routing ────────────────────────────────────


class TestUnassignIssueMilestoneRouting:
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

        monkeypatch.setattr("coord.github_ops.unassign_issue_milestone", _boom)

        state.unassign_issue_milestone("api", 42, repo_github="acme/api")
        # #1946: was POST /issue-milestone-remove. An explicit null milestone
        # is what CLEARS it — omitting the key would mean "leave it alone".
        assert (captured["method"], captured["path"]) == ("PATCH", "/issue/api/42")
        assert "milestone" in captured["payload"]
        assert captured["payload"]["milestone"] is None
        assert captured["payload"]["repo_github"] == "acme/api"

    def test_local_path_calls_github_ops_and_clears_cache(
        self, coord_db, monkeypatch
    ) -> None:
        from coord import state

        _seed_issue(number=42, milestone_number=7, milestone_title="v1.0")
        calls: list = []
        monkeypatch.setattr(
            "coord.github_ops.unassign_issue_milestone",
            lambda repo, issue: calls.append((repo, issue)),
        )

        state.unassign_issue_milestone("api", 42, repo_github="acme/api")

        assert calls == [("acme/api", 42)]
        row = state_mod.get_connection().execute(
            "SELECT milestone_number, milestone_title FROM issues"
            " WHERE repo_name='api' AND number=42"
        ).fetchone()
        assert row is not None
        assert row["milestone_number"] is None
        assert row["milestone_title"] is None

    def test_local_path_no_issue_row_does_not_crash(
        self, coord_db, monkeypatch
    ) -> None:
        from coord import state

        monkeypatch.setattr(
            "coord.github_ops.unassign_issue_milestone", lambda *a: None
        )
        state.unassign_issue_milestone("api", 999, repo_github="acme/api")


# ── state.close_issue routing ─────────────────────────────────────────────────


class TestCloseIssueRouting:
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

        monkeypatch.setattr("coord.github_ops.close_issue", _boom)

        state.close_issue("api", 42, comment="done", repo_github="acme/api")
        # #1946: was POST /issue-close.
        assert (captured["method"], captured["path"]) == ("PATCH", "/issue/api/42")
        assert captured["payload"]["state"] == "closed"
        assert captured["payload"]["comment"] == "done"
        assert captured["payload"]["repo_github"] == "acme/api"

    def test_local_path_calls_github_ops(self, coord_db, monkeypatch) -> None:
        from coord import state

        calls: list = []
        monkeypatch.setattr(
            "coord.github_ops.close_issue",
            lambda repo, issue, comment=None, force=False: calls.append(
                (repo, issue, comment, force)
            ),
        )

        state.close_issue("api", 42, comment="wrapping up", repo_github="acme/api")

        assert calls == [("acme/api", 42, "wrapping up", False)]

    def test_local_path_passes_force_through(self, coord_db, monkeypatch) -> None:
        # #1196: --force threads all the way to github_ops.close_issue's
        # open-children guard, mirroring --force-merge.
        from coord import state

        calls: list = []
        monkeypatch.setattr(
            "coord.github_ops.close_issue",
            lambda repo, issue, comment=None, force=False: calls.append(
                (repo, issue, comment, force)
            ),
        )

        state.close_issue("api", 42, repo_github="acme/api", force=True)

        assert calls == [("acme/api", 42, None, True)]

    def test_daemon_409_becomes_open_children_error(self, coord_db, monkeypatch) -> None:
        # #1196: a guard refusal on the daemon side (HTTP 409) must surface
        # to the caller as the same IssueHasOpenChildrenError a local close
        # would raise, not a raw httpx error.
        import httpx
        from coord import client as cc
        from coord import state
        from coord.github_ops import IssueHasOpenChildrenError

        monkeypatch.setattr(
            cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
        )

        # #1946: the refusal now comes off PATCH /issue/{repo}/{n}. A 409 is
        # NOT a "this daemon predates #1944" signal (only 404/405 are), so it
        # must propagate rather than silently falling back to the RPC route.
        def _raise_409(svc, method, path, payload=None, **kw):
            request = httpx.Request(method, f"http://d:7435{path}")
            response = httpx.Response(
                409,
                json={"error": "open children", "detail": "refusing to close: open children #1039"},
                request=request,
            )
            raise httpx.HTTPStatusError("409", request=request, response=response)

        monkeypatch.setattr(cc, "request_resource", _raise_409)
        monkeypatch.setattr(
            cc, "post_record",
            lambda *a, **k: pytest.fail("a 409 must not fall back to the RPC route"),
        )

        with pytest.raises(IssueHasOpenChildrenError, match="open children #1039"):
            state.close_issue("api", 1041, repo_github="acme/api")


# ── daemon endpoints ───────────────────────────────────────────────────────────


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


@pytest.fixture
def rw_db(tmp_path: Path):
    from coord import db
    from coord.db import _ensure_schema

    conn = _sqlite3.connect(str(tmp_path / "rw.db"), check_same_thread=False)
    conn.row_factory = _sqlite3.Row
    _ensure_schema(conn)
    db.override_connection(conn)
    yield conn


def test_serve_issue_milestone_remove_clears_cache(
    file_db: Path, rw_db, monkeypatch, tmp_path: Path
) -> None:
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)

    rw_db.execute(
        """
        INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at,
                            milestone_number, milestone_title)
        VALUES ('api', 42, 'Test issue', '', 'open', '[]', ?, 7, 'v1.0')
        """,
        (time.time(),),
    )
    rw_db.commit()

    calls: list = []
    monkeypatch.setattr(
        "coord.github_ops.unassign_issue_milestone",
        lambda repo, issue: calls.append((repo, issue)),
    )
    app = build_app(SqliteStore(file_db), load_config(p))
    with TestClient(app) as cli:
        resp = cli.post(
            "/issue-milestone-remove",
            json={"repo_name": "api", "issue_number": 42, "repo_github": "acme/api"},
        )
    assert resp.status_code == 200, resp.json()
    assert resp.json() == {"updated": True}
    assert calls == [("acme/api", 42)]

    row = rw_db.execute(
        "SELECT milestone_number, milestone_title FROM issues"
        " WHERE repo_name='api' AND number=42"
    ).fetchone()
    assert row["milestone_number"] is None
    assert row["milestone_title"] is None


def test_serve_issue_milestone_remove_missing_field_400(
    file_db: Path, tmp_path: Path
) -> None:
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    app = build_app(SqliteStore(file_db), load_config(p))
    with TestClient(app) as cli:
        resp = cli.post("/issue-milestone-remove", json={"issue_number": 42})
    assert resp.status_code == 400


def test_serve_issue_milestone_remove_gh_failure_503(
    file_db: Path, tmp_path: Path
) -> None:
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    with patch(
        "coord.github_ops.unassign_issue_milestone",
        side_effect=RuntimeError("gh: not found"),
    ):
        app = build_app(SqliteStore(file_db), load_config(p))
        with TestClient(app) as cli:
            resp = cli.post(
                "/issue-milestone-remove",
                json={"repo_name": "api", "issue_number": 42},
            )
    assert resp.status_code == 503


def test_serve_issue_close_calls_github_ops(
    file_db: Path, tmp_path: Path
) -> None:
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    calls: list = []
    with patch(
        "coord.github_ops.close_issue",
        lambda repo, issue, comment=None, force=False: calls.append(
            (repo, issue, comment, force)
        ),
    ):
        app = build_app(SqliteStore(file_db), load_config(p))
        with TestClient(app) as cli:
            resp = cli.post(
                "/issue-close",
                json={
                    "repo_name": "api",
                    "issue_number": 42,
                    "comment": "wrapping up",
                    "repo_github": "acme/api",
                },
            )
    assert resp.status_code == 200, resp.json()
    assert resp.json() == {"updated": True}
    assert calls == [("acme/api", 42, "wrapping up", False)]


def test_serve_issue_close_missing_field_400(file_db: Path, tmp_path: Path) -> None:
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    app = build_app(SqliteStore(file_db), load_config(p))
    with TestClient(app) as cli:
        resp = cli.post("/issue-close", json={"repo_name": "api"})
    assert resp.status_code == 400


def test_serve_issue_close_open_children_409(file_db: Path, tmp_path: Path) -> None:
    # #1196: the daemon converts the open-children guard refusal into a 409
    # (distinct from a generic 503 tracker failure) so state.close_issue can
    # convert it back into IssueHasOpenChildrenError client-side.
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.github_ops import IssueHasOpenChildrenError
    from coord.serve_app import build_app

    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)

    def _boom(repo, issue, comment=None, force=False):
        raise IssueHasOpenChildrenError(f"refusing to close #{issue}: open children #1039")

    with patch("coord.github_ops.close_issue", _boom):
        app = build_app(SqliteStore(file_db), load_config(p))
        with TestClient(app) as cli:
            resp = cli.post(
                "/issue-close",
                json={"repo_name": "api", "issue_number": 1041, "repo_github": "acme/api"},
            )
    assert resp.status_code == 409, resp.json()
    assert "open children" in resp.json()["detail"]


def test_serve_issue_close_force_passthrough(file_db: Path, tmp_path: Path) -> None:
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    calls: list = []
    with patch(
        "coord.github_ops.close_issue",
        lambda repo, issue, comment=None, force=False: calls.append(
            (repo, issue, comment, force)
        ),
    ):
        app = build_app(SqliteStore(file_db), load_config(p))
        with TestClient(app) as cli:
            resp = cli.post(
                "/issue-close",
                json={
                    "repo_name": "api",
                    "issue_number": 1041,
                    "repo_github": "acme/api",
                    "force": True,
                },
            )
    assert resp.status_code == 200, resp.json()
    assert calls == [("acme/api", 1041, None, True)]


def test_serve_issue_close_gh_failure_503(file_db: Path, tmp_path: Path) -> None:
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    with patch(
        "coord.github_ops.close_issue",
        side_effect=RuntimeError("gh: not found"),
    ):
        app = build_app(SqliteStore(file_db), load_config(p))
        with TestClient(app) as cli:
            resp = cli.post(
                "/issue-close", json={"repo_name": "api", "issue_number": 42}
            )
    assert resp.status_code == 503


# ── CLI `coord milestone remove` ────────────────────────────────────────────


class TestMilestoneRemoveCli:
    def test_removes_and_echoes_summary(self, config_file: Path) -> None:
        _seed_issue(number=42)
        with patch("coord.github_ops.unassign_issue_milestone") as mock_unassign:
            result = CliRunner().invoke(
                main,
                ["milestone", "remove", "api", "42", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        assert "#42" in result.output
        mock_unassign.assert_called_once_with("acme/api", 42)

    def test_unknown_repo_exits_2(self, config_file: Path) -> None:
        result = CliRunner().invoke(
            main,
            ["milestone", "remove", "nope", "42", "--config", str(config_file)],
        )
        assert result.exit_code == 2

    def test_gh_failure_exits_1(self, config_file: Path) -> None:
        _seed_issue(number=42)
        with patch(
            "coord.github_ops.unassign_issue_milestone",
            side_effect=RuntimeError("gh: not found"),
        ):
            result = CliRunner().invoke(
                main,
                ["milestone", "remove", "api", "42", "--config", str(config_file)],
            )
        assert result.exit_code == 1
        assert "error" in result.output.lower()


# ── CLI `coord issue close` ─────────────────────────────────────────────────


class TestIssueCloseCli:
    def test_closes_and_echoes_summary(self, config_file: Path) -> None:
        with patch("coord.github_ops.close_issue") as mock_close:
            result = CliRunner().invoke(
                main,
                ["issue", "close", "api", "42", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        assert "#42" in result.output
        mock_close.assert_called_once_with("acme/api", 42, comment=None, force=False)

    def test_closes_with_comment(self, config_file: Path) -> None:
        with patch("coord.github_ops.close_issue") as mock_close:
            result = CliRunner().invoke(
                main,
                [
                    "issue", "close", "api", "42",
                    "--comment", "shipped",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        mock_close.assert_called_once_with("acme/api", 42, comment="shipped", force=False)

    def test_gh_failure_exits_1(self, config_file: Path) -> None:
        with patch(
            "coord.github_ops.close_issue",
            side_effect=RuntimeError("gh: not found"),
        ):
            result = CliRunner().invoke(
                main,
                ["issue", "close", "api", "42", "--config", str(config_file)],
            )
        assert result.exit_code == 1
        assert "error" in result.output.lower()

    def test_force_flag_passes_through(self, config_file: Path) -> None:
        # #1196: --force mirrors `coord merge --force-merge`.
        with patch("coord.github_ops.close_issue") as mock_close:
            result = CliRunner().invoke(
                main,
                ["issue", "close", "api", "1041", "--force", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        mock_close.assert_called_once_with("acme/api", 1041, comment=None, force=True)

    def test_open_children_refusal_surfaces_clearly(self, config_file: Path) -> None:
        # #1196: the chokepoint's refusal must exit non-zero with a message
        # naming the open children — this is the "CLI ... surfaces the
        # refusal clearly" acceptance bar, exercised through the real
        # github_ops.close_issue guard (only the `gh` subprocess boundary
        # is mocked), not a stand-in exception.
        def fake_gh(*args: str, **_kwargs) -> str:
            if args[:2] == ("issue", "view"):
                return (
                    '{"number": 1041, "body": "## Sub-issues\\n- [ ] #1039\\n", '
                    '"title": "Epic", "state": "open", "milestone": null, '
                    '"labels": []}'
                )
            if args[:2] == ("api", "graphql"):
                # #1354: the guard also does a live batch state lookup; no
                # live answer here, so it falls back to the checkbox above.
                raise RuntimeError("gh api graphql: not available in this test")
            raise AssertionError(f"unexpected gh call: {args}")

        with patch("coord.github_ops._gh", fake_gh):
            result = CliRunner().invoke(
                main,
                ["issue", "close", "api", "1041", "--config", str(config_file)],
            )
        assert result.exit_code == 1
        assert "open children" in result.output.lower()
        assert "#1039" in result.output

    def test_unknown_repo_errors_cleanly_without_reaching_gh(
        self, config_file: Path
    ) -> None:
        # #2655: an unresolvable local name must never fall through to
        # `close_issue` (and, through it, `gh`) verbatim.
        with patch("coord.github_ops.close_issue") as mock_close:
            result = CliRunner().invoke(
                main,
                ["issue", "close", "code-coordinator", "42", "--config", str(config_file)],
            )
        assert result.exit_code != 0
        assert "unknown repo 'code-coordinator'" in result.output
        assert "coordinator.yml" in result.output
        mock_close.assert_not_called()
