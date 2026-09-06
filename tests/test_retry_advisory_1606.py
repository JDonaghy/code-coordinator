"""#1606: `coord retry` must accept a GENUINE zero-commit ADVISORY, not just
`failed` — that was the wedge: a worker that pushed 0 commits landed on
`advisory`, and every sanctioned recovery path refused it (`coord retry`
refused non-`failed` statuses outright; `coord diagnose --stage work`
reported "healthy"; `coord drive --accept-advisory` was the only path left
and — per #1606 Part 1 — must never adopt an empty branch as done work).

An advisory whose branch DOES carry real commits (the #1357 false-positive
signature: a `done` downgraded by an artifact-glob miss) must still be
refused by `coord retry` — that shape needs `coord drive --accept-advisory`
instead, so real work is never silently discarded by a retry that assumed
the branch was empty.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from coord.cli import main
from coord.models import Assignment, Board

from .conftest import output_and_stderr


def _advisory(*, branch: str | None = "issue-544-x") -> Assignment:
    return Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=544,
        issue_title="fix the thing",
        assignment_id="0256c844edfb",
        type="work",
        status="advisory",
        branch=branch,
        model="haiku",
    )


def _retried() -> Assignment:
    return Assignment(
        machine_name="server",
        repo_name="api",
        issue_number=544,
        issue_title="[retry] fix the thing",
        assignment_id="new-retry-id",
        type="work",
        status="running",
        branch="issue-544-x",
    )


class TestRetryAcceptsZeroCommitAdvisory:
    def test_zero_commit_advisory_is_re_dispatched(self, valid_config_path: Path) -> None:
        board = Board(completed=[_advisory()])
        with (
            patch("coord.board_service.read_board", return_value=board),
            patch("coord.board_service.write_board"),
            patch("coord.github_ops.branch_commits_ahead", return_value=0) as ahead,
            patch("coord.reconcile._reassign", return_value=_retried()) as reassign,
        ):
            result = CliRunner().invoke(
                main, ["retry", "0256c844edfb", "--config", str(valid_config_path)],
            )
        out = output_and_stderr(result)
        assert result.exit_code == 0, out
        assert "Retried:" in out
        ahead.assert_called_once()
        reassign.assert_called_once()

    def test_no_branch_at_all_is_treated_as_zero_commits(
        self, valid_config_path: Path
    ) -> None:
        board = Board(completed=[_advisory(branch=None)])
        with (
            patch("coord.board_service.read_board", return_value=board),
            patch("coord.board_service.write_board"),
            patch("coord.github_ops.branch_commits_ahead") as ahead,
            patch("coord.reconcile._reassign", return_value=_retried()),
        ):
            result = CliRunner().invoke(
                main, ["retry", "0256c844edfb", "--config", str(valid_config_path)],
            )
        out = output_and_stderr(result)
        assert result.exit_code == 0, out
        # No branch to ask GitHub about — short-circuited to 0, no gh call.
        ahead.assert_not_called()

    def test_advisory_with_real_commits_is_the_1357_shape_and_is_refused(
        self, valid_config_path: Path
    ) -> None:
        board = Board(completed=[_advisory()])
        with (
            patch("coord.board_service.read_board", return_value=board),
            patch("coord.board_service.write_board"),
            patch("coord.github_ops.branch_commits_ahead", return_value=3),
        ):
            result = CliRunner().invoke(
                main, ["retry", "0256c844edfb", "--config", str(valid_config_path)],
            )
        out = output_and_stderr(result)
        assert result.exit_code == 1
        assert "3 commit(s)" in out
        assert "--accept-advisory" in out

    def test_unconfirmable_commit_count_fails_closed(self, valid_config_path: Path) -> None:
        """A `gh` lookup failure returns None (never 0) — retry must not
        gamble on discarding possibly-real work, so it refuses rather than
        silently retrying."""
        board = Board(completed=[_advisory()])
        with (
            patch("coord.board_service.read_board", return_value=board),
            patch("coord.board_service.write_board"),
            patch("coord.github_ops.branch_commits_ahead", return_value=None),
        ):
            result = CliRunner().invoke(
                main, ["retry", "0256c844edfb", "--config", str(valid_config_path)],
            )
        out = output_and_stderr(result)
        assert result.exit_code == 1
        assert "could not be confirmed" in out
        # #2324: a genuine lookup failure is not confirmation that commits
        # exist. The message must not assert the #1357 false-positive shape
        # or steer the operator at a remedy (--accept-advisory) that assumes
        # real commits are sitting on the branch — nothing here established
        # that. It should instead say what to check.
        assert "#1357" not in out
        assert "--accept-advisory" not in out
        assert "existing commits" not in out
        assert "coord log 0256c844edfb" in out

    def test_done_status_is_still_refused(self, valid_config_path: Path) -> None:
        """Control: only `failed` and `advisory` are retryable — every other
        terminal status is unchanged."""
        a = _advisory()
        a.status = "done"
        board = Board(completed=[a])
        with patch("coord.board_service.read_board", return_value=board):
            result = CliRunner().invoke(
                main, ["retry", "0256c844edfb", "--config", str(valid_config_path)],
            )
        out = output_and_stderr(result)
        assert result.exit_code == 1
        # #1606 review nit: the refusal message now names BOTH accepted
        # statuses ('failed' and 'advisory') instead of just 'failed', since
        # a genuine zero-commit advisory is retryable too.
        assert "not 'failed' or 'advisory'" in out

    def test_refused_policy_status_gets_a_bespoke_message(
        self, valid_config_path: Path
    ) -> None:
        """#2234 review nit: `coord retry` on a `refused_policy` row must
        name the reason (like the drive-queue park message does) rather
        than reusing the generic "not 'failed' or 'advisory'" refusal —
        retrying a policy refusal is exactly what #2234 says never to do,
        and the operator shouldn't need a trip to the docs/code to learn
        why."""
        a = _advisory()
        a.status = "refused_policy"
        board = Board(completed=[a])
        with patch("coord.board_service.read_board", return_value=board):
            result = CliRunner().invoke(
                main, ["retry", "0256c844edfb", "--config", str(valid_config_path)],
            )
        out = output_and_stderr(result)
        assert result.exit_code == 1
        assert "refused_policy" in out
        assert "coordinator" in out
        assert "not 'failed' or 'advisory'" not in out

    def test_refused_premise_status_gets_a_bespoke_message(
        self, valid_config_path: Path
    ) -> None:
        """#3164: sibling of `test_refused_policy_status_gets_a_bespoke_
        message` above — `coord retry` on a `refused_premise` row must name
        the reason rather than reusing the generic "not 'failed' or
        'advisory'" refusal, and must steer the operator toward re-scoping
        or closing the issue rather than the refused_policy message's
        (inapplicable here) framing."""
        a = _advisory()
        a.status = "refused_premise"
        board = Board(completed=[a])
        with patch("coord.board_service.read_board", return_value=board):
            result = CliRunner().invoke(
                main, ["retry", "0256c844edfb", "--config", str(valid_config_path)],
            )
        out = output_and_stderr(result)
        assert result.exit_code == 1
        assert "refused_premise" in out
        assert "coordinator" in out
        assert "not 'failed' or 'advisory'" not in out
