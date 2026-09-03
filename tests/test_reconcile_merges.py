"""Tests for reconcile_board_merges: branch backfill (#611) + record merged (#609) + close stale PRs (#721) + prune stale queue (#732) + settle sibling ghosts (#894)."""

from __future__ import annotations

import time

import pytest

from coord import github_ops
from coord.config import Config
from coord.models import Assignment, Board, Repo
from coord.reconcile import close_stale_prs, reconcile_board_merges

# Captured at import time, before the autouse `_non_terminal_work` conftest
# fixture (tests/conftest.py) stubs `github_ops.work_is_terminal` to always
# return False for the duration of each test. The #2639 tests below restore
# this real reference so they exercise the actual issue_is_closed/
# pr_is_merged decision, not the blanket non-terminal default.
_REAL_WORK_IS_TERMINAL = github_ops.work_is_terminal


@pytest.fixture
def config() -> Config:
    return Config(
        repos=[Repo(name="api", github="acme/api", default_branch="main")],
        machines=[],
    )


def _done_work(
    *,
    assignment_id: str = "abc",
    issue_number: int = 42,
    branch: str | None = None,
    status: str = "done",
) -> Assignment:
    return Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=issue_number,
        issue_title="t",
        status=status,
        assignment_id=assignment_id,
        branch=branch,
        type="work",
    )


def _patch_probes(
    monkeypatch,
    *,
    remote_branches: set[str] | None = None,
    terminal: bool = False,
):
    """Stub the git/gh probes + record state writes; never hit the network.

    Also stubs ``list_open_prs`` to return an empty list so the stale-PR
    sweep (#721) does not fire for tests that only care about the earlier
    reconcile sweeps (branch backfill, record-merged).

    Now also stubs ``mark_sibling_review_done`` and ``mark_advisory_settled``
    (#894) so sweep (e) never touches the DB in these tests.
    """
    from coord import github_ops, state

    monkeypatch.setattr(
        github_ops, "list_remote_branch_names",
        lambda repo: set(remote_branches or set()),
    )
    monkeypatch.setattr(
        github_ops, "work_is_terminal",
        lambda repo, issue, branch, cache=None, trust_issue_closed=True: terminal,
    )
    # Suppress the stale-PR sweep for tests that don't need it.
    monkeypatch.setattr(github_ops, "list_open_prs", lambda repo: [])

    writes: list[tuple[str, str]] = []
    monkeypatch.setattr(
        state, "update_assignment_branch",
        lambda aid, branch: writes.append(("branch", aid)),
    )
    monkeypatch.setattr(
        state, "mark_assignment_merged",
        lambda aid: writes.append(("merged", aid)),
    )
    # #894: stub sibling-settling functions so sweep (e) never touches the DB.
    monkeypatch.setattr(
        state, "mark_sibling_review_done",
        lambda aid: writes.append(("sibling_review_done", aid)),
    )
    monkeypatch.setattr(
        state, "mark_advisory_settled",
        lambda aid: writes.append(("advisory_settled", aid)),
    )
    # #2234: stub the refused_policy sibling-settling function the same way.
    monkeypatch.setattr(
        state, "mark_refused_policy_settled",
        lambda aid: writes.append(("refused_policy_settled", aid)),
    )
    # #951: stub the type=work review_state settle so sweep (b) never touches
    # the DB in these tests.
    monkeypatch.setattr(
        state, "mark_work_review_settled",
        lambda aid: writes.append(("work_review_settled", aid)),
    )
    # #1180: stub the wedged test-author/mock-author review_state repair so
    # sweep (f) never touches the DB in these tests.
    monkeypatch.setattr(
        state, "reset_wedged_test_author_review",
        lambda aid: writes.append(("wedged_review_reset", aid)),
    )
    return writes


# ── #611 branch backfill ──────────────────────────────────────────────────


def test_backfills_branch_from_single_matching_remote(monkeypatch, config) -> None:
    a = _done_work(issue_number=42, branch=None)
    board = Board(completed=[a])
    writes = _patch_probes(
        monkeypatch, remote_branches={"issue-42-fix", "main"}, terminal=False
    )

    actions = reconcile_board_merges(board, config)

    assert a.branch == "issue-42-fix"
    assert ("branch", "abc") in writes
    assert any("backfill branch" in s for s in actions)


def test_no_backfill_when_branch_candidate_ambiguous(monkeypatch, config) -> None:
    a = _done_work(issue_number=42, branch=None)
    board = Board(completed=[a])
    writes = _patch_probes(
        monkeypatch,
        remote_branches={"issue-42-fix", "issue-42-other"},
        terminal=False,
    )

    actions = reconcile_board_merges(board, config)

    assert a.branch is None
    assert ("branch", "abc") not in writes
    assert any("ambiguous" in s for s in actions)


def test_no_backfill_when_no_matching_remote(monkeypatch, config) -> None:
    a = _done_work(issue_number=42, branch=None)
    board = Board(completed=[a])
    writes = _patch_probes(monkeypatch, remote_branches={"main"}, terminal=False)

    actions = reconcile_board_merges(board, config)

    assert a.branch is None
    assert writes == []
    assert any("no remote branch" in s for s in actions)


# ── #609 record out-of-band merges ─────────────────────────────────────────


def test_marks_merged_when_branch_is_terminal(monkeypatch, config) -> None:
    a = _done_work(issue_number=42, branch="issue-42-fix")
    board = Board(completed=[a])
    writes = _patch_probes(monkeypatch, terminal=True)

    actions = reconcile_board_merges(board, config)

    assert a.status == "merged"
    assert ("merged", "abc") in writes
    assert any("mark merged" in s for s in actions)


def test_does_not_mark_merged_when_not_terminal(monkeypatch, config) -> None:
    a = _done_work(issue_number=42, branch="issue-42-fix")
    board = Board(completed=[a])
    writes = _patch_probes(monkeypatch, terminal=False)

    actions = reconcile_board_merges(board, config)

    assert a.status == "done"
    assert writes == []


def test_reused_branch_with_new_commits_not_marked_merged(monkeypatch, config) -> None:
    """#1150 end-to-end: exercises the REAL ``work_is_terminal`` ->
    ``pr_is_merged`` chain (only ``_gh``/``issue_is_closed`` are stubbed, not
    ``work_is_terminal`` itself) for a branch that has a historical merged PR
    *plus* new, unmerged commits pushed on top of it (the ``--fix-of``/
    ``--force`` branch-reuse pattern). Sweep (b) must NOT flip this row to
    'merged' — the branch's current tip does not match what actually merged.
    """
    from coord import github_ops, state

    a = _done_work(issue_number=42, branch="issue-42-fix")
    board = Board(completed=[a])

    monkeypatch.setattr(github_ops, "issue_is_closed", lambda repo, n: False)
    monkeypatch.setattr(github_ops, "list_open_prs", lambda repo: [])

    def _gh_stub(*args, **kwargs):
        if args and args[0] == "pr":
            import json as _json
            return _json.dumps([
                {"number": 7, "state": "MERGED", "mergedAt": "2026-06-01T00:00:00Z",
                 "headRefOid": "oldshaFromFirstMerge"},
            ])
        if args and args[0] == "api" and any("branches" in a for a in args[1:]):
            import json as _json
            return _json.dumps({"commit": {"sha": "newshaFromFixCommit"}})
        raise AssertionError(f"unexpected gh call: {args}")

    monkeypatch.setattr(github_ops, "_gh", _gh_stub)

    writes: list[tuple[str, str]] = []
    monkeypatch.setattr(
        state, "mark_assignment_merged",
        lambda aid: writes.append(("merged", aid)),
    )

    actions = reconcile_board_merges(board, config)

    assert a.status == "done"
    assert writes == []
    assert not any("mark merged" in s for s in actions)


def test_backfill_then_mark_merged_in_one_sweep(monkeypatch, config) -> None:
    """A done-no-branch row that is also merged: backfill then flip in one pass."""
    a = _done_work(issue_number=42, branch=None)
    board = Board(completed=[a])
    writes = _patch_probes(
        monkeypatch, remote_branches={"issue-42-fix"}, terminal=True
    )

    reconcile_board_merges(board, config)

    assert a.branch == "issue-42-fix"
    assert a.status == "merged"
    assert ("branch", "abc") in writes
    assert ("merged", "abc") in writes


# ── #951 settle type=work review-stage ghost rows ──────────────────────────
#
# `mark_assignment_merged` (#609, sweep b above) only flips `status`, it never
# touches `review_state`. Every finished work assignment defaults to
# `review_state='pending'` (reconcile Pass 1 sets it unconditionally), so that
# ghost survives the status flip to 'merged' and the row keeps surfacing as
# "[awaiting review]" in `coord status` / the TUI forever — the display tag
# (coord/commands/status.py) is keyed on review_state independent of status.
# These rows also fell outside sweep (e) #894's sibling settle, which
# explicitly excludes type='work'. See issue #951.


def test_settles_work_review_state_when_terminal(monkeypatch, config) -> None:
    """A type=work done+review_state=pending row for a closed/merged issue
    must be settled: status flips to merged AND the review_state='pending'
    ghost clears, so it stops surfacing as "[awaiting review]" (#951)."""
    a = _done_work(issue_number=42, branch="issue-42-fix")
    a.review_state = "pending"
    board = Board(completed=[a])
    writes = _patch_probes(monkeypatch, terminal=True)

    actions = reconcile_board_merges(board, config)

    assert a.status == "merged"
    assert a.review_state == "done"
    assert ("merged", "abc") in writes
    assert ("work_review_settled", "abc") in writes
    assert any("mark merged" in s for s in actions)


def test_settles_work_review_state_without_branch_when_issue_closed(
    monkeypatch, config
) -> None:
    """#951: the issue-closed fast path must not require a resolvable branch.

    A done+pending work row whose branch could not be backfilled (no
    matching remote branch at all) still must settle when work_is_terminal
    is confirmed — e.g. via issue_is_closed, which needs no branch. Before
    the fix, the backfill's `continue` on a failed match stranded the row
    before sweep (b) ever ran the terminality check.
    """
    a = _done_work(issue_number=42, branch=None)
    a.review_state = "pending"
    board = Board(completed=[a])
    writes = _patch_probes(monkeypatch, remote_branches=set(), terminal=True)

    actions = reconcile_board_merges(board, config)

    assert a.branch is None  # never resolved — the fast path needs none
    assert a.status == "merged"
    assert a.review_state == "done"
    assert ("merged", "abc") in writes
    assert ("work_review_settled", "abc") in writes
    assert any("mark merged" in s for s in actions)


