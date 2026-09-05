"""Tests for ``coord.sql`` — the paramstyle + upsert dialect seam (#2719).

This module is a pure addition: nothing in the tree calls ``coord.sql`` yet
(that's Phase C slices 2-7 of #1948), so these tests exercise it directly
rather than through any existing call site.

``psycopg`` is an optional dependency (the ``postgres`` extra, #2886) — most
runs of this suite still don't have it installed, so the Postgres path is
exercised two ways: (1) dialect detection and SQL/param translation, which
need only a *fake* connection class whose ``__module__`` claims to be
``psycopg``, spied to capture exactly what would have reached a real driver
(this needs no driver and no server — it is the bulk of this file, and it
still runs on every machine, every time); and (2), in the
"round trip against a real server" section near the bottom, actual
``psycopg`` + an actual Postgres 16 connection — the CI ``postgres`` job in
``.github/workflows/test.yml`` provides both. Those tests skip cleanly (not
error) when ``psycopg`` isn't installed or no server is reachable, via the
``pg_conn``/``real_postgres`` fixtures below, so a developer with neither
sees skips, not failures — proving #2886's whole point: every Postgres
branch coord.sql has ever emitted a fake-connection assertion for now also
has at least one assertion that it is *accepted* by a real server, not just
*shaped like* what one would accept.
"""

from __future__ import annotations

import ast
import os
import re
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any, Iterator

import pytest

from coord import sql
from tests.backends import DSN_ENV_VAR, postgres_dsn


# ── fake Postgres-shaped connection (no real psycopg needed) ────────────────


class _FakeCursor:
    """Spies on what would reach a real DB-API cursor."""

    def __init__(self, fetchone_result=None, fetchall_result=None):
        self.executed: list[tuple[str, object]] = []
        self.executemany_calls: list[tuple[str, object]] = []
        self._fetchone_result = fetchone_result
        self._fetchall_result = fetchall_result if fetchall_result is not None else []

    def execute(self, sql_text, params=()):
        self.executed.append((sql_text, params))

    def executemany(self, sql_text, seq_of_params):
        self.executemany_calls.append((sql_text, list(seq_of_params)))

    def fetchone(self):
        return self._fetchone_result

    def fetchall(self):
        return self._fetchall_result


class _FakeConnection:
    """A connection object shaped like a real DB-API one, without importing
    a real driver.

    ``detect_dialect`` keys off ``type(conn).__module__`` — subclasses below
    set that to a real driver module name so they're indistinguishable from
    the real thing for dialect-detection *and* SQL-dispatch purposes,
    without requiring the dependency to be installed.
    """

    def __init__(self, fetchone_result=None, fetchall_result=None):
        self.cur = _FakeCursor(fetchone_result=fetchone_result, fetchall_result=fetchall_result)

    def cursor(self):
        return self.cur


class _FakePostgresConnection(_FakeConnection):
    pass


_FakePostgresConnection.__module__ = "psycopg"


class _FakePostgres2Connection(_FakeConnection):
    """Also spies on ``set_session`` — psycopg2's read-only knob, unlike
    psycopg3's plain settable ``.read_only`` attribute (see
    ``apply_connection_setup``'s read_only branch, #827)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.set_session_calls: list[dict] = []

    def set_session(self, **kwargs):
        self.set_session_calls.append(kwargs)


_FakePostgres2Connection.__module__ = "psycopg2"


class _FakeSqliteConnection(_FakeConnection):
    pass


_FakeSqliteConnection.__module__ = "sqlite3"


class _FakeUnknownConnection:
    pass


# ── dialect detection ───────────────────────────────────────────────────────


def test_detect_dialect_sqlite():
    conn = sqlite3.connect(":memory:")
    try:
        assert sql.detect_dialect(conn) == sql.DIALECT_SQLITE
    finally:
        conn.close()


def test_detect_dialect_postgres_psycopg3():
    assert sql.detect_dialect(_FakePostgresConnection()) == sql.DIALECT_POSTGRES


def test_detect_dialect_postgres_psycopg2():
    assert sql.detect_dialect(_FakePostgres2Connection()) == sql.DIALECT_POSTGRES


def test_detect_dialect_unknown_raises():
    with pytest.raises(sql.UnsupportedDialectError):
        sql.detect_dialect(_FakeUnknownConnection())


# ── connect() — the connection factory (#827) ───────────────────────────────


def test_connect_sqlite_writer_opens_a_normal_readwrite_connection(tmp_path):
    db_path = tmp_path / "writer.db"
    conn = sql.connect(backend=sql.DIALECT_SQLITE, sqlite_path=db_path)
    try:
        assert isinstance(conn, sqlite3.Connection)
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO t (id) VALUES (1)")
        conn.commit()
        assert conn.execute("SELECT id FROM t").fetchone() == (1,)
    finally:
        conn.close()
    assert db_path.exists()


def test_connect_sqlite_read_only_opens_mode_ro_and_rejects_writes(tmp_path):
    db_path = tmp_path / "ro.db"
    writer = sql.connect(backend=sql.DIALECT_SQLITE, sqlite_path=db_path)
    writer.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    writer.execute("INSERT INTO t (id) VALUES (1)")
    writer.commit()
    writer.close()

    conn = sql.connect(
        backend=sql.DIALECT_SQLITE, sqlite_path=db_path, read_only=True, check_same_thread=False
    )
    try:
        assert conn.execute("SELECT id FROM t").fetchone() == (1,)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO t (id) VALUES (2)")
    finally:
        conn.close()


def test_connect_sqlite_requires_sqlite_path():
    with pytest.raises(ValueError):
        sql.connect(backend=sql.DIALECT_SQLITE)


def test_connect_postgres_requires_dsn():
    with pytest.raises(ValueError):
        sql.connect(backend=sql.DIALECT_POSTGRES)


def test_connect_postgres_without_psycopg_raises_import_error(monkeypatch):
    """``psycopg`` is an optional dependency (the ``postgres`` extra,
    #2886) -- the honest failure mode for `backend="postgres"` with no
    driver present, matching `row_factory_for`/`driver_error`'s posture for
    the same gap.

    ``sys.modules["psycopg"] = None`` forces ``import psycopg`` to raise
    ``ImportError`` deterministically -- same technique
    ``tests/test_backend_selection.py`` uses -- so this passes identically
    whether or not the CI job running it happens to have the `[postgres]`
    extra installed (the ``postgres`` job in .github/workflows/test.yml
    does, everything else doesn't)."""
    monkeypatch.setitem(sys.modules, "psycopg", None)
    with pytest.raises(ImportError):
        sql.connect(backend=sql.DIALECT_POSTGRES, dsn="postgresql://user@host/db")


def test_connect_unknown_backend_raises():
    with pytest.raises(sql.UnsupportedDialectError):
        sql.connect(backend="mysql", sqlite_path="ignored")


# ── translation: identity for sqlite ────────────────────────────────────────


def test_translate_is_identity_for_sqlite():
    stmt = "SELECT * FROM t WHERE a = ? AND b = ?"
    assert sql.translate(stmt, sql.DIALECT_SQLITE) == stmt


# ── translation: qmark -> pyformat ──────────────────────────────────────────


def test_translate_qmark_to_pyformat_positional_order():
    stmt = "INSERT INTO t (a, b, c) VALUES (?, ?, ?)"
    translated = sql.translate(stmt, sql.DIALECT_POSTGRES)
    assert translated == "INSERT INTO t (a, b, c) VALUES (%s, %s, %s)"


def test_translate_executemany_style_statement():
    stmt = "UPDATE t SET a = ? WHERE id = ?"
    translated = sql.translate(stmt, sql.DIALECT_POSTGRES)
    assert translated == "UPDATE t SET a = %s WHERE id = %s"


# ── the one non-trivial case: a literal `?` inside a quoted string ─────────


def test_translate_preserves_literal_question_mark_in_string_literal():
    stmt = "SELECT * FROM t WHERE note = 'why?' AND id = ?"
    translated = sql.translate(stmt, sql.DIALECT_POSTGRES)
    assert translated == "SELECT * FROM t WHERE note = 'why?' AND id = %s"


def test_translate_preserves_multiple_literal_question_marks():
    stmt = "SELECT ? FROM t WHERE note = 'really? are you sure? ok?'"
    translated = sql.translate(stmt, sql.DIALECT_POSTGRES)
    assert translated == "SELECT %s FROM t WHERE note = 'really? are you sure? ok?'"


def test_translate_handles_doubled_quote_escape_inside_literal():
    # 'it''s ok?' is one SQL string literal containing a literal `'` (the
    # standard doubled-quote escape) and a literal `?` — neither should be
    # touched, and the scanner must not treat the escaped `'` as closing the
    # literal early (which would otherwise leave `s ok` outside the string).
    stmt = "SELECT * FROM t WHERE note = 'it''s ok?' AND id = ?"
    translated = sql.translate(stmt, sql.DIALECT_POSTGRES)
    assert translated == "SELECT * FROM t WHERE note = 'it''s ok?' AND id = %s"


def test_translate_preserves_question_mark_in_double_quoted_identifier():
    stmt = 'SELECT "weird?col" FROM t WHERE id = ?'
    translated = sql.translate(stmt, sql.DIALECT_POSTGRES)
    assert translated == 'SELECT "weird?col" FROM t WHERE id = %s'


# ── literal `%` must be doubled for pyformat, even the sqlite pass-through must not ──


def test_translate_escapes_percent_for_postgres_pyformat():
    stmt = "SELECT * FROM t WHERE name LIKE ?"
    # The LIKE pattern itself is a bound *parameter*, not SQL text, so it is
    # untouched by translation (params are never rewritten — only the SQL
    # string is). This test instead covers a literal `%` written directly
    # into the SQL text, e.g. a hardcoded pattern.
    stmt_literal_percent = "SELECT * FROM t WHERE name LIKE '%foo%' AND id = ?"
    translated = sql.translate(stmt_literal_percent, sql.DIALECT_POSTGRES)
    assert translated == "SELECT * FROM t WHERE name LIKE '%%foo%%' AND id = %s"
    # sqlite needs no escaping — qmark is not %-format based.
    assert sql.translate(stmt, sql.DIALECT_SQLITE) == stmt


# ── ON CONFLICT ... excluded. passes through untouched ─────────────────────


def test_on_conflict_excluded_passes_through_untouched_sqlite():
    stmt = (
        "INSERT INTO t (id, name) VALUES (?, ?) "
        "ON CONFLICT(id) DO UPDATE SET name = excluded.name"
    )
    assert sql.translate(stmt, sql.DIALECT_SQLITE) == stmt


def test_on_conflict_excluded_passes_through_untouched_postgres():
    stmt = (
        "INSERT INTO t (id, name) VALUES (?, ?) "
        "ON CONFLICT(id) DO UPDATE SET name = excluded.name"
    )
    translated = sql.translate(stmt, sql.DIALECT_POSTGRES)
    assert translated == (
        "INSERT INTO t (id, name) VALUES (%s, %s) "
        "ON CONFLICT(id) DO UPDATE SET name = excluded.name"
    )


# ── execute / executemany: real round trip against sqlite3 ─────────────────


@pytest.fixture
def memdb():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT, b TEXT)")
    conn.commit()
    yield conn
    conn.close()


def test_execute_round_trips_against_sqlite(memdb):
    sql.execute(memdb, "INSERT INTO t (id, a, b) VALUES (?, ?, ?)", (1, "x", "y"))
    memdb.commit()
    cur = sql.execute(memdb, "SELECT a, b FROM t WHERE id = ?", (1,))
    row = cur.fetchone()
    assert row == ("x", "y")


def test_executemany_round_trips_against_sqlite(memdb):
    rows = [(1, "a", "b"), (2, "c", "d"), (3, "e", "f")]
    sql.executemany(memdb, "INSERT INTO t (id, a, b) VALUES (?, ?, ?)", rows)
    memdb.commit()
    cur = sql.execute(memdb, "SELECT id, a, b FROM t ORDER BY id")
    assert cur.fetchall() == rows


def test_execute_preserves_literal_question_mark_against_sqlite(memdb):
    sql.execute(memdb, "INSERT INTO t (id, a, b) VALUES (?, ?, ?)", (1, "why?", "z"))
    memdb.commit()
    cur = sql.execute(memdb, "SELECT a FROM t WHERE b = ? AND a = 'why?'", ("z",))
    assert cur.fetchone() == ("why?",)


# ── execute / executemany: dispatch to a fake postgres-shaped connection ───


def test_execute_translates_before_dispatching_to_postgres_driver():
    conn = _FakePostgresConnection()
    sql.execute(conn, "SELECT * FROM t WHERE note = 'why?' AND id = ?", (5,))
    [(sent_sql, sent_params)] = conn.cur.executed
    assert sent_sql == "SELECT * FROM t WHERE note = 'why?' AND id = %s"
    assert sent_params == (5,)


def test_executemany_translates_before_dispatching_to_postgres_driver():
    conn = _FakePostgresConnection()
    rows = [(1, "a"), (2, "b")]
    sql.executemany(conn, "INSERT INTO t (id, a) VALUES (?, ?)", rows)
    [(sent_sql, sent_rows)] = conn.cur.executemany_calls
    assert sent_sql == "INSERT INTO t (id, a) VALUES (%s, %s)"
    assert sent_rows == rows


# ── upsert ───────────────────────────────────────────────────────────────


def test_upsert_inserts_then_updates_on_conflict_sqlite(memdb):
    memdb.execute("CREATE UNIQUE INDEX t_id_unique ON t(id)")
    sql.upsert(
        memdb, "t", ["id", "a", "b"], (1, "first", "y"), conflict_columns=["id"]
    )
    memdb.commit()
    sql.upsert(
        memdb, "t", ["id", "a", "b"], (1, "second", "z"), conflict_columns=["id"]
    )
    memdb.commit()
    cur = memdb.execute("SELECT a, b FROM t WHERE id = ?", (1,))
    assert cur.fetchone() == ("second", "z")
    assert memdb.execute("SELECT COUNT(*) FROM t").fetchone() == (1,)


def test_upsert_emits_do_nothing_when_update_columns_empty(memdb):
    memdb.execute("CREATE UNIQUE INDEX t_id_unique2 ON t(id)")
    sql.upsert(
        memdb,
        "t",
        ["id", "a"],
        (1, "first"),
        conflict_columns=["id"],
        update_columns=[],
    )
    memdb.commit()
    sql.upsert(
        memdb,
        "t",
        ["id", "a"],
        (1, "second"),
        conflict_columns=["id"],
        update_columns=[],
    )
    memdb.commit()
    cur = memdb.execute("SELECT a FROM t WHERE id = ?", (1,))
    assert cur.fetchone() == ("first",)  # untouched — DO NOTHING fired


def test_upsert_emits_identical_sql_shape_for_postgres():
    conn = _FakePostgresConnection()
    sql.upsert(conn, "t", ["id", "a"], (1, "x"), conflict_columns=["id"])
    [(sent_sql, sent_params)] = conn.cur.executed
    assert sent_sql == (
        "INSERT INTO t (id, a) VALUES (%s, %s) "
        "ON CONFLICT (id) DO UPDATE SET a = excluded.a"
    )
    assert sent_params == (1, "x")


# ── insert_ignore ────────────────────────────────────────────────────────


def test_insert_ignore_sqlite_silently_skips_conflict(memdb):
    memdb.execute("CREATE UNIQUE INDEX t_id_unique3 ON t(id)")
    sql.insert_ignore(memdb, "t", ["id", "a"], (1, "first"))
    memdb.commit()
    # No exception — the conflicting insert is silently ignored.
    sql.insert_ignore(memdb, "t", ["id", "a"], (1, "second"))
    memdb.commit()
    cur = memdb.execute("SELECT a FROM t WHERE id = ?", (1,))
    assert cur.fetchone() == ("first",)


def test_insert_ignore_emits_backend_specific_idiom():
    sqlite_conn = _FakeSqliteConnection()
    sql.insert_ignore(sqlite_conn, "t2", ["id", "a"], (1, "x"))
    [(sent_sql, sent_params)] = sqlite_conn.cur.executed
    assert sent_sql == "INSERT OR IGNORE INTO t2 (id, a) VALUES (?, ?)"
    assert sent_params == (1, "x")

    pg_conn = _FakePostgresConnection()
    sql.insert_ignore(pg_conn, "t", ["id", "a"], (1, "x"))
    [(sent_sql, _)] = pg_conn.cur.executed
    assert sent_sql == "INSERT INTO t (id, a) VALUES (%s, %s) ON CONFLICT DO NOTHING"


# ── table_columns (#2782) ───────────────────────────────────────────────────


def test_table_columns_sqlite_reports_name_and_type(memdb):
    assert sql.table_columns(memdb, "t") == [
        ("id", "INTEGER"),
        ("a", "TEXT"),
        ("b", "TEXT"),
    ]


def test_table_columns_sqlite_empty_for_missing_table(memdb):
    assert sql.table_columns(memdb, "no_such_table") == []


def test_table_columns_postgres_queries_information_schema():
    conn = _FakePostgresConnection(fetchall_result=[("id", "integer"), ("a", "text")])
    result = sql.table_columns(conn, "t")
    assert result == [("id", "integer"), ("a", "text")]
    [(sent_sql, sent_params)] = conn.cur.executed
    assert sent_sql == (
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = %s ORDER BY ordinal_position"
    )
    assert sent_params == ("t",)


def test_table_columns_postgres_tolerates_dict_row_factory():
    # apply_row_factory(conn) would have installed psycopg's dict_row on a
    # real connection -- rows come back dict-like, not as plain tuples.
    conn = _FakePostgresConnection(
        fetchall_result=[{"column_name": "id", "data_type": "integer"}]
    )
    assert sql.table_columns(conn, "t") == [("id", "integer")]


# ── WAL helpers: sqlite_journal_mode / sqlite_wal_checkpoint_truncate (#2782) ─


def test_sqlite_journal_mode_reports_wal_after_connection_setup(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "wal.db"))
    try:
        sql.apply_connection_setup(conn)
        assert sql.sqlite_journal_mode(conn) == "wal"
    finally:
        conn.close()


