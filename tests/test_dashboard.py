"""Tests for the web dashboard API endpoints."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from starlette.testclient import TestClient

from coord.config import Config
from coord.dashboard.server import build_app, dist_has_bundle
from coord.models import Assignment, Board, Machine, Proposal, Repo
from coord.state import save_board


@pytest.fixture(autouse=True)
def _no_spa_dist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the legacy dashboard in every test in this module.

    server.py activates SPA serving at ``/`` when the resolved bundle dir
    has an ``index.html``. Without this patch, tests that assert on legacy
    dashboard HTML would fail on any machine that happens to have one, and
    since #2009 the default is ``~/coord-web-dist`` — a path the daemon host
    really does have — so this isolation is now load-bearing on the very
    machines most likely to run the suite, not just on a dev box that had
    run ``npm run build``.

    The server logic is correct — the test suite just needs isolation from
    the real filesystem state.
    """
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


def _client(tmp_path: Path | None = None) -> TestClient:
    return TestClient(build_app(_config()))


@pytest.fixture
def rw_db(tmp_path: Path):
    """A thread-safe (file-backed, ``check_same_thread=False``) coord.db override.

    The autouse ``coord_db`` fixture (tests/conftest.py) installs a
    thread-bound ``:memory:`` connection, which ``TestClient`` (it runs the
    async handler on a worker thread) can't touch — mirrors
    ``tests/test_serve.py``'s fixture of the same name, for the same reason:
    production ``get_connection`` already uses ``check_same_thread=False``
    (a file DB), so this just matches production for a route that actually
    reads through it instead of a mocked seam.
    """
    from coord import db
    from coord.db import _ensure_schema

    conn = sqlite3.connect(str(tmp_path / "rw.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    db.override_connection(conn)
    yield conn


class TestIndexPage:
    def test_serves_html(self) -> None:
        client = _client()
        r = client.get("/")
        assert r.status_code == 200
        assert "coord dashboard" in r.text


class TestBoardAPI:
    def test_returns_board_data(self, tmp_path: Path) -> None:
        board = Board(
            round_number=3,
            active=[
                Assignment(
                    machine_name="laptop", repo_name="api",
                    issue_number=42, issue_title="Fix auth",
                    assignment_id="abc", status="running",
                ),
            ],
            completed=[
                Assignment(
                    machine_name="laptop", repo_name="api",
                    issue_number=10, issue_title="Add logging",
                    assignment_id="def", status="done",
                    finished_at=1.0,
                ),
            ],
        )

        client = _client()
        with patch("coord.dashboard.server.read_board") as mock_load:
            mock_load.return_value = board
            r = client.get("/api/board")

        assert r.status_code == 200
        data = r.json()
        assert data["round_number"] == 3
        assert len(data["active"]) == 1
        assert data["active"][0]["issue_number"] == 42

    def test_empty_board(self) -> None:
        client = _client()
        with (
            patch("coord.dashboard.server.read_board", return_value=Board()),
        ):
            r = client.get("/api/board")
        assert r.status_code == 200
        assert r.json()["active"] == []


class TestMachinesAPI:
    """#3023: served from the daemon's already-refreshed health snapshot,
    never a per-request fan-out probe of the fleet — see
    ``tests/test_dashboard_api.py::TestMachinesAPI`` for the fuller
    shape/no-probe/legacy-consumer coverage this issue asks for. This class
    keeps the smoke-level "does the route wire up" check colocated with the
    rest of this file's board/sessions API tests.
    """

    def test_returns_machine_list(self) -> None:
        health = {
            "laptop": {
                "state": "online",
                "reason": "",
                "latency_ms": 5.0,
                "received_at": time.time(),
                "health": {"schema": 1, "checked_at": time.time(), "results": []},
            },
        }
        client = _client()
        with (
            patch("coord.state.load_machine_health", return_value=health),
            patch("coord.dashboard.server.read_board", return_value=Board()),
        ):
            r = client.get("/api/machines")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["name"] == "laptop"
        assert data[0]["state"] == "online"


class TestSessionsAPI:
    """Tests for GET /api/sessions (#1066)."""

    def _board(self) -> Board:
        return Board(
            active=[
                Assignment(
                    machine_name="laptop", repo_name="api",
                    issue_number=42, issue_title="Fix auth",
                    assignment_id="abc123", status="running", type="work",
                ),
                Assignment(
                    machine_name="laptop", repo_name="api",
                    issue_number=7, issue_title="Add logging",
                    assignment_id="def456", status="running", type="review",
                ),
            ],
        )

    def test_no_sessions_returns_empty_list(self) -> None:
        client = _client()
        with (
            patch("coord.interactive.list_coord_tmux_sessions", return_value=[]),
            patch("coord.dashboard.server.read_board", return_value=Board()),
        ):
            r = client.get("/api/sessions")
        assert r.status_code == 200
        assert r.json() == []

    def test_seeds_two_live_sessions_and_asserts_json_shape(self) -> None:
        raw = [
            {"session_name": "coord-abc123", "pane_dead": "0", "attached": True},
            {"session_name": "coord-def456", "pane_dead": "1", "attached": False},
        ]
        client = _client()
        with (
            patch("coord.interactive.list_coord_tmux_sessions", return_value=raw),
            patch("coord.dashboard.server.read_board", return_value=self._board()),
        ):
            r = client.get("/api/sessions")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 2
        by_id = {s["session_id"]: s for s in data}

        s1 = by_id["abc123"]
        assert s1["session_name"] == "coord-abc123"
        assert s1["machine"] == "laptop"
        assert s1["host"] == "laptop.tailnet"
        assert s1["repo"] == "api"
        assert s1["issue"] == 42
        assert s1["issue_title"] == "Fix auth"
        assert s1["stage"] == "work"
        assert s1["status"] == "running"
        assert s1["attached"] is True
        assert s1["pane_dead"] is False

        s2 = by_id["def456"]
        assert s2["issue"] == 7
        assert s2["stage"] == "review"
        assert s2["attached"] is False
        assert s2["pane_dead"] is True

    def test_unmatched_session_has_null_board_metadata(self) -> None:
        """A tmux session with no matching board assignment still appears,
        tagged with the machine it was discovered on during the fleet sweep
        (#1217), with board-derived fields null (mirrors the `coord sessions
        --json` nulls-when-no-db-match behaviour). Previously `machine`/`host`
        were also null here even though the sweep knew exactly which host
        produced the session."""
        raw = [{"session_name": "coord-unknown-aid"}]
        client = _client()
        with (
            patch("coord.interactive.list_coord_tmux_sessions", return_value=raw),
            patch("coord.dashboard.server.read_board", return_value=Board()),
        ):
            r = client.get("/api/sessions")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        s = data[0]
        assert s["session_id"] == "unknown-aid"
        assert s["session_name"] == "coord-unknown-aid"
        assert s["machine"] == "laptop"
        assert s["host"] == "laptop.tailnet"
        assert s["repo"] is None
        assert s["issue"] is None
        assert s["stage"] is None
        assert s["status"] is None
        assert s["attached"] is False
        assert s["pane_dead"] is False

    def test_matches_completed_assignment_too(self) -> None:
        """A session for a just-finished assignment (still tmux-live, e.g. the
        operator hasn't detached yet) resolves off board.completed as well as
        board.active."""
        board = Board(
            completed=[
                Assignment(
                    machine_name="laptop", repo_name="api",
                    issue_number=9, issue_title="Done thing",
                    assignment_id="fin789", status="done", type="work",
                    finished_at=1.0,
                ),
            ],
        )
        raw = [{"session_name": "coord-fin789", "pane_dead": "1", "attached": False}]
        client = _client()
        with (
            patch("coord.interactive.list_coord_tmux_sessions", return_value=raw),
            patch("coord.dashboard.server.read_board", return_value=board),
        ):
            r = client.get("/api/sessions")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["repo"] == "api"
        assert data[0]["status"] == "done"

    def test_fan_out_discovers_session_on_non_local_machine(self) -> None:
        """#1217: /api/sessions must sweep EVERY configured machine, not just
        the local host `coord web` happens to run on. Fakes a two-machine
        fleet and a `list_coord_tmux_sessions` that only reports a session for
        the *second* machine's `TmuxHost(ssh_target=...)` — proving the
        session surfaces via the per-machine fan-out (the `coord sessions
        --remote` pattern), correctly tagged with the machine it came from."""
        config = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[
                Machine(name="laptop", host="laptop.tailnet", repos=["api"]),
                Machine(name="precision", host="precision.tailnet", repos=["api"]),
            ],
        )

        def _fake_list(*, host=None):
            # `host` is the TmuxHost the endpoint built for this particular
            # machine's sweep call; only "precision" has a live session.
            if host is not None and host.ssh_target == "precision.tailnet":
                return [
                    {"session_name": "coord-remote1", "pane_dead": "0", "attached": True},
                ]
            return []

        board = Board(
            active=[
                Assignment(
                    machine_name="precision", repo_name="api",
                    issue_number=1213, issue_title="Fix thing",
                    assignment_id="remote1", status="running", type="work",
                ),
            ],
        )

        client = TestClient(build_app(config))
        with (
            patch("coord.interactive.list_coord_tmux_sessions", side_effect=_fake_list),
            patch("coord.dashboard.server.read_board", return_value=board),
        ):
            r = client.get("/api/sessions")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        s = data[0]
        assert s["session_id"] == "remote1"
        assert s["machine"] == "precision"
        assert s["host"] == "precision.tailnet"
        assert s["repo"] == "api"
        assert s["issue"] == 1213
        assert s["attached"] is True


class TestSessionsFanOutResilience:
    """#1217 fix iteration 1: a single slow/unreachable machine must not let
    the fleet tmux sweep back up indefinitely.  Reviewer repro: the phone
    dashboard polls /api/sessions every 4s; a down machine's SSH probe takes
    up to ~4-5s (bounded ConnectTimeout inside `list_coord_tmux_sessions`),
    so a naive re-probe-every-poll design queues sweep tasks faster than they
    drain — which, on the shared default asyncio executor, eventually starved
    every other consumer of `run_in_executor(None, ...)` in the process and
    hung the whole dashboard. The fix: a dedicated executor for this sweep,
    plus an offline-cooldown cache so a machine that looked unreachable is
    skipped (not re-probed) for a cooldown window."""

    def test_unreachable_machine_is_cooled_down_after_one_slow_probe(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Tiny thresholds so the test runs fast while still exercising the
        # real slow-probe-detection + cooldown-skip code path.
        monkeypatch.setattr("coord.dashboard.server._SESSIONS_SLOW_THRESHOLD", 0.05)
        monkeypatch.setattr("coord.dashboard.server._SESSIONS_COOLDOWN", 60.0)

        config = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[
                Machine(name="quick", host="quick.tailnet", repos=["api"]),
                Machine(name="flaky", host="flaky.tailnet", repos=["api"]),
            ],
        )

        calls: list[str | None] = []

        def _fake_list(*, host=None):
            target = host.ssh_target if host is not None else None
            calls.append(target)
            if target == "flaky.tailnet":
                time.sleep(0.1)  # simulate an SSH probe hitting ConnectTimeout
            return []

        client = TestClient(build_app(config))
        with (
            patch("coord.interactive.list_coord_tmux_sessions", side_effect=_fake_list),
            patch("coord.dashboard.server.read_board", return_value=Board()),
        ):
            r1 = client.get("/api/sessions")
            r2 = client.get("/api/sessions")

        assert r1.status_code == 200
        assert r2.status_code == 200
        # The healthy machine is swept on every poll...
        assert calls.count("quick.tailnet") == 2
        # ...but the flaky one is only probed once — the second poll skips it
        # entirely because it's still within the cooldown window.
        assert calls.count("flaky.tailnet") == 1

    def test_machine_is_reprobed_once_cooldown_expires(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("coord.dashboard.server._SESSIONS_SLOW_THRESHOLD", 0.05)
        monkeypatch.setattr("coord.dashboard.server._SESSIONS_COOLDOWN", 0.05)

        config = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(name="flaky", host="flaky.tailnet", repos=["api"])],
        )

        calls: list[str | None] = []

        def _fake_list(*, host=None):
            calls.append(host.ssh_target if host is not None else None)
            time.sleep(0.1)
            return []

        client = TestClient(build_app(config))
        with (
            patch("coord.interactive.list_coord_tmux_sessions", side_effect=_fake_list),
            patch("coord.dashboard.server.read_board", return_value=Board()),
        ):
            client.get("/api/sessions")
            time.sleep(0.1)  # let the cooldown window lapse
            client.get("/api/sessions")

        # Once cooldown has lapsed, the machine is re-probed instead of being
        # skipped forever.
        assert calls.count("flaky.tailnet") == 2


