"""Tests for coord.state — proposal persistence."""

from __future__ import annotations

import json
import sqlite3
import time
import warnings

import pytest

from coord import db as db_mod
from coord import sql
from coord import state
from coord.models import Proposal
from coord.state import (
    save_proposals,
    load_proposals,
    clear_proposals,
    record_dispatched,
    record_test_verdict,
    record_uat_verdict,
    update_assignment_claude_session_id,
)
from tests import backends
from tests.test_db import (
    AbortOnErrorConn,
    abort_simulating_connection,
    schema_migrated_sqlite_connection,
)


@pytest.fixture
def proposals() -> list[Proposal]:
    return [
        Proposal(
            id=1,
            machine_name="laptop",
            repo_name="api",
            issue_number=10,
            issue_title="Fix auth",
            rationale="best fit",
            files_likely=["auth.py"],
            briefing="Fix the auth module",
        ),
        Proposal(
            id=2,
            machine_name="server",
            repo_name="shared",
            issue_number=5,
            issue_title="Add logging",
            rationale="only option",
        ),
    ]


class TestStatePersistence:
    def test_save_and_load_roundtrip(self, coord_db, proposals: list[Proposal]) -> None:
        save_proposals(proposals)
        loaded = load_proposals()

        assert len(loaded) == 2
        assert loaded[0].id == 1
        assert loaded[0].machine_name == "laptop"
        assert loaded[0].files_likely == ["auth.py"]
        assert loaded[1].id == 2
        assert loaded[1].briefing == ""

    def test_load_empty_returns_empty(self, coord_db) -> None:
        assert load_proposals() == []

    def test_clear_removes_proposals(self, coord_db, proposals: list[Proposal]) -> None:
        save_proposals(proposals)
        assert len(load_proposals()) == 2
        clear_proposals()
        assert load_proposals() == []

    def test_clear_when_empty_is_noop(self, coord_db) -> None:
        clear_proposals()  # should not raise
        assert load_proposals() == []

    def test_save_replaces_previous(self, coord_db, proposals: list[Proposal]) -> None:
        save_proposals(proposals)
        save_proposals([proposals[0]])  # save only first
        loaded = load_proposals()
        assert len(loaded) == 1
        assert loaded[0].id == 1


class TestClaudeSessionId:
    """#315: claude_session_id column on the assignments table."""

    def test_schema_has_claude_session_id_column(self, coord_db) -> None:
        """The assignments table must have a claude_session_id column."""
        from coord.db import get_connection
        conn = get_connection()
        # #3083: PRAGMA has no Postgres equivalent — sql.table_columns is
        # the seam's portable spelling of "what columns does this table have".
        cols = {name for name, _type in sql.table_columns(conn, "assignments")}
        assert "claude_session_id" in cols, (
            "assignments table is missing claude_session_id column — "
            "check _migrate_add_columns in coord/db.py"
        )

    def test_update_assignment_claude_session_id(self, coord_db) -> None:
        """update_assignment_claude_session_id persists the value on the row."""
        # Insert a minimal assignment row using record_dispatched.
        proposal = Proposal(
            id=1,
            machine_name="laptop",
            repo_name="api",
            issue_number=42,
            issue_title="Chat test",
            rationale="test",
            briefing="hello",
            type="refinement",
        )
        assignment_id = "test-sess-001"
        record_dispatched(
            assignment_id=assignment_id,
            proposal=proposal,
            repo_github="acme/api",
        )

        # Starts as NULL.
        from coord.db import get_connection
        conn = get_connection()
        row = conn.execute(
            "SELECT claude_session_id FROM assignments WHERE assignment_id=?",
            (assignment_id,),
        ).fetchone()
        assert row is not None
        assert row[0] is None

        # Persist the session ID.
        update_assignment_claude_session_id(assignment_id, "ses-xyz-42")

        row = conn.execute(
            "SELECT claude_session_id FROM assignments WHERE assignment_id=?",
            (assignment_id,),
        ).fetchone()
        assert row[0] == "ses-xyz-42"

    def test_update_assignment_claude_session_id_noop_on_missing(self, coord_db) -> None:
        """Calling with a nonexistent assignment_id silently does nothing."""
        update_assignment_claude_session_id("no-such-id", "ses-123")  # must not raise

    def test_update_assignment_claude_session_id_noop_on_empty(self, coord_db) -> None:
        """Calling with empty strings silently does nothing."""
        update_assignment_claude_session_id("", "ses-123")  # must not raise
        update_assignment_claude_session_id("some-id", "")  # must not raise


class TestStopReason:
    """#2316: stop_reason column on the assignments table.

    The enabling persistence step — the agent already sent `stop_reason` on
    every terminal `/status` `completed` entry (see
    `coord.agent.AgentServer.list_assignments`), but the coordinator had
    nowhere to put it and dropped it on receipt. This is the "everything
    else is a read of it" column the issue's classification/comment work
    (coord/agent.py) build on.
    """

    def test_schema_has_stop_reason_column(self, coord_db) -> None:
        """The assignments table must have a stop_reason column."""
        from coord.db import get_connection
        conn = get_connection()
        # #3083: PRAGMA has no Postgres equivalent — sql.table_columns is
        # the seam's portable spelling of "what columns does this table have".
        cols = {name for name, _type in sql.table_columns(conn, "assignments")}
        assert "stop_reason" in cols, (
            "assignments table is missing stop_reason column — "
            "check _migrate_add_columns in coord/db.py"
        )

    def test_update_assignment_stop_reason_persists_the_value(self, coord_db) -> None:
        """update_assignment_stop_reason persists the value on the row."""
        from coord.state import update_assignment_stop_reason

        proposal = Proposal(
            id=1,
            machine_name="laptop",
            repo_name="api",
            issue_number=2316,
            issue_title="Truncation test",
            rationale="test",
            briefing="hello",
        )
        assignment_id = "test-stop-reason-001"
        record_dispatched(
            assignment_id=assignment_id,
            proposal=proposal,
            repo_github="acme/api",
        )

        from coord.db import get_connection
        conn = get_connection()
        row = conn.execute(
            "SELECT stop_reason FROM assignments WHERE assignment_id=?",
            (assignment_id,),
        ).fetchone()
        assert row is not None
        assert row[0] is None

        update_assignment_stop_reason(assignment_id, "length")

        row = conn.execute(
            "SELECT stop_reason FROM assignments WHERE assignment_id=?",
            (assignment_id,),
        ).fetchone()
        assert row[0] == "length"

    def test_update_assignment_stop_reason_first_writer_wins(self, coord_db) -> None:
        """Idempotent: a second call with a different value is a no-op — a
        terminal row's stop reason cannot change after the fact, so a later
        reconcile tick re-observing the same /status entry must not clobber it."""
        from coord.state import update_assignment_stop_reason

        proposal = Proposal(
            id=1,
            machine_name="laptop",
            repo_name="api",
            issue_number=2316,
            issue_title="Truncation test",
            rationale="test",
            briefing="hello",
        )
        assignment_id = "test-stop-reason-002"
        record_dispatched(
            assignment_id=assignment_id,
            proposal=proposal,
            repo_github="acme/api",
        )

        update_assignment_stop_reason(assignment_id, "end_turn")
        update_assignment_stop_reason(assignment_id, "length")  # must be ignored

        from coord.db import get_connection
        conn = get_connection()
        row = conn.execute(
            "SELECT stop_reason FROM assignments WHERE assignment_id=?",
            (assignment_id,),
        ).fetchone()
        assert row[0] == "end_turn"

    def test_update_assignment_stop_reason_noop_on_missing(self, coord_db) -> None:
        """Calling with a nonexistent assignment_id silently does nothing."""
        from coord.state import update_assignment_stop_reason

        update_assignment_stop_reason("no-such-id", "length")  # must not raise

    def test_update_assignment_stop_reason_noop_on_empty(self, coord_db) -> None:
        """Calling with empty assignment_id/stop_reason silently does nothing."""
        from coord.state import update_assignment_stop_reason

        update_assignment_stop_reason("", "length")  # must not raise
        update_assignment_stop_reason("some-id", "")  # must not raise


class TestDispatchedByAssignmentId:
    """#2417: dispatched_by_assignment_id — the calling worker's own
    assignment id, captured from $COORD_ASSIGNMENT_ID at dispatch time, so a
    sibling assignment a worker's own turn spawns (`coord acceptance
    author`, `coord fix <other-id>`) is traceable back to the origin row
    instead of only discoverable by grepping the raw worker transcript.
    """

    def test_schema_has_dispatched_by_assignment_id_column(self, coord_db) -> None:
        from coord.db import get_connection
        conn = get_connection()
        # #3083: PRAGMA has no Postgres equivalent — sql.table_columns is
        # the seam's portable spelling of "what columns does this table have".
        cols = {name for name, _type in sql.table_columns(conn, "assignments")}
        assert "dispatched_by_assignment_id" in cols, (
            "assignments table is missing dispatched_by_assignment_id column — "
            "check _migrate_add_columns in coord/db.py"
        )

    def test_record_dispatched_captures_env_when_set(self, coord_db, monkeypatch) -> None:
        """A Proposal-based dispatch (coord fix's `_dispatch_followup`, plain
        `coord assign`, ...) picks up the calling worker's own assignment id
        from $COORD_ASSIGNMENT_ID — i.e. this dispatch happened FROM INSIDE
        that worker's own turn, not typed by a human."""
        monkeypatch.setenv("COORD_ASSIGNMENT_ID", "origin-work-001")
        proposal = Proposal(
            id=1, machine_name="laptop", repo_name="api", issue_number=42,
            issue_title="Fix auth", rationale="x",
        )
        record_dispatched(
            assignment_id="sibling-001", proposal=proposal, repo_github="acme/api",
        )

        from coord.db import get_connection
        conn = get_connection()
        row = conn.execute(
            "SELECT dispatched_by_assignment_id FROM assignments WHERE assignment_id=?",
            ("sibling-001",),
        ).fetchone()
        assert row[0] == "origin-work-001"

    def test_record_dispatched_none_when_env_unset(self, coord_db, monkeypatch) -> None:
        """A human typing `coord assign`/`coord fix` in their own shell never
        has $COORD_ASSIGNMENT_ID set — the column must stay NULL, not read as
        a phantom dispatch-by-worker."""
        monkeypatch.delenv("COORD_ASSIGNMENT_ID", raising=False)
        proposal = Proposal(
            id=1, machine_name="laptop", repo_name="api", issue_number=42,
            issue_title="Fix auth", rationale="x",
        )
        record_dispatched(
            assignment_id="sibling-002", proposal=proposal, repo_github="acme/api",
        )

        from coord.db import get_connection
        conn = get_connection()
        row = conn.execute(
            "SELECT dispatched_by_assignment_id FROM assignments WHERE assignment_id=?",
            ("sibling-002",),
        ).fetchone()
        assert row[0] is None

    def test_record_dispatched_assignment_captures_env_when_set(
        self, coord_db, monkeypatch
    ) -> None:
        """Mirrors the Proposal-path test above for the Assignment-based
        dispatch path (`coord.test_author.dispatch_test_author`'s
        `record_dispatched_assignment` call — the exact path coord-portal#119
        went through: a work session shelling out to `coord acceptance
        author`)."""
        from coord.models import Assignment
        from coord.state import record_dispatched_assignment

        monkeypatch.setenv("COORD_ASSIGNMENT_ID", "b1b6f90ca426")
        record_dispatched_assignment(
            assignment=Assignment(
                assignment_id="41249c1cebbd", machine_name="precision",
                repo_name="coord-portal", issue_number=16,
                issue_title="[test-author] ms-16 slice #10", type="test-author",
            ),
            repo_github="acme/coord-portal",
        )

        from coord.db import get_connection
        conn = get_connection()
        row = conn.execute(
            "SELECT dispatched_by_assignment_id FROM assignments WHERE assignment_id=?",
            ("41249c1cebbd",),
        ).fetchone()
        assert row[0] == "b1b6f90ca426"

    def test_explicit_value_on_the_dataclass_wins_over_env(
        self, coord_db, monkeypatch
    ) -> None:
        """A caller that already set dispatched_by_assignment_id explicitly
        (none do today, but the seam supports it) is never clobbered by the
        env-derived default."""
        monkeypatch.setenv("COORD_ASSIGNMENT_ID", "some-other-worker")
        proposal = Proposal(
            id=1, machine_name="laptop", repo_name="api", issue_number=42,
            issue_title="Fix auth", rationale="x",
            dispatched_by_assignment_id="explicit-parent",
        )
        record_dispatched(
            assignment_id="sibling-003", proposal=proposal, repo_github="acme/api",
        )

        from coord.db import get_connection
        conn = get_connection()
        row = conn.execute(
            "SELECT dispatched_by_assignment_id FROM assignments WHERE assignment_id=?",
            ("sibling-003",),
        ).fetchone()
        assert row[0] == "explicit-parent"

    def test_audit_summary_names_the_dispatching_assignment(
        self, coord_db, monkeypatch
    ) -> None:
        """#2417's other half: `coord audit`'s default (non-JSON) table must
        show the link too, not just a --json details field — this is what
        let coord-portal#119's dispatch go unnoticed without grepping the raw
        transcript."""
        from coord.state import list_audit_log

        monkeypatch.setenv("COORD_ASSIGNMENT_ID", "origin-work-audit")
        proposal = Proposal(
            id=1, machine_name="laptop", repo_name="api", issue_number=42,
            issue_title="Fix auth", rationale="x",
        )
        record_dispatched(
            assignment_id="sibling-audit-001", proposal=proposal, repo_github="acme/api",
        )

        result = list_audit_log(assignment_id="sibling-audit-001")
        entries = result["entries"]
        assert len(entries) == 1
        assert "dispatched by assignment origin-work-audit" in entries[0]["summary"]
        assert entries[0]["details"]["dispatched_by_assignment_id"] == "origin-work-audit"

    def test_load_dispatched_surfaces_the_field(self, coord_db, monkeypatch) -> None:
        """The board-level reverse-lookup surface (`coord.state.load_dispatched`,
        which both `coord sessions`/`coord log` and the TUI's board payload
        read from) must carry the field so a consumer can find "which
        assignment dispatched me" without a raw-log grep."""
        from coord.state import load_dispatched

        monkeypatch.setenv("COORD_ASSIGNMENT_ID", "origin-work-load")
        proposal = Proposal(
            id=1, machine_name="laptop", repo_name="api", issue_number=42,
            issue_title="Fix auth", rationale="x",
        )
        record_dispatched(
            assignment_id="sibling-load-001", proposal=proposal, repo_github="acme/api",
        )

        rows = {r["assignment_id"]: r for r in load_dispatched()}
        assert rows["sibling-load-001"]["dispatched_by_assignment_id"] == "origin-work-load"


class TestRecordDispatchedAssignmentLockContention:
    """#2538: `_record_dispatched_assignment_local`'s INSERT is the one
    write in that function that's actually load-bearing (unlike the
    best-effort `_record_audit` call alongside it) — a concurrent writer
    (the daemon's own passive tick, another `coord merge`/`coord notify`
    invocation) holding the DB for a moment must not be fatal. It now
    retries transient `database is locked` collisions via
    `coord.db.retry_on_locked` before giving up.
    """

    class _FlakyConn:
        """Wraps a real (in-memory) connection, raising `database is
        locked` on the first *fail_times* `execute()` calls before
        delegating to the real one — simulates a concurrent writer holding
        the DB for a few moments.

        #2726: `_record_dispatched_assignment_local`'s INSERT now goes
        through `coord.sql.execute()`, which calls `conn.cursor()` then
        `cursor.execute()` rather than the sqlite3 connection-level
        `.execute()` shortcut — so `cursor()` must be implemented too, not
        just `execute()`. `__module__` is pinned to `"sqlite3"` so
        `coord.sql.detect_dialect` (keyed off `type(conn).__module__`)
        recognizes this fake as SQLite instead of raising
        `UnsupportedDialectError`.
        """

        __module__ = "sqlite3"

        def __init__(self, real_conn, fail_times: int) -> None:
            self._real = real_conn
            self._fail_times = fail_times
            self.calls = 0

        def cursor(self):
            return self

        def execute(self, *args, **kwargs):
            self.calls += 1
            if self.calls <= self._fail_times:
                import sqlite3

                raise sqlite3.OperationalError("database is locked")
            return self._real.execute(*args, **kwargs)

        def commit(self):
            return self._real.commit()

    def test_retries_through_transient_lock_then_persists(
        self, coord_db, monkeypatch
    ) -> None:
        from coord.models import Assignment
        from coord.state import record_dispatched_assignment

        monkeypatch.setattr("coord.db.time.sleep", lambda s: None)
        flaky = self._FlakyConn(coord_db, fail_times=2)
        monkeypatch.setattr("coord.state.get_connection", lambda: flaky)

        record_dispatched_assignment(
            assignment=Assignment(
                assignment_id="ci-fix-2538", machine_name="laptop",
                repo_name="api", issue_number=9, issue_title="[fix-1] thing",
                type="work",
            ),
            repo_github="acme/api",
        )

        assert flaky.calls == 3  # two collisions, then the write that lands
        row = coord_db.execute(
            "SELECT status FROM assignments WHERE assignment_id=?",
            ("ci-fix-2538",),
        ).fetchone()
        assert row is not None, "assignment must be durably recorded once the lock clears"
        assert row["status"] == "running"

    def test_raises_once_the_retry_budget_is_exhausted(
        self, coord_db, monkeypatch
    ) -> None:
        """A lock that never clears within the bounded retry budget must
        still surface to the caller — `coord.auto_loop._dispatch_fix` is
        the layer responsible for turning that into a graceful "declined"
        rather than a crash, not this one silently swallowing it."""
        import sqlite3

        from coord.models import Assignment
        from coord.state import record_dispatched_assignment

        monkeypatch.setattr("coord.db.time.sleep", lambda s: None)
        flaky = self._FlakyConn(coord_db, fail_times=999)
        monkeypatch.setattr("coord.state.get_connection", lambda: flaky)

        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            record_dispatched_assignment(
                assignment=Assignment(
                    assignment_id="ci-fix-2538-b", machine_name="laptop",
                    repo_name="api", issue_number=9, issue_title="[fix-1] thing",
                    type="work",
                ),
                repo_github="acme/api",
            )