def test_work_review_state_left_untouched_when_issue_open(monkeypatch, config) -> None:
    """Fail-open: a still-OPEN issue's done+pending work row must be left alone."""
    a = _done_work(issue_number=42, branch="issue-42-fix")
    a.review_state = "pending"
    board = Board(completed=[a])
    writes = _patch_probes(monkeypatch, terminal=False)

    actions = reconcile_board_merges(board, config)

    assert a.status == "done"
    assert a.review_state == "pending"
    assert writes == []
    assert not any("mark merged" in s for s in actions)


def test_settles_work_review_state_when_already_merged(monkeypatch, config) -> None:
    """#951 (review round 2): a row that is ALREADY `status='merged'` with a
    lingering `review_state='pending'` ghost — the actual state of the bug
    report's cited examples (quadraui #406/407/409/411,
    claude-coordinator #782/795/946, vimcode #552) — must also settle.

    Once `mark_assignment_merged` flips `status` to 'merged' (in this run's
    sweep (b), or in a prior reconcile run), the row permanently drops out of
    sweep (b)'s `status=='done'` candidates list, so the review_state ghost is
    only reachable via sweep (e)'s ghost_candidates.
    """
    a = _done_work(issue_number=42, branch="issue-42-fix", status="merged")
    a.review_state = "pending"
    board = Board(completed=[a])
    writes = _patch_probes(monkeypatch, terminal=True)

    actions = reconcile_board_merges(board, config)

    assert a.status == "merged"
    assert a.review_state == "done"
    # sweep (b) never touches this row (status is already 'merged', not
    # 'done'), so no additional "merged" write — only the ghost settle.
    assert ("merged", "abc") not in writes
    assert ("work_review_settled", "abc") in writes
    assert any("settle work review_state" in s for s in actions)


# ── dry_run + filters ──────────────────────────────────────────────────────


def test_dry_run_makes_no_writes(monkeypatch, config) -> None:
    a = _done_work(issue_number=42, branch=None)
    board = Board(completed=[a])
    writes = _patch_probes(
        monkeypatch, remote_branches={"issue-42-fix"}, terminal=True
    )

    actions = reconcile_board_merges(board, config, dry_run=True)

    # No board mutation and no state writes.
    assert a.branch is None
    assert a.status == "done"
    assert writes == []
    # The actions still describe what WOULD change.
    assert any("[dry-run]" in s for s in actions)


def test_repo_filter_skips_other_repos(monkeypatch, config) -> None:
    a = _done_work(issue_number=42, branch="issue-42-fix")
    board = Board(completed=[a])
    writes = _patch_probes(monkeypatch, terminal=True)

    actions = reconcile_board_merges(board, config, repo="other-repo")

    assert a.status == "done"
    assert writes == []
    assert actions == []


def test_skips_non_work_and_non_done(monkeypatch, config) -> None:
    review = _done_work(assignment_id="rev", branch=None)
    review.type = "review"
    running = _done_work(assignment_id="run", status="running", branch=None)
    board = Board(active=[running], completed=[review])
    writes = _patch_probes(
        monkeypatch, remote_branches={"issue-42-fix"}, terminal=True
    )

    actions = reconcile_board_merges(board, config)

    assert writes == []
    assert actions == []


# ── #1083 test-author branch backfill inherits the #611 sweep ──────────────


def test_backfills_branch_for_test_author_type(monkeypatch, config) -> None:
    """type='test-author' rows never went through `coord.dispatch.dispatch()`
    (see coord/test_author.py), so they never got the same #611 safety net a
    type='work' row does when its branch is left NULL. Sweep (a) must cover
    test-author too so `coord pr <aid>` doesn't need a manual DB patch."""
    a = _done_work(assignment_id="ta1", issue_number=1041, branch=None)
    a.type = "test-author"
    board = Board(completed=[a])
    writes = _patch_probes(
        monkeypatch,
        remote_branches={"issue-1041-test-author-ms-33-acceptance-suite", "main"},
        terminal=False,
    )

    actions = reconcile_board_merges(board, config)

    assert a.branch == "issue-1041-test-author-ms-33-acceptance-suite"
    assert ("branch", "ta1") in writes
    assert any("backfill branch" in s for s in actions)


def test_test_author_marked_merged_by_sweep_b(monkeypatch, config) -> None:
    """#1574: sweep (b)'s type filter was scoped to `type='work'` only
    (#1083 left it that way deliberately, "out of scope"), so a
    `type='test-author'` row — every oracle-loop acceptance slice — could
    never reach `status='merged'` no matter how completely its branch
    landed. `work_is_terminal` is branch/commit-scoped (#1150) and already
    answers correctly for these rows, so there's nothing pipeline-specific
    left to gate on: a landed branch is a landed branch. Widened to
    `WORK_LIKE_TYPES` (work, mock-author, test-author).

    The `review_state='pending'` -> `'done'` ghost-clear that sweep (b) also
    does for `type='work'` (#951) stays `type='work'`-only (see the
    `test_test_author_review_state_not_settled_by_sweep_b` test below) — only
    the `status` flip widens here, per the issue's own fallback proposal."""
    a = _done_work(assignment_id="ta2", issue_number=1041, branch="issue-1041-ta")
    a.type = "test-author"
    a.review_state = "pending"
    board = Board(completed=[a])
    writes = _patch_probes(monkeypatch, terminal=True)

    reconcile_board_merges(board, config)

    assert a.status == "merged"
    assert ("merged", "ta2") in writes


def test_mock_author_marked_merged_by_sweep_b(monkeypatch, config) -> None:
    """Same #1574 widening applies to `type='mock-author'` (#930 Gate A) —
    it is structurally identical to test-author for this purpose (see
    `coord.models.WORK_LIKE_TYPES`)."""
    a = _done_work(assignment_id="ma1", issue_number=1041, branch="issue-1041-ma")
    a.type = "mock-author"
    a.review_state = "pending"
    board = Board(completed=[a])
    writes = _patch_probes(monkeypatch, terminal=True)

    reconcile_board_merges(board, config)

    assert a.status == "merged"
    assert ("merged", "ma1") in writes


def test_test_author_not_marked_merged_when_tracking_issue_closed_but_branch_unmerged(
    monkeypatch, config,
) -> None:
    """#2639 repro: a `test-author` row's `issue_number` is always the
    milestone's *tracking* issue (`for_issue_number` carries the real
    per-slice issue) — a tracking issue is closed for most of a milestone's
    life while slices are still being authored against it. Before this fix,
    sweep (b) trusted `issue_is_closed` for these rows exactly like it does
    for `type='work'`, so a closed epic alone flipped the row to
    `status='merged'` regardless of whether ITS OWN branch ever landed,
    silently evaporating the pushed slice (live case: row `b57cc3748a91`,
    `test-author-ms-1-slice-10`). Exercises the REAL `work_is_terminal` ->
    `issue_is_closed`/`pr_is_merged` chain, not a stubbed return, so it
    proves the fix at the actual decision point."""
    from coord import state  # noqa: PLC0415

    a = _done_work(assignment_id="ta-epic", issue_number=16, branch="test-author-ms-1-slice-10")
    a.type = "test-author"
    a.review_state = "pending"
    board = Board(completed=[a])

    # Restore the REAL work_is_terminal (the autouse conftest fixture stubs
    # it to always-False) so this test exercises the actual decision.
    monkeypatch.setattr(github_ops, "work_is_terminal", _REAL_WORK_IS_TERMINAL)
    # The tracking epic is closed...
    monkeypatch.setattr(github_ops, "issue_is_closed", lambda repo, n: True)
    # ...but THIS row's own branch never merged.
    monkeypatch.setattr(github_ops, "pr_is_merged", lambda repo, branch: False)
    monkeypatch.setattr(github_ops, "list_remote_branch_names", lambda repo: set())
    monkeypatch.setattr(github_ops, "list_open_prs", lambda repo: [])

    writes: list[tuple[str, str]] = []
    monkeypatch.setattr(
        state, "mark_assignment_merged",
        lambda aid: writes.append(("merged", aid)),
    )

    actions = reconcile_board_merges(board, config)

    assert a.status == "done", "the pushed slice must stay drivable, not evaporate"
    assert writes == []
    assert not any("mark merged" in s for s in actions)


def test_work_row_still_marked_merged_when_issue_closed_manually(
    monkeypatch, config,
) -> None:
    """Counterpart to the repro above: `type='work'` must keep trusting
    `issue_is_closed` on its own — the #522 flood guard this fix must not
    weaken. For `type='work'`, `issue_number` genuinely IS the row's own
    deliverable (`coord.models.CLOSES_ISSUE_TYPES`), so a manually-closed
    issue alone is still terminal even with no PR ever merged."""
    from coord import state  # noqa: PLC0415

    a = _done_work(assignment_id="w-closed", issue_number=349, branch="issue-349-fix")
    board = Board(completed=[a])

    monkeypatch.setattr(github_ops, "work_is_terminal", _REAL_WORK_IS_TERMINAL)
    monkeypatch.setattr(github_ops, "issue_is_closed", lambda repo, n: True)
    monkeypatch.setattr(github_ops, "pr_is_merged", lambda repo, branch: False)
    monkeypatch.setattr(github_ops, "list_remote_branch_names", lambda repo: set())
    monkeypatch.setattr(github_ops, "list_open_prs", lambda repo: [])

    writes: list[tuple[str, str]] = []
    monkeypatch.setattr(
        state, "mark_assignment_merged",
        lambda aid: writes.append(("merged", aid)),
    )

    actions = reconcile_board_merges(board, config)

    assert a.status == "merged"
    assert ("merged", "w-closed") in writes
    assert any("mark merged" in s for s in actions)


def test_test_author_review_state_not_settled_by_sweep_b(monkeypatch, config) -> None:
    """#1574: unlike `type='work'`, a test-author/mock-author row's
    `review_state='pending'` is deliberately left untouched by sweep (b)'s
    mark-merged step, even though `status` does flip to 'merged'. A
    test-author row's `review_state='done'` is exactly what sweep (f)'s
    #1180 wedged-review repair polices (a stray 'done' with no real review
    behind it) — settling it here would immediately be flagged as wedged
    and reset back to 'pending' by that sweep, pointless churn this fix
    doesn't need to introduce."""
    a = _done_work(assignment_id="ta2b", issue_number=1041, branch="issue-1041-ta")
    a.type = "test-author"
    a.review_state = "pending"
    board = Board(completed=[a])
    writes = _patch_probes(monkeypatch, terminal=True)

    reconcile_board_merges(board, config)

    assert a.status == "merged"
    assert a.review_state == "pending"
    assert ("work_review_settled", "ta2b") not in writes


