"""Tests for coord.usage_rollup (#1118): the pure cost/token/duration
aggregator + pricing estimator over daemon board assignment rows.

The seeded fixture below mirrors the shape (not the exact pricing table --
see the module docstring in tests/acceptance/ms-37/contract.md, which notes
its pricing table is a fixture value, not the shipped default) of the
Gate-A acceptance contract's seeded board: 6 legs, 2 issues, 2 repos, mixed
interactive/non-interactive, one unknown-model leg, one running (no
finished_at) leg. Where the contract's own worked-example numbers are
reproducible with this fixture's pricing table, we assert on them directly
as a cross-check against the documented contract.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from coord.config import ModelRates, PricingConfig
from coord.usage_rollup import (
    GroupRollup,
    IssueKey,
    TimeWindow,
    aggregate,
    leg_cost,
    leg_duration,
    leg_in_window,
    normalize_model,
    parse_timestamp,
    rollup,
    rollup_by_stage,
    window_month,
    window_since,
    window_today,
    window_week,
)


# ── Fixture: the ms-37 contract's 6-leg board, reproduced ───────────────────

REF_DAY = datetime(2026, 7, 13)
NOW = REF_DAY.replace(hour=15, minute=0, second=0).timestamp()


def _t(hour: int, minute: int = 0) -> float:
    return REF_DAY.replace(hour=hour, minute=minute, second=0).timestamp()


def _leg(
    *,
    issue_number: int,
    repo_name: str,
    type: str,
    model: str | None,
    is_interactive: bool,
    cost_usd: float | None,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    dispatched_at: float,
    finished_at: float | None,
    cache_creation_tokens: int = 0,
    num_turns: int = 0,
) -> dict:
    return {
        "issue_number": issue_number,
        "issue_title": f"issue {issue_number}",
        "repo_name": repo_name,
        "type": type,
        "model": model,
        "is_interactive": is_interactive,
        "status": "done" if finished_at is not None else "running",
        "cost_usd": cost_usd,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "num_turns": num_turns,
        "dispatched_at": dispatched_at,
        "finished_at": finished_at,
    }


# L1-L6, matching tests/acceptance/ms-37/contract.md's seeded board table.
L1 = _leg(
    issue_number=501, repo_name="alpha", type="work", model="sonnet", is_interactive=False,
    cost_usd=0.50, input_tokens=10_000, output_tokens=100_000, cache_read_tokens=1_000_000,
    dispatched_at=_t(9, 0), finished_at=_t(9, 10), num_turns=40,  # 600s
)
L2 = _leg(
    issue_number=501, repo_name="alpha", type="review", model="sonnet", is_interactive=True,
    cost_usd=None, input_tokens=2_000, output_tokens=50_000, cache_read_tokens=500_000,
    dispatched_at=_t(9, 20), finished_at=_t(9, 25), num_turns=10,  # 300s
)
L3 = _leg(
    issue_number=502, repo_name="beta", type="work", model="opus", is_interactive=False,
    cost_usd=2.00, input_tokens=20_000, output_tokens=200_000, cache_read_tokens=2_000_000,
    dispatched_at=_t(10, 0), finished_at=_t(10, 20), num_turns=80,  # 1200s
)
L4 = _leg(
    issue_number=502, repo_name="beta", type="smoke", model="sonnet", is_interactive=True,
    cost_usd=None, input_tokens=4_000, output_tokens=80_000, cache_read_tokens=800_000,
    dispatched_at=_t(10, 30), finished_at=_t(10, 30) + 400, num_turns=15,  # 400s = 6m40s
)
L5 = _leg(
    issue_number=502, repo_name="beta", type="chat", model="(unknown)", is_interactive=True,
    cost_usd=None, input_tokens=1_000, output_tokens=30_000, cache_read_tokens=300_000,
    dispatched_at=_t(11, 0), finished_at=_t(11, 0) + 200, num_turns=5,
)
L6 = _leg(
    issue_number=502, repo_name="beta", type="work", model="sonnet", is_interactive=False,
    cost_usd=None, input_tokens=0, output_tokens=0, cache_read_tokens=0,
    dispatched_at=_t(12, 0), finished_at=None,  # running; num_turns left at 0
)

ALL_LEGS = [L1, L2, L3, L4, L5, L6]

# The contract's fixture pricing table (per 1M tokens) -- explicitly a test
# fixture, not the shipped coordinator.yml defaults (those live in
# coord.config._default_pricing and are checked separately).
FIXTURE_PRICING = PricingConfig(
    models={
        "sonnet": ModelRates(input=3.00, output=15.00, cache_read=0.30, cache_creation=3.75),
        "opus": ModelRates(input=15.00, output=75.00, cache_read=1.50, cache_creation=18.75),
    }
)


# ── Cross-check against the ms-37 contract's Mock 1 (by-issue) numbers ─────


def test_rollup_by_issue_matches_contract_mock1() -> None:
    window = window_today(now=NOW)
    result = rollup(ALL_LEGS, group_by="issue", window=window, pricing=FIXTURE_PRICING)

    beta = result.groups[IssueKey("beta", 502)]
    assert beta.legs == 4
    assert beta.cost_captured == pytest.approx(2.00)
    assert beta.cost_est == pytest.approx(1.4520)
    assert beta.cost_total == pytest.approx(3.4520)
    assert beta.tokens.output == 310_000
    assert beta.tokens.cache_read == 3_100_000
    assert beta.duration_secs == pytest.approx(1800.0)
    assert beta.open_legs == 1
    assert beta.unknown_model_legs == 1
    assert beta.has_unknown_model is True
    # #2786: L3(80) + L4(15) + L5(5) + L6(0, running/never reported) = 100.
    assert beta.turns == 100

    alpha = result.groups[IssueKey("alpha", 501)]
    assert alpha.legs == 2
    assert alpha.cost_captured == pytest.approx(0.50)
    assert alpha.cost_est == pytest.approx(0.9060)
    assert alpha.cost_total == pytest.approx(1.4060)
    assert alpha.tokens.output == 150_000
    assert alpha.tokens.cache_read == 1_500_000
    assert alpha.duration_secs == pytest.approx(900.0)
    assert alpha.open_legs == 0
    assert alpha.unknown_model_legs == 0
    # #2786: L1(40) + L2(10) = 50.
    assert alpha.turns == 50

    total = result.total
    assert total.legs == 6
    assert total.cost_captured == pytest.approx(2.50)
    assert total.cost_est == pytest.approx(2.3580)
    assert total.cost_total == pytest.approx(4.8580)
    assert total.tokens.output == 460_000
    assert total.tokens.cache_read == 4_600_000
    assert total.duration_secs == pytest.approx(2700.0)
    assert total.open_legs == 1
    # #2786: 100 (beta) + 50 (alpha) = 150.
    assert total.turns == 150


def test_rollup_by_repo_matches_by_issue_totals_for_this_fixture() -> None:
    # This fixture has exactly one issue per repo, so by-repo sums equal
    # by-issue sums (a genuine cross-repo, multi-issue rollup is #1119's
    # concern; this is just a sanity check that group_by="repo" works).
    window = window_today(now=NOW)
    result = rollup(ALL_LEGS, group_by="repo", window=window, pricing=FIXTURE_PRICING)
    assert result.groups["beta"].cost_total == pytest.approx(3.4520)
    assert result.groups["alpha"].cost_total == pytest.approx(1.4060)


# ── #2786: num_turns accumulation ────────────────────────────────────────────


def test_turns_defaults_to_zero_for_an_empty_group() -> None:
    """A row predating the `assignments.num_turns` column (or a group with
    no legs at all) reads as 0, not a missing attribute."""
    assert GroupRollup(key=None).turns == 0


def test_turns_accumulates_and_degrades_gracefully_when_the_key_is_absent() -> None:
    """A row that never carries `num_turns` at all (e.g. hand-built board
    payload from before #2786) contributes 0 rather than raising."""
    window = window_today(now=NOW)
    with_turns = _leg(
        issue_number=1, repo_name="r", type="work", model="sonnet", is_interactive=False,
        cost_usd=0.10, input_tokens=100, output_tokens=100, cache_read_tokens=1_000,
        dispatched_at=_t(9, 0), finished_at=_t(9, 5), num_turns=7,
    )
    no_turns_key = _leg(
        issue_number=1, repo_name="r", type="work", model="sonnet", is_interactive=False,
        cost_usd=0.10, input_tokens=100, output_tokens=100, cache_read_tokens=1_000,
        dispatched_at=_t(9, 10), finished_at=_t(9, 15), num_turns=3,
    )
    del no_turns_key["num_turns"]

    result = rollup([with_turns, no_turns_key], group_by="repo", window=window, pricing=FIXTURE_PRICING)
    assert result.groups["r"].turns == 7
    assert result.groups["r"].legs == 2


def test_rollup_by_stage_matches_contract_mock4() -> None:
    window = window_today(now=NOW)
    result = rollup_by_stage(ALL_LEGS, window, pricing=FIXTURE_PRICING)

    work = result.groups["work"]
    assert work.legs == 3  # L1, L3, L6
    assert work.duration_secs == pytest.approx(1800.0)  # 600 + 1200 + 0 (L6 running)
    assert work.open_legs == 1

    smoke = result.groups["smoke"]
    assert smoke.legs == 1
    assert smoke.duration_secs == pytest.approx(400.0)

    review = result.groups["review"]
    assert review.legs == 1
    assert review.duration_secs == pytest.approx(300.0)

    chat = result.groups["chat"]
    assert chat.legs == 1
    assert chat.duration_secs == pytest.approx(200.0)

    assert result.total.duration_secs == pytest.approx(2700.0)


# ── Grouping: day/week/month buckets ─────────────────────────────────────────
#
# The Contract lists day/week/month (alongside issue/repo/stage) as grouping
# dimensions `_group_key_for` must support (see the module docstring). These
# were previously untested -- flagged by review as the trickiest date math in
# this module (ISO week numbering, year-end week/month rollover).


def _leg_at(
    dt: datetime, *, issue_number: int = 900, repo_name: str = "gamma", duration_secs: float = 600.0
) -> dict:
    """A minimal leg dispatched (and finished ``duration_secs`` later) at *dt*
    -- for pinning day/week/month bucket keys, not cost/token totals."""
    dispatched = dt.timestamp()
    return _leg(
        issue_number=issue_number, repo_name=repo_name, type="work", model="sonnet",
        is_interactive=False, cost_usd=1.0, input_tokens=1_000, output_tokens=1_000,
        cache_read_tokens=0, dispatched_at=dispatched, finished_at=dispatched + duration_secs,
    )


def test_rollup_by_day_week_month_on_fixture_single_bucket() -> None:
    # All 6 fixture legs are dispatched on the same calendar day (REF_DAY, a
    # Monday), so day/week/month grouping each produce exactly one bucket
    # holding the same totals as the by-issue grand total asserted above.
    window = window_today(now=NOW)
    for group_by, expected_key in (
        ("day", "2026-07-13"),
        ("week", "2026-W29"),
        ("month", "2026-07"),
    ):
        result = rollup(ALL_LEGS, group_by=group_by, window=window, pricing=FIXTURE_PRICING)
        assert set(result.groups) == {expected_key}
        bucket = result.groups[expected_key]
        assert bucket.legs == 6
        assert bucket.cost_total == pytest.approx(4.8580)
        assert bucket.duration_secs == pytest.approx(2700.0)


def test_group_by_day_buckets_by_calendar_date() -> None:
    leg_a = _leg_at(datetime(2026, 7, 13, 9, 0))
    leg_b = _leg_at(datetime(2026, 7, 14, 9, 0))
    window = TimeWindow(
        start=datetime(2026, 7, 13).timestamp(), end=datetime(2026, 7, 15).timestamp()
    )
    result = rollup([leg_a, leg_b], group_by="day", window=window)
    assert set(result.groups) == {"2026-07-13", "2026-07-14"}
    assert result.groups["2026-07-13"].legs == 1
    assert result.groups["2026-07-14"].legs == 1
    assert result.total.legs == 2


def test_group_by_week_buckets_by_iso_week() -> None:
    # 2026-07-13 (Mon) is ISO week 2026-W29; 8 days later crosses into the
    # next ISO week.
    leg_a = _leg_at(datetime(2026, 7, 13, 9, 0))
    leg_b = _leg_at(datetime(2026, 7, 21, 9, 0))
    window = TimeWindow(
        start=datetime(2026, 7, 1).timestamp(), end=datetime(2026, 8, 1).timestamp()
    )
    result = rollup([leg_a, leg_b], group_by="week", window=window)
    assert set(result.groups) == {"2026-W29", "2026-W30"}
    assert result.groups["2026-W29"].legs == 1
    assert result.groups["2026-W30"].legs == 1


def test_group_by_month_buckets_by_calendar_month() -> None:
    leg_a = _leg_at(datetime(2026, 7, 31, 9, 0))
    leg_b = _leg_at(datetime(2026, 8, 1, 9, 0))
    window = TimeWindow(
        start=datetime(2026, 7, 1).timestamp(), end=datetime(2026, 9, 1).timestamp()
    )
    result = rollup([leg_a, leg_b], group_by="month", window=window)
    assert set(result.groups) == {"2026-07", "2026-08"}
    assert result.groups["2026-07"].legs == 1
    assert result.groups["2026-08"].legs == 1


def test_group_by_day_week_month_year_end_rollover() -> None:
    """Boundary case: Dec 31 2025 -> Jan 1 2026 crosses a day, a month, AND a
    calendar-year boundary -- but per ISO 8601 both timestamps fall in the
    SAME ISO week (2026-W01), since ISO week 1 of a year is the Mon-Sun week
    containing that year's first Thursday, and Dec 29 2025 - Jan 4 2026 is a
    single such week. This pins the (correct, non-obvious) behavior that a
    day/month rollover doesn't necessarily imply a week rollover -- exactly
    the kind of date math review flagged as needing a pinned test.
    """
    leg_a = _leg_at(datetime(2025, 12, 31, 23, 0), issue_number=901)
    leg_b = _leg_at(datetime(2026, 1, 1, 1, 0), issue_number=902)
    window = TimeWindow(
        start=datetime(2025, 12, 31).timestamp(), end=datetime(2026, 1, 2).timestamp()
    )

    by_day = rollup([leg_a, leg_b], group_by="day", window=window)
    assert set(by_day.groups) == {"2025-12-31", "2026-01-01"}
    assert by_day.groups["2025-12-31"].legs == 1
    assert by_day.groups["2026-01-01"].legs == 1

    by_month = rollup([leg_a, leg_b], group_by="month", window=window)
    assert set(by_month.groups) == {"2025-12", "2026-01"}
    assert by_month.groups["2025-12"].legs == 1
    assert by_month.groups["2026-01"].legs == 1

    by_week = rollup([leg_a, leg_b], group_by="week", window=window)
    assert set(by_week.groups) == {"2026-W01"}
    assert by_week.groups["2026-W01"].legs == 2


def test_leg_rows_retained_for_drill_down() -> None:
    window = window_today(now=NOW)
    result = rollup(ALL_LEGS, group_by="issue", window=window, pricing=FIXTURE_PRICING)
    beta_rows = result.groups[IssueKey("beta", 502)].leg_rows
    assert len(beta_rows) == 4
    assert L5 in beta_rows
    assert L6 in beta_rows


# ── Estimator: alias normalization ───────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("sonnet", "sonnet"),
        ("SONNET", "sonnet"),
        (" sonnet ", "sonnet"),
        ("claude-sonnet-4-6", "sonnet"),
        ("claude-sonnet-5", "sonnet"),
        ("opus", "opus"),
        ("claude-opus-4-7", "opus"),
        ("claude-opus-4-8", "opus"),
        ("haiku", "haiku"),
        ("claude-haiku-4-5", "haiku"),
        ("fable", "fable"),
        ("FABLE", "fable"),
        (" fable ", "fable"),
        ("claude-fable-5", "fable"),
        ("(unknown)", "(unknown)"),
        ("", "(unknown)"),
        (None, "(unknown)"),
        ("some-future-model-nobody-mapped-yet", "(unknown)"),
    ],
)
def test_normalize_model(raw: str | None, expected: str) -> None:
    assert normalize_model(raw) == expected


