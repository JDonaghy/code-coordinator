"""#3376 deliverable A: unit tests for the single dispatch-liveness
precondition — `coord.dispatch_liveness.check_dispatch_liveness` — in
isolation from `coord.dispatch.dispatch()`'s wiring (covered separately in
`tests/test_dispatch.py::TestDispatchLivenessGate`).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from coord.config import Config
from coord.dispatch_liveness import (
    PREDICATE_BRANCH_MERGED,
    PREDICATE_ISSUE_CLOSED,
    PREDICATE_MACHINE_UNHEALTHY,
    check_dispatch_liveness,
    github_issue_liveness_fetcher,
    record_dispatch_refusal,
)
from coord.models import Repo


class TestCheckDispatchLiveness:
    def test_no_evidence_refuses_nothing(self) -> None:
        """Every predicate defaulting to `None` ("not probed") must be a
        pure no-op — the same posture every other structural gate in this
        codebase takes for absent evidence."""
        assert check_dispatch_liveness(
            repo_name="api", issue_number=1, machine_name="laptop",
        ) is None

    def test_open_unmerged_healthy_refuses_nothing(self) -> None:
        assert check_dispatch_liveness(
            repo_name="api", issue_number=1, machine_name="laptop",
            issue_closed=False, branch_merged=False, machine_healthy=True,
        ) is None

    def test_closed_issue_refuses(self) -> None:
        refusal = check_dispatch_liveness(
            repo_name="api", issue_number=1, machine_name="laptop",
            issue_closed=True,
        )
        assert refusal is not None
        assert refusal.predicate == PREDICATE_ISSUE_CLOSED
        assert "api#1" in refusal.reason
        assert "closed" in refusal.reason

    def test_merged_branch_refuses(self) -> None:
        refusal = check_dispatch_liveness(
            repo_name="api", issue_number=1, machine_name="laptop",
            branch_merged=True,
        )
        assert refusal is not None
        assert refusal.predicate == PREDICATE_BRANCH_MERGED
        assert "merged" in refusal.reason

    def test_unhealthy_machine_refuses(self) -> None:
        refusal = check_dispatch_liveness(
            repo_name="api", issue_number=1, machine_name="dead-box",
            machine_healthy=False,
        )
        assert refusal is not None
        assert refusal.predicate == PREDICATE_MACHINE_UNHEALTHY
        assert "dead-box" in refusal.reason
        assert "not routable" in refusal.reason

    def test_healthy_machine_true_refuses_nothing(self) -> None:
        """`machine_healthy=True` (a probe that positively confirmed
        health) must not be confused with `None` ("never probed") —
        both must refuse nothing, but for a different reason each time."""
        assert check_dispatch_liveness(
            repo_name="api", issue_number=1, machine_name="laptop",
            machine_healthy=True,
        ) is None

    def test_closed_wins_over_merged_and_unhealthy(self) -> None:
        """First predicate in #3376's own order wins — this function
        reports exactly one cause, not a combination."""
        refusal = check_dispatch_liveness(
            repo_name="api", issue_number=1, machine_name="laptop",
            issue_closed=True, branch_merged=True, machine_healthy=False,
        )
        assert refusal is not None
        assert refusal.predicate == PREDICATE_ISSUE_CLOSED

    def test_merged_wins_over_unhealthy(self) -> None:
        refusal = check_dispatch_liveness(
            repo_name="api", issue_number=1, machine_name="laptop",
            issue_closed=False, branch_merged=True, machine_healthy=False,
        )
        assert refusal is not None
        assert refusal.predicate == PREDICATE_BRANCH_MERGED


class TestRecordDispatchRefusal:
    @patch("coord.dispatch_liveness.record_audit")
    def test_records_one_audit_row_with_the_refusal_details(
        self, mock_audit: MagicMock,
    ) -> None:
        refusal = check_dispatch_liveness(
            repo_name="api", issue_number=1, machine_name="laptop",
            issue_closed=True,
        )
        assert refusal is not None
        record_dispatch_refusal(
            refusal,
            repo_name="api", issue_number=1, machine_name="laptop",
            assignment_type="review",
        )
        mock_audit.assert_called_once()
        kwargs = mock_audit.call_args.kwargs
        assert kwargs["tier"] == "business"
        assert kwargs["event_type"] == "dispatch_refused_liveness"
        assert kwargs["repo"] == "api"
        assert kwargs["issue"] == 1
        assert kwargs["machine"] == "laptop"
        assert kwargs["details"]["predicate"] == PREDICATE_ISSUE_CLOSED
        assert kwargs["details"]["assignment_type"] == "review"


class TestGithubIssueLivenessFetcher:
    """#3376 review round 1: `github_issue_liveness_fetcher` is the REAL
    fetcher production callers wire into `coord.dispatch.dispatch()`'s
    `issue_liveness_fetcher` — previously the two predicates existed
    (above) but nothing supplied live facts at any real dispatch
    chokepoint. These pin the factory's own behavior in isolation from any
    particular call site.
    """

    def _config(self) -> Config:
        return Config(repos=[Repo(name="api", github="acme/api")], machines=[])

    @patch("coord.claim.any_matching_branch_merged", return_value=False)
    @patch("coord.github_ops.issue_is_closed", return_value=True)
    def test_resolves_repo_name_to_github_and_reports_closed(
        self, mock_closed: MagicMock, mock_merged: MagicMock,
    ) -> None:
        fetcher = github_issue_liveness_fetcher(self._config())
        issue_closed, branch_merged = fetcher("api", 42)
        assert issue_closed is True
        assert branch_merged is False
        mock_closed.assert_called_once_with("acme/api", 42)
        mock_merged.assert_called_once_with("acme/api", 42, branch=None)

    @patch("coord.claim.any_matching_branch_merged", return_value=True)
    @patch("coord.github_ops.issue_is_closed", return_value=False)
    def test_reports_branch_merged(
        self, mock_closed: MagicMock, mock_merged: MagicMock,
    ) -> None:
        fetcher = github_issue_liveness_fetcher(self._config())
        issue_closed, branch_merged = fetcher("api", 42)
        assert issue_closed is False
        assert branch_merged is True

    @patch("coord.claim.any_matching_branch_merged", return_value=False)
    @patch("coord.github_ops.issue_is_closed", return_value=False)
    def test_unknown_repo_name_falls_back_to_itself_as_github_slug(
        self, mock_closed: MagicMock, mock_merged: MagicMock,
    ) -> None:
        # No repo named "ghost" in this Config — the fetcher must not
        # raise; it falls back to treating the internal name as the
        # `owner/repo` slug directly (same "never let a missing config
        # entry crash a dispatch" posture every other fetcher in this
        # module takes).
        fetcher = github_issue_liveness_fetcher(self._config())
        issue_closed, branch_merged = fetcher("ghost", 1)
        assert issue_closed is False
        assert branch_merged is False
        mock_closed.assert_called_once_with("ghost", 1)
        mock_merged.assert_called_once_with("ghost", 1, branch=None)

    @patch("coord.claim.any_matching_branch_merged", return_value=False)
    @patch("coord.github_ops.issue_is_closed", return_value=False)
    def test_passes_the_target_branch_through(
        self, mock_closed: MagicMock, mock_merged: MagicMock,
    ) -> None:
        """#3436: a caller that names a specific branch (the dispatch's
        actual target — e.g. `Proposal.target_branch`/`Assignment.branch`)
        must have it forwarded to `any_matching_branch_merged`'s
        branch-scoped check, not silently dropped in favour of the
        issue-scoped fallback."""
        fetcher = github_issue_liveness_fetcher(self._config())
        issue_closed, branch_merged = fetcher("api", 42, "issue-42-real-work")
        assert issue_closed is False
        assert branch_merged is False
        mock_merged.assert_called_once_with(
            "acme/api", 42, branch="issue-42-real-work"
        )
