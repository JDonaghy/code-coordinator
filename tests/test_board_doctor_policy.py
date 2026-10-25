"""Black-box coverage for #2268's board-doctor policy (Phases 2 and 3).

Drives the real, externally-observable seams: :func:`policy_tier_of` against
:data:`coord.board_doctor_policy.ALLOWLIST`, and
:func:`escalation_alert_dispatcher` against the real `drive_escalations`
table via :mod:`coord.state` — the same pattern `tests/test_board_doctor.py`
uses for the harness it plugs into.
"""

from __future__ import annotations

from coord import state
from coord.board_doctor import TIER_ALERT, TIER_AUTO, StuckRow
from coord.board_doctor_policy import (
    ALLOWLIST,
    escalation_alert_dispatcher,
    policy_tier_of,
)


def _row(key: str, cause: str, queue: str = "drive-queue", evidence: tuple = ()) -> StuckRow:
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
        # #2268: no cause is pre-approved for auto-action without Phase 0
        # block-log evidence of it recurring with a known-safe fix.
        assert ALLOWLIST == {}

    def test_every_cause_is_alert_tier_today(self) -> None:
        for cause in ("no_pr", "merge_conflict", "dead_leg", "ci_red", "anything_else"):
            assert policy_tier_of(cause) == TIER_ALERT

    def test_a_future_allowlist_entry_would_be_honored(self) -> None:
        # Pins the seam itself, not a specific entry: if a cause is ever
        # added to ALLOWLIST, policy_tier_of must actually read it rather
        # than hardcoding "always alert".
        ALLOWLIST["probe_only_cause"] = "probe-only-action"
        try:
            assert policy_tier_of("probe_only_cause") == TIER_AUTO
        finally:
            del ALLOWLIST["probe_only_cause"]


class TestEscalationAlertDispatcher:
    def test_drive_queue_row_files_a_real_escalation(self) -> None:
        row = _row("acme/widgets#1762", "merge_conflict")

        escalation_alert_dispatcher(row)

        escalation = state.get_drive_escalation("acme/widgets", 1762)
        assert escalation is not None
        assert escalation["stage"] == "board-doctor:drive-queue"
        assert escalation["proposed_command"] == "coord status"
        assert "merge_conflict" in escalation["reason"]
        assert "acme/widgets#1762" in escalation["reason"]
        assert "cause=merge_conflict" in escalation["gate_readings"]

    def test_merge_queue_row_parses_the_same_repo_hash_issue_key(self) -> None:
        row = _row("acme/widgets#99", "gate_blocked", queue="merge-queue")

        escalation_alert_dispatcher(row)

        escalation = state.get_drive_escalation("acme/widgets", 99)
        assert escalation is not None
        assert escalation["stage"] == "board-doctor:merge-queue"

    def test_renewing_the_same_cause_preserves_created_at(self) -> None:
        row = _row("acme/widgets#5", "ci_red")

        escalation_alert_dispatcher(row)
        first = state.get_drive_escalation("acme/widgets", 5)
        assert first is not None

        escalation_alert_dispatcher(row)
        second = state.get_drive_escalation("acme/widgets", 5)
        assert second is not None
        assert second["created_at"] == first["created_at"]

    def test_unparseable_key_does_not_raise(self) -> None:
        row = _row("no-hash-separator", "ci_red")

        # Must not raise — a malformed key is a dropped alert, not a crash.
        escalation_alert_dispatcher(row)

        assert state.list_drive_escalations() == []
