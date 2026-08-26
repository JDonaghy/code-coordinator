"""Tests for #1441 — the stalled-pipeline sweeper.

The auto-loop (coord.auto_loop) only reacts to review/fix TRANSITIONS: the
instant a review or fix flips to `done` during a given `coord notify` pass.
Once that transition is consumed, nothing re-examines the row — so a
precondition that lands late (a Test verdict backfilled two days after the
review completed, the vimcode #602 reference case) leaves it stranded
forever with no error and no surfacing.

`coord.notify.detect_stalled_pipeline` re-scans every *done* work chain on
the board each notify pass and flags the ones stuck on an unmet
precondition a fresh transition would already have resolved. Mirrors
`detect_needs_attention`'s contract (#846): detection + surfacing only, no
dispatch/kill/handoff, idempotent via the shared `notified` ledger.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from coord import notify as notify_mod
from coord import state as state_mod
from coord.auto_loop import LoopAction
from coord.comments import EVENT_STALLED, format_stalled_pipeline
from coord.config import Config, HealthConfig, PipelineConfig
from coord.github_ops import work_is_terminal as _real_work_is_terminal
from coord.health.checks.stalled_pipeline import (
    TERMINAL_STALL_REASONS,
    probe_stalled_pipeline,
)
from coord.health.models import HealthContext, Severity
from coord.merge_queue import CONFLICT, PENDING, QueuedMerge
from coord.models import Assignment, Board, Machine, Repo
from coord.worker_events import (
    UsageLimitKill,
    format_usage_limit_reason,
    is_usage_limit_reason,
)


# ── Fixtures / helpers ──────────────────────────────────────────────────────


@pytest.fixture
def config() -> Config:
    return Config(
        repos=[Repo(name="vimcode", github="acme/vimcode", default_branch="main")],
        machines=[
            Machine(
                name="mac-mini",
                host="mac-mini.tailnet",
                repos=["vimcode"],
                repo_paths={"vimcode": "/tmp/vimcode"},
            ),
        ],
        pipeline=PipelineConfig(default_gates=["review", "test", "merge"]),
    )


def _work(
    aid: str = "work-1",
    *,
    status: str = "done",
    test_state: str | None = None,
    provider_name: str | None = None,
    review_state: str | None = None,
    required_gates: list[str] | None = None,
    dispatched_at: float = 1000.0,
    finished_at: float | None = 1100.0,
    repo_name: str = "vimcode",
    issue_number: int = 602,
) -> Assignment:
    return Assignment(
        machine_name="mac-mini",
        repo_name=repo_name,
        issue_number=issue_number,
        issue_title="ctx_blocks_event gate uses ModalStack",
        assignment_id=aid,
        status=status,
        type="work",
        branch=f"issue-{issue_number}-fix",
        test_state=test_state,
        provider_name=provider_name,
        review_state=review_state,
        required_gates=required_gates or [],
        dispatched_at=dispatched_at,
        finished_at=finished_at,
    )


def _review(
    of_aid: str,
    *,
    aid: str = "review-1",
    status: str = "done",
    review_verdict: str | None = "request-changes",
    review_posted_at: float | None = 1150.0,
    dispatched_at: float = 1120.0,
    finished_at: float | None = 1140.0,
    repo_name: str = "vimcode",
    issue_number: int = 602,
) -> Assignment:
    return Assignment(
        machine_name="mac-mini",
        repo_name=repo_name,
        issue_number=issue_number,
        issue_title="[review] ctx_blocks_event gate uses ModalStack",
        assignment_id=aid,
        status=status,
        type="review",
        review_of_assignment_id=of_aid,
        review_verdict=review_verdict,
        review_posted_at=review_posted_at,
        dispatched_at=dispatched_at,
        finished_at=finished_at,
    )


def _fix(
    of_aid: str,
    *,
    aid: str = "fix-1",
    status: str = "done",
    dispatched_at: float = 1200.0,
    finished_at: float | None = 1300.0,
    test_state: str | None = None,
    provider_name: str | None = None,
    repo_name: str = "vimcode",
    issue_number: int = 602,
) -> Assignment:
    return Assignment(
        machine_name="mac-mini",
        repo_name=repo_name,
        issue_number=issue_number,
        issue_title="[fix-1] ctx_blocks_event gate uses ModalStack",
        assignment_id=aid,
        status=status,
        type="work",
        branch=f"issue-{issue_number}-fix",
        review_of_assignment_id=of_aid,
        review_iteration=1,
        test_state=test_state,
        provider_name=provider_name,
        dispatched_at=dispatched_at,
        finished_at=finished_at,
    )


def _board(*assignments: Assignment) -> Board:
    active = [a for a in assignments if a.status in ("running", "pending")]
    completed = [a for a in assignments if a.status not in ("running", "pending")]
    return Board(active=active, completed=completed)


def _mock_author_work(aid: str = "ma-work-1", **kwargs) -> Assignment:
    """#2302: a `type="mock-author"` (Gate A) head row — same shape as
    `_work()` but typed as a SEALED_PATH_AUTHOR_TYPES member so the
    `dispatch_stalled_pipeline_action` flag-bypass tests below can build a
    board around one without duplicating `_work`'s field list."""
    return replace(_work(aid, **kwargs), type="mock-author", branch="ms-65-gate-a")


# ── Reference fixture: vimcode #602 ──────────────────────────────────────────


def _vimcode_602_board() -> Board:
    """Board state captured 2026-07-26 for vimcode#602 — the concrete #1441
    reference instance: work done, test backfilled 'passed' two days after
    the review completed with 'request-changes', no fix ever dispatched."""
    work = _work(
        "work-602",
        status="done",
        test_state="passed",
        provider_name="claude-pty",
        review_state="dispatched",
    )
    review = _review("work-602", aid="review-602", review_verdict="request-changes")
    return _board(work, review)


class TestVimcode602ReferenceCase:
    def test_602_board_is_detected_as_stalled(self, config: Config) -> None:
        board = _vimcode_602_board()
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert len(results) == 1
        detection, work = results[0]
        assert detection.assignment_id == "work-602"
        assert detection.reason == "review_request_changes_no_fix"
        assert detection.issue_number == 602
        assert detection.repo_name == "vimcode"
        assert "review-602" in detection.detail
        assert work.assignment_id == "work-602"


# ── Candidate stall state 1: review request-changes, no fix dispatched ──────


class TestReviewRequestChangesNoFix:
    def test_flags_when_no_fix_exists(self, config: Config) -> None:
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="request-changes"),
        )
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert len(results) == 1
        assert results[0][0].reason == "review_request_changes_no_fix"

    def test_not_flagged_when_fix_already_dispatched(self, config: Config) -> None:
        """A fix worker was already dispatched for this review — being
        actively handled, not stalled."""
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="request-changes"),
            _fix("work-1", aid="fix-1", status="running", dispatched_at=1200.0),
        )
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert results == []

    def test_not_flagged_when_review_approved(self, config: Config) -> None:
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="approve"),
        )
        # approve => falls through to the merge-queue check; already queued
        # so nothing is flagged.
        queued = [QueuedMerge(
            assignment_id="work-1", repo_name="vimcode", repo_github="acme/vimcode",
            branch="issue-602-fix", target_branch="main", issue_number=602,
            issue_title="t", state=PENDING,
        )]
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=queued
        )
        assert results == []

    def test_not_flagged_while_review_still_running(self, config: Config) -> None:
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", status="running", review_verdict=None),
        )
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert results == []

    def test_superseded_head_uses_latest_fix_in_chain(self, config: Config) -> None:
        """work0 -> review1 (request-changes) -> fix1 -> review2
        (request-changes) -> no fix2. The stalled row is fix1 (the current
        head), not the original work0."""
        work0 = _work("work-0", test_state="passed", dispatched_at=1000.0, finished_at=1100.0)
        review1 = _review("work-0", aid="review-1", dispatched_at=1120.0, finished_at=1140.0)
        fix1 = _fix("work-0", aid="fix-1", dispatched_at=1200.0, finished_at=1300.0)
        review2 = _review(
            "fix-1", aid="review-2", dispatched_at=1320.0, finished_at=1340.0,
        )
        board = _board(work0, review1, fix1, review2)
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert len(results) == 1
        detection, work = results[0]
        assert detection.assignment_id == "fix-1"
        assert detection.reason == "review_request_changes_no_fix"


# ── Candidate stall state 2: review done, no verdict ever captured (#1582) ──


class TestReviewDoneNoVerdict:
    def test_flags_when_review_done_with_no_verdict(self, config: Config) -> None:
        """#1582/#812: a review that finalised `done` with `review_verdict
        IS NULL` matched none of the original three arms — this is the fix."""
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict=None),
        )
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert len(results) == 1
        detection, work = results[0]
        assert detection.reason == "review_done_no_verdict"
        assert detection.assignment_id == "work-1"
        assert "review-1" in detection.detail
        assert work.assignment_id == "work-1"

    def test_a_headless_review_is_not_blamed_on_the_closed_812(
        self, config: Config
    ) -> None:
        """#2019: the detail this arm emitted for the claude-coordinator#1956
        incident said "the session likely failed to start or exited before
        recording one (#812)". Every clause of that was wrong for the row it
        described — the session ran 392s, produced a complete 6.5KB review and
        exited 0; #812 is CLOSED; and #812 was about *interactive* reviews
        while this one was `interactive=False`. An operator who followed the
        message landed on a closed issue and a false cause.

        The headless wording now names the real class (#1956,
        END_REVIEW-without-verdict) and carries the relay command, since
        re-dispatching is explicitly the wrong move (docs/OPERATING_GOTCHAS.md:
        it re-derives a conclusion already in the log, and the drop reproduces
        at ~14%, #873).
        """
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict=None),
        )
        detection, _ = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )[0]
        assert "#812" not in detection.detail
        assert "#1956" in detection.detail
        assert "coord report-result --assignment review-1" in detection.detail

    def test_an_interactive_review_still_cites_812(self, config: Config) -> None:
        """The other half of the same fix: #812's "never started / exited
        before `coord report-result` ran" shape IS the right diagnosis for a
        `claude-pty` review, and must survive. Provider-aware, not blanket."""
        review = _review("work-1", aid="review-1", review_verdict=None)
        review.provider_name = "claude-pty"
        board = _board(_work("work-1", test_state="passed"), review)
        detection, _ = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )[0]
        assert "#812" in detection.detail
        assert "interactive" in detection.detail

    def test_not_flagged_when_review_still_running_no_verdict(
        self, config: Config
    ) -> None:
        """A verdict-less review that hasn't finished yet is not a stall —
        it's just not done. Distinguishes this arm from a live session."""
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", status="running", review_verdict=None),
        )
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert results == []

    def test_not_flagged_when_verdict_is_request_changes(self, config: Config) -> None:
        """Regression: a real `request-changes` verdict must keep going
        through `review_request_changes_no_fix`, not this arm."""
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="request-changes"),
        )
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert len(results) == 1
        assert results[0][0].reason == "review_request_changes_no_fix"

    def test_not_flagged_when_verdict_is_approve(self, config: Config) -> None:
        """Regression: a real `approve` verdict must keep going through the
        merge-queue checks, not this arm."""
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="approve"),
        )
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert len(results) == 1
        assert results[0][0].reason == "approved_not_queued"


