"""Tests for coord.db — schema creation, migration, connection override."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from coord import db as db_mod
from coord.db import (
    _ensure_schema,
    override_connection,
    close,
    _migrate_gate_order,
    retry_on_locked,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def isolated_conn():
    """Each test in this file uses an in-memory DB via the coord_db fixture pattern."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    override_connection(conn)
    yield conn
    close()


# ── Schema creation ────────────────────────────────────────────────────────────

class TestSchemaCreation:
    EXPECTED_TABLES = {
        "schema_version",
        "assignments",
        "notifications",
        "proposals",
        "split_proposals",
        "split_chunks",
        "merge_queue",
        "plans",
        "sessions",
        "machines",
        "board_meta",
        "issue_comments",
    }

    def test_all_tables_exist(self, isolated_conn: sqlite3.Connection) -> None:
        rows = isolated_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = {r["name"] for r in rows}
        assert self.EXPECTED_TABLES.issubset(names)

    def test_schema_version_row_inserted(self, isolated_conn: sqlite3.Connection) -> None:
        row = isolated_conn.execute("SELECT version FROM schema_version").fetchone()
        assert row is not None
        assert row["version"] == db_mod._DB_SCHEMA_VERSION

    def test_indexes_exist(self, isolated_conn: sqlite3.Connection) -> None:
        rows = isolated_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
        names = {r["name"] for r in rows}
        assert "idx_assignments_status" in names
        assert "idx_assignments_machine" in names
        assert "idx_merge_queue_state" in names

    def test_idempotent_multiple_calls(self, isolated_conn: sqlite3.Connection) -> None:
        """Calling _ensure_schema again should not raise."""
        _ensure_schema(isolated_conn)  # second call
        rows = isolated_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        assert len(rows) >= len(self.EXPECTED_TABLES)


# ── issue_comments (#873) ────────────────────────────────────────────────────

