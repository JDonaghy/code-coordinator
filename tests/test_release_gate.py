"""Tests for #3488: the opt-in cross-platform release gate.

Covers three layers:

1. :mod:`coord.release_gate` — the pure decision core (lane/bugbash
   grading, the override pattern). Every failure mode named in the issue's
   acceptance criteria gets its own test, per #2096 ("a gate must be able to
   fail" — a test that only observes the passing verdict does not prove
   that).
2. ``release_gate:`` parsing in :mod:`coord.config`.
3. The ``coord release gate`` CLI (:mod:`coord.commands.release`), including
   real ``git merge-base --is-ancestor`` SHA resolution for the bugbash
   "at or after" rule.
4. The #3488 parity-matrix report (:mod:`coord.reports`), rendered for
   vimcode from a fixture of lane results.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from coord.config import Config, ConfigError, ReleaseGateConfig, ReleaseGateRepoConfig, parse_mapping
from coord.models import Machine, Repo
from coord.release_gate import (
    BugbashRunRecord,
    GateOverride,
    LaneResult,
    apply_override,
    evaluate_release_gate,
    validate_override_reason,
)
from coord.reports import Tier1FeatureSupport, fold_release_parity_matrix, run_report


# ── coord.release_gate: the pure core ──────────────────────────────────────


def _lanes_all_green(sha: str = "deadbeef") -> list[LaneResult]:
    return [
        LaneResult(lane="tui-pty", sha=sha, passed=True),
        LaneResult(lane="win-native", sha=sha, passed=True),
        LaneResult(lane="mac-native", sha=sha, passed=True),
        LaneResult(lane="gtk-native", sha=sha, passed=True),
    ]


class TestEvaluateReleaseGateLanes:
    def test_all_lanes_green_and_no_bugbash_required_passes(self) -> None:
        verdict = evaluate_release_gate(
            repo="vimcode",
            release_sha="deadbeef",
            required_lanes=["tui-pty", "win-native"],
            lane_results=_lanes_all_green(),
        )
        assert verdict.gate_passed is True
        assert verdict.effective_passed is True
        assert verdict.failing_steps == ()

    def test_one_red_lane_fails_and_names_it(self) -> None:
        verdict = evaluate_release_gate(
            repo="vimcode",
            release_sha="deadbeef",
            required_lanes=["tui-pty", "win-native"],
            lane_results=[
                LaneResult(lane="tui-pty", sha="deadbeef", passed=True),
                LaneResult(
                    lane="win-native", sha="deadbeef", passed=False,
                    detail="no menu bar, no window controls (quadraui#1199/#1200)",
                ),
            ],
        )
        assert verdict.gate_passed is False
        assert verdict.effective_passed is False
        [failing] = verdict.failing_steps
        assert failing.name == "lane:win-native"
        assert "menu bar" in failing.detail

    def test_unavailable_lane_blocks_the_gate_but_is_labeled_unavailable_not_failed(self) -> None:
        """#3510: a locked/absent GUI session must still block the release
        (#2096: a gate must be able to fail) but must be distinguishable
        from an ordinary app-bug failure."""
        verdict = evaluate_release_gate(
            repo="vimcode",
            release_sha="deadbeef",
            required_lanes=["tui-pty", "win-native"],
            lane_results=[
                LaneResult(lane="tui-pty", sha="deadbeef", passed=True),
                LaneResult(
                    lane="win-native", sha="deadbeef", passed=False, unavailable=True,
                    detail="LogonUI.exe running — desktop is locked",
                ),
            ],
        )
        assert verdict.gate_passed is False
        assert verdict.effective_passed is False
        [failing] = verdict.failing_steps
        assert failing.name == "lane:win-native"
        assert failing.unavailable is True
        assert "desktop is locked" in failing.detail
        [unavailable_step] = verdict.unavailable_steps
        assert unavailable_step.name == "lane:win-native"

    def test_unavailable_lane_default_detail_tells_the_operator_to_unlock_not_debug(self) -> None:
        verdict = evaluate_release_gate(
            repo="vimcode",
            release_sha="deadbeef",
            required_lanes=["win-native"],
            lane_results=[
                LaneResult(lane="win-native", sha="deadbeef", passed=False, unavailable=True),
            ],
        )
        [step] = verdict.steps
        assert step.unavailable is True
        assert step.passed is False
        assert "unlock" in step.detail

    def test_failed_lane_is_not_unavailable(self) -> None:
        """A genuine app failure must never be mislabeled `unavailable` —
        only a lane result that actually set it is."""
        verdict = evaluate_release_gate(
            repo="vimcode",
            release_sha="deadbeef",
            required_lanes=["win-native"],
            lane_results=[
                LaneResult(lane="win-native", sha="deadbeef", passed=False, detail="crashed"),
            ],
        )
        [step] = verdict.steps
        assert step.unavailable is False
        assert verdict.unavailable_steps == ()

    def test_missing_lane_result_fails_never_defaults_to_pass(self) -> None:
        """#2096: a lane the caller never observed at all must not silently
        count as green."""
        verdict = evaluate_release_gate(
            repo="vimcode",
            release_sha="deadbeef",
            required_lanes=["tui-pty", "gtk-native"],
            lane_results=[LaneResult(lane="tui-pty", sha="deadbeef", passed=True)],
        )
        assert verdict.gate_passed is False
        [failing] = verdict.failing_steps
        assert failing.name == "lane:gtk-native"
        assert "no Tier-2 smoke result" in failing.detail

    def test_lane_result_at_a_different_sha_is_too_stale_and_fails(self) -> None:
        """#2096: a snapshot from a commit other than the release SHA cannot
        contradict anything that changed since — it must not be silently
        accepted as evidence for THIS release."""
        verdict = evaluate_release_gate(
            repo="vimcode",
            release_sha="deadbeef",
            required_lanes=["win-native"],
            lane_results=[
                LaneResult(lane="win-native", sha="oldsha000", passed=True, checked_at=5.0),
            ],
        )
        assert verdict.gate_passed is False
        [failing] = verdict.failing_steps
        assert failing.name == "lane:win-native"
        assert "too stale" in failing.detail
        assert "oldsha000" in failing.detail

    def test_picks_the_most_recently_checked_result_for_a_lane(self) -> None:
        verdict = evaluate_release_gate(
            repo="vimcode",
            release_sha="deadbeef",
            required_lanes=["win-native"],
            lane_results=[
                LaneResult(lane="win-native", sha="deadbeef", passed=False,
                           detail="stale failure", checked_at=1.0),
                LaneResult(lane="win-native", sha="deadbeef", passed=True,
                           detail="re-run passed", checked_at=2.0),
            ],
        )
        assert verdict.gate_passed is True
        assert verdict.steps[0].detail == "re-run passed"


class TestEvaluateReleaseGateBugbash:
    def test_bugbash_not_required_is_never_evaluated(self) -> None:
        verdict = evaluate_release_gate(
            repo="vimcode",
            release_sha="deadbeef",
            required_lanes=["tui-pty"],
            lane_results=[LaneResult(lane="tui-pty", sha="deadbeef", passed=True)],
            bugbash_required=False,
        )
        assert not any(s.name == "bugbash" for s in verdict.steps)
        assert verdict.gate_passed is True

    def test_no_eligible_bugbash_run_fails(self) -> None:
        verdict = evaluate_release_gate(
            repo="vimcode",
            release_sha="deadbeef",
            required_lanes=[],
            bugbash_required=True,
            bugbash_runs=[],
        )
        [step] = verdict.steps
        assert step.name == "bugbash"
        assert step.passed is False
        assert "no `coord bugbash` run found" in step.detail

    def test_bugbash_run_with_new_findings_fails(self) -> None:
        verdict = evaluate_release_gate(
            repo="vimcode",
            release_sha="deadbeef",
            required_lanes=[],
            bugbash_required=True,
            bugbash_runs=[BugbashRunRecord(sha="deadbeef", new_findings=3, ran_at=10.0)],
        )
        [step] = verdict.steps
        assert step.passed is False
        assert "3 new finding" in step.detail

    def test_unverified_bugbash_run_lane_failure_fails_not_a_clean_pass(self) -> None:
        """#2096: `coord bugbash`'s own `"lane_failure"` termination (every
        explored lane failed to dispatch/poll/log) must never be read here
        as a clean, zero-findings pass — see BugbashRunRecord.verified."""
        verdict = evaluate_release_gate(
            repo="vimcode",
            release_sha="deadbeef",
            required_lanes=[],
            bugbash_required=True,
            bugbash_runs=[
                BugbashRunRecord(
                    sha="deadbeef", new_findings=0, verified=False,
                    ran_at=10.0, detail="every lane failed to dispatch",
                ),
            ],
        )
        [step] = verdict.steps
        assert step.passed is False
        assert "not a verified clean pass" in step.detail

    def test_clean_verified_bugbash_run_passes(self) -> None:
        verdict = evaluate_release_gate(
            repo="vimcode",
            release_sha="deadbeef",
            required_lanes=[],
            bugbash_required=True,
            bugbash_runs=[BugbashRunRecord(sha="deadbeef", new_findings=0, ran_at=10.0)],
        )
        [step] = verdict.steps
        assert step.passed is True

    def test_at_or_after_comparator_admits_a_later_eligible_run(self) -> None:
        """A bugbash run at a commit AFTER the release SHA (per the injected
        ancestry comparator) counts — it does not have to match exactly, but
        the comparator (never this function) decides that."""
        verdict = evaluate_release_gate(
            repo="vimcode",
            release_sha="deadbeef",
            required_lanes=[],
            bugbash_required=True,
            bugbash_runs=[BugbashRunRecord(sha="latersha1", new_findings=0, ran_at=10.0)],
            sha_is_at_or_after=lambda candidate, release: candidate == "latersha1",
        )
        [step] = verdict.steps
        assert step.passed is True

    def test_default_comparator_rejects_a_non_identical_sha(self) -> None:
        """No ancestry oracle supplied -> only an EXACT match is provably "at
        or after" (#2096: never assume a descendant relationship)."""
        verdict = evaluate_release_gate(
            repo="vimcode",
            release_sha="deadbeef",
            required_lanes=[],
            bugbash_required=True,
            bugbash_runs=[BugbashRunRecord(sha="somethingelse", new_findings=0, ran_at=10.0)],
        )
        [step] = verdict.steps
        assert step.passed is False

    def test_picks_the_most_recent_eligible_run(self) -> None:
        verdict = evaluate_release_gate(
            repo="vimcode",
            release_sha="deadbeef",
            required_lanes=[],
            bugbash_required=True,
            bugbash_runs=[
                BugbashRunRecord(sha="deadbeef", new_findings=2, ran_at=1.0),
                BugbashRunRecord(sha="deadbeef", new_findings=0, ran_at=2.0),
            ],
        )
        [step] = verdict.steps
        assert step.passed is True


