"""Tests for coord.briefing_lessons — epic/milestone lesson carry-forward (#3676)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from coord import issue_store, state
from coord.briefing_lessons import (
    find_undispatched_siblings,
    record_sibling_lessons,
    render_sibling_lessons,
    sibling_lessons_block,
    split_findings,
)
from coord.config import Config
from coord.dispatch import dispatch
from coord.models import Machine, Proposal, Repo

REVIEW_BODY = """\
## Blocking findings
- No targeted driver test: the new `scroll_pane` behaviour is only covered by
  unit tests, CLAUDE.md requires a black-box driver test.
- Code comments cite issue numbers (`// #1343: ...`) — this repo forbids them.

## Non-blocking concerns
- The helper could be shared with the minimap.

## Nits
None.
"""


@pytest.fixture
def config() -> Config:
    return Config(
        repos=[Repo(name="quadraui", github="acme/quadraui")],
        machines=[Machine(name="laptop", host="laptop.tailnet", repos=["quadraui"],
                          repo_paths={"quadraui": "/src/quadraui"})],
    )


def _issue(number: int, *, milestone: int | None = 7, body: str = "", st: str = "open") -> None:
    state.upsert_issue("quadraui", {
        "number": number, "title": f"Issue {number}", "body": body, "state": st,
        "labels": [], "milestone_number": milestone,
        "milestone_title": f"ms-{milestone}" if milestone else None,
    })


def _record(aid: str, issue: int, typ: str) -> None:
    state.record_dispatched(
        assignment_id=aid,
        proposal=Proposal(id=0, machine_name="laptop", repo_name="quadraui",
                          issue_number=issue, issue_title=f"Issue {issue}",
                          rationale="t", briefing="b", type=typ),
        repo_github="acme/quadraui",
        provider_name="claude",
    )


def _request_changes(aid: str, issue: int, body: str = REVIEW_BODY) -> None:
    """A real request-changes verdict through the review-verdict seam."""
    _record(aid, issue, "review")
    with patch("coord.github_ops.post_issue_comment"):
        issue_store.post_result(issue_store.ResultRecord(
            assignment_id=aid, machine_name="laptop", repo_name="quadraui",
            repo_github="acme/quadraui", issue_number=issue, status="done",
            verdict="request-changes", summary="changes needed", findings_body=body,
        ))


@pytest.fixture
def epic() -> None:
    """Port-style epic: #1340 tracks #1343/#1344/#1346 (all milestone 7) and
    #1350 (no milestone — a sibling only through the epic checklist).
    #1347 is in the milestone but already dispatched; #1360 is in another
    milestone; #1348 is in the milestone but closed."""
    _issue(1340, body="## Sub-issues\n- [ ] #1343\n- [ ] #1344\n- [ ] #1346\n- [ ] #1350\n")
    for n in (1343, 1344, 1346, 1347):
        _issue(n)
    _issue(1348, st="closed")
    _issue(1350, milestone=None)
    _issue(1360, milestone=8)
    _record("work-1343", 1343, "work")
    _record("work-1347", 1347, "work")


class TestSplitFindings:
    def test_top_level_bullets_with_continuations(self) -> None:
        from coord.review import extract_blocking_section

        findings = split_findings(extract_blocking_section(REVIEW_BODY))
        assert len(findings) == 2
        assert findings[0].startswith("No targeted driver test")
        assert "black-box driver test" in findings[0]  # continuation folded in
        assert findings[1].startswith("Code comments cite issue numbers")

    def test_prose_without_bullets_is_one_finding(self) -> None:
        assert split_findings("The whole thing is wrong.") == ["The whole thing is wrong."]

    def test_empty(self) -> None:
        assert split_findings("") == []


class TestSiblings:
    def test_undispatched_open_siblings_via_milestone_and_epic(self, epic) -> None:
        # 1340 is the tracker (never a sibling), 1343 itself is excluded,
        # 1347 has a work leg, 1348 is closed, 1360 is another milestone.
        assert find_undispatched_siblings("quadraui", 1343) == [1344, 1346, 1350]

    def test_no_milestone_no_epic_means_no_siblings(self) -> None:
        _issue(5, milestone=None)
        _issue(6, milestone=None)
        assert find_undispatched_siblings("quadraui", 5) == []


