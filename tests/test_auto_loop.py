"""Tests for coord/auto_loop.py — automated review → fix → re-review cycle."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import replace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from coord.auto_loop import (
    LoopAction,
    _build_fix_briefing,
    _dispatch_fix,
    _fix_model_for_iteration,
    _post_max_iterations_notice,
    process_review_completion,
    run_for_fix_transition,
    run_for_review_transition,
)
from coord.config import Config, ModelsConfig, PipelineConfig, ReviewsConfig
from coord.models import Assignment, Board, Machine, Repo
from coord.review import ReviewFindings


# ── Shared fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def repo() -> Repo:
    return Repo(name="api", github="acme/api", depends_on=[], default_branch="main")


@pytest.fixture
def machine(repo: Repo) -> Machine:
    return Machine(
        name="laptop",
        host="laptop.tail",
        capabilities=["python"],
        repos=["api"],
        repo_paths={"api": "/work/api"},
    )


@pytest.fixture
def config(repo: Repo, machine: Machine) -> Config:
    return Config(
        repos=[repo],
        machines=[machine],
        reviews=ReviewsConfig(enabled=True, auto_dispatch=True),
        pipeline=PipelineConfig(auto_loop=True, max_review_iterations=3),
    )


@pytest.fixture
def config_loop_disabled(repo: Repo, machine: Machine) -> Config:
    return Config(
        repos=[repo],
        machines=[machine],
        pipeline=PipelineConfig(auto_loop=False),
    )


@pytest.fixture
def config_path(tmp_path, coord_db):
    """Write a minimal coordinator.yml so `coord bounce` can `_load_config` it.

    `coord_db` is requested to set up the per-test SQLite home — the
    bounce CLI loads/saves the board through that DB.
    """
    _ = coord_db  # required for save_board / load_board path
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: acme/api\n    default_branch: main\n"
        "machines:\n"
        "  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
        "    repo_paths:\n      api: /work/api\n"
        "pipeline:\n  auto_loop: true\n  max_review_iterations: 3\n"
    )
    return p


def _work_assignment(
    assignment_id: str = "work-abc",
    branch: str = "issue-1-fix",
    review_iteration: int = 0,
    type: str = "work",  # noqa: A002 - matches Assignment's field name
    for_issue_number: int | None = None,
) -> Assignment:
    return Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=1,
        issue_title="Fix the thing",
        briefing="Original briefing text.",
        assignment_id=assignment_id,
        status="done",
        branch=branch,
        pr_url="https://github.com/acme/api/pull/42",
        dispatched_at=0.0,
        finished_at=1.0,
        type=type,
        review_state="dispatched",
        review_iteration=review_iteration,
        for_issue_number=for_issue_number,
    )


def _review_assignment(
    assignment_id: str = "review-xyz",
    review_of: str = "work-abc",
) -> Assignment:
    return Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=1,
        issue_title="[review] Fix the thing",
        assignment_id=assignment_id,
        status="done",
        branch="issue-1-fix",
        dispatched_at=1.0,
        finished_at=2.0,
        type="review",
        review_of_assignment_id=review_of,
    )


def _board_with(work: Assignment, review: Assignment | None = None) -> Board:
    completed = [work]
    if review is not None:
        completed.append(review)
    return Board(
        repos=[Repo(name="api", github="acme/api")],
        machines=[],
        active=[],
        completed=completed,
    )


def _approve_findings() -> ReviewFindings:
    return ReviewFindings(verdict="approve", body="LGTM — all tests pass.")


def _request_changes_findings() -> ReviewFindings:
    return ReviewFindings(
        verdict="request-changes",
        body="## Issues\n- Missing test coverage for edge case X\n- Typo in docstring",
    )


# Default non-terminal stub for the #522 guard is provided by the autouse
# `_non_terminal_work` fixture in conftest.py — tests below opt into terminal
# behaviour by patching `coord.github_ops.work_is_terminal`.


# ── Unit tests: process_review_completion ───────────────────────────────────


class TestProcessReviewCompletion:
    def test_auto_loop_disabled_returns_disabled_action(
        self, config_loop_disabled: Config
    ) -> None:
        review = _review_assignment()
        work = _work_assignment()
        board = _board_with(work, review)

        actions = process_review_completion(
            review, board, config_loop_disabled, log_path=None
        )

        assert len(actions) == 1
        assert actions[0].kind == "disabled"

    def test_no_log_path_returns_no_findings(self, config: Config) -> None:
        review = _review_assignment()
        work = _work_assignment()
        board = _board_with(work, review)

        actions = process_review_completion(
            review, board, config, log_path=None
        )

        assert len(actions) == 1
        assert actions[0].kind == "no_findings"

    def test_log_parse_fails_returns_no_findings(
        self, config: Config, tmp_path
    ) -> None:
        log_file = tmp_path / "review.log"
        log_file.write_text("No structured output here.")

        review = _review_assignment()
        work = _work_assignment()
        board = _board_with(work, review)

        actions = process_review_completion(
            review, board, config, log_path=str(log_file)
        )

        assert len(actions) == 1
        assert actions[0].kind == "no_findings"

    def test_approve_verdict_returns_approved(self, config: Config, tmp_path) -> None:
        log_file = tmp_path / "review.log"
        log_file.write_text(
            "Some preamble.\n\n"
            "REVIEW_VERDICT: approve\n"
            "REVIEW_BODY:\n"
            "LGTM — all tests pass.\n"
            "END_REVIEW\n"
        )
        review = _review_assignment()
        work = _work_assignment()
        board = _board_with(work, review)

        actions = process_review_completion(
            review, board, config, log_path=str(log_file)
        )

        assert len(actions) == 1
        assert actions[0].kind == "approved"
        # Work assignment review_state updated
        assert work.review_state == "done"

    def test_approve_verdict_skips_review_state_update_when_work_not_found(
        self, config: Config, tmp_path
    ) -> None:
        """Approved verdict with no parent assignment on board should not crash."""
        log_file = tmp_path / "review.log"
        log_file.write_text(
            "REVIEW_VERDICT: approve\nREVIEW_BODY:\nGood.\nEND_REVIEW\n"
        )
        review = _review_assignment(review_of="nonexistent-id")
        board = Board(completed=[review])

        actions = process_review_completion(
            review, board, config, log_path=str(log_file)
        )

        assert actions[0].kind == "approved"  # should not raise

    def test_approve_verdict_targets_feature_branch_for_opted_in_milestone(
        self, tmp_path, coord_db,
    ) -> None:
        """#934 review should-fix: _advance_pipeline's milestone-aware
        target_branch (coord/auto_loop.py:329-343) shipped with no test.
        A repo that opted into the git model, approving work on an issue
        that belongs to a milestone, must refresh the merge-queue entry
        with target_branch=feature/ms-NN, not default_branch."""
        from coord import merge_queue as mq

        opted_in_repo = Repo(
            name="api", github="acme/api", default_branch="main",
            develop_branch="develop",
        )
        opted_in_config = Config(
            repos=[opted_in_repo],
            machines=[Machine(
                name="laptop", host="laptop.tail", capabilities=["python"],
                repos=["api"], repo_paths={"api": "/work/api"},
            )],
            reviews=ReviewsConfig(enabled=True, auto_dispatch=True),
            pipeline=PipelineConfig(auto_loop=True, max_review_iterations=3),
        )

        log_file = tmp_path / "review.log"
        log_file.write_text(
            "REVIEW_VERDICT: approve\nREVIEW_BODY:\nLGTM.\nEND_REVIEW\n"
        )
        review = _review_assignment()
        work = _work_assignment()
        board = _board_with(work, review)

        with patch("coord.github_ops.get_issue",
                   return_value={"milestone": {"number": 9, "title": "M9"}}), \
             patch("coord.github_ops.get_branch_diff_size", return_value=0):
            actions = process_review_completion(
                review, board, opted_in_config, log_path=str(log_file)
            )

        assert actions[0].kind == "approved"
        items = mq.load_queue()
        assert len(items) == 1
        assert items[0].target_branch == "feature/ms-9"

    def test_approve_verdict_targets_default_branch_when_no_milestone(
        self, config: Config, tmp_path, coord_db,
    ) -> None:
        """Default `config` fixture's repo has no develop_branch — the
        existing (un-opted-in) behavior is unchanged."""
        from coord import merge_queue as mq

        log_file = tmp_path / "review.log"
        log_file.write_text(
            "REVIEW_VERDICT: approve\nREVIEW_BODY:\nLGTM.\nEND_REVIEW\n"
        )
        review = _review_assignment()
        work = _work_assignment()
        board = _board_with(work, review)

        with patch("coord.github_ops.get_issue") as get_issue, \
             patch("coord.github_ops.get_branch_diff_size", return_value=0):
            actions = process_review_completion(
                review, board, config, log_path=str(log_file)
            )

        get_issue.assert_not_called()
        assert actions[0].kind == "approved"
        items = mq.load_queue()
        assert len(items) == 1
        assert items[0].target_branch == "main"

    def test_request_changes_dispatches_fix_worker(
        self, config: Config, tmp_path
    ) -> None:
        log_file = tmp_path / "review.log"
        log_file.write_text(
            "REVIEW_VERDICT: request-changes\n"
            "REVIEW_BODY:\n"
            "Missing tests for edge case X.\n"
            "END_REVIEW\n"
        )
        review = _review_assignment()
        work = _work_assignment(review_iteration=0)
        board = _board_with(work, review)

        fake_agent_resp = {"id": "fix-001"}
        mock_http = MagicMock()
        mock_http.post.return_value.json.return_value = fake_agent_resp
        mock_http.post.return_value.raise_for_status = MagicMock()

        with patch("coord.auto_loop.record_dispatched_assignment"):
            actions = process_review_completion(
                review, board, config,
                log_path=str(log_file),
                http_client=mock_http,
            )

        assert len(actions) == 1
        assert actions[0].kind == "fix_dispatched"
        # Fix worker was added to board.active
        assert len(board.active) == 1
        fix = board.active[0]
        assert fix.type == "work"
        assert fix.review_iteration == 1
        assert fix.branch == "issue-1-fix"
        assert fix.review_of_assignment_id == "work-abc"

        # #target_branch: the dispatch payload MUST tell the agent to
        # check out the original work's branch.  Without this the agent
        # would derive a new branch from the `[fix-1] …` slugified
        # title and the fix commits would land on an orphan branch
        # instead of the existing PR's branch.
        call_args = mock_http.post.call_args
        sent_payload = call_args.kwargs["json"]
        assert sent_payload["target_branch"] == "issue-1-fix", (
            f"fix dispatch must pin target_branch to the original work's "
            f"branch; got {sent_payload.get('target_branch')!r}"
        )

    def test_request_changes_work_not_on_board_returns_no_work_found(
        self, config: Config, tmp_path
    ) -> None:
        log_file = tmp_path / "review.log"
        log_file.write_text(
            "REVIEW_VERDICT: request-changes\nREVIEW_BODY:\nFix things.\nEND_REVIEW\n"
        )
        review = _review_assignment(review_of="missing-id")
        board = Board(completed=[review])

        actions = process_review_completion(
            review, board, config, log_path=str(log_file)
        )

        assert actions[0].kind == "no_work_found"

    def test_max_iterations_stops_loop(self, config: Config, tmp_path) -> None:
        """When work.review_iteration == max_review_iterations, stop and notify."""
        log_file = tmp_path / "review.log"
        log_file.write_text(
            "REVIEW_VERDICT: request-changes\nREVIEW_BODY:\nStill broken.\nEND_REVIEW\n"
        )
        # work.review_iteration == max_review_iterations → next would be 4 > 3
        review = _review_assignment()
        work = _work_assignment(review_iteration=3)  # already at max
        board = _board_with(work, review)

        with patch("coord.auto_loop._post_max_iterations_notice") as mock_notice:
            actions = process_review_completion(
                review, board, config, log_path=str(log_file)
            )

        assert actions[0].kind == "max_iterations"
        mock_notice.assert_called_once()
        # No new assignment dispatched
        assert len(board.active) == 0

    def test_fix_iteration_increments_correctly(
        self, config: Config, tmp_path
    ) -> None:
        """review_iteration on the fix worker = work.review_iteration + 1."""
        log_file = tmp_path / "review.log"
        log_file.write_text(
            "REVIEW_VERDICT: request-changes\nREVIEW_BODY:\nFix again.\nEND_REVIEW\n"
        )
        # Simulate a second round: fix_1 (review_iteration=1) was just reviewed
        review = _review_assignment(assignment_id="review-2", review_of="fix-1")
        fix_1 = _work_assignment(assignment_id="fix-1", review_iteration=1)
        board = Board(completed=[fix_1, review])

        mock_http = MagicMock()
        mock_http.post.return_value.json.return_value = {"id": "fix-2"}
        mock_http.post.return_value.raise_for_status = MagicMock()

        with patch("coord.auto_loop.record_dispatched_assignment"):
            actions = process_review_completion(
                review, board, config,
                log_path=str(log_file),
                http_client=mock_http,
            )

        assert actions[0].kind == "fix_dispatched"
        fix_2 = board.active[0]
        assert fix_2.review_iteration == 2

    def test_max_iterations_boundary_last_allowed_fix(
        self, config: Config, tmp_path
    ) -> None:
        """Iteration 3 (== max) should still dispatch the fix."""
        # config.pipeline.max_review_iterations == 3
        # work.review_iteration == 2 → next is 3 == max, which is still allowed
        log_file = tmp_path / "review.log"
        log_file.write_text(
            "REVIEW_VERDICT: request-changes\nREVIEW_BODY:\nFix it.\nEND_REVIEW\n"
        )
        review = _review_assignment(assignment_id="review-3", review_of="fix-2")
        fix_2 = _work_assignment(assignment_id="fix-2", review_iteration=2)
        board = Board(completed=[fix_2, review])

        mock_http = MagicMock()
        mock_http.post.return_value.json.return_value = {"id": "fix-3"}
        mock_http.post.return_value.raise_for_status = MagicMock()

        with patch("coord.auto_loop.record_dispatched_assignment"):
            actions = process_review_completion(
                review, board, config,
                log_path=str(log_file),
                http_client=mock_http,
            )

        # iteration 3 <= max(3) → dispatch allowed
        assert actions[0].kind == "fix_dispatched"
        fix_3 = board.active[0]
        assert fix_3.review_iteration == 3

    # ── #476 decision gate: advisory-only request-changes ────────────────────

    def test_request_changes_no_blocking_advances_with_nits(
        self, config: Config, tmp_path
    ) -> None:
        """request-changes with only non-blocking findings must NOT dispatch a
        fix — it advances the pipeline as approve-with-nits (#476).

        Since #1456 the gate requires POSITIVE evidence of zero blocking
        findings: an explicit (empty) blocking section, not merely the absence
        of one.  The fixture therefore carries `## Blocking findings` / `None`
        alongside the #532-style non-blocking bullets.
        """
        log_file = tmp_path / "review.log"
        log_file.write_text(
            "REVIEW_VERDICT: request-changes\n"
            "REVIEW_BODY:\n"
            "## Blocking findings\n"
            "None — nothing here blocks the merge.\n"
            "## Minor observations (not blocking)\n"
            "- Low-value test could exercise the real handler\n"
            "- Pre-existing issue in main, not this PR\n"
            "- Spec-wording nuance\n"
            "END_REVIEW\n"
        )
        review = _review_assignment()
        work = _work_assignment(review_iteration=2)
        board = _board_with(work, review)

        mock_http = MagicMock()  # must NOT be used — no dispatch expected
        with patch("coord.auto_loop._post_advisory_nits_notice") as mock_notice:
            actions = process_review_completion(
                review, board, config,
                log_path=str(log_file),
                http_client=mock_http,
            )

        assert actions[0].kind == "approved_with_nits"
        # No fix worker dispatched.
        assert len(board.active) == 0
        mock_http.post.assert_not_called()
        # Pipeline advanced: work marked review-done, verdict recorded as approve
        # so the merge gate lets it through.
        assert work.review_state == "done"
        assert review.review_verdict == "approve"
        # #1456: the override is recorded, not silent — the reviewer's own
        # verdict stays readable alongside the coordinator's.
        assert review.review_verdict_original == "request-changes"
        assert "blocking=0" in (review.review_verdict_override_reason or "")
        # #1956: this IS a coordinator override of the reviewer's own
        # verdict — provenance must say so, with the same reason.
        assert review.verdict_source == "overridden"
        assert review.verdict_source_reason == review.review_verdict_override_reason
        mock_notice.assert_called_once()
        # The GitHub notice must name the reviewer's original verdict.
        assert mock_notice.call_args.kwargs["original_verdict"] == "request-changes"

    def test_request_changes_with_blocking_still_dispatches_fix(
        self, config: Config, tmp_path
    ) -> None:
        """A real blocking finding (alongside nits) must still dispatch a fix —
        the #476 gate only suppresses advisory-only reviews."""
        log_file = tmp_path / "review.log"
        log_file.write_text(
            "REVIEW_VERDICT: request-changes\n"
            "REVIEW_BODY:\n"
            "## Blocking\n"
            "- Silent failure: toast suppression hides the error\n"
            "## Minor observations (not blocking)\n"
            "- Comment is slightly misleading\n"
            "END_REVIEW\n"
        )
        review = _review_assignment()
        work = _work_assignment(review_iteration=0)
        board = _board_with(work, review)

        mock_http = MagicMock()
        mock_http.post.return_value.json.return_value = {"id": "fix-001"}
        mock_http.post.return_value.raise_for_status = MagicMock()

        with patch("coord.auto_loop.record_dispatched_assignment"):
            actions = process_review_completion(
                review, board, config,
                log_path=str(log_file),
                http_client=mock_http,
            )

        assert actions[0].kind == "fix_dispatched"
        assert len(board.active) == 1
        assert board.active[0].review_iteration == 1

    def test_request_changes_unparseable_counts_dispatches_fix(
        self, config: Config, tmp_path
    ) -> None:
        """When the body has no recognisable section headings, the gate cannot
        prove there are zero blocking findings → fall back to dispatching a fix
        (preserves pre-#476 behaviour, fail-safe)."""
        log_file = tmp_path / "review.log"
        log_file.write_text(
            "REVIEW_VERDICT: request-changes\n"
            "REVIEW_BODY:\n"
            "This is broken, please fix the parser.\n"
            "END_REVIEW\n"
        )
        review = _review_assignment()
        work = _work_assignment(review_iteration=0)
        board = _board_with(work, review)

        mock_http = MagicMock()
        mock_http.post.return_value.json.return_value = {"id": "fix-001"}
        mock_http.post.return_value.raise_for_status = MagicMock()

        with patch("coord.auto_loop.record_dispatched_assignment"):
            actions = process_review_completion(
                review, board, config,
                log_path=str(log_file),
                http_client=mock_http,
            )

        assert actions[0].kind == "fix_dispatched"

    # ── #1456: the gate must never read "unknown" as "zero" ─────────────────

    def test_1445_prose_request_changes_survives(
        self, config: Config, tmp_path
    ) -> None:
        """#1456 regression: the #1445 review body — a well-formed prose
        `request-changes` with no blocking heading but a nits heading that
        parses as 0 — must stay `request-changes` and dispatch a fix.

        Before the fix, `estimate_review_counts` returned
        ``blocking=None nonblocking=None nits=0``; `parsed_any` was satisfied by
        that single 0 and `bool(None)` was False, so the #476 gate rewrote the
        verdict to `approve` and marked the work merge-ready — the only
        fail-OPEN defect found on 2026-07-26.
        """
        body = (
            "The PR does what the issue asks, but two problems block it.\n\n"
            "The leaked worktree is never cleaned up when the early-exit path\n"
            "fires, which undermines the PR's core \"fail cheaply\" premise.\n"
            "The new test also reads the real ~/.claude/settings.json, so it\n"
            "will flake on machines whose settings contain certain keys — a\n"
            "real and somewhat likely occurrence given the fleet already has\n"
            "this exact pattern on dellserver.\n\n"
            "#### Nits\n"
            "Nothing worth calling out.\n\n"
            "Given the leaked-worktree bug and the test-hermeticity gap, I'm\n"
            "requesting changes rather than approving as-is.\n"
        )
        # Guard the premise: this really is the (None, None, 0) shape that used
        # to trip the gate.  If the heuristic ever changes, this assertion tells
        # the next reader the fixture stopped reproducing #1445.
        from coord.review import estimate_review_counts

        assert estimate_review_counts(body) == (None, None, 0)

        log_file = tmp_path / "review.log"
        log_file.write_text(
            f"REVIEW_VERDICT: request-changes\nREVIEW_BODY:\n{body}END_REVIEW\n"
        )
        review = _review_assignment()
        work = _work_assignment(review_iteration=0)
        board = _board_with(work, review)

        mock_http = MagicMock()
        mock_http.post.return_value.json.return_value = {"id": "fix-1445"}
        mock_http.post.return_value.raise_for_status = MagicMock()

        with patch("coord.auto_loop.record_dispatched_assignment"):
            actions = process_review_completion(
                review, board, config,
                log_path=str(log_file),
                http_client=mock_http,
            )

        assert actions[0].kind == "fix_dispatched"
        assert review.review_verdict == "request-changes"
        assert review.review_verdict_original is None
        assert review.review_verdict_override_reason is None
        # #1663 changed how "the pipeline was NOT advanced as an approval" is
        # spelled here.  This used to read `work.review_state != "done"`, on the
        # reasoning that only `_advance_pipeline` ever wrote that column — but
        # `review_state` is a *stage* marker, not a verdict, and the fix path
        # now writes it too (the second #1663 gap: a real rejection used to
        # leave the parent row at `dispatched`/NULL, illegible to `coord drive`,
        # the TUI's Review stage, and any state-derived sweep).  What #1445 is
        # actually about is the VERDICT: a prose request-changes must never be
        # rewritten to `approve` and marked merge-ready.  Assert that directly,
        # plus the merge gate it feeds — neither of which `review_state` was
        # ever an input to.
        assert work.review_verdict == "request-changes"
        from coord.merge_queue import has_approved_review

        assert not has_approved_review(work, board), (
            "#1445: a prose request-changes must not satisfy the merge gate"
        )

    def test_nits_zero_alone_never_downgrades(
        self, config: Config, tmp_path
    ) -> None:
        """#1456: a parsed count in ANY bucket other than blocking is not
        evidence about blocking findings.  Minimal form of the #1445 bug."""
        log_file = tmp_path / "review.log"
        log_file.write_text(
            "REVIEW_VERDICT: request-changes\n"
            "REVIEW_BODY:\n"
            "## Nits\n"
            "## Summary\n"
            "The retry loop swallows the timeout — please fix before merge.\n"
            "END_REVIEW\n"
        )
        review = _review_assignment()
        board = _board_with(_work_assignment(review_iteration=0), review)

        mock_http = MagicMock()
        mock_http.post.return_value.json.return_value = {"id": "fix-1"}
        mock_http.post.return_value.raise_for_status = MagicMock()

        with patch("coord.auto_loop.record_dispatched_assignment"):
            actions = process_review_completion(
                review, board, config,
                log_path=str(log_file),
                http_client=mock_http,
            )

        assert actions[0].kind == "fix_dispatched"
        assert review.review_verdict == "request-changes"

    def test_prose_findings_under_blocking_heading_dispatch_fix(
        self, config: Config, tmp_path
    ) -> None:
        """#1456: a blocking section whose findings are paragraphs rather than
        bullets counts as unreadable, not empty — the bullet counter would
        otherwise report 0 and fail open."""
        log_file = tmp_path / "review.log"
        log_file.write_text(
            "REVIEW_VERDICT: request-changes\n"
            "REVIEW_BODY:\n"
            "## Blocking findings\n"
            "The worktree created on the early-exit path is never removed, so "
            "every failed dispatch leaks a directory until the disk fills.\n"
            "## Nits\n"
            "- Comment typo\n"
            "END_REVIEW\n"
        )
        review = _review_assignment()
        board = _board_with(_work_assignment(review_iteration=0), review)

        mock_http = MagicMock()
        mock_http.post.return_value.json.return_value = {"id": "fix-1"}
        mock_http.post.return_value.raise_for_status = MagicMock()

        with patch("coord.auto_loop.record_dispatched_assignment"):
            actions = process_review_completion(
                review, board, config,
                log_path=str(log_file),
                http_client=mock_http,
            )

        assert actions[0].kind == "fix_dispatched"
        assert review.review_verdict == "request-changes"


# ── Unit tests: _build_fix_briefing ─────────────────────────────────────────


class TestTerminalGuard522:
    """#522: never dispatch a fix / re-review for work whose issue is already
    closed or whose PR is already merged — root cause of the 2026-06-09 launch
    flood (#349 ×4, #194).  The guard is fail-open, so these tests opt in by
    patching the github_ops helpers (stubbed non-terminal by default)."""

    def _request_changes_log(self, tmp_path):
        log_file = tmp_path / "review.log"
        log_file.write_text(
            "REVIEW_VERDICT: request-changes\n"
            "REVIEW_BODY:\nMissing tests.\nEND_REVIEW\n"
        )
        return log_file

    def test_skips_fix_when_issue_closed(
        self, config: Config, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr("coord.github_ops.work_is_terminal", lambda *a, **k: True)
        review = _review_assignment()
        work = _work_assignment(review_iteration=0)
        board = _board_with(work, review)

        mock_http = MagicMock()
        actions = process_review_completion(
            review, board, config,
            log_path=str(self._request_changes_log(tmp_path)),
            http_client=mock_http,
        )

        assert [a.kind for a in actions] == ["terminal_skip"]
        assert board.active == []                  # nothing dispatched
        mock_http.post.assert_not_called()         # no agent /assign POST
        assert work.review_state == "done"         # review marked resolved

    def test_skips_fix_when_pr_merged_even_if_issue_open(
        self, config: Config, tmp_path, monkeypatch
    ) -> None:
        # issue stays OPEN (default stub); only the PR is merged — the quadraui
        # develop-merge case where merging does NOT auto-close the issue.
        # (work_is_terminal collapses both signals; the github_ops unit tests
        # cover the issue-open/PR-merged split directly.)
        monkeypatch.setattr("coord.github_ops.work_is_terminal", lambda *a, **k: True)
        review = _review_assignment()
        work = _work_assignment(review_iteration=0)
        board = _board_with(work, review)

        mock_http = MagicMock()
        actions = process_review_completion(
            review, board, config,
            log_path=str(self._request_changes_log(tmp_path)),
            http_client=mock_http,
        )

        assert [a.kind for a in actions] == ["terminal_skip"]
        assert board.active == []
        mock_http.post.assert_not_called()

    def test_dispatches_fix_when_not_terminal(
        self, config: Config, tmp_path
    ) -> None:
        # Default stub → non-terminal → the normal fix dispatch still fires.
        # Regression guard: the #522 check must not block legitimate fixes.
        review = _review_assignment()
        work = _work_assignment(review_iteration=0)
        board = _board_with(work, review)

        mock_http = MagicMock()
        mock_http.post.return_value.json.return_value = {"id": "fix-001"}
        mock_http.post.return_value.raise_for_status = MagicMock()
        with patch("coord.auto_loop.record_dispatched_assignment"):
            actions = process_review_completion(
                review, board, config,
                log_path=str(self._request_changes_log(tmp_path)),
                http_client=mock_http,
            )

        assert any(a.kind == "fix_dispatched" for a in actions)
        assert len(board.active) == 1
        mock_http.post.assert_called_once()

    def test_fix_completion_skips_rereview_when_terminal(
        self, config: Config, coord_db, monkeypatch
    ) -> None:
        from coord.state import load_board, save_board

        monkeypatch.setattr("coord.github_ops.work_is_terminal", lambda *a, **k: True)

        fix = _work_assignment(assignment_id="fix-1", review_iteration=1)
        fix.issue_title = "[fix-1] Fix the thing"
        fix.review_of_assignment_id = "work-abc"
        fix.review_state = "pending"
        save_board(Board(completed=[fix]))

        dispatched: dict = {}

        def fake_dispatch_review(*a, **k):
            dispatched["called"] = True
            return None

        monkeypatch.setattr("coord.auto_loop.dispatch_review", fake_dispatch_review)

        actions = run_for_fix_transition("fix-1", config)

        assert [a.kind for a in actions] == ["terminal_skip"]
        assert "called" not in dispatched          # dispatch_review never called
        loaded = load_board()
        assert loaded is not None
        reloaded = loaded.find_by_id("fix-1")
        assert reloaded is not None
        assert reloaded.review_state == "done"     # persisted to the board

    def test_fix_completion_skips_rereview_when_interactive(
        self, config: Config, coord_db, monkeypatch
    ) -> None:
        """#555: an *interactive* (provider_name='claude-pty') fix gets its
        re-review from the human-attended TUI flow, never a headless
        `claude -p` review — so the auto-loop dispatches nothing for it."""
        from coord.state import load_board, save_board

        fix = _work_assignment(assignment_id="fix-1", review_iteration=1)
        fix.issue_title = "[fix-1] Fix the thing"
        fix.review_of_assignment_id = "work-abc"
        fix.review_state = "pending"
        fix.provider_name = "claude-pty"
        save_board(Board(completed=[fix]))

        dispatched: dict = {}

        def fake_dispatch_review(*a, **k):
            dispatched["called"] = True
            return None

        monkeypatch.setattr("coord.auto_loop.dispatch_review", fake_dispatch_review)

        actions = run_for_fix_transition("fix-1", config)

        assert [a.kind for a in actions] == ["interactive_skip"]
        assert "called" not in dispatched          # no headless review dispatched
        _ = load_board()

    # The #349-×4 cache-collapse behaviour now lives in
    # coord.github_ops.work_is_terminal and is covered by
    # tests/test_github_ops.py::TestWorkIsTerminal::test_cache_collapses_repeat_calls.


class TestBuildFixBriefing:
    def test_contains_reviewer_findings(self) -> None:
        work = _work_assignment()
        findings = _request_changes_findings()
        briefing = _build_fix_briefing(work, findings, iteration=1, max_iter=3)
        assert "Missing test coverage for edge case X" in briefing

    def test_contains_iteration_info(self) -> None:
        work = _work_assignment()
        findings = _request_changes_findings()
        briefing = _build_fix_briefing(work, findings, iteration=2, max_iter=3)
        assert "iteration 2" in briefing
        assert "3" in briefing  # max shown

    def test_contains_branch_name(self) -> None:
        work = _work_assignment(branch="issue-42-cool-feature")
        findings = _request_changes_findings()
        briefing = _build_fix_briefing(work, findings, iteration=1, max_iter=3)
        assert "issue-42-cool-feature" in briefing

    def test_contains_original_briefing(self) -> None:
        work = _work_assignment()
        findings = _request_changes_findings()
        briefing = _build_fix_briefing(work, findings, iteration=1, max_iter=3)
        assert "Original briefing text." in briefing

    def test_no_crash_when_work_has_no_briefing(self) -> None:
        work = replace(_work_assignment(), briefing="")
        findings = _request_changes_findings()
        briefing = _build_fix_briefing(work, findings, iteration=1, max_iter=3)
        assert "Original work briefing" not in briefing  # omitted when empty

    def test_contains_do_not_change_branch_instruction(self) -> None:
        work = _work_assignment()
        findings = _request_changes_findings()
        briefing = _build_fix_briefing(work, findings, iteration=1, max_iter=3)
        assert "do not change the branch name" in briefing.lower()


class TestBuildFixBriefingTestAuthor:
    """#1176: a type="test-author" source row gets test-authoring-flavored
    fix instructions instead of the generic "implement + make tests pass"
    briefing — an oracle must stay RED, not turn green."""

    def test_contains_reviewer_findings(self) -> None:
        work = _work_assignment(type="test-author")
        findings = _request_changes_findings()
        briefing = _build_fix_briefing(work, findings, iteration=1, max_iter=3)
        assert "Missing test coverage for edge case X" in briefing

    def test_does_not_instruct_making_tests_pass(self) -> None:
        """The generic fix briefing's "ensure all tests pass" is actively
        wrong guidance for an oracle that must stay RED until the real
        implementation lands — must not leak into the test-author variant."""
        work = _work_assignment(type="test-author")
        findings = _request_changes_findings()
        briefing = _build_fix_briefing(work, findings, iteration=1, max_iter=3)
        assert "ensure all tests pass" not in briefing.lower()

    def test_instructs_staying_red(self) -> None:
        work = _work_assignment(type="test-author")
        findings = _request_changes_findings()
        briefing = _build_fix_briefing(work, findings, iteration=1, max_iter=3)
        assert "red" in briefing.lower()

    def test_contains_branch_name(self) -> None:
        work = _work_assignment(type="test-author", branch="test-author-ms-3-slice-9")
        findings = _request_changes_findings()
        briefing = _build_fix_briefing(work, findings, iteration=1, max_iter=3)
        assert "test-author-ms-3-slice-9" in briefing

    def test_contains_do_not_change_branch_instruction(self) -> None:
        work = _work_assignment(type="test-author")
        findings = _request_changes_findings()
        briefing = _build_fix_briefing(work, findings, iteration=1, max_iter=3)
        assert "do not change the branch name" in briefing.lower()

    def test_contains_original_briefing(self) -> None:
        work = _work_assignment(type="test-author")
        findings = _request_changes_findings()
        briefing = _build_fix_briefing(work, findings, iteration=1, max_iter=3)
        assert "Original briefing text." in briefing

    def test_work_type_still_gets_generic_briefing(self) -> None:
        """Regression guard: a plain type="work" bounce keeps today's
        wording unchanged."""
        work = _work_assignment(type="work")
        findings = _request_changes_findings()
        briefing = _build_fix_briefing(work, findings, iteration=1, max_iter=3)
        assert "ensure all tests pass" in briefing.lower()


class TestBuildFixBriefingMockAuthor:
    """#2302: a type="mock-author" source row (Gate A) gets its own
    fix-briefing variant instead of the generic "implement + make tests
    pass" briefing — the diff is a specification (contract.md + rendered
    mocks), there is no suite to run, and the diff must stay confined to
    tests/acceptance/ms-NN/."""

    def test_contains_reviewer_findings(self) -> None:
        work = _work_assignment(type="mock-author")
        findings = _request_changes_findings()
        briefing = _build_fix_briefing(work, findings, iteration=1, max_iter=3)
        assert "Missing test coverage for edge case X" in briefing

    def test_does_not_instruct_making_tests_pass(self) -> None:
        """The generic fix briefing's "ensure all tests pass" is wrong
        guidance for a specification diff with no suite behind it — must
        not leak into the mock-author variant."""
        work = _work_assignment(type="mock-author")
        findings = _request_changes_findings()
        briefing = _build_fix_briefing(work, findings, iteration=1, max_iter=3)
        assert "ensure all tests pass" not in briefing.lower()
        assert "make them pass" not in briefing.lower()

    def test_instructs_confining_diff_to_acceptance_dir(self) -> None:
        work = _work_assignment(type="mock-author")
        findings = _request_changes_findings()
        briefing = _build_fix_briefing(work, findings, iteration=1, max_iter=3)
        assert "tests/acceptance/ms-nn" in briefing.lower()
        assert "request-changes" in briefing.lower()

    def test_instructs_contract_and_mocks_agree_both_ways(self) -> None:
        work = _work_assignment(type="mock-author")
        findings = _request_changes_findings()
        briefing = _build_fix_briefing(work, findings, iteration=1, max_iter=3)
        assert "both directions" in briefing.lower()

    def test_contains_branch_name(self) -> None:
        work = _work_assignment(type="mock-author", branch="ms-65-gate-a")
        findings = _request_changes_findings()
        briefing = _build_fix_briefing(work, findings, iteration=1, max_iter=3)
        assert "ms-65-gate-a" in briefing

    def test_contains_do_not_change_branch_instruction(self) -> None:
        work = _work_assignment(type="mock-author")
        findings = _request_changes_findings()
        briefing = _build_fix_briefing(work, findings, iteration=1, max_iter=3)
        assert "do not change the branch name" in briefing.lower()

    def test_contains_original_briefing(self) -> None:
        work = _work_assignment(type="mock-author")
        findings = _request_changes_findings()
        briefing = _build_fix_briefing(work, findings, iteration=1, max_iter=3)
        assert "Original briefing text." in briefing


# ── Unit tests: _fix_model_for_iteration ─────────────────────────────────────


def _config_with_models(
    *,
    default: str = "sonnet",
    escalation: list[str] | None = None,
    escalate_fix_model: bool = True,
) -> Config:
    """Build a Config with a tunable models ladder + escalate knob."""
    return Config(
        repos=[Repo(name="api", github="acme/api")],
        machines=[],
        models=ModelsConfig(
            default=default,
            escalation=escalation or ["haiku", "sonnet", "opus"],
        ),
        pipeline=PipelineConfig(escalate_fix_model=escalate_fix_model),
    )


class TestFixModelForIteration:
    def test_iteration_1_returns_base_default(self) -> None:
        cfg = _config_with_models(default="sonnet")
        assert _fix_model_for_iteration(cfg, 1) == "sonnet"

    def test_iteration_2_returns_next_rung(self) -> None:
        # default sonnet, ladder [haiku, sonnet, opus] → iter 2 escalates to opus
        cfg = _config_with_models(default="sonnet")
        assert _fix_model_for_iteration(cfg, 2) == "opus"

    def test_iteration_beyond_ladder_caps_at_top(self) -> None:
        cfg = _config_with_models(default="sonnet")
        # iter 3+ stays capped at the top of the ladder (opus)
        assert _fix_model_for_iteration(cfg, 3) == "opus"
        assert _fix_model_for_iteration(cfg, 10) == "opus"

    def test_escalates_one_rung_per_iteration_from_bottom(self) -> None:
        # default haiku, ladder [haiku, sonnet, opus]
        cfg = _config_with_models(default="haiku")
        assert _fix_model_for_iteration(cfg, 1) == "haiku"
        assert _fix_model_for_iteration(cfg, 2) == "sonnet"
        assert _fix_model_for_iteration(cfg, 3) == "opus"
        assert _fix_model_for_iteration(cfg, 4) == "opus"  # capped

    def test_returns_none_when_escalation_disabled(self) -> None:
        cfg = _config_with_models(escalate_fix_model=False)
        assert _fix_model_for_iteration(cfg, 1) is None
        assert _fix_model_for_iteration(cfg, 2) is None
        assert _fix_model_for_iteration(cfg, 5) is None


class TestFixModelDispatch:
    """The escalated model lands on both the POST payload and the Assignment."""

    def _dispatch(self, config: Config, tmp_path) -> tuple[Any, Any]:
        log_file = tmp_path / "review.log"
        log_file.write_text(
            "REVIEW_VERDICT: request-changes\nREVIEW_BODY:\nFix.\nEND_REVIEW\n"
        )
        review = _review_assignment()
        work = _work_assignment(review_iteration=0)
        board = _board_with(work, review)

        mock_http = MagicMock()
        mock_http.post.return_value.json.return_value = {"id": "fix-001"}
        mock_http.post.return_value.raise_for_status = MagicMock()

        with patch("coord.auto_loop.record_dispatched_assignment"):
            process_review_completion(
                review, board, config,
                log_path=str(log_file),
                http_client=mock_http,
            )

        sent_payload = mock_http.post.call_args.kwargs["json"]
        fix = board.active[0]
        return sent_payload, fix

    def test_payload_and_assignment_carry_base_model_on_first_fix(
        self, tmp_path
    ) -> None:
        cfg = Config(
            repos=[Repo(name="api", github="acme/api", default_branch="main")],
            machines=[
                Machine(
                    name="laptop", host="laptop.tail",
                    repos=["api"], repo_paths={"api": "/work/api"},
                )
            ],
            reviews=ReviewsConfig(enabled=True, auto_dispatch=True),
            models=ModelsConfig(default="sonnet", escalation=["haiku", "sonnet", "opus"]),
            pipeline=PipelineConfig(auto_loop=True, escalate_fix_model=True),
        )
        # work.review_iteration=0 → next_iteration=1 → base model "sonnet"
        payload, fix = self._dispatch(cfg, tmp_path)
        assert payload["model"] == "sonnet"
        assert fix.model == "sonnet"

    def test_no_model_set_when_escalation_disabled(self, tmp_path) -> None:
        cfg = Config(
            repos=[Repo(name="api", github="acme/api", default_branch="main")],
            machines=[
                Machine(
                    name="laptop", host="laptop.tail",
                    repos=["api"], repo_paths={"api": "/work/api"},
                )
            ],
            reviews=ReviewsConfig(enabled=True, auto_dispatch=True),
            pipeline=PipelineConfig(auto_loop=True, escalate_fix_model=False),
        )
        payload, fix = self._dispatch(cfg, tmp_path)
        assert "model" not in payload  # legacy behaviour: no model key
        assert fix.model is None


# ── Unit tests: config parsing ───────────────────────────────────────────────


class TestPipelineConfigParsing:
    def test_auto_loop_defaults_to_true(self, tmp_path) -> None:
        from coord.config import load

        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: acme/api\n"
            "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
        )
        cfg = load(p)
        assert cfg.pipeline.auto_loop is True

    def test_max_review_iterations_defaults_to_5(self, tmp_path) -> None:
        from coord.config import load

        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: acme/api\n"
            "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
        )
        cfg = load(p)
        assert cfg.pipeline.max_review_iterations == 5

    def test_can_disable_auto_loop(self, tmp_path) -> None:
        from coord.config import load

        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: acme/api\n"
            "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
            "pipeline:\n  auto_loop: false\n  max_review_iterations: 5\n"
        )
        cfg = load(p)
        assert cfg.pipeline.auto_loop is False
        assert cfg.pipeline.max_review_iterations == 5

    def test_invalid_auto_loop_raises(self, tmp_path) -> None:
        from coord.config import ConfigError, load

        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: acme/api\n"
            "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
            "pipeline:\n  auto_loop: yes_please\n"
        )
        with pytest.raises(ConfigError, match="pipeline.auto_loop must be a boolean"):
            load(p)

    def test_invalid_max_review_iterations_raises(self, tmp_path) -> None:
        from coord.config import ConfigError, load

        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: acme/api\n"
            "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
            "pipeline:\n  max_review_iterations: 0\n"
        )
        with pytest.raises(
            ConfigError, match="pipeline.max_review_iterations must be a positive integer"
        ):
            load(p)

    def test_escalate_fix_model_defaults_to_true(self, tmp_path) -> None:
        from coord.config import load

        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: acme/api\n"
            "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
        )
        cfg = load(p)
        assert cfg.pipeline.escalate_fix_model is True

    def test_can_disable_escalate_fix_model(self, tmp_path) -> None:
        from coord.config import load

        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: acme/api\n"
            "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
            "pipeline:\n  escalate_fix_model: false\n"
        )
        cfg = load(p)
        assert cfg.pipeline.escalate_fix_model is False

    def test_invalid_escalate_fix_model_raises(self, tmp_path) -> None:
        from coord.config import ConfigError, load

        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: acme/api\n"
            "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
            "pipeline:\n  escalate_fix_model: maybe\n"
        )
        with pytest.raises(
            ConfigError, match="pipeline.escalate_fix_model must be a boolean"
        ):
            load(p)

    def test_auto_dispatch_stalled_defaults_to_false(self, tmp_path) -> None:
        """#1478: the stalled-pipeline sweeper's dispatch arm ships dark —
        detection/narration (#1441) is unconditional; only the action is
        gated, and it's opt-in."""
        from coord.config import load

        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: acme/api\n"
            "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
        )
        cfg = load(p)
        assert cfg.pipeline.auto_dispatch_stalled is False

    def test_can_enable_auto_dispatch_stalled(self, tmp_path) -> None:
        from coord.config import load

        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: acme/api\n"
            "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
            "pipeline:\n  auto_dispatch_stalled: true\n"
        )
        cfg = load(p)
        assert cfg.pipeline.auto_dispatch_stalled is True

    def test_invalid_auto_dispatch_stalled_raises(self, tmp_path) -> None:
        from coord.config import ConfigError, load

        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: acme/api\n"
            "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
            "pipeline:\n  auto_dispatch_stalled: maybe\n"
        )
        with pytest.raises(
            ConfigError, match="pipeline.auto_dispatch_stalled must be a boolean"
        ):
            load(p)

    def test_auto_heal_phantom_rows_defaults_to_true(self, tmp_path) -> None:
        """#2536: unlike auto_dispatch_stalled, the phantom-row self-heal
        sweep ships lit — every action it takes is gated behind a
        confirmed-dead liveness read plus an aged-out wall-clock buffer, and
        it never dispatches work or touches a branch."""
        from coord.config import load

        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: acme/api\n"
            "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
        )
        cfg = load(p)
        assert cfg.pipeline.auto_heal_phantom_rows is True

    def test_can_disable_auto_heal_phantom_rows(self, tmp_path) -> None:
        from coord.config import load

        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: acme/api\n"
            "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
            "pipeline:\n  auto_heal_phantom_rows: false\n"
        )
        cfg = load(p)
        assert cfg.pipeline.auto_heal_phantom_rows is False

    def test_invalid_auto_heal_phantom_rows_raises(self, tmp_path) -> None:
        from coord.config import ConfigError, load

        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: acme/api\n"
            "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
            "pipeline:\n  auto_heal_phantom_rows: maybe\n"
        )
        with pytest.raises(
            ConfigError, match="pipeline.auto_heal_phantom_rows must be a boolean"
        ):
            load(p)