class TestRecordTestVerdictLockContention:
    """#2802: `_record_test_verdict_local`'s UPDATE(s) must ride out
    transient `database is locked` collisions the same way every
    neighbouring write in this module does (#2597/#2538) — the bug report
    was an assignment stranded at `test_state='running'` forever because
    the verdict write lost a lock race and raised straight through the
    caller with nothing left to retry it, since the caller has already run
    the test and has no reason to call back in.
    """

    class _FlakyConn:
        """Like `TestRecordDispatchedAssignmentLockContention._FlakyConn`,
        but also delegates `fetchone`/`fetchall`/etc. to the most recent
        real cursor: `_record_test_verdict_local` issues a `SELECT` right
        after its writes (to resolve the staleness anchor / audit-log
        fields), and `coord.sql.execute` returns the object `.cursor()`
        produced — this fake, standing in for the connection — as if it
        were that cursor, so it must forward the fetch methods too."""

        __module__ = "sqlite3"

        def __init__(self, real_conn, fail_times: int) -> None:
            self._real = real_conn
            self._fail_times = fail_times
            self.calls = 0
            self._last_cursor = None

        def cursor(self):
            return self

        def execute(self, *args, **kwargs):
            self.calls += 1
            if self.calls <= self._fail_times:
                import sqlite3

                raise sqlite3.OperationalError("database is locked")
            self._last_cursor = self._real.execute(*args, **kwargs)
            return self._last_cursor

        def commit(self):
            return self._real.commit()

        def __getattr__(self, name):
            if self._last_cursor is not None:
                return getattr(self._last_cursor, name)
            raise AttributeError(name)

    @staticmethod
    def _seed_assignment(coord_db, *, assignment_id="aid-lock") -> None:
        coord_db.execute(
            "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
            "issue_number, issue_title, test_state) VALUES (?, 'm1', 'api', 1, 't', 'running')",
            (assignment_id,),
        )
        coord_db.commit()

    def test_retries_through_transient_lock_then_persists(
        self, coord_db, monkeypatch
    ) -> None:
        self._seed_assignment(coord_db)
        monkeypatch.setattr("coord.db.time.sleep", lambda s: None)
        flaky = self._FlakyConn(coord_db, fail_times=2)
        monkeypatch.setattr("coord.state.get_connection", lambda: flaky)

        record_test_verdict(assignment_id="aid-lock", test_state="passed")

        # Two collisions absorbed before the write lands — `_write` issues
        # more than one statement (test_state + the smoke_test mirror), so
        # this checks "retried at all" rather than pinning an exact count
        # that would drift with unrelated statements added to the writer.
        assert flaky.calls >= 3
        row = coord_db.execute(
            "SELECT test_state, smoke_test FROM assignments WHERE assignment_id=?",
            ("aid-lock",),
        ).fetchone()
        assert row is not None
        assert row["test_state"] == "passed", (
            "a verdict must land once the lock clears — never stranded at "
            "'running' by a lock collision"
        )
        assert row["smoke_test"] == "pass"

    def test_raises_once_the_retry_budget_is_exhausted(
        self, coord_db, monkeypatch
    ) -> None:
        """A lock that never clears must still surface to the caller
        (`notify.py`'s drain, which already knows how to log and move on)
        rather than being silently swallowed here — swallowing it would
        make the row look successfully updated when it wasn't."""
        self._seed_assignment(coord_db, assignment_id="aid-lock-b")
        monkeypatch.setattr("coord.db.time.sleep", lambda s: None)
        flaky = self._FlakyConn(coord_db, fail_times=999)
        monkeypatch.setattr("coord.state.get_connection", lambda: flaky)

        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            record_test_verdict(assignment_id="aid-lock-b", test_state="passed")

        row = coord_db.execute(
            "SELECT test_state FROM assignments WHERE assignment_id=?",
            ("aid-lock-b",),
        ).fetchone()
        assert row is not None
        assert row["test_state"] == "running", (
            "the exhausted-retry row must be left exactly where the existing "
            "stuck-test_state='running' self-heal (coord/diagnose.py, "
            "coord/merge_queue.py's staleness window) already knows to find it"
        )


class TestRecordDispatchedAssignmentBranch:
    """#557: record_dispatched_assignment must persist the branch column so
    coord reattach can find it for the remote push-back finalize."""

    def test_branch_persisted_when_set(self, coord_db) -> None:
        """A fix/rework assignment created with branch=<name> must have that
        branch written to the DB row, not left as NULL."""
        from coord.models import Assignment
        from coord.state import record_dispatched_assignment, get_connection

        assignment = Assignment(
            machine_name="precision",
            repo_name="myrepo",
            issue_number=514,
            issue_title="[fix-1] migrate terminal",
            assignment_id="971a1947ad91",
            status="running",
            branch="issue-514-migrate-terminal-onto-quadraui",
            type="work",
            provider_name="claude-pty",
            dispatched_at=0.0,
        )
        record_dispatched_assignment(
            assignment=assignment,
            repo_github="acme/myrepo",
        )

        conn = get_connection()
        row = conn.execute(
            "SELECT branch FROM assignments WHERE assignment_id=?",
            ("971a1947ad91",),
        ).fetchone()
        assert row is not None
        assert row[0] == "issue-514-migrate-terminal-onto-quadraui", (
            "record_dispatched_assignment must persist assignment.branch to the DB"
        )

    def test_branch_none_when_not_set(self, coord_db) -> None:
        """A review assignment (branch=None) must leave the DB branch as NULL."""
        from coord.models import Assignment
        from coord.state import record_dispatched_assignment, get_connection

        assignment = Assignment(
            machine_name="precision",
            repo_name="myrepo",
            issue_number=514,
            issue_title="[review] migrate terminal",
            assignment_id="6873d9f346d0",
            status="running",
            branch=None,
            type="review",
            provider_name="claude-pty",
            dispatched_at=0.0,
        )
        record_dispatched_assignment(
            assignment=assignment,
            repo_github="acme/myrepo",
        )

        conn = get_connection()
        row = conn.execute(
            "SELECT branch FROM assignments WHERE assignment_id=?",
            ("6873d9f346d0",),
        ).fetchone()
        assert row is not None
        assert row[0] is None

    def test_redispatch_does_not_clear_existing_branch(self, coord_db) -> None:
        """ON CONFLICT: re-dispatching with branch=None must not overwrite a
        branch that was already recorded (COALESCE guard)."""
        from coord.models import Assignment
        from coord.state import record_dispatched_assignment, get_connection

        # First dispatch — with a branch.
        assignment_v1 = Assignment(
            machine_name="precision",
            repo_name="myrepo",
            issue_number=1,
            issue_title="First dispatch",
            assignment_id="abc123",
            status="running",
            branch="issue-1-some-branch",
            type="work",
            dispatched_at=0.0,
        )
        record_dispatched_assignment(assignment=assignment_v1, repo_github="acme/myrepo")

        # Re-dispatch without a branch (e.g. a retry that doesn't know the branch).
        assignment_v2 = Assignment(
            machine_name="precision",
            repo_name="myrepo",
            issue_number=1,
            issue_title="Re-dispatch",
            assignment_id="abc123",
            status="running",
            branch=None,
            type="work",
            dispatched_at=1.0,
        )
        record_dispatched_assignment(assignment=assignment_v2, repo_github="acme/myrepo")

        conn = get_connection()
        row = conn.execute(
            "SELECT branch FROM assignments WHERE assignment_id=?",
            ("abc123",),
        ).fetchone()
        assert row is not None
        assert row[0] == "issue-1-some-branch", (
            "COALESCE must prevent a branch-less re-dispatch from clearing the existing branch"
        )


class TestReconcileBoardWriteHelpers:
    """#611/#609: targeted, idempotent UPDATE helpers used by the
    reconcile-merges sweep."""

    def _insert_done_work(
        self,
        *,
        assignment_id: str,
        branch: str | None,
        status: str = "done",
        review_state: str | None = None,
        assignment_type: str = "work",
    ) -> None:
        from coord.db import get_connection
        from coord.models import Assignment
        from coord.state import record_dispatched_assignment

        assignment = Assignment(
            machine_name="laptop",
            repo_name="myrepo",
            issue_number=42,
            issue_title="t",
            assignment_id=assignment_id,
            status=status,
            branch=branch,
            type=assignment_type,
            dispatched_at=0.0,
        )
        record_dispatched_assignment(
            assignment=assignment, repo_github="acme/myrepo"
        )
        # record_dispatched_assignment always inserts status='running' (it
        # mirrors a fresh dispatch); set the desired terminal status directly.
        conn = get_connection()
        conn.execute(
            "UPDATE assignments SET status=? WHERE assignment_id=?",
            (status, assignment_id),
        )
        if review_state is not None:
            conn.execute(
                "UPDATE assignments SET review_state=? WHERE assignment_id=?",
                (review_state, assignment_id),
            )
        conn.commit()

    def test_update_assignment_branch_backfills_when_empty(self, coord_db) -> None:
        from coord.db import get_connection
        from coord.state import update_assignment_branch

        self._insert_done_work(assignment_id="bf1", branch=None)
        update_assignment_branch("bf1", "issue-42-fix")

        conn = get_connection()
        row = conn.execute(
            "SELECT branch FROM assignments WHERE assignment_id=?", ("bf1",)
        ).fetchone()
        assert row[0] == "issue-42-fix"

    def test_update_assignment_branch_does_not_clobber_existing(self, coord_db) -> None:
        from coord.db import get_connection
        from coord.state import update_assignment_branch

        self._insert_done_work(assignment_id="bf2", branch="issue-42-original")
        update_assignment_branch("bf2", "issue-42-other")

        conn = get_connection()
        row = conn.execute(
            "SELECT branch FROM assignments WHERE assignment_id=?", ("bf2",)
        ).fetchone()
        assert row[0] == "issue-42-original"

    def test_update_assignment_branch_noop_on_empty_args(self, coord_db) -> None:
        from coord.state import update_assignment_branch

        update_assignment_branch("", "x")  # must not raise
        update_assignment_branch("some-id", "")  # must not raise

    def test_mark_assignment_merged_flips_done(self, coord_db) -> None:
        from coord.db import get_connection
        from coord.state import mark_assignment_merged

        self._insert_done_work(assignment_id="mg1", branch="issue-42-fix")
        mark_assignment_merged("mg1")

        conn = get_connection()
        row = conn.execute(
            "SELECT status FROM assignments WHERE assignment_id=?", ("mg1",)
        ).fetchone()
        assert row[0] == "merged"

    def test_mark_assignment_merged_only_acts_on_done(self, coord_db) -> None:
        from coord.db import get_connection
        from coord.state import mark_assignment_merged

        self._insert_done_work(
            assignment_id="mg2", branch="issue-42-fix", status="running"
        )
        mark_assignment_merged("mg2")

        conn = get_connection()
        row = conn.execute(
            "SELECT status FROM assignments WHERE assignment_id=?", ("mg2",)
        ).fetchone()
        assert row[0] == "running"

    def test_mark_assignment_merged_noop_on_empty_id(self, coord_db) -> None:
        from coord.state import mark_assignment_merged

        mark_assignment_merged("")  # must not raise

    def test_mark_work_review_settled_clears_pending(self, coord_db) -> None:
        """#951: a type=work row's review_state='pending' ghost flips to 'done'."""
        from coord.db import get_connection
        from coord.state import mark_work_review_settled

        self._insert_done_work(
            assignment_id="wrs1",
            branch="issue-42-fix",
            status="merged",
            review_state="pending",
        )
        mark_work_review_settled("wrs1")

        conn = get_connection()
        row = conn.execute(
            "SELECT review_state FROM assignments WHERE assignment_id=?", ("wrs1",)
        ).fetchone()
        assert row[0] == "done"

    def test_mark_work_review_settled_only_acts_on_pending(self, coord_db) -> None:
        from coord.db import get_connection
        from coord.state import mark_work_review_settled

        self._insert_done_work(
            assignment_id="wrs2",
            branch="issue-42-fix",
            status="merged",
            review_state="dispatched",
        )
        mark_work_review_settled("wrs2")

        conn = get_connection()
        row = conn.execute(
            "SELECT review_state FROM assignments WHERE assignment_id=?", ("wrs2",)
        ).fetchone()
        assert row[0] == "dispatched"

    def test_mark_work_review_settled_ignores_non_work_type(self, coord_db) -> None:
        """Only type='work' rows are in scope — siblings are settled elsewhere (#894)."""
        from coord.db import get_connection
        from coord.state import mark_work_review_settled

        self._insert_done_work(
            assignment_id="wrs3",
            branch="issue-42-fix",
            status="done",
            review_state="pending",
            assignment_type="review",
        )
        mark_work_review_settled("wrs3")

        conn = get_connection()
        row = conn.execute(
            "SELECT review_state FROM assignments WHERE assignment_id=?", ("wrs3",)
        ).fetchone()
        assert row[0] == "pending"

    def test_mark_work_review_settled_noop_on_empty_id(self, coord_db) -> None:
        from coord.state import mark_work_review_settled

        mark_work_review_settled("")  # must not raise

    def test_reset_work_review_state_covers_test_author(self, coord_db) -> None:
        """#1180: coord diagnose --stage review --reset routes through here
        regardless of which type the stage's `latest` row was — a wedged
        test-author row must actually get reset, not silently no-op. The
        caller (coord/diagnose.py) always knows the specific row being
        diagnosed, so it passes assignment_id for test-author/mock-author."""
        from coord.db import get_connection
        from coord.state import reset_work_review_state

        self._insert_done_work(
            assignment_id="ta-reset",
            branch="test-author-ms-37-slice-1115",
            status="done",
            review_state="done",
            assignment_type="test-author",
        )
        updated = reset_work_review_state("myrepo", 42, assignment_id="ta-reset")

        assert updated == 1
        conn = get_connection()
        row = conn.execute(
            "SELECT review_state, review_verdict FROM assignments "
            "WHERE assignment_id=?",
            ("ta-reset",),
        ).fetchone()
        assert row[0] == "pending"
        assert row[1] is None

    def test_reset_work_review_state_covers_mock_author(self, coord_db) -> None:
        from coord.db import get_connection
        from coord.state import reset_work_review_state

        self._insert_done_work(
            assignment_id="ma-reset",
            branch="mock-author-ms-1",
            status="done",
            review_state="done",
            assignment_type="mock-author",
        )
        reset_work_review_state("myrepo", 42, assignment_id="ma-reset")

        conn = get_connection()
        row = conn.execute(
            "SELECT review_state FROM assignments WHERE assignment_id=?",
            ("ma-reset",),
        ).fetchone()
        assert row[0] == "pending"

    def test_reset_work_review_state_still_ignores_review_type(self, coord_db) -> None:
        """The reset is issue-scoped over work/plan/test-author/mock-author —
        the type='review' rows themselves are handled by the sibling
        delete_assignments_for_issue call, not this function."""
        from coord.db import get_connection
        from coord.state import reset_work_review_state

        self._insert_done_work(
            assignment_id="rv-untouched",
            branch="issue-42-fix",
            status="done",
            review_state="done",
            assignment_type="review",
        )
        reset_work_review_state("myrepo", 42, assignment_id="rv-untouched")

        conn = get_connection()
        row = conn.execute(
            "SELECT review_state FROM assignments WHERE assignment_id=?",
            ("rv-untouched",),
        ).fetchone()
        assert row[0] == "done"

    def test_reset_work_review_state_without_assignment_id_ignores_test_author(
        self, coord_db
    ) -> None:
        """Backward-compat default: no assignment_id given → test-author/
        mock-author rows are left untouched entirely (never issue-wide
        blasted) rather than risk wiping a sibling slice's approval."""
        from coord.db import get_connection
        from coord.state import reset_work_review_state

        self._insert_done_work(
            assignment_id="ta-noid",
            branch="test-author-ms-37-slice-1115",
            status="done",
            review_state="done",
            assignment_type="test-author",
        )
        updated = reset_work_review_state("myrepo", 42)

        assert updated == 0
        conn = get_connection()
        row = conn.execute(
            "SELECT review_state FROM assignments WHERE assignment_id=?",
            ("ta-noid",),
        ).fetchone()
        assert row[0] == "done"

    def test_reset_work_review_state_multi_slice_does_not_wipe_sibling_approval(
        self, coord_db
    ) -> None:
        """#1180 review finding: a milestone tracking issue with multiple
        test-author slices (sharing issue_number) must only have the
        *targeted* slice's review reset — a sibling's genuinely approved
        review_verdict must survive untouched."""
        from coord.db import get_connection
        from coord.state import reset_work_review_state

        self._insert_done_work(
            assignment_id="ta-wedged",
            branch="test-author-ms-37-slice-1115",
            status="done",
            review_state="done",
            assignment_type="test-author",
        )
        self._insert_done_work(
            assignment_id="ta-approved",
            branch="test-author-ms-37-slice-1116",
            status="done",
            review_state="done",
            assignment_type="test-author",
        )
        conn = get_connection()
        conn.execute(
            "UPDATE assignments SET review_verdict='approve' WHERE assignment_id=?",
            ("ta-approved",),
        )
        conn.commit()

        updated = reset_work_review_state("myrepo", 42, assignment_id="ta-wedged")

        assert updated == 1
        wedged = conn.execute(
            "SELECT review_state, review_verdict FROM assignments WHERE assignment_id=?",
            ("ta-wedged",),
        ).fetchone()
        assert wedged[0] == "pending"
        assert wedged[1] is None
        sibling = conn.execute(
            "SELECT review_state, review_verdict FROM assignments WHERE assignment_id=?",
            ("ta-approved",),
        ).fetchone()
        assert sibling[0] == "done"
        assert sibling[1] == "approve"

    def _insert_review_row(
        self, *, assignment_id: str, branch: str, review_of_assignment_id: str
    ) -> None:
        from coord.models import Assignment
        from coord.state import record_dispatched_assignment

        review = Assignment(
            machine_name="laptop",
            repo_name="myrepo",
            issue_number=42,
            issue_title="t",
            assignment_id=assignment_id,
            status="done",
            branch=branch,
            type="review",
            dispatched_at=0.0,
            review_of_assignment_id=review_of_assignment_id,
        )
        record_dispatched_assignment(assignment=review, repo_github="acme/myrepo")

    def test_delete_assignments_for_issue_scopes_review_by_review_of_assignment_id(
        self, coord_db
    ) -> None:
        """#1180: same aliasing hazard as reset_work_review_state — a
        milestone tracking issue with multiple test-author slices has one
        type='review' row per slice, all sharing issue_number. Deleting one
        slice's wedged review must not delete a sibling's already-approved
        review row."""
        from coord.db import get_connection
        from coord.state import delete_assignments_for_issue

        self._insert_review_row(
            assignment_id="rv-wedged",
            branch="test-author-ms-37-slice-1115",
            review_of_assignment_id="ta-wedged",
        )
        self._insert_review_row(
            assignment_id="rv-approved",
            branch="test-author-ms-37-slice-1116",
            review_of_assignment_id="ta-approved",
        )

        deleted = delete_assignments_for_issue(
            "myrepo", 42, types=("review",), review_of_assignment_id="ta-wedged"
        )

        assert deleted == 1
        conn = get_connection()
        remaining = conn.execute(
            "SELECT assignment_id FROM assignments WHERE type='review' ORDER BY assignment_id"
        ).fetchall()
        assert [r[0] for r in remaining] == ["rv-approved"]

    def test_delete_assignments_for_issue_without_filter_deletes_all(
        self, coord_db
    ) -> None:
        """Backward compat: omitting review_of_assignment_id preserves the
        original issue-wide blast (the pre-#1180 behavior for plain
        work/plan issues, where it's safe)."""
        from coord.db import get_connection
        from coord.state import delete_assignments_for_issue

        self._insert_review_row(
            assignment_id="rv-a", branch="issue-42-fix", review_of_assignment_id="w1",
        )
        self._insert_review_row(
            assignment_id="rv-b", branch="issue-42-fix", review_of_assignment_id="w1",
        )

        deleted = delete_assignments_for_issue("myrepo", 42, types=("review",))

        assert deleted == 2
        conn = get_connection()
        remaining = conn.execute(
            "SELECT assignment_id FROM assignments WHERE type='review'"
        ).fetchall()
        assert remaining == []


