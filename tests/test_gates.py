"""Tests for coord.gates (#1657) — `coord gates <repo> <issue>`'s read-only
core: the raw board-column dump plus the live review/test/merge gate
decision, including #1479 staleness.
"""

from __future__ import annotations

import json

import pytest

from coord import merge_queue as mq
from coord.config import Config, PipelineConfig, ReviewsConfig
from coord.gates import (
    REVIEW_REQUIRED,
    SMOKE_REQUIRED,
    build_gate_report,
    format_gate_report,
    report_to_dict,
)
from coord.models import Assignment, Board, Machine, Repo


@pytest.fixture
def config() -> Config:
    return Config(
        repos=[Repo(name="api", github="acme/api", default_branch="main")],
        machines=[Machine(name="precision", host="precision.tailnet", repos=["api"])],
    )


def _work(
    *,
    aid: str = "w1",
    issue: int = 42,
    branch: str | None = "issue-42-foo",
    status: str = "done",
    test_state: str | None = None,
    test_reason: str | None = None,
    test_head_sha: str | None = None,
    test_base_sha: str | None = None,
    test_patch_id: str | None = None,
    test_toolchain: str | None = None,
    review_state: str | None = None,
    review_verdict: str | None = None,
    required_gates: list[str] | None = None,
    dispatched_at: float | None = 1.0,
) -> Assignment:
    return Assignment(
        machine_name="precision",
        repo_name="api",
        issue_number=issue,
        issue_title="t",
        assignment_id=aid,
        type="work",
        status=status,
        branch=branch,
        test_state=test_state,
        test_reason=test_reason,
        test_head_sha=test_head_sha,
        test_base_sha=test_base_sha,
        test_patch_id=test_patch_id,
        test_toolchain=test_toolchain,
        review_state=review_state,
        review_verdict=review_verdict,
        required_gates=required_gates or [],
        dispatched_at=dispatched_at,
    )


def _review(
    of_aid: str,
    *,
    aid: str = "r1",
    issue: int = 42,
    verdict: str | None = "approve",
    review_head_sha: str | None = None,
    review_patch_id: str | None = None,
    dispatched_at: float | None = 2.0,
) -> Assignment:
    return Assignment(
        machine_name="dellserver",
        repo_name="api",
        issue_number=issue,
        issue_title="t",
        assignment_id=aid,
        type="review",
        status="done",
        review_of_assignment_id=of_aid,
        review_verdict=verdict,
        review_head_sha=review_head_sha,
        review_patch_id=review_patch_id,
        dispatched_at=dispatched_at,
    )


def _slice_test_author(
    *,
    aid: str = "ta1",
    tracking_issue: int = 1537,
    for_issue: int = 1544,
    status: str = "done",
    branch: str | None = "issue-1537-oracle-loop",
    dispatched_at: float | None = 1.0,
) -> Assignment:
    """A `test-author` row booked to the milestone's tracking issue but
    attributed (#1553's `for_issue_number`) to the child issue it's really
    for — the shape `coord acceptance author` dispatches."""
    return Assignment(
        machine_name="precision",
        repo_name="api",
        issue_number=tracking_issue,
        issue_title="[test-author] slice",
        assignment_id=aid,
        type="test-author",
        status=status,
        branch=branch,
        for_issue_number=for_issue,
        dispatched_at=dispatched_at,
    )


class FakeGh:
    """Stub gh_ops — returns fixed SHAs/patch-ids, records calls made."""

    def __init__(self, *, branch_sha="branchsha", base_sha="basesha", patch_id="patchid"):
        self.branch_sha = branch_sha
        self.base_sha = base_sha
        self.patch_id = patch_id
        self.sha_calls: list[tuple[str, str]] = []
        self.patch_calls: list[tuple[str, str, str]] = []

    def get_branch_sha(self, repo: str, branch: str) -> str | None:
        self.sha_calls.append((repo, branch))
        return self.branch_sha if branch != "main" else self.base_sha

    def get_branch_patch_id(self, repo: str, base: str, branch: str) -> str | None:
        self.patch_calls.append((repo, base, branch))
        return self.patch_id