# ── Unit tests: Assignment.review_iteration persistence ─────────────────────


class TestReviewIterationPersistence:
    def test_review_iteration_saved_and_loaded(self, coord_db) -> None:
        from coord.state import load_board, save_board

        work = _work_assignment(review_iteration=2)
        work.status = "done"
        board = Board(completed=[work])
        save_board(board)

        loaded = load_board()
        assert loaded is not None
        found = loaded.find_by_id("work-abc")
        assert found is not None
        assert found.review_iteration == 2

    def test_review_iteration_defaults_to_zero_when_absent(self, coord_db) -> None:
        from coord.state import load_board, save_board

        work = _work_assignment()  # review_iteration=0 by default
        work.status = "done"
        board = Board(completed=[work])
        save_board(board)

        loaded = load_board()
        assert loaded is not None
        found = loaded.find_by_id("work-abc")
        assert found is not None
        assert found.review_iteration == 0


# ── Integration test: full work → review → fix → review → approve cycle ─────


class TestFullCycle:
    """End-to-end simulation of the auto-loop using mocked HTTP and log files."""

    def test_work_review_fix_review_approve(self, config: Config, tmp_path, coord_db) -> None:
        """Simulate: work done → review requests changes → fix dispatched →
        second review approves → pipeline advances."""
        from coord.state import load_board, save_board

        # -- Round 1: work assignment completes --
        work = _work_assignment(assignment_id="w-1", review_iteration=0)
        board = Board(completed=[work])
        save_board(board)

        # -- Round 2: review requests changes --
        review_log = tmp_path / "review1.log"
        review_log.write_text(
            "REVIEW_VERDICT: request-changes\n"
            "REVIEW_BODY:\n"
            "Missing input validation on the endpoint.\n"
            "END_REVIEW\n"
        )
        review1 = _review_assignment(assignment_id="r-1", review_of="w-1")
        review1.status = "done"
        board2 = load_board()
        assert board2 is not None
        board2.completed.append(review1)
        save_board(board2)

        mock_http = MagicMock()
        mock_http.post.return_value.json.return_value = {"id": "fix-1"}
        mock_http.post.return_value.raise_for_status = MagicMock()

        with patch("coord.auto_loop.record_dispatched_assignment"):
            actions = process_review_completion(
                review1, board2, config,
                log_path=str(review_log),
                http_client=mock_http,
            )

        assert actions[0].kind == "fix_dispatched"
        fix1 = board2.active[0]
        assert fix1.assignment_id == "fix-1"
        assert fix1.review_iteration == 1
        assert "Missing input validation" in fix1.briefing
        save_board(board2)

        # -- Round 3: fix completes, second review approves --
        review2_log = tmp_path / "review2.log"
        review2_log.write_text(
            "REVIEW_VERDICT: approve\n"
            "REVIEW_BODY:\n"
            "Validation added. Tests pass. LGTM.\n"
            "END_REVIEW\n"
        )
        # Transition fix-1 to done
        board3 = load_board()
        assert board3 is not None
        fix1_on_board = board3.find_by_id("fix-1")
        assert fix1_on_board is not None
        assert fix1_on_board.review_iteration == 1
        fix1_on_board.status = "done"
        board3.completed.append(
            board3.active.pop(board3.active.index(fix1_on_board))
        )

        review2 = _review_assignment(assignment_id="r-2", review_of="fix-1")
        review2.status = "done"
        board3.completed.append(review2)
        save_board(board3)

        actions2 = process_review_completion(
            review2, board3, config,
            log_path=str(review2_log),
        )

        assert actions2[0].kind == "approved"
        # fix-1's review_state was updated to "done"
        fix1_final = board3.find_by_id("fix-1")
        assert fix1_final is not None
        assert fix1_final.review_state == "done"

    def test_max_iterations_stops_at_configured_limit(
        self, config: Config, tmp_path
    ) -> None:
        """After max_review_iterations fix rounds, the loop stops and posts notice."""
        review_log = tmp_path / "review.log"
        review_log.write_text(
            "REVIEW_VERDICT: request-changes\nREVIEW_BODY:\nStill broken.\nEND_REVIEW\n"
        )

        # work already at max (review_iteration == max_review_iterations == 3)
        work = _work_assignment(review_iteration=3)
        review = _review_assignment(review_of="work-abc")
        board = _board_with(work, review)

        with patch("coord.auto_loop._post_max_iterations_notice") as mock_notice:
            actions = process_review_completion(
                review, board, config, log_path=str(review_log)
            )

        assert actions[0].kind == "max_iterations"
        mock_notice.assert_called_once_with(work, config)
        assert len(board.active) == 0  # no fix dispatched


