"""Tests for #4187: stable ids on scoped re-review blocking findings, the
structured resolved/unresolved/new_blocking_in_delta response, and the
out-of-delta merge gate.
"""

from __future__ import annotations

from unittest.mock import patch

from coord.merge_queue import QueuedMerge, out_of_delta_full_review_pending
from coord.models import Assignment, Board
from coord.review import (
    BlockingFinding,
    build_scoped_review_briefing,
    finding_id,
    format_findings_with_ids,
    parse_blocking_findings,
    parse_scoped_rereview_classification,
)


# ── finding_id / parse_blocking_findings ────────────────────────────────────

class TestFindingId:
    def test_deterministic_for_identical_text(self) -> None:
        assert finding_id("the worktree leaks on failure") == finding_id(
            "the worktree leaks on failure"
        )

    def test_whitespace_normalized(self) -> None:
        # Minor formatting (extra spaces/newlines) must not mint a new id —
        # otherwise re-deriving an id from a slightly-reflowed echo of the
        # same finding would silently disagree with the original.
        assert finding_id("a   b\nc") == finding_id("a b c")

    def test_different_text_different_id(self) -> None:
        assert finding_id("finding one") != finding_id("finding two")

    def test_short_and_hex(self) -> None:
        fid = finding_id("some finding")
        assert len(fid) == 6
        int(fid, 16)  # raises if not hex


class TestParseBlockingFindings:
    def test_no_blocking_section_returns_empty(self) -> None:
        body = "## Non-blocking concerns\n- a nit\n\n## Nits\nNone.\n"
        assert parse_blocking_findings(body) == []

    def test_explicit_none_returns_empty(self) -> None:
        body = "## Blocking findings\nNone.\n\n## Nits\nNone.\n"
        assert parse_blocking_findings(body) == []

    def test_splits_top_level_bullets(self) -> None:
        body = (
            "## Blocking findings\n"
            "- the worktree leaks a tempdir on failure\n"
            "- the retry loop never backs off\n"
        )
        findings = parse_blocking_findings(body)
        assert [f.text for f in findings] == [
            "the worktree leaks a tempdir on failure",
            "the retry loop never backs off",
        ]
        assert findings[0].id != findings[1].id
        # ids are content-derived, re-derivable from the text alone
        assert findings[0].id == finding_id(findings[0].text)

    def test_continuation_lines_fold_into_the_same_finding(self) -> None:
        body = (
            "## Blocking findings\n"
            "- the retry loop never backs off, which means:\n"
            "  - it hammers the API on every tick\n"
            "  - it never gives up\n"
            "- a second, unrelated finding\n"
        )
        findings = parse_blocking_findings(body)
        assert len(findings) == 2
        assert "it hammers the API on every tick" in findings[0].text
        assert "it never gives up" in findings[0].text
        assert findings[1].text == "a second, unrelated finding"

    def test_prose_without_bullets_becomes_one_finding(self) -> None:
        body = "## Blocking findings\nThe worktree leaks a tempdir on failure.\n"
        findings = parse_blocking_findings(body)
        assert len(findings) == 1
        assert findings[0].text == "The worktree leaks a tempdir on failure."


class TestFormatFindingsWithIds:
    def test_renders_tagged_bullets(self) -> None:
        findings = [BlockingFinding(id="abc123", text="first finding")]
        rendered = format_findings_with_ids(findings)
        assert rendered == "- `finding#abc123`: first finding"

    def test_multi_line_finding_indents_continuation(self) -> None:
        findings = [BlockingFinding(id="abc123", text="first line\nsecond line")]
        rendered = format_findings_with_ids(findings)
        assert rendered.splitlines() == [
            "- `finding#abc123`: first line",
            "  second line",
        ]


# ── build_scoped_review_briefing: ids + structured-response instructions ───

