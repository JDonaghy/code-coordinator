"""Test-suite backend selection: point the whole suite at a second database (#2884).

Why this exists
---------------
``tests/conftest.py``'s ``coord_db`` fixture is **autouse**, so all 393 test
files get an isolated database for free and every one of them routes through
``coord.db.override_connection()``.  That single chokepoint is the reason
"run the suite against Postgres" is a small change rather than a 393-file
rewrite: this module is the one place that decides *what kind* of connection
that fixture hands over.

``coord/sql.py`` (the #2719/#2724/#2782/#2784 dialect seam) already makes
``coord/db.py``'s schema DDL, paramstyle, row factory and driver-error
handling backend-neutral, and ``_ensure_schema()`` infers its dialect from
the connection object itself.  So the harness's job is narrow: open a
connection on the selected backend, give it a private namespace, and let
``_ensure_schema()`` do the rest.

Selection
---------
``COORD_TEST_BACKEND`` — ``sqlite`` (default) or ``postgres``.

Unset, the suite behaves **exactly** as it did before this module existed:
``sqlite3.connect(":memory:")`` with ``sqlite3.Row``, same runtime, no new
skips, no dependency on anything being installed.  Postgres is strictly
opt-in, so a developer machine with no server and no ``psycopg`` is
unaffected.

``COORD_TEST_POSTGRES_DSN`` — the server to connect to when the backend is
``postgres``.  Defaults to ``postgresql:///coord_test`` (local socket, a
database named ``coord_test``).

Per-test isolation on Postgres: schema-per-test
-----------------------------------------------
SQLite's ``:memory:`` gives isolation for free — each connection *is* its own
database.  Postgres has no equivalent, so one of three strategies has to be
picked (see #2884).  This module implements **schema-per-test**:

    CREATE SCHEMA <unique>;  SET search_path TO <unique>;  _ensure_schema(conn)
    ... test body ...
    DROP SCHEMA <unique> CASCADE

**Why not transaction-per-test + rollback** (the fastest option): ``coord/``
commits constantly — ``_ensure_schema``, ``_migrate_add_columns``,
``_set_schema_version`` and essentially every writer in ``coord/state.py``
call ``conn.commit()`` directly, and ``coord.db.retry_on_locked`` is built
around committing writes.  A test whose code under test commits blows the
enclosing rollback away, so the strategy would silently leak state between
tests in exactly the tests that write the most.  Non-starter for this tree.

**Why not database-per-worker + truncate**: it is the fastest *correct*
option, but truncation only resets rows — a test that alters schema (and
``tests/test_db.py``-adjacent migration tests do) contaminates every later
test on that worker, and the truncate list has to be rederived whenever the
schema grows.  Schema-per-test gets the same isolation guarantee ``:memory:``
gives today, with no list to maintain.

**The cost, stated plainly**: the full ``_ensure_schema()`` DDL runs once per
test rather than once per worker.  That is the trade — correctness and
"identical isolation semantics to today" bought with per-test DDL.  If #829
finds it too slow, the swap is local to :func:`_open_postgres` and nothing
else in the tree has to move.

**xdist**: schema names embed the ``PYTEST_XDIST_WORKER`` id and the pid, so
parallel workers never collide, and teardown drops only the schema it
created.  Nothing here touches a shared/global namespace, so ``-n auto`` is
safe.

Using it from a test
--------------------
Almost no test should: the autouse ``coord_db`` fixture already covers the
default path.  The exception is a test that genuinely needs a **second,
separate** database (asserting cross-connection visibility, handing a DB to a
subprocess, seeding fixture data in a database that is deliberately not the
one under test).  Those should call :func:`scratch_database` instead of
hardcoding ``sqlite3.connect`` so they follow the active backend — see
``tests/test_sqlite_connect_ratchet.py`` for the classification of every
existing site.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterator

BACKEND_SQLITE = "sqlite"
BACKEND_POSTGRES = "postgres"

_BACKENDS = (BACKEND_SQLITE, BACKEND_POSTGRES)

BACKEND_ENV_VAR = "COORD_TEST_BACKEND"
DSN_ENV_VAR = "COORD_TEST_POSTGRES_DSN"

DEFAULT_POSTGRES_DSN = "postgresql:///coord_test"

# Monotonic per-process counter feeding unique Postgres schema names.  Not a
# uuid: a readable ``coord_test_gw3_41207_12`` is far easier to spot in a
# ``\dn`` listing when a run dies mid-test and leaves a schema behind.
_schema_counter = 0


class UnknownTestBackendError(ValueError):
    """Raised when ``COORD_TEST_BACKEND`` names a backend that doesn't exist."""


