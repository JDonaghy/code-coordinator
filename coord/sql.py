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

from typing import Any, Iterable, Sequence

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
    imported function-locally -- ``psycopg`` is not a base or even a
    ``server``-extra dependency yet (see ``pyproject.toml``'s ``[project]
    dependencies`` comment on why third-party imports the base client
    doesn't need stay function-local) -- so calling this with
    :data:`DIALECT_POSTGRES` before a Postgres backend is actually installed
    raises ``ImportError``/``ModuleNotFoundError`` rather than breaking
    import of this module for everyone else.
    """
    if dialect == DIALECT_SQLITE:
        import sqlite3

        return sqlite3.Row
    if dialect == DIALECT_POSTGRES:
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

    *read_only* (#2766): set by a caller whose connection is opened purely
    for reads -- e.g. ``coord/dao.py``'s ``SqliteStore``, which owns a
    ``mode=ro`` connection pointed at the daemon's live, WAL-mode database.
    SQLite: skips ``journal_mode``/``foreign_keys`` (a ``mode=ro`` connection
    cannot write the WAL toggle -- attempting to costs an "attempt to write
    a readonly database" error -- and referential integrity is the writer's
    concern, not a read-only reader's) and instead sets ``PRAGMA
    query_only=ON``, a belt-and-suspenders guard against an accidental write
    ever reaching a connection meant to never issue one. ``busy_timeout`` is
    still set either way, so a read-only reader waits out a writer's
    momentary lock hold exactly as long as the writer connection itself
    would. Postgres: no-op regardless of *read_only* -- there is no
    read-only connection factory live yet for this seam to branch on, so
    the read-only intent has no effect here until one exists (a future
    Postgres bring-up would express it as ``SET SESSION CHARACTERISTICS AS
    TRANSACTION READ ONLY`` or a read-only role, not a connect-time pragma).
    """
    dialect = detect_dialect(conn)
    if dialect == DIALECT_SQLITE:
        if read_only:
            conn.execute("PRAGMA query_only=ON")
        else:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
    elif dialect != DIALECT_POSTGRES:
        raise UnsupportedDialectError(dialect)


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
        import psycopg  # noqa: PLC0415 -- optional dep, see row_factory_for

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
    ``psycopg`` is not a declared dependency (see :func:`row_factory_for`),
    so its absence is the normal case today, not an error: this degrades to
    ``(sqlite3.Error,)`` rather than raising ``ImportError``, the same
    "absence is normal" posture :func:`row_factory_for` takes for a
    *known* dialect with no live connection to detect it from -- except
    here there is no dialect to detect at all, so degrading silently (not
    even function-local, deferred-import-style) is correct: a caller doing
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
        import psycopg  # noqa: PLC0415 -- optional dep, see row_factory_for
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
