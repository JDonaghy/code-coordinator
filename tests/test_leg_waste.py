"""Tests for coord.leg_waste (#4099) — the triage-first waste report.

Pure-data tests: no daemon, no GitHub, no DB. Fixture board assignment rows
(daemon ``/board`` wire shape) + fixture audit-log entries
(``coord.state.list_audit_log``'s shape) go in; a
:class:`~coord.leg_waste.WasteReport` comes out.
"""

from __future__ import annotations

import pytest

from coord.leg_waste import (
    AVOIDABLE_KINDS,
    LEG_KINDS,
    audit_leg_kind_overrides,
    build_waste_report,
    classify_leg_kind,
    waste_report_to_dict,
)
from coord.usage_rollup import TimeWindow


def _row(
    assignment_id,
    *,
    repo="api",
    issue=501,
    type_="work",
    status="done",
    review_of_assignment_id=None,
    review_iteration=0,
    dispatched_at=None,
    finished_at=None,
    cost_usd=None,
    test_reason=None,
    smoke_test_reason=None,
    failure_reason=None,
    model=None,
    input_tokens=0,
    output_tokens=0,
):
    return {
        "assignment_id": assignment_id,
        "repo_name": repo,
        "issue_number": issue,
        "issue_title": f"issue {issue}",
        "type": type_,
        "status": status,
        "review_of_assignment_id": review_of_assignment_id,
        "review_iteration": review_iteration,
        "dispatched_at": dispatched_at,
        "finished_at": finished_at,
        "cost_usd": cost_usd,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "test_reason": test_reason,
        "smoke_test_reason": smoke_test_reason,
        "failure_reason": failure_reason,
    }


def _audit(
    *,
    repo="api",
    issue=501,
    category,
    event_type,
    ts,
    assignment_id=None,
    details=None,
):
    return {
        "repo": repo,
        "issue": issue,
        "category": category,
        "event_type": event_type,
        "ts": ts,
        "assignment_id": assignment_id,
        "details": details or {},
    }


_COMPILE_ERROR_TEXT = "error[E0308]: mismatched types\nerror: could not compile `foo`"


# ── classify_leg_kind / audit_leg_kind_overrides ────────────────────────────


def test_classify_leg_kind_root_work_is_root_placeholder():
    row = _row("w1", type_="work", review_of_assignment_id=None, review_iteration=0)
    assert classify_leg_kind(row) == "_root"


def test_classify_leg_kind_test_review_mechanic():
    assert classify_leg_kind(_row("t1", type_="smoke")) == "test"
    assert classify_leg_kind(_row("r1", type_="review")) == "review"
    assert classify_leg_kind(_row("m1", type_="conflict-fix")) == "mechanic"


def test_classify_leg_kind_fix_without_overrides():
    row = _row("f1", type_="work", review_of_assignment_id="w1", review_iteration=1)
    assert classify_leg_kind(row) == "fix"


def test_classify_leg_kind_fix_with_overrides():
    row = _row("f1", type_="work", review_of_assignment_id="w1", review_iteration=1)
    overrides = {"f1": "investigator"}
    assert classify_leg_kind(row, overrides) == "investigator"


def test_classify_leg_kind_other():
    assert classify_leg_kind(_row("p1", type_="plan")) == "other"


def test_audit_leg_kind_overrides_maps_each_event_type():
    entries = [
        _audit(category="board_doctor", event_type="fixit_leg_dispatched", ts=1.0, assignment_id="a1"),
        _audit(category="board_doctor", event_type="finisher_leg_dispatched", ts=2.0, assignment_id="a2"),
        _audit(category="merge", event_type="conflict_fix_dispatched", ts=3.0, assignment_id="a3"),
        _audit(category="merge", event_type="merged", ts=4.0, assignment_id="a4"),
    ]
    overrides = audit_leg_kind_overrides(entries)
    assert overrides == {"a1": "investigator", "a2": "finisher", "a3": "mechanic"}


def test_audit_leg_kind_overrides_empty_for_none():
    assert audit_leg_kind_overrides(None) == {}


# ── build_waste_report: full fixture board ──────────────────────────────────


