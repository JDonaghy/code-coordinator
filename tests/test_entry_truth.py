"""Tests for coord.entry_truth (#3922) — the single reconciled EntryTruth,
derived from live facts only, never a persisted conclusion.
"""

from __future__ import annotations

import pytest

from coord import merge_queue as mq
from coord.ci_store import CheckRun
from coord.config import Config, PipelineConfig
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
        pr_state_for_branch: str | None = None,
    ):
        self.branch_sha = branch_sha
        self.base_sha = base_sha
        self.patch_id = patch_id
        self.issue_state = issue_state
        self.pr = pr
        self.pr_merged = pr_merged
        self.mergeable_override = mergeable_override
        self.raise_on_branch_sha = raise_on_branch_sha
        # `None` means "this stand-in doesn't implement the optional
        # `get_pr_state_for_branch` capability at all" — distinct from the
        # method existing and itself returning `None` (no PR/read failure).
        self.pr_state_for_branch = pr_state_for_branch

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


class FakeGhWithPrState(FakeGh):
    """`FakeGh` plus the optional `get_pr_state_for_branch` capability, so a
    genuinely CONFIRMED closed-without-merging PR can be exercised
    distinctly from the ordinary (and far more common) "no PR yet" case."""

    def get_pr_state_for_branch(self, repo: str, branch: str) -> str | None:
        return self.pr_state_for_branch


