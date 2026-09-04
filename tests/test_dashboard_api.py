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

from coord.config import AcceptanceConfig, AcceptanceDriverConfig, Config
from coord.dashboard.server import build_app, openapi_spec
from coord.models import Assignment, Board, Machine, Repo
from coord.openapi import validate_json_schema


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

    def test_non_running_assignment_excluded_from_busy_card(self) -> None:
        """``_active_assignments_by_machine`` must filter to ``status ==
        "running"`` exactly like ``Board.idle_machines()`` and
        ``Board.active_files_by_repo()`` do — an unfiltered read of
        ``board.active`` would be a second, independent answer to "is this
        machine busy" that could diverge from those two (#2096 "one
        question, one answer")."""
        board = Board(active=[
            Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=99, issue_title="Not actually running",
                assignment_id="pending1", status="pending",
            ),
        ])
        client = _client()
        with (
            patch("coord.state.load_machine_health", return_value={}),
            patch("coord.dashboard.server.read_board", return_value=board),
        ):
            r = client.get("/api/machines")

        assert "assignments" not in r.json()[0]

    def test_thin_client_reads_the_daemons_published_fleet_health_block(
        self, monkeypatch
    ) -> None:
        """``board_service`` configured -> the raw ``/board`` payload's
        ``fleet_health.machine_health`` sibling key, not a local DB read and
        not a fresh probe of the fleet.

        Also asserts the payload is fetched exactly ONCE (#3023 review): the
        health rows and the board's active assignments both come from the
        same ``/board`` response, not two independent daemon round trips."""
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

        calls = []

        def fake_get(url, **kw):
            calls.append(url)

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

        assert len(calls) == 1, (
            f"expected exactly one /board round trip, got {len(calls)}: {calls}"
        )

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

        Also asserts the (documented, #3023 review) DROPPED half of the old
        shape: ``a.progress`` (the live per-worker ``STATUS:``/``STUCK:``
        log tail) is gone from BOTH sides — the server no longer serves it,
        and ``index.html`` no longer dereferences it — so this isn't a
        silent, undiscovered loss the way the reviewer flagged the first
        version of this PR for.
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
        # Dead branches removed, not just left dangling — a comment
        # referencing `a.progress` for context is fine, a live
        # `a.progress.updates`/`a.progress.stuck` dereference is not.
        assert "a.progress." not in index_html

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
        assert "progress" not in m["assignments"]["active"][0]


class TestMachinesStatsAPI:
    """#3025: GET /api/machines/stats -- per-machine work stats derived
    purely from the board (no new probe, no agent contact): active workers
    vs configured concurrency, completed/failed counts, and recent job
    history.
    """

    def _config(self, machines: list[Machine]) -> Config:
        return Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=machines,
        )

    def _client(self, machines: list[Machine]) -> TestClient:
        return TestClient(build_app(self._config(machines)))

    def test_machine_with_zero_jobs_reads_empty_stats(self) -> None:
        """A machine with nothing on the board at all -- fresh install, or
        everything aged out of the retention window -- reads zero counts
        and an empty history, never an error."""
        machines = [Machine(name="idle", host="idle.tailnet", repos=["api"])]
        client = self._client(machines)
        with patch("coord.dashboard.server.read_board", return_value=Board()):
            r = client.get("/api/machines/stats")

        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        m = data[0]
        assert m["name"] == "idle"
        assert m["capacity"] == {"active": 0, "max": 2}  # default concurrency.max_workers
        assert m["counts"] == {"completed": 0, "failed": 0}
        assert m["job_history"] == []

    def test_machine_at_its_concurrency_ceiling(self) -> None:
        """capacity.active reflects only RUNNING assignments (mirrors
        `coord.reconcile._running_by_machine`/`Board.idle_machines`), and
        capacity.max honours a per-machine `max_workers` override over the
        fleet-wide default (`coord.reconcile._machine_capacity`)."""
        machines = [
            Machine(name="laptop", host="laptop.tailnet", repos=["api"], max_workers=1),
        ]
        board = Board(active=[
            Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=1, issue_title="At capacity",
                assignment_id="running1", status="running",
            ),
            Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=2, issue_title="Not yet dispatched",
                assignment_id="pending1", status="pending",
            ),
        ])
        client = self._client(machines)
        with patch("coord.dashboard.server.read_board", return_value=board):
            r = client.get("/api/machines/stats")

        m = r.json()[0]
        assert m["capacity"] == {"active": 1, "max": 1}

    def test_completed_and_failed_counts_and_job_history_shape(self) -> None:
        now = time.time()
        board = Board(completed=[
            Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=10, issue_title="Older done",
                assignment_id="done_old", status="done",
                dispatched_at=now - 200, finished_at=now - 100,
            ),
            Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=11, issue_title="Newer failure",
                assignment_id="fail_new", status="failed",
                dispatched_at=now - 50, finished_at=now - 10,
            ),
            Assignment(
                machine_name="other", repo_name="api",
                issue_number=12, issue_title="Different machine",
                assignment_id="other_done", status="done",
                dispatched_at=now - 30, finished_at=now - 20,
            ),
        ])
        machines = [
            Machine(name="laptop", host="laptop.tailnet", repos=["api"]),
            Machine(name="other", host="other.tailnet", repos=["api"]),
        ]
        client = self._client(machines)
        with patch("coord.dashboard.server.read_board", return_value=board):
            r = client.get("/api/machines/stats")

        by_name = {m["name"]: m for m in r.json()}
        laptop = by_name["laptop"]
        assert laptop["counts"] == {"completed": 1, "failed": 1}
        # Newest (by finished_at) first.
        assert [j["assignment_id"] for j in laptop["job_history"]] == [
            "fail_new", "done_old",
        ]
        entry = laptop["job_history"][0]
        assert entry["issue_number"] == 11
        assert entry["issue_title"] == "Newer failure"
        assert entry["status"] == "failed"
        assert entry["repo_name"] == "api"
        assert entry["dispatched_at"] == now - 50
        assert entry["finished_at"] == now - 10

        other = by_name["other"]
        assert other["counts"] == {"completed": 1, "failed": 0}
        assert [j["assignment_id"] for j in other["job_history"]] == ["other_done"]

    def test_merged_status_counts_as_completed(self) -> None:
        """`coord.state.mark_assignment_merged` flips a done work assignment
        to `status="merged"` once GitHub confirms the merge -- that is the
        normal steady state for a successfully completed assignment, not a
        distinct outcome, so it must bucket into `counts.completed` the same
        way `coord.scorecard` treats it as the success signal (not silently
        fall into neither bucket like advisory/cancelled/refused_policy)."""
        now = time.time()
        board = Board(completed=[
            Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=30, issue_title="Merge-confirmed",
                assignment_id="merged1", status="merged",
                dispatched_at=now - 100, finished_at=now - 90,
            ),
            Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=31, issue_title="Still just done",
                assignment_id="done1", status="done",
                dispatched_at=now - 50, finished_at=now - 40,
            ),
        ])
        machines = [Machine(name="laptop", host="laptop.tailnet", repos=["api"])]
        client = self._client(machines)
        with patch("coord.dashboard.server.read_board", return_value=board):
            r = client.get("/api/machines/stats")

        m = r.json()[0]
        assert m["counts"] == {"completed": 2, "failed": 0}
        assert {j["assignment_id"] for j in m["job_history"]} == {"merged1", "done1"}
        # job_history preserves the raw status, unlike the collapsed count.
        history_by_id = {j["assignment_id"]: j for j in m["job_history"]}
        assert history_by_id["merged1"]["status"] == "merged"

    def test_advisory_and_cancelled_appear_in_history_but_not_in_counts(self) -> None:
        """#448/#2234: advisory/cancelled/refused_policy are neither a clean
        success nor a failure -- they still show up in job_history (it is a
        raw recent-activity feed) but must not inflate either count."""
        now = time.time()
        board = Board(completed=[
            Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=20, issue_title="Zero-commit clean exit",
                assignment_id="adv1", status="advisory",
                dispatched_at=now - 10, finished_at=now - 5,
            ),
            Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=21, issue_title="Cancelled mid-run",
                assignment_id="cancel1", status="cancelled",
                dispatched_at=now - 20, finished_at=now - 15,
            ),
        ])
        machines = [Machine(name="laptop", host="laptop.tailnet", repos=["api"])]
        client = self._client(machines)
        with patch("coord.dashboard.server.read_board", return_value=board):
            r = client.get("/api/machines/stats")

        m = r.json()[0]
        assert m["counts"] == {"completed": 0, "failed": 0}
        assert {j["assignment_id"] for j in m["job_history"]} == {"adv1", "cancel1"}

    def test_job_history_capped_at_most_recent_20(self) -> None:
        now = time.time()
        completed = [
            Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=i, issue_title=f"Job {i}",
                assignment_id=f"job{i}", status="done",
                dispatched_at=now - (100 - i), finished_at=now - (100 - i),
            )
            for i in range(25)
        ]
        board = Board(completed=completed)
        machines = [Machine(name="laptop", host="laptop.tailnet", repos=["api"])]
        client = self._client(machines)
        with patch("coord.dashboard.server.read_board", return_value=board):
            r = client.get("/api/machines/stats")

        m = r.json()[0]
        assert len(m["job_history"]) == 20
        # Newest finished_at first: job24 finished most recently.
        assert m["job_history"][0]["assignment_id"] == "job24"
        assert m["job_history"][-1]["assignment_id"] == "job5"
        assert m["counts"]["completed"] == 25  # counts are NOT capped like history


