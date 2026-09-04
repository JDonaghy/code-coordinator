"""Tests for issue claim detection (coord/claim.py)."""

from __future__ import annotations

import pytest

from coord.claim import (
    Claim,
    claim_message,
    claim_remedy_hint,
    find_work_claim,
    has_active_branch_followup,
    has_active_followup,
    has_active_work_followup,
    superseding_work_row,
)
from coord.models import Assignment, Board


# ── Helpers ────────────────────────────────────────────────────────────────


def _active(
    *,
    issue: int,
    repo: str = "api",
    machine: str = "laptop",
    branch: str | None = "issue-{n}-fix",
    aid: str = "abc",
    type_: str = "work",
    review_of: str | None = None,
) -> Assignment:
    return Assignment(
        machine_name=machine,
        repo_name=repo,
        issue_number=issue,
        issue_title="test",
        status="running",
        branch=(branch.replace("{n}", str(issue)) if branch else None),
        assignment_id=aid,
        type=type_,
        review_of_assignment_id=review_of,
    )


# ── find_work_claim ─────────────────────────────────────────────────────────


def test_no_claim_returns_none_when_board_empty_and_no_remote_branch() -> None:
    board = Board()
    claim = find_work_claim(
        16, "api", "acme/api", board, branch_lookup=lambda repo, n: []
    )
    assert claim is None


def test_board_claim_detected_when_active_assignment_matches() -> None:
    board = Board(active=[_active(issue=16, machine="server", aid="srv-1")])
    claim = find_work_claim(
        16, "api", "acme/api", board, branch_lookup=lambda repo, n: []
    )
    assert claim is not None
    assert claim.source == "board"
    assert claim.machine_name == "server"
    assert claim.assignment_id == "srv-1"
    assert claim.branch == "issue-16-fix"


def test_board_claim_ignores_other_repo() -> None:
    board = Board(active=[_active(issue=16, repo="other")])
    claim = find_work_claim(
        16, "api", "acme/api", board, branch_lookup=lambda repo, n: []
    )
    assert claim is None


def test_board_claim_ignores_other_issue() -> None:
    board = Board(active=[_active(issue=99)])
    claim = find_work_claim(
        16, "api", "acme/api", board, branch_lookup=lambda repo, n: []
    )
    assert claim is None


def test_remote_branch_claim_detected_when_board_clean() -> None:
    board = Board()
    claim = find_work_claim(
        16, "api", "acme/api", board,
        branch_lookup=lambda repo, n: ["issue-16-add-thing"],
    )
    assert claim is not None
    assert claim.source == "remote_branch"
    assert claim.branch == "issue-16-add-thing"


def test_board_claim_takes_priority_over_remote() -> None:
    """Board claim is cheaper to detect and more specific — return it first."""
    board = Board(active=[_active(issue=16, machine="server")])
    claim = find_work_claim(
        16, "api", "acme/api", board,
        branch_lookup=lambda repo, n: ["issue-16-something"],
    )
    assert claim is not None
    assert claim.source == "board"


def test_branch_lookup_receives_repo_github_and_issue_number() -> None:
    seen: list[tuple[str, int]] = []

    def lookup(repo, n):
        seen.append((repo, n))
        return []

    find_work_claim(42, "api", "acme/api", Board(), branch_lookup=lookup)
    assert seen == [("acme/api", 42)]


# ── claim_message ───────────────────────────────────────────────────────────


def test_claim_message_for_board_includes_machine_and_branch() -> None:
    msg = claim_message(Claim(
        issue_number=16, repo_name="api", source="board",
        machine_name="server", assignment_id="srv-1", branch="issue-16-fix",
    ))
    assert "#16" in msg
    assert "api" in msg
    assert "server" in msg
    assert "srv-1" in msg
    assert "issue-16-fix" in msg


def test_claim_message_for_board_handles_missing_fields() -> None:
    msg = claim_message(Claim(
        issue_number=16, repo_name="api", source="board",
        machine_name=None, assignment_id=None, branch=None,
    ))
    # Doesn't crash, doesn't include "by None" or empty parens
    assert "#16" in msg
    assert "None" not in msg
    assert "()" not in msg


