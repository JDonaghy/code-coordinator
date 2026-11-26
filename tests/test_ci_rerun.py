"""Tests for `coord ci rerun`'s structured outcomes and `--wait` (#4165).

#4165's incident shape (2026-11-26, JDonaghy/vimcode#2199): `coord ci
rerun REPO PR --failed` printed only the bare `rerun failed for REPO#PR`
whether the run was still in progress, `gh` genuinely refused, or discovery
found nothing to rerun at all — and, separately, a `--failed` attempt on a
COMPLETED run with one failed job among several still failed silently,
because discovery used the merge-gate-narrowed (required-contexts-only)
check set instead of every reported check.

This file covers:
- `github_ops.GhRerunResult`/`_rerun_gh`: reason + in-progress classification
  from `gh`'s stderr (the primitive everything else builds on).
- `GitHubCi.rerun_failed_for_pr_detail`: the #2199 regression — one failed,
  non-required job among several on a completed run must still be found and
  rerun.
- `coord ci rerun`'s CLI surface: every failure shape prints a specific
  reason, and `--wait` polls an in-progress run to completion and retries.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from coord import github_ops
from coord.ci_github import GitHubCi
from coord.ci_store import RerunOutcome
from coord.cli import main


def _gh_result(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# ── github_ops.GhRerunResult / _rerun_gh ────────────────────────────────────


class TestGhRerunResult:
    """`github_ops.rerun_workflow_run_detail`/`*_failed_detail`/`*_job_detail`
    — the structured primitives the bare-bool wrappers now delegate to."""

    def test_success_carries_no_reason(self) -> None:
        with patch("coord.github_ops.subprocess.run", return_value=_gh_result("")):
            result = github_ops.rerun_workflow_run_detail("acme/api", "123")
        assert result.ok is True
        assert result.reason is None
        assert result.in_progress is False
        assert bool(result) is True  # bool-compatible

    def test_generic_refusal_surfaces_stderr_first_line(self) -> None:
        with patch(
            "coord.github_ops.subprocess.run",
            return_value=_gh_result("", returncode=1, stderr="permission denied\nmore detail"),
        ):
            result = github_ops.rerun_workflow_run_detail("acme/api", "123")
        assert result.ok is False
        assert result.reason == "permission denied"
        assert result.in_progress is False
        assert bool(result) is False

    def test_in_progress_refusal_is_classified(self) -> None:
        with patch(
            "coord.github_ops.subprocess.run",
            return_value=_gh_result(
                "", returncode=1, stderr="run 45856678050 is still in progress",
            ),
        ):
            result = github_ops.rerun_workflow_run_failed_detail("acme/api", "45856678050")
        assert result.ok is False
        assert result.in_progress is True
        assert "still in progress" in result.reason

    def test_in_progress_refusal_classified_against_repos_own_fixture_wording(self) -> None:
        """This repo's pre-#4165 fixtures for this exact `gh` refusal
        (`tests/test_github_ops.py`, `tests/test_ci_store.py`) use "run
        already in progress" — `_is_run_in_progress_stderr`'s marker list
        must recognize that wording too, not only the synthetic "is still
        in progress" phrasing used elsewhere in this file."""
        with patch(
            "coord.github_ops.subprocess.run",
            return_value=_gh_result("", returncode=1, stderr="run already in progress"),
        ):
            result = github_ops.rerun_workflow_run_failed_detail("acme/api", "12345")
        assert result.ok is False
        assert result.in_progress is True

    def test_gh_missing_is_not_in_progress(self) -> None:
        with patch("coord.github_ops.subprocess.run", side_effect=FileNotFoundError("no gh")):
            result = github_ops.rerun_workflow_run_job_detail("acme/api", "123", "99")
        assert result.ok is False
        assert result.in_progress is False
        assert result.reason

    def test_bool_wrappers_stay_plain_bool(self) -> None:
        """Pre-#4165 callers (`coord.merge_queue`, `coord.ci_fix`) treat
        these as plain bools — the wrappers must return a real `bool`, not
        just something bool-compatible."""
        with patch("coord.github_ops.subprocess.run", return_value=_gh_result("")):
            assert github_ops.rerun_workflow_run("acme/api", "123") is True
        with patch(
            "coord.github_ops.subprocess.run",
            return_value=_gh_result("", returncode=1, stderr="boom"),
        ):
            assert github_ops.rerun_workflow_run_failed("acme/api", "123") is False