# ── Estimator: tokens x rate, unknown-model flag, no-double-count ──────────


def test_estimate_tokens_times_rate() -> None:
    row = _leg(
        issue_number=1, repo_name="r", type="work", model="sonnet", is_interactive=False,
        cost_usd=None, input_tokens=2_000, output_tokens=50_000, cache_read_tokens=500_000,
        dispatched_at=_t(9, 0), finished_at=_t(9, 5),
    )
    captured, est, unknown = leg_cost(row, FIXTURE_PRICING)
    assert captured == 0.0
    assert est == pytest.approx(0.9060)
    assert unknown is False


def test_estimate_unknown_model_flags_and_produces_no_estimate() -> None:
    row = _leg(
        issue_number=1, repo_name="r", type="chat", model="totally-unrecognized", is_interactive=True,
        cost_usd=None, input_tokens=1_000, output_tokens=1_000, cache_read_tokens=0,
        dispatched_at=_t(9, 0), finished_at=_t(9, 1),
    )
    captured, est, unknown = leg_cost(row, FIXTURE_PRICING)
    assert captured == 0.0
    assert est == 0.0
    assert unknown is True


def test_estimate_fable_model_priced_not_flagged() -> None:
    # Mirrors test_estimate_unknown_model_flags_and_produces_no_estimate but
    # for the positive case: a fable-attributed leg must be priced using its
    # rate, not flagged as unknown (#1290 fix — normalize_model previously
    # returned "(unknown)" for "fable" because _KNOWN_CANONICAL omitted it).
    pricing = PricingConfig(
        models={
            **FIXTURE_PRICING.models,
            "fable": ModelRates(input=10.00, output=50.00, cache_read=1.00, cache_creation=12.50),
        }
    )
    row = _leg(
        issue_number=1, repo_name="r", type="work", model="fable", is_interactive=False,
        cost_usd=None, input_tokens=1_000, output_tokens=1_000, cache_read_tokens=0,
        dispatched_at=_t(9, 0), finished_at=_t(9, 1),
    )
    captured, est, unknown = leg_cost(row, pricing)
    assert captured == 0.0
    assert est == pytest.approx(0.01 + 0.05)
    assert unknown is False