def _fixture_rows_and_audit():
    rows = []
    audit = []

    # Issue 501: a compile-error root failure, a "retry" root that merges,
    # a clean test leg against the retry, a zero-commit fix round, and a
    # clean test leg against that fix round.
    rows.append(
        _row(
            "w1", issue=501, type_="work", status="failed",
            dispatched_at=1000.0, finished_at=1100.0, cost_usd=1.0,
            test_reason=_COMPILE_ERROR_TEXT, smoke_test_reason="fail",
        )
    )
    audit.append(
        _audit(
            issue=501, category="test", event_type="test_failed", ts=1150.0,
            assignment_id="w1", details={"test_reason": _COMPILE_ERROR_TEXT},
        )
    )
    rows.append(
        _row(
            "w1b", issue=501, type_="work", status="merged",
            dispatched_at=1200.0, finished_at=1300.0, cost_usd=1.0,
        )
    )
    rows.append(
        _row(
            "t1", issue=501, type_="smoke", status="done",
            review_of_assignment_id="w1b", dispatched_at=1320.0,
            finished_at=1330.0, cost_usd=0.2,
        )
    )
    audit.append(
        _audit(
            issue=501, category="test", event_type="test_passed", ts=1330.0,
            assignment_id="w1b",
        )
    )
    rows.append(
        _row(
            "f1", issue=501, type_="work", status="advisory",
            review_of_assignment_id="w1b", review_iteration=1,
            dispatched_at=1400.0, finished_at=1450.0,
        )
    )
    rows.append(
        _row(
            "t2", issue=501, type_="smoke", status="done",
            review_of_assignment_id="f1", dispatched_at=1460.0,
            finished_at=1470.0, cost_usd=0.2,
        )
    )

    # Issue 503: a merged root, then a redundant Test leg re-run on the SAME
    # target after a compile-error verdict already fired for it.
    rows.append(
        _row(
            "w3", issue=503, type_="work", status="merged",
            dispatched_at=2000.0, finished_at=2100.0, cost_usd=1.0,
        )
    )
    rows.append(
        _row(
            "t3a", issue=503, type_="smoke", status="done",
            review_of_assignment_id="w3", dispatched_at=2050.0, finished_at=2080.0,
        )
    )
    audit.append(
        _audit(
            issue=503, category="test", event_type="test_failed", ts=2090.0,
            assignment_id="w3", details={"test_reason": _COMPILE_ERROR_TEXT},
        )
    )
    rows.append(
        _row(
            "t3b", issue=503, type_="smoke", status="done",
            review_of_assignment_id="w3", dispatched_at=2095.0, finished_at=2110.0,
        )
    )
    audit.append(
        _audit(
            issue=503, category="test", event_type="test_passed", ts=2200.0,
            assignment_id="w3",
        )
    )

    # Issue 504: one of each remaining kind (review, mechanic, investigator,
    # finisher, other).
    rows.append(
        _row("d1", issue=504, type_="work", status="merged", dispatched_at=3000.0, finished_at=3100.0)
    )
    rows.append(
        _row(
            "d1-review", issue=504, type_="review", status="done",
            review_of_assignment_id="d1", dispatched_at=3110.0, finished_at=3120.0,
        )
    )
    rows.append(
        _row(
            "d1-cf", issue=504, type_="conflict-fix", status="done",
            dispatched_at=3130.0, finished_at=3140.0,
        )
    )
    rows.append(
        _row(
            "d1-fix-inv", issue=504, type_="work", status="done",
            review_of_assignment_id="d1", review_iteration=1,
            dispatched_at=3150.0, finished_at=3160.0,
        )
    )
    audit.append(
        _audit(
            issue=504, category="board_doctor", event_type="fixit_leg_dispatched",
            ts=3150.0, assignment_id="d1-fix-inv",
        )
    )
    rows.append(
        _row(
            "d1-fix-fin", issue=504, type_="work", status="done",
            review_of_assignment_id="d1", review_iteration=2,
            dispatched_at=3170.0, finished_at=3180.0,
        )
    )
    audit.append(
        _audit(
            issue=504, category="board_doctor", event_type="finisher_leg_dispatched",
            ts=3170.0, assignment_id="d1-fix-fin",
        )
    )
    rows.append(
        _row(
            "d1-plan", issue=504, type_="plan", status="done",
            dispatched_at=3190.0, finished_at=3200.0,
        )
    )

    # Issue 505: never merged — must be excluded from the report entirely.
    rows.append(
        _row("w5", issue=505, type_="work", status="failed", dispatched_at=4000.0, finished_at=4100.0)
    )

    return rows, audit