# ── Candidate stall state 3: done, test verdict present, no review ever ────


class TestDoneNoReview:
    def test_flags_when_test_passed_and_no_review(self, config: Config) -> None:
        board = _board(_work("work-1", test_state="passed"))
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert len(results) == 1
        assert results[0][0].reason == "done_no_review"

    def test_not_flagged_when_test_verdict_still_missing(self, config: Config) -> None:
        """No review yet is EXPECTED while the test gate hasn't cleared —
        not a stall."""
        board = _board(_work("work-1", test_state=None))
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert results == []

    def test_not_flagged_for_interactive_completion(self, config: Config) -> None:
        """#555: an interactive (claude-pty) completion is excluded from
        automatic review dispatch by design — its absence isn't a bug."""
        board = _board(
            _work("work-1", test_state="passed", provider_name="claude-pty")
        )
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert results == []

    def test_not_flagged_as_done_no_review_when_review_gate_not_required(
        self, config: Config
    ) -> None:
        """No review dispatched is correct (not a stall) when "review" isn't
        even in required_gates. The row is still eligible for the
        merge-queue check (case 3) — supply a matching queue entry to
        isolate case 2's behaviour from case 3's."""
        board = _board(
            _work("work-1", test_state="passed", required_gates=["test", "merge"])
        )
        queued = [QueuedMerge(
            assignment_id="work-1", repo_name="vimcode", repo_github="acme/vimcode",
            branch="issue-602-fix", target_branch="main", issue_number=602,
            issue_title="t", state=PENDING, required_gates=["test", "merge"],
        )]
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=queued
        )
        assert results == []


# ── Candidate stall state 5: review worker died, no verdict (#1584) ────────


class TestReviewFailedNoVerdict:
    def test_flags_when_review_worker_failed_with_no_verdict(self, config: Config) -> None:
        """#1584: before this fix, a review worker that died (transient API
        error, network drop, ...) landed here unrecognised — `reason`
        stayed `None` and the row was silently skipped, disabling #1582's
        auto-recovery for exactly the failure mode #1582 was built around."""
        review = _review(
            "work-1", aid="review-1", status="failed", review_verdict=None,
        )
        review.failure_reason = "529 Overloaded"
        board = _board(_work("work-1", test_state="passed"), review)
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert len(results) == 1
        detection, work = results[0]
        assert detection.reason == "review_failed_no_verdict"
        assert work.assignment_id == "work-1"
        assert "529 Overloaded" in detection.detail

    def test_usage_limit_killed_review_is_not_flagged(self, config: Config) -> None:
        """#1461/#1584: a usage-limit kill is an account-wide exhausted
        budget, not a per-review defect. `AgentServer._reap` lands it on
        FAILED exactly like an api_error kill, so without a guard the sweep
        would spend this work row's one-shot auto-recovery on a
        `dispatch_review` guaranteed to die the same way until the reset —
        the anti-pattern `reconcile.py`'s `auto_reassign` and `drive.py`'s
        `_decide_review` are both already hardened against. Skipped at
        classification so the `notified` ledger is left untouched."""
        review = _review(
            "work-1", aid="review-1", status="failed", review_verdict=None,
        )
        review.failure_reason = format_usage_limit_reason(
            UsageLimitKill(reset_at_raw="3pm", excerpt="…hit your session limit")
        )
        assert is_usage_limit_reason(review.failure_reason)
        board = _board(_work("work-1", test_state="passed"), review)
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert results == []

    def test_usage_limit_killed_review_does_not_fall_through_to_approved(
        self, config: Config
    ) -> None:
        """Guarding the failed-review branch must not let the row slide into
        the `review is None or review.status == "done"` catch-all below it —
        that would misreport a dead review as `approved_not_queued` and
        enqueue unreviewed work for merge."""
        review = _review(
            "work-1", aid="review-1", status="failed", review_verdict="approve",
        )
        review.failure_reason = format_usage_limit_reason(
            UsageLimitKill(reset_at_raw="3pm", excerpt="…hit your session limit")
        )
        board = _board(_work("work-1", test_state="passed"), review)
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert results == []

    def test_not_flagged_when_review_still_pending(self, config: Config) -> None:
        """A review that's simply still running is not a stall — distinct
        from a review that already died."""
        board = _board(
            _work("work-1", test_state="passed"),
            Assignment(
                machine_name="mac-mini", repo_name="vimcode", issue_number=602,
                issue_title="[review]", assignment_id="review-1", status="running",
                type="review", review_of_assignment_id="work-1",
            ),
        )
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert results == []


# ── Candidate stall state 3: approved + tested, not in the merge queue ─────


class TestApprovedNotQueued:
    def test_flags_when_approved_and_not_queued(self, config: Config) -> None:
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="approve"),
        )
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert len(results) == 1
        assert results[0][0].reason == "approved_not_queued"

    def test_not_flagged_when_already_queued(self, config: Config) -> None:
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="approve"),
        )
        queued = [QueuedMerge(
            assignment_id="work-1", repo_name="vimcode", repo_github="acme/vimcode",
            branch="issue-602-fix", target_branch="main", issue_number=602,
            issue_title="t", state=PENDING,
        )]
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=queued
        )
        assert results == []

    def test_not_flagged_when_test_not_passed(self, config: Config) -> None:
        board = _board(
            _work("work-1", test_state=None),
            _review("work-1", aid="review-1", review_verdict="approve"),
        )
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert results == []

    def test_flags_fresh_approval_confirmed_via_live_branch_sha(
        self, config: Config, monkeypatch
    ) -> None:
        """#2085 (fix-iteration regression guard): a review that DOES carry
        a `review_head_sha` (essentially every real review completion,
        `coord.review`) must still be recognized as `approved_not_queued`
        when that SHA matches the branch's LIVE current head — not silently
        swallowed. `detect_stalled_pipeline` used to call
        `passes_merge_gates(work, config, board)` with no `gh_ops` at all,
        handing `has_approved_review` a raw work `Assignment` with no
        `branch_head_sha` attribute — since #2085 made an unconfirmed SHA
        fail CLOSED, that made this arm permanently unreachable for any
        review carrying a real SHA, i.e. `coord drive`'s own unattended
        recovery loop could no longer recover a perfectly good approval.
        """
        monkeypatch.setattr(
            "coord.github_ops.get_branch_sha",
            lambda repo, branch: "sha-current" if branch == "issue-602-fix" else None,
        )
        work = _work("work-1", test_state="passed")
        review = _review("work-1", aid="review-1", review_verdict="approve")
        review.review_head_sha = "sha-current"  # matches the branch's live head
        board = _board(work, review)
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert len(results) == 1
        assert results[0][0].reason == "approved_not_queued"


# ── Terminal-state guard (#522, reused not re-derived) ──────────────────────


class TestTerminalGuard:
    def test_terminal_work_never_surfaces(self, config: Config, monkeypatch) -> None:
        monkeypatch.setattr("coord.github_ops.work_is_terminal", lambda *a, **k: True)
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="request-changes"),
        )
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert results == []

    def test_terminal_cache_is_threaded_through_and_populated(
        self, config: Config
    ) -> None:
        """A caller-supplied terminal_cache is passed straight through to
        `work_is_terminal` (the #522 chokepoint pattern shared with the
        review/fix auto-loop) and ends up populated — so a caller sharing
        one cache dict across several sweep calls in the same notify pass
        gets the dedupe `work_is_terminal` itself implements."""
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="request-changes"),
        )
        cache: dict = {}
        # Bypass the autouse "always non-terminal" stub for this one test so
        # the REAL `work_is_terminal` (and its cache-population logic) runs;
        # stub its own `gh`-hitting internals instead.
        with patch("coord.github_ops.work_is_terminal", _real_work_is_terminal), \
             patch("coord.github_ops.issue_is_closed", return_value=False), \
             patch("coord.github_ops.pr_is_merged", return_value=False):
            notify_mod.detect_stalled_pipeline(
                config, board=board, merge_queue_items=[], terminal_cache=cache
            )
        # #2639: the cache key now also carries `trust_issue_closed` (default
        # True for this call site — `type='work'`, where `issue_number` is
        # the row's own deliverable).
        assert cache == {("acme/vimcode", 602, "issue-602-fix", True): False}


# ── Idempotency via the notified ledger ─────────────────────────────────────


class TestIdempotency:
    def test_already_notified_row_not_returned_again(
        self, config: Config, coord_db
    ) -> None:
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="request-changes"),
        )
        first = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert len(first) == 1
        with patch.object(notify_mod, "github_ops") as mock_gh:
            notify_mod.post_stalled_pipeline(first[0][0], config)
            assert mock_gh.post_issue_comment.called

        second = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert second == []


# ── post_stalled_pipeline ───────────────────────────────────────────────────


class TestPostStalledPipeline:
    def test_posts_comment_and_marks_notified(self, config: Config, coord_db) -> None:
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="request-changes"),
        )
        detection, _work_row = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )[0]

        with patch.object(notify_mod, "github_ops") as mock_gh:
            notify_mod.post_stalled_pipeline(detection, config)

        mock_gh.post_issue_comment.assert_called_once()
        args, _kwargs = mock_gh.post_issue_comment.call_args
        assert args[0] == "acme/vimcode"
        assert args[1] == 602
        assert "Pipeline row stalled" in args[2]

        notified = state_mod.load_notified()
        assert "work-1:stalled" in notified
        assert notified["work-1:stalled"]["event"] == EVENT_STALLED


# ── format_stalled_pipeline ──────────────────────────────────────────────────


class TestFormatStalledPipeline:
    def test_renders_reason_label_and_marker(self) -> None:
        body = format_stalled_pipeline(
            assignment_id="work-602",
            machine_name="mac-mini",
            repo_name="vimcode",
            issue_number=602,
            reason="review_request_changes_no_fix",
            detail="Review review-602 completed with request-changes...",
        )
        assert "work-602" in body
        assert "#602" in body
        assert "Review requested changes, no fix dispatched" in body
        assert f"<!-- coord:event={EVENT_STALLED}" in body

    def test_renders_review_failed_no_verdict_label(self) -> None:
        """#1584."""
        body = format_stalled_pipeline(
            assignment_id="work-602",
            machine_name="mac-mini",
            repo_name="vimcode",
            issue_number=602,
            reason="review_failed_no_verdict",
            detail="Review review-602 failed (529 Overloaded)...",
        )
        assert "Review worker failed before producing a verdict" in body