def test_claim_message_for_remote_branch() -> None:
    msg = claim_message(Claim(
        issue_number=16, repo_name="api", source="remote_branch",
        branch="issue-16-foo",
    ))
    assert "#16" in msg
    assert "remote branch" in msg
    assert "issue-16-foo" in msg


# ── claim_remedy_hint (#3103) ────────────────────────────────────────────────
# A board claim (dead session) and a remote_branch claim (leftover branch) need
# genuinely different remedies — `coord diagnose` can only clear the former.


def test_claim_remedy_hint_for_board_names_diagnose() -> None:
    hint = claim_remedy_hint(
        Claim(issue_number=100, repo_name="api", source="board", branch="issue-100-x"),
        "api", 100,
    )
    assert "coord diagnose api 100" in hint


def test_claim_remedy_hint_for_remote_branch_names_branch_delete_not_diagnose() -> None:
    hint = claim_remedy_hint(
        Claim(
            issue_number=122, repo_name="coord-portal", source="remote_branch",
            branch="issue-122-gate-a-amend-portal-epic-client-identity",
        ),
        "coord-portal", 122,
    )
    assert "git push origin --delete issue-122-gate-a-amend-portal-epic-client-identity" in hint
    # #3103: naming the wrong remedy is worse than naming none — `coord
    # diagnose` inspects board stages and cannot clear a remote_branch claim,
    # so it must not be offered here at all.
    assert "coord diagnose" not in hint


# ── has_active_followup ─────────────────────────────────────────────────────


def test_has_active_followup_finds_in_flight_review() -> None:
    board = Board(active=[
        _active(issue=16, type_="review", review_of="work-1", aid="rev-1"),
    ])
    assert has_active_followup(
        board, of_assignment_id="work-1", assignment_type="review"
    )


def test_has_active_followup_distinguishes_type() -> None:
    """A review in flight should NOT block a smoke dispatch."""
    board = Board(active=[
        _active(issue=16, type_="review", review_of="work-1"),
    ])
    assert not has_active_followup(
        board, of_assignment_id="work-1", assignment_type="smoke"
    )


def test_has_active_followup_distinguishes_target_assignment() -> None:
    """A review of one work assignment shouldn't block reviews of another."""
    board = Board(active=[
        _active(issue=16, type_="review", review_of="work-1"),
    ])
    assert not has_active_followup(
        board, of_assignment_id="work-2", assignment_type="review"
    )


def test_has_active_followup_returns_false_for_none_target() -> None:
    """No work assignment ID → can't dedupe; allow the dispatch."""
    board = Board(active=[_active(issue=16, type_="review", review_of=None)])
    assert not has_active_followup(
        board, of_assignment_id=None, assignment_type="review"
    )


# ── Integration: dispatch_review / dispatch_smoke respect the dedupe ────────


def test_dispatch_review_skipped_when_followup_already_active() -> None:
    """When a review for the same work assignment is in flight, skip."""
    from coord.config import Config, ReviewsConfig
    from coord.models import Machine, Repo
    from coord.review import dispatch_review

    repo = Repo(name="api", github="acme/api", depends_on=[], default_branch="main")
    cfg = Config(
        repos=[repo],
        machines=[
            Machine(name="laptop", host="laptop.tail", repos=["api"],
                    repo_paths={"api": "/w"}, capabilities=[]),
            Machine(name="server", host="server.tail", repos=["api"],
                    repo_paths={"api": "/s"}, capabilities=[]),
        ],
        reviews=ReviewsConfig(enabled=True, auto_dispatch=True),
    )

    completed = Assignment(
        machine_name="laptop", repo_name="api", issue_number=16,
        issue_title="X", status="done", branch="issue-16-fix",
        assignment_id="work-1", type="work",
    )
    existing_review = _active(
        issue=16, type_="review", review_of="work-1", aid="rev-existing"
    )
    board = Board(active=[existing_review])

    class _Client:
        def __init__(self):
            self.calls = 0

        def post(self, url, *, json, timeout):
            self.calls += 1
            raise AssertionError("should not be called when deduped")

    client = _Client()
    result = dispatch_review(
        completed, board, cfg,
        http_client=client,
        pr_lookup=lambda repo_github, **kw: {"number": 1, "url": "u", "existed": True},
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
    )
    assert result is None
    assert client.calls == 0


