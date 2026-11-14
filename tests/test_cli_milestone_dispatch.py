"""Black-box tests for `coord milestone dispatch` (#769 Phase 1).

Mocks `coord.github_ops` (no live `gh` calls) and `coord.dispatch.dispatch`
(no live HTTP POST to an agent) so the test drives the real Click command
end to end: fetch tracking issue -> parse work order -> resolve membership/
terminal state -> compute ready frontier -> pick machines -> (maybe)
dispatch. Mirrors tests/test_cli_milestone_order.py's fixture/mock style.

Includes the #769 acceptance-criteria scenario verbatim: a `group: A`
cohort + an `after`-gated node -> dry-run shows the cohort dispatching now
and the gated node waiting; simulating the cohort merging -> the gated node
enters the frontier.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from coord import milestone_gate as mg
from coord import state as state_mod
from tests import backends
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
  - name: server
    host: server.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
"""


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
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML_WITH_ACCEPTANCE_DRIVER)
    return p


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


TRACKING_BODY = """\
Milestone plan.

## Work order
- [ ] #762  {group: A}
- [ ] #763  {group: A}
- [ ] #765  {after: #762,#763}
"""


def _get_issue(repo, number, *, milestone_number=9, closed=frozenset(), bodies=None):
    bodies = bodies or {}
    if number == 100:
        return {
            "number": 100, "title": "tracking", "body": bodies.get(100, TRACKING_BODY),
            "state": "OPEN", "milestone": {"number": milestone_number, "title": "M"},
        }
    return {
        "number": number, "title": f"issue {number}", "body": bodies.get(number, ""),
        "state": "CLOSED" if number in closed else "OPEN",
        "milestone": {"number": milestone_number, "title": "M"},
        "labels": [],
    }


class TestMilestoneDispatchValidation:
    def test_unknown_repo_errors(self, config_file: Path) -> None:
        result = CliRunner().invoke(
            main, ["milestone", "dispatch", "nope", "100", "--config", str(config_file)]
        )
        assert result.exit_code == 2
        assert "unknown repo" in result.output


