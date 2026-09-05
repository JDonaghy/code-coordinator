"""Tests for coord/github_ops.py terminal-state helpers (#522).

``issue_is_closed`` and ``pr_is_merged`` are the GitHub-state guards the
auto-loop consults before dispatching a fix/re-review.  Both are best-effort
and **fail-open** — any ``gh`` error must resolve to ``False`` so a transient
failure never blocks a legitimate dispatch.
"""

from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from coord import github_ops
from coord.forge_availability import _flush_all_ok_aggregates

# Captured at import time — the real function object, immune to the conftest
# autouse `_non_terminal_work` stub which reassigns the module attribute.
_REAL_WORK_IS_TERMINAL = github_ops.work_is_terminal


class TestIssueIsClosed:
    def test_true_when_state_closed(self) -> None:
        with patch(
            "coord.github_ops._gh",
            return_value=json.dumps({"number": 1, "state": "CLOSED"}),
        ):
            assert github_ops.issue_is_closed("acme/api", 1) is True

    def test_false_when_state_open(self) -> None:
        with patch(
            "coord.github_ops._gh",
            return_value=json.dumps({"number": 1, "state": "OPEN"}),
        ):
            assert github_ops.issue_is_closed("acme/api", 1) is False

    def test_fail_open_on_gh_error(self) -> None:
        with patch("coord.github_ops._gh", side_effect=RuntimeError("gh boom")):
            assert github_ops.issue_is_closed("acme/api", 1) is False

    def test_fail_open_on_malformed_json(self) -> None:
        with patch("coord.github_ops._gh", return_value="not json"):
            assert github_ops.issue_is_closed("acme/api", 1) is False


class TestCheckPrMergeable:
    """#1477: check_pr_mergeable() re-tests GitHub's own mergeability
    computation, used to clear a merge-queue entry's stale CONFLICT verdict."""

    def test_true_when_mergeable(self) -> None:
        with patch(
            "coord.github_ops._gh",
            return_value=json.dumps({"mergeable": "MERGEABLE"}),
        ):
            assert github_ops.check_pr_mergeable("acme/api", 1) is True

    def test_false_when_conflicting(self) -> None:
        with patch(
            "coord.github_ops._gh",
            return_value=json.dumps({"mergeable": "CONFLICTING"}),
        ):
            assert github_ops.check_pr_mergeable("acme/api", 1) is False

    def test_none_when_unknown(self) -> None:
        """GitHub computes mergeability asynchronously — a very recent push
        can read back UNKNOWN for a few seconds. Must not be treated as a
        green light."""
        with patch(
            "coord.github_ops._gh",
            return_value=json.dumps({"mergeable": "UNKNOWN"}),
        ):
            assert github_ops.check_pr_mergeable("acme/api", 1) is None

    def test_none_on_gh_error(self) -> None:
        with patch("coord.github_ops._gh", side_effect=RuntimeError("gh boom")):
            assert github_ops.check_pr_mergeable("acme/api", 1) is None

    def test_none_on_malformed_json(self) -> None:
        with patch("coord.github_ops._gh", return_value="not json"):
            assert github_ops.check_pr_mergeable("acme/api", 1) is None


class TestBranchHasMergeCommit:
    """#1467: branch_has_merge_commit() detects the one failure mode
    check_pr_mergeable can't — a branch that's cleanly MERGEABLE but still
    refused by `gh pr merge --rebase` because it contains a merge commit."""

    def test_true_when_a_commit_has_two_parents(self) -> None:
        commits = [
            {"sha": "a1", "parents": [{"sha": "p1"}]},
            {"sha": "a2", "parents": [{"sha": "p1"}, {"sha": "p2"}]},
        ]
        with patch("coord.github_ops._gh", return_value=json.dumps(commits)):
            assert github_ops.branch_has_merge_commit("acme/api", 1) is True

    def test_false_when_every_commit_has_one_parent(self) -> None:
        commits = [
            {"sha": "a1", "parents": [{"sha": "p1"}]},
            {"sha": "a2", "parents": [{"sha": "p2"}]},
        ]
        with patch("coord.github_ops._gh", return_value=json.dumps(commits)):
            assert github_ops.branch_has_merge_commit("acme/api", 1) is False

    def test_false_for_a_root_commit_with_no_parents(self) -> None:
        commits = [{"sha": "a1", "parents": []}]
        with patch("coord.github_ops._gh", return_value=json.dumps(commits)):
            assert github_ops.branch_has_merge_commit("acme/api", 1) is False

    def test_none_on_gh_error(self) -> None:
        with patch("coord.github_ops._gh", side_effect=RuntimeError("gh boom")):
            assert github_ops.branch_has_merge_commit("acme/api", 1) is None

    def test_none_on_malformed_json(self) -> None:
        with patch("coord.github_ops._gh", return_value="not json"):
            assert github_ops.branch_has_merge_commit("acme/api", 1) is None

    def test_none_when_response_is_not_a_list(self) -> None:
        # e.g. `gh api` returning an error object instead of the commits array.
        with patch(
            "coord.github_ops._gh",
            return_value=json.dumps({"message": "Not Found"}),
        ):
            assert github_ops.branch_has_merge_commit("acme/api", 1) is None


def _gh_pr_and_branch(pr_list_json: str, branch_json: str | None):
    """Build a ``_gh`` ``side_effect`` that answers ``pr list`` with
    *pr_list_json* and ``api .../branches/<name>`` with *branch_json* (or
    raises ``RuntimeError`` when *branch_json* is ``None``, simulating a
    deleted/unresolvable branch — see :func:`coord.github_ops.get_branch_sha`).
    """

    def _dispatch(*args, **kwargs):
        if args and args[0] == "pr":
            return pr_list_json
        if branch_json is None:
            raise RuntimeError("gh api branches: 404 not found")
        return branch_json

    return _dispatch


class TestPrIsMerged:
    """#1150: a historical merge on a *reused* branch name must not be
    confused with "this branch's current commits are merged" —
    ``--fix-of``/``--rework-of``/``--force`` all legitimately continue on an
    existing branch. ``pr_is_merged`` now requires the branch's *current* tip
    (via ``get_branch_sha``) to match the merged PR's ``headRefOid``.
    """

    def test_true_when_current_tip_matches_merged_pr(self) -> None:
        """The exact commit now on the branch is what merged -> True."""
        pr_payload = json.dumps([
            {"number": 42, "state": "MERGED", "mergedAt": "2026-06-09T00:00:00Z",
             "headRefOid": "deadbeef"},
        ])
        branch_payload = json.dumps({"commit": {"sha": "deadbeef"}})
        with patch("coord.github_ops._gh", side_effect=_gh_pr_and_branch(pr_payload, branch_payload)):
            assert github_ops.pr_is_merged("acme/api", "issue-1-fix") is True

    def test_false_when_new_commits_pushed_after_historical_merge(self) -> None:
        """#1150 core case: branch reused (--fix-of/--force) after a prior
        merge, with new commits on top -> the current tip is NOT the SHA that
        merged, so this must NOT be reported as merged."""
        pr_payload = json.dumps([
            {"number": 42, "state": "MERGED", "mergedAt": "2026-06-09T00:00:00Z",
             "headRefOid": "oldsha1"},
        ])
        branch_payload = json.dumps({"commit": {"sha": "newsha2"}})
        with patch("coord.github_ops._gh", side_effect=_gh_pr_and_branch(pr_payload, branch_payload)):
            assert github_ops.pr_is_merged("acme/api", "issue-1-fix") is False

    def test_true_when_branch_deleted_after_merge(self) -> None:
        """Tip unresolvable AND the branch is positively confirmed gone (404,
        the common case: branch deleted post-merge) -> falls back to the
        pre-#1150 'any historical merge counts' behavior, since a deleted
        branch cannot have gained new commits since. The shared
        ``_gh_pr_and_branch`` stub raises a 404-shaped RuntimeError for any
        non-``pr`` call, which satisfies both the ``get_branch_sha`` lookup
        and the follow-up ``branch_exists_on_remote`` confirmation check."""
        pr_payload = json.dumps([
            {"number": 42, "state": "MERGED", "mergedAt": "2026-06-09T00:00:00Z",
             "headRefOid": "oldsha1"},
        ])
        with patch("coord.github_ops._gh", side_effect=_gh_pr_and_branch(pr_payload, None)):
            assert github_ops.pr_is_merged("acme/api", "issue-1-fix") is True

    def test_false_when_sha_lookup_fails_for_non_404_reason(self) -> None:
        """#1150 review: a transient gh/network failure that leaves the SHA
        unresolved must NOT be treated the same as 'branch confirmed gone'.
        Conflating the two reintroduces this issue's exact bug class under a
        transient-failure trigger -- a rate limit or auth blip at the wrong
        moment would read as 'already merged', and callers (reconcile's merge
        sweep, prune_stale_queue_entries) act on a single True reading by
        permanently marking live, unmerged work as done/deleting its queue
        entry. Only a positively-confirmed-absent branch (404) may fall back
        to trusting history; every other failure must fail open toward
        False, per this module's documented convention."""
        pr_payload = json.dumps([
            {"number": 42, "state": "MERGED", "mergedAt": "2026-06-09T00:00:00Z",
             "headRefOid": "oldsha1"},
        ])

        def _dispatch(*args, **kwargs):
            if args and args[0] == "pr":
                return pr_payload
            # Not a "not found" / 4xx signal -- a generic transient failure.
            raise RuntimeError("gh: connection timed out")

        with patch("coord.github_ops._gh", side_effect=_dispatch):
            assert github_ops.pr_is_merged("acme/api", "issue-1-fix") is False

    def test_true_when_merged_at_present(self) -> None:
        # Current tip matches the merged PR's headRefOid, so the #1150
        # commit-aware check passes on its own merits (not via the
        # unresolvable-SHA fallback).
        pr_payload = json.dumps([
            {"number": 42, "state": "MERGED", "mergedAt": "2026-06-09T00:00:00Z",
             "headRefOid": "deadbeef"},
        ])
        branch_payload = json.dumps({"commit": {"sha": "deadbeef"}})
        with patch("coord.github_ops._gh", side_effect=_gh_pr_and_branch(pr_payload, branch_payload)):
            assert github_ops.pr_is_merged("acme/api", "issue-1-fix") is True

    def test_true_when_state_merged_without_merged_at(self) -> None:
        pr_payload = json.dumps([
            {"number": 42, "state": "MERGED", "mergedAt": None,
             "headRefOid": "deadbeef"},
        ])
        branch_payload = json.dumps({"commit": {"sha": "deadbeef"}})
        with patch("coord.github_ops._gh", side_effect=_gh_pr_and_branch(pr_payload, branch_payload)):
            assert github_ops.pr_is_merged("acme/api", "issue-1-fix") is True

    def test_false_when_open(self) -> None:
        payload = json.dumps([{"number": 42, "state": "OPEN", "mergedAt": None}])
        with patch("coord.github_ops._gh", return_value=payload):
            assert github_ops.pr_is_merged("acme/api", "issue-1-fix") is False

    def test_false_when_no_pr_for_branch(self) -> None:
        with patch("coord.github_ops._gh", return_value="[]"):
            assert github_ops.pr_is_merged("acme/api", "issue-1-fix") is False

    def test_empty_branch_short_circuits_without_calling_gh(self) -> None:
        with patch(
            "coord.github_ops._gh",
            side_effect=AssertionError("gh must not be called for empty branch"),
        ):
            assert github_ops.pr_is_merged("acme/api", "") is False

    def test_fail_open_on_gh_error(self) -> None:
        with patch("coord.github_ops._gh", side_effect=RuntimeError("gh boom")):
            assert github_ops.pr_is_merged("acme/api", "issue-1-fix") is False

    def test_fail_open_on_malformed_json(self) -> None:
        with patch("coord.github_ops._gh", return_value="not json"):
            assert github_ops.pr_is_merged("acme/api", "issue-1-fix") is False


class TestGetIssue:
    """#1138 review: `enforce_oracle_readiness` derives the `oracle:exempt`
    escape hatch from `get_issue(...).get("labels")`. That silently always
    returned `[]` in production because `get_issue()`'s `--json` field list
    omitted `labels` — masked by tests that mocked `get_issue()` itself
    (handing back a `labels` key the real function never produced). These
    tests mock only `_gh` (the `gh` subprocess boundary) so the real
    `get_issue()` — field list included — is what's under test."""

    def test_json_field_list_requests_labels(self) -> None:
        with patch(
            "coord.github_ops._gh",
            return_value=json.dumps({
                "number": 1, "title": "t", "body": "b", "state": "OPEN",
                "milestone": None, "labels": [],
            }),
        ) as mock_gh:
            github_ops.get_issue("acme/api", 1)

        args = mock_gh.call_args.args
        assert args[0] == "issue" and args[1] == "view"
        json_fields = args[args.index("--json") + 1].split(",")
        assert "labels" in json_fields

    def test_returns_labels_from_real_gh_payload(self) -> None:
        with patch(
            "coord.github_ops._gh",
            return_value=json.dumps({
                "number": 1, "title": "t", "body": "b", "state": "OPEN",
                "milestone": {"number": 37},
                "labels": [{"name": "oracle:exempt"}, {"name": "coord"}],
            }),
        ):
            issue = github_ops.get_issue("acme/api", 1)

        label_names = [lbl.get("name", "") for lbl in issue.get("labels") or []]
        assert label_names == ["oracle:exempt", "coord"]


class TestWorkIsTerminal:
    """The #522 chokepoint guard: terminal == issue closed OR PR merged.

    Calls the captured real function (`_REAL_WORK_IS_TERMINAL`) so the conftest
    autouse non-terminal stub doesn't shadow it.  Patches the leaf helpers
    (`issue_is_closed` / `pr_is_merged`) which the real function looks up as
    module globals at call time.
    """

    def test_true_when_issue_closed(self) -> None:
        with patch("coord.github_ops.issue_is_closed", return_value=True), \
             patch("coord.github_ops.pr_is_merged", return_value=False):
            assert _REAL_WORK_IS_TERMINAL("acme/api", 1, "issue-1-fix") is True

    def test_true_when_pr_merged_even_if_issue_open(self) -> None:
        with patch("coord.github_ops.issue_is_closed", return_value=False), \
             patch("coord.github_ops.pr_is_merged", return_value=True):
            assert _REAL_WORK_IS_TERMINAL("acme/api", 1, "issue-1-fix") is True

    def test_false_when_neither(self) -> None:
        with patch("coord.github_ops.issue_is_closed", return_value=False), \
             patch("coord.github_ops.pr_is_merged", return_value=False):
            assert _REAL_WORK_IS_TERMINAL("acme/api", 1, "issue-1-fix") is False

    def test_false_for_empty_repo_without_calling_helpers(self) -> None:
        with patch(
            "coord.github_ops.issue_is_closed",
            side_effect=AssertionError("must not check state for empty repo"),
        ):
            assert _REAL_WORK_IS_TERMINAL("", 1, "issue-1-fix") is False

    def test_cache_collapses_repeat_calls(self) -> None:
        # The #349 ×4 case: a shared cache means the same merged issue costs
        # ONE issue_is_closed lookup across many revisits, not one per call.
        calls = {"n": 0}

        def counting_closed(*a, **k):
            calls["n"] += 1
            return True

        cache: dict = {}
        with patch("coord.github_ops.issue_is_closed", counting_closed):
            for _ in range(4):
                assert _REAL_WORK_IS_TERMINAL(
                    "acme/api", 349, "issue-349-fix", cache=cache
                ) is True

        assert calls["n"] == 1, "shared cache must collapse repeat gh lookups"

    def test_distinct_keys_not_collapsed(self) -> None:
        # Different (repo, issue, branch) keys must each be checked once.
        calls = {"n": 0}

        def counting_closed(*a, **k):
            calls["n"] += 1
            return False

        cache: dict = {}
        with patch("coord.github_ops.issue_is_closed", counting_closed), \
             patch("coord.github_ops.pr_is_merged", return_value=False):
            _REAL_WORK_IS_TERMINAL("acme/api", 1, "b1", cache=cache)
            _REAL_WORK_IS_TERMINAL("acme/api", 2, "b2", cache=cache)

        assert calls["n"] == 2

    # ── #2639: trust_issue_closed=False for tracking-issue rows ────────────
    #
    # A `test-author`/`mock-author` row's `issue_number` is always the
    # milestone's *tracking* issue, never something its own branch resolves.
    # A closed tracking epic must not report every slice booked against it
    # "terminal" regardless of whether that slice's own branch ever landed.

    def test_issue_closed_ignored_when_trust_issue_closed_is_false(self) -> None:
        with patch("coord.github_ops.issue_is_closed", return_value=True), \
             patch("coord.github_ops.pr_is_merged", return_value=False):
            assert _REAL_WORK_IS_TERMINAL(
                "acme/api", 16, "test-author-ms-1-slice-10",
                trust_issue_closed=False,
            ) is False

    def test_pr_merged_still_wins_when_trust_issue_closed_is_false(self) -> None:
        # The branch itself is the only thing that may decide for these rows —
        # and it still can, via pr_is_merged (branch/commit-scoped, #1150).
        with patch("coord.github_ops.issue_is_closed", return_value=True), \
             patch("coord.github_ops.pr_is_merged", return_value=True):
            assert _REAL_WORK_IS_TERMINAL(
                "acme/api", 16, "test-author-ms-1-slice-10",
                trust_issue_closed=False,
            ) is True

    def test_trust_issue_closed_defaults_true_preserving_522_flood_guard(
        self,
    ) -> None:
        # Every existing caller that doesn't pass the new kwarg — chiefly
        # type='work', where issue_number IS the row's own deliverable — must
        # keep today's behaviour: a manually-closed issue alone is terminal.
        with patch("coord.github_ops.issue_is_closed", return_value=True), \
             patch("coord.github_ops.pr_is_merged", return_value=False):
            assert _REAL_WORK_IS_TERMINAL("acme/api", 349, "issue-349-fix") is True

    def test_trust_issue_closed_is_part_of_the_cache_key(self) -> None:
        # A True and a False probe for the same (repo, issue, branch) must not
        # collapse onto each other's cached verdict.
        cache: dict = {}
        with patch("coord.github_ops.issue_is_closed", return_value=True), \
             patch("coord.github_ops.pr_is_merged", return_value=False):
            trusted = _REAL_WORK_IS_TERMINAL(
                "acme/api", 16, "b", cache=cache, trust_issue_closed=True
            )
            untrusted = _REAL_WORK_IS_TERMINAL(
                "acme/api", 16, "b", cache=cache, trust_issue_closed=False
            )
        assert trusted is True
        assert untrusted is False