class TestGetRunStatus:
    """`github_ops.get_run_status` — the read `coord ci rerun --wait` polls."""

    def test_parses_status(self) -> None:
        with patch(
            "coord.github_ops.subprocess.run",
            return_value=_gh_result(json.dumps({"status": "completed"})),
        ):
            assert github_ops.get_run_status("acme/api", "123") == "completed"

    def test_failure_degrades_to_empty_string(self) -> None:
        with patch("coord.github_ops.subprocess.run", side_effect=FileNotFoundError):
            assert github_ops.get_run_status("acme/api", "123") == ""


# ── GitHubCi.rerun_failed_for_pr_detail: the #2199 regression ──────────────


_REAL_REQUIRED_CONTEXTS = GitHubCi._required_contexts


def _checks_payload(entries: list[dict]) -> str:
    return json.dumps(entries)


class TestRerunFailedForPrAdvisoryCheckRegression:
    """#2199's exact shape: a multi-job Actions run on a completed PR where
    ONE job ("Smoke (GTK, Xvfb)") failed and is not (yet) a required status
    check — only an advisory one. `rerun_failed_for_pr`/`_detail` must still
    find and rerun it: discovery goes through `list_all_checks_for_pr`
    (every reported check), not the merge-gate-narrowed
    `list_checks_for_pr` (required contexts only), which is exactly what
    silently excluded this check before #4165.
    """

    def _payload(self) -> str:
        run_id = "45856678050"
        return _checks_payload([
            {
                "name": "Build", "state": "SUCCESS", "bucket": "pass",
                "link": f"https://github.com/JDonaghy/vimcode/actions/runs/{run_id}",
                "startedAt": "2026-11-26T10:00:00Z", "completedAt": "2026-11-26T10:05:00Z",
            },
            {
                "name": "Smoke (GTK, Xvfb)", "state": "FAILURE", "bucket": "fail",
                "link": f"https://github.com/JDonaghy/vimcode/actions/runs/{run_id}",
                "startedAt": "2026-11-26T10:00:00Z", "completedAt": "2026-11-26T10:06:00Z",
            },
        ])

    def test_detail_reruns_the_advisory_failing_job(self, monkeypatch) -> None:
        # Only "Build" is required — "Smoke (GTK, Xvfb)" is advisory, the
        # exact shape that made `list_checks_for_pr` (narrowed) omit it.
        monkeypatch.setattr(
            GitHubCi, "_required_contexts", lambda self, repo: frozenset({"Build"}),
        )
        store = GitHubCi()
        with patch("coord.ci_github.subprocess.run") as run:
            run.side_effect = [
                _gh_result(self._payload()),  # list_all_checks_for_pr
                _gh_result("", returncode=0),  # gh run rerun 45856678050 --failed
            ]
            outcome = store.rerun_failed_for_pr_detail("JDonaghy/vimcode", 2199)
        assert outcome.ok is True
        assert outcome.per_run == (("45856678050", True, None),)
        rerun_calls = [c for c in run.call_args_list if c.args[0][:3] == ["gh", "run", "rerun"]]
        assert len(rerun_calls) == 1
        assert rerun_calls[0].args[0] == [
            "gh", "run", "rerun", "45856678050", "--repo", "JDonaghy/vimcode", "--failed",
        ]

    def test_bool_wrapper_also_succeeds(self, monkeypatch) -> None:
        """The pre-#4165 bool contract (`rerun_failed_for_pr`) gets the fix
        for free since it now just projects `.ok` off the `_detail` sibling
        that discovers off the full check set."""
        monkeypatch.setattr(
            GitHubCi, "_required_contexts", lambda self, repo: frozenset({"Build"}),
        )
        store = GitHubCi()
        with patch("coord.ci_github.subprocess.run") as run:
            run.side_effect = [_gh_result(self._payload()), _gh_result("")]
            assert store.rerun_failed_for_pr("JDonaghy/vimcode", 2199) is True


