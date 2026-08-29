"""Tests for the report engine (#1742) — `coord/reports.py`, `coord report`,
and the daemon's `GET /report` + `GET /report/{report_id}`.

Three layers, tested at the seam each one actually owns:

* ``fold_issue_activity`` is **pure** (fixture events, explicit window, no
  clock), so every derivation — started_at / fix_iterations / verdict order /
  outcome / the anomaly notes — is asserted here without a DB or a daemon.
* pagination is asserted against a *fake paged source*, not by inspection:
  the audit read path hard-caps a single call at 500 rows, and a busy 13h
  window exceeds that.
* the CLI and the two endpoints are black-boxed (``CliRunner`` / Starlette
  ``TestClient``) against seeded ``audit_log`` rows, including the "running a
  report does not mutate the board" invariant.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner
from starlette.testclient import TestClient

from coord import state
from coord.audit import record_audit
from coord.cli import main
from coord.config import load as load_config
from coord.dao import SqliteStore
from coord.db import _ensure_schema
from coord.drive_queue import QUEUE_ALERT_ISSUE, QUEUE_ALERT_REPO
from coord.gate_a import park_marker
from coord.models import POLICY_REFUSAL_MARKER
from coord.reports import (
    COMPLETED_COLUMNS,
    REPORTS,
    TREND_COLUMN_META,
    TREND_COLUMNS,
    TREND_RANGE_CHOICES,
    TREND_TRAILING_BUCKETS,
    ColumnMeta,
    ReportError,
    ReportResult,
    UnknownReportError,
    catalogue,
    csv_filename,
    detect_prior_activity,
    fetch_audit_window,
    find_decision,
    fold_completed,
    fold_decisions,
    fold_deprecated_routes,
    fold_drive_queue_status,
    fold_issue_activity,
    fold_queue_outcomes,
    fold_trend,
    parse_duration,
    resolve_params,
    resolve_queue_outcomes_window,
    resolve_trend_range,
    result_to_csv,
    run_completed,
    run_decisions,
    run_deprecated_routes,
    run_drive_queue_status,
    run_queue_outcomes,
    run_report,
    run_trend,
)
from coord.serve_app import build_app

# A stable window: 2026-08-02 20:16Z → 2026-08-03 09:16Z, the known-good 13h
# window from the issue. Expressed as bare epoch floats so nothing here needs
# a clock.
T0 = 1_785_000_000.0
WINDOW = (T0, T0 + 13 * 3600)


def _ev(
    ts_offset: float,
    category: str,
    event_type: str,
    *,
    repo: str = "api",
    issue: int | None = 1,
    machine: str | None = None,
    details: dict | None = None,
    entry_id: int | None = None,
) -> dict:
    """One audit entry in the shape ``query_audit_log`` returns."""
    return {
        "id": int(ts_offset) if entry_id is None else entry_id,
        "ts": T0 + ts_offset,
        "tier": "business",
        "category": category,
        "event_type": event_type,
        "actor": "drive",
        "repo": repo,
        "issue": issue,
        "assignment_id": None,
        "machine": machine,
        "summary": f"{category}/{event_type}",
        "details": details,
    }


# ── the pure fold ──────────────────────────────────────────────────────────


class TestFoldIssueActivity:
    def test_one_row_per_issue_with_every_derived_field(self) -> None:
        entries = [
            _ev(10, "drive", "drive_started"),
            _ev(20, "dispatch", "dispatched", machine="precision", details={"type": "work"}),
            _ev(30, "test", "test_failed"),
            _ev(40, "dispatch", "dispatched", machine="dellserver", details={"type": "work"}),
            _ev(50, "test", "test_passed"),
            _ev(60, "review", "review_request-changes"),
            _ev(70, "review", "review_approve"),
            _ev(80, "merge", "merged"),
            _ev(90, "drive", "drive_exited", details={"exit_code": 0, "reason": "ok"}),
            # A second issue, so grouping is actually exercised.
            _ev(15, "dispatch", "dispatched", issue=2, details={"type": "work"}),
        ]
        result = fold_issue_activity(entries, WINDOW)

        assert result.report_id == "issue-activity"
        assert result.window == WINDOW
        # Pure: no clock reached; generated_at defaults to the window end.
        assert result.generated_at == WINDOW[1]

        row = next(r for r in result.rows if r["issue"] == 1)
        assert row["repo"] == "api"
        assert row["started_at"] == T0 + 10
        assert row["started_before_window"] is False
        assert row["machines"] == ["precision", "dellserver"]
        assert row["fix_iterations"] == 1  # two work dispatches, one is the first
        assert row["test_verdicts"] == ["failed", "passed"]
        assert row["review_verdicts"] == ["request-changes", "approve"]
        assert row["merged_at"] == T0 + 80
        assert row["drive_exit"] == {"at": T0 + 90, "exit_code": 0, "reason": "ok"}
        assert row["outcome"] == "merged"

        assert {r["issue"] for r in result.rows} == {1, 2}

    def test_columns_are_the_wire_contract(self) -> None:
        result = fold_issue_activity([_ev(10, "merge", "merged")], WINDOW)
        assert result.columns == [
            "repo", "issue", "title", "started_at", "machines",
            "fix_iterations", "test_verdicts", "review_verdicts",
            "merged_at", "drive_exit", "outcome",
        ]
        # Every declared column is present on every row.
        for row in result.rows:
            for column in result.columns:
                assert column in row

    def test_drive_started_wins_over_dispatch_for_started_at(self) -> None:
        entries = [
            _ev(5, "drive", "drive_started"),
            _ev(9, "dispatch", "dispatched", details={"type": "work"}),
        ]
        row = fold_issue_activity(entries, WINDOW).rows[0]
        assert row["started_at"] == T0 + 5

    def test_dispatch_alone_sets_started_at(self) -> None:
        row = fold_issue_activity(
            [_ev(7, "dispatch", "dispatched", details={"type": "work"})], WINDOW
        ).rows[0]
        assert row["started_at"] == T0 + 7
        assert row["started_before_window"] is False
        assert row["fix_iterations"] == 0

    def test_started_before_window_is_null_start_not_a_bogus_one(self) -> None:
        """An issue with in-window activity whose first dispatch predates the
        window must NOT report the first in-window event as its start."""
        entries = [
            _ev(120, "test", "test_passed"),
            _ev(300, "review", "review_approve"),
            _ev(600, "merge", "merged"),
        ]
        row = fold_issue_activity(entries, WINDOW).rows[0]
        assert row["started_at"] is None
        assert row["started_before_window"] is True
        # Nothing in-window is the first dispatch, so no fix iterations either.
        assert row["fix_iterations"] == 0

    def test_first_in_window_dispatch_is_the_start_and_the_rest_are_fixes(self) -> None:
        """Documented limitation: the fold sees only in-window events, so the
        first in-window work dispatch IS the start as far as the window is
        concerned — ``started_before_window`` fires only when the window
        contains no start event at all."""
        entries = [
            _ev(100, "dispatch", "dispatched", details={"type": "work"}),
            _ev(200, "dispatch", "dispatched", details={"type": "work"}),
            _ev(300, "dispatch", "dispatched", details={"type": "work"}),
        ]
        row = fold_issue_activity(entries, WINDOW).rows[0]
        assert row["started_at"] == T0 + 100
        assert row["started_before_window"] is False
        assert row["fix_iterations"] == 2

    def test_review_and_smoke_dispatches_are_not_fix_iterations(self) -> None:
        entries = [
            _ev(10, "dispatch", "dispatched", details={"type": "work"}),
            _ev(20, "dispatch", "dispatched", details={"type": "review"}),
            _ev(30, "dispatch", "dispatched", details={"type": "smoke"}),
        ]
        row = fold_issue_activity(entries, WINDOW).rows[0]
        assert row["fix_iterations"] == 0

    def test_entries_may_arrive_newest_first(self) -> None:
        """The audit read path is newest-first; ordered lists must still come
        out in chronological order."""
        entries = [
            _ev(70, "review", "review_approve"),
            _ev(60, "review", "review_request-changes"),
            _ev(30, "test", "test_failed"),
            _ev(50, "test", "test_passed"),
        ]
        row = fold_issue_activity(entries, WINDOW).rows[0]
        assert row["test_verdicts"] == ["failed", "passed"]
        assert row["review_verdicts"] == ["request-changes", "approve"]

    def test_titles_are_injected_not_guessed(self) -> None:
        result = fold_issue_activity(
            [_ev(10, "merge", "merged")], WINDOW, titles={("api", 1): "Fix the thing"}
        )
        assert result.rows[0]["title"] == "Fix the thing"

    def test_missing_title_is_none_not_an_error(self) -> None:
        assert fold_issue_activity([_ev(10, "merge", "merged")], WINDOW).rows[0]["title"] is None

    def test_events_without_repo_or_issue_are_noted_not_silently_dropped(self) -> None:
        entries = [
            _ev(10, "merge", "merged"),
            _ev(20, "housekeeping", "sweep", repo=None, issue=None),
            _ev(21, "housekeeping", "sweep", repo="api", issue=None),
        ]
        result = fold_issue_activity(entries, WINDOW)
        assert len(result.rows) == 1
        assert any("carry no repo/issue" in n for n in result.notes)
        assert any("2 event(s)" in n for n in result.notes)

    def test_counts_partial_defaults_to_false(self) -> None:
        """Regression guard: with the default empty ``prior_activity``, every
        row carries the new ``counts_partial`` key (additive) but it is
        always False — behaviour is unchanged from before #1760."""
        row = fold_issue_activity(
            [_ev(10, "dispatch", "dispatched", details={"type": "work"})], WINDOW
        ).rows[0]
        assert row["counts_partial"] is False


class TestPriorActivity:
    """#1760: the caller-supplied ``prior_activity`` set is the fold's only
    way to learn about events outside its own window.  These mirror the
    issue's own reproduction — claude-coordinator#1629, where the original
    dispatch predates the window but a fix-1 dispatch (and its review cycle)
    falls inside it."""

    def test_prior_activity_issue_reports_no_start_and_partial_counts(self) -> None:
        """Acceptance criterion #1 verbatim: an in-window work dispatch is
        present, but prior_activity says the issue really started earlier —
        the row must not claim that in-window dispatch as the start."""
        entries = [
            _ev(100, "dispatch", "dispatched", repo="claude-coordinator", issue=1629,
                details={"type": "work"}),
            _ev(110, "review", "review_approve", repo="claude-coordinator", issue=1629),
        ]
        result = fold_issue_activity(
            entries, WINDOW, prior_activity=frozenset({("claude-coordinator", 1629)})
        )
        row = result.rows[0]
        assert row["started_at"] is None
        assert row["started_before_window"] is True
        assert row["counts_partial"] is True

    def test_empty_prior_activity_is_a_no_op(self) -> None:
        """Acceptance criterion #2: same entries, empty prior_activity (the
        default) — behaviour is exactly what it was before #1760."""
        entries = [
            _ev(100, "dispatch", "dispatched", repo="claude-coordinator", issue=1629,
                details={"type": "work"}),
            _ev(110, "review", "review_approve", repo="claude-coordinator", issue=1629),
        ]
        row = fold_issue_activity(entries, WINDOW).rows[0]
        assert row["started_at"] == T0 + 100
        assert row["started_before_window"] is False
        assert row["counts_partial"] is False

    def test_every_in_window_dispatch_is_a_fix_when_prior_activity_is_known(self) -> None:
        """Acceptance criterion #3: with prior_activity set, ONE in-window
        work dispatch is already a re-dispatch (fix_iterations == 1), not
        the "first dispatch" that the no-prior-activity fold would treat it
        as (which would report fix_iterations == 0)."""
        entries = [
            _ev(100, "dispatch", "dispatched", details={"type": "work"}),
        ]
        row = fold_issue_activity(
            entries, WINDOW, prior_activity=frozenset({("api", 1)})
        ).rows[0]
        assert row["fix_iterations"] == 1

    def test_multiple_in_window_dispatches_all_count_as_fixes(self) -> None:
        entries = [
            _ev(100, "dispatch", "dispatched", details={"type": "work"}),
            _ev(200, "dispatch", "dispatched", details={"type": "work"}),
            _ev(300, "dispatch", "dispatched", details={"type": "work"}),
        ]
        row = fold_issue_activity(
            entries, WINDOW, prior_activity=frozenset({("api", 1)})
        ).rows[0]
        assert row["fix_iterations"] == 3

    def test_prior_activity_overrides_an_in_window_drive_started_too(self) -> None:
        """Prior activity must win even when the window DOES contain a
        drive_started event — the design says "regardless of whether an
        in-window dispatch exists"."""
        entries = [_ev(50, "drive", "drive_started")]
        row = fold_issue_activity(
            entries, WINDOW, prior_activity=frozenset({("api", 1)})
        ).rows[0]
        assert row["started_at"] is None
        assert row["started_before_window"] is True

    def test_prior_activity_only_applies_to_the_matching_issue(self) -> None:
        entries = [
            _ev(100, "dispatch", "dispatched", issue=1, details={"type": "work"}),
            _ev(100, "dispatch", "dispatched", issue=2, details={"type": "work"}),
        ]
        result = fold_issue_activity(
            entries, WINDOW, prior_activity=frozenset({("api", 1)})
        )
        by_issue = {r["issue"]: r for r in result.rows}
        assert by_issue[1]["counts_partial"] is True
        assert by_issue[2]["counts_partial"] is False
        assert by_issue[2]["started_at"] == T0 + 100


class TestOutcome:
    def test_merged(self) -> None:
        row = fold_issue_activity([_ev(10, "merge", "merged")], WINDOW).rows[0]
        assert row["outcome"] == "merged"

    def test_failed_on_nonzero_drive_exit(self) -> None:
        entries = [_ev(10, "drive", "drive_exited", details={"exit_code": 3, "reason": "deadline"})]
        assert fold_issue_activity(entries, WINDOW).rows[0]["outcome"] == "failed"

    def test_failed_on_crash_exit_with_no_code(self) -> None:
        entries = [_ev(10, "drive", "drive_exited", details={"exit_code": None, "error": "boom"})]
        row = fold_issue_activity(entries, WINDOW).rows[0]
        assert row["outcome"] == "failed"
        assert row["drive_exit"]["reason"] == "boom"

    def test_clean_drive_exit_without_merge_is_stalled(self) -> None:
        entries = [_ev(10, "drive", "drive_exited", details={"exit_code": 0, "reason": "ok"})]
        assert fold_issue_activity(entries, WINDOW).rows[0]["outcome"] == "stalled"

    def test_recent_activity_with_no_exit_is_in_flight(self) -> None:
        # Last event 10 minutes before the window end.
        entries = [_ev(13 * 3600 - 600, "test", "test_passed")]
        assert fold_issue_activity(entries, WINDOW).rows[0]["outcome"] == "in-flight"

    def test_quiet_since_the_start_of_a_long_window_is_stalled(self) -> None:
        entries = [_ev(60, "test", "test_passed")]
        assert fold_issue_activity(entries, WINDOW).rows[0]["outcome"] == "stalled"


class TestNotes:
    def test_nonzero_exit_but_merged_produces_a_note_naming_both_timestamps(self) -> None:
        """The #1631 case: the driver exited 1 with "merge attempted 3 times
        without landing", and the merge landed 13 minutes later anyway."""
        exit_ts_offset = 3600.0
        entries = [
            _ev(
                exit_ts_offset, "drive", "drive_exited", issue=1631,
                details={"exit_code": 1, "reason": "merge attempted 3 times without landing"},
            ),
            _ev(exit_ts_offset + 13 * 60, "merge", "merged", issue=1631),
        ]
        result = fold_issue_activity(entries, WINDOW)

        row = result.rows[0]
        assert row["outcome"] == "merged"

        note = next(n for n in result.notes if "1631" in n)
        assert "api#1631" in note
        assert "exit_code=1" in note
        # Both timestamps, spelled out.
        assert "2026-08-02" in note or "Z" in note
        from coord.reports import _iso

        assert _iso(T0 + exit_ts_offset) in note
        assert _iso(T0 + exit_ts_offset + 13 * 60) in note
        assert "merge attempted 3 times without landing" in note

    def test_clean_exit_and_merge_produces_no_anomaly_note(self) -> None:
        entries = [
            _ev(100, "drive", "drive_exited", details={"exit_code": 0, "reason": "ok"}),
            _ev(200, "merge", "merged"),
        ]
        assert fold_issue_activity(entries, WINDOW).notes == []

    def test_merged_with_a_failing_last_test_verdict_is_flagged(self) -> None:
        entries = [
            _ev(100, "test", "test_passed"),
            _ev(200, "test", "test_failed"),
            _ev(300, "merge", "merged"),
        ]
        notes = fold_issue_activity(entries, WINDOW).notes
        assert any("still 'failed'" in n for n in notes)

    def test_three_or_more_fix_iterations_is_flagged(self) -> None:
        entries = [
            _ev(10 * i, "dispatch", "dispatched", details={"type": "work"})
            for i in range(1, 5)
        ]
        notes = fold_issue_activity(entries, WINDOW).notes
        assert any("fix iterations" in n for n in notes)

    def test_truncation_note_is_explicit(self) -> None:
        result = fold_issue_activity([_ev(10, "merge", "merged")], WINDOW, truncated=True)
        assert result.notes
        assert result.notes[0].startswith("TRUNCATED:")

    def test_counts_partial_row_gets_a_lower_bound_note_naming_the_issue(self) -> None:
        """Acceptance criterion: every row with counts_partial produces a
        notes entry naming the issue and stating the counts are lower
        bounds."""
        entries = [
            _ev(100, "dispatch", "dispatched", repo="claude-coordinator", issue=1629,
                details={"type": "work"}),
        ]
        result = fold_issue_activity(
            entries, WINDOW, prior_activity=frozenset({("claude-coordinator", 1629)})
        )
        note = next(n for n in result.notes if "claude-coordinator#1629" in n)
        assert "lower bound" in note.lower()

    def test_request_changes_with_zero_fix_iterations_is_a_contradiction(self) -> None:
        """Acceptance criterion: a request-changes review verdict with
        fix_iterations == 0 and counts_partial False is not reachable in a
        correct fold — flag it rather than print it deadpan."""
        entries = [
            _ev(10, "review", "review_request-changes"),
        ]
        result = fold_issue_activity(entries, WINDOW)
        row = result.rows[0]
        assert row["fix_iterations"] == 0
        assert row["counts_partial"] is False
        note = next(n for n in result.notes if "api#1" in n)
        assert "inconsistent" in note or "should not happen" in note

    def test_request_changes_with_zero_fix_iterations_is_not_contradiction_when_partial(
        self,
    ) -> None:
        """The same shape is expected (not an error) when counts_partial is
        True — a review from before the window's known start doesn't
        contradict a fix count the row already admits is a lower bound."""
        entries = [
            _ev(10, "review", "review_request-changes"),
        ]
        result = fold_issue_activity(
            entries, WINDOW, prior_activity=frozenset({("api", 1)})
        )
        assert not any(
            "inconsistent" in n or "should not happen" in n for n in result.notes
        )

    def test_request_changes_with_a_real_fix_iteration_is_not_flagged(self) -> None:
        entries = [
            _ev(10, "dispatch", "dispatched", details={"type": "work"}),
            _ev(20, "review", "review_request-changes"),
            _ev(30, "dispatch", "dispatched", details={"type": "work"}),
        ]
        result = fold_issue_activity(entries, WINDOW)
        assert not any(
            "inconsistent" in n or "should not happen" in n for n in result.notes
        )


# ── pagination ─────────────────────────────────────────────────────────────


class _FakePagedSource:
    """A fake audit source that hands back fixed-size pages with a keyset
    cursor, exactly like ``query_audit_log``."""

    def __init__(self, entries: list[dict], page_size: int, *, drop_cursor: bool = False):
        self.entries = entries
        self.page_size = page_size
        self.drop_cursor = drop_cursor
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        cursor = kwargs.get("cursor")
        start = int(cursor) if cursor else 0
        page = self.entries[start : start + self.page_size]
        end = start + len(page)
        has_more = end < len(self.entries)
        return {
            "entries": page,
            "has_more": has_more,
            "next_cursor": None if (self.drop_cursor or not has_more) else str(end),
        }


class TestPagination:
    def test_window_spanning_multiple_pages_is_fully_covered(self) -> None:
        entries = [_ev(i, "test", "test_passed", entry_id=i) for i in range(1250)]
        source = _FakePagedSource(entries, page_size=500)

        fetched, truncated = fetch_audit_window(
            since=WINDOW[0], until=WINDOW[1], fetch=source
        )
        assert len(fetched) == 1250
        assert truncated is False
        # 3 pages: 500 + 500 + 250.
        assert len(source.calls) == 3
        assert source.calls[0]["cursor"] is None
        assert source.calls[1]["cursor"] == "500"
        assert source.calls[2]["cursor"] == "1000"

    def test_single_page_window_does_not_paginate(self) -> None:
        source = _FakePagedSource([_ev(1, "merge", "merged")], page_size=500)
        fetched, truncated = fetch_audit_window(since=WINDOW[0], until=WINDOW[1], fetch=source)
        assert len(fetched) == 1
        assert truncated is False
        assert len(source.calls) == 1

    def test_page_cap_sets_truncated(self) -> None:
        entries = [_ev(i, "test", "test_passed", entry_id=i) for i in range(50)]
        source = _FakePagedSource(entries, page_size=10)
        fetched, truncated = fetch_audit_window(
            since=WINDOW[0], until=WINDOW[1], fetch=source, max_pages=2
        )
        assert len(fetched) == 20
        assert truncated is True

    def test_has_more_without_a_cursor_sets_truncated(self) -> None:
        entries = [_ev(i, "test", "test_passed", entry_id=i) for i in range(50)]
        source = _FakePagedSource(entries, page_size=10, drop_cursor=True)
        fetched, truncated = fetch_audit_window(
            since=WINDOW[0], until=WINDOW[1], fetch=source
        )
        assert len(fetched) == 10
        assert truncated is True

    def test_repo_filter_is_pushed_down_to_the_source(self) -> None:
        source = _FakePagedSource([], page_size=500)
        fetch_audit_window(since=WINDOW[0], until=WINDOW[1], repo="api", fetch=source)
        assert source.calls[0]["repo"] == "api"
        assert source.calls[0]["since"] == WINDOW[0]
        assert source.calls[0]["until"] == WINDOW[1]

    def test_truncated_fetch_surfaces_as_a_note_in_the_run(self) -> None:
        from coord.reports import run_issue_activity

        entries = [_ev(i, "merge", "merged", issue=i, entry_id=i) for i in range(30)]
        source = _FakePagedSource(entries, page_size=10, drop_cursor=True)
        result = run_issue_activity(
            since="13h", now=WINDOW[1], fetch=source, title_lookup=lambda keys: {}
        )
        assert any(n.startswith("TRUNCATED:") for n in result.notes)


# ── prior-activity look-back (#1760) ────────────────────────────────────────


class _CountingIssueSource:
    """A fake audit source keyed by ``(repo, issue)`` — records every call so
    a test can assert the look-back issues exactly one query per issue, not
    one per event and not an unbounded scan."""

    def __init__(self, entries: list[dict]):
        self.entries = entries
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        repo = kwargs.get("repo")
        issue = kwargs.get("issue")
        until = kwargs.get("until")
        limit = kwargs.get("limit") or 500
        matches = [
            e
            for e in self.entries
            if e.get("repo") == repo
            and e.get("issue") == issue
            and (until is None or float(e["ts"]) <= until)
        ]
        matches.sort(key=lambda e: (-float(e["ts"]), -int(e["id"])))
        return {
            "entries": matches[:limit],
            "has_more": len(matches) > limit,
            "next_cursor": None,
        }


class TestDetectPriorActivity:
    def test_one_query_per_issue_not_per_event(self) -> None:
        # Four issues in the result set (the issue's own "4 issues for a 13h
        # window" example) — the look-back must not touch each issue's
        # events one at a time.
        prior_entries = [
            _ev(-100, "dispatch", "dispatched", issue=1, entry_id=901, details={"type": "work"}),
            _ev(-90, "dispatch", "dispatched", issue=1, entry_id=902, details={"type": "work"}),
            _ev(-50, "dispatch", "dispatched", issue=2, entry_id=903, details={"type": "work"}),
        ]
        source = _CountingIssueSource(prior_entries)
        keys = [("api", 1), ("api", 2), ("api", 3), ("api", 4)]

        result = detect_prior_activity(keys, until=T0, fetch=source)

        assert len(source.calls) == 4
        assert result == frozenset({("api", 1), ("api", 2)})

    def test_no_prior_activity_is_empty(self) -> None:
        source = _CountingIssueSource([])
        assert detect_prior_activity([("api", 1)], until=T0, fetch=source) == frozenset()
        assert len(source.calls) == 1

    def test_duplicate_keys_still_issue_one_query_each(self) -> None:
        source = _CountingIssueSource([])
        detect_prior_activity([("api", 1), ("api", 1), ("api", 2)], until=T0, fetch=source)
        assert len(source.calls) == 2

    def test_query_is_bounded_at_the_window_start_with_limit_one(self) -> None:
        source = _CountingIssueSource([])
        detect_prior_activity([("api", 1)], until=T0, fetch=source)
        call = source.calls[0]
        assert call["until"] == T0
        assert call["limit"] == 1
        assert call["repo"] == "api"
        assert call["issue"] == 1

    def test_events_after_the_window_start_do_not_count_as_prior(self) -> None:
        source = _CountingIssueSource(
            [_ev(50, "dispatch", "dispatched", issue=1, details={"type": "work"})]
        )
        # The only event for issue 1 is AFTER `until` (T0), so it must not
        # register as prior activity.
        assert detect_prior_activity([("api", 1)], until=T0, fetch=source) == frozenset()


# ── drive-queue-status (#1805) ──────────────────────────────────────────────