class TestProposalsAPI:
    def test_returns_proposals(self, tmp_path: Path) -> None:
        proposals = [
            Proposal(
                id=1, machine_name="laptop", repo_name="api",
                issue_number=42, issue_title="Fix auth",
                rationale="test", files_likely=["auth.py"],
            ),
        ]
        client = _client()
        with patch("coord.dashboard.server.load_proposals", return_value=proposals):
            r = client.get("/api/proposals")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["issue_number"] == 42

    def test_empty_proposals(self) -> None:
        client = _client()
        with patch("coord.dashboard.server.load_proposals", return_value=[]):
            r = client.get("/api/proposals")
        assert r.status_code == 200
        assert r.json() == []


class TestDriveQueueAPI:
    """GET /api/drive-queue (#2428 DQW-1).

    Unlike `TestBoardAPI`/`TestProposalsAPI` above, this reads straight
    through `coord.state.list_drive_queue` -> the local DB (the `coord_db`
    autouse fixture in conftest.py gives every test a fresh in-memory
    SQLite, and `_no_board_service` guarantees `resolve_board_service()` is
    unset) rather than mocking the read seam — the same "seeded DB -> GET ->
    assert shape" bar every other `/api/*` route in this file meets.
    """

    def test_empty_queue(self, rw_db) -> None:
        client = _client()
        r = client.get("/api/drive-queue")
        assert r.status_code == 200
        data = r.json()
        assert data == {
            "entries": [],
            "summary": {
                "level": "empty",
                "pending": 0,
                "running": 0,
                "waiting": 0,
                "blocked": 0,
                "eligible": 0,
                "held": 0,
                "fleet_held": 0,
            },
            "leg_counts": {},
        }

    def test_seeded_db_returns_entries_and_summary(self, rw_db) -> None:
        from coord.state import (
            _enqueue_drive_queue_local,
            _update_drive_queue_entry_local,
        )

        _enqueue_drive_queue_local("api", 1)
        _enqueue_drive_queue_local("api", 2, after=["api#1"])
        _update_drive_queue_entry_local("api", 1, state="running")
        _enqueue_drive_queue_local("web", 9)
        _update_drive_queue_entry_local(
            "web", 9, state="blocked", last_reason="merge conflict"
        )

        client = _client()
        r = client.get("/api/drive-queue")
        assert r.status_code == 200
        data = r.json()

        entries = data["entries"]
        assert len(entries) == 3
        by_key = {(e["repo_name"], e["issue_number"]): e for e in entries}
        assert by_key[("api", 1)]["state"] == "running"
        assert by_key[("api", 2)]["state"] == "waiting"
        assert by_key[("api", 2)]["after_json"] == ["api#1"]
        assert by_key[("web", 9)]["state"] == "blocked"
        assert by_key[("web", 9)]["last_reason"] == "merge conflict"
        # #2428: raw entries carry the same columns the daemon's own
        # GET /drive-queue and /board's drive_queue field already do — no
        # reshaping, no invented fields.
        assert {
            "repo_name", "issue_number", "position", "state", "machine",
            "attempts", "after_json", "hold_state", "hold_scope", "hold_after",
            "last_reason", "resumes", "deferrals",
        } <= set(by_key[("api", 1)])

        summary = data["summary"]
        assert summary["running"] == 1
        assert summary["blocked"] == 1
        # api#2's after=["api#1"] is unsatisfied — api#1 is `running`, not
        # `done` — so it counts as waiting but NOT eligible.
        assert summary["waiting"] == 1
        assert summary["eligible"] == 0
        assert summary["pending"] == 3
        # blocked outranks everything else in the level ranking.
        assert summary["level"] == "blocked"

    def test_repo_filter_scopes_entries_but_not_the_summary(self, rw_db) -> None:
        """``?repo=`` narrows ``entries``; ``summary`` stays fleet-wide.

        Non-blocking review finding on #2428: `fleet_held`/`level` are
        documented (both in `tui/src/app/drive_queue.rs` and the Python port)
        as fleet-wide facts — "non-zero means the tick launches nothing at
        all" — and the summary's `_after_satisfied` treats a pre-req absent
        from the entries it's given as satisfied. Summarizing only the
        `?repo=`-filtered subset would let `GET /api/drive-queue?repo=api`
        report `level: "normal"` while a DIFFERENT repo's fired fleet gate is
        actually holding the whole queue. So `summary` must always reflect
        the full queue, exactly like `entries_from_rows`' one Rust call site
        always passes the unfiltered board queue.
        """
        from coord.state import _enqueue_drive_queue_local, _update_drive_queue_entry_local

        _enqueue_drive_queue_local("api", 1)
        _enqueue_drive_queue_local(
            "web", 2, hold_after=True, hold_reason="deploy gate", hold_scope="fleet",
        )
        _update_drive_queue_entry_local("web", 2, hold_state="fired")

        client = _client()
        r = client.get("/api/drive-queue", params={"repo": "api"})
        assert r.status_code == 200
        data = r.json()
        # entries: narrowed to the requested repo.
        assert [e["repo_name"] for e in data["entries"]] == ["api"]
        # summary: unaffected by the filter — the "web" entry's fleet-scoped
        # fired gate is still visible even though its row is filtered out of
        # `entries`.
        unfiltered = client.get("/api/drive-queue").json()
        assert data["summary"] == unfiltered["summary"]
        assert data["summary"]["pending"] == 2
        assert data["summary"]["fleet_held"] == 1
        assert data["summary"]["held"] == 1
        assert data["summary"]["level"] == "held"

    def test_leg_counts_keyed_repo_hash_issue_and_broken_out_by_type(
        self, rw_db
    ) -> None:
        """#3060: `leg_counts` is a THIRD sibling of `entries`/`summary`, not
        a reshaping of either — sourced from `assignments`, not `drive_queue`."""
        rw_db.execute(
            "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
            "issue_number, issue_title, type) VALUES ('a-1', 'm', 'api', 1, 't', 'work')"
        )
        rw_db.execute(
            "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
            "issue_number, issue_title, type) VALUES ('a-2', 'm', 'api', 1, 't', 'review')"
        )
        rw_db.execute(
            "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
            "issue_number, issue_title, type) VALUES ('a-3', 'm', 'web', 9, 't', 'smoke')"
        )
        rw_db.commit()

        client = _client()
        data = client.get("/api/drive-queue").json()
        assert data["leg_counts"] == {
            "api#1": {"work": 1, "review": 1},
            "web#9": {"smoke": 1},
        }

    def test_leg_counts_is_computed_over_the_full_history_not_the_repo_filter(
        self, rw_db
    ) -> None:
        """Mirrors `test_repo_filter_scopes_entries_but_not_the_summary`:
        `?repo=` narrows `entries` only — `leg_counts` stays fleet-wide, same
        posture as `summary`, so a client can still look up a filtered-out
        repo's own counts by key if it ever needs to."""
        rw_db.execute(
            "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
            "issue_number, issue_title, type) VALUES ('a-1', 'm', 'api', 1, 't', 'work')"
        )
        rw_db.execute(
            "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
            "issue_number, issue_title, type) VALUES ('a-2', 'm', 'web', 9, 't', 'smoke')"
        )
        rw_db.commit()

        client = _client()
        filtered = client.get("/api/drive-queue", params={"repo": "api"}).json()
        unfiltered = client.get("/api/drive-queue").json()
        assert filtered["leg_counts"] == unfiltered["leg_counts"] == {
            "api#1": {"work": 1},
            "web#9": {"smoke": 1},
        }

    def test_leg_counts_does_not_reset_on_a_drive_queue_relaunch(self, rw_db) -> None:
        """#2972: `drive_queue.attempts` resets on a relaunch while the fix
        budget it's meant to track keeps burning — that's the whole reason
        this field exists. Prove the two diverge: bump `attempts` back down
        (what a relaunch does to the queue row) and confirm `leg_counts`
        — sourced from `assignments`, a table a queue relaunch never
        touches — is unaffected."""
        from coord.state import _enqueue_drive_queue_local, _update_drive_queue_entry_local

        _enqueue_drive_queue_local("api", 1)
        _update_drive_queue_entry_local("api", 1, attempts=2)
        rw_db.execute(
            "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
            "issue_number, issue_title, type) VALUES ('a-1', 'm', 'api', 1, 't', 'work')"
        )
        rw_db.execute(
            "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
            "issue_number, issue_title, type) VALUES ('a-2', 'm', 'api', 1, 't', 'work')"
        )
        rw_db.commit()

        client = _client()
        before = client.get("/api/drive-queue").json()
        assert before["entries"][0]["attempts"] == 2
        assert before["leg_counts"]["api#1"] == {"work": 2}

        # Simulate a drive-queue relaunch: attempts resets to 0, the entry's
        # queue-side history is wiped — but the two dispatched legs already
        # recorded in `assignments` are untouched.
        _update_drive_queue_entry_local("api", 1, attempts=0)

        after = client.get("/api/drive-queue").json()
        assert after["entries"][0]["attempts"] == 0
        assert after["leg_counts"]["api#1"] == {"work": 2}


