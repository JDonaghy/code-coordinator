"""Tests for `coord pr`'s Closes/Refs briefing keyword selection (#1077).

Non-blocking nit from the #1077 review: plan_followup.py's `pr()` command
switched its hardcoded "Closes #{issue_number}" briefing text to the new
`ref_keyword`/`CLOSES_ISSUE_TYPES` branch (mirroring the deterministic
merge_queue.py/review.py behavior), but had no test coverage. This is only a
textual hint for the worker (which still runs `gh pr create` itself) — much
lower stakes than the deterministic paths, which are why this is a separate,
small test rather than blocking the #1077 fix.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

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


def _make_assignment(assignment_id: str, type: str) -> Assignment:
    return Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=42,
        issue_title="Some issue",
        assignment_id=assignment_id,
        type=type,
        status="done",
        branch="issue-42-fix",
    )


class TestPrBriefingKeyword:
    def test_uses_closes_for_work_assignment(self, config_file: Path, coord_db) -> None:
        a = _make_assignment("work-001", type="work")
        board = Board(active=[], completed=[a])
        state_mod.save_board(board)

        with (
            patch(
                "coord.commands.plan_followup._dispatch_followup", return_value="pr-001"
            ) as disp,
            patch("coord.github_ops.get_issue", return_value={"labels": []}) as get_issue,
        ):
            result = CliRunner().invoke(
                main, ["pr", "work-001", "--config", str(config_file)]
            )

        assert result.exit_code == 0, result.output
        briefing = disp.call_args[0][2]
        assert 'Closes #42' in briefing
        assert 'Refs #42' not in briefing
        # The keyword decision reads the issue's labels; the review dispatch
        # that follows may fetch the same issue again for its milestone.
        assert get_issue.call_args_list[0] == (("acme/api", 42),)

    def test_uses_refs_for_work_assignment_against_epic_issue(
        self, config_file: Path, coord_db
    ) -> None:
        """#1314: a type="work" assignment dispatched directly against a
        tracking/epic issue's own number must not get the closing keyword —
        merging it would wrongly flip the epic to "done" while its real
        children (untouched by this PR) are still open."""
        a = _make_assignment("work-002", type="work")
        board = Board(active=[], completed=[a])
        state_mod.save_board(board)

        with (
            patch(
                "coord.commands.plan_followup._dispatch_followup", return_value="pr-999"
            ) as disp,
            patch(
                "coord.github_ops.get_issue",
                return_value={"labels": [{"name": "epic"}]},
            ),
        ):
            result = CliRunner().invoke(
                main, ["pr", "work-002", "--config", str(config_file)]
            )

        assert result.exit_code == 0, result.output
        briefing = disp.call_args[0][2]
        assert 'Refs #42' in briefing
        assert 'Closes #42' not in briefing

    def test_closes_for_work_assignment_when_issue_lookup_fails(
        self, config_file: Path, coord_db
    ) -> None:
        """Fail-open: a GitHub read error must not block PR creation — fall
        back to the type-only verdict rather than raise."""
        a = _make_assignment("work-003", type="work")
        board = Board(active=[], completed=[a])
        state_mod.save_board(board)

        with (
            patch(
                "coord.commands.plan_followup._dispatch_followup", return_value="pr-998"
            ) as disp,
            patch(
                "coord.github_ops.get_issue",
                side_effect=RuntimeError("gh issue view failed"),
            ),
        ):
            result = CliRunner().invoke(
                main, ["pr", "work-003", "--config", str(config_file)]
            )

        assert result.exit_code == 0, result.output
        briefing = disp.call_args[0][2]
        assert 'Closes #42' in briefing
        assert 'Refs #42' not in briefing

    def test_uses_refs_for_mock_author_assignment(self, config_file: Path, coord_db) -> None:
        a = _make_assignment("mock-001", type="mock-author")
        board = Board(active=[], completed=[a])
        state_mod.save_board(board)

        with patch(
            "coord.commands.plan_followup._dispatch_followup", return_value="pr-002"
        ) as disp:
            result = CliRunner().invoke(
                main, ["pr", "mock-001", "--config", str(config_file)]
            )

        assert result.exit_code == 0, result.output
        briefing = disp.call_args[0][2]
        assert 'Refs #42' in briefing
        assert 'Closes #42' not in briefing


class TestPrHelperAssignmentType:
    """#1142: the PR-opening helper's own `type` must not be a bare "work"
    default when the original assignment doesn't itself resolve the issue —
    otherwise a merged helper for a test-author/mock-author original (whose
    issue_number is a milestone tracking issue) gets mistaken for that
    tracking issue's own merged work by
    `coord.stage_projection.merge_stage_status_for`'s #775 fallback.
    """

    def test_work_original_dispatches_work_helper(self, config_file: Path, coord_db) -> None:
        a = _make_assignment("work-001", type="work")
        board = Board(active=[], completed=[a])
        state_mod.save_board(board)

        with (
            patch(
                "coord.commands.plan_followup._dispatch_followup", return_value="pr-001"
            ) as disp,
            patch("coord.github_ops.get_issue", return_value={"labels": []}),
        ):
            result = CliRunner().invoke(
                main, ["pr", "work-001", "--config", str(config_file)]
            )

        assert result.exit_code == 0, result.output
        assert disp.call_args.kwargs["type"] == "work"

    def test_test_author_original_dispatches_pr_helper_type(
        self, config_file: Path, coord_db
    ) -> None:
        a = _make_assignment("ta-001", type="test-author")
        board = Board(active=[], completed=[a])
        state_mod.save_board(board)

        with patch(
            "coord.commands.plan_followup._dispatch_followup", return_value="pr-002"
        ) as disp:
            result = CliRunner().invoke(
                main, ["pr", "ta-001", "--config", str(config_file)]
            )

        assert result.exit_code == 0, result.output
        assert disp.call_args.kwargs["type"] == "pr-helper"

    def test_mock_author_original_dispatches_pr_helper_type(
        self, config_file: Path, coord_db
    ) -> None:
        a = _make_assignment("mock-002", type="mock-author")
        board = Board(active=[], completed=[a])
        state_mod.save_board(board)

        with patch(
            "coord.commands.plan_followup._dispatch_followup", return_value="pr-003"
        ) as disp:
            result = CliRunner().invoke(
                main, ["pr", "mock-002", "--config", str(config_file)]
            )

        assert result.exit_code == 0, result.output
        assert disp.call_args.kwargs["type"] == "pr-helper"