def test_dispatch_smoke_skipped_when_followup_already_active() -> None:
    from coord.config import Config, SmokeRule, SmokeTestsConfig
    from coord.models import Machine, Repo
    from coord.smoke import dispatch_smoke

    repo = Repo(name="api", github="acme/api", depends_on=[], default_branch="main")
    cfg = Config(
        repos=[repo],
        machines=[
            Machine(name="laptop", host="laptop.tail", repos=["api"],
                    repo_paths={"api": "/w"}, capabilities=["gtk"]),
        ],
        smoke_tests=SmokeTestsConfig(
            auto_queue=True,
            capability_rules=[SmokeRule(files=["src/"], requires=["gtk"])],
        ),
    )

    completed = Assignment(
        machine_name="laptop", repo_name="api", issue_number=16,
        issue_title="X", status="done", branch="issue-16-fix",
        assignment_id="work-1", type="work",
    )
    existing_smoke = _active(
        issue=16, type_="smoke", review_of="work-1", aid="smoke-existing"
    )
    board = Board(active=[existing_smoke])

    class _Client:
        def post(self, url, *, json, timeout):
            raise AssertionError("should not be called when deduped")

    result = dispatch_smoke(
        completed, board, cfg,
        http_client=_Client(),
        diff_lookup=lambda repo, branch: ["src/main.c"],
    )
    assert result is None


# ── Claim filtering by status and type ────────────────────────────────────


def test_failed_assignment_does_not_block_claim() -> None:
    failed = _active(issue=42, aid="old-fail")
    failed.status = "failed"
    board = Board(active=[failed])
    claim = find_work_claim(42, "api", "acme/api", board, branch_lookup=lambda *a: [])
    assert claim is None


def test_plan_assignment_does_not_block_claim() -> None:
    plan = _active(issue=42, type_="plan", aid="plan-1")
    board = Board(active=[plan])
    claim = find_work_claim(42, "api", "acme/api", board, branch_lookup=lambda *a: [])
    assert claim is None


def test_review_assignment_does_not_block_claim() -> None:
    review = _active(issue=42, type_="review", aid="rev-1", review_of="work-1")
    board = Board(active=[review])
    claim = find_work_claim(42, "api", "acme/api", board, branch_lookup=lambda *a: [])
    assert claim is None


def test_smoke_assignment_does_not_block_claim() -> None:
    smoke = _active(issue=42, type_="smoke", aid="smoke-1", review_of="work-1")
    board = Board(active=[smoke])
    claim = find_work_claim(42, "api", "acme/api", board, branch_lookup=lambda *a: [])
    assert claim is None


def test_chat_assignment_does_not_block_claim() -> None:
    """#1059: a "Chat about issue" session (#628) is read-only/diagnostic —
    a stale one left on an issue must not permanently wedge a real dispatch
    (reproduced against issue #1041's dangling Gate-A claim)."""
    chat = _active(issue=42, type_="chat", aid="chat-1")
    board = Board(active=[chat])
    claim = find_work_claim(42, "api", "acme/api", board, branch_lookup=lambda *a: [])
    assert claim is None


def test_troubleshoot_assignment_does_not_block_claim() -> None:
    """#1059: "troubleshoot" is the legacy predecessor of "chat" (#628) —
    same read-only/advisory-only treatment in coord/issue_store.py, same
    exemption here."""
    troubleshoot = _active(issue=42, type_="troubleshoot", aid="ts-1")
    board = Board(active=[troubleshoot])
    claim = find_work_claim(42, "api", "acme/api", board, branch_lookup=lambda *a: [])
    assert claim is None


def test_running_work_assignment_still_blocks_claim() -> None:
    work = _active(issue=42, type_="work", aid="work-1")
    board = Board(active=[work])
    claim = find_work_claim(42, "api", "acme/api", board, branch_lookup=lambda *a: [])
    assert claim is not None
    assert claim.source == "board"