def _dq_row(
    issue: int,
    *,
    repo: str = "api",
    position: int = 0,
    machine: str | None = None,
    after_json: list | None = None,
    state_: str = "waiting",
    attempts: int = 0,
    deferrals: int = 0,
    last_reason: str = "",
    reason_at: float | None = None,
    session_name: str = "",
    launched_at: float | None = None,
    enqueued_at: float = 100.0,
    hold_state: str = "",
    hold_reason: str = "",
    resume_when: str = "",
    hold_scope: str = "entry",
) -> dict:
    """One row in the exact shape ``coord.state.list_drive_queue`` returns —
    raw column names (``repo_name``/``issue_number``/``state``), ``after_json``
    already decoded to a list."""
    return {
        "id": issue,
        "repo_name": repo,
        "issue_number": issue,
        "position": position,
        "machine": machine,
        "after_json": list(after_json or []),
        "state": state_,
        "attempts": attempts,
        "deferrals": deferrals,
        "last_reason": last_reason,
        "reason_at": reason_at,
        "session_name": session_name,
        "launched_at": launched_at,
        "enqueued_at": enqueued_at,
        "hold_after": 0,
        "hold_reason": hold_reason,
        "resume_when": resume_when,
        "hold_state": hold_state,
        "hold_probes": 0,
        "hold_scope": hold_scope,
    }


class TestFoldDriveQueueStatus:
    """Pure fold — fixture rows in, ``ReportResult`` out.  No DB, no daemon."""

    def test_empty_queue_is_not_an_error(self) -> None:
        result = fold_drive_queue_status([], 1000.0)
        assert result.rows == []
        assert result.notes == ["The drive queue is empty."]
        assert result.report_id == "drive-queue-status"
        assert result.window == (1000.0, 1000.0)

    def test_window_is_degenerate_generated_at_generated_at(self) -> None:
        result = fold_drive_queue_status([_dq_row(1)], 4242.0)
        assert result.generated_at == 4242.0
        assert result.window == (4242.0, 4242.0)

    def test_column_meta_matches_columns_one_to_one_same_order(self) -> None:
        result = fold_drive_queue_status([_dq_row(1)], 1000.0)
        assert [m.id for m in result.column_meta] == result.columns
        assert result.columns == [
            "position", "repo", "issue", "title", "state", "machine",
            "attempts", "deferrals", "last_reason", "reason_at", "enqueued_at",
            "launched_at", "hold_state", "after",
        ]

    def test_mixed_states_counted_in_notes(self) -> None:
        # 4 rows total, but only 3 are non-terminal (1 running + 2 waiting).
        # The headline must count those 3, not all 4 (#1855) — and the
        # `blocked` entry must be named, not silently folded away.
        rows = [
            _dq_row(1, position=0, state_="running"),
            _dq_row(2, position=1, state_="waiting"),
            _dq_row(3, position=2, state_="waiting"),
            _dq_row(4, position=3, state_="blocked"),
        ]
        result = fold_drive_queue_status(rows, 1000.0)
        assert len(result.rows) == 4
        assert any(
            "3 entries queued" in n and "1 running" in n and "2 waiting" in n and "1 blocked" in n
            for n in result.notes
        )
        assert not any("4 entries queued" in n for n in result.notes)

    def test_headline_count_is_non_terminal_entries_only(self) -> None:
        # 8 non-terminal (1 running, 7 waiting) + 11 done == 19 rows total,
        # matching the issue's own reproduction (#1855): the headline must
        # read 8, never 19.
        rows = (
            [_dq_row(1, position=0, state_="running")]
            + [_dq_row(n, position=n, state_="waiting") for n in range(2, 9)]
            + [_dq_row(n, position=n, state_="done") for n in range(9, 20)]
        )
        result = fold_drive_queue_status(rows, 1000.0)
        assert len(result.rows) == 19
        headline = next(n for n in result.notes if "queued" in n)
        assert "8 entries queued" in headline
        assert "11 done" in headline

    def test_blocked_named_in_summary(self) -> None:
        rows = [_dq_row(1, state_="waiting"), _dq_row(2, state_="blocked")]
        result = fold_drive_queue_status(rows, 1000.0)
        headline = next(n for n in result.notes if "queued" in n)
        assert "1 blocked" in headline

    def test_failed_named_in_summary(self) -> None:
        rows = [_dq_row(1, state_="waiting"), _dq_row(2, state_="failed")]
        result = fold_drive_queue_status(rows, 1000.0)
        headline = next(n for n in result.notes if "queued" in n)
        assert "1 failed" in headline

    def test_zero_count_states_do_not_appear(self) -> None:
        rows = [_dq_row(1, state_="waiting")]
        result = fold_drive_queue_status(rows, 1000.0)
        headline = next(n for n in result.notes if "queued" in n)
        assert "running" not in headline
        assert "blocked" not in headline
        assert "failed" not in headline
        assert "done" not in headline

    def test_all_done_queue_does_not_read_as_entries_queued(self) -> None:
        rows = [_dq_row(n, state_="done") for n in range(1, 4)]
        result = fold_drive_queue_status(rows, 1000.0)
        headline = next(n for n in result.notes if "queued" in n)
        assert "3 entries queued" not in headline
        assert "0 entries queued" in headline
        assert "3 done" in headline

    def test_run_order_preserved_from_input_not_resorted(self) -> None:
        # list_drive_queue already returns ORDER BY position, id — the fold
        # must not reorder it.
        rows = [_dq_row(3, position=2), _dq_row(1, position=0), _dq_row(2, position=1)]
        result = fold_drive_queue_status(rows, 1000.0)
        assert [r["issue"] for r in result.rows] == [3, 1, 2]

    def test_attempts_ge_1_named_in_notes(self) -> None:
        rows = [
            _dq_row(1, attempts=0),
            _dq_row(2, attempts=1, last_reason="launch failed, retrying"),
            _dq_row(3, attempts=3),
        ]
        result = fold_drive_queue_status(rows, 1000.0)
        tell = next(n for n in result.notes if n.startswith("attempts>=1"))
        assert "api#2 (attempts=1)" in tell
        assert "api#3 (attempts=3)" in tell
        assert "api#1" not in tell

    def test_no_attempts_note_when_all_zero(self) -> None:
        result = fold_drive_queue_status([_dq_row(1, attempts=0)], 1000.0)
        assert not any(n.startswith("attempts>=1") for n in result.notes)

    def test_unpinned_machine_is_empty_string_not_none(self) -> None:
        result = fold_drive_queue_status([_dq_row(1, machine=None)], 1000.0)
        assert result.rows[0]["machine"] == ""

    def test_title_lookup_applied_missing_title_is_none(self) -> None:
        rows = [_dq_row(1), _dq_row(2)]
        result = fold_drive_queue_status(rows, 1000.0, titles={("api", 1): "Fix the thing"})
        by_issue = {r["issue"]: r for r in result.rows}
        assert by_issue[1]["title"] == "Fix the thing"
        assert by_issue[2]["title"] is None

    def test_after_is_a_list_column(self) -> None:
        result = fold_drive_queue_status(
            [_dq_row(2, after_json=["api#1"])], 1000.0
        )
        assert result.rows[0]["after"] == ["api#1"]

    def test_extra_keys_beyond_columns_are_present(self) -> None:
        result = fold_drive_queue_status(
            [_dq_row(1, session_name="s1", hold_reason="deploy gate", resume_when="ready")],
            1000.0,
        )
        row = result.rows[0]
        assert row["session_name"] == "s1"
        assert row["hold_reason"] == "deploy gate"
        assert row["resume_when"] == "ready"

    def test_hold_scope_is_present_and_defaults_to_entry(self) -> None:
        """#2186: a report consumer needs to tell "this entry alone is
        held" from "the whole queue stopped" — the same blind spot the TUI
        had before its own #2186 fix."""
        result = fold_drive_queue_status([_dq_row(1)], 1000.0)
        assert result.rows[0]["hold_scope"] == "entry"

    def test_hold_scope_fleet_passes_through(self) -> None:
        result = fold_drive_queue_status(
            [_dq_row(1, hold_scope="fleet")], 1000.0
        )
        assert result.rows[0]["hold_scope"] == "fleet"

    def test_hold_scope_fails_closed_to_entry_on_garbage(self) -> None:
        """Mirrors `QueueEntry._normalize_hold_scope`: anything other than
        the literal `"fleet"` — a row predating the column, or a value this
        build has never heard of — reads as the narrower `"entry"`, never
        silently as a fleet-wide stop."""
        result = fold_drive_queue_status(
            [_dq_row(1, hold_scope="something-unexpected")], 1000.0
        )
        assert result.rows[0]["hold_scope"] == "entry"

    def test_standing_queue_escalation_surfaced_in_notes_when_present(self) -> None:
        result = fold_drive_queue_status(
            [_dq_row(1)],
            1000.0,
            queue_escalation={"stage": "blocked", "reason": "3 entries stuck"},
        )
        assert any("3 entries stuck" in n for n in result.notes)

    def test_no_escalation_note_when_none(self) -> None:
        result = fold_drive_queue_status([_dq_row(1)], 1000.0, queue_escalation=None)
        assert not any("escalation" in n for n in result.notes)


class TestRunDriveQueueStatus:
    """The runner — fetch=/now=/title_lookup=/escalation_lookup= seams, the
    same test-seam shape as ``run_issue_activity``'s ``fetch=``."""

    def test_repo_param_forwarded_to_fetch(self) -> None:
        calls: list[str | None] = []

        def fetch(repo):
            calls.append(repo)
            return []

        run_drive_queue_status(
            repo="api", now=1000.0, fetch=fetch,
            title_lookup=lambda keys: {}, escalation_lookup=lambda: None,
        )
        assert calls == ["api"]

    def test_empty_repo_param_means_no_filter(self) -> None:
        calls: list[str | None] = []

        def fetch(repo):
            calls.append(repo)
            return []

        run_drive_queue_status(
            repo="", now=1000.0, fetch=fetch,
            title_lookup=lambda keys: {}, escalation_lookup=lambda: None,
        )
        assert calls == [None]

    def test_now_seam_sets_generated_at_and_window(self) -> None:
        result = run_drive_queue_status(
            now=555.0, fetch=lambda repo: [],
            title_lookup=lambda keys: {}, escalation_lookup=lambda: None,
        )
        assert result.generated_at == 555.0
        assert result.window == (555.0, 555.0)

    def test_title_lookup_receives_keys_from_fetched_rows(self) -> None:
        seen_keys: set = set()

        def title_lookup(keys):
            seen_keys.update(keys)
            return {}

        run_drive_queue_status(
            now=1000.0,
            fetch=lambda repo: [_dq_row(7, repo="web"), _dq_row(9, repo="web")],
            title_lookup=title_lookup,
            escalation_lookup=lambda: None,
        )
        assert seen_keys == {("web", 7), ("web", 9)}

    def test_rows_fold_through_from_injected_fetch(self) -> None:
        result = run_drive_queue_status(
            now=1000.0,
            fetch=lambda repo: [_dq_row(1, state_="running", attempts=2)],
            title_lookup=lambda keys: {},
            escalation_lookup=lambda: None,
        )
        assert len(result.rows) == 1
        assert result.rows[0]["state"] == "running"
        assert result.rows[0]["attempts"] == 2


# ── deprecated-routes (#1945): evidence for RPC retirement ────────────────

_DEP_ROUTES = {"/old-a": "PATCH /new-a", "/old-b": "PATCH /new-b"}


def _dep_entry(
    ts: float, route: str, *, client: str = "coord-py", version: str = "1.0"
) -> dict:
    return {
        "ts": ts,
        "details": {"route": route, "client": client, "client_version": version},
    }


class TestFoldDeprecatedRoutes:
    """Pure fold — fixture audit entries in, ``ReportResult`` out."""

    def test_no_entries_at_all_is_no_data_for_every_route(self) -> None:
        result = fold_deprecated_routes([], 1000.0, routes=_DEP_ROUTES)
        assert result.report_id == "deprecated-routes"
        assert {r["route"]: r["status"] for r in result.rows} == {
            "/old-a": "no_data", "/old-b": "no_data",
        }
        assert any("no_data" in n or "UNKNOWN" in n for n in result.notes)

    def test_calls_for_one_route_do_not_make_a_silent_route_zero_calls_wrongly(
        self,
    ) -> None:
        """Once ANY deprecation row exists anywhere, telemetry is proven
        live, so a route with none of its own reads as the real, actionable
        `zero_calls` — not the ambiguous `no_data`."""
        result = fold_deprecated_routes(
            [_dep_entry(500.0, "/old-a")], 1000.0, routes=_DEP_ROUTES
        )
        by_route = {r["route"]: r for r in result.rows}
        assert by_route["/old-a"]["status"] == "in_use"
        assert by_route["/old-b"]["status"] == "zero_calls"
        assert not any("no_data" in n for n in result.notes)

    def test_last_call_is_the_max_timestamp_and_count_is_exact(self) -> None:
        entries = [
            _dep_entry(100.0, "/old-a"),
            _dep_entry(900.0, "/old-a"),
            _dep_entry(500.0, "/old-a"),
        ]
        result = fold_deprecated_routes(entries, 1000.0, routes=_DEP_ROUTES)
        row = next(r for r in result.rows if r["route"] == "/old-a")
        assert row["last_call"] == 900.0
        assert row["call_count"] == 3

    def test_distinct_client_version_pairs_deduped_newest_first(self) -> None:
        entries = [
            _dep_entry(100.0, "/old-a", client="coord-py", version="1.0"),
            _dep_entry(300.0, "/old-a", client="coord-tui", version="2.0"),
            _dep_entry(200.0, "/old-a", client="coord-py", version="1.0"),  # dup
        ]
        result = fold_deprecated_routes(entries, 1000.0, routes=_DEP_ROUTES)
        row = next(r for r in result.rows if r["route"] == "/old-a")
        assert row["clients"] == ["coord-tui@2.0", "coord-py@1.0"]

    def test_missing_client_or_version_details_read_as_unknown(self) -> None:
        entries = [{"ts": 1.0, "details": {"route": "/old-a"}}]
        result = fold_deprecated_routes(entries, 1000.0, routes=_DEP_ROUTES)
        row = next(r for r in result.rows if r["route"] == "/old-a")
        assert row["clients"] == ["unknown@unknown"]

    def test_replacement_text_is_carried_through(self) -> None:
        result = fold_deprecated_routes([], 1000.0, routes=_DEP_ROUTES)
        row = next(r for r in result.rows if r["route"] == "/old-a")
        assert row["replacement"] == "PATCH /new-a"

    def test_column_meta_matches_columns_one_to_one_same_order(self) -> None:
        result = fold_deprecated_routes([], 1000.0, routes=_DEP_ROUTES)
        assert [m.id for m in result.column_meta] == result.columns

    def test_rows_are_sorted_by_route(self) -> None:
        result = fold_deprecated_routes([], 1000.0, routes={"/z": "r-z", "/a": "r-a"})
        assert [r["route"] for r in result.rows] == ["/a", "/z"]

    def test_window_is_degenerate_generated_at_generated_at(self) -> None:
        result = fold_deprecated_routes([], 4242.0, routes=_DEP_ROUTES)
        assert result.generated_at == 4242.0
        assert result.window == (4242.0, 4242.0)

    def test_entries_for_an_unknown_route_are_ignored_not_a_crash(self) -> None:
        entries = [_dep_entry(1.0, "/not-in-the-table")]
        result = fold_deprecated_routes(entries, 1000.0, routes=_DEP_ROUTES)
        assert all(r["call_count"] == 0 for r in result.rows)

    def test_defaults_to_the_real_rpc_superseded_by_resource_table(self) -> None:
        """No ``routes=`` override -- #1945 acceptance: every route marked
        deprecated in the OpenAPI spec is covered. `RPC_SUPERSEDED_BY_RESOURCE`
        is exactly the table #1944 stamps `deprecated: true` from, so
        matching it here (the default) means this report can never silently
        drop a route the spec calls deprecated."""
        from coord.serve_app import RPC_SUPERSEDED_BY_RESOURCE

        result = fold_deprecated_routes([], 1000.0)
        assert {r["route"] for r in result.rows} == set(RPC_SUPERSEDED_BY_RESOURCE)


class TestRunDeprecatedRoutes:
    """The runner — ``fetch=``/``now=``/``routes=`` seams."""

    def test_now_seam_sets_generated_at_and_window(self) -> None:
        result = run_deprecated_routes(
            now=555.0, fetch=lambda now: ([], False), routes=_DEP_ROUTES
        )
        assert result.generated_at == 555.0
        assert result.window == (555.0, 555.0)

    def test_fetch_receives_generated_at(self) -> None:
        seen: list[float] = []

        def fetch(now):
            seen.append(now)
            return [], False

        run_deprecated_routes(now=42.0, fetch=fetch, routes=_DEP_ROUTES)
        assert seen == [42.0]

    def test_entries_fold_through_from_injected_fetch(self) -> None:
        result = run_deprecated_routes(
            now=1000.0,
            fetch=lambda now: ([_dep_entry(500.0, "/old-a")], False),
            routes=_DEP_ROUTES,
        )
        row = next(r for r in result.rows if r["route"] == "/old-a")
        assert row["status"] == "in_use"

    def test_truncated_fetch_adds_a_note(self) -> None:
        result = run_deprecated_routes(
            now=1000.0, fetch=lambda now: ([], True), routes=_DEP_ROUTES
        )
        assert any("page cap" in n for n in result.notes)

    def test_not_truncated_adds_no_extra_note(self) -> None:
        result = run_deprecated_routes(
            now=1000.0, fetch=lambda now: ([], False), routes=_DEP_ROUTES
        )
        assert not any("page cap" in n for n in result.notes)


# ── decisions (#2369): escalations + blocked queue roots as cards ─────────


def _esc_row(
    issue: int,
    *,
    repo: str = "claude-coordinator",
    stage: str = "merge",
    reason: str = "stuck",
    gate_readings: str = "",
    proposed_command: str = "coord merge --plan --repo claude-coordinator",
    created_at: float = 500.0,
    assignment_id: str | None = None,
) -> dict:
    """One row in the exact shape ``coord.state.list_drive_escalations``
    returns."""
    return {
        "id": issue,
        "repo_name": repo,
        "issue_number": issue,
        "stage": stage,
        "assignment_id": assignment_id,
        "reason": reason,
        "gate_readings": gate_readings,
        "proposed_command": proposed_command,
        "created_at": created_at,
    }


# #2283's worked shape — a terminal acceptance-author dead end whose
# `last_reason` already embeds three de facto options.
_ACCEPTANCE_DEAD_END_REASON = (
    "acceptance author aid-1 exited DONE, but its branch acc/foo carries no "
    "commits — nothing was authored, so there is no slice to land, and DONE "
    "is terminal: it will never change on its own.\n"
    "   inspect: coord log aid-1 --machine dellserver\n"
    "   Re-author by hand: coord acceptance author api 99 --issue 1\n"
    "   or re-run coord drive with --no-acceptance to skip JIT authoring. — "
    "the board row is terminal and unactionable (nothing active, no gate "
    "transition available), which cannot change on retry (#2019); blocking "
    "without spending an attempt"
)

# coord-portal#107's worked shape — a CI-shaped merge block with the
# existing "Inspect the gates: coord merge --plan --repo ..." line.
_MERGE_ATTEMPTS_REASON = (
    "merge attempted 3 times without landing.\n"
    "   Last board state: status='CONFLICT' reason='none'\n"
    "   Last `coord merge --only` diagnostic:\n"
    "     sealed acceptance suite (ms-1): failing\n"
    "   Inspect the gates: coord merge --plan --repo coord-portal"
)


class TestFoldDecisions:
    """Pure fold — fixture escalation/queue rows in, ``ReportResult`` out.
    No DB, no daemon."""

    def test_nothing_pending_is_not_an_error(self) -> None:
        result = fold_decisions([], [], 1000.0)
        assert result.rows == []
        assert result.report_id == "decisions"
        assert result.window == (1000.0, 1000.0)
        assert any("nothing" in n.lower() for n in result.notes)

    def test_column_meta_matches_columns_one_to_one_same_order(self) -> None:
        result = fold_decisions(
            [_esc_row(1)], [], 1000.0
        )
        assert [m.id for m in result.column_meta] == result.columns
        assert result.columns == [
            "repo", "issue", "title", "why", "options",
            "downstream_count", "downstream", "since", "source",
        ]

    def test_escalation_row_produces_one_card_with_stored_proposed_command(
        self,
    ) -> None:
        """#2360's worked example: the stored ``proposed_command`` is reused
        verbatim as the recommended option, never re-derived."""
        result = fold_decisions(
            [
                _esc_row(
                    2360,
                    reason=(
                        "smoke_required — coord merge's own gate reports "
                        "'test verdict stale (recorded against base "
                        "11401bb, base now 8e02ded)', but this driver's OWN "
                        "view already shows test_state='passed' — the two "
                        "cannot converge by retrying the identical `coord "
                        "merge` command (#1526); a human must reconcile them"
                    ),
                    proposed_command=(
                        "coord diagnose claude-coordinator 2360 --stage "
                        "test --reset"
                    ),
                    created_at=900.0,
                )
            ],
            [],
            1000.0,
        )
        assert len(result.rows) == 1
        row = result.rows[0]
        assert row["repo"] == "claude-coordinator"
        assert row["issue"] == 2360
        assert row["source"] == "escalation"
        assert row["options"][0]["command_or_action"] == (
            "coord diagnose claude-coordinator 2360 --stage test --reset"
        )
        assert row["options"][0]["recommended"] is True
        assert row["since"] == 900.0
        # #2369's plain-language pairing for the recognised stale-verdict
        # shape — the raw reason alone is not sufficient.
        assert "stale" in row["why"].lower()
        assert "safe to merge" in row["why"].lower()

    def test_after_chain_collapses_into_one_root_card_with_downstream_count(
        self,
    ) -> None:
        """A 3-entry ``after=`` chain where only the root's block is a real
        cause: the two dependents cascade-blocked via `_resolve_prereqs`'s
        "it will never satisfy" verdict and must not get cards of their
        own — #2283's cascade collapse."""
        rows = [
            _dq_row(1, repo="api", state_="blocked", reason_at=100.0,
                     last_reason=_ACCEPTANCE_DEAD_END_REASON),
            _dq_row(
                2, repo="api", state_="blocked", after_json=["api#1"],
                last_reason=(
                    "api#2's pre-req(s) (api#1) queued but blocked/failed "
                    "— it will never satisfy"
                ),
            ),
            _dq_row(
                3, repo="api", state_="blocked", after_json=["api#2"],
                last_reason=(
                    "api#3's pre-req(s) (api#2) queued but blocked/failed "
                    "— it will never satisfy"
                ),
            ),
        ]
        result = fold_decisions([], rows, 1000.0)
        assert len(result.rows) == 1
        card = result.rows[0]
        assert (card["repo"], card["issue"]) == ("api", 1)
        assert card["downstream_count"] == 2
        assert card["downstream"] == ["api#2", "api#3"]

    def test_blocked_queue_row_with_no_escalation_parses_inspect_and_remedy(
        self,
    ) -> None:
        """#2283's shape: a `blocked` row with no matching escalation-table
        row still gets a card, options parsed from `last_reason`."""
        rows = [
            _dq_row(
                1, repo="api", state_="blocked",
                last_reason=_ACCEPTANCE_DEAD_END_REASON,
            )
        ]
        result = fold_decisions([], rows, 1000.0)
        assert len(result.rows) == 1
        card = result.rows[0]
        assert card["source"] == "queue"
        labels = {o["label"] for o in card["options"]}
        assert "Inspect" in labels
        assert "Re-author by hand" in labels
        assert any(o["recommended"] for o in card["options"])
        # #2369 review: `drive_queue.py`'s `reason = f"{own_reason} — "
        # f"{explanation}"` appends its rationale to `own_reason`'s LAST
        # line (the "or re-run ..." line here) with no newline first — the
        # parsed `Re-run` option must be just the runnable command, not that
        # appended " — the board row is terminal ..." rationale too.
        rerun = next(o for o in card["options"] if o["label"] == "Re-run")
        assert rerun["command_or_action"] == (
            "coord drive with --no-acceptance to skip JIT authoring."
        )
        assert "—" not in rerun["command_or_action"]
        assert "blocking without spending an attempt" not in rerun["command_or_action"]

    def test_ci_shaped_block_parses_inspect_the_gates_line(self) -> None:
        """coord-portal#107's shape: a failing CI check, with the existing
        "Inspect the gates: coord merge --plan --repo ..." line parsed."""
        rows = [
            _dq_row(
                107, repo="coord-portal", state_="failed",
                last_reason=_MERGE_ATTEMPTS_REASON,
            )
        ]
        result = fold_decisions([], rows, 1000.0)
        card = result.rows[0]
        option = next(
            o for o in card["options"] if o["label"] == "Inspect the gates"
        )
        assert option["command_or_action"] == (
            "coord merge --plan --repo coord-portal"
        )

    def test_novel_shape_still_gets_a_card_with_generic_fallback(self) -> None:
        """No template matches — the card still exists (#2369: never
        silently drop a stuck item), `why` is the raw reason verbatim, and
        the single fallback option is marked recommended."""
        rows = [
            _dq_row(
                55, repo="api", state_="blocked",
                last_reason="something totally novel happened here",
            )
        ]
        result = fold_decisions([], rows, 1000.0)
        card = result.rows[0]
        assert card["why"] == "something totally novel happened here"
        assert len(card["options"]) == 1
        assert card["options"][0]["recommended"] is True
        # #2369 review: `coord log <session_name or entry_key>` resolves
        # nothing — neither is a valid `coord log` ASSIGNMENT_ID. The
        # fallback must point at a command that actually resolves.
        assert card["options"][0]["command_or_action"] == (
            "coord drive-queue list --repo api"
        )

    def test_waiting_and_done_rows_are_never_cards(self) -> None:
        rows = [
            _dq_row(1, state_="waiting"),
            _dq_row(2, state_="done"),
            _dq_row(3, state_="running"),
        ]
        result = fold_decisions([], rows, 1000.0)
        assert result.rows == []

    def test_root_cards_sort_numerically_not_lexically(self) -> None:
        """#2369 review nit: sorting raw `"repo#issue"` strings puts
        `"api#10"` before `"api#2"`; `parse_key`'s `(repo, issue)` sorts the
        issue number as a number."""
        rows = [
            _dq_row(10, repo="api", state_="blocked", last_reason="x"),
            _dq_row(2, repo="api", state_="blocked", last_reason="y"),
        ]
        result = fold_decisions([], rows, 1000.0)
        assert [r["issue"] for r in result.rows] == [2, 10]

    def test_title_lookup_applied(self) -> None:
        result = fold_decisions(
            [_esc_row(1)], [], 1000.0, titles={("claude-coordinator", 1): "Fix it"}
        )
        assert result.rows[0]["title"] == "Fix it"

    def test_escalation_takes_precedence_over_a_matching_queue_row(self) -> None:
        """An issue with BOTH an escalation and a blocked queue row gets
        exactly one card, from the richer escalation source."""
        rows = [_dq_row(1, repo="api", state_="blocked", last_reason="x")]
        escalations = [_esc_row(1, repo="api", proposed_command="coord fix")]
        result = fold_decisions(escalations, rows, 1000.0)
        assert len(result.rows) == 1
        assert result.rows[0]["source"] == "escalation"

    def test_pending_headline_note_names_the_count(self) -> None:
        result = fold_decisions([_esc_row(1), _esc_row(2)], [], 1000.0)
        assert any("2 decisions pending" in n for n in result.notes)

    def test_remedy_labeled_line_parses_as_an_option(self) -> None:
        """`coord drive-queue list` already structures a `blocked` row's
        remedy as a `remedy: ...` line (`_BLOCKED_REMEDY`) — this fold
        recognises the same label shape if it ever lands in `last_reason`."""
        rows = [
            _dq_row(
                1, repo="api", state_="blocked",
                last_reason=(
                    "acceptance author aid-1 failed.\n"
                    "   remedy: coord acceptance author api 99 --issue 1"
                ),
            )
        ]
        result = fold_decisions([], rows, 1000.0)
        option = next(
            o for o in result.rows[0]["options"] if o["label"] == "Remedy"
        )
        assert option["command_or_action"] == "coord acceptance author api 99 --issue 1"