# ── raw column dump ─────────────────────────────────────────────────────────

class TestRows:
    def test_no_assignments_found(self, config: Config) -> None:
        board = Board(active=[], completed=[])
        report = build_gate_report(board, config, "api", 42)
        assert report.rows == []
        assert report.decisions == []
        assert any("no assignments found" in n for n in report.notes)

    def test_row_dump_matches_assignment_columns(self, config: Config) -> None:
        work = _work(
            test_state="passed", test_reason="headless smoke",
            test_toolchain="rustc 1.95.0",
            review_state="done", review_verdict="approve",
        )
        board = Board(active=[], completed=[work])
        report = build_gate_report(board, config, "api", 42)
        assert len(report.rows) == 1
        row = report.rows[0]
        assert row.assignment_id == "w1"
        assert row.type == "work"
        assert row.status == "done"
        assert row.branch == "issue-42-foo"
        assert row.test_state == "passed"
        assert row.test_reason == "headless smoke"
        assert row.test_toolchain == "rustc 1.95.0"
        assert row.review_state == "done"
        assert row.review_verdict == "approve"
        assert row.review_of_assignment_id is None

    def test_row_dump_toolchain_defaults_to_none_for_a_historical_verdict(
        self, config: Config
    ) -> None:
        """#1629: a verdict recorded before this field existed must render as
        unknown (None), not break the report."""
        work = _work(test_state="passed")
        board = Board(active=[], completed=[work])
        report = build_gate_report(board, config, "api", 42)
        assert report.rows[0].test_toolchain is None

    def test_rows_scoped_to_repo_and_issue(self, config: Config) -> None:
        matching = _work(aid="w1", issue=42)
        other_issue = _work(aid="w2", issue=99)
        other_repo = Assignment(
            machine_name="m", repo_name="shared", issue_number=42,
            issue_title="t", assignment_id="w3", type="work",
        )
        board = Board(active=[], completed=[matching, other_issue, other_repo])
        report = build_gate_report(board, config, "api", 42)
        assert [r.assignment_id for r in report.rows] == ["w1"]

    def test_rows_sorted_chronologically(self, config: Config) -> None:
        first = _work(aid="w1", dispatched_at=5.0)
        second = _work(aid="fix-w1", dispatched_at=1.0)
        board = Board(active=[], completed=[first, second])
        report = build_gate_report(board, config, "api", 42)
        assert [r.assignment_id for r in report.rows] == ["fix-w1", "w1"]

    def test_repo_not_in_config_still_dumps_rows(self) -> None:
        # A board row whose repo_name is real, but coordinator.yml doesn't
        # (yet, or anymore) carry that repo — the raw columns must still be
        # readable even though the live gate decision can't be computed.
        bare_config = Config(repos=[], machines=[])
        work = _work()
        board = Board(active=[], completed=[work])
        report = build_gate_report(board, bare_config, "api", 42)
        assert len(report.rows) == 1
        assert report.decisions == []
        assert any("not in coordinator.yml" in n for n in report.notes)

    def test_no_work_like_assignment_leaves_decision_empty(self, config: Config) -> None:
        review = _review("ghost")
        board = Board(active=[], completed=[review])
        report = build_gate_report(board, config, "api", 42)
        assert len(report.rows) == 1
        assert report.decisions == []
        assert any("no work-like assignment" in n for n in report.notes)

    def test_winner_with_no_branch_leaves_decision_empty(self, config: Config) -> None:
        work = _work(branch=None)
        board = Board(active=[], completed=[work])
        report = build_gate_report(board, config, "api", 42)
        assert report.decisions == []
        assert any("no branch" in n for n in report.notes)

    def test_winner_picks_most_recently_dispatched_work_row(self, config: Config) -> None:
        # A bounce/fix chain: two work-like rows on the same issue — the gate
        # decision must track the most recent one's branch, not the first.
        original = _work(aid="w1", branch="issue-42-orig", dispatched_at=1.0)
        fix = _work(aid="fix-w1", branch="issue-42-fix", dispatched_at=2.0)
        board = Board(active=[], completed=[original, fix])
        report = build_gate_report(board, config, "api", 42)
        assert report.branch == "issue-42-fix"


