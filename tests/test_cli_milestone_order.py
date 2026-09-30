"""Black-box tests for `coord milestone order` (#768 Phase 0 CLI glue).

Mocks `coord.github_ops` (no live `gh` calls) and `coord.board_service`
so the test drives the real Click command end to end: fetch tracking issue
-> parse work order -> resolve membership/terminal state -> print DAG +
ready frontier. Board reads go through `board_service.read_board()` (#615
thin-client seam), not `coord.state.load_board()` directly — see
tests/test_thin_client_board_audit.py.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.models import Board


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


CONFIG_YAML_WITH_ACCEPTANCE_DRIVER = CONFIG_YAML + """\
acceptance:
  drivers:
    api:
      kind: tui-tuidriver
      run: "cargo test"
      mock: "*.screen"
"""


@pytest.fixture
def config_file_with_gate_a(tmp_path: Path) -> Path:
    """A repo under oracle-loop control (#2542) — an `acceptance.drivers`
    entry configured, mirroring tests/test_cli_milestone_dispatch.py's
    identically-named fixture."""
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML_WITH_ACCEPTANCE_DRIVER)
    return p


TRACKING_BODY = """\
Milestone plan.

## Work order
- [ ] #762  {group: A}
- [ ] #763  {group: A}
- [ ] #765  {after: #762,#763}
"""


def _get_issue(repo, number, *, milestone_number=9, states=None, bodies=None):
    states = states or {}
    bodies = bodies or {}
    if number == 100:
        return {
            "number": 100,
            "title": "tracking",
            "body": bodies.get(100, TRACKING_BODY),
            "state": "OPEN",
            "milestone": {"number": milestone_number, "title": "M"},
        }
    return {
        "number": number,
        "title": f"issue {number}",
        "body": bodies.get(number, ""),
        "state": states.get(number, "OPEN"),
        "milestone": {"number": milestone_number, "title": "M"},
    }


class TestMilestoneOrderCmd:
    def test_prints_dag_and_ready_frontier(self, config_file: Path) -> None:
        open_issues = [
            {"number": 762, "milestone": {"number": 9}},
            {"number": 763, "milestone": {"number": 9}},
            {"number": 765, "milestone": {"number": 9}},
        ]
        with patch("coord.github_ops.get_issue", side_effect=_get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.board_service.read_board", return_value=Board()):
            result = CliRunner().invoke(
                main,
                ["milestone", "order", "api", "100", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        assert "#762" in result.output
        assert "#763" in result.output
        assert "#765" in result.output
        assert "Ready frontier:" in result.output
        # 765 depends on 762+763, neither terminal -> blocked, not ready.
        ready_section = result.output.split("Ready frontier:")[1]
        blocked_section = ready_section.split("Blocked:")[1] if "Blocked:" in ready_section else ""
        assert "#765" in blocked_section
        assert "waiting on #762, #763" in blocked_section

    def test_unknown_repo_errors(self, config_file: Path) -> None:
        result = CliRunner().invoke(
            main,
            ["milestone", "order", "nope", "100", "--config", str(config_file)],
        )
        assert result.exit_code == 2
        assert "unknown repo" in result.output

    def test_tracking_issue_without_milestone_errors(self, config_file: Path) -> None:
        def get_issue_no_milestone(repo, number):
            return {"number": number, "title": "t", "body": TRACKING_BODY, "state": "OPEN", "milestone": None}

        with patch("coord.github_ops.get_issue", side_effect=get_issue_no_milestone):
            result = CliRunner().invoke(
                main,
                ["milestone", "order", "api", "100", "--config", str(config_file)],
            )
        assert result.exit_code == 1
        assert "no milestone" in result.output

    def test_no_work_order_block_reports_and_exits_zero(self, config_file: Path) -> None:
        def get_issue_no_block(repo, number):
            return {
                "number": number,
                "title": "t",
                "body": "just prose, no work order here",
                "state": "OPEN",
                "milestone": {"number": 9, "title": "M"},
            }

        with patch("coord.github_ops.get_issue", side_effect=get_issue_no_block):
            result = CliRunner().invoke(
                main,
                ["milestone", "order", "api", "100", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        assert "no `## Work order` block found" in result.output

    def test_foreign_issue_in_work_order_errors(self, config_file: Path) -> None:
        body = "## Work order\n- [ ] #762\n- [ ] #999\n"

        def get_issue(repo, number):
            if number == 100:
                return {
                    "number": 100, "title": "tracking", "body": body,
                    "state": "OPEN", "milestone": {"number": 9, "title": "M"},
                }
            if number == 999:
                # Belongs to a different milestone entirely.
                return {
                    "number": 999, "title": "foreign", "body": "",
                    "state": "OPEN", "milestone": {"number": 42, "title": "Other"},
                }
            return {
                "number": number, "title": "x", "body": "",
                "state": "OPEN", "milestone": {"number": 9, "title": "M"},
            }

        open_issues = [{"number": 762, "milestone": {"number": 9}}]
        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues):
            result = CliRunner().invoke(
                main,
                ["milestone", "order", "api", "100", "--config", str(config_file)],
            )
        assert result.exit_code == 1
        assert "#999" in result.output
        assert "not an issue under this milestone" in result.output

    def test_cycle_in_work_order_errors(self, config_file: Path) -> None:
        body = "## Work order\n- [ ] #1 {after: #2}\n- [ ] #2 {after: #1}\n"

        def get_issue(repo, number):
            return {
                "number": number, "title": "tracking", "body": body,
                "state": "OPEN", "milestone": {"number": 9, "title": "M"},
            }

        with patch("coord.github_ops.get_issue", side_effect=get_issue):
            result = CliRunner().invoke(
                main,
                ["milestone", "order", "api", "100", "--config", str(config_file)],
            )
        assert result.exit_code == 1
        assert "cycle" in result.output


# ── `coord milestone write-order` (#770 Phase 2 write path) ────────────────


class TestMilestoneWriteOrderCmd:
    def test_writes_valid_block_via_stdin(self, config_file: Path) -> None:
        tracking_body = "Milestone plan.\n\n## Refs\nsome refs\n"

        def get_issue(repo, number):
            return {
                "number": 100, "title": "tracking", "body": tracking_body,
                "state": "OPEN", "milestone": {"number": 9, "title": "M"},
                "labels": [{"name": "epic"}],
            }

        open_issues = [
            {"number": 762, "milestone": {"number": 9}},
            {"number": 763, "milestone": {"number": 9}},
        ]
        new_block = "- [ ] #762  {group: A}\n- [ ] #763  {after: #762}\n"
        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.github_ops.add_issue_labels") as mock_add_labels, \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "write-order", "api", "100", "--config", str(config_file)],
                input=new_block,
            )
        assert result.exit_code == 0, result.output
        assert "wrote `## Work order` block (2 node(s))" in result.output
        mock_update.assert_called_once()
        call_repo, call_issue, call_body = mock_update.call_args[0]
        assert call_repo == "acme/api"
        assert call_issue == 100
        assert "Milestone plan." in call_body
        assert "## Refs\nsome refs" in call_body
        assert "#762" in call_body and "#763" in call_body
        # #1057: tracking issue already carries `epic` — no-op.
        mock_add_labels.assert_not_called()

    def test_writes_from_file_option(self, config_file: Path, tmp_path: Path) -> None:
        block_file = tmp_path / "order.txt"
        block_file.write_text("- [ ] #762\n")

        def get_issue(repo, number):
            return {
                "number": 100, "title": "tracking", "body": "",
                "state": "OPEN", "milestone": {"number": 9, "title": "M"},
                "labels": [{"name": "epic"}],
            }

        open_issues = [{"number": 762, "milestone": {"number": 9}}]
        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.github_ops.add_issue_labels") as mock_add_labels, \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                [
                    "milestone", "write-order", "api", "100",
                    "--file", str(block_file), "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        mock_update.assert_called_once()
        mock_add_labels.assert_not_called()

    def test_idempotent_rewrite_is_a_noop(self, config_file: Path) -> None:
        tracking_body = "## Work order\n- [ ] #762  {group: A}\n"

        def get_issue(repo, number):
            return {
                "number": 100, "title": "tracking", "body": tracking_body,
                "state": "OPEN", "milestone": {"number": 9, "title": "M"},
                "labels": [{"name": "epic"}],
            }

        open_issues = [{"number": 762, "milestone": {"number": 9}}]
        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.github_ops.add_issue_labels") as mock_add_labels, \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "write-order", "api", "100", "--config", str(config_file)],
                input="- [ ] #762  {group: A}\n",
            )
        assert result.exit_code == 0, result.output
        assert "unchanged (idempotent no-op)" in result.output
        mock_update.assert_not_called()
        mock_add_labels.assert_not_called()

    def test_adds_missing_epic_label_even_when_body_unchanged(
        self, config_file: Path
    ) -> None:
        """#1057 repro: #1051 had a valid `## Work order` already written but
        was still flagged `no_work_order` in `coord plans` because the
        tracking issue never got the `epic` label. Re-running `write-order`
        with the same content must self-heal the label even though the body
        splice itself is a no-op."""
        tracking_body = "## Work order\n- [ ] #762  {group: A}\n"

        def get_issue(repo, number):
            return {
                "number": 100, "title": "tracking", "body": tracking_body,
                "state": "OPEN", "milestone": {"number": 9, "title": "M"},
                "labels": [],
            }

        open_issues = [{"number": 762, "milestone": {"number": 9}}]
        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.github_ops.add_issue_labels") as mock_add_labels, \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "write-order", "api", "100", "--config", str(config_file)],
                input="- [ ] #762  {group: A}\n",
            )
        assert result.exit_code == 0, result.output
        assert "added missing 'epic' label" in result.output
        assert "unchanged (idempotent no-op)" in result.output
        mock_add_labels.assert_called_once_with("acme/api", 100, ["epic"])
        mock_update.assert_not_called()

    def test_adds_missing_epic_label_on_a_real_write(self, config_file: Path) -> None:
        def get_issue(repo, number):
            return {
                "number": 100, "title": "tracking", "body": "",
                "state": "OPEN", "milestone": {"number": 9, "title": "M"},
                "labels": [{"name": "coord"}],
            }

        open_issues = [{"number": 762, "milestone": {"number": 9}}]
        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.github_ops.add_issue_labels") as mock_add_labels, \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "write-order", "api", "100", "--config", str(config_file)],
                input="- [ ] #762  {group: A}\n",
            )
        assert result.exit_code == 0, result.output
        assert "added missing 'epic' label" in result.output
        assert "wrote `## Work order` block (1 node(s))" in result.output
        mock_add_labels.assert_called_once_with("acme/api", 100, ["epic"])
        mock_update.assert_called_once()

    def test_refuses_to_write_a_cycle(self, config_file: Path) -> None:
        def get_issue(repo, number):
            return {
                "number": 100, "title": "tracking", "body": "",
                "state": "OPEN", "milestone": {"number": 9, "title": "M"},
            }

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "write-order", "api", "100", "--config", str(config_file)],
                input="- [ ] #1 {after: #2}\n- [ ] #2 {after: #1}\n",
            )
        assert result.exit_code == 1
        assert "cycle" in result.output
        mock_update.assert_not_called()

    def test_refuses_foreign_issue(self, config_file: Path) -> None:
        def get_issue(repo, number):
            if number == 100:
                return {
                    "number": 100, "title": "tracking", "body": "",
                    "state": "OPEN", "milestone": {"number": 9, "title": "M"},
                }
            return {
                "number": number, "title": "x", "body": "",
                "state": "OPEN", "milestone": {"number": 42, "title": "Other"},
            }

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=[]), \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "write-order", "api", "100", "--config", str(config_file)],
                input="- [ ] #999\n",
            )
        assert result.exit_code == 1
        assert "not an issue under this milestone" in result.output
        mock_update.assert_not_called()

    def test_refuses_empty_block(self, config_file: Path) -> None:
        with patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "write-order", "api", "100", "--config", str(config_file)],
                input="",
            )
        assert result.exit_code == 2
        assert "no work-order content" in result.output
        mock_update.assert_not_called()

    def test_tracking_issue_without_milestone_errors(self, config_file: Path) -> None:
        def get_issue_no_milestone(repo, number):
            return {"number": number, "title": "t", "body": "", "state": "OPEN", "milestone": None}

        with patch("coord.github_ops.get_issue", side_effect=get_issue_no_milestone), \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "write-order", "api", "100", "--config", str(config_file)],
                input="- [ ] #1\n",
            )
        assert result.exit_code == 1
        assert "no milestone" in result.output
        mock_update.assert_not_called()

    def test_unknown_repo_errors(self, config_file: Path) -> None:
        result = CliRunner().invoke(
            main,
            ["milestone", "write-order", "nope", "100", "--config", str(config_file)],
            input="- [ ] #1\n",
        )
        assert result.exit_code == 2
        assert "unknown repo" in result.output

    def test_refuses_shared_group_under_oracle_loop(
        self, config_file_with_gate_a: Path
    ) -> None:
        """#2542: this MILESTONE is under oracle-loop control — the repo
        has an acceptance driver configured AND this milestone's Gate-A
        contract already exists (#2785: both halves matter, not just the
        repo-level driver config) — a proposed work order putting
        #762/#763 in the same `{group: A}` cohort must be refused before
        it's ever written, since both would author their JIT acceptance
        slice into the SAME tests/acceptance/ms-N/manifest.yml concurrently
        (coord-portal#122)."""
        def get_issue(repo, number):
            return {
                "number": 100, "title": "tracking", "body": "",
                "state": "OPEN", "milestone": {"number": 9, "title": "M"},
            }

        open_issues = [
            {"number": 762, "milestone": {"number": 9}},
            {"number": 763, "milestone": {"number": 9}},
        ]
        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.github_ops.get_repo_file", return_value="# Contract\n"), \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "write-order", "api", "100", "--config",
                 str(config_file_with_gate_a)],
                input="- [ ] #762  {group: A}\n- [ ] #763  {group: A}\n",
            )
        assert result.exit_code == 1
        assert "#762, #763 share group 'A'" in result.output
        assert "oracle-loop" in result.output
        mock_update.assert_not_called()

    def test_shared_group_without_acceptance_driver_still_writes(
        self, config_file: Path
    ) -> None:
        """The same shared-group block is a no-op check for a repo NOT
        under oracle-loop control — no `acceptance.drivers` entry -> writes
        exactly as before #2542."""
        def get_issue(repo, number):
            return {
                "number": 100, "title": "tracking", "body": "",
                "state": "OPEN", "milestone": {"number": 9, "title": "M"},
                "labels": [{"name": "epic"}],
            }

        open_issues = [
            {"number": 762, "milestone": {"number": 9}},
            {"number": 763, "milestone": {"number": 9}},
        ]
        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.github_ops.add_issue_labels") as mock_add_labels, \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "write-order", "api", "100", "--config", str(config_file)],
                input="- [ ] #762  {group: A}\n- [ ] #763  {group: A}\n",
            )
        assert result.exit_code == 0, result.output
        mock_update.assert_called_once()

    def test_shared_group_with_driver_but_no_contract_still_writes(
        self, config_file_with_gate_a: Path
    ) -> None:
        """#2785: the repo has an acceptance driver configured (so
        `has_driver(repo)` is True), but THIS milestone has no Gate-A
        contract at all — `tests/acceptance/ms-9/contract.md` 404s, the
        same signal `gate_a_status` checks. That means there is no
        `tests/acceptance/ms-9/manifest.yml` for two group members' JIT
        slices to race on either, so #2542's shared-group refusal must NOT
        fire — mirrors #1949 (ms-60, Store Service): a milestone with
        ordinary parallel work sharing a repo with real oracle-loop
        milestones must keep writing exactly as it could before #2542."""
        def get_issue(repo, number):
            return {
                "number": 100, "title": "tracking", "body": "",
                "state": "OPEN", "milestone": {"number": 9, "title": "M"},
                "labels": [{"name": "epic"}],
            }

        open_issues = [
            {"number": 762, "milestone": {"number": 9}},
            {"number": 763, "milestone": {"number": 9}},
        ]
        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch(
                 "coord.github_ops.get_repo_file",
                 side_effect=RuntimeError("404"),
             ), \
             patch("coord.github_ops.add_issue_labels") as mock_add_labels, \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "write-order", "api", "100", "--config",
                 str(config_file_with_gate_a)],
                input="- [ ] #762  {group: A}\n- [ ] #763  {group: A}\n",
            )
        assert result.exit_code == 0, result.output
        mock_update.assert_called_once()


