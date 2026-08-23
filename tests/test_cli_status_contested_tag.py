"""#2579: `coord status` must surface a post-approval #2464 confirmation
refutation distinctly and visibly, not as a neutral "[review done]" row.

`_confirmed_pass_verdict` (coord/notify.py) writes `test_state="contested"`
(coord.notify.TEST_STATE_CONTESTED) instead of a plain "failed" when an
independent re-run refutes a pass claim on a work row whose review already
carries a terminal "approve" verdict — this keeps the automatic fix-dispatch
door (which keys off the literal "failed") from silently bouncing an
already-approved branch back to a fix worker. But that same distinctness
means the pre-existing #1116 override in `coord status` (`test_state ==
"failed"` -> "[✗ test FAILED — needs fix]") does not fire for "contested",
so without its own tag a contested row would fall through to
`_REVIEW_STATE_TAGS.get(a.review_state, ...)` — "[review done]" in exactly
the scenario this issue is about, which reads as fine when it very much is
not (see coord/commands/status.py for the fix).
"""

from __future__ import annotations

import coord.network as network_mod
from click.testing import CliRunner

from coord.commands.status import status as status_cmd
from coord.models import Assignment, Board
from coord.state import save_board


def _work(
    aid: str = "w1",
    *,
    test_state: str | None = None,
    review_state: str = "pending",
) -> Assignment:
    return Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=2579,
        issue_title="Fix the widget",
        assignment_id=aid,
        type="work",
        status="done",
        branch=f"issue-2579-{aid}",
        test_state=test_state,
        review_state=review_state,
    )


def _run_status(valid_config_path, monkeypatch) -> str:
    monkeypatch.setattr(network_mod, "check_all", lambda *a, **k: [])
    runner = CliRunner()
    result = runner.invoke(
        status_cmd, ["--config", str(valid_config_path)], catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    return result.output


def test_contested_test_state_shows_contested_tag_not_review_done(
    valid_config_path, monkeypatch, coord_db,
) -> None:
    """The #2579 case: a confirmation refuted a pass claim AFTER its review
    already reached 'done' — the row must show the contested tag, not
    '[review done]', which would misleadingly read as fine."""
    save_board(Board(completed=[_work("w1", test_state="contested", review_state="done")]))

    output = _run_status(valid_config_path, monkeypatch)

    assert "CONTESTED" in output, output
    assert "[review done]" not in output, output


def test_contested_test_state_not_confused_with_plain_failed_tag(
    valid_config_path, monkeypatch, coord_db,
) -> None:
    """"contested" is deliberately distinct from "failed" everywhere else in
    the codebase (the automatic fix-dispatch gate keys off the literal
    "failed" string) — the display tag must keep that distinction too rather
    than collapsing onto the plain test-FAILED tag."""
    save_board(Board(completed=[_work("w1", test_state="contested", review_state="done")]))

    output = _run_status(valid_config_path, monkeypatch)

    assert "[✗ test FAILED — needs fix]" not in output, output


def test_contested_test_state_not_confused_with_other_issue(
    valid_config_path, monkeypatch, coord_db,
) -> None:
    """When multiple completed items are present, the contested tag only
    applies to the contested item; others keep their review-state tag."""
    contested_item = _work("w-contested", test_state="contested", review_state="done")
    ok_item = Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=42,
        issue_title="Other issue",
        assignment_id="w-ok",
        type="work",
        status="done",
        branch="issue-42-w-ok",
        test_state=None,
        review_state="pending",
    )
    save_board(Board(completed=[contested_item, ok_item]))

    output = _run_status(valid_config_path, monkeypatch)

    assert "CONTESTED" in output, output
    assert "[awaiting review]" in output, output