# ── has_active_work_followup (#459) ─────────────────────────────────────────


def test_has_active_work_followup_detects_running_work() -> None:
    """A running work assignment for the same issue blocks review dispatch."""
    board = Board(active=[_active(issue=16, repo="api", type_="work", aid="work-2")])
    assert has_active_work_followup(board, repo_name="api", issue_number=16)


def test_has_active_work_followup_detects_conflict_fix() -> None:
    """A running conflict-fix for the same issue also blocks review dispatch."""
    board = Board(active=[_active(issue=16, repo="api", type_="conflict-fix", aid="cf-1")])
    assert has_active_work_followup(board, repo_name="api", issue_number=16)


def test_has_active_work_followup_ignores_other_issue() -> None:
    board = Board(active=[_active(issue=99, repo="api", type_="work", aid="work-x")])
    assert not has_active_work_followup(board, repo_name="api", issue_number=16)


def test_has_active_work_followup_ignores_other_repo() -> None:
    board = Board(active=[_active(issue=16, repo="other", type_="work", aid="work-y")])
    assert not has_active_work_followup(board, repo_name="api", issue_number=16)


def test_has_active_work_followup_ignores_review_type() -> None:
    """An active review should not trigger the work-followup guard."""
    board = Board(active=[
        _active(issue=16, repo="api", type_="review", review_of="work-1", aid="rev-1"),
    ])
    assert not has_active_work_followup(board, repo_name="api", issue_number=16)


def test_has_active_work_followup_ignores_failed_work() -> None:
    """A failed work assignment is not 'active' — should not block."""
    failed = _active(issue=16, repo="api", type_="work", aid="work-bad")
    failed.status = "failed"
    board = Board(active=[failed])
    assert not has_active_work_followup(board, repo_name="api", issue_number=16)


def test_has_active_work_followup_returns_false_for_empty_board() -> None:
    assert not has_active_work_followup(Board(), repo_name="api", issue_number=16)


# ── Integration: dispatch_review respects the work-followup guard (#459) ────


def test_dispatch_review_skipped_when_active_work_rewriting_branch() -> None:
    """dispatch_review returns None when a work assignment is actively running
    for the same issue, even if the completed assignment has no review yet."""
    from coord.config import Config, ReviewsConfig
    from coord.models import Machine, Repo
    from coord.review import dispatch_review

    repo = Repo(name="api", github="acme/api", depends_on=[], default_branch="main")
    cfg = Config(
        repos=[repo],
        machines=[
            Machine(name="laptop", host="laptop.tail", repos=["api"],
                    repo_paths={"api": "/w"}, capabilities=[]),
            Machine(name="server", host="server.tail", repos=["api"],
                    repo_paths={"api": "/s"}, capabilities=[]),
        ],
        reviews=ReviewsConfig(enabled=True, auto_dispatch=True),
    )

    completed = Assignment(
        machine_name="laptop", repo_name="api", issue_number=16,
        issue_title="X", status="done", branch="issue-16-fix",
        assignment_id="work-1", type="work",
    )
    # A coord-bounce fix (work type) is actively rewriting the branch.
    active_fix = _active(issue=16, repo="api", type_="work", aid="work-2")
    board = Board(active=[active_fix])

    class _Client:
        def post(self, url, *, json, timeout):
            raise AssertionError("should not POST a review while fix is live")

    result = dispatch_review(
        completed, board, cfg,
        http_client=_Client(),
        pr_lookup=lambda repo_github, **kw: {"number": 1, "url": "u", "existed": True},
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
    )
    assert result is None
    # #1627: the guard names itself on the assignment so a caller (the
    # `coord review` CLI, chiefly) can report why instead of guessing —
    # and points at the phantom-row remedy (`coord diagnose`) since a dead
    # fix worker leaving status=running behind looks identical to this.
    assert completed.review_dispatch_reason is not None
    assert "actively rewriting the branch" in completed.review_dispatch_reason
    assert "coord diagnose api 16" in completed.review_dispatch_reason


