"""Tests for coord.milestone_order — #768 Phase 0 (work-order parser + DAG +
ready frontier).

Black-box shape per the issue's acceptance criteria: seed a tracking-issue
body (+ a Board for claim detection), assert the parsed DAG and the
computed ready frontier, and assert that a cycle / unknown-target /
non-milestone-issue each raise a clear WorkOrderError.
"""

from __future__ import annotations

import pytest

from coord.milestone_order import (
    Frontier,
    ProgressStatus,
    WorkOrder,
    WorkOrderError,
    WorkOrderNode,
    compute_progress,
    milestone_work_order_membership,
    parse_progress,
    parse_sub_issues,
    parse_work_order,
    ready_frontier,
    remove_sub_issues_section,
    render_progress,
    render_sub_issues,
    render_work_order,
    replace_progress_section,
    replace_sub_issues_section,
    replace_work_order_section,
    validate_milestone_membership,
    validate_no_shared_oracle_group,
)
from coord.models import Assignment, Board


SAMPLE_BODY = """\
Some intro prose about the milestone.

## Work order
- [ ] #762  {group: A}        # may run concurrently (cohort A)
- [ ] #763  {group: A}
- [ ] #765  {after: #762,#763}   # hard dependency edge
- [ ] #766  {after: #765}
- [ ] #767

## Refs
Not part of the work order.
"""


def _active(
    *, issue: int, repo: str = "api", branch: str | None = None
) -> Assignment:
    return Assignment(
        machine_name="laptop",
        repo_name=repo,
        issue_number=issue,
        issue_title="test",
        status="running",
        branch=branch or f"issue-{issue}-fix",
        assignment_id=f"a{issue}",
        type="work",
    )


# ── parse_work_order: happy path ────────────────────────────────────────────


class TestParseWorkOrder:
    def test_parses_groups_and_after_edges(self) -> None:
        wo = parse_work_order(SAMPLE_BODY)
        assert wo.issue_numbers == (762, 763, 765, 766, 767)
        assert wo.node(762) == WorkOrderNode(762, group="A", after=())
        assert wo.node(763) == WorkOrderNode(763, group="A", after=())
        assert wo.node(765) == WorkOrderNode(765, group=None, after=(762, 763))
        assert wo.node(766) == WorkOrderNode(766, group=None, after=(765,))
        # A bare line (no annotation) means no constraint.
        assert wo.node(767) == WorkOrderNode(767, group=None, after=())

    def test_stops_at_the_next_heading(self) -> None:
        wo = parse_work_order(SAMPLE_BODY)
        assert 768 not in wo.issue_numbers  # nothing from '## Refs' leaks in

    def test_no_work_order_heading_returns_empty(self) -> None:
        wo = parse_work_order("just some prose, no heading here")
        assert wo == WorkOrder(nodes=())

    def test_checked_item_is_tracked(self) -> None:
        wo = parse_work_order(
            "## Work order\n- [x] #1\n- [ ] #2 {after: #1}\n"
        )
        assert wo.node(1).checked is True
        assert wo.node(2).checked is False

    def test_checkbox_free_grammar_parses(self) -> None:
        """#1061: the `[ ]`/`[x]` box is now optional — a bare `- #N {...}`
        line parses identically to its checkbox counterpart, `checked`
        simply defaulting to False (it's decorative either way)."""
        wo = parse_work_order(
            "## Work order\n- #1  {group: A}\n- #2  {after: #1}\n"
        )
        assert wo.node(1) == WorkOrderNode(1, group="A", after=())
        assert wo.node(2) == WorkOrderNode(2, group=None, after=(1,))
        assert wo.node(1).checked is False
        assert wo.node(2).checked is False

    def test_checkbox_and_checkbox_free_lines_coexist(self) -> None:
        """Both grammars are accepted in the same block during the
        migration — an epic doesn't have to be all-or-nothing."""
        wo = parse_work_order(
            "## Work order\n- [x] #1\n- #2  {after: #1}\n"
        )
        assert wo.issue_numbers == (1, 2)
        assert wo.node(1).checked is True
        assert wo.node(2).checked is False
        assert wo.node(2).after == (1,)

    def test_combined_group_and_after_annotation(self) -> None:
        wo = parse_work_order(
            "## Work order\n- [ ] #1\n- [ ] #2 {group: B, after: #1}\n"
        )
        assert wo.node(2).group == "B"
        assert wo.node(2).after == (1,)


# ── parse_work_order: validation errors ─────────────────────────────────────


