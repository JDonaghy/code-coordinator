"""Tests for coord/nightly_smoke.py — the #3652 nightly real-platform
smoke runner's pure decision core.

Four things are under test, matching the module docstring's four
"Wanted" items from #3652:

1. :func:`resolve_artifact_plan` — build-vs-download artifact resolution.
2. :func:`pick_nightly_host` — routing reuses `coord.smoke.
   rank_smoke_machines`, never a second capability matcher, and walks the
   full ranked list against each candidate's live `/health` probe rather
   than trusting a single head pick (#1672/#1678).
3. :func:`classify_step`/:func:`classify_nightly_run` — the
   #2096-disciplined verdict core (never classifies an unobserved result;
   a clean green and an expected-red known bug never alert).
4. :func:`process_nightly_step` — files/updates/closes exactly one issue
   per (spec, step), via an EXACT key match (never
   `coord.bugbash.dedupe_finding`'s fuzzy title-similarity scoring, which
   would fold two different steps of the same spec into one issue).
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


class _FakeHealthResp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeHealthClient:
    """Fake httpx client with PER-HOST `/health` responses (mirrors
    `tests/test_smoke.py`'s `_MultiHostClient`) — lets a test say exactly
    which machine's probe disagrees with its declared capabilities."""

    def __init__(self, health: dict[str, dict]) -> None:
        self._health = health
        self.get_calls: list[str] = []

    @staticmethod
    def _host(url: str) -> str:
        return url.split("//", 1)[1].split(":", 1)[0]

    def get(self, url, *, timeout) -> _FakeHealthResp:
        self.get_calls.append(url)
        return _FakeHealthResp(self._health.get(self._host(url), {}))


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

    def test_skips_a_candidate_whose_probe_contradicts_its_capability(self) -> None:
        """#1678/#1570 D, ported to the nightly runner (#3652 review): a
        single bad candidate must never end the whole routing attempt — the
        next capability-matched, probe-clean machine is tried instead."""
        config = Config(
            repos=[Repo(name="vimcode", github="acme/vimcode")],
            machines=[
                _machine("macmini-a", caps=["python", "macos"]),
                _machine("macmini-b", caps=["python", "macos"]),
            ],
            smoke_tests=SmokeTestsConfig(auto_queue=True),
        )
        client = _FakeHealthClient({
            "macmini-a.tail": {
                "tool_versions": {
                    "pyobjc-quartz": {
                        "found": False, "version": None, "min_version": None,
                        "meets_floor": None, "capability": "macos", "ok": False,
                    },
                },
            },
        })
        choice = pick_nightly_host(
            ["macos"], "vimcode", Board(), config, http_client=client,
        )
        assert choice is not None
        assert choice.machine.name == "macmini-b"
        assert client.get_calls == [
            "http://macmini-a.tail:7433/health",
            "http://macmini-b.tail:7433/health",
        ]

    def test_every_candidate_probe_failing_returns_none(self) -> None:
        config = Config(
            repos=[Repo(name="vimcode", github="acme/vimcode")],
            machines=[_machine("macmini-a", caps=["python", "macos"])],
            smoke_tests=SmokeTestsConfig(auto_queue=True),
        )
        client = _FakeHealthClient({
            "macmini-a.tail": {
                "tool_versions": {
                    "pyobjc-quartz": {
                        "found": False, "version": None, "min_version": None,
                        "meets_floor": None, "capability": "macos", "ok": False,
                    },
                },
            },
        })
        assert pick_nightly_host(
            ["macos"], "vimcode", Board(), config, http_client=client,
        ) is None


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

    def test_rejects_a_github_owner_repo_slug(self) -> None:
        """#3652 review: `coord issue comment|close REPO ...` resolves REPO
        against the LOCAL name under `repos:`, never a GitHub `owner/repo`
        slug — a spec author writing the slug must get a clear parse-time
        error, not a confusing runtime failure from `coord issue`."""
        with pytest.raises(ValueError, match="not a GitHub"):
            parse_known_bug_ref("acme/vimcode#1583")


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
        outcome = process_nightly_step(
            verdict, open_issues=[], closed_issues=[], dry_run=False,
        )
        assert outcome.action == "none"

    def test_expected_red_known_bug_never_calls_runner(self) -> None:
        verdict = classify_step(_obs(passed=False), "vimcode#1583")
        outcome = process_nightly_step(
            verdict, open_issues=[], closed_issues=[], dry_run=False,
        )
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

    def test_two_distinct_steps_of_the_same_spec_get_two_distinct_issues(self) -> None:
        """BLOCKING #3652 review finding: `coord.bugbash.dedupe_finding`'s
        fuzzy Jaccard title-similarity would score `install.yaml::launch`
        vs `install.yaml::uninstall` at 0.80 — comfortably above
        `DEFAULT_DEDUPE_THRESHOLD` — and fold step B's failure into step
        A's already-open issue. This asserts the LITERAL #3652 Wanted #2
        contract: "files or updates exactly one issue per (spec, step)",
        for two DIFFERENT steps of the SAME spec, both currently open with
        no prior issue for either."""
        launch = classify_step(
            _obs(
                spec="install.yaml", step="launch", passed=False,
                detail="window never appeared",
            ),
            None,
        )
        uninstall = classify_step(
            _obs(
                spec="install.yaml", step="uninstall", passed=False,
                detail="leaves files behind",
            ),
            None,
        )
        runner = FakeRunner(next_issue_number=500)

        outcome_a = process_nightly_step(
            launch, open_issues=[], closed_issues=[], lane=_lane(),
            runner=runner, dry_run=False,
        )
        assert outcome_a.action == "filed"
        assert outcome_a.issue_number == 500

        # Step B is classified/filed against a corpus that now includes
        # step A's freshly-filed issue — exactly what a real nightly run's
        # second step would see if it fetched open issues once up front.
        finding_a = finding_from_step(launch)
        open_issues_after_a = [{"number": 500, "title": finding_a.title}]
        outcome_b = process_nightly_step(
            uninstall, open_issues=open_issues_after_a, closed_issues=[],
            lane=_lane(), runner=runner, dry_run=False,
        )
        assert outcome_b.action == "filed"
        assert outcome_b.issue_number == 501
        assert outcome_b.issue_number != outcome_a.issue_number

    def test_regression_against_a_closed_issue_reopens_via_the_same_key(self) -> None:
        """The REGRESSION path (closed-issue exact-key match) through
        `process_nightly_step` — never exercised before this round."""
        verdict = classify_step(_obs(spec="install.yaml", step="launch", passed=False), None)
        finding = finding_from_step(verdict)
        closed_issues = [{"number": 42, "title": finding.title}]
        runner = FakeRunner(next_issue_number=900)
        outcome = process_nightly_step(
            verdict, open_issues=[], closed_issues=closed_issues, lane=_lane(),
            runner=runner, dry_run=False,
        )
        assert outcome.action == "filed"
        assert outcome.issue_number == 900
        assert runner.calls[0][:3] == ["issue", "create", "vimcode"]
        # The regression note names the closed issue it reopens.
        create_call = runner.calls[0]
        evidence_idx = create_call.index("--evidence") + 1
        assert "#42" in create_call[evidence_idx]

    def test_dry_run_never_calls_runner_for_a_new_finding(self) -> None:
        verdict = classify_step(_obs(passed=False), None)
        outcome = process_nightly_step(
            verdict, open_issues=[], closed_issues=[], dry_run=True,
        )
        assert outcome.action == "would-file"

    def test_green_known_bug_fixed_comments_and_closes(self) -> None:
        verdict = classify_step(_obs(passed=True), "vimcode#1583")
        runner = FakeRunner()
        outcome = process_nightly_step(
            verdict, open_issues=[], closed_issues=[], runner=runner, dry_run=False,
        )
        assert outcome.action == "closed"
        assert outcome.issue_number == 1583
        assert runner.calls[0][:3] == ["issue", "comment", "vimcode"]
        assert runner.calls[0][3] == "1583"
        assert runner.calls[1] == ["issue", "close", "vimcode", "1583"]

    def test_green_known_bug_fixed_dry_run_never_calls_runner(self) -> None:
        verdict = classify_step(_obs(passed=True), "vimcode#1583")
        outcome = process_nightly_step(
            verdict, open_issues=[], closed_issues=[], dry_run=True,
        )
        assert outcome.action == "would-close"
        assert outcome.issue_number == 1583

    def test_alerting_step_without_runner_raises(self) -> None:
        verdict = classify_step(_obs(passed=False), None)
        with pytest.raises(ValueError, match="needs a runner"):
            process_nightly_step(verdict, open_issues=[], closed_issues=[], dry_run=False)

    def test_malformed_known_bug_ref_raises_mid_flight(self) -> None:
        """#3652 review test gap: a malformed `known_bug` ref reaching
        `process_nightly_step` on the GREEN_KNOWN_BUG_FIXED path must
        raise from `parse_known_bug_ref`, not silently swallow the close
        — even though `classify_step` already committed to an alerting
        verdict before this point."""
        verdict = classify_step(_obs(passed=True), "not-a-valid-ref")
        with pytest.raises(ValueError, match="not '<repo>#<number>'"):
            process_nightly_step(
                verdict, open_issues=[], closed_issues=[], dry_run=False,
                runner=FakeRunner(),
            )

    def test_default_platform_comes_from_the_lane_not_a_constant(self) -> None:
        """#3652 review: a caller passing `lane=<some-platform lane>` with
        no explicit `platform=` must get an issue tagged for THAT lane's
        platform, not a careless constant default that would dedupe a
        macOS failure and a GTK-Linux failure of the identical step into
        one issue."""
        verdict = classify_step(_obs(passed=False, detail="crashed"), None)
        runner = FakeRunner(next_issue_number=700)
        gtk_lane = BugbashLane(
            platform="gtk-native", driver_kind="gtk-native", machine="deb-gtk",
            capability="gtk",
        )
        outcome = process_nightly_step(
            verdict, open_issues=[], closed_issues=[], lane=gtk_lane,
            runner=runner, dry_run=False,
        )
        assert outcome.action == "filed"
        create_call = runner.calls[0]
        title_idx = create_call.index("--title") + 1
        assert "[bugbash:gtk-native]" in create_call[title_idx]