class TestOverride:
    def test_validate_override_reason_rejects_blank(self) -> None:
        with pytest.raises(ValueError):
            validate_override_reason("")
        with pytest.raises(ValueError):
            validate_override_reason("   ")
        with pytest.raises(ValueError):
            validate_override_reason(None)

    def test_validate_override_reason_accepts_and_strips(self) -> None:
        assert validate_override_reason("  known issue #9999  ") == "known issue #9999"

    def test_apply_override_requires_non_empty_reason(self) -> None:
        verdict = evaluate_release_gate(
            repo="vimcode", release_sha="deadbeef", required_lanes=[],
        )
        with pytest.raises(ValueError):
            apply_override(verdict, reason="")

    def test_override_flips_effective_passed_but_keeps_failing_steps_visible(self) -> None:
        """#2096: an override must never erase what the gate actually
        observed — only change whether a release may proceed anyway."""
        verdict = evaluate_release_gate(
            repo="vimcode",
            release_sha="deadbeef",
            required_lanes=["win-native"],
            lane_results=[
                LaneResult(lane="win-native", sha="deadbeef", passed=False, detail="no menu bar"),
            ],
        )
        assert verdict.effective_passed is False

        overridden = apply_override(
            verdict, reason="known win-native regression, hotfix tracked", by="alice",
        )
        assert overridden.gate_passed is False  # unchanged — still genuinely failing
        assert overridden.effective_passed is True  # but may now proceed
        assert overridden.failing_steps == verdict.failing_steps
        assert isinstance(overridden.override, GateOverride)
        assert overridden.override.reason == "known win-native regression, hotfix tracked"
        assert overridden.override.by == "alice"

    def test_gate_with_no_required_lanes_or_bugbash_is_vacuously_green(self) -> None:
        """Not a #2096 violation: an empty declaration is rejected at the
        CONFIG layer (see TestParseReleaseGateConfig), never silently
        tolerated by `coordinator.yml` — this function just documents that
        it takes its inputs as given."""
        verdict = evaluate_release_gate(
            repo="vimcode", release_sha="deadbeef", required_lanes=[],
        )
        assert verdict.steps == ()
        assert verdict.gate_passed is True


