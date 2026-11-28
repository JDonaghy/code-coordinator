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
    pace_decision_for_unreadable_config,
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

# The same reset, as a `coordinator.yml` `pacing:` block — for the CLI-level
# (black-box) tests below, where `evaluate_pace` is reached through
# `coord drive-queue status`/`tick`, not called directly.
ENABLED_PACING = (
    BASE
    + """\
pacing:
  enabled: true
  reset_weekday: saturday
  reset_time: "09:00"
  tz: UTC
concurrency:
  max_workers: 100
"""
)

# `pacing.enabled: true` with no `tz` — the exact first-run mistake
# `coord.config._parse_pacing` rejects with `ConfigError`. Used to prove
# `coord drive-queue status`/`tick` report this as "the config could not be
# read" (`status="unknown"`), never as the OBSERVED `"disabled"` a
# fail-soft substitution used to assert without ever reading the config.
INVALID_PACING = (
    BASE
    + """\
pacing:
  enabled: true
"""
)

# One hour after the Saturday 09:00 UTC reset — close to the START of the
# week, so the target spend curve is near 0%.
_JUST_AFTER_RESET = datetime(2026, 11, 28, 10, 0, tzinfo=ZoneInfo("UTC")).timestamp()

# One hour before the NEXT Saturday 09:00 UTC reset — close to the END of
# the week, so the target spend curve is near 100%.
_JUST_BEFORE_RESET = datetime(2026, 12, 5, 8, 0, tzinfo=ZoneInfo("UTC")).timestamp()


def _ts(year, month, day, hour=0, minute=0, tz="UTC") -> float:
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(tz)).timestamp()


