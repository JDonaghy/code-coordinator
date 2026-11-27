"""Black-box coverage for #4126's model-chosen investigator decision.

Drives the real seams: :func:`decide_with_model_investigator` (the one
entry point `_investigate` calls when the opt-in is armed),
:func:`_parse_model_response` (the gate that must be able to fail on a
malformed model reply), :func:`_model_investigator_cap_reason` (the
per-row/per-repo cap, read off the real `coord.audit` log), and
:func:`_dispatch_model_eligible_action` (the thin arg-shape layer over the
SAME `dispatch_investigator_action` wrapper a human `doctor-act`
invocation uses).
"""

from __future__ import annotations

import pytest

import coord.board_doctor_policy as board_doctor_policy
import coord.commands._common as common
from coord import queue_diagnose, state
from coord.board_doctor import StuckRow
from coord.board_doctor_permissions import INVESTIGATOR_ACTIONS
from coord.board_doctor_policy import (
    MODEL_DECISION_CAP_PER_REPO_DAILY,
    MODEL_DECISION_CAP_PER_ROW_24H,
    MODEL_DECISION_CAPPED_EVENT_TYPE,
    MODEL_DECISION_EVENT_TYPE,
    MODEL_ELIGIBLE_ACTIONS,
    MODEL_INVESTIGATOR_ENV_VAR,
    ModelInvestigatorDecision,
    _dispatch_model_eligible_action,
    _investigate,
    _model_investigator_cap_reason,
    _parse_model_response,
    decide_with_model_investigator,
    score_model_investigator_decisions,
)
from coord.config import Config, Repo
from coord.models import Board


def _row(cause: str, queue: str = "drive-queue") -> StuckRow:
    return StuckRow(
        queue=queue,
        key="acme/widgets#40",
        state="stuck",
        cause=cause,
        confidence=queue_diagnose.CONFIDENCE_MEDIUM,
        evidence=(f"cause={cause}",),
        fresh=True,
    )


def _config() -> Config:
    return Config(repos=[Repo(name="acme/widgets", github="acme/widgets")], machines=[])


class TestModelEligibleActions:
    def test_every_eligible_action_is_a_real_investigator_action(self) -> None:
        # #4126's own ask: the model may only choose from the SAME closed
        # #3881 vocabulary a human doctor-act invocation uses, never a
        # name that wrapper would not recognize.
        assert MODEL_ELIGIBLE_ACTIONS <= INVESTIGATOR_ACTIONS

    def test_high_stakes_actions_that_mutate_board_state_are_eligible(self) -> None:
        assert {"mark_done", "merge"} <= MODEL_ELIGIBLE_ACTIONS


class TestParseModelResponse:
    def test_a_well_formed_reply_parses(self) -> None:
        action, confidence, reasoning = _parse_model_response(
            '{"action": "requeue", "confidence": 0.92, "reasoning": "clear"}'
        )
        assert (action, confidence, reasoning) == ("requeue", 0.92, "clear")

    def test_code_fenced_json_still_parses(self) -> None:
        action, confidence, _ = _parse_model_response(
            '```json\n{"action": "mark_done", "confidence": 0.8, "reasoning": "x"}\n```'
        )
        assert (action, confidence) == ("mark_done", 0.8)

    def test_the_none_sentinel_means_no_action(self) -> None:
        action, confidence, reasoning = _parse_model_response(
            '{"action": "none", "confidence": 0.2, "reasoning": "unclear"}'
        )
        assert action is None
        assert confidence == 0.2
        assert reasoning == "unclear"

    @pytest.mark.parametrize(
        "text",
        [
            "not json at all",
            '{"action": "force_merge", "confidence": 0.9, "reasoning": "x"}',
            '{"action": "requeue", "confidence": "high", "reasoning": "x"}',
            '{"action": "requeue", "confidence": 1.5, "reasoning": "x"}',
            '["not", "an", "object"]',
            '{"confidence": 0.9, "reasoning": "x"}',
        ],
    )
    def test_every_malformed_shape_refuses_rather_than_guesses(self, text: str) -> None:
        # #2096: the gate must be able to fail. A response this cannot
        # trust must never resolve to a confident, actionable decision.
        action, confidence, _ = _parse_model_response(text)
        assert action is None
        assert confidence == 0.0


