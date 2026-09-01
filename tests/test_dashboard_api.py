"""Tests for the dashboard's #2990 exposure of #2986's `coord portal answer`
write path: `GET /api/portal/needs-input` + `POST /api/portal/answer`.

Uses the same ``rw_db`` (thread-safe, file-backed sqlite) pattern as
``tests/test_dashboard.py`` — ``TestClient`` runs the async handler on a
worker thread, which the autouse ``coord_db`` in-memory connection
(``tests/conftest.py``) can't touch.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from coord.config import Config
from coord.dashboard.server import build_app
from coord.models import Assignment, Board, Machine, Repo


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


class TestPortalThinClientRouting:
    """#2990 review fix round: on a thin-client dashboard host
    (``board_service`` configured — a ``coord web`` process running
    anywhere other than the daemon host, which the architecture explicitly
    supports), both endpoints must route through the daemon instead of
    reading/writing this process's own local ``coord.db`` directly. Before
    this fix, ``api_portal_needs_input``/``api_portal_answer`` called
    ``portal_store.list_submissions()``/``get_submission()``/
    ``ledger_for_submission()``/``_current_open_question_revision()``
    unconditionally — silently wrong off the daemon host per
    ``coord/portal_store.py``'s own module docstring. This is the scenario
    the review flagged as completely untested: every other test in this
    file builds a ``Config`` with no ``board_service`` set and reads/writes
    the same local ``rw_db``.
    """

    def test_needs_input_routes_to_the_daemon_instead_of_the_local_db(
        self, rw_db, monkeypatch
    ) -> None:
        import coord.client as cc

        # Sits in THIS (thin client's) own local DB — must NOT show up in
        # the response, proving the handler isn't reading it.
        _set_needs_input("local_only_sub")
        _push_question("local_only_sub", "Local question?")

        monkeypatch.setattr(
            cc, "resolve_board_service",
            lambda *a, **k: cc.ServiceConfig("http://daemon:7435"),
        )

        def fake_get(url, **kw):
            assert url == "http://daemon:7435/portal-needs-input"

            class _Resp:
                status_code = 200

                def raise_for_status(self) -> None:
                    return None

                def json(self):
                    return {
                        "submissions": [
                            {
                                "submission_id": "remote_sub",
                                "question_revision": 3,
                                "question": "Remote question?",
                            }
                        ]
                    }

            return _Resp()

        monkeypatch.setattr(cc.httpx, "get", fake_get)

        client = _client()
        r = client.get("/api/portal/needs-input")

        assert r.status_code == 200
        assert r.json() == {
            "submissions": [
                {
                    "submission_id": "remote_sub",
                    "question_revision": 3,
                    "question": "Remote question?",
                }
            ]
        }

    def test_answer_routes_preflight_checks_and_the_write_to_the_daemon(
        self, rw_db, monkeypatch
    ) -> None:
        """The 404/409/idempotency gating and the actual write must all
        agree with the DAEMON's data, not this thin client's empty local
        DB — `remote_sub` exists only on the (mocked) daemon side."""
        import coord.client as cc
        from coord import portal_store

        monkeypatch.setattr(
            cc, "resolve_board_service",
            lambda *a, **k: cc.ServiceConfig("http://daemon:7435"),
        )

        def fake_get(url, **kw):
            assert url == "http://daemon:7435/portal-answer-preflight"
            assert kw["params"] == {"submission_id": "remote_sub"}

            class _Resp:
                status_code = 200

                def raise_for_status(self) -> None:
                    return None

                def json(self):
                    return {
                        "preflight": {
                            "current_open_revision": 5,
                            "relayed_answers": [],
                        }
                    }

            return _Resp()

        posted = []

        def fake_post(url, *, json=None, **kw):
            posted.append((url, json))

            class _Resp:
                status_code = 200

                def raise_for_status(self) -> None:
                    return None

                def json(self):
                    return {
                        "entry": {
                            "id": 1,
                            "submission_id": "remote_sub",
                            "seq": 1,
                            "kind": "question_answered",
                            "question_revision": 5,
                            "text": "Yes, offline-first.",
                            "actor": "operator:jane",
                            "source_event_id": None,
                            "payload_json": (
                                '{"relayed": true, "source": "phone"}'
                            ),
                            "recorded_at": 100.0,
                        }
                    }

            return _Resp()

        monkeypatch.setattr(cc.httpx, "get", fake_get)
        monkeypatch.setattr(cc.httpx, "post", fake_post)

        client = _client()
        r = client.post(
            "/api/portal/answer",
            json={
                "submission_id": "remote_sub",
                "text": "Yes, offline-first.",
                "source": "phone",
                "revision": 5,
            },
        )

        assert r.status_code == 200, r.text
        entry = r.json()["entry"]
        assert entry["submission_id"] == "remote_sub"
        assert entry["question_revision"] == 5
        assert posted == [
            (
                "http://daemon:7435/portal-answer",
                {
                    "submission_id": "remote_sub",
                    "text": "Yes, offline-first.",
                    "source": "phone",
                    "revision": 5,
                    "actor": "",
                },
            )
        ]
        # Nothing was written to this (thin client's) own local DB — the
        # submission doesn't even exist there.
        assert portal_store.get_submission("remote_sub") is None
        assert portal_store.ledger_for_submission("remote_sub") == []

    def test_answer_404s_on_a_submission_the_daemon_does_not_know_either(
        self, rw_db, monkeypatch
    ) -> None:
        """A submission that happens to exist in THIS thin client's own
        local DB must not paper over a daemon-side 404 — the daemon's
        answer is authoritative, never the local one."""
        import coord.client as cc

        # Exists locally, but the (mocked) daemon has never heard of it.
        _set_needs_input("local_sub")
        _push_question("local_sub", "Local question?")

        monkeypatch.setattr(
            cc, "resolve_board_service",
            lambda *a, **k: cc.ServiceConfig("http://daemon:7435"),
        )

        def fake_get(url, **kw):
            class _Resp:
                status_code = 404

            return _Resp()

        monkeypatch.setattr(cc.httpx, "get", fake_get)

        client = _client()
        r = client.post(
            "/api/portal/answer",
            json={
                "submission_id": "local_sub",
                "text": "Yes.",
                "source": "verbal",
                "revision": 1,
            },
        )

        assert r.status_code == 404


class TestMachinesAPI:
    """#3023: GET /api/machines serves the daemon's already-refreshed
    machine state — never a synchronous per-request fan-out probe of the
    fleet (the old shape: ``check_all`` against every machine's
    ``/health``, then ``fetch_status`` against every one that answered).
    """

    def _health_row(self, **overrides) -> dict:
        now = time.time()
        row = {
            "state": "online",
            "reason": "",
            "latency_ms": 7.5,
            "received_at": now,
            "health": {
                "schema": 1,
                "checked_at": now,
                "results": [],
                "worktree_bytes": 4096,
                "agent_runtime_version": "0.42.0",
            },
        }
        row.update(overrides)
        return row

    def test_served_shape_carries_daemon_refreshed_fields(self) -> None:
        """reachability, state/reason, latency, agent version, worktree
        bytes — all sourced from the tick-refreshed health snapshot, not a
        live probe of the agent."""
        client = _client()
        with (
            patch(
                "coord.state.load_machine_health",
                return_value={"laptop": self._health_row()},
            ),
            patch("coord.dashboard.server.read_board", return_value=Board()),
        ):
            r = client.get("/api/machines")

        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        m = data[0]
        assert m["name"] == "laptop"
        assert m["host"] == "laptop.tailnet"
        assert m["repos"] == ["api"]
        assert m["state"] == "online"
        assert m["reason"] == ""
        assert m["latency_ms"] == 7.5
        assert m["agent_version"] == "0.42.0"
        assert m["worktree_bytes"] == 4096

    def test_never_polled_machine_reads_unknown_not_absent(self) -> None:
        client = _client()
        with (
            patch("coord.state.load_machine_health", return_value={}),
            patch("coord.dashboard.server.read_board", return_value=Board()),
        ):
            r = client.get("/api/machines")

        assert r.status_code == 200
        data = r.json()
        assert data[0]["state"] == "unknown"

    def test_performs_no_per_request_fleet_probe(self) -> None:
        """The whole point of #3023: assert on the ABSENCE of the fan-out
        probe call, not on wall-clock timing (a slow mock could satisfy a
        timing assertion while still being the wrong architecture).
        """
        client = _client()
        with (
            patch("coord.network.check_all") as mock_check_all,
            patch("coord.network.check_machine") as mock_check_machine,
            patch("coord.network.fetch_status") as mock_fetch_status,
            patch("coord.state.load_machine_health", return_value={}),
            patch("coord.dashboard.server.read_board", return_value=Board()),
        ):
            r = client.get("/api/machines")

        assert r.status_code == 200
        mock_check_all.assert_not_called()
        mock_check_machine.assert_not_called()
        mock_fetch_status.assert_not_called()

    def test_busy_machine_carries_its_active_assignment_from_the_board(self) -> None:
        """The legacy dashboard's per-machine 'busy' card used to come from
        a live per-agent GET /status probe; it now comes from the same
        board this dashboard already reads for every other panel."""
        board = Board(active=[
            Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=42, issue_title="Fix auth",
                assignment_id="abc123", status="running",
            ),
        ])
        client = _client()
        with (
            patch("coord.state.load_machine_health", return_value={}),
            patch("coord.dashboard.server.read_board", return_value=board),
        ):
            r = client.get("/api/machines")

        assert r.status_code == 200
        active = r.json()[0]["assignments"]["active"]
        assert len(active) == 1
        assert active[0]["spec"]["issue_number"] == 42
        assert active[0]["spec"]["issue_title"] == "Fix auth"

    def test_idle_machine_carries_no_assignments_key(self) -> None:
        client = _client()
        with (
            patch("coord.state.load_machine_health", return_value={}),
            patch("coord.dashboard.server.read_board", return_value=Board()),
        ):
            r = client.get("/api/machines")

        assert "assignments" not in r.json()[0]

    def test_thin_client_reads_the_daemons_published_fleet_health_block(
        self, monkeypatch
    ) -> None:
        """``board_service`` configured -> the raw ``/board`` payload's
        ``fleet_health.machine_health`` sibling key, not a local DB read and
        not a fresh probe of the fleet."""
        import coord.client as cc

        monkeypatch.setattr(
            cc, "resolve_board_service",
            lambda *a, **k: cc.ServiceConfig("http://daemon:7435"),
        )

        now = time.time()
        payload = {
            "assignments": [],
            "plans": {},
            "round_number": 0,
            "fleet_health": {
                "schema": 1,
                "refreshed_at": now,
                "truncated": False,
                "machine_health": [
                    {
                        "machine": "laptop",
                        "state": "online",
                        "reason": "",
                        "latency_ms": 3.2,
                        "received_at": now,
                        "stale": False,
                        "severity": "ok",
                        "checked_at": now,
                        "results": [],
                        "worktree_bytes": 999,
                        "agent_runtime_version": "1.0.0",
                    },
                ],
                "fleet_checks": [],
            },
        }

        def fake_get(url, **kw):
            class _Resp:
                status_code = 200

                def raise_for_status(self):
                    return None

                def json(self):
                    return payload

            return _Resp()

        monkeypatch.setattr(cc.httpx, "get", fake_get)

        with patch("coord.network.check_all") as mock_check_all:
            client = _client()
            r = client.get("/api/machines")

        assert r.status_code == 200
        mock_check_all.assert_not_called()
        data = r.json()
        assert data[0]["worktree_bytes"] == 999
        assert data[0]["agent_version"] == "1.0.0"

    def test_legacy_dashboard_still_consumes_the_response(self) -> None:
        """Compatibility check (this issue's explicit ask):
        ``coord/dashboard/index.html``'s ``loadMachines()`` reads
        ``m.state``, ``m.latency_ms``, ``m.name``, ``m.host``, ``m.repos``,
        and ``m.assignments.active[0].spec.{issue_number,issue_title}`` —
        every key it dereferences must still be present in the served
        shape.
        """
        board = Board(active=[
            Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=7, issue_title="Add logging",
                assignment_id="def456", status="running",
            ),
        ])
        client = _client()

        index_html = client.get("/").text
        assert "loadMachines" in index_html
        assert "m.assignments.active" in index_html
        assert "m.repos" in index_html

        with (
            patch(
                "coord.state.load_machine_health",
                return_value={"laptop": self._health_row()},
            ),
            patch("coord.dashboard.server.read_board", return_value=board),
        ):
            r = client.get("/api/machines")

        m = r.json()[0]
        for key in ("name", "host", "repos", "state", "latency_ms"):
            assert key in m
        assert m["assignments"]["active"][0]["spec"]["issue_number"] == 7
        assert m["assignments"]["active"][0]["spec"]["issue_title"] == "Add logging"
