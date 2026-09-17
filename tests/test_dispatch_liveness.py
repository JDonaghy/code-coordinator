"""#3376 deliverable A: unit tests for the single dispatch-liveness
precondition — `coord.dispatch_liveness.check_dispatch_liveness` — in
isolation from `coord.dispatch.dispatch()`'s wiring (covered separately in
`tests/test_dispatch.py::TestDispatchLivenessGate`).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from coord.dispatch_liveness import (
    PREDICATE_BRANCH_MERGED,
    PREDICATE_ISSUE_CLOSED,
    PREDICATE_MACHINE_UNHEALTHY,
    check_dispatch_liveness,
    record_dispatch_refusal,
)


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
