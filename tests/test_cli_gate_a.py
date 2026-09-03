"""CLI-level coverage for ``coord gate-a`` (#2063).

The verdict half of the Gate-A human sign-off gate: a board-recorded
approval keyed to the contract's content, mirroring ``coord test
--passed|--fail``. The refusal half it feeds lives in
``coord.milestone_dispatch.issue_oracle_ready`` (see tests/test_gate_a.py).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

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
acceptance:
  drivers:
    api:
      kind: cli-pytest
      run: pytest
"""

CONTRACT_V1 = "# Contract\n\n- the Save button says `Save`\n"
CONTRACT_V2 = "# Contract\n\n- the Save button says `Publish`\n"


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


def _run(config_file: Path, args: list[str], *, contract: str | None = CONTRACT_V1):
    def _repo_file(repo: str, path: str, branch: str | None = None) -> str:
        if path.endswith("contract.md") and contract is not None:
            return contract
        raise RuntimeError("404")

    with patch(
        "coord.github_ops.get_issue",
        return_value={"title": "ms-37 epic", "milestone": {"number": 37}, "labels": []},
    ), patch("coord.github_ops.get_repo_file", side_effect=_repo_file):
        return CliRunner().invoke(
            main, ["gate-a", *args, "--config", str(config_file)]
        )


class TestGateACommand:
    def test_approving_records_a_verdict_keyed_to_the_contract(
        self, config_file: Path, coord_db
    ) -> None:
        from coord.gate_a import contract_digest
        from coord.state import get_gate_a_approval

        result = _run(config_file, ["--approved", "api", "900"])
        assert result.exit_code == 0, result.output
        assert "approved" in result.output

        stored = get_gate_a_approval(repo_name="api", milestone_number=37)
        assert stored is not None
        assert stored["verdict"] == "approved"
        assert stored["contract_sha"] == contract_digest(CONTRACT_V1)
        assert stored["tracking_issue"] == 900

    def test_changes_records_a_rejection_with_the_note(
        self, config_file: Path, coord_db
    ) -> None:
        from coord.state import get_gate_a_approval

        result = _run(
            config_file,
            ["--changes", "api", "900", "--note", "status vocabulary is wrong"],
        )
        assert result.exit_code == 0, result.output
        stored = get_gate_a_approval(repo_name="api", milestone_number=37)
        assert stored["verdict"] == "changes"
        assert stored["note"] == "status vocabulary is wrong"
        assert "--amend" in result.output

    def test_re_approving_replaces_rather_than_appends(
        self, config_file: Path, coord_db
    ) -> None:
        from coord.state import list_gate_a_approvals

        _run(config_file, ["--changes", "api", "900"])
        _run(config_file, ["--approved", "api", "900"])
        approvals = list_gate_a_approvals()
        assert len(approvals) == 1
        assert approvals[0]["verdict"] == "approved"

    def test_read_only_invocation_reports_missing_verdict(
        self, config_file: Path, coord_db
    ) -> None:
        result = _run(config_file, ["api", "900"])
        assert result.exit_code == 1
        assert "not approved" in result.output

    def test_read_only_invocation_after_approval_exits_zero(
        self, config_file: Path, coord_db
    ) -> None:
        _run(config_file, ["--approved", "api", "900"])
        result = _run(config_file, ["api", "900"])
        assert result.exit_code == 0, result.output
        assert "approved" in result.output

    def test_read_only_invocation_surfaces_an_unmerged_amend_branch(
        self, config_file: Path, coord_db
    ) -> None:
        """#3065: the incident this issue reports — an approved contract
        reads clean while an approved-but-unmerged `--amend` branch sits on
        the board. The read path must say so, without changing the exit
        code (this is enrichment, not a new refusal)."""
        from coord.models import Assignment, Board
        from coord.state import save_board

        _run(config_file, ["--approved", "api", "900"], contract=CONTRACT_V1)

        mock_author = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=900,
            issue_title="[gate-a-amend] ms-37 — contract correction",
            assignment_id="mock-1",
            status="done",
            branch="issue-900-gate-a-amend-1",
            type="mock-author",
            dispatched_at=1000.0,
        )
        review = Assignment(
            machine_name="desktop",
            repo_name="api",
            issue_number=900,
            issue_title="review",
            assignment_id="review-1",
            status="done",
            type="review",
            review_of_assignment_id="mock-1",
            review_verdict="approve",
            dispatched_at=2000.0,
        )
        save_board(Board(completed=[mock_author, review]))

        with patch("coord.github_ops.pr_is_merged", return_value=False):
            result = _run(config_file, ["api", "900"], contract=CONTRACT_V1)

        assert result.exit_code == 0, result.output
        assert "issue-900-gate-a-amend-1" in result.output
        assert "review: approve" in result.output
        assert "NOT that branch" in result.output

    def test_read_only_invocation_omits_the_amend_note_once_merged(
        self, config_file: Path, coord_db
    ) -> None:
        """Once the branch merges, the contract on main moves and
        `evaluate()`'s own STATE_STALE takes over — this note must not
        double-report the same fact."""
        from coord.models import Assignment, Board
        from coord.state import save_board

        _run(config_file, ["--approved", "api", "900"], contract=CONTRACT_V1)

        mock_author = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=900,
            issue_title="[gate-a-amend] ms-37 — contract correction",
            assignment_id="mock-1",
            status="done",
            branch="issue-900-gate-a-amend-1",
            type="mock-author",
            dispatched_at=1000.0,
        )
        save_board(Board(completed=[mock_author]))

        with patch("coord.github_ops.pr_is_merged", return_value=True):
            result = _run(config_file, ["api", "900"], contract=CONTRACT_V1)

        assert result.exit_code == 0, result.output
        assert "waiting to merge" not in result.output

    def test_read_only_invocation_reports_a_stale_approval(
        self, config_file: Path, coord_db
    ) -> None:
        """#2063's own trap: approving v1 must not silently approve v2."""
        _run(config_file, ["--approved", "api", "900"], contract=CONTRACT_V1)
        result = _run(config_file, ["api", "900"], contract=CONTRACT_V2)
        assert result.exit_code == 1
        assert "stale" in result.output

    def test_refuses_when_the_contract_does_not_exist(
        self, config_file: Path, coord_db
    ) -> None:
        result = _run(config_file, ["--approved", "api", "900"], contract=None)
        assert result.exit_code == 1
        assert "coord acceptance mock" in result.output

    def test_unknown_repo_exits_two(self, config_file: Path, coord_db) -> None:
        result = _run(config_file, ["--approved", "nope", "900"])
        assert result.exit_code == 2

    def test_issue_without_a_milestone_exits_two(
        self, config_file: Path, coord_db
    ) -> None:
        with patch(
            "coord.github_ops.get_issue",
            return_value={"title": "orphan", "milestone": None, "labels": []},
        ):
            result = CliRunner().invoke(
                main,
                ["gate-a", "--approved", "api", "900", "--config", str(config_file)],
            )
        assert result.exit_code == 2
        assert "milestone" in result.output

    def test_verdict_is_audited(self, config_file: Path, coord_db) -> None:
        _run(config_file, ["--approved", "api", "900"])
        rows = coord_db.execute(
            "SELECT event_type, repo, issue FROM audit_log "
            "WHERE category = 'gate'"
        ).fetchall()
        assert [r["event_type"] for r in rows] == ["gate_a_approved"]
        assert rows[0]["repo"] == "api"
        assert rows[0]["issue"] == 900
