"""Black-box tests for `coord ci runs`/`coord ci jobs`/`coord ci checks`/
`coord ci rerun` (#3405).

Drives the CLI end-to-end against a stubbed `CiStore` (patched at
`coord.ci_store.build_ci_store`, the exact seam `coord pr merge`'s own tests
already use — see `tests/test_cli_pr.py`) and asserts on rendered stdout —
CLAUDE.md's black-box bar. These are the acceptance tests the issue calls
for: a run whose only failure is `windows` must render `windows` as the sole
failing row while a `skipped` job (e.g. the advisory `postgres` job) is never
reported as one, and `--branch main --event push` must only render
push-event runs.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from coord.ci_store import CheckRun, JobRun, RunSummary
from coord.cli import main

CONFIG_YAML = """\
repos:
  - name: api
    github: acme/api
    default_branch: main
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


def _invoke(*args: str, config_file: Path) -> object:
    return CliRunner().invoke(main, [*args, "--config", str(config_file)])


class _FakeCiStore:
    def __init__(
        self,
        *,
        runs: list[RunSummary] | None = None,
        jobs: list[JobRun] | None = None,
        checks: list[CheckRun] | None = None,
        rerun_for_pr_result: bool = True,
        rerun_failed_for_pr_result: bool = True,
        rerun_job_result: bool = True,
        rerun_run_result: bool = True,
        supports_rerun_job: bool = True,
        supports_rerun_run: bool = True,
    ) -> None:
        self._runs = runs or []
        self._jobs = jobs or []
        self._checks = checks or []
        self.runs_calls: list[tuple] = []
        self._rerun_for_pr_result = rerun_for_pr_result
        self._rerun_failed_for_pr_result = rerun_failed_for_pr_result
        self._rerun_job_result = rerun_job_result
        self._rerun_run_result = rerun_run_result
        self.rerun_for_pr_calls: list[tuple] = []
        self.rerun_failed_for_pr_calls: list[tuple] = []
        self.rerun_job_calls: list[tuple] = []
        self.rerun_run_calls: list[tuple] = []
        if supports_rerun_job:
            self.rerun_job = self._rerun_job
        # else: no `rerun_job` attribute at all — the duck-typed "this
        # backend predates (or never modeled) single-job reruns" case.
        if supports_rerun_run:
            self.rerun_run = self._rerun_run
        # else: no `rerun_run` attribute at all — the duck-typed "this
        # backend does not implement explicit-run-id reruns" case.

    def list_runs_for_branch(self, repo, branch, *, event=None, limit=20):
        self.runs_calls.append((repo, branch, event, limit))
        if event is None:
            return self._runs
        return [r for r in self._runs if r.event == event]

    def list_jobs_for_run(self, repo, run_id):
        return self._jobs

    def list_all_checks_for_pr(self, repo, number):
        return self._checks

    def rerun_for_pr(self, repo, number):
        self.rerun_for_pr_calls.append((repo, number))
        return self._rerun_for_pr_result

    def rerun_failed_for_pr(self, repo, number):
        self.rerun_failed_for_pr_calls.append((repo, number))
        return self._rerun_failed_for_pr_result

    def _rerun_job(self, repo, run_id, job_id):
        self.rerun_job_calls.append((repo, run_id, job_id))
        return self._rerun_job_result

    def _rerun_run(self, repo, run_id):
        self.rerun_run_calls.append((repo, run_id))
        return self._rerun_run_result


def _run(run_id: str, *, event: str, conclusion: str | None, name: str = "test") -> RunSummary:
    return RunSummary(
        run_id=run_id, name=name, status="completed", conclusion=conclusion,
        event=event, branch="main", url=f"https://github.com/acme/api/actions/runs/{run_id}",
        created_at=1758000000.0,
    )


def _job(name: str, conclusion: str | None, *, runner_name: str = "ubuntu-latest") -> JobRun:
    return JobRun(name=name, conclusion=conclusion, runner_name=runner_name, steps=[])