class TestDriveQueueActionAPI:
    """POST /api/drive-queue/action (#2429 DQW-2).

    Same posture as ``TestDriveQueueAPI``: no board_service configured, so
    every write reaches the local DB (the `coord_db`/`rw_db` fixtures) via
    ``_drive_queue_write``'s local branch — the same ``_*_local`` functions
    ``coord/serve_app.py``'s own ``post_drive_queue`` route calls.
    """

    def test_move(self, rw_db) -> None:
        from coord.state import _enqueue_drive_queue_local

        _enqueue_drive_queue_local("api", 1)
        _enqueue_drive_queue_local("api", 2)
        _enqueue_drive_queue_local("api", 3)

        client = _client()
        r = client.post("/api/drive-queue/action", json={
            "repo_name": "api", "issue_number": 3,
            "action": "move", "to_position": 0,
        })
        assert r.status_code == 200
        assert r.json() == {"ok": True}

        entries = client.get("/api/drive-queue").json()["entries"]
        by_key = {(e["repo_name"], e["issue_number"]): e["position"] for e in entries}
        assert by_key[("api", 3)] == 0

    def test_move_requires_to_position(self, rw_db) -> None:
        from coord.state import _enqueue_drive_queue_local

        _enqueue_drive_queue_local("api", 1)
        client = _client()
        r = client.post("/api/drive-queue/action", json={
            "repo_name": "api", "issue_number": 1, "action": "move",
        })
        assert r.status_code == 400

    def test_remove(self, rw_db) -> None:
        from coord.state import _enqueue_drive_queue_local

        _enqueue_drive_queue_local("api", 1)

        client = _client()
        r = client.post("/api/drive-queue/action", json={
            "repo_name": "api", "issue_number": 1, "action": "remove",
        })
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        assert client.get("/api/drive-queue").json()["entries"] == []

    def test_unblock_dequeues_and_reenqueues_dropping_after_but_keeping_machine(
        self, rw_db
    ) -> None:
        """Mirrors `dispatch_drive_queue_unblock` in `tui/src/app/drive_queue.rs`:
        the machine pin survives, `after` does not (an unsatisfiable pre-req
        is one of the two things that blocks a row — re-adding it would just
        re-block immediately).
        """
        from coord.state import (
            _enqueue_drive_queue_local,
            _update_drive_queue_entry_local,
        )

        _enqueue_drive_queue_local("api", 1, machine="laptop", after=["api#0"])
        _update_drive_queue_entry_local(
            "api", 1, state="blocked", last_reason="unsatisfiable after", attempts=3,
        )

        client = _client()
        r = client.post("/api/drive-queue/action", json={
            "repo_name": "api", "issue_number": 1, "action": "unblock",
        })
        assert r.status_code == 200
        assert r.json() == {"ok": True}

        entries = client.get("/api/drive-queue").json()["entries"]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["state"] == "waiting"
        assert entry["machine"] == "laptop"
        assert entry["after_json"] == []
        assert entry["attempts"] == 0

    def test_unblock_refuses_a_non_blocked_row(self, rw_db) -> None:
        from coord.state import _enqueue_drive_queue_local

        _enqueue_drive_queue_local("api", 1)  # state == "waiting"

        client = _client()
        r = client.post("/api/drive-queue/action", json={
            "repo_name": "api", "issue_number": 1, "action": "unblock",
        })
        assert r.status_code == 400
        assert r.json()["ok"] is False
        entries = client.get("/api/drive-queue").json()["entries"]
        assert entries[0]["state"] == "waiting"

    def test_unblock_does_not_reenqueue_when_dequeue_finds_nothing(
        self, monkeypatch
    ) -> None:
        """The guard read (``_read_drive_queue()``) and the write are not
        atomic — the row can be removed by a concurrent dequeue/daemon tick
        between the two. If the dequeue that ``unblock`` issues finds
        nothing to delete, it must NOT still re-enqueue a fresh row: that
        would silently resurrect an entry someone else legitimately removed
        (#2429 DQW-2 review).
        """
        from coord import client as cc

        monkeypatch.setattr(
            cc, "resolve_board_service",
            lambda *a, **k: cc.ServiceConfig("http://d:7435"),
        )
        monkeypatch.setattr(
            cc, "fetch_drive_queue",
            lambda svc, repo_name=None, **kw: [{
                "repo_name": "api", "issue_number": 7, "state": "blocked",
                "hold_state": "", "machine": "laptop", "after_json": [],
            }],
        )
        calls: list[dict] = []

        def _post_drive_queue(svc, action, **fields):
            calls.append({"action": action, **fields})
            if action == "dequeue":
                return {"deleted": False}
            raise AssertionError(f"unblock must not call {action!r} after a failed dequeue")

        monkeypatch.setattr(cc, "post_drive_queue", _post_drive_queue)

        client = _client()
        r = client.post("/api/drive-queue/action", json={
            "repo_name": "api", "issue_number": 7, "action": "unblock",
        })
        assert r.status_code == 404
        assert r.json()["ok"] is False
        assert calls == [{"action": "dequeue", "repo_name": "api", "issue_number": 7}]

    def test_resume_releases_a_fired_gate(self, rw_db) -> None:
        from coord.state import (
            _enqueue_drive_queue_local,
            _update_drive_queue_entry_local,
        )

        _enqueue_drive_queue_local(
            "api", 1, hold_after=True, hold_reason="deploy gate", hold_scope="fleet",
        )
        _update_drive_queue_entry_local("api", 1, hold_state="fired", hold_probes=3)

        client = _client()
        r = client.post("/api/drive-queue/action", json={
            "repo_name": "api", "issue_number": 1, "action": "resume",
        })
        assert r.status_code == 200
        assert r.json() == {"ok": True}

        entry = client.get("/api/drive-queue").json()["entries"][0]
        assert entry["hold_state"] == "released"
        assert entry["hold_probes"] == 0

    def test_resume_refuses_an_unfired_gate(self, rw_db) -> None:
        from coord.state import _enqueue_drive_queue_local

        _enqueue_drive_queue_local("api", 1, hold_after=True, hold_reason="deploy gate")
        # hold_state == "armed" (declared but never fired) — not "fired".

        client = _client()
        r = client.post("/api/drive-queue/action", json={
            "repo_name": "api", "issue_number": 1, "action": "resume",
        })
        assert r.status_code == 400
        assert r.json()["ok"] is False

    def test_entry_not_found(self, rw_db) -> None:
        client = _client()
        r = client.post("/api/drive-queue/action", json={
            "repo_name": "api", "issue_number": 999, "action": "remove",
        })
        assert r.status_code == 404

    def test_unknown_action(self, rw_db) -> None:
        from coord.state import _enqueue_drive_queue_local

        _enqueue_drive_queue_local("api", 1)
        client = _client()
        r = client.post("/api/drive-queue/action", json={
            "repo_name": "api", "issue_number": 1, "action": "teleport",
        })
        assert r.status_code == 400

    def test_missing_fields(self, rw_db) -> None:
        client = _client()
        r = client.post("/api/drive-queue/action", json={"action": "remove"})
        assert r.status_code == 400

    def test_routes_through_the_daemon_when_board_service_is_set(
        self, monkeypatch
    ) -> None:
        """Thin-client posture: with `board_service` configured, the write
        goes through `coord.client.post_drive_queue` — never a local
        `_*_local` call — exactly like `_read_board()`/`_write_board()` do
        for the board (#2429 DQW-2).
        """
        from coord import client as cc

        monkeypatch.setattr(
            cc, "resolve_board_service",
            lambda *a, **k: cc.ServiceConfig("http://d:7435"),
        )
        monkeypatch.setattr(
            cc, "fetch_drive_queue",
            lambda svc, repo_name=None, **kw: [{
                "repo_name": "api", "issue_number": 7, "state": "blocked",
                "hold_state": "", "machine": "laptop", "after_json": [],
            }],
        )
        calls: list[dict] = []

        def _post_drive_queue(svc, action, **fields):
            calls.append({"action": action, **fields})
            return {"deleted": True}

        monkeypatch.setattr(cc, "post_drive_queue", _post_drive_queue)

        client = _client()
        r = client.post("/api/drive-queue/action", json={
            "repo_name": "api", "issue_number": 7, "action": "remove",
        })
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        assert calls == [{"action": "dequeue", "repo_name": "api", "issue_number": 7}]


class TestReportAPI:
    """GET /api/report + GET /api/report/{report_id} (#2492 RPT-1).

    Same "seeded DB -> GET -> assert shape" bar as `TestDriveQueueAPI` above:
    `drive-queue-status` is the cheapest report to seed for a dashboard-level
    shape test (a couple of `_enqueue_drive_queue_local` rows, no audit_log
    fixture needed) — the report *engine* itself already has an exhaustive
    suite in `tests/test_reports.py`; this only pins that the dashboard's
    thin routes reach it and shape the response/errors the same way
    `coord/serve_app.py`'s `GET /report` + `GET /report/{report_id}` do.
    """

    def test_catalogue_lists_every_report_with_param_metadata(self, rw_db) -> None:
        client = _client()
        r = client.get("/api/report")
        assert r.status_code == 200
        body = r.json()
        ids = [rep["id"] for rep in body["reports"]]
        assert "drive-queue-status" in ids
        assert "issue-activity" in ids
        rep = next(rep for rep in body["reports"] if rep["id"] == "drive-queue-status")
        assert rep["title"] == "Drive Queue Status"
        params = {p["id"]: p for p in rep["params"]}
        assert set(params) == {"repo"}

    def test_run_returns_report_result_for_seeded_rows(self, rw_db) -> None:
        from coord.state import _enqueue_drive_queue_local, _update_drive_queue_entry_local

        _enqueue_drive_queue_local("api", 1)
        _enqueue_drive_queue_local("api", 2, after=["api#1"])
        _update_drive_queue_entry_local("api", 1, state="running")

        client = _client()
        r = client.get("/api/report/drive-queue-status")
        assert r.status_code == 200
        body = r.json()
        assert body["report_id"] == "drive-queue-status"
        assert {row["issue"] for row in body["rows"]} == {1, 2}
        by_issue = {row["issue"]: row for row in body["rows"]}
        assert by_issue[1]["state"] == "running"
        # #1760: additive display metadata, one entry per `columns` entry.
        assert [m["id"] for m in body["column_meta"]] == body["columns"]

    def test_run_repo_param_narrows_rows(self, rw_db) -> None:
        from coord.state import _enqueue_drive_queue_local

        _enqueue_drive_queue_local("api", 1)
        _enqueue_drive_queue_local("web", 2)

        client = _client()
        r = client.get("/api/report/drive-queue-status", params={"repo": "api"})
        assert r.status_code == 200
        rows = r.json()["rows"]
        assert {row["repo"] for row in rows} == {"api"}

    def test_format_csv_returns_text_csv_with_a_filename(self, rw_db) -> None:
        from coord.state import _enqueue_drive_queue_local

        _enqueue_drive_queue_local("api", 1)

        client = _client()
        r = client.get(
            "/api/report/drive-queue-status", params={"format": "csv"}
        )
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        disposition = r.headers["content-disposition"]
        assert disposition.startswith("attachment; filename=")
        assert "drive-queue-status-" in disposition and ".csv" in disposition
        assert "# report: drive-queue-status" in r.text

    def test_unknown_report_id_is_404(self, rw_db) -> None:
        client = _client()
        r = client.get("/api/report/no-such-report")
        assert r.status_code == 404
        assert "drive-queue-status" in r.json()["error"]

    def test_unknown_format_is_400(self, rw_db) -> None:
        client = _client()
        r = client.get(
            "/api/report/drive-queue-status", params={"format": "xlsx"}
        )
        assert r.status_code == 400
        assert "csv" in r.json()["error"]

    def test_format_is_not_treated_as_a_report_parameter(self, rw_db) -> None:
        """`resolve_params` rejects unknown parameters — `format` must be
        popped before it gets there (#1765)."""
        client = _client()
        r = client.get(
            "/api/report/drive-queue-status", params={"format": "csv"}
        )
        assert r.status_code == 200


class TestApproveAPI:
    # #749: board_service.read_board() tries load_board() before build_board()
    # — mock both so the real load_board() (which needs a live connection on
    # this thread) never runs.
    @patch("coord.state.load_board", return_value=None)
    @patch("coord.state.build_board", return_value=Board())
    @patch("coord.state.save_board")
    @patch("coord.state.clear_proposals")
    @patch("coord.state.record_dispatched")
    @patch("coord.state.load_dispatched", return_value=[])
    @patch("coord.state.load_proposals")
    @patch("coord.dispatch.post_briefing")
    @patch("coord.dispatch.httpx.post")
    def test_approve_dispatches(
        self, mock_post, mock_briefing, mock_load_p, mock_load_d,
        mock_record, mock_clear, mock_save, mock_build, mock_load_board,
    ) -> None:
        mock_load_p.return_value = [
            Proposal(
                id=1, machine_name="laptop", repo_name="api",
                issue_number=42, issue_title="Fix",
                rationale="test",
            ),
        ]
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "xyz"}
        mock_resp.raise_for_status = lambda: None
        mock_post.return_value = mock_resp

        client = _client()
        r = client.post("/api/approve", json={"ids": [1]})
        assert r.status_code == 200
        data = r.json()
        assert data["results"][0]["ok"]

    def test_approve_invalid_json(self) -> None:
        client = _client()
        r = client.post("/api/approve", content="not json", headers={"content-type": "application/json"})
        assert r.status_code == 400

    def test_approve_empty_ids(self) -> None:
        client = _client()
        r = client.post("/api/approve", json={"ids": []})
        assert r.status_code == 400


class TestRejectAPI:
    def test_reject_removes_proposals(self) -> None:
        proposals = [
            Proposal(id=1, machine_name="m", repo_name="api",
                     issue_number=1, issue_title="A", rationale=""),
            Proposal(id=2, machine_name="m", repo_name="api",
                     issue_number=2, issue_title="B", rationale=""),
        ]
        client = _client()
        with (
            patch("coord.state.load_proposals", return_value=proposals),
            patch("coord.state.save_proposals") as mock_save,
        ):
            r = client.post("/api/reject", json={"ids": [1]})
        assert r.status_code == 200
        data = r.json()
        assert data["removed"] == 1
        assert data["remaining"] == 1
        saved = mock_save.call_args.args[0]
        assert len(saved) == 1
        assert saved[0].id == 2

    def test_reject_invalid_json(self) -> None:
        client = _client()
        r = client.post("/api/reject", content="bad", headers={"content-type": "application/json"})
        assert r.status_code == 400

    def test_reject_empty_ids(self) -> None:
        client = _client()
        r = client.post("/api/reject", json={"ids": []})
        assert r.status_code == 400