class TestParseWorkOrderErrors:
    def test_cycle_raises_clear_error(self) -> None:
        body = "## Work order\n- [ ] #1 {after: #2}\n- [ ] #2 {after: #1}\n"
        with pytest.raises(WorkOrderError, match=r"cycle.*#1.*#2"):
            parse_work_order(body)

    def test_unknown_after_target_raises_clear_error(self) -> None:
        body = "## Work order\n- [ ] #1 {after: #99}\n"
        with pytest.raises(WorkOrderError, match=r"#1.*after:#99.*not declared"):
            parse_work_order(body)

    def test_duplicate_issue_raises(self) -> None:
        body = "## Work order\n- [ ] #1\n- [ ] #1\n"
        with pytest.raises(WorkOrderError, match=r"#1.*more than once"):
            parse_work_order(body)

    def test_unknown_annotation_key_raises(self) -> None:
        body = "## Work order\n- [ ] #1 {bogus: x}\n"
        with pytest.raises(WorkOrderError, match="unknown annotation key"):
            parse_work_order(body)

    def test_malformed_after_entry_raises(self) -> None:
        body = "## Work order\n- [ ] #1 {after: not-a-number}\n"
        with pytest.raises(WorkOrderError, match="malformed after-entry"):
            parse_work_order(body)

    def test_unparseable_checklist_line_raises(self) -> None:
        body = "## Work order\n- this is not a work-order item\n"
        with pytest.raises(WorkOrderError, match="unparseable line"):
            parse_work_order(body)

    def test_self_loop_is_a_cycle(self) -> None:
        body = "## Work order\n- [ ] #1 {after: #1}\n"
        with pytest.raises(WorkOrderError, match="cycle"):
            parse_work_order(body)


# ── validate_milestone_membership ───────────────────────────────────────────


class TestValidateMilestoneMembership:
    def test_all_nodes_under_milestone_passes(self) -> None:
        wo = parse_work_order(SAMPLE_BODY)
        validate_milestone_membership(wo, {762, 763, 765, 766, 767})  # no raise

    def test_foreign_issue_raises_clear_error(self) -> None:
        wo = parse_work_order(SAMPLE_BODY)
        with pytest.raises(WorkOrderError, match=r"#767.*not an issue under this milestone"):
            validate_milestone_membership(wo, {762, 763, 765, 766})

    def test_closed_dependency_still_counts_as_membership(self) -> None:
        """A node that has already closed is still a valid DAG member —
        membership doesn't require currently-open state (see module
        docstring design note)."""
        body = "## Work order\n- [ ] #1\n- [ ] #2 {after: #1}\n"
        wo = parse_work_order(body)
        # #1 closed, #2 still open — both are legitimately "under the
        # milestone"; the caller supplies membership regardless of state.
        validate_milestone_membership(wo, {1, 2})


# ── validate_no_shared_oracle_group (#2542) ─────────────────────────────────


class TestValidateNoSharedOracleGroup:
    def test_shared_group_under_oracle_loop_raises(self) -> None:
        wo = parse_work_order(SAMPLE_BODY)  # #762/#763 share {group: A}
        with pytest.raises(
            WorkOrderError, match=r"#762, #763 share group 'A'.*oracle-loop"
        ):
            validate_no_shared_oracle_group(wo, oracle_loop=True)

    def test_shared_group_outside_oracle_loop_is_a_no_op(self) -> None:
        wo = parse_work_order(SAMPLE_BODY)
        validate_no_shared_oracle_group(wo, oracle_loop=False)  # no raise

    def test_distinct_groups_under_oracle_loop_pass(self) -> None:
        body = "## Work order\n- [ ] #1 {group: A}\n- [ ] #2 {group: B}\n"
        wo = parse_work_order(body)
        validate_no_shared_oracle_group(wo, oracle_loop=True)  # no raise

    def test_no_group_under_oracle_loop_passes(self) -> None:
        """This check only catches the EXPLICIT-group case — two ungrouped
        nodes with no declared edge between them are a different hazard
        (drive-queue concurrency), closed separately by plan_queue's
        oracle_loop serialization, not here."""
        body = "## Work order\n- [ ] #1\n- [ ] #2\n"
        wo = parse_work_order(body)
        validate_no_shared_oracle_group(wo, oracle_loop=True)  # no raise

    def test_single_member_group_under_oracle_loop_passes(self) -> None:
        body = "## Work order\n- [ ] #1 {group: A}\n- [ ] #2 {after: #1}\n"
        wo = parse_work_order(body)
        validate_no_shared_oracle_group(wo, oracle_loop=True)  # no raise


# ── ready_frontier ───────────────────────────────────────────────────────────