class TestModelInvestigatorCap:
    def test_not_capped_with_no_prior_decisions(self) -> None:
        assert _model_investigator_cap_reason("acme/widgets", 40) is None

    def test_capped_once_the_per_row_window_is_full(self) -> None:
        from coord.audit import record_audit

        for _ in range(MODEL_DECISION_CAP_PER_ROW_24H):
            record_audit(
                tier="business",
                category="board_doctor_investigator",
                event_type=MODEL_DECISION_EVENT_TYPE,
                actor="board_doctor_investigator_model",
                summary="decision",
                repo="acme/widgets",
                issue=40,
            )
        reason = _model_investigator_cap_reason("acme/widgets", 40)
        assert reason is not None
        assert "24h" in reason

    def test_a_different_row_in_the_same_repo_is_unaffected_by_the_row_cap(self) -> None:
        from coord.audit import record_audit

        for _ in range(MODEL_DECISION_CAP_PER_ROW_24H):
            record_audit(
                tier="business",
                category="board_doctor_investigator",
                event_type=MODEL_DECISION_EVENT_TYPE,
                actor="board_doctor_investigator_model",
                summary="decision",
                repo="acme/widgets",
                issue=40,
            )
        # #41 has not hit its OWN row cap, but the repo-daily cap below
        # the row cap's own count fires instead once the fleet total for
        # the repo reaches MODEL_DECISION_CAP_PER_REPO_DAILY.
        reason = _model_investigator_cap_reason("acme/widgets", 41)
        if MODEL_DECISION_CAP_PER_ROW_24H < MODEL_DECISION_CAP_PER_REPO_DAILY:
            assert reason is None

    def test_capped_once_the_per_repo_daily_window_is_full(self) -> None:
        from coord.audit import record_audit

        for i in range(MODEL_DECISION_CAP_PER_REPO_DAILY):
            record_audit(
                tier="business",
                category="board_doctor_investigator",
                event_type=MODEL_DECISION_EVENT_TYPE,
                actor="board_doctor_investigator_model",
                summary="decision",
                repo="acme/widgets",
                issue=100 + i,
            )
        reason = _model_investigator_cap_reason("acme/widgets", 999)
        assert reason is not None
        assert "repo-wide" in reason

    def test_a_capped_round_does_not_ratchet_its_own_cap(self, monkeypatch) -> None:
        # Regression for the review finding: a row that legitimately spends
        # its per-24h quota of REAL decisions must stay capped for 24h, not
        # forever — the capped round itself must never count toward either
        # cap. Drives `decide_with_model_investigator` through the real
        # (unmocked) cap check, twice, rather than asserting on
        # `_model_investigator_cap_reason` directly.
        from coord.audit import query_audit_log

        def fake_invoke(system, user, *, model, timeout=60.0):
            return '{"action": "none", "confidence": 0.9, "reasoning": "clear"}'

        row = _row(queue_diagnose.CAUSE_GATE_BLOCKED)
        board, config = Board(active=[]), _config()
        for _ in range(MODEL_DECISION_CAP_PER_ROW_24H):
            decision = decide_with_model_investigator(
                row, "acme/widgets", 40, board, config, invoke_model=fake_invoke,
            )
            assert decision.model_used  # a real model call happened

        # The row is now capped.
        assert _model_investigator_cap_reason("acme/widgets", 40) is not None

        # A capped round (and a second one, simulating a later hourly
        # sweep) must not itself get counted as a "real" decision.
        for _ in range(2):
            capped_decision = decide_with_model_investigator(
                row, "acme/widgets", 40, board, config, invoke_model=fake_invoke,
            )
            assert capped_decision.acted is False
            assert capped_decision.model_used == ""

        real_decisions = query_audit_log(
            event_type=MODEL_DECISION_EVENT_TYPE, repo="acme/widgets", issue=40,
        )["entries"]
        assert len(real_decisions) == MODEL_DECISION_CAP_PER_ROW_24H

        capped_rows = query_audit_log(
            event_type=MODEL_DECISION_CAPPED_EVENT_TYPE, repo="acme/widgets", issue=40,
        )["entries"]
        assert len(capped_rows) == 2

        # The cap reason is still reported off the real-decision count only.
        reason = _model_investigator_cap_reason("acme/widgets", 40)
        assert reason is not None
        assert str(MODEL_DECISION_CAP_PER_ROW_24H) in reason


