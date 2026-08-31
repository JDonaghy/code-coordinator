"""Tests for coord.forge_availability (#1896 Phase 0: forge/CI availability
measurement).

Scope per the issue's acceptance bar:
- one audit_log row per observation at each of the three seams, with
  timestamp/outcome/duration -- except `outcome="ok"`, which #2654 rolls up
  into one aggregate row per bucket (see TestOkRollup below);
- capture is strictly best-effort — a failure to record never raises, and
  neither does a failure while flushing a buffered "ok" aggregate;
- the read-out (`availability_report`) computes uptime %, longest
  contiguous unavailable stretch, and refusal counts by reason correctly
  over a seeded set of observations, identically whether those observations
  arrived as raw per-row writes or as rolled-up aggregates;
- the retention sweep bounds growth without deleting recent data;
- #2988: every `gh_call` row carries a normalised subcommand `shape` and a
  non-empty `caller` tag (never the pre-#2988 `argv0`-only key), the
  `ok`-aggregate buckets on `(caller, shape)`, and templated shapes actually
  collapse volume (851 distinct branch refs -> ONE shape key) -- see
  TestGhCallShape / TestCallerAttribution below.
"""

from __future__ import annotations

import json
import time

import pytest

from coord.forge_availability import (
    CATEGORY,
    EVENT_GH_CALL,
    EVENT_MERGE_GATE_REFUSAL,
    MERGE_GATE_REFUSAL_KINDS,
    RETENTION_DAYS,
    _OkAggregate,
    _flush_all_ok_aggregates,
    _infer_caller_tag,
    _maybe_prune,
    availability_report,
    format_report_lines,
    gh_call_shape,
    record_ci_check_fetch,
    record_gh_call,
    record_merge_gate_refusal,
    summary_line,
)


def _rows(coord_db, *, event_type: str | None = None) -> list:
    if event_type is None:
        return coord_db.execute(
            "SELECT * FROM audit_log WHERE category=? ORDER BY id", (CATEGORY,)
        ).fetchall()
    return coord_db.execute(
        "SELECT * FROM audit_log WHERE category=? AND event_type=? ORDER BY id",
        (CATEGORY, event_type),
    ).fetchall()


def _details(coord_db, *, event_type: str | None = None) -> list:
    return [json.loads(r["details_json"]) for r in _rows(coord_db, event_type=event_type)]


class TestRecordGhCall:
    def test_ok_observation_is_buffered_until_flushed(self, coord_db) -> None:
        """#2654: an `outcome="ok"` call does not write a row by itself --
        it accumulates in memory until a bucket roll / atexit / an
        interesting outcome (tested below) flushes it."""
        record_gh_call(("pr", "view", "1"), outcome="ok", duration_s=0.42)

        assert _rows(coord_db, event_type="gh_call") == []

        _flush_all_ok_aggregates()

        rows = _rows(coord_db, event_type="gh_call")
        assert len(rows) == 1
        assert rows[0]["tier"] == "operational"
        details = json.loads(rows[0]["details_json"])
        assert details["outcome"] == "ok"
        assert details["count"] == 1
        assert details["duration_s_total"] == pytest.approx(0.42)
        assert details["shape"] == "pr view {n}"
        assert details["caller"] == "tests.test_forge_availability"

    def test_records_unreachable_outcome(self, coord_db) -> None:
        record_gh_call(("pr", "view"), outcome="unreachable", duration_s=30.0,
                        detail="timed out")

        details = json.loads(_rows(coord_db, event_type="gh_call")[0]["details_json"])
        assert details["outcome"] == "unreachable"
        assert details["detail"] == "timed out"

    def test_never_raises_when_the_underlying_store_always_throws(
        self, coord_db, monkeypatch
    ) -> None:
        """Acceptance bar: 'Assert this with a store that always throws.'"""
        def _boom(*a, **k):
            raise RuntimeError("disk I/O error")

        monkeypatch.setattr("coord.forge_availability.record_audit", _boom)

        record_gh_call(("pr", "view"), outcome="unreachable", duration_s=0.1)  # must not raise

        assert _rows(coord_db) == []

    def test_ok_aggregate_flush_never_raises_when_store_always_throws(
        self, coord_db, monkeypatch
    ) -> None:
        """Same acceptance bar, extended to the new flush path (#2654) —
        the buffer must never become a way for measurement to take coord
        down, including at bucket-roll/atexit/pre-interesting-event flush."""
        def _boom(*a, **k):
            raise RuntimeError("disk I/O error")

        monkeypatch.setattr("coord.forge_availability.record_audit", _boom)

        record_gh_call(("pr", "view"), outcome="ok", duration_s=0.1)
        _flush_all_ok_aggregates()  # must not raise

        assert _rows(coord_db) == []


