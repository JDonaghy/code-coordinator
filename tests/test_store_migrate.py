"""Tests for ``coord.store_migrate`` -- the one-shot SQLite -> Postgres
importer (#828 second half).

The source side is always real SQLite (that's the scenario: "load an
existing ``~/.coord/coord.db``"), built through ``coord.db._open()`` exactly
like ``tests/test_db.py`` does for its own migration tests. The *target*
side goes through ``tests/backends.py``'s ``scratch_database`` -- a second,
already-``_ensure_schema``'d database on whatever ``COORD_TEST_BACKEND``
selects. Unset (the default), that's a second SQLite file, so the whole
orchestration below (table discovery, both audits, row-count parity,
idempotency) runs on every machine with no server or driver required.
``COORD_TEST_BACKEND=postgres`` (CI's ``postgres`` job) points the exact same
tests at a real Postgres schema, so ``run_import``'s Postgres write path
gets a real round trip, not just a dialect-primitive assertion.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from coord import db as coord_db
from coord import sql
from coord import store_migrate
from tests.backends import scratch_database


def _open_source(tmp_path: Path, name: str = "source.db") -> sqlite3.Connection:
    return coord_db._open(tmp_path / name)


class TestDiscoverTables:
    def test_includes_static_schema_tables(self, tmp_path: Path) -> None:
        conn = _open_source(tmp_path)
        try:
            tables = store_migrate.discover_tables(conn)
            assert "assignments" in tables
            assert "issues" in tables
            assert "split_proposals" in tables
            assert "split_chunks" in tables
        finally:
            conn.close()

    def test_excludes_schema_version_bookkeeping(self, tmp_path: Path) -> None:
        conn = _open_source(tmp_path)
        try:
            tables = store_migrate.discover_tables(conn)
            assert "schema_version" not in tables
            assert "schema_version_new" not in tables
        finally:
            conn.close()

    def test_discovers_dynamic_archive_mirror_not_in_static_schema(
        self, tmp_path: Path
    ) -> None:
        """coord/housekeeping.py:92 creates archive mirrors dynamically --
        this must find them without a hardcoded name, proving the importer
        covers them without enumerating them."""
        conn = _open_source(tmp_path)
        try:
            sql.execute(conn, 'CREATE TABLE assignments_archive ("assignment_id" TEXT)')
            conn.commit()
            tables = store_migrate.discover_tables(conn)
            assert "assignments_archive" in tables
        finally:
            conn.close()


class TestAuditTypeAffinity:
    def test_clean_database_has_no_violations(self, tmp_path: Path) -> None:
        conn = _open_source(tmp_path)
        try:
            conn.execute(
                "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
                "issue_number, issue_title) VALUES (?, ?, ?, ?, ?)",
                ("a1", "m1", "r1", 42, "t1"),
            )
            conn.commit()
            tables = store_migrate.discover_tables(conn)
            assert store_migrate.audit_type_affinity(conn, tables) == []
        finally:
            conn.close()

    def test_detects_string_drifted_into_integer_column(self, tmp_path: Path) -> None:
        """The exact SQLite-ism this half of #828 exists for: a column
        declared INTEGER can still hold a TEXT storage-class value when
        SQLite couldn't losslessly convert what was inserted -- Postgres
        would reject it outright."""
        conn = _open_source(tmp_path)
        try:
            conn.execute(
                "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
                "issue_number, issue_title) VALUES (?, ?, ?, ?, ?)",
                ("a1", "m1", "r1", "not-a-number", "t1"),
            )
            conn.commit()
            tables = store_migrate.discover_tables(conn)
            violations = store_migrate.audit_type_affinity(conn, tables)
            assert len(violations) == 1
            v = violations[0]
            assert v.table == "assignments"
            assert v.column == "issue_number"
            assert v.value == "not-a-number"
            assert v.expected_affinity == "INTEGER"
            # Reported before any insert -- callers can print this directly.
            assert "assignments.issue_number" in str(v)
        finally:
            conn.close()

    def test_null_in_integer_column_is_not_a_violation(self, tmp_path: Path) -> None:
        conn = _open_source(tmp_path)
        try:
            # pr_number is a nullable INTEGER column with no DEFAULT -- left
            # unset here, so it's stored as NULL.
            conn.execute(
                "INSERT INTO merge_queue (assignment_id, repo_name, repo_github, "
                "branch, target_branch, issue_number, issue_title) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("a1", "r1", "org/r1", "issue-1", "main", 1, "t1"),
            )
            conn.commit()
            tables = store_migrate.discover_tables(conn)
            violations = store_migrate.audit_type_affinity(conn, tables)
            assert violations == []
        finally:
            conn.close()


class TestAuditReferentialIntegrity:
    def test_clean_fk_has_no_violations(self, tmp_path: Path) -> None:
        conn = _open_source(tmp_path)
        try:
            cur = conn.execute(
                "INSERT INTO split_proposals (repo_name, issue_number, issue_title) "
                "VALUES (?, ?, ?)",
                ("r1", 1, "t1"),
            )
            proposal_id = cur.lastrowid
            conn.execute(
                "INSERT INTO split_chunks (split_proposal_id, title, scope) "
                "VALUES (?, ?, ?)",
                (proposal_id, "chunk", "scope"),
            )
            conn.commit()
            tables = store_migrate.discover_tables(conn)
            assert store_migrate.audit_referential_integrity(conn, tables) == []
        finally:
            conn.close()

    def test_detects_orphaned_fk_reference(self, tmp_path: Path) -> None:
        """SQLite only enforces FOREIGN KEY on a connection with
        ``PRAGMA foreign_keys=ON`` -- a row written by a connection that
        never set it (or with it explicitly off) can be orphaned today with
        SQLite never having complained. Postgres enforces it unconditionally,
        so this must be caught before import, not discovered as an insert
        failure."""
        conn = _open_source(tmp_path)
        try:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute(
                "INSERT INTO split_chunks (split_proposal_id, title, scope) "
                "VALUES (?, ?, ?)",
                (999999, "orphan", "scope"),
            )
            conn.commit()
            tables = store_migrate.discover_tables(conn)
            violations = store_migrate.audit_referential_integrity(conn, tables)
            assert len(violations) == 1
            v = violations[0]
            assert v.table == "split_chunks"
            assert v.column == "split_proposal_id"
            assert v.ref_table == "split_proposals"
            assert v.orphan_count == 1
        finally:
            conn.close()


class TestRunImport:
    def _seed_source(self, tmp_path: Path) -> Path:
        conn = _open_source(tmp_path)
        try:
            conn.execute(
                "INSERT INTO machines (name, host) VALUES (?, ?)", ("m1", "m1.local")
            )
            conn.execute(
                "INSERT INTO issues (repo_name, number, title, state) VALUES (?, ?, ?, ?)",
                ("r1", 1, "t1", "open"),
            )
            conn.execute(
                "INSERT INTO issues (repo_name, number, title, state) VALUES (?, ?, ?, ?)",
                ("r1", 2, "t2", "closed"),
            )
            conn.commit()
        finally:
            conn.close()
        return tmp_path / "source.db"

    def test_row_count_parity_reported_for_every_table(self, tmp_path: Path) -> None:
        source_path = self._seed_source(tmp_path)
        with scratch_database(tmp_path, "target.db") as target_conn:
            report = store_migrate.run_import(
                sqlite_path=source_path,
                target_connector=lambda: target_conn,
            )
            assert not report.dry_run
            assert report.ok
            by_table = {t.table: t for t in report.tables}
            assert by_table["machines"].source_rows == 1
            assert by_table["machines"].target_rows == 1
            assert by_table["issues"].source_rows == 2
            assert by_table["issues"].target_rows == 2
            # Confirm the rows are actually queryable on the target, not
            # just counted.
            rows = sql.execute(target_conn, "SELECT name FROM machines").fetchall()
            assert [dict(r)["name"] for r in rows] == ["m1"]

    def test_dry_run_never_opens_the_target(self, tmp_path: Path) -> None:
        source_path = self._seed_source(tmp_path)

        def _boom():
            raise AssertionError("target must not be opened during a dry run")

        report = store_migrate.run_import(
            sqlite_path=source_path, dry_run=True, target_connector=_boom
        )
        assert report.dry_run
        by_table = {t.table: t for t in report.tables}
        assert by_table["machines"].source_rows == 1
        assert by_table["issues"].source_rows == 2

    def test_aborts_before_opening_target_on_type_affinity_violation(
        self, tmp_path: Path
    ) -> None:
        conn = _open_source(tmp_path)
        try:
            conn.execute(
                "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
                "issue_number, issue_title) VALUES (?, ?, ?, ?, ?)",
                ("a1", "m1", "r1", "bad", "t1"),
            )
            conn.commit()
        finally:
            conn.close()

        def _boom():
            raise AssertionError("target must not be opened when a pre-flight audit fails")

        with pytest.raises(store_migrate.ImportAborted, match="type-affinity"):
            store_migrate.run_import(
                sqlite_path=tmp_path / "source.db", target_connector=_boom
            )

    def test_rerun_against_populated_target_refuses_by_default(
        self, tmp_path: Path
    ) -> None:
        source_path = self._seed_source(tmp_path)
        with scratch_database(tmp_path, "target.db") as target_conn:
            store_migrate.run_import(sqlite_path=source_path, target_connector=lambda: target_conn)

            with pytest.raises(store_migrate.ImportAborted, match="already has rows"):
                store_migrate.run_import(
                    sqlite_path=source_path, target_connector=lambda: target_conn
                )

    def test_rerun_with_force_is_idempotent(self, tmp_path: Path) -> None:
        source_path = self._seed_source(tmp_path)
        with scratch_database(tmp_path, "target.db") as target_conn:
            store_migrate.run_import(sqlite_path=source_path, target_connector=lambda: target_conn)

            report = store_migrate.run_import(
                sqlite_path=source_path, force=True, target_connector=lambda: target_conn
            )
            assert report.ok
            by_table = {t.table: t for t in report.tables}
            assert by_table["machines"].target_rows == 1
            assert by_table["issues"].target_rows == 2

    def test_creates_dynamic_archive_mirror_on_target(self, tmp_path: Path) -> None:
        conn = _open_source(tmp_path)
        try:
            sql.execute(
                conn,
                'CREATE TABLE assignments_archive ("assignment_id" TEXT, "status" TEXT)',
            )
            conn.execute(
                'INSERT INTO assignments_archive ("assignment_id", "status") VALUES (?, ?)',
                ("old-1", "merged"),
            )
            conn.commit()
        finally:
            conn.close()

        with scratch_database(tmp_path, "target.db") as target_conn:
            assert sql.table_columns(target_conn, "assignments_archive") == []
            report = store_migrate.run_import(
                sqlite_path=tmp_path / "source.db", target_connector=lambda: target_conn
            )
            by_table = {t.table: t for t in report.tables}
            assert by_table["assignments_archive"].source_rows == 1
            assert by_table["assignments_archive"].target_rows == 1
            assert sql.table_columns(target_conn, "assignments_archive") != []

    def test_source_not_found_raises_import_aborted(self, tmp_path: Path) -> None:
        with pytest.raises(store_migrate.ImportAborted, match="not found"):
            store_migrate.run_import(sqlite_path=tmp_path / "nope.db")
