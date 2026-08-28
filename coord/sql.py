"""The dialect seam: paramstyle translation, upsert idioms, row factories,
and ``lastrowid``/``RETURNING`` (#2719, Phase C slice 1/7 of #1948).

PEP 249 (DB-API 2.0) standardizes the *connection and cursor protocol* --
``.execute()``, ``.executemany()``, ``.fetchone()`` -- but deliberately does
NOT standardize ``paramstyle``: it is a module attribute each driver picks for
itself.  ``sqlite3`` is ``qmark`` (``?``); ``psycopg``/``psycopg2`` are
``pyformat`` (``%s``).  This tree has on the order of 300 ``execute()``/
``executemany()`` calls and several hundred ``?`` placeholders written
directly against sqlite3's paramstyle, plus SQLite-only idioms
(``INSERT OR IGNORE``, ``lastrowid``) that a bare connection swap would not
touch -- see ``coord/dao.py``'s module docstring and #1949/#2708 for the full
adjudication of why "swap the connection" is not portability.

This module is that seam.  Every function below takes an open DB-API
connection and infers its dialect from the connection object itself --
**never** a config flag, per #2719 -- so a deployment's own connection
factory (wherever it lives, once one exists) is the single source of truth
for which backend is live.

**This PR wires nothing up.**  It is a pure addition: no existing call site
imports this module yet, so it ships unused and cannot regress the fleet.
Slices 2-7 of #1948 adopt it one file/domain at a time.

Callers always write SQL in sqlite3's native ``qmark`` style (``?``) --
that's what the ~300 existing call sites already look like, and it is the
style every call site written *before* this seam existed already uses.
:func:`execute`/:func:`executemany` translate to the active connection's
paramstyle before running the statement; SQLite needs no translation (qmark
is already its native style) and Postgres gets ``?`` rewritten to ``%s``.

One case is genuinely non-trivial: a literal ``?`` inside a quoted SQL string
(``'why?'``) must NOT be rewritten -- see :func:`_qmark_to_pyformat`.

``ON CONFLICT ... DO UPDATE SET col = excluded.col`` is *already* portable
(SQLite adopted Postgres's UPSERT syntax in 3.24, the version bundled with
every Python this project supports) -- 84 sites tree-wide use it today and
this seam passes that text through completely untouched.  :func:`upsert`
still exists as a helper because building the column/placeholder scaffolding
by hand at 84 call sites is exactly the kind of repetition a seam should
absorb, not because the *idiom* itself needs to differ per backend.
:func:`insert_ignore` is the case that genuinely does: SQLite's ``INSERT OR
IGNORE`` has no Postgres equivalent, so the two backends emit different SQL
text for the same intent.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import urlsplit, urlunsplit

# ── dialects ──────────────────────────────────────────────────────────────
#
# Plain string constants, not an Enum -- this tree doesn't use Enum anywhere
# else (statuses etc. are plain string literals throughout coord/), so this
# stays consistent rather than introducing a new pattern for one module.
DIALECT_SQLITE = "sqlite"
DIALECT_POSTGRES = "postgres"

# Maps a connection class's defining module (its top-level package name) to
# the dialect it speaks.  Keyed off the *driver*, so a future third backend
# is a one-line addition here rather than a new code path anywhere else.
_DRIVER_DIALECTS: dict[str, str] = {
    "sqlite3": DIALECT_SQLITE,
    "psycopg": DIALECT_POSTGRES,  # psycopg 3
    "psycopg2": DIALECT_POSTGRES,
}


class UnsupportedDialectError(ValueError):
    """Raised when a connection's driver module maps to no known dialect."""


#: What to tell a caller who hit a Postgres codepath without the extra
#: installed -- mirrors ``coord.commands._common.SERVER_EXTRA_INSTALL_HINT``'s
#: shape for the ``[server]`` extra.
POSTGRES_EXTRA_INSTALL_HINT = "pip install 'code-coordinator[postgres]'"


# Libpq keyword=value DSN tokens: ``key=value`` or ``key='quoted value'``
# (single-quoted values may escape ``\'`` and ``\\`` per libpq's own
# grammar). Used only by :func:`redact_dsn` below.
_DSN_KV_RE = re.compile(r"(\w+)\s*=\s*('(?:\\.|[^'\\])*'|\S+)")


def redact_dsn(dsn: str) -> str:
    """*dsn* with every credential-bearing field removed -- safe to print,
    log, or post as a GitHub comment.

    A Postgres DSN routinely embeds ``user:password@host`` (URI form,
    ``postgresql://user:pass@host:port/db``) or ``user=... password=...``
    (libpq keyword=value form, ``host=... dbname=... user=... password=...``)
    -- ``coord/db.py``'s ``_migrate_if_needed`` docstring already states the
    policy this exists to satisfy: a DSN must "deliberately never" appear in
    text that "can end up in a log or a GitHub comment", since agent failure
    reports post their captured stdout/stderr verbatim. Every caller that
    would otherwise echo a configured ``store.dsn`` (e.g. ``coord
    migrate-to-postgres``'s ``--dsn`` echo) must route through this first.

    Returns host/dbname only (port too, when the DSN specifies one) -- not a
    masked-but-recognizable form, so nothing of the credential survives even
    truncated. Handles both DSN shapes; an unparseable field is rendered as
    ``?`` rather than raising, since this exists for a diagnostic message,
    not validation -- a malformed DSN should fail *later*, at
    :func:`connect`, with a real error naming what's wrong.
    """
    stripped = dsn.strip()
    if "://" in stripped:
        parts = urlsplit(stripped)
        host = parts.hostname or "?"
        if parts.port:
            host = f"{host}:{parts.port}"
        dbname = parts.path.lstrip("/") or "?"
        return urlunsplit((parts.scheme, host, dbname, "", ""))

    def _unquote(value: str) -> str:
        if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
            return value[1:-1].replace("\\'", "'").replace("\\\\", "\\")
        return value

    fields = {key: _unquote(value) for key, value in _DSN_KV_RE.findall(stripped)}
    host = fields.get("host", "?")
    if fields.get("port"):
        host = f"{host}:{fields['port']}"
    dbname = fields.get("dbname", "?")
    return f"host={host} dbname={dbname}"


