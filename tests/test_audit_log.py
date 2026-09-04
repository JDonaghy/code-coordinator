"""Tests for the #1036 audit trail: the `audit_log` schema, `record_audit()`
(coord/audit.py), and the hooks at the state._*_local / issue_store write
choke points.

Scope per the issue's acceptance bar:
- exactly one `audit_log` row per transition, with the right
  event_type/actor/assignment_id/tier;
- `details_json` round-trips;
- `record_audit` swallows a bad write without breaking the board write it
  rode on.
"""

from __future__ import annotations

import json
import logging
import sqlite3

import pytest

from coord import sql
from coord.audit import record_audit
from coord.models import Proposal
from coord.state import record_dispatched, record_test_verdict, mark_notified


def _audit_rows(conn, *, assignment_id: str | None = None) -> list:
    if assignment_id is None:
        return conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
    return conn.execute(
        "SELECT * FROM audit_log WHERE assignment_id=? ORDER BY id", (assignment_id,)
    ).fetchall()


def _dispatch(coord_db, *, assignment_id: str = "aid-1", issue_number: int = 42) -> None:
    proposal = Proposal(
        id=1,
        machine_name="laptop",
        repo_name="api",
        issue_number=issue_number,
        issue_title="Fix auth",
        rationale="best fit",
        briefing="Fix the auth module",
    )
    record_dispatched(
        assignment_id=assignment_id,
        proposal=proposal,
        repo_github="acme/api",
    )


class TestSchema:
    def test_audit_log_table_exists_with_expected_columns(self, coord_db) -> None:
        # #3083: `PRAGMA table_info` is SQLite-only and a hard syntax error
        # against psycopg — sql.table_columns is the seam's portable form.
        cols = {name for name, _type in sql.table_columns(coord_db, "audit_log")}
        assert cols == {
            "id", "ts", "tier", "category", "event_type", "actor",
            "repo", "issue", "assignment_id", "machine", "summary", "details_json",
        }

    def test_indexes_exist(self, coord_db) -> None:
        # #3083: `PRAGMA index_list` likewise. Asserted as a subset, not an
        # exact set: the two backends disagree on whether a PRIMARY KEY
        # materializes a listed index, and only these two are deliberate.
        names = set(sql.index_names(coord_db, "audit_log"))
        assert "idx_audit_log_ts" in names
        assert "idx_audit_log_assignment" in names


class TestRecordAudit:
    def test_basic_insert(self, coord_db) -> None:
        record_audit(
            tier="business",
            category="test",
            event_type="test_passed",
            actor="user",
            summary="Test passed",
            repo="api",
            issue=42,
            assignment_id="aid-1",
            machine="laptop",
        )
        rows = _audit_rows(coord_db)
        assert len(rows) == 1
        row = rows[0]
        assert row["tier"] == "business"
        assert row["category"] == "test"
        assert row["event_type"] == "test_passed"
        assert row["actor"] == "user"
        assert row["repo"] == "api"
        assert row["issue"] == 42
        assert row["assignment_id"] == "aid-1"
        assert row["machine"] == "laptop"
        assert row["ts"] is not None

    def test_details_json_roundtrips(self, coord_db) -> None:
        details = {"test_reason": "flaky assertion", "count": 3, "nested": {"a": [1, 2]}}
        record_audit(
            tier="business",
            category="test",
            event_type="test_failed",
            actor="user",
            summary="Test failed",
            assignment_id="aid-1",
            details=details,
        )
        row = _audit_rows(coord_db)[0]
        assert json.loads(row["details_json"]) == details

    def test_details_none_stores_null(self, coord_db) -> None:
        record_audit(
            tier="business", category="merge", event_type="merged",
            actor="coordinator", summary="merged",
        )
        row = _audit_rows(coord_db)[0]
        assert row["details_json"] is None

    def test_swallows_bad_write_without_raising(self, coord_db, monkeypatch) -> None:
        def _boom():
            raise RuntimeError("disk I/O error")

        monkeypatch.setattr("coord.audit.get_connection", _boom)
        # Must not raise.
        record_audit(
            tier="business", category="test", event_type="test_passed",
            actor="user", summary="should not blow up",
        )

    def test_bad_write_does_not_break_the_board_write_it_rode_on(
        self, coord_db, monkeypatch
    ) -> None:
        """The acceptance-bar scenario: record_test_verdict's assignments
        UPDATE must succeed even when the audit_log write fails."""
        _dispatch(coord_db, assignment_id="aid-1")

        def _boom():
            raise RuntimeError("audit_log write exploded")

        monkeypatch.setattr("coord.audit.get_connection", _boom)

        # Must not raise, despite the audit layer being completely broken.
        record_test_verdict(assignment_id="aid-1", test_state="passed")

        row = coord_db.execute(
            "SELECT test_state FROM assignments WHERE assignment_id=?", ("aid-1",)
        ).fetchone()
        assert row["test_state"] == "passed"
        # No test-verdict audit row landed, since the write genuinely
        # failed (the earlier dispatch's own row, written before the
        # monkeypatch took effect, is unaffected).
        assert [
            r for r in _audit_rows(coord_db, assignment_id="aid-1")
            if r["category"] == "test"
        ] == []