def test_sqlite_journal_mode_reports_non_wal_on_memory_db(memdb):
    assert sql.sqlite_journal_mode(memdb) != "wal"


def test_sqlite_wal_checkpoint_truncate_returns_three_ints(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "wal2.db"))
    try:
        sql.apply_connection_setup(conn)
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.commit()
        busy, log_pages, checkpointed = sql.sqlite_wal_checkpoint_truncate(conn)
        assert (busy, log_pages, checkpointed) == (0, 0, 0)
    finally:
        conn.close()


# ── integrity helper: sqlite_integrity_check (#3118) ─────────────────────
#
# The backup lane's verify-before-it-counts gate calls this instead of
# spelling `PRAGMA integrity_check` in coord/backup.py, where the SQLite-only
# statement-text ratchet below would (correctly) reject it.


def test_sqlite_integrity_check_reports_ok_on_a_healthy_database(memdb):
    sql.execute(memdb, "CREATE TABLE integrity_probe (id INTEGER PRIMARY KEY)")
    assert sql.sqlite_integrity_check(memdb) == "ok"


def test_sqlite_integrity_check_returns_the_first_line_of_a_corruption_report(monkeypatch):
    """A corrupt database answers with one row *per problem found*; the
    caller's contract is "ok, or a reason", so only the first is returned.

    Driven through a stub rather than a hand-corrupted file: the point being
    pinned is the multi-row -> single-string reduction, and SQLite chooses
    for itself how many rows a given kind of damage produces.
    """

    class _Cursor:
        def fetchall(self):
            return [("*** in database main ***",), ("Page 42 is never used",)]

    monkeypatch.setattr(sql, "execute", lambda conn, statement, params=(): _Cursor())
    assert sql.sqlite_integrity_check(object()) == "*** in database main ***"


def test_sqlite_integrity_check_degrades_to_a_placeholder_on_an_empty_result(monkeypatch):
    """Real SQLite always answers with at least one row, but a caller that
    turned an empty result into an IndexError would report a snapshot as
    *unverifiable for an unrelated reason* — so this degrades to a legible
    non-"ok" verdict, which every caller already rejects."""

    class _Cursor:
        def fetchall(self):
            return []

    monkeypatch.setattr(sql, "execute", lambda conn, statement, params=(): _Cursor())
    assert sql.sqlite_integrity_check(object()) == "<no output>"


# ── row factory ──────────────────────────────────────────────────────────


def test_row_factory_for_sqlite_is_sqlite_row():
    assert sql.row_factory_for(sql.DIALECT_SQLITE) is sqlite3.Row


def test_row_factory_for_postgres_without_psycopg_raises_import_error(monkeypatch):
    """See ``test_connect_postgres_without_psycopg_raises_import_error`` for
    why this forces the absence rather than assuming it (#2886)."""
    monkeypatch.setitem(sys.modules, "psycopg", None)
    with pytest.raises(ImportError):
        sql.row_factory_for(sql.DIALECT_POSTGRES)


def test_row_factory_for_unknown_dialect_raises():
    with pytest.raises(sql.UnsupportedDialectError):
        sql.row_factory_for("mysql")


def test_apply_row_factory_makes_rows_dict_accessible(memdb):
    sql.apply_row_factory(memdb)
    memdb.execute("INSERT INTO t (id, a, b) VALUES (1, 'x', 'y')")
    memdb.commit()
    row = memdb.execute("SELECT a, b FROM t WHERE id = 1").fetchone()
    assert row["a"] == "x"
    assert row["b"] == "y"


# ── insert_returning_id ──────────────────────────────────────────────────


def test_insert_returning_id_sqlite_uses_lastrowid(memdb):
    first = sql.insert_returning_id(memdb, "INSERT INTO t (a, b) VALUES (?, ?)", ("x", "y"))
    second = sql.insert_returning_id(memdb, "INSERT INTO t (a, b) VALUES (?, ?)", ("p", "q"))
    memdb.commit()
    assert (first, second) == (1, 2)
    assert memdb.execute("SELECT a FROM t WHERE id = ?", (first,)).fetchone() == ("x",)


def test_insert_returning_id_postgres_appends_returning_and_fetches_tuple_row():
    conn = _FakePostgresConnection(fetchone_result=(42,))
    new_id = sql.insert_returning_id(conn, "INSERT INTO t (a) VALUES (?)", ("x",))
    assert new_id == 42
    [(sent_sql, sent_params)] = conn.cur.executed
    assert sent_sql == "INSERT INTO t (a) VALUES (%s) RETURNING id"
    assert sent_params == ("x",)


def test_insert_returning_id_postgres_fetches_dict_row_by_pk_column():
    conn = _FakePostgresConnection(fetchone_result={"id": 7})
    new_id = sql.insert_returning_id(conn, "INSERT INTO t (a) VALUES (?)", ("x",))
    assert new_id == 7


def test_insert_returning_id_postgres_honors_custom_pk_column():
    conn = _FakePostgresConnection(fetchone_result={"uuid": "abc-123"})
    new_id = sql.insert_returning_id(
        conn, "INSERT INTO t (a) VALUES (?)", ("x",), pk_column="uuid"
    )
    assert new_id == "abc-123"
    [(sent_sql, _)] = conn.cur.executed
    assert sent_sql == "INSERT INTO t (a) VALUES (%s) RETURNING uuid"


def test_insert_returning_id_postgres_raises_when_no_row_returned():
    conn = _FakePostgresConnection(fetchone_result=None)
    with pytest.raises(RuntimeError):
        sql.insert_returning_id(conn, "INSERT INTO t (a) VALUES (?)", ("x",))


def test_insert_returning_id_strips_trailing_semicolon_before_appending_returning():
    conn = _FakePostgresConnection(fetchone_result=(1,))
    sql.insert_returning_id(conn, "INSERT INTO t (a) VALUES (?);", ("x",))
    [(sent_sql, _)] = conn.cur.executed
    assert sent_sql == "INSERT INTO t (a) VALUES (%s) RETURNING id"


# ── apply_connection_setup(read_only=...) (#2766) ────────────────────────────


def test_apply_connection_setup_default_sets_writer_pragmas(tmp_path):
    # WAL mode is a no-op on `:memory:` databases (SQLite silently keeps
    # "memory"), so this needs a real on-disk connection to observe it.
    conn = sqlite3.connect(str(tmp_path / "writer.db"))
    try:
        sql.apply_connection_setup(conn)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        # query_only must NOT be set on a writer connection.
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 0
    finally:
        conn.close()