class TestLivePrSnapshot:
    """Regression coverage: #4126's evidence bundle must read actual PR
    lifecycle state (open/merged/closed), never the issue-only
    open/closed vocabulary — and must say so distinctly when the live
    read itself fails rather than silently rendering as "nothing wrong"."""

    @staticmethod
    def _queued_merge(**overrides):
        from coord.merge_queue import QueuedMerge

        fields = {
            "assignment_id": "asn-1",
            "repo_name": "acme/widgets",
            "repo_github": "acme/widgets",
            "branch": "issue-40-fix",
            "target_branch": "main",
            "issue_number": 40,
            "issue_title": "x",
            "pr_number": 7,
        }
        fields.update(overrides)
        return QueuedMerge(**fields)

    def test_reports_live_pr_state_not_issue_state(self, monkeypatch) -> None:
        from coord import ci_store as ci_store_mod
        from coord import github_ops
        from coord import merge_queue

        monkeypatch.setattr(merge_queue, "load_queue", lambda: [self._queued_merge()])
        monkeypatch.setattr(
            github_ops, "get_pr_state_for_branch", lambda repo, branch: "MERGED",
        )
        monkeypatch.setattr(
            ci_store_mod, "build_ci_store", lambda *a, **k: ci_store_mod.NoOpCi(),
        )

        snapshot = board_doctor_policy._live_pr_snapshot("acme/widgets", 40, _config())
        assert snapshot is not None
        # #2096/review: `get_issue_state` can only ever answer "open" or
        # "closed" for an issue — never "merged", the one state the
        # `mark_done` verb this evidence feeds is defined around. The real
        # `get_pr_state_for_branch` read must come through unmodified.
        assert snapshot["pr_state"] == "MERGED"

    def test_a_failed_live_read_is_unknown_not_none_or_open(self, monkeypatch) -> None:
        from coord import ci_store as ci_store_mod
        from coord import github_ops
        from coord import merge_queue

        monkeypatch.setattr(merge_queue, "load_queue", lambda: [self._queued_merge()])
        monkeypatch.setattr(
            github_ops, "get_pr_state_for_branch", lambda repo, branch: None,
        )
        monkeypatch.setattr(
            ci_store_mod, "build_ci_store", lambda *a, **k: ci_store_mod.NoOpCi(),
        )

        snapshot = board_doctor_policy._live_pr_snapshot("acme/widgets", 40, _config())
        assert snapshot is not None
        assert snapshot["pr_state"] == "unknown"

    def test_an_unavailable_ci_store_reports_unavailable_not_an_empty_all_clear(
        self, monkeypatch,
    ) -> None:
        from coord import ci_store as ci_store_mod
        from coord import github_ops
        from coord import merge_queue

        monkeypatch.setattr(merge_queue, "load_queue", lambda: [self._queued_merge()])
        monkeypatch.setattr(
            github_ops, "get_pr_state_for_branch", lambda repo, branch: "OPEN",
        )
        monkeypatch.setattr(
            ci_store_mod, "build_ci_store", lambda *a, **k: ci_store_mod.NoOpCi(),
        )

        snapshot = board_doctor_policy._live_pr_snapshot("acme/widgets", 40, _config())
        assert snapshot is not None
        # NoOpCi.is_available is False — a blind read, never "all clear".
        assert snapshot["failing_check_names"] == "unavailable"
        assert snapshot["pending_check_names"] == "unavailable"

    def test_rendered_evidence_uses_the_pr_state_field_not_issue_state(self) -> None:
        row = _row(queue_diagnose.CAUSE_GATE_BLOCKED)
        evidence = board_doctor_policy._render_model_evidence(
            row, "acme/widgets", 40, stuck_hours=1.0,
            pr_snapshot={
                "pr_number": 7, "pr_state": "MERGED",
                "failing_check_names": [], "pending_check_names": [],
            },
        )
        assert "live_pr_state: MERGED" in evidence
        assert "issue_state" not in evidence