def test_test_author_wedged_review_repair_still_fires_alongside_mark_merged(
    monkeypatch, config
) -> None:
    """End-to-end #1574 repro: a test-author row whose `review_state` was
    already wedged 'done' (the pre-#1150 `work_is_terminal` false-positive,
    #1180) with no real review ever having run. Before this fix,
    `reconcile-merges` only proposed the #1180 repair (done -> pending) and
    never `mark merged` for it — the exact symptom from the issue's live
    repro (ms-38 slice for #1124). After this fix both sweeps fire in the
    same pass: sweep (b) flips `status` to 'merged', sweep (f) independently
    resets the still-wedged `review_state` to 'pending' so the (now-fixed)
    review dispatch loop can retry a real review."""
    a = _done_work(assignment_id="ta2c", issue_number=1041, branch="issue-1041-ta")
    a.type = "test-author"
    a.review_state = "done"
    a.review_verdict = None
    board = Board(completed=[a])
    writes = _patch_probes(monkeypatch, terminal=True)

    actions = reconcile_board_merges(board, config)

    assert a.status == "merged"
    assert a.review_state == "pending"
    assert ("merged", "ta2c") in writes
    assert ("wedged_review_reset", "ta2c") in writes
    assert any("mark merged" in s for s in actions)
    assert any("repair wedged review_state" in s for s in actions)


def test_test_author_reconcile_converges_after_merge(monkeypatch, config) -> None:
    """#1574 acceptance: once a test-author row is flipped to 'merged', a
    second `reconcile_board_merges` pass proposes no further action for it —
    it has permanently dropped out of sweep (b)'s `status='done'` candidate
    list, same as a type='work' row does."""
    a = _done_work(assignment_id="ta3", issue_number=1041, branch="issue-1041-ta")
    a.type = "test-author"
    a.review_state = "pending"
    board = Board(completed=[a])
    _patch_probes(monkeypatch, terminal=True)

    reconcile_board_merges(board, config)
    assert a.status == "merged"

    writes_second = _patch_probes(monkeypatch, terminal=True)
    actions_second = reconcile_board_merges(board, config)

    assert writes_second == []
    assert actions_second == []


def test_review_type_still_excluded_from_mark_merged(monkeypatch, config) -> None:
    """Regression (#1574 acceptance): `type='review'` rows must remain
    excluded from sweep (b)'s terminal-merge check — the widening to
    `WORK_LIKE_TYPES` must not accidentally sweep in review rows too."""
    a = _done_work(assignment_id="rev1", issue_number=1041, branch="issue-1041-rev")
    a.type = "review"
    board = Board(completed=[a])
    writes = _patch_probes(monkeypatch, terminal=True)

    actions = reconcile_board_merges(board, config)

    assert a.status == "done"
    assert ("merged", "rev1") not in writes
    assert actions == []


# ── #1110 interactive merge session terminal detection ─────────────────────
#
# Interactive `--merge-of` sessions are dispatched with type="conflict-fix"
# — the same type the automated #241 conflict-fix worker uses — so sweep (b)
# must discriminate on `is_interactive_merge_session` (provider_name +
# review_of_assignment_id) rather than type alone, or it would either miss
# merge sessions entirely or wrongly start flipping automated conflict-fix
# workers to 'merged' too.


def test_interactive_merge_session_marked_merged_by_sweep_b(
    monkeypatch, config
) -> None:
    """An interactive merge-of session (type='conflict-fix',
    provider_name='claude-pty', review_of_assignment_id set) is flipped to
    'merged' by sweep (b) when the underlying issue/PR is terminal — this is
    what lets #1110's auto-reaper pick it up."""
    a = _done_work(
        assignment_id="merge1", issue_number=42, branch="issue-42-fix"
    )
    a.type = "conflict-fix"
    a.provider_name = "claude-pty"
    a.review_of_assignment_id = "work1"
    board = Board(completed=[a])
    writes = _patch_probes(monkeypatch, terminal=True)

    reconcile_board_merges(board, config)

    assert a.status == "merged"
    assert ("merged", "merge1") in writes


def test_automated_conflict_fix_worker_not_marked_merged_by_sweep_b(
    monkeypatch, config
) -> None:
    """The automated #241 conflict-fix worker also uses type='conflict-fix'
    and sets review_of_assignment_id, but never provider_name='claude-pty' —
    it must NOT be swept into 'merged' by the #1110 interactive-merge-session
    carve-out."""
    a = _done_work(
        assignment_id="cf1", issue_number=42, branch="issue-42-fix"
    )
    a.type = "conflict-fix"
    a.review_of_assignment_id = "work1"
    board = Board(completed=[a])
    writes = _patch_probes(monkeypatch, terminal=True)

    reconcile_board_merges(board, config)

    assert a.status == "done"
    assert ("merged", "cf1") not in writes


def test_interactive_merge_session_without_review_of_not_marked_merged(
    monkeypatch, config
) -> None:
    """provider_name='claude-pty' alone (e.g. an interactive review/fix
    session of some other type) is not enough — review_of_assignment_id must
    also be set for the row to count as an interactive merge session."""
    a = _done_work(
        assignment_id="cf2", issue_number=42, branch="issue-42-fix"
    )
    a.type = "conflict-fix"
    a.provider_name = "claude-pty"
    board = Board(completed=[a])
    writes = _patch_probes(monkeypatch, terminal=True)

    reconcile_board_merges(board, config)

    assert a.status == "done"
    assert ("merged", "cf2") not in writes


# ── #721 close stale PRs ──────────────────────────────────────────────────────


def _patch_stale_pr_probes(
    monkeypatch,
    *,
    open_prs: list[dict] | None = None,
    issue_closed: bool = False,
    fully_merged: bool = False,
) -> list[tuple]:
    """Stub the github_ops probes for the stale-PR sweep; record close calls."""
    from coord import github_ops

    monkeypatch.setattr(
        github_ops, "list_open_prs",
        lambda repo: list(open_prs or []),
    )
    monkeypatch.setattr(
        github_ops, "issue_is_closed",
        lambda repo, num: issue_closed,
    )
    monkeypatch.setattr(
        github_ops, "branch_is_fully_merged",
        lambda repo, branch, default_branch: fully_merged,
    )

    closed: list[tuple] = []
    monkeypatch.setattr(
        github_ops, "close_pr",
        lambda repo, number, comment=None: closed.append((repo, number)),
    )
    return closed


def test_stale_pr_closed_when_issue_is_closed(monkeypatch, config) -> None:
    """A PR linked to a closed issue must be closed by the sweep."""
    prs = [{"number": 99, "headRefName": "issue-42-the-fix"}]
    closed = _patch_stale_pr_probes(
        monkeypatch, open_prs=prs, issue_closed=True, fully_merged=False
    )

    actions = close_stale_prs(config)

    assert ("acme/api", 99) in closed
    assert any("close PR #99" in s and "issue #42 is closed" in s for s in actions)


def test_stale_pr_not_closed_for_mock_author_on_closed_tracking_epic(
    monkeypatch, config,
) -> None:
    """#3063 repro: a mock-author row's PR head branch is named after the
    milestone's *tracking* issue (#122), not its own deliverable — a closed
    tracking epic must not read as "this PR's work landed" (#2639's
    carve-out, applied here via the board-derived assignment type). Before
    this fix, close_stale_prs used a bare `issue_is_closed` with no
    carve-out, closing this PR every tick even though its own branch never
    merged — feeding the enqueue -> open -> close -> prune -> re-enqueue
    loop from the live coord-portal#122 incident."""
    prs = [{"number": 201, "headRefName": "issue-122-mock"}]
    closed = _patch_stale_pr_probes(
        monkeypatch, open_prs=prs, issue_closed=True, fully_merged=False
    )

    a = _done_work(assignment_id="mock-row", issue_number=122, branch="issue-122-mock")
    a.type = "mock-author"
    board = Board(completed=[a])

    actions = close_stale_prs(config, board=board)

    assert closed == []
    assert not any("close PR" in s for s in actions)


def test_stale_pr_closed_for_mock_author_once_its_own_branch_merges(
    monkeypatch, config,
) -> None:
    """Counterpart to the repro above: the carve-out doesn't wedge the PR
    open forever — once THIS row's own branch is confirmed merged (not just
    the tracking epic closed), the PR is still closed via the
    branch_is_fully_merged fallback."""
    prs = [{"number": 202, "headRefName": "issue-122-mock"}]
    closed = _patch_stale_pr_probes(
        monkeypatch, open_prs=prs, issue_closed=True, fully_merged=True
    )

    a = _done_work(assignment_id="mock-row", issue_number=122, branch="issue-122-mock")
    a.type = "mock-author"
    board = Board(completed=[a])

    actions = close_stale_prs(config, board=board)

    assert ("acme/api", 202) in closed
    assert any("close PR #202" in s and "already on" in s for s in actions)


def test_stale_pr_closed_when_branch_fully_merged(monkeypatch, config) -> None:
    """A PR whose branch is fully on the default branch must be closed."""
    prs = [{"number": 77, "headRefName": "issue-10-feature"}]
    closed = _patch_stale_pr_probes(
        monkeypatch, open_prs=prs, issue_closed=False, fully_merged=True
    )

    actions = close_stale_prs(config)

    assert ("acme/api", 77) in closed
    assert any("close PR #77" in s and "already on" in s for s in actions)


def test_live_pr_not_closed(monkeypatch, config) -> None:
    """A PR whose issue is open and branch still has commits ahead must be left alone."""
    prs = [{"number": 55, "headRefName": "issue-7-wip"}]
    closed = _patch_stale_pr_probes(
        monkeypatch, open_prs=prs, issue_closed=False, fully_merged=False
    )

    actions = close_stale_prs(config)

    assert closed == []
    assert not any("close PR" in s for s in actions)


def test_stale_pr_dry_run_no_close(monkeypatch, config) -> None:
    """dry_run=True must list stale PRs without closing them."""
    prs = [{"number": 33, "headRefName": "issue-5-done"}]
    closed = _patch_stale_pr_probes(
        monkeypatch, open_prs=prs, issue_closed=True, fully_merged=False
    )

    actions = close_stale_prs(config, dry_run=True)

    assert closed == []
    assert any("[dry-run]" in s for s in actions)
    assert any("close PR #33" in s for s in actions)


def test_non_coord_branch_skipped(monkeypatch, config) -> None:
    """PRs whose head branch does not follow issue-{N}-* must be ignored."""
    prs = [
        {"number": 11, "headRefName": "feature/some-thing"},
        {"number": 12, "headRefName": "dependabot/pip/requests-2.32"},
    ]
    closed = _patch_stale_pr_probes(
        monkeypatch, open_prs=prs, issue_closed=True, fully_merged=True
    )

    actions = close_stale_prs(config)

    assert closed == []
    assert not any("close PR" in s for s in actions)