def test_captured_cost_never_double_counted_with_estimate() -> None:
    row = _leg(
        issue_number=1, repo_name="r", type="work", model="sonnet", is_interactive=False,
        cost_usd=2.00, input_tokens=20_000, output_tokens=200_000, cache_read_tokens=2_000_000,
        dispatched_at=_t(10, 0), finished_at=_t(10, 20),
    )
    captured, est, unknown = leg_cost(row, FIXTURE_PRICING)
    assert captured == pytest.approx(2.00)
    assert est == 0.0
    assert unknown is False


def test_cost_usd_zero_is_treated_as_missing_and_estimated() -> None:
    row = _leg(
        issue_number=1, repo_name="r", type="work", model="sonnet", is_interactive=False,
        cost_usd=0.0, input_tokens=2_000, output_tokens=50_000, cache_read_tokens=500_000,
        dispatched_at=_t(9, 0), finished_at=_t(9, 5),
    )
    captured, est, unknown = leg_cost(row, FIXTURE_PRICING)
    assert captured == 0.0
    assert est == pytest.approx(0.9060)
    assert unknown is False


def test_no_tokens_and_no_cost_is_all_zero() -> None:
    row = _leg(
        issue_number=1, repo_name="r", type="work", model="sonnet", is_interactive=False,
        cost_usd=None, input_tokens=0, output_tokens=0, cache_read_tokens=0,
        dispatched_at=_t(12, 0), finished_at=None,
    )
    captured, est, unknown = leg_cost(row, FIXTURE_PRICING)
    assert (captured, est, unknown) == (0.0, 0.0, False)


