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
from click.testing import CliRunner

from coord import db as coord_db
from coord import sql
from coord import store_migrate
from coord.commands.store_migrate import migrate_to_postgres
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


class TestTopologicalImportOrder:
    def test_no_edges_preserves_alphabetical_order(self) -> None:
        tables = ["b", "a", "c"]
        assert store_migrate._topological_import_order(tables, []) == ["a", "b", "c"]

    def test_referenced_table_sorts_before_referencing_table(self) -> None:
        """The exact regression this exists for: split_chunks sorts before
        split_proposals alphabetically, but split_chunks.split_proposal_id
        references split_proposals.id, so split_proposals must import
        first."""
        tables = ["split_chunks", "split_proposals"]
        edges = [("split_chunks", "split_proposal_id", "split_proposals", "id")]
        assert store_migrate._topological_import_order(tables, edges) == [
            "split_proposals",
            "split_chunks",
        ]

    def test_unrelated_tables_keep_split_proposals_before_split_chunks(self) -> None:
        """Kahn's algorithm places every table with no *unplaced* dependency
        in one alphabetically-sorted batch per round, so a table with zero
        dependencies (``zzz_last``) can land in an earlier round than
        ``split_chunks`` even though ``zzz_last`` sorts after it -- what
        matters for #828's fix is only that ``split_proposals`` precedes
        ``split_chunks``, which every table in *tables* is free to interleave
        around."""
        tables = ["assignments", "split_chunks", "split_proposals", "zzz_last"]
        edges = [("split_chunks", "split_proposal_id", "split_proposals", "id")]
        order = store_migrate._topological_import_order(tables, edges)
        assert set(order) == set(tables)
        assert order.index("split_proposals") < order.index("split_chunks")

    def test_edge_referencing_table_outside_the_import_set_is_ignored(self) -> None:
        tables = ["split_chunks"]
        edges = [("split_chunks", "split_proposal_id", "split_proposals", "id")]
        assert store_migrate._topological_import_order(tables, edges) == ["split_chunks"]


class TestFormatViolations:
    def _violations(self, n: int) -> list[store_migrate.TypeAffinityViolation]:
        return [
            store_migrate.TypeAffinityViolation("t", "c", i, "x", "INTEGER")
            for i in range(n)
        ]

    def test_under_the_cap_lists_every_violation_with_no_truncation_note(self) -> None:
        text = store_migrate._format_violations(self._violations(3))
        lines = text.splitlines()
        assert len(lines) == 3
        assert "more" not in text

    def test_over_the_cap_truncates_and_reports_remaining_count(self) -> None:
        violations = self._violations(store_migrate._MAX_LISTED_VIOLATIONS + 5)
        text = store_migrate._format_violations(violations)
        lines = text.splitlines()
        assert len(lines) == store_migrate._MAX_LISTED_VIOLATIONS + 1
        assert lines[-1] == "  ... and 5 more"


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

    def test_import_respects_fk_dependency_order_for_split_workflow(
        self, tmp_path: Path
    ) -> None:
        """Regression for the exact ordering bug: discover_tables' plain
        alphabetical order sorts split_chunks before split_proposals, but
        split_chunks.split_proposal_id references split_proposals.id. A
        target that enforces that FK (Postgres always does; SQLite only
        when a connection turned PRAGMA foreign_keys on) must therefore see
        split_proposals' rows land first, or the import fails mid-way.

        tests/backends.py's scratch_database deliberately leaves FK
        enforcement off for its SQLite branch (so as not to change default
        suite behavior) -- turned on explicitly here so this test actually
        exercises the ordering fix rather than passing vacuously.
        """
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
        finally:
            conn.close()

        with scratch_database(tmp_path, "target.db") as target_conn:
            sql.execute(target_conn, "PRAGMA foreign_keys=ON")
            report = store_migrate.run_import(
                sqlite_path=tmp_path / "source.db", target_connector=lambda: target_conn
            )
            assert report.ok
            by_table = {t.table: t for t in report.tables}
            assert by_table["split_proposals"].target_rows == 1
            assert by_table["split_chunks"].target_rows == 1

    def _seed_split_workflow_source(self, tmp_path: Path) -> Path:
        conn = _open_source(tmp_path)
        try:
            cur = conn.execute(
                "INSERT INTO split_proposals (repo_name, issue_number, issue_title) "
                "VALUES (?, ?, ?)",
                ("r1", 1, "t1"),
            )
            proposal_id = cur.lastrowid
            conn.execute(
                "INSERT INTO split_chunks (split_proposal_id, title, scope) VALUES (?, ?, ?)",
                (proposal_id, "chunk", "scope"),
            )
            conn.execute(
                "INSERT INTO machines (name, host) VALUES (?, ?)", ("m1", "m1.local")
            )
            conn.commit()
        finally:
            conn.close()
        return tmp_path / "source.db"

    def test_force_rerun_wipes_children_before_parents(self, tmp_path: Path) -> None:
        """Regression for the *delete* half of the FK-ordering bug.

        ``tables`` is in parent-first order so the INSERTs are FK-safe, but
        that order is backwards for ``force``'s wipe: a single forward pass
        that deleted and re-inserted each table in turn would ``DELETE FROM
        split_proposals`` while split_chunks' *old* rows (wiped only on a
        later iteration) still reference them --
        ``split_chunks.split_proposal_id`` is NOT NULL REFERENCES
        split_proposals(id) with no ON DELETE CASCADE and not DEFERRABLE, so
        an enforcing target rejects that DELETE outright.

        This is the scenario --force exists for: a target that *already
        holds* FK-linked rows from a prior successful import. FK enforcement
        is turned on explicitly (scratch_database's SQLite branch leaves it
        off) so the test fails loudly on a regression rather than vacuously
        passing.
        """
        source_path = self._seed_split_workflow_source(tmp_path)
        with scratch_database(tmp_path, "target.db") as target_conn:
            sql.execute(target_conn, "PRAGMA foreign_keys=ON")
            store_migrate.run_import(sqlite_path=source_path, target_connector=lambda: target_conn)

            # The target now holds FK-linked rows -- exactly the state the
            # per-table delete-then-insert pass blew up on.
            report = store_migrate.run_import(
                sqlite_path=source_path, force=True, target_connector=lambda: target_conn
            )
            assert report.ok
            by_table = {t.table: t for t in report.tables}
            assert by_table["split_proposals"].target_rows == 1
            assert by_table["split_chunks"].target_rows == 1
            assert by_table["machines"].target_rows == 1
            # And the child rows still point at a live parent after the wipe.
            rows = sql.execute(
                target_conn,
                "SELECT c.id FROM split_chunks c "
                "JOIN split_proposals p ON p.id = c.split_proposal_id",
            ).fetchall()
            assert len(rows) == 1


