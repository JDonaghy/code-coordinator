"""CLI-surface tests for `coord drive` (#1392).

Flag parity with the deleted ``scripts/drive-issue.sh`` is the acceptance bar,
so the first test asserts the flag *set* rather than any single option — a
dropped flag silently breaks every operator habit and every runbook.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.commands.drive import drive
from coord.drive import EXIT_TERMINAL_FAILURE, EXIT_USAGE, DriveError


# Every flag drive-issue.sh accepted. --machine/--model/--repo-path/--merge-method
# etc. are here too, but these are the ones #1392 names explicitly.
LEGACY_FLAGS = {
    "--plan",
    "--skip-test",
    "--max-fix-rounds",
    "--accept-advisory",
    "--force-review",
    "--no-merge",
    "--dry-run",
    "--deadline",
    "--poll",
    # the rest of the bash surface
    "--machine",
    "--model",
    "--briefing-file",
    "--repo-path",
    "--max-work-retries",
    "--stall",
    "--notify",
    "--merge-method",
}


def declared_flags() -> set[str]:
    flags: set[str] = set()
    for param in drive.params:
        flags.update(param.opts)
    return flags


@pytest.mark.parametrize("flag", sorted(LEGACY_FLAGS))
def test_every_drive_issue_sh_flag_survived_the_port(flag):
    assert flag in declared_flags()


def test_drive_is_registered_on_the_main_cli():
    assert "drive" in main.commands


def test_help_renders_without_touching_the_board():
    result = CliRunner().invoke(main, ["drive", "--help"])
    assert result.exit_code == 0
    assert "Work → Test → Review → Merge" in result.output


def test_issue_must_be_a_number():
    result = CliRunner().invoke(main, ["drive", "somerepo", "not-a-number"])
    assert result.exit_code != 0


def test_a_drive_error_becomes_its_own_exit_code(monkeypatch, valid_config_path: Path):
    """DriveError's exit_code is the script contract: 2 usage, 1 terminal, 3 deadline."""

    def boom(self):
        raise DriveError("nope", EXIT_USAGE)

    monkeypatch.setattr("coord.drive.Driver.run", boom)
    result = CliRunner().invoke(
        main, ["drive", "--config", str(valid_config_path), "somerepo", "1"]
    )
    assert result.exit_code == EXIT_USAGE
    assert "nope" in result.output


def test_the_drivers_return_code_is_the_processs_exit_code(
    monkeypatch, valid_config_path: Path
):
    monkeypatch.setattr(
        "coord.drive.Driver.run", lambda self: EXIT_TERMINAL_FAILURE
    )
    result = CliRunner().invoke(
        main, ["drive", "--config", str(valid_config_path), "somerepo", "1"]
    )
    assert result.exit_code == EXIT_TERMINAL_FAILURE


def test_flags_are_threaded_into_drive_options(monkeypatch, valid_config_path: Path):
    seen: dict = {}

    def capture(self):
        seen["opts"] = self.opts
        seen["repo"] = self.repo
        seen["issue"] = self.issue
        return 0

    monkeypatch.setattr("coord.drive.Driver.run", capture)
    result = CliRunner().invoke(
        main,
        [
            "drive",
            "--config", str(valid_config_path),
            "--machine", "precision",
            "--model", "opus",
            "--plan",
            "--skip-test",
            "--accept-advisory",
            "--force-review",
            "--no-merge",
            "--merge-method", "squash",
            "--max-fix-rounds", "7",
            "--poll", "5",
            "--deadline", "30",
            "--stall", "4",
            "--notify",
            "--dry-run",
            "--no-acceptance",
            "somerepo", "1392",
        ],
    )
    assert result.exit_code == 0, result.output
    opts = seen["opts"]
    assert (seen["repo"], seen["issue"]) == ("somerepo", 1392)
    assert opts.machine == "precision"
    assert opts.model == "opus"
    assert opts.do_plan is True
    assert opts.skip_test is True
    assert opts.accept_advisory is True
    assert opts.force_review is True
    assert opts.do_merge is False  # --no-merge inverts
    assert opts.merge_method == "squash"
    assert opts.max_fix_rounds == 7
    assert opts.poll == 5
    assert opts.deadline_mins == 30
    assert opts.stall_mins == 4
    assert opts.notify is True
    assert opts.dry_run is True
    assert opts.no_acceptance is True
    # Pinned so every `coord` subprocess reads the same file this run does.
    assert opts.config_path == str(valid_config_path)


