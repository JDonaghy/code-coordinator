"""Black-box tests for ``coord milestone assign`` (#967).

Coverage targets (mirroring test_milestone_seam.py / test_cli_issue_create_label.py):
- github_ops.assign_issue_milestone: correct gh call shape
- github_ops.get_repo_milestones: correct gh call + jq parsing
- github_ops.get_milestone: correct single-fetch call
- state.assign_issue_milestone: daemon routing when board_service is set
- state._assign_issue_milestone_local: gh call + local cache update
- daemon endpoint POST /issue-milestone: happy path + missing-field 400
- CLI coord milestone assign: by number + by title + error paths
"""

from __future__ import annotations

import inspect
import json
import re
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
    number: int = 10,
    labels: list[str] | None = None,
) -> None:
    """Insert a minimal issue row into the test DB."""
    conn = state_mod.get_connection()
    conn.execute(
        """
        INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at,
                            milestone_number, milestone_title)
        VALUES (?, ?, 'Test issue', '', 'open', ?, ?, NULL, NULL)
        ON CONFLICT (repo_name, number) DO NOTHING
        """,
        (repo_name, number, json.dumps(labels or []), time.time()),
    )
    conn.commit()


# ── github_ops.assign_issue_milestone ────────────────────────────────────────


class TestGithubOpsAssignMilestone:
    def test_assign_sends_patch_with_integer_milestone(self) -> None:
        """assign_issue_milestone must use -F (not -f) so the value is sent
        as a JSON integer, matching what the GitHub REST API requires."""
        fake_result = MagicMock(returncode=0, stdout="{}", stderr="")
        with patch("subprocess.run", return_value=fake_result) as mock_run:
            from coord import github_ops

            github_ops.assign_issue_milestone("acme/api", 42, 7)

        args = mock_run.call_args[0][0]
        assert args[:5] == ["gh", "api", "-X", "PATCH", "repos/acme/api/issues/42"]
        # -F sends as JSON integer; -f would send as string.
        assert "-F" in args
        assert "milestone=7" in args

    def test_assign_raises_on_gh_failure(self) -> None:
        fake_result = MagicMock(returncode=1, stdout="", stderr="not found")
        with patch("subprocess.run", return_value=fake_result):
            from coord import github_ops

            with pytest.raises(RuntimeError, match="not found"):
                github_ops.assign_issue_milestone("acme/api", 42, 7)


class TestGithubOpsGetRepoMilestones:
    def test_returns_parsed_milestones(self) -> None:
        """get_repo_milestones parses the --jq output (one JSON object per line)."""
        # --jq emits one compact object per line (newline-separated).
        jq_output = (
            '{"number": 3, "title": "v1.0"}\n'
            '{"number": 4, "title": "v2.0"}\n'
        )
        fake_result = MagicMock(returncode=0, stdout=jq_output, stderr="")
        with patch("subprocess.run", return_value=fake_result) as mock_run:
            from coord import github_ops

            results = github_ops.get_repo_milestones("acme/api")

        assert results == [
            {"number": 3, "title": "v1.0"},
            {"number": 4, "title": "v2.0"},
        ]
        args = mock_run.call_args[0][0]
        assert "repos/acme/api/milestones?state=open" in " ".join(args)
        assert "--jq" in args
        # Assert the exact filter string (not just presence of "--jq") so a
        # future edit that reintroduces invalid jq syntax is caught here even
        # without a live jq engine.
        jq_index = args.index("--jq")
        assert args[jq_index + 1] == ".[] | {number: .number, title: .title}"

    def test_jq_filter_is_valid_jq_syntax(self) -> None:
        """Regression test for the #967 review finding: `.[].{...}` (no pipe)
        is invalid jq syntax and made the title-lookup path fail end to end.
        Run the *actual* filter through a real jq engine to catch this class
        of bug even though the rest of this test class mocks subprocess.run.
        """
        jq = pytest.importorskip("jq")
        from coord import github_ops

        source = inspect.getsource(github_ops.get_repo_milestones)
        match = re.search(r'"--jq",\s*"([^"]+)"', source)
        assert match, "could not find --jq filter string in get_repo_milestones"
        filter_str = match.group(1)

        sample = [
            {"number": 3, "title": "v1.0", "state": "open"},
            {"number": 4, "title": "v2.0", "state": "open"},
        ]
        result = jq.compile(filter_str).input_value(sample).all()
        assert result == [
            {"number": 3, "title": "v1.0"},
            {"number": 4, "title": "v2.0"},
        ]

    def test_forwards_state_query_param(self) -> None:
        fake_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=fake_result) as mock_run:
            from coord import github_ops

            github_ops.get_repo_milestones("acme/api", state="all")

        args = mock_run.call_args[0][0]
        assert "repos/acme/api/milestones?state=all" in " ".join(args)

    def test_empty_repo_returns_empty_list(self) -> None:
        fake_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=fake_result):
            from coord import github_ops

            assert github_ops.get_repo_milestones("acme/api") == []


