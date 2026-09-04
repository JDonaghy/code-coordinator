"""The content-parity oracle: dump two databases in canonical form and diff
them (#3086, extracted from #2885's ``tests/write_parity.py``).

Two callers, one implementation
-------------------------------
This machinery was built by #2885 as a *test* harness — replay a recorded
workload against two backends and diff the resulting state — and lived in
``tests/write_parity.py``.  #3086 needs the identical oracle from **shipped**
code: ``coord migrate-to-postgres --verify`` / ``--rehearse`` answers "is the
imported data the same data" by dumping (source SQLite, imported Postgres) and
comparing them, and that runs on cutover day out of an installed venv where
``tests/`` does not exist (``pyproject.toml``'s
``[tool.setuptools.packages.find] include = ["coord*"]``).

So the dump/diff half moved here and ``tests/write_parity.py`` re-exports it.
Deliberately **not** copied: an oracle with two implementations is an oracle
that drifts, and the whole value of pointing the migration at #2885's harness
is that #829 reads *the same* report shape on cutover day that CI has been
exercising all along.  What stayed in ``tests/write_parity.py`` is the part
that is genuinely test-only — the recorded ``WORKLOAD``, the frozen clock, and
the two-backend replay driver.

What it does
------------
1. :func:`dump_database` reads **every** table back out, discovering the table
   list and each table's primary key from the live schema (so a new table in
   ``coord/db.py`` joins the comparison with no edit here), and canonicalises
   the values.
2. :func:`compare_dumps` matches rows by primary key and returns a
   :class:`ParityReport` — a list of :class:`Difference` records, each naming a
   table, a row key, a column and both values.  **A diff, not a boolean**: the
   point is that an operator reads the report and classifies the entries, and
   "False" would tell them nothing.

What is deliberately tolerated
------------------------------
- **Auto-increment key values** may be replaced by their rank within the table
  (:func:`_rank_surrogate_keys`), because a Postgres identity sequence is not
  rolled back by a failed statement while a SQLite ``rowid`` is — so absolute
  sequence values can legitimately differ between two *independent* runs of the
  same workload, while relative order cannot.  That normalisation is right for
  #2885's replay comparison and **wrong** for #3086's migration check, where
  the importer copies every id verbatim and any id drift is a real finding:
  hence ``rank_surrogate_keys=False``, which the migration verification passes.

Nothing else is normalised.  In particular ``True`` is not folded into ``1``
and ``1`` is not folded into ``1.0`` — see :func:`values_differ` for why those
two are the divergences that matter most on this schema.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from coord import sql


# ── the canonical dump ───────────────────────────────────────────────────────

def table_names(conn: Any) -> tuple[str, ...]:
    """Every user table on *conn*, sorted.

    Discovered from the live database rather than from a hand-maintained list,
    so a table added to ``coord/db.py``'s schema joins the comparison with no
    edit here — the same "append one entry, no new test code" property
    ``tests/test_store_contract.py``'s ``BACKENDS`` has, applied to tables.

    The dialect branch lives here rather than in ``coord/sql.py`` on purpose:
    ``coord/sql.py`` is the *statement* seam (paramstyle, DDL, driver errors),
    and "enumerate a database's own tables" is a whole-database introspection
    concern that belongs with the oracle that needs it.  ``sql.detect_dialect``
    is still what decides, so this stays consistent with the rest of the seam
    and fails the same way on an unknown connection.
    """
    dialect = sql.detect_dialect(conn)
    if dialect == sql.DIALECT_SQLITE:
        rows = sql.execute(
            conn,
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'",
        ).fetchall()
    elif dialect == sql.DIALECT_POSTGRES:
        rows = sql.execute(
            conn,
            "SELECT table_name AS name FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'",
        ).fetchall()
    else:  # pragma: no cover -- detect_dialect already raised
        raise sql.UnsupportedDialectError(dialect)
    return tuple(sorted(_cell(row, "name", 0) for row in rows))


def primary_key(conn: Any, table: str) -> tuple[str, ...]:
    """*table*'s primary-key columns, in key order (empty when it has none).

    Row matching in :func:`compare_dumps` is by primary key, so the report can
    say "``assignments['work-2885a'].repo_github`` differs" instead of "row 3
    differs".  Derived from the schema for the same reason as
    :func:`table_names`: a hand-maintained key map is one more thing to drift.

    The dialect branch itself moved to :func:`coord.sql.primary_key_columns`
    when this module moved into ``coord/`` (#3086): SQLite spells it
    ``PRAGMA table_info``, and a ``PRAGMA`` outside ``coord/sql.py`` and
    ``coord/db.py`` is what ``tests/test_sql_dialect.py``'s #2782 ratchet
    refuses -- correctly, since a portable introspection primitive is exactly
    what the seam is for.  This stays as the oracle's own name for it, next to
    :func:`table_names`.
    """
    return sql.primary_key_columns(conn, table)


def _cell(row: Any, name: str, index: int) -> Any:
    """Read a column off a row that may be a tuple or a mapping.

    ``sql.apply_row_factory`` may or may not have run on the connection handed
    to this harness, and psycopg's ``dict_row`` and ``sqlite3.Row`` disagree
    about which access works — the same fallback shape ``sql.table_columns``
    already uses.
    """
    try:
        return row[name]
    except (KeyError, TypeError, IndexError):
        return row[index]


def _canonical_value(value: Any) -> Any:
    """Normalise one cell to a comparable, JSON-renderable form.

    See the module docstring for what is deliberately *not* normalised here.
    """
    if isinstance(value, Decimal):
        # Driver representation, not stored value: psycopg decodes NUMERIC to
        # Decimal where sqlite3 hands back a float for the same number.
        return float(value)
    if isinstance(value, memoryview):
        return ("bytes", value.tobytes().hex())
    if isinstance(value, (bytes, bytearray)):
        return ("bytes", bytes(value).hex())
    if isinstance(value, (list, dict)):
        # A json/jsonb column decoded by the driver.  coord/db.py stores JSON in
        # TEXT columns today, so this is defensive — but a future jsonb column
        # must not make the dump unhashable/unsortable.
        return json.dumps(value, sort_keys=True)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _rank_surrogate_keys(
    rows: list[dict[str, Any]], key_columns: Sequence[str]
) -> list[dict[str, Any]]:
    """Replace a single-column integer primary key with its ``#rank``.

    Only fires for a one-column integer key — i.e. the ``id`` surrogate on
    ``merge_queue``/``audit_log``/``drive_queue``/…, never a natural key like
    ``issues(repo_name, number)`` or ``plans(assignment_id)``, whose values are
    genuine data and must be compared exactly.

    See the module docstring: absolute sequence values can legitimately differ
    between backends (a Postgres identity sequence is not rolled back by a
    failed statement), while *relative order* is what call sites depend on and
    is preserved exactly by rank.
    """
    if len(key_columns) != 1:
        return rows
    column = key_columns[0]
    values = [row.get(column) for row in rows]
    if not values or not all(isinstance(v, int) and not isinstance(v, bool) for v in values):
        return rows
    ranks = {v: i + 1 for i, v in enumerate(sorted(values))}  # type: ignore[arg-type]
    return [{**row, column: f"#{ranks[row[column]]}"} for row in rows]


@dataclasses.dataclass(frozen=True)
class Dump:
    """A whole database, canonicalised for comparison."""

    label: str
    #: table → column names, in schema order
    columns: dict[str, tuple[str, ...]]
    #: table → primary-key column names (possibly empty)
    keys: dict[str, tuple[str, ...]]
    #: table → rows, each a ``{column: canonical value}`` mapping
    rows: dict[str, list[dict[str, Any]]]

    @property
    def tables(self) -> tuple[str, ...]:
        return tuple(sorted(self.rows))

    def row_count(self, table: str) -> int:
        return len(self.rows.get(table, []))


def dump_database(
    conn: Any,
    *,
    label: str,
    tables: Sequence[str] | None = None,
    rank_surrogate_keys: bool = True,
) -> Dump:
    """Read every table on *conn* back out in canonical form.

    *tables*, when given, restricts the dump to those table names (a name that
    does not exist on *conn* is simply absent from the dump, which
    :func:`compare_dumps` then reports as a ``table-only-in-*`` difference
    rather than raising).  #3086's migration check needs this: the importer
    deliberately skips ``schema_version``/``schema_version_new`` — the target
    re-derives its own migration bookkeeping on connect — so an unrestricted
    dump of both sides would manufacture differences on exactly the two tables
    the importer is *correct* to leave alone.  Default ``None`` keeps #2885's
    whole-database behaviour, where "a new table joins the comparison
    automatically" is the point.

    *rank_surrogate_keys* controls the one normalisation that is
    context-dependent (see the module docstring): pass ``False`` when the two
    databases are supposed to hold *identical* ids — the migration importer
    copies every id verbatim, so id drift there is a real finding, not the
    benign sequence divergence #2885's independent replays produce.
    """
    columns: dict[str, tuple[str, ...]] = {}
    keys: dict[str, tuple[str, ...]] = {}
    rows: dict[str, list[dict[str, Any]]] = {}
    wanted = None if tables is None else set(tables)
    for table in table_names(conn):
        if wanted is not None and table not in wanted:
            continue
        table_cols = tuple(name for name, _type in sql.table_columns(conn, table))
        columns[table] = table_cols
        key_columns = primary_key(conn, table)
        keys[table] = key_columns
        fetched = sql.execute(conn, f"SELECT * FROM {table}").fetchall()  # noqa: S608
        table_rows = [
            {
                name: _canonical_value(_cell(row, name, index))
                for index, name in enumerate(table_cols)
            }
            for row in fetched
        ]
        rows[table] = (
            _rank_surrogate_keys(table_rows, key_columns)
            if rank_surrogate_keys
            else table_rows
        )
    return Dump(label=label, columns=columns, keys=keys, rows=rows)


# ── the diff ─────────────────────────────────────────────────────────────────

KIND_TABLE_ONLY_IN_A = "table-only-in-a"
KIND_TABLE_ONLY_IN_B = "table-only-in-b"
KIND_COLUMNS = "columns"
KIND_ROW_ONLY_IN_A = "row-only-in-a"
KIND_ROW_ONLY_IN_B = "row-only-in-b"
KIND_CELL = "cell"


@dataclasses.dataclass(frozen=True)
class Difference:
    """One concrete disagreement between two dumps."""

    table: str
    kind: str
    key: str
    column: str | None = None
    value_a: Any = None
    value_b: Any = None

    def render(self, label_a: str, label_b: str) -> str:
        if self.kind == KIND_TABLE_ONLY_IN_A:
            return f"  {self.table}: present in {label_a}, absent in {label_b}"
        if self.kind == KIND_TABLE_ONLY_IN_B:
            return f"  {self.table}: absent in {label_a}, present in {label_b}"
        if self.kind == KIND_COLUMNS:
            return (
                f"  {self.table}: column list differs\n"
                f"      {label_a}: {self.value_a}\n"
                f"      {label_b}: {self.value_b}"
            )
        if self.kind == KIND_ROW_ONLY_IN_A:
            return f"  {self.table}[{self.key}]: only in {label_a} -> {self.value_a}"
        if self.kind == KIND_ROW_ONLY_IN_B:
            return f"  {self.table}[{self.key}]: only in {label_b} -> {self.value_b}"
        return (
            f"  {self.table}[{self.key}].{self.column}: "
            f"{label_a}={self.value_a!r} {label_b}={self.value_b!r}"
        )


@dataclasses.dataclass(frozen=True)
class ParityReport:
    """The harness's output: **a diff, not a boolean**.

    ``bool(report)`` is deliberately *not* defined.  #2885's acceptance is that
    the mechanism "reports a diff, not a boolean", and a truthiness protocol is
    how a caller accidentally collapses it back into one.  Read
    :attr:`differences`, or print :meth:`render`.
    """

    label_a: str
    label_b: str
    differences: tuple[Difference, ...]
    tables_compared: tuple[str, ...]

    @property
    def is_clean(self) -> bool:
        return not self.differences

    def tables_with_differences(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for diff in self.differences:
            seen[diff.table] = None
        return tuple(seen)

    def for_table(self, table: str) -> tuple[Difference, ...]:
        return tuple(d for d in self.differences if d.table == table)

    def render(self) -> str:
        header = (
            f"write-path parity: {self.label_a} vs {self.label_b} "
            f"({len(self.tables_compared)} tables compared, "
            f"{len(self.differences)} difference(s))"
        )
        if self.is_clean:
            return f"{header}\n  no differences"
        lines = [header]
        for table in self.tables_with_differences():
            lines.append(f"{table}:")
            lines.extend(
                d.render(self.label_a, self.label_b) for d in self.for_table(table)
            )
        return "\n".join(lines)


def _row_key(row: dict[str, Any], key_columns: Sequence[str]) -> str:
    return "|".join(repr(row.get(c)) for c in key_columns)


def values_differ(value_a: Any, value_b: Any) -> bool:
    """Cell comparison, **type-aware** — the reason this is not a bare ``!=``.

    Python's ``1 == True`` and ``1 == 1.0`` are exactly the two equalities a
    storage-engine swap must not be allowed to hide:

    - ``True`` where the other backend has ``1``.  Every flag column in
      ``coord/db.py`` is declared ``INTEGER DEFAULT 0`` (``sql.FLAG_COLUMN_DDL``)
      precisely so the wire keeps shipping ``0``/``1``; a real ``BOOLEAN``
      column would serialise to JSON ``true``, which fails the parse of the
      **whole** ``BoardPayload`` and blanks the TUI board (#632/#546/#628).
      ``tests/test_store_contract.py`` guards this on the read side with the
      same ``isinstance(True, int)`` reasoning; this is its write-side twin.
    - ``1`` where the other backend has ``1.0``.  Same class of problem one
      column over: a JSON ``1`` and a JSON ``1.0`` deserialise differently into
      a typed Rust struct.

    A bare ``!=`` reports neither, and would make the module docstring's
    "``True`` is not folded into ``1``" a false claim.
    """
    if type(value_a) is not type(value_b):
        return True
    return value_a != value_b


def _row_signature(row: dict[str, Any], columns: Sequence[str]) -> tuple:
    """A row reduced to something hashable, sortable and **type-aware**.

    Carries the type name alongside each value for the same reason
    :func:`values_differ` exists — a plain tuple of values would compare equal
    across the ``True``/``1`` and ``1``/``1.0`` splits.
    """
    return tuple(
        (column, type(row.get(column)).__name__, repr(row.get(column)))
        for column in columns
    )


def _sort_key(row: dict[str, Any], columns: Sequence[str]) -> str:
    return json.dumps(_row_signature(row, columns))


def compare_dumps(dump_a: Dump, dump_b: Dump) -> ParityReport:
    """Diff two dumps, matching rows by primary key where one exists.

    Tables with no primary key (none in today's schema, but the harness must
    not assume that) fall back to a multiset comparison of whole rows: the
    report then says "this row is only on one side" rather than pinning a
    column, which is the most it can honestly claim without a key.
    """
    differences: list[Difference] = []
    tables = sorted(set(dump_a.rows) | set(dump_b.rows))
    for table in tables:
        in_a, in_b = table in dump_a.rows, table in dump_b.rows
        if not in_b:
            differences.append(Difference(table, KIND_TABLE_ONLY_IN_A, key=""))
            continue
        if not in_a:
            differences.append(Difference(table, KIND_TABLE_ONLY_IN_B, key=""))
            continue

        cols_a, cols_b = dump_a.columns[table], dump_b.columns[table]
        if set(cols_a) != set(cols_b):
            differences.append(
                Difference(
                    table,
                    KIND_COLUMNS,
                    key="",
                    value_a=sorted(cols_a),
                    value_b=sorted(cols_b),
                )
            )
            continue

        shared_columns = list(cols_a)
        key_columns = dump_a.keys.get(table) or dump_b.keys.get(table) or ()
        if key_columns:
            differences.extend(
                _compare_keyed(table, dump_a, dump_b, key_columns, shared_columns)
            )
        else:
            differences.extend(_compare_unkeyed(table, dump_a, dump_b, shared_columns))
    return ParityReport(
        label_a=dump_a.label,
        label_b=dump_b.label,
        differences=tuple(differences),
        tables_compared=tuple(tables),
    )


def _compare_keyed(
    table: str,
    dump_a: Dump,
    dump_b: Dump,
    key_columns: Sequence[str],
    columns: Sequence[str],
) -> list[Difference]:
    by_key_a = {_row_key(r, key_columns): r for r in dump_a.rows[table]}
    by_key_b = {_row_key(r, key_columns): r for r in dump_b.rows[table]}
    out: list[Difference] = []
    for key in sorted(set(by_key_a) - set(by_key_b)):
        out.append(Difference(table, KIND_ROW_ONLY_IN_A, key, value_a=by_key_a[key]))
    for key in sorted(set(by_key_b) - set(by_key_a)):
        out.append(Difference(table, KIND_ROW_ONLY_IN_B, key, value_b=by_key_b[key]))
    for key in sorted(set(by_key_a) & set(by_key_b)):
        row_a, row_b = by_key_a[key], by_key_b[key]
        for column in columns:
            if values_differ(row_a.get(column), row_b.get(column)):
                out.append(
                    Difference(
                        table,
                        KIND_CELL,
                        key,
                        column=column,
                        value_a=row_a.get(column),
                        value_b=row_b.get(column),
                    )
                )
    return out


def _compare_unkeyed(
    table: str, dump_a: Dump, dump_b: Dump, columns: Sequence[str]
) -> list[Difference]:
    sorted_a = sorted(dump_a.rows[table], key=lambda r: _sort_key(r, columns))
    sorted_b = sorted(dump_b.rows[table], key=lambda r: _sort_key(r, columns))
    remaining_b = [(_row_signature(r, columns), r) for r in sorted_b]
    out: list[Difference] = []
    for row in sorted_a:
        signature = _row_signature(row, columns)
        match = next((pair for pair in remaining_b if pair[0] == signature), None)
        if match is not None:
            remaining_b.remove(match)
        else:
            out.append(Difference(table, KIND_ROW_ONLY_IN_A, key="?", value_a=row))
    for _signature, row in remaining_b:
        out.append(Difference(table, KIND_ROW_ONLY_IN_B, key="?", value_b=row))
    return out

