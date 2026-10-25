"""Black-box coverage for #2268's board-doctor policy (Phases 2 and 3).

Drives the real, externally-observable seams: :func:`policy_tier_of` against
:data:`coord.board_doctor_policy.ALLOWLIST`, and
:func:`escalation_alert_dispatcher` against the real `drive_escalations`
table via :mod:`coord.state` — the same pattern `tests/test_board_doctor.py`
uses for the harness it plugs into.
"""

from __future__ import annotations

import pytest

import coord.board_doctor_policy as board_doctor_policy
import coord.commands._common as common
from coord import queue_diagnose, state
from coord.board_doctor import (
    ALERT_SKIPPED,
    CAUSE_BASELINE_RED,
    CAUSE_CEILING_BLOCKED_MERGEABLE,
    CONVERGENCE_LIMIT,
    TIER_ALERT,
    TIER_AUTO,
    StuckRow,
    sweep,
)
from coord.board_doctor_permissions import SweepPermissionError
from coord.board_doctor_policy import (
    ALLOWLIST,
    AUTO_DISPATCHERS,
    INVESTIGATE_CAUSES,
    _investigate,
    _register_auto_action,
    dismiss_resolved_escalation,
    escalation_alert_dispatcher,
    policy_tier_of,
)

_IN_FLIGHT_DETAIL = (
    "acme/widgets#40 already has a running or pending assignment — a "
    "fix-it leg is already in flight, not dispatching a second one"
)
_UNPINNABLE_DETAIL = (
    "acme/widgets#40 fix-it leg dispatch could not be confirmed started — "
    "no capable machine, an unreachable agent, or a declined dispatch"
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
    def test_allowlist_holds_exactly_the_two_3760_entries(self) -> None:
        # No cause is pre-approved for auto-action without a reviewed,
        # known-safe fix behind it. #3760 (C3) names exactly two: a
        # ceiling-blocked-but-mergeable row gets a fresh fix-round budget,
        # and a row whose PR already merged gets cleared. Every other
        # cause stays alert-tier.
        assert ALLOWLIST == {
            CAUSE_CEILING_BLOCKED_MERGEABLE: "drive_queue_requeue",
            queue_diagnose.CAUSE_PR_MERGED: "drive_queue_remove",
        }

    def test_every_other_cause_is_alert_tier_today(self) -> None:
        # Causes with no safe mechanical fix AND no investigate-tier
        # dispatcher registered (#3769): "nothing actually wrong" states
        # (nothing-blocking, ci-green), a whole-repo condition no single
        # row can fix (baseline-red), and a genuinely unknown string.
        for cause in (
            "no_pr",
            "merge_conflict",
            "dead_leg",
            "ci_red",
            "anything_else",
            queue_diagnose.CAUSE_NOTHING_BLOCKING,
            queue_diagnose.CAUSE_CI_GREEN,
            CAUSE_BASELINE_RED,
        ):
            assert policy_tier_of(cause) == TIER_ALERT

    def test_registered_3760_causes_are_auto_tier(self) -> None:
        assert policy_tier_of(CAUSE_CEILING_BLOCKED_MERGEABLE) == TIER_AUTO
        assert policy_tier_of(queue_diagnose.CAUSE_PR_MERGED) == TIER_AUTO

    def test_investigate_eligible_causes_are_auto_tier_via_fixit_leg(self) -> None:
        # #3769: a cause with no safe mechanical fix still reaches
        # TIER_AUTO once it is routed to the investigate tier — it lands in
        # AUTO_DISPATCHERS (what the cause -> TIER_AUTO check actually
        # reads) but never in ALLOWLIST, which stays reserved for fixes
        # that touch the queue row itself (#3760).
        assert INVESTIGATE_CAUSES == {
            queue_diagnose.CAUSE_PR_CLOSED,
            queue_diagnose.CAUSE_MERGE_CONFLICT,
            queue_diagnose.CAUSE_DEAD_LEG,
            queue_diagnose.CAUSE_NO_PR,
            queue_diagnose.CAUSE_CI_RED,
            queue_diagnose.CAUSE_CI_PENDING,
            queue_diagnose.CAUSE_GATE_BLOCKED,
            queue_diagnose.CAUSE_AGENT_UNREACHABLE,
            queue_diagnose.CAUSE_UNKNOWN,
        }
        for cause in INVESTIGATE_CAUSES:
            assert policy_tier_of(cause) == TIER_AUTO
            assert cause in AUTO_DISPATCHERS
            assert cause not in ALLOWLIST

    def test_a_future_allowlist_entry_would_be_honored(self, monkeypatch) -> None:
        # Pins the seam itself, not a specific entry: if a cause is ever
        # registered for a real safe-tier action, policy_tier_of must
        # actually read the mappings rather than hardcoding "always alert".
        monkeypatch.setitem(ALLOWLIST, "probe_only_cause", "drive_queue_add")
        monkeypatch.setitem(AUTO_DISPATCHERS, "probe_only_cause", lambda row: True)
        assert policy_tier_of("probe_only_cause") == TIER_AUTO

    def test_allowlist_entry_without_a_dispatcher_stays_alert_tier(self, monkeypatch) -> None:
        # An allowlist entry with no real dispatcher behind it must never
        # silently promote to auto-tier — that would suppress the alert
        # while doing nothing.
        monkeypatch.setitem(ALLOWLIST, "probe_only_cause", "drive_queue_add")
        assert "probe_only_cause" not in AUTO_DISPATCHERS
        assert policy_tier_of("probe_only_cause") == TIER_ALERT

    def test_an_allowlist_entry_naming_an_unsafe_action_stays_alert_tier(
        self, monkeypatch
    ) -> None:
        # Guards against a future bug that assigns into ALLOWLIST /
        # AUTO_DISPATCHERS directly, bypassing _register_auto_action: an
        # entry naming an action outside SAFE_TIER_ACTIONS must not promote
        # to auto-tier even though both mappings "agree".
        monkeypatch.setitem(ALLOWLIST, "probe_only_cause", "force_merge")
        monkeypatch.setitem(AUTO_DISPATCHERS, "probe_only_cause", lambda row: True)
        assert policy_tier_of("probe_only_cause") == TIER_ALERT


class TestRegisterAutoAction:
    def test_registers_a_safe_tier_action_in_both_mappings(self, monkeypatch) -> None:
        calls: list[StuckRow] = []
        # Claim both keys through monkeypatch first — they are absent, so
        # its undo DELETES them — then let the real _register_auto_action
        # overwrite both. Same teardown as every neighbouring test, with no
        # hand-rolled finally block.
        monkeypatch.setitem(ALLOWLIST, "probe_only_cause", "unregistered")
        monkeypatch.setitem(AUTO_DISPATCHERS, "probe_only_cause", lambda row: None)

        _register_auto_action("probe_only_cause", "drive_queue_add", calls.append)

        assert ALLOWLIST["probe_only_cause"] == "drive_queue_add"
        assert policy_tier_of("probe_only_cause") == TIER_AUTO

        row = _row("acme/widgets#1", "probe_only_cause")
        AUTO_DISPATCHERS["probe_only_cause"](row)
        assert calls == [row]

    def test_refuses_to_register_an_action_outside_the_safe_tier(self) -> None:
        with pytest.raises(SweepPermissionError, match="force_merge"):
            _register_auto_action("probe_only_cause", "force_merge", lambda row: None)

        assert "probe_only_cause" not in ALLOWLIST
        assert "probe_only_cause" not in AUTO_DISPATCHERS


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

    def test_gate_blocked_merge_queue_row_gets_the_override_command(self) -> None:
        # #3761: on a merge-queue row this cause is only ever minted from
        # the queue's own `human_required` state, so the override is
        # always valid there — surfaced as a one-line, ready-to-run command
        # rather than the generic "coord status" pointer. The sweep itself
        # never runs it.
        row = _row(
            "acme/widgets#21", queue_diagnose.CAUSE_GATE_BLOCKED, queue="merge-queue"
        )

        assert escalation_alert_dispatcher(row) is True

        escalation = state.get_drive_escalation("acme/widgets", 21)
        assert escalation is not None
        assert escalation["proposed_command"] == (
            "coord merge --only acme/widgets#21 "
            '--override-human-required "<reason the override is safe>"'
        )
        assert "--override-human-required" in escalation["reason"]

    def test_gate_blocked_drive_queue_row_points_at_diagnose_not_override(self) -> None:
        # #3761 review: on a drive-queue row this cause means generic
        # `live.gate_ready is False` (unapproved review, a different red
        # required check, or a non-mergeable PR) — NOT necessarily a
        # merge-queue `human_required` state, so `coord merge
        # --override-human-required` would hard-error there. The surfaced
        # command must be the read-only diagnose, not the override.
        row = _row(
            "acme/widgets#21", queue_diagnose.CAUSE_GATE_BLOCKED, queue="drive-queue"
        )

        assert escalation_alert_dispatcher(row) is True

        escalation = state.get_drive_escalation("acme/widgets", 21)
        assert escalation is not None
        assert escalation["proposed_command"] == "coord drive-queue diagnose --json"
        assert "--override-human-required" not in escalation["proposed_command"]

    def test_baseline_red_row_keeps_coord_status_with_an_explanatory_reason(self) -> None:
        # #3761: there is no single per-row command that fixes a red base,
        # so the fallback command stays, but the reason explains why.
        row = _row("acme/widgets#0", CAUSE_BASELINE_RED)

        assert escalation_alert_dispatcher(row) is True

        escalation = state.get_drive_escalation("acme/widgets", 0)
        assert escalation is not None
        assert escalation["proposed_command"] == "coord status"
        assert "merge base is red" in escalation["reason"]

    def test_an_unrelated_cause_keeps_the_generic_coord_status_default(self) -> None:
        row = _row("acme/widgets#22", "ci_red")

        assert escalation_alert_dispatcher(row) is True

        escalation = state.get_drive_escalation("acme/widgets", 22)
        assert escalation is not None
        assert escalation["proposed_command"] == "coord status"


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


class TestInvestigate:
    """#3769's C1 wiring: `_investigate` is the one handler behind every
    entry in :data:`INVESTIGATE_CAUSES`.
    """

    def test_dispatches_a_fixit_leg_when_the_drive_loop_has_given_up(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(common, "resolve_and_load_config", lambda path, **k: object())
        monkeypatch.setattr(state, "build_board", lambda: object())
        monkeypatch.setattr(
            board_doctor_policy,
            "dispatch_sweep_action",
            lambda *a, **k: {
                "dispatched": True,
                "assignment_id": "asn-1",
                "machine": "mac1",
                "detail": "acme/widgets#40 fix-it leg dispatched to mac1 (iteration 1)",
            },
        )
        row = _row("acme/widgets#40", queue_diagnose.CAUSE_CI_RED)

        assert _investigate(row) is True
        # A started leg is not an alert-worthy outcome.
        assert state.list_drive_escalations() == []

    def test_leaves_an_in_flight_row_alone(self, monkeypatch) -> None:
        monkeypatch.setattr(common, "resolve_and_load_config", lambda path, **k: object())
        monkeypatch.setattr(state, "build_board", lambda: object())
        monkeypatch.setattr(
            board_doctor_policy,
            "dispatch_sweep_action",
            lambda *a, **k: {
                "dispatched": False,
                "assignment_id": None,
                "machine": None,
                "detail": _IN_FLIGHT_DETAIL,
            },
        )
        row = _row("acme/widgets#40", queue_diagnose.CAUSE_CI_RED)

        assert _investigate(row) is False
        # The drive loop (or a prior fix-it leg) is already on this row —
        # left alone, no human-visible alert filed.
        assert state.list_drive_escalations() == []

    def test_falls_back_to_the_alert_tier_when_no_machine_can_be_pinned(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(common, "resolve_and_load_config", lambda path, **k: object())
        monkeypatch.setattr(state, "build_board", lambda: object())
        monkeypatch.setattr(
            board_doctor_policy,
            "dispatch_sweep_action",
            lambda *a, **k: {
                "dispatched": False,
                "assignment_id": None,
                "machine": None,
                "detail": _UNPINNABLE_DETAIL,
            },
        )
        row = _row("acme/widgets#40", queue_diagnose.CAUSE_CI_RED)

        assert _investigate(row) is False
        escalation = state.get_drive_escalation("acme/widgets", 40)
        assert escalation is not None
        assert escalation["stage"] == "board-doctor:drive-queue"

    def test_falls_back_to_the_alert_tier_when_config_cannot_be_resolved(
        self, monkeypatch
    ) -> None:
        def boom(path, **kwargs):
            raise common.ConfigError("no config file found")

        monkeypatch.setattr(common, "resolve_and_load_config", boom)
        row = _row("acme/widgets#40", queue_diagnose.CAUSE_CI_RED)

        assert _investigate(row) is False
        escalation = state.get_drive_escalation("acme/widgets", 40)
        assert escalation is not None

    def test_unparseable_key_does_not_raise(self) -> None:
        row = _row("no-hash-separator", queue_diagnose.CAUSE_CI_RED)

        assert _investigate(row) is False
        assert state.list_drive_escalations() == []


class TestInvestigateConvergence:
    def test_same_row_escalates_once_after_the_convergence_limit(
        self, monkeypatch
    ) -> None:
        # #3769's last acceptance criterion: this is `coord.board_doctor
        # .sweep`'s existing #3758 convergence contract (untouched by this
        # issue) exercised against the real `policy_tier_of` /
        # `AUTO_DISPATCHERS` / `escalation_alert_dispatcher` this module
        # wires in, not a new mechanism — the same row left alone every
        # sweep (drive loop still on it, so `_investigate` never alerts on
        # its own) nonetheless escalates exactly once once the sweep count
        # reaches CONVERGENCE_LIMIT, and stops being handed to
        # `_investigate` at all after that.
        monkeypatch.setattr(common, "resolve_and_load_config", lambda path, **k: object())
        monkeypatch.setattr(state, "build_board", lambda: object())

        calls: list[int] = []

        def fake_dispatch(*args, **kwargs):
            calls.append(1)
            return {
                "dispatched": False,
                "assignment_id": None,
                "machine": None,
                "detail": _IN_FLIGHT_DETAIL,
            }

        monkeypatch.setattr(board_doctor_policy, "dispatch_sweep_action", fake_dispatch)

        key = "acme/widgets#40"
        cause = queue_diagnose.CAUSE_CI_RED
        for _ in range(5):
            sweep(
                [_row(key, cause)],
                tier_of=policy_tier_of,
                auto_dispatchers=AUTO_DISPATCHERS,
                alert_dispatcher=escalation_alert_dispatcher,
                convergence_limit=CONVERGENCE_LIMIT,
            )

        # Sweeps 1-2 dispatch (left alone, no alert); sweep 3 hits the
        # convergence limit and escalates via the alert tier exactly once,
        # bypassing `_investigate` entirely; sweeps 4-5 neither re-dispatch
        # nor re-alert.
        assert len(calls) == CONVERGENCE_LIMIT - 1
        row_state = state.board_doctor_row_state("drive-queue", key)
        assert row_state["escalated"] is True

        escalation = state.get_drive_escalation("acme/widgets", 40)
        assert escalation is not None