def test_apply_connection_setup_read_only_sets_query_only_not_wal(memdb):
    sql.apply_connection_setup(memdb, read_only=True)
    assert memdb.execute("PRAGMA query_only").fetchone()[0] == 1
    assert memdb.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    # journal_mode is left alone -- a read-only connection can't set it.
    assert memdb.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal"


def test_apply_connection_setup_read_only_survives_a_true_mode_ro_connection(tmp_path):
    """The scenario #2766 actually cares about: SQLite's ``mode=ro`` URI
    connection (what ``coord/dao.py``'s ``SqliteStore`` opens) rejects a
    ``PRAGMA journal_mode=WAL`` with "attempt to write a readonly database"
    -- ``read_only=True`` must avoid tripping that."""
    db_path = tmp_path / "ro.db"
    writer = sqlite3.connect(str(db_path))
    writer.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    writer.commit()
    writer.close()

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        sql.apply_connection_setup(conn, read_only=True)  # must not raise
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
    finally:
        conn.close()


def test_apply_connection_setup_postgres_writer_is_still_a_noop():
    """*read_only=False* (a writer connection) has no connect-time pragma to
    set for Postgres, unlike SQLite's journal_mode/foreign_keys -- unchanged
    by #827."""
    conn = _FakePostgresConnection()
    sql.apply_connection_setup(conn)  # read_only=False (default)
    assert conn.cur.executed == []
    assert not hasattr(conn, "read_only")


def test_apply_connection_setup_postgres_read_only_sets_psycopg3_read_only_attribute():
    """#827: unlike #2766 (a true no-op -- "there is no live read-only
    connection factory yet for the seam to branch on"), a Postgres
    connection factory (``sql.connect``) now exists, so this sets psycopg3's
    settable ``.read_only`` property instead of doing nothing. No SQL is
    sent through the cursor (unlike SQLite's PRAGMA) -- psycopg3 applies the
    property to transactions opened after it's set, not via a statement."""
    conn = _FakePostgresConnection()
    sql.apply_connection_setup(conn, read_only=True)
    assert conn.read_only is True
    assert conn.cur.executed == []


def test_apply_connection_setup_postgres_read_only_uses_set_session_for_psycopg2():
    """psycopg2 has no ``.read_only`` attribute -- its read-only knob is
    ``conn.set_session(readonly=True)`` (#827)."""
    conn = _FakePostgres2Connection()
    sql.apply_connection_setup(conn, read_only=True)
    assert conn.set_session_calls == [{"readonly": True}]
    assert not hasattr(conn, "read_only")
    assert conn.cur.executed == []


# ── driver_error (#2766) ─────────────────────────────────────────────────────


def test_driver_error_sqlite_is_sqlite_error(memdb):
    assert sql.driver_error(memdb) is sqlite3.Error


def test_driver_error_catches_a_real_sqlite_operational_error(memdb):
    with pytest.raises(sql.driver_error(memdb)):
        memdb.execute("SELECT * FROM no_such_table")


def test_driver_error_postgres_without_psycopg_raises_import_error(monkeypatch):
    """See ``test_connect_postgres_without_psycopg_raises_import_error`` for
    why this forces the absence rather than assuming it (#2886)."""
    monkeypatch.setitem(sys.modules, "psycopg", None)
    conn = _FakePostgresConnection()
    with pytest.raises(ImportError):
        sql.driver_error(conn)


def test_driver_error_unknown_dialect_raises():
    with pytest.raises(sql.UnsupportedDialectError):
        sql.driver_error(_FakeUnknownConnection())


# ── driver_errors (#2784) ─────────────────────────────────────────────────────
#
# Unlike driver_error(conn), this needs no connection -- it's for the ~18
# call sites wrapping a retry_on_locked(...) call (or another write with no
# connection in scope) that used to hardcode `except sqlite3.OperationalError:`,
# going silently inert under Postgres. psycopg is an optional dependency
# (#2886) that most runs of this suite don't have installed, so degrading to
# (sqlite3.Error,) rather than raising ImportError is the case actually
# under test here -- forced deterministically below rather than assumed, so
# this test still passes in the one CI job that DOES have the `[postgres]`
# extra installed (`postgres` in .github/workflows/test.yml).


def test_driver_errors_degrades_to_sqlite_only_when_psycopg_absent(monkeypatch):
    monkeypatch.setitem(sys.modules, "psycopg", None)
    assert sql.driver_errors() == (sqlite3.Error,)


def test_driver_errors_catches_a_real_sqlite_operational_error(memdb):
    with pytest.raises(sql.driver_errors()):
        memdb.execute("SELECT * FROM no_such_table")


def test_driver_errors_is_a_tuple_suitable_for_except():
    errors = sql.driver_errors()
    assert isinstance(errors, tuple)
    assert sqlite3.Error in errors


# ── is_lock_contention_error dialect dispatch (#2784) ───────────────────────
#
# coord.db.is_lock_contention_error dispatches on the exception itself, no
# connection and no config flag -- these synthesize a psycopg-shaped
# exception (an `.sqlstate` attribute is all that's needed) without psycopg
# actually being installed, exercising the Postgres arm the same way the
# fake-connection classes above exercise dialect detection without a real
# driver.


class _FakePsycopgOperationalError(Exception):
    """Stands in for `psycopg.OperationalError`, which carries `.sqlstate`
    (psycopg3) -- no real psycopg needed, matching this module's existing
    approach of faking driver *shape* rather than importing the driver."""

    def __init__(self, message: str, sqlstate: str) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


class _FakePsycopg2OperationalError(Exception):
    """Stands in for `psycopg2.OperationalError`, which carries `.pgcode`
    instead of `.sqlstate`."""

    def __init__(self, message: str, pgcode: str) -> None:
        super().__init__(message)
        self.pgcode = pgcode


@pytest.mark.parametrize(
    "sqlstate",
    ["55P03", "40001", "40P01"],
    ids=["lock_not_available", "serialization_failure", "deadlock_detected"],
)
def test_is_lock_contention_error_true_for_each_postgres_sqlstate(sqlstate):
    from coord.db import is_lock_contention_error

    exc = _FakePsycopgOperationalError("contention", sqlstate=sqlstate)
    assert is_lock_contention_error(exc) is True


def test_is_lock_contention_error_true_for_psycopg2_pgcode():
    from coord.db import is_lock_contention_error

    exc = _FakePsycopg2OperationalError("contention", pgcode="40P01")
    assert is_lock_contention_error(exc) is True


def test_is_lock_contention_error_false_for_unrelated_postgres_sqlstate():
    from coord.db import is_lock_contention_error

    # 42601 syntax_error -- a real bug, not contention; must not be retried.
    exc = _FakePsycopgOperationalError("syntax error", sqlstate="42601")
    assert is_lock_contention_error(exc) is False


def test_is_lock_contention_error_false_for_query_canceled_statement_timeout():
    from coord.db import is_lock_contention_error

    # 57014 query_canceled: contention-shaped but deliberately excluded (see
    # coord/db.py's _POSTGRES_LOCK_CONTENTION_SQLSTATES comment) -- a
    # statement that times out may just be slow/wrong, not lock-blocked, so
    # this must NOT be treated as retry-safe contention.
    exc = _FakePsycopgOperationalError("canceled", sqlstate="57014")
    assert is_lock_contention_error(exc) is False


def test_is_lock_contention_error_false_for_unrelated_exception_with_no_sqlstate():
    from coord.db import is_lock_contention_error

    assert is_lock_contention_error(ValueError("not a DB error at all")) is False


# ── round trip against a real server (#2886) ────────────────────────────────
#
# Everything above proves what SQL text/params coord.sql *would* send to a
# real driver, using a fake connection whose __module__ merely claims to be
# "psycopg" (see the module docstring). None of it proves Postgres *accepts*
# that text -- coord/sql.py has spoken Postgres since #2719 and none of its
# Postgres branches had ever executed against a real server before this
# section existed. These tests fill that gap for the branches #2886 calls
# out by name: execute/executemany translation, upsert, insert_ignore,
# insert_returning_id's RETURNING path, table_columns against
# information_schema, apply_row_factory's dict_row, and
# autoincrement_pk_ddl's identity-column DDL actually creating a table.
#
# `psycopg` is an optional dependency (the `postgres` extra) and most runs of
# this suite have neither it nor a reachable server -- both the driver import
# and the connection attempt below skip cleanly (pytest.importorskip /
# pytest.skip) rather than raising, so `pytest tests/test_sql_dialect.py`
# with no Postgres anywhere behaves exactly as it did before this section
# existed: skips, not errors, and NOT gated on `COORD_TEST_BACKEND` (these
# tests must skip cleanly under the suite's default, unset backend too, not
# just under an explicit non-`postgres` selection). CI's `postgres` job in
# .github/workflows/test.yml provides both, and reuses tests/backends.py's
# `COORD_TEST_POSTGRES_DSN` (#2884) so a developer or CI run only ever needs
# to set the one env var to make both that harness and these targeted tests
# exercise the same server.


def _require_psycopg() -> Any:
    return pytest.importorskip(
        "psycopg",
        reason="psycopg not installed -- pip install 'code-coordinator[postgres]'",
    )


@pytest.fixture
def pg_conn() -> Iterator[Any]:
    """A real ``psycopg`` connection, opened through :func:`sql.connect` (the
    #827 connection factory, so that factory itself is exercised for real
    too) -- in its own private schema, dropped on teardown.

    Schema-per-test, the same isolation strategy ``tests/backends.py`` uses
    for the whole suite's own Postgres opt-in (#2884) -- a small,
    self-contained copy rather than reaching into that module's private
    ``_open_postgres`` helper, because this fixture has a different
    contract: it must skip cleanly under the suite's *default*
    ``COORD_TEST_BACKEND`` (unset, i.e. SQLite), not just under an explicit
    ``postgres`` selection -- see the section header above.

    Row factory is left at psycopg's default (tuple rows) -- matching
    ``sqlite3``'s own default, which the ``memdb`` fixture above relies on
    -- so a test that cares about ``dict_row`` calls
    :func:`sql.apply_row_factory` itself, the same way
    ``test_apply_row_factory_makes_rows_dict_accessible`` does for SQLite.
    """
    _require_psycopg()
    dsn = postgres_dsn()
    try:
        conn = sql.connect(backend=sql.DIALECT_POSTGRES, dsn=dsn)
    except Exception as exc:  # noqa: BLE001 -- any connect failure is a skip, not a failure
        pytest.skip(f"Postgres not reachable at {dsn!r} ({DSN_ENV_VAR}): {exc}")
    schema = f"coord_sql_dialect_test_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
        # search_path is a session setting, so every later statement on this
        # connection resolves unqualified table names into the private
        # schema without any test needing to know it exists.
        cur.execute(f'SET search_path TO "{schema}"')
    conn.commit()
    try:
        yield conn
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SET search_path TO public")
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        conn.commit()
        conn.close()


def test_detect_dialect_real_postgres_connection(pg_conn):
    assert sql.detect_dialect(pg_conn) == sql.DIALECT_POSTGRES


def test_connect_postgres_round_trip_opens_a_working_connection(pg_conn):
    """:func:`sql.connect` itself (#827), not just its ``ImportError`` guard
    (already covered above) -- ``pg_conn`` opened through it; this proves
    the result is a live, usable connection."""
    cur = sql.execute(pg_conn, "SELECT 1")
    assert cur.fetchone() == (1,)


def test_execute_and_executemany_round_trip_against_real_postgres(pg_conn):
    sql.execute(pg_conn, "CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT, b TEXT)")
    sql.execute(pg_conn, "INSERT INTO t (id, a, b) VALUES (?, ?, ?)", (1, "x", "y"))
    sql.executemany(
        pg_conn,
        "INSERT INTO t (id, a, b) VALUES (?, ?, ?)",
        [(2, "p", "q"), (3, "r", "s")],
    )
    pg_conn.commit()
    cur = sql.execute(pg_conn, "SELECT id, a, b FROM t ORDER BY id")
    assert cur.fetchall() == [(1, "x", "y"), (2, "p", "q"), (3, "r", "s")]