class TestRunDecisions:
    """The runner — ``*_fetch=``/``now=``/``title_lookup=`` seams."""

    def test_repo_param_filters_the_finished_cards(self) -> None:
        result = run_decisions(
            repo="web",
            now=1000.0,
            escalations_fetch=lambda repo: [_esc_row(1, repo="api"), _esc_row(2, repo="web")],
            queue_fetch=lambda repo: [],
            title_lookup=lambda keys: {},
        )
        assert [r["repo"] for r in result.rows] == ["web"]

    def test_repo_param_scopes_notes_to_the_filtered_rows(self) -> None:
        """#2369 review: `notes` was computed from the FULL, unfiltered
        fold before `repo` was applied to `rows`, so a `--repo web` call
        with 2 `api` escalations + 1 `web` escalation reported "3 decisions
        pending" next to a single-row table. `notes` must match `rows`."""
        result = run_decisions(
            repo="web",
            now=1000.0,
            escalations_fetch=lambda repo: [
                _esc_row(1, repo="api"), _esc_row(2, repo="api"),
                _esc_row(3, repo="web"),
            ],
            queue_fetch=lambda repo: [],
            title_lookup=lambda keys: {},
        )
        assert len(result.rows) == 1
        assert any("1 decision" in n for n in result.notes)
        assert not any("3 decision" in n for n in result.notes)

    def test_full_queue_fetched_regardless_of_repo_param(self) -> None:
        """#2183-style: the cascade needs the FULL queue, not a `--repo`
        filtered slice, so both fetches are always called with ``None``."""
        calls: list[str | None] = []

        def queue_fetch(repo):
            calls.append(repo)
            return []

        run_decisions(
            repo="web", now=1000.0,
            escalations_fetch=lambda repo: [],
            queue_fetch=queue_fetch,
            title_lookup=lambda keys: {},
        )
        assert calls == [None]

    def test_now_seam_sets_generated_at_and_window(self) -> None:
        result = run_decisions(
            now=555.0,
            escalations_fetch=lambda repo: [],
            queue_fetch=lambda repo: [],
            title_lookup=lambda keys: {},
        )
        assert result.generated_at == 555.0
        assert result.window == (555.0, 555.0)

    def test_title_lookup_receives_keys_from_both_sources(self) -> None:
        seen_keys: set = set()

        def title_lookup(keys):
            seen_keys.update(keys)
            return {}

        run_decisions(
            now=1000.0,
            escalations_fetch=lambda repo: [_esc_row(1, repo="api")],
            queue_fetch=lambda repo: [_dq_row(2, repo="web", state_="blocked")],
            title_lookup=title_lookup,
        )
        assert seen_keys == {("api", 1), ("web", 2)}

    def test_title_lookup_excludes_queue_rows_that_never_become_cards(self) -> None:
        """#2369 review non-blocking finding: the queue fetch is
        intentionally the FULL, unfiltered queue (every state, needed to
        resolve `after=` roots) — but `title_lookup` must be scoped to the
        rows that actually surface as cards, not every row that fetch
        returned, or a real fleet's 280+ historical `waiting`/`done`/
        `running` rows reintroduce the per-row title lookup this report
        exists to avoid."""
        seen_keys: set = set()

        def title_lookup(keys):
            seen_keys.update(keys)
            return {}

        run_decisions(
            now=1000.0,
            escalations_fetch=lambda repo: [],
            queue_fetch=lambda repo: [
                _dq_row(1, repo="api", state_="blocked", last_reason="x"),
                _dq_row(2, repo="api", state_="waiting"),
                _dq_row(3, repo="api", state_="done"),
                _dq_row(4, repo="api", state_="running"),
            ],
            title_lookup=title_lookup,
        )
        assert seen_keys == {("api", 1)}


class TestFindDecision:
    """`find_decision` (#2370) — the single-issue lookup `coord decide` calls
    fresh every time instead of caching its own copy of "what the options
    are". Same fold as `run_decisions`, minus the title lookup."""

    def test_returns_none_when_nothing_is_pending(self) -> None:
        assert find_decision(
            "api", 7, now=1000.0,
            escalations_fetch=lambda repo: [],
            queue_fetch=lambda repo: [],
        ) is None

    def test_finds_the_escalation_card(self) -> None:
        card = find_decision(
            "api", 7, now=1000.0,
            escalations_fetch=lambda repo: [_esc_row(7, repo="api", proposed_command="coord fix")],
            queue_fetch=lambda repo: [],
        )
        assert card is not None
        assert card["source"] == "escalation"
        assert card["options"][0]["command_or_action"] == "coord fix"

    def test_finds_the_queue_only_card(self) -> None:
        rows = [
            _dq_row(
                55, repo="api", state_="blocked",
                last_reason="something totally novel happened here",
            )
        ]
        card = find_decision(
            "api", 55, now=1000.0,
            escalations_fetch=lambda repo: [],
            queue_fetch=lambda repo: rows,
        )
        assert card is not None
        assert card["source"] == "queue"

    def test_returns_none_for_a_downstream_row_folded_into_another_card(self) -> None:
        """A queue row that cascade-collapses into a root's `downstream` list
        (#2283) gets no card of its own in the report — `find_decision` must
        agree, not invent a standalone card the report would never show."""
        rows = [
            _dq_row(1, repo="api", state_="blocked", reason_at=100.0,
                     last_reason=_ACCEPTANCE_DEAD_END_REASON),
            _dq_row(
                2, repo="api", state_="blocked", after_json=["api#1"],
                last_reason=(
                    "api#2's pre-req(s) (api#1) queued but blocked/failed "
                    "— it will never satisfy"
                ),
            ),
        ]
        assert find_decision(
            "api", 2, now=1000.0,
            escalations_fetch=lambda repo: [],
            queue_fetch=lambda repo: rows,
        ) is None
        # the root still resolves fine
        root = find_decision(
            "api", 1, now=1000.0,
            escalations_fetch=lambda repo: [],
            queue_fetch=lambda repo: rows,
        )
        assert root is not None
        assert root["downstream"] == ["api#2"]

    def test_queue_fetch_is_always_the_full_unfiltered_queue(self) -> None:
        """Same #2183 reasoning `run_decisions` documents: an `after=` chain
        can cross repos, so the queue fetch must never be scoped to `repo`
        even though `find_decision` only wants one issue back."""
        calls: list[str | None] = []

        def queue_fetch(repo):
            calls.append(repo)
            return []

        find_decision(
            "api", 7, now=1000.0,
            escalations_fetch=lambda repo: [],
            queue_fetch=queue_fetch,
        )
        assert calls == [None]


# ── registry + parameter validation ────────────────────────────────────────


class TestCatalogue:
    def test_the_registered_reports(self) -> None:
        assert set(REPORTS) == {
            "issue-activity", "completed", "drive-queue-status", "decisions",
            "usage", "queue-outcomes", "trend", "deprecated-routes",
        }

    def test_catalogue_carries_full_param_metadata(self) -> None:
        cat = catalogue()
        assert [r["id"] for r in cat["reports"]] == [
            "completed", "decisions", "deprecated-routes", "drive-queue-status",
            "issue-activity", "queue-outcomes", "trend", "usage",
        ]
        rep = next(r for r in cat["reports"] if r["id"] == "issue-activity")
        assert rep["title"] == "Issue Activity"
        assert rep["description"]
        params = {p["id"]: p for p in rep["params"]}
        assert set(params) == {"since", "until", "repo"}
        assert params["since"]["kind"] == "choice"
        assert params["since"]["choices"] == ["1h", "6h", "24h", "3d", "7d"]
        assert params["since"]["default"] == "24h"
        assert params["since"]["free_form"] is True
        assert params["repo"]["kind"] == "text"

    def test_drive_queue_status_catalogue_entry(self) -> None:
        cat = catalogue()
        rep = next(r for r in cat["reports"] if r["id"] == "drive-queue-status")
        assert rep["title"] == "Drive Queue Status"
        assert rep["description"]
        params = {p["id"]: p for p in rep["params"]}
        assert set(params) == {"repo"}
        assert params["repo"]["kind"] == "text"
        assert params["repo"]["default"] == ""

    def test_decisions_catalogue_entry(self) -> None:
        cat = catalogue()
        rep = next(r for r in cat["reports"] if r["id"] == "decisions")
        assert rep["title"] == "Decisions"
        assert rep["description"]
        params = {p["id"]: p for p in rep["params"]}
        assert set(params) == {"repo"}
        assert params["repo"]["kind"] == "text"
        assert params["repo"]["default"] == ""

    def test_deprecated_routes_catalogue_entry(self) -> None:
        cat = catalogue()
        rep = next(r for r in cat["reports"] if r["id"] == "deprecated-routes")
        assert rep["title"] == "Deprecated RPC Routes"
        assert rep["description"]
        assert rep["params"] == []

    def test_catalogue_is_json_serialisable(self) -> None:
        json.dumps(catalogue())


class TestParams:
    def test_defaults_fill_in(self) -> None:
        resolved = resolve_params(REPORTS["issue-activity"], {})
        assert resolved == {"since": "24h", "until": "", "repo": ""}

    def test_preset_and_free_form_durations_both_accepted(self) -> None:
        report = REPORTS["issue-activity"]
        assert resolve_params(report, {"since": "24h"})["since"] == "24h"
        assert resolve_params(report, {"since": "13h"})["since"] == "13h"
        assert resolve_params(report, {"since": "90m"})["since"] == "90m"

    def test_bad_since_names_the_allowed_values(self) -> None:
        with pytest.raises(ReportError) as exc:
            resolve_params(REPORTS["issue-activity"], {"since": "nonsense"})
        message = str(exc.value)
        assert "nonsense" in message
        for preset in ("1h", "6h", "24h", "3d", "7d"):
            assert preset in message

    def test_bad_until_is_a_clean_error(self) -> None:
        with pytest.raises(ReportError):
            resolve_params(REPORTS["issue-activity"], {"until": "not-a-time"})

    def test_unknown_param_names_the_known_ones(self) -> None:
        with pytest.raises(ReportError) as exc:
            resolve_params(REPORTS["issue-activity"], {"nope": "1"})
        assert "nope" in str(exc.value)
        assert "since" in str(exc.value)

    def test_unknown_report_id(self) -> None:
        with pytest.raises(UnknownReportError) as exc:
            run_report("no-such-report", {})
        assert "issue-activity" in str(exc.value)

    def test_parse_duration_units(self) -> None:
        assert parse_duration("30s") == 30
        assert parse_duration("90m") == 5400
        assert parse_duration("13h") == 46800
        assert parse_duration("3d") == 259200
        assert parse_duration("1w") == 604800
        with pytest.raises(ReportError):
            parse_duration("13 fortnights")


# ── CLI ────────────────────────────────────────────────────────────────────


def _seed_known_good_window(coord_db) -> None:
    """The issue's known-good cross-check, shrunk: four issues that all
    merged, one of which (#1631) had its driver exit 1 before the merge."""
    base = T0
    for issue, offset in ((1629, 100), (1729, 200)):
        record_audit(tier="business", category="dispatch", event_type="dispatched", actor="drive", summary="d", repo="api", issue=issue, machine="precision", details={"type": "work"}, ts=base + offset)
        record_audit(tier="business", category="review", event_type="review_request-changes", actor="reviewer", summary="r", repo="api", issue=issue, ts=base + offset + 10)
        record_audit(tier="business", category="dispatch", event_type="dispatched", actor="drive", summary="d", repo="api", issue=issue, machine="precision", details={"type": "work"}, ts=base + offset + 20)
        record_audit(tier="business", category="review", event_type="review_approve", actor="reviewer", summary="r", repo="api", issue=issue, ts=base + offset + 30)
        record_audit(tier="business", category="merge", event_type="merged", actor="coordinator", summary="m", repo="api", issue=issue, ts=base + offset + 40)
    # #1728: uneventful merge.
    record_audit(tier="business", category="dispatch", event_type="dispatched", actor="drive", summary="d", repo="api", issue=1728, machine="dellserver", details={"type": "work"}, ts=base + 300)
    record_audit(tier="business", category="merge", event_type="merged", actor="coordinator", summary="m", repo="api", issue=1728, ts=base + 340)
    # #1631: driver gave up, merge landed anyway.
    record_audit(tier="business", category="dispatch", event_type="dispatched", actor="drive", summary="d", repo="api", issue=1631, machine="dellserver", details={"type": "work"}, ts=base + 400)
    record_audit(tier="business", category="drive", event_type="drive_exited", actor="drive", summary="x", repo="api", issue=1631, details={"exit_code": 1, "reason": "merge attempted 3 times without landing"}, ts=base + 500)
    record_audit(tier="business", category="merge", event_type="merged", actor="coordinator", summary="m", repo="api", issue=1631, ts=base + 500 + 13 * 60)


def _seed_started_before_window_case(coord_db) -> None:
    """#1760's own reproduction, shrunk to its essential shape.

    #1629: the original dispatch lands at ``T0 - 3600`` — outside a 13h
    window (whose start IS ``T0``) but inside a 20h window (whose start is
    ``T0 - 25200``). The request-changes review, the fix-1 dispatch, its
    test and its approve review are all inside BOTH windows — mirroring the
    live case where only the original dispatch predates the window, not the
    whole review cycle around it.

    #1729 is the control: its entire history is inside both windows.

    #1631 is the already-covered "driver exited 1 but merged anyway"
    anomaly — reseeded here (independent DB per test) to assert it keeps
    firing in both windows once #1629's row stops being self-contradictory.
    """
    base = T0
    # #1629 — original dispatch OUTSIDE the 13h window, inside the 20h one.
    record_audit(tier="business", category="dispatch", event_type="dispatched", actor="drive", summary="d", repo="api", issue=1629, machine="precision", details={"type": "work"}, ts=base - 3600)
    record_audit(tier="business", category="review", event_type="review_request-changes", actor="reviewer", summary="r", repo="api", issue=1629, ts=base + 10)
    record_audit(tier="business", category="dispatch", event_type="dispatched", actor="drive", summary="d", repo="api", issue=1629, machine="precision", details={"type": "work"}, ts=base + 100)
    record_audit(tier="business", category="test", event_type="test_passed", actor="drive", summary="t", repo="api", issue=1629, ts=base + 110)
    record_audit(tier="business", category="review", event_type="review_approve", actor="reviewer", summary="r", repo="api", issue=1629, ts=base + 120)
    # #1729 — control: entirely in-window in both cases.
    record_audit(tier="business", category="dispatch", event_type="dispatched", actor="drive", summary="d", repo="api", issue=1729, machine="precision", details={"type": "work"}, ts=base + 200)
    record_audit(tier="business", category="review", event_type="review_request-changes", actor="reviewer", summary="r", repo="api", issue=1729, ts=base + 210)
    record_audit(tier="business", category="dispatch", event_type="dispatched", actor="drive", summary="d", repo="api", issue=1729, machine="precision", details={"type": "work"}, ts=base + 220)
    record_audit(tier="business", category="test", event_type="test_passed", actor="drive", summary="t", repo="api", issue=1729, ts=base + 230)
    record_audit(tier="business", category="review", event_type="review_approve", actor="reviewer", summary="r", repo="api", issue=1729, ts=base + 240)
    # #1631 — driver gave up, merge landed anyway; must fire in both windows.
    record_audit(tier="business", category="dispatch", event_type="dispatched", actor="drive", summary="d", repo="api", issue=1631, machine="dellserver", details={"type": "work"}, ts=base + 400)
    record_audit(tier="business", category="drive", event_type="drive_exited", actor="drive", summary="x", repo="api", issue=1631, details={"exit_code": 1, "reason": "merge attempted 3 times without landing"}, ts=base + 500)
    record_audit(tier="business", category="merge", event_type="merged", actor="coordinator", summary="m", repo="api", issue=1631, ts=base + 500 + 13 * 60)


@pytest.fixture(autouse=True)
def _frozen_now(monkeypatch):
    """Freeze the report engine's clock at the known-good window end so
    ``since=13h`` covers the seeded rows deterministically."""
    monkeypatch.setattr("coord.reports.time.time", lambda: WINDOW[1])


class TestCli:
    def test_report_list_prints_the_one_report_with_params(self, coord_db) -> None:
        result = CliRunner().invoke(main, ["report", "list"])
        assert result.exit_code == 0, result.output
        assert "issue-activity" in result.output
        assert "Issue Activity" in result.output
        assert "since" in result.output
        assert "24h" in result.output  # the default
        for preset in ("1h", "6h", "3d", "7d"):
            assert preset in result.output
        # Every registered report appears in the catalogue.
        assert result.output.count("—  ") == len(REPORTS)

    def test_report_list_json(self, coord_db) -> None:
        result = CliRunner().invoke(main, ["report", "list", "--json"])
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert [r["id"] for r in body["reports"]] == [
            "completed", "decisions", "deprecated-routes", "drive-queue-status",
            "issue-activity", "queue-outcomes", "trend", "usage",
        ]

    def test_report_run_json_shape(self, coord_db) -> None:
        _seed_known_good_window(coord_db)
        result = CliRunner().invoke(
            main, ["report", "run", "issue-activity", "--param", "since=13h", "--json"]
        )
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert set(body) == {
            "report_id", "generated_at", "window", "columns", "column_meta",
            "rows", "notes", "totals", "chart",
        }
        # #1763: additive and None for every report that has no meaningful sum.
        assert body["totals"] is None
        # #2271: same posture for the chart declaration.
        assert body["chart"] is None
        assert body["report_id"] == "issue-activity"
        assert body["window"] == [WINDOW[1] - 13 * 3600, WINDOW[1]]

        by_issue = {r["issue"]: r for r in body["rows"]}
        assert set(by_issue) == {1629, 1631, 1728, 1729}
        for row in body["rows"]:
            for key in (
                "started_at", "test_verdicts", "review_verdicts", "merged_at",
                "drive_exit", "outcome",
            ):
                assert key in row
            assert row["outcome"] == "merged"

        assert by_issue[1629]["fix_iterations"] == 1
        assert by_issue[1629]["review_verdicts"] == ["request-changes", "approve"]
        assert by_issue[1729]["fix_iterations"] == 1
        assert by_issue[1729]["review_verdicts"] == ["request-changes", "approve"]
        assert by_issue[1728]["fix_iterations"] == 0

        assert any("1631" in n and "exit_code=1" in n for n in body["notes"])

    def test_report_run_human_table(self, coord_db) -> None:
        _seed_known_good_window(coord_db)
        result = CliRunner().invoke(
            main, ["report", "run", "issue-activity", "--param", "since=13h"]
        )
        assert result.exit_code == 0, result.output
        assert "OUTCOME" in result.output
        assert "1631" in result.output
        assert "merged" in result.output
        assert "notes" in result.output
        assert "request-changes,approve" in result.output

    def test_started_before_window_across_two_windows_agrees_with_itself(
        self, coord_db
    ) -> None:
        """Live acceptance criterion: at since=13h, #1629's row must no
        longer claim a start it can't support — no start time, at least one
        fix iteration, and a lower-bound note naming it.  At since=20h it is
        a complete row with no such note.  #1729 (the control) and #1631
        (the drive-exit-but-merged anomaly) are unaffected in both."""
        _seed_started_before_window_case(coord_db)

        result_13h = json.loads(
            CliRunner()
            .invoke(main, ["report", "run", "issue-activity", "--param", "since=13h", "--json"])
            .output
        )
        result_20h = json.loads(
            CliRunner()
            .invoke(main, ["report", "run", "issue-activity", "--param", "since=20h", "--json"])
            .output
        )

        row_13h = next(r for r in result_13h["rows"] if r["issue"] == 1629)
        assert row_13h["started_at"] is None
        assert row_13h["started_before_window"] is True
        assert row_13h["fix_iterations"] == 1
        assert row_13h["counts_partial"] is True
        note_13h = next(n for n in result_13h["notes"] if "api#1629" in n)
        assert "lower bound" in note_13h.lower()

        row_20h = next(r for r in result_20h["rows"] if r["issue"] == 1629)
        assert row_20h["started_at"] == T0 - 3600
        assert row_20h["started_before_window"] is False
        assert row_20h["fix_iterations"] == 1
        assert row_20h["counts_partial"] is False
        assert not any("api#1629" in n for n in result_20h["notes"])

        # #1729 (control) — unaffected in both windows.
        for result in (result_13h, result_20h):
            row_1729 = next(r for r in result["rows"] if r["issue"] == 1729)
            assert row_1729["started_before_window"] is False
            assert row_1729["fix_iterations"] == 1
            assert row_1729["counts_partial"] is False
            assert not any("api#1729" in n for n in result["notes"])

        # #1631's driver-exit-but-merged anomaly must still fire in both.
        for result in (result_13h, result_20h):
            assert any("1631" in n and "exit_code=1" in n for n in result["notes"])

    def test_column_meta_is_present_and_matches_columns(self, coord_db) -> None:
        _seed_known_good_window(coord_db)
        result = CliRunner().invoke(
            main, ["report", "run", "issue-activity", "--param", "since=13h", "--json"]
        )
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)

        assert [m["id"] for m in body["column_meta"]] == body["columns"]

        meta_by_id = {m["id"]: m for m in body["column_meta"]}
        assert meta_by_id["started_at"]["kind"] == "timestamp"
        assert meta_by_id["merged_at"]["kind"] == "timestamp"
        assert meta_by_id["machines"]["kind"] == "list"
        assert meta_by_id["test_verdicts"]["kind"] == "list"
        assert meta_by_id["review_verdicts"]["kind"] == "list"
        assert meta_by_id["fix_iterations"]["kind"] == "int"
        assert meta_by_id["fix_iterations"]["align"] == "right"
        assert meta_by_id["title"]["weight"] > meta_by_id["issue"]["weight"]

        # Row values are unchanged: started_at is still an epoch float,
        # machines still a list — presentation moved, data did not.
        row = next(r for r in body["rows"] if r["issue"] == 1629)
        assert isinstance(row["started_at"], float)
        assert isinstance(row["machines"], list)

    def test_report_run_empty_window(self, coord_db) -> None:
        result = CliRunner().invoke(
            main, ["report", "run", "issue-activity", "--param", "since=1h"]
        )
        assert result.exit_code == 0, result.output
        assert "no activity in this window" in result.output

    def test_report_run_bad_param_value_exits_nonzero_naming_allowed_values(
        self, coord_db
    ) -> None:
        from tests.conftest import output_and_stderr

        result = CliRunner().invoke(
            main, ["report", "run", "issue-activity", "--param", "since=nonsense"]
        )
        assert result.exit_code != 0
        text = output_and_stderr(result)
        assert "nonsense" in text
        assert "1h" in text and "7d" in text
        assert "Traceback" not in text

    def test_report_run_unknown_report_exits_nonzero(self, coord_db) -> None:
        from tests.conftest import output_and_stderr

        result = CliRunner().invoke(main, ["report", "run", "no-such-report"])
        assert result.exit_code != 0
        text = output_and_stderr(result)
        assert "no-such-report" in text
        assert "issue-activity" in text

    def test_report_run_unknown_param_exits_nonzero(self, coord_db) -> None:
        from tests.conftest import output_and_stderr

        result = CliRunner().invoke(
            main, ["report", "run", "issue-activity", "--param", "bogus=1"]
        )
        assert result.exit_code != 0
        assert "bogus" in output_and_stderr(result)

    def test_report_run_malformed_param_exits_nonzero(self, coord_db) -> None:
        from tests.conftest import output_and_stderr

        result = CliRunner().invoke(
            main, ["report", "run", "issue-activity", "--param", "since"]
        )
        assert result.exit_code != 0
        assert "KEY=VALUE" in output_and_stderr(result)

    def test_repo_param_narrows(self, coord_db) -> None:
        _seed_known_good_window(coord_db)
        record_audit(tier="business", category="merge", event_type="merged", actor="coordinator", summary="m", repo="web", issue=9, ts=T0 + 600)

        result = CliRunner().invoke(
            main,
            ["report", "run", "issue-activity", "--param", "since=13h",
             "--param", "repo=web", "--json"],
        )
        assert result.exit_code == 0, result.output
        rows = json.loads(result.output)["rows"]
        assert [r["issue"] for r in rows] == [9]

    def test_titles_come_from_the_local_board(self, coord_db) -> None:
        coord_db.execute(
            "INSERT INTO issues (repo_name, number, title, body, state, labels, "
            "synced_at) VALUES (?,?,?,?,?,?,?)",
            ("api", 1631, "Merge queue gives up early", "", "closed", "[]", 0.0),
        )
        coord_db.commit()
        _seed_known_good_window(coord_db)

        result = CliRunner().invoke(
            main, ["report", "run", "issue-activity", "--param", "since=13h", "--json"]
        )
        assert result.exit_code == 0, result.output
        rows = {r["issue"]: r for r in json.loads(result.output)["rows"]}
        assert rows[1631]["title"] == "Merge queue gives up early"
        assert rows[1728]["title"] is None