# ── Unit tests: run_for_review_transition (notify integration) ───────────────


class TestRunForReviewTransition:
    def test_returns_disabled_when_auto_loop_off(
        self, config_loop_disabled: Config, coord_db
    ) -> None:
        from coord.state import save_board

        work = _work_assignment()
        review = _review_assignment()
        save_board(Board(completed=[work, review]))

        record = {"type": "review", "review_of_assignment_id": "work-abc"}
        entry = {"log_path": None}

        actions = run_for_review_transition(
            "review-xyz", record, entry, config_loop_disabled
        )
        assert actions[0].kind == "disabled"

    def test_returns_empty_for_non_review_type(self, config: Config, coord_db) -> None:
        record = {"type": "work"}
        entry: dict = {}
        actions = run_for_review_transition("some-id", record, entry, config)
        assert actions == []

    def test_returns_empty_when_no_board(self, config: Config) -> None:
        """If there is no saved board, run_for_review_transition returns []."""
        record = {"type": "review"}
        entry: dict = {}
        # coord_db fixture not used → no board exists; read_board() (#749)
        # falls back to an empty Board rather than None.
        with patch("coord.auto_loop.read_board", return_value=Board()):
            actions = run_for_review_transition("r-1", record, entry, config)
        assert actions == []

    def test_saves_board_when_fix_dispatched(
        self, config: Config, tmp_path, coord_db
    ) -> None:
        from coord.state import load_board, save_board

        review_log = tmp_path / "review.log"
        review_log.write_text(
            "REVIEW_VERDICT: request-changes\nREVIEW_BODY:\nFix it.\nEND_REVIEW\n"
        )
        work = _work_assignment()
        review = _review_assignment()
        board = Board(completed=[work, review])
        save_board(board)

        record = {"type": "review", "review_of_assignment_id": "work-abc"}
        entry = {"log_path": str(review_log)}

        mock_http = MagicMock()
        mock_http.post.return_value.json.return_value = {"id": "fix-new"}
        mock_http.post.return_value.raise_for_status = MagicMock()

        with patch("coord.auto_loop.record_dispatched_assignment"), \
             patch("coord.auto_loop.httpx", mock_http):
            actions = run_for_review_transition(
                "review-xyz", record, entry, config
            )

        assert any(a.kind == "fix_dispatched" for a in actions)
        # Board was saved: newly dispatched fix should appear in loaded board
        loaded = load_board()
        assert loaded is not None
        # The fix was added to board.active before save
        assert any(a.assignment_id == "fix-new" for a in loaded.active)

    def test_advisory_advance_persists_to_db(
        self, config: Config, tmp_path, coord_db
    ) -> None:
        """#476 follow-up: a request-changes review with no blocking findings
        advances the pipeline (approved_with_nits) — and that advance MUST be
        persisted (review_state="done" + review_verdict="approve") so the merge
        gate unblocks.  Regression guard for the save-condition gap that left
        an advisory-advanced PR silently un-mergeable."""
        from coord.state import load_board, save_board

        review_log = tmp_path / "review.log"
        # Body has an EXPLICITLY EMPTY blocking section plus non-blocking nits
        # — the only shape that may downgrade a verdict since #1456 (a missing
        # blocking section is "unknown", and fails closed).
        review_log.write_text(
            "REVIEW_VERDICT: request-changes\n"
            "REVIEW_BODY:\n"
            "## Blocking findings\n"
            "None.\n"
            "## Minor observations (not blocking)\n"
            "- nit one\n- nit two\n"
            "END_REVIEW\n"
        )
        work = _work_assignment()
        review = _review_assignment()
        board = Board(completed=[work, review])
        save_board(board)

        record = {"type": "review", "review_of_assignment_id": "work-abc"}
        entry = {"log_path": str(review_log)}

        with patch("coord.auto_loop._post_advisory_nits_notice"):
            actions = run_for_review_transition("review-xyz", record, entry, config)

        assert any(a.kind == "approved_with_nits" for a in actions)
        # The advance must have been SAVED — reload from DB and check.
        loaded = load_board()
        assert loaded is not None
        work_loaded = loaded.find_by_id("work-abc")
        assert work_loaded is not None
        assert work_loaded.review_state == "done", (
            "advisory-advance must persist review_state=done so the merge gate unblocks"
        )
        review_loaded = loaded.find_by_id("review-xyz")
        assert review_loaded is not None
        assert review_loaded.review_verdict == "approve"
        # #1456: the reviewer's own verdict must survive the override, and it
        # must survive the round-trip through the DB — otherwise the override
        # is unauditable exactly as it was on #1445.
        assert review_loaded.review_verdict_original == "request-changes"
        assert "blocking=0" in (review_loaded.review_verdict_override_reason or "")
        # #1456: the third leg of the audit trail — a durable business-tier
        # event, so "which merges rode an overridden verdict?" is answerable
        # after the issue is closed and the board row has aged out of view.
        from coord.audit import query_audit_log

        entries = query_audit_log(event_type="review_verdict_overridden")["entries"]
        assert len(entries) == 1, entries
        assert entries[0]["details"]["original_verdict"] == "request-changes"
        assert entries[0]["details"]["effective_verdict"] == "approve"
        assert entries[0]["details"]["blocking"] == 0

    def test_review_not_on_board_returns_empty(
        self, config: Config, tmp_path, coord_db
    ) -> None:
        from coord.state import save_board

        # Board has work but not the review
        work = _work_assignment()
        save_board(Board(completed=[work]))

        record = {"type": "review", "review_of_assignment_id": "work-abc"}
        entry = {"log_path": None}

        actions = run_for_review_transition(
            "review-xyz",  # not on board
            record, entry, config,
        )
        # Review not found on board → no actions (empty list or no_findings)
        # Should not raise; either returns [] or a no_findings/no_work_found action
        assert isinstance(actions, list)


