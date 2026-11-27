"""Tests for #4187: stable ids on scoped re-review blocking findings, the
structured resolved/unresolved/new_blocking_in_delta response, and the
out-of-delta merge gate. Also #4194: auto-dispatching the full review that
gate requires.
"""

from __future__ import annotations

from unittest.mock import patch

from coord.merge_queue import (
    PENDING,
    QueuedMerge,
    dispatch_full_review_for_out_of_delta_gate,
    out_of_delta_full_review_pending,
    process,
)
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
    def _briefing(self) -> str:
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
        briefing = self._briefing()
        fid = finding_id("the worktree leaks a tempdir on failure")
        assert f"finding#{fid}" in briefing

    def test_instructs_finding_resolution_section(self) -> None:
        briefing = self._briefing()
        assert "## Finding resolution" in briefing
        assert "resolved:" in briefing
        assert "unresolved:" in briefing

    def test_instructs_in_delta_and_out_of_delta_headings(self) -> None:
        briefing = self._briefing()
        assert "## New blocking findings (in delta)" in briefing
        # #4187 review: the out-of-delta heading's own text must NOT
        # contain "blocking" — see `_bucket_for_heading`'s "blocking"
        # substring match, which every existing reader (
        # `extract_blocking_section`, `classify_blocking_evidence`, ...)
        # goes through. "New non-blocking findings" buckets correctly.
        assert "## New non-blocking findings (out of delta)" in briefing
        assert "## New blocking findings (out of delta)" not in briefing
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
            "## New non-blocking findings (out of delta)\nNone.\n"
        )
        result = parse_scoped_rereview_classification(body)
        assert result.new_blocking_in_delta == (
            "the fix itself introduces a null deref",
        )
        assert result.new_blocking_out_of_delta == ()

    def test_new_blocking_out_of_delta_bullets(self) -> None:
        body = (
            "## New blocking findings (in delta)\nNone.\n"
            "## New non-blocking findings (out of delta)\n"
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
                "## New non-blocking findings (out of delta)\n"
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

    def test_fetch_failure_fails_closed_when_a_scoped_round_exists(self) -> None:
        # #4187 review: a `gh` outage/rate-limit must not read the same as
        # "confirmed nothing was flagged" -- there's a scoped round in this
        # chain whose out-of-delta state simply couldn't be checked.
        board, _findings_by_id = self._chain(None, None)
        entry = _q("a")
        with patch(
            "coord.review.fetch_review_findings_from_github_many",
            side_effect=RuntimeError("gh: rate limited"),
        ):
            assert out_of_delta_full_review_pending(entry, board) is True

    def test_fetch_failure_with_no_scoped_round_still_no_gate(self) -> None:
        # The fail-closed branch above only applies once there's something
        # to be worried about -- a chain with no scoped round at all never
        # even attempts the fetch, so an unrelated GitHub outage can't hold
        # it hostage.
        board = Board(active=[], completed=[])
        entry = _q("a")
        with patch(
            "coord.review.fetch_review_findings_from_github_many",
            side_effect=RuntimeError("gh: rate limited"),
        ):
            assert out_of_delta_full_review_pending(entry, board) is False

    def test_scoped_approval_with_unresolved_prior_finding_gates(self) -> None:
        # #4187 review: `resolved_ids`/`unresolved_ids` must actually be
        # consulted somewhere real. A scoped round that `approve`s without
        # ever classifying the prior round's blocking finding fails closed
        # to "unresolved" (`parse_scoped_rereview_classification`) -- an
        # approval that contradicts its own structured response is treated
        # the same as an out-of-delta flag: one full review required.
        work = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="a", type="work", status="done", branch="worker/a",
        )
        prior_full = Assignment(
            machine_name="m2", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="rev-1", type="review", status="done",
            review_of_assignment_id="a", review_verdict="request-changes",
            review_scoped=False, dispatched_at=50.0,
        )
        scoped = Assignment(
            machine_name="m3", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="rev-scoped", type="review", status="done",
            review_of_assignment_id="a", review_verdict="approve",
            review_scoped=True, dispatched_at=100.0,
        )
        board = Board(active=[], completed=[work, prior_full, scoped])
        findings_by_id = {
            "rev-1": _findings(
                "## Blocking findings\n- the worktree leaks a tempdir\n"
            ),
            # Approves, but never classifies the prior finding at all (no
            # `## Finding resolution` section) -- fails closed to
            # unresolved.
            "rev-scoped": _findings(
                "## Blocking findings\nNone.\n\n"
                "## New non-blocking findings (out of delta)\nNone.\n"
            ),
        }
        entry = _q("a")
        with patch(
            "coord.review.fetch_review_findings_from_github_many",
            side_effect=lambda repo, issue, ids: {
                i: findings_by_id[i] for i in ids if i in findings_by_id
            },
        ):
            assert out_of_delta_full_review_pending(entry, board) is True

    def test_scoped_approval_with_explicitly_resolved_prior_finding_no_gate(
        self,
    ) -> None:
        # Same shape as above, but the scoped round DOES classify the
        # prior finding as resolved -- no contradiction, no gate.
        fid = finding_id("the worktree leaks a tempdir")
        work = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="a", type="work", status="done", branch="worker/a",
        )
        prior_full = Assignment(
            machine_name="m2", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="rev-1", type="review", status="done",
            review_of_assignment_id="a", review_verdict="request-changes",
            review_scoped=False, dispatched_at=50.0,
        )
        scoped = Assignment(
            machine_name="m3", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="rev-scoped", type="review", status="done",
            review_of_assignment_id="a", review_verdict="approve",
            review_scoped=True, dispatched_at=100.0,
        )
        board = Board(active=[], completed=[work, prior_full, scoped])
        findings_by_id = {
            "rev-1": _findings(
                "## Blocking findings\n- the worktree leaks a tempdir\n"
            ),
            "rev-scoped": _findings(
                "## Blocking findings\nNone.\n\n"
                "## Finding resolution\n"
                f"resolved: finding#{fid}\n"
                "unresolved: (none)\n\n"
                "## New non-blocking findings (out of delta)\nNone.\n"
            ),
        }
        entry = _q("a")
        with patch(
            "coord.review.fetch_review_findings_from_github_many",
            side_effect=lambda repo, issue, ids: {
                i: findings_by_id[i] for i in ids if i in findings_by_id
            },
        ):
            assert out_of_delta_full_review_pending(entry, board) is False


