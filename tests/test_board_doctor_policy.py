"""Black-box coverage for #2268's board-doctor policy (Phases 2 and 3).

Drives the real, externally-observable seams: :func:`policy_tier_of` against
:data:`coord.board_doctor_policy.ALLOWLIST`, and
:func:`escalation_alert_dispatcher` against the real `drive_escalations`
table via :mod:`coord.state` — the same pattern `tests/test_board_doctor.py`
uses for the harness it plugs into.
"""

from __future__ import annotations

from coord import state
from coord.board_doctor import ALERT_SKIPPED, TIER_ALERT, TIER_AUTO, StuckRow
from coord.board_doctor_policy import (
    ALLOWLIST,
    AUTO_DISPATCHERS,
    dismiss_resolved_escalation,
    escalation_alert_dispatcher,
    policy_tier_of,
)


def _row(
    key: str, cause: str, queue: str = "drive-queue", evidence: tuple[str, ...] = ()
) -> StuckRow:
    return StuckRow(
        queue=queue,
        key=key,
        state="stuck",
        cause=cause,
        confidence="high",
        evidence=evidence or (f"cause={cause}",),
    )


class TestPolicyTierOf:
    def test_allowlist_is_empty_by_design(self) -> None:
        # No cause is pre-approved for auto-action without Phase 0
        # block-log evidence of it recurring with a known-safe fix.
        assert ALLOWLIST == {}

    def test_every_cause_is_alert_tier_today(self) -> None:
        for cause in ("no_pr", "merge_conflict", "dead_leg", "ci_red", "anything_else"):
            assert policy_tier_of(cause) == TIER_ALERT

    def test_a_future_allowlist_entry_would_be_honored(self, monkeypatch) -> None:
        # Pins the seam itself, not a specific entry: if a cause is ever
        # added to BOTH mappings, policy_tier_of must actually read them
        # rather than hardcoding "always alert".
        monkeypatch.setitem(ALLOWLIST, "probe_only_cause", "probe-only-action")
        monkeypatch.setitem(AUTO_DISPATCHERS, "probe_only_cause", lambda row: True)
        assert policy_tier_of("probe_only_cause") == TIER_AUTO

    def test_allowlist_entry_without_a_dispatcher_stays_alert_tier(self, monkeypatch) -> None:
        # An allowlist entry with no real dispatcher behind it must never
        # silently promote to auto-tier — that would suppress the alert
        # while doing nothing.
        monkeypatch.setitem(ALLOWLIST, "probe_only_cause", "probe-only-action")
        assert "probe_only_cause" not in AUTO_DISPATCHERS
        assert policy_tier_of("probe_only_cause") == TIER_ALERT


