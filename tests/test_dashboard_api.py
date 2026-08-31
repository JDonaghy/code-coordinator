"""Tests for the dashboard's #2990 exposure of #2986's `coord portal answer`
write path: `GET /api/portal/needs-input` + `POST /api/portal/answer`.

Uses the same ``rw_db`` (thread-safe, file-backed sqlite) pattern as
``tests/test_dashboard.py`` — ``TestClient`` runs the async handler on a
worker thread, which the autouse ``coord_db`` in-memory connection
(``tests/conftest.py``) can't touch.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from coord.config import Config
from coord.dashboard.server import build_app
from coord.models import Machine, Repo


@pytest.fixture(autouse=True)
def _no_spa_dist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the legacy dashboard — see test_dashboard.py's identical fixture
    for why this isolation is load-bearing even on a dev box."""
    monkeypatch.setattr(
        "coord.dashboard.server.WEBAPP_DIST",
        Path("/nonexistent/dist"),
    )


def _config() -> Config:
    return Config(
        repos=[Repo(name="api", github="acme/api")],
        machines=[Machine(
            name="laptop", host="laptop.tailnet", repos=["api"],
            repo_paths={"api": "/tmp/api"},
        )],
    )


def _client() -> TestClient:
    return TestClient(build_app(_config()))


