"""Connection management and schema for coordinator state.

Single database — SQLite at ``~/.coord/coord.db`` with WAL mode by default,
or Postgres when ``coordinator.yml``'s ``store:`` block opts in (#827; see
:class:`coord.config.StoreConfig`). All coordinator state lives here:
assignments, proposals, merge queue, sessions, etc. Every connection this
module opens goes through :func:`coord.sql.connect`, the one dialect-aware
factory (#827) — this module names no driver's ``.connect()`` directly.

Usage
-----
- Production code: ``get_connection()`` returns this process's connection.
- Tests: call ``override_connection(sqlite3.connect(":memory:"))`` then
  ``close()`` in teardown (the ``coord_db`` fixture in conftest.py does this).

Connection-sharing model (#827)
--------------------------------
SQLite (today's default, and the only backend most deployments will ever
configure): one connection for the whole process, opened with
``check_same_thread=False`` — unchanged from before this issue. SQLite (with
WAL mode + the ``busy_timeout`` this module sets via
``sql.apply_connection_setup``) tolerates being handed directly to multiple
threads.

Postgres: a driver connection is not safe to use concurrently from multiple
threads the same way, so instead of one process-wide singleton, each THREAD
gets its own lazily-opened connection, cached in thread-local storage (see
``_pg_thread_local`` below) — ``coord serve``'s worker-thread-per-request
model then never hands one live connection to two threads at once. This is
the "explicit per-thread" option #827 names as an alternative to a pool: no
new dependency (``psycopg_pool`` is a separate package from ``psycopg``
itself), and every existing ``get_connection()`` caller is unaffected by the
choice — they get *a* connection back either way and never inspect its
identity. A true multi-connection pool is deferred until #829's cutover
measures whether the daemon's threading actually needs one under real load;
today this module is "mergeable and inert with no Postgres anywhere in the
deployment" (#827's own framing), so there is no live traffic to measure yet.
``override_connection()`` always wins, on every thread, regardless of
backend — this is what lets ``tests/conftest.py``'s autouse ``coord_db``
fixture keep injecting one in-memory SQLite connection without any test
needing to know this function branches on the configured backend at all.

Neither cache had an invalidation path before #3082: once a connection was
cached, ``get_connection()`` returned it unconditionally forever, so a
Postgres session the server (or a schema-per-test teardown) had already
closed kept being handed back dead, surfacing as ``psycopg.OperationalError:
the connection is closed`` on whatever statement ran next. Both caches now
go through :func:`_connection_is_closed` before being returned — see that
function's docstring — so a closed cached connection is discarded and
reopened instead.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar

from coord import __version__ as _coord_version
from coord import sql
from coord.platform_paths import default_coord_dir

_conn: Any | None = None

# #827: per-thread cache of this process's Postgres connection, when the
# configured backend is postgres — see the module docstring's
# "Connection-sharing model" section. Unused (empty) for the SQLite-only
# fleet this ships into; only ever populated by get_connection() below.
_pg_thread_local = threading.local()


def _connection_is_closed(conn: Any) -> bool:
    """True when *conn* is a driver connection that is already closed (#3082).

    :func:`get_connection` calls this on every cached connection it is about
    to hand back — the ``_conn`` singleton/override slot and the per-thread
    Postgres cache alike — so a connection the server or driver already
    closed out from under this process is never handed back a second time.
    Before #3082 neither cache had any invalidation path at all: once
    populated, both were returned unconditionally until something called
    :func:`close`, so a dropped Postgres session (schema-per-test teardown
    in ``tests/backends.py``, or — the shape this must also cover — a
    production daemon's connection dropped by the server) wedged every
    later ``get_connection()`` call on that thread onto the same dead
    object, surfacing as ``psycopg.OperationalError: the connection is
    closed`` on whatever statement ran next.

    Checked via ``getattr(conn, "closed", False)`` rather than importing
    psycopg — an optional dependency, see ``coord/sql.py``'s module
    docstring for why nothing in this seam imports a driver directly:
    psycopg3 exposes a bool ``.closed``, psycopg2 an int (``0`` open,
    non-zero closed) — either way, truthy means closed.
    ``sqlite3.Connection`` has no ``.closed`` attribute at all, so this
    always reads False for it: the SQLite singleton path (and any test's
    ``sqlite3.connect`` override) is byte-identical to before #3082, exactly
    matching the module docstring's "unchanged from before this issue"
    promise for that backend.
    """
    return bool(getattr(conn, "closed", False))


def __getattr__(name: str) -> Path:
    """PEP 562 lazy fallback for ``COORD_DIR``/``DB_PATH`` (#2781).

    Pre-#2781 these were bound eagerly at import time, so ``$COORD_DIR`` set
    *after* this module was first imported -- e.g. by a pytest fixture --
    never reached them, unlike :func:`default_coord_dir` itself which is
    "computed fresh on every call" by design (see its docstring). Not binding
    them eagerly here means any access -- ``coord.db.COORD_DIR``, or the
    internal ``sys.modules[__name__].COORD_DIR`` lookups below -- re-resolves
    against the current environment.

    This only engages when the name hasn't been bound directly in this
    module's namespace, so ``monkeypatch.setattr(coord.db, "DB_PATH", ...)``
    (used throughout tests/test_db.py) still takes priority exactly as
    before: Python calls ``__getattr__`` only when normal attribute lookup
    fails.
    """
    if name == "COORD_DIR":
        return default_coord_dir()
    if name == "DB_PATH":
        return sys.modules[__name__].COORD_DIR / "coord.db"
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class ProductionDatabaseGuardError(RuntimeError):
    """Raised when unreleased or test code tries to write to the live
    ``~/.coord/coord.db`` (#1960, #2752).

    Two independent triggers share this class because they share a root
    cause -- code that isn't the released binary reaching the one live
    database -- and a caller catching one should catch the other the same
    way:

    1. **Under pytest** (#1960). The autouse ``coord_db`` fixture in
       ``tests/conftest.py`` overrides the module-level singleton with an
       isolated ``:memory:`` connection before every test body runs, so
       ``get_connection()`` normally never reaches ``_open()`` at all during
       a test. This guard exists for the paths that fixture can't reach:
       code that calls ``_open(DB_PATH)`` directly, a subprocess the test
       spawned that re-imports ``coord.db`` fresh (pytest's
       ``PYTEST_CURRENT_TEST`` env var is inherited by child processes by
       default), or a test that closes the override and lets the singleton
       fall back to the real path.

    2. **From a non-release build, for schema writes only** (#2752).
       ``COORD_DIR`` defaults to ``~/.coord/`` regardless of cwd, so a
       worktree checkout, an editable install, or any process running code
       that was never tagged/released can still reach the live database with
       no pytest involved. Reads are unaffected -- an already-caught-up
       database opens exactly as before -- but stamping ``schema_version``
       forward from unreleased code permanently skips any migration the
       *released* code later adds under that same version number (the
       mechanism behind #2675/#2709). See :func:`_is_release_build`.
    """


@dataclass(frozen=True)
class _StoreTarget:
    """Which backend/DSN :func:`get_connection` should open (#827)."""

    backend: str
    dsn: str | None = None


def _resolve_store_target() -> _StoreTarget:
    """Resolve ``coordinator.yml``'s ``store:`` block (#827).

    Two different failure shapes get two deliberately different outcomes
    (#827 review, blocking finding 2 — see that finding for the full
    incident this replaced):

    - **No config resolvable at all**, or a config that exists but fails for
      a reason that has nothing to do with an explicit ``store:`` block
      (bad YAML, a bad ``repos:``/``machines:`` entry, ...) fails OPEN to
      SQLite — a fresh worktree, a bare test invocation, or an agent that
      hasn't been handed a ``coordinator.yml`` yet must never be unable to
      open its local database. This mirrors ``coord.audit._cached_config``'s
      fail-open shape for the same reason.
    - **An explicit ``store:`` block that fails to validate** (a typo'd
      ``backend``, ``backend: postgres`` with no ``dsn``, ...) fails LOUD —
      whatever :func:`coord.config._parse_store` raises propagates
      unchanged. A deployment that intentionally opted into Postgres and
      then typo'd or broke that block must find out immediately; silently
      falling back to an empty local SQLite file with no error is exactly
      the split-brain this config seam exists to prevent — a broken/missing
      config must never be how a caller discovers it's talking to the wrong
      database, in *either* direction.

    Deliberately does NOT run the full :func:`coord.config.load` /
    :func:`coord.config.parse_mapping` validation pipeline — it reads only
    the raw ``store:`` mapping out of the YAML directly, so a config problem
    completely unrelated to storage can never masquerade as, or suppress, a
    storage-backend decision; only ``store:``'s own shape is ever
    load-bearing here.

    Called at most once per process: only from :func:`get_connection`'s
    first-open path (for SQLite) or its first-open-on-this-thread path (for
    Postgres) — never on every call, since ``_conn``/the thread-local cache
    short-circuits every call after that. A YAML read+parse is real I/O, but
    "once per process" (or "once per thread", under Postgres) is not the
    hot-loop cost ``coord.audit``'s own caching comment warns about (~2,900
    writes/hour) — so unlike that module, this intentionally does not cache
    across calls; there is nothing to amortize.
    """
    import yaml  # noqa: PLC0415

    from coord.config import resolve_config_path  # noqa: PLC0415

    try:
        path = resolve_config_path()
        raw = yaml.safe_load(path.read_text()) if path.exists() else None
    except Exception:
        return _StoreTarget(backend=sql.DIALECT_SQLITE)

    if not isinstance(raw, dict) or "store" not in raw:
        return _StoreTarget(backend=sql.DIALECT_SQLITE)

    from coord.config import _parse_store  # noqa: PLC0415

    # A malformed `store:` block raises ConfigError here -- deliberately NOT
    # caught. See the docstring above: this is the one config problem that
    # must fail loud rather than fail open.
    store = _parse_store(raw["store"])
    if store.backend != sql.DIALECT_POSTGRES:
        return _StoreTarget(backend=sql.DIALECT_SQLITE)
    return _StoreTarget(backend=sql.DIALECT_POSTGRES, dsn=store.dsn)


def resolve_store_backend() -> tuple[str, str | None]:
    """Public, DSN-redacting view of which backend :func:`get_connection`
    will open (#3084) — the one accessor every operator-facing surface (the
    ``coord serve`` banner, ``GET /healthz``, ``coord doctor``) goes through
    to answer "which store am I on?" without ever having a chance to print a
    raw DSN. Wraps :func:`_resolve_store_target`, the same resolution
    ``get_connection()`` itself uses, so this can never drift from the
    backend a connection actually opens against.

    Returns ``(backend, redacted_target)``. *redacted_target* is ``None``
    for SQLite -- there is no DSN, and a caller that wants to name the
    on-disk file uses :data:`DB_PATH` itself, same as always -- or
    :func:`coord.sql.redact_dsn`'s host/dbname-only rendering of the
    configured Postgres DSN when the resolved backend is
    :data:`coord.sql.DIALECT_POSTGRES`. There is no code path here that can
    return the raw DSN.

    Like :func:`_resolve_store_target`, a config problem unrelated to an
    explicit ``store:`` block fails open to SQLite; an explicit, broken
    ``store:`` block still raises (see that function's docstring) -- this
    wrapper changes nothing about that contract, it only adds redaction on
    top of the resolved target.
    """
    target = _resolve_store_target()
    if target.backend == sql.DIALECT_POSTGRES:
        return target.backend, sql.redact_dsn(target.dsn or "")
    return target.backend, None


def get_connection() -> Any:
    """Return this process's connection, opening it on first call.

    See the module docstring's "Connection-sharing model" section: SQLite
    returns the process-wide singleton (unchanged); Postgres returns this
    THREAD's lazily-opened connection. ``override_connection()`` always wins
    over either, regardless of which thread calls this.

    #3082: neither cache is trusted blindly — :func:`_connection_is_closed`
    is checked first, and a connection that already closed underneath this
    process is discarded and reopened rather than handed back dead. See that
    function's docstring for the incident this closes.
    """
    global _conn
    if _conn is not None:
        if not _connection_is_closed(_conn):
            return _conn
        _conn = None
    target = _resolve_store_target()
    if target.backend == sql.DIALECT_POSTGRES:
        conn = getattr(_pg_thread_local, "conn", None)
        if conn is not None and _connection_is_closed(conn):
            conn = None
            _pg_thread_local.conn = None
        if conn is None:
            conn = _open_postgres(target.dsn)
            _pg_thread_local.conn = conn
        return conn
    _conn = _open(sys.modules[__name__].DB_PATH)
    return _conn


def _migrate_if_needed(conn: Any, *, is_production: bool, target_desc: str) -> None:
    """Shared by :func:`_open` (SQLite) and :func:`_open_postgres` (#827):
    the schema-version gate, the #2752 non-release-build guard, and — when a
    write is actually needed — the migration functions themselves.

    Every one of those functions (``_ensure_schema`` down to
    ``_backfill_orphaned_review_verdicts``) already routes through
    ``coord.sql``'s dialect seam (#1948/#2724/#2782/#2784), so nothing here
    is SQLite-specific — this is #827 problem 3's "does Postgres get the
    same migration path" answered by construction rather than by comment:
    there is only one migration implementation, used by both backends.

    *target_desc* is a human-readable phrase for the guard's error message
    (e.g. ``"the production coordinator database at /path/to/coord.db"``) —
    deliberately never the raw DSN for a Postgres target, since a DSN can
    carry a password and this exception's message can end up in a log or a
    GitHub comment (agent failure reports post their stdout/stderr).

    #2598: this whole block is gated on a cheap read-only version check — a
    database already at ``_DB_SCHEMA_VERSION`` does none of this work, so a
    read-only command (``coord status``) issues no write at all just to open
    a connection it only ever intended to read from.
    """
    if _read_schema_version(conn) < _DB_SCHEMA_VERSION:
        # #2752: this is the schema *write* path (_ensure_schema and, inside
        # it, _set_schema_version) — the only place a process permanently
        # stamps how caught-up the database is. A non-release build reaching
        # here would advance schema_version to whatever _DB_SCHEMA_VERSION
        # *that branch* declares, and every later open by the actually-
        # released code (same or lower version number) reads
        # `_read_schema_version(conn) < _DB_SCHEMA_VERSION` as False and
        # skips this block forever -- permanently missing any migration the
        # release adds under that version number. Reads are unaffected: an
        # already-caught-up database never reaches this branch at all.
        if is_production and not _is_release_build(_coord_version):
            raise ProductionDatabaseGuardError(
                f"Refusing to write schema changes to {target_desc} from a "
                f"non-release build (coord.__version__={_coord_version!r} is "
                "not a clean X.Y.Z release tag). Stamping schema_version "
                "forward from unreleased code -- a worktree, an editable "
                "install, a branch that bumped _DB_SCHEMA_VERSION -- "
                "permanently skips any migration the released code later "
                "adds under that same version number; see #2752 (and the "
                "incidents it names, #2675/#2709). Fix: point COORD_DIR at a "
                "scratch directory (e.g. `COORD_DIR=/tmp/coord-dev coord "
                "...`) instead of touching the live production database "
                "from unreleased code."
            )
        _ensure_schema(conn)
        _maybe_migrate_json(conn)
        _migrate_gate_order(conn)
        _backfill_orphaned_review_verdicts(conn)


def _open(path: Path) -> sqlite3.Connection:
    db_path = sys.modules[__name__].DB_PATH
    is_production = path == db_path
    if is_production and os.environ.get("PYTEST_CURRENT_TEST"):
        raise ProductionDatabaseGuardError(
            f"Refusing to open the production coordinator database at "
            f"{path} while running under pytest "
            f"(PYTEST_CURRENT_TEST={os.environ['PYTEST_CURRENT_TEST']!r}). "
            "A test (or a subprocess it spawned) resolved the real "
            "~/.coord/coord.db instead of an isolated one -- see #1960. "
            "Fix: rely on the autouse `coord_db` fixture (already active for "
            "every test and overrides this singleton with an in-memory DB), "
            "or pass an explicit isolated path instead of coord.db.DB_PATH."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sql.connect(backend=sql.DIALECT_SQLITE, sqlite_path=path, check_same_thread=False)
    sql.apply_connection_setup(conn)
    sql.apply_row_factory(conn)
    _migrate_if_needed(
        conn,
        is_production=is_production,
        target_desc=f"the production coordinator database at {path}",
    )
    return conn


def refuse_postgres_under_pytest(target_desc: str) -> None:
    """Raise :class:`ProductionDatabaseGuardError` if called during a pytest
    run (#1960's SQLite analogue, widened to Postgres by #827).

    Shared by :func:`_open_postgres` (this module's write-path opener) and
    ``coord.dao.SqliteStore._connect`` (the daemon's read path) — both are
    "the one place a real Postgres connection gets opened" for their
    respective sides, and neither should ever reach a live Postgres server
    from a test: no test's ``coordinator.yml`` should set
    ``store.backend: postgres``, but if one somehow does (a leaked ambient
    config, a copy-pasted fixture), this fails loud instead of a test suite
    silently trying to open a real socket -- or hanging on one.
    """
    marker = os.environ.get("PYTEST_CURRENT_TEST")
    if not marker:
        return
    raise ProductionDatabaseGuardError(
        f"Refusing to open {target_desc} while running under pytest "
        f"(PYTEST_CURRENT_TEST={marker!r}). No test should ever resolve "
        "`store.backend: postgres` from coordinator.yml -- see #1960, the "
        "SQLite analogue of this guard. Fix: rely on the autouse `coord_db` "
        "fixture (it overrides the connection before this is ever reached "
        "for the write path), or don't set store.backend in a test "
        "environment's config."
    )


def _open_postgres(dsn: str) -> Any:
    """Open (and, if needed, migrate) the configured Postgres connection —
    the Postgres analogue of :func:`_open` (#827).

    There is exactly one configured DSN (``coordinator.yml``'s
    ``store.dsn``), so opening it always means opening THE production store
    -- unlike ``_open``, there is no "arbitrary path a test/caller passed in"
    case to distinguish, hence no *is_production* parameter here.

    #827 review, non-blocking concern: the per-THREAD connection cache
    :func:`get_connection` builds this into means ``_migrate_if_needed``
    (below) can run concurrently from more than one thread on first connect
    against a fresh Postgres database -- the old process-wide SQLite
    singleton naturally serialized this, a per-thread cache does not. Two
    request-handler threads racing to open the first connection could both
    observe ``schema_version < _DB_SCHEMA_VERSION`` and both attempt the
    migration DDL at once. Not exercised today (no Postgres is live
    anywhere per this issue's own "mergeable and inert" scope), and the
    migration functions themselves are written to be idempotent/order-
    tolerant on SQLite already, but this has never been proven safe under
    genuine Postgres concurrency -- left as a flagged gap for #829's
    cutover to either measure (maybe never actually races in practice) or
    close (e.g. a Postgres advisory lock around this call) before real
    traffic depends on it.
    """
    refuse_postgres_under_pytest("the configured production Postgres store")
    conn = sql.connect(backend=sql.DIALECT_POSTGRES, dsn=dsn)
    sql.apply_connection_setup(conn)
    sql.apply_row_factory(conn)
    _migrate_if_needed(
        conn,
        is_production=True,
        target_desc="the configured production Postgres store (coordinator.yml's store.dsn)",
    )
    return conn


def open_postgres_connection(dsn: str) -> Any:
    """Public entry point onto :func:`_open_postgres` for tooling that needs
    a fully-migrated Postgres connection outside the ``get_connection()``
    singleton -- today, only ``coord.store_migrate``'s importer (#828), which
    opens a target that may not (yet) be the DSN this process's own
    ``coordinator.yml`` names, so it calls this directly with whatever DSN
    the operator gave it rather than going through :func:`get_connection`'s
    config-resolution path. Same guarantees as every other caller of
    :func:`_open_postgres`: refuses under pytest, and runs the same schema
    creation/migration path SQLite gets (#827 item 3).
    """
    return _open_postgres(dsn)


# #2752: a clean, tagged release is always exactly "X.Y.Z" -- setuptools_scm
# only emits anything else (a ".devN+g<sha>" local/dev suffix, a bare
# "0+unknown" when no distribution resolves at all, or git-describe's
# hyphenated "X.Y.Z-N-g<sha>[-dirty]" fallback from coord/__init__.py's
# _live_scm_version) when HEAD is not exactly at a release tag -- a
# worktree, an editable checkout, or a build with no install at all. See
# coord/__init__.py's _resolve_version for how __version__ is computed.
_RELEASE_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")


def _is_release_build(version: str) -> bool:
    """True when *version* is shaped like a clean ``X.Y.Z`` release tag,
    with no dev/local-version suffix (#2752). See :data:`_RELEASE_VERSION_RE`
    for why any other shape means "not built from a release tag"."""
    return _RELEASE_VERSION_RE.fullmatch(version) is not None


def override_connection(conn: Any) -> None:
    """Replace the singleton connection.  Used in tests to inject :memory: DBs.

    Wins over both connection-sharing models #827 introduced: once set,
    :func:`get_connection` returns *conn* unconditionally, on every thread,
    whether the configured backend is SQLite or Postgres — the per-thread
    Postgres cache is never even consulted while an override is active.
    """
    global _conn
    _conn = conn


def close() -> None:
    """Close this process's connection(s) and reset the singleton(s) to None.

    SQLite: closes and clears the process-wide ``_conn`` singleton (or
    whatever :func:`override_connection` last injected), exactly as before
    #827. Postgres: additionally closes and clears THIS THREAD's connection
    out of the per-thread cache :func:`get_connection` populates — the only
    one this call has any business touching; a multi-threaded Postgres
    deployment closing every thread's connection is each thread's own
    responsibility (or the OS reclaiming the socket on process exit either
    way). This function has only ever promised to reset the connection(s)
    *this* call can see.
    """
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None
    pg_conn = getattr(_pg_thread_local, "conn", None)
    if pg_conn is not None:
        pg_conn.close()
        _pg_thread_local.conn = None


_T = TypeVar("_T")


# ── Recovering a connection after a swallowed driver error (#2983) ──────────


def _board_connection_if_open() -> Any | None:
    """This process's already-open board connection, or ``None`` (#2983).

    Deliberately NOT :func:`get_connection`: this is called from inside a
    driver-error handler, where *opening* a connection that wasn't open
    before would be a surprising side effect (and, on the SQLite path,
    would create the database file). It only ever hands back a connection
    something else already opened — the ``override_connection()``/SQLite
    singleton, else THIS thread's Postgres connection, mirroring
    :func:`get_connection`'s own precedence order.
    """
    if _conn is not None:
        return _conn
    return getattr(_pg_thread_local, "conn", None)


def rollback_after_driver_error(conn: Any | None, exc: BaseException) -> None:
    """Clear the aborted transaction *exc* left behind on *conn*, so a
    handler that swallowed *exc* and keeps using the same connection still
    has a usable one (#2983).

    **The rule this implements.** After catching a driver error through the
    dialect seam (``except sql.driver_error(conn):`` /
    ``except sql.driver_errors():``), a handler that intends to CONTINUE on
    the same connection must roll back first. A handler that re-raises need
    not. Postgres aborts the *whole* transaction on any failed statement,
    so without this every statement issued afterwards on that connection —
    for the rest of the process, since ``coord.db``'s connection is a
    process-/thread-lived singleton — raises
    ``psycopg.errors.InFailedSqlTransaction``. That is what made the whole
    #2597/#2689/#2784 degrade-gracefully layer inert or actively harmful on
    the second backend: guards written to *prevent* a crash caused one.
    SQLite has no such concept — a failed statement there leaves the
    connection perfectly usable — which is exactly why every one of these
    21 handler sites read as correct for years.

    **Gated on the exception, not on the connection's dialect.** *exc* is a
    Postgres driver error iff it carries a SQLSTATE (``psycopg3``'s
    ``.sqlstate``, ``psycopg2``'s ``.pgcode`` — the same pair
    :func:`is_lock_contention_error` dispatches on, for the same #2719
    reason: no config flag, no driver import). Keying off that rather than
    ``sql.detect_dialect(conn)`` makes "SQLite behaviour is byte-identical
    to before #2983" true *by construction* rather than by argument: a
    ``sqlite3.Error`` has no SQLSTATE, so this returns without touching the
    connection and no SQLite caller can lose uncommitted work it expected
    to survive into the next statement (``retry_on_locked``'s multi-
    statement writers being the case where that would actually be
    observable). #2982's sibling fix in ``_migrate_add_columns`` predates
    this helper and rolls back unconditionally; it is correct there (the
    only uncommitted statement at that point is the one that just failed)
    and is deliberately left as-is.

    A ``None`` *conn* is a no-op — see :func:`_board_connection_if_open`,
    which may legitimately have nothing to hand over. A failure of the
    rollback itself is suppressed: this runs inside an ``except`` block
    whose original exception is the interesting one, and masking it with a
    secondary error from the recovery attempt would be strictly worse than
    leaving the connection unrecovered.
    """
    if conn is None:
        return
    if getattr(exc, "sqlstate", None) is None and getattr(exc, "pgcode", None) is None:
        return  # not a Postgres driver error — nothing was aborted
    try:
        conn.rollback()
    except Exception:  # noqa: BLE001 — never mask the caught driver error
        pass


# #2538: a handful of consecutive short retries — long enough to ride out a
# concurrent writer (the daemon's own passive tick, another `coord merge`/
# `coord notify` invocation) that only holds the DB for a moment, short
# enough that a genuinely stuck lock still fails fast rather than hanging
# `coord merge` for a long time.
_LOCK_RETRY_ATTEMPTS = 5
_LOCK_RETRY_BASE_DELAY_S = 0.1


# #2784: Postgres SQLSTATEs that are contention-shaped the same way SQLite's
# SQLITE_BUSY is -- a concurrent transaction is in the way right now, and a
# short retry is very likely to let it clear.
#   55P03 lock_not_available   -- NOWAIT/lock-timeout equivalent of SQLITE_BUSY
#   40001 serialization_failure -- SERIALIZABLE isolation lost a write race
#   40P01 deadlock_detected     -- Postgres's deadlock detector picked this
#                                  transaction as the victim to abort
# 57014 query_canceled (statement_timeout) is deliberately NOT included: it
# fires when a statement runs too long, which is just as often a genuinely
# slow/wrong query as it is contention -- retrying a query that will always
# time out the same way is wrong, unlike the three codes above where the
# *same* statement retried a moment later commonly just succeeds.
_POSTGRES_LOCK_CONTENTION_SQLSTATES = frozenset({"55P03", "40001", "40P01"})


class LockContentionExhaustedError(RuntimeError):
    """Raised when sustained DB lock contention outlasts
    :func:`retry_on_locked`'s whole retry budget and a caller has decided
    the failure must surface loudly rather than degrade silently (#2784).

    Dialect-agnostic and coord-owned rather than re-raising (or
    synthesizing) a driver-specific exception: unlike the 18 sites this
    issue widens to ``except sql.driver_errors():``, ``coord/commands/
    merge.py``'s auto-enqueue scan doesn't have a real driver error it is
    forwarding from a live connection in scope — it collects
    ``(assignment_id, exc)`` pairs across a whole batch and, once the batch
    finishes, raises once to fail the ``coord merge`` invocation loudly.
    Before #2784, that final raise fabricated a bare ``sqlite3.
    OperationalError(...)`` — a lie about the exception's actual driver
    origin once a Postgres deployment exists, and exactly the kind of
    driver-named exception outside ``coord/sql.py`` the #2768 ratchet now
    forbids. This type says what actually happened (retries exhausted, not
    "SQLite raised an OperationalError") regardless of which dialect is
    live; the *original* per-assignment driver error is still preserved as
    ``__cause__`` (``raise ... from lock_contention_failures[-1][1]``) for
    anyone inspecting the traceback.
    """


def is_lock_contention_error(exc: BaseException) -> bool:
    """True when *exc* is transient lock/busy contention rather than a real
    bug (#2597, widened to Postgres by #2784).

    Centralizes a check that used to be duplicated — and drifting — between
    this module's own :func:`retry_on_locked` and ``coord.auto_loop``'s
    ``except`` block: only the ``"database is locked"`` substring was
    matched anywhere, missing both SQLite's other lock-collision message
    (``"database table is locked"``, raised for a table-level lock rather
    than the whole-database one) and the underlying ``SQLITE_BUSY`` result
    code, which a driver could in principle surface through a differently
    worded message. Checking the code as well as the text means a future
    SQLite/driver wording change can't silently turn this back into a
    "raise instead of retry" bug the way the single-substring version could.

    Dispatches on the exception itself -- no connection, no config flag, per
    #2719's whole seam posture (see ``coord/sql.py``'s module docstring): a
    ``psycopg.OperationalError`` carries a ``sqlstate`` attribute (psycopg3)
    or a ``.pgcode`` (psycopg2/error subclasses) that plays the same role
    SQLite's ``sqlite_errorcode`` does, so the Postgres arm below checks
    that instead of a message substring -- Postgres's error text is not a
    stable API the way its SQLSTATE codes are.
    """
    if isinstance(exc, sqlite3.OperationalError):
        message = str(exc).lower()
        if "database is locked" in message or "database table is locked" in message:
            return True
        return getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_BUSY
    # Not sqlite3 -- check for a Postgres driver error without importing
    # psycopg (it's an optional dependency, see coord/sql.py's
    # row_factory_for): psycopg3 exposes `.sqlstate`, psycopg2 exposes
    # `.pgcode` on the same class hierarchy -- either attribute, if present,
    # is the SQLSTATE 5-char code.
    sqlstate = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
    return sqlstate in _POSTGRES_LOCK_CONTENTION_SQLSTATES


def retry_on_locked(
    write: Callable[[], _T],
    *,
    conn: Any | None = None,
    attempts: int = _LOCK_RETRY_ATTEMPTS,
    base_delay: float = _LOCK_RETRY_BASE_DELAY_S,
) -> _T:
    """Run *write* (a zero-arg callable performing one or more database
    writes), retrying with exponential backoff when the active driver raises
    lock/busy contention (:func:`is_lock_contention_error`, #2538/#2597,
    widened to Postgres by #2784).

    That error is transient contention — a concurrent writer holding the DB
    at the exact moment this call tries to write, not a real failure — and a
    short wait is very likely to let it clear.  ``PRAGMA busy_timeout``
    (set on every SQLite connection in :func:`_open`) already makes SQLite
    itself wait before raising, but under sustained contention (several
    writers in a tight loop) that single wait can still be exhausted; this
    adds a few more short, backed-off attempts on top rather than failing on
    the first collision.

    Any other driver error (schema drift, a malformed statement, …) is
    re-raised immediately without retrying — those are not transient, and
    retrying would only delay surfacing a real bug. Caught via
    ``sql.driver_errors()`` — every installed driver's ``Error`` base,
    **not** a hardcoded ``sqlite3.OperationalError`` — so this still retries
    under Postgres instead of letting a psycopg exception, which is never an
    instance of ``sqlite3.OperationalError``, sail straight past the
    ``except`` and crash (#2784). After *attempts* consecutive
    lock-contention collisions, re-raises the last driver error so the
    caller can decide how to degrade (see
    ``coord.state._record_dispatched_assignment_local``, whose caller —
    ``coord.auto_loop._dispatch_fix`` — treats it as a declined dispatch
    rather than letting it crash the whole run).

    Transaction recovery, and whether "retry" means anything on Postgres
    (#2983)
    ------------------------------------------------------------------
    SQLite's ``SQLITE_BUSY`` retry model is "the statement never ran; the
    lock is held elsewhere; the transaction is untouched" — so re-running
    *write* verbatim is exactly the right move, and has been since #2538.
    Postgres's three contention SQLSTATEs
    (:data:`_POSTGRES_LOCK_CONTENTION_SQLSTATES`) are contention-shaped in
    the same way, but they ALSO abort the whole transaction. That makes the
    retry loop *worse than useless* on Postgres without recovery: attempt 2
    onwards raises ``InFailedSqlTransaction`` instead of re-attempting the
    write, ``is_lock_contention_error`` reads False for it, and the loop
    bails out on attempt 2 re-raising the wrong error — the #2597/#2689
    retry budget silently reduced to a single attempt.

    So the answer to "does SQLite's retry model have a Postgres analogue at
    all, or should the retry just be a no-op there?" is: it has one, and it
    is the standard Postgres idiom — **roll back, then re-run the whole
    transaction.** Every *write* callable in this tree is already a
    self-contained transaction (its own statements followed by its own
    ``conn.commit()``), which is precisely what makes re-running it after a
    rollback correct rather than a partial replay. Hence
    :func:`rollback_after_driver_error` before both the retry and the final
    re-raise: after the re-raise the connection must still be usable too,
    because ~13 of this tree's callers catch that exception and *swallow*
    it (a cache mirror whose upstream GitHub write already landed must not
    turn into a 503), then keep using the same singleton connection.
    Recovering here rather than in each of those handlers is what keeps the
    guarantee in one place: **when this function returns OR raises, it has
    not left an aborted transaction behind.**

    *conn* is the connection *write* writes through, needed only for that
    rollback. It defaults to :func:`_board_connection_if_open` because
    every call site in this tree writes through ``get_connection()``
    (``coord.state``, ``coord.merge_queue``, ``coord.audit``,
    ``coord.commands.merge``), so the default is correct for all of them
    and none had to be churned; pass it explicitly if you ever write
    through a different connection. Rolling back an innocent already-
    committed connection would be a no-op anyway — every writer here
    commits immediately — and the rollback is skipped entirely unless the
    caught exception carries a Postgres SQLSTATE, so the SQLite path is
    byte-identical to before #2983.
    """
    delay = base_delay
    for attempt in range(1, attempts + 1):
        try:
            return write()
        except sql.driver_errors() as exc:
            rollback_after_driver_error(
                conn if conn is not None else _board_connection_if_open(), exc
            )
            if not is_lock_contention_error(exc) or attempt >= attempts:
                raise
            time.sleep(delay)
            delay *= 2
    raise AssertionError("unreachable")  # loop above always returns or raises


# ── Schema ────────────────────────────────────────────────────────────────────

# #2598: bumped from the original unversioned "1" written by every process
# forever. Read by _read_schema_version()/_open() as the target a database
# must reach before _open() can skip its write path entirely. Bump this for
# ANY addition that a database already at the current version would
# otherwise never receive — a new one-shot _migrate_*/_backfill_* function,
# **or a new entry appended to the `_migrate_add_columns` list** (#2675:
# #2604 and #2589 each appended an `ALTER TABLE ... ADD COLUMN` there
# without bumping this, so every database already at version 2 read
# `_read_schema_version(conn) < _DB_SCHEMA_VERSION` as False and skipped
# `_ensure_schema` — and therefore `_migrate_add_columns` — entirely,
# permanently missing both columns. `_migrate_add_columns` itself is safe to
# call repeatedly (idempotent, swallows the "column already exists" error),
# but that only matters if `_open()` actually calls it — an already-
# caught-up database (version == _DB_SCHEMA_VERSION) never will, because the
# whole write path is gated on this compare. There is no exemption: if you
# touched `_migrate_add_columns`, bump this.
#
# #2709: that comment alone did not stop a THIRD occurrence — #2687
# appended `uat_state`/`uat_reason` without bumping this, one day after
# #2675 added the comment above. A comment is not a gate. There is now a
# structural one: `_MIGRATE_ADD_COLUMNS` (below `_migrate_add_columns`) is
# a module-level list precisely so
# `tests/test_db.py::TestMigrateAddColumnsVersionGuard` can pin its length
# next to this version number — append an entry without updating both and
# that test fails red. If you touched `_migrate_add_columns`, bump this
# AND update the pinned count in that test, in the same commit.
#
# #2786: bumped 5 -> 6 for the `assignments.num_turns` column appended to
# `_migrate_add_columns` below.
#
# #2987: bumped 6 -> 7 for the two `portal_sync_state.relayed_answer_
# watermark_*` columns appended to `_migrate_add_columns` below.
#
# #3011: bumped 7 -> 8 for the two `merge_queue.ci_fix_head_sha`/
# `ci_fix_noop_streak` columns appended to `_migrate_add_columns` below.
_DB_SCHEMA_VERSION = 8


def _read_schema_version(conn: sqlite3.Connection) -> int:
    """Cheap, read-only probe of how caught-up *conn*'s database is (#2598).

    A plain ``SELECT`` — never begins a write transaction, so calling this
    to decide whether _open() needs to do any write work cannot itself take
    the write lock.

    Returns 0 when ``schema_version`` doesn't exist yet (brand-new database)
    or is empty. Otherwise returns the highest stored value, which reads
    correctly against *either* table shape: the pre-#2598 unconstrained
    table (every row holds the same value, so MAX is that value — junk rows
    don't change the answer) or the post-#2598 constrained table (exactly
    one row). A pre-#2598 database therefore reads as version 1, which is
    ``< _DB_SCHEMA_VERSION`` — so it takes the write path exactly once more,
    which is what collapses its schema_version duplicates and applies the
    PRIMARY KEY (see _fix_schema_version_table). Every open after that reads
    the current ``_DB_SCHEMA_VERSION`` and skips the write path entirely —
    until the next bump (#2675: that skip is exactly what silently dropped
    two `_migrate_add_columns` entries when they landed without one).

    #2983: the "table doesn't exist yet" swallow below needs its own
    rollback — #2982's fix is one layer downstream and does not cover this.
    On a fresh Postgres database this is the FIRST statement `_open_postgres`
    -> `_migrate_if_needed` runs, it errors (`UndefinedTable`), and that
    aborts the transaction; the caller then continues on the same connection
    straight into `_fix_schema_version_table`'s `CREATE TABLE IF NOT
    EXISTS`, which raises `InFailedSqlTransaction`. So on Postgres the
    brand-new-database path — the one case this guard exists for — could
    never get past its own first statement.
    """
    try:
        row = sql.execute(conn, "SELECT MAX(version) FROM schema_version").fetchone()
    except sql.driver_errors() as exc:  # #2784: was sqlite3.OperationalError only
        rollback_after_driver_error(conn, exc)  # #2983: caller keeps using `conn`
        return 0  # table doesn't exist yet
    if row is None or row[0] is None:
        return 0
    return int(row[0])


def _fix_schema_version_table(conn: sqlite3.Connection) -> None:
    """Collapse the pre-#2598 unconstrained ``schema_version`` table.

    The original table declared no ``PRIMARY KEY``/``UNIQUE`` constraint, so
    ``INSERT OR IGNORE INTO schema_version VALUES (1)`` — meant to be a
    once-per-database no-op — actually succeeded on every single process
    open, leaving one junk row per open (45,708 observed in the field).
    This rebuilds the table with a real ``PRIMARY KEY`` on ``version`` so
    that no longer happens, collapsing any existing duplicate rows down to
    their distinct values in the process.

    Safe to call against any existing shape: the old broken table (any
    number of duplicate rows, no constraint), the new constrained table
    (already migrated — a no-op), or no table at all (fresh database).

    The duplicate-row shape is SQLite-only legacy debt (#2724): it can only
    exist on a database that lived through the pre-#2598 unconstrained
    ``INSERT OR IGNORE`` era, which no Postgres deployment ever did. A
    Postgres connection therefore skips straight to ensuring the
    constrained table exists, with none of the collapse dance.
    """
    dialect = sql.detect_dialect(conn)
    if dialect != sql.DIALECT_SQLITE:
        sql.executescript(
            conn, "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)"
        )
        conn.commit()
        return
    row = sql.execute(
        conn, "SELECT sql FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if row is not None and "PRIMARY KEY" in (row[0] or ""):
        return  # already the constrained post-#2598 shape
    sql.executescript(
        conn,
        """
        CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
        CREATE TABLE schema_version_new (version INTEGER PRIMARY KEY);
        """,
    )
    sql.insert_ignore_select(
        conn, "schema_version_new", "SELECT DISTINCT version FROM schema_version"
    )
    sql.executescript(
        conn,
        """
        DROP TABLE schema_version;
        ALTER TABLE schema_version_new RENAME TO schema_version;
        """,
    )
    conn.commit()


def _set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    """Overwrite ``schema_version`` with a single row holding *version*.

    ``schema_version`` holds exactly one fact — how caught-up this database
    is — so a version bump DELETEs the old row rather than accumulating one
    row per version ever seen (which ``INSERT OR IGNORE``/``INSERT OR
    REPLACE`` would do here, since they only dedupe on a *matching* primary
    key, and a version bump's whole point is that the key changes).
    """
    sql.execute(conn, "DELETE FROM schema_version")
    sql.execute(conn, "INSERT INTO schema_version (version) VALUES (?)", (version,))
    conn.commit()


# Every ``__AUTOPK_DDL__`` sentinel below is substituted at schema-creation
# time (see :func:`_ensure_schema`) with the dialect-appropriate
# auto-incrementing-integer-primary-key DDL from
# :func:`coord.sql.autoincrement_pk_ddl` -- this file names no backend's
# schema syntax directly (#2724).
_SCHEMA_SQL = """
        CREATE TABLE IF NOT EXISTS assignments (
            assignment_id TEXT PRIMARY KEY,
            machine_name TEXT NOT NULL,
            repo_name TEXT NOT NULL,
            repo_github TEXT,
            issue_number INTEGER NOT NULL,
            issue_title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            type TEXT NOT NULL DEFAULT 'work',
            branch TEXT,
            pr_url TEXT,
            briefing TEXT DEFAULT '',
            files_allowed TEXT DEFAULT '[]',
            files_forbidden TEXT DEFAULT '[]',
            model TEXT,
            dispatched_at REAL,
            finished_at REAL,
            smoke_test TEXT,
            smoke_test_reason TEXT,
            review_state TEXT,
            review_of_assignment_id TEXT,
            review_target TEXT,
            required_gates TEXT DEFAULT '[]',
            plan TEXT,
            unreachable_count INTEGER DEFAULT 0,
            exit_code INTEGER,
            review_iteration INTEGER DEFAULT 0,
            review_posted_at REAL,
            test_state TEXT,
            test_reason TEXT,
            uat_state TEXT,
            uat_reason TEXT,
            cost_usd REAL,
            smoke_tests TEXT,
            review_findings TEXT,
            test_plan TEXT
        );

        CREATE TABLE IF NOT EXISTS notifications (
            assignment_id TEXT PRIMARY KEY,
            event TEXT NOT NULL,
            branch TEXT,
            posted_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS proposals (
            id INTEGER PRIMARY KEY,
            machine_name TEXT NOT NULL,
            repo_name TEXT NOT NULL,
            issue_number INTEGER NOT NULL,
            issue_title TEXT NOT NULL,
            rationale TEXT DEFAULT '',
            files_likely TEXT DEFAULT '[]',
            briefing TEXT DEFAULT '',
            model TEXT,
            type TEXT DEFAULT 'work',
            required_gates TEXT DEFAULT '[]'
        );

        CREATE TABLE IF NOT EXISTS split_proposals (
            id INTEGER PRIMARY KEY,
            repo_name TEXT NOT NULL,
            issue_number INTEGER NOT NULL,
            issue_title TEXT NOT NULL,
            rationale TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS split_chunks (
            id __AUTOPK_DDL__,
            split_proposal_id INTEGER NOT NULL REFERENCES split_proposals(id),
            title TEXT NOT NULL,
            scope TEXT NOT NULL,
            files_likely TEXT DEFAULT '[]'
        );

        CREATE TABLE IF NOT EXISTS merge_queue (
            id __AUTOPK_DDL__,
            assignment_id TEXT NOT NULL,
            repo_name TEXT NOT NULL,
            repo_github TEXT NOT NULL,
            branch TEXT NOT NULL,
            target_branch TEXT NOT NULL,
            issue_number INTEGER NOT NULL,
            issue_title TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending',
            pr_number INTEGER,
            pr_url TEXT,
            size INTEGER,
            last_attempt REAL,
            error TEXT,
            enqueued_at REAL,
            assignment_type TEXT DEFAULT 'work',
            required_gates TEXT,
            ci_infra_reruns INTEGER NOT NULL DEFAULT 0,
            ci_stale_reruns INTEGER NOT NULL DEFAULT 0,
            ci_flaky_reruns INTEGER NOT NULL DEFAULT 0,
            ci_flaky_pending TEXT NOT NULL DEFAULT '',
            ci_unreadable_reruns INTEGER NOT NULL DEFAULT 0,
            ci_fix_dispatches INTEGER NOT NULL DEFAULT 0,
            ci_fix_head_sha TEXT NOT NULL DEFAULT '',
            ci_fix_noop_streak INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS plans (
            assignment_id TEXT PRIMARY KEY,
            plan_data TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id __AUTOPK_DDL__,
            started_at TEXT,
            ended_at TEXT,
            clean_shutdown INTEGER DEFAULT 0,
            completed_this_session TEXT,
            issues_closed TEXT,
            total_cost_usd REAL
        );

        CREATE TABLE IF NOT EXISTS machines (
            name TEXT PRIMARY KEY,
            host TEXT NOT NULL,
            capabilities TEXT DEFAULT '[]',
            repos TEXT DEFAULT '[]'
        );

        CREATE TABLE IF NOT EXISTS board_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS issues (
            repo_name  TEXT    NOT NULL,
            number     INTEGER NOT NULL,
            title      TEXT    NOT NULL DEFAULT '',
            body       TEXT    NOT NULL DEFAULT '',
            state      TEXT    NOT NULL DEFAULT 'open',
            labels     TEXT    NOT NULL DEFAULT '[]',
            synced_at  REAL,
            PRIMARY KEY (repo_name, number)
        );

        -- #603: per-issue rolling context digest.  Short-lived curated notes
        -- (cross-repo deps, failed approaches, hard constraints) that every
        -- agent reads at the top of its briefing.  Structured entries rendered
        -- to markdown at read time; pinned rows stay on top, newest-first
        -- below, oldest non-pinned aged out by a budget; dropped on close.
        CREATE TABLE IF NOT EXISTS issue_context (
            id           INTEGER PRIMARY KEY,
            repo_name    TEXT    NOT NULL,
            issue_number INTEGER NOT NULL,
            pinned       INTEGER NOT NULL DEFAULT 0,
            source       TEXT,
            body         TEXT    NOT NULL,
            created_at   REAL    NOT NULL
        );

        -- #1036: durable, append-only audit trail.  One row per real
        -- state-changing transition, captured at the state._*_local /
        -- issue_store write choke points (never at the ~30 CLI call
        -- sites) so the log is topology-agnostic (thin-client vs daemon).
        -- Modeled on issue_context above: additive, no UPDATE/DELETE
        -- except the opportunistic audit.max_rows trim in coord/audit.py.
        CREATE TABLE IF NOT EXISTS audit_log (
            id            __AUTOPK_DDL__,
            ts            REAL    NOT NULL,
            tier          TEXT    NOT NULL,
            category      TEXT    NOT NULL,
            event_type    TEXT    NOT NULL,
            actor         TEXT    NOT NULL,
            repo          TEXT,
            issue         INTEGER,
            assignment_id TEXT,
            machine       TEXT,
            summary       TEXT    NOT NULL DEFAULT '',
            details_json  TEXT
        );

        -- #873: durable, queryable mirror of GitHub issue comments — the
        -- prose message bus (completion summaries, review bodies, failure
        -- reports) today lives only on GitHub. Populated two ways: (1)
        -- capture-at-write, instrumented at the single choke point
        -- coord.github_ops.post_issue_comment (which close_issue/close_pr
        -- also funnel through), so a coord-authored comment is durable the
        -- instant it posts, regardless of which machine posted it; (2) the
        -- backfill sync (coord.state.sync_issue_comments), which upserts
        -- human + out-of-band comments idempotently keyed on gh_comment_id.
        -- Retained INDEFINITELY — never pruned on close, unlike the
        -- short-lived issue_context digest above.
        --
        -- repo_name here holds whatever identifier the write site had —
        -- in practice the GitHub "owner/repo" slug (github_ops functions
        -- only ever see the slug, never the coordinator's config repo
        -- key) — which differs from the issues/issue_context tables above
        -- where repo_name is the coordinator.yml repo key. Documented here
        -- since it's a real inconsistency a future reader could trip on.
        --
        -- created_at/updated_at are epoch seconds (REAL), matching the
        -- rest of this schema's timestamp columns. body_ref is reserved,
        -- unused for now — the future Azure-blob offload seam.
        CREATE TABLE IF NOT EXISTS issue_comments (
            id                  __AUTOPK_DDL__,
            gh_comment_id       INTEGER UNIQUE,
            repo_name           TEXT    NOT NULL,
            issue_number        INTEGER NOT NULL,
            author              TEXT,
            created_at          REAL,
            updated_at          REAL,
            body                TEXT    NOT NULL DEFAULT '',
            body_ref            TEXT,
            coord_event         TEXT,
            coord_assignment_id TEXT,
            machine             TEXT,
            verdict             TEXT
        );

        -- #1505: board-visible "driver stuck" record.  Written by `coord
        -- drive`'s merge stage the moment it hits a status no amount of
        -- retrying can fix (NEEDS_ATTENTION / an unrecognised status) instead
        -- of silently burning the merge-attempt budget on it (see
        -- coord/drive.py's `_decide_merge`).  One row per (repo_name,
        -- issue_number) — a fresh escalation replaces the previous one
        -- (`ON CONFLICT ... DO UPDATE`, coord/state.py
        -- `_record_drive_escalation_local`); `coord escalate dismiss` (or the
        -- TUI's "Dismiss" menu item) deletes it outright. `gate_readings` is
        -- a human-readable "key=value | key=value" summary, not JSON — the
        -- whole record exists to be read by a human via the Pipeline row's
        -- right-click menu, not machine-parsed.
        CREATE TABLE IF NOT EXISTS drive_escalations (
            id               __AUTOPK_DDL__,
            repo_name        TEXT    NOT NULL,
            issue_number     INTEGER NOT NULL,
            stage            TEXT    NOT NULL DEFAULT 'merge',
            assignment_id    TEXT,
            reason           TEXT    NOT NULL,
            gate_readings    TEXT    NOT NULL DEFAULT '',
            proposed_command TEXT    NOT NULL DEFAULT '',
            created_at       REAL    NOT NULL,
            UNIQUE(repo_name, issue_number)
        );

        -- #1753 (DQ-1): the operator-declared `coord drive` work queue.  One
        -- row per (repo_name, issue_number) — enqueueing an issue that is
        -- already queued updates it in place (`ON CONFLICT ... DO UPDATE`,
        -- coord/state.py `_enqueue_drive_queue_local`) rather than creating a
        -- second row, so the queue can never hold the same issue twice.
        -- Written by `coord drive-queue` (DQ-2) and by the tick processor that
        -- launches entries; cleared by `coord drive-queue remove` (dequeue) —
        -- terminal rows (`done`/`failed`) stay put until an operator removes
        -- them, so the list doubles as a short run history.
        --
        -- `position` is DENSE and 0-BASED: enqueue-without-a-position appends
        -- at max(position)+1 and `move` renumbers the affected span in one
        -- transaction (no fractional positions — the queue is short by nature
        -- and `coord drive-queue list` prints a dense integer order).
        -- `machine` NULL means "let `coord drive` route it".  `after_json` is
        -- a JSON list of FULLY QUALIFIED pre-req keys (`["repo#N", ...]`) so a
        -- cross-repo queue needs no second column; it is decoded to a real
        -- list on the wire via its `list[str]` field on
        -- coord/board_schema.py's BoardDriveQueueEntry.  This table
        -- only STORES `after_json` — interpreting it (pre-req satisfaction) is
        -- the tick processor's job, not the schema's.
        --
        -- #1757 (deploy gates) added the five `hold_*`/`resume_when` columns
        -- below.  They are declared here so a FRESH database gets them from
        -- the CREATE, and repeated in `_migrate_add_columns` so an EXISTING
        -- ~/.coord/coord.db (DQ-1 shipped before this) gains them in place —
        -- SQLite has no "ADD COLUMN IF NOT EXISTS", hence both.  See
        -- coord/drive_queue.py for the lifecycle those columns encode.
        --
        -- #1870: `launch_host` is the SHORT HOSTNAME of the machine whose tick
        -- actually ran `coord drive --tmux` for this entry — set alongside
        -- `session_name`/`launched_at` when the launch succeeds, '' for a row
        -- that predates this column or was hand-flipped to `running`.
        -- Liveness (`coord.drive.list_drive_sessions`) is always a LOCAL tmux
        -- read; without this column a tick running on a DIFFERENT host than
        -- the one that launched the drive has no way to know that, sees no
        -- session, and reaps a healthy drive out from under another machine
        -- (the 2026-08-06 incident: a drive 47 minutes into Test on
        -- `elitebook` was declared dead and relaunched by the timer on
        -- `dellserver`).  `_reconcile_running` treats a mismatch as UNKNOWN,
        -- never as dead — see coord/drive_queue.py.
        CREATE TABLE IF NOT EXISTS drive_queue (
            id            __AUTOPK_DDL__,
            repo_name     TEXT    NOT NULL,
            issue_number  INTEGER NOT NULL,
            position      INTEGER NOT NULL,
            machine       TEXT,
            after_json    TEXT    NOT NULL DEFAULT '[]',
            state         TEXT    NOT NULL DEFAULT 'waiting',
            attempts      INTEGER NOT NULL DEFAULT 0,
            deferrals     INTEGER NOT NULL DEFAULT 0,
            last_reason   TEXT    NOT NULL DEFAULT '',
            -- #2133: wall-clock time `last_reason` was last WRITTEN, stamped
            -- by `coord.state._update_drive_queue_entry_local` every time a
            -- `last_reason` update lands (never by a caller — it is not in
            -- `_DRIVE_QUEUE_UPDATABLE`). `last_reason` is a point-in-time
            -- observation, not a live probe; without this a `blocked` entry's
            -- displayed text ages silently and a stale-but-plausible reason
            -- reads as current state (2026-08-11's #2104 incident: a 3-hour-
            -- stale `checks_failed` outlived the CI failure it named while
            -- the real, later blocker — a request-changes review — went
            -- unmentioned). NULL for a row written before this migration or
            -- for a fresh row whose `last_reason` is still the '' default.
            reason_at     REAL,
            session_name  TEXT,
            launched_at   REAL,
            enqueued_at   REAL    NOT NULL,
            hold_after    INTEGER NOT NULL DEFAULT 0,
            hold_reason   TEXT    NOT NULL DEFAULT '',
            resume_when   TEXT    NOT NULL DEFAULT '',
            hold_state    TEXT    NOT NULL DEFAULT '',
            hold_probes   INTEGER NOT NULL DEFAULT 0,
            launch_host   TEXT    NOT NULL DEFAULT '',
            -- #2186: how far a FIRED gate reaches. 'entry' (the default) holds
            -- only entries whose own after_json names this row's key; 'fleet'
            -- is the pre-#2186 whole-queue stop, kept for an explicit
            -- --scope=fleet. NOT NULL DEFAULT 'entry' so a row written before
            -- this column existed reads as the narrower scope, never a silent
            -- fleet-wide one — see coord.drive_queue.QueueEntry.hold_scope.
            hold_scope    TEXT    NOT NULL DEFAULT 'entry',
            -- #2230: count of times the tick's blocked-reconciliation sweep
            -- has auto-resumed THIS row from 'blocked' back to 'waiting' —
            -- see coord.drive_queue.MAX_BLOCKED_RESUMES / _reconcile_blocked.
            -- `blocked` used to be genuinely terminal (nothing ever asked
            -- again whether the gate that blocked an entry had since
            -- cleared); this bounds how many times the sweep will requeue
            -- the SAME entry before it stops and leaves it blocked for an
            -- operator, so a gate that itself flaps cannot oscillate the
            -- entry forever. 0 for every row predating this column, same as
            -- a freshly-enqueued entry that has never been auto-resumed.
            resumes       INTEGER NOT NULL DEFAULT 0,
            -- #2273 (post-review): the wall-clock moment a `retry` reconcile
            -- recorded a death, and ONLY that — see coord.drive_queue's
            -- `_retry_backoff_reason` docstring. Deliberately a SEPARATE
            -- column from `reason_at` above: `reason_at` is re-stamped by
            -- EVERY `last_reason` write, including the backoff-deferral's
            -- own per-tick status refresh (`deferrals`/`last_reason`,
            -- written every tick an entry is still backing off) — keying the
            -- backoff window off a field the backoff mechanism itself
            -- rewrites made the window's own clock reset every tick it was
            -- checked, so an entry whose backoff exceeded the tick interval
            -- could never finish waiting (the "moving target" bug: age
            -- computed at any later tick was always ~one tick interval, never
            -- the true elapsed time). `retry_backoff_at` is written ONLY by
            -- the `retry` reconcile in `_reconcile_running` and never
            -- touched by the deferral write, so it stays fixed for the whole
            -- backoff window, the same way `launched_at` stays fixed for
            -- #1794's grace window. NULL for every row predating this column
            -- and for any entry that has never died — `_retry_backoff_reason`
            -- treats that identically to `attempts <= 0` (no backoff yet).
            retry_backoff_at REAL,
            -- #2604: operator override of `coord drive`'s `--max-fix-rounds`
            -- for THIS entry's tick-launched drive — see
            -- coord.drive_queue.effective_max_fix_rounds for the resolution
            -- order (this column, then pipeline.max_fix_rounds, then
            -- coord.drive_queue.DEFAULT_TICK_MAX_FIX_ROUNDS). NULL means "no
            -- per-entry override" — every row predating this column, and any
            -- entry enqueued without `--max-fix-rounds`, reads that way and
            -- falls through to the config/tick default exactly as if the
            -- column did not exist.
            max_fix_rounds INTEGER,
            -- #2589: operator override — this entry's tick-launched drive
            -- gets `coord drive --no-acceptance` (skip #1453's oracle-loop
            -- JIT slice authoring), same per-entry-passthrough shape as
            -- max_fix_rounds above. 0 for every row predating this column
            -- and for any entry enqueued without --no-acceptance, read
            -- identically to "no passthrough" by _launch_argv.
            no_acceptance INTEGER NOT NULL DEFAULT 0,
            UNIQUE(repo_name, issue_number)
        );

        -- #1630: daemon's aggregated view of each agent's /health payload
        -- (H-1's check-registry results, when the agent reports them) plus
        -- the poll's own reachability verdict.  One row per machine, always
        -- overwritten in place (`ON CONFLICT ... DO UPDATE`,
        -- coord/state.py `save_machine_health`) — this is a live snapshot,
        -- not a history. `received_at` is THIS daemon's clock at poll time,
        -- not anything the agent claims, so staleness detection (an agent
        -- that stops reporting must read `unknown`, never "still green")
        -- can never be fooled by a stopped agent's stale self-reported
        -- timestamp. `health_json` is NULL for an unreachable machine or an
        -- agent too old to report a health block — advisory only, never
        -- consulted by dispatch/routing/merge-queue code (see
        -- tests/test_health_advisory_only.py).
        CREATE TABLE IF NOT EXISTS machine_health (
            machine_name TEXT PRIMARY KEY,
            state        TEXT    NOT NULL,
            reason       TEXT    NOT NULL DEFAULT '',
            latency_ms   REAL,
            health_json  TEXT,
            received_at  REAL    NOT NULL
        );

        -- #2048: per-assignment tracking state for the cheap per-turn
        -- liveness auditor (coord/liveness_auditor.py). One row per
        -- assignment ever audited; consecutive_blocked is the running
        -- streak of BLOCKED verdicts (reset to 0 by any continue/done),
        -- and `raised` flips to 1 the one time the streak first reaches
        -- the configured strike count, so a stall is reported once rather
        -- than on every subsequent poll while the worker stays blocked.
        -- Read/written ONLY by coord.notify.detect_liveness_stall and
        -- coord.state's accessors below — never by anything that also
        -- touches Assignment.status/review_state/test_state (the auditor
        -- is a tripwire, not a gate).
        CREATE TABLE IF NOT EXISTS liveness_audits (
            assignment_id       TEXT PRIMARY KEY,
            consecutive_blocked INTEGER NOT NULL DEFAULT 0,
            last_audit_at       REAL,
            last_verdict        TEXT,
            raised              INTEGER NOT NULL DEFAULT 0
        );

        -- #1982 (epic #836): the coord-side half of the portal sync bridge —
        -- see coord/portal_sync.py for the loop that reads/writes these three
        -- tables and docs/CUSTOMER_PORTAL.md ("The sync bridge") for why the
        -- bridge is outbound-only.
        --
        -- OWNERSHIP, which is the whole reason there are two tables and not
        -- one: the portal is the SOLE WRITER of customer-authored facts
        -- (intake text, sign-off verdicts, answers) and coord is the SOLE
        -- WRITER of engineer-authored facts (status, design rounds,
        -- questions).  `portal_events` below is a read-only MIRROR of the
        -- former — nothing in coord ever pushes a row of it back — and
        -- `portal_outbox` is the queue of the latter.  Nothing is co-written,
        -- so there is no merge problem and no split-brain.

        -- Inbound mirror: one row per event pulled from
        -- `GET /api/bridge/pull`, keyed on the portal's own stable event id
        -- so a replay from a stale cursor is a no-op (`INSERT OR IGNORE`,
        -- coord/portal_store.py `record_events`).  The cursor only advances
        -- AFTER the page's rows commit, so a crash mid-page replays the page
        -- rather than skipping it — a submission made while the daemon was
        -- down queues, it does not vanish.  `payload_json` is the raw event
        -- verbatim: this table is an inbox, and parsing an event into
        -- coord-side work is a separate (downstream) concern that must be
        -- able to re-read the original.  `handled_at` is NULL until
        -- something downstream consumes the event.
        CREATE TABLE IF NOT EXISTS portal_events (
            event_id      TEXT    PRIMARY KEY,
            submission_id TEXT    NOT NULL DEFAULT '',
            kind          TEXT    NOT NULL DEFAULT '',
            occurred_at   TEXT    NOT NULL DEFAULT '',
            payload_json  TEXT    NOT NULL DEFAULT '{}',
            received_at   REAL    NOT NULL,
            handled_at    REAL
        );

        -- Outbound queue: one row per coord-owned fact waiting to be pushed.
        --
        -- `seq` is a DENSE per-submission FIFO order and `revision` the
        -- monotonic counter the portal dedupes on (its `applyUpdate`
        -- watermark ignores anything at or below what it already stored, so
        -- re-pushing an unconfirmed row is safe).  Both are allocated at
        -- ENQUEUE time and never rewritten, which is what makes a retry
        -- idempotent rather than a second write at a new revision.
        --
        -- `announces` is the ordering safety belt and the reason this is a
        -- queue instead of a direct call.  A status like `awaiting-signoff`
        -- does not merely display — the portal EMAILS the customer "your
        -- design is ready, go look at it".  Pushing it before the design
        -- round it announces lands the customer on an empty screen (measured
        -- in production on 2026-08-14, dogfood #835).  status and
        -- design_round are separate coord-owned fields and the portal
        -- enforces no ordering between them because it cannot — both are
        -- ours.  So an announcing row names the `requires_kind` row that
        -- must be CONFIRMED applied first, and coord/portal_sync.py refuses
        -- to send it until that is true.  '' for a row that announces
        -- nothing.
        CREATE TABLE IF NOT EXISTS portal_outbox (
            id            __AUTOPK_DDL__,
            submission_id TEXT    NOT NULL,
            seq           INTEGER NOT NULL,
            revision      INTEGER NOT NULL,
            kind          TEXT    NOT NULL,
            fields_json   TEXT    NOT NULL DEFAULT '{}',
            announces     TEXT    NOT NULL DEFAULT '',
            requires_kind TEXT    NOT NULL DEFAULT '',
            state         TEXT    NOT NULL DEFAULT 'pending',
            reason        TEXT    NOT NULL DEFAULT '',
            attempts      INTEGER NOT NULL DEFAULT 0,
            enqueued_at   REAL    NOT NULL,
            sent_at       REAL,
            UNIQUE(submission_id, seq)
        );

        -- Per-submission bookkeeping: the revision/seq allocators, plus what
        -- coord has CONFIRMED the portal applied (not what it hoped to
        -- send).  `design_round` is the highest round number confirmed
        -- applied, which is exactly what the `awaiting-signoff` guard above
        -- consults.  `customer_json` is the mirrored, read-only customer
        -- half — written only from pulled events, never pushed back.
        CREATE TABLE IF NOT EXISTS portal_submissions (
            submission_id TEXT    PRIMARY KEY,
            last_revision INTEGER NOT NULL DEFAULT 0,
            last_seq      INTEGER NOT NULL DEFAULT 0,
            last_status   TEXT    NOT NULL DEFAULT '',
            design_round  INTEGER NOT NULL DEFAULT 0,
            open_question TEXT    NOT NULL DEFAULT '',
            preview_url   TEXT    NOT NULL DEFAULT '',
            customer_json TEXT    NOT NULL DEFAULT '{}',
            first_seen_at REAL    NOT NULL,
            updated_at    REAL    NOT NULL
        );

        -- Single-row cursor + liveness bookkeeping for the bridge (the
        -- CHECK pins it to one row so "which cursor?" can never be a
        -- question).  `pull_cursor` is opaque to coord — it is whatever the
        -- portal handed back — and is the replay point on daemon restart.
        --
        -- `verdict_watermark_at` / `verdict_watermark_rowid` (#2509 review
        -- fix): the verdict consumer's OWN read position into
        -- `portal_events`, independent of the shared `handled_at` column.
        -- `unhandled_events()`/`mark_event_handled()` are shared plumbing —
        -- this consumer only stamps `handled_at` for a `changes_requested`
        -- event it actually dispatched, leaving every other kind (a new
        -- submission, an `approved` verdict, a Q&A answer) NULL by design so
        -- a future consumer can still read it.  Scanning by `handled_at IS
        -- NULL` therefore re-returns that growing, never-marked backlog
        -- ahead of anything newer forever once it exceeds one page — a real
        -- `changes-requested` event behind it would never surface.  A
        -- private watermark sidesteps that: it advances past every event
        -- this consumer has looked at, acted on or not, so a pile of
        -- non-actionable events cannot block the ones behind it.
        -- `(received_at, event_id)` mirrors `unhandled_events`'s own
        -- tiebreaker ordering. `verdict_watermark_rowid` held a SQLite
        -- `rowid` before #2723 (Phase C slice 5/7 of #1948); Postgres has no
        -- `rowid`, so coord/portal_store.py now stores `portal_events`' own
        -- primary key (`event_id`, TEXT) here instead — column name kept
        -- as-is, a rename is a separate schema migration this slice did not
        -- take on.
        -- Both NULL until the first tick with this column runs (an empty
        -- `~/.coord/coord.db` or one predating this migration), read as
        -- `(0.0, "")` — before every real event's `received_at`/`event_id`.
        CREATE TABLE IF NOT EXISTS portal_sync_state (
            id                    INTEGER PRIMARY KEY CHECK (id = 1),
            pull_cursor           TEXT,
            last_pull_at          REAL,
            last_push_at          REAL,
            last_heartbeat_at     REAL,
            last_error            TEXT NOT NULL DEFAULT '',
            verdict_watermark_at    REAL,
            verdict_watermark_rowid TEXT,
            question_watermark_at    REAL,
            question_watermark_rowid TEXT,
            -- #2987: the relayed-answer CONFIRMATION consumer's own read
            -- position — same shape and same reason as
            -- `question_watermark_at`/`question_watermark_rowid` just above
            -- (a private watermark, independent of `handled_at`, so the
            -- never-marked backlog every OTHER event kind leaves behind
            -- cannot starve this consumer of a `relayed_answer.confirmed`
            -- event newer than it). See
            -- `coord.portal_sync._consume_relayed_answer_confirmations`.
            relayed_answer_watermark_at    REAL,
            relayed_answer_watermark_rowid TEXT
        );

        -- #2749 (IL-3, epic #2746): the running-context ledger — the record
        -- half of the four-layer store issue #2749's design section
        -- describes ("Ledger / Decisions / Narrative / Briefing"; not yet
        -- folded into `docs/CUSTOMER_PORTAL.md`). Three tables, one per
        -- durable layer (the fourth, Briefing, is
        -- rendered on demand from the other three — `coord portal ledger`
        -- — and owns no storage of its own).
        --
        -- `portal_ledger`: append-only, immutable, VERBATIM — everything
        -- coord observes without asking an agent: a question exactly as it
        -- was actually pushed, an answer exactly as it was actually
        -- received. Never edited after insert; a correction is a new row,
        -- not an UPDATE. `kind` is open-ended (only `question_pushed` /
        -- `question_answered` are written as of this migration —
        -- `coord.portal_store.mark_applied`'s `kind == "question"` branch
        -- and `coord.portal_sync._consume_questions` respectively) so a
        -- later consumer (a dispatch record, a verdict) can extend it
        -- without a schema change. `seq` is a dense per-submission sequence
        -- (mirrors `portal_outbox.seq`) so a ledger read is stably ordered
        -- without depending on `id`/`rowid`, which #2723 established is not
        -- portable. `question_revision` pairs an answer back to the
        -- `portal_outbox` row (by its `revision`) that asked it — NULL for
        -- a kind that isn't part of a Q&A pair. `source_event_id` is the
        -- originating `portal_events.event_id` for a row derived from a
        -- pulled event (`question_answered`) — NULL (not '') for a row
        -- that isn't, e.g. `question_pushed`, so `UNIQUE(submission_id,
        -- kind, source_event_id)` only actually dedupes the rows that need
        -- it: SQL's NULL != NULL means any number of NULL-source rows for
        -- the same submission/kind coexist freely, while a genuine replay
        -- of the same pulled event (a crash between this insert and
        -- `mark_event_handled`, #2749) is a harmless no-op via
        -- `INSERT OR IGNORE` against that same constraint. `payload_json`
        -- keeps the full observed record for anything the typed columns
        -- above don't capture.
        CREATE TABLE IF NOT EXISTS portal_ledger (
            id                __AUTOPK_DDL__,
            submission_id     TEXT    NOT NULL,
            seq               INTEGER NOT NULL,
            kind              TEXT    NOT NULL,
            question_revision INTEGER,
            text              TEXT    NOT NULL DEFAULT '',
            actor             TEXT    NOT NULL DEFAULT '',
            source_event_id   TEXT,
            payload_json      TEXT    NOT NULL DEFAULT '{}',
            recorded_at       REAL    NOT NULL,
            UNIQUE(submission_id, seq),
            UNIQUE(submission_id, kind, source_event_id)
        );

        -- `portal_decisions`: append-only ROWS with a mutable `state` —
        -- unlike the ledger above, a decision's `state` legitimately
        -- transitions in place (`proposed` -> `confirmed`, or ->
        -- `superseded` / `rejected`) via UPDATE; what never happens is a
        -- row being deleted or its `text` rewritten, so the history of
        -- what was ever proposed is never lost even once superseded or
        -- rejected. `reason` is required (enforced in
        -- `coord.portal_store`, not here) when `state = 'rejected'` — a
        -- rejection with no reason is exactly the "re-litigated in a later
        -- iteration" failure mode #2749 exists to close.
        -- `superseded_by_seq` names the replacement decision's own `seq`
        -- once this row is superseded; NULL otherwise. `actor` is who
        -- proposed/transitioned it (an agent session id, or an operator's
        -- username for a hand-run `coord portal decision`).
        CREATE TABLE IF NOT EXISTS portal_decisions (
            id                 __AUTOPK_DDL__,
            submission_id      TEXT    NOT NULL,
            seq                INTEGER NOT NULL,
            text               TEXT    NOT NULL,
            state              TEXT    NOT NULL DEFAULT 'proposed',
            reason             TEXT    NOT NULL DEFAULT '',
            superseded_by_seq  INTEGER,
            actor              TEXT    NOT NULL DEFAULT '',
            recorded_at        REAL    NOT NULL,
            updated_at         REAL    NOT NULL,
            UNIQUE(submission_id, seq)
        );

        -- `portal_narrative`: the regenerable, disposable layer — one row
        -- per submission, always OVERWRITTEN wholesale, never appended to
        -- and never read back as an input to its own next generation (that
        -- is the property that makes a wrong narrative merely regenerable
        -- rather than, like a bad ledger row, unrecoverable). No history is
        -- kept on purpose.
        CREATE TABLE IF NOT EXISTS portal_narrative (
            submission_id TEXT    PRIMARY KEY,
            text          TEXT    NOT NULL DEFAULT '',
            actor         TEXT    NOT NULL DEFAULT '',
            recorded_at   REAL    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_assignments_status ON assignments(status);
        CREATE INDEX IF NOT EXISTS idx_assignments_machine ON assignments(machine_name);
        CREATE INDEX IF NOT EXISTS idx_merge_queue_state ON merge_queue(state);
        CREATE INDEX IF NOT EXISTS idx_issue_context_issue
            ON issue_context(repo_name, issue_number);
        CREATE INDEX IF NOT EXISTS idx_audit_log_ts ON audit_log(ts);
        CREATE INDEX IF NOT EXISTS idx_audit_log_assignment ON audit_log(assignment_id);
        CREATE INDEX IF NOT EXISTS idx_issue_comments_issue
            ON issue_comments(repo_name, issue_number);
        CREATE INDEX IF NOT EXISTS idx_drive_escalations_issue
            ON drive_escalations(repo_name, issue_number);
        CREATE INDEX IF NOT EXISTS idx_drive_queue_state
            ON drive_queue(state, position);
        -- #1982: the push loop's hot read is "pending rows, oldest first,
        -- grouped by submission" — see coord.portal_store.pending_outbox.
        CREATE INDEX IF NOT EXISTS idx_portal_outbox_pending
            ON portal_outbox(state, submission_id, seq);
        CREATE INDEX IF NOT EXISTS idx_portal_events_submission
            ON portal_events(submission_id);
        -- #2749: `coord portal ledger`'s hot read is "every row for one
        -- submission, in seq order" — see coord.portal_store.ledger_for_submission
        -- / decisions_for_submission.
        CREATE INDEX IF NOT EXISTS idx_portal_ledger_submission
            ON portal_ledger(submission_id, seq);
        CREATE INDEX IF NOT EXISTS idx_portal_decisions_submission
            ON portal_decisions(submission_id, seq);
"""


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create all tables and indexes if they don't already exist.

    ``_SCHEMA_SQL``'s 8 auto-incrementing primary key columns are written
    as the dialect-neutral sentinel ``__AUTOPK_DDL__`` rather than literal
    ``INTEGER PRIMARY KEY AUTOINCREMENT`` text (#2724) -- the actual DDL
    fragment for *conn*'s dialect comes from
    :func:`coord.sql.autoincrement_pk_ddl`, so this module names no
    backend-specific schema syntax itself.
    """
    _fix_schema_version_table(conn)
    dialect = sql.detect_dialect(conn)
    pk_ddl = sql.autoincrement_pk_ddl(dialect)
    schema_sql = _SCHEMA_SQL.replace("__AUTOPK_DDL__", pk_ddl)
    sql.executescript(conn, schema_sql)
    conn.commit()
    # Column-level migrations for existing databases.  SQLite does not support
    # "ADD COLUMN IF NOT EXISTS", so we catch OperationalError instead.
    _migrate_add_columns(conn)
    _set_schema_version(conn, _DB_SCHEMA_VERSION)


# #2709: module-level (not a local inside _migrate_add_columns below) so
# tests/test_db.py's TestMigrateAddColumnsVersionGuard can inspect its
# length without executing any SQL. `len(_MIGRATE_ADD_COLUMNS)` is pinned
# there alongside `_DB_SCHEMA_VERSION` — appending an entry here changes
# the pinned count and fails that test red, forcing this list and the
# version bump above to be edited in the same commit. See that test's
# docstring, and the _DB_SCHEMA_VERSION comment above, for why a plain
# code comment already failed to stop this three times (#2604, #2589,
# #2687).
_MIGRATE_ADD_COLUMNS: list[str] = [
    "ALTER TABLE assignments ADD COLUMN review_iteration INTEGER DEFAULT 0",
    "ALTER TABLE assignments ADD COLUMN review_posted_at REAL",
    # #200: human-driven Test gate between Work and Review.
    "ALTER TABLE assignments ADD COLUMN test_state TEXT",
    "ALTER TABLE assignments ADD COLUMN test_reason TEXT",
    # #253: persisted adversarial-review verdict so the merge gate can
    # check approval without re-parsing logs after restart.
    "ALTER TABLE assignments ADD COLUMN review_verdict TEXT",
    # #208: worker cost captured from the final stream-json result event.
    "ALTER TABLE assignments ADD COLUMN cost_usd REAL",
    # #252: worker-emitted smoke-test list (JSON array of strings;
    # NULL = not emitted, '[]' = explicit "(none — internal)").
    "ALTER TABLE assignments ADD COLUMN smoke_tests TEXT",
    # #bounce: cached review-findings body (markdown text) so coord
    # bounce + the upcoming per-stage display don't have to re-fetch
    # the review log from the agent.  Populated by notify.py when
    # the review is first parsed.  NULL = not yet parsed; populated
    # = full findings.body text.
    "ALTER TABLE assignments ADD COLUMN review_findings TEXT",
    # #315: claude session ID captured from the worker's `system.init`
    # event.  Set by notify.py after the agent reports completion.  Used
    # by `coord chat-continue` to pass `--resume <id>` to the next
    # worker so it loads the prior conversation and continues it.
    "ALTER TABLE assignments ADD COLUMN claude_session_id TEXT",
    # #342 Phase A: AI-generated smoke test plan (JSON-encoded).
    # NULL = not yet generated.  Set by `coord test-plan` and read back
    # by the CLI (cache hit) and eventually by the TUI (Phase B).
    "ALTER TABLE assignments ADD COLUMN test_plan TEXT",
    # #349 Phase B: branch HEAD SHA at the time `coord test-plan` last ran.
    # Used by the TUI to detect staleness: if the branch has advanced
    # since the plan was generated, it re-runs `coord test-plan --refresh`
    # automatically.  NULL = plan not yet generated, or generated without
    # branch tracking (legacy).  Always reset to NULL when set_test_plan
    # is called with branch_head=None so no stale SHA persists.
    "ALTER TABLE assignments ADD COLUMN test_plan_branch_head TEXT",
    # #406 Phase A: milestone columns on the issues table.
    # milestone_number is the GitHub milestone number (integer id); NULL for
    # unassigned.  milestone_title is the human-readable name (e.g. "v0.5");
    # NULL when no milestone is assigned.  Idempotent — SQLite raises
    # OperationalError when the column already exists, which is swallowed
    # below.
    "ALTER TABLE issues ADD COLUMN milestone_number INTEGER",
    "ALTER TABLE issues ADD COLUMN milestone_title TEXT",
    # #324: resolved provider name recorded at dispatch time so the TUI
    # can surface it in the assignment detail panel (#327).  NULL for rows
    # dispatched before #324 landed; the TUI shows "claude" as the
    # implicit default when the column is NULL.
    "ALTER TABLE assignments ADD COLUMN provider_name TEXT",
    # #546: token counts for automated (claude -p) assignments, parsed from
    # the final stream-json result event alongside cost_usd.  All default
    # 0 so the TUI can sum them without NULLs.  Interactive (Max/OAuth)
    # sessions do not bill per-token; those rows stay at 0 and the TUI
    # labels them "Max (subscription)" rather than showing a dollar figure.
    "ALTER TABLE assignments ADD COLUMN input_tokens INTEGER DEFAULT 0",
    "ALTER TABLE assignments ADD COLUMN output_tokens INTEGER DEFAULT 0",
    "ALTER TABLE assignments ADD COLUMN cache_creation_tokens INTEGER DEFAULT 0",
    "ALTER TABLE assignments ADD COLUMN cache_read_tokens INTEGER DEFAULT 0",
    # #546: track whether an assignment ran as a human-attended interactive
    # session (Max/Pro subscription).  Used by the TUI to show
    # "Max (subscription)" accurately without misidentifying old automated
    # rows that also lack cost_usd + token data.
    "ALTER TABLE assignments ADD COLUMN is_interactive INTEGER DEFAULT 0",
    # #618: short human-readable reason for a launch failure (e.g.
    # "branch already checked out at ~/.coord/worktrees/<old-aid>").
    # Written by the CLI immediately when an interactive session can't
    # start so the TUI can explain the red box without any log file.
    # NULL for assignments that launched successfully.
    "ALTER TABLE assignments ADD COLUMN failure_reason TEXT",
    # #776 (Merge Queue v2-A): track when an entry was added to the queue
    # so the merge plan can display age and sort stably.  NULL for entries
    # created before this migration ran.
    "ALTER TABLE merge_queue ADD COLUMN enqueued_at REAL",
    # #821: commit-bound review gate — SHA of the branch HEAD at the time
    # the review assignment ran.  When both this column and the merge-queue
    # entry's branch_head_sha are populated and differ, `has_approved_review`
    # treats the approval as stale (new commits since the review → re-review
    # required).  NULL for rows predating this feature.
    "ALTER TABLE assignments ADD COLUMN review_head_sha TEXT",
    # #944: the Acceptance-gate verdict (oracle loop, docs/ORACLE_LOOP.md)
    # — set by `coord acceptance record --issue N --sha <sha>`, the
    # coordinator's external re-run of the sealed suite against the
    # pushed SHA. NULL until a record has run. acceptance_reason carries
    # a short failing-test summary (mirrors test_reason); acceptance_sha
    # is the exact commit the verdict was recorded against, so staleness
    # (new commits since the last record) is detectable the same way
    # review_head_sha detects a stale review approval.
    "ALTER TABLE assignments ADD COLUMN acceptance_state TEXT",
    "ALTER TABLE assignments ADD COLUMN acceptance_reason TEXT",
    "ALTER TABLE assignments ADD COLUMN acceptance_sha TEXT",
    # #932: per-test counts alongside acceptance_state, so the Acceptance
    # box can render "3/7 acceptance green" instead of a bare verdict.
    "ALTER TABLE assignments ADD COLUMN acceptance_total INTEGER",
    "ALTER TABLE assignments ADD COLUMN acceptance_passed INTEGER",
    # #874: persist the worker's ### Summary prose block so the TUI's
    # Summary tab has a durable, board-sourced field.  NULL when the
    # worker emitted no summary (best-effort; never blocks completion).
    "ALTER TABLE assignments ADD COLUMN completion_summary TEXT",
    # #886 Phase 2: structured Milestone Outcome Audit verdict. Written by
    # issue_store._persist_audit_result for type="audit" assignments only
    # (the epic's issue_number doubles as the audit's issue_number — see
    # #885's _dispatch_audit_of). audit_goals_json is a JSON array of
    # {goal, metric_before, metric_after, verdict (met|partial|gap),
    # evidence} — kept as a raw JSON string on the wire, same convention
    # as review_findings above. audit_run_number increments once per
    # `--audit-of <epic>` run against the same (repo_name, issue_number)
    # so later runs can diff against earlier ones. NULL for every row
    # predating this feature and for non-audit assignment types.
    "ALTER TABLE assignments ADD COLUMN audit_goals_json TEXT",
    "ALTER TABLE assignments ADD COLUMN audit_bottom_line TEXT",
    "ALTER TABLE assignments ADD COLUMN audit_run_number INTEGER",
    # #1077: the originating assignment's `type` (e.g. "work",
    # "mock-author"), so the merge processor can tell whether merging
    # this entry's PR should deterministically close `issue_number` —
    # see coord.models.CLOSES_ISSUE_TYPES. Existing rows default to
    # 'work', preserving the prior always-close behavior for entries
    # enqueued before this column existed.
    "ALTER TABLE merge_queue ADD COLUMN assignment_type TEXT DEFAULT 'work'",
    # #1084: for type="test-author" JIT-mode assignments, the work-order
    # member issue this dispatch is extending the acceptance suite for —
    # see coord.models.Assignment.for_issue_number. NULL for milestone-
    # mode (Gate A) authoring, every other assignment type, and rows
    # predating this column.
    "ALTER TABLE assignments ADD COLUMN for_issue_number INTEGER",
    # #1213: snapshot of the originating assignment's resolved
    # required_gates (JSON array; NULL/'[]' = no per-issue override —
    # callers fall back to config.pipeline.default_gates), captured at
    # enqueue time so the review/smoke merge gates are commit-bound
    # rather than re-resolved from the live board at merge time. NULL
    # for rows predating this column, which fall back the same way.
    "ALTER TABLE merge_queue ADD COLUMN required_gates TEXT",
    # #1456: audit trail for a coordinator override of a reviewer's verdict
    # (the #476 approve-with-nits gate).  `review_verdict_original` holds
    # the reviewer's own verdict and `review_verdict_override_reason` the
    # parsed counts that justified the override; `review_verdict` keeps the
    # effective value the merge gate reads.  NULL for every row where the
    # coordinator never overrode anything, and for rows predating this
    # column — see coord.models.Assignment.review_verdict_original.
    "ALTER TABLE assignments ADD COLUMN review_verdict_original TEXT",
    "ALTER TABLE assignments ADD COLUMN review_verdict_override_reason TEXT",
    # #1475: content-addressed fingerprint of the diff a review approved
    # (`git patch-id --stable`), captured alongside `review_head_sha`. A
    # rebase that changes no content produces the same patch-id even
    # though the branch's HEAD SHA moved, so `has_approved_review` can
    # carry the approval forward instead of re-blocking on the #821 SHA
    # check. NULL for rows predating this column or where the patch-id
    # could not be computed — those fall back to today's SHA-only
    # staleness behaviour (fail closed).
    "ALTER TABLE assignments ADD COLUMN review_patch_id TEXT",
    # #1479: Test-gate staleness anchor, captured (best-effort) alongside
    # a terminal test_state write in
    # `coord.state._record_test_verdict_local`. `test_head_sha` /
    # `test_patch_id` mirror `review_head_sha` / `review_patch_id`
    # (#821/#1475) for the branch's own content; `test_base_sha` is the
    # NEW piece — the target/merge branch's HEAD SHA at test time, so
    # `coord.merge_queue.has_smoke_verdict` can detect a base that moved
    # out from under an otherwise content-identical branch (a rebase can
    # break tests without changing the branch's own diff). NULL for rows
    # predating this column or where the anchor couldn't be computed —
    # those fall back to today's no-staleness-check behavior (fail open).
    "ALTER TABLE assignments ADD COLUMN test_head_sha TEXT",
    "ALTER TABLE assignments ADD COLUMN test_patch_id TEXT",
    "ALTER TABLE assignments ADD COLUMN test_base_sha TEXT",
    # #1476: audit trail for a SCOPED re-review — dispatched when a
    # conflict-fix rebase changed content under an already-approved
    # review (patch-id mismatch) but no other work/fix commit
    # intervened. `review_scoped` marks the review row as scoped (vs. a
    # full re-review of the whole PR); `review_scope_base_sha` records
    # the prior review's `review_head_sha` — the commit the resolution
    # delta was computed from — so a later audit can distinguish
    # "reviewed the 15-line resolution" from "reviewed the whole PR".
    # 0/NULL for every row predating this column and for ordinary
    # (non-scoped) reviews.
    "ALTER TABLE assignments ADD COLUMN review_scoped INTEGER DEFAULT 0",
    "ALTER TABLE assignments ADD COLUMN review_scope_base_sha TEXT",
    # #1499: durable provenance — `f"drive:{repo}#{issue}"` when this
    # assignment was dispatched by `coord drive` (via `coord assign
    # --driven-by`), NULL for a hand `coord assign` and for every row
    # predating this column. This is the piece that survives the
    # driver process exiting — see coord.models.Assignment.driven_by.
    "ALTER TABLE assignments ADD COLUMN driven_by TEXT",
    # #1629 (H-2): the toolchain string that produced a Test-gate
    # verdict — e.g. "rustc 1.95.0" or "python 3.12.4, node 20.11.0" —
    # captured (best-effort) alongside `test_state` in
    # `coord.state._record_test_verdict_local`. Annotation only: nothing
    # reads this to gate dispatch/review/merge (see
    # coord.health.checks.toolchain's fleet_toolchain_skew, which is the
    # thing that actually judges skew). NULL for every row predating
    # this column and for any verdict recorded without a resolvable
    # toolchain — a historical/unknown value, never treated as a mismatch.
    "ALTER TABLE assignments ADD COLUMN test_toolchain TEXT",
    # #1757 (deploy gates): mark a queue entry so that when it completes,
    # the tick STOPS launching and waits for a human deploy step —
    # `merged != live` is this repo's most-repeated operational lesson and
    # a queue that models merge but not deploy sequences work straight into
    # it, unattended.  DQ-1 shipped before this, so an existing
    # ~/.coord/coord.db is upgraded in place here (the same columns are in
    # `_ensure_schema`'s CREATE for fresh databases; the duplicate-column
    # OperationalError is swallowed below).
    #
    #   hold_after   0/1 — this entry ends with a deploy gate
    #   hold_reason  shown to the operator when the gate fires
    #   resume_when  optional probe command; '' = manual resume only
    #   hold_state   ''|armed|fired|released (coord/drive_queue.py)
    #   hold_probes  consecutive failed `resume_when` runs since the gate
    #                fired — a TYPED count, so the alert's rising attempt
    #                number never has to be parsed back out of prose
    #                (#1523 §2: typed state, never CLI prose).
    "ALTER TABLE drive_queue ADD COLUMN hold_after INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE drive_queue ADD COLUMN hold_reason TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE drive_queue ADD COLUMN resume_when TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE drive_queue ADD COLUMN hold_state TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE drive_queue ADD COLUMN hold_probes INTEGER NOT NULL DEFAULT 0",
    # #1870: the short hostname of the machine that actually launched this
    # entry's `coord drive --tmux` — see the CREATE TABLE comment above.
    # '' for every row written before this migration; `_reconcile_running`
    # treats that exactly like "launched here" (today's behaviour).
    "ALTER TABLE drive_queue ADD COLUMN launch_host TEXT NOT NULL DEFAULT ''",
    # #1892: count of automatic `CiStore.rerun_for_pr` calls `merge_queue
    # .process()` has issued for this entry's current verdictless-CI-
    # failure streak — see `coord.merge_queue.MAX_CI_INFRA_RERUNS` and
    # `QueuedMerge.ci_infra_reruns`'s docstring. 0 for every row
    # predating this column (no auto-reruns spent yet), same as the
    # column's own default for freshly-enqueued entries.
    "ALTER TABLE merge_queue ADD COLUMN ci_infra_reruns INTEGER NOT NULL DEFAULT 0",
    # #2197: the CI-staleness auto-rerun's OWN counter — see
    # `coord.merge_queue.MAX_CI_STALE_RERUNS` and
    # `QueuedMerge.ci_stale_reruns`'s docstring for why it is kept
    # separate from `ci_infra_reruns` rather than sharing it. 0 for
    # every row predating this column, same as a freshly-enqueued entry.
    "ALTER TABLE merge_queue ADD COLUMN ci_stale_reruns INTEGER NOT NULL DEFAULT 0",
    # #1956: verdict provenance — WHO recorded `review_verdict` and HOW
    # (see coord.models.Assignment.verdict_source for the three values
    # and why conflating them was the second half of #1956). NULL for
    # every row predating this column and for the common case (the
    # reviewer's own log was parsed) — callers treat NULL identically to
    # "agent".
    "ALTER TABLE assignments ADD COLUMN verdict_source TEXT",
    "ALTER TABLE assignments ADD COLUMN verdict_source_reason TEXT",
    # #2133: see the CREATE TABLE comment above — capture time of
    # `drive_queue.last_reason`, so a `blocked` entry's displayed reason
    # carries its age instead of rendering a stale snapshot as current
    # state. NULL for every row predating this migration.
    "ALTER TABLE drive_queue ADD COLUMN reason_at REAL",
    # #2186: deploy-gate SCOPE — see the CREATE TABLE comment above and
    # coord/drive_queue.py's HOLD_SCOPE_ENTRY/HOLD_SCOPE_FLEET. Every row
    # predating this migration (and every armed-but-not-yet-fired gate)
    # defaults to 'entry', the narrower reading — a fired gate on such a
    # row holds only its own dependents rather than the whole queue, which
    # is the #2186 fix itself, not just a schema detail.
    "ALTER TABLE drive_queue ADD COLUMN hold_scope TEXT NOT NULL DEFAULT 'entry'",
    # #2230: see the CREATE TABLE comment above — count of times the
    # blocked-reconciliation sweep has auto-resumed a row from 'blocked'.
    # 0 for every row predating this migration, same as the column's own
    # default for a freshly-enqueued entry.
    "ALTER TABLE drive_queue ADD COLUMN resumes INTEGER NOT NULL DEFAULT 0",
    # #2252: the flake-recheck auto-rerun's OWN counter/pending-state —
    # see `coord.merge_queue.MAX_CI_FLAKY_RERUNS` and
    # `QueuedMerge.ci_flaky_reruns`/`ci_flaky_pending`'s docstrings for
    # why they're kept separate from `ci_infra_reruns`/`ci_stale_reruns`
    # above rather than sharing either. 0/'' for every row predating
    # this migration — no flake re-run ever spent/pending — same as the
    # columns' own defaults for a freshly-enqueued entry.
    "ALTER TABLE merge_queue ADD COLUMN ci_flaky_reruns INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE merge_queue ADD COLUMN ci_flaky_pending TEXT NOT NULL DEFAULT ''",
    # #2273 (post-review): see the CREATE TABLE comment above — a
    # backoff-window anchor the deferral's own per-tick status write
    # never touches, fixing the "moving target" bug where re-stamping
    # `reason_at` every backing-off tick reset the very clock it fed.
    # NULL for every row predating this migration, same as the column's
    # own default and identical in effect to `attempts <= 0` (no backoff
    # window active).
    "ALTER TABLE drive_queue ADD COLUMN retry_backoff_at REAL",
    # #2316: the worker's own terminal `stop_reason` (e.g. `"end_turn"`,
    # opencode's `"length"`, claude's `"max_tokens"`) — see
    # `coord.agent.AgentServer._reap`'s `/status` `completed` entry
    # (already sent by every agent build) and `coord.reconcile._capture_
    # stop_reason_best_effort`, which persists it here for EVERY terminal
    # assignment, not just failed ones. Previously this value reached the
    # agent's own `/status` response and nothing else — `coord gates`,
    # `coord status` and the dashboard had no column to read it from and
    # a truncated (`stop_reason == "length"`/`"max_tokens"`), 0-commit
    # run was recorded `advisory` with no trace of WHY. NULL for every
    # row predating this migration and for a non-stream-json / PTY
    # worker whose log carries no such field.
    "ALTER TABLE assignments ADD COLUMN stop_reason TEXT",
    # #2347: count of automatic checks `merge_queue.process()` has made
    # against this entry's CURRENT streak of bare check-list FETCH
    # failures (GitHub unreachable, not a real CI verdict) — see
    # `coord.merge_queue.MAX_CI_UNREADABLE_RERUNS` and
    # `QueuedMerge.ci_unreadable_reruns`'s docstring. 0 for every row
    # predating this column, same as the column's own default for a
    # freshly-enqueued entry.
    "ALTER TABLE merge_queue ADD COLUMN ci_unreadable_reruns INTEGER NOT NULL DEFAULT 0",
    # #2359: the preview-approval gate's coord-owned preview URL, mirroring
    # `design_round` — the highest-confirmed preview build's URL, written
    # only by `coord.portal_store.mark_applied`'s `kind == "preview"`
    # branch. '' for every row predating this migration and for a
    # submission with no preview queued yet, same as `open_question`'s
    # own default.
    "ALTER TABLE portal_submissions ADD COLUMN preview_url TEXT NOT NULL DEFAULT ''",
    # #2417: the CALLING assignment's id when this row was dispatched by
    # a `coord` subcommand run from INSIDE another worker's own turn
    # (e.g. `coord acceptance author`/`coord fix` shelled out to from a
    # `type="work"` session's own bash tool) rather than typed by a
    # human — see `coord.models.Assignment.dispatched_by_assignment_id`.
    # NULL for a hand dispatch, a coordinator/brain-proposed dispatch,
    # and every row predating this column.
    "ALTER TABLE assignments ADD COLUMN dispatched_by_assignment_id TEXT",
    # #2510: count of `coord.ci_fix.dispatch_ci_fix` fix-worker dispatches
    # issued for this entry's CURRENT confirmed (non-infra, non-first-
    # flake) `checks_failed` streak — see `QueuedMerge.ci_fix_dispatches`'s
    # docstring and `coord.ci_fix.MAX_CI_FIX_DISPATCHES`. 0 for every row
    # predating this migration, same as the column's own default for a
    # freshly-enqueued entry.
    "ALTER TABLE merge_queue ADD COLUMN ci_fix_dispatches INTEGER NOT NULL DEFAULT 0",
    # #3011: durable snapshot of `QueuedMerge.branch_head_sha` taken at the
    # moment `coord.ci_fix.dispatch_ci_fix` last dispatched a fix worker —
    # unlike `branch_head_sha` itself (recomputed from GitHub every tick,
    # never persisted), this survives to the NEXT tick so
    # `coord.ci_fix.dispatch_was_noop` can tell whether that leg actually
    # moved the branch. '' for every row predating this migration and for
    # an entry with no ci-fix dispatch currently unaccounted-for.
    "ALTER TABLE merge_queue ADD COLUMN ci_fix_head_sha TEXT NOT NULL DEFAULT ''",
    # #3011: count of CONSECUTIVE ci-fix legs that completed with the branch
    # HEAD unchanged (a worker correctly declined and pushed no commit) —
    # kept separate from `ci_fix_dispatches` so a no-op leg can be refunded
    # (doesn't count toward `MAX_CI_FIX_DISPATCHES`) while still bounding
    # how many times that can happen before escalating to HUMAN_REQUIRED
    # with a distinct "not attributable to this branch" reason — see
    # `coord.ci_fix.MAX_CI_FIX_NOOP_STREAK`. 0 for every row predating this
    # migration and for an entry that has never had a no-op ci-fix leg.
    "ALTER TABLE merge_queue ADD COLUMN ci_fix_noop_streak INTEGER NOT NULL DEFAULT 0",
    # #2509 review fix: the verdict consumer's own read position into
    # `portal_events` — see the CREATE TABLE comment above for why it
    # cannot reuse the shared `handled_at` column. NULL (read as
    # `(0.0, "")`, before every real event) for every database predating
    # this migration, which replays the full existing inbox exactly once
    # on upgrade — safe: re-scanning a non-actionable event is a no-op,
    # and `_consume_verdicts` skips (never re-dispatches) any event
    # whose `handled_at` is already set, so an already-consumed
    # `changes_requested` event from before this migration is walked
    # past, not re-sent — see `coord.portal_sync._consume_verdicts`.
    # `verdict_watermark_rowid` is TEXT (not INTEGER) despite the name:
    # #2723 (Phase C slice 5/7 of #1948) repointed it at
    # `portal_events.event_id`, a TEXT primary key that is frequently
    # non-numeric (`_synthetic_event_id` mints `sha256:<hash>` ids) — an
    # INTEGER column would reject those outright under Postgres.
    "ALTER TABLE portal_sync_state ADD COLUMN verdict_watermark_at REAL",
    "ALTER TABLE portal_sync_state ADD COLUMN verdict_watermark_rowid TEXT",
    # #2604: see the CREATE TABLE comment above — a per-entry
    # `--max-fix-rounds` override for the tick's `coord drive --tmux`
    # launch. NULL for every row predating this migration, read
    # identically to "no override" by `coord.drive_queue.
    # effective_max_fix_rounds`.
    "ALTER TABLE drive_queue ADD COLUMN max_fix_rounds INTEGER",
    # #2589: see the CREATE TABLE comment above — a per-entry
    # `--no-acceptance` passthrough for the tick's `coord drive --tmux`
    # launch. 0 (no passthrough) for every row predating this migration.
    "ALTER TABLE drive_queue ADD COLUMN no_acceptance INTEGER NOT NULL DEFAULT 0",
    # #2687: the pre-merge UAT (User Acceptance Test) gate's human
    # verdict — see coord.models.Assignment.uat_state/uat_reason and
    # `coord uat <id> --passed|--failed`. NULL for every row predating
    # this column and for every repo that hasn't opted in via
    # `Repo.uat_preview` (`coord.merge_queue.requires_uat` no-ops on a
    # NULL `uat_preview` regardless of this column).
    "ALTER TABLE assignments ADD COLUMN uat_state TEXT",
    "ALTER TABLE assignments ADD COLUMN uat_reason TEXT",
    # #2749 (IL-3): the `question.answered` consumer's own read position
    # into `portal_events` — same reason and same shape as
    # `verdict_watermark_at`/`verdict_watermark_rowid` above (a private
    # watermark independent of `handled_at`, so the never-marked backlog
    # every OTHER event kind leaves behind cannot starve this consumer of
    # events newer than it — see `coord.portal_sync._consume_questions`).
    # NULL (read as `(0.0, "")`, before every real event) for every
    # database predating this migration, which replays the full existing
    # inbox exactly once on upgrade — safe, for the same reason replaying
    # it is safe for the verdict consumer: `append_ledger_entry`'s
    # `(submission_id, kind, source_event_id)` dedupe makes a re-observed
    # `question_answered` event a no-op rather than a duplicate ledger row.
    "ALTER TABLE portal_sync_state ADD COLUMN question_watermark_at REAL",
    "ALTER TABLE portal_sync_state ADD COLUMN question_watermark_rowid TEXT",
    # #2987: the relayed-answer confirmation consumer's own read position
    # into `portal_events` — same reason and same shape as
    # `question_watermark_at`/`question_watermark_rowid` above. NULL (read
    # as `(0.0, "")`, before every real event) for every database predating
    # this migration, which replays the full existing inbox exactly once on
    # upgrade — safe for the same reason replaying it is safe for the
    # question consumer: `append_ledger_entry`'s `(submission_id, kind,
    # source_event_id)` dedupe makes a re-observed event a no-op rather than
    # a duplicate ledger row. See
    # `coord.portal_sync._consume_relayed_answer_confirmations`.
    "ALTER TABLE portal_sync_state ADD COLUMN relayed_answer_watermark_at REAL",
    "ALTER TABLE portal_sync_state ADD COLUMN relayed_answer_watermark_rowid TEXT",
    # #2786: worker-reported turn count, parsed off the final stream-json
    # `result` event into `WorkerSummary.num_turns` (coord/worker_events.py)
    # alongside the four token columns above, but never persisted until now.
    # Cache-read cost is context-size × turns-per-leg — without this column
    # there is no way to tell "long context" from "many turns" apart, which
    # is exactly what made ~66% of `work`-leg spend unmeasurable. Default 0,
    # same convention as the token columns: an interactive/Max session or a
    # row predating this migration reads as 0, not NULL, so `coord usage`
    # can sum it without a None-check.
    "ALTER TABLE assignments ADD COLUMN num_turns INTEGER DEFAULT 0",
]


def _migrate_add_columns(conn: sqlite3.Connection) -> None:
    """Add new columns to existing databases via ALTER TABLE.

    Safe to call on databases that already have the columns — the driver
    error raised for a duplicate column (SQLite's ``OperationalError``;
    Postgres's equivalent under ``sql.driver_errors()``, #2784) is silently
    swallowed.

    #2982: Postgres aborts the whole transaction on a failed statement, so
    without a ``conn.rollback()`` here the *first* duplicate ALTER (every
    entry in ``_MIGRATE_ADD_COLUMNS`` is one, against a freshly-created
    schema) leaves the connection unusable -- every statement after it,
    including the remaining ALTERs and ``_set_schema_version``'s own
    ``DELETE FROM schema_version``, fails with
    ``psycopg.errors.InFailedSqlTransaction``. SQLite has no such concept
    (a failed statement there doesn't touch the transaction), so the
    rollback is a no-op on that backend -- and by this point in
    ``_ensure_schema`` there is nothing uncommitted to lose on either
    backend: the schema script already committed, and each successful ALTER
    in this loop commits immediately, so a rollback can only ever discard
    the one statement that just failed.
    """
    for ddl in _MIGRATE_ADD_COLUMNS:
        try:
            sql.execute(conn, ddl)
            conn.commit()
        except sql.driver_errors():
            conn.rollback()  # Column already exists; Postgres needs the tx cleared


# ── JSON migration ─────────────────────────────────────────────────────────────

def _maybe_migrate_json(conn: sqlite3.Connection) -> None:
    """Migrate old JSON files to SQLite if dispatched.json exists and DB is empty.

    The ``json_migrated`` marker in board_meta is checked first.  Once set it
    persists forever, so migration never re-runs even if JSON files reappear
    (e.g. from stale code, test fixtures, or an agent writing legacy state).
    """
    # Marker check must come first — bail out immediately if migration already ran.
    cursor = sql.execute(
        conn, "SELECT value FROM board_meta WHERE key='json_migrated'"
    )
    if cursor.fetchone() is not None:
        return
    coord_dir = sys.modules[__name__].COORD_DIR
    dispatched_json = coord_dir / "dispatched.json"
    if not dispatched_json.exists():
        return
    cursor = sql.execute(conn, "SELECT COUNT(*) FROM assignments")
    if cursor.fetchone()[0] > 0:
        return
    try:
        _migrate_json(conn)
    except Exception as exc:  # noqa: BLE001 — migration is best-effort
        print(f"coord: warning: JSON→SQLite migration failed: {exc}", file=sys.stderr)


def _migrate_json(conn: sqlite3.Connection) -> None:  # noqa: C901 — acceptable complexity
    """One-shot migration from JSON files to SQLite.  Renames JSON files to .bak."""
    import time as _time

    coord_dir = sys.modules[__name__].COORD_DIR
    dispatched_json = coord_dir / "dispatched.json"
    notified_json = coord_dir / "notified.json"
    board_json = coord_dir / "board.json"
    proposals_json = coord_dir / "pending_proposals.json"
    splits_json = coord_dir / "pending_splits.json"
    plans_json = coord_dir / "plans.json"
    session_json = coord_dir / "session.json"
    merge_queue_json = coord_dir / "merge_queue.json"

    with conn:  # single transaction
        # 1. dispatched.json → assignments (initial insert, status='running')
        dispatched_data: list[dict] = []
        if dispatched_json.exists():
            try:
                dispatched_data = json.loads(dispatched_json.read_text())
            except Exception:  # noqa: BLE001
                pass
        for rec in dispatched_data:
            sql.insert_ignore(
                conn,
                "assignments",
                [
                    "assignment_id", "machine_name", "repo_name", "repo_github",
                    "issue_number", "issue_title", "status", "type", "briefing",
                    "files_allowed", "model", "dispatched_at",
                    "review_of_assignment_id", "required_gates",
                ],
                (
                    rec.get("assignment_id", ""),
                    rec.get("machine_name", ""),
                    rec.get("repo_name", ""),
                    rec.get("repo_github"),
                    rec.get("issue_number", 0),
                    rec.get("issue_title", ""),
                    "running",
                    rec.get("type", "work"),
                    rec.get("briefing", ""),
                    json.dumps(rec.get("files_likely", [])),
                    rec.get("model"),
                    rec.get("dispatched_at"),
                    rec.get("review_of_assignment_id"),
                    json.dumps(rec.get("required_gates", [])),
                ),
            )

        # 2. notified.json → notifications
        if notified_json.exists():
            try:
                notified: dict[str, dict] = json.loads(notified_json.read_text())
                for aid, info in notified.items():
                    sql.upsert(
                        conn,
                        "notifications",
                        ["assignment_id", "event", "branch", "posted_at"],
                        (aid, info.get("event", ""), info.get("branch"),
                         info.get("posted_at", _time.time())),
                        conflict_columns=["assignment_id"],
                    )
            except Exception:  # noqa: BLE001
                pass

        # 3. board.json → assignments (richer status fields via REPLACE)
        if board_json.exists():
            try:
                board_data = json.loads(board_json.read_text())
                all_entries = (
                    board_data.get("active", []) + board_data.get("completed", [])
                )
                for a in all_entries:
                    aid = a.get("assignment_id")
                    if not aid:
                        continue
                    sql.upsert(
                        conn,
                        "assignments",
                        [
                            "assignment_id", "machine_name", "repo_name", "repo_github",
                            "issue_number", "issue_title", "status", "type", "branch",
                            "pr_url", "briefing", "files_allowed", "files_forbidden",
                            "model", "dispatched_at", "finished_at", "smoke_test",
                            "smoke_test_reason", "review_state", "review_of_assignment_id",
                            "review_target", "required_gates", "plan",
                            "unreachable_count", "exit_code",
                        ],
                        (
                            aid,
                            a.get("machine_name", ""),
                            a.get("repo_name", ""),
                            None,  # repo_github not in board JSON
                            a.get("issue_number", 0),
                            a.get("issue_title", ""),
                            a.get("status", "running"),
                            a.get("type", "work"),
                            a.get("branch"),
                            a.get("pr_url"),
                            a.get("briefing", ""),
                            json.dumps(a.get("files_allowed", [])),
                            json.dumps(a.get("files_forbidden", [])),
                            a.get("model"),
                            a.get("dispatched_at"),
                            a.get("finished_at"),
                            a.get("smoke_test"),
                            a.get("smoke_test_reason"),
                            a.get("review_state"),
                            a.get("review_of_assignment_id"),
                            a.get("review_target"),
                            json.dumps(a.get("required_gates", [])),
                            json.dumps(a.get("plan")) if a.get("plan") else None,
                            a.get("unreachable_count", 0),
                            a.get("exit_code"),
                        ),
                        conflict_columns=["assignment_id"],
                    )
                round_number = board_data.get("round_number", 0)
                sql.upsert(
                    conn, "board_meta", ["key", "value"],
                    ("round_number", str(round_number)),
                    conflict_columns=["key"],
                )
                sql.upsert(
                    conn, "board_meta", ["key", "value"],
                    ("board_initialized", "1"),
                    conflict_columns=["key"],
                )
            except Exception:  # noqa: BLE001
                pass

        # 4. proposals
        if proposals_json.exists():
            try:
                for p in json.loads(proposals_json.read_text()):
                    sql.insert_ignore(
                        conn,
                        "proposals",
                        [
                            "id", "machine_name", "repo_name", "issue_number",
                            "issue_title", "rationale", "files_likely", "briefing",
                            "model", "type", "required_gates",
                        ],
                        (
                            p.get("id"), p.get("machine_name", ""), p.get("repo_name", ""),
                            p.get("issue_number", 0), p.get("issue_title", ""),
                            p.get("rationale", ""), json.dumps(p.get("files_likely", [])),
                            p.get("briefing", ""), p.get("model"),
                            p.get("type", "work"), json.dumps(p.get("required_gates", [])),
                        ),
                    )
            except Exception:  # noqa: BLE001
                pass

        # 5. split proposals + chunks
        if splits_json.exists():
            try:
                for s in json.loads(splits_json.read_text()):
                    sql.insert_ignore(
                        conn,
                        "split_proposals",
                        ["id", "repo_name", "issue_number", "issue_title", "rationale"],
                        (s.get("id"), s.get("repo_name", ""), s.get("issue_number", 0),
                         s.get("issue_title", ""), s.get("rationale", "")),
                    )
                    for chunk in s.get("chunks", []):
                        sql.execute(
                            conn,
                            """INSERT INTO split_chunks
                               (split_proposal_id, title, scope, files_likely)
                               VALUES (?, ?, ?, ?)""",
                            (s.get("id"), chunk.get("title", ""), chunk.get("scope", ""),
                             json.dumps(chunk.get("files_likely", []))),
                        )
            except Exception:  # noqa: BLE001
                pass

        # 6. plans
        if plans_json.exists():
            try:
                for aid, plan_dict in json.loads(plans_json.read_text()).items():
                    sql.insert_ignore(
                        conn,
                        "plans",
                        ["assignment_id", "plan_data"],
                        (aid, json.dumps(plan_dict)),
                    )
            except Exception:  # noqa: BLE001
                pass

        # 7. session
        if session_json.exists():
            try:
                sess = json.loads(session_json.read_text())
                sql.execute(
                    conn,
                    """INSERT INTO sessions
                       (started_at, ended_at, clean_shutdown,
                        completed_this_session, issues_closed, total_cost_usd)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        sess.get("started_at"),
                        sess.get("ended_at"),
                        1 if sess.get("clean_shutdown") else 0,
                        json.dumps(sess.get("completed_this_session", [])),
                        json.dumps(sess.get("issues_closed", [])),
                        sess.get("total_cost_usd"),
                    ),
                )
            except Exception:  # noqa: BLE001
                pass

        # 8. merge queue
        if merge_queue_json.exists():
            try:
                for entry in json.loads(merge_queue_json.read_text()):
                    sql.execute(
                        conn,
                        """INSERT INTO merge_queue (
                            assignment_id, repo_name, repo_github, branch,
                            target_branch, issue_number, issue_title, state,
                            pr_number, pr_url, size, last_attempt, error
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            entry.get("assignment_id"), entry.get("repo_name"),
                            entry.get("repo_github"), entry.get("branch"),
                            entry.get("target_branch"), entry.get("issue_number"),
                            entry.get("issue_title"), entry.get("state", "pending"),
                            entry.get("pr_number"), entry.get("pr_url"),
                            entry.get("size"), entry.get("last_attempt"),
                            entry.get("error"),
                        ),
                    )
            except Exception:  # noqa: BLE001
                pass

        # Persist migration marker — this is the canonical "already migrated" signal.
        # Checked at the top of _maybe_migrate_json(), so JSON files reappearing
        # later (stale code, test fixtures, agent writing legacy state) won't
        # re-trigger the migration.
        sql.upsert(
            conn, "board_meta", ["key", "value"],
            ("json_migrated", str(_time.time())),
            conflict_columns=["key"],
        )

        # Rename JSON files to .bak so migration doesn't re-run
        for f in [
            dispatched_json, notified_json, board_json, proposals_json,
            splits_json, plans_json, session_json, merge_queue_json,
        ]:
            if f.exists():
                try:
                    f.rename(f.with_suffix(f.suffix + ".bak"))
                except Exception:  # noqa: BLE001
                    pass


# ── Gate-order migration (Test-before-Review reorder) ─────────────────────────

def _migrate_gate_order(conn: sqlite3.Connection) -> None:
    """Rewrite the stale default gate order in stored rows.

    The default gate order moved Test ahead of Review — from the #520-era
    ``["review", "test", "merge"]`` to ``["test", "review", "merge"]`` — now
    that the agent-assisted Testing stage is smooth enough to run as a smoke
    test *before* the PR/review (the natural order), reversing #520's "get the
    review over with first" workaround.  Any assignment, proposal, or
    ``board_meta`` row still carrying the previous implicit default is updated
    so the pipeline display and the headless review gate
    (:meth:`PipelineConfig.test_precedes_review`) agree.  Only the exact
    previous-default JSON string is touched — user-customised gate lists
    (anything else) are left unchanged.

    The target string (``["test", "review", "merge"]``) is also the pre-#520
    original, so a DB that never migrated forward is already in the desired
    state and untouched.

    This function is idempotent: if the previous-default string is absent, no
    rows change.
    """
    _OLD = '["review", "test", "merge"]'
    _NEW = '["test", "review", "merge"]'
    sql.execute(
        conn,
        "UPDATE assignments SET required_gates = ? WHERE required_gates = ?",
        (_NEW, _OLD),
    )
    sql.execute(
        conn,
        "UPDATE proposals SET required_gates = ? WHERE required_gates = ?",
        (_NEW, _OLD),
    )
    sql.execute(
        conn,
        "UPDATE board_meta SET value = ? "
        "WHERE key = 'pipeline_default_gates' AND value = ?",
        (_NEW, _OLD),
    )
    conn.commit()


def _backfill_orphaned_review_verdicts(conn: sqlite3.Connection) -> int:
    """#1663: copy a captured review verdict onto its parent WORK row.

    ``run_drain`` (#1616) captured the verdict onto the ``type='review'`` row
    and never propagated it to the parent, because the only path to that write
    ran through ``auto_loop.process_review_completion`` — which also dispatches
    fix workers, so the daemon's clock refused to enter it at all.  Every
    verdict the drain consumed instead of a human's ``coord notify`` therefore
    left its work row at ``review_state='dispatched'`` / ``review_verdict``
    NULL.  The code fix is in ``coord.notify._run_drain_locked`` +
    ``coord.auto_loop.propagate_review_verdict``; this repairs the rows already
    stranded when it landed (the 2026-08-01 batch — #1527 #1624 #1658 #1633
    #1353 — plus #544, #1078 and #1122), so none of them needs a re-review at
    $1-3 a head.

    **Copies only.**  The verdict is read from the existing review row via
    ``review_of_assignment_id``; nothing is synthesised, re-derived from a
    findings body, or inferred from pipeline state.  A work row whose review
    row never captured a verdict (#1122's ``188ae219aca3``, lost to #1636/#1658
    and recovered by hand onto PR #1656) has nothing to copy and is left
    exactly as it is — a fabricated verdict there would overwrite hand-recovered
    findings with a guess.

    Scoped to work rows that are actually stranded: a work-like ``type``, a NULL
    ``review_verdict`` of their own, and a review stage that demonstrably ran
    (``review_state`` in ``dispatched``/``done``).  Rows at ``pending`` /
    ``advisory`` / NULL are untouched — their review hasn't run or was waived.

    Idempotent, and runs on every connection open like
    :func:`_migrate_gate_order`: after the first pass every candidate has a
    non-NULL ``review_verdict``, so the ``UPDATE`` matches nothing.  Returns the
    number of rows repaired (0 on the steady state) for logging/tests.
    """
    # Latest verdict-carrying review for this work row.  `dispatched_at DESC`
    # picks the most recent round when a work row was reviewed more than once.
    # Deliberately NOT filtered on `r.status='done'`: #1122's review row is
    # `failed`, and a verdict recovered onto a failed row by hand (#617's
    # transcript recovery, `coord diagnose --stage review`) was still earned by
    # a real review and is exactly what we want to preserve.
    _LATEST_VERDICT = """
        SELECT r.review_verdict FROM assignments r
         WHERE r.review_of_assignment_id = assignments.assignment_id
           AND r.type = 'review'
           AND r.review_verdict IS NOT NULL
           AND r.review_verdict != ''
         ORDER BY r.dispatched_at DESC
         LIMIT 1
    """
    cur = sql.execute(
        conn,
        f"""
        UPDATE assignments
           SET review_verdict = ({_LATEST_VERDICT}),
               review_state = 'done'
         WHERE type IN ('work', 'mock-author', 'test-author')
           AND review_verdict IS NULL
           AND review_state IN ('dispatched', 'done')
           AND ({_LATEST_VERDICT}) IS NOT NULL
        """
    )
    conn.commit()
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
