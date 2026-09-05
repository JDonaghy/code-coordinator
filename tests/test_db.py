"""Tests for coord.db — schema creation, migration, connection override."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from coord import db as db_mod
from coord import sql
from coord.config import ConfigError
from coord.db import (
    _ensure_schema,
    override_connection,
    close,
    _migrate_gate_order,
    is_lock_contention_error,
    retry_on_locked,
)
from tests import backends


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def isolated_conn(coord_db):
    """The autouse ``coord_db`` connection, under this file's historical name.

    #2884 bucket B: this used to be its own ``autouse`` fixture doing
    ``sqlite3.connect(":memory:")`` + ``_ensure_schema`` + ``override_
    connection`` — a verbatim re-implementation of conftest's autouse
    ``coord_db``, which every test in this file was already getting anyway.
    Aliasing rather than renaming keeps the ~130 ``isolated_conn`` parameters
    below untouched while making the connection follow ``COORD_TEST_BACKEND``
    like the rest of the suite.

    (Many of the tests it feeds are legitimately SQLite-specific — they probe
    ``sqlite_master``, WAL, ``busy_timeout``, ``PRAGMA table_info`` — and are
    *expected* to fail on a non-SQLite backend. That is a failure in a test
    body, which is the point: #2884 delivers the failure list, #829 acts on
    it.)
    """
    return coord_db


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

    def test_autoincrement_pk_columns_actually_autoincrement(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        """#2724: ``_SCHEMA_SQL``'s 8 ``id`` columns are written as the
        dialect-neutral ``__AUTOPK_DDL__`` sentinel and substituted with
        ``coord.sql.autoincrement_pk_ddl(dialect)`` at ``_ensure_schema()``
        time -- if that substitution silently failed to run (e.g. the
        sentinel text drifted out of sync between ``_SCHEMA_SQL`` and the
        ``.replace()`` call), SQLite would create the column as a literal
        ``__AUTOPK_DDL__``-named/typed thing instead of an integer primary
        key, and this insert would either raise or fail to assign
        monotonic ids. ``merge_queue`` is one of the 8 sites.
        """
        cur1 = isolated_conn.execute(
            "INSERT INTO merge_queue "
            "(assignment_id, repo_name, repo_github, branch, target_branch, "
            " issue_number, issue_title) VALUES ('a1', 'api', 'x/api', 'b1', 'main', 1, 't')"
        )
        cur2 = isolated_conn.execute(
            "INSERT INTO merge_queue "
            "(assignment_id, repo_name, repo_github, branch, target_branch, "
            " issue_number, issue_title) VALUES ('a2', 'api', 'x/api', 'b2', 'main', 2, 't')"
        )
        isolated_conn.commit()
        assert cur1.lastrowid is not None
        assert cur2.lastrowid == cur1.lastrowid + 1


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
        # #3083: goes through the seam, not `PRAGMA` — `isolated_conn` is an
        # alias for the autouse `coord_db` fixture, so under
        # COORD_TEST_BACKEND=postgres this is a psycopg connection and a
        # literal PRAGMA is a syntax error. The neighbouring pre-migration
        # checks below keep their raw PRAGMA on purpose: those run against a
        # hand-built SQLite *file*, which is SQLite on every backend.
        cols = {name for name, _type in sql.table_columns(isolated_conn, "issue_comments")}
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


def _drive_queue_columns(conn) -> set[str]:
    """#3083: through the seam, because this helper is handed *both* kinds of
    connection — a raw SQLite file DB (the pre-migration checks) and the
    autouse fixture connection, which is psycopg under
    COORD_TEST_BACKEND=postgres and cannot parse a literal PRAGMA."""
    return {name for name, _type in sql.table_columns(conn, "drive_queue")}


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


# ── drive_queue.max_fix_rounds / no_acceptance columns (#2604, #2589, #2675) ─
#
# #2604 and #2589 each appended an `ALTER TABLE` to `_migrate_add_columns`
# but neither bumped `_DB_SCHEMA_VERSION` (#2675) — so `_open()`'s version
# gate (`_read_schema_version(conn) < _DB_SCHEMA_VERSION`) treated every
# database already at version 2 as fully caught up and skipped
# `_ensure_schema` — and therefore `_migrate_add_columns` — ENTIRELY, on
# every open, forever. Every sibling class above calls `_ensure_schema`
# directly, bypassing that exact gate, so none of them would have caught
# this. These tests go through `db_mod._open()` instead — the real
# production entry point — against a database seeded at schema_version 2,
# exactly what every real ``~/.coord/coord.db`` looked like when #2604 and
# #2589 shipped.

_PRE_2604_DRIVE_QUEUE_TABLE = """
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
        resumes       INTEGER NOT NULL DEFAULT 0,
        retry_backoff_at REAL,
        UNIQUE(repo_name, issue_number)
    )