def test_execute_preserves_literal_question_mark_against_real_postgres(pg_conn):
    sql.execute(pg_conn, "CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT, b TEXT)")
    sql.execute(pg_conn, "INSERT INTO t (id, a, b) VALUES (?, ?, ?)", (1, "why?", "z"))
    pg_conn.commit()
    cur = sql.execute(pg_conn, "SELECT a FROM t WHERE b = ? AND a = 'why?'", ("z",))
    assert cur.fetchone() == ("why?",)


def test_upsert_round_trips_against_real_postgres(pg_conn):
    sql.execute(pg_conn, "CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT, b TEXT)")
    pg_conn.commit()
    sql.upsert(pg_conn, "t", ["id", "a", "b"], (1, "first", "y"), conflict_columns=["id"])
    pg_conn.commit()
    sql.upsert(pg_conn, "t", ["id", "a", "b"], (1, "second", "z"), conflict_columns=["id"])
    pg_conn.commit()
    cur = sql.execute(pg_conn, "SELECT a, b FROM t WHERE id = ?", (1,))
    assert cur.fetchone() == ("second", "z")
    assert sql.execute(pg_conn, "SELECT COUNT(*) FROM t").fetchone() == (1,)


def test_insert_ignore_round_trips_against_real_postgres(pg_conn):
    sql.execute(pg_conn, "CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT)")
    pg_conn.commit()
    sql.insert_ignore(pg_conn, "t", ["id", "a"], (1, "first"))
    pg_conn.commit()
    sql.insert_ignore(pg_conn, "t", ["id", "a"], (1, "second"))
    pg_conn.commit()
    cur = sql.execute(pg_conn, "SELECT a FROM t WHERE id = ?", (1,))
    assert cur.fetchone() == ("first",)


def test_insert_returning_id_round_trips_against_real_postgres(pg_conn):
    ddl = sql.autoincrement_pk_ddl(sql.DIALECT_POSTGRES)
    sql.execute(pg_conn, f"CREATE TABLE t (id {ddl}, a TEXT)")
    pg_conn.commit()
    new_id = sql.insert_returning_id(pg_conn, "INSERT INTO t (a) VALUES (?)", ("x",))
    pg_conn.commit()
    assert isinstance(new_id, int)
    cur = sql.execute(pg_conn, "SELECT a FROM t WHERE id = ?", (new_id,))
    assert cur.fetchone() == ("x",)


def test_table_columns_round_trips_against_real_information_schema(pg_conn):
    sql.execute(pg_conn, "CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT, b TEXT)")
    pg_conn.commit()
    columns = sql.table_columns(pg_conn, "t")
    assert [name for name, _type in columns] == ["id", "a", "b"]


def test_table_columns_round_trips_empty_for_missing_table_against_real_postgres(pg_conn):
    assert sql.table_columns(pg_conn, "no_such_table") == []


def test_apply_row_factory_dict_row_round_trips_against_real_postgres(pg_conn):
    sql.apply_row_factory(pg_conn)
    sql.execute(pg_conn, "CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT, b TEXT)")
    sql.execute(pg_conn, "INSERT INTO t (id, a, b) VALUES (?, ?, ?)", (1, "x", "y"))
    pg_conn.commit()
    row = sql.execute(pg_conn, "SELECT a, b FROM t WHERE id = ?", (1,)).fetchone()
    assert row["a"] == "x"
    assert row["b"] == "y"


def test_autoincrement_pk_ddl_round_trips_against_real_postgres(pg_conn):
    """The identity-column DDL fragment actually creating a table and
    actually generating monotonic ids -- this branch is pure DDL text with
    no dedicated round trip other than using it exactly as coord/db.py
    does: substituted into a CREATE TABLE and then inserted through."""
    ddl = sql.autoincrement_pk_ddl(sql.DIALECT_POSTGRES)
    sql.execute(pg_conn, f"CREATE TABLE t (id {ddl}, a TEXT)")
    pg_conn.commit()
    first = sql.insert_returning_id(pg_conn, "INSERT INTO t (a) VALUES (?)", ("x",))
    second = sql.insert_returning_id(pg_conn, "INSERT INTO t (a) VALUES (?)", ("y",))
    pg_conn.commit()
    assert second == first + 1


def test_foreign_keys_round_trips_against_real_postgres(pg_conn):
    sql.execute(pg_conn, "CREATE TABLE parent (id INTEGER PRIMARY KEY)")
    sql.execute(
        pg_conn,
        "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER "
        "REFERENCES parent(id))",
    )
    pg_conn.commit()
    assert sql.foreign_keys(pg_conn, "child") == [("parent_id", "parent", "id")]
    assert sql.foreign_keys(pg_conn, "parent") == []


def test_identity_columns_and_resync_round_trip_against_real_postgres(pg_conn):
    """The exact sequence :func:`coord.store_migrate.import_table` runs:
    bulk-insert explicit ids into a GENERATED BY DEFAULT identity column,
    resync, then confirm an ordinary no-explicit-id INSERT doesn't collide
    with an id the bulk insert already used."""
    ddl = sql.autoincrement_pk_ddl(sql.DIALECT_POSTGRES)
    sql.execute(pg_conn, f"CREATE TABLE t (id {ddl}, a TEXT)")
    pg_conn.commit()
    assert sql.identity_columns(pg_conn, "t") == ["id"]

    # Simulate a bulk import writing explicit, non-sequential ids.
    sql.executemany(
        pg_conn, "INSERT INTO t (id, a) VALUES (?, ?)", [(1, "x"), (5, "y")]
    )
    pg_conn.commit()

    sql.resync_identity_sequence(pg_conn, "t", "id")
    pg_conn.commit()

    # An ordinary write with no explicit id must get something > 5, not
    # collide with the id=5 the "import" already used.
    new_id = sql.insert_returning_id(pg_conn, "INSERT INTO t (a) VALUES (?)", ("z",))
    pg_conn.commit()
    assert new_id > 5


def test_resync_identity_sequence_handles_empty_table_against_real_postgres(pg_conn):
    """No rows imported -- resync must leave the sequence at its start value
    (1), not skip ahead, per :func:`resync_identity_sequence`'s ``is_called``
    handling of the empty-table case."""
    ddl = sql.autoincrement_pk_ddl(sql.DIALECT_POSTGRES)
    sql.execute(pg_conn, f"CREATE TABLE t (id {ddl}, a TEXT)")
    pg_conn.commit()

    sql.resync_identity_sequence(pg_conn, "t", "id")
    pg_conn.commit()

    new_id = sql.insert_returning_id(pg_conn, "INSERT INTO t (a) VALUES (?)", ("x",))
    assert new_id == 1


# ── #3083's six signatures, against a real server ───────────────────────────
#
# The fake-connection tests further down assert on the SQL text that *would*
# reach a driver; these assert the server actually accepts it. Every one of
# them reproduced a real `postgres`-job failure before #3083, and each skips
# (never silently passes) without psycopg + a reachable server -- see the
# `pg_conn` fixture.


def test_qmark_via_the_proxy_round_trips_against_real_postgres(pg_conn):
    """Signature 1: `the query has 0 placeholders but N parameters were
    passed` -- what ~460 test bodies' direct `conn.execute("... ?")` hit."""
    conn = sql.translating_connection(pg_conn)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT)")
    conn.execute("INSERT INTO t (id, a) VALUES (?, ?)", (1, "x"))
    pg_conn.commit()
    assert conn.execute("SELECT a FROM t WHERE id = ?", (1,)).fetchone()[0] == "x"


def test_named_params_via_the_proxy_round_trip_against_real_postgres(pg_conn):
    """Signature 4: `syntax error at or near ":"`."""
    conn = sql.translating_connection(pg_conn)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT)")
    conn.execute("INSERT INTO t (id, a) VALUES (:id, :a)", {"id": 1, "a": "x"})
    pg_conn.commit()
    assert conn.execute("SELECT a FROM t WHERE id = ?", (1,)).fetchone()[0] == "x"


def test_state_upserts_are_accepted_by_a_real_postgres_server(pg_conn):
    """Signatures 2 and 3 at once, against the real schema:
    `AmbiguousColumn: column reference "finished_at" is ambiguous` and
    `the query has 17 placeholders but 21 parameters were passed`.

    Both statements are run verbatim, through the seam, against a schema
    built by ``coord.db._ensure_schema`` -- so this fails if either the
    ``DO UPDATE SET`` qualification or the comment-aware translation
    regresses, and it cannot pass vacuously (the second call of each pair
    exercises the conflict branch, not just the insert).
    """
    from coord.db import _ensure_schema
    from coord.models import Assignment
    from coord.state import (
        _DISPATCHED_UPSERT_SQL,
        _UPSERT_SQL,
        _assignment_upsert_params,
    )

    _ensure_schema(pg_conn)
    pg_conn.commit()

    # #1553's correlated-subquery upsert: 21 parameters, and the four after
    # the "child's Pipeline row" comment are the ones that used to vanish.
    dispatch_params = (
        "aid-1", "m1", "repo", "acme/repo", 7, "title", "work", "briefing",
        "[]", "model", 1000.0, None, None, "[]", 0, "claude", "issue-7", None,
        None, None, None,
    )
    assert len(dispatch_params) == _qmark_placeholder_count(_DISPATCHED_UPSERT_SQL)
    sql.execute(pg_conn, _DISPATCHED_UPSERT_SQL, dispatch_params)
    sql.execute(pg_conn, _DISPATCHED_UPSERT_SQL, dispatch_params)  # the DO UPDATE half
    pg_conn.commit()

    # The whole-board upsert, with its params built by the same function
    # save_board() uses -- so the tuple can never drift from the statement.
    assignment = Assignment(
        assignment_id="aid-1",
        machine_name="m1",
        repo_name="repo",
        issue_number=7,
        issue_title="title",
        status="done",
        finished_at=2000.0,
    )
    board_params = _assignment_upsert_params(assignment)
    assert len(board_params) == _qmark_placeholder_count(_UPSERT_SQL)
    sql.execute(pg_conn, _UPSERT_SQL, board_params)
    sql.execute(pg_conn, _UPSERT_SQL, board_params)  # the DO UPDATE half
    pg_conn.commit()

    # Indexed positionally: `pg_conn` deliberately applies no row factory
    # (it is `sql.connect()`'s raw result), unlike the `coord_db` fixture.
    status, finished_at = sql.execute(
        pg_conn,
        "SELECT status, finished_at FROM assignments WHERE assignment_id = ?",
        ("aid-1",),
    ).fetchone()
    assert status == "done"
    assert finished_at == 2000.0


def test_index_names_round_trips_against_real_postgres(pg_conn):
    """Signature 5: `syntax error at or near "PRAGMA"` -- index
    introspection, which SQLite spells as a pragma."""
    sql.execute(pg_conn, "CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT)")
    sql.execute(pg_conn, "CREATE INDEX idx_t_a ON t (a)")
    pg_conn.commit()
    assert "idx_t_a" in sql.index_names(pg_conn, "t")


def test_enable_foreign_keys_is_accepted_by_a_real_postgres_server(pg_conn):
    """Signature 5, the other half: the no-op must genuinely emit nothing
    rather than a PRAGMA the server would reject."""
    sql.enable_foreign_keys(pg_conn)
    assert sql.execute(pg_conn, "SELECT 1").fetchone()[0] == 1


def test_greatest_round_trips_against_real_postgres(pg_conn):
    """Signature 6: `function max(integer, smallint) does not exist`."""
    sql.execute(pg_conn, "CREATE TABLE s (id TEXT PRIMARY KEY, rev INTEGER)")
    sql.execute(pg_conn, "INSERT INTO s VALUES (?, ?)", ("x", 5))
    pg_conn.commit()
    stmt = f"UPDATE s SET rev = {sql.greatest(pg_conn, 'rev', '?')} WHERE id = ?"
    sql.execute(pg_conn, stmt, (3, "x"))
    assert sql.execute(pg_conn, "SELECT rev FROM s").fetchone()[0] == 5
    sql.execute(pg_conn, stmt, (9, "x"))
    assert sql.execute(pg_conn, "SELECT rev FROM s").fetchone()[0] == 9


# ── redact_dsn (#828) ────────────────────────────────────────────────────