class TestPromoteAdvisoryWithCommits:
    """#3099: `coord.smoke.dispatch_pending_smoke` used to skip every
    ``status='advisory'`` row unconditionally (``if completed.status !=
    'done': continue``), even the #1357 false-positive shape where the
    branch demonstrably carries commits — `coord drive --accept-advisory`
    accepts that row and falls through to Test/Review/Merge, but the daemon
    never actually dispatched Test for it, so nothing downstream ever
    moved. `promote_advisory_with_commits` is the DB-level fix: it undoes
    the mis-classification directly, and this class covers its four
    contractual guarantees in isolation from the smoke-dispatch call site
    (covered separately in tests/test_smoke.py)."""

    def _insert_advisory_work(
        self,
        *,
        assignment_id: str,
        branch: str | None = "issue-3099-x",
        review_state: str | None = None,
    ) -> None:
        from coord.db import get_connection
        from coord.models import Assignment
        from coord.state import record_dispatched_assignment

        assignment = Assignment(
            machine_name="laptop",
            repo_name="myrepo",
            issue_number=3099,
            issue_title="t",
            assignment_id=assignment_id,
            status="advisory",
            branch=branch,
            type="work",
            dispatched_at=0.0,
        )
        record_dispatched_assignment(assignment=assignment, repo_github="acme/myrepo")
        conn = get_connection()
        conn.execute(
            "UPDATE assignments SET status='advisory' WHERE assignment_id=?",
            (assignment_id,),
        )
        if review_state is not None:
            conn.execute(
                "UPDATE assignments SET review_state=? WHERE assignment_id=?",
                (review_state, assignment_id),
            )
        conn.commit()

    def test_promotes_advisory_to_done_and_clears_advisory_review_state(
        self, coord_db
    ) -> None:
        from coord.db import get_connection
        from coord.state import promote_advisory_with_commits

        self._insert_advisory_work(assignment_id="adv-1", review_state="advisory")

        updated = promote_advisory_with_commits("adv-1")

        assert updated is True
        conn = get_connection()
        row = conn.execute(
            "SELECT status, review_state FROM assignments WHERE assignment_id=?",
            ("adv-1",),
        ).fetchone()
        assert row[0] == "done"
        assert row[1] is None

    def test_leaves_a_non_advisory_review_state_untouched(self, coord_db) -> None:
        """Only the specific 'advisory' stamp (reconcile.py's own #448
        downgrade write) is cleared — a row that had already progressed to
        review_state='dispatched'/'done' some other way must not be
        silently reset."""
        from coord.db import get_connection
        from coord.state import promote_advisory_with_commits

        self._insert_advisory_work(assignment_id="adv-2", review_state="dispatched")

        promote_advisory_with_commits("adv-2")

        conn = get_connection()
        row = conn.execute(
            "SELECT status, review_state FROM assignments WHERE assignment_id=?",
            ("adv-2",),
        ).fetchone()
        assert row[0] == "done"
        assert row[1] == "dispatched"

    def test_is_a_noop_when_status_is_not_advisory(self, coord_db) -> None:
        """Scoped `WHERE status='advisory'` guard: calling this on a row
        some other writer already resolved a different way (a human's
        `coord retry`, a race) must not clobber that outcome."""
        from coord.db import get_connection
        from coord.state import promote_advisory_with_commits

        self._insert_advisory_work(assignment_id="adv-3")
        conn = get_connection()
        conn.execute(
            "UPDATE assignments SET status='failed' WHERE assignment_id=?",
            ("adv-3",),
        )
        conn.commit()

        updated = promote_advisory_with_commits("adv-3")

        assert updated is False
        row = conn.execute(
            "SELECT status FROM assignments WHERE assignment_id=?", ("adv-3",)
        ).fetchone()
        assert row[0] == "failed"

    def test_returns_false_for_unknown_assignment_id(self, coord_db) -> None:
        from coord.state import promote_advisory_with_commits

        assert promote_advisory_with_commits("does-not-exist") is False


class TestResetWedgedTestAuthorReview:
    """#1180: repairs a test-author/mock-author row whose review_state was
    stamped 'done' by a pre-#1150 work_is_terminal false positive (tracking-
    issue aliasing), leaving it permanently invisible to both
    dispatch_pending_reviews (which only reconsiders review_state in (None,
    'pending')) and the merge gate (which requires a real approved
    type='review' row)."""

    def _insert(
        self,
        *,
        assignment_id: str,
        assignment_type: str = "test-author",
        review_state: str | None = "done",
        review_verdict: str | None = None,
    ) -> None:
        from coord.db import get_connection
        from coord.models import Assignment
        from coord.state import record_dispatched_assignment

        assignment = Assignment(
            machine_name="laptop",
            repo_name="myrepo",
            issue_number=1117,
            issue_title="t",
            assignment_id=assignment_id,
            status="done",
            branch="test-author-ms-37-slice-1115",
            type=assignment_type,
            dispatched_at=0.0,
        )
        record_dispatched_assignment(assignment=assignment, repo_github="acme/myrepo")
        conn = get_connection()
        conn.execute(
            "UPDATE assignments SET status='done', review_state=?, review_verdict=? "
            "WHERE assignment_id=?",
            (review_state, review_verdict, assignment_id),
        )
        conn.commit()

    def test_resets_wedged_test_author_row(self, coord_db) -> None:
        from coord.db import get_connection
        from coord.state import reset_wedged_test_author_review

        self._insert(assignment_id="ta-w1")
        reset_wedged_test_author_review("ta-w1")

        conn = get_connection()
        row = conn.execute(
            "SELECT review_state FROM assignments WHERE assignment_id=?",
            ("ta-w1",),
        ).fetchone()
        assert row[0] == "pending"

    def test_resets_wedged_mock_author_row(self, coord_db) -> None:
        from coord.db import get_connection
        from coord.state import reset_wedged_test_author_review

        self._insert(assignment_id="ma-w1", assignment_type="mock-author")
        reset_wedged_test_author_review("ma-w1")

        conn = get_connection()
        row = conn.execute(
            "SELECT review_state FROM assignments WHERE assignment_id=?",
            ("ma-w1",),
        ).fetchone()
        assert row[0] == "pending"

    def test_ignores_row_with_a_captured_verdict(self, coord_db) -> None:
        """A non-NULL review_verdict means a real review ran — not wedged."""
        from coord.db import get_connection
        from coord.state import reset_wedged_test_author_review

        self._insert(assignment_id="ta-w2", review_verdict="approve")
        reset_wedged_test_author_review("ta-w2")

        conn = get_connection()
        row = conn.execute(
            "SELECT review_state FROM assignments WHERE assignment_id=?",
            ("ta-w2",),
        ).fetchone()
        assert row[0] == "done"

    def test_ignores_row_not_review_state_done(self, coord_db) -> None:
        from coord.db import get_connection
        from coord.state import reset_wedged_test_author_review

        self._insert(assignment_id="ta-w3", review_state="pending")
        reset_wedged_test_author_review("ta-w3")

        conn = get_connection()
        row = conn.execute(
            "SELECT review_state FROM assignments WHERE assignment_id=?",
            ("ta-w3",),
        ).fetchone()
        assert row[0] == "pending"

    def test_ignores_non_test_author_type(self, coord_db) -> None:
        """type='work' rows are out of scope — this helper is scoped to the
        JIT test-author/mock-author aliasing bug shape only."""
        from coord.db import get_connection
        from coord.state import reset_wedged_test_author_review

        self._insert(assignment_id="wk-w1", assignment_type="work")
        reset_wedged_test_author_review("wk-w1")

        conn = get_connection()
        row = conn.execute(
            "SELECT review_state FROM assignments WHERE assignment_id=?",
            ("wk-w1",),
        ).fetchone()
        assert row[0] == "done"

    def test_noop_on_empty_id(self, coord_db) -> None:
        from coord.state import reset_wedged_test_author_review

        reset_wedged_test_author_review("")  # must not raise


class TestRecordDispatchedBranch:
    """#706: _record_dispatched_local must persist the branch column so
    completed work rows are never branch=NULL in the TUI."""

    def test_branch_derived_from_issue_title(self, coord_db) -> None:
        """branch is set to issue-{N}-{slug} when target_branch is not set."""
        from coord.agent import _slugify
        from coord.db import get_connection
        from coord.state import record_dispatched

        proposal = Proposal(
            id=1,
            machine_name="precision",
            repo_name="myrepo",
            issue_number=706,
            issue_title="Record the work branch at dispatch",
            rationale="test",
            briefing="fix it",
            type="work",
        )
        assignment_id = "aid-706-auto"
        record_dispatched(
            assignment_id=assignment_id,
            proposal=proposal,
            repo_github="acme/myrepo",
        )

        conn = get_connection()
        row = conn.execute(
            "SELECT branch FROM assignments WHERE assignment_id=?",
            (assignment_id,),
        ).fetchone()
        assert row is not None
        expected = f"issue-706-{_slugify('Record the work branch at dispatch')}"
        assert row[0] == expected, (
            f"branch should be {expected!r}, got {row[0]!r}"
        )

    def test_explicit_target_branch_is_used(self, coord_db) -> None:
        """When proposal.target_branch is set, that branch is recorded instead."""
        from coord.db import get_connection
        from coord.state import record_dispatched

        proposal = Proposal(
            id=2,
            machine_name="precision",
            repo_name="myrepo",
            issue_number=706,
            issue_title="This title would normally be slugified",
            rationale="test",
            briefing="fix it",
            type="work",
            target_branch="issue-706-explicit-branch-override",
        )
        assignment_id = "aid-706-explicit"
        record_dispatched(
            assignment_id=assignment_id,
            proposal=proposal,
            repo_github="acme/myrepo",
        )

        conn = get_connection()
        row = conn.execute(
            "SELECT branch FROM assignments WHERE assignment_id=?",
            (assignment_id,),
        ).fetchone()
        assert row is not None
        assert row[0] == "issue-706-explicit-branch-override", (
            "proposal.target_branch must be persisted verbatim"
        )

    def test_redispatch_does_not_clobber_branch(self, coord_db) -> None:
        """ON CONFLICT(assignment_id) DO NOTHING: a second call with the same
        assignment_id must NOT overwrite the branch that was already stored."""
        from coord.db import get_connection
        from coord.state import record_dispatched

        proposal_v1 = Proposal(
            id=3,
            machine_name="precision",
            repo_name="myrepo",
            issue_number=706,
            issue_title="First dispatch",
            rationale="test",
            type="work",
        )
        assignment_id = "aid-706-nodupe"
        record_dispatched(
            assignment_id=assignment_id,
            proposal=proposal_v1,
            repo_github="acme/myrepo",
        )

        # Second call with a different title (would produce a different slug).
        proposal_v2 = Proposal(
            id=3,
            machine_name="precision",
            repo_name="myrepo",
            issue_number=706,
            issue_title="Different title on redispatch",
            rationale="test",
            type="work",
        )
        record_dispatched(
            assignment_id=assignment_id,
            proposal=proposal_v2,
            repo_github="acme/myrepo",
        )

        conn = get_connection()
        row = conn.execute(
            "SELECT branch FROM assignments WHERE assignment_id=?",
            (assignment_id,),
        ).fetchone()
        assert row is not None
        # Must still carry the FIRST dispatch's branch.
        from coord.agent import _slugify
        assert row[0] == f"issue-706-{_slugify('First dispatch')}", (
            "ON CONFLICT DO NOTHING must leave the original branch untouched"
        )


