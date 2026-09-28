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
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

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


# ── #3469: operational-tier audit retention + one-off reclaim ──────────────

def test_sweep_wires_audit_operational_retention(coord_db, monkeypatch):
    """`housekeeping.sweep()` calls `coord.audit.sweep_operational_retention`
    and reports its count -- independent of `COORD_ARCHIVE_RETENTION_DAYS`
    (0 here disables DB archiving; the audit sweep must still run, same as
    the confirm-worktree sweep already does)."""
    monkeypatch.setenv("COORD_ARCHIVE_RETENTION_DAYS", "0")
    monkeypatch.setattr("coord.audit._resolve_operational_retention_days", lambda: 7.0)
    from coord import housekeeping
    from coord.audit import record_audit

    conn = coord_db
    old_ts = NOW - 30 * 86400.0
    record_audit(
        tier="operational", category="reconcile", event_type="passive_reconcile",
        actor="daemon", summary="old operational", ts=old_ts,
    )
    record_audit(
        tier="business", category="merge", event_type="merged",
        actor="coordinator", summary="old business", ts=old_ts,
    )
    conn.commit()

    res = housekeeping.sweep(now=NOW)

    assert res["audit_operational_deleted"] == 1
    rows = {r[0] for r in conn.execute("SELECT summary FROM audit_log")}
    assert rows == {"old business"}


def test_sweep_dry_run_does_not_delete_audit_rows(coord_db, monkeypatch):
    monkeypatch.setattr("coord.audit._resolve_operational_retention_days", lambda: 7.0)
    from coord import housekeeping
    from coord.audit import record_audit

    conn = coord_db
    old_ts = NOW - 30 * 86400.0
    record_audit(
        tier="operational", category="reconcile", event_type="passive_reconcile",
        actor="daemon", summary="old operational", ts=old_ts,
    )
    conn.commit()

    res = housekeeping.sweep(dry_run=True, now=NOW)

    assert res["audit_operational_deleted"] == 1  # would-be count, reported
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 1


def test_sweep_reclaim_false_by_default_leaves_reclaimed_false(coord_db):
    from coord import housekeeping

    res = housekeeping.sweep(now=NOW)

    assert res["reclaimed"] is False


def test_sweep_reclaim_true_runs_vacuum(coord_db):
    from coord import housekeeping

    res = housekeeping.sweep(now=NOW, reclaim=True)

    assert res["reclaimed"] is True


def test_reclaim_space_runs_vacuum_on_sqlite(coord_db):
    """Dialect-routed through `coord.sql.reclaim_space` -- a plain call must
    not raise against the suite's real (in-memory or file) SQLite
    connection."""
    from coord import housekeeping

    assert housekeeping.reclaim_space(coord_db) is True


# ── #3469 review: `--reclaim` must actually be reachable from an operator ──
#
# `sweep(reclaim=...)`/`reclaim_space()` above were real from the start, but
# nothing operator-facing could ever pass `reclaim=True`: `coord housekeeping`
# only accepted `--dry-run`, and the daemon's `POST /housekeeping` route only
# read `dry_run` out of the request body. These tests pin the two surfaces
# that close that gap so the wiring can't silently regress back to
# unreachable.

def test_cli_housekeeping_reclaim_flag_runs_local_sweep_with_reclaim(coord_db, monkeypatch):
    """`coord housekeeping --reclaim`, run with no board service configured
    (i.e. this process *is* local/host mode), must call `housekeeping.sweep`
    with `reclaim=True` -- not just leave the flag on the ground."""
    from coord.cli import main

    monkeypatch.setattr("coord.board_service.resolve", lambda: None)
    fake_result = {
        "archived_assignments": 0, "archived_notifications": 0,
        "removed_confirm_worktrees": 0, "audit_operational_deleted": 0,
        "reclaimed": True, "dry_run": False, "retention_days": 30,
    }
    with patch("coord.housekeeping.sweep", return_value=fake_result) as mock_sweep:
        runner = CliRunner()
        result = runner.invoke(main, ["housekeeping", "--reclaim"])

    assert result.exit_code == 0, result.output
    mock_sweep.assert_called_once_with(dry_run=False, reclaim=True)
    assert "reclaimed disk space" in result.output


def test_cli_housekeeping_without_reclaim_flag_defaults_reclaim_false(coord_db, monkeypatch):
    from coord.cli import main

    monkeypatch.setattr("coord.board_service.resolve", lambda: None)
    fake_result = {
        "archived_assignments": 0, "archived_notifications": 0,
        "removed_confirm_worktrees": 0, "audit_operational_deleted": 0,
        "reclaimed": False, "dry_run": False, "retention_days": 30,
    }
    with patch("coord.housekeeping.sweep", return_value=fake_result) as mock_sweep:
        runner = CliRunner()
        result = runner.invoke(main, ["housekeeping"])

    assert result.exit_code == 0, result.output
    mock_sweep.assert_called_once_with(dry_run=False, reclaim=False)


def test_cli_housekeeping_reclaim_flag_posts_to_daemon_when_board_service_configured(
    coord_db, monkeypatch
):
    """Thin-client mode: `coord housekeeping --reclaim` must forward
    `reclaim: true` in the POST /housekeeping body, not just `dry_run`."""
    from coord.cli import main

    fake_service = object()
    monkeypatch.setattr("coord.board_service.resolve", lambda: fake_service)
    fake_result = {
        "archived_assignments": 0, "archived_notifications": 0,
        "removed_confirm_worktrees": 0, "audit_operational_deleted": 0,
        "reclaimed": True, "dry_run": False, "retention_days": 30,
    }
    mock_post = MagicMock(return_value=fake_result)
    with patch("coord.client.post_record", mock_post):
        runner = CliRunner()
        result = runner.invoke(main, ["housekeeping", "--reclaim"])

    assert result.exit_code == 0, result.output
    args, kwargs = mock_post.call_args
    assert args[0] is fake_service
    assert args[1] == "/housekeeping"
    assert args[2] == {"dry_run": False, "reclaim": True}


def test_post_housekeeping_route_passes_reclaim_flag_through_to_sweep(
    file_db, monkeypatch, valid_config_path
):
    """The daemon's `POST /housekeeping` handler must read `reclaim` out of
    the request body and pass it to `housekeeping.sweep`, mirroring `dry_run`
    -- this is the other half of the operator-facing surface #3469's `--reclaim`
    needs to actually reach `sql.reclaim_space`.  Reuses the module's `file_db`
    fixture (see above) rather than opening a fresh `sqlite3.connect` --
    #2884's ratchet pins the connect-site count per test file."""
    from starlette.testclient import TestClient

    from coord.config import load as load_config
    from coord.serve_app import build_app

    path, _conn = file_db

    captured = {}

    def _fake_sweep(*, dry_run=False, reclaim=False, now=None):
        captured["dry_run"] = dry_run
        captured["reclaim"] = reclaim
        return {
            "archived_assignments": 0, "archived_notifications": 0,
            "removed_confirm_worktrees": 0, "audit_operational_deleted": 0,
            "reclaimed": reclaim, "dry_run": dry_run, "retention_days": 30,
        }

    monkeypatch.setattr("coord.housekeeping.sweep", _fake_sweep)

    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(path), cfg)
    with TestClient(app) as cli:
        resp = cli.post("/housekeeping", json={"dry_run": False, "reclaim": True})

    assert resp.status_code == 200, resp.text
    assert captured == {"dry_run": False, "reclaim": True}
    assert resp.json()["reclaimed"] is True