def _import_psycopg() -> Any:
    """Import ``psycopg``, translating its absence into an actionable message
    (#2886).

    ``psycopg`` is an optional dependency -- the ``postgres`` extra -- never a
    base or ``[dev]`` one (see ``pyproject.toml``'s ``[project.optional-
    dependencies]``): every existing SQLite deployment must keep working
    without ever pulling it in. Hitting this on an install that never asked
    for Postgres is therefore the expected, common case, not a bug -- so
    every Postgres call site in this module that needs the driver
    (:func:`connect`, :func:`row_factory_for`, :func:`driver_error`) routes
    its import through here rather than a bare ``import psycopg``, so the
    failure names the extra to install instead of surfacing a raw
    ``ModuleNotFoundError: No module named 'psycopg'`` traceback.

    :func:`driver_errors` deliberately does NOT route through here -- it
    degrades silently to ``(sqlite3.Error,)`` when psycopg is absent (that is
    its whole contract), so it keeps its own bare ``import psycopg`` inside a
    caught ``try/except ImportError`` rather than raising through this
    helper's message just to swallow it unread.
    """
    try:
        import psycopg  # noqa: PLC0415 -- optional dep, see docstring
    except ImportError as exc:  # ModuleNotFoundError is a subclass
        raise ModuleNotFoundError(
            "the Postgres backend needs the `psycopg` driver, which is not "
            f"installed. Install it with: {POSTGRES_EXTRA_INSTALL_HINT}"
        ) from exc
    return psycopg


def detect_dialect(conn: Any) -> str:
    """Identify *conn*'s SQL dialect from its concrete connection type.

    Keyed off ``type(conn).__module__`` -- the connection/driver -- never a
    config flag (#2719): a deployment already picks its engine by which
    connection object it hands this seam, so that decision is the single
    source of truth rather than a second, independently-settable flag that
    could drift from it.

    ``sqlite3.Connection`` (and subclasses -- e.g. the ``mode=ro`` URI
    connections ``coord/dao.py`` opens) resolves to :data:`DIALECT_SQLITE`.
    Both ``psycopg`` (v3) and ``psycopg2`` connections resolve to
    :data:`DIALECT_POSTGRES` -- Postgres is Postgres regardless of which
    driver generation opened the connection.
    """
    module = type(conn).__module__.partition(".")[0]
    try:
        return _DRIVER_DIALECTS[module]
    except KeyError:
        raise UnsupportedDialectError(
            f"unrecognized DB-API driver module {module!r} for connection "
            f"{conn!r} -- coord.sql only knows about: "
            f"{sorted(set(_DRIVER_DIALECTS))}"
        ) from None


# ── connection factory (#827) ────────────────────────────────────────────


def connect(
    *,
    backend: str,
    sqlite_path: str | Path | None = None,
    dsn: str | None = None,
    read_only: bool = False,
    check_same_thread: bool = True,
) -> Any:
    """Open a new DB-API connection for *backend* -- the ONE place in this
    tree a raw driver ``.connect()`` call may appear outside a caller's own
    test fixtures (#827; enforced by
    ``tests/test_sql_dialect.py::test_no_raw_driver_connect_call_outside_the_dialect_seam``,
    the ``connect``-call sibling of #2768's ``execute``-call ratchet).

    Callers pick a backend explicitly rather than this function inferring
    one from *which of sqlite_path/dsn was given* -- ``backend`` is always
    the thing a caller already resolved from ``coordinator.yml``'s
    ``store:`` block (see ``coord.db._resolve_store_target``), and an
    explicit mismatch (``backend="postgres"`` with no ``dsn``) is a caller
    bug worth a loud ``ValueError`` rather than a guess.

    **SQLite** (``sqlite_path`` required): plain ``sqlite3.connect(str(path),
    check_same_thread=...)`` for a writer, or -- when *read_only* is set --
    the ``file:...?mode=ro`` URI form. This absorbs both call sites that used
    to open sqlite3 connections directly: ``coord/db.py``'s read/write
    singleton (``_open``, always ``check_same_thread=False`` -- see that
    module's connection-sharing docstring on :func:`coord.db.get_connection`)
    and ``coord/dao.py``'s ``SqliteStore`` read-only reader (``read_only=True``,
    also ``check_same_thread=False`` -- a fresh connection per call, safe from
    any thread). The ``mode=ro`` URI itself moved here from ``coord/dao.py``
    now that a real second backend's connection factory exists to make this a
    genuine dialect branch rather than a one-backend module growing dialect
    awareness ahead of needing it -- see that module's docstring for the
    superseded #2766 decision note.

    **Postgres** (``dsn`` required): ``psycopg.connect(dsn)``. ``psycopg`` is
    an optional dependency (see :func:`_import_psycopg`) imported
    function-locally, so calling this with ``backend=DIALECT_POSTGRES`` before
    psycopg is installed raises ``ImportError``/``ModuleNotFoundError`` rather
    than breaking import of this module for everyone else. *read_only* has no
    effect here -- unlike SQLite there is no connect-time spelling of
    "read-only" for Postgres; that half is :func:`apply_connection_setup`'s
    job, called separately by every caller right after ``connect()`` returns
    (exactly as it already was for SQLite's pragmas).
    """
    if backend == DIALECT_SQLITE:
        if not sqlite_path:
            raise ValueError("sql.connect(backend='sqlite') requires sqlite_path")
        import sqlite3

        if read_only:
            uri = f"file:{sqlite_path}?mode=ro"
            return sqlite3.connect(uri, uri=True, check_same_thread=check_same_thread)
        return sqlite3.connect(str(sqlite_path), check_same_thread=check_same_thread)
    if backend == DIALECT_POSTGRES:
        if not dsn:
            raise ValueError("sql.connect(backend='postgres') requires dsn")
        psycopg = _import_psycopg()

        return psycopg.connect(dsn)
    raise UnsupportedDialectError(backend)