# ── Reachable from `coord notify`, not only `reconcile()` (§7) ─────────────


class TestReachableFromNotify:
    def test_run_surfaces_the_602_reference_case(
        self, config: Config, coord_db
    ) -> None:
        """`coord notify` (coord.notify.run) must reach the sweeper on its
        own — the #1441 regression class (docs/OPERATING_GOTCHAS.md §7) is
        exactly a sweeper that only ever got wired into `reconcile()`, which
        a thin-client/timer-only `coord-notify.timer` setup never calls."""
        board = _vimcode_602_board()
        state_mod.save_board(board)

        # Patch the specific posting call, not the whole `github_ops`
        # module — `detect_stalled_pipeline` also calls
        # `github_ops.work_is_terminal` (real, autouse-stubbed to False by
        # `conftest._non_terminal_work`) and a blanket module mock would
        # make that call return a truthy MagicMock, hiding every row as
        # falsely "terminal".
        with patch.object(
            notify_mod, "_agent_status", return_value={"completed": [], "active": []}
        ), patch("coord.notify.github_ops.post_issue_comment") as mock_post_comment:
            notify_mod.run(config)

        mock_post_comment.assert_called_once()
        args, _kwargs = mock_post_comment.call_args
        assert args[0] == "acme/vimcode"
        assert args[1] == 602
        assert "Pipeline row stalled" in args[2]

        notified = state_mod.load_notified()
        assert "work-602:stalled" in notified

    def test_run_is_idempotent_across_two_calls(
        self, config: Config, coord_db
    ) -> None:
        board = _vimcode_602_board()
        state_mod.save_board(board)

        with patch.object(
            notify_mod, "_agent_status", return_value={"completed": [], "active": []}
        ), patch("coord.notify.github_ops.post_issue_comment") as mock_post_comment:
            notify_mod.run(config)
            notify_mod.run(config)

        mock_post_comment.assert_called_once()

    def test_run_returns_stalled_as_fourth_tuple_element(
        self, config: Config, coord_db
    ) -> None:
        """`run()` must return the stalled detections to its caller, not just
        post a GitHub comment — the CLI (and any future board/TUI consumer)
        can only surface what it's handed back. Regression guard for the
        review finding that the sweep was invisible from `coord notify`'s own
        output even though it fired."""
        board = _vimcode_602_board()
        state_mod.save_board(board)

        with patch.object(
            notify_mod, "_agent_status", return_value={"completed": [], "active": []}
        ), patch("coord.notify.github_ops.post_issue_comment"):
            posted, stuck, needs_attention, stalled, _liveness, _phantom, _stuck_test = notify_mod.run(config)

        assert posted == []
        assert stuck == []
        assert needs_attention == []
        assert len(stalled) == 1
        assert stalled[0].assignment_id == "work-602"
        assert stalled[0].reason == "review_request_changes_no_fix"


class TestNotifyCliSurfacesStalled:
    """#1441 review finding: detection alone isn't "surfacing" — the issue's
    explicit ask was CLI + board surfacing, mirroring how `detect_needs_
    attention` results are echoed by `coord notify`'s own CLI command
    (coord/commands/lifecycle.py). Drives the actual click command, not just
    `notify.run()`, so a future refactor that stops threading the stalled
    list through the CLI fails this test rather than shipping silently."""

    def test_notify_command_echoes_stalled_detection(
        self, config: Config, coord_db, capsys, monkeypatch
    ) -> None:
        from pathlib import Path

        from coord.commands import lifecycle

        board = _vimcode_602_board()
        state_mod.save_board(board)

        monkeypatch.setattr(lifecycle, "_load_config", lambda _p: config)

        with patch.object(
            notify_mod, "_agent_status", return_value={"completed": [], "active": []}
        ), patch("coord.notify.github_ops.post_issue_comment"):
            lifecycle.notify.callback(config_path=Path("unused"))

        out = capsys.readouterr().out
        assert "stalled-pipeline detection" in out
        assert "[stalled:review_request_changes_no_fix]" in out
        assert "vimcode #602" in out
        assert "work-602" in out
        # The "no new transitions" early-return guard must account for the
        # stalled set too — a stalled-only pass must never print the
        # misleading "nothing to do" message (the review's called-out
        # failure scenario).
        assert "No new transitions to notify." not in out


# ── Candidate stall state 4: merge queue entry stuck CONFLICT (#1478) ──────


class TestMergeConflictUnresolved:
    def test_flags_rebaseable_conflict_with_no_prior_fix(self, config: Config) -> None:
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="approve"),
        )
        queued = [QueuedMerge(
            assignment_id="work-1", repo_name="vimcode", repo_github="acme/vimcode",
            branch="issue-602-fix", target_branch="main", issue_number=602,
            issue_title="t", state=CONFLICT, error="could not be rebased onto main",
        )]
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=queued
        )
        assert len(results) == 1
        assert results[0][0].reason == "merge_conflict_unresolved"

    def test_not_flagged_when_pending(self, config: Config) -> None:
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="approve"),
        )
        queued = [QueuedMerge(
            assignment_id="work-1", repo_name="vimcode", repo_github="acme/vimcode",
            branch="issue-602-fix", target_branch="main", issue_number=602,
            issue_title="t", state=PENDING,
        )]
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=queued
        )
        assert results == []

    def test_not_flagged_when_conflict_is_not_rebaseable(self, config: Config) -> None:
        """A permission/branch-protection error classifies as "human", not
        "rebaseable" — #1474's own classify-and-dispatch step marks it
        HUMAN_REQUIRED rather than retrying, so it isn't a stall a dispatch
        arm should touch."""
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="approve"),
        )
        queued = [QueuedMerge(
            assignment_id="work-1", repo_name="vimcode", repo_github="acme/vimcode",
            branch="issue-602-fix", target_branch="main", issue_number=602,
            issue_title="t", state=CONFLICT, error="required status check is missing",
        )]
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=queued
        )
        assert results == []

    def test_not_flagged_when_conflict_fix_already_active(self, config: Config) -> None:
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="approve"),
            Assignment(
                machine_name="mac-mini", repo_name="vimcode", issue_number=602,
                issue_title="[conflict-fix] t", assignment_id="cf-1", status="running",
                type="conflict-fix", review_of_assignment_id="work-1",
            ),
        )
        queued = [QueuedMerge(
            assignment_id="work-1", repo_name="vimcode", repo_github="acme/vimcode",
            branch="issue-602-fix", target_branch="main", issue_number=602,
            issue_title="t", state=CONFLICT, error="could not be rebased onto main",
        )]
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=queued
        )
        assert results == []

    def test_not_flagged_when_conflict_fix_retry_cap_hit(self, config: Config) -> None:
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="approve"),
            Assignment(
                machine_name="mac-mini", repo_name="vimcode", issue_number=602,
                issue_title="[conflict-fix] t", assignment_id="cf-1", status="failed",
                type="conflict-fix", review_of_assignment_id="work-1",
            ),
        )
        queued = [QueuedMerge(
            assignment_id="work-1", repo_name="vimcode", repo_github="acme/vimcode",
            branch="issue-602-fix", target_branch="main", issue_number=602,
            issue_title="t", state=CONFLICT, error="could not be rebased onto main",
        )]
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=queued
        )
        assert results == []


# ── #1478: the dispatch arm ─────────────────────────────────────────────────
#
# `dispatch_stalled_pipeline_action` reuses the SAME dispatch machinery the
# on-time transition would have used for each reason — these tests assert
# the routing (right reused call, right result), not the internals of the
# reused function (already covered by test_auto_loop.py / test_review.py /
# test_merge_queue.py / test_conflict_fix.py).


class TestDispatchDisabledByDefault:
    def test_dispatch_declines_when_flag_off(self, config: Config) -> None:
        """`auto_dispatch_stalled` defaults to False — detection-only,
        #1441's shipped behaviour, must be unchanged by default."""
        assert config.pipeline.auto_dispatch_stalled is False
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="approve"),
        )
        detection, work = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )[0]

        action = notify_mod.dispatch_stalled_pipeline_action(detection, work, board, config)

        assert action.kind == "disabled"


