"""Tests for `coord smoke nightly-sweep` (#3661) — the CLI wiring that fans
`coord smoke nightly`'s own per-repo run out across every repo+artifact
`coordinator.yml` opts into via `release_gate.<repo>.nightly_required`.

The per-pair run itself (`_run_one_nightly_smoke`) is a thin re-use of
`coord.nightly_runner.run_nightly_smoke`, already covered end-to-end by
tests/test_nightly_runner.py and tests/test_cli_smoke_nightly.py — this file
only checks the SWEEP seam: which pairs it picks, that one pair crashing
does not abort the others, and the whole-process exit-code contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from coord.config import Config, ReleaseGateConfig, ReleaseGateRepoConfig
from coord.models import Board, Machine, Repo
from coord.nightly_runner import NightlyRunnerError, NightlyRunPlan, NightlyRunReport
from coord.nightly_smoke import (
    NightlyStepObservation,
    NightlyStepOutcome,
    StepVerdict,
    StepVerdictKind,
)


def _plan(*, repo: str = "vimcode", artifact: str = "macos-dmg") -> NightlyRunPlan:
    return NightlyRunPlan(
        repo=repo, artifact=artifact, spec="tests/smoke-spec/install.yaml",
        driver_kind="cli-pytest", source="build", ref="integration",
        detail="build it", host=None, host_rationale="",
        infra_blocked=False, infra_reason="",
    )


def _outcome(*, action: str, step: str = "launch") -> NightlyStepOutcome:
    obs = NightlyStepObservation(
        repo="vimcode", spec="tests/smoke-spec/install.yaml", step=step,
        sha="deadbeef", passed=action == "none", checked_at=1.0,
    )
    kind = StepVerdictKind.GREEN_CLEAN if action == "none" else StepVerdictKind.RED_NEEDS_FILING
    verdict = StepVerdict(observation=obs, known_bug=None, kind=kind)
    return NightlyStepOutcome(verdict=verdict, action=action, issue_number=99 if action == "filed" else None)


@pytest.fixture(autouse=True)
def _coord_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COORD_DIR", str(tmp_path / "coord_dir"))


def _config_two_pairs() -> Config:
    return Config(
        repos=[
            Repo(name="vimcode", github="acme/vimcode"),
            Repo(name="natal-chart", github="acme/natal-chart"),
        ],
        machines=[Machine(name="m1", host="m1.tail", repos=["vimcode", "natal-chart"])],
        release_gate=ReleaseGateConfig(repos={
            "vimcode": ReleaseGateRepoConfig(
                nightly_required=True, nightly_artifacts=["macos-dmg"],
            ),
            "natal-chart": ReleaseGateRepoConfig(
                nightly_required=True, nightly_artifacts=["win-exe"],
            ),
        }),
    )


def _config_no_pairs() -> Config:
    return Config(
        repos=[Repo(name="vimcode", github="acme/vimcode")],
        machines=[Machine(name="m1", host="m1.tail", repos=["vimcode"])],
    )


def _patch_github(monkeypatch: pytest.MonkeyPatch, config: Config) -> None:
    monkeypatch.setattr("coord.commands.smoke._load_config", lambda path: config)
    monkeypatch.setattr("coord.board_service.read_board", lambda: Board())
    monkeypatch.setattr("coord.github_ops.get_open_issues", lambda slug: [])
    monkeypatch.setattr("coord.commands.smoke._fetch_closed_issues", lambda slug: [])


class TestSmokeNightlySweepCli:
    def test_no_repo_opted_in_exits_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from coord.cli import main

        monkeypatch.setattr("coord.commands.smoke._load_config", lambda path: _config_no_pairs())
        result = CliRunner().invoke(main, ["smoke", "nightly-sweep"])
        assert result.exit_code == 1
        assert "nothing to sweep" in result.output

    def test_runs_one_pair_per_configured_artifact(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from coord.cli import main

        config = _config_two_pairs()
        _patch_github(monkeypatch, config)
        calls: list[tuple[str, str]] = []

        def _fake_run(*, repo, artifact, **kwargs):
            calls.append((repo, artifact))
            return NightlyRunReport(plan=_plan(repo=repo, artifact=artifact), ran=True, sha="deadbeef")

        monkeypatch.setattr("coord.nightly_runner.run_nightly_smoke", _fake_run)
        result = CliRunner().invoke(main, ["smoke", "nightly-sweep"])
        assert result.exit_code == 0, result.output
        assert sorted(calls) == [("natal-chart", "win-exe"), ("vimcode", "macos-dmg")]

    def test_one_pairs_runner_error_does_not_abort_the_other(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from coord.cli import main

        config = _config_two_pairs()
        _patch_github(monkeypatch, config)

        def _fake_run(*, repo, artifact, **kwargs):
            if repo == "vimcode":
                raise NightlyRunnerError("no acceptance driver configured")
            return NightlyRunReport(plan=_plan(repo=repo, artifact=artifact), ran=True, sha="deadbeef")

        monkeypatch.setattr("coord.nightly_runner.run_nightly_smoke", _fake_run)
        result = CliRunner().invoke(main, ["smoke", "nightly-sweep"])
        assert result.exit_code == 0, result.output
        assert "no acceptance driver configured" in result.output
        assert "natal-chart" in result.output

    def test_one_pairs_unexpected_crash_does_not_abort_the_other(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#3661 review round 1: the one type that already worked
        (`NightlyRunnerError`) is not the only thing `run_nightly_smoke` can
        raise unwrapped — a `resolve_sha` git/network hiccup, a live host
        probe in `_resolve_bugbash_lane`, or `subprocess_coord_runner`
        raising on a non-zero `coord` exit while filing all surface as some
        OTHER exception type. The sweep must catch those too, or repo A's
        hiccup silently drops every repo that sorts after it."""
        from coord.cli import main

        config = _config_two_pairs()
        _patch_github(monkeypatch, config)

        def _fake_run(*, repo, artifact, **kwargs):
            if repo == "vimcode":
                raise RuntimeError("git fetch timed out")
            return NightlyRunReport(plan=_plan(repo=repo, artifact=artifact), ran=True, sha="deadbeef")

        monkeypatch.setattr("coord.nightly_runner.run_nightly_smoke", _fake_run)
        result = CliRunner().invoke(main, ["smoke", "nightly-sweep"])
        assert result.exit_code == 0, result.output
        assert "git fetch timed out" in result.output
        assert "natal-chart" in result.output

    def test_a_dropped_finding_in_any_pair_exits_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Mirrors the single-repo command's own exit-code contract
        (`any_dropped` outranks everything else) — a defect in THIS
        command's own filing step, not an app-red/INFRA night."""
        from coord.cli import main

        config = _config_two_pairs()
        _patch_github(monkeypatch, config)

        def _fake_run(*, repo, artifact, **kwargs):
            outcomes = (_outcome(action="not-filed"),) if repo == "vimcode" else ()
            return NightlyRunReport(
                plan=_plan(repo=repo, artifact=artifact), ran=True, sha="deadbeef",
                outcomes=outcomes,
            )

        monkeypatch.setattr("coord.nightly_runner.run_nightly_smoke", _fake_run)
        result = CliRunner().invoke(main, ["smoke", "nightly-sweep"])
        assert result.exit_code == 1, result.output

    def test_an_app_red_pair_does_not_fail_the_whole_sweep(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#3661 "leave the queue alone": an app-red/INFRA night is
        reported and persisted, never this process's own failure."""
        from coord.cli import main

        config = _config_two_pairs()
        _patch_github(monkeypatch, config)

        def _fake_run(*, repo, artifact, **kwargs):
            outcomes = (_outcome(action="filed"),) if repo == "vimcode" else ()
            return NightlyRunReport(
                plan=_plan(repo=repo, artifact=artifact), ran=True, sha="deadbeef",
                outcomes=outcomes,
            )

        monkeypatch.setattr("coord.nightly_runner.run_nightly_smoke", _fake_run)
        result = CliRunner().invoke(main, ["smoke", "nightly-sweep"])
        assert result.exit_code == 0, result.output
        assert "#99" in result.output

    def test_dry_run_passes_through_per_pair(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from coord.cli import main

        config = _config_two_pairs()
        monkeypatch.setattr("coord.commands.smoke._load_config", lambda path: config)
        monkeypatch.setattr("coord.board_service.read_board", lambda: Board())
        captured: list[bool] = []

        def _fake_run(*, repo, artifact, dry_run, **kwargs):
            captured.append(dry_run)
            return NightlyRunReport(plan=_plan(repo=repo, artifact=artifact), ran=False)

        monkeypatch.setattr("coord.nightly_runner.run_nightly_smoke", _fake_run)
        result = CliRunner().invoke(main, ["smoke", "nightly-sweep", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert captured == [True, True]

    def test_json_emits_one_results_array(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import json as _json

        from coord.cli import main

        config = _config_two_pairs()
        _patch_github(monkeypatch, config)

        def _fake_run(*, repo, artifact, **kwargs):
            return NightlyRunReport(plan=_plan(repo=repo, artifact=artifact), ran=True, sha="deadbeef")

        monkeypatch.setattr("coord.nightly_runner.run_nightly_smoke", _fake_run)
        result = CliRunner().invoke(main, ["smoke", "nightly-sweep", "--json"])
        assert result.exit_code == 0, result.output
        payload = _json.loads(result.output)
        assert len(payload["results"]) == 2
        assert {r["repo"] for r in payload["results"]} == {"vimcode", "natal-chart"}