def test_dispatch_review_proceeds_when_no_active_work() -> None:
    """dispatch_review proceeds normally when there's no active work for the issue."""
    from unittest.mock import patch
    from coord.config import Config, ReviewsConfig
    from coord.models import Machine, Repo
    from coord.review import dispatch_review

    repo = Repo(name="api", github="acme/api", depends_on=[], default_branch="main")
    cfg = Config(
        repos=[repo],
        machines=[
            Machine(name="laptop", host="laptop.tail", repos=["api"],
                    repo_paths={"api": "/w"}, capabilities=[]),
            Machine(name="server", host="server.tail", repos=["api"],
                    repo_paths={"api": "/s"}, capabilities=[]),
        ],
        reviews=ReviewsConfig(enabled=True, auto_dispatch=True),
    )

    completed = Assignment(
        machine_name="laptop", repo_name="api", issue_number=16,
        issue_title="X", status="done", branch="issue-16-fix",
        assignment_id="work-1", type="work",
    )
    board = Board()  # No active assignments — review should proceed.

    posted: list[dict] = []

    class _Client:
        def post(self, url, *, json, timeout):
            posted.append(json)

            class _Resp:
                def raise_for_status(self):
                    pass
                def json(self):
                    return {"assignment_id": "rev-new"}
            return _Resp()

    result = dispatch_review(
        completed, board, cfg,
        http_client=_Client(),
        pr_lookup=lambda repo_github, **kw: {"number": 1, "url": "u", "existed": True},
        claude_md_reader=lambda p: None,
        issue_body_fetcher=lambda repo, num: "",
    )
    assert result is not None
    assert posted, "expected an HTTP POST to dispatch the review"


# ── merged-branch filter: stale merged branches don't block work ─────────────
# A fully-merged issue-N-* branch (e.g. a PR head not auto-deleted) must NOT be
# treated as an active claim — otherwise it blocks new work on the issue forever
# (the chat→work block on a long-merged branch).


def _gh_stub(default_branch: str, ahead_by: dict[str, int]):
    """github_ops._gh stub: serves the repo default branch and per-head compare
    `ahead_by` based on the API path (`_gh("api", "<path>")`)."""
    import json

    def _fake(*args, **kwargs):
        path = args[1] if len(args) > 1 else ""
        if "/compare/" in path:
            head = path.split("...", 1)[1]
            return json.dumps({"ahead_by": ahead_by.get(head, 1)})
        return json.dumps({"default_branch": default_branch})

    return _fake


def test_drop_merged_branches_drops_fully_merged(monkeypatch) -> None:
    import coord.claim as claim_mod

    monkeypatch.setattr("coord.github_ops._gh", _gh_stub("main", {"issue-9-done": 0}))
    assert claim_mod._drop_merged_branches("acme/api", ["issue-9-done"]) == []


def test_drop_merged_branches_keeps_unmerged(monkeypatch) -> None:
    import coord.claim as claim_mod

    monkeypatch.setattr("coord.github_ops._gh", _gh_stub("main", {"issue-9-live": 3}))
    assert claim_mod._drop_merged_branches("acme/api", ["issue-9-live"]) == [
        "issue-9-live"
    ]


def test_drop_merged_branches_keeps_on_compare_error(monkeypatch) -> None:
    import json

    import coord.claim as claim_mod

    def _fake(*a, **k):
        if "/compare/" in a[1]:
            raise RuntimeError("gh down")
        return json.dumps({"default_branch": "main"})

    monkeypatch.setattr("coord.github_ops._gh", _fake)
    assert claim_mod._drop_merged_branches("acme/api", ["issue-9-x"]) == ["issue-9-x"]


def test_drop_merged_branches_keeps_when_default_unknown(monkeypatch) -> None:
    import coord.claim as claim_mod

    def _boom(*a, **k):
        raise RuntimeError("gh down")

    monkeypatch.setattr("coord.github_ops._gh", _boom)
    assert claim_mod._drop_merged_branches("acme/api", ["issue-9-x"]) == ["issue-9-x"]