@pytest.fixture
def rw_db(tmp_path: Path):
    """A thread-safe (file-backed) coord.db override — see
    tests/test_dashboard.py's identical fixture for the full rationale."""
    from coord import db
    from coord.db import _ensure_schema

    conn = sqlite3.connect(str(tmp_path / "rw.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    db.override_connection(conn)
    yield conn


def _push_question(submission_id: str, question: str):
    from coord import portal_store

    row = portal_store.enqueue(submission_id, "question", {"question": question})
    portal_store.mark_applied(row)
    return row


def _set_needs_input(submission_id: str) -> None:
    from coord import portal_store

    row = portal_store.enqueue(submission_id, "status", {"status": "needs-input"})
    portal_store.mark_applied(row)


class TestPortalNeedsInputAPI:
    def test_lists_needs_input_submissions_with_open_question_and_revision(
        self, rw_db
    ) -> None:
        _set_needs_input("sub_1")
        q = _push_question("sub_1", "Offline-first?")

        client = _client()
        r = client.get("/api/portal/needs-input")

        assert r.status_code == 200
        assert r.json() == {
            "submissions": [
                {
                    "submission_id": "sub_1",
                    "question_revision": q.revision,
                    "question": "Offline-first?",
                }
            ]
        }

    def test_excludes_submissions_not_in_needs_input(self, rw_db) -> None:
        _push_question("sub_1", "Offline-first?")  # status never set to needs-input

        client = _client()
        r = client.get("/api/portal/needs-input")

        assert r.status_code == 200
        assert r.json() == {"submissions": []}

    def test_excludes_needs_input_submission_with_no_open_question(
        self, rw_db
    ) -> None:
        _set_needs_input("sub_1")  # no question ever pushed

        client = _client()
        r = client.get("/api/portal/needs-input")

        assert r.status_code == 200
        assert r.json() == {"submissions": []}

    def test_excludes_submission_whose_only_question_is_already_answered(
        self, rw_db
    ) -> None:
        from coord import portal_store

        _set_needs_input("sub_1")
        q = _push_question("sub_1", "Offline-first?")
        portal_store.answer_question("sub_1", "Yes.", revision=q.revision)

        client = _client()
        r = client.get("/api/portal/needs-input")

        assert r.status_code == 200
        assert r.json() == {"submissions": []}


class TestPortalAnswerAPI:
    def test_records_answer_paired_to_revision_and_submission_leaves_needs_input(
        self, rw_db, monkeypatch
    ) -> None:
        """#2990 acceptance: one store call (#2986's `answer_question`), and
        the submission leaves needs-input exactly as the CLI path does."""
        from coord import portal_store, portal_sync, state

        _set_needs_input("sub_1")
        q = _push_question("sub_1", "Offline-first?")
        state._save_portal_link_local(
            {"repo_name": "api", "milestone_number": 3, "submission_id": "sub_1"}
        )

        def _fake_fold(config, repo_name, milestone_number, **kw):
            # Simulate the real fold nudge resolving the submission off
            # needs-input, the same effect `fold_status_for_milestone`
            # would have — via the same outbox mechanism, not a shortcut.
            row = portal_store.enqueue("sub_1", "status", {"status": "resolved"})
            portal_store.mark_applied(row)

        monkeypatch.setattr(portal_sync, "fold_status_for_milestone", _fake_fold)

        client = _client()
        r = client.post(
            "/api/portal/answer",
            json={
                "submission_id": "sub_1",
                "text": "Yes, offline-first.",
                "source": "phone",
                "revision": q.revision,
            },
        )

        assert r.status_code == 200, r.text
        entry = r.json()["entry"]
        assert entry["submission_id"] == "sub_1"
        assert entry["question_revision"] == q.revision
        assert entry["text"] == "Yes, offline-first."
        assert entry["kind"] == "question_answered"
        import json as _json

        assert _json.loads(entry["payload_json"]) == {
            "relayed": True,
            "source": "phone",
        }

        [stored] = [
            e for e in portal_store.ledger_for_submission("sub_1")
            if e.kind == portal_store.LEDGER_KIND_QUESTION_ANSWERED
        ]
        assert stored.question_revision == q.revision
        assert stored.text == "Yes, offline-first."

        assert portal_store.get_submission("sub_1").last_status != "needs-input"

    def test_rejects_a_revision_that_is_not_the_current_open_question(
        self, rw_db
    ) -> None:
        from coord import portal_store

        _set_needs_input("sub_1")
        q = _push_question("sub_1", "Offline-first?")
        stale_revision = q.revision - 1 if q.revision else 999

        client = _client()
        r = client.post(
            "/api/portal/answer",
            json={
                "submission_id": "sub_1",
                "text": "Yes.",
                "source": "verbal",
                "revision": stale_revision,
            },
        )

        assert r.status_code == 409
        assert "current open question" in r.json()["error"]
        answered = [
            e for e in portal_store.ledger_for_submission("sub_1")
            if e.kind == portal_store.LEDGER_KIND_QUESTION_ANSWERED
        ]
        assert answered == []

    def test_repeated_post_of_the_same_answer_converges(self, rw_db) -> None:
        """A browser client retrying on a flaky phone connection must not
        double-record the same answer."""
        from coord import portal_store

        _set_needs_input("sub_1")
        q = _push_question("sub_1", "Offline-first?")

        client = _client()
        body = {
            "submission_id": "sub_1",
            "text": "Yes, offline-first.",
            "source": "verbal",
            "revision": q.revision,
        }
        r1 = client.post("/api/portal/answer", json=body)
        r2 = client.post("/api/portal/answer", json=body)

        assert r1.status_code == 200, r1.text
        assert r2.status_code == 200, r2.text
        assert r1.json()["entry"]["id"] == r2.json()["entry"]["id"]

        answered = [
            e for e in portal_store.ledger_for_submission("sub_1")
            if e.kind == portal_store.LEDGER_KIND_QUESTION_ANSWERED
        ]
        assert len(answered) == 1

    @pytest.mark.parametrize(
        "missing_field",
        ["submission_id", "text", "source", "revision"],
    )
    def test_missing_required_field_is_4xx(self, rw_db, missing_field) -> None:
        _set_needs_input("sub_1")
        q = _push_question("sub_1", "Offline-first?")
        body = {
            "submission_id": "sub_1",
            "text": "Yes.",
            "source": "verbal",
            "revision": q.revision,
        }
        del body[missing_field]

        client = _client()
        r = client.post("/api/portal/answer", json=body)

        assert 400 <= r.status_code < 500

    def test_unknown_submission_is_404(self, rw_db) -> None:
        client = _client()
        r = client.post(
            "/api/portal/answer",
            json={
                "submission_id": "sub_nope",
                "text": "Yes.",
                "source": "verbal",
                "revision": 1,
            },
        )
        assert r.status_code == 404

    def test_unknown_source_is_rejected(self, rw_db) -> None:
        _set_needs_input("sub_1")
        q = _push_question("sub_1", "Offline-first?")

        client = _client()
        r = client.post(
            "/api/portal/answer",
            json={
                "submission_id": "sub_1",
                "text": "Yes.",
                "source": "carrier-pigeon",
                "revision": q.revision,
            },
        )

        assert r.status_code == 400