class TestRerunFailedForPrDetailReasons:
    """Every discovery/refusal shape must surface a SPECIFIC reason, never
    a bare unexplained failure (#4165 acceptance)."""

    def test_no_checks_at_all(self, monkeypatch) -> None:
        monkeypatch.setattr(GitHubCi, "_required_contexts", lambda self, repo: None)
        store = GitHubCi()
        with patch("coord.ci_github.subprocess.run", return_value=_gh_result("[]")):
            outcome = store.rerun_failed_for_pr_detail("acme/api", 42)
        assert outcome.ok is False
        assert outcome.reason == "no failing checks found"
        assert outcome.in_progress is False

    def test_in_progress_run_is_reported_as_such(self, monkeypatch) -> None:
        monkeypatch.setattr(GitHubCi, "_required_contexts", lambda self, repo: None)
        payload = _checks_payload([{
            "name": "test", "state": "FAILURE", "bucket": "fail",
            "link": "https://github.com/acme/api/actions/runs/777",
            "startedAt": "2026-11-26T10:00:00Z", "completedAt": "2026-11-26T10:06:00Z",
        }])
        store = GitHubCi()
        with patch("coord.ci_github.subprocess.run") as run:
            run.side_effect = [
                _gh_result(payload),
                _gh_result("", returncode=1, stderr="run 777 is already running"),
            ]
            outcome = store.rerun_failed_for_pr_detail("acme/api", 42)
        assert outcome.ok is False
        assert outcome.in_progress is True
        assert "777" in outcome.reason

    def test_gh_refusal_surfaces_stderr(self, monkeypatch) -> None:
        monkeypatch.setattr(GitHubCi, "_required_contexts", lambda self, repo: None)
        payload = _checks_payload([{
            "name": "test", "state": "FAILURE", "bucket": "fail",
            "link": "https://github.com/acme/api/actions/runs/777",
            "startedAt": "2026-11-26T10:00:00Z", "completedAt": "2026-11-26T10:06:00Z",
        }])
        store = GitHubCi()
        with patch("coord.ci_github.subprocess.run") as run:
            run.side_effect = [
                _gh_result(payload),
                _gh_result("", returncode=1, stderr="HTTP 403: must have admin rights"),
            ]
            outcome = store.rerun_failed_for_pr_detail("acme/api", 42)
        assert outcome.ok is False
        assert outcome.in_progress is False
        assert outcome.reason == "gh refused: HTTP 403: must have admin rights"


# ── CLI: `coord ci rerun` surfaces a specific reason, and --wait retries ───

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


def _invoke(*args: str, config_file: Path):
    return CliRunner().invoke(main, [*args, "--config", str(config_file)])


class _DetailCiStore:
    """A `CiStore` stand-in implementing the #4165 `_detail` surface
    directly, with scripted outcomes/statuses — the CLI-level counterpart
    to `GitHubCi`'s own tests above."""

    def __init__(self, outcomes: list[RerunOutcome], statuses: list[str] | None = None) -> None:
        self._outcomes = list(outcomes)
        self._statuses = list(statuses or [])
        self.rerun_failed_for_pr_detail_calls = 0
        self.get_run_status_calls: list[tuple] = []

    def rerun_failed_for_pr_detail(self, repo, number) -> RerunOutcome:
        self.rerun_failed_for_pr_detail_calls += 1
        if len(self._outcomes) > 1:
            return self._outcomes.pop(0)
        return self._outcomes[0]

    def get_run_status(self, repo, run_id) -> str:
        self.get_run_status_calls.append((repo, run_id))
        if len(self._statuses) > 1:
            return self._statuses.pop(0)
        return self._statuses[0] if self._statuses else ""


