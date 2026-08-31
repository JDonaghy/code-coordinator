"""Tests for coord.parentage / coord.parentage_github (#1195).

Covers:
- MarkdownParentage.children()/parent()/add_child()/remove_child() (pure —
  no gh calls)
- GitHubParentage.children()/parent()/add_child()/remove_child() (gh mocked
  at the coord.github_ops._gh boundary)
- build_parentage_store() backend dispatch
- The github_ops sub-issues wrappers themselves, including the number -> id
  resolution gotcha called out in #1195's filing notes
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from coord import github_ops
from coord.parentage import (
    Child,
    MarkdownParentage,
    ParentRef,
    build_parentage_store,
)
from coord.parentage_github import GitHubParentage


# ── MarkdownParentage ────────────────────────────────────────────────────────

_EPIC_BODY = """\
Tracking issue for the milestone.

## Sub-issues
- [ ] #101  {group: A}
- [x] #102  {group: A}
- [ ] #103  {after: #101}

## Notes
Not part of the sub-issues block.
"""


class TestMarkdownParentageChildren:
    def test_parses_checklist_into_children(self) -> None:
        store = MarkdownParentage()
        kids = store.children("acme/api", 500, body=_EPIC_BODY)
        by_num = {c.number: c for c in kids}
        assert set(by_num) == {101, 102, 103}
        assert by_num[101] == Child(number=101, state="open")
        assert by_num[102] == Child(number=102, state="closed")
        assert by_num[103] == Child(number=103, state="open")

    def test_no_sub_issues_heading_returns_empty(self) -> None:
        store = MarkdownParentage()
        assert store.children("acme/api", 500, body="just some text") == []

    def test_empty_body_returns_empty(self) -> None:
        store = MarkdownParentage()
        assert store.children("acme/api", 500) == []


_WORK_ORDER_ONLY_BODY = """\
Tracking issue for the milestone — predates the #1008 `## Sub-issues`
convention, only ever got a `## Work order` block.

