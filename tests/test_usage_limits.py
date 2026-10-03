"""Tests for coord/usage_limits.py — the Max-plan 5h/weekly usage probe and
dispatch gate (#1466).

Covers:
- parse_usage_probe_output: the real `.result` prose shape, per-model rows,
  and defensive handling of garbled/unexpected output (never raises).
- probe_plan_limits: subprocess plumbing (resolved binary, non-zero exit,
  timeout, malformed JSON) — all degrade to status="unknown", never raise.
- get_plan_limits: ~60s caching so a dispatch batch doesn't hammer the
  (itself rate-limited) /usage endpoint.
- evaluate_usage_gate: the pure gate decision — off/warn/block/reroute
  modes, threshold comparisons, and "unknown never blocks, warns, or
  reroutes".
- select_reroute_route: the first escalation rung whose provider escapes
  the constrained one (#1649).
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from coord.config import ModelsConfig, UsageGateConfig
from coord.usage_limits import (
    ModelWeekUsage,
    PlanLimits,
    evaluate_usage_gate,
    format_plan_limits,
    get_plan_limits,
    parse_usage_probe_output,
    probe_plan_limits,
    reset_cache,
    select_reroute_route,
)


# ── parse_usage_probe_output ─────────────────────────────────────────────────


REAL_RESULT = (
    "Current session: 57% used · resets Jul 27, 1:30am (America/Chicago)\n"
    "Current week (all models): 29% used · resets Aug 1, 12pm (America/Chicago)\n"
    "Current week (Fable): 0% used\n"
)


class TestParseUsageProbeOutput:
    def test_parses_real_shape(self) -> None:
        limits = parse_usage_probe_output(REAL_RESULT)
        assert limits.status == "ok"
        assert limits.session_pct == pytest.approx(57.0)
        assert limits.session_resets_at == "Jul 27, 1:30am (America/Chicago)"
        assert limits.week_pct == pytest.approx(29.0)
        assert limits.week_resets_at == "Aug 1, 12pm (America/Chicago)"
        assert limits.week_by_model == (
            ModelWeekUsage(label="Fable", used_pct=0.0, resets_at=None),
        )
        assert limits.ok is True

    def test_session_only(self) -> None:
        limits = parse_usage_probe_output(
            "Current session: 12% used · resets 3:00pm (UTC)\n"
        )
        assert limits.status == "ok"
        assert limits.session_pct == pytest.approx(12.0)
        assert limits.week_pct is None

    def test_week_only(self) -> None:
        limits = parse_usage_probe_output(
            "Current week (all models): 88% used · resets tomorrow\n"
        )
        assert limits.status == "ok"
        assert limits.session_pct is None
        assert limits.week_pct == pytest.approx(88.0)

    def test_no_reset_time_is_optional(self) -> None:
        limits = parse_usage_probe_output("Current session: 5% used\n")
        assert limits.status == "ok"
        assert limits.session_pct == pytest.approx(5.0)
        assert limits.session_resets_at is None

    def test_empty_string_is_unknown(self) -> None:
        limits = parse_usage_probe_output("")
        assert limits.status == "unknown"
        assert limits.ok is False
        assert limits.error

    def test_garbled_output_is_unknown_not_raised(self) -> None:
        """#1466: `.result` is prose, not a stable contract — a reworded or
        entirely different CLI response must degrade to unknown, never
        raise."""
        limits = parse_usage_probe_output(
            "Your plan has been upgraded. Please restart the CLI to continue."
        )
        assert limits.status == "unknown"
        assert limits.session_pct is None
        assert limits.week_pct is None

    def test_non_string_input_is_unknown(self) -> None:
        limits = parse_usage_probe_output(None)  # type: ignore[arg-type]
        assert limits.status == "unknown"

    def test_all_models_bucket_not_double_counted_as_per_model_row(self) -> None:
        limits = parse_usage_probe_output(REAL_RESULT)
        labels = [m.label for m in limits.week_by_model]
        assert "all models" not in [lbl.lower() for lbl in labels]
        assert "Fable" in labels


# ── probe_plan_limits (subprocess plumbing) ──────────────────────────────────


class TestProbePlanLimits:
    def test_success_parses_result_field(self) -> None:
        fake = MagicMock(
            returncode=0,
            stdout='{"result": "Current session: 40% used · resets 5pm (UTC)\\n", "total_cost_usd": 0, "num_turns": 0}',
            stderr="",
        )
        with patch("subprocess.run", return_value=fake):
            limits = probe_plan_limits()
        assert limits.status == "ok"
        assert limits.session_pct == pytest.approx(40.0)

    def test_uses_resolved_binary_and_usage_slash_command(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_BIN", "/home/svc/.local/bin/claude")
        fake = MagicMock(returncode=0, stdout='{"result": "Current session: 1% used\\n"}', stderr="")
        with patch("subprocess.run", return_value=fake) as mock_run:
            probe_plan_limits()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "/home/svc/.local/bin/claude"
        assert "/usage" in cmd
        assert "--output-format" in cmd

    def test_nonzero_exit_is_unknown(self) -> None:
        fake = MagicMock(returncode=1, stdout="", stderr="not logged in")
        with patch("subprocess.run", return_value=fake):
            limits = probe_plan_limits()
        assert limits.status == "unknown"
        assert "not logged in" in (limits.error or "")

    def test_malformed_json_is_unknown(self) -> None:
        fake = MagicMock(returncode=0, stdout="not json", stderr="")
        with patch("subprocess.run", return_value=fake):
            limits = probe_plan_limits()
        assert limits.status == "unknown"

    def test_missing_result_field_is_unknown(self) -> None:
        fake = MagicMock(returncode=0, stdout='{"total_cost_usd": 0}', stderr="")
        with patch("subprocess.run", return_value=fake):
            limits = probe_plan_limits()
        assert limits.status == "unknown"

    def test_timeout_is_unknown_not_raised(self) -> None:
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=15.0),
        ):
            limits = probe_plan_limits()
        assert limits.status == "unknown"

    def test_missing_binary_is_unknown_not_raised(self) -> None:
        """API key / no `claude` install / etc — never raises up to the
        dispatch gate."""
        with patch("subprocess.run", side_effect=FileNotFoundError("no such file")):
            limits = probe_plan_limits()
        assert limits.status == "unknown"

    def test_non_oauth_output_degrades_to_unknown(self) -> None:
        """Under an API key/Bedrock/Vertex the plan windows don't apply —
        `/usage` returns something that isn't the session/week bars, which
        the parser correctly can't recognise."""
        fake = MagicMock(
            returncode=0,
            stdout='{"result": "Usage tracking is not available for API key authentication."}',
            stderr="",
        )
        with patch("subprocess.run", return_value=fake):
            limits = probe_plan_limits()
        assert limits.status == "unknown"