# ── `coord milestone chat` (#770 Phase 2 dispatch) ──────────────────────────


class TestMilestoneChatCmd:
    def test_dispatches_and_prints_assignment_id(self, config_file: Path) -> None:
        with patch(
            "coord.milestone_chat.dispatch_milestone_chat",
            return_value=("asg123", "laptop"),
        ) as mock_dispatch:
            result = CliRunner().invoke(
                main,
                ["milestone", "chat", "api", "100", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        assert result.output.strip().splitlines()[-1] == "asg123"
        mock_dispatch.assert_called_once_with(
            "api", 100, mock_dispatch.call_args[0][2],
            machine_override=None, add_child_issue=None,
        )

    def test_dispatch_failure_reports_error(self, config_file: Path) -> None:
        with patch(
            "coord.milestone_chat.dispatch_milestone_chat",
            side_effect=RuntimeError("no machine claims repo 'api'"),
        ):
            result = CliRunner().invoke(
                main,
                ["milestone", "chat", "api", "100", "--config", str(config_file)],
            )
        assert result.exit_code == 1
        assert "no machine claims repo" in result.output

    def test_unknown_repo_errors(self, config_file: Path) -> None:
        result = CliRunner().invoke(
            main,
            ["milestone", "chat", "nope", "100", "--config", str(config_file)],
        )
        assert result.exit_code == 2
        assert "unknown repo" in result.output

    def test_missing_tracking_issue_without_new_errors(self, config_file: Path) -> None:
        result = CliRunner().invoke(
            main,
            ["milestone", "chat", "api", "--config", str(config_file)],
        )
        assert result.exit_code == 2
        assert "TRACKING_ISSUE is required" in result.output

    def test_new_and_tracking_issue_together_errors(self, config_file: Path) -> None:
        result = CliRunner().invoke(
            main,
            ["milestone", "chat", "api", "100", "--new", "--config", str(config_file)],
        )
        assert result.exit_code == 2
        assert "not both" in result.output

    def test_seed_options_without_new_error(self, config_file: Path) -> None:
        result = CliRunner().invoke(
            main,
            ["milestone", "chat", "api", "100", "--title", "Foo", "--config", str(config_file)],
        )
        assert result.exit_code == 2
        assert "only apply with --new" in result.output


# ── `coord milestone add-child` (#1008: epic-child splice helper) ──────────


class TestMilestoneAddChildCmd:
    def test_adds_first_child_creating_section(self, config_file: Path) -> None:
        def get_issue(repo, number):
            if number == 100:
                return {
                    "number": 100, "title": "epic",
                    "body": "Epic intro.\n", "state": "OPEN",
                }
            return {"number": number, "title": f"issue {number}", "body": "", "state": "OPEN"}

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "add-child", "api", "100", "1050", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        assert "#1050 added to #100's" in result.output
        mock_update.assert_called_once()
        call_repo, call_issue, call_body = mock_update.call_args[0]
        assert call_repo == "acme/api"
        assert call_issue == 100
        assert "Epic intro." in call_body
        assert "## Sub-issues" in call_body
        assert "#1050" in call_body

    def test_adds_with_group_and_after_annotations(self, config_file: Path) -> None:
        tracking_body = "## Sub-issues\n- [ ] #1050\n"

        def get_issue(repo, number):
            if number == 100:
                return {"number": 100, "title": "epic", "body": tracking_body, "state": "OPEN"}
            return {"number": number, "title": f"issue {number}", "body": "", "state": "OPEN"}

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                [
                    "milestone", "add-child", "api", "100", "1051",
                    "--group", "B", "--after", "1050",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        mock_update.assert_called_once()
        _repo, _issue, call_body = mock_update.call_args[0]
        assert "#1050" in call_body and "#1051" in call_body
        from coord.milestone_order import parse_sub_issues
        wo = parse_sub_issues(call_body)
        assert wo.node(1051).group == "B"
        assert wo.node(1051).after == (1050,)

    def test_idempotent_readd_is_a_noop(self, config_file: Path) -> None:
        tracking_body = "## Sub-issues\n- [ ] #1050  {group: A}\n"

        def get_issue(repo, number):
            if number == 100:
                return {"number": 100, "title": "epic", "body": tracking_body, "state": "OPEN"}
            return {"number": number, "title": f"issue {number}", "body": "", "state": "OPEN"}

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                [
                    "milestone", "add-child", "api", "100", "1050",
                    "--group", "A", "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        assert "unchanged (idempotent no-op)" in result.output or "already in" in result.output
        mock_update.assert_not_called()

    def test_readd_with_different_annotations_updates_in_place(self, config_file: Path) -> None:
        tracking_body = "## Sub-issues\n- [x] #1050  {group: A}\n"

        def get_issue(repo, number):
            if number == 100:
                return {"number": 100, "title": "epic", "body": tracking_body, "state": "OPEN"}
            return {"number": number, "title": f"issue {number}", "body": "", "state": "OPEN"}

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                [
                    "milestone", "add-child", "api", "100", "1050",
                    "--group", "B", "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        mock_update.assert_called_once()
        _repo, _issue, call_body = mock_update.call_args[0]
        from coord.milestone_order import parse_sub_issues
        wo = parse_sub_issues(call_body)
        # Annotation updated, but the existing checked state is preserved.
        assert wo.node(1050).group == "B"
        assert wo.node(1050).checked is True

    def test_remove_drops_existing_child(self, config_file: Path) -> None:
        tracking_body = "## Sub-issues\n- [ ] #1050\n- [ ] #1051\n"

        def get_issue(repo, number):
            return {"number": 100, "title": "epic", "body": tracking_body, "state": "OPEN"}

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "add-child", "api", "100", "1050", "--remove", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        assert "removed from" in result.output
        mock_update.assert_called_once()
        _repo, _issue, call_body = mock_update.call_args[0]
        from coord.milestone_order import parse_sub_issues
        assert parse_sub_issues(call_body).issue_numbers == (1051,)

    def test_remove_absent_child_is_a_noop(self, config_file: Path) -> None:
        tracking_body = "## Sub-issues\n- [ ] #1050\n"

        def get_issue(repo, number):
            return {"number": 100, "title": "epic", "body": tracking_body, "state": "OPEN"}

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "add-child", "api", "100", "9999", "--remove", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        assert "no-op" in result.output
        mock_update.assert_not_called()

    def test_remove_rejects_group_and_after(self, config_file: Path) -> None:
        result = CliRunner().invoke(
            main,
            [
                "milestone", "add-child", "api", "100", "1050",
                "--remove", "--group", "A", "--config", str(config_file),
            ],
        )
        assert result.exit_code == 2
        assert "cannot be combined" in result.output

    def test_leaves_existing_work_order_section_untouched(self, config_file: Path) -> None:
        tracking_body = (
            "## Work order\n- [ ] #762  {group: A}\n\n"
            "## Sub-issues\n- [ ] #1050\n"
        )

        def get_issue(repo, number):
            if number == 100:
                return {"number": 100, "title": "epic", "body": tracking_body, "state": "OPEN"}
            return {"number": number, "title": f"issue {number}", "body": "", "state": "OPEN"}

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "add-child", "api", "100", "1051", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        _repo, _issue, call_body = mock_update.call_args[0]
        from coord.milestone_order import parse_sub_issues, parse_work_order
        assert parse_work_order(call_body).issue_numbers == (762,)
        assert parse_sub_issues(call_body).issue_numbers == (1050, 1051)

    def test_invalid_after_value_errors(self, config_file: Path) -> None:
        result = CliRunner().invoke(
            main,
            [
                "milestone", "add-child", "api", "100", "1050",
                "--after", "not-a-number", "--config", str(config_file),
            ],
        )
        assert result.exit_code == 2
        assert "--after must be a comma-separated list" in result.output

    def test_refuses_to_write_a_cycle(self, config_file: Path) -> None:
        tracking_body = "## Sub-issues\n- [ ] #1050  {after: #1051}\n- [ ] #1051\n"

        def get_issue(repo, number):
            if number == 100:
                return {"number": 100, "title": "epic", "body": tracking_body, "state": "OPEN"}
            return {"number": number, "title": f"issue {number}", "body": "", "state": "OPEN"}

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                [
                    "milestone", "add-child", "api", "100", "1051",
                    "--after", "1050", "--config", str(config_file),
                ],
            )
        assert result.exit_code == 1
        assert "cycle" in result.output
        mock_update.assert_not_called()

    def test_unknown_child_issue_errors(self, config_file: Path) -> None:
        def get_issue(repo, number):
            if number == 100:
                return {"number": 100, "title": "epic", "body": "", "state": "OPEN"}
            raise RuntimeError("gh issue view 9999 failed: no such issue")

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "add-child", "api", "100", "9999", "--config", str(config_file)],
            )
        assert result.exit_code == 1
        assert "could not fetch issue #9999" in result.output
        mock_update.assert_not_called()

    def test_unknown_epic_errors(self, config_file: Path) -> None:
        with patch(
            "coord.github_ops.get_issue",
            side_effect=RuntimeError("gh issue view 100 failed: no such issue"),
        ), patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "add-child", "api", "100", "1050", "--config", str(config_file)],
            )
        assert result.exit_code == 1
        assert "could not fetch epic #100" in result.output
        mock_update.assert_not_called()

    def test_unknown_repo_errors(self, config_file: Path) -> None:
        result = CliRunner().invoke(
            main,
            ["milestone", "add-child", "nope", "100", "1050", "--config", str(config_file)],
        )
        assert result.exit_code == 2
        assert "unknown repo" in result.output


