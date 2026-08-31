"""Tests for #2510: bounded auto-fix dispatch for a CONFIRMED CI checks_failed
merge-gate failure, plus HUMAN_REQUIRED escalation once the retry cap is
spent.

Covers:
- Briefing assembly
- Dispatcher integration with the board (reuses coord.auto_loop._dispatch_fix)
- Retry cap / active-fix-in-flight guards
- The CLI's `_dispatch_ci_fixes` classification wrapper (dispatch vs
  HUMAN_REQUIRED escalation)
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from coord.ci_fix import (
    MAX_CI_FIX_DISPATCHES,
    MAX_CI_FIX_NOOP_STREAK,
    CI_FIX_TITLE_PREFIX,
    build_ci_fix_briefing,
    dispatch_ci_fix,
    dispatch_was_noop,
    refund_noop_ci_fix,
)
from coord.config import Config, ReviewsConfig
from coord.merge_queue import HUMAN_REQUIRED, PENDING, MergeEvent, QueuedMerge
from coord.models import Assignment, Board, Machine, Repo


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def repo() -> Repo:
    return Repo(name="api", github="acme/api", default_branch="main", test_command="pytest")


@pytest.fixture
def two_machine_config(repo: Repo) -> Config:
    return Config(
        repos=[repo],
        machines=[
            Machine(
                name="laptop", host="laptop.tail",
                repos=["api"], repo_paths={"api": "/work/api"},
            ),
            Machine(
                name="server", host="server.tail",
                repos=["api"], repo_paths={"api": "/srv/api"},
            ),
        ],
        reviews=ReviewsConfig(enabled=True, auto_dispatch=False),
    )


def _entry(*, error: str | None = "checks failed: acceptance (failure)") -> QueuedMerge:
    return QueuedMerge(
        assignment_id="w1",
        repo_name="api",
        repo_github="acme/api",
        branch="issue-1-fix",
        target_branch="main",
        issue_number=1,
        issue_title="Fix the thing",
        state=PENDING,
        pr_number=42,
        pr_url="https://github.com/acme/api/pull/42",
        error=error,
    )


def _work_assignment(*, aid: str = "w1", status: str = "done") -> Assignment:
    return Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=1,
        issue_title="Fix the thing",
        assignment_id=aid,
        status=status,
        type="work",
        branch="issue-1-fix",
        files_allowed=["src/foo.py"],
        files_forbidden=["docs/**"],
        dispatched_at=10.0,
    )


class _FakeHTTPResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


class _FakeHTTPClient:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, *, json: dict, timeout: float) -> _FakeHTTPResponse:
        self.calls.append((url, json))
        return _FakeHTTPResponse(self._payload)


# ── Briefing ─────────────────────────────────────────────────────────────────


class TestBuildBriefing:
    def test_contains_check_summary_and_issue(self) -> None:
        briefing = build_ci_fix_briefing(
            entry=_entry(), checks_summary="acceptance (failure)", attempt=1,
        )
        assert "acceptance (failure)" in briefing
        assert "#1" in briefing
        assert "Fix the thing" in briefing
        assert "issue-1-fix" in briefing

    def test_includes_attempt_count(self) -> None:
        briefing = build_ci_fix_briefing(
            entry=_entry(), checks_summary="x", attempt=2,
        )
        assert f"2/{MAX_CI_FIX_DISPATCHES}" in briefing

    def test_denies_gh_and_workflow_gaming(self) -> None:
        briefing = build_ci_fix_briefing(
            entry=_entry(), checks_summary="x", attempt=1,
        )
        assert "gh" in briefing
        assert "workflow config" in briefing


# ── #3011: no-op leg detection/refund ────────────────────────────────────────


class TestDispatchWasNoop:
    def test_false_when_no_prior_dispatch(self) -> None:
        entry = _entry()
        entry.ci_fix_head_sha = ""
        entry.branch_head_sha = "abc123"
        assert dispatch_was_noop(entry) is False

    def test_false_when_current_sha_unknown(self) -> None:
        entry = _entry()
        entry.ci_fix_head_sha = "abc123"
        entry.branch_head_sha = None
        assert dispatch_was_noop(entry) is False

    def test_false_when_branch_moved(self) -> None:
        entry = _entry()
        entry.ci_fix_head_sha = "abc123"
        entry.branch_head_sha = "def456"
        assert dispatch_was_noop(entry) is False

    def test_true_when_branch_unchanged(self) -> None:
        entry = _entry()
        entry.ci_fix_head_sha = "abc123"
        entry.branch_head_sha = "abc123"
        assert dispatch_was_noop(entry) is True


class TestRefundNoopCiFix:
    def test_decrements_dispatches_and_clears_sha(self) -> None:
        entry = _entry()
        entry.ci_fix_dispatches = 1
        entry.ci_fix_head_sha = "abc123"
        entry.branch_head_sha = "abc123"

        refund_noop_ci_fix(entry)

        assert entry.ci_fix_dispatches == 0
        assert entry.ci_fix_head_sha == ""
        assert entry.ci_fix_noop_streak == 1

    def test_does_not_go_below_zero(self) -> None:
        entry = _entry()
        entry.ci_fix_dispatches = 0
        entry.ci_fix_head_sha = "abc123"
        entry.branch_head_sha = "abc123"

        refund_noop_ci_fix(entry)

        assert entry.ci_fix_dispatches == 0

    def test_accumulates_streak_across_calls(self) -> None:
        entry = _entry()
        entry.ci_fix_dispatches = 2
        entry.ci_fix_head_sha = "abc123"
        entry.branch_head_sha = "abc123"

        refund_noop_ci_fix(entry)
        entry.ci_fix_head_sha = "abc123"  # a fresh dispatch would re-snapshot
        refund_noop_ci_fix(entry)

        assert entry.ci_fix_noop_streak == 2


# ── Dispatch ────────────────────────────────────────────────────────────────


class TestDispatchCiFix:
    def test_appends_to_board_and_sends_payload(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        board = Board()
        board.completed.append(_work_assignment())
        client = _FakeHTTPClient({"id": "ci-fix-1"})

        result = dispatch_ci_fix(
            _entry(), board, two_machine_config,
            checks_summary="acceptance (failure)", http_client=client,
        )

        assert result is not None
        assert result.type == "work"
        assert result.branch == "issue-1-fix"
        assert result.review_of_assignment_id == "w1"
        assert result.issue_title.startswith(CI_FIX_TITLE_PREFIX)
        assert result in board.active

        assert len(client.calls) == 1
        _, payload = client.calls[0]
        assert payload["target_branch"] == "issue-1-fix"
        assert "acceptance (failure)" in payload["briefing"]

    def test_increments_ci_fix_dispatches_on_success(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        board = Board()
        board.completed.append(_work_assignment())
        entry = _entry()
        assert entry.ci_fix_dispatches == 0

        dispatch_ci_fix(
            entry, board, two_machine_config,
            http_client=_FakeHTTPClient({"id": "ci-fix-2"}),
        )

        assert entry.ci_fix_dispatches == 1

    def test_snapshots_branch_head_sha_on_success(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        """#3011: a successful dispatch snapshots the branch HEAD it saw so
        a later tick can tell whether THIS leg moved the branch."""
        board = Board()
        board.completed.append(_work_assignment())
        entry = _entry()
        entry.branch_head_sha = "abc123"

        dispatch_ci_fix(
            entry, board, two_machine_config,
            http_client=_FakeHTTPClient({"id": "ci-fix-snap"}),
        )

        assert entry.ci_fix_head_sha == "abc123"

    def test_real_progress_resets_noop_streak(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        """#3011: if the branch moved past the sha a prior dispatch
        snapshotted, that prior leg was a genuine attempt — any no-op
        streak it left behind must not linger into this fresh dispatch."""
        board = Board()
        board.completed.append(_work_assignment())
        entry = _entry()
        entry.ci_fix_head_sha = "abc123"  # snapshot from a prior dispatch
        entry.ci_fix_noop_streak = 1  # e.g. an earlier, unrelated no-op
        entry.branch_head_sha = "def456"  # branch has since moved

        dispatch_ci_fix(
            entry, board, two_machine_config,
            http_client=_FakeHTTPClient({"id": "ci-fix-progress"}),
        )

        assert entry.ci_fix_noop_streak == 0
        assert entry.ci_fix_head_sha == "def456"

    def test_falls_back_to_entry_error_when_no_summary_given(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        board = Board()
        board.completed.append(_work_assignment())
        client = _FakeHTTPClient({"id": "ci-fix-3"})

        dispatch_ci_fix(_entry(), board, two_machine_config, http_client=client)

        _, payload = client.calls[0]
        assert "checks failed: acceptance (failure)" in payload["briefing"]

    def test_returns_none_when_work_assignment_not_found(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        result = dispatch_ci_fix(
            _entry(), Board(), two_machine_config,
            http_client=_FakeHTTPClient({"id": "would-not-fire"}),
        )
        assert result is None

    def test_returns_none_when_retry_cap_spent(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        board = Board()
        board.completed.append(_work_assignment())
        entry = _entry()
        entry.ci_fix_dispatches = MAX_CI_FIX_DISPATCHES

        result = dispatch_ci_fix(
            entry, board, two_machine_config,
            http_client=_FakeHTTPClient({"id": "would-not-fire"}),
        )
        assert result is None
        assert entry.ci_fix_dispatches == MAX_CI_FIX_DISPATCHES

    def test_returns_none_when_a_fix_is_already_in_flight(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        board = Board()
        board.completed.append(_work_assignment())
        board.active.append(Assignment(
            machine_name="server", repo_name="api", issue_number=1, issue_title="x",
            assignment_id="prior-fix", status="running",
            type="work", review_of_assignment_id="w1",
        ))
        result = dispatch_ci_fix(
            _entry(), board, two_machine_config,
            http_client=_FakeHTTPClient({"id": "would-not-fire"}),
        )
        assert result is None

    def test_returns_none_when_http_fails(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        import httpx

        class _Failing:
            def post(self, url, *, json, timeout):
                raise httpx.ConnectError("offline")

        board = Board()
        board.completed.append(_work_assignment())
        result = dispatch_ci_fix(
            _entry(), board, two_machine_config, http_client=_Failing(),
        )
        assert result is None


# ── CLI classification wrapper ───────────────────────────────────────────────


class TestDispatchCiFixesWrapper:
    """`coord.commands.merge._dispatch_ci_fixes` — the same classify-and-
    dispatch shape as `_dispatch_conflict_fixes`, for the checks_failed event
    kind instead of conflict."""

    def _checks_failed_event(self, entry: QueuedMerge) -> MergeEvent:
        return MergeEvent(entry, "checks_failed", entry.error or "")

    def test_ignores_non_checks_failed_events(self) -> None:
        from coord.commands import merge as merge_cmd

        entry = _entry()
        events = [MergeEvent(entry, "checks_pending", "waiting")]
        with patch("coord.ci_fix.dispatch_ci_fix") as dispatch:
            merge_cmd._dispatch_ci_fixes(events, object(), dry_run=False)
        dispatch.assert_not_called()

    def test_dry_run_never_dispatches(self) -> None:
        from coord.commands import merge as merge_cmd

        entry = _entry()
        events = [self._checks_failed_event(entry)]
        with patch("coord.ci_fix.dispatch_ci_fix") as dispatch:
            merge_cmd._dispatch_ci_fixes(events, object(), dry_run=True)
        dispatch.assert_not_called()

    def test_successful_dispatch_leaves_entry_pending(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        from coord.commands import merge as merge_cmd
        from coord.state import save_board

        board = Board()
        board.completed.append(_work_assignment())
        save_board(board)

        entry = _entry()
        events = [self._checks_failed_event(entry)]

        with patch(
            "coord.ci_fix.dispatch_ci_fix",
            return_value=_work_assignment(aid="ci-fix-x", status="running"),
        ) as dispatch:
            merge_cmd._dispatch_ci_fixes(events, two_machine_config, dry_run=False)

        dispatch.assert_called_once()
        assert entry.state == PENDING

    def test_retry_cap_hit_escalates_to_human_required(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        from coord.commands import merge as merge_cmd
        from coord.state import save_board

        board = Board()
        save_board(board)

        entry = _entry()
        entry.ci_fix_dispatches = MAX_CI_FIX_DISPATCHES
        events = [self._checks_failed_event(entry)]

        with patch("coord.ci_fix.dispatch_ci_fix", return_value=None):
            merge_cmd._dispatch_ci_fixes(events, two_machine_config, dry_run=False)

        assert entry.state == HUMAN_REQUIRED

    def test_declined_dispatch_under_budget_leaves_entry_pending(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        """Budget remains but dispatch declined for another reason (no
        machine, agent unreachable, …) — leave PENDING for the next tick to
        retry, same as a declined conflict-fix dispatch."""
        from coord.commands import merge as merge_cmd
        from coord.state import save_board

        board = Board()
        save_board(board)

        entry = _entry()
        events = [self._checks_failed_event(entry)]

        with patch("coord.ci_fix.dispatch_ci_fix", return_value=None):
            merge_cmd._dispatch_ci_fixes(events, two_machine_config, dry_run=False)

        assert entry.state == PENDING

    def test_declined_dispatch_under_budget_is_echoed(
        self, two_machine_config: Config, coord_db, capsys,
    ) -> None:
        """#2538: a declined dispatch (including `_dispatch_fix` hitting
        persistent DB-lock contention, which surfaces exactly like any
        other decline — see coord.auto_loop._dispatch_fix) must be visible
        in `coord merge` output, not silently dropped — otherwise an
        operator watching the run has no idea this entry didn't advance."""
        from coord.commands import merge as merge_cmd
        from coord.state import save_board

        board = Board()
        save_board(board)

        entry = _entry()
        events = [self._checks_failed_event(entry)]

        with patch("coord.ci_fix.dispatch_ci_fix", return_value=None):
            merge_cmd._dispatch_ci_fixes(events, two_machine_config, dry_run=False)

        out = capsys.readouterr().out
        assert "ci-fix not dispatched" in out
        assert f"#{entry.issue_number}" in out


