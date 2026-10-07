"""Tests for coord/nightly_smoke.py — the #3652 nightly real-platform
smoke runner's pure decision core.

Three things are under test, matching the module docstring's four
"Wanted" items from #3652:

1. :func:`resolve_artifact_plan` — build-vs-download artifact resolution.
2. :func:`pick_nightly_host` — routing reuses `coord.smoke.
   pick_smoke_machine`, never a second capability matcher.
3. :func:`classify_step`/:func:`classify_nightly_run` — the
   #2096-disciplined verdict core (never classifies an unobserved result;
   a clean green and an expected-red known bug never alert).
4. :func:`process_nightly_step` — files/updates/closes exactly one issue
   per (spec, step) through the SAME `coord.bugbash` dedupe/file path
   every other coord-filed bug uses.
"""

from __future__ import annotations

import pytest

from coord.bugbash import BugbashLane
from coord.config import Config, SmokeTestsConfig
from coord.models import Board, Machine, Repo
from coord.nightly_smoke import (
    ArtifactSource,
    NightlyStepObservation,
    StepVerdictKind,
    classify_nightly_run,
    classify_step,
    finding_from_step,
    parse_known_bug_ref,
    pick_nightly_host,
    process_nightly_step,
    resolve_artifact_plan,
)


# ── resolve_artifact_plan ────────────────────────────────────────────────


class TestResolveArtifactPlan:
    def test_integration_branch_wins_over_release_tag(self) -> None:
        plan = resolve_artifact_plan(
            repo="vimcode", artifact="macos-dmg",
            integration_branch="integration", latest_release_tag="v0.15.0",
        )
        assert plan.source is ArtifactSource.BUILD
        assert plan.ref == "integration"

    def test_falls_back_to_release_tag_with_no_integration_branch(self) -> None:
        plan = resolve_artifact_plan(
            repo="vimcode", artifact="macos-dmg",
            integration_branch=None, latest_release_tag="v0.15.0",
        )
        assert plan.source is ArtifactSource.DOWNLOAD
        assert plan.ref == "v0.15.0"

    def test_neither_available_raises(self) -> None:
        """#2096: nothing to build or download must be a loud error, never
        a plan a caller could mistake for 'nothing to do, call it green'."""
        with pytest.raises(ValueError, match="nothing to build or download"):
            resolve_artifact_plan(
                repo="vimcode", artifact="macos-dmg",
                integration_branch=None, latest_release_tag=None,
            )


# ── pick_nightly_host ────────────────────────────────────────────────────


def _machine(name: str, *, caps: list[str]) -> Machine:
    return Machine(name=name, host=f"{name}.tail", capabilities=caps, repos=["vimcode"])


class TestPickNightlyHost:
    def test_picks_a_capable_machine(self) -> None:
        config = Config(
            repos=[Repo(name="vimcode", github="acme/vimcode")],
            machines=[
                _machine("linux-ci", caps=["python"]),
                _machine("macmini", caps=["python", "macos"]),
            ],
            smoke_tests=SmokeTestsConfig(auto_queue=True),
        )
        choice = pick_nightly_host(["macos"], "vimcode", Board(), config)
        assert choice is not None
        assert choice.machine.name == "macmini"

    def test_no_capable_machine_returns_none(self) -> None:
        config = Config(
            repos=[Repo(name="vimcode", github="acme/vimcode")],
            machines=[_machine("linux-ci", caps=["python"])],
            smoke_tests=SmokeTestsConfig(auto_queue=True),
        )
        assert pick_nightly_host(["macos"], "vimcode", Board(), config) is None


# ── parse_known_bug_ref ──────────────────────────────────────────────────


class TestParseKnownBugRef:
    def test_parses_repo_and_number(self) -> None:
        assert parse_known_bug_ref("vimcode#1583") == ("vimcode", 1583)

    def test_rejects_missing_hash(self) -> None:
        with pytest.raises(ValueError, match="not '<repo>#<number>'"):
            parse_known_bug_ref("vimcode1583")

    def test_rejects_non_numeric_issue(self) -> None:
        with pytest.raises(ValueError):
            parse_known_bug_ref("vimcode#abc")