def test_stale_pr_sweep_uses_feature_branch_base_for_opted_in_milestone(
    monkeypatch,
) -> None:
    """#934 review should-fix: close_stale_prs's milestone-aware `pr_base`
    (coord/reconcile.py:845-860) shipped with no test. A repo that opted
    into the git model, on a PR whose issue belongs to a milestone, must
    check branch_is_fully_merged against feature/ms-NN — not the flat
    default_branch — or a live in-milestone branch could be misclassified
    as stale and closed."""
    from coord import github_ops

    cfg = Config(
        repos=[Repo(name="api", github="acme/api", default_branch="main",
                     develop_branch="develop")],
        machines=[],
    )
    prs = [{"number": 77, "headRefName": "issue-10-feature"}]
    closed = _patch_stale_pr_probes(
        monkeypatch, open_prs=prs, issue_closed=False, fully_merged=False,
    )
    monkeypatch.setattr(
        github_ops, "get_issue",
        lambda repo, num: {"milestone": {"number": 9, "title": "M9"}},
    )

    captured: list[tuple] = []
    real_fully_merged = github_ops.branch_is_fully_merged

    def _spy(repo, branch, default_branch):
        captured.append((repo, branch, default_branch))
        return False

    monkeypatch.setattr(github_ops, "branch_is_fully_merged", _spy)

    close_stale_prs(cfg)

    assert captured == [("acme/api", "issue-10-feature", "feature/ms-9")]
    assert closed == []


def test_stale_pr_closed_against_feature_branch_when_actually_stale(
    monkeypatch,
) -> None:
    """Same milestone setup, but the branch IS fully merged into
    feature/ms-9 — the PR should be closed with a message naming the
    feature branch, not `main`."""
    from coord import github_ops

    cfg = Config(
        repos=[Repo(name="api", github="acme/api", default_branch="main",
                     develop_branch="develop")],
        machines=[],
    )
    prs = [{"number": 77, "headRefName": "issue-10-feature"}]
    closed = _patch_stale_pr_probes(
        monkeypatch, open_prs=prs, issue_closed=False, fully_merged=True,
    )
    monkeypatch.setattr(
        github_ops, "get_issue",
        lambda repo, num: {"milestone": {"number": 9, "title": "M9"}},
    )

    actions = close_stale_prs(cfg)

    assert ("acme/api", 77) in closed
    assert any(
        "close PR #77" in s and "already on feature/ms-9" in s for s in actions
    )


def test_stale_pr_sweep_uses_default_branch_when_not_opted_in(
    monkeypatch,
) -> None:
    """A repo without develop_branch never calls get_issue for the
    milestone lookup at all — zero extra cost for non-opted-in repos."""
    from coord import github_ops

    cfg = Config(
        repos=[Repo(name="api", github="acme/api", default_branch="main")],
        machines=[],
    )
    prs = [{"number": 77, "headRefName": "issue-10-feature"}]
    closed = _patch_stale_pr_probes(
        monkeypatch, open_prs=prs, issue_closed=False, fully_merged=True,
    )

    def _boom(repo, num):
        raise AssertionError("get_issue must not be called for a non-opted-in repo")

    monkeypatch.setattr(github_ops, "get_issue", _boom)

    actions = close_stale_prs(cfg)

    assert ("acme/api", 77) in closed
    assert any("already on main" in s for s in actions)


def test_stale_pr_sweep_integrated_into_reconcile_board_merges(
    monkeypatch, config
) -> None:
    """reconcile_board_merges must include stale-PR actions in its output."""
    # Empty board — all three earlier sweeps produce nothing.
    board = Board(completed=[], active=[])
    # Patch the board-level probes so reconcile_board_merges doesn't error.
    _patch_probes(monkeypatch, remote_branches=set(), terminal=False)

    prs = [{"number": 44, "headRefName": "issue-9-old-work"}]
    _patch_stale_pr_probes(
        monkeypatch, open_prs=prs, issue_closed=True, fully_merged=False
    )

    actions = reconcile_board_merges(board, config)

    assert any("close PR #44" in s for s in actions)


# ── #2994: dormant-repo skip ──────────────────────────────────────────────────


@pytest.fixture
def two_repo_config() -> Config:
    return Config(
        repos=[
            Repo(name="api", github="acme/api", default_branch="main"),
            Repo(name="idle", github="acme/idle", default_branch="main"),
        ],
        machines=[],
    )


def test_close_stale_prs_skips_dormant_repo_when_opted_in(
    monkeypatch, two_repo_config
) -> None:
    """#2994: with skip_dormant_repos=True and a board showing activity on
    'api' but nothing at all on 'idle', the sweep must not call
    list_open_prs for 'idle' -- and must say so in its actions."""
    from coord import repo_dormancy

    calls: list[str] = []
    monkeypatch.setattr(
        github_ops,
        "list_open_prs",
        lambda repo: calls.append(repo) or [],
    )

    # 'idle' already had a baseline sweep, well inside the floor -- only a
    # repo that has been swept before is even eligible to be skipped (see
    # test_never_swept_repo_is_not_skipped in test_repo_dormancy.py).
    repo_dormancy.record_swept("idle", repo_dormancy.KIND_PRS, now=time.time())

    board = Board(
        active=[],
        completed=[_done_work(assignment_id="w1", branch="issue-1-x")],
    )
    board.completed[0].repo_name = "api"

    actions = close_stale_prs(two_repo_config, board=board, skip_dormant_repos=True)

    assert calls == ["acme/api"]
    assert any("skipped 1 dormant repo" in s for s in actions)


def test_close_stale_prs_default_does_not_skip_idle_repo(
    monkeypatch, two_repo_config
) -> None:
    """Without opting in (the manual `coord reconcile-merges` path), every
    repo is still swept even when a board is supplied and shows no
    activity anywhere -- skip_dormant_repos defaults to False."""
    calls: list[str] = []
    monkeypatch.setattr(
        github_ops,
        "list_open_prs",
        lambda repo: calls.append(repo) or [],
    )

    board = Board(active=[], completed=[])

    actions = close_stale_prs(two_repo_config, board=board)

    assert calls == ["acme/api", "acme/idle"]
    assert not any("dormant" in s for s in actions)


def test_close_stale_prs_dormant_repo_swept_again_past_the_floor(
    monkeypatch, config
) -> None:
    """A dormant repo is only skipped inside DORMANT_SWEEP_FLOOR_S of its
    last real sweep -- past that, the next tick sweeps it for real again so
    out-of-band activity is still noticed eventually."""
    from coord import repo_dormancy

    calls: list[str] = []
    monkeypatch.setattr(
        github_ops,
        "list_open_prs",
        lambda repo: calls.append(repo) or [],
    )

    now = time.time()
    repo_dormancy.record_swept(
        "api", repo_dormancy.KIND_PRS, now=now - repo_dormancy.DORMANT_SWEEP_FLOOR_S - 1.0
    )

    board = Board(active=[], completed=[])  # no activity -- 'api' is idle

    actions = close_stale_prs(config, board=board, skip_dormant_repos=True)

    assert calls == ["acme/api"]
    assert not any("dormant" in s for s in actions)


def test_reconcile_board_merges_wakes_dormant_repo_when_work_is_queued(
    monkeypatch, two_repo_config
) -> None:
    """#2994 acceptance: queuing work for a dormant repo puts it back on the
    normal cadence on the very next tick, not after the floor expires."""
    from coord import repo_dormancy

    _patch_probes(monkeypatch, remote_branches=set(), terminal=False)
    calls: list[str] = []
    monkeypatch.setattr(
        github_ops,
        "list_open_prs",
        lambda repo: calls.append(repo) or [],
    )

    now = time.time()
    # Both repos need a baseline sweep before dormancy skip applies at all
    # ('never swept' always sweeps -- see test_never_swept_repo_is_not_skipped
    # in test_repo_dormancy.py). 'api' has real activity below so it's never
    # skipped regardless; 'idle' was just swept -- well inside the floor.
    repo_dormancy.record_swept("api", repo_dormancy.KIND_PRS, now=now)
    repo_dormancy.record_swept("idle", repo_dormancy.KIND_PRS, now=now)

    api_assignment = _done_work(assignment_id="w-api", status="running")
    api_assignment.repo_name = "api"
    board = Board(active=[api_assignment], completed=[])
    reconcile_board_merges(board, two_repo_config, skip_dormant_repos=True)
    assert calls == ["acme/api"]  # 'idle' skipped -- still inside the floor

    # Work gets queued for 'idle' (an open assignment appears on the board).
    calls.clear()
    idle_assignment = _done_work(assignment_id="w-idle", status="pending")
    idle_assignment.repo_name = "idle"
    board.active.append(idle_assignment)

    reconcile_board_merges(board, two_repo_config, skip_dormant_repos=True)

    assert calls == ["acme/api", "acme/idle"]


# ── #732 prune stale merge_queue entries ─────────────────────────────────────


def test_reconcile_prunes_conflict_entry_for_closed_issue(
    monkeypatch, config, coord_db
) -> None:
    """Acceptance test: a CONFLICT entry whose issue is closed is pruned by
    reconcile_board_merges and no longer appears in pending_summary (#732)."""
    from coord import github_ops
    from coord import merge_queue as mq
    from coord.merge_queue import CONFLICT, QueuedMerge, save_queue

    # Seed a stale conflict entry (mirrors the #217 incident: assignment
    # id=60275968b733, issue=#217, state=conflict, issue now closed).
    save_queue([
        QueuedMerge(
            assignment_id="60275968b733",
            repo_name="api",
            repo_github="acme/api",
            branch="issue-217-old-work",
            target_branch="main",
            issue_number=217,
            issue_title="Old closed issue",
            state=CONFLICT,
        )
    ])

    # Stub network calls: issue 217 is closed; no open PRs.
    monkeypatch.setattr(github_ops, "issue_is_closed", lambda repo, n: n == 217)
    monkeypatch.setattr(github_ops, "pr_is_merged", lambda repo, b: False)
    monkeypatch.setattr(github_ops, "list_remote_branch_names", lambda repo: set())
    monkeypatch.setattr(github_ops, "work_is_terminal", lambda *a, **kw: False)
    monkeypatch.setattr(github_ops, "list_open_prs", lambda repo: [])

    board = Board(completed=[], active=[])
    actions = reconcile_board_merges(board, config)

    # The prune action must be reported.
    assert any("prune queue entry 60275968b733" in s for s in actions)

    # The queue must now be empty.
    remaining = mq.load_queue()
    assert remaining == [], f"Expected empty queue, got {remaining}"

    # pending_summary must no longer report a conflict.
    summary = mq.pending_summary(mq.load_queue())
    assert summary == {}, f"Expected no pending entries, got {summary}"