# ── paramstyle translation ───────────────────────────────────────────────


def _qmark_to_pyformat(sql: str) -> str:
    """Rewrite sqlite3-style ``?`` placeholders to psycopg-style ``%s``.

    Two things make this non-trivial rather than a blind ``sql.replace("?",
    "%s")``:

    1. A ``?`` inside a quoted SQL string literal (``'why?'``) is data, not a
       placeholder, and must survive unchanged.  This is a single-pass
       scanner that tracks whether it is inside a ``'...'``/``"..."``
       literal (honoring the standard doubled-quote escape, e.g.
       ``'it''s ok?'``) and only rewrites ``?`` outside one.
    2. ``pyformat`` is literally Python ``%``-formatting under the hood, so
       ANY literal ``%`` in the statement -- including inside a string
       literal, e.g. ``LIKE '%foo%'`` -- must be doubled to ``%%`` or
       psycopg misparses the format string.  qmark has no such character, so
       this class of bug cannot exist before translation; it can only be
       introduced by it, which is why this function must be the one to fix
       it up rather than leaving it to callers.
    """
    out: list[str] = []
    in_string: str | None = None  # active quote char ("'" or '"'), or None
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if in_string is not None:
            if ch == in_string:
                out.append(ch)
                i += 1
                if i < n and sql[i] == in_string:
                    # Doubled-quote escape (e.g. '' inside a '...' literal)
                    # -- still inside the literal, not the closing quote.
                    out.append(sql[i])
                    i += 1
                    continue
                in_string = None
                continue
            if ch == "%":
                out.append("%%")
                i += 1
                continue
            out.append(ch)
            i += 1
            continue
        # Not inside a string literal.
        if ch in ("'", '"'):
            in_string = ch
            out.append(ch)
            i += 1
            continue
        if ch == "?":
            out.append("%s")
            i += 1
            continue
        if ch == "%":
            out.append("%%")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def translate(sql: str, dialect: str) -> str:
    """Translate *sql*, written in sqlite3's native ``qmark`` style, to
    *dialect*'s paramstyle.

    A no-op for :data:`DIALECT_SQLITE` (qmark is already its native style,
    so returning *sql* unchanged also means zero risk of this seam
    corrupting a statement no translation was ever needed for).
    """
    if dialect == DIALECT_SQLITE:
        return sql
    if dialect == DIALECT_POSTGRES:
        return _qmark_to_pyformat(sql)
    raise UnsupportedDialectError(dialect)


# ── execute / executemany ────────────────────────────────────────────────


def execute(conn: Any, sql: str, params: Sequence[Any] = ()) -> Any:
    """``cursor.execute()``, translating *sql* from qmark to *conn*'s
    paramstyle first.  Returns the cursor (so ``.fetchone()``/``.fetchall()``/
    ``.lastrowid`` etc. all still work exactly as they do today).

    Uses ``conn.cursor()`` + ``cursor.execute()`` -- the PEP 249 standard
    shape both sqlite3 and psycopg implement -- rather than either driver's
    connection-level convenience shortcut, so this works identically
    regardless of which shortcuts a given driver generation happens to ship.
    """
    dialect = detect_dialect(conn)
    cur = conn.cursor()
    cur.execute(translate(sql, dialect), params)
    return cur


def executemany(conn: Any, sql: str, seq_of_params: Iterable[Sequence[Any]]) -> Any:
    """``cursor.executemany()``, translating *sql* from qmark to *conn*'s
    paramstyle first.  See :func:`execute`."""
    dialect = detect_dialect(conn)
    cur = conn.cursor()
    cur.executemany(translate(sql, dialect), seq_of_params)
    return cur


# ── upsert / insert_ignore ───────────────────────────────────────────────


