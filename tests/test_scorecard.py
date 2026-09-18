"""Tests for coord.scorecard — the dogfood scorecard aggregator (#1559).

Pure-data tests: no daemon, no GitHub, no DB. Fixture issues (GitHub
issue-list JSON shape) + fixture board assignment rows (daemon ``/board``
wire shape) go in; :class:`~coord.scorecard.MilestoneScorecard` comes out.
"""

from __future__ import annotations

from coord.scorecard import (
    ESCAPED_DEFECT_STAGES,
    INTERVENTION_KINDS,
    build_milestone_scorecard,
    classify_intervention,
    is_root_work,
    scorecard_to_dict,
)


def _issue(number, *, title="an issue", state="CLOSED", labels=None):
    return {
        "number": number,
        "title": title,
        "state": state,
        "labels": [{"name": lbl} for lbl in (labels or [])],
    }


def _root_row(issue_number, *, assignment_id, status="merged", repo="api", dispatched_at=1000.0, finished_at=1100.0, cost_usd=1.0):
    return {
        "assignment_id": assignment_id,
        "repo_name": repo,
        "issue_number": issue_number,
        "issue_title": f"issue {issue_number}",
        "type": "work",
        "status": status,
        "review_of_assignment_id": None,
        "review_iteration": 0,
        "provider_name": None,
        "dispatched_at": dispatched_at,
        "finished_at": finished_at,
        "cost_usd": cost_usd,
        "model": "sonnet",
        "input_tokens": 100,
        "output_tokens": 50,
    }


def _human_fix_row(issue_number, *, root_id, assignment_id, iteration=1, status="merged", repo="api", kind_tag="fix"):
    tag = "rework" if kind_tag == "rescue" else "fix"
    return {
        "assignment_id": assignment_id,
        "repo_name": repo,
        "issue_number": issue_number,
        "issue_title": f"[{tag}-{iteration}] issue {issue_number}",
        "type": "work",
        "status": status,
        "review_of_assignment_id": root_id,
        "review_iteration": iteration,
        "provider_name": "claude-pty",
        "dispatched_at": 1200.0,
        "finished_at": 1300.0,
        "cost_usd": 0.5,
        "model": "sonnet",
        "input_tokens": 10,
        "output_tokens": 5,
    }


def _auto_fix_row(issue_number, *, root_id, assignment_id, iteration=1, status="merged", repo="api"):
    """auto_loop's headless bounce-fix — SAME title-tag/review_of_assignment_id
    shape as a human fix, but no provider_name="claude-pty" (#1559 gotcha)."""
    return {
        "assignment_id": assignment_id,
        "repo_name": repo,
        "issue_number": issue_number,
        "issue_title": f"[fix-{iteration}] issue {issue_number}",
        "type": "work",
        "status": status,
        "review_of_assignment_id": root_id,
        "review_iteration": iteration,
        "provider_name": None,
        "dispatched_at": 1200.0,
        "finished_at": 1300.0,
        "cost_usd": 0.5,
        "model": "sonnet",
        "input_tokens": 10,
        "output_tokens": 5,
    }


def _nudge_row(issue_number, *, assignment_id, rtype="chat", repo="api"):
    return {
        "assignment_id": assignment_id,
        "repo_name": repo,
        "issue_number": issue_number,
        "issue_title": f"chat about {issue_number}",
        "type": rtype,
        "status": "done",
        "review_of_assignment_id": None,
        "review_iteration": 0,
        "provider_name": "claude-pty",
        "dispatched_at": 1150.0,
        "finished_at": 1160.0,
        "cost_usd": 0.1,
        "model": "sonnet",
        "input_tokens": 5,
        "output_tokens": 5,
    }


# ── classify_intervention / is_root_work ────────────────────────────────────


def test_is_root_work_true_for_fresh_work_dispatch():
    assert is_root_work(_root_row(1, assignment_id="r1"))


def test_is_root_work_false_when_review_of_assignment_id_set():
    row = _human_fix_row(1, root_id="r1", assignment_id="f1")
    assert not is_root_work(row)


def test_is_root_work_false_for_review_type():
    row = {"type": "review", "review_of_assignment_id": None, "review_iteration": 0}
    assert not is_root_work(row)


def test_classify_human_fix():
    row = _human_fix_row(1, root_id="r1", assignment_id="f1")
    assert classify_intervention(row) == "fix"


def test_classify_human_rescue_by_rework_title():
    row = _human_fix_row(1, root_id="r1", assignment_id="f1", kind_tag="rescue")
    assert classify_intervention(row) == "rescue"