class TestReadyFrontier:
    def test_frontier_with_empty_board_and_no_terminal_issues(self) -> None:
        wo = parse_work_order(SAMPLE_BODY)
        board = Board()
        frontier = ready_frontier(
            wo,
            board,
            repo_name="api",
            repo_github="acme/api",
            terminal_issues=set(),
            branch_lookup=lambda repo, n: [],
        )
        # Only nodes with a fully-satisfied (empty) after-set are ready.
        ready_numbers = {e.issue_number for e in frontier.ready}
        assert ready_numbers == {762, 763, 767}
        blocked_numbers = {b.issue_number for b in frontier.blocked}
        assert blocked_numbers == {765, 766}
        blocked_by = {b.issue_number: b.waiting_on_deps for b in frontier.blocked}
        assert blocked_by[765] == (762, 763)
        assert blocked_by[766] == (765,)

    def test_frontier_advances_as_deps_go_terminal(self) -> None:
        wo = parse_work_order(SAMPLE_BODY)
        board = Board()
        frontier = ready_frontier(
            wo,
            board,
            repo_name="api",
            repo_github="acme/api",
            terminal_issues={762, 763},
            branch_lookup=lambda repo, n: [],
        )
        ready_numbers = {e.issue_number for e in frontier.ready}
        # 762/763 are terminal (dropped from the frontier entirely), 765's
        # after-set is now fully satisfied, 766 still waits on 765, 767 is
        # unconstrained and stays ready.
        assert ready_numbers == {765, 767}
        blocked_numbers = {b.issue_number for b in frontier.blocked}
        assert blocked_numbers == {766}

    def test_claimed_node_is_blocked_not_ready(self) -> None:
        wo = parse_work_order(SAMPLE_BODY)
        board = Board()
        board.active.append(_active(issue=762))
        frontier = ready_frontier(
            wo,
            board,
            repo_name="api",
            repo_github="acme/api",
            terminal_issues=set(),
            branch_lookup=lambda repo, n: [],
        )
        ready_numbers = {e.issue_number for e in frontier.ready}
        assert 762 not in ready_numbers
        blocked = {b.issue_number: b for b in frontier.blocked}
        assert blocked[762].claim is not None
        assert blocked[762].claim.source == "board"
        assert "claimed" in blocked[762].reason

    def test_remote_branch_claim_blocks_via_branch_lookup(self) -> None:
        wo = parse_work_order(SAMPLE_BODY)
        board = Board()
        frontier = ready_frontier(
            wo,
            board,
            repo_name="api",
            repo_github="acme/api",
            terminal_issues=set(),
            branch_lookup=lambda repo, n: (
                ["issue-763-already-started"] if n == 763 else []
            ),
        )
        ready_numbers = {e.issue_number for e in frontier.ready}
        assert 763 not in ready_numbers
        blocked = {b.issue_number: b for b in frontier.blocked}
        assert blocked[763].claim.source == "remote_branch"

    def test_conflict_checker_blocks_a_node(self) -> None:
        wo = parse_work_order(SAMPLE_BODY)
        board = Board()
        frontier = ready_frontier(
            wo,
            board,
            repo_name="api",
            repo_github="acme/api",
            terminal_issues=set(),
            branch_lookup=lambda repo, n: [],
            conflict_checker=lambda n: n == 767,
        )
        ready_numbers = {e.issue_number for e in frontier.ready}
        assert 767 not in ready_numbers
        assert ready_numbers == {762, 763}
        blocked = {b.issue_number: b for b in frontier.blocked}
        assert blocked[767].conflict is True
        assert blocked[767].reason == "conflict-blocked"

    def test_fully_terminal_work_order_yields_empty_frontier(self) -> None:
        wo = parse_work_order(SAMPLE_BODY)
        board = Board()
        frontier = ready_frontier(
            wo,
            board,
            repo_name="api",
            repo_github="acme/api",
            terminal_issues={762, 763, 765, 766, 767},
            branch_lookup=lambda repo, n: [],
        )
        assert frontier == Frontier(ready=(), blocked=())

    def test_identical_before_and_after_the_checkbox_migration(self) -> None:
        """#1061 acceptance criterion: `ready_frontier` already ignores
        `checked` (it keys entirely off live `terminal_issues`), so
        migrating a body from the checkbox grammar to the checkbox-free one
        must produce a byte-identical frontier — proven here by parsing the
        same DAG in both grammars and asserting equal `Frontier`s across a
        matrix of terminal-state snapshots, not just the empty-board case."""
        old_grammar_body = SAMPLE_BODY  # `- [ ] #N {...}` throughout
        new_grammar_body = (
            "## Work order\n"
            "- #762  {group: A}\n"
            "- #763  {group: A}\n"
            "- #765  {after: #762,#763}\n"
            "- #766  {after: #765}\n"
            "- #767\n"
        )
        wo_old = parse_work_order(old_grammar_body)
        wo_new = parse_work_order(new_grammar_body)
        assert wo_old.issue_numbers == wo_new.issue_numbers

        board = Board()
        for terminal_issues in (set(), {762, 763}, {762, 763, 765, 766, 767}):
            frontier_old = ready_frontier(
                wo_old, board, repo_name="api", repo_github="acme/api",
                terminal_issues=terminal_issues, branch_lookup=lambda repo, n: [],
            )
            frontier_new = ready_frontier(
                wo_new, board, repo_name="api", repo_github="acme/api",
                terminal_issues=terminal_issues, branch_lookup=lambda repo, n: [],
            )
            assert frontier_old == frontier_new