# ── coordinator.yml: release_gate: parsing ─────────────────────────────────


def _mapping(release_gate: dict) -> dict:
    return {
        "repos": [{"name": "vimcode", "github": "acme/vimcode"}],
        "machines": [{"name": "m1", "host": "m1.example.ts.net", "repos": ["vimcode"]}],
        "release_gate": release_gate,
    }


class TestParseReleaseGateConfig:
    def test_absent_block_means_no_repo_has_a_gate(self) -> None:
        cfg = parse_mapping({
            "repos": [{"name": "vimcode", "github": "acme/vimcode"}],
            "machines": [{"name": "m1", "host": "m1.example.ts.net", "repos": ["vimcode"]}],
        })
        assert isinstance(cfg.release_gate, ReleaseGateConfig)
        assert cfg.release_gate.for_repo("vimcode") is None

    def test_valid_entry_parses(self) -> None:
        cfg = parse_mapping(_mapping({
            "vimcode": {"lanes": ["tui-pty", "win-native"], "bugbash": "required"},
        }))
        entry = cfg.release_gate.for_repo("vimcode")
        assert entry == ReleaseGateRepoConfig(
            lanes=["tui-pty", "win-native"], bugbash_required=True,
        )

    def test_bugbash_defaults_to_off(self) -> None:
        cfg = parse_mapping(_mapping({"vimcode": {"lanes": ["tui-pty"]}}))
        assert cfg.release_gate.for_repo("vimcode").bugbash_required is False

    def test_unknown_repo_rejected(self) -> None:
        with pytest.raises(ConfigError, match="unknown repo"):
            parse_mapping(_mapping({"nonexistent": {"lanes": ["tui-pty"]}}))

    def test_empty_lanes_rejected(self) -> None:
        """#2096: a gate with zero lanes and no bugbash requirement can never
        observe a failure — reject it at parse time."""
        with pytest.raises(ConfigError, match="lanes must be non-empty"):
            parse_mapping(_mapping({"vimcode": {"lanes": []}}))

    def test_empty_lanes_allowed_when_bugbash_required(self) -> None:
        """A lanes-less, bugbash-only gate can still fail (see
        ``evaluate_release_gate``'s own ``test_no_eligible_bugbash_run_fails``)
        — a repo that only wants to gate on bugbash findings must not be
        forced to also name a Tier-2 lane it doesn't care about."""
        cfg = parse_mapping(
            _mapping({"vimcode": {"lanes": [], "bugbash": "required"}})
        )
        entry = cfg.release_gate.for_repo("vimcode")
        assert entry == ReleaseGateRepoConfig(lanes=[], bugbash_required=True)

    def test_omitted_lanes_allowed_when_bugbash_required(self) -> None:
        cfg = parse_mapping(_mapping({"vimcode": {"bugbash": "required"}}))
        entry = cfg.release_gate.for_repo("vimcode")
        assert entry.lanes == []
        assert entry.bugbash_required is True

    def test_invalid_bugbash_value_rejected(self) -> None:
        with pytest.raises(ConfigError, match="bugbash"):
            parse_mapping(_mapping({"vimcode": {"lanes": ["tui-pty"], "bugbash": "sometimes"}}))

    def test_unknown_option_rejected(self) -> None:
        with pytest.raises(ConfigError, match="unknown option"):
            parse_mapping(_mapping({"vimcode": {"lanes": ["tui-pty"], "typo": True}}))

    def test_not_a_mapping_rejected(self) -> None:
        with pytest.raises(ConfigError, match="mapping"):
            parse_mapping(_mapping(["not", "a", "mapping"]))


