"""Tests for coord.entry_truth (#3922) — the single reconciled EntryTruth,
derived from live facts only, never a persisted conclusion.
"""

from __future__ import annotations

import pytest

from coord.ci_store import CheckRun
from coord.config import Config
from coord.entry_truth import (
    NEXT_ACTION_CONFLICT_FIX,
    NEXT_ACTION_DISPATCH_CI_FIX,
    NEXT_ACTION_DISPATCH_REVIEW,
    NEXT_ACTION_DISPATCH_TEST,
    NEXT_ACTION_ESCALATE,
    NEXT_ACTION_MARK_DONE,
    NEXT_ACTION_MERGE,
    NEXT_ACTION_UNKNOWN,
    NEXT_ACTION_WAIT,
    reconcile_entry,
)
from coord.models import Assignment, Board, Machine, Repo


@pytest.fixture
def config() -> Config:
    return Config(
        repos=[Repo(name="api", github="acme/api", default_branch="main")],
        machines=[Machine(name="precision", host="precision.tailnet", repos=["api"])],
    )


def _work(
    *,
    aid: str = "w1",
    issue: int = 42,
    branch: str | None = "issue-42-foo",
    status: str = "done",
    test_state: str | None = "passed",
    review_state: str | None = "done",
    review_verdict: str | None = "approve",
    required_gates: list[str] | None = None,
    dispatched_at: float | None = 1.0,
) -> Assignment:
    return Assignment(
        machine_name="precision",
        repo_name="api",
        issue_number=issue,
        issue_title="t",
        assignment_id=aid,
        type="work",
        status=status,
        branch=branch,
        test_state=test_state,
        review_state=review_state,
        review_verdict=review_verdict,
        required_gates=required_gates or [],
        dispatched_at=dispatched_at,
    )


def _review(
    of_aid: str, *, aid: str = "r1", issue: int = 42, verdict: str | None = "approve",
) -> Assignment:
    return Assignment(
        machine_name="dellserver",
        repo_name="api",
        issue_number=issue,
        issue_title="t",
        assignment_id=aid,
        type="review",
        status="done",
        review_of_assignment_id=of_aid,
        review_verdict=verdict,
        dispatched_at=2.0,
    )


class FakeGh:
    """Stub gh_ops — every live fact `reconcile_entry` reads, fully under
    test control, with none of it ever read back from a persisted value."""

    def __init__(
        self,
        *,
        branch_sha: str | None = "branchsha",
        base_sha: str | None = "basesha",
        patch_id: str | None = "patchid",
        issue_state: str | None = "open",
        pr: dict | None = None,
        pr_merged: bool = False,
        mergeable_override: bool | None = None,
        raise_on_branch_sha: bool = False,
    ):
        self.branch_sha = branch_sha
        self.base_sha = base_sha
        self.patch_id = patch_id
        self.issue_state = issue_state
        self.pr = pr
        self.pr_merged = pr_merged
        self.mergeable_override = mergeable_override
        self.raise_on_branch_sha = raise_on_branch_sha

    def get_branch_sha(self, repo: str, branch: str) -> str | None:
        if self.raise_on_branch_sha:
            raise RuntimeError("gh unreachable")
        return self.branch_sha if branch != "main" else self.base_sha

    def get_branch_patch_id(self, repo: str, base: str, branch: str) -> str | None:
        return self.patch_id

    def get_issue_state(self, repo: str, issue_number: int) -> str | None:
        return self.issue_state

    def find_pr_for_branch(self, repo: str, branch: str) -> dict | None:
        return self.pr

    def pr_is_merged(self, repo: str, branch: str) -> bool:
        return self.pr_merged

    def check_pr_mergeable(self, repo: str, number: int) -> bool | None:
        return self.mergeable_override


class FakeCi:
    def __init__(self, checks: list[CheckRun] | None = None):
        self.checks = checks or []

    def list_checks_for_pr(self, repo: str, number: int) -> list[CheckRun]:
        return self.checks


def _check(name: str, status: str, conclusion: str | None) -> CheckRun:
    return CheckRun(
        name=name, status=status, conclusion=conclusion, url="https://x",
        run_id="1", started_at=None, completed_at=None,
    )


class TestLiveFacts:
    def test_no_gh_ops_leaves_every_live_field_unknown(self, config: Config) -> None:
        board = Board(active=[], completed=[_work()])
        truth = reconcile_entry(board, config, "api", 42)
        assert truth.head_sha is None
        assert truth.patch_id is None
        assert truth.issue_state == "unknown"
        assert truth.pr_state == "unknown"
        assert truth.next_action == NEXT_ACTION_UNKNOWN

    def test_branch_head_probe_failure_fails_closed_not_stale_reused(
        self, config: Config
    ) -> None:
        """#2704: a probe failure must never be silently treated as "use
        whatever was last known" — it reads unknown and escalates."""
        board = Board(active=[], completed=[_work(), _review("w1")])
        gh = FakeGh(raise_on_branch_sha=True)
        truth = reconcile_entry(board, config, "api", 42, gh_ops=gh)
        assert truth.head_sha is None
        assert truth.next_action == NEXT_ACTION_ESCALATE
        assert "2704" in truth.next_action_reason

    def test_latest_worker_status_is_a_direct_live_read(self, config: Config) -> None:
        board = Board(active=[], completed=[_work(status="refused_premise")])
        gh = FakeGh()
        truth = reconcile_entry(board, config, "api", 42, gh_ops=gh)
        assert truth.latest_worker_status == "refused_premise"
        assert truth.latest_worker_assignment_id == "w1"


