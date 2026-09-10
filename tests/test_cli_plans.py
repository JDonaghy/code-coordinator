"""Tests for ``coord plans`` — milestone-roster aggregation (#974).

Black-box shape:
- Seed milestones + open-issues + a Board (or empty board).
- Call :func:`coord.plans.aggregate_plan` / :func:`aggregate_repo_plans`
  directly (pure-function tier) and assert on ``PlanEntry`` fields.
- Run ``coord plans --json`` via Click's ``CliRunner`` with GitHub ops mocked
  to confirm the JSON shape + CLI integration.

All GitHub I/O is monkey-patched; no network calls are made.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.models import Assignment, Board
from coord.plans import (
    PlanEntry,
    aggregate_plan,
    aggregate_repo_plans,
    find_tracking_issue,
    find_unlabelled_epics,
)
from coord.state import upsert_open_issues


# ── Helpers ───────────────────────────────────────────────────────────────────


def _active(*, issue: int, repo: str = "api") -> Assignment:
    return Assignment(
        machine_name="laptop",
        repo_name=repo,
        issue_number=issue,
        issue_title=f"issue #{issue}",
        status="running",
        branch=f"issue-{issue}-fix",
        assignment_id=f"a{issue}",
        type="work",
    )


def _chat(
    *, issue: int, repo: str = "api", status: str = "running"
) -> Assignment:
    """A ``type="milestone-chat"`` assignment against the tracking issue —
    what `coord milestone chat <repo> <tracking_issue>` dispatches (#976)."""
    return Assignment(
        machine_name="laptop",
        repo_name=repo,
        issue_number=issue,
        issue_title=f"Milestone chat #{issue}",
        status=status,
        assignment_id=f"chat{issue}",
        type="milestone-chat",
    )


def _ms(number: int, title: str = "") -> dict:
    return {"number": number, "title": title or f"Milestone {number}"}


def _issue(
    number: int,
    *,
    milestone_number: int | None = None,
    labels: list[str] | None = None,
    body: str = "",
) -> dict:
    ms = {"number": milestone_number} if milestone_number is not None else None
    return {
        "number": number,
        "title": f"Issue #{number}",
        "body": body,
        "labels": [{"name": lbl} for lbl in (labels or [])],
        "milestone": ms,
    }


WORK_ORDER_BODY = """\
## Work order
- [ ] #10  {group: A}
- [ ] #11  {group: A}
- [ ] #12  {after: #10,#11}
"""

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


# ── find_tracking_issue ───────────────────────────────────────────────────────


class TestFindTrackingIssue:
    def test_returns_epic_for_matching_milestone(self) -> None:
        issues = [
            _issue(1, milestone_number=5, labels=["bug"]),
            _issue(2, milestone_number=5, labels=["epic"]),
            _issue(3, milestone_number=6, labels=["epic"]),
        ]
        found = find_tracking_issue(5, issues)
        assert found is not None
        assert found["number"] == 2

    def test_returns_none_when_no_epic(self) -> None:
        issues = [_issue(1, milestone_number=5, labels=["bug"])]
        assert find_tracking_issue(5, issues) is None

    def test_returns_none_when_no_issues_for_milestone(self) -> None:
        issues = [_issue(1, milestone_number=7, labels=["epic"])]
        assert find_tracking_issue(5, issues) is None

    def test_returns_first_match_when_multiple_epics(self) -> None:
        issues = [
            _issue(2, milestone_number=5, labels=["epic"]),
            _issue(3, milestone_number=5, labels=["epic"]),
        ]
        found = find_tracking_issue(5, issues)
        assert found is not None
        assert found["number"] == 2


# ── aggregate_plan — needs_you signals ───────────────────────────────────────


class TestAggregatePlanSignals:
    def test_no_tracking_issue_gives_no_work_order(self) -> None:
        entry = aggregate_plan(
            milestone_title="v1",
            milestone_number=1,
            repo_name="api",
            repo_github="acme/api",
            tracking_issue_number=None,
            tracking_body=None,
            board=Board(),
            open_issue_numbers=set(),
        )
        assert entry.has_work_order is False
        assert entry.ready_frontier == 0
        assert entry.total == 0
        assert "no_work_order" in entry.needs_you

    def test_tracking_issue_with_no_work_order_block_gives_no_work_order(self) -> None:
        entry = aggregate_plan(
            milestone_title="v1",
            milestone_number=1,
            repo_name="api",
            repo_github="acme/api",
            tracking_issue_number=99,
            tracking_body="Some prose, no work order heading here.",
            board=Board(),
            open_issue_numbers=set(),
        )
        assert entry.has_work_order is False
        assert "no_work_order" in entry.needs_you
        assert entry.tracking_issue == 99

    def test_ready_frontier_gives_ready_waiting(self) -> None:
        """Work order with no claims → both #10 and #11 are ready."""
        entry = aggregate_plan(
            milestone_title="v1",
            milestone_number=1,
            repo_name="api",
            repo_github="acme/api",
            tracking_issue_number=9,
            tracking_body=WORK_ORDER_BODY,
            board=Board(),
            open_issue_numbers={10, 11, 12},  # all open
        )
        assert entry.has_work_order is True
        assert entry.ready_frontier == 2  # #10 and #11 are ready
        assert entry.blocked == 1  # #12 is blocked on #10,#11
        assert entry.in_flight == 0
        assert entry.done == 0
        assert entry.total == 3
        assert "ready_waiting" in entry.needs_you
        assert "stalled" not in entry.needs_you

    def test_all_in_flight_gives_no_signal(self) -> None:
        """All ready nodes are claimed on the board → no attention signal."""
        board = Board()
        board.active.append(_active(issue=10))
        board.active.append(_active(issue=11))
        entry = aggregate_plan(
            milestone_title="v1",
            milestone_number=1,
            repo_name="api",
            repo_github="acme/api",
            tracking_issue_number=9,
            tracking_body=WORK_ORDER_BODY,
            board=board,
            open_issue_numbers={10, 11, 12},
        )
        assert entry.in_flight == 2
        assert entry.ready_frontier == 0
        assert entry.blocked == 1  # #12 still waiting on deps
        assert "ready_waiting" not in entry.needs_you
        assert "stalled" not in entry.needs_you

    def test_stalled_when_all_blocked_by_conflict(self, monkeypatch) -> None:
        """Stalled signal fires when nothing is ready or in-flight but work remains."""
        from coord import plans as plans_module
        from coord.milestone_order import BlockedNode, Frontier

        # Stub ready_frontier to return: no ready, no claims, one dep-blocked node.
        monkeypatch.setattr(
            plans_module,
            "ready_frontier",
            lambda *a, **kw: Frontier(
                ready=(),
                blocked=(BlockedNode(10, waiting_on_deps=(11,)),),
            ),
        )

        entry = aggregate_plan(
            milestone_title="v1",
            milestone_number=1,
            repo_name="api",
            repo_github="acme/api",
            tracking_issue_number=9,
            tracking_body=WORK_ORDER_BODY,
            board=Board(),
            open_issue_numbers={10, 11, 12},
        )
        assert entry.ready_frontier == 0
        assert entry.in_flight == 0
        assert entry.blocked == 1
        assert "stalled" in entry.needs_you
        assert "ready_waiting" not in entry.needs_you

    def test_done_milestone_has_no_signal(self) -> None:
        """All work-order nodes closed → done==total → empty needs_you."""
        entry = aggregate_plan(
            milestone_title="v1",
            milestone_number=1,
            repo_name="api",
            repo_github="acme/api",
            tracking_issue_number=9,
            tracking_body=WORK_ORDER_BODY,
            board=Board(),
            open_issue_numbers=set(),  # all closed → all terminal
        )
        assert entry.done == 3
        assert entry.total == 3
        assert entry.ready_frontier == 0
        assert entry.needs_you == []

    def test_partial_done_with_ready_nodes(self) -> None:
        """Some nodes done, ready frontier remains → ready_waiting."""
        entry = aggregate_plan(
            milestone_title="v1",
            milestone_number=1,
            repo_name="api",
            repo_github="acme/api",
            tracking_issue_number=9,
            tracking_body=WORK_ORDER_BODY,
            board=Board(),
            open_issue_numbers={12},  # #10,#11 closed → terminal; #12 open
        )
        assert entry.done == 2   # #10 and #11 are terminal
        assert entry.total == 3
        # #12 depends on #10,#11 — both terminal → #12 is now ready
        assert entry.ready_frontier == 1
        assert "ready_waiting" in entry.needs_you

    def test_in_flight_count_from_board(self) -> None:
        board = Board()
        board.active.append(_active(issue=10))
        entry = aggregate_plan(
            milestone_title="v1",
            milestone_number=1,
            repo_name="api",
            repo_github="acme/api",
            tracking_issue_number=9,
            tracking_body=WORK_ORDER_BODY,
            board=board,
            open_issue_numbers={10, 11, 12},
        )
        assert entry.in_flight == 1   # #10 is claimed
        assert entry.ready_frontier == 1  # #11 still ready
        assert entry.blocked == 1  # #12 dep-blocked

    def test_malformed_work_order_treated_as_no_work_order(self) -> None:
        body = "## Work order\n- not a valid checklist\n"
        entry = aggregate_plan(
            milestone_title="v1",
            milestone_number=1,
            repo_name="api",
            repo_github="acme/api",
            tracking_issue_number=9,
            tracking_body=body,
            board=Board(),
            open_issue_numbers=set(),
        )
        assert entry.has_work_order is False
        assert "no_work_order" in entry.needs_you


# ── aggregate_plan — chat_pending signal (#976) ──────────────────────────────


class TestChatPendingSignal:
    def test_chat_pending_alongside_ready_waiting(self) -> None:
        """A running milestone-chat session on the tracking issue surfaces
        `chat_pending` in addition to whatever other signal already fired."""
        board = Board()
        board.active.append(_chat(issue=9))
        entry = aggregate_plan(
            milestone_title="v1",
            milestone_number=1,
            repo_name="api",
            repo_github="acme/api",
            tracking_issue_number=9,
            tracking_body=WORK_ORDER_BODY,
            board=board,
            open_issue_numbers={10, 11, 12},
        )
        assert "ready_waiting" in entry.needs_you
        assert "chat_pending" in entry.needs_you

    def test_chat_pending_with_no_work_order(self) -> None:
        """A milestone-chat session against a tracking issue that has no
        parseable work order yet still surfaces alongside `no_work_order`."""
        board = Board()
        board.active.append(_chat(issue=9))
        entry = aggregate_plan(
            milestone_title="v1",
            milestone_number=1,
            repo_name="api",
            repo_github="acme/api",
            tracking_issue_number=9,
            tracking_body=None,
            board=board,
            open_issue_numbers=set(),
        )
        assert entry.has_work_order is False
        assert "no_work_order" in entry.needs_you
        assert "chat_pending" in entry.needs_you

    def test_chat_pending_absent_when_no_active_chat(self) -> None:
        entry = aggregate_plan(
            milestone_title="v1",
            milestone_number=1,
            repo_name="api",
            repo_github="acme/api",
            tracking_issue_number=9,
            tracking_body=WORK_ORDER_BODY,
            board=Board(),
            open_issue_numbers={10, 11, 12},
        )
        assert "chat_pending" not in entry.needs_you

    def test_chat_pending_ignores_other_assignment_types(self) -> None:
        """A `work` assignment on the tracking issue number is not a chat —
        only `type="milestone-chat"` counts."""
        board = Board()
        board.active.append(_active(issue=9))
        entry = aggregate_plan(
            milestone_title="v1",
            milestone_number=1,
            repo_name="api",
            repo_github="acme/api",
            tracking_issue_number=9,
            tracking_body=WORK_ORDER_BODY,
            board=board,
            open_issue_numbers={10, 11, 12},
        )
        assert "chat_pending" not in entry.needs_you

    def test_chat_pending_ignores_failed_chat(self) -> None:
        """A failed chat dispatch never actually opened — not "pending"."""
        board = Board()
        board.active.append(_chat(issue=9, status="failed"))
        entry = aggregate_plan(
            milestone_title="v1",
            milestone_number=1,
            repo_name="api",
            repo_github="acme/api",
            tracking_issue_number=9,
            tracking_body=WORK_ORDER_BODY,
            board=board,
            open_issue_numbers={10, 11, 12},
        )
        assert "chat_pending" not in entry.needs_you

    def test_chat_pending_scoped_to_repo(self) -> None:
        """A milestone-chat session on the same issue number but a different
        repo must not leak across repos."""
        board = Board()
        board.active.append(_chat(issue=9, repo="other-repo"))
        entry = aggregate_plan(
            milestone_title="v1",
            milestone_number=1,
            repo_name="api",
            repo_github="acme/api",
            tracking_issue_number=9,
            tracking_body=WORK_ORDER_BODY,
            board=board,
            open_issue_numbers={10, 11, 12},
        )
        assert "chat_pending" not in entry.needs_you

    def test_chat_pending_true_on_done_milestone(self) -> None:
        """chat_pending is orthogonal — it can fire even when every
        work-order node is done and no other signal would otherwise."""
        board = Board()
        board.active.append(_chat(issue=9))
        entry = aggregate_plan(
            milestone_title="v1",
            milestone_number=1,
            repo_name="api",
            repo_github="acme/api",
            tracking_issue_number=9,
            tracking_body=WORK_ORDER_BODY,
            board=board,
            open_issue_numbers=set(),  # all closed → all terminal
        )
        assert entry.done == entry.total
        assert entry.needs_you == ["chat_pending"]


# ── aggregate_repo_plans ─────────────────────────────────────────────────────


class TestAggregateRepoPlans:
    def test_returns_one_entry_per_milestone(self) -> None:
        milestones = [_ms(1, "v1"), _ms(2, "v2")]
        open_issues = [
            _issue(9, milestone_number=1, labels=["epic"], body=WORK_ORDER_BODY),
            _issue(10, milestone_number=1),
            _issue(11, milestone_number=1),
            _issue(12, milestone_number=1),
        ]
        entries = aggregate_repo_plans(
            repo_name="api",
            repo_github="acme/api",
            milestones=milestones,
            open_issues=open_issues,
            board=Board(),
        )
        assert len(entries) == 2
        assert entries[0].milestone_number == 1
        assert entries[1].milestone_number == 2

    def test_milestone_without_epic_gets_no_work_order(self) -> None:
        milestones = [_ms(3, "v3")]
        open_issues = [_issue(30, milestone_number=3, labels=["bug"])]
        entries = aggregate_repo_plans(
            repo_name="api",
            repo_github="acme/api",
            milestones=milestones,
            open_issues=open_issues,
            board=Board(),
        )
        assert len(entries) == 1
        assert entries[0].has_work_order is False
        assert "no_work_order" in entries[0].needs_you

    def test_uses_body_from_open_issues_index(self) -> None:
        milestones = [_ms(4, "v4")]
        # The epic's body comes from the open_issues snapshot.
        open_issues = [
            _issue(40, milestone_number=4, labels=["epic"], body=WORK_ORDER_BODY),
            _issue(10, milestone_number=4),
            _issue(11, milestone_number=4),
            _issue(12, milestone_number=4),
        ]
        entries = aggregate_repo_plans(
            repo_name="api",
            repo_github="acme/api",
            milestones=milestones,
            open_issues=open_issues,
            board=Board(),
        )
        assert entries[0].has_work_order is True
        assert entries[0].tracking_issue == 40

    def test_issue_body_fetcher_called_when_body_not_in_snapshot(self) -> None:
        """Body fetcher is used when the epic's body isn't in open_issues."""
        milestones = [_ms(5, "v5")]
        # Epic is listed but its body is empty in the snapshot.
        open_issues = [
            _issue(50, milestone_number=5, labels=["epic"], body=""),
            _issue(10, milestone_number=5),
            _issue(11, milestone_number=5),
            _issue(12, milestone_number=5),
        ]
        fetcher_calls: list[int] = []

        def _fetcher(issue_number: int) -> str:
            fetcher_calls.append(issue_number)
            return WORK_ORDER_BODY

        entries = aggregate_repo_plans(
            repo_name="api",
            repo_github="acme/api",
            milestones=milestones,
            open_issues=open_issues,
            board=Board(),
            issue_body_fetcher=_fetcher,
        )
        # Empty body → falsy → fetcher is called.
        assert fetcher_calls == [50]
        assert entries[0].has_work_order is True

    def test_closed_epic_still_found_via_closed_tracking_issues(self) -> None:
        """A milestone whose tracking epic was closed (but the milestone
        stayed open) still resolves its work order when the closed epic is
        supplied via ``closed_tracking_issues`` — #974 review finding #1."""
        milestones = [_ms(6, "v6")]
        # No open epic under milestone 6 — only plain work-order-node issues.
        open_issues = [
            _issue(10, milestone_number=6),
            _issue(11, milestone_number=6),
            _issue(12, milestone_number=6),
        ]
        closed_epics = [
            _issue(60, milestone_number=6, labels=["epic"], body=WORK_ORDER_BODY),
        ]
        entries = aggregate_repo_plans(
            repo_name="api",
            repo_github="acme/api",
            milestones=milestones,
            open_issues=open_issues,
            board=Board(),
            closed_tracking_issues=closed_epics,
        )
        assert len(entries) == 1
        assert entries[0].tracking_issue == 60
        assert entries[0].has_work_order is True
        # #10 and #11 open and unclaimed → ready.
        assert entries[0].ready_frontier == 2

    def test_without_closed_tracking_issues_reports_no_work_order(self) -> None:
        """Baseline: omitting ``closed_tracking_issues`` (the default) keeps
        the open-only behaviour — a closed-only epic is not found."""
        milestones = [_ms(7, "v7")]
        open_issues = [_issue(10, milestone_number=7)]
        entries = aggregate_repo_plans(
            repo_name="api",
            repo_github="acme/api",
            milestones=milestones,
            open_issues=open_issues,
            board=Board(),
        )
        assert entries[0].has_work_order is False
        assert "no_work_order" in entries[0].needs_you


# ── CLI integration ───────────────────────────────────────────────────────────


class TestPlansCli:
    def test_json_output_is_valid_json_array(self, config_file: Path) -> None:
        milestones = [{"number": 1, "title": "v1"}]
        open_issues = [
            _issue(9, milestone_number=1, labels=["epic"], body=WORK_ORDER_BODY),
            _issue(10, milestone_number=1),
            _issue(11, milestone_number=1),
            _issue(12, milestone_number=1),
        ]
        with (
            patch("coord.github_ops.get_repo_milestones", return_value=milestones),
            patch("coord.github_ops.get_open_issues", return_value=open_issues),
            patch("coord.github_ops.get_closed_epics", return_value=[]),
        ):
            result = CliRunner().invoke(
                main, ["plans", "--json", "--config", str(config_file)]
            )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["milestone_number"] == 1
        assert data[0]["title"] == "v1"
        assert data[0]["repo"] == "api"
        assert data[0]["has_work_order"] is True
        assert data[0]["ready_frontier"] == 2
        assert "ready_waiting" in data[0]["needs_you"]

    def test_json_contains_all_expected_fields(self, config_file: Path) -> None:
        milestones = [{"number": 2, "title": "v2"}]
        open_issues: list[dict] = []  # no epic → no_work_order
        with (
            patch("coord.github_ops.get_repo_milestones", return_value=milestones),
            patch("coord.github_ops.get_open_issues", return_value=open_issues),
            patch("coord.github_ops.get_closed_epics", return_value=[]),
        ):
            result = CliRunner().invoke(
                main, ["plans", "--json", "--config", str(config_file)]
            )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert len(data) == 1
        entry = data[0]
        for field in (
            "repo", "title", "milestone_number", "tracking_issue",
            "has_work_order", "ready_frontier", "blocked",
            "in_flight", "done", "total", "needs_you",
        ):
            assert field in entry, f"missing field: {field}"
        assert entry["needs_you"] == ["no_work_order"]
        assert entry["tracking_issue"] is None

    def test_no_milestones_prints_empty_message(self, config_file: Path) -> None:
        with (
            patch("coord.github_ops.get_repo_milestones", return_value=[]),
            patch("coord.github_ops.get_open_issues", return_value=[]),
            patch("coord.github_ops.get_closed_epics", return_value=[]),
        ):
            result = CliRunner().invoke(
                main, ["plans", "--config", str(config_file)]
            )
        assert result.exit_code == 0
        assert "No open milestones" in result.output

    def test_plain_output_without_json_flag(self, config_file: Path) -> None:
        milestones = [{"number": 3, "title": "v3"}]
        open_issues = [
            _issue(30, milestone_number=3, labels=["epic"], body=WORK_ORDER_BODY),
            _issue(10, milestone_number=3),
            _issue(11, milestone_number=3),
            _issue(12, milestone_number=3),
        ]
        with (
            patch("coord.github_ops.get_repo_milestones", return_value=milestones),
            patch("coord.github_ops.get_open_issues", return_value=open_issues),
            patch("coord.github_ops.get_closed_epics", return_value=[]),
        ):
            result = CliRunner().invoke(
                main, ["plans", "--config", str(config_file)]
            )
        assert result.exit_code == 0
        assert "#3" in result.output
        assert "v3" in result.output
        assert "ready_waiting" in result.output

    def test_repo_filter_restricts_to_single_repo(self, config_file: Path) -> None:
        """--repo limits the query to one repo."""
        milestones = [{"number": 5, "title": "v5"}]
        open_issues: list[dict] = []
        with (
            patch(
                "coord.github_ops.get_repo_milestones", return_value=milestones
            ) as mock_ms,
            patch("coord.github_ops.get_open_issues", return_value=open_issues),
            patch("coord.github_ops.get_closed_epics", return_value=[]),
        ):
            result = CliRunner().invoke(
                main, ["plans", "--repo", "api", "--json", "--config", str(config_file)]
            )
        assert result.exit_code == 0
        # Only one get_repo_milestones call — for the one repo.
        mock_ms.assert_called_once_with("acme/api")

    def test_unknown_repo_exits_with_error(self, config_file: Path) -> None:
        result = CliRunner().invoke(
            main, ["plans", "--repo", "nonexistent", "--config", str(config_file)]
        )
        assert result.exit_code == 2

    def test_github_error_emits_warning_and_continues(self, config_file: Path) -> None:
        with (
            patch(
                "coord.github_ops.get_repo_milestones",
                side_effect=RuntimeError("network down"),
            ),
        ):
            result = CliRunner().invoke(
                main, ["plans", "--json", "--config", str(config_file)]
            )
        assert result.exit_code == 0
        # The output contains a warning line and then the JSON array.
        # Extract the JSON by finding the first '[' character.
        output = result.output
        assert "warning" in output.lower()
        json_start = output.index("[")
        data = json.loads(output[json_start:])
        assert data == []

    def test_closed_epic_resolves_milestone_end_to_end(self, config_file: Path) -> None:
        """#974 review finding #1: a closed tracking epic (from
        ``get_closed_epics``) is still used to resolve the milestone's work
        order, end-to-end through the CLI command."""
        milestones = [{"number": 8, "title": "v8"}]
        open_issues = [
            _issue(10, milestone_number=8),
            _issue(11, milestone_number=8),
            _issue(12, milestone_number=8),
        ]
        closed_epics = [
            _issue(80, milestone_number=8, labels=["epic"], body=WORK_ORDER_BODY),
        ]
        with (
            patch("coord.github_ops.get_repo_milestones", return_value=milestones),
            patch("coord.github_ops.get_open_issues", return_value=open_issues),
            patch("coord.github_ops.get_closed_epics", return_value=closed_epics),
        ):
            result = CliRunner().invoke(
                main, ["plans", "--json", "--config", str(config_file)]
            )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert len(data) == 1
        assert data[0]["tracking_issue"] == 80
        assert data[0]["has_work_order"] is True
        assert "no_work_order" not in data[0]["needs_you"]

    def test_closed_epics_fetch_error_falls_back_to_open_only(
        self, config_file: Path
    ) -> None:
        """A failure fetching closed epics degrades to open-only lookup
        (with a warning) instead of failing the whole command."""
        milestones = [{"number": 9, "title": "v9"}]
        open_issues = [
            _issue(90, milestone_number=9, labels=["epic"], body=WORK_ORDER_BODY),
            _issue(10, milestone_number=9),
            _issue(11, milestone_number=9),
            _issue(12, milestone_number=9),
        ]
        with (
            patch("coord.github_ops.get_repo_milestones", return_value=milestones),
            patch("coord.github_ops.get_open_issues", return_value=open_issues),
            patch(
                "coord.github_ops.get_closed_epics",
                side_effect=RuntimeError("network down"),
            ),
        ):
            result = CliRunner().invoke(
                main, ["plans", "--json", "--config", str(config_file)]
            )
        assert result.exit_code == 0, result.output
        assert "warning" in result.output.lower()
        json_start = result.output.index("[")
        data = json.loads(result.output[json_start:])
        assert len(data) == 1
        assert data[0]["tracking_issue"] == 90
        assert data[0]["has_work_order"] is True

    def test_to_dict_round_trips_all_fields(self) -> None:
        entry = PlanEntry(
            repo="api",
            title="v1",
            milestone_number=1,
            tracking_issue=42,
            has_work_order=True,
            ready_frontier=2,
            blocked=1,
            in_flight=1,
            done=0,
            total=4,
            needs_you=["ready_waiting"],
        )
        d = entry.to_dict()
        assert d["repo"] == "api"
        assert d["tracking_issue"] == 42
        assert d["needs_you"] == ["ready_waiting"]
        # Confirm it's JSON-serialisable.
        round_tripped = json.loads(json.dumps(d))
        assert round_tripped == d


# ── find_unlabelled_epics (#3227) ────────────────────────────────────────────


def _cached_issue(
    number: int,
    title: str,
    *,
    repo_name: str = "api",
    state: str = "open",
    labels: list[str] | None = None,
) -> dict:
    """A local-cache-shaped issue dict, matching what
    :func:`coord.dao.SqliteStore.list_issues` decodes from the ``issues``
    table (``labels`` as a plain ``list[str]``, unlike GitHub's raw
    ``[{"name": ...}]`` shape that :func:`_issue` above builds)."""
    return {
        "repo_name": repo_name,
        "number": number,
        "title": title,
        "state": state,
        "labels": labels or [],
    }


class TestFindUnlabelledEpics:
    def test_title_matches_but_unlabelled_is_flagged(self) -> None:
        issues = [_cached_issue(837, "[platform] Epic: multi-tenant billing")]
        hits = find_unlabelled_epics(issues)
        assert [h["number"] for h in hits] == [837]

    def test_title_matches_and_labelled_is_not_flagged(self) -> None:
        issues = [
            _cached_issue(
                836, "[platform] Epic: something already fixed", labels=["epic"]
            )
        ]
        assert find_unlabelled_epics(issues) == []

    def test_title_doesnt_match_is_not_flagged(self) -> None:
        issues = [_cached_issue(1, "Fix flaky test in reconcile loop")]
        assert find_unlabelled_epics(issues) == []

    def test_matches_bare_epic_prefix_case_insensitive(self) -> None:
        issues = [
            _cached_issue(380, "Epic: goal-driven autonomous planner"),
            _cached_issue(531, "EPIC: coordinator<->vimcode integration"),
        ]
        hits = find_unlabelled_epics(issues)
        assert {h["number"] for h in hits} == {380, 531}

    def test_matches_bare_bracket_epic_tag(self) -> None:
        issues = [_cached_issue(1, "[epic] some big initiative")]
        hits = find_unlabelled_epics(issues)
        assert [h["number"] for h in hits] == [1]

    def test_closed_issue_is_not_flagged(self) -> None:
        issues = [_cached_issue(2, "Epic: dead epic", state="closed")]
        assert find_unlabelled_epics(issues) == []

    def test_word_epic_mid_title_does_not_false_positive(self) -> None:
        """"Epic" appearing later in a title, or without a colon
        immediately after it, must not trip the lint."""
        issues = [
            _cached_issue(3, "Epicurious recipe importer"),
            _cached_issue(4, "Make the onboarding flow more epic"),
        ]
        assert find_unlabelled_epics(issues) == []

    def test_accepts_github_shaped_labels_too(self) -> None:
        """A GitHub-raw ``[{"name": ...}]`` labels list (as
        :func:`find_tracking_issue` consumes) is also tolerated."""
        issues = [
            _cached_issue(5, "Epic: labelled via github shape")
            | {"labels": [{"name": "epic"}]}
        ]
        assert find_unlabelled_epics(issues) == []


# ── coord plans --lint-epics CLI integration (#3227) ────────────────────────


class TestLintEpicsCli:
    def test_flags_unlabelled_epic_from_local_cache(self, config_file: Path) -> None:
        upsert_open_issues(
            "api",
            [
                {
                    "number": 837,
                    "title": "[platform] Epic: multi-tenant billing",
                    "body": "",
                    "labels": [],
                },
                {
                    "number": 836,
                    "title": "[platform] Epic: already labelled",
                    "body": "",
                    "labels": [{"name": "epic"}],
                },
                {
                    "number": 1,
                    "title": "Fix flaky test",
                    "body": "",
                    "labels": [],
                },
            ],
        )
        with (
            patch("coord.github_ops.get_repo_milestones", return_value=[]),
            patch("coord.github_ops.get_open_issues", return_value=[]),
            patch("coord.github_ops.get_closed_epics", return_value=[]),
        ):
            result = CliRunner().invoke(
                main,
                ["plans", "--lint-epics", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        assert "#837" in result.output
        assert "already labelled" not in result.output
        assert "Fix flaky test" not in result.output
        # No GitHub calls happened for the lint itself — get_open_issues was
        # only ever called with [] (no milestones), confirming the lint read
        # came from the local cache, not a network fetch.

    def test_no_hits_prints_clean_message(self, config_file: Path) -> None:
        upsert_open_issues(
            "api",
            [{"number": 1, "title": "Fix flaky test", "body": "", "labels": []}],
        )
        with (
            patch("coord.github_ops.get_repo_milestones", return_value=[]),
            patch("coord.github_ops.get_open_issues", return_value=[]),
            patch("coord.github_ops.get_closed_epics", return_value=[]),
        ):
            result = CliRunner().invoke(
                main,
                ["plans", "--lint-epics", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        assert "No unlabelled epics found." in result.output

    def test_json_output_carries_unlabelled_epics_key(self, config_file: Path) -> None:
        upsert_open_issues(
            "api",
            [
                {
                    "number": 380,
                    "title": "Epic: goal-driven autonomous planner",
                    "body": "",
                    "labels": [],
                }
            ],
        )
        with (
            patch("coord.github_ops.get_repo_milestones", return_value=[]),
            patch("coord.github_ops.get_open_issues", return_value=[]),
            patch("coord.github_ops.get_closed_epics", return_value=[]),
        ):
            result = CliRunner().invoke(
                main,
                ["plans", "--lint-epics", "--json", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["plans"] == []
        assert data["unlabelled_epics"] == [
            {"repo": "api", "number": 380, "title": "Epic: goal-driven autonomous planner"}
        ]

    def test_without_flag_json_stays_a_plain_array(self, config_file: Path) -> None:
        """Backward compatibility: omitting --lint-epics keeps --json's
        historical shape (a bare array), unaffected by the new flag."""
        with (
            patch("coord.github_ops.get_repo_milestones", return_value=[]),
            patch("coord.github_ops.get_open_issues", return_value=[]),
            patch("coord.github_ops.get_closed_epics", return_value=[]),
        ):
            result = CliRunner().invoke(
                main, ["plans", "--json", "--config", str(config_file)]
            )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data == []