class TestDiffAPI:
    def test_diff_not_found(self) -> None:
        client = _client()
        with (
            patch("coord.dashboard.server.read_board", return_value=Board()),
        ):
            r = client.get("/api/diff/nonexistent")
        assert r.status_code == 404

    def test_diff_no_branch(self) -> None:
        board = Board(completed=[
            Assignment(machine_name="m", repo_name="api", issue_number=1,
                       issue_title="t", assignment_id="abc", status="done",
                       branch=None),
        ])
        client = _client()
        with patch("coord.dashboard.server.read_board", return_value=board):
            r = client.get("/api/diff/abc")
        assert r.status_code == 404
        assert "no branch" in r.json()["error"]

    @patch("coord.github_ops._gh")
    def test_diff_from_pr(self, mock_gh: MagicMock) -> None:
        board = Board(completed=[
            Assignment(machine_name="m", repo_name="api", issue_number=1,
                       issue_title="t", assignment_id="abc", status="done",
                       branch="feat/x"),
        ])
        mock_gh.return_value = "diff --git a/f.py b/f.py\n+new line"
        client = _client()
        with patch("coord.dashboard.server.read_board", return_value=board):
            r = client.get("/api/diff/abc")
        assert r.status_code == 200
        assert "new line" in r.json()["diff"]


class TestBriefingOverride:
    # #749: board_service.read_board() tries load_board() before build_board().
    @patch("coord.state.load_board", return_value=None)
    @patch("coord.state.build_board", return_value=Board())
    @patch("coord.state.save_board")
    @patch("coord.state.clear_proposals")
    @patch("coord.state.record_dispatched")
    @patch("coord.state.load_dispatched", return_value=[])
    @patch("coord.state.load_proposals")
    @patch("coord.dispatch.post_briefing")
    @patch("coord.dispatch.httpx.post")
    def test_briefing_override_applied(
        self, mock_post, mock_briefing, mock_load_p, *_mocks,
    ) -> None:
        mock_load_p.return_value = [
            Proposal(id=1, machine_name="laptop", repo_name="api",
                     issue_number=42, issue_title="Fix",
                     rationale="test", briefing="original"),
        ]
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "xyz"}
        mock_resp.raise_for_status = lambda: None
        mock_post.return_value = mock_resp

        client = _client()
        r = client.post("/api/approve", json={
            "ids": [1],
            "briefings": {"1": "edited briefing"},
        })
        assert r.status_code == 200
        call_args = mock_post.call_args
        payload = call_args.kwargs.get("json") or call_args[1].get("json")
        assert payload["briefing"] == "edited briefing"


class TestChatAPI:
    def test_chat_requires_message(self) -> None:
        client = _client()
        r = client.post("/api/chat", json={"message": ""})
        assert r.status_code == 400

    def test_chat_invalid_json(self) -> None:
        client = _client()
        r = client.post("/api/chat", content="bad", headers={"content-type": "application/json"})
        assert r.status_code == 400

    def test_chat_uses_default_provider_command(self) -> None:
        """api_chat builds its subprocess command via the config's default provider.

        Verifies that the command handed to create_subprocess_exec comes from
        the provider layer (no hard-coded "claude" string), and that the
        output_format flag is NOT added (dashboard streams plain text, not JSON).
        """
        from unittest.mock import AsyncMock, MagicMock

        # Minimal async process stub
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.close = MagicMock()
        mock_proc.wait = AsyncMock(return_value=0)

        # Async iterator that yields nothing (empty response)
        async def _empty_aiter():
            return
            yield  # make it an async generator

        mock_proc.stdout = _empty_aiter()

        captured_cmd: list = []

        async def fake_exec(*args, **kwargs):
            captured_cmd.extend(args)
            return mock_proc

        with (
            patch("coord.dashboard.server.read_board", return_value=Board()),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        ):
            client = _client()
            # The streaming response is consumed fully by TestClient
            r = client.post("/api/chat", json={"message": "hello"})

        # The command must be derived from the provider (ClaudeProvider default).
        assert captured_cmd[0] == "claude", (
            f"Expected 'claude' binary, got {captured_cmd[0]!r}; "
            "the dashboard chat must route through the provider layer"
        )
        assert "-p" in captured_cmd
        assert "--system-prompt" in captured_cmd
        # output_format=None path: no --output-format flag for plain-text streaming.
        assert "--output-format" not in captured_cmd


class TestPipelineAction:
    """Tests for /api/pipeline/action — dispatch feedback fields."""

    def _board_with_done(self) -> "Board":
        return Board(
            active=[],
            completed=[
                Assignment(
                    machine_name="laptop", repo_name="api",
                    issue_number=42, issue_title="Fix auth",
                    assignment_id="work001", status="done",
                    branch="issue-42-fix-auth",
                    finished_at=1.0,
                ),
            ],
        )

    def test_dispatch_review_returns_machine_and_id(self) -> None:
        review_assignment = Assignment(
            machine_name="desktop", repo_name="api",
            issue_number=42, issue_title="Fix auth",
            assignment_id="rev00001", status="running", type="review",
        )
        client = _client()
        with (
            patch("coord.dashboard.server.read_board", return_value=self._board_with_done()),
            patch("coord.review.dispatch_review", return_value=review_assignment) as mock_dr,
            patch("coord.dashboard.server.write_board"),
        ):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "work001",
                "action": "dispatch_review",
            })
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["machine_name"] == "desktop"
        assert data["assignment_id"] == "rev00001"

    def test_dispatch_review_none_returns_error(self) -> None:
        client = _client()
        with (
            patch("coord.dashboard.server.read_board", return_value=self._board_with_done()),
            patch("coord.review.dispatch_review", return_value=None),
        ):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "work001",
                "action": "dispatch_review",
            })
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is False
        assert "error" in data
        assert len(data["error"]) > 0

    def test_dispatch_review_exception_returns_500(self) -> None:
        client = _client()
        with (
            patch("coord.dashboard.server.read_board", return_value=self._board_with_done()),
            patch("coord.review.dispatch_review", side_effect=RuntimeError("agent down")),
        ):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "work001",
                "action": "dispatch_review",
            })
        assert r.status_code == 500
        data = r.json()
        assert data["ok"] is False
        assert "agent down" in data["error"]

    def test_dispatch_smoke_returns_machine_and_id(self) -> None:
        smoke_assignment = Assignment(
            machine_name="gpu-box", repo_name="api",
            issue_number=42, issue_title="Fix auth",
            assignment_id="smk00001", status="running", type="smoke",
        )
        client = _client()
        with (
            patch("coord.dashboard.server.read_board", return_value=self._board_with_done()),
            patch("coord.smoke.dispatch_smoke", return_value=smoke_assignment),
            patch("coord.dashboard.server.write_board"),
        ):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "work001",
                "action": "dispatch_smoke",
            })
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["machine_name"] == "gpu-box"
        assert data["assignment_id"] == "smk00001"

    def test_dispatch_smoke_none_returns_error(self) -> None:
        client = _client()
        with (
            patch("coord.dashboard.server.read_board", return_value=self._board_with_done()),
            patch("coord.smoke.dispatch_smoke", return_value=None),
        ):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "work001",
                "action": "dispatch_smoke",
            })
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is False
        assert "error" in data

    def test_pipeline_action_unknown_assignment(self) -> None:
        client = _client()
        with patch("coord.dashboard.server.read_board", return_value=Board()):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "doesnotexist",
                "action": "dispatch_review",
            })
        assert r.status_code == 404

    def test_pipeline_action_missing_fields(self) -> None:
        client = _client()
        r = client.post("/api/pipeline/action", json={"action": "dispatch_review"})
        assert r.status_code == 400


class TestPipelineActionTestVerdict:
    """Tests for /api/pipeline/action action='test-verdict'."""

    def _board_with_done(self) -> "Board":
        return Board(
            active=[],
            completed=[
                Assignment(
                    machine_name="laptop", repo_name="api",
                    issue_number=42, issue_title="Fix auth",
                    assignment_id="work001", status="done",
                    branch="issue-42-fix-auth",
                    finished_at=1.0,
                ),
            ],
        )

    def test_pass_verdict_records_passed(self) -> None:
        client = _client()
        with (
            patch("coord.dashboard.server.read_board", return_value=self._board_with_done()),
            patch("coord.state.record_test_verdict") as mock_rtv,
        ):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "work001",
                "action": "test-verdict",
                "verdict": "pass",
            })
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["test_state"] == "passed"
        mock_rtv.assert_called_once_with(
            assignment_id="work001",
            test_state="passed",
            test_reason=None,
            smoke_test="pass",
            smoke_test_reason=None,
        )

    def test_fail_verdict_records_failed_with_reason(self) -> None:
        client = _client()
        with (
            patch("coord.dashboard.server.read_board", return_value=self._board_with_done()),
            patch("coord.state.record_test_verdict") as mock_rtv,
        ):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "work001",
                "action": "test-verdict",
                "verdict": "fail",
                "reason": "cargo test failed on line 42",
            })
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["test_state"] == "failed"
        mock_rtv.assert_called_once_with(
            assignment_id="work001",
            test_state="failed",
            test_reason="cargo test failed on line 42",
            smoke_test="fail",
            smoke_test_reason="cargo test failed on line 42",
        )

    def test_skip_verdict_records_skipped(self) -> None:
        client = _client()
        with (
            patch("coord.dashboard.server.read_board", return_value=self._board_with_done()),
            patch("coord.state.record_test_verdict") as mock_rtv,
        ):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "work001",
                "action": "test-verdict",
                "verdict": "skip",
            })
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["test_state"] == "skipped"
        # skip does not mirror to smoke_test
        mock_rtv.assert_called_once_with(
            assignment_id="work001",
            test_state="skipped",
            test_reason=None,
            smoke_test=None,
            smoke_test_reason=None,
        )

    def test_invalid_verdict_returns_400(self) -> None:
        client = _client()
        with patch("coord.dashboard.server.read_board", return_value=self._board_with_done()):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "work001",
                "action": "test-verdict",
                "verdict": "notaverdict",
            })
        assert r.status_code == 400
        assert "error" in r.json()

    def test_exception_returns_500(self) -> None:
        client = _client()
        with (
            patch("coord.dashboard.server.read_board", return_value=self._board_with_done()),
            patch("coord.state.record_test_verdict", side_effect=RuntimeError("db locked")),
        ):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "work001",
                "action": "test-verdict",
                "verdict": "pass",
            })
        assert r.status_code == 500
        data = r.json()
        assert data["ok"] is False
        assert "db locked" in data["error"]


