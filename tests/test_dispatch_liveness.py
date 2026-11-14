"""#3376 deliverable A: unit tests for the single dispatch-liveness
precondition — `coord.dispatch_liveness.check_dispatch_liveness` — in
isolation from `coord.dispatch.dispatch()`'s wiring (covered separately in
`tests/test_dispatch.py::TestDispatchLivenessGate`).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from coord.config import Config
from coord.dispatch_liveness import (
    CREDENTIAL_PROBE_REFUSAL_MARKER,
    PREDICATE_BRANCH_MERGED,
    PREDICATE_ISSUE_CLOSED,
    PREDICATE_MACHINE_UNHEALTHY,
    check_dispatch_liveness,
    github_issue_liveness_fetcher,
    is_credential_probe_refusal_reason,
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

    def test_unhealthy_machine_reason_carries_the_credential_probe_marker(
        self,
    ) -> None:
        """#4004: `is_credential_probe_refusal_reason` (this module)
        recognises this ONE predicate by this marker, independent of the
        exit-code bookkeeping every `DispatchRefused` shares — pin that
        the marker is actually in the default (non-timeout) message."""
        refusal = check_dispatch_liveness(
            repo_name="api", issue_number=1, machine_name="dead-box",
            machine_healthy=False,
        )
        assert refusal is not None
        assert CREDENTIAL_PROBE_REFUSAL_MARKER in refusal.reason
        assert is_credential_probe_refusal_reason(refusal.reason)

    def test_timed_out_probe_gets_a_different_message_without_re_authenticate(
        self,
    ) -> None:
        """#4004: a probe TIMEOUT is not evidence the credential is dead —
        the message must say so distinctly, and must NOT tell the operator
        to re-authenticate on that evidence."""
        refusal = check_dispatch_liveness(
            repo_name="api", issue_number=1, machine_name="precision",
            machine_healthy=False, machine_health_timed_out=True,
        )
        assert refusal is not None
        assert refusal.predicate == PREDICATE_MACHINE_UNHEALTHY
        assert "TIMED OUT" in refusal.reason
        assert "re-authenticate" not in refusal.reason
        # Still recognisable as the SAME predicate/marker as the
        # confirmed-dead wording — #4004's re-evaluable classification in
        # `coord.drive_queue` must catch both shapes identically.
        assert CREDENTIAL_PROBE_REFUSAL_MARKER in refusal.reason
        assert is_credential_probe_refusal_reason(refusal.reason)

    def test_confirmed_dead_probe_still_says_re_authenticate(self) -> None:
        """The default (`machine_health_timed_out=False`, every pre-#4004
        caller) must be byte-for-byte unchanged — a REAL dead credential
        still gets told to re-authenticate."""
        refusal = check_dispatch_liveness(
            repo_name="api", issue_number=1, machine_name="dead-box",
            machine_healthy=False, machine_health_timed_out=False,
        )
        assert refusal is not None
        assert "re-authenticate" in refusal.reason
        assert "TIMED OUT" not in refusal.reason


class TestIsCredentialProbeRefusalReason:
    def test_none_and_empty_are_false(self) -> None:
        assert is_credential_probe_refusal_reason(None) is False
        assert is_credential_probe_refusal_reason("") is False

    def test_unrelated_refusal_reason_is_false(self) -> None:
        """A `(#1844)`-shaped guard refusal or either of the OTHER two
        `check_dispatch_liveness` predicates must not be mistaken for this
        one — the whole point of a marker-based classifier."""
        closed = check_dispatch_liveness(
            repo_name="api", issue_number=1, machine_name="laptop",
            issue_closed=True,
        )
        assert closed is not None
        assert is_credential_probe_refusal_reason(closed.reason) is False
        assert is_credential_probe_refusal_reason(
            "drive refused to launch api#1: the diff touches a sealed "
            "acceptance path (#1844)"
        ) is False

    def test_marked_reason_is_true_even_with_extra_text_appended(self) -> None:
        """`coord.drive_queue` layers its OWN explanation onto the raw
        `own_reason` text (same convention every other `is_*_reason`
        classifier in this codebase relies on) — the marker must still be
        found after that."""
        refusal = check_dispatch_liveness(
            repo_name="api", issue_number=1, machine_name="dead-box",
            machine_healthy=False,
        )
        assert refusal is not None
        decorated = f"{refusal.reason} — blocking as RE-EVALUABLE (#4004)"
        assert is_credential_probe_refusal_reason(decorated)


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