# ── classify_step / classify_nightly_run ─────────────────────────────────


def _obs(*, passed: bool, checked_at: float | None = 100.0, **kwargs) -> NightlyStepObservation:
    defaults = dict(
        repo="vimcode", spec="install.yaml", step="launch", sha="deadbeef",
    )
    defaults.update(kwargs)
    return NightlyStepObservation(passed=passed, checked_at=checked_at, **defaults)


class TestClassifyStep:
    def test_unobserved_result_raises(self) -> None:
        """#2096: unconfirmed success (or failure) is a defect — a step
        with no checked_at was never actually run, so there is nothing to
        classify."""
        with pytest.raises(ValueError, match="refusing to classify"):
            classify_step(_obs(passed=True, checked_at=None), None)

    def test_green_with_no_known_bug_is_clean(self) -> None:
        verdict = classify_step(_obs(passed=True), None)
        assert verdict.kind is StepVerdictKind.GREEN_CLEAN
        assert verdict.alerts is False

    def test_red_with_no_known_bug_needs_filing(self) -> None:
        verdict = classify_step(_obs(passed=False), None)
        assert verdict.kind is StepVerdictKind.RED_NEEDS_FILING
        assert verdict.alerts is True

    def test_red_known_bug_is_expected_and_silent(self) -> None:
        verdict = classify_step(_obs(passed=False), "vimcode#1583")
        assert verdict.kind is StepVerdictKind.RED_EXPECTED_KNOWN_BUG
        assert verdict.alerts is False

    def test_green_known_bug_alerts_to_close_it(self) -> None:
        """#3652 Wanted #3: the bidirectional half of the known-bug gate —
        a parked bug that starts passing must alert, not stay silent."""
        verdict = classify_step(_obs(passed=True), "vimcode#1583")
        assert verdict.kind is StepVerdictKind.GREEN_KNOWN_BUG_FIXED
        assert verdict.alerts is True


class TestClassifyNightlyRun:
    def test_maps_known_bugs_by_spec_and_step(self) -> None:
        observations = [
            _obs(spec="install.yaml", step="launch", passed=True),
            _obs(spec="install.yaml", step="uninstall", passed=False),
        ]
        known_bugs = {("install.yaml", "uninstall"): "vimcode#42"}
        verdicts = classify_nightly_run(observations, known_bugs)
        assert verdicts[0].kind is StepVerdictKind.GREEN_CLEAN
        assert verdicts[1].kind is StepVerdictKind.RED_EXPECTED_KNOWN_BUG

    def test_step_with_no_known_bug_entry_is_not_known(self) -> None:
        verdicts = classify_nightly_run([_obs(passed=False)], {})
        assert verdicts[0].kind is StepVerdictKind.RED_NEEDS_FILING


# ── finding_from_step ─────────────────────────────────────────────────────


class TestFindingFromStep:
    def test_builds_a_finding_with_evidence(self) -> None:
        verdict = classify_step(
            _obs(passed=False, detail="window never appeared", evidence=("shot.png",)),
            None,
        )
        finding = finding_from_step(verdict)
        assert finding.repo == "vimcode"
        assert "install.yaml::launch" in finding.title
        assert finding.captures == ("shot.png",)
        assert "window never appeared" in finding.actual

    def test_rejects_a_non_filing_verdict(self) -> None:
        verdict = classify_step(_obs(passed=True), None)
        with pytest.raises(ValueError, match="RED_NEEDS_FILING"):
            finding_from_step(verdict)


# ── process_nightly_step ──────────────────────────────────────────────────