def upsert(
    conn: Any,
    table: str,
    columns: Sequence[str],
    params: Sequence[Any],
    *,
    conflict_columns: Sequence[str],
    update_columns: Sequence[str] | None = None,
) -> Any:
    """``INSERT INTO ... ON CONFLICT (...) DO UPDATE SET col = excluded.col``.

    The emitted SQL text is identical across SQLite (>=3.24) and Postgres --
    see the module docstring -- so this exists to absorb the column/
    placeholder scaffolding repeated at 84 call sites tree-wide, not because
    the idiom itself branches per backend.  ``detect_dialect`` still runs
    first so an unsupported connection fails the same way every other
    function in this module does, and so this stays structured to add a
    backend whose UPSERT syntax genuinely differs (e.g. MySQL's
    ``ON DUPLICATE KEY UPDATE``) without disturbing existing callers.

    ``update_columns`` defaults to every column except the conflict key(s)
    -- the common case of "touch every field on a re-seen row".  Passing an
    empty sequence emits ``DO NOTHING`` instead of ``DO UPDATE``.
    """
    detect_dialect(conn)  # validate; raises UnsupportedDialectError early
    if update_columns is None:
        conflict_set = set(conflict_columns)
        update_columns = [c for c in columns if c not in conflict_set]

    collist = ", ".join(columns)
    placeholders = ", ".join(["?"] * len(columns))
    conflict = ", ".join(conflict_columns)
    if update_columns:
        set_clause = ", ".join(f"{c} = excluded.{c}" for c in update_columns)
        action = f"DO UPDATE SET {set_clause}"
    else:
        action = "DO NOTHING"
    sql = (
        f"INSERT INTO {table} ({collist}) VALUES ({placeholders}) "
        f"ON CONFLICT ({conflict}) {action}"
    )
    return execute(conn, sql, params)


def insert_ignore(conn: Any, table: str, columns: Sequence[str], params: Sequence[Any]) -> Any:
    """Insert a row, silently doing nothing on a conflict.

    This is the one upsert-family idiom that genuinely differs per backend:
    SQLite's ``INSERT OR IGNORE`` has no Postgres equivalent, so Postgres
    gets the portable ``ON CONFLICT DO NOTHING`` form instead (target-less,
    matching ``INSERT OR IGNORE``'s "any conflict" semantics rather than
    naming a specific constraint).
    """
    dialect = detect_dialect(conn)
    collist = ", ".join(columns)
    placeholders = ", ".join(["?"] * len(columns))
    if dialect == DIALECT_SQLITE:
        sql = f"INSERT OR IGNORE INTO {table} ({collist}) VALUES ({placeholders})"
    elif dialect == DIALECT_POSTGRES:
        sql = f"INSERT INTO {table} ({collist}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"
    else:  # pragma: no cover -- detect_dialect already raised for anything else
        raise UnsupportedDialectError(dialect)
    return execute(conn, sql, params)


# ── row factory ───────────────────────────────────────────────────────────


def row_factory_for(dialect: str) -> Any:
    """The row-factory value that makes ``row["col"]`` work for *dialect*.

    ``sqlite3.Row`` is returned directly (it's a base-dependency import,
    already used throughout this tree).  ``psycopg.rows.dict_row`` is
    imported function-locally -- ``psycopg`` is an optional dependency (the
    ``postgres`` extra, #2886), never a base or ``[dev]`` one (see
    ``pyproject.toml``'s ``[project.optional-dependencies]``) -- so calling
    this with :data:`DIALECT_POSTGRES` before the extra is installed raises
    ``ImportError``/``ModuleNotFoundError``, naming the extra to install
    (see :func:`_import_psycopg`), rather than breaking import of this
    module for everyone else.
    """
    if dialect == DIALECT_SQLITE:
        import sqlite3

        return sqlite3.Row
    if dialect == DIALECT_POSTGRES:
        _import_psycopg()  # translate a missing psycopg into an actionable message
        from psycopg.rows import dict_row  # noqa: PLC0415 -- optional dep, see docstring

        return dict_row
    raise UnsupportedDialectError(dialect)


def apply_row_factory(conn: Any) -> None:
    """Set ``conn.row_factory`` to whatever makes ``row["col"]`` work for
    *conn*'s dialect.  Both ``sqlite3.Connection`` and psycopg3's
    ``Connection`` expose ``row_factory`` as a plain settable attribute, so
    this is the same one-liner ``coord/db.py`` already does for SQLite,
    generalized across the seam."""
    conn.row_factory = row_factory_for(detect_dialect(conn))


# ── schema DDL (#2724, Phase C slice 6/7 of #1948) ──────────────────────────
#
# coord/db.py is the one module in this tree that emits *schema* DDL (CREATE
# TABLE / ALTER TABLE / connection-setup pragmas), and it is the file where
# every SQLite-only schema idiom tree-wide is concentrated: this section is
# where each one gets a portable seam so coord/db.py's own SQL text never
# has to name a backend.


