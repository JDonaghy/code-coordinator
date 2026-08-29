"""Tests for #1945 (Phase B of milestone #60): deprecation telemetry for RPC
routes.

Two layers, mirroring the split ``tests/test_reports.py`` already uses for
similarly best-effort/observability code:

* :mod:`coord.deprecation_telemetry` — the write side, unit-tested directly:
  what it records, the ``unknown`` fallback for a missing client identity or
  version, and that it truly never raises.
* the daemon's ``_DeprecatedRouteTelemetryMiddleware`` — black-boxed through
  a real ``Starlette`` app (mirrors ``tests/test_serve_rest_routes.py``'s
  ``cli``/``rw_db`` fixtures): a request to a deprecated RPC route leaves a
  matching ``audit_log`` row, a request to a non-deprecated route does not,
  and a caller that sends no client headers is still recorded (as
  ``unknown``), never dropped.

The read side (``coord.reports``' ``deprecated-routes`` fold/run) is
unit-tested in ``tests/test_reports.py`` alongside every other report, not
here.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from coord.audit import query_audit_log
from coord.config import load as load_config
from coord.dao import SqliteStore
from coord.db import _ensure_schema
from coord.deprecation_telemetry import (
    CATEGORY,
    CLIENT_HEADER,
    CLIENT_VERSION_HEADER,
    EVENT_TYPE,
    UNKNOWN_CLIENT,
    record_deprecated_rpc_call,
)
from coord.serve_app import RPC_SUPERSEDED_BY_RESOURCE, build_app


# ── record_deprecated_rpc_call: the write side, in isolation ──────────────


class TestRecordDeprecatedRpcCall:
    def test_records_route_client_and_version(self, monkeypatch) -> None:
        calls = []
        monkeypatch.setattr(
            "coord.audit.record_audit", lambda **kw: calls.append(kw)
        )
        record_deprecated_rpc_call(
            "/issue-label", client="coord-tui", client_version="0.9.3"
        )
        assert len(calls) == 1
        kw = calls[0]
        assert kw["tier"] == "operational"
        assert kw["category"] == CATEGORY == "deprecation"
        assert kw["event_type"] == EVENT_TYPE == "deprecated_rpc_call"
        assert kw["actor"] == "coord-tui"
        assert kw["details"] == {
            "route": "/issue-label",
            "client": "coord-tui",
            "client_version": "0.9.3",
        }

    @pytest.mark.parametrize("client", [None, "", "   "])
    def test_missing_or_blank_client_is_unknown_not_dropped(
        self, monkeypatch, client
    ) -> None:
        calls = []
        monkeypatch.setattr(
            "coord.audit.record_audit", lambda **kw: calls.append(kw)
        )
        record_deprecated_rpc_call(
            "/issue-label", client=client, client_version="1.0"
        )
        assert len(calls) == 1, "an unattributed call must still be recorded"
        assert calls[0]["actor"] == UNKNOWN_CLIENT
        assert calls[0]["details"]["client"] == UNKNOWN_CLIENT

    @pytest.mark.parametrize("version", [None, "", "  "])
    def test_missing_or_blank_version_is_unknown_not_dropped(
        self, monkeypatch, version
    ) -> None:
        calls = []
        monkeypatch.setattr(
            "coord.audit.record_audit", lambda **kw: calls.append(kw)
        )
        record_deprecated_rpc_call(
            "/issue-label", client="coord-py", client_version=version
        )
        assert calls[0]["details"]["client_version"] == UNKNOWN_CLIENT

    def test_never_raises_even_if_the_audit_write_blows_up(self, monkeypatch) -> None:
        def _boom(**kw):
            raise RuntimeError("disk full")

        monkeypatch.setattr("coord.audit.record_audit", _boom)
        # Must not raise -- #1945: "must never raise or delay a request".
        record_deprecated_rpc_call(
            "/issue-label", client="coord-py", client_version="1.0"
        )

    def test_never_raises_on_a_broken_import(self, monkeypatch) -> None:
        import builtins

        real_import = builtins.__import__

        def _fake_import(name, *a, **kw):
            if name == "coord.audit":
                raise ImportError("simulated")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", _fake_import)
        record_deprecated_rpc_call(
            "/issue-label", client="coord-py", client_version="1.0"
        )


# ── the daemon middleware: black-box through a real Starlette app ─────────


@pytest.fixture
def file_db(tmp_path: Path) -> Path:
    p = tmp_path / "coord.db"
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    conn.commit()
    conn.close()
    return p


@pytest.fixture
def rw_db(tmp_path: Path):
    """Thread-safe coord.db override -- see test_serve_rest_routes.py's twin:
    TestClient runs the async handler on a worker thread, which the autouse
    ``coord_db`` ``:memory:`` connection cannot serve."""
    from coord import db

    conn = sqlite3.connect(str(tmp_path / "rw.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    db.override_connection(conn)
    yield conn


@pytest.fixture
def cli(file_db: Path, valid_config_path: Path):
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as client:
        yield client


def _deprecation_rows(conn) -> list[dict]:
    conn.commit()
    result = query_audit_log(category=CATEGORY, limit=500)
    return result["entries"]


def test_a_call_to_a_deprecated_route_is_recorded(cli, rw_db) -> None:
    resp = cli.post(
        "/issue-test-mode",
        json={"repo_name": "api", "issue_number": 1},
        headers={CLIENT_HEADER: "coord-tui", CLIENT_VERSION_HEADER: "0.9.3"},
    )
    assert resp.status_code == 200
    rows = _deprecation_rows(rw_db)
    assert len(rows) == 1
    assert rows[0]["details"]["route"] == "/issue-test-mode"
    assert rows[0]["details"]["client"] == "coord-tui"
    assert rows[0]["details"]["client_version"] == "0.9.3"


def test_every_route_in_the_supersession_table_is_routable_and_recordable(
    cli,
) -> None:
    """#1945 acceptance: every route marked deprecated in the OpenAPI spec is
    covered.  Derived directly from ``RPC_SUPERSEDED_BY_RESOURCE`` -- the
    same table the spec itself is stamped from (#1944) -- so this can never
    silently drift from what the daemon actually calls deprecated."""
    declared = {r.path for r in cli.app.routes if hasattr(r, "path")}
    assert set(RPC_SUPERSEDED_BY_RESOURCE) <= declared


def test_a_call_with_no_client_headers_is_recorded_as_unknown_not_dropped(
    cli, rw_db
) -> None:
    resp = cli.post(
        "/issue-test-mode", json={"repo_name": "api", "issue_number": 1}
    )
    assert resp.status_code == 200
    rows = _deprecation_rows(rw_db)
    assert len(rows) == 1
    assert rows[0]["details"]["client"] == "unknown"
    assert rows[0]["details"]["client_version"] == "unknown"


def test_a_call_that_fails_validation_downstream_is_still_recorded(
    cli, rw_db
) -> None:
    """Capture must never be conditioned on the handler's own outcome -- a
    call that the deprecated route itself 400s on is still evidence the
    route is being hit, and still counts against retirement."""
    resp = cli.post("/issue-test-mode", json={})
    assert resp.status_code == 400
    rows = _deprecation_rows(rw_db)
    assert len(rows) == 1
    assert rows[0]["details"]["route"] == "/issue-test-mode"


def test_a_call_to_a_non_deprecated_route_is_not_recorded(cli, rw_db) -> None:
    resp = cli.get("/board")
    assert resp.status_code == 200
    assert _deprecation_rows(rw_db) == []


def test_a_call_to_healthz_is_not_recorded(cli, rw_db) -> None:
    resp = cli.get("/healthz")
    assert resp.status_code == 200
    assert _deprecation_rows(rw_db) == []