class TestEscalationAlertDispatcher:
    def test_drive_queue_row_files_a_real_escalation(self) -> None:
        row = _row("acme/widgets#1762", "merge_conflict")

        assert escalation_alert_dispatcher(row) is True

        escalation = state.get_drive_escalation("acme/widgets", 1762)
        assert escalation is not None
        assert escalation["stage"] == "board-doctor:drive-queue"
        assert escalation["proposed_command"] == "coord status"
        assert "merge_conflict" in escalation["reason"]
        assert "acme/widgets#1762" in escalation["reason"]
        assert "cause=merge_conflict" in escalation["gate_readings"]

    def test_merge_queue_row_parses_the_same_repo_hash_issue_key(self) -> None:
        row = _row("acme/widgets#99", "gate_blocked", queue="merge-queue")

        assert escalation_alert_dispatcher(row) is True

        escalation = state.get_drive_escalation("acme/widgets", 99)
        assert escalation is not None
        assert escalation["stage"] == "board-doctor:merge-queue"

    def test_renewing_the_same_cause_preserves_created_at(self) -> None:
        row = _row("acme/widgets#5", "ci_red")

        escalation_alert_dispatcher(row)
        first = state.get_drive_escalation("acme/widgets", 5)
        assert first is not None

        assert escalation_alert_dispatcher(row) is True
        second = state.get_drive_escalation("acme/widgets", 5)
        assert second is not None
        assert second["created_at"] == first["created_at"]

    def test_unparseable_key_does_not_raise(self) -> None:
        row = _row("no-hash-separator", "ci_red")

        # Must not raise — a malformed key is a dropped alert, not a crash.
        assert escalation_alert_dispatcher(row) is False

        assert state.list_drive_escalations() == []

    def test_empty_repo_name_is_rejected_without_writing(self) -> None:
        # A key like "#12" rpartitions to an empty repo_name — must not
        # write an escalation keyed on an empty repo.
        row = _row("#12", "ci_red")

        assert escalation_alert_dispatcher(row) is False

        assert state.list_drive_escalations() == []

    def test_a_more_specific_existing_escalation_is_left_untouched(self) -> None:
        # A differently-staged escalation (e.g. filed by plan.blocked)
        # already covers this issue — the generic board-doctor alert must
        # not clobber its reason with "coord status".
        state.record_drive_escalation(
            "acme/widgets",
            7,
            stage="plan.blocked",
            reason="blocked on a human decision",
            gate_readings="",
            proposed_command="coord approve 7",
        )
        row = _row("acme/widgets#7", "ci_red")

        # Reported as skipped, NOT delivered: nothing board-doctor filed,
        # so `sweep` must not mark the row escalated and suppress the
        # alert forever — the covering escalation can be dismissed by its
        # own writer at any time.
        assert escalation_alert_dispatcher(row) == ALERT_SKIPPED

        escalation = state.get_drive_escalation("acme/widgets", 7)
        assert escalation is not None
        assert escalation["stage"] == "plan.blocked"
        assert escalation["reason"] == "blocked on a human decision"

    def test_the_alert_is_filed_once_a_covering_escalation_is_dismissed(self) -> None:
        state.record_drive_escalation(
            "acme/widgets",
            11,
            stage="plan.blocked",
            reason="blocked on a human decision",
            gate_readings="",
            proposed_command="coord approve 11",
        )
        row = _row("acme/widgets#11", "ci_red")
        assert escalation_alert_dispatcher(row) == ALERT_SKIPPED

        state.dismiss_drive_escalation("acme/widgets", 11)

        assert escalation_alert_dispatcher(row) is True
        escalation = state.get_drive_escalation("acme/widgets", 11)
        assert escalation is not None
        assert escalation["stage"] == "board-doctor:drive-queue"

    def test_a_write_failure_is_reported_as_not_delivered(self, monkeypatch) -> None:
        # The DB being unavailable must read as a failed alert, not as a
        # silently-swallowed success — `sweep` keys the "escalate exactly
        # once" suppression off this return value.
        def boom(*args, **kwargs):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(state, "record_drive_escalation", boom)

        assert escalation_alert_dispatcher(_row("acme/widgets#12", "ci_red")) is False
        assert state.list_drive_escalations() == []

    def test_a_write_that_does_not_read_back_is_reported_as_not_delivered(
        self, monkeypatch
    ) -> None:
        # Confirmation is an observation taken AFTER the write: a write
        # that reports no error but leaves nothing on file is a failure.
        monkeypatch.setattr(state, "record_drive_escalation", lambda *a, **k: None)

        assert escalation_alert_dispatcher(_row("acme/widgets#13", "ci_red")) is False


class TestDismissResolvedEscalation:
    def test_dismisses_an_escalation_this_module_filed(self) -> None:
        row = _row("acme/widgets#8", "ci_red")
        escalation_alert_dispatcher(row)
        assert state.get_drive_escalation("acme/widgets", 8) is not None

        dismiss_resolved_escalation("drive-queue", "acme/widgets#8")

        assert state.get_drive_escalation("acme/widgets", 8) is None

    def test_leaves_a_differently_staged_escalation_untouched(self) -> None:
        state.record_drive_escalation(
            "acme/widgets",
            9,
            stage="plan.blocked",
            reason="blocked on a human decision",
            gate_readings="",
            proposed_command="coord approve 9",
        )

        dismiss_resolved_escalation("drive-queue", "acme/widgets#9")

        escalation = state.get_drive_escalation("acme/widgets", 9)
        assert escalation is not None
        assert escalation["stage"] == "plan.blocked"

    def test_malformed_key_does_not_raise(self) -> None:
        # Must not raise — a malformed key means nothing to clean up.
        dismiss_resolved_escalation("drive-queue", "no-hash-separator")
        dismiss_resolved_escalation("drive-queue", "#12")

    def test_missing_escalation_does_not_raise(self) -> None:
        dismiss_resolved_escalation("drive-queue", "acme/widgets#404")