class TestCliRerunReasons:
    def test_no_failing_checks_prints_specific_reason(self, config_file: Path) -> None:
        store = _DetailCiStore([RerunOutcome(ok=False, reason="no failing checks found")])
        with patch("coord.ci_store.build_ci_store", return_value=store):
            result = _invoke("ci", "rerun", "api", "7", "--failed", config_file=config_file)
        assert result.exit_code != 0
        assert "no failing checks found" in result.output

    def test_gh_refusal_prints_the_stderr_reason(self, config_file: Path) -> None:
        store = _DetailCiStore(
            [RerunOutcome(ok=False, reason="gh refused: HTTP 403: must have admin rights")],
        )
        with patch("coord.ci_store.build_ci_store", return_value=store):
            result = _invoke("ci", "rerun", "api", "7", "--failed", config_file=config_file)
        assert result.exit_code != 0
        assert "gh refused: HTTP 403: must have admin rights" in result.output

    def test_in_progress_without_wait_hints_at_the_flag(self, config_file: Path) -> None:
        store = _DetailCiStore([
            RerunOutcome(
                ok=False, reason="run 777 is still in progress", in_progress=True,
                per_run=(("777", False, "run 777 is already running"),),
            ),
        ])
        with patch("coord.ci_store.build_ci_store", return_value=store):
            result = _invoke("ci", "rerun", "api", "7", "--failed", config_file=config_file)
        assert result.exit_code != 0
        assert "still in progress" in result.output
        assert "--wait" in result.output

    def test_wait_polls_until_completion_then_retries(self, config_file: Path) -> None:
        in_progress = RerunOutcome(
            ok=False, reason="run 777 is still in progress", in_progress=True,
            per_run=(("777", False, "run 777 is already running"),),
        )
        success = RerunOutcome(ok=True, per_run=(("777", True, None),))
        store = _DetailCiStore([in_progress, success], statuses=["in_progress", "completed"])
        with patch("coord.ci_store.build_ci_store", return_value=store), \
             patch("coord.commands.ci.time.sleep") as sleep_mock:
            result = _invoke(
                "ci", "rerun", "api", "7", "--failed", "--wait", config_file=config_file,
            )
        assert result.exit_code == 0
        assert "rerun triggered" in result.output
        assert store.rerun_failed_for_pr_detail_calls == 2
        assert sleep_mock.call_count >= 1

    def test_wait_gives_up_after_max_polls_still_in_progress(
        self, config_file: Path, monkeypatch,
    ) -> None:
        import coord.commands.ci as ci_module

        monkeypatch.setattr(ci_module, "_WAIT_MAX_POLLS", 2)
        in_progress = RerunOutcome(
            ok=False, reason="run 777 is still in progress", in_progress=True,
            per_run=(("777", False, "run 777 is already running"),),
        )
        store = _DetailCiStore([in_progress], statuses=["in_progress"])
        with patch("coord.ci_store.build_ci_store", return_value=store), \
             patch("coord.commands.ci.time.sleep"):
            result = _invoke(
                "ci", "rerun", "api", "7", "--failed", "--wait", config_file=config_file,
            )
        assert result.exit_code != 0
        assert "still in progress" in result.output
        # Only the original attempt — never got to a completed status, so
        # `attempt()` must not have been called a second time.
        assert store.rerun_failed_for_pr_detail_calls == 1

    def test_backend_without_detail_method_falls_back_to_bool(self, config_file: Path) -> None:
        """A `CiStore` that predates #4165's `_detail` surface (a GitLab
        backend, an old test stub) must still work through the plain bool
        method — no reason, but no crash either."""

        class _BoolOnlyCiStore:
            def rerun_failed_for_pr(self, repo, number):
                return False

        with patch("coord.ci_store.build_ci_store", return_value=_BoolOnlyCiStore()):
            result = _invoke("ci", "rerun", "api", "7", "--failed", config_file=config_file)
        assert result.exit_code != 0
        assert "rerun failed" in result.output
