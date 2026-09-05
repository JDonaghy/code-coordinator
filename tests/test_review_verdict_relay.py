"""#905 regression guard: review verdict relay must route through the daemon.

Root cause: ``load_done_reviews_needing_post``, ``update_assignment_review_findings``,
and ``mark_review_posted`` all hit the LOCAL SQLite directly.  On a thin client
that table is ~empty so ``coord notify`` / ``coord post-pending-reviews`` finds no
candidates and captures no verdict → ``coord merge`` blocks on "review required"
forever.

Fix: all three now route through the daemon (``GET /board`` for reading candidates,
``POST /review-findings`` for persisting verdict+body, ``POST /review-posted`` for
stamping ``review_posted_at``) when ``board_service`` is configured.

Test structure mirrors ``tests/test_state_review_findings_daemon.py`` (the #877
thin-client read fix): use an empty local DB to prove no local-DB fallthrough.
"""

from __future__ import annotations

import json
import sqlite3
import warnings
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

import coord.client as cc
from coord.config import load as load_config
from coord.dao import SqliteStore
from coord.db import _ensure_schema, get_connection
from coord.serve_app import build_app


# ── helpers ──────────────────────────────────────────────────────────────────


class _FakeSvc:
    url = "http://daemon:7435"
    token = "t"


def _daemon_payload_with_review(
    *,
    assignment_id: str = "rev-905",
    status: str = "done",
    review_posted_at=None,
    repo_name: str = "claude-coordinator",
    machine_name: str = "precision",
    repo_github: str = "acme/repo",
    issue_number: int = 905,
    review_target: str = "42",
) -> dict:
    """Build a minimal /board payload containing one review assignment."""
    return {
        "assignments": [
            {
                "assignment_id": assignment_id,
                "type": "review",
                "status": status,
                "machine_name": machine_name,
                "repo_name": repo_name,
                "repo_github": repo_github,
                "issue_number": issue_number,
                "issue_title": "Test issue",
                "review_target": review_target,
                "review_of_assignment_id": "work-1",
                "review_posted_at": review_posted_at,
            }
        ]
    }


# ── load_done_reviews_needing_post: daemon-aware read ────────────────────────


def test_load_done_reviews_reads_from_daemon_when_board_service_set(
    monkeypatch, coord_db
) -> None:
    """Thin-client mode: the local DB is empty; candidates live on the daemon.

    The function must return the daemon's done-review row, NOT an empty list.
    """
    from coord.state import load_done_reviews_needing_post

    # Local DB has NO rows — mirrors the thin-client reality.
    assert get_connection().execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 0

    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: _FakeSvc())
    monkeypatch.setattr(
        cc,
        "fetch_board_payload",
        lambda svc, **kw: _daemon_payload_with_review(),
    )

    candidates = load_done_reviews_needing_post()
    assert len(candidates) == 1
    assert candidates[0]["assignment_id"] == "rev-905"
    assert candidates[0]["review_target"] == "42"


def test_load_done_reviews_skips_already_posted(monkeypatch, coord_db) -> None:
    """Done reviews with review_posted_at set must be excluded."""
    from coord.state import load_done_reviews_needing_post

    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: _FakeSvc())
    monkeypatch.setattr(
        cc,
        "fetch_board_payload",
        lambda svc, **kw: _daemon_payload_with_review(review_posted_at=1234567.0),
    )

    candidates = load_done_reviews_needing_post()
    assert candidates == []


def test_load_done_reviews_filters_by_repo(monkeypatch, coord_db) -> None:
    """Optional repo_name filter is applied to the daemon payload."""
    from coord.state import load_done_reviews_needing_post

    payload = {
        "assignments": [
            {
                "assignment_id": "rev-api",
                "type": "review",
                "status": "done",
                "machine_name": "precision",
                "repo_name": "api",
                "repo_github": "acme/api",
                "issue_number": 1,
                "review_posted_at": None,
                "review_target": "10",
            },
            {
                "assignment_id": "rev-lib",
                "type": "review",
                "status": "done",
                "machine_name": "precision",
                "repo_name": "lib",
                "repo_github": "acme/lib",
                "issue_number": 2,
                "review_posted_at": None,
                "review_target": "20",
            },
        ]
    }
    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: _FakeSvc())
    monkeypatch.setattr(cc, "fetch_board_payload", lambda svc, **kw: payload)

    api_only = load_done_reviews_needing_post(repo_name="api")
    assert len(api_only) == 1 and api_only[0]["assignment_id"] == "rev-api"

    all_repos = load_done_reviews_needing_post()
    assert {r["assignment_id"] for r in all_repos} == {"rev-api", "rev-lib"}


