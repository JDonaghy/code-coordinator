"""#3922: `coord gates` prints the EntryTruth-derived `next_action` — the
SAME live facts the merge gate itself would act on — alongside the existing
per-gate columns, so an operator reading `coord gates` sees what `coord
merge` actually enforces rather than a narrower "review/test/registry gates
only" summary that can read READY while the merge gate refuses.
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from coord import github_ops
from coord.ci_github import GitHubCi
from coord.commands.gates import gates as gates_cmd
from coord.models import Assignment, Board


def _work(status: str = "done") -> Assignment:
    return Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=42,
        issue_title="t",
        assignment_id="w1",
        type="work",
        status=status,
        branch="issue-42-foo",
        test_state="passed",
        review_state="done",
        review_verdict=None,
        dispatched_at=1.0,
    )


def _review() -> Assignment:
    return Assignment(
        machine_name="server",
        repo_name="api",
        issue_number=42,
        issue_title="t",
        assignment_id="r1",
        type="review",
        status="done",
        review_of_assignment_id="w1",
        review_verdict="approve",
        dispatched_at=2.0,
    )


def _patch_live_facts(monkeypatch, *, pr: dict | None, pr_merged: bool, issue_state: str):
    monkeypatch.setattr(github_ops, "get_branch_sha", lambda repo, branch: "sha123")
    monkeypatch.setattr(github_ops, "get_branch_patch_id", lambda repo, base, branch: "patchid")
    monkeypatch.setattr(github_ops, "get_issue_state", lambda repo, issue: issue_state)
    monkeypatch.setattr(github_ops, "find_pr_for_branch", lambda repo, branch: pr)
    monkeypatch.setattr(github_ops, "pr_is_merged", lambda repo, branch: pr_merged)
    monkeypatch.setattr(github_ops, "check_pr_mergeable", lambda repo, number: True)
    monkeypatch.setattr(GitHubCi, "list_checks_for_pr", lambda self, repo, number: [])


def _run_gates(valid_config_path, monkeypatch, *, as_json: bool = False):
    board = Board(active=[], completed=[_work(), _review()])
    monkeypatch.setattr("coord.state.build_board", lambda: board)

    runner = CliRunner()
    args = ["--config", str(valid_config_path), "api", "42"]
    if as_json:
        args.append("--json")
    result = runner.invoke(gates_cmd, args, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    return result.output


def test_merge_ready_entry_prints_merge_next_action(
    valid_config_path, monkeypatch, coord_db,
) -> None:
    _patch_live_facts(
        monkeypatch, pr={"number": 7, "mergeable": True}, pr_merged=False, issue_state="open",
    )
    output = _run_gates(valid_config_path, monkeypatch)
    assert "next_action: merge" in output, output


def test_closed_issue_prints_mark_done_next_action(
    valid_config_path, monkeypatch, coord_db,
) -> None:
    _patch_live_facts(monkeypatch, pr=None, pr_merged=True, issue_state="closed")
    output = _run_gates(valid_config_path, monkeypatch)
    assert "next_action: mark_done" in output, output


def test_json_output_carries_next_action_fields(
    valid_config_path, monkeypatch, coord_db,
) -> None:
    _patch_live_facts(
        monkeypatch, pr={"number": 7, "mergeable": True}, pr_merged=False, issue_state="open",
    )
    output = _run_gates(valid_config_path, monkeypatch, as_json=True)
    payload = json.loads(output)
    assert payload["next_action"] == "merge"
    assert "next_action_reason" in payload