# ── `coord milestone sync` (#1061 EP-2: backfill the sub-issues API) ───────


class TestMilestoneSyncCmd:
    def test_backfills_children_and_rewrites_body(self, config_file: Path) -> None:
        epic_body = (
            "Epic intro.\n\n"
            "## Work order\n"
            "- [ ] #1050  {group: A}\n"
            "- [ ] #1051  {after: #1050}\n"
        )

        def get_issue(repo, number):
            assert number == 100
            return {"number": 100, "title": "epic", "body": epic_body, "state": "OPEN"}

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.get_sub_issues", return_value=[]) as mock_get_subs, \
             patch("coord.github_ops.add_sub_issue") as mock_add, \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "sync", "api", "100", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        mock_get_subs.assert_called_once_with("acme/api", 100)
        assert mock_add.call_args_list == [
            (("acme/api", 100, 1050), {}),
            (("acme/api", 100, 1051), {}),
        ]
        mock_update.assert_called_once()
        call_repo, call_issue, call_body = mock_update.call_args[0]
        assert call_repo == "acme/api"
        assert call_issue == 100
        from coord.milestone_order import parse_work_order

        wo = parse_work_order(call_body)
        assert wo.issue_numbers == (1050, 1051)
        assert wo.node(1050).group == "A"
        assert wo.node(1051).after == (1050,)
        assert "[ ]" not in call_body and "[x]" not in call_body
        assert "- #1050" in call_body

    def test_strips_a_coexisting_sub_issues_section(self, config_file: Path) -> None:
        epic_body = (
            "## Work order\n"
            "- [ ] #1050  {group: A}\n\n"
            "## Sub-issues\n"
            "- [ ] #1050\n"
        )

        def get_issue(repo, number):
            return {"number": 100, "title": "epic", "body": epic_body, "state": "OPEN"}

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.get_sub_issues", return_value=[{"number": 1050, "state": "open"}]), \
             patch("coord.github_ops.add_sub_issue") as mock_add, \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "sync", "api", "100", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        mock_add.assert_not_called()  # already linked live — no-op
        mock_update.assert_called_once()
        _repo, _issue, call_body = mock_update.call_args[0]
        assert "## Sub-issues" not in call_body
        assert "[ ]" not in call_body

    def test_already_synced_epic_is_a_full_noop(self, config_file: Path) -> None:
        epic_body = "Epic intro.\n\n## Work order\n- #1050  {group: A}\n"

        def get_issue(repo, number):
            return {"number": 100, "title": "epic", "body": epic_body, "state": "OPEN"}

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.get_sub_issues", return_value=[{"number": 1050, "state": "open"}]), \
             patch("coord.github_ops.add_sub_issue") as mock_add, \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "sync", "api", "100", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        assert "no-op" in result.output
        mock_add.assert_not_called()
        mock_update.assert_not_called()

    def test_dry_run_makes_no_live_calls(self, config_file: Path) -> None:
        epic_body = "## Work order\n- [ ] #1050  {group: A}\n"

        def get_issue(repo, number):
            return {"number": 100, "title": "epic", "body": epic_body, "state": "OPEN"}

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.get_sub_issues", return_value=[]), \
             patch("coord.github_ops.add_sub_issue") as mock_add, \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "sync", "api", "100", "--dry-run", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        assert "would link" in result.output
        assert "would rewrite" in result.output
        mock_add.assert_not_called()
        mock_update.assert_not_called()

    def test_partial_link_failure_reports_and_exits_nonzero_but_still_rewrites(
        self, config_file: Path
    ) -> None:
        epic_body = "## Work order\n- [ ] #1050\n- [ ] #1051\n"

        def get_issue(repo, number):
            return {"number": 100, "title": "epic", "body": epic_body, "state": "OPEN"}

        def add_sub_issue(repo, parent, child):
            if child == 1051:
                raise RuntimeError("gh api ... failed: 422")

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.get_sub_issues", return_value=[]), \
             patch("coord.github_ops.add_sub_issue", side_effect=add_sub_issue), \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "sync", "api", "100", "--config", str(config_file)],
            )
        assert result.exit_code == 1
        assert "could not link #1051" in result.output
        # The body rewrite (grammar migration) still happens even though one
        # child failed to link — the two concerns are independent.
        mock_update.assert_called_once()

    def test_sub_issues_only_epic_is_still_linked_and_folded_in(
        self, config_file: Path
    ) -> None:
        # #1061 fix-iteration 1: an epic created via standalone `add-child`
        # can have a `## Sub-issues` checklist and *no* `## Work order`
        # block at all — sync must not bail out as "nothing to sync" in
        # that case (the original blocking review finding).
        epic_body = "Epic intro.\n\n## Sub-issues\n- [ ] #1050  {group: A}\n- [ ] #1051\n"

        def get_issue(repo, number):
            return {"number": 100, "title": "epic", "body": epic_body, "state": "OPEN"}

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.get_sub_issues", return_value=[]) as mock_get_subs, \
             patch("coord.github_ops.add_sub_issue") as mock_add, \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "sync", "api", "100", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        mock_get_subs.assert_called_once_with("acme/api", 100)
        assert mock_add.call_args_list == [
            (("acme/api", 100, 1050), {}),
            (("acme/api", 100, 1051), {}),
        ]
        mock_update.assert_called_once()
        call_repo, call_issue, call_body = mock_update.call_args[0]
        assert call_repo == "acme/api"
        assert call_issue == 100
        from coord.milestone_order import parse_work_order

        wo = parse_work_order(call_body)
        assert wo.issue_numbers == (1050, 1051)
        assert wo.node(1050).group == "A"
        assert "## Sub-issues" not in call_body

    def test_divergent_work_order_and_sub_issues_are_unioned_not_dropped(
        self, config_file: Path
    ) -> None:
        # #1061 fix-iteration 1: `## Sub-issues` lists a child (#1052) not
        # present in `## Work order` — it must still be linked live and
        # survive into the rewritten `## Work order`, not be silently
        # discarded when `## Sub-issues` is removed.
        epic_body = (
            "## Work order\n"
            "- [ ] #1050  {group: A}\n\n"
            "## Sub-issues\n"
            "- [ ] #1050\n"
            "- [ ] #1052\n"
        )

        def get_issue(repo, number):
            return {"number": 100, "title": "epic", "body": epic_body, "state": "OPEN"}

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.get_sub_issues", return_value=[]) as mock_get_subs, \
             patch("coord.github_ops.add_sub_issue") as mock_add, \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "sync", "api", "100", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        mock_get_subs.assert_called_once_with("acme/api", 100)
        assert mock_add.call_args_list == [
            (("acme/api", 100, 1050), {}),
            (("acme/api", 100, 1052), {}),
        ]
        mock_update.assert_called_once()
        _repo, _issue, call_body = mock_update.call_args[0]
        from coord.milestone_order import parse_work_order

        wo = parse_work_order(call_body)
        assert wo.issue_numbers == (1050, 1052)
        assert wo.node(1050).group == "A"
        assert "## Sub-issues" not in call_body

    def test_no_work_order_block_reports_and_exits_zero(self, config_file: Path) -> None:
        def get_issue(repo, number):
            return {"number": 100, "title": "epic", "body": "no work order here", "state": "OPEN"}

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "sync", "api", "100", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        assert "nothing to sync" in result.output
        mock_update.assert_not_called()

    def test_invalid_work_order_errors(self, config_file: Path) -> None:
        def get_issue(repo, number):
            return {
                "number": 100, "title": "epic",
                "body": "## Work order\n- [ ] #1 {after: #1}\n",
                "state": "OPEN",
            }

        with patch("coord.github_ops.get_issue", side_effect=get_issue):
            result = CliRunner().invoke(
                main,
                ["milestone", "sync", "api", "100", "--config", str(config_file)],
            )
        assert result.exit_code == 1
        assert "cycle" in result.output

    def test_unknown_epic_errors(self, config_file: Path) -> None:
        with patch(
            "coord.github_ops.get_issue",
            side_effect=RuntimeError("gh issue view 100 failed: no such issue"),
        ):
            result = CliRunner().invoke(
                main,
                ["milestone", "sync", "api", "100", "--config", str(config_file)],
            )
        assert result.exit_code == 1
        assert "could not fetch epic #100" in result.output

    def test_unknown_repo_errors(self, config_file: Path) -> None:
        result = CliRunner().invoke(
            main,
            ["milestone", "sync", "nope", "100", "--config", str(config_file)],
        )
        assert result.exit_code == 2
        assert "unknown repo" in result.output


