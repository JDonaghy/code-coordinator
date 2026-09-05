"""Tests for #3114: threading a failing job's name/step/log excerpt into a
ci-fix briefing instead of a bare one-line `checks_summary`.

Covers:
- `coord.github_ops.get_job_log` — the single `gh` sink for a job's raw log
- `coord.ci_github.GitHubCi._fetch_jobs` — carries the job id
  (`JobRun.job_id`) `get_job_log` needs, on top of #1892's existing fields
- `coord.ci_github.build_ci_failure_detail` — the pure-ish orchestration
  that turns a `CiStore` + repo/PR into a `CIFailureDetail`, fetching the
  job's log exactly once and failing soft on any error
- `coord.ci_github._bound_log_excerpt` — the truncation bound itself
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from coord import github_ops
from coord.ci_github import (
    CI_FIX_LOG_MAX_BYTES,
    CI_FIX_LOG_MAX_LINES,
    GitHubCi,
    _bound_log_excerpt,
    build_ci_failure_detail,
)
from coord.ci_store import CheckRun, JobRun, JobStep


def _gh_result(stdout: str = "[]", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# ── github_ops.get_job_log ──────────────────────────────────────────────────


class TestGetJobLog:
    def test_returns_raw_stdout(self, coord_db) -> None:
        with patch(
            "coord.github_ops.subprocess.run",
            return_value=_gh_result("line one\nline two\n"),
        ):
            assert github_ops.get_job_log("acme/api", "456") == "line one\nline two"

    def test_calls_the_documented_gh_api_endpoint(self, coord_db) -> None:
        with patch(
            "coord.github_ops.subprocess.run", return_value=_gh_result("log text"),
        ) as run:
            github_ops.get_job_log("acme/api", "456")
        assert run.call_args.args[0] == [
            "gh", "api", "repos/acme/api/actions/jobs/456/logs",
        ]

    def test_read_failure_raises(self, coord_db) -> None:
        with patch("coord.github_ops.subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(RuntimeError):
                github_ops.get_job_log("acme/api", "456")


# ── JobRun.job_id (#3114 addition on top of #1892's list_jobs_for_run) ──────


class TestListJobsForRunCarriesJobId:
    JOBS_PAYLOAD = json.dumps({
        "total_count": 1,
        "jobs": [
            {
                "id": 456, "run_id": 999, "name": "Test (Linux, headless)",
                "status": "completed", "conclusion": "failure",
                "runner_name": "GitHub Actions 1",
                "steps": [
                    {"name": "Set up job", "status": "completed", "conclusion": "success"},
                    {"name": "Run tests", "status": "completed", "conclusion": "failure"},
                ],
            },
        ],
    })

    def test_job_id_is_parsed(self) -> None:
        store = GitHubCi()
        with patch(
            "coord.ci_github.subprocess.run",
            return_value=_gh_result(self.JOBS_PAYLOAD),
        ):
            jobs = store.list_jobs_for_run("acme/api", "999")
        assert len(jobs) == 1
        assert jobs[0].job_id == "456"

    def test_missing_id_defaults_to_empty_string(self) -> None:
        store = GitHubCi()
        payload = json.dumps({"jobs": [{"name": "lint", "conclusion": "success", "steps": []}]})
        with patch("coord.ci_github.subprocess.run", return_value=_gh_result(payload)):
            jobs = store.list_jobs_for_run("acme/api", "1")
        assert jobs[0].job_id == ""


# ── _bound_log_excerpt ───────────────────────────────────────────────────────


class TestBoundLogExcerpt:
    def test_short_text_is_unchanged_and_not_truncated(self) -> None:
        text = "line1\nline2\n"
        excerpt, truncated = _bound_log_excerpt(text)
        assert excerpt == "line1\nline2"
        assert truncated is False

    def test_line_count_bound_keeps_the_tail(self) -> None:
        lines = [f"line{i}" for i in range(CI_FIX_LOG_MAX_LINES + 50)]
        excerpt, truncated = _bound_log_excerpt("\n".join(lines))
        assert truncated is True
        result_lines = excerpt.split("\n")
        assert len(result_lines) == CI_FIX_LOG_MAX_LINES
        # The tail, not the head, survives.
        assert result_lines[-1] == lines[-1]
        assert result_lines[0] == lines[50]

    def test_byte_count_bound_keeps_the_tail(self) -> None:
        # One line, no newlines to trip the line-count bound, but well over
        # the byte bound.
        text = "x" * (CI_FIX_LOG_MAX_BYTES * 2)
        excerpt, truncated = _bound_log_excerpt(text)
        assert truncated is True
        assert len(excerpt.encode("utf-8")) <= CI_FIX_LOG_MAX_BYTES
        assert excerpt == text[-len(excerpt):]


# ── build_ci_failure_detail ──────────────────────────────────────────────────


class _StubCiStore:
    def __init__(self, checks: list[CheckRun], jobs_by_run: dict[str, list[JobRun]]) -> None:
        self._checks = checks
        self._jobs_by_run = jobs_by_run

    def list_checks_for_pr(self, repo: str, number: int) -> list[CheckRun]:
        return self._checks

    def list_jobs_for_run(self, repo: str, run_id: str) -> list[JobRun]:
        return self._jobs_by_run.get(run_id, [])


def _failed_check(*, name: str = "Test (Linux, headless)", run_id: str = "999") -> CheckRun:
    return CheckRun(
        name=name, status="completed", conclusion="failure",
        url=f"https://github.com/acme/api/actions/runs/{run_id}",
        run_id=run_id, started_at=None, completed_at=None,
    )


class TestBuildCiFailureDetail:
    def test_no_failed_checks_returns_none(self) -> None:
        passing = CheckRun(
            name="lint", status="completed", conclusion="success",
            url="", run_id="1", started_at=None, completed_at=None,
        )
        store = _StubCiStore(checks=[passing], jobs_by_run={})
        assert build_ci_failure_detail(store, "acme/api", 42) is None

    def test_names_failing_job_and_step_and_carries_bounded_log(self) -> None:
        """#3114 black-box acceptance: the built detail names the failing
        test/job the CI-fix worker needs, straight from a fixture failed
        run — no `gh` rediscovery required."""
        check = _failed_check()
        job = JobRun(
            name=check.name, conclusion="failure", runner_name="GitHub Actions 1",
            steps=[
                JobStep(name="Set up job", conclusion="success"),
                JobStep(name="Run tests", conclusion="failure"),
            ],
            job_id="456",
        )
        store = _StubCiStore(checks=[check], jobs_by_run={"999": [job]})

        with patch(
            "coord.ci_github.github_ops.get_job_log",
            return_value="running...\nFAIL: test_i_0_ctrl_d_keys_off_last_keystroke\n",
        ) as get_log:
            detail = build_ci_failure_detail(store, "acme/api", 42)

        get_log.assert_called_once_with("acme/api", "456")
        assert detail is not None
        assert detail.check_name == check.name
        assert detail.job_name == check.name
        assert detail.step_name == "Run tests"
        assert "test_i_0_ctrl_d_keys_off_last_keystroke" in detail.log_excerpt
        assert detail.run_url == f"https://github.com/acme/api/actions/runs/999"
        assert detail.truncated is False

    def test_no_matching_job_leaves_job_and_step_empty(self) -> None:
        check = _failed_check()
        store = _StubCiStore(checks=[check], jobs_by_run={"999": []})
        detail = build_ci_failure_detail(store, "acme/api", 42)
        assert detail is not None
        assert detail.check_name == check.name
        assert detail.job_name == ""
        assert detail.step_name == ""
        assert detail.log_excerpt == ""

    def test_log_fetch_failure_degrades_to_none_not_partial(self) -> None:
        """#3114's documented fail-soft contract: a throttled/rate-limited
        log fetch falls back to "today's one-line summary" — i.e. no
        detail at all — rather than raising or handing back a partial
        detail with an empty log excerpt."""
        check = _failed_check()
        job = JobRun(
            name=check.name, conclusion="failure", runner_name="r",
            steps=[JobStep(name="Run tests", conclusion="failure")],
            job_id="456",
        )
        store = _StubCiStore(checks=[check], jobs_by_run={"999": [job]})

        with patch(
            "coord.ci_github.github_ops.get_job_log",
            side_effect=RuntimeError("gh api ... rate limited"),
        ):
            detail = build_ci_failure_detail(store, "acme/api", 42)

        assert detail is None

    def test_checks_read_failure_returns_none_not_raises(self) -> None:
        """#3114 acceptance: a raising fetcher degrades gracefully — the
        caller (`coord.commands.merge._dispatch_ci_fixes`) falls back to
        the plain `checks_summary` briefing rather than being blocked."""
        class _RaisingCiStore:
            def list_checks_for_pr(self, repo: str, number: int) -> list[CheckRun]:
                raise RuntimeError("gh throttled")

            def list_jobs_for_run(self, repo: str, run_id: str) -> list[JobRun]:
                return []

        assert build_ci_failure_detail(_RaisingCiStore(), "acme/api", 42) is None