# ── render_work_order / replace_work_order_section (#770 Phase 2 write path) ─


class TestRenderWorkOrder:
    def test_round_trips_through_parse(self) -> None:
        wo = parse_work_order(SAMPLE_BODY)
        rendered = render_work_order(wo)
        reparsed = parse_work_order("## Work order\n" + rendered)
        assert reparsed == wo

    def test_renders_group_and_after_annotations(self) -> None:
        wo = WorkOrder(nodes=(
            WorkOrderNode(1, group="A"),
            WorkOrderNode(2, after=(1,)),
            WorkOrderNode(3),
        ))
        rendered = render_work_order(wo)
        assert rendered == (
            "- [ ] #1  {group: A}\n"
            "- [ ] #2  {after: #1}\n"
            "- [ ] #3"
        )

    def test_checkbox_false_renders_checkbox_free_grammar(self) -> None:
        """#1061: the write path `coord milestone sync` uses."""
        wo = WorkOrder(nodes=(
            WorkOrderNode(1, group="A"),
            WorkOrderNode(2, after=(1,)),
            WorkOrderNode(3),
        ))
        rendered = render_work_order(wo, checkbox=False)
        assert rendered == (
            "- #1  {group: A}\n"
            "- #2  {after: #1}\n"
            "- #3"
        )

    def test_checkbox_false_ignores_the_checked_flag(self) -> None:
        """Dropping the box means a `checked=True` node still renders
        without one — there's nowhere left to put it."""
        wo = WorkOrder(nodes=(WorkOrderNode(1, checked=True),))
        assert render_work_order(wo, checkbox=False) == "- #1"

    def test_checkbox_false_round_trips_through_parse(self) -> None:
        wo = parse_work_order(SAMPLE_BODY)
        rendered = render_work_order(wo, checkbox=False)
        reparsed = parse_work_order("## Work order\n" + rendered)
        # checked is always False on the reparsed side (no box to read) —
        # compare node-by-node on the fields the checkbox-free grammar can
        # actually carry, matching the acceptance criterion that dropping
        # the box doesn't change anything readiness cares about.
        assert reparsed.issue_numbers == wo.issue_numbers
        for n in wo.nodes:
            reparsed_node = reparsed.node(n.issue_number)
            assert reparsed_node.group == n.group
            assert reparsed_node.after == n.after
            assert reparsed_node.checked is False

    def test_renders_checked_box(self) -> None:
        wo = WorkOrder(nodes=(WorkOrderNode(1, checked=True),))
        assert render_work_order(wo) == "- [x] #1"

    def test_empty_work_order_renders_empty_string(self) -> None:
        assert render_work_order(WorkOrder()) == ""


class TestReplaceWorkOrderSection:
    def test_replaces_existing_section_in_place(self) -> None:
        body = (
            "Intro.\n\n"
            "## Work order\n"
            "- [ ] #1\n\n"
            "## Refs\n"
            "other stuff\n"
        )
        new_body = replace_work_order_section(body, "- [ ] #1  {group: A}\n- [ ] #2  {after: #1}")
        assert "## Refs\nother stuff" in new_body
        assert "Intro." in new_body
        wo = parse_work_order(new_body)
        assert wo.issue_numbers == (1, 2)
        assert wo.node(2).after == (1,)
        # Old single-line block is gone, not duplicated alongside the new one.
        assert new_body.count("## Work order") == 1

    def test_appends_section_when_absent(self) -> None:
        body = "Just prose, no work order yet.\n"
        new_body = replace_work_order_section(body, "- [ ] #1")
        assert "Just prose, no work order yet." in new_body
        wo = parse_work_order(new_body)
        assert wo.issue_numbers == (1,)

    def test_appends_section_to_empty_body(self) -> None:
        new_body = replace_work_order_section("", "- [ ] #1")
        wo = parse_work_order(new_body)
        assert wo.issue_numbers == (1,)

    def test_is_idempotent(self) -> None:
        body = "## Work order\n- [ ] #1  {group: A}\n"
        once = replace_work_order_section(body, "- [ ] #1  {group: A}")
        twice = replace_work_order_section(once, "- [ ] #1  {group: A}")
        assert once == twice

    def test_round_trip_with_render_work_order(self) -> None:
        """render → replace → parse recovers the same WorkOrder (the shape
        `coord milestone write-order` actually exercises)."""
        wo = parse_work_order(SAMPLE_BODY)
        tracking_body = "Milestone plan.\n\n## Work order\n(stale)\n"
        new_body = replace_work_order_section(tracking_body, render_work_order(wo))
        assert parse_work_order(new_body) == wo
        assert "Milestone plan." in new_body

    def test_preserves_content_after_next_heading_of_any_level(self) -> None:
        body = "## Work order\n- [ ] #1\n\n### Sub-heading\nkept\n"
        new_body = replace_work_order_section(body, "- [ ] #1\n- [ ] #2")
        assert "### Sub-heading\nkept" in new_body
        assert parse_work_order(new_body).issue_numbers == (1, 2)