def test_load_done_reviews_falls_back_to_local_when_no_service(coord_db) -> None:
    """Daemon host (board_service unset by autouse fixture): reads local DB."""
    from coord.state import load_done_reviews_needing_post

    conn = get_connection()
    conn.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, repo_github, "
        " issue_number, issue_title, status, type, review_target) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("rev-local", "precision", "api", "acme/api", 1, "t", "done", "review", "5"),
    )
    conn.commit()

    candidates = load_done_reviews_needing_post()
    assert any(r["assignment_id"] == "rev-local" for r in candidates)


def test_load_done_reviews_falls_back_to_local_on_daemon_error(
    monkeypatch, coord_db
) -> None:
    """If the daemon fetch raises, fall back to the local DB (best-effort)."""
    from coord.state import load_done_reviews_needing_post

    conn = get_connection()
    conn.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, repo_github, "
        " issue_number, issue_title, status, type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("rev-fallback", "precision", "api", "acme/api", 2, "t", "done", "review"),
    )
    conn.commit()

    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: _FakeSvc())
    monkeypatch.setattr(
        cc,
        "fetch_board_payload",
        lambda svc, **kw: (_ for _ in ()).throw(RuntimeError("daemon down")),
    )

    candidates = load_done_reviews_needing_post()
    assert any(r["assignment_id"] == "rev-fallback" for r in candidates)


# ── update_assignment_review_findings: daemon routing ────────────────────────


def test_update_review_findings_routes_to_daemon(monkeypatch, coord_db) -> None:
    """When board_service is set, persist routes to /review-findings NOT local DB."""
    from coord.state import update_assignment_review_findings

    captured: dict = {}
    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: _FakeSvc())
    monkeypatch.setattr(
        cc,
        "post_record",
        lambda svc, path, payload, **kw: captured.update(path=path, payload=payload)
        or {"ok": True},
    )

    update_assignment_review_findings(
        "rev-905", verdict="approve", body="LGTM"
    )

    assert captured["path"] == "/review-findings"
    assert captured["payload"]["assignment_id"] == "rev-905"
    assert captured["payload"]["verdict"] == "approve"
    assert captured["payload"]["body"] == "LGTM"

    # Local DB must NOT have been written (empty local DB, thin-client)
    row = get_connection().execute(
        "SELECT review_verdict FROM assignments WHERE assignment_id='rev-905'"
    ).fetchone()
    assert row is None


def test_update_review_findings_writes_local_when_no_service(coord_db) -> None:
    """Daemon host (no board_service): writes go to the local SQLite."""
    from coord.state import update_assignment_review_findings

    conn = get_connection()
    conn.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, issue_number, issue_title) "
        "VALUES (?, ?, ?, ?, ?)",
        ("rev-local2", "m", "api", 1, "t"),
    )
    conn.commit()

    update_assignment_review_findings("rev-local2", verdict="request-changes", body="Fix it")

    row = conn.execute(
        "SELECT review_verdict, review_findings FROM assignments "
        "WHERE assignment_id='rev-local2'"
    ).fetchone()
    assert row["review_verdict"] == "request-changes"
    findings = json.loads(row["review_findings"])
    assert findings["verdict"] == "request-changes" and findings["body"] == "Fix it"


# ── mark_review_posted: daemon routing ───────────────────────────────────────


def test_mark_review_posted_routes_to_daemon(monkeypatch, coord_db) -> None:
    """When board_service is set, mark_review_posted POSTs to /review-posted."""
    from coord.state import mark_review_posted

    captured: dict = {}
    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: _FakeSvc())
    monkeypatch.setattr(
        cc,
        "post_record",
        lambda svc, path, payload, **kw: captured.update(path=path, payload=payload)
        or {"ok": True},
    )

    mark_review_posted("rev-905")

    assert captured["path"] == "/review-posted"
    assert captured["payload"]["assignment_id"] == "rev-905"

    # Local DB must NOT have been written (empty local DB, thin-client)
    row = get_connection().execute(
        "SELECT review_posted_at FROM assignments WHERE assignment_id='rev-905'"
    ).fetchone()
    assert row is None


def test_mark_review_posted_writes_local_when_no_service(coord_db) -> None:
    """Daemon host: mark_review_posted sets review_posted_at on local DB."""
    from coord.state import mark_review_posted

    conn = get_connection()
    conn.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, issue_number, issue_title) "
        "VALUES (?, ?, ?, ?, ?)",
        ("rev-local3", "m", "api", 1, "t"),
    )
    conn.commit()

    mark_review_posted("rev-local3")

    row = conn.execute(
        "SELECT review_posted_at FROM assignments WHERE assignment_id='rev-local3'"
    ).fetchone()
    assert row["review_posted_at"] is not None