class TestCiJobs:
    def test_renders_one_row_per_job_and_flags_the_real_failure(self, config_file: Path) -> None:
        """The exact acceptance scenario from #3405: a run whose only
        failure is `windows` must render `windows` as the sole failing row,
        while the advisory `postgres` job (skipped) is not reported as one."""
        store = _FakeCiStore(jobs=[
            _job("windows", "failure"),
            _job("postgres", "skipped"),
            _job("linux", "success"),
        ])
        with patch("coord.ci_store.build_ci_store", return_value=store):
            result = _invoke("ci", "jobs", "api", "12345", config_file=config_file)

        assert result.exit_code == 0, result.output
        assert "1 failing" in result.output
        lines = result.output.splitlines()
        windows_line = next(l for l in lines if l.startswith("windows"))
        postgres_line = next(l for l in lines if l.startswith("postgres"))
        linux_line = next(l for l in lines if l.startswith("linux"))
        assert "FAILED" in windows_line
        assert "FAILED" not in postgres_line
        assert "FAILED" not in linux_line

    def test_no_jobs_reports_none_found(self, config_file: Path) -> None:
        store = _FakeCiStore(jobs=[])
        with patch("coord.ci_store.build_ci_store", return_value=store):
            result = _invoke("ci", "jobs", "api", "999", config_file=config_file)

        assert result.exit_code == 0, result.output
        assert "no jobs found" in result.output

    def test_unknown_repo_lists_known_names(self, config_file: Path) -> None:
        result = _invoke("ci", "jobs", "nope", "1", config_file=config_file)

        assert result.exit_code != 0
        assert "unknown repo" in result.output


class TestCiRuns:
    def test_renders_only_the_requested_event(self, config_file: Path) -> None:
        store = _FakeCiStore(runs=[
            _run("1", event="push", conclusion="failure"),
            _run("2", event="pull_request", conclusion="success"),
        ])
        with patch("coord.ci_store.build_ci_store", return_value=store):
            result = _invoke(
                "ci", "runs", "api", "--branch", "main", "--event", "push",
                config_file=config_file,
            )

        assert result.exit_code == 0, result.output
        assert store.runs_calls == [("acme/api", "main", "push", 20)]
        assert "1\tpush" in result.output
        assert "pull_request" not in result.output

    def test_defaults_to_the_repo_default_branch(self, config_file: Path) -> None:
        store = _FakeCiStore(runs=[])
        with patch("coord.ci_store.build_ci_store", return_value=store):
            result = _invoke("ci", "runs", "api", config_file=config_file)

        assert result.exit_code == 0, result.output
        assert store.runs_calls == [("acme/api", "main", None, 20)]

    def test_failing_run_is_flagged(self, config_file: Path) -> None:
        store = _FakeCiStore(runs=[_run("1", event="push", conclusion="failure")])
        with patch("coord.ci_store.build_ci_store", return_value=store):
            result = _invoke("ci", "runs", "api", config_file=config_file)

        assert result.exit_code == 0, result.output
        assert "FAILED" in result.output

    def test_no_runs_reports_none_found(self, config_file: Path) -> None:
        store = _FakeCiStore(runs=[])
        with patch("coord.ci_store.build_ci_store", return_value=store):
            result = _invoke("ci", "runs", "api", config_file=config_file)

        assert result.exit_code == 0, result.output
        assert "no runs found" in result.output


class TestCiChecks:
    def test_renders_checks_with_failure_flagged(self, config_file: Path) -> None:
        checks = [
            CheckRun(
                name="build", status="completed", conclusion="failure",
                url="https://github.com/acme/api/pull/7/checks", run_id="1",
                started_at=None, completed_at=None,
            ),
            CheckRun(
                name="lint", status="completed", conclusion="success",
                url="", run_id="1", started_at=None, completed_at=None,
            ),
        ]
        store = _FakeCiStore(checks=checks)
        with patch("coord.ci_store.build_ci_store", return_value=store):
            result = _invoke("ci", "checks", "api", "7", config_file=config_file)

        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        build_line = next(l for l in lines if l.startswith("build"))
        lint_line = next(l for l in lines if l.startswith("lint"))
        assert "FAILED" in build_line
        assert "FAILED" not in lint_line

    def test_no_checks_reports_none_found(self, config_file: Path) -> None:
        store = _FakeCiStore(checks=[])
        with patch("coord.ci_store.build_ci_store", return_value=store):
            result = _invoke("ci", "checks", "api", "7", config_file=config_file)

        assert result.exit_code == 0, result.output
        assert "no checks reported" in result.output


