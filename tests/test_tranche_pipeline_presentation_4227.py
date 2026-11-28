"""Tests for the native tranche row's pipeline presentation (#4129 Phase 1,
this slice #4227):

- `coord.drive_queue.tranche_master_key` / `tranche_child_key` /
  `tranche_riding_members` — the one place "is this issue riding inside a
  tranche right now" is answered.
- `coord.drive_queue.tranche_row_pipeline_label` / `render_tranche_row` /
  `render_tranche_rows` — the master-row/riding-children view: one master
  row carries Work -> Test -> Review -> Merge; children render as "riding
  in tranche `<master>`" instead of owning a stage row of their own.
- `coord.drive_queue.TrancheRequeuePlan` / `tranche_rows_ready_to_requeue` /
  `tranche_requeue_plan` — unfinished children of a failed row dropping
  back to the queue as ordinary rows, with the remainder recorded.

#4199 (row shape), #4200 (parallel child dispatch), #4201 (coord-side
merge/integrator) and #4202 (Test/review/PR tail) are the sibling slices
this builds on; #4226 (tranche auto-detection) is the sibling that forms a
row in the first place.
"""

from __future__ import annotations

import pytest

from coord.drive_queue import (
    TrancheRequeuePlan,
    render_tranche_row,
    render_tranche_rows,
    tranche_child_key,
    tranche_master_key,
    tranche_requeue_plan,
    tranche_riding_members,
    tranche_row_pipeline_label,
    tranche_rows_ready_to_requeue,
)
from coord.models import TrancheChild, TrancheRow


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


class TestTrancheKeys:
    def test_master_key_is_repo_issue_not_row_id(self) -> None:
        row = _row(id=999, repo_name="api", issue_number=100)
        assert tranche_master_key(row) == "api#100"

    def test_child_key_is_independent_of_the_row(self) -> None:
        child = TrancheChild(issue_number=101, repo_name="api")
        assert tranche_child_key(child) == "api#101"


class TestTrancheRidingMembers:
    def test_every_child_of_a_live_row_rides_it(self) -> None:
        row = _row(state="dispatched")
        riding = tranche_riding_members([row])
        assert riding == {"api#101": "api#100", "api#102": "api#100"}

    def test_done_row_children_still_ride_until_the_pr_closes_them(self) -> None:
        # `row.state == "done"` only means the mechanical merge succeeded —
        # the Test/review/PR tail is still ahead of it, so its children
        # have not closed yet either.
        row = _row(state="done")
        assert tranche_riding_members([row]) == {
            "api#101": "api#100", "api#102": "api#100",
        }

    def test_failed_row_children_are_not_riding_anything(self) -> None:
        row = _row(state="failed")
        assert tranche_riding_members([row]) == {}

    def test_merges_across_several_rows(self) -> None:
        row_a = _row(id=1, issue_number=100, children=[
            TrancheChild(issue_number=101, repo_name="api"),
        ])
        row_b = _row(id=2, issue_number=200, children=[
            TrancheChild(issue_number=201, repo_name="api"),
        ])
        riding = tranche_riding_members([row_a, row_b])
        assert riding == {"api#101": "api#100", "api#201": "api#200"}


class TestTrancheRowPipelineLabel:
    @pytest.mark.parametrize("state", ["pending", "dispatched", "integrating"])
    def test_pre_integration_states_read_as_work_in_progress(self, state) -> None:
        row = _row(state=state)
        assert tranche_row_pipeline_label(row) == "[Work] -> Test -> Review -> Merge"

    def test_done_reads_as_the_test_review_merge_tail(self) -> None:
        row = _row(state="done")
        assert (
            tranche_row_pipeline_label(row) == "Work -> [Test -> Review -> Merge]"
        )

    def test_failed_names_the_only_stage_it_can_confirm(self) -> None:
        row = _row(state="failed")
        assert tranche_row_pipeline_label(row) == "[Work: FAILED]"