# ── #1730: oracle-loop slice attribution (for_issue_number) ─────────────────

class TestSliceAttribution:
    """#1553 taught the TUI to resolve `for_issue_number` when attributing an
    oracle-loop slice's work to its child issue; `coord gates` never got the
    same fix, so it reported "no assignments found" for a child issue with a
    finished, reviewed, PR-open slice in flight (#1730)."""

    def test_finds_slice_by_the_child_issue_it_is_for(self, config: Config) -> None:
        ta = _slice_test_author(tracking_issue=1537, for_issue=1544)
        board = Board(active=[], completed=[ta])

        report = build_gate_report(board, config, "api", 1544)

        assert [r.assignment_id for r in report.rows] == ["ta1"]
        assert report.notes == []  # no "no assignments found"

    def test_still_finds_it_when_queried_by_the_tracking_issue(self, config: Config) -> None:
        # Do not "fix" this by moving the row to the child — the tracking
        # issue must keep finding its own rows too.
        ta = _slice_test_author(tracking_issue=1537, for_issue=1544)
        board = Board(active=[], completed=[ta])

        report = build_gate_report(board, config, "api", 1537)

        assert [r.assignment_id for r in report.rows] == ["ta1"]

    def test_does_not_leak_into_an_unrelated_child(self, config: Config) -> None:
        ta = _slice_test_author(tracking_issue=1537, for_issue=1544)
        board = Board(active=[], completed=[ta])

        report = build_gate_report(board, config, "api", 1999)

        assert report.rows == []
        assert any("no assignments found" in n for n in report.notes)

    def test_ordinary_assignment_with_no_for_issue_number_is_unchanged(
        self, config: Config
    ) -> None:
        work = _work(aid="w1", issue=42)
        board = Board(active=[], completed=[work])

        report = build_gate_report(board, config, "api", 42)

        assert [r.assignment_id for r in report.rows] == ["w1"]
        row = report.rows[0]
        assert row.issue_number == 42
        assert row.for_issue_number is None

    def test_row_carries_both_the_booked_and_for_issue_numbers(self, config: Config) -> None:
        ta = _slice_test_author(tracking_issue=1537, for_issue=1544)
        board = Board(active=[], completed=[ta])

        report = build_gate_report(board, config, "api", 1544)

        row = report.rows[0]
        assert row.issue_number == 1537
        assert row.for_issue_number == 1544

    def test_format_makes_the_two_numbers_legible(self, config: Config) -> None:
        ta = _slice_test_author(tracking_issue=1537, for_issue=1544)
        board = Board(active=[], completed=[ta])
        report = build_gate_report(board, config, "api", 1544)

        text = format_gate_report(report)

        assert "booked_to=#1537" in text
        assert "for=#1544" in text

    def test_format_omits_attribution_suffix_for_ordinary_rows(self, config: Config) -> None:
        work = _work(aid="w1", issue=42)
        board = Board(active=[], completed=[work])
        report = build_gate_report(board, config, "api", 42)

        assert "booked_to=" not in format_gate_report(report)


# ── gate decision ────────────────────────────────────────────────────────────