# ── process(): the two real call sites of the out-of-delta gate ────────────

class TestProcessOutOfDeltaGate:
    """#4187 review: the pure-function tests above exercise the gate's
    logic, but nothing asserted it is actually WIRED into `process()` at
    either of its two call sites (the live merge-gate check and the
    `dry_run` preview). Mirrors `tests.test_merge_queue`'s own
    `test_process_emits_review_required_event_and_halts_merge` /
    `test_process_fails_closed_when_board_none_and_review_required` shape
    for the sibling review gate.
    """

    @staticmethod
    def _config():
        from dataclasses import dataclass as _dc, field as _dc_field

        @_dc
        class _Reviews:
            enabled: bool = True

        @_dc
        class _Pipeline:
            default_gates: list[str] | None = None

        @_dc
        class _Cfg:
            reviews: _Reviews = _dc_field(default_factory=_Reviews)
            pipeline: _Pipeline = _dc_field(default_factory=_Pipeline)

        cfg = _Cfg()
        cfg.pipeline.default_gates = ["review", "merge"]
        return cfg

    @staticmethod
    def _flagged_board_and_findings():
        work = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="w1", type="work", status="done", branch="worker/w1",
        )
        scoped = Assignment(
            machine_name="m2", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="rev-scoped", type="review", status="done",
            review_of_assignment_id="w1", review_verdict="approve",
            review_scoped=True, dispatched_at=100.0,
        )
        board = Board(active=[], completed=[work, scoped])
        findings_by_id = {
            "rev-scoped": _findings(
                "## Blocking findings\nNone.\n\n"
                "## New non-blocking findings (out of delta)\n"
                "- an unrelated file leaks a secret\n"
            ),
        }
        return board, findings_by_id

    def test_process_emits_full_review_required_event_and_halts_merge(self) -> None:
        from tests.test_merge_queue import FakeGh

        cfg = self._config()
        board, findings_by_id = self._flagged_board_and_findings()
        items = [_q("w1", size=10)]
        gh = FakeGh()
        with patch(
            "coord.review.fetch_review_findings_from_github_many",
            side_effect=lambda repo, issue, ids: {
                i: findings_by_id[i] for i in ids if i in findings_by_id
            },
        ):
            events = process(items, gh, config=cfg, board=board)

        kinds = [e.kind for e in events]
        assert "full_review_required" in kinds
        assert "merged" not in kinds
        assert gh.merge_calls == []
        assert items[0].state == PENDING
        assert "one full review" in (items[0].error or "")

    def test_process_wires_the_auto_dispatch_into_the_live_gate(self) -> None:
        """#4194: `process()`'s live merge-gate check must actually CALL the
        auto-dispatch helper for the entry/board/config it just evaluated
        the gate against — not just report the gate's own verdict."""
        from tests.test_merge_queue import FakeGh

        cfg = self._config()
        board, findings_by_id = self._flagged_board_and_findings()
        items = [_q("w1", size=10)]
        gh = FakeGh()
        with patch(
            "coord.review.fetch_review_findings_from_github_many",
            side_effect=lambda repo, issue, ids: {
                i: findings_by_id[i] for i in ids if i in findings_by_id
            },
        ), patch(
            "coord.merge_queue.dispatch_full_review_for_out_of_delta_gate",
            return_value=None,
        ) as mock_dispatch:
            process(items, gh, config=cfg, board=board)

        mock_dispatch.assert_called_once_with(items[0], board, cfg)

    def test_process_dry_run_reports_full_review_required(self) -> None:
        from tests.test_merge_queue import FakeGh

        cfg = self._config()
        board, findings_by_id = self._flagged_board_and_findings()
        items = [_q("w1", size=10)]
        gh = FakeGh()
        with patch(
            "coord.review.fetch_review_findings_from_github_many",
            side_effect=lambda repo, issue, ids: {
                i: findings_by_id[i] for i in ids if i in findings_by_id
            },
        ):
            events = process(items, gh, config=cfg, board=board, dry_run=True)

        kinds = [e.kind for e in events]
        assert "full_review_required" in kinds
        assert "merged" not in kinds

    def test_process_skip_review_bypasses_the_gate(self) -> None:
        from tests.test_merge_queue import FakeGh

        cfg = self._config()
        board, findings_by_id = self._flagged_board_and_findings()
        items = [_q("w1", size=10)]
        gh = FakeGh()
        with patch(
            "coord.review.fetch_review_findings_from_github_many",
            side_effect=lambda repo, issue, ids: {
                i: findings_by_id[i] for i in ids if i in findings_by_id
            },
        ):
            events = process(items, gh, config=cfg, board=board, skip_review=True)

        kinds = [e.kind for e in events]
        assert "full_review_required" not in kinds
        assert "merged" in kinds

    def test_process_merges_once_a_full_review_approves_after_the_flag(self) -> None:
        """#4194 acceptance: once the auto-dispatched full review APPROVES
        on a round dispatched after the scoped round's flag, the gate
        clears and the very next `process()` tick merges — no dispatch is
        attempted at all, since the gate is no longer pending."""
        from tests.test_merge_queue import FakeGh

        cfg = self._config()
        work = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="w1", type="work", status="done", branch="worker/w1",
        )
        scoped = Assignment(
            machine_name="m2", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="rev-scoped", type="review", status="done",
            review_of_assignment_id="w1", review_verdict="approve",
            review_scoped=True, dispatched_at=100.0,
        )
        full = Assignment(
            machine_name="m3", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="rev-full", type="review", status="done",
            review_of_assignment_id="w1", review_verdict="approve",
            review_scoped=False, dispatched_at=200.0,
        )
        board = Board(active=[], completed=[work, scoped, full])
        findings_by_id = {
            "rev-scoped": _findings(
                "## Blocking findings\nNone.\n\n"
                "## New non-blocking findings (out of delta)\n"
                "- an unrelated file leaks a secret\n"
            ),
            "rev-full": _findings("## Blocking findings\nNone.\n"),
        }
        items = [_q("w1", size=10)]
        gh = FakeGh()
        with patch(
            "coord.review.fetch_review_findings_from_github_many",
            side_effect=lambda repo, issue, ids: {
                i: findings_by_id[i] for i in ids if i in findings_by_id
            },
        ), patch(
            "coord.merge_queue.dispatch_full_review_for_out_of_delta_gate",
        ) as mock_dispatch:
            events = process(items, gh, config=cfg, board=board)

        kinds = [e.kind for e in events]
        assert "full_review_required" not in kinds
        assert "merged" in kinds
        mock_dispatch.assert_not_called()