class TestPipelineActionRecordReviewVerdict:
    """Tests for /api/pipeline/action action='record-review-verdict'.

    The phone client sends the WORK assignment id (as returned by GET
    /api/pipeline).  The handler must look up the linked review assignment and
    write to THAT row — not the work row — because compute_pipeline reads
    findings back from the review assignment.
    """

    def _board_with_work_and_review(self) -> "Board":
        """A completed work assignment with a linked completed review assignment."""
        return Board(
            active=[],
            completed=[
                Assignment(
                    machine_name="laptop", repo_name="api",
                    issue_number=42, issue_title="Fix auth",
                    assignment_id="work001", status="done",
                    finished_at=1.0,
                ),
                Assignment(
                    machine_name="desktop", repo_name="api",
                    issue_number=42, issue_title="Fix auth",
                    assignment_id="rev001", status="done",
                    type="review",
                    review_of_assignment_id="work001",
                    finished_at=2.0,
                ),
            ],
        )

    def _board_with_work_only(self) -> "Board":
        """A work assignment with NO linked review assignment."""
        return Board(
            active=[],
            completed=[
                Assignment(
                    machine_name="laptop", repo_name="api",
                    issue_number=42, issue_title="Fix auth",
                    assignment_id="work001", status="done",
                    finished_at=1.0,
                ),
            ],
        )

    def test_approve_verdict_persists_to_review_id(self) -> None:
        """The mock must be called with the review assignment id, not the work id."""
        client = _client()
        with (
            patch("coord.dashboard.server.read_board", return_value=self._board_with_work_and_review()),
            patch("coord.notify._persist_review_findings") as mock_prf,
        ):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "work001",
                "action": "record-review-verdict",
                "verdict": "approve",
                "body": "LGTM — code is clean.",
            })
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        # Key assertion: written to rev001 (the review row), NOT work001.
        mock_prf.assert_called_once_with("rev001", "approve", "LGTM — code is clean.")

    def test_request_changes_verdict_persists_to_review_id(self) -> None:
        client = _client()
        with (
            patch("coord.dashboard.server.read_board", return_value=self._board_with_work_and_review()),
            patch("coord.notify._persist_review_findings") as mock_prf,
        ):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "work001",
                "action": "record-review-verdict",
                "verdict": "request-changes",
                "body": "Missing tests on the new endpoint.",
            })
        assert r.status_code == 200
        assert r.json()["ok"] is True
        mock_prf.assert_called_once_with(
            "rev001", "request-changes", "Missing tests on the new endpoint.",
        )

    def test_no_review_assignment_returns_404(self) -> None:
        """404 when the work assignment has no linked review assignment."""
        client = _client()
        with patch("coord.dashboard.server.read_board", return_value=self._board_with_work_only()):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "work001",
                "action": "record-review-verdict",
                "verdict": "approve",
                "body": "LGTM",
            })
        assert r.status_code == 404
        assert "no review assignment" in r.json()["error"]

    def test_invalid_verdict_returns_400(self) -> None:
        client = _client()
        with patch("coord.dashboard.server.read_board", return_value=self._board_with_work_and_review()):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "work001",
                "action": "record-review-verdict",
                "verdict": "reject",
                "body": "Some body",
            })
        assert r.status_code == 400
        assert "error" in r.json()

    def test_missing_body_returns_400(self) -> None:
        client = _client()
        with patch("coord.dashboard.server.read_board", return_value=self._board_with_work_and_review()):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "work001",
                "action": "record-review-verdict",
                "verdict": "approve",
            })
        assert r.status_code == 400
        assert "body" in r.json()["error"]

    def test_exception_returns_500(self) -> None:
        client = _client()
        with (
            patch("coord.dashboard.server.read_board", return_value=self._board_with_work_and_review()),
            patch("coord.notify._persist_review_findings", side_effect=RuntimeError("db locked")),
        ):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "work001",
                "action": "record-review-verdict",
                "verdict": "approve",
                "body": "LGTM",
            })
        assert r.status_code == 500
        assert r.json()["ok"] is False


class TestPipelineReviewFindings:
    """Tests that GET /api/pipeline includes review_verdict and review_findings_body."""

    def _board_with_review(self) -> "Board":
        # #2066: recent, not epoch, timestamps — /api/pipeline now bounds its
        # default response to a recency window, and these tests are about the
        # review-verdict fields, not about that window.
        now = time.time()
        work = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=42, issue_title="Fix auth",
            assignment_id="work001", status="done",
            branch="issue-42-fix-auth",
            finished_at=now,
        )
        review = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=42, issue_title="Fix auth",
            assignment_id="rev001", status="done",
            type="review",
            review_of_assignment_id="work001",
            review_verdict="approve",
            review_posted_at=now,
            finished_at=now,
        )
        return Board(active=[], completed=[work, review])

    def test_review_verdict_and_body_in_pipeline_response(self) -> None:
        client = _client()
        with (
            patch("coord.dashboard.server.read_board", return_value=self._board_with_review()),
            patch("coord.merge_queue.load_queue", return_value=[]),
            patch(
                "coord.state.load_assignment_review_findings",
                return_value=("approve", "LGTM — clean diff."),
            ),
        ):
            r = client.get("/api/pipeline")
        assert r.status_code == 200
        items = r.json()
        # Only work assignments appear in the pipeline view.
        assert len(items) == 1
        item = items[0]
        assert item["assignment_id"] == "work001"
        assert item["review_verdict"] == "approve"
        assert item["review_findings_body"] == "LGTM — clean diff."

    def test_no_review_assignment_yields_none_findings(self) -> None:
        """A work assignment with no review yet has None verdict + body."""
        work = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=99, issue_title="Standalone",
            assignment_id="work002", status="done",
            finished_at=time.time(),
        )
        board = Board(active=[], completed=[work])
        client = _client()
        with (
            patch("coord.dashboard.server.read_board", return_value=board),
            patch("coord.merge_queue.load_queue", return_value=[]),
            patch("coord.state.load_assignment_review_findings") as mock_lrf,
        ):
            r = client.get("/api/pipeline")
        assert r.status_code == 200
        items = r.json()
        assert len(items) == 1
        assert items[0]["review_verdict"] is None
        assert items[0]["review_findings_body"] is None
        # load_assignment_review_findings must not be called when there's no review.
        mock_lrf.assert_not_called()


class TestDashboardDispatchUI:
    """Tests confirming the HTML includes the new dispatch-feedback elements."""

    def test_dispatch_status_css_present(self) -> None:
        client = _client()
        r = client.get("/")
        assert "dispatch-status" in r.text
        assert "dispatch-pending" in r.text
        assert "dispatch-ok" in r.text
        assert "dispatch-err" in r.text

    def test_pipeline_area_wrapper_in_source(self) -> None:
        client = _client()
        r = client.get("/")
        assert "pipeline-area" in r.text

    def test_dispatch_status_js_in_source(self) -> None:
        client = _client()
        r = client.get("/")
        assert "dispatchStatus" in r.text
        assert "renderDispatchStatus" in r.text
        assert "updateCardPipeline" in r.text
        assert "Dispatching #" in r.text
        assert "✓ Dispatched" in r.text
        assert "✗ Dispatch failed" in r.text


class TestXSSSafety:
    def test_html_served_has_escape_function(self) -> None:
        client = _client()
        r = client.get("/")
        assert "const E = " in r.text

    def test_board_data_does_not_appear_unescaped_in_source(self) -> None:
        client = _client()
        r = client.get("/")
        assert "${a.issue_title}" not in r.text
        assert "E(a.issue_title)" in r.text


# ── _poll_once unit tests ────────────────────────────────────────────────────


class TestPollOnce:
    """Unit tests for the module-level _poll_once function.

    Each test drives _poll_once directly with a fake board and mocked
    _fetch_agent_status so no real network calls are made.
    """

    def _make_config(self) -> Config:
        return Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"])],
        )

    def _running_board(self, aid: str = "abc") -> Board:
        return Board(
            active=[
                Assignment(
                    machine_name="laptop", repo_name="api",
                    issue_number=42, issue_title="Fix auth",
                    assignment_id=aid, status="running",
                    dispatched_at=1.0,
                ),
            ],
        )

    def _agent_resp(self, aid: str, status: str) -> dict:
        return {
            "active": [],
            "completed": [{"id": aid, "status": status}],
        }

    def test_running_to_done_fires_assignment_completed(self) -> None:
        from coord.dashboard.server import _poll_once
        from coord.events import ASSIGNMENT_COMPLETED, EventSource

        config = self._make_config()
        es = EventSource()
        seen: set[str] = set()
        orphaned: dict[str, float] = {}
        board = self._running_board("abc")

        with patch(
            "coord.dashboard.server._fetch_agent_status",
            return_value=self._agent_resp("abc", "done"),
        ):
            asyncio.run(_poll_once(config, es, seen, orphaned, board=board, now=1000.0))

        assert len(es._history) == 1
        assert es._history[0].type == ASSIGNMENT_COMPLETED
        assert "abc" in seen

    def test_running_to_failed_fires_assignment_failed(self) -> None:
        from coord.dashboard.server import _poll_once
        from coord.events import ASSIGNMENT_FAILED, EventSource

        config = self._make_config()
        es = EventSource()
        seen: set[str] = set()
        orphaned: dict[str, float] = {}
        board = self._running_board("xyz")

        with patch(
            "coord.dashboard.server._fetch_agent_status",
            return_value=self._agent_resp("xyz", "failed"),
        ):
            asyncio.run(_poll_once(config, es, seen, orphaned, board=board, now=1000.0))

        assert len(es._history) == 1
        assert es._history[0].type == ASSIGNMENT_FAILED

    def test_running_to_cancelled_fires_assignment_cancelled(self) -> None:
        """Bug 1 regression: cancelled must not fire ASSIGNMENT_FAILED."""
        from coord.dashboard.server import ASSIGNMENT_CANCELLED, _poll_once
        from coord.events import EventSource

        config = self._make_config()
        es = EventSource()
        seen: set[str] = set()
        orphaned: dict[str, float] = {}
        board = self._running_board("ccc")

        with patch(
            "coord.dashboard.server._fetch_agent_status",
            return_value=self._agent_resp("ccc", "cancelled"),
        ):
            asyncio.run(_poll_once(config, es, seen, orphaned, board=board, now=1000.0))

        assert len(es._history) == 1
        assert es._history[0].type == ASSIGNMENT_CANCELLED

    def test_running_to_advisory_fires_assignment_advisory(self) -> None:
        """#448 regression: advisory must fire ASSIGNMENT_ADVISORY, not FAILED.

        Without the dashboard fix, advisory fell through to the else-branch
        and emitted a red ASSIGNMENT_FAILED toast for a clean 0-commit exit.
        """
        from coord.dashboard.server import ASSIGNMENT_ADVISORY, _poll_once
        from coord.events import ASSIGNMENT_FAILED, EventSource

        config = self._make_config()
        es = EventSource()
        seen: set[str] = set()
        orphaned: dict[str, float] = {}
        board = self._running_board("adv1")

        agent_resp = {
            "active": [],
            "completed": [{
                "id": "adv1",
                "status": "advisory",
                "zero_commit_reason": "worker exited cleanly but pushed 0 commits",
            }],
        }
        with patch(
            "coord.dashboard.server._fetch_agent_status",
            return_value=agent_resp,
        ):
            asyncio.run(_poll_once(config, es, seen, orphaned, board=board, now=1000.0))

        assert len(es._history) == 1
        event = es._history[0]
        assert event.type == ASSIGNMENT_ADVISORY, (
            f"expected {ASSIGNMENT_ADVISORY}, got {event.type!r} — "
            "advisory must not be routed to ASSIGNMENT_FAILED"
        )
        assert event.type != ASSIGNMENT_FAILED
        # zero_commit_reason should be carried through to the client.
        assert event.data.get("zero_commit_reason") == (
            "worker exited cleanly but pushed 0 commits"
        )

    def test_running_to_refused_policy_fires_assignment_refused_policy(self) -> None:
        """#2234 regression: refused_policy must fire ASSIGNMENT_REFUSED_POLICY,
        not FAILED.

        Before this fix, refused_policy matched none of the named branches
        ("done"/"cancelled"/"advisory") and fell into the `else` arm —
        explicitly commented "'failed' and any other unexpected terminal
        status" — publishing ASSIGNMENT_FAILED for a worker that correctly
        refused a CLAUDE.md-prohibited task. That reproduces #2234's own
        headline defect on the phone dashboard.
        """
        from coord.dashboard.server import (
            ASSIGNMENT_REFUSED_POLICY,
            _poll_once,
        )
        from coord.events import ASSIGNMENT_FAILED, EventSource

        config = self._make_config()
        es = EventSource()
        seen: set[str] = set()
        orphaned: dict[str, float] = {}
        board = self._running_board("rp1")

        agent_resp = {
            "active": [],
            "completed": [{
                "id": "rp1",
                "status": "refused_policy",
                "policy_refusal_reason": (
                    "CLAUDE.md line 156: only the coordinator writes docs"
                ),
            }],
        }
        with patch(
            "coord.dashboard.server._fetch_agent_status",
            return_value=agent_resp,
        ):
            asyncio.run(_poll_once(config, es, seen, orphaned, board=board, now=1000.0))

        assert len(es._history) == 1
        event = es._history[0]
        assert event.type == ASSIGNMENT_REFUSED_POLICY, (
            f"expected {ASSIGNMENT_REFUSED_POLICY}, got {event.type!r} — "
            "refused_policy must not be routed to ASSIGNMENT_FAILED"
        )
        assert event.type != ASSIGNMENT_FAILED
        # policy_refusal_reason should be carried through to the client.
        assert event.data.get("policy_refusal_reason") == (
            "CLAUDE.md line 156: only the coordinator writes docs"
        )

    def test_absent_over_threshold_appears_in_possibly_stuck(self) -> None:
        """An assignment absent from agent data past the threshold is stuck."""
        from coord.dashboard.server import _poll_once
        from coord.events import EventSource

        config = self._make_config()
        es = EventSource()
        seen: set[str] = set()
        orphaned: dict[str, float] = {}
        board = self._running_board("stuck1")

        # Agent is reachable but knows nothing about "stuck1".
        agent_resp = {"active": [], "completed": []}
        with patch(
            "coord.dashboard.server._fetch_agent_status",
            return_value=agent_resp,
        ):
            # dispatched_at=1.0, now=1000.0 → 999 s > _STUCK_THRESHOLD (300 s)
            result = asyncio.run(
                _poll_once(config, es, seen, orphaned, board=board, now=1000.0)
            )

        ids = [r["assignment_id"] for r in result]
        assert "stuck1" in ids

    def test_absent_under_threshold_not_in_possibly_stuck(self) -> None:
        """An assignment absent from agent data under the threshold is not stuck."""
        from coord.dashboard.server import _poll_once
        from coord.events import EventSource

        config = self._make_config()
        es = EventSource()
        seen: set[str] = set()
        orphaned: dict[str, float] = {}
        board = self._running_board("fresh1")

        agent_resp = {"active": [], "completed": []}
        with patch(
            "coord.dashboard.server._fetch_agent_status",
            return_value=agent_resp,
        ):
            # dispatched_at=1.0, now=100.0 → 99 s < _STUCK_THRESHOLD (300 s)
            result = asyncio.run(
                _poll_once(config, es, seen, orphaned, board=board, now=100.0)
            )

        ids = [r["assignment_id"] for r in result]
        assert "fresh1" not in ids

    def test_seen_terminal_prevents_refiring(self) -> None:
        """An assignment already in seen_terminal must not query the agent again."""
        from coord.dashboard.server import _poll_once
        from coord.events import EventSource

        config = self._make_config()
        es = EventSource()
        # Pre-populate seen_terminal with the assignment's id.
        seen: set[str] = {"abc"}
        orphaned: dict[str, float] = {}
        board = self._running_board("abc")

        with patch(
            "coord.dashboard.server._fetch_agent_status",
        ) as mock_fetch:
            asyncio.run(_poll_once(config, es, seen, orphaned, board=board, now=1000.0))

        # running set will be empty because aid is in seen_terminal → early return
        mock_fetch.assert_not_called()
        assert len(es._history) == 0


