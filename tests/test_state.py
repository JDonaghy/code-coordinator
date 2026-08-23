"""Tests for coord.state — proposal persistence."""

from __future__ import annotations

import sqlite3
import time
import warnings

import pytest

from coord.models import Proposal
from coord.state import (
    save_proposals,
    load_proposals,
    clear_proposals,
    record_dispatched,
    record_test_verdict,
    update_assignment_claude_session_id,
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
        cols = {row[1] for row in conn.execute("PRAGMA table_info(assignments)").fetchall()}
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
        cols = {row[1] for row in conn.execute("PRAGMA table_info(assignments)").fetchall()}
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
        cols = {row[1] for row in conn.execute("PRAGMA table_info(assignments)").fetchall()}
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
        the DB for a few moments."""

        def __init__(self, real_conn, fail_times: int) -> None:
            self._real = real_conn
            self._fail_times = fail_times
            self.calls = 0

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
        cols = {row[1] for row in conn.execute("PRAGMA table_info(assignments)").fetchall()}
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
        record_issue_comment_capture(
            repo_name="acme/api", issue_number=1, body="x", gh_comment_id=444,
        )
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
            cc, "post_record",
            lambda svc, path, payload, **kw: captured.update(
                path=path, payload=payload
            ) or {"synced": 3},
        )
        assert sync_issue_comments("api", 7, repo_github="acme/api") == 3
        assert captured["path"] == "/issue-comments"
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
    """

    def __init__(self, real_conn, fail_times: int) -> None:
        self._real = real_conn
        self._fail_times = fail_times
        self.calls = 0

    def execute(self, *args, **kwargs):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise sqlite3.OperationalError("database is locked")
        return self._real.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


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
