"""Tests for #4228's budget-paced mode selection:

- ``coord.config._parse_pacing``/``PacingConfig`` — the ``pacing:`` block
  (reset day/time/tz, dead-band widths, concurrency multipliers).
- ``coord.usage_limits.evaluate_pace`` (and its curve helpers
  ``seconds_since_weekly_reset``/``target_week_spend_pct``) — the pure
  pace decision: behind/ahead/on-pace against the target spend curve, and
  the independent near-session-limit override.
- ``coord.drive_queue.pace_adjusted_max_parallel`` — scaling an
  already-resolved concurrency ceiling by the pace decision.
- ``coord drive-queue status`` — surfaces the pace in both text and
  ``--json`` output.
"""

from __future__ import annotations

import json as _json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.config import ConfigError, PacingConfig, load
from coord.drive_queue import pace_adjusted_max_parallel
from coord.usage_limits import (
    PACE_MODE_SINGLE,
    PACE_MODE_TRANCHE_OR_WORKFLOW,
    PACE_MODE_UNCONSTRAINED,
    PlanLimits,
    evaluate_pace,
    format_pace_decision,
    seconds_since_weekly_reset,
    target_week_spend_pct,
)


BASE = """\
repos:
  - name: coord-tui
    github: acme/coord-tui
machines:
  - name: laptop
    host: laptop.tail
    repos: [coord-tui]
"""

# Saturday 09:00 UTC reset, same shape the operator described in the issue.
_SATURDAY_RESET = PacingConfig(enabled=True, reset_weekday=5, reset_time=PacingConfig().reset_time, tz="UTC")


def _ts(year, month, day, hour=0, minute=0, tz="UTC") -> float:
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(tz)).timestamp()


# ── coord.config._parse_pacing / PacingConfig ───────────────────────────────