def test_reconcile_prunes_conflict_entry_for_merged_pr(
    monkeypatch, config, coord_db
) -> None:
    """A CONFLICT entry whose PR is already merged is pruned."""
    from coord import github_ops
    from coord import merge_queue as mq
    from coord.merge_queue import CONFLICT, QueuedMerge, save_queue

    save_queue([
        QueuedMerge(
            assignment_id="aid-merged-pr",
            repo_name="api",
            repo_github="acme/api",
            branch="issue-50-feature",
            target_branch="main",
            issue_number=50,
            issue_title="Already merged",
            state=CONFLICT,
        )
    ])

    monkeypatch.setattr(github_ops, "issue_is_closed", lambda repo, n: False)
    monkeypatch.setattr(
        github_ops, "pr_is_merged",
        lambda repo, b: b == "issue-50-feature",
    )
    monkeypatch.setattr(github_ops, "list_remote_branch_names", lambda repo: set())
    monkeypatch.setattr(github_ops, "work_is_terminal", lambda *a, **kw: False)
    monkeypatch.setattr(github_ops, "list_open_prs", lambda repo: [])

    board = Board(completed=[], active=[])
    actions = reconcile_board_merges(board, config)

    assert any("prune queue entry aid-merged-pr" in s for s in actions)
    assert mq.load_queue() == []


def test_reconcile_prune_dry_run_does_not_remove_entry(
    monkeypatch, config, coord_db
) -> None:
    """dry_run=True reports what would be pruned without modifying the queue."""
    from coord import github_ops
    from coord import merge_queue as mq
    from coord.merge_queue import CONFLICT, QueuedMerge, save_queue

    save_queue([
        QueuedMerge(
            assignment_id="dry-stale",
            repo_name="api",
            repo_github="acme/api",
            branch="issue-99-stale",
            target_branch="main",
            issue_number=99,
            issue_title="Stale",
            state=CONFLICT,
        )
    ])

    monkeypatch.setattr(github_ops, "issue_is_closed", lambda repo, n: True)
    monkeypatch.setattr(github_ops, "pr_is_merged", lambda repo, b: False)
    monkeypatch.setattr(github_ops, "list_remote_branch_names", lambda repo: set())
    monkeypatch.setattr(github_ops, "work_is_terminal", lambda *a, **kw: False)
    monkeypatch.setattr(github_ops, "list_open_prs", lambda repo: [])

    board = Board(completed=[], active=[])
    actions = reconcile_board_merges(board, config, dry_run=True)

    assert any("dry-stale" in s and "dry-run" in s for s in actions)
    assert len(mq.load_queue()) == 1  # still there


# ── #894 settle sibling ghost rows ────────────────────────────────────────────


def _ghost_sibling(
    *,
    assignment_id: str,
    issue_number: int = 42,
    sibling_type: str = "review",
    status: str = "done",
    review_state: str | None = "pending",
    branch: str | None = None,
) -> Assignment:
    """Build a completed sibling assignment (review/smoke/conflict-fix/advisory)."""
    return Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=issue_number,
        issue_title="t",
        status=status,
        assignment_id=assignment_id,
        branch=branch,
        type=sibling_type,
        review_state=review_state,
    )


def test_settles_review_sibling_when_issue_terminal(monkeypatch, config) -> None:
    """A type=review done+review_state=pending row must be settled when work_is_terminal."""
    work = _done_work(assignment_id="work-1", issue_number=42, branch="issue-42-fix")
    review = _ghost_sibling(assignment_id="rev-1", sibling_type="review", review_state="pending")
    board = Board(completed=[work, review])
    writes = _patch_probes(monkeypatch, terminal=True)

    actions = reconcile_board_merges(board, config)

    # work row: flipped to merged
    assert work.status == "merged"
    assert ("merged", "work-1") in writes
    # review sibling: review_state cleared
    assert review.review_state == "done"
    assert ("sibling_review_done", "rev-1") in writes
    assert any("settle sibling" in s and "rev-1" in s for s in actions)


def test_settles_smoke_sibling_when_issue_terminal(monkeypatch, config) -> None:
    """A type=smoke done+review_state=pending row must be settled when work_is_terminal."""
    work = _done_work(assignment_id="work-2", issue_number=42, branch="issue-42-fix")
    smoke = _ghost_sibling(assignment_id="smk-1", sibling_type="smoke", review_state="pending")
    board = Board(completed=[work, smoke])
    writes = _patch_probes(monkeypatch, terminal=True)

    actions = reconcile_board_merges(board, config)

    assert smoke.review_state == "done"
    assert ("sibling_review_done", "smk-1") in writes
    assert any("settle sibling" in s and "smk-1" in s for s in actions)


def test_settles_advisory_row_when_issue_terminal(monkeypatch, config) -> None:
    """A status=advisory row must be flipped to merged when work_is_terminal."""
    work = _done_work(assignment_id="work-3", issue_number=42, branch="issue-42-fix")
    advisory = _ghost_sibling(
        assignment_id="adv-1", sibling_type="work",
        status="advisory", review_state=None,
    )
    board = Board(completed=[work, advisory])
    writes = _patch_probes(monkeypatch, terminal=True)

    actions = reconcile_board_merges(board, config)

    assert advisory.status == "merged"
    assert ("advisory_settled", "adv-1") in writes
    assert any("settle advisory" in s and "adv-1" in s for s in actions)


def test_settles_refused_policy_row_when_issue_terminal(monkeypatch, config) -> None:
    """#2234: a status=refused_policy row must be flipped to merged when
    work_is_terminal — same shape as the advisory settle above."""
    work = _done_work(assignment_id="work-4", issue_number=42, branch="issue-42-fix")
    refused = _ghost_sibling(
        assignment_id="rp-1", sibling_type="work",
        status="refused_policy", review_state=None,
    )
    board = Board(completed=[work, refused])
    writes = _patch_probes(monkeypatch, terminal=True)

    actions = reconcile_board_merges(board, config)

    assert refused.status == "merged"
    assert ("refused_policy_settled", "rp-1") in writes
    assert any("settle refused_policy" in s and "rp-1" in s for s in actions)


def test_refused_policy_row_not_settled_when_issue_not_terminal(
    monkeypatch, config
) -> None:
    """#2234: a refused_policy row for a still-open/unmerged issue is left
    untouched, mirroring the advisory case."""
    work = _done_work(assignment_id="w-live2", issue_number=8, branch="issue-8-fix")
    refused = _ghost_sibling(
        assignment_id="rp-live", sibling_type="work",
        issue_number=8, status="refused_policy", review_state=None,
    )
    board = Board(completed=[work, refused])
    writes = _patch_probes(monkeypatch, terminal=False)  # issue NOT terminal

    reconcile_board_merges(board, config)

    assert refused.status == "refused_policy"  # untouched
    assert ("refused_policy_settled", "rp-live") not in writes


def test_settles_all_ghost_types_in_one_pass(monkeypatch, config) -> None:
    """All three ghost-row types — review, smoke, advisory — are settled together.

    Acceptance criterion: a merged+closed issue with leftover type=review/smoke/
    advisory rows → after reconcile_board_merges, NONE remain non-terminal.
    """
    work = _done_work(assignment_id="w0", issue_number=42, branch="issue-42-fix")
    review = _ghost_sibling(assignment_id="rv0", sibling_type="review", review_state="pending")
    smoke = _ghost_sibling(assignment_id="sk0", sibling_type="smoke", review_state="pending")
    conflict_fix = _ghost_sibling(
        assignment_id="cf0", sibling_type="conflict-fix", review_state="pending"
    )
    advisory = _ghost_sibling(
        assignment_id="ad0", sibling_type="work", status="advisory", review_state=None,
    )
    board = Board(completed=[work, review, smoke, conflict_fix, advisory])
    writes = _patch_probes(monkeypatch, terminal=True)

    reconcile_board_merges(board, config)

    # All ghost rows settled — none remain non-terminal.
    assert review.review_state == "done", "review sibling must have review_state='done'"
    assert smoke.review_state == "done", "smoke sibling must have review_state='done'"
    assert conflict_fix.review_state == "done", "conflict-fix sibling must be settled"
    assert advisory.status == "merged", "advisory row must be flipped to 'merged'"
    # Work row also settled.
    assert work.status == "merged"


def test_ghost_rows_not_settled_when_issue_not_terminal(monkeypatch, config) -> None:
    """Ghost rows for a still-open/unmerged issue must be left untouched.

    Acceptance criterion: a still-open/unmerged issue's rows are left untouched.
    """
    work = _done_work(assignment_id="w-live", issue_number=7, branch="issue-7-fix")
    review = _ghost_sibling(
        assignment_id="rv-live", sibling_type="review",
        issue_number=7, review_state="pending",
    )
    advisory = _ghost_sibling(
        assignment_id="ad-live", sibling_type="work",
        issue_number=7, status="advisory", review_state=None,
    )
    board = Board(completed=[work, review, advisory])
    writes = _patch_probes(monkeypatch, terminal=False)  # issue NOT terminal

    reconcile_board_merges(board, config)

    assert work.status == "done"            # work untouched
    assert review.review_state == "pending" # sibling untouched
    assert advisory.status == "advisory"    # advisory untouched
    assert not any("settle" in s for s in [])
    assert ("sibling_review_done", "rv-live") not in writes
    assert ("advisory_settled", "ad-live") not in writes


def test_sibling_settle_dry_run_makes_no_writes(monkeypatch, config) -> None:
    """dry_run=True describes what would settle without mutating anything."""
    work = _done_work(assignment_id="w-dry", issue_number=42, branch="issue-42-fix")
    review = _ghost_sibling(assignment_id="rv-dry", sibling_type="review", review_state="pending")
    advisory = _ghost_sibling(
        assignment_id="ad-dry", sibling_type="work", status="advisory", review_state=None,
    )
    board = Board(completed=[work, review, advisory])
    writes = _patch_probes(monkeypatch, terminal=True)

    actions = reconcile_board_merges(board, config, dry_run=True)

    # No in-memory mutations.
    assert review.review_state == "pending"
    assert advisory.status == "advisory"
    assert work.status == "done"
    # No state writes.
    assert ("sibling_review_done", "rv-dry") not in writes
    assert ("advisory_settled", "ad-dry") not in writes
    # Actions describe what WOULD happen.
    assert any("settle sibling" in s and "[dry-run]" in s for s in actions)
    assert any("settle advisory" in s and "[dry-run]" in s for s in actions)


def test_sibling_settle_respects_issue_filter(monkeypatch, config) -> None:
    """The --issue filter scopes sibling settling to the targeted issue."""
    # Issue 42: terminal, has ghost review sibling.
    work42 = _done_work(assignment_id="w42", issue_number=42, branch="issue-42-fix")
    review42 = _ghost_sibling(assignment_id="rv42", sibling_type="review", issue_number=42)
    # Issue 7: also terminal, has ghost review sibling — but outside the filter.
    work7 = _done_work(assignment_id="w7", issue_number=7, branch="issue-7-fix")
    review7 = _ghost_sibling(assignment_id="rv7", sibling_type="review", issue_number=7)
    board = Board(completed=[work42, review42, work7, review7])
    writes = _patch_probes(monkeypatch, terminal=True)

    reconcile_board_merges(board, config, issue=42)

    # Issue 42 ghost settled; issue 7 ghost untouched.
    assert review42.review_state == "done"
    assert ("sibling_review_done", "rv42") in writes
    assert review7.review_state == "pending"
    assert ("sibling_review_done", "rv7") not in writes