# ── daemon endpoints: POST /review-findings + POST /review-posted ─────────────


@pytest.fixture
def rw_db(tmp_path: Path):
    """Thread-safe file-backed DB for TestClient (mirrors test_serve.py)."""
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


def test_post_review_findings_endpoint_persists_verdict(
    file_db: Path, valid_config_path: Path, rw_db
) -> None:
    """POST /review-findings writes verdict+body to the daemon's DB."""
    # Seed a done review row in the daemon DB.
    rw_db.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, repo_github, "
        " issue_number, issue_title, status, type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("rev-905", "precision", "api", "acme/api", 905, "t", "done", "review"),
    )
    rw_db.commit()

    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/review-findings",
            json={"assignment_id": "rev-905", "verdict": "approve", "body": "LGTM"},
        )
    assert resp.status_code == 200 and resp.json()["ok"] is True

    row = rw_db.execute(
        "SELECT review_verdict, review_findings FROM assignments "
        "WHERE assignment_id='rev-905'"
    ).fetchone()
    assert row["review_verdict"] == "approve"
    findings = json.loads(row["review_findings"])
    assert findings["verdict"] == "approve" and findings["body"] == "LGTM"


def test_post_review_findings_endpoint_missing_field_returns_400(
    file_db: Path, valid_config_path: Path, rw_db
) -> None:
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/review-findings",
            json={"assignment_id": "rev-905"},  # missing verdict + body
        )
    assert resp.status_code == 400


def test_post_review_posted_endpoint_sets_timestamp(
    file_db: Path, valid_config_path: Path, rw_db
) -> None:
    """POST /review-posted stamps review_posted_at on the daemon's DB."""
    rw_db.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, repo_github, "
        " issue_number, issue_title, status, type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("rev-905", "precision", "api", "acme/api", 905, "t", "done", "review"),
    )
    rw_db.commit()

    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post("/review-posted", json={"assignment_id": "rev-905"})
    assert resp.status_code == 200 and resp.json()["ok"] is True

    row = rw_db.execute(
        "SELECT review_posted_at FROM assignments WHERE assignment_id='rev-905'"
    ).fetchone()
    assert row["review_posted_at"] is not None and float(row["review_posted_at"]) > 0


def test_post_review_posted_endpoint_missing_field_returns_400(
    file_db: Path, valid_config_path: Path, rw_db
) -> None:
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post("/review-posted", json={})  # missing assignment_id
    assert resp.status_code == 400


# ── POST /notified (#1493) ────────────────────────────────────────────────────


def test_post_notified_endpoint_writes_ledger_and_status(
    file_db: Path, valid_config_path: Path, rw_db
) -> None:
    """POST /notified writes the notification ledger AND updates assignment
    status on the daemon's DB — the endpoint backing mark_notified's daemon
    route (#1493)."""
    rw_db.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, repo_github, "
        " issue_number, issue_title, status, type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("aid-1493", "precision", "api", "acme/api", 1493, "t", "running", "work"),
    )
    rw_db.commit()

    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/notified",
            json={
                "assignment_id": "aid-1493",
                "event": "completion",
                "branch": "issue-1493-foo",
            },
        )
    assert resp.status_code == 200 and resp.json()["ok"] is True

    notif_row = rw_db.execute(
        "SELECT event, branch FROM notifications WHERE assignment_id='aid-1493'"
    ).fetchone()
    assert notif_row["event"] == "completion"
    assert notif_row["branch"] == "issue-1493-foo"

    assignment_row = rw_db.execute(
        "SELECT status, branch FROM assignments WHERE assignment_id='aid-1493'"
    ).fetchone()
    assert assignment_row["status"] == "done"
    assert assignment_row["branch"] == "issue-1493-foo"


def test_post_notified_endpoint_missing_field_returns_400(
    file_db: Path, valid_config_path: Path, rw_db
) -> None:
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post("/notified", json={"assignment_id": "aid-x"})  # missing event
    assert resp.status_code == 400


# ── idempotency via review_posted_at ─────────────────────────────────────────


def test_post_review_posted_idempotent(
    file_db: Path, valid_config_path: Path, rw_db
) -> None:
    """Calling /review-posted twice should not raise; second call is a harmless UPDATE."""
    rw_db.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, repo_github, "
        " issue_number, issue_title, status, type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("rev-idem", "precision", "api", "acme/api", 1, "t", "done", "review"),
    )
    rw_db.commit()

    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        r1 = cli.post("/review-posted", json={"assignment_id": "rev-idem"})
        r2 = cli.post("/review-posted", json={"assignment_id": "rev-idem"})
    assert r1.status_code == 200 and r2.status_code == 200