def test_drop_merged_branches_drops_squash_merged_branch(monkeypatch) -> None:
    """#3103: a squash merge lands the PR's content as a NEW commit on the
    default branch — the branch's own commits are never ancestors of it, so
    `ahead_by` stays nonzero forever even though the work fully landed. The
    branch must still be dropped once `pr_is_merged` (GitHub's own PR state,
    not commit ancestry) confirms the merge.
    """
    import coord.claim as claim_mod
    from coord import github_ops

    # Ancestry alone would keep this branch forever (ahead_by never 0 for a
    # squash merge) — assert the stub proves that, then assert pr_is_merged
    # overrides it.
    monkeypatch.setattr(
        "coord.github_ops._gh", _gh_stub("main", {"issue-122-amend": 3})
    )
    monkeypatch.setattr(github_ops, "pr_is_merged", lambda repo, b: True)
    assert claim_mod._drop_merged_branches("acme/portal", ["issue-122-amend"]) == []


def test_drop_merged_branches_keeps_branch_when_pr_open_and_ahead(monkeypatch) -> None:
    """No regression: an active branch with an open (or absent) PR, and still
    ahead of default, remains a claim."""
    import coord.claim as claim_mod
    from coord import github_ops

    monkeypatch.setattr(
        "coord.github_ops._gh", _gh_stub("main", {"issue-122-live": 2})
    )
    monkeypatch.setattr(github_ops, "pr_is_merged", lambda repo, b: False)
    assert claim_mod._drop_merged_branches("acme/portal", ["issue-122-live"]) == [
        "issue-122-live"
    ]


def test_find_work_claim_skips_squash_merged_remote_branch(monkeypatch) -> None:
    """End-to-end repro of #3103: a squash-merged Gate-A amend branch must not
    permanently claim the tracking issue just because it's still "ahead" by
    commit ancestry."""
    import json

    from coord import github_ops

    def _fake(*args, **kwargs):
        path = args[1]
        if "matching-refs" in path:
            return json.dumps(
                [{"ref": "refs/heads/issue-122-gate-a-amend-portal-epic"}]
            )
        if "/compare/" in path:
            return json.dumps({"ahead_by": 4})  # never 0 for a squash merge
        return json.dumps({"default_branch": "main"})

    monkeypatch.setattr("coord.github_ops._gh", _fake)
    monkeypatch.setattr(github_ops, "pr_is_merged", lambda repo, b: True)
    assert find_work_claim(122, "coord-portal", "acme/portal", Board()) is None


def test_find_work_claim_skips_merged_remote_branch(monkeypatch) -> None:
    """End-to-end: a fully-merged issue-N-* branch must NOT claim the issue."""
    import json

    def _fake(*args, **kwargs):
        path = args[1]
        if "matching-refs" in path:
            return json.dumps([{"ref": "refs/heads/issue-319-old"}])
        if "/compare/" in path:
            return json.dumps({"ahead_by": 0})  # fully merged
        return json.dumps({"default_branch": "main"})

    monkeypatch.setattr("coord.github_ops._gh", _fake)
    # No branch_lookup override → exercises _default_branch_lookup + the filter.
    assert find_work_claim(319, "api", "acme/api", Board()) is None


def test_find_work_claim_still_blocks_unmerged_remote_branch(monkeypatch) -> None:
    """An unmerged issue-N-* branch still claims the issue (no regression)."""
    import json

    def _fake(*args, **kwargs):
        path = args[1]
        if "matching-refs" in path:
            return json.dumps([{"ref": "refs/heads/issue-319-active"}])
        if "/compare/" in path:
            return json.dumps({"ahead_by": 2})  # unmerged work
        return json.dumps({"default_branch": "main"})

    monkeypatch.setattr("coord.github_ops._gh", _fake)
    claim = find_work_claim(319, "api", "acme/api", Board())
    assert claim is not None
    assert claim.branch == "issue-319-active"


# ── #1553: has_active_work_followup keys on the EFFECTIVE issue ─────────────