# ── get_plan_limits (caching) ────────────────────────────────────────────────


class TestGetPlanLimitsCaching:
    def setup_method(self) -> None:
        reset_cache()

    def teardown_method(self) -> None:
        reset_cache()

    def test_second_call_within_ttl_does_not_reprobe(self) -> None:
        fake = MagicMock(returncode=0, stdout='{"result": "Current session: 1% used\\n"}', stderr="")
        with patch("subprocess.run", return_value=fake) as mock_run:
            get_plan_limits()
            get_plan_limits()
        assert mock_run.call_count == 1

    def test_force_bypasses_cache(self) -> None:
        fake = MagicMock(returncode=0, stdout='{"result": "Current session: 1% used\\n"}', stderr="")
        with patch("subprocess.run", return_value=fake) as mock_run:
            get_plan_limits()
            get_plan_limits(force=True)
        assert mock_run.call_count == 2


# ── evaluate_usage_gate (pure) ───────────────────────────────────────────────


class TestEvaluateUsageGate:
    def test_disabled_mode_always_proceeds(self) -> None:
        cfg = UsageGateConfig(mode="disabled", session_threshold_pct=1.0, week_threshold_pct=1.0)
        limits = PlanLimits(status="ok", session_pct=99.0, week_pct=99.0)
        result = evaluate_usage_gate(limits, cfg)
        assert result.action == "proceed"

    def test_unknown_probe_always_proceeds_even_in_block_mode(self) -> None:
        cfg = UsageGateConfig(mode="block")
        limits = PlanLimits(status="unknown")
        result = evaluate_usage_gate(limits, cfg)
        assert result.action == "proceed"

    def test_below_threshold_proceeds(self) -> None:
        cfg = UsageGateConfig(mode="warn", session_threshold_pct=85.0, week_threshold_pct=90.0)
        limits = PlanLimits(status="ok", session_pct=50.0, week_pct=50.0)
        result = evaluate_usage_gate(limits, cfg)
        assert result.action == "proceed"

    def test_session_at_threshold_warns_by_default(self) -> None:
        cfg = UsageGateConfig(mode="warn", session_threshold_pct=85.0, week_threshold_pct=90.0)
        limits = PlanLimits(status="ok", session_pct=85.0, session_resets_at="8pm (UTC)", week_pct=10.0)
        result = evaluate_usage_gate(limits, cfg)
        assert result.action == "warn"
        assert "session" in result.message
        assert "85" in result.message
        assert "8pm (UTC)" in result.message

    def test_week_over_threshold_warns(self) -> None:
        cfg = UsageGateConfig(mode="warn", session_threshold_pct=85.0, week_threshold_pct=90.0)
        limits = PlanLimits(status="ok", session_pct=10.0, week_pct=95.0, week_resets_at="Aug 1")
        result = evaluate_usage_gate(limits, cfg)
        assert result.action == "warn"
        assert "week" in result.message
        assert "Aug 1" in result.message

    def test_block_mode_refuses_above_threshold(self) -> None:
        cfg = UsageGateConfig(mode="block", session_threshold_pct=85.0, week_threshold_pct=90.0)
        limits = PlanLimits(status="ok", session_pct=90.0)
        result = evaluate_usage_gate(limits, cfg)
        assert result.action == "block"
        assert result.blocks is True

    def test_warn_mode_never_blocks(self) -> None:
        cfg = UsageGateConfig(mode="warn", session_threshold_pct=85.0, week_threshold_pct=90.0)
        limits = PlanLimits(status="ok", session_pct=100.0, week_pct=100.0)
        result = evaluate_usage_gate(limits, cfg)
        assert result.action == "warn"
        assert result.blocks is False


