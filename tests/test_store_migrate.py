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

#3086 adds the rehearsal and content-verification modes to that. Two things
worth knowing before reading the bottom half of this file:

- The **default** scratch factory
  (``store_migrate.postgres_scratch_schema``) calls
  ``coord.db.refuse_postgres_under_pytest``, so no test can ever reach a live
  server through it -- deliberately, and
  ``TestRunRehearsal::test_default_factory_refuses_to_touch_a_real_server_under_pytest``
  asserts exactly that. Tests inject a ``ScratchTarget`` over
  ``tests/backends.py``'s ``scratch_database`` instead, which is how the
  rehearsal *orchestration* still gets exercised against a real Postgres
  schema when the ``postgres`` job runs (and against a second SQLite file
  everywhere else).
- ``TestVerificationOracleCanFail`` is the #2096 half: an oracle that has
  never failed is not an oracle, so each test there injects a real divergence
  into the import path and asserts the verification catches it **and names the
  right table and column**.
"""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path
from typing import Any, Iterator

import pytest
from click.testing import CliRunner

from coord import db as coord_db
from coord import sql
from coord import store_migrate, store_parity
from coord.commands.store_migrate import migrate_to_postgres
from tests.backends import BACKEND_POSTGRES, active_backend, scratch_database


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
            # #3083: `PRAGMA foreign_keys=ON` is SQLite-only — a hard
            # syntax error against psycopg, and unnecessary there (Postgres
            # always enforces). sql.enable_foreign_keys() is the seam's name
            # for the intent, and a documented no-op on Postgres.
            sql.enable_foreign_keys(target_conn)
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
            # #3083: `PRAGMA foreign_keys=ON` is SQLite-only — a hard
            # syntax error against psycopg, and unnecessary there (Postgres
            # always enforces). sql.enable_foreign_keys() is the seam's name
            # for the intent, and a documented no-op on Postgres.
            sql.enable_foreign_keys(target_conn)
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


# ── #3086: content verification, timing and the rehearsal ────────────────


def _seed_rich_source(tmp_path: Path, name: str = "source.db") -> Path:
    """A source with rows in a text-keyed table, an integer-surrogate-keyed
    table and a plain one -- enough shapes that ``compare_dumps``'s keyed and
    surrogate-key paths both get exercised by the verification tests."""
    conn = coord_db._open(tmp_path / name)
    try:
        conn.execute(
            "INSERT INTO machines (name, host, capabilities, repos) VALUES (?, ?, ?, ?)",
            ("precision", "precision.local", "gtk,browser", "coord"),
        )
        conn.execute(
            "INSERT INTO machines (name, host) VALUES (?, ?)", ("nuc", "nuc.local")
        )
        conn.execute(
            "INSERT INTO issues (repo_name, number, title, state) VALUES (?, ?, ?, ?)",
            ("coord", 3086, "rehearse the cutover", "open"),
        )
        for i in (1, 2, 3):
            conn.execute(
                "INSERT INTO merge_queue (assignment_id, repo_name, repo_github, "
                "branch, target_branch, issue_number, issue_title, state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (f"a{i}", "coord", "o/coord", f"issue-{i}", "main", i, f"t{i}", "queued"),
            )
        conn.commit()
    finally:
        conn.close()
    return tmp_path / name


@contextlib.contextmanager
def _scratch_target(tmp_path: Path, name: str = "rehearsal.db") -> Iterator[Any]:
    """A ``ScratchTarget`` over ``tests/backends.py``'s ``scratch_database``.

    This is the *scratch_factory* seam :func:`store_migrate.run_rehearsal`
    documents: the production factory
    (``store_migrate.postgres_scratch_schema``) calls
    ``coord.db.refuse_postgres_under_pytest`` and so can never be exercised
    from a test -- deliberately, and there is a test below asserting exactly
    that. Injecting here means the rehearsal *orchestration* still runs
    against a second SQLite file by default and against a real Postgres schema
    when ``COORD_TEST_BACKEND=postgres`` (CI's ``postgres`` job), which is what
    #3086's acceptance asks for.
    """
    with scratch_database(tmp_path, name) as conn:
        yield store_migrate.ScratchTarget(conn=conn, name=f"test scratch ({name})")


class TestVerifyImport:
    """``--verify``: content parity, the check row counts cannot make."""

    def test_clean_import_has_no_content_differences(self, tmp_path: Path) -> None:
        source_path = _seed_rich_source(tmp_path)
        with _scratch_target(tmp_path) as target:
            report = store_migrate.run_import(
                sqlite_path=source_path,
                verify=True,
                target_connector=lambda: target.conn,
            )
            assert report.row_counts_ok
            assert report.parity is not None
            assert report.parity.differences == (), report.parity.render()
            assert report.content_ok is True
            assert report.ok

    def test_excluded_bookkeeping_tables_are_not_reported_as_differences(
        self, tmp_path: Path
    ) -> None:
        """``schema_version``/``schema_version_new`` are deliberately never
        imported (the target re-derives its own on connect), so a verification
        that dumped whole databases would manufacture a difference on exactly
        the two tables the importer is *correct* to skip."""
        source_path = _seed_rich_source(tmp_path)
        with _scratch_target(tmp_path) as target:
            report = store_migrate.run_import(
                sqlite_path=source_path,
                verify=True,
                target_connector=lambda: target.conn,
            )
            assert report.parity is not None
            assert "schema_version" not in report.parity.tables_compared
            assert "schema_version_new" not in report.parity.tables_compared
            assert "machines" in report.parity.tables_compared

    def test_verify_is_off_by_default(self, tmp_path: Path) -> None:
        source_path = _seed_rich_source(tmp_path)
        with _scratch_target(tmp_path) as target:
            report = store_migrate.run_import(
                sqlite_path=source_path, target_connector=lambda: target.conn
            )
            assert report.parity is None
            assert report.content_ok is None
            assert report.ok  # row counts alone still gate the default path

    def test_dry_run_never_verifies(self, tmp_path: Path) -> None:
        source_path = _seed_rich_source(tmp_path)

        def _boom() -> Any:
            raise AssertionError("target must not be opened during a dry run")

        report = store_migrate.run_import(
            sqlite_path=source_path, dry_run=True, verify=True, target_connector=_boom
        )
        assert report.parity is None


class TestVerificationOracleCanFail:
    """#2096: an oracle that has never failed is not an oracle.

    Each test below injects a **real** divergence into the import path -- the
    same technique ``tests/test_write_parity.py`` uses when it reverts
    ``coord.state._UPSERT_SQL`` to its pre-#2726 ``INSERT OR REPLACE`` form --
    and asserts the migration verification catches it *and names the right
    table and column*. "It reported something" is not enough: #829 has to
    classify the entries, so the report has to be specific.
    """

    def _lossy_import_table(self, table: str, column: str) -> Any:
        """The real :func:`store_migrate.import_table`, then one column blanked
        on the target -- an importer that copies every row but loses a column's
        values. Row counts are **identical**, which is the whole point: this is
        precisely the failure row-count parity (#828's only check) cannot see.
        """
        real = store_migrate.import_table

        def _patched(source_conn: Any, target_conn: Any, name: str) -> Any:
            result = real(source_conn, target_conn, name)
            if name == table:
                sql.execute(target_conn, f'UPDATE "{table}" SET "{column}" = NULL')
                target_conn.commit()
            return result

        return _patched

    def test_lost_column_values_are_caught_and_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source_path = _seed_rich_source(tmp_path)
        monkeypatch.setattr(
            store_migrate,
            "import_table",
            self._lossy_import_table("machines", "capabilities"),
        )
        with _scratch_target(tmp_path) as target:
            report = store_migrate.run_import(
                sqlite_path=source_path,
                verify=True,
                target_connector=lambda: target.conn,
            )

        # Row counts still match -- the injected bug is invisible to them.
        assert report.row_counts_ok
        assert report.content_ok is False
        assert not report.ok

        assert report.parity is not None
        machines = report.parity.for_table("machines")
        assert machines, report.parity.render()
        cells = [d for d in machines if d.kind == store_parity.KIND_CELL]
        assert {d.column for d in cells} == {"capabilities"}
        offending = next(d for d in cells if d.value_a == "gtk,browser")
        assert offending.value_b is None
        # And it renders as text a human can act on, naming both sides.
        assert "machines" in report.parity.render()
        assert "capabilities" in report.parity.render()

    def test_dropped_row_is_caught_and_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An importer that silently loses a row. Row counts catch this one
        too -- the assertion is that content parity *also* does, and says
        which row, so the two gates agree instead of one hiding the other."""
        real = store_migrate.import_table

        def _patched(source_conn: Any, target_conn: Any, name: str) -> Any:
            result = real(source_conn, target_conn, name)
            if name == "machines":
                sql.execute(target_conn, "DELETE FROM machines WHERE name = 'nuc'")
                target_conn.commit()
            return result

        monkeypatch.setattr(store_migrate, "import_table", _patched)
        source_path = _seed_rich_source(tmp_path)
        with _scratch_target(tmp_path) as target:
            report = store_migrate.run_import(
                sqlite_path=source_path,
                verify=True,
                target_connector=lambda: target.conn,
            )

        assert report.content_ok is False
        assert report.parity is not None
        only_in_source = [
            d
            for d in report.parity.for_table("machines")
            if d.kind == store_parity.KIND_ROW_ONLY_IN_A
        ]
        assert len(only_in_source) == 1
        assert only_in_source[0].value_a["name"] == "nuc"

    def test_drifted_surrogate_ids_are_caught_not_normalised_away(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The check that pins ``rank_surrogate_keys=False``.

        #2885 normalises a single-column integer primary key to its rank,
        because two *independent* replays legitimately land on different
        sequence values. A migration is not that: the importer copies every id
        verbatim, so an id that moved is a real finding. With ranking left on,
        this divergence would compare clean -- which is exactly the assertion
        at the bottom.
        """
        real = store_migrate.import_table

        def _patched(source_conn: Any, target_conn: Any, name: str) -> Any:
            result = real(source_conn, target_conn, name)
            if name == "merge_queue":
                sql.execute(target_conn, "UPDATE merge_queue SET id = id + 100")
                target_conn.commit()
            return result

        monkeypatch.setattr(store_migrate, "import_table", _patched)
        source_path = _seed_rich_source(tmp_path)
        with _scratch_target(tmp_path) as target:
            report = store_migrate.run_import(
                sqlite_path=source_path,
                verify=True,
                target_connector=lambda: target.conn,
            )
            assert report.row_counts_ok
            assert report.content_ok is False
            assert "merge_queue" in report.parity.tables_with_differences()

            # ...and the same two databases DO compare clean under #2885's
            # rank normalisation, proving the migration check needs its own
            # setting rather than the replay harness's default.
            tables = [t.table for t in report.tables]
            source_conn = sql.connect(
                backend=sql.DIALECT_SQLITE, sqlite_path=source_path, read_only=True
            )
            try:
                sql.apply_row_factory(source_conn)
                ranked = store_parity.compare_dumps(
                    store_parity.dump_database(source_conn, label="a", tables=tables),
                    store_parity.dump_database(target.conn, label="b", tables=tables),
                )
            finally:
                source_conn.close()
            assert "merge_queue" not in ranked.tables_with_differences()


class TestImportTiming:
    """The rehearsal's primary deliverable: a number you can size an outage
    with. Without it the mode is decorative."""

    def test_per_table_and_total_elapsed_are_reported(self, tmp_path: Path) -> None:
        source_path = _seed_rich_source(tmp_path)
        with _scratch_target(tmp_path) as target:
            report = store_migrate.run_import(
                sqlite_path=source_path, target_connector=lambda: target.conn
            )
        assert report.tables
        assert all(t.elapsed_seconds >= 0.0 for t in report.tables)
        assert report.elapsed_seconds > 0.0
        # The total covers the audits and the verification too, not just the
        # sum of the per-table writes -- it is what the cutover actually costs.
        assert report.elapsed_seconds >= sum(t.elapsed_seconds for t in report.tables)

    def test_dry_run_is_timed_but_writes_nothing(self, tmp_path: Path) -> None:
        source_path = _seed_rich_source(tmp_path)
        report = store_migrate.run_import(
            sqlite_path=source_path,
            dry_run=True,
            target_connector=lambda: pytest.fail("dry run opened the target"),
        )
        assert report.elapsed_seconds > 0.0
        assert all(t.elapsed_seconds == 0.0 for t in report.tables)


class TestRunRehearsal:
    def test_clean_rehearsal_verifies_times_and_drops_the_target(
        self, tmp_path: Path
    ) -> None:
        source_path = _seed_rich_source(tmp_path)
        seen: list[store_migrate.ScratchTarget] = []

        @contextlib.contextmanager
        def _factory() -> Iterator[store_migrate.ScratchTarget]:
            with _scratch_target(tmp_path) as target:
                seen.append(target)
                yield target

        report = store_migrate.run_rehearsal(
            sqlite_path=source_path, scratch_factory=_factory
        )

        assert report.ok
        assert report.parity is not None and report.parity.is_clean
        assert report.elapsed_seconds > 0.0
        assert report.scratch_target
        # Nothing left behind on a clean run: the scratch target is named in
        # the report but was NOT marked for retention.
        assert report.retained_target == ""
        assert seen and seen[0].keep is False

    def test_failed_rehearsal_retains_and_names_the_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dropping the evidence on failure is the wrong default (#3086)."""
        real = store_migrate.import_table

        def _patched(source_conn: Any, target_conn: Any, name: str) -> Any:
            result = real(source_conn, target_conn, name)
            if name == "machines":
                sql.execute(target_conn, "UPDATE machines SET host = 'drifted'")
                target_conn.commit()
            return result

        monkeypatch.setattr(store_migrate, "import_table", _patched)
        source_path = _seed_rich_source(tmp_path)
        seen: list[store_migrate.ScratchTarget] = []

        @contextlib.contextmanager
        def _factory() -> Iterator[store_migrate.ScratchTarget]:
            with _scratch_target(tmp_path) as target:
                seen.append(target)
                yield target

        report = store_migrate.run_rehearsal(
            sqlite_path=source_path, scratch_factory=_factory
        )

        assert not report.ok
        assert report.retained_target == report.scratch_target != ""
        assert seen and seen[0].keep is True

    def test_aborted_rehearsal_carries_the_retained_target_on_the_exception(
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

        @contextlib.contextmanager
        def _factory() -> Iterator[store_migrate.ScratchTarget]:
            with _scratch_target(tmp_path) as target:
                yield target

        with pytest.raises(store_migrate.ImportAborted) as excinfo:
            store_migrate.run_rehearsal(
                sqlite_path=tmp_path / "source.db", scratch_factory=_factory
            )
        assert excinfo.value.retained_target

    def test_default_factory_refuses_to_touch_a_real_server_under_pytest(self) -> None:
        """``refuse_postgres_under_pytest`` is a correct guard and #3086 must
        not weaken it -- the rehearsal's default scratch factory calls it on
        its own account, so no test can ever reach a live server this way."""
        with pytest.raises(coord_db.ProductionDatabaseGuardError):
            with store_migrate.postgres_scratch_schema(
                "postgresql://u:p@nonexistent.invalid/coord"
            ):
                pytest.fail("the pytest guard did not fire")

    def test_the_guard_message_never_leaks_the_dsn(self) -> None:
        with pytest.raises(coord_db.ProductionDatabaseGuardError) as excinfo:
            with store_migrate.postgres_scratch_schema(
                "postgresql://coorduser:s3cr3tpw@dbhost.example:5432/coord"
            ):
                pytest.fail("the pytest guard did not fire")
        assert "s3cr3tpw" not in str(excinfo.value)
        assert "coorduser" not in str(excinfo.value)
        assert "dbhost.example" in str(excinfo.value)


class TestRehearsalCli:
    """``coord migrate-to-postgres --rehearse`` / ``--verify`` output and exit
    codes. ``run_rehearsal``/``run_import`` are stubbed where a real target
    would be needed: the guard above means a CLI test can never open one, and
    what is under test here is the rendering and the exit status."""

    def _report(
        self, *, parity: Any, retained: str = "", scratch: str = "host=db dbname=coord schema s1"
    ) -> store_migrate.ImportReport:
        report = store_migrate.ImportReport(dry_run=False)
        report.tables = [
            store_migrate.TableReport("machines", 2, 2, elapsed_seconds=0.5),
            store_migrate.TableReport("issues", 1, 1, elapsed_seconds=1.25),
        ]
        report.elapsed_seconds = 2.0
        report.parity = parity
        report.scratch_target = scratch
        report.retained_target = retained
        return report

    def _clean_parity(self) -> store_parity.ParityReport:
        return store_parity.ParityReport(
            label_a="source (sqlite)",
            label_b="imported target",
            differences=(),
            tables_compared=("issues", "machines"),
        )

    def _dirty_parity(self) -> store_parity.ParityReport:
        return store_parity.ParityReport(
            label_a="source (sqlite)",
            label_b="imported target",
            differences=(
                store_parity.Difference(
                    "machines",
                    store_parity.KIND_CELL,
                    key="'nuc'",
                    column="host",
                    value_a="nuc.local",
                    value_b=None,
                ),
            ),
            tables_compared=("issues", "machines"),
        )

    def test_rehearsal_prints_timing_parity_and_the_dropped_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "coord.commands.store_migrate.run_rehearsal",
            lambda **kwargs: self._report(parity=self._clean_parity()),
        )
        conn = coord_db._open(tmp_path / "source.db")
        conn.close()

        result = CliRunner().invoke(
            migrate_to_postgres,
            [
                "--source",
                str(tmp_path / "source.db"),
                "--dsn",
                "postgresql://coorduser:s3cr3tpw@dbhost.example:5432/coord",
                "--rehearse",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "2.00s elapsed" in result.output      # the outage-sizing number
        assert "1.25" in result.output               # the per-table breakdown
        assert "no differences" in result.output     # the parity report
        assert "Scratch target dropped" in result.output
        # The runbook facts #3086 documents rather than changes.
        assert "commits PER TABLE" in result.output
        assert "COORD_TEST_POSTGRES_DSN" in result.output
        assert "coord.db.latest" in result.output
        # ...and still no credential anywhere in the output.
        assert "s3cr3tpw" not in result.output
        assert "coorduser" not in result.output

    def test_failed_content_parity_exits_non_zero_and_names_the_residue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "coord.commands.store_migrate.run_rehearsal",
            lambda **kwargs: self._report(
                parity=self._dirty_parity(), retained="host=db dbname=coord schema s1"
            ),
        )
        conn = coord_db._open(tmp_path / "source.db")
        conn.close()

        result = CliRunner().invoke(
            migrate_to_postgres,
            [
                "--source",
                str(tmp_path / "source.db"),
                "--dsn",
                "postgresql://u:p@host/db",
                "--rehearse",
            ],
        )
        assert result.exit_code != 0
        assert "content parity FAILED" in result.output
        assert "machines" in result.output and "host" in result.output
        assert "RETAINED for inspection" in result.output
        assert "Scratch target dropped" not in result.output

    def test_verify_on_a_real_import_exits_non_zero_on_a_dirty_report(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--verify`` is available on the real (non-rehearsal) import too."""
        captured: dict[str, Any] = {}

        def _fake_run_import(**kwargs: Any) -> store_migrate.ImportReport:
            captured.update(kwargs)
            return self._report(parity=self._dirty_parity(), scratch="")

        monkeypatch.setattr("coord.commands.store_migrate.run_import", _fake_run_import)
        conn = coord_db._open(tmp_path / "source.db")
        conn.close()

        result = CliRunner().invoke(
            migrate_to_postgres,
            [
                "--source",
                str(tmp_path / "source.db"),
                "--dsn",
                "postgresql://u:p@host/db",
                "--verify",
            ],
        )
        assert captured["verify"] is True
        assert result.exit_code != 0
        assert "content parity FAILED" in result.output

    def test_import_without_verify_says_content_was_not_checked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "coord.commands.store_migrate.run_import",
            lambda **kwargs: self._report(parity=None, scratch=""),
        )
        conn = coord_db._open(tmp_path / "source.db")
        conn.close()

        result = CliRunner().invoke(
            migrate_to_postgres,
            [
                "--source",
                str(tmp_path / "source.db"),
                "--dsn",
                "postgresql://u:p@host/db",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Content NOT verified" in result.output

    def test_rehearse_and_dry_run_are_mutually_exclusive(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(
            migrate_to_postgres,
            ["--source", str(tmp_path / "nope.db"), "--rehearse", "--dry-run"],
        )
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output

    def test_rehearse_rejects_force(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(
            migrate_to_postgres,
            ["--source", str(tmp_path / "nope.db"), "--rehearse", "--force"],
        )
        assert result.exit_code != 0
        assert "meaningless with --rehearse" in result.output

    def test_dry_run_help_says_plainly_it_never_opens_the_target(self) -> None:
        """The two modes must not be confused: --dry-run's own help has to say
        it proves nothing about the target."""
        result = CliRunner().invoke(migrate_to_postgres, ["--help"])
        assert result.exit_code == 0
        text = " ".join(result.output.split())
        assert "SOURCE-ONLY" in text
        assert "without opening or writing the target" in text
        assert "use --rehearse" in text

    def test_dry_run_still_reports_source_counts_only(self, tmp_path: Path) -> None:
        """#3086 must not change --dry-run: it stays the cheap source audit."""
        source_path = _seed_rich_source(tmp_path)
        result = CliRunner().invoke(
            migrate_to_postgres,
            ["--source", str(source_path), "--dsn", "postgresql://u:p@host/db", "--dry-run"],
        )
        assert result.exit_code == 0, result.output
        assert "Audits passed" in result.output
        assert "no target opened, no rows written" in result.output
        assert "source rows" in result.output
        assert "Content NOT verified" not in result.output


@pytest.mark.skipif(
    active_backend() != BACKEND_POSTGRES,
    reason=(
        "rehearsal-against-real-Postgres check: needs COORD_TEST_BACKEND=postgres "
        "(CI's `postgres` job). Every other test in this file runs the same "
        "orchestration against a second SQLite file, so a laptop with no server "
        "still covers it -- this one exists to prove the target really was "
        "Postgres when the job that can prove it runs."
    ),
)
def test_rehearsal_runs_against_a_real_postgres_target(tmp_path: Path) -> None:
    """#3086 acceptance: the rehearsal orchestration is exercised against a
    **real** Postgres when CI's ``postgres`` job runs this file.

    The value the SQLite default cannot give: Postgres enforces every FK at
    insert time, rejects the type drift SQLite tolerates, and needs the
    identity-sequence resync :func:`store_migrate.import_table` does. A green
    row-count *and* a clean content report here is the first evidence that the
    cutover's write path actually works end to end.
    """
    source_path = _seed_rich_source(tmp_path)

    @contextlib.contextmanager
    def _factory() -> Iterator[store_migrate.ScratchTarget]:
        with _scratch_target(tmp_path) as target:
            assert sql.detect_dialect(target.conn) == sql.DIALECT_POSTGRES
            yield target

    report = store_migrate.run_rehearsal(sqlite_path=source_path, scratch_factory=_factory)
    assert report.ok, report.parity.render() if report.parity else "row counts"
    assert report.parity is not None and report.parity.is_clean
    assert report.elapsed_seconds > 0.0
    assert report.retained_target == ""