class TestDispatchPerReason:
    def test_review_request_changes_no_fix_dispatches_fix(
        self, config: Config, monkeypatch
    ) -> None:
        config.pipeline.auto_dispatch_stalled = True
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="request-changes"),
        )
        detection, work = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )[0]
        assert detection.reason == "review_request_changes_no_fix"

        stub = MagicMock(return_value=[
            LoopAction(
                kind="fix_dispatched", assignment_id="review-1",
                detail="fix worker fix-9 dispatched to mac-mini (iteration 1/5)",
            ),
        ])
        monkeypatch.setattr("coord.auto_loop.process_review_completion", stub)

        action = notify_mod.dispatch_stalled_pipeline_action(detection, work, board, config)

        assert action.kind == "fix_dispatch_attempted"
        assert "fix_dispatched" in action.detail
        stub.assert_called_once()

    def test_review_request_changes_no_fix_approved_is_not_no_action(
        self, config: Config, monkeypatch
    ) -> None:
        """#1478 review fix: `process_review_completion` resolving as
        `approved` (no fix dispatched) still mutates `board` in place
        (`review_verdict`, `work.review_state`, merge-queue refresh) per its
        own documented contract. Classifying that as `no_action` silently
        dropped the mutation — see `test_sweep_persists_approved_transition`
        for the end-to-end persistence assertion."""
        config.pipeline.auto_dispatch_stalled = True
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="request-changes"),
        )
        detection, work = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )[0]
        assert detection.reason == "review_request_changes_no_fix"

        stub = MagicMock(return_value=[
            LoopAction(
                kind="approved", assignment_id="review-1",
                detail="Review verdict: approve — pipeline advancing",
            ),
        ])
        monkeypatch.setattr("coord.auto_loop.process_review_completion", stub)

        action = notify_mod.dispatch_stalled_pipeline_action(detection, work, board, config)

        assert action.kind == "review_transition_applied"
        assert action.kind in notify_mod._STALLED_DISPATCH_KINDS
        stub.assert_called_once()

    def test_review_request_changes_no_fix_approved_with_nits_is_not_no_action(
        self, config: Config, monkeypatch
    ) -> None:
        """Same as above for the #476 advisory-only approve-with-nits gate."""
        config.pipeline.auto_dispatch_stalled = True
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="request-changes"),
        )
        detection, work = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )[0]

        stub = MagicMock(return_value=[
            LoopAction(
                kind="approved_with_nits", assignment_id="review-1",
                detail="advancing as approve-with-nits; no fix dispatched",
            ),
        ])
        monkeypatch.setattr("coord.auto_loop.process_review_completion", stub)

        action = notify_mod.dispatch_stalled_pipeline_action(detection, work, board, config)

        assert action.kind == "review_transition_applied"
        assert action.kind in notify_mod._STALLED_DISPATCH_KINDS

    def test_review_request_changes_no_fix_terminal_skip_is_not_no_action(
        self, config: Config, monkeypatch
    ) -> None:
        """`terminal_skip` still flips `work.review_state = "done"` in
        `_dispatch_fix_for_review` even though no fix is dispatched."""
        config.pipeline.auto_dispatch_stalled = True
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="request-changes"),
        )
        detection, work = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )[0]

        stub = MagicMock(return_value=[
            LoopAction(
                kind="terminal_skip", assignment_id="review-1",
                detail="issue #602 already merged/closed — no fix dispatched",
            ),
        ])
        monkeypatch.setattr("coord.auto_loop.process_review_completion", stub)

        action = notify_mod.dispatch_stalled_pipeline_action(detection, work, board, config)

        assert action.kind == "review_transition_applied"
        assert action.kind in notify_mod._STALLED_DISPATCH_KINDS

    def test_review_request_changes_no_fix_genuine_no_action_stays_no_action(
        self, config: Config, monkeypatch
    ) -> None:
        """`no_work_found`/`max_iterations`/`disabled`/`no_findings` genuinely
        do not mutate `board` — must still classify as `no_action` so a
        no-op doesn't get treated (and audited) as a real dispatch."""
        config.pipeline.auto_dispatch_stalled = True
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="request-changes"),
        )
        detection, work = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )[0]

        stub = MagicMock(return_value=[
            LoopAction(
                kind="max_iterations", assignment_id="review-1",
                detail="max_review_iterations=5 reached",
            ),
        ])
        monkeypatch.setattr("coord.auto_loop.process_review_completion", stub)

        action = notify_mod.dispatch_stalled_pipeline_action(detection, work, board, config)

        assert action.kind == "no_action"
        assert action.kind not in notify_mod._STALLED_DISPATCH_KINDS

    def test_review_done_no_verdict_recovered_verdict_dispatches_fix(
        self, config: Config, monkeypatch
    ) -> None:
        """#1582: a verdict recovered from the reviewing session's own
        transcript is run through the SAME auto-loop chokepoint a live
        review completion would use — a recovered `request-changes` still
        gets its fix worker."""
        config.pipeline.auto_dispatch_stalled = True
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict=None),
        )
        detection, work = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )[0]
        assert detection.reason == "review_done_no_verdict"

        monkeypatch.setattr(
            "coord.diagnose._recover_review_findings",
            MagicMock(return_value="request-changes"),
        )
        stub = MagicMock(return_value=[
            LoopAction(
                kind="fix_dispatched", assignment_id="review-1",
                detail="fix worker fix-9 dispatched to mac-mini (iteration 1/5)",
            ),
        ])
        monkeypatch.setattr("coord.auto_loop.process_review_completion", stub)

        action = notify_mod.dispatch_stalled_pipeline_action(detection, work, board, config)

        assert action.kind == "fix_dispatch_attempted"
        assert "recovered verdict" in action.detail
        stub.assert_called_once()

    def test_review_done_no_verdict_recovered_approve_is_not_no_action(
        self, config: Config, monkeypatch
    ) -> None:
        """A recovered `approve` (no fix dispatched) still mutates `board`
        in place via `process_review_completion` — must classify as
        `review_transition_applied`, not `no_action` (mirrors the #1478
        review fix for the `review_request_changes_no_fix` arm)."""
        config.pipeline.auto_dispatch_stalled = True
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict=None),
        )
        detection, work = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )[0]

        monkeypatch.setattr(
            "coord.diagnose._recover_review_findings", MagicMock(return_value="approve"),
        )
        stub = MagicMock(return_value=[
            LoopAction(
                kind="approved", assignment_id="review-1",
                detail="Review verdict: approve — pipeline advancing",
            ),
        ])
        monkeypatch.setattr("coord.auto_loop.process_review_completion", stub)

        action = notify_mod.dispatch_stalled_pipeline_action(detection, work, board, config)

        assert action.kind == "review_transition_applied"
        assert action.kind in notify_mod._STALLED_DISPATCH_KINDS

    def test_review_done_no_verdict_recovered_but_auto_loop_disabled(
        self, config: Config, monkeypatch
    ) -> None:
        """Recovery still counts as a real dispatch (the verdict was
        durably persisted) even when `process_review_completion` itself
        declines outright (e.g. `pipeline.auto_loop` off) — surfaced as its
        own kind rather than misreported as `no_action`."""
        config.pipeline.auto_dispatch_stalled = True
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict=None),
        )
        detection, work = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )[0]

        monkeypatch.setattr(
            "coord.diagnose._recover_review_findings", MagicMock(return_value="approve"),
        )
        monkeypatch.setattr(
            "coord.auto_loop.process_review_completion",
            MagicMock(return_value=[LoopAction(kind="disabled", assignment_id="review-1")]),
        )

        action = notify_mod.dispatch_stalled_pipeline_action(detection, work, board, config)

        assert action.kind == "review_verdict_recovered"
        assert action.kind in notify_mod._STALLED_DISPATCH_KINDS

    def test_review_done_no_verdict_resets_and_redispatches(
        self, config: Config, monkeypatch
    ) -> None:
        """#1582 core case: nothing recoverable from the transcript — reset
        the review stage (delete the review row, clear review_state) and
        re-dispatch a fresh review. Branch/commits are never touched (the
        existing `--reset` contract)."""
        config.pipeline.auto_dispatch_stalled = True
        work = _work("work-1", test_state="passed")
        review = _review("work-1", aid="review-1", review_verdict=None)
        board = _board(work, review)
        original_branch = work.branch

        detection, work_row = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )[0]
        assert detection.reason == "review_done_no_verdict"

        monkeypatch.setattr(
            "coord.diagnose._recover_review_findings", MagicMock(return_value=None),
        )
        new_review = _review(
            "work-1", aid="review-99", status="pending", review_verdict=None,
        )
        dispatch_stub = MagicMock(return_value=new_review)
        monkeypatch.setattr("coord.review.dispatch_review", dispatch_stub)

        action = notify_mod.dispatch_stalled_pipeline_action(
            detection, work_row, board, config,
        )

        assert action.kind == "review_reset_redispatched"
        assert "review-99" in action.detail
        dispatch_stub.assert_called_once()

        # The stale review row is gone from the in-memory board (mirrors the
        # DB delete `_reset_review_stage` performed) — otherwise a later
        # `write_board` would resurrect it.
        assert all(
            a.assignment_id != "review-1" for a in board.active + board.completed
        )
        assert work_row.review_state == "pending"
        assert work_row.review_verdict is None
        # Branch/commits preserved — the existing `--reset` contract.
        assert work_row.branch == original_branch

    def test_review_done_no_verdict_reset_declines_when_redispatch_fails(
        self, config: Config, monkeypatch
    ) -> None:
        """`dispatch_review` declining post-reset (no capable machine, etc.)
        must not be misreported as a successful dispatch."""
        config.pipeline.auto_dispatch_stalled = True
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict=None),
        )
        detection, work = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )[0]

        monkeypatch.setattr(
            "coord.diagnose._recover_review_findings", MagicMock(return_value=None),
        )
        monkeypatch.setattr("coord.review.dispatch_review", MagicMock(return_value=None))

        action = notify_mod.dispatch_stalled_pipeline_action(detection, work, board, config)

        assert action.kind == "no_action"
        assert action.kind not in notify_mod._STALLED_DISPATCH_KINDS

    def test_done_no_review_dispatches_review(self, config: Config, monkeypatch) -> None:
        config.pipeline.auto_dispatch_stalled = True
        board = _board(_work("work-1", test_state="passed"))
        detection, work = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )[0]
        assert detection.reason == "done_no_review"

        new_review = _review("work-1", aid="review-99", status="pending")
        stub = MagicMock(return_value=new_review)
        monkeypatch.setattr("coord.review.dispatch_review", stub)

        action = notify_mod.dispatch_stalled_pipeline_action(detection, work, board, config)

        assert action.kind == "review_dispatched"
        assert "review-99" in action.detail
        stub.assert_called_once()

    def test_review_failed_no_verdict_dispatches_a_fresh_review(
        self, config: Config, monkeypatch
    ) -> None:
        """#1584: recovery for a review worker that died with no verdict is
        identical to `done_no_review` — the underlying `work` row is still
        `status="done"`, so a fresh `dispatch_review` call is a normal,
        ungated re-dispatch."""
        config.pipeline.auto_dispatch_stalled = True
        review = _review(
            "work-1", aid="review-1", status="failed", review_verdict=None,
        )
        review.failure_reason = "529 Overloaded"
        board = _board(_work("work-1", test_state="passed"), review)
        detection, work = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )[0]
        assert detection.reason == "review_failed_no_verdict"

        new_review = _review("work-1", aid="review-99", status="pending")
        stub = MagicMock(return_value=new_review)
        monkeypatch.setattr("coord.review.dispatch_review", stub)

        action = notify_mod.dispatch_stalled_pipeline_action(detection, work, board, config)

        assert action.kind == "review_dispatched"
        assert action.kind in notify_mod._STALLED_DISPATCH_KINDS
        assert "review-99" in action.detail
        stub.assert_called_once()

    def test_review_failed_no_verdict_declines_on_a_usage_limit_kill(
        self, config: Config, monkeypatch
    ) -> None:
        """#1461/#1584 belt-and-braces: `detect_stalled_pipeline` already
        skips these rows, but `dispatch_stalled_pipeline_action` is public
        and reachable with a caller-built detection (or after a race that
        stamps the usage-limit reason between detection and dispatch). It
        must never re-dispatch into an account-wide exhausted budget."""
        config.pipeline.auto_dispatch_stalled = True
        review = _review(
            "work-1", aid="review-1", status="failed", review_verdict=None,
        )
        review.failure_reason = format_usage_limit_reason(
            UsageLimitKill(reset_at_raw="3pm", excerpt="…hit your session limit")
        )
        work = _work("work-1", test_state="passed")
        board = _board(work, review)
        detection = notify_mod.StalledDetection(
            assignment_id="work-1", machine_name="mac-mini", repo_name="vimcode",
            issue_number=602, reason="review_failed_no_verdict",
            detail="review-1 failed before producing a verdict",
        )

        stub = MagicMock(return_value=_review("work-1", aid="review-99", status="pending"))
        monkeypatch.setattr("coord.review.dispatch_review", stub)

        action = notify_mod.dispatch_stalled_pipeline_action(
            detection, work, board, config
        )

        assert action.kind == "no_action"
        assert action.kind not in notify_mod._STALLED_DISPATCH_KINDS
        assert "usage limit" in action.detail
        stub.assert_not_called()

    def test_review_failed_no_verdict_no_action_when_dispatch_declines(
        self, config: Config, monkeypatch
    ) -> None:
        config.pipeline.auto_dispatch_stalled = True
        review = _review(
            "work-1", aid="review-1", status="failed", review_verdict=None,
        )
        board = _board(_work("work-1", test_state="passed"), review)
        detection, work = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )[0]

        monkeypatch.setattr(
            "coord.review.dispatch_review", MagicMock(return_value=None)
        )
        action = notify_mod.dispatch_stalled_pipeline_action(detection, work, board, config)
        assert action.kind == "no_action"

    def test_approved_not_queued_enqueues(self, config: Config, monkeypatch) -> None:
        config.pipeline.auto_dispatch_stalled = True
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="approve"),
        )
        detection, work = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )[0]
        assert detection.reason == "approved_not_queued"

        stub = MagicMock(return_value=["work-1"])
        monkeypatch.setattr("coord.merge_queue.enqueue_approved_work", stub)

        action = notify_mod.dispatch_stalled_pipeline_action(detection, work, board, config)

        assert action.kind == "enqueued"
        stub.assert_called_once_with(config, board)

    def test_approved_not_queued_already_queued_by_earlier_row_still_reports_enqueued(
        self, config: Config, monkeypatch
    ) -> None:
        """#1478 review non-blocking finding: `enqueue_approved_work`
        bulk-enqueues every eligible row, so a *second* `approved_not_queued`
        row detected in the same sweep tick sees an empty `changed` list even
        though it genuinely was queued as a side effect of the first row's
        call. Checking queue membership directly (not just `changed`) must
        still classify this as `enqueued`, not a misleading `no_action`."""
        config.pipeline.auto_dispatch_stalled = True
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="approve"),
        )
        detection, work = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )[0]
        assert detection.reason == "approved_not_queued"

        # This call returns nothing new for work-1 (an earlier row in the
        # same tick already triggered its enqueue)...
        enqueue_stub = MagicMock(return_value=[])
        monkeypatch.setattr("coord.merge_queue.enqueue_approved_work", enqueue_stub)
        # ...but it is, in fact, already sitting in the queue.
        already_queued = [QueuedMerge(
            assignment_id="work-1", repo_name="vimcode", repo_github="acme/vimcode",
            branch="issue-602-fix", target_branch="main", issue_number=602,
            issue_title="t", state=PENDING,
        )]
        monkeypatch.setattr("coord.merge_queue.load_queue", lambda: already_queued)

        action = notify_mod.dispatch_stalled_pipeline_action(detection, work, board, config)

        assert action.kind == "enqueued"
        assert "already enqueued" in action.detail

    def test_merge_conflict_unresolved_dispatches_conflict_fix(
        self, config: Config, monkeypatch
    ) -> None:
        config.pipeline.auto_dispatch_stalled = True
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="approve"),
        )
        queued = [QueuedMerge(
            assignment_id="work-1", repo_name="vimcode", repo_github="acme/vimcode",
            branch="issue-602-fix", target_branch="main", issue_number=602,
            issue_title="t", state=CONFLICT, error="could not be rebased onto main",
        )]
        detection, work = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=queued
        )[0]
        assert detection.reason == "merge_conflict_unresolved"

        fix_assignment = Assignment(
            machine_name="mac-mini", repo_name="vimcode", issue_number=602,
            issue_title="[conflict-fix] t", assignment_id="cf-1", status="pending",
            type="conflict-fix",
        )
        stub = MagicMock(return_value=fix_assignment)
        monkeypatch.setattr("coord.conflict_fix.dispatch_conflict_fix", stub)
        monkeypatch.setattr("coord.merge_queue.load_queue", lambda: queued)

        action = notify_mod.dispatch_stalled_pipeline_action(detection, work, board, config)

        assert action.kind == "conflict_fix_dispatched"
        assert "cf-1" in action.detail
        stub.assert_called_once()


