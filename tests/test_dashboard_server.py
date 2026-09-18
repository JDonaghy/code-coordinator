"""Tests for #3295's per-process board memo in ``coord/dashboard/server.py``.

Before this, every ``/api/*`` request on a co-located dashboard (no
``board_service`` configured — the deployment actually in use on the daemon
host) fell through to ``read_board()``: a fresh SQLite query + full
``assemble_board()`` walk, on EVERY request. A single screen load (several
panels, each polling its own endpoint) or one ``_background_poller()`` tick
fanned that out into 2-5 independent builds of the identical board.

These tests assert on a **read counter**, never on timing (#2096: a gate
that can only ever pass proves nothing) — each one also has a companion
assertion that shows the memo is NOT a permanent or broken cache: it expires
on its own TTL and a write invalidates it, so a bug that made the cache
infinite (which would also make "two requests -> one read" trivially true)
would still be caught.

See ``tests/test_client.py`` for the client-side half (#3295's other
independent fix): ``coord.client.fetch_board_payload``'s ``If-None-Match``
conditional GET, which is what makes a THIN-CLIENT dashboard's repeat reads
cheap.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from coord.config import Config
from coord.dashboard.server import build_app
from coord.models import Board, Machine, Repo


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


class TestBoardMemoLocalMode:
    """No ``board_service`` configured — the daemon-host deployment #3295
    exists to fix. ``coord.dashboard.server.read_board`` is the exact call
    every one of these handlers used to make independently; a
    ``MagicMock`` in its place turns "how many times was the board actually
    built" into a plain call-count assertion.
    """

    def test_two_api_board_requests_within_the_window_read_once(self) -> None:
        """The issue's own acceptance test: two ``/api/*`` requests inside
        the memo window must cost ONE board read, not two."""
        counter = MagicMock(return_value=Board())
        with patch("coord.dashboard.server.read_board", counter):
            client = _client()
            r1 = client.get("/api/board")
            r2 = client.get("/api/board")

        assert r1.status_code == 200
        assert r2.status_code == 200
        assert counter.call_count == 1

    def test_board_and_machines_endpoints_share_one_build(self) -> None:
        """Not just repeats of the same endpoint: ``_read_board()`` and
        ``_read_board_and_machine_health()`` (the latter backs
        ``GET /api/machines``) must read through the SAME memo rather than
        each calling ``read_board()`` on their own — the issue's explicit
        "must share the memo rather than each doing their own read"."""
        counter = MagicMock(return_value=Board())
        with (
            patch("coord.dashboard.server.read_board", counter),
            patch("coord.state.load_machine_health", return_value={}),
        ):
            client = _client()
            assert client.get("/api/board").status_code == 200
            assert client.get("/api/machines").status_code == 200

        assert counter.call_count == 1

    def test_memo_expires_after_the_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The memo is a short TTL, not a permanent cache — proves the
        first two tests aren't passing merely because the cache never
        refreshes at all (a #2096 "can this gate ever fail" check on the
        memo itself)."""
        counter = MagicMock(return_value=Board())
        fake_now = [1_000.0]
        monkeypatch.setattr(
            "coord.dashboard.server.time.monotonic", lambda: fake_now[0]
        )
        with patch("coord.dashboard.server.read_board", counter):
            client = _client()
            assert client.get("/api/board").status_code == 200
            fake_now[0] += 5.0  # comfortably past the ~2s memo window
            assert client.get("/api/board").status_code == 200

        assert counter.call_count == 2

    def test_write_invalidates_the_memo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A write (``/api/pipeline/action``'s ``unstick``, like every other
        action here, reads the board, mutates it, and ``_write_board``s it
        back) must not leave a stale cached board sitting around for up to
        the full TTL — the next read has to see fresh state, not the
        pre-write memoized snapshot."""
        from coord.models import Assignment

        board_with_work = Board(active=[
            Assignment(
                machine_name="laptop", repo_name="api", issue_number=1,
                issue_title="in flight", assignment_id="w1", status="running",
            ),
        ])
        counter = MagicMock(return_value=board_with_work)

        def _no_network(*a, **k):
            raise RuntimeError("no network in tests")

        monkeypatch.setattr("coord.dashboard.server.httpx.post", _no_network)

        with (
            patch("coord.dashboard.server.read_board", counter),
            patch("coord.dashboard.server.write_board") as write_mock,
        ):
            client = _client()
            assert client.get("/api/board").status_code == 200
            assert counter.call_count == 1

            # "unstick" reads the board through the SAME memo entry the GET
            # above populated (no extra read here), marks the assignment
            # failed, and writes the board back.
            resp = client.post(
                "/api/pipeline/action",
                json={"assignment_id": "w1", "action": "unstick"},
            )
            assert resp.status_code == 200
            assert resp.json()["cancelled_on_agent"] is False
            write_mock.assert_called_once()
            assert counter.call_count == 1  # still just the one GET so far

            # The write must have invalidated the memo: this read has to
            # rebuild rather than silently reusing the pre-write snapshot.
            assert client.get("/api/board").status_code == 200

        assert counter.call_count == 2


class TestBoardMemoThinClientMode:
    """``board_service`` configured. #3295's other half of "must share the
    memo": ``_read_board_and_machine_health()`` and ``_read_fleet_health()``
    each used to fire their OWN ``fetch_board_payload()`` daemon round trip
    for the exact same ``/board`` snapshot — doubling this dashboard's
    daemon I/O on every fleet-panel poll.
    """

    def _fake_daemon(self, monkeypatch: pytest.MonkeyPatch, payload: dict) -> list[str]:
        import coord.client as cc

        monkeypatch.setattr(
            cc, "resolve_board_service",
            lambda *a, **k: cc.ServiceConfig("http://daemon:7435"),
        )
        calls: list[str] = []

        def fake_get(url, **kw):
            calls.append(url)

            class _Resp:
                status_code = 200
                headers: dict = {}

                def raise_for_status(self) -> None:
                    return None

                def json(self):
                    return payload

            return _Resp()

        monkeypatch.setattr(cc.httpx, "get", fake_get)
        return calls

    def test_machines_and_machines_health_share_one_daemon_fetch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = {
            "assignments": [],
            "plans": {},
            "round_number": 0,
            "fleet_health": {
                "schema": 1,
                "refreshed_at": 1.0,
                "truncated": False,
                "machine_health": [],
                "fleet_checks": [],
            },
        }
        calls = self._fake_daemon(monkeypatch, payload)

        client = _client()
        r1 = client.get("/api/machines")
        r2 = client.get("/api/machines/health")

        assert r1.status_code == 200
        assert r2.status_code == 200
        assert len(calls) == 1, (
            f"expected exactly ONE /board round trip, got {len(calls)}: {calls}"
        )