class _FlakyConn:
    """Wraps a real sqlite3 connection and makes its first *fail_times*
    ``execute()`` calls raise ``database is locked`` before delegating to
    the real connection — simulates a momentary collision with a
    concurrent writer.  ``fail_times=None`` never delegates (sustained
    contention that outlasts the whole retry budget).

    #2767: ``record_audit``'s write now goes through ``coord.sql.execute()``,
    which calls ``conn.cursor()`` then ``cursor.execute()`` rather than the
    sqlite3 connection-level ``.execute()`` shortcut — so ``cursor()`` must
    be implemented too, not just ``execute()``. ``__module__`` is pinned to
    ``"sqlite3"`` so ``coord.sql.detect_dialect`` (keyed off
    ``type(conn).__module__``) recognizes this fake as SQLite instead of
    raising ``UnsupportedDialectError`` before the intended lock error ever
    fires — mirrors ``tests/test_state.py``'s ``_FlakyConn`` (#2726) and
    ``tests/test_serve.py``'s ``_AlwaysLockedConn`` (#2726).
    """

    __module__ = "sqlite3"

    def __init__(self, real_conn, fail_times: int | None) -> None:
        self._real = real_conn
        self._fail_times = fail_times
        self.calls = 0

    def cursor(self):
        return self

    def execute(self, *args, **kwargs):
        self.calls += 1
        if self._fail_times is None or self.calls <= self._fail_times:
            raise sqlite3.OperationalError("database is locked")
        return self._real.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


class _TrimLockedConn:
    """Wraps a real connection so every ``INSERT`` succeeds normally but
    every ``DELETE`` (the opportunistic ``audit.max_rows`` trim) hits
    sustained lock contention — isolates a trim-only failure from the
    INSERT that already committed successfully above it.

    #2767: see ``_FlakyConn`` above — ``cursor()``/``__module__`` are needed
    for the same reason now that both the insert and the trim route through
    ``coord.sql.execute()``.
    """

    __module__ = "sqlite3"

    def __init__(self, real_conn) -> None:
        self._real = real_conn

    def cursor(self):
        return self

    def execute(self, sql, *args, **kwargs):
        if sql.strip().upper().startswith("DELETE"):
            raise sqlite3.OperationalError("database is locked")
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