class TestGithubOpsGetMilestone:
    def test_fetches_single_milestone(self) -> None:
        payload = {"number": 5, "title": "v1.0", "description": "First release"}
        fake_result = MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")
        with patch("subprocess.run", return_value=fake_result) as mock_run:
            from coord import github_ops

            result = github_ops.get_milestone("acme/api", 5)

        assert result["number"] == 5
        assert result["title"] == "v1.0"
        args = mock_run.call_args[0][0]
        assert "repos/acme/api/milestones/5" in " ".join(args)

    def test_raises_on_missing_milestone(self) -> None:
        fake_result = MagicMock(returncode=1, stdout="", stderr="HTTP 404")
        with patch("subprocess.run", return_value=fake_result):
            from coord import github_ops

            with pytest.raises(RuntimeError):
                github_ops.get_milestone("acme/api", 999)


# ── state.assign_issue_milestone routing ─────────────────────────────────────


class TestAssignIssueMilestoneRouting:
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

        monkeypatch.setattr("coord.github_ops.assign_issue_milestone", _boom)

        state.assign_issue_milestone(
            "api", 42, 7, milestone_title="v1.0", repo_github="acme/api"
        )
        # #1946: was POST /issue-milestone {"milestone_number": 7}.
        assert (captured["method"], captured["path"]) == ("PATCH", "/issue/api/42")
        assert captured["payload"]["milestone"] == 7
        assert captured["payload"]["milestone_title"] == "v1.0"
        assert captured["payload"]["repo_github"] == "acme/api"

    def test_local_path_calls_github_ops_and_updates_cache(
        self, coord_db, monkeypatch
    ) -> None:
        from coord import state

        _seed_issue(number=42)
        calls: list = []
        monkeypatch.setattr(
            "coord.github_ops.assign_issue_milestone",
            lambda repo, issue, ms: calls.append((repo, issue, ms)),
        )

        state.assign_issue_milestone(
            "api", 42, 7, milestone_title="v1.0", repo_github="acme/api"
        )

        assert calls == [("acme/api", 42, 7)]
        row = state_mod.get_connection().execute(
            "SELECT milestone_number, milestone_title FROM issues"
            " WHERE repo_name='api' AND number=42"
        ).fetchone()
        assert row is not None
        assert row["milestone_number"] == 7
        assert row["milestone_title"] == "v1.0"

    def test_local_path_no_issue_row_does_not_crash(
        self, coord_db, monkeypatch
    ) -> None:
        """If the issue is not in the local cache, the UPDATE is a no-op
        (0 rows affected) but must not raise."""
        from coord import state

        monkeypatch.setattr(
            "coord.github_ops.assign_issue_milestone",
            lambda *a: None,
        )
        # Issue 999 is not in the DB — should not raise
        state.assign_issue_milestone("api", 999, 3, repo_github="acme/api")


# ── daemon endpoint POST /issue-milestone ─────────────────────────────────────


import sqlite3 as _sqlite3


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
    """Thread-safe file-backed coord.db override for TestClient tests.

    The autouse ``coord_db`` fixture installs a thread-bound ``:memory:`` conn;
    the Starlette TestClient runs async handlers in a worker thread and can't
    use it. This fixture mirrors the pattern in test_serve.py: a real file DB
    with ``check_same_thread=False`` so the ASGI handler and the test assertion
    can both access it safely.
    """
    from coord import db
    from coord.db import _ensure_schema

    conn = _sqlite3.connect(str(tmp_path / "rw.db"), check_same_thread=False)
    conn.row_factory = _sqlite3.Row
    _ensure_schema(conn)
    db.override_connection(conn)
    yield conn