class FakeCi:
    def __init__(self, checks: list[CheckRun] | None = None, *, expects_checks: bool = True):
        self.checks = checks or []
        self._expects_checks = expects_checks

    def list_checks_for_pr(self, repo: str, number: int) -> list[CheckRun]:
        return self.checks

    def expects_checks(self, repo: str, number: int) -> bool:
        return self._expects_checks


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

    def test_merge_queue_error_string_never_drives_next_action(
        self, config: Config, coord_db,
    ) -> None:
        """Rule 1: a persisted `merge_queue` row's raw `error` TEXT never
        drives `next_action` on its own — only a genuine operator latch
        (HUMAN_REQUIRED/SKIPPED, see `test_human_required_merge_queue_entry_
        escalates_rather_than_merging` below) does. A PENDING row carrying
        an `error` string (e.g. a transient failure note from an earlier
        attempt) must not, by itself, block a now-clean entry."""
        board = Board(active=[], completed=[_work(), _review("w1")])
        mq.save_queue([
            mq.QueuedMerge(
                assignment_id="w1", repo_name="api", repo_github="acme/api",
                branch="issue-42-foo", target_branch="main", issue_number=42,
                issue_title="t", state=mq.PENDING,
                error="transient failure from an earlier attempt",
            )
        ])
        gh = FakeGh(pr={"number": 7, "mergeable": True})
        ci = FakeCi([_check("build", "completed", "success")])
        truth = reconcile_entry(board, config, "api", 42, gh_ops=gh, ci_store=ci)
        assert truth.next_action == NEXT_ACTION_MERGE

    def test_human_required_merge_queue_entry_escalates_rather_than_merging(
        self, config: Config, coord_db,
    ) -> None:
        """#3445: a latched HUMAN_REQUIRED `merge_queue` row is the one fact
        nothing else `reconcile_entry` derives can see — every review/test/
        CI/PR live fact can read clear while a human has already parked
        this entry, and `coord merge` genuinely refuses it without
        `--override-human-required`. `next_action` must say so, not `merge`."""
        board = Board(active=[], completed=[_work(), _review("w1")])
        mq.save_queue([
            mq.QueuedMerge(
                assignment_id="w1", repo_name="api", repo_github="acme/api",
                branch="issue-42-foo", target_branch="main", issue_number=42,
                issue_title="t", state=mq.HUMAN_REQUIRED,
                error="stale-rebase worker refused to push",
                last_attempt=1_700_000_000.0,
            )
        ])
        gh = FakeGh(pr={"number": 7, "mergeable": True})
        ci = FakeCi([_check("build", "completed", "success")])
        truth = reconcile_entry(board, config, "api", 42, gh_ops=gh, ci_store=ci)
        assert truth.next_action == NEXT_ACTION_ESCALATE
        assert "HUMAN_REQUIRED" in truth.next_action_reason

    def test_in_flight_worker_leg_waits_rather_than_redispatching(self, config: Config) -> None:
        """A still-running work leg has no verdicts yet — that must read
        `wait`, not `dispatch_review`/`dispatch_test`, which would race it."""
        board = Board(active=[_work(status="active", test_state=None)], completed=[])
        gh = FakeGh()
        truth = reconcile_entry(board, config, "api", 42, gh_ops=gh)
        assert truth.next_action == NEXT_ACTION_WAIT
        assert "w1" in truth.next_action_reason

    def test_test_unmet_outranks_review_unmet_when_test_precedes_review(
        self, config: Config,
    ) -> None:
        """CLAUDE.md: Work → Test → Review. Default `pipeline.default_gates`
        orders `test` ahead of `review`, matching `coord.review.
        dispatch_pending_reviews`'s own gate — when BOTH are unmet, this
        must name `dispatch_test`, never `dispatch_review`."""
        board = Board(active=[], completed=[_work(test_state=None, review_state=None)])
        gh = FakeGh()
        truth = reconcile_entry(board, config, "api", 42, gh_ops=gh)
        assert truth.test is not None and not truth.test.ok
        assert truth.review is not None and not truth.review.ok
        assert truth.next_action == NEXT_ACTION_DISPATCH_TEST

    def test_review_unmet_outranks_test_unmet_when_review_precedes_test(self) -> None:
        """The mirror of the test above, for a repo whose `default_gates`
        orders `review` ahead of `test` — `next_action` must flip to match,
        never hardcode review-first (or test-first) regardless of config."""
        config = Config(
            repos=[Repo(name="api", github="acme/api", default_branch="main")],
            machines=[Machine(name="precision", host="precision.tailnet", repos=["api"])],
            pipeline=PipelineConfig(default_gates=["review", "test", "merge"]),
        )
        board = Board(active=[], completed=[_work(test_state=None, review_state=None)])
        gh = FakeGh()
        truth = reconcile_entry(board, config, "api", 42, gh_ops=gh)
        assert truth.test is not None and not truth.test.ok
        assert truth.review is not None and not truth.review.ok
        assert truth.next_action == NEXT_ACTION_DISPATCH_REVIEW

    def test_pr_mergeable_unknown_waits_never_merges(self, config: Config) -> None:
        """#2704/`check_pr_mergeable`'s own contract: an inconclusive
        mergeability read (`None` — still computing, or the `gh` call
        itself failed) must be treated the same as `False` for deciding
        whether to merge — never a green light."""
        board = Board(active=[], completed=[_work(), _review("w1")])
        gh = FakeGh(pr={"number": 7, "mergeable": "UNKNOWN"}, mergeable_override=None)
        truth = reconcile_entry(board, config, "api", 42, gh_ops=gh)
        assert truth.pr_mergeable is None
        assert truth.next_action == NEXT_ACTION_WAIT

    def test_pr_mergeable_string_shape_matches_production(self, config: Config) -> None:
        """`github_ops.find_pr_for_branch` reports GitHub's `mergeable`
        field as a STRING (`"MERGEABLE"`/`"CONFLICTING"`/`"UNKNOWN"`), never
        a `bool` — the shape every real `gh_ops` call returns, not just the
        bool shape a stub is free to use instead."""
        board = Board(active=[], completed=[_work(), _review("w1")])
        gh = FakeGh(pr={"number": 7, "mergeable": "CONFLICTING"})
        truth = reconcile_entry(board, config, "api", 42, gh_ops=gh)
        assert truth.pr_mergeable is False
        assert truth.next_action == NEXT_ACTION_CONFLICT_FIX

    def test_checks_absent_but_expected_dispatches_ci_fix_not_merge(
        self, config: Config,
    ) -> None:
        """#1904: an empty check list on a repo that DOES declare CI must
        fail closed — never read the same as "no CI gate wired here"."""
        board = Board(active=[], completed=[_work(), _review("w1")])
        gh = FakeGh(pr={"number": 7, "mergeable": True})
        ci = FakeCi([], expects_checks=True)
        truth = reconcile_entry(board, config, "api", 42, gh_ops=gh, ci_store=ci)
        assert truth.ci_state == "checks_absent"
        assert truth.next_action == NEXT_ACTION_DISPATCH_CI_FIX

    def test_checks_absent_and_not_expected_reads_none_and_merges(
        self, config: Config,
    ) -> None:
        """The legitimate "no CI gate wired for this repo" case must stay
        unaffected by the #1904 `checks_absent` fail-closed handling."""
        board = Board(active=[], completed=[_work(), _review("w1")])
        gh = FakeGh(pr={"number": 7, "mergeable": True})
        ci = FakeCi([], expects_checks=False)
        truth = reconcile_entry(board, config, "api", 42, gh_ops=gh, ci_store=ci)
        assert truth.ci_state == "none"
        assert truth.next_action == NEXT_ACTION_MERGE

    def test_ci_probe_failure_waits_rather_than_merging(self, config: Config) -> None:
        """A `ci_store` that raises on the live read is a genuine probe
        failure (`ci_probed=True`) — fail closed to `wait`, never `merge`."""

        class RaisingCi:
            def list_checks_for_pr(self, repo: str, number: int) -> list[CheckRun]:
                raise RuntimeError("ci backend unreachable")

        board = Board(active=[], completed=[_work(), _review("w1")])
        gh = FakeGh(pr={"number": 7, "mergeable": True})
        truth = reconcile_entry(board, config, "api", 42, gh_ops=gh, ci_store=RaisingCi())
        assert truth.ci_state == "unknown"
        assert truth.ci_probed is True
        assert truth.next_action == NEXT_ACTION_WAIT

    def test_no_pr_yet_is_benign_and_still_merges(self, config: Config) -> None:
        """"Approved + tested, PR not yet created" is a normal pipeline
        state (`merge_queue.process()` is what opens the PR) — it must
        never read the same as a confirmed "closed without merging"."""
        board = Board(active=[], completed=[_work(), _review("w1")])
        gh = FakeGh(pr=None, pr_merged=False)
        truth = reconcile_entry(board, config, "api", 42, gh_ops=gh)
        assert truth.pr_state == "none"
        assert truth.next_action == NEXT_ACTION_MERGE

    def test_confirmed_closed_pr_escalates(self, config: Config) -> None:
        """A LIVE, CONFIRMED "closed without merging" read (distinct from
        merely "no open PR found") still escalates — this is the one case
        `get_pr_state_for_branch` exists to confirm."""
        board = Board(active=[], completed=[_work(), _review("w1")])
        gh = FakeGhWithPrState(pr=None, pr_merged=False, pr_state_for_branch="CLOSED")
        truth = reconcile_entry(board, config, "api", 42, gh_ops=gh)
        assert truth.pr_state == "closed"
        assert truth.next_action == NEXT_ACTION_ESCALATE
        assert "closed" in truth.next_action_reason.lower()
