"""Read-only data-access layer for the coordinator board (#584/#589).

``CoordStore`` is the seam that lets the ``coord serve`` daemon front the
board's **read** path without callers knowing the storage engine.  It
abstracts the daemon's board projection — ``GET /board`` and the point reads
the detail endpoints serve — and nothing else.

The write path is **not** here.  Routing writes through the daemon landed in
``coord.state`` + ``coord.board_service`` (#590), not in this module: every
public write in ``coord.state`` resolves ``board_service`` and either POSTs to
the daemon or falls through to a ``_*_local()`` SQL function.  That
``_*_local()`` family (the ``_record_*_local`` / ``_update_*_local`` /
``_mark_*_local`` functions in ``coord/state.py``, plus the ``issue_store`` and
``commands/acceptance`` analogues) is the single write choke point — #1036
hooked the audit log there for exactly that reason — and is the portability
seam a future Postgres backend (#282/#828) would actually swap.  DB-API 2.0
abstracts the *connection and cursor protocol*, not the SQL dialect or the
parameter style: ``sqlite3.paramstyle`` is ``qmark`` (``?``) and ``psycopg``'s
is ``pyformat`` (``%s``), and this tree has on the order of 220 ``?``
placeholders plus SQLite-only idioms (``INSERT OR REPLACE``,
``AUTOINCREMENT``, ``PRAGMA``/WAL, ``lastrowid``) that a connection swap alone
would not touch — the portability work is a dialect seam, not a drop-in
(#1948).  A ``CoordStore``-style write interface would still buy a cleaner
API, just not portability that is otherwise missing (#1948).

``SqliteStore`` is the concrete SQLite implementation of this read contract.
It owns its *own* read-only connection (NOT ``coord.db``'s read/write
singleton): it opens the DB with ``mode=ro`` + ``PRAGMA query_only`` and never
runs schema/migration DDL, so it is safe to point at a live ``coord.db`` that
the coordinator process is writing in WAL mode.

#2766: every statement this module runs routes through ``coord.sql`` (the
dialect seam, #2719/#1948) rather than calling ``conn.execute()`` directly —
paramstyle translation, the row factory, and connection setup are the seam's
job now, not hand-rolled here. The read-only ``PRAGMA query_only=ON`` (no
Postgres connect-time equivalent — see ``sql.apply_connection_setup``'s
``read_only`` flag) and ``busy_timeout`` both come from that one seam call
instead of two bare PRAGMAs.

#827 (superseding the #2766 decision note that used to live here): the
``mode=ro`` URI itself has now moved into ``coord.sql.connect`` too. #2766
kept it in this module's ``_connect()`` because "there is no live Postgres
connection factory yet for the seam to branch on" — #827 is that factory, so
the premise no longer holds, and ``SqliteStore._connect`` now calls
``sql.connect(backend=..., ...)`` like every other connection opener in the
tree rather than naming ``sqlite3.connect`` directly (enforced by
``tests/test_sql_dialect.py``'s connect-call ratchet, the sibling of #2768's
execute-call one).

**Which backend** ``_connect()`` opens is resolved through
``coord.db._resolve_store_target()`` — the identical decision
``coord.db.get_connection()``'s write path makes — rather than this class
hardcoding SQLite (#827 review fix: an earlier version of this PR left this
class's name doing double duty as its behavior, so ``store.backend:
postgres`` silently split-brained the daemon's read path from its write
path — see the issue's blocking-finding history). ``SqliteStore`` still owns
*which* backend it opens — nothing here forces a future ``PostgresStore`` to
share this class, and this class is not renamed because nothing about its
read contract or its "never migrates" invariant changes — it just no longer
assumes the answer is always SQLite, matching ``coord.db``'s write path one
config read away.

All SQLite idioms (JSON-encoded TEXT columns, the ``mode=ro`` URI) that
remain are encapsulated here: read methods return plain Python dicts with
JSON columns decoded to native lists/objects, so neither the wire format nor
a future non-SQLite backend inherits any SQLite-only idiom.

#1849: the *shape* of those dicts is no longer this module's business either.
The seven board projections are defined by the dataclasses in
``coord/board_schema.py``, and :func:`_decode_row` projects each row through
its DTO — so the ``GET /board`` wire contract is a property of the declared
schema rather than of whichever storage engine (and whichever migration state)
happens to be underneath.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from coord import board_schema, sql
from coord import db as db_mod
from coord.db import DB_PATH

# Bump when the /board payload shape changes incompatibly.  Clients may branch
# on this; today everything is additive.
#
# #1943: this is the *maximum* schema version the daemon can serve. A client
# negotiates against it via the ``X-Coord-Schema`` request header (see the
# schema-negotiation middleware in ``coord.serve_app``) -- absent or ``1``
# means today's shape, byte-identical to pre-#1943 responses. The minimum
# served version is :data:`MIN_SCHEMA_VERSION`, below.
SCHEMA_VERSION = 1

# #1943: the oldest schema version the daemon still serves. A client whose
# ``X-Coord-Schema`` falls outside ``[MIN_SCHEMA_VERSION, SCHEMA_VERSION]``
# gets a clear 4xx naming this range -- never a silent downgrade to v1, which
# would look like success while quietly shipping the wrong shape. Equal to
# ``SCHEMA_VERSION`` until a v2 body exists and versions start being retired.
MIN_SCHEMA_VERSION = 1

# #762: terminal assignment statuses.  Anything NOT in this set (running /
# pending) is "in-flight" and always kept on the board projection.
# #2234: "refused_policy" is terminal exactly like "advisory" — see
# coord.agent.REFUSED_POLICY.
TERMINAL_STATUSES = frozenset(
    {"done", "merged", "failed", "cancelled", "advisory", "refused_policy"}
)

# #762: how many days of *terminal* assignment history the /board projection
# carries.  The board grew unbounded (1209 assignments / 4.37 MB) and overran
# the TUI's fetch timeout, blanking the whole board.  Cap the wire to active +
# pipeline-referenced + this many days of terminal rows.  0 disables the cap
# (serve everything — the pre-#762 behaviour).
_DEFAULT_BOARD_RETENTION_DAYS = 14


def _board_retention_days() -> int:
    try:
        return int(os.environ.get("COORD_BOARD_RETENTION_DAYS", _DEFAULT_BOARD_RETENTION_DAYS))
    except (TypeError, ValueError):
        return _DEFAULT_BOARD_RETENTION_DAYS


# #1037: recency window (seconds) behind the /board `audit_recent_count`
# summary field — see SqliteStore._audit_recent_count.
_AUDIT_RECENT_WINDOW_SECONDS = 900


def _board_retention_cutoff(now: float | None = None) -> float | None:
    """Unix-timestamp floor for terminal rows on the board, or None when the
    cap is disabled (``COORD_BOARD_RETENTION_DAYS=0``)."""
    days = _board_retention_days()
    if days <= 0:
        return None
    return (time.time() if now is None else now) - days * 86400.0


# #1849: the wire shape of the seven board projections is defined by the
# dataclasses in ``coord/board_schema.py``, NOT by whatever columns
# ``SELECT *`` happens to return.  The hand-curated ``_JSON_COLUMNS`` /
# ``_DROP_COLUMNS`` tables that used to live here (patches over the leak) are
# gone: a JSON-encoded TEXT column is now a field typed ``list[str]``/``dict``
# on its DTO, and a column the board must not carry (e.g.
# ``assignments.briefing``, ~8 MB of an ~12 MB live payload) is simply absent
# from the DTO.  ``review_findings`` stays a plain ``str`` field on purpose —
# the coord-tui client consumes it as a raw JSON string (``Option<String>``).


@runtime_checkable
class CoordStore(Protocol):
    """Read interface over coordinator board state.

    This is the read contract the ``coord serve`` daemon's board projection
    serves — the ``list_*`` collection reads, the point reads the detail
    endpoints use, and the ``board_projection`` snapshot.  Writes are not part
    of this protocol: they live in ``coord.state``'s ``_*_local()`` family (see
    the module docstring) and route through ``coord.board_service``.
    ``SqliteStore`` implements every method below; none raise
    ``NotImplementedError``.
    """

    # ── reads ────────────────────────────────────────────────────────────────
    def list_assignments(self) -> list[dict]: ...
    def list_machines(self) -> list[dict]: ...
    def list_merge_queue(self) -> list[dict]: ...
    def list_drive_escalations(self) -> list[dict]: ...
    def list_drive_queue(self) -> list[dict]: ...
    def list_proposals(self) -> list[dict]: ...
    def list_issues(self) -> list[dict]: ...
    def list_plans(self) -> dict[str, Any]: ...
    def list_notifications(self) -> list[dict]: ...
    def board_meta(self) -> dict[str, str]: ...
    def round_number(self) -> int: ...
    def board_projection(self) -> dict: ...

    # ── point reads (#1336/#1337: detail endpoints) ──────────────────────────
    def get_assignment(self, assignment_id: str) -> dict | None: ...
    def get_issue(self, repo_name: str, number: int) -> dict | None: ...


def compute_board_keep_ids(
    assignment_index: list[dict],
    merge_queue_ids: set[str],
    open_issue_keys: set[tuple[str, int]],
    cutoff: float | None,
) -> set[str]:
    """Return the set of ``assignment_id``s the /board projection must carry.

    Pure function (no DB) so the read path (:meth:`SqliteStore.board_projection`)
    and the #762 archival sweep apply *identical* rules.

    ``assignment_index`` rows need only: ``assignment_id``, ``repo_name``,
    ``issue_number``, ``status``, ``dispatched_at``, ``finished_at``,
    ``review_of_assignment_id``.

    A row is kept when it is **active** (status not terminal), **recent** (its
    finish — or dispatch when never finished — is within the retention window),
    **queued for merge**, or the **latest assignment of a still-open issue**.
    The kept set is then closed over ``review_of_assignment_id`` in both
    directions, so an in-flight item never loses its work↔review pairing.

    ``cutoff=None`` (cap disabled) keeps everything.
    """
    by_id: dict[str, dict] = {
        r["assignment_id"]: r for r in assignment_index if r.get("assignment_id")
    }
    if cutoff is None:
        return set(by_id)

    keep: set[str] = set()
    latest_open: dict[tuple, tuple[float, str]] = {}
    for r in assignment_index:
        aid = r.get("assignment_id")
        if not aid:
            continue
        status = (r.get("status") or "").lower()
        if status not in TERMINAL_STATUSES:
            keep.add(aid)
        else:
            ts = r.get("finished_at")
            if ts is None:
                ts = r.get("dispatched_at")
            # Conservative: only drop a terminal row we can POSITIVELY date as
            # old.  An undatable terminal row (no finish/dispatch timestamp) is
            # kept rather than risk dropping something we can't age.
            if ts is None or ts >= cutoff:
                keep.add(aid)
            elif aid in merge_queue_ids:
                keep.add(aid)
        # Track the most-recently-dispatched assignment per open issue so an
        # open pipeline card never loses its latest state to the age cap.
        key = (r.get("repo_name"), r.get("issue_number"))
        if key in open_issue_keys:
            disp = r.get("dispatched_at") or 0.0
            cur = latest_open.get(key)
            if cur is None or disp >= cur[0]:
                latest_open[key] = (disp, aid)
    for _, aid in latest_open.values():
        keep.add(aid)

    # Closure over review links (work → its reviews, review → its work).
    reviews_of: dict[str, list[str]] = {}
    for aid, r in by_id.items():
        tgt = r.get("review_of_assignment_id")
        if tgt:
            reviews_of.setdefault(tgt, []).append(aid)
    frontier = list(keep)
    while frontier:
        aid = frontier.pop()
        tgt = by_id.get(aid, {}).get("review_of_assignment_id")
        if tgt and tgt in by_id and tgt not in keep:
            keep.add(tgt)
            frontier.append(tgt)
        for rev in reviews_of.get(aid, ()):
            if rev not in keep:
                keep.add(rev)
                frontier.append(rev)
    return keep


_KEEP_INDEX_COLUMNS = (
    "assignment_id, repo_name, issue_number, status, dispatched_at, "
    "finished_at, review_of_assignment_id"
)


def _decode_row(table: str, row: board_schema.RowLike, *, full: bool = False) -> dict:
    """A DB-API row (``sqlite3.Row``, or a future dialect's mapping row) →
    the plain dict the wire carries.

    #1849: for the seven board tables this projects the row through that
    table's DTO in ``coord/board_schema.py`` — only declared fields survive,
    in declared order, with JSON-encoded TEXT columns decoded — so a later
    ``ALTER TABLE ... ADD COLUMN`` cannot silently widen ``/board``.  A table
    with no DTO (e.g. ``notifications``) is passed through unchanged, exactly
    as before.

    ``full=True`` keeps every column (including ones the board projection
    omits, e.g. ``assignments.briefing``) and applies only the JSON decoding —
    used by the single-resource *detail* reads (#1336/#1337), which serve the
    complete row; the collection projection stays slim.
    """
    return board_schema.decode_row(table, row, full=full)


class SqliteStore:
    """Read-only :class:`CoordStore`, SQLite by default, Postgres when
    ``coordinator.yml``'s ``store: backend: postgres`` opts in (#827 — see
    :func:`_connect` and the module docstring's "Which backend" note; the
    class keeps its pre-#827 name since its read contract and its "never
    migrates the DB" invariant are unchanged either way).

    Opens a fresh connection per call (cheap for SQLite, thread-safe under
    the daemon's request handling; a Postgres deployment gets the same
    "fresh connection per call" for now — #829 decides whether that needs
    pooling under real load) and never migrates the DB. ``board_projection``
    opens a single connection so the whole payload is one consistent
    snapshot.
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._path = Path(db_path) if db_path is not None else DB_PATH

    # ── connection ────────────────────────────────────────────────────────────
    def _connect(self) -> Any:
        # #827 (review fix): which backend to open is resolved through the
        # SAME dialect decision `coord.db.get_connection()`'s write path
        # uses -- `coord.db._resolve_store_target()` -- rather than this
        # class hardcoding SQLite. `SqliteStore` still owns *which* backend
        # it opens (there is no separate `PostgresStore`); it just no longer
        # assumes the answer is always SQLite. `self._path` (an explicit or
        # default on-disk path) is only meaningful for the SQLite branch --
        # a Postgres deployment has no path, only `store.dsn`.
        target = db_mod._resolve_store_target()
        if target.backend == sql.DIALECT_POSTGRES:
            # #1960's SQLite write-path guard has a Postgres analogue for
            # exactly this read path -- see `coord.db.refuse_postgres_under_pytest`.
            db_mod.refuse_postgres_under_pytest(
                "the configured production Postgres store (dao read path)"
            )
            conn = sql.connect(backend=sql.DIALECT_POSTGRES, dsn=target.dsn)
        else:
            # #827: the `mode=ro` URI construction now lives in `sql.connect`
            # — see the module docstring's updated decision note. This is a
            # fresh connection per call (cheap for SQLite, thread-safe under
            # the daemon's request handling), matching
            # `check_same_thread=False`. #829 measures whether a Postgres
            # deployment needs anything more than "fresh connection per
            # call" too -- this issue only needs the read path to be
            # *capable* of reaching Postgres, not tuned for it yet.
            conn = sql.connect(
                backend=sql.DIALECT_SQLITE,
                sqlite_path=self._path,
                read_only=True,
                check_same_thread=False,
            )
        sql.apply_row_factory(conn)
        # #2159: without a busy_timeout SQLite fails a locked read INSTANTLY
        # (the default is 0ms) instead of waiting out a writer's momentary
        # hold — this is the daemon's `/board` read path, hit by every thin
        # client's `coord drive-queue tick`, `coord notify`, and the web
        # dashboard, so it is also the connection most exposed to lock
        # contention scaling with the fleet. `read_only=True` also sets
        # `PRAGMA query_only=ON` (SQLite) / the read-only session/transaction
        # flag (Postgres) instead of the writer's WAL/foreign_keys pragmas
        # (a `mode=ro` SQLite connection can't write the WAL toggle) — see
        # `sql.apply_connection_setup`.
        sql.apply_connection_setup(conn, read_only=True)
        return conn

    # ── internal builders (take an open connection) ────────────────────────────
    def _table(self, conn: sqlite3.Connection, table: str, order: str | None = None) -> list[dict]:
        stmt = f"SELECT * FROM {table}"  # noqa: S608 — table names are literals, not user input
        if order:
            stmt += f" ORDER BY {order}"
        return [_decode_row(table, r) for r in sql.execute(conn, stmt).fetchall()]

    def _plans(self, conn: sqlite3.Connection) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for r in sql.execute(conn, "SELECT assignment_id, plan_data FROM plans").fetchall():
            try:
                out[r["assignment_id"]] = json.loads(r["plan_data"])
            except (json.JSONDecodeError, TypeError):
                continue
        return out

    def _board_meta(self, conn: sqlite3.Connection) -> dict[str, str]:
        # Served as raw strings; each client parses the keys it knows (the TUI
        # JSON-decodes pipeline_* keys, mirroring its local-SQLite behaviour).
        rows = sql.execute(conn, "SELECT key, value FROM board_meta").fetchall()
        return {r["key"]: r["value"] for r in rows}

    def _round_number(self, conn: sqlite3.Connection) -> int:
        row = sql.execute(
            conn, "SELECT value FROM board_meta WHERE key = 'round_number'"
        ).fetchone()
        try:
            return int(row["value"]) if row else 0
        except (TypeError, ValueError):
            return 0

    def _audit_recent_count(self, conn: sqlite3.Connection) -> int:
        """#1037: single-integer summary for the TUI activity-bar badge —
        rows written to ``audit_log`` in the last :data:`_AUDIT_RECENT_WINDOW_SECONDS`.
        A real "unseen since I last looked" count needs per-client read-cursor
        state that doesn't exist yet; a rolling recency window is the
        forward-compatible stand-in the issue asks for (#1039 can replace the
        semantics without a wire-shape change — it's still one integer).
        Fail-open to 0 so a missing/pre-migration table never 503s the board.

        #2983: that fail-open needs a rollback to actually be fail-open on
        Postgres. This runs inside a caller's ``with closing(self._connect())``
        block alongside many other reads (``_board_meta``, ``_round_number``,
        every ``_table`` call in ``board_projection``), and Postgres aborts the
        whole transaction on a failed statement — so a missing ``audit_log``
        took out every *sibling* read on that connection too, producing
        precisely the 503 this guard exists to prevent. Nothing uncommitted
        can be lost: ``SqliteStore``'s connections are opened read-only.
        """
        try:
            cutoff = time.time() - _AUDIT_RECENT_WINDOW_SECONDS
            row = sql.execute(
                conn, "SELECT COUNT(*) AS n FROM audit_log WHERE ts >= ?", (cutoff,)
            ).fetchone()
            return int(row["n"]) if row else 0
        except sql.driver_error(conn) as exc:
            db_mod.rollback_after_driver_error(conn, exc)  # #2983: caller reuses `conn`
            return 0

    # ── public reads ────────────────────────────────────────────────────────────
    def list_assignments(self) -> list[dict]:
        with closing(self._connect()) as conn:
            return self._table(conn, "assignments", order="dispatched_at DESC")

    def list_machines(self) -> list[dict]:
        with closing(self._connect()) as conn:
            return self._table(conn, "machines", order="name")

    def list_merge_queue(self) -> list[dict]:
        with closing(self._connect()) as conn:
            return self._table(conn, "merge_queue", order="id")

    def list_drive_escalations(self) -> list[dict]:
        with closing(self._connect()) as conn:
            return self._table(conn, "drive_escalations", order="id")

    def list_drive_queue(self) -> list[dict]:
        with closing(self._connect()) as conn:
            return self._table(conn, "drive_queue", order="position")

    def list_proposals(self) -> list[dict]:
        with closing(self._connect()) as conn:
            return self._table(conn, "proposals", order="id")

    def list_issues(self) -> list[dict]:
        with closing(self._connect()) as conn:
            return self._table(conn, "issues", order="repo_name, number")

    def list_plans(self) -> dict[str, Any]:
        with closing(self._connect()) as conn:
            return self._plans(conn)

    def list_notifications(self) -> list[dict]:
        with closing(self._connect()) as conn:
            return self._table(conn, "notifications")

    def board_meta(self) -> dict[str, str]:
        with closing(self._connect()) as conn:
            return self._board_meta(conn)

    def round_number(self) -> int:
        with closing(self._connect()) as conn:
            return self._round_number(conn)

    # ── point reads (#1336/#1337) ────────────────────────────────────────────
    def get_assignment(self, assignment_id: str) -> dict | None:
        """One full assignment row (JSON columns decoded), or ``None``.

        Unlike the collection projection this keeps every column — including
        ``briefing`` and the unbounded free-text fields the collection wire
        bounds/previews — because a detail read is one row by definition,
        so it can never grow with board size.  Zero third-party I/O.
        """
        with closing(self._connect()) as conn:
            row = sql.execute(
                conn,
                "SELECT * FROM assignments WHERE assignment_id = ?",
                (assignment_id,),
            ).fetchone()
            return _decode_row("assignments", row, full=True) if row is not None else None

    def get_issue(self, repo_name: str, number: int) -> dict | None:
        """One full issue row (labels decoded, full ``body``), or ``None``."""
        with closing(self._connect()) as conn:
            row = sql.execute(
                conn,
                "SELECT * FROM issues WHERE repo_name = ? AND number = ?",
                (repo_name, number),
            ).fetchone()
            return _decode_row("issues", row, full=True) if row is not None else None

    def _board_keep_ids(self, conn: sqlite3.Connection, cutoff: float | None) -> set[str]:
        """#762: assignment_ids the projection must carry (see
        :func:`compute_board_keep_ids`)."""
        index = [
            dict(r)
            for r in sql.execute(
                conn, f"SELECT {_KEEP_INDEX_COLUMNS} FROM assignments"
            ).fetchall()
        ]
        mq_ids = {
            r["assignment_id"]
            for r in sql.execute(conn, "SELECT assignment_id FROM merge_queue").fetchall()
            if r["assignment_id"]
        }
        open_keys = {
            (r["repo_name"], r["number"])
            for r in sql.execute(
                conn, "SELECT repo_name, number FROM issues WHERE LOWER(state) != 'closed'"
            ).fetchall()
        }
        return compute_board_keep_ids(index, mq_ids, open_keys, cutoff)

    def _capped_assignments(self, conn: sqlite3.Connection, keep: set[str] | None) -> list[dict]:
        rows = sql.execute(
            conn, "SELECT * FROM assignments ORDER BY dispatched_at DESC"
        ).fetchall()
        if keep is None:
            return [_decode_row("assignments", r) for r in rows]
        return [
            _decode_row("assignments", r)
            for r in rows
            if r["assignment_id"] in keep
        ]

    def _capped_notifications(
        self, conn: sqlite3.Connection, keep: set[str] | None, cutoff: float | None
    ) -> list[dict]:
        rows = sql.execute(conn, "SELECT * FROM notifications").fetchall()
        if cutoff is None:
            return [_decode_row("notifications", r) for r in rows]
        out: list[dict] = []
        for r in rows:
            posted = r["posted_at"]
            if (posted is not None and posted >= cutoff) or (
                keep is not None and r["assignment_id"] in keep
            ):
                out.append(_decode_row("notifications", r))
        return out

    def board_projection(self) -> dict:
        """The full ``GET /board`` payload — one consistent snapshot.

        A superset of what the Rust TUI's ``load_data()`` reads from SQLite
        today, minus the live machine-reachability probes (the client keeps
        doing those itself over the tailnet).

        #762: assignments + notifications are capped to active + pipeline-
        referenced + the last ``COORD_BOARD_RETENTION_DAYS`` (default 14) of
        terminal rows, so the wire can't grow unbounded and overrun the TUI's
        fetch timeout (which blanks the whole board on any error).
        """
        with closing(self._connect()) as conn:
            cutoff = _board_retention_cutoff()
            keep = self._board_keep_ids(conn, cutoff) if cutoff is not None else None
            return {
                "schema_version": SCHEMA_VERSION,
                "round_number": self._round_number(conn),
                "assignments": self._capped_assignments(conn, keep),
                "machines": self._table(conn, "machines", order="name"),
                "merge_queue": self._table(conn, "merge_queue", order="id"),
                "proposals": self._table(conn, "proposals", order="id"),
                "issues": self._table(conn, "issues", order="repo_name, number"),
                "plans": self._plans(conn),
                "notifications": self._capped_notifications(conn, keep, cutoff),
                "board_meta": self._board_meta(conn),
                "audit_recent_count": self._audit_recent_count(conn),
                # #1505: board-visible driver-escalation records — see
                # coord/db.py's drive_escalations table docstring. Small,
                # unbounded-but-tiny table (one row per stuck issue, cleared
                # on dismiss), so no retention cap like assignments/
                # notifications above.
                "escalations": self._table(conn, "drive_escalations", order="id"),
                # #1753: the operator-declared `coord drive` queue, in the
                # order it will run (dense, 0-based `position`). Same
                # rationale as `escalations` above — bounded by hand, so no
                # retention cap; `after_json` arrives decoded as a real list.
                "drive_queue": self._table(conn, "drive_queue", order="position"),
            }
