"""Black-box tests for `coord ci runs`/`coord ci jobs`/`coord ci checks` (#3405).

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
    ) -> None:
        self._runs = runs or []
        self._jobs = jobs or []
        self._checks = checks or []
        self.runs_calls: list[tuple] = []

    def list_runs_for_branch(self, repo, branch, *, event=None, limit=20):
        self.runs_calls.append((repo, branch, event, limit))
        if event is None:
            return self._runs
        return [r for r in self._runs if r.event == event]

    def list_jobs_for_run(self, repo, run_id):
        return self._jobs

    def list_all_checks_for_pr(self, repo, number):
        return self._checks


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