class TestMigrateToPostgresCli:
    """``coord migrate-to-postgres`` -- the CLI wrapper around ``run_import``.

    ``--dry-run`` is used throughout: it never opens the target (see
    ``run_import``'s docstring), so these tests need no reachable Postgres
    server or installed driver, matching the rest of this file's default
    (SQLite-only) posture -- while still exercising the exact code path
    (the ``--dsn`` echo) the DSN-leak finding was about, since that line
    prints unconditionally whenever a --dsn was given, dry run or not.
    """

    def _seed_source(self, tmp_path: Path) -> Path:
        conn = coord_db._open(tmp_path / "source.db")
        conn.close()
        return tmp_path / "source.db"

    def test_dsn_uri_form_is_redacted_not_echoed_raw(self, tmp_path: Path) -> None:
        source_path = self._seed_source(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            migrate_to_postgres,
            [
                "--source",
                str(source_path),
                "--dsn",
                "postgresql://coorduser:s3cr3tpw@dbhost.example:5432/coord",
                "--dry-run",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "s3cr3tpw" not in result.output
        assert "coorduser" not in result.output
        assert "dbhost.example" in result.output

    def test_dsn_keyword_value_form_is_redacted_not_echoed_raw(
        self, tmp_path: Path
    ) -> None:
        source_path = self._seed_source(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            migrate_to_postgres,
            [
                "--source",
                str(source_path),
                "--dsn",
                "host=dbhost.example port=5432 dbname=coord user=coorduser "
                "password=s3cr3tpw",
                "--dry-run",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "s3cr3tpw" not in result.output
        assert "coorduser" not in result.output
        assert "dbhost.example" in result.output

    def test_dry_run_reports_source_row_counts(self, tmp_path: Path) -> None:
        conn = coord_db._open(tmp_path / "source.db")
        try:
            conn.execute(
                "INSERT INTO machines (name, host) VALUES (?, ?)", ("m1", "m1.local")
            )
            conn.commit()
        finally:
            conn.close()

        runner = CliRunner()
        result = runner.invoke(
            migrate_to_postgres,
            [
                "--source",
                str(tmp_path / "source.db"),
                "--dsn",
                "postgresql://u:p@host/db",
                "--dry-run",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "machines" in result.output
        assert "Audits passed" in result.output

    def test_type_affinity_violation_aborts_with_click_exception(
        self, tmp_path: Path
    ) -> None:
        conn = coord_db._open(tmp_path / "source.db")
        try:
            conn.execute(
                "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
                "issue_number, issue_title) VALUES (?, ?, ?, ?, ?)",
                ("a1", "m1", "r1", "not-a-number", "t1"),
            )
            conn.commit()
        finally:
            conn.close()

        runner = CliRunner()
        result = runner.invoke(
            migrate_to_postgres,
            [
                "--source",
                str(tmp_path / "source.db"),
                "--dsn",
                "postgresql://u:p@host/db",
                "--dry-run",
            ],
        )
        assert result.exit_code != 0
        assert "type-affinity" in result.output