class TestDispatchModelEligibleAction:
    @staticmethod
    def _fake_board() -> Board:
        return Board(active=[])

    def test_requeue_dispatches_with_board_only(self, monkeypatch) -> None:
        captured = {}

        def fake(action, *args, **kwargs):
            captured["action"] = action
            captured["args"] = args
            captured["kwargs"] = kwargs
            return {"acted": True, "action": action, "repo": "acme/widgets", "issue": 40, "detail": "ok"}

        monkeypatch.setattr(board_doctor_policy, "dispatch_investigator_action", fake)
        board = self._fake_board()
        result = _dispatch_model_eligible_action(
            "requeue", "acme/widgets", 40, board, _config(), reason="because",
        )
        assert result["acted"] is True
        assert captured["action"] == "requeue"
        assert captured["args"] == ("acme/widgets", 40, board)
        assert captured["kwargs"]["actor"] == "board_doctor_investigator_model"

    def test_mark_done_dispatches_with_board_and_config(self, monkeypatch) -> None:
        captured = {}

        def fake(action, *args, **kwargs):
            captured["args"] = args
            return {"acted": False, "action": action, "repo": "r", "issue": 1, "detail": "no-op"}

        monkeypatch.setattr(board_doctor_policy, "dispatch_investigator_action", fake)
        board, config = self._fake_board(), _config()
        _dispatch_model_eligible_action("mark_done", "acme/widgets", 40, board, config, reason="x")
        assert captured["args"] == ("acme/widgets", 40, board, config)

    @pytest.mark.parametrize("action", ["clear_latch", "merge"])
    def test_clear_latch_and_merge_dispatch_with_gh_ops(self, monkeypatch, action: str) -> None:
        captured = {}

        def fake(act, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return {"acted": True, "action": act, "repo": "r", "issue": 1, "detail": "ok"}

        monkeypatch.setattr(board_doctor_policy, "dispatch_investigator_action", fake)
        board, config = self._fake_board(), _config()
        _dispatch_model_eligible_action(action, "acme/widgets", 40, board, config, reason="x")
        assert len(captured["args"]) == 5  # repo, issue, board, config, gh_ops
        if action == "merge":
            # #3902/#4126: never let the unattended model path fan out
            # into a conflict-fix/ci-fix leg of its own.
            assert captured["kwargs"]["allow_fix_dispatch"] is False


class TestDecideWithModelInvestigator:
    def _decide(self, monkeypatch, *, responses, stuck_hours=None, cap_reason=None, pr_snapshot=None):
        calls: list[tuple[str, str]] = []

        def fake_invoke(system, user, *, model, timeout=60.0):
            calls.append((model, user))
            return responses[len(calls) - 1]

        monkeypatch.setattr(
            board_doctor_policy, "_model_investigator_cap_reason", lambda *a, **k: cap_reason,
        )
        monkeypatch.setattr(
            board_doctor_policy, "_row_stuck_hours", lambda *a, **k: stuck_hours,
        )
        monkeypatch.setattr(
            board_doctor_policy, "_live_pr_snapshot", lambda *a, **k: pr_snapshot,
        )
        row = _row(queue_diagnose.CAUSE_GATE_BLOCKED)
        decision = decide_with_model_investigator(
            row, "acme/widgets", 40, Board(active=[]), _config(), invoke_model=fake_invoke,
        )
        return decision, calls

    def test_capped_decision_never_invokes_the_model(self, monkeypatch) -> None:
        def must_not_be_called(*a, **k):
            raise AssertionError("a capped row must never call the model")

        monkeypatch.setattr(
            board_doctor_policy, "_model_investigator_cap_reason",
            lambda *a, **k: "acme/widgets#40 is capped",
        )
        row = _row(queue_diagnose.CAUSE_GATE_BLOCKED)
        decision = decide_with_model_investigator(
            row, "acme/widgets", 40, Board(active=[]), _config(), invoke_model=must_not_be_called,
        )
        assert decision.acted is False
        assert decision.action is None
        assert "capped" in decision.detail

    def test_a_confident_single_call_never_escalates(self, monkeypatch) -> None:
        decision, calls = self._decide(
            monkeypatch,
            responses=['{"action": "none", "confidence": 0.9, "reasoning": "clear"}'],
            stuck_hours=10.0,
        )
        assert len(calls) == 1
        assert calls[0][0] == "haiku"
        assert decision.escalated is False
        assert decision.action is None
        assert decision.acted is False

    def test_low_confidence_and_long_stuck_time_escalates_to_sonnet(self, monkeypatch) -> None:
        decision, calls = self._decide(
            monkeypatch,
            responses=[
                '{"action": "none", "confidence": 0.3, "reasoning": "unsure"}',
                '{"action": "requeue", "confidence": 0.95, "reasoning": "now clear"}',
            ],
            stuck_hours=12.0,
        )
        assert [m for m, _ in calls] == ["haiku", "sonnet"]
        assert decision.escalated is True
        assert decision.model_used == "sonnet"
        assert decision.confidence == 0.95

    def test_low_confidence_but_not_stuck_long_enough_does_not_escalate(self, monkeypatch) -> None:
        decision, calls = self._decide(
            monkeypatch,
            responses=['{"action": "none", "confidence": 0.3, "reasoning": "unsure"}'],
            stuck_hours=2.0,
        )
        assert len(calls) == 1
        assert decision.escalated is False

    def test_low_confidence_with_unknown_stuck_time_does_not_escalate(self, monkeypatch) -> None:
        decision, calls = self._decide(
            monkeypatch,
            responses=['{"action": "none", "confidence": 0.3, "reasoning": "unsure"}'],
            stuck_hours=None,
        )
        assert len(calls) == 1
        assert decision.escalated is False

    def test_high_stakes_action_below_confidence_floor_is_not_acted(self, monkeypatch) -> None:
        decision, _ = self._decide(
            monkeypatch,
            responses=['{"action": "mark_done", "confidence": 0.79, "reasoning": "looks merged"}'],
            stuck_hours=1.0,
        )
        assert decision.action == "mark_done"
        assert decision.acted is False
        assert "0.8" in decision.detail

    def test_high_stakes_action_at_the_confidence_floor_is_dispatched(self, monkeypatch) -> None:
        monkeypatch.setattr(
            board_doctor_policy,
            "_dispatch_model_eligible_action",
            lambda action, *a, **k: {"acted": True, "action": action, "repo": "r", "issue": 1, "detail": "merged"},
        )
        decision, _ = self._decide(
            monkeypatch,
            responses=['{"action": "mark_done", "confidence": 0.8, "reasoning": "looks merged"}'],
            stuck_hours=1.0,
        )
        assert decision.acted is True
        assert decision.detail == "merged"

    def test_low_stakes_action_below_08_still_dispatches(self, monkeypatch) -> None:
        # requeue/clear_latch are not high-stakes — any parsed confidence
        # above 0 is enough to try them, since the underlying primitive
        # re-checks its own precondition live either way.
        monkeypatch.setattr(
            board_doctor_policy,
            "_dispatch_model_eligible_action",
            lambda action, *a, **k: {"acted": True, "action": action, "repo": "r", "issue": 1, "detail": "requeued"},
        )
        decision, _ = self._decide(
            monkeypatch,
            responses=['{"action": "requeue", "confidence": 0.4, "reasoning": "blocker cleared"}'],
            stuck_hours=1.0,
        )
        assert decision.acted is True

    def test_a_dispatch_exception_is_a_refusal_not_a_crash(self, monkeypatch) -> None:
        def boom(action, *a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(board_doctor_policy, "_dispatch_model_eligible_action", boom)
        decision, _ = self._decide(
            monkeypatch,
            responses=['{"action": "requeue", "confidence": 0.9, "reasoning": "x"}'],
            stuck_hours=1.0,
        )
        assert decision.acted is False
        assert "boom" in decision.detail

    def test_every_decision_is_audited(self, monkeypatch) -> None:
        from coord.audit import query_audit_log

        self._decide(
            monkeypatch,
            responses=['{"action": "none", "confidence": 0.9, "reasoning": "clear"}'],
            stuck_hours=1.0,
        )
        entries = query_audit_log(
            event_type=MODEL_DECISION_EVENT_TYPE, repo="acme/widgets", issue=40,
        )["entries"]
        assert len(entries) == 1
        assert entries[0]["details"]["model_used"] == "haiku"

    def test_acted_is_the_primitives_own_result_never_the_models_say_so(self, monkeypatch) -> None:
        # #2096: a model that claims high confidence must not get
        # `acted=True` unless the underlying primitive itself confirms it.
        monkeypatch.setattr(
            board_doctor_policy,
            "_dispatch_model_eligible_action",
            lambda action, *a, **k: {
                "acted": False, "action": action, "repo": "r", "issue": 1,
                "detail": "precondition no longer holds",
            },
        )
        decision, _ = self._decide(
            monkeypatch,
            responses=['{"action": "requeue", "confidence": 0.99, "reasoning": "very sure"}'],
            stuck_hours=1.0,
        )
        assert decision.acted is False
        assert decision.detail == "precondition no longer holds"


class TestWiredIntoInvestigate:
    def test_disabled_by_default_falls_straight_to_the_fixit_leg(self, monkeypatch) -> None:
        monkeypatch.delenv(MODEL_INVESTIGATOR_ENV_VAR, raising=False)

        def must_not_be_called(*a, **k):
            raise AssertionError("the model path must not run when disarmed")

        monkeypatch.setattr(
            board_doctor_policy, "decide_with_model_investigator", must_not_be_called,
        )
        monkeypatch.setattr(common, "resolve_and_load_config", lambda path, **k: object())
        monkeypatch.setattr(board_doctor_policy, "_investigate_tier_enabled", lambda *a, **k: True)
        monkeypatch.setattr(state, "build_board", lambda: object())
        monkeypatch.setattr(
            board_doctor_policy,
            "dispatch_sweep_action",
            lambda *a, **k: {"dispatched": True, "assignment_id": "asn-1", "machine": "mac1", "detail": "ok"},
        )
        row = _row(queue_diagnose.CAUSE_CI_RED)
        assert _investigate(row) is True

    def test_enabled_and_acted_skips_the_fixit_leg_entirely(self, monkeypatch) -> None:
        monkeypatch.setenv(MODEL_INVESTIGATOR_ENV_VAR, "1")
        monkeypatch.setattr(common, "resolve_and_load_config", lambda path, **k: object())
        monkeypatch.setattr(board_doctor_policy, "_investigate_tier_enabled", lambda *a, **k: True)
        monkeypatch.setattr(state, "build_board", lambda: object())
        monkeypatch.setattr(
            board_doctor_policy,
            "decide_with_model_investigator",
            lambda *a, **k: ModelInvestigatorDecision(
                repo_name="acme/widgets", issue_number=40, action="requeue",
                confidence=0.9, reasoning="x", model_used="haiku", escalated=False,
                acted=True, detail="requeued",
            ),
        )

        def must_not_be_called(*a, **k):
            raise AssertionError("a model-acted row must not also dispatch a fix-it leg")

        monkeypatch.setattr(board_doctor_policy, "dispatch_sweep_action", must_not_be_called)
        row = _row(queue_diagnose.CAUSE_CI_RED)
        assert _investigate(row) is True

    def test_enabled_but_declined_still_falls_through_to_the_fixit_leg(self, monkeypatch) -> None:
        monkeypatch.setenv(MODEL_INVESTIGATOR_ENV_VAR, "1")
        monkeypatch.setattr(common, "resolve_and_load_config", lambda path, **k: object())
        monkeypatch.setattr(board_doctor_policy, "_investigate_tier_enabled", lambda *a, **k: True)
        monkeypatch.setattr(state, "build_board", lambda: object())
        monkeypatch.setattr(
            board_doctor_policy,
            "decide_with_model_investigator",
            lambda *a, **k: ModelInvestigatorDecision(
                repo_name="acme/widgets", issue_number=40, action=None,
                confidence=0.2, reasoning="unsure", model_used="haiku", escalated=False,
                acted=False, detail="unsure",
            ),
        )
        dispatched = []
        monkeypatch.setattr(
            board_doctor_policy,
            "dispatch_sweep_action",
            lambda *a, **k: (dispatched.append(1), {
                "dispatched": True, "assignment_id": "asn-1", "machine": "mac1", "detail": "ok",
            })[1],
        )
        row = _row(queue_diagnose.CAUSE_CI_RED)
        assert _investigate(row) is True
        assert dispatched == [1]


class TestScoreModelInvestigatorDecisions:
    _NOW = 1_700_000_000.0

    def _entry(self, *, issue: int, ts: float, acted: bool, action, confidence=0.9, model_used="haiku", escalated=False):
        return {
            "event_type": MODEL_DECISION_EVENT_TYPE,
            "repo": "acme/widgets",
            "issue": issue,
            "ts": ts,
            "details": {
                "acted": acted, "action": action, "confidence": confidence,
                "model_used": model_used, "escalated": escalated,
            },
        }

    def test_non_model_decision_audit_entries_are_ignored(self) -> None:
        entries = [{"event_type": "fixit_leg_dispatched", "repo": "r", "issue": 1, "ts": 0, "details": {}}]
        assert score_model_investigator_decisions(entries, [], now=self._NOW) == []

    def test_a_decision_inside_the_score_window_is_too_recent(self) -> None:
        entry = self._entry(issue=40, ts=self._NOW - 3600, acted=True, action="requeue")
        outcomes = score_model_investigator_decisions([entry], [], now=self._NOW)
        assert outcomes[0].outcome == "too_recent"

    def test_a_declined_decision_is_never_scored_unstuck_or_still_stuck(self) -> None:
        entry = self._entry(issue=40, ts=self._NOW - 25 * 3600, acted=False, action=None)
        outcomes = score_model_investigator_decisions([entry], [], now=self._NOW)
        assert outcomes[0].outcome == "declined"

    def test_an_acted_decision_followed_by_a_terminal_row_is_unstuck(self) -> None:
        old_ts = self._NOW - 25 * 3600
        entry = self._entry(issue=40, ts=old_ts, acted=True, action="requeue")
        rows = [
            {"repo_name": "acme/widgets", "issue_number": 40, "status": "merged", "finished_at": old_ts + 60},
        ]
        outcomes = score_model_investigator_decisions([entry], rows, now=self._NOW)
        assert outcomes[0].outcome == "unstuck"

    def test_an_acted_decision_with_no_later_terminal_row_is_still_stuck(self) -> None:
        old_ts = self._NOW - 25 * 3600
        entry = self._entry(issue=40, ts=old_ts, acted=True, action="requeue")
        outcomes = score_model_investigator_decisions([entry], [], now=self._NOW)
        assert outcomes[0].outcome == "still_stuck"

    def test_a_terminal_row_that_predates_the_decision_does_not_count(self) -> None:
        # A row that was ALREADY merged before the decision fired proves
        # nothing about whether this decision helped.
        old_ts = self._NOW - 25 * 3600
        entry = self._entry(issue=40, ts=old_ts, acted=True, action="mark_done")
        rows = [
            {"repo_name": "acme/widgets", "issue_number": 40, "status": "merged", "finished_at": old_ts - 1000},
        ]
        outcomes = score_model_investigator_decisions([entry], rows, now=self._NOW)
        assert outcomes[0].outcome == "still_stuck"

    def test_a_different_issues_terminal_row_does_not_count(self) -> None:
        old_ts = self._NOW - 25 * 3600
        entry = self._entry(issue=40, ts=old_ts, acted=True, action="requeue")
        rows = [
            {"repo_name": "acme/widgets", "issue_number": 41, "status": "merged", "finished_at": old_ts + 60},
        ]
        outcomes = score_model_investigator_decisions([entry], rows, now=self._NOW)
        assert outcomes[0].outcome == "still_stuck"


class TestFormatModelInvestigatorDecisions:
    def test_empty_outcomes_says_so(self) -> None:
        from coord.commands.stats import format_model_investigator_decisions

        assert "none in window" in format_model_investigator_decisions([])

    def test_renders_totals_and_outcome_counts(self) -> None:
        from coord.board_doctor_policy import ModelDecisionOutcome
        from coord.commands.stats import format_model_investigator_decisions

        outcomes = [
            ModelDecisionOutcome(
                repo_name="acme/widgets", issue_number=40, ts=1.0, action="requeue",
                confidence=0.9, model_used="haiku", escalated=False, acted=True,
                outcome="unstuck",
            ),
            ModelDecisionOutcome(
                repo_name="acme/widgets", issue_number=41, ts=2.0, action=None,
                confidence=0.2, model_used="haiku", escalated=False, acted=False,
                outcome="declined",
            ),
        ]
        rendered = format_model_investigator_decisions(outcomes)
        assert "total=2" in rendered
        assert "acted=1" in rendered
        assert "unstuck=1" in rendered
        assert "declined=1" in rendered
        assert "requeue=1" in rendered