def test_classify_human_fix_reads_review_iteration_not_the_title_text():
    """#3323: `classify_intervention` never parses a round number out of the
    title string (only `review_iteration`'s type == "work" and title tags
    like "[rework-" for the rescue/fix split matter) — so it classifies
    correctly whether the title is round 1's `[fix-1] …` or a later,
    #3323-normalized round's `[fix-3] …`, with no `[fix-1] [fix-2]`
    residue either way."""
    row = _human_fix_row(1, root_id="r1", assignment_id="f3", iteration=3)
    assert row["issue_title"] == "[fix-3] issue 1"
    assert classify_intervention(row) == "fix"


def test_classify_auto_loop_fix_is_not_an_intervention():
    """The #1559 gotcha this module exists to get right: auto_loop's headless
    bounce-fix shares review_of_assignment_id/review_iteration/title-tag shape
    with a human fix. provider_name is what tells them apart."""
    row = _auto_fix_row(1, root_id="r1", assignment_id="af1")
    assert classify_intervention(row) is None


def test_classify_conflict_fix_automated_is_not_an_intervention():
    row = {
        "type": "conflict-fix",
        "review_of_assignment_id": "r1",
        "review_iteration": 0,
        "provider_name": None,
        "issue_title": "issue 1",
    }
    assert classify_intervention(row) is None


def test_classify_nudge_types():
    for rtype in ("chat", "troubleshoot", "test-chat", "refinement"):
        row = _nudge_row(1, assignment_id="n1", rtype=rtype)
        assert classify_intervention(row) == "nudge", rtype


def test_classify_root_work_is_not_an_intervention():
    assert classify_intervention(_root_row(1, assignment_id="r1")) is None


# ── build_milestone_scorecard: first-pass acceptance ────────────────────────


def test_first_pass_yes_clean_merge_no_followups():
    issues = [_issue(1)]
    rows = [_root_row(1, assignment_id="r1", status="merged")]
    card = build_milestone_scorecard(
        milestone=49, milestone_title="M49", repo_name="api",
        issues=issues, assignment_rows=rows,
    )
    (c,) = card.issues
    assert c.first_pass == "yes"
    assert c.interventions == {"fix": 0, "rescue": 0, "nudge": 0, "abandon": 0}
    assert card.totals["first_pass"]["yes"] == 1
    assert card.totals["first_pass"]["rate"] == 1.0


def test_first_pass_no_when_human_fix_dispatched():
    issues = [_issue(1)]
    rows = [
        _root_row(1, assignment_id="r1", status="merged"),
        _human_fix_row(1, root_id="r1", assignment_id="f1", status="merged"),
    ]
    card = build_milestone_scorecard(
        milestone=49, milestone_title="M49", repo_name="api",
        issues=issues, assignment_rows=rows,
    )
    (c,) = card.issues
    assert c.first_pass == "no"
    assert c.interventions["fix"] == 1
    assert card.totals["first_pass"]["no"] == 1
    assert card.totals["interventions"]["by_kind"]["fix"] == 1


def test_first_pass_yes_despite_automated_bounce_fix():
    """An auto_loop review->fix->re-review cycle that never needed a human
    still counts as a clean first-pass acceptance — that's the whole point
    of the auto-loop existing."""
    issues = [_issue(1)]
    rows = [
        _root_row(1, assignment_id="r1", status="merged"),
        _auto_fix_row(1, root_id="r1", assignment_id="af1", status="merged"),
    ]
    card = build_milestone_scorecard(
        milestone=49, milestone_title="M49", repo_name="api",
        issues=issues, assignment_rows=rows,
    )
    (c,) = card.issues
    assert c.first_pass == "yes"
    assert c.interventions == {"fix": 0, "rescue": 0, "nudge": 0, "abandon": 0}


def test_first_pass_unknown_when_no_board_data():
    issues = [_issue(1)]
    card = build_milestone_scorecard(
        milestone=49, milestone_title="M49", repo_name="api",
        issues=issues, assignment_rows=[],
    )
    (c,) = card.issues
    assert c.first_pass == "unknown"
    assert c.has_assignment_data is False
    assert card.totals["first_pass"]["unknown"] == 1
    # unknown never counts toward the rate denominator
    assert card.totals["first_pass"]["rate"] is None


