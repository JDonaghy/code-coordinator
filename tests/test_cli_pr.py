"""Tests for `coord pr open`/`coord pr merge` (#2790).

The forge seam already has `create_pr()`/`merge_pr()` in `coord/github_ops.py`
with no CLI route for a branch with no board assignment. These tests drive
the new subcommands against a stubbed `github_ops`/`ci_store`, and confirm
the pre-existing bare `coord pr <ASSIGNMENT_ID>` (dispatch-a-PR-worker)
behaviour is unchanged now that `pr` is a `click.Group`.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from coord.ci_store import CheckRun
from coord.cli import main
from coord.models import Assignment, Board
from coord import state as state_mod

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


def _passing_check(name: str = "build") -> CheckRun:
    return CheckRun(
        name=name, status="completed", conclusion="success",
        url="", run_id="1", started_at=None, completed_at=None,
    )


def _failing_check(name: str = "build") -> CheckRun:
    return CheckRun(
        name=name, status="completed", conclusion="failure",
        url="", run_id="1", started_at=None, completed_at=None,
    )


def _pending_check(name: str = "build") -> CheckRun:
    return CheckRun(
        name=name, status="in_progress", conclusion=None,
        url="", run_id="1", started_at=None, completed_at=None,
    )


class _FakeCiStore:
    def __init__(self, checks: list[CheckRun], *, expects: bool = True, available: bool = True) -> None:
        self._checks = checks
        self._expects = expects
        self._available = available

    @property
    def is_available(self) -> bool:
        return self._available

    def list_checks_for_pr(self, repo: str, number: int) -> list[CheckRun]:
        return self._checks

    def expects_checks(self, repo: str, number: int) -> bool:
        return self._expects


class TestPrOpen:
    def test_opens_new_pr(self, config_file: Path) -> None:
        with patch(
            "coord.github_ops.create_pr",
            return_value={"number": 7, "url": "https://github.com/acme/api/pull/7", "existed": False},
        ) as create:
            result = _invoke(
                "pr", "open", "api", "--head", "throwaway", "--title", "seam test",
                "--body", "ignore", config_file=config_file,
            )

        assert result.exit_code == 0, result.output
        assert "PR #7 opened" in result.output
        create.assert_called_once_with(
            "acme/api", base="main", head="throwaway", title="seam test", body="ignore",
        )

    def test_rerun_reports_existing_pr_and_exits_zero(self, config_file: Path) -> None:
        with patch(
            "coord.github_ops.create_pr",
            return_value={"number": 7, "url": "https://github.com/acme/api/pull/7", "existed": True},
        ):
            result = _invoke(
                "pr", "open", "api", "--head", "throwaway", "--title", "seam test",
                "--body", "ignore", config_file=config_file,
            )

        assert result.exit_code == 0, result.output
        assert "already exists" in result.output
        assert "#7" in result.output

    def test_unknown_repo_lists_known_names(self, config_file: Path) -> None:
        result = _invoke(
            "pr", "open", "nope", "--head", "throwaway", "--title", "t", "--body", "b",
            config_file=config_file,
        )

        assert result.exit_code != 0
        assert "unknown repo" in result.output
        assert "api" in result.output

    def test_body_and_body_file_are_mutually_exclusive(self, config_file: Path, tmp_path: Path) -> None:
        body_file = tmp_path / "body.md"
        body_file.write_text("hi")
        result = _invoke(
            "pr", "open", "api", "--head", "throwaway", "--title", "t",
            "--body", "b", "--body-file", str(body_file),
            config_file=config_file,
        )

        assert result.exit_code != 0
        assert "mutually exclusive" in result.output


class TestPrMerge:
    def test_merges_when_checks_are_green(self, config_file: Path) -> None:
        with (
            patch("coord.github_ops.get_pr_head_ref", return_value="throwaway"),
            patch("coord.ci_store.build_ci_store", return_value=_FakeCiStore([_passing_check()])),
            patch("coord.github_ops.merge_pr", return_value=(True, "merged")) as merge,
        ):
            result = _invoke(
                "pr", "merge", "api", "7", "--method", "squash", "--delete-branch",
                config_file=config_file,
            )

        assert result.exit_code == 0, result.output
        assert "merged" in result.output
        merge.assert_called_once_with("acme/api", 7, method="squash", delete_branch=True)

    def test_refuses_when_a_check_is_failing(self, config_file: Path) -> None:
        with (
            patch("coord.github_ops.get_pr_head_ref", return_value="throwaway"),
            patch(
                "coord.ci_store.build_ci_store",
                return_value=_FakeCiStore([_failing_check("cargo-test")]),
            ),
            patch("coord.github_ops.merge_pr") as merge,
        ):
            result = _invoke("pr", "merge", "api", "7", config_file=config_file)

        assert result.exit_code != 0
        assert "cargo-test" in result.output
        merge.assert_not_called()

    def test_refuses_when_a_check_is_pending(self, config_file: Path) -> None:
        with (
            patch("coord.github_ops.get_pr_head_ref", return_value="throwaway"),
            patch(
                "coord.ci_store.build_ci_store",
                return_value=_FakeCiStore([_pending_check("cargo-test")]),
            ),
            patch("coord.github_ops.merge_pr") as merge,
        ):
            result = _invoke("pr", "merge", "api", "7", config_file=config_file)

        assert result.exit_code != 0
        assert "cargo-test" in result.output
        merge.assert_not_called()

    def test_refuses_when_checks_are_absent(self, config_file: Path) -> None:
        with (
            patch("coord.github_ops.get_pr_head_ref", return_value="throwaway"),
            patch(
                "coord.ci_store.build_ci_store",
                return_value=_FakeCiStore([], expects=True),
            ),
            patch("coord.github_ops.merge_pr") as merge,
        ):
            result = _invoke("pr", "merge", "api", "7", config_file=config_file)

        assert result.exit_code != 0
        assert "no reported checks" in result.output
        merge.assert_not_called()

    def test_force_merge_overrides_failing_checks(self, config_file: Path) -> None:
        with (
            patch("coord.github_ops.get_pr_head_ref", return_value="throwaway"),
            patch(
                "coord.ci_store.build_ci_store",
                return_value=_FakeCiStore([_failing_check()]),
            ),
            patch("coord.github_ops.merge_pr", return_value=(True, "merged")) as merge,
        ):
            result = _invoke(
                "pr", "merge", "api", "7", "--force-merge", config_file=config_file,
            )

        assert result.exit_code == 0, result.output
        merge.assert_called_once()

    def test_refuses_when_branch_has_a_merge_queue_entry(self, config_file: Path) -> None:
        from coord.merge_queue import QueuedMerge, save_queue

        entry = QueuedMerge(
            assignment_id="work-9",
            repo_name="api",
            repo_github="acme/api",
            branch="throwaway",
            target_branch="main",
            issue_number=9,
            issue_title="Some issue",
            pr_number=7,
        )
        save_queue([entry])

        with (
            patch("coord.github_ops.get_pr_head_ref", return_value="throwaway"),
            patch("coord.github_ops.merge_pr") as merge,
        ):
            result = _invoke("pr", "merge", "api", "7", config_file=config_file)

        assert result.exit_code != 0
        assert "coord merge --only work-9" in result.output
        merge.assert_not_called()

    def test_unknown_repo_lists_known_names(self, config_file: Path) -> None:
        result = _invoke("pr", "merge", "nope", "7", config_file=config_file)

        assert result.exit_code != 0
        assert "unknown repo" in result.output
        assert "api" in result.output


class TestGithubOpsMergePrDeleteBranch:
    """`github_ops.merge_pr`'s new `delete_branch` kwarg (#2790) — the actual
    `gh pr merge ... --delete-branch=...` flag construction, mocking only the
    `_gh` subprocess boundary (mirrors tests/test_github_ops.py's style)."""

    def test_defaults_to_delete_branch_false(self) -> None:
        from coord import github_ops

        with patch("coord.github_ops._gh", return_value="ok") as gh:
            ok, message = github_ops.merge_pr("acme/api", 7, method="squash")

        assert ok is True
        assert message == "ok"
        gh.assert_called_once_with(
            "pr", "merge", "7", "--repo", "acme/api", "--squash", "--delete-branch=false",
        )

    def test_delete_branch_true_passes_through(self) -> None:
        from coord import github_ops

        with patch("coord.github_ops._gh", return_value="ok") as gh:
            github_ops.merge_pr("acme/api", 7, method="squash", delete_branch=True)

        gh.assert_called_once_with(
            "pr", "merge", "7", "--repo", "acme/api", "--squash", "--delete-branch=true",
        )


class TestLegacyPrAssignmentUnchanged:
    """#2790: `pr` becoming a `click.Group` must not change the pre-existing
    `coord pr <ASSIGNMENT_ID>` behaviour (dispatch a PR-opening worker) —
    also covered end-to-end in test_plan_followup_pr_command.py."""

    def test_bare_assignment_id_still_dispatches_a_pr_worker(
        self, config_file: Path, coord_db
    ) -> None:
        a = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=42,
            issue_title="Some issue",
            assignment_id="work-001",
            type="work",
            status="done",
            branch="issue-42-fix",
        )
        board = Board(active=[], completed=[a])
        state_mod.save_board(board)

        with (
            patch(
                "coord.commands.plan_followup._dispatch_followup", return_value="pr-001"
            ) as disp,
            patch("coord.github_ops.get_issue", return_value={"labels": []}),
        ):
            result = _invoke("pr", "work-001", config_file=config_file)

        assert result.exit_code == 0, result.output
        assert "PR worker dispatched" in result.output
        disp.assert_called_once()