## Work order
- [ ] #101  {group: A}
- [x] #102  {group: A}
- [ ] #103  {after: #101}
"""


class TestMarkdownParentageChildrenWorkOrderFallback:
    """#1197 fix-iteration: an epic seeded only with `## Work order` (never
    additionally spliced with `coord milestone add-child`'s `## Sub-issues`
    checklist) must still report its children when the caller opts in via
    fallback_to_work_order=True — this is what made epic #1200 render with
    no nested children on the live board despite the nesting logic itself
    being correct."""

    def test_falls_back_to_work_order_when_opted_in(self) -> None:
        store = MarkdownParentage()
        kids = store.children(
            "acme/api", 500, body=_WORK_ORDER_ONLY_BODY, fallback_to_work_order=True,
        )
        by_num = {c.number: c for c in kids}
        assert set(by_num) == {101, 102, 103}
        assert by_num[101] == Child(number=101, state="open")
        assert by_num[102] == Child(number=102, state="closed")
        assert by_num[103] == Child(number=103, state="open")

    def test_no_fallback_by_default(self) -> None:
        """Default behavior (used by the #1196 close-guard) is unchanged:
        an epic with only `## Work order` and no `## Sub-issues` still
        reports zero children unless the caller explicitly opts in."""
        store = MarkdownParentage()
        assert store.children("acme/api", 500, body=_WORK_ORDER_ONLY_BODY) == []

    def test_sub_issues_block_wins_over_work_order_even_with_fallback(self) -> None:
        """When a `## Sub-issues` block IS present, it's used as-is — the
        work-order fallback only kicks in when sub-issues parsing finds
        nothing."""
        store = MarkdownParentage()
        kids = store.children(
            "acme/api", 500, body=_EPIC_BODY, fallback_to_work_order=True,
        )
        assert {c.number for c in kids} == {101, 102, 103}

    def test_self_reference_excluded(self) -> None:
        """Defensive: a tracking issue must never nest under itself even if
        its own number somehow appears in the parsed checklist."""
        store = MarkdownParentage()
        body = "## Work order\n- [ ] #500  {group: A}\n- [ ] #101  {group: A}\n"
        kids = store.children("acme/api", 500, body=body, fallback_to_work_order=True)
        assert {c.number for c in kids} == {101}


class TestMarkdownParentageParent:
    def test_finds_owning_epic(self) -> None:
        store = MarkdownParentage()
        epics = [
            {"number": 500, "state": "open", "body": _EPIC_BODY},
            {"number": 600, "state": "open", "body": "## Sub-issues\n- [ ] #999\n"},
        ]
        assert store.parent("acme/api", 101, epics=epics) == ParentRef(number=500, state="open")

    def test_returns_none_when_no_candidate_matches(self) -> None:
        store = MarkdownParentage()
        epics = [{"number": 500, "state": "open", "body": _EPIC_BODY}]
        assert store.parent("acme/api", 12345, epics=epics) is None

    def test_returns_none_without_epics(self) -> None:
        store = MarkdownParentage()
        assert store.parent("acme/api", 101) is None

    def test_skips_malformed_epic_body(self) -> None:
        """A garbled `## Sub-issues` block in one epic must not crash the
        search — it's just not a match, and later candidates still get a
        fair look."""
        store = MarkdownParentage()
        epics = [
            {"number": 400, "state": "open", "body": "## Sub-issues\n- garbage line\n"},
            {"number": 500, "state": "open", "body": _EPIC_BODY},
        ]
        assert store.parent("acme/api", 101, epics=epics) == ParentRef(number=500, state="open")


class TestMarkdownParentageWrites:
    def test_add_child_raises_not_implemented(self) -> None:
        store = MarkdownParentage()
        with pytest.raises(NotImplementedError):
            store.add_child("acme/api", 500, 101)

    def test_remove_child_raises_not_implemented(self) -> None:
        store = MarkdownParentage()
        with pytest.raises(NotImplementedError):
            store.remove_child("acme/api", 500, 101)


# ── build_parentage_store ────────────────────────────────────────────────────

class TestBuildParentageStore:
    def test_github(self) -> None:
        store = build_parentage_store("github")
        assert isinstance(store, GitHubParentage)

    def test_markdown(self) -> None:
        store = build_parentage_store("markdown")
        assert isinstance(store, MarkdownParentage)

    def test_unknown_falls_back_to_markdown(self) -> None:
        store = build_parentage_store("gitlab-but-not-built-yet")
        assert isinstance(store, MarkdownParentage)


# ── GitHubParentage (gh mocked) ──────────────────────────────────────────────

class TestGitHubParentageChildren:
    def test_maps_sub_issues_to_children(self) -> None:
        store = GitHubParentage()
        payload = json.dumps([
            {"number": 1039, "state": "closed"},
            {"number": 1040, "state": "open"},
        ])
        with patch("coord.github_ops._gh", return_value=payload):
            kids = store.children("acme/api", 1041)
        assert kids == [
            Child(number=1039, state="closed"),
            Child(number=1040, state="open"),
        ]

    def test_empty_list_is_not_an_error(self) -> None:
        """#1195 filing note: GET .../sub_issues returns [] (not 404/410)
        for an issue with no sub-issues yet — this is normal, not a failure."""
        store = GitHubParentage()
        with patch("coord.github_ops._gh", return_value="[]"):
            assert store.children("acme/api", 1041) == []

    def test_skips_malformed_entries(self) -> None:
        store = GitHubParentage()
        payload = json.dumps([{"state": "open"}, "not a dict", {"number": 5, "state": "open"}])
        with patch("coord.github_ops._gh", return_value=payload):
            kids = store.children("acme/api", 1041)
        assert kids == [Child(number=5, state="open")]


class TestGitHubParentageParent:
    def test_returns_parent_ref(self) -> None:
        store = GitHubParentage()
        with patch("coord.github_ops._gh", return_value=json.dumps({"number": 500, "state": "open"})):
            assert store.parent("acme/api", 101) == ParentRef(number=500, state="open")

    def test_null_parent_returns_none(self) -> None:
        store = GitHubParentage()
        with patch("coord.github_ops._gh", return_value="null"):
            assert store.parent("acme/api", 500) is None


class TestGitHubParentageWrites:
    def test_add_child_resolves_number_to_id_then_posts(self) -> None:
        """#1195's core gotcha: the write path needs the child's database
        `id`, not its issue `number` — verify both `gh` calls happen, in
        order, with the resolved id in the POST payload."""
        store = GitHubParentage()
        calls: list[tuple] = []

        def fake_gh(*args, **_kwargs):
            calls.append(args)
            if "--jq" in args:
                return "4856912446"
            return ""

        with patch("coord.github_ops._gh", side_effect=fake_gh):
            store.add_child("acme/api", 500, 1039)

        assert len(calls) == 2
        resolve_call, write_call = calls
        assert resolve_call[:2] == ("api", "repos/acme/api/issues/1039")
        assert write_call[0] == "api"
        assert write_call[1] == "repos/acme/api/issues/500/sub_issues"
        assert "-X" in write_call and "POST" in write_call
        assert "sub_issue_id=4856912446" in write_call

    def test_remove_child_uses_singular_endpoint(self) -> None:
        store = GitHubParentage()
        calls: list[tuple] = []

        def fake_gh(*args, **_kwargs):
            calls.append(args)
            if "--jq" in args:
                return "4856912446"
            return ""

        with patch("coord.github_ops._gh", side_effect=fake_gh):
            store.remove_child("acme/api", 500, 1039)

        write_call = calls[-1]
        assert write_call[1] == "repos/acme/api/issues/500/sub_issue"
        assert "-X" in write_call and "DELETE" in write_call
        assert "sub_issue_id=4856912446" in write_call


# ── github_ops sub-issues wrappers ───────────────────────────────────────────

class TestGithubOpsSubIssues:
    def test_get_sub_issues_parses_json(self) -> None:
        payload = json.dumps([{"number": 1, "state": "open"}])
        with patch("coord.github_ops._gh", return_value=payload):
            assert github_ops.get_sub_issues("acme/api", 1041) == [{"number": 1, "state": "open"}]

    def test_get_issue_parent_none_on_null(self) -> None:
        with patch("coord.github_ops._gh", return_value="null"):
            assert github_ops.get_issue_parent("acme/api", 1041) is None

    def test_get_issue_parent_none_on_empty(self) -> None:
        with patch("coord.github_ops._gh", return_value=""):
            assert github_ops.get_issue_parent("acme/api", 1041) is None

    def test_get_issue_parent_returns_dict(self) -> None:
        with patch("coord.github_ops._gh", return_value=json.dumps({"number": 500, "state": "open"})):
            assert github_ops.get_issue_parent("acme/api", 1041) == {"number": 500, "state": "open"}

    def test_resolve_issue_id(self) -> None:
        with patch("coord.github_ops._gh", return_value="4856912446"):
            assert github_ops._resolve_issue_id("acme/api", 1039) == 4856912446
