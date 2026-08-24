"""Tests for coord.ci_store (Phase 1 of #240).

The unit tests cover:
- Protocol + NoOpCi behaviour
- Helpers: failed_checks / in_flight_checks / summarize
- GitHubCi field mapping and caching
- Merge gate integration: failed/pending check blocks merge; --force-merge overrides
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from coord import github_ops
from coord.ci_github import GitHubCi
from coord.forge_availability import _flush_all_ok_aggregates
from coord.ci_store import (
    CheckRun,
    JobRun,
    JobStep,
    NoOpCi,
    build_ci_store,
    checks_are_stale,
    failed_checks,
    in_flight_checks,
    is_verdictless_job,
    summarize,
)


# Captured before the autouse fixture below ever patches it, so a test that
# needs the REAL caching/lookup behavior can restore it for just itself.
_REAL_REQUIRED_CONTEXTS = GitHubCi._required_contexts


@pytest.fixture(autouse=True)
def _no_required_contexts_filter(monkeypatch):
    """#2388: default every `GitHubCi` in this file to "couldn't determine
    required contexts, don't filter" — every test below that mocks raw
    `gh` subprocess calls does so with an exact expected call sequence, and
    the required-contexts lookup (a repo lookup + a branch-protection
    lookup) would otherwise insert two extra calls into that sequence for
    tests that never intended to exercise it. `TestGitHubCiRequiredContexts`
    below overrides this directly to exercise the real filtering path.
    """
    monkeypatch.setattr(GitHubCi, "_required_contexts", lambda self, repo: None)


# ── NoOpCi ───────────────────────────────────────────────────────────────────

class TestNoOpCi:
    def test_is_not_available(self) -> None:
        assert NoOpCi().is_available is False

    def test_returns_empty(self) -> None:
        assert NoOpCi().list_checks_for_pr("acme/api", 1) == []

    def test_list_all_checks_for_pr_returns_empty(self) -> None:
        """#2446: the unfiltered visibility view is just as much a no-op as
        the gate view when CI gating is opted out entirely."""
        assert NoOpCi().list_all_checks_for_pr("acme/api", 1) == []

    def test_rerun_for_pr_is_a_noop(self) -> None:
        """#1851: `ci_store: { type: none }` disables CI gating entirely —
        rerun_for_pr must not pretend to do anything."""
        assert NoOpCi().rerun_for_pr("acme/api", 1) is False

    def test_rerun_failed_for_pr_is_a_noop(self) -> None:
        """#2252: same opt-out for the narrower failed-jobs-only rerun."""
        assert NoOpCi().rerun_failed_for_pr("acme/api", 1) is False

    def test_expects_checks_is_false(self) -> None:
        """#1904: `ci_store: { type: none }` is the supported "this repo has
        no CI" opt-out — an empty check list from `NoOpCi` must never read
        as `checks_absent`."""
        assert NoOpCi().expects_checks("acme/api", 1) is False


# ── Helpers ──────────────────────────────────────────────────────────────────

def _check(
    name: str,
    status: str = "completed",
    conclusion: str | None = "success",
    started_at: float | None = None,
) -> CheckRun:
    return CheckRun(
        name=name, status=status, conclusion=conclusion,
        url=f"https://gh/runs/{name}", run_id=name,
        started_at=started_at, completed_at=None,
    )


class TestFailedChecks:
    def test_picks_failure(self) -> None:
        items = [_check("a"), _check("b", conclusion="failure"), _check("c")]
        assert [x.name for x in failed_checks(items)] == ["b"]

    def test_picks_cancelled_and_timed_out_and_action_required(self) -> None:
        items = [
            _check("a", conclusion="cancelled"),
            _check("b", conclusion="timed_out"),
            _check("c", conclusion="action_required"),
            _check("ok"),
        ]
        names = {x.name for x in failed_checks(items)}
        assert names == {"a", "b", "c"}

    def test_skipped_is_not_failed(self) -> None:
        assert failed_checks([_check("a", conclusion="skipped")]) == []

    def test_neutral_is_not_failed(self) -> None:
        assert failed_checks([_check("a", conclusion="neutral")]) == []

    def test_stale_is_failed(self) -> None:
        """#1525: allow-list, not deny-list — GitHub's `stale` conclusion
        (superseded by a newer run) wasn't in the old deny-list at all and
        would have silently passed."""
        assert [c.name for c in failed_checks([_check("a", conclusion="stale")])] == ["a"]

    def test_unrecognised_conclusion_is_failed(self) -> None:
        """#1525: a conclusion this codebase has never seen (a future GitHub
        addition, or the synthetic "unknown" ci_github.py emits on a read
        failure) must default to blocking, not passing."""
        assert [
            c.name for c in failed_checks([_check("a", conclusion="something_new")])
        ] == ["a"]

    def test_in_flight_check_is_not_failed(self) -> None:
        """A queued/running check has conclusion=None and must be classified
        by in_flight_checks, never counted as failed here."""
        items = [_check("a", status="in_progress", conclusion=None)]
        assert failed_checks(items) == []


class TestInFlightChecks:
    def test_picks_queued_and_running(self) -> None:
        items = [
            _check("a", status="queued", conclusion=None),
            _check("b", status="in_progress", conclusion=None),
            _check("c"),
        ]
        names = {x.name for x in in_flight_checks(items)}
        assert names == {"a", "b"}


class _FakeClock:
    """Deterministic (clock, sleep) pair for `wait_for_ci_settle` tests
    (#1925) — `sleep` advances the same counter `clock` reads, so a bounded
    poll loop runs to completion instantly with no real wall-clock wait."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _ScriptedCi:
    """`list_checks_for_pr` returns each item of *script* in turn (repeating
    the last one once exhausted); `invalidate` is a no-op spy."""

    def __init__(self, script: list[list]) -> None:
        self.script = script
        self.calls = 0
        self.invalidated: list[tuple] = []

    def list_checks_for_pr(self, repo, number):
        i = min(self.calls, len(self.script) - 1)
        self.calls += 1
        return self.script[i]

    def invalidate(self, repo, number):
        self.invalidated.append((repo, number))


def _unreadable(detail: str = "gh pr checks failed") -> CheckRun:
    return CheckRun(
        name=f"coord: could not read CI status for acme/api#1 ({detail})",
        status="completed", conclusion="unknown",
        url="", run_id="", started_at=None, completed_at=None,
    )


class TestWaitForCiSettle:
    """#1925: `--revalidate`'s CI arm triggers a re-run and then used to
    evaluate the merge gate before the new run had registered on GitHub —
    `gh pr checks` errors in that window, which reads as the #1525 synthetic
    `unknown` conclusion and fail-closes on a condition the command just
    created. `wait_for_ci_settle` is the bounded poll that closes the gap."""

    def test_already_settled_returns_immediately_no_sleep(self) -> None:
        from coord.ci_store import wait_for_ci_settle

        ci = _ScriptedCi([[_check("build")]])
        clk = _FakeClock()

        result = wait_for_ci_settle(
            ci, "acme/api", 1, sleep=clk.sleep, clock=clk.clock,
        )

        assert result.settled is True
        assert result.registering is False
        assert clk.sleeps == []
        assert ci.invalidated == [("acme/api", 1)]

    def test_registering_then_settles_green(self) -> None:
        """The exact #1925 shape: the first read(s) hit the registration
        gap (synthetic unreadable check), then the real run shows up green.
        Must NOT be reported as settled until the real check appears."""
        from coord.ci_store import wait_for_ci_settle

        ci = _ScriptedCi([
            [_unreadable()],
            [_unreadable()],
            [_check("build", conclusion="success")],
        ])
        clk = _FakeClock()

        result = wait_for_ci_settle(
            ci, "acme/api", 1, sleep=clk.sleep, clock=clk.clock,
        )

        assert result.settled is True
        assert result.registering is False
        assert [c.conclusion for c in result.checks] == ["success"]
        assert len(clk.sleeps) == 2  # polled past the two unreadable reads

    def test_registering_then_settles_red_is_still_a_real_result(self) -> None:
        """A genuinely failing composed re-run must settle as a real failure
        — never laundered into a pass, and never left stuck on 'registering'
        just because it's bad news."""
        from coord.ci_store import wait_for_ci_settle

        ci = _ScriptedCi([
            [_unreadable()],
            [_check("build", conclusion="failure")],
        ])
        clk = _FakeClock()

        result = wait_for_ci_settle(
            ci, "acme/api", 1, sleep=clk.sleep, clock=clk.clock,
        )

        assert result.settled is True
        assert [c.conclusion for c in result.checks] == ["failure"]

    def test_never_registers_times_out_as_registering(self) -> None:
        """A re-run that never registers within the budget must NOT be
        confused with a genuinely in-flight real check — `registering=True`
        is the signal the caller uses to defer rather than block."""
        from coord.ci_store import wait_for_ci_settle

        ci = _ScriptedCi([[_unreadable()]])
        clk = _FakeClock()

        result = wait_for_ci_settle(
            ci, "acme/api", 1, timeout=30.0, poll_interval=10.0,
            sleep=clk.sleep, clock=clk.clock,
        )

        assert result.settled is False
        assert result.registering is True
        assert result.waited_seconds >= 30.0

    def test_genuinely_in_flight_real_check_times_out_as_not_registering(
        self,
    ) -> None:
        """A real check that's simply still running when the budget runs
        out is an honest 'CI running' — distinct from the registration gap,
        so the caller must NOT treat it as self-inflicted churn."""
        from coord.ci_store import wait_for_ci_settle

        ci = _ScriptedCi([[_check("build", status="in_progress", conclusion=None)]])
        clk = _FakeClock()

        result = wait_for_ci_settle(
            ci, "acme/api", 1, timeout=20.0, poll_interval=10.0,
            sleep=clk.sleep, clock=clk.clock,
        )

        assert result.settled is False
        assert result.registering is False

    def test_empty_checks_counts_as_registering(self) -> None:
        from coord.ci_store import wait_for_ci_settle

        ci = _ScriptedCi([[], [_check("build")]])
        clk = _FakeClock()

        result = wait_for_ci_settle(
            ci, "acme/api", 1, sleep=clk.sleep, clock=clk.clock,
        )

        assert result.settled is True
        assert len(clk.sleeps) == 1

    def test_missing_invalidate_is_tolerated(self) -> None:
        """A CiStore stub with no `invalidate` method (most test fakes, and
        `NoOpCi`) must not raise — `getattr(..., None)` guards it."""
        from coord.ci_store import wait_for_ci_settle

        class _NoInvalidate:
            def list_checks_for_pr(self, repo, number):
                return [_check("build")]

        result = wait_for_ci_settle(_NoInvalidate(), "acme/api", 1)
        assert result.settled is True


