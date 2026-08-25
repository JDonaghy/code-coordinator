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