class TestIssueCommentsSchema:
    def test_index_exists(self, isolated_conn: sqlite3.Connection) -> None:
        rows = isolated_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
        names = {r["name"] for r in rows}
        assert "idx_issue_comments_issue" in names

    def test_gh_comment_id_unique(self, isolated_conn: sqlite3.Connection) -> None:
        isolated_conn.execute(
            "INSERT INTO issue_comments (gh_comment_id, repo_name, issue_number, body) "
            "VALUES (111, 'api', 1, 'first')"
        )
        isolated_conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            isolated_conn.execute(
                "INSERT INTO issue_comments (gh_comment_id, repo_name, issue_number, body) "
                "VALUES (111, 'api', 1, 'dupe')"
            )

    def test_multiple_null_gh_comment_ids_allowed(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        """SQLite treats NULL as distinct under UNIQUE — rows whose comment
        id couldn't be resolved at capture-at-write time (rare) don't
        collide with each other."""
        isolated_conn.execute(
            "INSERT INTO issue_comments (repo_name, issue_number, body) "
            "VALUES ('api', 1, 'a')"
        )
        isolated_conn.execute(
            "INSERT INTO issue_comments (repo_name, issue_number, body) "
            "VALUES ('api', 1, 'b')"
        )
        isolated_conn.commit()
        count = isolated_conn.execute(
            "SELECT COUNT(*) c FROM issue_comments"
        ).fetchone()["c"]
        assert count == 2

    def test_body_ref_column_present_and_unused(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        """The Azure-blob offload seam column exists but is never populated
        by current code — reserved for a future body_ref migration."""
        cols = {
            r[1] for r in isolated_conn.execute(
                "PRAGMA table_info(issue_comments)"
            ).fetchall()
        }
        assert "body_ref" in cols


# ── drive_queue deploy-gate columns (#1757) ───────────────────────────────────

# DQ-1 (#1753) shipped `drive_queue` WITHOUT the gate columns and merged on
# 2026-08-03, so the "fold them into the CREATE TABLE" window closed. The
# upgrade-in-place path below is therefore the one that runs on every existing
# ~/.coord/coord.db, and it is the one that must be tested — a fresh-DB-only
# test would pass while every real installation kept the old five-column-short
# table and every `coord drive-queue` read blew up on `no such column`.

_DQ1_ORIGINAL_TABLE = """
    CREATE TABLE drive_queue (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        repo_name     TEXT    NOT NULL,
        issue_number  INTEGER NOT NULL,
        position      INTEGER NOT NULL,
        machine       TEXT,
        after_json    TEXT    NOT NULL DEFAULT '[]',
        state         TEXT    NOT NULL DEFAULT 'waiting',
        attempts      INTEGER NOT NULL DEFAULT 0,
        deferrals     INTEGER NOT NULL DEFAULT 0,
        last_reason   TEXT    NOT NULL DEFAULT '',
        session_name  TEXT,
        launched_at   REAL,
        enqueued_at   REAL    NOT NULL,
        UNIQUE(repo_name, issue_number)
    )
"""

_GATE_COLUMNS = {
    "hold_after",
    "hold_reason",
    "resume_when",
    "hold_state",
    "hold_probes",
}


def _drive_queue_columns(conn: sqlite3.Connection) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(drive_queue)").fetchall()}


class TestDriveQueueDeployGateColumns:
    def test_fresh_database_has_them_from_the_create(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        assert _GATE_COLUMNS <= _drive_queue_columns(isolated_conn)

    def test_existing_dq1_database_gains_them_in_place(self) -> None:
        """The real path: a coord.db created by DQ-1, upgraded by _ensure_schema.

        Built with DQ-1's ORIGINAL `CREATE TABLE` (not the current one) so this
        test keeps asserting the migration even after the CREATE grew the
        columns — otherwise `CREATE TABLE IF NOT EXISTS` would quietly make the
        migration untested.
        """
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(_DQ1_ORIGINAL_TABLE)
        conn.execute(
            "INSERT INTO drive_queue "
            "(repo_name, issue_number, position, after_json, enqueued_at) "
            "VALUES ('api', 7, 0, '[]', 100.0)"
        )
        conn.commit()
        assert not (_GATE_COLUMNS & _drive_queue_columns(conn))

        _ensure_schema(conn)

        assert _GATE_COLUMNS <= _drive_queue_columns(conn)
        # The pre-existing row survives and reads as "no gate" — an upgraded
        # database must not spontaneously hold anybody's queue.
        row = conn.execute(
            "SELECT hold_after, hold_reason, resume_when, hold_state, hold_probes "
            "FROM drive_queue WHERE issue_number = 7"
        ).fetchone()
        assert row["hold_after"] == 0
        assert row["hold_reason"] == ""
        assert row["resume_when"] == ""
        assert row["hold_state"] == ""
        assert row["hold_probes"] == 0
        conn.close()

    def test_migration_is_idempotent(self) -> None:
        """Re-running _ensure_schema on an already-migrated DB must not raise."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(_DQ1_ORIGINAL_TABLE)
        conn.commit()
        _ensure_schema(conn)
        _ensure_schema(conn)
        _ensure_schema(conn)
        assert _GATE_COLUMNS <= _drive_queue_columns(conn)
        conn.close()

    def test_state_accessors_read_the_upgraded_columns(self) -> None:
        """`_DRIVE_QUEUE_COLUMNS` names them, so a stale table would 500 here."""
        from coord import state

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(_DQ1_ORIGINAL_TABLE)
        conn.commit()
        _ensure_schema(conn)
        override_connection(conn)
        try:
            state._enqueue_drive_queue_local(
                "api", 9, hold_after=True, hold_reason="restart coord-serve"
            )
            entry = state._get_drive_queue_entry_local("api", 9)
        finally:
            close()
        assert entry["hold_after"] == 1
        assert entry["hold_reason"] == "restart coord-serve"
        assert entry["hold_state"] == "armed"


# ── drive_queue.hold_scope column (#2186) ───────────────────────────────────

# DQ-1 + #1757's five gate columns, but predating #2186's `hold_scope` — the
# real upgrade path for every ~/.coord/coord.db that already has a deploy
# gate declared on it.
_PRE_2186_TABLE_WITH_GATES = """
    CREATE TABLE drive_queue (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        repo_name     TEXT    NOT NULL,
        issue_number  INTEGER NOT NULL,
        position      INTEGER NOT NULL,
        machine       TEXT,
        after_json    TEXT    NOT NULL DEFAULT '[]',
        state         TEXT    NOT NULL DEFAULT 'waiting',
        attempts      INTEGER NOT NULL DEFAULT 0,
        deferrals     INTEGER NOT NULL DEFAULT 0,
        last_reason   TEXT    NOT NULL DEFAULT '',
        session_name  TEXT,
        launched_at   REAL,
        enqueued_at   REAL    NOT NULL,
        hold_after    INTEGER NOT NULL DEFAULT 0,
        hold_reason   TEXT    NOT NULL DEFAULT '',
        resume_when   TEXT    NOT NULL DEFAULT '',
        hold_state    TEXT    NOT NULL DEFAULT '',
        hold_probes   INTEGER NOT NULL DEFAULT 0,
        UNIQUE(repo_name, issue_number)
    )
"""


class TestDriveQueueHoldScopeColumn:
    def test_fresh_database_has_it_from_the_create(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        assert "hold_scope" in _drive_queue_columns(isolated_conn)

    def test_existing_pre_2186_database_gains_it_in_place(self) -> None:
        """The real path: a gate already declared before #2186 shipped.

        Built with the pre-#2186 `CREATE TABLE` (gate columns present,
        `hold_scope` absent) — including a row with a FIRED gate, the exact
        shape the 2026-08-13 incident's queue was in — so the migration is
        exercised the same way `_ensure_schema` will actually hit it in the
        field, not just on an empty table.
        """
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(_PRE_2186_TABLE_WITH_GATES)
        conn.execute(
            "INSERT INTO drive_queue "
            "(repo_name, issue_number, position, after_json, enqueued_at, "
            " hold_after, hold_reason, hold_state) "
            "VALUES ('api', 2146, 0, '[]', 100.0, 1, 'deploy', 'fired')"
        )
        conn.commit()
        assert "hold_scope" not in _drive_queue_columns(conn)

        _ensure_schema(conn)

        assert "hold_scope" in _drive_queue_columns(conn)
        row = conn.execute(
            "SELECT hold_scope FROM drive_queue WHERE issue_number = 2146"
        ).fetchone()
        # The whole point of #2186: a gate that predates the scope column
        # upgrades to the NARROW reading, never to a silent fleet-wide stop.
        assert row["hold_scope"] == "entry"
        conn.close()

    def test_migration_is_idempotent(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(_PRE_2186_TABLE_WITH_GATES)
        conn.commit()
        _ensure_schema(conn)
        _ensure_schema(conn)
        _ensure_schema(conn)
        assert "hold_scope" in _drive_queue_columns(conn)
        conn.close()

    def test_state_accessor_reads_and_writes_the_upgraded_column(self) -> None:
        from coord import state

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(_PRE_2186_TABLE_WITH_GATES)
        conn.commit()
        _ensure_schema(conn)
        override_connection(conn)
        try:
            state._enqueue_drive_queue_local(
                "api", 9, hold_after=True, hold_reason="deploy", hold_scope="fleet"
            )
            entry = state._get_drive_queue_entry_local("api", 9)
        finally:
            close()
        assert entry["hold_scope"] == "fleet"


# ── drive_queue.resumes column (#2230) ──────────────────────────────────────

# Everything up to and including #2186's `hold_scope` — the real upgrade path
# for every ~/.coord/coord.db in the field right now, since #2230 is the
# first migration to add a column after it.
_PRE_2230_TABLE = """
    CREATE TABLE drive_queue (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        repo_name     TEXT    NOT NULL,
        issue_number  INTEGER NOT NULL,
        position      INTEGER NOT NULL,
        machine       TEXT,
        after_json    TEXT    NOT NULL DEFAULT '[]',
        state         TEXT    NOT NULL DEFAULT 'waiting',
        attempts      INTEGER NOT NULL DEFAULT 0,
        deferrals     INTEGER NOT NULL DEFAULT 0,
        last_reason   TEXT    NOT NULL DEFAULT '',
        reason_at     REAL,
        session_name  TEXT,
        launched_at   REAL,
        enqueued_at   REAL    NOT NULL,
        hold_after    INTEGER NOT NULL DEFAULT 0,
        hold_reason   TEXT    NOT NULL DEFAULT '',
        resume_when   TEXT    NOT NULL DEFAULT '',
        hold_state    TEXT    NOT NULL DEFAULT '',
        hold_probes   INTEGER NOT NULL DEFAULT 0,
        launch_host   TEXT    NOT NULL DEFAULT '',
        hold_scope    TEXT    NOT NULL DEFAULT 'entry',
        UNIQUE(repo_name, issue_number)
    )
"""


class TestDriveQueueResumesColumn:
    def test_fresh_database_has_it_from_the_create(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        assert "resumes" in _drive_queue_columns(isolated_conn)

    def test_existing_pre_2230_database_gains_it_in_place(self) -> None:
        """The real path: a `blocked` row written before #2230 shipped —
        including one that is currently `blocked`, the exact shape #2230's
        sweep must be able to read `resumes=0` off without raising.
        """
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(_PRE_2230_TABLE)
        conn.execute(
            "INSERT INTO drive_queue "
            "(repo_name, issue_number, position, after_json, enqueued_at, "
            " state, attempts, last_reason) "
            "VALUES ('quadraui', 309, 0, '[]', 100.0, 'blocked', 2, "
            "'drive session died without landing the work 2/2 times')"
        )
        conn.commit()
        assert "resumes" not in _drive_queue_columns(conn)

        _ensure_schema(conn)

        assert "resumes" in _drive_queue_columns(conn)
        row = conn.execute(
            "SELECT resumes FROM drive_queue WHERE issue_number = 309"
        ).fetchone()
        # A row predating this column has never been auto-resumed — 0, not
        # NULL, so `QueueEntry.from_row`'s `int(row.get('resumes') or 0)`
        # never has to guess.
        assert row["resumes"] == 0
        conn.close()

    def test_migration_is_idempotent(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(_PRE_2230_TABLE)
        conn.commit()
        _ensure_schema(conn)
        _ensure_schema(conn)
        _ensure_schema(conn)
        assert "resumes" in _drive_queue_columns(conn)
        conn.close()

    def test_state_accessor_reads_and_writes_the_upgraded_column(self) -> None:
        from coord import state

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(_PRE_2230_TABLE)
        conn.commit()
        _ensure_schema(conn)
        override_connection(conn)
        try:
            state._enqueue_drive_queue_local("quadraui", 309)
            state._update_drive_queue_entry_local(
                "quadraui", 309, state="waiting", attempts=0, resumes=1
            )
            entry = state._get_drive_queue_entry_local("quadraui", 309)
        finally:
            close()
        assert entry["resumes"] == 1


# ── merge_queue.ci_infra_reruns column (#1892) ──────────────────────────────

_PRE_1892_MERGE_QUEUE_TABLE = """
    CREATE TABLE merge_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        assignment_id TEXT NOT NULL,
        repo_name TEXT NOT NULL,
        repo_github TEXT NOT NULL,
        branch TEXT NOT NULL,
        target_branch TEXT NOT NULL,
        issue_number INTEGER NOT NULL,
        issue_title TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending',
        pr_number INTEGER,
        pr_url TEXT,
        size INTEGER,
        last_attempt REAL,
        error TEXT,
        enqueued_at REAL,
        assignment_type TEXT DEFAULT 'work',
        required_gates TEXT
    )
"""


def _merge_queue_columns(conn: sqlite3.Connection) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(merge_queue)").fetchall()}


class TestMergeQueueCiInfraRerunsColumn:
    def test_fresh_database_has_it_from_the_create(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        assert "ci_infra_reruns" in _merge_queue_columns(isolated_conn)

    def test_existing_database_gains_it_in_place(self) -> None:
        """The real path: a coord.db created before #1892, upgraded by
        _ensure_schema — built with the pre-#1892 `CREATE TABLE` (not the
        current one) so this keeps testing the migration even after the
        CREATE itself grew the column."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(_PRE_1892_MERGE_QUEUE_TABLE)
        conn.execute(
            "INSERT INTO merge_queue "
            "(assignment_id, repo_name, repo_github, branch, target_branch, "
            "issue_number, issue_title) "
            "VALUES ('w1', 'api', 'acme/api', 'b', 'main', 7, 't')"
        )
        conn.commit()
        assert "ci_infra_reruns" not in _merge_queue_columns(conn)

        _ensure_schema(conn)

        assert "ci_infra_reruns" in _merge_queue_columns(conn)
        # A pre-existing row survives and reads as "no auto-reruns spent
        # yet" — the same default a freshly-enqueued entry gets.
        row = conn.execute(
            "SELECT ci_infra_reruns FROM merge_queue WHERE issue_number = 7"
        ).fetchone()
        assert row["ci_infra_reruns"] == 0
        conn.close()

    def test_migration_is_idempotent(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(_PRE_1892_MERGE_QUEUE_TABLE)
        conn.commit()
        _ensure_schema(conn)
        _ensure_schema(conn)
        _ensure_schema(conn)
        assert "ci_infra_reruns" in _merge_queue_columns(conn)
        conn.close()


# ── merge_queue.ci_stale_reruns column (#2197) ───────────────────────────────

# Same pre-migration shape as _PRE_1892_MERGE_QUEUE_TABLE, plus the #1892
# column — exercises the #2197 migration landing on a DB that already
# picked up ci_infra_reruns but predates ci_stale_reruns.
_PRE_2197_MERGE_QUEUE_TABLE = """
    CREATE TABLE merge_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        assignment_id TEXT NOT NULL,
        repo_name TEXT NOT NULL,
        repo_github TEXT NOT NULL,
        branch TEXT NOT NULL,
        target_branch TEXT NOT NULL,
        issue_number INTEGER NOT NULL,
        issue_title TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending',
        pr_number INTEGER,
        pr_url TEXT,
        size INTEGER,
        last_attempt REAL,
        error TEXT,
        enqueued_at REAL,
        assignment_type TEXT DEFAULT 'work',
        required_gates TEXT,
        ci_infra_reruns INTEGER NOT NULL DEFAULT 0
    )
"""


class TestMergeQueueCiStaleRerunsColumn:
    def test_fresh_database_has_it_from_the_create(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        assert "ci_stale_reruns" in _merge_queue_columns(isolated_conn)

    def test_existing_database_gains_it_in_place(self) -> None:
        """The real path: a coord.db created before #2197 (but after #1892),
        upgraded by _ensure_schema."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(_PRE_2197_MERGE_QUEUE_TABLE)
        conn.execute(
            "INSERT INTO merge_queue "
            "(assignment_id, repo_name, repo_github, branch, target_branch, "
            "issue_number, issue_title) "
            "VALUES ('w1', 'api', 'acme/api', 'b', 'main', 7, 't')"
        )
        conn.commit()
        assert "ci_stale_reruns" not in _merge_queue_columns(conn)

        _ensure_schema(conn)

        assert "ci_stale_reruns" in _merge_queue_columns(conn)
        # A pre-existing row survives and reads as "no auto-reruns spent
        # yet" — the same default a freshly-enqueued entry gets.
        row = conn.execute(
            "SELECT ci_stale_reruns FROM merge_queue WHERE issue_number = 7"
        ).fetchone()
        assert row["ci_stale_reruns"] == 0
        conn.close()

    def test_migration_is_idempotent(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(_PRE_2197_MERGE_QUEUE_TABLE)
        conn.commit()
        _ensure_schema(conn)
        _ensure_schema(conn)
        _ensure_schema(conn)
        assert "ci_stale_reruns" in _merge_queue_columns(conn)
        conn.close()


# ── merge_queue.ci_flaky_reruns / ci_flaky_pending columns (#2252) ──────────

# Same pre-migration shape as _PRE_2197_MERGE_QUEUE_TABLE, plus the #2197
# column — exercises the #2252 migration landing on a DB that already
# picked up ci_infra_reruns/ci_stale_reruns but predates ci_flaky_reruns/
# ci_flaky_pending.
_PRE_2252_MERGE_QUEUE_TABLE = """
    CREATE TABLE merge_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        assignment_id TEXT NOT NULL,
        repo_name TEXT NOT NULL,
        repo_github TEXT NOT NULL,
        branch TEXT NOT NULL,
        target_branch TEXT NOT NULL,
        issue_number INTEGER NOT NULL,
        issue_title TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending',
        pr_number INTEGER,
        pr_url TEXT,
        size INTEGER,
        last_attempt REAL,
        error TEXT,
        enqueued_at REAL,
        assignment_type TEXT DEFAULT 'work',
        required_gates TEXT,
        ci_infra_reruns INTEGER NOT NULL DEFAULT 0,
        ci_stale_reruns INTEGER NOT NULL DEFAULT 0
    )
"""


class TestMergeQueueCiFlakyRerunsColumn:
    def test_fresh_database_has_it_from_the_create(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        assert "ci_flaky_reruns" in _merge_queue_columns(isolated_conn)
        assert "ci_flaky_pending" in _merge_queue_columns(isolated_conn)

    def test_existing_database_gains_it_in_place(self) -> None:
        """The real path: a coord.db created before #2252 (but after
        #2197), upgraded by _ensure_schema."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(_PRE_2252_MERGE_QUEUE_TABLE)
        conn.execute(
            "INSERT INTO merge_queue "
            "(assignment_id, repo_name, repo_github, branch, target_branch, "
            "issue_number, issue_title) "
            "VALUES ('w1', 'api', 'acme/api', 'b', 'main', 7, 't')"
        )
        conn.commit()
        assert "ci_flaky_reruns" not in _merge_queue_columns(conn)
        assert "ci_flaky_pending" not in _merge_queue_columns(conn)

        _ensure_schema(conn)

        assert "ci_flaky_reruns" in _merge_queue_columns(conn)
        assert "ci_flaky_pending" in _merge_queue_columns(conn)
        # A pre-existing row survives and reads as "no flake re-run ever
        # spent/pending" — the same defaults a freshly-enqueued entry gets.
        row = conn.execute(
            "SELECT ci_flaky_reruns, ci_flaky_pending FROM merge_queue "
            "WHERE issue_number = 7"
        ).fetchone()
        assert row["ci_flaky_reruns"] == 0
        assert row["ci_flaky_pending"] == ""
        conn.close()

    def test_migration_is_idempotent(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(_PRE_2252_MERGE_QUEUE_TABLE)
        conn.commit()
        _ensure_schema(conn)
        _ensure_schema(conn)
        _ensure_schema(conn)
        assert "ci_flaky_reruns" in _merge_queue_columns(conn)
        assert "ci_flaky_pending" in _merge_queue_columns(conn)
        conn.close()


# ── merge_queue.ci_fix_dispatches column (#2510) ────────────────────────────

# Same pre-migration shape as _PRE_2252_MERGE_QUEUE_TABLE, minus the columns
# every migration after it (including this one) adds — exercises the #2510
# migration landing on a DB that predates ci_flaky_reruns/ci_flaky_pending/
# ci_unreadable_reruns/ci_fix_dispatches entirely.
_PRE_2510_MERGE_QUEUE_TABLE = _PRE_2252_MERGE_QUEUE_TABLE


class TestMergeQueueCiFixDispatchesColumn:
    def test_fresh_database_has_it_from_the_create(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        assert "ci_fix_dispatches" in _merge_queue_columns(isolated_conn)

    def test_existing_database_gains_it_in_place(self) -> None:
        """The real path: a coord.db created before #2510, upgraded by
        _ensure_schema."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(_PRE_2510_MERGE_QUEUE_TABLE)
        conn.execute(
            "INSERT INTO merge_queue "
            "(assignment_id, repo_name, repo_github, branch, target_branch, "
            "issue_number, issue_title) "
            "VALUES ('w1', 'api', 'acme/api', 'b', 'main', 7, 't')"
        )
        conn.commit()
        assert "ci_fix_dispatches" not in _merge_queue_columns(conn)

        _ensure_schema(conn)

        assert "ci_fix_dispatches" in _merge_queue_columns(conn)
        # A pre-existing row survives and reads as "no ci-fix dispatched
        # yet" — the same default a freshly-enqueued entry gets.
        row = conn.execute(
            "SELECT ci_fix_dispatches FROM merge_queue WHERE issue_number = 7"
        ).fetchone()
        assert row["ci_fix_dispatches"] == 0
        conn.close()

    def test_migration_is_idempotent(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(_PRE_2510_MERGE_QUEUE_TABLE)
        conn.commit()
        _ensure_schema(conn)
        _ensure_schema(conn)
        _ensure_schema(conn)
        assert "ci_fix_dispatches" in _merge_queue_columns(conn)
        conn.close()


# ── retry_on_locked (#2538) ─────────────────────────────────────────────────


class TestRetryOnLocked:
    """#2538: `coord merge` crashed the whole run on a transient
    `sqlite3.OperationalError: database is locked` raised while recording a
    dispatched CI-fix assignment. `retry_on_locked` gives a momentary
    collision (the daemon's own passive tick, another `coord merge`/`coord
    notify` invocation holding the DB) a few short, backed-off attempts to
    clear before it becomes the caller's problem.
    """

    def test_succeeds_immediately_when_no_contention(self) -> None:
        calls = []

        def _write() -> str:
            calls.append(1)
            return "ok"

        assert retry_on_locked(_write) == "ok"
        assert len(calls) == 1

    def test_retries_through_transient_lock_then_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sleeps: list[float] = []
        monkeypatch.setattr(db_mod.time, "sleep", lambda s: sleeps.append(s))

        attempts = {"n": 0}

        def _write() -> str:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise sqlite3.OperationalError("database is locked")
            return "ok"

        result = retry_on_locked(_write)

        assert result == "ok"
        assert attempts["n"] == 3
        # Two collisions before the third, successful attempt — backed off
        # (each wait longer than the last), never a busy-loop.
        assert len(sleeps) == 2
        assert sleeps[1] > sleeps[0]

    def test_raises_after_exhausting_the_retry_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(db_mod.time, "sleep", lambda s: None)

        def _write() -> None:
            raise sqlite3.OperationalError("database is locked")

        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            retry_on_locked(_write, attempts=3, base_delay=0.0)

    def test_unrelated_operational_error_is_not_retried(self) -> None:
        """A schema/statement bug is a real failure, not transient
        contention — it must surface on the very first attempt rather than
        being retried (and delayed) as if it were lock contention."""
        calls = []

        def _write() -> None:
            calls.append(1)
            raise sqlite3.OperationalError("no such table: bogus")

        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            retry_on_locked(_write)

        assert len(calls) == 1


# ── override_connection ────────────────────────────────────────────────────────

class TestOverrideConnection:
    def test_override_makes_get_connection_return_override(self) -> None:
        from coord.db import get_connection

        fresh_conn = sqlite3.connect(":memory:")
        fresh_conn.row_factory = sqlite3.Row
        _ensure_schema(fresh_conn)
        override_connection(fresh_conn)
        try:
            assert get_connection() is fresh_conn
        finally:
            close()
            # Restore for other tests
            override_connection(sqlite3.connect(":memory:"))
            _ensure_schema(db_mod.get_connection())

    def test_close_resets_connection(self) -> None:
        from coord.db import get_connection

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        override_connection(conn)
        close()
        assert db_mod._conn is None
        # Restore
        _ensure_schema(sqlite3.connect(":memory:"))


# ── JSON migration ────────────────────────────────────────────────────────────

class TestJsonMigration:
    def _write_json(self, path: Path, data: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def test_migration_imports_dispatched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When dispatched.json exists and assignments table is empty, it is migrated."""
        monkeypatch.setattr(db_mod, "COORD_DIR", tmp_path)

        dispatched = [
            {
                "assignment_id": "aaa",
                "machine_name": "laptop",
                "repo_name": "api",
                "repo_github": "acme/api",
                "issue_number": 1,
                "issue_title": "Fix auth",
                "files_likely": ["auth.py"],
                "briefing": "do it",
                "dispatched_at": 1000.0,
                "type": "work",
                "required_gates": [],
            }
        ]
        self._write_json(tmp_path / "dispatched.json", dispatched)

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        db_mod._maybe_migrate_json(conn)

        rows = conn.execute("SELECT * FROM assignments").fetchall()
        assert len(rows) == 1
        assert rows[0]["assignment_id"] == "aaa"
        assert rows[0]["machine_name"] == "laptop"

    def test_migration_imports_notified(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(db_mod, "COORD_DIR", tmp_path)

        dispatched = [
            {
                "assignment_id": "bbb",
                "machine_name": "m", "repo_name": "api", "repo_github": "a/b",
                "issue_number": 2, "issue_title": "t", "files_likely": [],
                "briefing": "", "dispatched_at": 100.0, "type": "work",
                "required_gates": [],
            }
        ]
        notified = {"bbb": {"event": "completion", "posted_at": 200.0}}
        self._write_json(tmp_path / "dispatched.json", dispatched)
        self._write_json(tmp_path / "notified.json", notified)

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        db_mod._maybe_migrate_json(conn)

        n_rows = conn.execute("SELECT * FROM notifications").fetchall()
        assert len(n_rows) == 1
        assert n_rows[0]["assignment_id"] == "bbb"
        assert n_rows[0]["event"] == "completion"

    def test_migration_skipped_when_assignments_exist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_conn: sqlite3.Connection
    ) -> None:
        """Migration should not run when DB already has assignments."""
        monkeypatch.setattr(db_mod, "COORD_DIR", tmp_path)
        self._write_json(tmp_path / "dispatched.json", [])

        isolated_conn.execute(
            """INSERT INTO assignments
               (assignment_id, machine_name, repo_name, issue_number, issue_title)
               VALUES ('existing', 'm', 'r', 1, 't')"""
        )
        isolated_conn.commit()

        db_mod._maybe_migrate_json(isolated_conn)
        rows = isolated_conn.execute("SELECT * FROM assignments").fetchall()
        assert len(rows) == 1  # unchanged

    def test_migration_renames_json_to_bak(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(db_mod, "COORD_DIR", tmp_path)
        self._write_json(tmp_path / "dispatched.json", [])

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        db_mod._maybe_migrate_json(conn)

        assert not (tmp_path / "dispatched.json").exists()
        assert (tmp_path / "dispatched.json.bak").exists()

    def test_migration_skipped_when_no_dispatched_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_conn: sqlite3.Connection
    ) -> None:
        """If dispatched.json doesn't exist, migration is a no-op."""
        monkeypatch.setattr(db_mod, "COORD_DIR", tmp_path)
        # Don't create dispatched.json
        db_mod._maybe_migrate_json(isolated_conn)
        rows = isolated_conn.execute("SELECT * FROM assignments").fetchall()
        assert rows == []

    def test_migration_writes_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After migration, board_meta must contain a 'json_migrated' row."""
        monkeypatch.setattr(db_mod, "COORD_DIR", tmp_path)
        self._write_json(tmp_path / "dispatched.json", [])

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        db_mod._maybe_migrate_json(conn)

        row = conn.execute(
            "SELECT value FROM board_meta WHERE key='json_migrated'"
        ).fetchone()
        assert row is not None, "json_migrated marker must be written after migration"
        # value should be a parseable float timestamp
        assert float(row["value"]) > 0

    def test_migration_does_not_retrigger_when_marker_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If json_migrated marker is present, migration must not run again — even when
        dispatched.json reappears and the assignments table is empty."""
        monkeypatch.setattr(db_mod, "COORD_DIR", tmp_path)

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)

        # Plant the marker (simulates a prior successful migration)
        conn.execute(
            "INSERT INTO board_meta (key, value) VALUES ('json_migrated', '1000.0')"
        )
        conn.commit()

        # Simulate stale JSON file reappearing with data
        stale_dispatched = [
            {
                "assignment_id": "stale-001",
                "machine_name": "ghost",
                "repo_name": "api",
                "repo_github": "acme/api",
                "issue_number": 99,
                "issue_title": "Stale entry",
                "files_likely": [],
                "briefing": "",
                "dispatched_at": 9999.0,
                "type": "work",
                "required_gates": [],
            }
        ]
        self._write_json(tmp_path / "dispatched.json", stale_dispatched)

        # Assignments table is empty — the old guard would have triggered re-migration
        count_before = conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0]
        assert count_before == 0

        db_mod._maybe_migrate_json(conn)

        # Stale data must NOT have been imported
        rows = conn.execute("SELECT * FROM assignments").fetchall()
        assert len(rows) == 0, (
            "Migration re-triggered after marker was set; stale data was imported"
        )


# ── Gate-order migration (Test-before-Review reorder) ─────────────────────────

class TestMigrateGateOrder:
    """_migrate_gate_order rewrites the old default gate JSON in stored rows.

    Direction: the #520-era default ``["review", "test", "merge"]`` is rewritten
    to the new Test-before-Review default ``["test", "review", "merge"]``.
    """

    _OLD = '["review", "test", "merge"]'
    _NEW = '["test", "review", "merge"]'
    _CUSTOM = '["review", "merge"]'  # should never be touched

    def _insert_assignment(
        self,
        conn: sqlite3.Connection,
        aid: str,
        required_gates: str,
    ) -> None:
        conn.execute(
            "INSERT INTO assignments "
            "(assignment_id, machine_name, repo_name, issue_number, issue_title, required_gates) "
            "VALUES (?, 'm', 'r', 1, 't', ?)",
            (aid, required_gates),
        )
        conn.commit()

    def _insert_proposal(
        self,
        conn: sqlite3.Connection,
        pid: int,
        required_gates: str,
    ) -> None:
        conn.execute(
            "INSERT INTO proposals "
            "(id, machine_name, repo_name, issue_number, issue_title, required_gates) "
            "VALUES (?, 'm', 'r', 1, 't', ?)",
            (pid, required_gates),
        )
        conn.commit()

    def _set_board_meta(self, conn: sqlite3.Connection, value: str) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO board_meta (key, value) VALUES ('pipeline_default_gates', ?)",
            (value,),
        )
        conn.commit()

    def test_rewrites_old_default_in_assignments(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        """Assignments storing the old default gate order are rewritten."""
        self._insert_assignment(isolated_conn, "a1", self._OLD)
        _migrate_gate_order(isolated_conn)
        row = isolated_conn.execute(
            "SELECT required_gates FROM assignments WHERE assignment_id='a1'"
        ).fetchone()
        assert row["required_gates"] == self._NEW

    def test_rewrites_old_default_in_proposals(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        """Proposals storing the old default gate order are rewritten."""
        self._insert_proposal(isolated_conn, 1, self._OLD)
        _migrate_gate_order(isolated_conn)
        row = isolated_conn.execute(
            "SELECT required_gates FROM proposals WHERE id=1"
        ).fetchone()
        assert row["required_gates"] == self._NEW

    def test_rewrites_board_meta_pipeline_default_gates(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        """board_meta['pipeline_default_gates'] is updated when it holds the old value."""
        self._set_board_meta(isolated_conn, self._OLD)
        _migrate_gate_order(isolated_conn)
        row = isolated_conn.execute(
            "SELECT value FROM board_meta WHERE key='pipeline_default_gates'"
        ).fetchone()
        assert row["value"] == self._NEW

    def test_does_not_touch_custom_gate_lists(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        """Assignments with user-customised gate lists are left unchanged."""
        self._insert_assignment(isolated_conn, "a2", self._CUSTOM)
        _migrate_gate_order(isolated_conn)
        row = isolated_conn.execute(
            "SELECT required_gates FROM assignments WHERE assignment_id='a2'"
        ).fetchone()
        assert row["required_gates"] == self._CUSTOM

    def test_idempotent(self, isolated_conn: sqlite3.Connection) -> None:
        """Running the migration twice produces the same result."""
        self._insert_assignment(isolated_conn, "a3", self._OLD)
        _migrate_gate_order(isolated_conn)
        _migrate_gate_order(isolated_conn)  # second call — no-op
        row = isolated_conn.execute(
            "SELECT required_gates FROM assignments WHERE assignment_id='a3'"
        ).fetchone()
        assert row["required_gates"] == self._NEW

    def test_noop_when_already_new_order(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        """Rows already storing the new order are not affected."""
        self._insert_assignment(isolated_conn, "a4", self._NEW)
        _migrate_gate_order(isolated_conn)
        row = isolated_conn.execute(
            "SELECT required_gates FROM assignments WHERE assignment_id='a4'"
        ).fetchone()
        assert row["required_gates"] == self._NEW

    def test_board_meta_absent_is_noop(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        """If pipeline_default_gates is absent from board_meta, migration is a no-op."""
        _migrate_gate_order(isolated_conn)  # no board_meta row — must not raise
        row = isolated_conn.execute(
            "SELECT value FROM board_meta WHERE key='pipeline_default_gates'"
        ).fetchone()
        assert row is None


# ── #1663: stranded review verdicts ───────────────────────────────────────────


class TestBackfillOrphanedReviewVerdicts:
    """#1663: ``run_drain`` captured verdicts on the review row and never
    propagated them to the parent work row, stranding eight rows across the
    2026-08-01 overnight batch (#1527 #1624 #1658 #1633 #1353) plus #544, #1078
    and #1122.  The backfill copies each verdict from the review row that
    actually earned it — it never synthesises one."""

    @staticmethod
    def _work(
        conn: sqlite3.Connection,
        aid: str,
        *,
        atype: str = "work",
        review_state: str | None = "dispatched",
        review_verdict: str | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
            "issue_number, issue_title, type, status, review_state, review_verdict) "
            "VALUES (?, 'laptop', 'api', 42, 't', ?, 'done', ?, ?)",
            (aid, atype, review_state, review_verdict),
        )

    @staticmethod
    def _review(
        conn: sqlite3.Connection,
        aid: str,
        work_aid: str,
        verdict: str | None,
        *,
        status: str = "done",
        dispatched_at: float = 1000.0,
    ) -> None:
        conn.execute(
            "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
            "issue_number, issue_title, type, status, review_of_assignment_id, "
            "review_verdict, dispatched_at) "
            "VALUES (?, 'laptop', 'api', 42, '[review] t', 'review', ?, ?, ?, ?)",
            (aid, status, work_aid, verdict, dispatched_at),
        )

    @staticmethod
    def _row(conn: sqlite3.Connection, aid: str) -> dict:
        r = conn.execute(
            "SELECT review_state, review_verdict FROM assignments "
            "WHERE assignment_id=?",
            (aid,),
        ).fetchone()
        return {"review_state": r["review_state"], "review_verdict": r["review_verdict"]}

    def test_copies_approve_from_the_review_row(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        """#1527's shape: work at dispatched/NULL, review at done/approve."""
        self._work(isolated_conn, "28d54c5b8873")
        self._review(isolated_conn, "6415c03e6ea2", "28d54c5b8873", "approve")

        assert db_mod._backfill_orphaned_review_verdicts(isolated_conn) == 1
        assert self._row(isolated_conn, "28d54c5b8873") == {
            "review_state": "done", "review_verdict": "approve",
        }

    def test_copies_request_changes_from_the_review_row(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        """#544 / #1078's shape.  A request-changes must be copied verbatim, not
        normalised to approve — the row is what tells a human a fix is owed."""
        self._work(isolated_conn, "ff4927937695")
        self._review(
            isolated_conn, "cb64561942fc", "ff4927937695", "request-changes",
        )

        assert db_mod._backfill_orphaned_review_verdicts(isolated_conn) == 1
        assert self._row(isolated_conn, "ff4927937695") == {
            "review_state": "done", "review_verdict": "request-changes",
        }

    def test_never_synthesises_a_verdict_the_review_row_does_not_carry(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        """#1122's shape: the review row ``188ae219aca3`` FAILED and its verdict
        was lost entirely (#1636/#1658).  Its findings were recovered from the
        worker transcript by hand and posted to PR #1656 — a fabricated verdict
        here would overwrite that with a guess.  Nothing to copy ⇒ no write."""
        self._work(isolated_conn, "a822bbd9eae3")
        self._review(
            isolated_conn, "188ae219aca3", "a822bbd9eae3", None, status="failed",
        )

        assert db_mod._backfill_orphaned_review_verdicts(isolated_conn) == 0
        assert self._row(isolated_conn, "a822bbd9eae3") == {
            "review_state": "dispatched", "review_verdict": None,
        }

    def test_copies_a_verdict_recovered_onto_a_failed_review_row(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        """The converse: when a verdict WAS recovered by hand onto a failed
        review row (#617's transcript recovery), it was earned by a real review
        and must be carried across.  Hence no ``status='done'`` filter."""
        self._work(isolated_conn, "wk-recovered")
        self._review(
            isolated_conn, "rev-recovered", "wk-recovered", "request-changes",
            status="failed",
        )

        assert db_mod._backfill_orphaned_review_verdicts(isolated_conn) == 1
        assert self._row(isolated_conn, "wk-recovered")["review_verdict"] == (
            "request-changes"
        )

    def test_takes_the_latest_round_when_a_row_was_reviewed_twice(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        self._work(isolated_conn, "wk-2rounds")
        self._review(
            isolated_conn, "rev-r1", "wk-2rounds", "request-changes",
            dispatched_at=1000.0,
        )
        self._review(
            isolated_conn, "rev-r2", "wk-2rounds", "approve", dispatched_at=2000.0,
        )

        db_mod._backfill_orphaned_review_verdicts(isolated_conn)
        assert self._row(isolated_conn, "wk-2rounds")["review_verdict"] == "approve"

    def test_leaves_a_work_row_that_already_has_a_verdict_alone(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        self._work(
            isolated_conn, "wk-has", review_state="done", review_verdict="approve",
        )
        self._review(
            isolated_conn, "rev-has", "wk-has", "request-changes",
        )

        assert db_mod._backfill_orphaned_review_verdicts(isolated_conn) == 0
        assert self._row(isolated_conn, "wk-has")["review_verdict"] == "approve"

    def test_leaves_rows_whose_review_stage_never_ran_alone(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        """``pending`` / ``advisory`` / NULL means no review ran or it was
        waived — not stranded, and not ours to stamp."""
        for state in ("pending", "advisory", None):
            aid = f"wk-{state}"
            self._work(isolated_conn, aid, review_state=state)
            self._review(isolated_conn, f"rev-{state}", aid, "approve")

        assert db_mod._backfill_orphaned_review_verdicts(isolated_conn) == 0
        for state in ("pending", "advisory", None):
            assert self._row(isolated_conn, f"wk-{state}")["review_verdict"] is None

    def test_ignores_non_work_like_rows(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        """Only WORK_LIKE_TYPES carry a parent review verdict.  A ``review`` or
        ``smoke`` row must never be rewritten by its own children."""
        self._work(isolated_conn, "sm-1", atype="smoke")
        self._review(isolated_conn, "rev-sm", "sm-1", "approve")

        assert db_mod._backfill_orphaned_review_verdicts(isolated_conn) == 0
        assert self._row(isolated_conn, "sm-1")["review_verdict"] is None

    def test_covers_every_work_like_type(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        for atype in ("work", "mock-author", "test-author"):
            self._work(isolated_conn, f"wk-{atype}", atype=atype)
            self._review(isolated_conn, f"rev-{atype}", f"wk-{atype}", "approve")

        assert db_mod._backfill_orphaned_review_verdicts(isolated_conn) == 3

    def test_is_idempotent(self, isolated_conn: sqlite3.Connection) -> None:
        self._work(isolated_conn, "wk-idem")
        self._review(isolated_conn, "rev-idem", "wk-idem", "approve")

        assert db_mod._backfill_orphaned_review_verdicts(isolated_conn) == 1
        assert db_mod._backfill_orphaned_review_verdicts(isolated_conn) == 0
        assert self._row(isolated_conn, "wk-idem")["review_verdict"] == "approve"

    def test_empty_db_is_a_noop(self, isolated_conn: sqlite3.Connection) -> None:
        assert db_mod._backfill_orphaned_review_verdicts(isolated_conn) == 0


# ── schema_version write-gate (#2598) ───────────────────────────────────────
#
# Every DB open ran the full _ensure_schema/_maybe_migrate_json/
# _migrate_gate_order/_backfill_orphaned_review_verdicts write path
# unconditionally, and did so against an unconstrained schema_version table
# — so INSERT OR IGNORE never actually ignored anything (45,708 duplicate
# rows observed in the field) and a read-only `coord status` took the write
# lock at startup for no reason. This section is the regression net: the
# table stays constrained to one row, an up-to-date database's open issues
# no write at all, and an existing on-disk database with junk rows converges
# back to one on its next open.


class TestSchemaVersionGate:
    def test_ensure_schema_produces_exactly_one_schema_version_row(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        rows = isolated_conn.execute("SELECT version FROM schema_version").fetchall()
        assert len(rows) == 1
        assert rows[0]["version"] == db_mod._DB_SCHEMA_VERSION

    def test_ensure_schema_collapses_preexisting_duplicate_rows(self) -> None:
        """The exact field shape: an unconstrained table with many identical
        rows, the way every pre-#2598 process's INSERT OR IGNORE left it."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        conn.executemany("INSERT INTO schema_version VALUES (?)", [(1,)] * 500)
        conn.commit()
        count_before = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
        assert count_before == 500

        db_mod._ensure_schema(conn)

        rows = conn.execute("SELECT version FROM schema_version").fetchall()
        assert len(rows) == 1
        assert rows[0]["version"] == db_mod._DB_SCHEMA_VERSION
        conn.close()

    def test_schema_version_table_is_actually_constrained_after_the_fix(self) -> None:
        """Once fixed, a duplicate insert of the current version is a true
        no-op (PRIMARY KEY conflict, ignored) rather than a second row —
        the constraint that was missing is the entire bug."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db_mod._ensure_schema(conn)
        version = db_mod._DB_SCHEMA_VERSION

        conn.execute("INSERT OR IGNORE INTO schema_version VALUES (?)", (version,))
        conn.execute("INSERT OR IGNORE INTO schema_version VALUES (?)", (version,))

        rows = conn.execute("SELECT version FROM schema_version").fetchall()
        assert len(rows) == 1
        conn.close()

    def test_read_schema_version_is_read_only(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        before = isolated_conn.total_changes
        db_mod._read_schema_version(isolated_conn)
        assert isolated_conn.total_changes == before

    def test_read_schema_version_reads_zero_before_any_table_exists(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        assert db_mod._read_schema_version(conn) == 0
        conn.close()

    def test_open_on_up_to_date_database_issues_no_write(self, tmp_path: Path) -> None:
        """Black-box acceptance check: opening an already-migrated database
        a second time performs zero writes — the defect this issue reports
        (a write transaction, and a junk row, on every single open)."""
        db_path = tmp_path / "coord.db"
        first = db_mod._open(db_path)
        assert first.total_changes > 0  # the initializing open does write
        first.close()

        second = db_mod._open(db_path)
        try:
            assert second.total_changes == 0
            rows = second.execute("SELECT version FROM schema_version").fetchall()
            assert len(rows) == 1
            assert rows[0]["version"] == db_mod._DB_SCHEMA_VERSION
        finally:
            second.close()

    def test_open_on_a_fresh_database_still_initializes_it(self, tmp_path: Path) -> None:
        db_path = tmp_path / "coord.db"
        conn = db_mod._open(db_path)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            names = {r["name"] for r in rows}
            assert "assignments" in names
            assert "schema_version" in names
        finally:
            conn.close()

    def test_open_migrates_an_existing_on_disk_database_with_duplicate_rows(
        self, tmp_path: Path
    ) -> None:
        """The migration acceptance criterion: a real on-disk database with
        N duplicate schema_version rows (45,708 observed in the field)
        collapses to one on its next open — without a caller doing anything
        special to trigger it."""
        db_path = tmp_path / "coord.db"
        raw = sqlite3.connect(str(db_path))
        raw.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        raw.executemany("INSERT INTO schema_version VALUES (?)", [(1,)] * 200)
        raw.commit()
        raw.close()

        conn = db_mod._open(db_path)
        try:
            rows = conn.execute("SELECT version FROM schema_version").fetchall()
            assert len(rows) == 1
            assert rows[0]["version"] == db_mod._DB_SCHEMA_VERSION
        finally:
            conn.close()