def _issue(report, number):
    for i in report.issues:
        if i.issue_number == number:
            return i
    raise AssertionError(f"issue {number} not in report")


def test_build_waste_report_excludes_unmerged_issues():
    rows, audit = _fixture_rows_and_audit()
    report = build_waste_report(rows, window=TimeWindow(), repo="api", audit_entries=audit)
    assert {i.issue_number for i in report.issues} == {501, 503, 504}


def test_build_waste_report_leg_kinds_issue_501():
    rows, audit = _fixture_rows_and_audit()
    report = build_waste_report(rows, window=TimeWindow(), repo="api", audit_entries=audit)
    issue = _issue(report, 501)
    assert issue.legs.work == 1
    assert issue.legs.retry == 1
    assert issue.legs.fix == 1
    assert issue.legs.test == 2
    assert issue.legs.total == 5


def test_build_waste_report_avoidable_compile_error_retry():
    rows, audit = _fixture_rows_and_audit()
    report = build_waste_report(rows, window=TimeWindow(), repo="api", audit_entries=audit)
    issue = _issue(report, 501)
    assert issue.avoidable.compile_error_retries == 1
    assert issue.avoidable.zero_commit_fix_rounds == 1
    # Both Test legs in this issue retest a fresh target with no PRIOR
    # failure recorded against that exact target — neither is a known-
    # broken-SHA rerun.
    assert issue.avoidable.test_reruns_on_known_broken_sha == 0


def test_build_waste_report_failure_to_green_issue_501():
    rows, audit = _fixture_rows_and_audit()
    report = build_waste_report(rows, window=TimeWindow(), repo="api", audit_entries=audit)
    issue = _issue(report, 501)
    assert issue.failure_to_green_secs == 180.0  # 1330 - 1150
    assert issue.had_unresolved_failure is False
    assert issue.has_audit_data is True


def test_build_waste_report_test_rerun_on_known_broken_sha():
    rows, audit = _fixture_rows_and_audit()
    report = build_waste_report(rows, window=TimeWindow(), repo="api", audit_entries=audit)
    issue = _issue(report, 503)
    assert issue.legs.test == 2
    assert issue.avoidable.test_reruns_on_known_broken_sha == 1
    assert issue.failure_to_green_secs == 110.0  # 2200 - 2090


def test_build_waste_report_kind_split_needs_audit_issue_504():
    rows, audit = _fixture_rows_and_audit()
    report = build_waste_report(rows, window=TimeWindow(), repo="api", audit_entries=audit)
    issue = _issue(report, 504)
    assert issue.legs.work == 1
    assert issue.legs.review == 1
    assert issue.legs.mechanic == 1
    assert issue.legs.investigator == 1
    assert issue.legs.finisher == 1
    assert issue.legs.other == 1
    assert issue.legs.fix == 0
    # No test-verdict audit events exist for this issue.
    assert issue.failure_to_green_secs is None
    assert issue.has_audit_data is False


def test_build_waste_report_degrades_without_audit_entries():
    rows, _audit = _fixture_rows_and_audit()
    report = build_waste_report(rows, window=TimeWindow(), repo="api", audit_entries=None)
    issue_504 = _issue(report, 504)
    # Without audit data, investigator/finisher collapse into "fix" rather
    # than vanishing or raising.
    assert issue_504.legs.investigator == 0
    assert issue_504.legs.finisher == 0
    assert issue_504.legs.fix == 2
    assert issue_504.failure_to_green_secs is None
    assert issue_504.has_audit_data is False

    issue_503 = _issue(report, 503)
    assert issue_503.avoidable.test_reruns_on_known_broken_sha == 0


def test_build_waste_report_repo_filter():
    rows, audit = _fixture_rows_and_audit()
    other_repo_row = _row("x1", repo="other", issue=901, type_="work", status="merged",
                           dispatched_at=5000.0, finished_at=5100.0)
    report = build_waste_report(rows + [other_repo_row], window=TimeWindow(), repo="api", audit_entries=audit)
    assert all(i.repo_name == "api" for i in report.issues)
    assert 901 not in {i.issue_number for i in report.issues}


