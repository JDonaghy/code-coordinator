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

import sqlite3

import pytest

from coord import sql


# ── fake Postgres-shaped connection (no real psycopg needed) ────────────────


class _FakeCursor:
    """Spies on what would reach a real DB-API cursor."""

    def __init__(self, fetchone_result=None):
        self.executed: list[tuple[str, object]] = []
        self.executemany_calls: list[tuple[str, object]] = []
        self._fetchone_result = fetchone_result

    def execute(self, sql_text, params=()):
        self.executed.append((sql_text, params))

    def executemany(self, sql_text, seq_of_params):
        self.executemany_calls.append((sql_text, list(seq_of_params)))

    def fetchone(self):
        return self._fetchone_result


class _FakeConnection:
    """A connection object shaped like a real DB-API one, without importing
    a real driver.

    ``detect_dialect`` keys off ``type(conn).__module__`` — subclasses below
    set that to a real driver module name so they're indistinguishable from
    the real thing for dialect-detection *and* SQL-dispatch purposes,
    without requiring the dependency to be installed.
    """

    def __init__(self, fetchone_result=None):
        self.cur = _FakeCursor(fetchone_result=fetchone_result)

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
