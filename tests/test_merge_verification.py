"""Black-box tests for the #2109 merge-time verification note.

Drives the REAL merge path (`coord.merge_queue.process()`) against a seeded
board, through the REAL `coord.state.upsert_issue_comment` ->
`coord.github_ops` seam (stubbed only at the `gh`-call boundary), and asserts
on the actually-rendered comment body — not just a unit test on the
formatter (that lives in `tests/test_comments.py`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from coord.comments import EVENT_VERIFICATION, parse_marker
from coord.merge_queue import process
from coord.models import Assignment, Board
from tests.test_merge_queue import FakeGh, _q


@dataclass
class _VerificationGh(FakeGh):
    """`FakeGh` + the diff/issue surface `_build_verification_comment` reads.

    All three extra lookups are OPTIONAL on `GhOps` (accessed via
    `getattr`/try-except in `coord.merge_queue`), so this subclass only
    needs to supply them, never required to widen the shared `FakeGh`.
    """

    compare_files: list[str] = field(default_factory=list)
    issue_bodies: dict[int, str] = field(default_factory=dict)
    branch_shas: dict[str, str] = field(default_factory=dict)

    def get_compare_files(self, repo: str, base: str, head: str) -> list[str] | None:
        return self.compare_files

    def get_branch_sha(self, repo: str, branch: str) -> str | None:
        return self.branch_shas.get(branch)

    def get_issue(self, repo: str, issue_number: int) -> dict:
        return {"number": issue_number, "body": self.issue_bodies.get(issue_number, "")}


def _board_with_work_test_review(
    *,
    work_aid: str = "a",
    issue: int = 553,
    test_verdict: str = "passed",
    test_machine: str = "precision",
    review_verdict: str = "approve",
    review_machine: str = "dellserver",
) -> Board:
    work = Assignment(
        machine_name="worker1", repo_name="api", issue_number=issue, issue_title="t",
        assignment_id=work_aid, type="work", status="done", branch=f"worker/{work_aid}",
        test_state=test_verdict,
    )
    smoke = Assignment(
        machine_name=test_machine, repo_name="api", issue_number=issue, issue_title="t",
        assignment_id=f"{work_aid}-smoke", type="smoke", status="done",
        review_of_assignment_id=work_aid, dispatched_at=100.0,
    )
    review = Assignment(
        machine_name=review_machine, repo_name="api", issue_number=issue, issue_title="t",
        assignment_id=f"{work_aid}-rev", type="review", status="done",
        review_of_assignment_id=work_aid, review_verdict=review_verdict, dispatched_at=200.0,
    )
    return Board(active=[], completed=[work, smoke, review])


class TestVerificationCommentOnMerge:
    def test_posts_a_fully_populated_verification_comment(self) -> None:
        board = _board_with_work_test_review()
        gh = _VerificationGh(
            compare_files=["src/auth.py", "tests/test_auth.py", "README.md"],
            issue_bodies={553: (
                "## Repro\n\n1. Log in twice\n2. Watch the token expire early\n\n"
                "## Acceptance\n\nTokens last the full TTL."
            )},
            branch_shas={"main": "cafef00d"},
        )
        items = [_q("a", issue_number=553)]

        posted: dict = {}

        def _fake_upsert(repo_name, issue_number, body, *, repo_github=None):
            posted["repo_name"] = repo_name
            posted["issue_number"] = issue_number
            posted["body"] = body

        from unittest.mock import patch

        with patch("coord.merge_queue.upsert_issue_comment", side_effect=_fake_upsert):
            events = process(items, gh, board=board)

        assert any(e.kind == "verification_note_posted" for e in events)
        assert posted["issue_number"] == 553
        body = posted["body"]

        marker = parse_marker(body)
        assert marker is not None
        assert marker.event == EVENT_VERIFICATION
        assert marker.fields["issue"] == "553"

        assert "`cafef00d`" in body
        assert "`tests/test_auth.py`" in body
        assert "README.md" not in body  # not a test file — not listed
        assert "**Test:** passed" in body and "`precision`" in body
        assert "**Review:** approve" in body and "`dellserver`" in body
        assert "Log in twice" in body  # quoted straight from the issue's own Repro section
        assert "not worker-authored" in body

    def test_no_verification_note_for_non_closing_assignment_types(self) -> None:
        # mock-author/test-author entries have no single "did this fix the
        # bug" question for a verification note to answer — same scope the
        # close/comment decision already uses (CLOSES_ISSUE_TYPES).
        from unittest.mock import patch

        board = _board_with_work_test_review()
        items = [_q("a", issue_number=553, assignment_type="mock-author")]
        gh = _VerificationGh()

        with patch("coord.merge_queue.upsert_issue_comment") as fake_upsert:
            events = process(items, gh, board=board)

        assert not any(e.kind.startswith("verification_note") for e in events)
        fake_upsert.assert_not_called()

    def test_verification_failure_is_reported_but_never_undoes_the_merge(self) -> None:
        from unittest.mock import patch

        board = _board_with_work_test_review()
        items = [_q("a", issue_number=553)]
        gh = _VerificationGh()

        with patch(
            "coord.merge_queue.upsert_issue_comment",
            side_effect=RuntimeError("gh comment failed"),
        ):
            events = process(items, gh, board=board)

        assert items[0].state == "merged"
        assert any(e.kind == "verification_note_failed" for e in events)
        assert any(e.kind == "merged" for e in events)


class TestVerificationCommentUpsertEndToEnd:
    """Drives `coord.state.upsert_issue_comment` for real (no board_service
    configured in the test env, so the local path runs) — stubbed only at
    `coord.github_ops`'s own `gh`-call boundary, with an in-memory fake
    comment store standing in for GitHub itself."""

    def test_second_merge_on_the_same_issue_updates_in_place(self) -> None:
        from unittest.mock import patch

        from coord import github_ops

        store: dict[int, dict] = {}
        next_id = {"n": 1000}

        def _fake_post(repo, issue_number, body):
            cid = next_id["n"]
            next_id["n"] += 1
            store[cid] = {"body": body}
            return f"https://github.com/{repo}/issues/{issue_number}#issuecomment-{cid}"

        def _fake_get_comments(repo, issue_number):
            return [
                {"url": f"https://github.com/{repo}/issues/{issue_number}#issuecomment-{cid}",
                 "body": c["body"]}
                for cid, c in store.items()
            ]

        def _fake_update(repo, issue_number, comment_id, body):
            store[comment_id]["body"] = body

        with patch.object(github_ops, "post_issue_comment", side_effect=_fake_post), \
             patch.object(github_ops, "get_issue_comments", side_effect=_fake_get_comments), \
             patch.object(github_ops, "update_issue_comment", side_effect=_fake_update):
            board1 = _board_with_work_test_review(work_aid="a", test_verdict="passed")
            gh1 = _VerificationGh(branch_shas={"main": "sha-one"})
            events1 = process([_q("a", issue_number=553)], gh1, board=board1)
            assert any(e.kind == "verification_note_posted" for e in events1)
            assert len(store) == 1
            first_body = next(iter(store.values()))["body"]
            assert "`sha-one`" in first_body

            # A retry (fix-1) lands a SECOND branch/entry for the same issue.
            board2 = _board_with_work_test_review(
                work_aid="b", test_verdict="passed", review_verdict="approve",
            )
            gh2 = _VerificationGh(branch_shas={"main": "sha-two"})
            events2 = process([_q("b", issue_number=553)], gh2, board=board2)
            assert any(e.kind == "verification_note_posted" for e in events2)

        # Still exactly one comment, with the REFRESHED content.
        assert len(store) == 1
        second_body = next(iter(store.values()))["body"]
        assert "`sha-two`" in second_body
        assert "`sha-one`" not in second_body