class TestOkRollup:
    """#2654: `outcome="ok"` observations accumulate into one aggregate row
    per bucket instead of one row each."""

    def test_multiple_ok_calls_in_one_bucket_produce_one_row(self, coord_db) -> None:
        record_gh_call(("pr",), outcome="ok", duration_s=0.1)
        record_gh_call(("pr",), outcome="ok", duration_s=0.2)
        record_gh_call(("pr",), outcome="ok", duration_s=0.3)
        _flush_all_ok_aggregates()

        rows = _rows(coord_db, event_type="gh_call")
        assert len(rows) == 1
        details = json.loads(rows[0]["details_json"])
        assert details["count"] == 3
        assert details["duration_s_total"] == pytest.approx(0.6)

    def test_ci_check_fetch_ok_calls_are_aggregated_per_repo_and_issue(
        self, coord_db
    ) -> None:
        # Two live reads of the SAME PR fold into one aggregate, with the
        # check-level conclusion distribution summed across the bucket.
        record_ci_check_fetch("acme/api", 1, outcome="ok", duration_s=0.4,
                               conclusions={"success": 2, "failure": 1})
        record_ci_check_fetch("acme/api", 1, outcome="ok", duration_s=0.5,
                               conclusions={"success": 3})
        # A different PR gets its own aggregate -- one row can't describe
        # two repos/issues at once.
        record_ci_check_fetch("acme/other", 2, outcome="ok", duration_s=0.2,
                               conclusions={"success": 1})
        _flush_all_ok_aggregates()

        rows = _rows(coord_db, event_type="ci_check_fetch")
        assert len(rows) == 2
        by_repo = {r["repo"]: json.loads(r["details_json"]) for r in rows}
        assert {r["repo"]: r["issue"] for r in rows} == {"acme/api": 1, "acme/other": 2}

        api_details = by_repo["acme/api"]
        assert api_details["outcome"] == "ok"
        assert api_details["count"] == 2
        assert api_details["duration_s_total"] == pytest.approx(0.9)
        assert api_details["conclusions"] == {"success": 5, "failure": 1}

        other_details = by_repo["acme/other"]
        assert other_details["count"] == 1
        assert other_details["conclusions"] == {"success": 1}

    def test_different_shapes_get_separate_aggregates(self, coord_db) -> None:
        record_gh_call(("pr",), outcome="ok", duration_s=0.1)
        record_gh_call(("issue",), outcome="ok", duration_s=0.1)
        _flush_all_ok_aggregates()

        rows = _details(coord_db, event_type="gh_call")
        assert {r["shape"] for r in rows} == {"pr", "issue"}
        assert all(r["count"] == 1 for r in rows)

    def test_different_callers_of_the_same_shape_get_separate_aggregates(
        self, coord_db
    ) -> None:
        """#2988: the bucket key is `(caller, shape)`, not `shape` alone --
        two different code paths hitting the exact same endpoint class must
        not be folded into one aggregate, or the whole point of this issue
        (attributing volume to a caller) is lost."""
        record_gh_call(("pr", "view", "1"), outcome="ok", duration_s=0.1,
                        caller="coord.reconcile")
        record_gh_call(("pr", "view", "2"), outcome="ok", duration_s=0.1,
                        caller="coord.drive")
        _flush_all_ok_aggregates()

        rows = _details(coord_db, event_type="gh_call")
        assert {(r["caller"], r["shape"]) for r in rows} == {
            ("coord.reconcile", "pr view {n}"),
            ("coord.drive", "pr view {n}"),
        }
        assert all(r["count"] == 1 for r in rows)

    def test_bucket_roll_flushes_the_old_bucket_and_starts_a_new_one(
        self, coord_db, monkeypatch
    ) -> None:
        t = [1_000_000.0]
        monkeypatch.setattr("coord.forge_availability.time.time", lambda: t[0])

        record_gh_call(("pr",), outcome="ok", duration_s=0.1)
        t[0] += 61.0  # past _OK_BUCKET_S (60s)
        record_gh_call(("pr",), outcome="ok", duration_s=0.2)

        # The roll flushed the first bucket already, without an explicit
        # flush call.
        rows = _details(coord_db, event_type="gh_call")
        assert len(rows) == 1
        assert rows[0]["count"] == 1
        assert rows[0]["duration_s_total"] == pytest.approx(0.1)

        _flush_all_ok_aggregates()
        rows = _details(coord_db, event_type="gh_call")
        assert len(rows) == 2
        assert rows[1]["count"] == 1
        assert rows[1]["duration_s_total"] == pytest.approx(0.2)

    def test_interesting_outcome_flushes_pending_ok_aggregate_first(
        self, coord_db
    ) -> None:
        """The aggregate must never land, chronologically, after an event
        it precedes -- an `unreachable` call flushes any pending `ok`
        aggregate for the same seam before writing its own row."""
        record_gh_call(("pr",), outcome="ok", duration_s=0.1)
        record_gh_call(("pr",), outcome="ok", duration_s=0.1)
        record_gh_call(("pr",), outcome="unreachable", duration_s=1.0)

        rows = _rows(coord_db, event_type="gh_call")
        assert len(rows) == 2
        first, second = (json.loads(r["details_json"]) for r in rows)
        assert first["outcome"] == "ok"
        assert first["count"] == 2
        assert second["outcome"] == "unreachable"

    def test_atexit_flush_is_registered_exactly_once(self, coord_db, monkeypatch) -> None:
        calls = []
        monkeypatch.setattr(
            "coord.forge_availability.atexit.register", lambda fn: calls.append(fn)
        )
        monkeypatch.setattr("coord.forge_availability._atexit_flush_registered", False)

        record_gh_call(("pr",), outcome="ok", duration_s=0.1)
        record_gh_call(("pr",), outcome="ok", duration_s=0.1)

        assert calls == [_flush_all_ok_aggregates]

    def test_pending_aggregate_from_a_stale_connection_is_dropped_not_flushed(
        self, coord_db
    ) -> None:
        """#2654: guards the exact hazard a shared in-memory buffer would
        otherwise create across tests (and, in principle, any other place
        the DB connection is swapped mid-process) -- an aggregate that
        started accumulating against a connection that is no longer current
        must be discarded, not written into whatever connection is current
        now."""
        import coord.forge_availability as fa

        fa._ok_aggregates[("gh_call", "stale")] = _OkAggregate(time.time(), 0.1)
        fa._ok_aggregates_conn = object()  # guaranteed not `is` the current connection

        _flush_all_ok_aggregates()

        assert _rows(coord_db) == []
        assert fa._ok_aggregates == {}


