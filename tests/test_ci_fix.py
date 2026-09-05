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
from coord.ci_store import CheckRun, CIFailureDetail, JobRun, JobStep
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

    def test_no_detail_is_byte_identical_to_pre_3114(self) -> None:
        """`detail=None` (the default) must not change the briefing at all —
        every pre-#3114 caller (and every un-updated call site) keeps
        getting exactly what it always got."""
        without_kw = build_ci_fix_briefing(
            entry=_entry(), checks_summary="x", attempt=1,
        )
        with_explicit_none = build_ci_fix_briefing(
            entry=_entry(), checks_summary="x", attempt=1, detail=None,
        )
        assert without_kw == with_explicit_none


class TestBuildBriefingWithDetail:
    """#3114: a fixture failed-run's `CIFailureDetail` — job name, failing
    step, and a bounded log excerpt — must show up in the built briefing,
    not just the one-line `checks_summary` rollup."""

    def test_names_failing_job_step_and_carries_log_excerpt(self) -> None:
        detail = CIFailureDetail(
            check_name="Test (Linux, headless)",
            job_name="Test (Linux, headless)",
            step_name="Run tests",
            log_excerpt=(
                "running suite...\n"
                "FAIL: test_i_0_ctrl_d_keys_off_last_keystroke\n"
                "AssertionError: expected buffer flush, got no-op"
            ),
            run_url="https://github.com/acme/api/actions/runs/999",
        )
        briefing = build_ci_fix_briefing(
            entry=_entry(), checks_summary="checks failed: Test (Linux, headless) (failure)",
            attempt=1, detail=detail,
        )
        assert "Test (Linux, headless)" in briefing
        assert "Run tests" in briefing
        assert "https://github.com/acme/api/actions/runs/999" in briefing
        assert "test_i_0_ctrl_d_keys_off_last_keystroke" in briefing

    def test_truncation_is_visible_in_the_text(self) -> None:
        detail = CIFailureDetail(
            check_name="Test (Linux, headless)",
            job_name="Test (Linux, headless)",
            step_name="Run tests",
            log_excerpt="...tail of a much longer log...",
            run_url="https://github.com/acme/api/actions/runs/999",
            truncated=True,
        )
        briefing = build_ci_fix_briefing(
            entry=_entry(), checks_summary="x", attempt=1, detail=detail,
        )
        assert "truncated" in briefing.lower()

    def test_no_truncation_marker_when_not_truncated(self) -> None:
        detail = CIFailureDetail(
            check_name="lint", job_name="lint", step_name="Run lint",
            log_excerpt="all fine, just short",
            run_url="https://github.com/acme/api/actions/runs/1",
            truncated=False,
        )
        briefing = build_ci_fix_briefing(
            entry=_entry(), checks_summary="x", attempt=1, detail=detail,
        )
        assert "truncated" not in briefing.lower()


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


# ── #3114: structured CI failure detail threaded into the ci-fix briefing ───


class _StubCiStoreForDetail:
    """Minimal duck-typed CiStore stand-in — only the two methods
    `build_ci_failure_detail` actually calls."""

    def __init__(self, checks: list, jobs_by_run: dict) -> None:
        self._checks = checks
        self._jobs_by_run = jobs_by_run

    def list_checks_for_pr(self, repo: str, number: int) -> list:
        return self._checks

    def list_jobs_for_run(self, repo: str, run_id: str) -> list:
        return self._jobs_by_run.get(run_id, [])


def _failed_check(*, name: str = "Test (Linux, headless)", run_id: str = "999") -> CheckRun:
    return CheckRun(
        name=name, status="completed", conclusion="failure",
        url=f"https://github.com/acme/api/actions/runs/{run_id}",
        run_id=run_id, started_at=None, completed_at=None,
    )