class TestNextAction:
    def test_issue_closed_wins_over_everything_else(self, config: Config) -> None:
        board = Board(active=[], completed=[_work(), _review("w1")])
        gh = FakeGh(issue_state="closed")
        truth = reconcile_entry(board, config, "api", 42, gh_ops=gh)
        assert truth.next_action == NEXT_ACTION_MARK_DONE

    def test_merged_pr_reports_mark_done(self, config: Config) -> None:
        board = Board(active=[], completed=[_work(), _review("w1")])
        gh = FakeGh(pr=None, pr_merged=True)
        truth = reconcile_entry(board, config, "api", 42, gh_ops=gh)
        assert truth.pr_state == "merged"
        assert truth.next_action == NEXT_ACTION_MARK_DONE

    def test_refused_premise_escalates_rather_than_redispatching(
        self, config: Config
    ) -> None:
        """#3923: an ADVISORY/REFUSED_PREMISE worker outcome needs operator
        recovery, not another dispatch pretending the gates are just unmet."""
        board = Board(active=[], completed=[_work(status="refused_premise")])
        gh = FakeGh()
        truth = reconcile_entry(board, config, "api", 42, gh_ops=gh)
        assert truth.next_action == NEXT_ACTION_ESCALATE
        assert "refused_premise" in truth.next_action_reason

    def test_review_required_but_missing_dispatches_review(self, config: Config) -> None:
        board = Board(active=[], completed=[_work()])
        gh = FakeGh()
        truth = reconcile_entry(board, config, "api", 42, gh_ops=gh)
        assert truth.review is not None and truth.review.required
        assert not truth.review.ok
        assert truth.next_action == NEXT_ACTION_DISPATCH_REVIEW

    def test_test_required_but_missing_dispatches_test(self, config: Config) -> None:
        # default_gates == ["test", "review", "merge"] (coord.config.
        # PipelineConfig) already requires "test" with no per-row override.
        board = Board(
            active=[], completed=[_work(test_state=None), _review("w1")],
        )
        gh = FakeGh()
        truth = reconcile_entry(board, config, "api", 42, gh_ops=gh)
        assert truth.next_action == NEXT_ACTION_DISPATCH_TEST

    def test_pr_conflicting_dispatches_conflict_fix(self, config: Config) -> None:
        board = Board(active=[], completed=[_work(), _review("w1")])
        gh = FakeGh(pr={"number": 7, "mergeable": False})
        truth = reconcile_entry(board, config, "api", 42, gh_ops=gh)
        assert truth.pr_mergeable is False
        assert truth.next_action == NEXT_ACTION_CONFLICT_FIX

    def test_red_ci_dispatches_ci_fix(self, config: Config) -> None:
        board = Board(active=[], completed=[_work(), _review("w1")])
        gh = FakeGh(pr={"number": 7, "mergeable": True})
        ci = FakeCi([_check("build", "completed", "failure")])
        truth = reconcile_entry(board, config, "api", 42, gh_ops=gh, ci_store=ci)
        assert truth.ci_state == "red"
        assert truth.next_action == NEXT_ACTION_DISPATCH_CI_FIX

    def test_pending_ci_waits(self, config: Config) -> None:
        board = Board(active=[], completed=[_work(), _review("w1")])
        gh = FakeGh(pr={"number": 7, "mergeable": True})
        ci = FakeCi([_check("build", "in_progress", None)])
        truth = reconcile_entry(board, config, "api", 42, gh_ops=gh, ci_store=ci)
        assert truth.ci_state == "pending"
        assert truth.next_action == NEXT_ACTION_WAIT

    def test_all_clear_reports_merge(self, config: Config) -> None:
        board = Board(active=[], completed=[_work(), _review("w1")])
        gh = FakeGh(pr={"number": 7, "mergeable": True})
        ci = FakeCi([_check("build", "completed", "success")])
        truth = reconcile_entry(board, config, "api", 42, gh_ops=gh, ci_store=ci)
        assert truth.ci_state == "green"
        assert truth.next_action == NEXT_ACTION_MERGE

    def test_merge_queue_error_string_never_drives_next_action(self, config: Config) -> None:
        """Rule 1: a persisted `merge_queue` row's `error` is NOT one of the
        live facts `reconcile_entry` reads at all — this test documents that
        by asserting a clean merge-ready entry stays `merge` regardless of
        what any out-of-band merge_queue row might say, since no merge_queue
        read happens in `reconcile_entry` in the first place."""
        board = Board(active=[], completed=[_work(), _review("w1")])
        gh = FakeGh(pr={"number": 7, "mergeable": True})
        ci = FakeCi([_check("build", "completed", "success")])
        truth = reconcile_entry(board, config, "api", 42, gh_ops=gh, ci_store=ci)
        assert truth.next_action == NEXT_ACTION_MERGE