def executescript(conn: Any, script: str) -> Any:
    """Run a multi-statement DDL/DML script in one shot.

    Schema scripts carry no ``?`` placeholders (DDL has none, and the one
    seed-value literal in coord/db.py's legacy schema_version collapse is
    copied by ``SELECT``, not bound), so there is no paramstyle translation
    to do here -- what differs per backend is the *execution mechanism*.
    SQLite exposes ``Connection.executescript()`` as a distinct API because
    PEP 249's ``Cursor.execute()`` is documented as one statement at a time
    and sqlite3 enforces that split. Postgres has no such restriction: a
    psycopg cursor sends a parameter-less multi-statement string as a
    single simple-query protocol message and the server runs every
    ``;``-separated statement in it -- which is exactly what a schema
    script is. Returns the cursor.
    """
    dialect = detect_dialect(conn)
    cur = conn.cursor()
    if dialect == DIALECT_SQLITE:
        cur.executescript(script)
        return cur
    if dialect == DIALECT_POSTGRES:
        cur.execute(script)
        return cur
    raise UnsupportedDialectError(dialect)


def autoincrement_pk_ddl(dialect: str) -> str:
    """The type+constraint DDL fragment for an auto-incrementing integer
    primary key column -- everything after the column name (#2724).

    SQLite: ``INTEGER PRIMARY KEY AUTOINCREMENT`` -- ROWID aliasing, so the
    value is monotonic and never reused, which is the whole reason every
    site in this tree that needs a durable ordinal id (``merge_queue.id``,
    ``audit_log.id``, ...) uses it over plain ``INTEGER PRIMARY KEY``.

    Postgres: ``INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY`` --
    the SQL-standard identity-column syntax, equivalent to ``SERIAL PRIMARY
    KEY`` but without ``SERIAL``'s implicit, separately-owned sequence
    object (identity columns are dropped/renamed atomically with the
    column itself).

    coord/db.py calls this once per ``_ensure_schema()`` and substitutes
    the result for every ``__AUTOPK_DDL__`` sentinel in its schema
    template (which itself names no backend-specific syntax) -- rather
    than each call site branching on dialect, matching the rest of this
    seam's shape (callers write one dialect-neutral thing; this module
    decides what it becomes).
    """
    if dialect == DIALECT_SQLITE:
        return "INTEGER PRIMARY KEY AUTOINCREMENT"
    if dialect == DIALECT_POSTGRES:
        return "INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"
    raise UnsupportedDialectError(dialect)


def identity_columns(conn: Any, table: str) -> list[str]:
    """Column names in *table* declared ``GENERATED { ALWAYS | BY DEFAULT }
    AS IDENTITY`` -- i.e. produced by :func:`autoincrement_pk_ddl`'s Postgres
    branch. Always ``[]`` on SQLite: ``AUTOINCREMENT`` is a ROWID alias, not
    a separate sequence object, so there is nothing to fall out of sync
    (see :func:`resync_identity_sequence`).

    Used by ``coord.store_migrate.import_table`` -- a bulk import writes
    explicit ``id`` values into these columns (accepted by
    ``GENERATED BY DEFAULT``, unlike ``GENERATED ALWAYS``), which does not
    itself advance the underlying sequence, so the caller must resync it
    afterward or the next ordinary write collides with an id the import
    already used.
    """
    dialect = detect_dialect(conn)
    if dialect == DIALECT_SQLITE:
        return []
    if dialect == DIALECT_POSTGRES:
        cur = execute(
            conn,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = ? AND identity_generation IS NOT NULL "
            "ORDER BY ordinal_position",
            (table,),
        )
        names = []
        for row in cur.fetchall():
            try:
                names.append(row["column_name"])
            except (KeyError, TypeError, IndexError):
                names.append(row[0])
        return names
    raise UnsupportedDialectError(dialect)


def resync_identity_sequence(conn: Any, table: str, column: str) -> None:
    """Advance *table*.*column*'s identity sequence to match the data
    actually in the table -- a no-op on SQLite.

    A bulk import that INSERTs explicit ``id`` values into a
    ``GENERATED BY DEFAULT AS IDENTITY`` column (see
    :func:`autoincrement_pk_ddl`) does not advance that column's sequence as
    a side effect -- the sequence only moves when something calls
    ``nextval()`` on it, which an explicit-value INSERT never does. Left
    unresynced, the next ordinary write with no explicit id (how every
    caller in this tree writes today) is handed a value the import already
    used and rejected as a primary-key violation.

    ``setval(pg_get_serial_sequence(...), ..., is_called)``'s three-arg form
    is used rather than the two-arg form specifically for the empty-table
    case: with *is_called* explicitly computed as "does the table have any
    rows", an empty table correctly leaves the sequence so the *next*
    ``nextval()`` returns the identity's own start value (1) instead of 2,
    which the two-arg form (which always behaves as if ``is_called=true``)
    would get wrong.
    """
    dialect = detect_dialect(conn)
    if dialect == DIALECT_SQLITE:
        return
    if dialect == DIALECT_POSTGRES:
        execute(
            conn,
            "SELECT setval(pg_get_serial_sequence(?, ?), "
            f'COALESCE((SELECT MAX("{column}") FROM "{table}"), 1), '  # noqa: S608 -- column/table from introspection, not user input
            f'(SELECT MAX("{column}") FROM "{table}") IS NOT NULL)',  # noqa: S608
            (table, column),
        )
        return
    raise UnsupportedDialectError(dialect)


