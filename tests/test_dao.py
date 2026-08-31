"""Tests for ``coord.dao`` — the read-only board data-access layer (#584/#589).

#1823 narrowed ``CoordStore`` to the read contract it actually serves and
deleted three ``NotImplementedError`` write stubs (``record_result`` /
``record_completion`` / ``record_dispatched``) that described a design which
was not taken — routing writes through the daemon landed in ``coord.state`` +
``coord.board_service`` (#590), not here.  These tests guard that narrowing so
the stubs (or the misleading "write side declared for #590" docstring) do not
creep back.
"""

from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

import pytest

from coord import db as db_mod
from coord import sql
from coord.dao import CoordStore, SqliteStore
from coord.db import _ensure_schema
from tests import backends
from tests.test_db import (
    AbortOnErrorConn,
    abort_simulating_connection,
    schema_migrated_sqlite_connection,
)


@pytest.fixture
def read_db(tmp_path: Path) -> Path:
    """An on-disk, schema-migrated ``coord.db`` for read-only ``SqliteStore``.

    Empty is enough — every read method returns ``[]``/``None``/``{}`` against a
    migrated DB with no rows, so we can invoke the whole read contract without
    seeding data.  The writer commits and closes before ``SqliteStore`` opens
    its own ``mode=ro`` connection.
    """
    p = tmp_path / "coord.db"
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    conn.commit()
    conn.close()
    return p


@pytest.fixture(autouse=True)
def _default_to_sqlite_store_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """#827: ``SqliteStore._connect()`` now consults
    ``coord.db._resolve_store_target()`` to decide its backend on every call.
    Unlike ``coord.db.get_connection()``'s write-path singleton (protected by
    conftest.py's autouse ``coord_db`` fixture, which overrides the
    connection before ``_resolve_store_target`` is ever reached),
    ``SqliteStore`` has no such override -- so without this, a real ambient
    ``coordinator.yml`` on whatever machine runs this suite (e.g. an
    operator's own ``~/.coord/coordinator.yml``) could silently point every
    read in this file at Postgres. Pin the default to SQLite here, the same
    way conftest.py's ``_no_board_service`` pins board-service resolution off
    by default -- a test that wants to exercise the Postgres routing
    overrides this via its own ``monkeypatch.setattr`` (applied after this
    fixture's setup, so it wins).
    """
    monkeypatch.setattr(
        db_mod, "_resolve_store_target", lambda: db_mod._StoreTarget(backend=sql.DIALECT_SQLITE)
    )


def _dummy_for(param: inspect.Parameter) -> object:
    """A safe positional arg for *param* — ``""`` for str, ``0`` for int, else
    ``None``.  Used only to drive each read method's body so a
    ``NotImplementedError`` (the #1823 dead stub) is observable."""
    ann = param.annotation
    if ann is inspect.Parameter.empty:
        return None
    if ann is str or ann == "str":
        return ""
    if ann is int or ann == "int":
        return 0
    return None


def test_coordstore_protocol_methods_are_all_implemented(read_db: Path) -> None:
    """Every ``CoordStore`` protocol method is callable on ``SqliteStore`` and
    none raise ``NotImplementedError``.

    Named regression guard for #1823.  ``dao.py`` used to declare three write
    stubs (``record_result`` / ``record_completion`` / ``record_dispatched``)
    that raised ``NotImplementedError`` pointing at #590.  #590 landed in
    ``coord.state`` + ``coord.board_service`` instead, so the stubs were dead
    code.  This test enumerates the protocol and invokes each method on a
    ``SqliteStore`` — it MUST fail against the pre-#1823 code (the stubs were
    protocol members and raised when called) and pass once the protocol is
    narrowed to the read contract that is actually served.
    """
    store = SqliteStore(read_db)
    protocol_methods = sorted(
        name
        for name, value in vars(CoordStore).items()
        if callable(value) and not name.startswith("_")
    )
    assert protocol_methods, "CoordStore declared no methods — enumeration is broken"

    for name in protocol_methods:
        bound = getattr(store, name, None)
        assert callable(bound), f"SqliteStore is missing protocol method {name!r}"
        args = [_dummy_for(p) for p in inspect.signature(bound).parameters.values()]
        try:
            bound(*args)
        except NotImplementedError as exc:
            pytest.fail(
                f"CoordStore.{name}() raised NotImplementedError — a dead write "
                f"stub leaked back into the read protocol: {exc}"
            )
        except Exception:  # noqa: BLE001 — not the contract under test
            # Read methods run against a migrated DB with dummy args, so they
            # normally return empty/None.  Any *other* error is tolerated: the
            # #1823 contract is solely "no NotImplementedError stubs".
            pass