# ── coord bounce CLI + HTTP fallback ────────────────────────────────────────


class TestProcessReviewCompletionAgentFallback:
    """When the local log isn't reachable, process_review_completion
    falls back to fetching the structured findings via the agent's
    `/logs/<id>` HTTP endpoint.  Closes the gap that left quadraui#166
    without an auto-fix dispatch."""

    def test_falls_back_to_agent_when_local_log_missing(
        self, config: Config, monkeypatch
    ) -> None:
        review = _review_assignment()
        work = _work_assignment()
        board = _board_with(work, review)

        # Local log doesn't exist; agent HTTP returns findings.
        from coord.review import ReviewFindings
        called = {}

        def fake_agent(host, aid, *args, **kwargs):
            called["host"] = host
            called["aid"] = aid
            return ReviewFindings(
                verdict="request-changes",
                body="Issue in src/main.py — handle None case.",
            )

        monkeypatch.setattr(
            "coord.auto_loop.parse_review_from_agent", fake_agent,
        )

        def fake_dispatch(*args, **kwargs):
            # Stub the dispatch so the test doesn't need an agent server.
            from coord.models import Assignment as A
            return A(
                machine_name="laptop", repo_name="api",
                issue_number=42, issue_title="t", briefing="",
                assignment_id="fix-1", status="running",
                type="work", review_iteration=1,
                review_of_assignment_id=work.assignment_id,
            )

        monkeypatch.setattr("coord.auto_loop._dispatch_fix", fake_dispatch)

        actions = process_review_completion(
            review,
            board,
            config,
            log_path=None,  # no local log
            machine_host="elitebook.tailnet",
        )

        assert called.get("host") == "elitebook.tailnet"
        assert called.get("aid") == review.assignment_id
        # Should have dispatched a fix worker via the HTTP-fetched findings.
        assert any(a.kind == "fix_dispatched" for a in actions), actions

    def test_no_fallback_when_no_host_supplied(
        self, config: Config, monkeypatch
    ) -> None:
        """Without a machine_host the function can't fall back — must
        still degrade to no_findings rather than crash."""
        review = _review_assignment()
        work = _work_assignment()
        board = _board_with(work, review)

        # The agent fallback must NOT be invoked when host is None.
        def boom(*args, **kwargs):
            raise AssertionError("parse_review_from_agent should not be called")

        monkeypatch.setattr("coord.auto_loop.parse_review_from_agent", boom)

        actions = process_review_completion(
            review, board, config, log_path=None, machine_host=None,
        )
        assert actions[0].kind == "no_findings"