class FakeRunner:
    """Fakes the `coord` seam — records every call, never touches GitHub."""

    def __init__(self, next_issue_number: int = 100):
        self.calls: list[list[str]] = []
        self._next_issue_number = next_issue_number

    def __call__(self, args):
        args = list(args)
        self.calls.append(args)
        if args[:2] == ["issue", "create"]:
            number = self._next_issue_number
            self._next_issue_number += 1
            return f"#{number} created\n"
        if args[:2] == ["drive-queue", "add"]:
            return "queued\n"
        if args[:2] == ["issue", "comment"]:
            return "commented\n"
        if args[:2] == ["issue", "close"]:
            return "closed\n"
        raise AssertionError(f"unexpected coord call: {args}")


def _lane() -> BugbashLane:
    return BugbashLane(
        platform="nightly-smoke", driver_kind="nightly-smoke", machine="macmini",
        capability="macos",
    )


class TestProcessNightlyStep:
    def test_clean_green_never_calls_runner(self) -> None:
        verdict = classify_step(_obs(passed=True), None)
        outcome = process_nightly_step(verdict, dry_run=False)
        assert outcome.action == "none"

    def test_expected_red_known_bug_never_calls_runner(self) -> None:
        verdict = classify_step(_obs(passed=False), "vimcode#1583")
        outcome = process_nightly_step(verdict, dry_run=False)
        assert outcome.action == "none"

    def test_new_red_step_files_through_bugbash_dedupe(self) -> None:
        verdict = classify_step(_obs(passed=False, detail="crashed"), None)
        runner = FakeRunner(next_issue_number=500)
        outcome = process_nightly_step(
            verdict, open_issues=[], closed_issues=[], lane=_lane(),
            runner=runner, dry_run=False,
        )
        assert outcome.action == "filed"
        assert outcome.issue_number == 500
        assert runner.calls[0][:3] == ["issue", "create", "vimcode"]

    def test_repeated_red_step_comments_on_the_same_open_issue(self) -> None:
        """#3652 Wanted #2: 'repeated failures update the same issue rather
        than filing new ones' — literal behaviour, not merely dedup."""
        verdict = classify_step(_obs(passed=False, detail="crashed again"), None)
        finding = finding_from_step(verdict)
        open_issues = [{"number": 77, "title": finding.title}]
        runner = FakeRunner()
        outcome = process_nightly_step(
            verdict, open_issues=open_issues, closed_issues=[], lane=_lane(),
            runner=runner, dry_run=False,
        )
        assert outcome.action == "commented"
        assert outcome.issue_number == 77
        assert len(runner.calls) == 1  # never re-filed, only commented
        assert runner.calls[0][:3] == ["issue", "comment", "vimcode"]
        assert runner.calls[0][3] == "77"

    def test_dry_run_never_calls_runner_for_a_new_finding(self) -> None:
        verdict = classify_step(_obs(passed=False), None)
        outcome = process_nightly_step(verdict, dry_run=True)
        assert outcome.action == "would-file"

    def test_green_known_bug_fixed_comments_and_closes(self) -> None:
        verdict = classify_step(_obs(passed=True), "vimcode#1583")
        runner = FakeRunner()
        outcome = process_nightly_step(verdict, runner=runner, dry_run=False)
        assert outcome.action == "closed"
        assert outcome.issue_number == 1583
        assert runner.calls[0][:3] == ["issue", "comment", "vimcode"]
        assert runner.calls[0][3] == "1583"
        assert runner.calls[1] == ["issue", "close", "vimcode", "1583"]

    def test_green_known_bug_fixed_dry_run_never_calls_runner(self) -> None:
        verdict = classify_step(_obs(passed=True), "vimcode#1583")
        outcome = process_nightly_step(verdict, dry_run=True)
        assert outcome.action == "would-close"
        assert outcome.issue_number == 1583

    def test_alerting_step_without_runner_raises(self) -> None:
        verdict = classify_step(_obs(passed=False), None)
        with pytest.raises(ValueError, match="needs a runner"):
            process_nightly_step(verdict, dry_run=False)