# ── Duration + open legs ─────────────────────────────────────────────────────


def test_duration_running_leg_contributes_zero_and_is_open() -> None:
    duration, is_open = leg_duration(L6)
    assert duration == 0.0
    assert is_open is True


def test_duration_completed_leg() -> None:
    duration, is_open = leg_duration(L3)
    assert duration == pytest.approx(1200.0)
    assert is_open is False


def test_duration_clamps_negative_to_zero() -> None:
    row = dict(L1)
    row["dispatched_at"], row["finished_at"] = row["finished_at"], row["dispatched_at"]
    duration, is_open = leg_duration(row)
    assert duration == 0.0
    assert is_open is False


# ── Window predicate: dispatched-or-finished, boundary ──────────────────────


def test_window_dispatched_or_finished_either_counts() -> None:
    window = TimeWindow(start=_t(10, 0), end=_t(11, 0))
    # Started before the window, finished inside it -- still in-window.
    straddling = _leg(
        issue_number=1, repo_name="r", type="work", model="sonnet", is_interactive=False,
        cost_usd=None, input_tokens=0, output_tokens=0, cache_read_tokens=0,
        dispatched_at=_t(9, 30), finished_at=_t(10, 30),
    )
    assert leg_in_window(straddling, window) is True

    # Dispatched inside, still running (no finished_at) -- in-window.
    running = _leg(
        issue_number=1, repo_name="r", type="work", model="sonnet", is_interactive=False,
        cost_usd=None, input_tokens=0, output_tokens=0, cache_read_tokens=0,
        dispatched_at=_t(10, 15), finished_at=None,
    )
    assert leg_in_window(running, window) is True

    # Entirely before the window.
    outside = _leg(
        issue_number=1, repo_name="r", type="work", model="sonnet", is_interactive=False,
        cost_usd=None, input_tokens=0, output_tokens=0, cache_read_tokens=0,
        dispatched_at=_t(8, 0), finished_at=_t(8, 30),
    )
    assert leg_in_window(outside, window) is False