def active_backend() -> str:
    """The backend this pytest run targets — ``sqlite`` unless opted out.

    Read fresh on every call rather than captured at import: a test that
    exercises the harness itself can ``monkeypatch.setenv`` and see the
    change, and nothing here caches a decision the environment can later
    contradict (the same reasoning as ``coord.platform_paths``'s
    "computed fresh on every call" posture, and #2781's lazy constants).
    """
    value = (os.environ.get(BACKEND_ENV_VAR) or BACKEND_SQLITE).strip().lower()
    if value not in _BACKENDS:
        raise UnknownTestBackendError(
            f"{BACKEND_ENV_VAR}={value!r} is not a known test backend. "
            f"Valid values: {', '.join(_BACKENDS)} (default: {BACKEND_SQLITE})."
        )
    return value


def postgres_dsn() -> str:
    """The DSN used when the active backend is Postgres."""
    return os.environ.get(DSN_ENV_VAR) or DEFAULT_POSTGRES_DSN


def _worker_tag() -> str:
    """A token unique to this pytest process, for naming Postgres schemas.

    ``PYTEST_XDIST_WORKER`` is set by ``pytest-xdist`` (``gw0``, ``gw1``, ...)
    and absent on a serial run; the pid disambiguates two *separate* pytest
    invocations against the same server (a developer running the suite while
    CI runs it against a shared database).
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER") or "main"
    safe = "".join(ch if ch.isalnum() else "_" for ch in worker)
    return f"{safe}_{os.getpid()}"


def _next_schema_name() -> str:
    global _schema_counter
    _schema_counter += 1
    return f"coord_test_{_worker_tag()}_{_schema_counter}"


def _import_psycopg() -> Any:
    """Import ``psycopg`` with an actionable message when it isn't installed.

    ``psycopg`` is an optional dependency — the ``postgres`` extra (#2886) —
    never a base or ``[dev]`` one, so a plain ``pip install -e ".[dev]"``
    does not pull it in.  Selecting the Postgres backend without it is
    therefore a normal, expected state that deserves a real explanation
    rather than a bare ``ModuleNotFoundError`` traceback out of a fixture.
    """
    try:
        import psycopg  # noqa: PLC0415 -- optional dep, see docstring
    except ImportError as exc:  # ModuleNotFoundError is a subclass
        raise ModuleNotFoundError(
            f"{BACKEND_ENV_VAR}={BACKEND_POSTGRES} was requested but the "
            "`psycopg` driver is not installed. It is an optional "
            "dependency (the `postgres` extra, #2886) — install it with "
            "`pip install 'code-coordinator[postgres]'` and point "
            f"{DSN_ENV_VAR} at a reachable server "
            f"(default: {DEFAULT_POSTGRES_DSN})."
        ) from exc
    return psycopg


class BackendSession:
    """One test's private database on the active backend.

    ``conn`` is the connection ``coord.db.override_connection()`` is pointed
    at.  ``close()`` releases it and destroys the private namespace, so a
    Postgres run leaves no schemas behind for the next test to trip over.
    """

    def __init__(self, backend: str, conn: Any, *, teardown: Any = None) -> None:
        self.backend = backend
        self.conn = conn
        self._teardown = teardown
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._teardown is not None:
            self._teardown()


def _open_sqlite() -> BackendSession:
    """The pre-#2884 default path, unchanged.

    Deliberately *not* routed through ``sql.apply_connection_setup`` — that
    would newly turn on ``PRAGMA foreign_keys`` for every test in the suite,
    which is a behaviour change this issue explicitly must not make ("plain
    ``pytest`` behaves exactly as today").  The row factory does go through
    the seam, because ``sql.row_factory_for("sqlite")`` returns
    ``sqlite3.Row`` — literally the same object the old inline assignment
    used.
    """
    from coord import sql

    conn = sqlite3.connect(":memory:")
    sql.apply_row_factory(conn)
    return BackendSession(BACKEND_SQLITE, conn)


def _open_postgres() -> BackendSession:
    """A private Postgres schema for one test — see the module docstring for
    why schema-per-test rather than rollback-per-test or truncate."""
    from coord import sql

    psycopg = _import_psycopg()
    schema = _next_schema_name()
    conn = psycopg.connect(postgres_dsn(), autocommit=False)
    sql.apply_row_factory(conn)
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
        # search_path is a session setting, so every later statement on this
        # connection — including everything coord/ runs through
        # override_connection — resolves unqualified table names into the
        # private schema without any call site knowing it exists.
        cur.execute(f'SET search_path TO "{schema}"')
    conn.commit()

    def _teardown() -> None:
        try:
            conn.rollback()
            with conn.cursor() as cur:
                # Reset search_path first: dropping the schema the session is
                # currently pointed at is legal but leaves the connection
                # resolving into a schema that no longer exists.
                cur.execute("SET search_path TO public")
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            conn.commit()
        finally:
            conn.close()

    return BackendSession(BACKEND_POSTGRES, conn, teardown=_teardown)


def open_named_session(backend: str) -> BackendSession:
    """Open an isolated database on *backend*, ignoring ``COORD_TEST_BACKEND``.

    :func:`open_session` is the autouse fixture's entry point and asks the
    environment which backend the *whole run* targets.  A caller that needs
    **two backends at once** — the #2885 write-path parity harness, which
    replays one workload against each and diffs the result — cannot express
    that through an env var, so this is the by-name form.  Everything else
    (schema-per-test on Postgres, ``:memory:`` on SQLite, teardown) is
    identical; :func:`open_session` is now a thin wrapper over it, so there
    is exactly one place that knows how to open each backend.
    """
    if backend == BACKEND_SQLITE:
        return _open_sqlite()
    if backend == BACKEND_POSTGRES:
        return _open_postgres()
    raise UnknownTestBackendError(
        f"{backend!r} is not a known test backend. "
        f"Valid values: {', '.join(_BACKENDS)}."
    )


def open_session() -> BackendSession:
    """Open one test's isolated database on the active backend.

    The schema is created by ``coord.db._ensure_schema()`` — the caller's job,
    not this module's, so that the fixture stays the *first consumer* of
    whatever schema posture ``coord/db.py`` settles on (#827 item 3) rather
    than growing a second, divergent copy of it here.
    """
    return open_named_session(active_backend())


def preflight() -> None:
    """Validate the backend selection **once**, before collection.

    Called from ``pytest_configure``. Without it, a mis-set
    ``COORD_TEST_BACKEND``, a missing ``psycopg`` or an unreachable server
    fails inside the *autouse* ``coord_db`` fixture — which means one ERROR
    per test, roughly 5000 identical tracebacks, and a run whose actual
    signal (#2884's deliverable is a concrete **failure** list) is buried
    under fixture noise.  #2884's acceptance is explicit that the error list
    must be empty and the failure list must be the output, so the setup
    problems that would flood it get caught here instead, as a single
    ``UsageError``.

    Raising rather than skipping is deliberate: a developer who typed
    ``COORD_TEST_BACKEND=postgres`` asked for a Postgres run, and quietly
    giving them a green SQLite run instead is the worst possible answer.
    """
    import pytest  # noqa: PLC0415 -- only used on the failure path

    try:
        backend = active_backend()
    except UnknownTestBackendError as exc:
        raise pytest.UsageError(str(exc)) from exc

    if backend == BACKEND_SQLITE:
        return

    try:
        psycopg = _import_psycopg()
    except ImportError as exc:
        raise pytest.UsageError(str(exc)) from exc

    dsn = postgres_dsn()
    try:
        psycopg.connect(dsn).close()
    except Exception as exc:  # noqa: BLE001 -- any connect failure is the same advice
        raise pytest.UsageError(
            f"{BACKEND_ENV_VAR}={BACKEND_POSTGRES} was requested but the "
            f"server at {dsn!r} is not reachable: {exc}\n"
            f"Point {DSN_ENV_VAR} at a running Postgres with an existing, "
            "empty database (the harness creates and drops one schema per "
            "test inside it, so the role needs CREATE on that database)."
        ) from exc


def postgres_available() -> str | None:
    """``None`` when a Postgres backend can be opened right now, else *why not*.

    :func:`preflight` answers the same question for the whole run and raises
    ``pytest.UsageError`` — correct there, because the operator explicitly
    asked for Postgres.  A caller that wants the second backend only *if it
    happens to be there* (the #2885 parity harness's ``sqlite`` vs
    ``postgres`` comparison, which must stay skippable on a laptop with no
    server) needs the non-raising form, and it must not be a second copy of
    the import/connect probe — so both go through the same helpers.
    """
    try:
        psycopg = _import_psycopg()
    except ImportError as exc:
        return str(exc)
    dsn = postgres_dsn()
    try:
        psycopg.connect(dsn).close()
    except Exception as exc:  # noqa: BLE001 -- any connect failure is the same answer
        return f"cannot connect to {dsn!r}: {exc}"
    return None


@contextlib.contextmanager
def scratch_database(tmp_path: Path, name: str = "scratch.db") -> Iterator[Any]:
    """A **second**, separate, schema'd database on the active backend.

    For the bucket-C tests in ``tests/test_sqlite_connect_ratchet.py``: those
    that genuinely need a database *other* than the one the ``coord_db``
    fixture installed — cross-connection visibility checks, a DB handed to a
    subprocess, deliberately-separate fixture data.  Using this instead of a
    hardcoded ``sqlite3.connect`` is what lets those tests follow
    ``COORD_TEST_BACKEND`` too.

    On SQLite this is a file under *tmp_path* (not ``:memory:`` — a second
    ``:memory:`` connection is a different, empty database, which is the
    whole reason these sites hardcode a path today).  On Postgres it is
    another private schema on the same server; *tmp_path* and *name* are
    ignored there, and a test that needs a real filesystem path for a
    subprocess is inherently SQLite-specific and belongs in bucket A.
    """
    from coord import sql
    from coord.db import _ensure_schema

    backend = active_backend()
    if backend == BACKEND_SQLITE:
        conn = sqlite3.connect(str(tmp_path / name))
        sql.apply_row_factory(conn)
        _ensure_schema(conn)
        try:
            yield conn
        finally:
            conn.close()
        return

    session = _open_postgres()
    _ensure_schema(session.conn)
    try:
        yield session.conn
    finally:
        session.close()