def test_coordstore_protocol_omits_dead_write_stubs() -> None:
    """The three ``NotImplementedError`` write stubs removed in #1823 must stay
    out of the read protocol and off ``SqliteStore`` — writes live in
    ``coord.state``'s ``_*_local()`` family, not here."""
    names = {n for n in vars(CoordStore) if not n.startswith("_")}
    assert "record_result" not in names
    assert "record_completion" not in names
    assert "record_dispatched" not in names
    for dead in ("record_result", "record_completion", "record_dispatched", "_not_yet"):
        assert not hasattr(SqliteStore, dead), (
            f"SqliteStore still carries dead stub {dead!r}"
        )


def test_coordstore_is_runtime_checkable_against_sqlite_store(read_db: Path) -> None:
    """``SqliteStore`` satisfies the narrowed ``CoordStore`` read protocol —
    the ``runtime_checkable`` ``isinstance`` still holds after the write
    methods were dropped from both the protocol and the class."""
    store = SqliteStore(read_db)
    assert isinstance(store, CoordStore)


def test_connect_opens_a_genuinely_read_only_connection(read_db: Path) -> None:
    """#2766: ``_connect()`` now sets up the connection via
    ``coord.sql.apply_connection_setup(conn, read_only=True)`` instead of a
    bare ``PRAGMA query_only=ON``. Guard that the seam call still produces a
    connection that refuses writes -- the whole point of ``SqliteStore``
    never touching the live board DB it reads."""
    store = SqliteStore(read_db)
    conn = store._connect()
    try:
        with pytest.raises(sqlite3.Error):
            conn.execute("INSERT INTO board_meta (key, value) VALUES ('x', 'y')")
    finally:
        conn.close()