def test_window_boundary_start_inclusive_end_exclusive() -> None:
    window = TimeWindow(start=_t(10, 0), end=_t(11, 0))
    at_start = _leg(
        issue_number=1, repo_name="r", type="work", model="sonnet", is_interactive=False,
        cost_usd=None, input_tokens=0, output_tokens=0, cache_read_tokens=0,
        dispatched_at=_t(10, 0), finished_at=None,
    )
    at_end = _leg(
        issue_number=1, repo_name="r", type="work", model="sonnet", is_interactive=False,
        cost_usd=None, input_tokens=0, output_tokens=0, cache_read_tokens=0,
        dispatched_at=_t(11, 0), finished_at=None,
    )
    assert leg_in_window(at_start, window) is True
    assert leg_in_window(at_end, window) is False


def test_explicit_bounded_range_both_ends_set() -> None:
    # The general TimeWindow constructor (arbitrary start+end), not a preset --
    # e.g. the TUI's range picker (#1116) or a future CLI --since/--until pair.
    window = TimeWindow(start=_t(9, 15), end=_t(10, 25), label="custom")
    result = rollup(ALL_LEGS, group_by="issue", window=window, pricing=FIXTURE_PRICING)
    # L1 (09:00-09:10) is entirely before the window. L2 (dispatched 09:20)
    # and L3 (dispatched 10:00) both fall inside [09:15, 10:25). L4-L6 start
    # at/after 10:30, at or past the window's end.
    assert result.total.legs == 2
    assert result.groups[IssueKey("alpha", 501)].legs == 1  # L2 only
    assert result.groups[IssueKey("beta", 502)].legs == 1  # L3 only