def test_sibling_already_merged_branch_used_for_terminality(monkeypatch, config) -> None:
    """When the sibling row has no branch, the work row's branch is used for
    the work_is_terminal probe so the pr_is_merged fast-path can fire."""
    # Work row has a branch; sibling has none.
    work = _done_work(assignment_id="w-nb", issue_number=42, branch="issue-42-fix")
    # Sibling row has no branch — relies on work row's branch for the probe.
    review = _ghost_sibling(
        assignment_id="rv-nb", sibling_type="review",
        review_state="pending", branch=None,
    )
    board = Board(completed=[work, review])

    probed_branches: list[str | None] = []

    from coord import github_ops, state  # noqa: PLC0415

    def _track_terminal(repo, issue, branch, cache=None, trust_issue_closed=True):
        probed_branches.append(branch)
        return True  # always terminal for this test

    monkeypatch.setattr(github_ops, "work_is_terminal", _track_terminal)
    monkeypatch.setattr(github_ops, "list_remote_branch_names", lambda repo: set())
    monkeypatch.setattr(github_ops, "list_open_prs", lambda repo: [])
    monkeypatch.setattr(state, "update_assignment_branch", lambda *a: None)
    monkeypatch.setattr(state, "mark_assignment_merged", lambda *a: None)
    monkeypatch.setattr(state, "mark_sibling_review_done", lambda *a: None)
    monkeypatch.setattr(state, "mark_advisory_settled", lambda *a: None)

    reconcile_board_merges(board, config)

    # The sweep should have used the work row's branch for the sibling probe.
    assert "issue-42-fix" in probed_branches, (
        f"Expected 'issue-42-fix' in probed branches; got {probed_branches}"
    )


# ── #1180 wedged test-author/mock-author review_state repair ────────────────


def _wedged_test_author(
    *,
    assignment_id: str = "ta-wedged",
    issue_number: int = 1117,
    branch: str = "test-author-ms-37-slice-1115",
    typ: str = "test-author",
    review_state: str | None = "done",
    review_verdict: str | None = None,
) -> Assignment:
    return Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=issue_number,
        issue_title="t",
        status="done",
        assignment_id=assignment_id,
        branch=branch,
        type=typ,
        review_state=review_state,
        review_verdict=review_verdict,
    )


def test_repairs_wedged_test_author_review_when_no_review_ran(monkeypatch, config) -> None:
    """The #1180 repro: a test-author row false-positived work_is_terminal
    pre-#1150 and got stamped review_state='done' with no verdict, and no
    type='review' assignment ever ran against its branch. The row must be
    reset to review_state='pending' so the (now-fixed) auto-loop retries it."""
    a = _wedged_test_author()
    board = Board(completed=[a])
    writes = _patch_probes(monkeypatch, terminal=False)

    actions = reconcile_board_merges(board, config)

    assert a.review_state == "pending"
    assert ("wedged_review_reset", "ta-wedged") in writes
    assert any("repair wedged review_state" in s and "ta-wedged" in s for s in actions)


def test_mock_author_wedged_review_also_repaired(monkeypatch, config) -> None:
    a = _wedged_test_author(assignment_id="ma-wedged", typ="mock-author")
    board = Board(completed=[a])
    writes = _patch_probes(monkeypatch, terminal=False)

    reconcile_board_merges(board, config)

    assert a.review_state == "pending"
    assert ("wedged_review_reset", "ma-wedged") in writes


def test_wedged_review_left_alone_when_a_review_actually_ran(monkeypatch, config) -> None:
    """A completed type='review' assignment on the SAME branch means a review
    genuinely happened — do not touch review_state (it's not wedged)."""
    a = _wedged_test_author()
    review = Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=1117,
        issue_title="t",
        status="done",
        assignment_id="rev-real",
        branch=a.branch,
        type="review",
        review_verdict="approve",
    )
    board = Board(completed=[a, review])
    writes = _patch_probes(monkeypatch, terminal=False)

    actions = reconcile_board_merges(board, config)

    assert a.review_state == "done"
    assert ("wedged_review_reset", "ta-wedged") not in writes
    assert not any("repair wedged review_state" in s for s in actions)


def test_wedged_review_left_alone_when_review_still_finalizing(monkeypatch, config) -> None:
    """#1566: a review that just finished lands on status='finalizing' (not
    'done') until `coord notify` parses and posts its verdict. That window
    must NOT be mistaken for "no review ever ran" — the #1180 sweep repairing
    it out from under a review that is still actively wrapping up would
    trigger a spurious repair + a duplicate dispatch_pending_reviews pass
    racing the one already in flight."""
    a = _wedged_test_author()
    review = Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=1117,
        issue_title="t",
        status="finalizing",
        assignment_id="rev-finalizing",
        branch=a.branch,
        type="review",
        review_verdict=None,
    )
    board = Board(completed=[a, review])
    writes = _patch_probes(monkeypatch, terminal=False)

    actions = reconcile_board_merges(board, config)

    assert a.review_state == "done"
    assert ("wedged_review_reset", "ta-wedged") not in writes
    assert not any("repair wedged review_state" in s for s in actions)


def test_wedged_review_left_alone_when_review_state_not_done(monkeypatch, config) -> None:
    """review_state='pending' is already the eligible/healthy state for the
    normal dispatch loop — sweep (f) only repairs review_state='done'."""
    a = _wedged_test_author(review_state="pending")
    board = Board(completed=[a])
    writes = _patch_probes(monkeypatch, terminal=False)

    reconcile_board_merges(board, config)

    assert a.review_state == "pending"
    assert ("wedged_review_reset", "ta-wedged") not in writes


def test_wedged_review_left_alone_when_verdict_present(monkeypatch, config) -> None:
    """A captured review_verdict means a real review ran (or its verdict was
    recovered) — not the #1180 false-positive shape."""
    a = _wedged_test_author(review_verdict="approve")
    board = Board(completed=[a])
    writes = _patch_probes(monkeypatch, terminal=False)

    reconcile_board_merges(board, config)

    assert a.review_state == "done"
    assert ("wedged_review_reset", "ta-wedged") not in writes


def test_wedged_review_repair_skipped_without_branch(monkeypatch, config) -> None:
    """No branch means nothing to key the review-existence check on — leave
    it for the branch-backfill sweep (a) instead."""
    a = _wedged_test_author(branch=None)
    board = Board(completed=[a])
    writes = _patch_probes(monkeypatch, terminal=False)

    reconcile_board_merges(board, config)

    assert a.review_state == "done"
    assert ("wedged_review_reset", "ta-wedged") not in writes


def test_wedged_review_repair_dry_run_makes_no_writes(monkeypatch, config) -> None:
    a = _wedged_test_author()
    board = Board(completed=[a])
    writes = _patch_probes(monkeypatch, terminal=False)

    actions = reconcile_board_merges(board, config, dry_run=True)

    assert a.review_state == "done"  # unchanged
    assert ("wedged_review_reset", "ta-wedged") not in writes
    assert any(
        "repair wedged review_state" in s and "[dry-run]" in s for s in actions
    )


def test_wedged_review_repair_respects_issue_filter(monkeypatch, config) -> None:
    a1 = _wedged_test_author(assignment_id="ta-1117", issue_number=1117, branch="b-1117")
    a2 = _wedged_test_author(assignment_id="ta-2000", issue_number=2000, branch="b-2000")
    board = Board(completed=[a1, a2])
    writes = _patch_probes(monkeypatch, terminal=False)

    reconcile_board_merges(board, config, issue=1117)

    assert a1.review_state == "pending"
    assert a2.review_state == "done"
    assert ("wedged_review_reset", "ta-1117") in writes
    assert ("wedged_review_reset", "ta-2000") not in writes


# ── #1767 drop stale drive escalations for out-of-band resolutions ─────────


def test_dismisses_escalation_when_issue_is_terminal(monkeypatch, config) -> None:
    from coord import state

    state._record_drive_escalation_local(
        "api", 42,
        stage="merge",
        reason="merge: BLOCKED — smoke_required",
        gate_readings="smoke=missing",
        proposed_command="gh pr merge 9 --rebase",
    )
    _patch_probes(monkeypatch, terminal=True)

    actions = reconcile_board_merges(Board(), config)

    assert state._get_drive_escalation_local("api", 42) is None
    assert any("dismiss escalation" in s and "#1767" in s for s in actions)


def test_leaves_escalation_when_issue_still_open(monkeypatch, config) -> None:
    from coord import state

    state._record_drive_escalation_local(
        "api", 42,
        stage="merge",
        reason="merge: BLOCKED — smoke_required",
        gate_readings="smoke=missing",
        proposed_command="gh pr merge 9 --rebase",
    )
    _patch_probes(monkeypatch, terminal=False)

    actions = reconcile_board_merges(Board(), config)

    assert state._get_drive_escalation_local("api", 42) is not None
    assert not any("dismiss escalation" in s for s in actions)


def test_escalation_dismiss_dry_run_makes_no_writes(monkeypatch, config) -> None:
    from coord import state

    state._record_drive_escalation_local(
        "api", 42,
        stage="merge",
        reason="merge: BLOCKED — smoke_required",
        gate_readings="smoke=missing",
        proposed_command="gh pr merge 9 --rebase",
    )
    _patch_probes(monkeypatch, terminal=True)

    actions = reconcile_board_merges(Board(), config, dry_run=True)

    assert state._get_drive_escalation_local("api", 42) is not None
    assert any(
        "dismiss escalation" in s and "[dry-run]" in s for s in actions
    )


def test_escalation_for_unknown_repo_is_left_alone(monkeypatch, config) -> None:
    """The drive-queue's own synthetic alert entry (repo_name="(drive-queue)",
    not a real GitHub repo) must never be probed via `gh` or dismissed by
    this sweep — it has its own lifecycle (#1753 DQ-1)."""
    from coord import state

    state._record_drive_escalation_local(
        "(drive-queue)", 0,
        stage="queue-alert",
        reason="QUEUE: BLOCKED 2",
        gate_readings="",
        proposed_command="",
    )
    _patch_probes(monkeypatch, terminal=True)

    reconcile_board_merges(Board(), config)

    assert state._get_drive_escalation_local("(drive-queue)", 0) is not None


def test_escalation_dismiss_respects_issue_filter(monkeypatch, config) -> None:
    from coord import state

    state._record_drive_escalation_local(
        "api", 42, stage="merge", reason="r1", gate_readings="", proposed_command="",
    )
    state._record_drive_escalation_local(
        "api", 99, stage="merge", reason="r2", gate_readings="", proposed_command="",
    )
    _patch_probes(monkeypatch, terminal=True)

    reconcile_board_merges(Board(), config, issue=42)

    assert state._get_drive_escalation_local("api", 42) is None
    assert state._get_drive_escalation_local("api", 99) is not None