# ── close-invariant chokepoint (#1196) ──────────────────────────────────────

_EPIC_WITH_OPEN_CHILD = json.dumps({
    "number": 1041,
    "title": "Epic",
    "state": "open",
    "milestone": None,
    "labels": [],
    "body": "## Sub-issues\n- [ ] #1039\n- [x] #1040\n",
})

_EPIC_ALL_CHILDREN_CLOSED = json.dumps({
    "number": 1041,
    "title": "Epic",
    "state": "open",
    "milestone": None,
    "labels": [],
    "body": "## Sub-issues\n- [x] #1039\n- [x] #1040\n",
})

_REGULAR_ISSUE = json.dumps({
    "number": 42,
    "title": "Fix auth",
    "state": "open",
    "milestone": None,
    "labels": [],
    "body": "Just a regular issue, no checklist.",
})


class TestGetOpenChildren:
    def test_returns_open_children_only(self) -> None:
        with patch("coord.github_ops._gh", return_value=_EPIC_WITH_OPEN_CHILD):
            children = github_ops.get_open_children("acme/api", 1041)
        assert children == [{"number": 1039, "state": "open"}]

    def test_empty_when_all_children_closed(self) -> None:
        with patch("coord.github_ops._gh", return_value=_EPIC_ALL_CHILDREN_CLOSED):
            assert github_ops.get_open_children("acme/api", 1041) == []

    def test_empty_for_regular_issue(self) -> None:
        with patch("coord.github_ops._gh", return_value=_REGULAR_ISSUE):
            assert github_ops.get_open_children("acme/api", 42) == []

    def test_fails_open_on_gh_error(self) -> None:
        # A transient lookup failure must not permanently wedge every close
        # in the system — close_issue is the enforcement point, not this.
        with patch("coord.github_ops._gh", side_effect=RuntimeError("gh boom")):
            assert github_ops.get_open_children("acme/api", 1041) == []

    def test_fails_open_on_malformed_checklist(self) -> None:
        # #1195's own precedent (per-epic parse-failure isolation on
        # /board): a malformed `## Sub-issues` block on *this* issue must
        # not wedge closing some *other* well-formed one.
        malformed = json.dumps({
            "number": 1041, "title": "Epic", "state": "open", "milestone": None,
            "labels": [],
            "body": "## Sub-issues\n- [ ] #1039\n- [ ] #1039\n",  # duplicate
        })
        with patch("coord.github_ops._gh", return_value=malformed):
            assert github_ops.get_open_children("acme/api", 1041) == []

    def test_falls_back_to_work_order_when_no_sub_issues(self) -> None:
        # #1221: epic #1200 (real data) predates the `## Sub-issues`
        # convention (#1008) and was seeded via `coord milestone write-order`
        # with only a `## Work order` block. The close-guard must detect
        # its children via the fallback.
        work_order_only = json.dumps({
            "number": 1041, "title": "Epic", "state": "open", "milestone": None,
            "labels": [],
            "body": "Tracking issue.\n\n## Work order\n- [ ] #1039\n- [x] #1040\n",
        })
        with patch("coord.github_ops._gh", return_value=work_order_only):
            children = github_ops.get_open_children("acme/api", 1041)
        assert children == [{"number": 1039, "state": "open"}]

    def test_fails_open_on_malformed_work_order(self) -> None:
        # #1222 sibling case: a malformed `## Work order` block (e.g.
        # with invalid syntax that throws during parse_work_order) must
        # not wedge the close-guard — it must fail open and return [].
        malformed_work_order = json.dumps({
            "number": 1041, "title": "Epic", "state": "open", "milestone": None,
            "labels": [],
            "body": "Tracking issue.\n\n## Work order\n" + ("- [ ] #invalidnumber\n" * 1000),
        })
        with patch("coord.github_ops._gh", return_value=malformed_work_order):
            # Even if parsing fails, must not raise — must return []
            children = github_ops.get_open_children("acme/api", 1041)
        assert children == []

    def test_has_open_children_true_false(self) -> None:
        with patch("coord.github_ops._gh", return_value=_EPIC_WITH_OPEN_CHILD):
            assert github_ops.has_open_children("acme/api", 1041) is True
        with patch("coord.github_ops._gh", return_value=_EPIC_ALL_CHILDREN_CLOSED):
            assert github_ops.has_open_children("acme/api", 1041) is False


def _fake_gh_dispatch(issue_body_json: str, graphql: str | Exception):
    """Route ``_gh`` calls by subcommand: ``issue view`` returns the parent's
    body, ``api graphql`` returns (or raises) the live-state batch lookup —
    the two `_gh` calls `get_open_children` now makes per invocation (#1354)."""

    def _fake(*args: str, **_kwargs) -> str:
        if args[:2] == ("issue", "view"):
            return issue_body_json
        if args[:2] == ("api", "graphql"):
            if isinstance(graphql, Exception):
                raise graphql
            return graphql
        raise AssertionError(f"unexpected gh args: {args}")

    return _fake


def _graphql_states(states: dict[int, str]) -> str:
    """Build a ``gh api graphql`` response matching
    :func:`coord.github_ops.get_issues_live_state`'s expected shape: one
    aliased ``issue(number: N)`` field per entry."""
    repository = {
        f"n{number}": {"number": number, "state": state.upper()}
        for number, state in states.items()
    }
    return json.dumps({"data": {"repository": repository}})


class TestGetOpenChildrenLiveState:
    """#1354: the checklist box is only a proxy for child state and drifts —
    the guard must trust a live lookup over the box in both directions."""

    def test_stale_unticked_box_but_child_closed_live_is_excluded(self) -> None:
        # Box says #1039 is still open; live GitHub says it's closed (the
        # box was simply never ticked). The live answer wins.
        with patch(
            "coord.github_ops._gh",
            side_effect=_fake_gh_dispatch(
                _EPIC_WITH_OPEN_CHILD,
                _graphql_states({1039: "closed", 1040: "closed"}),
            ),
        ):
            assert github_ops.get_open_children("acme/api", 1041) == []

    def test_ticked_box_over_genuinely_open_child_is_included(self) -> None:
        # False-negative direction (no coverage before #1354): the box says
        # #1039 is checked off, but GitHub says it's still open (reopened,
        # or the box was ticked in error). The live answer must win here
        # too — a ticked box must not let a close sail over a real open
        # child.
        with patch(
            "coord.github_ops._gh",
            side_effect=_fake_gh_dispatch(
                _EPIC_ALL_CHILDREN_CLOSED,
                _graphql_states({1039: "open", 1040: "closed"}),
            ),
        ):
            children = github_ops.get_open_children("acme/api", 1041)
        assert children == [{"number": 1039, "state": "open"}]

    def test_live_lookup_failure_falls_back_to_checkbox_state(self) -> None:
        # The batch GraphQL call itself fails (network/auth/rate-limit) —
        # this must not wedge the close; fall back to the checklist's own
        # signal, i.e. today's pre-#1354 behavior, rather than refusing
        # outright or silently allowing every close through.
        with patch(
            "coord.github_ops._gh",
            side_effect=_fake_gh_dispatch(
                _EPIC_WITH_OPEN_CHILD, RuntimeError("gh boom"),
            ),
        ):
            children = github_ops.get_open_children("acme/api", 1041)
        assert children == [{"number": 1039, "state": "open"}]

    def test_live_lookup_missing_a_number_falls_back_for_that_child_only(self) -> None:
        # GitHub doesn't resolve every aliased number (e.g. deleted issue) —
        # the response simply omits that field. Only the unresolved child
        # falls back to its checkbox state; a resolved sibling still uses
        # its live state.
        with patch(
            "coord.github_ops._gh",
            side_effect=_fake_gh_dispatch(
                _EPIC_WITH_OPEN_CHILD,  # #1039 open (box), #1040 closed (box)
                _graphql_states({1040: "open"}),  # #1039 missing; #1040 live-open
            ),
        ):
            children = github_ops.get_open_children("acme/api", 1041)
        assert {c["number"] for c in children} == {1039, 1040}


class _FakeCompletedProcess:
    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr


class TestCloseIssueGuard:
    # `close_issue`'s actual `gh issue close` call goes through raw
    # `subprocess.run`, not the `_gh()` helper (idempotency on "already
    # closed" needs the exit code + stderr, which `_gh()` doesn't expose) —
    # so the guard's `get_issue` lookup is mocked via `_gh`, and the close
    # call itself via `subprocess.run`.

    def test_refuses_close_with_open_children(self) -> None:
        with patch("coord.github_ops._gh", return_value=_EPIC_WITH_OPEN_CHILD), \
             patch(
                 "coord.github_ops.subprocess.run",
                 side_effect=AssertionError("must not attempt the close call"),
             ):
            with pytest.raises(github_ops.IssueHasOpenChildrenError, match=r"#1039"):
                github_ops.close_issue("acme/api", 1041)

    def test_closes_over_stale_unticked_boxes_when_children_are_live_closed(self) -> None:
        # #1354 repro (epics #929/#1034): every child is actually closed on
        # GitHub, but the checklist boxes were never ticked. The live
        # lookup must let this close through with no `--force`.
        with patch(
            "coord.github_ops._gh",
            side_effect=_fake_gh_dispatch(
                _EPIC_WITH_OPEN_CHILD,  # boxes: #1039 unticked, #1040 ticked
                _graphql_states({1039: "closed", 1040: "closed"}),
            ),
        ), patch(
            "coord.github_ops.subprocess.run", return_value=_FakeCompletedProcess(),
        ) as mock_run:
            github_ops.close_issue("acme/api", 1041)
        mock_run.assert_called_once()

    def test_refuses_close_over_ticked_box_when_child_is_live_open(self) -> None:
        # #1354's inverse defect: a ticked box over a genuinely open child
        # must not let the close sail through.
        with patch(
            "coord.github_ops._gh",
            side_effect=_fake_gh_dispatch(
                _EPIC_ALL_CHILDREN_CLOSED,  # boxes: both ticked
                _graphql_states({1039: "open", 1040: "closed"}),
            ),
        ), patch(
            "coord.github_ops.subprocess.run",
            side_effect=AssertionError("must not attempt the close call"),
        ):
            with pytest.raises(github_ops.IssueHasOpenChildrenError, match=r"#1039"):
                github_ops.close_issue("acme/api", 1041)

    def test_force_overrides_the_guard(self) -> None:
        with patch(
            "coord.github_ops._gh",
            side_effect=AssertionError("force=True must skip the open-children lookup"),
        ) as mock_gh, patch(
            "coord.github_ops.subprocess.run", return_value=_FakeCompletedProcess(),
        ) as mock_run:
            github_ops.close_issue("acme/api", 1041, force=True)
        mock_gh.assert_not_called()
        mock_run.assert_called_once()
        assert mock_run.call_args.args[0] == [
            "gh", "issue", "close", "1041", "--repo", "acme/api",
        ]

    def test_regular_issue_closes_unchanged(self) -> None:
        # No regression for the common case: an issue with no children
        # closes exactly as before, guard lookup included.
        with patch("coord.github_ops._gh", return_value=_REGULAR_ISSUE), \
             patch(
                 "coord.github_ops.subprocess.run",
                 return_value=_FakeCompletedProcess(),
             ) as mock_run:
            github_ops.close_issue("acme/api", 42)
        assert mock_run.call_args.args[0] == [
            "gh", "issue", "close", "42", "--repo", "acme/api",
        ]

    def test_still_idempotent_on_already_closed(self) -> None:
        with patch("coord.github_ops._gh", return_value=_REGULAR_ISSUE), \
             patch(
                 "coord.github_ops.subprocess.run",
                 return_value=_FakeCompletedProcess(1, "GraphQL: Issue already closed"),
             ):
            github_ops.close_issue("acme/api", 42)  # must not raise


class TestReopenIssue:
    # `reopen_issue` (#1078) is the complement of `close_issue`: no
    # open-children guard, but the same "idempotent on the gh error text
    # for the terminal-state-already-set case" contract.

    def test_regular_issue_reopens(self) -> None:
        with patch(
            "coord.github_ops.subprocess.run",
            return_value=_FakeCompletedProcess(),
        ) as mock_run:
            github_ops.reopen_issue("acme/api", 42)
        assert mock_run.call_args.args[0] == [
            "gh", "issue", "reopen", "42", "--repo", "acme/api",
        ]

    def test_still_idempotent_on_already_open(self) -> None:
        with patch(
            "coord.github_ops.subprocess.run",
            return_value=_FakeCompletedProcess(1, "GraphQL: Issue is already open"),
        ):
            github_ops.reopen_issue("acme/api", 42)  # must not raise

    def test_raises_on_other_gh_failure(self) -> None:
        with patch(
            "coord.github_ops.subprocess.run",
            return_value=_FakeCompletedProcess(1, "gh: issue not found"),
        ):
            with pytest.raises(RuntimeError, match="issue not found"):
                github_ops.reopen_issue("acme/api", 42)

    def test_posts_comment_before_reopening(self) -> None:
        calls: list = []
        with patch(
            "coord.github_ops.post_issue_comment",
            lambda repo, issue, comment: calls.append((repo, issue, comment)),
        ), patch(
            "coord.github_ops.subprocess.run",
            return_value=_FakeCompletedProcess(),
        ) as mock_run:
            github_ops.reopen_issue("acme/api", 42, comment="reopening, wrong call")
        assert calls == [("acme/api", 42, "reopening, wrong call")]
        mock_run.assert_called_once()

    def test_no_comment_posted_when_none(self) -> None:
        with patch(
            "coord.github_ops.post_issue_comment",
            side_effect=AssertionError("must not post a comment when none given"),
        ), patch(
            "coord.github_ops.subprocess.run",
            return_value=_FakeCompletedProcess(),
        ):
            github_ops.reopen_issue("acme/api", 42)


class TestPrBodyWrappers:
    def test_get_pr_body(self) -> None:
        with patch(
            "coord.github_ops._gh",
            return_value=json.dumps({"body": "Closes #99"}),
        ):
            assert github_ops.get_pr_body("acme/api", 5) == "Closes #99"

    def test_get_pr_body_missing_field(self) -> None:
        with patch("coord.github_ops._gh", return_value=json.dumps({})):
            assert github_ops.get_pr_body("acme/api", 5) == ""

    def test_edit_pr_body(self) -> None:
        calls: list[tuple] = []
        with patch("coord.github_ops._gh", lambda *a, **k: calls.append(a) or ""):
            github_ops.edit_pr_body("acme/api", 5, "Refs #99")
        assert calls == [("pr", "edit", "5", "--repo", "acme/api", "--body", "Refs #99")]


# ── Pre-merge epic-closing-keyword guard (#1318) ────────────────────────────

_EPIC_ISSUE = json.dumps({
    "number": 1120, "title": "Epic", "state": "open", "milestone": None,
    "labels": [{"name": "epic"}], "body": "",
})