def test_audit_recent_count_fails_open_when_table_missing(tmp_path: Path) -> None:
    """#2766: ``_audit_recent_count`` used to catch ``sqlite3.Error``
    directly; it now catches ``coord.sql.driver_error(conn)``. Guard that the
    fail-open behaviour (#1037: a missing/pre-migration ``audit_log`` table
    must never blow up the board) survives the seam migration."""
    p = tmp_path / "no_audit_log.db"
    conn = sqlite3.connect(str(p))
    conn.execute("CREATE TABLE board_meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()

    store = SqliteStore(p)
    ro_conn = store._connect()
    try:
        assert store._audit_recent_count(ro_conn) == 0
    finally:
        ro_conn.close()


# ── _connect() dialect routing (#827 review fix) ─────────────────────────────
#
# Blocking finding: `SqliteStore._connect()` used to hardcode
# `backend=sql.DIALECT_SQLITE` unconditionally, so `dao.py`'s read path
# (the `coord serve` daemon's `/board`, `coord notifier`, `coord reports`,
# `coord usage`, ...) was structurally incapable of ever reaching a
# configured Postgres store -- with `store.backend: postgres` fully
# configured, writes went to Postgres while every read silently kept
# serving a stale, empty local SQLite file. `_connect()` now resolves its
# backend the same way `coord.db.get_connection()`'s write path does.


class TestConnectDialectRouting:
    def test_connect_uses_sqlite_by_default(
        self, read_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sanity check for the routing itself (not just the autouse
        default): explicitly resolving to SQLite still opens the on-disk
        path this store was constructed with."""
        monkeypatch.setattr(
            db_mod,
            "_resolve_store_target",
            lambda: db_mod._StoreTarget(backend=sql.DIALECT_SQLITE),
        )
        store = SqliteStore(read_db)
        conn = store._connect()
        try:
            assert sql.detect_dialect(conn) == sql.DIALECT_SQLITE
        finally:
            conn.close()

    def test_connect_routes_to_postgres_when_store_configured(
        self, read_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When `coordinator.yml` configures `store.backend: postgres`,
        `_connect()` must ask `coord.sql.connect` for a Postgres connection
        with the configured DSN -- not silently keep opening
        `self._path` (SQLite) regardless of config, which is exactly the
        split-brain this fix closes. `coord.sql.connect` itself is faked
        here (psycopg is an optional dependency, not installed in this
        environment -- see coord/sql.py's module docstring); what's under
        test is `_connect()`'s ROUTING, not the Postgres connection
        machinery, which `tests/test_sql_dialect.py` already covers via its
        own fake-psycopg-shaped connections.
        """
        monkeypatch.setattr(
            db_mod,
            "_resolve_store_target",
            lambda: db_mod._StoreTarget(
                backend=sql.DIALECT_POSTGRES, dsn="postgresql://user@host/db"
            ),
        )
        # Past `coord.db.refuse_postgres_under_pytest`'s #1960-analogue guard
        # -- mirrors `tests/test_db.py`'s own `_bypass_pytest_trigger` pattern
        # for exercising code on the far side of that guard.
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

        calls: list[dict] = []

        def _fake_sql_connect(**kwargs):
            calls.append(kwargs)
            return sqlite3.connect(":memory:")

        monkeypatch.setattr(sql, "connect", _fake_sql_connect)

        store = SqliteStore(read_db)
        conn = store._connect()
        try:
            assert calls == [
                {"backend": sql.DIALECT_POSTGRES, "dsn": "postgresql://user@host/db"}
            ]
        finally:
            conn.close()

    def test_connect_refuses_postgres_under_pytest(
        self, read_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#1960's SQLite write-path guard has a read-path analogue here too
        (`coord.db.refuse_postgres_under_pytest`) -- no test should ever open
        a real Postgres socket, even if `_resolve_store_target()` somehow
        resolves to one (a leaked ambient config, a copy-pasted fixture)."""
        monkeypatch.setattr(
            db_mod,
            "_resolve_store_target",
            lambda: db_mod._StoreTarget(
                backend=sql.DIALECT_POSTGRES, dsn="postgresql://user@host/db"
            ),
        )
        store = SqliteStore(read_db)
        with pytest.raises(db_mod.ProductionDatabaseGuardError):
            store._connect()


# ── #2983: a swallowed driver error must not poison the read connection ─────
#
# `_audit_recent_count` is documented as failing "open to 0 so a
# missing/pre-migration table never 503s the board".  On Postgres a failed
# statement aborts the whole transaction, so before #2983 the swallow took
# out every *sibling* read sharing that `with closing(self._connect())`
# block -- `escalations` and `drive_queue` are evaluated after it in
# `board_projection`'s dict literal -- producing precisely the 503 the guard
# exists to prevent.


class TestAuditRecentCountRollsBackOnDriverError:
    """Drives the regression with the shared abort-simulating stub, so it is
    meaningful on a SQLite-only dev machine with no Postgres server."""

    def _store_with_missing_audit_log(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[SqliteStore, AbortOnErrorConn]:
        conn = abort_simulating_connection(
            monkeypatch, schema_migrated_sqlite_connection(drop=("audit_log",))
        )
        monkeypatch.setattr(SqliteStore, "_connect", lambda self: conn)
        return SqliteStore(Path("unused-the-connection-is-injected.db")), conn

    def test_returns_zero_and_leaves_the_connection_usable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store, conn = self._store_with_missing_audit_log(monkeypatch)

        assert store._audit_recent_count(conn) == 0
        assert conn.rollbacks == 1
        # The "further operation on the same connection" acceptance
        # criterion -- pre-fix this raises "current transaction is aborted".
        assert sql.execute(conn, "SELECT 1 AS ok").fetchone()["ok"] == 1

    def test_board_projection_still_serves_the_reads_after_the_swallow(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The user-visible shape: a missing `audit_log` must degrade to
        `audit_recent_count: 0`, not 503 the whole board.  `escalations` and
        `drive_queue` are read *after* `_audit_recent_count` in
        `board_projection`'s dict literal, so they are what actually broke."""
        store, conn = self._store_with_missing_audit_log(monkeypatch)

        payload = store.board_projection()

        assert payload["audit_recent_count"] == 0
        assert payload["escalations"] == []
        assert payload["drive_queue"] == []
        assert payload["assignments"] == []


class TestAuditRecentCountRollsBackOnRealPostgres:
    """The same regression against an actual Postgres server, when one is
    reachable -- `psycopg.errors.UndefinedTable` followed by
    `InFailedSqlTransaction`, the real shapes the stub above simulates."""

    def test_sibling_reads_survive_a_missing_audit_log(self) -> None:
        unavailable = backends.postgres_available()
        if unavailable:
            pytest.skip(f"no Postgres backend available: {unavailable}")

        session = backends.open_named_session(backends.BACKEND_POSTGRES)
        try:
            _ensure_schema(session.conn)
            sql.execute(session.conn, "DROP TABLE audit_log")
            session.conn.commit()
            store = SqliteStore(Path("unused-the-connection-is-injected.db"))

            assert store._audit_recent_count(session.conn) == 0
            # Pre-fix: psycopg.errors.InFailedSqlTransaction.
            assert sql.execute(session.conn, "SELECT 1 AS ok").fetchone()["ok"] == 1
        finally:
            session.close()
