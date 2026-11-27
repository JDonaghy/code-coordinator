"""Tests for the native tranche row's parallel child dispatch and
structured result contract (#4129 Phase 1, this slice #4200):

- `coord.dispatch.dispatch_tranche_row_children` — fans a row's pending
  children out in parallel, each branched off the row's shared
  `base_sha`, with no per-child PR or review.
- `coord.dispatch.parse_tranche_child_result` — the structured result
  contract every child's completion text is graded against, failing
  closed on anything unparseable.
- `coord.drive_queue.tranche_rows_ready_to_dispatch` — which rows a tick
  should dispatch.
- `coord.drive.apply_tranche_child_result` / `tranche_row_all_children_
  terminal` — folding a child's graded result back into the row.

No merge/integration/Test/review behavior exists yet — those are the
sibling slices (#4201/#4202).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from coord.config import Config
from coord.dispatch import (
    TRANCHE_CHILD_RESULT_CONTRACT,
    TRANCHE_CHILD_RESULT_MARKER,
    build_tranche_child_proposal,
    dispatch_tranche_row_children,
    ensure_tranche_child_branch,
    parse_tranche_child_result,
    tranche_child_branch_name,
)
from coord.drive import apply_tranche_child_result, tranche_row_all_children_terminal
from coord.drive_queue import tranche_rows_ready_to_dispatch
from coord.models import Machine, Repo, TrancheChild, TrancheRow


def _row(**overrides) -> TrancheRow:
    defaults: dict = dict(
        id=7,
        repo_name="api",
        issue_number=100,
        issue_title="Decompose the big thing",
        base_sha="deadbeef",
        children=[
            TrancheChild(issue_number=101, repo_name="api", title="Child A"),
            TrancheChild(issue_number=102, repo_name="api", title="Child B"),
        ],
    )
    defaults.update(overrides)
    return TrancheRow(**defaults)


class TestTrancheChildBranchName:
    def test_deterministic_from_row_id_and_issue_number(self) -> None:
        row = _row()
        child = row.children[0]
        assert tranche_child_branch_name(row, child) == "tranche-7-child-101"

    def test_raises_when_row_has_no_id(self) -> None:
        row = _row(id=None)
        with pytest.raises(ValueError, match="row.id is None"):
            tranche_child_branch_name(row, row.children[0])


class TestEnsureTrancheChildBranch:
    def test_noop_when_branch_already_exists(self) -> None:
        exists = MagicMock(return_value=True)
        create = MagicMock(return_value=True)
        get_sha = MagicMock(return_value="deadbeef")
        ensure_tranche_child_branch(
            "acme/api", "tranche-7-child-101", "deadbeef",
            exists=exists, create=create, get_sha=get_sha,
        )
        exists.assert_called_once_with("acme/api", "tranche-7-child-101")
        create.assert_not_called()

    def test_creates_branch_at_base_sha_when_missing(self) -> None:
        exists = MagicMock(return_value=False)
        create = MagicMock(return_value=True)
        get_sha = MagicMock(return_value="deadbeef")
        ensure_tranche_child_branch(
            "acme/api", "tranche-7-child-101", "deadbeef",
            exists=exists, create=create, get_sha=get_sha,
        )
        create.assert_called_once_with("acme/api", "tranche-7-child-101", "deadbeef")

    def test_fails_closed_when_create_reports_failure(self) -> None:
        exists = MagicMock(return_value=False)
        create = MagicMock(return_value=False)
        get_sha = MagicMock(return_value="deadbeef")
        with pytest.raises(RuntimeError, match="failed to create"):
            ensure_tranche_child_branch(
                "acme/api", "tranche-7-child-101", "deadbeef",
                exists=exists, create=create, get_sha=get_sha,
            )

    def test_fails_closed_when_existing_branch_sha_does_not_match_base(self) -> None:
        # `exists` fails OPEN on any `gh` hiccup, so it alone cannot prove
        # the branch is really at `base_sha` — a stale branch from a
        # previous attempt, or a reused row id after a DB restore, must
        # still be caught here.
        exists = MagicMock(return_value=True)
        create = MagicMock(return_value=True)
        get_sha = MagicMock(return_value="stale-sha")
        with pytest.raises(RuntimeError, match="not the expected base"):
            ensure_tranche_child_branch(
                "acme/api", "tranche-7-child-101", "deadbeef",
                exists=exists, create=create, get_sha=get_sha,
            )
        create.assert_not_called()

    def test_fails_closed_when_newly_created_branch_sha_is_unreadable(self) -> None:
        exists = MagicMock(return_value=False)
        create = MagicMock(return_value=True)
        get_sha = MagicMock(return_value=None)
        with pytest.raises(RuntimeError, match="not the expected base"):
            ensure_tranche_child_branch(
                "acme/api", "tranche-7-child-101", "deadbeef",
                exists=exists, create=create, get_sha=get_sha,
            )


class TestParseTrancheChildResult:
    def test_valid_marker_parses(self) -> None:
        text = (
            "did the work\n"
            f'{TRANCHE_CHILD_RESULT_MARKER} {{"outcome": "done", '
            '"summary": "added the thing", "files": ["a.py", "b.py"]}\n'
        )
        result = parse_tranche_child_result(text)
        assert result == {
            "outcome": "done",
            "summary": "added the thing",
            "files": ["a.py", "b.py"],
            "fail_closed_unparseable": False,
        }

    def test_valid_marker_with_defaults_omitted(self) -> None:
        text = f'{TRANCHE_CHILD_RESULT_MARKER} {{"outcome": "failed"}}'
        result = parse_tranche_child_result(text)
        assert result["outcome"] == "failed"
        assert result["summary"] == ""
        assert result["files"] == []
        assert result["fail_closed_unparseable"] is False

    def test_last_marker_wins(self) -> None:
        text = (
            f'{TRANCHE_CHILD_RESULT_MARKER} {{"outcome": "done", "summary": "draft"}}\n'
            "more work happened\n"
            f'{TRANCHE_CHILD_RESULT_MARKER} {{"outcome": "failed", "summary": "final"}}\n'
        )
        result = parse_tranche_child_result(text)
        assert result["outcome"] == "failed"
        assert result["summary"] == "final"

    @pytest.mark.parametrize(
        "text",
        [
            None,
            "",
            "no marker anywhere in this completion text",
            f"{TRANCHE_CHILD_RESULT_MARKER} not json at all",
            f'{TRANCHE_CHILD_RESULT_MARKER} {{"outcome": "maybe"}}',
            f'{TRANCHE_CHILD_RESULT_MARKER} {{"outcome": "done", "summary": 5}}',
            f'{TRANCHE_CHILD_RESULT_MARKER} {{"outcome": "done", "files": "a.py"}}',
            f'{TRANCHE_CHILD_RESULT_MARKER} {{"outcome": "done", "files": [1, 2]}}',
            f'{TRANCHE_CHILD_RESULT_MARKER} ["not", "an", "object"]',
        ],
    )
    def test_fails_closed_on_unparseable_input(self, text) -> None:
        result = parse_tranche_child_result(text)
        assert result["outcome"] == "failed"
        assert result["fail_closed_unparseable"] is True

    def test_never_raises_on_garbage(self) -> None:
        # Defensive: a worker's raw transcript can contain anything.
        parse_tranche_child_result("\x00\x01 garbage \n" * 3)


class TestBuildTrancheChildProposal:
    def test_proposal_targets_the_per_child_branch_and_carries_the_contract(self) -> None:
        row = _row()
        child = row.children[0]
        proposal = build_tranche_child_proposal(row, child, "laptop")
        assert proposal.machine_name == "laptop"
        assert proposal.repo_name == "api"
        assert proposal.issue_number == 101
        assert proposal.type == "work"
        assert proposal.target_branch == "tranche-7-child-101"
        assert "Child A" in proposal.briefing
        assert TRANCHE_CHILD_RESULT_MARKER in proposal.briefing
        assert "no PR" in TRANCHE_CHILD_RESULT_CONTRACT.lower() or "pull request" in proposal.briefing.lower()


class TestDispatchTrancheRowChildren:
    @pytest.fixture
    def config(self) -> Config:
        return Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[
                Machine(name="laptop", host="laptop.tailnet", repos=["api"],
                         repo_paths={"api": "/home/user/src/api"}),
                Machine(name="desktop", host="desktop.tailnet", repos=["api"],
                         repo_paths={"api": "/home/user/src/api"}),
            ],
        )

    def test_dispatches_every_pending_child_in_parallel(self, config: Config) -> None:
        row = _row()
        dispatcher = MagicMock(return_value={"id": "agent-assignment-1"})
        exists = MagicMock(return_value=True)
        create = MagicMock(return_value=True)
        sha = MagicMock(return_value="deadbeef")

        result_row = dispatch_tranche_row_children(
            row, config, ["laptop", "desktop"],
            dispatcher=dispatcher, branch_exists=exists, create_branch=create,
            branch_sha=sha,
        )

        assert result_row is row
        assert row.state == "dispatched"
        assert {c.state for c in row.children} == {"dispatched"}
        assert dispatcher.call_count == 2
        dispatched_machines = {
            call.args[0].machine_name for call in dispatcher.call_args_list
        }
        assert dispatched_machines == {"laptop", "desktop"}
        # Each child's result carries the handle (#4200) a later poll
        # needs to go collect its completion — the agent assignment id
        # and the machine it actually landed on.
        for child in row.children:
            assert child.result == {
                "assignment_id": "agent-assignment-1",
                "machine_name": child.result["machine_name"],
            }
            assert child.result["machine_name"] in {"laptop", "desktop"}

    def test_round_robins_machines_when_fewer_than_children(self, config: Config) -> None:
        row = _row(children=[
            TrancheChild(issue_number=101, repo_name="api", title="A"),
            TrancheChild(issue_number=102, repo_name="api", title="B"),
            TrancheChild(issue_number=103, repo_name="api", title="C"),
        ])
        dispatcher = MagicMock(return_value={"id": "x"})
        dispatch_tranche_row_children(
            row, config, ["laptop"],
            dispatcher=dispatcher,
            branch_exists=MagicMock(return_value=True),
            create_branch=MagicMock(return_value=True),
            branch_sha=MagicMock(return_value="deadbeef"),
        )
        assert dispatcher.call_count == 3
        assert all(
            call.args[0].machine_name == "laptop" for call in dispatcher.call_args_list
        )

    def test_a_childs_dispatch_failure_fails_closed_without_blocking_siblings(
        self, config: Config,
    ) -> None:
        row = _row()

        def _flaky_dispatch(proposal, _config, **_kwargs):
            if proposal.issue_number == 101:
                raise RuntimeError("agent unreachable")
            return {"id": "ok"}

        dispatch_tranche_row_children(
            row, config, ["laptop"],
            dispatcher=_flaky_dispatch,
            branch_exists=MagicMock(return_value=True),
            create_branch=MagicMock(return_value=True),
            branch_sha=MagicMock(return_value="deadbeef"),
        )

        failed = next(c for c in row.children if c.issue_number == 101)
        ok = next(c for c in row.children if c.issue_number == 102)
        assert failed.state == "failed"
        assert failed.result is not None
        assert failed.result["outcome"] == "failed"
        assert "agent unreachable" in failed.result["summary"]
        assert ok.state == "dispatched"
        assert ok.result == {"assignment_id": "ok", "machine_name": "laptop"}
        # The row still advances — #4201 needs to see which children
        # actually started, not have the whole row stuck "pending".
        assert row.state == "dispatched"

    def test_fails_closed_when_agent_response_reports_failure(
        self, config: Config,
    ) -> None:
        # `/assign` returns HTTP 202 even when the agent's own worktree
        # setup failed — `dispatch()`'s `raise_for_status()` is happy with
        # that, so a 2xx response alone proves nothing. The child must be
        # read as `"failed"`, not `"dispatched"`, when the agent already
        # said so.
        row = _row()
        dispatcher = MagicMock(return_value={
            "id": "agent-assignment-1", "status": "failed",
            "error": "worktree not writable: disk full",
        })

        dispatch_tranche_row_children(
            row, config, ["laptop"],
            dispatcher=dispatcher,
            branch_exists=MagicMock(return_value=True),
            create_branch=MagicMock(return_value=True),
            branch_sha=MagicMock(return_value="deadbeef"),
        )

        assert {c.state for c in row.children} == {"failed"}
        for child in row.children:
            assert "worktree not writable" in child.result["summary"]

    def test_fails_closed_when_agent_response_carries_no_id(
        self, config: Config,
    ) -> None:
        row = _row()
        dispatcher = MagicMock(return_value={})

        dispatch_tranche_row_children(
            row, config, ["laptop"],
            dispatcher=dispatcher,
            branch_exists=MagicMock(return_value=True),
            create_branch=MagicMock(return_value=True),
            branch_sha=MagicMock(return_value="deadbeef"),
        )

        assert {c.state for c in row.children} == {"failed"}

    def test_fails_closed_when_branch_cannot_be_confirmed_at_base_sha(
        self, config: Config,
    ) -> None:
        # `branch_exists_on_remote` fails OPEN on a `gh` hiccup, so a
        # stale/foreign branch reading as "exists" must still block
        # dispatch rather than let the agent's worker start from whatever
        # that ref actually points at.
        row = _row()
        dispatcher = MagicMock(return_value={"id": "agent-assignment-1"})

        dispatch_tranche_row_children(
            row, config, ["laptop"],
            dispatcher=dispatcher,
            branch_exists=MagicMock(return_value=True),
            create_branch=MagicMock(return_value=True),
            branch_sha=MagicMock(return_value="some-other-sha"),
        )

        assert {c.state for c in row.children} == {"failed"}
        dispatcher.assert_not_called()

    def test_raises_when_repo_is_not_configured(self) -> None:
        # `Repo.github` is a mandatory field, so the only way
        # `config.repo(row.repo_name)` yields a falsy slug is when the
        # row's repo is absent from `coordinator.yml` entirely.
        row = _row()
        config = Config(
            repos=[Repo(name="other", github="acme/other")],
            machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"],
                               repo_paths={"api": "/home/user/src/api"})],
        )
        with pytest.raises(ValueError, match="no `github` slug configured"):
            dispatch_tranche_row_children(row, config, ["laptop"])

    def test_forwards_routing_fetchers_and_board_to_dispatcher(
        self, config: Config,
    ) -> None:
        row = _row()
        dispatcher = MagicMock(return_value={"id": "agent-assignment-1"})
        status_fetcher = MagicMock()
        credential_fetcher = MagicMock()
        board = MagicMock()

        dispatch_tranche_row_children(
            row, config, ["laptop"],
            dispatcher=dispatcher,
            branch_exists=MagicMock(return_value=True),
            create_branch=MagicMock(return_value=True),
            branch_sha=MagicMock(return_value="deadbeef"),
            status_fetcher=status_fetcher,
            credential_fetcher=credential_fetcher,
            board=board,
        )

        for call in dispatcher.call_args_list:
            assert call.kwargs["status_fetcher"] is status_fetcher
            assert call.kwargs["credential_fetcher"] is credential_fetcher
            assert call.kwargs["board"] is board

    def test_raises_when_row_has_no_id(self, config: Config) -> None:
        row = _row(id=None)
        with pytest.raises(ValueError, match="row.id is None"):
            dispatch_tranche_row_children(row, config, ["laptop"])

    def test_raises_when_no_pending_children(self, config: Config) -> None:
        row = _row(children=[
            TrancheChild(issue_number=101, repo_name="api", state="done"),
        ])
        with pytest.raises(ValueError, match="no pending children"):
            dispatch_tranche_row_children(row, config, ["laptop"])

    def test_raises_when_machine_names_empty(self, config: Config) -> None:
        row = _row()
        with pytest.raises(ValueError, match="machine_names is empty"):
            dispatch_tranche_row_children(row, config, [])


class TestTrancheRowsReadyToDispatch:
    def test_filters_to_pending_rows_with_children(self) -> None:
        pending_with_children = _row(id=1, state="pending")
        pending_no_children = _row(id=2, state="pending", children=[])
        already_dispatched = _row(id=3, state="dispatched")
        rows = [pending_with_children, pending_no_children, already_dispatched]
        assert tranche_rows_ready_to_dispatch(rows) == [pending_with_children]

    def test_empty_input_returns_empty(self) -> None:
        assert tranche_rows_ready_to_dispatch([]) == []


class TestApplyTrancheChildResult:
    def test_marks_child_done_on_a_done_outcome(self) -> None:
        row = _row()
        text = f'{TRANCHE_CHILD_RESULT_MARKER} {{"outcome": "done", "summary": "ok"}}'
        child = apply_tranche_child_result(row, 101, text)
        assert child.state == "done"
        assert child.result["outcome"] == "done"
        assert row.children[0].state == "done"

    def test_marks_child_failed_on_an_unparseable_result(self) -> None:
        row = _row()
        child = apply_tranche_child_result(row, 101, "no marker here")
        assert child.state == "failed"
        assert child.result["fail_closed_unparseable"] is True

    def test_raises_for_unknown_issue_number(self) -> None:
        row = _row()
        with pytest.raises(ValueError, match="no child issue #999"):
            apply_tranche_child_result(row, 999, "whatever")


class TestTrancheRowAllChildrenTerminal:
    def test_false_while_any_child_pending_or_dispatched(self) -> None:
        row = _row()
        row.children[0].state = "done"
        row.children[1].state = "dispatched"
        assert tranche_row_all_children_terminal(row) is False

    def test_true_once_every_child_is_done_or_failed(self) -> None:
        row = _row()
        row.children[0].state = "done"
        row.children[1].state = "failed"
        assert tranche_row_all_children_terminal(row) is True

    def test_false_for_a_row_with_no_children(self) -> None:
        row = _row(children=[])
        assert tranche_row_all_children_terminal(row) is False