# ── #2537: merge_conflict_unresolved on a sealed-author row confined to the
# sealed acceptance paths must not spend a worker session on a guaranteed
# no-op conflict-fix dispatch ─────────────────────────────────────────────


class TestMergeConflictSealedConfinement:
    def _sealed_config(self, config: Config) -> Config:
        from coord.config import AcceptanceConfig, AcceptanceDriverConfig

        config.acceptance = AcceptanceConfig(
            drivers={"vimcode": AcceptanceDriverConfig(kind="cli-pytest", run="pytest")}
        )
        return config

    def test_dispatches_sealed_conflict_fix_when_confined_to_manifest_yml(
        self, config: Config, monkeypatch
    ) -> None:
        """#2555: a conflict confined to a milestone's `manifest.yml` is
        exactly what the sealed-aware conflict-fix branch
        (`coord.conflict_fix.dispatch_conflict_fix`'s `sealed_author`
        branch) is authorized to resolve, so this no longer skips — it
        falls through to the ordinary dispatch call, which now lands it.
        Was `test_skips_dispatch_when_conflict_confined_to_sealed_paths`
        before #2555 supplied a resolver that could actually handle this
        exact shape."""
        config.pipeline.auto_dispatch_stalled = True
        config = self._sealed_config(config)
        board = _board(
            _mock_author_work("ma-work-1", test_state="passed"),
            _review("ma-work-1", aid="ma-review-1", review_verdict="approve"),
        )
        queued = [QueuedMerge(
            assignment_id="ma-work-1", repo_name="vimcode", repo_github="acme/vimcode",
            branch="ms-65-gate-a", target_branch="main", issue_number=602,
            issue_title="t", state=CONFLICT, error="could not be rebased onto main",
        )]
        detection, work = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=queued
        )[0]
        assert detection.reason == "merge_conflict_unresolved"
        assert work.type == "mock-author"

        fix_assignment = Assignment(
            machine_name="mac-mini", repo_name="vimcode", issue_number=602,
            issue_title="[sealed-conflict-fix] t", assignment_id="cf-sealed-1",
            status="pending", type="conflict-fix",
        )
        conflict_fix_stub = MagicMock(return_value=fix_assignment)
        monkeypatch.setattr("coord.conflict_fix.dispatch_conflict_fix", conflict_fix_stub)
        monkeypatch.setattr("coord.merge_queue.load_queue", lambda: queued)
        monkeypatch.setattr(
            "coord.github_ops.get_compare_files",
            lambda repo, base, head: ["tests/acceptance/ms-4/manifest.yml"],
        )

        action = notify_mod.dispatch_stalled_pipeline_action(detection, work, board, config)

        assert action.kind == "conflict_fix_dispatched"
        conflict_fix_stub.assert_called_once()

    def test_dispatches_sealed_conflict_fix_when_branch_also_authored_a_spec_file(
        self, config: Config, monkeypatch
    ) -> None:
        """#2555 review fix: the REALISTIC shape for a test-author/mock-author
        slice is that its own branch diff contains its `manifest.yml` edit
        PLUS the new spec/test file it authored alongside it (`#132`'s
        conflict was exactly `manifest.yml` plus one new spec file) — the
        three-dot compare (`get_compare_files`) reports the WHOLE branch
        diff, not just what's actually in git-merge conflict, so this
        mixed-but-still-sealed list is the common case, not an edge case.
        Requiring every file in that superset to be a manifest.yml
        (the pre-fix `sealed_conflict_is_manifest_only` gate) rejected this
        shape outright and fell to `skipped_sealed_conflict`, defeating the
        whole point of #2555 for exactly the scenario it was filed for.
        Gating on "a manifest.yml appears somewhere in the list" instead
        must dispatch here, trusting the sealed-aware worker's own runtime
        restriction to do the precise per-file filtering."""
        config.pipeline.auto_dispatch_stalled = True
        config = self._sealed_config(config)
        board = _board(
            _mock_author_work("ma-work-1c", test_state="passed"),
            _review("ma-work-1c", aid="ma-review-1c", review_verdict="approve"),
        )
        queued = [QueuedMerge(
            assignment_id="ma-work-1c", repo_name="vimcode", repo_github="acme/vimcode",
            branch="ms-65-gate-a", target_branch="main", issue_number=602,
            issue_title="t", state=CONFLICT, error="could not be rebased onto main",
        )]
        detection, work = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=queued
        )[0]
        assert detection.reason == "merge_conflict_unresolved"
        assert work.type == "mock-author"

        fix_assignment = Assignment(
            machine_name="mac-mini", repo_name="vimcode", issue_number=602,
            issue_title="[sealed-conflict-fix] t", assignment_id="cf-sealed-1c",
            status="pending", type="conflict-fix",
        )
        conflict_fix_stub = MagicMock(return_value=fix_assignment)
        monkeypatch.setattr("coord.conflict_fix.dispatch_conflict_fix", conflict_fix_stub)
        monkeypatch.setattr("coord.merge_queue.load_queue", lambda: queued)
        monkeypatch.setattr(
            "coord.github_ops.get_compare_files",
            lambda repo, base, head: [
                "tests/acceptance/ms-4/manifest.yml",
                "tests/acceptance/ms-4/new_spec.rs",
            ],
        )

        action = notify_mod.dispatch_stalled_pipeline_action(detection, work, board, config)

        assert action.kind == "conflict_fix_dispatched"
        conflict_fix_stub.assert_called_once()

    def test_skips_dispatch_when_conflict_touches_sealed_path_outside_manifest(
        self, config: Config, monkeypatch
    ) -> None:
        """#2555: a conflict confined to the sealed tree but reaching beyond
        `manifest.yml` (here, a test body) is still out of the sealed
        conflict-fix branch's authority — a worker dispatched for it would
        push nothing, so this stays skipped exactly like the pre-#2555
        behavior for the manifest-only case."""
        config.pipeline.auto_dispatch_stalled = True
        config = self._sealed_config(config)
        board = _board(
            _mock_author_work("ma-work-1b", test_state="passed"),
            _review("ma-work-1b", aid="ma-review-1b", review_verdict="approve"),
        )
        queued = [QueuedMerge(
            assignment_id="ma-work-1b", repo_name="vimcode", repo_github="acme/vimcode",
            branch="ms-65-gate-a", target_branch="main", issue_number=602,
            issue_title="t", state=CONFLICT, error="could not be rebased onto main",
        )]
        detection, work = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=queued
        )[0]
        assert detection.reason == "merge_conflict_unresolved"
        assert work.type == "mock-author"

        conflict_fix_stub = MagicMock()
        monkeypatch.setattr("coord.conflict_fix.dispatch_conflict_fix", conflict_fix_stub)
        monkeypatch.setattr("coord.merge_queue.load_queue", lambda: queued)
        monkeypatch.setattr(
            "coord.github_ops.get_compare_files",
            lambda repo, base, head: ["tests/acceptance/ms-4/audit_test.rs"],
        )

        action = notify_mod.dispatch_stalled_pipeline_action(detection, work, board, config)

        assert action.kind == "skipped_sealed_conflict"
        assert "tests/acceptance/ms-4/audit_test.rs" in action.detail
        assert action.kind not in notify_mod._STALLED_DISPATCH_KINDS
        conflict_fix_stub.assert_not_called()

    def test_falls_back_to_conflict_fix_when_conflict_touches_outside_sealed_paths(
        self, config: Config, monkeypatch
    ) -> None:
        config.pipeline.auto_dispatch_stalled = True
        config = self._sealed_config(config)
        board = _board(
            _mock_author_work("ma-work-2", test_state="passed"),
            _review("ma-work-2", aid="ma-review-2", review_verdict="approve"),
        )
        queued = [QueuedMerge(
            assignment_id="ma-work-2", repo_name="vimcode", repo_github="acme/vimcode",
            branch="ms-65-gate-a", target_branch="main", issue_number=602,
            issue_title="t", state=CONFLICT, error="could not be rebased onto main",
        )]
        detection, work = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=queued
        )[0]

        fix_assignment = Assignment(
            machine_name="mac-mini", repo_name="vimcode", issue_number=602,
            issue_title="[conflict-fix] t", assignment_id="cf-2", status="pending",
            type="conflict-fix",
        )
        conflict_fix_stub = MagicMock(return_value=fix_assignment)
        monkeypatch.setattr("coord.conflict_fix.dispatch_conflict_fix", conflict_fix_stub)
        monkeypatch.setattr("coord.merge_queue.load_queue", lambda: queued)
        monkeypatch.setattr(
            "coord.github_ops.get_compare_files",
            lambda repo, base, head: [
                "tests/acceptance/ms-4/manifest.yml", "coord/some_module.py",
            ],
        )

        action = notify_mod.dispatch_stalled_pipeline_action(detection, work, board, config)

        assert action.kind == "conflict_fix_dispatched"
        conflict_fix_stub.assert_called_once()

    def test_falls_back_to_conflict_fix_when_compare_api_fails(
        self, config: Config, monkeypatch
    ) -> None:
        config.pipeline.auto_dispatch_stalled = True
        config = self._sealed_config(config)
        board = _board(
            _mock_author_work("ma-work-3", test_state="passed"),
            _review("ma-work-3", aid="ma-review-3", review_verdict="approve"),
        )
        queued = [QueuedMerge(
            assignment_id="ma-work-3", repo_name="vimcode", repo_github="acme/vimcode",
            branch="ms-65-gate-a", target_branch="main", issue_number=602,
            issue_title="t", state=CONFLICT, error="could not be rebased onto main",
        )]
        detection, work = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=queued
        )[0]

        fix_assignment = Assignment(
            machine_name="mac-mini", repo_name="vimcode", issue_number=602,
            issue_title="[conflict-fix] t", assignment_id="cf-3", status="pending",
            type="conflict-fix",
        )
        conflict_fix_stub = MagicMock(return_value=fix_assignment)
        monkeypatch.setattr("coord.conflict_fix.dispatch_conflict_fix", conflict_fix_stub)
        monkeypatch.setattr("coord.merge_queue.load_queue", lambda: queued)
        monkeypatch.setattr(
            "coord.github_ops.get_compare_files", lambda repo, base, head: None,
        )

        action = notify_mod.dispatch_stalled_pipeline_action(detection, work, board, config)

        assert action.kind == "conflict_fix_dispatched"
        conflict_fix_stub.assert_called_once()

    def test_plain_work_row_never_gets_the_sealed_confinement_check(
        self, config: Config, monkeypatch
    ) -> None:
        """The confinement check only applies to SEALED_PATH_AUTHOR_TYPES —
        a plain `work` row always goes straight to `dispatch_conflict_fix`,
        even when its conflict happens to be confined to sealed paths (a
        `work` row touching sealed paths at all is already a scope
        violation caught elsewhere, not something to special-case here)."""
        config.pipeline.auto_dispatch_stalled = True
        config = self._sealed_config(config)
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="approve"),
        )
        queued = [QueuedMerge(
            assignment_id="work-1", repo_name="vimcode", repo_github="acme/vimcode",
            branch="issue-602-fix", target_branch="main", issue_number=602,
            issue_title="t", state=CONFLICT, error="could not be rebased onto main",
        )]
        detection, work = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=queued
        )[0]
        assert work.type == "work"

        fix_assignment = Assignment(
            machine_name="mac-mini", repo_name="vimcode", issue_number=602,
            issue_title="[conflict-fix] t", assignment_id="cf-4", status="pending",
            type="conflict-fix",
        )
        conflict_fix_stub = MagicMock(return_value=fix_assignment)
        get_compare_files_stub = MagicMock(
            return_value=["tests/acceptance/ms-4/manifest.yml"]
        )
        monkeypatch.setattr("coord.conflict_fix.dispatch_conflict_fix", conflict_fix_stub)
        monkeypatch.setattr("coord.merge_queue.load_queue", lambda: queued)
        monkeypatch.setattr("coord.github_ops.get_compare_files", get_compare_files_stub)

        action = notify_mod.dispatch_stalled_pipeline_action(detection, work, board, config)

        assert action.kind == "conflict_fix_dispatched"
        conflict_fix_stub.assert_called_once()
        get_compare_files_stub.assert_not_called()