def test_window_since_relative_days() -> None:
    window = window_since("2d", now=NOW)
    assert window.end is None
    assert window.start == pytest.approx(NOW - 2 * 86400)


def test_window_since_relative_hours() -> None:
    window = window_since("6h", now=NOW)
    assert window.end is None
    assert window.start == pytest.approx(NOW - 6 * 3600)


def test_window_since_iso() -> None:
    window = window_since("2026-07-01T00:00:00", now=NOW)
    assert window.end is None
    assert window.start == pytest.approx(datetime(2026, 7, 1).timestamp())


def test_window_since_invalid_spec_raises() -> None:
    with pytest.raises(ValueError):
        window_since("not-a-date", now=NOW)


def test_window_today_week_month_presets_are_half_open_intervals() -> None:
    today = window_today(now=NOW)
    week = window_week(now=NOW)
    month = window_month(now=NOW)
    for w in (today, week, month):
        assert w.start is not None
        assert w.end is not None
        assert w.start < w.end
    # "today" is nested inside "week" is nested inside "month" for this fixture.
    assert week.start <= today.start
    assert today.end <= week.end
    assert month.start <= week.start or month.start <= today.start


# ── Timestamp parsing ─────────────────────────────────────────────────────────


def test_parse_timestamp_accepts_float_and_int() -> None:
    assert parse_timestamp(123.5) == 123.5
    assert parse_timestamp(123) == 123.0