# ── `coord release gate` CLI ────────────────────────────────────────────────


def _config_with_gate(*, lanes: list[str], bugbash_required: bool) -> Config:
    return Config(
        repos=[Repo(name="vimcode", github="acme/vimcode")],
        machines=[Machine(name="m1", host="m1.example.ts.net", repos=["vimcode"])],
        release_gate=ReleaseGateConfig(
            repos={
                "vimcode": ReleaseGateRepoConfig(
                    lanes=lanes, bugbash_required=bugbash_required,
                ),
            }
        ),
    )


def _write_json(path: Path, payload: dict) -> Path:
    import json

    path.write_text(json.dumps(payload))
    return path


class TestReleaseGateCli:
    def test_refuses_when_repo_has_no_release_gate_entry(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        from coord.cli import main

        monkeypatch.setattr(
            "coord.commands._common._load_config",
            lambda path: Config(
                repos=[Repo(name="vimcode", github="acme/vimcode")], machines=[],
            ),
        )
        observed = _write_json(tmp_path / "observed.json", {"lanes": [], "bugbash": []})
        result = CliRunner().invoke(
            main,
            ["release", "gate", "vimcode", "--sha", "deadbeef",
             "--from-json", str(observed), "--config", str(tmp_path / "coordinator.yml")],
        )
        assert result.exit_code == 2, result.output
        assert "has not opted into" in result.output

    def test_exits_nonzero_and_names_the_failing_lane(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        from coord.cli import main

        monkeypatch.setattr(
            "coord.commands._common._load_config",
            lambda path: _config_with_gate(lanes=["tui-pty", "win-native"], bugbash_required=False),
        )
        observed = _write_json(tmp_path / "observed.json", {
            "lanes": [
                {"lane": "tui-pty", "sha": "deadbeef", "passed": True},
                {"lane": "win-native", "sha": "deadbeef", "passed": False,
                 "detail": "no menu bar, no window controls"},
            ],
        })
        result = CliRunner().invoke(
            main,
            ["release", "gate", "vimcode", "--sha", "deadbeef",
             "--from-json", str(observed), "--config", str(tmp_path / "coordinator.yml")],
        )
        assert result.exit_code == 1, result.output
        assert "lane:win-native" in result.output
        assert "no menu bar" in result.output
        assert "RESULT: FAIL" in result.output

    def test_unavailable_lane_blocks_and_renders_distinctly_from_fail(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """#3510: the CLI's human-readable output must label an unavailable
        lane distinctly from an app-bug failure, while still refusing the
        release (exit nonzero, RESULT: FAIL)."""
        from coord.cli import main

        monkeypatch.setattr(
            "coord.commands._common._load_config",
            lambda path: _config_with_gate(lanes=["tui-pty", "win-native"], bugbash_required=False),
        )
        observed = _write_json(tmp_path / "observed.json", {
            "lanes": [
                {"lane": "tui-pty", "sha": "deadbeef", "passed": True},
                {"lane": "win-native", "sha": "deadbeef", "passed": False,
                 "unavailable": True,
                 "detail": "LogonUI.exe running — desktop is locked"},
            ],
        })
        result = CliRunner().invoke(
            main,
            ["release", "gate", "vimcode", "--sha", "deadbeef",
             "--from-json", str(observed), "--config", str(tmp_path / "coordinator.yml")],
        )
        assert result.exit_code == 1, result.output
        assert "[UNAVAILABLE] lane:win-native" in result.output
        assert "desktop is locked" in result.output
        assert "RESULT: FAIL" in result.output
        # Never mislabeled as an ordinary failure.
        assert "[FAIL] lane:win-native" not in result.output

    def test_truthy_non_bool_unavailable_is_rejected_not_coerced(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """#2096-review precedent extended to the new field: a hand-authored
        payload spelling `"unavailable": "true"` must be rejected, not
        silently coerced to a real boolean."""
        from coord.cli import main

        monkeypatch.setattr(
            "coord.commands._common._load_config",
            lambda path: _config_with_gate(lanes=["win-native"], bugbash_required=False),
        )
        observed = _write_json(tmp_path / "observed.json", {
            "lanes": [
                {"lane": "win-native", "sha": "deadbeef", "passed": False,
                 "unavailable": "true"},
            ],
        })
        result = CliRunner().invoke(
            main,
            ["release", "gate", "vimcode", "--sha", "deadbeef",
             "--from-json", str(observed), "--config", str(tmp_path / "coordinator.yml")],
        )
        assert result.exit_code == 2, result.output
        assert "unavailable" in result.output

    def test_passes_when_everything_is_green(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        from coord.cli import main

        monkeypatch.setattr(
            "coord.commands._common._load_config",
            lambda path: _config_with_gate(lanes=["tui-pty"], bugbash_required=True),
        )
        observed = _write_json(tmp_path / "observed.json", {
            "lanes": [{"lane": "tui-pty", "sha": "deadbeef", "passed": True}],
            "bugbash": [{"sha": "deadbeef", "new_findings": 0, "ran_at": 5}],
        })
        result = CliRunner().invoke(
            main,
            ["release", "gate", "vimcode", "--sha", "deadbeef",
             "--from-json", str(observed), "--config", str(tmp_path / "coordinator.yml")],
        )
        assert result.exit_code == 0, result.output
        assert "RESULT: PASS" in result.output

    def test_empty_override_reason_rejected_before_anything_else_runs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        from coord.cli import main

        called = False

        def _boom(path):
            nonlocal called
            called = True
            raise AssertionError("must not load config before validating --override")

        monkeypatch.setattr("coord.commands._common._load_config", _boom)
        observed = _write_json(tmp_path / "observed.json", {})
        result = CliRunner().invoke(
            main,
            ["release", "gate", "vimcode", "--sha", "deadbeef",
             "--from-json", str(observed), "--override", "   ",
             "--config", str(tmp_path / "coordinator.yml")],
        )
        assert result.exit_code == 1, result.output
        assert "non-empty reason" in result.output
        assert called is False

    def test_override_lets_a_failing_gate_pass_and_records_the_reason(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        from coord.cli import main

        monkeypatch.setattr(
            "coord.commands._common._load_config",
            lambda path: _config_with_gate(lanes=["win-native"], bugbash_required=False),
        )
        observed = _write_json(tmp_path / "observed.json", {
            "lanes": [{"lane": "win-native", "sha": "deadbeef", "passed": False,
                       "detail": "no menu bar"}],
        })
        reason = "known win-native regression, hotfix tracked in #9999"
        result = CliRunner().invoke(
            main,
            ["release", "gate", "vimcode", "--sha", "deadbeef",
             "--from-json", str(observed), "--override", reason,
             "--config", str(tmp_path / "coordinator.yml"), "--json"],
        )
        assert result.exit_code == 0, result.output
        assert reason in result.output
        assert '"effective_passed": true' in result.output
        assert '"gate_passed": false' in result.output
        # The underlying failure must still be visible in the JSON, not erased:
        assert "no menu bar" in result.output

    def test_malformed_json_file_is_a_clean_error_not_a_traceback(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        from coord.cli import main

        monkeypatch.setattr(
            "coord.commands._common._load_config",
            lambda path: _config_with_gate(lanes=["tui-pty"], bugbash_required=False),
        )
        bad = tmp_path / "observed.json"
        bad.write_text("{not json")
        result = CliRunner().invoke(
            main,
            ["release", "gate", "vimcode", "--sha", "deadbeef",
             "--from-json", str(bad), "--config", str(tmp_path / "coordinator.yml")],
        )
        assert result.exit_code == 2, result.output
        assert "malformed" in result.output or "could not read" in result.output

    @pytest.mark.parametrize("bad_value", ["false", "0", "no", "", 0, 1, None])
    def test_truthy_non_bool_passed_is_rejected_not_coerced(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad_value: object,
    ) -> None:
        """#2096-review: a hand-authored ``--from-json`` payload with
        ``"passed": "false"`` (or ``"0"``, ``0``, ...) must be rejected, not
        silently coerced to ``True`` by Python's own truthiness — that would
        let a genuinely-red lane read as a clean PASS with no error at all."""
        from coord.cli import main

        monkeypatch.setattr(
            "coord.commands._common._load_config",
            lambda path: _config_with_gate(lanes=["tui-pty"], bugbash_required=False),
        )
        observed = _write_json(tmp_path / "observed.json", {
            "lanes": [{"lane": "tui-pty", "sha": "deadbeef", "passed": bad_value}],
        })
        result = CliRunner().invoke(
            main,
            ["release", "gate", "vimcode", "--sha", "deadbeef",
             "--from-json", str(observed), "--config", str(tmp_path / "coordinator.yml")],
        )
        assert result.exit_code == 2, result.output
        assert "malformed" in result.output

    @pytest.mark.parametrize("bad_value", ["false", "0", 0])
    def test_truthy_non_bool_verified_is_rejected_not_coerced(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad_value: object,
    ) -> None:
        """Same trap, on ``bugbash[].verified`` — a lane-failure round that
        never actually observed zero findings must not read as verified."""
        from coord.cli import main

        monkeypatch.setattr(
            "coord.commands._common._load_config",
            lambda path: _config_with_gate(lanes=["tui-pty"], bugbash_required=True),
        )
        observed = _write_json(tmp_path / "observed.json", {
            "lanes": [{"lane": "tui-pty", "sha": "deadbeef", "passed": True}],
            "bugbash": [{"sha": "deadbeef", "new_findings": 0, "verified": bad_value}],
        })
        result = CliRunner().invoke(
            main,
            ["release", "gate", "vimcode", "--sha", "deadbeef",
             "--from-json", str(observed), "--config", str(tmp_path / "coordinator.yml")],
        )
        assert result.exit_code == 2, result.output
        assert "malformed" in result.output

    def test_real_bool_values_still_accepted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """Defense-in-depth for the bool check above: real ``true``/``false``
        JSON booleans must keep working exactly as before."""
        from coord.cli import main

        monkeypatch.setattr(
            "coord.commands._common._load_config",
            lambda path: _config_with_gate(lanes=["tui-pty"], bugbash_required=True),
        )
        observed = _write_json(tmp_path / "observed.json", {
            "lanes": [{"lane": "tui-pty", "sha": "deadbeef", "passed": True}],
            "bugbash": [{"sha": "deadbeef", "new_findings": 0, "verified": False}],
        })
        result = CliRunner().invoke(
            main,
            ["release", "gate", "vimcode", "--sha", "deadbeef",
             "--from-json", str(observed), "--config", str(tmp_path / "coordinator.yml")],
        )
        # verified=False -> the bugbash run never actually observed zero
        # findings -> the bugbash step must fail, not be silently coerced
        # away.
        assert result.exit_code == 1, result.output
        assert "RESULT: FAIL" in result.output

    def test_override_is_recorded_to_the_durable_audit_log(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, coord_db,
    ) -> None:
        """The issue's own acceptance criterion: an override "needs an
        audited reason string, following the `--override-human-required`
        pattern" — and that pattern (coord/commands/merge.py) writes to the
        durable, queryable audit_log via `coord.audit.record_audit`, not
        just stdout/--json. #3488-review: this must mirror it exactly."""
        import getpass

        from coord.audit import query_audit_log
        from coord.cli import main

        monkeypatch.setattr(
            "coord.commands._common._load_config",
            lambda path: _config_with_gate(lanes=["win-native"], bugbash_required=False),
        )
        observed = _write_json(tmp_path / "observed.json", {
            "lanes": [{"lane": "win-native", "sha": "deadbeef", "passed": False,
                       "detail": "no menu bar"}],
        })
        reason = "known win-native regression, hotfix tracked in #9999"
        result = CliRunner().invoke(
            main,
            ["release", "gate", "vimcode", "--sha", "deadbeef",
             "--from-json", str(observed), "--override", reason,
             "--config", str(tmp_path / "coordinator.yml")],
        )
        assert result.exit_code == 0, result.output

        log = query_audit_log(category="release_gate")
        entries = log["entries"]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["event_type"] == "release_gate_override"
        assert entry["tier"] == "business"
        assert entry["repo"] == "vimcode"
        assert reason in entry["summary"]
        assert entry["details"]["reason"] == reason
        assert entry["details"]["release_sha"] == "deadbeef"
        assert entry["details"]["gate_passed"] is False
        assert entry["details"]["failing_steps"] == ["lane:win-native"]
        assert entry["actor"] == getpass.getuser()

    def test_override_by_is_populated_not_unknown(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, coord_db,
    ) -> None:
        """Nit from review: `GateOverride.by` used to never be populated by
        the CLI, so the rendered 'by=unknown' branch was the only one ever
        reachable."""
        import getpass

        from coord.cli import main

        monkeypatch.setattr(
            "coord.commands._common._load_config",
            lambda path: _config_with_gate(lanes=["win-native"], bugbash_required=False),
        )
        observed = _write_json(tmp_path / "observed.json", {
            "lanes": [{"lane": "win-native", "sha": "deadbeef", "passed": False,
                       "detail": "no menu bar"}],
        })
        result = CliRunner().invoke(
            main,
            ["release", "gate", "vimcode", "--sha", "deadbeef",
             "--from-json", str(observed), "--override", "known issue",
             "--config", str(tmp_path / "coordinator.yml"), "--json"],
        )
        assert result.exit_code == 0, result.output
        assert f'"by": "{getpass.getuser()}"' in result.output


class TestReleaseGateShaAncestry:
    """The bugbash "at or after" rule resolved for real, via `git merge-base
    --is-ancestor`, against a throwaway local repo — no network."""

    @staticmethod
    def _git(cwd: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        ).stdout.strip()

    @pytest.fixture
    def repo(self, tmp_path: Path) -> Path:
        r = tmp_path / "repo"
        r.mkdir()
        self._git(r, "init", "-b", "main")
        self._git(r, "config", "user.email", "test@example.com")
        self._git(r, "config", "user.name", "Test")
        return r

    def _commit(self, repo: Path, name: str) -> str:
        (repo / name).write_text(name)
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-m", name)
        return self._git(repo, "rev-parse", "HEAD")

    def test_bugbash_run_descended_from_release_sha_is_eligible(
        self, monkeypatch: pytest.MonkeyPatch, repo: Path, tmp_path: Path,
    ) -> None:
        from coord.cli import main

        release_sha = self._commit(repo, "a.txt")
        bugbash_sha = self._commit(repo, "b.txt")  # descends from release_sha

        monkeypatch.setattr(
            "coord.commands._common._load_config",
            lambda path: _config_with_gate(lanes=[], bugbash_required=True),
        )
        observed = _write_json(tmp_path / "observed.json", {
            "bugbash": [{"sha": bugbash_sha, "new_findings": 0, "ran_at": 1}],
        })
        result = CliRunner().invoke(
            main,
            ["release", "gate", "vimcode", "--sha", release_sha,
             "--from-json", str(observed), "--repo-path", str(repo),
             "--config", str(tmp_path / "coordinator.yml")],
        )
        assert result.exit_code == 0, result.output
        assert "RESULT: PASS" in result.output

    def test_bugbash_run_from_before_the_release_sha_is_not_eligible(
        self, monkeypatch: pytest.MonkeyPatch, repo: Path, tmp_path: Path,
    ) -> None:
        from coord.cli import main

        stale_sha = self._commit(repo, "a.txt")
        release_sha = self._commit(repo, "b.txt")  # descends from stale_sha

        monkeypatch.setattr(
            "coord.commands._common._load_config",
            lambda path: _config_with_gate(lanes=[], bugbash_required=True),
        )
        observed = _write_json(tmp_path / "observed.json", {
            "bugbash": [{"sha": stale_sha, "new_findings": 0, "ran_at": 1}],
        })
        result = CliRunner().invoke(
            main,
            ["release", "gate", "vimcode", "--sha", release_sha,
             "--from-json", str(observed), "--repo-path", str(repo),
             "--config", str(tmp_path / "coordinator.yml")],
        )
        assert result.exit_code == 1, result.output
        assert "no `coord bugbash` run found" in result.output


# ── the #3488 parity matrix report ─────────────────────────────────────────


class TestReleaseParityMatrix:
    def test_renders_for_vimcode_from_a_fixture_of_lane_results(self) -> None:
        lane_results = [
            LaneResult(lane="tui-pty", sha="s1", passed=True, checked_at=1.0),
            LaneResult(lane="win-native", sha="s1", passed=False,
                       detail="no menu bar", checked_at=2.0),
            LaneResult(lane="gtk-native", sha="s1", passed=True, checked_at=3.0),
        ]
        tier1 = [
            Tier1FeatureSupport(feature="menu-bar", platform="win-native", supported=True),
            Tier1FeatureSupport(feature="menu-bar", platform="gtk-native", supported=True),
            Tier1FeatureSupport(feature="window-controls", platform="win-native",
                                 supported=False, detail="#1199"),
        ]

        result = run_report(
            "release-parity-matrix",
            {"repo": "vimcode"},
            tier1_fetch=lambda repo: tier1,
            lane_results_fetch=lambda repo: lane_results,
        )

        assert result.columns == ["feature", "gtk-native", "tui-pty", "win-native"]
        by_feature = {row["feature"]: row for row in result.rows}
        assert by_feature["menu-bar"]["win-native"] == "supported / fail"
        assert by_feature["menu-bar"]["gtk-native"] == "supported / pass"
        # tui-pty has no Tier-1 feature data at all:
        assert by_feature["menu-bar"]["tui-pty"] == "no Tier-1 data / pass"
        assert by_feature["window-controls"]["win-native"] == "unsupported / fail"

    def test_missing_tier1_or_tier2_data_is_noted_not_a_crash(self) -> None:
        result = fold_release_parity_matrix(
            "vimcode", tier1=[], lane_results=[], generated_at=0.0,
        )
        assert result.rows == []
        assert any("No Tier-1" in n for n in result.notes)
        assert any("No Tier-2" in n for n in result.notes)

    def test_lane_with_no_tier1_feature_still_reports_tier2(self) -> None:
        result = fold_release_parity_matrix(
            "vimcode",
            tier1=[Tier1FeatureSupport(feature="menu-bar", platform="win-native", supported=True)],
            lane_results=[LaneResult(lane="mac-native", sha="s1", passed=True)],
            generated_at=0.0,
        )
        assert "mac-native" in result.columns
        row = result.rows[0]
        assert row["mac-native"] == "no Tier-1 data / pass"

    def test_repo_param_is_required(self) -> None:
        from coord.reports import ReportError

        with pytest.raises(ReportError):
            run_report("release-parity-matrix", {})
