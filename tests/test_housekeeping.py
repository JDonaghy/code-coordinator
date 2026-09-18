"""#3296: extend `coord housekeeping` to archive terminal `drive_queue` rows
and `plans` rows orphaned by an archived assignment.

`coord.housekeeping.sweep()` (#762/#1107) already moves stale terminal
`assignments`/`notifications`/`merge_queue` rows to their `_archive` tables
on a retention timer — move-not-delete, nothing ever deleted. A live-fleet
sample found the two tables this issue covers made up ~800 KB of every
`/board` poll with no retention policy at all: 1,114 `drive_queue` rows all
`state=done`, and 13 of 14 `plans` rows belonging to assignments no longer
on the board. These tests exercise the real write path (the sweep) and the
real read paths (`SqliteStore.board_projection` + the `/drive-queue` HTTP
route), not just a pure helper — the same shape as `tests/test_board_cap_762.py`.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from coord.dao import SqliteStore
from coord.db import _ensure_schema

NOW = time.time()
RECENT = NOW - 2 * 86400      # 2 days ago  → inside the 30d archive window
OLD = NOW - 40 * 86400        # 40 days ago → outside the archive window


@pytest.fixture(autouse=True)
def _isolated_confirm_worktrees_dir(monkeypatch, tmp_path):
    """See `tests/test_board_cap_762.py`'s fixture of the same name:
    `housekeeping.sweep()` always also sweeps `~/.coord/confirm-worktrees/`
    (#2974), so every test in this module that calls the real sweep needs
    `coord.state.COORD_DIR` pinned to a private tmp dir, or it would touch
    the operator's real `~/.coord/` as a side effect of running pytest.
    """
    monkeypatch.setattr("coord.state.COORD_DIR", tmp_path / "coord-state")


def _ins_assignment(
    conn: sqlite3.Connection,
    aid: str,
    *,
    status: str,
    issue: int,
    repo: str = "r",
    dispatched_at: float | None = None,
    finished_at: float | None = None,
) -> None:
    conn.execute(
        "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
        "issue_number, issue_title, status, type, dispatched_at, finished_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (aid, "m", repo, issue, f"#{issue}", status, "work", dispatched_at, finished_at),
    )


def _ins_issue(conn: sqlite3.Connection, number: int, state: str, repo: str = "r") -> None:
    conn.execute(
        "INSERT INTO issues (repo_name, number, state) VALUES (?,?,?)",
        (repo, number, state),
    )


def _ins_drive_queue(
    conn: sqlite3.Connection,
    repo: str,
    issue: int,
    *,
    position: int,
    state: str,
    enqueued_at: float,
    reason_at: float | None = None,
    launched_at: float | None = None,
) -> None:
    conn.execute(
        "INSERT INTO drive_queue (repo_name, issue_number, position, state, "
        "enqueued_at, reason_at, launched_at) VALUES (?,?,?,?,?,?,?)",
        (repo, issue, position, state, enqueued_at, reason_at, launched_at),
    )


def _ins_plan(conn: sqlite3.Connection, assignment_id: str, plan_data: str = "{}") -> None:
    conn.execute(
        "INSERT INTO plans (assignment_id, plan_data) VALUES (?, ?)",
        (assignment_id, plan_data),
    )


# ── drive_queue archival ───────────────────────────────────────────────────

def test_housekeeping_archives_old_terminal_drive_queue(coord_db, monkeypatch):
    monkeypatch.setenv("COORD_ARCHIVE_RETENTION_DAYS", "30")
    from coord import housekeeping

    conn = coord_db
    _ins_drive_queue(conn, "r", 1, position=0, state="waiting", enqueued_at=OLD)
    _ins_drive_queue(
        conn, "r", 2, position=1, state="done", enqueued_at=RECENT, reason_at=RECENT
    )
    _ins_drive_queue(
        conn, "r", 3, position=2, state="done", enqueued_at=OLD, reason_at=OLD
    )
    _ins_drive_queue(
        conn, "r", 4, position=3, state="blocked", enqueued_at=OLD, reason_at=OLD
    )
    # No reason_at/launched_at at all → falls back to enqueued_at, same
    # fallback chain merge_queue's archival already relies on.
    _ins_drive_queue(conn, "r", 5, position=4, state="failed", enqueued_at=OLD)
    conn.commit()

    dry = housekeeping.sweep(dry_run=True)
    assert dry["archived_drive_queue"] == 3 and dry["dry_run"] is True
    assert conn.execute("SELECT COUNT(*) FROM drive_queue").fetchone()[0] == 5

    res = housekeeping.sweep()
    assert res["archived_drive_queue"] == 3
    live = {r[0] for r in conn.execute("SELECT issue_number FROM drive_queue")}
    arch = {r[0] for r in conn.execute("SELECT issue_number FROM drive_queue_archive")}
    assert live == {1, 2}          # non-terminal + recent-terminal untouched
    assert arch == {3, 4, 5}        # old done/blocked/failed all moved
    # conservation: nothing lost
    assert len(live) + len(arch) == 5


def test_housekeeping_drive_queue_archive_row_shape_preserved(coord_db, monkeypatch):
    """The archived row keeps its full column set (position, after_json, ...)
    — dumb-mirror storage, not a lossy projection."""
    monkeypatch.setenv("COORD_ARCHIVE_RETENTION_DAYS", "30")
    from coord import housekeeping

    conn = coord_db
    _ins_drive_queue(conn, "r", 9, position=0, state="done", enqueued_at=OLD, reason_at=OLD)
    conn.commit()

    housekeeping.sweep()
    row = conn.execute(
        "SELECT repo_name, issue_number, state, position FROM drive_queue_archive"
    ).fetchone()
    assert tuple(row) == ("r", 9, "done", 0)


# ── plans archival ─────────────────────────────────────────────────────────

def test_housekeeping_archives_orphaned_plans(coord_db, monkeypatch):
    monkeypatch.setenv("COORD_ARCHIVE_RETENTION_DAYS", "30")
    from coord import housekeeping

    conn = coord_db
    _ins_assignment(conn, "active", status="running", issue=1, dispatched_at=OLD)
    _ins_assignment(
        conn, "old_closed", status="merged", issue=2, dispatched_at=OLD, finished_at=OLD
    )
    _ins_issue(conn, 2, "closed")
    _ins_plan(conn, "active")       # assignment stays live → plan stays
    _ins_plan(conn, "old_closed")   # assignment archived THIS sweep → plan archived
    _ins_plan(conn, "long_gone")    # no matching assignment at all → pre-existing orphan
    conn.commit()

    dry = housekeeping.sweep(dry_run=True)
    assert dry["archived_plans"] == 2 and dry["dry_run"] is True
    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 3

    res = housekeeping.sweep()
    assert res["archived_plans"] == 2
    live = {r[0] for r in conn.execute("SELECT assignment_id FROM plans")}
    arch = {r[0] for r in conn.execute("SELECT assignment_id FROM plans_archive")}
    assert live == {"active"}
    assert arch == {"old_closed", "long_gone"}
    # conservation: nothing lost
    assert len(live) + len(arch) == 3


# ── board_projection excludes archived rows (real read path, file-backed) ──

@pytest.fixture
def file_db(tmp_path):
    path = tmp_path / "coord.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    yield path, conn
    conn.close()


def test_board_projection_excludes_archived_drive_queue_and_plans(file_db, monkeypatch):
    monkeypatch.setenv("COORD_ARCHIVE_RETENTION_DAYS", "30")
    from coord import db, housekeeping

    path, conn = file_db
    _ins_assignment(
        conn, "old_closed", status="merged", issue=3, dispatched_at=OLD, finished_at=OLD
    )
    _ins_issue(conn, 3, "closed")
    _ins_drive_queue(conn, "r", 1, position=0, state="done", enqueued_at=OLD, reason_at=OLD)
    _ins_drive_queue(conn, "r", 2, position=1, state="waiting", enqueued_at=RECENT)
    _ins_plan(conn, "old_closed")
    conn.commit()

    # housekeeping.sweep() reads/writes via coord.db.get_connection() — point
    # that singleton at this same file-backed connection so the sweep and the
    # projection read agree, mirroring test_board_cap_762's daemon-route test.
    db.override_connection(conn)
    housekeeping.sweep()

    proj = SqliteStore(path).board_projection()
    dq_issues = {e["issue_number"] for e in proj["drive_queue"]}
    assert dq_issues == {2}                 # the archived 'done' row is gone
    assert proj["plans"] == {}               # the orphaned plan is gone


# ── GET /drive-queue?state= reaches the archive (black-box, real route) ────

def test_drive_queue_endpoint_state_param_reads_archived_history(
    tmp_path, valid_config_path, monkeypatch
):
    monkeypatch.setenv("COORD_ARCHIVE_RETENTION_DAYS", "30")
    from starlette.testclient import TestClient

    from coord import db, housekeeping
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    path = tmp_path / "coord.db"
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    db.override_connection(conn)
    _ins_drive_queue(conn, "r", 7, position=0, state="done", enqueued_at=OLD, reason_at=OLD)
    _ins_drive_queue(conn, "r", 8, position=1, state="waiting", enqueued_at=RECENT)
    conn.commit()
    housekeeping.sweep()  # moves issue 7 to drive_queue_archive

    app = build_app(SqliteStore(path), load_config(valid_config_path))
    with TestClient(app) as cli:
        # Un-shipped-by-default: no `state` filter → archived row stays hidden.
        default = cli.get("/drive-queue", params={"repo_name": "r"}).json()
        assert {e["issue_number"] for e in default["entries"]} == {8}

        # Explicit `state=done` reaches into the archive → archived row returns.
        by_state = cli.get(
            "/drive-queue", params={"repo_name": "r", "state": "done"}
        ).json()
        assert {e["issue_number"] for e in by_state["entries"]} == {7}

        # Point lookup by (repo_name, issue_number) + state also finds it.
        point = cli.get(
            "/drive-queue",
            params={"repo_name": "r", "issue_number": 7, "state": "done"},
        ).json()
        assert {e["issue_number"] for e in point["entries"]} == {7}

        # Same point lookup with NO state param stays un-shipped-by-default.
        point_no_state = cli.get(
            "/drive-queue", params={"repo_name": "r", "issue_number": 7}
        ).json()
        assert point_no_state["entries"] == []