def test_no_acceptance_defaults_off(monkeypatch, valid_config_path: Path):
    """#1453: without the flag, oracle-loop JIT authoring is not skipped."""
    seen: dict = {}

    def capture(self):
        seen["opts"] = self.opts
        return 0

    monkeypatch.setattr("coord.drive.Driver.run", capture)
    result = CliRunner().invoke(
        main, ["drive", "--config", str(valid_config_path), "somerepo", "1392"]
    )
    assert result.exit_code == 0, result.output
    assert seen["opts"].no_acceptance is False


def test_merge_method_is_constrained_to_the_three_coord_merge_supports():
    result = CliRunner().invoke(main, ["drive", "--merge-method", "octopus", "r", "1"])
    assert result.exit_code != 0


# ── coord mechanic-dispatch (#4095) ────────────────────────────────────────


def test_mechanic_dispatch_is_registered_on_the_main_cli():
    assert "mechanic-dispatch" in main.commands


def test_mechanic_dispatch_refuses_on_a_thin_client(monkeypatch, valid_config_path: Path):
    monkeypatch.setattr("coord.board_service.is_remote", lambda: True)
    result = CliRunner().invoke(
        main,
        [
            "mechanic-dispatch", "--config", str(valid_config_path),
            "somerepo", "1392", "deadbeef",
        ],
    )
    assert result.exit_code != 0
    assert "thin client" in result.output or "daemon host" in result.output