def test_serve_issue_milestone_assigns_and_updates_cache(
    file_db: Path, rw_db, monkeypatch, tmp_path: Path
) -> None:
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)

    # Seed an issue row into the rw_db (the thread-safe DB that get_connection()
    # returns inside the Starlette handler).
    rw_db.execute(
        """
        INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at,
                            milestone_number, milestone_title)
        VALUES ('api', 42, 'Test issue', '', 'open', '[]', ?, NULL, NULL)
        """,
        (time.time(),),
    )
    rw_db.commit()

    calls: list = []
    monkeypatch.setattr(
        "coord.github_ops.assign_issue_milestone",
        lambda repo, issue, ms: calls.append((repo, issue, ms)),
    )
    app = build_app(SqliteStore(file_db), load_config(p))
    with TestClient(app) as cli:
        resp = cli.post(
            "/issue-milestone",
            json={
                "repo_name": "api",
                "issue_number": 42,
                "milestone_number": 7,
                "milestone_title": "v1.0",
                "repo_github": "acme/api",
            },
        )
    assert resp.status_code == 200, resp.json()
    assert resp.json() == {"updated": True}
    assert calls == [("acme/api", 42, 7)]

    # Verify the cache was updated in the rw_db (the same conn the handler used).
    row = rw_db.execute(
        "SELECT milestone_number, milestone_title FROM issues"
        " WHERE repo_name='api' AND number=42"
    ).fetchone()
    assert row is not None
    assert row["milestone_number"] == 7
    assert row["milestone_title"] == "v1.0"


def test_serve_issue_milestone_missing_repo_name_400(
    file_db: Path, tmp_path: Path
) -> None:
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    app = build_app(SqliteStore(file_db), load_config(p))
    with TestClient(app) as cli:
        resp = cli.post(
            "/issue-milestone",
            json={"issue_number": 42, "milestone_number": 7},
        )
    assert resp.status_code == 400


def test_serve_issue_milestone_missing_milestone_number_400(
    file_db: Path, tmp_path: Path
) -> None:
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    app = build_app(SqliteStore(file_db), load_config(p))
    with TestClient(app) as cli:
        resp = cli.post(
            "/issue-milestone",
            json={"repo_name": "api", "issue_number": 42},
        )
    assert resp.status_code == 400


def test_serve_issue_milestone_gh_failure_503(
    file_db: Path, rw_db, monkeypatch, tmp_path: Path
) -> None:
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    monkeypatch.setattr(
        "coord.github_ops.assign_issue_milestone",
        lambda *a: (_ for _ in ()).throw(RuntimeError("gh: not found")),
    )
    app = build_app(SqliteStore(file_db), load_config(p))
    with TestClient(app) as cli:
        resp = cli.post(
            "/issue-milestone",
            json={"repo_name": "api", "issue_number": 42, "milestone_number": 7},
        )
    assert resp.status_code == 503


# ── CLI coord milestone assign ────────────────────────────────────────────────


