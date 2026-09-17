"""#3360: `coord retry` must classify what actually failed before climbing
the model-escalation ladder — the same rule ``coord fix``'s Test/CI arm
already applies. A compliance nit (a ratchet, a lint/formatter check, a
``files_forbidden`` violation) re-dispatches at the SAME model rung; only a
genuine behavioural failure climbs.

This is one of the three "escalation-gated" doors the codebase's own comment
names alongside ``coord fix`` / ``coord resume-stuck`` (``coord/models.py``,
near the ``failure_reason`` field) — the #3360 review flagged it as
completely unwired in the first pass.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from coord.cli import main
from coord.models import Assignment, Board

from .conftest import output_and_stderr


def _failed(*, failure_reason: str | None) -> Assignment:
    return Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=42,
        issue_title="fix the thing",
        assignment_id="failedid",
        type="work",
        status="failed",
        branch="issue-42-fix-the-thing",
        model="sonnet",
        failure_reason=failure_reason,
    )


def _retried() -> Assignment:
    return Assignment(
        machine_name="server",
        repo_name="api",
        issue_number=42,
        issue_title="[retry] fix the thing",
        assignment_id="new-retry-id",
        type="work",
        status="running",
        branch="issue-42-fix-the-thing",
    )


class TestRetryClassifiesBeforeEscalating:
    def test_ratchet_failure_does_not_escalate(self, valid_config_path: Path) -> None:
        board = Board(completed=[_failed(
            failure_reason=(
                "FAILED tests/test_sqlite_connect_ratchet.py::"
                "test_sqlite_connect_site_counts_are_pinned - the number of "
                "sqlite3.connect call sites changed"
            ),
        )])
        with (
            patch("coord.board_service.read_board", return_value=board),
            patch("coord.board_service.write_board"),
            patch("coord.reconcile._reassign", return_value=_retried()) as reassign,
        ):
            result = CliRunner().invoke(
                main, ["retry", "failedid", "--config", str(valid_config_path)],
            )
        out = output_and_stderr(result)
        assert result.exit_code == 0, out
        assert "not escalating model" in out
        assert "compliance" in out
        reassign.assert_called_once()
        # Stays on the original rung ("sonnet"), not escalated to "opus".
        assert reassign.call_args.kwargs["model"] == "sonnet"

    def test_behavioural_failure_still_escalates(self, valid_config_path: Path) -> None:
        board = Board(completed=[_failed(
            failure_reason=(
                "FAILED tests/test_widget.py::test_returns_sorted - "
                "AssertionError: assert [3, 1, 2] == [1, 2, 3]"
            ),
        )])
        with (
            patch("coord.board_service.read_board", return_value=board),
            patch("coord.board_service.write_board"),
            patch("coord.reconcile._reassign", return_value=_retried()) as reassign,
        ):
            result = CliRunner().invoke(
                main, ["retry", "failedid", "--config", str(valid_config_path)],
            )
        out = output_and_stderr(result)
        assert result.exit_code == 0, out
        assert "escalating model: sonnet → opus" in out
        reassign.assert_called_once()
        assert reassign.call_args.kwargs["model"] == "opus"

    def test_no_failure_text_defaults_to_not_escalating(
        self, valid_config_path: Path
    ) -> None:
        """#3360 acceptance: 'default to NOT escalating' when there's no
        evidence to classify — the asymmetric cost of a wrong escalation
        (the top rung, #3357) outweighs a wrong same-rung retry."""
        board = Board(completed=[_failed(failure_reason=None)])
        with (
            patch("coord.board_service.read_board", return_value=board),
            patch("coord.board_service.write_board"),
            patch("coord.reconcile._reassign", return_value=_retried()) as reassign,
        ):
            result = CliRunner().invoke(
                main, ["retry", "failedid", "--config", str(valid_config_path)],
            )
        out = output_and_stderr(result)
        assert result.exit_code == 0, out
        assert "not escalating model" in out
        reassign.assert_called_once()
        assert reassign.call_args.kwargs["model"] == "sonnet"