class TestPacingConfigParsing:
    def test_absent_block_defaults_to_disabled(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(BASE)
        cfg = load(p)
        assert cfg.pacing == PacingConfig()
        assert cfg.pacing.enabled is False

    def test_parses_a_full_block(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            BASE
            + "pacing:\n"
            "  enabled: true\n"
            "  reset_weekday: saturday\n"
            "  reset_time: '09:00'\n"
            "  tz: America/Chicago\n"
            "  behind_gap_pct: 15\n"
            "  ahead_gap_pct: 5\n"
            "  session_near_limit_pct: 75\n"
            "  behind_concurrency_multiplier: 2.0\n"
            "  ahead_concurrency_multiplier: 0.25\n"
        )
        cfg = load(p)
        assert cfg.pacing.enabled is True
        assert cfg.pacing.reset_weekday == 5  # Saturday, Mon=0
        assert cfg.pacing.reset_time.hour == 9
        assert cfg.pacing.reset_time.minute == 0
        assert cfg.pacing.tz == "America/Chicago"
        assert cfg.pacing.behind_gap_pct == 15.0
        assert cfg.pacing.ahead_gap_pct == 5.0
        assert cfg.pacing.session_near_limit_pct == 75.0
        assert cfg.pacing.behind_concurrency_multiplier == 2.0
        assert cfg.pacing.ahead_concurrency_multiplier == 0.25

    def test_enabled_without_tz_is_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(BASE + "pacing:\n  enabled: true\n")
        with pytest.raises(ConfigError, match="pacing.tz is required"):
            load(p)

    def test_bad_reset_weekday_is_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(BASE + "pacing:\n  reset_weekday: funday\n")
        with pytest.raises(ConfigError, match="reset_weekday"):
            load(p)

    def test_bad_tz_is_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(BASE + "pacing:\n  tz: Not/AZone\n")
        with pytest.raises(ConfigError, match="not a known IANA zone name"):
            load(p)

    def test_unknown_key_is_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(BASE + "pacing:\n  bogus: true\n")
        with pytest.raises(ConfigError, match="unknown key"):
            load(p)

    def test_non_mapping_is_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(BASE + "pacing: nope\n")
        with pytest.raises(ConfigError, match="must be a mapping"):
            load(p)


# ── target spend curve ───────────────────────────────────────────────────────


class TestTargetSpendCurve:
    def test_zero_right_at_the_reset_instant(self) -> None:
        now = _ts(2026, 11, 28, 9, 0)  # a Saturday, 09:00 UTC
        assert seconds_since_weekly_reset(
            now, reset_weekday=5, reset_time=_SATURDAY_RESET.reset_time, tz="UTC"
        ) == pytest.approx(0.0, abs=1.0)
        assert target_week_spend_pct(now, _SATURDAY_RESET) == pytest.approx(0.0, abs=0.01)

    def test_one_hundred_percent_almost_a_full_week_later(self) -> None:
        now = _ts(2026, 12, 5, 8, 59)  # next Saturday, one minute before reset
        pct = target_week_spend_pct(now, _SATURDAY_RESET)
        assert 99.0 < pct < 100.0

    def test_midweek_is_roughly_proportional(self) -> None:
        # Monday noon UTC is 2.5 days (51h) after a Saturday 09:00 reset.
        now = _ts(2026, 11, 30, 12, 0)
        pct = target_week_spend_pct(now, _SATURDAY_RESET)
        assert pct == pytest.approx(51.0 / 168.0 * 100.0, abs=0.1)


# ── evaluate_pace ─────────────────────────────────────────────────────────────


class TestEvaluatePace:
    NOW = _ts(2026, 11, 30, 12, 0)  # ~30% through the week on the fixture curve

    def test_disabled_pacing_is_unconstrained(self) -> None:
        limits = PlanLimits(status="ok", session_pct=10.0, week_pct=90.0)
        decision = evaluate_pace(limits, PacingConfig(enabled=False), now=self.NOW)
        assert decision.status == "disabled"
        assert decision.preferred_mode == PACE_MODE_UNCONSTRAINED
        assert decision.concurrency_multiplier == 1.0
        assert decision.allow_tranche_or_workflow is True

    def test_unknown_probe_is_unconstrained(self) -> None:
        decision = evaluate_pace(
            PlanLimits(status="unknown"), _SATURDAY_RESET, now=self.NOW
        )
        assert decision.status == "unknown"
        assert decision.preferred_mode == PACE_MODE_UNCONSTRAINED
        assert decision.allow_tranche_or_workflow is True

    def test_behind_curve_prefers_tranche_or_workflow_and_raises_concurrency(self) -> None:
        limits = PlanLimits(status="ok", session_pct=10.0, week_pct=5.0)
        decision = evaluate_pace(limits, _SATURDAY_RESET, now=self.NOW)
        assert decision.status == "behind"
        assert decision.preferred_mode == PACE_MODE_TRANCHE_OR_WORKFLOW
        assert decision.allow_tranche_or_workflow is True
        assert decision.concurrency_multiplier == _SATURDAY_RESET.behind_concurrency_multiplier
        assert decision.concurrency_multiplier > 1.0

    def test_ahead_of_curve_forces_single_and_lowers_concurrency(self) -> None:
        limits = PlanLimits(status="ok", session_pct=10.0, week_pct=90.0)
        decision = evaluate_pace(limits, _SATURDAY_RESET, now=self.NOW)
        assert decision.status == "ahead"
        assert decision.preferred_mode == PACE_MODE_SINGLE
        assert decision.allow_tranche_or_workflow is False
        assert decision.concurrency_multiplier == _SATURDAY_RESET.ahead_concurrency_multiplier
        assert decision.concurrency_multiplier < 1.0

    def test_on_pace_is_unconstrained(self) -> None:
        # target is ~30%; stay inside both dead bands.
        limits = PlanLimits(status="ok", session_pct=10.0, week_pct=30.0)
        decision = evaluate_pace(limits, _SATURDAY_RESET, now=self.NOW)
        assert decision.status == "on_pace"
        assert decision.preferred_mode == PACE_MODE_UNCONSTRAINED
        assert decision.concurrency_multiplier == 1.0
        assert decision.allow_tranche_or_workflow is True

    def test_near_session_limit_blocks_long_starts_even_when_behind(self) -> None:
        limits = PlanLimits(status="ok", session_pct=80.0, week_pct=5.0)
        decision = evaluate_pace(limits, _SATURDAY_RESET, now=self.NOW)
        assert decision.status == "behind"  # weekly reading is unchanged
        assert decision.near_session_limit is True
        assert decision.preferred_mode == PACE_MODE_SINGLE
        assert decision.allow_tranche_or_workflow is False

    def test_message_and_formatting_are_non_empty(self) -> None:
        limits = PlanLimits(status="ok", session_pct=10.0, week_pct=5.0)
        decision = evaluate_pace(limits, _SATURDAY_RESET, now=self.NOW)
        assert decision.message
        rendered = format_pace_decision(decision)
        assert "Budget pace" in rendered
        assert decision.status in rendered

    def test_to_dict_round_trips_every_field(self) -> None:
        limits = PlanLimits(status="ok", session_pct=10.0, week_pct=5.0)
        decision = evaluate_pace(limits, _SATURDAY_RESET, now=self.NOW)
        d = decision.to_dict()
        assert d["status"] == "behind"
        assert d["preferred_mode"] == PACE_MODE_TRANCHE_OR_WORKFLOW
        assert d["allow_tranche_or_workflow"] is True
        assert d["concurrency_multiplier"] == decision.concurrency_multiplier


# ── pace_adjusted_max_parallel ───────────────────────────────────────────────


class TestPaceAdjustedMaxParallel:
    def test_zero_base_passes_through_unchanged(self) -> None:
        limits = PlanLimits(status="ok", session_pct=10.0, week_pct=5.0)
        decision = evaluate_pace(limits, _SATURDAY_RESET, now=TestEvaluatePace.NOW)
        assert pace_adjusted_max_parallel(0, decision) == 0

    def test_negative_base_passes_through_unchanged(self) -> None:
        limits = PlanLimits(status="ok", session_pct=10.0, week_pct=5.0)
        decision = evaluate_pace(limits, _SATURDAY_RESET, now=TestEvaluatePace.NOW)
        assert pace_adjusted_max_parallel(-1, decision) == -1

    def test_behind_scales_up(self) -> None:
        limits = PlanLimits(status="ok", session_pct=10.0, week_pct=5.0)
        decision = evaluate_pace(limits, _SATURDAY_RESET, now=TestEvaluatePace.NOW)
        assert pace_adjusted_max_parallel(4, decision) == 6  # 4 * 1.5

    def test_ahead_scales_down_but_never_below_one(self) -> None:
        limits = PlanLimits(status="ok", session_pct=10.0, week_pct=90.0)
        decision = evaluate_pace(limits, _SATURDAY_RESET, now=TestEvaluatePace.NOW)
        assert pace_adjusted_max_parallel(1, decision) == 1
        assert pace_adjusted_max_parallel(4, decision) == 2  # 4 * 0.5

    def test_no_signal_is_a_no_op(self) -> None:
        decision = evaluate_pace(PlanLimits(status="unknown"), PacingConfig())
        assert pace_adjusted_max_parallel(7, decision) == 7


# ── coord drive-queue status wiring ─────────────────────────────────────────


class TestDriveQueueStatusSurfacesPace:
    def test_text_output_includes_a_pace_line(self, tmp_path: Path) -> None:
        config_path = tmp_path / "coordinator.yml"
        config_path.write_text(BASE)
        result = CliRunner().invoke(
            main, ["drive-queue", "status", "--config", str(config_path)],
        )
        assert result.exit_code == 0, result.output
        assert "Budget pace:" in result.output

    def test_json_output_carries_a_pace_object(self, tmp_path: Path) -> None:
        config_path = tmp_path / "coordinator.yml"
        config_path.write_text(BASE)
        result = CliRunner().invoke(
            main,
            ["drive-queue", "status", "--json", "--config", str(config_path)],
        )
        assert result.exit_code == 0, result.output
        payload = _json.loads(result.output)
        assert "pace" in payload
        assert payload["pace"]["status"] in ("disabled", "unknown")

    def test_disabled_pacing_reads_as_disabled_not_unknown(self, tmp_path: Path) -> None:
        # `pacing:` absent from the config -> `enabled` defaults to False,
        # so the probe is never consulted and the status is unambiguous
        # ("disabled"), not "unknown" (which would suggest a probe failed).
        config_path = tmp_path / "coordinator.yml"
        config_path.write_text(BASE)
        result = CliRunner().invoke(
            main,
            ["drive-queue", "status", "--json", "--config", str(config_path)],
        )
        assert result.exit_code == 0, result.output
        payload = _json.loads(result.output)
        assert payload["pace"]["status"] == "disabled"