# ── parse_sub_issues / render_sub_issues / replace_sub_issues_section (#1008) ─
# Mirrors the Work-order test classes above almost line for line — same
# grammar, same validation, different heading — plus a coexistence check
# proving the two sections don't step on each other in one tracking body.


SUB_ISSUES_BODY = """\
Epic intro prose.

## Sub-issues
- [ ] #1050  {group: A}
- [ ] #1051  {after: #1050}
- [x] #1052

## Refs
Not part of the sub-issues checklist.
"""


class TestParseSubIssues:
    def test_parses_nodes_and_annotations(self) -> None:
        wo = parse_sub_issues(SUB_ISSUES_BODY)
        assert wo.issue_numbers == (1050, 1051, 1052)
        assert wo.node(1050).group == "A"
        assert wo.node(1051).after == (1050,)
        assert wo.node(1052).checked is True

    def test_no_heading_returns_empty(self) -> None:
        assert parse_sub_issues("just prose, no sub-issues here").nodes == ()

    def test_does_not_pick_up_a_work_order_block(self) -> None:
        """A body with only `## Work order` (no `## Sub-issues`) parses empty
        for parse_sub_issues — the two sections are independent."""
        assert parse_sub_issues(SAMPLE_BODY).nodes == ()


class TestParseSubIssuesErrors:
    def test_cycle_raises(self) -> None:
        body = "## Sub-issues\n- [ ] #1 {after: #2}\n- [ ] #2 {after: #1}\n"
        with pytest.raises(WorkOrderError, match=r"cycle.*#1.*#2"):
            parse_sub_issues(body)

    def test_undeclared_after_target_raises(self) -> None:
        body = "## Sub-issues\n- [ ] #1 {after: #99}\n"
        with pytest.raises(WorkOrderError, match=r"sub-issues.*#1.*after:#99.*not declared"):
            parse_sub_issues(body)

    def test_duplicate_issue_raises(self) -> None:
        body = "## Sub-issues\n- [ ] #1\n- [ ] #1\n"
        with pytest.raises(WorkOrderError, match=r"sub-issues.*#1.*more than once"):
            parse_sub_issues(body)

    def test_unknown_annotation_key_raises(self) -> None:
        body = "## Sub-issues\n- [ ] #1 {bogus: x}\n"
        with pytest.raises(WorkOrderError, match="unknown annotation key"):
            parse_sub_issues(body)

    def test_unparseable_line_raises(self) -> None:
        body = "## Sub-issues\n- this is not a sub-issue item\n"
        with pytest.raises(WorkOrderError, match="unparseable line"):
            parse_sub_issues(body)


class TestRenderSubIssues:
    def test_is_render_work_order(self) -> None:
        """render_sub_issues is an alias — heading-agnostic rendering means
        there's only one checklist-rendering implementation to maintain."""
        assert render_sub_issues is render_work_order

    def test_round_trips_through_parse(self) -> None:
        wo = parse_sub_issues(SUB_ISSUES_BODY)
        rendered = render_sub_issues(wo)
        reparsed = parse_sub_issues("## Sub-issues\n" + rendered)
        assert reparsed == wo