class TestChecksAreStale:
    """#1851: a green check whose `started_at` predates the base's newest
    commit is stale — GitHub only re-runs `pull_request` checks on head
    `synchronize`, never on base movement."""

    def test_no_checks_is_not_stale(self) -> None:
        """Nothing to compare — the caller's own "no checks" handling covers
        this, not staleness."""
        assert checks_are_stale([], 1000.0) is False

    def test_check_started_before_base_commit_is_stale(self) -> None:
        checks = [_check("ci", started_at=500.0)]
        assert checks_are_stale(checks, 1000.0) is True

    def test_check_started_after_base_commit_is_fresh(self) -> None:
        checks = [_check("ci", started_at=1500.0)]
        assert checks_are_stale(checks, 1000.0) is False

    def test_one_stale_check_among_fresh_ones_is_stale(self) -> None:
        """Bias toward stale: ANY check predating the base commit is enough,
        even if its siblings are fresh."""
        checks = [_check("a", started_at=1500.0), _check("b", started_at=500.0)]
        assert checks_are_stale(checks, 1000.0) is True

    def test_unreadable_base_commit_time_is_stale(self) -> None:
        """Fail-closed path 1: base_commit_time is None (unreadable / no
        gh_ops capability to ask)."""
        checks = [_check("ci", started_at=1500.0)]
        assert checks_are_stale(checks, None) is True

    def test_missing_started_at_is_stale(self) -> None:
        """Fail-closed path 2: the check itself carries no started_at, even
        though a base_commit_time is known."""
        checks = [_check("ci", started_at=None)]
        assert checks_are_stale(checks, 1000.0) is True


# ── is_verdictless_job (#1892) ────────────────────────────────────────────────
#
# Fixtures below are lifted verbatim (trimmed to the fields the classifier
# reads) from the two real signatures the issue documents:
# JDonaghy/claude-coordinator run 31117792472 attempt 2 (the "died at Set up
# job" job, `e2e`) and JDonaghy/vimcode run 31119463000 attempt 2 (the
# "never assigned a runner" job, `Test (Linux, headless)`).

def _never_assigned_a_runner() -> JobRun:
    """GitHub's own shape for a job cancelled at the queue timeout: a job
    record DOES exist, but with an empty runner and zero steps."""
    return JobRun(name="e2e", conclusion="cancelled", runner_name="", steps=[])


def _died_before_checkout() -> JobRun:
    """GitHub's own shape for a job that got a runner but died setting it
    up: exactly one step, named literally "Set up job", non-passing."""
    return JobRun(
        name="e2e", conclusion="failure", runner_name="GitHub Actions 1000009736",
        steps=[JobStep(name="Set up job", conclusion="failure")],
    )


def _ran_and_failed() -> JobRun:
    """A real failure: got a runner, ran past "Set up job", and a LATER
    step failed — this carries a verdict about the code."""
    return JobRun(
        name="e2e", conclusion="failure", runner_name="GitHub Actions 1000009718",
        steps=[
            JobStep(name="Set up job", conclusion="success"),
            JobStep(name="Run actions/checkout@v4", conclusion="success"),
            JobStep(name="Run pytest", conclusion="failure"),
        ],
    )


class TestIsVerdictlessJob:
    def test_never_assigned_a_runner_is_verdictless(self) -> None:
        check = _check("e2e", conclusion="cancelled")
        assert is_verdictless_job(check, _never_assigned_a_runner()) is True

    def test_died_before_checkout_is_verdictless(self) -> None:
        check = _check("e2e", conclusion="failure")
        assert is_verdictless_job(check, _died_before_checkout()) is True

    def test_a_real_failure_carries_a_verdict(self) -> None:
        """The hazard the issue warns against: this must NOT be laundered
        into "infrastructure" just because it also has a runner and steps."""
        check = _check("e2e", conclusion="failure")
        assert is_verdictless_job(check, _ran_and_failed()) is False

    def test_cancelled_with_steps_recorded_carries_a_verdict(self) -> None:
        """Only EXACTLY zero steps is the "never started" signature — a
        cancelled job that got partway through is a different story."""
        check = _check("e2e", conclusion="cancelled")
        job = JobRun(
            name="e2e", conclusion="cancelled", runner_name="GitHub Actions 1",
            steps=[JobStep(name="Set up job", conclusion="success")],
        )
        assert is_verdictless_job(check, job) is False

    def test_failure_with_a_second_failed_step_carries_a_verdict(self) -> None:
        """Real-world third shape (webapp-types, run 31123113788 attempt 1):
        a job with a runner assigned, conclusion=failure, but ZERO recorded
        steps — the runner itself likely died mid-run. This is neither
        documented signature (not cancelled; no "Set up job" step at all),
        so per the false-negative bias it must read as carrying a verdict,
        not as infrastructure noise."""
        check = _check("webapp-types", conclusion="failure")
        job = JobRun(
            name="webapp-types", conclusion="failure",
            runner_name="GitHub Actions 1000009756", steps=[],
        )
        assert is_verdictless_job(check, job) is False

    def test_no_job_data_carries_a_verdict(self) -> None:
        """No matching job (unmatched name, or CiStore.list_jobs_for_run
        failed/returned nothing) must default to "carries a verdict" — the
        safe false-negative direction, never the reverse."""
        check = _check("e2e", conclusion="cancelled")
        assert is_verdictless_job(check, None) is False

    def test_in_flight_check_carries_a_verdict(self) -> None:
        """Only a completed check is ever asked about — an in-flight one
        must never be misread as verdictless."""
        check = _check("e2e", status="in_progress", conclusion=None)
        assert is_verdictless_job(check, _never_assigned_a_runner()) is False

    def test_success_conclusion_is_not_relevant_here(self) -> None:
        """Not a signature this function is meant to see in practice
        (callers only ask about `failed_checks` output) but a defensive
        check: a passing check with an empty-steps job must not read as
        verdictless — the "cancelled" branch is conclusion-specific."""
        check = _check("e2e", conclusion="success")
        assert is_verdictless_job(check, _never_assigned_a_runner()) is False


class TestSummarize:
    def test_empty(self) -> None:
        assert summarize([]) == "no checks"

    def test_mixed(self) -> None:
        items = [
            _check("ok"),
            _check("bad", conclusion="failure"),
            _check("wip", status="in_progress", conclusion=None),
        ]
        s = summarize(items)
        assert "1✓" in s
        assert "1✗" in s
        assert "1⋯" in s


# ── build_ci_store ───────────────────────────────────────────────────────────

class TestBuildCiStore:
    def test_github(self) -> None:
        store = build_ci_store("github")
        assert isinstance(store, GitHubCi)
        assert store.is_available is True

    def test_none(self) -> None:
        store = build_ci_store("none")
        assert isinstance(store, NoOpCi)
        assert store.is_available is False

    def test_unknown_falls_back_to_noop(self) -> None:
        # A typo in coordinator.yml shouldn't crash the merge command.
        store = build_ci_store("buildkite-but-misspelled")
        assert isinstance(store, NoOpCi)


# ── GitHubCi backend (subprocess mocked) ─────────────────────────────────────