class TestRecordSiblingLessons:
    def test_request_changes_review_carries_findings_to_siblings(self, epic) -> None:
        _request_changes("rev-1343", 1343)

        for sibling in (1344, 1346, 1350):
            block = sibling_lessons_block("quadraui", sibling)
            assert "## Lessons from siblings" in block
            assert "(from #1343) No targeted driver test" in block
            assert "(from #1343) Code comments cite issue numbers" in block
            # Non-blocking concerns are not lessons.
            assert "shared with the minimap" not in block
        for not_a_target in (1340, 1343, 1347, 1348, 1360):
            assert sibling_lessons_block("quadraui", not_a_target) == ""

    def test_dedup_across_reviews_and_siblings(self, epic) -> None:
        _request_changes("rev-1343", 1343)
        # The same review re-recorded, and a second sibling tripping the
        # identical rule (different case/markdown), add nothing new.
        added = record_sibling_lessons(
            "quadraui", 1343,
            "- No targeted driver test: the new `scroll_pane` behaviour is only covered by\n"
            "  unit tests, CLAUDE.md requires a black-box driver test.\n",
        )
        assert added == {}
        _record("work-1344", 1344, "work")  # 1344 now in flight
        added = record_sibling_lessons(
            "quadraui", 1344,
            "- **code comments cite issue numbers (`// #1343: ...`) — this repo forbids them**\n"
            "- A brand-new finding.\n",
        )
        assert added == {1346: 1, 1350: 1}
        lessons = [e for e in state.list_issue_context("quadraui", 1346)
                   if e["source"] == state.SIBLING_LESSON_SOURCE]
        assert len(lessons) == 3

    def test_lessons_stay_out_of_the_per_issue_digest(self, epic) -> None:
        _request_changes("rev-1343", 1343)
        assert state.issue_context_block("quadraui", 1344) == ""

    def test_render_dedupes_and_caps(self) -> None:
        entries = [
            {"source": state.SIBLING_LESSON_SOURCE, "body": "(from #1) Same thing", "created_at": 1},
            {"source": state.SIBLING_LESSON_SOURCE, "body": "(from #2) same  THING.", "created_at": 2},
            {"source": "review", "body": "not a lesson", "created_at": 3},
        ]
        out = render_sibling_lessons(entries)
        assert out.count("\n- ") == 1
        assert "not a lesson" not in out
        assert render_sibling_lessons([]) == ""


class TestDispatchBriefing:
    @patch("coord.dispatch.httpx.post")
    def test_sibling_dispatched_after_request_changes_gets_the_findings(
        self, mock_post: MagicMock, epic, config: Config,
    ) -> None:
        """#3676 acceptance: a sibling dispatched after a request-changes
        review on another child gets that review's blocking findings in its
        briefing, under "Lessons from siblings"."""
        _request_changes("rev-1343", 1343)
        mock_post.return_value = MagicMock(json=MagicMock(return_value={"ok": True}))

        dispatch(Proposal(id=1, machine_name="laptop", repo_name="quadraui",
                          issue_number=1344, issue_title="Port the minimap",
                          rationale="r", files_likely=["src/minimap.rs"],
                          briefing="Port the minimap"), config)

        briefing = mock_post.call_args.kwargs["json"]["briefing"]
        assert "## Lessons from siblings" in briefing
        assert "(from #1343) No targeted driver test" in briefing
        assert "(from #1343) Code comments cite issue numbers" in briefing
        assert briefing.index("Port the minimap") < briefing.index("## Lessons from siblings")

    @patch("coord.dispatch.httpx.post")
    def test_no_lessons_section_without_a_sibling_review(
        self, mock_post: MagicMock, epic, config: Config,
    ) -> None:
        mock_post.return_value = MagicMock(json=MagicMock(return_value={"ok": True}))
        dispatch(Proposal(id=1, machine_name="laptop", repo_name="quadraui",
                          issue_number=1344, issue_title="Port the minimap",
                          rationale="r", briefing="Port the minimap"), config)
        assert "Lessons from siblings" not in mock_post.call_args.kwargs["json"]["briefing"]