# ── #846: needs_attention live SSE toast ────────────────────────────────────


class TestPollOnceNeedsAttention:
    """`_poll_once`'s optional live counterpart to the coordinator's
    GitHub-comment backstop (coord.notify.detect_needs_attention) — same
    coord.notify.attention_signal core, gated behind needs_attention_seen so
    callers that don't pass it (e.g. the pre-#846 tests above) see no
    behaviour change."""

    def _make_config(self) -> Config:
        return Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"])],
        )

    def _running_board(self, aid: str = "abc", dispatched_at: float = 1.0) -> Board:
        return Board(
            active=[
                Assignment(
                    machine_name="laptop", repo_name="api",
                    issue_number=42, issue_title="Fix auth",
                    assignment_id=aid, status="running",
                    dispatched_at=dispatched_at,
                ),
            ],
        )

    def test_omitted_seen_set_skips_the_check_entirely(self) -> None:
        """Backward compat: no needs_attention_seen kwarg -> no new event,
        even for an assignment that's been running for hours."""
        from coord.dashboard.server import ASSIGNMENT_NEEDS_ATTENTION, _poll_once
        from coord.events import EventSource

        config = self._make_config()
        es = EventSource()
        board = self._running_board("abc", dispatched_at=0.0)

        with patch("coord.dashboard.server._fetch_agent_status", return_value=None):
            asyncio.run(_poll_once(config, es, {}, {}, board=board, now=100000.0))

        assert not any(e.type == ASSIGNMENT_NEEDS_ATTENTION for e in es._history)

    def test_wall_clock_over_threshold_fires_once(self) -> None:
        from coord.dashboard.server import ASSIGNMENT_NEEDS_ATTENTION, _poll_once
        from coord.events import EventSource

        config = self._make_config()
        es = EventSource()
        board = self._running_board("abc", dispatched_at=0.0)
        seen: set[str] = set()

        with patch("coord.dashboard.server._fetch_agent_status", return_value=None):
            # Past the default 45m "work" threshold.
            asyncio.run(_poll_once(
                config, es, set(), {}, board=board, now=100000.0,
                needs_attention_seen=seen,
            ))
            assert "abc" in seen
            attn_events = [e for e in es._history if e.type == ASSIGNMENT_NEEDS_ATTENTION]
            assert len(attn_events) == 1
            assert attn_events[0].data["reason"] == "wall_clock"

            # A second poll must not re-fire for the same assignment.
            asyncio.run(_poll_once(
                config, es, set(), {}, board=board, now=100001.0,
                needs_attention_seen=seen,
            ))
            attn_events = [e for e in es._history if e.type == ASSIGNMENT_NEEDS_ATTENTION]
            assert len(attn_events) == 1

    def test_under_threshold_does_not_fire(self) -> None:
        from coord.dashboard.server import ASSIGNMENT_NEEDS_ATTENTION, _poll_once
        from coord.events import EventSource

        config = self._make_config()
        es = EventSource()
        board = self._running_board("abc", dispatched_at=1000.0)
        seen: set[str] = set()

        with patch("coord.dashboard.server._fetch_agent_status", return_value=None):
            asyncio.run(_poll_once(
                config, es, set(), {}, board=board, now=1010.0,
                needs_attention_seen=seen,
            ))

        assert not any(e.type == ASSIGNMENT_NEEDS_ATTENTION for e in es._history)
        assert seen == set()


# ── Bug regression tests (HTML/JS) ──────────────────────────────────────────


class TestBugFixes:
    """Regression tests that ensure the HTML/JS bug fixes stay in place."""

    def test_bug2_toast_uses_textcontent_not_e(self) -> None:
        """Bug 2: showToast uses textContent so E() in toast strings double-encodes."""
        client = _client()
        r = client.get("/")
        assert r.status_code == 200
        # The fixed code passes plain d.repo_name / d.machine_name, not E(...).
        assert "E(d.repo_name)" not in r.text
        assert "E(d.machine_name)" not in r.text

    def test_bug3_cost_null_guard(self) -> None:
        """Bug 3: a cost of $0 was hidden by the falsy check; fix uses != null."""
        client = _client()
        r = client.get("/")
        assert r.status_code == 200
        # The fixed code uses optional-chain + != null for both stats fields.
        assert "!= null" in r.text

    def test_bug4_no_invalid_css_title_property(self) -> None:
        """Bug 4: `title:` is not a valid CSS property — must not appear in <style>."""
        client = _client()
        r = client.get("/")
        assert r.status_code == 200
        # Extract the style block and confirm `title:` is absent.
        import re
        style_blocks = re.findall(r"<style[^>]*>(.*?)</style>", r.text, re.DOTALL)
        for block in style_blocks:
            assert "title:" not in block, (
                f"Found invalid CSS `title:` property in <style> block"
            )

    def test_bug6_bell_toggle_present(self) -> None:
        """Bug 6: bell toggle button, function, and localStorage persistence."""
        client = _client()
        r = client.get("/")
        assert r.status_code == 200
        assert "bell-btn" in r.text
        assert "toggleBell" in r.text
        assert "bellEnabled" in r.text
        assert "localStorage" in r.text

    def test_bug1_cancelled_event_handled_in_html(self) -> None:
        """Bug 1: client-side listener for assignment_cancelled must exist."""
        client = _client()
        r = client.get("/")
        assert r.status_code == 200
        assert "assignment_cancelled" in r.text

    def test_bug1_assignment_cancelled_constant_exported(self) -> None:
        """Bug 1: ASSIGNMENT_CANCELLED must be importable from server module."""
        from coord.dashboard.server import ASSIGNMENT_CANCELLED
        assert ASSIGNMENT_CANCELLED == "assignment_cancelled"