class TestBuildScopedReviewBriefingFindingIds:
    def _briefing(self, prior_findings_body: str) -> str:
        return build_scoped_review_briefing(
            pr_number=42,
            pr_url="https://github.com/acme/api/pull/42",
            repo_github="acme/api",
            repo_name="api",
            issue_number=7,
            issue_title="Fix the thing",
            branch="worker/a",
            resolution_delta="diff --git a/x b/x\n+fix\n",
            prior_verdict="request-changes",
            prior_findings_body=(
                "## Blocking findings\n"
                "- the worktree leaks a tempdir on failure\n"
            ),
        )

    def test_includes_id_tagged_findings(self) -> None:
        briefing = self._briefing("unused")
        fid = finding_id("the worktree leaks a tempdir on failure")
        assert f"finding#{fid}" in briefing

    def test_instructs_finding_resolution_section(self) -> None:
        briefing = self._briefing("unused")
        assert "## Finding resolution" in briefing
        assert "resolved:" in briefing
        assert "unresolved:" in briefing

    def test_instructs_in_delta_and_out_of_delta_headings(self) -> None:
        briefing = self._briefing("unused")
        assert "## New blocking findings (in delta)" in briefing
        assert "## New blocking findings (out of delta)" in briefing
        # The out-of-delta heading must explain the non-blocking-but-gated
        # contract, not just name itself.
        assert "non-blocking" in briefing.lower()
        assert "full" in briefing.lower()

    def test_no_prior_findings_omits_structured_instructions(self) -> None:
        briefing = build_scoped_review_briefing(
            pr_number=42,
            pr_url=None,
            repo_github="acme/api",
            repo_name="api",
            issue_number=7,
            issue_title="Fix the thing",
            branch="worker/a",
            resolution_delta="diff --git a/x b/x\n+fix\n",
            prior_verdict="request-changes",
            prior_findings_body="## Blocking findings\nNone.\n",
        )
        assert "## Finding resolution" not in briefing


# ── parse_scoped_rereview_classification ────────────────────────────────────

class TestParseScopedRereviewClassification:
    def test_explicit_resolved_and_unresolved(self) -> None:
        body = (
            "## Blocking findings\nNone.\n\n"
            "## Finding resolution\n"
            "resolved: finding#aaa111, finding#bbb222\n"
            "unresolved: ccc333\n"
        )
        result = parse_scoped_rereview_classification(
            body, prior_ids=["aaa111", "bbb222", "ccc333"],
        )
        assert result.resolved_ids == frozenset({"aaa111", "bbb222"})
        assert result.unresolved_ids == frozenset({"ccc333"})

    def test_omitted_prior_id_fails_closed_to_unresolved(self) -> None:
        body = "## Finding resolution\nresolved: aaa111\nunresolved: (none)\n"
        result = parse_scoped_rereview_classification(
            body, prior_ids=["aaa111", "bbb222"],
        )
        assert result.resolved_ids == frozenset({"aaa111"})
        # bbb222 was never mentioned anywhere -- must not read as resolved.
        assert "bbb222" in result.unresolved_ids

    def test_missing_section_entirely_carries_every_prior_id_forward(self) -> None:
        body = "## Blocking findings\nNone.\n"
        result = parse_scoped_rereview_classification(
            body, prior_ids=["aaa111", "bbb222"],
        )
        assert result.resolved_ids == frozenset()
        assert result.unresolved_ids == frozenset({"aaa111", "bbb222"})

    def test_conflicting_id_is_never_resolved(self) -> None:
        # An id listed under both `resolved:` and `unresolved:` must not be
        # trusted as resolved -- the more cautious reading wins.
        body = "resolved: aaa111\nunresolved: aaa111\n"
        result = parse_scoped_rereview_classification(body, prior_ids=["aaa111"])
        assert "aaa111" not in result.resolved_ids
        assert "aaa111" in result.unresolved_ids

    def test_new_blocking_in_delta_bullets(self) -> None:
        body = (
            "## New blocking findings (in delta)\n"
            "- the fix itself introduces a null deref\n"
            "## New blocking findings (out of delta)\nNone.\n"
        )
        result = parse_scoped_rereview_classification(body)
        assert result.new_blocking_in_delta == (
            "the fix itself introduces a null deref",
        )
        assert result.new_blocking_out_of_delta == ()

    def test_new_blocking_out_of_delta_bullets(self) -> None:
        body = (
            "## New blocking findings (in delta)\nNone.\n"
            "## New blocking findings (out of delta)\n"
            "- an unrelated file leaks a secret\n"
        )
        result = parse_scoped_rereview_classification(body)
        assert result.new_blocking_in_delta == ()
        assert result.new_blocking_out_of_delta == (
            "an unrelated file leaks a secret",
        )