def test_has_active_work_followup_keys_on_for_issue_number() -> None:
    """A slice fix booked under a tracking issue guards the CHILD, not the epic.

    `coord acceptance author <repo> <tracking> --issue N` records
    `issue_number = <tracking>` (the whole milestone's slices share one
    branch/PR) and `for_issue_number = N`. The guard must follow the child.
    """
    fix = _active(issue=1120, repo="api", type_="work", aid="fix-1124")
    fix.for_issue_number = 1124
    board = Board(active=[fix])
    assert has_active_work_followup(board, repo_name="api", issue_number=1124)


def test_has_active_work_followup_does_not_block_the_tracking_issue() -> None:
    """A child's slice fix must not block review dispatch on the epic itself."""
    fix = _active(issue=1120, repo="api", type_="work", aid="fix-1124")
    fix.for_issue_number = 1124
    board = Board(active=[fix])
    assert not has_active_work_followup(board, repo_name="api", issue_number=1120)


def test_has_active_work_followup_does_not_block_a_sibling_child() -> None:
    """#1553's cross-child stall: two children's slices share one issue_number.

    Before the effective-issue keying, an in-flight fix for child #1124 made
    the guard fire for child #1125 too (both rows carry issue_number 1120),
    silently stalling review dispatch for an unrelated issue.
    """
    fix = _active(issue=1120, repo="api", type_="work", aid="fix-1124")
    fix.for_issue_number = 1124
    board = Board(active=[fix])
    assert not has_active_work_followup(board, repo_name="api", issue_number=1125)


def test_has_active_work_followup_unchanged_for_ordinary_work() -> None:
    """No `for_issue_number` → identical behaviour to before #1553."""
    board = Board(active=[_active(issue=16, repo="api", type_="work", aid="w")])
    assert has_active_work_followup(board, repo_name="api", issue_number=16)
    assert not has_active_work_followup(board, repo_name="api", issue_number=17)


# ── #1819: has_active_branch_followup / superseding_work_row ────────────────


def _work_row(
    *, aid: str, branch: str, at: float, repo: str = "api",
    status: str = "done", type_: str = "work",
) -> Assignment:
    return Assignment(
        machine_name="m", repo_name=repo, issue_number=16, issue_title="t",
        assignment_id=aid, type=type_, status=status, branch=branch,
        dispatched_at=at,
    )


def test_has_active_branch_followup_sees_a_sibling_rows_smoke() -> None:
    """#1819: the shape the row-keyed dedupe missed — one branch, two work
    rows (the fix-round shape), a smoke in flight for the OTHER row."""
    board = Board(active=[
        _active(issue=16, type_="smoke", review_of="work-1", branch="issue-16-fix"),
    ])
    # Row-keyed: "work-2 has no smoke" → waves a duplicate through.
    assert not has_active_followup(
        board, of_assignment_id="work-2", assignment_type="smoke"
    )
    # Branch-keyed: the branch is already being tested.
    assert has_active_branch_followup(
        board, repo_name="api", branch="issue-16-fix", assignment_type="smoke"
    )


def test_has_active_branch_followup_distinguishes_branch_repo_and_type() -> None:
    board = Board(active=[
        _active(issue=16, type_="smoke", review_of="work-1", branch="issue-16-fix"),
    ])
    assert not has_active_branch_followup(
        board, repo_name="api", branch="issue-99-other", assignment_type="smoke"
    )
    assert not has_active_branch_followup(
        board, repo_name="other-repo", branch="issue-16-fix", assignment_type="smoke"
    )
    assert not has_active_branch_followup(
        board, repo_name="api", branch="issue-16-fix", assignment_type="review"
    )


def test_has_active_branch_followup_false_for_no_branch() -> None:
    """A branchless row can't be deduped on a branch — allow the dispatch."""
    board = Board(active=[
        _active(issue=16, type_="smoke", review_of="work-1", branch="issue-16-fix"),
    ])
    assert not has_active_branch_followup(
        board, repo_name="api", branch=None, assignment_type="smoke"
    )