def test_first_pass_unknown_is_never_confused_with_zero_interventions():
    """Distinguishing unknown from zero (#1559 acceptance criterion) at the
    per-issue level: an issue with real zero interventions and an issue with
    no data at all must not render the same."""
    issues = [_issue(1), _issue(2)]
    rows = [_root_row(1, assignment_id="r1", status="merged")]
    card = build_milestone_scorecard(
        milestone=49, milestone_title="M49", repo_name="api",
        issues=issues, assignment_rows=rows,
    )
    known, unknown = card.issues
    assert known.first_pass == "yes"
    assert known.has_assignment_data is True
    assert unknown.first_pass == "unknown"
    assert unknown.has_assignment_data is False


def test_abandoned_issue_closed_with_board_history_but_never_merged():
    issues = [_issue(1, state="CLOSED")]
    rows = [_root_row(1, assignment_id="r1", status="failed")]
    card = build_milestone_scorecard(
        milestone=49, milestone_title="M49", repo_name="api",
        issues=issues, assignment_rows=rows,
    )
    (c,) = card.issues
    assert c.first_pass == "no"
    assert c.interventions["abandon"] == 1
    assert card.totals["interventions"]["by_kind"]["abandon"] == 1


def test_audit_merged_event_is_a_durability_cross_check():
    """A board row that never got its status flipped to 'merged' (a
    reconcile-sweep gap) still counts as merged if the audit trail recorded
    it — board isn't the only source of truth for 'did this land'."""
    issues = [_issue(1, state="CLOSED")]
    rows = [_root_row(1, assignment_id="r1", status="done")]  # never flipped
    audit_entries = [
        {"event_type": "merged", "repo": "api", "issue": 1, "ts": 1500.0},
    ]
    card = build_milestone_scorecard(
        milestone=49, milestone_title="M49", repo_name="api",
        issues=issues, assignment_rows=rows, audit_entries=audit_entries,
    )
    (c,) = card.issues
    assert c.first_pass == "yes"
    assert c.interventions["abandon"] == 0


def test_multiple_roots_fails_first_pass_without_any_intervention_kind():
    """A second, unrelated root (e.g. `coord retry` dispatching a fresh
    unlinked work row after a failure) fails first_pass but isn't a
    fix/rescue/nudge/abandon — it needs its own signal so a reader doesn't
    mistake "no interventions" for "clean first pass"."""
    issues = [_issue(1)]
    rows = [
        _root_row(1, assignment_id="r1", status="failed"),
        _root_row(1, assignment_id="r2", status="merged"),
    ]
    card = build_milestone_scorecard(
        milestone=49, milestone_title="M49", repo_name="api",
        issues=issues, assignment_rows=rows,
    )
    (c,) = card.issues
    assert c.first_pass == "no"
    assert c.multiple_roots is True
    assert c.interventions == {"fix": 0, "rescue": 0, "nudge": 0, "abandon": 0}
    assert card.totals["interventions"]["issues_with_multiple_roots"] == 1


def test_single_root_is_not_flagged_multiple_roots():
    issues = [_issue(1)]
    rows = [_root_row(1, assignment_id="r1", status="merged")]
    card = build_milestone_scorecard(
        milestone=49, milestone_title="M49", repo_name="api",
        issues=issues, assignment_rows=rows,
    )
    (c,) = card.issues
    assert c.multiple_roots is False
    assert card.totals["interventions"]["issues_with_multiple_roots"] == 0


# ── interventions: count + kind aggregation ─────────────────────────────────


def test_interventions_all_kinds_aggregate_by_milestone():
    issues = [_issue(1), _issue(2), _issue(3)]
    rows = [
        _root_row(1, assignment_id="r1", status="merged"),
        _human_fix_row(1, root_id="r1", assignment_id="f1", status="merged"),
        _root_row(2, assignment_id="r2", status="merged"),
        _human_fix_row(2, root_id="r2", assignment_id="rw1", kind_tag="rescue", status="merged"),
        _nudge_row(2, assignment_id="n1"),
        _root_row(3, assignment_id="r3", status="failed"),
    ]
    issues[2]["state"] = "CLOSED"
    card = build_milestone_scorecard(
        milestone=50, milestone_title="M50", repo_name="api",
        issues=issues, assignment_rows=rows,
    )
    totals = card.totals["interventions"]["by_kind"]
    assert totals == {"fix": 1, "rescue": 1, "nudge": 1, "abandon": 1}
    assert card.totals["interventions"]["total"] == 4
    assert set(INTERVENTION_KINDS) == {"fix", "rescue", "nudge", "abandon"}


# ── cost + wall-clock ─────────────────────────────────────────────────────