class TestDecision:
    def test_review_and_test_pass_merge_ready(self, config: Config) -> None:
        work = _work(test_state="passed")
        review = _review("w1", verdict="approve")
        board = Board(active=[], completed=[work, review])
        report = build_gate_report(board, config, "api", 42, gh_ops=FakeGh())

        by_gate = {d.gate: d for d in report.decisions}
        assert by_gate["review"].ok is True
        assert by_gate["test"].ok is True
        assert by_gate["merge"].ok is True
        assert by_gate["merge"].reason is None
        assert any("CI checks" in n for n in report.notes)

    def test_review_not_approved_blocks_merge(self, config: Config) -> None:
        work = _work(test_state="passed")
        board = Board(active=[], completed=[work])  # no review at all
        report = build_gate_report(board, config, "api", 42, gh_ops=FakeGh())

        by_gate = {d.gate: d for d in report.decisions}
        assert by_gate["review"].required is True
        assert by_gate["review"].ok is False
        assert by_gate["merge"].ok is False
        assert by_gate["merge"].reason == REVIEW_REQUIRED

    def test_smoke_missing_blocks_merge(self, config: Config) -> None:
        work = _work(test_state=None)  # never tested
        review = _review("w1", verdict="approve")
        board = Board(active=[], completed=[work, review])
        report = build_gate_report(board, config, "api", 42, gh_ops=FakeGh())

        by_gate = {d.gate: d for d in report.decisions}
        assert by_gate["test"].required is True
        assert by_gate["test"].ok is False
        assert by_gate["test"].anchor is None  # MISSING, not STALE
        assert by_gate["merge"].reason == SMOKE_REQUIRED

    def test_stale_base_names_1479_and_shas(self, config: Config) -> None:
        # #1479: the verdict was recorded against an old base SHA; the base
        # has since moved. The branch's own head/patch-id are unchanged.
        work = _work(
            test_state="passed", test_reason="headless smoke",
            test_head_sha="branchsha", test_base_sha="oldbase",
        )
        review = _review("w1", verdict="approve", review_head_sha="branchsha")
        board = Board(active=[], completed=[work, review])
        gh = FakeGh(branch_sha="branchsha", base_sha="newbase", patch_id="samepatch")
        report = build_gate_report(board, config, "api", 42, gh_ops=gh)

        by_gate = {d.gate: d for d in report.decisions}
        test_decision = by_gate["test"]
        assert test_decision.ok is False
        assert test_decision.anchor == "base"
        assert test_decision.recorded_sha == "oldbase"
        assert test_decision.current_sha == "newbase"
        assert "#1479" not in (test_decision.reason or "")  # reason is merge_queue's own wording
        assert by_gate["merge"].reason == SMOKE_REQUIRED
        # gh_ops was actually consulted for both the branch and the base.
        assert ("acme/api", "issue-42-foo") in gh.sha_calls
        assert ("acme/api", "main") in gh.sha_calls

    def test_stale_branch_content_change(self, config: Config) -> None:
        # Branch content changed (patch-id differs) since the test ran —
        # anchor="branch", not "base".
        work = _work(
            test_state="passed",
            test_head_sha="oldbranchsha", test_base_sha="basesha",
            test_patch_id="oldpatch",
        )
        review = _review("w1", verdict="approve", review_head_sha="newbranchsha",
                          review_patch_id="newpatch")
        board = Board(active=[], completed=[work, review])
        gh = FakeGh(branch_sha="newbranchsha", base_sha="basesha", patch_id="newpatch")
        report = build_gate_report(board, config, "api", 42, gh_ops=gh)

        by_gate = {d.gate: d for d in report.decisions}
        test_decision = by_gate["test"]
        assert test_decision.ok is False
        assert test_decision.anchor == "branch"
        assert test_decision.recorded_sha == "oldbranchsha"
        assert test_decision.current_sha == "newbranchsha"

    def test_gates_disabled_merge_ready_with_no_evidence(self, config: Config) -> None:
        config.reviews = ReviewsConfig(enabled=False)
        config.pipeline = PipelineConfig(default_gates=["merge"])
        work = _work(test_state=None)  # never tested — but gate is off
        board = Board(active=[], completed=[work])
        report = build_gate_report(board, config, "api", 42, gh_ops=FakeGh())

        by_gate = {d.gate: d for d in report.decisions}
        assert by_gate["review"].required is False
        assert by_gate["test"].required is False
        assert by_gate["merge"].ok is True

    def test_review_verdict_unparseable_reported_distinctly(self, config: Config) -> None:
        """#1956: a review row that finished (status='done') with NO
        parseable verdict must be reported distinctly from a review that
        simply hasn't run yet (test_review_not_approved_blocks_merge above)
        — needs operator recovery, not another dispatched review."""
        work = _work(test_state="passed")
        review = _review("w1", verdict=None)  # status="done" per `_review`'s default
        board = Board(active=[], completed=[work, review])
        report = build_gate_report(board, config, "api", 42, gh_ops=FakeGh())

        by_gate = {d.gate: d for d in report.decisions}
        assert by_gate["review"].ok is False
        assert by_gate["review"].verdict_unparseable is True
        assert "r1" in by_gate["review"].reason
        assert "coord report-result" in by_gate["review"].reason
        assert "--verdict-source recovered" in by_gate["review"].reason
        assert by_gate["merge"].ok is False
        assert by_gate["merge"].reason == REVIEW_REQUIRED

    def test_review_not_approved_without_verdict_row_is_not_unparseable(
        self, config: Config,
    ) -> None:
        """The ordinary 'no review dispatched at all' case (no review row on
        the board) must NOT be misreported as verdict_unparseable — that flag
        is specifically for a review row that FINISHED with a NULL verdict."""
        work = _work(test_state="passed")
        board = Board(active=[], completed=[work])  # no review row at all
        report = build_gate_report(board, config, "api", 42, gh_ops=FakeGh())

        by_gate = {d.gate: d for d in report.decisions}
        assert by_gate["review"].ok is False
        assert by_gate["review"].verdict_unparseable is False
        assert by_gate["review"].reason == "review required but not approved"

    def test_review_request_changes_is_not_unparseable(self, config: Config) -> None:
        """A review that genuinely requested changes (a real, present verdict)
        must not be misreported as the #1956 defect either."""
        work = _work(test_state="passed")
        review = _review("w1", verdict="request-changes")
        board = Board(active=[], completed=[work, review])
        report = build_gate_report(board, config, "api", 42, gh_ops=FakeGh())

        by_gate = {d.gate: d for d in report.decisions}
        assert by_gate["review"].ok is False
        assert by_gate["review"].verdict_unparseable is False

    def test_gh_ops_none_skips_live_lookups_fail_open(self, config: Config) -> None:
        # #1479-review: without gh_ops, `evaluate_smoke_verdict`'s staleness
        # comparison has no live SHAs to compare against, so the recorded
        # test verdict is trusted as-is (the pre-#1479 fail-open convention,
        # unchanged by #2085 — smoke staleness is a separate question from
        # review staleness and this test still pins its behaviour).
        work = _work(
            test_state="passed",
            test_head_sha="branchsha", test_base_sha="oldbase",
        )
        review = _review("w1", verdict="approve", review_head_sha="branchsha")
        board = Board(active=[], completed=[work, review])
        report = build_gate_report(board, config, "api", 42, gh_ops=None)

        by_gate = {d.gate: d for d in report.decisions}
        assert by_gate["test"].ok is True
        # #2085: `has_approved_review` no longer fails OPEN when it cannot
        # confirm the branch's current head — and without `gh_ops`, this
        # `entry.branch_head_sha` is never populated (see the "populate the
        # freshness anchors LIVE" block above, guarded on `gh_ops is not
        # None`) even though the review DOES carry a `review_head_sha`. A
        # `coord gates` run with no live GitHub access can no longer report
        # "merge READY" for something `coord merge --dry-run` (which always
        # has `gh_ops`) would refuse — the exact board-vs-gate disagreement
        # #2085 traces to this same fail-open shape at other call sites.
        assert by_gate["merge"].ok is False
        assert by_gate["merge"].reason == "review_required"
        # #2704: the review gate's OWN reason must not fabricate "not
        # approved" for a branch head this report could never confirm —
        # exactly the `coord gates`/`coord drive-queue diagnose` surface the
        # issue's incident report quotes verbatim.
        assert by_gate["review"].reason == mq.UNKNOWN_BRANCH_HEAD_REASON
        assert report.target_branch == "main"  # falls back, no milestone lookup

    def test_unknown_head_reason_not_fabricated_as_not_approved(self, config: Config) -> None:
        """#2704 regression, dedicated: `build_gate_report` must consult
        `ApprovalScan.unknown_head` (via `scan_approved_reviews`) instead of
        collapsing every `has_approved_review() is False` into the generic
        "review required but not approved" — the literal repro from the
        issue body (`coord drive-queue diagnose` printing that reason for
        every board row whose branch head could not be read)."""
        work = _work(test_state="passed")
        # Review DID capture a head SHA to compare, but this report has no
        # gh_ops — entry.branch_head_sha is never populated, so the scan
        # cannot confirm OR refute freshness: unknown, not refused.
        review = _review("w1", verdict="approve", review_head_sha="branchsha")
        board = Board(active=[], completed=[work, review])
        report = build_gate_report(board, config, "api", 42, gh_ops=None)

        by_gate = {d.gate: d for d in report.decisions}
        assert by_gate["review"].ok is False
        assert by_gate["review"].reason == mq.UNKNOWN_BRANCH_HEAD_REASON
        assert by_gate["review"].reason != "review required but not approved"
        assert by_gate["review"].verdict_unparseable is False


