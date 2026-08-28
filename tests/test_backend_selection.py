"""#2884: the test suite's own backend switch, tested.

``tests/backends.py`` is the one function standing between "the suite runs on
SQLite" and "the suite runs on Postgres".  Its two load-bearing properties are
worth pinning explicitly, because both fail *silently* if they regress:

* **plain ``pytest`` must behave exactly as it did before #2884** — same
  driver, same isolation, no new skips, no dependency on anything being
  installed.  A regression here is invisible (the suite still passes) right up
  until someone notices tests are sharing state or the default changed.
* **``COORD_TEST_BACKEND=postgres`` must fail loudly and usefully** when
  ``psycopg`` isn't installed.  It isn't a declared dependency yet (#2886), so
  "selected but unavailable" is a normal state a developer will hit, and the
  difference between a bare ``ModuleNotFoundError`` out of a fixture and a
  message naming the install command is the whole user experience of this
  feature.
"""

from __future__ import annotations

import sqlite3
import sys

import pytest

from tests import backends


# ── selection ────────────────────────────────────────────────────────────────

def test_default_backend_is_sqlite_with_no_env_set(monkeypatch):
    """The acceptance bar: unset env → SQLite, exactly as before #2884."""
    monkeypatch.delenv(backends.BACKEND_ENV_VAR, raising=False)
    assert backends.active_backend() == backends.BACKEND_SQLITE


@pytest.mark.parametrize("value", ["postgres", "POSTGRES", "  Postgres  "])
def test_backend_env_var_is_case_and_whitespace_tolerant(monkeypatch, value):
    """``COORD_TEST_BACKEND`` comes from a shell/CI env, where stray case and
    whitespace are routine — normalise rather than silently falling back to
    SQLite and pretending the Postgres run happened."""
    monkeypatch.setenv(backends.BACKEND_ENV_VAR, value)
    assert backends.active_backend() == backends.BACKEND_POSTGRES


def test_empty_backend_env_var_falls_back_to_sqlite(monkeypatch):
    """``COORD_TEST_BACKEND=`` (exported but empty, a very common CI shape)
    must mean 'default', not 'unknown backend'."""
    monkeypatch.setenv(backends.BACKEND_ENV_VAR, "")
    assert backends.active_backend() == backends.BACKEND_SQLITE


def test_unknown_backend_names_the_valid_values(monkeypatch):
    """A typo must not silently degrade to SQLite — that would report a green
    'Postgres run' that never touched Postgres."""
    monkeypatch.setenv(backends.BACKEND_ENV_VAR, "postgress")
    with pytest.raises(backends.UnknownTestBackendError) as exc:
        backends.active_backend()
    message = str(exc.value)
    assert "postgress" in message
    assert backends.BACKEND_SQLITE in message and backends.BACKEND_POSTGRES in message


def test_postgres_dsn_defaults_and_is_overridable(monkeypatch):
    monkeypatch.delenv(backends.DSN_ENV_VAR, raising=False)
    assert backends.postgres_dsn() == backends.DEFAULT_POSTGRES_DSN
    monkeypatch.setenv(backends.DSN_ENV_VAR, "postgresql://u@db.example/coord")
    assert backends.postgres_dsn() == "postgresql://u@db.example/coord"


# ── the SQLite default path ──────────────────────────────────────────────────

def test_open_session_default_is_an_isolated_sqlite_memory_db(monkeypatch):
    """Byte-for-byte the pre-#2884 fixture behaviour: an in-memory SQLite
    connection with a ``sqlite3.Row`` row factory."""
    monkeypatch.delenv(backends.BACKEND_ENV_VAR, raising=False)
    session = backends.open_session()
    try:
        assert session.backend == backends.BACKEND_SQLITE
        assert isinstance(session.conn, sqlite3.Connection)
        assert session.conn.row_factory is sqlite3.Row
    finally:
        session.close()