def test_build_waste_report_window_filters_rows_and_audit():
    rows, audit = _fixture_rows_and_audit()
    # Window starting after issue 503's rows/events — its Test legs are
    # outside the window, so compile-error signal and leg counts both drop.
    window = TimeWindow(start=2096.0, end=None, label="since")
    report = build_waste_report(rows, window=window, repo="api", audit_entries=audit)
    issue_numbers = {i.issue_number for i in report.issues}
    assert 501 not in issue_numbers  # entirely before the window
    issue_503 = _issue(report, 503)
    # t3a (dispatched 2050, finished 2080) is fully before the window and
    # drops out; t3b (dispatched 2095, finished 2110) straddles the start
    # and counts (leg_in_window is dispatched_at OR finished_at in range).
    # w3 (dispatched 2000, finished 2100) also straddles the start the same
    # way, so it still counts too.
    assert issue_503.legs.test == 1
    assert issue_503.legs.work == 1


def test_build_waste_report_totals_and_by_repo():
    rows, audit = _fixture_rows_and_audit()
    report = build_waste_report(rows, window=TimeWindow(), repo="api", audit_entries=audit)
    assert report.totals["issue_count"] == 3
    assert report.totals["legs_total"] == sum(i.legs.total for i in report.issues)
    assert report.totals["avoidable_total"] == sum(i.avoidable.total for i in report.issues)
    assert "api" in report.by_repo
    assert report.by_repo["api"]["issue_count"] == 3


def test_build_waste_report_cost_by_stage():
    rows, audit = _fixture_rows_and_audit()
    report = build_waste_report(rows, window=TimeWindow(), repo="api", audit_entries=audit)
    issue = _issue(report, 501)
    assert issue.cost_by_stage["work"]["legs"] == 3  # w1, w1b, f1
    assert issue.cost_by_stage["smoke"]["legs"] == 2  # t1, t2
    assert issue.cost_captured == pytest.approx(2.4)  # 1.0 + 1.0 + 0.2 + 0.2


def test_waste_report_to_dict_shape():
    rows, audit = _fixture_rows_and_audit()
    report = build_waste_report(rows, window=TimeWindow(), repo="api", audit_entries=audit)
    d = waste_report_to_dict(report)
    assert set(d.keys()) == {"window", "repo_filter", "issues", "totals", "by_repo"}
    assert d["repo_filter"] == "api"
    for issue_dict in d["issues"]:
        assert set(LEG_KINDS) <= set(issue_dict["legs"].keys())
        assert set(AVOIDABLE_KINDS) <= set(issue_dict["avoidable"].keys())


def test_build_waste_report_uses_the_passed_in_pricing_not_the_builtin_default():
    """An operator ``pricing:`` override must change the reported cost —
    the whole point of threading *pricing* through instead of defaulting."""
    from coord.config import ModelRates, PricingConfig

    rows = [
        _row(
            "w1", issue=701, status="merged", dispatched_at=1000.0, finished_at=1100.0,
            model="sonnet", input_tokens=100, output_tokens=50,
        ),
    ]
    builtin_report = build_waste_report(rows, window=TimeWindow(), repo="api")
    builtin_cost = _issue(builtin_report, 701).cost_est

    override = PricingConfig(models={"sonnet": ModelRates(input=3000.0, output=15.0)})
    overridden_report = build_waste_report(rows, window=TimeWindow(), repo="api", pricing=override)
    overridden_cost = _issue(overridden_report, 701).cost_est

    assert overridden_cost != pytest.approx(builtin_cost)
    assert overridden_cost == pytest.approx((100 * 3000.0 + 50 * 15.0) / 1_000_000.0)


def test_build_waste_report_flags_unknown_model_legs_instead_of_silent_zero():
    """A leg that ran on a model with no priced rate must surface as
    ``unknown_model_legs``, not disappear as a silent $0 in cost_total."""
    rows = [
        _row(
            "w1", issue=702, status="merged", dispatched_at=1000.0, finished_at=1100.0,
            model="opencode/glm-5.2", input_tokens=100, output_tokens=50,
        ),
        _row(
            "w2", issue=703, status="merged", dispatched_at=1000.0, finished_at=1100.0,
            model="sonnet", input_tokens=100, output_tokens=50,
        ),
    ]
    report = build_waste_report(rows, window=TimeWindow(), repo="api")

    unpriced = _issue(report, 702)
    assert unpriced.unknown_model_legs == 1
    assert unpriced.cost_total == 0.0
    assert unpriced.to_dict()["unknown_model_legs"] == 1

    priced = _issue(report, 703)
    assert priced.unknown_model_legs == 0

    assert report.totals["unknown_model_legs"] == 1
    assert report.by_repo["api"]["unknown_model_legs"] == 1