class TestThinClientLocalBoardGuard:
    """#659: save_board/load_board/build_board warn (or raise) on thin clients.

    This is the guard added in #659 so that the remaining local-board
    write/read sites in cli.py are loud about their un-routed status.
    Tests here verify:
    - thin client (board_service set) → UserWarning containing '#615'
    - thin client + COORD_STRICT_LOCAL_BOARD=1 → RuntimeError
    - daemon host (board_service unset) → no #615 warning emitted
    """

    def _make_empty_board(self):
        from coord.models import Board
        return Board(active=[], completed=[], round_number=0)

    def _set_thin_client(self, monkeypatch) -> None:
        """Make _board_service() return a non-None ServiceConfig."""
        import coord.client as cc
        monkeypatch.setattr(
            cc, "resolve_board_service",
            lambda *a, **k: cc.ServiceConfig("http://daemon:7435"),
        )

    def _set_daemon_host(self, monkeypatch) -> None:
        """Make _board_service() return None (daemon host / standalone)."""
        import coord.client as cc
        monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: None)

    # ── save_board ────────────────────────────────────────────────────────────

    def test_save_board_warns_on_thin_client(self, coord_db, monkeypatch) -> None:
        from coord.state import save_board

        self._set_thin_client(monkeypatch)
        board = self._make_empty_board()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            save_board(board)

        guard_warns = [w for w in caught if "#615" in str(w.message)]
        assert guard_warns, "save_board on thin client must emit a #615 UserWarning"
        msg = str(guard_warns[0].message)
        assert "save_board" in msg
        assert "wrote" in msg
        assert "daemon" in msg

    def test_save_board_raises_in_strict_mode(self, coord_db, monkeypatch) -> None:
        from coord.state import save_board

        self._set_thin_client(monkeypatch)
        monkeypatch.setenv("COORD_STRICT_LOCAL_BOARD", "1")
        board = self._make_empty_board()

        with pytest.raises(RuntimeError, match="#615"):
            save_board(board)

    def test_save_board_no_warning_on_daemon_host(self, coord_db, monkeypatch) -> None:
        from coord.state import save_board

        self._set_daemon_host(monkeypatch)
        board = self._make_empty_board()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            save_board(board)

        guard_warns = [w for w in caught if "#615" in str(w.message)]
        assert not guard_warns, "save_board on daemon host must NOT emit a #615 warning"

    # ── load_board ────────────────────────────────────────────────────────────

    def test_load_board_warns_on_thin_client(self, coord_db, monkeypatch) -> None:
        from coord.state import load_board

        self._set_thin_client(monkeypatch)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            load_board()

        guard_warns = [w for w in caught if "#615" in str(w.message)]
        assert guard_warns, "load_board on thin client must emit a #615 UserWarning"
        msg = str(guard_warns[0].message)
        assert "load_board" in msg
        assert "read" in msg

    def test_load_board_raises_in_strict_mode(self, coord_db, monkeypatch) -> None:
        from coord.state import load_board

        self._set_thin_client(monkeypatch)
        monkeypatch.setenv("COORD_STRICT_LOCAL_BOARD", "1")

        with pytest.raises(RuntimeError, match="#615"):
            load_board()

    def test_load_board_no_warning_on_daemon_host(self, coord_db, monkeypatch) -> None:
        from coord.state import load_board

        self._set_daemon_host(monkeypatch)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            load_board()

        guard_warns = [w for w in caught if "#615" in str(w.message)]
        assert not guard_warns, "load_board on daemon host must NOT emit a #615 warning"

    # ── build_board ───────────────────────────────────────────────────────────

    def test_build_board_warns_on_thin_client(self, coord_db, monkeypatch) -> None:
        from coord.state import build_board

        self._set_thin_client(monkeypatch)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            build_board()

        guard_warns = [w for w in caught if "#615" in str(w.message)]
        assert guard_warns, "build_board on thin client must emit a #615 UserWarning"
        msg = str(guard_warns[0].message)
        assert "build_board" in msg
        assert "read" in msg

    def test_build_board_raises_in_strict_mode(self, coord_db, monkeypatch) -> None:
        from coord.state import build_board

        self._set_thin_client(monkeypatch)
        monkeypatch.setenv("COORD_STRICT_LOCAL_BOARD", "1")

        with pytest.raises(RuntimeError, match="#615"):
            build_board()

    def test_build_board_no_warning_on_daemon_host(self, coord_db, monkeypatch) -> None:
        from coord.state import build_board

        self._set_daemon_host(monkeypatch)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            build_board()

        guard_warns = [w for w in caught if "#615" in str(w.message)]
        assert not guard_warns, "build_board on daemon host must NOT emit a #615 warning"

    # ── warning content ───────────────────────────────────────────────────────

    def test_warning_carries_caller_info(self, coord_db, monkeypatch) -> None:
        """The warning message must include a caller-identifying frame string."""
        from coord.state import save_board

        self._set_thin_client(monkeypatch)
        board = self._make_empty_board()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            save_board(board)

        guard_warns = [w for w in caught if "#615" in str(w.message)]
        assert guard_warns
        msg = str(guard_warns[0].message)
        # "Caller:" must appear followed by some module/file info.
        assert "Caller:" in msg
        assert "(" in msg and ":" in msg  # "module.fn (file.py:NN)"

    def test_strict_mode_does_not_fire_on_daemon_host(
        self, coord_db, monkeypatch
    ) -> None:
        """COORD_STRICT_LOCAL_BOARD=1 must be a no-op on the daemon host."""
        from coord.state import save_board

        self._set_daemon_host(monkeypatch)
        monkeypatch.setenv("COORD_STRICT_LOCAL_BOARD", "1")
        board = self._make_empty_board()

        # Must not raise — the guard is inactive on the daemon host.
        save_board(board)

    # ── #906: mark_notified / save_plan / load_dispatched ──────────────────────
    # The #906 review flagged these three guard extensions (added alongside
    # the original build_board/save_board/load_board triad above) as having
    # NO dedicated warn/raise/no-op coverage of their own — only the static
    # AST audit (test_thin_client_board_audit.py) exercised them. These close
    # that gap with the same triad shape used above.

    # #1493: mark_notified is no longer merely guarded — it's daemon-routed
    # (POST /notified), mirroring mark_review_posted /
    # mark_needs_attention_notified.  These replace the old warn/raise/no-warn
    # triad above (which asserted the OLD behavior: warn-then-write-locally,
    # the exact silent divergence #1493 fixed) with routing coverage.  Full
    # daemon-endpoint + post_orphaned_review_findings integration coverage
    # lives in tests/test_review_verdict_relay.py.

    def test_mark_notified_routes_to_daemon_when_service_configured(
        self, coord_db, monkeypatch
    ) -> None:
        import coord.client as cc
        from coord.state import mark_notified

        self._set_thin_client(monkeypatch)
        captured: dict = {}
        monkeypatch.setattr(
            cc,
            "post_record",
            lambda svc, path, payload, **kw: captured.update(
                path=path, payload=payload
            )
            or {"ok": True},
        )

        mark_notified("aid-1493", "completion", branch="issue-1-foo")

        assert captured["path"] == "/notified"
        assert captured["payload"] == {
            "assignment_id": "aid-1493",
            "event": "completion",
            "branch": "issue-1-foo",
            "failure_reason": None,
            "exit_code": None,
        }

        # Local DB must NOT have been written (empty local DB, thin-client).
        from coord.state import load_notified

        assert "aid-1493" not in load_notified()

    def test_mark_notified_writes_local_ledger_when_no_service(
        self, coord_db, monkeypatch
    ) -> None:
        from coord.state import load_notified, mark_notified

        self._set_daemon_host(monkeypatch)

        mark_notified("aid-local", "completion")

        assert load_notified()["aid-local"]["event"] == "completion"

    def test_mark_notified_no_615_warning_either_way(self, coord_db, monkeypatch) -> None:
        """Neither branch emits the #615 guard warning any more (#1493)."""
        import coord.client as cc
        from coord.state import mark_notified

        self._set_thin_client(monkeypatch)
        monkeypatch.setattr(
            cc, "post_record", lambda svc, path, payload, **kw: {"ok": True}
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            mark_notified("aid-thin", "completion")
        assert not [w for w in caught if "#615" in str(w.message)]

        self._set_daemon_host(monkeypatch)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            mark_notified("aid-daemon", "completion")
        assert not [w for w in caught if "#615" in str(w.message)]

    def test_save_plan_warns_on_thin_client(self, coord_db, monkeypatch) -> None:
        from coord.state import save_plan

        self._set_thin_client(monkeypatch)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            save_plan("no-such-id", {"steps": [], "blockers": []})

        guard_warns = [w for w in caught if "#615" in str(w.message)]
        assert guard_warns, "save_plan on thin client must emit a #615 UserWarning"
        assert "save_plan" in str(guard_warns[0].message)

    def test_save_plan_raises_in_strict_mode(self, coord_db, monkeypatch) -> None:
        from coord.state import save_plan

        self._set_thin_client(monkeypatch)
        monkeypatch.setenv("COORD_STRICT_LOCAL_BOARD", "1")

        with pytest.raises(RuntimeError, match="#615"):
            save_plan("no-such-id", {"steps": [], "blockers": []})

    def test_save_plan_no_warning_on_daemon_host(self, coord_db, monkeypatch) -> None:
        from coord.state import save_plan

        self._set_daemon_host(monkeypatch)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            save_plan("no-such-id", {"steps": [], "blockers": []})

        guard_warns = [w for w in caught if "#615" in str(w.message)]
        assert not guard_warns, "save_plan on daemon host must NOT emit a #615 warning"

    # #1493: load_dispatched is no longer merely guarded — it's daemon-routed
    # (reads GET /board, filtered to dispatched_at IS NOT NULL), mirroring
    # load_done_reviews_needing_post (#905). These replace the old
    # warn/raise/no-warn triad with routing coverage.

    def test_load_dispatched_reads_from_daemon_when_service_configured(
        self, coord_db, monkeypatch
    ) -> None:
        import coord.client as cc
        from coord.state import load_dispatched

        self._set_thin_client(monkeypatch)
        monkeypatch.setattr(
            cc,
            "fetch_board_payload",
            lambda svc, **kw: {
                "assignments": [
                    {
                        "assignment_id": "aid-905",
                        "machine_name": "precision",
                        "repo_name": "api",
                        "repo_github": "acme/api",
                        "issue_number": 42,
                        "issue_title": "t",
                        "status": "running",
                        "type": "work",
                        "dispatched_at": 12345.0,
                    },
                    {
                        # Never dispatched (e.g. a save_board-only test row) —
                        # must be excluded, matching the local SQL's
                        # dispatched_at IS NOT NULL filter.
                        "assignment_id": "aid-undispatched",
                        "dispatched_at": None,
                    },
                ]
            },
        )

        result = load_dispatched()
        assert [r["assignment_id"] for r in result] == ["aid-905"]
        assert result[0]["machine_name"] == "precision"
        assert result[0]["dispatched_at"] == 12345.0

    def test_load_dispatched_falls_back_to_local_when_no_service(
        self, coord_db, monkeypatch
    ) -> None:
        from coord.state import load_dispatched, record_dispatched
        from coord.models import Proposal

        self._set_daemon_host(monkeypatch)
        record_dispatched(
            assignment_id="aid-local",
            proposal=Proposal(
                id=1,
                machine_name="laptop",
                repo_name="api",
                issue_number=1,
                issue_title="t",
                rationale="r",
                files_likely=[],
            ),
            repo_github="acme/api",
        )

        result = load_dispatched()
        assert any(r["assignment_id"] == "aid-local" for r in result)

    def test_load_dispatched_falls_back_to_local_on_daemon_error(
        self, coord_db, monkeypatch
    ) -> None:
        """If the daemon fetch raises, fall back to the local DB (best-effort)."""
        import coord.client as cc
        from coord.state import load_dispatched

        self._set_thin_client(monkeypatch)
        monkeypatch.setattr(
            cc,
            "fetch_board_payload",
            lambda svc, **kw: (_ for _ in ()).throw(RuntimeError("daemon down")),
        )

        # Empty local DB + unreachable daemon → empty list, not an exception.
        assert load_dispatched() == []

    def test_load_dispatched_no_615_warning_either_way(self, coord_db, monkeypatch) -> None:
        """Neither branch emits the #615 guard warning any more (#1493)."""
        import coord.client as cc
        from coord.state import load_dispatched

        self._set_thin_client(monkeypatch)
        monkeypatch.setattr(cc, "fetch_board_payload", lambda svc, **kw: {"assignments": []})
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            load_dispatched()
        assert not [w for w in caught if "#615" in str(w.message)]

        self._set_daemon_host(monkeypatch)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            load_dispatched()
        assert not [w for w in caught if "#615" in str(w.message)]


class TestSetAssignmentFailureReason:
    """#618: set_assignment_failure_reason() persists launch-failure info on the row."""

    def _insert_assignment(self, coord_db, assignment_id: str) -> None:
        """Insert a minimal running assignment row for testing."""
        from coord.models import Assignment
        from coord.state import record_dispatched_assignment

        a = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=42,
            issue_title="test issue",
            assignment_id=assignment_id,
            status="running",
            branch="issue-42-foo",
            type="work",
            dispatched_at=0.0,
        )
        record_dispatched_assignment(assignment=a, repo_github="acme/api")

    def test_schema_has_failure_reason_column(self, coord_db) -> None:
        """The assignments table must have a failure_reason column (#618)."""
        from coord.db import get_connection

        conn = get_connection()
        # #3083: PRAGMA has no Postgres equivalent — sql.table_columns is
        # the seam's portable spelling of "what columns does this table have".
        cols = {name for name, _type in sql.table_columns(conn, "assignments")}
        assert "failure_reason" in cols, (
            "assignments table is missing failure_reason column — "
            "check _migrate_add_columns in coord/db.py"
        )

    def test_persists_reason_and_marks_failed(self, coord_db) -> None:
        """set_assignment_failure_reason writes reason + flips status to 'failed'."""
        from coord.db import get_connection
        from coord.state import set_assignment_failure_reason

        aid = "test-fail-001"
        self._insert_assignment(coord_db, aid)
        set_assignment_failure_reason(aid, "branch already checked out at /some/path")

        conn = get_connection()
        row = conn.execute(
            "SELECT status, failure_reason, finished_at FROM assignments WHERE assignment_id=?",
            (aid,),
        ).fetchone()
        assert row is not None
        assert row["status"] == "failed"
        assert row["failure_reason"] == "branch already checked out at /some/path"
        assert row["finished_at"] is not None

    def test_long_reason_is_truncated_to_512_chars(self, coord_db) -> None:
        """Reasons longer than 512 chars are truncated, not rejected."""
        from coord.db import get_connection
        from coord.state import set_assignment_failure_reason

        aid = "test-fail-002"
        self._insert_assignment(coord_db, aid)
        long_reason = "x" * 1000
        set_assignment_failure_reason(aid, long_reason)

        conn = get_connection()
        row = conn.execute(
            "SELECT failure_reason FROM assignments WHERE assignment_id=?",
            (aid,),
        ).fetchone()
        assert row is not None
        assert len(row[0]) == 512

    def test_noop_on_empty_assignment_id(self, coord_db) -> None:
        """Calling with empty string silently does nothing."""
        from coord.state import set_assignment_failure_reason

        set_assignment_failure_reason("", "reason")  # must not raise

    def test_noop_on_missing_assignment_id(self, coord_db) -> None:
        """Calling with a non-existent ID silently does nothing."""
        from coord.state import set_assignment_failure_reason

        set_assignment_failure_reason("no-such-id", "reason")  # must not raise


# ── Durable issue_comments mirror (#873) ─────────────────────────────────────

