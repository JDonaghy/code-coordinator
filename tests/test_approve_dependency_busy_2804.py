"""#2804: `coord approve` must not pull a build dep's single shared
checkout out from under a concurrent assignment on the same machine.

This is the CLI-level counterpart to `coord.freshness.repo_busy_elsewhere`
(unit-tested in tests/test_freshness.py) and to `coord assign`'s existing
freshness wiring (tests/test_cli_assign.py::TestAssignFreshness) — this
file is the one exercising `coord approve`'s copy of the same check.

Motivating incident: a vimcode work assignment was running on `laptop`
when a second proposal for the same repo (which `depends_on: [lib]`, standing
in for quadraui) was approved with `--auto-pull`. Before this fix,
`coord approve` would happily `git pull` the dependency's single shared
checkout out from under the running assignment — exactly the scheduling-
dependent phantom-red mechanism #2804 reports for vimcode/quadraui.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from coord.cli import main


CONFIG_YAML = """\
repos:
  - name: lib
    github: acme/lib
    default_branch: main
  - name: api
    github: acme/api
    default_branch: main
    depends_on: [lib]
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api, lib]
    repo_paths:
      api: /tmp/api
      lib: /tmp/lib
usage_gate:
  mode: disabled
"""


def _config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


def _invoke_approve(config_file: Path, *extra_args: str):
    return CliRunner().invoke(
        main, ["approve", "1", "--config", str(config_file), *extra_args]
    )


class TestApproveSkipsBusyDependencyPull:
    def test_auto_pull_skips_a_dep_a_running_assignment_relies_on(
        self, tmp_path: Path, coord_db
    ) -> None:
        from coord.models import Assignment, Board, Proposal
        from coord.state import save_board, save_proposals

        save_board(
            Board(
                active=[
                    Assignment(
                        machine_name="laptop",
                        repo_name="api",
                        issue_number=555,
                        issue_title="in flight",
                        status="running",
                    ),
                ]
            )
        )
        save_proposals(
            [
                Proposal(
                    id=1,
                    machine_name="laptop",
                    repo_name="api",
                    issue_number=2,
                    issue_title="second one",
                    rationale="work",
                    files_likely=["api/a.py"],
                ),
            ]
        )
        config_file = _config_file(tmp_path)

        with patch(
            "coord.github_ops.get_issue", return_value={"labels": []}
        ), patch(
            "coord.dispatch.dispatch_with_retry", return_value={"id": "f-1"}
        ) as mock_dispatch, patch("coord.dispatch.post_briefing"), patch(
            "coord.claim.find_work_claim", return_value=None
        ), patch(
            "coord.network.fetch_repos",
            return_value={"lib": {"sha": "OLD", "branch": "main", "dirty": False}},
        ), patch(
            "coord.github_ops.get_default_branch_head", return_value="NEW"
        ):
            result = _invoke_approve(config_file, "--auto-pull")

        assert result.exit_code == 0, result.output
        assert "not pulling" in result.output
        assert "#2804" in result.output
        _, kwargs = mock_dispatch.call_args
        assert kwargs.get("pull_repos") == []

    def test_auto_pull_still_pulls_when_nothing_else_is_running(
        self, tmp_path: Path, coord_db
    ) -> None:
        """Control case: with no concurrent dependent on the board, the
        ordinary auto-pull behaviour (pre-#2804) is unchanged."""
        from coord.models import Proposal
        from coord.state import save_proposals

        save_proposals(
            [
                Proposal(
                    id=1,
                    machine_name="laptop",
                    repo_name="api",
                    issue_number=2,
                    issue_title="only one",
                    rationale="work",
                    files_likely=["api/a.py"],
                ),
            ]
        )
        config_file = _config_file(tmp_path)

        with patch(
            "coord.github_ops.get_issue", return_value={"labels": []}
        ), patch(
            "coord.dispatch.dispatch_with_retry", return_value={"id": "f-1"}
        ) as mock_dispatch, patch("coord.dispatch.post_briefing"), patch(
            "coord.claim.find_work_claim", return_value=None
        ), patch(
            "coord.network.fetch_repos",
            return_value={"lib": {"sha": "OLD", "branch": "main", "dirty": False}},
        ), patch(
            "coord.github_ops.get_default_branch_head", return_value="NEW"
        ):
            result = _invoke_approve(config_file, "--auto-pull")

        assert result.exit_code == 0, result.output
        assert "not pulling" not in result.output
        _, kwargs = mock_dispatch.call_args
        assert kwargs.get("pull_repos") == ["lib"]