# SQLite has no BOOLEAN type -- every flag column in coord/db.py's schema
# (e.g. ``pinned INTEGER NOT NULL DEFAULT 0``, ``hold_after INTEGER NOT
# NULL DEFAULT 0``) is declared ``INTEGER DEFAULT 0``, using 0/1 as
# SQLite's conventional boolean encoding. This genuinely is a per-backend
# DDL divergence -- unlike AUTOINCREMENT (a hard incompatibility) or
# PRAGMA (connection setup, not schema), an INTEGER-typed flag column is
# not *wrong* on Postgres, just not idiomatic there, where a real BOOLEAN
# type exists. #1849 already severed the *wire* schema
# (coord/board_schema.py) from this DDL, so no API consumer sees the
# column type either way, in either backend. Recorded here (#2724) as the
# seam's explicit position rather than something a future Postgres
# bring-up has to reverse-engineer from silence: coord/db.py's existing
# ``INTEGER DEFAULT 0`` flag columns do not need to change to adopt
# Postgres -- a real ``BOOLEAN`` column type is optional future polish,
# not a migration blocker, so no calling code is forced to route through
# this constant today.
FLAG_COLUMN_DDL = "INTEGER DEFAULT 0"


def apply_connection_setup(conn: Any, *, read_only: bool = False) -> None:
    """Run backend-specific one-time connection setup (#2724).

    SQLite: ``PRAGMA journal_mode=WAL`` / ``busy_timeout=5000`` /
    ``foreign_keys=ON`` -- SQLite-only connection pragmas with no Postgres
    equivalent (Postgres's WAL is always on and not a per-connection
    setting; its lock-wait and referential-integrity behavior are
    server/schema-level, not something a client connection opts into via a
    pragma). Postgres: no-op, so this is safe to call unconditionally right
    after ``connect()`` regardless of which backend is live.

    *read_only* (#2766, widened to Postgres by #827): set by a caller whose
    connection is opened purely for reads -- e.g. ``coord/dao.py``'s
    ``SqliteStore``, which owns a ``mode=ro`` connection pointed at the
    daemon's live, WAL-mode database. SQLite: skips
    ``journal_mode``/``foreign_keys`` (a ``mode=ro`` connection cannot write
    the WAL toggle -- attempting to costs an "attempt to write a readonly
    database" error -- and referential integrity is the writer's concern,
    not a read-only reader's) and instead sets ``PRAGMA query_only=ON``, a
    belt-and-suspenders guard against an accidental write ever reaching a
    connection meant to never issue one. ``busy_timeout`` is still set
    either way, so a read-only reader waits out a writer's momentary lock
    hold exactly as long as the writer connection itself would.

    Postgres: until #827, this was a documented no-op -- "there is no
    read-only connection factory live yet for this seam to branch on". Now
    that :func:`connect` is that factory, *read_only* sets the
    session/transaction read-only characteristic instead of doing nothing.
    ``psycopg2`` exposes this as ``conn.set_session(readonly=True)``;
    ``psycopg`` (v3) instead exposes a plain settable ``.read_only``
    property that takes effect for transactions opened after it's set --
    since every caller sets this immediately after ``connect()`` returns and
    before running any statement, that's always "the whole session". Checked
    via ``hasattr(conn, "set_session")`` rather than re-detecting which
    psycopg generation is live: both driver generations' connection objects
    already reach this function, and probing for the method they'd actually
    call is simpler than importing either driver just to ``isinstance``
    against its class. *read_only=False* (the default, i.e. a writer
    connection) is still a complete no-op for Postgres -- there is no
    connect-time pragma to set the way SQLite's ``journal_mode``/
    ``foreign_keys`` are.
    """
    dialect = detect_dialect(conn)
    if dialect == DIALECT_SQLITE:
        if read_only:
            conn.execute("PRAGMA query_only=ON")
        else:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
    elif dialect == DIALECT_POSTGRES:
        if read_only:
            set_session = getattr(conn, "set_session", None)
            if set_session is not None:
                set_session(readonly=True)  # psycopg2
            else:
                conn.read_only = True  # psycopg3
    else:
        raise UnsupportedDialectError(dialect)


def table_columns(conn: Any, table: str) -> list[tuple[str, str]]:
    """Return ``[(name, type), ...]`` for *table*'s columns, in schema order
    (empty if *table* doesn't exist) -- portably across SQLite's
    ``PRAGMA table_info(...)`` and Postgres's standard
    ``information_schema.columns`` (#2782).

    SQLite has no ANSI ``information_schema`` -- ``PRAGMA table_info`` is its
    (connection-scoped, non-parameterizable-by-table-name) introspection
    idiom, which is exactly the kind of SQLite-only statement text #1948's
    seam exists to keep out of every module but this one and ``coord/db.py``.
    Postgres exposes the same information through the SQL-standard
    ``information_schema.columns`` view instead, queryable with a normal
    parameterized ``WHERE table_name = ?`` -- no pragma, no per-backend
    branch at the call site.

    The table name is interpolated into the SQLite ``PRAGMA`` text (pragmas
    do not accept bound parameters for their target object -- the same
    constraint the ``f"PRAGMA table_info({table})"`` call site this
    replaces already lived with); every call site passes a hardcoded table
    constant, never user input.

    Row access tolerates either a tuple-row or a dict-like row (e.g.
    ``psycopg.rows.dict_row``, which :func:`apply_row_factory` may already
    have installed on *conn*) -- same fallback shape
    :func:`insert_returning_id` uses, since which row-factory (if any) a
    caller applied before calling this is not this function's concern.
    """
    dialect = detect_dialect(conn)
    if dialect == DIALECT_SQLITE:
        cur = execute(conn, f"PRAGMA table_info({table})")  # noqa: S608 -- table name, not user input
        return [(row[1], row[2] or "TEXT") for row in cur.fetchall()]
    if dialect == DIALECT_POSTGRES:
        cur = execute(
            conn,
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = ? ORDER BY ordinal_position",
            (table,),
        )
        columns = []
        for row in cur.fetchall():
            try:
                columns.append((row["column_name"], row["data_type"]))
            except (KeyError, TypeError, IndexError):
                columns.append((row[0], row[1]))
        return columns
    raise UnsupportedDialectError(dialect)