class TestGhCallShape:
    """#2988: `gh_call_shape` -- `argv[0]` plus a normalised `argv[1]`/
    `argv[2]` identifying the endpoint CLASS, with volatile parts (branch
    names, issue/PR numbers, owner/repo pairs) templated out so they
    aggregate instead of minting a fresh key per call."""

    def test_851_distinct_branch_refs_collapse_to_one_shape(self) -> None:
        """The issue's own acceptance bar, verbatim: 851 distinct branch
        refs must collapse to ONE shape key, not 851."""
        shapes = {
            gh_call_shape(("api", f"repos/acme/api/git/refs/heads/issue-{i}-x"))
            for i in range(851)
        }
        assert shapes == {"api repos/{owner}/{repo}/git/refs/heads/{branch}"}

    def test_repo_path_templates_owner_and_repo(self) -> None:
        assert (
            gh_call_shape(("api", "repos/acme/api/issues/123"))
            == "api repos/{owner}/{repo}/issues/{issue}"
        )
        assert (
            gh_call_shape(("api", "repos/other-org/other-repo/issues/999"))
            == "api repos/{owner}/{repo}/issues/{issue}"
        )

    def test_positional_pr_number_is_templated(self) -> None:
        assert gh_call_shape(("pr", "view", "123", "--repo", "acme/api")) == "pr view {n}"
        assert gh_call_shape(("pr", "view", "456", "--repo", "acme/api")) == "pr view {n}"

    def test_subcommand_words_are_not_templated(self) -> None:
        assert gh_call_shape(("issue", "list", "--repo", "acme/api")) == "issue list"
        assert gh_call_shape(("pr", "diff")) == "pr diff"

    def test_query_string_is_stripped(self) -> None:
        assert (
            gh_call_shape(("api", "repos/acme/api/pulls/7/commits?per_page=100"))
            == "api repos/{owner}/{repo}/pulls/{pr}/commits"
        )

    def test_flag_values_never_enter_the_shape(self) -> None:
        """A flag's VALUE (a repo slug, a JSON field list, a PR body) must
        never leak into the shape -- only the bare positional words before
        the first flag do. This is also the #2988 "no URL/token/path"
        guarantee: nothing past the first `-`-prefixed token is ever
        inspected."""
        shape = gh_call_shape((
            "issue", "edit", "42", "--repo", "acme/api",
            "--body", "see https://example.com/secret-token-abc123",
        ))
        assert "example.com" not in shape
        assert "secret-token" not in shape
        assert shape == "issue edit {n}"

    def test_empty_argv(self) -> None:
        assert gh_call_shape(()) == "(no args)"

    def test_graphql_query_body_is_not_in_the_shape(self) -> None:
        """`-f query=...` bodies can carry issue numbers / repo names in
        free text -- must never enter the shape (privacy + aggregation)."""
        shape = gh_call_shape((
            "api", "graphql", "-f", "query={ repository(owner: \"acme\") { id } }",
        ))
        assert shape == "api graphql"