class TestCoordBounceCommand:
    """The `coord bounce <review-id>` CLI command — manual trigger
    for the auto-loop's fix-dispatch path, used by the TUI's F key /
    'Address review findings' action."""

    def test_bounce_dispatches_when_verdict_is_request_changes(
        self, config_path, monkeypatch
    ) -> None:
        """Happy path: review with request-changes → fix worker
        dispatched, exit 0, board saved."""
        from click.testing import CliRunner
        from coord.cli import main as cli_main
        from coord.models import Assignment, Board
        from coord.state import save_board

        # Seed the board with paired work + review (request-changes).
        work = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=42, issue_title="t", briefing="b",
            assignment_id="work-1", status="done",
            type="work", branch="issue-42-t",
        )
        review = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=42, issue_title="t", briefing="",
            assignment_id="review-1", status="done",
            type="review", review_of_assignment_id="work-1",
            review_verdict="request-changes",
        )
        save_board(Board(completed=[work, review]))

        # Stub the dispatch so the test doesn't need a live agent.
        def fake_dispatch(*args, **kwargs):
            return Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=42, issue_title="t", briefing="",
                assignment_id="fix-1", status="running",
                type="work", review_iteration=1,
                review_of_assignment_id="work-1",
            )

        monkeypatch.setattr("coord.auto_loop._dispatch_fix", fake_dispatch)

        # Stub findings — bypass the log/HTTP path entirely.
        from coord.review import ReviewFindings
        monkeypatch.setattr(
            "coord.auto_loop.parse_review_from_agent",
            lambda *a, **kw: ReviewFindings(verdict="request-changes", body="fix x"),
        )

        result = CliRunner().invoke(cli_main, [
            "bounce", "review-1", "--config", str(config_path),
        ])
        assert result.exit_code == 0, result.output
        assert "fix_dispatched" in result.output

    def test_bounce_refuses_when_verdict_is_approve(
        self, config_path
    ) -> None:
        from click.testing import CliRunner
        from coord.cli import main as cli_main
        from coord.models import Assignment, Board
        from coord.state import save_board

        review = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=42, issue_title="t", briefing="",
            assignment_id="review-2", status="done",
            type="review", review_of_assignment_id="work-1",
            review_verdict="approve",
        )
        save_board(Board(completed=[review]))

        result = CliRunner().invoke(cli_main, [
            "bounce", "review-2", "--config", str(config_path),
        ])
        # Refuses with a clear message; doesn't dispatch anything.
        assert result.exit_code != 0
        assert "request-changes" in result.output

    def test_bounce_refuses_when_assignment_not_review(
        self, config_path
    ) -> None:
        from click.testing import CliRunner
        from coord.cli import main as cli_main
        from coord.models import Assignment, Board
        from coord.state import save_board

        work = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=42, issue_title="t", briefing="b",
            assignment_id="work-3", status="done", type="work",
        )
        save_board(Board(completed=[work]))

        result = CliRunner().invoke(cli_main, [
            "bounce", "work-3", "--config", str(config_path),
        ])
        assert result.exit_code != 0
        assert "not 'review'" in result.output or "work" in result.output.lower()

    def test_bounce_unknown_assignment_id(self, config_path) -> None:
        from click.testing import CliRunner
        from coord.cli import main as cli_main
        from coord.models import Board
        from coord.state import save_board

        save_board(Board())

        result = CliRunner().invoke(cli_main, [
            "bounce", "nope", "--config", str(config_path),
        ])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_bounce_exits_cleanly_for_terminal_work(
        self, config_path, monkeypatch
    ) -> None:
        """#522: `coord bounce` on a request-changes review whose work is
        already merged/closed must skip the fix, exit **0** (not 1), and
        persist review_state="done". Regression for the gap the adversarial
        review of PR #523 caught."""
        from click.testing import CliRunner
        from coord.cli import main as cli_main
        from coord.models import Assignment, Board
        from coord.review import ReviewFindings
        from coord.state import load_board, save_board

        work = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=42, issue_title="t", briefing="b",
            assignment_id="work-term", status="done",
            type="work", branch="issue-42-t",
        )
        review = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=42, issue_title="t", briefing="",
            assignment_id="review-term", status="done",
            type="review", review_of_assignment_id="work-term",
            review_verdict="request-changes",
        )
        save_board(Board(completed=[work, review]))

        # Findings say request-changes, but the work is already terminal.
        monkeypatch.setattr(
            "coord.auto_loop.parse_review_from_agent",
            lambda *a, **kw: ReviewFindings(verdict="request-changes", body="x"),
        )
        monkeypatch.setattr("coord.github_ops.work_is_terminal", lambda *a, **k: True)
        # A fix must NOT be dispatched for terminal work.
        def _no_fix(*a, **k):
            raise AssertionError("must not dispatch a fix for terminal work")
        monkeypatch.setattr("coord.auto_loop._dispatch_fix", _no_fix)

        result = CliRunner().invoke(cli_main, [
            "bounce", "review-term", "--config", str(config_path),
        ])
        assert result.exit_code == 0, result.output
        assert "terminal_skip" in result.output
        reloaded = load_board().find_by_id("work-term")
        assert reloaded is not None and reloaded.review_state == "done"