# ── is_interactive enrichment (#748/#632: not an Assignment dataclass field) ─

class TestIsInteractive:
    def test_backfills_from_assignments_table(self, config: Config, coord_db) -> None:
        from coord.state import _mark_assignment_interactive_local, save_board

        work = _work()
        board = Board(active=[], completed=[work])
        save_board(board)
        _mark_assignment_interactive_local("w1")

        report = build_gate_report(board, config, "api", 42)
        assert report.rows[0].is_interactive is True

    def test_none_when_row_not_persisted(self, config: Config, coord_db) -> None:
        work = _work()
        board = Board(active=[], completed=[work])
        report = build_gate_report(board, config, "api", 42)
        assert report.rows[0].is_interactive is None


# ── formatting / JSON round-trip ────────────────────────────────────────────

class TestFormatting:
    def test_format_includes_stale_wording(self, config: Config) -> None:
        work = _work(
            test_state="passed", test_head_sha="branchsha", test_base_sha="oldbase",
        )
        review = _review("w1", verdict="approve", review_head_sha="branchsha")
        board = Board(active=[], completed=[work, review])
        gh = FakeGh(branch_sha="branchsha", base_sha="newbase", patch_id="samepatch")
        report = build_gate_report(board, config, "api", 42, gh_ops=gh)

        text = format_gate_report(report)
        assert "STALE" in text
        assert "#1479" in text
        assert "oldbase"[:7] in text
        assert "newbase"[:7] in text
        assert "BLOCKED" in text

    def test_format_shows_error_not_blocked_for_unparseable_verdict(
        self, config: Config,
    ) -> None:
        """#1956: rendered as 'ERROR', never 'BLOCKED' — 'BLOCKED' reads
        identically to 'not yet reviewed'/'requested changes', exactly the
        ambiguity #1956 reports."""
        work = _work(test_state="passed")
        review = _review("w1", verdict=None)
        board = Board(active=[], completed=[work, review])
        report = build_gate_report(board, config, "api", 42, gh_ops=FakeGh())

        text = format_gate_report(report)
        assert "review : ERROR" in text
        assert "review : BLOCKED" not in text
        assert "coord report-result" in text

    def test_format_shows_verdict_source_when_present(self, config: Config) -> None:
        work = _work(test_state="passed")
        review = _review("w1", verdict="approve")
        review.verdict_source = "recovered"
        review.verdict_source_reason = "REVIEW_VERDICT header missing, recovered from transcript"
        board = Board(active=[], completed=[work, review])
        report = build_gate_report(board, config, "api", 42, gh_ops=FakeGh())

        text = format_gate_report(report)
        assert "verdict_source=recovered" in text
        assert "recovered from transcript" in text

    def test_format_omits_verdict_source_line_when_none(self, config: Config) -> None:
        """The overwhelming common case (verdict_source=None, meaning
        'agent') must not print noise on every ordinary row."""
        work = _work(test_state="passed")
        review = _review("w1", verdict="approve")
        board = Board(active=[], completed=[work, review])
        report = build_gate_report(board, config, "api", 42, gh_ops=FakeGh())

        text = format_gate_report(report)
        assert "verdict_source" not in text

    def test_format_omits_verdict_source_line_when_explicitly_agent(
        self, config: Config,
    ) -> None:
        """#1956 review follow-up: `issue_store._persist_verdict_source`
        always stamps the literal string "agent" (never leaves the column
        NULL) on every `coord report-result --verdict` call going forward —
        so the "quiet common case" from the test above must also hold when
        `verdict_source` is the explicit string "agent", not just `None`,
        or every ordinary review row would print noise post-#1956."""
        work = _work(test_state="passed")
        review = _review("w1", verdict="approve")
        review.verdict_source = "agent"
        board = Board(active=[], completed=[work, review])
        report = build_gate_report(board, config, "api", 42, gh_ops=FakeGh())

        text = format_gate_report(report)
        assert "verdict_source" not in text

    def test_format_shows_toolchain_and_falls_back_to_unknown(self, config: Config) -> None:
        with_toolchain = _work(test_state="passed", test_toolchain="rustc 1.95.0")
        board = Board(active=[], completed=[with_toolchain])
        report = build_gate_report(board, config, "api", 42)
        assert "test_toolchain=rustc 1.95.0" in format_gate_report(report)

        no_toolchain = _work(test_state="passed")
        board2 = Board(active=[], completed=[no_toolchain])
        report2 = build_gate_report(board2, config, "api", 42)
        assert "test_toolchain=unknown" in format_gate_report(report2)

    def test_report_to_dict_is_json_serializable(self, config: Config) -> None:
        work = _work(test_state="passed", test_toolchain="node 20.11.0")
        review = _review("w1", verdict="approve")
        board = Board(active=[], completed=[work, review])
        report = build_gate_report(board, config, "api", 42, gh_ops=FakeGh())

        payload = report_to_dict(report)
        # Must round-trip through json.dumps with no dataclass instances left.
        text = json.dumps(payload)
        reloaded = json.loads(text)
        assert reloaded["repo_name"] == "api"
        assert reloaded["issue_number"] == 42
        assert len(reloaded["rows"]) == 2
        assert len(reloaded["decisions"]) == 3
        assert reloaded["rows"][0]["test_toolchain"] == "node 20.11.0"

    def test_never_mutates_board_or_calls_write_seams(
        self, config: Config, monkeypatch,
    ) -> None:
        """Read-only guarantee: build_gate_report must never call
        save_board/save_queue — the whole point of #1657 (see the sibling
        #coord-diagnose-writes issue this explicitly calls out)."""
        import coord.state as state_mod
        import coord.merge_queue as mq_mod

        def _boom(*a, **k):
            raise AssertionError("build_gate_report must never write")

        monkeypatch.setattr(state_mod, "save_board", _boom, raising=False)
        monkeypatch.setattr(mq_mod, "save_queue", _boom, raising=False)

        work = _work(test_state="passed")
        review = _review("w1", verdict="approve")
        board = Board(active=[], completed=[work, review])
        build_gate_report(board, config, "api", 42, gh_ops=FakeGh())