class TestCallerAttribution:
    """#2988: every `record_gh_call` row carries a non-empty `caller` tag --
    an explicit one when given, else a module-name fallback -- so no row can
    ever record the pre-#2988 empty/absent attribution."""

    def test_explicit_caller_is_recorded_verbatim(self, coord_db) -> None:
        record_gh_call(
            ("pr", "view", "1"), outcome="unreachable", duration_s=1.0,
            caller="reconcile:false_merge_audit",
        )
        details = _details(coord_db, event_type="gh_call")
        assert details[0]["caller"] == "reconcile:false_merge_audit"

    def test_missing_caller_falls_back_to_a_non_empty_module_name(self, coord_db) -> None:
        """No call site -- tagged or not -- can ever record an empty
        `caller`. A call site that doesn't pass one gets the calling
        module's dotted name instead (module docstring: 'documented default
        that names the module')."""
        record_gh_call(("pr", "view"), outcome="unreachable", duration_s=1.0)
        details = _details(coord_db, event_type="gh_call")
        assert details[0]["caller"]
        assert details[0]["caller"] == "tests.test_forge_availability"

    def test_infer_caller_tag_never_returns_empty(self) -> None:
        assert _infer_caller_tag()

    def test_group_by_caller_answers_who_made_the_most_calls(self, coord_db) -> None:
        """The issue's own acceptance query, run for real against a seeded
        DB: `SELECT caller_tag, SUM(count) ... GROUP BY 1` (expressed here
        as `json_extract` over `details_json`, since `caller`/`count` live
        inside the JSON blob, same as every other audit_log detail)."""
        record_gh_call(("pr", "view", "1"), outcome="ok", duration_s=0.1,
                        caller="coord.reconcile")
        record_gh_call(("pr", "view", "2"), outcome="ok", duration_s=0.1,
                        caller="coord.reconcile")
        record_gh_call(("issue", "list"), outcome="ok", duration_s=0.1,
                        caller="coord.drive")
        _flush_all_ok_aggregates()

        rows = coord_db.execute(
            "SELECT json_extract(details_json, '$.caller') AS caller_tag, "
            "       SUM(json_extract(details_json, '$.count')) AS total "
            "FROM audit_log WHERE category=? AND event_type=? "
            "GROUP BY caller_tag ORDER BY total DESC",
            (CATEGORY, EVENT_GH_CALL),
        ).fetchall()
        totals = {r["caller_tag"]: r["total"] for r in rows}
        assert totals == {"coord.reconcile": 2, "coord.drive": 1}


