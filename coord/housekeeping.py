"""#762: bound DB growth by archiving stale terminal board rows.

The ``coord serve`` board grew unbounded (every assignment ever dispatched stayed
in ``assignments`` forever), bloating the ``/board`` projection until it overran
the TUI's fetch timeout and blanked the whole board.  Part 1 caps the *wire*
(``coord.dao.board_projection``); this module bounds the *storage*: it **moves**
(never deletes) terminal assignments older than the archive window — plus their
notifications — into ``assignments_archive`` / ``notifications_archive`` so the
hot tables stay small while the cost/token/timing history is preserved for
analytics.

Two guarantees make the sweep safe:

* It reuses :func:`coord.dao.compute_board_keep_ids` (the *same* keep logic as the
  board projection) with a **wider** window than the projection, so the protected
  set here is always a superset of what the projection keeps — archiving can never
  drop a row the board still shows.
* It only ever archives rows whose status is terminal, as a belt-and-suspenders
  guard on top of the keep set.

The sweep runs automatically on a low-cadence daemon tick and on demand via
``coord housekeeping`` (which routes through the daemon — the canonical DB lives
there).
"""

from __future__ import annotations

import os
import sqlite3
import time

from coord import sql
from coord.dao import (
    TERMINAL_STATUSES,
    _KEEP_INDEX_COLUMNS,
    compute_board_keep_ids,
)
from coord.db import get_connection

# Terminal rows older than this many days (and not referenced by anything live)
# are eligible to move to the archive.  Deliberately wider than the board
# projection window (``COORD_BOARD_RETENTION_DAYS``, default 14) so the live
# table always retains everything the wire shows, with margin for any logic that
# reaches back through ``build_board``.  0 disables archiving.
_DEFAULT_ARCHIVE_RETENTION_DAYS = 30

_ASSIGNMENTS = "assignments"
_ASSIGNMENTS_ARCHIVE = "assignments_archive"
_NOTIFICATIONS = "notifications"
_NOTIFICATIONS_ARCHIVE = "notifications_archive"
_BATCH = 400  # keep IN(...) clauses well under SQLite's 999-variable limit


def _archive_retention_days() -> int:
    try:
        return int(
            os.environ.get(
                "COORD_ARCHIVE_RETENTION_DAYS", _DEFAULT_ARCHIVE_RETENTION_DAYS
            )
        )
    except (TypeError, ValueError):
        return _DEFAULT_ARCHIVE_RETENTION_DAYS


def _archive_cutoff(now: float | None = None) -> float | None:
    days = _archive_retention_days()
    if days <= 0:
        return None
    return (time.time() if now is None else now) - days * 86400.0


def _columns(conn: sqlite3.Connection, table: str) -> list[tuple[str, str]]:
    """Return ``[(name, type), ...]`` for *table* (empty if it doesn't exist)."""
    return [
        (r[1], r[2] or "TEXT")
        for r in sql.execute(conn, f"PRAGMA table_info({table})").fetchall()  # noqa: S608
    ]


def _ensure_archive_mirror(conn: sqlite3.Connection, src: str, dst: str) -> list[str]:
    """Create/extend *dst* so it has every column of *src* (no constraints — the
    archive is dumb storage).  Robust to future ``ALTER TABLE`` on *src*.

    Returns the shared column-name list to use for the copy.
    """
    src_cols = _columns(conn, src)
    dst_existing = {name for name, _ in _columns(conn, dst)}
    if not dst_existing:
        coldefs = ", ".join(f'"{name}" {ctype}' for name, ctype in src_cols)
        sql.execute(conn, f"CREATE TABLE {dst} ({coldefs})")  # noqa: S608
    else:
        for name, ctype in src_cols:
            if name not in dst_existing:
                sql.execute(conn, f'ALTER TABLE {dst} ADD COLUMN "{name}" {ctype}')  # noqa: S608
    return [name for name, _ in src_cols]


def _move_rows(
    conn: sqlite3.Connection,
    src: str,
    dst: str,
    key_col: str,
    ids: list[str],
) -> None:
    """Copy then delete rows of *src* whose *key_col* is in *ids* (batched)."""
    cols = _ensure_archive_mirror(conn, src, dst)
    collist = ", ".join(f'"{c}"' for c in cols)
    for i in range(0, len(ids), _BATCH):
        batch = ids[i : i + _BATCH]
        placeholders = ",".join("?" for _ in batch)
        sql.execute(
            conn,
            f"INSERT INTO {dst} ({collist}) SELECT {collist} FROM {src} "  # noqa: S608
            f"WHERE {key_col} IN ({placeholders})",
            batch,
        )
        sql.execute(
            conn,
            f"DELETE FROM {src} WHERE {key_col} IN ({placeholders})",  # noqa: S608
            batch,
        )


def sweep(*, dry_run: bool = False, now: float | None = None) -> dict:
    """Archive stale terminal assignments + their notifications.

    Returns ``{"archived_assignments": N, "archived_notifications": M,
    "dry_run": bool, "retention_days": D}``.  ``archived_*`` are the counts that
    were (or, for ``dry_run``, would be) moved.  A no-op returns zeros.

    Conservative by construction: nothing active, recent (within the archive
    window), queued-for-merge, latest-of-an-open-issue, or review-linked to any
    such row is ever moved.
    """
    cutoff = _archive_cutoff(now)
    result = {
        "archived_assignments": 0,
        "archived_notifications": 0,
        "dry_run": dry_run,
        "retention_days": _archive_retention_days(),
    }
    if cutoff is None:
        return result  # archiving disabled

    conn = get_connection()
    index = [
        dict(r)
        for r in sql.execute(
            conn, f"SELECT {_KEEP_INDEX_COLUMNS} FROM {_ASSIGNMENTS}"  # noqa: S608
        ).fetchall()
    ]
    mq_ids = {
        r["assignment_id"]
        for r in sql.execute(
            conn, f"SELECT assignment_id FROM merge_queue"  # noqa: S608
        ).fetchall()
        if r["assignment_id"]
    }
    open_keys = {
        (r["repo_name"], r["number"])
        for r in sql.execute(
            conn, "SELECT repo_name, number FROM issues WHERE LOWER(state) != 'closed'"
        ).fetchall()
    }
    protected = compute_board_keep_ids(index, mq_ids, open_keys, cutoff)

    candidates = [
        r["assignment_id"]
        for r in index
        if r["assignment_id"]
        and r["assignment_id"] not in protected
        and (r["status"] or "").lower() in TERMINAL_STATUSES
    ]
    candidate_set = set(candidates)

    # Notifications to archive: those belonging to an archived assignment, plus
    # old notifications not referencing a still-protected assignment.
    notif_ids = [
        r["assignment_id"]
        for r in sql.execute(
            conn, f"SELECT assignment_id, posted_at FROM {_NOTIFICATIONS}"  # noqa: S608
        ).fetchall()
        if r["assignment_id"]
        and (
            r["assignment_id"] in candidate_set
            or (
                (r["posted_at"] is not None and r["posted_at"] < cutoff)
                and r["assignment_id"] not in protected
            )
        )
    ]

    result["archived_assignments"] = len(candidates)
    result["archived_notifications"] = len(notif_ids)
    if dry_run or (not candidates and not notif_ids):
        return result

    with conn:
        if notif_ids:
            _move_rows(
                conn, _NOTIFICATIONS, _NOTIFICATIONS_ARCHIVE, "assignment_id", notif_ids
            )
        if candidates:
            _move_rows(
                conn, _ASSIGNMENTS, _ASSIGNMENTS_ARCHIVE, "assignment_id", candidates
            )
    return result