"""

_MAX_FIX_ROUNDS_AND_NO_ACCEPTANCE_COLUMNS = {"max_fix_rounds", "no_acceptance"}


def _seed_pre_2604_database(db_path: Path) -> None:
    """Write a real on-disk database at schema_version 2 — exactly what
    every ``~/.coord/coord.db`` looked like right up to the #2675 incident,
    including one pre-existing row (the shape that broke: a row that reads
    back fine right up until a query names one of the two missing columns).
    """
    raw = sqlite3.connect(str(db_path))
    raw.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
    raw.execute("INSERT INTO schema_version VALUES (2)")
    raw.execute(_PRE_2604_DRIVE_QUEUE_TABLE)
    raw.execute(
        "INSERT INTO drive_queue "
        "(repo_name, issue_number, position, after_json, enqueued_at) "
        "VALUES ('api', 7, 0, '[]', 100.0)"
    )
    raw.commit()
    raw.close()


class TestDriveQueueMaxFixRoundsAndNoAcceptanceColumns:
    def test_existing_version_2_database_gains_them_on_open(
        self, tmp_path: Path
    ) -> None:
        """The exact #2675 repro. `coord drive-queue list`/`tick` SELECT
        ``max_fix_rounds, no_acceptance`` unconditionally
        (``coord/state.py``'s ``_DRIVE_QUEUE_COLUMNS``) — a database that
        never gains these columns makes the whole queue unreadable and
        undrivable, forever, on every existing installation.
        """
        db_path = tmp_path / "coord.db"
        _seed_pre_2604_database(db_path)

        before = sqlite3.connect(str(db_path))
        cols_before = {
            r[1] for r in before.execute("PRAGMA table_info(drive_queue)")
        }
        before.close()
        assert not (_MAX_FIX_ROUNDS_AND_NO_ACCEPTANCE_COLUMNS & cols_before)

        conn = db_mod._open(db_path)
        try:
            cols_after = {
                r[1] for r in conn.execute("PRAGMA table_info(drive_queue)")
            }
            assert _MAX_FIX_ROUNDS_AND_NO_ACCEPTANCE_COLUMNS <= cols_after

            row = conn.execute(
                "SELECT max_fix_rounds, no_acceptance FROM drive_queue "
                "WHERE issue_number = 7"
            ).fetchone()
            # A row predating these columns reads as "no override" —
            # max_fix_rounds NULL, no_acceptance 0 — never a silent
            # behavior change for an entry that never asked for one.
            assert row["max_fix_rounds"] is None
            assert row["no_acceptance"] == 0

            version = conn.execute(
                "SELECT MAX(version) FROM schema_version"
            ).fetchone()[0]
            assert version == db_mod._DB_SCHEMA_VERSION
        finally:
            conn.close()

    def test_migration_is_idempotent(self, tmp_path: Path) -> None:
        db_path = tmp_path / "coord.db"
        _seed_pre_2604_database(db_path)

        for _ in range(3):
            conn = db_mod._open(db_path)
            conn.close()

        conn = db_mod._open(db_path)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(drive_queue)")}
            assert _MAX_FIX_ROUNDS_AND_NO_ACCEPTANCE_COLUMNS <= cols
        finally:
            conn.close()

    def test_state_accessors_read_the_upgraded_columns(
        self, tmp_path: Path
    ) -> None:
        """``coord drive-queue add --max-fix-rounds/--no-acceptance`` and
        ``list``/``tick`` go through ``coord.state``'s ``_local`` helpers —
        confirm the upgraded columns are actually usable through that path,
        not just present in ``PRAGMA table_info``.
        """
        from coord import state

        db_path = tmp_path / "coord.db"
        _seed_pre_2604_database(db_path)

        conn = db_mod._open(db_path)
        override_connection(conn)
        try:
            state._enqueue_drive_queue_local(
                "api", 11, max_fix_rounds=5, no_acceptance=True
            )
            rows = state._list_drive_queue_local()
        finally:
            close()
        entry = next(r for r in rows if r["issue_number"] == 11)
        assert entry["max_fix_rounds"] == 5
        assert entry["no_acceptance"] == 1


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


def _merge_queue_columns(conn) -> set[str]:
    """#3083: through the seam — see :func:`_drive_queue_columns`."""
    return {name for name, _type in sql.table_columns(conn, "merge_queue")}


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


# ── is_lock_contention_error (#2597) ────────────────────────────────────────


class TestIsLockContentionError:
    """#2597: the substring check used to be duplicated (and drifting)
    between `retry_on_locked` here and `coord.auto_loop`'s except block —
    this predicate is the single shared source of truth both now call."""

    def test_true_for_database_is_locked(self) -> None:
        assert is_lock_contention_error(
            sqlite3.OperationalError("database is locked")
        )

    def test_true_for_uppercase_or_mixed_case_message(self) -> None:
        assert is_lock_contention_error(
            sqlite3.OperationalError("Database Is Locked")
        )

    def test_true_for_table_level_lock_message(self) -> None:
        """SQLite's other lock-collision wording — a table-level lock,
        distinct from (and not a substring of) "database is locked"."""
        assert is_lock_contention_error(
            sqlite3.OperationalError("database table is locked")
        )

    def test_true_when_sqlite_errorcode_is_busy_even_with_other_wording(self) -> None:
        """Belt-and-suspenders: a differently-worded message still counts
        as contention when the underlying SQLITE_BUSY result code says so."""
        exc = sqlite3.OperationalError("some other phrasing entirely")
        exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
        assert is_lock_contention_error(exc)

    def test_false_for_unrelated_operational_error(self) -> None:
        assert not is_lock_contention_error(
            sqlite3.OperationalError("no such table: bogus")
        )

    def test_false_for_non_operational_exception(self) -> None:
        assert not is_lock_contention_error(RuntimeError("database is locked"))

    def test_real_contention_between_two_connections_is_detected(
        self, tmp_path
    ) -> None:
        """Not just message-matching in the abstract — the real exception
        SQLite raises for genuine cross-connection contention on a file-
        backed DB is recognized."""
        path = tmp_path / "contend.db"
        writer = sqlite3.connect(str(path), timeout=0)
        writer.execute("CREATE TABLE t (x)")
        writer.commit()
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO t VALUES (1)")

        reader = sqlite3.connect(str(path), timeout=0)
        reader.execute("PRAGMA busy_timeout=50")
        try:
            reader.execute("INSERT INTO t VALUES (2)")
            raise AssertionError("expected the second writer to collide")
        except sqlite3.OperationalError as exc:
            assert is_lock_contention_error(exc)
        finally:
            writer.rollback()
            writer.close()
            reader.close()


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


# ── _resolve_store_target (#827) ─────────────────────────────────────────────

_VALID_REPOS_MACHINES_YAML = (
    "repos:\n  - name: a\n    github: x/a\n"
    "machines:\n  - name: m\n    host: h\n    repos: [a]\n"
)


class TestResolveStoreTarget:
    """coordinator.yml's `store:` block (#827), resolved via
    `db_mod._resolve_store_target` -- see that function's docstring for why
    it fails open to SQLite on any config problem."""

    def test_defaults_to_sqlite_when_no_config_resolvable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("COORD_CONFIG", str(tmp_path / "does-not-exist.yml"))
        target = db_mod._resolve_store_target()
        assert target.backend == sql.DIALECT_SQLITE
        assert target.dsn is None

    def test_defaults_to_sqlite_when_store_block_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "coordinator.yml"
        config_path.write_text(_VALID_REPOS_MACHINES_YAML)
        monkeypatch.setenv("COORD_CONFIG", str(config_path))
        target = db_mod._resolve_store_target()
        assert target.backend == sql.DIALECT_SQLITE
        assert target.dsn is None

    def test_defaults_to_sqlite_when_store_backend_is_explicit_sqlite(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "coordinator.yml"
        config_path.write_text(_VALID_REPOS_MACHINES_YAML + "store:\n  backend: sqlite\n")
        monkeypatch.setenv("COORD_CONFIG", str(config_path))
        target = db_mod._resolve_store_target()
        assert target.backend == sql.DIALECT_SQLITE

    def test_resolves_postgres_backend_and_dsn(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "coordinator.yml"
        config_path.write_text(
            _VALID_REPOS_MACHINES_YAML
            + "store:\n  backend: postgres\n  dsn: postgresql://user@host/db\n"
        )
        monkeypatch.setenv("COORD_CONFIG", str(config_path))
        target = db_mod._resolve_store_target()
        assert target.backend == sql.DIALECT_POSTGRES
        assert target.dsn == "postgresql://user@host/db"

    def test_defaults_to_sqlite_on_a_malformed_config(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A config that fails to parse for a reason unrelated to `store:`
        (e.g. a bad `repos:` entry) must not surface as a storage-backend
        failure -- it fails open to SQLite exactly like a missing file."""
        config_path = tmp_path / "coordinator.yml"
        config_path.write_text("repos: not-a-list\n")
        monkeypatch.setenv("COORD_CONFIG", str(config_path))
        target = db_mod._resolve_store_target()
        assert target.backend == sql.DIALECT_SQLITE

    def test_propagates_config_error_for_a_malformed_explicit_store_block(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """#827 review, blocking finding 2: an explicit `store:` block that
        fails to validate (here: `backend: postgres` with no `dsn`) must
        fail LOUD -- raise, not silently resolve to SQLite. Before this fix
        the same broad `except Exception` that legitimately fails open for
        "no config file at all" also swallowed this, so a deployment that
        intentionally opted into Postgres and typo'd/broke its `store:`
        block got zero error and silently started serving out of an empty
        local SQLite file."""
        config_path = tmp_path / "coordinator.yml"
        config_path.write_text(_VALID_REPOS_MACHINES_YAML + "store:\n  backend: postgres\n")
        monkeypatch.setenv("COORD_CONFIG", str(config_path))
        with pytest.raises(ConfigError, match="dsn"):
            db_mod._resolve_store_target()

    def test_propagates_config_error_for_an_invalid_store_backend_value(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Same as above for the other `_parse_store` validation failure
        shape -- an unrecognized `backend` value."""
        config_path = tmp_path / "coordinator.yml"
        config_path.write_text(_VALID_REPOS_MACHINES_YAML + "store:\n  backend: mysql\n")
        monkeypatch.setenv("COORD_CONFIG", str(config_path))
        with pytest.raises(ConfigError, match="backend"):
            db_mod._resolve_store_target()

    def test_defaults_to_sqlite_on_invalid_yaml_syntax(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Genuinely broken YAML (not just semantically wrong) is exactly
        the "config problem unrelated to an explicit store: block" shape --
        it can't even be parsed far enough to know whether `store:` was
        present, so this fails open like a missing file, not loud."""
        config_path = tmp_path / "coordinator.yml"
        config_path.write_text("repos: [\n  - unterminated\n")
        monkeypatch.setenv("COORD_CONFIG", str(config_path))
        target = db_mod._resolve_store_target()
        assert target.backend == sql.DIALECT_SQLITE


# ── resolve_store_backend (#3084) ────────────────────────────────────────────


class TestResolveStoreBackend:
    """Public, DSN-redacting wrapper around `_resolve_store_target()`
    (#3084) -- the one accessor the `coord serve` banner, `GET /healthz`,
    and `coord doctor` all go through so a raw DSN can never reach any of
    those surfaces."""

    def test_sqlite_backend_has_no_redacted_target(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("COORD_CONFIG", str(tmp_path / "does-not-exist.yml"))
        backend, redacted = db_mod.resolve_store_backend()
        assert backend == sql.DIALECT_SQLITE
        assert redacted is None

    def test_postgres_backend_returns_redacted_host_and_dbname(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "coordinator.yml"
        config_path.write_text(
            _VALID_REPOS_MACHINES_YAML
            + "store:\n  backend: postgres\n"
            + "  dsn: postgresql://admin:s3cret-password@dbhost:5432/coorddb\n"
        )
        monkeypatch.setenv("COORD_CONFIG", str(config_path))
        backend, redacted = db_mod.resolve_store_backend()
        assert backend == sql.DIALECT_POSTGRES
        assert redacted == "postgresql://dbhost:5432/coorddb"

    def test_postgres_backend_never_returns_the_raw_dsn(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """#3084 acceptance: no raw DSN (password included) reaches any
        caller of this accessor."""
        config_path = tmp_path / "coordinator.yml"
        config_path.write_text(
            _VALID_REPOS_MACHINES_YAML
            + "store:\n  backend: postgres\n"
            + "  dsn: postgresql://admin:s3cret-password@dbhost:5432/coorddb\n"
        )
        monkeypatch.setenv("COORD_CONFIG", str(config_path))
        _backend, redacted = db_mod.resolve_store_backend()
        assert "s3cret-password" not in (redacted or "")
        assert "admin" not in (redacted or "")

    def test_matches_get_connections_own_resolution(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """This wrapper must never drift from the backend `get_connection()`
        itself would open -- it's a thin wrapper around the exact same
        `_resolve_store_target()` call, not a second, independent read."""
        config_path = tmp_path / "coordinator.yml"
        config_path.write_text(_VALID_REPOS_MACHINES_YAML + "store:\n  backend: sqlite\n")
        monkeypatch.setenv("COORD_CONFIG", str(config_path))
        backend, _redacted = db_mod.resolve_store_backend()
        assert backend == db_mod._resolve_store_target().backend


# ── get_connection() Postgres per-thread routing (#827) ─────────────────────


class _FakePgConn:
    """Cheap stand-in for a psycopg connection -- just enough for
    `get_connection()`'s caching/threading contract to exercise `.close()`.
    psycopg is not installed in this repo (see coord/sql.py's module
    docstring); `_open_postgres`'s own migration plumbing already routes
    through coord.sql's dialect-neutral seam, covered by
    tests/test_sql_dialect.py's fake-postgres-connection tests -- what's
    under test here is get_connection()'s routing, not the SQL it runs once
    connected, so `_open_postgres` itself is monkeypatched out below."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.closed = False

    def close(self) -> None:
        self.closed = True


class TestGetConnectionPostgresPerThread:
    """#827: when the configured backend is postgres, get_connection() opens
    ONE CONNECTION PER THREAD instead of sharing the SQLite singleton across
    threads -- see coord/db.py's module docstring, "Connection-sharing
    model"."""

    def _clear_override_without_closing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Reset the override slot for this test WITHOUT calling ``.close()``
        on the real connection the autouse ``coord_db`` fixture installed
        there (#3082 review, non-blocking finding).

        Every test below replaces routing with a fake Postgres connection
        for its own duration, and needs ``_conn`` cleared first so
        ``get_connection()`` doesn't just keep returning the fixture's real
        override. The original code did this by calling the module's
        ``close()``, which also calls ``.close()`` on whatever ``_conn``
        currently is -- on the real Postgres backend (``COORD_TEST_BACKEND=
        postgres``) that is the *fixture's own connection*, so closing it
        here left ``tests/backends.py``'s teardown (schema DROP + rollback)
        to run against an already-closed connection afterwards, raising
        ``psycopg.OperationalError: the connection is closed`` at fixture
        teardown -- a spurious error with nothing to do with this issue,
        invisible on SQLite only because ``_open_sqlite()`` has no teardown
        to trip over it.

        ``monkeypatch.setattr`` restores whatever ``_conn`` held before this
        call once the test ends, regardless of what the test body
        reassigns it to in between (``override_connection()``, the real
        ``close()`` in a ``finally`` block, ...) -- so the fixture's real
        connection is always intact again by the time its own teardown
        runs.
        """
        monkeypatch.setattr(db_mod, "_conn", None)

    def _route_to_fake_postgres(self, monkeypatch: pytest.MonkeyPatch) -> list[_FakePgConn]:
        opened: list[_FakePgConn] = []

        def _fake_open_postgres(dsn: str) -> _FakePgConn:
            conn = _FakePgConn(dsn)
            opened.append(conn)
            return conn

        monkeypatch.setattr(
            db_mod,
            "_resolve_store_target",
            lambda: db_mod._StoreTarget(
                backend=sql.DIALECT_POSTGRES, dsn="postgresql://user@host/db"
            ),
        )
        monkeypatch.setattr(db_mod, "_open_postgres", _fake_open_postgres)
        return opened

    def test_same_thread_reuses_the_same_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_override_without_closing(monkeypatch)
        opened = self._route_to_fake_postgres(monkeypatch)
        try:
            first = db_mod.get_connection()
            second = db_mod.get_connection()
            assert first is second
            assert len(opened) == 1
        finally:
            close()

    def test_different_threads_get_different_connections(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_override_without_closing(monkeypatch)
        opened = self._route_to_fake_postgres(monkeypatch)
        results: dict[str, object] = {}

        def _grab() -> None:
            results["other"] = db_mod.get_connection()

        try:
            main_conn = db_mod.get_connection()
            t = threading.Thread(target=_grab)
            t.start()
            t.join()
            assert results["other"] is not main_conn
            assert len(opened) == 2
        finally:
            close()
            # The spawned thread's connection is invisible to this thread's
            # close() (see close()'s docstring) -- clean it up directly so it
            # doesn't leak into another test.
            other = results.get("other")
            if isinstance(other, _FakePgConn):
                other.close()

    def test_override_connection_wins_over_postgres_routing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_override_without_closing(monkeypatch)
        opened = self._route_to_fake_postgres(monkeypatch)
        override_conn = sqlite3.connect(":memory:")
        override_connection(override_conn)
        try:
            assert db_mod.get_connection() is override_conn
            assert opened == []  # postgres routing never even consulted
        finally:
            close()

    def test_close_closes_this_threads_postgres_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_override_without_closing(monkeypatch)
        self._route_to_fake_postgres(monkeypatch)
        conn = db_mod.get_connection()
        assert isinstance(conn, _FakePgConn)
        close()
        assert conn.closed is True
        assert getattr(db_mod._pg_thread_local, "conn", None) is None

    def test_get_connection_reopens_after_the_cached_connection_is_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#3082: the per-thread cache has no invalidation path -- before the
        fix, once ``_pg_thread_local.conn`` was populated, get_connection()
        returned it unconditionally forever, even after something (the
        server, a schema-per-test teardown) closed it out from under this
        process. That is exactly what surfaced as 1,342 of the postgres
        job's failures: ``psycopg.OperationalError: the connection is
        closed`` on whatever statement ran next.

        Without the fix this goes red on the ``second is not first``
        assertion: get_connection() hands back the same (now-closed) object
        both times, since nothing ever discarded it from the thread-local
        cache."""
        self._clear_override_without_closing(monkeypatch)
        opened = self._route_to_fake_postgres(monkeypatch)
        try:
            first = db_mod.get_connection()
            first.close()  # simulate the server (or driver) dropping it
            second = db_mod.get_connection()
            assert second is not first
            assert second.closed is False
            assert len(opened) == 2  # the original open, plus the reopen
        finally:
            close()

    def test_get_connection_discards_a_closed_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#3082 suspect 2: ``override_connection()`` always wins over the
        thread-local cache, so a closed override that is never re-installed
        must not be handed back forever either. Discarding it re-enters the
        normal resolution path, which under pytest means the #1960/#827
        production-database guards fire loudly instead of silently returning
        a dead connection -- a strict improvement over wedging forever."""
        self._clear_override_without_closing(monkeypatch)
        fake = _FakePgConn("postgresql://user@host/db")
        override_connection(fake)
        fake.close()
        try:
            with pytest.raises(db_mod.ProductionDatabaseGuardError):
                db_mod.get_connection()
        finally:
            close()


class TestOpenPostgresPytestGuard:
    def test_open_postgres_refuses_under_pytest(self) -> None:
        """Mirrors #1960's SQLite guard: no test should ever reach a real
        Postgres connect call -- `PYTEST_CURRENT_TEST` is always set during a
        pytest run, so this fires before `sql.connect` (and therefore before
        any `import psycopg`) is ever reached."""
        with pytest.raises(db_mod.ProductionDatabaseGuardError):
            db_mod._open_postgres("postgresql://user@host/db")


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

    def test_migration_imports_board_proposals_splits_plans_session_and_merge_queue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#2724: exercises every ``_migrate_json`` branch not already covered
        above -- board.json (whose per-row write was ``INSERT OR REPLACE``,
        now ``sql.upsert``), proposals/split_proposals/plans (``INSERT OR
        IGNORE``, now ``sql.insert_ignore``), and split_chunks/sessions/
        merge_queue (plain ``INSERT``, now ``sql.execute``) -- all migrated
        off raw ``conn.execute()`` onto the coord.sql dialect seam. Also
        covers board.json listing the same ``assignment_id`` in both
        ``active`` and ``completed`` (a real shape the legacy writer
        produced), which exercises ``sql.upsert``'s ON CONFLICT DO UPDATE
        path, not just a first insert.
        """
        monkeypatch.setattr(db_mod, "COORD_DIR", tmp_path)
        self._write_json(tmp_path / "dispatched.json", [])

        board_data = {
            "round_number": 7,
            "active": [
                {
                    "assignment_id": "dup-1",
                    "machine_name": "laptop",
                    "repo_name": "api",
                    "issue_number": 5,
                    "issue_title": "first pass",
                    "status": "running",
                    "type": "work",
                },
            ],
            "completed": [
                {
                    "assignment_id": "dup-1",
                    "machine_name": "laptop",
                    "repo_name": "api",
                    "issue_number": 5,
                    "issue_title": "first pass",
                    "status": "done",
                    "type": "work",
                    "exit_code": 0,
                },
            ],
        }
        self._write_json(tmp_path / "board.json", board_data)
        self._write_json(
            tmp_path / "pending_proposals.json",
            [{"id": 1, "machine_name": "m", "repo_name": "api", "issue_number": 9,
              "issue_title": "prop"}],
        )
        self._write_json(
            tmp_path / "pending_splits.json",
            [{"id": 2, "repo_name": "api", "issue_number": 10, "issue_title": "split",
              "chunks": [{"title": "chunk A", "scope": "part A"}]}],
        )
        self._write_json(tmp_path / "plans.json", {"dup-1": {"steps": ["a", "b"]}})
        self._write_json(
            tmp_path / "session.json",
            {"started_at": "t0", "ended_at": "t1", "clean_shutdown": True,
             "total_cost_usd": 1.5},
        )
        self._write_json(
            tmp_path / "merge_queue.json",
            [{"assignment_id": "dup-1", "repo_name": "api", "repo_github": "acme/api",
              "branch": "issue-5", "target_branch": "main", "issue_number": 5,
              "issue_title": "first pass", "state": "pending"}],
        )

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        db_mod._migrate_json(conn)

        a_rows = conn.execute("SELECT * FROM assignments").fetchall()
        assert len(a_rows) == 1  # the duplicate assignment_id upserted, not duplicated
        assert a_rows[0]["status"] == "done"  # completed's row is the later upsert
        assert a_rows[0]["exit_code"] == 0

        round_row = conn.execute(
            "SELECT value FROM board_meta WHERE key='round_number'"
        ).fetchone()
        assert round_row["value"] == "7"
        init_row = conn.execute(
            "SELECT value FROM board_meta WHERE key='board_initialized'"
        ).fetchone()
        assert init_row["value"] == "1"

        p_rows = conn.execute("SELECT * FROM proposals").fetchall()
        assert len(p_rows) == 1
        assert p_rows[0]["issue_title"] == "prop"

        sp_rows = conn.execute("SELECT * FROM split_proposals").fetchall()
        assert len(sp_rows) == 1
        sc_rows = conn.execute("SELECT * FROM split_chunks").fetchall()
        assert len(sc_rows) == 1
        assert sc_rows[0]["title"] == "chunk A"

        plan_rows = conn.execute("SELECT * FROM plans").fetchall()
        assert len(plan_rows) == 1
        assert json.loads(plan_rows[0]["plan_data"]) == {"steps": ["a", "b"]}

        sess_rows = conn.execute("SELECT * FROM sessions").fetchall()
        assert len(sess_rows) == 1
        assert sess_rows[0]["clean_shutdown"] == 1
        assert sess_rows[0]["total_cost_usd"] == 1.5

        mq_rows = conn.execute("SELECT * FROM merge_queue").fetchall()
        assert len(mq_rows) == 1
        assert mq_rows[0]["branch"] == "issue-5"


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


# ── assignments.uat_state / uat_reason columns (#2687 / #2709) ─────────────

_UAT_STATE_AND_REASON_COLUMNS = {"uat_state", "uat_reason"}

# The `assignments` CREATE TABLE literal exactly as it stood immediately
# before #2687 (i.e. every column #2687 itself did NOT add), at
# schema_version 3 -- exactly what every real ``~/.coord/coord.db`` looked
# like the moment #2687 merged: #2675's fix had already bumped
# _DB_SCHEMA_VERSION to 3 for the #2604/#2589 columns, and #2687 landed its
# own two ALTER TABLE lines into `_migrate_add_columns` without a further
# bump (#2709).
_PRE_2687_ASSIGNMENTS_TABLE = """
    CREATE TABLE assignments (
        assignment_id TEXT PRIMARY KEY,
        machine_name TEXT NOT NULL,
        repo_name TEXT NOT NULL,
        repo_github TEXT,
        issue_number INTEGER NOT NULL,
        issue_title TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'running',
        type TEXT NOT NULL DEFAULT 'work',
        branch TEXT,
        pr_url TEXT,
        briefing TEXT DEFAULT '',
        files_allowed TEXT DEFAULT '[]',
        files_forbidden TEXT DEFAULT '[]',
        model TEXT,
        dispatched_at REAL,
        finished_at REAL,
        smoke_test TEXT,
        smoke_test_reason TEXT,
        review_state TEXT,
        review_of_assignment_id TEXT,
        review_target TEXT,
        required_gates TEXT DEFAULT '[]',
        plan TEXT,
        unreachable_count INTEGER DEFAULT 0,
        exit_code INTEGER,
        review_iteration INTEGER DEFAULT 0,
        review_posted_at REAL,
        test_state TEXT,
        test_reason TEXT,
        cost_usd REAL,
        smoke_tests TEXT,
        review_findings TEXT,
        test_plan TEXT
    )
"""


def _seed_pre_2687_database(db_path: Path) -> None:
    """Write a real on-disk database at schema_version 3 whose ``assignments``
    table predates #2687 -- the exact #2709 repro.  ``_open()``'s
    ``_read_schema_version(conn) < _DB_SCHEMA_VERSION`` compare read
    ``3 < 3`` as False (before this fix) and skipped ``_ensure_schema`` --
    and therefore ``_migrate_add_columns`` -- forever, on every database
    already at version 3, leaving ``uat_state``/``uat_reason`` permanently
    missing.
    """
    raw = sqlite3.connect(str(db_path))
    raw.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
    raw.execute("INSERT INTO schema_version VALUES (3)")
    raw.execute(_PRE_2687_ASSIGNMENTS_TABLE)
    raw.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, issue_number, issue_title) "
        "VALUES ('a1', 'host1', 'api', 42, 'demo')"
    )
    raw.commit()
    raw.close()


class TestUatStateAndReasonColumns:
    def test_existing_version_3_database_gains_them_on_open(
        self, tmp_path: Path
    ) -> None:
        """The exact #2709 repro.  ``save_board``'s ``_UPSERT_SQL`` writes
        ``uat_state``/``uat_reason`` unconditionally for every assignments
        row (``coord/state.py``) -- a database that never gains these
        columns makes every dispatch write raise ``OperationalError:
        table assignments has no column named uat_state``, forever, on
        every existing installation.
        """
        db_path = tmp_path / "coord.db"
        _seed_pre_2687_database(db_path)

        before = sqlite3.connect(str(db_path))
        cols_before = {
            r[1] for r in before.execute("PRAGMA table_info(assignments)")
        }
        before.close()
        assert not (_UAT_STATE_AND_REASON_COLUMNS & cols_before)

        conn = db_mod._open(db_path)
        try:
            cols_after = {
                r[1] for r in conn.execute("PRAGMA table_info(assignments)")
            }
            assert _UAT_STATE_AND_REASON_COLUMNS <= cols_after

            row = conn.execute(
                "SELECT uat_state, uat_reason FROM assignments "
                "WHERE assignment_id = 'a1'"
            ).fetchone()
            # A row predating these columns reads as "no UAT verdict yet" --
            # never a silent behavior change for an entry that never had one.
            assert row["uat_state"] is None
            assert row["uat_reason"] is None

            version = conn.execute(
                "SELECT MAX(version) FROM schema_version"
            ).fetchone()[0]
            assert version == db_mod._DB_SCHEMA_VERSION
        finally:
            conn.close()

    def test_migration_is_idempotent(self, tmp_path: Path) -> None:
        db_path = tmp_path / "coord.db"
        _seed_pre_2687_database(db_path)

        for _ in range(3):
            conn = db_mod._open(db_path)
            conn.close()

        conn = db_mod._open(db_path)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(assignments)")}
            assert _UAT_STATE_AND_REASON_COLUMNS <= cols
        finally:
            conn.close()

    def test_save_board_records_a_dispatch_without_raising(
        self, tmp_path: Path
    ) -> None:
        """``save_board`` is the exact function+line from #2709's
        traceback (``conn.execute(_UPSERT_SQL, _assignment_upsert_params(a))``)
        -- the write path that records every dispatch onto the board,
        whole-board snapshot included (`coord.state.save_board`'s own
        docstring: the daemon's generic ``/board`` thin-client endpoint
        backing ``assign``/``approve``/... and ``auto_loop``). Confirms it
        no longer raises against a database that was stuck at version 3.
        """
        from coord import state
        from coord.models import Assignment, Board

        db_path = tmp_path / "coord.db"
        _seed_pre_2687_database(db_path)

        conn = db_mod._open(db_path)
        override_connection(conn)
        try:
            board = Board(
                active=[
                    Assignment(
                        machine_name="host1",
                        repo_name="api",
                        issue_number=99,
                        issue_title="new dispatch",
                        assignment_id="a2",
                        status="running",
                    )
                ]
            )
            state.save_board(board)  # must not raise OperationalError

            row = conn.execute(
                "SELECT uat_state, uat_reason FROM assignments "
                "WHERE assignment_id = 'a2'"
            ).fetchone()
            assert row["uat_state"] is None
            assert row["uat_reason"] is None
        finally:
            close()


# ── _migrate_add_columns / _DB_SCHEMA_VERSION structural guard (#2709) ─────

# Pinned together: (_DB_SCHEMA_VERSION, len(_MIGRATE_ADD_COLUMNS)) as of this
# commit.  #2604, #2589, and #2687 each appended an entry to
# `_migrate_add_columns` without bumping `_DB_SCHEMA_VERSION` -- the
# hand-written warning comment directly above `_DB_SCHEMA_VERSION` in
# coord/db.py did not stop the third occurrence (#2687).  This test is the
# structural version: change either number in coord/db.py without updating
# this pinned tuple to match, and it fails red -- see the test body for the
# exact assertion.
#
# #2786 bumped this to (6, 74): `assignments.num_turns`.
# #2987 bumped this to (7, 76): `portal_sync_state.relayed_answer_
# watermark_at` / `relayed_answer_watermark_rowid`.
# #3114 bumped this to (9, 80): `merge_queue.ci_fix_detail_sha` /
# `ci_fix_detail_json`.
# #3113 bumped this to (10, 80): a new `review_claims` TABLE (not a column —
# `_MIGRATE_ADD_COLUMNS`'s length is unchanged at 80, so only the version
# moved, to force `_ensure_schema`'s `CREATE TABLE IF NOT EXISTS` to run once
# more on an existing database).
_PINNED_SCHEMA_VERSION_AND_MIGRATION_COUNT = (10, 80)


class TestMigrateAddColumnsVersionGuard:
    def test_migration_count_is_pinned_to_the_schema_version(self) -> None:
        """Appending a new ``ALTER TABLE ...`` line to
        ``coord.db._MIGRATE_ADD_COLUMNS`` changes ``len(...)`` without
        touching ``_DB_SCHEMA_VERSION`` -- exactly the #2604/#2589/#2687
        mistake.  This assertion goes red the moment that happens, because
        the actual ``(version, count)`` pair no longer matches the value
        pinned above.

        The fix when this fires: bump ``_DB_SCHEMA_VERSION`` in
        ``coord/db.py``, then update
        ``_PINNED_SCHEMA_VERSION_AND_MIGRATION_COUNT`` above to match the
        new pair -- in the SAME commit as the new migration entry, which is
        the whole point (#2709).
        """
        actual = (db_mod._DB_SCHEMA_VERSION, len(db_mod._MIGRATE_ADD_COLUMNS))
        assert actual == _PINNED_SCHEMA_VERSION_AND_MIGRATION_COUNT, (
            f"coord.db._DB_SCHEMA_VERSION={actual[0]}, "
            f"len(_MIGRATE_ADD_COLUMNS)={actual[1]} no longer matches the "
            f"pinned {_PINNED_SCHEMA_VERSION_AND_MIGRATION_COUNT}. If you "
            "just appended a migration, bump _DB_SCHEMA_VERSION in "
            "coord/db.py (#2709 -- this is the third time an appended "
            "migration shipped without one) and update the pinned tuple "
            "above to match, in the same commit."
        )


# ── _migrate_add_columns rolls back on a Postgres-style tx abort (#2982) ───
#
# Root cause: every entry in `_MIGRATE_ADD_COLUMNS` duplicates a column
# `_SCHEMA_SQL` already creates, so on a freshly-created schema the very
# first ALTER in the loop errors. SQLite tolerates a failed statement without
# touching the transaction, so the old `except sql.driver_errors(): pass`
# was invisible there -- but Postgres aborts the *whole* transaction on any
# error, so without a `conn.rollback()` every statement after the first
# swallowed error fails with `psycopg.errors.InFailedSqlTransaction`,
# including `_set_schema_version`'s own `DELETE FROM schema_version` inside
# the very same `_ensure_schema()` call that is running the loop.


# #2983 generalized this stub so the other 20 swallowed-driver-error sites
# (coord/state.py, coord/dao.py, coord/merge_queue.py) can drive the same
# defect shape without each test file growing its own copy -- see
# `PostgresStyleDriverError` below for the one behavioural change: the
# simulated errors now carry a SQLSTATE, because that (not the connection's
# dialect) is what `db.rollback_after_driver_error` keys off.

# SQLSTATEs the stub hands out.  Real codes, so a reader can look them up.
SQLSTATE_UNDEFINED_TABLE = "42P01"
SQLSTATE_IN_FAILED_SQL_TRANSACTION = "25P02"
SQLSTATE_SERIALIZATION_FAILURE = "40001"  # one of db._POSTGRES_LOCK_CONTENTION_SQLSTATES


class PostgresStyleDriverError(sqlite3.Error):
    """A driver error that is Postgres-shaped in the one way #2983 cares
    about -- it carries a SQLSTATE -- while still being an instance of
    ``sqlite3.Error``, so ``sql.driver_errors()`` / ``sql.driver_error(conn)``
    catch it against a SQLite connection exactly as psycopg's would against
    a real one.

    Deliberately NOT a ``sqlite3.OperationalError`` subclass:
    ``db.is_lock_contention_error`` checks ``isinstance(exc,
    sqlite3.OperationalError)`` *first* and, for that branch, only looks at
    the message text.  Subclassing the base ``sqlite3.Error`` instead sends
    it down the same ``.sqlstate``/``.pgcode`` branch a real psycopg error
    takes, which is what makes ``SQLSTATE_SERIALIZATION_FAILURE`` read as
    genuine lock contention here without any message-text games.
    """

    def __init__(self, message: str, sqlstate: str) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


class AbortOnErrorCursor:
    """Delegates to a real sqlite3 cursor, except it flags its parent
    connection as aborted the moment a statement raises -- see
    ``AbortOnErrorConn`` below."""

    def __init__(self, real_cursor: Any, parent: "AbortOnErrorConn") -> None:
        self._real = real_cursor
        self._parent = parent

    def execute(self, *args: Any, **kwargs: Any) -> "AbortOnErrorCursor":
        if self._parent.aborted:
            raise PostgresStyleDriverError(
                "current transaction is aborted, commands ignored until end of "
                "transaction block",
                SQLSTATE_IN_FAILED_SQL_TRANSACTION,
            )
        try:
            self._real.execute(*args, **kwargs)
        except sqlite3.Error as exc:
            self._parent.aborted = True
            raise PostgresStyleDriverError(str(exc), self._parent.sqlstate) from exc
        return self

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class AbortOnErrorConn:
    """Wraps a real, already-schema'd sqlite3 connection and reproduces
    Postgres's abort-the-whole-transaction-on-error behaviour on top of it:
    once a statement raises, every later statement -- including
    ``commit()`` -- raises the same error until ``rollback()`` clears it.

    This is what lets the #2982/#2983 regressions be driven -- and go red on
    the pre-fix code -- on a SQLite-only dev machine with no Postgres server:
    nothing here talks to a real Postgres, but the observable shape (a
    swallowed error leaves the connection unusable until rolled back) is
    the same defect.

    ``rollbacks`` counts recovery attempts so a test can assert the *absence*
    of a rollback too -- which is how "SQLite behaviour is byte-identical"
    gets a real assertion rather than a promise.
    """

    def __init__(
        self, real: sqlite3.Connection, *, sqlstate: str = SQLSTATE_UNDEFINED_TABLE
    ) -> None:
        self._real = real
        self.aborted = False
        self.sqlstate = sqlstate
        self.rollbacks = 0

    def cursor(self) -> AbortOnErrorCursor:
        if self.aborted:
            raise PostgresStyleDriverError(
                "current transaction is aborted", SQLSTATE_IN_FAILED_SQL_TRANSACTION
            )
        return AbortOnErrorCursor(self._real.cursor(), self)

    def commit(self) -> None:
        if self.aborted:
            raise PostgresStyleDriverError(
                "current transaction is aborted", SQLSTATE_IN_FAILED_SQL_TRANSACTION
            )
        self._real.commit()

    def rollback(self) -> None:
        self.rollbacks += 1
        self.aborted = False
        self._real.rollback()

    def close(self) -> None:
        self._real.close()


def pin_sqlite_dialect(monkeypatch: pytest.MonkeyPatch) -> None:
    """``sql.detect_dialect`` keys off ``type(conn).__module__``, which
    :class:`AbortOnErrorConn` fails (it isn't a ``sqlite3.Connection``
    subclass) -- pin it to ``"sqlite"`` so ``sql.execute``/
    ``sql.executescript``'s ``translate()`` and ``cursor()`` calls behave
    exactly as they would against the real connection underneath, and so
    ``sql.driver_error(conn)`` still resolves to ``sqlite3.Error``.

    Note this pin does NOT weaken what the #2983 tests prove:
    ``db.rollback_after_driver_error`` dispatches on the *exception's*
    SQLSTATE, never on the connection's dialect, precisely so that the
    SQLite path stays byte-identical by construction.
    """
    monkeypatch.setattr(sql, "detect_dialect", lambda conn: sql.DIALECT_SQLITE)


def schema_migrated_sqlite_connection(*, drop: tuple[str, ...] = ()) -> sqlite3.Connection:
    """A real, schema-migrated in-memory SQLite connection for
    :class:`AbortOnErrorConn` to wrap, with *drop* tables removed again.

    The **only** ``sqlite3.connect`` site the #2982/#2983 abort simulations
    use, deliberately: ``tests/test_sqlite_connect_ratchet.py`` pins a
    per-file count, and routing all four test files (``test_db``,
    ``test_dao``, ``test_state``, ``test_merge_queue``) through this one
    factory means the sweep adds no new site to any of them.

    The connection underneath has to be a genuine sqlite3 one — the stub
    raises/catches ``sqlite3.Error`` and :func:`pin_sqlite_dialect` pins
    ``sql.detect_dialect``, so ``translate()``/``cursor()`` behave exactly as
    they do against the real driver.  ``coord_db`` is out (it is already
    installed as ``override_connection``, and under
    ``COORD_TEST_BACKEND=postgres`` it is a psycopg connection that already
    aborts for real, which is the behaviour being *simulated* here) and
    ``scratch_database()`` is out for the same backend-following reason.

    *drop* exists because the sites #2983 fixes swallow a **missing table**
    error, and the table in question is either created by
    ``coord.housekeeping`` rather than ``_ensure_schema`` (``assignments_
    archive``, ``merge_queue_archive`` — nothing to drop, absent already) or
    is in the schema and has to be taken back out (``audit_log``,
    ``schema_version``).
    """
    real = sqlite3.connect(":memory:")
    real.row_factory = sqlite3.Row
    db_mod._ensure_schema(real)
    for table in drop:
        sql.execute(real, f"DROP TABLE IF EXISTS {table}")  # noqa: S608 -- test-local literal
    real.commit()
    return real


def abort_simulating_connection(
    monkeypatch: pytest.MonkeyPatch,
    real: sqlite3.Connection,
    *,
    sqlstate: str = SQLSTATE_UNDEFINED_TABLE,
) -> AbortOnErrorConn:
    """The two lines every #2983 regression test needs, in one call."""
    pin_sqlite_dialect(monkeypatch)
    return AbortOnErrorConn(real, sqlstate=sqlstate)


class TestMigrateAddColumnsRollsBackOnDriverError:
    """Drives the #2982 regression with the abort-simulating stub above --
    meaningful without a Postgres server, per the issue's acceptance
    criterion."""

    def _wrapped_conn_with_columns_already_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> AbortOnErrorConn:
        # every _MIGRATE_ADD_COLUMNS column now exists, so every ALTER is a dup
        real = schema_migrated_sqlite_connection()
        return abort_simulating_connection(monkeypatch, real)

    def test_ensure_schema_completes_when_a_duplicate_alter_aborts_the_tx(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The acceptance criterion, verbatim: `_ensure_schema()` runs to
        completion, and `schema_version` ends up holding
        `_DB_SCHEMA_VERSION`, on a connection where a duplicate ALTER
        errors. Pre-fix, this raises `OperationalError("current
        transaction is aborted")` out of `_set_schema_version`'s `DELETE
        FROM schema_version` -- the exact failure the issue reports,
        reproduced without touching a real Postgres."""
        conn = self._wrapped_conn_with_columns_already_present(monkeypatch)

        db_mod._ensure_schema(conn)  # every ALTER this pass is a duplicate

        rows = sql.execute(conn, "SELECT version FROM schema_version").fetchall()
        assert len(rows) == 1
        assert rows[0]["version"] == db_mod._DB_SCHEMA_VERSION

    def test_migrate_add_columns_leaves_the_connection_usable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Narrower unit check directly on `_migrate_add_columns`: every one
        of the 74 duplicate ALTERs must be individually recovered, not just
        "the suite happens to still work by the end" -- a plain `commit()`
        right after must not raise."""
        conn = self._wrapped_conn_with_columns_already_present(monkeypatch)

        db_mod._migrate_add_columns(conn)

        conn.commit()  # raises "current transaction is aborted" pre-fix


class TestMigrateAddColumnsRollsBackOnRealPostgres:
    """The same #2982 regression against an actual Postgres server, when one
    is reachable -- the stub-based class above proves the fix is logically
    correct; this proves it against the real driver-error shape
    (`psycopg.errors.InFailedSqlTransaction`) the issue was filed against.
    Skips outright on a machine with no Postgres server, matching the
    #2885 write-parity harness's own skip pattern."""

    def test_ensure_schema_completes_on_postgres_with_columns_already_present(
        self,
    ) -> None:
        unavailable = backends.postgres_available()
        if unavailable:
            pytest.skip(f"no Postgres backend available: {unavailable}")

        session = backends.open_named_session(backends.BACKEND_POSTGRES)
        try:
            db_mod._ensure_schema(session.conn)  # first pass: fresh schema
            db_mod._ensure_schema(session.conn)  # second pass: every ALTER duplicates

            rows = sql.execute(
                session.conn, "SELECT version FROM schema_version"
            ).fetchall()
            assert len(rows) == 1
            assert rows[0]["version"] == db_mod._DB_SCHEMA_VERSION
        finally:
            session.close()


# ── #2983: the other 20 swallowed-driver-error sites ────────────────────────
#
# #2982 fixed one instance of "catch a driver error through the dialect seam,
# then keep using the same connection".  This sweeps the rest.  The shared
# rule lives in `db.rollback_after_driver_error`; these tests pin (a) that
# helper's contract, (b) `retry_on_locked`'s "never leaves an aborted
# transaction behind" guarantee, which is what makes the ~13 handlers whose
# `try` body is just a `retry_on_locked(...)` call safe without their own
# rollback, and (c) `_read_schema_version`, which #2982's fix does NOT cover.


class _RollbackRecorder:
    """Minimal connection stand-in: records rollbacks, nothing else."""

    def __init__(self, *, fail: bool = False) -> None:
        self.rollbacks = 0
        self._fail = fail

    def rollback(self) -> None:
        self.rollbacks += 1
        if self._fail:
            raise sqlite3.OperationalError("rollback itself failed")

    def close(self) -> None:
        """Only so a test may install this as ``db._conn`` and still survive
        the ``coord_db`` fixture's ``db.close()`` teardown."""


class TestRollbackAfterDriverError:
    """The #2983 rule, unit-tested: roll back iff the caught exception is a
    Postgres driver error (it carries a SQLSTATE)."""

    def test_rolls_back_for_a_psycopg3_style_sqlstate(self) -> None:
        conn = _RollbackRecorder()

        db_mod.rollback_after_driver_error(
            conn, PostgresStyleDriverError("no such table", SQLSTATE_UNDEFINED_TABLE)
        )

        assert conn.rollbacks == 1

    def test_rolls_back_for_a_psycopg2_style_pgcode(self) -> None:
        """psycopg2 spells the same field ``.pgcode`` — both must work, same
        as ``is_lock_contention_error`` already accepts either."""
        conn = _RollbackRecorder()
        exc = sqlite3.OperationalError("relation does not exist")
        exc.pgcode = SQLSTATE_UNDEFINED_TABLE  # type: ignore[attr-defined]

        db_mod.rollback_after_driver_error(conn, exc)

        assert conn.rollbacks == 1

    def test_plain_sqlite_error_is_left_completely_alone(self) -> None:
        """The "SQLite behaviour is byte-identical" criterion, as an
        assertion rather than a promise: a real ``sqlite3`` error carries no
        SQLSTATE, so no SQLite caller can lose uncommitted work it expected
        to survive into the retry/next statement."""
        conn = _RollbackRecorder()

        db_mod.rollback_after_driver_error(
            conn, sqlite3.OperationalError("database is locked")
        )

        assert conn.rollbacks == 0

    def test_none_connection_is_a_no_op(self) -> None:
        db_mod.rollback_after_driver_error(
            None, PostgresStyleDriverError("boom", SQLSTATE_UNDEFINED_TABLE)
        )  # must not raise

    def test_a_failing_rollback_never_masks_the_caught_error(self) -> None:
        """This runs inside an ``except`` block; a secondary error from the
        recovery attempt replacing the original driver error would be
        strictly worse than leaving the connection unrecovered."""
        conn = _RollbackRecorder(fail=True)

        db_mod.rollback_after_driver_error(
            conn, PostgresStyleDriverError("boom", SQLSTATE_UNDEFINED_TABLE)
        )

        assert conn.rollbacks == 1


class TestBoardConnectionIfOpen:
    """`retry_on_locked`'s default connection resolution — it must never
    *open* one from inside an error handler."""

    def test_returns_the_installed_override(self, coord_db) -> None:
        assert db_mod._board_connection_if_open() is coord_db

    def test_returns_none_when_nothing_is_open(self, monkeypatch) -> None:
        monkeypatch.setattr(db_mod, "_conn", None)
        monkeypatch.setattr(db_mod, "_pg_thread_local", threading.local())

        assert db_mod._board_connection_if_open() is None


class TestRetryOnLockedRecoversAbortedTransaction:
    """The highest-value site in #2983: on Postgres the first contention
    failure aborts the transaction, so without a rollback attempt 2 onwards
    raises ``InFailedSqlTransaction`` (which is not lock contention), the
    loop bails out immediately, and the whole #2597/#2689 retry budget is
    silently one attempt."""

    def test_the_retry_budget_still_works_after_a_postgres_style_abort(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(db_mod.time, "sleep", lambda _s: None)
        conn = abort_simulating_connection(
            monkeypatch,
            schema_migrated_sqlite_connection(),
            sqlstate=SQLSTATE_SERIALIZATION_FAILURE,
        )
        attempts: list[int] = []

        def _write() -> str:
            attempts.append(len(attempts) + 1)
            if len(attempts) < 3:
                # A genuinely failing statement — this is what aborts the tx.
                sql.execute(conn, "SELECT 1 FROM no_such_table")
            sql.execute(conn, "SELECT 1")
            conn.commit()
            return "written"

        # Pre-fix this raises on attempt 2: the aborted-transaction error is
        # not lock contention, so `retry_on_locked` re-raises it instead of
        # retrying, and reports the wrong error to boot.
        assert db_mod.retry_on_locked(_write, conn=conn) == "written"
        assert len(attempts) == 3
        assert conn.rollbacks == 2  # one per swallowed collision

    def test_connection_is_usable_after_the_budget_is_exhausted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The acceptance criterion for this site: drive the error path, then
        perform a further operation on the same connection.  ~13 callers
        catch this raise and *swallow* it (a cache mirror whose GitHub write
        already landed must not 503), then keep using the same singleton."""
        monkeypatch.setattr(db_mod.time, "sleep", lambda _s: None)
        conn = abort_simulating_connection(
            monkeypatch,
            schema_migrated_sqlite_connection(),
            sqlstate=SQLSTATE_SERIALIZATION_FAILURE,
        )

        with pytest.raises(sqlite3.Error):
            db_mod.retry_on_locked(
                lambda: sql.execute(conn, "SELECT 1 FROM no_such_table"), conn=conn
            )

        # Pre-fix: raises "current transaction is aborted".
        assert sql.execute(conn, "SELECT 1 AS ok").fetchone()["ok"] == 1

    def test_sqlite_contention_path_is_byte_identical(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A real SQLite ``database is locked`` error carries no SQLSTATE, so
        the retry loop behaves exactly as it did before #2983 — no rollback,
        so a multi-statement writer's uncommitted work still survives into
        the retry."""
        monkeypatch.setattr(db_mod.time, "sleep", lambda _s: None)
        conn = _RollbackRecorder()
        calls: list[int] = []

        def _write() -> str:
            calls.append(1)
            if len(calls) < 2:
                raise sqlite3.OperationalError("database is locked")
            return "written"

        assert db_mod.retry_on_locked(_write, conn=conn) == "written"
        assert conn.rollbacks == 0

    def test_defaults_to_the_open_board_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No call site in coord/ passes ``conn=`` — they all write through
        ``get_connection()``, so the default has to find it, which is what
        keeps ``coord/audit.py``, ``coord/auto_loop.py`` and
        ``coord/commands/merge.py`` fixed without being touched."""
        monkeypatch.setattr(db_mod.time, "sleep", lambda _s: None)
        conn = _RollbackRecorder()
        monkeypatch.setattr(db_mod, "_conn", conn)

        with pytest.raises(PostgresStyleDriverError):
            db_mod.retry_on_locked(
                lambda: (_ for _ in ()).throw(
                    PostgresStyleDriverError("aborted", SQLSTATE_UNDEFINED_TABLE)
                )
            )

        assert conn.rollbacks == 1


class TestReadSchemaVersionRollsBackOnDriverError:
    """`_read_schema_version`'s "table doesn't exist yet" swallow is on the
    same first-open path #2982 fixed, but one layer earlier, and #2982's fix
    does not reach it: on a fresh Postgres database this is the FIRST
    statement `_migrate_if_needed` runs, its `UndefinedTable` aborts the
    transaction, and the caller then continues on the same connection
    straight into `_fix_schema_version_table`'s `CREATE TABLE IF NOT
    EXISTS`."""

    def test_returns_zero_and_leaves_the_connection_usable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = abort_simulating_connection(
            monkeypatch, schema_migrated_sqlite_connection(drop=("schema_version",))
        )

        assert db_mod._read_schema_version(conn) == 0

        # Pre-fix: raises "current transaction is aborted".
        assert sql.execute(conn, "SELECT 1 AS ok").fetchone()["ok"] == 1

    def test_migrate_if_needed_completes_over_the_swallowed_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole first-open path, end to end — the caller really does
        keep using the connection, so this is the shape that broke."""
        conn = abort_simulating_connection(
            monkeypatch, schema_migrated_sqlite_connection(drop=("schema_version",))
        )

        db_mod._migrate_if_needed(
            conn, is_production=False, target_desc="a #2983 scratch database"
        )

        rows = sql.execute(conn, "SELECT version FROM schema_version").fetchall()
        assert [r["version"] for r in rows] == [db_mod._DB_SCHEMA_VERSION]


class TestReadSchemaVersionRollsBackOnRealPostgres:
    """The same regression against an actual Postgres server, when one is
    reachable — `psycopg.errors.UndefinedTable` then
    `InFailedSqlTransaction`, the real shapes the stub above simulates."""

    def test_migrate_if_needed_completes_on_a_fresh_postgres_schema(self) -> None:
        unavailable = backends.postgres_available()
        if unavailable:
            pytest.skip(f"no Postgres backend available: {unavailable}")

        session = backends.open_named_session(backends.BACKEND_POSTGRES)
        try:
            # A brand-new private schema: `schema_version` genuinely does not
            # exist, so `_read_schema_version`'s SELECT really does raise
            # UndefinedTable and really does abort the transaction.
            db_mod._migrate_if_needed(
                session.conn,
                is_production=False,
                target_desc="a #2983 scratch Postgres schema",
            )

            rows = sql.execute(
                session.conn, "SELECT version FROM schema_version"
            ).fetchall()
            assert [r["version"] for r in rows] == [db_mod._DB_SCHEMA_VERSION]
        finally:
            session.close()


# ── #2752: non-release builds must not stamp the production DB's schema ─────
#
# #1960's guard only fires under pytest, so it can't prove anything about
# _open()'s *other* trigger (a non-release build). These tests bypass the
# pytest trigger deliberately (monkeypatch.delenv) so the version-shape guard
# is exercised on its own, then restore DB_PATH via monkeypatch teardown.

class TestIsReleaseBuild:
    """Unit tests for the version-shape test itself -- no I/O."""

    @pytest.mark.parametrize(
        "version",
        [
            "0.5.244",
            "1.0.0",
            "0.0.1",
        ],
    )
    def test_clean_tag_is_a_release_build(self, version: str) -> None:
        assert db_mod._is_release_build(version) is True

    @pytest.mark.parametrize(
        "version",
        [
            "0.5.244.dev11+g98076d93e",  # setuptools_scm dev/local suffix
            "0+unknown",  # #2010 sentinel: no distribution installed at all
            "0.5.244-11-g98076d93e-dirty",  # git-describe fallback
            "0.5.244-11-g98076d93e",  # git-describe fallback, not dirty
            "0.0.0.dev0",  # setuptools_scm.get_version's own fallback_version
        ],
    )
    def test_non_release_shapes_are_not_a_release_build(self, version: str) -> None:
        assert db_mod._is_release_build(version) is False


class TestProductionDatabaseGuardBlocksNonReleaseSchemaWrites:
    def _bypass_pytest_trigger(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unset PYTEST_CURRENT_TEST so _open()'s #1960 pytest guard doesn't
        fire first and mask the #2752 guard under test here."""
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    def test_dev_build_refuses_to_stamp_a_fresh_production_db(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A brand-new (unstamped) DB_PATH is exactly the schema-write path
        -- _read_schema_version reads 0, which is < _DB_SCHEMA_VERSION -- so
        a non-release build must refuse rather than stamp it forward."""
        self._bypass_pytest_trigger(monkeypatch)
        db_path = tmp_path / "coord.db"
        monkeypatch.setattr(db_mod, "DB_PATH", db_path)
        monkeypatch.setattr(db_mod, "_coord_version", "0.5.244.dev11+g98076d93e")

        with pytest.raises(db_mod.ProductionDatabaseGuardError) as excinfo:
            db_mod._open(db_path)

        message = str(excinfo.value)
        assert str(db_path) in message
        assert "2752" in message

    def test_dev_build_refuses_to_advance_a_stale_schema_version(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A database already stamped at an OLDER version (the #2675/#2709
        shape: released code bumped _DB_SCHEMA_VERSION since this file was
        last opened) must also refuse under a non-release build -- not just
        a from-scratch database."""
        self._bypass_pytest_trigger(monkeypatch)
        db_path = tmp_path / "coord.db"
        monkeypatch.setattr(db_mod, "DB_PATH", db_path)

        # First, a release build opens it and stamps the current version.
        monkeypatch.setattr(db_mod, "_coord_version", "0.5.244")
        conn = db_mod._open(db_path)
        conn.close()

        # Simulate a released migration landing later under a higher
        # version number by bumping the module's notion of "current".
        monkeypatch.setattr(db_mod, "_DB_SCHEMA_VERSION", db_mod._DB_SCHEMA_VERSION + 1)
        monkeypatch.setattr(db_mod, "_coord_version", "0.5.245.dev3+gdeadbeef")

        with pytest.raises(db_mod.ProductionDatabaseGuardError):
            db_mod._open(db_path)

    def test_release_build_is_unaffected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sanity check: the guard is scoped to non-release builds -- a
        release build must keep stamping a fresh DB exactly as before."""
        self._bypass_pytest_trigger(monkeypatch)
        db_path = tmp_path / "coord.db"
        monkeypatch.setattr(db_mod, "DB_PATH", db_path)
        monkeypatch.setattr(db_mod, "_coord_version", "0.5.244")

        conn = db_mod._open(db_path)
        try:
            conn.execute("SELECT 1 FROM machines")
        finally:
            conn.close()

    def test_dev_build_does_not_block_reads_of_an_already_current_database(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard is scoped to schema *writes* -- once a database is
        already caught up to _DB_SCHEMA_VERSION, a non-release build can
        still open (read) it, matching #2752's "reads stay unaffected"."""
        self._bypass_pytest_trigger(monkeypatch)
        db_path = tmp_path / "coord.db"
        monkeypatch.setattr(db_mod, "DB_PATH", db_path)

        # A release build brings it up to the current version first.
        monkeypatch.setattr(db_mod, "_coord_version", "0.5.244")
        conn = db_mod._open(db_path)
        conn.close()

        # A non-release build re-opening the now-current database must not
        # raise -- there is no schema write left to guard against.
        monkeypatch.setattr(db_mod, "_coord_version", "0.5.244.dev11+g98076d93e")
        conn = db_mod._open(db_path)
        try:
            conn.execute("SELECT 1 FROM machines")
        finally:
            conn.close()

    def test_dev_build_still_blocked_under_pytest_via_the_1960_guard(self) -> None:
        """Without bypassing PYTEST_CURRENT_TEST, opening the real DB_PATH
        still raises via the pre-existing #1960 guard -- this fix adds a
        second trigger, it does not weaken the first."""
        with pytest.raises(db_mod.ProductionDatabaseGuardError):
            db_mod._open(db_mod.DB_PATH)
