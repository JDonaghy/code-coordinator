"""#2996: `enqueue-status planned` silently withdraws a submission from
decomposition, with no warning and an unhelpful error later.

Covers the two new, non-underscore seams `coord.approved_work` gained so
callers elsewhere (`coord.commands.portal`, `coord.decomposition_chat`) can
reason about `_PULLED_STATUSES` without reaching into a private constant:

* :func:`coord.approved_work.is_pulled_status` — the public "would this
  status withdraw a submission?" predicate `enqueue-status` warns on.
* :func:`coord.approved_work.disqualifying_status` — "why is this submission
  missing from `approved_submissions`", used to make `resolve_approved_
  submission`'s failure message name the actual cause instead of the
  generic, unhelpful "is not a currently-approved portal submission".
"""

from __future__ import annotations

import pytest

from coord.approved_work import disqualifying_status, is_pulled_status


def _signoff(event_id: str, submission_id: str, verdict: str) -> dict:
    return {
        "id": event_id,
        "submission_id": submission_id,
        "type": f"signoff.{verdict}",
    }


def _approve(submission_id: str, *, now: float = 100.0) -> None:
    from coord import portal_store

    portal_store.record_events(
        [_signoff(f"ev-{submission_id}", submission_id, "approved")], now=now
    )


def _set_last_status(submission_id: str, status: str, *, now: float) -> None:
    """Confirm *status* as the submission's `last_status`, bypassing the real
    push round-trip — mirrors `tests/test_approved_work_2532.py`'s own
    helper of the same name/shape."""
    from coord import portal_store

    row = portal_store.enqueue(submission_id, "status", {"status": status}, now=now)
    portal_store.mark_applied(row, now=now)


class TestIsPulledStatus:
    @pytest.mark.parametrize(
        "status", ["planned", "in-progress", "quality-check", "shipped"]
    )
    def test_true_for_every_pulled_status(self, status: str) -> None:
        assert is_pulled_status(status) is True

    @pytest.mark.parametrize(
        "status",
        ["", "describing", "in-design", "awaiting-signoff", "needs-input", "on-hold"],
    )
    def test_false_for_every_pre_decomposition_or_interrupt_status(
        self, status: str
    ) -> None:
        assert is_pulled_status(status) is False


class TestDisqualifyingStatus:
    def test_none_for_a_submission_never_seen_at_all(self, coord_db) -> None:
        assert disqualifying_status("sub_never_seen") is None

    def test_none_when_the_submission_was_never_approved(self, coord_db) -> None:
        """`last_status` alone is never enough — only an approved sign-off
        that has ALSO moved to a pulled status is disqualified for that
        reason. A submission that just happens to carry `planned` with no
        signoff at all was never a candidate for the panel in the first
        place, so this must not misreport `planned` as the cause."""
        _set_last_status("sub_unapproved", "planned", now=1.0)
        assert disqualifying_status("sub_unapproved") is None

    def test_none_when_approved_and_not_pulled(self, coord_db) -> None:
        _approve("sub_waiting")
        _set_last_status("sub_waiting", "in-design", now=2.0)
        assert disqualifying_status("sub_waiting") is None

    @pytest.mark.parametrize(
        "status", ["planned", "in-progress", "quality-check", "shipped"]
    )
    def test_names_the_pulled_status_when_that_is_the_cause(
        self, status: str, coord_db
    ) -> None:
        _approve("sub_pulled")
        _set_last_status("sub_pulled", status, now=2.0)
        assert disqualifying_status("sub_pulled") == status

    def test_a_walked_back_approval_is_not_reported_as_disqualified(
        self, coord_db
    ) -> None:
        """A `changes_requested` verdict after an earlier `approved` one
        means the LATEST verdict is not `approved` at all — this function's
        job is narrower than "was ever approved", so it must not report a
        `_PULLED_STATUSES` value here as the reason (there is a more
        fundamental one: it was never approved to begin with)."""
        from coord import portal_store

        portal_store.record_events(
            [_signoff("e1", "sub_walked_back", "approved")], now=1.0
        )
        portal_store.record_events(
            [_signoff("e2", "sub_walked_back", "changes_requested")], now=2.0
        )
        _set_last_status("sub_walked_back", "planned", now=3.0)
        assert disqualifying_status("sub_walked_back") is None


class TestDescribeUnapprovedSubmissionNeverRaises:
    """`describe_unapproved_submission` only *phrases* a failure its callers
    have already decided on, so an exception escaping its best-effort
    enrichment lookup would replace a clear, actionable error with a
    traceback about an unrelated subsystem (an unreadable/locked portal
    store, a schema older than `disqualifying_status` expects). It must
    degrade to the plain generic message instead."""

    def test_falls_back_to_the_generic_message_when_the_store_read_raises(
        self, coord_db, monkeypatch
    ) -> None:
        from coord import approved_work, decomposition_chat

        def _boom(_submission_id: str) -> str | None:
            raise RuntimeError("portal store unreadable")

        monkeypatch.setattr(approved_work, "disqualifying_status", _boom)

        msg = decomposition_chat.describe_unapproved_submission(None, "sub_boom")

        assert "is not a currently-approved portal submission" in msg
        assert "its last_status is" not in msg
        assert "portal store unreadable" not in msg

    def test_falls_back_when_board_service_resolution_itself_raises(
        self, coord_db, monkeypatch
    ) -> None:
        from coord import board_service, decomposition_chat

        def _boom():
            raise RuntimeError("client.toml is corrupt")

        monkeypatch.setattr(board_service, "resolve", _boom)

        msg = decomposition_chat.describe_unapproved_submission(None, "sub_boom")

        assert "is not a currently-approved portal submission" in msg
        assert "client.toml is corrupt" not in msg

    def test_still_names_the_status_on_the_happy_path(self, coord_db) -> None:
        """The fallback must not have swallowed the enrichment itself."""
        from coord import decomposition_chat

        _approve("sub_enriched")
        _set_last_status("sub_enriched", "planned", now=2.0)

        msg = decomposition_chat.describe_unapproved_submission(None, "sub_enriched")

        assert "its last_status is 'planned'" in msg
        assert "in-design" in msg