class TestMachinesHealthAPI:
    """#3024: GET /api/machines/health -- the fleet-wide `FleetHealthSnapshot`
    (severity/stale/headroom detail per machine, plus fleet-scope checks)
    that `GET /api/machines` deliberately keeps out of its own response.
    """

    def _health_row(self, *, received_at=None, results=None, **overrides) -> dict:
        now = time.time()
        row = {
            "state": "online",
            "reason": "",
            "latency_ms": 7.5,
            "received_at": now if received_at is None else received_at,
            "health": {
                "schema": 1,
                "checked_at": now,
                "severity": "ok",
                "results": results if results is not None else [
                    {"check_id": "disk", "severity": "ok", "headroom": "42% free"},
                ],
                "worktree_bytes": 4096,
                "agent_runtime_version": "0.42.0",
            },
        }
        row.update(overrides)
        return row

    def test_local_mode_passes_severity_and_results_through_verbatim(self) -> None:
        """No `board_service` configured -> the same local-DB reassembly
        `coord status` uses (`coord.health.aggregate.local_fleet_health_block`),
        with every per-check field (`severity`, `results`/`headroom`) intact
        -- never re-derived or collapsed at this layer."""
        client = _client()
        with patch(
            "coord.state.load_machine_health",
            return_value={"laptop": self._health_row()},
        ):
            r = client.get("/api/machines/health")

        assert r.status_code == 200
        data = r.json()
        assert data["fleet_checks"] == []
        rows = {row["machine"]: row for row in data["machine_health"]}
        laptop = rows["laptop"]
        assert laptop["severity"] == "ok"
        assert laptop["stale"] is False
        assert laptop["results"] == [
            {"check_id": "disk", "severity": "ok", "headroom": "42% free"},
        ]

    def test_stale_machine_reads_unknown_but_retains_last_known_results(
        self,
    ) -> None:
        """#1630's honesty contract: a machine whose last poll is older than
        `STALE_AFTER_SECONDS` must report `severity="unknown"` and
        `stale=True` -- NEVER a carried-forward `ok` -- while its last-known
        `results`/`checked_at` are still served, so a renderer can tell
        "OK" apart from "last measured OK, a while ago"."""
        from coord.health.fleet_snapshot import STALE_AFTER_SECONDS

        long_ago = time.time() - STALE_AFTER_SECONDS - 100
        stale_results = [
            {"check_id": "disk", "severity": "ok", "headroom": "42% free"},
        ]
        client = _client()
        with patch(
            "coord.state.load_machine_health",
            return_value={
                "laptop": self._health_row(
                    received_at=long_ago, results=stale_results
                ),
            },
        ):
            r = client.get("/api/machines/health")

        assert r.status_code == 200
        laptop = {row["machine"]: row for row in r.json()["machine_health"]}["laptop"]
        assert laptop["severity"] == "unknown"
        assert laptop["stale"] is True
        # Last-known detail is retained, not dropped just because the
        # severity above it was downgraded for staleness.
        assert laptop["results"] == stale_results

    def test_never_polled_machine_reads_unknown_not_absent(self) -> None:
        client = _client()
        with patch("coord.state.load_machine_health", return_value={}):
            r = client.get("/api/machines/health")

        assert r.status_code == 200
        data = r.json()
        assert len(data["machine_health"]) == 1
        assert data["machine_health"][0]["machine"] == "laptop"
        assert data["machine_health"][0]["severity"] == "unknown"

    def test_thin_client_forwards_the_daemons_fleet_health_block_verbatim(
        self, monkeypatch
    ) -> None:
        """`board_service` configured -> the daemon's own `GET /board`
        `fleet_health` key, forwarded unexamined -- including `fleet_checks`,
        which the local-mode path can never populate (#3024)."""
        import coord.client as cc

        monkeypatch.setattr(
            cc, "resolve_board_service",
            lambda *a, **k: cc.ServiceConfig("http://daemon:7435"),
        )

        now = time.time()
        fleet_health = {
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
                    "severity": "warn",
                    "checked_at": now,
                    "results": [
                        {"check_id": "disk", "severity": "warn", "headroom": "8% free"},
                    ],
                    "worktree_bytes": 999,
                    "agent_runtime_version": "1.0.0",
                },
            ],
            "fleet_checks": [
                {"check_id": "fleet_board_latency", "severity": "ok"},
            ],
        }
        payload = {
            "assignments": [], "plans": {}, "round_number": 0,
            "fleet_health": fleet_health,
        }

        calls = []

        def fake_get(url, **kw):
            calls.append(url)

            class _Resp:
                status_code = 200

                def raise_for_status(self):
                    return None

                def json(self):
                    return payload

            return _Resp()

        monkeypatch.setattr(cc.httpx, "get", fake_get)

        client = _client()
        r = client.get("/api/machines/health")

        assert len(calls) == 1
        assert r.status_code == 200
        assert r.json() == fleet_health

    def test_unreachable_daemon_degrades_to_an_explicit_error(
        self, monkeypatch
    ) -> None:
        """A daemon that can't be reached must come back as an explicit
        error -- NEVER a 200 with a stale/empty block a renderer could
        mistake for "fleet quiet, all healthy" (mirrors #3022's
        `/api/machines/metrics` degradation)."""
        import httpx

        import coord.client as cc

        monkeypatch.setattr(
            cc, "resolve_board_service",
            lambda *a, **k: cc.ServiceConfig("http://daemon:7435"),
        )

        def raising_get(url, **kw):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(cc.httpx, "get", raising_get)

        client = _client()
        r = client.get("/api/machines/health")

        assert r.status_code == 503
        body = r.json()
        assert body["reachable"] is False
        assert "error" in body