def test_two_sessions_do_not_share_state(monkeypatch):
    """Per-test isolation, the property the whole suite silently depends on."""
    from coord.db import _ensure_schema

    monkeypatch.delenv(backends.BACKEND_ENV_VAR, raising=False)
    first = backends.open_session()
    second = backends.open_session()
    try:
        for session in (first, second):
            _ensure_schema(session.conn)
        first.conn.execute(
            "INSERT INTO machines (name, host, capabilities, repos) VALUES (?,?,?,?)",
            ("dellserver", "dellserver", "[]", "[]"),
        )
        first.conn.commit()
        assert first.conn.execute("SELECT COUNT(*) FROM machines").fetchone()[0] == 1
        assert second.conn.execute("SELECT COUNT(*) FROM machines").fetchone()[0] == 0
    finally:
        first.close()
        second.close()


def test_session_close_is_idempotent(monkeypatch):
    """The ``coord_db`` fixture calls ``session.close()`` and then
    ``db.close()``, and a test may itself have called ``db.close()`` first —
    so double-close must not raise out of teardown and mask the real result."""
    monkeypatch.delenv(backends.BACKEND_ENV_VAR, raising=False)
    session = backends.open_session()
    session.close()
    session.close()


# ── the autouse fixture, end to end ──────────────────────────────────────────

def test_coord_db_fixture_gives_a_schemad_db_wired_into_coord_db(coord_db):
    """The chokepoint itself: the autouse fixture's connection is fully
    migrated AND is the connection ``coord.db.get_connection()`` hands to
    production code — that identity is why parametrising this one fixture
    repoints all 393 test files."""
    from coord import db

    assert db.get_connection() is coord_db
    # Fully schema'd, not just "a connection": a real table and the version row.
    coord_db.execute("SELECT COUNT(*) FROM assignments").fetchone()
    version = coord_db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == db._DB_SCHEMA_VERSION


def test_coord_db_fixture_is_clean_for_every_test(coord_db):
    """Companion to the test below — together they prove the fixture does not
    leak rows between tests (whichever order pytest runs them in)."""
    assert coord_db.execute("SELECT COUNT(*) FROM machines").fetchone()[0] == 0
    coord_db.execute(
        "INSERT INTO machines (name, host, capabilities, repos) VALUES (?,?,?,?)",
        ("precision", "precision", "[]", "[]"),
    )
    coord_db.commit()


def test_coord_db_fixture_is_clean_for_every_test_too(coord_db):
    assert coord_db.execute("SELECT COUNT(*) FROM machines").fetchone()[0] == 0
    coord_db.execute(
        "INSERT INTO machines (name, host, capabilities, repos) VALUES (?,?,?,?)",
        ("precision", "precision", "[]", "[]"),
    )
    coord_db.commit()


# ── the second-connection helper ─────────────────────────────────────────────

def test_scratch_database_is_a_separate_schemad_db(coord_db, tmp_path, monkeypatch):
    """Bucket C's replacement for a hardcoded ``sqlite3.connect``: a real,
    separate, already-migrated database that follows the active backend."""
    monkeypatch.delenv(backends.BACKEND_ENV_VAR, raising=False)
    with backends.scratch_database(tmp_path) as scratch:
        assert scratch is not coord_db
        scratch.execute(
            "INSERT INTO machines (name, host, capabilities, repos) VALUES (?,?,?,?)",
            ("m1", "m1", "[]", "[]"),
        )
        scratch.commit()
        assert scratch.execute("SELECT COUNT(*) FROM machines").fetchone()[0] == 1
        # ...and the DB under test is untouched.
        assert coord_db.execute("SELECT COUNT(*) FROM machines").fetchone()[0] == 0


def test_scratch_database_on_sqlite_is_a_real_file(tmp_path, monkeypatch):
    """Not ``:memory:`` — the point of bucket C is that a *second* connection
    (or a subprocess, or ``SqliteStore(db_path)``) can open the same database,
    which a second ``:memory:`` connection can never do."""
    monkeypatch.delenv(backends.BACKEND_ENV_VAR, raising=False)
    with backends.scratch_database(tmp_path, "board.db"):
        pass
    assert (tmp_path / "board.db").exists()


# ── the Postgres arm, without a Postgres ─────────────────────────────────────