def _enabled_pacing_with_max_workers(max_workers: int) -> str:
    """`ENABLED_PACING` with a tight `concurrency.max_workers` instead of
    its deliberately-generous 100 — for the tests that drive the pace
    scaler's interaction with the fleet's real worker capacity."""
    return (
        BASE
        + f"""\
pacing:
  enabled: true
  reset_weekday: saturday
  reset_time: "09:00"
  tz: UTC
concurrency:
  max_workers: {max_workers}
"""
    )


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

    def test_max_workers_cap_bounds_the_raise(self) -> None:
        # An explicit `--max-parallel`/`pipeline.max_parallel` is never
        # clamped to `concurrency.max_workers` upstream (only
        # `resolve_max_parallel`'s derived-default branch is) — so
        # `behind_concurrency_multiplier` can scale it above the fleet's
        # real worker capacity unless this function bounds the raise.
        limits = PlanLimits(status="ok", session_pct=10.0, week_pct=5.0)
        decision = evaluate_pace(limits, _SATURDAY_RESET, now=TestEvaluatePace.NOW)
        assert pace_adjusted_max_parallel(4, decision, max_workers_cap=5) == 5  # 6, capped
        # `max_workers_cap<=0` keeps today's "no cap known" convention.
        assert pace_adjusted_max_parallel(4, decision, max_workers_cap=0) == 6
        assert pace_adjusted_max_parallel(4, decision, max_workers_cap=-1) == 6

    def test_max_workers_cap_never_lowers_the_resolved_ceiling(self) -> None:
        # #3388's resolution order: an explicit `--max-parallel 7`
        # outranks `concurrency.max_workers` and dispatches against 7 even
        # on a 2-worker fleet. Pacing scales that answer; it is not a
        # second place `concurrency.max_workers` gets to overrule it, so a
        # cap BELOW the resolved ceiling leaves the ceiling alone (the
        # raise is simply refused) rather than cutting it to the cap.
        limits = PlanLimits(status="ok", session_pct=10.0, week_pct=5.0)
        behind = evaluate_pace(limits, _SATURDAY_RESET, now=TestEvaluatePace.NOW)
        assert behind.concurrency_multiplier == 1.5
        assert pace_adjusted_max_parallel(7, behind, max_workers_cap=2) == 7

        # Same for the multiplier-of-1.0 no-op: a fleet that never opted
        # into `pacing:` must see its resolved ceiling untouched, which is
        # only true if the cap cannot lower it.
        inert = evaluate_pace(PlanLimits(status="unknown"), PacingConfig())
        assert pace_adjusted_max_parallel(7, inert, max_workers_cap=2) == 7

        # An AHEAD reading still lowers it — pacing's own scaling is not
        # what the base floor protects against.
        ahead_limits = PlanLimits(status="ok", session_pct=10.0, week_pct=90.0)
        ahead = evaluate_pace(ahead_limits, _SATURDAY_RESET, now=TestEvaluatePace.NOW)
        assert pace_adjusted_max_parallel(4, ahead, max_workers_cap=2) == 2  # 4 * 0.5


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

    def test_invalid_config_reads_as_unknown_not_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `pacing.enabled: true` with no `tz` fails `coord.config.load` —
        # `status` must report that it never actually read a disabled
        # config, not assert `"disabled"` for a config it couldn't parse
        # at all (the #2096 "unconfirmed verdict" shape).
        config_path = tmp_path / "coordinator.yml"
        config_path.write_text(INVALID_PACING)
        # The probe must never be consulted either — the config was
        # rejected before `evaluate_pace` was ever reached.
        monkeypatch.setattr(
            "coord.usage_limits.get_plan_limits",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("probe must not run when the config is unreadable")
            ),
        )
        result = CliRunner().invoke(
            main,
            ["drive-queue", "status", "--json", "--config", str(config_path)],
        )
        assert result.exit_code == 0, result.output
        payload = _json.loads(result.output)
        assert payload["pace"]["status"] == "unknown"
        assert "could not be read" in payload["pace"]["message"]

        # Text output must not claim `pacing.enabled: false` either.
        text_result = CliRunner().invoke(
            main, ["drive-queue", "status", "--config", str(config_path)],
        )
        assert text_result.exit_code == 0, text_result.output
        assert "pacing.enabled: false" not in text_result.output

    def test_disabled_pacing_never_consults_the_probe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = tmp_path / "coordinator.yml"
        config_path.write_text(BASE)  # no `pacing:` block -> disabled
        monkeypatch.setattr(
            "coord.usage_limits.get_plan_limits",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("probe must not run while pacing is disabled")
            ),
        )
        result = CliRunner().invoke(
            main,
            ["drive-queue", "status", "--json", "--config", str(config_path)],
        )
        assert result.exit_code == 0, result.output
        assert _json.loads(result.output)["pace"]["status"] == "disabled"

    def test_enabled_behind_pace_renders_through_status(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = tmp_path / "coordinator.yml"
        config_path.write_text(ENABLED_PACING)
        monkeypatch.setattr("coord.usage_limits.time.time", lambda: _JUST_BEFORE_RESET)
        monkeypatch.setattr(
            "coord.usage_limits.get_plan_limits",
            lambda *a, **k: PlanLimits(status="ok", week_pct=5.0, session_pct=10.0),
        )

        json_result = CliRunner().invoke(
            main,
            ["drive-queue", "status", "--json", "--config", str(config_path)],
        )
        assert json_result.exit_code == 0, json_result.output
        pace = _json.loads(json_result.output)["pace"]
        assert pace["status"] == "behind"
        assert pace["preferred_mode"] == PACE_MODE_TRANCHE_OR_WORKFLOW
        assert pace["allow_tranche_or_workflow"] is True
        assert pace["concurrency_multiplier"] == 1.5

        text_result = CliRunner().invoke(
            main, ["drive-queue", "status", "--config", str(config_path)],
        )
        assert text_result.exit_code == 0, text_result.output
        assert "Budget pace: behind" in text_result.output
        assert f"mode preference: {PACE_MODE_TRANCHE_OR_WORKFLOW}" in text_result.output
        assert "concurrency x1.50" in text_result.output


# ── `_current_pace_decision` (status/tick's shared config load) ─────────────


class TestCurrentPaceDecision:
    def test_unreadable_config_decision_is_unknown_not_disabled(self) -> None:
        decision = pace_decision_for_unreadable_config(
            ConfigError("pacing.tz is required when pacing.enabled is true")
        )
        assert decision.status == "unknown"
        assert decision.preferred_mode == PACE_MODE_UNCONSTRAINED
        assert decision.allow_tranche_or_workflow is True
        assert "could not be read" in decision.message
        assert "pacing.tz is required" in decision.message


# ── `coord drive-queue tick` — the one place pacing changes real behaviour ──


class TestDriveQueueTickScalesConcurrency:
    def test_minus_one_max_parallel_is_rejected_even_when_pacing_is_enabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The `< 0` validation runs BEFORE pace scaling — a bad
        # `--max-parallel` is caught regardless of what pacing would do
        # with it.
        config_path = tmp_path / "coordinator.yml"
        config_path.write_text(ENABLED_PACING)
        monkeypatch.setattr("coord.usage_limits.time.time", lambda: _JUST_BEFORE_RESET)
        monkeypatch.setattr(
            "coord.usage_limits.get_plan_limits",
            lambda *a, **k: PlanLimits(status="ok", week_pct=5.0, session_pct=10.0),
        )
        result = CliRunner().invoke(
            main,
            [
                "drive-queue", "tick", "--dry-run", "--max-parallel", "-1",
                "--config", str(config_path),
            ],
        )
        assert result.exit_code != 0
        assert "--max-parallel must be at least 0" in result.output

    def test_behind_pace_scales_up_the_dry_run_ceiling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = tmp_path / "coordinator.yml"
        config_path.write_text(ENABLED_PACING)
        monkeypatch.setattr("coord.usage_limits.time.time", lambda: _JUST_BEFORE_RESET)
        monkeypatch.setattr(
            "coord.usage_limits.get_plan_limits",
            lambda *a, **k: PlanLimits(status="ok", week_pct=5.0, session_pct=10.0),
        )
        result = CliRunner().invoke(
            main,
            [
                "drive-queue", "tick", "--dry-run", "--max-parallel", "4",
                "--config", str(config_path),
            ],
        )
        assert result.exit_code == 0, result.output
        # 4 * 1.5 (behind_concurrency_multiplier) == 6.
        assert "capacity: 0/6 occupied, 6 free" in result.output

    def test_behind_pace_raise_is_bounded_by_max_workers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An explicit `--max-parallel` is not itself clamped to
        # `concurrency.max_workers` upstream, so without the pace scaler
        # bounding its own raise this would dispatch against 6, past the
        # fleet's declared real worker capacity.
        config_path = tmp_path / "coordinator.yml"
        config_path.write_text(_enabled_pacing_with_max_workers(5))
        monkeypatch.setattr("coord.usage_limits.time.time", lambda: _JUST_BEFORE_RESET)
        monkeypatch.setattr(
            "coord.usage_limits.get_plan_limits",
            lambda *a, **k: PlanLimits(status="ok", week_pct=5.0, session_pct=10.0),
        )
        result = CliRunner().invoke(
            main,
            [
                "drive-queue", "tick", "--dry-run", "--max-parallel", "4",
                "--config", str(config_path),
            ],
        )
        assert result.exit_code == 0, result.output
        # 4 * 1.5 == 6, bounded by concurrency.max_workers (5).
        assert "capacity: 0/5 occupied, 5 free" in result.output

    def test_max_workers_below_the_explicit_ceiling_does_not_lower_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # #3388's resolution order survives pacing: `--max-parallel 4`
        # outranks `concurrency.max_workers: 2`, and a `behind` reading
        # may only refuse to raise it — never cut it to the cap.
        config_path = tmp_path / "coordinator.yml"
        config_path.write_text(_enabled_pacing_with_max_workers(2))
        monkeypatch.setattr("coord.usage_limits.time.time", lambda: _JUST_BEFORE_RESET)
        monkeypatch.setattr(
            "coord.usage_limits.get_plan_limits",
            lambda *a, **k: PlanLimits(status="ok", week_pct=5.0, session_pct=10.0),
        )
        result = CliRunner().invoke(
            main,
            [
                "drive-queue", "tick", "--dry-run", "--max-parallel", "4",
                "--config", str(config_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "capacity: 0/4 occupied, 4 free" in result.output

    def test_ahead_pace_scales_down_but_never_to_reconcile_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = tmp_path / "coordinator.yml"
        config_path.write_text(ENABLED_PACING)
        monkeypatch.setattr("coord.usage_limits.time.time", lambda: _JUST_AFTER_RESET)
        monkeypatch.setattr(
            "coord.usage_limits.get_plan_limits",
            lambda *a, **k: PlanLimits(status="ok", week_pct=50.0, session_pct=10.0),
        )
        result = CliRunner().invoke(
            main,
            [
                "drive-queue", "tick", "--dry-run", "--max-parallel", "1",
                "--config", str(config_path),
            ],
        )
        assert result.exit_code == 0, result.output
        # `round(1 * 0.5) == 0`, floored to 1 — scaling must never flip a
        # launching tick into reconcile-only on its own.
        assert "capacity: 0/1 occupied, 1 free" in result.output
        assert "--reconcile-only: updating queue state, launching nothing" not in result.output

    def test_explicit_max_parallel_zero_stays_reconcile_only_under_pacing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = tmp_path / "coordinator.yml"
        config_path.write_text(ENABLED_PACING)
        monkeypatch.setattr("coord.usage_limits.time.time", lambda: _JUST_BEFORE_RESET)
        monkeypatch.setattr(
            "coord.usage_limits.get_plan_limits",
            lambda *a, **k: PlanLimits(status="ok", week_pct=5.0, session_pct=10.0),
        )
        result = CliRunner().invoke(
            main,
            [
                "drive-queue", "tick", "--dry-run", "--max-parallel", "0",
                "--config", str(config_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "--reconcile-only: updating queue state, launching nothing" in result.output