class TestCiRerun:
    """`coord ci rerun` — the sanctioned seam for triggering a CI rerun
    instead of a raw `gh run rerun`."""

    def test_no_flags_reruns_the_whole_pr(self, config_file: Path) -> None:
        store = _FakeCiStore()
        with patch("coord.ci_store.build_ci_store", return_value=store):
            result = _invoke("ci", "rerun", "api", "7", config_file=config_file)

        assert result.exit_code == 0, result.output
        assert store.rerun_for_pr_calls == [("acme/api", 7)]
        assert store.rerun_failed_for_pr_calls == []
        assert "rerun triggered" in result.output

    def test_failed_flag_reruns_only_failing_run_ids(self, config_file: Path) -> None:
        store = _FakeCiStore()
        with patch("coord.ci_store.build_ci_store", return_value=store):
            result = _invoke("ci", "rerun", "api", "7", "--failed", config_file=config_file)

        assert result.exit_code == 0, result.output
        assert store.rerun_failed_for_pr_calls == [("acme/api", 7)]
        assert store.rerun_for_pr_calls == []

    def test_failed_and_run_are_mutually_exclusive(self, config_file: Path) -> None:
        result = _invoke(
            "ci", "rerun", "api", "7", "--failed", "--run", "123",
            config_file=config_file,
        )

        assert result.exit_code != 0
        assert "mutually exclusive" in result.output

    def test_job_without_run_is_rejected(self, config_file: Path) -> None:
        result = _invoke(
            "ci", "rerun", "api", "7", "--job", "99", config_file=config_file,
        )

        assert result.exit_code != 0
        assert "--job requires --run" in result.output

    def test_run_without_job_reruns_the_whole_run_via_the_backend(
        self, config_file: Path,
    ) -> None:
        store = _FakeCiStore()
        with patch("coord.ci_store.build_ci_store", return_value=store):
            result = _invoke(
                "ci", "rerun", "api", "7", "--run", "123", config_file=config_file,
            )

        assert result.exit_code == 0, result.output
        assert store.rerun_run_calls == [("acme/api", "123")]
        assert store.rerun_for_pr_calls == []
        assert store.rerun_failed_for_pr_calls == []
        assert store.rerun_job_calls == []

    def test_run_without_job_on_a_backend_without_rerun_run_errors_cleanly(
        self, config_file: Path,
    ) -> None:
        """A non-GitHub (or `type: none`) backend must decline an explicit
        `--run` rerun rather than this command silently falling back to
        GitHub regardless of the configured `ci_store.type`."""
        store = _FakeCiStore(supports_rerun_run=False)
        with patch("coord.ci_store.build_ci_store", return_value=store):
            result = _invoke(
                "ci", "rerun", "api", "7", "--run", "123", config_file=config_file,
            )

        assert result.exit_code != 0
        assert "does not support explicit run-id reruns" in result.output

    def test_run_and_job_reruns_exactly_that_job(self, config_file: Path) -> None:
        store = _FakeCiStore()
        with patch("coord.ci_store.build_ci_store", return_value=store):
            result = _invoke(
                "ci", "rerun", "api", "7", "--run", "123", "--job", "99",
                config_file=config_file,
            )

        assert result.exit_code == 0, result.output
        assert store.rerun_job_calls == [("acme/api", "123", "99")]
        assert "rerun triggered" in result.output

    def test_run_and_job_on_a_backend_without_rerun_job_errors_cleanly(
        self, config_file: Path,
    ) -> None:
        store = _FakeCiStore(supports_rerun_job=False)
        with patch("coord.ci_store.build_ci_store", return_value=store):
            result = _invoke(
                "ci", "rerun", "api", "7", "--run", "123", "--job", "99",
                config_file=config_file,
            )

        assert result.exit_code != 0
        assert "does not support single-job reruns" in result.output

    def test_stuck_reruns_exactly_that_job_with_a_distinct_label(
        self, config_file: Path,
    ) -> None:
        """#4071: `--stuck` is the manual counterpart to the merge-queue
        sweep's own bounded auto-heal — same underlying `rerun_job` call as
        a bare `--run`/`--job`, just labeled distinctly so a human (or a
        log) can tell a deliberate stuck-check escalation apart from an
        ordinary explicit-id rerun."""
        store = _FakeCiStore()
        with patch("coord.ci_store.build_ci_store", return_value=store):
            result = _invoke(
                "ci", "rerun", "api", "7", "--run", "123", "--job", "99",
                "--stuck", config_file=config_file,
            )

        assert result.exit_code == 0, result.output
        assert store.rerun_job_calls == [("acme/api", "123", "99")]
        assert "stuck-check rerun triggered" in result.output

    def test_stuck_without_run_and_job_is_rejected(self, config_file: Path) -> None:
        with patch("coord.ci_store.build_ci_store", return_value=_FakeCiStore()):
            result = _invoke(
                "ci", "rerun", "api", "7", "--stuck", config_file=config_file,
            )

        assert result.exit_code != 0
        assert "--stuck requires --run and --job" in result.output

    def test_failed_gh_exit_reports_nonzero_without_raising(self, config_file: Path) -> None:
        """A non-zero `gh` exit (surfaced as a `False` return, not a raise —
        see `github_ops.rerun_workflow_run`'s own contract) must still
        produce a clean CLI failure, not a traceback."""
        store = _FakeCiStore(rerun_for_pr_result=False)
        with patch("coord.ci_store.build_ci_store", return_value=store):
            result = _invoke("ci", "rerun", "api", "7", config_file=config_file)

        assert result.exit_code != 0
        # A clean `sys.exit(1)` — a `SystemExit`, not some other raised
        # exception bubbling up as a traceback.
        assert isinstance(result.exception, SystemExit)
        assert "rerun failed" in result.output