class TestRecordAuditRetriesLockContention:
    """#2597: record_audit's write previously had zero retry protection at
    all — 1,804 audit rows/24h were measured lost to ordinary, momentary
    lock contention (a concurrent writer holding the DB for a beat) on
    dellserver. It now rides out a transient collision via
    `coord.db.retry_on_locked` before falling back to the documented
    best-effort swallow (unaffected by this fix — see
    `TestRecordAudit.test_swallows_bad_write_without_raising` above)."""

    def test_retries_transient_contention_then_writes_the_row(
        self, coord_db, monkeypatch
    ) -> None:
        proxy = _FlakyConn(coord_db, fail_times=2)
        monkeypatch.setattr("coord.audit.get_connection", lambda: proxy)
        monkeypatch.setattr("coord.db.time.sleep", lambda s: None)

        record_audit(
            tier="business", category="test", event_type="test_passed",
            actor="user", summary="retried through contention",
            assignment_id="aid-retry",
        )

        assert proxy.calls == 3
        rows = _audit_rows(coord_db, assignment_id="aid-retry")
        assert len(rows) == 1
        assert rows[0]["summary"] == "retried through contention"

    def test_sustained_contention_is_still_swallowed_not_raised(
        self, coord_db, monkeypatch
    ) -> None:
        proxy = _FlakyConn(coord_db, fail_times=None)
        monkeypatch.setattr("coord.audit.get_connection", lambda: proxy)
        monkeypatch.setattr("coord.db.time.sleep", lambda s: None)

        # Must not raise — record_audit's documented contract, unchanged.
        record_audit(
            tier="business", category="test", event_type="test_passed",
            actor="user", summary="lost to sustained contention",
        )

        assert _audit_rows(coord_db) == []


class TestLockContentionLossCounter:
    """#2597: 1,804 identical WARNING lines/day is not a usable signal — a
    write lost to sustained contention is now counted instead of logged
    individually, so a per-run aggregate can report the total in one
    line."""

    def test_stays_at_zero_when_nothing_is_lost(self, coord_db) -> None:
        from coord.audit import audit_lock_contention_losses

        baseline = audit_lock_contention_losses()
        record_audit(
            tier="business", category="test", event_type="test_passed",
            actor="user", summary="ordinary write",
        )
        assert audit_lock_contention_losses() == baseline

    def test_increments_when_a_write_is_genuinely_dropped(
        self, coord_db, monkeypatch
    ) -> None:
        from coord.audit import audit_lock_contention_losses

        monkeypatch.setattr("coord.db.time.sleep", lambda s: None)
        baseline = audit_lock_contention_losses()
        proxy = _FlakyConn(coord_db, fail_times=None)
        monkeypatch.setattr("coord.audit.get_connection", lambda: proxy)

        record_audit(
            tier="business", category="test", event_type="test_passed",
            actor="user", summary="should be counted, not logged per-row",
        )

        assert audit_lock_contention_losses() == baseline + 1

    def test_flush_emits_one_aggregated_warning_and_resets(
        self, coord_db, monkeypatch, caplog
    ) -> None:
        from coord.audit import (
            _flush_lock_contention_summary,
            audit_lock_contention_losses,
        )

        monkeypatch.setattr("coord.db.time.sleep", lambda s: None)
        proxy = _FlakyConn(coord_db, fail_times=None)
        monkeypatch.setattr("coord.audit.get_connection", lambda: proxy)

        for _ in range(3):
            record_audit(
                tier="business", category="test", event_type="test_passed",
                actor="user", summary="lost",
            )
        losses_before_flush = audit_lock_contention_losses()
        assert losses_before_flush >= 3

        with caplog.at_level(logging.WARNING, logger="coord.audit"):
            _flush_lock_contention_summary()

        assert audit_lock_contention_losses() == 0
        matches = [
            r for r in caplog.records
            if "writes lost to lock contention" in r.message
        ]
        # ONE aggregated line, not one per lost write.
        assert len(matches) == 1
        assert str(losses_before_flush) in matches[0].getMessage()

    def test_trim_only_failure_does_not_count_as_a_lost_write(
        self, coord_db, monkeypatch
    ) -> None:
        """#2597-review: `_maybe_trim`'s DELETE is opportunistic
        housekeeping over rows the INSERT above it already committed
        durably. A trim that hits sustained lock contention must not be
        misclassified by the loss counter as a *lost audit write* — the row
        made it into audit_log just fine; only the retention cap didn't run
        this pass. Over-counting here would undercut the whole point of the
        counter (a trustworthy loss rate)."""
        from coord.audit import audit_lock_contention_losses

        monkeypatch.setattr("coord.audit._resolve_max_rows", lambda: 5)
        monkeypatch.setattr("coord.db.time.sleep", lambda s: None)
        proxy = _TrimLockedConn(coord_db)
        monkeypatch.setattr("coord.audit.get_connection", lambda: proxy)
        baseline = audit_lock_contention_losses()

        record_audit(
            tier="business", category="test", event_type="test_passed",
            actor="user", summary="row written, trim contended",
        )

        # The row itself made it in...
        rows = _audit_rows(coord_db)
        assert any(
            r["summary"] == "row written, trim contended" for r in rows
        )
        # ...and the loss counter must not blame it for the trim's failure.
        assert audit_lock_contention_losses() == baseline

    def test_flush_lock_contention_summary_public_wrapper(
        self, coord_db, monkeypatch, caplog
    ) -> None:
        """#2597-review: `flush_lock_contention_summary` is the public
        entry point meant to be called on a periodic cadence by a
        long-running process (`coord serve`'s tick loop) rather than only
        at `atexit` — it must actually flush the pending count and log the
        aggregate, identical to the internal function it wraps."""
        from coord.audit import (
            audit_lock_contention_losses,
            flush_lock_contention_summary,
        )

        monkeypatch.setattr("coord.db.time.sleep", lambda s: None)
        proxy = _FlakyConn(coord_db, fail_times=None)
        monkeypatch.setattr("coord.audit.get_connection", lambda: proxy)

        record_audit(
            tier="business", category="test", event_type="test_passed",
            actor="user", summary="lost",
        )
        assert audit_lock_contention_losses() >= 1

        with caplog.at_level(logging.WARNING, logger="coord.audit"):
            flush_lock_contention_summary()

        assert audit_lock_contention_losses() == 0
        assert any(
            "writes lost to lock contention" in r.message for r in caplog.records
        )