def test_redact_dsn_uri_form_strips_credentials():
    assert (
        sql.redact_dsn("postgresql://coorduser:s3cr3t@dbhost.example:5432/coord")
        == "postgresql://dbhost.example:5432/coord"
    )


def test_redact_dsn_uri_form_no_port():
    assert sql.redact_dsn("postgresql://user:pw@dbhost/coord") == "postgresql://dbhost/coord"


def test_redact_dsn_uri_form_with_no_credentials_present():
    assert sql.redact_dsn("postgresql://dbhost:5432/coord") == "postgresql://dbhost:5432/coord"


def test_redact_dsn_keyword_value_form_strips_user_and_password():
    redacted = sql.redact_dsn(
        "host=dbhost.example port=5432 dbname=coord user=coorduser password=s3cr3t"
    )
    assert redacted == "host=dbhost.example:5432 dbname=coord"
    assert "s3cr3t" not in redacted
    assert "coorduser" not in redacted


def test_redact_dsn_keyword_value_form_quoted_password_is_stripped():
    redacted = sql.redact_dsn("host=dbhost dbname=coord password='has space pw'")
    assert "has space pw" not in redacted
    assert redacted == "host=dbhost dbname=coord"


def test_redact_dsn_keyword_value_form_missing_fields_render_as_placeholder():
    assert sql.redact_dsn("user=coorduser password=s3cr3t") == "host=? dbname=?"


# ── identity_columns / resync_identity_sequence (#828) ──────────────────────


def test_identity_columns_sqlite_is_always_empty(memdb):
    assert sql.identity_columns(memdb, "t") == []


def test_identity_columns_postgres_queries_information_schema():
    conn = _FakePostgresConnection(fetchall_result=[("id",)])
    assert sql.identity_columns(conn, "widgets") == ["id"]
    [(sent_sql, sent_params)] = conn.cur.executed
    assert "identity_generation IS NOT NULL" in sent_sql
    assert sent_params == ("widgets",)


def test_identity_columns_postgres_tolerates_dict_row_factory():
    conn = _FakePostgresConnection(fetchall_result=[{"column_name": "id"}])
    assert sql.identity_columns(conn, "widgets") == ["id"]


def test_identity_columns_postgres_empty_for_no_identity_column():
    conn = _FakePostgresConnection(fetchall_result=[])
    assert sql.identity_columns(conn, "widgets") == []


def test_resync_identity_sequence_sqlite_is_a_noop(memdb):
    # No exception, and nothing executed against the connection at all.
    sql.resync_identity_sequence(memdb, "t", "id")


def test_resync_identity_sequence_postgres_calls_setval_with_table_and_column():
    conn = _FakePostgresConnection()
    sql.resync_identity_sequence(conn, "widgets", "id")
    [(sent_sql, sent_params)] = conn.cur.executed
    assert "setval(pg_get_serial_sequence" in sent_sql
    assert 'MAX("id")' in sent_sql
    assert '"widgets"' in sent_sql
    assert sent_params == ("widgets", "id")


# ── #3083 helpers: SQL-text scanners shared by the tests and the ratchets ───


def _scan_placeholders(sql_text: str) -> tuple[int, list[str]]:
    """``(count of bare ``?``, [named ``:param`` names])`` in *sql_text*,
    counting only placeholders **outside** quoted string literals and
    ``--``/``/* */`` SQL comments.

    The comment half is exactly what ``coord.sql._qmark_to_pyformat`` was
    missing before #3083 (a rhetorical ``-- which cursor?`` in coord/db.py's
    schema DDL, an apostrophe in coord/state.py's dispatch upsert), and it
    is needed here for the same reason: this is the reference answer the
    seam's own translation is checked against.
    """
    qmarks = 0
    named: list[str] = []
    in_string: str | None = None
    i, n = 0, len(sql_text)
    while i < n:
        ch = sql_text[i]
        if in_string is not None:
            if ch == in_string:
                if sql_text[i : i + 2] == in_string * 2:
                    i += 2
                    continue
                in_string = None
            i += 1
            continue
        if sql_text[i : i + 2] == "--":
            end = sql_text.find("\n", i)
            i = n if end == -1 else end
            continue
        if sql_text[i : i + 2] == "/*":
            depth, j = 1, i + 2
            while j < n and depth:
                if sql_text[j : j + 2] == "/*":
                    depth += 1
                    j += 2
                elif sql_text[j : j + 2] == "*/":
                    depth -= 1
                    j += 2
                else:
                    j += 1
            i = j
            continue
        if ch in ("'", '"'):
            in_string = ch
            i += 1
            continue
        if ch == "?":
            qmarks += 1
            i += 1
            continue
        if ch == ":":
            if sql_text[i : i + 2] == "::":  # Postgres cast, not a placeholder
                i += 2
                continue
            match = sql._NAMED_PARAM_RE.match(sql_text, i)
            if match is not None:
                named.append(match.group(1))
                i = match.end()
                continue
        i += 1
    return qmarks, named


def _qmark_placeholder_count(sql_text: str) -> int:
    return _scan_placeholders(sql_text)[0]


def _named_placeholders(sql_text: str) -> list[str]:
    return _scan_placeholders(sql_text)[1]


#: Words that may legitimately appear bare on the right-hand side of a
#: ``DO UPDATE SET`` — SQL keywords, operators and function names, none of
#: which are column references.
_DO_UPDATE_NON_COLUMNS = {
    "CASE", "WHEN", "THEN", "ELSE", "END", "COALESCE", "NULL", "AND", "OR",
    "NOT", "IS", "IN", "SELECT", "FROM", "WHERE", "CAST", "AS", "DO",
    "UPDATE", "SET", "ON", "CONFLICT", "NOTHING", "excluded", "MAX", "MIN",
    "GREATEST", "LEAST", "TRUE", "FALSE", "DEFAULT",
}


def _unqualified_do_update_refs(sql_text: str) -> list[str]:
    """Bare (unqualified) column references on the right-hand side of
    *sql_text*'s ``ON CONFLICT ... DO UPDATE SET`` clause.

    Inside a ``DO UPDATE``, both the target table and the ``excluded``
    pseudo-row are in scope, and ``excluded`` carries every column of the
    target — so a bare column name on the right-hand side is ambiguous.
    Postgres raises ``AmbiguousColumn``; SQLite silently resolves it to the
    target row, which is why this cannot be caught by running the SQLite
    suite. The SET *targets* (left of each ``=``, at clause depth) are
    excluded here: those are unambiguous by construction, and Postgres
    rejects a qualified one.
    """
    if "DO UPDATE SET" not in sql_text:
        return []
    body = sql_text.split("DO UPDATE SET", 1)[1]
    body = re.sub(r"--[^\n]*", " ", body)
    body = re.sub(r"/\*.*?\*/", " ", body, flags=re.S)
    body = re.sub(r"'(?:[^']|'')*'", " '' ", body)

    tokens = [m.group(0) for m in re.finditer(r"[A-Za-z_]\w*|[(),.=]|\S", body)]
    offenders: list[str] = []
    depth = 0
    for index, token in enumerate(tokens):
        if token == "(":
            depth += 1
            continue
        if token == ")":
            depth -= 1
            continue
        if not re.fullmatch(r"[A-Za-z_]\w*", token):
            continue
        if token in _DO_UPDATE_NON_COLUMNS:
            continue
        previous = tokens[index - 1] if index else None
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        if previous == ".":          # `excluded.x` / `assignments.x` — qualified
            continue
        if following == ".":         # the qualifier itself
            continue
        if depth == 0 and following == "=" and previous in (None, ","):
            continue                 # a SET target, not a reference
        offenders.append(token)
    return sorted(set(offenders))