# ── #3113: atomic review-dispatch claim — daemon routing + endpoints ────────


def test_claim_review_dispatch_routes_to_daemon(monkeypatch, coord_db) -> None:
    """When board_service is set, the claim POSTs to /review-claim NOT the
    local DB (mirrors test_update_review_findings_routes_to_daemon above)."""
    from coord.state import claim_review_dispatch

    captured: dict = {}
    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: _FakeSvc())
    monkeypatch.setattr(
        cc,
        "post_record",
        lambda svc, path, payload, **kw: captured.update(path=path, payload=payload)
        or {"ok": True, "claimed": True},
    )

    won = claim_review_dispatch("work-905")

    assert won is True
    assert captured["path"] == "/review-claim"
    assert captured["payload"]["of_assignment_id"] == "work-905"
    # Local DB must NOT have been written (empty local DB, thin-client).
    row = get_connection().execute(
        "SELECT 1 FROM review_claims WHERE of_assignment_id='work-905'"
    ).fetchone()
    assert row is None


def test_release_review_dispatch_claim_routes_to_daemon(monkeypatch, coord_db) -> None:
    """Release must route through the SAME daemon seam as claim — a
    purely-local release on a thin client would touch the wrong (empty) DB
    and silently no-op, leaving the daemon-held claim stuck forever."""
    from coord.state import release_review_dispatch_claim

    captured: dict = {}
    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: _FakeSvc())
    monkeypatch.setattr(
        cc,
        "post_record",
        lambda svc, path, payload, **kw: captured.update(path=path, payload=payload)
        or {"ok": True},
    )

    release_review_dispatch_claim("work-905")

    assert captured["path"] == "/review-claim-release"
    assert captured["payload"]["of_assignment_id"] == "work-905"


def test_claim_review_dispatch_writes_local_when_no_service(coord_db) -> None:
    """Daemon host (no board_service): the claim writes to the local SQLite."""
    from coord.state import claim_review_dispatch

    assert claim_review_dispatch("work-local") is True
    row = get_connection().execute(
        "SELECT 1 FROM review_claims WHERE of_assignment_id='work-local'"
    ).fetchone()
    assert row is not None


def test_post_review_claim_endpoint_second_call_loses(
    file_db: Path, valid_config_path: Path, rw_db
) -> None:
    """POST /review-claim: first call wins (claimed=True), a second call for
    the same of_assignment_id loses (claimed=False) — the daemon-side half
    of the atomic race fix."""
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        r1 = cli.post("/review-claim", json={"of_assignment_id": "work-abc"})
        r2 = cli.post("/review-claim", json={"of_assignment_id": "work-abc"})

    assert r1.status_code == 200 and r1.json()["claimed"] is True
    assert r2.status_code == 200 and r2.json()["claimed"] is False


def test_post_review_claim_endpoint_missing_field_returns_400(
    file_db: Path, valid_config_path: Path, rw_db
) -> None:
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post("/review-claim", json={})
    assert resp.status_code == 400


def test_post_review_claim_release_endpoint_allows_reclaim(
    file_db: Path, valid_config_path: Path, rw_db
) -> None:
    """POST /review-claim-release frees a claim so a later /review-claim for
    the same of_assignment_id wins again."""
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        first = cli.post("/review-claim", json={"of_assignment_id": "work-def"})
        assert first.json()["claimed"] is True

        released = cli.post(
            "/review-claim-release", json={"of_assignment_id": "work-def"}
        )
        assert released.status_code == 200

        reclaimed = cli.post("/review-claim", json={"of_assignment_id": "work-def"})
    assert reclaimed.json()["claimed"] is True


def test_post_review_claim_release_endpoint_missing_field_returns_400(
    file_db: Path, valid_config_path: Path, rw_db
) -> None:
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post("/review-claim-release", json={})
    assert resp.status_code == 400


# ── request-changes persists correctly ───────────────────────────────────────