class TestCLI:
    def test_web_help(self) -> None:
        from click.testing import CliRunner
        from coord.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["web", "--help"])
        assert result.exit_code == 0
        assert "7434" in result.output

    def test_web_help_documents_dist_override(self) -> None:
        """#1543: --dist / $COORD_WEB_DIST must be discoverable from --help --
        it's the seam a build hook uses to serve merged main without
        upgrading ~/.coord-venv."""
        from click.testing import CliRunner
        from coord.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["web", "--help"])
        assert result.exit_code == 0
        assert "--dist" in result.output
        assert "COORD_WEB_DIST" in result.output

    def test_web_dist_flag_passed_to_build_app(self, tmp_path: Path, coord_db) -> None:
        """`coord web --dist PATH` must thread PATH through to
        build_app(dist_path=...) rather than being accepted and ignored."""
        from click.testing import CliRunner
        from coord.cli import main

        config_file = tmp_path / "coordinator.yml"
        config_file.write_text(
            "repos:\n"
            "  - name: api\n"
            "    github: acme/api\n"
            "    default_branch: main\n"
            "machines:\n"
            "  - name: laptop\n"
            "    host: laptop.tailnet\n"
            "    repos: [api]\n"
            "    repo_paths:\n"
            "      api: /tmp/api\n"
        )

        captured = {}

        def _fake_build_app(config, *, token=None, session_attacher=None, fixture=None, dist_path=None):
            captured["dist_path"] = dist_path
            raise SystemExit(0)  # bail out before uvicorn.run would block

        runner = CliRunner()
        with patch("coord.dashboard.server.build_app", _fake_build_app):
            runner.invoke(
                main,
                ["web", "--config", str(config_file), "--dist", "/tmp/some-dist-dir"],
                catch_exceptions=True,
            )
        assert captured.get("dist_path") == Path("/tmp/some-dist-dir")

    @staticmethod
    def _config_file(tmp_path: Path) -> Path:
        config_file = tmp_path / "coordinator.yml"
        config_file.write_text(
            "repos:\n"
            "  - name: api\n"
            "    github: acme/api\n"
            "    default_branch: main\n"
            "machines:\n"
            "  - name: laptop\n"
            "    host: laptop.tailnet\n"
            "    repos: [api]\n"
            "    repo_paths:\n"
            "      api: /tmp/api\n"
        )
        return config_file

    def test_web_dist_env_var_honored(self, tmp_path: Path, coord_db) -> None:
        """#2003: ``$COORD_WEB_DIST`` alone (no ``--dist`` flag) must thread
        through to ``build_app(dist_path=...)`` -- the env-var half of the
        seam a systemd unit / build-hook timer relies on, since it can't pass
        a CLI flag."""
        from click.testing import CliRunner
        from coord.cli import main

        config_file = self._config_file(tmp_path)
        env_dist = tmp_path / "env-dist"

        captured = {}

        def _fake_build_app(config, *, token=None, session_attacher=None, fixture=None, dist_path=None):
            captured["dist_path"] = dist_path
            raise SystemExit(0)

        runner = CliRunner()
        with patch("coord.dashboard.server.build_app", _fake_build_app):
            runner.invoke(
                main,
                ["web", "--config", str(config_file)],
                env={"COORD_WEB_DIST": str(env_dist)},
                catch_exceptions=True,
            )
        assert captured.get("dist_path") == env_dist

    def test_web_dist_flag_overrides_env_var(self, tmp_path: Path, coord_db) -> None:
        """#2003: precedence when both are set -- ``--dist`` wins over
        ``$COORD_WEB_DIST`` (click's normal flag-over-envvar resolution,
        pinned here since this is the exact seam a misconfigured deploy
        would trip over silently)."""
        from click.testing import CliRunner
        from coord.cli import main

        config_file = self._config_file(tmp_path)
        env_dist = tmp_path / "env-dist"
        flag_dist = tmp_path / "flag-dist"

        captured = {}

        def _fake_build_app(config, *, token=None, session_attacher=None, fixture=None, dist_path=None):
            captured["dist_path"] = dist_path
            raise SystemExit(0)

        runner = CliRunner()
        with patch("coord.dashboard.server.build_app", _fake_build_app):
            runner.invoke(
                main,
                ["web", "--config", str(config_file), "--dist", str(flag_dist)],
                env={"COORD_WEB_DIST": str(env_dist)},
                catch_exceptions=True,
            )
        assert captured.get("dist_path") == flag_dist
        assert captured.get("dist_path") != env_dist

    def test_web_dist_missing_prints_loud_warning(self, tmp_path: Path, coord_db) -> None:
        """#2003: the operationally important case -- ``--dist`` pointing at
        a directory that doesn't exist must print a clear warning, not the
        same success message a valid ``--dist`` gets. Before this fix `coord
        web` printed "serving webapp bundle from {path}" unconditionally,
        even though the server was silently falling back to the legacy
        single-file dashboard -- exactly the invisible regression #2003
        warns about."""
        from click.testing import CliRunner
        from coord.cli import main

        config_file = self._config_file(tmp_path)
        missing_dist = tmp_path / "does-not-exist"

        runner = CliRunner()
        # Let the real build_app run (cheap, no network); only bail out
        # before uvicorn.run would block forever serving the app.
        with patch("uvicorn.run", side_effect=SystemExit(0)):
            result = runner.invoke(
                main,
                ["web", "--config", str(config_file), "--dist", str(missing_dist)],
                catch_exceptions=True,
            )
        assert "warning" in result.output.lower()
        assert str(missing_dist) in result.output
        assert "legacy" in result.output.lower()
        assert f"serving webapp bundle from {missing_dist}" not in result.output

    def test_web_dist_empty_dir_prints_loud_warning(self, tmp_path: Path, coord_db) -> None:
        """Same as above but the directory exists and is merely empty (no
        index.html) -- an interrupted/failed build, not a typo'd path."""
        from click.testing import CliRunner
        from coord.cli import main

        config_file = self._config_file(tmp_path)
        empty_dist = tmp_path / "empty-dist"
        empty_dist.mkdir()

        runner = CliRunner()
        with patch("uvicorn.run", side_effect=SystemExit(0)):
            result = runner.invoke(
                main,
                ["web", "--config", str(config_file), "--dist", str(empty_dist)],
                catch_exceptions=True,
            )
        assert "warning" in result.output.lower()
        assert f"serving webapp bundle from {empty_dist}" not in result.output

    def test_web_dist_valid_prints_success_not_warning(self, tmp_path: Path, coord_db) -> None:
        """Contrast case: a valid ``--dist`` (real index.html present) keeps
        printing the plain success message with no warning noise."""
        from click.testing import CliRunner
        from coord.cli import main

        config_file = self._config_file(tmp_path)
        valid_dist = tmp_path / "valid-dist"
        valid_dist.mkdir()
        (valid_dist / "index.html").write_text("<html></html>")

        runner = CliRunner()
        with patch("uvicorn.run", side_effect=SystemExit(0)):
            result = runner.invoke(
                main,
                ["web", "--config", str(config_file), "--dist", str(valid_dist)],
                catch_exceptions=True,
            )
        assert f"serving webapp bundle from {valid_dist}" in result.output
        # No dist-related warning noise -- the unrelated "no bearer token"
        # warning is expected (no --token passed) and out of scope here.
        assert "no index.html" not in result.output.lower()
        assert "falling back to the legacy" not in result.output.lower()


class TestSSEEvents:
    """Tests for /events SSE endpoint (issue #214)."""

    def test_events_route_is_registered(self) -> None:
        """The /events route must exist in the dashboard app.

        We verify by inspecting the Starlette app routes directly — streaming
        the body would block the test because SSE never closes on the server.
        """
        from coord.dashboard.server import build_app as _build_app
        app = _build_app(_config())
        route_paths = [
            getattr(r, "path", None)
            for r in getattr(app, "routes", [])
        ]
        assert "/events" in route_paths, f"Expected /events in routes, got: {route_paths}"

    def test_events_html_includes_sse_connection(self) -> None:
        """The HTML must include the SSE connection code."""
        client = _client()
        r = client.get("/")
        assert r.status_code == 200
        assert "connectSSE" in r.text
        assert "EventSource" in r.text
        assert "/events" in r.text

    def test_html_includes_toast_system(self) -> None:
        """The HTML must include toast notification elements."""
        client = _client()
        r = client.get("/")
        assert r.status_code == 200
        assert "toast-container" in r.text
        assert "showToast" in r.text
        assert "toast-done" in r.text
        assert "toast-failed" in r.text

    def test_html_includes_audio_bell(self) -> None:
        """The HTML must include audio bell code."""
        client = _client()
        r = client.get("/")
        assert r.status_code == 200
        assert "playBell" in r.text
        assert "AudioContext" in r.text

    def test_html_includes_sse_dot_indicator(self) -> None:
        """The HTML must show a live-events connection status indicator."""
        client = _client()
        r = client.get("/")
        assert r.status_code == 200
        assert "sse-dot" in r.text

    def test_html_includes_stuck_detection(self) -> None:
        """The HTML must show possibly-stuck warning and unstick button."""
        client = _client()
        r = client.get("/")
        assert r.status_code == 200
        assert "possibly_stuck" in r.text
        assert "stuck-banner" in r.text
        assert "btn-unstick" in r.text
        assert "Cancel" in r.text


class TestUnstickAction:
    """Tests for the 'unstick' pipeline action (issue #214)."""

    def _board_with_running(self) -> "Board":
        import time as _t
        return Board(
            active=[
                Assignment(
                    machine_name="laptop", repo_name="api",
                    issue_number=42, issue_title="Fix auth",
                    assignment_id="run001", status="running",
                    dispatched_at=_t.time() - 600,  # 10 min ago
                ),
            ],
        )

    def test_unstick_marks_failed_and_returns_ok(self) -> None:
        client = _client()
        board = self._board_with_running()
        with (
            patch("coord.dashboard.server.read_board", return_value=board),
            patch("coord.dashboard.server.write_board"),
            patch("coord.dashboard.server.httpx.post", side_effect=Exception("unreachable")),
        ):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "run001",
                "action": "unstick",
            })
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["cancelled_on_agent"] is False
        # Assignment should be marked failed in the board
        a = board.find_by_id("run001")
        assert a is not None
        assert a.status == "failed"

    def test_unstick_cancelled_on_agent_when_reachable(self) -> None:
        client = _client()
        board = self._board_with_running()
        mock_response = MagicMock()
        mock_response.status_code = 200
        with (
            patch("coord.dashboard.server.read_board", return_value=board),
            patch("coord.dashboard.server.write_board"),
            patch("coord.dashboard.server.httpx.post", return_value=mock_response),
        ):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "run001",
                "action": "unstick",
            })
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["cancelled_on_agent"] is True

    def test_unstick_unknown_assignment_returns_404(self) -> None:
        client = _client()
        with patch("coord.dashboard.server.read_board", return_value=Board()):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "doesnotexist",
                "action": "unstick",
            })
        assert r.status_code == 404


class TestSPAServing:
    """Verify the built React webapp is served correctly when dist/ exists."""

    def _make_dist(self, tmp_path: Path, index_content: str = "<html><title>coord dashboard</title></html>") -> Path:
        """Create a minimal fake dist/ directory."""
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "index.html").write_text(index_content)
        return dist

    def test_serves_webapp_index_when_dist_exists(self, tmp_path: Path) -> None:
        """When webapp/dist/ is present, GET / returns the SPA index.html."""
        dist = self._make_dist(tmp_path, "<html><title>coord dashboard spa</title></html>")
        with patch("coord.dashboard.server.WEBAPP_DIST", dist):
            client = TestClient(build_app(_config()))
            r = client.get("/")
        assert r.status_code == 200
        assert "coord dashboard spa" in r.text

    def test_api_routes_unaffected_when_dist_exists(self, tmp_path: Path) -> None:
        """All /api/* routes continue to work normally when the webapp dist is present."""
        dist = self._make_dist(tmp_path)
        board = Board(round_number=99)
        with patch("coord.dashboard.server.WEBAPP_DIST", dist):
            client = TestClient(build_app(_config()))
            with patch("coord.dashboard.server.read_board", return_value=board):
                r = client.get("/api/board")
        assert r.status_code == 200
        assert r.json()["round_number"] == 99

    def test_spa_catch_all_serves_index_for_unknown_paths(self, tmp_path: Path) -> None:
        """SPA client-side routes (e.g. /pipeline) return index.html for React Router."""
        dist = self._make_dist(tmp_path, "<html><title>coord spa route</title></html>")
        with patch("coord.dashboard.server.WEBAPP_DIST", dist):
            client = TestClient(build_app(_config()))
            r = client.get("/pipeline")
        assert r.status_code == 200
        assert "coord spa route" in r.text

    def test_static_assets_served_from_dist_assets(self, tmp_path: Path) -> None:
        """Vite hashed bundles under /assets/ are served directly."""
        dist = self._make_dist(tmp_path)
        assets = dist / "assets"
        assets.mkdir()
        (assets / "index.abc123.js").write_text("// bundle")
        with patch("coord.dashboard.server.WEBAPP_DIST", dist):
            client = TestClient(build_app(_config()))
            r = client.get("/assets/index.abc123.js")
        assert r.status_code == 200

    def test_dist_root_static_files_served(self, tmp_path: Path) -> None:
        """sw.js and manifest.webmanifest from dist/ root are served as files."""
        dist = self._make_dist(tmp_path)
        (dist / "sw.js").write_text("// service worker")
        (dist / "manifest.webmanifest").write_text('{"name":"coord"}')
        with patch("coord.dashboard.server.WEBAPP_DIST", dist):
            client = TestClient(build_app(_config()))
            sw = client.get("/sw.js")
            mf = client.get("/manifest.webmanifest")
        assert sw.status_code == 200
        assert "service worker" in sw.text
        assert mf.status_code == 200
        assert "coord" in mf.text

    def test_falls_back_to_legacy_dashboard_when_no_dist(self) -> None:
        """With no bundle, the legacy dashboard is still fully served."""
        # WEBAPP_DIST is patched to a non-existent path by the autouse
        # fixture at the top of this module.
        client = _client()
        r = client.get("/")
        assert r.status_code == 200
        # The legacy dashboard always contains "coord dashboard" in its markup.
        assert "coord dashboard" in r.text

    def test_unknown_api_path_returns_404_json(self, tmp_path: Path) -> None:
        """#3042: an unregistered /api/* path must 404 with a JSON body, not
        the SPA's index.html — otherwise a missing endpoint is
        indistinguishable from a working one at the HTTP layer."""
        dist = self._make_dist(tmp_path, "<html><title>coord spa route</title></html>")
        with patch("coord.dashboard.server.WEBAPP_DIST", dist):
            client = TestClient(build_app(_config()))
            r = client.get("/api/this-route-does-not-exist")
        assert r.status_code == 404
        assert r.headers["content-type"].startswith("application/json")
        assert "coord spa route" not in r.text

    def test_unknown_nested_api_path_returns_404_json(self, tmp_path: Path) -> None:
        """Same guard for a deeper unregistered /api/ path, matching the
        machines-panel endpoints from #3042 (e.g. /api/machines/x/metrics)."""
        dist = self._make_dist(tmp_path)
        with patch("coord.dashboard.server.WEBAPP_DIST", dist):
            client = TestClient(build_app(_config()))
            r = client.get("/api/machines/precision/metrics")
        assert r.status_code == 404
        assert r.headers["content-type"].startswith("application/json")

    def test_non_api_unknown_path_still_serves_spa(self, tmp_path: Path) -> None:
        """The /api/ 404 guard must not regress client-side routing: a
        non-API unknown path still falls through to index.html."""
        dist = self._make_dist(tmp_path, "<html><title>coord spa route</title></html>")
        with patch("coord.dashboard.server.WEBAPP_DIST", dist):
            client = TestClient(build_app(_config()))
            r = client.get("/issues/42")
        assert r.status_code == 200
        assert "coord spa route" in r.text