class TestIsEpicIssue:
    def test_true_for_epic_labelled_issue(self) -> None:
        with patch("coord.github_ops._gh", return_value=_EPIC_ISSUE):
            assert github_ops.is_epic_issue("acme/api", 1120) is True

    def test_false_for_regular_issue(self) -> None:
        with patch("coord.github_ops._gh", return_value=_REGULAR_ISSUE):
            assert github_ops.is_epic_issue("acme/api", 42) is False

    def test_fails_open_on_gh_error(self) -> None:
        with patch("coord.github_ops._gh", side_effect=RuntimeError("gh boom")):
            assert github_ops.is_epic_issue("acme/api", 1120) is False


class TestGetPrCommitMessages:
    def test_joins_headline_and_body(self) -> None:
        raw = json.dumps({"commits": [
            {"messageHeadline": "fix(#55): a bug", "messageBody": "Closes #55"},
        ]})
        with patch("coord.github_ops._gh", return_value=raw):
            messages = github_ops.get_pr_commit_messages("acme/api", 5)
        assert messages == ["fix(#55): a bug\n\nCloses #55"]

    def test_headline_only_when_body_empty(self) -> None:
        raw = json.dumps({"commits": [
            {"messageHeadline": "fix(#55): a bug", "messageBody": ""},
        ]})
        with patch("coord.github_ops._gh", return_value=raw):
            messages = github_ops.get_pr_commit_messages("acme/api", 5)
        assert messages == ["fix(#55): a bug"]

    def test_multiple_commits_preserve_order(self) -> None:
        raw = json.dumps({"commits": [
            {"messageHeadline": "first", "messageBody": ""},
            {"messageHeadline": "second", "messageBody": "detail"},
        ]})
        with patch("coord.github_ops._gh", return_value=raw):
            messages = github_ops.get_pr_commit_messages("acme/api", 5)
        assert messages == ["first", "second\n\ndetail"]

    def test_missing_commits_field_returns_empty(self) -> None:
        with patch("coord.github_ops._gh", return_value=json.dumps({"body": "x"})):
            assert github_ops.get_pr_commit_messages("acme/api", 5) == []

    def test_fails_open_on_gh_error(self) -> None:
        with patch("coord.github_ops._gh", side_effect=RuntimeError("gh boom")):
            assert github_ops.get_pr_commit_messages("acme/api", 5) == []


# ── Fix A + B: change_issue_labels auto-create and typed errors ──────────────

_CURRENT_LABELS_JSON = json.dumps({"labels": [{"name": "existing"}]})


class TestIsLabelNotFound:
    """Unit-level coverage for the error-classification helper."""

    def test_graphql_label_error_is_label_not_found(self) -> None:
        exc = RuntimeError(
            "gh issue edit 5 --repo x/y failed: "
            "GraphQL: Could not resolve to a Label with the name 'foo'."
        )
        assert github_ops._is_label_not_found(exc) is True

    def test_label_not_found_phrase_matches(self) -> None:
        exc = RuntimeError(
            "gh issue edit 5 --repo x/y --add-label missing failed: "
            "label 'missing' not found in repository x/y"
        )
        assert github_ops._is_label_not_found(exc) is True

    def test_auth_error_is_not_label_not_found(self) -> None:
        exc = RuntimeError(
            "gh issue edit 5 --repo x/y failed: HTTP 401: Bad credentials"
        )
        assert github_ops._is_label_not_found(exc) is False

    def test_rate_limit_error_is_not_label_not_found(self) -> None:
        exc = RuntimeError(
            "gh issue edit 5 --repo x/y failed: HTTP 429: API rate limit exceeded"
        )
        assert github_ops._is_label_not_found(exc) is False

    def test_network_error_is_not_label_not_found(self) -> None:
        exc = RuntimeError(
            "gh issue edit 5 --repo x/y failed: "
            "dial tcp: lookup api.github.com: no such host"
        )
        assert github_ops._is_label_not_found(exc) is False

    def test_connection_refused_is_not_label_not_found(self) -> None:
        exc = RuntimeError(
            "gh issue edit 5 --repo x/y failed: connection refused"
        )
        assert github_ops._is_label_not_found(exc) is False

    def test_unrelated_not_found_without_label_keyword(self) -> None:
        # "not found" for an issue, not a label — no "label" keyword in
        # the error, so this must NOT trigger auto-creation.
        exc = RuntimeError(
            "gh issue edit 999 --repo x/y failed: "
            "GraphQL: Could not resolve to an Issue with the number 999."
        )
        assert github_ops._is_label_not_found(exc) is False


class TestChangeIssueLabelsAutoCreate:
    """Fix A: change_issue_labels auto-creates a missing add-label and retries."""

    def _make_gh(self, calls: list, *, first_edit_error: str | None = None) -> None:
        """Build a ``_gh`` side-effect that records all calls.

        The first ``issue edit`` call raises *first_edit_error* if given.
        Subsequent calls (``label create`` and the retry edit) succeed.
        ``issue view`` always returns a single-label set ``[{"name":"x"}]``.
        """
        edit_calls = {"n": 0}

        def _dispatch(*args: str, **_kwargs) -> str:
            calls.append(args)
            if args[0] == "issue" and args[1] == "view":
                return _CURRENT_LABELS_JSON
            if args[0] == "issue" and args[1] == "edit":
                edit_calls["n"] += 1
                if edit_calls["n"] == 1 and first_edit_error:
                    raise RuntimeError(first_edit_error)
            return ""

        return _dispatch

    def test_success_without_auto_create_when_no_error(self) -> None:
        """Happy path: edit succeeds → no label create call."""
        calls: list = []
        with patch("coord.github_ops._gh", side_effect=self._make_gh(calls)):
            new_labels, changed = github_ops.change_issue_labels(
                "acme/api", 7, add={"new"}, remove=set()
            )
        assert changed is True
        assert "new" in new_labels
        # No ``label create`` call should appear
        create_calls = [c for c in calls if c[0] == "label" and c[1] == "create"]
        assert create_calls == []

    def test_auto_creates_label_and_retries_on_not_found(self) -> None:
        """Fix A core: when ``gh issue edit --add-label`` fails with label-not-found,
        ``gh label create`` is called and the edit is retried."""
        calls: list = []
        label_not_found_error = (
            "gh issue edit 7 --repo acme/api --add-label new failed: "
            "GraphQL: Could not resolve to a Label with the name 'new'."
        )
        with patch(
            "coord.github_ops._gh",
            side_effect=self._make_gh(calls, first_edit_error=label_not_found_error),
        ):
            new_labels, changed = github_ops.change_issue_labels(
                "acme/api", 7, add={"new"}, remove=set()
            )

        assert changed is True
        assert "new" in new_labels
        # A ``label create new`` call must have been made.
        create_calls = [
            c for c in calls if c[0] == "label" and c[1] == "create" and "new" in c
        ]
        assert len(create_calls) == 1
        # The edit was called twice: the failing first attempt + the successful retry.
        edit_calls = [c for c in calls if c[0] == "issue" and c[1] == "edit"]
        assert len(edit_calls) == 2

    def test_raises_gh_not_found_when_retry_still_fails(self) -> None:
        """If the retry also reports label-not-found, raise GhNotFound (4xx signal)."""
        not_found_msg = (
            "gh issue edit 7 --repo acme/api --add-label phantom failed: "
            "GraphQL: Could not resolve to a Label with the name 'phantom'."
        )

        def _always_fail(*args: str, **_kwargs) -> str:
            if args[0] == "issue" and args[1] == "view":
                return _CURRENT_LABELS_JSON
            if args[0] == "issue" and args[1] == "edit":
                raise RuntimeError(not_found_msg)
            return ""  # label create succeeds silently

        with patch("coord.github_ops._gh", side_effect=_always_fail):
            with pytest.raises(github_ops.GhNotFound):
                github_ops.change_issue_labels(
                    "acme/api", 7, add={"phantom"}, remove=set()
                )

    def test_reraises_non_label_error_without_auto_create(self) -> None:
        """An auth/network/rate-limit failure must NOT trigger label auto-creation."""
        auth_error = (
            "gh issue edit 7 --repo acme/api failed: HTTP 401: Bad credentials"
        )
        create_called = {"v": False}

        def _dispatch(*args: str, **_kwargs) -> str:
            if args[0] == "issue" and args[1] == "view":
                return _CURRENT_LABELS_JSON
            if args[0] == "label" and args[1] == "create":
                create_called["v"] = True
                return ""
            if args[0] == "issue" and args[1] == "edit":
                raise RuntimeError(auth_error)
            return ""

        with patch("coord.github_ops._gh", side_effect=_dispatch):
            with pytest.raises(RuntimeError, match="401"):
                github_ops.change_issue_labels(
                    "acme/api", 7, add={"new"}, remove=set()
                )

        assert create_called["v"] is False, "auto-create must not fire on auth failure"

    def test_reraises_retry_error_that_is_not_label_not_found(self) -> None:
        """If the retry fails for a non-label reason (e.g. network), re-raise as
        plain RuntimeError — not GhNotFound."""
        label_not_found_msg = (
            "gh issue edit 7 --repo acme/api --add-label new failed: "
            "GraphQL: Could not resolve to a Label with the name 'new'."
        )
        network_error = "gh issue edit 7 --repo acme/api failed: connection refused"
        edit_calls = {"n": 0}

        def _dispatch(*args: str, **_kwargs) -> str:
            if args[0] == "issue" and args[1] == "view":
                return _CURRENT_LABELS_JSON
            if args[0] == "label" and args[1] == "create":
                return ""  # auto-create succeeds
            if args[0] == "issue" and args[1] == "edit":
                edit_calls["n"] += 1
                if edit_calls["n"] == 1:
                    raise RuntimeError(label_not_found_msg)
                raise RuntimeError(network_error)
            return ""

        with patch("coord.github_ops._gh", side_effect=_dispatch):
            with pytest.raises(RuntimeError) as exc_info:
                github_ops.change_issue_labels(
                    "acme/api", 7, add={"new"}, remove=set()
                )
        # Should be a plain RuntimeError, NOT a GhNotFound
        assert not isinstance(exc_info.value, github_ops.GhNotFound)
        assert "connection refused" in str(exc_info.value)

    def test_remove_path_is_unaffected_by_auto_create_logic(self) -> None:
        """Fix A scope: the auto-create path is only for adds; a remove-only
        change that fails must propagate the error unchanged."""
        remove_error = RuntimeError(
            "gh issue edit 7 --repo acme/api failed: some unexpected error"
        )
        create_called = {"v": False}

        def _dispatch(*args: str, **_kwargs) -> str:
            if args[0] == "issue" and args[1] == "view":
                return json.dumps({"labels": [{"name": "existing"}]})
            if args[0] == "label" and args[1] == "create":
                create_called["v"] = True
                return ""
            if args[0] == "issue" and args[1] == "edit":
                raise remove_error
            return ""

        with patch("coord.github_ops._gh", side_effect=_dispatch):
            with pytest.raises(RuntimeError, match="some unexpected error"):
                # to_add is empty — only a remove
                github_ops.change_issue_labels(
                    "acme/api", 7, add=set(), remove={"existing"}
                )
        assert create_called["v"] is False, "auto-create must not fire for remove-only changes"


# ── #873: durable issue_comments mirror — capture-at-write ──────────────────

class TestParseCommentId:
    def test_extracts_id_from_issue_comment_url(self) -> None:
        url = "https://github.com/acme/api/issues/42#issuecomment-4861387759"
        assert github_ops.parse_comment_id(url) == 4861387759

    def test_returns_none_for_blank(self) -> None:
        assert github_ops.parse_comment_id("") is None
        assert github_ops.parse_comment_id(None) is None

    def test_returns_none_for_unrecognized_format(self) -> None:
        assert github_ops.parse_comment_id("https://github.com/acme/api/issues/42") is None


class TestGetIssueComments:
    def test_returns_comments_list(self) -> None:
        payload = json.dumps({"comments": [{"body": "hi", "url": "u"}]})
        with patch("coord.github_ops._gh", return_value=payload) as mock_gh:
            comments = github_ops.get_issue_comments("acme/api", 42)
        assert comments == [{"body": "hi", "url": "u"}]
        mock_gh.assert_called_once_with(
            "issue", "view", "42", "--repo", "acme/api", "--json", "comments",
            caller="github_ops.get_issue_comments",
        )

    def test_returns_empty_list_when_no_comments_key(self) -> None:
        with patch("coord.github_ops._gh", return_value="{}"):
            assert github_ops.get_issue_comments("acme/api", 42) == []


class TestPostIssueCommentCaptureAtWrite:
    """post_issue_comment (the single choke point close_issue/close_pr also
    funnel through) must mirror every posted comment into the durable
    issue_comments table via coord.state.record_issue_comment_capture."""

    def setup_method(self) -> None:
        github_ops._login_cache.clear()

    def test_posts_comment_and_captures_it(self) -> None:
        comment_url = "https://github.com/acme/api/issues/42#issuecomment-123456"
        with patch("coord.github_ops._gh", return_value=comment_url) as mock_gh:
            with patch("coord.state.record_issue_comment_capture") as mock_capture:
                github_ops.post_issue_comment("acme/api", 42, "hello world")
        # First _gh call is the actual comment post; a second (best-effort
        # `gh api user` login lookup) may follow — see _current_gh_login.
        assert mock_gh.call_args_list[0].args == (
            "issue", "comment", "42", "--repo", "acme/api", "--body", "hello world"
        )
        mock_capture.assert_called_once()
        _, kwargs = mock_capture.call_args
        assert kwargs["repo_name"] == "acme/api"
        assert kwargs["issue_number"] == 42
        assert kwargs["body"] == "hello world"
        assert kwargs["gh_comment_id"] == 123456

    def test_capture_failure_never_raises(self) -> None:
        """A DB/daemon hiccup while mirroring must never surface as a
        failure of the (already-successful) GitHub post."""
        comment_url = "https://github.com/acme/api/issues/42#issuecomment-1"
        with patch("coord.github_ops._gh", return_value=comment_url):
            with patch(
                "coord.state.record_issue_comment_capture",
                side_effect=RuntimeError("db exploded"),
            ):
                github_ops.post_issue_comment("acme/api", 42, "hello")  # must not raise

    def test_capture_with_unresolvable_comment_id(self) -> None:
        with patch("coord.github_ops._gh", return_value="not a url"):
            with patch("coord.state.record_issue_comment_capture") as mock_capture:
                github_ops.post_issue_comment("acme/api", 42, "hello")
        assert mock_capture.call_args.kwargs["gh_comment_id"] is None

    def test_close_issue_comment_path_also_captures(self) -> None:
        """close_issue posts its --comment through post_issue_comment, so
        the capture hook fires for it without any separate instrumentation."""

        def _dispatch(*args: str, **_kwargs) -> str:
            if args[0] == "issue" and args[1] == "comment":
                return "https://github.com/acme/api/issues/42#issuecomment-99"
            return ""

        with patch("coord.github_ops._gh", side_effect=_dispatch):
            with patch(
                "coord.github_ops.get_open_children", return_value=[]
            ):
                with patch(
                    "coord.github_ops.subprocess.run",
                    return_value=_FakeCompletedProcess(),
                ):
                    with patch(
                        "coord.state.record_issue_comment_capture"
                    ) as mock_capture:
                        github_ops.close_issue(
                            "acme/api", 42, comment="closing this out"
                        )
        mock_capture.assert_called_once()
        assert mock_capture.call_args.kwargs["body"] == "closing this out"


class TestCurrentGhLogin:
    def setup_method(self) -> None:
        github_ops._login_cache.clear()

    def test_caches_login_across_calls(self) -> None:
        with patch("coord.github_ops._gh", return_value="octocat") as mock_gh:
            assert github_ops._current_gh_login() == "octocat"
            assert github_ops._current_gh_login() == "octocat"
        mock_gh.assert_called_once()

    def test_returns_none_on_failure(self) -> None:
        with patch("coord.github_ops._gh", side_effect=RuntimeError("no auth")):
            assert github_ops._current_gh_login() is None


class TestListRepoDir:
    """#1453 review finding 1: the GitHub-fetch backing coord.acceptance.
    resolve_for_path's default mock-lister — same `contents` endpoint
    get_repo_file uses, but returning a directory listing instead of one
    file's content."""

    def test_returns_file_names_only(self) -> None:
        payload = json.dumps([
            {"name": "a.screen", "type": "file"},
            {"name": "b.screen", "type": "file"},
            {"name": "subdir", "type": "dir"},
        ])
        with patch("coord.github_ops._gh", return_value=payload) as mock_gh:
            names = github_ops.list_repo_dir("acme/api", "tests/acceptance/ms-1/mocks", branch="main")
        assert sorted(names) == ["a.screen", "b.screen"]
        args = mock_gh.call_args.args
        assert args[0] == "api"
        assert args[1] == "repos/acme/api/contents/tests/acceptance/ms-1/mocks?ref=main"

    def test_a_single_file_path_returns_empty_list(self) -> None:
        # The contents endpoint returns a single JSON *object* (not a list)
        # when the path names a file, not a directory.
        payload = json.dumps({"name": "contract.md", "type": "file"})
        with patch("coord.github_ops._gh", return_value=payload):
            assert github_ops.list_repo_dir("acme/api", "tests/acceptance/ms-1/contract.md") == []

    def test_missing_path_propagates_the_gh_error(self) -> None:
        with patch("coord.github_ops._gh", side_effect=RuntimeError("gh boom")):
            with pytest.raises(RuntimeError):
                github_ops.list_repo_dir("acme/api", "tests/acceptance/ms-1/mocks")