# ── select_reroute_route (pure) ─────────────────────────────────────────────


class TestSelectRerouteRoute:
    def test_every_bare_rung_is_implicitly_the_constrained_provider(self) -> None:
        """#1649: until #55's cross-provider routes land, every bare rung
        is implicitly on "claude" — there is nothing to reroute to yet,
        and this must say so rather than guess."""
        assert select_reroute_route(["haiku", "sonnet", "opus"], "claude") is None

    def test_first_non_constrained_rung_wins(self) -> None:
        route = select_reroute_route(
            ["haiku", "sonnet", "opencode/glm-5.2", "opus"], "claude",
        )
        assert route == "opencode/glm-5.2"

    def test_a_non_constrained_rung_before_a_constrained_one_still_wins(self) -> None:
        route = select_reroute_route(["opencode/glm-5.2", "opus"], "claude")
        assert route == "opencode/glm-5.2"

    def test_empty_ladder_has_nothing_to_reroute_to(self) -> None:
        assert select_reroute_route([], "claude") is None


# ── evaluate_usage_gate (mode="reroute") ─────────────────────────────────────


class TestEvaluateUsageGateReroute:
    def test_reroute_picks_first_non_constrained_rung(self) -> None:
        cfg = UsageGateConfig(mode="reroute", session_threshold_pct=85.0)
        models = ModelsConfig(escalation=["haiku", "sonnet", "opencode/glm-5.2"])
        limits = PlanLimits(status="ok", session_pct=92.0, session_resets_at="8pm (UTC)")
        result = evaluate_usage_gate(
            limits, cfg,
            models_cfg=models, effective_provider_name="claude", assignment_type="work",
        )
        assert result.action == "reroute"
        assert result.route == "opencode/glm-5.2"
        # Loud, once per dispatch: names the trigger, the old and new
        # route, and the reset time (#1649).
        assert "session" in result.message
        assert "92" in result.message
        assert "8pm (UTC)" in result.message
        assert "claude" in result.message
        assert "opencode/glm-5.2" in result.message

    def test_falls_back_to_warn_when_no_rung_escapes_the_constrained_provider(
        self,
    ) -> None:
        """Regression guard (#1649): the default `models.escalation` is
        entirely bare (implicitly-claude) aliases — this must never be
        mistaken for "nothing to do" and silently dispatch anyway."""
        cfg = UsageGateConfig(
            mode="reroute", session_threshold_pct=85.0, reroute_fallback="warn",
        )
        models = ModelsConfig()  # default escalation: haiku/sonnet/opus, all bare
        limits = PlanLimits(status="ok", session_pct=92.0)
        result = evaluate_usage_gate(
            limits, cfg,
            models_cfg=models, effective_provider_name="claude", assignment_type="work",
        )
        assert result.action == "warn"
        assert result.route is None

    def test_falls_back_to_block_when_configured_and_no_rung_escapes(self) -> None:
        cfg = UsageGateConfig(
            mode="reroute", session_threshold_pct=85.0, reroute_fallback="block",
        )
        models = ModelsConfig()
        limits = PlanLimits(status="ok", session_pct=92.0)
        result = evaluate_usage_gate(
            limits, cfg,
            models_cfg=models, effective_provider_name="claude", assignment_type="work",
        )
        assert result.action == "block"
        assert result.blocks is True
        assert result.route is None

    def test_no_models_cfg_behaves_like_an_empty_ladder(self) -> None:
        cfg = UsageGateConfig(mode="reroute", session_threshold_pct=85.0)
        limits = PlanLimits(status="ok", session_pct=92.0)
        result = evaluate_usage_gate(limits, cfg, effective_provider_name="claude")
        assert result.action == "warn"
        assert result.route is None

    def test_pinned_stage_is_never_rerouted(self) -> None:
        """#1650 crossing: a type pinned via `models.pinned` (e.g.
        "review") must warn/block per config instead of degrading, even
        though its own pinned route stays on the constrained provider and
        an escape rung genuinely exists on the ladder."""
        cfg = UsageGateConfig(
            mode="reroute", session_threshold_pct=85.0, reroute_fallback="block",
        )
        models = ModelsConfig(
            pinned={"review": "opus"},
            escalation=["haiku", "sonnet", "opencode/glm-5.2"],
        )
        limits = PlanLimits(status="ok", session_pct=92.0)
        result = evaluate_usage_gate(
            limits, cfg,
            models_cfg=models, effective_provider_name="claude", assignment_type="review",
        )
        assert result.action == "block"
        assert result.route is None
        assert "pinned" in result.message

    def test_unpinned_type_on_the_same_ladder_still_reroutes(self) -> None:
        """Same config as the pinned-stage test above, but for an unpinned
        type — proves the exemption is keyed to the type, not some global
        "reroute is broken" state."""
        cfg = UsageGateConfig(mode="reroute", session_threshold_pct=85.0)
        models = ModelsConfig(
            pinned={"review": "opus"},
            escalation=["haiku", "sonnet", "opencode/glm-5.2"],
        )
        limits = PlanLimits(status="ok", session_pct=92.0)
        result = evaluate_usage_gate(
            limits, cfg,
            models_cfg=models, effective_provider_name="claude", assignment_type="work",
        )
        assert result.action == "reroute"
        assert result.route == "opencode/glm-5.2"

    def test_unknown_probe_never_reroutes_warns_or_blocks(self) -> None:
        """Regression guard on the #1466 safety property, inherited
        exactly for reroute mode (#1649) — an unparseable/failed probe must
        never be mistaken for "at limit"."""
        cfg = UsageGateConfig(mode="reroute", session_threshold_pct=1.0)
        models = ModelsConfig(escalation=["haiku", "opencode/glm-5.2"])
        limits = PlanLimits(status="unknown", error="probe timed out")
        result = evaluate_usage_gate(
            limits, cfg,
            models_cfg=models, effective_provider_name="claude", assignment_type="work",
        )
        assert result.action == "proceed"
        assert result.route is None

    def test_disabled_warn_block_modes_are_byte_identical_to_before(self) -> None:
        """Regression guard: calling evaluate_usage_gate the OLD way (no
        new kwargs) for every pre-#1649 mode must produce the exact same
        result it always did."""
        limits = PlanLimits(status="ok", session_pct=92.0, session_resets_at="8pm (UTC)")
        for mode in ("disabled", "warn", "block"):
            cfg = UsageGateConfig(mode=mode, session_threshold_pct=85.0)
            old_style = evaluate_usage_gate(limits, cfg)
            new_style = evaluate_usage_gate(
                limits, cfg,
                models_cfg=ModelsConfig(), effective_provider_name="claude",
                assignment_type="work",
            )
            assert old_style == new_style


# ── format_plan_limits ───────────────────────────────────────────────────────


class TestFormatPlanLimits:
    def test_unknown_status(self) -> None:
        out = format_plan_limits(PlanLimits(status="unknown", error="boom"))
        assert "unknown" in out
        assert "boom" in out

    def test_ok_status_includes_percentages_and_resets(self) -> None:
        limits = parse_usage_probe_output(REAL_RESULT)
        out = format_plan_limits(limits)
        assert "57" in out
        assert "29" in out
        assert "Fable" in out