class TestReviewFindingsDbCache:
    """The DB cache layer for review findings.  notify populates it on
    first parse; coord bounce reads it back near-instantly so we don't
    have to refetch the multi-MB worker log over Tailscale every time."""

    def test_save_and_load_roundtrip(self, coord_db) -> None:
        from coord.state import (
            update_assignment_review_findings,
            load_assignment_review_findings,
        )
        from coord.models import Assignment, Board
        from coord.state import save_board

        review = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1,
            issue_title="t", briefing="",
            assignment_id="r1", status="done", type="review",
        )
        save_board(Board(completed=[review]))

        update_assignment_review_findings(
            "r1", verdict="request-changes",
            body="### Required changes\n- Handle None case",
        )
        result = load_assignment_review_findings("r1")
        assert result is not None
        verdict, body = result
        assert verdict == "request-changes"
        assert "Handle None case" in body

    def test_load_returns_none_when_unset(self, coord_db) -> None:
        from coord.state import (
            save_board, load_assignment_review_findings,
        )
        from coord.models import Assignment, Board

        review = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1,
            issue_title="t", briefing="",
            assignment_id="r2", status="done", type="review",
        )
        save_board(Board(completed=[review]))
        # Never wrote findings — should be None.
        assert load_assignment_review_findings("r2") is None

    def test_load_returns_none_for_unknown_id(self, coord_db) -> None:
        from coord.state import load_assignment_review_findings
        assert load_assignment_review_findings("ghost") is None

    def test_load_findings_via_cache_skips_log_and_http(
        self, config: Config, coord_db, monkeypatch
    ) -> None:
        """When DB has the cached findings, neither the local log nor
        the agent HTTP fallback are touched."""
        from coord.auto_loop import _load_review_findings
        from coord.state import (
            update_assignment_review_findings, save_board,
        )
        from coord.models import Assignment, Board

        review = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1,
            issue_title="t", briefing="",
            assignment_id="r3", status="done", type="review",
        )
        save_board(Board(completed=[review]))
        update_assignment_review_findings(
            "r3", verdict="approve", body="Looks good."
        )

        # If the function reached the HTTP fallback it would call this:
        def boom_http(*args, **kwargs):
            raise AssertionError(
                "DB cache should have served the request — HTTP fetch must not run"
            )

        def boom_log(*args, **kwargs):
            raise AssertionError(
                "DB cache should have served the request — log parse must not run"
            )

        monkeypatch.setattr("coord.auto_loop.parse_review_from_agent", boom_http)
        monkeypatch.setattr("coord.auto_loop.parse_review_from_log", boom_log)

        findings = _load_review_findings(review, log_path="/no/such", machine_host="x")
        assert findings is not None
        assert findings.verdict == "approve"
        assert findings.body == "Looks good."

    def test_falls_back_to_http_when_cache_empty(
        self, config: Config, coord_db, monkeypatch
    ) -> None:
        """When the DB row exists but review_findings is NULL (e.g. a
        review that completed before this cache landed), the loader
        falls back to local log → HTTP as before."""
        from coord.auto_loop import _load_review_findings
        from coord.state import save_board
        from coord.models import Assignment, Board
        from coord.review import ReviewFindings

        review = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1,
            issue_title="t", briefing="",
            assignment_id="r4", status="done", type="review",
        )
        save_board(Board(completed=[review]))
        # No update_assignment_review_findings call — cache stays NULL.

        monkeypatch.setattr(
            "coord.auto_loop.parse_review_from_agent",
            lambda h, aid, *a, **kw: ReviewFindings(
                verdict="request-changes", body="from http"
            ),
        )
        findings = _load_review_findings(
            review, log_path=None, machine_host="elitebook.tail"
        )
        assert findings is not None
        assert findings.body == "from http"


# ── Unit tests: run_for_fix_transition ──────────────────────────────────────


def _fix_assignment(
    assignment_id: str = "fix-1",
    review_iteration: int = 1,
    review_of: str = "work-abc",
    test_state: str | None = "passed",
) -> Assignment:
    """Build a bounce-fix work assignment (the type dispatched by process_review_completion).

    ``test_state`` defaults to ``"passed"`` so callers that aren't exercising
    the #1612 test gate get the pre-#1612 dispatch-proceeds behaviour without
    having to think about it; tests targeting the gate itself pass an
    explicit ``test_state`` (``None``/``"running"``/``"failed"``).

    ``review_state`` is reset to ``None`` here (overriding
    ``_work_assignment``'s ``"dispatched"`` default, which models the
    *original* work row after its first review went out -- not a freshly
    completed fix worker, which carries ``review_state=None`` until
    ``run_for_fix_transition``/``dispatch_pending_reviews`` first touches
    it). This matters for #1612: the DB's whole-board upsert has a CAS
    guard (state.py's ``_UPSERT_SQL``, #1565) that refuses to revert an
    already-non-pending, non-NULL ``review_state`` back to ``"pending"`` --
    exactly what the #1612 gate-hold path writes. Seeding the fixture with
    the unrealistic ``"dispatched"`` default would trip that guard and mask
    a real write with a silently-discarded one.
    """
    a = _work_assignment(assignment_id=assignment_id, review_iteration=review_iteration)
    a.review_of_assignment_id = review_of
    a.issue_title = f"[fix-{review_iteration}] Fix the thing"
    a.test_state = test_state
    a.review_state = None
    return a


def _stub_review_assignment(assignment_id: str = "re-review-1") -> Assignment:
    """Build a minimal review assignment to stand in for dispatch_review's return value."""
    return Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=1,
        issue_title="[review] Fix the thing",
        assignment_id=assignment_id,
        status="running",
        type="review",
        review_of_assignment_id="fix-1",
    )


class TestMultiReviewFixBriefing:
    """#3113: two reviews can complete for the same work assignment — a
    dispatch race (the vimcode#804 incident: two reviews 3 seconds apart,
    both to the same machine, $4.41 combined) or a legacy duplicate. The fix
    briefing must contain BOTH reviews' blocking findings, not just whichever
    review's completion happened to trigger the fix dispatch."""

    def test_fix_briefing_contains_both_reviews_findings_untruncated(
        self, config: Config, tmp_path, coord_db,
    ) -> None:
        """Black-box: drive `process_review_completion` (the real entry
        point `coord.notify` calls) for ONE of two completed reviews on the
        same work row, and assert the fix worker's actual dispatched
        briefing carries both reviews' full, untruncated findings."""
        from coord.state import save_board, update_assignment_review_findings

        work = _work_assignment(assignment_id="work-abc", review_iteration=0)
        review1 = _review_assignment(assignment_id="review-1", review_of="work-abc")
        review2 = _review_assignment(assignment_id="review-2", review_of="work-abc")
        board = Board(
            repos=[Repo(name="api", github="acme/api")],
            machines=[], active=[], completed=[work, review1, review2],
        )
        save_board(board)

        # review1's findings arrive via a local log (the path that triggers
        # this call) — the real perf-bug shape from the incident, sized like
        # the real ~1.9KB blocking section so this also proves nothing
        # upstream (briefing assembly) truncates it.
        first_finding = (
            "## Blocking findings\n\n- missing RED statement in the "
            "acceptance test scaffold. " * 30
        ).rstrip()
        assert len(first_finding) > 900
        log_file = tmp_path / "review1.log"
        log_file.write_text(
            f"REVIEW_VERDICT: request-changes\nREVIEW_BODY:\n{first_finding}\nEND_REVIEW\n"
        )

        # review2's findings are already cached on its own row (as they
        # would be after notify parsed ITS completion) — the loser of the
        # race in the real incident, whose findings used to be silently
        # discarded.
        second_finding = (
            "## Blocking findings\n\n- split_insert_undo_group() on every "
            "insert-mode arrow key makes finish_undo_group do an O(buffer) "
            "full-text clone per keystroke. " * 30
        ).rstrip()
        assert len(second_finding) > 900
        assert len(first_finding) + len(second_finding) > 2500  # > ISSUE_CONTEXT_MAX_CHARS
        update_assignment_review_findings(
            "review-2", verdict="request-changes", body=second_finding,
        )

        mock_http = MagicMock()
        mock_http.post.return_value.json.return_value = {"id": "fix-mrg-1"}
        mock_http.post.return_value.raise_for_status = MagicMock()

        with patch("coord.auto_loop.record_dispatched_assignment"):
            actions = process_review_completion(
                review1, board, config,
                log_path=str(log_file),
                http_client=mock_http,
            )

        assert len(actions) == 1
        assert actions[0].kind == "fix_dispatched"

        sent_payload = mock_http.post.call_args.kwargs["json"]
        briefing = sent_payload["briefing"]
        assert first_finding in briefing, "review1's blocking finding is missing/truncated"
        assert second_finding in briefing, (
            "review2's blocking finding — the one #3113 fixes — is missing/truncated"
        )
        assert "## Reviewer findings to address" in briefing

    def test_single_review_briefing_is_byte_identical_to_before(
        self, config: Config, tmp_path, coord_db,
    ) -> None:
        """Regression guard: the overwhelmingly common single-review case
        must render exactly as it did before #3113 — no extra wrapper text
        around the one review's body."""
        from coord.state import save_board

        work = _work_assignment(assignment_id="work-solo", review_iteration=0)
        review = _review_assignment(assignment_id="review-solo", review_of="work-solo")
        board = _board_with(work, review)
        save_board(board)

        log_file = tmp_path / "review.log"
        log_file.write_text(
            "REVIEW_VERDICT: request-changes\n"
            "REVIEW_BODY:\n"
            "Missing tests for edge case X.\n"
            "END_REVIEW\n"
        )
        mock_http = MagicMock()
        mock_http.post.return_value.json.return_value = {"id": "fix-solo-1"}
        mock_http.post.return_value.raise_for_status = MagicMock()

        with patch("coord.auto_loop.record_dispatched_assignment"):
            actions = process_review_completion(
                review, board, config,
                log_path=str(log_file),
                http_client=mock_http,
            )

        assert actions[0].kind == "fix_dispatched"
        briefing = mock_http.post.call_args.kwargs["json"]["briefing"]
        assert "Missing tests for edge case X." in briefing
        assert "Additional blocking findings" not in briefing


