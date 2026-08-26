"""Tests for ``coord.sql`` — the paramstyle + upsert dialect seam (#2719).

This module is a pure addition: nothing in the tree calls ``coord.sql`` yet
(that's Phase C slices 2-7 of #1948), so these tests exercise it directly
rather than through any existing call site.

Real ``psycopg``/``psycopg2`` are not installed (this repo has no Postgres
dependency yet — see ``pyproject.toml``), so the Postgres path is exercised
two ways: (1) dialect detection and SQL/param translation, which need only a
*fake* connection class whose ``__module__`` claims to be ``psycopg``, spied
to capture exactly what would have reached a real driver; and (2)
``row_factory_for``, which asserts the honest failure mode (``ImportError``)
when the optional dependency genuinely isn't there.
"""

from __future__ import annotations

import ast
import re
import sqlite3
from pathlib import Path

import pytest

from coord import sql


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
    pass


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


# ── row factory ──────────────────────────────────────────────────────────


def test_row_factory_for_sqlite_is_sqlite_row():
    assert sql.row_factory_for(sql.DIALECT_SQLITE) is sqlite3.Row


def test_row_factory_for_postgres_without_psycopg_raises_import_error():
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


def test_apply_connection_setup_postgres_read_only_is_still_a_noop():
    conn = _FakePostgresConnection()
    sql.apply_connection_setup(conn, read_only=True)  # must not raise
    assert conn.cur.executed == []


# ── driver_error (#2766) ─────────────────────────────────────────────────────


def test_driver_error_sqlite_is_sqlite_error(memdb):
    assert sql.driver_error(memdb) is sqlite3.Error


def test_driver_error_catches_a_real_sqlite_operational_error(memdb):
    with pytest.raises(sql.driver_error(memdb)):
        memdb.execute("SELECT * FROM no_such_table")


def test_driver_error_postgres_without_psycopg_raises_import_error():
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
# going silently inert under Postgres. psycopg is not installed in this repo
# (see the module docstring), so degrading to (sqlite3.Error,) rather than
# raising ImportError is the case actually under test here.


def test_driver_errors_degrades_to_sqlite_only_when_psycopg_absent():
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

    Extends :func:`coord.sql._qmark_to_pyformat`'s quote-tracking with
    comment-awareness — needed because coord/db.py's schema DDL has a
    rhetorical "?" inside a ``--`` comment (``"which cursor?"``) that is not
    a placeholder and must not trip this check.
    """
    in_string: str | None = None
    in_line_comment = False
    in_block_comment = False
    i, n = 0, len(sql_text)
    while i < n:
        ch = sql_text[i]
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            if sql_text[i : i + 2] == "*/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_string is not None:
            if ch == in_string:
                if sql_text[i : i + 2] == in_string * 2:
                    i += 2
                    continue
                in_string = None
            i += 1
            continue
        if ch in ("'", '"'):
            in_string = ch
            i += 1
            continue
        if sql_text[i : i + 2] == "--":
            in_line_comment = True
            i += 2
            continue
        if sql_text[i : i + 2] == "/*":
            in_block_comment = True
            i += 2
            continue
        if ch == "?":
            return True
        i += 1
    return False


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