class TestRecordCiCheckFetch:
    def test_never_raises_when_the_underlying_store_always_throws(
        self, coord_db, monkeypatch
    ) -> None:
        def _boom(*a, **k):
            raise RuntimeError("disk I/O error")

        monkeypatch.setattr("coord.forge_availability.record_audit", _boom)

        record_ci_check_fetch("acme/api", 1, outcome="unreachable", duration_s=30.0)

    def test_records_unreachable_with_repo_and_issue(self, coord_db) -> None:
        record_ci_check_fetch("acme/api", 42, outcome="unreachable", duration_s=30.0,
                               detail="timed out")

        row = _rows(coord_db, event_type="ci_check_fetch")[0]
        assert row["repo"] == "acme/api"
        assert row["issue"] == 42
        details = json.loads(row["details_json"])
        assert details["outcome"] == "unreachable"
        assert details["detail"] == "timed out"


class TestRecordMergeGateRefusal:
    def test_records_reason_and_message(self, coord_db) -> None:
        record_merge_gate_refusal(
            repo="api", issue=7, reason="checks_failed", message="build (failure)",
        )

        row = _rows(coord_db, event_type="merge_gate_refusal")[0]
        assert row["repo"] == "api"
        assert row["issue"] == 7
        details = json.loads(row["details_json"])
        assert details == {"reason": "checks_failed", "message": "build (failure)"}

    def test_scope_is_exactly_the_three_named_kinds(self) -> None:
        assert MERGE_GATE_REFUSAL_KINDS == {
            "checks_failed", "checks_pending", "checks_stale",
        }

    def test_never_raises_when_the_underlying_store_always_throws(
        self, coord_db, monkeypatch
    ) -> None:
        def _boom(*a, **k):
            raise RuntimeError("disk I/O error")

        monkeypatch.setattr("coord.forge_availability.record_audit", _boom)

        record_merge_gate_refusal(repo="api", issue=1, reason="checks_failed", message="x")