class TestRunForFixTransition:
    """run_for_fix_transition: auto-dispatch a fresh review when a fix worker completes."""

    def test_run_for_fix_transition_dispatches_review(
        self, config: Config, coord_db
    ) -> None:
        """Happy path: fix worker completes → fresh review dispatched, board saved."""
        from coord.state import load_board, save_board

        fix = _fix_assignment()
        board = Board(completed=[fix])
        save_board(board)

        stub_review = _stub_review_assignment()

        with patch("coord.auto_loop.dispatch_review", return_value=stub_review):
            actions = run_for_fix_transition("fix-1", config)

        assert len(actions) == 1
        assert actions[0].kind == "review_dispatched"
        assert actions[0].assignment_id == "fix-1"

        # Board was saved with the fix's review_state updated.
        loaded = load_board()
        assert loaded is not None
        found = loaded.find_by_id("fix-1")
        assert found is not None
        assert found.review_state == "dispatched"

    def test_run_for_fix_transition_iteration_cap_hit(
        self, config: Config, coord_db
    ) -> None:
        """fix.review_iteration == max_review_iterations → no review dispatched."""
        from coord.state import save_board

        # review_iteration == max_review_iterations (3) → cap hit.
        fix = _fix_assignment(assignment_id="fix-3", review_iteration=3)
        board = Board(completed=[fix])
        save_board(board)

        with (
            patch("coord.auto_loop.dispatch_review") as mock_dispatch,
            patch("coord.auto_loop._post_max_iterations_notice") as mock_notice,
        ):
            actions = run_for_fix_transition("fix-3", config)

        assert len(actions) == 1
        assert actions[0].kind == "iteration_cap_hit"
        # dispatch_review must NOT be called when the cap is hit.
        mock_dispatch.assert_not_called()
        # GitHub notice must be posted when the cap is hit.
        mock_notice.assert_called_once()

    def test_run_for_fix_transition_cap_hit_posts_comment_and_marks_board(
        self, config: Config, coord_db
    ) -> None:
        """Cap-hit path posts a GitHub comment and persists review_state='cap_hit'."""
        from coord.state import load_board, save_board

        fix = _fix_assignment(assignment_id="fix-cap", review_iteration=3)
        board = Board(completed=[fix])
        save_board(board)

        with (
            patch("coord.auto_loop.dispatch_review"),
            patch("coord.auto_loop._post_max_iterations_notice") as mock_notice,
        ):
            actions = run_for_fix_transition("fix-cap", config)

        # GitHub comment was posted exactly once, with the fix assignment and config.
        # We check individual args rather than the whole object because `fix` is
        # mutated in-place (review_state set to "cap_hit") after the mock call.
        mock_notice.assert_called_once()
        called_with_fix, called_with_config = mock_notice.call_args[0]
        assert called_with_fix.assignment_id == "fix-cap"
        assert called_with_config is config

        # Board was saved with the fix marked as cap_hit.
        loaded = load_board()
        assert loaded is not None
        entry = loaded.find_by_id("fix-cap")
        assert entry is not None
        assert entry.review_state == "cap_hit"

        # Action kind confirms cap was hit.
        assert len(actions) == 1
        assert actions[0].kind == "iteration_cap_hit"

    def test_run_for_fix_transition_no_machine_available(
        self, config: Config, coord_db
    ) -> None:
        """dispatch_review returns None (no capable machine) → graceful no-op."""
        from coord.state import save_board

        fix = _fix_assignment()
        board = Board(completed=[fix])
        save_board(board)

        with patch("coord.auto_loop.dispatch_review", return_value=None):
            actions = run_for_fix_transition("fix-1", config)

        # No dispatch possible → empty list (caller can retry later).
        assert actions == []

    def test_run_for_fix_transition_disabled(
        self, config_loop_disabled: Config, coord_db
    ) -> None:
        """auto_loop=false → disabled action, no dispatch attempt."""
        from coord.state import save_board

        fix = _fix_assignment()
        board = Board(completed=[fix])
        save_board(board)

        with patch("coord.auto_loop.dispatch_review") as mock_dispatch:
            actions = run_for_fix_transition("fix-1", config_loop_disabled)

        assert len(actions) == 1
        assert actions[0].kind == "disabled"
        mock_dispatch.assert_not_called()

    def test_run_for_fix_transition_no_board(self, config: Config) -> None:
        """No saved board → returns empty list without raising."""
        # read_board() (#749) falls back to an empty Board rather than None.
        with patch("coord.auto_loop.read_board", return_value=Board()):
            actions = run_for_fix_transition("fix-1", config)
        assert actions == []

    def test_run_for_fix_transition_assignment_not_on_board(
        self, config: Config, coord_db
    ) -> None:
        """Fix assignment not found on board → returns empty list without raising."""
        from coord.state import save_board

        save_board(Board())  # empty board

        actions = run_for_fix_transition("nonexistent-id", config)
        assert actions == []

    def test_run_for_fix_transition_below_cap_dispatches(
        self, config: Config, coord_db
    ) -> None:
        """review_iteration < max_review_iterations → dispatch proceeds."""
        from coord.state import save_board

        # iteration=2, max=3 → still allowed.
        fix = _fix_assignment(assignment_id="fix-2", review_iteration=2)
        board = Board(completed=[fix])
        save_board(board)

        stub_review = _stub_review_assignment(assignment_id="re-review-2")

        with patch("coord.auto_loop.dispatch_review", return_value=stub_review):
            actions = run_for_fix_transition("fix-2", config)

        assert len(actions) == 1
        assert actions[0].kind == "review_dispatched"

    # ── #1612: fix-round reviews must not bypass the Test gate ─────────────

    @pytest.mark.parametrize("test_state", [None, "running", "failed"])
    def test_run_for_fix_transition_holds_for_untested_fix(
        self, config: Config, coord_db, test_state: str | None
    ) -> None:
        """#1612: default_gates=[test, review, merge] (test precedes review) and
        the fix carries no passed/skipped verdict → no review dispatched, and
        the row is handed to the ``dispatch_pending_reviews`` gated path
        instead of stranded (review_state set back to "pending"), not
        dispatched directly via the ungated path this function used to take.
        """
        from coord.state import load_board, save_board

        assert config.pipeline.test_precedes_review()  # sanity: default_gates order

        fix = _fix_assignment(assignment_id="fix-untested", test_state=test_state)
        board = Board(completed=[fix])
        save_board(board)

        with patch("coord.auto_loop.dispatch_review") as mock_dispatch:
            actions = run_for_fix_transition("fix-untested", config)

        mock_dispatch.assert_not_called()
        assert len(actions) == 1
        assert actions[0].kind == "test_gate_held"
        assert actions[0].assignment_id == "fix-untested"

        loaded = load_board()
        assert loaded is not None
        entry = loaded.find_by_id("fix-untested")
        assert entry is not None
        assert entry.review_state == "pending"

    def test_run_for_fix_transition_held_fix_then_picked_up_by_dispatch_pending_reviews(
        self, config: Config, coord_db
    ) -> None:
        """#1612 regression: the hold is a deferral, not a drop. Once the
        held row's test verdict lands (passed), ``dispatch_pending_reviews``
        — the always-gated bulk path — must dispatch the review it deferred.

        ``run_for_fix_transition`` reads/writes the board through its own
        ``read_board()``/``write_board()`` calls (#749 routing), which is a
        distinct ``Board``/``Assignment`` object graph from whatever the test
        constructed locally — so the persisted state has to be re-fetched via
        ``load_board()`` afterward rather than asserted on the original local
        ``fix`` object, which ``run_for_fix_transition`` never touches.
        """
        from coord.review import dispatch_pending_reviews
        from coord.state import load_board, save_board

        fix = _fix_assignment(assignment_id="fix-deferred", test_state="running")
        board = Board(completed=[fix])
        save_board(board)

        with patch("coord.auto_loop.dispatch_review") as mock_dispatch:
            actions = run_for_fix_transition("fix-deferred", config)
        assert actions[0].kind == "test_gate_held"
        mock_dispatch.assert_not_called()

        board = load_board()
        assert board is not None
        held = board.find_by_id("fix-deferred")
        assert held is not None
        assert held.review_state == "pending"

        # Test verdict lands.
        held.test_state = "passed"

        stub_review = _stub_review_assignment(assignment_id="re-review-deferred")
        with patch("coord.review.dispatch_review", return_value=stub_review):
            dispatched = dispatch_pending_reviews(board, config)

        assert len(dispatched) == 1
        assert dispatched[0].assignment_id == "re-review-deferred"
        assert held.review_state == "dispatched"

    def test_run_for_fix_transition_held_fix_stays_held_when_test_fails(
        self, config: Config, coord_db
    ) -> None:
        """#1612: a "failed" verdict is still not a passed/skipped verdict —
        no review dispatched by either the fix-transition path or the bulk
        ``dispatch_pending_reviews`` path once the failure lands.

        See the previous test's docstring for why the board is re-fetched
        via ``load_board()`` rather than asserted on the local ``fix``.
        """
        from coord.review import dispatch_pending_reviews
        from coord.state import load_board, save_board

        fix = _fix_assignment(assignment_id="fix-failed", test_state="running")
        board = Board(completed=[fix])
        save_board(board)

        with patch("coord.auto_loop.dispatch_review") as mock_dispatch:
            actions = run_for_fix_transition("fix-failed", config)
        assert actions[0].kind == "test_gate_held"
        mock_dispatch.assert_not_called()

        board = load_board()
        assert board is not None
        held = board.find_by_id("fix-failed")
        assert held is not None
        held.test_state = "failed"

        with patch("coord.review.dispatch_review") as mock_bulk_dispatch:
            dispatched = dispatch_pending_reviews(board, config)

        assert dispatched == []
        mock_bulk_dispatch.assert_not_called()
        assert held.review_state == "pending"

    def test_run_for_fix_transition_test_after_review_gate_dispatches_immediately(
        self, repo: Repo, machine: Machine, coord_db
    ) -> None:
        """#1612: when default_gates orders review before test, the fix
        round must dispatch immediately — the configurable ordering itself
        must not regress."""
        from coord.state import save_board

        config = Config(
            repos=[repo],
            machines=[machine],
            reviews=ReviewsConfig(enabled=True, auto_dispatch=True),
            pipeline=PipelineConfig(
                auto_loop=True,
                max_review_iterations=3,
                default_gates=["review", "test", "merge"],
            ),
        )
        assert not config.pipeline.test_precedes_review()

        fix = _fix_assignment(assignment_id="fix-untested-2", test_state=None)
        board = Board(completed=[fix])
        save_board(board)

        stub_review = _stub_review_assignment(assignment_id="re-review-3")
        with patch("coord.auto_loop.dispatch_review", return_value=stub_review):
            actions = run_for_fix_transition("fix-untested-2", config)

        assert len(actions) == 1
        assert actions[0].kind == "review_dispatched"


# ── #586: branch-not-on-remote guard in _dispatch_fix ───────────────────────


def _two_machine_config() -> Config:
    """Config with 'laptop' + 'server' both capable of 'api'."""
    return Config(
        repos=[Repo(name="api", github="acme/api", default_branch="main")],
        machines=[
            Machine(
                name="laptop", host="laptop.tail",
                capabilities=["python"], repos=["api"],
                repo_paths={"api": "/work/api"},
            ),
            Machine(
                name="server", host="server.tail",
                capabilities=["python"], repos=["api"],
                repo_paths={"api": "/srv/api"},
            ),
        ],
        reviews=ReviewsConfig(enabled=True, auto_dispatch=True),
        pipeline=PipelineConfig(auto_loop=True, max_review_iterations=3),
    )


class TestDispatchFixRemoteBranchGuard:
    """#586: _dispatch_fix must not route to a different machine when the
    branch isn't on the remote — that would crash the fix worker in 2 seconds
    with no commits and no exit code."""

    def _work(self, machine: str = "laptop") -> Assignment:
        return Assignment(
            machine_name=machine,
            repo_name="api",
            issue_number=5,
            issue_title="Fix thing",
            briefing="Original briefing.",
            assignment_id="work-586",
            status="done",
            branch="issue-5-fix-thing",
            dispatched_at=0.0,
            finished_at=1.0,
            type="work",
        )

    def test_same_machine_dispatch_skips_remote_check(self) -> None:
        """When the original worker machine is still available, _dispatch_fix
        routes to it directly — no remote check needed (branch is local)."""
        cfg = _two_machine_config()
        work = self._work(machine="laptop")
        board = Board(completed=[work])
        mock_http = MagicMock()
        mock_http.post.return_value.json.return_value = {"id": "fix-586-a"}
        mock_http.post.return_value.raise_for_status = MagicMock()
        # Checker that always returns False — should NOT be called when same machine.
        checker_called: list[bool] = []

        def _checker(repo: str, branch: str) -> bool:
            checker_called.append(True)
            return False

        with patch("coord.auto_loop.record_dispatched_assignment"):
            result = _dispatch_fix(
                work, "Fix briefing.", board, cfg, iteration=1,
                http_client=mock_http,
                remote_branch_checker=_checker,
            )

        assert result is not None
        assert result.machine_name == "laptop"
        # The remote check was NOT invoked — routing to same machine is safe.
        assert checker_called == []

    def test_cross_machine_dispatch_blocked_when_branch_not_on_remote(self) -> None:
        """When the original machine is paused/unavailable and the fallback
        machine is different, and the branch isn't on the remote,
        _dispatch_fix must return None rather than sending the assignment."""
        cfg = _two_machine_config()
        # Work done on 'laptop'; we'll pause laptop so the fallback picks 'server'.
        work = self._work(machine="laptop")
        board = Board(completed=[work])
        mock_http = MagicMock()

        with (
            patch("coord.machine_pause.paused_set", return_value={"laptop"}),
            patch("coord.auto_loop.record_dispatched_assignment"),
        ):
            result = _dispatch_fix(
                work, "Fix briefing.", board, cfg, iteration=1,
                http_client=mock_http,
                remote_branch_checker=lambda repo, branch: False,
            )

        assert result is None
        # The HTTP post must NOT have been called.
        mock_http.post.assert_not_called()

    def test_cross_machine_dispatch_proceeds_when_branch_on_remote(self) -> None:
        """When the fallback machine is different but the branch IS on the
        remote, the dispatch proceeds normally."""
        cfg = _two_machine_config()
        work = self._work(machine="laptop")
        board = Board(completed=[work])
        mock_http = MagicMock()
        mock_http.post.return_value.json.return_value = {"id": "fix-586-c"}
        mock_http.post.return_value.raise_for_status = MagicMock()

        with (
            patch("coord.machine_pause.paused_set", return_value={"laptop"}),
            patch("coord.auto_loop.record_dispatched_assignment"),
        ):
            result = _dispatch_fix(
                work, "Fix briefing.", board, cfg, iteration=1,
                http_client=mock_http,
                remote_branch_checker=lambda repo, branch: True,
            )

        assert result is not None
        assert result.machine_name == "server"


# ── #2538: persistent "database is locked" must decline, not crash ─────────