class TestRecordIssueCommentCapture:
    def test_writes_local_row_with_parsed_marker_columns(self, coord_db) -> None:
        from coord.comments import format_completion
        from coord.state import record_issue_comment_capture

        body = format_completion(
            assignment_id="abc123",
            machine_name="macbook",
            repo_name="acme/api",
            issue_number=42,
            exit_code=0,
        )
        record_issue_comment_capture(
            repo_name="acme/api", issue_number=42, body=body, gh_comment_id=111,
        )
        row = coord_db.execute(
            "SELECT * FROM issue_comments WHERE gh_comment_id=111"
        ).fetchone()
        assert row is not None
        assert row["repo_name"] == "acme/api"
        assert row["issue_number"] == 42
        assert row["coord_event"] == "completion"
        assert row["coord_assignment_id"] == "abc123"
        assert row["machine"] == "macbook"
        assert row["body"] == body

    def test_non_coord_body_leaves_marker_columns_null(self, coord_db) -> None:
        from coord.state import record_issue_comment_capture

        record_issue_comment_capture(
            repo_name="acme/api", issue_number=1, body="just a human comment",
            gh_comment_id=222,
        )
        row = coord_db.execute(
            "SELECT * FROM issue_comments WHERE gh_comment_id=222"
        ).fetchone()
        assert row["coord_event"] is None
        assert row["coord_assignment_id"] is None

    def test_upsert_idempotent_on_gh_comment_id(self, coord_db) -> None:
        from coord.state import record_issue_comment_capture

        record_issue_comment_capture(
            repo_name="acme/api", issue_number=1, body="v1", gh_comment_id=333,
        )
        record_issue_comment_capture(
            repo_name="acme/api", issue_number=1, body="v2 (edited)", gh_comment_id=333,
        )
        rows = coord_db.execute(
            "SELECT body FROM issue_comments WHERE gh_comment_id=333"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["body"] == "v2 (edited)"

    def test_null_gh_comment_id_never_dedups(self, coord_db) -> None:
        """A comment id that couldn't be resolved (rare) still gets a
        durable row each call — no natural key to upsert against."""
        from coord.state import record_issue_comment_capture

        record_issue_comment_capture(repo_name="acme/api", issue_number=1, body="a")
        record_issue_comment_capture(repo_name="acme/api", issue_number=1, body="b")
        count = coord_db.execute(
            "SELECT COUNT(*) c FROM issue_comments WHERE gh_comment_id IS NULL"
        ).fetchone()["c"]
        assert count == 2

    def test_routes_to_daemon_when_service_set(self, coord_db, monkeypatch) -> None:
        from coord import client as cc
        from coord.state import record_issue_comment_capture

        monkeypatch.setattr(
            cc, "resolve_board_service",
            lambda *a, **k: cc.ServiceConfig("http://d:7435"),
        )
        captured: dict = {}
        monkeypatch.setattr(
            cc, "post_record",
            lambda svc, path, payload, **kw: captured.update(
                path=path, payload=payload
            ) or {"ok": True},
        )
        monkeypatch.setattr(
            cc, "request_resource",
            lambda *a, **k: pytest.fail(
                "an owner/repo-slugged repo_name is not addressable as a "
                "resource path segment — it must not be attempted (#1946)"
            ),
        )
        record_issue_comment_capture(
            repo_name="acme/api", issue_number=1, body="x", gh_comment_id=444,
        )
        # #1946: capture-at-write keys `issue_comments` on the gh SLUG, and
        # Starlette's {repo_name} converter cannot match a slash, so this one
        # seam stays on the RPC route by design — see
        # board_service.resource_addressable. Its telemetry is NOT evidence of
        # an unmigrated client.
        assert captured["path"] == "/issue-comments"
        assert captured["payload"]["action"] == "capture"
        assert captured["payload"]["gh_comment_id"] == 444
        # Routed → no local row created.
        assert coord_db.execute(
            "SELECT COUNT(*) c FROM issue_comments"
        ).fetchone()["c"] == 0


class TestSyncIssueComments:
    def test_sync_upserts_comments_from_github(self, coord_db, monkeypatch) -> None:
        from coord import github_ops
        from coord.state import sync_issue_comments

        fetched = [
            {
                "url": "https://github.com/acme/api/issues/7#issuecomment-1",
                "body": "human comment",
                "author": {"login": "someone"},
                "createdAt": "2026-07-02T01:27:50Z",
            },
            {
                "url": "https://github.com/acme/api/issues/7#issuecomment-2",
                "body": "<!-- coord:event=completion assignment=a1 machine=m -->\ndone",
                "author": {"login": "coord-bot"},
                "createdAt": "2026-07-02T02:00:00Z",
            },
        ]
        monkeypatch.setattr(github_ops, "get_issue_comments", lambda *a, **k: fetched)

        n = sync_issue_comments("api", 7, repo_github="acme/api")
        assert n == 2
        # #873 fix: rows must land under the GitHub slug (repo_github), not the
        # coordinator.yml config key (repo_name) — matching what capture-at-write
        # always stores, so both write paths agree on one convention.
        rows = coord_db.execute(
            "SELECT * FROM issue_comments WHERE repo_name='acme/api' AND issue_number=7 "
            "ORDER BY gh_comment_id"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["gh_comment_id"] == 1
        assert rows[0]["author"] == "someone"
        assert rows[0]["coord_event"] is None
        assert rows[1]["gh_comment_id"] == 2
        assert rows[1]["coord_event"] == "completion"
        assert rows[1]["coord_assignment_id"] == "a1"

    def test_sync_repo_name_matches_capture_at_write_convention(
        self, coord_db, monkeypatch
    ) -> None:
        """Regression for the #873 review finding: capture-at-write always
        receives the GitHub slug (real call sites pass ``repo.github``), so
        the backfill-sync path — invoked with the coordinator.yml config key
        as its first arg, e.g. ``sync_issue_comments(repo.name, ...,
        repo_github=repo.github)`` — must store rows under the *same* slug,
        not the config key. Otherwise a row's ``repo_name`` flips depending
        on which path last touched it (both upsert on ``gh_comment_id``),
        and callers filtering by exact ``repo_name`` silently see only half
        the comment history."""
        from coord import github_ops
        from coord.state import record_issue_comment_capture, sync_issue_comments

        # Capture-at-write path: always given the slug, per every real call site.
        record_issue_comment_capture(
            repo_name="acme/api", issue_number=7, body="from capture-at-write",
            gh_comment_id=1,
        )

        # Backfill-sync path: given the config key ("api") as repo_name, plus
        # the slug separately as repo_github — mirroring issues.py's real call.
        fetched = [{
            "url": "https://github.com/acme/api/issues/7#issuecomment-1",
            "body": "from sync (should self-heal, same gh_comment_id)",
            "author": {"login": "someone"},
            "createdAt": "2026-07-02T01:27:50Z",
        }]
        monkeypatch.setattr(github_ops, "get_issue_comments", lambda *a, **k: fetched)
        sync_issue_comments("api", 7, repo_github="acme/api")

        rows = coord_db.execute(
            "SELECT repo_name FROM issue_comments WHERE gh_comment_id=1"
        ).fetchall()
        # Same natural key (gh_comment_id) from both paths → exactly one row,
        # and it must be stored under the slug both paths agree on.
        assert len(rows) == 1
        assert rows[0]["repo_name"] == "acme/api"

    def test_sync_idempotent_rerun_no_dupes(self, coord_db, monkeypatch) -> None:
        from coord import github_ops
        from coord.state import sync_issue_comments

        fetched = [{
            "url": "https://github.com/acme/api/issues/7#issuecomment-9",
            "body": "hi", "author": {"login": "x"}, "createdAt": "2026-07-02T00:00:00Z",
        }]
        monkeypatch.setattr(github_ops, "get_issue_comments", lambda *a, **k: fetched)

        sync_issue_comments("api", 7, repo_github="acme/api")
        sync_issue_comments("api", 7, repo_github="acme/api")
        count = coord_db.execute(
            "SELECT COUNT(*) c FROM issue_comments WHERE gh_comment_id=9"
        ).fetchone()["c"]
        assert count == 1

    def test_sync_returns_zero_on_github_error(self, coord_db, monkeypatch) -> None:
        from coord import github_ops
        from coord.state import sync_issue_comments

        def _boom(*a, **k):
            raise RuntimeError("gh unreachable")

        monkeypatch.setattr(github_ops, "get_issue_comments", _boom)
        assert sync_issue_comments("api", 7, repo_github="acme/api") == 0

    def test_sync_routes_to_daemon_when_service_set(self, coord_db, monkeypatch) -> None:
        from coord import client as cc
        from coord.state import sync_issue_comments

        monkeypatch.setattr(
            cc, "resolve_board_service",
            lambda *a, **k: cc.ServiceConfig("http://d:7435"),
        )
        captured: dict = {}
        monkeypatch.setattr(
            cc, "request_resource",
            lambda svc, method, path, payload=None, **kw: captured.update(
                method=method, path=path, payload=payload
            ) or {"ok": True, "action": "sync", "synced": 3},
        )
        assert sync_issue_comments("api", 7, repo_github="acme/api") == 3
        # #1946: was POST /issue-comments. Unlike the capture seam, `sync`'s
        # repo_name is the SHORT name, so it is resource-addressable.
        assert (captured["method"], captured["path"]) == (
            "POST", "/issue/api/7/comments",
        )
        assert captured["payload"]["action"] == "sync"
        assert captured["payload"]["repo_github"] == "acme/api"


class TestListIssueNumbersWithAssignments:
    def test_local_reads_from_assignments_table(self, coord_db) -> None:
        from coord.state import list_issue_numbers_with_assignments

        coord_db.execute(
            "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
            "issue_number, issue_title) VALUES ('a1', 'm', 'api', 7, 't')"
        )
        coord_db.execute(
            "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
            "issue_number, issue_title) VALUES ('a2', 'm', 'other-repo', 9, 't')"
        )
        coord_db.commit()
        assert list_issue_numbers_with_assignments("api") == {7}

    def test_missing_assignments_archive_table_is_tolerated(self, coord_db) -> None:
        """assignments_archive doesn't exist until housekeeping runs at
        least once — must not raise."""
        from coord.state import list_issue_numbers_with_assignments

        assert list_issue_numbers_with_assignments("api") == set()

    def test_routes_to_daemon_via_board_fetch(self, coord_db, monkeypatch) -> None:
        from coord import client as cc
        from coord.models import Assignment, Board
        from coord.state import list_issue_numbers_with_assignments

        monkeypatch.setattr(
            cc, "resolve_board_service",
            lambda *a, **k: cc.ServiceConfig("http://d:7435"),
        )
        board = Board(
            active=[Assignment(machine_name="m", repo_name="api", issue_number=5,
                                issue_title="t")],
            completed=[Assignment(machine_name="m", repo_name="api", issue_number=6,
                                   issue_title="t")],
        )
        monkeypatch.setattr(cc, "fetch_remote_board", lambda svc, **kw: board)
        assert list_issue_numbers_with_assignments("api") == {5, 6}


class TestLegCounts:
    """`coord.state.leg_counts` / `_leg_counts_local` (#3060).

    All-time per-issue assignment leg counts by type, keyed `"repo#N"` — the
    thing that replaces the misleading `drive_queue.attempts` relaunch
    counter. Mirrors `TestListIssueNumbersWithAssignments`'s union-across-
    `assignments`+`assignments_archive` coverage, since `_leg_counts_local`
    is built the same way for the same reason (#2983's archive trap).
    """

    @staticmethod
    def _insert(conn, table, assignment_id, repo, issue, atype):
        conn.execute(
            f"INSERT INTO {table} (assignment_id, machine_name, repo_name, "  # noqa: S608
            "issue_number, issue_title, type) VALUES (?, 'm', ?, ?, 't', ?)",
            (assignment_id, repo, issue, atype),
        )

    def test_local_counts_by_type_keyed_repo_hash_issue(self, coord_db) -> None:
        from coord.state import leg_counts

        self._insert(coord_db, "assignments", "a-1", "api", 7, "work")
        self._insert(coord_db, "assignments", "a-2", "api", 7, "work")
        self._insert(coord_db, "assignments", "a-3", "api", 7, "review")
        self._insert(coord_db, "assignments", "a-4", "other-repo", 9, "smoke")
        coord_db.commit()

        assert leg_counts() == {
            "api#7": {"work": 2, "review": 1},
            "other-repo#9": {"smoke": 1},
        }

    def test_missing_assignments_archive_table_is_tolerated(self, coord_db) -> None:
        """assignments_archive doesn't exist until housekeeping runs at least
        once — must not raise (mirrors the identical rule for
        `list_issue_numbers_with_assignments`)."""
        from coord.state import leg_counts

        assert leg_counts() == {}

    def test_spans_both_assignments_and_assignments_archive(self, coord_db) -> None:
        """#3060's "archive trap": a naive `SELECT ... FROM assignments`
        alone would undercount once `coord housekeeping` moves a terminal
        leg into `assignments_archive` — the whole point of this field is
        that the count does NOT shrink when that happens."""
        from coord.housekeeping import _ensure_archive_mirror
        from coord.state import leg_counts

        self._insert(coord_db, "assignments", "a-1", "api", 7, "work")
        coord_db.commit()
        _ensure_archive_mirror(coord_db, "assignments", "assignments_archive")
        coord_db.execute(
            "INSERT INTO assignments_archive SELECT * FROM assignments WHERE assignment_id='a-1'"
        )
        coord_db.execute("DELETE FROM assignments WHERE assignment_id='a-1'")
        self._insert(coord_db, "assignments", "a-2", "api", 7, "review")
        coord_db.commit()

        assert leg_counts() == {"api#7": {"work": 1, "review": 1}}

    def test_routes_to_daemon_when_board_service_set(self, coord_db, monkeypatch) -> None:
        from coord import client as cc
        from coord.state import leg_counts

        monkeypatch.setattr(
            cc, "resolve_board_service",
            lambda *a, **k: cc.ServiceConfig("http://d:7435"),
        )
        monkeypatch.setattr(
            cc, "fetch_leg_counts", lambda svc, **kw: {"api#7": {"work": 3}},
        )
        assert leg_counts() == {"api#7": {"work": 3}}


class TestTestVerdictStalenessAnchor:
    """#1479: `record_test_verdict` best-effort captures test_head_sha /
    test_patch_id / test_base_sha alongside a terminal (passed/skipped)
    verdict, so `coord.merge_queue.has_smoke_verdict` can later detect a
    stale verdict (moved base or changed branch content)."""

    @staticmethod
    def _seed_assignment(coord_db, *, assignment_id="aid-1", branch="worker/aid-1"):
        coord_db.execute(
            "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
            "issue_number, issue_title, branch) VALUES (?, 'm1', 'api', 1, 't', ?)",
            (assignment_id, branch),
        )
        coord_db.commit()

    @staticmethod
    def _config(monkeypatch, *, develop_branch=None):
        from coord import config as _config_mod
        from coord.models import Repo

        repo = Repo(name="api", github="acme/api", default_branch="main",
                    develop_branch=develop_branch)

        class _Cfg:
            def repo(self, name):
                return repo if name == "api" else None

        monkeypatch.setattr(_config_mod, "load", lambda *a, **k: _Cfg())
        return repo

    def test_stamps_anchor_fields_on_passed_verdict(self, coord_db, monkeypatch) -> None:
        self._seed_assignment(coord_db)
        self._config(monkeypatch)
        monkeypatch.setattr(
            "coord.github_ops.get_branch_sha",
            lambda repo, branch: {"worker/aid-1": "branch-sha", "main": "main-sha"}[branch],
        )
        monkeypatch.setattr(
            "coord.github_ops.get_branch_patch_id", lambda repo, base, branch: "patch-id-1",
        )

        record_test_verdict(assignment_id="aid-1", test_state="passed")

        row = coord_db.execute(
            "SELECT test_head_sha, test_patch_id, test_base_sha "
            "FROM assignments WHERE assignment_id='aid-1'"
        ).fetchone()
        assert row["test_head_sha"] == "branch-sha"
        assert row["test_patch_id"] == "patch-id-1"
        assert row["test_base_sha"] == "main-sha"

    def test_stamps_anchor_fields_on_skipped_verdict(self, coord_db, monkeypatch) -> None:
        self._seed_assignment(coord_db)
        self._config(monkeypatch)
        monkeypatch.setattr("coord.github_ops.get_branch_sha", lambda repo, branch: "sha")
        monkeypatch.setattr(
            "coord.github_ops.get_branch_patch_id", lambda repo, base, branch: "patch",
        )

        record_test_verdict(assignment_id="aid-1", test_state="skipped")

        row = coord_db.execute(
            "SELECT test_base_sha FROM assignments WHERE assignment_id='aid-1'"
        ).fetchone()
        assert row["test_base_sha"] == "sha"

    def test_no_anchor_stamped_on_failed_verdict(self, coord_db, monkeypatch) -> None:
        """A 'failed' verdict already blocks the merge gate on its own — the
        extra `gh` round trips are skipped."""
        self._seed_assignment(coord_db)
        calls = []
        monkeypatch.setattr(
            "coord.github_ops.get_branch_sha",
            lambda repo, branch: calls.append(branch) or "sha",
        )

        record_test_verdict(assignment_id="aid-1", test_state="failed", test_reason="boom")

        assert calls == []
        row = coord_db.execute(
            "SELECT test_base_sha FROM assignments WHERE assignment_id='aid-1'"
        ).fetchone()
        assert row["test_base_sha"] is None

    def test_gh_failure_leaves_anchor_null_without_raising(self, coord_db, monkeypatch) -> None:
        self._seed_assignment(coord_db)
        self._config(monkeypatch)
        monkeypatch.setattr(
            "coord.github_ops.get_branch_sha",
            lambda repo, branch: (_ for _ in ()).throw(RuntimeError("gh unavailable")),
        )

        # Must not raise — the verdict write itself must still succeed.
        record_test_verdict(assignment_id="aid-1", test_state="passed")

        row = coord_db.execute(
            "SELECT test_state, test_base_sha FROM assignments WHERE assignment_id='aid-1'"
        ).fetchone()
        assert row["test_state"] == "passed"
        assert row["test_base_sha"] is None

    def test_retest_with_gh_exception_clears_stale_anchors_from_prior_verdict(
        self, coord_db, monkeypatch
    ) -> None:
        """#2706: a RE-test whose `gh` probes raise must NOT leave the
        PREVIOUS verdict's anchors standing. A prior passing verdict left
        real SHAs in these columns; if the re-test's probes fail and the
        write is skipped, the new verdict is silently attributed to a
        branch/base it never tested — worse than NULL, which the merge gate
        already treats as "staleness tracking unavailable" and skips."""
        self._seed_assignment(coord_db)
        coord_db.execute(
            "UPDATE assignments SET test_head_sha='stale-head', "
            "test_base_sha='stale-base', test_patch_id='stale-patch' "
            "WHERE assignment_id='aid-1'"
        )
        coord_db.commit()
        self._config(monkeypatch)
        monkeypatch.setattr(
            "coord.github_ops.get_branch_sha",
            lambda repo, branch: (_ for _ in ()).throw(RuntimeError("rate limited")),
        )

        record_test_verdict(assignment_id="aid-1", test_state="passed")

        row = coord_db.execute(
            "SELECT test_state, test_head_sha, test_base_sha, test_patch_id "
            "FROM assignments WHERE assignment_id='aid-1'"
        ).fetchone()
        assert row["test_state"] == "passed"
        assert row["test_head_sha"] is None
        assert row["test_base_sha"] is None
        assert row["test_patch_id"] is None

    def test_retest_with_probes_returning_none_clears_stale_anchors(
        self, coord_db, monkeypatch
    ) -> None:
        """The observed quadraui#595 failure mode: the probes don't raise,
        they just all come back `None` (a secondary rate-limit 403 that
        `get_branch_sha`/`get_branch_patch_id` swallow into `None` rather
        than raising). The old `if head is None and base is None and
        patch_id is None: return` guard treated this identically to "nothing
        to capture" and skipped the write — which is right for a first
        verdict (nothing to lose) but wrong for a re-test (loses the fact
        that the standing anchors are now stale)."""
        self._seed_assignment(coord_db)
        coord_db.execute(
            "UPDATE assignments SET test_head_sha='stale-head', "
            "test_base_sha='stale-base', test_patch_id='stale-patch' "
            "WHERE assignment_id='aid-1'"
        )
        coord_db.commit()
        self._config(monkeypatch)
        monkeypatch.setattr("coord.github_ops.get_branch_sha", lambda repo, branch: None)
        monkeypatch.setattr(
            "coord.github_ops.get_branch_patch_id", lambda repo, base, branch: None,
        )

        record_test_verdict(assignment_id="aid-1", test_state="passed")

        row = coord_db.execute(
            "SELECT test_head_sha, test_base_sha, test_patch_id "
            "FROM assignments WHERE assignment_id='aid-1'"
        ).fetchone()
        assert row["test_head_sha"] is None
        assert row["test_base_sha"] is None
        assert row["test_patch_id"] is None

    def test_repo_not_in_config_leaves_anchor_null(self, coord_db, monkeypatch) -> None:
        self._seed_assignment(coord_db)

        class _EmptyCfg:
            def repo(self, name):
                return None

        from coord import config as _config_mod
        monkeypatch.setattr(_config_mod, "load", lambda *a, **k: _EmptyCfg())

        record_test_verdict(assignment_id="aid-1", test_state="passed")

        row = coord_db.execute(
            "SELECT test_base_sha FROM assignments WHERE assignment_id='aid-1'"
        ).fetchone()
        assert row["test_base_sha"] is None

    def test_repo_not_in_config_on_retest_clears_stale_anchor(
        self, coord_db, monkeypatch
    ) -> None:
        """#2706: same guarantee as the `gh`-probe-failure cases, but for the
        repo-unresolvable-in-config path — it must also null out a prior
        verdict's anchors on a re-test rather than silently leaving them."""
        self._seed_assignment(coord_db)
        coord_db.execute(
            "UPDATE assignments SET test_head_sha='stale-head', "
            "test_base_sha='stale-base', test_patch_id='stale-patch' "
            "WHERE assignment_id='aid-1'"
        )
        coord_db.commit()

        class _EmptyCfg:
            def repo(self, name):
                return None

        from coord import config as _config_mod
        monkeypatch.setattr(_config_mod, "load", lambda *a, **k: _EmptyCfg())

        record_test_verdict(assignment_id="aid-1", test_state="passed")

        row = coord_db.execute(
            "SELECT test_head_sha, test_base_sha, test_patch_id "
            "FROM assignments WHERE assignment_id='aid-1'"
        ).fetchone()
        assert row["test_head_sha"] is None
        assert row["test_base_sha"] is None
        assert row["test_patch_id"] is None

    def test_milestone_repo_resolves_feature_branch_as_base(
        self, coord_db, monkeypatch
    ) -> None:
        """#934: a repo that opted into the develop/feature-branch git model
        resolves the milestone's feature/ms-NN branch as the base — not the
        flat default_branch — mirroring `enqueue_approved_work`."""
        self._seed_assignment(coord_db)
        self._config(monkeypatch, develop_branch="develop")
        monkeypatch.setattr(
            "coord.github_ops.get_issue",
            lambda repo, issue_number: {"milestone": {"number": 7}},
        )
        seen_bases = []

        def _get_sha(repo, branch):
            seen_bases.append(branch)
            return f"sha-for-{branch}"

        monkeypatch.setattr("coord.github_ops.get_branch_sha", _get_sha)
        monkeypatch.setattr(
            "coord.github_ops.get_branch_patch_id", lambda repo, base, branch: "patch",
        )

        record_test_verdict(assignment_id="aid-1", test_state="passed")

        row = coord_db.execute(
            "SELECT test_base_sha FROM assignments WHERE assignment_id='aid-1'"
        ).fetchone()
        assert row["test_base_sha"] == "sha-for-feature/ms-7"
        assert "feature/ms-7" in seen_bases


class TestRecordTestVerdictToolchain:
    """#1629 (H-2): `test_toolchain` — the toolchain that produced a Test
    verdict — round-trips through `record_test_verdict` -> the DB -> the
    board projection, and a historical verdict with no toolchain renders as
    unknown rather than breaking anything."""

    @staticmethod
    def _seed_assignment(coord_db, *, assignment_id="aid-1"):
        coord_db.execute(
            "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
            "issue_number, issue_title, branch) VALUES (?, 'm1', 'api', 1, 't', ?)",
            (assignment_id, f"worker/{assignment_id}"),
        )
        coord_db.commit()

    def test_toolchain_round_trips_through_the_db(self, coord_db) -> None:
        self._seed_assignment(coord_db)

        record_test_verdict(
            assignment_id="aid-1", test_state="passed", test_toolchain="rustc 1.95.0",
        )

        row = coord_db.execute(
            "SELECT test_state, test_toolchain FROM assignments WHERE assignment_id='aid-1'"
        ).fetchone()
        assert row["test_state"] == "passed"
        assert row["test_toolchain"] == "rustc 1.95.0"

    def test_toolchain_round_trips_through_build_board(self, coord_db) -> None:
        from coord.state import build_board

        self._seed_assignment(coord_db)

        record_test_verdict(
            assignment_id="aid-1", test_state="failed",
            test_reason="boom", test_toolchain="python 3.12.4, node 20.11.0",
        )

        board = build_board()
        row = next(a for a in board.active if a.assignment_id == "aid-1")
        assert row.test_toolchain == "python 3.12.4, node 20.11.0"

    def test_omitted_toolchain_defaults_to_none(self, coord_db) -> None:
        """Every caller predating #1629 (and any that just doesn't have a
        toolchain to report) must keep working unchanged."""
        self._seed_assignment(coord_db)

        record_test_verdict(assignment_id="aid-1", test_state="passed")

        row = coord_db.execute(
            "SELECT test_toolchain FROM assignments WHERE assignment_id='aid-1'"
        ).fetchone()
        assert row["test_toolchain"] is None

    def test_a_later_verdict_without_a_toolchain_clears_the_old_one(self, coord_db) -> None:
        """test_toolchain describes THIS verdict — it must not survive a
        re-test that didn't resolve one, or a stale toolchain would
        misattribute the new result to hardware that didn't produce it."""
        self._seed_assignment(coord_db)
        record_test_verdict(
            assignment_id="aid-1", test_state="passed", test_toolchain="rustc 1.95.0",
        )

        record_test_verdict(assignment_id="aid-1", test_state="passed")

        row = coord_db.execute(
            "SELECT test_toolchain FROM assignments WHERE assignment_id='aid-1'"
        ).fetchone()
        assert row["test_toolchain"] is None

    def test_historical_row_with_no_toolchain_column_value_is_none_not_a_crash(
        self, coord_db
    ) -> None:
        """A row written before #1629 has test_toolchain=NULL by construction
        (the ALTER TABLE migration adds the column with no default) — assert
        the read path (build_board -> Assignment) tolerates it."""
        from coord.state import build_board

        self._seed_assignment(coord_db, assignment_id="aid-old")
        coord_db.execute(
            "UPDATE assignments SET test_state='passed' WHERE assignment_id='aid-old'"
        )
        coord_db.commit()

        board = build_board()
        row = next(a for a in board.active if a.assignment_id == "aid-old")
        assert row.test_toolchain is None


class TestRecordUatVerdict:
    """#2687: `record_uat_verdict` — the single-row seam writer behind
    `coord uat <id> --passed|--failed`. Deliberately simpler than
    `record_test_verdict`: no legacy mirror column, no staleness anchor —
    see `coord.models.Assignment.uat_state`'s docstring for why."""

    @staticmethod
    def _seed_assignment(coord_db, *, assignment_id="aid-1", branch="worker/aid-1"):
        coord_db.execute(
            "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
            "issue_number, issue_title, branch) VALUES (?, 'm1', 'api', 1, 't', ?)",
            (assignment_id, branch),
        )
        coord_db.commit()

    def test_records_passed_verdict(self, coord_db) -> None:
        self._seed_assignment(coord_db)
        record_uat_verdict(assignment_id="aid-1", uat_state="passed")

        row = coord_db.execute(
            "SELECT uat_state, uat_reason FROM assignments WHERE assignment_id='aid-1'"
        ).fetchone()
        assert row["uat_state"] == "passed"
        assert row["uat_reason"] is None

    def test_records_failed_verdict_with_reason(self, coord_db) -> None:
        self._seed_assignment(coord_db)
        record_uat_verdict(
            assignment_id="aid-1", uat_state="failed", uat_reason="logo is cropped",
        )

        row = coord_db.execute(
            "SELECT uat_state, uat_reason FROM assignments WHERE assignment_id='aid-1'"
        ).fetchone()
        assert row["uat_state"] == "failed"
        assert row["uat_reason"] == "logo is cropped"

    def test_clearing_verdict_sets_null(self, coord_db) -> None:
        self._seed_assignment(coord_db)
        record_uat_verdict(assignment_id="aid-1", uat_state="passed")
        record_uat_verdict(assignment_id="aid-1", uat_state=None)

        row = coord_db.execute(
            "SELECT uat_state FROM assignments WHERE assignment_id='aid-1'"
        ).fetchone()
        assert row["uat_state"] is None

    def test_failed_verdict_adds_issue_context_entry(self, coord_db) -> None:
        # #2687: "a --failed verdict should read as actionable feedback on
        # the PR the same way a failed test verdict does" — carried into
        # the per-issue digest exactly like a failed Test-gate verdict is.
        self._seed_assignment(coord_db)
        record_uat_verdict(
            assignment_id="aid-1", uat_state="failed", uat_reason="logo is cropped",
        )

        rows = coord_db.execute(
            "SELECT body, source FROM issue_context WHERE repo_name='api' AND issue_number=1"
        ).fetchall()
        assert any(
            r["source"] == "uat" and "logo is cropped" in r["body"] for r in rows
        )

    def test_passed_verdict_round_trips_through_build_board(self, coord_db) -> None:
        from coord.state import build_board

        self._seed_assignment(coord_db)
        record_uat_verdict(assignment_id="aid-1", uat_state="passed")

        board = build_board()
        row = next(a for a in board.active if a.assignment_id == "aid-1")
        assert row.uat_state == "passed"

    def test_uat_state_excluded_from_whole_board_upsert(self, coord_db) -> None:
        """Mirrors test_state's #1482 exclusion: a stale in-memory
        `save_board()` snapshot (no `uat_state` known to it — every
        `Assignment` defaults to `None`) must never clobber a verdict
        already recorded through the dedicated seam writer."""
        from coord.models import Assignment, Board
        from coord.state import build_board, save_board

        self._seed_assignment(coord_db)
        record_uat_verdict(assignment_id="aid-1", uat_state="passed")

        stale_snapshot = Board(active=[
            Assignment(
                machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
                assignment_id="aid-1", branch="worker/aid-1", uat_state=None,
            ),
        ])
        save_board(stale_snapshot)

        board = build_board()
        row = next(a for a in board.active if a.assignment_id == "aid-1")
        assert row.uat_state == "passed"


class TestVerdictSourceRoundTrip:
    """#1956: `verdict_source`/`verdict_source_reason` round-trip through the
    whole-board upsert (`save_board` -> `_UPSERT_SQL`/`_assignment_upsert_params`
    -> `build_board` -> `row_to_assignment`) — the write path #1956's
    `coord.auto_loop` override stamps into, and every other whole-board saver
    (the periodic reconcile ticks chief among them) shares."""

    def test_round_trips_through_save_and_build_board(self, coord_db) -> None:
        from coord.models import Assignment, Board
        from coord.state import build_board, save_board

        review = Assignment(
            machine_name="precision",
            repo_name="api",
            issue_number=42,
            issue_title="[review] fix the thing",
            assignment_id="rev-vs-1",
            type="review",
            status="done",
            review_verdict="approve",
            verdict_source="overridden",
            verdict_source_reason="#476 approve-with-nits gate: blocking=0",
            dispatched_at=1.0,
            finished_at=2.0,
        )
        save_board(Board(active=[], completed=[review]))

        board = build_board()
        row = next(a for a in board.completed if a.assignment_id == "rev-vs-1")
        assert row.verdict_source == "overridden"
        assert row.verdict_source_reason == "#476 approve-with-nits gate: blocking=0"

    def test_once_set_a_later_upsert_without_it_does_not_erase_it(
        self, coord_db,
    ) -> None:
        """Mirrors review_verdict_original's own COALESCE-preserve guard
        (#1456): a later whole-board save from a path that doesn't know
        about provenance (agent reload, thin-client round-trip) must not
        silently erase a value already recorded."""
        from coord.models import Assignment, Board
        from coord.state import build_board, save_board

        review = Assignment(
            machine_name="precision",
            repo_name="api",
            issue_number=42,
            issue_title="[review] fix the thing",
            assignment_id="rev-vs-2",
            type="review",
            status="done",
            review_verdict="approve",
            verdict_source="recovered",
            verdict_source_reason="header missing, recovered from transcript",
            dispatched_at=1.0,
            finished_at=2.0,
        )
        save_board(Board(active=[], completed=[review]))

        # A later save of the SAME row with no provenance set (e.g. a stale
        # snapshot from a code path predating #1956).
        stale = Assignment(
            machine_name="precision",
            repo_name="api",
            issue_number=42,
            issue_title="[review] fix the thing",
            assignment_id="rev-vs-2",
            type="review",
            status="done",
            review_verdict="approve",
            dispatched_at=1.0,
            finished_at=2.0,
        )
        save_board(Board(active=[], completed=[stale]))

        board = build_board()
        row = next(a for a in board.completed if a.assignment_id == "rev-vs-2")
        assert row.verdict_source == "recovered"
        assert row.verdict_source_reason == "header missing, recovered from transcript"


class TestLoadReviewAssignmentsMissingCost:
    """#2476: coord.state.load_review_assignments_missing_cost — feeds the
    `coord backfill-review-cost` one-shot repair."""

    def _review(self, aid: str, *, status: str, cost_usd: float | None = None):
        from coord.models import Assignment

        return Assignment(
            machine_name="laptop", repo_name="api", issue_number=42,
            issue_title="[review] fix", assignment_id=aid, type="review",
            status=status, dispatched_at=1.0, finished_at=2.0,
            cost_usd=cost_usd,
        )

    def test_finds_terminal_review_with_null_cost(self, coord_db) -> None:
        from coord.models import Board
        from coord.state import load_review_assignments_missing_cost, save_board

        save_board(Board(active=[], completed=[self._review("m1", status="done")]))

        rows = load_review_assignments_missing_cost()
        assert [r["assignment_id"] for r in rows] == ["m1"]

    def test_excludes_row_that_already_has_cost(self, coord_db) -> None:
        from coord.models import Board
        from coord.state import load_review_assignments_missing_cost, save_board

        save_board(Board(
            active=[], completed=[self._review("m2", status="done", cost_usd=1.5)],
        ))

        rows = load_review_assignments_missing_cost()
        assert rows == []

    def test_excludes_zero_cost_row_too(self, coord_db) -> None:
        """cost_usd=0.0 is exactly as uncaptured as NULL — must still be a
        candidate (never actually written by the live capture path, which
        only ever writes cost > 0, but a defensive belt-and-suspenders
        check)."""
        from coord.models import Board
        from coord.state import load_review_assignments_missing_cost, save_board

        save_board(Board(
            active=[], completed=[self._review("m3", status="done", cost_usd=0.0)],
        ))

        rows = load_review_assignments_missing_cost()
        assert [r["assignment_id"] for r in rows] == ["m3"]

    def test_excludes_running_pending_and_finalizing(self, coord_db) -> None:
        """A still-in-flight or not-yet-promoted review has no final cost to
        recover yet — those are for the live path (or a later backfill run),
        not this one-shot repair."""
        from coord.models import Board
        from coord.state import load_review_assignments_missing_cost, save_board

        save_board(Board(active=[
            self._review("running1", status="running"),
            self._review("pending1", status="pending"),
            self._review("finalizing1", status="finalizing"),
        ], completed=[]))

        rows = load_review_assignments_missing_cost()
        assert rows == []

    def test_includes_failed_and_advisory(self, coord_db) -> None:
        """Cost capture is independent of whether the review's findings post
        succeeded — a failed/advisory review still spent real money and
        should still be recovered."""
        from coord.models import Board
        from coord.state import load_review_assignments_missing_cost, save_board

        save_board(Board(active=[], completed=[
            self._review("failed1", status="failed"),
            self._review("adv1", status="advisory"),
        ]))

        rows = {r["assignment_id"] for r in load_review_assignments_missing_cost()}
        assert rows == {"failed1", "adv1"}

    def test_filters_by_repo(self, coord_db) -> None:
        from coord.models import Assignment, Board
        from coord.state import load_review_assignments_missing_cost, save_board

        other_repo = Assignment(
            machine_name="laptop", repo_name="other", issue_number=1,
            issue_title="[review] fix", assignment_id="other1", type="review",
            status="done", dispatched_at=1.0, finished_at=2.0,
        )
        save_board(Board(
            active=[], completed=[self._review("api1", status="done"), other_repo],
        ))

        rows = load_review_assignments_missing_cost(repo_name="api")
        assert [r["assignment_id"] for r in rows] == ["api1"]


# ── #2597: cost/token capture rides out transient lock contention ──────────


class _FlakyConnProxy:
    """Wraps a real sqlite3 connection and makes its first *fail_times*
    ``execute()`` calls raise ``database is locked`` before delegating to
    the real connection — simulates a momentary collision with a concurrent
    writer without needing a genuine second OS-level connection against the
    in-memory ``coord_db`` fixture (which, being ``:memory:``, has no
    cross-connection contention to hold in the first place).

    #2726: the writers below now go through ``coord.sql.execute()``, which
    calls ``conn.cursor()`` then ``cursor.execute()`` rather than the
    sqlite3 connection-level ``.execute()`` shortcut — so ``cursor()`` must
    be implemented too, routed through a proxy cursor (below) that keeps
    the same counting/raising behavior. ``__module__`` is pinned to
    ``"sqlite3"`` so ``coord.sql.detect_dialect`` (keyed off
    ``type(conn).__module__``) recognizes this fake as SQLite instead of
    raising ``UnsupportedDialectError``.
    """

    __module__ = "sqlite3"

    def __init__(self, real_conn, fail_times: int) -> None:
        self._real = real_conn
        self._fail_times = fail_times
        self.calls = 0

    def execute(self, *args, **kwargs):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise sqlite3.OperationalError("database is locked")
        return self._real.execute(*args, **kwargs)

    def cursor(self):
        return _FlakyCursorProxy(self)

    def __getattr__(self, name):
        return getattr(self._real, name)


class _FlakyCursorProxy:
    """The cursor ``coord.sql.execute()``'s ``conn.cursor()`` call receives
    from :class:`_FlakyConnProxy` (#2726) — routes ``.execute()`` back
    through the proxy's own counting/raising logic instead of a real,
    unproxied cursor obtained straight from the underlying connection."""

    def __init__(self, proxy: "_FlakyConnProxy") -> None:
        self._proxy = proxy
        self._real_cursor = None

    def execute(self, *args, **kwargs):
        self._real_cursor = self._proxy.execute(*args, **kwargs)
        return self

    def __getattr__(self, name):
        return getattr(self._real_cursor, name)


def _dispatch_for_cost(coord_db, assignment_id: str = "aid-cost") -> None:
    proposal = Proposal(
        id=1, machine_name="laptop", repo_name="api", issue_number=7,
        issue_title="Fix thing", rationale="", briefing="Fix it",
    )
    record_dispatched(
        assignment_id=assignment_id, proposal=proposal, repo_github="acme/api",
    )


class TestUpdateAssignmentCostRetriesLockContention:
    """#2597: this write previously had no retry protection at all — a
    momentary lock collision (a concurrent writer holding the DB for a
    beat) raised straight out to `coord.notify._capture_cost`, which logs
    and swallows it, silently understating recorded spend for that
    assignment. Now rides out a transient collision the same way every
    other load-bearing write in this codebase does."""

    def test_retries_transient_contention_then_succeeds(
        self, coord_db, monkeypatch
    ) -> None:
        from coord.state import _update_assignment_cost_local

        _dispatch_for_cost(coord_db, "aid-cost-1")
        proxy = _FlakyConnProxy(coord_db, fail_times=2)
        monkeypatch.setattr("coord.state.get_connection", lambda: proxy)
        monkeypatch.setattr("coord.db.time.sleep", lambda s: None)

        _update_assignment_cost_local("aid-cost-1", 1.23)

        assert proxy.calls == 3
        row = coord_db.execute(
            "SELECT cost_usd FROM assignments WHERE assignment_id=?",
            ("aid-cost-1",),
        ).fetchone()
        assert row["cost_usd"] == 1.23

    def test_propagates_once_retry_budget_is_exhausted(
        self, coord_db, monkeypatch
    ) -> None:
        """Sustained (not momentary) contention still surfaces as an
        `OperationalError` to the caller — unchanged from before #2597,
        which only added the retry, not a new swallow. `_capture_cost`'s
        own try/except (coord/notify.py) is the existing best-effort net
        for this case."""
        from coord.state import _update_assignment_cost_local

        _dispatch_for_cost(coord_db, "aid-cost-2")
        proxy = _FlakyConnProxy(coord_db, fail_times=999)
        monkeypatch.setattr("coord.state.get_connection", lambda: proxy)
        monkeypatch.setattr("coord.db.time.sleep", lambda s: None)

        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            _update_assignment_cost_local("aid-cost-2", 1.23)


class TestUpdateAssignmentTokensRetriesLockContention:
    """#2597: mirrors TestUpdateAssignmentCostRetriesLockContention — before
    this, EVERY OperationalError (a missing pre-migration column, or lock
    contention) was silently swallowed with no retry at all."""

    def test_retries_transient_contention_then_succeeds(
        self, coord_db, monkeypatch
    ) -> None:
        from coord.state import _update_assignment_tokens_local

        _dispatch_for_cost(coord_db, "aid-tok-1")
        proxy = _FlakyConnProxy(coord_db, fail_times=2)
        monkeypatch.setattr("coord.state.get_connection", lambda: proxy)
        monkeypatch.setattr("coord.db.time.sleep", lambda s: None)

        _update_assignment_tokens_local(
            "aid-tok-1", input_tokens=10, output_tokens=20,
            cache_creation_tokens=0, cache_read_tokens=0,
        )

        assert proxy.calls == 3
        row = coord_db.execute(
            "SELECT input_tokens, output_tokens FROM assignments "
            "WHERE assignment_id=?",
            ("aid-tok-1",),
        ).fetchone()
        assert row["input_tokens"] == 10
        assert row["output_tokens"] == 20

    def test_stays_silent_once_retry_budget_is_exhausted(
        self, coord_db, monkeypatch
    ) -> None:
        """Preserves this function's pre-#2597 best-effort contract (its
        docstring: "Silently swallows OperationalError") for the case that
        contract was written for — now also covering a lock that outlasts
        the retry budget, not just a genuinely missing column."""
        from coord.state import _update_assignment_tokens_local

        _dispatch_for_cost(coord_db, "aid-tok-2")
        proxy = _FlakyConnProxy(coord_db, fail_times=999)
        monkeypatch.setattr("coord.state.get_connection", lambda: proxy)
        monkeypatch.setattr("coord.db.time.sleep", lambda s: None)

        # Must not raise.
        _update_assignment_tokens_local(
            "aid-tok-2", input_tokens=10, output_tokens=20,
            cache_creation_tokens=0, cache_read_tokens=0,
        )


class TestUpdateAssignmentTokensPersistsNumTurns:
    """#2786: `num_turns` rides the same write as the four token columns —
    parsed off the same final `result` event (`WorkerSummary.num_turns`,
    coord/worker_events.py) but previously never reached the DB at all."""

    def test_local_write_persists_num_turns(self, coord_db) -> None:
        from coord.state import _update_assignment_tokens_local

        _dispatch_for_cost(coord_db, "aid-turns-1")
        _update_assignment_tokens_local(
            "aid-turns-1", input_tokens=10_000, output_tokens=5_000,
            cache_creation_tokens=100_000, cache_read_tokens=11_300_000,
            num_turns=87,
        )
        row = coord_db.execute(
            "SELECT num_turns, cache_read_tokens FROM assignments WHERE assignment_id=?",
            ("aid-turns-1",),
        ).fetchone()
        assert row["num_turns"] == 87
        assert row["cache_read_tokens"] == 11_300_000

    def test_public_wrapper_persists_num_turns(self, coord_db) -> None:
        """`update_assignment_tokens` (the notify.py/reconcile.py-facing
        wrapper) threads `num_turns` through to the same local write when no
        board service is configured."""
        from coord.state import update_assignment_tokens

        _dispatch_for_cost(coord_db, "aid-turns-2")
        update_assignment_tokens(
            "aid-turns-2", input_tokens=1, output_tokens=1,
            cache_creation_tokens=1, cache_read_tokens=1, num_turns=42,
        )
        row = coord_db.execute(
            "SELECT num_turns FROM assignments WHERE assignment_id=?",
            ("aid-turns-2",),
        ).fetchone()
        assert row["num_turns"] == 42

    def test_a_row_predating_the_column_reads_zero_not_null(self, coord_db) -> None:
        """No backfill for historical rows (explicitly out of scope) — a
        never-written row must read as 0, the same convention the four
        token columns already use, not NULL."""
        _dispatch_for_cost(coord_db, "aid-turns-3")
        row = coord_db.execute(
            "SELECT num_turns FROM assignments WHERE assignment_id=?",
            ("aid-turns-3",),
        ).fetchone()
        assert row["num_turns"] == 0

    def test_all_zero_call_is_a_no_op(self, coord_db) -> None:
        """The existing guard (interactive/Max sessions produce no per-token
        data and must not overwrite 0 with 0) still applies when every
        argument, including `num_turns`, is 0 — no write is attempted at
        all, not even one that sets num_turns to the same 0 it already is."""
        from coord.state import _update_assignment_tokens_local

        _dispatch_for_cost(coord_db, "aid-turns-4")
        _update_assignment_tokens_local("aid-turns-4", num_turns=0)
        row = coord_db.execute(
            "SELECT num_turns FROM assignments WHERE assignment_id=?",
            ("aid-turns-4",),
        ).fetchone()
        assert row["num_turns"] == 0

    def test_num_turns_alone_is_enough_to_pass_the_guard(self, coord_db) -> None:
        """A non-zero `num_turns` with all four token counts at 0 still
        passes the "is there anything real to write" guard — the guard's
        job is to reject an all-zero, no-signal call, not to require tokens
        specifically."""
        from coord.state import _update_assignment_tokens_local

        _dispatch_for_cost(coord_db, "aid-turns-5")
        _update_assignment_tokens_local("aid-turns-5", num_turns=3)
        row = coord_db.execute(
            "SELECT num_turns FROM assignments WHERE assignment_id=?",
            ("aid-turns-5",),
        ).fetchone()
        assert row["num_turns"] == 3


# ── #2689: issue-tracker seam writes ride out transient lock contention,
# and never fail the call once the (irreversible) upstream GitHub write has
# already landed ─────────────────────────────────────────────────────────


class _BrokenConn:
    """A connection whose every ``execute()`` raises a non-contention
    `OperationalError` — simulates a real bug (schema drift, a typo in
    hand-written SQL) rather than transient lock contention, so it must
    surface immediately instead of being retried or swallowed.

    #2726: ``cursor()`` returns ``self`` so ``coord.sql.execute()``'s
    ``conn.cursor().execute()`` call still reaches this same raise;
    ``__module__`` is pinned to ``"sqlite3"`` so ``coord.sql.detect_dialect``
    recognizes this fake as SQLite instead of raising
    ``UnsupportedDialectError`` before the intended error ever fires.
    """

    __module__ = "sqlite3"

    def cursor(self):
        return self

    def execute(self, *args, **kwargs):
        raise sqlite3.OperationalError("no such column: bogus")


class TestCreateIssueLocalLockContention:
    """`_create_issue_local` does an irreversible GitHub write (the issue
    now exists) followed by a local cache-mirror write. Before #2689, any
    `OperationalError` on that second write — including transient lock
    contention from a concurrent writer elsewhere in the daemon — propagated
    out as a 503. The natural response (retry) filed a duplicate GitHub
    issue, because nothing told the caller the first call had actually
    succeeded."""

    @staticmethod
    def _mock_create_issue(monkeypatch, calls: list, number: int = 99) -> None:
        def _fake(repo, title, body, *, labels=None):
            calls.append((repo, title, body, labels))
            return {"number": number, "url": f"https://github.com/{repo}/issues/{number}"}

        monkeypatch.setattr("coord.github_ops.create_issue", _fake)

    def test_retries_transient_contention_then_returns_result(
        self, coord_db, monkeypatch
    ) -> None:
        from coord.state import _create_issue_local

        calls: list = []
        self._mock_create_issue(monkeypatch, calls)
        proxy = _FlakyConnProxy(coord_db, fail_times=2)
        monkeypatch.setattr("coord.state.get_connection", lambda: proxy)
        monkeypatch.setattr("coord.db.time.sleep", lambda s: None)

        result = _create_issue_local(
            "api", "new issue", "body", repo_github="acme/api"
        )

        assert result == {"number": 99, "url": "https://github.com/acme/api/issues/99"}
        assert len(calls) == 1, "GitHub create_issue must be called exactly once"
        assert proxy.calls == 3

    def test_returns_result_without_raising_once_retry_budget_is_exhausted(
        self, coord_db, monkeypatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The duplicate-filing regression, asserted directly: even when the
        cache mirror never lands, the caller must see the created issue back
        — not a 503 that reads as "nothing happened" and invites a retry
        that creates a second issue on GitHub."""
        from coord.state import _create_issue_local

        calls: list = []
        self._mock_create_issue(monkeypatch, calls)
        proxy = _FlakyConnProxy(coord_db, fail_times=999)
        monkeypatch.setattr("coord.state.get_connection", lambda: proxy)
        monkeypatch.setattr("coord.db.time.sleep", lambda s: None)

        with caplog.at_level("ERROR", logger="coord.state"):
            result = _create_issue_local(
                "api", "new issue", "body", repo_github="acme/api"
            )

        assert result == {"number": 99, "url": "https://github.com/acme/api/issues/99"}
        assert len(calls) == 1, "GitHub create_issue must be called exactly once"
        assert any("2689" in rec.message for rec in caplog.records)

    def test_raises_on_non_contention_operational_error(
        self, coord_db, monkeypatch
    ) -> None:
        """A genuine bug in the hand-written cache-mirror SQL (schema drift,
        a typo) must still surface — only lock contention is absorbed."""
        from coord.state import _create_issue_local

        calls: list = []
        self._mock_create_issue(monkeypatch, calls)
        monkeypatch.setattr("coord.state.get_connection", lambda: _BrokenConn())

        with pytest.raises(sqlite3.OperationalError, match="no such column"):
            _create_issue_local("api", "new issue", "body", repo_github="acme/api")
        assert len(calls) == 1, "the GitHub write already happened before the raise"


class TestEditIssueContentLocalLockContention:
    """Mirrors TestCreateIssueLocalLockContention for `_edit_issue_content_local`
    — the sibling the #2689 issue names as already sharing this exact shape."""

    @staticmethod
    def _mock_edit_issue(monkeypatch, calls: list) -> None:
        def _fake(repo, issue_number, *, title=None, body=None):
            calls.append((repo, issue_number, title, body))

        monkeypatch.setattr("coord.github_ops.edit_issue", _fake)

    def test_retries_transient_contention_then_returns_true(
        self, coord_db, monkeypatch
    ) -> None:
        from coord.state import _edit_issue_content_local

        calls: list = []
        self._mock_edit_issue(monkeypatch, calls)
        proxy = _FlakyConnProxy(coord_db, fail_times=2)
        monkeypatch.setattr("coord.state.get_connection", lambda: proxy)
        monkeypatch.setattr("coord.db.time.sleep", lambda s: None)

        result = _edit_issue_content_local(
            "api", 9, title="new title", repo_github="acme/api"
        )

        assert result is True
        assert calls == [("acme/api", 9, "new title", None)]
        assert proxy.calls == 3

    def test_returns_true_without_raising_once_retry_budget_is_exhausted(
        self, coord_db, monkeypatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        from coord.state import _edit_issue_content_local

        calls: list = []
        self._mock_edit_issue(monkeypatch, calls)
        proxy = _FlakyConnProxy(coord_db, fail_times=999)
        monkeypatch.setattr("coord.state.get_connection", lambda: proxy)
        monkeypatch.setattr("coord.db.time.sleep", lambda s: None)

        with caplog.at_level("ERROR", logger="coord.state"):
            result = _edit_issue_content_local(
                "api", 9, title="new title", repo_github="acme/api"
            )

        assert result is True
        assert len(calls) == 1, "the GitHub edit must not be retried"
        assert any("2689" in rec.message for rec in caplog.records)

    def test_raises_on_non_contention_operational_error(
        self, coord_db, monkeypatch
    ) -> None:
        from coord.state import _edit_issue_content_local

        calls: list = []
        self._mock_edit_issue(monkeypatch, calls)
        monkeypatch.setattr("coord.state.get_connection", lambda: _BrokenConn())

        with pytest.raises(sqlite3.OperationalError, match="no such column"):
            _edit_issue_content_local(
                "api", 9, title="new title", repo_github="acme/api"
            )
        assert len(calls) == 1, "the GitHub write already happened before the raise"


class TestApplyIssueLabelsLocalLockContention:
    """Same shape as `_create_issue_local`: `github_ops.change_issue_labels`
    is the irreversible upstream write; the cache mirror (via
    `_update_issue_labels_local`) must not fail the call once it has
    landed."""

    def test_retries_transient_contention_then_returns_labels(
        self, coord_db, monkeypatch
    ) -> None:
        from coord.state import _apply_issue_labels_local

        monkeypatch.setattr(
            "coord.github_ops.change_issue_labels",
            lambda repo, num, *, add, remove: (["bug", "coord"], True),
        )
        proxy = _FlakyConnProxy(coord_db, fail_times=2)
        monkeypatch.setattr("coord.state.get_connection", lambda: proxy)
        monkeypatch.setattr("coord.db.time.sleep", lambda s: None)

        new_labels, changed = _apply_issue_labels_local(
            "api", 9, add={"coord"}, remove=set(), repo_github="acme/api"
        )

        assert (new_labels, changed) == (["bug", "coord"], True)
        assert proxy.calls == 3

    def test_returns_labels_without_raising_once_retry_budget_is_exhausted(
        self, coord_db, monkeypatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        from coord.state import _apply_issue_labels_local

        monkeypatch.setattr(
            "coord.github_ops.change_issue_labels",
            lambda repo, num, *, add, remove: (["bug", "coord"], True),
        )
        proxy = _FlakyConnProxy(coord_db, fail_times=999)
        monkeypatch.setattr("coord.state.get_connection", lambda: proxy)
        monkeypatch.setattr("coord.db.time.sleep", lambda s: None)

        with caplog.at_level("ERROR", logger="coord.state"):
            new_labels, changed = _apply_issue_labels_local(
                "api", 9, add={"coord"}, remove=set(), repo_github="acme/api"
            )

        assert (new_labels, changed) == (["bug", "coord"], True)
        # #2846: the guard moved into `_update_issue_labels_local` itself
        # (shared with the `/issue-labels` plural endpoint's direct caller),
        # so the log line it emits on exhaustion now cites #2846.
        assert any("2846" in rec.message for rec in caplog.records)


class TestUpdateIssueLabelsLocalDirectCallLockContention:
    """#2846: unlike `_apply_issue_labels_local` (`/issue-label`, already
    guarded by #2689), the `/issue-labels` plural endpoint calls
    `_update_issue_labels_local` DIRECTLY — its own frame had no guard at
    all before #2846. The GitHub label change it mirrors already happened
    (by the caller's own contract, see `update_issue_labels`'s docstring),
    so a lock outlasting the retry budget must not raise past this
    function either, exercised here with no `_apply_issue_labels_local` in
    the call stack at all."""

    def test_retries_transient_contention_then_returns_true(
        self, coord_db, monkeypatch
    ) -> None:
        from coord.state import _update_issue_labels_local

        coord_db.execute(
            "INSERT INTO issues (repo_name, number, title, body, state, labels, "
            "synced_at) VALUES ('api', 9, 't', 'b', 'open', '[]', 0)"
        )
        coord_db.commit()
        proxy = _FlakyConnProxy(coord_db, fail_times=2)
        monkeypatch.setattr("coord.state.get_connection", lambda: proxy)
        monkeypatch.setattr("coord.db.time.sleep", lambda s: None)

        updated = _update_issue_labels_local("api", 9, ["bug", "coord"])

        assert updated is True
        assert proxy.calls == 3

    def test_returns_false_without_raising_once_retry_budget_is_exhausted(
        self, coord_db, monkeypatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        from coord.state import _update_issue_labels_local

        proxy = _FlakyConnProxy(coord_db, fail_times=999)
        monkeypatch.setattr("coord.state.get_connection", lambda: proxy)
        monkeypatch.setattr("coord.db.time.sleep", lambda s: None)

        with caplog.at_level("ERROR", logger="coord.state"):
            updated = _update_issue_labels_local("api", 9, ["bug"])

        assert updated is False
        assert any("2846" in rec.message for rec in caplog.records)


class TestRecordIssueCommentCaptureLocalLockContention:
    """#2846: `_record_issue_comment_capture_local` is reached two ways —
    directly from the daemon's own `/issue-comment` route (via
    `_comment_on_issue_local` -> `github_ops.post_issue_comment`), and
    cross-module from ANY caller of `github_ops.post_issue_comment`
    (`_capture_comment_write`'s fail-open wrapper previously swallowed a
    lock immediately, with zero retry — this both retries first, matching
    every neighbouring mirror write, and covers the direct-HTTP-capture
    path via `/issue-comments`'s `capture` action, which had no fail-open
    net at all). The GitHub comment this mirrors has already posted by the
    time this runs."""

    def test_retries_transient_contention_then_writes_row(
        self, coord_db, monkeypatch
    ) -> None:
        from coord.state import _record_issue_comment_capture_local

        proxy = _FlakyConnProxy(coord_db, fail_times=2)
        monkeypatch.setattr("coord.state.get_connection", lambda: proxy)
        monkeypatch.setattr("coord.db.time.sleep", lambda s: None)

        _record_issue_comment_capture_local(
            repo_name="api",
            issue_number=9,
            body="a comment",
            gh_comment_id=555,
            author="bot",
        )

        assert proxy.calls == 3
        row = coord_db.execute(
            "SELECT body FROM issue_comments WHERE gh_comment_id = 555"
        ).fetchone()
        assert row["body"] == "a comment"

    def test_swallows_without_raising_once_retry_budget_is_exhausted(
        self, coord_db, monkeypatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        from coord.state import _record_issue_comment_capture_local

        proxy = _FlakyConnProxy(coord_db, fail_times=999)
        monkeypatch.setattr("coord.state.get_connection", lambda: proxy)
        monkeypatch.setattr("coord.db.time.sleep", lambda s: None)

        with caplog.at_level("ERROR", logger="coord.state"):
            _record_issue_comment_capture_local(
                repo_name="api", issue_number=9, body="a comment", gh_comment_id=556,
            )

        assert any("2846" in rec.message for rec in caplog.records)
        row = coord_db.execute(
            "SELECT body FROM issue_comments WHERE gh_comment_id = 556"
        ).fetchone()
        assert row is None, "the row never landed -- exhaustion must not raise, not fake success"


class TestDriveQueueLocalWritesRetryLockContention:
    """#2846: the four `/drive-queue` local writers issued bare `sql.execute`
    calls with zero retry, unlike every neighbouring write in this module —
    a transient `database is locked` collision (a concurrent tick or a
    second operator action landing at the same moment) used to propagate
    straight out as a 503.

    Unlike the cache-mirror sites above, these writes are the primary
    record (there is no separate "it already happened on GitHub" fact to
    protect) and are idempotent by natural key (upsert-by-`(repo_name,
    issue_number)`, delete-by-key, update-by-key) — so extending the retry
    budget via `retry_on_locked` is pure upside, and a genuinely exhausted
    retry budget still surfaces a real error rather than fabricating
    success for a write that did not happen."""

    def test_enqueue_retries_transient_contention_then_succeeds(
        self, coord_db, monkeypatch
    ) -> None:
        from coord.state import _enqueue_drive_queue_local

        proxy = _FlakyConnProxy(coord_db, fail_times=2)
        monkeypatch.setattr("coord.state.get_connection", lambda: proxy)
        monkeypatch.setattr("coord.db.time.sleep", lambda s: None)

        entry_id = _enqueue_drive_queue_local("api", 9)

        assert isinstance(entry_id, int)
        # enqueue issues more than one statement per attempt (a lookup then
        # an insert/update), so the exact count depends on how many of that
        # attempt's statements land before the injected failure -- what
        # matters is that it retried at all rather than raising on the
        # first collision.
        assert proxy.calls > 2

    def test_enqueue_propagates_once_retry_budget_is_exhausted(
        self, coord_db, monkeypatch
    ) -> None:
        from coord.state import _enqueue_drive_queue_local

        proxy = _FlakyConnProxy(coord_db, fail_times=999)
        monkeypatch.setattr("coord.state.get_connection", lambda: proxy)
        monkeypatch.setattr("coord.db.time.sleep", lambda s: None)

        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            _enqueue_drive_queue_local("api", 9)

    def test_dequeue_retries_transient_contention_then_succeeds(
        self, coord_db, monkeypatch
    ) -> None:
        from coord.state import _dequeue_drive_queue_local, _enqueue_drive_queue_local

        _enqueue_drive_queue_local("api", 9)
        proxy = _FlakyConnProxy(coord_db, fail_times=2)
        monkeypatch.setattr("coord.state.get_connection", lambda: proxy)
        monkeypatch.setattr("coord.db.time.sleep", lambda s: None)

        removed = _dequeue_drive_queue_local("api", 9)

        assert removed is True
        # dequeue issues more than one statement per attempt when a row is
        # actually removed (the delete, then a renumber pass) -- see the
        # enqueue test above for why this isn't an exact count.
        assert proxy.calls > 2

    def test_update_retries_transient_contention_then_succeeds(
        self, coord_db, monkeypatch
    ) -> None:
        from coord.state import _enqueue_drive_queue_local, _update_drive_queue_entry_local

        _enqueue_drive_queue_local("api", 9)
        proxy = _FlakyConnProxy(coord_db, fail_times=2)
        monkeypatch.setattr("coord.state.get_connection", lambda: proxy)
        monkeypatch.setattr("coord.db.time.sleep", lambda s: None)

        updated = _update_drive_queue_entry_local("api", 9, state="running")

        assert updated is True
        assert proxy.calls == 3

    def test_move_retries_transient_contention_then_succeeds(
        self, coord_db, monkeypatch
    ) -> None:
        from coord.state import _enqueue_drive_queue_local, _move_drive_queue_entry_local

        _enqueue_drive_queue_local("api", 9)
        _enqueue_drive_queue_local("api", 10)
        proxy = _FlakyConnProxy(coord_db, fail_times=2)
        monkeypatch.setattr("coord.state.get_connection", lambda: proxy)
        monkeypatch.setattr("coord.db.time.sleep", lambda s: None)

        moved = _move_drive_queue_entry_local("api", 10, 0)

        assert moved is True
        # move issues one statement per queue row being renumbered -- see
        # the enqueue test above for why this isn't an exact count.
        assert proxy.calls > 2

    def test_enqueue_with_position_survives_move_retry_exhaustion(
        self, coord_db, monkeypatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """#2846 follow-up: `_enqueue_drive_queue_local(..., position=N)`
        commits the row write (its own `retry_on_locked`-guarded
        transaction) and only then calls `_move_drive_queue_entry_local` in
        a *separate* transaction. If that second call's own retry budget is
        exhausted by sustained contention, the enqueue must still report
        success -- the row already landed durably, so a 503 here would be
        the exact "already happened, told you it didn't" shape #2846 closed
        for the cache-mirror sites. The position not landing self-heals on
        the next explicit move/renumber."""
        from coord import state
        from coord.state import _enqueue_drive_queue_local, _get_drive_queue_entry_local

        def _always_locked(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(state, "_move_drive_queue_entry_local", _always_locked)

        with caplog.at_level("ERROR", logger="coord.state"):
            entry_id = _enqueue_drive_queue_local("api", 9, position=1)

        assert isinstance(entry_id, int)  # must not raise
        # The enqueue itself is durable even though the position move failed.
        assert _get_drive_queue_entry_local("api", 9) is not None
        assert any("2846" in rec.message for rec in caplog.records)

    def test_enqueue_with_position_propagates_non_lock_move_errors(
        self, coord_db, monkeypatch
    ) -> None:
        """A genuine (non-contention) error out of the move step is a real
        bug, not transient contention -- it must still surface, not be
        swallowed alongside the lock-contention case."""
        from coord import state
        from coord.state import _enqueue_drive_queue_local

        def _boom(*args, **kwargs):
            raise sqlite3.OperationalError("no such table: drive_queue")

        monkeypatch.setattr(state, "_move_drive_queue_entry_local", _boom)

        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            _enqueue_drive_queue_local("api", 9, position=1)


class TestMilestoneLocalLockContention:
    """Same shape, for `_assign_issue_milestone_local` /
    `_unassign_issue_milestone_local`."""

    def test_assign_retries_transient_contention_without_raising(
        self, coord_db, monkeypatch
    ) -> None:
        from coord.state import _assign_issue_milestone_local

        monkeypatch.setattr("coord.github_ops.assign_issue_milestone", lambda *a, **k: None)
        proxy = _FlakyConnProxy(coord_db, fail_times=2)
        monkeypatch.setattr("coord.state.get_connection", lambda: proxy)
        monkeypatch.setattr("coord.db.time.sleep", lambda s: None)

        _assign_issue_milestone_local(
            "api", 9, 3, milestone_title="v1", repo_github="acme/api"
        )  # must not raise

        assert proxy.calls == 3

    def test_assign_does_not_raise_once_retry_budget_is_exhausted(
        self, coord_db, monkeypatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        from coord.state import _assign_issue_milestone_local

        monkeypatch.setattr("coord.github_ops.assign_issue_milestone", lambda *a, **k: None)
        proxy = _FlakyConnProxy(coord_db, fail_times=999)
        monkeypatch.setattr("coord.state.get_connection", lambda: proxy)
        monkeypatch.setattr("coord.db.time.sleep", lambda s: None)

        with caplog.at_level("ERROR", logger="coord.state"):
            _assign_issue_milestone_local(
                "api", 9, 3, milestone_title="v1", repo_github="acme/api"
            )  # must not raise
        assert any("2689" in rec.message for rec in caplog.records)

    def test_unassign_does_not_raise_once_retry_budget_is_exhausted(
        self, coord_db, monkeypatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        from coord.state import _unassign_issue_milestone_local

        monkeypatch.setattr("coord.github_ops.unassign_issue_milestone", lambda *a, **k: None)
        proxy = _FlakyConnProxy(coord_db, fail_times=999)
        monkeypatch.setattr("coord.state.get_connection", lambda: proxy)
        monkeypatch.setattr("coord.db.time.sleep", lambda s: None)

        with caplog.at_level("ERROR", logger="coord.state"):
            _unassign_issue_milestone_local(
                "api", 9, repo_github="acme/api"
            )  # must not raise
        assert any("2689" in rec.message for rec in caplog.records)


class TestMarkReviewPostedLocalLockContention:
    """`_mark_review_posted_local` is pure local bookkeeping — no GitHub
    call inside it — but the review findings it records as "posted" were
    already posted to GitHub by its caller before this runs. This was the
    one confirmed *live* instance of the #2689 bug class (observed firing
    every few minutes on dellserver from `post_orphaned_review_findings`)."""

    def test_retries_transient_contention_without_raising(
        self, coord_db, monkeypatch
    ) -> None:
        from coord.state import _mark_review_posted_local

        proxy = _FlakyConnProxy(coord_db, fail_times=2)
        monkeypatch.setattr("coord.state.get_connection", lambda: proxy)
        monkeypatch.setattr("coord.db.time.sleep", lambda s: None)

        _mark_review_posted_local("aid-does-not-exist")  # must not raise
        # 2 failed UPDATE attempts + 1 successful UPDATE + the follow-up
        # SELECT this function does afterward (for the audit-log lookup).
        assert proxy.calls == 4

    def test_does_not_raise_once_retry_budget_is_exhausted(
        self, coord_db, monkeypatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        from coord.state import _mark_review_posted_local

        proxy = _FlakyConnProxy(coord_db, fail_times=999)
        monkeypatch.setattr("coord.state.get_connection", lambda: proxy)
        monkeypatch.setattr("coord.db.time.sleep", lambda s: None)

        with caplog.at_level("ERROR", logger="coord.state"):
            _mark_review_posted_local("aid-does-not-exist")  # must not raise
        assert any("2689" in rec.message for rec in caplog.records)


# ── #2726: the 10 `INSERT OR REPLACE` sites rewritten to `coord.sql.upsert()`
# (`ON CONFLICT ... DO UPDATE`) ──────────────────────────────────────────────
#
# Every affected table enforces its own PK (`notifications.assignment_id`,
# `plans.assignment_id`, `board_meta.key`), so a plain `INSERT` would already
# raise `IntegrityError` on a second write for the same key — that alone
# doesn't prove the seam rewrite preserved "second write replaces the first
# row in place" semantics (a bug here could just as easily raise
# `IntegrityError` on the second write, or a stray plain `INSERT` could crash
# instead of updating). These pin it directly: write twice with different
# payloads, assert exactly one row survives holding the *second* payload.


class TestUpsertSeamReplacesNotDuplicates:
    def test_mark_notified_second_call_replaces_not_duplicates(
        self, coord_db
    ) -> None:
        from coord.state import mark_notified

        mark_notified("aid-upsert", "plan")
        mark_notified("aid-upsert", "completion", branch="issue-1-fix")

        rows = coord_db.execute(
            "SELECT event, branch FROM notifications WHERE assignment_id=?",
            ("aid-upsert",),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["event"] == "completion"
        assert rows[0]["branch"] == "issue-1-fix"

    def test_save_plan_second_call_replaces_not_duplicates(self, coord_db) -> None:
        from coord.state import save_plan

        save_plan("aid-plan", {"steps": ["a"]})
        save_plan("aid-plan", {"steps": ["a", "b"]})

        rows = coord_db.execute(
            "SELECT plan_data FROM plans WHERE assignment_id=?", ("aid-plan",),
        ).fetchall()
        assert len(rows) == 1
        assert json.loads(rows[0]["plan_data"]) == {"steps": ["a", "b"]}

    def test_save_board_round_number_and_initialized_replace_not_duplicate(
        self, coord_db
    ) -> None:
        from coord.models import Board
        from coord.state import save_board

        save_board(Board(active=[], completed=[], round_number=1))
        save_board(Board(active=[], completed=[], round_number=2))

        rows = coord_db.execute(
            "SELECT key, value FROM board_meta WHERE key IN "
            "('round_number', 'board_initialized')"
        ).fetchall()
        by_key = {r["key"]: r["value"] for r in rows}
        assert by_key == {"round_number": "2", "board_initialized": "1"}

    def test_milestone_drain_register_is_idempotent_one_board_meta_row(
        self, coord_db
    ) -> None:
        from coord.state import list_milestone_drains, register_milestone_drain

        register_milestone_drain(repo_name="api", tracking_issue=1)
        register_milestone_drain(repo_name="api", tracking_issue=1)  # no-op re-register
        register_milestone_drain(repo_name="api", tracking_issue=2)

        rows = coord_db.execute(
            "SELECT value FROM board_meta WHERE key='milestone_drains'"
        ).fetchall()
        assert len(rows) == 1
        assert list_milestone_drains() == [
            {"repo_name": "api", "tracking_issue": 1},
            {"repo_name": "api", "tracking_issue": 2},
        ]

    def test_milestone_gate_save_and_delete_replace_one_board_meta_row(
        self, coord_db
    ) -> None:
        from coord.state import (
            delete_milestone_gate,
            list_milestone_gates,
            save_milestone_gate,
        )

        save_milestone_gate({"repo_name": "api", "tracking_issue": 1, "gate": "A"})
        save_milestone_gate({"repo_name": "api", "tracking_issue": 1, "gate": "B"})

        rows = coord_db.execute(
            "SELECT value FROM board_meta WHERE key='milestone_gates'"
        ).fetchall()
        assert len(rows) == 1
        assert list_milestone_gates() == [
            {"repo_name": "api", "tracking_issue": 1, "gate": "B"}
        ]

        delete_milestone_gate(repo_name="api", tracking_issue=1)
        rows = coord_db.execute(
            "SELECT value FROM board_meta WHERE key='milestone_gates'"
        ).fetchall()
        assert len(rows) == 1, "delete rewrites the same row rather than removing it"
        assert list_milestone_gates() == []

    def test_gate_a_approval_save_replaces_one_board_meta_row(self, coord_db) -> None:
        from coord.state import list_gate_a_approvals, save_gate_a_approval

        save_gate_a_approval(
            {"repo_name": "api", "milestone_number": 1, "verdict": "approve"}
        )
        save_gate_a_approval(
            {"repo_name": "api", "milestone_number": 1, "verdict": "reject"}
        )

        rows = coord_db.execute(
            "SELECT value FROM board_meta WHERE key='gate_a_approvals'"
        ).fetchall()
        assert len(rows) == 1
        assert [a["verdict"] for a in list_gate_a_approvals()] == ["reject"]

    def test_portal_link_save_replaces_one_board_meta_row(self, coord_db) -> None:
        from coord.state import list_portal_links, save_portal_link

        save_portal_link(
            {"repo_name": "api", "milestone_number": 1, "submission_id": "s1"}
        )
        save_portal_link(
            {"repo_name": "api", "milestone_number": 1, "submission_id": "s2"}
        )

        rows = coord_db.execute(
            "SELECT value FROM board_meta WHERE key='portal_links'"
        ).fetchall()
        assert len(rows) == 1
        assert [link["submission_id"] for link in list_portal_links()] == ["s2"]


# ── #2983: swallowed driver errors must leave the connection usable ─────────
#
# `coord.db.get_connection()` is a process-/thread-lived singleton, so these
# handlers do not just "swallow and carry on" for the rest of their own
# function -- on Postgres an un-rolled-back abort takes out every LATER
# statement in the process too, including ones in completely unrelated
# request handlers.


def _abort_conn_with_assignment_rows(
    monkeypatch: pytest.MonkeyPatch, *, drop: tuple[str, ...] = ()
) -> AbortOnErrorConn:
    """The `get_connection()` singleton, replaced by an abort-simulating stub
    over a real schema-migrated SQLite connection with one assignment row.

    `monkeypatch.setattr` rather than `db_mod.override_connection` so the
    autouse `coord_db` fixture's own connection is restored before its
    teardown runs.
    """
    real = schema_migrated_sqlite_connection(drop=drop)
    if "assignments" not in drop:
        sql.execute(
            real,
            "INSERT INTO assignments (assignment_id, repo_name, issue_number, "
            "issue_title, machine_name, status) VALUES (?, ?, ?, ?, ?, ?)",
            ("a-1", "demo", 42, "Demo issue", "laptop", "running"),
        )
        real.commit()
    conn = abort_simulating_connection(monkeypatch, real)
    monkeypatch.setattr(db_mod, "_conn", conn)
    return conn


class TestListIssueNumbersWithAssignmentsRollsBack:
    """`_list_issue_numbers_with_assignments_local` loops over
    ``("assignments", "assignments_archive")`` and `continue`s past a missing
    table **on the same connection**.  `assignments_archive` is created by
    `coord.housekeeping`, not `_ensure_schema`, so "it doesn't exist yet" is
    the ordinary case the comment names -- and on Postgres it aborted the
    transaction for everything that came afterwards."""

    def test_returns_live_issue_numbers_and_leaves_the_connection_usable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = _abort_conn_with_assignment_rows(monkeypatch)

        assert state._list_issue_numbers_with_assignments_local("demo") == {42}
        assert conn.rollbacks == 1  # the archive miss, recovered

        # The acceptance criterion: a further operation on the same
        # connection.  Pre-fix this raises "current transaction is aborted"
        # -- and it is the *shared singleton*, so every later caller in the
        # process saw it, not just this one.
        assert sql.execute(conn, "SELECT 1 AS ok").fetchone()["ok"] == 1

    def test_a_later_state_read_on_the_same_singleton_still_works(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The blast radius, spelled out: the very next `coord.state` call
        through `get_connection()` must not inherit the aborted transaction."""
        _abort_conn_with_assignment_rows(monkeypatch)

        state._list_issue_numbers_with_assignments_local("demo")

        assert state._list_issue_numbers_with_assignments_local("demo") == {42}


class TestBestEffortColumnSwallowsRollBack:
    """The three `update_assignment_*`-family handlers that swallow a
    "column may not exist on a pre-migration DB" error with a bare `pass` /
    `return` and no rollback."""

    def test_update_stop_reason_leaves_the_connection_usable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = _abort_conn_with_assignment_rows(monkeypatch, drop=("assignments",))

        state._update_assignment_stop_reason_local("a-1", "max_turns")

        assert conn.rollbacks == 1
        assert sql.execute(conn, "SELECT 1 AS ok").fetchone()["ok"] == 1

    def test_mark_interactive_leaves_the_connection_usable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = _abort_conn_with_assignment_rows(monkeypatch, drop=("assignments",))

        state._mark_assignment_interactive_local("a-1")

        assert conn.rollbacks == 1
        assert sql.execute(conn, "SELECT 1 AS ok").fetchone()["ok"] == 1

    def test_set_failure_reason_leaves_the_connection_usable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = _abort_conn_with_assignment_rows(monkeypatch, drop=("assignments",))

        state._set_assignment_failure_reason_local("a-1", "agent unreachable")

        assert conn.rollbacks == 1
        assert sql.execute(conn, "SELECT 1 AS ok").fetchone()["ok"] == 1


class TestListIssueNumbersWithAssignmentsOnRealPostgres:
    """The same regression against an actual Postgres server, when one is
    reachable -- `psycopg.errors.UndefinedTable` then
    `InFailedSqlTransaction`, the real shapes the stub above simulates."""

    def test_missing_archive_table_does_not_poison_the_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        unavailable = backends.postgres_available()
        if unavailable:
            pytest.skip(f"no Postgres backend available: {unavailable}")

        session = backends.open_named_session(backends.BACKEND_POSTGRES)
        try:
            db_mod._ensure_schema(session.conn)
            sql.execute(
                session.conn,
                "INSERT INTO assignments (assignment_id, repo_name, issue_number, "
                "issue_title, machine_name, status) VALUES (?, ?, ?, ?, ?, ?)",
                ("a-1", "demo", 42, "Demo issue", "laptop", "running"),
            )
            session.conn.commit()
            monkeypatch.setattr(db_mod, "_conn", session.conn)

            assert state._list_issue_numbers_with_assignments_local("demo") == {42}
            assert sql.execute(session.conn, "SELECT 1 AS ok").fetchone()["ok"] == 1
        finally:
            monkeypatch.undo()
            session.close()


# ── #3113: atomic review-dispatch claim ─────────────────────────────────────


class TestReviewDispatchClaim:
    """`claim_review_dispatch` / `release_review_dispatch_claim`: the
    DB-level conditional insert that replaced `dispatch_review`'s old
    board-snapshot dedupe (`coord.claim.has_active_followup`), which raced —
    two coordinator passes each reading "no review in flight" from their own
    stale snapshot both dispatched a metered review for the same completed
    assignment (the vimcode#804 incident)."""

    def test_second_claim_for_the_same_assignment_loses(self, coord_db) -> None:
        assert state.claim_review_dispatch("w1") is True
        # Same DB, same key — simulates a second, concurrent dispatch_review
        # call racing the first: it must lose deterministically.
        assert state.claim_review_dispatch("w1") is False

    def test_claims_for_different_assignments_both_win(self, coord_db) -> None:
        assert state.claim_review_dispatch("w1") is True
        assert state.claim_review_dispatch("w2") is True

    def test_release_then_reclaim_succeeds(self, coord_db) -> None:
        """A legitimate later re-review of the same work assignment (the
        `coord review <id>` escape hatch) must not be permanently stranded
        once the first review concludes and releases its claim."""
        assert state.claim_review_dispatch("w1") is True
        state.release_review_dispatch_claim("w1")
        assert state.claim_review_dispatch("w1") is True

    def test_release_is_idempotent(self, coord_db) -> None:
        state.release_review_dispatch_claim("never-claimed")  # must not raise
        assert state.claim_review_dispatch("never-claimed") is True

    def test_empty_assignment_id_always_wins(self, coord_db) -> None:
        # Mirrors coord.claim.has_active_followup's own `of_assignment_id is
        # None` short-circuit — nothing to key a claim on, so never block.
        assert state.claim_review_dispatch("") is True
        assert state.claim_review_dispatch("") is True


# ── #3113: render_issue_context_entries exempts review findings from the
#    block-level char cap ───────────────────────────────────────────────────


class TestRenderIssueContextReviewFindingsExemption:
    def test_two_full_size_review_sections_both_survive_uncut(self) -> None:
        """#2466 widened `source="review"` context entries to carry a
        reviewer's full `## Blocking findings` section verbatim, but left
        `ISSUE_CONTEXT_MAX_CHARS` (2500) unaware of that — two racing
        reviews' blocking sections (~1.8KB + ~1.9KB in the vimcode#804
        incident) together exceeded the cap and the raw `block[:max_chars]`
        slice cut the second one off mid-word. Both must now survive whole."""
        first_finding = (
            "## Blocking findings\n\n"
            + "- missing RED statement in the acceptance test scaffold. " * 40
        )
        second_finding = (
            "## Blocking findings\n\n"
            + "- split_insert_undo_group() on every insert-mode arrow key "
            "makes finish_undo_group do an O(buffer) full-text clone per "
            "keystroke. " * 40
        )
        assert len(first_finding) > 1500
        assert len(second_finding) > 1500
        assert len(first_finding) + len(second_finding) > state.ISSUE_CONTEXT_MAX_CHARS

        entries = [
            {
                "id": 1, "pinned": False, "source": "review",
                "body": first_finding, "created_at": 1.0,
            },
            {
                "id": 2, "pinned": False, "source": "review",
                "body": second_finding, "created_at": 2.0,
            },
        ]
        out = state.render_issue_context_entries(entries)
        assert first_finding in out
        assert second_finding in out
        # Neither survived by accident from raising the cap globally — the
        # rendered block genuinely exceeds the nominal cap.
        assert len(out) > state.ISSUE_CONTEXT_MAX_CHARS
        assert "(truncated" not in out

    def test_non_review_entries_are_trimmed_to_protect_review_entries(self) -> None:
        """A huge `source="review"` entry must not silently balloon the
        briefing forever — ordinary (non-review) notes still yield first."""
        big_review = "## Blocking findings\n\n" + ("x" * (state.ISSUE_CONTEXT_MAX_CHARS + 500))
        entries = [
            {"id": 1, "pinned": False, "source": "review", "body": big_review, "created_at": 2.0},
            {"id": 2, "pinned": False, "source": "work", "body": "an ordinary note", "created_at": 1.0},
        ]
        out = state.render_issue_context_entries(entries)
        assert big_review in out
        assert "an ordinary note" not in out
        assert "trimmed" in out

    def test_review_entries_beyond_the_uncapped_limit_can_be_trimmed(self) -> None:
        """A heavily-iterated issue with MORE than
        `ISSUE_CONTEXT_MAX_UNCAPPED_REVIEW_ENTRIES` review entries must not be
        allowed to balloon every future briefing with unbounded uncapped
        review text — only the newest N stay protected; older ones fall back
        into the normal char-capped/droppable pool."""
        assert state.ISSUE_CONTEXT_MAX_UNCAPPED_REVIEW_ENTRIES == 4
        num_reviews = state.ISSUE_CONTEXT_MAX_UNCAPPED_REVIEW_ENTRIES + 2
        chunk = state.ISSUE_CONTEXT_MAX_CHARS  # each entry alone exceeds nothing,
        # but together they blow well past the cap.
        entries = [
            {
                "id": i,
                "pinned": False,
                "source": "review",
                "body": f"## Blocking findings\n\nfinding-{i} " + ("y" * (chunk // 3)),
                "created_at": float(i),
            }
            for i in range(1, num_reviews + 1)
        ]
        out = state.render_issue_context_entries(entries)
        # The newest N (highest created_at / id) survive whole and uncapped.
        newest_ids = range(num_reviews - state.ISSUE_CONTEXT_MAX_UNCAPPED_REVIEW_ENTRIES + 1, num_reviews + 1)
        for i in newest_ids:
            assert f"finding-{i} " in out
        # At least one of the oldest entries beyond the uncapped budget is
        # dropped/trimmed rather than rendered in full forever.
        oldest_ids = range(1, num_reviews - state.ISSUE_CONTEXT_MAX_UNCAPPED_REVIEW_ENTRIES + 1)
        assert any(f"finding-{i} " not in out for i in oldest_ids)
        assert "trimmed" in out

    def test_no_review_entries_behaves_exactly_as_before(self) -> None:
        """Regression guard: with no `source="review"` entries, ordering and
        truncation behavior is unchanged from pre-#3113."""
        entries = [
            {"id": 1, "pinned": True, "source": "operator", "body": "PIN dep #99", "created_at": 1.0},
            {"id": 2, "pinned": False, "source": "test", "body": "old note", "created_at": 2.0},
            {"id": 3, "pinned": False, "source": "work", "body": "new note", "created_at": 3.0},
        ]
        out = state.render_issue_context_entries(entries)
        lines = out.splitlines()
        assert lines[0].startswith("- 📌 PIN dep #99")
        assert "new note" in lines[1] and "old note" in lines[2]