def _two_argument_max_sites(path: Path) -> list[str]:
    """``"file:line: text"`` for every SQL literal in *path* naming a
    two-argument ``MAX(a, b)`` — SQLite's scalar max, which Postgres spells
    ``GREATEST`` and rejects as ``function max(integer, smallint) does not
    exist``.

    The one-argument aggregate ``MAX(x)`` is identical on both backends and
    is deliberately not flagged; the discriminator is a comma at the
    function's own paren depth.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstring_ids = _docstring_constant_ids(tree)
    sites: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in docstring_ids:
            continue  # prose, e.g. a docstring explaining `max(0, x - y)`
        text = node.value
        if not _SQL_LIKE_RE.match(text):
            continue
        for match in re.finditer(r"\bMAX\s*\(", text, re.IGNORECASE):
            depth, i, n = 1, match.end(), len(text)
            while i < n and depth:
                ch = text[i]
                if ch in ("'", '"'):
                    quote, i = ch, i + 1
                    while i < n and text[i] != quote:
                        i += 1
                elif ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                elif ch == "," and depth == 1:
                    sites.append(f"{path.name}:{node.lineno}: {match.group(0)}...")
                    depth = 0
                    break
                i += 1
    return sites


# ── #3083: one seam-level test per Postgres-lane failure signature ──────────
#
# #3083's second acceptance bullet: each of the six mechanically-diagnosable
# signatures in the `postgres` job gets a test that exercises the *seam*
# directly, not only the call site that happened to surface it. Fixing a
# call site without one just moves the next occurrence a commit away — the
# same reasoning #2096 makes about a fix nobody can show was ever red.
#
# Each test below names its signature verbatim in the docstring, and each is
# red against the pre-#3083 code:
#
#   1. `the query has 0 placeholders but N parameters were passed`
#      — TranslatingConnection did not exist, so ~460 test bodies' direct
#        `conn.execute("... ?", params)` reached psycopg untranslated.
#   2. `AmbiguousColumn: column reference "finished_at" is ambiguous`
#      — coord/state.py's upserts referenced stored columns unqualified
#        inside `DO UPDATE SET`, where `excluded` also has them.
#   3. `the query has 17 placeholders but 21 parameters were passed`
#      — _qmark_to_pyformat had no notion of SQL comments, so an apostrophe
#        in one opened a phantom string literal that ate 4 placeholders.
#   4. `syntax error at or near ":"`
#      — translate() rewrote `?` only, never sqlite3's `named` paramstyle.
#   5. `syntax error at or near "PRAGMA"`
#      — no portable index-introspection / FK-enforcement helper existed, so
#        call sites carried literal PRAGMA text.
#   6. `function max(integer, smallint) does not exist`
#      — no sql.greatest(); the two-argument scalar MAX went out verbatim.


class TestSignatureZeroPlaceholders:
    """`the query has 0 placeholders but N parameters were passed`.

    The largest signature in the lane (~460), and the most misleading: the
    statement is not mistranslated, it is *untranslated* — `?` simply is not
    a placeholder in Postgres, so psycopg counts zero of them and refuses
    the parameters. :class:`coord.sql.TranslatingConnection` is the fix, and
    ``tests/backends.py`` is the one place that installs it.
    """

    def test_direct_conn_execute_translates_qmark_to_pyformat(self):
        raw = _FakePostgresConnection()
        conn = sql.translating_connection(raw)

        conn.execute("SELECT a FROM t WHERE b = ? AND c = ?", ("x", "y"))

        [(sent_sql, sent_params)] = raw.cur.executed
        assert sent_sql == "SELECT a FROM t WHERE b = %s AND c = %s"
        assert sent_sql.count("%s") == len(sent_params) == 2

    def test_direct_conn_executemany_translates_too(self):
        raw = _FakePostgresConnection()
        conn = sql.translating_connection(raw)

        conn.executemany("INSERT INTO t (a, b) VALUES (?, ?)", [(1, 2), (3, 4)])

        [(sent_sql, seq)] = raw.cur.executemany_calls
        assert sent_sql == "INSERT INTO t (a, b) VALUES (%s, %s)"
        assert seq == [(1, 2), (3, 4)]

    def test_wrapping_sqlite_leaves_the_statement_alone(self, memdb):
        """qmark is SQLite's native style, so the proxy must be a pure
        pass-through there — the default lane must not change shape."""
        conn = sql.translating_connection(memdb)
        conn.execute("INSERT INTO t (id, a) VALUES (?, ?)", (1, "x"))

        assert conn.execute("SELECT a FROM t WHERE id = ?", (1,)).fetchone()[0] == "x"

    def test_proxy_is_transparent_for_everything_else(self, memdb):
        """Only the three statement-running methods are intercepted; the
        rest of the connection has to behave as if the proxy weren't there,
        or wrapping it in ``tests/backends.py`` breaks the whole suite."""
        conn = sql.translating_connection(memdb)

        assert sql.detect_dialect(conn) == sql.DIALECT_SQLITE
        assert sql.unwrap(conn) is memdb
        assert getattr(conn, "closed", False) is False  # coord/db.py's #3082 check
        conn.row_factory = sqlite3.Row  # settable attribute forwards
        assert memdb.row_factory is sqlite3.Row
        with conn:  # context manager forwards (state.py's `with conn:` writes)
            conn.execute("INSERT INTO t (id) VALUES (?)", (1,))
        assert memdb.cursor().execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1

    def test_wrapping_is_idempotent(self):
        once = sql.translating_connection(_FakePostgresConnection())
        assert sql.translating_connection(once) is once

    def test_seam_functions_accept_a_wrapped_connection(self):
        """`sql.execute(conn, ...)` must keep working when handed the proxy
        — the fixture hands the *same* object to both the test body and
        every ``coord/`` writer under test."""
        raw = _FakePostgresConnection()
        sql.execute(sql.translating_connection(raw), "SELECT ? ", (1,))
        assert raw.cur.executed == [("SELECT %s ", (1,))]


class TestSignatureAmbiguousFinishedAt:
    """`AmbiguousColumn: column reference "finished_at" is ambiguous`.

    Inside `ON CONFLICT ... DO UPDATE SET`, both the target table and the
    `excluded` pseudo-row are in scope, and `excluded` carries *every*
    column of the target — so an unqualified stored-column reference on the
    right-hand side is genuinely ambiguous. SQLite silently resolves it to
    the target row; Postgres refuses the statement.
    """

    def test_state_upsert_qualifies_every_stored_column_reference(self):
        """The regression guard for ``coord/state.py``'s two upserts: no
        bare column name may appear on the right-hand side of a
        ``DO UPDATE SET``."""
        from coord.state import _UPSERT_SQL

        offenders = _unqualified_do_update_refs(_UPSERT_SQL)
        assert not offenders, (
            "unqualified column reference(s) on the right-hand side of "
            "coord/state.py's _UPSERT_SQL DO UPDATE SET (#3083) — Postgres "
            f"rejects these as ambiguous against `excluded`: {offenders}"
        )

    def test_finished_at_cas_still_means_the_same_thing_on_sqlite(self, memdb):
        """Qualifying must be *only* a disambiguation: the #1451 CAS still
        has to refuse a stale terminal write."""
        sql.execute(memdb, "CREATE TABLE a (id TEXT PRIMARY KEY, status TEXT, finished_at REAL)")
        sql.execute(memdb, "INSERT INTO a VALUES ('x', 'done', 100.0)")
        upsert = """
            INSERT INTO a (id, status, finished_at) VALUES (?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status = CASE
                    WHEN a.finished_at IS NULL THEN excluded.status
                    WHEN excluded.finished_at IS NOT NULL
                         AND excluded.finished_at >= a.finished_at THEN excluded.status
                    ELSE a.status
                END
        """
        sql.execute(memdb, upsert, ("x", "failed", 50.0))  # stale — refused
        assert sql.execute(memdb, "SELECT status FROM a").fetchone()[0] == "done"
        sql.execute(memdb, upsert, ("x", "merged", 200.0))  # newer — accepted
        assert sql.execute(memdb, "SELECT status FROM a").fetchone()[0] == "merged"


class TestSignaturePlaceholderCountMismatch:
    """`the query has 17 placeholders but 21 parameters were passed`.

    An apostrophe inside a ``--`` comment used to open a string literal the
    translator never saw closed, so every ``?`` after it was treated as
    string content. ``coord/state.py``'s dispatch upsert has exactly that
    comment ("the child's Pipeline row"), four placeholders after it, and 21
    parameters — hence 17 vs 21.
    """

    def test_apostrophe_in_a_line_comment_does_not_swallow_placeholders(self):
        text = (
            "INSERT INTO t (a, b) VALUES (\n"
            "    ?,\n"
            "    -- which is exactly the \"child's Pipeline row\" bug\n"
            "    ?)"
        )
        assert sql.translate(text, sql.DIALECT_POSTGRES).count("%s") == 2

    def test_apostrophe_in_a_block_comment_does_not_swallow_placeholders(self):
        text = "SELECT ? /* the worker's own note */ , ? FROM t"
        assert sql.translate(text, sql.DIALECT_POSTGRES).count("%s") == 2

    def test_question_mark_inside_a_comment_is_not_a_placeholder(self):
        text = "SELECT a FROM t -- which cursor?\nWHERE b = ?"
        translated = sql.translate(text, sql.DIALECT_POSTGRES)
        assert translated.count("%s") == 1
        assert "which cursor?" in translated

    def test_percent_inside_a_comment_is_still_doubled(self):
        """pyformat is %-formatting, so a literal `%` anywhere — including a
        comment — must be escaped or psycopg misparses the statement."""
        translated = sql.translate("SELECT ? -- 100% sure\n", sql.DIALECT_POSTGRES)
        assert "100%% sure" in translated

    @pytest.mark.parametrize(
        "name",
        ["_UPSERT_SQL", "_DISPATCHED_UPSERT_SQL"],
    )
    def test_every_state_upsert_keeps_its_placeholder_count(self, name):
        """The two statements this signature was measured on, pinned: what
        goes in as ``?`` must come out as ``%s``, one for one."""
        import coord.state as state_mod

        text = getattr(state_mod, name)
        translated = sql.translate(text, sql.DIALECT_POSTGRES)
        assert translated.count("%s") == _qmark_placeholder_count(text)

    def test_state_upserts_round_trip_against_the_active_backend(self, coord_db):
        """Both statements run, twice each, against a real schema on
        whichever backend ``COORD_TEST_BACKEND`` selects.

        The paired assertions are the point: a parameter tuple that no
        longer matches its statement's placeholder count is exactly how
        signature 3 presented, and the `?`-vs-`%s` count is checked against
        the *translated* text, so a translation that silently drops
        placeholders fails here even on the SQLite lane where the statement
        itself would still run.
        """
        from coord.models import Assignment
        from coord.state import (
            _DISPATCHED_UPSERT_SQL,
            _UPSERT_SQL,
            _assignment_upsert_params,
        )

        dispatch_params = (
            "aid-1", "m1", "repo", "acme/repo", 7, "title", "work", "briefing",
            "[]", "model", 1000.0, None, None, "[]", 0, "claude", "issue-7",
            None, None, None, None,
        )
        assert len(dispatch_params) == _qmark_placeholder_count(_DISPATCHED_UPSERT_SQL)
        sql.execute(coord_db, _DISPATCHED_UPSERT_SQL, dispatch_params)
        sql.execute(coord_db, _DISPATCHED_UPSERT_SQL, dispatch_params)

        assignment = Assignment(
            assignment_id="aid-1",
            machine_name="m1",
            repo_name="repo",
            issue_number=7,
            issue_title="title",
            status="done",
            finished_at=2000.0,
        )
        board_params = _assignment_upsert_params(assignment)
        assert len(board_params) == _qmark_placeholder_count(_UPSERT_SQL)
        sql.execute(coord_db, _UPSERT_SQL, board_params)
        sql.execute(coord_db, _UPSERT_SQL, board_params)
        coord_db.commit()

        row = sql.execute(
            coord_db,
            "SELECT status, finished_at FROM assignments WHERE assignment_id = ?",
            ("aid-1",),
        ).fetchone()
        assert row["status"] == "done"
        assert row["finished_at"] == 2000.0

    def test_comment_awareness_does_not_break_real_string_literals(self):
        """The fix must not overcorrect: a `?` inside a genuine quoted
        string is still data, and a quote inside a comment still isn't."""
        text = "SELECT 'why?' FROM t -- it's fine\nWHERE a = ? AND b = 'ok?'"
        translated = sql.translate(text, sql.DIALECT_POSTGRES)
        assert translated.count("%s") == 1
        assert "'why?'" in translated and "'ok?'" in translated


class TestSignatureNamedPlaceholderSyntaxError:
    """`syntax error at or near ":"`.

    sqlite3 supports a second paramstyle — ``named`` (``:name``, bound from
    a dict) — and a handful of statements use it. The seam only ever
    rewrote ``?``, so those reached Postgres verbatim.
    """

    def test_named_placeholders_become_pyformat(self):
        translated = sql.translate(
            "INSERT INTO t (a, b) VALUES (:a, :b)", sql.DIALECT_POSTGRES
        )
        assert translated == "INSERT INTO t (a, b) VALUES (%(a)s, %(b)s)"

    def test_named_placeholders_are_untouched_on_sqlite(self):
        text = "INSERT INTO t (a) VALUES (:a)"
        assert sql.translate(text, sql.DIALECT_SQLITE) == text

    def test_named_placeholder_inside_a_string_literal_survives(self):
        translated = sql.translate(
            "SELECT ':not_a_param' FROM t WHERE a = :a", sql.DIALECT_POSTGRES
        )
        assert "':not_a_param'" in translated
        assert "%(a)s" in translated

    def test_postgres_cast_operator_is_not_a_named_placeholder(self):
        """``::`` is Postgres's cast operator — mangling it would break
        every statement the seam itself emits with one."""
        assert (
            sql.translate("SELECT x::int FROM t WHERE y = ?", sql.DIALECT_POSTGRES)
            == "SELECT x::int FROM t WHERE y = %s"
        )

    def test_named_statement_round_trips_through_the_proxy_with_a_dict(self):
        raw = _FakePostgresConnection()
        conn = sql.translating_connection(raw)

        conn.execute("INSERT INTO t (a, b) VALUES (:a, :b)", {"a": 1, "b": 2})

        [(sent_sql, sent_params)] = raw.cur.executed
        assert sent_sql == "INSERT INTO t (a, b) VALUES (%(a)s, %(b)s)"
        assert sent_params == {"a": 1, "b": 2}

    def test_named_statement_still_round_trips_against_sqlite(self, memdb):
        conn = sql.translating_connection(memdb)
        conn.execute("INSERT INTO t (id, a) VALUES (:id, :a)", {"id": 1, "a": "x"})
        assert conn.execute("SELECT a FROM t").fetchone()[0] == "x"


class TestSignaturePragmaSyntaxError:
    """`syntax error at or near "PRAGMA"`.

    ``PRAGMA`` is SQLite-only with no Postgres spelling at all, so the seam
    cannot translate it — the rule (``coord/serve_app.py``'s
    ``_wal_checkpoint_tick``, #2782) is that a caller asks for the *intent*
    through a named helper and the seam decides what that becomes.
    """

    def test_index_names_reports_declared_indexes_on_sqlite(self, memdb):
        sql.execute(memdb, "CREATE INDEX idx_t_a ON t (a)")
        assert "idx_t_a" in sql.index_names(memdb, "t")

    def test_index_names_is_empty_for_a_table_with_no_indexes(self, memdb):
        assert sql.index_names(memdb, "t") == []

    def test_index_names_postgres_queries_pg_indexes_not_pragma(self):
        conn = _FakePostgresConnection(fetchall_result=[("idx_t_a",)])
        assert sql.index_names(conn, "t") == ["idx_t_a"]
        [(sent_sql, sent_params)] = conn.cur.executed
        assert "PRAGMA" not in sent_sql
        assert "pg_indexes" in sent_sql
        assert sent_params == ("t",)

    def test_index_names_postgres_tolerates_a_dict_row_factory(self):
        conn = _FakePostgresConnection(fetchall_result=[{"name": "idx_t_a"}])
        assert sql.index_names(conn, "t") == ["idx_t_a"]

    def test_enable_foreign_keys_turns_them_on_for_sqlite(self, memdb):
        sql.enable_foreign_keys(memdb)
        assert sql.execute(memdb, "PRAGMA foreign_keys").fetchone()[0] == 1

    def test_enable_foreign_keys_is_a_documented_noop_on_postgres(self):
        """Postgres enforces declared constraints unconditionally — there is
        no session switch, so this must emit *nothing* rather than a PRAGMA
        the server cannot parse."""
        conn = _FakePostgresConnection()
        sql.enable_foreign_keys(conn)
        assert conn.cur.executed == []


class TestSignatureTwoArgumentMax:
    """`function max(integer, smallint) does not exist`.

    SQLite overloads ``MAX`` as both the one-argument aggregate and a
    multi-argument scalar. Postgres has only the aggregate; the scalar is
    ``GREATEST``. The error names an argument-type mismatch, which is why
    this one is easy to misread as a casting problem.
    """

    def test_greatest_emits_max_on_sqlite(self, memdb):
        assert sql.greatest(memdb, "a", "?") == "MAX(a, ?)"

    def test_greatest_emits_greatest_on_postgres(self):
        conn = _FakePostgresConnection()
        assert sql.greatest(conn, "a", "?") == "GREATEST(a, ?)"

    def test_greatest_takes_more_than_two_operands(self):
        conn = _FakePostgresConnection()
        assert sql.greatest(conn, "a", "b", "?") == "GREATEST(a, b, ?)"

    def test_greatest_refuses_a_single_operand(self, memdb):
        """A one-argument ``MAX`` is the *aggregate*, spelled identically on
        both backends — routing it through here would silently rewrite it to
        ``GREATEST`` and change its meaning."""
        with pytest.raises(ValueError, match="two operands"):
            sql.greatest(memdb, "a")

    def test_greatest_monotonic_update_still_only_raises_on_sqlite(self, memdb):
        """The behaviour both ``coord/portal_store.py`` call sites depend
        on: the counter never goes backwards."""
        sql.execute(memdb, "CREATE TABLE s (id TEXT PRIMARY KEY, rev INTEGER)")
        sql.execute(memdb, "INSERT INTO s VALUES ('x', 5)")
        stmt = f"UPDATE s SET rev = {sql.greatest(memdb, 'rev', '?')} WHERE id = ?"
        sql.execute(memdb, stmt, (3, "x"))
        assert sql.execute(memdb, "SELECT rev FROM s").fetchone()[0] == 5
        sql.execute(memdb, stmt, (9, "x"))
        assert sql.execute(memdb, "SELECT rev FROM s").fetchone()[0] == 9

    def test_portal_store_names_no_bare_two_argument_max(self):
        """``coord/portal_store.py``'s two sites go through the seam — the
        file-scoped half of the ratchet leg below."""
        offenders = _two_argument_max_sites(Path(sql.__file__).parent / "portal_store.py")
        assert not offenders, offenders


# ── the ratchet: no raw `?` reaches a driver outside the seam (#2768, #1948) ─
#
# #1948's first acceptance bullet: "No raw `?` placeholder reaches a driver:
# every execute() goes through the dialect seam, enforced by a test that
# greps the tree." Nothing implemented that until now — the 34 tests above
# all exercise coord/sql.py in isolation; none of them look at the rest of
# coord/**. This is that test, sequenced last (#2768) because it can only be
# green once every migration slice (#2721 -> #2766 -> #2767) has landed.
#
# AST over regex, per the issue: a `Call` node whose `func` is an
# `Attribute` named execute/executemany/executescript is unambiguous and
# can't be confused with the word "execute" inside a comment/docstring (this
# very module's own docstring says "``conn.execute()``" in prose — a naive
# grep would have to dodge that) or a same-named method on something that
# isn't a DB-API connection.
#
# Exactly one exemption, by explicit constant: coord/sql.py — it *is* the
# seam, so `cur.execute()` inside it is the seam doing its job, not a caller
# routing around it. coord/db.py needs none: every DDL/DML call site in it
# already goes through coord.sql (verified below, by the same walk, as part
# of the assertion rather than assumed) — #2724 genuinely left nothing that
# can't route through `sql.executescript()`.
_COORD_DIR = Path(sql.__file__).parent
_SEAM_RELPATH = "coord/sql.py"

# coord.sql function names that legitimately take a qmark-style SQL string
# and are the whole point of the seam — an argument to one of these is
# "through the seam", not a violation.
_SEAM_FUNCS = {
    "execute",
    "executemany",
    "executescript",
    "upsert",
    "insert_ignore",
    "insert_ignore_select",
    "insert_returning_id",
}

# The DB-API 2.0 cursor/connection methods a raw call must never reach
# outside the seam.
_DRIVER_METHODS = {"execute", "executemany", "executescript"}

_SQL_LIKE_RE = re.compile(
    r"^\s*(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|PRAGMA|REPLACE|WITH)\b",
    re.IGNORECASE,
)


def _tree_modules():
    """Every ``coord/**/*.py`` module, excluding the seam itself."""
    for path in sorted(_COORD_DIR.rglob("*.py")):
        rel = str(path.relative_to(_COORD_DIR.parent))
        if rel == _SEAM_RELPATH:
            continue
        yield rel, path


def _parse(path: Path):
    src = path.read_text(encoding="utf-8")
    return src, ast.parse(src, filename=str(path))


def _seam_alias_names(tree: ast.AST) -> set[str]:
    """Local names bound to the ``coord.sql`` module in this file.

    Covers the plain ``from coord import sql`` used almost everywhere, and
    the two call sites that alias it to dodge a same-named local — the
    ``_sql``/``_sql_wb`` lazy imports in ``coord/commands/dispatch.py`` and
    ``coord/commands/dispatch_workers.py``.
    """
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "coord":
            for alias in node.names:
                if alias.name == "sql":
                    names.add(alias.asname or "sql")
    return names


def _is_seam_call(call: ast.Call, seam_names: set[str]) -> bool:
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr in _SEAM_FUNCS
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id in seam_names
    )