# ── #2302: sealed-author rows bypass the auto_dispatch_stalled flag ─────────
#
# `pipeline.auto_dispatch_stalled` bounds blast radius on rows another loop
# already owns (`coord drive`'s request-changes -> fix arm for `work` rows,
# #1692). A `test-author`/`mock-author` row has no such owner — `coord
# drive` explicitly `_die()`s on those and Gate A is dispatched standalone
# with no drive run over it at all (#2289) — so THIS one reason+type
# combination must dispatch regardless of the flag. `work` rows under the
# same reason keep the opt-in gate unchanged.
#
# #2537 extends the SAME bypass to `merge_conflict_unresolved` on these row
# types — see `TestDispatchSealedAuthorBypassesFlagCoversMergeConflict`
# below.


class TestDispatchSealedAuthorBypassesFlag:
    def test_mock_author_stall_dispatches_with_flag_off(
        self, config: Config, monkeypatch
    ) -> None:
        assert config.pipeline.auto_dispatch_stalled is False
        board = _board(
            _mock_author_work("ma-work-1", test_state="passed"),
            _review("ma-work-1", aid="ma-review-1", review_verdict="request-changes"),
        )
        detection, work = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )[0]
        assert detection.reason == "review_request_changes_no_fix"
        assert work.type == "mock-author"

        stub = MagicMock(return_value=[
            LoopAction(
                kind="fix_dispatched", assignment_id="ma-review-1",
                detail="fix worker fix-9 dispatched to mac-mini (iteration 1/5)",
            ),
        ])
        monkeypatch.setattr("coord.auto_loop.process_review_completion", stub)

        action = notify_mod.dispatch_stalled_pipeline_action(detection, work, board, config)

        assert action.kind == "fix_dispatch_attempted"
        stub.assert_called_once()

    def test_test_author_stall_dispatches_with_flag_off(
        self, config: Config, monkeypatch
    ) -> None:
        """The scope is BOTH sealed-author types — test-author has the
        identical hole (`coord/drive.py` `_die()`s on it too)."""
        assert config.pipeline.auto_dispatch_stalled is False
        board = _board(
            replace(_mock_author_work("ta-work-1", test_state="passed"), type="test-author"),
            _review("ta-work-1", aid="ta-review-1", review_verdict="request-changes"),
        )
        detection, work = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )[0]
        assert detection.reason == "review_request_changes_no_fix"
        assert work.type == "test-author"

        stub = MagicMock(return_value=[
            LoopAction(
                kind="fix_dispatched", assignment_id="ta-review-1",
                detail="fix worker fix-9 dispatched to mac-mini (iteration 1/5)",
            ),
        ])
        monkeypatch.setattr("coord.auto_loop.process_review_completion", stub)

        action = notify_mod.dispatch_stalled_pipeline_action(detection, work, board, config)

        assert action.kind == "fix_dispatch_attempted"
        stub.assert_called_once()

    def test_work_stall_still_disabled_with_flag_off(
        self, config: Config, monkeypatch
    ) -> None:
        """Regression guard: a plain `work` row under the identical reason
        stays gated behind the flag — `coord drive` owns that row and a
        second dispatcher racing it would duplicate/clobber its fix."""
        assert config.pipeline.auto_dispatch_stalled is False
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="request-changes"),
        )
        detection, work = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )[0]
        assert detection.reason == "review_request_changes_no_fix"
        assert work.type == "work"

        stub = MagicMock()
        monkeypatch.setattr("coord.auto_loop.process_review_completion", stub)

        action = notify_mod.dispatch_stalled_pipeline_action(detection, work, board, config)

        assert action.kind == "disabled"
        stub.assert_not_called()

    def test_mock_author_stall_still_respects_live_session_guard(
        self, config: Config, monkeypatch
    ) -> None:
        """The flag bypass must not skip the #602 live-session guard — an
        interactive session on the same (repo, issue) still wins."""
        assert config.pipeline.auto_dispatch_stalled is False
        work = _mock_author_work("ma-work-2", test_state="passed")
        review = _review("ma-work-2", aid="ma-review-2", review_verdict="request-changes")
        live_session = Assignment(
            machine_name="mac-mini", repo_name="vimcode", issue_number=602,
            issue_title="interactive smoke", assignment_id="smoke-live",
            status="running", type="smoke",
        )
        board = Board(active=[live_session], completed=[work, review])

        detection, work_row = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )[0]
        assert detection.reason == "review_request_changes_no_fix"

        stub = MagicMock()
        monkeypatch.setattr("coord.auto_loop.process_review_completion", stub)

        action = notify_mod.dispatch_stalled_pipeline_action(
            detection, work_row, board, config
        )

        assert action.kind == "skipped_live_session"
        stub.assert_not_called()

    def test_mock_author_stall_still_respects_iteration_cap(
        self, config: Config, monkeypatch
    ) -> None:
        """The flag bypass must not skip `process_review_completion`'s own
        `max_review_iterations` cap — a `max_iterations` resolution stays
        `no_action`, not a dispatch."""
        assert config.pipeline.auto_dispatch_stalled is False
        board = _board(
            _mock_author_work("ma-work-3", test_state="passed"),
            _review("ma-work-3", aid="ma-review-3", review_verdict="request-changes"),
        )
        detection, work = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )[0]

        stub = MagicMock(return_value=[
            LoopAction(
                kind="max_iterations", assignment_id="ma-review-3",
                detail="max_review_iterations=5 reached",
            ),
        ])
        monkeypatch.setattr("coord.auto_loop.process_review_completion", stub)

        action = notify_mod.dispatch_stalled_pipeline_action(detection, work, board, config)

        assert action.kind == "no_action"
        assert action.kind not in notify_mod._STALLED_DISPATCH_KINDS
        stub.assert_called_once()