class TestRenderTrancheRow:
    def test_master_line_then_one_riding_line_per_child(self) -> None:
        row = _row(state="dispatched")
        lines = render_tranche_row(row)
        assert lines[0] == (
            "api#100  TRANCHE [Work] -> Test -> Review -> Merge  "
            "(Decompose the big thing)"
        )
        assert lines[1:] == [
            "  api#101  riding in tranche api#100  (Child A)",
            "  api#102  riding in tranche api#100  (Child B)",
        ]

    def test_children_are_ordered_by_issue_number_not_list_order(self) -> None:
        row = _row(children=[
            TrancheChild(issue_number=102, repo_name="api", title="Child B"),
            TrancheChild(issue_number=101, repo_name="api", title="Child A"),
        ])
        lines = render_tranche_row(row)
        assert [line for line in lines if "riding" in line] == [
            "  api#101  riding in tranche api#100  (Child A)",
            "  api#102  riding in tranche api#100  (Child B)",
        ]

    def test_a_row_with_no_children_renders_nothing(self) -> None:
        row = _row(children=[])
        assert render_tranche_row(row) == []

    def test_no_title_omits_the_parenthetical(self) -> None:
        row = _row(issue_title="", children=[
            TrancheChild(issue_number=101, repo_name="api", title=""),
        ])
        lines = render_tranche_row(row)
        assert lines[0] == "api#100  TRANCHE [Work] -> Test -> Review -> Merge"
        assert lines[1] == "  api#101  riding in tranche api#100"


class TestRenderTrancheRows:
    def test_concatenates_every_row_in_order(self) -> None:
        row_a = _row(id=1, issue_number=100, issue_title="", children=[
            TrancheChild(issue_number=101, repo_name="api"),
        ])
        row_b = _row(id=2, issue_number=200, issue_title="Second tranche", children=[
            TrancheChild(issue_number=201, repo_name="api"),
        ])
        lines = render_tranche_rows([row_a, row_b])
        assert lines == [
            "api#100  TRANCHE [Work] -> Test -> Review -> Merge",
            "  api#101  riding in tranche api#100",
            "api#200  TRANCHE [Work] -> Test -> Review -> Merge  "
            "(Second tranche)",
            "  api#201  riding in tranche api#200",
        ]

    def test_empty_rows_contribute_nothing(self) -> None:
        assert render_tranche_rows([]) == []
        assert render_tranche_rows([_row(children=[])]) == []


class TestTrancheRowsReadyToRequeue:
    def test_only_failed_rows_with_children_are_ready(self) -> None:
        failed = _row(id=1, state="failed")
        done = _row(id=2, issue_number=200, state="done")
        failed_no_children = _row(id=3, issue_number=300, state="failed", children=[])
        rows = [failed, done, failed_no_children]
        assert tranche_rows_ready_to_requeue(rows) == [failed]


class TestTrancheRequeuePlan:
    def test_requeues_every_child_sorted_by_issue_number(self) -> None:
        row = _row(state="failed", children=[
            TrancheChild(issue_number=102, repo_name="api", state="done"),
            TrancheChild(issue_number=101, repo_name="api", state="failed"),
        ])
        plan = tranche_requeue_plan(row)
        assert isinstance(plan, TrancheRequeuePlan)
        assert plan.row_id == 7
        assert plan.master_key == "api#100"
        assert [c.issue_number for c in plan.requeue] == [101, 102]

    def test_requeues_done_children_too_since_nothing_merged(self) -> None:
        # `merge_tranche_children` declines the WHOLE merge the moment any
        # one child failed — a sibling that itself reported "done" never
        # rode a closing PR either, so it comes back too.
        row = _row(state="failed", children=[
            TrancheChild(issue_number=101, repo_name="api", state="done"),
            TrancheChild(issue_number=102, repo_name="api", state="failed"),
        ])
        plan = tranche_requeue_plan(row)
        assert {c.issue_number for c in plan.requeue} == {101, 102}

    def test_remainder_note_names_the_row_and_every_child(self) -> None:
        row = _row(state="failed")
        plan = tranche_requeue_plan(row)
        assert "tranche row 7" in plan.remainder_note
        assert "api#100" in plan.remainder_note
        assert "#101" in plan.remainder_note
        assert "#102" in plan.remainder_note
        assert "dropped back to the queue" in plan.remainder_note

    def test_refuses_a_row_that_is_not_failed(self) -> None:
        row = _row(state="done")
        with pytest.raises(ValueError, match="not 'failed'"):
            tranche_requeue_plan(row)