def foreign_keys(conn: Any, table: str) -> list[tuple[str, str, str]]:
    """Return ``[(from_column, ref_table, ref_column), ...]`` for every FK
    *table* declares (empty if none, or if *table* doesn't exist) --
    portably across SQLite's ``PRAGMA foreign_key_list(...)`` and Postgres's
    standard ``information_schema`` constraint views (#828).

    SQLite's FK introspection is a pragma, like ``table_info`` above -- same
    reasoning, same fix: this is the one place that pragma's statement text
    may appear outside ``coord/db.py``, so a caller (``coord.store_migrate``,
    today) never writes it directly. Postgres has no single-pragma
    equivalent; the standard three-way join across
    ``information_schema.table_constraints`` /
    ``key_column_usage`` / ``constraint_column_usage`` is the SQL-standard
    way to ask "what does this table reference, and through which column"
    without touching Postgres's own (non-standard) ``pg_catalog`` tables.

    The table name is interpolated into the SQLite ``PRAGMA`` text for the
    same reason :func:`table_columns` does -- pragmas take no bound
    parameter for their target object; every call site passes a hardcoded
    table constant, never user input.

    Known limitation, Postgres branch: the three-way join keys purely on
    ``constraint_name``/``table_schema``, with no ordinal-position join
    between ``key_column_usage`` and ``constraint_column_usage``. For a
    *composite* (multi-column) foreign key this can mis-pair columns --
    e.g. return ``(col_a, ref_table, ref_col_b)`` instead of the two
    correctly-paired single-column edges. Not triggered today: the schema
    has exactly one FK, and it's single-column. Fix before adding a
    composite FK to ``coord/db.py``'s schema: join on ``ordinal_position``
    too (Postgres exposes it on both views).
    """
    dialect = detect_dialect(conn)
    if dialect == DIALECT_SQLITE:
        cur = execute(conn, f"PRAGMA foreign_key_list({table})")  # noqa: S608 -- table name, not user input
        edges = []
        for row in cur.fetchall():
            values = tuple(row)
            # id, seq, table, from, to, on_update, on_delete, match
            edges.append((values[3], values[2], values[4] or "rowid"))
        return edges
    if dialect == DIALECT_POSTGRES:
        cur = execute(
            conn,
            "SELECT kcu.column_name, ccu.table_name AS ref_table, "
            "ccu.column_name AS ref_column "
            "FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "  ON tc.constraint_name = kcu.constraint_name "
            "  AND tc.table_schema = kcu.table_schema "
            "JOIN information_schema.constraint_column_usage ccu "
            "  ON tc.constraint_name = ccu.constraint_name "
            "  AND tc.table_schema = ccu.table_schema "
            "WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_name = ?",
            (table,),
        )
        edges = []
        for row in cur.fetchall():
            try:
                edges.append((row["column_name"], row["ref_table"], row["ref_column"]))
            except (KeyError, TypeError, IndexError):
                edges.append((row[0], row[1], row[2]))
        return edges
    raise UnsupportedDialectError(dialect)


# ── WAL (#2782) ───────────────────────────────────────────────────────────
#
# WAL is a SQLite storage concept with no Postgres equivalent at all -- unlike
# every other split in this seam, there is no translation to grow here.  These
# two helpers exist purely so the literal ``PRAGMA`` text for them lives in
# exactly one place (this module) instead of at the two call sites in
# coord/serve_app.py -- callers MUST dialect-guard with :func:`detect_dialect`
# before calling either; neither checks the dialect itself, matching
# :func:`table_columns`'s SQLite branch (a helper that unconditionally speaks
# one dialect's statement text, not one that decides whether to).


def sqlite_journal_mode(conn: Any) -> str:
    """Return SQLite's current ``journal_mode`` (e.g. ``"wal"``,
    ``"delete"``) via ``PRAGMA journal_mode``.  SQLite-only -- callers must
    already know *conn* is a SQLite connection before calling this."""
    return execute(conn, "PRAGMA journal_mode").fetchone()[0]