def _findings(body: str):
    from coord.review import ReviewFindings

    return ReviewFindings(verdict="approve", body=body)


# ── dispatch_full_review_for_out_of_delta_gate (#4194) ──────────────────────

class TestAutoDispatchFullReviewForOutOfDeltaGate:
    """The out-of-delta gate (`_out_of_delta_review_gate`) only ever reports
    a *verdict*; nothing dispatched the full review it requires until this.
    These exercise `dispatch_full_review_for_out_of_delta_gate`'s real
    wiring through `coord.review.dispatch_review` (a mocked HTTP agent, not
    a stand-in for the function itself), covering the issue's three
    acceptance shapes: exactly one dispatch for a flagged leg, no second
    dispatch on a later tick while that review is still in flight, and a
    fresh dispatch once a new work leg (a push) appears.

    Every board here stamps a real `review_head_sha` on the scoped review
    AND injects a `branch_sha_fetcher` that resolves the SAME sha for the
    entry's branch (`HEAD_SHA`/`_dispatch_kwargs` below) — without both of
    those, `dispatch_review`'s #4002 `head_already_approved` check never
    resolves a head at all (`if not branch or not branch_head_sha: return
    False`), so these tests would pass even if the
    `require_full_review_for_out_of_delta_gate` override never reached that
    check. Resolving a real, matching head is what proves the override
    actually bypasses it (and, separately, that a FULL review already
    covering that head still blocks a redundant redispatch — see
    `test_does_not_redispatch_once_a_full_review_already_covers_the_head`).
    """

    HEAD_SHA = "sha-w1"

    @staticmethod
    def _config():
        from coord.config import Config, ReviewsConfig
        from coord.models import Machine, Repo

        repo = Repo(name="api", github="acme/api", depends_on=[], default_branch="main")
        return Config(
            repos=[repo],
            machines=[
                Machine(
                    name="laptop", host="laptop.tail",
                    capabilities=["python"], repos=["api"],
                    repo_paths={"api": "/work/api"},
                ),
            ],
            reviews=ReviewsConfig(enabled=True, auto_dispatch=True),
        )

    @classmethod
    def _dispatch_kwargs(cls, client, *, branch: str = "worker/w1", head_sha: str | None = None):
        head_sha = cls.HEAD_SHA if head_sha is None else head_sha
        return dict(
            http_client=client,
            pr_lookup=lambda repo_github, **kw: {
                "number": 42, "url": "https://github.com/acme/api/pull/42",
                "existed": True,
            },
            claude_md_reader=lambda p: None,
            issue_body_fetcher=lambda repo, num: "",
            remote_branch_checker=lambda repo, branch: True,
            # Resolves the SAME head sha `_flagged_board` stamped on the
            # scoped review's `review_head_sha` — the production shape
            # `head_already_approved`'s SHA-match gate needs to actually
            # be reachable (see class docstring).
            branch_sha_fetcher=lambda repo_github, br: head_sha if br == branch else None,
        )

    @classmethod
    def _flagged_board(
        cls, work_id: str = "w1", scoped_id: str = "rev-scoped",
        head_sha: str | None = None,
    ) -> Board:
        # The WORK leg already carries `review_verdict="approve"` — exactly
        # what `propagate_review_verdict` writes onto it once the scoped
        # round below lands — so a test proving the override actually
        # bypasses `dispatch_review`'s ordinary "one review per leg" guard
        # starts from the state that guard would otherwise refuse.
        head_sha = cls.HEAD_SHA if head_sha is None else head_sha
        work = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1, issue_title="t",
            assignment_id=work_id, type="work", status="done",
            branch=f"worker/{work_id}", review_verdict="approve",
        )
        scoped = Assignment(
            machine_name="reviewer", repo_name="api", issue_number=1, issue_title="t",
            assignment_id=scoped_id, type="review", status="done",
            review_of_assignment_id=work_id, review_verdict="approve",
            review_scoped=True, dispatched_at=100.0, review_head_sha=head_sha,
        )
        return Board(active=[], completed=[work, scoped])

    def test_dispatch_fires_once_for_the_flagged_leg(self) -> None:
        from tests.test_review import _FakeHTTPClient

        cfg = self._config()
        board = self._flagged_board()
        client = _FakeHTTPClient({"id": "review-id-1"})
        entry = _q("w1")

        # The scoped review above already carries an `approve` whose
        # `review_head_sha` matches the SAME head `_dispatch_kwargs`
        # resolves for this branch — exactly the state #4002's
        # `head_already_approved` would otherwise refuse a fresh dispatch
        # against (`claim_review_item_dispatch_gate`'s `check_already_
        # approved`). This only passes because `dispatch_review`'s
        # `require_full_review_for_out_of_delta_gate` override actually
        # disables that check for this caller.
        result = dispatch_full_review_for_out_of_delta_gate(
            entry, board, cfg, **self._dispatch_kwargs(client),
        )

        assert result is not None
        assert result.review_scoped is False
        assert result.review_of_assignment_id == "w1"
        assert len(client.calls) == 1
        assert result in board.active

    def test_does_not_redispatch_once_a_full_review_already_covers_the_head(
        self,
    ) -> None:
        # A FULL (non-scoped) review already approved this exact head --
        # the out-of-delta gate would already read as clear
        # (`_out_of_delta_review_gate`) -- so a later, independent call to
        # the auto-dispatch helper (e.g. a stale/duplicate merge-queue
        # tick) must not fire a second, redundant metered review for it.
        from tests.test_review import _FakeHTTPClient

        cfg = self._config()
        board = self._flagged_board()
        full = Assignment(
            machine_name="reviewer2", repo_name="api", issue_number=1,
            issue_title="t", assignment_id="rev-full", type="review",
            status="done", review_of_assignment_id="w1",
            review_verdict="approve", review_scoped=False,
            dispatched_at=200.0, review_head_sha=self.HEAD_SHA,
        )
        board.completed.append(full)
        client = _FakeHTTPClient({"id": "review-id-1"})
        entry = _q("w1")

        result = dispatch_full_review_for_out_of_delta_gate(
            entry, board, cfg, **self._dispatch_kwargs(client),
        )

        assert result is None
        assert client.calls == []

    def test_second_tick_does_not_dispatch_again_while_in_flight(self) -> None:
        from tests.test_review import _FakeHTTPClient

        cfg = self._config()
        board = self._flagged_board()
        client = _FakeHTTPClient({"id": "review-id-1"})
        entry = _q("w1")

        first = dispatch_full_review_for_out_of_delta_gate(
            entry, board, cfg, **self._dispatch_kwargs(client),
        )
        assert first is not None
        assert len(client.calls) == 1

        second = dispatch_full_review_for_out_of_delta_gate(
            entry, board, cfg, **self._dispatch_kwargs(client),
        )
        assert second is None
        assert len(client.calls) == 1  # no second POST on the second tick

    def test_a_new_push_re_arms_the_dispatch_for_the_new_head(self) -> None:
        from coord.state import release_review_item_lease
        from tests.test_review import _FakeHTTPClient

        cfg = self._config()
        board = self._flagged_board()
        client = _FakeHTTPClient({"id": "review-id-1"})
        entry = _q("w1")

        first = dispatch_full_review_for_out_of_delta_gate(
            entry, board, cfg, **self._dispatch_kwargs(client),
        )
        assert first is not None
        assert len(client.calls) == 1

        # The first dispatch concludes (e.g. a `request-changes` verdict
        # that sends the issue through the normal fix-round path) — the
        # real terminal-status write path
        # (`coord.state.release_review_claim_if_row_is_review`) releases
        # BOTH the per-assignment claim and the #4002 per-(repo, issue)
        # lease `dispatch_review` also holds for as long as a review leg is
        # live. A fix round then lands a NEW work leg — a different
        # `assignment_id` on the SAME branch/issue, which is what a push
        # re-dispatch actually looks like — and the merge entry re-keys to
        # it.
        release_review_item_lease("api", 1)
        first.status = "done"
        first.review_verdict = "request-changes"
        work2 = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="w2", type="work", status="done", branch="worker/w1",
            review_of_assignment_id="w1",
        )
        board.completed.append(work2)
        board.active.remove(first)
        board.completed.append(first)
        entry2 = _q("w2")

        second = dispatch_full_review_for_out_of_delta_gate(
            entry2, board, cfg, **self._dispatch_kwargs(client),
        )
        assert second is not None
        assert second.review_of_assignment_id == "w2"
        assert len(client.calls) == 2

    def test_no_work_leg_resolves_to_no_dispatch(self) -> None:
        board = Board(active=[], completed=[])
        entry = _q("missing")
        assert dispatch_full_review_for_out_of_delta_gate(entry, board, self._config()) is None

    def test_board_none_means_no_dispatch(self) -> None:
        entry = _q("w1")
        assert dispatch_full_review_for_out_of_delta_gate(entry, None, self._config()) is None