def test_parse_timestamp_accepts_iso_string() -> None:
    ts = parse_timestamp("2026-07-13T09:00:00")
    assert ts == pytest.approx(datetime(2026, 7, 13, 9, 0, 0).timestamp())


def test_parse_timestamp_none_and_empty() -> None:
    assert parse_timestamp(None) is None
    assert parse_timestamp("") is None
    assert parse_timestamp("not-a-timestamp") is None


# ── Misc ──────────────────────────────────────────────────────────────────────


def test_rollup_rejects_unknown_group_by() -> None:
    with pytest.raises(ValueError, match="unknown group_by"):
        rollup(ALL_LEGS, group_by="bogus", window=window_today(now=NOW))


def test_group_rollup_default_pricing_when_omitted() -> None:
    # rollup() must not require a PricingConfig -- the module-level default
    # (coord.config.PricingConfig()) is used when omitted.
    result = rollup(ALL_LEGS, group_by="repo", window=window_today(now=NOW))
    assert isinstance(result.total, GroupRollup)
    assert result.total.legs == 6


def test_aggregate_by_issue_collides_across_repos_tracked_limitation() -> None:
    """KNOWN LIMITATION (#1118 review, tracked not fixed): ``aggregate()``'s
    ``by="issue"`` key is the bare ``issue_number`` int, unlike the internal
    :func:`rollup` path which scopes by :class:`IssueKey` (repo + issue).
    GitHub issue numbers are per-repo, not globally unique, and
    ``coordinator.yml`` is explicitly multi-repo, so two different repos'
    issue ``#N`` silently merge into one group here.

    This pins the *current, intentional-per-sealed-contract* behavior (see
    ``_agg_key``'s docstring in ``coord/usage_rollup.py``) so it's visible
    and won't be "fixed" by accident without also updating the sealed
    Gate-A acceptance contract (``tests/acceptance/ms-37/contract.md``,
    which is read-only for workers) that pins bare-int keys.
    """
    same_number_other_repo = dict(L1, issue_number=502, repo_name="gamma")
    rows = [L1, L3, same_number_other_repo]  # L1: alpha#501, L3: beta#502
    result = aggregate(
        rows,
        by="issue",
        window=window_today(now=NOW),
        pricing={"sonnet": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_creation": 3.75},
                 "opus": {"input": 15.0, "output": 75.0, "cache_read": 1.5, "cache_creation": 18.75}},
    )
    groups_by_key = {g["key"]: g for g in result["groups"]}
    # alpha#501 stays isolated (no collision)...
    assert groups_by_key[501]["legs"] == 1
    # ...but beta#502 and gamma#502 are merged into a single "502" group,
    # even though they're different repos' issues -- the tracked bug.
    assert groups_by_key[502]["legs"] == 2
    repos_in_502_group = {row["repo_name"] for row in groups_by_key[502]["rows"]}
    assert repos_in_502_group == {"beta", "gamma"}