class TestListRepoSubdirs:
    """#2164: the ``type == "dir"`` sibling of ``list_repo_dir`` — used to
    enumerate ``tests/acceptance/ms-*/`` via the API alone (no local
    checkout) when hunting for the manifest that maps a given issue."""

    def test_returns_dir_names_only(self) -> None:
        payload = json.dumps([
            {"name": "ms01", "type": "dir"},
            {"name": "ms02", "type": "dir"},
            {"name": "README.md", "type": "file"},
        ])
        with patch("coord.github_ops._gh", return_value=payload):
            names = github_ops.list_repo_subdirs("acme/api", "tests/acceptance", branch="main")
        assert sorted(names) == ["ms01", "ms02"]

    def test_a_single_file_path_returns_empty_list(self) -> None:
        payload = json.dumps({"name": "contract.md", "type": "file"})
        with patch("coord.github_ops._gh", return_value=payload):
            assert github_ops.list_repo_subdirs("acme/api", "tests/acceptance/ms-1/contract.md") == []


class TestGetRepoFileWithSha:
    """#2164: get_repo_file's sha-returning sibling — the optimistic-
    concurrency token update_repo_file needs to PUT an edit."""

    def test_returns_content_and_sha(self) -> None:
        import base64
        payload = json.dumps({"content": base64.b64encode(b"hello\n").decode(), "sha": "abc123"})
        with patch("coord.github_ops._gh", return_value=payload):
            content, sha = github_ops.get_repo_file_with_sha("acme/api", "README.md", branch="main")
        assert content == "hello\n"
        assert sha == "abc123"

    def test_get_repo_file_is_a_thin_wrapper(self) -> None:
        import base64
        payload = json.dumps({"content": base64.b64encode(b"hi\n").decode(), "sha": "xyz"})
        with patch("coord.github_ops._gh", return_value=payload):
            assert github_ops.get_repo_file("acme/api", "README.md") == "hi\n"

    def test_missing_sha_is_treated_as_malformed(self) -> None:
        payload = json.dumps({"content": "aGk="})  # no "sha" key
        with patch("coord.github_ops._gh", return_value=payload):
            with pytest.raises(RuntimeError):
                github_ops.get_repo_file_with_sha("acme/api", "README.md")


class TestUpdateRepoFile:
    """#2164: the Contents-API write half of the post-merge expected_red
    clearing sweep — a single commit directly on a branch, no local
    checkout, no raw `git push`."""

    def test_puts_base64_content_with_message_branch_and_sha(self) -> None:
        payload = json.dumps({"commit": {"sha": "newsha"}})
        with patch("coord.github_ops._gh", return_value=payload) as mock_gh:
            result = github_ops.update_repo_file(
                "acme/api", "tests/acceptance/ms01/manifest.yml", "coord/clear-1",
                "tests:\n  a: 1\n", "coord acceptance: clear expected_red",
                sha="blobsha",
            )
        assert result == "newsha"
        args = mock_gh.call_args.args
        assert args[:3] == ("api", "-X", "PUT")
        assert args[3] == "repos/acme/api/contents/tests/acceptance/ms01/manifest.yml"
        joined = " ".join(args)
        assert "message=coord acceptance: clear expected_red" in joined
        assert "branch=coord/clear-1" in joined
        assert "sha=blobsha" in joined


def _unified_diff(content: str) -> str:
    """A minimal single-file unified diff adding *content* as line 1 of ``foo``."""
    return (
        "diff --git a/foo b/foo\n"
        "index e69de29..d95f3ad 100644\n"
        "--- a/foo\n"
        "+++ b/foo\n"
        "@@ -0,0 +1 @@\n"
        f"+{content}\n"
    )