def test_merging_via_coord_merge_leaves_no_dangling_escalation_after_reconcile(
    monkeypatch, config
) -> None:
    """End-to-end sanity for the two-layer fix (#1767): a `work` row that's
    already flipped to 'merged' with no lingering ghost still gets its
    escalation cleared by this sweep, covering the case where the merge
    happened out of band and only reconciliation ever sees it."""
    from coord import state

    a = _done_work(issue_number=7, branch="issue-7-fix", status="merged")
    board = Board(completed=[a])
    state._record_drive_escalation_local(
        "api", 7, stage="merge", reason="stale", gate_readings="", proposed_command="",
    )
    _patch_probes(monkeypatch, terminal=True)

    reconcile_board_merges(board, config)

    assert state._get_drive_escalation_local("api", 7) is None


# ── #2639 second half — sweep (h): flag a falsely-`merged` row ─────────────
#
# `github_ops.work_is_terminal`'s issue-closed check (before the fix in this
# same PR) could flip a `status='merged'` row whose branch never actually
# landed anywhere — this sweep re-derives "did this really land" from
# git/GitHub reality for every already-`merged` row, so a historical mis-flip
# doesn't stay permanently invisible to `coord diagnose`/`coord merge
# --dry-run`. DETECTION ONLY: it must never mutate board state.


def _merged_row(
    *,
    assignment_id: str = "ta-1",
    issue_number: int = 16,
    branch: str = "test-author-ms-1-slice-10",
    row_type: str = "test-author",
) -> Assignment:
    return Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=issue_number,
        issue_title="t",
        status="merged",
        assignment_id=assignment_id,
        branch=branch,
        type=row_type,
    )


def _patch_false_merge_probes(
    monkeypatch,
    *,
    branch_exists: bool = True,
    pr_merged: bool = False,
    ahead: int | None = 2,
    changed_files: list[str] | None = None,
    file_contents: dict[str, tuple[str | None, str | None]] | None = None,
):
    """Stub the sweep (h) probes directly (bypassing `_patch_probes`'s
    `work_is_terminal` stub, which sweep (h) never calls).

    *file_contents* maps ``path -> (branch_content, base_content)``;
    ``get_repo_file`` raises ``RuntimeError`` (mirroring a real 404) when the
    requested ref's content is ``None``.
    """
    from coord import github_ops

    # #2989: the sweep now passes a per-pass `cache=` dict (dedup), so the
    # stub must accept it. Calls are recorded so a test can assert the dedup
    # actually happened.
    calls: dict[str, list] = {"branch_exists": [], "pr_is_merged": []}

    def _fake_branch_exists(repo, branch, *, cache=None):
        key = (repo, branch)
        if cache is not None and key in cache:
            return cache[key]
        calls["branch_exists"].append(key)
        result = branch_exists
        if cache is not None:
            cache[key] = result
        return result

    def _fake_pr_is_merged(repo, branch):
        calls["pr_is_merged"].append((repo, branch))
        return pr_merged

    monkeypatch.setattr(github_ops, "branch_exists_on_remote", _fake_branch_exists)
    monkeypatch.setattr(github_ops, "pr_is_merged", _fake_pr_is_merged)
    monkeypatch.setattr(
        github_ops,
        "branch_commits_ahead",
        lambda repo, base, branch: ahead,
    )
    monkeypatch.setattr(
        github_ops,
        "get_compare_files",
        lambda repo, base, branch: changed_files,
    )

    _contents = file_contents or {}

    def _fake_get_repo_file(repo, path, ref):
        branch_content, base_content = _contents.get(path, (None, None))
        content = branch_content if ref != "main" else base_content
        if content is None:
            raise RuntimeError("404")
        return content

    monkeypatch.setattr(github_ops, "get_repo_file", _fake_get_repo_file)
    return calls


def test_falsely_merged_row_with_differing_content_is_flagged(
    monkeypatch, config
) -> None:
    """The #2639 headline case: a `test-author` row flipped to `status=
    'merged'` whose branch is still ahead of main, has no merged PR at its
    tip, and whose changed file's content genuinely differs from main's
    current copy — this is exactly the live `test-author-ms-1-slice-10-v2`
    casualty (#2639's blast-radius sweep)."""
    a = _merged_row()
    board = Board(completed=[a])
    _patch_false_merge_probes(
        monkeypatch,
        ahead=1,
        changed_files=["tests/acceptance/ms-1/10-up-mapping.spec.ts"],
        file_contents={
            "tests/acceptance/ms-1/10-up-mapping.spec.ts": ("new content", "old content"),
        },
    )

    actions = reconcile_board_merges(board, config)

    assert any("POSSIBLY LOST" in s and "ta-1" in s for s in actions)
    # DETECTION ONLY — the row itself must be untouched.
    assert a.status == "merged"


def test_falsely_merged_sweep_skips_when_branch_already_deleted(
    monkeypatch, config
) -> None:
    """The dominant, benign case: the branch was deleted after a real merge
    + cleanup — nothing to flag."""
    a = _merged_row()
    board = Board(completed=[a])
    _patch_false_merge_probes(monkeypatch, branch_exists=False)

    actions = reconcile_board_merges(board, config)

    assert not any("POSSIBLY LOST" in s for s in actions)


def test_falsely_merged_sweep_skips_when_pr_is_merged_confirms_tip(
    monkeypatch, config
) -> None:
    """`pr_is_merged` (#1150, SHA-exact) already confirms this exact tip
    merged — correctly tracked, no flag."""
    a = _merged_row()
    board = Board(completed=[a])
    _patch_false_merge_probes(monkeypatch, pr_merged=True)

    actions = reconcile_board_merges(board, config)

    assert not any("POSSIBLY LOST" in s for s in actions)


def test_falsely_merged_sweep_skips_when_zero_commits_ahead(monkeypatch, config) -> None:
    """False positive #1 from the live #2639 blast-radius sweep:
    `issue-2531-config-portal-project-repo-mapping` — the branch's tip is an
    ancestor of main, so it's 0 commits ahead despite no merged PR. Content
    is fully present; never flag."""
    a = _merged_row()
    board = Board(completed=[a])
    _patch_false_merge_probes(monkeypatch, ahead=0)

    actions = reconcile_board_merges(board, config)

    assert not any("POSSIBLY LOST" in s for s in actions)


def test_falsely_merged_sweep_skips_when_ahead_is_unconfirmable(
    monkeypatch, config
) -> None:
    """`branch_commits_ahead` fails open to ``None`` on a `gh` error — never
    treated as "0 commits ahead" (that would wrongly clear a real loss) NOR
    as evidence of loss; skip, fail open."""
    a = _merged_row()
    board = Board(completed=[a])
    _patch_false_merge_probes(monkeypatch, ahead=None)

    actions = reconcile_board_merges(board, config)

    assert not any("POSSIBLY LOST" in s for s in actions)


def test_falsely_merged_sweep_skips_when_content_matches_base_verbatim(
    monkeypatch, config
) -> None:
    """False positive #2 from the live #2639 blast-radius sweep: the
    coord-portal `issue-16-gate-a-...` row — a rebase moved the SHA but the
    content is byte-identical on main. SHA mismatch alone must never be
    treated as proof of loss."""
    a = _merged_row()
    board = Board(completed=[a])
    _patch_false_merge_probes(
        monkeypatch,
        ahead=1,
        changed_files=["contract.md"],
        file_contents={"contract.md": ("same content", "same content")},
    )

    actions = reconcile_board_merges(board, config)

    assert not any("POSSIBLY LOST" in s for s in actions)


def test_falsely_merged_sweep_scoped_to_work_like_types(monkeypatch, config) -> None:
    """A `type='review'` row (never itself a WORK_LIKE_TYPES/interactive-
    merge-session candidate) must never reach this sweep even if somehow
    `status='merged'` — matches every other sweep's WORK_LIKE_TYPES scope."""
    a = _merged_row(row_type="review")
    board = Board(completed=[a])
    _patch_false_merge_probes(
        monkeypatch,
        ahead=1,
        changed_files=["x.py"],
        file_contents={"x.py": ("new", "old")},
    )

    actions = reconcile_board_merges(board, config)

    assert not any("POSSIBLY LOST" in s for s in actions)


def test_falsely_merged_sweep_respects_repo_and_issue_filters(
    monkeypatch, config
) -> None:
    a = _merged_row(assignment_id="ta-1", issue_number=16)
    b = _merged_row(assignment_id="ta-2", issue_number=99)
    board = Board(completed=[a, b])
    _patch_false_merge_probes(
        monkeypatch,
        ahead=1,
        changed_files=["x.py"],
        file_contents={"x.py": ("new", "old")},
    )

    actions = reconcile_board_merges(board, config, issue=16)

    assert any("ta-1" in s and "POSSIBLY LOST" in s for s in actions)
    assert not any("ta-2" in s for s in actions)


# ── #2989: bounding sweep (h)'s candidate set ───────────────────────────────
#
# Before this, the sweep selected EVERY `status='merged'` work-like row in
# the board — 1,302 rows on the drive host, proportional to project history,
# +1 per merge, never shrinking — and re-probed all of them against GitHub
# on the daemon's 30s tick. Measured: 1,304 of one pass's 1,346 `gh`
# invocations (97%). These tests pin the three mechanisms that bound it
# (terminal marker, recency cap, per-pass ref dedup) plus the cadence gate,
# and — critically — that NONE of them changed what the sweep concludes.


def _merged_rows(n: int, *, start: int = 1000) -> list[Assignment]:
    """*n* distinct merged work rows, one branch each, newest-issue last."""
    return [
        _merged_row(
            assignment_id=f"hist-{i}",
            issue_number=start + i,
            branch=f"issue-{start + i}-old-work",
            row_type="work",
        )
        for i in range(n)
    ]


def test_false_merge_candidate_set_does_not_scale_with_project_history(
    monkeypatch, config
) -> None:
    """THE headline acceptance bar: doubling merged history must not double
    the sweep's probe count. Every row here is a genuine flag (branch exists,
    no merged PR, ahead, content differs) so nothing is filtered out by the
    clean-marker — only the recency cap can bound this."""
    from coord.reconcile import _FALSE_MERGE_AUDIT_MAX_ROWS

    def _probe_count(n: int) -> int:
        board = Board(completed=_merged_rows(n))
        calls = _patch_false_merge_probes(
            monkeypatch,
            ahead=1,
            changed_files=["x.py"],
            file_contents={"x.py": ("new", "old")},
        )
        reconcile_board_merges(board, config, dry_run=True)
        return len(calls["branch_exists"])

    small = _probe_count(_FALSE_MERGE_AUDIT_MAX_ROWS * 2)
    large = _probe_count(_FALSE_MERGE_AUDIT_MAX_ROWS * 8)

    assert small == _FALSE_MERGE_AUDIT_MAX_ROWS
    # 4x the history, same cost — the whole point.
    assert large == small