def sqlite_wal_checkpoint_truncate(conn: Any) -> tuple[int, int, int]:
    """Run ``PRAGMA wal_checkpoint(TRUNCATE)`` and return the three integers
    SQLite reports: ``(busy, log, checkpointed)`` -- ``busy`` is 1 if an
    active reader blocked the truncate, ``log`` is WAL frames written since
    the last checkpoint, ``checkpointed`` is frames successfully
    checkpointed.  SQLite-only -- callers must already know *conn* is a
    SQLite connection, in WAL mode, before calling this."""
    row = execute(conn, "PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    return (row[0], row[1], row[2]) if row else (0, 0, 0)


def driver_error(conn: Any) -> type[BaseException]:
    """The DB-API ``Error`` base class *conn*'s driver raises (#2766).

    Every DB-API 2.0 driver exposes a module-level ``Error`` that is the
    root of that driver's whole exception hierarchy (``OperationalError``,
    ``IntegrityError``, ...), so ``except sql.driver_error(conn):`` replaces
    a driver-named ``except sqlite3.Error:`` without narrowing what gets
    caught -- and, unlike the hardcoded sqlite3 name, keeps catching under
    Postgres instead of silently going fail-open once a psycopg connection
    is the one in play.
    """
    dialect = detect_dialect(conn)
    if dialect == DIALECT_SQLITE:
        import sqlite3

        return sqlite3.Error
    if dialect == DIALECT_POSTGRES:
        psycopg = _import_psycopg()

        return psycopg.Error
    raise UnsupportedDialectError(dialect)


def driver_errors() -> tuple[type[BaseException], ...]:
    """The DB-API ``Error`` base class(es) of every *installed* driver
    (#2784).

    Unlike :func:`driver_error`, this needs no connection -- it exists for
    the ~18 call sites tree-wide that wrap a ``retry_on_locked(...)`` call
    (or another write with no connection in scope) in
    ``except sqlite3.OperationalError:``, hardcoding the one driver those
    call sites happened to be written against.  That hardcoding is exactly
    why #2784 found the whole #2597/#2689 degrade-gracefully layer silently
    inert on Postgres: a psycopg exception is never an instance of
    ``sqlite3.OperationalError``, so the ``except`` clause never matches,
    the handler never runs, and a transient lock becomes an uncaught crash
    instead of a retried write.

    ``sqlite3`` is a stdlib module -- always present, always included.
    ``psycopg`` is an optional dependency, not a base or ``[dev]`` one (see
    :func:`_import_psycopg`), so its absence is the normal case today, not an
    error: this degrades to ``(sqlite3.Error,)`` rather than raising
    ``ImportError`` -- unlike every other Postgres call site in this module,
    this one does NOT route through :func:`_import_psycopg` and its
    actionable message, precisely because the message would just be
    swallowed unread by the ``except ImportError`` below; there is no dialect
    to detect at all here, so degrading silently (not even
    function-local, deferred-import-style) is correct: a caller doing
    ``except sql.driver_errors():`` on a SQLite-only install must keep
    behaving exactly as ``except sqlite3.OperationalError:`` always did.

    Returns a tuple (not a single class) because ``except`` accepts either,
    and a tuple is what every call site actually needs: catch whichever
    driver's error the live connection happens to raise, without knowing in
    advance which driver that is.
    """
    import sqlite3

    errors: list[type[BaseException]] = [sqlite3.Error]
    try:
        import psycopg  # noqa: PLC0415 -- optional dep, absence degrades silently, see docstring
    except ImportError:
        pass
    else:
        errors.append(psycopg.Error)
    return tuple(errors)


def insert_ignore_select(conn: Any, table: str, select_sql: str) -> Any:
    """``INSERT OR IGNORE INTO table <select_sql>`` -- the SELECT-sourced
    sibling of :func:`insert_ignore`, for the one call site (coord/db.py's
    one-time legacy ``schema_version`` table collapse) that copies rows
    from a query rather than binding literal params.

    Same backend split as :func:`insert_ignore`: SQLite's ``INSERT OR
    IGNORE`` has no Postgres equivalent, so Postgres gets the portable
    ``ON CONFLICT DO NOTHING`` form instead.
    """
    dialect = detect_dialect(conn)
    if dialect == DIALECT_SQLITE:
        sql_text = f"INSERT OR IGNORE INTO {table} {select_sql}"
    elif dialect == DIALECT_POSTGRES:
        sql_text = f"INSERT INTO {table} {select_sql} ON CONFLICT DO NOTHING"
    else:
        raise UnsupportedDialectError(dialect)
    return execute(conn, sql_text)


# ── lastrowid / RETURNING ────────────────────────────────────────────────


def insert_returning_id(
    conn: Any, sql: str, params: Sequence[Any] = (), *, pk_column: str = "id"
) -> Any:
    """Run an INSERT, returning the new row's primary-key value -- portably
    across SQLite's ``cursor.lastrowid`` and Postgres's ``RETURNING``.

    *sql* must be a plain ``INSERT`` with no ``RETURNING`` clause of its own;
    one is appended only on the Postgres path, where ``lastrowid`` does not
    exist. This keeps the qmark SQL a caller writes identical across both
    backends -- the four tree-wide ``lastrowid`` call sites each become one
    call to this function, not a per-backend branch at the call site.
    """
    dialect = detect_dialect(conn)
    if dialect == DIALECT_POSTGRES:
        returning_sql = f"{sql.rstrip().rstrip(';')} RETURNING {pk_column}"
        cur = execute(conn, returning_sql, params)
        row = cur.fetchone()
        if row is None:
            raise RuntimeError(
                f"INSERT ... RETURNING {pk_column} produced no row for: {sql!r}"
            )
        try:
            return row[pk_column]
        except (KeyError, TypeError):
            # A tuple-row cursor (no dict-like row factory applied) --
            # RETURNING projects exactly the one column we asked for.
            return row[0]
    cur = execute(conn, sql, params)
    return cur.lastrowid