# ── `coord milestone sync-progress` (#1412 deliverable 2) ──────────────────


class TestMilestoneSyncProgressCmd:
    def test_writes_progress_section_from_live_state(self, config_file: Path) -> None:
        tracking_body = (
            "Milestone plan.\n\n"
            "## Work order\n"
            "- #762  {group: A}\n"
            "- #765  {after: #762}\n"
        )

        def get_issue(repo, number):
            return _get_issue(repo, number, bodies={100: tracking_body}, states={762: "CLOSED"})

        open_issues = [{"number": 765, "milestone": {"number": 9}}]
        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.board_service.read_board", return_value=Board()), \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "sync-progress", "api", "100", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        assert "[done] #762" in result.output
        assert "[ready] #765" in result.output
        mock_update.assert_called_once()
        call_repo, call_issue, call_body = mock_update.call_args[0]
        assert call_repo == "acme/api"
        assert call_issue == 100

        from coord.milestone_order import ProgressStatus, parse_progress, parse_work_order

        statuses = parse_progress(call_body)
        assert statuses == (
            ProgressStatus(762, "done", "A"),
            ProgressStatus(765, "ready"),
        )
        # `## Work order` is byte-identical — the write path never touches it.
        assert "## Work order\n- #762  {group: A}\n- #765  {after: #762}\n" in call_body
        assert parse_work_order(call_body) == parse_work_order(tracking_body)

    def test_unchanged_state_is_a_full_noop(self, config_file: Path) -> None:
        from coord.milestone_order import (
            compute_progress,
            parse_work_order,
            ready_frontier,
            render_progress,
            replace_progress_section,
        )

        wo = parse_work_order(TRACKING_BODY)
        frontier = ready_frontier(
            wo, Board(), repo_name="api", repo_github="acme/api", terminal_issues=set(),
        )
        statuses = compute_progress(wo, frontier, set())
        rendered = render_progress(statuses, generated_at="2020-01-01T00:00:00Z")
        tracking_body = replace_progress_section(TRACKING_BODY, rendered)

        def get_issue(repo, number):
            return _get_issue(repo, number, bodies={100: tracking_body})

        open_issues = [
            {"number": 762, "milestone": {"number": 9}},
            {"number": 763, "milestone": {"number": 9}},
            {"number": 765, "milestone": {"number": 9}},
        ]
        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.board_service.read_board", return_value=Board()), \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "sync-progress", "api", "100", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        assert "no-op" in result.output
        mock_update.assert_not_called()

    def test_dry_run_does_not_write(self, config_file: Path) -> None:
        def get_issue(repo, number):
            return _get_issue(repo, number)

        open_issues = [
            {"number": 762, "milestone": {"number": 9}},
            {"number": 763, "milestone": {"number": 9}},
            {"number": 765, "milestone": {"number": 9}},
        ]
        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.board_service.read_board", return_value=Board()), \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                [
                    "milestone", "sync-progress", "api", "100",
                    "--dry-run", "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        assert "would update" in result.output
        mock_update.assert_not_called()

    def test_composes_with_a_coexisting_sub_issues_section(self, config_file: Path) -> None:
        """#1412 acceptance: a test covers the sync against a body carrying
        both `## Work order` and a legacy `## Sub-issues` section — neither
        is disturbed by the progress splice."""
        tracking_body = (
            "## Work order\n"
            "- #762  {group: A}\n\n"
            "## Sub-issues\n"
            "- [ ] #762\n"
        )

        def get_issue(repo, number):
            return _get_issue(repo, number, bodies={100: tracking_body}, states={762: "CLOSED"})

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=[]), \
             patch("coord.board_service.read_board", return_value=Board()), \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "sync-progress", "api", "100", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        mock_update.assert_called_once()
        _repo, _issue, call_body = mock_update.call_args[0]
        assert "## Sub-issues\n- [ ] #762\n" in call_body
        assert "## Work order\n- #762  {group: A}\n" in call_body
        assert "## Progress" in call_body

    def test_no_work_order_block_reports_and_exits_zero(self, config_file: Path) -> None:
        def get_issue(repo, number):
            return {
                "number": number, "title": "t", "body": "just prose, no work order",
                "state": "OPEN", "milestone": {"number": 9, "title": "M"},
            }

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "sync-progress", "api", "100", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        assert "nothing to report progress on" in result.output
        mock_update.assert_not_called()

    def test_unknown_repo_errors(self, config_file: Path) -> None:
        result = CliRunner().invoke(
            main,
            ["milestone", "sync-progress", "nope", "100", "--config", str(config_file)],
        )
        assert result.exit_code == 2
        assert "unknown repo" in result.output