class TestAvailabilityReport:
    def test_empty_window_reports_no_observations(self, coord_db) -> None:
        report = availability_report(window_days=7.0, now=1_000_000.0)

        assert report.uptime_pct is None
        assert report.total_observations == 0
        assert report.refusals_by_reason == {}

    def test_uptime_pct_over_mixed_outcomes(self, coord_db) -> None:
        now = time.time()
        # 3 available, 1 unavailable => 75% uptime.
        record_gh_call(("a",), outcome="ok", duration_s=0.1)
        record_gh_call(("b",), outcome="app_error", duration_s=0.1)
        record_ci_check_fetch("api", 1, outcome="ok", duration_s=0.1, conclusions={})
        record_gh_call(("c",), outcome="unreachable", duration_s=1.0)
        _flush_all_ok_aggregates()

        report = availability_report(window_days=7.0, now=now + 10)

        assert report.gh_calls == 3
        assert report.ci_fetches == 1
        assert report.available == 3
        assert report.unavailable == 1
        assert report.uptime_pct == pytest.approx(75.0)

    def test_aggregate_count_weighs_availability_and_call_totals(self, coord_db) -> None:
        """A rolled-up aggregate with count=N must contribute N, not 1, to
        both `available` and `gh_calls` -- the whole point of #2654 is that
        this stays true even though it is now backed by one row."""
        for _ in range(5):
            record_gh_call(("a",), outcome="ok", duration_s=0.1)
        record_gh_call(("b",), outcome="unreachable", duration_s=1.0)
        _flush_all_ok_aggregates()

        assert len(_rows(coord_db, event_type="gh_call")) == 2  # 1 aggregate + 1 raw

        report = availability_report(window_days=7.0, now=time.time() + 10)

        assert report.gh_calls == 6
        assert report.available == 5
        assert report.unavailable == 1
        assert report.uptime_pct == pytest.approx(5 / 6 * 100)

    def test_excludes_observations_outside_the_window(self, coord_db) -> None:
        now = time.time()
        old_ts = now - 40 * 86400.0  # 40 days ago
        record_gh_call(("old",), outcome="unreachable", duration_s=1.0)
        # Force the row's ts to be outside a 30-day window.
        coord_db.execute("UPDATE audit_log SET ts=? WHERE category=?", (old_ts, CATEGORY))
        coord_db.commit()
        record_gh_call(("new",), outcome="ok", duration_s=0.1)
        _flush_all_ok_aggregates()

        report = availability_report(window_days=30.0, now=time.time() + 10)

        assert report.total_observations == 1
        assert report.available == 1

    def test_longest_unavailable_stretch_is_contiguous_run_span(self, coord_db) -> None:
        now = 1_000_000.0
        # Three consecutive unavailable observations, 100s apart, the last
        # one itself taking 5s -- span = (t0 + 200 + 5) - t0 = 205s.
        for i, ts_offset in enumerate((0.0, 100.0, 200.0)):
            record_gh_call((f"x{i}",), outcome="unreachable", duration_s=5.0)
            coord_db.execute(
                "UPDATE audit_log SET ts=? WHERE category=? AND id="
                "(SELECT MAX(id) FROM audit_log WHERE category=?)",
                (now + ts_offset, CATEGORY, CATEGORY),
            )
        coord_db.commit()
        # A later, available observation ends the run.
        record_gh_call(("ok",), outcome="ok", duration_s=0.1)
        _flush_all_ok_aggregates()
        coord_db.execute(
            "UPDATE audit_log SET ts=? WHERE category=? AND id="
            "(SELECT MAX(id) FROM audit_log WHERE category=?)",
            (now + 500.0, CATEGORY, CATEGORY),
        )
        coord_db.commit()

        report = availability_report(window_days=30.0, now=now + 1000.0)

        assert report.longest_unavailable_stretch_s == pytest.approx(205.0)

    def test_refusals_by_reason_counts(self, coord_db) -> None:
        record_merge_gate_refusal(repo="api", issue=1, reason="checks_failed", message="x")
        record_merge_gate_refusal(repo="api", issue=2, reason="checks_failed", message="y")
        record_merge_gate_refusal(repo="api", issue=3, reason="checks_pending", message="z")

        report = availability_report(window_days=7.0, now=time.time() + 10)

        assert report.refusals_by_reason == {"checks_failed": 2, "checks_pending": 1}

    def test_format_report_lines_and_summary_line_render(self, coord_db) -> None:
        record_gh_call(("a",), outcome="ok", duration_s=0.1)
        record_merge_gate_refusal(repo="api", issue=1, reason="checks_stale", message="x")
        _flush_all_ok_aggregates()

        report = availability_report(window_days=7.0, now=time.time() + 10)
        lines = format_report_lines(report)
        line = summary_line(report)

        assert any("uptime" in l for l in lines)
        assert any("checks_stale: 1" in l for l in lines)
        assert line.startswith("FORGE_AVAILABILITY: ")
        assert "uptime_pct=100.00" in line
        assert "refusals_total=1" in line

    def test_report_is_identical_whether_ok_rows_are_raw_or_rolled_up(
        self, coord_db
    ) -> None:
        """Acceptance bar: `coord diagnose --forge-availability` must report
        the same uptime_pct/longest_unavailable_stretch_s/refusal counts for
        a given synthetic observation sequence whether it lands as one raw
        row per "ok" observation (pre-#2654 shape, still possible for any
        row already on disk from before this change) or as a rolled-up
        aggregate (post-#2654 shape) -- computed here directly rather than
        taken on faith.
        """
        from coord.audit import record_audit

        base = 2_000_000.0

        # "Before" shape: hand-write one raw row per "ok" observation, plus
        # the non-"ok" observations and refusals exactly as recorded today.
        for i in range(4):
            record_audit(
                tier="operational", category=CATEGORY, event_type=EVENT_GH_CALL,
                actor="system", summary="ok", ts=base + i,
                details={"outcome": "ok", "duration_s": 0.1},
            )
        record_audit(
            tier="operational", category=CATEGORY, event_type=EVENT_GH_CALL,
            actor="system", summary="unreachable", ts=base + 10,
            details={"outcome": "unreachable", "duration_s": 2.0},
        )
        record_audit(
            tier="operational", category=CATEGORY, event_type=EVENT_MERGE_GATE_REFUSAL,
            actor="system", summary="blocked", ts=base + 20, repo="api", issue=1,
            details={"reason": "checks_failed", "message": "x"},
        )
        before = availability_report(window_days=30.0, now=base + 1000.0)

        # "After" shape: the same 4 "ok" observations, this time rolled up
        # into a single aggregate row via the real recording path.
        coord_db.execute("DELETE FROM audit_log")
        coord_db.commit()
        for _ in range(4):
            record_gh_call(("a",), outcome="ok", duration_s=0.1)
        _flush_all_ok_aggregates()
        record_gh_call(("b",), outcome="unreachable", duration_s=2.0)
        record_merge_gate_refusal(repo="api", issue=1, reason="checks_failed", message="x")
        after = availability_report(window_days=30.0, now=time.time() + 1000.0)

        assert len(_rows(coord_db, event_type="gh_call")) == 2  # 1 aggregate + 1 raw
        assert after.gh_calls == before.gh_calls == 5
        assert after.available == before.available == 4
        assert after.unavailable == before.unavailable == 1
        assert after.uptime_pct == pytest.approx(before.uptime_pct)
        assert after.refusals_by_reason == before.refusals_by_reason == {"checks_failed": 1}