class TestReplaceSubIssuesSection:
    def test_replaces_existing_section_in_place(self) -> None:
        body = (
            "Intro.\n\n"
            "## Sub-issues\n"
            "- [ ] #1050\n\n"
            "## Refs\n"
            "other stuff\n"
        )
        new_body = replace_sub_issues_section(
            body, "- [ ] #1050  {group: A}\n- [ ] #1051  {after: #1050}"
        )
        assert "## Refs\nother stuff" in new_body
        assert "Intro." in new_body
        wo = parse_sub_issues(new_body)
        assert wo.issue_numbers == (1050, 1051)
        assert new_body.count("## Sub-issues") == 1

    def test_appends_section_when_absent(self) -> None:
        body = "Just prose, no sub-issues yet.\n"
        new_body = replace_sub_issues_section(body, "- [ ] #1050")
        assert "Just prose, no sub-issues yet." in new_body
        assert parse_sub_issues(new_body).issue_numbers == (1050,)

    def test_is_idempotent(self) -> None:
        body = "## Sub-issues\n- [ ] #1050  {group: A}\n"
        once = replace_sub_issues_section(body, "- [ ] #1050  {group: A}")
        twice = replace_sub_issues_section(once, "- [ ] #1050  {group: A}")
        assert once == twice

    def test_does_not_disturb_a_coexisting_work_order_section(self) -> None:
        """The whole point of keying on separate headings (#1008): splicing
        `## Sub-issues` must leave an existing `## Work order` block (and
        vice versa) completely untouched."""
        body = (
            "## Work order\n"
            "- [ ] #762  {group: A}\n\n"
            "## Sub-issues\n"
            "- [ ] #1050\n"
        )
        new_body = replace_sub_issues_section(body, "- [ ] #1050\n- [ ] #1051")
        assert parse_work_order(new_body).issue_numbers == (762,)
        assert parse_sub_issues(new_body).issue_numbers == (1050, 1051)

        # And the reverse: replacing `## Work order` must leave `##
        # Sub-issues` untouched too.
        newer_body = replace_work_order_section(new_body, "- [ ] #762\n- [ ] #763")
        assert parse_work_order(newer_body).issue_numbers == (762, 763)
        assert parse_sub_issues(newer_body).issue_numbers == (1050, 1051)


# ── remove_sub_issues_section (#1061: retiring `## Sub-issues`) ────────────


class TestRemoveSubIssuesSection:
    def test_removes_heading_and_content(self) -> None:
        body = (
            "Intro.\n\n"
            "## Sub-issues\n"
            "- [ ] #1050\n"
            "- [x] #1051\n\n"
            "## Refs\n"
            "other stuff\n"
        )
        new_body = remove_sub_issues_section(body)
        assert "## Sub-issues" not in new_body
        assert "#1050" not in new_body
        assert "Intro." in new_body
        assert "## Refs\nother stuff" in new_body

    def test_no_heading_is_a_noop(self) -> None:
        body = "## Work order\n- [ ] #1\n"
        assert remove_sub_issues_section(body) == body

    def test_leaves_a_coexisting_work_order_section_untouched(self) -> None:
        body = (
            "## Work order\n"
            "- [ ] #762  {group: A}\n\n"
            "## Sub-issues\n"
            "- [ ] #1050\n"
        )
        new_body = remove_sub_issues_section(body)
        assert parse_work_order(new_body).issue_numbers == (762,)
        assert parse_sub_issues(new_body).nodes == ()
        assert "## Sub-issues" not in new_body

    def test_is_idempotent(self) -> None:
        body = "## Sub-issues\n- [ ] #1050\n"
        once = remove_sub_issues_section(body)
        twice = remove_sub_issues_section(once)
        assert once == twice

    def test_sole_section_removed_leaves_clean_body(self) -> None:
        body = "Epic intro.\n\n## Sub-issues\n- [ ] #1050\n"
        new_body = remove_sub_issues_section(body)
        assert new_body == "Epic intro.\n"


# ── compute_progress / render_progress / parse_progress / ─────────────────
# replace_progress_section (#1412 deliverable 2) ────────────────────────────
#
# `## Progress` is derived, never hand-authored, so the acceptance bar is
# different from the checklist sections above: it must (1) agree with
# exactly what `coord milestone order` would print right now (no second
# readiness notion), (2) round-trip through parse_progress for the
# idempotent-no-op check, and (3) never disturb a coexisting `## Work
# order` / `## Sub-issues` section.


def _board() -> Board:
    return Board()