class TestMachineMetricsAPI:
    """``GET /api/machines/metrics`` — the dashboard's proxy of #3021's daemon
    endpoint (#3022). ``coord web``'s own origin is what the browser (and
    the phone webapp's Machines panel) actually talks to; this handler
    resolves where the REAL data lives and forwards there.

    The autouse fixture in ``tests/conftest.py`` (``COORD_SERVICE_URL``/
    ``COORD_TOKEN`` deleted, ``CLIENT_TOML`` pointed at a nonexistent path)
    means ``coord.client.resolve_board_service()`` returns ``None`` by
    default in every test here unless a test monkeypatches it itself — i.e.
    the *daemon-local* (loopback) branch is what an unmodified test hits.
    """

    _PAYLOAD = {
        "schema": 1,
        "generated_at": 1234.5,
        "since": None,
        "resolution": None,
        "machines": {"laptop": [{"timestamp": 1234.5, "cpu_percent": 12.0}]},
    }

    @staticmethod
    def _fake_get(payload, calls, *, status_code=200):
        def fake_get(url, **kw):
            calls.append((url, kw.get("params"), kw.get("headers")))

            class _Resp:
                def __init__(self):
                    self.status_code = status_code

                def raise_for_status(self):
                    if self.status_code >= 400:
                        import httpx

                        raise httpx.HTTPStatusError(
                            "boom", request=None, response=self
                        )

                def json(self):
                    return payload

            return _Resp()

        return fake_get

    def test_daemon_remote_resolution_proxies_to_the_configured_board_service(
        self, monkeypatch
    ) -> None:
        """``board_service`` configured (thin-client dashboard) -> the request
        goes to THAT daemon's URL over Tailscale, not a local loopback."""
        import coord.client as cc

        monkeypatch.setattr(
            cc, "resolve_board_service",
            lambda *a, **k: cc.ServiceConfig("http://daemon:7435"),
        )
        calls = []
        monkeypatch.setattr(cc.httpx, "get", self._fake_get(self._PAYLOAD, calls))

        client = _client()
        r = client.get("/api/machines/metrics")

        assert r.status_code == 200
        assert r.json() == self._PAYLOAD
        assert len(calls) == 1
        url, params, _headers = calls[0]
        assert url == "http://daemon:7435/machines/metrics"

    def test_daemon_local_resolution_hits_the_loopback_daemon_not_itself(
        self, monkeypatch
    ) -> None:
        """No ``board_service`` configured (dashboard co-located with the
        daemon host) -> a loopback call to the daemon's OWN port
        (``coord.serve_app.SERVE_PORT``, #3020/#3021's home), carrying its
        bearer-token convention -- never a recursive call back into this
        same dashboard process's own port."""
        import coord.client as cc
        from coord.serve_app import SERVE_PORT

        monkeypatch.setattr(
            "coord.serve_app.resolve_serve_token", lambda *a, **k: "secret-tok"
        )
        calls = []
        monkeypatch.setattr(cc.httpx, "get", self._fake_get(self._PAYLOAD, calls))

        client = _client()
        r = client.get("/api/machines/metrics")

        assert r.status_code == 200
        assert r.json() == self._PAYLOAD
        assert len(calls) == 1
        url, params, headers = calls[0]
        assert url == f"http://127.0.0.1:{SERVE_PORT}/machines/metrics"
        assert headers["Authorization"] == "Bearer secret-tok"

    def test_since_resolution_machine_query_params_are_forwarded_verbatim(
        self, monkeypatch
    ) -> None:
        import coord.client as cc

        calls = []
        monkeypatch.setattr(cc.httpx, "get", self._fake_get(self._PAYLOAD, calls))

        client = _client()
        r = client.get(
            "/api/machines/metrics",
            params={"since": "6h", "resolution": "50", "machine": "laptop"},
        )

        assert r.status_code == 200
        assert len(calls) == 1
        _url, params, _headers = calls[0]
        assert params == {"since": "6h", "resolution": "50", "machine": "laptop"}

    def test_unreachable_daemon_degrades_to_an_explicit_error_not_an_empty_series(
        self, monkeypatch
    ) -> None:
        """A daemon that can't be reached must come back as an explicit
        error the UI can render as "no data" -- NEVER a 200 with an empty
        (or missing) ``machines`` series, which would read as "fleet quiet,
        all healthy" rather than "we don't know"."""
        import httpx

        import coord.client as cc

        def raising_get(url, **kw):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(cc.httpx, "get", raising_get)

        client = _client()
        r = client.get("/api/machines/metrics")

        assert r.status_code == 503
        body = r.json()
        assert body["reachable"] is False
        assert "error" in body
        assert "machines" not in body

    def test_daemon_5xx_also_degrades_rather_than_forwarding_a_500(
        self, monkeypatch
    ) -> None:
        import coord.client as cc

        calls = []
        monkeypatch.setattr(
            cc.httpx, "get", self._fake_get({}, calls, status_code=500)
        )

        client = _client()
        r = client.get("/api/machines/metrics")

        assert r.status_code == 503
        assert r.json()["reachable"] is False

    def test_daemon_400_is_forwarded_as_a_caller_error_not_folded_into_unreachable(
        self, monkeypatch
    ) -> None:
        """A malformed ``since``/``resolution`` is the CALLER's bad input --
        the daemon said so via a 400 -- and must stay a 400, distinct from
        "the daemon didn't answer" (503)."""
        import coord.client as cc

        calls = []
        monkeypatch.setattr(
            cc.httpx,
            "get",
            self._fake_get({"error": "bad since='not-a-time'"}, calls, status_code=400),
        )

        client = _client()
        r = client.get("/api/machines/metrics", params={"since": "not-a-time"})

        assert r.status_code == 400
        assert "reachable" not in r.json()

    def test_fixture_mode_serves_the_seeded_series_never_the_live_daemon(
        self, monkeypatch
    ) -> None:
        """``coord web --fixture`` (#3026) never proxies to a live daemon --
        it runs the fixture's own seeded series through the same
        ``resolve_since``/``build_metrics_response`` pipeline instead.
        An unseeded fixture (no ``machine_metrics`` key) reads back as an
        empty ``machines`` dict -- the same "nothing sampled yet" shape the
        live sampler reports for a machine it has never polled -- rather
        than a canned unreachable error (that was the pre-#3026 placeholder,
        superseded now that the fixture has a real seeding path)."""
        import coord.client as cc
        from coord.dashboard.fixture import FixtureServer

        def _boom(*a, **k):
            raise AssertionError("fixture mode reached a live daemon")

        monkeypatch.setattr(cc, "fetch_machine_metrics", _boom)

        client = TestClient(build_app(_config(), fixture=FixtureServer()))

        r = client.get("/api/machines/metrics")

        assert r.status_code == 200
        assert r.json()["machines"] == {}

        seeded = FixtureServer(machine_metrics_raw={"laptop": [
            {"timestamp": 1234.5, "status": "ok", "cpu_percent": 12.0,
             "mem_percent": 30.0, "mem_used_mb": 100.0, "mem_total_mb": 400.0,
             "reason": ""},
        ]})
        client = TestClient(build_app(_config(), fixture=seeded))
        r = client.get("/api/machines/metrics")
        assert r.status_code == 200
        assert r.json()["machines"] == {"laptop": seeded.machine_metrics_raw["laptop"]}