# ── `coord milestone chat --new` (#1009 brand-new-milestone dispatch) ───────


class TestMilestoneChatNewCmd:
    def test_dispatches_and_prints_assignment_id(self, config_file: Path) -> None:
        with patch(
            "coord.milestone_chat.dispatch_new_milestone_chat",
            return_value=("asg456", "laptop"),
        ) as mock_dispatch:
            result = CliRunner().invoke(
                main,
                [
                    "milestone", "chat", "api", "--new",
                    "--title", "Q4 push", "--seed", "ship the widget",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        assert result.output.strip().splitlines()[-1] == "asg456"
        mock_dispatch.assert_called_once_with(
            "api",
            mock_dispatch.call_args[0][1],
            seed_title="Q4 push",
            seed_prompt="ship the widget",
            machine_override=None,
        )

    def test_dispatch_failure_reports_error(self, config_file: Path) -> None:
        with patch(
            "coord.milestone_chat.dispatch_new_milestone_chat",
            side_effect=RuntimeError("no machine claims repo 'api'"),
        ):
            result = CliRunner().invoke(
                main,
                ["milestone", "chat", "api", "--new", "--config", str(config_file)],
            )
        assert result.exit_code == 1
        assert "no machine claims repo" in result.output

    def test_unknown_repo_errors(self, config_file: Path) -> None:
        result = CliRunner().invoke(
            main,
            ["milestone", "chat", "nope", "--new", "--config", str(config_file)],
        )
        assert result.exit_code == 2
        assert "unknown repo" in result.output