def test_mechanic_dispatch_exits_zero_and_forwards_the_verdict_when_confirmed(
    monkeypatch, valid_config_path: Path
):
    """`coord.drive._decide_test` only knows "dispatched" from a zero exit —
    never from the mere absence of an exception (#2096) — so the verdict
    this command rebuilds from its flags must be the one actually handed to
    `dispatch_mechanic_leg_for_triage`, unchanged."""
    seen: dict = {}

    def fake_dispatch(
        repo_name, issue_number, board, config, *,
        verdict, branch_sha, evidence=(), failed_stage_commands=(),
    ):
        seen["repo_name"] = repo_name
        seen["issue_number"] = issue_number
        seen["verdict"] = verdict
        seen["branch_sha"] = branch_sha
        seen["evidence"] = evidence
        seen["failed_stage_commands"] = failed_stage_commands
        return True

    monkeypatch.setattr("coord.board_service.is_remote", lambda: False)
    monkeypatch.setattr("coord.state.build_board", lambda: object())
    monkeypatch.setattr(
        "coord.board_doctor_policy.dispatch_mechanic_leg_for_triage", fake_dispatch
    )
    result = CliRunner().invoke(
        main,
        [
            "mechanic-dispatch", "--config", str(valid_config_path),
            "somerepo", "1392", "deadbeef",
            "--diagnosis", "ratchet off by one",
            "--confidence", "0.9",
            "--via", "model",
            "--repro-capability", "windows",
            "--proposed-file", "tests/test_foo.py",
            "--proposed-line-delta", "1",
            "--evidence", "3 failed, 1 passed",
            "--failed-stage-command", "pytest tests/test_foo.py",
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen["repo_name"] == "somerepo"
    assert seen["issue_number"] == 1392
    assert seen["branch_sha"] == "deadbeef"
    assert seen["evidence"] == ("3 failed, 1 passed",)
    assert seen["failed_stage_commands"] == ("pytest tests/test_foo.py",)
    verdict = seen["verdict"]
    assert verdict.diagnosis == "ratchet off by one"
    assert verdict.confidence == 0.9
    assert verdict.via == "model"
    assert verdict.repro_capability == "windows"
    assert verdict.proposed_scope.files == ("tests/test_foo.py",)
    assert verdict.proposed_scope.estimated_line_delta == 1


def test_mechanic_dispatch_exits_nonzero_on_a_decline(monkeypatch, valid_config_path: Path):
    monkeypatch.setattr("coord.board_service.is_remote", lambda: False)
    monkeypatch.setattr("coord.state.build_board", lambda: object())
    monkeypatch.setattr(
        "coord.board_doctor_policy.dispatch_mechanic_leg_for_triage",
        lambda *a, **kw: False,
    )
    result = CliRunner().invoke(
        main,
        [
            "mechanic-dispatch", "--config", str(valid_config_path),
            "somerepo", "1392", "deadbeef",
        ],
    )
    assert result.exit_code != 0


# ── #4095 fix review: persist the verdict so `coord gates` shows it ───────


def test_annotate_test_reason_stamps_the_winning_work_rows_test_reason(monkeypatch):
    """`_annotate_test_reason` is the shared write both `mechanic-dispatch`
    and `escalate record --annotate-test-reason` use to persist a triage
    verdict onto the row `coord gates` already renders `test_reason` from —
    no new column, the existing `record_test_verdict` plumbing."""
    from coord.commands.drive import _annotate_test_reason
    from coord.models import Assignment, Board

    board = Board()
    board.completed.append(
        Assignment(
            machine_name="precision", repo_name="somerepo", issue_number=1392,
            issue_title="x", type="work", assignment_id="w1", status="done",
            test_state="failed", test_reason="original failure text",
            dispatched_at=1.0,
        )
    )
    captured: dict = {}

    def fake_record_test_verdict(*, assignment_id, test_state, test_reason):
        captured["assignment_id"] = assignment_id
        captured["test_state"] = test_state
        captured["test_reason"] = test_reason

    monkeypatch.setattr("coord.state.record_test_verdict", fake_record_test_verdict)

    ok = _annotate_test_reason(board, "somerepo", 1392, "[triage=mechanical] off by one")

    assert ok is True
    assert captured["assignment_id"] == "w1"
    assert captured["test_state"] == "failed"
    assert "[triage=mechanical] off by one" in captured["test_reason"]
    assert "original failure text" in captured["test_reason"]


def test_annotate_test_reason_is_a_noop_for_an_issue_with_no_work_row(monkeypatch):
    from coord.commands.drive import _annotate_test_reason
    from coord.models import Board

    calls = []
    monkeypatch.setattr(
        "coord.state.record_test_verdict", lambda **kw: calls.append(kw)
    )

    ok = _annotate_test_reason(Board(), "somerepo", 1392, "text")

    assert ok is False
    assert calls == []


def test_escalate_record_annotate_test_reason_calls_the_shared_helper(
    monkeypatch, valid_config_path: Path
):
    """`escalate record --annotate-test-reason` is the `human`-bucket call
    site for the same persistence `mechanic-dispatch` does for the
    `mechanical` bucket."""
    seen: dict = {}

    monkeypatch.setattr("coord.state.record_drive_escalation", lambda *a, **kw: 1)
    monkeypatch.setattr(
        "coord.commands.drive._board_for_annotation", lambda: object()
    )
    monkeypatch.setattr(
        "coord.commands.drive._annotate_test_reason",
        lambda board, repo, issue, text: seen.update(
            board=board, repo=repo, issue=issue, text=text
        ),
    )
    result = CliRunner().invoke(
        main,
        [
            "escalate", "record", "somerepo", "1392",
            "--stage", "test",
            "--reason", "test_triage_human — ambiguous spec",
            "--command", "coord log w1",
            "--assignment", "w1",
            "--annotate-test-reason", "test_triage_human — ambiguous spec",
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen["repo"] == "somerepo"
    assert seen["issue"] == 1392
    assert seen["text"] == "test_triage_human — ambiguous spec"