def test_clean_audit_row_is_not_reprobed_on_the_next_pass(
    monkeypatch, config
) -> None:
    """A row confirmed correctly-merged is confirmed forever. The terminal
    marker makes the candidate set proportional to *unaudited* merges."""
    board = Board(completed=_merged_rows(5))
    # branch deleted from origin == the dominant benign case: merged + cleaned up.
    calls = _patch_false_merge_probes(monkeypatch, branch_exists=False)

    first = reconcile_board_merges(board, config)
    assert len(calls["branch_exists"]) == 5
    assert not any("POSSIBLY LOST" in s for s in first)

    calls["branch_exists"].clear()
    second = reconcile_board_merges(board, config)

    assert calls["branch_exists"] == []  # zero re-probes
    assert not any("POSSIBLY LOST" in s for s in second)


def test_clean_marker_records_every_terminal_verdict_shape(
    monkeypatch, config
) -> None:
    """All four "clean" exits are permanent and must all mark: branch gone,
    merged PR at the tip, 0 commits ahead, content byte-identical on base."""
    from coord import state

    shapes = [
        dict(branch_exists=False),
        dict(pr_merged=True),
        dict(ahead=0),
        dict(
            ahead=1,
            changed_files=["c.md"],
            file_contents={"c.md": ("same", "same")},
        ),
    ]
    for i, kwargs in enumerate(shapes):
        row = _merged_row(assignment_id=f"shape-{i}", issue_number=2000 + i)
        _patch_false_merge_probes(monkeypatch, **kwargs)
        reconcile_board_merges(Board(completed=[row]), config)

    clean = state.load_false_merge_audit_clean()
    assert {f"shape-{i}" for i in range(len(shapes))} <= clean


def test_fail_open_verdicts_are_never_marked_clean(monkeypatch, config) -> None:
    """`branch_commits_ahead` failing open to None (a `gh` error) is NOT
    evidence the row is fine — it must be re-probed next pass, or a transient
    GitHub blip would permanently retire a row from the audit."""
    from coord import state

    row = _merged_row(assignment_id="flaky-1")
    calls = _patch_false_merge_probes(monkeypatch, ahead=None)

    reconcile_board_merges(Board(completed=[row]), config)

    assert "flaky-1" not in state.load_false_merge_audit_clean()

    calls["branch_exists"].clear()
    reconcile_board_merges(Board(completed=[row]), config)
    assert calls["branch_exists"] != []  # re-probed


def test_flagged_row_is_never_marked_clean_and_reflags(monkeypatch, config) -> None:
    """A POSSIBLY LOST row must keep surfacing until an operator acts on it —
    marking it clean would hide the very thing the sweep exists to find."""
    from coord import state

    row = _merged_row(assignment_id="lost-1")
    board = Board(completed=[row])
    _patch_false_merge_probes(
        monkeypatch,
        ahead=1,
        changed_files=["x.py"],
        file_contents={"x.py": ("new", "old")},
    )

    first = reconcile_board_merges(board, config)
    assert any("POSSIBLY LOST" in s and "lost-1" in s for s in first)
    assert "lost-1" not in state.load_false_merge_audit_clean()

    second = reconcile_board_merges(board, config)
    assert any("POSSIBLY LOST" in s and "lost-1" in s for s in second)


def test_genuine_false_merge_still_detected_among_clean_history(
    monkeypatch, config
) -> None:
    """Regression bar: bounding must not change what the sweep CONCLUDES.
    One genuinely-lost row buried in otherwise-clean history is still found."""
    lost = _merged_row(assignment_id="lost-1", issue_number=42, branch="issue-42-work")
    board = Board(completed=[*_merged_rows(10), lost])

    from coord import github_ops

    _patch_false_merge_probes(
        monkeypatch,
        ahead=1,
        changed_files=["x.py"],
        file_contents={"x.py": ("new", "old")},
    )
    # Only `issue-42-work` is still ahead of base; the history rows were
    # merged and cleaned up normally.
    monkeypatch.setattr(
        github_ops,
        "branch_exists_on_remote",
        lambda repo, branch, *, cache=None: branch == "issue-42-work",
    )

    actions = reconcile_board_merges(board, config)

    assert any("POSSIBLY LOST" in s and "lost-1" in s for s in actions)
    assert lost.status == "merged"  # still detection-only


def test_branch_ref_lookups_are_deduped_within_one_pass(monkeypatch, config) -> None:
    """K rows sharing ONE branch make ONE ref lookup, not K. Measured on the
    live board: 1,304 lookups for 851 distinct refs (~453 wasted calls)."""
    rows = [
        _merged_row(
            assignment_id=f"sib-{i}",
            issue_number=77,
            branch="issue-77-shared",
            row_type="work",
        )
        for i in range(8)
    ]
    board = Board(completed=rows)
    calls = _patch_false_merge_probes(monkeypatch, branch_exists=False)

    reconcile_board_merges(board, config, dry_run=True)

    assert calls["branch_exists"] == [("acme/api", "issue-77-shared")]


def test_branch_exists_on_remote_cache_is_caller_scoped(monkeypatch) -> None:
    """The `cache=` seam itself: memoised within one dict, and a FRESH dict
    re-reads (branch existence must never be cached across passes)."""
    seen: list[str] = []

    def _fake_gh(*args, **kwargs):
        seen.append(args[-1])
        return ""

    monkeypatch.setattr(github_ops, "_gh", _fake_gh)

    cache: dict = {}
    assert github_ops.branch_exists_on_remote("acme/api", "b", cache=cache) is True
    assert github_ops.branch_exists_on_remote("acme/api", "b", cache=cache) is True
    assert len(seen) == 1
    # different branch, same cache -> its own lookup
    github_ops.branch_exists_on_remote("acme/api", "other", cache=cache)
    assert len(seen) == 2
    # fresh per-pass cache -> re-read
    github_ops.branch_exists_on_remote("acme/api", "b", cache={})
    assert len(seen) == 3
    # no cache at all -> unchanged legacy behaviour
    github_ops.branch_exists_on_remote("acme/api", "b")
    assert len(seen) == 4


def test_dry_run_does_not_persist_the_clean_marker(monkeypatch, config) -> None:
    """`--dry-run` must not change what the next real pass does."""
    from coord import state

    board = Board(completed=[_merged_row(assignment_id="dr-1")])
    _patch_false_merge_probes(monkeypatch, branch_exists=False)

    reconcile_board_merges(board, config, dry_run=True)

    assert "dr-1" not in state.load_false_merge_audit_clean()


def test_targeted_issue_audit_bypasses_the_clean_marker(monkeypatch, config) -> None:
    """An operator who asks for `--issue N` gets a real re-probe, even for a
    row previously retired as clean — otherwise the marker would make a
    suspected row permanently un-recheckable by hand."""
    row = _merged_row(assignment_id="ta-1", issue_number=16)
    board = Board(completed=[row])
    calls = _patch_false_merge_probes(monkeypatch, branch_exists=False)

    reconcile_board_merges(board, config)
    assert len(calls["branch_exists"]) == 1

    calls["branch_exists"].clear()
    reconcile_board_merges(board, config, issue=16)

    assert len(calls["branch_exists"]) == 1


def test_throttled_audit_runs_at_most_hourly(monkeypatch, config) -> None:
    """The daemon's 30s tick opts into throttling: sweep (h) detects a rare,
    non-urgent condition, so it gets one turn an hour instead of 120."""
    import coord.reconcile as rec

    board = Board(completed=_merged_rows(3))
    calls = _patch_false_merge_probes(monkeypatch, ahead=None)  # never marked clean

    now = [1_000_000.0]
    monkeypatch.setattr(rec.time, "time", lambda: now[0])

    reconcile_board_merges(board, config, throttle_false_merge_audit=True)
    assert len(calls["branch_exists"]) == 3

    calls["branch_exists"].clear()
    now[0] += 30.0  # the very next daemon tick
    reconcile_board_merges(board, config, throttle_false_merge_audit=True)
    assert calls["branch_exists"] == []

    now[0] += rec._FALSE_MERGE_AUDIT_MIN_INTERVAL_SECONDS + 1
    reconcile_board_merges(board, config, throttle_false_merge_audit=True)
    assert len(calls["branch_exists"]) == 3


def test_unthrottled_manual_run_always_sweeps(monkeypatch, config) -> None:
    """A manual `coord reconcile-merges` is deliberately NOT throttled — the
    operator asked, and the before/after measurement in #2989 has to be
    deterministic."""
    board = Board(completed=_merged_rows(3))
    calls = _patch_false_merge_probes(monkeypatch, ahead=None)

    reconcile_board_merges(board, config, throttle_false_merge_audit=True)
    calls["branch_exists"].clear()
    reconcile_board_merges(board, config)  # default: no throttle

    assert len(calls["branch_exists"]) == 3


def test_rows_for_unmapped_repos_do_not_consume_the_recency_cap(
    monkeypatch, config
) -> None:
    """An orphaned row (repo since dropped from coordinator.yml) is
    unprobeable, so it must be filtered BEFORE the cap — otherwise a pile of
    them starves the rows the sweep can actually audit."""
    from coord.reconcile import _FALSE_MERGE_AUDIT_MAX_ROWS

    orphans = [
        Assignment(
            machine_name="laptop",
            repo_name="gone",
            issue_number=3000 + i,
            issue_title="t",
            status="merged",
            assignment_id=f"orphan-{i}",
            branch=f"issue-{3000 + i}-x",
            type="work",
        )
        for i in range(_FALSE_MERGE_AUDIT_MAX_ROWS * 2)
    ]
    real = _merged_rows(3)
    board = Board(completed=orphans + real)
    calls = _patch_false_merge_probes(monkeypatch, branch_exists=False)

    reconcile_board_merges(board, config, dry_run=True)

    assert len(calls["branch_exists"]) == 3


def test_throttled_dry_run_does_not_consume_the_real_runs_turn(
    monkeypatch, config
) -> None:
    """A throttled `--dry-run` must not stamp the hourly clock — otherwise a
    read-only preview would silently suppress the next real sweep."""
    import coord.reconcile as rec

    board = Board(completed=_merged_rows(2))
    calls = _patch_false_merge_probes(monkeypatch, ahead=None)
    monkeypatch.setattr(rec.time, "time", lambda: 2_000_000.0)

    reconcile_board_merges(
        board, config, dry_run=True, throttle_false_merge_audit=True
    )
    calls["branch_exists"].clear()
    reconcile_board_merges(board, config, throttle_false_merge_audit=True)

    assert len(calls["branch_exists"]) == 2