def test_superseding_work_row_finds_the_later_row_on_the_same_branch() -> None:
    old = _work_row(aid="w1", branch="issue-16-fix", at=100.0)
    new = _work_row(aid="w2", branch="issue-16-fix", at=200.0)
    board = Board(completed=[old, new])

    assert superseding_work_row(board, old) is new
    assert superseding_work_row(board, new) is None


def test_superseding_work_row_ignores_other_branches_and_repos() -> None:
    row = _work_row(aid="w1", branch="issue-16-fix", at=100.0)
    other_branch = _work_row(aid="w2", branch="issue-17-fix", at=200.0)
    other_repo = _work_row(aid="w3", branch="issue-16-fix", at=200.0, repo="tui")
    board = Board(completed=[row, other_branch, other_repo])

    assert superseding_work_row(board, row) is None


def test_superseding_work_row_ignores_failed_later_rows() -> None:
    """A failed fix round produced nothing — the earlier row still authored
    the branch."""
    old = _work_row(aid="w1", branch="issue-16-fix", at=100.0)
    failed = _work_row(aid="w2", branch="issue-16-fix", at=200.0, status="failed")
    board = Board(completed=[old, failed])

    assert superseding_work_row(board, old) is None


def test_superseding_work_row_ignores_advisory_later_rows() -> None:
    """A `--fix-of` round that lands zero commits ("already fixed", or a
    graceful usage-limit exit) is `status=advisory` — the real, documented
    terminal status for a work-like assignment with nothing pushed
    (`_ZERO_COMMIT_TYPES` in coord/agent.py). It must not supersede the row
    it was fixing: an advisory row is itself never a valid Test-stage
    dispatch target (`dispatch_smoke` requires `status == "done"`), so
    treating it as superseding would leave the branch with NO dispatch
    target at all."""
    old = _work_row(aid="w1", branch="issue-16-fix", at=100.0)
    advisory = _work_row(aid="w2", branch="issue-16-fix", at=200.0, status="advisory")
    board = Board(completed=[old, advisory])

    assert superseding_work_row(board, old) is None


def test_superseding_work_row_ignores_refused_policy_later_rows() -> None:
    """#2234: a `--fix-of` round whose worker exits 0-commit citing a
    standing repo-rule prohibition is `status=refused_policy` — drawn from
    the same `_ZERO_COMMIT_TYPES` gate as `advisory` in coord/agent.py's
    `_reap`. It must not supersede the row it was fixing, for the identical
    reason `advisory` doesn't: a `refused_policy` row is itself never a
    valid Test-stage dispatch target (`dispatch_smoke` requires `status ==
    "done"`), so treating it as superseding would leave the branch with NO
    dispatch target at all."""
    old = _work_row(aid="w1", branch="issue-16-fix", at=100.0)
    refused = _work_row(
        aid="w2", branch="issue-16-fix", at=200.0, status="refused_policy"
    )
    board = Board(completed=[old, refused])

    assert superseding_work_row(board, old) is None


def test_superseding_work_row_ignores_non_work_types() -> None:
    """The review and smoke of a branch are not its author."""
    row = _work_row(aid="w1", branch="issue-16-fix", at=100.0)
    review = _work_row(aid="r1", branch="issue-16-fix", at=200.0, type_="review")
    smoke = _work_row(aid="s1", branch="issue-16-fix", at=300.0, type_="smoke")
    board = Board(completed=[row], active=[review, smoke])

    assert superseding_work_row(board, row) is None


def test_superseding_work_row_breaks_dispatched_at_ties_deterministically() -> None:
    """Two rows stamped in the same second must still order stably, or BOTH
    would read as superseded (or neither) and the dedupe would flap."""
    a = _work_row(aid="aaa", branch="issue-16-fix", at=100.0)
    b = _work_row(aid="bbb", branch="issue-16-fix", at=100.0)
    board = Board(completed=[a, b])

    assert superseding_work_row(board, a) is b
    assert superseding_work_row(board, b) is None


def test_superseding_work_row_none_for_branchless_row() -> None:
    row = _work_row(aid="w1", branch="issue-16-fix", at=100.0)
    row.branch = None
    board = Board(completed=[row])

    assert superseding_work_row(board, row) is None