class TestMilestoneAssignCli:
    def test_assign_by_number_echoes_title_and_number(
        self, config_file: Path
    ) -> None:
        """coord milestone assign by number: resolves title, calls backend, prints summary."""
        _seed_issue(number=42)
        with patch(
            "coord.github_ops.get_milestone",
            return_value={"number": 7, "title": "v1.0"},
        ), patch(
            "coord.github_ops.assign_issue_milestone",
        ) as mock_assign:
            result = CliRunner().invoke(
                main,
                [
                    "milestone", "assign", "api", "42", "7",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        assert "#42" in result.output
        assert "v1.0" in result.output
        assert "7" in result.output
        mock_assign.assert_called_once_with("acme/api", 42, 7)

    def test_assign_by_title_resolves_number(
        self, config_file: Path
    ) -> None:
        """coord milestone assign by title: lists milestones, resolves number, calls backend."""
        _seed_issue(number=42)
        with patch(
            "coord.github_ops.get_repo_milestones",
            return_value=[
                {"number": 7, "title": "v1.0"},
                {"number": 8, "title": "v2.0"},
            ],
        ), patch(
            "coord.github_ops.assign_issue_milestone",
        ) as mock_assign:
            result = CliRunner().invoke(
                main,
                [
                    "milestone", "assign", "api", "42", "v1.0",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        assert "v1.0" in result.output
        assert "#7" in result.output or "7" in result.output
        mock_assign.assert_called_once_with("acme/api", 42, 7)

    def test_assign_by_title_not_found_exits_nonzero(
        self, config_file: Path
    ) -> None:
        """A title that does not match any open milestone → exit code 1."""
        with patch(
            "coord.github_ops.get_repo_milestones",
            return_value=[{"number": 7, "title": "v1.0"}],
        ):
            result = CliRunner().invoke(
                main,
                [
                    "milestone", "assign", "api", "42", "nonexistent",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 1
        assert "error" in result.output.lower()

    def test_assign_ambiguous_title_exits_nonzero(
        self, config_file: Path
    ) -> None:
        """Multiple milestones with the same title → error, user must use a number."""
        with patch(
            "coord.github_ops.get_repo_milestones",
            return_value=[
                {"number": 3, "title": "duplicate"},
                {"number": 4, "title": "duplicate"},
            ],
        ):
            result = CliRunner().invoke(
                main,
                [
                    "milestone", "assign", "api", "42", "duplicate",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 1
        assert "multiple" in result.output.lower() or "error" in result.output.lower()

    def test_assign_unknown_repo_exits_2(self, config_file: Path) -> None:
        """Unknown repo → usage error (exit 2)."""
        result = CliRunner().invoke(
            main,
            [
                "milestone", "assign", "nope", "42", "7",
                "--config", str(config_file),
            ],
        )
        assert result.exit_code == 2

    def test_assign_updates_local_cache(self, config_file: Path) -> None:
        """After assign, the local issues cache milestone_number/title is updated."""
        _seed_issue(number=77)
        with patch(
            "coord.github_ops.get_milestone",
            return_value={"number": 5, "title": "sprint-1"},
        ), patch("coord.github_ops.assign_issue_milestone"):
            result = CliRunner().invoke(
                main,
                [
                    "milestone", "assign", "api", "77", "5",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        row = state_mod.get_connection().execute(
            "SELECT milestone_number, milestone_title FROM issues"
            " WHERE repo_name='api' AND number=77"
        ).fetchone()
        assert row is not None
        assert row["milestone_number"] == 5
        assert row["milestone_title"] == "sprint-1"

    def test_assign_thin_client_posts_to_seam(self, config_file: Path) -> None:
        """On a thin client, assign routes to the daemon.

        #1946: via ``PATCH /issue/{repo}/{n}``, replacing ``POST
        /issue-milestone``.
        """
        fake_svc = MagicMock()
        fake_svc.url = "http://daemon:7435"
        fake_svc.token = None

        with patch("coord.state._board_service", return_value=fake_svc), patch(
            "coord.client.request_resource",
            return_value={"updated": True, "applied": ["milestone"]},
        ) as mock_req, patch(
            "coord.github_ops.get_milestone",
            return_value={"number": 7, "title": "v1.0"},
        ):
            result = CliRunner().invoke(
                main,
                [
                    "milestone", "assign", "api", "42", "7",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        mock_req.assert_called_once()
        _svc, method, path, payload = mock_req.call_args[0]
        assert (method, path) == ("PATCH", "/issue/api/42")
        assert payload["milestone"] == 7
        assert payload["milestone_title"] == "v1.0"

    def test_assign_gh_failure_exits_nonzero(self, config_file: Path) -> None:
        """A gh RuntimeError surfaces as exit code 1 with an error message."""
        with patch(
            "coord.github_ops.get_milestone",
            return_value={"number": 7, "title": "v1.0"},
        ), patch(
            "coord.github_ops.assign_issue_milestone",
            side_effect=RuntimeError("gh: network error"),
        ):
            result = CliRunner().invoke(
                main,
                [
                    "milestone", "assign", "api", "42", "7",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 1
        assert "error" in result.output.lower()

    def test_assign_milestone_number_fetch_failure_exits_nonzero(
        self, config_file: Path
    ) -> None:
        """If get_milestone fails (e.g. milestone doesn't exist), exit 1."""
        with patch(
            "coord.github_ops.get_milestone",
            side_effect=RuntimeError("HTTP 404"),
        ):
            result = CliRunner().invoke(
                main,
                [
                    "milestone", "assign", "api", "42", "999",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 1
        assert "error" in result.output.lower()