class TestRetentionSweep:
    def test_prune_deletes_rows_older_than_retention_but_keeps_recent(
        self, coord_db
    ) -> None:
        now = time.time()
        record_gh_call(("old",), outcome="ok", duration_s=0.1)
        _flush_all_ok_aggregates()
        old_ts = now - (RETENTION_DAYS + 1) * 86400.0
        coord_db.execute("UPDATE audit_log SET ts=? WHERE category=?", (old_ts, CATEGORY))
        coord_db.commit()
        record_gh_call(("new",), outcome="ok", duration_s=0.1)
        _flush_all_ok_aggregates()

        _maybe_prune(force=True)

        rows = _rows(coord_db)
        assert len(rows) == 1
        assert json.loads(rows[0]["details_json"])["shape"] == "new"

    def test_prune_never_touches_other_categories(self, coord_db) -> None:
        from coord.audit import record_audit

        old_ts = time.time() - (RETENTION_DAYS + 1) * 86400.0
        record_audit(
            tier="business", category="merge", event_type="merged",
            actor="system", summary="unrelated", ts=old_ts,
        )

        _maybe_prune(force=True)

        rows = coord_db.execute(
            "SELECT * FROM audit_log WHERE category='merge'"
        ).fetchall()
        assert len(rows) == 1

    def test_prune_sweep_failure_never_raises(self, coord_db, monkeypatch) -> None:
        monkeypatch.setattr(
            "coord.db.get_connection",
            lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        _maybe_prune(force=True)  # must not raise