class TestDispatchCiFixesWithCiStore:
    """`_dispatch_ci_fixes` fetches `CIFailureDetail` at dispatch time when a
    `ci_store` is given, and passes it straight into `dispatch_ci_fix` —
    the wiring that closes #3114 (a ci-fix worker used to get only the
    one-line `checks_summary`, never the failing job/step/log)."""

    def test_fetches_and_forwards_detail_when_ci_store_given(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        from coord.commands import merge as merge_cmd
        from coord.state import save_board

        board = Board()
        # #3114 review fix: `_dispatch_ci_fixes` now only fetches CI-failure
        # detail once `coord.ci_fix.dispatch_precheck` confirms the entry is
        # otherwise dispatch-eligible — which requires the originating work
        # assignment to actually resolve on the board (see
        # `dispatch_precheck`'s docstring). An empty board made every entry
        # ineligible and this test's fetch assertion below would never fire.
        board.completed.append(_work_assignment())
        save_board(board)
        entry = _entry()
        events = [MergeEvent(entry, "checks_failed", entry.error or "")]
        check = _failed_check()
        job = JobRun(
            name=check.name, conclusion="failure", runner_name="GitHub Actions 1",
            steps=[
                JobStep(name="Set up job", conclusion="success"),
                JobStep(name="Run tests", conclusion="failure"),
            ],
            job_id="456",
        )
        ci_store = _StubCiStoreForDetail(checks=[check], jobs_by_run={"999": [job]})

        with patch(
            "coord.ci_github.github_ops.get_job_log",
            return_value="line1\nAssertionError: boom\n",
        ), patch("coord.ci_fix.dispatch_ci_fix") as dispatch:
            merge_cmd._dispatch_ci_fixes(
                events, two_machine_config, ci_store, dry_run=False,
            )

        dispatch.assert_called_once()
        detail = dispatch.call_args.kwargs["detail"]
        assert detail is not None
        assert detail.job_name == check.name
        assert detail.step_name == "Run tests"
        assert "AssertionError: boom" in detail.log_excerpt

    def test_no_ci_store_forwards_detail_none(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        from coord.commands import merge as merge_cmd
        from coord.state import save_board

        save_board(Board())
        entry = _entry()
        events = [MergeEvent(entry, "checks_failed", entry.error or "")]

        with patch("coord.ci_fix.dispatch_ci_fix") as dispatch:
            merge_cmd._dispatch_ci_fixes(events, two_machine_config, dry_run=False)

        dispatch.assert_called_once()
        assert dispatch.call_args.kwargs["detail"] is None

    def test_detail_fetch_failure_still_dispatches(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        """#3114 acceptance: a raising fetcher degrades to the plain
        checks_summary briefing (detail=None) rather than blocking dispatch."""
        from coord.commands import merge as merge_cmd
        from coord.state import save_board

        # #3114 review fix: the entry must be otherwise dispatch-eligible
        # (see `dispatch_precheck`) for `_dispatch_ci_fixes` to attempt the
        # fetch at all — an empty board would skip the fetch (and this
        # test's whole point) before the patched raising fetcher ever runs.
        board = Board()
        board.completed.append(_work_assignment())
        save_board(board)
        entry = _entry()
        events = [MergeEvent(entry, "checks_failed", entry.error or "")]
        ci_store = _StubCiStoreForDetail(checks=[], jobs_by_run={})

        with patch(
            "coord.ci_github.build_ci_failure_detail",
            side_effect=RuntimeError("gh throttled"),
        ), patch("coord.ci_fix.dispatch_ci_fix") as dispatch:
            merge_cmd._dispatch_ci_fixes(
                events, two_machine_config, ci_store, dry_run=False,
            )

        dispatch.assert_called_once()
        assert dispatch.call_args.kwargs["detail"] is None

    def test_declined_dispatch_with_ci_store_caches_detail_across_ticks(
        self, two_machine_config: Config, coord_db,
    ) -> None:
        """#3114 review fix: a dispatch declined for a reason unrelated to
        CI (budget remains, but the underlying `dispatch_ci_fix` call
        itself returns None — no capable machine, agent unreachable, the
        #2538 DB-lock-contention case) leaves the entry PENDING for the
        next tick to retry. Before this fix, EVERY such retry re-fetched
        the failing job's log from `gh` — exactly the per-tick GitHub
        probing #2989/#2988 warn against. A second tick against the SAME
        still-failing `branch_head_sha` must reuse the cached detail
        instead of calling `build_ci_failure_detail` (and the `gh api
        .../logs` fetch behind it) again."""
        from coord.commands import merge as merge_cmd
        from coord.state import save_board

        board = Board()
        board.completed.append(_work_assignment())
        save_board(board)

        entry = _entry()
        entry.branch_head_sha = "deadbeef"  # unchanged across both ticks
        check = _failed_check()
        job = JobRun(
            name=check.name, conclusion="failure", runner_name="GitHub Actions 1",
            steps=[
                JobStep(name="Set up job", conclusion="success"),
                JobStep(name="Run tests", conclusion="failure"),
            ],
            job_id="456",
        )
        ci_store = _StubCiStoreForDetail(checks=[check], jobs_by_run={"999": [job]})

        with patch(
            "coord.ci_github.github_ops.get_job_log",
            return_value="line1\nAssertionError: boom\n",
        ) as get_log, patch(
            "coord.ci_fix.dispatch_ci_fix", return_value=None,
        ) as dispatch:
            # Tick 1: dispatch declines (budget remains — see the module's
            # own `test_declined_dispatch_under_budget_leaves_entry_pending`
            # for the un-enriched twin of this scenario), but the detail
            # fetch runs because the entry is otherwise dispatch-eligible.
            events = [MergeEvent(entry, "checks_failed", entry.error or "")]
            merge_cmd._dispatch_ci_fixes(
                events, two_machine_config, ci_store, dry_run=False,
            )
            assert entry.state == PENDING
            assert get_log.call_count == 1
            first_detail = dispatch.call_args.kwargs["detail"]
            assert first_detail is not None
            assert first_detail.step_name == "Run tests"
            assert entry.ci_fix_detail_sha == "deadbeef"
            assert entry.ci_fix_detail_json is not None

            # Tick 2: same still-failing SHA, dispatch declines again — the
            # fetch must NOT run a second time.
            events = [MergeEvent(entry, "checks_failed", entry.error or "")]
            merge_cmd._dispatch_ci_fixes(
                events, two_machine_config, ci_store, dry_run=False,
            )
            assert get_log.call_count == 1, (
                "a repeat tick against the same still-failing SHA must "
                "reuse the cached detail, not re-fetch the log"
            )
            second_detail = dispatch.call_args.kwargs["detail"]
            assert second_detail == first_detail