def test_selecting_postgres_without_psycopg_explains_itself(monkeypatch):
    """#2886 hasn't landed, so ``psycopg`` is not installed — the expected
    developer experience of ``COORD_TEST_BACKEND=postgres pytest`` today is a
    message that says what to install and where to point it, not a bare
    import traceback out of an autouse fixture.

    ``sys.modules["psycopg"] = None`` makes ``import psycopg`` raise
    ``ImportError`` deterministically, so this test asserts the same behaviour
    whether or not the driver is installed on the machine running it.
    """
    monkeypatch.setitem(sys.modules, "psycopg", None)
    with pytest.raises(ImportError) as exc:
        backends._import_psycopg()
    message = str(exc.value)
    assert "psycopg" in message
    assert "#2886" in message, "should point at the issue that adds the dependency"
    assert "pip install" in message, "should say how to fix it"
    assert backends.DSN_ENV_VAR in message, "should say where to point it"


def test_open_session_postgres_surfaces_the_missing_driver(monkeypatch):
    """The same message reaches the caller through ``open_session()`` — i.e.
    through the path the autouse fixture actually takes."""
    monkeypatch.setenv(backends.BACKEND_ENV_VAR, "postgres")
    monkeypatch.setitem(sys.modules, "psycopg", None)
    with pytest.raises(ImportError, match="psycopg"):
        backends.open_session()


def test_preflight_is_a_noop_on_the_default_backend(monkeypatch):
    """``pytest_configure`` calls this on every single run — on SQLite it must
    not import a driver or open a socket, or the default path stops being
    'exactly as today'."""
    monkeypatch.delenv(backends.BACKEND_ENV_VAR, raising=False)
    monkeypatch.setitem(sys.modules, "psycopg", None)  # would raise if imported
    assert backends.preflight() is None


def test_preflight_turns_a_bad_selection_into_one_usage_error(monkeypatch):
    """One ``UsageError`` before collection, not ~5000 identical autouse-fixture
    ERRORs — #2884's deliverable is a *failure* list, which fixture noise on
    that scale would bury."""
    monkeypatch.setenv(backends.BACKEND_ENV_VAR, "postgress")
    with pytest.raises(pytest.UsageError, match="not a known test backend"):
        backends.preflight()

    monkeypatch.setenv(backends.BACKEND_ENV_VAR, "postgres")
    monkeypatch.setitem(sys.modules, "psycopg", None)
    with pytest.raises(pytest.UsageError, match="psycopg"):
        backends.preflight()


# ── the Postgres isolation strategy (driver-free) ────────────────────────────

class _FakeCursor:
    def __init__(self, log):
        self._log = log

    def execute(self, sql_text, *args):
        self._log.append(sql_text)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConnection:
    """Enough of psycopg3's Connection for :func:`backends._open_postgres`.

    ``__module__`` is forced to ``"psycopg"`` because ``coord.sql`` detects the
    dialect from ``type(conn).__module__`` (#2719) — keyed off the driver, never
    a flag — so a stand-in has to claim the driver's identity to be routed down
    the Postgres arm.
    """

    __module__ = "psycopg"

    def __init__(self, log):
        self.log = log
        self.row_factory = None
        self.closed = False
        self.commits = 0

    def cursor(self):
        return _FakeCursor(self.log)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.log.append("ROLLBACK")

    def close(self):
        self.closed = True


@pytest.fixture
def fake_psycopg(monkeypatch):
    """Install a stand-in ``psycopg`` so the schema-per-test DDL can be
    asserted on a machine with no driver and no server (which is every machine
    until #2886)."""
    import types

    log: list[str] = []
    connections: list[_FakeConnection] = []

    rows = types.ModuleType("psycopg.rows")
    rows.dict_row = object()

    module = types.ModuleType("psycopg")
    module.rows = rows

    def _connect(dsn, **kwargs):
        log.append(f"CONNECT {dsn} {sorted(kwargs.items())}")
        conn = _FakeConnection(log)
        connections.append(conn)
        return conn

    module.connect = _connect
    monkeypatch.setitem(sys.modules, "psycopg", module)
    monkeypatch.setitem(sys.modules, "psycopg.rows", rows)
    monkeypatch.setenv(backends.BACKEND_ENV_VAR, "postgres")
    return types.SimpleNamespace(log=log, connections=connections, rows=rows)


