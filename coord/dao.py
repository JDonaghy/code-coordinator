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
already abstracts ``sqlite3`` vs ``psycopg`` at the connection layer, so the
port keeps the raw-SQL shape and swaps the connection rather than rewriting the
call sites into store methods; a ``CoordStore``-style write interface would buy
a cleaner API, not portability that is otherwise missing.

``SqliteStore`` is the concrete SQLite implementation of this read contract.
It owns its *own* read-only connection (NOT ``coord.db``'s read/write
singleton): it opens the DB with ``mode=ro`` + ``PRAGMA query_only`` and never
runs schema/migration DDL, so it is safe to point at a live ``coord.db`` that
the coordinator process is writing in WAL mode.

All SQLite idioms (``sqlite3.Row``, JSON-encoded TEXT columns, the ``mode=ro``
URI) are encapsulated here: read methods return plain Python dicts with JSON
columns decoded to native lists/objects, so neither the wire format nor a future
non-SQLite backend inherits any SQLite-only idiom.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from coord.db import DB_PATH

# Bump when the /board payload shape changes incompatibly.  Clients may branch
# on this; today everything is additive.
SCHEMA_VERSION = 1

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

# JSON-encoded columns per table — decoded to native objects on read so no
# SQLite idiom (JSON-in-TEXT) leaks past the DAO.  Columns added by later ALTER
# migrations are picked up automatically via ``SELECT *``; only JSON ones need
# listing here.
_JSON_COLUMNS: dict[str, set[str]] = {
    "assignments": {
        "files_allowed",
        "files_forbidden",
        "required_gates",
        "plan",
        "smoke_tests",
        "test_plan",
        # NOTE: review_findings is deliberately NOT decoded — the coord-tui
        # client consumes it as a raw JSON string (Option<String>), so it must
        # stay a string on the wire.
    },
    "proposals": {"files_likely", "required_gates"},
    "merge_queue": {"required_gates"},
    "issues": {"labels"},
    "machines": {"capabilities", "repos"},
    # #1753: the drive queue's pre-req list — ["repo#N", ...] on the wire, a
    # JSON string in SQLite.
    "drive_queue": {"after_json"},
}

# Columns omitted from the board projection.  ``assignments.briefing`` is ~8 MB
# of an ~12 MB live payload and is NOT part of the board view (the TUI's board
# query never selects it; the Python mapper defaults it to ""), so dropping it
# keeps refreshes fast over Tailscale.  A per-assignment endpoint can serve full
# briefings later if a detail view needs them.
_DROP_COLUMNS: dict[str, set[str]] = {
    "assignments": {"briefing"},
}


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


def _decode_row(table: str, row: sqlite3.Row, *, full: bool = False) -> dict:
    """sqlite3.Row → plain dict with that table's JSON columns decoded.

    ``full=True`` keeps the :data:`_DROP_COLUMNS` fields (e.g.
    ``assignments.briefing``) — used by the single-resource *detail* reads
    (#1336/#1337), which serve the complete row; the collection projection
    stays slim.
    """
    d = dict(row)
    if not full:
        for col in _DROP_COLUMNS.get(table, ()):
            d.pop(col, None)
    for col in _JSON_COLUMNS.get(table, ()):
        val = d.get(col)
        if isinstance(val, (str, bytes, bytearray)):
            try:
                d[col] = json.loads(val) if val else None
            except (json.JSONDecodeError, TypeError):
                d[col] = None
    return d


class SqliteStore:
    """Read-only SQLite-backed :class:`CoordStore`.

    Opens a fresh ``mode=ro`` connection per call (cheap for SQLite, thread-safe
    under the daemon's request handling, and never migrates the DB).
    ``board_projection`` opens a single connection so the whole payload is one
    consistent snapshot.
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._path = Path(db_path) if db_path is not None else DB_PATH

    # ── connection ────────────────────────────────────────────────────────────
    def _connect(self) -> sqlite3.Connection:
        uri = f"file:{self._path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        # #2159: without a busy_timeout SQLite fails a locked read INSTANTLY
        # (the default is 0ms) instead of waiting out a writer's momentary
        # hold — this is the daemon's `/board` read path, hit by every thin
        # client's `coord drive-queue tick`, `coord notify`, and the web
        # dashboard, so it is also the connection most exposed to lock
        # contention scaling with the fleet. Same value as the writer
        # connection's own `busy_timeout` (`coord.db._open`), so a read-only
        # request waits out a writer exactly as long as another writer would.
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    # ── internal builders (take an open connection) ────────────────────────────
    def _table(self, conn: sqlite3.Connection, table: str, order: str | None = None) -> list[dict]:
        sql = f"SELECT * FROM {table}"  # noqa: S608 — table names are literals, not user input
        if order:
            sql += f" ORDER BY {order}"
        return [_decode_row(table, r) for r in conn.execute(sql).fetchall()]

    def _plans(self, conn: sqlite3.Connection) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for r in conn.execute("SELECT assignment_id, plan_data FROM plans").fetchall():
            try:
                out[r["assignment_id"]] = json.loads(r["plan_data"])
            except (json.JSONDecodeError, TypeError):
                continue
        return out

    def _board_meta(self, conn: sqlite3.Connection) -> dict[str, str]:
        # Served as raw strings; each client parses the keys it knows (the TUI
        # JSON-decodes pipeline_* keys, mirroring its local-SQLite behaviour).
        return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM board_meta").fetchall()}

    def _round_number(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT value FROM board_meta WHERE key = 'round_number'").fetchone()
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
        """
        try:
            cutoff = time.time() - _AUDIT_RECENT_WINDOW_SECONDS
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM audit_log WHERE ts >= ?", (cutoff,)
            ).fetchone()
            return int(row["n"]) if row else 0
        except sqlite3.Error:
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
            row = conn.execute(
                "SELECT * FROM assignments WHERE assignment_id = ?",
                (assignment_id,),
            ).fetchone()
            return _decode_row("assignments", row, full=True) if row is not None else None

    def get_issue(self, repo_name: str, number: int) -> dict | None:
        """One full issue row (labels decoded, full ``body``), or ``None``."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM issues WHERE repo_name = ? AND number = ?",
                (repo_name, number),
            ).fetchone()
            return _decode_row("issues", row, full=True) if row is not None else None

    def _board_keep_ids(self, conn: sqlite3.Connection, cutoff: float | None) -> set[str]:
        """#762: assignment_ids the projection must carry (see
        :func:`compute_board_keep_ids`)."""
        index = [
            dict(r)
            for r in conn.execute(f"SELECT {_KEEP_INDEX_COLUMNS} FROM assignments").fetchall()
        ]
        mq_ids = {
            r["assignment_id"]
            for r in conn.execute("SELECT assignment_id FROM merge_queue").fetchall()
            if r["assignment_id"]
        }
        open_keys = {
            (r["repo_name"], r["number"])
            for r in conn.execute(
                "SELECT repo_name, number FROM issues WHERE LOWER(state) != 'closed'"
            ).fetchall()
        }
        return compute_board_keep_ids(index, mq_ids, open_keys, cutoff)

    def _capped_assignments(self, conn: sqlite3.Connection, keep: set[str] | None) -> list[dict]:
        rows = conn.execute(
            "SELECT * FROM assignments ORDER BY dispatched_at DESC"
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
        rows = conn.execute("SELECT * FROM notifications").fetchall()
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