def _gh_result(
    stdout: str = "[]", returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# #1564: this is the *real* shape `gh pr checks --json name,state,bucket,
# link,startedAt,completedAt` returns — no `conclusion` field, and `state`
# is a verdict (SUCCESS/FAILURE/...), not a lifecycle phase. `bucket` is
# gh's own pass/fail/pending rollup and is what GitHubCi now keys off.
GH_SAMPLE = json.dumps([
    {
        "name": "test (3.12)",
        "state": "FAILURE",
        "bucket": "fail",
        "link": "https://github.com/acme/api/actions/runs/123/job/456",
        "startedAt": "2026-05-24T12:00:00Z",
        "completedAt": "2026-05-24T12:05:00Z",
    },
    {
        "name": "lint",
        "state": "SUCCESS",
        "bucket": "pass",
        "link": "",
        "startedAt": "",
        "completedAt": "",
    },
    {
        "name": "deploy-preview",
        "state": "PENDING",
        "bucket": "pending",
        "link": "https://github.com/acme/api/actions/runs/789",
        "startedAt": "2026-05-24T12:10:00Z",
        "completedAt": "",
    },
])


class TestGitHubCi:
    def test_maps_fields(self) -> None:
        store = GitHubCi()
        with patch("coord.ci_github.subprocess.run", return_value=_gh_result(GH_SAMPLE)):
            checks = store.list_checks_for_pr("acme/api", 42)
        assert len(checks) == 3
        by_name = {c.name: c for c in checks}
        assert by_name["test (3.12)"].status == "completed"
        assert by_name["test (3.12)"].conclusion == "failure"
        assert by_name["test (3.12)"].url.endswith("/job/456")
        assert by_name["lint"].conclusion == "success"
        assert by_name["deploy-preview"].status == "in_progress"
        assert by_name["deploy-preview"].conclusion is None
        # Timestamps are parsed to floats when present.
        assert isinstance(by_name["test (3.12)"].started_at, float)
        assert by_name["lint"].started_at is None

    def test_real_gh_shape_all_pass_yields_zero_failed_and_zero_inflight(self) -> None:
        """#1564 addendum acceptance test: feed exactly the JSON shape a real
        `gh pr checks --json name,state,bucket,...` call returns for an
        all-green PR (no `conclusion` field at all) through GitHubCi and
        confirm it reads as green — the pre-fix code failed this on both
        counts (every check normalised to "in_progress" forever)."""
        payload = json.dumps([
            {
                "name": "test (3.13)", "state": "SUCCESS", "bucket": "pass",
                "link": "https://github.com/acme/api/actions/runs/1/job/1",
                "startedAt": "2026-07-28T00:00:00Z", "completedAt": "2026-07-28T00:01:00Z",
            },
            {
                "name": "e2e", "state": "SUCCESS", "bucket": "pass",
                "link": "https://github.com/acme/api/actions/runs/1/job/2",
                "startedAt": "2026-07-28T00:00:00Z", "completedAt": "2026-07-28T00:01:00Z",
            },
        ])
        store = GitHubCi()
        with patch("coord.ci_github.subprocess.run", return_value=_gh_result(payload)):
            checks = store.list_checks_for_pr("acme/api", 1562)
        assert failed_checks(checks) == []
        assert in_flight_checks(checks) == []

    def test_bucket_maps_to_conclusion_and_status(self) -> None:
        """#1564: gh's documented buckets (pass/fail/pending/skipping/cancel)
        map to CheckRun's status/conclusion — this is the mapping the merge
        gate actually reads."""
        payload = json.dumps([
            {"name": "a", "state": "SUCCESS", "bucket": "pass",
             "link": "", "startedAt": "", "completedAt": ""},
            {"name": "b", "state": "FAILURE", "bucket": "fail",
             "link": "", "startedAt": "", "completedAt": ""},
            {"name": "c", "state": "SKIPPED", "bucket": "skipping",
             "link": "", "startedAt": "", "completedAt": ""},
            {"name": "d", "state": "CANCELLED", "bucket": "cancel",
             "link": "", "startedAt": "", "completedAt": ""},
            {"name": "e", "state": "PENDING", "bucket": "pending",
             "link": "", "startedAt": "", "completedAt": ""},
        ])
        store = GitHubCi()
        with patch("coord.ci_github.subprocess.run", return_value=_gh_result(payload)):
            checks = store.list_checks_for_pr("acme/api", 1)
        by_name = {c.name: c for c in checks}
        assert (by_name["a"].status, by_name["a"].conclusion) == ("completed", "success")
        assert (by_name["b"].status, by_name["b"].conclusion) == ("completed", "failure")
        assert (by_name["c"].status, by_name["c"].conclusion) == ("completed", "skipped")
        assert (by_name["d"].status, by_name["d"].conclusion) == ("completed", "cancelled")
        assert (by_name["e"].status, by_name["e"].conclusion) == ("in_progress", None)

    def test_unrecognised_bucket_is_unknown_not_passing(self) -> None:
        """#1525's fail-closed rule extended to `bucket`: a future gh bucket
        value this code has never seen must not be silently read as passing."""
        payload = json.dumps([
            {"name": "weird", "state": "SOMETHING_NEW", "bucket": "mystery",
             "link": "", "startedAt": "", "completedAt": ""},
        ])
        store = GitHubCi()
        with patch("coord.ci_github.subprocess.run", return_value=_gh_result(payload)):
            checks = store.list_checks_for_pr("acme/api", 1)
        assert checks[0].status == "completed"
        assert checks[0].conclusion == "unknown"
        assert failed_checks(checks) == checks

    def test_handles_failing_gh_with_valid_json(self) -> None:
        """gh exits non-zero when checks fail but stdout is still valid JSON."""
        store = GitHubCi()
        with patch(
            "coord.ci_github.subprocess.run",
            return_value=_gh_result(GH_SAMPLE, returncode=1),
        ):
            checks = store.list_checks_for_pr("acme/api", 42)
        assert len(checks) == 3

    def test_gh_too_old_for_json_flag_yields_distinct_actionable_check(self) -> None:
        """#1564 Addendum 2: dellserver's gh 2.45.0 doesn't support `--json`
        on `pr checks` at all — confirmed real-world shape is `unknown flag:
        --json`, rc=1, empty stdout. That must fail closed (same as any
        other unreadable-CI read failure) but be surfaced *distinctly* —
        naming the version floor and gh's actual version — instead of the
        same undiagnosable "could not read CI status" text used for
        auth/network flakes.
        """
        store = GitHubCi()
        checks_result = _gh_result("", returncode=1, stderr="unknown flag: --json")
        version_result = _gh_result("gh version 2.45.0 (2024-01-01)\n")
        with patch(
            "coord.ci_github.subprocess.run",
            side_effect=[checks_result, version_result],
        ):
            checks = store.list_checks_for_pr("acme/api", 42)
        assert len(checks) == 1
        assert checks[0].conclusion == "unknown"
        assert failed_checks(checks) == checks  # still fails closed, #1525
        assert "2.45.0" in checks[0].name
        assert github_ops.GH_PR_CHECKS_JSON_MIN_VERSION in checks[0].name
        # Distinguishable from the generic unreadable-check wording so an
        # operator never has to guess which of the two this was.
        assert "could not read CI status" not in checks[0].name

    def test_handles_missing_gh(self) -> None:
        # #1525: a read failure must fail CLOSED — a synthetic "unknown"
        # check, not an empty list indistinguishable from "no checks
        # configured". `failed_checks` must pick it up as a hard failure.
        store = GitHubCi()
        with patch("coord.ci_github.subprocess.run", side_effect=FileNotFoundError):
            checks = store.list_checks_for_pr("acme/api", 42)
        assert len(checks) == 1
        assert checks[0].conclusion == "unknown"
        assert failed_checks(checks) == checks

    def test_handles_timeout(self) -> None:
        store = GitHubCi()
        with patch(
            "coord.ci_github.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=30),
        ):
            checks = store.list_checks_for_pr("acme/api", 42)
        assert len(checks) == 1
        assert checks[0].conclusion == "unknown"
        assert failed_checks(checks) == checks

    def test_handles_invalid_json(self) -> None:
        store = GitHubCi()
        with patch(
            "coord.ci_github.subprocess.run",
            return_value=_gh_result("not json", returncode=0),
        ):
            checks = store.list_checks_for_pr("acme/api", 42)
        assert len(checks) == 1
        assert checks[0].conclusion == "unknown"
        assert failed_checks(checks) == checks

    def test_handles_non_list_json(self) -> None:
        """Valid JSON that isn't a list (e.g. an error object) also fails closed."""
        store = GitHubCi()
        with patch(
            "coord.ci_github.subprocess.run",
            return_value=_gh_result('{"error": "rate limited"}', returncode=0),
        ):
            checks = store.list_checks_for_pr("acme/api", 42)
        assert len(checks) == 1
        assert checks[0].conclusion == "unknown"

    def test_cache_avoids_second_call(self) -> None:
        store = GitHubCi(cache_ttl=60.0)
        with patch(
            "coord.ci_github.subprocess.run",
            return_value=_gh_result(GH_SAMPLE),
        ) as run:
            store.list_checks_for_pr("acme/api", 42)
            store.list_checks_for_pr("acme/api", 42)
        assert run.call_count == 1

    def test_cache_invalidate(self) -> None:
        store = GitHubCi(cache_ttl=60.0)
        with patch(
            "coord.ci_github.subprocess.run",
            return_value=_gh_result(GH_SAMPLE),
        ) as run:
            store.list_checks_for_pr("acme/api", 42)
            store.invalidate()
            store.list_checks_for_pr("acme/api", 42)
        assert run.call_count == 2

    def test_cache_keyed_per_pr(self) -> None:
        store = GitHubCi(cache_ttl=60.0)
        with patch(
            "coord.ci_github.subprocess.run",
            return_value=_gh_result(GH_SAMPLE),
        ) as run:
            store.list_checks_for_pr("acme/api", 42)
            store.list_checks_for_pr("acme/api", 43)  # different PR
        assert run.call_count == 2

    def test_run_id_extracted_from_job_link(self) -> None:
        """#1851: a job-shaped link (".../runs/{run_id}/job/{job_id}") must
        yield the *run* id, not the trailing job id — `gh run rerun` takes a
        run id. Pre-#1851 this field took the last path segment (the job
        id); nothing read `run_id` before #1851 (see `coord/ci_store.py`'s
        Phase 1 header), so this is a fix, not a behaviour change any caller
        depended on."""
        store = GitHubCi()
        with patch("coord.ci_github.subprocess.run", return_value=_gh_result(GH_SAMPLE)):
            checks = store.list_checks_for_pr("acme/api", 42)
        by_name = {c.name: c for c in checks}
        assert by_name["test (3.12)"].run_id == "123"

    def test_run_id_extracted_from_bare_run_link(self) -> None:
        store = GitHubCi()
        with patch("coord.ci_github.subprocess.run", return_value=_gh_result(GH_SAMPLE)):
            checks = store.list_checks_for_pr("acme/api", 42)
        by_name = {c.name: c for c in checks}
        assert by_name["deploy-preview"].run_id == "789"

    def test_run_id_empty_when_link_empty(self) -> None:
        store = GitHubCi()
        with patch("coord.ci_github.subprocess.run", return_value=_gh_result(GH_SAMPLE)):
            checks = store.list_checks_for_pr("acme/api", 42)
        by_name = {c.name: c for c in checks}
        assert by_name["lint"].run_id == ""


class TestGitHubCiForgeAvailabilityRecording:
    """#1896 Phase 0: `GitHubCi._fetch` records one forge-availability
    observation per LIVE (cache-miss) ``gh pr checks`` read — reachable/
    unreachable, plus the check-level conclusion distribution."""

    @staticmethod
    def _rows(coord_db) -> list:
        return coord_db.execute(
            "SELECT * FROM audit_log WHERE category='forge_availability' "
            "AND event_type='ci_check_fetch' ORDER BY id"
        ).fetchall()

    def test_successful_fetch_records_ok_with_conclusion_distribution(
        self, coord_db
    ) -> None:
        store = GitHubCi()
        with patch("coord.ci_github.subprocess.run", return_value=_gh_result(GH_SAMPLE)):
            store.list_checks_for_pr("acme/api", 42)
        _flush_all_ok_aggregates()  # #2654: "ok" observations buffer until flushed

        rows = self._rows(coord_db)
        assert len(rows) == 1
        assert rows[0]["repo"] == "acme/api"
        assert rows[0]["issue"] == 42
        details = json.loads(rows[0]["details_json"])
        assert details["outcome"] == "ok"
        # GH_SAMPLE: one failure, one success, one pending (bucket "pending"
        # -> conclusion None -> bucketed under "pending" in the distribution).
        assert details["conclusions"] == {"failure": 1, "success": 1, "pending": 1}

    def test_cached_hit_records_nothing_extra(self, coord_db) -> None:
        """A cache hit never calls `gh` again, so it must not manufacture a
        second observation either — that would overstate real forge traffic."""
        store = GitHubCi(cache_ttl=60.0)
        with patch("coord.ci_github.subprocess.run", return_value=_gh_result(GH_SAMPLE)):
            store.list_checks_for_pr("acme/api", 42)
            store.list_checks_for_pr("acme/api", 42)  # cache hit
        _flush_all_ok_aggregates()  # #2654: "ok" observations buffer until flushed

        assert len(self._rows(coord_db)) == 1

    def test_unreadable_fetch_records_unreachable(self, coord_db) -> None:
        store = GitHubCi()
        with patch(
            "coord.ci_github.subprocess.run",
            return_value=_gh_result(stdout="", returncode=1, stderr="HTTP 503"),
        ):
            store.list_checks_for_pr("acme/api", 42)

        rows = self._rows(coord_db)
        assert len(rows) == 1
        assert json.loads(rows[0]["details_json"])["outcome"] == "unreachable"

    def test_recording_failure_never_breaks_a_real_fetch(self, coord_db, monkeypatch) -> None:
        """Acceptance bar: a store that always throws must never raise into
        `list_checks_for_pr`'s caller."""
        monkeypatch.setattr(
            "coord.forge_availability.record_audit",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        store = GitHubCi()
        with patch("coord.ci_github.subprocess.run", return_value=_gh_result(GH_SAMPLE)):
            checks = store.list_checks_for_pr("acme/api", 42)  # must not raise
        assert len(checks) == 3


class TestGitHubCiExpectsChecks:
    """#1904: `GitHubCi.expects_checks` — the signal that tells "no CI
    configured for this repo" apart from "CI exists but never triggered"
    when `list_checks_for_pr` comes back empty."""

    def test_true_when_repo_declares_workflows(self) -> None:
        store = GitHubCi()
        with patch("coord.github_ops.get_repo_workflow_count", return_value=3):
            assert store.expects_checks("acme/api", 42) is True

    def test_false_when_repo_has_no_workflows(self) -> None:
        store = GitHubCi()
        with patch("coord.github_ops.get_repo_workflow_count", return_value=0):
            assert store.expects_checks("acme/api", 42) is False

    def test_fails_closed_on_read_error(self) -> None:
        """An unreadable `gh api .../actions/workflows` call must default to
        `True` (checks were expected), not `False` — #1525's rule that an
        unknown reads as blocking, not as a free pass."""
        store = GitHubCi()
        with patch(
            "coord.github_ops.get_repo_workflow_count",
            side_effect=RuntimeError("gh: authentication required"),
        ):
            assert store.expects_checks("acme/api", 42) is True

    def test_cached_per_repo_not_per_pr(self) -> None:
        """Workflow declarations are repo-wide — two different PRs in the
        same repo must share one cached answer, not pay a `gh api` round
        trip each."""
        store = GitHubCi(cache_ttl=60.0)
        with patch(
            "coord.github_ops.get_repo_workflow_count", return_value=1
        ) as fn:
            store.expects_checks("acme/api", 42)
            store.expects_checks("acme/api", 43)
        assert fn.call_count == 1

    def test_cache_keyed_per_repo(self) -> None:
        store = GitHubCi(cache_ttl=60.0)
        with patch(
            "coord.github_ops.get_repo_workflow_count", return_value=1
        ) as fn:
            store.expects_checks("acme/api", 42)
            store.expects_checks("acme/ui", 42)  # different repo
        assert fn.call_count == 2


class TestGitHubCiRequiredContexts:
    """#2388: `list_checks_for_pr` narrows to branch-protection-required
    contexts when they're determinable, so an advisory job (e.g. a hung,
    unconditional Playwright/Chromium install with no timeout) can't block
    the merge gate on its own — GitHub itself doesn't wait on it either."""

    def test_narrows_to_required_contexts_when_known(self) -> None:
        payload = json.dumps([
            {"name": "test (3.12)", "state": "SUCCESS", "bucket": "pass",
             "link": "", "startedAt": "", "completedAt": ""},
            {"name": "acceptance", "state": "PENDING", "bucket": "pending",
             "link": "", "startedAt": "", "completedAt": ""},
        ])
        store = GitHubCi()
        with (
            patch("coord.ci_github.subprocess.run", return_value=_gh_result(payload)),
            patch.object(
                GitHubCi, "_required_contexts",
                lambda self, repo: frozenset({"test (3.12)"}),
            ),
        ):
            checks = store.list_checks_for_pr("acme/api", 42)
        assert [c.name for c in checks] == ["test (3.12)"]
        # The gate no longer sees the advisory `acceptance` check at all —
        # a hung advisory job can't make this read as in-flight.
        assert in_flight_checks(checks) == []

    def test_does_not_filter_when_required_contexts_unknown(self) -> None:
        """No branch protection configured / unreadable — #1525's bias:
        unknown must read as "wait on everything reported", not as license
        to stop waiting on something that might in fact be required."""
        payload = json.dumps([
            {"name": "test (3.12)", "state": "SUCCESS", "bucket": "pass",
             "link": "", "startedAt": "", "completedAt": ""},
            {"name": "acceptance", "state": "PENDING", "bucket": "pending",
             "link": "", "startedAt": "", "completedAt": ""},
        ])
        store = GitHubCi()
        with patch("coord.ci_github.subprocess.run", return_value=_gh_result(payload)):
            checks = store.list_checks_for_pr("acme/api", 42)  # fixture default: None
        assert {c.name for c in checks} == {"test (3.12)", "acceptance"}
        assert len(in_flight_checks(checks)) == 1

    def test_required_contexts_cached_per_repo(self, monkeypatch) -> None:
        monkeypatch.setattr(GitHubCi, "_required_contexts", _REAL_REQUIRED_CONTEXTS)
        store = GitHubCi(cache_ttl=60.0)
        with patch(
            "coord.github_ops.get_required_status_check_contexts",
            return_value=["test (3.12)"],
        ) as fn:
            store._required_contexts("acme/api")
            store._required_contexts("acme/api")
        assert fn.call_count == 1

    def test_empty_required_list_reads_as_unknown_not_nothing_required(
        self, monkeypatch,
    ) -> None:
        """An empty `contexts` list from the API (protection configured but
        zero required checks — an unusual but real repo state) must not be
        read as "filter everything out". Only a non-empty list narrows."""
        monkeypatch.setattr(GitHubCi, "_required_contexts", _REAL_REQUIRED_CONTEXTS)
        store = GitHubCi()
        with patch(
            "coord.github_ops.get_required_status_check_contexts", return_value=[],
        ):
            assert store._required_contexts("acme/api") is None


class TestGitHubCiListAllChecksForPr:
    """#2446: `list_all_checks_for_pr` is the unfiltered counterpart to
    `list_checks_for_pr` — required + advisory both, backing `coord merge
    --plan`'s visibility so a regressed advisory check (e.g. a hung/flaky
    `Acceptance (web)`) is still visible even though the merge gate itself
    (`list_checks_for_pr`) correctly no longer waits on it."""

    def test_returns_advisory_checks_the_gate_filters_out(self) -> None:
        payload = json.dumps([
            {"name": "test (3.12)", "state": "SUCCESS", "bucket": "pass",
             "link": "", "startedAt": "", "completedAt": ""},
            {"name": "acceptance", "state": "PENDING", "bucket": "pending",
             "link": "", "startedAt": "", "completedAt": ""},
        ])
        store = GitHubCi()
        with (
            patch("coord.ci_github.subprocess.run", return_value=_gh_result(payload)),
            patch.object(
                GitHubCi, "_required_contexts",
                lambda self, repo: frozenset({"test (3.12)"}),
            ),
        ):
            gate_checks = store.list_checks_for_pr("acme/api", 42)
            all_checks = store.list_all_checks_for_pr("acme/api", 42)
        assert [c.name for c in gate_checks] == ["test (3.12)"]
        assert {c.name for c in all_checks} == {"test (3.12)", "acceptance"}

    def test_shares_one_subprocess_call_with_list_checks_for_pr(self) -> None:
        """Both views read the same cached fetch — asking for both must not
        double the `gh pr checks` subprocess cost."""
        payload = json.dumps([
            {"name": "test (3.12)", "state": "SUCCESS", "bucket": "pass",
             "link": "", "startedAt": "", "completedAt": ""},
        ])
        store = GitHubCi(cache_ttl=60.0)
        with (
            patch(
                "coord.ci_github.subprocess.run", return_value=_gh_result(payload),
            ) as run,
            patch.object(
                GitHubCi, "_required_contexts",
                lambda self, repo: frozenset({"test (3.12)"}),
            ),
        ):
            store.list_checks_for_pr("acme/api", 42)
            store.list_all_checks_for_pr("acme/api", 42)
            store.list_checks_for_pr("acme/api", 42)
        assert run.call_count == 1

    def test_equals_gate_view_when_required_contexts_unknown(self) -> None:
        """No branch protection configured/readable — same #1525 "unknown
        means don't narrow" bias applies to the visibility view too, so the
        two lists agree rather than one silently diverging."""
        payload = json.dumps([
            {"name": "test (3.12)", "state": "SUCCESS", "bucket": "pass",
             "link": "", "startedAt": "", "completedAt": ""},
        ])
        store = GitHubCi()
        with patch("coord.ci_github.subprocess.run", return_value=_gh_result(payload)):
            gate_checks = store.list_checks_for_pr("acme/api", 42)
            all_checks = store.list_all_checks_for_pr("acme/api", 42)
        assert [c.name for c in gate_checks] == [c.name for c in all_checks]


class TestGitHubCiRerunForPr:
    """#1851: the remedy side — re-running a PR's CI via `gh run rerun`."""

    def _payload(self, run_ids: list[str]) -> str:
        return json.dumps([
            {
                "name": f"check-{rid}", "state": "SUCCESS", "bucket": "pass",
                "link": f"https://github.com/acme/api/actions/runs/{rid}",
                "startedAt": "2026-05-24T12:00:00Z", "completedAt": "2026-05-24T12:05:00Z",
            }
            for rid in run_ids
        ])

    def test_reruns_every_distinct_run_id(self) -> None:
        store = GitHubCi()
        with patch("coord.ci_github.subprocess.run") as run:
            run.side_effect = [
                _gh_result(self._payload(["1", "2"])),  # list_checks_for_pr
                _gh_result(""),  # gh run rerun 1
                _gh_result(""),  # gh run rerun 2
            ]
            ok = store.rerun_for_pr("acme/api", 42)
        assert ok is True
        rerun_calls = [c for c in run.call_args_list if c.args[0][:3] == ["gh", "run", "rerun"]]
        assert len(rerun_calls) == 2
        rerun_ids = {c.args[0][3] for c in rerun_calls}
        assert rerun_ids == {"1", "2"}
        for c in rerun_calls:
            assert c.args[0][4:] == ["--repo", "acme/api"]

    def test_no_checks_returns_false(self) -> None:
        store = GitHubCi()
        with patch("coord.ci_github.subprocess.run", return_value=_gh_result("[]")):
            assert store.rerun_for_pr("acme/api", 42) is False

    def test_check_with_no_readable_run_id_is_skipped(self) -> None:
        payload = json.dumps([
            {"name": "third-party", "state": "SUCCESS", "bucket": "pass",
             "link": "", "startedAt": "2026-05-24T12:00:00Z", "completedAt": ""},
        ])
        store = GitHubCi()
        with patch("coord.ci_github.subprocess.run", return_value=_gh_result(payload)) as run:
            ok = store.rerun_for_pr("acme/api", 42)
        assert ok is False
        assert run.call_count == 1  # only the list_checks_for_pr call

    def test_partial_failure_reports_false(self) -> None:
        """One `gh run rerun` failing must not report success — the caller
        needs to know the rerun only partially worked."""
        store = GitHubCi()
        with patch("coord.ci_github.subprocess.run") as run:
            run.side_effect = [
                _gh_result(self._payload(["1", "2"])),
                _gh_result("", returncode=0),
                _gh_result("", returncode=1, stderr="run already in progress"),
            ]
            ok = store.rerun_for_pr("acme/api", 42)
        assert ok is False

    def test_success_invalidates_cache(self) -> None:
        store = GitHubCi(cache_ttl=60.0)
        with patch("coord.ci_github.subprocess.run") as run:
            run.side_effect = [
                _gh_result(self._payload(["1"])),
                _gh_result(""),
                _gh_result(self._payload(["1"])),  # re-fetched after invalidate
            ]
            store.rerun_for_pr("acme/api", 42)
            store.list_checks_for_pr("acme/api", 42)
        assert run.call_count == 3

    def test_success_invalidates_the_jobs_cache_for_the_reran_run_ids(self) -> None:
        """#1892 non-blocking finding: `gh run rerun` reruns the SAME Actions
        run id, so a cached `list_jobs_for_run` entry for that run id must
        not survive a successful rerun — otherwise a `list_jobs_for_run`
        call shortly after the rerun (within `cache_ttl`) could hand back
        stale pre-rerun job/step detail, which paired with a fresh
        `list_checks_for_pr` re-read is exactly the "launders a real
        failure into infrastructure" hazard the issue warns against."""
        jobs_payload = json.dumps({
            "total_count": 1,
            "jobs": [{
                "id": 1, "run_id": 1, "name": "test",
                "status": "completed", "conclusion": "cancelled",
                "runner_name": "", "steps": [],
            }],
        })
        store = GitHubCi(cache_ttl=60.0)
        with patch("coord.ci_github.subprocess.run") as run:
            run.side_effect = [
                _gh_result(jobs_payload),  # list_jobs_for_run, pre-rerun
                _gh_result(self._payload(["1"])),  # list_checks_for_pr (inside rerun_for_pr)
                _gh_result(""),  # gh run rerun 1
                _gh_result(jobs_payload),  # list_jobs_for_run, post-rerun — must NOT be cached
            ]
            store.list_jobs_for_run("acme/api", "1")
            store.rerun_for_pr("acme/api", 42)
            store.list_jobs_for_run("acme/api", "1")
        assert run.call_count == 4

    def test_failed_rerun_leaves_the_jobs_cache_alone(self) -> None:
        """A rerun that didn't actually succeed has nothing stale to worry
        about invalidating — mirrors the existing checks-cache behavior
        (only a success invalidates)."""
        jobs_payload = json.dumps({
            "total_count": 1,
            "jobs": [{
                "id": 1, "run_id": 1, "name": "test",
                "status": "completed", "conclusion": "cancelled",
                "runner_name": "", "steps": [],
            }],
        })
        store = GitHubCi(cache_ttl=60.0)
        with patch("coord.ci_github.subprocess.run") as run:
            run.side_effect = [
                _gh_result(jobs_payload),  # list_jobs_for_run, pre-rerun
                _gh_result(self._payload(["1"])),  # list_checks_for_pr
                _gh_result("", returncode=1, stderr="boom"),  # gh run rerun fails
                # list_jobs_for_run below must be served from cache — no
                # further subprocess call queued.
            ]
            store.list_jobs_for_run("acme/api", "1")
            ok = store.rerun_for_pr("acme/api", 42)
            assert ok is False
            store.list_jobs_for_run("acme/api", "1")
        assert run.call_count == 3


class TestGitHubCiRerunFailedForPr:
    """#2252: the narrower ``--failed`` sibling of `rerun_for_pr` — reruns
    only the run ids behind currently-FAILING checks, leaving passing ones
    (and their run ids) untouched."""

    def _payload(self, *, failing: list[str], passing: list[str]) -> str:
        checks = [
            {
                "name": f"fail-{rid}", "state": "FAILURE", "bucket": "fail",
                "link": f"https://github.com/acme/api/actions/runs/{rid}",
                "startedAt": "2026-05-24T12:00:00Z", "completedAt": "2026-05-24T12:05:00Z",
            }
            for rid in failing
        ] + [
            {
                "name": f"pass-{rid}", "state": "SUCCESS", "bucket": "pass",
                "link": f"https://github.com/acme/api/actions/runs/{rid}",
                "startedAt": "2026-05-24T12:00:00Z", "completedAt": "2026-05-24T12:05:00Z",
            }
            for rid in passing
        ]
        return json.dumps(checks)

    def test_reruns_only_the_failing_run_ids(self) -> None:
        store = GitHubCi()
        payload = self._payload(failing=["1"], passing=["2"])
        with patch("coord.ci_github.subprocess.run") as run:
            run.side_effect = [
                _gh_result(payload),  # list_checks_for_pr
                _gh_result(""),  # gh run rerun 1 --failed
            ]
            ok = store.rerun_failed_for_pr("acme/api", 42)
        assert ok is True
        assert run.call_count == 2  # never touches run id 2 (the passing one)
        rerun_call = run.call_args_list[1].args[0]
        assert rerun_call == [
            "gh", "run", "rerun", "1", "--repo", "acme/api", "--failed",
        ]

    def test_multiple_failing_run_ids_all_rerun_scoped_to_failed(self) -> None:
        store = GitHubCi()
        payload = self._payload(failing=["1", "2"], passing=["3"])
        with patch("coord.ci_github.subprocess.run") as run:
            run.side_effect = [
                _gh_result(payload),
                _gh_result(""),
                _gh_result(""),
            ]
            ok = store.rerun_failed_for_pr("acme/api", 42)
        assert ok is True
        rerun_calls = [
            c for c in run.call_args_list if c.args[0][:3] == ["gh", "run", "rerun"]
        ]
        assert len(rerun_calls) == 2
        rerun_ids = {c.args[0][3] for c in rerun_calls}
        assert rerun_ids == {"1", "2"}
        for c in rerun_calls:
            assert c.args[0][4:] == ["--repo", "acme/api", "--failed"]

    def test_all_green_returns_false_without_reruning_anything(self) -> None:
        store = GitHubCi()
        payload = self._payload(failing=[], passing=["1", "2"])
        with patch("coord.ci_github.subprocess.run", return_value=_gh_result(payload)) as run:
            ok = store.rerun_failed_for_pr("acme/api", 42)
        assert ok is False
        assert run.call_count == 1  # only the list_checks_for_pr call

    def test_no_checks_returns_false(self) -> None:
        store = GitHubCi()
        with patch("coord.ci_github.subprocess.run", return_value=_gh_result("[]")):
            assert store.rerun_failed_for_pr("acme/api", 42) is False

    def test_partial_failure_reports_false(self) -> None:
        store = GitHubCi()
        payload = self._payload(failing=["1", "2"], passing=[])
        with patch("coord.ci_github.subprocess.run") as run:
            run.side_effect = [
                _gh_result(payload),
                _gh_result("", returncode=0),
                _gh_result("", returncode=1, stderr="run already in progress"),
            ]
            ok = store.rerun_failed_for_pr("acme/api", 42)
        assert ok is False

    def test_success_invalidates_cache(self) -> None:
        store = GitHubCi(cache_ttl=60.0)
        payload = self._payload(failing=["1"], passing=[])
        with patch("coord.ci_github.subprocess.run") as run:
            run.side_effect = [
                _gh_result(payload),
                _gh_result(""),
                _gh_result(payload),  # re-fetched after invalidate
            ]
            store.rerun_failed_for_pr("acme/api", 42)
            store.list_checks_for_pr("acme/api", 42)
        assert run.call_count == 3


class TestGitHubCiListJobsForRun:
    """#1892: `gh api repos/{repo}/actions/runs/{id}/jobs` job/step detail —
    the extra call the CI-failure classification path pays for. Fixture
    shapes below are the real ``jobs`` response bodies recorded from
    JDonaghy/vimcode run 31119463000 (trimmed to the fields this code
    reads)."""

    # Attempt 1: got a runner, died at "Set up job".
    DIED_BEFORE_CHECKOUT = json.dumps({
        "total_count": 1,
        "jobs": [
            {
                "id": 1, "run_id": 31119463000, "name": "Test (Linux, headless)",
                "status": "completed", "conclusion": "failure",
                "runner_name": "GitHub Actions 1000009740",
                "steps": [
                    {"name": "Set up job", "status": "completed", "conclusion": "failure", "number": 1},
                ],
            },
        ],
    })

    # Attempt 2: never assigned a runner — cancelled at the queue timeout.
    NEVER_ASSIGNED_A_RUNNER = json.dumps({
        "total_count": 1,
        "jobs": [
            {
                "id": 2, "run_id": 31119463000, "name": "Test (Linux, headless)",
                "status": "completed", "conclusion": "cancelled",
                "runner_name": "", "steps": [],
            },
        ],
    })

    def test_parses_died_before_checkout_shape(self) -> None:
        store = GitHubCi()
        with patch(
            "coord.ci_github.subprocess.run",
            return_value=_gh_result(self.DIED_BEFORE_CHECKOUT),
        ):
            jobs = store.list_jobs_for_run("JDonaghy/vimcode", "31119463000")
        assert len(jobs) == 1
        job = jobs[0]
        assert job.name == "Test (Linux, headless)"
        assert job.conclusion == "failure"
        assert job.runner_name == "GitHub Actions 1000009740"
        assert [(s.name, s.conclusion) for s in job.steps] == [("Set up job", "failure")]

    def test_parses_never_assigned_a_runner_shape(self) -> None:
        store = GitHubCi()
        with patch(
            "coord.ci_github.subprocess.run",
            return_value=_gh_result(self.NEVER_ASSIGNED_A_RUNNER),
        ):
            jobs = store.list_jobs_for_run("JDonaghy/vimcode", "31119463000")
        assert len(jobs) == 1
        job = jobs[0]
        assert job.conclusion == "cancelled"
        assert job.runner_name == ""
        assert job.steps == []

    def test_calls_the_documented_gh_api_endpoint(self) -> None:
        store = GitHubCi()
        with patch(
            "coord.ci_github.subprocess.run",
            return_value=_gh_result(self.NEVER_ASSIGNED_A_RUNNER),
        ) as run:
            store.list_jobs_for_run("JDonaghy/vimcode", "31119463000")
        args = run.call_args.args[0]
        assert args == [
            "gh", "api", "repos/JDonaghy/vimcode/actions/runs/31119463000/jobs",
        ]

    def test_read_failure_returns_empty_not_raises(self) -> None:
        """#1892's false-negative bias: a failed fetch must degrade to "no
        job data" — the classifier already treats that as "not verdictless",
        so there is nothing to raise or synthesize here."""
        store = GitHubCi()
        with patch("coord.ci_github.subprocess.run", side_effect=FileNotFoundError):
            assert store.list_jobs_for_run("acme/api", "1") == []

    def test_malformed_response_returns_empty(self) -> None:
        store = GitHubCi()
        with patch(
            "coord.ci_github.subprocess.run",
            return_value=_gh_result("not json"),
        ):
            assert store.list_jobs_for_run("acme/api", "1") == []

    def test_cache_avoids_second_call(self) -> None:
        store = GitHubCi(cache_ttl=60.0)
        with patch(
            "coord.ci_github.subprocess.run",
            return_value=_gh_result(self.NEVER_ASSIGNED_A_RUNNER),
        ) as run:
            store.list_jobs_for_run("acme/api", "1")
            store.list_jobs_for_run("acme/api", "1")
        assert run.call_count == 1

    def test_cache_keyed_per_run_id(self) -> None:
        store = GitHubCi(cache_ttl=60.0)
        with patch(
            "coord.ci_github.subprocess.run",
            return_value=_gh_result(self.NEVER_ASSIGNED_A_RUNNER),
        ) as run:
            store.list_jobs_for_run("acme/api", "1")
            store.list_jobs_for_run("acme/api", "2")
        assert run.call_count == 2


# ── Merge gate integration ───────────────────────────────────────────────────

from dataclasses import dataclass, field as dataclass_field
from coord.merge_queue import MERGED, MERGING, PENDING, QueuedMerge, process


@dataclass
class FakeCi:
    """Stub CiStore that returns canned responses per PR number."""

    by_pr: dict[int, list[CheckRun]] = dataclass_field(default_factory=dict)
    is_available: bool = True
    # #1904: whether a PR *not* listed in `by_pr` (so `list_checks_for_pr`
    # returns `[]`) should read as "checks expected but absent" (True) or
    # as the pre-#1904 "nothing to check for this PR" default (False).
    # Defaults False so every existing test here — which only ever sets up
    # `by_pr` for the specific PR(s) it cares about and never expects an
    # unrelated PR to block — keeps its prior behaviour unchanged.
    declares_ci: bool = False

    def list_checks_for_pr(self, repo: str, number: int) -> list[CheckRun]:
        return self.by_pr.get(number, [])

    def expects_checks(self, repo: str, number: int) -> bool:
        return self.declares_ci


@dataclass
class FakeGh:
    next_pr: int = 100
    merge_calls: list[tuple[int, str]] = dataclass_field(default_factory=list)
    # #1624: branch name -> already-open PR dict ({"number", "url"}). Empty by
    # default so existing tests (none of which set this) see no PR and get
    # the pre-#1624 "no PR found" behaviour.
    existing_prs: dict[str, dict] = dataclass_field(default_factory=dict)
    find_pr_calls: list[tuple[str, str]] = dataclass_field(default_factory=list)
    # #1851: the target branch's "current" commit timestamp for the CI
    # staleness check. Defaults to the epoch so any check carrying a real
    # (post-1970) `started_at` reads as fresh by default — tests that care
    # about staleness set this explicitly instead.
    branch_commit_timestamp: float | None = 0.0
    # #1877: before committing to a `checks_absent` block, the gate asks
    # whether the PR is CONFLICTED — a PR that can't build a merge ref never
    # gets checks, and that reading routes to #241's conflict-fix rebase
    # instead of a human-only block. Mirrors the same pair on
    # tests/test_merge_queue.py's FakeGh.
    #
    # Defaults to an empty dict, so an unset PR reads `None` — INCONCLUSIVE,
    # not "mergeable". Only a confirmed `False` diverts; None leaves the
    # checks_absent block exactly as it was pre-#1877, which is what every
    # test here (none of which is about conflicts) expects.
    mergeable_results: dict[int, bool | None] = dataclass_field(default_factory=dict)
    mergeable_calls: list[tuple[str, int]] = dataclass_field(default_factory=list)

    def check_pr_mergeable(self, repo: str, number: int) -> bool | None:
        self.mergeable_calls.append((repo, number))
        return self.mergeable_results.get(number)

    def create_pr(self, repo: str, *, base: str, head: str, title: str, body: str) -> dict:
        n = self.next_pr
        self.next_pr += 1
        return {"number": n, "url": f"https://gh/x/{n}", "existed": False}

    def get_pr_size(self, repo: str, number: int) -> int:
        return 10

    def merge_pr(self, repo: str, number: int, method: str = "rebase") -> tuple[bool, str]:
        self.merge_calls.append((number, method))
        return True, "merged"

    def find_pr_for_branch(self, repo: str, branch: str) -> dict | None:
        self.find_pr_calls.append((repo, branch))
        return self.existing_prs.get(branch)

    def get_branch_commit_timestamp(self, repo: str, branch: str) -> float | None:
        return self.branch_commit_timestamp


def _entry(aid: str = "a") -> QueuedMerge:
    return QueuedMerge(
        assignment_id=aid,
        repo_name="api",
        repo_github="acme/api",
        branch=f"worker/{aid}",
        target_branch="main",
        issue_number=1,
        issue_title="t",
        state=PENDING,
    )


class TestMergeGate:
    def test_failed_check_blocks_merge(self) -> None:
        items = [_entry("a")]
        gh = FakeGh()
        ci = FakeCi(by_pr={100: [_check("ci", conclusion="failure")]})
        events = process(items, gh, ci_store=ci)
        assert gh.merge_calls == []
        assert items[0].state == PENDING
        kinds = [e.kind for e in events]
        assert "checks_failed" in kinds

    def test_pending_check_blocks_merge(self) -> None:
        items = [_entry("a")]
        gh = FakeGh()
        ci = FakeCi(by_pr={100: [_check("ci", status="in_progress", conclusion=None)]})
        events = process(items, gh, ci_store=ci)
        assert gh.merge_calls == []
        kinds = [e.kind for e in events]
        assert "checks_pending" in kinds

    def test_passing_checks_allow_merge(self) -> None:
        items = [_entry("a")]
        gh = FakeGh()
        # #1851: a real (post-epoch) started_at, fresh against FakeGh's
        # default `branch_commit_timestamp=0.0` — otherwise a check with no
        # recorded start time reads as CI-stale (fail-closed) and blocks.
        ci = FakeCi(by_pr={100: [_check("ci", conclusion="success", started_at=1000.0)]})
        process(items, gh, ci_store=ci)
        assert gh.merge_calls == [(100, "rebase")]
        assert items[0].state == MERGED

    def test_force_merge_overrides_failed(self) -> None:
        items = [_entry("a")]
        gh = FakeGh()
        ci = FakeCi(by_pr={100: [_check("ci", conclusion="failure")]})
        process(items, gh, ci_store=ci, force_merge=True)
        assert gh.merge_calls == [(100, "rebase")]
        assert items[0].state == MERGED

    def test_unreadable_ci_blocks_merge(self) -> None:
        """#1525 regression: a CI read that failed (represented here the same
        way GitHubCi._fetch represents it — a synthetic "unknown" check) must
        refuse to merge, exactly like a real CI failure."""
        items = [_entry("a")]
        gh = FakeGh()
        ci = FakeCi(by_pr={100: [_check("ci", conclusion="unknown")]})
        events = process(items, gh, ci_store=ci)
        assert gh.merge_calls == []
        assert items[0].state == PENDING
        kinds = [e.kind for e in events]
        assert "checks_failed" in kinds

    def test_cancelled_check_blocks_merge(self) -> None:
        """#1525 regression: CANCELLED (e.g. a fail-fast sibling of a real
        failure) must refuse to merge without --force-merge."""
        items = [_entry("a")]
        gh = FakeGh()
        ci = FakeCi(by_pr={100: [_check("ci", conclusion="cancelled")]})
        events = process(items, gh, ci_store=ci)
        assert gh.merge_calls == []
        kinds = [e.kind for e in events]
        assert "checks_failed" in kinds

    def test_force_merge_overrides_unreadable_ci(self) -> None:
        items = [_entry("a")]
        gh = FakeGh()
        ci = FakeCi(by_pr={100: [_check("ci", conclusion="unknown")]})
        process(items, gh, ci_store=ci, force_merge=True)
        assert gh.merge_calls == [(100, "rebase")]
        assert items[0].state == MERGED

    def test_noop_ci_allows_merge(self) -> None:
        items = [_entry("a")]
        gh = FakeGh()
        process(items, gh, ci_store=NoOpCi())
        assert gh.merge_calls == [(100, "rebase")]

    def test_no_ci_store_allows_merge(self) -> None:
        """Backwards-compat: callers that don't pass ci_store still work."""
        items = [_entry("a")]
        gh = FakeGh()
        process(items, gh)
        assert gh.merge_calls == [(100, "rebase")]

    # ── #1904: checks == [] is ambiguous — "no CI configured" (merge is
    # correct) vs. "CI exists but never triggered for this PR" (merge is
    # wrong). `FakeCi.declares_ci` is the stub's answer to that question.

    def test_checks_absent_blocks_merge_when_ci_declared(self) -> None:
        """A repo that declares CI but never reported a single check for
        this PR — the exact 2026-08-06 webhook-throttle shape — must block,
        not silently merge untested code."""
        items = [_entry("a")]
        gh = FakeGh()
        ci = FakeCi(by_pr={}, declares_ci=True)
        events = process(items, gh, ci_store=ci)
        assert gh.merge_calls == []
        assert items[0].state == PENDING
        kinds = [e.kind for e in events]
        assert "checks_absent" in kinds
        assert "checks_failed" not in kinds
        assert "checks_pending" not in kinds
        absent_event = next(e for e in events if e.kind == "checks_absent")
        assert "CI never ran" in absent_event.message

    def test_checks_absent_reason_persisted_on_entry(self) -> None:
        """The blocked reason lands on `entry.error` — the field
        `IssueState.merge_reason` and the board read (#1891's fallback)
        consult — not just the transient event."""
        items = [_entry("a")]
        gh = FakeGh()
        ci = FakeCi(by_pr={}, declares_ci=True)
        process(items, gh, ci_store=ci)
        assert items[0].error is not None
        assert "CI never ran" in items[0].error

    def test_no_workflows_declared_still_allows_merge(self) -> None:
        """Companion regression: a repo with no CI configured at all
        (`expects_checks` answers False, mirroring a repo with no
        `.github/workflows` or `GitHubCi` reading zero declared workflows)
        must not be deadlocked by the #1904 fix — an empty check list here
        is the correct, unremarkable reading."""
        items = [_entry("a")]
        gh = FakeGh()
        ci = FakeCi(by_pr={}, declares_ci=False)
        process(items, gh, ci_store=ci)
        assert gh.merge_calls == [(100, "rebase")]
        assert items[0].state == MERGED

    def test_force_merge_overrides_checks_absent(self) -> None:
        items = [_entry("a")]
        gh = FakeGh()
        ci = FakeCi(by_pr={}, declares_ci=True)
        process(items, gh, ci_store=ci, force_merge=True)
        assert gh.merge_calls == [(100, "rebase")]
        assert items[0].state == MERGED

    def test_checks_absent_dry_run_matches_real_path(self) -> None:
        """--dry-run must report the same verdict the real path would —
        the #1904 incident's dry-run mirrors the real bug exactly, so the
        usual "check with --dry-run first" habit didn't catch it either.
        Uses an already-open PR (`existing_prs`) so dry-run's CI gate is
        actually evaluated — a brand-new entry with no PR yet is a
        different, already-covered case (`test_reports_ci_unknown_when_no_pr_yet`)."""
        items = [_entry("a")]
        gh = FakeGh(existing_prs={"worker/a": {"number": 612, "url": "https://gh/x/612"}})
        ci = FakeCi(by_pr={}, declares_ci=True)
        events = process(items, gh, ci_store=ci, dry_run=True)
        assert gh.merge_calls == []
        kinds = [e.kind for e in events]
        assert "checks_absent" in kinds
        merged_events = [e for e in events if e.kind == "merged"]
        assert not merged_events

    def test_failed_check_halts_group_only(self) -> None:
        """A failed check on one PR shouldn't block PRs in other groups."""
        items = [
            _entry("a"),
            QueuedMerge(
                assignment_id="b",
                repo_name="ui",
                repo_github="acme/ui",
                branch="worker/b",
                target_branch="main",
                issue_number=2,
                issue_title="t",
                state=PENDING,
            ),
        ]
        gh = FakeGh()
        ci = FakeCi(by_pr={100: [_check("ci", conclusion="failure")]})
        process(items, gh, ci_store=ci)
        # `a` blocked, `b` (different repo group) merged
        merged_prs = [c[0] for c in gh.merge_calls]
        assert 100 not in merged_prs
        assert 101 in merged_prs


class TestDryRunCiGate:
    """#1624: `coord merge --dry-run` used to unconditionally report "would
    open PR" for every entry, even one whose branch already has an open PR —
    so the CI gate (which needs a real PR number) was silently skipped and a
    PR with failing checks was previewed as mergeable. These assert the fix:
    dry-run resolves the existing PR via `find_pr_for_branch` (mirroring what
    `create_pr` already does on the real path) and evaluates the CI gate
    against it, same as a real merge would."""

    def test_resolves_existing_pr_and_blocks_on_failed_ci(self) -> None:
        items = [_entry("a")]
        gh = FakeGh(existing_prs={"worker/a": {"number": 612, "url": "https://gh/x/612"}})
        ci = FakeCi(by_pr={612: [_check("ci", conclusion="failure")]})
        events = process(items, gh, ci_store=ci, dry_run=True)

        # No real gh calls of any kind — a preview never mutates GitHub.
        assert gh.merge_calls == []
        assert items[0].state == PENDING

        opened = next(e for e in events if e.kind == "opened")
        assert "PR #612 (existed)" in opened.message
        assert "would open PR" not in opened.message

        kinds = [e.kind for e in events]
        assert "checks_failed" in kinds
        # The bug: this used to render as "would merge" with the CI gate
        # never evaluated. It must not be reported as mergeable.
        assert "merged" not in kinds

    def test_resolves_existing_pr_and_allows_on_green_ci(self) -> None:
        items = [_entry("a")]
        gh = FakeGh(existing_prs={"worker/a": {"number": 612, "url": "https://gh/x/612"}})
        ci = FakeCi(by_pr={612: [_check("ci", conclusion="success", started_at=1000.0)]})
        events = process(items, gh, ci_store=ci, dry_run=True)

        assert gh.merge_calls == []
        assert items[0].state == PENDING

        opened = next(e for e in events if e.kind == "opened")
        assert "PR #612 (existed)" in opened.message

        kinds = [e.kind for e in events]
        assert "checks_failed" not in kinds
        assert "checks_pending" not in kinds
        assert "checks_stale" not in kinds
        assert "merged" in kinds

    def test_reports_ci_unknown_when_no_pr_yet(self) -> None:
        """A brand-new entry has no PR to check CI against — dry-run must say
        so explicitly rather than silently rendering "would merge" as if the
        gate had passed."""
        items = [_entry("a")]
        gh = FakeGh()  # no existing_prs — branch genuinely has no open PR
        ci = FakeCi(by_pr={})
        events = process(items, gh, ci_store=ci, dry_run=True)

        assert gh.merge_calls == []
        opened = next(e for e in events if e.kind == "opened")
        assert "would open PR" in opened.message

        kinds = [e.kind for e in events]
        assert "checks_failed" not in kinds
        assert "checks_pending" not in kinds
        merged = next(e for e in events if e.kind == "merged")
        assert "gate: unknown (no PR yet)" in merged.message

    def test_force_merge_skips_ci_gate_in_dry_run_too(self) -> None:
        """--force-merge's real-path CI bypass is previewed identically in
        dry-run — this issue is about honesty, not about changing
        --force-merge semantics."""
        items = [_entry("a")]
        gh = FakeGh(existing_prs={"worker/a": {"number": 612, "url": "https://gh/x/612"}})
        ci = FakeCi(by_pr={612: [_check("ci", conclusion="failure")]})
        events = process(items, gh, ci_store=ci, dry_run=True, force_merge=True)

        kinds = [e.kind for e in events]
        assert "checks_failed" not in kinds
        assert "merged" in kinds

    def test_no_real_gh_or_merge_calls_in_dry_run(self) -> None:
        """Regression: dry-run still opens/merges nothing, even once it looks
        up the real PR and evaluates real CI status."""
        items = [_entry("a")]
        gh = FakeGh(existing_prs={"worker/a": {"number": 612, "url": "https://gh/x/612"}})
        ci = FakeCi(by_pr={612: [_check("ci", conclusion="success")]})
        process(items, gh, ci_store=ci, dry_run=True)

        assert gh.merge_calls == []
        assert gh.find_pr_calls == [("acme/api", "worker/a")]
        assert items[0].state == PENDING
        # pr_number is resolved in-memory (same pattern as branch_head_sha
        # elsewhere in the dry-run path) — persisting it is the caller's
        # job (cli.py only calls save_queue() when not dry_run), out of
        # scope for process() itself.
        assert items[0].pr_number == 612


class TestMergeGateThroughGitHubCi:
    """#1564 acceptance: black-box through the *real* :class:`GitHubCi`
    backend (not the ``FakeCi`` stub above) with `gh`'s actual
    ``--json name,state,bucket,...`` shape — green merges, red refuses and
    names the failing check, and an unreachable ``gh`` refuses as
    "unavailable" rather than silently allowing the merge."""

    def test_green_allows_merge(self) -> None:
        items = [_entry("a")]
        gh = FakeGh()
        payload = json.dumps([
            {"name": "test (3.13)", "state": "SUCCESS", "bucket": "pass",
             "link": "", "startedAt": "2026-05-24T12:00:00Z", "completedAt": "2026-05-24T12:05:00Z"},
        ])
        ci = GitHubCi()
        with patch("coord.ci_github.subprocess.run", return_value=_gh_result(payload)):
            process(items, gh, ci_store=ci)
        assert gh.merge_calls == [(100, "rebase")]
        assert items[0].state == MERGED

    def test_red_refuses_and_names_failing_check(self) -> None:
        """#2252: a red check with a real verdict gets ONE scoped re-run
        (`GitHubCi.rerun_failed_for_pr`, `gh run rerun ... --failed`) before
        `process()` treats it as broken — the first call re-checks instead
        of refusing outright (`ci_flaky_rerun`, still not merged). This mock
        always returns the same failing payload (a real flake would come
        back green), so the second call observes it red again and confirms
        it: the pre-#2252 single-call refusal, naming the failing check."""
        items = [_entry("a")]
        gh = FakeGh()
        payload = json.dumps([
            {"name": "test (3.13)", "state": "FAILURE", "bucket": "fail",
             "link": "https://github.com/acme/api/actions/runs/1/job/1",
             "startedAt": "", "completedAt": ""},
        ])
        ci = GitHubCi()
        with patch("coord.ci_github.subprocess.run", return_value=_gh_result(payload)):
            first_events = process(items, gh, ci_store=ci)
            first_kinds = [e.kind for e in first_events]
            assert "ci_flaky_rerun" in first_kinds
            assert "checks_failed" not in first_kinds
            assert "merged" not in first_kinds
            assert gh.merge_calls == []
            assert items[0].state == PENDING

            events = process(items, gh, ci_store=ci)
        assert gh.merge_calls == []
        assert items[0].state == PENDING
        failed_event = next(e for e in events if e.kind == "checks_failed")
        assert "test (3.13)" in failed_event.message
        assert "failure" in failed_event.message

    def test_unreachable_gh_refuses_as_unavailable(self) -> None:
        """#2347: a bare check-list FETCH failure (gh itself unreachable —
        `FileNotFoundError`, the #1525 fail-closed synthetic "could not read
        CI status" stand-in) is classified as `checks_unreadable`, distinct
        from a plain `checks_failed` block — it is a transport failure, not
        a real CI verdict of any kind. Still refuses the merge (fail-closed,
        #1525's rule is unchanged), just with the correct, distinguishable
        reason."""
        items = [_entry("a")]
        gh = FakeGh()
        ci = GitHubCi()
        with patch("coord.ci_github.subprocess.run", side_effect=FileNotFoundError):
            events = process(items, gh, ci_store=ci)
        assert gh.merge_calls == []
        assert items[0].state == PENDING
        kinds = [e.kind for e in events]
        assert "checks_failed" not in kinds
        unreadable_event = next(e for e in events if e.kind == "checks_unreadable")
        assert "could not read CI status" in unreadable_event.message
        assert "GitHub could not be reached" in unreadable_event.message


# ── Config ───────────────────────────────────────────────────────────────────

from coord.config import _parse_ci_store, ConfigError


class TestParseCiStore:
    def test_absent_defaults_to_github(self) -> None:
        cfg = _parse_ci_store(None)
        assert cfg.type == "github"

    def test_explicit_none(self) -> None:
        cfg = _parse_ci_store({"type": "none"})
        assert cfg.type == "none"

    def test_explicit_github(self) -> None:
        cfg = _parse_ci_store({"type": "github"})
        assert cfg.type == "github"

    def test_invalid_type_raises(self) -> None:
        with pytest.raises(ConfigError):
            _parse_ci_store({"type": "buildkite"})

    def test_non_mapping_raises(self) -> None:
        with pytest.raises(ConfigError):
            _parse_ci_store(["github"])