class TestMachinesEndpointsMatchOpenApiSpec:
    """#3027: every ``/api/machines*`` endpoint (#3021-#3026) must now carry a
    real response schema in ``openapi_spec()``, not the bare ``{"200":
    {"description": "OK"}}`` stub ``/api/machines`` had carried until now.

    Mirrors ``tests/test_openapi.py``'s
    ``test_serve_openapi_board_schema_validates_golden_fixture`` pattern:
    exercise the REAL (non-fixture) handler with a richly populated payload
    -- assignments, multi-severity health results, metric samples, job
    history -- so every optional/nullable field the schema declares is
    actually hit at least once, then validate the served JSON against the
    schema the SAME ``openapi_spec()`` this repo's codegen reads declares
    for that path. A schema that silently drifted from what the handler
    actually returns fails right here.
    """

    def _schema_for(self, spec: dict, path: str, status: str = "200") -> dict:
        return spec["paths"][path]["get"]["responses"][status]["content"][
            "application/json"
        ]["schema"]

    def test_machines_response_matches_its_schema(self) -> None:
        board = Board(active=[
            Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=42, issue_title="Fix auth",
                assignment_id="abc123", status="running",
            ),
        ])
        health = {
            "laptop": {
                "state": "online", "reason": "", "latency_ms": 7.5,
                "received_at": time.time(),
                "health": {
                    "schema": 1, "checked_at": time.time(), "severity": "ok",
                    "results": [], "worktree_bytes": 4096,
                    "agent_runtime_version": "0.42.0",
                },
            },
        }
        client = _client()
        with (
            patch("coord.state.load_machine_health", return_value=health),
            patch("coord.dashboard.server.read_board", return_value=board),
        ):
            r = client.get("/api/machines")

        assert r.status_code == 200
        spec = openapi_spec()
        schema = self._schema_for(spec, "/api/machines")
        errors = validate_json_schema(r.json(), schema, spec["components"]["schemas"])
        assert errors == [], errors
        # A machine with no running work at all (the idle-machine case)
        # must validate too -- `assignments` absent entirely, not null.
        with (
            patch("coord.state.load_machine_health", return_value={}),
            patch("coord.dashboard.server.read_board", return_value=Board()),
        ):
            r2 = client.get("/api/machines")
        errors2 = validate_json_schema(r2.json(), schema, spec["components"]["schemas"])
        assert errors2 == [], errors2

    def test_machines_stats_response_matches_its_schema(self) -> None:
        now = time.time()
        board = Board(completed=[
            Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=10, issue_title="Older done",
                assignment_id="done_old", status="done",
                dispatched_at=now - 200, finished_at=now - 100,
            ),
            Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=11, issue_title="Newer failure",
                assignment_id="fail_new", status="failed",
                dispatched_at=now - 50, finished_at=now - 10,
            ),
        ])
        client = _client()
        with patch("coord.dashboard.server.read_board", return_value=board):
            r = client.get("/api/machines/stats")

        assert r.status_code == 200
        spec = openapi_spec()
        schema = self._schema_for(spec, "/api/machines/stats")
        errors = validate_json_schema(r.json(), schema, spec["components"]["schemas"])
        assert errors == [], errors

    def test_machines_health_response_matches_its_schema(self) -> None:
        now = time.time()
        full_result = {
            "key": "disk", "check_id": "disk", "scope": "machine",
            "subject": "/home", "title": "disk", "label": "disk /home",
            "severity": "warn", "headroom": "8% free",
            "threshold": "crit at 5%", "detail": "", "trend": None,
            "values": {"free_gb": 12}, "error": None,
        }
        health = {
            "laptop": {
                "state": "online", "reason": "", "latency_ms": 7.5,
                "received_at": now,
                "health": {
                    "schema": 1, "checked_at": now, "severity": "warn",
                    "results": [full_result], "worktree_bytes": 4096,
                    "agent_runtime_version": "0.42.0",
                },
            },
        }
        client = _client()
        with patch("coord.state.load_machine_health", return_value=health):
            r = client.get("/api/machines/health")

        assert r.status_code == 200
        spec = openapi_spec()
        schema = self._schema_for(spec, "/api/machines/health")
        errors = validate_json_schema(r.json(), schema, spec["components"]["schemas"])
        assert errors == [], errors

    def test_machines_health_unreachable_error_matches_its_schema(
        self, monkeypatch
    ) -> None:
        import httpx

        import coord.client as cc

        monkeypatch.setattr(
            cc, "resolve_board_service",
            lambda *a, **k: cc.ServiceConfig("http://daemon:7435"),
        )
        monkeypatch.setattr(
            cc.httpx, "get",
            lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("refused")),
        )

        client = _client()
        r = client.get("/api/machines/health")

        assert r.status_code == 503
        spec = openapi_spec()
        schema = self._schema_for(spec, "/api/machines/health", status="503")
        errors = validate_json_schema(r.json(), schema, spec["components"]["schemas"])
        assert errors == [], errors

    def test_machine_metrics_response_matches_its_schema(self, monkeypatch) -> None:
        import coord.client as cc

        payload = {
            "schema": 1, "generated_at": 1234.5, "since": None, "resolution": None,
            "machines": {
                "laptop": [
                    {"timestamp": 1234.5, "status": "ok", "cpu_percent": 12.0,
                     "mem_percent": 30.0, "mem_used_mb": 100.0,
                     "mem_total_mb": 400.0, "reason": ""},
                    {"timestamp": 1249.5, "status": "unknown", "cpu_percent": None,
                     "mem_percent": None, "mem_used_mb": None,
                     "mem_total_mb": None, "reason": "psutil not installed"},
                ],
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

        client = _client()
        r = client.get("/api/machines/metrics")

        assert r.status_code == 200
        spec = openapi_spec()
        schema = self._schema_for(spec, "/api/machines/metrics")
        errors = validate_json_schema(r.json(), schema, spec["components"]["schemas"])
        assert errors == [], errors

    def test_machine_metrics_bad_request_matches_its_schema(self, monkeypatch) -> None:
        """A malformed since/resolution is the DAEMON's 400, forwarded as-is
        (see ``test_daemon_400_is_forwarded_as_a_caller_error_not_folded_into_unreachable``
        above) -- so the daemon call itself has to be mocked to return one."""
        import coord.client as cc

        def fake_get(url, **kw):
            class _Resp:
                status_code = 400

                def raise_for_status(self):
                    import httpx

                    raise httpx.HTTPStatusError("boom", request=None, response=self)

                def json(self):
                    return {"error": "bad resolution='0': must be a positive integer"}

            return _Resp()

        monkeypatch.setattr(cc.httpx, "get", fake_get)

        client = _client()
        r = client.get("/api/machines/metrics", params={"resolution": "0"})

        assert r.status_code == 400
        spec = openapi_spec()
        schema = self._schema_for(spec, "/api/machines/metrics", status="400")
        errors = validate_json_schema(r.json(), schema, spec["components"]["schemas"])
        assert errors == [], errors

    def test_machine_metrics_unreachable_error_matches_its_schema(
        self, monkeypatch
    ) -> None:
        import httpx

        import coord.client as cc

        monkeypatch.setattr(
            cc.httpx, "get",
            lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("refused")),
        )

        client = _client()
        r = client.get("/api/machines/metrics")

        assert r.status_code == 503
        spec = openapi_spec()
        schema = self._schema_for(spec, "/api/machines/metrics", status="503")
        errors = validate_json_schema(r.json(), schema, spec["components"]["schemas"])
        assert errors == [], errors


# ── GET /api/gate-a/{repo}/{tracking_issue} (#3069) ─────────────────────────


def _gate_a_config(driver: AcceptanceDriverConfig | None = None) -> Config:
    drivers = {"portal": driver} if driver is not None else {}
    return Config(
        repos=[Repo(name="portal", github="acme/portal", default_branch="main")],
        machines=[Machine(name="laptop", host="laptop.tailnet", repos=["portal"])],
        acceptance=AcceptanceConfig(drivers=drivers),
    )


def _gate_a_client(driver: AcceptanceDriverConfig | None = None) -> TestClient:
    return TestClient(build_app(_gate_a_config(driver)))


def _fake_get_repo_file(files: dict[str, str]):
    def fn(repo, path, branch="develop"):
        if path in files:
            return files[path]
        raise RuntimeError(f"gh: 404 not found: {path}")

    return fn


def _fake_list_repo_dir(names_by_path: dict[str, list[str]]):
    def fn(repo, path, branch):
        return names_by_path.get(path, [])

    return fn


def _fake_repo_file_exists(files: dict[str, str]):
    def fn(repo, path, branch):
        return path in files

    return fn


def _issue_with_milestone(number: int, milestone_number: int, milestone_title: str):
    def fn(repo, n):
        return {
            "number": n,
            "title": f"tracking issue {n}",
            "milestone": {"number": milestone_number, "title": milestone_title},
        }

    return fn


class TestGateAPacketAPI:
    """#3069: `GET /api/gate-a/{repo}/{tracking_issue}` — the operator-facing
    Gate-A packet. Every test drives the real ``coord.gate_a.evaluate`` /
    ``coord.mock_author.collect_mock_bundle_files`` machinery through faked
    GitHub Contents API reads (``github_ops.get_issue`` /
    ``get_repo_file`` / ``list_repo_dir`` / ``repo_file_exists``) — never a
    re-implementation of the gate decision, mirroring how ``coord gate-a``
    itself reads.
    """

    def test_unknown_repo_returns_clean_404(self) -> None:
        client = _gate_a_client()
        r = client.get("/api/gate-a/nonexistent/1")
        assert r.status_code == 404
        assert "traceback" not in r.text.lower()

    def test_unknown_issue_returns_clean_404(self, monkeypatch) -> None:
        from coord import github_ops

        def _raise(repo, n):
            raise RuntimeError("gh: issue not found")

        monkeypatch.setattr(github_ops, "get_issue", _raise)

        client = _gate_a_client()
        r = client.get("/api/gate-a/portal/999")

        assert r.status_code == 404
        assert "traceback" not in r.text.lower()

    def test_tracking_issue_without_milestone_returns_404(self, monkeypatch) -> None:
        from coord import github_ops

        monkeypatch.setattr(
            github_ops, "get_issue",
            lambda repo, n: {"number": n, "title": "t", "milestone": None},
        )

        client = _gate_a_client()
        r = client.get("/api/gate-a/portal/5")

        assert r.status_code == 404

    def test_signed_off_contract_reports_not_stale_with_recorded_sha(
        self, rw_db, monkeypatch
    ) -> None:
        from coord import gate_a, github_ops, state

        monkeypatch.setattr(
            github_ops, "get_issue", _issue_with_milestone(122, 4, "Design round 4")
        )
        contract = "# Contract\n\nSome pinned surface text.\n"
        monkeypatch.setattr(
            github_ops, "get_repo_file",
            _fake_get_repo_file({"tests/acceptance/ms-4/contract.md": contract}),
        )
        sha = gate_a.contract_digest(contract)
        record = gate_a.make_record(
            repo_name="portal", milestone_number=4, verdict=gate_a.VERDICT_APPROVED,
            contract_sha=sha, tracking_issue=122, actor="jane",
        )
        state.save_gate_a_approval(record.to_dict())

        client = _gate_a_client()
        r = client.get("/api/gate-a/portal/122")

        assert r.status_code == 200
        body = r.json()
        assert body["state"] == "approved"
        assert body["ok"] is True
        assert body["stale"] is False
        assert body["contract_sha"] == sha
        assert body["contract_markdown"] == contract
        assert body["milestone_number"] == 4
        assert body["milestone_title"] == "Design round 4"
        assert body["approval"]["actor"] == "jane"
        assert body["approval"]["verdict"] == "approved"

    def test_amendment_after_signoff_reports_stale(self, rw_db, monkeypatch) -> None:
        from coord import gate_a, github_ops, state

        monkeypatch.setattr(
            github_ops, "get_issue", _issue_with_milestone(122, 4, "Design round 4")
        )
        old_contract = "# Contract v1\n"
        new_contract = "# Contract v2 — amended\n"
        monkeypatch.setattr(
            github_ops, "get_repo_file",
            _fake_get_repo_file({"tests/acceptance/ms-4/contract.md": new_contract}),
        )
        old_sha = gate_a.contract_digest(old_contract)
        record = gate_a.make_record(
            repo_name="portal", milestone_number=4, verdict=gate_a.VERDICT_APPROVED,
            contract_sha=old_sha, tracking_issue=122, actor="jane",
        )
        state.save_gate_a_approval(record.to_dict())

        client = _gate_a_client()
        r = client.get("/api/gate-a/portal/122")

        assert r.status_code == 200
        body = r.json()
        assert body["state"] == "stale"
        assert body["stale"] is True
        assert body["contract_sha"] == gate_a.contract_digest(new_contract)
        # The approval on file is still surfaced — a human can see WHAT was
        # approved even though it no longer matches the live contract.
        assert body["approval"]["contract_sha"] == old_sha

    def test_mocks_render_standalone_with_inlined_stylesheet_and_title(
        self, rw_db, monkeypatch
    ) -> None:
        """coord-portal ms-4's mocks link their stylesheet relatively
        (``../../../../public/tokens.css`` from
        ``tests/acceptance/ms-4/mocks/<name>.html``, four levels up to the
        repo root) — a path that only resolves in a checkout. This pins
        that the endpoint inlines it so the mock is self-contained."""
        from coord import github_ops

        monkeypatch.setattr(
            github_ops, "get_issue", _issue_with_milestone(122, 4, "Design round 4")
        )
        mock_html = (
            "<html><head><title>Home — Active</title>"
            '<link rel="stylesheet" href="../../../../public/tokens.css">'
            "</head><body>Hi</body></html>"
        )
        files = {
            "tests/acceptance/ms-4/contract.md": "# Contract",
            "tests/acceptance/ms-4/mocks/home.html": mock_html,
            "public/tokens.css": "body { color: red; }",
        }
        monkeypatch.setattr(github_ops, "get_repo_file", _fake_get_repo_file(files))
        monkeypatch.setattr(
            github_ops, "repo_file_exists", _fake_repo_file_exists(files)
        )
        monkeypatch.setattr(
            github_ops, "list_repo_dir",
            _fake_list_repo_dir({"tests/acceptance/ms-4/mocks": ["home.html"]}),
        )

        driver = AcceptanceDriverConfig(kind="web-playwright", mock="*.html")
        client = _gate_a_client(driver)
        r = client.get("/api/gate-a/portal/122")

        assert r.status_code == 200
        body = r.json()
        assert len(body["mocks"]) == 1
        mock = body["mocks"][0]
        assert mock["name"] == "home.html"
        assert mock["title"] == "Home — Active"
        # No further fetch needed to render it: the stylesheet is inlined,
        # the <link> is gone.
        assert "<link" not in mock["html"]
        assert "<style>" in mock["html"]
        assert "color: red" in mock["html"]

    def test_no_viewable_driver_yields_empty_mocks_with_a_reason(
        self, rw_db, monkeypatch
    ) -> None:
        """A repo with no acceptance driver configured (or a non-browser-
        viewable one, e.g. tui-tuidriver's `.screen` fixtures) must not
        crash or guess `*.html` — it degrades to an empty, explained mocks
        list (#3068's `resolve_viewable_mock_glob`)."""
        from coord import github_ops

        monkeypatch.setattr(
            github_ops, "get_issue", _issue_with_milestone(122, 4, "Design round 4")
        )
        monkeypatch.setattr(
            github_ops, "get_repo_file",
            _fake_get_repo_file({"tests/acceptance/ms-4/contract.md": "# Contract"}),
        )

        client = _gate_a_client()  # no driver configured
        r = client.get("/api/gate-a/portal/122")

        assert r.status_code == 200
        body = r.json()
        assert body["mocks"] == []
        assert body["mocks_note"] != ""

    def test_response_matches_its_openapi_schema(self, rw_db, monkeypatch) -> None:
        from coord import gate_a, github_ops, state

        monkeypatch.setattr(
            github_ops, "get_issue", _issue_with_milestone(122, 4, "Design round 4")
        )
        contract = "# Contract\n"
        monkeypatch.setattr(
            github_ops, "get_repo_file",
            _fake_get_repo_file({"tests/acceptance/ms-4/contract.md": contract}),
        )
        sha = gate_a.contract_digest(contract)
        record = gate_a.make_record(
            repo_name="portal", milestone_number=4, verdict=gate_a.VERDICT_APPROVED,
            contract_sha=sha, tracking_issue=122, actor="jane",
        )
        state.save_gate_a_approval(record.to_dict())

        client = _gate_a_client()
        r = client.get("/api/gate-a/portal/122")

        assert r.status_code == 200
        spec = openapi_spec()
        schema = spec["paths"]["/api/gate-a/{repo}/{tracking_issue}"]["get"][
            "responses"
        ]["200"]["content"]["application/json"]["schema"]
        errors = validate_json_schema(r.json(), schema, spec["components"]["schemas"])
        assert errors == [], errors


# ── GET /api/milestones{,/{repo}/{number}} (#3072) ──────────────────────────
#
# The milestone roster. Every test drives the REAL aggregation
# (`coord.plans.aggregate_repo_plans`, `coord.milestone_order.parse_work_order`,
# `coord.gates.gate_columns_for_issue`, `coord.gate_a.evaluate`) through faked
# GitHub reads — never a re-implementation of the roster, which is the whole
# point of building on the `coord plans` path.


#: An epic body whose `## Work order` deliberately declares its nodes in an
#: order GitHub would never return them in (12 before 10) — the fixture that
#: makes "ordered by the work order, not by milestone membership" falsifiable.
_WORK_ORDER_BODY = """\
Some preamble.

## Work order

- #12  {group: ms-4}
- #10  {after: #12}
- #11  {after: #10}
"""


def _ms_config(driver: AcceptanceDriverConfig | None = None) -> Config:
    drivers = {"portal": driver} if driver is not None else {}
    return Config(
        repos=[Repo(name="portal", github="acme/portal", default_branch="main")],
        machines=[Machine(name="laptop", host="laptop.tailnet", repos=["portal"])],
        acceptance=AcceptanceConfig(drivers=drivers),
    )


def _ms_client(driver: AcceptanceDriverConfig | None = None) -> TestClient:
    return TestClient(build_app(_ms_config(driver)))


def _epic(number: int = 122, milestone_number: int = 4, body: str = _WORK_ORDER_BODY):
    return {
        "number": number,
        "title": "ms-4 epic",
        "body": body,
        "labels": [{"name": "epic"}],
        "milestone": {"number": milestone_number, "title": "ms-4"},
    }


def _milestone(
    number: int = 4, *, title: str = "ms-4", state: str = "open",
    open_issues: int = 3, closed_issues: int = 1,
):
    return {
        "number": number,
        "title": title,
        "state": state,
        "open_issues": open_issues,
        "closed_issues": closed_issues,
        "description": "",
    }


def _patch_github(monkeypatch, **overrides) -> None:
    """Point every GitHub read the milestone endpoints make at a fake.

    Defaults describe one open milestone (ms-4) with a three-node work order
    whose declared order (12, 10, 11) differs from GitHub's membership order
    (10, 11, 12). Override any single read by keyword.
    """
    from coord import github_ops

    defaults = {
        "get_repo_milestones_with_counts": lambda repo, state="open": [_milestone()],
        "get_milestone": lambda repo, n: _milestone() if n == 4 else {},
        "get_open_issues": lambda repo, **kw: [_epic()],
        "get_closed_epics": lambda repo, **kw: [],
        "get_milestone_issues": lambda repo, title, state="all": [
            {"number": 10, "title": "slice one", "state": "CLOSED"},
            {"number": 11, "title": "slice two", "state": "OPEN"},
            {"number": 12, "title": "slice zero", "state": "CLOSED"},
        ],
        "get_repo_file": _fake_get_repo_file({}),
    }
    for name, fn in {**defaults, **overrides}.items():
        monkeypatch.setattr(github_ops, name, fn)


class TestMilestoneListAPI:
    """`GET /api/milestones` — the roster."""

    def test_unknown_repo_filter_returns_clean_404(self) -> None:
        client = _ms_client()
        r = client.get("/api/milestones?repo=nonexistent")
        assert r.status_code == 404
        assert "traceback" not in r.text.lower()

    def test_repo_with_no_milestones_returns_empty_list_not_a_500(
        self, monkeypatch
    ) -> None:
        _patch_github(
            monkeypatch, get_repo_milestones_with_counts=lambda repo, state="open": []
        )
        client = _ms_client()
        with patch("coord.dashboard.server.read_board", return_value=Board()):
            r = client.get("/api/milestones")

        assert r.status_code == 200
        assert r.json() == {"milestones": [], "warnings": []}

    def test_row_carries_github_counts_work_order_progress_and_oracle_flag(
        self, monkeypatch
    ) -> None:
        _patch_github(monkeypatch)
        client = _ms_client(AcceptanceDriverConfig(kind="web-playwright"))
        with patch("coord.dashboard.server.read_board", return_value=Board()):
            r = client.get("/api/milestones")

        assert r.status_code == 200
        rows = r.json()["milestones"]
        assert len(rows) == 1
        row = rows[0]
        assert row["repo_name"] == "portal"
        assert row["milestone_number"] == 4
        assert row["title"] == "ms-4"
        assert row["state"] == "open"
        assert row["tracking_issue"] == 122
        # GitHub's own counters, not a local re-count.
        assert row["open_issues"] == 3
        assert row["closed_issues"] == 1
        assert row["oracle"] is True
        # Work-order scope is a DIFFERENT number from the milestone's issue
        # counts: 3 declared nodes, none of them open (only the epic is).
        assert row["has_work_order"] is True
        assert row["work_order_total"] == 3
        assert row["work_order_done"] == 3

    def test_non_oracle_repo_reports_oracle_false(self, monkeypatch) -> None:
        _patch_github(monkeypatch)
        client = _ms_client()  # no acceptance driver configured
        with patch("coord.dashboard.server.read_board", return_value=Board()):
            r = client.get("/api/milestones")

        assert r.json()["milestones"][0]["oracle"] is False

    def test_all_closed_milestone_reports_closed_counts_matching_github(
        self, monkeypatch
    ) -> None:
        """A milestone whose issues are all closed must report GitHub's
        closed count, and must not report its work order as still in
        progress."""
        done = _milestone(state="closed", open_issues=0, closed_issues=4)
        _patch_github(
            monkeypatch,
            get_repo_milestones_with_counts=lambda repo, state="open": [done],
            # The epic itself is closed too — `coord plans`'s #974 rule: a
            # closed epic is still the tracking issue.
            get_open_issues=lambda repo, **kw: [],
            get_closed_epics=lambda repo, **kw: [_epic()],
        )
        client = _ms_client()
        with patch("coord.dashboard.server.read_board", return_value=Board()):
            r = client.get("/api/milestones")

        row = r.json()["milestones"][0]
        assert row["state"] == "closed"
        assert row["open_issues"] == 0
        assert row["closed_issues"] == 4
        assert row["tracking_issue"] == 122
        assert row["work_order_done"] == row["work_order_total"] == 3
        assert row["ready_frontier"] == 0

    def test_repo_fetch_failure_becomes_a_warning_not_a_500(self, monkeypatch) -> None:
        def _boom(repo, state="open"):
            raise RuntimeError("gh: API rate limit exceeded")

        _patch_github(monkeypatch, get_repo_milestones_with_counts=_boom)
        client = _ms_client()
        with patch("coord.dashboard.server.read_board", return_value=Board()):
            r = client.get("/api/milestones")

        assert r.status_code == 200
        body = r.json()
        assert body["milestones"] == []
        assert len(body["warnings"]) == 1
        assert "rate limit" in body["warnings"][0]

    def test_response_matches_its_openapi_schema(self, monkeypatch) -> None:
        _patch_github(monkeypatch)
        client = _ms_client()
        with patch("coord.dashboard.server.read_board", return_value=Board()):
            r = client.get("/api/milestones")

        assert r.status_code == 200
        spec = openapi_spec()
        schema = spec["paths"]["/api/milestones"]["get"]["responses"]["200"][
            "content"
        ]["application/json"]["schema"]
        errors = validate_json_schema(r.json(), schema, spec["components"]["schemas"])
        assert errors == [], errors


class TestMilestoneDetailAPI:
    """`GET /api/milestones/{repo}/{number}` — one milestone's story."""

    def test_unknown_repo_returns_clean_404(self) -> None:
        client = _ms_client()
        r = client.get("/api/milestones/nonexistent/4")
        assert r.status_code == 404
        assert "traceback" not in r.text.lower()

    def test_unknown_milestone_number_returns_clean_404(self, monkeypatch) -> None:
        _patch_github(monkeypatch, get_milestone=lambda repo, n: {})
        client = _ms_client()
        r = client.get("/api/milestones/portal/999")
        assert r.status_code == 404
        assert "traceback" not in r.text.lower()

    def test_milestone_fetch_error_returns_clean_404(self, monkeypatch) -> None:
        def _raise(repo, n):
            raise RuntimeError("gh: Not Found")

        _patch_github(monkeypatch, get_milestone=_raise)
        client = _ms_client()
        r = client.get("/api/milestones/portal/4")
        assert r.status_code == 404
        assert "traceback" not in r.text.lower()

    def test_non_integer_milestone_number_returns_clean_404(self) -> None:
        client = _ms_client()
        r = client.get("/api/milestones/portal/not-a-number")
        assert r.status_code == 404
        assert "traceback" not in r.text.lower()

    def test_entries_follow_the_work_order_not_github_membership(
        self, monkeypatch
    ) -> None:
        """THE acceptance criterion: GitHub hands back milestone membership
        as 10, 11, 12; the `## Work order` declares 12, 10, 11. Only the
        latter carries sequence, so that is what the endpoint must return."""
        _patch_github(monkeypatch)
        client = _ms_client()
        with patch("coord.dashboard.server.read_board", return_value=Board()):
            r = client.get("/api/milestones/portal/4")

        assert r.status_code == 200
        body = r.json()
        assert body["has_work_order"] is True
        assert [e["issue_number"] for e in body["entries"]] == [12, 10, 11]
        assert [e["position"] for e in body["entries"]] == [1, 2, 3]
        # Titles + live state come from GitHub, joined onto the work order.
        assert [e["title"] for e in body["entries"]] == [
            "slice zero", "slice one", "slice two",
        ]
        assert [e["state"] for e in body["entries"]] == ["closed", "closed", "open"]
        # The `after:`/`group:` annotations survive the join.
        assert body["entries"][0]["group"] == "ms-4"
        assert body["entries"][1]["after"] == [12]
        assert body["entries"][2]["after"] == [10]

    def test_milestone_without_a_tracking_epic_reports_no_work_order(
        self, monkeypatch
    ) -> None:
        _patch_github(monkeypatch, get_open_issues=lambda repo, **kw: [])
        client = _ms_client()
        with patch("coord.dashboard.server.read_board", return_value=Board()):
            r = client.get("/api/milestones/portal/4")

        assert r.status_code == 200
        body = r.json()
        assert body["has_work_order"] is False
        assert body["entries"] == []
        assert body["tracking_issue"] is None
        assert any("no `epic`-labelled" in w for w in body["warnings"])

    def test_entry_carries_the_board_gate_columns_coord_gates_reports(
        self, monkeypatch
    ) -> None:
        """Per-entry gate columns must be the winning work row's, selected
        the same way `coord gates` selects it — here the LATER of two work
        rows on the same issue."""
        _patch_github(monkeypatch)
        board = Board(completed=[
            Assignment(
                machine_name="laptop", repo_name="portal", issue_number=10,
                issue_title="slice one", assignment_id="old",
                type="work", status="failed",
                branch="issue-10-first", dispatched_at=100.0,
                test_state="failed",
            ),
            Assignment(
                machine_name="laptop", repo_name="portal", issue_number=10,
                issue_title="slice one", assignment_id="new",
                type="work", status="done",
                branch="issue-10-second", dispatched_at=200.0,
                test_state="passed", smoke_test="pass",
                review_state="done", review_verdict="approve",
            ),
        ])
        client = _ms_client()
        with patch("coord.dashboard.server.read_board", return_value=board):
            r = client.get("/api/milestones/portal/4")

        entries = {e["issue_number"]: e for e in r.json()["entries"]}
        gates = entries[10]["gates"]
        assert gates["assignment_id"] == "new"
        assert gates["branch"] == "issue-10-second"
        assert gates["status"] == "done"
        assert gates["test_state"] == "passed"
        assert gates["smoke_test"] == "pass"
        assert gates["review_state"] == "done"
        assert gates["review_verdict"] == "approve"
        # An entry that was never dispatched reports null gates — a
        # different fact from "dispatched, no verdict yet".
        assert entries[11]["gates"] is None

    def test_non_oracle_milestone_carries_null_gate_a_not_an_error(
        self, monkeypatch
    ) -> None:
        _patch_github(monkeypatch)
        client = _ms_client()  # no acceptance driver => not in the oracle loop
        with patch("coord.dashboard.server.read_board", return_value=Board()):
            r = client.get("/api/milestones/portal/4")

        assert r.status_code == 200
        body = r.json()
        assert body["oracle"] is False
        assert body["gate_a"] is None
        assert body["warnings"] == []

    def test_oracle_milestone_carries_its_gate_a_verdict_and_contract_sha(
        self, rw_db, monkeypatch
    ) -> None:
        from coord import gate_a, state

        contract = "# Contract\n\nSome pinned surface text.\n"
        _patch_github(
            monkeypatch,
            get_repo_file=_fake_get_repo_file(
                {"tests/acceptance/ms-4/contract.md": contract}
            ),
        )
        sha = gate_a.contract_digest(contract)
        state.save_gate_a_approval(
            gate_a.make_record(
                repo_name="portal", milestone_number=4,
                verdict=gate_a.VERDICT_APPROVED, contract_sha=sha,
                tracking_issue=122, actor="jane",
            ).to_dict()
        )

        client = _ms_client(AcceptanceDriverConfig(kind="web-playwright"))
        with patch("coord.dashboard.server.read_board", return_value=Board()):
            r = client.get("/api/milestones/portal/4")

        assert r.status_code == 200
        gate = r.json()["gate_a"]
        assert gate["state"] == "approved"
        assert gate["ok"] is True
        assert gate["verdict"] == "approved"
        assert gate["actor"] == "jane"
        assert gate["contract_sha"] == sha
        assert gate["approved_contract_sha"] == sha
        # Links to #3069's full packet rather than duplicating it.
        assert gate["href"] == "/api/gate-a/portal/122"

    def test_amended_contract_reports_stale_with_both_shas(
        self, rw_db, monkeypatch
    ) -> None:
        """coord-portal ms-4's real shape: a work order, an amendment, and a
        recorded approval that predates it."""
        from coord import gate_a, state

        old_sha = gate_a.contract_digest("# Contract v1\n")
        new_contract = "# Contract v2 — amended\n"
        _patch_github(
            monkeypatch,
            get_repo_file=_fake_get_repo_file(
                {"tests/acceptance/ms-4/contract.md": new_contract}
            ),
        )
        state.save_gate_a_approval(
            gate_a.make_record(
                repo_name="portal", milestone_number=4,
                verdict=gate_a.VERDICT_APPROVED, contract_sha=old_sha,
                tracking_issue=122, actor="jane",
            ).to_dict()
        )

        client = _ms_client(AcceptanceDriverConfig(kind="web-playwright"))
        with patch("coord.dashboard.server.read_board", return_value=Board()):
            r = client.get("/api/milestones/portal/4")

        gate = r.json()["gate_a"]
        assert gate["state"] == "stale"
        assert gate["ok"] is False
        assert gate["contract_sha"] == gate_a.contract_digest(new_contract)
        assert gate["approved_contract_sha"] == old_sha

    def test_gate_a_matches_the_dedicated_gate_a_endpoint(
        self, rw_db, monkeypatch
    ) -> None:
        """Both surfaces read `coord.gate_a.evaluate` through the SAME
        helper, so they cannot report different states for one milestone."""
        from coord import gate_a, github_ops, state

        contract = "# Contract\n"
        _patch_github(
            monkeypatch,
            get_repo_file=_fake_get_repo_file(
                {"tests/acceptance/ms-4/contract.md": contract}
            ),
        )
        monkeypatch.setattr(
            github_ops, "get_issue", _issue_with_milestone(122, 4, "ms-4")
        )
        state.save_gate_a_approval(
            gate_a.make_record(
                repo_name="portal", milestone_number=4,
                verdict=gate_a.VERDICT_APPROVED,
                contract_sha=gate_a.contract_digest(contract),
                tracking_issue=122, actor="jane",
            ).to_dict()
        )

        client = _ms_client(AcceptanceDriverConfig(kind="web-playwright"))
        with patch("coord.dashboard.server.read_board", return_value=Board()):
            roster = client.get("/api/milestones/portal/4").json()["gate_a"]
            packet = client.get(roster["href"]).json()

        assert roster["state"] == packet["state"]
        assert roster["ok"] == packet["ok"]
        assert roster["contract_sha"] == packet["contract_sha"]
        assert roster["verdict"] == packet["approval"]["verdict"]

    def test_gate_a_read_failure_degrades_to_null_with_a_warning(
        self, monkeypatch
    ) -> None:
        from coord import state

        _patch_github(monkeypatch)

        def _boom(**kwargs):
            raise RuntimeError("board daemon unreachable")

        monkeypatch.setattr(state, "get_gate_a_approval", _boom)

        client = _ms_client(AcceptanceDriverConfig(kind="web-playwright"))
        with patch("coord.dashboard.server.read_board", return_value=Board()):
            r = client.get("/api/milestones/portal/4")

        assert r.status_code == 200
        body = r.json()
        assert body["gate_a"] is None
        assert any("Gate A" in w for w in body["warnings"])

    def test_response_matches_its_openapi_schema(self, rw_db, monkeypatch) -> None:
        _patch_github(monkeypatch)
        board = Board(completed=[
            Assignment(
                machine_name="laptop", repo_name="portal", issue_number=10,
                issue_title="slice one", assignment_id="a1",
                type="work", status="done",
                branch="issue-10", dispatched_at=200.0, test_state="passed",
            ),
        ])
        client = _ms_client(AcceptanceDriverConfig(kind="web-playwright"))
        with patch("coord.dashboard.server.read_board", return_value=board):
            r = client.get("/api/milestones/portal/4")

        assert r.status_code == 200
        spec = openapi_spec()
        schema = spec["paths"]["/api/milestones/{repo}/{number}"]["get"][
            "responses"
        ]["200"]["content"]["application/json"]["schema"]
        errors = validate_json_schema(r.json(), schema, spec["components"]["schemas"])
        assert errors == [], errors


# ── #3091: GET /api/journal/{submission_id} ─────────────────────────────────
#
# Built directly on #3071's aggregator (`coord.portal_store.
# render_journal_payload`) — the same read `coord journal --json` prints —
# so the acceptance bar (issue #3091) is that this endpoint and the CLI can
# never disagree, curled against a real running app rather than trusted from
# unit tests alone.

JSUB = "sub-journal-3091"


def _journal_seed_applied(kind: str, fields: dict, *, now: float):
    """Queue a coord-owned fact and mark it applied — the state the
    journal's outbox fold reads. Mirrors tests/test_portal_store.py's
    identical helper."""
    from coord import portal_store

    row = portal_store.enqueue(JSUB, kind, fields, now=now)
    portal_store.mark_applied(row, now=now)
    return row


def _journal_seed_signoff(verdict: str = "approved", comments: str = "ship it", *, now: float):
    from coord import portal_store

    portal_store.record_events(
        [
            {
                "id": f"ev-{verdict}",
                "submission_id": JSUB,
                "type": f"signoff.{verdict}",
                "data": {"verdict": verdict, "comments": comments},
            }
        ],
        now=now,
    )


class TestJournalAPI:
    def test_full_history_matches_the_cli_entry_for_entry_and_in_order(
        self, rw_db
    ) -> None:
        """Acceptance bullet 1: a submission with a full history returns
        entries in the same order `coord journal` prints them, and the two
        outputs agree entry-for-entry."""
        from click.testing import CliRunner

        from coord import portal_store
        from coord.audit import record_audit
        from coord.cli import main

        portal_store.link_issue(repo_name="api", issue_number=42, submission_id=JSUB)
        _journal_seed_applied(
            "design_round",
            {"design_round": {"round": 1, "bundle_key": "r2://bundles/4"}},
            now=10.0,
        )
        _journal_seed_applied(
            "preview", {"preview_url": "https://pr-7.pages.dev"}, now=20.0,
        )
        _journal_seed_signoff(now=30.0)
        record_audit(
            tier="business", category="dispatch", event_type="dispatched",
            actor="drive", summary="Dispatched work to precision: api#42",
            repo="api", issue=42, ts=40.0,
        )
        record_audit(
            tier="business", category="merge", event_type="merged",
            actor="coordinator", summary="Merged: api#42",
            repo="api", issue=42, ts=50.0,
            details={"pr_url": "https://github.com/acme/api/pull/7"},
        )

        cli_result = CliRunner().invoke(main, ["journal", JSUB, "--json"])
        assert cli_result.exit_code == 0, cli_result.output
        cli_payload = __import__("json").loads(cli_result.output)

        client = _client()
        r = client.get(f"/api/journal/{JSUB}")

        assert r.status_code == 200
        body = r.json()
        assert body["submission_id"] == JSUB
        assert body["entries"] == cli_payload["entries"]
        assert [e["kind"] for e in body["entries"]] == [
            "design_round_published", "preview_published",
            "signoff_recorded", "dispatched", "merged",
        ]

    def test_unlinked_submission_returns_200_with_empty_timeline(self, rw_db) -> None:
        """Acceptance bullet 2: a submission coord knows about (it has a
        customer mirror) but that nobody ever ran `coord portal link` on
        — no dispatch/merge history is resolvable without a link, and
        (with no design rounds/previews/sign-offs either) the timeline is
        genuinely empty, not an error."""
        from coord import portal_store

        portal_store.mirror_customer_facts(JSUB, {"project_label": "Acme rebuild"})
        assert portal_store.get_link_by_submission(JSUB) is None

        client = _client()
        r = client.get(f"/api/journal/{JSUB}")

        assert r.status_code == 200
        body = r.json()
        assert body["entries"] == []
        assert body["link"] is None
        assert body["title"] == "Acme rebuild"
        assert any("no repo/milestone linked" in g for g in body["gaps"])

    def test_unknown_submission_id_returns_200_not_500(self, rw_db) -> None:
        """Acceptance bullet 3."""
        client = _client()
        r = client.get("/api/journal/sub-never-seen-anywhere")

        assert r.status_code == 200
        body = r.json()
        assert body["submission_id"] == "sub-never-seen-anywhere"
        assert body["entries"] == []
        assert body["title"] == ""
        assert body["customer_status"] == ""

    def test_every_artifact_is_null_or_an_absolute_url(self, rw_db) -> None:
        """Acceptance bullet 4."""
        from coord import portal_store
        from coord.audit import record_audit

        portal_store.link_issue(repo_name="api", issue_number=9, submission_id=JSUB)
        _journal_seed_applied(
            "design_round",
            {"design_round": {"round": 1, "bundle_key": "bundles/sub/r1.tar"}},
            now=10.0,
        )
        _journal_seed_applied(
            "preview", {"preview_url": "https://pr-9.pages.dev"}, now=20.0,
        )
        record_audit(
            tier="business", category="merge", event_type="merged",
            actor="coordinator", summary="Merged: api#9",
            repo="api", issue=9, ts=30.0,
        )

        client = _client()
        r = client.get(f"/api/journal/{JSUB}")

        assert r.status_code == 200
        entries = r.json()["entries"]
        assert entries  # non-trivial fixture
        for entry in entries:
            artifact = entry["artifact"]
            assert artifact is None or "://" in artifact

    def test_link_and_identity_fields_are_populated_when_on_file(
        self, rw_db
    ) -> None:
        from coord import portal_store

        portal_store.link_milestone(
            repo_name="api", milestone_number=4, submission_id=JSUB
        )
        portal_store.mirror_customer_facts(JSUB, {"project_label": "Acme rebuild"})

        client = _client()
        r = client.get(f"/api/journal/{JSUB}")

        assert r.status_code == 200
        body = r.json()
        assert body["title"] == "Acme rebuild"
        assert body["link"] == {
            "repo_name": "api",
            "milestone_number": 4,
            "issue_number": None,
            "submission_id": JSUB,
            "linked_at": body["link"]["linked_at"],
            "actor": "",
            "schema": body["link"]["schema"],
        }

    def test_response_matches_its_openapi_schema(self, rw_db) -> None:
        from coord import portal_store

        portal_store.link_milestone(
            repo_name="api", milestone_number=4, submission_id=JSUB
        )
        _journal_seed_applied(
            "preview", {"preview_url": "https://pr-7.pages.dev"}, now=1.0,
        )

        client = _client()
        r = client.get(f"/api/journal/{JSUB}")

        assert r.status_code == 200
        spec = openapi_spec()
        schema = spec["paths"]["/api/journal/{submission_id}"]["get"]["responses"][
            "200"
        ]["content"]["application/json"]["schema"]
        errors = validate_json_schema(r.json(), schema, spec["components"]["schemas"])
        assert errors == [], errors
