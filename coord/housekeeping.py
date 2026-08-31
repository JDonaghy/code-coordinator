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

#1107 Part 3: the same sweep also archives ``merge_queue`` rows in the
``MERGED`` terminal state once they're older than the retention window, into
``merge_queue_archive`` — mirroring the move-not-delete pattern above.
``merge_queue`` has no natural "close" event of its own (unlike assignments,
which age out via ``TERMINAL_STATUSES``), so without this a MERGED row lives
forever: 192 such rows had accumulated by 2026-07-12 and 61 more by
2026-07-30, both requiring a manual one-off ``DELETE``/``coord merge --drop``
declog. ``coord.merge_queue.merged_issue_keys()`` unions the live table with
the archive so the "already merged, don't re-enqueue" dedup in
``enqueue_approved_work``/``staging_items`` keeps working after a row ages
out.

#2974: this is also the one existing low-cadence, filesystem-safe hook the
daemon already calls on a timer (and that ``coord housekeeping`` calls on
demand) — so it is where :func:`coord.confirm_test.sweep_stale_confirm_worktrees`
is wired in too, reclaiming ``~/.coord/confirm-worktrees/`` entries that
outlived ``confirm_branch``'s own per-run cleanup. That is disk hygiene, not
DB archiving, but reusing this sweep's cadence beats inventing a second timer
for a second maintenance job.
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
_MERGE_QUEUE = "merge_queue"
_MERGE_QUEUE_ARCHIVE = "merge_queue_archive"
_MERGE_QUEUE_MERGED_STATE = "merged"
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
    """Return ``[(name, type), ...]`` for *table* (empty if it doesn't exist).

    Delegates to :func:`coord.sql.table_columns` (#2782) -- SQLite's
    ``PRAGMA table_info`` has no Postgres equivalent, so the dialect split
    lives in the seam, not here.
    """
    return sql.table_columns(conn, table)


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
    """Archive stale terminal assignments + their notifications + merged
    merge_queue entries, and reclaim leaked confirm-worktree directories.

    Returns ``{"archived_assignments": N, "archived_notifications": M,
    "archived_merge_queue": K, "removed_confirm_worktrees": W, "dry_run": bool,
    "retention_days": D}``. ``archived_*``/``removed_confirm_worktrees`` are
    the counts that were (or, for ``dry_run``, would be) moved/deleted.  A
    no-op returns zeros.

    Conservative by construction: nothing active, recent (within the archive
    window), queued-for-merge, latest-of-an-open-issue, or review-linked to any
    such row is ever moved. Merge queue entries are archived only once they
    are in the terminal ``MERGED`` state (#1107 Part 3) — non-terminal /
    conflicted entries are left for ``prune_stale_queue_entries`` or manual
    ``coord merge --drop``.

    #2974: ``removed_confirm_worktrees`` is independent of the DB-archiving
    ``cutoff``/``retention_days`` above (it has its own, much shorter,
    :data:`coord.confirm_test.STALE_WORKTREE_MAX_AGE_HOURS` window) and runs
    even when DB archiving itself is disabled (``COORD_ARCHIVE_RETENTION_DAYS``
    ``<= 0``) — a disk leak on the daemon host is not something an operator
    who merely turned off DB archiving meant to also keep.
    """
    from coord.confirm_test import sweep_stale_confirm_worktrees  # noqa: PLC0415

    swept_worktrees = sweep_stale_confirm_worktrees(dry_run=dry_run, now=now)

    cutoff = _archive_cutoff(now)
    result = {
        "archived_assignments": 0,
        "archived_notifications": 0,
        "archived_merge_queue": 0,
        "removed_confirm_worktrees": len(swept_worktrees["removed"]),
        "dry_run": dry_run,
        "retention_days": _archive_retention_days(),
    }
    if cutoff is None:
        return result  # DB archiving disabled; the worktree sweep above still ran

    conn = get_connection()
    index = [
        dict(r)
        for r in sql.execute(
            conn, f"SELECT {_KEEP_INDEX_COLUMNS} FROM {_ASSIGNMENTS}"  # noqa: S608
        ).fetchall()
    ]
    mq_rows = sql.execute(
        conn,
        "SELECT assignment_id, state, last_attempt, enqueued_at "  # noqa: S608
        f"FROM {_MERGE_QUEUE}",
    ).fetchall()
    mq_ids = {r["assignment_id"] for r in mq_rows if r["assignment_id"]}
    mq_merged_ids = [
        r["assignment_id"]
        for r in mq_rows
        if r["assignment_id"]
        and (r["state"] or "").lower() == _MERGE_QUEUE_MERGED_STATE
        and (r["last_attempt"] or r["enqueued_at"] or 0) < cutoff
    ]
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
    result["archived_merge_queue"] = len(mq_merged_ids)
    if dry_run or (not candidates and not notif_ids and not mq_merged_ids):
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
        if mq_merged_ids:
            _move_rows(
                conn, _MERGE_QUEUE, _MERGE_QUEUE_ARCHIVE, "assignment_id", mq_merged_ids
            )
    return result