class TestNoopCiFixRefund:
    """#3011: a ci-fix leg that completes with the branch HEAD unchanged
    must be refunded, not counted toward MAX_CI_FIX_DISPATCHES — and must
    not silently walk the entry to HUMAN_REQUIRED on the strength of two
    correct declines."""

    def _checks_failed_event(self, entry: QueuedMerge) -> MergeEvent:
        return MergeEvent(entry, "checks_failed", entry.error or "")

    def test_noop_leg_is_refunded_and_not_dispatched_again(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        from coord.commands import merge as merge_cmd
        from coord.state import save_board

        board = Board()
        save_board(board)

        entry = _entry()
        entry.ci_fix_dispatches = 1
        entry.ci_fix_head_sha = "abc123"
        entry.branch_head_sha = "abc123"  # unchanged since that dispatch
        events = [self._checks_failed_event(entry)]

        with patch("coord.ci_fix.dispatch_ci_fix") as dispatch:
            merge_cmd._dispatch_ci_fixes(events, two_machine_config, dry_run=False)

        dispatch.assert_not_called()
        assert entry.ci_fix_dispatches == 0
        assert entry.ci_fix_noop_streak == 1
        assert entry.ci_fix_head_sha == ""
        assert entry.state == PENDING

    def test_noop_leg_is_echoed(
        self, two_machine_config: Config, coord_db, capsys,
    ) -> None:
        from coord.commands import merge as merge_cmd
        from coord.state import save_board

        board = Board()
        save_board(board)

        entry = _entry()
        entry.ci_fix_dispatches = 1
        entry.ci_fix_head_sha = "abc123"
        entry.branch_head_sha = "abc123"
        events = [self._checks_failed_event(entry)]

        with patch("coord.ci_fix.dispatch_ci_fix"):
            merge_cmd._dispatch_ci_fixes(events, two_machine_config, dry_run=False)

        out = capsys.readouterr().out
        assert "pushed no commit" in out
        assert f"#{entry.issue_number}" in out

    def test_noop_streak_cap_escalates_to_human_required(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        """Two CONSECUTIVE no-op legs — the exact coord-portal#164 shape —
        must still reach HUMAN_REQUIRED (a human needs to look), but via
        the noop-streak cap, not by exhausting MAX_CI_FIX_DISPATCHES."""
        from coord.commands import merge as merge_cmd
        from coord.state import save_board

        board = Board()
        save_board(board)

        entry = _entry()
        entry.ci_fix_dispatches = 1
        entry.ci_fix_noop_streak = MAX_CI_FIX_NOOP_STREAK - 1
        entry.ci_fix_head_sha = "abc123"
        entry.branch_head_sha = "abc123"
        events = [self._checks_failed_event(entry)]

        with patch("coord.ci_fix.dispatch_ci_fix") as dispatch:
            merge_cmd._dispatch_ci_fixes(events, two_machine_config, dry_run=False)

        dispatch.assert_not_called()
        assert entry.ci_fix_noop_streak == MAX_CI_FIX_NOOP_STREAK
        assert entry.state == HUMAN_REQUIRED
        # #3011: never spent a real MAX_CI_FIX_DISPATCHES attempt getting
        # here — both legs were correct declines, refunded each time.
        assert entry.ci_fix_dispatches == 0

    def test_noop_streak_cap_hit_is_echoed_distinctly_from_retry_cap(
        self, two_machine_config: Config, coord_db, capsys,
    ) -> None:
        from coord.commands import merge as merge_cmd
        from coord.state import save_board

        board = Board()
        save_board(board)

        entry = _entry()
        entry.ci_fix_dispatches = 1
        entry.ci_fix_noop_streak = MAX_CI_FIX_NOOP_STREAK - 1
        entry.ci_fix_head_sha = "abc123"
        entry.branch_head_sha = "abc123"
        events = [self._checks_failed_event(entry)]

        with patch("coord.ci_fix.dispatch_ci_fix"):
            merge_cmd._dispatch_ci_fixes(events, two_machine_config, dry_run=False)

        out = capsys.readouterr().out
        assert "not attributable to this branch" in out

    def test_no_prior_dispatch_falls_through_to_normal_flow(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        """A fresh confirmed failure (no ci-fix dispatched yet for this
        streak) must never be misclassified as a no-op — `ci_fix_head_sha`
        starts empty, so `dispatch_was_noop` is False and the normal
        dispatch path runs exactly as before #3011."""
        from coord.commands import merge as merge_cmd
        from coord.state import save_board

        board = Board()
        save_board(board)

        entry = _entry()
        assert entry.ci_fix_head_sha == ""
        events = [self._checks_failed_event(entry)]

        with patch("coord.ci_fix.dispatch_ci_fix", return_value=None) as dispatch:
            merge_cmd._dispatch_ci_fixes(events, two_machine_config, dry_run=False)

        dispatch.assert_called_once()

    def test_active_fix_in_flight_is_not_treated_as_noop(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        """The blocking gap from review: a ci-fix leg stays PENDING for its
        WHOLE lifetime, so on the very next tick after dispatch — before the
        worker has had any chance to push — `entry.branch_head_sha` still
        equals `entry.ci_fix_head_sha` exactly like a genuine no-op would.
        `_has_active_fix` must be checked FIRST: while the fix worker is
        still running/pending, this must not be refunded, must not bump the
        noop streak, and must not dispatch a fresh attempt — the entry is
        left untouched for the next tick, same as the generic "already in
        flight" decline in `TestDispatchCiFixesWrapper`.
        """
        from coord.commands import merge as merge_cmd
        from coord.state import save_board

        board = Board()
        board.completed.append(_work_assignment())
        # The in-flight ci-fix worker itself — chained to the original work
        # assignment via review_of_assignment_id, same linkage
        # `dispatch_ci_fix` records (see `test_returns_none_when_a_fix_is_
        # already_in_flight` in TestDispatchCiFix).
        board.active.append(Assignment(
            machine_name="server", repo_name="api", issue_number=1,
            issue_title="[ci-fix] Fix the thing",
            assignment_id="prior-ci-fix", status="running",
            type="work", review_of_assignment_id="w1",
        ))
        save_board(board)

        entry = _entry()
        entry.ci_fix_dispatches = 1
        # The SHA the still-running leg was dispatched at — unchanged, just
        # like the genuine in-flight case, since the worker hasn't pushed
        # yet.
        entry.ci_fix_head_sha = "abc123"
        entry.branch_head_sha = "abc123"
        events = [self._checks_failed_event(entry)]

        with patch("coord.ci_fix.dispatch_ci_fix") as dispatch:
            merge_cmd._dispatch_ci_fixes(events, two_machine_config, dry_run=False)

        dispatch.assert_not_called()
        assert entry.ci_fix_dispatches == 1
        assert entry.ci_fix_noop_streak == 0
        assert entry.ci_fix_head_sha == "abc123"
        assert entry.state == PENDING

    def test_active_fix_in_flight_is_echoed(
        self, two_machine_config: Config, coord_db, capsys,
    ) -> None:
        from coord.commands import merge as merge_cmd
        from coord.state import save_board

        board = Board()
        board.completed.append(_work_assignment())
        board.active.append(Assignment(
            machine_name="server", repo_name="api", issue_number=1,
            issue_title="[ci-fix] Fix the thing",
            assignment_id="prior-ci-fix", status="running",
            type="work", review_of_assignment_id="w1",
        ))
        save_board(board)

        entry = _entry()
        entry.ci_fix_dispatches = 1
        entry.ci_fix_head_sha = "abc123"
        entry.branch_head_sha = "abc123"
        events = [self._checks_failed_event(entry)]

        with patch("coord.ci_fix.dispatch_ci_fix"):
            merge_cmd._dispatch_ci_fixes(events, two_machine_config, dry_run=False)

        out = capsys.readouterr().out
        assert "already in flight" in out
        assert f"#{entry.issue_number}" in out