# ── #2024: `coord gates` vs the driver — same branch, two different rows ────


class TestFixRoundTestGateAttribution:
    """The #2024 reading gap, live on JDonaghy/vimcode#635 (2026-08-08).

    A `--fix-of` round is a NEW work row on the SAME branch, carrying its own
    empty ``test_state``. The merge gate is branch-scoped by design (#1819: a
    Test run measures the ``(branch, base)`` pair), so it is satisfied by the
    PARENT's verdict and ``coord gates`` printed a bare ``test : passed`` —
    while ``coord drive`` and ``dispatch_pending_reviews``, which gate on the
    CURRENT row's own verdict, correctly held on ``test=-``. Neither reading
    was wrong; the summary was silent about which row it described, and that
    silence is what turned a blocked pipeline into an invisible one (25
    minutes, then 160).
    """

    def _fix_chain(self):
        """work (test passed) → review request-changes → fix round, untested,
        all on ONE branch."""
        parent = _work(aid="8965c04", test_state="passed", dispatched_at=1.0)
        review = _review("8965c04", aid="b4f2741", verdict="request-changes",
                         dispatched_at=2.0)
        fix = _work(aid="78cfb47", test_state=None, dispatched_at=3.0)
        fix.review_of_assignment_id = "8965c04"
        fix.review_iteration = 1
        return parent, review, fix

    def test_test_decision_names_the_row_that_supplied_the_verdict(
        self, config: Config,
    ) -> None:
        parent, review, fix = self._fix_chain()
        review.review_verdict = "approve"  # so only the TEST gate is at issue
        board = Board(active=[], completed=[parent, review, fix])
        report = build_gate_report(board, config, "api", 42, gh_ops=FakeGh())

        test = {d.gate: d for d in report.decisions}["test"]
        assert test.ok is True
        # The verdict is the PARENT's, and the report now says so.
        assert test.assignment_id == "8965c04"

    def test_note_states_the_current_row_has_no_verdict_of_its_own(
        self, config: Config,
    ) -> None:
        parent, review, fix = self._fix_chain()
        review.review_verdict = "approve"
        board = Board(active=[], completed=[parent, review, fix])
        report = build_gate_report(board, config, "api", 42, gh_ops=FakeGh())

        note = next((n for n in report.notes if "#2024" in n), None)
        assert note is not None, report.notes
        assert "8965c04" in note      # where the verdict came from
        assert "78cfb47" in note      # the row the driver is actually gating on
        assert "coord test 78cfb47 --passed" in note

    def test_no_note_when_the_current_row_carries_its_own_verdict(
        self, config: Config,
    ) -> None:
        """The ordinary case — every round tested — must stay quiet, or the
        note becomes noise nobody reads."""
        parent, review, fix = self._fix_chain()
        review.review_verdict = "approve"
        fix.test_state = "passed"
        board = Board(active=[], completed=[parent, review, fix])
        report = build_gate_report(board, config, "api", 42, gh_ops=FakeGh())

        assert not any("#2024" in n for n in report.notes)

    def test_no_note_on_a_single_row_issue(self, config: Config) -> None:
        work = _work(test_state="passed")
        review = _review("w1", verdict="approve")
        board = Board(active=[], completed=[work, review])
        report = build_gate_report(board, config, "api", 42, gh_ops=FakeGh())

        assert not any("#2024" in n for n in report.notes)

    def test_format_prints_the_row_the_verdict_was_recorded_on(
        self, config: Config,
    ) -> None:
        parent, review, fix = self._fix_chain()
        review.review_verdict = "approve"
        board = Board(active=[], completed=[parent, review, fix])
        report = build_gate_report(board, config, "api", 42, gh_ops=FakeGh())

        text = format_gate_report(report)
        assert "test   : passed (recorded on 8965c04)" in text