def test_post_review_findings_request_changes(
    file_db: Path, valid_config_path: Path, rw_db
) -> None:
    """request-changes verdict must persist so coord bounce can read it."""
    rw_db.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, repo_github, "
        " issue_number, issue_title, status, type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("rev-rc", "precision", "api", "acme/api", 1, "t", "done", "review"),
    )
    rw_db.commit()

    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/review-findings",
            json={
                "assignment_id": "rev-rc",
                "verdict": "request-changes",
                "body": "Has a bug on line 42.",
            },
        )
    assert resp.status_code == 200

    row = rw_db.execute(
        "SELECT review_verdict, review_findings FROM assignments "
        "WHERE assignment_id='rev-rc'"
    ).fetchone()
    assert row["review_verdict"] == "request-changes"
    findings = json.loads(row["review_findings"])
    assert findings["verdict"] == "request-changes"


# ── integration: post_orphaned_review_findings on a thin client ───────────────


def test_post_orphaned_finds_and_posts_from_daemon_board(
    monkeypatch, coord_db, tmp_path: Path
) -> None:
    """Core integration test for #905.

    Setup: local DB is EMPTY (thin-client reality). The daemon board has a done
    review with review_posted_at=None and a parseable REVIEW_VERDICT in its log.

    After post_orphaned_review_findings():
    - /review-findings was called on the daemon (captured by fake post_record)
    - /review-posted was called on the daemon after the GitHub post
    - The local DB remains empty (no local writes)
    """
    import coord.notify as notify_mod
    from coord.config import load as load_config

    # Confirm local DB is empty.
    assert get_connection().execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 0

    # Build a minimal config (one machine).
    cfg_path = tmp_path / "coordinator.yml"
    cfg_path.write_text(
        "repos:\n  - name: api\n    github: acme/api\n"
        "machines:\n  - name: precision\n    host: precision.tailnet\n"
        "    capabilities: [python]\n    repos: [api]\n"
    )
    config = load_config(cfg_path)

    # Daemon board has the done review candidate.
    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: _FakeSvc())
    monkeypatch.setattr(
        cc,
        "fetch_board_payload",
        lambda svc, **kw: _daemon_payload_with_review(
            assignment_id="rev-905",
            repo_name="api",
            machine_name="precision",
            repo_github="acme/api",
            review_target="99",
        ),
    )

    # Track daemon write calls.
    daemon_calls: list[tuple[str, dict]] = []

    def fake_post_record(svc, path, payload, **kw):
        daemon_calls.append((path, payload))
        return {"ok": True}

    monkeypatch.setattr(cc, "post_record", fake_post_record)

    # The agent has the log for this review (must include END_REVIEW terminator).
    log_path = tmp_path / "review.log"
    log_path.write_text(
        "REVIEW_VERDICT: approve\nREVIEW_BODY:\nLooks great!\nEND_REVIEW\n",
        encoding="utf-8",
    )
    agent_status = {
        "active": [],
        "completed": [{"id": "rev-905", "log_path": str(log_path), "status": "done"}],
    }
    monkeypatch.setattr(notify_mod, "_agent_status", lambda host: agent_status)

    # Stub GitHub posting.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with patch("coord.notify.github_ops.post_pr_review") as mock_gh_review, \
             patch("coord.notify.github_ops.post_issue_comment"):
            posted = notify_mod.post_orphaned_review_findings(config)

    # The function reported success.
    assert "rev-905" in posted

    # GitHub PR review was posted.
    mock_gh_review.assert_called_once()

    # /review-findings was sent to the daemon.
    findings_calls = [c for c in daemon_calls if c[0] == "/review-findings"]
    assert len(findings_calls) == 1
    assert findings_calls[0][1]["assignment_id"] == "rev-905"
    assert findings_calls[0][1]["verdict"] == "approve"

    # /review-posted was sent to the daemon after successful GitHub post.
    posted_calls = [c for c in daemon_calls if c[0] == "/review-posted"]
    assert len(posted_calls) == 1
    assert posted_calls[0][1]["assignment_id"] == "rev-905"

    # #1493: /notified was ALSO sent to the daemon — mark_notified() is called
    # right after mark_review_posted() (notify.py's `if not already_notified:`
    # branch) and, before #1493, wrote straight to the empty local ledger
    # instead of routing here, the exact gap this test guards against.
    notified_calls = [c for c in daemon_calls if c[0] == "/notified"]
    assert len(notified_calls) == 1
    assert notified_calls[0][1]["assignment_id"] == "rev-905"
    assert notified_calls[0][1]["event"] == "completion"

    # Local DB was NOT written (empty still) — neither table.
    assert get_connection().execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 0
    assert get_connection().execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0

    # #1493: no #615 UserWarning — mark_notified is now routed, not guarded.
    guard_warns = [w for w in caught if "#615" in str(w.message)]
    assert not guard_warns, f"unexpected #615 warning(s): {[str(w.message) for w in guard_warns]}"