def test_no_raw_driver_execute_call_outside_the_dialect_seam():
    """No ``.execute()``/``.executemany()``/``.executescript()`` call
    anywhere in ``coord/**`` reaches a driver directly — every one must be a
    call to ``coord.sql``'s wrapper of the same name.

    Deliberately introducing e.g. ``conn.execute("SELECT 1 WHERE x = ?",
    (1,))`` in any ``coord/`` module makes this red: it's an ``Attribute``
    call named ``execute`` whose ``func.value`` is not a name bound to
    ``coord.sql`` in that file.
    """
    violations = []
    for rel, path in _tree_modules():
        src, tree = _parse(path)
        seam_names = _seam_alias_names(tree)
        lines = src.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in _DRIVER_METHODS:
                continue
            if _is_seam_call(node, seam_names):
                continue
            lineno = node.lineno
            src_line = (
                lines[lineno - 1].strip() if 0 < lineno <= len(lines) else "<no source line>"
            )
            violations.append(f"{rel}:{lineno}: {src_line}")
    assert not violations, (
        "raw DB-API execute-family call(s) bypassing the coord.sql dialect "
        "seam (#2768/#1948) — route these through coord.sql.execute()/"
        "executemany()/executescript() instead:\n" + "\n".join(violations)
    )


# ── the ratchet, extended: no raw driver `.connect()` outside the seam (#827) ─
#
# #827's acceptance bullet: "db.py and dao.py obtain connections through one
# dialect-aware factory; neither names sqlite3.connect directly." The two
# call sites that used to do that (coord/db.py's `_open`, coord/dao.py's
# `SqliteStore._connect`) now route through `coord.sql.connect` -- this is
# the `connect`-call sibling of #2768's `execute`-call ratchet above, so a
# regression (a new module importing sqlite3/psycopg/psycopg2 directly and
# calling `.connect()` on it) is caught the same way: an AST walk, not a
# grep, so a docstring/comment merely mentioning "sqlite3.connect" can't
# trip it and a real call site can't dodge it by reformatting.

_CONNECT_DRIVER_MODULES = {"sqlite3", "psycopg", "psycopg2"}


def test_no_raw_driver_connect_call_outside_the_dialect_seam():
    """No ``sqlite3.connect()``/``psycopg.connect()``/``psycopg2.connect()``
    call anywhere in ``coord/**`` reaches a driver directly — every
    connection must be opened through ``coord.sql.connect()`` (#827).

    Deliberately introducing e.g. ``sqlite3.connect(":memory:")`` in any
    ``coord/`` module outside ``coord/sql.py`` makes this red.
    """
    violations = []
    for rel, path in _tree_modules():
        _src, tree = _parse(path)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr != "connect":
                continue
            value = node.func.value
            if isinstance(value, ast.Name) and value.id in _CONNECT_DRIVER_MODULES:
                violations.append(f"{rel}:{node.lineno}: {value.id}.connect(...)")
    assert not violations, (
        "raw driver .connect() call(s) bypassing the coord.sql dialect seam "
        "connection factory (#827) — route these through coord.sql.connect() "
        "instead:\n" + "\n".join(violations)
    )


def _reachable_seam_literal_ids(tree: ast.AST) -> set[int]:
    """``id()`` of every string-literal ``ast.Constant`` node in *tree* that
    is provably routed through a ``coord.sql`` seam call — either directly
    (the literal is itself a call argument) or transitively, through the
    one-hop local-assignment pattern ``coord/db.py`` uses for its schema
    constant (``_SCHEMA_SQL = "..."``; ``schema_sql =
    _SCHEMA_SQL.replace(...)``; ``sql.executescript(conn, schema_sql)``).

    This is a small fixed-point over local ``Assign``/``AnnAssign`` nodes,
    not full interprocedural dataflow — good enough for the shallow,
    single-file indirection this tree actually uses, and precise (no
    filename-based allowlisting) rather than a pattern that could paper
    over a genuinely unrouted literal.
    """
    seam_names = _seam_alias_names(tree)
    seam_call_args = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_seam_call(node, seam_names):
            seam_call_args.extend(node.args)
            seam_call_args.extend(kw.value for kw in node.keywords)

    protected_ids: set[int] = set()
    reachable_names: set[str] = set()
    for arg in seam_call_args:
        for sub in ast.walk(arg):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                protected_ids.add(id(sub))
            elif isinstance(sub, ast.Name):
                reachable_names.add(sub.id)

    assigns = [n for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.AnnAssign))]
    for _ in range(len(assigns) + 1):  # fixed point, bounded by #assignments
        changed = False
        for a in assigns:
            value = a.value
            if value is None:
                continue
            targets = a.targets if isinstance(a, ast.Assign) else [a.target]
            target_names = {t.id for t in targets if isinstance(t, ast.Name)}
            if not (target_names & reachable_names):
                continue
            for sub in ast.walk(value):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    if id(sub) not in protected_ids:
                        protected_ids.add(id(sub))
                        changed = True
                elif isinstance(sub, ast.Name) and sub.id not in reachable_names:
                    reachable_names.add(sub.id)
                    changed = True
        if not changed:
            break
    return protected_ids


def _contains_bare_placeholder(sql_text: str) -> bool:
    """Does *sql_text* contain a ``?`` outside a quoted string literal or a
    ``--``/``/* */`` SQL comment?

    Delegates to :func:`_scan_placeholders` (#3083), which is the same
    quote- and comment-aware scan this used to carry inline — kept as one
    implementation so the ratchet and the seam-level placeholder-count
    assertions can never disagree about what counts as a placeholder.
    Comment-awareness is load-bearing: coord/db.py's schema DDL has a
    rhetorical "?" inside a ``--`` comment (``"which cursor?"``) that is not
    a placeholder and must not trip this check.
    """
    return _qmark_placeholder_count(sql_text) > 0