class TestAuditLevel:
    """#1038: `audit.level` gates the operational tier at the record_audit
    choke point.  Business-tier rows are never gated."""

    def test_operational_row_suppressed_when_level_is_business(
        self, coord_db, monkeypatch
    ) -> None:
        monkeypatch.setattr("coord.audit._resolve_level", lambda: "business")
        record_audit(
            tier="operational", category="reconcile", event_type="passive_reconcile",
            actor="daemon", summary="should be dropped",
        )
        assert _audit_rows(coord_db) == []

    def test_operational_row_included_when_level_is_operational(
        self, coord_db, monkeypatch
    ) -> None:
        monkeypatch.setattr("coord.audit._resolve_level", lambda: "operational")
        record_audit(
            tier="operational", category="reconcile", event_type="passive_reconcile",
            actor="daemon", summary="should land",
        )
        rows = _audit_rows(coord_db)
        assert len(rows) == 1
        assert rows[0]["tier"] == "operational"
        assert rows[0]["actor"] == "daemon"

    def test_operational_row_included_by_default(
        self, coord_db, monkeypatch, tmp_path
    ) -> None:
        """No resolvable coordinator.yml → `_resolve_level` defaults to
        `"operational"`, so operational rows are captured, not silently
        dropped.  Points ``$COORD_CONFIG`` at a nonexistent path so this is
        deterministic regardless of the host's real config."""
        monkeypatch.setenv("COORD_CONFIG", str(tmp_path / "nonexistent.yml"))
        record_audit(
            tier="operational", category="reconcile", event_type="passive_reconcile",
            actor="daemon", summary="default level",
        )
        assert len(_audit_rows(coord_db)) == 1

    def test_business_row_never_gated_by_level(self, coord_db, monkeypatch) -> None:
        monkeypatch.setattr("coord.audit._resolve_level", lambda: "business")
        record_audit(
            tier="business", category="merge", event_type="merged",
            actor="coordinator", summary="business rows always land",
        )
        assert len(_audit_rows(coord_db)) == 1