# ── out_of_delta_full_review_pending (the merge gate) ───────────────────────

def _q(aid: str, **kwargs) -> QueuedMerge:
    return QueuedMerge(
        assignment_id=aid,
        repo_name=kwargs.pop("repo", "api"),
        repo_github=kwargs.pop("repo_github", "acme/api"),
        branch=kwargs.pop("branch", f"worker/{aid}"),
        target_branch=kwargs.pop("target", "main"),
        issue_number=kwargs.pop("issue_number", 1),
        issue_title="t",
        **kwargs,
    )


class TestOutOfDeltaFullReviewPending:
    def _chain(self, out_of_delta_text: str | None, full_review_approve_at: float | None):
        work = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="a", type="work", status="done", branch="worker/a",
        )
        scoped = Assignment(
            machine_name="m2", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="rev-scoped", type="review", status="done",
            review_of_assignment_id="a", review_verdict="approve",
            review_scoped=True, dispatched_at=100.0,
        )
        assignments = [work, scoped]
        findings_by_id = {
            "rev-scoped": _findings(
                "## Blocking findings\nNone.\n\n"
                "## New blocking findings (out of delta)\n"
                + (f"- {out_of_delta_text}\n" if out_of_delta_text else "None.\n")
            ),
        }
        if full_review_approve_at is not None:
            full = Assignment(
                machine_name="m3", repo_name="api", issue_number=1, issue_title="t",
                assignment_id="rev-full", type="review", status="done",
                review_of_assignment_id="a", review_verdict="approve",
                review_scoped=False, dispatched_at=full_review_approve_at,
            )
            assignments.append(full)
            findings_by_id["rev-full"] = _findings("## Blocking findings\nNone.\n")
        board = Board(active=[], completed=assignments)
        return board, findings_by_id

    def test_no_scoped_rounds_means_no_gate(self) -> None:
        board = Board(active=[], completed=[])
        entry = _q("a")
        assert out_of_delta_full_review_pending(entry, board) is False

    def test_board_none_means_no_gate(self) -> None:
        entry = _q("a")
        assert out_of_delta_full_review_pending(entry, None) is False

    def test_scoped_round_with_no_out_of_delta_finding_does_not_gate(self) -> None:
        board, findings_by_id = self._chain(None, None)
        entry = _q("a")
        with patch(
            "coord.review.fetch_review_findings_from_github_many",
            side_effect=lambda repo, issue, ids: {
                i: findings_by_id[i] for i in ids if i in findings_by_id
            },
        ):
            assert out_of_delta_full_review_pending(entry, board) is False

    def test_out_of_delta_finding_with_no_full_review_yet_gates(self) -> None:
        board, findings_by_id = self._chain("an unrelated secret leak", None)
        entry = _q("a")
        with patch(
            "coord.review.fetch_review_findings_from_github_many",
            side_effect=lambda repo, issue, ids: {
                i: findings_by_id[i] for i in ids if i in findings_by_id
            },
        ):
            assert out_of_delta_full_review_pending(entry, board) is True

    def test_out_of_delta_finding_cleared_by_later_full_review(self) -> None:
        board, findings_by_id = self._chain("an unrelated secret leak", 200.0)
        entry = _q("a")
        with patch(
            "coord.review.fetch_review_findings_from_github_many",
            side_effect=lambda repo, issue, ids: {
                i: findings_by_id[i] for i in ids if i in findings_by_id
            },
        ):
            assert out_of_delta_full_review_pending(entry, board) is False

    def test_full_review_before_the_flag_does_not_clear_it(self) -> None:
        # A full review dispatched BEFORE the scoped round raised its flag
        # cannot have looked at the concern it raised.
        board, findings_by_id = self._chain("an unrelated secret leak", 50.0)
        entry = _q("a")
        with patch(
            "coord.review.fetch_review_findings_from_github_many",
            side_effect=lambda repo, issue, ids: {
                i: findings_by_id[i] for i in ids if i in findings_by_id
            },
        ):
            assert out_of_delta_full_review_pending(entry, board) is True


def _findings(body: str):
    from coord.review import ReviewFindings

    return ReviewFindings(verdict="approve", body=body)