def test_cost_and_duration_roll_up_per_issue_and_total():
    issues = [_issue(1)]
    rows = [
        _root_row(1, assignment_id="r1", status="merged", cost_usd=2.0, dispatched_at=0.0, finished_at=100.0),
        _human_fix_row(1, root_id="r1", assignment_id="f1", status="merged"),
    ]
    card = build_milestone_scorecard(
        milestone=49, milestone_title="M49", repo_name="api",
        issues=issues, assignment_rows=rows,
    )
    (c,) = card.issues
    assert c.has_cost_data is True
    assert c.legs == 2
    assert c.cost_captured == 2.5
    assert card.totals["cost"]["total_usd"] == c.cost_total
    assert card.totals["cost"]["issues_with_data"] == 1
    assert card.totals["cost"]["issues_without_data"] == 0


def test_cost_unknown_when_no_board_rows_for_issue():
    issues = [_issue(1)]
    card = build_milestone_scorecard(
        milestone=49, milestone_title="M49", repo_name="api",
        issues=issues, assignment_rows=[],
    )
    (c,) = card.issues
    assert c.has_cost_data is False
    assert c.cost_total == 0.0
    assert card.totals["cost"]["issues_without_data"] == 1


def test_rows_from_other_repos_are_excluded():
    issues = [_issue(1)]
    rows = [
        _root_row(1, assignment_id="r1", status="merged", repo="api"),
        _root_row(1, assignment_id="r2", status="merged", repo="shared"),
    ]
    card = build_milestone_scorecard(
        milestone=49, milestone_title="M49", repo_name="api",
        issues=issues, assignment_rows=rows,
    )
    (c,) = card.issues
    assert c.legs == 1


# ── escaped defects / process bugs (label conventions) ──────────────────────


def test_escaped_defect_stage_from_label():
    issues = [_issue(1, labels=["escaped:post-merge"])]
    card = build_milestone_scorecard(
        milestone=49, milestone_title="M49", repo_name="api",
        issues=issues, assignment_rows=[],
    )
    (c,) = card.issues
    assert c.escaped_defect_stage == "post-merge"
    assert card.totals["escaped_defects"]["by_stage"]["post-merge"] == 1
    assert card.totals["escaped_defects"]["issues_unlabeled"] == 0


def test_escaped_defect_unlabeled_is_not_zero():
    issues = [_issue(1)]
    card = build_milestone_scorecard(
        milestone=49, milestone_title="M49", repo_name="api",
        issues=issues, assignment_rows=[],
    )
    (c,) = card.issues
    assert c.escaped_defect_stage is None
    assert card.totals["escaped_defects"]["issues_unlabeled"] == 1
    for stage in ESCAPED_DEFECT_STAGES:
        assert card.totals["escaped_defects"]["by_stage"][stage] == 0


def test_process_bug_regression_test_landed():
    issues = [_issue(1, labels=["process-bug", "regression-test:landed"])]
    card = build_milestone_scorecard(
        milestone=49, milestone_title="M49", repo_name="api",
        issues=issues, assignment_rows=[],
    )
    (c,) = card.issues
    assert c.process_bug is True
    assert c.regression_test == "landed"
    assert card.totals["process_bugs"]["count"] == 1
    assert card.totals["process_bugs"]["regression_test"]["landed"] == 1


def test_process_bug_regression_test_unknown_when_untagged():
    issues = [_issue(1, labels=["process-bug"])]
    card = build_milestone_scorecard(
        milestone=49, milestone_title="M49", repo_name="api",
        issues=issues, assignment_rows=[],
    )
    (c,) = card.issues
    assert c.process_bug is True
    assert c.regression_test == "unknown"
    assert card.totals["process_bugs"]["regression_test"]["unknown"] == 1


def test_non_process_bug_has_no_regression_test_value():
    issues = [_issue(1)]
    card = build_milestone_scorecard(
        milestone=49, milestone_title="M49", repo_name="api",
        issues=issues, assignment_rows=[],
    )
    (c,) = card.issues
    assert c.process_bug is False
    assert c.regression_test is None


# ── JSON round-trip ──────────────────────────────────────────────────────────


def test_scorecard_to_dict_is_json_safe():
    import json

    issues = [_issue(1, labels=["process-bug", "escaped:review"])]
    rows = [_root_row(1, assignment_id="r1", status="merged")]
    card = build_milestone_scorecard(
        milestone=49, milestone_title="M49", repo_name="api",
        issues=issues, assignment_rows=rows,
    )
    payload = scorecard_to_dict(card)
    dumped = json.dumps(payload)
    reloaded = json.loads(dumped)
    assert reloaded["milestone"] == 49
    assert reloaded["issues"][0]["number"] == 1
    assert reloaded["issues"][0]["escaped_defect_stage"] == "review"
    assert reloaded["totals"]["issue_count"] == 1