class TestComputePatchId:
    """#1475: content-addressed fingerprint of a diff, via the real
    ``git patch-id --stable`` binary — a pure function on diff text, no repo
    checkout or network required, so exercising the real subprocess is both
    safe and the most faithful test."""

    def test_identical_diffs_produce_the_same_patch_id(self) -> None:
        diff = _unified_diff("hello")
        a = github_ops.compute_patch_id(diff)
        b = github_ops.compute_patch_id(diff)
        assert a is not None
        assert a == b

    def test_a_pure_rebase_reproduces_the_same_diff_text(self) -> None:
        # A rebase that changes no content re-emits byte-identical diff text
        # against the new base (only the commit SHA / context outside the
        # diff changes) — patch-id must therefore be stable across it.
        diff_before_rebase = _unified_diff("hello")
        diff_after_rebase = _unified_diff("hello")
        assert github_ops.compute_patch_id(diff_before_rebase) == (
            github_ops.compute_patch_id(diff_after_rebase)
        )

    def test_different_content_produces_a_different_patch_id(self) -> None:
        a = github_ops.compute_patch_id(_unified_diff("hello"))
        b = github_ops.compute_patch_id(_unified_diff("goodbye"))
        assert a is not None
        assert b is not None
        assert a != b

    def test_none_input_returns_none(self) -> None:
        assert github_ops.compute_patch_id(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert github_ops.compute_patch_id("") is None
        assert github_ops.compute_patch_id("   \n") is None

    def test_subprocess_failure_fails_closed_to_none(self) -> None:
        with patch(
            "coord.github_ops.subprocess.run",
            side_effect=OSError("git not found"),
        ):
            assert github_ops.compute_patch_id(_unified_diff("hello")) is None

    def test_nonzero_exit_fails_closed_to_none(self) -> None:
        class _FakeResult:
            returncode = 1
            stdout = ""

        with patch("coord.github_ops.subprocess.run", return_value=_FakeResult()):
            assert github_ops.compute_patch_id(_unified_diff("hello")) is None


def _gh_ref_dispatch(repo: str, base: str, branch: str, *, head_error=None, base_error=None):
    """Build a ``_gh`` ``side_effect`` for :func:`branch_commits_ahead`'s
    #2324 fallback path: the initial ``compare`` call always fails (the
    caller supplies that via a separate ``side_effect``/``compare_error``),
    and this answers the two follow-up ``git/refs/heads/<ref>`` lookups —
    raising *head_error*/*base_error* when given, succeeding otherwise.
    """
    head_path = f"repos/{repo}/git/refs/heads/{branch}"
    base_path = f"repos/{repo}/git/refs/heads/{base}"

    def _dispatch(*args, **kwargs):
        path = args[1] if len(args) > 1 else ""
        if path == head_path:
            if head_error is not None:
                raise head_error
            return "{}"
        if path == base_path:
            if base_error is not None:
                raise base_error
            return "{}"
        raise AssertionError(f"unexpected _gh call: {args!r}")

    return _dispatch


class TestGhRefConfirmedMissing:
    """#2324: distinguishes a confirmed 404 (GitHub positively saying a ref
    doesn't exist) from an auth/rate-limit/network failure that merely
    looks like one — the exact conflation that made a deleted branch read
    as "unconfirmed" instead of "confirmed empty"."""

    def test_true_on_explicit_404(self) -> None:
        assert github_ops._gh_ref_confirmed_missing(
            RuntimeError("gh api branches: 404 not found")
        ) is True

    def test_true_on_not_found_message(self) -> None:
        assert github_ops._gh_ref_confirmed_missing(
            RuntimeError("gh: Not Found (HTTP 404)")
        ) is True

    def test_false_on_auth_failure(self) -> None:
        assert github_ops._gh_ref_confirmed_missing(
            RuntimeError("gh: HTTP 403: Bad credentials")
        ) is False

    def test_false_on_rate_limit(self) -> None:
        assert github_ops._gh_ref_confirmed_missing(
            RuntimeError("gh: API rate limit exceeded")
        ) is False

    def test_false_on_network_timeout(self) -> None:
        assert github_ops._gh_ref_confirmed_missing(
            RuntimeError("gh: connection timed out")
        ) is False


class TestBranchCommitsAhead:
    """#2324: a compare-API failure that turns out to be a *confirmed* 404 on
    the head branch — with the base branch still resolving — must read as 0
    commits ahead, not None. A deleted branch is the strongest possible
    evidence nothing was pushed; treating it as "unconfirmed" steered `coord
    retry` toward refusing a genuine zero-commit advisory."""

    def test_head_confirmed_deleted_base_resolves_is_zero(self) -> None:
        repo, base, branch = "acme/api", "main", "issue-1-x"

        def _dispatch(*args, **kwargs):
            path = args[1] if len(args) > 1 else ""
            if path == f"repos/{repo}/compare/{base}...{branch}":
                raise RuntimeError("gh: Not Found (HTTP 404)")
            return _gh_ref_dispatch(
                repo, base, branch,
                head_error=RuntimeError("gh: Not Found (HTTP 404)"),
            )(*args, **kwargs)

        with patch("coord.github_ops._gh", side_effect=_dispatch):
            assert github_ops.branch_commits_ahead(repo, base, branch) == 0

    def test_head_resolves_fine_compare_failure_is_none(self) -> None:
        """The compare call fails for some reason other than the head branch
        being gone (e.g. a transient blip) — the head ref lookup itself
        succeeds, so there is nothing to trust. Must stay None."""
        repo, base, branch = "acme/api", "main", "issue-1-x"

        def _dispatch(*args, **kwargs):
            path = args[1] if len(args) > 1 else ""
            if path == f"repos/{repo}/compare/{base}...{branch}":
                raise RuntimeError("gh: connection timed out")
            return _gh_ref_dispatch(repo, base, branch)(*args, **kwargs)

        with patch("coord.github_ops._gh", side_effect=_dispatch):
            assert github_ops.branch_commits_ahead(repo, base, branch) is None

    def test_head_lookup_inconclusive_is_none(self) -> None:
        """Compare fails, and the head-ref lookup fails too but for a
        non-404 (auth/network) reason -- not a confirmed deletion, so this
        must not be guessed as zero."""
        repo, base, branch = "acme/api", "main", "issue-1-x"

        def _dispatch(*args, **kwargs):
            path = args[1] if len(args) > 1 else ""
            if path == f"repos/{repo}/compare/{base}...{branch}":
                raise RuntimeError("gh: Not Found (HTTP 404)")
            return _gh_ref_dispatch(
                repo, base, branch,
                head_error=RuntimeError("gh: HTTP 403: Bad credentials"),
            )(*args, **kwargs)

        with patch("coord.github_ops._gh", side_effect=_dispatch):
            assert github_ops.branch_commits_ahead(repo, base, branch) is None

    def test_head_deleted_but_base_unconfirmable_is_none(self) -> None:
        """Head 404s, but the base ref itself can't be confirmed (e.g. the
        whole repo is having an outage) — not the conclusive "head gone,
        base fine" shape, so this must stay None rather than guess."""
        repo, base, branch = "acme/api", "main", "issue-1-x"

        def _dispatch(*args, **kwargs):
            path = args[1] if len(args) > 1 else ""
            if path == f"repos/{repo}/compare/{base}...{branch}":
                raise RuntimeError("gh: Not Found (HTTP 404)")
            return _gh_ref_dispatch(
                repo, base, branch,
                head_error=RuntimeError("gh: Not Found (HTTP 404)"),
                base_error=RuntimeError("gh: connection timed out"),
            )(*args, **kwargs)

        with patch("coord.github_ops._gh", side_effect=_dispatch):
            assert github_ops.branch_commits_ahead(repo, base, branch) is None

    def test_head_deleted_base_also_deleted_is_none(self) -> None:
        """Both refs 404 -- e.g. the whole repo was renamed/deleted -- so
        there is no confirmed base to compare against. Must not read as
        zero."""
        repo, base, branch = "acme/api", "main", "issue-1-x"

        def _dispatch(*args, **kwargs):
            path = args[1] if len(args) > 1 else ""
            if path == f"repos/{repo}/compare/{base}...{branch}":
                raise RuntimeError("gh: Not Found (HTTP 404)")
            return _gh_ref_dispatch(
                repo, base, branch,
                head_error=RuntimeError("gh: Not Found (HTTP 404)"),
                base_error=RuntimeError("gh: Not Found (HTTP 404)"),
            )(*args, **kwargs)

        with patch("coord.github_ops._gh", side_effect=_dispatch):
            assert github_ops.branch_commits_ahead(repo, base, branch) is None

    def test_successful_compare_unaffected(self) -> None:
        """The happy path (compare succeeds) never touches the new fallback
        -- unchanged from before #2324."""
        with patch(
            "coord.github_ops._gh",
            return_value=json.dumps({"ahead_by": 3}),
        ) as gh:
            assert github_ops.branch_commits_ahead("acme/api", "main", "issue-1-x") == 3
        gh.assert_called_once()


class TestBranchCommitsAheadForAssignment:
    """#1606: the one shared implementation of "branch empty -> 0, repo
    missing -> None, else ask GitHub" — `coord retry`'s advisory gate
    (coord/commands/dispatch.py) and `coord diagnose --stage work`'s
    ADVISORY-row recovery (coord/diagnose.py) both call this instead of
    each keeping an independent inline copy. Duck-typed: *assignment* only
    needs `.branch`/`.repo_name`, *config* only needs `.repo(name)`."""

    class _Assignment:
        def __init__(self, branch: str | None, repo_name: str = "myrepo") -> None:
            self.branch = branch
            self.repo_name = repo_name

    class _RepoCfg:
        def __init__(self, github: str, default_branch: str = "main") -> None:
            self.github = github
            self.default_branch = default_branch

    class _Config:
        def __init__(self, repo_cfg) -> None:
            self._repo_cfg = repo_cfg

        def repo(self, name: str):
            return self._repo_cfg

    def test_empty_branch_is_zero_without_calling_github(self) -> None:
        assignment = self._Assignment(branch="")
        config = self._Config(self._RepoCfg("acme/api"))
        with patch("coord.github_ops._gh") as gh:
            result = github_ops.branch_commits_ahead_for_assignment(assignment, config)
        assert result == 0
        gh.assert_not_called()

    def test_none_branch_is_zero_without_calling_github(self) -> None:
        assignment = self._Assignment(branch=None)
        config = self._Config(self._RepoCfg("acme/api"))
        with patch("coord.github_ops._gh") as gh:
            result = github_ops.branch_commits_ahead_for_assignment(assignment, config)
        assert result == 0
        gh.assert_not_called()

    def test_unknown_repo_is_none(self) -> None:
        assignment = self._Assignment(branch="issue-1-x", repo_name="not-configured")
        config = self._Config(None)
        with patch("coord.github_ops._gh") as gh:
            result = github_ops.branch_commits_ahead_for_assignment(assignment, config)
        assert result is None
        gh.assert_not_called()

    def test_delegates_to_branch_commits_ahead_for_a_real_branch(self) -> None:
        assignment = self._Assignment(branch="issue-1-x")
        config = self._Config(self._RepoCfg("acme/api", default_branch="main"))
        with patch(
            "coord.github_ops._gh",
            return_value=json.dumps({"ahead_by": 7}),
        ) as gh:
            result = github_ops.branch_commits_ahead_for_assignment(assignment, config)
        assert result == 7
        args = gh.call_args.args
        assert args[1] == "repos/acme/api/compare/main...issue-1-x"

    def test_gh_failure_is_none(self) -> None:
        assignment = self._Assignment(branch="issue-1-x")
        config = self._Config(self._RepoCfg("acme/api"))
        with patch("coord.github_ops._gh", side_effect=RuntimeError("gh boom")):
            result = github_ops.branch_commits_ahead_for_assignment(assignment, config)
        assert result is None

    def test_deleted_head_branch_with_resolving_base_is_zero(self) -> None:
        """#2324: `coord retry` on space-invaders#1's exact shape — a
        zero-commit advisory whose branch was already deleted. The compare
        call 404s because the head branch is gone; the base branch
        (default_branch) still resolves. That must read as a genuine
        zero-commit advisory (0), not "could not be confirmed" (None) —
        the None reading is what steered the operator toward the wrong
        remedy (`coord drive --accept-advisory`, which assumes commits
        exist)."""
        assignment = self._Assignment(branch="issue-1-fix")
        config = self._Config(self._RepoCfg("acme/api", default_branch="main"))

        def _dispatch(*args, **kwargs):
            path = args[1] if len(args) > 1 else ""
            if path == "repos/acme/api/compare/main...issue-1-fix":
                raise RuntimeError("gh: Not Found (HTTP 404)")
            if path == "repos/acme/api/git/refs/heads/issue-1-fix":
                raise RuntimeError("gh: Not Found (HTTP 404)")
            if path == "repos/acme/api/git/refs/heads/main":
                return "{}"
            raise AssertionError(f"unexpected _gh call: {args!r}")

        with patch("coord.github_ops._gh", side_effect=_dispatch):
            result = github_ops.branch_commits_ahead_for_assignment(assignment, config)
        assert result == 0


class TestGetBranchSha:
    """#2704: `raise_on_transient` is opt-in and off by default — every
    existing caller's fold-everything-to-``None`` contract is unchanged;
    only a caller that passes the flag sees `GhTransientError` for a
    confirmed transient failure."""

    def test_returns_sha_on_success(self) -> None:
        with patch(
            "coord.github_ops._gh",
            return_value=json.dumps({"commit": {"sha": "abc123"}}),
        ):
            assert github_ops.get_branch_sha("acme/api", "main") == "abc123"

    def test_returns_none_on_gh_error_by_default(self) -> None:
        with patch(
            "coord.github_ops._gh",
            side_effect=RuntimeError("gh api ... failed: HTTP 404: Not Found"),
        ):
            assert github_ops.get_branch_sha("acme/api", "deleted-branch") is None

    def test_returns_none_on_rate_limit_by_default(self) -> None:
        """Without opting in, a rate limit folds to `None` exactly like any
        other failure — the pre-#2704 behaviour every existing caller
        still gets."""
        with patch(
            "coord.github_ops._gh",
            side_effect=RuntimeError(
                "gh api ... failed: HTTP 403: API rate limit exceeded"
            ),
        ):
            assert github_ops.get_branch_sha("acme/api", "main") is None

    def test_raise_on_transient_raises_for_rate_limit(self) -> None:
        with patch(
            "coord.github_ops._gh",
            side_effect=RuntimeError(
                "gh api ... failed: HTTP 403: API rate limit exceeded"
            ),
        ):
            with pytest.raises(github_ops.GhTransientError):
                github_ops.get_branch_sha(
                    "acme/api", "main", raise_on_transient=True
                )

    def test_raise_on_transient_still_returns_none_for_confirmed_absent(self) -> None:
        """A 404 (branch genuinely gone) is not transient — it must keep
        folding to `None` even with the flag set, never raise."""
        with patch(
            "coord.github_ops._gh",
            side_effect=RuntimeError("gh api ... failed: HTTP 404: Not Found"),
        ):
            assert (
                github_ops.get_branch_sha(
                    "acme/api", "deleted-branch", raise_on_transient=True
                )
                is None
            )

    def test_calls_gh_with_include_flag(self) -> None:
        """#2809: `-i` is what lets a rate limit here carry real HTTP
        headers — see `TestGhRateLimitDetection.
        test_include_headers_recover_precise_retry_after`."""
        with patch(
            "coord.github_ops._gh",
            return_value=json.dumps({"commit": {"sha": "abc123"}}),
        ) as gh_mock:
            github_ops.get_branch_sha("acme/api", "main")
        gh_mock.assert_called_once_with(
            "api", "-i", "repos/acme/api/branches/main", caller="github_ops.get_branch_sha",
        )

    def test_success_tolerates_a_bare_gh_stub_with_no_include_headers(self) -> None:
        """A test double (or an old `gh`) that just hands back plain JSON
        with no header block must still parse -- `_parse_gh_include` falls
        back to treating the whole string as the body."""
        with patch(
            "coord.github_ops._gh",
            return_value=json.dumps({"commit": {"sha": "xyz789"}}),
        ):
            assert github_ops.get_branch_sha("acme/api", "main") == "xyz789"

    def test_raise_on_transient_preserves_ghratelimiterror_detail(self) -> None:
        """#2809: when `_gh` itself raised the structured
        `GhRateLimitError` (not a bare RuntimeError), that exact object —
        status/request-id/retry-after intact — must reach the caller, not a
        re-stringified generic `GhTransientError` that throws the detail
        away."""
        rate_limit_exc = github_ops.GhRateLimitError(
            "gh api ... failed: HTTP 403: API rate limit exceeded",
            status_code=403, request_id="req-42", retry_after_s=30.0,
            secondary=True,
        )
        with patch("coord.github_ops._gh", side_effect=rate_limit_exc):
            with pytest.raises(github_ops.GhRateLimitError) as excinfo:
                github_ops.get_branch_sha(
                    "acme/api", "main", raise_on_transient=True
                )
        assert excinfo.value is rate_limit_exc
        assert excinfo.value.status_code == 403
        assert excinfo.value.request_id == "req-42"
        assert excinfo.value.retry_after_s == 30.0
        assert excinfo.value.secondary is True

    def test_raise_on_transient_reraises_from_cache_backoff_error_despite_message_wording(
        self,
    ) -> None:
        """#2809 review: the from-cache "coordinated backoff active" error
        `_gh` raises while `github_throttle`'s shared backoff is active
        (coord/github_ops.py ~337-345) uses `active_backoff.reason` verbatim
        ("secondary_rate_limit", underscore) and renders "status=403" (not
        "HTTP 403") — neither of which matches `_is_transient_error`'s
        substring keyword list. It must still be recognized as transient and
        re-raised AS-IS, because it already IS a `GhRateLimitError` — the
        `isinstance` check must not depend on the keyword scan succeeding.

        Before the #2809-review fix this returned `None` with no exception
        at all: `_gh_get_branch_sha` (merge_queue.py) relies on catching
        `GhTransientError` to distinguish a CONFIRMED transient failure from
        a confirmed-absent branch, and losing that distinction here silently
        regressed #2704's fail-closed smoke-verdict protection.
        """
        from_cache_exc = github_ops.GhRateLimitError(
            "gh api ... skipped: GitHub secondary_rate_limit backoff active "
            "for 45s more (status=403, request_id=req-cache-1)",
            status_code=403, request_id="req-cache-1", retry_after_s=45.0,
            secondary=True, from_cache=True,
        )
        # Sanity: this message is exactly the shape that must NOT rely on
        # keyword matching -- if this assertion ever fails, the from-cache
        # message wording changed and this test should be revisited.
        assert not github_ops._is_transient_error(from_cache_exc)
        with patch("coord.github_ops._gh", side_effect=from_cache_exc):
            with pytest.raises(github_ops.GhRateLimitError) as excinfo:
                github_ops.get_branch_sha(
                    "acme/api", "main", raise_on_transient=True
                )
        assert excinfo.value is from_cache_exc
        assert excinfo.value.status_code == 403
        assert excinfo.value.request_id == "req-cache-1"
        assert excinfo.value.retry_after_s == 45.0
        assert excinfo.value.from_cache is True


class TestGetDefaultBranchHead:
    def test_calls_gh_with_include_flag(self) -> None:
        with patch(
            "coord.github_ops._gh",
            return_value=json.dumps({"commit": {"sha": "abc123"}}),
        ) as gh_mock:
            sha = github_ops.get_default_branch_head("acme/api", "main")
        assert sha == "abc123"
        gh_mock.assert_called_once_with(
            "api", "-i", "repos/acme/api/branches/main",
            caller="github_ops.get_default_branch_head",
        )

    def test_strips_include_headers_before_parsing_json(self) -> None:
        raw = (
            "HTTP/2.0 200 OK\r\n"
            "X-Github-Request-Id: DEAD:BEEF\r\n"
            "\r\n"
            '{"commit": {"sha": "abc123"}}'
        )
        with patch("coord.github_ops._gh", return_value=raw):
            assert github_ops.get_default_branch_head("acme/api", "main") == "abc123"

    def test_malformed_response_raises_runtimeerror(self) -> None:
        with patch("coord.github_ops._gh", return_value="not json"):
            with pytest.raises(RuntimeError):
                github_ops.get_default_branch_head("acme/api", "main")


class TestGetBranchPatchId:
    """#1475: fetches the three-dot compare diff (no PR required, mirroring
    get_branch_diff_size) and hashes it."""

    def test_computes_patch_id_from_compare_diff(self) -> None:
        diff = _unified_diff("hello")
        with patch("coord.github_ops._gh", return_value=diff) as mock_gh:
            result = github_ops.get_branch_patch_id("acme/api", "main", "feature")
        assert result == github_ops.compute_patch_id(diff)
        args = mock_gh.call_args.args
        assert args[0] == "api"
        assert args[1] == "repos/acme/api/compare/main...feature"

    def test_returns_none_on_gh_error(self) -> None:
        with patch("coord.github_ops._gh", side_effect=RuntimeError("gh boom")):
            assert github_ops.get_branch_patch_id("acme/api", "main", "feature") is None


class TestGetCompareDiff:
    """#1476: the raw three-dot diff fetch factored out of get_branch_patch_id
    so a scoped re-review can fetch the diff for a historical SHA too."""

    def test_fetches_the_compare_diff(self) -> None:
        diff = _unified_diff("hello")
        with patch("coord.github_ops._gh", return_value=diff) as mock_gh:
            result = github_ops.get_compare_diff("acme/api", "main", "feature")
        assert result == diff
        args = mock_gh.call_args.args
        assert args[0] == "api"
        assert args[1] == "repos/acme/api/compare/main...feature"

    def test_works_with_a_sha_as_head(self) -> None:
        """A SHA that is no longer any branch's tip — e.g. a review's
        review_head_sha after a conflict-fix rebase moved the branch on —
        is a valid `head` just like a branch name; GitHub's compare API
        doesn't distinguish them."""
        diff = _unified_diff("hello")
        with patch("coord.github_ops._gh", return_value=diff) as mock_gh:
            result = github_ops.get_compare_diff("acme/api", "main", "deadbeef1234")
        assert result == diff
        args = mock_gh.call_args.args
        assert args[1] == "repos/acme/api/compare/main...deadbeef1234"

    def test_returns_none_on_gh_error(self) -> None:
        with patch("coord.github_ops._gh", side_effect=RuntimeError("gh boom")):
            assert github_ops.get_compare_diff("acme/api", "main", "feature") is None

    def test_get_branch_patch_id_delegates_to_get_compare_diff(self) -> None:
        """Refactor guard: get_branch_patch_id must still hash exactly the
        diff get_compare_diff would fetch (#1475 behaviour unchanged)."""
        diff = _unified_diff("hello")
        with patch("coord.github_ops.get_compare_diff", return_value=diff) as mock_diff:
            result = github_ops.get_branch_patch_id("acme/api", "main", "feature")
        mock_diff.assert_called_once_with("acme/api", "main", "feature")
        assert result == github_ops.compute_patch_id(diff)


class TestGhMissingOrHung:
    """#1483: `_gh` is the single seam every helper in this module funnels
    through, so a missing/hung `gh` binary must fail the same way (a
    `RuntimeError` subclass) for every caller — not just `RuntimeError` on a
    non-zero exit. Regression coverage for the elitebook incident: a worker
    whose PATH didn't include `gh` crashed instead of degrading gracefully.
    """

    def test_gh_not_found_raises_gherror(self) -> None:
        with patch("coord.github_ops.subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(github_ops.GhError):
                github_ops._gh("issue", "view", "1")

    def test_gh_timeout_raises_gherror(self) -> None:
        with patch(
            "coord.github_ops.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=30),
        ):
            with pytest.raises(github_ops.GhError):
                github_ops._gh("issue", "view", "1")

    def test_gherror_is_a_runtimeerror(self) -> None:
        """GhError must subclass RuntimeError so existing `except
        RuntimeError` call sites catch a missing/hung gh without change."""
        with patch("coord.github_ops.subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(RuntimeError):
                github_ops._gh("issue", "view", "1")


class TestGhForgeAvailabilityRecording:
    """#1896 Phase 0: `_gh` is the single seam every `gh` invocation in this
    module funnels through (#1483), so it is also the single place that
    records one forge-availability observation per call — exit status,
    duration, and a reachability classification distinguishing "gh could
    not even run" / "an ordinary app-level error" / "an auth/network/rate-
    limit failure" from a clean success."""

    def test_success_records_ok(self, coord_db) -> None:
        with patch(
            "coord.github_ops.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="hi", stderr=""),
        ):
            github_ops._gh("issue", "view", "1")
        # #2654: "ok" observations buffer in-process and flush on bucket
        # roll/atexit/an interesting outcome rather than writing immediately.
        _flush_all_ok_aggregates()

        row = coord_db.execute(
            "SELECT * FROM audit_log WHERE category='forge_availability'"
        ).fetchone()
        assert row is not None
        details = json.loads(row["details_json"])
        assert details["outcome"] == "ok"
        assert details["duration_s_total"] >= 0  # #2654: aggregate field, not per-call

    def test_gh_not_found_records_unreachable(self, coord_db) -> None:
        with patch("coord.github_ops.subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(github_ops.GhError):
                github_ops._gh("issue", "view", "1")

        row = coord_db.execute(
            "SELECT * FROM audit_log WHERE category='forge_availability'"
        ).fetchone()
        assert json.loads(row["details_json"])["outcome"] == "unreachable"

    def test_ordinary_app_error_is_not_classified_as_transient(self, coord_db) -> None:
        with patch(
            "coord.github_ops.subprocess.run",
            return_value=MagicMock(returncode=1, stdout="", stderr="label 'x' not found"),
        ):
            with pytest.raises(RuntimeError):
                github_ops._gh("label", "create", "x")

        row = coord_db.execute(
            "SELECT * FROM audit_log WHERE category='forge_availability'"
        ).fetchone()
        assert json.loads(row["details_json"])["outcome"] == "app_error"

    def test_auth_failure_is_classified_as_transient(self, coord_db) -> None:
        with patch(
            "coord.github_ops.subprocess.run",
            return_value=MagicMock(returncode=1, stdout="", stderr="HTTP 401: Bad credentials"),
        ):
            with pytest.raises(RuntimeError):
                github_ops._gh("issue", "view", "1")

        row = coord_db.execute(
            "SELECT * FROM audit_log WHERE category='forge_availability'"
        ).fetchone()
        assert json.loads(row["details_json"])["outcome"] == "transient"

    def test_recording_failure_never_breaks_a_real_gh_call(self, coord_db, monkeypatch) -> None:
        """Acceptance bar: capture is strictly best-effort — a store that
        always throws must never raise into `_gh`'s caller."""
        monkeypatch.setattr(
            "coord.forge_availability.record_audit",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        with patch(
            "coord.github_ops.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="hi", stderr=""),
        ):
            assert github_ops._gh("issue", "view", "1") == "hi"


class TestGhRateLimitDetection:
    """#2809: `_gh` must classify a rate-limit failure specifically (not just
    generic "transient"), extract what detail `gh`'s stderr text offers, feed
    the shared backoff (`coord.github_throttle`), and — for calls that pass
    `-i` — recover the real `Retry-After`/`X-GitHub-Request-Id` headers from
    stdout even though the call still exits non-zero.
    """

    def test_primary_rate_limit_raises_ghratelimiterror(self, coord_db) -> None:
        with patch(
            "coord.github_ops.subprocess.run",
            return_value=MagicMock(
                returncode=1, stdout="",
                stderr=(
                    "gh: API rate limit exceeded for user ID 3506413. "
                    "request ID E126:C7B0E:4B13E6D:FBCA178:6A8F32B0 (HTTP 403)"
                ),
            ),
        ):
            with pytest.raises(github_ops.GhRateLimitError) as excinfo:
                github_ops._gh("api", "repos/acme/api/branches/main")
        exc = excinfo.value
        assert exc.status_code == 403
        assert exc.request_id == "E126:C7B0E:4B13E6D:FBCA178:6A8F32B0"
        assert exc.secondary is False
        assert exc.from_cache is False
        # A GhRateLimitError is still a GhTransientError/RuntimeError — every
        # existing `except` call site catches it unchanged.
        assert isinstance(exc, github_ops.GhTransientError)
        assert isinstance(exc, RuntimeError)

    def test_secondary_rate_limit_is_flagged_as_such(self, coord_db) -> None:
        with patch(
            "coord.github_ops.subprocess.run",
            return_value=MagicMock(
                returncode=1, stdout="",
                stderr="gh: You have exceeded a secondary rate limit (HTTP 403)",
            ),
        ):
            with pytest.raises(github_ops.GhRateLimitError) as excinfo:
                github_ops._gh("api", "repos/acme/api/branches/main")
        assert excinfo.value.secondary is True

    def test_plain_403_without_rate_limit_wording_is_not_misclassified(
        self, coord_db,
    ) -> None:
        """A permissions 403 (token lacks a scope) must NOT engage the
        shared backoff -- no wait fixes a scope problem, and pausing every
        other `gh` call on the host for it would be pure self-harm."""
        with patch(
            "coord.github_ops.subprocess.run",
            return_value=MagicMock(
                returncode=1, stdout="",
                stderr="gh: Resource not accessible by integration (HTTP 403)",
            ),
        ):
            with pytest.raises(RuntimeError) as excinfo:
                github_ops._gh("api", "repos/acme/api/branches/main")
        assert not isinstance(excinfo.value, github_ops.GhRateLimitError)
        from coord import github_throttle
        assert github_throttle.current() is None

    def test_rate_limit_feeds_the_shared_backoff(self, coord_db) -> None:
        from coord import github_throttle

        assert github_throttle.current() is None
        with patch(
            "coord.github_ops.subprocess.run",
            return_value=MagicMock(
                returncode=1, stdout="",
                stderr="gh: API rate limit exceeded for user ID 1 (HTTP 403)",
            ),
        ):
            with pytest.raises(github_ops.GhRateLimitError):
                github_ops._gh("api", "repos/acme/api/branches/main")
        backoff = github_throttle.current()
        assert backoff is not None
        assert backoff.status == 403

    def test_include_headers_recover_precise_retry_after(self, coord_db) -> None:
        """A `-i` call (e.g. get_branch_sha) puts real HTTP headers on
        stdout even on a non-2xx response -- `_gh` must prefer that
        `Retry-After` over the (always-None, text has no such field) stderr
        extraction."""
        stdout = (
            "HTTP/2.0 403 Forbidden\r\n"
            "X-Github-Request-Id: AAAA:BBBB:CCCC\r\n"
            "Retry-After: 47\r\n"
            "\r\n"
            '{"message": "API rate limit exceeded", "status": "403"}'
        )
        with patch(
            "coord.github_ops.subprocess.run",
            return_value=MagicMock(
                returncode=1, stdout=stdout,
                stderr="gh: API rate limit exceeded (HTTP 403)",
            ),
        ):
            with pytest.raises(github_ops.GhRateLimitError) as excinfo:
                github_ops._gh("api", "-i", "repos/acme/api/branches/main")
        exc = excinfo.value
        assert exc.status_code == 403
        assert exc.request_id == "AAAA:BBBB:CCCC"
        assert exc.retry_after_s == 47.0

    def test_primary_wording_reclassified_secondary_when_quota_healthy(
        self, coord_db,
    ) -> None:
        """#2858: `gh`'s own stderr says nothing about "secondary", but a
        live `gh api rate_limit` read (the second `subprocess.run` call --
        `_primary_quota_healthy`) shows the primary quota comfortably
        unused. That is the real signature of the secondary (abuse-
        detection) limiter, per `coord.github_throttle`'s own docstring --
        must reclassify rather than trust `gh`'s ambiguous wording.
        """
        responses = [
            MagicMock(
                returncode=1, stdout="",
                stderr="gh: API rate limit exceeded for user ID 1 (HTTP 403)",
            ),
            MagicMock(
                returncode=0,
                stdout=json.dumps(
                    {"resources": {"core": {"limit": 5000, "remaining": 4986}}}
                ),
                stderr="",
            ),
        ]
        with patch("coord.github_ops.subprocess.run", side_effect=responses):
            with pytest.raises(github_ops.GhRateLimitError) as excinfo:
                github_ops._gh("api", "repos/acme/api/branches/main")
        assert excinfo.value.secondary is True
        from coord import github_throttle

        backoff = github_throttle.current()
        assert backoff is not None and backoff.reason == "secondary_rate_limit"

    def test_primary_wording_stays_primary_when_quota_check_fails(
        self, coord_db,
    ) -> None:
        """The `gh api rate_limit` confirmation call itself can fail (auth,
        network) -- `_primary_quota_healthy` returns unknown, and #2858
        leaves the pre-existing classification untouched rather than
        guessing."""
        responses = [
            MagicMock(
                returncode=1, stdout="",
                stderr="gh: API rate limit exceeded (HTTP 403)",
            ),
            MagicMock(returncode=1, stdout="", stderr="gh: auth error"),
        ]
        with patch("coord.github_ops.subprocess.run", side_effect=responses):
            with pytest.raises(github_ops.GhRateLimitError) as excinfo:
                github_ops._gh("api", "repos/acme/api/branches/main")
        assert excinfo.value.secondary is False

    def test_primary_wording_stays_primary_when_quota_genuinely_exhausted(
        self, coord_db,
    ) -> None:
        """A REAL primary-quota exhaustion (remaining near 0) must NOT be
        reclassified as secondary -- #2858's fix only overrides the label
        when there is positive evidence the primary quota is healthy."""
        responses = [
            MagicMock(
                returncode=1, stdout="",
                stderr="gh: API rate limit exceeded (HTTP 403)",
            ),
            MagicMock(
                returncode=0,
                stdout=json.dumps(
                    {"resources": {"core": {"limit": 5000, "remaining": 0}}}
                ),
                stderr="",
            ),
        ]
        with patch("coord.github_ops.subprocess.run", side_effect=responses):
            with pytest.raises(github_ops.GhRateLimitError) as excinfo:
                github_ops._gh("api", "repos/acme/api/branches/main")
        assert excinfo.value.secondary is False


class TestGhBackoffConsultedBeforeEachCall:
    """#2809: `_gh` consults the shared backoff BEFORE issuing a network
    call — deep inside an active window it skips the call entirely (the
    actual damping), and inside a short remaining window it rides it out
    with a bounded sleep instead."""

    def test_deep_inside_backoff_skips_the_network_call(self, coord_db) -> None:
        from coord import github_throttle

        github_throttle.record(
            reason="secondary_rate_limit", status=403,
            request_id="orig-request-id", retry_after_s=600.0,
        )
        run_mock = MagicMock()
        with patch("coord.github_ops.subprocess.run", run_mock):
            with pytest.raises(github_ops.GhRateLimitError) as excinfo:
                github_ops._gh("issue", "view", "1")
        run_mock.assert_not_called()
        exc = excinfo.value
        assert exc.from_cache is True
        assert exc.request_id == "orig-request-id"

    def test_get_branch_sha_reraises_through_a_deep_active_backoff(self, coord_db) -> None:
        """#2809 review, end-to-end regression for the swallow: with the
        shared backoff active (as it will be for most calls during a
        sustained incident — the dominant state, not the exceptional one),
        `get_branch_sha(..., raise_on_transient=True)` — the exact call
        `_gh_get_branch_sha`/`evaluate_smoke_verdict` make — must raise
        `GhTransientError`, not swallow the from-cache error into a bare
        `None`.

        This combines what neither pre-existing test file combined: a real
        active `github_throttle` backoff with a real `get_branch_sha` call
        through the genuine `coord.github_ops` implementation (not a
        hand-written stub that raises `GhRateLimitError` directly and
        bypasses `_is_transient_error`'s guard).
        """
        from coord import github_throttle

        github_throttle.record(
            reason="secondary_rate_limit", status=403,
            request_id="orig-request-id", retry_after_s=600.0,
        )
        run_mock = MagicMock()
        with patch("coord.github_ops.subprocess.run", run_mock):
            with pytest.raises(github_ops.GhTransientError) as excinfo:
                github_ops.get_branch_sha(
                    "acme/api", "main", raise_on_transient=True
                )
        run_mock.assert_not_called()
        exc = excinfo.value
        assert isinstance(exc, github_ops.GhRateLimitError)
        assert exc.from_cache is True
        assert exc.request_id == "orig-request-id"

    def test_near_end_of_backoff_sleeps_then_proceeds(self, coord_db) -> None:
        from coord import github_throttle

        github_throttle.record(
            reason="secondary_rate_limit", status=403,
            request_id=None, retry_after_s=2.0,
        )
        sleeps = []
        with patch("coord.github_ops.time.sleep", side_effect=sleeps.append), patch(
            "coord.github_ops.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="hi", stderr=""),
        ) as run_mock:
            result = github_ops._gh("issue", "view", "1")
        assert result == "hi"
        run_mock.assert_called_once()
        assert len(sleeps) == 1
        assert sleeps[0] > 0

    def test_no_backoff_never_sleeps(self, coord_db) -> None:
        with patch("coord.github_ops.time.sleep") as sleep_mock, patch(
            "coord.github_ops.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="hi", stderr=""),
        ):
            github_ops._gh("issue", "view", "1")
        sleep_mock.assert_not_called()

    def test_force_through_backoff_bypasses_deep_skip_and_succeeds(
        self, coord_db,
    ) -> None:
        """#2858: a caller that sets ``force_through_backoff=True`` still
        gets the short jittered pre-call sleep, but skips the "still deep
        inside the window, don't even try" raise that
        ``test_deep_inside_backoff_skips_the_network_call`` above confirms
        for an ordinary caller — the starvation-floor escape hatch for
        ``coord.serve_app._sync_issues_tick``.
        """
        from coord import github_throttle

        github_throttle.record(
            reason="secondary_rate_limit", status=403,
            request_id="orig-request-id", retry_after_s=600.0,
        )
        with patch("coord.github_ops.time.sleep") as sleep_mock, patch(
            "coord.github_ops.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="[]", stderr=""),
        ) as run_mock:
            result = github_ops._gh(
                "issue", "list", force_through_backoff=True
            )
        assert result == "[]"
        run_mock.assert_called_once()
        sleep_mock.assert_called_once()

    def test_force_through_backoff_still_raises_on_a_real_rate_limit(
        self, coord_db,
    ) -> None:
        """Bypassing the pre-emptive skip is not a guarantee of success --
        if the real network call still comes back rate-limited, it still
        raises (and still re-records the hit) exactly like any other
        caller's real attempt would; #2858 only removes the SAMPLING
        starvation, not the limiter itself."""
        from coord import github_throttle

        github_throttle.record(
            reason="secondary_rate_limit", status=403,
            request_id="orig-request-id", retry_after_s=600.0,
        )
        with patch("coord.github_ops.time.sleep"), patch(
            "coord.github_ops.subprocess.run",
            return_value=MagicMock(
                returncode=1, stdout="",
                stderr="gh: You have exceeded a secondary rate limit (HTTP 403)",
            ),
        ):
            with pytest.raises(github_ops.GhRateLimitError) as excinfo:
                github_ops._gh("issue", "list", force_through_backoff=True)
        assert excinfo.value.from_cache is False


class TestParseGhInclude:
    """`_parse_gh_include` splits `gh api -i ...` output into (meta, body) —
    the seam that recovers `Retry-After`/`X-GitHub-Request-Id` from a real
    HTTP response rendered as text."""

    def test_splits_headers_from_body(self) -> None:
        raw = (
            "HTTP/2.0 200 OK\r\n"
            "X-Github-Request-Id: DEAD:BEEF\r\n"
            "\r\n"
            '{"commit": {"sha": "abc123"}}'
        )
        meta, body = github_ops._parse_gh_include(raw)
        assert meta.status == 200
        assert meta.request_id == "DEAD:BEEF"
        assert body == '{"commit": {"sha": "abc123"}}'

    def test_no_header_block_returns_raw_as_body(self) -> None:
        raw = '{"commit": {"sha": "abc123"}}'
        meta, body = github_ops._parse_gh_include(raw)
        assert meta.status is None
        assert meta.request_id is None
        assert meta.retry_after_s is None
        assert body == raw

    def test_lf_only_separator_also_parses(self) -> None:
        raw = "HTTP/2.0 403 Forbidden\nRetry-After: 12\n\n{}"
        meta, body = github_ops._parse_gh_include(raw)
        assert meta.status == 403
        assert meta.retry_after_s == 12.0
        assert body == "{}"


def _forge_availability_rows(coord_db) -> list[dict]:
    rows = coord_db.execute(
        "SELECT * FROM audit_log WHERE category='forge_availability'"
        " ORDER BY id"
    ).fetchall()
    return [json.loads(r["details_json"]) for r in rows]


class TestDirectGhCallSitesRecordForgeAvailability:
    """#1896 review: `close_issue`, `reopen_issue`, `edit_issue`,
    `rerun_workflow_run`, and `rerun_workflow_run_failed` all shell out to
    `gh` directly instead of through `_gh` (each for a documented,
    behavior-preserving reason), so they used to be a silent gap in the
    forge-availability measurement — a real outage coinciding with a wave of
    issue-close/reopen/rerun calls would have been invisible. Each now
    records the same observation `_gh` would have."""

    def test_close_issue_records_ok(self, coord_db) -> None:
        with patch("coord.github_ops.subprocess.run",
                    return_value=MagicMock(returncode=0, stdout="", stderr="")):
            github_ops.close_issue("acme/api", 42, force=True)
        _flush_all_ok_aggregates()  # #2654: "ok" observations buffer until flushed
        details = _forge_availability_rows(coord_db)
        assert details[-1]["outcome"] == "ok"
        assert details[-1]["shape"] == "issue close {n}"
        assert details[-1]["caller"] == "github_ops.close_issue"

    def test_close_issue_already_closed_still_records_and_does_not_raise(
        self, coord_db,
    ) -> None:
        with patch(
            "coord.github_ops.subprocess.run",
            return_value=MagicMock(returncode=1, stdout="", stderr="GraphQL: Issue already closed"),
        ):
            github_ops.close_issue("acme/api", 42, force=True)  # must not raise
        details = _forge_availability_rows(coord_db)
        assert details[-1]["outcome"] == "app_error"

    def test_close_issue_gh_missing_records_unreachable(self, coord_db) -> None:
        with patch("coord.github_ops.subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(FileNotFoundError):
                github_ops.close_issue("acme/api", 42, force=True)
        details = _forge_availability_rows(coord_db)
        assert details[-1]["outcome"] == "unreachable"

    def test_reopen_issue_records_ok(self, coord_db) -> None:
        with patch("coord.github_ops.subprocess.run",
                    return_value=MagicMock(returncode=0, stdout="", stderr="")):
            github_ops.reopen_issue("acme/api", 42)
        _flush_all_ok_aggregates()  # #2654: "ok" observations buffer until flushed
        details = _forge_availability_rows(coord_db)
        assert details[-1]["outcome"] == "ok"

    def test_reopen_issue_transient_failure_records_transient_and_raises(
        self, coord_db,
    ) -> None:
        with patch(
            "coord.github_ops.subprocess.run",
            return_value=MagicMock(returncode=1, stdout="", stderr="HTTP 401: Bad credentials"),
        ):
            with pytest.raises(RuntimeError):
                github_ops.reopen_issue("acme/api", 42)
        details = _forge_availability_rows(coord_db)
        assert details[-1]["outcome"] == "transient"

    def test_edit_issue_records_ok(self, coord_db) -> None:
        with patch("coord.github_ops.subprocess.run",
                    return_value=MagicMock(returncode=0, stdout="", stderr="")):
            github_ops.edit_issue("acme/api", 42, title="new title")
        _flush_all_ok_aggregates()  # #2654: "ok" observations buffer until flushed
        details = _forge_availability_rows(coord_db)
        assert details[-1]["outcome"] == "ok"
        assert details[-1]["shape"] == "issue edit {n}"
        assert details[-1]["caller"] == "github_ops.edit_issue"

    def test_edit_issue_failure_records_app_error_and_raises(self, coord_db) -> None:
        with patch(
            "coord.github_ops.subprocess.run",
            return_value=MagicMock(returncode=1, stdout="", stderr="some error"),
        ):
            with pytest.raises(RuntimeError):
                github_ops.edit_issue("acme/api", 42, title="new title")
        details = _forge_availability_rows(coord_db)
        assert details[-1]["outcome"] == "app_error"

    def test_rerun_workflow_run_records_ok(self, coord_db) -> None:
        with patch("coord.github_ops.subprocess.run",
                    return_value=MagicMock(returncode=0, stdout="", stderr="")):
            assert github_ops.rerun_workflow_run("acme/api", "12345") is True
        _flush_all_ok_aggregates()  # #2654: "ok" observations buffer until flushed
        details = _forge_availability_rows(coord_db)
        assert details[-1]["outcome"] == "ok"
        assert details[-1]["shape"] == "run rerun {n}"
        assert details[-1]["caller"] == "github_ops.rerun_workflow_run"

    def test_rerun_workflow_run_gh_missing_records_unreachable(self, coord_db) -> None:
        with patch("coord.github_ops.subprocess.run", side_effect=FileNotFoundError):
            assert github_ops.rerun_workflow_run("acme/api", "12345") is False
        details = _forge_availability_rows(coord_db)
        assert details[-1]["outcome"] == "unreachable"

    def test_rerun_workflow_run_failed_records_ok(self, coord_db) -> None:
        with patch("coord.github_ops.subprocess.run",
                    return_value=MagicMock(returncode=0, stdout="", stderr="")):
            assert github_ops.rerun_workflow_run_failed("acme/api", "12345") is True
        _flush_all_ok_aggregates()  # #2654: "ok" observations buffer until flushed
        details = _forge_availability_rows(coord_db)
        assert details[-1]["outcome"] == "ok"
        assert details[-1]["shape"] == "run rerun {n}"
        assert details[-1]["caller"] == "github_ops.rerun_workflow_run_failed"

    def test_rerun_workflow_run_failed_nonzero_exit_records_app_error(
        self, coord_db,
    ) -> None:
        with patch(
            "coord.github_ops.subprocess.run",
            return_value=MagicMock(returncode=1, stdout="", stderr="run already in progress"),
        ):
            assert github_ops.rerun_workflow_run_failed("acme/api", "12345") is False
        details = _forge_availability_rows(coord_db)
        assert details[-1]["outcome"] == "app_error"


class TestGhJsonHelper:
    """#1353: `_gh` treats a `gh` call that exits 0 with empty stdout as a
    success, indistinguishable from a real empty payload — so a bare
    `json.loads(_gh(...))` at ~15 call sites in this module used to raise an
    unattributable `json.JSONDecodeError: Expecting value: line 1 column 1
    (char 0)` on that edge case (the incident that prompted this issue).
    `_json_loads_or`/`_gh_json` are the one guarded decode every such site
    now routes through, so this degrades to a documented *default* instead."""

    def test_json_loads_or_returns_default_on_empty_string(self) -> None:
        assert github_ops._json_loads_or("", default=[]) == []
        assert github_ops._json_loads_or("   ", default={}) == {}

    def test_json_loads_or_returns_default_on_malformed_json(self) -> None:
        assert github_ops._json_loads_or("{not valid json", default=[]) == []

    def test_json_loads_or_decodes_valid_json(self) -> None:
        assert github_ops._json_loads_or('{"a": 1}', default={}) == {"a": 1}

    def test_gh_json_fails_open_on_empty_stdout(self) -> None:
        """The exact #1353 trigger: `gh` exits 0 with empty stdout."""
        with patch("coord.github_ops._gh", return_value=""):
            assert github_ops._gh_json("pr", "view", "1", default={}) == {}

    def test_gh_json_still_raises_on_nonzero_gh_exit(self) -> None:
        """Only the decode step fails open — `_gh`'s own non-zero-exit
        contract (a real `gh` failure, not a garbage-but-successful
        response) is unchanged."""
        with patch(
            "coord.github_ops.subprocess.run",
            return_value=MagicMock(returncode=1, stdout="", stderr="boom"),
        ):
            with pytest.raises(RuntimeError):
                github_ops._gh_json("pr", "view", "1", default={})

    def test_get_open_issues_degrades_to_empty_list_on_empty_stdout(self) -> None:
        """Regression: one of the explicitly-named unguarded sites (#1353) —
        this used to crash with a bare JSONDecodeError."""
        with patch("coord.github_ops._gh", return_value=""):
            assert github_ops.get_open_issues("acme/api") == []

    def test_get_pr_size_degrades_to_zero_on_empty_stdout(self) -> None:
        """Regression: another explicitly-named unguarded site (#1353)."""
        with patch("coord.github_ops._gh", return_value=""):
            assert github_ops.get_pr_size("acme/api", 42) == 0

    def test_find_pr_for_branch_degrades_to_none_on_empty_stdout(self) -> None:
        """Regression: another explicitly-named unguarded site (#1353)."""
        with patch("coord.github_ops._gh", return_value=""):
            assert github_ops.find_pr_for_branch("acme/api", "some-branch") is None


class TestCreateLabel:
    """#1483: the seam behind `coord set-test-mode`'s label pre-creation."""

    def test_builds_expected_argv(self) -> None:
        with patch("coord.github_ops._gh", return_value="") as mock_gh:
            github_ops.create_label(
                "acme/api", "test-mode:auto", color="0075ca", description="d",
            )
        args = mock_gh.call_args.args
        assert args[:3] == ("label", "create", "test-mode:auto")
        assert "--force" in args  # force=True is the default

    def test_raises_runtimeerror_on_gh_failure(self) -> None:
        with patch("coord.github_ops._gh", side_effect=RuntimeError("gh boom")):
            with pytest.raises(RuntimeError):
                github_ops.create_label("acme/api", "test-mode:auto")

    def test_raises_runtimeerror_when_gh_is_missing(self) -> None:
        """A missing `gh` binary must still surface as a `RuntimeError` (via
        `GhError`), not an uncaught `FileNotFoundError` — `coord.commands.
        test_gate.set_test_mode` only catches `except RuntimeError`."""
        with patch("coord.github_ops.subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(RuntimeError):
                github_ops.create_label("acme/api", "test-mode:auto")


class TestGetPrStateForBranch:
    """#1483: used by GitMergeVerifier.verify_merged, which calls this with
    no try/except of its own — the function's own fail-to-None contract is
    the only thing standing between a missing/hung gh and an unhandled crash
    in `coord drive`."""

    def test_returns_state_on_success(self) -> None:
        with patch("coord.github_ops._gh", return_value="MERGED") as mock_gh:
            assert github_ops.get_pr_state_for_branch("acme/api", "feature") == "MERGED"
        args = mock_gh.call_args.args
        assert args[:2] == ("pr", "view")
        assert "feature" in args

    def test_returns_none_on_gh_error(self) -> None:
        with patch("coord.github_ops._gh", side_effect=RuntimeError("no such PR")):
            assert github_ops.get_pr_state_for_branch("acme/api", "feature") is None

    def test_returns_none_when_gh_is_missing(self) -> None:
        with patch("coord.github_ops.subprocess.run", side_effect=FileNotFoundError):
            assert github_ops.get_pr_state_for_branch("acme/api", "feature") is None

    def test_returns_none_when_gh_times_out(self) -> None:
        with patch(
            "coord.github_ops.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=30),
        ):
            assert github_ops.get_pr_state_for_branch("acme/api", "feature") is None


class TestGetPrHeadRef:
    """#1483: used by coord.commands.test_gate._maybe_reconcile_branch, which
    calls this with no try/except of its own — same contract as
    get_pr_state_for_branch above."""

    def test_returns_head_ref_on_success(self) -> None:
        with patch("coord.github_ops._gh", return_value="issue-42-fix") as mock_gh:
            assert github_ops.get_pr_head_ref("acme/api", 42) == "issue-42-fix"
        args = mock_gh.call_args.args
        assert args[:2] == ("pr", "view")

    def test_returns_none_on_gh_error(self) -> None:
        with patch("coord.github_ops._gh", side_effect=RuntimeError("no such PR")):
            assert github_ops.get_pr_head_ref("acme/api", 42) is None

    def test_returns_none_when_gh_is_missing(self) -> None:
        with patch("coord.github_ops.subprocess.run", side_effect=FileNotFoundError):
            assert github_ops.get_pr_head_ref("acme/api", 42) is None

    def test_returns_none_when_gh_times_out(self) -> None:
        with patch(
            "coord.github_ops.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=30),
        ):
            assert github_ops.get_pr_head_ref("acme/api", 42) is None


class TestGetPrDeploymentUrl:
    """#2948: the live GitHub-Deployment lookup that replaced the
    ``{pr_branch_slug}`` Cloudflare-Pages template placeholder — confirmed
    live to never resolve for a real project (see docs/CUSTOMER_FACING_APPS.md
    §1 and coord.models.Repo.uat_preview's docstring)."""

    def test_matches_preview_environment_not_recency(self) -> None:
        # A production deployment (id 2) is newer/first in the list — must
        # be skipped in favour of the "(Preview)" one, not picked for being
        # first.
        deployments = json.dumps([
            {"id": 2, "environment": "natal-chart (Production)"},
            {"id": 1, "environment": "natal-chart (Preview)"},
        ])
        statuses = json.dumps([
            {"environment_url": "https://abc123.natal-chart-3ew.pages.dev"},
        ])
        with patch(
            "coord.github_ops._gh", side_effect=[deployments, statuses],
        ) as mock_gh:
            url = github_ops.get_pr_deployment_url("acme/natal-chart", "issue-1-x")
        assert url == "https://abc123.natal-chart-3ew.pages.dev"
        assert mock_gh.call_count == 2
        assert mock_gh.call_args_list[0].args[1] == (
            "repos/acme/natal-chart/deployments?ref=issue-1-x"
        )
        assert mock_gh.call_args_list[1].args[1] == (
            "repos/acme/natal-chart/deployments/1/statuses"
        )

    def test_skips_non_preview_environment_deployments(self) -> None:
        deployments = json.dumps([{"id": 5, "environment": "natal-chart (Production)"}])
        with patch("coord.github_ops._gh", return_value=deployments):
            assert github_ops.get_pr_deployment_url("acme/natal-chart", "main") is None

    def test_returns_none_when_no_deployments(self) -> None:
        with patch("coord.github_ops._gh", return_value="[]"):
            assert github_ops.get_pr_deployment_url("acme/api", "issue-1-x") is None

    def test_returns_none_on_gh_failure(self) -> None:
        with patch("coord.github_ops._gh", side_effect=RuntimeError("gh boom")):
            assert github_ops.get_pr_deployment_url("acme/api", "issue-1-x") is None

    def test_returns_none_when_matched_deployment_has_no_status_url_yet(self) -> None:
        deployments = json.dumps([{"id": 1, "environment": "api (Preview)"}])
        statuses = json.dumps([{"state": "pending"}])  # no environment_url yet
        with patch("coord.github_ops._gh", side_effect=[deployments, statuses]):
            assert github_ops.get_pr_deployment_url("acme/api", "issue-1-x") is None

    def test_falls_through_to_next_preview_deployment_on_malformed_statuses(self) -> None:
        deployments = json.dumps([
            {"id": 1, "environment": "api (Preview)"},
            {"id": 2, "environment": "api (Preview)"},
        ])
        with patch(
            "coord.github_ops._gh",
            side_effect=[
                deployments, "not json",
                json.dumps([{"environment_url": "https://ok.example"}]),
            ],
        ):
            url = github_ops.get_pr_deployment_url("acme/api", "issue-1-x")
        assert url == "https://ok.example"


class TestGetRepoWorkflowCount:
    """#1904: backs `GitHubCi.expects_checks` — the signal that tells "no CI
    configured for this repo" apart from "CI exists but never triggered"
    when `gh pr checks` comes back empty."""

    def test_returns_total_count(self) -> None:
        payload = json.dumps({"total_count": 3, "workflows": [{}, {}, {}]})

        class _FakeResult:
            returncode = 0
            stdout = payload
            stderr = ""

        with patch("coord.github_ops.subprocess.run", return_value=_FakeResult()):
            assert github_ops.get_repo_workflow_count("acme/api") == 3

    def test_zero_workflows(self) -> None:
        payload = json.dumps({"total_count": 0, "workflows": []})

        class _FakeResult:
            returncode = 0
            stdout = payload
            stderr = ""

        with patch("coord.github_ops.subprocess.run", return_value=_FakeResult()):
            assert github_ops.get_repo_workflow_count("acme/api") == 0

    def test_nonzero_exit_raises(self) -> None:
        class _FakeResult:
            returncode = 1
            stdout = ""
            stderr = "authentication required"

        with patch("coord.github_ops.subprocess.run", return_value=_FakeResult()):
            with pytest.raises(RuntimeError):
                github_ops.get_repo_workflow_count("acme/api")

    def test_malformed_response_raises_not_fails_open(self) -> None:
        """A response missing `total_count` must raise — not default to 0 —
        so `GitHubCi.expects_checks` fails closed (#1525's rule applied
        here: unknown must read as "checks were expected")."""
        class _FakeResult:
            returncode = 0
            stdout = json.dumps({"unexpected": "shape"})
            stderr = ""

        with patch("coord.github_ops.subprocess.run", return_value=_FakeResult()):
            with pytest.raises(RuntimeError):
                github_ops.get_repo_workflow_count("acme/api")

    def test_gh_missing_raises(self) -> None:
        with patch("coord.github_ops.subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(RuntimeError):
                github_ops.get_repo_workflow_count("acme/api")


class TestGetPrChecks:
    """#1483: the single gh sink for coord.ci_github.GitHubCi, the CI backend
    behind the merge gate."""

    def test_returns_checks_on_success(self) -> None:
        payload = json.dumps([{"name": "test", "state": "COMPLETED", "conclusion": "SUCCESS"}])

        class _FakeResult:
            returncode = 0
            stdout = payload
            stderr = ""

        with patch("coord.github_ops.subprocess.run", return_value=_FakeResult()):
            checks = github_ops.get_pr_checks("acme/api", 42)
        assert checks == json.loads(payload)

    def test_nonzero_exit_with_valid_json_still_returns_checks(self) -> None:
        """`gh pr checks` exits non-zero when a check failed, but stdout is
        still valid JSON in that case."""
        payload = json.dumps([{"name": "test", "state": "COMPLETED", "conclusion": "FAILURE"}])

        class _FakeResult:
            returncode = 1
            stdout = payload
            stderr = ""

        with patch("coord.github_ops.subprocess.run", return_value=_FakeResult()):
            checks = github_ops.get_pr_checks("acme/api", 42)
        assert checks == json.loads(payload)

    def test_nonzero_exit_with_empty_stdout_raises(self) -> None:
        class _FakeResult:
            returncode = 1
            stdout = ""
            stderr = "authentication required"

        with patch("coord.github_ops.subprocess.run", return_value=_FakeResult()):
            with pytest.raises(RuntimeError):
                github_ops.get_pr_checks("acme/api", 42)

    def test_gh_missing_raises_filenotfounderror(self) -> None:
        """get_pr_checks does not go through `_gh` — its caller,
        coord.ci_github.GitHubCi._fetch, is the one that catches
        FileNotFoundError/TimeoutExpired directly (see test_ci_store.py)."""
        with patch("coord.github_ops.subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(FileNotFoundError):
                github_ops.get_pr_checks("acme/api", 42)

    def test_nonzero_length_malformed_stdout_raises_not_fails_open(self) -> None:
        """#1353/#1525: unlike most of this module's `_gh`-backed helpers
        (which now fail OPEN to a default on a malformed decode — see
        `_gh_json`/`_json_loads_or`), a genuinely malformed (non-empty)
        response here must still raise. `ci_github.GitHubCi._fetch` relies on
        catching that `ValueError` to turn it into a synthetic *failing*
        check — #1525's fix for a real incident where an unreadable CI
        status silently read as "no checks" and let a merge through past a
        real CI failure. Only truly empty stdout is a deliberate `[]`
        default (`gh`'s normal "no checks configured" response)."""

        class _FakeResult:
            returncode = 0
            stdout = "not json"
            stderr = ""

        with patch("coord.github_ops.subprocess.run", return_value=_FakeResult()):
            with pytest.raises(json.JSONDecodeError):
                github_ops.get_pr_checks("acme/api", 42)

    def test_requested_json_fields_omit_conclusion(self) -> None:
        """#1564: `conclusion` is not, and never has been, a valid `gh pr
        checks --json` field — requesting it makes `gh` exit 1 with empty
        stdout, which reads as a total lookup failure."""
        assert "conclusion" not in github_ops.PR_CHECKS_JSON_FIELDS

    def test_get_pr_checks_requests_the_pinned_field_list(self) -> None:
        with patch(
            "coord.github_ops.subprocess.run",
            return_value=type(
                "R", (), {"returncode": 0, "stdout": "[]", "stderr": ""}
            )(),
        ) as mock_run:
            github_ops.get_pr_checks("acme/api", 42)
        args = mock_run.call_args.args[0]
        json_flag_index = args.index("--json")
        requested = args[json_flag_index + 1].split(",")
        assert requested == list(github_ops.PR_CHECKS_JSON_FIELDS)

    def test_gh_too_old_for_json_flag_raises_distinct_error(self) -> None:
        """#1564 Addendum 2: dellserver's gh 2.45.0 doesn't support `--json`
        on `pr checks` at all — confirmed real-world shape: exit 1, empty
        stdout, stderr `unknown flag: --json`. This must raise a distinct,
        actionable error (not the generic `RuntimeError` used for auth/
        network failures) naming the required version floor and the host.
        """

        class _ChecksResult:
            returncode = 1
            stdout = ""
            stderr = (
                "unknown flag: --json\n\n"
                "Usage:  gh pr checks [<number> | <url> | <branch>] [flags]\n"
            )

        class _VersionResult:
            returncode = 0
            stdout = "gh version 2.45.0 (2024-01-01)\nhttps://github.com/cli/cli/releases/tag/v2.45.0\n"
            stderr = ""

        with patch(
            "coord.github_ops.subprocess.run",
            side_effect=[_ChecksResult(), _VersionResult()],
        ):
            with pytest.raises(github_ops.GhTooOldForJsonChecks) as exc_info:
                github_ops.get_pr_checks("acme/api", 42)
        message = str(exc_info.value)
        assert github_ops.GH_PR_CHECKS_JSON_MIN_VERSION in message
        assert "2.45.0" in message
        assert socket.gethostname() in message

    def test_gh_too_old_error_is_a_runtime_error(self) -> None:
        """Subclasses RuntimeError so any existing `except RuntimeError`
        call site keeps failing closed even if it doesn't know about this
        specific subclass yet."""
        assert issubclass(github_ops.GhTooOldForJsonChecks, RuntimeError)

    def test_gh_too_old_message_handles_unparseable_version_probe(self) -> None:
        """`gh --version` itself failing (missing/timeout/unparseable) must
        not blow up the error path — the message just says "unknown"."""

        class _ChecksResult:
            returncode = 1
            stdout = ""
            stderr = "unknown flag: --json"

        with patch(
            "coord.github_ops.subprocess.run",
            side_effect=[_ChecksResult(), FileNotFoundError],
        ):
            with pytest.raises(github_ops.GhTooOldForJsonChecks) as exc_info:
                github_ops.get_pr_checks("acme/api", 42)
        assert "unknown" in str(exc_info.value)

    def test_regular_unrecognised_field_error_stays_generic_runtime_error(self) -> None:
        """A newer gh that supports `--json` but rejects one field name
        (the original `conclusion` bug) is a different failure mode than
        "gh doesn't have `--json` at all" — must stay the plain
        `RuntimeError` this always was, not the too-old subclass."""

        class _ChecksResult:
            returncode = 1
            stdout = ""
            stderr = 'Unknown JSON field: "conclusion"\nAvailable fields:\n  bucket\n'

        with patch("coord.github_ops.subprocess.run", return_value=_ChecksResult()):
            with pytest.raises(RuntimeError) as exc_info:
                github_ops.get_pr_checks("acme/api", 42)
        assert not isinstance(exc_info.value, github_ops.GhTooOldForJsonChecks)


class TestRerunWorkflowRun:
    """#1851: the single gh sink for coord.ci_github.GitHubCi.rerun_for_pr."""

    def test_success_returns_true(self) -> None:
        class _FakeResult:
            returncode = 0
            stdout = ""
            stderr = ""

        with patch(
            "coord.github_ops.subprocess.run", return_value=_FakeResult()
        ) as mock_run:
            assert github_ops.rerun_workflow_run("acme/api", "12345") is True
        args = mock_run.call_args.args[0]
        assert args == ["gh", "run", "rerun", "12345", "--repo", "acme/api"]

    def test_nonzero_exit_returns_false(self) -> None:
        class _FakeResult:
            returncode = 1
            stdout = ""
            stderr = "run already in progress"

        with patch("coord.github_ops.subprocess.run", return_value=_FakeResult()):
            assert github_ops.rerun_workflow_run("acme/api", "12345") is False

    def test_gh_missing_returns_false(self) -> None:
        with patch("coord.github_ops.subprocess.run", side_effect=FileNotFoundError):
            assert github_ops.rerun_workflow_run("acme/api", "12345") is False

    def test_gh_timeout_returns_false(self) -> None:
        with patch(
            "coord.github_ops.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=30),
        ):
            assert github_ops.rerun_workflow_run("acme/api", "12345") is False


class TestRerunWorkflowRunFailed:
    """#2252: the single gh sink for coord.ci_github.GitHubCi.
    rerun_failed_for_pr — the narrower ``--failed`` sibling of
    ``rerun_workflow_run`` above."""

    def test_success_returns_true_and_passes_failed_flag(self) -> None:
        class _FakeResult:
            returncode = 0
            stdout = ""
            stderr = ""

        with patch(
            "coord.github_ops.subprocess.run", return_value=_FakeResult()
        ) as mock_run:
            assert github_ops.rerun_workflow_run_failed("acme/api", "12345") is True
        args = mock_run.call_args.args[0]
        assert args == [
            "gh", "run", "rerun", "12345", "--repo", "acme/api", "--failed",
        ]

    def test_nonzero_exit_returns_false(self) -> None:
        class _FakeResult:
            returncode = 1
            stdout = ""
            stderr = "run already in progress"

        with patch("coord.github_ops.subprocess.run", return_value=_FakeResult()):
            assert github_ops.rerun_workflow_run_failed("acme/api", "12345") is False

    def test_gh_missing_returns_false(self) -> None:
        with patch("coord.github_ops.subprocess.run", side_effect=FileNotFoundError):
            assert github_ops.rerun_workflow_run_failed("acme/api", "12345") is False

    def test_gh_timeout_returns_false(self) -> None:
        with patch(
            "coord.github_ops.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=30),
        ):
            assert github_ops.rerun_workflow_run_failed("acme/api", "12345") is False


class TestGetBranchCommitTimestamp:
    """#1851: the base-side half of the CI-staleness comparison."""

    def test_parses_committer_date(self) -> None:
        payload = json.dumps({
            "commit": {
                "sha": "abc123",
                "commit": {"committer": {"date": "2026-05-24T12:00:00Z"}},
            }
        })
        with patch("coord.github_ops._gh", return_value=payload):
            ts = github_ops.get_branch_commit_timestamp("acme/api", "main")
        assert isinstance(ts, float)
        import datetime as _dt
        assert _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).year == 2026

    def test_returns_none_on_gh_error(self) -> None:
        with patch("coord.github_ops._gh", side_effect=RuntimeError("gh boom")):
            assert github_ops.get_branch_commit_timestamp("acme/api", "main") is None

    def test_returns_none_on_malformed_response(self) -> None:
        with patch("coord.github_ops._gh", return_value="not json"):
            assert github_ops.get_branch_commit_timestamp("acme/api", "main") is None

    def test_returns_none_on_missing_fields(self) -> None:
        with patch("coord.github_ops._gh", return_value=json.dumps({"commit": {}})):
            assert github_ops.get_branch_commit_timestamp("acme/api", "main") is None


class TestPrChecksJsonFieldsAreValid:
    """#1564 regression: `coord.github_ops.PR_CHECKS_JSON_FIELDS` must stay a
    subset of what the installed `gh` actually advertises via
    `gh pr checks --help`'s "JSON FIELDS" line — this is what would have
    caught `conclusion` (never a valid field) before it shipped, and catches
    the next `gh` schema change as a test failure instead of a silently
    broken merge gate.
    """

    def test_requested_fields_are_advertised_by_gh(self) -> None:
        gh = shutil.which("gh")
        if gh is None:
            pytest.skip("gh not installed in this environment")
        result = subprocess.run(
            ["gh", "pr", "checks", "--help"],
            capture_output=True, text=True, timeout=10,
        )
        help_text = f"{result.stdout}\n{result.stderr}"
        match = re.search(r"JSON FIELDS\s*\n\s*(.+)", help_text)
        assert match, f"could not find a JSON FIELDS section in gh help:\n{help_text}"
        advertised = {f.strip() for f in match.group(1).split(",")}
        requested = set(github_ops.PR_CHECKS_JSON_FIELDS)
        missing = requested - advertised
        assert not missing, (
            f"{sorted(missing)} requested by github_ops.PR_CHECKS_JSON_FIELDS "
            f"but not advertised by this gh's `--json` help ({sorted(advertised)})"
        )


class TestDiffPureRenames:
    """#2896 review: coord.review's sealed-tamper carve-out needs to tell a
    byte-identical `git mv` apart from an ordinary two-sided edit — this is
    where that fact is recovered from the diff text itself."""

    def test_pure_rename_is_detected(self) -> None:
        diff = (
            "diff --git a/tests/acceptance/ms-65/foo.rs b/tui/tests/acceptance/ms-65/foo.rs\n"
            "similarity index 100%\n"
            "rename from tests/acceptance/ms-65/foo.rs\n"
            "rename to tui/tests/acceptance/ms-65/foo.rs\n"
        )
        assert github_ops.diff_pure_renames(diff) == [
            ("tests/acceptance/ms-65/foo.rs", "tui/tests/acceptance/ms-65/foo.rs"),
        ]

    def test_partial_similarity_rename_is_not_a_pure_rename(self) -> None:
        """Content changed too (similarity < 100%) — must NOT be treated as
        a no-op move; the tamper check still needs to see this file."""
        diff = (
            "diff --git a/tests/acceptance/ms-65/foo.rs b/tui/tests/acceptance/ms-65/foo.rs\n"
            "similarity index 87%\n"
            "rename from tests/acceptance/ms-65/foo.rs\n"
            "rename to tui/tests/acceptance/ms-65/foo.rs\n"
            "--- a/tests/acceptance/ms-65/foo.rs\n"
            "+++ b/tui/tests/acceptance/ms-65/foo.rs\n"
            "@@ -1,1 +1,1 @@\n"
            "-cheated\n"
            "+still cheated\n"
        )
        assert github_ops.diff_pure_renames(diff) == []

    def test_in_place_content_edit_is_not_a_rename(self) -> None:
        """Same path both sides, no `rename from`/`rename to` lines at all —
        an ordinary edit, however textually similar old and new content."""
        diff = (
            "diff --git a/tui/tests/acceptance.rs b/tui/tests/acceptance.rs\n"
            "--- a/tui/tests/acceptance.rs\n"
            "+++ b/tui/tests/acceptance.rs\n"
            "@@ -1,1 +1,1 @@\n"
            "-include!(\"../../tests/acceptance/ms-33/audit_1039.rs\");\n"
            "+include!(\"acceptance/ms-33/audit_1039.rs\");\n"
        )
        assert github_ops.diff_pure_renames(diff) == []

    def test_multiple_renames_in_one_diff(self) -> None:
        diff = (
            "diff --git a/a.rs b/tui/a.rs\n"
            "similarity index 100%\n"
            "rename from a.rs\n"
            "rename to tui/a.rs\n"
            "diff --git a/b.rs b/tui/b.rs\n"
            "similarity index 100%\n"
            "rename from b.rs\n"
            "rename to tui/b.rs\n"
        )
        assert github_ops.diff_pure_renames(diff) == [
            ("a.rs", "tui/a.rs"), ("b.rs", "tui/b.rs"),
        ]

    def test_no_renames_in_a_plain_diff(self) -> None:
        diff = "diff --git a/src/foo.py b/src/foo.py\n"
        assert github_ops.diff_pure_renames(diff) == []


class TestGetRepoMilestonesWithCounts:
    """#3072: the roster projection — milestones plus GitHub's own open/closed
    issue counters, backing ``GET /api/milestones`` on ``coord web``."""

    def test_parses_the_jq_output_and_keeps_githubs_counts(self) -> None:
        jq_output = (
            '{"number": 4, "title": "ms-4", "state": "open", '
            '"open_issues": 3, "closed_issues": 1, "description": ""}\n'
            '{"number": 9, "title": "ms-9", "state": "closed", '
            '"open_issues": 0, "closed_issues": 7, "description": ""}\n'
        )
        fake_result = MagicMock(returncode=0, stdout=jq_output, stderr="")
        with patch("subprocess.run", return_value=fake_result) as mock_run:
            results = github_ops.get_repo_milestones_with_counts("acme/api")

        assert results == [
            {"number": 4, "title": "ms-4", "state": "open",
             "open_issues": 3, "closed_issues": 1, "description": ""},
            {"number": 9, "title": "ms-9", "state": "closed",
             "open_issues": 0, "closed_issues": 7, "description": ""},
        ]
        args = mock_run.call_args[0][0]
        assert "repos/acme/api/milestones?state=open" in " ".join(args)
        assert args[args.index("--jq") + 1] == github_ops.MILESTONE_COUNTS_JQ

    def test_returns_the_superset_get_repo_milestones_returns(self) -> None:
        """`coord.plans.aggregate_repo_plans` takes this list directly, so
        every key `get_repo_milestones` promises must still be present —
        otherwise the roster silently aggregates against a different shape
        than `coord plans` does."""
        jq_output = (
            '{"number": 4, "title": "ms-4", "state": "open", '
            '"open_issues": 3, "closed_issues": 1, "description": ""}\n'
        )
        fake_result = MagicMock(returncode=0, stdout=jq_output, stderr="")
        with patch("subprocess.run", return_value=fake_result):
            (row,) = github_ops.get_repo_milestones_with_counts("acme/api")

        assert {"number", "title"} <= set(row)

    def test_skips_a_single_malformed_line(self) -> None:
        """#1353's rule, inherited from `get_repo_milestones`: one bad line
        must not discard every well-formed milestone alongside it."""
        jq_output = (
            '{"number": 4, "title": "ms-4", "state": "open", '
            '"open_issues": 3, "closed_issues": 1, "description": ""}\n'
            'not json at all\n'
        )
        fake_result = MagicMock(returncode=0, stdout=jq_output, stderr="")
        with patch("subprocess.run", return_value=fake_result):
            results = github_ops.get_repo_milestones_with_counts("acme/api")

        assert [r["number"] for r in results] == [4]

    def test_no_milestones_is_an_empty_list_not_an_error(self) -> None:
        fake_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=fake_result):
            assert github_ops.get_repo_milestones_with_counts("acme/api") == []

    def test_forwards_state_query_param(self) -> None:
        fake_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=fake_result) as mock_run:
            github_ops.get_repo_milestones_with_counts("acme/api", state="all")

        assert "state=all" in " ".join(mock_run.call_args[0][0])

    def test_jq_filter_is_valid_jq_syntax(self) -> None:
        """The #967 guard, for this filter: `.[].{...}` (no pipe) is invalid
        jq and fails the whole call end to end — a class of bug every
        mocked-`subprocess.run` test above is blind to. Runs the ACTUAL
        pinned filter through a real jq engine.
        """
        jq = pytest.importorskip("jq")

        sample = [
            {"number": 4, "title": "ms-4", "state": "open", "open_issues": 3,
             "closed_issues": 1, "description": "d", "html_url": "ignored"},
        ]
        result = jq.compile(github_ops.MILESTONE_COUNTS_JQ).input_value(sample).all()
        assert result == [
            {"number": 4, "title": "ms-4", "state": "open",
             "open_issues": 3, "closed_issues": 1, "description": "d"},
        ]


class TestDrCredentialProbes:
    """#3129: the two `coord dr promote` capability probes, whose argv moved
    into this module so the #1902/#2135 chokepoint invariant still holds.

    `tests/test_dr_promote.py` covers the *verdicts* these produce, end to end
    against a real `gh` shim on ``$PATH``. What that lane cannot see is the
    argv itself, which is exactly what moving it here put at risk — so these
    pin the two shapes, and pin that a refusal comes back as a value rather
    than an exception (this path grades a refusal; it must not raise on one).
    """

    def test_push_probe_argv_includes_the_jq_filter(self) -> None:
        """`--jq .permissions.push` is load-bearing: without it `gh` returns
        the whole repo object and the caller's true/false parse fails."""
        seen: list[list[str]] = []

        def run(argv):
            seen.append(list(argv))
            return 0, "true\n"

        code, out = github_ops.probe_repo_push_permission("acme/api", run=run)

        assert seen == [["gh", "api", "repos/acme/api", "--jq",
                         ".permissions.push"]]
        assert (code, out) == (0, "true\n")

    def test_issues_probe_argv_asks_for_one_issue(self) -> None:
        seen: list[list[str]] = []

        def run(argv):
            seen.append(list(argv))
            return 0, "[]\n"

        code, out = github_ops.probe_issues_readable("acme/api", run=run)

        assert seen == [["gh", "api", "repos/acme/api/issues?per_page=1"]]
        assert (code, out) == (0, "[]\n")

    @pytest.mark.parametrize(
        "probe",
        [github_ops.probe_repo_push_permission, github_ops.probe_issues_readable],
        ids=["push", "issues"],
    )
    def test_a_refusal_is_returned_not_raised(self, probe) -> None:
        """The failing verdict has to be reachable: `dr promote` renders
        "this token cannot merge here" as a blocker, which it can only do if
        the non-zero exit arrives as a return value."""
        code, out = probe(
            "acme/api", run=lambda argv: (1, "gh: HTTP 401 Bad credentials")
        )

        assert code == 1
        assert "401" in out

    @pytest.mark.parametrize(
        "probe",
        [github_ops.probe_repo_push_permission, github_ops.probe_issues_readable],
        ids=["push", "issues"],
    )
    def test_probes_do_not_touch_the_throttle_or_telemetry_seams(
        self, probe, monkeypatch
    ) -> None:
        """They run on a host whose store is mid-restore, so they must not
        write `github_throttle`/`forge_availability` state — which is the
        whole reason they take a runner instead of calling `_gh`."""
        def boom(*args, **kwargs):  # pragma: no cover - must not be reached
            raise AssertionError("DR probe reached the _gh backoff/telemetry seam")

        monkeypatch.setattr(github_ops, "_gh", boom)
        monkeypatch.setattr(github_ops.github_throttle, "consult", boom)
        monkeypatch.setattr(github_ops, "record_gh_call", boom)

        assert probe("acme/api", run=lambda argv: (0, "ok"))[0] == 0