class TestDispatchSealedAuthorBypassesFlagCoversMergeConflict:
    """#2537: the #2302 bypass now also fires for `merge_conflict_unresolved`
    on a SEALED_PATH_AUTHOR_TYPES row — the identical shape (`coord drive`
    `_die()`s on these row types before ever reaching a merge, so nothing
    else owns the stall)."""

    def test_mock_author_conflict_stall_dispatches_with_flag_off(
        self, config: Config, monkeypatch
    ) -> None:
        assert config.pipeline.auto_dispatch_stalled is False
        board = _board(
            _mock_author_work("ma-work-4", test_state="passed"),
            _review("ma-work-4", aid="ma-review-4", review_verdict="approve"),
        )
        queued = [QueuedMerge(
            assignment_id="ma-work-4", repo_name="vimcode", repo_github="acme/vimcode",
            branch="ms-65-gate-a", target_branch="main", issue_number=602,
            issue_title="t", state=CONFLICT, error="could not be rebased onto main",
        )]
        detection, work = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=queued
        )[0]
        assert detection.reason == "merge_conflict_unresolved"
        assert work.type == "mock-author"

        fix_assignment = Assignment(
            machine_name="mac-mini", repo_name="vimcode", issue_number=602,
            issue_title="[conflict-fix] t", assignment_id="cf-5", status="pending",
            type="conflict-fix",
        )
        stub = MagicMock(return_value=fix_assignment)
        monkeypatch.setattr("coord.conflict_fix.dispatch_conflict_fix", stub)
        monkeypatch.setattr("coord.merge_queue.load_queue", lambda: queued)

        action = notify_mod.dispatch_stalled_pipeline_action(detection, work, board, config)

        assert action.kind == "conflict_fix_dispatched"
        stub.assert_called_once()

    def test_test_author_conflict_stall_dispatches_with_flag_off(
        self, config: Config, monkeypatch
    ) -> None:
        assert config.pipeline.auto_dispatch_stalled is False
        board = _board(
            replace(_mock_author_work("ta-work-4", test_state="passed"), type="test-author"),
            _review("ta-work-4", aid="ta-review-4", review_verdict="approve"),
        )
        queued = [QueuedMerge(
            assignment_id="ta-work-4", repo_name="vimcode", repo_github="acme/vimcode",
            branch="ms-65-gate-a", target_branch="main", issue_number=602,
            issue_title="t", state=CONFLICT, error="could not be rebased onto main",
        )]
        detection, work = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=queued
        )[0]
        assert detection.reason == "merge_conflict_unresolved"
        assert work.type == "test-author"

        fix_assignment = Assignment(
            machine_name="mac-mini", repo_name="vimcode", issue_number=602,
            issue_title="[conflict-fix] t", assignment_id="cf-6", status="pending",
            type="conflict-fix",
        )
        stub = MagicMock(return_value=fix_assignment)
        monkeypatch.setattr("coord.conflict_fix.dispatch_conflict_fix", stub)
        monkeypatch.setattr("coord.merge_queue.load_queue", lambda: queued)

        action = notify_mod.dispatch_stalled_pipeline_action(detection, work, board, config)

        assert action.kind == "conflict_fix_dispatched"
        stub.assert_called_once()

    def test_work_conflict_stall_still_disabled_with_flag_off(
        self, config: Config, monkeypatch
    ) -> None:
        """Regression guard: a plain `work` row under the identical reason
        stays gated behind the flag — unchanged from before #2537."""
        assert config.pipeline.auto_dispatch_stalled is False
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="approve"),
        )
        queued = [QueuedMerge(
            assignment_id="work-1", repo_name="vimcode", repo_github="acme/vimcode",
            branch="issue-602-fix", target_branch="main", issue_number=602,
            issue_title="t", state=CONFLICT, error="could not be rebased onto main",
        )]
        detection, work = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=queued
        )[0]
        assert work.type == "work"

        stub = MagicMock()
        monkeypatch.setattr("coord.conflict_fix.dispatch_conflict_fix", stub)
        monkeypatch.setattr("coord.merge_queue.load_queue", lambda: queued)

        action = notify_mod.dispatch_stalled_pipeline_action(detection, work, board, config)

        assert action.kind == "disabled"
        stub.assert_not_called()


class TestLiveSessionGuard:
    def test_dispatch_skipped_when_live_session_active(
        self, config: Config, monkeypatch
    ) -> None:
        """#602: never act on a row with a live (running/pending) session
        for the same (repo, issue) — an interactive smoke/fix/review
        session may be mid-flight and racing an auto-dispatch would
        duplicate or clobber it. `smoke` isn't a WORK_LIKE_TYPES type, so it
        doesn't change which assignment `_pipeline_heads` treats as the
        stalled row — it's purely a live-session signal."""
        config.pipeline.auto_dispatch_stalled = True
        work = _work("work-1", test_state="passed")
        review = _review("work-1", aid="review-1", review_verdict="approve")
        live_session = Assignment(
            machine_name="mac-mini", repo_name="vimcode", issue_number=602,
            issue_title="interactive smoke", assignment_id="smoke-live",
            status="running", type="smoke",
        )
        board = Board(active=[live_session], completed=[work, review])

        detections = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert len(detections) == 1
        detection, work_row = detections[0]
        assert detection.reason == "approved_not_queued"

        stub = MagicMock(return_value=["work-1"])
        monkeypatch.setattr("coord.merge_queue.enqueue_approved_work", stub)

        action = notify_mod.dispatch_stalled_pipeline_action(
            detection, work_row, board, config
        )

        assert action.kind == "skipped_live_session"
        stub.assert_not_called()


class TestSweepStalledPipeline:
    """Integration-level tests for `_sweep_stalled_pipeline` — the function
    `run()` calls, wiring detection + post + dispatch + the one-shot ledger
    together."""

    def test_dispatches_once_and_marks_notified(
        self, config: Config, coord_db, monkeypatch
    ) -> None:
        config.pipeline.auto_dispatch_stalled = True
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="approve"),
        )
        state_mod.save_board(board)

        stub = MagicMock(return_value=["work-1"])
        monkeypatch.setattr("coord.merge_queue.enqueue_approved_work", stub)

        with patch("coord.notify.github_ops.post_issue_comment") as mock_post:
            first = notify_mod._sweep_stalled_pipeline(config, terminal_cache={})

        assert len(first) == 1
        assert first[0].reason == "approved_not_queued"
        stub.assert_called_once()
        mock_post.assert_called_once()
        args, _kwargs = mock_post.call_args
        assert "auto-dispatched" in args[2].lower()

        notified = state_mod.load_notified()
        assert "work-1:stalled" in notified

    def test_second_tick_does_not_redispatch(
        self, config: Config, coord_db, monkeypatch
    ) -> None:
        config.pipeline.auto_dispatch_stalled = True
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="approve"),
        )
        state_mod.save_board(board)

        stub = MagicMock(return_value=["work-1"])
        monkeypatch.setattr("coord.merge_queue.enqueue_approved_work", stub)

        with patch("coord.notify.github_ops.post_issue_comment"):
            first = notify_mod._sweep_stalled_pipeline(config, terminal_cache={})
            second = notify_mod._sweep_stalled_pipeline(config, terminal_cache={})

        assert len(first) == 1
        assert second == []
        stub.assert_called_once()

    def test_no_dispatch_when_flag_off_default_behaviour_unchanged(
        self, config: Config, coord_db, monkeypatch
    ) -> None:
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="approve"),
        )
        state_mod.save_board(board)

        stub = MagicMock(return_value=["work-1"])
        monkeypatch.setattr("coord.merge_queue.enqueue_approved_work", stub)

        with patch("coord.notify.github_ops.post_issue_comment") as mock_post:
            posted = notify_mod._sweep_stalled_pipeline(config, terminal_cache={})

        assert len(posted) == 1
        stub.assert_not_called()
        args, _kwargs = mock_post.call_args
        assert "nothing was dispatched automatically" in args[2].lower()

    def test_sweep_persists_review_transition_applied_mutation(
        self, config: Config, coord_db, monkeypatch
    ) -> None:
        """#1478 review fix: an `approved`/`approved_with_nits`/`terminal_skip`
        resolution from `process_review_completion` (no fix dispatched) must
        still be persisted — mirrors what the real function does to `board`
        in place (`review.review_verdict`, `work.review_state = "done"`).
        Before the fix, this classified as `no_action`, `board_dirty` never
        got set, and the mutation was silently dropped even though the row
        was permanently marked notified."""
        config.pipeline.auto_dispatch_stalled = True
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="request-changes"),
        )
        state_mod.save_board(board)

        def fake_process_review_completion(review, board, config, **kwargs):
            # Mirror what the real `process_review_completion` does for an
            # `approved` resolution: mutate `board` in place and return an
            # action list with no `fix_dispatched` kind.
            work = board.find_by_id(review.review_of_assignment_id)
            work.review_state = "done"
            review.review_verdict = "approve"
            return [LoopAction(
                kind="approved", assignment_id=review.assignment_id,
                detail="Review verdict: approve — pipeline advancing",
            )]

        monkeypatch.setattr(
            "coord.auto_loop.process_review_completion", fake_process_review_completion
        )

        with patch("coord.notify.github_ops.post_issue_comment") as mock_post:
            first = notify_mod._sweep_stalled_pipeline(config, terminal_cache={})

        assert len(first) == 1
        assert first[0].reason == "review_request_changes_no_fix"
        mock_post.assert_called_once()
        args, _kwargs = mock_post.call_args
        assert "auto-dispatched" in args[2].lower()

        # The mutation must have been persisted, not silently dropped.
        persisted = state_mod.load_board()
        persisted_work = persisted.find_by_id("work-1")
        assert persisted_work.review_state == "done"

        # And it's marked notified exactly like a real dispatch — one-shot.
        notified = state_mod.load_notified()
        assert "work-1:stalled" in notified

    def test_sweep_does_not_mark_notified_on_dispatch_exception(
        self, config: Config, coord_db, monkeypatch
    ) -> None:
        """A transient failure inside `dispatch_stalled_pipeline_action`
        (e.g. an unreachable agent) must not permanently foreclose future
        retries the way a considered decline does — no comment is posted and
        the row stays off the one-shot ledger, so the next tick retries it."""
        config.pipeline.auto_dispatch_stalled = True
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="approve"),
        )
        state_mod.save_board(board)

        monkeypatch.setattr(
            "coord.merge_queue.enqueue_approved_work",
            MagicMock(side_effect=RuntimeError("agent unreachable")),
        )

        with patch("coord.notify.github_ops.post_issue_comment") as mock_post:
            first = notify_mod._sweep_stalled_pipeline(config, terminal_cache={})

        assert first == []
        mock_post.assert_not_called()
        notified = state_mod.load_notified()
        assert "work-1:stalled" not in notified

    def test_review_done_no_verdict_reset_persists_and_does_not_refire(
        self, config: Config, coord_db, monkeypatch
    ) -> None:
        """#1582 acceptance: end-to-end through the sweep — the reset is
        persisted to the canonical DB (old review row gone, work re-
        reviewable, branch untouched) and a second tick does not re-detect
        `review_done_no_verdict` while the replacement review is live (in
        fact not at all — the one-shot ledger locks the row exactly like
        every other stalled-pipeline reason)."""
        config.pipeline.auto_dispatch_stalled = True
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict=None),
        )
        state_mod.save_board(board)

        monkeypatch.setattr(
            "coord.diagnose._recover_review_findings", MagicMock(return_value=None),
        )
        new_review = _review(
            "work-1", aid="review-99", status="pending", review_verdict=None,
        )
        monkeypatch.setattr(
            "coord.review.dispatch_review", MagicMock(return_value=new_review),
        )

        with patch("coord.notify.github_ops.post_issue_comment") as mock_post:
            first = notify_mod._sweep_stalled_pipeline(config, terminal_cache={})
            second = notify_mod._sweep_stalled_pipeline(config, terminal_cache={})

        assert len(first) == 1
        assert first[0].reason == "review_done_no_verdict"
        assert second == []
        mock_post.assert_called_once()
        args, _kwargs = mock_post.call_args
        assert "auto-dispatched" in args[2].lower()

        persisted = state_mod.load_board()
        assert persisted.find_by_id("review-1") is None
        persisted_work = persisted.find_by_id("work-1")
        assert persisted_work.review_state == "pending"
        assert persisted_work.review_verdict is None
        assert persisted_work.branch == "issue-602-fix"

        notified = state_mod.load_notified()
        assert "work-1:stalled" in notified


