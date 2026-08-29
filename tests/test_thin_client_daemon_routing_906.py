"""#906 regression guard: dynamic/behavioral coverage for the daemon-routing
runtime code added by the #906 fix (the static AST audit in
``test_thin_client_board_audit.py`` only checks *which* functions call the
local board — it does not exercise the daemon-routing/fail-open behavior
itself). Adds tests the #906 review flagged as missing:

- ``coord.state.get_issue_test_mode`` daemon routing + fail-open fallback
  (the blocking fix: it must route to the daemon, not read the empty local
  DB, when a thin client's ``coord resume`` -> ``reconcile()`` calls it).
- ``update_assignment_claude_session_id`` / ``get_test_plan`` /
  ``set_assignment_failure_reason`` daemon-routing paths (existing tests only
  covered the local-DB path).
- The new ``serve_app.py`` daemon endpoints: ``/assignment-session-id``,
  ``/assignment-failure-reason``, ``/assignment-test-plan``,
  ``/issue-test-mode``.
- ``chat_continue``'s board-based prior-assignment lookup + the
  thin-client-gated local ``claude_session_id`` read.
- The ``board.active``-based ``in_flight`` peer-conflict rebuild in
  ``_dispatch_followup`` (``coord/commands/plan_followup.py``) and
  ``_dispatch_headless`` (``coord/commands/dispatch_workers.py``).

Test structure mirrors ``tests/test_review_verdict_relay.py`` (the #905
sibling fix): monkeypatch ``coord.client.resolve_board_service`` /
``fetch_board_payload`` / ``post_record`` to simulate a thin client talking to
a fake daemon, with an empty local DB proving there is no local fallthrough.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import coord.client as cc
from coord.config import Config, load as load_config
from coord.dao import SqliteStore
from coord.db import _ensure_schema, get_connection
from coord.models import Machine, Repo
from coord.serve_app import build_app


class _FakeSvc:
    url = "http://daemon:7435"
    token = "t"


# ══════════════════════════════════════════════════════════════════════════
# get_issue_test_mode: daemon routing + fail-open fallback
# ══════════════════════════════════════════════════════════════════════════


def test_get_issue_test_mode_routes_to_daemon(monkeypatch, coord_db) -> None:
    """Thin-client mode: reads the daemon's issue row, NOT the (empty) local
    `issues` table.

    #1946: the read is now ``GET /issue/{repo}/{n}`` + the shared
    ``test_mode_from_labels`` derivation, replacing ``POST /issue-test-mode``.
    """
    from coord.state import get_issue_test_mode

    assert get_connection().execute("SELECT COUNT(*) FROM issues").fetchone()[0] == 0

    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: _FakeSvc())
    captured: dict = {}

    def _fake_fetch_issue(svc, repo_name, number, **kw):
        captured.update(repo_name=repo_name, number=number)
        return {"repo_name": repo_name, "number": number, "labels": ["test-mode:smoke"]}

    monkeypatch.setattr(cc, "fetch_issue", _fake_fetch_issue)

    result = get_issue_test_mode("api", 287)

    assert result == "smoke"
    assert (captured["repo_name"], captured["number"]) == ("api", 287)


def test_get_issue_test_mode_falls_back_to_local_on_daemon_error(monkeypatch, coord_db) -> None:
    """If the daemon read raises, fail open to the local DB (best-effort)."""
    import json

    from coord.state import get_issue_test_mode

    conn = get_connection()
    conn.execute(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("api", 287, "t", "", "open", json.dumps(["test-mode:auto"]), 1.0),
    )
    conn.commit()

    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: _FakeSvc())
    monkeypatch.setattr(
        cc,
        "fetch_issue",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("daemon down")),
    )

    assert get_issue_test_mode("api", 287) == "auto"


def test_get_issue_test_mode_writes_local_when_no_service(coord_db) -> None:
    """Daemon host (board_service unset by autouse fixture): reads the local DB."""
    import json

    from coord.state import get_issue_test_mode

    conn = get_connection()
    conn.execute(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("api", 5, "t", "", "open", json.dumps(["test-mode:smoke"]), 1.0),
    )
    conn.commit()

    assert get_issue_test_mode("api", 5) == "smoke"
    assert get_issue_test_mode("api", 999) is None


def test_get_issue_test_mode_ignores_garbage_daemon_value(monkeypatch, coord_db) -> None:
    """A daemon row whose labels name no test-mode policy yields None.

    #1946: the daemon no longer hands back a `test_mode` string to trust or
    distrust — it hands back the labels and `test_mode_from_labels` derives
    the policy, so an unrecognised label simply is not a policy.
    """
    from coord.state import get_issue_test_mode

    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: _FakeSvc())
    monkeypatch.setattr(
        cc, "fetch_issue", lambda *a, **k: {"labels": ["test-mode:garbage"]}
    )

    assert get_issue_test_mode("api", 1) is None

    # ...and a row with no labels key at all is also not a policy.
    monkeypatch.setattr(cc, "fetch_issue", lambda *a, **k: {})
    assert get_issue_test_mode("api", 1) is None


def test_get_issue_test_mode_decodes_json_string_labels(monkeypatch, coord_db) -> None:
    """A daemon serving `labels` as raw JSON TEXT still yields the policy.

    #1946: handed a *string*, `test_mode_from_labels` iterates it
    characterwise and answers None — indistinguishable from "no label set",
    which is exactly the silent auto-dispatch #906 exists to prevent. So the
    shape is normalised before the derivation rather than trusted.
    """
    import json

    from coord.state import get_issue_test_mode

    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: _FakeSvc())
    monkeypatch.setattr(
        cc, "fetch_issue",
        lambda *a, **k: {"labels": json.dumps(["coord", "test-mode:smoke"])},
    )

    assert get_issue_test_mode("api", 287) == "smoke"


# ══════════════════════════════════════════════════════════════════════════
# update_assignment_claude_session_id / get_test_plan /
# set_assignment_failure_reason: daemon-routing paths
# ══════════════════════════════════════════════════════════════════════════


def test_update_claude_session_id_routes_to_daemon(monkeypatch, coord_db) -> None:
    from coord.state import update_assignment_claude_session_id

    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: _FakeSvc())
    captured: dict = {}
    monkeypatch.setattr(
        cc,
        "request_resource",
        lambda svc, method, path, payload=None, **kw: captured.update(
            method=method, path=path, payload=payload
        ) or {"updated": True},
    )

    update_assignment_claude_session_id("assign-1", "ses-abc")

    # #1946: was POST /assignment-session-id.
    assert (captured["method"], captured["path"]) == ("PATCH", "/assignment/assign-1")
    assert captured["payload"] == {"claude_session_id": "ses-abc"}
    # Local DB must NOT have been written (empty local DB, thin-client).
    row = get_connection().execute(
        "SELECT claude_session_id FROM assignments WHERE assignment_id='assign-1'"
    ).fetchone()
    assert row is None


def test_update_claude_session_id_falls_back_to_local_on_daemon_error(monkeypatch, coord_db) -> None:
    import httpx

    conn = get_connection()
    conn.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, issue_number, issue_title) "
        "VALUES (?, ?, ?, ?, ?)",
        ("assign-2", "m", "api", 1, "t"),
    )
    conn.commit()

    from coord.state import update_assignment_claude_session_id

    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: _FakeSvc())
    monkeypatch.setattr(
        cc,
        "request_resource",
        lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("daemon down")),
    )

    update_assignment_claude_session_id("assign-2", "ses-xyz")

    row = conn.execute(
        "SELECT claude_session_id FROM assignments WHERE assignment_id='assign-2'"
    ).fetchone()
    assert row["claude_session_id"] == "ses-xyz"


def test_get_test_plan_routes_to_daemon(monkeypatch, coord_db) -> None:
    """#1946: the read is now ``GET /assignment/{id}``'s ``test_plan`` field,
    replacing ``POST /assignment-test-plan``."""
    from coord.state import get_test_plan

    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: _FakeSvc())
    captured: dict = {}

    def _fake_fetch_assignment(svc, assignment_id, **kw):
        captured["assignment_id"] = assignment_id
        return {"assignment_id": assignment_id, "test_plan": {"steps": ["a"], "blockers": []}}

    monkeypatch.setattr(cc, "fetch_assignment", _fake_fetch_assignment)

    plan = get_test_plan("assign-1")

    assert plan == {"steps": ["a"], "blockers": []}
    assert captured["assignment_id"] == "assign-1"


def test_get_test_plan_decodes_a_json_string_test_plan(monkeypatch, coord_db) -> None:
    """A daemon that still serves ``test_plan`` as a JSON *string* is decoded.

    ``board_schema`` types it ``dict | None``, but the RPC route this replaces
    handed back the raw column, so the client keeps accepting both rather than
    depending on which side of #1849 the daemon is on.
    """
    import json

    from coord.state import get_test_plan

    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: _FakeSvc())
    monkeypatch.setattr(
        cc, "fetch_assignment",
        lambda svc, aid, **kw: {"test_plan": json.dumps({"steps": ["a"], "blockers": []})},
    )

    assert get_test_plan("assign-1") == {"steps": ["a"], "blockers": []}


def test_get_test_plan_falls_back_to_local_on_daemon_error(monkeypatch, coord_db) -> None:
    import json

    conn = get_connection()
    conn.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, issue_number, issue_title, test_plan) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("assign-3", "m", "api", 1, "t", json.dumps({"steps": [], "blockers": []})),
    )
    conn.commit()

    from coord.state import get_test_plan

    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: _FakeSvc())
    monkeypatch.setattr(
        cc,
        "fetch_assignment",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("daemon down")),
    )

    assert get_test_plan("assign-3") == {"steps": [], "blockers": []}


def test_set_assignment_failure_reason_routes_to_daemon(monkeypatch, coord_db) -> None:
    from coord.state import set_assignment_failure_reason

    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: _FakeSvc())
    captured: dict = {}
    monkeypatch.setattr(
        cc,
        "request_resource",
        lambda svc, method, path, payload=None, **kw: captured.update(
            method=method, path=path, payload=payload
        ) or {"updated": True},
    )

    set_assignment_failure_reason("assign-1", "worktree add failed")

    # #1946: was POST /assignment-failure-reason {"reason": ...}; the resource
    # route spells the field `failure_reason`.
    assert (captured["method"], captured["path"]) == ("PATCH", "/assignment/assign-1")
    assert captured["payload"] == {"failure_reason": "worktree add failed"}
    row = get_connection().execute(
        "SELECT failure_reason FROM assignments WHERE assignment_id='assign-1'"
    ).fetchone()
    assert row is None


def test_set_assignment_failure_reason_falls_back_to_local_on_daemon_error(
    monkeypatch, coord_db
) -> None:
    conn = get_connection()
    conn.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, issue_number, issue_title, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("assign-4", "m", "api", 1, "t", "running"),
    )
    conn.commit()

    import httpx

    from coord.state import set_assignment_failure_reason

    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: _FakeSvc())
    monkeypatch.setattr(
        cc,
        "post_record",
        lambda svc, path, payload, **kw: (_ for _ in ()).throw(
            httpx.ConnectError("daemon down")
        ),
    )

    set_assignment_failure_reason("assign-4", "boom")

    row = conn.execute(
        "SELECT failure_reason, status FROM assignments WHERE assignment_id='assign-4'"
    ).fetchone()
    assert row["failure_reason"] == "boom"
    assert row["status"] == "failed"


# ══════════════════════════════════════════════════════════════════════════
# serve_app daemon endpoints
# ══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def rw_db(tmp_path: Path):
    """Thread-safe file-backed DB for TestClient (mirrors test_serve.py /
    test_review_verdict_relay.py)."""
    from coord import db

    conn = sqlite3.connect(str(tmp_path / "rw.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    db.override_connection(conn)
    yield conn


@pytest.fixture
def file_db(tmp_path: Path) -> Path:
    """Minimal on-disk coord.db for SqliteStore (read-only DAO)."""
    p = tmp_path / "coord.db"
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    conn.commit()
    conn.close()
    return p


def test_post_assignment_session_id_endpoint(
    file_db: Path, valid_config_path: Path, rw_db
) -> None:
    rw_db.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, issue_number, issue_title) "
        "VALUES (?, ?, ?, ?, ?)",
        ("a1", "m", "api", 1, "t"),
    )
    rw_db.commit()

    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/assignment-session-id",
            json={"assignment_id": "a1", "claude_session_id": "ses-1"},
        )
    assert resp.status_code == 200 and resp.json()["ok"] is True
    row = rw_db.execute(
        "SELECT claude_session_id FROM assignments WHERE assignment_id='a1'"
    ).fetchone()
    assert row["claude_session_id"] == "ses-1"


def test_post_assignment_session_id_endpoint_missing_field(
    file_db: Path, valid_config_path: Path, rw_db
) -> None:
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post("/assignment-session-id", json={"assignment_id": "a1"})
    assert resp.status_code == 400


def test_post_assignment_failure_reason_endpoint(
    file_db: Path, valid_config_path: Path, rw_db
) -> None:
    rw_db.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, issue_number, issue_title, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("a2", "m", "api", 1, "t", "running"),
    )
    rw_db.commit()

    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/assignment-failure-reason",
            json={"assignment_id": "a2", "reason": "launch failed"},
        )
    assert resp.status_code == 200 and resp.json()["ok"] is True
    row = rw_db.execute(
        "SELECT failure_reason, status FROM assignments WHERE assignment_id='a2'"
    ).fetchone()
    assert row["failure_reason"] == "launch failed"
    assert row["status"] == "failed"


def test_post_assignment_failure_reason_endpoint_missing_field(
    file_db: Path, valid_config_path: Path, rw_db
) -> None:
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post("/assignment-failure-reason", json={"assignment_id": "a2"})
    assert resp.status_code == 400


def test_post_assignment_test_plan_endpoint(
    file_db: Path, valid_config_path: Path, rw_db
) -> None:
    import json

    rw_db.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, issue_number, issue_title, test_plan) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("a3", "m", "api", 1, "t", json.dumps({"steps": ["x"], "blockers": []})),
    )
    rw_db.commit()

    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post("/assignment-test-plan", json={"assignment_id": "a3"})
    assert resp.status_code == 200
    assert json.loads(resp.json()["test_plan"]) == {"steps": ["x"], "blockers": []}


def test_post_assignment_test_plan_endpoint_missing_field(
    file_db: Path, valid_config_path: Path, rw_db
) -> None:
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post("/assignment-test-plan", json={})
    assert resp.status_code == 400


def test_post_issue_test_mode_endpoint(
    file_db: Path, valid_config_path: Path, rw_db
) -> None:
    import json

    rw_db.execute(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("api", 287, "t", "", "open", json.dumps(["test-mode:smoke"]), 1.0),
    )
    rw_db.commit()

    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/issue-test-mode", json={"repo_name": "api", "issue_number": 287}
        )
    assert resp.status_code == 200
    assert resp.json()["test_mode"] == "smoke"


def test_post_issue_test_mode_endpoint_no_row_returns_null(
    file_db: Path, valid_config_path: Path, rw_db
) -> None:
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/issue-test-mode", json={"repo_name": "api", "issue_number": 404}
        )
    assert resp.status_code == 200
    assert resp.json()["test_mode"] is None


def test_post_issue_test_mode_endpoint_missing_field(
    file_db: Path, valid_config_path: Path, rw_db
) -> None:
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post("/issue-test-mode", json={"repo_name": "api"})
    assert resp.status_code == 400


# ══════════════════════════════════════════════════════════════════════════
# chat_continue: board-based lookup + conditional local session-id read
# ══════════════════════════════════════════════════════════════════════════


def test_chat_continue_finds_prior_assignment_via_daemon_board(monkeypatch, coord_db, tmp_path: Path) -> None:
    """On a thin client (empty local board), chat_continue must find the prior
    assignment via read_board() (the daemon's board), not a local lookup —
    and must NOT attempt the local claude_session_id DB read (svc is set)."""
    from click.testing import CliRunner
    from coord.cli import main

    assert get_connection().execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 0

    cfg_path = tmp_path / "coordinator.yml"
    cfg_path.write_text(
        "repos:\n  - name: api\n    github: acme/api\n"
        "machines:\n  - name: laptop\n    host: laptop.tailnet\n"
        "    repos: [api]\n"
    )

    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: _FakeSvc())
    # #1080: _load_config now always fetches on a thin client (never trusts a
    # local file that happens to exist), so stand in for the daemon's /config
    # with the same coordinator.yml this test already wrote to cfg_path.
    monkeypatch.setattr(cc, "fetch_remote_config", lambda svc, **kw: cfg_path)
    monkeypatch.setattr(
        cc,
        "fetch_board_payload",
        lambda svc, **kw: {
            "assignments": [
                {
                    "assignment_id": "chat-1",
                    "type": "refinement",
                    "status": "done",
                    "machine_name": "laptop",
                    "repo_name": "api",
                    "repo_github": "acme/api",
                    "issue_number": 42,
                    "issue_title": "Refine feature",
                    "branch": "issue-42-refine",
                }
            ]
        },
    )

    # Prove the local-DB session-id read path is never reached: /status will
    # supply the session id instead (the #315 fallback).
    from coord import network as network_mod

    class _StatusResult:
        ok = True
        data = {
            "active": [],
            "completed": [
                {"id": "chat-1", "claude_session_id": "ses-daemon"},
            ],
        }

    monkeypatch.setattr(network_mod, "fetch_status", lambda *a, **k: _StatusResult())

    captured = {}

    def _fake_dispatch(proposal, config, **kw):
        captured["resume_session_id"] = proposal.resume_session_id
        captured["type"] = proposal.type
        return {"id": "chat-2"}

    monkeypatch.setattr("coord.dispatch.dispatch", _fake_dispatch)
    monkeypatch.setattr("coord.state.record_dispatched", lambda **kw: None)
    monkeypatch.setattr(
        cc, "post_record", lambda svc, path, payload, **kw: {"ok": True}
    )

    result = CliRunner().invoke(
        main, ["chat-continue", "chat-1", "--config", str(cfg_path), "next message"]
    )

    assert result.exit_code == 0, result.output
    assert captured["resume_session_id"] == "ses-daemon"
    assert captured["type"] == "refinement"

    # Local assignments table is still empty — proves no local board write/read.
    assert get_connection().execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 0


def test_chat_continue_not_found_reports_error(monkeypatch, coord_db, tmp_path: Path) -> None:
    from click.testing import CliRunner
    from coord.cli import main

    cfg_path = tmp_path / "coordinator.yml"
    cfg_path.write_text(
        "repos:\n  - name: api\n    github: acme/api\n"
        "machines:\n  - name: laptop\n    host: laptop.tailnet\n"
        "    repos: [api]\n"
    )

    result = CliRunner().invoke(
        main, ["chat-continue", "no-such-id", "--config", str(cfg_path), "hi"]
    )
    assert result.exit_code != 0
    from .conftest import output_and_stderr

    assert "not found" in output_and_stderr(result)


# ══════════════════════════════════════════════════════════════════════════
# board.active-based in_flight rebuild: _dispatch_followup / _dispatch_headless
# ══════════════════════════════════════════════════════════════════════════


def _daemon_board_with_peer(**overrides) -> dict:
    entry = {
        "assignment_id": "peer-1",
        "type": "work",
        "status": "running",
        "machine_name": "server",
        "repo_name": "api",
        "repo_github": "acme/api",
        "issue_number": 99,
        "issue_title": "Peer work",
        "files_allowed": ["shared.py"],
    }
    entry.update(overrides)
    return {"assignments": [entry]}


def test_dispatch_followup_in_flight_from_daemon_board(monkeypatch, coord_db) -> None:
    """#906: _dispatch_followup's peer-conflict in_flight list must come from
    read_board() (the daemon's board.active on a thin client), not the local
    (empty) dispatched.json ledger."""
    from coord.commands.plan_followup import _dispatch_followup
    from coord.models import Assignment

    cfg = Config(
        repos=[Repo(name="api", github="acme/api")],
        machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"])],
    )
    original = Assignment(
        assignment_id="work-1", machine_name="laptop", repo_name="api",
        issue_number=10, issue_title="Fix bug", status="done",
        branch="issue-10-fix",
    )

    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: _FakeSvc())
    monkeypatch.setattr(
        cc, "fetch_board_payload", lambda svc, **kw: _daemon_board_with_peer()
    )
    monkeypatch.setattr(cc, "post_record", lambda svc, path, payload, **kw: {"ok": True})

    captured: dict = {}

    def _fake_dispatch(proposal, config, **kw):
        return {"id": "assign-2"}

    def _fake_post_briefing(proposal, config, *, assignment_id, do_not_touch=None):
        captured["do_not_touch"] = do_not_touch
        captured["assignment_id"] = assignment_id

    monkeypatch.setattr("coord.dispatch.dispatch", _fake_dispatch)
    monkeypatch.setattr("coord.dispatch.post_briefing", _fake_post_briefing)
    monkeypatch.setattr("coord.state.record_dispatched", lambda **kw: None)

    result = _dispatch_followup(cfg, original, "follow-up briefing")

    assert result == "assign-2"
    assert captured["assignment_id"] == "assign-2"
    assert ("shared.py", "server is working there") in captured["do_not_touch"]


def test_dispatch_headless_in_flight_from_daemon_board(monkeypatch, coord_db) -> None:
    """#906: `coord assign`'s _dispatch_headless in_flight peer-conflict list
    must come from read_board() (the daemon's board.active on a thin client)."""
    from coord.commands.dispatch_workers import _dispatch_headless

    cfg = Config(
        repos=[Repo(name="api", github="acme/api")],
        machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"])],
    )

    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: _FakeSvc())
    monkeypatch.setattr(
        cc, "fetch_board_payload", lambda svc, **kw: _daemon_board_with_peer()
    )
    monkeypatch.setattr(cc, "post_record", lambda svc, path, payload, **kw: {"ok": True})

    captured: dict = {}

    def _fake_dispatch(proposal, config, **kw):
        return {"id": "assign-2"}

    def _fake_post_briefing(proposal, config, *, assignment_id, do_not_touch=None):
        captured["do_not_touch"] = do_not_touch

    monkeypatch.setattr("coord.dispatch.dispatch", _fake_dispatch)
    monkeypatch.setattr("coord.dispatch.post_briefing", _fake_post_briefing)
    monkeypatch.setattr("coord.state.record_dispatched", lambda **kw: None)

    _dispatch_headless(
        machine="laptop", repo="api", issue=10, briefing="do the thing",
        model=None, dry_run=False, plan_only=False, no_plan=False,
        force=True, no_pull=True, skip_freshness=True, cfg=cfg,
        machine_obj=cfg.machines[0], repo_cfg=cfg.repos[0],
        issue_data={}, issue_title="Fix bug",
    )

    assert ("shared.py", "server is working there") in captured["do_not_touch"]