def test_no_bare_placeholder_in_sql_literal_outside_the_dialect_seam():
    """No SQL string literal in ``coord/**`` outside the seam carries a bare
    ``?`` placeholder — the *symptom* half of the ratchet.

    The call-site test above catches a raw ``conn.execute("... ?", ...)``.
    This one is the backstop for what it can't see: SQL text built as a
    module-level (or local) constant and forwarded to a driver call by a
    helper the AST walk doesn't recognize as ``execute`` (e.g. one that
    takes a cursor and a string and calls ``cursor.execute(sql, params)``
    several frames from where the string was written). A literal is exempt
    only if it is provably an argument — directly, or through the kind of
    one-hop local assignment coord/db.py's own schema constant uses — to a
    ``coord.sql`` seam call; anything else that is SQL-shaped and carries a
    bare ``?`` is exactly the "raw placeholder reaches a driver" failure
    #1948 names.

    Deliberately introducing e.g. ``_RAW_SQL = "SELECT * FROM t WHERE id =
    ?"`` in any ``coord/`` module, unrouted to ``coord.sql``, makes this
    red.
    """
    violations = []
    for rel, path in _tree_modules():
        _src, tree = _parse(path)
        protected_ids = _reachable_seam_literal_ids(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if id(node) in protected_ids:
                continue
            text = node.value
            if not _SQL_LIKE_RE.match(text):
                continue
            if not _contains_bare_placeholder(text):
                continue
            lineno = node.lineno
            snippet = text.strip()
            for i, line in enumerate(text.split("\n")):
                if "?" in line:
                    lineno = node.lineno + i
                    snippet = line.strip()
                    break
            violations.append(f"{rel}:{lineno}: {snippet}")
    assert not violations, (
        "bare `?` placeholder in a SQL string literal outside the coord.sql "
        "dialect seam (#2768/#1948) — route this SQL through "
        "coord.sql.execute()/executemany()/upsert()/etc. instead of a raw "
        "driver call:\n" + "\n".join(violations)
    )


# ── the ratchet, extended: no driver-named exception outside the seam (#2784) ─
#
# #2768's two ratchets above cover *statements* — a raw `.execute()` call and
# a bare `?` placeholder. #2784 found the seam's other half wide open:
# *exceptions*. 18 call sites across coord/db.py, coord/state.py,
# coord/audit.py, coord/auto_loop.py and coord/commands/merge.py caught
# `sqlite3.OperationalError` directly — the #2597/#2689 whole
# retry/graceful-degradation layer, silently inert on Postgres because a
# psycopg exception is never an instance of a sqlite3 one. This is that
# ratchet's third leg: no `except sqlite3.<X>` and no `raise sqlite3.<X>(...)`
# anywhere in coord/** outside coord/sql.py. Bare `sqlite3.<X>` usage that
# ISN'T naming an exception type — `sqlite3.connect(...)`, `sqlite3.Connection`
# type hints, `sqlite3.Row` — is unaffected: those aren't dialect bugs (the
# connection/type IS sqlite3-specific by construction at today's one call
# site, coord/db.py's own `_open()`), only a driver-named except/raise is.


def _sqlite_attr_refs(node: ast.AST) -> list[ast.Attribute]:
    """Every ``sqlite3.<X>`` attribute-access node under *node*."""
    return [
        sub
        for sub in ast.walk(node)
        if isinstance(sub, ast.Attribute)
        and isinstance(sub.value, ast.Name)
        and sub.value.id == "sqlite3"
    ]


def _sqlite_named_exception_refs(tree: ast.AST) -> list[ast.Attribute]:
    """``sqlite3.<X>`` references used to name an exception TYPE in an
    ``except``/``raise`` — not just any ``sqlite3.*`` usage in the module
    (``sqlite3.connect()``, ``sqlite3.Connection`` type hints, and
    ``sqlite3.Row`` are all fine and common outside the seam; only naming a
    sqlite3 exception class in an except/raise is the dialect-specific bug
    #2784 fixes).

    Covers ``except sqlite3.OperationalError:``, ``except (sqlite3.Error,
    ValueError):`` (walking into the tuple), and
    ``raise sqlite3.OperationalError(...)`` / bare ``raise
    sqlite3.OperationalError``.
    """
    refs: list[ast.Attribute] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is not None:
            refs.extend(_sqlite_attr_refs(node.type))
        elif isinstance(node, ast.Raise) and node.exc is not None:
            target = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            refs.extend(_sqlite_attr_refs(target))
    return refs


def test_no_sqlite_named_exception_outside_the_dialect_seam():
    """No ``except sqlite3.<X>:`` / ``raise sqlite3.<X>(...)`` anywhere in
    ``coord/**`` outside ``coord/sql.py`` — every driver-named exception
    catch/raise must go through ``sql.driver_errors()`` (or a coord-owned
    exception type) instead (#2784).

    Deliberately introducing e.g. ``except sqlite3.OperationalError:`` in
    any ``coord/`` module outside the seam makes this red — the same class
    of regression the #2768 ratchets above catch for raw ``.execute()``
    calls and bare ``?`` placeholders, extended to exception types.
    """
    violations = []
    for rel, path in _tree_modules():
        src, tree = _parse(path)
        lines = src.splitlines()
        for node in _sqlite_named_exception_refs(tree):
            lineno = node.lineno
            src_line = (
                lines[lineno - 1].strip() if 0 < lineno <= len(lines) else "<no source line>"
            )
            violations.append(f"{rel}:{lineno}: {src_line}")
    assert not violations, (
        "sqlite3-named exception except/raise outside the coord.sql dialect "
        "seam (#2784/#2768/#1948) — this goes silently inert on Postgres "
        "(a psycopg exception is never an instance of a sqlite3 one). Widen "
        "the except to `sql.driver_errors()`, or raise a coord-owned "
        "exception type, instead:\n" + "\n".join(violations)
    )


# ── the ratchet, extended: no SQLite-only construct in statement text
# ── outside coord/db.py and the seam itself (#2782) ─────────────────────────
#
# #1948's last unmet acceptance box: "coord/db.py is the only module naming a
# SQLite-specific DDL construct." Phase C's own seam (coord/sql.py) legitimately
# names these too -- that's the whole point of a seam -- so the box becomes
# "no module outside coord/db.py *and* coord/sql.py". #2782 closes the three
# residual sites (housekeeping.py's PRAGMA table_info, serve_app.py's PRAGMA
# journal_mode / PRAGMA wal_checkpoint) by routing them through named
# coord.sql helpers (table_columns / sqlite_journal_mode /
# sqlite_wal_checkpoint_truncate) instead of leaving the literal PRAGMA text
# at the call site.

_SQLITE_ONLY_MARKERS = ("PRAGMA", "AUTOINCREMENT", "INSERT OR IGNORE")
_DB_MODULE_RELPATH = "coord/db.py"


def test_no_sqlite_only_construct_in_statement_text_outside_db_and_seam():
    """No ``coord/**`` module besides ``coord/db.py`` and ``coord/sql.py``
    names a SQLite-only construct (``PRAGMA``, ``AUTOINCREMENT``,
    ``INSERT OR IGNORE``) in its own statement text.

    ``PRAGMA``/``AUTOINCREMENT`` have no Postgres equivalent at all;
    ``INSERT OR IGNORE`` has a portable substitute the seam already provides
    (:func:`coord.sql.insert_ignore`). Any of the three appearing as
    statement text outside ``coord/db.py``'s schema DDL or ``coord/sql.py``'s
    seam helpers is exactly the box #1948 left open and #2782 closes.

    Only string literals that are themselves SQL/PRAGMA statement text (the
    same ``_SQL_LIKE_RE`` gate the ratchets above use) count — prose in a
    docstring or comment that merely *mentions* ``PRAGMA`` is not a
    violation, only text that *is* one.

    Deliberately reintroducing e.g. ``conn.execute("PRAGMA journal_mode")``
    in ``coord/serve_app.py``, or any other non-db.py/sql.py module, makes
    this red.
    """
    violations = []
    for rel, path in _tree_modules():
        if rel == _DB_MODULE_RELPATH:
            continue
        _src, tree = _parse(path)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            text = node.value
            if not _SQL_LIKE_RE.match(text):
                continue
            if not any(marker in text for marker in _SQLITE_ONLY_MARKERS):
                continue
            violations.append(f"{rel}:{node.lineno}: {text.strip()}")
    assert not violations, (
        "SQLite-only construct (PRAGMA/AUTOINCREMENT/INSERT OR IGNORE) in "
        "statement text outside coord/db.py and the coord.sql dialect seam "
        "(#2782/#1948) — route this through a coord.sql seam helper "
        "instead:\n" + "\n".join(violations)
    )


# ── the ratchet, extended: no unrouted named `:param` placeholder (#3083) ────
#
# The fifth leg. #2768's third leg (`_contains_bare_placeholder` above)
# catches a raw `?` in an unrouted SQL literal — and that is *all* it
# catches, because it was written when `?` was the only paramstyle anyone
# in this tree used. sqlite3 supports a second one, `named` (`:name`, bound
# from a dict), and statements written in it sailed straight past the
# ratchet and straight into Postgres as `syntax error at or near ":"` (34
# failures across three test files). Closing the `:` class without a
# ratchet leg would just leave it to reopen the way the `?` class did.
#
# Two things it deliberately does NOT flag, both of which the tree contains
# today: a `:name` inside a literal that IS routed through the seam (that is
# the seam doing its job — `translate()` rewrites it to `%(name)s`), and a
# Sphinx role (`:func:`, `:class:`) in a docstring that happens to begin
# with a SQL keyword. Docstrings are excluded by AST position rather than by
# pattern, and `sql._NAMED_PARAM_RE`'s trailing guard rejects a `:role:`
# anyway.


def _docstring_constant_ids(tree: ast.AST) -> set[int]:
    """``id()`` of every ``ast.Constant`` that is a module/class/function
    docstring — prose, never statement text."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ids.add(id(first.value))
    return ids


def test_no_unrouted_named_placeholder_in_sql_literal_outside_the_dialect_seam():
    """No SQL string literal in ``coord/**`` outside the seam carries a
    ``:name`` placeholder unless it is provably routed through
    ``coord.sql`` (#3083) — the ``?`` leg's named-paramstyle sibling.

    ``?`` and ``:name`` are two spellings of the same mistake against
    Postgres: neither is a placeholder there. The ``?`` half has been
    ratcheted since #2768; this closes the half that was open, so a
    statement cannot re-enter the tree in the paramstyle nobody was
    watching.

    Deliberately introducing e.g. ``_RAW_SQL = "SELECT * FROM t WHERE id =
    :id"`` in any ``coord/`` module, unrouted to ``coord.sql``, makes this
    red.
    """
    violations = []
    for rel, path in _tree_modules():
        _src, tree = _parse(path)
        protected_ids = _reachable_seam_literal_ids(tree)
        docstring_ids = _docstring_constant_ids(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if id(node) in protected_ids or id(node) in docstring_ids:
                continue
            text = node.value
            if not _SQL_LIKE_RE.match(text):
                continue
            names = _named_placeholders(text)
            if not names:
                continue
            violations.append(f"{rel}:{node.lineno}: {', '.join(':' + n for n in names)}")
    assert not violations, (
        "named `:param` placeholder(s) in a SQL string literal outside the "
        "coord.sql dialect seam (#3083/#2768/#1948) — `:name` is no more a "
        "Postgres placeholder than `?` is (`syntax error at or near \":\"`). "
        "Route this SQL through coord.sql.execute()/executemany() so "
        "translate() can rewrite it to `%(name)s`:\n" + "\n".join(violations)
    )


# ── the ratchet, extended: no bare two-argument `MAX(` outside the seam ─────
#
# The sixth leg (#3083). `MAX(a, b)` is SQLite's *scalar* max; Postgres has
# only the aggregate and spells the scalar `GREATEST`, failing with
# `function max(integer, smallint) does not exist` — an error that names an
# argument-type mismatch and so reads as a casting problem rather than a
# spelling one. It is invisible on SQLite by construction, which is exactly
# the shape of bug a ratchet is for. The one-argument aggregate is identical
# on both backends and is not flagged; `coord/sql.py` itself is exempt,
# because `greatest()` is where the SQLite spelling is supposed to live.


def test_no_two_argument_max_in_statement_text_outside_the_dialect_seam():
    """No ``coord/**`` module outside ``coord/sql.py`` names a
    two-argument ``MAX(a, b)`` in its own statement text — that is the
    SQLite-only scalar, and it must go through
    :func:`coord.sql.greatest` (#3083).

    Deliberately reintroducing e.g. ``"UPDATE t SET n = MAX(n, ?)"`` in
    ``coord/portal_store.py`` makes this red.
    """
    violations = []
    for rel, path in _tree_modules():
        for site in _two_argument_max_sites(path):
            violations.append(f"{rel}: {site}")
    assert not violations, (
        "two-argument `MAX(a, b)` in statement text outside the coord.sql "
        "dialect seam (#3083) — that is SQLite's scalar max; Postgres "
        "spells it GREATEST and rejects this with `function max(integer, "
        "smallint) does not exist`. Route it through coord.sql.greatest() "
        "instead:\n" + "\n".join(violations)
    )