class TestMilestoneDispatchDryRun:
    def test_dry_run_shows_queue_plan_with_after_edges(self, config_file: Path) -> None:
        """#2335: bulk dry-run previews the drive-queue enqueue plan — the
        whole DAG, with the gated node's `after` edges spelled out — and
        writes/dispatches nothing."""
        open_issues = [
            {"number": 762, "milestone": {"number": 9}},
            {"number": 763, "milestone": {"number": 9}},
            {"number": 765, "milestone": {"number": 9}},
        ]
        with patch("coord.github_ops.get_issue", side_effect=_get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.board_service.read_board", return_value=Board()), \
             patch("coord.dispatch.dispatch") as disp:
            result = CliRunner().invoke(
                main,
                ["milestone", "dispatch", "api", "100", "--config", str(config_file), "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        disp.assert_not_called()

        plan_section = result.output.split("Will queue (drive-queue), in dependency order:")[1]
        assert "#762" in plan_section
        assert "#763" in plan_section
        assert "#765  (after api#762, api#763)" in plan_section
        assert "dry run" in result.output
        # Nothing written — the dry run leaves the queue untouched.
        assert state_mod.list_drive_queue() == []

    def test_terminal_prereqs_are_dropped_from_after_edges(self, config_file: Path) -> None:
        """#2335: #762/#763 already closed -> only #765 queues, with no
        `after` edges (a closed issue never enters the queue, so an edge at
        it would read as unsatisfiable to the tick)."""
        open_issues = [{"number": 765, "milestone": {"number": 9}}]

        def get_issue(repo, number):
            return _get_issue(repo, number, closed=frozenset({762, 763}))

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.board_service.read_board", return_value=Board()), \
             patch("coord.dispatch.dispatch") as disp:
            result = CliRunner().invoke(
                main,
                ["milestone", "dispatch", "api", "100", "--config", str(config_file), "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        disp.assert_not_called()

        plan_section = result.output.split("Will queue (drive-queue), in dependency order:")[1]
        assert "#765" in plan_section
        assert "after" not in plan_section
        assert "#762" not in plan_section

    def test_no_work_order_block_reports_and_exits_zero(self, config_file: Path) -> None:
        def get_issue_no_block(repo, number):
            return {
                "number": number, "title": "t", "body": "just prose",
                "state": "OPEN", "milestone": {"number": 9, "title": "M"},
            }

        with patch("coord.github_ops.get_issue", side_effect=get_issue_no_block):
            result = CliRunner().invoke(
                main,
                ["milestone", "dispatch", "api", "100", "--config", str(config_file), "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        assert "no `## Work order` block found" in result.output


class TestMilestoneDispatchBulk:
    def test_enqueues_whole_dag_into_drive_queue(self, config_file: Path) -> None:
        """#2335: bulk mode queues every open work-order node into the
        drive-queue with `after=` edges carrying the DAG — it never calls
        `coord.dispatch.dispatch` directly, and it no longer registers the
        milestone for the daemon's direct-dispatch auto-drain (the queue IS
        the durable drain)."""
        open_issues = [
            {"number": 762, "milestone": {"number": 9}},
            {"number": 763, "milestone": {"number": 9}},
            {"number": 765, "milestone": {"number": 9}},
        ]
        with patch("coord.github_ops.get_issue", side_effect=_get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.board_service.read_board", return_value=Board()), \
             patch("coord.dispatch.dispatch") as disp:
            result = CliRunner().invoke(
                main,
                ["milestone", "dispatch", "api", "100", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        disp.assert_not_called()

        rows = state_mod.list_drive_queue()
        assert [(r["repo_name"], r["issue_number"]) for r in rows] == [
            ("api", 762), ("api", 763), ("api", 765),
        ]
        by_issue = {r["issue_number"]: r for r in rows}
        assert by_issue[762]["after_json"] == []
        assert by_issue[763]["after_json"] == []
        assert by_issue[765]["after_json"] == ["api#762", "api#763"]

        assert "queued api#762" in result.output
        assert "queued api#765 after api#762, api#763" in result.output
        assert "3 issue(s) queued" in result.output
        assert "coord drive-queue tick" in result.output

        # No direct dispatch happened, so nothing was recorded as dispatched
        # and no daemon auto-drain registration is needed or wanted.
        assert state_mod.load_dispatched() == []
        assert state_mod.list_milestone_drains() == []
        assert "registered for daemon auto-drain" not in result.output

    def test_dependent_declared_first_still_queues_after_its_prereq(
        self, config_file: Path
    ) -> None:
        """#2335: declared order is not necessarily topological — a node
        declared before its own pre-req still queues after it, so queue
        positions read in dependency order."""
        body = "## Work order\n- [ ] #765  {after: #762}\n- [ ] #762\n"
        open_issues = [
            {"number": 762, "milestone": {"number": 9}},
            {"number": 765, "milestone": {"number": 9}},
        ]

        def get_issue(repo, number):
            return _get_issue(repo, number, bodies={100: body})

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.board_service.read_board", return_value=Board()), \
             patch("coord.dispatch.dispatch") as disp:
            result = CliRunner().invoke(
                main,
                ["milestone", "dispatch", "api", "100", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        disp.assert_not_called()

        rows = state_mod.list_drive_queue()
        assert [r["issue_number"] for r in rows] == [762, 765]
        assert rows[1]["after_json"] == ["api#762"]

    def test_node_declaring_a_coordinator_owned_doc_warns_but_still_queues(
        self, config_file: Path, coord_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#4008: a bulk enqueue surfaces the same coordinator-owned-doc
        warning a manual `coord drive-queue add` does — the node whose own
        `## Files` names `CLAUDE.md` is flagged, the one that doesn't is
        not, and both still queue (order/advise, never refuse)."""
        body = "## Work order\n- [ ] #762\n- [ ] #763\n"
        open_issues = [
            {"number": 762, "milestone": {"number": 9}},
            {"number": 763, "milestone": {"number": 9}},
        ]
        declarations = {
            762: "## Files\n- `coord/review.py`\n- `CLAUDE.md`\n",
            763: "## Files\n- `coord/dispatch.py`\n",
        }
        for number, decl in declarations.items():
            backends.upsert_issue(
                coord_db, repo_name="api", number=number,
                title=f"issue {number}", body=decl, state="open",
            )
        coord_db.commit()
        # The candidate's live re-read is the one process boundary the
        # predictor crosses for a declaring issue; answer it from the same
        # declarations instead of shelling out to `gh`.
        monkeypatch.setattr(
            "coord.commands.drive_queue._live_issue_body",
            lambda repo_github, number: declarations.get(number),
        )

        def get_issue(repo, number):
            return _get_issue(repo, number, bodies={100: body, **declarations})

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.board_service.read_board", return_value=Board()), \
             patch("coord.dispatch.dispatch") as disp:
            result = CliRunner().invoke(
                main,
                ["milestone", "dispatch", "api", "100", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        disp.assert_not_called()

        queued_762 = result.output.split("queued api#762")[1].split("queued api#763")[0]
        assert "coordinator-owned doc" in queued_762
        assert "`CLAUDE.md`" in queued_762
        queued_763 = result.output.split("queued api#763")[1]
        assert "coordinator-owned doc" not in queued_763

        # Advisory only — both nodes are in the queue.
        rows = state_mod.list_drive_queue()
        assert [r["issue_number"] for r in rows] == [762, 763]

    def test_already_complete_milestone_queues_nothing_and_does_not_register(
        self, config_file: Path
    ) -> None:
        """Every node already terminal at fetch time -> nothing to queue,
        nothing dispatched, no daemon auto-drain registration."""
        body = "## Work order\n- [ ] #762\n"
        open_issues: list[dict] = []

        def get_issue(repo, number):
            return _get_issue(repo, number, bodies={100: body}, closed=frozenset({762}))

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.board_service.read_board", return_value=Board()), \
             patch("coord.dispatch.dispatch") as disp:
            result = CliRunner().invoke(
                main,
                ["milestone", "dispatch", "api", "100", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        disp.assert_not_called()
        assert "Nothing to queue" in result.output
        assert state_mod.list_drive_queue() == []
        assert state_mod.list_milestone_drains() == []


class TestMilestoneDispatchNext:
    def test_next_lists_choices_and_dispatches_the_pick(self, config_file: Path) -> None:
        open_issues = [
            {"number": 762, "milestone": {"number": 9}},
            {"number": 763, "milestone": {"number": 9}},
            {"number": 765, "milestone": {"number": 9}},
        ]
        with patch("coord.github_ops.get_issue", side_effect=_get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.board_service.read_board", return_value=Board()), \
             patch("coord.dispatch.dispatch", return_value={"id": "picked-1"}) as disp, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False):
            result = CliRunner().invoke(
                main,
                ["milestone", "dispatch", "api", "100", "--config", str(config_file), "--next"],
                input="2\n",
            )
        assert result.exit_code == 0, result.output
        assert "pick one" in result.output.lower()
        disp.assert_called_once()
        assert "picked-1" in result.output

        # --next never registers for daemon auto-drain — the lighter,
        # fully-manual single-pick path.
        assert state_mod.list_milestone_drains() == []

    def test_next_dry_run_lists_without_dispatching(self, config_file: Path) -> None:
        open_issues = [
            {"number": 762, "milestone": {"number": 9}},
            {"number": 763, "milestone": {"number": 9}},
            {"number": 765, "milestone": {"number": 9}},
        ]
        with patch("coord.github_ops.get_issue", side_effect=_get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.board_service.read_board", return_value=Board()), \
             patch("coord.dispatch.dispatch") as disp:
            result = CliRunner().invoke(
                main,
                ["milestone", "dispatch", "api", "100", "--config", str(config_file),
                 "--next", "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        disp.assert_not_called()
        assert "#762" in result.output
        assert "#763" in result.output

    def test_pick_dispatches_without_prompt(self, config_file: Path) -> None:
        """#1003: --pick is the non-interactive companion to --next — the
        coord-tui "Dispatch next…" action's backend, which has no TTY to
        answer `click.prompt`."""
        open_issues = [
            {"number": 762, "milestone": {"number": 9}},
            {"number": 763, "milestone": {"number": 9}},
            {"number": 765, "milestone": {"number": 9}},
        ]
        with patch("coord.github_ops.get_issue", side_effect=_get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.board_service.read_board", return_value=Board()), \
             patch("coord.dispatch.dispatch", return_value={"id": "picked-1"}) as disp, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False):
            result = CliRunner().invoke(
                main,
                [
                    "milestone", "dispatch", "api", "100", "--config", str(config_file),
                    "--next", "--pick", "763",
                ],
                # No stdin — a prompt here would hang/abort the test.
                input="",
            )
        assert result.exit_code == 0, result.output
        disp.assert_called_once()
        assert "picked-1" in result.output
        assert state_mod.list_milestone_drains() == []

    def test_pick_dry_run_previews_without_dispatching(self, config_file: Path) -> None:
        open_issues = [
            {"number": 762, "milestone": {"number": 9}},
            {"number": 763, "milestone": {"number": 9}},
            {"number": 765, "milestone": {"number": 9}},
        ]
        with patch("coord.github_ops.get_issue", side_effect=_get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.board_service.read_board", return_value=Board()), \
             patch("coord.dispatch.dispatch") as disp:
            result = CliRunner().invoke(
                main,
                [
                    "milestone", "dispatch", "api", "100", "--config", str(config_file),
                    "--next", "--pick", "762", "--dry-run",
                ],
            )
        assert result.exit_code == 0, result.output
        disp.assert_not_called()
        assert "#762" in result.output
        assert "dry run" in result.output.lower()

    def test_pick_issue_not_in_ready_frontier_errors(self, config_file: Path) -> None:
        open_issues = [
            {"number": 762, "milestone": {"number": 9}},
            {"number": 763, "milestone": {"number": 9}},
            {"number": 765, "milestone": {"number": 9}},
        ]
        with patch("coord.github_ops.get_issue", side_effect=_get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.board_service.read_board", return_value=Board()), \
             patch("coord.dispatch.dispatch") as disp:
            result = CliRunner().invoke(
                main,
                [
                    "milestone", "dispatch", "api", "100", "--config", str(config_file),
                    # #765 is gated on #762/#763 — not ready yet.
                    "--next", "--pick", "765",
                ],
            )
        assert result.exit_code == 1
        assert "not in the ready-to-dispatch frontier" in result.output
        disp.assert_not_called()

    def test_pick_without_next_errors(self, config_file: Path) -> None:
        result = CliRunner().invoke(
            main,
            [
                "milestone", "dispatch", "api", "100", "--config", str(config_file),
                "--pick", "762",
            ],
        )
        assert result.exit_code == 2
        assert "--pick requires --next" in result.output


class TestMilestoneDispatchGateA:
    """#930 (docs/ORACLE_LOOP.md, Gate A) — the issue's specified black-box
    scenario: a milestone with no contract refuses issue dispatch; with a
    contract, allows it. Only applies to repos with an acceptance driver
    configured; ``config_file`` (no driver) is exercised everywhere above
    and is unaffected by this gate."""

    def test_no_contract_refuses_dispatch(self, config_file_with_gate_a: Path) -> None:
        open_issues = [
            {"number": 762, "milestone": {"number": 9}},
            {"number": 763, "milestone": {"number": 9}},
            {"number": 765, "milestone": {"number": 9}},
        ]
        with patch("coord.github_ops.get_issue", side_effect=_get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.github_ops.get_repo_file", side_effect=RuntimeError("404")), \
             patch("coord.board_service.read_board", return_value=Board()), \
             patch("coord.dispatch.dispatch") as disp:
            result = CliRunner().invoke(
                main,
                ["milestone", "dispatch", "api", "100", "--config",
                 str(config_file_with_gate_a)],
            )
        assert result.exit_code == 1, result.output
        assert "Gate A not satisfied" in result.output
        assert "tests/acceptance/ms-9/contract.md" in result.output
        assert "coord acceptance mock api" in result.output
        disp.assert_not_called()

    def test_no_contract_refuses_even_under_dry_run_and_next(
        self, config_file_with_gate_a: Path,
    ) -> None:
        open_issues = [
            {"number": 762, "milestone": {"number": 9}},
            {"number": 763, "milestone": {"number": 9}},
            {"number": 765, "milestone": {"number": 9}},
        ]
        with patch("coord.github_ops.get_issue", side_effect=_get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.github_ops.get_repo_file", side_effect=RuntimeError("404")), \
             patch("coord.dispatch.dispatch") as disp:
            result = CliRunner().invoke(
                main,
                ["milestone", "dispatch", "api", "100", "--config",
                 str(config_file_with_gate_a), "--dry-run", "--next"],
            )
        assert result.exit_code == 1, result.output
        assert "Gate A not satisfied" in result.output
        disp.assert_not_called()

    def test_contract_present_allows_dispatch(self, config_file_with_gate_a: Path) -> None:
        open_issues = [
            {"number": 762, "milestone": {"number": 9}},
            {"number": 763, "milestone": {"number": 9}},
            {"number": 765, "milestone": {"number": 9}},
        ]
        with patch("coord.github_ops.get_issue", side_effect=_get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.github_ops.get_repo_file", return_value="# Contract\n"), \
             patch("coord.board_service.read_board", return_value=Board()), \
             patch("coord.dispatch.dispatch") as disp:
            result = CliRunner().invoke(
                main,
                ["milestone", "dispatch", "api", "100", "--config",
                 str(config_file_with_gate_a), "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        assert "Gate A" not in result.output
        plan_section = result.output.split("Will queue (drive-queue), in dependency order:")[1]
        assert "#762" in plan_section
        assert "#763" in plan_section

    def test_repo_without_acceptance_driver_is_unaffected(self, config_file: Path) -> None:
        """No `acceptance.drivers` entry for this repo -> Gate A is a no-op,
        exactly as before #930."""
        with patch("coord.github_ops.get_issue", side_effect=_get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=[]), \
             patch("coord.github_ops.get_repo_file") as get_file:
            result = CliRunner().invoke(
                main,
                ["milestone", "dispatch", "api", "100", "--config", str(config_file),
                 "--dry-run"],
            )
        get_file.assert_not_called()
        assert "Gate A" not in result.output


class TestMilestoneDispatchOracleLoopSerializes:
    """#2542: under oracle-loop control (an acceptance driver configured —
    Gate A already satisfied by the `config_file_with_gate_a` fixture's
    mocked contract), bulk `coord milestone dispatch` must chain the WHOLE
    milestone into the drive-queue one entry at a time — the #130/#132
    coord-portal#122 collision this issue's correction describes had no
    shared `group` at all, so only the drive-queue translation itself (not
    the write-order validator) can close it."""

    def test_group_cohort_gets_a_serializing_after_edge(
        self, config_file_with_gate_a: Path,
    ) -> None:
        """#762/#763 share {group: A} — plain (non-oracle) dispatch queues
        them with no edge between them (see TestMilestoneDispatchBulk); under
        an acceptance driver, #763 additionally gets `after api#762`."""
        open_issues = [
            {"number": 762, "milestone": {"number": 9}},
            {"number": 763, "milestone": {"number": 9}},
            {"number": 765, "milestone": {"number": 9}},
        ]
        with patch("coord.github_ops.get_issue", side_effect=_get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.github_ops.get_repo_file", return_value="# Contract\n"), \
             patch("coord.board_service.read_board", return_value=Board()), \
             patch("coord.dispatch.dispatch") as disp:
            result = CliRunner().invoke(
                main,
                ["milestone", "dispatch", "api", "100", "--config",
                 str(config_file_with_gate_a)],
            )
        assert result.exit_code == 0, result.output
        disp.assert_not_called()

        rows = state_mod.list_drive_queue()
        by_issue = {r["issue_number"]: r for r in rows}
        assert by_issue[762]["after_json"] == []
        assert by_issue[763]["after_json"] == ["api#762"]
        # #765 was already declared `after: #762,#763` — unaffected by the
        # implicit chain (its immediate predecessor, #763, is already there).
        assert by_issue[765]["after_json"] == ["api#762", "api#763"]

    def test_ungrouped_independent_issues_still_get_chained(
        self, config_file_with_gate_a: Path,
    ) -> None:
        """The SECOND coord-portal#122 collision (#130 vs. #132): no shared
        group, no declared `after` between them at all — the drive-queue's
        per-repo concurrency cap alone would launch both. Confirms the fix
        isn't scoped to the explicit-group case."""
        body = "## Work order\n- [ ] #762\n- [ ] #763\n"
        open_issues = [
            {"number": 762, "milestone": {"number": 9}},
            {"number": 763, "milestone": {"number": 9}},
        ]

        def get_issue(repo, number):
            return _get_issue(repo, number, bodies={100: body})

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.github_ops.get_repo_file", return_value="# Contract\n"), \
             patch("coord.board_service.read_board", return_value=Board()), \
             patch("coord.dispatch.dispatch") as disp:
            result = CliRunner().invoke(
                main,
                ["milestone", "dispatch", "api", "100", "--config",
                 str(config_file_with_gate_a)],
            )
        assert result.exit_code == 0, result.output
        disp.assert_not_called()

        rows = state_mod.list_drive_queue()
        by_issue = {r["issue_number"]: r for r in rows}
        assert by_issue[762]["after_json"] == []
        assert by_issue[763]["after_json"] == ["api#762"]

    def test_repo_without_acceptance_driver_is_not_chained(
        self, config_file: Path,
    ) -> None:
        """No acceptance driver configured -> not under oracle-loop control
        -> the group cohort dispatches exactly as before #2542 (no implicit
        edge)."""
        open_issues = [
            {"number": 762, "milestone": {"number": 9}},
            {"number": 763, "milestone": {"number": 9}},
            {"number": 765, "milestone": {"number": 9}},
        ]
        with patch("coord.github_ops.get_issue", side_effect=_get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.board_service.read_board", return_value=Board()), \
             patch("coord.dispatch.dispatch") as disp:
            result = CliRunner().invoke(
                main,
                ["milestone", "dispatch", "api", "100", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        disp.assert_not_called()

        rows = state_mod.list_drive_queue()
        by_issue = {r["issue_number"]: r for r in rows}
        assert by_issue[762]["after_json"] == []
        assert by_issue[763]["after_json"] == []


class TestMilestoneDispatchGateControlled:
    """#1930 (epic #1440 S-2): a milestone under gate control (``coord
    milestone drive``) is drained exclusively by the daemon's gate tick.
    Manual ``coord milestone dispatch`` against that same milestone is the
    second-dispatch-path race #1440 exists to close structurally — it must
    refuse, not race the daemon's own `work`-state drain. This is the case
    the issue calls out as "the one an operator will actually hit"."""

    def test_bulk_dispatch_refused_against_gate_controlled_milestone(
        self, config_file: Path,
    ) -> None:
        state_mod.save_milestone_gate(
            mg.GateRecord(
                repo_name="api", tracking_issue=100, gate=mg.WORK,
            ).to_dict()
        )
        with patch("coord.github_ops.get_issue", side_effect=_get_issue) as get_issue, \
             patch("coord.github_ops.get_open_issues") as get_open, \
             patch("coord.dispatch.dispatch") as disp:
            result = CliRunner().invoke(
                main,
                ["milestone", "dispatch", "api", "100", "--config", str(config_file)],
            )
        assert result.exit_code == 1, result.output
        assert "under gate control" in result.output
        assert "work — drain the ready frontier" in result.output
        assert "coord milestone drive --dry-run" in result.output
        # Refused before the GitHub fetch even happens — cheap and
        # unconditional, mirroring Gate A's posture.
        get_issue.assert_not_called()
        get_open.assert_not_called()
        disp.assert_not_called()

    def test_dry_run_and_next_also_refused(self, config_file: Path) -> None:
        state_mod.save_milestone_gate(
            mg.GateRecord(
                repo_name="api", tracking_issue=100, gate=mg.GATE_B,
            ).to_dict()
        )
        with patch("coord.github_ops.get_issue", side_effect=_get_issue), \
             patch("coord.github_ops.get_open_issues"), \
             patch("coord.dispatch.dispatch") as disp:
            result = CliRunner().invoke(
                main,
                ["milestone", "dispatch", "api", "100", "--config", str(config_file),
                 "--dry-run", "--next"],
            )
        assert result.exit_code == 1, result.output
        assert "under gate control" in result.output
        assert "Gate B" in result.output
        disp.assert_not_called()

    def test_no_gate_record_dispatches_normally(self, config_file: Path) -> None:
        """Control: a milestone with no gate record at all is unaffected —
        the check must not false-positive on every dispatch."""
        open_issues = [
            {"number": 762, "milestone": {"number": 9}},
            {"number": 763, "milestone": {"number": 9}},
            {"number": 765, "milestone": {"number": 9}},
        ]
        with patch("coord.github_ops.get_issue", side_effect=_get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.board_service.read_board", return_value=Board()), \
             patch("coord.dispatch.dispatch") as disp:
            result = CliRunner().invoke(
                main,
                ["milestone", "dispatch", "api", "100", "--config", str(config_file),
                 "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        assert "under gate control" not in result.output
        disp.assert_not_called()  # dry-run never dispatches regardless


class TestMilestoneDispatchGateControlledThinClient:
    """#1930 fix-review: the guard above must also hold on a thin client
    (``board_service`` configured, per docs/ARCHITECTURE.md's "coord and
    coord-tui on any machine render and drive the same board as bearer-token
    thin clients"), where the local SQLite DB never received the gate record
    `coord milestone drive` posted to the daemon. `state.get_milestone_gate`
    must route the read to the daemon (mirroring `save_milestone_gate`'s
    write routing) rather than silently consulting an empty local DB."""

    def test_gate_controlled_milestone_refused_via_daemon_routed_read(
        self, config_file: Path, monkeypatch,
    ) -> None:
        from coord import client as cc

        monkeypatch.setattr(
            cc, "resolve_board_service",
            lambda *a, **k: cc.ServiceConfig("http://daemon:7435"),
        )
        # `_load_config` also routes through `resolve_board_service` — feed
        # it the local config file rather than actually reaching out.
        monkeypatch.setattr(cc, "fetch_remote_config", lambda *a, **k: config_file)
        record = mg.GateRecord(
            repo_name="api", tracking_issue=100, gate=mg.GATE_C,
        ).to_dict()
        monkeypatch.setattr(
            cc, "fetch_milestone_gate", lambda svc, repo, issue, **kw: record,
        )
        # The local DB genuinely has nothing — proving the refusal came from
        # the routed read, not a local write that leaked in.
        assert state_mod.list_milestone_gates() == []

        with patch("coord.github_ops.get_issue", side_effect=_get_issue) as get_issue, \
             patch("coord.github_ops.get_open_issues") as get_open, \
             patch("coord.dispatch.dispatch") as disp:
            result = CliRunner().invoke(
                main,
                ["milestone", "dispatch", "api", "100", "--config", str(config_file)],
            )
        assert result.exit_code == 1, result.output
        assert "under gate control" in result.output
        assert "Gate C" in result.output
        get_issue.assert_not_called()
        get_open.assert_not_called()
        disp.assert_not_called()

    def test_no_gate_on_daemon_dispatches_normally(
        self, config_file: Path, monkeypatch,
    ) -> None:
        """Control: the routed read confirming "not gated" (not just "daemon
        unreachable") must still let dispatch proceed."""
        from coord import client as cc

        monkeypatch.setattr(
            cc, "resolve_board_service",
            lambda *a, **k: cc.ServiceConfig("http://daemon:7435"),
        )
        monkeypatch.setattr(cc, "fetch_remote_config", lambda *a, **k: config_file)
        monkeypatch.setattr(
            cc, "fetch_milestone_gate", lambda svc, repo, issue, **kw: None,
        )
        open_issues = [
            {"number": 762, "milestone": {"number": 9}},
            {"number": 763, "milestone": {"number": 9}},
            {"number": 765, "milestone": {"number": 9}},
        ]
        with patch("coord.github_ops.get_issue", side_effect=_get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.board_service.read_board", return_value=Board()), \
             patch("coord.dispatch.dispatch") as disp:
            result = CliRunner().invoke(
                main,
                ["milestone", "dispatch", "api", "100", "--config", str(config_file),
                 "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        assert "under gate control" not in result.output
        disp.assert_not_called()  # dry-run never dispatches regardless

    def test_unreachable_daemon_refuses_rather_than_silently_dispatching(
        self, config_file: Path, monkeypatch,
    ) -> None:
        """The blocking failure mode: a thin client that can't reach the
        daemon has no way to know whether this milestone is gate-controlled.
        It must fail loud/safe (refuse) rather than silently treat "couldn't
        ask" as "not gated" and race the daemon's tick."""
        from coord import client as cc

        monkeypatch.setattr(
            cc, "resolve_board_service",
            lambda *a, **k: cc.ServiceConfig("http://daemon:7435"),
        )
        monkeypatch.setattr(cc, "fetch_remote_config", lambda *a, **k: config_file)

        def _boom(*a, **k):
            raise ConnectionError("daemon unreachable")

        monkeypatch.setattr(cc, "fetch_milestone_gate", _boom)

        with patch("coord.github_ops.get_issue", side_effect=_get_issue) as get_issue, \
             patch("coord.github_ops.get_open_issues") as get_open, \
             patch("coord.dispatch.dispatch") as disp:
            result = CliRunner().invoke(
                main,
                ["milestone", "dispatch", "api", "100", "--config", str(config_file)],
            )
        assert result.exit_code == 1, result.output
        assert "could not check" in result.output
        assert "refusing to dispatch" in result.output
        get_issue.assert_not_called()
        get_open.assert_not_called()
        disp.assert_not_called()