# ── #2679: `ignore_notified` — the notified ledger must be bypassable ──────
#
# The `notified` ledger is right for the one-shot GitHub comment
# (`post_stalled_pipeline`'s caller marks it right after posting) but wrong
# for a caller that wants to re-derive live state on every call, e.g.
# `coord health`'s `stalled_pipeline` check. Without `ignore_notified`, a row
# that was announced once becomes permanently invisible to EVERY caller of
# `detect_stalled_pipeline`, not just the one-shot GitHub comment — exactly
# the #2679 incident (three rows sat stalled for 8 days after their single
# comment landed).


class TestIgnoreNotifiedLedgerBypass:
    def test_default_still_suppresses_an_already_notified_row(
        self, config: Config, coord_db
    ) -> None:
        """Unchanged default behaviour: every existing caller (the one-shot
        GitHub-comment sweep) must keep seeing nothing for a row already on
        the ledger."""
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="request-changes"),
        )
        first = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert len(first) == 1
        with patch.object(notify_mod, "github_ops") as mock_gh:
            notify_mod.post_stalled_pipeline(first[0][0], config)
            assert mock_gh.post_issue_comment.called

        second = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert second == []

    def test_ignore_notified_still_returns_an_already_notified_row(
        self, config: Config, coord_db
    ) -> None:
        """The #2679 regression guard: a row already on the `notified`
        ledger (announced once, then invisible forever under the default)
        must still be returned when the caller passes
        `ignore_notified=True` — this is exactly what lets `coord health`
        re-derive live state independent of whatever the GitHub-comment
        sweep already recorded."""
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="request-changes"),
        )
        first = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert len(first) == 1
        with patch.object(notify_mod, "github_ops") as mock_gh:
            notify_mod.post_stalled_pipeline(first[0][0], config)
            assert mock_gh.post_issue_comment.called

        # Default call still suppresses it...
        assert notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        ) == []

        # ...but `ignore_notified=True` re-derives it from live state anyway.
        again = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[], ignore_notified=True,
        )
        assert len(again) == 1
        detection, work = again[0]
        assert detection.assignment_id == "work-1"
        assert detection.reason == "review_request_changes_no_fix"
        assert work.assignment_id == "work-1"

    def test_ignore_notified_still_respects_the_terminal_guard(
        self, config: Config, monkeypatch
    ) -> None:
        """`ignore_notified` bypasses only the ledger — a terminal (closed
        issue / merged PR) row must still never surface."""
        monkeypatch.setattr("coord.github_ops.work_is_terminal", lambda *a, **k: True)
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="request-changes"),
        )
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[], ignore_notified=True,
        )
        assert results == []


# ── #2679: `coord health`'s `stalled_pipeline` check ────────────────────────


def _health_ctx(config: Config | None, now: float = 10_000.0) -> HealthContext:
    return HealthContext(
        thresholds=HealthConfig(),
        home=Path("/tmp/unused-home"),
        coord_dir=Path("/tmp/unused-home/.coord"),
        now=now,
        config=config,
    )


class TestStalledPipelineHealthCheck:
    def test_unknown_when_no_config(self) -> None:
        result = probe_stalled_pipeline(_health_ctx(None))
        assert result.severity == Severity.UNKNOWN
        assert "no coordinator.yml" in result.headroom

    def test_ok_when_nothing_stalled(self, config: Config, monkeypatch) -> None:
        monkeypatch.setattr(
            notify_mod, "detect_stalled_pipeline", lambda cfg, **kw: []
        )
        result = probe_stalled_pipeline(_health_ctx(config))
        assert result.severity == Severity.OK
        assert result.headroom == "0 stalled pipeline rows"

    def test_crit_for_a_terminal_reason(self, config: Config, monkeypatch) -> None:
        work = _work("work-1", test_state="passed")
        detection = notify_mod.StalledDetection(
            assignment_id="work-1", machine_name="mac-mini", repo_name="vimcode",
            issue_number=602, reason="review_request_changes_no_fix",
            detail="Review review-1 completed with request-changes...",
        )
        monkeypatch.setattr(
            notify_mod, "detect_stalled_pipeline",
            lambda cfg, **kw: [(detection, work)],
        )
        result = probe_stalled_pipeline(_health_ctx(config))
        assert result.severity == Severity.CRIT
        assert "1 terminal" in result.headroom
        assert "vimcode#602" in result.detail
        assert result.values["terminal"] == 1
        assert result.values["rows"][0]["reason"] == "review_request_changes_no_fix"
        assert result.values["rows"][0]["terminal"] is True

    def test_warn_for_a_transient_reason_only(self, config: Config, monkeypatch) -> None:
        work = _work("work-1", test_state="passed")
        detection = notify_mod.StalledDetection(
            assignment_id="work-1", machine_name="mac-mini", repo_name="vimcode",
            issue_number=602, reason="done_no_review",
            detail="no review dispatched",
        )
        monkeypatch.setattr(
            notify_mod, "detect_stalled_pipeline",
            lambda cfg, **kw: [(detection, work)],
        )
        result = probe_stalled_pipeline(_health_ctx(config))
        assert result.severity == Severity.WARN
        assert "1 transient" in result.headroom
        assert result.values["terminal"] == 0

    def test_unknown_when_detection_raises(self, config: Config, monkeypatch) -> None:
        def _boom(cfg, **kw):
            raise RuntimeError("board unreachable")

        monkeypatch.setattr(notify_mod, "detect_stalled_pipeline", _boom)
        result = probe_stalled_pipeline(_health_ctx(config))
        assert result.severity == Severity.UNKNOWN
        assert "board unreachable" in result.headroom

    def test_all_terminal_reasons_are_covered(self) -> None:
        """Regression guard: every reason `detect_stalled_pipeline` can
        actually emit for the three "cannot self-resolve" shapes the issue
        names must be in the CRIT set — a typo here would silently downgrade
        a real #2679 incident row to WARN."""
        assert TERMINAL_STALL_REASONS == {
            "review_request_changes_no_fix",
            "merge_conflict_unresolved",
            "review_done_no_verdict",
        }

    def test_already_notified_row_still_reports_end_to_end(
        self, config: Config, coord_db
    ) -> None:
        """The #2679 acceptance case, end to end through the real (not
        mocked) `detect_stalled_pipeline`: a row already sitting in the
        `notified` ledger — today's ledger guard means it reports nothing to
        `coord notify` — must still surface here, because the health check
        has no ledger of its own to go stale."""
        board = _vimcode_602_board()
        state_mod.save_board(board)

        with patch.object(
            notify_mod, "_agent_status", return_value={"completed": [], "active": []}
        ), patch("coord.notify.github_ops.post_issue_comment"):
            notify_mod.run(config)  # posts the one-shot comment, marks notified

        # Confirm the ledger guard really did suppress it for the ordinary path.
        assert notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        ) == []

        result = probe_stalled_pipeline(_health_ctx(config))
        assert result.severity == Severity.CRIT
        assert "vimcode#602" in result.detail
        assert result.values["rows"][0]["assignment_id"] == "work-602"

    def test_is_a_network_cost_check(self) -> None:
        """For every already-`done` head, `detect_stalled_pipeline` calls
        `github_ops.work_is_terminal` — a real `gh` CLI round-trip per row —
        so this must be excluded from the ~2s cheap-check budget. Registered
        as `cost=COST_CHEAP` (the default), this check would run silently
        inside the automatic per-agent `/health` poll, which explicitly
        calls `build_context(..., allow_network=False, ...)` to avoid doing
        network work on every 5-minute tick, and would also break the
        documented `coord health --no-network` promise (#2679 review)."""
        from coord.health import registry as reg

        assert reg.get("stalled_pipeline").cost == reg.COST_NETWORK

    def test_disappears_once_the_review_is_no_longer_stalled(
        self, config: Config, coord_db
    ) -> None:
        """The other half of the acceptance bar: once the underlying
        precondition clears (here, a fix worker gets dispatched), the row
        must stop reporting — this check has no memory of its own, so a
        resolved row simply falls out of `detect_stalled_pipeline`'s live
        scan."""
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="request-changes"),
            _fix("work-1", aid="fix-1", status="running", dispatched_at=1200.0),
        )
        state_mod.save_board(board)

        result = probe_stalled_pipeline(_health_ctx(config))
        assert result.severity == Severity.OK
        assert result.headroom == "0 stalled pipeline rows"