class TestWebappBundleMissingSignal:
    """#2009: "no bundle" must never be a silent 200.

    Before the split, an absent bundle meant "you didn't run ``npm run
    build``" — a dev-only state, and the wheel shipped one anyway. Now the
    bundle arrives on a timer from a different repo (``coord-web``), so an
    absent bundle means a *delivery lane is broken*, and the old behaviour —
    serve the legacy single-file dashboard, 200, no signal anywhere —
    renders a broken lane as a working page. That is the same
    silent-staleness class as the ``~/.coord-cli-venv`` incident.

    Three independent signals are asserted below, one per audience: a
    journal line for an unattended host, a banner for whoever is looking at
    the page, and a response header for anything scripted.
    """

    def _make_dist(self, tmp_path: Path) -> Path:
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "index.html").write_text("<html><title>real bundle</title></html>")
        return dist

    def test_default_dist_is_not_a_path_inside_the_installed_package(self) -> None:
        """The packaged ``coord/dashboard/webapp/dist`` is GONE (#2009).

        Pointing the default back inside the package would guarantee a
        permanent miss with nothing an operator could do to fix it, since
        neither this repo nor the wheel contains that directory any more.
        """
        from coord.dashboard.server import DASHBOARD_DIR, DEFAULT_WEBAPP_DIST

        assert DEFAULT_WEBAPP_DIST == "~/coord-web-dist"
        resolved = Path(DEFAULT_WEBAPP_DIST).expanduser().resolve()
        assert not str(resolved).startswith(str(DASHBOARD_DIR.resolve())), (
            f"default bundle path {resolved} is inside the installed package "
            "— the vendored webapp bundle was removed in #2009"
        )
        assert not (DASHBOARD_DIR / "webapp").exists(), (
            "coord/dashboard/webapp/ is back in this repo — it belongs to "
            "the coord-web repo (#2009, epic #2002)"
        )

    def test_default_matches_the_path_the_health_check_grades(self) -> None:
        """The server and ``coord release verify``'s ``webapp_bundle`` lane
        must agree on where the bundle lives, or the fleet grades staleness
        on a directory nothing is serving."""
        from coord.dashboard.server import DEFAULT_WEBAPP_DIST
        from coord.health.checks.deploy_lane_facts import _DEFAULT_WEBAPP_DIST

        assert DEFAULT_WEBAPP_DIST == _DEFAULT_WEBAPP_DIST

    def test_missing_bundle_sets_the_missing_header(self) -> None:
        from coord.dashboard.server import WEBAPP_BUNDLE_HEADER, WEBAPP_BUNDLE_MISSING

        r = _client().get("/")
        assert r.status_code == 200
        assert r.headers[WEBAPP_BUNDLE_HEADER] == WEBAPP_BUNDLE_MISSING

    def test_missing_bundle_renders_an_in_page_banner(self) -> None:
        r = _client().get("/")
        assert 'id="coord-webapp-bundle-missing"' in r.text
        assert "coord-web" in r.text
        # ...without cannibalising the legacy dashboard it is annotating.
        assert "coord dashboard" in r.text

    def test_missing_bundle_logs_a_warning_at_startup(self, caplog) -> None:
        """The unattended-host signal: a host whose bundle lane died says so
        in ``journalctl --user -u coord-web`` with nobody watching."""
        import logging

        with caplog.at_level(logging.WARNING, logger="coord.dashboard.server"):
            build_app(_config())
        assert any(
            "no coord-web bundle" in record.getMessage() for record in caplog.records
        ), [r.getMessage() for r in caplog.records]

    def test_present_bundle_names_itself_in_the_header_and_skips_the_banner(
        self, tmp_path: Path
    ) -> None:
        """The contrast case — a working bundle must stay completely clean."""
        from coord.dashboard.server import WEBAPP_BUNDLE_HEADER

        dist = self._make_dist(tmp_path)
        with patch("coord.dashboard.server.WEBAPP_DIST", dist):
            r = TestClient(build_app(_config())).get("/")
        assert r.status_code == 200
        assert r.headers[WEBAPP_BUNDLE_HEADER] == str(dist)
        assert "real bundle" in r.text
        assert "coord-webapp-bundle-missing" not in r.text

    def test_present_bundle_logs_no_warning(self, tmp_path: Path, caplog) -> None:
        import logging

        dist = self._make_dist(tmp_path)
        with caplog.at_level(logging.WARNING, logger="coord.dashboard.server"):
            with patch("coord.dashboard.server.WEBAPP_DIST", dist):
                build_app(_config())
        assert not [
            r for r in caplog.records if "no coord-web bundle" in r.getMessage()
        ]

    def test_bundle_appearing_later_is_picked_up_without_a_restart(
        self, tmp_path: Path
    ) -> None:
        """``coord-web-dist-build.sh`` repoints the live symlink under a
        long-running ``coord web`` (#1543 — no restart on publish). The
        #2009 signalling must not freeze the answer at build time and keep
        insisting a bundle is missing after one arrives."""
        from coord.dashboard.server import WEBAPP_BUNDLE_HEADER, WEBAPP_BUNDLE_MISSING

        dist = tmp_path / "dist"
        dist.mkdir()  # exists but empty — no index.html yet
        with patch("coord.dashboard.server.WEBAPP_DIST", dist):
            client = TestClient(build_app(_config()))
            before = client.get("/")
            (dist / "index.html").write_text("<html><title>arrived</title></html>")
            after = client.get("/")
        assert before.headers[WEBAPP_BUNDLE_HEADER] == WEBAPP_BUNDLE_MISSING
        assert after.headers[WEBAPP_BUNDLE_HEADER] == str(dist)
        assert "arrived" in after.text

    def test_cli_warns_with_no_dist_flag_at_all(
        self, tmp_path: Path, coord_db
    ) -> None:
        """#2009: the warning no longer hinges on ``--dist`` being passed.

        A bare ``coord web`` used to fall back to the bundle vendored in the
        wheel, so "no bundle" wasn't worth a word. That bundle is gone, which
        makes a bare ``coord web`` the case MOST likely to have nothing to
        serve — and the one an operator is least likely to suspect.
        """
        from click.testing import CliRunner
        from coord.cli import main

        config_file = TestCLI._config_file(tmp_path)
        with patch("coord.dashboard.server.WEBAPP_DIST", tmp_path / "nope"):
            with patch("uvicorn.run", side_effect=SystemExit(0)):
                result = CliRunner().invoke(
                    main,
                    ["web", "--config", str(config_file)],
                    catch_exceptions=True,
                )
        assert "no coord-web bundle" in result.output
        assert "legacy" in result.output.lower()


class TestDistPathOverride:
    """#1543: `build_app(dist_path=...)` serves the webapp from an explicit
    directory instead of the bundled `coord/dashboard/webapp/dist` — the seam
    `coord web --dist PATH` uses to serve a checkout a build hook keeps in
    sync with merged main, without upgrading ~/.coord-venv."""

    def _make_dist(self, tmp_path: Path, index_content: str = "<html><title>coord dist override</title></html>") -> Path:
        dist = tmp_path / "custom-dist"
        dist.mkdir()
        (dist / "index.html").write_text(index_content)
        return dist

    def test_dist_path_overrides_bundled_webapp_dist(self, tmp_path: Path) -> None:
        """dist_path is honored even when the bundled WEBAPP_DIST is absent."""
        dist = self._make_dist(tmp_path)
        # Deliberately do NOT patch WEBAPP_DIST — proves dist_path alone drives
        # which directory is served, independent of the module-level default.
        client = TestClient(build_app(_config(), dist_path=dist))
        r = client.get("/")
        assert r.status_code == 200
        assert "coord dist override" in r.text

    def test_dist_path_assets_served(self, tmp_path: Path) -> None:
        dist = self._make_dist(tmp_path)
        assets = dist / "assets"
        assets.mkdir()
        (assets / "index.abc123.js").write_text("// bundle")
        client = TestClient(build_app(_config(), dist_path=dist))
        r = client.get("/assets/index.abc123.js")
        assert r.status_code == 200

    def test_dist_path_none_keeps_default_webapp_dist_behavior(self, tmp_path: Path) -> None:
        """Omitting dist_path (the CLI default) still reads the patched module
        global -- guards against a regression that captures WEBAPP_DIST as a
        stale default-arg value instead of re-reading it per call."""
        dist = self._make_dist(tmp_path, "<html><title>coord module default</title></html>")
        with patch("coord.dashboard.server.WEBAPP_DIST", dist):
            client = TestClient(build_app(_config()))
            r = client.get("/")
        assert r.status_code == 200
        assert "coord module default" in r.text

    def test_dist_path_wins_over_bundled_webapp_dist(self, tmp_path: Path) -> None:
        """#2003: when BOTH the bundled ``coord/dashboard/webapp/dist`` and an
        explicit ``dist_path`` resolve to real bundles, the explicit override
        wins -- pinning the precedence a `coord-web-dist-build.sh` release
        depends on (it must not silently keep serving a stale packaged
        dist/ that happens to also exist in the venv)."""
        bundled = tmp_path / "bundled"
        bundled.mkdir()
        (bundled / "index.html").write_text("<html><title>stale bundled dist</title></html>")

        override = self._make_dist(tmp_path, "<html><title>coord dist override</title></html>")

        with patch("coord.dashboard.server.WEBAPP_DIST", bundled):
            client = TestClient(build_app(_config(), dist_path=override))
            r = client.get("/")
        assert r.status_code == 200
        assert "coord dist override" in r.text
        assert "stale bundled dist" not in r.text

    def test_missing_dist_path_falls_back_to_legacy_dashboard(self, tmp_path: Path) -> None:
        """#2003: a ``--dist`` pointing at a directory that does not exist at
        all must not error or serve a stale bundle -- it falls back to the
        legacy single-file dashboard, same as the no-override default. This
        pins the HTTP-layer half of the fallback; ``TestCLI`` below pins that
        the CLI surfaces a loud warning for this case rather than the plain
        success message it prints for a valid ``--dist``."""
        missing = tmp_path / "does-not-exist"
        client = TestClient(build_app(_config(), dist_path=missing))
        r = client.get("/")
        assert r.status_code == 200
        assert "coord dashboard" in r.text
        assert "coord dist override" not in r.text

    def test_empty_dist_path_directory_falls_back_to_legacy_dashboard(self, tmp_path: Path) -> None:
        """Same as above but the directory exists (e.g. an interrupted build
        left an empty dir) rather than being wholly absent -- both must fail
        the same way, not just the "doesn't exist" case."""
        empty = tmp_path / "empty-dist"
        empty.mkdir()
        client = TestClient(build_app(_config(), dist_path=empty))
        r = client.get("/")
        assert r.status_code == 200
        assert "coord dashboard" in r.text


class TestDistHasBundle:
    """#2003 (epic #2096): `dist_has_bundle` is the ONE predicate both
    `index()` (this module) and `coord web`'s CLI (coord/commands/lifecycle.py)
    call to decide whether a dist directory has a servable bundle -- pinning
    it directly here guards against the two call sites drifting back apart
    into independent re-derivations of the same check."""

    def test_true_when_index_html_present(self, tmp_path: Path) -> None:
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "index.html").write_text("<html></html>")
        assert dist_has_bundle(dist) is True

    def test_false_when_directory_missing(self, tmp_path: Path) -> None:
        assert dist_has_bundle(tmp_path / "does-not-exist") is False

    def test_false_when_directory_empty(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        assert dist_has_bundle(empty) is False
