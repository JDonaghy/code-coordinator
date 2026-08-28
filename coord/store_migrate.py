"""One-shot importer: load an existing SQLite ``coord.db`` into a Postgres
store opened through #827's dialect-aware connection factory (#828 second
half — the first half, fixing statement-shaped SQLite-isms, shipped as
#1948).

Three things a hardcoded table list would miss (see the issue's own
"Scope it against the real schema" note):

1. ``coord/housekeeping.py:92`` creates **archive mirror tables dynamically**
   (``assignments_archive``, ``notifications_archive``, ``merge_queue_archive``
   — one per table that has ever had a sweep run against it), with columns
   derived from the source table at sweep time. A live ``coord.db`` can
   therefore contain tables :func:`coord.db._ensure_schema` never declares.
   :func:`discover_tables` reads them straight out of ``sqlite_master``, so
   they are covered without being enumerated here.
2. ``schema_version``/``schema_version_new`` carry the *source* database's
   migration bookkeeping, which has nothing to do with which migrations the
   *target*'s own connection-open path already applied when it created (and
   thereby versioned) the target schema — #827 gives Postgres the exact same
   migration path SQLite has (``coord.db._migrate_if_needed``), so the target
   re-derives its own ``schema_version`` on connect rather than inheriting
   the source's. These two tables are therefore never imported (see
   :data:`_EXCLUDED_TABLES`).
3. SQLite's lax type affinity lets a row drift a string into a column
   declared ``INTEGER`` (the bool-as-int columns this issue's rewrite
   settled as *not* needing a schema change are exactly this shape) —
   Postgres enforces the declared type at insert time, so a drifted row
   becomes a mid-import failure unless it's caught first.
   :func:`audit_type_affinity` catches it *before* :func:`run_import` opens
   the target at all.

No ``INTEGER`` -> ``BOOLEAN`` conversion happens anywhere in this module —
that conversion was explicitly ruled out of scope by #2724 and reaffirmed by
this issue's rewrite.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from coord import sql

# schema_version/schema_version_new are migration *bookkeeping*, not data --
# see the module docstring, point 2. Re-derived by the target's own connect
# path, never copied from the source.
_EXCLUDED_TABLES = frozenset({"schema_version", "schema_version_new"})


@dataclass(frozen=True)
class TypeAffinityViolation:
    """One row/column whose stored Python type doesn't match its column's
    declared SQLite type affinity -- see :func:`audit_type_affinity`."""

    table: str
    column: str
    rowid: int
    value: Any
    expected_affinity: str

    def __str__(self) -> str:
        return (
            f"{self.table}.{self.column} (rowid={self.rowid}): value "
            f"{self.value!r} ({type(self.value).__name__}) does not match "
            f"declared affinity {self.expected_affinity}"
        )


@dataclass(frozen=True)
class ReferentialIntegrityViolation:
    """One FK edge (per ``PRAGMA foreign_key_list``) with orphaned rows on
    the source -- see :func:`audit_referential_integrity`."""

    table: str
    column: str
    ref_table: str
    ref_column: str
    orphan_count: int

    def __str__(self) -> str:
        return (
            f"{self.table}.{self.column} -> {self.ref_table}.{self.ref_column}: "
            f"{self.orphan_count} orphaned row(s) -- source was written by a "
            "connection that never enforced this FK (PRAGMA foreign_keys=OFF); "
            "see coord.sql.apply_connection_setup"
        )


class ImportAborted(RuntimeError):
    """Raised before any target write -- a pre-flight audit failed, the
    source is missing, or the target already has rows and ``force`` wasn't
    passed."""


@dataclass
class TableReport:
    table: str
    source_rows: int
    target_rows: int

    @property
    def ok(self) -> bool:
        return self.source_rows == self.target_rows


@dataclass
class ImportReport:
    tables: list[TableReport] = field(default_factory=list)
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return all(t.ok for t in self.tables)


# ── source introspection ─────────────────────────────────────────────────


def discover_tables(conn: sqlite3.Connection) -> list[str]:
    """Every real table in *conn*, sorted, minus sqlite's own internal
    tables and :data:`_EXCLUDED_TABLES` -- discovered from the database
    itself (``sqlite_master``), never a hardcoded list, so housekeeping's
    dynamic archive mirrors are covered automatically (see module
    docstring, point 1)."""
    cur = sql.execute(
        conn,
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name",
    )
    names = [row[0] for row in cur.fetchall()]
    return [n for n in names if n not in _EXCLUDED_TABLES]


def table_row_count(conn: Any, table: str) -> int:
    cur = sql.execute(conn, f'SELECT COUNT(*) FROM "{table}"')  # noqa: S608 -- table from discover_tables
    return cur.fetchone()[0]


# ── type-affinity audit ──────────────────────────────────────────────────


def _affinity(decl_type: str) -> str:
    """SQLite's own column-affinity rule (five-way, applied in this order --
    see https://www.sqlite.org/datatype3.html#determination_of_column_affinity),
    applied to a declared type string."""
    t = (decl_type or "").upper()
    if "INT" in t:
        return "INTEGER"
    if "CHAR" in t or "CLOB" in t or "TEXT" in t:
        return "TEXT"
    if "BLOB" in t or t == "":
        return "BLOB"
    if "REAL" in t or "FLOA" in t or "DOUB" in t:
        return "REAL"
    return "NUMERIC"


# Which Python types a value legitimately has for each affinity, given how
# SQLite actually stores data (see coord/db.py's FLAG_COLUMN_DDL comment for
# why INTEGER-affinity drift is the practically important case here: a
# column declared INTEGER can still hold a TEXT storage-class value if a row
# was ever written with a non-numeric string and SQLite couldn't losslessly
# convert it). NUMERIC affinity is intentionally permissive (SQLite itself
# only converts numeric-looking text; anything else is legitimately TEXT
# there), since no column in coord/db.py's schema is declared with a type
# that resolves to NUMERIC today -- this exists for forward compatibility,
# not a construct currently in use.
_AFFINITY_PYTYPES: dict[str, tuple[type, ...]] = {
    "INTEGER": (int,),
    "TEXT": (str,),
    "REAL": (int, float),
    "BLOB": (bytes,),
    "NUMERIC": (int, float, str),
}


def audit_type_affinity(
    conn: sqlite3.Connection, tables: Sequence[str]
) -> list[TypeAffinityViolation]:
    """Scan every row of every table in *tables* for a value whose Python
    type doesn't match its column's declared affinity -- the "type-affinity
    laxness" half of this issue: SQLite accepts it, Postgres doesn't, and a
    partial import is worse than an upfront, complete report naming every
    table/column/rowid involved."""
    violations: list[TypeAffinityViolation] = []
    for table in tables:
        columns = sql.table_columns(conn, table)
        if not columns:
            continue
        affinities = [(name, _affinity(decl)) for name, decl in columns]
        collist = ", ".join(f'"{name}"' for name, _ in affinities)
        cur = sql.execute(
            conn, f'SELECT rowid, {collist} FROM "{table}"'  # noqa: S608
        )
        for row in cur.fetchall():
            values = tuple(row)
            rowid = values[0]
            for idx, (name, affinity) in enumerate(affinities, start=1):
                value = values[idx]
                if value is None:
                    continue
                if not isinstance(value, _AFFINITY_PYTYPES[affinity]):
                    violations.append(
                        TypeAffinityViolation(table, name, rowid, value, affinity)
                    )
    return violations


# ── referential-integrity audit ──────────────────────────────────────────


def discover_foreign_keys(
    conn: sqlite3.Connection, tables: Sequence[str]
) -> list[tuple[str, str, str, str]]:
    """``[(table, from_column, ref_table, ref_column), ...]`` for every FK
    declared on any table in *tables*, via :func:`coord.sql.foreign_keys` --
    discovered from the schema, not hardcoded, the same way
    :func:`discover_tables` is (#828's acceptance bar for both)."""
    edges: list[tuple[str, str, str, str]] = []
    for table in tables:
        for from_col, ref_table, ref_col in sql.foreign_keys(conn, table):
            edges.append((table, from_col, ref_table, ref_col))
    return edges


def audit_referential_integrity(
    conn: sqlite3.Connection, tables: Sequence[str]
) -> list[ReferentialIntegrityViolation]:
    """Orphaned FK references on the *source* -- checked here because SQLite
    only enforces ``FOREIGN KEY`` when ``PRAGMA foreign_keys=ON`` was set on
    the *writing* connection (not guaranteed for every historical writer;
    see ``coord.sql.apply_connection_setup``), so a row written before that
    pragma existed -- or by a connection that skipped it -- can be orphaned
    today with SQLite never having complained. Postgres enforces every FK at
    insert time regardless, so checking here means the importer discovers
    this as a clear, actionable report instead of a mid-import driver
    exception naming neither the table nor the row."""
    violations: list[ReferentialIntegrityViolation] = []
    table_set = set(tables)
    for table, from_col, ref_table, to_col in discover_foreign_keys(conn, tables):
        if ref_table not in table_set:
            continue
        cur = sql.execute(
            conn,
            f'SELECT COUNT(*) FROM "{table}" WHERE "{from_col}" IS NOT NULL '  # noqa: S608
            f'AND "{from_col}" NOT IN (SELECT "{to_col}" FROM "{ref_table}")',
        )
        count = cur.fetchone()[0]
        if count:
            violations.append(
                ReferentialIntegrityViolation(table, from_col, ref_table, to_col, count)
            )
    return violations


# ── target write path ────────────────────────────────────────────────────


def _ensure_target_table(source_conn: Any, target_conn: Any, table: str) -> None:
    """Create *table* on the target if it isn't already there.

    Every table :func:`coord.db._ensure_schema` declares already exists on
    the target by the time this runs (:func:`run_import` opens the target
    through the same connect-and-migrate path production code uses). This
    only fires for housekeeping's dynamic archive mirrors, which live on
    whichever database instance actually ran a sweep and are declared
    nowhere in the static schema -- mirroring
    ``coord.housekeeping._ensure_archive_mirror``'s "no constraints, dumb
    storage" shape, using the source's own column types verbatim (every
    type coord/db.py's schema actually uses -- TEXT/INTEGER/REAL -- is a
    valid Postgres type name unchanged, so no translation table is needed).
    """
    if sql.table_columns(target_conn, table):
        return
    src_cols = sql.table_columns(source_conn, table)
    coldefs = ", ".join(f'"{name}" {ctype}' for name, ctype in src_cols)
    sql.execute(target_conn, f'CREATE TABLE "{table}" ({coldefs})')  # noqa: S608


def _existing_target_rows(target_conn: Any, tables: Sequence[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in tables:
        if not sql.table_columns(target_conn, table):
            continue  # not created yet -- can't have rows, and never in force's wipe list
        n = table_row_count(target_conn, table)
        if n:
            counts[table] = n
    return counts


def import_table(source_conn: Any, target_conn: Any, table: str) -> TableReport:
    """Copy every row of *table* from source to target, preserving values
    (including any explicit id -- ``coord.sql.autoincrement_pk_ddl``'s
    Postgres DDL is ``GENERATED BY DEFAULT AS IDENTITY``, which -- unlike
    ``GENERATED ALWAYS`` -- accepts an explicit value with no
    ``OVERRIDING SYSTEM VALUE`` needed). Returns the row-count-parity report
    for *table*."""
    columns = [name for name, _ in sql.table_columns(source_conn, table)]
    collist = ", ".join(f'"{c}"' for c in columns)
    cur = sql.execute(source_conn, f'SELECT {collist} FROM "{table}"')  # noqa: S608
    rows = [tuple(row) for row in cur.fetchall()]
    if rows:
        placeholders = ", ".join(["?"] * len(columns))
        sql.executemany(
            target_conn,
            f'INSERT INTO "{table}" ({collist}) VALUES ({placeholders})',  # noqa: S608
            rows,
        )
    target_conn.commit()
    return TableReport(
        table=table, source_rows=len(rows), target_rows=table_row_count(target_conn, table)
    )


def run_import(
    *,
    sqlite_path: str | Path,
    dsn: str = "",
    force: bool = False,
    dry_run: bool = False,
    target_connector: Callable[[], Any] | None = None,
) -> ImportReport:
    """Import *sqlite_path* into the Postgres database *dsn* points at.

    Pre-flight order (every check below runs, and must pass, before the
    target is ever opened -- "fail loudly... rather than aborting halfway
    through a partial import", per the issue):

    1. Source exists and has at least one table.
    2. :func:`audit_referential_integrity` -- source-side FK orphans.
    3. :func:`audit_type_affinity` -- source-side type drift.

    Idempotency (#828 acceptance: "decide which, state it, test it"): this
    importer **refuses** a target that already has rows in any table it
    would import into, unless *force* is passed -- in which case those
    tables are wiped (``DELETE``, not ``DROP``, so a table
    :func:`_ensure_target_table` didn't just create keeps its schema) and
    re-imported. Refuse-by-default was chosen over silent upsert because
    there is no single conflict key that's safe to upsert on across all 28+
    tables (composite keys, archive mirrors with no PK at all) -- a blind
    "insert or replace everything" risks masking a genuine double-run
    mistake instead of catching it.

    *target_connector*, when given, replaces the default
    ``coord.db.open_postgres_connection(dsn)`` -- used by this module's own
    tests to point the target at a second SQLite/Postgres database opened
    through ``tests/backends.py``'s ``scratch_database`` instead of a real
    DSN, so the whole orchestration (not just coord.sql's dialect
    primitives) gets exercised against a real Postgres server whenever CI's
    ``postgres`` job runs this file. When *target_connector* is given, this
    function does not close the connection it returns -- the caller
    (whoever constructed it) owns that lifecycle.
    """
    sqlite_path = Path(sqlite_path)
    if not sqlite_path.exists():
        raise ImportAborted(f"source SQLite database not found: {sqlite_path}")

    source_conn = sql.connect(backend=sql.DIALECT_SQLITE, sqlite_path=sqlite_path, read_only=True)
    sql.apply_row_factory(source_conn)
    try:
        tables = discover_tables(source_conn)
        if not tables:
            raise ImportAborted(f"no tables found in {sqlite_path}")

        fk_violations = audit_referential_integrity(source_conn, tables)
        if fk_violations:
            raise ImportAborted(
                "referential integrity violations in source -- fix before importing:\n"
                + "\n".join(f"  {v}" for v in fk_violations)
            )

        type_violations = audit_type_affinity(source_conn, tables)
        if type_violations:
            raise ImportAborted(
                "type-affinity violations in source -- fix before importing:\n"
                + "\n".join(f"  {v}" for v in type_violations)
            )

        if dry_run:
            report = ImportReport(dry_run=True)
            for table in tables:
                report.tables.append(
                    TableReport(
                        table=table, source_rows=table_row_count(source_conn, table), target_rows=0
                    )
                )
            return report

        owns_target = target_connector is None
        if target_connector is None:
            from coord import db as coord_db  # noqa: PLC0415 -- keeps psycopg optional at import time

            target_connector = lambda: coord_db.open_postgres_connection(dsn)  # noqa: E731

        target_conn = target_connector()
        try:
            existing = _existing_target_rows(target_conn, tables)
            if existing and not force:
                raise ImportAborted(
                    "target already has rows in: "
                    + ", ".join(f"{t} ({n})" for t, n in sorted(existing.items()))
                    + " -- refusing to import into a populated target "
                    "(pass force=True / --force to wipe and re-import)"
                )

            report = ImportReport(dry_run=False)
            for table in tables:
                _ensure_target_table(source_conn, target_conn, table)
                if table in existing:
                    sql.execute(target_conn, f'DELETE FROM "{table}"')  # noqa: S608
                report.tables.append(import_table(source_conn, target_conn, table))
            return report
        finally:
            if owns_target:
                target_conn.close()
    finally:
        source_conn.close()