class TestCliDriveQueueStatus:
    """``coord report run drive-queue-status`` against a real ``drive_queue``
    table, seeded through the routed ``coord.state`` writers (#1805)."""

    def _seed(self, coord_db) -> None:
        state.enqueue_drive_queue("api", 10, machine="dellserver")
        state.enqueue_drive_queue("api", 11, after=["api#10"])
        state.enqueue_drive_queue("web", 20)
        state._update_drive_queue_entry_local(
            "api", 10, state="running", session_name="s1"
        )
        state._update_drive_queue_entry_local(
            "api", 11, state="waiting", attempts=2, last_reason="deferred: api#10 not done"
        )

    def test_prints_a_table_of_the_live_queue(self, coord_db) -> None:
        self._seed(coord_db)
        result = CliRunner().invoke(main, ["report", "run", "drive-queue-status"])
        assert result.exit_code == 0, result.output
        assert "STATE" in result.output
        assert "10" in result.output and "11" in result.output and "20" in result.output
        assert "running" in result.output

    def test_json_emits_documented_shape_including_column_meta(self, coord_db) -> None:
        self._seed(coord_db)
        result = CliRunner().invoke(
            main, ["report", "run", "drive-queue-status", "--json"]
        )
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert set(body) == {
            "report_id", "generated_at", "window", "columns", "column_meta",
            "rows", "notes", "totals", "chart",
        }
        # #1763: additive and None for every report that has no meaningful sum.
        assert body["totals"] is None
        # #2271: same posture for the chart declaration.
        assert body["chart"] is None
        assert body["report_id"] == "drive-queue-status"
        assert body["window"][0] == body["window"][1]
        assert [m["id"] for m in body["column_meta"]] == body["columns"]
        assert {r["issue"] for r in body["rows"]} == {10, 11, 20}

    def test_repo_param_restricts_to_one_repo(self, coord_db) -> None:
        self._seed(coord_db)
        result = CliRunner().invoke(
            main, ["report", "run", "drive-queue-status", "--param", "repo=web", "--json"]
        )
        assert result.exit_code == 0, result.output
        rows = json.loads(result.output)["rows"]
        assert [r["issue"] for r in rows] == [20]

    def test_omitting_repo_returns_all_repos(self, coord_db) -> None:
        self._seed(coord_db)
        result = CliRunner().invoke(
            main, ["report", "run", "drive-queue-status", "--json"]
        )
        rows = json.loads(result.output)["rows"]
        assert {r["repo"] for r in rows} == {"api", "web"}

    def test_unknown_param_rejected_naming_allowed(self, coord_db) -> None:
        from tests.conftest import output_and_stderr

        result = CliRunner().invoke(
            main, ["report", "run", "drive-queue-status", "--param", "since=13h"]
        )
        assert result.exit_code != 0
        text = output_and_stderr(result)
        assert "since" in text
        assert "repo" in text

    def test_empty_queue_returns_rows_empty_with_a_note(self, coord_db) -> None:
        result = CliRunner().invoke(
            main, ["report", "run", "drive-queue-status", "--json"]
        )
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert body["rows"] == []
        assert any("empty" in n.lower() for n in body["notes"])

    def test_attempts_ge_1_tell_appears_in_notes(self, coord_db) -> None:
        self._seed(coord_db)
        result = CliRunner().invoke(
            main, ["report", "run", "drive-queue-status", "--json"]
        )
        body = json.loads(result.output)
        assert any("api#11" in n and "attempts=2" in n for n in body["notes"])

    def test_timestamps_render_as_dates_not_epochs_in_the_human_table(
        self, coord_db
    ) -> None:
        self._seed(coord_db)
        result = CliRunner().invoke(main, ["report", "run", "drive-queue-status"])
        assert result.exit_code == 0, result.output
        # enqueued_at is a timestamp column (column_meta kind="timestamp") —
        # the human table renders it relative/aliased, never a bare epoch
        # float with a decimal point.
        import re

        assert not re.search(r"\b\d{9,}\.\d+\b", result.output)

    def test_title_from_local_board(self, coord_db) -> None:
        coord_db.execute(
            "INSERT INTO issues (repo_name, number, title, body, state, labels, "
            "synced_at) VALUES (?,?,?,?,?,?,?)",
            ("api", 10, "Tighten the startup window", "", "open", "[]", 0.0),
        )
        coord_db.commit()
        self._seed(coord_db)
        result = CliRunner().invoke(
            main, ["report", "run", "drive-queue-status", "--json"]
        )
        rows = {r["issue"]: r for r in json.loads(result.output)["rows"]}
        assert rows[10]["title"] == "Tighten the startup window"
        assert rows[11]["title"] is None

    def test_does_not_run_a_tick(self, coord_db, monkeypatch) -> None:
        """Acceptance: a report must never call plan_tick."""
        import coord.drive_queue as dq

        def _boom(*a, **k):  # noqa: ANN002, ANN003
            raise AssertionError("drive-queue-status report called plan_tick")

        monkeypatch.setattr(dq, "plan_tick", _boom)
        self._seed(coord_db)
        result = CliRunner().invoke(
            main, ["report", "run", "drive-queue-status", "--json"]
        )
        assert result.exit_code == 0, result.output


class TestCliDecisions:
    """``coord report run decisions`` against real `drive_escalations` /
    `drive_queue` tables, seeded through the routed `coord.state` writers
    (#2369)."""

    def test_empty_board_returns_no_rows_with_a_note(self, coord_db) -> None:
        result = CliRunner().invoke(
            main, ["report", "run", "decisions", "--json"]
        )
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert body["report_id"] == "decisions"
        assert body["rows"] == []
        assert any("nothing" in n.lower() for n in body["notes"])

    def test_escalation_row_surfaces_as_a_card(self, coord_db) -> None:
        state.record_drive_escalation(
            "api", 7,
            stage="merge",
            reason="merge_status=NEEDS_ATTENTION — no number of retries changes this",
            gate_readings="merge_status=NEEDS_ATTENTION",
            proposed_command="coord merge --plan --repo api",
        )
        result = CliRunner().invoke(
            main, ["report", "run", "decisions", "--json"]
        )
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert len(body["rows"]) == 1
        row = body["rows"][0]
        assert row["repo"] == "api"
        assert row["issue"] == 7
        assert row["options"][0]["command_or_action"] == "coord merge --plan --repo api"

    def test_blocked_queue_row_surfaces_as_a_card(self, coord_db) -> None:
        state.enqueue_drive_queue("api", 9)
        state._update_drive_queue_entry_local(
            "api", 9, state="blocked",
            last_reason="acceptance author aid-1 failed.\n   inspect: coord log aid-1 --machine dellserver",
        )
        result = CliRunner().invoke(
            main, ["report", "run", "decisions", "--json"]
        )
        assert result.exit_code == 0, result.output
        rows = json.loads(result.output)["rows"]
        assert [r["issue"] for r in rows] == [9]
        assert rows[0]["source"] == "queue"

    def test_human_table_prints_the_why_column(self, coord_db) -> None:
        state.record_drive_escalation(
            "api", 7, stage="merge", reason="stuck for a reason",
            gate_readings="", proposed_command="coord merge --plan --repo api",
        )
        result = CliRunner().invoke(main, ["report", "run", "decisions"])
        assert result.exit_code == 0, result.output
        assert "api" in result.output
        assert "7" in result.output

    def test_does_not_run_a_tick(self, coord_db, monkeypatch) -> None:
        import coord.drive_queue as dq

        def _boom(*a, **k):  # noqa: ANN002, ANN003
            raise AssertionError("decisions report called plan_tick")

        monkeypatch.setattr(dq, "plan_tick", _boom)
        state.enqueue_drive_queue("api", 9)
        state._update_drive_queue_entry_local(
            "api", 9, state="blocked", last_reason="x"
        )
        result = CliRunner().invoke(
            main, ["report", "run", "decisions", "--json"]
        )
        assert result.exit_code == 0, result.output


# ── daemon endpoints ───────────────────────────────────────────────────────


def _make_daemon_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    conn.commit()
    conn.close()


@pytest.fixture
def daemon_db(tmp_path: Path) -> Path:
    p = tmp_path / "coord.db"
    _make_daemon_db(p)
    return p