class TestDispatchFixDbContention:
    """coord-portal#2538: a concurrent writer (the daemon's own passive
    tick, another `coord merge`/`coord notify` invocation) can hold the DB
    at the exact moment `record_dispatched_assignment` tries to record a
    dispatched fix. `coord.state`'s own bounded retry
    (`coord.db.retry_on_locked`) already rides out a short collision; this
    covers what happens once that retry budget is well and truly exhausted
    — `_dispatch_fix` must degrade to its documented "None on failure"
    contract instead of letting the raw `sqlite3.OperationalError` crash
    the caller (`coord merge`'s CI-fix queue, the review auto-loop, …)."""

    def _work(self, machine: str = "laptop") -> Assignment:
        return Assignment(
            machine_name=machine,
            repo_name="api",
            issue_number=5,
            issue_title="Fix thing",
            briefing="Original briefing.",
            assignment_id="work-2538",
            status="done",
            branch="issue-5-fix-thing",
            dispatched_at=0.0,
            finished_at=1.0,
            type="work",
        )

    def test_persistent_lock_contention_returns_none_without_crashing(
        self,
    ) -> None:
        cfg = _two_machine_config()
        work = self._work()
        board = Board(completed=[work])
        mock_http = MagicMock()
        mock_http.post.return_value.json.return_value = {"id": "fix-2538-a"}
        mock_http.post.return_value.raise_for_status = MagicMock()

        with patch(
            "coord.auto_loop.record_dispatched_assignment",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            result = _dispatch_fix(
                work, "Fix briefing.", board, cfg, iteration=1,
                http_client=mock_http,
            )

        assert result is None

    def test_persistent_lock_contention_rolls_back_the_board_append(
        self,
    ) -> None:
        """The fix assignment is appended to `board.active` before the DB
        write is attempted (so a fully successful dispatch needs no extra
        round trip) — a failed write must undo that append, or a later
        `save_board` call (triggered by some OTHER entry succeeding in the
        same `coord merge` tick) would persist a phantom row that was never
        actually recorded."""
        cfg = _two_machine_config()
        work = self._work()
        board = Board(completed=[work])
        mock_http = MagicMock()
        mock_http.post.return_value.json.return_value = {"id": "fix-2538-b"}
        mock_http.post.return_value.raise_for_status = MagicMock()

        with patch(
            "coord.auto_loop.record_dispatched_assignment",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            _dispatch_fix(
                work, "Fix briefing.", board, cfg, iteration=1,
                http_client=mock_http,
            )

        assert board.active == []

    def test_unrelated_operational_error_still_propagates(self) -> None:
        """A genuine bug (schema drift, a malformed statement) must not be
        swallowed as if it were routine transient contention — only the
        specific `database is locked` message is treated as declinable."""
        cfg = _two_machine_config()
        work = self._work()
        board = Board(completed=[work])
        mock_http = MagicMock()
        mock_http.post.return_value.json.return_value = {"id": "fix-2538-c"}
        mock_http.post.return_value.raise_for_status = MagicMock()

        with patch(
            "coord.auto_loop.record_dispatched_assignment",
            side_effect=sqlite3.OperationalError("no such column: bogus"),
        ):
            with pytest.raises(sqlite3.OperationalError, match="no such column"):
                _dispatch_fix(
                    work, "Fix briefing.", board, cfg, iteration=1,
                    http_client=mock_http,
                )


# ── #1176: test-author bounce gets a type="test-author" fix, not "work" ─────


class TestDispatchFixTestAuthorType:
    """A review bounce of a type="test-author" row must dispatch a
    type="test-author" fix (with TEST_AUTHOR_SYSTEM_PROMPT + its deny-list
    and permission to touch tests/acceptance/**), not a plain type="work"
    fix that is forbidden from the exact files it needs to fix."""

    def _dispatch(
        self, config: Config, *, work_type: str, for_issue_number: int | None = None
    ) -> tuple[dict, Assignment]:
        work = _work_assignment(
            assignment_id="ta-work-1",
            branch="test-author-ms-3-slice-9",
            type=work_type,
            for_issue_number=for_issue_number,
        )
        board = Board(completed=[work])
        mock_http = MagicMock()
        mock_http.post.return_value.json.return_value = {"id": "fix-ta-1"}
        mock_http.post.return_value.raise_for_status = MagicMock()

        with patch("coord.auto_loop.record_dispatched_assignment"):
            result = _dispatch_fix(
                work, "Fix briefing.", board, config, iteration=1,
                http_client=mock_http,
            )

        assert result is not None
        sent_payload = mock_http.post.call_args.kwargs["json"]
        return sent_payload, result

    def test_payload_type_is_test_author(self, config: Config) -> None:
        payload, _ = self._dispatch(config, work_type="test-author")
        assert payload["type"] == "test-author"

    def test_assignment_type_is_test_author(self, config: Config) -> None:
        _, fix = self._dispatch(config, work_type="test-author")
        assert fix.type == "test-author"

    def test_payload_carries_test_author_system_prompt(self, config: Config) -> None:
        from coord.test_author import TEST_AUTHOR_SYSTEM_PROMPT

        payload, _ = self._dispatch(config, work_type="test-author")
        assert payload["system_prompt"] == TEST_AUTHOR_SYSTEM_PROMPT

    def test_payload_carries_test_author_deny_commands(self, config: Config) -> None:
        payload, _ = self._dispatch(config, work_type="test-author")
        assert "Bash(gh *)" in payload["deny_commands"]

    def test_files_forbidden_does_not_block_acceptance_path(
        self, config: Config
    ) -> None:
        """The source test-author row carries files_forbidden=[] (#931) —
        confirm the fix dispatch doesn't add tests/acceptance/ to it."""
        payload, _ = self._dispatch(config, work_type="test-author")
        assert not any(
            "tests/acceptance" in f for f in payload["files_forbidden"]
        )

    def test_for_issue_number_carried_over(self, config: Config) -> None:
        """#1084 JIT-slice correlation survives the bounce so the TUI still
        recognizes this fix as the same member issue's slice."""
        _, fix = self._dispatch(config, work_type="test-author", for_issue_number=42)
        assert fix.for_issue_number == 42

    def test_work_type_bounce_is_unchanged(self, config: Config) -> None:
        """Regression guard: a type="work" bounce still gets a plain "work"
        fix with no test-author system prompt or deny-list injected."""
        payload, fix = self._dispatch(config, work_type="work")
        assert payload["type"] == "work"
        assert fix.type == "work"
        assert "system_prompt" not in payload
        assert "deny_commands" not in payload


# ── #2302: mock-author bounce gets a type="mock-author" fix, not "work" ─────


class TestDispatchFixMockAuthorType:
    """A review bounce of a type="mock-author" (Gate A) row must dispatch a
    type="mock-author" fix, not a plain type="work" fix — `coord/review.py`
    only inverts the sealed-path tamper rule for
    `coord.models.SEALED_PATH_AUTHOR_TYPES`, so a "work"-typed fix confined
    to `tests/acceptance/ms-NN/**` trips "TAMPER DETECTED" on every round."""

    def _dispatch(
        self, config: Config, *, work_type: str, for_issue_number: int | None = None
    ) -> tuple[dict, Assignment]:
        work = _work_assignment(
            assignment_id="ma-work-1",
            branch="ms-65-gate-a",
            type=work_type,
            for_issue_number=for_issue_number,
        )
        board = Board(completed=[work])
        mock_http = MagicMock()
        mock_http.post.return_value.json.return_value = {"id": "fix-ma-1"}
        mock_http.post.return_value.raise_for_status = MagicMock()

        with patch("coord.auto_loop.record_dispatched_assignment"):
            result = _dispatch_fix(
                work, "Fix briefing.", board, config, iteration=1,
                http_client=mock_http,
            )

        assert result is not None
        sent_payload = mock_http.post.call_args.kwargs["json"]
        return sent_payload, result

    def test_payload_type_is_mock_author(self, config: Config) -> None:
        payload, _ = self._dispatch(config, work_type="mock-author")
        assert payload["type"] == "mock-author"

    def test_assignment_type_is_mock_author(self, config: Config) -> None:
        _, fix = self._dispatch(config, work_type="mock-author")
        assert fix.type == "mock-author"

    def test_files_forbidden_does_not_block_acceptance_path(
        self, config: Config
    ) -> None:
        """The source mock-author row carries files_forbidden=[] — confirm
        the fix dispatch doesn't add tests/acceptance/ to it."""
        payload, _ = self._dispatch(config, work_type="mock-author")
        assert not any(
            "tests/acceptance" in f for f in payload["files_forbidden"]
        )

    def test_for_issue_number_carried_over(self, config: Config) -> None:
        _, fix = self._dispatch(config, work_type="mock-author", for_issue_number=65)
        assert fix.for_issue_number == 65

    def test_work_type_bounce_is_unchanged(self, config: Config) -> None:
        """Regression guard: a type="work" bounce still gets a plain "work"
        fix — untouched by the mock-author addition."""
        payload, fix = self._dispatch(config, work_type="work")
        assert payload["type"] == "work"
        assert fix.type == "work"


class TestFixDispatchTypesIncludesMockAuthor:
    """#2302: FIX_DISPATCH_TYPES must include every type `_dispatch_fix` can
    actually emit — this is the same drift class as #1141
    ("test-author was never added to WORK_LIKE_TYPES")."""

    def test_mock_author_is_a_member(self) -> None:
        from coord.auto_loop import FIX_DISPATCH_TYPES

        assert "mock-author" in FIX_DISPATCH_TYPES

    def test_all_sealed_path_author_types_are_members(self) -> None:
        from coord.auto_loop import FIX_DISPATCH_TYPES
        from coord.models import SEALED_PATH_AUTHOR_TYPES

        assert SEALED_PATH_AUTHOR_TYPES <= FIX_DISPATCH_TYPES


class TestProcessReviewCompletionTestAuthorType:
    """End-to-end: a `coord bounce` (via process_review_completion) of a
    request-changes review on a type="test-author" work row dispatches a
    type="test-author" fix carrying the reviewer's findings."""

    def test_full_cycle_dispatches_test_author_fix(
        self, config: Config, tmp_path
    ) -> None:
        log_file = tmp_path / "review.log"
        log_file.write_text(
            "REVIEW_VERDICT: request-changes\n"
            "REVIEW_BODY:\n"
            "- Dead monkeypatch.setattr on a non-existent attribute\n"
            "- Fragile assertion would false-fail a correct implementation\n"
            "END_REVIEW\n"
        )
        work = _work_assignment(
            assignment_id="ta-work-2",
            branch="test-author-ms-3-slice-9",
            type="test-author",
        )
        review = _review_assignment(assignment_id="ta-review-2", review_of="ta-work-2")
        board = _board_with(work, review)

        mock_http = MagicMock()
        mock_http.post.return_value.json.return_value = {"id": "fix-ta-2"}
        mock_http.post.return_value.raise_for_status = MagicMock()

        with patch("coord.auto_loop.record_dispatched_assignment"):
            actions = process_review_completion(
                review, board, config,
                log_path=str(log_file),
                http_client=mock_http,
            )

        assert any(a.kind == "fix_dispatched" for a in actions)
        sent_payload = mock_http.post.call_args.kwargs["json"]
        fix = board.active[0]

        assert sent_payload["type"] == "test-author"
        assert fix.type == "test-author"
        assert "Dead monkeypatch.setattr" in sent_payload["briefing"]
        assert "Fragile assertion" in sent_payload["briefing"]
        assert "ensure all tests pass" not in sent_payload["briefing"].lower()


class TestProcessReviewCompletionMockAuthorType:
    """End-to-end: a `coord bounce` (via process_review_completion) of a
    request-changes review on a type="mock-author" (Gate A) work row
    dispatches a type="mock-author" fix carrying the reviewer's findings —
    the #2289 shape this issue exists to fix."""

    def test_full_cycle_dispatches_mock_author_fix(
        self, config: Config, tmp_path
    ) -> None:
        log_file = tmp_path / "review.log"
        log_file.write_text(
            "REVIEW_VERDICT: request-changes\n"
            "REVIEW_BODY:\n"
            "- Mock depicts a state the contract's own rules can't reach\n"
            "- Contract names a field the mocks never render\n"
            "END_REVIEW\n"
        )
        work = _work_assignment(
            assignment_id="ma-work-2",
            branch="ms-65-gate-a",
            type="mock-author",
        )
        review = _review_assignment(assignment_id="ma-review-2", review_of="ma-work-2")
        board = _board_with(work, review)

        mock_http = MagicMock()
        mock_http.post.return_value.json.return_value = {"id": "fix-ma-2"}
        mock_http.post.return_value.raise_for_status = MagicMock()

        with patch("coord.auto_loop.record_dispatched_assignment"):
            actions = process_review_completion(
                review, board, config,
                log_path=str(log_file),
                http_client=mock_http,
            )

        assert any(a.kind == "fix_dispatched" for a in actions)
        sent_payload = mock_http.post.call_args.kwargs["json"]
        fix = board.active[0]

        assert sent_payload["type"] == "mock-author"
        assert fix.type == "mock-author"
        assert "Mock depicts a state" in sent_payload["briefing"]
        assert "Contract names a field" in sent_payload["briefing"]
        assert "ensure all tests pass" not in sent_payload["briefing"].lower()
        assert "make them pass" not in sent_payload["briefing"].lower()