class TestComputeProgress:
    def test_terminal_node_is_done(self) -> None:
        wo = parse_work_order("## Work order\n- #1\n")
        frontier = ready_frontier(
            wo, _board(), repo_name="api", repo_github="acme/api",
            terminal_issues={1},
        )
        statuses = compute_progress(wo, frontier, {1})
        assert statuses == (ProgressStatus(1, "done", None, None),)

    def test_unblocked_node_is_ready(self) -> None:
        wo = parse_work_order("## Work order\n- #1  {group: A}\n")
        frontier = ready_frontier(
            wo, _board(), repo_name="api", repo_github="acme/api",
            terminal_issues=set(),
        )
        statuses = compute_progress(wo, frontier, set())
        assert statuses == (ProgressStatus(1, "ready", "A", None),)

    def test_dependency_wait_is_blocked_with_reason(self) -> None:
        wo = parse_work_order("## Work order\n- #1\n- #2  {after: #1}\n")
        frontier = ready_frontier(
            wo, _board(), repo_name="api", repo_github="acme/api",
            terminal_issues=set(),
        )
        statuses = compute_progress(wo, frontier, set())
        assert statuses[0] == ProgressStatus(1, "ready", None, None)
        assert statuses[1] == ProgressStatus(2, "blocked", None, "waiting on #1")

    def test_claimed_node_is_blocked_with_claim_reason(self) -> None:
        wo = parse_work_order("## Work order\n- #1\n")
        board = Board(active=[_active(issue=1)])
        frontier = ready_frontier(
            wo, board, repo_name="api", repo_github="acme/api",
            terminal_issues=set(),
        )
        statuses = compute_progress(wo, frontier, set())
        assert statuses[0].status == "blocked"
        assert "claimed" in statuses[0].detail

    def test_preserves_declared_order_including_terminal_nodes(self) -> None:
        wo = parse_work_order(SAMPLE_BODY)
        frontier = ready_frontier(
            wo, _board(), repo_name="api", repo_github="acme/api",
            terminal_issues={762, 763},
        )
        statuses = compute_progress(wo, frontier, {762, 763})
        assert [s.issue_number for s in statuses] == [762, 763, 765, 766, 767]
        assert statuses[0].status == "done" and statuses[1].status == "done"


class TestRenderProgress:
    def test_round_trips_through_parse_progress(self) -> None:
        wo = parse_work_order(SAMPLE_BODY)
        frontier = ready_frontier(
            wo, _board(), repo_name="api", repo_github="acme/api",
            terminal_issues={762, 763},
        )
        statuses = compute_progress(wo, frontier, {762, 763})
        rendered = render_progress(statuses, generated_at="2026-07-28T00:00:00Z")
        reparsed = parse_progress("## Progress\n" + rendered)
        assert reparsed == statuses

    def test_includes_the_generated_at_timestamp(self) -> None:
        statuses = (ProgressStatus(1, "done"),)
        rendered = render_progress(statuses, generated_at="2026-07-28T00:00:00Z")
        assert "2026-07-28T00:00:00Z" in rendered

    def test_includes_a_summary_line(self) -> None:
        statuses = (
            ProgressStatus(1, "done"),
            ProgressStatus(2, "ready"),
            ProgressStatus(3, "blocked", detail="waiting on #2"),
        )
        rendered = render_progress(statuses, generated_at="X")
        assert "1/3 done" in rendered
        assert "1 ready" in rendered
        assert "1 blocked" in rendered

    def test_empty_statuses_renders_no_summary(self) -> None:
        rendered = render_progress((), generated_at="X")
        assert "done" not in rendered.split("\n\n")[-1]

    def test_renders_group_annotation(self) -> None:
        rendered = render_progress(
            (ProgressStatus(1, "ready", group="A"),), generated_at="X"
        )
        assert "- [ready] #1 (group A)" in rendered


class TestParseProgress:
    def test_no_heading_returns_empty(self) -> None:
        assert parse_progress("just prose, no progress section") == ()

    def test_ignores_generated_by_note_and_summary_line(self) -> None:
        body = (
            "## Progress\n"
            "_Generated by `coord milestone sync-progress` ... ._\n\n"
            "- [done] #1\n\n"
            "**1/1 done** · 0 ready · 0 blocked\n"
        )
        assert parse_progress(body) == (ProgressStatus(1, "done"),)

    def test_stops_at_next_heading(self) -> None:
        body = "## Progress\n- [done] #1\n\n## Refs\n- [ ] #999\n"
        statuses = parse_progress(body)
        assert statuses == (ProgressStatus(1, "done"),)

    def test_does_not_pick_up_a_work_order_block(self) -> None:
        assert parse_progress(SAMPLE_BODY) == ()