@pytest.fixture
def rw_db(tmp_path: Path):
    """Thread-safe file-backed ``coord.db`` override — the autouse
    ``coord_db`` fixture's thread-bound ``:memory:`` conn is unusable from
    the ASGI worker thread TestClient runs handlers on."""
    from coord import db as db_mod

    conn = sqlite3.connect(str(tmp_path / "rw.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    db_mod.override_connection(conn)
    yield conn
    db_mod.close()


@pytest.fixture
def report_client(daemon_db: Path, valid_config_path: Path, rw_db) -> TestClient:
    app = build_app(SqliteStore(daemon_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        yield cli


class TestDaemonEndpoints:
    def test_get_report_returns_the_catalogue_with_param_metadata(
        self, report_client: TestClient
    ) -> None:
        resp = report_client.get("/report")
        assert resp.status_code == 200
        body = resp.json()
        assert [r["id"] for r in body["reports"]] == [
            "completed", "decisions", "deprecated-routes", "drive-queue-status",
            "issue-activity", "queue-outcomes", "trend", "usage",
        ]
        rep = next(r for r in body["reports"] if r["id"] == "issue-activity")
        params = {p["id"]: p for p in rep["params"]}
        assert params["since"]["choices"] == ["1h", "6h", "24h", "3d", "7d"]
        assert params["since"]["default"] == "24h"
        assert params["since"]["kind"] == "choice"
        assert params["repo"]["kind"] == "text"

    def test_get_report_catalogue_includes_drive_queue_status(
        self, report_client: TestClient
    ) -> None:
        resp = report_client.get("/report")
        body = resp.json()
        rep = next(r for r in body["reports"] if r["id"] == "drive-queue-status")
        assert rep["title"] == "Drive Queue Status"
        params = {p["id"]: p for p in rep["params"]}
        assert set(params) == {"repo"}

    def test_get_report_run_returns_report_result(
        self, report_client: TestClient, rw_db
    ) -> None:
        _seed_known_good_window(rw_db)
        resp = report_client.get("/report/issue-activity", params={"since": "13h"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["report_id"] == "issue-activity"
        assert {r["issue"] for r in body["rows"]} == {1629, 1631, 1728, 1729}
        assert any("1631" in n for n in body["notes"])
        # #1760: additive display metadata, one entry per `columns` entry.
        assert [m["id"] for m in body["column_meta"]] == body["columns"]

    def test_column_meta_is_additive_columns_and_rows_are_unchanged(
        self, report_client: TestClient, rw_db
    ) -> None:
        """#1760 acceptance: a client that ignores `column_meta` entirely
        gets the v0.4.100 `columns`/`rows` shape byte-for-byte — `columns`
        stays a plain `list[str]`, and existing row keys keep their values."""
        _seed_known_good_window(rw_db)
        resp = report_client.get("/report/issue-activity", params={"since": "13h"})
        body = resp.json()

        assert body["columns"] == [
            "repo", "issue", "title", "started_at", "machines",
            "fix_iterations", "test_verdicts", "review_verdicts",
            "merged_at", "drive_exit", "outcome",
        ]
        assert all(isinstance(c, str) for c in body["columns"])

        by_issue = {r["issue"]: r for r in body["rows"]}
        assert by_issue[1629]["started_at"] == WINDOW[0] + 100
        assert by_issue[1629]["fix_iterations"] == 1
        assert by_issue[1629]["review_verdicts"] == ["request-changes", "approve"]
        assert by_issue[1728]["fix_iterations"] == 0

    def test_endpoint_and_cli_agree_byte_for_byte_on_the_same_window(
        self, report_client: TestClient, rw_db
    ) -> None:
        """The daemon's JSON and the CLI's ``--json`` are the same bytes for
        the same window — that is what makes the TUI panel (#1741) and a
        terminal answer the same question the same way."""
        _seed_known_good_window(rw_db)
        until = repr(WINDOW[1])

        endpoint = report_client.get(
            "/report/issue-activity", params={"since": "13h", "until": until}
        ).json()
        cli_result = CliRunner().invoke(
            main,
            ["report", "run", "issue-activity", "--param", "since=13h",
             "--param", f"until={until}", "--json"],
        )
        assert cli_result.exit_code == 0, cli_result.output
        cli_body = json.loads(cli_result.output)

        assert json.dumps(cli_body, sort_keys=True) == json.dumps(
            endpoint, sort_keys=True
        )

    def test_report_endpoints_require_auth_when_token_set(
        self, daemon_db: Path, valid_config_path: Path, rw_db
    ) -> None:
        app = build_app(SqliteStore(daemon_db), load_config(valid_config_path), token="s3cret")
        with TestClient(app) as cli:
            assert cli.get("/report").status_code == 401
            assert cli.get("/report/issue-activity").status_code == 401
            headers = {"Authorization": "Bearer s3cret"}
            assert cli.get("/report", headers=headers).status_code == 200
            assert cli.get(
                "/report/issue-activity", params={"since": "1h"}, headers=headers
            ).status_code == 200

    def test_unknown_report_is_404(self, report_client: TestClient) -> None:
        resp = report_client.get("/report/no-such-report")
        assert resp.status_code == 404
        assert "issue-activity" in resp.json()["error"]

    def test_bad_param_value_is_400_naming_allowed_values(
        self, report_client: TestClient
    ) -> None:
        resp = report_client.get(
            "/report/issue-activity", params={"since": "nonsense"}
        )
        assert resp.status_code == 400
        error = resp.json()["error"]
        assert "nonsense" in error
        assert "1h" in error and "7d" in error

    def test_unknown_param_is_400(self, report_client: TestClient) -> None:
        resp = report_client.get("/report/issue-activity", params={"bogus": "1"})
        assert resp.status_code == 400
        assert "bogus" in resp.json()["error"]

    def test_report_run_makes_no_subprocess_calls(
        self, report_client: TestClient, rw_db, monkeypatch
    ) -> None:
        """Read-only means read-only: no ``gh``, no shell-out."""
        import subprocess

        def _no_subprocess(*args, **kwargs):  # noqa: ANN002, ANN003
            argv = args[0] if args else kwargs.get("args")
            raise AssertionError(f"subprocess spawned on a report run: {argv!r}")

        _seed_known_good_window(rw_db)
        monkeypatch.setattr(subprocess, "run", _no_subprocess)
        monkeypatch.setattr(subprocess, "Popen", _no_subprocess)
        assert report_client.get(
            "/report/issue-activity", params={"since": "13h"}
        ).status_code == 200

    def test_running_a_report_does_not_mutate_the_board(
        self, report_client: TestClient, rw_db
    ) -> None:
        """The acceptance invariant: a report is a read.  Assert the board's
        ``updated`` timestamp and its assignment rows are untouched across a
        run — this repo's recurring failure mode is a read path quietly
        growing a write."""
        _seed_known_good_window(rw_db)
        rw_db.execute(
            "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
            "repo_github, issue_number, issue_title, status, type) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("work1", "laptop", "api", "acme/api", 1629, "t", "done", "work"),
        )
        rw_db.execute(
            "INSERT OR REPLACE INTO board_meta (key, value) VALUES ('updated', '123.5')"
        )
        rw_db.commit()

        def _snapshot() -> tuple:
            updated = rw_db.execute(
                "SELECT value FROM board_meta WHERE key = 'updated'"
            ).fetchone()["value"]
            rows = rw_db.execute(
                "SELECT * FROM assignments ORDER BY assignment_id"
            ).fetchall()
            audit_count = rw_db.execute(
                "SELECT COUNT(*) AS c FROM audit_log"
            ).fetchone()["c"]
            return updated, [tuple(r) for r in rows], audit_count

        before = _snapshot()
        assert report_client.get(
            "/report/issue-activity", params={"since": "13h"}
        ).status_code == 200
        assert _snapshot() == before


class TestDaemonDriveQueueStatus:
    """``GET /report/drive-queue-status`` (#1805) — same seam/acceptance bar
    as ``TestDaemonEndpoints`` above, scoped to the new report."""

    def _seed(self, rw_db) -> None:
        state.enqueue_drive_queue("api", 10, machine="dellserver")
        state.enqueue_drive_queue("api", 11, after=["api#10"])
        state.enqueue_drive_queue("web", 20)
        state._update_drive_queue_entry_local("api", 10, state="running")
        state._update_drive_queue_entry_local(
            "api", 11, state="waiting", attempts=2, last_reason="deferred"
        )

    def test_get_report_run_returns_report_result(
        self, report_client: TestClient, rw_db
    ) -> None:
        self._seed(rw_db)
        resp = report_client.get("/report/drive-queue-status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["report_id"] == "drive-queue-status"
        assert {r["issue"] for r in body["rows"]} == {10, 11, 20}
        assert body["window"][0] == body["window"][1] == body["generated_at"]
        assert [m["id"] for m in body["column_meta"]] == body["columns"]

    def test_repo_param_restricts_to_one_repo(
        self, report_client: TestClient, rw_db
    ) -> None:
        self._seed(rw_db)
        resp = report_client.get(
            "/report/drive-queue-status", params={"repo": "web"}
        )
        assert resp.status_code == 200
        assert [r["issue"] for r in resp.json()["rows"]] == [20]

    def test_unknown_param_is_400_naming_repo(
        self, report_client: TestClient, rw_db
    ) -> None:
        resp = report_client.get(
            "/report/drive-queue-status", params={"since": "13h"}
        )
        assert resp.status_code == 400
        error = resp.json()["error"]
        assert "since" in error
        assert "repo" in error

    def test_empty_queue_returns_rows_empty_with_a_note(
        self, report_client: TestClient, rw_db
    ) -> None:
        resp = report_client.get("/report/drive-queue-status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["rows"] == []
        assert any("empty" in n.lower() for n in body["notes"])

    def test_endpoint_and_cli_agree_byte_for_byte(
        self, report_client: TestClient, rw_db
    ) -> None:
        self._seed(rw_db)
        # Freeze both sides' clock so `generated_at`/`window` agree exactly.
        endpoint = report_client.get(
            "/report/drive-queue-status", params={"repo": "api"}
        )
        assert endpoint.status_code == 200
        cli_result = CliRunner().invoke(
            main,
            ["report", "run", "drive-queue-status", "--param", "repo=api", "--json"],
        )
        assert cli_result.exit_code == 0, cli_result.output
        endpoint_body = endpoint.json()
        cli_body = json.loads(cli_result.output)
        # generated_at/window are wall-clock and legitimately differ between
        # the two separate processes/threads; compare everything else.
        for body in (endpoint_body, cli_body):
            body.pop("generated_at")
            body.pop("window")
        assert json.dumps(cli_body, sort_keys=True) == json.dumps(
            endpoint_body, sort_keys=True
        )

    def test_running_a_report_does_not_mutate_the_board(
        self, report_client: TestClient, rw_db
    ) -> None:
        self._seed(rw_db)

        def _snapshot() -> tuple:
            rows = rw_db.execute(
                "SELECT * FROM drive_queue ORDER BY id"
            ).fetchall()
            audit_count = rw_db.execute(
                "SELECT COUNT(*) AS c FROM audit_log"
            ).fetchone()["c"]
            return [tuple(r) for r in rows], audit_count

        before = _snapshot()
        assert report_client.get("/report/drive-queue-status").status_code == 200
        assert _snapshot() == before


def test_report_seam_routes_to_the_daemon_when_board_service_is_set(monkeypatch) -> None:
    """A thin client folds nothing locally — both the catalogue and the run
    go over HTTP, because the audit trail lives on the daemon host."""
    import coord.client as cc
    from coord import state

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    calls: dict = {}
    monkeypatch.setattr(
        cc,
        "fetch_report_catalogue",
        lambda svc, **kw: calls.update(catalogue_url=svc.url) or {"reports": []},
    )
    monkeypatch.setattr(
        cc,
        "fetch_report",
        lambda svc, report_id, params, **kw: calls.update(
            report_id=report_id, params=params
        )
        or {"report_id": report_id, "rows": []},
    )

    assert state.list_reports() == {"reports": []}
    assert state.run_report("issue-activity", {"since": "13h"})["rows"] == []
    assert calls["catalogue_url"] == "http://d:7435"
    assert calls["report_id"] == "issue-activity"
    assert calls["params"] == {"since": "13h"}


# ── CLI human-table rendering from column_meta (#1760) ─────────────────────


class TestFormatCell:
    """``<window`` means "started_before_window" — it belongs to
    ``started_at`` alone.  A regression caught while smoke-testing #1760's
    own fix: generalising the ``<window`` marker to every ``kind:
    "timestamp"`` column made an unmerged, started-before-window issue's
    empty ``merged_at`` also render as ``<window``, which reads as "this
    merge happened before the window" — false."""

    def test_window_marker_is_scoped_to_started_at(self) -> None:
        from coord.commands.report import _format_cell

        row = {
            "started_at": None,
            "started_before_window": True,
            "merged_at": None,
        }
        started_meta = {"id": "started_at", "kind": "timestamp"}
        merged_meta = {"id": "merged_at", "kind": "timestamp"}

        assert _format_cell("started_at", row, started_meta) == "<window"
        assert _format_cell("merged_at", row, merged_meta) == "-"

    def test_list_of_option_dicts_renders_as_label_colon_command(self) -> None:
        """#2369 review: `decisions`' `options` column is `kind: "list"`
        but holds `{label, command_or_action, ...}` dicts, not the scalar
        strings every other `list` column carries — the old
        `",".join(str(v) for v in value)` rendered a Python dict repr."""
        from coord.commands.report import _format_cell

        row = {
            "options": [
                {
                    "label": "Recommended",
                    "command_or_action": "coord diagnose api 1 --reset",
                    "what_happens": "Runs the fix.",
                    "recommended": True,
                },
                {
                    "label": "Inspect",
                    "command_or_action": "coord escalate list --repo api",
                    "what_happens": "Shows the record.",
                    "recommended": False,
                },
            ]
        }
        meta = {"id": "options", "kind": "list"}
        cell = _format_cell("options", row, meta)
        assert "{" not in cell and "'" not in cell
        assert "Recommended: coord diagnose api 1 --reset" in cell
        assert "Inspect: coord escalate list --repo api" in cell

    def test_render_table_never_prints_window_marker_for_merged_at(self) -> None:
        from coord.commands.report import _render_table
        from coord.reports import ISSUE_ACTIVITY_COLUMN_META, ISSUE_ACTIVITY_COLUMNS

        result = {
            "columns": ISSUE_ACTIVITY_COLUMNS,
            "column_meta": [m.to_dict() for m in ISSUE_ACTIVITY_COLUMN_META],
            "rows": [
                {
                    "repo": "api",
                    "issue": 1629,
                    "title": None,
                    "started_at": None,
                    "started_before_window": True,
                    "machines": ["precision"],
                    "fix_iterations": 1,
                    "counts_partial": True,
                    "test_verdicts": [],
                    "review_verdicts": ["request-changes", "approve"],
                    "merged_at": None,
                    "drive_exit": None,
                    "outcome": "stalled",
                }
            ],
        }
        lines = _render_table(result)
        data_line = lines[1]
        assert data_line.count("<window") == 1


# ── usage (#1763) ──────────────────────────────────────────────────────────
#
# The panel this report replaced was a Rust port of `coord/usage_rollup.py`
# with a *hardcoded* pricing snapshot, so an operator's `pricing:` override
# moved `coord usage` and left the TUI showing different numbers with nothing
# on screen saying which was right. Everything below exists to make that
# divergence impossible to reintroduce: the figures are asserted equal to the
# ones `coord usage --by-issue` computes over the same rows, and an overridden
# rate is asserted to move the estimate.

# A fixed window the fixtures sit inside, so nothing here needs a clock.
_U_NOW = 1_785_000_000.0


def _leg(
    repo: str,
    issue: int,
    *,
    title: str = "",
    stage: str = "work",
    model: str | None = "claude-sonnet-4-6",
    cost_usd: float | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cache_read: int = 0,
    cache_creation: int = 0,
    num_turns: int = 0,
    dispatched_at: float | None = None,
    finished_at: float | None = None,
    for_issue_number: int | None = None,
) -> dict:
    """One board assignment row in the daemon `/board` wire shape."""
    row = {
        "repo_name": repo,
        "issue_number": issue,
        "issue_title": title,
        "type": stage,
        "model": model,
        "cost_usd": cost_usd,
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_creation,
        "num_turns": num_turns,
        "dispatched_at": _U_NOW - 3600 if dispatched_at is None else dispatched_at,
        "finished_at": _U_NOW - 1800 if finished_at is None else finished_at,
    }
    if for_issue_number is not None:
        row["for_issue_number"] = for_issue_number
    return row


def _usage_fixture_rows() -> list[dict]:
    return [
        # api#7: one captured-cost leg + one estimated leg (no cost_usd).
        _leg("api", 7, title="Do a thing", cost_usd=1.5, tokens_in=100, tokens_out=50),
        _leg("api", 7, title="Do a thing", stage="review", model="opus",
             tokens_in=1_000_000, tokens_out=1_000_000),
        # api#9: estimate only.
        _leg("api", 9, title="Another", tokens_in=2_000_000, tokens_out=100_000),
        # web#3: a model with no pricing entry — must be flagged, never $0.
        _leg("web", 3, title="Mystery", model="gpt-hypothetical",
             tokens_in=500, tokens_out=500),
    ]


def _unbounded_window():
    from coord.usage_rollup import TimeWindow

    return TimeWindow(start=None, end=None, label="all")


class TestUsageCatalogue:
    def test_usage_is_in_the_catalogue_alongside_issue_activity(self) -> None:
        ids = [r["id"] for r in catalogue()["reports"]]
        assert "issue-activity" in ids
        assert "usage" in ids

    def test_usage_params_are_window_group_by_and_repo(self) -> None:
        params = {p["id"]: p for p in REPORTS["usage"].to_dict()["params"]}
        assert set(params) == {"window", "group_by", "repo"}
        assert params["window"]["default"] == "today"
        assert params["window"]["choices"] == ["today", "week", "month", "7d", "30d"]
        assert params["group_by"]["default"] == "issue"
        assert params["group_by"]["choices"] == ["issue", "repo"]

    def test_bad_window_names_the_allowed_values(self) -> None:
        with pytest.raises(ReportError) as exc:
            resolve_params(REPORTS["usage"], {"window": "fortnight"})
        assert "today" in str(exc.value) and "30d" in str(exc.value)

    def test_bad_group_by_names_the_allowed_values(self) -> None:
        with pytest.raises(ReportError) as exc:
            resolve_params(REPORTS["usage"], {"group_by": "machine"})
        assert "issue" in str(exc.value) and "repo" in str(exc.value)

    def test_every_column_has_metadata_in_order(self) -> None:
        from coord.reports import fold_usage

        for group_by in ("issue", "repo"):
            result = fold_usage(
                _usage_fixture_rows(), _unbounded_window(), group_by=group_by
            )
            assert [m.id for m in result.column_meta] == result.columns


class TestUsageMatchesCoordUsage:
    """The load-bearing guard: the report and `coord usage --by-issue` must
    agree, because they are the same fold over the same rows priced by the
    same config — that is the whole reason #1763 exists."""

    def test_report_figures_equal_the_coord_usage_aggregate(self) -> None:
        from coord.config import PricingConfig
        from coord.reports import fold_usage
        from coord.usage import pricing_dict_from_config
        from coord.usage_rollup import aggregate

        rows = _usage_fixture_rows()
        window = _unbounded_window()
        pricing = PricingConfig()

        # Path A — what `coord usage --by-issue` computes.
        cli = aggregate(
            rows, by="issue", window=window, pricing=pricing_dict_from_config(pricing)
        )
        # Path B — what `coord report run usage` returns.
        report = fold_usage(rows, window, group_by="issue", pricing=pricing)

        by_issue = {r["issue"]: r for r in report.rows}
        assert len(by_issue) == len(cli["groups"])
        for group in cli["groups"]:
            row = by_issue[group["key"]]
            assert row["legs"] == group["legs"]
            assert row["tokens_in"] == group["tokens"]["input"]
            assert row["tokens_out"] == group["tokens"]["output"]
            assert row["cost_captured"] == pytest.approx(group["cost_captured"])
            assert row["cost_est"] == pytest.approx(group["cost_est"])
            assert row["cost_total"] == pytest.approx(group["cost_total"])

        assert report.totals["cost_total"] == pytest.approx(cli["totals"]["cost_total"])
        assert report.totals["legs"] == cli["totals"]["legs"]

    def test_default_order_is_biggest_spend_first(self) -> None:
        from coord.reports import fold_usage

        result = fold_usage(_usage_fixture_rows(), _unbounded_window())
        costs = [r["cost_total"] for r in result.rows]
        assert costs == sorted(costs, reverse=True)

    def test_captured_cost_is_never_also_estimated(self) -> None:
        from coord.reports import fold_usage

        rows = [_leg("api", 1, cost_usd=2.0, tokens_in=1_000_000, tokens_out=1_000_000)]
        result = fold_usage(rows, _unbounded_window())
        assert result.rows[0]["cost_captured"] == pytest.approx(2.0)
        assert result.rows[0]["cost_est"] == 0.0


class TestUsageSurfacesCacheReadAndTurns:
    """#2786: the column that carries the money.  `tokens_in` was ~0.001% of
    `work`-leg spend while cache-read tokens (priced identically in
    `usage_rollup.leg_cost`) were ~66% of it and never shown — this is the
    fix, plus the persisted `num_turns` that makes "long context" and "many
    turns" distinguishable."""

    def test_cache_read_and_create_and_turns_are_columns(self) -> None:
        from coord.reports import USAGE_ISSUE_COLUMNS, USAGE_REPO_COLUMNS

        for columns in (USAGE_ISSUE_COLUMNS, USAGE_REPO_COLUMNS):
            assert "cache_read" in columns
            assert "cache_create" in columns
            assert "turns" in columns

    def test_tokens_in_stays_raw_input_not_a_sum(self) -> None:
        """A leg whose real cost is dominated by cache reads must not
        silently inflate `tokens_in` — that column keeps meaning exactly
        what it always meant (raw uncached input tokens)."""
        from coord.reports import fold_usage

        rows = [
            _leg(
                "api", 1, cost_usd=None,
                tokens_in=300, tokens_out=5_000,
                cache_read=11_300_000, cache_creation=150_000,
                num_turns=87,
            )
        ]
        result = fold_usage(rows, _unbounded_window())
        row = result.rows[0]
        # The bug: this leg's real cost is dominated by 11.3M cache-read
        # tokens, but `tokens_in` (raw input) stays a tiny 300 — that is
        # correct, not a regression, per the issue's explicit design ("leave
        # tokens_in meaning exactly what it means today").
        assert row["tokens_in"] == 300
        assert row["tokens_out"] == 5_000
        # ...and the number that actually carries the money is now visible
        # alongside it, not hidden.
        assert row["cache_read"] == 11_300_000
        assert row["cache_create"] == 150_000
        assert row["turns"] == 87

    def test_tok_per_turn_is_zero_when_no_turns_recorded(self) -> None:
        """A row predating `num_turns` (reads 0) must not divide by zero."""
        from coord.reports import fold_usage

        rows = [_leg("api", 1, cost_usd=1.0, tokens_in=100, tokens_out=100, cache_read=1_000)]
        result = fold_usage(rows, _unbounded_window())
        assert result.rows[0]["turns"] == 0
        assert result.rows[0]["tok_per_turn"] == 0

    def test_tok_per_turn_is_total_tokens_over_turns(self) -> None:
        from coord.reports import fold_usage

        rows = [
            _leg(
                "api", 1, cost_usd=1.0,
                tokens_in=100, tokens_out=100, cache_read=800, cache_creation=0,
                num_turns=10,
            )
        ]
        result = fold_usage(rows, _unbounded_window())
        # (100 + 100 + 800 + 0) / 10 = 100.
        assert result.rows[0]["tok_per_turn"] == 100

    def test_tokens_in_is_labelled_raw_in_not_tok_in(self) -> None:
        """#2825: "Tok In" next to a full "Tok Out" reads as a matched pair —
        it is not one, `tokens_in` is ~0.001% of real input. The column `id`
        stays `tokens_in` (no historical number or saved sort changes), only
        the display `label` moves."""
        from coord.reports import fold_usage

        result = fold_usage(_usage_fixture_rows(), _unbounded_window())
        meta = {m.id: m for m in result.column_meta}
        assert meta["tokens_in"].label == "Raw In"
        assert meta["tokens_out"].label == "Tok Out"

    def test_totals_row_also_carries_the_new_columns(self) -> None:
        from coord.reports import fold_usage

        rows = _usage_fixture_rows()
        result = fold_usage(rows, _unbounded_window())
        for key in ("cache_read", "cache_create", "turns"):
            assert key in result.totals

    def test_cost_totals_are_unchanged_by_the_new_columns(self) -> None:
        """Display + capture only — no pricing change. The same fixture's
        cost figures must come out byte-identical to what they were before
        cache_read/cache_create/turns existed."""
        from coord.reports import fold_usage

        rows = _usage_fixture_rows()
        result = fold_usage(rows, _unbounded_window())
        assert result.totals["cost_total"] == pytest.approx(
            sum(r["cost_total"] for r in result.rows)
        )
        # api#7 has a captured $1.50 leg plus an estimated opus leg; this is
        # the same figure `TestUsageMatchesCoordUsage` cross-checks against
        # `coord usage --by-issue`'s own aggregate, unaffected by the new
        # columns landing alongside it.
        row7 = next(r for r in result.rows if r.get("issue") == 7)
        assert row7["cost_captured"] == pytest.approx(1.5)


class TestUsagePricingFollowsConfig:
    """#1116's divergence, closed. The panel priced from a compiled-in
    snapshot; this prices from the loaded `PricingConfig`."""

    def test_overriding_a_rate_moves_the_estimate(self) -> None:
        from coord.config import ModelRates, PricingConfig
        from coord.reports import fold_usage

        rows = [_leg("api", 1, model="sonnet", tokens_in=1_000_000, tokens_out=0)]
        window = _unbounded_window()

        default = fold_usage(rows, window, pricing=PricingConfig())
        assert default.rows[0]["cost_est"] == pytest.approx(3.00)

        override = PricingConfig(
            models={"sonnet": ModelRates(input=99.0, output=0.0)}
        )
        moved = fold_usage(rows, window, pricing=override)
        assert moved.rows[0]["cost_est"] == pytest.approx(99.00)
        assert moved.rows[0]["cost_est"] != default.rows[0]["cost_est"]

    def test_pricing_block_in_coordinator_yml_reaches_the_report(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: the rate an operator writes in coordinator.yml is the
        rate the report estimates with — no snapshot in between."""
        from coord.reports import run_usage

        cfg = tmp_path / "coordinator.yml"
        cfg.write_text(
            "repos:\n"
            "  - name: api\n"
            "    github: acme/api\n"
            "machines:\n"
            "  - name: laptop\n"
            "    host: laptop.tail\n"
            "    repos: [api]\n"
            "pricing:\n"
            "  sonnet:\n"
            "    input: 42.0\n"
            "    output: 0.0\n"
            "    cache_read: 0.0\n"
            "    cache_creation: 0.0\n"
        )
        monkeypatch.setenv("COORD_CONFIG", str(cfg))

        rows = [_leg("api", 1, model="sonnet", tokens_in=1_000_000, tokens_out=0)]
        result = run_usage(
            window="30d", group_by="issue", now=_U_NOW, fetch=lambda repo: rows
        )
        assert result.rows[0]["cost_est"] == pytest.approx(42.00)
        assert not any(n.startswith("WARNING") for n in result.notes)

    def test_unloadable_config_says_so_instead_of_silently_defaulting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from coord.reports import run_usage

        monkeypatch.setenv("COORD_CONFIG", str(tmp_path / "does-not-exist.yml"))
        result = run_usage(
            window="30d", now=_U_NOW,
            fetch=lambda repo: [_leg("api", 1, model="sonnet", tokens_in=1_000)],
        )
        assert any("coordinator.yml could not be loaded" in n for n in result.notes)


class TestUsageGrouping:
    def test_group_by_repo_aggregates_across_issues(self) -> None:
        from coord.reports import fold_usage

        result = fold_usage(_usage_fixture_rows(), _unbounded_window(), group_by="repo")
        assert result.columns[0] == "repo"
        assert "issue" not in result.columns
        by_repo = {r["repo"]: r for r in result.rows}
        assert set(by_repo) == {"api", "web"}
        # api#7 (2 legs) + api#9 (1 leg) folded into one row.
        assert by_repo["api"]["legs"] == 3

    def test_repo_param_restricts_to_one_repo(self) -> None:
        from coord.reports import run_usage

        rows = _usage_fixture_rows()
        result = run_usage(
            window="30d", group_by="issue", repo="web", now=_U_NOW,
            fetch=lambda repo: rows, pricing=None,
        )
        assert {r["repo"] for r in result.rows} == {"web"}

    def test_issue_rows_are_repo_scoped(self) -> None:
        """Two repos' issue #5 must stay two rows — GitHub numbers are
        per-repo, and `coordinator.yml` is explicitly multi-repo."""
        from coord.reports import fold_usage

        rows = [_leg("api", 5, cost_usd=1.0), _leg("web", 5, cost_usd=2.0)]
        result = fold_usage(rows, _unbounded_window())
        assert len(result.rows) == 2
        assert {(r["repo"], r["issue"]) for r in result.rows} == {("api", 5), ("web", 5)}

    def test_attributed_issue_wins_over_the_tracking_issue(self) -> None:
        """#1553: a slice authored *for* a child books its spend to the child."""
        from coord.reports import fold_usage

        rows = [_leg("api", 1120, stage="test-author", cost_usd=7.9, for_issue_number=1124)]
        result = fold_usage(rows, _unbounded_window())
        assert [r["issue"] for r in result.rows] == [1124]

    def test_stage_breakdown_rides_along_on_each_row(self) -> None:
        from coord.reports import fold_usage

        result = fold_usage(_usage_fixture_rows(), _unbounded_window())
        api7 = next(r for r in result.rows if r["issue"] == 7)
        assert {s["stage"] for s in api7["stages"]} == {"work", "review"}
        assert sum(s["legs"] for s in api7["stages"]) == api7["legs"]


class TestUsageWindows:
    def test_each_preset_resolves_to_a_bounded_interval(self) -> None:
        from coord.reports import USAGE_WINDOW_CHOICES, resolve_usage_window

        for name in USAGE_WINDOW_CHOICES:
            window = resolve_usage_window(name, _U_NOW)
            assert window.start is not None, name
            assert window.end is not None, name
            assert window.end > window.start, name

    def test_presets_delegate_to_usage_rollup_not_a_local_calendar(self) -> None:
        from coord.reports import resolve_usage_window
        from coord.usage_rollup import window_month, window_today, window_week

        assert resolve_usage_window("today", _U_NOW) == window_today(_U_NOW)
        assert resolve_usage_window("week", _U_NOW) == window_week(_U_NOW)
        assert resolve_usage_window("month", _U_NOW) == window_month(_U_NOW)

    def test_out_of_window_legs_contribute_to_nothing(self) -> None:
        from coord.reports import fold_usage
        from coord.usage_rollup import TimeWindow

        rows = [
            _leg("api", 1, cost_usd=5.0, dispatched_at=100.0, finished_at=200.0),
            _leg("api", 2, cost_usd=9.0, dispatched_at=10_000.0, finished_at=11_000.0),
        ]
        result = fold_usage(rows, TimeWindow(start=0.0, end=1_000.0))
        assert [r["issue"] for r in result.rows] == [1]
        assert result.totals["cost_total"] == pytest.approx(5.0)

    def test_empty_window_says_so_rather_than_rendering_a_bare_header(self) -> None:
        from coord.reports import fold_usage
        from coord.usage_rollup import TimeWindow

        result = fold_usage(_usage_fixture_rows(), TimeWindow(start=0.0, end=1.0))
        assert result.rows == []
        assert any("No usage recorded" in n for n in result.notes)


class TestUsageUnknownModel:
    def test_unpriced_model_is_flagged_in_notes_not_priced_at_zero(self) -> None:
        from coord.reports import fold_usage

        result = fold_usage(_usage_fixture_rows(), _unbounded_window())
        web3 = next(r for r in result.rows if r["repo"] == "web")
        assert web3["unknown_model_legs"] == 1
        assert web3["cost_est"] == 0.0
        # The tokens are still counted — only the *pricing* is unknown.
        assert web3["tokens_in"] == 500
        assert any("web#3" in n and "no entry in the loaded" in n for n in result.notes)

    def test_a_fully_priced_window_produces_no_unknown_model_note(self) -> None:
        from coord.reports import fold_usage

        rows = [_leg("api", 1, model="sonnet", tokens_in=10, tokens_out=10)]
        result = fold_usage(rows, _unbounded_window())
        assert not any("no entry in the loaded" in n for n in result.notes)


class TestUsageTotals:
    def test_totals_is_present_for_usage(self) -> None:
        from coord.reports import fold_usage

        result = fold_usage(_usage_fixture_rows(), _unbounded_window())
        assert result.totals is not None
        assert result.totals["legs"] == 4
        assert result.totals["cost_total"] == pytest.approx(
            sum(r["cost_total"] for r in result.rows)
        )
        # Identity columns are deliberately absent — the renderer picks its
        # own marker (Σ) rather than the wire inventing a fake value.
        assert "issue" not in result.totals
        assert "repo" not in result.totals

    def test_totals_is_none_for_issue_activity(self) -> None:
        result = fold_issue_activity([], WINDOW)
        assert result.totals is None
        assert result.to_dict()["totals"] is None

    def test_totals_is_none_for_drive_queue_status(self) -> None:
        result = fold_drive_queue_status([], T0)
        assert result.totals is None

    def test_the_totals_key_is_additive_and_nothing_else_moved(self) -> None:
        """Compatibility guard for the already-merged #1741 panel: adding
        `totals` must not change any other field of an existing report."""
        before = {
            "report_id", "generated_at", "window", "columns",
            "column_meta", "rows", "notes",
        }
        payload = fold_issue_activity([], WINDOW).to_dict()
        assert set(payload) == before | {"totals", "chart"}

    def test_cli_table_renders_the_totals_row(self) -> None:
        from coord.commands.report import _render_table
        from coord.reports import fold_usage

        result = fold_usage(_usage_fixture_rows(), _unbounded_window()).to_dict()
        lines = _render_table(result)
        assert lines[-1].lstrip().startswith("Σ")

    def test_cli_table_omits_the_totals_row_when_absent(self) -> None:
        from coord.commands.report import _render_table

        lines = _render_table(
            {
                "columns": ["a", "b"],
                "rows": [{"a": 1, "b": 2}],
                "notes": [],
            }
        )
        assert len(lines) == 2


class TestUsageEndToEnd:
    def test_report_run_usage_prints_a_table(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from coord.reports import fold_usage

        fixture = fold_usage(_usage_fixture_rows(), _unbounded_window()).to_dict()
        monkeypatch.setattr(state, "run_report", lambda rid, params=None: fixture)
        result = CliRunner().invoke(
            main, ["report", "run", "usage", "--param", "window=today"]
        )
        assert result.exit_code == 0, result.output
        assert "Do a thing" in result.output
        assert "Σ" in result.output

    def test_daemon_endpoint_serves_usage(
        self, report_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = _usage_fixture_rows()
        monkeypatch.setattr("coord.reports._default_usage_rows", lambda repo: rows)
        listed = report_client.get("/report").json()
        assert "usage" in [r["id"] for r in listed["reports"]]
        body = report_client.get(
            "/report/usage", params={"window": "30d", "group_by": "repo"}
        ).json()
        assert body["report_id"] == "usage"
        assert body["totals"] is not None
        assert body["columns"][0] == "repo"

    def test_daemon_rejects_a_bad_usage_param(self, report_client: TestClient) -> None:
        resp = report_client.get("/report/usage", params={"group_by": "machine"})
        assert resp.status_code == 400


# ── CSV export (#1765) ─────────────────────────────────────────────────────

# #1631's real driver-exit reason, extended with the comma-and-newline shape
# that hand-rolled CSV writers get wrong. This is the escaping fixture: it
# has to survive `result_to_csv` → `csv.reader` unchanged.
NASTY_REASON = (
    'merge attempted 3 times without landing, last error: "conflict"\n'
    "  hint: rebase onto origin/main and re-run"
)


def _csv_fixture_result() -> ReportResult:
    """A one-row `ReportResult` carrying every cell shape the serializer has
    to handle: raw epoch, list, bool, None, int, and a dict whose free text
    contains a comma, a quote and a newline."""
    return ReportResult(
        report_id="issue-activity",
        generated_at=WINDOW[1],
        window=(WINDOW[0], WINDOW[1]),
        columns=[
            "repo", "issue", "started_at", "machines", "fix_iterations",
            "test_verdicts", "merged_at", "counts_partial", "drive_exit",
        ],
        rows=[
            {
                "repo": "api",
                "issue": 1631,
                "started_at": WINDOW[0] + 400,
                "machines": ["dellserver", "precision"],
                "fix_iterations": 3,
                "test_verdicts": ["failed", "passed"],
                "merged_at": None,
                "counts_partial": True,
                "drive_exit": {
                    "at": WINDOW[0] + 500,
                    "exit_code": 1,
                    "reason": NASTY_REASON,
                },
            }
        ],
        notes=[
            "api#1631: driver exited exit_code=1 but the PR merged anyway",
            "a note that\nspans two lines",
        ],
        column_meta=[
            ColumnMeta(id="repo", label="Repo", kind="text"),
            ColumnMeta(id="issue", label="Issue", kind="int", align="right"),
            ColumnMeta(id="started_at", label="Started", kind="timestamp"),
            ColumnMeta(id="machines", label="Machines", kind="list"),
        ],
    )


def _parse_csv(text: str) -> list[list[str]]:
    """Parse CSV that has leading `#` comment lines.

    Strips only the *leading* comment block and hands the rest to
    `csv.reader` whole — never a per-line `startswith('#')` filter, which
    would corrupt a quoted field whose embedded newline is followed by a
    `#`.
    """
    lines = text.splitlines(keepends=True)
    body_at = 0
    for i, line in enumerate(lines):
        if not line.startswith("#"):
            body_at = i
            break
    return list(csv.reader(io.StringIO("".join(lines[body_at:]))))


class TestCsvSerializer:
    def test_header_row_uses_column_meta_labels_and_falls_back_to_keys(self) -> None:
        rows = _parse_csv(result_to_csv(_csv_fixture_result()))
        # Labelled columns use `column_meta.label` (#1760); the rest degrade
        # gracefully to the raw column key rather than vanishing.
        assert rows[0] == [
            "Repo", "Issue", "Started", "Machines", "fix_iterations",
            "test_verdicts", "merged_at", "counts_partial", "drive_exit",
        ]

    def test_one_row_per_row(self) -> None:
        rows = _parse_csv(result_to_csv(_csv_fixture_result()))
        assert len(rows) == 2  # header + one data row

    def test_list_of_option_dicts_exports_as_label_colon_command(self) -> None:
        """#2369 review: `_csv_scalar`'s `str(value)` fallback rendered a
        `decisions`-style `options` list of `{label, command_or_action,
        ...}` dicts as a Python dict repr — `format_option_cell` fixes it
        for CSV the same way it does for the CLI table."""
        result = ReportResult(
            report_id="decisions",
            generated_at=WINDOW[1],
            window=WINDOW,
            columns=["issue", "options"],
            rows=[
                {
                    "issue": 2360,
                    "options": [
                        {
                            "label": "Recommended",
                            "command_or_action": "coord diagnose api 1 --reset",
                            "what_happens": "Runs the fix.",
                            "recommended": True,
                        },
                    ],
                }
            ],
            notes=[],
        )
        rows = _parse_csv(result_to_csv(result))
        cell = rows[1][rows[0].index("options")]
        assert "{" not in cell and "'" not in cell
        assert "Recommended: coord diagnose api 1 --reset" in cell

    def test_started_at_exports_as_the_raw_epoch_not_a_relative_string(self) -> None:
        """The whole reason the serializer is server-side: an epoch must
        survive as a number a spreadsheet can sort, not as `13h ago`."""
        rows = _parse_csv(result_to_csv(_csv_fixture_result()))
        started = rows[1][rows[0].index("Started")]
        assert float(started) == WINDOW[0] + 400
        assert "ago" not in started

    def test_list_cell_is_one_field_joined_with_semicolons(self) -> None:
        text = result_to_csv(_csv_fixture_result())
        rows = _parse_csv(text)
        assert rows[1][rows[0].index("Machines")] == "dellserver; precision"
        # One field, not two columns: every row is as wide as the header.
        assert len(rows[1]) == len(rows[0])
        # And the field is quoted only when it needs to be — `; ` doesn't.
        assert "dellserver; precision" in text

    def test_nasty_drive_exit_reason_round_trips_through_csv_reader(self) -> None:
        """#1631's reason with a comma, a quote AND a newline comes back
        byte-identical — the escaping regression test."""
        rows = _parse_csv(result_to_csv(_csv_fixture_result()))
        cell = rows[1][rows[0].index("drive_exit")]
        assert cell.endswith(f"reason={NASTY_REASON}")
        assert NASTY_REASON in cell
        # The embedded newline stayed inside one cell rather than spilling
        # into an extra row.
        assert len(rows) == 2

    def test_null_is_empty_and_bool_is_true_false(self) -> None:
        rows = _parse_csv(result_to_csv(_csv_fixture_result()))
        assert rows[1][rows[0].index("merged_at")] == ""
        assert rows[1][rows[0].index("counts_partial")] == "true"

    def test_every_note_appears_as_a_comment_line(self) -> None:
        result = _csv_fixture_result()
        text = result_to_csv(result)
        comments = [l for l in text.splitlines() if l.startswith("#")]
        assert any("1631" in c and "merged anyway" in c for c in comments)
        # A multi-line note gets one `#` per physical line, so no fragment
        # can escape into the data and be read as a row.
        assert "# a note that" in comments
        assert "# spans two lines" in comments
        # ...and the file still parses once the comment block is skipped.
        assert _parse_csv(text)[0][0] == "Repo"

    def test_report_id_and_window_are_in_the_comment_header(self) -> None:
        text = result_to_csv(_csv_fixture_result())
        assert text.startswith("# report: issue-activity\n")
        assert "# window: " in text

    def test_notes_are_not_rows(self) -> None:
        rows = _parse_csv(result_to_csv(_csv_fixture_result()))
        assert all("merged anyway" not in cell for row in rows for cell in row)

    def test_accepts_the_dict_wire_form_identically(self) -> None:
        """The CLI holds a `to_dict()` result off the wire, the daemon holds
        a `ReportResult`; both must serialise to the same bytes."""
        result = _csv_fixture_result()
        assert result_to_csv(result) == result_to_csv(result.to_dict())

    def test_a_result_with_no_rows_is_still_a_header(self) -> None:
        result = ReportResult(
            report_id="issue-activity",
            generated_at=WINDOW[1],
            window=WINDOW,
            columns=["repo", "issue"],
            rows=[],
            notes=[],
        )
        rows = _parse_csv(result_to_csv(result))
        assert rows == [["repo", "issue"]]

    def test_totals_ride_along_as_a_final_flagged_row(self) -> None:
        """#1763's grand total exports as the last row, announced in the
        comments so it can't be mistaken for another data row."""
        result = ReportResult(
            report_id="usage",
            generated_at=WINDOW[1],
            window=WINDOW,
            columns=["issue", "cost"],
            rows=[{"issue": 1, "cost": 0.5}],
            notes=[],
            totals={"cost": 0.5},
        )
        text = result_to_csv(result)
        assert "# totals:" in text
        rows = _parse_csv(text)
        assert rows[-1] == ["", "0.5"]

    def test_a_report_without_totals_says_nothing_about_them(self) -> None:
        assert "# totals:" not in result_to_csv(_csv_fixture_result())

    def test_filename_is_derived_from_the_result_not_the_clock(self) -> None:
        name = csv_filename(_csv_fixture_result())
        assert name.startswith("issue-activity-")
        assert name.endswith(".csv")
        # Same result → same name, however long you wait.
        assert name == csv_filename(_csv_fixture_result())


class TestCsvCli:
    def test_report_run_format_csv(self, coord_db) -> None:
        _seed_known_good_window(coord_db)
        result = CliRunner().invoke(
            main,
            ["report", "run", "issue-activity", "--param", "since=13h",
             "--format", "csv"],
        )
        assert result.exit_code == 0, result.output
        rows = _parse_csv(result.output)
        # Header + one row per issue in the seeded window.
        assert rows[0][:2] == ["Repo", "Issue"]
        assert {r[1] for r in rows[1:]} == {"1629", "1631", "1728", "1729"}

    def test_csv_started_at_is_an_epoch(self, coord_db) -> None:
        _seed_known_good_window(coord_db)
        result = CliRunner().invoke(
            main,
            ["report", "run", "issue-activity", "--param", "since=13h",
             "--format", "csv"],
        )
        rows = _parse_csv(result.output)
        started = rows[0].index("Started")
        assert float(rows[1][started]) > 1_000_000_000

    def test_csv_carries_the_notes_as_comment_lines(self, coord_db) -> None:
        _seed_known_good_window(coord_db)
        result = CliRunner().invoke(
            main,
            ["report", "run", "issue-activity", "--param", "since=13h",
             "--format", "csv"],
        )
        assert any(
            l.startswith("#") and "1631" in l for l in result.output.splitlines()
        )

    def test_json_flag_is_still_accepted_as_an_alias(self, coord_db) -> None:
        _seed_known_good_window(coord_db)
        legacy = CliRunner().invoke(
            main,
            ["report", "run", "issue-activity", "--param", "since=13h", "--json"],
        )
        explicit = CliRunner().invoke(
            main,
            ["report", "run", "issue-activity", "--param", "since=13h",
             "--format", "json"],
        )
        assert legacy.exit_code == 0, legacy.output
        assert explicit.exit_code == 0, explicit.output
        assert json.loads(legacy.output) == json.loads(explicit.output)

    def test_json_flag_is_hidden_from_help(self, coord_db) -> None:
        result = CliRunner().invoke(main, ["report", "run", "--help"])
        assert result.exit_code == 0, result.output
        assert "--format" in result.output
        assert "--json" not in result.output

    def test_default_output_is_still_the_human_table(self, coord_db) -> None:
        _seed_known_good_window(coord_db)
        result = CliRunner().invoke(
            main, ["report", "run", "issue-activity", "--param", "since=13h"]
        )
        assert result.exit_code == 0, result.output
        assert "OUTCOME" in result.output
        assert "# report:" not in result.output

    def test_bad_format_is_a_usage_error(self, coord_db) -> None:
        result = CliRunner().invoke(
            main, ["report", "run", "issue-activity", "--format", "xlsx"]
        )
        assert result.exit_code != 0
        assert "xlsx" in result.output


class TestCsvEndpoint:
    def test_format_csv_returns_text_csv_with_a_filename(
        self, report_client: TestClient, rw_db
    ) -> None:
        _seed_known_good_window(rw_db)
        resp = report_client.get(
            "/report/issue-activity", params={"since": "13h", "format": "csv"}
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        disposition = resp.headers["content-disposition"]
        assert disposition.startswith("attachment; filename=")
        assert "issue-activity-" in disposition and ".csv" in disposition
        rows = _parse_csv(resp.text)
        assert {r[1] for r in rows[1:]} == {"1629", "1631", "1728", "1729"}

    def test_absent_format_is_byte_identical_json_to_before(
        self, report_client: TestClient, rw_db
    ) -> None:
        """Compatibility guard for the merged #1741 panel: adding `format`
        must not change the default response at all."""
        _seed_known_good_window(rw_db)
        until = repr(WINDOW[1])
        plain = report_client.get(
            "/report/issue-activity", params={"since": "13h", "until": until}
        )
        explicit = report_client.get(
            "/report/issue-activity",
            params={"since": "13h", "until": until, "format": "json"},
        )
        assert plain.headers["content-type"].startswith("application/json")
        assert plain.content == explicit.content

    def test_cli_csv_and_daemon_csv_are_byte_identical(
        self, report_client: TestClient, rw_db
    ) -> None:
        _seed_known_good_window(rw_db)
        until = repr(WINDOW[1])
        endpoint = report_client.get(
            "/report/issue-activity",
            params={"since": "13h", "until": until, "format": "csv"},
        ).text
        cli = CliRunner().invoke(
            main,
            ["report", "run", "issue-activity", "--param", "since=13h",
             "--param", f"until={until}", "--format", "csv"],
        )
        assert cli.exit_code == 0, cli.output
        assert cli.output == endpoint

    def test_format_is_not_treated_as_a_report_parameter(
        self, report_client: TestClient, rw_db
    ) -> None:
        """`resolve_params` rejects unknown parameters — `format` is a
        rendering choice and must be popped before it gets there."""
        resp = report_client.get(
            "/report/issue-activity", params={"since": "13h", "format": "csv"}
        )
        assert resp.status_code == 200

    def test_unknown_format_is_a_400_naming_what_was_allowed(
        self, report_client: TestClient
    ) -> None:
        resp = report_client.get(
            "/report/issue-activity", params={"format": "xlsx"}
        )
        assert resp.status_code == 400
        assert "csv" in resp.json()["error"]

    def test_csv_route_still_requires_auth(
        self, daemon_db: Path, valid_config_path: Path, rw_db
    ) -> None:
        app = build_app(
            SqliteStore(daemon_db), load_config(valid_config_path), token="s3cret"
        )
        with TestClient(app) as cli:
            assert cli.get(
                "/report/issue-activity", params={"format": "csv"}
            ).status_code == 401


# ── queue-outcomes (#2270) ─────────────────────────────────────────────────
#
# The one number the morning report is for: what fraction of the queue got
# over the line without a human.  Three things are load-bearing enough to be
# pinned rather than eyeballed — the bucket counts (including the `by_design`
# split and a category the fixture INVENTS, which the report has never seen);
# the refusal to answer at all on a host with no block log (a table of zeros
# there reads as a perfect week); and attributability, i.e. every count
# drilling back to the exact `(repo, issue)` list behind it.

QO_END = T0 + 13 * 3600  # == WINDOW[1], the frozen clock
QO_DAY = 86400.0


def _abs_iso(ts: float) -> str:
    """The same UTC rendering `coord.reports._iso` puts in a note."""
    from coord.reports import _iso

    return _iso(ts)


def _episode(
    key: str,
    *,
    entered_at: float,
    resolved_at: float | None = None,
    true_cause: str = "",
    human_acted: bool | None = None,
    stated_reason: str = "exhausted",
    state: str = "blocked",
    source: str = "tick",
) -> dict:
    """One paired episode, in exactly the shape `block_log.episodes` returns."""
    episode = {
        "key": key,
        "state": state,
        "stated_reason": stated_reason,
        "entered_at": entered_at,
        "attempts": 2,
        "host": "dellserver",
        "resolved": resolved_at is not None,
        "resolution": "",
        "true_cause": true_cause,
        "human_acted": human_acted,
        "resolved_at": resolved_at,
        "stalled_seconds": None if resolved_at is None else resolved_at - entered_at,
        "diagnoses": 0,
        "diagnosed_cause": "",
        "diagnosis_confidence": "",
        "diagnosis_evidence": [],
        "diagnosis_contradicts_stated": False,
        "agreement": "",
    }
    if resolved_at is not None:
        episode["source"] = source
    return episode


def _qo_fixture() -> list[dict]:
    """The pinned fixture: one episode per bucket the log can produce, plus a
    cause this build has never met.

    Deliberately includes BOTH kinds of human stall — the Gate-A sign-off
    (which is supposed to stop for a person) and an operator `remove` (which
    is not) — because collapsing them is the failure mode #2270 names: a
    target metric that can never reach 100% and so reads as permanent failure.
    """
    return [
        # auto, a deterministic arm (#2230's gate re-check).
        _episode(
            "api#1",
            entered_at=QO_END - 7200,
            resolved_at=QO_END - 3600,
            true_cause="gate-cleared-after-giveup — the merge gate read clear again",
            human_acted=False,
            stated_reason="CI red, 2/2 attempts",
        ),
        # auto, the rescue AGENT — structurally impossible today; the fixture
        # forges the source so the series is proven to exist before #2268 does.
        _episode(
            "api#2",
            entered_at=QO_END - 7000,
            resolved_at=QO_END - 3500,
            true_cause="dead-leg — the leg was dead, not slow",
            human_acted=False,
            source="rescue",
        ),
        # human, BY DESIGN: a Gate-A sign-off (#2063).
        _episode(
            "api#3",
            entered_at=QO_END - 6800,
            resolved_at=QO_END - 3400,
            true_cause="gate-a-signed — released only because a human recorded the sign-off",
            human_acted=True,
            state="parked",
            stated_reason="Gate A not approved",
        ),
        # human, a real defect: the operator cleared it by hand.
        _episode(
            "shared#4",
            entered_at=QO_END - 6600,
            resolved_at=QO_END - 3300,
            true_cause="operator-intervened — a human cleared it by hand",
            human_acted=True,
        ),
        # open, with a cause NOTHING in this codebase defines (#2276 could
        # diagnose it tomorrow). It must survive to the row as itself.
        _episode(
            "shared#5",
            entered_at=QO_END - 6400,
            true_cause="solar-flare — a category the fixture invented",
        ),
    ]


def _row(result, bucket: str, category: str) -> dict:
    rows = [
        r for r in result.rows
        if r["bucket"] == bucket and r["category"] == category
    ]
    assert len(rows) == 1, f"expected one {bucket}/{category} row, got {rows}"
    return rows[0]


class TestQueueOutcomesFold:
    def test_every_bucket_count_is_pinned(self) -> None:
        result = fold_queue_outcomes(
            _qo_fixture(),
            (QO_END - QO_DAY, QO_END),
            merged=[("api#9", QO_END - 1000), ("api#1", QO_END - 3600)],
        )
        assert result.report_id == "queue-outcomes"
        counts = {(r["bucket"], r["category"]): r["count"] for r in result.rows}
        assert counts == {
            # api#9 merged and never stalled. api#1 ALSO merged, but it
            # stalled first — it is auto_resolved, not succeeded, and must
            # not be counted twice.
            ("succeeded", "merged"): 1,
            ("auto_resolved_mechanism", "gate-cleared-after-giveup"): 1,
            ("auto_resolved_rescue", "dead-leg"): 1,
            ("human", "gate-a-signed"): 1,
            ("human", "operator-intervened"): 1,
            ("open", "solar-flare"): 1,
        }
        assert result.totals == {"count": 6, "share_pct": 100.0}

    def test_a_category_the_report_has_never_seen_survives_as_itself(self) -> None:
        result = fold_queue_outcomes(_qo_fixture(), (QO_END - QO_DAY, QO_END))
        # Not "other", not dropped, not an error: the category vocabulary is
        # the `true_cause` vocabulary, and that is read from the data.
        assert _row(result, "open", "solar-flare")["count"] == 1

    def test_the_by_design_split_is_carried_on_the_row_and_the_headline(self) -> None:
        result = fold_queue_outcomes(
            _qo_fixture(),
            (QO_END - QO_DAY, QO_END),
            merged=[("api#9", QO_END - 1000)],
        )
        assert _row(result, "human", "gate-a-signed")["by_design"] is True
        assert _row(result, "human", "operator-intervened")["by_design"] is False
        headline = next(n for n in result.notes if n.startswith("headline:"))
        # 3 of 6 auto (succeeded + mechanism + rescue).
        assert "50.0% got over the line without a human (3/6)" in headline
        # Excluding the one that is SUPPOSED to stop for a person: 3/5.
        assert "BY DESIGN" in headline
        assert "60.0% (3/5)" in headline

    @pytest.mark.parametrize(
        "reason",
        [
            # The queue's OWN markers — read through its own predicates, so a
            # rename in either place breaks this test rather than silently
            # reclassifying a by-design stop as a defect.
            "Gate A not approved " + park_marker("api", 51),
            f"blocked: coordinator-owned docs {POLICY_REFUSAL_MARKER}",
        ],
    )
    def test_an_undiagnosed_by_design_stall_still_reads_as_by_design(
        self, reason: str
    ) -> None:
        """The marker lives in the reason the queue itself stamped, so this
        works before #2276 has been anywhere near the episode."""
        result = fold_queue_outcomes(
            [
                _episode(
                    "api#7",
                    entered_at=QO_END - 5000,
                    state="parked",
                    stated_reason=reason,
                )
            ],
            (QO_END - QO_DAY, QO_END),
        )
        assert _row(result, "open", "(unresolved)")["by_design"] is True

    def test_rows_are_attributable_to_the_issues_behind_them(self) -> None:
        result = fold_queue_outcomes(
            [
                _episode("api#1", entered_at=QO_END - 900),
                _episode("shared#2", entered_at=QO_END - 800),
            ],
            (QO_END - QO_DAY, QO_END),
        )
        row = _row(result, "open", "(unresolved)")
        assert row["count"] == 2
        assert row["issues"] == ["api#1", "shared#2"]

    def test_an_empty_window_keeps_its_columns_and_says_it_is_empty(self) -> None:
        result = fold_queue_outcomes((), (QO_END - QO_DAY, QO_END))
        assert result.rows == []
        assert result.columns == [
            "period_start", "bucket", "category", "by_design", "count",
            "share_pct", "issues",
        ]
        assert [m.id for m in result.column_meta] == result.columns
        assert result.totals is None
        assert any("not a 100% score" in n for n in result.notes)

    def test_a_stall_that_predates_the_window_and_is_still_open_is_counted(self) -> None:
        """The most flattering bug available to this report is dropping the
        oldest unresolved stalls off the back of the window."""
        result = fold_queue_outcomes(
            [_episode("api#1", entered_at=QO_END - 30 * QO_DAY)],
            (QO_END - QO_DAY, QO_END),
        )
        assert _row(result, "open", "(unresolved)")["count"] == 1
        assert result.rows[0]["period_start"] == QO_END - QO_DAY
        assert any("stalled before this window opened" in n for n in result.notes)

    def test_an_episode_outside_the_window_is_excluded(self) -> None:
        result = fold_queue_outcomes(
            [
                _episode(
                    "api#1",
                    entered_at=QO_END - 40 * QO_DAY,
                    resolved_at=QO_END - 30 * QO_DAY,
                    true_cause="ci-reported — x",
                    human_acted=False,
                )
            ],
            (QO_END - QO_DAY, QO_END),
        )
        assert result.rows == []

    def test_the_rescue_series_is_modelled_even_when_structurally_zero(self) -> None:
        result = fold_queue_outcomes(
            [
                _episode(
                    "api#1",
                    entered_at=QO_END - 900,
                    resolved_at=QO_END - 800,
                    true_cause="ci-reported — x",
                    human_acted=False,
                )
            ],
            (QO_END - QO_DAY, QO_END),
        )
        assert any("auto_resolved_rescue` is 0" in n for n in result.notes)
        assert any("#2268" in n for n in result.notes)

    def test_undiagnosed_stalls_are_called_out_not_folded_into_a_cause(self) -> None:
        result = fold_queue_outcomes(
            [_episode("api#1", entered_at=QO_END - 900)],
            (QO_END - QO_DAY, QO_END),
        )
        assert _row(result, "open", "(unresolved)")["count"] == 1
        assert any("coord drive-queue diagnose" in n for n in result.notes)


class TestQueueOutcomesWindows:
    def test_the_three_presets_resolve_to_their_spans_and_periods(self) -> None:
        assert resolve_queue_outcomes_window("24h", QO_END) == (
            QO_END - QO_DAY, QO_END, QO_DAY,
        )
        assert resolve_queue_outcomes_window("7d", QO_END) == (
            QO_END - 7 * QO_DAY, QO_END, QO_DAY,
        )
        assert resolve_queue_outcomes_window("4w", QO_END) == (
            QO_END - 28 * QO_DAY, QO_END, 7 * QO_DAY,
        )

    def test_an_unknown_window_names_the_allowed_values(self) -> None:
        with pytest.raises(ReportError) as exc:
            resolve_queue_outcomes_window("13h", QO_END)
        for allowed in ("24h", "7d", "4w"):
            assert allowed in str(exc.value)

    def test_7d_is_the_same_arithmetic_in_seven_daily_periods(self) -> None:
        start, end, period = resolve_queue_outcomes_window("7d", QO_END)
        episodes = [
            _episode(
                f"api#{i}",
                entered_at=end - (i + 0.5) * QO_DAY,
                resolved_at=end - (i + 0.4) * QO_DAY,
                true_cause="ci-reported — x",
                human_acted=False,
            )
            for i in range(7)
        ]
        result = fold_queue_outcomes(
            episodes, (start, end), period_seconds=period
        )
        # One point per day — the trendline, without a wire-contract change:
        # a client groups rows on `period_start`.
        assert len({r["period_start"] for r in result.rows}) == 7
        assert sorted(r["period_start"] for r in result.rows) == [
            start + i * QO_DAY for i in range(7)
        ]
        assert all(r["count"] == 1 for r in result.rows)
        # A per-period headline line for each period, plus the overall one.
        assert sum(1 for n in result.notes if "got over the line" in n) == 8


class TestQueueOutcomesRunner:
    def test_a_host_with_no_block_log_refuses_to_report_a_perfect_score(self) -> None:
        calls: list[dict] = []

        def _fetch(**kwargs):
            calls.append(kwargs)
            return {"entries": [], "has_more": False}

        result = run_queue_outcomes(
            now=QO_END,
            location={"path": "/nope/queue-block-log.jsonl", "host": "laptop", "exists": False},
            episode_source=lambda: [],
            fetch=_fetch,
        )
        assert result.rows == []
        assert result.columns[0] == "period_start"  # columns intact
        note = " ".join(result.notes)
        assert "NO BLOCK LOG ON THIS HOST" in note
        assert "/nope/queue-block-log.jsonl" in note
        assert "laptop" in note
        # And it does not go on to price the window off half a source.
        assert calls == []

    def test_the_source_host_and_path_are_always_named(self) -> None:
        result = run_queue_outcomes(
            now=QO_END,
            location={"path": "/var/log/qbl.jsonl", "host": "dellserver", "exists": True},
            episode_source=_qo_fixture,
            fetch=lambda **kw: {"entries": [], "has_more": False},
        )
        assert result.notes[0] == "source: the block log on dellserver (/var/log/qbl.jsonl)."

    def test_the_repo_param_filters_episodes_and_the_merge_read(self) -> None:
        seen: list[dict] = []

        def _fetch(**kwargs):
            seen.append(kwargs)
            return {"entries": [], "has_more": False}

        result = run_queue_outcomes(
            repo="api",
            now=QO_END,
            location={"path": "p", "host": "h", "exists": True},
            episode_source=_qo_fixture,
            fetch=_fetch,
        )
        assert all(
            i.startswith("api#") for r in result.rows for i in r["issues"]
        )
        assert seen[0]["repo"] == "api"
        # The merge read is pushed down to the audit filter rather than
        # paging four weeks of every event to keep a handful.
        assert seen[0]["category"] == "merge"
        assert seen[0]["event_type"] == "merged"

    def test_an_unreadable_audit_trail_is_a_loud_note_not_a_dead_report(self) -> None:
        def _boom(**kwargs):
            raise RuntimeError("no such table: audit_log")

        result = run_queue_outcomes(
            now=QO_END,
            location={"path": "p", "host": "h", "exists": True},
            episode_source=_qo_fixture,
            fetch=_boom,
        )
        assert result.rows  # the block-log half still reported
        assert any("`succeeded` bucket" in n and "MISSING" in n for n in result.notes)


class TestQueueOutcomesCatalogue:
    def test_the_catalogue_entry_shows_its_params(self) -> None:
        rep = next(
            r for r in catalogue()["reports"] if r["id"] == "queue-outcomes"
        )
        assert rep["title"] == "Queue Outcomes"
        assert rep["description"]
        params = {p["id"]: p for p in rep["params"]}
        assert set(params) == {"window", "until", "repo"}
        assert params["window"]["choices"] == ["24h", "7d", "4w"]
        assert params["window"]["default"] == "24h"
        # #2270: follow issue-activity's existing since/until convention
        # rather than inventing a new one.
        assert params["until"]["kind"] == "text"

    def test_a_bad_window_is_a_clean_error_naming_the_allowed_values(self) -> None:
        with pytest.raises(ReportError) as exc:
            resolve_params(REPORTS["queue-outcomes"], {"window": "13h"})
        for allowed in ("24h", "7d", "4w"):
            assert allowed in str(exc.value)


class TestQueueOutcomesCli:
    """Black-box: drive `coord report run queue-outcomes` over a seeded log
    and assert on the rendered output (#2270's acceptance)."""

    @pytest.fixture()
    def seeded_log(self, tmp_path: Path, monkeypatch) -> Path:
        from coord.block_log import EVENT_ENTER as _ENTER  # noqa: N806
        from coord.block_log import EVENT_RESOLVE as _RESOLVE  # noqa: N806

        path = tmp_path / "queue-block-log.jsonl"
        records = [
            # auto: stalled, then a deterministic arm cleared it.
            {"event": _ENTER, "ts": QO_END - 7200, "key": "api#1",
             "state": "blocked", "stated_reason": "CI red, 2/2 attempts"},
            {"event": _RESOLVE, "ts": QO_END - 3600, "key": "api#1",
             "state": "waiting", "resolution": "auto_resumed", "source": "tick",
             "true_cause": "gate-cleared-after-giveup — the gate read clear again",
             "human_acted": False},
            # human, by design: the Gate-A sign-off.
            {"event": _ENTER, "ts": QO_END - 7000, "key": "api#2",
             "state": "parked", "stated_reason": "Gate A not approved"},
            {"event": _RESOLVE, "ts": QO_END - 3000, "key": "api#2",
             "state": "waiting", "resolution": "auto_resumed", "source": "tick",
             "true_cause": "gate-a-signed — a human recorded the sign-off",
             "human_acted": True},
            # open, with a cause this build has never seen.
            {"event": _ENTER, "ts": QO_END - 5000, "key": "shared#3",
             "state": "blocked", "stated_reason": "stale test verdict"},
            {"event": "diagnosis", "ts": QO_END - 4000, "key": "shared#3",
             "cause": "solar-flare", "confidence": "low", "evidence": [],
             "true_cause": "solar-flare — a cause this build has never seen"},
        ]
        path.write_text(
            "".join(json.dumps(r, sort_keys=True) + "\n" for r in records)
        )
        monkeypatch.setenv("COORD_BLOCK_LOG", str(path))
        return path

    def test_the_table_and_the_headline_render(self, coord_db, seeded_log) -> None:
        # One merge with no stall at all — the `succeeded` bucket, which the
        # block log cannot see and the audit trail can.
        record_audit(
            tier="business", category="merge", event_type="merged",
            actor="coordinator", summary="m", repo="api", issue=9,
            ts=QO_END - 1000,
        )
        result = CliRunner().invoke(
            main, ["report", "run", "queue-outcomes", "--param", "window=24h"]
        )
        assert result.exit_code == 0, result.output
        assert "queue-outcomes" in result.output
        for bucket in ("succeeded", "auto_resolved_mechanism", "human", "open"):
            assert bucket in result.output
        # The invented category survives to the rendered table.
        assert "solar-flare" in result.output
        # 2 of 4 without a human; 2 of 3 once the by-design Gate A is out.
        assert "50.0% got over the line without a human (2/4)" in result.output
        assert "66.7% (2/3)" in result.output
        # Attributable, right there in the table.
        assert "api#9" in result.output
        assert str(seeded_log) in result.output

    def test_json_carries_the_whole_issue_list_per_row(
        self, coord_db, seeded_log
    ) -> None:
        result = CliRunner().invoke(
            main,
            ["report", "run", "queue-outcomes", "--param", "window=7d",
             "--format", "json"],
        )
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert body["report_id"] == "queue-outcomes"
        assert body["window"] == [QO_END - 7 * QO_DAY, QO_END]
        by_bucket = {r["bucket"]: r for r in body["rows"]}
        assert by_bucket["human"]["issues"] == ["api#2"]
        assert by_bucket["human"]["by_design"] is True
        assert by_bucket["open"]["category"] == "solar-flare"

    def test_a_window_with_no_data_is_an_empty_result_not_an_error(
        self, coord_db, seeded_log
    ) -> None:
        # Every seeded episode predates this window's start by weeks — and
        # `until` is the same param name `issue-activity` uses.
        result = CliRunner().invoke(
            main,
            ["report", "run", "queue-outcomes", "--param", "window=24h",
             "--param", f"until={QO_END - 60 * QO_DAY}"],
        )
        assert result.exit_code == 0, result.output
        assert "(no activity in this window)" in result.output
        assert "not a 100% score" in result.output

    def test_a_host_without_the_log_says_so_rather_than_scoring_zero(
        self, coord_db, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setenv("COORD_BLOCK_LOG", str(tmp_path / "absent.jsonl"))
        result = CliRunner().invoke(
            main, ["report", "run", "queue-outcomes", "--param", "window=24h"]
        )
        assert result.exit_code == 0, result.output
        assert "NO BLOCK LOG ON THIS HOST" in result.output
        assert "absent.jsonl" in result.output

    def test_csv_export_carries_the_rows_and_the_notes(
        self, coord_db, seeded_log
    ) -> None:
        result = CliRunner().invoke(
            main,
            ["report", "run", "queue-outcomes", "--param", "window=24h",
             "--format", "csv"],
        )
        assert result.exit_code == 0, result.output
        rows = _parse_csv(result.output)
        assert rows[0] == [
            "Period", "Bucket", "Category", "By Design", "Count", "Share %",
            "Issues",
        ]
        assert any(r[1] == "open" and r[2] == "solar-flare" for r in rows[1:])
        assert "# report: queue-outcomes" in result.output


class TestQueueOutcomesDaemon:
    """The thin-client path, which is the whole answer to the per-host log.

    ``coord.state.run_report`` routes a board_service client's request to
    ``GET /report/{id}`` on the daemon — which runs on the tick host, which is
    where the block log actually is (#1806: a fleet check that measures the
    wrong machine's filesystem is worse than no check).
    """

    def test_the_report_runs_over_the_daemon_hosts_log(
        self, report_client: TestClient, tmp_path: Path, monkeypatch
    ) -> None:
        path = tmp_path / "queue-block-log.jsonl"
        path.write_text(
            json.dumps({"event": "enter", "ts": QO_END - 900, "key": "api#1",
                        "state": "blocked", "stated_reason": "exhausted"})
            + "\n"
        )
        monkeypatch.setenv("COORD_BLOCK_LOG", str(path))
        resp = report_client.get(
            "/report/queue-outcomes",
            params={"window": "24h", "until": str(QO_END)},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["report_id"] == "queue-outcomes"
        assert body["columns"][0] == "period_start"
        assert [r["bucket"] for r in body["rows"]] == ["open"]
        assert body["rows"][0]["issues"] == ["api#1"]
        assert any(str(path) in n for n in body["notes"])

    def test_a_daemon_host_with_no_log_says_so_over_the_wire(
        self, report_client: TestClient, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("COORD_BLOCK_LOG", str(tmp_path / "absent.jsonl"))
        resp = report_client.get("/report/queue-outcomes")
        assert resp.status_code == 200
        body = resp.json()
        assert body["rows"] == []
        assert body["columns"]  # intact
        assert any("NO BLOCK LOG ON THIS HOST" in n for n in body["notes"])

    def test_an_unknown_window_is_a_400_naming_what_was_allowed(
        self, report_client: TestClient
    ) -> None:
        resp = report_client.get(
            "/report/queue-outcomes", params={"window": "13h"}
        )
        assert resp.status_code == 400
        assert "4w" in resp.json()["error"]


class TestQueueOutcomesPartialWindow:
    """A period with merges but no stall records is UNMEASURED, not perfect.

    #2270's own blocking prerequisite: the recorder (#2235) landed in v0.5.90
    and the fleet was on v0.5.88, so every early window has a complete merge
    history and an empty stall log — which scores 100% for the most flattering
    possible reason. Same failure as a missing log, one granularity down.
    """

    def test_a_window_that_predates_the_log_says_so(self) -> None:
        result = fold_queue_outcomes(
            [
                _episode(
                    "api#1",
                    entered_at=QO_END - 900,
                    resolved_at=QO_END - 800,
                    true_cause="ci-reported — x",
                    human_acted=False,
                )
            ],
            (QO_END - 7 * QO_DAY, QO_END),
            period_seconds=QO_DAY,
            merged=[("api#2", QO_END - 6 * QO_DAY)],
            log_starts_at=QO_END - 900,
        )
        note = next(n for n in result.notes if n.startswith("PARTIAL WINDOW"))
        assert "unmeasured, not perfect" in note
        assert "recorder was not running yet" in note

    def test_a_window_the_log_fully_covers_carries_no_such_caveat(self) -> None:
        result = fold_queue_outcomes(
            [_episode("api#1", entered_at=QO_END - 900)],
            (QO_END - QO_DAY, QO_END),
            log_starts_at=QO_END - 30 * QO_DAY,
        )
        assert not any(n.startswith("PARTIAL WINDOW") for n in result.notes)

    def test_the_runner_derives_it_from_the_log_not_the_filtered_repo(self) -> None:
        # `shared#9` is the oldest record in the FILE. Filtering to `api` must
        # not make the log look younger than it is and hide the caveat.
        episodes = [
            _episode("shared#9", entered_at=QO_END - 3 * QO_DAY),
            _episode("api#1", entered_at=QO_END - 900),
        ]
        result = run_queue_outcomes(
            window="7d",
            repo="api",
            now=QO_END,
            location={"path": "p", "host": "h", "exists": True},
            episode_source=lambda: episodes,
            fetch=lambda **kw: {"entries": [], "has_more": False},
        )
        note = next(n for n in result.notes if n.startswith("PARTIAL WINDOW"))
        assert _abs_iso(QO_END - 3 * QO_DAY) in note


# ── #2271: the chart declaration ───────────────────────────────────────────


class TestChartDeclaration:
    """The additive chart block on `ReportResult` (#2271).

    The whole contract is "additive, and a client that does not understand it
    renders the table" — so these tests are mostly about what did NOT change.
    """

    def test_chart_defaults_to_none_and_serialises_as_none(self) -> None:
        result = ReportResult(
            report_id="r",
            generated_at=1.0,
            window=(0.0, 1.0),
            columns=["a"],
            rows=[{"a": 1}],
            notes=[],
        )
        assert result.chart is None
        assert result.to_dict()["chart"] is None

    def test_the_chart_key_is_additive_and_nothing_else_moved(self) -> None:
        """The already-shipped #1741/#1763 panel deserialises the other eight
        keys; adding `chart` must not disturb one of them."""
        from coord.reports import ChartSeries, ChartSpec

        plain = fold_issue_activity([], WINDOW).to_dict()
        charted = fold_issue_activity([], WINDOW)
        charted.chart = ChartSpec(
            kind="line", series=(ChartSeries(label="L", column="issue"),), x="issue"
        )
        charted_payload = charted.to_dict()
        assert set(charted_payload) == set(plain)
        for key in plain:
            if key != "chart":
                assert charted_payload[key] == plain[key], key

    def test_series_and_spec_wire_shape_is_pinned(self) -> None:
        from coord.reports import ChartSeries, ChartSpec

        spec = ChartSpec(
            kind="bar",
            series=(ChartSeries(label="Entries", column="count", color="#50a0f0"),),
            x="category",
            group_by="bucket",
            stacked=True,
            title="T",
            y_label="Y",
        )
        assert spec.to_dict() == {
            "kind": "bar",
            "series": [
                {"label": "Entries", "column": "count", "color": "#50a0f0"}
            ],
            "x": "category",
            "group_by": "bucket",
            "stacked": True,
            "title": "T",
            "y_label": "Y",
        }

    def test_series_defaults_leave_colour_unset(self) -> None:
        from coord.reports import ChartSeries, ChartSpec

        spec = ChartSpec(kind="line", series=(ChartSeries(label="L", column="c"),))
        assert spec.to_dict() == {
            "kind": "line",
            "series": [{"label": "L", "column": "c", "color": None}],
            "x": None,
            "group_by": None,
            "stacked": False,
            "title": "",
            "y_label": "",
        }

    def test_every_declared_series_column_exists_in_columns(self) -> None:
        """The one invariant that makes "derive from existing columns" true:
        a series naming a column the table does not carry would be a parallel
        data block with extra steps."""
        result = fold_queue_outcomes(
            _qo_fixture(), (QO_END - QO_DAY, QO_END), merged=[]
        )
        assert result.chart is not None
        for series in result.chart.series:
            assert series.column in result.columns
        assert result.chart.x in result.columns
        assert result.chart.group_by in result.columns

    def test_csv_export_of_a_chart_bearing_report_is_unchanged(self) -> None:
        """#1765's serializer is driven by columns/rows/totals and must not
        grow a chart column, a chart comment, or anything else."""
        from coord.reports import ChartSeries, ChartSpec

        charted = fold_queue_outcomes(
            _qo_fixture(), (QO_END - QO_DAY, QO_END), merged=[]
        )
        assert charted.chart is not None
        plain = fold_queue_outcomes(
            _qo_fixture(), (QO_END - QO_DAY, QO_END), merged=[]
        )
        plain.chart = None
        assert result_to_csv(charted) == result_to_csv(plain)

        # And a chart on a report that has none also changes nothing.
        activity = fold_issue_activity([], WINDOW)
        before = result_to_csv(activity)
        activity.chart = ChartSpec(
            kind="line", series=(ChartSeries(label="L", column="issue"),)
        )
        assert result_to_csv(activity) == before


class TestQueueOutcomesChart:
    def test_24h_declares_a_stacked_bar_over_buckets(self) -> None:
        result = fold_queue_outcomes(
            _qo_fixture(), (QO_END - QO_DAY, QO_END), merged=[]
        )
        chart = result.chart
        assert chart is not None
        assert chart.kind == "bar"
        assert chart.stacked is True
        # Same column twice, on purpose: a terminal chart has no per-tick
        # category text, so grouping on the axis column is what puts the
        # bucket names in the legend instead of five anonymous bars.
        assert chart.x == "bucket"
        assert chart.group_by == "bucket"
        assert [s.column for s in chart.series] == ["count"]

    def test_7d_declares_a_trendline_per_bucket(self) -> None:
        result = fold_queue_outcomes(
            _qo_fixture(),
            (QO_END - 7 * QO_DAY, QO_END),
            period_seconds=QO_DAY,
            merged=[],
        )
        chart = result.chart
        assert chart is not None
        assert chart.kind == "line"
        assert chart.stacked is False
        assert chart.x == "period_start"
        assert chart.group_by == "bucket"

    def test_4w_declares_a_trendline_too(self) -> None:
        result = run_queue_outcomes(
            window="4w",
            now=QO_END,
            location={"path": "p", "host": "h", "exists": True},
            episode_source=_qo_fixture,
            fetch=lambda **kw: {"entries": [], "has_more": False},
        )
        assert result.chart is not None
        assert result.chart.kind == "line"

    def test_an_empty_fold_declares_no_chart(self) -> None:
        """An EMPTY result is not a zero score — an axis with no marks on it
        reads as one, so there must be no chart to draw."""
        result = fold_queue_outcomes([], (QO_END - QO_DAY, QO_END), merged=[])
        assert result.rows == []
        assert result.chart is None
        assert result.to_dict()["chart"] is None

    def test_the_chart_reaches_the_cli_json(self) -> None:
        result = fold_queue_outcomes(
            _qo_fixture(), (QO_END - QO_DAY, QO_END), merged=[]
        )
        payload = json.loads(json.dumps(result.to_dict()))
        assert payload["chart"]["kind"] == "bar"
        assert payload["chart"]["series"][0]["column"] == "count"

    def test_the_chart_never_carries_its_own_numbers(self) -> None:
        """One source of truth: the block names columns, never values."""
        result = fold_queue_outcomes(
            _qo_fixture(), (QO_END - QO_DAY, QO_END), merged=[]
        )
        blob = json.dumps(result.to_dict()["chart"])
        assert "data" not in blob
        assert "values" not in blob


# ── #2454: the `completed` report ──────────────────────────────────────────

#: A round window with plenty of room either side of the fixture timestamps.
C_END = 10_000.0
C_START = 0.0


def _completed_issues() -> list[dict]:
    return [
        {"repo_name": "myrepo", "number": 7, "title": "Closed one", "state": "closed"},
        {"repo_name": "myrepo", "number": 9, "title": "Merged, still open", "state": "open"},
        {"repo_name": "myrepo", "number": 11, "title": "Still in flight", "state": "open"},
        {"repo_name": "other", "number": 13, "title": "Another repo", "state": "closed"},
    ]


def _completed_assignments() -> list[dict]:
    """#2454's timestamps plus #2472's spend, on the same rows.

    The spend numbers are chosen to be exact under the DEFAULT
    :class:`~coord.config.PricingConfig` (sonnet input is $3.00/Mtok — see
    `TestUsagePricingFollowsConfig`), so the assertions can name a dollar
    figure instead of an approximation of one:

      * myrepo#7's first leg estimates to exactly $3.00 and its RETRY carries a
        captured $2.00, so the issue is 2 legs / $5.00 — and the two halves
        exercise both branches of `leg_cost` in one row.
      * myrepo#9 is merged with NO assignment at all: 0 legs, $0.
      * other#7 is a fat leg that must not leak into myrepo#7's spend any more
        than it leaks into its timestamps.
    """
    return [
        # Two legs on #7 — STARTED is the FIRST dispatch, ENDED the LAST finish.
        {"repo_name": "myrepo", "issue_number": 7, "dispatched_at": 100.0, "finished_at": 200.0,
         "model": "sonnet", "input_tokens": 1_000_000, "output_tokens": 0},
        {"repo_name": "myrepo", "issue_number": 7, "dispatched_at": 150.0, "finished_at": 250.0,
         "model": "sonnet", "cost_usd": 2.0, "input_tokens": 10, "output_tokens": 4},
        {"repo_name": "myrepo", "issue_number": 11, "dispatched_at": 900.0, "finished_at": None,
         "model": "sonnet", "input_tokens": 500, "output_tokens": 500},
        {"repo_name": "other", "issue_number": 13, "dispatched_at": 50.0, "finished_at": 300.0,
         "model": "sonnet", "cost_usd": 0.5, "input_tokens": 7, "output_tokens": 3},
        # Same issue NUMBER in a different repo — must not contribute to
        # myrepo#7's timestamps, nor to its tokens or cost.
        {"repo_name": "other", "issue_number": 7, "dispatched_at": 1.0, "finished_at": 9999.0,
         "model": "sonnet", "cost_usd": 999.0, "input_tokens": 9_000_000, "output_tokens": 9_000},
    ]


def _completed_merge_queue() -> list[dict]:
    return [
        {"repo_name": "myrepo", "issue_number": 9, "state": "merged", "last_attempt": 400.0},
        # A non-merged row for an issue that is otherwise NOT done — must not
        # promote #11 into the report.
        {"repo_name": "myrepo", "issue_number": 11, "state": "pending", "last_attempt": 950.0},
    ]


def _fold_completed(window=(C_START, C_END), **kw) -> ReportResult:
    return fold_completed(
        _completed_issues(),
        _completed_assignments(),
        _completed_merge_queue(),
        window,
        generated_at=C_END,
        **kw,
    )


class TestCompletedFold:
    def test_columns_are_the_wire_contract(self) -> None:
        result = _fold_completed()
        assert result.report_id == "completed"
        assert result.columns == [
            "repo", "issue", "title", "started_at", "ended_at",
            # #2472's four, APPENDED — the client sorts by column index, so
            # #2454's five must keep the indices they had.
            # #2825's `cache_read`, appended after `tokens_out` (before
            # `cost_total`, which shifts by one index — see the module
            # comment).
            "legs", "tokens_in", "tokens_out", "cache_read", "cost_total",
        ]
        assert [m.id for m in result.column_meta] == result.columns

    def test_only_closed_or_merged_issues_appear(self) -> None:
        rows = _fold_completed().rows
        assert [(r["repo"], r["issue"]) for r in rows] == [
            ("myrepo", 9),   # merged at 400
            ("other", 13),   # finished at 300
            ("myrepo", 7),   # finished at 250
        ]

    def test_an_open_issue_whose_merge_queue_row_says_merged_is_done(self) -> None:
        """`pipeline_lifecycle_section` rule 3 — the PR closed it via
        `fixes #N` before the brain synced the GitHub close."""
        row = next(r for r in _fold_completed().rows if r["issue"] == 9)
        assert row["ended_at"] == 400.0

    def test_a_pending_merge_queue_row_does_not_make_an_issue_done(self) -> None:
        assert all(r["issue"] != 11 for r in _fold_completed().rows)

    def test_started_is_the_first_dispatch_and_ended_the_last_finish(self) -> None:
        row = next(r for r in _fold_completed().rows if r["issue"] == 7)
        assert row["started_at"] == 100.0
        assert row["ended_at"] == 250.0

    def test_timestamps_are_scoped_by_coord_local_repo(self) -> None:
        """other#7's assignments must not leak into myrepo#7's row."""
        row = next(
            r for r in _fold_completed().rows
            if r["repo"] == "myrepo" and r["issue"] == 7
        )
        assert row["ended_at"] == 250.0

    def test_a_merged_timestamp_wins_over_assignment_finished_at(self) -> None:
        result = fold_completed(
            [{"repo_name": "myrepo", "number": 7, "title": "t", "state": "closed"}],
            [{"repo_name": "myrepo", "issue_number": 7,
              "dispatched_at": 10.0, "finished_at": 20.0}],
            [{"repo_name": "myrepo", "issue_number": 7,
              "state": "merged", "last_attempt": 55.0}],
            (C_START, C_END),
            generated_at=C_END,
        )
        assert result.rows[0]["ended_at"] == 55.0

    def test_the_window_is_applied_to_ended_not_started(self) -> None:
        rows = _fold_completed(window=(260.0, C_END)).rows
        # #7 ended at 250 — outside; #13 (300) and #9 (400) are inside even
        # though #13 STARTED at 50, well before the window.
        assert sorted(r["issue"] for r in rows) == [9, 13]

    def test_rows_come_back_newest_ended_first(self) -> None:
        ends = [r["ended_at"] for r in _fold_completed().rows]
        assert ends == sorted(ends, reverse=True)

    def test_repo_filter_restricts_to_one_coord_local_repo(self) -> None:
        rows = _fold_completed(repo="other").rows
        assert [(r["repo"], r["issue"]) for r in rows] == [("other", 13)]

    def test_an_issue_with_no_end_timestamp_is_dropped_and_noted(self) -> None:
        result = fold_completed(
            [{"repo_name": "myrepo", "number": 7, "title": "t", "state": "closed"}],
            [],
            [],
            (C_START, C_END),
            generated_at=C_END,
        )
        assert result.rows == []
        assert any("no end timestamp" in n for n in result.notes)

    def test_an_unknown_repo_filter_says_so_rather_than_looking_empty(self) -> None:
        result = _fold_completed(repo="acme/myrepo")
        assert result.rows == []
        assert any("coord-local repo name" in n for n in result.notes)

    def test_a_clean_window_produces_no_notes(self) -> None:
        assert _fold_completed().notes == []

    def test_the_result_is_json_serialisable(self) -> None:
        json.dumps(_fold_completed().to_dict())


class TestCompletedSpend:
    """#2472: the spend half of a row — `legs`, tokens and `cost_total`.

    Every number here comes out of `coord.usage_rollup.rollup`; these tests
    pin the *joining* (right issue, right repo, right window) and the fact
    that `completed` and `usage` cannot disagree, which is the whole point of
    reusing the rollup instead of writing a second cost calculator.
    """

    def _row(self, issue: int = 7, repo: str = "myrepo", **kw) -> dict:
        return next(
            r for r in _fold_completed(**kw).rows
            if r["repo"] == repo and r["issue"] == issue
        )

    def test_legs_counts_agent_sessions_not_issues(self) -> None:
        """myrepo#7 was dispatched twice — a retry. `legs` is 2, not 1."""
        assert self._row(7)["legs"] == 2
        # …and an issue with a single leg reads 1, so `legs` is not just
        # "number of rows folded".
        assert self._row(13, repo="other")["legs"] == 1

    def test_tokens_are_summed_across_every_leg(self) -> None:
        row = self._row(7)
        assert row["tokens_in"] == 1_000_010  # 1_000_000 + 10
        assert row["tokens_out"] == 4         # 0 + 4

    def test_cost_total_is_captured_plus_estimated(self) -> None:
        row = self._row(7)
        # The retry's captured $2.00 is kept verbatim; the first leg has no
        # captured cost so its 1M sonnet input tokens estimate to $3.00.
        assert row["cost_total"] == pytest.approx(5.0)

    def test_cache_read_is_a_declared_column_not_just_a_row_key(self) -> None:
        """#2825: `completed` showed a full `Tok Out` next to a `tokens_in`
        that is ~0.001% of a leg's real input — the raw uncached count sitting
        alone next to output read as "output dwarfs input", backwards by five
        orders of magnitude. `cache_read` was already computed into every row
        by `_usage_metrics`; this only had to become a declared column.
        """
        result = fold_completed(
            [{"repo_name": "myrepo", "number": 21, "title": "t", "state": "closed"}],
            [{"repo_name": "myrepo", "issue_number": 21,
              "dispatched_at": 10.0, "finished_at": 20.0, "model": "sonnet",
              "input_tokens": 634, "output_tokens": 186_209,
              "cache_read_tokens": 59_908_777}],
            [],
            (C_START, C_END),
            generated_at=C_END,
        )
        assert "cache_read" in result.columns
        meta = {m.id: m for m in result.column_meta}
        assert meta["cache_read"].label == "Cache Rd"
        row = result.rows[0]
        assert row["tokens_in"] == 634
        assert row["tokens_out"] == 186_209
        # The number that actually carries the input volume — orders of
        # magnitude above `tokens_in`, exactly the #2825 scenario.
        assert row["cache_read"] == 59_908_777

    def test_the_captured_estimated_split_ships_as_extra_row_keys(self) -> None:
        """One `Total $` COLUMN, but no information thrown away — a client
        that wants `usage`'s split can read it off the row."""
        row = self._row(7)
        assert row["cost_captured"] == pytest.approx(2.0)
        assert row["cost_est"] == pytest.approx(3.0)
        assert "cost_captured" not in COMPLETED_COLUMNS
        assert "cost_est" not in COMPLETED_COLUMNS

    def test_spend_is_scoped_by_coord_local_repo(self) -> None:
        """other#7's fat leg ($999, 9M tokens) must not reach myrepo#7 — the
        same scoping the timestamps get, applied to the money."""
        row = self._row(7)
        assert row["cost_total"] == pytest.approx(5.0)
        assert row["tokens_in"] == 1_000_010

    def test_spend_is_lifetime_not_windowed(self) -> None:
        """The documented call: the window filters ENDED, never the legs.

        myrepo#7's legs were dispatched at 100/150 and the window opens at
        240 — a windowed figure would report $2.00 (or $0) here. It reports
        the issue's whole-life $5.00, because "what did finishing this issue
        cost me" must not shrink when the operator narrows the range.
        """
        wide = self._row(7)
        narrow = self._row(7, window=(240.0, C_END))
        assert narrow["ended_at"] == 250.0, "still in the window"
        assert narrow["legs"] == wide["legs"] == 2
        assert narrow["cost_total"] == wide["cost_total"] == pytest.approx(5.0)
        assert narrow["tokens_in"] == wide["tokens_in"]

    def test_an_issue_with_no_legs_reads_zero_not_blank(self) -> None:
        """myrepo#9 is merged with no assignment against it at all."""
        row = self._row(9)
        assert row["legs"] == 0
        assert row["tokens_in"] == 0
        assert row["tokens_out"] == 0
        assert row["cost_total"] == 0.0
        # A real 0, not a hole a client would have to render as "?".
        for column in COMPLETED_COLUMNS:
            assert column in row

    def test_the_no_legs_default_is_not_shared_between_rows(self) -> None:
        """`_COMPLETED_NO_LEGS` is a module constant; two zero-leg rows must
        not end up aliasing one mapping."""
        result = fold_completed(
            [
                {"repo_name": "r", "number": 1, "title": "a", "state": "closed"},
                {"repo_name": "r", "number": 2, "title": "b", "state": "closed"},
            ],
            [],
            [
                {"repo_name": "r", "issue_number": 1, "state": "merged", "last_attempt": 10.0},
                {"repo_name": "r", "issue_number": 2, "state": "merged", "last_attempt": 20.0},
            ],
            (C_START, C_END),
            generated_at=C_END,
        )
        first, second = result.rows
        first["legs"] = 99
        assert second["legs"] == 0

    def test_every_completed_row_agrees_with_the_usage_report(self) -> None:
        """The load-bearing guard, and the reason #2472 exists: an operator
        must never have to open `usage` and cross-reference by issue number to
        find a different number for the same issue."""
        from coord.config import PricingConfig
        from coord.reports import fold_usage
        from coord.usage_rollup import TimeWindow

        pricing = PricingConfig()
        completed = _fold_completed(pricing=pricing)
        usage = fold_usage(
            _completed_assignments(),
            TimeWindow(start=None, end=None, label="all"),
            group_by="issue",
            pricing=pricing,
        )
        by_issue = {(r["repo"], r["issue"]): r for r in usage.rows}

        checked = 0
        for row in completed.rows:
            peer = by_issue.get((row["repo"], row["issue"]))
            if peer is None:
                assert row["legs"] == 0, "only a no-leg issue may be absent from usage"
                continue
            for key in ("legs", "tokens_in", "tokens_out", "cache_read"):
                assert row[key] == peer[key], f"{row['repo']}#{row['issue']} {key}"
            assert row["cost_total"] == pytest.approx(peer["cost_total"])
            checked += 1
        assert checked >= 2, "the comparison must actually have compared something"

    def test_a_leg_booked_to_another_issue_follows_usages_attribution(self) -> None:
        """#1553: a leg carrying `for_issue_number` is spend on THAT issue.

        `completed`'s timestamps key on the raw `issue_number` (#2454's rule,
        a port of `completed_rows`), but the cost columns must key the way
        `usage` does or the two reports would disagree — which is exactly what
        the previous test forbids.
        """
        result = fold_completed(
            [{"repo_name": "myrepo", "number": 7, "title": "t", "state": "closed"}],
            [
                {"repo_name": "myrepo", "issue_number": 7,
                 "dispatched_at": 100.0, "finished_at": 200.0,
                 "model": "sonnet", "cost_usd": 1.0},
                # An oracle-loop acceptance slice: filed under the milestone's
                # tracking issue 999, but its spend belongs to #7.
                {"repo_name": "myrepo", "issue_number": 999, "for_issue_number": 7,
                 "dispatched_at": 300.0, "finished_at": 400.0,
                 "model": "sonnet", "cost_usd": 4.0},
            ],
            [],
            (C_START, C_END),
            generated_at=C_END,
        )
        row = result.rows[0]
        assert row["legs"] == 2
        assert row["cost_total"] == pytest.approx(5.0)
        # …while ENDED stays #2454's: the last finish of an assignment whose
        # own `issue_number` is 7.
        assert row["ended_at"] == 200.0

    def test_an_unpriced_model_is_noted_rather_than_shown_as_zero(self) -> None:
        """#1763's rule, kept: a leg on a model with no `pricing:` entry is
        never silently priced at $0."""
        result = fold_completed(
            [{"repo_name": "myrepo", "number": 7, "title": "t", "state": "closed"}],
            [{"repo_name": "myrepo", "issue_number": 7,
              "dispatched_at": 100.0, "finished_at": 200.0,
              "model": "gpt-hypothetical", "input_tokens": 500, "output_tokens": 500}],
            [],
            (C_START, C_END),
            generated_at=C_END,
        )
        row = result.rows[0]
        # Tokens counted, spend NOT invented.
        assert row["tokens_in"] == 500
        assert row["cost_total"] == 0.0
        note = next(n for n in result.notes if "pricing" in n)
        assert "myrepo#7" in note
        assert "read LOW" in note

    def test_the_unpriced_note_is_one_line_not_one_per_issue(self) -> None:
        """`usage` emits a note per issue; `completed` is a time-range list
        that can carry a hundred rows, so it aggregates."""
        issues = [
            {"repo_name": "myrepo", "number": n, "title": "t", "state": "closed"}
            for n in range(1, 9)
        ]
        assignments = [
            {"repo_name": "myrepo", "issue_number": n,
             "dispatched_at": 10.0 * n, "finished_at": 20.0 * n,
             "model": "gpt-hypothetical", "input_tokens": 10, "output_tokens": 10}
            for n in range(1, 9)
        ]
        result = fold_completed(
            issues, assignments, [], (C_START, C_END), generated_at=C_END
        )
        pricing_notes = [n for n in result.notes if "pricing" in n]
        assert len(pricing_notes) == 1, pricing_notes
        assert "8 issue(s)" in pricing_notes[0]
        assert "and 3 more" in pricing_notes[0], "the list is capped, and says so"

    def test_a_priced_fixture_still_produces_no_notes(self) -> None:
        """The spend columns must not make every clean report noisy."""
        assert _fold_completed().notes == []

    def test_pricing_overrides_move_the_estimate(self) -> None:
        """Same #1116/#1763 seam `usage` has: the fleet's own `pricing:` block
        is what the dollar column is computed from, not a snapshot."""
        from coord.config import ModelRates, PricingConfig

        override = PricingConfig(models={"sonnet": ModelRates(input=9.0, output=0.0)})
        row = self._row(7, pricing=override)
        # The captured $2.00 is untouched; the estimated leg's 1M input tokens
        # now price at $9.00 instead of $3.00.
        assert row["cost_captured"] == pytest.approx(2.0)
        assert row["cost_est"] == pytest.approx(9.0)
        assert row["cost_total"] == pytest.approx(11.0)

    def test_a_leg_with_no_timestamps_at_all_is_not_counted(self) -> None:
        """Documented consequence of the unbounded rollup window: a row with
        neither `dispatched_at` nor `finished_at` is not evidence a session
        ran, and `usage` excludes it on the same rule."""
        result = fold_completed(
            [{"repo_name": "myrepo", "number": 7, "title": "t", "state": "closed"}],
            [
                {"repo_name": "myrepo", "issue_number": 7,
                 "dispatched_at": 100.0, "finished_at": 200.0, "cost_usd": 1.0},
                {"repo_name": "myrepo", "issue_number": 7,
                 "dispatched_at": None, "finished_at": None, "cost_usd": 50.0},
            ],
            [],
            (C_START, C_END),
            generated_at=C_END,
        )
        assert result.rows[0]["legs"] == 1
        assert result.rows[0]["cost_total"] == pytest.approx(1.0)

    def test_the_result_is_still_json_serialisable(self) -> None:
        json.dumps(_fold_completed().to_dict())


class TestCompletedRunner:
    def test_since_and_until_bound_the_window(self) -> None:
        seen: list[tuple] = []

        def source():
            seen.append(())
            return (
                _completed_issues(),
                _completed_assignments(),
                _completed_merge_queue(),
            )

        result = run_completed(since="1h", until="400", source=source)
        assert seen, "the source seam must actually be used"
        assert result.window == (400.0 - 3600.0, 400.0)
        # myrepo#9 merged exactly at the window end.
        assert any(r["issue"] == 9 for r in result.rows)

    def test_an_empty_until_means_now(self) -> None:
        result = run_completed(
            since="1h", now=C_END, source=lambda: ([], [], [])
        )
        assert result.window == (C_END - 3600.0, C_END)

    def test_run_report_routes_to_it_with_defaults(self) -> None:
        result = run_report(
            "completed", {}, source=lambda: ([], [], []), now=C_END
        )
        assert result.report_id == "completed"
        assert result.window == (C_END - 86400.0, C_END)

    def test_a_bad_since_is_a_clean_error(self) -> None:
        with pytest.raises(ReportError) as exc:
            resolve_params(REPORTS["completed"], {"since": "banana"})
        assert "since" in str(exc.value)


class TestCompletedAgainstTheRealSchema:
    """The `source` seam above lets every rule be tested without a DB — which
    is exactly why the *default* source needs its own test: its three SELECTs
    name real columns of `coord/db.py`'s schema, and a typo there would only
    ever surface at runtime, on the daemon."""

    def _seed(self, coord_db) -> None:
        with coord_db:
            coord_db.execute(
                "INSERT INTO issues (repo_name, number, title, state) "
                "VALUES ('api', 1629, 'Closed and done', 'closed')"
            )
            coord_db.execute(
                "INSERT INTO issues (repo_name, number, title, state) "
                "VALUES ('api', 1630, 'Still open', 'open')"
            )
            coord_db.execute(
                "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
                "issue_number, issue_title, status, dispatched_at, finished_at, "
                "model, cost_usd, input_tokens, output_tokens) "
                "VALUES ('a1', 'precision', 'api', 1629, 'Closed and done', 'done', "
                "1000.0, 2000.0, 'sonnet', 1.25, 4000, 700)"
            )
            # #2472: a RETRY, so `legs` has something to count past 1 and the
            # widened SELECT is proved to read every leg's tokens, not just
            # the first row's.
            coord_db.execute(
                "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
                "issue_number, issue_title, status, dispatched_at, finished_at, "
                "model, cost_usd, input_tokens, output_tokens) "
                "VALUES ('a2', 'precision', 'api', 1629, 'Closed and done', 'done', "
                "2100.0, 2200.0, 'sonnet', 0.75, 1000, 300)"
            )
            coord_db.execute(
                "INSERT INTO merge_queue (assignment_id, repo_name, repo_github, "
                "branch, target_branch, issue_number, issue_title, state, last_attempt) "
                "VALUES ('a1', 'api', 'acme/api', 'b', 'main', 1629, "
                "'Closed and done', 'merged', 2500.0)"
            )

    def test_the_default_source_reads_the_real_tables(self, coord_db) -> None:
        self._seed(coord_db)
        result = run_completed(since="7d", until="3000", repo="api")
        assert [r["issue"] for r in result.rows] == [1629]
        row = result.rows[0]
        assert row["repo"] == "api"
        assert row["title"] == "Closed and done"
        assert row["started_at"] == 1000.0
        # The merged merge_queue row wins over the assignment's finished_at.
        assert row["ended_at"] == 2500.0

    def test_the_widened_select_reaches_the_spend_columns(self, coord_db) -> None:
        """#2472 added `for_issue_number`/tokens/`cost_usd`/`model` to that
        SELECT. They are all migration-added columns, and the source swallows
        every exception into an empty report — so a typo would show up as a
        silently blank report, never a traceback. Hence this."""
        self._seed(coord_db)
        row = run_completed(since="7d", until="3000", repo="api").rows[0]
        assert row["legs"] == 2, "both the first attempt and the retry"
        assert row["tokens_in"] == 5000   # 4000 + 1000
        assert row["tokens_out"] == 1000  # 700 + 300
        assert row["cost_total"] == pytest.approx(2.0)  # 1.25 + 0.75, both captured

    def test_it_runs_through_the_cli(self, coord_db) -> None:
        self._seed(coord_db)
        result = CliRunner().invoke(
            main,
            ["report", "run", "completed", "--param", "until=3000",
             "--param", "since=7d", "--json"],
        )
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert body["report_id"] == "completed"
        assert [r["issue"] for r in body["rows"]] == [1629]

    def test_it_runs_through_the_daemon_route(self, report_client, rw_db) -> None:
        # `rw_db`, not `coord_db` — the ASGI worker thread runs the handler,
        # and the autouse `:memory:` conn is thread-bound (see that fixture).
        self._seed(rw_db)
        resp = report_client.get("/report/completed?until=3000&since=7d")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["report_id"] == "completed"
        assert [r["issue"] for r in body["rows"]] == [1629]


class TestCompletedCatalogue:
    def test_completed_is_a_real_catalogue_entry(self) -> None:
        ids = [r["id"] for r in catalogue()["reports"]]
        assert "completed" in ids

    def test_its_params_mirror_issue_activitys(self) -> None:
        rep = next(r for r in catalogue()["reports"] if r["id"] == "completed")
        assert rep["title"] == "Completed"
        assert rep["description"]
        params = {p["id"]: p for p in rep["params"]}
        assert set(params) == {"since", "until", "repo"}
        assert params["since"]["choices"] == ["1h", "6h", "24h", "3d", "7d"]
        assert params["since"]["default"] == "24h"
        assert params["since"]["free_form"] is True
        assert params["until"]["kind"] == "text"
        assert params["repo"]["default"] == ""

    def test_it_is_exportable_as_csv_like_every_other_report(self) -> None:
        text = result_to_csv(_fold_completed())
        # The header row is the `column_meta` LABELS (the shared serializer's
        # rule), and the values stay raw epochs — not display strings.
        # #2472's four labels (plus #2825's `cache_read`) are `usage`'s
        # verbatim, so a spreadsheet built off one export lines up with the
        # other. `tokens_in` is labelled "Raw In", not "Tok In" (#2825) — it
        # is not `Tok Out`'s pair, it's ~0.001% of a leg's real input.
        assert "Repo,Issue,Title,Started,Ended,Legs,Raw In,Tok Out,Cache Rd,Total $" in text
        # myrepo#9 merged with no assignment: a real 0/0/0/0/0.0, not five
        # empty cells.
        assert 'myrepo,9,"Merged, still open",,400.0,0,0,0,0,0.0' in text
        # myrepo#7's retry: 2 legs, $2.00 captured + $3.00 estimated, no
        # cache_read_tokens in the fixture so that column reads 0.
        assert "myrepo,7,Closed one,100.0,250.0,2,1000010,4,0,5.0" in text


class TestRowIdentity:
    """#2454: the catalogue declares which columns name a `(repo, issue)`, so
    a client can offer per-row navigation without a per-report `match`."""

    def test_completed_and_issue_activity_declare_one(self) -> None:
        by_id = {r["id"]: r for r in catalogue()["reports"]}
        for report_id in ("completed", "issue-activity"):
            assert by_id[report_id]["row_identity"] == {
                "repo_column": "repo",
                "issue_column": "issue",
            }

    def test_the_declared_columns_actually_exist_in_the_rows(self) -> None:
        """A declaration naming a column the rows don't carry would produce a
        menu item that can never resolve."""
        row = _fold_completed().rows[0]
        identity = REPORTS["completed"].row_identity
        assert identity is not None
        assert identity.repo_column in row
        assert identity.issue_column in row

    def test_reports_without_a_per_row_issue_declare_none(self) -> None:
        by_id = {r["id"]: r for r in catalogue()["reports"]}
        # `usage` can be grouped by repo, `decisions` rows are cards,
        # `queue-outcomes` rows are per-period aggregates whose `issues`
        # column is a LIST, and `trend` rows are time BUCKETS, not issues —
        # none has a single `(repo, issue)`.
        for report_id in (
            "usage", "decisions", "queue-outcomes", "drive-queue-status", "trend",
            "deprecated-routes",
        ):
            assert by_id[report_id]["row_identity"] is None

    def test_the_key_is_always_present_so_a_client_can_rely_on_it(self) -> None:
        for rep in catalogue()["reports"]:
            assert "row_identity" in rep


# ── #2826: the `trend` report ────────────────────────────────────────────

#: `7d`'s bucket width (6h) and point count (28), chosen so `TR_END` lands
#: window_start on a clean `0.0` and every fixture timestamp below can be
#: written as a small offset from it rather than an absolute epoch.
TR_BUCKET = 6 * 3600.0
TR_POINTS = 28
TR_END = TR_POINTS * TR_BUCKET


def _trend_issue(number: int, *, repo: str = "myrepo", state: str = "closed") -> dict:
    return {"repo_name": repo, "number": number, "title": f"issue {number}", "state": state}


def _trend_assignment(
    number: int, dispatched: float, finished: float, cost: float, *, repo: str = "myrepo"
) -> dict:
    return {
        "repo_name": repo,
        "issue_number": number,
        "dispatched_at": dispatched,
        "finished_at": finished,
        "model": "sonnet",
        "cost_usd": cost,
    }


def _trend_fold(issues, assignments, merge_queue=(), **kw) -> ReportResult:
    return fold_trend(
        list(issues), list(assignments), list(merge_queue), TR_END,
        range_="7d", generated_at=TR_END, **kw,
    )


class TestFoldTrend:
    def test_columns_are_the_wire_contract(self) -> None:
        result = _trend_fold([], [], [])
        assert result.report_id == "trend"
        assert result.columns == TREND_COLUMNS == [
            "bucket_start", "merged", "cost_per_issue", "legs_per_issue",
        ]
        assert [m.id for m in result.column_meta] == result.columns

    def test_column_meta_declares_money_and_float_kinds(self) -> None:
        meta = {m.id: m for m in TREND_COLUMN_META}
        assert meta["bucket_start"].kind == "timestamp"
        assert meta["merged"].kind == "int"
        assert meta["cost_per_issue"].kind == "money"
        assert meta["legs_per_issue"].kind == "float"

    def test_bucket_width_and_point_count_match_the_range_table(self) -> None:
        # #2826's table: 1d=hourly/24, 3d=3-hourly/24, 7d=6-hourly/28,
        # 1m=daily/30 — ~24-30 points either way.
        assert resolve_trend_range("1d") == (3600.0, 24)
        assert resolve_trend_range("3d") == (3 * 3600.0, 24)
        assert resolve_trend_range("7d") == (6 * 3600.0, 28)
        assert resolve_trend_range("1m") == (86400.0, 30)

    def test_an_unknown_range_is_a_clean_error(self) -> None:
        with pytest.raises(ReportError) as exc:
            resolve_trend_range("9d")
        assert "range" in str(exc.value)

    def test_trailing_window_width_is_five_buckets(self) -> None:
        assert TREND_TRAILING_BUCKETS == 5

    def test_rows_are_one_per_bucket_spanning_the_whole_range(self) -> None:
        result = _trend_fold([], [], [])
        assert len(result.rows) == TR_POINTS
        assert result.rows[0]["bucket_start"] == 0.0
        assert result.rows[-1]["bucket_start"] == (TR_POINTS - 1) * TR_BUCKET
        assert result.window == (0.0, TR_END)

    def test_an_empty_fold_is_null_everywhere_not_zero(self) -> None:
        """The #2826 headline rule: an empty trailing window must never come
        back as `0.0` — the chart widget this report feeds has no way to
        draw a gap, so a `0.0` here would read as a cost collapse that never
        happened."""
        result = _trend_fold([], [], [])
        assert all(r["merged"] == 0 for r in result.rows)
        assert all(r["cost_per_issue"] is None for r in result.rows)
        assert all(r["legs_per_issue"] is None for r in result.rows)

    def test_merged_counts_only_issues_ended_in_that_bucket(self) -> None:
        issues = [_trend_issue(1), _trend_issue(2), _trend_issue(3)]
        assignments = [
            _trend_assignment(1, 900.0, 1000.0, 3.0),  # bucket 0
            _trend_assignment(2, 4900.0, 5000.0, 1.0),  # bucket 0
            _trend_assignment(3, 432000.0 + 50, 432000.0 + 100, 10.0),  # bucket 20
        ]
        result = _trend_fold(issues, assignments, [])
        assert result.rows[0]["merged"] == 2
        assert result.rows[20]["merged"] == 1
        assert sum(r["merged"] for r in result.rows) == 3

    def test_cost_per_issue_is_a_trailing_mean_not_a_per_bucket_mean(self) -> None:
        issues = [_trend_issue(1), _trend_issue(2)]
        assignments = [
            _trend_assignment(1, 900.0, 1000.0, 3.0),  # bucket 0, $3
            _trend_assignment(2, 4900.0, 5000.0, 1.0),  # bucket 0, $1
        ]
        result = _trend_fold(issues, assignments, [])
        # bucket 0: mean of the two issues that just merged.
        assert result.rows[0]["cost_per_issue"] == pytest.approx(2.0)
        # bucket 4 is still within the 5-bucket trailing window of bucket 0
        # (this bucket plus the previous 4 reaches back to bucket 0) — same
        # mean, NOT a per-bucket mean of a bucket with zero merges of its own.
        assert result.rows[4]["merged"] == 0
        assert result.rows[4]["cost_per_issue"] == pytest.approx(2.0)
        # bucket 5 is one bucket PAST the trailing window — no merge falls in
        # buckets {1..5}, so the mean is undefined: None, never a stale or a
        # false $0 value.
        assert result.rows[5]["cost_per_issue"] is None
        assert result.rows[5]["legs_per_issue"] is None

    def test_the_earliest_bucket_gets_a_full_trailing_window_via_lookback(self) -> None:
        """A merge just BEFORE the visible window still feeds bucket 0's
        trailing mean — otherwise bucket 0 would get a narrower trailing
        window than every other bucket in the series."""
        issues = [_trend_issue(1), _trend_issue(2)]
        assignments = [
            # Ends inside the lookback (`fetch_start` reaches back 4 buckets
            # before `window_start`), but before the visible window opens.
            _trend_assignment(1, -70000.0, -68000.0, 4.0),
            _trend_assignment(2, 900.0, 1000.0, 2.0),  # bucket 0
        ]
        result = _trend_fold(issues, assignments, [])
        # Bucket 0 itself sees only ONE merge (issue 2) ...
        assert result.rows[0]["merged"] == 1
        # ... but issue 1's cost still feeds bucket 0's trailing mean.
        assert result.rows[0]["cost_per_issue"] == pytest.approx(3.0)  # (4 + 2) / 2

    def test_legs_per_issue_averages_agent_sessions_not_issue_count(self) -> None:
        """myrepo#1 was dispatched twice (a retry) — `legs` is 2 for that ONE
        issue, mirroring `completed`'s own `legs` semantics (#2472)."""
        issues = [_trend_issue(1)]
        assignments = [
            _trend_assignment(1, 700.0, 800.0, 1.0),
            _trend_assignment(1, 800.0, 1000.0, 1.0),
        ]
        result = _trend_fold(issues, assignments, [])
        assert result.rows[0]["legs_per_issue"] == pytest.approx(2.0)

    def test_merged_is_defined_exactly_like_completed(self) -> None:
        """An OPEN issue with no merge_queue row is not MERGED — the same
        rule `completed`'s ENDED uses, so the two reports can never silently
        disagree on what counts."""
        issues = [_trend_issue(1, state="open")]
        assignments = [_trend_assignment(1, 900.0, 1000.0, 3.0)]
        result = _trend_fold(issues, assignments, [])
        assert all(r["merged"] == 0 for r in result.rows)

    def test_a_merge_queue_row_makes_an_open_issue_count(self) -> None:
        issues = [_trend_issue(1, state="open")]
        assignments = [_trend_assignment(1, 900.0, 950.0, 3.0)]
        merge_queue = [
            {"repo_name": "myrepo", "issue_number": 1, "state": "merged", "last_attempt": 1000.0}
        ]
        result = _trend_fold(issues, assignments, merge_queue)
        assert result.rows[0]["merged"] == 1

    def test_repo_filter_restricts_to_one_coord_local_repo(self) -> None:
        issues = [_trend_issue(1), _trend_issue(1, repo="other")]
        assignments = [
            _trend_assignment(1, 900.0, 1000.0, 3.0),
            _trend_assignment(1, 900.0, 1000.0, 5.0, repo="other"),
        ]
        result = _trend_fold(issues, assignments, [], repo="other")
        assert result.rows[0]["merged"] == 1
        assert result.rows[0]["cost_per_issue"] == pytest.approx(5.0)

    def test_notes_report_how_many_buckets_are_null(self) -> None:
        issues = [_trend_issue(1), _trend_issue(2)]
        assignments = [
            _trend_assignment(1, 900.0, 1000.0, 3.0),
            _trend_assignment(2, 4900.0, 5000.0, 1.0),
        ]
        result = _trend_fold(issues, assignments, [])
        null_count = sum(1 for r in result.rows if r["cost_per_issue"] is None)
        assert null_count > 0
        assert any(f"{null_count} of {TR_POINTS} bucket" in n for n in result.notes)

    def test_short_ranges_carry_an_honesty_note_long_ones_dont(self) -> None:
        result_1d = fold_trend([], [], [], 24 * 3600.0, range_="1d", generated_at=24 * 3600.0)
        assert any("noisy" in n for n in result_1d.notes)
        result_7d = _trend_fold([], [], [])
        assert not any("noisy" in n for n in result_7d.notes)

    def test_the_result_is_json_serialisable(self) -> None:
        issues = [_trend_issue(1)]
        assignments = [_trend_assignment(1, 900.0, 1000.0, 3.0)]
        json.dumps(_trend_fold(issues, assignments, []).to_dict())


class TestRunTrend:
    def test_range_and_until_bound_the_window(self) -> None:
        seen: list[tuple] = []

        def source():
            seen.append(())
            return ([], [], [])

        result = run_trend(range="1d", until="100000", source=source)
        assert seen, "the source seam must actually be used"
        assert result.window == (100000.0 - 24 * 3600.0, 100000.0)
        assert len(result.rows) == 24

    def test_an_empty_until_means_now(self) -> None:
        result = run_trend(range="1d", now=100000.0, source=lambda: ([], [], []))
        assert result.window == (100000.0 - 24 * 3600.0, 100000.0)

    def test_run_report_routes_to_it_with_defaults(self) -> None:
        result = run_report("trend", {}, source=lambda: ([], [], []), now=100000.0)
        assert result.report_id == "trend"
        # default range is `7d`: 28 buckets x 6h.
        assert result.window == (100000.0 - 28 * 6 * 3600.0, 100000.0)

    def test_a_bad_range_is_a_clean_error(self) -> None:
        with pytest.raises(ReportError) as exc:
            resolve_params(REPORTS["trend"], {"range": "9d"})
        assert "range" in str(exc.value)


class TestTrendAgainstTheRealSchema:
    """Same posture as `TestCompletedAgainstTheRealSchema`: `run_trend`'s
    default source reads the SAME three real tables `run_completed`'s does
    (#2826 reuses `fold_completed`'s own fold, not just its idea), so this
    pins that the real board schema still reaches it end to end."""

    def _seed(self, coord_db) -> None:
        with coord_db:
            coord_db.execute(
                "INSERT INTO issues (repo_name, number, title, state) "
                "VALUES ('api', 1629, 'Closed and done', 'closed')"
            )
            coord_db.execute(
                "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
                "issue_number, issue_title, status, dispatched_at, finished_at, "
                "model, cost_usd, input_tokens, output_tokens) "
                "VALUES ('a1', 'precision', 'api', 1629, 'Closed and done', 'done', "
                "1000.0, 2000.0, 'sonnet', 1.25, 4000, 700)"
            )
            coord_db.execute(
                "INSERT INTO merge_queue (assignment_id, repo_name, repo_github, "
                "branch, target_branch, issue_number, issue_title, state, last_attempt) "
                "VALUES ('a1', 'api', 'acme/api', 'b', 'main', 1629, "
                "'Closed and done', 'merged', 2500.0)"
            )

    def test_the_default_source_reads_the_real_tables(self, coord_db) -> None:
        self._seed(coord_db)
        result = run_trend(range="1d", until="6100", repo="api")
        assert sum(r["merged"] for r in result.rows) == 1
        priced = [r["cost_per_issue"] for r in result.rows if r["cost_per_issue"] is not None]
        assert priced == [pytest.approx(1.25)]

    def test_it_runs_through_the_cli(self, coord_db) -> None:
        self._seed(coord_db)
        result = CliRunner().invoke(
            main,
            ["report", "run", "trend", "--param", "until=6100",
             "--param", "range=1d", "--json"],
        )
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert body["report_id"] == "trend"
        assert sum(r["merged"] for r in body["rows"]) == 1

    def test_it_runs_through_the_daemon_route(self, report_client, rw_db) -> None:
        # `rw_db`, not `coord_db` — the ASGI worker thread runs the handler,
        # and the autouse `:memory:` conn is thread-bound (see that fixture).
        self._seed(rw_db)
        resp = report_client.get("/report/trend?until=6100&range=1d")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["report_id"] == "trend"
        assert sum(r["merged"] for r in body["rows"]) == 1


class TestTrendCatalogue:
    def test_trend_is_a_real_catalogue_entry(self) -> None:
        ids = [r["id"] for r in catalogue()["reports"]]
        assert "trend" in ids

    def test_its_params_are_range_until_and_repo(self) -> None:
        rep = next(r for r in catalogue()["reports"] if r["id"] == "trend")
        assert rep["title"] == "Trend"
        assert rep["description"]
        params = {p["id"]: p for p in rep["params"]}
        assert set(params) == {"range", "until", "repo"}
        assert params["range"]["kind"] == "choice"
        assert params["range"]["choices"] == list(TREND_RANGE_CHOICES)
        assert params["range"]["default"] == "7d"
        assert params["until"]["kind"] == "text"
        assert params["repo"]["default"] == ""

    def test_row_identity_is_none_rows_are_buckets_not_issues(self) -> None:
        rep = next(r for r in catalogue()["reports"] if r["id"] == "trend")
        assert rep["row_identity"] is None