def test_postgres_session_creates_and_enters_a_private_schema(fake_psycopg, monkeypatch):
    """The chosen isolation strategy, pinned: CREATE SCHEMA + SET search_path,
    committed, before ``_ensure_schema`` ever runs.

    ``search_path`` is what makes this transparent — every unqualified table
    name in ``coord/``'s ~300 statements resolves into the private schema
    without a single call site knowing it exists.
    """
    monkeypatch.setenv(backends.DSN_ENV_VAR, "postgresql:///coord_test_fixture")
    session = backends.open_session()

    assert session.backend == backends.BACKEND_POSTGRES
    assert session.conn.row_factory is fake_psycopg.rows.dict_row
    assert "CONNECT postgresql:///coord_test_fixture [('autocommit', False)]" in fake_psycopg.log

    create = [s for s in fake_psycopg.log if s.startswith("CREATE SCHEMA")]
    search = [s for s in fake_psycopg.log if s.startswith("SET search_path")]
    assert len(create) == 1 and len(search) == 1
    schema = create[0].split('"')[1]
    assert search[0] == f'SET search_path TO "{schema}"'
    assert session.conn.commits == 1, "the schema must be committed before the test body"

    session.close()


def test_postgres_session_drops_its_schema_on_teardown(fake_psycopg):
    """Schema-per-test only holds if teardown actually reclaims the schema —
    otherwise a long run leaves thousands behind and the next one collides."""
    session = backends.open_session()
    schema = [s for s in fake_psycopg.log if s.startswith("CREATE SCHEMA")][0].split('"')[1]
    session.close()

    assert f'DROP SCHEMA IF EXISTS "{schema}" CASCADE' in fake_psycopg.log
    # search_path is reset first: dropping the schema the session is pointed
    # at is legal but leaves the connection resolving into nothing.
    drop_at = fake_psycopg.log.index(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    assert fake_psycopg.log.index("SET search_path TO public") < drop_at
    assert session.conn.closed


def test_postgres_session_closes_the_connection_even_if_the_drop_fails(fake_psycopg):
    """A failed DROP must not leak the connection — a run that leaks one per
    test exhausts ``max_connections`` long before the suite finishes."""
    session = backends.open_session()

    def _boom(*_args, **_kwargs):
        raise RuntimeError("server went away")

    session.conn.cursor = _boom
    with pytest.raises(RuntimeError):
        session.close()
    assert session.conn.closed


def test_each_postgres_session_gets_its_own_schema(fake_psycopg):
    """Two sessions must never share a namespace — that is the whole isolation
    guarantee ``:memory:`` gives for free on SQLite."""
    first = backends.open_session()
    second = backends.open_session()
    try:
        created = [s for s in fake_psycopg.log if s.startswith("CREATE SCHEMA")]
        assert len(created) == 2
        assert created[0] != created[1]
    finally:
        first.close()
        second.close()


# ── xdist safety of the Postgres isolation strategy ──────────────────────────

def test_schema_names_are_unique_per_call():
    """Schema-per-test only isolates if the names never repeat within a
    process — otherwise two tests share a namespace and the strategy silently
    degrades to no isolation at all."""
    names = {backends._next_schema_name() for _ in range(50)}
    assert len(names) == 50


def test_schema_names_carry_the_xdist_worker_id(monkeypatch):
    """Two ``pytest-xdist`` workers run in separate processes against the same
    server; without the worker id in the name they would collide on the shared
    per-process counter and drop each other's schemas mid-test."""
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw3")
    assert "gw3" in backends._next_schema_name()


def test_schema_names_are_distinct_across_processes(monkeypatch):
    """Even serially (no ``PYTEST_XDIST_WORKER``), two concurrent pytest
    invocations pointed at one shared database must not collide — the pid
    disambiguates them."""
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    tag = backends._worker_tag()
    assert tag.startswith("main_")
    assert str(__import__("os").getpid()) in tag


def test_schema_names_are_safe_sql_identifiers(monkeypatch):
    """The name is interpolated into ``CREATE SCHEMA "..."``. Keep it to
    ``[A-Za-z0-9_]`` so a hostile-looking worker id can't break out of the
    quoting (xdist ids are tame, but this is a string that reaches DDL)."""
    monkeypatch.setenv("PYTEST_XDIST_WORKER", 'gw0"; DROP SCHEMA public; --')
    name = backends._next_schema_name()
    assert all(ch.isalnum() or ch == "_" for ch in name), name