class TestHookedTransitions:
    """One audit_log row per real transition at the state.py choke points."""

    def test_dispatch_writes_one_row(self, coord_db) -> None:
        _dispatch(coord_db, assignment_id="aid-1")
        rows = _audit_rows(coord_db, assignment_id="aid-1")
        assert len(rows) == 1
        assert rows[0]["tier"] == "business"
        assert rows[0]["category"] == "dispatch"
        assert rows[0]["event_type"] == "dispatched"
        assert rows[0]["repo"] == "api"
        assert rows[0]["issue"] == 42

    def test_test_verdict_writes_one_row_with_right_fields(self, coord_db) -> None:
        _dispatch(coord_db, assignment_id="aid-1")
        record_test_verdict(
            assignment_id="aid-1", test_state="passed", test_reason=None,
        )
        rows = [
            r for r in _audit_rows(coord_db, assignment_id="aid-1")
            if r["category"] == "test"
        ]
        assert len(rows) == 1
        row = rows[0]
        assert row["event_type"] == "test_passed"
        assert row["actor"] == "user"
        assert row["assignment_id"] == "aid-1"
        assert row["tier"] == "business"

    def test_test_verdict_failed_reason_in_details(self, coord_db) -> None:
        _dispatch(coord_db, assignment_id="aid-1")
        record_test_verdict(
            assignment_id="aid-1", test_state="failed", test_reason="boom",
        )
        rows = [
            r for r in _audit_rows(coord_db, assignment_id="aid-1")
            if r["event_type"] == "test_failed"
        ]
        assert len(rows) == 1
        assert json.loads(rows[0]["details_json"])["test_reason"] == "boom"

    def test_test_verdict_skipped_reason_in_details(self, coord_db) -> None:
        # #1213: a --skipped verdict's reason is the audit trail for why the
        # human Test gate was bypassed — it must land in details, same as
        # --fail's reason does.
        _dispatch(coord_db, assignment_id="aid-1")
        record_test_verdict(
            assignment_id="aid-1", test_state="skipped",
            test_reason="trivial dep bump, covered by regression test",
        )
        rows = [
            r for r in _audit_rows(coord_db, assignment_id="aid-1")
            if r["event_type"] == "test_skipped"
        ]
        assert len(rows) == 1
        assert json.loads(rows[0]["details_json"])["test_reason"] == (
            "trivial dep bump, covered by regression test"
        )

    def test_mark_notified_completion_writes_one_row(self, coord_db) -> None:
        _dispatch(coord_db, assignment_id="aid-1")
        from coord.comments import EVENT_COMPLETION

        mark_notified("aid-1", EVENT_COMPLETION, branch="issue-42-fix")
        rows = [
            r for r in _audit_rows(coord_db, assignment_id="aid-1")
            if r["event_type"] == EVENT_COMPLETION
        ]
        assert len(rows) == 1
        assert rows[0]["actor"] == "worker"
        assert rows[0]["repo"] == "api"
        assert rows[0]["issue"] == 42

    def test_mark_notified_stuck_strips_composite_key(self, coord_db) -> None:
        _dispatch(coord_db, assignment_id="aid-1")
        from coord.comments import EVENT_STUCK

        mark_notified("aid-1:stuck", EVENT_STUCK)
        rows = [
            r for r in _audit_rows(coord_db, assignment_id="aid-1")
            if r["event_type"] == EVENT_STUCK
        ]
        assert len(rows) == 1
        assert rows[0]["actor"] == "daemon"
        assert rows[0]["repo"] == "api"
        assert rows[0]["issue"] == 42

    def test_mark_assignment_merged_writes_one_row_only_on_real_transition(
        self, coord_db
    ) -> None:
        from coord.state import mark_assignment_merged

        _dispatch(coord_db, assignment_id="aid-1")
        # Not 'done' yet — mark_assignment_merged is a no-op, no audit row.
        mark_assignment_merged("aid-1")
        assert [
            r for r in _audit_rows(coord_db, assignment_id="aid-1")
            if r["category"] == "merge"
        ] == []

        coord_db.execute(
            "UPDATE assignments SET status='done' WHERE assignment_id=?", ("aid-1",)
        )
        coord_db.commit()
        mark_assignment_merged("aid-1")
        merge_rows = [
            r for r in _audit_rows(coord_db, assignment_id="aid-1")
            if r["category"] == "merge"
        ]
        assert len(merge_rows) == 1
        assert merge_rows[0]["event_type"] == "merged"

        # Idempotent: calling again after it's already merged writes no
        # second row.
        mark_assignment_merged("aid-1")
        merge_rows_2 = [
            r for r in _audit_rows(coord_db, assignment_id="aid-1")
            if r["category"] == "merge"
        ]
        assert len(merge_rows_2) == 1

    def test_mark_assignment_merged_carries_the_boards_pr_url_into_details(
        self, coord_db
    ) -> None:
        """#3071 review: `coord journal`'s merge entries read `details.pr_url`
        off exactly this row — with nowhere else to learn one from, the PR
        URL the board already has on file (`assignments.pr_url`) must reach
        `audit_log.details`, not just the human-readable `summary` text."""
        import json as _json

        from coord.state import mark_assignment_merged

        _dispatch(coord_db, assignment_id="aid-1")
        coord_db.execute(
            "UPDATE assignments SET status='done', pr_url=? WHERE assignment_id=?",
            ("https://github.com/acme/api/pull/7", "aid-1"),
        )
        coord_db.commit()

        mark_assignment_merged("aid-1")

        [merge_row] = [
            r for r in _audit_rows(coord_db, assignment_id="aid-1")
            if r["category"] == "merge"
        ]
        details = _json.loads(merge_row["details_json"] or "{}")
        assert details.get("pr_url") == "https://github.com/acme/api/pull/7"

    def test_mark_assignment_merged_with_no_pr_url_on_file_omits_it(
        self, coord_db
    ) -> None:
        """A merge recorded with no PR ever attached to the row (e.g. a
        direct out-of-band merge) must not raise, and must not fabricate a
        `pr_url` — the downstream journal fold treats an absent key as a gap
        in the pointer, never a crash."""
        import json as _json

        from coord.state import mark_assignment_merged

        _dispatch(coord_db, assignment_id="aid-1")
        coord_db.execute(
            "UPDATE assignments SET status='done' WHERE assignment_id=?", ("aid-1",)
        )
        coord_db.commit()

        mark_assignment_merged("aid-1")

        [merge_row] = [
            r for r in _audit_rows(coord_db, assignment_id="aid-1")
            if r["category"] == "merge"
        ]
        details = _json.loads(merge_row["details_json"] or "{}")
        assert "pr_url" not in details

    def test_update_assignment_branch_writes_one_row_and_is_idempotent(
        self, coord_db
    ) -> None:
        proposal = Proposal(
            id=1, machine_name="laptop", repo_name="api", issue_number=42,
            issue_title="Fix auth", rationale="x",
        )
        # Dispatch with no target_branch so the row gets the auto-slugified
        # branch (non-empty) — use record_dispatched_assignment instead to
        # land a NULL branch, matching #611's scenario.
        from coord.models import Assignment
        from coord.state import record_dispatched_assignment, update_assignment_branch

        record_dispatched_assignment(
            assignment=Assignment(
                assignment_id="aid-2", machine_name="laptop", repo_name="api",
                issue_number=42, issue_title="Fix auth", type="work", branch=None,
            ),
            repo_github="acme/api",
        )
        update_assignment_branch("aid-2", "issue-42-fix-auth")
        rows = [
            r for r in _audit_rows(coord_db, assignment_id="aid-2")
            if r["event_type"] == "branch_set"
        ]
        assert len(rows) == 1

        # Second call is a no-op (branch already set) — no new row.
        update_assignment_branch("aid-2", "issue-42-fix-auth")
        rows_2 = [
            r for r in _audit_rows(coord_db, assignment_id="aid-2")
            if r["event_type"] == "branch_set"
        ]
        assert len(rows_2) == 1

    def test_dispatch_duplicate_assignment_id_writes_no_second_row(
        self, coord_db
    ) -> None:
        """#1036 fix review finding 1: a second dispatch with the same
        assignment_id hits ON CONFLICT DO NOTHING — the INSERT is a no-op,
        so it must not emit a phantom second 'dispatched' row."""
        _dispatch(coord_db, assignment_id="aid-1", issue_number=42)
        rows = _audit_rows(coord_db, assignment_id="aid-1")
        assert len(rows) == 1

        # Same assignment_id, different issue — simulates a retry/collision;
        # the INSERT no-ops so the original row's issue must be untouched
        # and no new audit row should appear.
        _dispatch(coord_db, assignment_id="aid-1", issue_number=99)
        rows_2 = _audit_rows(coord_db, assignment_id="aid-1")
        assert len(rows_2) == 1
        assert rows_2[0]["issue"] == 42

    def test_launch_failure_reason_no_audit_for_unknown_assignment_id(
        self, coord_db
    ) -> None:
        """#1036 fix review finding 2: the UPDATE ... WHERE assignment_id=?
        touches no row for a bad/stale id — must not emit an audit row for
        a transition that didn't happen."""
        from coord.state import set_assignment_failure_reason

        set_assignment_failure_reason("no-such-assignment", "boom")
        rows = [
            r for r in _audit_rows(coord_db)
            if r["event_type"] == "launch_failed"
        ]
        assert rows == []

    def test_launch_failure_reason_writes_one_row_with_coordinator_actor(
        self, coord_db
    ) -> None:
        """#1036 fix review finding 3: this is a coordinator/launcher-side
        backstop (fires before the worker session starts), not a worker
        self-report — actor should be 'coordinator'."""
        from coord.state import set_assignment_failure_reason

        _dispatch(coord_db, assignment_id="aid-1")
        set_assignment_failure_reason("aid-1", "worktree add failed")
        rows = [
            r for r in _audit_rows(coord_db, assignment_id="aid-1")
            if r["event_type"] == "launch_failed"
        ]
        assert len(rows) == 1
        assert rows[0]["actor"] == "coordinator"
        assert rows[0]["category"] == "error"

    def test_review_findings_retry_with_same_verdict_writes_one_row(
        self, coord_db
    ) -> None:
        """#1036 fix review finding 4: a retried write of the identical
        (verdict, body) pair — the shape of issue_store._persist_review_
        verdict's retry loop when a successful UPDATE is followed by a
        readback that looks mismatched — must not double the audit row."""
        from coord.state import update_assignment_review_findings

        _dispatch(coord_db, assignment_id="aid-1")
        update_assignment_review_findings(
            "aid-1", verdict="approved", body="looks good"
        )
        rows = [
            r for r in _audit_rows(coord_db, assignment_id="aid-1")
            if r["event_type"] == "review_approved"
        ]
        assert len(rows) == 1

        # Retry with the identical verdict + body — no second row.
        update_assignment_review_findings(
            "aid-1", verdict="approved", body="looks good"
        )
        rows_2 = [
            r for r in _audit_rows(coord_db, assignment_id="aid-1")
            if r["event_type"] == "review_approved"
        ]
        assert len(rows_2) == 1

        # A genuinely new verdict/body IS a real transition — new row.
        update_assignment_review_findings(
            "aid-1", verdict="request-changes", body="needs work"
        )
        rows_3 = [
            r for r in _audit_rows(coord_db, assignment_id="aid-1")
            if r["event_type"] in ("review_approved", "review_request-changes")
        ]
        assert len(rows_3) == 2