class TestReplaceProgressSection:
    def test_appends_when_absent(self) -> None:
        body = "Epic intro.\n\n## Work order\n- #1\n"
        new_body = replace_progress_section(body, "- [done] #1")
        assert "## Progress\n- [done] #1" in new_body
        # `## Work order` is untouched, byte for byte.
        assert "## Work order\n- #1\n" in new_body

    def test_is_idempotent(self) -> None:
        body = "## Progress\n- [done] #1\n"
        once = replace_progress_section(body, "- [done] #1")
        twice = replace_progress_section(once, "- [done] #1")
        assert once == twice

    def test_replaces_existing_section_in_place(self) -> None:
        body = "## Progress\n- [ready] #1\n\n## Refs\nkept\n"
        new_body = replace_progress_section(body, "- [done] #1")
        assert "## Refs\nkept" in new_body
        assert parse_progress(new_body) == (ProgressStatus(1, "done"),)
        assert new_body.count("## Progress") == 1

    def test_never_disturbs_a_coexisting_work_order_or_sub_issues_section(
        self,
    ) -> None:
        """The #1412 acceptance bar: `## Work order` (and, transitionally,
        `## Sub-issues`) must be byte-identical before and after a progress
        sync, no matter where `## Progress` ends up being spliced."""
        body = (
            "Epic intro.\n\n"
            "## Work order\n"
            "- #762  {group: A}\n"
            "- #765  {after: #762}\n\n"
            "## Sub-issues\n"
            "- [ ] #762\n"
            "- [ ] #765\n\n"
            "## Refs\n"
            "some refs\n"
        )
        wo = parse_work_order(body)
        frontier = ready_frontier(
            wo, _board(), repo_name="api", repo_github="acme/api",
            terminal_issues={762},
        )
        statuses = compute_progress(wo, frontier, {762})
        rendered = render_progress(statuses, generated_at="X")
        new_body = replace_progress_section(body, rendered)

        work_order_block = new_body.split("## Work order\n")[1].split("\n\n")[0]
        assert work_order_block == "- #762  {group: A}\n- #765  {after: #762}"
        assert parse_work_order(new_body) == wo
        assert parse_sub_issues(new_body) == parse_sub_issues(body)
        assert "## Refs\nsome refs" in new_body
        assert "## Progress" in new_body

        # Re-running with the same statuses is a no-op — no duplicate
        # `## Progress` section, no further churn.
        again = replace_progress_section(new_body, rendered)
        assert again == new_body


# ── milestone_work_order_membership (#2040) ──────────────────────────────────


def _issue(number: int, *, labels=(), body="", repo_name="api", **extra) -> dict:
    return {
        "repo_name": repo_name,
        "number": number,
        "labels": list(labels),
        "body": body,
        **extra,
    }


class TestMilestoneWorkOrderMembership:
    def test_resolves_tracking_issue_and_member_issue_numbers(self) -> None:
        issues = [
            _issue(
                1120,
                labels=["epic"],
                body="## Work order\n- #1392 {group: A}\n- #1393 {after: #1392}\n",
                milestone_title="ms-38",
            ),
            _issue(1392, labels=[]),
        ]
        result = milestone_work_order_membership(issues)
        assert result == [
            {
                "repo_name": "api",
                "tracking_issue": 1120,
                "milestone_title": "ms-38",
                "nodes": [{"issue_number": 1392}, {"issue_number": 1393}],
            }
        ]

    def test_skips_a_non_epic_issue_even_with_a_work_order_shaped_body(self) -> None:
        issues = [_issue(1120, labels=[], body="## Work order\n- #1392\n")]
        assert milestone_work_order_membership(issues) == []

    def test_skips_an_epic_with_no_work_order_block(self) -> None:
        issues = [_issue(1120, labels=["epic"], body="just some text")]
        assert milestone_work_order_membership(issues) == []

    def test_skips_an_epic_with_an_unparseable_work_order_rather_than_raising(
        self,
    ) -> None:
        """Fail-open, mirroring `coord.serve_app`'s board() handler: one bad
        epic body must never blank the whole projection."""
        issues = [
            _issue(
                1120,
                labels=["epic"],
                body="## Work order\n- #1392 {after: #9999}\n",  # undeclared target
            )
        ]
        assert milestone_work_order_membership(issues) == []

    def test_defaults_milestone_title_when_absent(self) -> None:
        issues = [_issue(1120, labels=["epic"], body="## Work order\n- #1392\n")]
        result = milestone_work_order_membership(issues)
        assert result[0]["milestone_title"] == ""

    def test_multiple_repos_and_epics_each_produce_their_own_entry(self) -> None:
        issues = [
            _issue(
                1120, labels=["epic"], body="## Work order\n- #1392\n", repo_name="api",
            ),
            _issue(
                55, labels=["epic"], body="## Work order\n- #7\n", repo_name="web",
            ),
        ]
        result = milestone_work_order_membership(issues)
        assert {(r["repo_name"], r["tracking_issue"]) for r in result} == {
            ("api", 1120), ("web", 55),
        }
