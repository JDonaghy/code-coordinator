"""Tests for coord.merge_queue — sequencing logic and the gh-driven processor."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from unittest.mock import patch

import pytest

from coord import merge_queue as mq
from coord.ci_store import CheckRun, JobRun, JobStep
from coord.merge_queue import (
    CONFLICT,
    MERGED,
    MERGING,
    PENDING,
    QueuedMerge,
    enqueue,
    load_queue,
    pending_summary,
    process,
    reorder,
    save_queue,
    sequence,
)
from coord.models import Assignment
from coord import db as db_mod
from coord import sql
from tests import backends
from tests.test_db import (
    AbortOnErrorConn,
    abort_simulating_connection,
    schema_migrated_sqlite_connection,
)


def _check(
    name: str,
    *,
    status: str = "completed",
    conclusion: str | None = "failure",
    run_id: str = "1",
) -> CheckRun:
    """#1892 test helper: a `CheckRun` with a settable `run_id`, needed to
    exercise `_ci_infra_reason`'s per-run job lookup — mirrors
    `tests/test_ci_store.py`'s `_check`, which has no `run_id` parameter."""
    return CheckRun(
        name=name, status=status, conclusion=conclusion,
        url=f"https://gh/runs/{run_id}", run_id=run_id,
        started_at=None, completed_at=None,
    )


def _q(
    aid: str,
    *,
    repo: str = "api",
    repo_github: str = "acme/api",
    branch: str | None = None,
    target: str = "main",
    size: int | None = None,
    state: str = PENDING,
    pr: int | None = None,
    assignment_type: str = "work",
    required_gates: list[str] | None = None,
) -> QueuedMerge:
    return QueuedMerge(
        assignment_id=aid,
        repo_name=repo,
        repo_github=repo_github,
        branch=branch or f"worker/{aid}",
        target_branch=target,
        issue_number=1,
        issue_title="t",
        state=state,
        size=size,
        pr_number=pr,
        assignment_type=assignment_type,
        required_gates=required_gates if required_gates is not None else [],
    )


# ── Pure logic ───────────────────────────────────────────────────────────────

class TestSequence:
    def test_sorts_by_size_ascending(self) -> None:
        items = [_q("a", size=500), _q("b", size=50), _q("c", size=100)]
        ordered = sequence(items)
        assert [x.assignment_id for x in ordered] == ["b", "c", "a"]

    def test_unknown_size_goes_last_and_tiebreaks_by_id(self) -> None:
        items = [_q("z"), _q("a"), _q("m", size=10)]
        ordered = sequence(items)
        assert [x.assignment_id for x in ordered] == ["m", "a", "z"]

    def test_only_pending_returned(self) -> None:
        items = [
            _q("a", size=10, state=PENDING),
            _q("b", size=5, state=MERGED),
            _q("c", size=20, state=CONFLICT),
        ]
        assert [x.assignment_id for x in sequence(items)] == ["a"]


class TestReorder:
    def test_explicit_order_wins(self) -> None:
        items = [_q("a", size=10), _q("b", size=20), _q("c", size=5)]
        out = reorder(items, ["b", "a"])
        assert [x.assignment_id for x in out] == ["b", "a", "c"]

    def test_unknown_ids_dropped(self) -> None:
        items = [_q("a"), _q("b")]
        out = reorder(items, ["ghost", "a"])
        assert [x.assignment_id for x in out] == ["a", "b"]


# ── Persistence (SQLite-based) ────────────────────────────────────────────────

class TestPersistence:
    def test_roundtrip(self, coord_db) -> None:
        items = [_q("a", size=10), _q("b", size=20)]
        save_queue(items)
        again = load_queue()
        assert [x.assignment_id for x in again] == ["a", "b"]
        assert again[0].size == 10

    def test_load_empty_returns_empty(self, coord_db) -> None:
        assert load_queue() == []

    def test_save_replaces_all(self, coord_db) -> None:
        save_queue([_q("old")])
        save_queue([_q("new1"), _q("new2")])
        items = load_queue()
        assert [x.assignment_id for x in items] == ["new1", "new2"]

    def test_roundtrip_preserves_assignment_type(self, coord_db) -> None:
        # #1077: assignment_type must survive a save/load cycle so the merge
        # processor can still tell a mock-author entry apart after a daemon
        # restart re-reads the queue from disk.
        save_queue([_q("a", assignment_type="mock-author"), _q("b")])
        again = {x.assignment_id: x.assignment_type for x in load_queue()}
        assert again == {"a": "mock-author", "b": "work"}

    def test_roundtrip_preserves_required_gates(self, coord_db) -> None:
        # #1213: a label-resolved gate list must survive a save/load cycle
        # so the merge gate stays commit-bound after a daemon restart.
        save_queue([_q("a", required_gates=["merge"]), _q("b")])
        again = {x.assignment_id: x.required_gates for x in load_queue()}
        assert again == {"a": ["merge"], "b": []}

    def test_roundtrip_preserves_ci_infra_reruns(self, coord_db) -> None:
        # #1892: the auto-rerun cap must survive a save/load cycle, or a
        # daemon restart mid-way through a verdictless-CI-failure streak
        # would reset the counter and let a broken workflow rerun forever.
        a = _q("a")
        a.ci_infra_reruns = 2
        save_queue([a, _q("b")])
        again = {x.assignment_id: x.ci_infra_reruns for x in load_queue()}
        assert again == {"a": 2, "b": 0}

    def test_roundtrip_preserves_ci_stale_reruns(self, coord_db) -> None:
        # #2197: same durability requirement as ci_infra_reruns above, for
        # the CI-staleness auto-rerun's own independent counter.
        a = _q("a")
        a.ci_stale_reruns = 2
        save_queue([a, _q("b")])
        again = {x.assignment_id: x.ci_stale_reruns for x in load_queue()}
        assert again == {"a": 2, "b": 0}

    def test_roundtrip_preserves_ci_flaky_reruns_and_pending(self, coord_db) -> None:
        # #2252: same durability requirement as ci_infra_reruns/
        # ci_stale_reruns above — a daemon restart mid-way through a
        # pending flake re-check must not lose track of the failure it's
        # waiting on (ci_flaky_pending) or reset the one-shot budget
        # (ci_flaky_reruns), which would let the re-run fire again.
        a = _q("a")
        a.ci_flaky_reruns = 1
        a.ci_flaky_pending = '{"checks": [{"name": "e2e", "conclusion": "failure"}], "sha": "abc123"}'
        save_queue([a, _q("b")])
        again = {x.assignment_id: (x.ci_flaky_reruns, x.ci_flaky_pending) for x in load_queue()}
        assert again == {"a": (1, a.ci_flaky_pending), "b": (0, "")}

    def test_roundtrip_preserves_ci_unreadable_reruns(self, coord_db) -> None:
        # #2347: same durability requirement as ci_infra_reruns/
        # ci_stale_reruns/ci_flaky_reruns above — a daemon restart mid-way
        # through a run of GitHub-unreachable reads must not lose track of
        # the count and let the "escalate the wording" ceiling never fire.
        a = _q("a")
        a.ci_unreadable_reruns = 2
        save_queue([a, _q("b")])
        again = {x.assignment_id: x.ci_unreadable_reruns for x in load_queue()}
        assert again == {"a": 2, "b": 0}

    def test_roundtrip_preserves_ci_fix_detail_cache(self, coord_db) -> None:
        # #3114 review fix: the cached CI-failure-detail fetch (keyed by the
        # branch_head_sha it was fetched for) must survive a save/load
        # cycle — this is the whole point of caching it on the persisted
        # queue row rather than in-process: `coord merge` is a fresh CLI
        # invocation each tick, so an in-memory-only cache would never
        # actually avoid the repeat `gh api .../logs` fetch across ticks.
        a = _q("a")
        a.ci_fix_detail_sha = "deadbeef"
        a.ci_fix_detail_json = '{"check_name": "Test", "job_name": "Test"}'
        save_queue([a, _q("b")])
        again = {
            x.assignment_id: (x.ci_fix_detail_sha, x.ci_fix_detail_json)
            for x in load_queue()
        }
        assert again == {
            "a": ("deadbeef", '{"check_name": "Test", "job_name": "Test"}'),
            "b": ("", None),
        }


class TestSaveQueueLockContention:
    """#2802: `save_queue`'s DELETE+re-INSERT rewrite must ride out
    transient `database is locked` collisions the same way every other
    write in the codebase does (#2597/#2538) — the bug report's `notify`
    drain died mid-batch when this write lost a lock race to a concurrent
    writer, aborting review dispatch for every entry behind it in the same
    drain."""

    class _FlakyConn:
        """Wraps a real (in-memory) connection, raising `database is
        locked` on the first *fail_times* `execute()` calls before
        delegating to the real one. Also forwards the `with conn:`
        transaction protocol `save_queue` uses, since sqlite3.Connection's
        context manager (not just `.execute`/`.commit`) is what actually
        drives the DELETE+INSERT batch's commit/rollback here, and
        delegates `fetchone`/`fetchall`/etc. to the most recent real
        cursor — `coord.sql.execute` returns whatever `.cursor()`
        produced (this fake, standing in for the connection) as if it
        were the cursor itself, and `load_queue`'s read after the retried
        write exercises that same path.
        """

        __module__ = "sqlite3"

        def __init__(self, real_conn, fail_times: int) -> None:
            self._real = real_conn
            self._fail_times = fail_times
            self.calls = 0
            self._last_cursor = None

        def cursor(self):
            return self

        def execute(self, *args, **kwargs):
            self.calls += 1
            if self.calls <= self._fail_times:
                import sqlite3

                raise sqlite3.OperationalError("database is locked")
            self._last_cursor = self._real.execute(*args, **kwargs)
            return self._last_cursor

        def commit(self):
            return self._real.commit()

        def __enter__(self):
            self._real.__enter__()
            return self

        def __exit__(self, exc_type, exc, tb):
            return self._real.__exit__(exc_type, exc, tb)

        def __getattr__(self, name):
            if self._last_cursor is not None:
                return getattr(self._last_cursor, name)
            raise AttributeError(name)

    def test_retries_through_transient_lock_then_persists(
        self, coord_db, monkeypatch
    ) -> None:
        monkeypatch.setattr("coord.db.time.sleep", lambda s: None)
        flaky = self._FlakyConn(coord_db, fail_times=2)
        monkeypatch.setattr("coord.merge_queue.get_connection", lambda: flaky)

        save_queue([_q("a", size=10)])

        assert flaky.calls >= 3  # two collisions, then the write that lands
        items = load_queue()
        assert [x.assignment_id for x in items] == ["a"], (
            "the queue must land durably once the lock clears — never lost "
            "to a transient lock collision"
        )

    def test_raises_once_the_retry_budget_is_exhausted(
        self, coord_db, monkeypatch
    ) -> None:
        """A lock that never clears must still surface to the caller —
        `coord.notify`'s drain is the layer responsible for logging and
        moving on, not this one silently swallowing it and leaving the
        on-disk queue looking unchanged when a save was actually dropped."""
        import sqlite3

        monkeypatch.setattr("coord.db.time.sleep", lambda s: None)
        flaky = self._FlakyConn(coord_db, fail_times=999)
        monkeypatch.setattr("coord.merge_queue.get_connection", lambda: flaky)

        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            save_queue([_q("a", size=10)])


class TestEnqueue:
    def _assignment(self, *, branch: str | None = "worker/foo") -> Assignment:
        return Assignment(
            machine_name="m", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="abc", branch=branch, status="done",
        )

    def test_enqueue_appends(self, coord_db) -> None:
        entry = enqueue(self._assignment(), repo_github="acme/api", target_branch="main")
        assert entry is not None
        assert load_queue()[0].assignment_id == "abc"

    def test_enqueue_carries_assignment_type(self, coord_db) -> None:
        # #1077: the queued entry must remember the originating assignment's
        # type so `process()` can decide whether merging closes the issue.
        a = Assignment(
            machine_name="m", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="ga", branch="worker/ga", status="done",
            type="mock-author",
        )
        entry = enqueue(a, repo_github="acme/api", target_branch="main")
        assert entry is not None
        assert entry.assignment_type == "mock-author"
        assert load_queue()[0].assignment_type == "mock-author"

    def test_enqueue_snapshots_required_gates(self, coord_db) -> None:
        # #1213: a label-resolved gate list on the assignment must be
        # snapshotted onto the queue entry at enqueue time (commit-bound).
        a = Assignment(
            machine_name="m", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="ga", branch="worker/ga", status="done",
            required_gates=["merge"],
        )
        entry = enqueue(a, repo_github="acme/api", target_branch="main")
        assert entry is not None
        assert entry.required_gates == ["merge"]
        assert load_queue()[0].required_gates == ["merge"]

    def test_enqueue_untagged_work_gets_empty_required_gates(self, coord_db) -> None:
        # Untagged work (no label override) must snapshot [] — the fallback
        # sentinel — not None, so requires_review/requires_smoke fall back to
        # config.pipeline.default_gates unchanged (#1213 compatibility contract).
        entry = enqueue(self._assignment(), repo_github="acme/api", target_branch="main")
        assert entry is not None
        assert entry.required_gates == []

    def test_idempotent(self, coord_db) -> None:
        enqueue(self._assignment(), repo_github="acme/api", target_branch="main")
        second = enqueue(self._assignment(), repo_github="acme/api", target_branch="main")
        assert second is None
        assert len(load_queue()) == 1

    def test_skipped_when_no_branch(self, coord_db) -> None:
        a = self._assignment(branch=None)
        assert enqueue(a, repo_github="acme/api", target_branch="main") is None
        assert load_queue() == []

    def test_dedup_by_branch_not_assignment_id(self, coord_db) -> None:
        """#274: a second work assignment on the same branch — fix-1 in the
        auto-loop, or the PR-creator dispatched by ``coord pr`` — must not
        produce a duplicate queue row."""
        first = Assignment(
            machine_name="m", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="orig", branch="issue-1-foo", status="done",
        )
        fix = Assignment(
            machine_name="m", repo_name="api", issue_number=1, issue_title="[fix-1] t",
            assignment_id="fix1", branch="issue-1-foo", status="done",
        )
        assert enqueue(first, repo_github="acme/api", target_branch="main") is not None
        assert enqueue(fix, repo_github="acme/api", target_branch="main") is None
        items = load_queue()
        assert len(items) == 1
        assert items[0].assignment_id == "orig"

    def test_different_branch_same_repo_still_enqueues(self, coord_db) -> None:
        """Sanity: dedup is scoped to (repo_github, branch), not repo alone."""
        a1 = Assignment(
            machine_name="m", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="a1", branch="issue-1-foo", status="done",
        )
        a2 = Assignment(
            machine_name="m", repo_name="api", issue_number=2, issue_title="t",
            assignment_id="a2", branch="issue-2-bar", status="done",
        )
        assert enqueue(a1, repo_github="acme/api", target_branch="main") is not None
        assert enqueue(a2, repo_github="acme/api", target_branch="main") is not None
        assert len(load_queue()) == 2


# ── Processing with a stub gh ops ────────────────────────────────────────────

@dataclass
class FakeGh:
    """Stub the surface in `coord.merge_queue.GhOps`."""

    sizes: dict[int, int] = field(default_factory=dict)
    merge_results: dict[int, tuple[bool, str]] = field(default_factory=dict)
    create_calls: list[tuple[str, dict]] = field(default_factory=list)
    merge_calls: list[tuple[str, int, str]] = field(default_factory=list)
    close_calls: list[tuple[str, int]] = field(default_factory=list)
    close_raises: bool = False
    next_pr: int = 100
    # #1196 hole 2 (PR-body lint): PR number -> body text; issue number ->
    # whether it currently has open children. Defaults keep every prior
    # test (none of which set these) inert — get_pr_body returns "" so
    # `process()`'s lint step is a no-op, matching pre-#1196 behavior.
    pr_bodies: dict[int, str] = field(default_factory=dict)
    open_children: set[int] = field(default_factory=set)
    edit_body_calls: list[tuple[str, int, str]] = field(default_factory=list)
    # #1318: PR number -> commit messages on that PR; issue number -> whether
    # it carries the epic/tracking label. Defaults keep every prior test
    # (none of which set these) inert — get_pr_commit_messages returns []
    # and is_epic_issue returns False, matching pre-#1318 behavior.
    pr_commit_messages: dict[int, list[str]] = field(default_factory=dict)
    epic_issues: set[int] = field(default_factory=set)
    # #1477: PR number -> mergeable verdict (True/False/None). Defaults keep
    # every prior test (none of which set this) inert — check_pr_mergeable
    # returns None ("unknown") so reconcile_conflict_entries never unparks
    # an entry unless a test opts in explicitly.
    mergeable_results: dict[int, bool | None] = field(default_factory=dict)
    mergeable_calls: list[tuple[str, int]] = field(default_factory=list)
    # #1467: PR number -> whether the branch carries a merge commit
    # (True/False/None). Defaults keep every prior test (none of which set
    # this) inert — branch_has_merge_commit returns None ("unknown") so the
    # pre-flight squash fallback never fires unless a test opts in.
    merge_commit_results: dict[int, bool | None] = field(default_factory=dict)
    merge_commit_calls: list[tuple[str, int]] = field(default_factory=list)
    # #1624: branch name -> already-open PR dict ({"number", "url"}). Defaults
    # keep every prior test (none of which set this) inert —
    # find_pr_for_branch returns None so the dry-run "no PR yet" path runs,
    # matching pre-#1624 behavior.
    existing_prs: dict[str, dict] = field(default_factory=dict)
    find_pr_calls: list[tuple[str, str]] = field(default_factory=list)
    # #2143: branch names already merged by "another driver" — checked
    # immediately before `create_pr` so a duplicate PR is never opened
    # against a branch that merged while this run was mid-flight (e.g. a
    # `--revalidate` CI-settle wait). Defaults keep every prior test (none
    # of which set this) inert — `pr_is_merged` returns False for every
    # branch, matching pre-#2143 behavior.
    merged_branches: set[str] = field(default_factory=set)
    pr_is_merged_calls: list[tuple[str, str]] = field(default_factory=list)
    # #2948: branch name -> resolved live preview-deployment URL. Defaults
    # keep every prior test (none of which set this) inert —
    # `get_pr_deployment_url` returns None for every branch, matching the
    # "can't confirm a URL" fail-closed default `evaluate_uat_verdict` relies
    # on when a repo opts into `uat_live_preview` but the fake has nothing
    # configured for that branch.
    deployment_urls: dict[str, str] = field(default_factory=dict)
    deployment_url_calls: list[tuple[str, str]] = field(default_factory=list)

    def get_pr_deployment_url(self, repo: str, branch: str) -> str | None:
        self.deployment_url_calls.append((repo, branch))
        return self.deployment_urls.get(branch)

    def find_pr_for_branch(self, repo: str, branch: str) -> dict | None:
        self.find_pr_calls.append((repo, branch))
        return self.existing_prs.get(branch)

    def pr_is_merged(self, repo: str, branch: str) -> bool:
        self.pr_is_merged_calls.append((repo, branch))
        return branch in self.merged_branches

    def create_pr(self, repo: str, *, base: str, head: str, title: str, body: str) -> dict:
        self.create_calls.append((repo, {"base": base, "head": head, "title": title}))
        pr_num = self.next_pr
        self.next_pr += 1
        self.pr_bodies.setdefault(pr_num, body)
        return {"number": pr_num, "url": f"https://gh/x/{pr_num}", "existed": False}

    def get_pr_size(self, repo: str, number: int) -> int:
        return self.sizes.get(number, 100)

    def merge_pr(self, repo: str, number: int, method: str = "rebase") -> tuple[bool, str]:
        self.merge_calls.append((repo, number, method))
        return self.merge_results.get(number, (True, "merged"))

    def close_issue(self, repo: str, issue_number: int) -> None:
        self.close_calls.append((repo, issue_number))
        if self.close_raises:
            raise RuntimeError("gh issue close failed")

    def get_branch_sha(self, repo: str, branch: str) -> str | None:
        # Tests don't exercise SHA tracking by default; return None so the
        # backward-compatible "no SHA → skip staleness check" path runs.
        return None

    def get_branch_patch_id(self, repo: str, base: str, branch: str) -> str | None:
        # #1475: tests don't exercise patch-id tracking by default; return
        # None so the fail-closed "no patch-id → stale on SHA mismatch"
        # path runs, matching get_branch_sha's default above.
        return None

    def get_compare_files(self, repo: str, base: str, head: str) -> list[str] | None:
        # #1738: tests don't exercise the inert-base-move check by default;
        # return None so `_base_move_is_inert` fails closed ("not proven
        # inert" → stale on SHA mismatch), matching the two defaults above.
        return None

    def get_pr_body(self, repo: str, number: int) -> str:
        return self.pr_bodies.get(number, "")

    def edit_pr_body(self, repo: str, number: int, body: str) -> None:
        self.edit_body_calls.append((repo, number, body))
        self.pr_bodies[number] = body

    def has_open_children(self, repo: str, issue_number: int) -> bool:
        return issue_number in self.open_children

    def is_epic_issue(self, repo: str, issue_number: int) -> bool:
        return issue_number in self.epic_issues

    def get_pr_commit_messages(self, repo: str, number: int) -> list[str]:
        return self.pr_commit_messages.get(number, [])

    def check_pr_mergeable(self, repo: str, number: int) -> bool | None:
        self.mergeable_calls.append((repo, number))
        return self.mergeable_results.get(number)

    def branch_has_merge_commit(self, repo: str, number: int) -> bool | None:
        self.merge_commit_calls.append((repo, number))
        return self.merge_commit_results.get(number)


class TestProcess:
    def test_opens_pr_sizes_and_merges_in_size_order(self) -> None:
        items = [_q("big"), _q("small"), _q("mid")]
        gh = FakeGh(sizes={100: 500, 101: 10, 102: 100})
        events = process(items, gh)

        # PRs opened in original order
        opened = [e.entry.assignment_id for e in events if e.kind == "opened"]
        assert opened == ["big", "small", "mid"]
        # Merge order driven by size: small (101) → mid (102) → big (100)
        merge_seq = [c[1] for c in gh.merge_calls]
        assert merge_seq == [101, 102, 100]
        # All entries left in MERGED state
        assert {x.state for x in items} == {MERGED}

    def test_skips_pr_creation_when_branch_already_merged(self) -> None:
        # #2143: an entry with no pr_number yet (e.g. a stale in-memory
        # snapshot resolved before another merge driver merged this exact
        # branch, or a full `--revalidate` CI-settle wait that ran long
        # enough for a sibling driver to land it) must not get a second,
        # purposeless PR opened against it.
        items = [_q("a", branch="issue-1-done")]
        gh = FakeGh(merged_branches={"issue-1-done"})
        events = process(items, gh)

        assert gh.create_calls == []
        assert items[0].state == MERGED
        assert items[0].error is None
        already_merged = [e for e in events if e.kind == "already_merged"]
        assert len(already_merged) == 1
        assert "issue-1-done" in already_merged[0].message
        # No merge attempted either — nothing to merge, it's already done.
        assert gh.merge_calls == []

    def test_pr_is_merged_check_is_optional_on_gh_ops_stub(self) -> None:
        # #2143: `pr_is_merged` is optional on GhOps, same contract as
        # `find_pr_for_branch`/`branch_has_merge_commit` — a stub predating
        # #2143 must keep opening PRs exactly as before.
        @dataclass
        class _NoMergedCheckGh(FakeGh):
            # Shadow the inherited method with plain `None`, so
            # `getattr(gh_ops, "pr_is_merged", None)` sees the same
            # "missing method" shape a pre-#2143 stub would.
            pr_is_merged = None

        items = [_q("a")]
        gh = _NoMergedCheckGh()
        events = process(items, gh)
        assert len(gh.create_calls) == 1
        assert items[0].state == MERGED
        assert any(e.kind == "opened" for e in events)

    def test_dry_run_previews_already_merged_instead_of_would_open(self) -> None:
        # #2143: the dry-run preview mirrors the real path's check so it
        # never claims "would open PR" for a branch another driver already
        # merged.
        items = [_q("a", branch="issue-1-done")]
        gh = FakeGh(merged_branches={"issue-1-done"})
        events = process(items, gh, dry_run=True)

        assert gh.create_calls == []
        already_merged = [e for e in events if e.kind == "already_merged"]
        assert len(already_merged) == 1
        assert "(dry run)" in already_merged[0].message
        assert not any(
            e.kind == "opened" and "would open PR" in e.message for e in events
        )

    def test_closes_linked_issue_on_merge(self) -> None:
        # #806: a successful merge must close the linked issue deterministically,
        # not rely on the worker having put `Closes #N` in the PR body.
        items = [_q("a")]
        process(items, gh := FakeGh())
        assert items[0].state == MERGED
        assert gh.close_calls == [(items[0].repo_github, items[0].issue_number)]

    def test_close_failure_does_not_revert_merge(self) -> None:
        # #806: closing is best-effort — a `gh issue close` failure must leave
        # the merge standing and surface a warning, never undo MERGED.
        items = [_q("a")]
        events = process(items, FakeGh(close_raises=True))
        assert items[0].state == MERGED
        merged = [e for e in events if e.kind == "merged"]
        assert merged and "could not close" in merged[0].message

    def test_dry_run_does_not_close(self) -> None:
        # #806: dry-run never reaches the real merge path, so no issue is closed.
        items = [_q("a")]
        process(items, gh := FakeGh(), dry_run=True)
        assert gh.close_calls == []

    def test_mock_author_merge_does_not_close_tracking_issue(self) -> None:
        # #1077: a "mock-author" (Gate A) entry's issue_number is the
        # milestone's tracking issue, not something the PR resolves — merging
        # it must NOT close that issue, unlike a "work" entry (#806 above).
        items = [_q("a", assignment_type="mock-author")]
        events = process(items, gh := FakeGh())
        assert items[0].state == MERGED
        assert gh.close_calls == []
        merged = [e for e in events if e.kind == "merged"]
        assert merged and "left open" in merged[0].message

    def test_epic_decompose_merge_does_not_close_the_epic(self) -> None:
        # #3132 acceptance: merging an "epic-decompose" entry's PR must
        # leave the epic OPEN — asserted here against `process()`'s actual
        # behavior (the `close_issue` call it does or doesn't make), not
        # just the PR body text a separate test already covers above.
        items = [_q("a", assignment_type="epic-decompose")]
        events = process(items, gh := FakeGh())
        assert items[0].state == MERGED
        assert gh.close_calls == []
        merged = [e for e in events if e.kind == "merged"]
        assert merged and "left open" in merged[0].message

    def test_successful_merge_dismisses_the_drive_escalation(self, coord_db) -> None:
        # #1767: a merge landing through `coord merge` is exactly the kind
        # of resolution that should clear a stale drive escalation for the
        # same issue — nothing else in the pipeline ever does.
        from coord import state

        state._record_drive_escalation_local(
            "api", 1,
            stage="merge",
            reason="merge: BLOCKED — smoke_required",
            gate_readings="smoke=missing",
            proposed_command="coord merge --only a",
        )
        items = [_q("a")]  # repo="api", issue_number=1 per the `_q` helper
        process(items, FakeGh())

        assert items[0].state == MERGED
        assert state._get_drive_escalation_local("api", 1) is None

    def test_successful_merge_with_no_escalation_is_a_noop(self, coord_db) -> None:
        # #1767: dismissing must be safe (and silent) when there was never
        # an escalation on file — no existence check should be required.
        from coord import state

        items = [_q("a")]
        process(items, FakeGh())

        assert items[0].state == MERGED
        assert state._get_drive_escalation_local("api", 1) is None

    def test_dry_run_never_dismisses_the_drive_escalation(self, coord_db) -> None:
        # #1767: a dry-run preview never reaches the real merge path, so it
        # must not touch the escalation record either.
        from coord import state

        state._record_drive_escalation_local(
            "api", 1,
            stage="merge",
            reason="merge: BLOCKED — smoke_required",
            gate_readings="smoke=missing",
            proposed_command="coord merge --only a",
        )
        items = [_q("a")]
        process(items, FakeGh(), dry_run=True)

        assert state._get_drive_escalation_local("api", 1) is not None

    def test_unrelated_escalation_is_untouched_by_a_merge(self, coord_db) -> None:
        # #1767: dismissal is scoped to the exact (repo, issue) that merged —
        # an escalation for a different issue must survive.
        from coord import state

        state._record_drive_escalation_local(
            "api", 999,
            stage="merge",
            reason="merge: BLOCKED — smoke_required",
            gate_readings="smoke=missing",
            proposed_command="coord merge --only other",
        )
        items = [_q("a")]  # issue_number=1, distinct from the seeded #999
        process(items, FakeGh())

        assert items[0].state == MERGED
        assert state._get_drive_escalation_local("api", 999) is not None

    def test_briefing_body_uses_refs_for_mock_author(self) -> None:
        # #1077: the fallback create_pr body (when no PR was opened upstream)
        # must use the non-closing "Refs #N" for mock-author entries.
        from coord.merge_queue import _briefing_body

        entry = _q("a", assignment_type="mock-author")
        body = _briefing_body(entry)
        assert "Refs #1" in body
        assert "Closes #1" not in body

    def test_briefing_body_uses_closes_for_work(self) -> None:
        # #1077: "work" entries keep the #806 closing-keyword behavior.
        from coord.merge_queue import _briefing_body

        entry = _q("a", assignment_type="work")
        body = _briefing_body(entry)
        assert body.startswith("Closes #1\n\n")

    def test_briefing_body_uses_refs_for_epic_decompose(self) -> None:
        # #3132: like mock-author, epic-decompose's issue_number is the
        # epic itself — the fallback create_pr body must use the
        # non-closing "Refs #N", never "Closes #N".
        from coord.merge_queue import _briefing_body

        entry = _q("a", assignment_type="epic-decompose")
        body = _briefing_body(entry)
        assert "Refs #1" in body
        assert "Closes #1" not in body

    def test_conflict_does_not_halt_other_repo_groups(self) -> None:
        """A conflict in one (repo, target) group must not touch other groups."""
        items = [
            _q("a", size=10),
            _q("other", repo="ui", repo_github="acme/ui", size=5),
        ]
        gh = FakeGh(
            sizes={100: 10, 101: 5},
            merge_results={100: (False, "Merge conflict")},
        )
        events = process(items, gh)
        states = {x.assignment_id: x.state for x in items}
        assert states["a"] == CONFLICT
        # Different repo group still processes
        assert states["other"] == MERGED
        kinds = [e.kind for e in events]
        assert "conflict" in kinds

    def test_conflict_parks_entry_and_sibling_still_merges(self) -> None:
        """#735: a conflicting entry is parked (CONFLICT) and siblings in the
        same (repo, target) group continue to merge — no group-wide halt."""
        items = [
            _q("a", size=10),
            _q("b", size=20),
        ]
        # PR 100 → `a` (first opened), PR 101 → `b`
        gh = FakeGh(
            sizes={100: 10, 101: 20},
            merge_results={100: (False, "Merge conflict")},
        )
        events = process(items, gh, presorted=True)
        states = {x.assignment_id: x.state for x in items}
        # Conflicting entry is parked
        assert states["a"] == CONFLICT
        # Sibling in the same group still merges (#735)
        assert states["b"] == MERGED
        kinds = [e.kind for e in events]
        assert "conflict" in kinds
        assert "merged" in kinds

    def test_dry_run_no_gh_calls(self) -> None:
        items = [_q("a"), _q("b")]
        gh = FakeGh()
        events = process(items, gh, dry_run=True)
        assert gh.create_calls == []
        assert gh.merge_calls == []
        assert all(e.kind in ("opened", "merged") for e in events)
        # State untouched in dry-run
        assert all(x.state == PENDING for x in items)

    def test_skips_terminal_entries(self) -> None:
        items = [
            _q("done", state=MERGED, pr=1),
            _q("pending", size=10),
        ]
        gh = FakeGh()
        process(items, gh)
        # No second call for the already-merged entry
        assert all(c[1] != 1 for c in gh.merge_calls)

    # ── #1196 hole 2: pre-merge PR-body closing-keyword lint ──────────────

    def test_downgrades_worker_pr_body_closes_for_epic_with_open_children(self) -> None:
        # GitHub's own closing-keyword magic reads the PR body directly at
        # merge time and never calls github_ops.close_issue — the only
        # place that can stop it is a pre-merge scan/rewrite.
        items = [_q("a", pr=100, size=10)]
        gh = FakeGh(pr_bodies={100: "Closes #1041\n\nWorker-authored PR."}, open_children={1041})
        events = process(items, gh)
        assert items[0].state == MERGED
        assert gh.edit_body_calls == [
            ("acme/api", 100, "Refs #1041\n\nWorker-authored PR.")
        ]
        downgraded = [e for e in events if e.kind == "pr_body_downgraded"]
        assert downgraded and "#1041" in downgraded[0].message

    def test_leaves_regular_pr_body_untouched(self) -> None:
        # No regression for the common case: a PR body closing a regular
        # (childless) issue is never rewritten.
        items = [_q("a", pr=100, size=10)]
        gh = FakeGh(pr_bodies={100: "Closes #55"}, open_children=set())
        process(items, gh)
        assert gh.edit_body_calls == []

    def test_lint_ignores_pr_body_with_no_closing_keyword(self) -> None:
        items = [_q("a", pr=100, size=10)]
        gh = FakeGh(pr_bodies={100: "Refs #1041, unrelated context."}, open_children={1041})
        process(items, gh)
        assert gh.edit_body_calls == []

    def test_lint_failure_never_blocks_the_merge(self) -> None:
        # Best-effort throughout: a get_pr_body/has_open_children/
        # edit_pr_body failure must not prevent (or revert) a merge.
        class _BoomOnBody(FakeGh):
            def get_pr_body(self, repo: str, number: int) -> str:
                raise RuntimeError("gh pr view failed")

        items = [_q("a", pr=100, size=10)]
        gh = _BoomOnBody()
        process(items, gh)
        assert items[0].state == MERGED

    # ── #1318: pre-merge epic-closing-keyword guard (commit messages) ─────

    def test_epic_closing_keyword_in_commit_blocks_merge(self) -> None:
        # The #1314 incident: the PR body carries no closing keyword at all,
        # but a commit message on the branch does — GitHub's own scanner
        # reads commit messages verbatim once they land on the base branch,
        # so this must block the merge, not just lint the PR body.
        items = [_q("a", pr=100, size=10)]
        gh = FakeGh(
            pr_commit_messages={100: [
                "fix(#1314): harden downstream breakages\n\n"
                "...its body carry \"Closes #1120\", which GitHub's native..."
            ]},
            epic_issues={1120},
        )
        events = process(items, gh)
        assert items[0].state == PENDING  # never merged
        assert gh.merge_calls == []
        blocked = [e for e in events if e.kind == "epic_closing_keyword_in_commit"]
        assert blocked and "#1120" in blocked[0].message
        assert items[0].error is not None and "#1120" in items[0].error

    def test_ordinary_closing_keyword_in_commit_passes_through(self) -> None:
        # Acceptance criterion from #1318: an ordinary `Closes #<non-epic>`
        # in a commit message must merge untouched — no epic label, no block.
        items = [_q("a", pr=100, size=10)]
        gh = FakeGh(
            pr_commit_messages={100: ["fix(#55): a normal bug fix\n\nCloses #55"]},
            epic_issues=set(),
        )
        events = process(items, gh)
        assert items[0].state == MERGED
        assert not [e for e in events if "epic_closing_keyword" in e.kind]

    def test_force_merge_overrides_but_still_warns(self) -> None:
        # The override must never be silent — a warning event still fires
        # even though the merge proceeds.
        items = [_q("a", pr=100, size=10)]
        gh = FakeGh(
            pr_commit_messages={100: ["Closes #1120"]},
            epic_issues={1120},
        )
        events = process(items, gh, force_merge=True)
        assert items[0].state == MERGED
        forced = [
            e for e in events if e.kind == "epic_closing_keyword_in_commit_forced"
        ]
        assert forced and "#1120" in forced[0].message

    def test_commit_message_lint_failure_never_blocks_the_merge(self) -> None:
        # Best-effort: a get_pr_commit_messages/is_epic_issue failure must
        # not itself prevent a merge.
        class _BoomOnCommits(FakeGh):
            def get_pr_commit_messages(self, repo: str, number: int) -> list[str]:
                raise RuntimeError("gh pr view --json commits failed")

        items = [_q("a", pr=100, size=10)]
        gh = _BoomOnCommits()
        process(items, gh)
        assert items[0].state == MERGED

    def test_pr_body_downgraded_for_epic_label_with_no_open_children(self) -> None:
        # #1318 widens the existing #1196 body downgrade: a fresh epic with
        # zero open children yet must still be protected, not just an epic
        # that already has open sub-issues.
        items = [_q("a", pr=100, size=10)]
        gh = FakeGh(
            pr_bodies={100: "Closes #1120\n\nWorker-authored PR."},
            open_children=set(),
            epic_issues={1120},
        )
        events = process(items, gh)
        assert items[0].state == MERGED
        assert gh.edit_body_calls == [
            ("acme/api", 100, "Refs #1120\n\nWorker-authored PR.")
        ]
        downgraded = [e for e in events if e.kind == "pr_body_downgraded"]
        assert downgraded and "#1120" in downgraded[0].message


class TestProcessLinearityFallback:
    """#1467: `gh pr merge --rebase` refuses any branch containing a merge
    commit ("This branch can't be rebased") — a linearity failure GitHub's
    `mergeable` field can't predict. process() pre-flight-checks for a merge
    commit and falls back to --squash, which is always valid here."""

    def test_falls_back_to_squash_when_branch_has_merge_commit(self) -> None:
        items = [_q("a", pr=100, size=10)]
        gh = FakeGh(merge_commit_results={100: True})
        events = process(items, gh, method="rebase")

        assert items[0].state == MERGED
        assert gh.merge_calls == [("acme/api", 100, "squash")]
        fallback = [e for e in events if e.kind == "method_fallback"]
        assert len(fallback) == 1
        assert "squash" in fallback[0].message
        assert "#1467" in fallback[0].message

    def test_stays_on_rebase_when_branch_is_linear(self) -> None:
        items = [_q("a", pr=100, size=10)]
        gh = FakeGh(merge_commit_results={100: False})
        events = process(items, gh, method="rebase")

        assert items[0].state == MERGED
        assert gh.merge_calls == [("acme/api", 100, "rebase")]
        assert not [e for e in events if e.kind == "method_fallback"]

    def test_fail_closed_on_inconclusive_probe(self) -> None:
        # merge_commit_results defaults to {} -> None (inconclusive). The
        # method must stay unchanged rather than guess.
        items = [_q("a", pr=100, size=10)]
        gh = FakeGh()
        events = process(items, gh, method="rebase")

        assert gh.merge_calls == [("acme/api", 100, "rebase")]
        assert not [e for e in events if e.kind == "method_fallback"]

    def test_fail_closed_when_probe_raises(self) -> None:
        class RaisingGh(FakeGh):
            def branch_has_merge_commit(self, repo: str, number: int) -> bool | None:
                raise RuntimeError("gh timeout")

        items = [_q("a", pr=100, size=10)]
        gh = RaisingGh()
        events = process(items, gh, method="rebase")

        assert gh.merge_calls == [("acme/api", 100, "rebase")]
        assert not [e for e in events if e.kind == "method_fallback"]

    def test_backward_compatible_with_gh_ops_lacking_the_probe(self) -> None:
        # A pre-#1467 stub GhOps without branch_has_merge_commit at all must
        # keep working — getattr(..., None) fails closed, same as an
        # inconclusive read. A standalone class (not a FakeGh subclass) so
        # the method is genuinely absent, not merely deleted.
        class LegacyGh:
            def __init__(self) -> None:
                self.merge_calls: list[tuple[str, int, str]] = []

            def get_pr_size(self, repo: str, number: int) -> int:
                return 10

            def merge_pr(self, repo: str, number: int, method: str = "rebase"):
                self.merge_calls.append((repo, number, method))
                return True, "merged"

            def close_issue(self, repo: str, issue_number: int) -> None:
                pass

            def get_pr_body(self, repo: str, number: int) -> str:
                return ""

            def has_open_children(self, repo: str, issue_number: int) -> bool:
                return False

            def is_epic_issue(self, repo: str, issue_number: int) -> bool:
                return False

            def get_pr_commit_messages(self, repo: str, number: int) -> list[str]:
                return []

        items = [_q("a", pr=100, size=10)]
        gh = LegacyGh()
        events = process(items, gh, method="rebase")

        assert gh.merge_calls == [("acme/api", 100, "rebase")]
        assert not [e for e in events if e.kind == "method_fallback"]
        assert not hasattr(gh, "branch_has_merge_commit")

    def test_no_probe_when_method_is_not_rebase(self) -> None:
        # squash/merge never hit the "can't be rebased" refusal — no need
        # to spend a `gh api` round trip checking.
        items = [_q("a", pr=100, size=10)]
        gh = FakeGh(merge_commit_results={100: True})
        process(items, gh, method="squash")

        assert gh.merge_commit_calls == []
        assert gh.merge_calls == [("acme/api", 100, "squash")]


class TestProcessDryRunLinearityPreview:
    """#1467-review: `coord merge --dry-run` previews the review/smoke gates
    but, before this, never previewed the rebase→squash fallback — a
    dry-run over an entry already carrying a merge commit silently said
    "would merge ... via --rebase" even though the real run would fall
    back to --squash. Only reachable when the entry already has a
    pr_number (from an earlier non-dry-run attempt), since dry-run itself
    never opens a PR and the probe needs one to query.
    """

    def test_previews_squash_fallback_when_pr_already_exists(self) -> None:
        items = [_q("a", pr=100, size=10)]
        gh = FakeGh(merge_commit_results={100: True})
        events = process(items, gh, method="rebase", dry_run=True)

        assert gh.merge_calls == []  # dry-run never actually merges
        fallback = [e for e in events if e.kind == "method_fallback"]
        assert len(fallback) == 1
        assert "dry run" in fallback[0].message
        assert "squash" in fallback[0].message
        merged = [e for e in events if e.kind == "merged"]
        assert merged and "--squash" in merged[0].message

    def test_no_preview_without_a_prior_pr_number(self) -> None:
        # A brand-new entry has no pr_number yet in dry-run (dry-run never
        # creates one) — nothing to probe, so no fallback preview and the
        # merge preview reports the requested method unchanged.
        items = [_q("a", size=10)]
        gh = FakeGh(merge_commit_results={100: True})
        events = process(items, gh, method="rebase", dry_run=True)

        assert not [e for e in events if e.kind == "method_fallback"]
        merged = [e for e in events if e.kind == "merged"]
        assert merged and "--rebase" in merged[0].message

    def test_fail_closed_on_inconclusive_probe_in_dry_run(self) -> None:
        items = [_q("a", pr=100, size=10)]
        gh = FakeGh()  # merge_commit_results defaults to {} -> None
        events = process(items, gh, method="rebase", dry_run=True)

        assert not [e for e in events if e.kind == "method_fallback"]
        merged = [e for e in events if e.kind == "merged"]
        assert merged and "--rebase" in merged[0].message


class TestProcessRealGithubOpsChokepoint:
    """#1196 acceptance criterion: 'Dispatching type="work" against an epic
    with an open child and merging it leaves the epic OPEN' — driven through
    the REAL `coord.github_ops` module wired in as `gh_ops` (only the `gh`
    subprocess boundary is faked), not `FakeGh`'s `close_raises` stand-in.
    This exercises the actual #1196 chokepoint end to end: both hole 1 (a
    "work" assignment whose issue_number IS the epic) and hole 2 (the PR
    body's own `Closes #<epic>` keyword) in one pass.

    The `fake_gh` stubs below take `**_kwargs` (#2988): `_gh` now carries a
    `caller=` attribution tag that every `github_ops` call site passes, and a
    positional-only stub would raise TypeError instead of answering. These
    tests assert on which `gh` subcommand ran, never on `_gh`'s keywords, so
    absorbing them is the intended shape — not a loosened assertion.
    """

    def test_type_work_direct_on_epic_with_open_child_stays_open(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from coord import github_ops as real_gh_ops

        epic_json = json.dumps({
            "number": 1041, "title": "Epic", "state": "open", "milestone": None,
            "labels": [], "body": "## Sub-issues\n- [ ] #1039\n- [x] #1040\n",
        })

        def fake_gh(*args: str, **_kwargs: object) -> str:
            if args[:2] == ("pr", "list"):
                return "[]"
            if args[:2] == ("pr", "create"):
                return "https://github.com/acme/api/pull/500"
            if args[:2] == ("pr", "view"):
                return json.dumps({
                    "body": "Closes #1041\n\nAutomated merge from the coordinator."
                })
            if args[:2] == ("issue", "view"):
                return epic_json
            if args[:2] == ("pr", "edit"):
                return ""
            if args[:2] == ("pr", "merge"):
                return "merged"
            if args[:2] == ("api", "graphql"):
                # #1354: the close-guard also does a live batch state
                # lookup; no live answer here, so it falls back to the
                # checkbox in epic_json above (#1039 unticked -> open).
                raise RuntimeError("gh api graphql: not available in this test")
            raise AssertionError(f"unexpected gh call: {args}")

        monkeypatch.setattr(real_gh_ops, "_gh", fake_gh)

        def _boom_subprocess(*a, **k):
            raise AssertionError(
                "must never reach the real `gh issue close` subprocess call "
                "— the epic has an open child"
            )

        monkeypatch.setattr(real_gh_ops.subprocess, "run", _boom_subprocess)

        entry = _q("w1", repo="api", repo_github="acme/api", target="main", size=10)
        entry.issue_number = 1041  # #1196 hole 1: the epic itself, type="work"

        events = process([entry], real_gh_ops)

        # Merge succeeded — the PR itself lands.
        assert entry.state == MERGED
        # But the epic was never closed: the chokepoint's guard refused.
        merged_events = [e for e in events if e.kind == "merged"]
        assert merged_events
        assert "could not close" in merged_events[0].message
        assert "open children" in merged_events[0].message.lower()
        assert "#1039" in merged_events[0].message
        # Hole 2: the PR body's own `Closes #1041` was downgraded pre-merge.
        downgrade_events = [e for e in events if e.kind == "pr_body_downgraded"]
        assert downgrade_events and "#1041" in downgrade_events[0].message

    def test_commit_message_closes_epic_blocks_merge(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#1318 acceptance criterion: a branch commit message containing
        `Closes #<epic>` is caught even though the PR body itself is clean
        — the #1196 body-lint alone can't see this (the actual #1314/#1120
        incident: the closing keyword sat in the *commit message*'s
        explanatory prose, not the PR body)."""
        from coord import github_ops as real_gh_ops

        epic_json = json.dumps({
            "number": 1120, "title": "Epic", "state": "open", "milestone": None,
            "labels": [{"name": "epic"}], "body": "",
        })
        calls: list[tuple[str, ...]] = []

        def fake_gh(*args: str, **_kwargs: object) -> str:
            calls.append(args)
            if args[:2] == ("pr", "list"):
                return "[]"
            if args[:2] == ("pr", "create"):
                return "https://github.com/acme/api/pull/500"
            if args[:2] == ("pr", "view"):
                if args[-1] == "commits":
                    return json.dumps({"commits": [{
                        "messageHeadline": 'fix(#1314): harden downstream breakages',
                        "messageBody": (
                            "...its body carry \"Closes #1120\", which "
                            "GitHub's native closing-keyword auto-close "
                            "used to close the epic..."
                        ),
                    }]})
                return json.dumps({"body": "Automated merge from the coordinator."})
            if args[:2] == ("issue", "view"):
                return epic_json
            raise AssertionError(f"unexpected gh call: {args}")

        monkeypatch.setattr(real_gh_ops, "_gh", fake_gh)

        def _boom_subprocess(*a, **k):
            raise AssertionError(
                "must never reach a real `gh` subprocess call — the merge "
                "is refused before `gh pr merge`/`gh issue close`"
            )

        monkeypatch.setattr(real_gh_ops.subprocess, "run", _boom_subprocess)

        entry = _q("w1", repo="api", repo_github="acme/api", target="main", size=10)
        entry.issue_number = 200  # ordinary work issue; the epic only appears in the commit prose

        events = process([entry], real_gh_ops)

        assert entry.state == PENDING  # refused, never merged
        assert not any(a[:2] == ("pr", "merge") for a in calls)
        blocked = [e for e in events if e.kind == "epic_closing_keyword_in_commit"]
        assert blocked and "#1120" in blocked[0].message

    def test_commit_message_closes_ordinary_issue_merges_through(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Counterpart acceptance criterion from #1318: an ordinary `Closes
        #<non-epic>` in a commit message merges through untouched — the
        guard only fires for epic-labelled targets."""
        from coord import github_ops as real_gh_ops

        ordinary_json = json.dumps({
            "number": 55, "title": "Bug", "state": "open", "milestone": None,
            "labels": [], "body": "",
        })

        def fake_gh(*args: str, **_kwargs: object) -> str:
            if args[:2] == ("pr", "list"):
                return "[]"
            if args[:2] == ("pr", "create"):
                return "https://github.com/acme/api/pull/500"
            if args[:2] == ("pr", "view"):
                if args[-1] == "commits":
                    return json.dumps({"commits": [{
                        "messageHeadline": "fix(#55): a normal bug fix",
                        "messageBody": "Closes #55",
                    }]})
                return json.dumps({"body": "Automated merge from the coordinator."})
            if args[:2] == ("issue", "view"):
                return ordinary_json
            if args[:2] == ("pr", "merge"):
                return "merged"
            raise AssertionError(f"unexpected gh call: {args}")

        monkeypatch.setattr(real_gh_ops, "_gh", fake_gh)

        class _FakeCompleted:
            returncode = 0
            stderr = ""

        def _fake_run(cmd, **kwargs):
            # `gh issue close` (#806's deterministic close path) shells out
            # via subprocess.run directly, not `_gh`.
            return _FakeCompleted()

        monkeypatch.setattr(real_gh_ops.subprocess, "run", _fake_run)

        entry = _q("w2", repo="api", repo_github="acme/api", target="main", size=10)
        entry.issue_number = 55

        events = process([entry], real_gh_ops)

        assert entry.state == MERGED
        assert not [e for e in events if "epic_closing_keyword" in e.kind]


class _ExpectedRedGh(FakeGh):
    """#2164: FakeGh + the API-only surface
    `coord.acceptance.clear_expected_red_via_pr` needs. Defaults to a
    single ms-dir manifest mapping issue 1 (the default `_q()` issue
    number) with one `expected_red` id — enough for the clearing sweep to
    find something and succeed end-to-end unless a test overrides it.
    """

    def __init__(self, *, manifest_text: str | None = None, branch_sha: str | None = "cafesha", **kw):
        super().__init__(**kw)
        self.manifest_text = manifest_text if manifest_text is not None else (
            "tests:\n  ms01::a: 1\nexpected_red:\n  1:\n    - ms01::a\n"
        )
        self._branch_sha = branch_sha
        self.update_repo_file_calls: list[tuple[str, str]] = []

    def get_branch_sha(self, repo: str, branch: str) -> str | None:
        return self._branch_sha

    def list_repo_subdirs(self, repo: str, path: str, branch: str = "develop") -> list[str]:
        return ["ms01"]

    def get_repo_file_with_sha(self, repo: str, path: str, branch: str = "develop") -> tuple[str, str]:
        if path != "tests/acceptance/ms01/manifest.yml":
            raise RuntimeError("not found")
        return self.manifest_text, "blob-sha"

    def get_default_branch_head(self, repo: str, branch: str) -> str:
        return "default-tip-sha"

    def create_remote_branch(self, repo: str, branch: str, sha: str) -> bool:
        return True

    def update_repo_file(
        self, repo: str, path: str, branch: str, content: str, message: str, *, sha: str,
    ) -> str:
        self.update_repo_file_calls.append((path, content))
        return "new-sha"


class _PatchIdGh(_ExpectedRedGh):
    """#2298: `_ExpectedRedGh` + a controllable `get_branch_patch_id`, keyed
    by the *branch* arg (either the live PR branch name or a bare
    acceptance_sha) so a test can simulate "same content, different SHA"
    (a pure rebase) independently of `get_branch_sha`/`branch_head_sha`.
    """

    def __init__(self, *, patch_ids: dict[str, str | None], **kw):
        super().__init__(**kw)
        self._patch_ids = patch_ids
        self.patch_id_calls: list[tuple[str, str, str]] = []

    def get_branch_patch_id(self, repo: str, base: str, branch: str) -> str | None:
        self.patch_id_calls.append((repo, base, branch))
        return self._patch_ids.get(branch)


class _RelocatedExpectedRedGh(_ExpectedRedGh):
    """#2896 review: `_ExpectedRedGh`, but the ms-dir manifest lives under
    the RELOCATED (entrypoint-linked) acceptance root — `tui/tests/
    acceptance/ms01/manifest.yml`, where a `tui-tuidriver` route's slices
    now live — and nothing at all sits under the shared repo-root tree.
    `list_repo_subdirs` honours the path it is asked about (the real
    `github_ops` one does), so a sweep that only ever looks at
    `tests/acceptance/` finds nothing here, exactly as it would on the
    real fleet."""

    RELOCATED = "tui/tests/acceptance/ms01/manifest.yml"

    def list_repo_subdirs(self, repo: str, path: str, branch: str = "develop") -> list[str]:
        if path.rstrip("/") != "tui/tests/acceptance":
            raise RuntimeError(f"not found: {path}")
        return ["ms01"]

    def get_repo_file_with_sha(self, repo: str, path: str, branch: str = "develop") -> tuple[str, str]:
        if path != self.RELOCATED:
            raise RuntimeError("not found")
        return self.manifest_text, "blob-sha"


def _relocated_config():
    """A `Config` whose only repo routes `tui/**` through an
    entrypoint-linked driver — so `acceptance_search_roots("api")` is
    `["tests/acceptance/", "tui/tests/acceptance/"]` (#2896)."""
    from coord.config import AcceptanceConfig, AcceptanceDriverConfig, Config
    from coord.models import Repo

    return Config(
        repos=[Repo(name="api", github="acme/api")],
        machines=[],
        acceptance=AcceptanceConfig(drivers={
            "api": AcceptanceDriverConfig(routes=[
                AcceptanceDriverConfig(match="coord/**", kind="cli-pytest", run="pytest"),
                AcceptanceDriverConfig(
                    match="tui/**", kind="tui-tuidriver", run="cargo test",
                    entrypoint="tui/tests/acceptance.rs",
                ),
            ]),
        }),
    )


class TestExpectedRedClearForRelocatedMilestone:
    """#2896 review (blocking, impact 1): with the slices relocated under
    `tui/tests/acceptance/`, the merge queue's `expected_red` lookup — which
    hardcoded the repo-root tree — found nothing, so
    `_maybe_clear_expected_red` took its "not in scope for the oracle loop
    at all" branch and returned bare `None`: no clear, no `MergeEvent`, no
    audit row. That is precisely the #2199 regression its own docstring says
    was fixed ("indistinguishable from #1965's genuine vacuous-assertion
    alarm"), reintroduced for every relocated milestone."""

    @staticmethod
    def _board(completed):
        from coord.models import Board
        return Board(active=[], completed=list(completed))

    @staticmethod
    def _work(*, acceptance_state=None, acceptance_sha=None) -> Assignment:
        return Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="w1", type="work", status="done", branch="worker/w1",
            acceptance_state=acceptance_state, acceptance_sha=acceptance_sha,
        )

    def test_clears_a_relocated_milestones_entries(self) -> None:
        board = self._board([self._work(acceptance_state="passed", acceptance_sha="cafesha")])
        gh = _RelocatedExpectedRedGh()

        events = process(
            [_q("w1", size=10)], gh, board=board, config=_relocated_config(),
            # The review/smoke gates only engage once a `config` is present;
            # they are orthogonal to the expected_red sweep under test.
            skip_review=True, skip_smoke=True,
        )

        assert events[-1].kind == "expected_red_clear"
        assert "ms01::a" in events[-1].message
        assert gh.update_repo_file_calls
        assert gh.update_repo_file_calls[0][0] == _RelocatedExpectedRedGh.RELOCATED
        assert "expected_red" not in gh.update_repo_file_calls[0][1]

    def test_without_a_config_the_sweep_degrades_to_the_legacy_root(self) -> None:
        """The documented fallback, pinned (`coord.acceptance.
        search_roots_for_repo(None, ...)`): a caller with no `config` in
        hand degrades to the legacy repo-root tree rather than raising — so
        it finds nothing here. This is the shape of the bug the three tests
        above fix; it stays only as the no-config degradation contract, not
        as behaviour any live `coord merge` takes (`process` always has a
        `config`)."""
        board = self._board([self._work(acceptance_state="passed", acceptance_sha="cafesha")])
        gh = _RelocatedExpectedRedGh()

        events = process([_q("w1", size=10)], gh, board=board)

        assert not gh.update_repo_file_calls
        assert not [e for e in events if e.kind == "expected_red_clear"]

    def test_no_acceptance_skip_is_still_named_for_a_relocated_milestone(self) -> None:
        """The silent-`None` half of the same regression: an in-scope issue
        with no passing trust-gate verdict must still get its loud,
        actionable diagnostic (#2199), not be misread as out of scope."""
        board = self._board([self._work()])  # acceptance_state=None
        gh = _RelocatedExpectedRedGh()

        events = process(
            [_q("w1", size=10)], gh, board=board, config=_relocated_config(),
            # The review/smoke gates only engage once a `config` is present;
            # they are orthogonal to the expected_red sweep under test.
            skip_review=True, skip_smoke=True,
        )

        assert events[-1].kind == "expected_red_clear_skipped_no_acceptance"
        assert "coord acceptance record" in events[-1].message

    def test_out_of_scope_merge_stays_silent_with_a_config(self) -> None:
        """#2199 review finding 2, preserved: an issue with no
        `expected_red` entries under ANY root is genuinely out of scope and
        must still skip silently — threading `config` must not make the
        loud diagnostic fire fleet-wide."""
        board = self._board([self._work()])  # acceptance_state=None
        gh = _RelocatedExpectedRedGh(manifest_text="tests:\n  ms01::a: 1\n")

        events = process(
            [_q("w1", size=10)], gh, board=board, config=_relocated_config(),
            # The review/smoke gates only engage once a `config` is present;
            # they are orthogonal to the expected_red sweep under test.
            skip_review=True, skip_smoke=True,
        )

        assert not [e for e in events if e.kind.startswith("expected_red_clear")]


class TestExpectedRedClearOnMerge:
    """#2164 review (blocking finding 1): clearing `expected_red` must wait
    for the fix's own PR to actually merge into the default branch, not
    fire at `coord acceptance record` time. `process()` calls
    `coord.acceptance.clear_expected_red_via_pr` right after `gh_ops.
    merge_pr` succeeds for a `type="work"` entry whose acceptance was
    recorded "passed" against the exact SHA that merged."""

    @staticmethod
    def _board(active=None, completed=None):
        from coord.models import Board
        return Board(active=list(active or []), completed=list(completed or []))

    @staticmethod
    def _work(aid: str = "w1", *, acceptance_state=None, acceptance_sha=None) -> Assignment:
        return Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id=aid, type="work", status="done", branch=f"worker/{aid}",
            acceptance_state=acceptance_state, acceptance_sha=acceptance_sha,
        )

    def test_clears_expected_red_after_a_passed_acceptance_merges(self) -> None:
        work = self._work("w1", acceptance_state="passed", acceptance_sha="cafesha")
        board = self._board(completed=[work])
        gh = _ExpectedRedGh()

        events = process([_q("w1", size=10)], gh, board=board)

        assert events[-1].kind == "expected_red_clear"
        assert "ms01::a" in events[-1].message
        assert gh.update_repo_file_calls  # the manifest text was actually edited
        assert "expected_red" not in gh.update_repo_file_calls[0][1]

    def test_a_successful_clear_writes_a_durable_audit_row(self, coord_db) -> None:
        """#2266 scope 2: a clear isn't just a `coord merge` output line
        that scrolls past — it lands a queryable `audit_log` row too."""
        work = self._work("w1", acceptance_state="passed", acceptance_sha="cafesha")
        board = self._board(completed=[work])
        gh = _ExpectedRedGh()

        process([_q("w1", size=10)], gh, board=board)

        rows = coord_db.execute(
            "SELECT issue, repo FROM audit_log WHERE event_type = 'expected_red_clear'",
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["issue"] == 1

    def test_an_ordinary_merge_with_nothing_to_clear_writes_no_audit_row(
        self, coord_db,
    ) -> None:
        """#2266 review (blocking finding 1): both guards passing
        (acceptance recorded "passed" against the exact merged SHA) but
        the issue has no `expected_red` entries at all is the *common*
        case for an ordinary oracle-loop merge — not a failure. Before
        this fix, `clear_expected_red_via_pr`'s "no expected_red entries
        for this issue" message classified as "not cleared" just like a
        genuine failure, so every such merge wrote a persisted
        `expected_red_clear_failed` row."""
        work = self._work("w1", acceptance_state="passed", acceptance_sha="cafesha")
        board = self._board(completed=[work])
        gh = _ExpectedRedGh(manifest_text="tests:\n  ms01::a: 1\n")  # no expected_red: block

        events = process([_q("w1", size=10)], gh, board=board)

        assert events[-1].kind == "expected_red_clear_noop"
        assert not gh.update_repo_file_calls
        assert not coord_db.execute(
            "SELECT 1 FROM audit_log WHERE event_type LIKE 'expected_red_clear%'",
        ).fetchall()

    def test_names_the_skip_when_acceptance_was_never_recorded(self) -> None:
        """#2199: this used to be a silent `return None` — and, before
        #2199 gave `coord acceptance record` a call site at all, the
        UNIVERSAL case. A quiet skip here is indistinguishable from a
        clear that already happened, which is exactly how quadraui#542's
        `expected_red` entries got stuck permanently red. Must now name
        the reason instead."""
        work = self._work("w1")  # acceptance_state=None
        board = self._board(completed=[work])
        gh = _ExpectedRedGh()

        events = process([_q("w1", size=10)], gh, board=board)

        assert events[-1].kind == "expected_red_clear_skipped_no_acceptance"
        assert "acceptance_state=None" in events[-1].message
        assert "coord acceptance record" in events[-1].message
        assert not gh.update_repo_file_calls

    def test_no_acceptance_skip_writes_a_distinct_durable_audit_row(self, coord_db) -> None:
        """#2266 scope 3: "acceptance never recorded" is a different
        problem from a stale SHA (below) — it must be queryable as such,
        not just another line that scrolled past in `coord merge` output."""
        work = self._work("w1")  # acceptance_state=None
        board = self._board(completed=[work])
        gh = _ExpectedRedGh()

        process([_q("w1", size=10)], gh, board=board)

        rows = coord_db.execute(
            "SELECT issue FROM audit_log "
            "WHERE event_type = 'expected_red_clear_skipped_no_acceptance'",
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["issue"] == 1
        assert not coord_db.execute(
            "SELECT 1 FROM audit_log WHERE event_type = 'expected_red_clear_skipped_sha_mismatch'",
        ).fetchall()

    def test_names_the_skip_when_acceptance_failed(self) -> None:
        work = self._work("w1", acceptance_state="failed", acceptance_sha="cafesha")
        board = self._board(completed=[work])
        gh = _ExpectedRedGh()

        events = process([_q("w1", size=10)], gh, board=board)

        assert events[-1].kind == "expected_red_clear_skipped_no_acceptance"
        assert "acceptance_state='failed'" in events[-1].message
        assert not gh.update_repo_file_calls

    def test_no_diagnostic_when_the_manifest_has_no_expected_red_for_this_issue(self) -> None:
        """#2199 review (blocking finding 2): the loud "no passing
        trust-gate verdict" diagnostic must only fire for an issue actually
        in scope for the oracle trust gate. A manifest that covers this
        issue's tests but records no `expected_red` entry for it (e.g. an
        oracle-active issue whose sealed suite is already fully green, or
        one the manifest's `exempt:` list opted out) has nothing here for
        `coord acceptance record` to ever have cleared — the pre-#2199
        silent `None` is still correct, not a regression."""
        work = self._work("w1")  # acceptance_state=None
        board = self._board(completed=[work])
        gh = _ExpectedRedGh(manifest_text="tests:\n  ms01::a: 1\n")  # no expected_red: block

        events = process([_q("w1", size=10)], gh, board=board)

        assert not [e for e in events if e.kind.startswith("expected_red_clear")]

    def test_no_diagnostic_for_a_driverless_repo_with_no_acceptance_dir_at_all(self) -> None:
        """The overwhelmingly common case (#2199's issue): a repo with no
        `tests/acceptance/` directory at all. Must degrade to the same
        silent no-op as before #2199 — not the loud diagnostic, which
        would tell the operator to run a `coord acceptance record` that
        has no driver to run against."""
        work = self._work("w1")  # acceptance_state=None
        board = self._board(completed=[work])

        class _NoAcceptanceDirGh(_ExpectedRedGh):
            def list_repo_subdirs(self, repo: str, path: str, branch: str = "develop") -> list[str]:
                raise RuntimeError("404: tests/acceptance not found")

        events = process([_q("w1", size=10)], _NoAcceptanceDirGh(), board=board)

        assert not [e for e in events if e.kind.startswith("expected_red_clear")]

    def test_skips_and_warns_when_acceptance_sha_is_stale(self) -> None:
        """The recorded verdict is for a different commit than what just
        merged (branch moved after the last `record`) — must not clear on
        a stale observation."""
        work = self._work("w1", acceptance_state="passed", acceptance_sha="an-old-sha")
        board = self._board(completed=[work])
        gh = _ExpectedRedGh(branch_sha="cafesha")  # branch_head_sha != acceptance_sha

        events = process([_q("w1", size=10)], gh, board=board)

        assert events[-1].kind == "expected_red_clear_skipped"
        assert not gh.update_repo_file_calls

    def test_sha_mismatch_skip_writes_a_distinct_durable_audit_row(self, coord_db) -> None:
        """#2266 scope 3: the SHA-mismatch guard is a different problem
        from "acceptance never recorded" (above) — a distinct, queryable
        event_type, not the same silence either guard reached before."""
        work = self._work("w1", acceptance_state="passed", acceptance_sha="an-old-sha")
        board = self._board(completed=[work])
        gh = _ExpectedRedGh(branch_sha="cafesha")  # branch_head_sha != acceptance_sha

        process([_q("w1", size=10)], gh, board=board)

        rows = coord_db.execute(
            "SELECT issue FROM audit_log "
            "WHERE event_type = 'expected_red_clear_skipped_sha_mismatch'",
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["issue"] == 1
        assert not coord_db.execute(
            "SELECT 1 FROM audit_log WHERE event_type = 'expected_red_clear_skipped_no_acceptance'",
        ).fetchall()

    def test_mock_author_entries_never_trigger_a_clear(self) -> None:
        """`assignment_type="mock-author"` doesn't close an issue at all
        (#1077) — issue_number there is the milestone tracking issue, not
        a fix; must never attempt an expected_red clear for it."""
        board = self._board(completed=[
            self._work("w1", acceptance_state="passed", acceptance_sha="cafesha"),
        ])
        gh = _ExpectedRedGh()

        events = process([_q("w1", size=10, assignment_type="mock-author")], gh, board=board)

        assert not [e for e in events if e.kind.startswith("expected_red_clear")]

    def test_no_board_names_the_skip_instead_of_crashing(self) -> None:
        """board=None (a caller that can't supply one) must not crash —
        best-effort, same posture as every other lookup in this sweep —
        but #2199 still names it rather than staying silent."""
        gh = _ExpectedRedGh()
        events = process([_q("w1", size=10)], gh, board=None)
        assert events[-1].kind == "expected_red_clear_skipped_no_work"

    def test_gh_ops_lacking_the_api_surface_degrades_to_a_warning(self) -> None:
        """An older GhOps stub (predates #2164) that doesn't implement the
        new API-only methods must not crash process() — degrades to a
        'not supported' message, same optional-attribute convention as
        `branch_has_merge_commit`/`find_pr_for_branch`."""
        work = self._work("w1", acceptance_state="passed", acceptance_sha="cafesha")
        board = self._board(completed=[work])

        class _PlainFakeGh(FakeGh):
            def get_branch_sha(self, repo: str, branch: str) -> str | None:
                return "cafesha"

        events = process([_q("w1", size=10)], _PlainFakeGh(), board=board)

        # Must not crash `process()` — degrades to a harmless "found
        # nothing" event (classified "no_op", #2266 review blocking
        # finding 1) rather than an AttributeError, same fail-soft posture
        # the rest of the sweep uses when the API surface is missing
        # (`find_ms_manifest_for_issue_via_api` itself degrades to
        # "nothing found" for the same reason).
        assert events[-1].kind == "expected_red_clear_noop"
        assert events[-1].entry.state == MERGED

    def test_a_failed_clear_writes_a_distinct_durable_audit_row(self, coord_db) -> None:
        """#2266 scope 2: `clear_expected_red_via_pr` never raises — every
        failure degrades to a `warning: ...` string the caller can log.
        That must still be durable, with an event_type distinct from a
        real clear, so a repeat failure is queryable as "still stuck"."""
        work = self._work("w1", acceptance_state="passed", acceptance_sha="cafesha")
        board = self._board(completed=[work])

        class _CommitFailsGh(_ExpectedRedGh):
            def update_repo_file(self, repo, path, branch, content, message, *, sha):
                raise RuntimeError("boom")

        process([_q("w1", size=10)], _CommitFailsGh(), board=board)

        rows = coord_db.execute(
            "SELECT issue FROM audit_log WHERE event_type = 'expected_red_clear_failed'",
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["issue"] == 1
        assert not coord_db.execute(
            "SELECT 1 FROM audit_log WHERE event_type = 'expected_red_clear'",
        ).fetchall()

    # ── #2298: SHA mismatch ≠ content change ────────────────────────────

    def test_clears_when_sha_mismatches_but_patch_id_confirms_a_pure_rebase(
        self,
    ) -> None:
        """#2298: `checks_stale`/`smoke_required` force a rebase before a
        PR sitting behind a moved base can merge at all — which rewrites
        `branch_head_sha` even when nothing about the PR's own diff
        changed. A patch-id match (the branch's current diff against
        target == the diff `acceptance_sha` introduced against target)
        must clear exactly like an exact-SHA match does, not skip."""
        work = self._work("w1", acceptance_state="passed", acceptance_sha="old-sha")
        board = self._board(completed=[work])
        gh = _PatchIdGh(
            branch_sha="new-sha",  # branch_head_sha != acceptance_sha
            patch_ids={"worker/w1": "same-patch", "old-sha": "same-patch"},
        )

        events = process([_q("w1", size=10)], gh, board=board)

        assert events[-1].kind == "expected_red_clear"
        assert "ms01::a" in events[-1].message
        assert "patch-id" in events[-1].message  # names the arm it took
        assert gh.update_repo_file_calls  # the manifest text was actually edited

    def test_still_skips_when_sha_mismatches_and_patch_id_also_differs(self) -> None:
        """The counterpart: content genuinely changed after the verdict
        was recorded (a conflict resolved differently, an extra commit) —
        must still skip, same named event as a bare SHA mismatch."""
        work = self._work("w1", acceptance_state="passed", acceptance_sha="old-sha")
        board = self._board(completed=[work])
        gh = _PatchIdGh(
            branch_sha="new-sha",
            patch_ids={"worker/w1": "patch-new", "old-sha": "patch-old"},
        )

        events = process([_q("w1", size=10)], gh, board=board)

        assert events[-1].kind == "expected_red_clear_skipped"
        assert not gh.update_repo_file_calls

    def test_still_skips_when_patch_id_is_unavailable(self) -> None:
        """Fail closed: a SHA mismatch with no patch-id on either side
        (an older `gh_ops`, or a lookup failure) must skip exactly like
        before #2298 — "cannot confirm identical" is never "confirmed"."""
        work = self._work("w1", acceptance_state="passed", acceptance_sha="old-sha")
        board = self._board(completed=[work])
        gh = _PatchIdGh(branch_sha="new-sha", patch_ids={})  # every lookup -> None

        events = process([_q("w1", size=10)], gh, board=board)

        assert events[-1].kind == "expected_red_clear_skipped"
        assert not gh.update_repo_file_calls

    def test_patch_id_verified_clear_writes_a_distinct_durable_audit_row(
        self, coord_db,
    ) -> None:
        """The rebase-not-content-change arm is queryable on its own —
        not just implied by the eventual `expected_red_clear` row — the
        same "name every branch" posture #2199/#2266 already established
        for the other arms of this function."""
        work = self._work("w1", acceptance_state="passed", acceptance_sha="old-sha")
        board = self._board(completed=[work])
        gh = _PatchIdGh(
            branch_sha="new-sha",
            patch_ids={"worker/w1": "same-patch", "old-sha": "same-patch"},
        )

        process([_q("w1", size=10)], gh, board=board)

        rows = coord_db.execute(
            "SELECT issue FROM audit_log "
            "WHERE event_type = 'expected_red_sha_mismatch_patch_id_verified'",
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["issue"] == 1

    def test_skip_remedy_points_at_a_command_that_works_post_merge(self) -> None:
        """#2298 (also worth fixing here): the pre-#2298 advice —
        `coord acceptance record --sha <merged sha>` — targets an open
        issue's live work assignment. By the time this guard skips, the
        issue this entry closed is already closed (`gh_ops.close_issue`
        ran just before this), so that advice leads nowhere. Point at the
        remedy that actually works from the state the operator is in."""
        work = self._work("w1", acceptance_state="passed", acceptance_sha="an-old-sha")
        board = self._board(completed=[work])
        gh = _ExpectedRedGh(branch_sha="cafesha")  # branch_head_sha != acceptance_sha

        events = process([_q("w1", size=10)], gh, board=board)

        assert "coord acceptance expected-red api --clear --issue 1" in events[-1].message


class _StubResponse:
    """Minimal `httpx.Response` stand-in — mirrors `test_portal_bridge.py`'s
    `_Response`, needed here too since `_maybe_push_design_round` (PDR-3,
    #2508) drives a real `PortalBridgeClient` over `httpx.post`."""

    def __init__(self, status_code=200, json_body=None, text="") -> None:
        self.status_code = status_code
        self._json_body = json_body
        self.text = text or (str(json_body) if json_body is not None else "")

    def json(self):
        if self._json_body is None:
            raise ValueError("no body")
        return self._json_body


@dataclass
class _MockAuthorGh(FakeGh):
    """FakeGh + `get_issue` — the one extra surface
    `_maybe_push_design_round` (PDR-3, #2508) needs to resolve a
    `type="mock-author"` entry's milestone. Not part of `GhOps` proper (see
    `_maybe_push_design_round`'s optional-probe docstring), so every other
    `FakeGh`-based test in this file is unaffected."""

    issue_milestone_number: int | None = 9
    issue_title: str = "Q3 push"
    issue_body: str = "Ship the thing."

    def get_issue(self, repo: str, issue_number: int) -> dict:
        milestone = (
            {"number": self.issue_milestone_number, "title": "Q3 push"}
            if self.issue_milestone_number is not None
            else None
        )
        return {"title": self.issue_title, "body": self.issue_body, "milestone": milestone}


class TestDesignRoundPushOnMerge:
    """PDR-3 (#2508): a merged `type="mock-author"` (Gate A) PR auto-pushes
    a design round to the portal — but only for a milestone with a portal
    link on file (`coord portal link`, PDR-1/#2507). No link, no portal
    config, or a GhOps stub that can't even resolve the tracking issue all
    degrade to a no-op, matching `coord.portal_bridge`'s fail-open posture
    for the rest of this bridge ("a portal outage must never block a
    merge")."""

    @staticmethod
    def _board(completed=None):
        from coord.models import Board
        return Board(active=[], completed=list(completed or []))

    @staticmethod
    def _config(
        *,
        portal_enabled: bool = True,
        gate_design_rounds: bool = True,
        driver_mock_glob: str | None = "*.html",
    ):
        """A minimal config-like object carrying only what
        `_maybe_push_design_round` and the ordinary merge-gate defaults
        read — same "build the smallest _Cfg that satisfies the gate
        reads" pattern `TestReviewGate._config` uses just above.

        *gate_design_rounds* is #2903's draft gate: True (the shipped
        default) holds the auto-pushed round for an operator, False is the
        pre-#2903 straight-to-`pending` behaviour.

        *driver_mock_glob* (#3068) seeds `acceptance.drivers["api"].mock` —
        the default `"*.html"` mirrors a `web-playwright` repo, the common
        case every pre-#3068 test here implicitly assumed. `None` omits the
        driver entirely (an unconfigured-acceptance repo).
        """
        from coord.config import (
            DEFAULT_PORTAL_APPROVAL,
            AcceptanceConfig,
            AcceptanceDriverConfig,
            PortalApprovalConfig,
            PortalConfig,
        )

        @dataclass
        class _Cfg:
            portal: PortalConfig = field(default_factory=PortalConfig)
            acceptance: AcceptanceConfig = field(default_factory=AcceptanceConfig)

        cfg = _Cfg()
        cfg.portal = PortalConfig(
            enabled=portal_enabled,
            base_url="https://intake.example.com",
            bridge_client_id="id-123",
            bridge_client_secret="secret-456",
            approval=PortalApprovalConfig(
                kinds={**DEFAULT_PORTAL_APPROVAL, "design_round": gate_design_rounds}
            ),
        )
        if driver_mock_glob is not None:
            cfg.acceptance = AcceptanceConfig(
                drivers={
                    "api": AcceptanceDriverConfig(
                        kind="web-playwright", run="npx playwright test", mock=driver_mock_glob,
                    )
                }
            )
        return cfg

    def _link(self, submission_id: str = "sub_1", milestone_number: int = 9) -> None:
        from coord import portal_store
        portal_store.link_milestone(
            repo_name="api", milestone_number=milestone_number, submission_id=submission_id,
        )

    def test_no_op_when_portal_not_configured(self) -> None:
        self._link()
        events = process(
            [_q("w1", size=10, assignment_type="mock-author")], _MockAuthorGh(),
            config=None, board=self._board(),
        )
        assert not [e for e in events if e.kind.startswith("design_round")]

    def test_no_op_when_portal_disabled(self) -> None:
        self._link()
        cfg = self._config(portal_enabled=False)
        events = process(
            [_q("w1", size=10, assignment_type="mock-author")], _MockAuthorGh(),
            config=cfg, board=self._board(),
        )
        assert not [e for e in events if e.kind.startswith("design_round")]

    def test_no_op_when_milestone_has_no_portal_link(self) -> None:
        # No `_link()` call — milestone 9 is never linked.
        cfg = self._config()
        events = process(
            [_q("w1", size=10, assignment_type="mock-author")], _MockAuthorGh(),
            config=cfg, board=self._board(),
        )
        assert not [e for e in events if e.kind.startswith("design_round")]
        assert events[-1].kind == "merged"  # the ordinary "left open" event still fires

    def test_no_op_for_a_plain_work_entry(self) -> None:
        """Only `type="mock-author"` triggers this hook — a normal `work`
        merge must never attempt a portal push."""
        self._link()
        cfg = self._config()
        events = process(
            [_q("w1", size=10, assignment_type="work")], _MockAuthorGh(),
            config=cfg, board=self._board(),
        )
        assert not [e for e in events if e.kind.startswith("design_round")]

    def test_gh_ops_lacking_get_issue_degrades_to_a_noop(self) -> None:
        """An ordinary `FakeGh` (no `get_issue`) must not crash — same
        optional-probe convention `branch_has_merge_commit` already uses."""
        self._link()
        cfg = self._config()
        events = process(
            [_q("w1", size=10, assignment_type="mock-author")], FakeGh(),
            config=cfg, board=self._board(),
        )
        assert not [e for e in events if e.kind.startswith("design_round")]

    def test_pushes_a_design_round_when_linked(self, monkeypatch) -> None:
        self._link(submission_id="sub_1", milestone_number=9)
        cfg = self._config()

        monkeypatch.setattr(
            "coord.mock_author.collect_mock_bundle_files",
            lambda repo_github, milestone_number, branch, driver_mock_glob: {"contract.md": "# contract"},
        )
        seen_upload = {}

        def _post(url, json=None, headers=None, timeout=None):
            seen_upload["url"] = url
            seen_upload["json"] = json
            return _StubResponse(200, {"bundle_key": "bundles/sub_1/r1.tar"})

        monkeypatch.setattr("httpx.post", _post)

        events = process(
            [_q("w1", size=10, assignment_type="mock-author")], _MockAuthorGh(),
            config=cfg, board=self._board(),
        )

        # #2903: the round is uploaded and enqueued, but it is DRAFTED, not
        # queued — an operator has to read it before the customer does.
        assert events[-1].kind == "design_round_drafted"
        assert "awaiting operator approval" in events[-1].message
        assert "sub_1" in events[-1].message
        assert seen_upload["url"] == "https://intake.example.com/api/bridge/upload"
        assert seen_upload["json"]["submission_id"] == "sub_1"
        assert seen_upload["json"]["files"] == {"contract.md": "# contract"}

        from coord import portal_store
        rows = portal_store.outbox_for_submission("sub_1")
        assert len(rows) == 1
        assert rows[0].kind == "design_round"
        assert rows[0].fields["design_round"]["bundle_key"] == "bundles/sub_1/r1.tar"
        assert "Ship the thing." in rows[0].fields["design_round"]["outcome_definition"]
        assert rows[0].state == portal_store.STATE_DRAFT
        assert portal_store.pending_outbox() == []

    def test_an_ungated_design_round_is_queued_outright(self, monkeypatch) -> None:
        """#2903: `portal.approval.design_round: false` restores the
        pre-gate behaviour exactly."""
        self._link(submission_id="sub_1", milestone_number=9)
        cfg = self._config(gate_design_rounds=False)

        monkeypatch.setattr(
            "coord.mock_author.collect_mock_bundle_files",
            lambda repo_github, milestone_number, branch, driver_mock_glob: {"contract.md": "# contract"},
        )
        monkeypatch.setattr(
            "httpx.post",
            lambda url, json=None, headers=None, timeout=None: _StubResponse(
                200, {"bundle_key": "bundles/sub_1/r1.tar"}
            ),
        )

        events = process(
            [_q("w1", size=10, assignment_type="mock-author")], _MockAuthorGh(),
            config=cfg, board=self._board(),
        )

        assert events[-1].kind == "design_round_queued"

        from coord import portal_store
        assert [r.state for r in portal_store.outbox_for_submission("sub_1")] == [
            portal_store.STATE_PENDING
        ]

    def test_no_bundle_files_yields_a_skip_event(self, monkeypatch) -> None:
        self._link()
        cfg = self._config()
        monkeypatch.setattr(
            "coord.mock_author.collect_mock_bundle_files",
            lambda repo_github, milestone_number, branch, driver_mock_glob: {},
        )

        events = process(
            [_q("w1", size=10, assignment_type="mock-author")], _MockAuthorGh(),
            config=cfg, board=self._board(),
        )

        assert events[-1].kind == "design_round_push_skipped"

    def test_non_html_driver_glob_skips_without_collecting(self, monkeypatch) -> None:
        """#3068: a `tui-tuidriver` repo (`.screen` mocks) must never push a
        design round — those mocks aren't browser-viewable — and the skip
        must fire BEFORE `collect_mock_bundle_files` is even called, since
        the outcome can't change once the glob is known non-viewable."""
        self._link()
        cfg = self._config(driver_mock_glob="*.screen")

        called = []
        monkeypatch.setattr(
            "coord.mock_author.collect_mock_bundle_files",
            lambda *a, **k: called.append(1) or {"contract.md": "# contract"},
        )

        events = process(
            [_q("w1", size=10, assignment_type="mock-author")], _MockAuthorGh(),
            config=cfg, board=self._board(),
        )

        assert events[-1].kind == "design_round_push_skipped"
        assert "not browser-viewable" in events[-1].message
        assert called == []

    def test_no_acceptance_driver_configured_skips(self, monkeypatch) -> None:
        """#3068: a `mock-author` entry for a repo with no acceptance driver
        on file at all (e.g. a hand-dispatched entry) must skip visibly
        rather than silently guessing `*.html`."""
        self._link()
        cfg = self._config(driver_mock_glob=None)

        events = process(
            [_q("w1", size=10, assignment_type="mock-author")], _MockAuthorGh(),
            config=cfg, board=self._board(),
        )

        assert events[-1].kind == "design_round_push_skipped"
        assert "no acceptance driver configured" in events[-1].message

    def test_routed_repo_with_one_agreed_viewable_glob_still_pushes(
        self, monkeypatch
    ) -> None:
        """#3068 review follow-up: a *routed* repo (#1125) has no flat driver,
        but if every route declares the same browser-viewable glob the answer
        is unambiguous — that's a resolution, not a guess, so the design round
        must still push rather than silently regress to a skip."""
        from coord.config import AcceptanceConfig, AcceptanceDriverConfig

        self._link()
        cfg = self._config()
        cfg.acceptance = AcceptanceConfig(
            drivers={
                "api": AcceptanceDriverConfig(
                    routes=[
                        AcceptanceDriverConfig(
                            match="web/**", kind="web-playwright", mock="*.html",
                        ),
                        AcceptanceDriverConfig(
                            match="api/**", kind="web-playwright", mock="*.html",
                        ),
                    ]
                )
            }
        )
        monkeypatch.setattr(
            "coord.mock_author.collect_mock_bundle_files",
            lambda repo_github, milestone_number, branch, driver_mock_glob: (
                {"contract.md": "# contract", "mocks/a.html": "<html>"}
            ),
        )
        monkeypatch.setattr(
            "httpx.post",
            lambda *a, **k: _StubResponse(200, {"bundle_key": "bundles/sub_1/r1.tar"}),
        )

        events = process(
            [_q("w1", size=10, assignment_type="mock-author")], _MockAuthorGh(),
            config=cfg, board=self._board(),
        )

        assert events[-1].kind == "design_round_drafted", events[-1].message

    def test_routed_repo_with_disagreeing_globs_skips_with_a_reason(
        self, monkeypatch
    ) -> None:
        """#3068: routes that disagree on the mock glob genuinely can't be
        resolved milestone-wide (a milestone isn't one file), and publishing
        the wrong route's mocks to a customer is worse than publishing none —
        so skip, naming the ambiguity rather than picking one."""
        from coord.config import AcceptanceConfig, AcceptanceDriverConfig

        self._link()
        cfg = self._config()
        cfg.acceptance = AcceptanceConfig(
            drivers={
                "api": AcceptanceDriverConfig(
                    routes=[
                        AcceptanceDriverConfig(
                            match="web/**", kind="web-playwright", mock="*.html",
                        ),
                        AcceptanceDriverConfig(
                            match="tui/**", kind="tui-tuidriver", mock="*.screen",
                        ),
                    ]
                )
            }
        )
        called = []
        monkeypatch.setattr(
            "coord.mock_author.collect_mock_bundle_files",
            lambda *a, **k: called.append(1) or {},
        )

        events = process(
            [_q("w1", size=10, assignment_type="mock-author")], _MockAuthorGh(),
            config=cfg, board=self._board(),
        )

        assert events[-1].kind == "design_round_push_skipped"
        assert "different mock globs" in events[-1].message
        assert called == []

    def test_upload_failure_degrades_to_a_failed_event_not_an_exception(
        self, monkeypatch,
    ) -> None:
        self._link()
        cfg = self._config()
        monkeypatch.setattr(
            "coord.mock_author.collect_mock_bundle_files",
            lambda repo_github, milestone_number, branch, driver_mock_glob: {"contract.md": "# contract"},
        )
        monkeypatch.setattr(
            "httpx.post", lambda *a, **k: _StubResponse(401, {}, text="unauthorized"),
        )

        events = process(
            [_q("w1", size=10, assignment_type="mock-author")], _MockAuthorGh(),
            config=cfg, board=self._board(),
        )

        assert events[-1].kind == "design_round_push_failed"
        # The merge itself still went through — this hook never undoes it.
        assert any(e.kind == "merged" for e in events)


class TestStatusPushOnMerge:
    """#2588: a merged `type="work"` PR that closed its issue folds every
    issue under that issue's milestone into one customer status and pushes
    it if it changed — the same PDR-3/#2508 auto-push pattern
    `TestDesignRoundPushOnMerge` above already covers for design rounds,
    applied here to status (the pattern #2588 itself names as the model to
    follow). Same fail-open posture: no link, no portal config, or a GhOps
    stub that can't resolve the issue's milestone all degrade to a no-op."""

    @staticmethod
    def _board(completed=None):
        from coord.models import Board
        return Board(active=[], completed=list(completed or []))

    @staticmethod
    def _config(*, portal_enabled: bool = True):
        from coord.config import PortalConfig

        @dataclass
        class _RepoCfg:
            github: str = "acme/api"

        @dataclass
        class _Cfg:
            portal: PortalConfig = field(default_factory=PortalConfig)

            def repo(self, name):
                # `fold_status_for_milestone` resolves `repo_cfg.github` to
                # call GitHub — unlike the design-round hook above, which
                # never needs `config.repo()` at all (its `repo_github`
                # comes straight off the entry).
                return _RepoCfg() if name == "api" else None

        cfg = _Cfg()
        cfg.portal = PortalConfig(
            enabled=portal_enabled,
            base_url="https://intake.example.com",
            bridge_client_id="id-123",
            bridge_client_secret="secret-456",
        )
        return cfg

    def _link(self, submission_id: str = "sub_1", milestone_number: int = 9) -> None:
        from coord import portal_store
        portal_store.link_milestone(
            repo_name="api", milestone_number=milestone_number, submission_id=submission_id,
        )

    def test_no_op_when_portal_not_configured(self) -> None:
        self._link()
        events = process(
            [_q("w1", size=10, assignment_type="work")], _MockAuthorGh(),
            config=None, board=self._board(),
        )
        assert not [e for e in events if e.kind.startswith("status_")]

    def test_no_op_when_portal_disabled(self) -> None:
        self._link()
        cfg = self._config(portal_enabled=False)
        events = process(
            [_q("w1", size=10, assignment_type="work")], _MockAuthorGh(),
            config=cfg, board=self._board(),
        )
        assert not [e for e in events if e.kind.startswith("status_")]

    def test_no_op_when_milestone_has_no_portal_link(self) -> None:
        # No `_link()` call — milestone 9 is never linked.
        cfg = self._config()
        events = process(
            [_q("w1", size=10, assignment_type="work")], _MockAuthorGh(),
            config=cfg, board=self._board(),
        )
        assert not [e for e in events if e.kind.startswith("status_")]
        # the ordinary close-issue event still fires (alongside whatever the
        # unrelated expected-red-clear hook reports for a plain work entry)
        assert any(e.kind == "merged" for e in events)

    def test_no_op_for_a_non_closing_entry_type(self) -> None:
        """Only a `CLOSES_ISSUE_TYPES` entry (`work`) triggers this hook —
        e.g. a `mock-author` merge (design round's own trigger) must not
        also fire the status fold."""
        self._link()
        cfg = self._config()
        events = process(
            [_q("w1", size=10, assignment_type="mock-author")], _MockAuthorGh(),
            config=cfg, board=self._board(),
        )
        assert not [e for e in events if e.kind.startswith("status_")]

    def test_gh_ops_lacking_get_issue_degrades_to_a_noop(self) -> None:
        """An ordinary `FakeGh` (no `get_issue`) must not crash — same
        optional-probe convention `branch_has_merge_commit` already uses."""
        self._link()
        cfg = self._config()
        events = process(
            [_q("w1", size=10, assignment_type="work")], FakeGh(),
            config=cfg, board=self._board(),
        )
        assert not [e for e in events if e.kind.startswith("status_")]

    def test_full_stack_fold_reads_github_and_queues(self, monkeypatch) -> None:
        """End-to-end through the real `fold_status_for_milestone` — the
        five-issues-one-shipped scenario #2588 names explicitly, shrunk to
        two so the fixture stays readable."""
        self._link(submission_id="sub_1", milestone_number=9)
        cfg = self._config()

        monkeypatch.setattr(
            "coord.github_ops.get_milestone",
            lambda repo, ms: {"number": ms, "title": "Q3 push"},
        )
        monkeypatch.setattr(
            "coord.github_ops.get_milestone_issues",
            lambda repo, title, state="all": [
                {"number": 1, "state": "CLOSED"}, {"number": 2, "state": "CLOSED"},
            ],
        )

        events = process(
            [_q("w1", size=10, assignment_type="work")], _MockAuthorGh(),
            config=cfg, board=self._board(),
        )

        assert events[-1].kind == "status_queued"
        assert "sub_1" in events[-1].message
        assert "shipped" in events[-1].message

        from coord import portal_store
        rows = portal_store.outbox_for_submission("sub_1")
        assert len(rows) == 1
        assert rows[0].fields["status"] == "shipped"

    def test_milestone_less_issue_folds_through_its_issue_link(
        self, monkeypatch,
    ) -> None:
        """#3096: this hook and the daemon tick must fold the SAME set of
        links. A milestone-less issue with a `coord portal link --issue`
        (#2665) on it was visible to the tick and invisible here, so the two
        automatic callers saw different link universes and could reach
        different answers for one submission."""
        from coord import portal_store
        portal_store.link_issue(
            repo_name="api", issue_number=1, submission_id="sub_2",
        )
        cfg = self._config()
        monkeypatch.setattr(
            "coord.github_ops.get_issue",
            lambda repo, n: {"number": n, "state": "CLOSED"},
        )

        events = process(
            [_q("w1", size=10, assignment_type="work")],
            _MockAuthorGh(issue_milestone_number=None),
            config=cfg, board=self._board(),
        )

        assert events[-1].kind == "status_queued"
        assert "sub_2" in events[-1].message
        assert "shipped" in events[-1].message

    def test_milestone_less_unlinked_issue_stays_a_silent_no_op(self) -> None:
        """The overwhelmingly common case — no link on file — must still cost
        nothing and say nothing."""
        cfg = self._config()

        events = process(
            [_q("w1", size=10, assignment_type="work")],
            _MockAuthorGh(issue_milestone_number=None),
            config=cfg, board=self._board(),
        )

        assert not [e for e in events if e.kind.startswith("status_")]
        assert any(e.kind == "merged" for e in events)

    def test_unchanged_status_produces_no_event(self, monkeypatch) -> None:
        self._link()
        cfg = self._config()

        from coord import portal_sync
        result = portal_sync.StatusFoldResult(
            "sub_1", "planned", "unchanged since last push — not re-notifying (#2588)",
        )
        monkeypatch.setattr(
            portal_sync, "fold_status_for_milestone", lambda *a, **kw: result,
        )

        events = process(
            [_q("w1", size=10, assignment_type="work")], _MockAuthorGh(),
            config=cfg, board=self._board(),
        )

        assert not [e for e in events if e.kind.startswith("status_")]
        assert any(e.kind == "merged" for e in events)

    def test_read_failure_degrades_to_a_failed_event_not_an_exception(
        self, monkeypatch,
    ) -> None:
        self._link()
        cfg = self._config()

        from coord import portal_sync
        result = portal_sync.StatusFoldResult(
            "sub_1", None, "could not read ms-9's issues: gh api rate limited",
            failed=True,
        )
        monkeypatch.setattr(
            portal_sync, "fold_status_for_milestone", lambda *a, **kw: result,
        )

        events = process(
            [_q("w1", size=10, assignment_type="work")], _MockAuthorGh(),
            config=cfg, board=self._board(),
        )

        assert events[-1].kind == "status_push_failed"
        # The merge itself still went through — this hook never undoes it.
        assert any(e.kind == "merged" for e in events)


class _TestAuthorGateGh(FakeGh):
    """#2191: FakeGh + the API-only manifest/issue-state surface
    `coord.acceptance.missing_expected_red_warning` needs. Defaults to a
    single ms-dir manifest mapping issue 944 with no `expected_red` block —
    the "unwritten registry" signature — and issue 944 reporting "open",
    so the warning fires unless a test overrides one of them."""

    def __init__(
        self,
        *,
        manifest_text: str | None = None,
        issue_states: dict[int, str] | None = None,
        **kw,
    ):
        super().__init__(**kw)
        self.manifest_text = (
            manifest_text if manifest_text is not None else "tests:\n  ms01::a: 944\n"
        )
        self.issue_states = issue_states or {}

    def list_repo_subdirs(self, repo: str, path: str, branch: str = "develop") -> list[str]:
        return ["ms01"]

    def get_repo_file_with_sha(self, repo: str, path: str, branch: str = "develop") -> tuple[str, str]:
        if path != "tests/acceptance/ms01/manifest.yml":
            raise RuntimeError("not found")
        return self.manifest_text, "blob-sha"

    def get_issues_live_state(self, repo: str, numbers: list[int]) -> dict[int, str]:
        return {n: self.issue_states.get(n, "open") for n in numbers}


class TestExpectedRedMissingWarningOnSliceOpen:
    """#2191: the coordinator-side gate half — a `type="test-author"`
    slice's PR-open moment checks whether its manifest maps the child
    issue's test ids with zero `expected_red` entries recorded, and
    surfaces a non-blocking `MergeEvent` when it finds the unwritten-
    registry signature. The slice PR still opens either way — this is
    "warned", not "refused"."""

    @staticmethod
    def _board(assignments) -> "Board":
        from coord.models import Board
        return Board(active=[], completed=list(assignments))

    @staticmethod
    def _slice_assignment(
        aid: str = "w1", *, tracking: int = 100, for_issue: int | None = 944,
    ) -> Assignment:
        return Assignment(
            machine_name="m1", repo_name="api", issue_number=tracking, issue_title="t",
            assignment_id=aid, type="test-author", status="done",
            branch=f"worker/{aid}", for_issue_number=for_issue,
        )

    @staticmethod
    def _entry(aid: str = "w1", *, issue: int = 100, assignment_type: str = "test-author") -> QueuedMerge:
        return QueuedMerge(
            assignment_id=aid, repo_name="api", repo_github="acme/api",
            branch=f"worker/{aid}", target_branch="main", issue_number=issue,
            issue_title="t", state=PENDING, pr_number=None,
            assignment_type=assignment_type,
        )

    def test_warns_when_manifest_lacks_expected_red_for_the_open_child_issue(self) -> None:
        board = self._board([self._slice_assignment()])
        gh = _TestAuthorGateGh()

        events = process([self._entry()], gh, board=board)

        warnings = [e for e in events if e.kind == "expected_red_missing_warning"]
        assert len(warnings) == 1
        assert "#944" in warnings[0].message
        assert "ms01::a" in warnings[0].message
        # Advisory only — the PR still opened.
        assert [e for e in events if e.kind == "opened"]

    def test_no_warning_when_expected_red_is_already_recorded(self) -> None:
        board = self._board([self._slice_assignment()])
        gh = _TestAuthorGateGh(
            manifest_text="tests:\n  ms01::a: 944\nexpected_red:\n  944:\n    - ms01::a\n",
        )

        events = process([self._entry()], gh, board=board)

        assert not [e for e in events if e.kind == "expected_red_missing_warning"]

    def test_no_warning_for_milestone_mode_authoring_with_no_for_issue_number(self) -> None:
        """Gate-A (milestone-mode) authoring never sets `for_issue_number`
        — `effective_issue_number` falls back to `issue_number` itself
        (the tracking issue), which equals `entry.issue_number`, so the
        gate is skipped entirely: no manifest ever maps a test id to the
        tracking issue."""
        board = self._board([self._slice_assignment(for_issue=None)])
        gh = _TestAuthorGateGh()

        events = process([self._entry()], gh, board=board)

        assert not [e for e in events if e.kind == "expected_red_missing_warning"]

    def test_no_warning_for_a_work_entry(self) -> None:
        board = self._board([self._slice_assignment()])
        gh = _TestAuthorGateGh()

        events = process([self._entry(assignment_type="work")], gh, board=board)

        assert not [e for e in events if e.kind == "expected_red_missing_warning"]

    def test_no_warning_when_the_child_issue_is_closed(self) -> None:
        board = self._board([self._slice_assignment()])
        gh = _TestAuthorGateGh(issue_states={944: "closed"})

        events = process([self._entry()], gh, board=board)

        assert not [e for e in events if e.kind == "expected_red_missing_warning"]

    def test_no_warning_when_no_originating_assignment_is_on_the_board(self) -> None:
        gh = _TestAuthorGateGh()

        events = process([self._entry()], gh, board=self._board([]))

        assert not [e for e in events if e.kind == "expected_red_missing_warning"]


class TestReviewGate:
    """#253: process() must refuse to merge when reviews are required and
    no approved review is on the board.

    Reproduces the symptom from quadraui#233: a PR was opened and merged in
    the same `coord merge` invocation, in 2 seconds, with no review.  These
    tests cover the regression for both the legacy code path (no config/board
    passed → gate skipped) and the new code path (config+board passed → gate
    fires).
    """

    @staticmethod
    def _config(*, enabled: bool = True, gates: list[str] | None = None):
        """Build a minimal config-like object with the fields the gate reads."""
        from dataclasses import dataclass
        @dataclass
        class _Reviews:
            enabled: bool = True
        @dataclass
        class _Pipeline:
            default_gates: list[str] | None = None
        @dataclass
        class _Cfg:
            reviews: _Reviews = field(default_factory=_Reviews)
            pipeline: _Pipeline = field(default_factory=_Pipeline)
        cfg = _Cfg()
        cfg.reviews.enabled = enabled
        cfg.pipeline.default_gates = gates if gates is not None else ["review", "merge"]
        return cfg

    @staticmethod
    def _board(active=None, completed=None):
        from coord.models import Board
        return Board(active=list(active or []), completed=list(completed or []))

    @staticmethod
    def _work(aid: str = "w1") -> Assignment:
        return Assignment(
            machine_name="m1",
            repo_name="api",
            issue_number=1,
            issue_title="t",
            assignment_id=aid,
            type="work",
            status="done",
            branch=f"worker/{aid}",
        )

    @staticmethod
    def _review(of_aid: str, *, verdict: str | None = "approve", status: str = "done") -> Assignment:
        return Assignment(
            machine_name="m2",
            repo_name="api",
            issue_number=1,
            issue_title="t",
            assignment_id=f"rev-{of_aid}",
            type="review",
            status=status,
            review_of_assignment_id=of_aid,
            review_verdict=verdict,
        )

    def test_requires_review_helper_honours_config(self) -> None:
        cfg = self._config(enabled=True, gates=["review", "merge"])
        assert mq.requires_review(_q("a"), cfg) is True
        cfg_off = self._config(enabled=False)
        assert mq.requires_review(_q("a"), cfg_off) is False
        cfg_no_gate = self._config(enabled=True, gates=["merge"])
        assert mq.requires_review(_q("a"), cfg_no_gate) is False

    def test_requires_review_entry_override_bypasses_default(self) -> None:
        # #1213: an entry whose snapshotted required_gates drops "review"
        # bypasses the gate even though the default policy requires it.
        cfg = self._config(enabled=True, gates=["review", "merge"])
        entry = _q("a", required_gates=["merge"])
        assert mq.requires_review(entry, cfg) is False

    def test_requires_review_entry_override_can_also_require_it(self) -> None:
        # An override that keeps "review" still gates, same as default.
        cfg = self._config(enabled=True, gates=["merge"])
        entry = _q("a", required_gates=["review", "merge"])
        assert mq.requires_review(entry, cfg) is True

    def test_requires_review_empty_entry_gates_falls_back_to_default(self) -> None:
        # #1213 compatibility contract: untagged work (entry.required_gates
        # empty/absent) must behave exactly as before — default policy wins.
        cfg = self._config(enabled=True, gates=["review", "merge"])
        assert mq.requires_review(_q("a", required_gates=[]), cfg) is True
        cfg_without = self._config(enabled=True, gates=["merge"])
        assert mq.requires_review(_q("a", required_gates=[]), cfg_without) is False

    def test_has_approved_review_finds_matching_review(self) -> None:
        work = self._work("w1")
        review = self._review("w1", verdict="approve")
        board = self._board(completed=[work, review])
        assert mq.has_approved_review(_q("w1"), board) is True

    def test_has_approved_review_rejects_request_changes(self) -> None:
        work = self._work("w1")
        review = self._review("w1", verdict="request-changes")
        board = self._board(completed=[work, review])
        assert mq.has_approved_review(_q("w1"), board) is False

    def test_has_approved_review_ignores_unrelated_reviews(self) -> None:
        work = self._work("w1")
        # Approved review but for a different work assignment
        review = self._review("w99", verdict="approve")
        board = self._board(completed=[work, review])
        assert mq.has_approved_review(_q("w1"), board) is False

    def test_process_emits_review_required_event_and_halts_merge(self) -> None:
        """The smoking-gun #233 regression: no review on board → no merge_pr call."""
        cfg = self._config()
        board = self._board(completed=[self._work("w1")])
        items = [_q("w1", size=10)]
        gh = FakeGh()
        events = process(items, gh, config=cfg, board=board)

        # PR is opened (so the user can inspect) but merge_pr is never called.
        kinds = [e.kind for e in events]
        assert "opened" in kinds
        assert "review_required" in kinds
        assert "merged" not in kinds
        assert gh.merge_calls == []
        # Item remains PENDING with an error so the TUI can surface it.
        assert items[0].state == PENDING
        assert items[0].error == "review required but not approved"

    def test_process_reports_unknown_head_not_a_fabricated_refusal(self) -> None:
        """#2704: `process()`'s live review-required message must not claim
        "not approved" when the branch head could not even be read — the
        same `ApprovalScan.unknown_head` distinction `merge_gate_failures`
        already makes, now asserted end-to-end through `process()` itself
        (the two call sites at the dry-run and live merge-gate checks)."""
        cfg = self._config()
        work = self._work("w1")
        review = self._review("w1", verdict="approve")
        review.review_head_sha = "sha-any"  # review DID capture a SHA to compare
        board = self._board(completed=[work, review])
        items = [_q("w1", size=10)]  # branch_head_sha left None — unconfirmable
        gh = FakeGh()
        events = process(items, gh, config=cfg, board=board)

        kinds = [e.kind for e in events]
        assert "review_required" in kinds
        assert "merged" not in kinds
        assert items[0].error == mq.UNKNOWN_BRANCH_HEAD_REASON

    def test_process_proceeds_when_review_is_approved(self) -> None:
        cfg = self._config()
        board = self._board(completed=[
            self._work("w1"),
            self._review("w1", verdict="approve"),
        ])
        items = [_q("w1", size=10)]
        gh = FakeGh()
        events = process(items, gh, config=cfg, board=board)

        assert any(e.kind == "merged" for e in events)
        assert gh.merge_calls and gh.merge_calls[0][1] == 100  # the opened PR
        assert items[0].state == MERGED

    def test_skip_review_bypasses_gate(self) -> None:
        """--skip-review must let a no-review merge proceed."""
        cfg = self._config()
        board = self._board(completed=[self._work("w1")])
        items = [_q("w1", size=10)]
        gh = FakeGh()
        events = process(items, gh, config=cfg, board=board, skip_review=True)

        kinds = [e.kind for e in events]
        assert "review_required" not in kinds
        assert "merged" in kinds
        assert items[0].state == MERGED

    def test_reviews_disabled_bypasses_gate(self) -> None:
        cfg = self._config(enabled=False)
        board = self._board(completed=[self._work("w1")])
        items = [_q("w1", size=10)]
        gh = FakeGh()
        events = process(items, gh, config=cfg, board=board)

        kinds = [e.kind for e in events]
        assert "review_required" not in kinds
        assert "merged" in kinds

    def test_legacy_callers_without_config_unaffected(self) -> None:
        """Callers that don't pass config/board still work (no surprise breakage).

        When config is None, requires_review() returns False so no gate fires.
        The fail-closed rule (#821) only applies when config is present and
        confirms review is required but board is absent.
        """
        items = [_q("w1", size=10)]
        gh = FakeGh()
        events = process(items, gh)
        assert any(e.kind == "merged" for e in events)

    # ── #821: fail-closed gates ───────────────────────────────────────────

    def test_process_fails_closed_when_board_none_and_review_required(self) -> None:
        """#821: process() with board=None must block a review-required entry."""
        cfg = self._config()  # reviews.enabled=True, gate includes "review"
        items = [_q("w1", size=10)]
        gh = FakeGh()
        # No board → cannot confirm review approval → fail closed.
        events = process(items, gh, config=cfg, board=None)

        kinds = [e.kind for e in events]
        assert "review_required" in kinds, "gate must fire when board is None"
        assert "merged" not in kinds, "merge must not proceed without confirmed review"
        assert items[0].state == PENDING
        assert items[0].error is not None

    def test_process_fails_closed_when_board_none_and_smoke_required(self) -> None:
        """#821: process() with board=None must block a smoke-required entry."""
        from dataclasses import dataclass as _dc, field as _dc_field

        @_dc
        class _Reviews:
            enabled: bool = False  # review gate off

        @_dc
        class _Pipeline:
            default_gates: list | None = None

        @_dc
        class _SmokeConfig:
            reviews: _Reviews = _dc_field(default_factory=_Reviews)
            pipeline: _Pipeline = _dc_field(default_factory=_Pipeline)

        cfg = _SmokeConfig()
        cfg.pipeline.default_gates = ["test", "merge"]  # smoke gate on, review off
        items = [_q("w1", size=10)]
        gh = FakeGh()
        # No board → cannot confirm smoke verdict → fail closed.
        events = process(items, gh, config=cfg, board=None)

        kinds = [e.kind for e in events]
        assert "smoke_required" in kinds, "smoke gate must fire when board is None"
        assert "merged" not in kinds
        assert items[0].state == PENDING, "blocked entry must remain PENDING"
        assert items[0].error is not None, "blocked entry must carry an error message"

    def test_process_fail_closed_board_none_skip_review_still_merges(self) -> None:
        """#821: explicit skip_review=True can still bypass the gate for local overrides."""
        cfg = self._config()
        items = [_q("w1", size=10)]
        gh = FakeGh()
        # skip_review=True is the explicit local override; must still work.
        events = process(items, gh, config=cfg, board=None, skip_review=True)

        kinds = [e.kind for e in events]
        assert "review_required" not in kinds
        assert "merged" in kinds

    # ── #821: commit-bound approval — production population ──────────────

    def test_process_populates_branch_head_sha_from_gh_ops(self) -> None:
        """#821: process() must populate entry.branch_head_sha via gh_ops.get_branch_sha.

        This verifies the *production population* path — that get_branch_sha is
        actually called (not just that has_approved_review checks the value).
        """
        from dataclasses import dataclass as _dc, field as _dc_field

        sha_calls: list[tuple[str, str]] = []

        class _TrackingGh(FakeGh):
            def get_branch_sha(self, repo: str, branch: str) -> str | None:
                sha_calls.append((repo, branch))
                return "cafebabe"

        cfg = self._config()
        work = self._work("w1")
        review = self._review("w1", verdict="approve")
        review.review_head_sha = "cafebabe"  # matches what _TrackingGh returns
        board = self._board(completed=[work, review])

        items = [_q("w1", size=10)]
        process(items, _TrackingGh(), config=cfg, board=board)

        # get_branch_sha must have been called for the entry.
        assert len(sha_calls) >= 1, "process() must call gh_ops.get_branch_sha"
        assert sha_calls[0][1] == items[0].branch, "must fetch SHA for the entry's branch"
        # The field must be populated on the entry.
        assert items[0].branch_head_sha == "cafebabe"

    def test_process_stale_sha_blocks_merge_end_to_end(self) -> None:
        """#821: end-to-end — review at old SHA + branch moved → process blocks merge."""
        cfg = self._config()
        work = self._work("w1")
        review = self._review("w1", verdict="approve")
        review.review_head_sha = "oldsha"  # review was at this commit

        class _MovedBranchGh(FakeGh):
            def get_branch_sha(self, repo: str, branch: str) -> str | None:
                return "newsha"  # branch has new commits since review

        board = self._board(completed=[work, review])
        items = [_q("w1", size=10)]
        events = process(items, _MovedBranchGh(), config=cfg, board=board)

        kinds = [e.kind for e in events]
        assert "merged" not in kinds, "stale approval must not allow merge"
        assert "review_required" in kinds, "stale approval must re-block the review gate"

    def test_process_populates_branch_patch_id_from_gh_ops(self) -> None:
        """#1475: process() must populate entry.branch_patch_id via
        gh_ops.get_branch_patch_id — the production population path."""
        patch_id_calls: list[tuple[str, str, str]] = []

        class _TrackingGh(FakeGh):
            def get_branch_patch_id(self, repo: str, base: str, branch: str) -> str | None:
                patch_id_calls.append((repo, base, branch))
                return "patchid-abc"

        cfg = self._config()
        work = self._work("w1")
        review = self._review("w1", verdict="approve")
        board = self._board(completed=[work, review])

        items = [_q("w1", size=10)]
        process(items, _TrackingGh(), config=cfg, board=board)

        assert len(patch_id_calls) >= 1, "process() must call gh_ops.get_branch_patch_id"
        assert patch_id_calls[0][2] == items[0].branch, "must fetch patch-id for the entry's branch"
        assert items[0].branch_patch_id == "patchid-abc"

    def test_process_skips_branch_patch_id_fetch_when_review_not_required(self) -> None:
        """#1475 (non-blocking review finding): has_approved_review never
        consults branch_patch_id unless a review is actually required for the
        entry, so process() must not spend a `gh api compare` round trip
        populating it in that case (gate disabled here via default_gates)."""
        patch_id_calls: list[tuple[str, str, str]] = []

        class _TrackingGh(FakeGh):
            def get_branch_patch_id(self, repo: str, base: str, branch: str) -> str | None:
                patch_id_calls.append((repo, base, branch))
                return "patchid-abc"

        cfg = self._config(gates=["merge"])  # "review" not in the effective gates
        work = self._work("w1")
        board = self._board(completed=[work])

        items = [_q("w1", size=10)]
        process(items, _TrackingGh(), config=cfg, board=board)

        assert patch_id_calls == [], "review not required — must not fetch branch_patch_id"
        assert items[0].branch_patch_id is None

    def test_process_skips_branch_patch_id_fetch_when_skip_review(self) -> None:
        """#1475 (non-blocking review finding): --skip-review means the review
        gate (and its patch-id check) is never consulted either."""
        patch_id_calls: list[tuple[str, str, str]] = []

        class _TrackingGh(FakeGh):
            def get_branch_patch_id(self, repo: str, base: str, branch: str) -> str | None:
                patch_id_calls.append((repo, base, branch))
                return "patchid-abc"

        cfg = self._config(gates=["review", "merge"])
        work = self._work("w1")
        board = self._board(completed=[work])

        items = [_q("w1", size=10)]
        process(items, _TrackingGh(), config=cfg, board=board, skip_review=True)

        assert patch_id_calls == [], "skip_review — must not fetch branch_patch_id"
        assert items[0].branch_patch_id is None

    def test_process_rebase_with_matching_patch_id_still_merges_end_to_end(self) -> None:
        """#1475: a rebase that moves the SHA but not the content must not
        force a re-review — the merge proceeds on the carried-forward approval."""
        cfg = self._config()
        work = self._work("w1")
        review = self._review("w1", verdict="approve")
        review.review_head_sha = "oldsha"
        review.review_patch_id = "patchid-same"

        class _RebasedGh(FakeGh):
            def get_branch_sha(self, repo: str, branch: str) -> str | None:
                return "newsha"  # rebase moved the head

            def get_branch_patch_id(self, repo: str, base: str, branch: str) -> str | None:
                return "patchid-same"  # but the content is byte-identical

        board = self._board(completed=[work, review])
        items = [_q("w1", size=10)]
        events = process(items, _RebasedGh(), config=cfg, board=board)

        kinds = [e.kind for e in events]
        assert "review_required" not in kinds, "content-identical rebase must not re-block review"
        assert "merged" in kinds, "approval must carry forward across a pure rebase"

    # ── #821: commit-bound approval ───────────────────────────────────────

    def test_has_approved_review_stale_sha_blocks(self) -> None:
        """#821: an approval covering a different commit SHA is rejected."""
        work = self._work("w1")
        review = self._review("w1", verdict="approve")
        review.review_head_sha = "abc123"  # SHA when review was done

        entry = _q("w1", branch="worker/w1")
        entry.branch_head_sha = "def456"  # branch moved since review

        board = self._board(completed=[work, review])
        # Review SHA != branch SHA → stale approval → must return False.
        assert mq.has_approved_review(entry, board) is False

    def test_has_approved_review_matching_sha_passes(self) -> None:
        """#821: an approval at the same commit SHA is accepted."""
        work = self._work("w1")
        review = self._review("w1", verdict="approve")
        review.review_head_sha = "abc123"

        entry = _q("w1", branch="worker/w1")
        entry.branch_head_sha = "abc123"  # same SHA as review

        board = self._board(completed=[work, review])
        assert mq.has_approved_review(entry, board) is True

    # ── #2085: a caller that cannot supply the current branch SHA must fail
    # closed, not skip the check — the opposite gap from #1396's "review
    # predates SHA tracking" backward-compat case just below.

    def test_has_approved_review_missing_current_sha_fails_closed(self) -> None:
        """#2085: `entry.branch_head_sha` unset (never populated — e.g. a
        deleted branch, per #2085's "second, sharper failure mode": GitHub's
        `get_branch_sha` returns None for a branch that no longer exists, and
        that None is what lands in `branch_head_sha`) with a review that DOES
        carry a `review_head_sha` must be treated as unconfirmed, not
        approved. Before #2085 this fell through to the same `return True` as
        the pre-#821 "review predates SHA tracking" case — the exact
        fail-open gap the #1966 chain and the deleted-branch READY flip both
        traced back to."""
        work = self._work("w1")
        review = self._review("w1", verdict="approve")
        review.review_head_sha = "abc123"  # the review DID capture a SHA

        entry = _q("w1", branch="worker/w1")
        entry.branch_head_sha = None  # unknown to this caller (or branch deleted)

        board = self._board(completed=[work, review])
        assert mq.has_approved_review(entry, board) is False

    def test_has_approved_review_raw_work_assignment_caller_fails_closed(self) -> None:
        """#2085: the exact caller shape the issue names — `has_approved_review`
        invoked with a raw work `Assignment` (no `branch_head_sha` attribute
        at all, not merely `None`), as `merge_gate_failures`/`passes_merge_gates`
        do for `enqueue_approved_work` and the board's stage projection's
        underlying question. `getattr(entry, "branch_head_sha", None)` used to
        make this indistinguishable from "no SHA to compare against" and
        accept any historical approval; it must now refuse one that carries a
        `review_head_sha` this caller cannot confirm."""
        work = self._work("w1")
        review = self._review("w1", verdict="approve")
        review.review_head_sha = "abc123"
        board = self._board(completed=[work, review])

        # `work` itself stands in for the caller's entry — a plain Assignment,
        # never a QueuedMerge, so it has no branch_head_sha attribute.
        assert mq.has_approved_review(work, board) is False

    def test_has_approved_review_superseded_by_later_request_changes(self) -> None:
        """#2085: the #1966 chain itself — an `approve` at an earlier commit,
        superseded by a `request-changes` review dispatched against later
        commits. The live merge gate (entry.branch_head_sha populated from
        the CURRENT branch tip, as `process()` does) must refuse: the only
        `approve` review's SHA no longer matches the branch, and there is no
        patch-id to prove the content is unchanged."""
        work_orig = self._work("w-orig")
        review_orig = self._review("w-orig", verdict="approve")
        review_orig.review_head_sha = "sha-a"

        work_fix = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="w-fix", type="work", status="done",
            branch="worker/w-orig", review_of_assignment_id="w-orig",
        )
        review_fix = self._review("w-fix", verdict="request-changes")
        review_fix.review_head_sha = "sha-b"

        entry = _q("w-fix", branch="worker/w-orig")
        entry.branch_head_sha = "sha-b"  # the CURRENT branch tip

        board = self._board(completed=[work_orig, review_orig, work_fix, review_fix])
        assert mq.has_approved_review(entry, board) is False

    def test_has_approved_review_no_sha_skips_commit_check(self) -> None:
        """#821: when SHAs are absent, the commit check is skipped (backward compat)."""
        work = self._work("w1")
        # review_head_sha unset (pre-821 row)
        review = self._review("w1", verdict="approve")

        entry = _q("w1", branch="worker/w1")
        # branch_head_sha also unset

        board = self._board(completed=[work, review])
        # No SHAs → skip the commit check → approval still valid.
        assert mq.has_approved_review(entry, board) is True

    # ── #1475: patch-id carries an approval across a content-identical rebase ──

    def test_has_approved_review_matching_patch_id_survives_sha_move(self) -> None:
        """#1475: a pure rebase moves the SHA but not the content — the
        approval must still count when the patch-id matches."""
        work = self._work("w1")
        review = self._review("w1", verdict="approve")
        review.review_head_sha = "abc123"
        review.review_patch_id = "patchid-same"

        entry = _q("w1", branch="worker/w1")
        entry.branch_head_sha = "def456"  # SHA moved — a rebase happened
        entry.branch_patch_id = "patchid-same"  # but the diff is identical

        board = self._board(completed=[work, review])
        assert mq.has_approved_review(entry, board) is True

    def test_has_approved_review_differing_patch_id_stays_stale(self) -> None:
        """#1475: a genuine content change must still void the approval even
        though both patch-ids are present."""
        work = self._work("w1")
        review = self._review("w1", verdict="approve")
        review.review_head_sha = "abc123"
        review.review_patch_id = "patchid-old"

        entry = _q("w1", branch="worker/w1")
        entry.branch_head_sha = "def456"
        entry.branch_patch_id = "patchid-new"  # conflict resolution changed content

        board = self._board(completed=[work, review])
        assert mq.has_approved_review(entry, board) is False

    def test_has_approved_review_missing_patch_id_fails_closed(self) -> None:
        """#1475: when the patch-id can't be computed on either side, the SHA
        mismatch alone must still void the approval (fail closed, not open)."""
        work = self._work("w1")
        review = self._review("w1", verdict="approve")
        review.review_head_sha = "abc123"
        review.review_patch_id = None  # patch-id unavailable at review time

        entry = _q("w1", branch="worker/w1")
        entry.branch_head_sha = "def456"
        entry.branch_patch_id = None  # patch-id unavailable at merge time

        board = self._board(completed=[work, review])
        assert mq.has_approved_review(entry, board) is False

    def test_has_approved_review_one_sided_patch_id_fails_closed(self) -> None:
        """#1475: a patch-id present on only one side must not be trusted."""
        work = self._work("w1")
        review = self._review("w1", verdict="approve")
        review.review_head_sha = "abc123"
        review.review_patch_id = "patchid-same"

        entry = _q("w1", branch="worker/w1")
        entry.branch_head_sha = "def456"
        entry.branch_patch_id = None  # merge-time fetch failed

        board = self._board(completed=[work, review])
        assert mq.has_approved_review(entry, board) is False

    # ── #1506: compute branch_patch_id on demand instead of voiding ────────

    def test_has_approved_review_null_branch_patch_id_computed_via_gh_ops(self) -> None:
        """#1506: an entry whose approval predates #1475 (branch_patch_id
        never backfilled) must not be voided outright — when gh_ops is
        supplied, the current patch-id is computed on demand and, if it
        matches the review's, the approval still counts."""
        work = self._work("w1")
        review = self._review("w1", verdict="approve")
        review.review_head_sha = "abc123"
        review.review_patch_id = "patchid-same"

        entry = _q("w1", branch="worker/w1", target="main", repo_github="acme/api")
        entry.branch_head_sha = "def456"  # rebased — SHA moved
        entry.branch_patch_id = None      # never backfilled (pre-#1475 review)

        board = self._board(completed=[work, review])

        class _Gh:
            calls: list[tuple[str, str, str]] = []
            def get_branch_patch_id(self, repo: str, base: str, branch: str) -> str | None:
                self.calls.append((repo, base, branch))
                return "patchid-same"

        gh = _Gh()
        assert mq.has_approved_review(entry, board, gh) is True
        assert gh.calls == [("acme/api", "main", "worker/w1")]
        # #1506 acceptance: the computed value is backfilled so a later call
        # (e.g. process()'s own save_queue) persists it and never re-fetches.
        assert entry.branch_patch_id == "patchid-same"

    def test_has_approved_review_null_branch_patch_id_without_gh_ops_fails_closed(self) -> None:
        """Backward compatibility: callers that don't pass gh_ops (e.g.
        display_error, which is intentionally I/O-free) keep the pre-#1506
        fail-closed behaviour."""
        work = self._work("w1")
        review = self._review("w1", verdict="approve")
        review.review_head_sha = "abc123"
        review.review_patch_id = "patchid-same"

        entry = _q("w1", branch="worker/w1")
        entry.branch_head_sha = "def456"
        entry.branch_patch_id = None

        board = self._board(completed=[work, review])
        assert mq.has_approved_review(entry, board) is False
        assert entry.branch_patch_id is None  # never touched — no gh_ops given

    def test_has_approved_review_computed_patch_id_still_voids_on_genuine_change(self) -> None:
        """#1506: computing the patch-id on demand must not turn into a
        rubber stamp — a genuinely different diff still voids the approval."""
        work = self._work("w1")
        review = self._review("w1", verdict="approve")
        review.review_head_sha = "abc123"
        review.review_patch_id = "patchid-old"

        entry = _q("w1", branch="worker/w1", target="main", repo_github="acme/api")
        entry.branch_head_sha = "def456"
        entry.branch_patch_id = None

        class _Gh:
            def get_branch_patch_id(self, repo: str, base: str, branch: str) -> str | None:
                return "patchid-new"  # conflict resolution actually changed content

        assert mq.has_approved_review(entry, self._board(completed=[work, review]), _Gh()) is False

    def test_has_approved_review_computes_against_merge_base_not_baseRefOid(self) -> None:
        """#1506: the base passed for patch-id computation must be
        entry.target_branch (a branch name — GitHub's three-dot compare API
        resolves this to the true merge-base) and never a PR's recorded
        baseRefOid. This fixture makes the two diverge: computing against
        the (wrong) baseRefOid-like SHA yields a value that does NOT match
        the review's patch-id, while computing against the branch name
        (merge-base) yields the correct match — proving the verdict follows
        merge-base."""
        work = self._work("w1")
        review = self._review("w1", verdict="approve")
        review.review_head_sha = "abc123"
        review.review_patch_id = "patchid-correct"

        entry = _q("w1", branch="worker/w1", target="main", repo_github="acme/api")
        entry.branch_head_sha = "def456"
        entry.branch_patch_id = None

        stale_base_ref_oid = "0ldbaser3f0idsha"

        class _Gh:
            def get_branch_patch_id(self, repo: str, base: str, branch: str) -> str | None:
                if base == stale_base_ref_oid:
                    return "patchid-wrong-from-stale-base"
                if base == "main":  # entry.target_branch — the merge-base path
                    return "patchid-correct"
                return None

        board = self._board(completed=[work, review])
        assert mq.has_approved_review(entry, board, _Gh()) is True

    # ── #292 Defect 1: has_approved_review with bounce ────────────────────

    def test_has_approved_review_bounce_fix_approves(self) -> None:
        """#292: approval on fix-work is found even when entry is keyed to orig-work."""
        orig_work = self._work("orig")
        fix_work = Assignment(
            machine_name="m1",
            repo_name="api",
            issue_number=1,
            issue_title="[fix-1] t",
            assignment_id="fix1",
            type="work",
            status="done",
            # Same branch as orig_work
            branch="worker/orig",
        )
        # Review that approved the fix work (not the original)
        re_review = self._review("fix1", verdict="approve")
        # Original review requested changes
        orig_review = self._review("orig", verdict="request-changes")
        board = self._board(completed=[orig_work, orig_review, fix_work, re_review])
        # Entry keyed to orig-work (as it would be after the first coord merge)
        entry = _q("orig", branch="worker/orig")
        assert mq.has_approved_review(entry, board) is True

    def test_has_approved_review_bounce_no_approve_yet(self) -> None:
        """#292: if no approval at all across the branch, still returns False."""
        orig_work = self._work("orig")
        fix_work = Assignment(
            machine_name="m1",
            repo_name="api",
            issue_number=1,
            issue_title="[fix-1] t",
            assignment_id="fix1",
            type="work",
            status="done",
            branch="worker/orig",
        )
        orig_review = self._review("orig", verdict="request-changes")
        fix_review = self._review("fix1", verdict="request-changes")
        board = self._board(completed=[orig_work, orig_review, fix_work, fix_review])
        entry = _q("orig", branch="worker/orig")
        assert mq.has_approved_review(entry, board) is False

    # ── #567: chain resolution when a fix worker has branch=NULL ──────────

    def test_has_approved_review_bounce_fix_null_branch_approves(self) -> None:
        """#567: a fix worker dispatched with branch=NULL (the #557 gap)
        still counts — the chain is reconstructed via
        review_of_assignment_id instead of branch equality."""
        orig_work = self._work("orig")
        fix_work = Assignment(
            machine_name="m1",
            repo_name="api",
            issue_number=1,
            issue_title="[fix-1] t",
            assignment_id="fix1",
            type="work",
            status="done",
            branch=None,  # #557 remote-interactive-rework gap
            review_of_assignment_id="orig",
        )
        re_review = self._review("fix1", verdict="approve")
        orig_review = self._review("orig", verdict="request-changes")
        board = self._board(completed=[orig_work, orig_review, fix_work, re_review])
        entry = _q("orig", branch="worker/orig")
        assert mq.has_approved_review(entry, board) is True

    def test_has_approved_review_entry_keyed_to_child_finds_parent_approval(
        self,
    ) -> None:
        """#1601: the backward-chain companion to the smoke-gate regression
        above, isolated the same way — no branch bridge at all (both rows'
        branches deliberately differ from the entry's), so ONLY the
        review_of_assignment_id id-chain can connect a CHILD-keyed entry back
        to an approval recorded against its PARENT. Before #1601 the walk was
        forward-only (a known parent pulled in its child) and could not
        resolve this direction."""
        orig_work = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="orig", type="work", status="done",
            branch="worker/orig-real",
        )
        fix_work = Assignment(
            machine_name="m1", repo_name="api", issue_number=1,
            issue_title="[fix-1] t", assignment_id="fix1", type="work",
            status="done", branch=None, review_of_assignment_id="orig",
        )
        orig_review = self._review("orig", verdict="approve")
        board = self._board(completed=[orig_work, fix_work, orig_review])
        # Entry keyed to the CHILD; its own `.branch` matches neither row, so
        # branch equality contributes nothing — only the id-chain can bridge.
        entry = _q("fix1", branch="worker/entry-only")
        assert mq.has_approved_review(entry, board) is True

    def test_has_approved_review_multi_hop_null_branch_chain(self) -> None:
        """#567: a fix-of-a-fix chain (both branch=NULL) resolves via the
        fixed-point expansion, not just one hop."""
        orig_work = self._work("orig")
        fix1 = Assignment(
            machine_name="m1", repo_name="api", issue_number=1,
            issue_title="[fix-1] t", assignment_id="fix1", type="work",
            status="done", branch=None, review_of_assignment_id="orig",
        )
        fix2 = Assignment(
            machine_name="m1", repo_name="api", issue_number=1,
            issue_title="[fix-2] t", assignment_id="fix2", type="work",
            status="done", branch=None, review_of_assignment_id="fix1",
        )
        re_review = self._review("fix2", verdict="approve")
        board = self._board(completed=[orig_work, fix1, fix2, re_review])
        entry = _q("orig", branch="worker/orig")
        assert mq.has_approved_review(entry, board) is True

    # ── #292 Defect 3: skip-and-proceed instead of group-halt ────────────

    def test_process_review_gated_entry_does_not_block_approved_sibling(self) -> None:
        """#292: an un-reviewed entry should not block an approved sibling."""
        cfg = self._config()
        approved_work = self._work("approved")
        approved_review = self._review("approved", verdict="approve")
        board = self._board(completed=[
            self._work("ungated"),  # no review
            approved_work,
            approved_review,
        ])
        # Two entries in the same (repo, target) group
        items = [
            _q("ungated", size=10),
            _q("approved", size=20),
        ]
        gh = FakeGh()
        events = process(items, gh, config=cfg, board=board)

        kinds = [e.kind for e in events]
        # ungated entry is blocked
        assert "review_required" in kinds
        # approved entry still merges
        assert "merged" in kinds
        # Both PRC opened
        assert len(gh.create_calls) == 2
        states = {x.assignment_id: x.state for x in items}
        assert states["ungated"] == PENDING
        assert states["approved"] == MERGED

    def test_process_review_gated_entry_does_not_block_first_entry_if_second_approved(self) -> None:
        """#292: approved entry merges even when it is sequenced AFTER a blocked one."""
        cfg = self._config()
        board = self._board(completed=[
            self._work("blocked"),  # no review
            self._work("approved"),
            self._review("approved", verdict="approve"),
        ])
        # Explicit ordering: blocked first, approved second
        items = [_q("blocked", size=5), _q("approved", size=50)]
        gh = FakeGh()
        events = process(items, gh, config=cfg, board=board, presorted=True)

        kinds = [e.kind for e in events]
        assert "review_required" in kinds
        assert "merged" in kinds
        states = {x.assignment_id: x.state for x in items}
        assert states["blocked"] == PENDING
        assert states["approved"] == MERGED

    # ── #292 Defect 4: dry-run applies the review gate ────────────────────

    def test_dry_run_shows_review_required_for_unapproved(self) -> None:
        """#292: dry-run must surface review_required, not 'would merge'."""
        cfg = self._config()
        board = self._board(completed=[self._work("w1")])  # no approval
        items = [_q("w1", size=10)]
        gh = FakeGh()
        events = process(items, gh, config=cfg, board=board, dry_run=True)

        kinds = [e.kind for e in events]
        assert "review_required" in kinds
        assert "merged" not in kinds
        # dry-run never touches state
        assert items[0].state == PENDING

    def test_dry_run_reports_unknown_head_not_a_fabricated_refusal(self) -> None:
        """#2704: the dry-run "would be blocked" message must name the
        unreadable branch head, not fabricate "not approved" — the same
        distinction the live path makes, asserted through the dry-run
        message-construction site specifically."""
        cfg = self._config()
        work = self._work("w1")
        review = self._review("w1", verdict="approve")
        review.review_head_sha = "sha-any"  # review DID capture a SHA to compare
        board = self._board(completed=[work, review])
        items = [_q("w1", size=10)]  # branch_head_sha left None — unconfirmable
        gh = FakeGh()
        events = process(items, gh, config=cfg, board=board, dry_run=True)

        kinds = [e.kind for e in events]
        assert "review_required" in kinds
        assert "merged" not in kinds
        review_event = next(e for e in events if e.kind == "review_required")
        assert mq.UNKNOWN_BRANCH_HEAD_REASON in review_event.message
        assert "not approved" not in review_event.message

    def test_dry_run_shows_merged_for_approved(self) -> None:
        """#292: dry-run with a real approval → would-merge event."""
        cfg = self._config()
        board = self._board(completed=[
            self._work("w1"),
            self._review("w1", verdict="approve"),
        ])
        items = [_q("w1", size=10)]
        gh = FakeGh()
        events = process(items, gh, config=cfg, board=board, dry_run=True)

        kinds = [e.kind for e in events]
        assert "merged" in kinds
        assert "review_required" not in kinds
        assert items[0].state == PENDING  # dry-run: state untouched


class TestScopedReviewCandidate:
    """#1476: find_scoped_review_candidate / only_conflict_fix_since_review —
    the pure-logic gate deciding whether a voided approval qualifies for a
    re-review SCOPED to the conflict-fix resolution delta instead of a full
    re-review of the whole PR."""

    @staticmethod
    def _board(active=None, completed=None):
        from coord.models import Board
        return Board(active=list(active or []), completed=list(completed or []))

    @staticmethod
    def _work(aid: str = "w1") -> Assignment:
        return Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id=aid, type="work", status="done", branch=f"worker/{aid}",
        )

    @staticmethod
    def _review(
        of_aid: str, *, verdict: str | None = "approve", status: str = "done",
        head_sha: str | None = "abc123", patch_id: str | None = "patchid-old",
        dispatched_at: float = 100.0,
    ) -> Assignment:
        return Assignment(
            machine_name="m2", repo_name="api", issue_number=1, issue_title="t",
            assignment_id=f"rev-{of_aid}", type="review", status=status,
            review_of_assignment_id=of_aid, review_verdict=verdict,
            review_head_sha=head_sha, review_patch_id=patch_id,
            dispatched_at=dispatched_at,
        )

    @staticmethod
    def _conflict_fix(
        merge_entry_id: str, *, status: str = "done", dispatched_at: float = 200.0,
    ) -> Assignment:
        return Assignment(
            machine_name="m1", repo_name="api", issue_number=1,
            issue_title="[conflict-fix] t", assignment_id="cf1",
            type="conflict-fix", status=status,
            review_of_assignment_id=merge_entry_id, dispatched_at=dispatched_at,
        )

    def _voided_entry(self) -> QueuedMerge:
        entry = _q("w1", branch="worker/w1")
        entry.branch_head_sha = "def456"
        entry.branch_patch_id = "patchid-new"
        return entry

    # ── find_scoped_review_candidate ───────────────────────────────────────

    def test_finds_candidate_on_patch_id_mismatch(self) -> None:
        work = self._work("w1")
        review = self._review("w1")
        board = self._board(completed=[work, review])
        entry = self._voided_entry()
        found = mq.find_scoped_review_candidate(entry, board)
        assert found is review

    def test_returns_none_when_content_identical(self) -> None:
        """#1475 already carries this approval forward — nothing to scope."""
        work = self._work("w1")
        review = self._review("w1", patch_id="patchid-same")
        board = self._board(completed=[work, review])
        entry = _q("w1", branch="worker/w1")
        entry.branch_head_sha = "def456"
        entry.branch_patch_id = "patchid-same"
        assert mq.find_scoped_review_candidate(entry, board) is None

    def test_returns_none_when_sha_unchanged(self) -> None:
        work = self._work("w1")
        review = self._review("w1", head_sha="abc123")
        board = self._board(completed=[work, review])
        entry = _q("w1", branch="worker/w1")
        entry.branch_head_sha = "abc123"  # nothing moved
        entry.branch_patch_id = "patchid-old"
        assert mq.find_scoped_review_candidate(entry, board) is None

    def test_returns_none_when_review_patch_id_missing(self) -> None:
        """Fail closed — an unconfirmable diff gets a full review."""
        work = self._work("w1")
        review = self._review("w1", patch_id=None)
        board = self._board(completed=[work, review])
        entry = self._voided_entry()
        assert mq.find_scoped_review_candidate(entry, board) is None

    def test_returns_none_when_current_patch_id_missing(self) -> None:
        work = self._work("w1")
        review = self._review("w1")
        board = self._board(completed=[work, review])
        entry = _q("w1", branch="worker/w1")
        entry.branch_head_sha = "def456"
        entry.branch_patch_id = None
        assert mq.find_scoped_review_candidate(entry, board) is None

    def test_computes_current_patch_id_via_gh_ops_when_null(self) -> None:
        """#1506: when gh_ops is supplied, a null branch_patch_id is computed
        on demand (same as has_approved_review) instead of bailing out
        immediately — so a genuinely-voided pre-#1475 approval can still be
        scoped to the conflict-fix delta rather than falling to a full
        re-review."""
        work = self._work("w1")
        review = self._review("w1", patch_id="patchid-old")
        board = self._board(completed=[work, review])
        entry = _q("w1", branch="worker/w1", target="main", repo_github="acme/api")
        entry.branch_head_sha = "def456"
        entry.branch_patch_id = None

        class _Gh:
            def get_branch_patch_id(self, repo: str, base: str, branch: str) -> str | None:
                assert (repo, base, branch) == ("acme/api", "main", "worker/w1")
                return "patchid-new"

        found = mq.find_scoped_review_candidate(entry, board, _Gh())
        assert found is review
        assert entry.branch_patch_id == "patchid-new"  # backfilled, computed once

    def test_returns_none_when_no_review_at_all(self) -> None:
        work = self._work("w1")
        board = self._board(completed=[work])
        entry = self._voided_entry()
        assert mq.find_scoped_review_candidate(entry, board) is None

    def test_returns_none_when_verdict_was_request_changes(self) -> None:
        work = self._work("w1")
        review = self._review("w1", verdict="request-changes")
        board = self._board(completed=[work, review])
        entry = self._voided_entry()
        assert mq.find_scoped_review_candidate(entry, board) is None

    # ── only_conflict_fix_since_review ──────────────────────────────────────

    def test_true_when_only_a_conflict_fix_intervened(self) -> None:
        work = self._work("w1")
        review = self._review("w1", dispatched_at=100.0)
        cf = self._conflict_fix("w1", dispatched_at=200.0)
        board = self._board(completed=[work, review, cf])
        entry = self._voided_entry()
        assert mq.only_conflict_fix_since_review(entry, board, review) is True

    def test_false_when_no_conflict_fix_found(self) -> None:
        """Nothing to attribute the content change to — fail closed."""
        work = self._work("w1")
        review = self._review("w1", dispatched_at=100.0)
        board = self._board(completed=[work, review])
        entry = self._voided_entry()
        assert mq.only_conflict_fix_since_review(entry, board, review) is False

    def test_false_when_a_fix_round_also_ran_after_the_review(self) -> None:
        """Guardrail: any other new commit ⇒ full review, not scoped."""
        work = self._work("w1")
        review = self._review("w1", dispatched_at=100.0)
        cf = self._conflict_fix("w1", dispatched_at=200.0)
        fix_work = Assignment(
            machine_name="m1", repo_name="api", issue_number=1,
            issue_title="[fix-1] t", assignment_id="fix1", type="work",
            status="done", branch="worker/w1",
            review_of_assignment_id="w1", dispatched_at=250.0,
        )
        board = self._board(completed=[work, review, cf, fix_work])
        entry = self._voided_entry()
        assert mq.only_conflict_fix_since_review(entry, board, review) is False

    def test_conflict_fix_before_review_not_relevant(self) -> None:
        """A conflict-fix that ran BEFORE this review doesn't count as the
        source of the post-approval content change — fail closed."""
        work = self._work("w1")
        review = self._review("w1", dispatched_at=300.0)
        cf = self._conflict_fix("w1", dispatched_at=100.0)  # earlier
        board = self._board(completed=[work, review, cf])
        entry = self._voided_entry()
        assert mq.only_conflict_fix_since_review(entry, board, review) is False

    def test_ignores_conflict_fix_for_a_different_entry(self) -> None:
        work = self._work("w1")
        review = self._review("w1", dispatched_at=100.0)
        cf = self._conflict_fix("other-entry", dispatched_at=200.0)
        board = self._board(completed=[work, review, cf])
        entry = self._voided_entry()
        assert mq.only_conflict_fix_since_review(entry, board, review) is False

    def test_ignores_failed_conflict_fix(self) -> None:
        work = self._work("w1")
        review = self._review("w1", dispatched_at=100.0)
        cf = self._conflict_fix("w1", status="failed", dispatched_at=200.0)
        board = self._board(completed=[work, review, cf])
        entry = self._voided_entry()
        assert mq.only_conflict_fix_since_review(entry, board, review) is False

    def test_fix_round_before_review_does_not_disqualify(self) -> None:
        """A fix round that ran BEFORE the review (and so is exactly what the
        review covered) must not itself disqualify the scoped path."""
        earlier_fix = Assignment(
            machine_name="m1", repo_name="api", issue_number=1,
            issue_title="[fix-1] t", assignment_id="fix1", type="work",
            status="done", branch="worker/w1",
            review_of_assignment_id="w1", dispatched_at=50.0,
        )
        work = self._work("w1")
        review = self._review("w1", dispatched_at=100.0)
        cf = self._conflict_fix("w1", dispatched_at=200.0)
        board = self._board(completed=[work, earlier_fix, review, cf])
        entry = self._voided_entry()
        assert mq.only_conflict_fix_since_review(entry, board, review) is True

    # ── intervening_work_since_review ───────────────────────────────────────
    # #1488: `coord review-reaffirm` needs to tell only_conflict_fix_since_
    # review's two distinct False reasons apart — "a new work/fix round landed"
    # (hard refuse) vs "no conflict-fix explains the delta" (warn, the
    # hand-run-rebase case the escape hatch exists for).

    def test_intervening_empty_when_only_a_conflict_fix_ran(self) -> None:
        work = self._work("w1")
        review = self._review("w1", dispatched_at=100.0)
        cf = self._conflict_fix("w1", dispatched_at=200.0)
        board = self._board(completed=[work, review, cf])
        entry = self._voided_entry()
        assert mq.intervening_work_since_review(entry, board, review) == []

    def test_intervening_empty_when_nothing_at_all_ran(self) -> None:
        """The hand-run-rebase case: unattributable, but NOT new logic."""
        work = self._work("w1")
        review = self._review("w1", dispatched_at=100.0)
        board = self._board(completed=[work, review])
        entry = self._voided_entry()
        assert mq.intervening_work_since_review(entry, board, review) == []
        assert mq.only_conflict_fix_since_review(entry, board, review) is False

    def test_intervening_lists_a_fix_round_dispatched_after_the_review(self) -> None:
        work = self._work("w1")
        review = self._review("w1", dispatched_at=100.0)
        fix_work = Assignment(
            machine_name="m1", repo_name="api", issue_number=1,
            issue_title="[fix-1] t", assignment_id="fix1", type="work",
            status="done", branch="worker/w1",
            review_of_assignment_id="w1", dispatched_at=250.0,
        )
        board = self._board(completed=[work, review, fix_work])
        entry = self._voided_entry()
        got = mq.intervening_work_since_review(entry, board, review)
        assert [a.assignment_id for a in got] == ["fix1"]

    def test_intervening_ignores_work_dispatched_before_the_review(self) -> None:
        earlier_fix = Assignment(
            machine_name="m1", repo_name="api", issue_number=1,
            issue_title="[fix-1] t", assignment_id="fix1", type="work",
            status="done", branch="worker/w1",
            review_of_assignment_id="w1", dispatched_at=50.0,
        )
        work = self._work("w1")
        review = self._review("w1", dispatched_at=100.0)
        board = self._board(completed=[work, earlier_fix, review])
        entry = self._voided_entry()
        assert mq.intervening_work_since_review(entry, board, review) == []

    def test_intervening_ignores_work_on_another_branch(self) -> None:
        other = Assignment(
            machine_name="m1", repo_name="api", issue_number=9, issue_title="t",
            assignment_id="w-other", type="work", status="done",
            branch="worker/other", dispatched_at=250.0,
        )
        work = self._work("w1")
        review = self._review("w1", dispatched_at=100.0)
        board = self._board(completed=[work, review, other])
        entry = self._voided_entry()
        assert mq.intervening_work_since_review(entry, board, review) == []

    def test_intervening_empty_when_review_has_no_dispatch_time(self) -> None:
        """No dispatch anchor ⇒ nothing is provably "after" ⇒ empty (matches
        only_conflict_fix_since_review's own pre-#1488 posture)."""
        work = self._work("w1")
        review = self._review("w1")
        review.dispatched_at = None
        fix_work = Assignment(
            machine_name="m1", repo_name="api", issue_number=1,
            issue_title="[fix-1] t", assignment_id="fix1", type="work",
            status="done", branch="worker/w1",
            review_of_assignment_id="w1", dispatched_at=250.0,
        )
        board = self._board(completed=[work, review, fix_work])
        entry = self._voided_entry()
        assert mq.intervening_work_since_review(entry, board, review) == []


class TestPassesMergeGates:
    """#946: passes_merge_gates() is the shared predicate composing the
    review + smoke gates, used by every enqueue path (enqueue_approved_work,
    the `coord merge` auto-enqueue loop, and enqueue()) so none of them can
    drift out of sync with the others."""

    @staticmethod
    def _config(*, reviews_enabled: bool = True, gates: list[str] | None = None):
        from dataclasses import dataclass, field as dc_field

        @dataclass
        class _Reviews:
            enabled: bool = True

        @dataclass
        class _Pipeline:
            default_gates: list[str] | None = None

        @dataclass
        class _Cfg:
            reviews: _Reviews = dc_field(default_factory=_Reviews)
            pipeline: _Pipeline = dc_field(default_factory=_Pipeline)

        cfg = _Cfg()
        cfg.reviews.enabled = reviews_enabled
        cfg.pipeline.default_gates = gates if gates is not None else ["test", "review", "merge"]
        return cfg

    @staticmethod
    def _board(active=None, completed=None):
        from coord.models import Board
        return Board(active=list(active or []), completed=list(completed or []))

    @staticmethod
    def _work(aid: str = "w1", *, test_state: str | None = None) -> Assignment:
        return Assignment(
            machine_name="m1",
            repo_name="api",
            issue_number=1,
            issue_title="t",
            assignment_id=aid,
            type="work",
            status="done",
            branch=f"worker/{aid}",
            test_state=test_state,
        )

    @staticmethod
    def _review(of_aid: str, *, verdict: str | None = "approve") -> Assignment:
        return Assignment(
            machine_name="m2",
            repo_name="api",
            issue_number=1,
            issue_title="t",
            assignment_id=f"rev-{of_aid}",
            type="review",
            status="done",
            review_of_assignment_id=of_aid,
            review_verdict=verdict,
        )

    def test_refused_on_failed_test_and_no_review(self) -> None:
        """#782 repro: failed test, no review → gate refuses."""
        cfg = self._config()
        work = self._work("w1", test_state="failed")
        board = self._board(completed=[work])
        assert mq.passes_merge_gates(work, cfg, board) is False

    def test_refused_on_no_verdict_and_no_review(self) -> None:
        """#795 repro: no test verdict at all, no review → gate refuses."""
        cfg = self._config()
        work = self._work("w1", test_state=None)
        board = self._board(completed=[work])
        assert mq.passes_merge_gates(work, cfg, board) is False

    def test_passes_with_passed_test_and_approved_review(self) -> None:
        cfg = self._config()
        work = self._work("w1", test_state="passed")
        review = self._review("w1", verdict="approve")
        board = self._board(completed=[work, review])
        assert mq.passes_merge_gates(work, cfg, board) is True

    def test_passes_when_gates_disabled(self) -> None:
        cfg = self._config(reviews_enabled=False, gates=["merge"])
        work = self._work("w1", test_state=None)
        board = self._board(completed=[work])
        assert mq.passes_merge_gates(work, cfg, board) is True

    # ── #2085: enqueue()'s own internal gate builds its confirmation data ──

    def test_enqueue_gate_confirms_a_fresh_approval_itself(
        self, monkeypatch, coord_db
    ) -> None:
        """#2085: `enqueue(..., config=...)` must gate against
        `live_gate_entry`, never the raw `Assignment` it was handed.

        FAILS against the pre-fix code: `enqueue` called
        `passes_merge_gates(assignment, config, board)` internally with no
        `gh_ops` and no way to accept one, so `has_approved_review` saw a
        raw `Assignment` (no `branch_head_sha` attribute at all) and refused
        every review carrying a real `review_head_sha`. `repo_github` and
        `target_branch` are already parameters here, so the confirmation
        data is BUILT rather than demanded — a caller cannot reintroduce the
        regression by forgetting to thread `gh_ops` through, which is
        exactly how the dashboard enqueue path was missed.
        """
        from coord import github_ops

        monkeypatch.setattr(
            github_ops, "get_branch_sha",
            lambda repo, branch: "sha-current" if branch == "worker/w1" else None,
        )

        cfg = self._config()
        work = self._work("w1", test_state="passed")
        review = self._review("w1", verdict="approve")
        review.review_head_sha = "sha-current"  # matches the live head
        board = self._board(completed=[work, review])

        entry = mq.enqueue(
            work, repo_github="acme/api", target_branch="main",
            config=cfg, board=board,
        )
        assert entry is not None, (
            "a fresh approval whose review_head_sha matches the branch's "
            "live head must pass enqueue()'s internal gate"
        )

    def test_enqueue_gate_still_refuses_a_superseded_approval(
        self, monkeypatch, coord_db
    ) -> None:
        """The companion: the #1966 chain (approved at SHA A, branch since
        moved to SHA B) must still be refused by enqueue()'s own gate — the
        fix makes the gate confirmable, not permissive."""
        from coord import github_ops

        monkeypatch.setattr(github_ops, "get_branch_sha", lambda *a, **k: "sha-new")
        monkeypatch.setattr(github_ops, "get_branch_patch_id", lambda *a, **k: None)

        cfg = self._config()
        work = self._work("w1", test_state="passed")
        review = self._review("w1", verdict="approve")
        review.review_head_sha = "sha-old"  # commits landed after approval
        board = self._board(completed=[work, review])

        assert mq.enqueue(
            work, repo_github="acme/api", target_branch="main",
            config=cfg, board=board,
        ) is None

    # ── #2704: the review gate's reason must not fabricate a refusal when
    # the branch head is genuinely unconfirmable (`ApprovalScan.unknown_head`,
    # #2085) — this is `entry.branch_head_sha` simply never having been
    # populated on this caller's entry, the same condition a live GitHub
    # read failing (rate limit, auth, network) produces after `process()`.

    def test_merge_gate_failures_reports_unknown_head_not_a_fabricated_refusal(
        self,
    ) -> None:
        cfg = self._config()
        work = self._work("w1", test_state="passed")
        review = self._review("w1", verdict="approve")
        review.review_head_sha = "sha-any"  # review DID capture a SHA to compare
        board = self._board(completed=[work, review])
        entry = _q("w1")  # branch_head_sha left None — unconfirmable here

        failures = mq.merge_gate_failures(entry, cfg, board)

        review_failure = next(f for f in failures if f.gate == "review")
        assert review_failure.reason == mq.UNKNOWN_BRANCH_HEAD_REASON
        assert "not approved" not in review_failure.reason

    def test_merge_gate_failures_still_reports_genuine_refusal(self) -> None:
        """The generic wording is preserved for an actual refusal — a
        #2704 regression guard: the new branch must not swallow every
        review failure into "unknown"."""
        cfg = self._config()
        work = self._work("w1", test_state="passed")
        board = self._board(completed=[work])  # no review at all
        entry = _q("w1")

        failures = mq.merge_gate_failures(entry, cfg, board)

        review_failure = next(f for f in failures if f.gate == "review")
        assert review_failure.reason == "review required but not approved"

    # ── #2704 follow-up: an unreadable branch head only explains the refusal
    # while the approval is still the chain's LAST word. The #2085/#1966
    # chain ends in an explicit `request-changes`, which needs no head SHA
    # to be true — reporting "branch head unknown" there hides a real
    # refusal behind a transient-looking probe failure (and tells
    # `coord.drive` to wait for a probe that can never unblock it).

    @staticmethod
    def _superseded_chain_board(board_factory):
        """The #1966 chain on one branch: approve (sha-a) → fix round →
        request-changes (sha-b), newest last by ``dispatched_at``."""
        def _work(aid: str, at: float) -> Assignment:
            return Assignment(
                machine_name="m1", repo_name="api", issue_number=1966,
                issue_title="t", assignment_id=aid, type="work", status="done",
                branch="issue-1966-fix", test_state="passed", dispatched_at=at,
            )

        def _review(aid: str, of: str, verdict: str, sha: str, at: float) -> Assignment:
            return Assignment(
                machine_name="m2", repo_name="api", issue_number=1966,
                issue_title="t", assignment_id=aid, type="review", status="done",
                review_of_assignment_id=of, review_verdict=verdict,
                review_head_sha=sha, dispatched_at=at,
            )

        return board_factory(completed=[
            _work("c908129d", 1.0),
            _review("rev-orig", "c908129d", "approve", "sha-a", 2.0),
            _work("8e3eb76e", 3.0),
            _review("rev-fix", "8e3eb76e", "request-changes", "sha-b", 4.0),
        ])

    def test_approval_superseded_by_later_request_changes_is_not_unknown_head(
        self,
    ) -> None:
        """The scan's own verdict: not approved, and NOT ``unknown_head`` —
        the refusal is on the record regardless of the head SHA."""
        board = self._superseded_chain_board(self._board)
        entry = _q("8e3eb76e", branch="issue-1966-fix")  # branch_head_sha None

        scan = mq.scan_approved_reviews(entry, board)

        assert scan.approved is False
        assert scan.unknown_head is False

    def test_merge_gate_failures_reports_the_refusal_not_the_unknown_head(
        self,
    ) -> None:
        """#2704 regression: the gate reason an operator (and the `/board`
        merge plan) reads must name the review, not the unreadable head."""
        cfg = self._config()
        board = self._superseded_chain_board(self._board)
        entry = _q("8e3eb76e", branch="issue-1966-fix")

        failures = mq.merge_gate_failures(entry, cfg, board)

        review_failure = next(f for f in failures if f.gate == "review")
        assert review_failure.reason == "review required but not approved"
        assert review_failure.reason != mq.UNKNOWN_BRANCH_HEAD_REASON

    def test_unfinished_later_review_does_not_settle_the_question(self) -> None:
        """A review still in flight (no verdict) settles nothing — the
        unreadable head remains the honest reason."""
        cfg = self._config()
        work = self._work("w1", test_state="passed")
        approve = self._review("w1", verdict="approve")
        approve.review_head_sha = "sha-any"
        approve.dispatched_at = 2.0
        pending = self._review("w1", verdict=None)
        pending.dispatched_at = 3.0
        board = self._board(completed=[work, approve], active=[pending])
        entry = _q("w1")

        failures = mq.merge_gate_failures(entry, cfg, board)

        review_failure = next(f for f in failures if f.gate == "review")
        assert review_failure.reason == mq.UNKNOWN_BRANCH_HEAD_REASON


# ── #2809: structured rate-limit detail survives from a live `get_branch_sha`
# call all the way to the operator-facing gate-refusal string, instead of
# being swallowed into the generic UNKNOWN_BRANCH_HEAD_REASON sentence.


class _RateLimitedGh(FakeGh):
    """A `gh_ops` whose `get_branch_sha` supports `raise_on_transient`
    (like the real `coord.github_ops.get_branch_sha`) and raises
    `GhRateLimitError` — simulating the #2809 incident: a live branch-head
    probe hitting GitHub's secondary rate limiter."""

    def get_branch_sha(
        self, repo: str, branch: str, *, raise_on_transient: bool = False,
    ) -> str | None:
        if raise_on_transient:
            from coord.github_ops import GhRateLimitError
            raise GhRateLimitError(
                "gh api ... failed: HTTP 403: secondary rate limit",
                status_code=403, request_id="E126:C7B0E:4B13E6D",
                retry_after_s=45.0, secondary=True,
            )
        return None


class TestUnknownBranchHeadReasonEnrichment:
    def test_none_returns_the_unchanged_constant(self) -> None:
        assert mq.unknown_branch_head_reason(None) == mq.UNKNOWN_BRANCH_HEAD_REASON

    def test_bare_transient_error_with_no_detail_returns_unchanged_constant(self) -> None:
        from coord.github_ops import GhTransientError

        reason = mq.unknown_branch_head_reason(GhTransientError("HTTP 401: Bad credentials"))
        assert reason == mq.UNKNOWN_BRANCH_HEAD_REASON

    def test_rate_limit_error_appends_structured_detail(self) -> None:
        from coord.github_ops import GhRateLimitError

        exc = GhRateLimitError(
            "gh api ... failed", status_code=403, request_id="E126:C7B0E",
            retry_after_s=45.0, secondary=True,
        )
        reason = mq.unknown_branch_head_reason(exc)
        # The generic sentence stays an exact PREFIX (`coord.drive.
        # _merge_gate_kind` substring-matches the bare constant against
        # this), with the new detail appended, not interpolated inline.
        assert reason.startswith(mq.UNKNOWN_BRANCH_HEAD_REASON)
        assert "secondary rate limit" in reason
        assert "HTTP 403" in reason
        assert "E126:C7B0E" in reason
        assert "45s" in reason

    def test_primary_rate_limit_is_worded_distinctly_from_secondary(self) -> None:
        from coord.github_ops import GhRateLimitError

        exc = GhRateLimitError(
            "gh api ... failed", status_code=403, request_id=None,
            retry_after_s=None, secondary=False,
        )
        reason = mq.unknown_branch_head_reason(exc)
        assert "secondary" not in reason
        assert "rate limit" in reason


class TestGhGetBranchShaThreeTuple:
    def test_unsupported_stub_returns_none_error(self) -> None:
        sha, probe_failed, error = mq._gh_get_branch_sha(FakeGh(), "acme/api", "main")
        assert sha is None
        assert probe_failed is False
        assert error is None

    def test_rate_limit_confirms_transient_and_returns_the_error_object(self) -> None:
        gh = _RateLimitedGh()
        sha, probe_failed, error = mq._gh_get_branch_sha(gh, "acme/api", "main")
        assert sha is None
        assert probe_failed is True
        assert error is not None
        assert error.status_code == 403
        assert error.request_id == "E126:C7B0E:4B13E6D"


class TestLiveGateEntryRateLimitPropagation:
    """`live_gate_entry` is the exact swallow site #2809 named
    (`gh_ops.get_branch_sha(...)` with no `raise_on_transient`, discarding
    the 403/Retry-After/request-id) — this is `coord merge --only`'s and
    `coord.gates.build_gate_report`'s live gate-check path."""

    def test_confirmed_rate_limit_is_captured_on_the_entry(self) -> None:
        a = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="w1", type="work", status="done", branch="issue-1-fix",
        )
        entry = mq.live_gate_entry(a, "acme/api", "main", _RateLimitedGh())

        assert entry.branch_head_sha is None
        assert entry.branch_head_probe_error is not None
        assert entry.branch_head_probe_error.status_code == 403
        assert entry.branch_head_probe_error.secondary is True

    def test_no_gh_ops_leaves_probe_error_none(self) -> None:
        a = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="w1", type="work", status="done", branch="issue-1-fix",
        )
        entry = mq.live_gate_entry(a, "acme/api", "main", None)
        assert entry.branch_head_probe_error is None

    def test_merge_gate_failures_reports_the_rate_limit_detail(self) -> None:
        """End-to-end: a rate-limited live probe reaches the operator-facing
        review-gate refusal string with status/request-id intact, exactly
        what the issue's evidence shows coord discarding today."""
        cfg = TestPassesMergeGates._config()
        work = TestPassesMergeGates._work("w1", test_state="passed")
        review = TestPassesMergeGates._review("w1", verdict="approve")
        review.review_head_sha = "sha-any"
        board = TestPassesMergeGates._board(completed=[work, review])

        a = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="w1", type="work", status="done", branch="worker/w1",
        )
        entry = mq.live_gate_entry(a, "acme/api", "main", _RateLimitedGh())

        failures = mq.merge_gate_failures(entry, cfg, board)

        review_failure = next(f for f in failures if f.gate == "review")
        assert "HTTP 403" in review_failure.reason
        assert "E126:C7B0E:4B13E6D" in review_failure.reason
        assert review_failure.reason.startswith(mq.UNKNOWN_BRANCH_HEAD_REASON)


class TestLiveAnchorEntry:
    """`live_anchor_entry` (#2809 review) — factored out of `live_gate_entry`
    so a caller holding an already-real, already-persisted `QueuedMerge`
    (not a raw work `Assignment`) can re-anchor it against CURRENT GitHub
    state in place. `coord merge --only` is the motivating caller: it
    resolves its entry straight off the queue DB, which may never have been
    live-anchored (or was, long before this invocation), so its
    `branch_head_probe_error` was stale/unset — exactly the gap the issue's
    own `coord merge --only` reproduction hit for the review gate."""

    def _entry(self) -> "mq.QueuedMerge":
        return mq.QueuedMerge(
            assignment_id="w1", repo_name="api", repo_github="acme/api",
            branch="worker/w1", target_branch="main", issue_number=1,
            issue_title="t", state=mq.PENDING,
        )

    def test_populates_probe_error_on_an_existing_entry(self) -> None:
        entry = self._entry()
        assert entry.branch_head_probe_error is None

        mq.live_anchor_entry(entry, _RateLimitedGh())

        assert entry.branch_head_sha is None
        assert entry.branch_head_probe_error is not None
        assert entry.branch_head_probe_error.status_code == 403
        assert entry.branch_head_probe_error.secondary is True

    def test_no_gh_ops_is_a_no_op(self) -> None:
        entry = self._entry()
        mq.live_anchor_entry(entry, None)
        assert entry.branch_head_probe_error is None
        assert entry.branch_head_sha is None

    def test_refreshes_a_previously_confirmed_sha(self) -> None:
        """A stale `branch_head_sha` from an earlier tick must be replaced
        by the live value, not merely left alone when it's already set."""
        entry = self._entry()
        entry.branch_head_sha = "stale-sha-from-a-much-earlier-tick"

        class _FreshGh(FakeGh):
            def get_branch_sha(self, repo, branch, *, raise_on_transient=False):
                return "fresh-sha"

        mq.live_anchor_entry(entry, _FreshGh())

        assert entry.branch_head_sha == "fresh-sha"

    def test_only_path_merge_gate_failures_reports_enriched_detail_after_anchoring(
        self,
    ) -> None:
        """End-to-end for the non-blocking finding: an entry resolved off
        the queue DB with NO probe error recorded yet (the `--only` shape)
        must, after `live_anchor_entry`, produce the same enriched
        review-gate reason a freshly-built `live_gate_entry` would — not the
        bare generic fallback string."""
        cfg = TestPassesMergeGates._config()
        work = TestPassesMergeGates._work("w1", test_state="passed")
        review = TestPassesMergeGates._review("w1", verdict="approve")
        review.review_head_sha = "sha-any"
        board = TestPassesMergeGates._board(completed=[work, review])

        entry = self._entry()
        assert entry.branch_head_probe_error is None  # the --only-path gap

        mq.live_anchor_entry(entry, _RateLimitedGh())
        failures = mq.merge_gate_failures(entry, cfg, board)

        review_failure = next(f for f in failures if f.gate == "review")
        assert "HTTP 403" in review_failure.reason
        assert "E126:C7B0E:4B13E6D" in review_failure.reason


class TestHasPassedTest:
    """#2350: :func:`has_passed_test` — the Merge-only fast path's bare
    recorded-state read, deliberately narrower than :func:`has_smoke_verdict`
    (no staleness re-derivation, no ``skipped`` short-circuit, no *gh_ops*)."""

    @staticmethod
    def _board(active=None, completed=None):
        from coord.models import Board
        return Board(active=list(active or []), completed=list(completed or []))

    @staticmethod
    def _work(aid: str = "w1", *, test_state: str | None = None) -> Assignment:
        return Assignment(
            machine_name="m1",
            repo_name="api",
            issue_number=1,
            issue_title="t",
            assignment_id=aid,
            type="work",
            status="done",
            branch=f"worker/{aid}",
            test_state=test_state,
        )

    def test_finds_a_passed_verdict_on_the_chained_work_assignment(self) -> None:
        work = self._work("w1", test_state="passed")
        board = self._board(completed=[work])
        assert mq.has_passed_test(_q("w1"), board) is True

    def test_missing_verdict_reads_false(self) -> None:
        work = self._work("w1", test_state=None)
        board = self._board(completed=[work])
        assert mq.has_passed_test(_q("w1"), board) is False

    def test_failed_verdict_reads_false(self) -> None:
        work = self._work("w1", test_state="failed")
        board = self._board(completed=[work])
        assert mq.has_passed_test(_q("w1"), board) is False

    def test_skipped_verdict_reads_false_unlike_the_smoke_gate(self) -> None:
        """The deliberate narrowing vs. :func:`has_smoke_verdict`: a
        `skipped` verdict is a true smoke-gate pass but not literally "Test
        passed", so #2350's fast path must not treat it as one."""
        work = self._work("w1", test_state="skipped")
        board = self._board(completed=[work])
        assert mq.has_passed_test(_q("w1"), board) is False

    def test_ignores_an_unrelated_work_assignments_verdict(self) -> None:
        work = self._work("w99", test_state="passed")
        board = self._board(completed=[work])
        assert mq.has_passed_test(_q("w1"), board) is False

    def test_no_matching_work_at_all_reads_false(self) -> None:
        board = self._board(completed=[])
        assert mq.has_passed_test(_q("w1"), board) is False


class TestSmokeGate:
    """#465: process() must refuse to merge when interactive smoke is required
    and no passing/skipped verdict is recorded on the work assignment.

    The smoke gate is the second gate (after review, before CI).  It mirrors
    the review gate in structure: skip-not-halt, same legacy-caller semantics,
    dry-run applies it.
    """

    @staticmethod
    def _config(*, gates: list[str] | None = None):
        """Build a minimal config-like object that includes the smoke gate."""
        from dataclasses import dataclass, field as dc_field
        @dataclass
        class _Reviews:
            enabled: bool = False  # review gate off by default in smoke tests
        @dataclass
        class _Pipeline:
            default_gates: list[str] | None = None
        @dataclass
        class _Cfg:
            reviews: _Reviews = dc_field(default_factory=_Reviews)
            pipeline: _Pipeline = dc_field(default_factory=_Pipeline)
        cfg = _Cfg()
        cfg.pipeline.default_gates = gates if gates is not None else ["test", "merge"]
        return cfg

    @staticmethod
    def _board(completed=None, active=None):
        from coord.models import Board
        return Board(active=list(active or []), completed=list(completed or []))

    @staticmethod
    def _work(aid: str = "w1", *, test_state: str | None = None) -> Assignment:
        return Assignment(
            machine_name="m1",
            repo_name="api",
            issue_number=1,
            issue_title="t",
            assignment_id=aid,
            type="work",
            status="done",
            branch=f"worker/{aid}",
            test_state=test_state,
        )

    # ── requires_smoke / has_smoke_verdict helpers ──

    def test_requires_smoke_honours_config(self) -> None:
        cfg_with = self._config(gates=["test", "merge"])
        assert mq.requires_smoke(_q("a"), cfg_with) is True

    def test_requires_smoke_false_when_test_not_in_gates(self) -> None:
        cfg_without = self._config(gates=["review", "merge"])
        assert mq.requires_smoke(_q("a"), cfg_without) is False

    def test_requires_smoke_false_when_no_pipeline(self) -> None:
        from dataclasses import dataclass
        @dataclass
        class _NoPipelineCfg:
            pass
        assert mq.requires_smoke(_q("a"), _NoPipelineCfg()) is False

    def test_requires_smoke_entry_override_bypasses_default(self) -> None:
        # #1213: an entry whose snapshotted required_gates drops "test"
        # bypasses the smoke gate even though the default policy requires it.
        cfg = self._config(gates=["test", "merge"])
        entry = _q("a", required_gates=["merge"])
        assert mq.requires_smoke(entry, cfg) is False

    def test_requires_smoke_entry_override_can_also_require_it(self) -> None:
        cfg = self._config(gates=["merge"])
        entry = _q("a", required_gates=["test", "merge"])
        assert mq.requires_smoke(entry, cfg) is True

    def test_requires_smoke_empty_entry_gates_falls_back_to_default(self) -> None:
        # #1213 compatibility contract: untagged work (entry.required_gates
        # empty/absent) must behave exactly as before — default policy wins.
        cfg = self._config(gates=["test", "merge"])
        assert mq.requires_smoke(_q("a", required_gates=[]), cfg) is True
        cfg_without = self._config(gates=["merge"])
        assert mq.requires_smoke(_q("a", required_gates=[]), cfg_without) is False

    def test_has_smoke_verdict_passed(self) -> None:
        work = self._work("w1", test_state="passed")
        board = self._board(completed=[work])
        assert mq.has_smoke_verdict(_q("w1"), board) is True

    def test_has_smoke_verdict_skipped(self) -> None:
        work = self._work("w1", test_state="skipped")
        board = self._board(completed=[work])
        assert mq.has_smoke_verdict(_q("w1"), board) is True

    def test_has_smoke_verdict_none_returns_false(self) -> None:
        work = self._work("w1", test_state=None)
        board = self._board(completed=[work])
        assert mq.has_smoke_verdict(_q("w1"), board) is False

    def test_has_smoke_verdict_failed_returns_false(self) -> None:
        work = self._work("w1", test_state="failed")
        board = self._board(completed=[work])
        assert mq.has_smoke_verdict(_q("w1"), board) is False

    def test_has_smoke_verdict_mock_author_none_returns_false(self) -> None:
        """#930 fix: a ``type="mock-author"`` (Gate A) entry with no test
        verdict must correctly fail the gate (``False``), not silently fail
        open — before the fix, the ``type == "work"`` filter excluded the
        mock-author row itself from ``branch_work``, so this incorrectly
        returned ``True`` (fail-open) regardless of ``test_state``."""
        mock_author = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="ma1", type="mock-author", status="done",
            branch="ms-5-gate-a", test_state=None,
        )
        board = self._board(completed=[mock_author])
        assert mq.has_smoke_verdict(_q("ma1", branch="ms-5-gate-a"), board) is False

    def test_has_smoke_verdict_mock_author_passed(self) -> None:
        """#930 fix: same as above but with a passed verdict — must now
        correctly return True by actually checking test_state, rather than
        via the old accidental fail-open."""
        mock_author = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="ma1", type="mock-author", status="done",
            branch="ms-5-gate-a", test_state="passed",
        )
        board = self._board(completed=[mock_author])
        assert mq.has_smoke_verdict(_q("ma1", branch="ms-5-gate-a"), board) is True

    def test_has_smoke_verdict_test_author_none_returns_false(self) -> None:
        """#1141 fix: a ``type="test-author"`` (#931, per-issue JIT
        acceptance-slice authoring) entry with no test verdict must correctly
        fail the gate (``False``), not silently fail open — mirrors the
        mock-author fix from #930, which test-author never got."""
        test_author = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="ta1", type="test-author", status="done",
            branch="ms-37-test-author", test_state=None,
        )
        board = self._board(completed=[test_author])
        assert mq.has_smoke_verdict(_q("ta1", branch="ms-37-test-author"), board) is False

    def test_has_smoke_verdict_test_author_skipped(self) -> None:
        """#1141 fix: same as above but with a ``skipped`` verdict — the
        expected verdict for a fixtures/tests-only test-author diff (nothing
        to smoke) — must correctly return True."""
        test_author = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="ta1", type="test-author", status="done",
            branch="ms-37-test-author", test_state="skipped",
        )
        board = self._board(completed=[test_author])
        assert mq.has_smoke_verdict(_q("ta1", branch="ms-37-test-author"), board) is True

    def test_has_smoke_verdict_no_matching_work_fails_open(self) -> None:
        """When no work assignment for the branch is found on the board, the
        gate fails open (returns True) — can't block without evidence."""
        # Work on a different branch — does not count for entry w1.
        unrelated = Assignment(
            machine_name="m1", repo_name="api", issue_number=2, issue_title="t",
            assignment_id="w99", type="work", status="done",
            branch="worker/w99", test_state="passed",
        )
        board = self._board(completed=[unrelated])
        # No work for "w1"'s branch on the board → fail open.
        assert mq.has_smoke_verdict(_q("w1"), board) is True

    def test_has_smoke_verdict_empty_board_fails_open(self) -> None:
        """Empty board → fail open."""
        board = self._board()
        assert mq.has_smoke_verdict(_q("w1"), board) is True

    def test_has_smoke_verdict_bounce_fix_counts(self) -> None:
        """Fix-work on the same branch with a passing test_state satisfies the gate."""
        orig_work = self._work("orig", test_state=None)
        fix_work = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="[fix] t",
            assignment_id="fix1", type="work", status="done",
            branch="worker/orig",  # same branch as orig_work
            test_state="passed",
        )
        board = self._board(completed=[orig_work, fix_work])
        entry = _q("orig", branch="worker/orig")
        assert mq.has_smoke_verdict(entry, board) is True

    def test_has_smoke_verdict_bounce_fix_null_branch_counts(self) -> None:
        """#567: fix-work with branch=NULL (the #557 gap) still satisfies the
        gate — resolved via review_of_assignment_id instead of branch."""
        orig_work = self._work("orig", test_state=None)
        fix_work = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="[fix] t",
            assignment_id="fix1", type="work", status="done",
            branch=None,  # #557 remote-interactive-rework gap
            review_of_assignment_id="orig",
            test_state="passed",
        )
        board = self._board(completed=[orig_work, fix_work])
        entry = _q("orig", branch="worker/orig")
        assert mq.has_smoke_verdict(entry, board) is True

    def test_has_smoke_verdict_entry_keyed_to_child_finds_parent_verdict(self) -> None:
        """#1601: the #567 chain walk was forward-only — a known PARENT
        pulled in its child, but an entry keyed to the CHILD (the fix round,
        e.g. after #292's re-keying) could not walk *backward* to reach a
        parent whose own smoke/test verdict is the only one on the branch —
        exactly the #1566 incident shape (a fix round approved by review but
        never re-tested). Isolated with NO branch bridge at all (both rows'
        branches deliberately differ from the entry's) so only the
        review_of_assignment_id id-chain can connect them — the chain must
        be symmetric: it should not matter which round it's keyed to."""
        orig_work = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="orig", type="work", status="done",
            branch="worker/orig-real", test_state="passed",
        )
        fix_work = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="[fix] t",
            assignment_id="fix1", type="work", status="done",
            branch=None,  # #557 remote-interactive-rework gap
            review_of_assignment_id="orig",
            test_state=None,  # the fix round never re-ran its own test/smoke
        )
        board = self._board(completed=[orig_work, fix_work])
        # Entry keyed to the CHILD (fix1) — the #292 re-key direction. Its own
        # `.branch` matches neither row, so branch equality contributes
        # nothing — only the id-chain can bridge to orig_work's verdict.
        entry = _q("fix1", branch="worker/entry-only")
        assert mq.has_smoke_verdict(entry, board) is True

    # ── #1479: test-verdict staleness (base-moved vs content-changed) ──

    def test_has_smoke_verdict_stale_when_base_moved(self) -> None:
        """Base moved, branch diff identical → test verdict is stale even
        though the branch's own content fingerprint didn't change."""
        work = self._work("w1", test_state="passed")
        work.test_head_sha = "branch-sha-1"
        work.test_patch_id = "patch-1"
        work.test_base_sha = "main-sha-old"
        board = self._board(completed=[work])

        entry = _q("w1")
        entry.branch_head_sha = "branch-sha-1"       # unchanged
        entry.branch_patch_id = "patch-1"             # unchanged — identical content
        entry.target_branch_head_sha = "main-sha-new"  # main advanced since the test ran

        assert mq.has_smoke_verdict(entry, board) is False

    def test_has_smoke_verdict_stale_when_branch_content_changed(self) -> None:
        """Branch content changed (new commit) → test verdict is stale."""
        work = self._work("w1", test_state="passed")
        work.test_head_sha = "branch-sha-1"
        work.test_patch_id = "patch-1"
        work.test_base_sha = "main-sha-1"
        board = self._board(completed=[work])

        entry = _q("w1")
        entry.branch_head_sha = "branch-sha-2"    # new commit pushed
        entry.branch_patch_id = "patch-2"          # content actually changed
        entry.target_branch_head_sha = "main-sha-1"  # base unchanged

        assert mq.has_smoke_verdict(entry, board) is False

    def test_has_smoke_verdict_fresh_when_neither_moved(self) -> None:
        """Base unchanged, branch content unchanged → verdict still counts."""
        work = self._work("w1", test_state="passed")
        work.test_head_sha = "branch-sha-1"
        work.test_patch_id = "patch-1"
        work.test_base_sha = "main-sha-1"
        board = self._board(completed=[work])

        entry = _q("w1")
        entry.branch_head_sha = "branch-sha-1"
        entry.branch_patch_id = "patch-1"
        entry.target_branch_head_sha = "main-sha-1"

        assert mq.has_smoke_verdict(entry, board) is True

    def test_has_smoke_verdict_fresh_across_content_identical_rebase(self) -> None:
        """SHA moved but the diff didn't (a clean rebase that replayed onto
        the *same* base tip) — falls back to the patch-id match, same as the
        review gate's #1475 behaviour."""
        work = self._work("w1", test_state="passed")
        work.test_head_sha = "branch-sha-1"
        work.test_patch_id = "patch-1"
        work.test_base_sha = "main-sha-1"
        board = self._board(completed=[work])

        entry = _q("w1")
        entry.branch_head_sha = "branch-sha-2"    # commit SHA changed...
        entry.branch_patch_id = "patch-1"          # ...but content is identical
        entry.target_branch_head_sha = "main-sha-1"

        assert mq.has_smoke_verdict(entry, board) is True

    def test_has_smoke_verdict_missing_anchors_fails_open(self) -> None:
        """Rows predating #1479 (no test_base_sha/test_head_sha captured)
        skip the staleness check entirely — same backward-compat contract as
        #821/#1475 for the review gate."""
        work = self._work("w1", test_state="passed")
        board = self._board(completed=[work])

        entry = _q("w1")
        entry.branch_head_sha = "branch-sha-2"
        entry.branch_patch_id = "patch-2"
        entry.target_branch_head_sha = "main-sha-new"

        assert mq.has_smoke_verdict(entry, board) is True

    # ── process() smoke gate ──

    def test_process_emits_smoke_required_when_no_verdict(self) -> None:
        """No smoke verdict → PR is opened but merge is blocked."""
        cfg = self._config()
        work = self._work("w1", test_state=None)
        board = self._board(completed=[work])
        items = [_q("w1", size=10)]
        gh = FakeGh()
        events = process(items, gh, config=cfg, board=board)

        kinds = [e.kind for e in events]
        assert "opened" in kinds
        assert "smoke_required" in kinds
        assert "merged" not in kinds
        assert gh.merge_calls == []
        assert items[0].state == PENDING
        assert items[0].error == "smoke test required but no verdict recorded"

    def test_process_proceeds_when_smoke_passed(self) -> None:
        """Smoke passed → merge proceeds (no smoke_required event)."""
        cfg = self._config()
        work = self._work("w1", test_state="passed")
        board = self._board(completed=[work])
        items = [_q("w1", size=10)]
        gh = FakeGh()
        events = process(items, gh, config=cfg, board=board)

        assert any(e.kind == "merged" for e in events)
        assert not any(e.kind == "smoke_required" for e in events)
        assert items[0].state == MERGED

    def test_process_proceeds_when_smoke_skipped(self) -> None:
        """Smoke skipped → merge proceeds."""
        cfg = self._config()
        work = self._work("w1", test_state="skipped")
        board = self._board(completed=[work])
        items = [_q("w1", size=10)]
        gh = FakeGh()
        events = process(items, gh, config=cfg, board=board)

        assert any(e.kind == "merged" for e in events)
        assert items[0].state == MERGED

    def test_process_gate_a_test_author_skipped_verdict_merges_despite_moved_base(
        self,
    ) -> None:
        """#1732 acceptance: a Gate-A test-author slice recorded `skipped`
        ("contract/fixture-only diff, nothing to smoke-test" — #1076/#1152)
        must merge on its own — no `--skip-smoke`, no human — even though a
        sibling merge has since moved the target branch out from under the
        recorded anchor. This is the unattended oracle-loop path #1732 was
        filed to unblock: `skipped` is a structural statement about the
        diff's shape, not a measurement at a SHA, so it cannot go stale."""
        cfg = self._config()
        work = self._work("w1", test_state="skipped")
        work.type = "test-author"
        work.test_head_sha = "branch-sha"
        work.test_patch_id = "patch-1"
        work.test_base_sha = "base-old"
        board = self._board(completed=[work])
        items = [_q("w1", size=10, assignment_type="test-author")]

        class _MovedBaseGh(FakeGh):
            def get_branch_sha(self, repo: str, branch: str) -> str | None:
                return "base-new" if branch == "main" else "branch-sha"

            def get_branch_patch_id(self, repo: str, base: str, branch: str) -> str | None:
                return "patch-1"

        events = process(items, _MovedBaseGh(), config=cfg, board=board)

        kinds = [e.kind for e in events]
        assert "smoke_required" not in kinds, (
            "a `skipped` verdict must never be treated as #1479-stale (#1732)"
        )
        assert "merged" in kinds
        assert items[0].state == MERGED

    def test_process_skip_smoke_bypasses_gate(self) -> None:
        """--skip-smoke must let a no-verdict merge proceed."""
        cfg = self._config()
        work = self._work("w1", test_state=None)
        board = self._board(completed=[work])
        items = [_q("w1", size=10)]
        gh = FakeGh()
        events = process(items, gh, config=cfg, board=board, skip_smoke=True)

        kinds = [e.kind for e in events]
        assert "smoke_required" not in kinds
        assert "merged" in kinds
        assert items[0].state == MERGED

    def test_process_smoke_gate_off_when_test_not_in_gates(self) -> None:
        """When 'test' is not in default_gates the smoke gate is disabled."""
        cfg = self._config(gates=["review", "merge"])  # no "test"
        work = self._work("w1", test_state=None)
        board = self._board(completed=[work])
        items = [_q("w1", size=10)]
        gh = FakeGh()
        events = process(items, gh, config=cfg, board=board)

        kinds = [e.kind for e in events]
        assert "smoke_required" not in kinds
        assert "merged" in kinds

    def test_process_legacy_callers_without_config_unaffected(self) -> None:
        """Legacy callers that don't pass config/board still work.

        When config is None, requires_smoke() returns False (no "test" gate
        configured) so no smoke gate fires.  The fail-closed rule (#821) only
        applies when config is present and says smoke is required but board
        is absent.
        """
        items = [_q("w1", size=10)]
        gh = FakeGh()
        events = process(items, gh)
        assert any(e.kind == "merged" for e in events)

    def test_process_smoke_gate_does_not_block_sibling(self) -> None:
        """An unsmoked entry must not halt the group — its sibling with a
        verdict should still merge."""
        cfg = self._config()
        unsmoked = self._work("unsmoked", test_state=None)
        smoked = self._work("smoked", test_state="passed")
        board = self._board(completed=[unsmoked, smoked])
        items = [
            _q("unsmoked", branch="worker/unsmoked", size=10),
            _q("smoked", branch="worker/smoked", size=20),
        ]
        gh = FakeGh()
        events = process(items, gh, config=cfg, board=board)

        kinds = [e.kind for e in events]
        assert "smoke_required" in kinds
        assert "merged" in kinds
        states = {x.assignment_id: x.state for x in items}
        assert states["unsmoked"] == PENDING
        assert states["smoked"] == MERGED

    def test_dry_run_shows_smoke_required_for_no_verdict(self) -> None:
        """dry-run must surface smoke_required, not 'would merge'."""
        cfg = self._config()
        work = self._work("w1", test_state=None)
        board = self._board(completed=[work])
        items = [_q("w1", size=10)]
        gh = FakeGh()
        events = process(items, gh, config=cfg, board=board, dry_run=True)

        kinds = [e.kind for e in events]
        assert "smoke_required" in kinds
        assert "merged" not in kinds
        assert items[0].state == PENDING  # dry-run never mutates state

    def test_dry_run_shows_merged_for_passed_smoke(self) -> None:
        """dry-run with passed smoke verdict → would-merge event."""
        cfg = self._config()
        work = self._work("w1", test_state="passed")
        board = self._board(completed=[work])
        items = [_q("w1", size=10)]
        gh = FakeGh()
        events = process(items, gh, config=cfg, board=board, dry_run=True)

        kinds = [e.kind for e in events]
        assert "merged" in kinds
        assert "smoke_required" not in kinds
        assert items[0].state == PENDING  # dry-run: state untouched

    # ── #1479-review: target_branch_head_sha population ──

    def test_process_populates_target_branch_head_sha_from_gh_ops(self) -> None:
        """#1479: process() must populate entry.target_branch_head_sha via
        gh_ops.get_branch_sha(target_branch) — the production population path
        has_smoke_verdict's base-moved staleness check relies on."""
        sha_calls: list[tuple[str, str]] = []

        class _TrackingGh(FakeGh):
            def get_branch_sha(self, repo: str, branch: str) -> str | None:
                sha_calls.append((repo, branch))
                return "main-sha-123"

        cfg = self._config()
        work = self._work("w1", test_state="passed")
        board = self._board(completed=[work])
        items = [_q("w1", size=10)]
        process(items, _TrackingGh(), config=cfg, board=board)

        assert ("acme/api", "main") in sha_calls, (
            "process() must call gh_ops.get_branch_sha for the target branch"
        )
        assert items[0].target_branch_head_sha == "main-sha-123"

    def test_process_hoists_target_branch_head_sha_fetch_per_group(self) -> None:
        """#1479-review (non-blocking): entries grouped under the same
        (repo_github, target_branch) share an identical target_branch_head_sha
        — process() must fetch it once per group, not once per entry."""
        sha_calls: list[tuple[str, str]] = []

        class _TrackingGh(FakeGh):
            def get_branch_sha(self, repo: str, branch: str) -> str | None:
                sha_calls.append((repo, branch))
                return "main-sha-shared"

        cfg = self._config()
        w1 = self._work("w1", test_state="passed")
        w2 = self._work("w2", test_state="passed")
        board = self._board(completed=[w1, w2])
        items = [
            _q("w1", branch="worker/w1", size=10),
            _q("w2", branch="worker/w2", size=20),
        ]
        process(items, _TrackingGh(), config=cfg, board=board)

        target_calls = [c for c in sha_calls if c == ("acme/api", "main")]
        assert len(target_calls) == 1, (
            "target_branch_head_sha must be fetched once per group, "
            f"got {len(target_calls)} calls: {sha_calls}"
        )
        assert items[0].target_branch_head_sha == "main-sha-shared"
        assert items[1].target_branch_head_sha == "main-sha-shared"


class TestUatGate:
    """#2687/#2948: the pre-merge UAT gate. Two-part opt-in — "uat" must be
    in the effective gate list AND the entry's own repo must have
    ``uat_preview`` (an explicit override template) OR ``uat_live_preview``
    (opt in to the live GitHub-Deployment lookup) configured — so a repo
    that hasn't opted into either is unaffected even if
    ``pipeline.default_gates`` grows "uat" fleet-wide."""

    @staticmethod
    def _config(
        *,
        gates: list[str] | None = None,
        uat_preview: str | None = "https://preview.example/{branch}",
        uat_live_preview: bool = False,
        repo_name: str = "api",
    ):
        """A minimal config-like object with a real Repo behind ``.repo()``."""
        from dataclasses import dataclass, field as dc_field

        from coord.models import Repo

        @dataclass
        class _Pipeline:
            default_gates: list[str] | None = None

        @dataclass
        class _Cfg:
            pipeline: _Pipeline = dc_field(default_factory=_Pipeline)
            _repo: Repo | None = None

            def repo(self, name: str):
                return self._repo if self._repo and self._repo.name == name else None

        cfg = _Cfg()
        # Default gates deliberately isolate the UAT gate — no "review"/"test"
        # in the default list, and no `reviews` attribute on this fake config
        # at all, so requires_review/requires_smoke both no-op and these
        # tests observe the uat gate alone, mirroring TestSmokeGate's own
        # review-disabled-by-default isolation.
        cfg.pipeline.default_gates = gates if gates is not None else ["uat", "merge"]
        cfg._repo = Repo(
            name=repo_name, github="acme/api",
            uat_preview=uat_preview, uat_live_preview=uat_live_preview,
        )
        return cfg

    @staticmethod
    def _board(active=None, completed=None):
        from coord.models import Board
        return Board(active=list(active or []), completed=list(completed or []))

    @staticmethod
    def _work(
        aid: str = "w1", *, uat_state: str | None = None, uat_reason: str | None = None,
        dispatched_at: float | None = None,
    ) -> Assignment:
        return Assignment(
            machine_name="m1",
            repo_name="api",
            issue_number=1,
            issue_title="t",
            assignment_id=aid,
            type="work",
            status="done",
            branch=f"worker/{aid}",
            uat_state=uat_state,
            uat_reason=uat_reason,
            dispatched_at=dispatched_at,
        )

    # ── requires_uat ──

    def test_requires_uat_true_when_gate_and_repo_both_opt_in(self) -> None:
        cfg = self._config(gates=["review", "uat", "merge"])
        assert mq.requires_uat(_q("a"), cfg) is True

    def test_requires_uat_false_when_gate_absent(self) -> None:
        cfg = self._config(gates=["review", "merge"])
        assert mq.requires_uat(_q("a"), cfg) is False

    def test_requires_uat_false_when_repo_has_no_uat_preview(self) -> None:
        # The fleet-wide gate list opts in, but THIS repo hasn't — the
        # "ship the mechanism with the default off everywhere" contract.
        cfg = self._config(gates=["review", "uat", "merge"], uat_preview=None)
        assert mq.requires_uat(_q("a"), cfg) is False

    def test_requires_uat_false_for_a_different_repo(self) -> None:
        cfg = self._config(gates=["review", "uat", "merge"], repo_name="other-repo")
        assert mq.requires_uat(_q("a", repo="api"), cfg) is False

    def test_requires_uat_false_when_no_pipeline(self) -> None:
        from dataclasses import dataclass
        @dataclass
        class _NoPipelineCfg:
            pass
        assert mq.requires_uat(_q("a"), _NoPipelineCfg()) is False

    def test_requires_uat_entry_override_bypasses_default(self) -> None:
        # #1213-style per-issue override: an entry whose snapshotted
        # required_gates drops "uat" bypasses the gate even though the
        # default policy requires it.
        cfg = self._config(gates=["uat", "merge"])
        entry = _q("a", required_gates=["merge"])
        assert mq.requires_uat(entry, cfg) is False

    def test_requires_uat_empty_entry_gates_falls_back_to_default(self) -> None:
        cfg = self._config(gates=["uat", "merge"])
        assert mq.requires_uat(_q("a", required_gates=[]), cfg) is True

    def test_requires_uat_true_via_live_preview_alone(self) -> None:
        # #2948: `uat_live_preview` is a full per-repo opt-in on its own —
        # a repo needs no `uat_preview` template at all to turn the gate on.
        cfg = self._config(
            gates=["review", "uat", "merge"], uat_preview=None, uat_live_preview=True,
        )
        assert mq.requires_uat(_q("a"), cfg) is True

    def test_requires_uat_false_when_neither_uat_option_set(self) -> None:
        cfg = self._config(
            gates=["review", "uat", "merge"], uat_preview=None, uat_live_preview=False,
        )
        assert mq.requires_uat(_q("a"), cfg) is False

    # ── evaluate_uat_verdict ──

    def test_evaluate_uat_verdict_missing_names_preview_and_command(self) -> None:
        cfg = self._config()
        work = self._work("w1", uat_state=None)
        board = self._board(completed=[work])
        ok, message = mq.evaluate_uat_verdict(_q("w1"), board, cfg)
        assert ok is False
        assert "uat verdict missing" in message
        assert "preview: https://preview.example/worker/w1" in message
        assert "coord uat w1 --passed|--failed" in message

    def test_evaluate_uat_verdict_passed(self) -> None:
        cfg = self._config()
        work = self._work("w1", uat_state="passed")
        board = self._board(completed=[work])
        ok, message = mq.evaluate_uat_verdict(_q("w1"), board, cfg)
        assert ok is True
        assert message == ""

    def test_evaluate_uat_verdict_failed_carries_reason(self) -> None:
        cfg = self._config()
        work = self._work("w1", uat_state="failed", uat_reason="logo is cropped")
        board = self._board(completed=[work])
        ok, message = mq.evaluate_uat_verdict(_q("w1"), board, cfg)
        assert ok is False
        assert "uat verdict FAILED: logo is cropped" in message

    def test_evaluate_uat_verdict_fails_closed_with_no_work_assignment(self) -> None:
        # Unlike evaluate_smoke_verdict (fails open), an unidentifiable work
        # chain must NOT pass a gate that exists to force a human decision.
        cfg = self._config()
        board = self._board()
        ok, _ = mq.evaluate_uat_verdict(_q("w1"), board, cfg)
        assert ok is False

    def test_evaluate_uat_verdict_prefers_most_recently_dispatched_verdict(self) -> None:
        # A bounce/fix round's fresh work assignment is a NEW thing to look
        # at — an older sibling's stale "passed" must not paper over it.
        cfg = self._config()
        old = self._work("w1", uat_state="passed", dispatched_at=1.0)
        new = self._work("w2", uat_state="failed", uat_reason="regressed", dispatched_at=2.0)
        # Both must chain to the same entry via review_of_assignment_id or
        # branch — simplest: give them the SAME assignment_id chain by
        # reusing _chain_work_ids' branch-match path (same branch as entry).
        new.branch = "worker/w1"
        board = self._board(completed=[old, new])
        ok, message = mq.evaluate_uat_verdict(_q("w1"), board, cfg)
        assert ok is False
        assert "regressed" in message

    # ── evaluate_uat_verdict: live preview lookup (#2948) ──

    def test_evaluate_uat_verdict_resolves_via_live_deployment_lookup(self) -> None:
        # No `uat_preview` override — relies entirely on `uat_live_preview`
        # and a live `gh_ops.get_pr_deployment_url` lookup.
        cfg = self._config(uat_preview=None, uat_live_preview=True)
        work = self._work("w1", uat_state=None)
        board = self._board(completed=[work])
        gh = FakeGh(deployment_urls={"worker/w1": "https://abc123.example.pages.dev"})
        ok, message = mq.evaluate_uat_verdict(_q("w1"), board, cfg, gh)
        assert ok is False
        assert "preview: https://abc123.example.pages.dev" in message
        assert ("acme/api", "worker/w1") in gh.deployment_url_calls

    def test_evaluate_uat_verdict_override_template_wins_over_live_lookup(self) -> None:
        # An explicit `uat_preview` override always wins, even when the repo
        # ALSO has `uat_live_preview` set — the live lookup is never even
        # attempted.
        cfg = self._config(
            uat_preview="https://preview.example/{branch}", uat_live_preview=True,
        )
        work = self._work("w1", uat_state=None)
        board = self._board(completed=[work])
        gh = FakeGh(deployment_urls={"worker/w1": "https://abc123.example.pages.dev"})
        ok, message = mq.evaluate_uat_verdict(_q("w1"), board, cfg, gh)
        assert ok is False
        assert "preview: https://preview.example/worker/w1" in message
        assert "abc123" not in message
        assert gh.deployment_url_calls == []

    def test_evaluate_uat_verdict_unresolved_preview_says_so(self) -> None:
        # #2948 acceptance bar: `uat_live_preview` is set but the live lookup
        # finds nothing for this branch — the message must say the URL is
        # unresolved, never fall back to a constructed guess.
        cfg = self._config(uat_preview=None, uat_live_preview=True)
        work = self._work("w1", uat_state=None)
        board = self._board(completed=[work])
        ok, message = mq.evaluate_uat_verdict(_q("w1"), board, cfg, FakeGh())
        assert ok is False
        assert "preview:" not in message
        assert "could not be resolved" in message

    def test_evaluate_uat_verdict_no_gh_ops_says_unresolved(self) -> None:
        # A caller that doesn't pass gh_ops at all (the default, e.g.
        # display_error's deliberately I/O-free recompute) never attempts
        # the live lookup — same "unresolved" wording, not a crash.
        cfg = self._config(uat_preview=None, uat_live_preview=True)
        work = self._work("w1", uat_state=None)
        board = self._board(completed=[work])
        ok, message = mq.evaluate_uat_verdict(_q("w1"), board, cfg)
        assert ok is False
        assert "could not be resolved" in message

    # ── merge_gate_failures / passes_merge_gates ──

    def test_merge_gate_failures_includes_uat(self) -> None:
        cfg = self._config()
        work = self._work("w1", uat_state=None)
        board = self._board(completed=[work])
        failures = mq.merge_gate_failures(_q("w1"), cfg, board)
        assert any(f.gate == "uat" for f in failures)
        uat_failure = next(f for f in failures if f.gate == "uat")
        assert "coord uat" in uat_failure.waiver_flag

    def test_passes_merge_gates_false_when_uat_missing(self) -> None:
        cfg = self._config()
        work = self._work("w1", uat_state=None)
        board = self._board(completed=[work])
        assert mq.passes_merge_gates(_q("w1"), cfg, board) is False

    def test_passes_merge_gates_true_when_uat_passed(self) -> None:
        cfg = self._config()
        work = self._work("w1", uat_state="passed")
        board = self._board(completed=[work])
        assert mq.passes_merge_gates(_q("w1"), cfg, board) is True

    def test_passes_merge_gates_true_when_repo_not_opted_in(self) -> None:
        cfg = self._config(uat_preview=None)
        work = self._work("w1", uat_state=None)
        board = self._board(completed=[work])
        assert mq.passes_merge_gates(_q("w1"), cfg, board) is True

    # ── live process() wiring ──

    def test_process_emits_uat_required_event_and_halts_merge(self) -> None:
        cfg = self._config()
        board = self._board(completed=[self._work("w1")])
        items = [_q("w1", size=10)]
        gh = FakeGh()
        events = process(items, gh, config=cfg, board=board)

        kinds = [e.kind for e in events]
        assert "opened" in kinds
        assert "uat_required" in kinds
        assert "merged" not in kinds
        assert gh.merge_calls == []
        assert items[0].state == PENDING
        assert items[0].error is not None
        assert items[0].error.startswith("uat verdict missing")

    def test_process_uat_required_event_carries_live_preview_url(self) -> None:
        # #2948: the live GhOps.get_pr_deployment_url lookup is threaded all
        # the way through process()'s live gate evaluation, not just through
        # evaluate_uat_verdict called directly.
        cfg = self._config(uat_preview=None, uat_live_preview=True)
        board = self._board(completed=[self._work("w1")])
        items = [_q("w1", size=10)]
        gh = FakeGh(deployment_urls={"worker/w1": "https://abc123.example.pages.dev"})
        events = process(items, gh, config=cfg, board=board)

        uat_events = [e for e in events if e.kind == "uat_required"]
        assert len(uat_events) == 1
        assert "https://abc123.example.pages.dev" in uat_events[0].message

    def test_process_proceeds_when_uat_passed(self) -> None:
        cfg = self._config()
        board = self._board(completed=[self._work("w1", uat_state="passed")])
        items = [_q("w1", size=10)]
        gh = FakeGh()
        events = process(items, gh, config=cfg, board=board)

        assert any(e.kind == "merged" for e in events)
        assert items[0].state == MERGED

    def test_process_skip_uat_bypasses_the_gate(self) -> None:
        cfg = self._config()
        board = self._board(completed=[self._work("w1", uat_state=None)])
        items = [_q("w1", size=10)]
        gh = FakeGh()
        events = process(items, gh, config=cfg, board=board, skip_uat=True)

        assert any(e.kind == "merged" for e in events)
        assert not any(e.kind == "uat_required" for e in events)

    def test_dry_run_previews_uat_block(self) -> None:
        cfg = self._config()
        board = self._board(completed=[self._work("w1")])
        items = [_q("w1", size=10)]
        events = process(items, FakeGh(), config=cfg, board=board, dry_run=True)

        blocked = [e for e in events if e.kind == "uat_required"]
        assert len(blocked) == 1
        assert "(dry run)" in blocked[0].message
        assert "uat verdict missing" in blocked[0].message

    # ── _bypassed_gates / gate-bypass audit ──

    def test_bypassed_gates_reports_uat_when_configured_and_dropped(self) -> None:
        cfg = self._config(gates=["review", "uat", "merge"])
        cfg.reviews = type("_R", (), {"enabled": False})()  # keep review out of it
        entry = _q("a", required_gates=["merge"])
        assert "uat" in mq._bypassed_gates(entry, cfg)

    def test_bypassed_gates_omits_uat_when_repo_not_opted_in(self) -> None:
        cfg = self._config(gates=["review", "uat", "merge"], uat_preview=None)
        cfg.reviews = type("_R", (), {"enabled": False})()
        entry = _q("a", required_gates=["merge"])
        assert "uat" not in mq._bypassed_gates(entry, cfg)

    # ── display_error recompute (#420) ──

    def test_display_error_clears_once_uat_verdict_recorded(self) -> None:
        cfg = self._config()
        entry = _q("w1", size=10)
        entry.error = "uat verdict missing — preview: https://x.example.pages.dev/ — run: coord uat w1 --passed|--failed"
        board = self._board(completed=[self._work("w1", uat_state="passed")])
        assert mq.display_error(entry, board, cfg) is None

    def test_display_error_recomputes_uat_failure_message(self) -> None:
        cfg = self._config()
        entry = _q("w1", size=10)
        entry.error = "uat verdict missing — run: coord uat w1 --passed|--failed"
        board = self._board(completed=[self._work("w1", uat_state="failed", uat_reason="broken layout")])
        live = mq.display_error(entry, board, cfg)
        assert live is not None
        assert "broken layout" in live

    def test_display_error_returns_none_when_gate_no_longer_required(self) -> None:
        # The repo dropped uat_preview after the error was stored — the
        # stale block must clear, not keep quoting a gate that no longer
        # applies.
        cfg = self._config(uat_preview=None)
        entry = _q("w1", size=10)
        entry.error = "uat verdict missing — run: coord uat w1 --passed|--failed"
        board = self._board(completed=[self._work("w1", uat_state=None)])
        assert mq.display_error(entry, board, cfg) is None


class TestGateBypassAudit:
    """#1213: a per-issue label override honoured by requires_review /
    requires_smoke merges without the bypassed gate(s), and every bypass
    writes a ``gate_bypassed`` business-tier audit row + a CLI-visible note
    on the "merged" event — never silent."""

    @staticmethod
    def _config(*, default_gates=None, labels=None, reviews_enabled=True):
        from dataclasses import dataclass, field as dc_field
        @dataclass
        class _Reviews:
            enabled: bool = True
        @dataclass
        class _Pipeline:
            default_gates: list[str] | None = None
            labels: dict = dc_field(default_factory=dict)
        @dataclass
        class _Cfg:
            reviews: _Reviews = dc_field(default_factory=_Reviews)
            pipeline: _Pipeline = dc_field(default_factory=_Pipeline)
        cfg = _Cfg()
        cfg.reviews.enabled = reviews_enabled
        cfg.pipeline.default_gates = (
            default_gates if default_gates is not None else ["test", "review", "merge"]
        )
        cfg.pipeline.labels = labels or {}
        return cfg

    @staticmethod
    def _board(completed=None, active=None):
        from coord.models import Board
        return Board(active=list(active or []), completed=list(completed or []))

    @staticmethod
    def _audit_rows(coord_db, event_type: str = "gate_bypassed") -> list:
        return coord_db.execute(
            "SELECT * FROM audit_log WHERE event_type = ?", (event_type,)
        ).fetchall()

    def test_merge_only_label_bypasses_review_and_smoke(self, coord_db) -> None:
        cfg = self._config(labels={"gate:trivial": ["merge"]})
        board = self._board()  # no review, no smoke verdict anywhere
        items = [_q("a", required_gates=["merge"])]
        events = process(items, FakeGh(), config=cfg, board=board)

        assert items[0].state == MERGED
        merged = [e for e in events if e.kind == "merged"]
        assert merged
        assert "gate bypass" in merged[0].message
        assert "gate:trivial" in merged[0].message

        rows = self._audit_rows(coord_db)
        assert len(rows) == 1
        assert rows[0]["tier"] == "business"
        assert rows[0]["category"] == "gate"
        assert rows[0]["actor"] == "user"
        details = json.loads(rows[0]["details_json"])
        assert details["label"] == "gate:trivial"
        assert sorted(details["bypassed_gates"]) == ["review", "test"]
        assert details["resolved_gates"] == ["merge"]

    def test_reviews_globally_disabled_does_not_report_phantom_review_bypass(
        self, coord_db
    ) -> None:
        # Review finding #1: when config.reviews.enabled is False, review was
        # never going to be required regardless of the label — requires_review
        # already returns False unconditionally. A ["merge"]-only label drops
        # "review" from the resolved gate list too, but that changes nothing
        # about enforcement, so it must NOT be reported as a bypassed gate
        # (only "test" is a real bypass here).
        cfg = self._config(labels={"gate:trivial": ["merge"]}, reviews_enabled=False)
        board = self._board()  # no review, no smoke verdict anywhere
        items = [_q("a", required_gates=["merge"])]
        events = process(items, FakeGh(), config=cfg, board=board)

        assert items[0].state == MERGED
        merged = [e for e in events if e.kind == "merged"]
        assert merged
        assert "gate bypass" in merged[0].message
        assert "test" in merged[0].message
        assert "review" not in merged[0].message

        rows = self._audit_rows(coord_db)
        assert len(rows) == 1
        details = json.loads(rows[0]["details_json"])
        assert details["bypassed_gates"] == ["test"]
        assert "review" not in details["bypassed_gates"]

    def test_untagged_work_is_completely_unaffected(self, coord_db) -> None:
        # #1213 acceptance: the important regression test — untagged work
        # (no per-issue override) must still be gated exactly as before.
        cfg = self._config()
        board = self._board()  # no review, no smoke verdict
        items = [_q("a", required_gates=[])]
        events = process(items, FakeGh(), config=cfg, board=board)

        assert items[0].state == PENDING
        kinds = [e.kind for e in events]
        assert "review_required" in kinds
        assert "merged" not in kinds
        assert self._audit_rows(coord_db) == []

    def test_label_resolving_to_test_and_merge_still_requires_test(self, coord_db) -> None:
        # An issue whose label resolves to ["test", "merge"] still requires
        # a Test verdict, just not a review.  Board carries the matching work
        # assignment with no verdict yet, so the smoke gate fails closed
        # (has_smoke_verdict only fails *open* when no matching branch work
        # is found on the board at all).
        cfg = self._config(labels={"needs-test": ["test", "merge"]})
        work = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="a", type="work", status="done", branch="worker/a",
            test_state=None,
        )
        board = self._board(completed=[work])
        items = [_q("a", required_gates=["test", "merge"])]
        events = process(items, FakeGh(), config=cfg, board=board)

        assert items[0].state == PENDING
        kinds = [e.kind for e in events]
        assert "smoke_required" in kinds
        assert "review_required" not in kinds
        assert self._audit_rows(coord_db) == []

    def test_label_resolving_to_test_and_merge_merges_once_tested(self, coord_db) -> None:
        cfg = self._config(labels={"needs-test": ["test", "merge"]})
        work = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="a", type="work", status="done", branch="worker/a",
            test_state="passed",
        )
        board = self._board(completed=[work])
        items = [_q("a", required_gates=["test", "merge"])]
        events = process(items, FakeGh(), config=cfg, board=board)

        assert items[0].state == MERGED
        merged = [e for e in events if e.kind == "merged"]
        assert merged and "review" in merged[0].message

        rows = self._audit_rows(coord_db)
        assert len(rows) == 1
        details = json.loads(rows[0]["details_json"])
        assert details["bypassed_gates"] == ["review"]

    def test_no_audit_row_when_resolved_gates_match_default(self, coord_db) -> None:
        # An entry carrying required_gates that happens to equal the default
        # policy isn't a real bypass — no phantom audit row.
        cfg = self._config(default_gates=["merge"])
        board = self._board()
        items = [_q("a", required_gates=["merge"])]
        events = process(items, FakeGh(), config=cfg, board=board)

        assert items[0].state == MERGED
        merged = [e for e in events if e.kind == "merged"]
        assert merged and "gate bypass" not in merged[0].message
        assert self._audit_rows(coord_db) == []

    def test_dry_run_shows_bypass_note_but_writes_no_audit(self, coord_db) -> None:
        # #1213: "coord merge output names any bypassed gate" applies to the
        # dry-run preview too, but a preview must never write an audit row.
        cfg = self._config(labels={"gate:trivial": ["merge"]})
        board = self._board()
        items = [_q("a", required_gates=["merge"])]
        events = process(items, FakeGh(), config=cfg, board=board, dry_run=True)

        merged = [e for e in events if e.kind == "merged"]
        assert merged and "gate bypass" in merged[0].message
        assert self._audit_rows(coord_db) == []


class TestGroupBranchCandidates:
    """#1490: a fix/bounce cycle piles up more than one WORK_LIKE_TYPES row
    on the same branch (the original dispatch + every retry) —
    group_branch_candidates resolves each branch to a single winner instead
    of every caller processing (and re-announcing) every row."""

    @staticmethod
    def _work(
        aid: str,
        *,
        branch: str = "issue-1-fix",
        test_state: str | None = None,
        dispatched_at: float | None = None,
        repo: str = "api",
        status: str = "done",
        atype: str = "work",
    ) -> Assignment:
        return Assignment(
            machine_name="m1", repo_name=repo, issue_number=1, issue_title="t",
            assignment_id=aid, type=atype, status=status, branch=branch,
            test_state=test_state, dispatched_at=dispatched_at,
        )

    def test_single_row_is_its_own_winner(self) -> None:
        a = self._work("a1")
        result = mq.group_branch_candidates([a])
        assert result == [(a, [])]

    def test_three_rows_one_branch_resolve_to_latest_passed(self) -> None:
        """The #1445 scenario verbatim: one failed test_state, two passed —
        the latest-dispatched *passed* row wins; the other two are
        superseded."""
        failed = self._work("31bd30875eb3", test_state="failed", dispatched_at=1000)
        passed1 = self._work("12fced1dfa80", test_state="passed", dispatched_at=2000)
        passed2 = self._work("5ed99d1f7edf", test_state="passed", dispatched_at=3000)
        result = mq.group_branch_candidates([failed, passed1, passed2])

        assert len(result) == 1
        winner, superseded = result[0]
        assert winner is passed2
        assert {id(x) for x in superseded} == {id(failed), id(passed1)}

    def test_falls_back_to_latest_overall_when_none_passed(self) -> None:
        """The branch is still mid-cycle (nothing has passed yet) — it must
        still resolve to a single winner (the most recent row) rather than
        disappearing entirely."""
        a1 = self._work("a1", dispatched_at=1000)
        a2 = self._work("a2", dispatched_at=2000)
        winner, superseded = mq.group_branch_candidates([a1, a2])[0]
        assert winner is a2
        assert superseded == [a1]

    def test_distinct_branches_are_separate_groups(self) -> None:
        a1 = self._work("a1", branch="issue-1-fix")
        a2 = self._work("a2", branch="issue-2-fix")
        result = mq.group_branch_candidates([a1, a2])
        assert len(result) == 2
        assert {w.assignment_id for w, _ in result} == {"a1", "a2"}
        assert all(superseded == [] for _, superseded in result)

    def test_filters_non_work_like_and_incomplete_rows(self) -> None:
        review = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="r1", type="review", status="done", branch="issue-1-fix",
        )
        not_done = self._work("nd", status="running")
        no_branch = self._work("nb", branch=None)
        no_aid = self._work("", branch="issue-1-fix")
        result = mq.group_branch_candidates([review, not_done, no_branch, no_aid])
        assert result == []

    def test_mock_author_and_test_author_are_grouped_too(self) -> None:
        """#930/#1141: WORK_LIKE_TYPES is 'work', 'mock-author', 'test-author'
        — all three flow through the same auto-enqueue path and must be
        grouped the same way."""
        ma = self._work("ma1", atype="mock-author", branch="ms-5-gate-a")
        ta = self._work("ta1", atype="test-author", branch="ms-37-test-author")
        result = mq.group_branch_candidates([ma, ta])
        assert len(result) == 2

    def test_order_is_stable_first_seen(self) -> None:
        a1 = self._work("a1", branch="issue-1-fix")
        b1 = self._work("b1", branch="issue-2-fix")
        a2 = self._work("a2", branch="issue-1-fix")
        result = mq.group_branch_candidates([a1, b1, a2])
        assert [w.branch for w, _ in result] == ["issue-1-fix", "issue-2-fix"]


class TestRefreshEntryAssignment:
    """#292: refresh_entry_assignment creates or updates queue entries."""

    def _work(self, aid: str, branch: str = "worker/orig") -> Assignment:
        return Assignment(
            machine_name="m1",
            repo_name="api",
            issue_number=1,
            issue_title="t",
            assignment_id=aid,
            type="work",
            status="done",
            branch=branch,
        )

    def test_creates_entry_when_none_exists(self, coord_db) -> None:
        work = self._work("fix1")
        result = mq.refresh_entry_assignment(work, repo_github="acme/api", target_branch="main")
        assert result is True
        items = load_queue()
        assert len(items) == 1
        assert items[0].assignment_id == "fix1"

    def test_updates_assignment_id_for_existing_pending_entry(self, coord_db) -> None:
        # Seed with orig-work keyed entry
        orig = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="orig", branch="worker/orig", status="done",
        )
        mq.enqueue(orig, repo_github="acme/api", target_branch="main")
        assert load_queue()[0].assignment_id == "orig"

        fix = self._work("fix1", branch="worker/orig")
        result = mq.refresh_entry_assignment(fix, repo_github="acme/api", target_branch="main")
        assert result is True
        items = load_queue()
        assert len(items) == 1
        assert items[0].assignment_id == "fix1"

    def test_preserves_assignment_type_across_review_bounce(self, coord_db) -> None:
        # #1077 (review round 1): a mock-author entry's assignment_type must
        # survive a review bounce. auto_loop._dispatch_fix_for_review
        # unconditionally dispatches fix workers with type="work" regardless
        # of the original assignment's type, and that fix assignment is what
        # reaches refresh_entry_assignment once its own re-review approves
        # (via _advance_pipeline). If assignment_type were re-keyed from the
        # fix assignment here, every ordinary request-changes round trip on a
        # Gate A mock-author PR would flip the entry back to "work" and
        # re-enable close-on-merge -- reproducing the original #1077 bug.
        orig = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="orig", type="mock-author", branch="worker/orig",
            status="done",
        )
        mq.enqueue(orig, repo_github="acme/api", target_branch="main")
        assert load_queue()[0].assignment_type == "mock-author"

        # Simulate the bounce: fix worker is dispatched with type="work"
        # hardcoded, same branch as the original.
        fix = self._work("fix1", branch="worker/orig")
        assert fix.type == "work"
        result = mq.refresh_entry_assignment(fix, repo_github="acme/api", target_branch="main")
        assert result is True
        items = load_queue()
        assert items[0].assignment_id == "fix1"  # assignment_id does re-key
        assert items[0].assignment_type == "mock-author"  # type does NOT

    def test_clears_stale_review_error(self, coord_db) -> None:
        orig = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="orig", branch="worker/orig", status="done",
        )
        mq.enqueue(orig, repo_github="acme/api", target_branch="main")
        items = load_queue()
        items[0].error = "review required but not approved"
        mq.save_queue(items)

        fix = self._work("fix1", branch="worker/orig")
        mq.refresh_entry_assignment(fix, repo_github="acme/api", target_branch="main")
        assert load_queue()[0].error is None

    def test_no_change_when_assignment_id_already_correct(self, coord_db) -> None:
        work = self._work("fix1")
        mq.enqueue(work, repo_github="acme/api", target_branch="main")
        result = mq.refresh_entry_assignment(work, repo_github="acme/api", target_branch="main")
        assert result is False  # no change

    def test_does_not_touch_merged_entry(self, coord_db) -> None:
        orig = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="orig", branch="worker/orig", status="done",
        )
        mq.enqueue(orig, repo_github="acme/api", target_branch="main")
        items = load_queue()
        items[0].state = mq.MERGED
        mq.save_queue(items)

        fix = self._work("fix1", branch="worker/orig")
        result = mq.refresh_entry_assignment(fix, repo_github="acme/api", target_branch="main")
        assert result is False
        assert load_queue()[0].assignment_id == "orig"  # untouched

    def test_noop_when_no_branch(self, coord_db) -> None:
        work = self._work("fix1", branch="")
        work.branch = None  # type: ignore[assignment]
        result = mq.refresh_entry_assignment(work, repo_github="acme/api", target_branch="main")
        assert result is False
        assert load_queue() == []


class TestReconcileConflictEntries:
    """#1477: a CONFLICT entry re-tests its cached verdict on every tick
    instead of trusting the `gh pr merge` failure recorded whenever the
    queue last attempted it."""

    def test_clears_conflict_when_pr_now_mergeable(self, coord_db) -> None:
        entry = _q("a", state=CONFLICT, pr=100)
        entry.error = "gh pr merge 1464 ... --rebase failed: X Pull request #1464 is not mergeable"
        save_queue([entry])

        gh = FakeGh(mergeable_results={100: True})
        events = mq.reconcile_conflict_entries(gh)

        assert len(events) == 1
        assert events[0].kind == "reopened"
        reloaded = load_queue()[0]
        assert reloaded.state == PENDING
        assert reloaded.error is None
        assert gh.mergeable_calls == [("acme/api", 100)]

    def test_leaves_entry_parked_when_still_conflicting(self, coord_db) -> None:
        entry = _q("a", state=CONFLICT, pr=100)
        entry.error = "not mergeable"
        save_queue([entry])

        gh = FakeGh(mergeable_results={100: False})
        events = mq.reconcile_conflict_entries(gh)

        assert events == []
        reloaded = load_queue()[0]
        assert reloaded.state == CONFLICT
        assert reloaded.error == "not mergeable"

    def test_leaves_entry_parked_when_mergeability_unknown(self, coord_db) -> None:
        """Fail-closed: `None` (gh error / GitHub still computing) must never
        be treated as a green light to unpark an entry."""
        entry = _q("a", state=CONFLICT, pr=100)
        save_queue([entry])

        gh = FakeGh()  # mergeable_results defaults to {} -> None
        events = mq.reconcile_conflict_entries(gh)

        assert events == []
        assert load_queue()[0].state == CONFLICT

    def test_skips_entry_with_no_pr_number(self, coord_db) -> None:
        entry = _q("a", state=CONFLICT, pr=None)
        save_queue([entry])

        gh = FakeGh(mergeable_results={100: True})
        events = mq.reconcile_conflict_entries(gh)

        assert events == []
        assert gh.mergeable_calls == []
        assert load_queue()[0].state == CONFLICT

    def test_only_touches_conflict_entries(self, coord_db) -> None:
        """PENDING/MERGED/HUMAN_REQUIRED entries are never re-tested."""
        pending = _q("p", state=PENDING, pr=200)
        merged = _q("m", state=MERGED, pr=201)
        human = _q("h", state=mq.HUMAN_REQUIRED, pr=202)
        save_queue([pending, merged, human])

        gh = FakeGh(mergeable_results={200: True, 201: True, 202: True})
        events = mq.reconcile_conflict_entries(gh)

        assert events == []
        assert gh.mergeable_calls == []
        states = {x.assignment_id: x.state for x in load_queue()}
        assert states == {"p": PENDING, "m": MERGED, "h": mq.HUMAN_REQUIRED}

    def test_gh_exception_does_not_wedge_the_tick(self, coord_db) -> None:
        entry = _q("a", state=CONFLICT, pr=100)
        save_queue([entry])

        class RaisingGh(FakeGh):
            def check_pr_mergeable(self, repo: str, number: int) -> bool | None:
                raise RuntimeError("gh timeout")

        events = mq.reconcile_conflict_entries(RaisingGh())
        assert events == []
        assert load_queue()[0].state == CONFLICT

    def test_multiple_conflict_entries_reconciled_independently(self, coord_db) -> None:
        clean = _q("clean", state=CONFLICT, pr=100)
        still_broken = _q("broken", state=CONFLICT, pr=101)
        save_queue([clean, still_broken])

        gh = FakeGh(mergeable_results={100: True, 101: False})
        events = mq.reconcile_conflict_entries(gh)

        assert [e.entry.assignment_id for e in events] == ["clean"]
        states = {x.assignment_id: x.state for x in load_queue()}
        assert states == {"clean": PENDING, "broken": CONFLICT}


class TestReconcileConflictEntriesRebaseRefusalGuard:
    """#1467: a `mergeable: MERGEABLE` verdict is not evidence that a
    *rebase* merge will succeed — GitHub reports a branch carrying a merge
    commit as MERGEABLE right up until `gh pr merge --rebase` refuses it.
    An entry parked on that specific refusal must not unpark on the
    mergeable check alone; it also needs confirmation the branch has
    actually gone linear."""

    _REBASE_REFUSAL = "GraphQL: This branch can't be rebased (mergePullRequest)"

    def test_stays_parked_while_merge_commit_persists(self, coord_db) -> None:
        entry = _q("a", state=CONFLICT, pr=100)
        entry.error = self._REBASE_REFUSAL
        save_queue([entry])

        gh = FakeGh(mergeable_results={100: True}, merge_commit_results={100: True})
        events = mq.reconcile_conflict_entries(gh)

        assert events == []
        assert load_queue()[0].state == CONFLICT
        assert gh.merge_commit_calls == [("acme/api", 100)]

    def test_stays_parked_when_merge_commit_probe_is_inconclusive(self, coord_db) -> None:
        entry = _q("a", state=CONFLICT, pr=100)
        entry.error = self._REBASE_REFUSAL
        save_queue([entry])

        # merge_commit_results defaults to {} -> None (fail-closed).
        gh = FakeGh(mergeable_results={100: True})
        events = mq.reconcile_conflict_entries(gh)

        assert events == []
        assert load_queue()[0].state == CONFLICT

    def test_stays_parked_when_gh_ops_lacks_the_probe(self, coord_db) -> None:
        entry = _q("a", state=CONFLICT, pr=100)
        entry.error = self._REBASE_REFUSAL
        save_queue([entry])

        class NoProbeGh(FakeGh):
            branch_has_merge_commit = None  # simulate a pre-#1467 stub

        gh = NoProbeGh(mergeable_results={100: True})
        events = mq.reconcile_conflict_entries(gh)

        assert events == []
        assert load_queue()[0].state == CONFLICT

    def test_unparks_once_branch_is_confirmed_linear(self, coord_db) -> None:
        entry = _q("a", state=CONFLICT, pr=100)
        entry.error = self._REBASE_REFUSAL
        save_queue([entry])

        gh = FakeGh(mergeable_results={100: True}, merge_commit_results={100: False})
        events = mq.reconcile_conflict_entries(gh)

        assert [e.kind for e in events] == ["reopened"]
        reloaded = load_queue()[0]
        assert reloaded.state == PENDING
        assert reloaded.error is None

    def test_plain_conflict_unaffected_never_probes_merge_commit(self, coord_db) -> None:
        # A content conflict (not a rebase refusal) keeps the pre-#1467
        # mergeable-only behaviour untouched — the extra probe never fires.
        entry = _q("a", state=CONFLICT, pr=100)
        entry.error = "Pull request #100 is not mergeable"
        save_queue([entry])

        gh = FakeGh(mergeable_results={100: True})
        events = mq.reconcile_conflict_entries(gh)

        assert [e.kind for e in events] == ["reopened"]
        assert gh.merge_commit_calls == []
        assert load_queue()[0].state == PENDING


class TestReconcileOscillationRegression:
    """#1467 regression: an entry parked on a rebase-refusal whose PR
    reports MERGEABLE must not endlessly unpark -> retry -> re-park across
    ticks. Drives reconcile across multiple passes and (separately)
    verifies the terminal, merged outcome once the branch actually becomes
    linear."""

    _REBASE_REFUSAL = "GraphQL: This branch can't be rebased (mergePullRequest)"

    def test_does_not_oscillate_across_repeated_ticks(self, coord_db) -> None:
        entry = _q("a", state=CONFLICT, pr=100)
        entry.error = self._REBASE_REFUSAL
        save_queue([entry])

        # Worst case for the old behaviour: GitHub always reports
        # MERGEABLE (true of a merge-commit branch) and the merge commit
        # never resolves (e.g. no conflict-fix worker landed yet).
        gh = FakeGh(mergeable_results={100: True}, merge_commit_results={100: True})

        for tick in range(3):
            events = mq.reconcile_conflict_entries(gh)
            assert events == [], f"tick {tick}: entry unparked despite unresolved merge commit"
            assert load_queue()[0].state == CONFLICT

    def test_reaches_terminal_merged_state_once_branch_goes_linear(self, coord_db) -> None:
        entry = _q("a", state=CONFLICT, pr=100, size=10)
        entry.error = self._REBASE_REFUSAL
        save_queue([entry])

        gh = FakeGh(mergeable_results={100: True}, merge_commit_results={100: True})

        # Pass 1: still has a merge commit -> stays parked, no wasted merge
        # attempt or misleading "conflict cleared" event.
        assert mq.reconcile_conflict_entries(gh) == []
        assert load_queue()[0].state == CONFLICT

        # A conflict-fix worker (or a human) rebases the branch onto main —
        # the merge commit is gone.
        gh.merge_commit_results[100] = False

        # Pass 2: now unparks...
        events = mq.reconcile_conflict_entries(gh)
        assert [e.kind for e in events] == ["reopened"]
        items = load_queue()
        assert items[0].state == PENDING

        # ...and the next merge pass succeeds cleanly via --rebase — a
        # terminal state, not another park.
        merge_events = mq.process(items, gh, method="rebase")
        assert items[0].state == MERGED
        assert not [e for e in merge_events if e.kind == "conflict"]


class TestResolveEntryKey:
    """#1477: --only/--drop accept a durable 'repo#issue' key in addition to
    a raw assignment_id, since the id mints fresh across a drop + re-enqueue
    cycle and can silently stop matching what an operator last saw."""

    def test_resolves_exact_assignment_id(self, coord_db) -> None:
        items = [_q("aid1"), _q("aid2")]
        assert mq.resolve_entry_key(items, "aid2") is items[1]

    def test_resolves_durable_repo_issue_key(self, coord_db) -> None:
        entry = QueuedMerge(
            assignment_id="aee6301971bf", repo_name="api", repo_github="acme/api",
            branch="issue-1461-fix", target_branch="main", issue_number=1461,
            issue_title="t", state=PENDING,
        )
        items = [entry]
        assert mq.resolve_entry_key(items, "api#1461") is entry
        assert mq.resolve_entry_key(items, "acme/api#1461") is entry

    def test_survives_drop_and_reenqueue_with_new_assignment_id(self, coord_db) -> None:
        """The exact bug in #1477: the id changes across drop + re-enqueue,
        but the durable key still finds the (new) row."""
        original = QueuedMerge(
            assignment_id="292740800331", repo_name="api", repo_github="acme/api",
            branch="issue-1461-fix", target_branch="main", issue_number=1461,
            issue_title="t", state=CONFLICT,
        )
        save_queue([original])
        assert mq.drop_entry("api#1461") is True
        assert load_queue() == []

        # Re-enqueue mints a fresh assignment id for the same branch/issue.
        retry = Assignment(
            machine_name="m", repo_name="api", issue_number=1461, issue_title="t",
            assignment_id="aee6301971bf", branch="issue-1461-fix", status="done",
        )
        enqueue(retry, repo_github="acme/api", target_branch="main")

        resolved = mq.resolve_entry_key(load_queue(), "api#1461")
        assert resolved is not None
        assert resolved.assignment_id == "aee6301971bf"

    def test_returns_none_when_no_match(self, coord_db) -> None:
        items = [_q("aid1")]
        assert mq.resolve_entry_key(items, "nonexistent") is None
        assert mq.resolve_entry_key(items, "api#9999") is None

    def test_does_not_fuzzy_match_plain_ids(self, coord_db) -> None:
        """A plain id with no '#' must never fall through to a durable-key
        scan — only an exact assignment_id match is attempted."""
        items = [_q("aid")]
        assert mq.resolve_entry_key(items, "ai") is None

    def test_ambiguous_durable_key_prefers_most_recent(self, coord_db) -> None:
        old = _q("old-aid", state=MERGED)
        old.issue_number = 1461
        new = _q("new-aid", state=PENDING)
        new.issue_number = 1461
        items = [old, new]
        assert mq.resolve_entry_key(items, "api#1461") is new

    # ── #1490: bare issue number + branch-name fallbacks ────────────────────

    def test_resolves_bare_issue_number(self, coord_db) -> None:
        entry = _q("aid1")
        entry.issue_number = 1461
        items = [entry]
        assert mq.resolve_entry_key(items, "1461") is entry

    def test_resolves_branch_name(self, coord_db) -> None:
        entry = _q("aid1", branch="issue-1461-fix")
        items = [entry]
        assert mq.resolve_entry_key(items, "issue-1461-fix") is entry

    def test_branch_resolves_even_when_assignment_id_was_rekeyed(self, coord_db) -> None:
        """#1490's actual failure mode: an operator reads assignment_id
        'X' off the board, but a concurrent auto-enqueue tick re-keys the
        entry to 'Y' before ``--only X`` runs. The stale id now matches
        nothing — but the branch, which never changes for the life of the
        entry, still resolves it."""
        entry = _q("Y", branch="issue-1445-fix")
        items = [entry]
        assert mq.resolve_entry_key(items, "X") is None  # the stale id: a hard miss
        assert mq.resolve_entry_key(items, "issue-1445-fix") is entry  # the stable fallback

    def test_bare_issue_number_takes_priority_over_a_coincidental_branch_name(
        self, coord_db
    ) -> None:
        """When a numeric key resolves via the issue-number form, that match
        wins outright — the branch fallback is never even consulted."""
        decoy = _q("aid1", branch="1461")
        decoy.issue_number = 9999
        target = _q("aid2")
        target.issue_number = 1461
        items = [decoy, target]
        assert mq.resolve_entry_key(items, "1461") is target

    def test_assignment_id_takes_priority_over_issue_number_and_branch(
        self, coord_db
    ) -> None:
        exact = _q("1461")  # assignment_id happens to look numeric
        exact.issue_number = 42
        other = _q("aid2")
        other.issue_number = 1461
        items = [exact, other]
        assert mq.resolve_entry_key(items, "1461") is exact

    def test_ambiguous_bare_issue_number_prefers_most_recent(self, coord_db) -> None:
        old = _q("old-aid", state=MERGED)
        old.issue_number = 1461
        new = _q("new-aid", state=PENDING)
        new.issue_number = 1461
        items = [old, new]
        assert mq.resolve_entry_key(items, "1461") is new

    # ── #2080: two slices of one milestone share the tracking issue ────────

    def test_ambiguous_bare_issue_number_prefers_pending_over_merged_sibling(
        self, coord_db
    ) -> None:
        """The exact #2080 sequence: slice-32 is enqueued first and is still
        PENDING; slice-33 is enqueued after it and merges first (an operator
        ran ``--only <tracking-issue>`` once and it happened to land on
        slice-33). A second ``--only <tracking-issue>`` run must reach the
        still-pending slice-32, not the merged slice-33 that "most recently
        added" would otherwise re-pick — that's the bug that made slice-32
        unreachable by the identifier the board itself printed."""
        slice_32 = _q("slice-32-aid", branch="ms-2-slice-32", state=PENDING)
        slice_32.issue_number = 34  # the shared milestone tracking issue
        slice_33 = _q("slice-33-aid", branch="ms-2-slice-33", state=MERGED)
        slice_33.issue_number = 34
        items = [slice_32, slice_33]  # insertion order: 32 then 33
        assert mq.resolve_entry_key(items, "34") is slice_32

    def test_ambiguous_durable_key_prefers_pending_over_merged_sibling(
        self, coord_db
    ) -> None:
        slice_32 = _q(
            "slice-32-aid", branch="ms-2-slice-32", state=PENDING,
            repo="acceptance", repo_github="acme/acceptance",
        )
        slice_32.issue_number = 34
        slice_33 = _q(
            "slice-33-aid", branch="ms-2-slice-33", state=MERGED,
            repo="acceptance", repo_github="acme/acceptance",
        )
        slice_33.issue_number = 34
        items = [slice_32, slice_33]
        assert mq.resolve_entry_key(items, "acceptance#34") is slice_32
        assert mq.resolve_entry_key(items, "acme/acceptance#34") is slice_32

    def test_ambiguous_bare_issue_number_still_prefers_most_recent_when_both_pending(
        self, coord_db
    ) -> None:
        """When two matches are equally actionable (neither has merged yet),
        there is no way to tell them apart from state alone — the pre-#2080
        "most recently added" tie-break still applies, and the operator is
        still on the hook to disambiguate by branch name if they meant the
        other one."""
        slice_32 = _q("slice-32-aid", branch="ms-2-slice-32", state=PENDING)
        slice_32.issue_number = 34
        slice_33 = _q("slice-33-aid", branch="ms-2-slice-33", state=PENDING)
        slice_33.issue_number = 34
        items = [slice_32, slice_33]
        assert mq.resolve_entry_key(items, "34") is slice_33

    def test_ambiguous_bare_issue_number_falls_back_to_most_recent_when_all_terminal(
        self, coord_db
    ) -> None:
        """Both matches already at rest (e.g. both merged) is the one case
        where preferring "actionable" can't disambiguate — fall back to the
        pre-#2080 "most recently added" behaviour rather than matching
        nothing."""
        older = _q("older-aid", branch="ms-2-slice-32", state=MERGED)
        older.issue_number = 34
        newer = _q("newer-aid", branch="ms-2-slice-33", state=mq.SKIPPED)
        newer.issue_number = 34
        items = [older, newer]
        assert mq.resolve_entry_key(items, "34") is newer


class TestDropEntryDurableKey:
    """#1477: drop_entry() resolves the durable 'repo#issue' form too."""

    def test_drops_by_durable_key(self, coord_db) -> None:
        entry = _q("aid1")
        entry.issue_number = 42
        save_queue([entry])
        assert mq.drop_entry("api#42") is True
        assert load_queue() == []

    def test_returns_false_when_durable_key_has_no_match(self, coord_db) -> None:
        save_queue([_q("aid1")])
        assert mq.drop_entry("api#9999") is False
        assert len(load_queue()) == 1


class TestEnqueueApprovedWork:
    """#736: enqueue_approved_work() is the daemon-tick path for reliable
    enqueue-on-approval — called from _passive_tick every 30 seconds so
    approved+tested work enters the merge queue without a manual coord merge.
    """

    # ── helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _config(*, review_enabled: bool = True, gates: list[str] | None = None):
        """Minimal config-like object with .reviews, .pipeline, and .repo()."""
        from dataclasses import dataclass, field as dc_field

        @dataclass
        class _Reviews:
            enabled: bool = True

        @dataclass
        class _Pipeline:
            default_gates: list[str] | None = None

        @dataclass
        class _Repo:
            name: str = "api"
            github: str = "acme/api"
            default_branch: str = "main"

        @dataclass
        class _Cfg:
            reviews: _Reviews = dc_field(default_factory=_Reviews)
            pipeline: _Pipeline = dc_field(default_factory=_Pipeline)
            _repos: list = dc_field(default_factory=lambda: [_Repo()])

            def repo(self, name: str):
                return next((r for r in self._repos if r.name == name), None)

        cfg = _Cfg()
        cfg.reviews.enabled = review_enabled
        cfg.pipeline.default_gates = gates if gates is not None else ["review", "test", "merge"]
        return cfg

    @staticmethod
    def _board(completed=None, active=None):
        from coord.models import Board
        return Board(active=list(active or []), completed=list(completed or []))

    @staticmethod
    def _work(aid: str, *, test_state: str | None = "passed", branch: str | None = None) -> Assignment:
        return Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id=aid, type="work", status="done",
            branch=branch or f"issue-1-{aid}",
            test_state=test_state,
        )

    @staticmethod
    def _review(of_aid: str, *, verdict: str = "approve") -> Assignment:
        return Assignment(
            machine_name="m2", repo_name="api", issue_number=1, issue_title="t",
            assignment_id=f"rev-{of_aid}", type="review", status="done",
            review_of_assignment_id=of_aid, review_verdict=verdict,
        )

    # ── basic happy path ──────────────────────────────────────────────────

    def test_enqueues_when_approved_and_test_passed(self, coord_db) -> None:
        """Approved review + passed test → entry created in merge queue."""
        cfg = self._config()
        work = self._work("w1", test_state="passed")
        rev = self._review("w1", verdict="approve")
        board = self._board(completed=[work, rev])

        changed = mq.enqueue_approved_work(cfg, board)

        assert changed == ["w1"]
        items = load_queue()
        assert len(items) == 1
        assert items[0].assignment_id == "w1"
        assert items[0].branch == "issue-1-w1"

    def test_enqueues_mock_author_completion(self, coord_db) -> None:
        """#930 fix: a completed ``type="mock-author"`` (Gate A) assignment
        with an approved review + passed test must be enqueued the same as
        ordinary work — previously the scan hard-filtered on
        ``type == "work"`` so a Gate A branch could never reach the merge
        queue through any coord command."""
        cfg = self._config()
        mock_author = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="ma1", type="mock-author", status="done",
            branch="ms-5-gate-a", test_state="passed",
        )
        rev = self._review("ma1", verdict="approve")
        board = self._board(completed=[mock_author, rev])

        changed = mq.enqueue_approved_work(cfg, board)

        assert changed == ["ma1"]
        items = load_queue()
        assert len(items) == 1
        assert items[0].assignment_id == "ma1"
        assert items[0].branch == "ms-5-gate-a"

    def test_enqueues_test_author_completion(self, coord_db) -> None:
        """#1141 fix: a completed ``type="test-author"`` (#931, per-issue JIT
        acceptance-slice authoring) assignment with an approved review +
        skipped test must be enqueued the same as ordinary work — previously
        the scan didn't recognize ``test-author`` so a JIT slice could never
        reach the merge queue through any coord command (confirmed live on
        PR #1139, epic #1117/ms-37 retrofit)."""
        cfg = self._config()
        test_author = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="ta1", type="test-author", status="done",
            branch="ms-37-test-author", test_state="skipped",
        )
        rev = self._review("ta1", verdict="approve")
        board = self._board(completed=[test_author, rev])

        changed = mq.enqueue_approved_work(cfg, board)

        assert changed == ["ta1"]
        items = load_queue()
        assert len(items) == 1
        assert items[0].assignment_id == "ta1"
        assert items[0].branch == "ms-37-test-author"

    def test_enqueues_when_test_state_is_skipped(self, coord_db) -> None:
        """test_state='skipped' also satisfies the smoke gate."""
        cfg = self._config()
        work = self._work("w2", test_state="skipped")
        rev = self._review("w2", verdict="approve")
        board = self._board(completed=[work, rev])

        changed = mq.enqueue_approved_work(cfg, board)

        assert "w2" in changed
        assert any(i.assignment_id == "w2" for i in load_queue())

    # ── idempotency ───────────────────────────────────────────────────────

    def test_is_idempotent(self, coord_db) -> None:
        """Second call with the same board is a no-op."""
        cfg = self._config()
        work = self._work("w1", test_state="passed")
        rev = self._review("w1", verdict="approve")
        board = self._board(completed=[work, rev])

        first = mq.enqueue_approved_work(cfg, board)
        second = mq.enqueue_approved_work(cfg, board)

        assert first == ["w1"]
        assert second == []  # already enqueued, no change
        assert len(load_queue()) == 1

    # ── gate conditions ───────────────────────────────────────────────────

    def test_skips_when_review_required_but_not_approved(self, coord_db) -> None:
        """No approved review → item is NOT enqueued when review is required."""
        cfg = self._config(review_enabled=True, gates=["review", "test", "merge"])
        work = self._work("w1", test_state="passed")
        # No review assignment on the board.
        board = self._board(completed=[work])

        changed = mq.enqueue_approved_work(cfg, board)

        assert changed == []
        assert load_queue() == []

    def test_skips_when_test_required_but_no_verdict(self, coord_db) -> None:
        """No test verdict → item is NOT enqueued when smoke is required."""
        cfg = self._config(gates=["review", "test", "merge"])
        work = self._work("w1", test_state=None)
        rev = self._review("w1", verdict="approve")
        board = self._board(completed=[work, rev])

        changed = mq.enqueue_approved_work(cfg, board)

        assert changed == []
        assert load_queue() == []

    def test_enqueues_when_reviews_disabled(self, coord_db) -> None:
        """When reviews.enabled=False, the review gate is skipped entirely
        and items with a passing smoke verdict are enqueued."""
        cfg = self._config(review_enabled=False, gates=["test", "merge"])
        work = self._work("w1", test_state="passed")
        # No review on board — but reviews are disabled so it doesn't matter.
        board = self._board(completed=[work])

        changed = mq.enqueue_approved_work(cfg, board)

        assert "w1" in changed
        assert len(load_queue()) == 1

    def test_enqueues_when_smoke_gate_not_configured(self, coord_db) -> None:
        """When 'test' is absent from default_gates, smoke is not required."""
        cfg = self._config(gates=["review", "merge"])  # no 'test' gate
        work = self._work("w1", test_state=None)  # no test verdict — but gate off
        rev = self._review("w1", verdict="approve")
        board = self._board(completed=[work, rev])

        changed = mq.enqueue_approved_work(cfg, board)

        assert "w1" in changed

    def test_skips_work_with_no_branch(self, coord_db) -> None:
        """Assignments without a branch are silently ignored."""
        cfg = self._config()
        work = self._work("w1", test_state="passed")
        work.branch = None  # type: ignore[assignment]
        rev = self._review("w1", verdict="approve")
        board = self._board(completed=[work, rev])

        changed = mq.enqueue_approved_work(cfg, board)

        assert changed == []

    def test_stale_merged_entry_for_different_branch_does_not_block_enqueue(
        self, coord_db
    ) -> None:
        """#1150: a MERGED queue entry from a *prior* work attempt on a
        different branch (same issue) must NOT block enqueue of fresh work —
        the old issue-level ``already_merged`` shortcut conflated "this issue
        has ever had a merge" with "this exact branch/commit is already
        merged". Termination is now decided solely by Gate 3's commit-aware
        ``work_is_terminal`` (stubbed non-terminal by the autouse fixture)."""
        cfg = self._config()
        work = self._work("w1", test_state="passed")  # branch "issue-1-w1"
        rev = self._review("w1", verdict="approve")
        board = self._board(completed=[work, rev])
        # Seed a MERGED entry for the SAME issue but a DIFFERENT branch — e.g.
        # the issue's original, already-shipped PR from a prior cycle.
        mq.save_queue([_q("orig", state=mq.MERGED, repo="api", branch="worker/orig")])

        changed = mq.enqueue_approved_work(cfg, board)

        assert changed == ["w1"]
        branches = {x.branch for x in load_queue()}
        assert "issue-1-w1" in branches
        # The historical MERGED entry is untouched.
        merged = [x for x in load_queue() if x.assignment_id == "orig"]
        assert merged and merged[0].state == mq.MERGED

    def test_still_skips_when_work_is_terminal_reports_true(
        self, coord_db, monkeypatch
    ) -> None:
        """When Gate 3 (``work_is_terminal``, commit-aware post-#1150) genuinely
        reports this branch as terminal, enqueue is still correctly skipped."""
        from coord import github_ops

        cfg = self._config()
        work = self._work("w1", test_state="passed")
        rev = self._review("w1", verdict="approve")
        board = self._board(completed=[work, rev])
        monkeypatch.setattr(github_ops, "work_is_terminal", lambda *a, **k: True)

        changed = mq.enqueue_approved_work(cfg, board)

        assert changed == []
        assert load_queue() == []

    def test_skips_unknown_repo(self, coord_db) -> None:
        """Assignments for a repo not in config are silently skipped."""
        cfg = self._config()  # only has 'api'
        work = Assignment(
            machine_name="m1", repo_name="unknown-repo", issue_number=1,
            issue_title="t", assignment_id="w1", type="work",
            status="done", branch="issue-1-w1", test_state="passed",
        )
        rev = Assignment(
            machine_name="m2", repo_name="unknown-repo", issue_number=1,
            issue_title="t", assignment_id="rev-w1", type="review",
            status="done", review_of_assignment_id="w1", review_verdict="approve",
        )
        board = self._board(completed=[work, rev])

        changed = mq.enqueue_approved_work(cfg, board)

        assert changed == []

    # ── re-keying after bounce (#292) ─────────────────────────────────────

    def test_rekeyes_after_bounce(self, coord_db) -> None:
        """After a review bounce the fix work's approval re-keys the queue
        entry so has_approved_review can find it (#292 Defect 2)."""
        cfg = self._config()

        # Original work is done; its entry was created by a prior coord merge run.
        orig_work = self._work("orig", branch="issue-1-orig")
        mq.save_queue([
            QueuedMerge(
                assignment_id="orig",
                repo_name="api",
                repo_github="acme/api",
                branch="issue-1-orig",
                target_branch="main",
                issue_number=1,
                issue_title="t",
            )
        ])

        # Fix work is now done on the same branch; it was approved.
        fix_work = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="[fix-1] t",
            assignment_id="fix1", type="work", status="done",
            branch="issue-1-orig",  # same branch as orig_work
            test_state="passed",
        )
        fix_rev = Assignment(
            machine_name="m2", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="rev-fix1", type="review", status="done",
            review_of_assignment_id="fix1", review_verdict="approve",
        )
        board = self._board(completed=[orig_work, fix_work, fix_rev])

        changed = mq.enqueue_approved_work(cfg, board)

        # The entry was re-keyed to fix1 (the approved fix assignment).
        assert changed == ["fix1"]
        items = load_queue()
        assert len(items) == 1
        assert items[0].assignment_id == "fix1"
        assert items[0].branch == "issue-1-orig"

    def test_rekeying_is_idempotent(self, coord_db) -> None:
        """Re-keying is a no-op when the entry is already keyed to fix1."""
        cfg = self._config()

        # Entry already keyed to fix1.
        mq.save_queue([
            QueuedMerge(
                assignment_id="fix1",
                repo_name="api",
                repo_github="acme/api",
                branch="issue-1-orig",
                target_branch="main",
                issue_number=1,
                issue_title="t",
            )
        ])

        fix_work = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="[fix-1] t",
            assignment_id="fix1", type="work", status="done",
            branch="issue-1-orig",
            test_state="passed",
        )
        fix_rev = Assignment(
            machine_name="m2", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="rev-fix1", type="review", status="done",
            review_of_assignment_id="fix1", review_verdict="approve",
        )
        board = self._board(completed=[fix_work, fix_rev])

        changed = mq.enqueue_approved_work(cfg, board)

        assert changed == []  # already correct — no change
        assert load_queue()[0].assignment_id == "fix1"

    def test_does_not_rekey_onto_an_approval_superseded_by_request_changes(
        self, coord_db
    ) -> None:
        """#2085: the #1966 chain. round 1 (approve) is followed by round 2
        (a fix round on the SAME branch whose review comes back
        request-changes). `group_branch_candidates` picks round 2 as the
        branch's current winner (most recent passed test verdict), so the
        gate this daemon tick actually evaluates is round 2's — and it must
        see NO approved review, because the only `approve` on this branch's
        chain covers a commit round 2 has already moved past.

        Before #2085, `passes_merge_gates` -> `has_approved_review(a, board,
        gh_ops=None)` — called with a raw work ``Assignment``, no
        ``branch_head_sha`` attribute — treated that missing attribute as
        "nothing to compare against" and accepted round 1's stale approval
        outright, re-keying (or creating) a queue entry the merge gate would
        then refuse on the very next real merge attempt: exactly the
        re-enqueue-forever loop #2085 traces `enqueue_approved_work` to.
        """
        cfg = self._config()

        round1_work = self._work("round1", branch="issue-1-impl")
        round1_review = Assignment(
            machine_name="m2", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="rev-round1", type="review", status="done",
            review_of_assignment_id="round1", review_verdict="approve",
            review_head_sha="sha-a",
        )
        # An earlier daemon tick already enqueued round 1's (then-valid)
        # approval — the entry exists, keyed to round1.
        mq.save_queue([
            QueuedMerge(
                assignment_id="round1",
                repo_name="api",
                repo_github="acme/api",
                branch="issue-1-impl",
                target_branch="main",
                issue_number=1,
                issue_title="t",
            )
        ])

        round2_work = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="[fix-1] t",
            assignment_id="round2", type="work", status="done",
            branch="issue-1-impl", review_of_assignment_id="round1",
            test_state="passed",
        )
        round2_review = Assignment(
            machine_name="m2", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="rev-round2", type="review", status="done",
            review_of_assignment_id="round2", review_verdict="request-changes",
            review_head_sha="sha-b",
        )
        board = self._board(
            completed=[round1_work, round1_review, round2_work, round2_review]
        )

        changed = mq.enqueue_approved_work(cfg, board)

        assert changed == [], (
            "a superseded approval must not re-key the queue entry onto the "
            "unapproved round"
        )
        items = load_queue()
        assert len(items) == 1
        # Left exactly as it was — still keyed to round1, not silently
        # advanced to round2 behind an approval that doesn't cover it.
        assert items[0].assignment_id == "round1"

    def test_enqueues_fresh_approval_confirmed_via_live_branch_sha(
        self, coord_db, monkeypatch
    ) -> None:
        """#2085 (fix-iteration regression guard): the review panel on this
        same issue found that #2085's own fix — folding "current SHA
        unknown" into the fail-CLOSED branch of `has_approved_review` —
        broke the ordinary case, not just the superseded-approval one this
        test's sibling above covers. `enqueue_approved_work` used to call
        `passes_merge_gates(a, config, board)` with NO `gh_ops` at all,
        handing `has_approved_review` a raw work `Assignment` with no
        `branch_head_sha` attribute. Since a review captures
        `review_head_sha` on essentially every real completion
        (`coord.review`), that made `current_sha is None` true for almost
        every approval — not just superseded ones — which the #2085 fix
        then refused unconditionally: the daemon's passive-tick auto-enqueue
        path (#736) would never enqueue a normal, unsuperseded approval
        again.

        `enqueue_approved_work` must now thread a LIVE `gh_ops` (mirroring
        `coord.gates.build_gate_report`'s construction, via
        `mq.live_gate_entry`) so a review whose `review_head_sha` matches the
        branch's live current head is recognized as fresh and the work is
        enqueued — exactly what a real `coord merge` run would do.
        """
        from coord import github_ops

        cfg = self._config()
        work = self._work("w1", branch="issue-1-impl", test_state="passed")
        review = self._review("w1", verdict="approve")
        review.review_head_sha = "sha-current"  # captured when the review ran
        board = self._board(completed=[work, review])

        # The branch's CURRENT head, live from GitHub — matches the review's
        # captured SHA, i.e. no commits landed after the review completed.
        monkeypatch.setattr(
            github_ops, "get_branch_sha",
            lambda repo, branch: "sha-current" if branch == "issue-1-impl" else None,
        )
        monkeypatch.setattr(github_ops, "get_branch_patch_id", lambda *a, **k: None)
        monkeypatch.setattr(github_ops, "work_is_terminal", lambda *a, **k: False)

        changed = mq.enqueue_approved_work(cfg, board)

        assert changed == ["w1"], (
            "a fresh, unsuperseded approval whose review_head_sha matches "
            "the branch's LIVE current head must still be auto-enqueued — "
            "#736's daemon-tick guarantee must not regress into 'never "
            "auto-enqueues a real approval again'"
        )
        items = load_queue()
        assert len(items) == 1
        assert items[0].assignment_id == "w1"

    # ── #934 milestone-aware target_branch ──────────────────────────────────

    @staticmethod
    def _config_with_develop_branch(*, develop_branch: str | None = "develop"):
        """Same shape as ``_config`` but the repo stand-in also carries
        ``develop_branch`` — #934's opt-in git model."""
        from dataclasses import dataclass, field as dc_field

        @dataclass
        class _Reviews:
            enabled: bool = True

        @dataclass
        class _Pipeline:
            default_gates: list[str] | None = None

        @dataclass
        class _Repo:
            name: str = "api"
            github: str = "acme/api"
            default_branch: str = "main"
            develop_branch: str | None = None

        @dataclass
        class _Cfg:
            reviews: _Reviews = dc_field(default_factory=_Reviews)
            pipeline: _Pipeline = dc_field(default_factory=_Pipeline)
            _repos: list = dc_field(default_factory=lambda: [_Repo(develop_branch=develop_branch)])

            def repo(self, name: str):
                return next((r for r in self._repos if r.name == name), None)

        cfg = _Cfg()
        cfg.pipeline.default_gates = ["review", "test", "merge"]
        return cfg

    def test_targets_feature_branch_for_opted_in_repo_with_milestone(self, coord_db) -> None:
        """#934 review should-fix: the "merge targets the right base" seam
        the issue explicitly asked for — enqueue_approved_work must resolve
        target_branch to feature/ms-NN when the repo opted into the git
        model and the issue belongs to a milestone, not hardcode
        default_branch."""
        cfg = self._config_with_develop_branch(develop_branch="develop")
        work = self._work("w1", test_state="passed")
        rev = self._review("w1", verdict="approve")
        board = self._board(completed=[work, rev])

        with patch(
            "coord.github_ops.get_issue",
            return_value={"milestone": {"number": 9, "title": "M9"}},
        ):
            changed = mq.enqueue_approved_work(cfg, board)

        assert changed == ["w1"]
        items = load_queue()
        assert len(items) == 1
        assert items[0].target_branch == "feature/ms-9"

    def test_targets_default_branch_when_issue_has_no_milestone(self, coord_db) -> None:
        """Opted-in repo, but this issue isn't tagged to any milestone —
        falls back to default_branch, same as an un-opted-in repo."""
        cfg = self._config_with_develop_branch(develop_branch="develop")
        work = self._work("w1", test_state="passed")
        rev = self._review("w1", verdict="approve")
        board = self._board(completed=[work, rev])

        with patch("coord.github_ops.get_issue", return_value={"milestone": None}):
            changed = mq.enqueue_approved_work(cfg, board)

        assert changed == ["w1"]
        items = load_queue()
        assert items[0].target_branch == "main"

    def test_targets_default_branch_when_repo_not_opted_in(self, coord_db) -> None:
        """No develop_branch configured → default_branch, and the milestone
        `gh` lookup must never even happen (zero extra cost for repos that
        haven't opted in)."""
        cfg = self._config_with_develop_branch(develop_branch=None)
        work = self._work("w1", test_state="passed")
        rev = self._review("w1", verdict="approve")
        board = self._board(completed=[work, rev])

        with patch("coord.github_ops.get_issue") as get_issue:
            changed = mq.enqueue_approved_work(cfg, board)

        get_issue.assert_not_called()
        assert changed == ["w1"]
        items = load_queue()
        assert items[0].target_branch == "main"

    # ── #1490: one branch, N work rows, one queue entry ────────────────────

    def test_three_work_rows_one_branch_produce_one_entry(self, coord_db) -> None:
        """The exact #1445 scenario: one failed test_state, two passed, all
        on the same branch. Must produce exactly one queue entry, keyed to
        the winning (approved + test-passed) row — not whichever row the
        board happened to list last."""
        cfg = self._config()
        branch = "issue-1445-fix"
        failed = self._work("31bd30875eb3", test_state="failed", branch=branch)
        failed.dispatched_at = 1000
        passed1 = self._work("12fced1dfa80", test_state="passed", branch=branch)
        passed1.dispatched_at = 2000
        passed2 = self._work("5ed99d1f7edf", test_state="passed", branch=branch)
        passed2.dispatched_at = 3000
        # An approval anywhere in the branch's chain covers the whole branch
        # (has_approved_review scans by shared branch) — point it at the
        # winning row, matching what actually happened on #1445.
        rev = self._review("5ed99d1f7edf", verdict="approve")
        board = self._board(completed=[failed, passed1, passed2, rev])

        changed = mq.enqueue_approved_work(cfg, board)

        assert changed == ["5ed99d1f7edf"]
        items = load_queue()
        assert len(items) == 1
        assert items[0].assignment_id == "5ed99d1f7edf"
        assert items[0].branch == branch

    def test_repeated_ticks_do_not_reannounce_same_branch(self, coord_db) -> None:
        """#1490 regression: before the fix, every tick re-keyed the one
        queue entry to whichever row was processed last in board.completed
        order and reported it as a change — forever, even with zero new
        work. A second call with the same board must be a true no-op."""
        cfg = self._config()
        branch = "issue-1445-fix"
        failed = self._work("31bd30875eb3", test_state="failed", branch=branch)
        passed1 = self._work("12fced1dfa80", test_state="passed", branch=branch)
        passed2 = self._work("5ed99d1f7edf", test_state="passed", branch=branch)
        failed.dispatched_at, passed1.dispatched_at, passed2.dispatched_at = (
            1000, 2000, 3000,
        )
        rev = self._review("5ed99d1f7edf", verdict="approve")
        board = self._board(completed=[failed, passed1, passed2, rev])

        first = mq.enqueue_approved_work(cfg, board)
        second = mq.enqueue_approved_work(cfg, board)
        third = mq.enqueue_approved_work(cfg, board)

        assert first == ["5ed99d1f7edf"]
        assert second == []
        assert third == []
        assert len(load_queue()) == 1

    def test_iteration_order_does_not_change_the_winner(self, coord_db) -> None:
        """The winner is picked by dispatched_at, not by position in
        board.completed — reordering the same three rows must resolve to
        the same winner."""
        cfg = self._config()
        branch = "issue-1445-fix"
        failed = self._work("31bd30875eb3", test_state="failed", branch=branch)
        failed.dispatched_at = 1000
        passed1 = self._work("12fced1dfa80", test_state="passed", branch=branch)
        passed1.dispatched_at = 2000
        passed2 = self._work("5ed99d1f7edf", test_state="passed", branch=branch)
        passed2.dispatched_at = 3000
        rev = self._review("5ed99d1f7edf", verdict="approve")
        # Deliberately out of dispatch order.
        board = self._board(completed=[passed2, failed, passed1, rev])

        changed = mq.enqueue_approved_work(cfg, board)

        assert changed == ["5ed99d1f7edf"]
        assert load_queue()[0].assignment_id == "5ed99d1f7edf"

    # ── #1601: sweep on a condition, not an event ───────────────────────────

    def test_enqueues_1566_topology_without_any_transition_event(
        self, coord_db, monkeypatch
    ) -> None:
        """#1601 (the #1566 incident): a review verdict written with no
        corresponding transition event still results in an enqueued merge on
        the next sweep. This board is constructed directly — no
        `process_review_completion`/`_advance_pipeline` call ever ran — the
        exact "the transition that would have triggered enqueue was missed"
        shape #1441 fixed for reviews, applied here to the merge queue.

        Board shape (from the #1566 incident): parent work is done, tested,
        and smoked, but its own `review_state` is stuck at "dispatched" with
        no verdict (superseded by a fix round); the fix round is done and
        approved but never re-tested; a second review approved the fix.
        `enqueue_approved_work` must resolve the branch's winner (the
        parent — it's the only row with a fresh terminal test_state) and
        find the fix round's approval through the chain."""
        from coord import github_ops

        monkeypatch.setattr(github_ops, "work_is_terminal", lambda *a, **k: False)
        cfg = self._config()

        parent = self._work("8b26520edabb", test_state="passed", branch="issue-1566-fix")
        parent.review_state = "dispatched"
        parent.review_verdict = None
        parent.dispatched_at = 1.0
        review1 = self._review("8b26520edabb", verdict="request-changes")
        review1.dispatched_at = 2.0
        fix = self._work("adaff508c83d", test_state=None, branch="issue-1566-fix")
        fix.review_of_assignment_id = "8b26520edabb"
        fix.review_state = "done"
        fix.review_verdict = "approve"
        fix.dispatched_at = 3.0
        review2 = self._review("adaff508c83d", verdict="approve")
        review2.dispatched_at = 4.0
        board = self._board(completed=[parent, review1, fix, review2])

        changed = mq.enqueue_approved_work(cfg, board)

        assert changed == ["8b26520edabb"]
        items = load_queue()
        assert len(items) == 1
        assert items[0].assignment_id == "8b26520edabb"
        assert items[0].branch == "issue-1566-fix"


class TestPendingSummary:
    def test_groups_by_repo_excludes_terminal(self) -> None:
        items = [
            _q("a", repo="api"),
            _q("b", repo="api", state=MERGED),
            _q("c", repo="ui", state=CONFLICT),
        ]
        s = pending_summary(items)
        assert set(s.keys()) == {"api", "ui"}
        assert [x.assignment_id for x in s["api"]] == ["a"]
        assert [x.assignment_id for x in s["ui"]] == ["c"]


# ── #732 drop_entry / prune_stale_queue_entries ───────────────────────────────

class TestDropEntry:
    """#732: drop_entry() removes exactly one row by assignment_id."""

    def test_drops_existing_entry(self, coord_db) -> None:
        save_queue([_q("aid1"), _q("aid2")])
        removed = mq.drop_entry("aid1")
        assert removed is True
        remaining = load_queue()
        assert [x.assignment_id for x in remaining] == ["aid2"]

    def test_returns_false_when_not_found(self, coord_db) -> None:
        save_queue([_q("aid1")])
        removed = mq.drop_entry("ghost")
        assert removed is False
        # original entry untouched
        assert len(load_queue()) == 1

    def test_returns_false_on_empty_queue(self, coord_db) -> None:
        assert mq.drop_entry("anything") is False

    def test_only_removes_exact_match(self, coord_db) -> None:
        """Prefix / substring of an ID must not match."""
        save_queue([_q("aid-long"), _q("aid")])
        mq.drop_entry("aid")
        remaining = [x.assignment_id for x in load_queue()]
        assert "aid-long" in remaining
        assert "aid" not in remaining


class TestPruneStaleQueueEntries:
    """#732: prune_stale_queue_entries() removes closed-issue / merged-PR entries."""

    def _seed(self, coord_db, entries: list[QueuedMerge]) -> None:
        save_queue(entries)

    def test_prunes_closed_issue(self, coord_db, monkeypatch) -> None:
        from coord import github_ops

        monkeypatch.setattr(github_ops, "issue_is_closed", lambda repo, n: n == 217)
        monkeypatch.setattr(github_ops, "pr_is_merged", lambda repo, branch: False)

        self._seed(coord_db, [
            _q("stale", state=CONFLICT),
            _q("live"),
        ])
        pruned = mq.prune_stale_queue_entries()
        assert len(pruned) == 0  # issue_number on _q() is 1, not 217
        # Seed with the right issue number
        save_queue([
            QueuedMerge(
                assignment_id="stale217",
                repo_name="api", repo_github="acme/api",
                branch="issue-217-foo", target_branch="main",
                issue_number=217, issue_title="closed issue",
                state=CONFLICT,
            ),
            _q("live"),
        ])
        pruned = mq.prune_stale_queue_entries()
        assert len(pruned) == 1
        assert pruned[0].assignment_id == "stale217"
        remaining = load_queue()
        assert len(remaining) == 1
        assert remaining[0].assignment_id == "live"

    def test_prunes_merged_pr(self, coord_db, monkeypatch) -> None:
        from coord import github_ops

        monkeypatch.setattr(github_ops, "issue_is_closed", lambda repo, n: False)
        monkeypatch.setattr(
            github_ops, "pr_is_merged",
            lambda repo, branch: branch == "issue-1-merged-branch",
        )

        save_queue([
            QueuedMerge(
                assignment_id="merged-aid",
                repo_name="api", repo_github="acme/api",
                branch="issue-1-merged-branch", target_branch="main",
                issue_number=1, issue_title="t",
                state=PENDING,
            ),
            _q("live", branch="issue-2-live"),
        ])
        pruned = mq.prune_stale_queue_entries()
        assert [x.assignment_id for x in pruned] == ["merged-aid"]
        assert [x.assignment_id for x in load_queue()] == ["live"]

    def test_leaves_merged_state_entry_untouched(self, coord_db, monkeypatch) -> None:
        """MERGED-state entries are correct history — must not be re-checked."""
        from coord import github_ops

        calls: list[str] = []
        monkeypatch.setattr(
            github_ops, "issue_is_closed",
            lambda repo, n: calls.append("closed") or False,
        )
        monkeypatch.setattr(
            github_ops, "pr_is_merged",
            lambda repo, b: calls.append("pr") or False,
        )

        save_queue([_q("done", state=MERGED)])
        pruned = mq.prune_stale_queue_entries()
        assert pruned == []
        assert calls == []  # no gh calls at all
        assert len(load_queue()) == 1

    def test_dry_run_does_not_write(self, coord_db, monkeypatch) -> None:
        from coord import github_ops

        monkeypatch.setattr(github_ops, "issue_is_closed", lambda repo, n: True)
        monkeypatch.setattr(github_ops, "pr_is_merged", lambda repo, b: False)

        save_queue([_q("stale")])
        pruned = mq.prune_stale_queue_entries(dry_run=True)
        assert len(pruned) == 1
        assert len(load_queue()) == 1  # still there — dry run

    def test_fail_open_on_gh_error(self, coord_db, monkeypatch) -> None:
        """A gh error in issue_is_closed keeps the entry (fail-open)."""
        from coord import github_ops

        monkeypatch.setattr(
            github_ops, "issue_is_closed",
            lambda repo, n: False,  # gh error simulated as False (fail-open)
        )
        monkeypatch.setattr(github_ops, "pr_is_merged", lambda repo, b: False)

        save_queue([_q("live")])
        pruned = mq.prune_stale_queue_entries()
        assert pruned == []
        assert len(load_queue()) == 1

    def test_mock_author_survives_closed_tracking_epic(self, coord_db, monkeypatch) -> None:
        """#3063: a mock-author row's issue_number is the tracking epic, not
        its own deliverable (#2639) — a closed epic must not prune it. Only
        `pr_is_merged` (branch-scoped) may retire it.
        """
        from coord import github_ops

        monkeypatch.setattr(github_ops, "issue_is_closed", lambda repo, n: True)
        monkeypatch.setattr(github_ops, "pr_is_merged", lambda repo, b: False)

        save_queue([
            _q("mock-row", branch="issue-122-mock", assignment_type="mock-author"),
        ])
        pruned = mq.prune_stale_queue_entries()
        assert pruned == []
        assert [x.assignment_id for x in load_queue()] == ["mock-row"]

    def test_mock_author_pruned_once_its_own_pr_merges(self, coord_db, monkeypatch) -> None:
        """#3063: the carve-out doesn't wedge a mock-author row forever — once
        its own branch is confirmed merged, pr_is_merged still prunes it.
        """
        from coord import github_ops

        monkeypatch.setattr(github_ops, "issue_is_closed", lambda repo, n: True)
        monkeypatch.setattr(
            github_ops, "pr_is_merged",
            lambda repo, branch: branch == "issue-122-mock",
        )

        save_queue([
            _q("mock-row", branch="issue-122-mock", assignment_type="mock-author"),
        ])
        pruned = mq.prune_stale_queue_entries()
        assert [x.assignment_id for x in pruned] == ["mock-row"]
        assert load_queue() == []


# ── #776: enqueued_at + size-at-enqueue-time ──────────────────────────────────

class TestEnqueuedAt:
    """#776: enqueue() sets enqueued_at and populates size via the compare API."""

    def _assignment(self, aid: str = "abc", branch: str = "issue-1-foo") -> Assignment:
        return Assignment(
            machine_name="m", repo_name="api", issue_number=1, issue_title="t",
            assignment_id=aid, branch=branch, status="done",
        )

    def test_enqueue_sets_enqueued_at(self, coord_db, monkeypatch) -> None:
        from coord import github_ops
        monkeypatch.setattr(github_ops, "get_branch_diff_size", lambda *a: 0)
        before = mq.__import_time = __import__("time").time()
        enqueue(self._assignment(), repo_github="acme/api", target_branch="main")
        items = load_queue()
        assert len(items) == 1
        assert items[0].enqueued_at is not None
        assert items[0].enqueued_at >= before

    def test_enqueue_populates_size_from_compare_api(self, coord_db, monkeypatch) -> None:
        from coord import github_ops
        monkeypatch.setattr(github_ops, "get_branch_diff_size", lambda repo, base, branch: 123)
        enqueue(self._assignment(), repo_github="acme/api", target_branch="main")
        items = load_queue()
        assert items[0].size == 123

    def test_enqueue_size_none_on_compare_failure(self, coord_db, monkeypatch) -> None:
        """When get_branch_diff_size returns 0, size is stored as None (unknown)."""
        from coord import github_ops
        monkeypatch.setattr(github_ops, "get_branch_diff_size", lambda *a: 0)
        enqueue(self._assignment(), repo_github="acme/api", target_branch="main")
        items = load_queue()
        # 0 is treated as unknown → None so unknown-size entries sort last.
        assert items[0].size is None

    def test_enqueue_size_survives_exception(self, coord_db, monkeypatch) -> None:
        """If the compare API raises, enqueue still succeeds with size=None."""
        from coord import github_ops
        def _raise(*a):
            raise RuntimeError("gh error")
        monkeypatch.setattr(github_ops, "get_branch_diff_size", _raise)
        entry = enqueue(self._assignment(), repo_github="acme/api", target_branch="main")
        assert entry is not None
        assert entry.size is None

    def test_enqueued_at_roundtrips_through_db(self, coord_db, monkeypatch) -> None:
        from coord import github_ops
        monkeypatch.setattr(github_ops, "get_branch_diff_size", lambda *a: 50)
        entry = enqueue(self._assignment(), repo_github="acme/api", target_branch="main")
        assert entry is not None
        loaded = load_queue()[0]
        assert loaded.enqueued_at == pytest.approx(entry.enqueued_at, abs=1.0)
        assert loaded.size == 50


# ── #776: plan() ─────────────────────────────────────────────────────────────

class TestPlan:
    """#776: plan() returns an ordered, gate-annotated PlannedMerge list.

    The plan is the single source of truth for ordering and gate-status —
    it must match sequence() exactly and apply the same gate logic as process().
    """

    # ── helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _config(*, review_enabled: bool = True, gates: list[str] | None = None):
        from dataclasses import dataclass, field as dc_field

        @dataclass
        class _Reviews:
            enabled: bool = True

        @dataclass
        class _Pipeline:
            default_gates: list[str] | None = None

        @dataclass
        class _Cfg:
            reviews: _Reviews = dc_field(default_factory=_Reviews)
            pipeline: _Pipeline = dc_field(default_factory=_Pipeline)

        cfg = _Cfg()
        cfg.reviews.enabled = review_enabled
        cfg.pipeline.default_gates = gates if gates is not None else ["review", "test", "merge"]
        return cfg

    @staticmethod
    def _board(completed=None, active=None):
        from coord.models import Board
        return Board(active=list(active or []), completed=list(completed or []))

    @staticmethod
    def _work(aid: str = "w1", *, test_state: str | None = "passed") -> Assignment:
        return Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id=aid, type="work", status="done",
            branch=f"issue-1-{aid}", test_state=test_state,
        )

    @staticmethod
    def _review(of_aid: str, *, verdict: str = "approve") -> Assignment:
        return Assignment(
            machine_name="m2", repo_name="api", issue_number=1, issue_title="t",
            assignment_id=f"rev-{of_aid}", type="review", status="done",
            review_of_assignment_id=of_aid, review_verdict=verdict,
        )

    @staticmethod
    def _seed_queue(
        items: list,
        *,
        monkeypatch,
        github_ops_mod=None,
    ) -> None:
        """Seed pre-built QueuedMerge items directly (bypass enqueue size-lookup)."""
        save_queue(items)

    # ── ordering tests ────────────────────────────────────────────────────

    def test_ordering_matches_sequence(self, coord_db) -> None:
        """Plan order within a group must match sequence() (size-ascending)."""
        items = [_q("big", size=500), _q("small", size=50), _q("mid", size=100)]
        save_queue(items)
        cfg = self._config(review_enabled=False, gates=["merge"])
        board = self._board()
        plan = mq.plan(board, cfg)
        aids = [p.assignment_id for p in plan]
        # sequence() returns [small, mid, big]
        assert aids == ["small", "mid", "big"]

    def test_rank_is_one_based_ascending(self, coord_db) -> None:
        """Rank starts at 1 and increments by 1 per entry."""
        items = [_q("a", size=10), _q("b", size=20), _q("c", size=30)]
        save_queue(items)
        cfg = self._config(review_enabled=False, gates=["merge"])
        board = self._board()
        plan = mq.plan(board, cfg)
        assert [p.rank for p in plan] == [1, 2, 3]

    def test_unknown_size_goes_last(self, coord_db) -> None:
        """Entries with unknown size are placed last (same as sequence())."""
        items = [_q("big", size=None), _q("small", size=50)]
        save_queue(items)
        cfg = self._config(review_enabled=False, gates=["merge"])
        board = self._board()
        plan = mq.plan(board, cfg)
        assert [p.assignment_id for p in plan] == ["small", "big"]

    def test_groups_by_repo_and_target_branch(self, coord_db) -> None:
        """Each (repo_github, target_branch) group is ordered independently."""
        items = [
            _q("api-big",   repo="api", repo_github="acme/api", target="main",    size=500),
            _q("api-small", repo="api", repo_github="acme/api", target="main",    size=50),
            _q("ui-big",    repo="ui",  repo_github="acme/ui",  target="develop", size=300),
        ]
        save_queue(items)
        cfg = self._config(review_enabled=False, gates=["merge"])
        board = self._board()
        plan = mq.plan(board, cfg)
        # Both groups present in plan; each group ordered by size
        aids = [p.assignment_id for p in plan]
        # api group: small first; ui group has one entry
        assert "api-small" in aids
        api_idx_small = aids.index("api-small")
        api_idx_big   = aids.index("api-big")
        assert api_idx_small < api_idx_big

    # ── gate-status tests ─────────────────────────────────────────────────

    def test_ready_when_all_gates_pass(self, coord_db) -> None:
        """An entry with approved review + passed test appears as READY."""
        items = [_q("w1", size=100)]
        save_queue(items)
        board = self._board(completed=[
            self._work("w1", test_state="passed"),
            self._review("w1", verdict="approve"),
        ])
        cfg = self._config()
        plan = mq.plan(board, cfg)
        assert len(plan) == 1
        assert plan[0].status == mq.PLAN_READY
        assert plan[0].reason is None
        assert plan[0].rank == 1
        assert plan[0].size == 100

    def test_ready_when_gh_ops_backfills_null_branch_patch_id(self, coord_db) -> None:
        """#1506: an entry whose approved review predates #1475
        (review_patch_id set, but the entry's own branch_patch_id was never
        backfilled — e.g. no `coord merge` tick ran between the rebase and
        this `plan()` call) must not display BLOCKED just because the
        stored field is null — plan() already receives gh_ops (used for the
        epic-closing-keyword gate); this proves it's also threaded into the
        review gate's on-demand patch-id computation."""
        items = [_q("w1", size=100, target="main", repo_github="acme/api")]
        items[0].branch_head_sha = "def456"  # rebased since the review ran
        items[0].branch_patch_id = None      # never backfilled
        save_queue(items)
        review = self._review("w1", verdict="approve")
        review.review_head_sha = "abc123"
        review.review_patch_id = "patchid-same"
        board = self._board(completed=[
            self._work("w1", test_state="passed"),
            review,
        ])
        cfg = self._config()

        class _Gh:
            def get_branch_patch_id(self, repo: str, base: str, branch: str) -> str | None:
                assert (repo, base) == ("acme/api", "main")
                return "patchid-same"
            def get_pr_commit_messages(self, repo: str, number: int) -> list[str]:
                return []
            def is_epic_issue(self, repo: str, issue_number: int) -> bool:
                return False

        plan = mq.plan(board, cfg, gh_ops=_Gh())
        assert plan[0].status == mq.PLAN_READY
        assert plan[0].reason is None

    def test_blocked_review_not_approved(self, coord_db) -> None:
        """Entry missing an approved review appears as BLOCKED with reason."""
        items = [_q("w1", size=50)]
        save_queue(items)
        # No review on the board
        board = self._board(completed=[self._work("w1", test_state="passed")])
        cfg = self._config()
        plan = mq.plan(board, cfg)
        assert plan[0].status == mq.PLAN_BLOCKED
        assert "review" in (plan[0].reason or "").lower()

    def test_blocked_unknown_branch_head_is_not_reported_as_not_approved(
        self, coord_db,
    ) -> None:
        """#2704: an approving review DOES exist, but this entry's branch
        head is unconfirmable (never populated, no live gh_ops) — the plan
        view must say so, not render the generic "review not approved" a
        genuine refusal gets (`test_blocked_review_not_approved` above)."""
        items = [_q("w1", size=50)]
        save_queue(items)
        work = self._work("w1", test_state="passed")
        review = self._review("w1", verdict="approve")
        review.review_head_sha = "sha-any"  # a SHA to compare, just unconfirmable here
        board = self._board(completed=[work, review])
        cfg = self._config()

        plan = mq.plan(board, cfg)

        assert plan[0].status == mq.PLAN_BLOCKED
        assert plan[0].reason == mq.UNKNOWN_BRANCH_HEAD_REASON

    def test_blocked_test_verdict_missing(self, coord_db) -> None:
        """Entry with no test verdict appears as BLOCKED with reason."""
        items = [_q("w1", size=50)]
        save_queue(items)
        board = self._board(completed=[
            self._work("w1", test_state=None),
            self._review("w1", verdict="approve"),
        ])
        cfg = self._config()
        plan = mq.plan(board, cfg)
        assert plan[0].status == mq.PLAN_BLOCKED
        assert "test" in (plan[0].reason or "").lower()

    def test_plan_and_only_agree_on_stale_parent_smoke_verdict(self, coord_db) -> None:
        """#1601 (the #1566 incident): a fix round is approved by review but
        never re-tested; the only test verdict anywhere on the branch is the
        PARENT's, and it's stale relative to the fix commit (its
        `test_head_sha` doesn't match the branch's live head). Before #1601,
        `has_smoke_verdict` only ever saw a freshly-enqueued entry's own
        (always-`None`) `branch_head_sha`/`branch_patch_id` fields — the
        staleness check silently no-op'd — so `coord merge --plan` showed
        READY for exactly the entry `coord merge --only` (whose `process()`
        DOES live-fetch those fields first) then refused as stale. Passing
        `gh_ops` into `has_smoke_verdict` (mirroring `has_approved_review`)
        closes that plan-vs-enforcement split: both must now see the SAME
        stale verdict and agree it's BLOCKED."""
        cfg = self._config()
        parent = self._work("8b26520edabb", test_state="passed")
        parent.branch = "issue-1566-fix"
        parent.test_head_sha = "sha-before-fix"
        parent.test_base_sha = "sha-base"
        parent.test_patch_id = "patch-before-fix"
        fix = Assignment(
            machine_name="m1", repo_name="api", issue_number=1,
            issue_title="[fix] t", assignment_id="adaff508c83d", type="work",
            status="done", branch="issue-1566-fix", test_state=None,
            review_of_assignment_id="8b26520edabb",
        )
        review2 = self._review("adaff508c83d", verdict="approve")
        board = self._board(completed=[parent, fix, review2])

        class _FixShaGh(FakeGh):
            def get_branch_sha(self, repo, branch):
                return "sha-base" if branch == "main" else "sha-after-fix"

            def get_branch_patch_id(self, repo, base, branch):
                return "patch-after-fix"

        gh = _FixShaGh()

        save_queue([_q("8b26520edabb", branch="issue-1566-fix", size=10)])
        plan_result = mq.plan(board, cfg, gh_ops=gh)
        assert plan_result[0].status == mq.PLAN_BLOCKED
        assert "test" in (plan_result[0].reason or "").lower()

        # `plan()` never persists its in-memory SHA backfill (read-only, no
        # DB writes) — reload a fresh entry so `--only`'s process() sees
        # exactly what a real invocation would: nothing pre-populated.
        save_queue([_q("8b26520edabb", branch="issue-1566-fix", size=10)])
        only_items = mq.load_queue()
        events = mq.process(only_items, gh, dry_run=True, config=cfg, board=board)
        kinds = [e.kind for e in events]
        assert "smoke_required" in kinds

    def test_blocked_ci_failed(self, coord_db) -> None:
        """Entry with a failed CI check appears as BLOCKED with CI reason."""
        from types import SimpleNamespace

        class FakeCi:
            is_available = True
            def list_checks_for_pr(self, repo, number):
                return [SimpleNamespace(name="build", status="completed", conclusion="failure")]

        items = [_q("w1", size=50, pr=99)]
        save_queue(items)
        board = self._board(completed=[
            self._work("w1", test_state="passed"),
            self._review("w1", verdict="approve"),
        ])
        cfg = self._config()
        plan = mq.plan(board, cfg, ci_store=FakeCi())
        assert plan[0].status == mq.PLAN_BLOCKED
        assert "CI failed" in (plan[0].reason or "")

    def test_blocked_ci_running(self, coord_db) -> None:
        """Entry with a still-running CI check appears as BLOCKED."""
        from types import SimpleNamespace

        class FakeCi:
            is_available = True
            def list_checks_for_pr(self, repo, number):
                return [SimpleNamespace(name="build", status="in_progress", conclusion=None)]

        items = [_q("w1", size=50, pr=99)]
        save_queue(items)
        board = self._board(completed=[
            self._work("w1", test_state="passed"),
            self._review("w1", verdict="approve"),
        ])
        cfg = self._config()
        plan = mq.plan(board, cfg, ci_store=FakeCi())
        assert plan[0].status == mq.PLAN_BLOCKED
        assert "CI running" in (plan[0].reason or "")

    def test_blocked_ci_unreadable(self, coord_db) -> None:
        """#2347: a check-list FETCH failure (the #1525 synthetic "could not
        read CI status" stand-in) must appear as BLOCKED with a
        CI_UNREADABLE_PREFIX reason — distinct from both "CI failed" (a real
        red verdict) and "CI running" (a real pending verdict), since this
        is neither: GitHub could not even be asked the question."""
        from types import SimpleNamespace

        class FakeCi:
            is_available = True
            def list_checks_for_pr(self, repo, number):
                return [SimpleNamespace(
                    name="coord: could not read CI status for acme/api#99 "
                         "(HTTP 503: No server is currently available)",
                    status="completed", conclusion="unknown",
                )]

        items = [_q("w1", size=50, pr=99)]
        save_queue(items)
        board = self._board(completed=[
            self._work("w1", test_state="passed"),
            self._review("w1", verdict="approve"),
        ])
        cfg = self._config()
        plan = mq.plan(board, cfg, ci_store=FakeCi())
        assert plan[0].status == mq.PLAN_BLOCKED
        assert mq.is_ci_unreadable_reason(plan[0].reason)
        assert not mq.is_ci_pending_reason(plan[0].reason)
        assert "GitHub could not be reached" in (plan[0].reason or "")
        assert "CI failed" not in (plan[0].reason or "")

    def test_blocked_ci_unreadable_not_confused_with_gate_snapshot_stale(
        self, coord_db
    ) -> None:
        """#2347 regression guard: `coord.gate_snapshot.GateSnapshot`'s OWN
        synthetic "could not trust this snapshot" stand-in
        (``_stale_check``) also carries ``conclusion="unknown"`` and a
        ``"coord: "``-prefixed name — but names a completely different local
        condition (the daemon's own refresh loop fell behind), not a
        GitHub read failure, and must NOT be relabeled CI_UNREADABLE_PREFIX.
        `is_unreadable_check` requires the "read CI status" phrase the two
        real ci_github stand-ins share and this one doesn't — see its
        docstring."""
        from types import SimpleNamespace

        class FakeCi:
            is_available = True
            def list_checks_for_pr(self, repo, number):
                return [SimpleNamespace(
                    name="coord: gate snapshot stale (999s old, max 180s)",
                    status="completed", conclusion="unknown",
                )]

        items = [_q("w1", size=50, pr=99)]
        save_queue(items)
        board = self._board(completed=[
            self._work("w1", test_state="passed"),
            self._review("w1", verdict="approve"),
        ])
        cfg = self._config()
        plan = mq.plan(board, cfg, ci_store=FakeCi())
        assert plan[0].status == mq.PLAN_BLOCKED
        assert not mq.is_ci_unreadable_reason(plan[0].reason)
        assert "CI failed" in (plan[0].reason or "")

    def test_ready_when_conflicted_and_checks_unreadable(self, coord_db) -> None:
        """#2380: a DIRTY/CONFLICTING PR's `gh pr checks` fetch can itself
        fail (GitHub can't build a merge ref to run anything against), which
        reads identically to a transient GitHub-unreachable blip —
        CI_UNREADABLE_PREFIX. Confirmed via `check_pr_mergeable` reading
        CONFLICTING, this must NOT block on "retry the read" (which can
        never succeed for a conflicting PR) — mirror the #1877 checks-absent
        fall-through immediately above so `coord merge` reaches the real
        merge attempt and routes to the #241 conflict-fix path."""
        from types import SimpleNamespace

        class FakeCi:
            is_available = True
            def list_checks_for_pr(self, repo, number):
                return [SimpleNamespace(
                    name="coord: could not read CI status for acme/api#99 "
                         "(HTTP 503: No server is currently available)",
                    status="completed", conclusion="unknown",
                )]

        items = [_q("w1", size=50, pr=99)]
        save_queue(items)
        board = self._board(completed=[
            self._work("w1", test_state="passed"),
            self._review("w1", verdict="approve"),
        ])
        cfg = self._config()
        gh = FakeGh(mergeable_results={99: False})
        plan = mq.plan(board, cfg, ci_store=FakeCi(), gh_ops=gh)
        assert plan[0].status == mq.PLAN_READY
        assert plan[0].reason is None
        assert ("acme/api", 99) in gh.mergeable_calls

    def test_blocked_ci_unreadable_stays_blocked_when_not_confirmed_conflicted(
        self, coord_db
    ) -> None:
        """#2380 companion (acceptance criterion): an UNKNOWN mergeability
        read (still computing, or no probe at all) must keep today's
        CI_UNREADABLE_PREFIX park — this is additive, not a general
        loosening of the CI-unreadable path."""
        from types import SimpleNamespace

        class FakeCi:
            is_available = True
            def list_checks_for_pr(self, repo, number):
                return [SimpleNamespace(
                    name="coord: could not read CI status for acme/api#99 "
                         "(HTTP 503: No server is currently available)",
                    status="completed", conclusion="unknown",
                )]

        cfg = self._config()
        board = self._board(completed=[
            self._work("w1", test_state="passed"),
            self._review("w1", verdict="approve"),
        ])

        for gh_ops in (FakeGh(mergeable_results={99: True}), FakeGh(mergeable_results={99: None}), None):
            save_queue([_q("w1", size=50, pr=99)])
            plan = mq.plan(board, cfg, ci_store=FakeCi(), gh_ops=gh_ops)
            assert plan[0].status == mq.PLAN_BLOCKED
            assert mq.is_ci_unreadable_reason(plan[0].reason)

    def test_blocked_ci_absent_when_repo_declares_ci(self, coord_db) -> None:
        """#1904: an empty check list for a repo that declares CI must show
        BLOCKED with a `checks_absent`-style reason — not READY, the
        pre-#1904 read that let a PR whose CI never ran merge as green."""

        class FakeCi:
            is_available = True
            def list_checks_for_pr(self, repo, number):
                return []
            def expects_checks(self, repo, number):
                return True

        items = [_q("w1", size=50, pr=99)]
        save_queue(items)
        board = self._board(completed=[
            self._work("w1", test_state="passed"),
            self._review("w1", verdict="approve"),
        ])
        cfg = self._config()
        plan = mq.plan(board, cfg, ci_store=FakeCi())
        assert plan[0].status == mq.PLAN_BLOCKED
        assert "CI never ran" in (plan[0].reason or "")
        assert mq.is_ci_absent_reason(plan[0].reason)

    def test_ready_when_no_workflows_declared_and_checks_empty(self, coord_db) -> None:
        """Companion regression: a repo with no CI configured at all
        (`expects_checks` answers False) must still show READY for an empty
        check list — the #1904 fix must not deadlock repos that legitimately
        have no workflows."""

        class FakeCi:
            is_available = True
            def list_checks_for_pr(self, repo, number):
                return []
            def expects_checks(self, repo, number):
                return False

        items = [_q("w1", size=50, pr=99)]
        save_queue(items)
        board = self._board(completed=[
            self._work("w1", test_state="passed"),
            self._review("w1", verdict="approve"),
        ])
        cfg = self._config()
        plan = mq.plan(board, cfg, ci_store=FakeCi())
        assert plan[0].status == mq.PLAN_READY
        assert plan[0].reason is None

    def test_ready_when_conflicted_and_checks_empty(self, coord_db) -> None:
        """#1877: GitHub cannot build a merge ref for a conflicted PR, so no
        `pull_request`-triggered workflow ever runs — its check list reads
        empty for the SAME reason a genuinely-untested PR's does, but needs
        the opposite response: don't block on `CI never ran`, fall through
        so `coord merge` attempts it and routes to the #241 conflict-fix
        path. `plan()` mirrors that fall-through by reporting READY rather
        than pre-empting it with the #1904 CI-absent block."""

        class FakeCi:
            is_available = True
            def list_checks_for_pr(self, repo, number):
                return []
            def expects_checks(self, repo, number):
                return True

        items = [_q("w1", size=50, pr=99)]
        save_queue(items)
        board = self._board(completed=[
            self._work("w1", test_state="passed"),
            self._review("w1", verdict="approve"),
        ])
        cfg = self._config()
        gh = FakeGh(mergeable_results={99: False})
        plan = mq.plan(board, cfg, ci_store=FakeCi(), gh_ops=gh)
        assert plan[0].status == mq.PLAN_READY
        assert plan[0].reason is None
        assert ("acme/api", 99) in gh.mergeable_calls

    def test_blocked_ci_absent_stays_blocked_when_not_confirmed_conflicted(
        self, coord_db
    ) -> None:
        """#1877 companion: the fall-through only fires on a CONFIRMED
        conflict (`check_pr_mergeable` returns `False`). A clean PR
        (`True`), an inconclusive read (`None` — still computing, or the
        probe errored), or no `gh_ops` at all must all keep today's #1904
        `CI never ran` block — this is not a license to skip the CI gate
        whenever mergeability merely isn't known."""

        class FakeCi:
            is_available = True
            def list_checks_for_pr(self, repo, number):
                return []
            def expects_checks(self, repo, number):
                return True

        cfg = self._config()
        board = self._board(completed=[
            self._work("w1", test_state="passed"),
            self._review("w1", verdict="approve"),
        ])

        for mergeable_results, gh_ops in [
            ({99: True}, FakeGh(mergeable_results={99: True})),
            ({99: None}, FakeGh(mergeable_results={99: None})),
            (None, None),
        ]:
            save_queue([_q("w1", size=50, pr=99)])
            plan = mq.plan(board, cfg, ci_store=FakeCi(), gh_ops=gh_ops)
            assert plan[0].status == mq.PLAN_BLOCKED, mergeable_results
            assert mq.is_ci_absent_reason(plan[0].reason), mergeable_results

    def test_ci_summary_populated_from_ci_store(self, coord_db) -> None:
        """#1344: plan() attaches a structured `ci_summary` + `pr_number` so
        the TUI can render CI badges straight from `/board` instead of
        shelling out to `gh pr checks` itself."""
        from types import SimpleNamespace

        class FakeCi:
            is_available = True

            def list_checks_for_pr(self, repo, number):
                return [
                    SimpleNamespace(name="build", status="completed", conclusion="success"),
                    SimpleNamespace(name="lint", status="completed", conclusion="failure", url="http://x/lint"),
                    SimpleNamespace(name="test", status="in_progress", conclusion=None),
                ]

        items = [_q("w1", size=50, pr=99)]
        save_queue(items)
        board = self._board(completed=[
            self._work("w1", test_state="passed"),
            self._review("w1", verdict="approve"),
        ])
        cfg = self._config()
        plan = mq.plan(board, cfg, ci_store=FakeCi())
        assert plan[0].pr_number == 99
        summary = plan[0].ci_summary
        assert summary is not None
        assert summary.passed == 1
        assert summary.failed == 1
        assert summary.running == 1
        assert summary.failed_names == ["lint"]
        assert summary.first_failed_url == "http://x/lint"

    def test_ci_summary_all_shows_advisory_checks_the_gate_filters_out(
        self, coord_db,
    ) -> None:
        """#2446: `ci_summary` (the gate's own required-only view) must not
        widen just because the store also offers an unfiltered view — but
        `ci_summary_all` should carry the advisory checks the gate itself
        (`list_checks_for_pr`) filtered out, so `coord merge --plan` can
        still show a regressed advisory check without it blocking anything."""
        from types import SimpleNamespace

        class FakeCi:
            is_available = True

            def list_checks_for_pr(self, repo, number):
                # Same required-only shape `GitHubCi.list_checks_for_pr`
                # narrows to post-#2388/#2446 — the gate's own view.
                return [
                    SimpleNamespace(
                        name="test (3.12)", status="completed", conclusion="success",
                    ),
                ]

            def list_all_checks_for_pr(self, repo, number):
                return [
                    SimpleNamespace(
                        name="test (3.12)", status="completed", conclusion="success",
                    ),
                    SimpleNamespace(
                        name="Acceptance (web)", status="in_progress", conclusion=None,
                    ),
                ]

        items = [_q("w1", size=50, pr=99)]
        save_queue(items)
        board = self._board(completed=[
            self._work("w1", test_state="passed"),
            self._review("w1", verdict="approve"),
        ])
        cfg = self._config()
        plan = mq.plan(board, cfg, ci_store=FakeCi())
        # The gate's own reading: one required check, all green.
        assert plan[0].ci_summary is not None
        assert plan[0].ci_summary.passed == 1
        assert plan[0].ci_summary.running == 0
        # The visibility reading: the pending advisory check is still there.
        assert plan[0].ci_summary_all is not None
        assert plan[0].ci_summary_all.passed == 1
        assert plan[0].ci_summary_all.running == 1

    def test_ci_summary_all_falls_back_to_ci_summary_without_the_capability(
        self, coord_db,
    ) -> None:
        """A `CiStore` that predates `list_all_checks_for_pr` (#2446) must
        still populate `ci_summary_all` — with the same (already-narrowed)
        data `ci_summary` has — rather than leaving it `None` and silently
        losing the plan's CI badge."""
        from types import SimpleNamespace

        class FakeCi:
            is_available = True

            def list_checks_for_pr(self, repo, number):
                return [
                    SimpleNamespace(name="build", status="completed", conclusion="success"),
                ]

        items = [_q("w1", size=50, pr=99)]
        save_queue(items)
        board = self._board(completed=[
            self._work("w1", test_state="passed"),
            self._review("w1", verdict="approve"),
        ])
        cfg = self._config()
        plan = mq.plan(board, cfg, ci_store=FakeCi())
        assert plan[0].ci_summary is not None
        assert plan[0].ci_summary_all is not None
        assert plan[0].ci_summary_all.passed == plan[0].ci_summary.passed

    def test_ci_summary_none_without_pr_number(self, coord_db) -> None:
        """No PR yet opened → no CI summary (mirrors the CI gate's own guard)."""
        from types import SimpleNamespace

        class FakeCi:
            is_available = True

            def list_checks_for_pr(self, repo, number):
                return [SimpleNamespace(name="build", status="completed", conclusion="success")]

        items = [_q("w1", size=50)]  # pr_number=None
        save_queue(items)
        board = self._board(completed=[
            self._work("w1", test_state="passed"),
            self._review("w1", verdict="approve"),
        ])
        cfg = self._config()
        plan = mq.plan(board, cfg, ci_store=FakeCi())
        assert plan[0].pr_number is None
        assert plan[0].ci_summary is None

    def test_ci_summary_not_fetched_for_merged_entries(self, coord_db) -> None:
        """Review fix (#1344): `plan()` must not call `list_checks_for_pr` for
        non-PENDING entries when handed a *live* `CiStore` — the callers that
        pass one (`_auto_drain_tick`'s auto-drain and `coord merge --plan`,
        as opposed to the daemon's snapshot-backed `/board` read) would
        otherwise shell out to `gh pr checks` once per historical MERGED
        entry in the queue on every call, since `merge_queue` never prunes
        MERGED rows. Scoping the CI-summary computation to PENDING entries
        (matching `_entry_gate_status`'s own scope) prevents that."""
        from types import SimpleNamespace

        calls: list[tuple[str, int]] = []

        class FakeCi:
            is_available = True

            def list_checks_for_pr(self, repo, number):
                calls.append((repo, number))
                return [SimpleNamespace(name="build", status="completed", conclusion="success")]

        items = [
            _q("w1", size=50, pr=101, state=MERGED),
            _q("w2", size=50, pr=102, state=MERGING),
        ]
        save_queue(items)
        board = self._board(completed=[
            self._work("w1", test_state="passed"),
            self._review("w1", verdict="approve"),
            self._work("w2", test_state="passed"),
            self._review("w2", verdict="approve"),
        ])
        cfg = self._config()
        plan = mq.plan(board, cfg, ci_store=FakeCi())
        assert calls == []
        assert all(pm.ci_summary is None for pm in plan)

    def test_ci_not_checked_without_pr_number(self, coord_db) -> None:
        """An entry with no PR yet opened is not blocked on CI."""
        from types import SimpleNamespace

        class AlwaysFailCi:
            is_available = True
            def list_checks_for_pr(self, repo, number):
                return [SimpleNamespace(name="build", status="completed", conclusion="failure")]

        # pr=None → no pr_number
        items = [_q("w1", size=50)]  # pr_number=None by default
        save_queue(items)
        board = self._board(completed=[
            self._work("w1", test_state="passed"),
            self._review("w1", verdict="approve"),
        ])
        cfg = self._config()
        # Even with a failing CI, no pr_number → CI gate skipped → READY
        plan = mq.plan(board, cfg, ci_store=AlwaysFailCi())
        assert plan[0].status == mq.PLAN_READY

    # ── #1318: epic-closing-keyword-in-commit gate ─────────────────────────

    def test_blocked_epic_closing_keyword_in_commit(self, coord_db) -> None:
        """A commit-message closing keyword for an epic shows PLAN_BLOCKED.

        This is the plan()/process() parity gap from #1318 review: an entry
        that `process()` would refuse to merge (epic auto-close hazard) must
        also show BLOCKED in the plan the operator checks beforehand, not
        just fail silently at merge time.
        """
        items = [_q("w1", size=50, pr=100)]
        save_queue(items)
        board = self._board(completed=[
            self._work("w1", test_state="passed"),
            self._review("w1", verdict="approve"),
        ])
        cfg = self._config()
        gh = FakeGh(
            pr_commit_messages={100: ["fix(#1314): ...\n\nCloses #1120"]},
            epic_issues={1120},
        )
        plan = mq.plan(board, cfg, gh_ops=gh)
        assert plan[0].status == mq.PLAN_BLOCKED
        assert "#1120" in (plan[0].reason or "")

    def test_not_blocked_ordinary_closing_keyword_in_commit(self, coord_db) -> None:
        """An ordinary (non-epic) closing keyword in a commit stays READY."""
        items = [_q("w1", size=50, pr=100)]
        save_queue(items)
        board = self._board(completed=[
            self._work("w1", test_state="passed"),
            self._review("w1", verdict="approve"),
        ])
        cfg = self._config()
        gh = FakeGh(
            pr_commit_messages={100: ["fix(#55): a normal bug fix\n\nCloses #55"]},
            epic_issues=set(),
        )
        plan = mq.plan(board, cfg, gh_ops=gh)
        assert plan[0].status == mq.PLAN_READY

    def test_epic_commit_gate_not_checked_without_pr_number(self, coord_db) -> None:
        """No PR yet opened → the commit-message gate is skipped, not blocked."""
        items = [_q("w1", size=50)]  # pr_number=None by default
        save_queue(items)
        board = self._board(completed=[
            self._work("w1", test_state="passed"),
            self._review("w1", verdict="approve"),
        ])
        cfg = self._config()
        gh = FakeGh(
            pr_commit_messages={100: ["Closes #1120"]},
            epic_issues={1120},
        )
        plan = mq.plan(board, cfg, gh_ops=gh)
        assert plan[0].status == mq.PLAN_READY

    def test_epic_commit_gate_skipped_without_gh_ops(self, coord_db) -> None:
        """Without gh_ops, the commit-message gate is skipped (backward compat)."""
        items = [_q("w1", size=50, pr=100)]
        save_queue(items)
        board = self._board(completed=[
            self._work("w1", test_state="passed"),
            self._review("w1", verdict="approve"),
        ])
        cfg = self._config()
        plan = mq.plan(board, cfg)
        assert plan[0].status == mq.PLAN_READY

    def test_epic_commit_gate_lint_failure_never_blocks_plan(self, coord_db) -> None:
        """A get_pr_commit_messages/is_epic_issue failure fails open, not blocked."""
        class _BoomOnCommits(FakeGh):
            def get_pr_commit_messages(self, repo: str, number: int) -> list[str]:
                raise RuntimeError("gh pr view --json commits failed")

        items = [_q("w1", size=50, pr=100)]
        save_queue(items)
        board = self._board(completed=[
            self._work("w1", test_state="passed"),
            self._review("w1", verdict="approve"),
        ])
        cfg = self._config()
        plan = mq.plan(board, cfg, gh_ops=_BoomOnCommits())
        assert plan[0].status == mq.PLAN_READY

    # ── non-PENDING state mapping ─────────────────────────────────────────

    def test_merging_entry_status(self, coord_db) -> None:
        items = [_q("w1", state=mq.MERGING)]
        save_queue(items)
        board = self._board()
        cfg = self._config(review_enabled=False, gates=["merge"])
        plan = mq.plan(board, cfg)
        assert plan[0].status == mq.PLAN_MERGING

    def test_merged_entry_status(self, coord_db) -> None:
        items = [_q("w1", state=mq.MERGED)]
        save_queue(items)
        board = self._board()
        cfg = self._config(review_enabled=False, gates=["merge"])
        plan = mq.plan(board, cfg)
        assert plan[0].status == mq.PLAN_MERGED

    def test_conflict_entry_status(self, coord_db) -> None:
        items = [_q("w1", state=mq.CONFLICT)]
        save_queue(items)
        board = self._board()
        cfg = self._config(review_enabled=False, gates=["merge"])
        plan = mq.plan(board, cfg)
        assert plan[0].status == mq.PLAN_NEEDS_ATTENTION

    # ── metadata fields ───────────────────────────────────────────────────

    def test_target_branch_is_populated(self, coord_db) -> None:
        items = [_q("w1", target="develop")]
        save_queue(items)
        cfg = self._config(review_enabled=False, gates=["merge"])
        board = self._board()
        plan = mq.plan(board, cfg)
        assert plan[0].target_branch == "develop"

    def test_enqueued_at_propagated(self, coord_db) -> None:
        import time as _time
        ts = _time.time() - 60.0
        q = QueuedMerge(
            assignment_id="w1", repo_name="api", repo_github="acme/api",
            branch="issue-1-w1", target_branch="main",
            issue_number=1, issue_title="t",
            enqueued_at=ts,
        )
        save_queue([q])
        cfg = self._config(review_enabled=False, gates=["merge"])
        board = self._board()
        plan = mq.plan(board, cfg)
        assert plan[0].enqueued_at == pytest.approx(ts, abs=1.0)

    def test_milestone_from_issues_table(self, coord_db) -> None:
        """Milestone title is pulled from the issues table when present."""
        from coord.db import get_connection
        conn = get_connection()
        backends.upsert_issue(
            conn,
            repo_name="api",
            number=1,
            title="t",
            body="",
            state="open",
            labels="[]",
            milestone_title="v1.0",
        )
        conn.commit()

        items = [_q("w1")]
        save_queue(items)
        cfg = self._config(review_enabled=False, gates=["merge"])
        board = self._board()
        plan = mq.plan(board, cfg)
        assert plan[0].milestone == "v1.0"

    def test_milestone_none_when_not_in_issues_table(self, coord_db) -> None:
        items = [_q("w1")]
        save_queue(items)
        cfg = self._config(review_enabled=False, gates=["merge"])
        board = self._board()
        plan = mq.plan(board, cfg)
        assert plan[0].milestone is None

    # ── empty queue ───────────────────────────────────────────────────────

    def test_empty_queue_returns_empty_list(self, coord_db) -> None:
        cfg = self._config()
        board = self._board()
        plan = mq.plan(board, cfg)
        assert plan == []

    # ── gate_status helper (unit test for _entry_gate_status) ─────────────

    def test_entry_gate_status_ready(self, coord_db) -> None:
        """All gates pass → READY."""
        board = self._board(completed=[
            self._work("w1", test_state="passed"),
            self._review("w1", verdict="approve"),
        ])
        entry = _q("w1")
        cfg = self._config()
        status, reason = mq._entry_gate_status(entry, board, cfg)
        assert status == mq.PLAN_READY
        assert reason is None

    def test_entry_gate_status_public_wrapper_delegates_identically(
        self, coord_db
    ) -> None:
        """#2182: `entry_gate_status` is the sanctioned cross-module seam onto
        `_entry_gate_status` — `coord.commands.drive_queue._fetch_live_ci_gate`
        calls it directly (a single-entry, bounded-cost re-check, unlike the
        whole-queue `plan()`), so it must answer byte-for-byte what the
        private function does, for both a READY and a BLOCKED verdict."""
        board = self._board(completed=[
            self._work("w1", test_state="passed"),
            self._review("w1", verdict="approve"),
        ])
        entry = _q("w1")
        cfg = self._config()
        assert mq.entry_gate_status(entry, board, cfg) == mq._entry_gate_status(
            entry, board, cfg
        )

        blocked_board = self._board(completed=[self._work("w1", test_state="passed")])
        assert mq.entry_gate_status(
            entry, blocked_board, cfg
        ) == mq._entry_gate_status(entry, blocked_board, cfg)
        status, reason = mq.entry_gate_status(entry, blocked_board, cfg)
        assert status == mq.PLAN_BLOCKED
        assert reason is not None

    def test_entry_gate_status_no_config_returns_ready(self) -> None:
        """Without config/board, gate evaluation is skipped → READY."""
        entry = _q("w1")
        status, reason = mq._entry_gate_status(entry, None, None)
        assert status == mq.PLAN_READY
        assert reason is None

    def test_entry_gate_status_blocked_epic_closing_keyword_in_commit(self) -> None:
        """#1318: a commit-message epic closing keyword → PLAN_BLOCKED."""
        entry = _q("w1", pr=100)
        gh = FakeGh(
            pr_commit_messages={100: ["Closes #1120"]},
            epic_issues={1120},
        )
        status, reason = mq._entry_gate_status(entry, None, None, gh_ops=gh)
        assert status == mq.PLAN_BLOCKED
        assert reason is not None and "#1120" in reason


# ── #1851: CI results staled by base movement ────────────────────────────────

class TestCiChecksAreStale:
    """Direct unit coverage of `_ci_checks_are_stale`, independent of the
    `plan()`/`process()` plumbing around it."""

    @staticmethod
    def _checks(started_at: float | None = 1500.0):
        from coord.ci_store import CheckRun
        return [CheckRun(
            name="build", status="completed", conclusion="success",
            url="", run_id="1", started_at=started_at, completed_at=None,
        )]

    class _Gh:
        def __init__(self, ts: float | None = 1000.0):
            self.ts = ts
        def get_branch_commit_timestamp(self, repo: str, branch: str) -> float | None:
            return self.ts

    def test_fresh_when_checks_postdate_base(self) -> None:
        checks = self._checks(started_at=1500.0)
        assert mq._ci_checks_are_stale(checks, self._Gh(1000.0), "acme/api", "main", None) is False

    def test_stale_when_checks_predate_base(self) -> None:
        checks = self._checks(started_at=500.0)
        assert mq._ci_checks_are_stale(checks, self._Gh(1000.0), "acme/api", "main", None) is True

    def test_fails_closed_without_gh_ops(self) -> None:
        checks = self._checks(started_at=1500.0)
        assert mq._ci_checks_are_stale(checks, None, "acme/api", "main", None) is True

    def test_fails_closed_without_target_branch(self) -> None:
        checks = self._checks(started_at=1500.0)
        assert mq._ci_checks_are_stale(checks, self._Gh(1000.0), "acme/api", None, None) is True

    def test_fails_closed_when_gh_ops_lacks_the_capability(self) -> None:
        """A gh_ops stand-in with no `get_branch_commit_timestamp` at all
        must degrade to stale, matching `_fetch_compare_files`'s documented
        AttributeError-fails-closed posture for the same reason. Before
        #1998, `coord.gate_snapshot.GateSnapshot` was exactly such a
        stand-in — see `test_gate_snapshot_serves_a_real_commit_timestamp`
        below for the regression coverage on that specific object."""
        class _NoTimestampGh:
            pass
        checks = self._checks(started_at=1500.0)
        assert mq._ci_checks_are_stale(checks, _NoTimestampGh(), "acme/api", "main", None) is True

    def test_gate_snapshot_serves_a_real_commit_timestamp(self) -> None:
        """#1998: `GateSnapshot` (the gh_ops stand-in `coord.serve_app`'s
        `/board` build hands to `plan()`) used to have no
        `get_branch_commit_timestamp` at all, so every green, non-pending CI
        check served through `/board` read as unconditionally stale —
        regardless of how fresh it actually was. `coord merge --plan`,
        served from `/board`, disagreed with the live gate (`coord merge
        --dry-run`/`--only`, which builds its own live `github_ops`) for
        every single entry whose checks had cleared "pending", exactly the
        #1640 "two readers, one truth" split repeated for a newer accessor.
        Once the snapshot actually caches a timestamp, the SAME
        `_ci_checks_are_stale` call the live gate makes must reach the same
        verdict from either object."""
        from coord.gate_snapshot import GateSnapshot

        snap = GateSnapshot(
            branch_commit_timestamps={("acme/api", "main"): 1000.0},
        )
        checks_fresh = self._checks(started_at=1500.0)
        assert mq._ci_checks_are_stale(checks_fresh, snap, "acme/api", "main", None) is False
        checks_stale = self._checks(started_at=500.0)
        assert mq._ci_checks_are_stale(checks_stale, snap, "acme/api", "main", None) is True

    def test_gate_snapshot_fails_closed_for_an_uncached_branch(self) -> None:
        """A `GateSnapshot` that has never resolved this (repo, branch) pair
        — never refreshed, or the underlying `gh api` lookup failed — still
        reads `None` (via dict.get, no AttributeError) and the consumer's own
        fail-closed posture still applies. This is the fail-*open caching*/
        fail-*closed verdict* split `branch_commit_timestamps`'s own
        docstring describes."""
        from coord.gate_snapshot import GateSnapshot

        snap = GateSnapshot()
        checks = self._checks(started_at=1500.0)
        assert mq._ci_checks_are_stale(checks, snap, "acme/api", "main", None) is True

    def test_fails_closed_when_lookup_raises(self) -> None:
        class _RaisingGh:
            def get_branch_commit_timestamp(self, repo, branch):
                raise RuntimeError("gh api boom")
        checks = self._checks(started_at=1500.0)
        assert mq._ci_checks_are_stale(checks, _RaisingGh(), "acme/api", "main", None) is True

    def test_smoke_spared_reason_short_circuits_to_fresh(self) -> None:
        """#1851: when the smoke gate already proved this base move inert/
        disjoint for the SAME entry, CI staleness is spared too — without
        even reading a timestamp (the fake gh_ops below has none)."""
        class _NoTimestampGh:
            pass
        spared = mq.SmokeVerdictStatus(
            ok=True, kind=mq.SMOKE_OK,
            spared_reason="base move and branch touch disjoint files (#1847)",
        )
        checks = self._checks(started_at=500.0)  # would be stale on timestamps alone
        assert mq._ci_checks_are_stale(
            checks, _NoTimestampGh(), "acme/api", "main", spared
        ) is False

    def test_smoke_ok_without_spared_reason_falls_back_to_timestamp(self) -> None:
        """smoke.ok=True but spared_reason=None means the base never moved at
        all for the smoke check — that tells us nothing about CI, so this
        still falls back to the timestamp comparison."""
        smoke = mq.SmokeVerdictStatus(ok=True, kind=mq.SMOKE_OK, spared_reason=None)
        checks = self._checks(started_at=500.0)
        assert mq._ci_checks_are_stale(
            checks, self._Gh(1000.0), "acme/api", "main", smoke
        ) is True


class TestPlanCiStaleness:
    """#1851 acceptance: `plan()`/`_entry_gate_status()` reads a stale-but-
    green CI result distinctly from "CI failed"/"CI running"."""

    @staticmethod
    def _config():
        return TestPlan._config(review_enabled=False, gates=["merge"])

    @staticmethod
    def _board():
        from coord.models import Board
        return Board(active=[], completed=[])

    @staticmethod
    def _ci(started_at: float | None):
        from types import SimpleNamespace

        class _Ci:
            is_available = True
            def list_checks_for_pr(self, repo, number):
                return [SimpleNamespace(
                    name="build", status="completed", conclusion="success",
                    started_at=started_at, completed_at=None,
                )]
        return _Ci()

    @staticmethod
    def _gh(ts: float | None):
        class _Gh:
            def get_branch_commit_timestamp(self, repo, branch):
                return ts
        return _Gh()

    def test_blocked_ci_stale_distinctly_from_failed_and_running(self, coord_db) -> None:
        items = [_q("w1", size=50, pr=99)]
        save_queue(items)
        plan = mq.plan(
            self._board(), self._config(),
            ci_store=self._ci(started_at=500.0), gh_ops=self._gh(1000.0),
        )
        assert plan[0].status == mq.PLAN_BLOCKED
        assert plan[0].reason is not None
        assert plan[0].reason.startswith(mq.CI_STALE_PREFIX)
        assert "CI failed" not in plan[0].reason
        assert "CI running" not in plan[0].reason

    def test_ready_when_checks_postdate_base(self, coord_db) -> None:
        items = [_q("w1", size=50, pr=99)]
        save_queue(items)
        plan = mq.plan(
            self._board(), self._config(),
            ci_store=self._ci(started_at=1500.0), gh_ops=self._gh(1000.0),
        )
        assert plan[0].status == mq.PLAN_READY
        assert plan[0].reason is None

    def test_blocked_ci_failed_is_not_reported_as_stale(self, coord_db) -> None:
        """Failed checks take precedence — never reported as stale."""
        from types import SimpleNamespace

        class _Ci:
            is_available = True
            def list_checks_for_pr(self, repo, number):
                return [SimpleNamespace(
                    name="build", status="completed", conclusion="failure",
                    started_at=500.0, completed_at=None,
                )]

        items = [_q("w1", size=50, pr=99)]
        save_queue(items)
        plan = mq.plan(self._board(), self._config(), ci_store=_Ci(), gh_ops=self._gh(1000.0))
        assert plan[0].status == mq.PLAN_BLOCKED
        assert "CI failed" in (plan[0].reason or "")
        assert not (plan[0].reason or "").startswith(mq.CI_STALE_PREFIX)


class TestCiRevalidationCandidates:
    """#1851: the eligibility policy for `coord merge --revalidate`'s CI
    re-run arm — the CI analogue of `revalidation_candidates`."""

    @staticmethod
    def _board():
        from coord.models import Board
        return Board(active=[], completed=[])

    @staticmethod
    def _ci(started_at: float | None, *, available: bool = True):
        from types import SimpleNamespace

        class _Ci:
            is_available = available
            def list_checks_for_pr(self, repo, number):
                return [SimpleNamespace(
                    name="build", status="completed", conclusion="success",
                    started_at=started_at, completed_at=None,
                )]
        return _Ci()

    @staticmethod
    def _gh(ts: float | None):
        class _Gh:
            def get_branch_commit_timestamp(self, repo, branch):
                return ts
        return _Gh()

    @staticmethod
    def _cfg():
        return TestPlan._config(review_enabled=False, gates=["merge"])

    def test_candidate_blocked_solely_on_ci_staleness(self) -> None:
        entry = _q("w1", size=50, pr=99)
        out = mq.ci_revalidation_candidates(
            [entry], self._board(), self._cfg(),
            self._ci(started_at=500.0), self._gh(1000.0),
        )
        assert out == [entry]

    def test_no_candidates_when_fresh(self) -> None:
        entry = _q("w1", size=50, pr=99)
        out = mq.ci_revalidation_candidates(
            [entry], self._board(), self._cfg(),
            self._ci(started_at=1500.0), self._gh(1000.0),
        )
        assert out == []

    def test_no_candidates_without_pr_number(self) -> None:
        entry = _q("w1", size=50)  # pr_number=None
        out = mq.ci_revalidation_candidates(
            [entry], self._board(), self._cfg(),
            self._ci(started_at=500.0), self._gh(1000.0),
        )
        assert out == []

    def test_no_candidates_when_ci_store_unavailable(self) -> None:
        entry = _q("w1", size=50, pr=99)
        out = mq.ci_revalidation_candidates(
            [entry], self._board(), self._cfg(),
            self._ci(started_at=500.0, available=False), self._gh(1000.0),
        )
        assert out == []

    def test_no_candidates_when_ci_store_is_none(self) -> None:
        entry = _q("w1", size=50, pr=99)
        out = mq.ci_revalidation_candidates([entry], self._board(), self._cfg(), None, self._gh(1000.0))
        assert out == []

    def test_no_candidates_when_also_blocked_on_review(self) -> None:
        """Blocked on review AND stale CI → not offered a CI re-run, mirroring
        `revalidation_candidates`'s "blocked solely on..." policy."""
        cfg = TestPlan._config(review_enabled=True, gates=["review", "merge"])
        entry = _q("w1", size=50, pr=99)
        out = mq.ci_revalidation_candidates(
            [entry], self._board(), cfg,
            self._ci(started_at=500.0), self._gh(1000.0),
        )
        assert out == []

    def test_no_candidates_for_non_pending_entries(self) -> None:
        entry = _q("w1", size=50, pr=99, state=MERGED)
        out = mq.ci_revalidation_candidates(
            [entry], self._board(), self._cfg(),
            self._ci(started_at=500.0), self._gh(1000.0),
        )
        assert out == []


class TestProcessCiStaleness:
    """#1851: `process()` refuses to merge a green-but-CI-stale entry, in
    both the live path and the `--dry-run` preview — named distinctly
    (`checks_stale`) from `checks_failed`/`checks_pending`.

    #2197: the live path no longer blocks on the FIRST stale reading — it
    auto-reruns CI first (mirroring #1892's verdictless-failure arm, same
    `CiStore.rerun_for_pr` call), parking as `checks_stale_rerun`
    (`CI_PENDING_PREFIX` wording, so `coord drive`'s #1891 "wait, don't
    spend an attempt" logic applies) up to `MAX_CI_STALE_RERUNS` times.
    Only once that budget is exhausted does it report the terminal
    `checks_stale` block a human has to act on. `--dry-run` is unaffected —
    it only ever previews, never mutates, so it keeps reporting
    `checks_stale` on the very first pass and never calls `rerun_for_pr`.
    """

    @staticmethod
    def _ci(started_at: float | None, *, rerun_ok: bool = True):
        from types import SimpleNamespace

        class _Ci:
            is_available = True

            def __init__(self):
                self.rerun_calls: list = []
                # Mutable — a test can flip this between two `process()`
                # calls to simulate the re-run GitHub was asked to trigger
                # reporting back, the same way a real CiStore's backing
                # check-run data would change between two live reads.
                self.checks = [SimpleNamespace(
                    name="build", status="completed", conclusion="success",
                    started_at=started_at, completed_at=None,
                )]

            def list_checks_for_pr(self, repo, number):
                return self.checks

            def rerun_for_pr(self, repo, number):
                self.rerun_calls.append((repo, number))
                return rerun_ok
        return _Ci()

    class _Gh(FakeGh):
        ts: float = 1000.0
        def get_branch_commit_timestamp(self, repo, branch):
            return self.ts

    def test_first_stale_reading_auto_reruns_instead_of_blocking(self) -> None:
        """The exact #2170 regression: a docs-only base move stales an
        otherwise-green PR. The first live pass must not escalate — it
        triggers a CI re-run and parks, unattended."""
        items = [_q("w1", pr=99)]
        gh = self._Gh()
        ci = self._ci(started_at=500.0)
        events = process(items, gh, ci_store=ci)
        assert items[0].state == PENDING
        assert gh.merge_calls == []
        assert ci.rerun_calls == [("acme/api", 99)]
        assert items[0].ci_stale_reruns == 1
        kinds = [e.kind for e in events]
        assert "checks_stale_rerun" in kinds
        assert "checks_stale" not in kinds
        assert "checks_failed" not in kinds
        assert "checks_pending" not in kinds
        # #1891: CI_PENDING_PREFIX wording — `coord drive` waits rather
        # than spending a merge attempt on this.
        assert items[0].error.startswith("CI running:")

    def test_merges_on_a_later_pass_once_the_rerun_reports_green(self) -> None:
        """Full #2170 lifecycle, end to end: stale → auto-rerun → a LATER
        `process()` tick (the re-run having reported back fresh and green,
        no operator involved) actually merges it. Acceptance criterion,
        verbatim from #2197: "process() triggers a CI re-run and the entry
        parks as checks_pending without spending an attempt, then merges on
        a later pass once green.\""""
        items = [_q("w1", pr=99)]
        gh = self._Gh()
        ci = self._ci(started_at=500.0)

        first = process(items, gh, ci_store=ci)
        assert items[0].state == PENDING
        assert "checks_stale_rerun" in [e.kind for e in first]
        assert ci.rerun_calls == [("acme/api", 99)]

        # The re-run GitHub was asked to trigger has now reported back: a
        # fresh, green check — the same `ci` object, no new `coord merge`
        # flag involved.
        from types import SimpleNamespace
        ci.checks = [SimpleNamespace(
            name="build", status="completed", conclusion="success",
            started_at=1500.0, completed_at=None,
        )]

        second = process(items, gh, ci_store=ci)
        assert items[0].state == MERGED
        assert ci.rerun_calls == [("acme/api", 99)]  # unchanged — no 2nd rerun
        kinds = [e.kind for e in second]
        assert "merged" in kinds
        assert "checks_stale" not in kinds
        assert "checks_stale_rerun" not in kinds

    def test_reruns_stop_at_the_cap_and_then_reports_checks_stale(self) -> None:
        from coord.merge_queue import MAX_CI_STALE_RERUNS

        items = [_q("w1", pr=99)]
        gh = self._Gh()
        ci = self._ci(started_at=500.0)
        for expected in range(1, MAX_CI_STALE_RERUNS + 1):
            events = process(items, gh, ci_store=ci)
            assert items[0].ci_stale_reruns == expected
            assert "checks_stale_rerun" in [e.kind for e in events]
        assert len(ci.rerun_calls) == MAX_CI_STALE_RERUNS

        # Budget exhausted — the next pass reports the terminal block and
        # triggers no further rerun.
        events = process(items, gh, ci_store=ci)
        assert len(ci.rerun_calls) == MAX_CI_STALE_RERUNS  # unchanged
        assert items[0].ci_stale_reruns == MAX_CI_STALE_RERUNS  # unchanged
        kinds = [e.kind for e in events]
        assert "checks_stale" in kinds
        assert "checks_stale_rerun" not in kinds
        assert items[0].state == PENDING

    def test_live_merge_proceeds_when_checks_fresh(self) -> None:
        items = [_q("w1", pr=99)]
        gh = self._Gh()
        ci = self._ci(started_at=1500.0)
        events = process(items, gh, ci_store=ci)
        assert items[0].state == MERGED
        assert ci.rerun_calls == []
        kinds = [e.kind for e in events]
        assert "checks_stale" not in kinds
        assert "checks_stale_rerun" not in kinds

    def test_resets_after_a_clean_pass_so_a_later_staleness_starts_fresh(self) -> None:
        """Mirrors `ci_infra_reruns`'s own reset test (#1892): a later,
        unrelated base move must not inherit a budget already spent on an
        earlier staleness streak."""
        from coord.merge_queue import MAX_CI_STALE_RERUNS

        items = [_q("w1", pr=99)]
        items[0].ci_stale_reruns = MAX_CI_STALE_RERUNS
        gh = self._Gh()
        ci = self._ci(started_at=1500.0)  # fresh — resolves the old streak
        process(items, gh, ci_store=ci)
        assert items[0].state == MERGED
        assert items[0].ci_stale_reruns == 0

    def test_force_merge_overrides_ci_staleness(self) -> None:
        items = [_q("w1", pr=99)]
        gh = self._Gh()
        ci = self._ci(started_at=500.0)
        process(items, gh, ci_store=ci, force_merge=True)
        assert items[0].state == MERGED
        assert ci.rerun_calls == []  # force_merge skips the CI gate entirely

    def test_force_merge_says_out_loud_that_stale_ci_is_being_waived(self) -> None:
        """#1826 acceptance: "--force-merge still overrides, and the override
        says explicitly that stale CI is being waived."

        The override stays an override — the merge happens, no re-run is
        triggered — but it is no longer SILENT. Before this, a forced merge
        over stale CI emitted no CI event at all, which reads in the audit
        trail exactly like "merged because CI was green"."""
        items = [_q("w1", pr=99)]
        gh = self._Gh()
        ci = self._ci(started_at=500.0)
        events = process(items, gh, ci_store=ci, force_merge=True)
        assert items[0].state == MERGED
        assert ci.rerun_calls == []
        forced = [e for e in events if e.kind == "checks_stale_forced"]
        assert forced, [e.kind for e in events]
        assert forced[0].message.startswith(mq.CI_STALE_WAIVED_PREFIX)
        assert "waived" in forced[0].message.lower()
        assert "--force-merge" in forced[0].message
        # ...but a merge that HAPPENED is not a gate refusal: this event must
        # not be counted as one by the #1896 forge-availability recorder.
        from coord.forge_availability import MERGE_GATE_REFUSAL_KINDS
        assert "checks_stale_forced" not in MERGE_GATE_REFUSAL_KINDS

    def test_force_merge_is_quiet_when_the_checks_are_fresh(self) -> None:
        """The waiver notice is about STALE CI specifically — a forced merge
        over green, current checks must not cry wolf."""
        items = [_q("w1", pr=99)]
        gh = self._Gh()
        ci = self._ci(started_at=1500.0)
        events = process(items, gh, ci_store=ci, force_merge=True)
        assert items[0].state == MERGED
        assert "checks_stale_forced" not in [e.kind for e in events]

    def test_force_merge_is_quiet_when_the_base_anchor_is_unreadable(self) -> None:
        """The GATE fails closed on an unreadable base timestamp (a false
        "stale" only costs a re-run). The WAIVER NOTICE must not — it is
        advisory prose beside a merge that is happening either way, and
        inventing "stale CI is being waived" for a PR whose checks may well
        be current is noise, not safety."""
        class _NoTimestampGh(FakeGh):
            pass

        items = [_q("w1", pr=99)]
        gh = _NoTimestampGh()
        ci = self._ci(started_at=500.0)
        events = process(items, gh, ci_store=ci, force_merge=True)
        assert items[0].state == MERGED
        assert "checks_stale_forced" not in [e.kind for e in events]

    def test_force_merge_never_blocks_on_a_ci_store_that_raises(self) -> None:
        """The waiver notice is best-effort: an unreadable CiStore costs the
        message, never the merge."""
        class _BoomCi:
            is_available = True
            rerun_calls: list = []

            def list_checks_for_pr(self, repo, number):
                raise RuntimeError("gh pr checks exploded")

        items = [_q("w1", pr=99)]
        events = process(items, self._Gh(), ci_store=_BoomCi(), force_merge=True)
        assert items[0].state == MERGED
        assert "checks_stale_forced" not in [e.kind for e in events]

    def test_dry_run_force_merge_previews_the_waiver(self) -> None:
        """#1826: a `--dry-run --force-merge` that just says "would merge"
        hides the single most consequential fact about the merge it previews."""
        items = [_q("w1", pr=99)]
        ci = self._ci(started_at=500.0)
        events = process(
            items, self._Gh(), ci_store=ci, force_merge=True, dry_run=True
        )
        merged = [e for e in events if e.kind == "merged"]
        assert merged
        assert mq.CI_STALE_WAIVED_PREFIX in merged[0].message

    def test_stale_reason_names_both_anchors_like_1479(self) -> None:
        """#1826 acceptance: STALE CI is reported "in wording that matches
        #1479's Test-verdict staleness" — which names what the verdict was
        recorded against AND what the anchor is now (`coord.gates`'
        "recorded against base X, base is now Y"). The CI anchor is a run
        timestamp rather than a SHA, but the sentence is the same."""
        items = [_q("w1", pr=99)]
        gh = self._Gh()
        ci = self._ci(started_at=500.0)
        items[0].ci_stale_reruns = mq.MAX_CI_STALE_RERUNS  # skip to the block
        events = process(items, gh, ci_store=ci)
        stale = [e for e in events if e.kind == "checks_stale"]
        assert stale
        msg = stale[0].message
        assert msg.startswith(mq.CI_STALE_PREFIX)
        assert "ran against main as of 1970-01-01T00:08:20Z" in msg
        assert "main now 1970-01-01T00:16:40Z" in msg
        # The remedy stays the last thing an operator reads (#1826).
        assert msg.endswith("re-run CI (`coord merge --revalidate`) before merging")

    def test_dry_run_previews_checks_stale_without_rerunning(self) -> None:
        items = [_q("w1", pr=99)]
        gh = self._Gh()
        ci = self._ci(started_at=500.0)
        events = process(items, gh, ci_store=ci, dry_run=True)
        assert items[0].state == PENDING
        assert gh.merge_calls == []
        assert ci.rerun_calls == []
        assert items[0].ci_stale_reruns == 0
        kinds = [e.kind for e in events]
        assert "checks_stale" in kinds
        assert "merged" not in kinds


class TestTwoGreenBranchesOneBaseMove:
    """#1826 regression, the 2026-08-04 incident replayed literally.

    17:50 #1798 merges, `main` green with it included. 18:02 #1796 merges on
    PR checks that were green against a base predating #1798. Both branches
    were individually correct; their combination was not, and `main` was red
    for two hours — every branch cut from it inherited failing CI, so nothing
    could merge and a release was structurally impossible.

    The sequence, exactly: A and B are both green against base X; A merges,
    making the base Y; B must NOT merge on its X-based checks. It doesn't
    matter for this test *which* of the two non-merging outcomes B lands in
    (#2197's unattended auto-rerun, or the terminal block once that budget is
    spent) — what #1826 is about is that B does not reach `gh pr merge` on
    evidence that predates the base it would be merging into.
    """

    @staticmethod
    def _ci(started_at: float):
        """One green check per PR, all started at *started_at* — i.e. both
        branches' CI ran against base X, before A landed."""
        from types import SimpleNamespace

        class _Ci:
            is_available = True

            def __init__(self):
                self.rerun_calls: list = []

            def list_checks_for_pr(self, repo, number):
                return [SimpleNamespace(
                    name="build", status="completed", conclusion="success",
                    started_at=started_at, completed_at=None,
                )]

            def rerun_for_pr(self, repo, number):
                self.rerun_calls.append((repo, number))
                return True
        return _Ci()

    class _MovingBaseGh(FakeGh):
        """`main`'s tip commit time, which advances the moment a merge lands
        — the base move that stales every other in-flight branch's checks."""

        BASE_X = 1000.0
        BASE_Y = 2000.0

        def get_branch_commit_timestamp(self, repo, branch):
            return self.BASE_Y if self.merge_calls else self.BASE_X

    def _items(self):
        # size drives merge order (`sequence`): A first, then B.
        return [
            _q("A", pr=101, branch="issue-1798-gate", size=10),
            _q("B", pr=102, branch="issue-1796-fixtures", size=20),
        ]

    def test_the_second_branch_does_not_merge_on_pre_move_checks(self) -> None:
        items = self._items()
        gh = self._MovingBaseGh()
        ci = self._ci(started_at=1500.0)  # green against base X (1000), not Y (2000)

        events = process(items, gh, ci_store=ci)

        a, b = items
        # A was fresh against X and merged — the common case keeps working.
        assert a.state == MERGED
        # B did NOT merge, and never reached `gh pr merge` at all.
        assert b.state == PENDING
        assert [c[1] for c in gh.merge_calls] == [101]
        # ...and it is B's CI that is named, not its review/test gates.
        b_events = [e for e in events if e.entry.assignment_id == "B"]
        assert [e.kind for e in b_events] == ["checks_stale_rerun"]
        assert "predate the current base" in (b.error or "")

    def test_b_blocks_terminally_once_the_rerun_budget_is_spent(self) -> None:
        """Same sequence, B having already spent #2197's unattended re-runs:
        the refusal is terminal, names STALE CI, and carries the remedy."""
        items = self._items()
        items[1].ci_stale_reruns = mq.MAX_CI_STALE_RERUNS
        gh = self._MovingBaseGh()
        ci = self._ci(started_at=1500.0)

        events = process(items, gh, ci_store=ci)

        assert items[0].state == MERGED
        assert items[1].state == PENDING
        assert [c[1] for c in gh.merge_calls] == [101]
        assert ci.rerun_calls == []  # budget spent — no more unattended re-runs
        stale = [
            e for e in events
            if e.entry.assignment_id == "B" and e.kind == "checks_stale"
        ]
        assert stale
        assert stale[0].message.startswith(mq.CI_STALE_PREFIX)
        # #1826: an actionable remedy, not just a refusal.
        assert "coord merge --revalidate" in stale[0].message

    def test_plan_reports_the_same_block_for_b(self, coord_db) -> None:
        """The board/plan render must agree with the live attempt — an
        operator reading `coord merge --plan` after A landed sees B blocked
        on STALE CI, not "ready"."""
        from coord.models import Board

        items = self._items()
        items[0].state = MERGED
        save_queue(items)
        gh = self._MovingBaseGh()
        gh.merge_calls.append(("acme/api", 101, "rebase"))  # A has landed

        plan = mq.plan(
            Board(active=[], completed=[]),
            TestPlan._config(review_enabled=False, gates=["merge"]),
            ci_store=self._ci(started_at=1500.0), gh_ops=gh,
        )
        rows = [p for p in plan if p.assignment_id == "B"]
        assert rows and rows[0].status == mq.PLAN_BLOCKED
        assert (rows[0].reason or "").startswith(mq.CI_STALE_PREFIX)

    def test_a_alone_still_merges_with_no_extra_round_trip(self) -> None:
        """#1826: "Checks that ran against the current base tip still merge
        with no extra round trip — this must not add latency to the common
        case." The #1479-parity anchor prose costs a second timestamp read;
        it must only be paid on the already-blocked stale path."""
        items = [_q("A", pr=101, size=10)]

        class _CountingGh(TestTwoGreenBranchesOneBaseMove._MovingBaseGh):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.ts_calls = 0

            def get_branch_commit_timestamp(self, repo, branch):
                self.ts_calls += 1
                return super().get_branch_commit_timestamp(repo, branch)

        gh = _CountingGh()
        process(items, gh, ci_store=self._ci(started_at=1500.0))
        assert items[0].state == MERGED
        assert gh.ts_calls == 1  # the gate's own read, and nothing more


class TestProcessConflictedEmptyChecks:
    """#1877: a conflicted PR reports zero CI checks — GitHub can't build a
    merge ref to run a `pull_request`-triggered workflow against it. Before
    this fix, that empty check list was indistinguishable from #1904's "CI
    never ran on a mergeable PR" and blocked the merge — pre-empting the
    #241 conflict-fix rebase that would have resolved it (the live incident
    behind this issue, claude-coordinator#1845, lost both of its queue
    retries to exactly this). `process()` must consult `check_pr_mergeable`
    and, for a CONFIRMED conflict only, fall through to the real merge
    attempt so the resulting `gh pr merge` failure routes through the
    existing `conflict` event / classify-and-dispatch machinery."""

    @staticmethod
    def _ci_no_checks():
        class _Ci:
            is_available = True
            def list_checks_for_pr(self, repo, number):
                return []
            def expects_checks(self, repo, number):
                return True
        return _Ci()

    def test_confirmed_conflict_falls_through_to_conflict_event(self) -> None:
        items = [_q("w1", pr=99)]
        gh = FakeGh(
            mergeable_results={99: False},
            merge_results={99: (False, "Pull Request is not mergeable")},
        )
        events = process(items, gh, ci_store=self._ci_no_checks())

        assert gh.mergeable_calls == [("acme/api", 99)]
        assert gh.merge_calls, "expected the real merge attempt to run"
        kinds = [e.kind for e in events]
        assert "conflict" in kinds
        assert "checks_absent" not in kinds
        assert items[0].state == CONFLICT
        # The real `gh pr merge` error routes through the SAME classifier
        # #241 already uses — this fix only changes reachability, not the
        # classify-and-dispatch machinery itself.
        assert mq.classify_conflict(items[0].error) == "rebaseable"

    def test_unconfirmed_mergeability_still_blocks_as_checks_absent(self) -> None:
        """Companion (acceptance criterion): a clean PR (`True`) or an
        inconclusive read (`None` — still computing, or the probe errored)
        must NOT fall through — only a confirmed `False` does. The #1904
        gate stays intact for the genuinely-untested-but-mergeable case."""
        for verdict in (True, None):
            items = [_q("w1", pr=99)]
            gh = FakeGh(mergeable_results={99: verdict})
            events = process(items, gh, ci_store=self._ci_no_checks())

            assert gh.merge_calls == [], verdict
            kinds = [e.kind for e in events]
            assert "checks_absent" in kinds, verdict
            assert "conflict" not in kinds, verdict
            assert items[0].state == PENDING, verdict
            assert mq.is_ci_absent_reason(items[0].error), verdict

    def test_failing_checks_still_block_regardless_of_mergeability(self) -> None:
        """Acceptance criterion: a PR with genuinely failing (non-empty)
        checks must block exactly as today — the #1877 fall-through only
        applies to the ambiguous empty-checks case, never overrides a real
        red check, and must not even pay for the extra `check_pr_mergeable`
        call outside that one case."""
        from types import SimpleNamespace

        class _Ci:
            is_available = True
            def list_checks_for_pr(self, repo, number):
                return [SimpleNamespace(
                    name="build", status="completed", conclusion="failure",
                    run_id=None,
                )]

        items = [_q("w1", pr=99)]
        gh = FakeGh(mergeable_results={99: False})
        events = process(items, gh, ci_store=_Ci())

        assert gh.mergeable_calls == [], "mergeability must not be consulted here"
        kinds = [e.kind for e in events]
        assert "checks_failed" in kinds
        assert "conflict" not in kinds
        assert items[0].state == PENDING

    def test_dry_run_previews_conflict_route_without_merging(self) -> None:
        items = [_q("w1", pr=99)]
        gh = FakeGh(mergeable_results={99: False})
        events = process(items, gh, ci_store=self._ci_no_checks(), dry_run=True)

        assert gh.merge_calls == []
        kinds = [e.kind for e in events]
        assert "conflict" in kinds
        assert "checks_absent" not in kinds
        assert "merged" not in kinds
        assert items[0].state == PENDING


class TestProcessConflictedUnreadableChecks:
    """#2380: a DIRTY/CONFLICTING PR whose `gh pr checks` FETCH itself fails
    (GitHub can't build a merge ref, so `gh pr checks` has nothing to read)
    reads identically to a transient GitHub-unreachable blip —
    CI_UNREADABLE_PREFIX. Before this fix that parked forever behind
    `checks_unreadable`'s "retry the read" logic: no amount of retrying
    reads GitHub cannot ever answer differently, so the park never resolved
    (the live incident behind this issue, claude-coordinator#2375 / PR
    #2379, parked 6x with zero forward progress). `process()` must consult
    `check_pr_mergeable` — same probe, same fail-closed contract as #1877's
    sibling checks-absent fix — and, for a CONFIRMED conflict only, route
    STRAIGHT to the `conflict` event / #241 conflict-fix dispatch instead of
    parking behind a read that can never succeed."""

    @staticmethod
    def _ci_unreadable():
        from types import SimpleNamespace

        class _Ci:
            is_available = True
            def list_checks_for_pr(self, repo, number):
                return [SimpleNamespace(
                    name="coord: could not read CI status for acme/api#99 "
                         "(HTTP 503: No server is currently available)",
                    status="completed", conclusion="unknown",
                )]
        return _Ci()

    def test_confirmed_conflict_routes_directly_to_conflict_event(self) -> None:
        items = [_q("w1", pr=99)]
        gh = FakeGh(mergeable_results={99: False})
        events = process(items, gh, ci_store=self._ci_unreadable())

        assert gh.mergeable_calls == [("acme/api", 99)]
        # Unlike #1877's empty-checks case, this routes DIRECTLY to a
        # conflict event/state — there is no real `gh pr merge` attempt to
        # fall through to, because the CI gate never reaches the merge
        # section for a non-empty (if unreadable) check list.
        assert gh.merge_calls == [], "no live merge attempt needed for this route"
        kinds = [e.kind for e in events]
        assert "conflict" in kinds
        assert "checks_unreadable" not in kinds
        assert items[0].state == CONFLICT
        # Routes through the SAME classifier #241 already uses — this fix
        # only changes reachability, not the classify-and-dispatch machinery.
        assert mq.classify_conflict(items[0].error) == "rebaseable"

    def test_unknown_mergeability_keeps_parking_as_checks_unreadable(self) -> None:
        """Acceptance criterion: a genuinely UNKNOWN mergeability read
        (still computing, no probe available, or the probe errored) must
        keep today's park-and-wait behavior unchanged — this is additive,
        not a general loosening of the CI-unreadable path."""
        for verdict in (True, None):
            items = [_q("w1", pr=99)]
            gh = FakeGh(mergeable_results={99: verdict})
            events = process(items, gh, ci_store=self._ci_unreadable())

            assert gh.merge_calls == [], verdict
            kinds = [e.kind for e in events]
            assert "checks_unreadable" in kinds, verdict
            assert "conflict" not in kinds, verdict
            assert items[0].state == PENDING, verdict
            assert mq.is_ci_unreadable_reason(items[0].error), verdict

    def test_dry_run_previews_conflict_route_without_merging(self) -> None:
        items = [_q("w1", pr=99)]
        gh = FakeGh(mergeable_results={99: False})
        events = process(items, gh, ci_store=self._ci_unreadable(), dry_run=True)

        assert gh.merge_calls == []
        kinds = [e.kind for e in events]
        assert "conflict" in kinds
        assert "checks_failed" not in kinds
        assert "merged" not in kinds
        assert items[0].state == PENDING


class TestCiInfraReason:
    """#1892: `_ci_infra_reason` — the classification helper, isolated from
    `process()`'s I/O. `getattr(ci, "list_jobs_for_run", None)` is the
    backward-compat seam: a CiStore stand-in that predates #1892 (most
    duck-typed test stubs in this file, and `coord.gate_snapshot.
    GateSnapshot`) must degrade to "no classification", not raise."""

    _fn = staticmethod(mq._ci_infra_reason)
    from coord.ci_store import JobRun as _JobRun, JobStep as _JobStep

    class _Ci:
        def __init__(self, jobs_by_run):
            self._jobs_by_run = jobs_by_run
            self.calls: list[tuple[str, str]] = []

        def list_jobs_for_run(self, repo, run_id):
            self.calls.append((repo, run_id))
            return self._jobs_by_run.get(run_id, [])

    def test_empty_failed_list_is_none_and_makes_no_calls(self) -> None:
        ci = self._Ci({})
        assert self._fn(ci, "acme/api", 1, []) is None
        assert ci.calls == []

    def test_all_verdictless_returns_the_prefixed_reason(self) -> None:
        checks = [_check("e2e", conclusion="cancelled")]
        checks[0].run_id = "999"
        ci = self._Ci({"999": [self._JobRun(name="e2e", conclusion="cancelled", runner_name="", steps=[])]})
        reason = self._fn(ci, "acme/api", 1, checks)
        assert reason is not None
        assert reason.startswith("CI infra:")
        assert "e2e (cancelled)" in reason

    def test_one_real_failure_among_verdictless_ones_returns_none(self) -> None:
        """The hazard the issue warns against: ANY genuinely-failed check in
        the mix must block the whole classification, not just be ignored."""
        infra = _check("e2e", conclusion="cancelled")
        infra.run_id = "999"
        real = _check("build", conclusion="failure")
        real.run_id = "999"
        ci = self._Ci({"999": [
            self._JobRun(name="e2e", conclusion="cancelled", runner_name="", steps=[]),
            self._JobRun(
                name="build", conclusion="failure", runner_name="r1",
                steps=[
                    self._JobStep(name="Set up job", conclusion="success"),
                    self._JobStep(name="Run pytest", conclusion="failure"),
                ],
            ),
        ]})
        assert self._fn(ci, "acme/api", 1, [infra, real]) is None

    def test_stub_without_list_jobs_for_run_degrades_to_none(self) -> None:
        """Every pre-#1892 duck-typed CiStore stub in this file (and
        `coord.gate_snapshot.GateSnapshot`, which deliberately does not
        implement this — the board *read* path must never make this extra
        call) must not raise; classification just isn't available."""
        class _OldCi:
            pass
        checks = [_check("e2e", conclusion="cancelled")]
        assert self._fn(_OldCi(), "acme/api", 1, checks) is None

    def test_only_one_call_per_distinct_run_id(self) -> None:
        checks = [_check("a", conclusion="cancelled"), _check("b", conclusion="cancelled")]
        checks[0].run_id = "999"
        checks[1].run_id = "999"
        ci = self._Ci({"999": [
            self._JobRun(name="a", conclusion="cancelled", runner_name="", steps=[]),
            self._JobRun(name="b", conclusion="cancelled", runner_name="", steps=[]),
        ]})
        self._fn(ci, "acme/api", 1, checks)
        assert ci.calls == [("acme/api", "999")]


class TestProcessCiInfraAutoRerun:
    """#1892: `process()`'s live merge path auto-reruns CI (instead of just
    blocking) when every failing check is verdictless, capped at
    MAX_CI_INFRA_RERUNS, logging every attempt — and behaves exactly like
    today for a genuine failure or a passing PR."""

    @staticmethod
    def _verdictless_checks():
        c = _check("e2e", conclusion="cancelled")
        c.run_id = "999"
        return [c]

    @staticmethod
    def _verdictless_jobs():
        return {"999": [JobRun(name="e2e", conclusion="cancelled", runner_name="", steps=[])]}

    class _Ci:
        is_available = True

        def __init__(self, *, checks, jobs_by_run, rerun_ok=True):
            self._checks = checks
            self._jobs_by_run = jobs_by_run
            self.rerun_ok = rerun_ok
            self.jobs_calls: list[tuple[str, str]] = []
            self.rerun_calls: list[tuple[str, int]] = []

        def list_checks_for_pr(self, repo, number):
            return self._checks

        def list_jobs_for_run(self, repo, run_id):
            self.jobs_calls.append((repo, run_id))
            return self._jobs_by_run.get(run_id, [])

        def rerun_for_pr(self, repo, number):
            self.rerun_calls.append((repo, number))
            return self.rerun_ok

    def test_first_failure_triggers_auto_rerun_not_a_plain_block(self, caplog) -> None:
        import logging
        caplog.set_level(logging.INFO, logger="coord.merge_queue")
        items = [_q("w1", pr=99)]
        gh = FakeGh()
        ci = self._Ci(checks=self._verdictless_checks(), jobs_by_run=self._verdictless_jobs())

        events = process(items, gh, ci_store=ci)

        assert items[0].state == PENDING
        assert items[0].ci_infra_reruns == 1
        assert ci.rerun_calls == [("acme/api", 99)]
        kinds = [e.kind for e in events]
        assert "ci_infra_rerun" in kinds
        assert "checks_failed" not in kinds
        assert items[0].error is not None
        assert items[0].error.startswith("CI infra:")
        assert "auto-rerun 1/2 triggered" in items[0].error
        assert any("#1892 auto-rerun" in r.message for r in caplog.records)

    def test_reruns_stop_at_the_cap_and_every_one_is_logged(self, caplog) -> None:
        import logging
        from coord.merge_queue import MAX_CI_INFRA_RERUNS
        caplog.set_level(logging.INFO, logger="coord.merge_queue")
        items = [_q("w1", pr=99)]
        gh = FakeGh()
        ci = self._Ci(checks=self._verdictless_checks(), jobs_by_run=self._verdictless_jobs())

        for expected in range(1, MAX_CI_INFRA_RERUNS + 1):
            process(items, gh, ci_store=ci)
            assert items[0].ci_infra_reruns == expected

        assert len(ci.rerun_calls) == MAX_CI_INFRA_RERUNS
        infra_logs = [r for r in caplog.records if "#1892 auto-rerun" in r.message]
        assert len(infra_logs) == MAX_CI_INFRA_RERUNS

        # One more tick: budget exhausted, no further rerun, falls back to a
        # plain (non-CI_INFRA-prefixed) checks_failed block for a human.
        events = process(items, gh, ci_store=ci)
        assert len(ci.rerun_calls) == MAX_CI_INFRA_RERUNS  # unchanged
        assert items[0].ci_infra_reruns == MAX_CI_INFRA_RERUNS  # unchanged
        kinds = [e.kind for e in events]
        assert "checks_failed" in kinds
        assert "ci_infra_rerun" not in kinds
        assert not items[0].error.startswith("CI infra:")
        assert "checks failed:" in items[0].error
        assert "auto-rerun budget exhausted" in items[0].error

    def test_pending_between_two_verdictless_failures_does_not_reset_the_budget(
        self, caplog
    ) -> None:
        """Regression: a tick that observes the rerun still in-flight (the
        realistic outcome — a real Actions run takes real wall-clock minutes
        to resolve, so the tick right after this same code triggers a rerun
        will almost always see it as pending, not yet completed) must NOT
        reset `ci_infra_reruns` to 0. Only a genuine resolution (nothing
        failed AND nothing pending) may reset it. Otherwise a workflow
        genuinely broken at "Set up job" fails -> reruns(1) ->
        pending(would wrongly reset to 0) -> fails again -> reruns(1 again)
        forever, never reaching MAX_CI_INFRA_RERUNS and never parking for a
        human — exactly the loop #1892 requires a hard cap against."""
        import logging
        from coord.merge_queue import MAX_CI_INFRA_RERUNS
        caplog.set_level(logging.INFO, logger="coord.merge_queue")
        items = [_q("w1", pr=99)]
        gh = FakeGh()
        ci = self._Ci(checks=self._verdictless_checks(), jobs_by_run=self._verdictless_jobs())

        # Tick 1: verdictless failure -> auto-rerun #1.
        process(items, gh, ci_store=ci)
        assert items[0].ci_infra_reruns == 1

        # Tick 2: the rerun triggered above hasn't resolved yet — it's
        # still queued/in-progress, not failed. This must NOT reset the
        # budget just because `failed_checks` is currently empty.
        pending_check = _check("e2e", status="in_progress", conclusion=None)
        pending_check.run_id = "999"
        ci._checks = [pending_check]
        events = process(items, gh, ci_store=ci)
        kinds = [e.kind for e in events]
        assert "checks_pending" in kinds
        assert items[0].ci_infra_reruns == 1  # unchanged — NOT reset to 0
        assert ci.rerun_calls == [("acme/api", 99)]  # no extra rerun issued

        # Tick 3: the (still-broken) workflow fails again -> auto-rerun #2,
        # correctly continuing the SAME budget rather than starting over.
        ci._checks = self._verdictless_checks()
        process(items, gh, ci_store=ci)
        assert items[0].ci_infra_reruns == 2
        assert len(ci.rerun_calls) == 2

        # Tick 4: budget now exhausted -> falls through to a human, not a
        # third auto-rerun.
        events = process(items, gh, ci_store=ci)
        assert len(ci.rerun_calls) == 2  # unchanged — cap held
        assert items[0].ci_infra_reruns == MAX_CI_INFRA_RERUNS
        kinds = [e.kind for e in events]
        assert "checks_failed" in kinds
        assert "ci_infra_rerun" not in kinds
        assert "auto-rerun budget exhausted" in items[0].error

    def test_a_genuine_failure_is_never_auto_rerun(self) -> None:
        """Acceptance criterion: a PR with ANY genuinely-failed check
        behaves exactly as today — plain checks_failed, no rerun call."""
        items = [_q("w1", pr=99)]
        gh = FakeGh()
        real_failure = _check("build", conclusion="failure")
        real_failure.run_id = "999"
        ci = self._Ci(
            checks=[real_failure],
            jobs_by_run={"999": [JobRun(
                name="build", conclusion="failure", runner_name="r1",
                steps=[
                    JobStep(name="Set up job", conclusion="success"),
                    JobStep(name="Run pytest", conclusion="failure"),
                ],
            )]},
        )

        events = process(items, gh, ci_store=ci)

        assert ci.rerun_calls == []
        assert items[0].ci_infra_reruns == 0
        kinds = [e.kind for e in events]
        assert "checks_failed" in kinds
        assert "ci_infra_rerun" not in kinds
        assert items[0].error == "checks failed: build (failure)"

    def test_jobs_api_never_called_on_the_all_green_path(self) -> None:
        """Acceptance criterion: the extra jobs API call happens only on the
        failure path — assert it is not called when all checks pass.

        #2197: a plain `FakeGh` has no `get_branch_commit_timestamp`, which
        makes the unrelated #1851 staleness gate fail closed (stale) — and
        since #2197 that ALSO calls `rerun_for_pr` (the very same method
        this test's `rerun_calls` tracks for the #1892 dimension), so the
        check here must be unambiguously fresh to keep this test isolated
        to the jobs-call dimension it's actually about.
        """
        items = [_q("w1", pr=99)]

        class _Gh(FakeGh):
            def get_branch_commit_timestamp(self, repo, branch):
                return 1000.0

        gh = _Gh()
        passing = _check("e2e", conclusion="success")
        passing.run_id = "999"
        passing.started_at = 1500.0  # after the mocked base — fresh, not stale
        ci = self._Ci(checks=[passing], jobs_by_run={})

        process(items, gh, ci_store=ci)

        assert ci.jobs_calls == []
        assert ci.rerun_calls == []

    def test_dry_run_previews_the_infra_reason_without_mutating_anything(self) -> None:
        items = [_q("w1", pr=99)]
        gh = FakeGh()
        ci = self._Ci(checks=self._verdictless_checks(), jobs_by_run=self._verdictless_jobs())

        events = process(items, gh, ci_store=ci, dry_run=True)

        assert items[0].state == PENDING
        assert items[0].ci_infra_reruns == 0  # #1892: dry-run never mutates
        assert ci.rerun_calls == []  # #1892: dry-run never reruns CI
        kinds = [e.kind for e in events]
        assert "checks_failed" in kinds
        msg = next(e.message for e in events if e.kind == "checks_failed")
        assert "CI infra:" in msg

    def test_resets_after_a_clean_pass_so_a_later_failure_starts_fresh(self) -> None:
        """A later, unrelated verdictless failure must get its own budget —
        not inherit an exhausted count from an earlier, already-resolved one."""
        from coord.merge_queue import MAX_CI_INFRA_RERUNS
        items = [_q("w1", pr=99)]
        items[0].ci_infra_reruns = MAX_CI_INFRA_RERUNS
        gh = FakeGh()
        passing = _check("e2e", conclusion="success")
        passing.run_id = "999"
        ci = self._Ci(checks=[passing], jobs_by_run={})

        process(items, gh, ci_store=ci)

        assert items[0].ci_infra_reruns == 0


class TestProcessCiUnreadableAutoRetry:
    """#2347: `process()`'s live merge path classifies a bare check-list
    FETCH failure (GitHub unreachable — the #1525 synthetic "could not read
    CI status" stand-in) distinctly from both a real "still running" verdict
    and a real "ran and failed" one — with its own bounded retry count,
    mirroring `MAX_CI_INFRA_RERUNS`'s shape, but — unlike #1892's own
    exhaustion — NEVER falling back to the generic "checks failed" wording
    even once that budget is exhausted: see `_ci_unreadable_reason`'s
    docstring for why a fetch failure is never a real CI verdict no matter
    how many times it repeats."""

    class _Ci:
        is_available = True

        def __init__(self, checks):
            self._checks = checks

        def list_checks_for_pr(self, repo, number):
            return self._checks

    @staticmethod
    def _unreadable_checks(
        detail: str = "HTTP 503: No server is currently available",
    ):
        from coord.ci_github import _unreadable_check
        return [_unreadable_check("acme/api", 99, detail)]

    def test_first_failure_is_classified_distinctly_not_as_checks_failed(
        self,
    ) -> None:
        items = [_q("w1", pr=99)]
        gh = FakeGh()
        ci = self._Ci(self._unreadable_checks())

        events = process(items, gh, ci_store=ci)

        assert items[0].state == PENDING
        assert items[0].ci_unreadable_reruns == 1
        kinds = [e.kind for e in events]
        assert "checks_unreadable" in kinds
        assert "checks_failed" not in kinds
        assert items[0].error is not None
        assert items[0].error.startswith("CI unreadable:")
        assert "GitHub could not be reached" in items[0].error
        assert "retrying automatically (1/2)" in items[0].error
        assert "no attempt spent" in items[0].error

    def test_never_collapses_into_checks_failed_even_after_budget_exhausted(
        self,
    ) -> None:
        from coord.merge_queue import MAX_CI_UNREADABLE_RERUNS

        items = [_q("w1", pr=99)]
        gh = FakeGh()
        ci = self._Ci(self._unreadable_checks())

        for expected in range(1, MAX_CI_UNREADABLE_RERUNS + 1):
            process(items, gh, ci_store=ci)
            assert items[0].ci_unreadable_reruns == expected

        # One (and several) more ticks past the cap: still bounded (pegged,
        # never incremented past the cap) AND still CI_UNREADABLE_PREFIX —
        # never the generic "checks failed" wording, #2347's whole point.
        for _ in range(2):
            events = process(items, gh, ci_store=ci)
            assert items[0].ci_unreadable_reruns == MAX_CI_UNREADABLE_RERUNS
            kinds = [e.kind for e in events]
            assert "checks_unreadable" in kinds
            assert "checks_failed" not in kinds
            assert items[0].error.startswith("CI unreadable:")
            assert "checks failed:" not in items[0].error
            assert "worth a human glance" in items[0].error

    def test_no_attempt_spent_never_calls_rerun_or_jobs_api(self) -> None:
        """Unlike #1892, there is no remedy action to trigger for a bare
        fetch failure — this stub deliberately does not implement
        `rerun_for_pr`/`list_jobs_for_run`/`rerun_failed_for_pr`, so a stray
        call to any of them would raise `AttributeError` and fail the test."""
        items = [_q("w1", pr=99)]
        gh = FakeGh()
        ci = self._Ci(self._unreadable_checks())

        process(items, gh, ci_store=ci)  # would raise if it touched any of them

    def test_a_genuine_failure_is_never_classified_as_unreadable(self) -> None:
        items = [_q("w1", pr=99)]
        gh = FakeGh()
        real_failure = _check("build", conclusion="failure")
        ci = self._Ci([real_failure])

        events = process(items, gh, ci_store=ci)

        kinds = [e.kind for e in events]
        assert "checks_unreadable" not in kinds
        assert "checks_failed" in kinds
        assert items[0].error == "checks failed: build (failure)"
        assert items[0].ci_unreadable_reruns == 0

    def test_a_pending_check_is_never_classified_as_unreadable(self) -> None:
        items = [_q("w1", pr=99)]
        gh = FakeGh()
        pending = _check("e2e", status="in_progress", conclusion=None)
        ci = self._Ci([pending])

        events = process(items, gh, ci_store=ci)

        kinds = [e.kind for e in events]
        assert "checks_unreadable" not in kinds
        assert "checks_pending" in kinds
        assert items[0].ci_unreadable_reruns == 0

    def test_resets_after_a_clean_read_so_a_later_failure_starts_fresh(
        self,
    ) -> None:
        """A later, unrelated fetch failure must get its own budget — not
        inherit an exhausted count from an earlier, already-resolved one."""
        from coord.merge_queue import MAX_CI_UNREADABLE_RERUNS

        class _Gh(FakeGh):
            def get_branch_commit_timestamp(self, repo, branch):
                return 1000.0

        items = [_q("w1", pr=99)]
        items[0].ci_unreadable_reruns = MAX_CI_UNREADABLE_RERUNS
        gh = _Gh()
        passing = _check("e2e", conclusion="success")
        passing.started_at = 1500.0  # after the mocked base — fresh, not stale
        ci = self._Ci([passing])

        process(items, gh, ci_store=ci)

        assert items[0].ci_unreadable_reruns == 0

    def test_dry_run_previews_without_mutating_anything(self) -> None:
        items = [_q("w1", pr=99)]
        gh = FakeGh()
        ci = self._Ci(self._unreadable_checks())

        events = process(items, gh, ci_store=ci, dry_run=True)

        assert items[0].ci_unreadable_reruns == 0  # dry-run never mutates
        kinds = [e.kind for e in events]
        assert "checks_failed" in kinds
        msg = next(e.message for e in events if e.kind == "checks_failed")
        assert "CI unreadable:" in msg
        assert "GitHub could not be reached" in msg


class TestProcessCiFlakyAutoRerun:
    """#2252: `process()`'s live merge path re-runs a genuinely-verdicted
    (non-infra) failure's failed job(s) ONCE — via `CiStore.
    rerun_failed_for_pr`, scoped so passing checks are left untouched —
    before spending a drive attempt on it. Passes on the second read: merge
    proceeds, zero attempts spent, the flake is recorded. Fails again:
    attempt spent, entry blocks — identical to today."""

    class _Ci:
        is_available = True

        def __init__(self, *, checks, rerun_ok=True):
            self._checks = checks
            self.rerun_ok = rerun_ok
            self.rerun_failed_calls: list[tuple[str, int]] = []

        def list_checks_for_pr(self, repo, number):
            return self._checks

        def rerun_failed_for_pr(self, repo, number):
            self.rerun_failed_calls.append((repo, number))
            return self.rerun_ok

    class _CiPredatingTheFeature:
        """A CiStore stand-in with no `rerun_failed_for_pr` at all — most
        duck-typed test stubs elsewhere in this file, and
        `coord.gate_snapshot.GateSnapshot`. Call sites duck-type via
        `getattr(ci, "rerun_failed_for_pr", None)`, so this must degrade to
        today's plain `checks_failed` block, not raise."""

        is_available = True

        def __init__(self, *, checks):
            self._checks = checks

        def list_checks_for_pr(self, repo, number):
            return self._checks

    @staticmethod
    def _genuine_failure():
        c = _check("build", conclusion="failure")
        c.run_id = "999"
        return [c]

    @staticmethod
    def _audit_rows(coord_db, event_type: str = "ci_flake_detected") -> list:
        return coord_db.execute(
            "SELECT * FROM audit_log WHERE event_type = ?", (event_type,)
        ).fetchall()

    def test_first_genuine_failure_triggers_one_re_check_not_a_plain_block(
        self, caplog
    ) -> None:
        import logging
        caplog.set_level(logging.INFO, logger="coord.merge_queue")
        items = [_q("w1", pr=99)]
        gh = FakeGh()
        ci = self._Ci(checks=self._genuine_failure())

        events = process(items, gh, ci_store=ci)

        assert items[0].state == PENDING
        assert items[0].ci_flaky_reruns == 1
        assert ci.rerun_failed_calls == [("acme/api", 99)]
        kinds = [e.kind for e in events]
        assert "ci_flaky_rerun" in kinds
        assert "checks_failed" not in kinds
        assert items[0].error is not None
        assert items[0].error.startswith("CI re-checking:")
        assert items[0].ci_flaky_pending  # stashed for the eventual audit row
        assert any("#2252 flake re-check" in r.message for r in caplog.records)

    def test_pass_on_second_read_merges_spends_zero_attempts_and_records_flake(
        self, coord_db
    ) -> None:
        """The issue's own black-box acceptance: red once, green on re-run
        -> merges, `ci_flaky_reruns` resets to 0 (never counted as a spent
        drive attempt), and the flake is durably recorded."""
        items = [_q("w1", pr=99)]

        class _Gh(FakeGh):
            def get_branch_commit_timestamp(self, repo, branch):
                return 1000.0  # #2197: keep the unrelated staleness gate green

        gh = _Gh()
        ci = self._Ci(checks=self._genuine_failure())

        # Tick 1: red -> one scoped re-run triggered, not yet blocked.
        process(items, gh, ci_store=ci)
        assert items[0].ci_flaky_reruns == 1
        assert items[0].ci_flaky_pending
        assert gh.merge_calls == []

        # Tick 2: the re-run landed green.
        passing = _check("build", conclusion="success")
        passing.run_id = "999"
        passing.started_at = 1500.0  # after the mocked base — fresh, not stale
        ci._checks = [passing]
        events = process(items, gh, ci_store=ci)

        assert items[0].state == MERGED
        assert gh.merge_calls == [("acme/api", 99, "rebase")]
        kinds = [e.kind for e in events]
        assert "checks_failed" not in kinds
        assert "ci_flaky_rerun" not in kinds
        assert items[0].ci_flaky_reruns == 0
        assert items[0].ci_flaky_pending == ""

        rows = self._audit_rows(coord_db)
        assert len(rows) == 1
        assert rows[0]["tier"] == "operational"
        assert rows[0]["category"] == "ci"
        assert rows[0]["repo"] == "api"
        details = json.loads(rows[0]["details_json"])
        assert details["pr_number"] == 99
        assert details["checks"] == [{"name": "build", "conclusion": "failure"}]

    def test_fails_again_spends_the_attempt_identical_to_today(self) -> None:
        """Acceptance: a check that fails twice behaves exactly like today
        — no third re-run, plain `checks_failed`, entry stays blocked."""
        from coord.merge_queue import MAX_CI_FLAKY_RERUNS
        items = [_q("w1", pr=99)]
        gh = FakeGh()
        ci = self._Ci(checks=self._genuine_failure())

        # Tick 1: red -> one scoped re-run triggered.
        process(items, gh, ci_store=ci)
        assert items[0].ci_flaky_reruns == MAX_CI_FLAKY_RERUNS
        assert ci.rerun_failed_calls == [("acme/api", 99)]

        # Tick 2: still red (a real, repeatable failure) -> confirmed,
        # budget exhausted, no second re-run call.
        events = process(items, gh, ci_store=ci)

        assert gh.merge_calls == []
        assert items[0].ci_flaky_reruns == MAX_CI_FLAKY_RERUNS  # unchanged
        assert ci.rerun_failed_calls == [("acme/api", 99)]  # unchanged — no 2nd call
        assert items[0].ci_flaky_pending == ""  # confirmed real, nothing to record
        kinds = [e.kind for e in events]
        assert "checks_failed" in kinds
        assert "ci_flaky_rerun" not in kinds
        assert items[0].error == "checks failed: build (failure)"

    def test_a_check_that_never_reported_is_untouched_by_this_path(self) -> None:
        """#1904/#2244: zero reported checks is a DIFFERENT condition — a
        re-run of nothing produces nothing but latency, so this path must
        never fire for it."""
        items = [_q("w1", pr=99)]
        ci = self._Ci(checks=[])
        gh = FakeGh(mergeable_results={99: True})

        events = process(items, gh, ci_store=ci)

        assert ci.rerun_failed_calls == []
        assert items[0].ci_flaky_reruns == 0
        kinds = [e.kind for e in events]
        assert "checks_absent" in kinds
        assert "ci_flaky_rerun" not in kinds

    def test_rerun_capability_missing_falls_back_to_todays_behaviour(self) -> None:
        """#2252 fail-safe: a `CiStore` that can't trigger a re-run at all
        (predates the capability) must degrade to spending the attempt
        exactly as if #2252 did not exist — never treat "could not re-run"
        as "passed"."""
        items = [_q("w1", pr=99)]
        gh = FakeGh()
        ci = self._CiPredatingTheFeature(checks=self._genuine_failure())

        events = process(items, gh, ci_store=ci)

        assert gh.merge_calls == []
        assert items[0].ci_flaky_reruns == 0
        assert items[0].ci_flaky_pending == ""
        kinds = [e.kind for e in events]
        assert "checks_failed" in kinds
        assert "ci_flaky_rerun" not in kinds
        assert items[0].error == "checks failed: build (failure)"

    def test_rerun_call_that_fails_to_trigger_falls_back_to_todays_behaviour(
        self,
    ) -> None:
        """Same fail-safe, the OTHER way a re-run can fail to happen: the
        capability exists but the `gh` call itself came back non-zero."""
        items = [_q("w1", pr=99)]
        gh = FakeGh()
        ci = self._Ci(checks=self._genuine_failure(), rerun_ok=False)

        events = process(items, gh, ci_store=ci)

        assert gh.merge_calls == []
        assert items[0].ci_flaky_reruns == 0  # never incremented — no rerun happened
        assert items[0].ci_flaky_pending == ""
        kinds = [e.kind for e in events]
        assert "checks_failed" in kinds
        assert "ci_flaky_rerun" not in kinds
        assert items[0].error == "checks failed: build (failure)"

    def test_dry_run_never_triggers_a_rerun_or_mutates_anything(self) -> None:
        """Mirrors #1892's own dry-run guarantee: `--dry-run` previews the
        gate, it never mutates CI or persisted counters."""
        items = [_q("w1", pr=99)]
        gh = FakeGh()
        ci = self._Ci(checks=self._genuine_failure())

        events = process(items, gh, ci_store=ci, dry_run=True)

        assert items[0].state == PENDING
        assert items[0].ci_flaky_reruns == 0
        assert ci.rerun_failed_calls == []
        kinds = [e.kind for e in events]
        assert "checks_failed" in kinds
        assert "ci_flaky_rerun" not in kinds


# ── #778: staging_items() ─────────────────────────────────────────────────────

class TestStagingItems:
    """#778: staging_items() surfaces approved/done work not yet in the queue.

    The helper must:
    - Return READY items when all gates pass.
    - Return BLOCKED items when the smoke gate fails.
    - Exclude items whose review is not yet approved.
    - Exclude items already tracked in the merge queue.
    - Exclude items from issues already MERGED.
    - Behave sensibly when review or smoke gates are disabled.
    """

    # ── helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _config(*, review_enabled: bool = True, gates: list[str] | None = None):
        from dataclasses import dataclass, field as dc_field

        @dataclass
        class _Reviews:
            enabled: bool = True

        @dataclass
        class _Pipeline:
            default_gates: list[str] | None = None

        @dataclass
        class _Cfg:
            reviews: _Reviews = dc_field(default_factory=_Reviews)
            pipeline: _Pipeline = dc_field(default_factory=_Pipeline)

        cfg = _Cfg()
        cfg.reviews.enabled = review_enabled
        cfg.pipeline.default_gates = gates if gates is not None else ["review", "test", "merge"]
        return cfg

    @staticmethod
    def _board(completed=None, active=None):
        from coord.models import Board
        return Board(active=list(active or []), completed=list(completed or []))

    @staticmethod
    def _work(
        aid: str = "w1",
        *,
        test_state: str | None = "passed",
        branch: str | None = None,
        issue_number: int = 42,
    ) -> Assignment:
        return Assignment(
            machine_name="m1", repo_name="api", issue_number=issue_number,
            issue_title="Some feature", assignment_id=aid, type="work",
            status="done", branch=branch or f"issue-{issue_number}-{aid}",
            test_state=test_state,
        )

    @staticmethod
    def _review(of_aid: str, *, verdict: str = "approve") -> Assignment:
        return Assignment(
            machine_name="m2", repo_name="api", issue_number=42,
            issue_title="Some feature", assignment_id=f"rev-{of_aid}",
            type="review", status="done",
            review_of_assignment_id=of_aid, review_verdict=verdict,
        )

    # ── ready path ────────────────────────────────────────────────────────

    def test_ready_when_approved_and_smoke_passed(self, coord_db) -> None:
        """Approved review + passed test → READY staging item."""
        work = self._work("w1", test_state="passed")
        rev = self._review("w1")
        board = self._board(completed=[work, rev])
        cfg = self._config()
        items = mq.staging_items(board, cfg)
        assert len(items) == 1
        assert items[0].assignment_id == "w1"
        assert items[0].status == mq.STAGING_READY
        assert items[0].reason is None

    def test_ready_when_mock_author_approved_and_smoke_passed(self, coord_db) -> None:
        """#930 fix: a ``type="mock-author"`` (Gate A) completion is a
        staging item too — mirrors ordinary work, since it must flow through
        the same Work -> Test -> Review -> Merge pipeline."""
        work = Assignment(
            machine_name="m1", repo_name="api", issue_number=42,
            issue_title="Some feature", assignment_id="ma1", type="mock-author",
            status="done", branch="ms-5-gate-a", test_state="passed",
        )
        rev = self._review("ma1")
        board = self._board(completed=[work, rev])
        cfg = self._config()
        items = mq.staging_items(board, cfg)
        assert len(items) == 1
        assert items[0].assignment_id == "ma1"
        assert items[0].status == mq.STAGING_READY

    def test_ready_when_test_author_approved_and_smoke_skipped(self, coord_db) -> None:
        """#1141 fix: a ``type="test-author"`` (#931, per-issue JIT
        acceptance-slice authoring) completion is a staging item too —
        mirrors ordinary work/mock-author, since it must flow through the
        same Work -> Test -> Review -> Merge pipeline. Uses a skipped test
        verdict, the expected verdict for a fixtures/tests-only diff."""
        work = Assignment(
            machine_name="m1", repo_name="api", issue_number=1117,
            issue_title="ms-37 acceptance slice", assignment_id="ta1",
            type="test-author", status="done", branch="ms-37-test-author",
            test_state="skipped",
        )
        rev = self._review("ta1")
        board = self._board(completed=[work, rev])
        cfg = self._config()
        items = mq.staging_items(board, cfg)
        assert len(items) == 1
        assert items[0].assignment_id == "ta1"
        assert items[0].status == mq.STAGING_READY

    def test_ready_when_approved_and_smoke_skipped(self, coord_db) -> None:
        """Approved review + skipped test → READY (skipped counts as verdict)."""
        work = self._work("w1", test_state="skipped")
        rev = self._review("w1")
        board = self._board(completed=[work, rev])
        cfg = self._config()
        items = mq.staging_items(board, cfg)
        assert len(items) == 1
        assert items[0].status == mq.STAGING_READY

    # ── blocked path ──────────────────────────────────────────────────────

    def test_blocked_when_smoke_verdict_missing(self, coord_db) -> None:
        """Approved review but no smoke verdict → BLOCKED with reason."""
        work = self._work("w1", test_state=None)
        rev = self._review("w1")
        board = self._board(completed=[work, rev])
        cfg = self._config()
        items = mq.staging_items(board, cfg)
        assert len(items) == 1
        assert items[0].status == mq.STAGING_BLOCKED
        assert items[0].reason == "test verdict missing"

    def test_blocked_when_smoke_verdict_failed(self, coord_db) -> None:
        """test_state='failed' counts as missing for staging purposes."""
        work = self._work("w1", test_state="failed")
        rev = self._review("w1")
        board = self._board(completed=[work, rev])
        cfg = self._config()
        items = mq.staging_items(board, cfg)
        assert len(items) == 1
        assert items[0].status == mq.STAGING_BLOCKED

    # ── exclusion: review not yet approved ────────────────────────────────

    def test_excluded_when_review_not_approved(self, coord_db) -> None:
        """Work with request-changes review is NOT a staging item."""
        work = self._work("w1")
        rev = self._review("w1", verdict="request-changes")
        board = self._board(completed=[work, rev])
        cfg = self._config()
        items = mq.staging_items(board, cfg)
        assert items == []

    def test_excluded_when_no_review_at_all(self, coord_db) -> None:
        """Work with no review at all is excluded when review gate is enabled."""
        work = self._work("w1")
        board = self._board(completed=[work])
        cfg = self._config()
        items = mq.staging_items(board, cfg)
        assert items == []

    # ── #567 follow-up: fix worker with branch=NULL must still be found ────

    def test_ready_when_fix_worker_approved_with_null_branch(self, coord_db) -> None:
        """#567 follow-up: `has_approved_review` (called directly by
        staging_items since #2085, the /board staging section) must
        recognize an approved review on a fix worker dispatched with
        branch=NULL (the #557 gap) via the review_of_assignment_id chain —
        not just branch-keyed siblings. Mirrors the has_approved_review fix,
        via the shared `_chain_work_ids`."""
        orig_work = self._work("orig", branch="worker/orig")
        fix_work = Assignment(
            machine_name="m1", repo_name="api", issue_number=42,
            issue_title="[fix-1] t", assignment_id="fix1", type="work",
            status="done", branch=None, review_of_assignment_id="orig",
        )
        re_review = self._review("fix1", verdict="approve")
        orig_review = self._review("orig", verdict="request-changes")
        board = self._board(completed=[orig_work, orig_review, fix_work, re_review])
        cfg = self._config()
        items = mq.staging_items(board, cfg)
        assert len(items) == 1
        assert items[0].assignment_id == "orig"
        assert items[0].status == mq.STAGING_READY

    # ── exclusion: already in queue ───────────────────────────────────────

    def test_excluded_when_already_queued(self, coord_db) -> None:
        """Items already in the merge queue are not shown in staging."""
        work = self._work("w1")
        rev = self._review("w1")
        board = self._board(completed=[work, rev])
        # Seed the queue with the same assignment_id.
        save_queue([_q("w1")])
        cfg = self._config()
        items = mq.staging_items(board, cfg)
        assert items == []

    def test_excluded_when_branch_already_queued_by_different_assignment(
        self, coord_db
    ) -> None:
        """A fix dispatched after the original work was enqueued must not
        appear in staging, even though its assignment_id differs from the
        queued entry.  Branch-level dedup catches this (#778 smoke-test
        failure: fix-1 cycled in/out of staging every ~30 s)."""
        branch = "issue-42-original"
        # The original work (different aid) is already in the queue.
        original_work = self._work("w-orig", branch=branch, issue_number=42)
        # A fix worker shares the same branch but has a fresh assignment_id.
        fix_work = self._work("w-fix", branch=branch, issue_number=42, test_state=None)
        rev = self._review("w-fix")
        board = self._board(completed=[original_work, fix_work, rev])
        # Queue contains the original assignment_id — NOT the fix's.
        save_queue([_q("w-orig", branch=branch)])
        cfg = self._config()
        items = mq.staging_items(board, cfg)
        # The fix must be excluded: its branch is already in the queue.
        assert items == [], (
            f"Expected no staging items but got: {items}"
        )

    def test_excluded_when_issue_already_merged(self, coord_db) -> None:
        """Items from an issue with a MERGED queue entry are excluded."""
        work = self._work("w1", issue_number=42)
        rev = self._review("w1")
        board = self._board(completed=[work, rev])
        # Seed a MERGED entry for the same (repo, issue) pair.
        merged_entry = QueuedMerge(
            assignment_id="old-w", repo_name="api", repo_github="acme/api",
            branch="issue-42-old", target_branch="main",
            issue_number=42, issue_title="Some feature",
            state=MERGED,
        )
        save_queue([merged_entry])
        cfg = self._config()
        items = mq.staging_items(board, cfg)
        assert items == []

    # ── gate-disabled paths ───────────────────────────────────────────────

    def test_included_when_review_gate_disabled(self, coord_db) -> None:
        """When reviews are disabled, work is included without needing a review."""
        work = self._work("w1")
        board = self._board(completed=[work])
        cfg = self._config(review_enabled=False, gates=["test", "merge"])
        items = mq.staging_items(board, cfg)
        assert len(items) == 1
        assert items[0].status == mq.STAGING_READY

    def test_included_when_smoke_gate_disabled(self, coord_db) -> None:
        """When 'test' is not in default_gates, missing verdict → READY."""
        work = self._work("w1", test_state=None)
        rev = self._review("w1")
        board = self._board(completed=[work, rev])
        cfg = self._config(gates=["review", "merge"])  # no "test" gate
        items = mq.staging_items(board, cfg)
        assert len(items) == 1
        assert items[0].status == mq.STAGING_READY

    # ── metadata ─────────────────────────────────────────────────────────

    def test_item_carries_metadata(self, coord_db) -> None:
        """StagingItem carries the correct repo/issue/branch metadata."""
        work = self._work("w1", issue_number=99, branch="issue-99-w1")
        rev = self._review("w1")
        board = self._board(completed=[work, rev])
        cfg = self._config()
        items = mq.staging_items(board, cfg)
        assert len(items) == 1
        item = items[0]
        assert item.assignment_id == "w1"
        assert item.repo_name == "api"
        assert item.issue_number == 99
        assert item.branch == "issue-99-w1"
        assert item.issue_title == "Some feature"

    # ── no-config / no-board ──────────────────────────────────────────────

    def test_returns_empty_without_board(self, coord_db) -> None:
        """Without a board there are no completed assignments to scan."""
        from coord.models import Board
        cfg = self._config()
        items = mq.staging_items(Board(active=[], completed=[]), cfg)
        assert items == []


# ── #920: find_sibling_overlaps ──────────────────────────────────────────────

class TestFindSiblingOverlaps:
    """#920: warn when ≥2 approved (PENDING), aging queue entries touch the
    same files — the #769/#645/#770 sibling-branch-collision shape.
    """

    AGING_HOURS = 2.0
    NOW = 1_000_000.0  # arbitrary fixed epoch so ages are deterministic

    @staticmethod
    def _config(aging_hours: float = AGING_HOURS):
        from coord.config import MergeConfig
        from types import SimpleNamespace
        return SimpleNamespace(merge=MergeConfig(sibling_overlap_aging_hours=aging_hours))

    @staticmethod
    def _board(completed=None, active=None):
        from coord.models import Board
        return Board(active=list(active or []), completed=list(completed or []))

    @staticmethod
    def _work(aid: str, *, issue_number: int, files: list[str]) -> Assignment:
        return Assignment(
            machine_name="m1", repo_name="api", issue_number=issue_number,
            issue_title=f"issue {issue_number}", assignment_id=aid, type="work",
            status="done", branch=f"issue-{issue_number}-{aid}",
            files_allowed=files,
        )

    def _entry(
        self, aid: str, *, issue_number: int, enqueued_at: float,
        repo_github: str = "acme/api", target_branch: str = "main",
        state: str = mq.PENDING,
    ) -> mq.QueuedMerge:
        return mq.QueuedMerge(
            assignment_id=aid, repo_name="api", repo_github=repo_github,
            branch=f"issue-{issue_number}-{aid}", target_branch=target_branch,
            issue_number=issue_number, issue_title=f"issue {issue_number}",
            state=state, enqueued_at=enqueued_at,
        )

    def test_warns_on_aged_overlapping_pair(self, coord_db) -> None:
        old_enqueued = self.NOW - (self.AGING_HOURS + 1) * 3600
        mq.save_queue([
            self._entry("a1", issue_number=101, enqueued_at=old_enqueued),
            self._entry("a2", issue_number=102, enqueued_at=self.NOW - 60),
        ])
        board = self._board(completed=[
            self._work("a1", issue_number=101, files=["coord/foo.py", "coord/bar.py"]),
            self._work("a2", issue_number=102, files=["coord/bar.py", "coord/baz.py"]),
        ])
        warnings = mq.find_sibling_overlaps(board, self._config(), now=self.NOW)
        assert len(warnings) == 1
        w = warnings[0]
        assert w.repo_name == "api"
        assert w.target_branch == "main"
        assert w.issue_numbers == (101, 102)  # oldest (a1) first
        assert w.overlapping_files == ("coord/bar.py",)
        assert w.oldest_age_hours == pytest.approx(self.AGING_HOURS + 1, abs=0.05)

    def test_no_warning_when_files_dont_overlap(self, coord_db) -> None:
        old_enqueued = self.NOW - (self.AGING_HOURS + 1) * 3600
        mq.save_queue([
            self._entry("a1", issue_number=101, enqueued_at=old_enqueued),
            self._entry("a2", issue_number=102, enqueued_at=self.NOW - 60),
        ])
        board = self._board(completed=[
            self._work("a1", issue_number=101, files=["coord/foo.py"]),
            self._work("a2", issue_number=102, files=["coord/baz.py"]),
        ])
        assert mq.find_sibling_overlaps(board, self._config(), now=self.NOW) == []

    def test_no_warning_when_not_yet_aged(self, coord_db) -> None:
        """Overlap exists but the oldest entry hasn't crossed the threshold yet."""
        mq.save_queue([
            self._entry("a1", issue_number=101, enqueued_at=self.NOW - 60),
            self._entry("a2", issue_number=102, enqueued_at=self.NOW - 30),
        ])
        board = self._board(completed=[
            self._work("a1", issue_number=101, files=["coord/foo.py"]),
            self._work("a2", issue_number=102, files=["coord/foo.py"]),
        ])
        assert mq.find_sibling_overlaps(board, self._config(), now=self.NOW) == []

    def test_no_warning_with_single_entry(self, coord_db) -> None:
        old_enqueued = self.NOW - (self.AGING_HOURS + 1) * 3600
        mq.save_queue([self._entry("a1", issue_number=101, enqueued_at=old_enqueued)])
        board = self._board(completed=[
            self._work("a1", issue_number=101, files=["coord/foo.py"]),
        ])
        assert mq.find_sibling_overlaps(board, self._config(), now=self.NOW) == []

    def test_non_pending_entries_ignored(self, coord_db) -> None:
        """A MERGED sibling doesn't trigger a warning against a live PENDING one."""
        old_enqueued = self.NOW - (self.AGING_HOURS + 1) * 3600
        mq.save_queue([
            self._entry("a1", issue_number=101, enqueued_at=old_enqueued, state=mq.MERGED),
            self._entry("a2", issue_number=102, enqueued_at=self.NOW - 60),
        ])
        board = self._board(completed=[
            self._work("a1", issue_number=101, files=["coord/foo.py"]),
            self._work("a2", issue_number=102, files=["coord/foo.py"]),
        ])
        assert mq.find_sibling_overlaps(board, self._config(), now=self.NOW) == []

    def test_different_target_branches_not_grouped(self, coord_db) -> None:
        old_enqueued = self.NOW - (self.AGING_HOURS + 1) * 3600
        mq.save_queue([
            self._entry("a1", issue_number=101, enqueued_at=old_enqueued, target_branch="main"),
            self._entry("a2", issue_number=102, enqueued_at=old_enqueued, target_branch="feature/ms-1"),
        ])
        board = self._board(completed=[
            self._work("a1", issue_number=101, files=["coord/foo.py"]),
            self._work("a2", issue_number=102, files=["coord/foo.py"]),
        ])
        assert mq.find_sibling_overlaps(board, self._config(), now=self.NOW) == []

    def test_transitive_cluster_of_three(self, coord_db) -> None:
        """a1↔a2 share a file, a2↔a3 share a different file — all three cluster."""
        old_enqueued = self.NOW - (self.AGING_HOURS + 1) * 3600
        mq.save_queue([
            self._entry("a1", issue_number=101, enqueued_at=old_enqueued),
            self._entry("a2", issue_number=102, enqueued_at=self.NOW - 120),
            self._entry("a3", issue_number=103, enqueued_at=self.NOW - 60),
        ])
        board = self._board(completed=[
            self._work("a1", issue_number=101, files=["coord/a.py"]),
            self._work("a2", issue_number=102, files=["coord/a.py", "coord/b.py"]),
            self._work("a3", issue_number=103, files=["coord/b.py"]),
        ])
        warnings = mq.find_sibling_overlaps(board, self._config(), now=self.NOW)
        assert len(warnings) == 1
        assert warnings[0].issue_numbers == (101, 102, 103)
        assert set(warnings[0].overlapping_files) == {"coord/a.py", "coord/b.py"}

    def test_disabled_via_zero_aging_hours(self, coord_db) -> None:
        old_enqueued = self.NOW - 1000 * 3600
        mq.save_queue([
            self._entry("a1", issue_number=101, enqueued_at=old_enqueued),
            self._entry("a2", issue_number=102, enqueued_at=old_enqueued),
        ])
        board = self._board(completed=[
            self._work("a1", issue_number=101, files=["coord/foo.py"]),
            self._work("a2", issue_number=102, files=["coord/foo.py"]),
        ])
        cfg = self._config(aging_hours=0)
        assert mq.find_sibling_overlaps(board, cfg, now=self.NOW) == []

    def test_missing_merge_config_defaults_to_24h(self, coord_db) -> None:
        """A config object with no `.merge` attribute falls back to the default."""
        from types import SimpleNamespace
        old_enqueued = self.NOW - 25 * 3600
        mq.save_queue([
            self._entry("a1", issue_number=101, enqueued_at=old_enqueued),
            self._entry("a2", issue_number=102, enqueued_at=self.NOW - 60),
        ])
        board = self._board(completed=[
            self._work("a1", issue_number=101, files=["coord/foo.py"]),
            self._work("a2", issue_number=102, files=["coord/foo.py"]),
        ])
        warnings = mq.find_sibling_overlaps(board, SimpleNamespace(), now=self.NOW)
        assert len(warnings) == 1


# ── #420: display_error — recompute stale gate errors live ──────────────────

class TestDisplayError:
    """`coord status`'s merge-queue section must not echo a stored
    ``entry.error`` verbatim when it was a review/smoke gate message — that
    string is only refreshed by a real merge attempt (`process()`), so an
    approval or verdict recorded afterward (the normal interactive path, no
    `coord merge`/auto-loop tick in between) would otherwise keep showing as
    "blocked" forever, inviting an operator to redundantly bounce already-
    approved work (the #410 real-world case).
    """

    @staticmethod
    def _config(*, review_enabled: bool = True, gates: list[str] | None = None):
        from dataclasses import dataclass, field as dc_field
        @dataclass
        class _Reviews:
            enabled: bool = True
        @dataclass
        class _Pipeline:
            default_gates: list[str] | None = None
        @dataclass
        class _Cfg:
            reviews: _Reviews = dc_field(default_factory=_Reviews)
            pipeline: _Pipeline = dc_field(default_factory=_Pipeline)
        cfg = _Cfg()
        cfg.reviews.enabled = review_enabled
        cfg.pipeline.default_gates = gates if gates is not None else ["review", "test", "merge"]
        return cfg

    @staticmethod
    def _board(active=None, completed=None):
        from coord.models import Board
        return Board(active=list(active or []), completed=list(completed or []))

    @staticmethod
    def _work(aid: str = "w1", *, test_state: str | None = None) -> Assignment:
        return Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id=aid, type="work", status="done", branch=f"worker/{aid}",
            test_state=test_state,
        )

    @staticmethod
    def _review(of_aid: str, *, verdict: str | None = "approve") -> Assignment:
        return Assignment(
            machine_name="m2", repo_name="api", issue_number=1, issue_title="t",
            assignment_id=f"rev-{of_aid}", type="review", status="done",
            review_of_assignment_id=of_aid, review_verdict=verdict,
        )

    def test_clears_stale_review_error_once_approved(self) -> None:
        """The #410 case: entry.error was stamped before the approval landed;
        a later read must not keep showing "review required but not approved"."""
        cfg = self._config()
        entry = _q("w1")
        entry.error = "review required but not approved"
        board = self._board(completed=[
            self._work("w1"), self._review("w1", verdict="approve"),
        ])
        assert mq.display_error(entry, board, cfg) is None

    def test_keeps_review_error_when_still_unapproved(self) -> None:
        cfg = self._config()
        entry = _q("w1")
        entry.error = "review required but not approved"
        board = self._board(completed=[self._work("w1")])
        assert mq.display_error(entry, board, cfg) == "review required but not approved"

    def test_keeps_review_error_when_request_changes(self) -> None:
        cfg = self._config()
        entry = _q("w1")
        entry.error = "review required but not approved"
        board = self._board(completed=[
            self._work("w1"), self._review("w1", verdict="request-changes"),
        ])
        assert mq.display_error(entry, board, cfg) == "review required but not approved"

    # ── #2085 review follow-up: "not approved" vs "not yet checked" ─────────

    def test_unknown_branch_head_is_reported_as_unconfirmed_not_unapproved(
        self,
    ) -> None:
        """#2085: a freshly-approved entry that no live `process()`/`plan()`
        tick has touched yet has `branch_head_sha is None`, so this
        deliberately I/O-free recompute cannot bind the review's
        `review_head_sha` to anything.

        FAILS against the pre-fix code, which called
        `has_approved_review(entry, board)` and got a bare `False` — the same
        answer a *confirmed* supersession gives — so it echoed the stale
        "review required but not approved" indefinitely for work that
        actually passes. That is an unconfirmed FAILURE verdict, the mirror
        image of the unconfirmed-success shape epic #2096 is about; clearing
        the string outright would be the unconfirmed-success one (the #1640
        trap the smoke branch documents). The honest third answer is neither.
        """
        cfg = self._config()
        entry = _q("w1")
        entry.error = "review required but not approved"
        entry.branch_head_sha = None  # never through a live tick
        review = self._review("w1", verdict="approve")
        review.review_head_sha = "sha-current"  # real reviews always set this
        board = self._board(completed=[self._work("w1"), review])

        assert mq.display_error(entry, board, cfg) == mq.REVIEW_UNCONFIRMED_ERROR
        assert mq.display_error(entry, board, cfg) != entry.error

    def test_confirmed_supersession_still_shows_the_real_refusal(self) -> None:
        """The companion: when the branch head IS known and demonstrably
        moved past the reviewed SHA (the #1966 chain), the refusal is
        confirmed — it must keep reading as "not approved", not soften into
        the unconfirmed wording."""
        cfg = self._config()
        entry = _q("w1")
        entry.error = "review required but not approved"
        entry.branch_head_sha = "sha-new"  # a live tick populated it
        review = self._review("w1", verdict="approve")
        review.review_head_sha = "sha-old"  # commits landed after approval
        board = self._board(completed=[self._work("w1"), review])

        assert mq.display_error(entry, board, cfg) == "review required but not approved"

    def test_unknown_head_with_no_approval_at_all_is_still_unapproved(self) -> None:
        """`unknown_head` must only be set when an approval was actually
        refused for want of a head SHA — not merely because the entry has no
        `branch_head_sha`. With no approving review anywhere, the stored
        refusal is confirmed and stands."""
        cfg = self._config()
        entry = _q("w1")
        entry.error = "review required but not approved"
        entry.branch_head_sha = None
        board = self._board(completed=[
            self._work("w1"), self._review("w1", verdict="request-changes"),
        ])

        assert mq.display_error(entry, board, cfg) == "review required but not approved"

    def test_legacy_review_without_a_sha_still_clears(self) -> None:
        """A pre-#821 approval carrying no `review_head_sha` at all has
        nothing to bind, takes the legacy skip path, and counts as approved —
        so the stale error clears outright, exactly as before #2085."""
        cfg = self._config()
        entry = _q("w1")
        entry.error = "review required but not approved"
        entry.branch_head_sha = None
        board = self._board(completed=[
            self._work("w1"), self._review("w1", verdict="approve"),
        ])

        assert mq.display_error(entry, board, cfg) is None

    def test_clears_stale_smoke_error_once_verdict_recorded(self) -> None:
        cfg = self._config(review_enabled=False, gates=["test", "merge"])
        entry = _q("w1")
        entry.error = "smoke test required but no verdict recorded"
        board = self._board(completed=[self._work("w1", test_state="passed")])
        assert mq.display_error(entry, board, cfg) is None

    def test_keeps_smoke_error_when_no_verdict_yet(self) -> None:
        cfg = self._config(review_enabled=False, gates=["test", "merge"])
        entry = _q("w1")
        entry.error = "smoke test required but no verdict recorded"
        board = self._board(completed=[self._work("w1")])
        assert mq.display_error(entry, board, cfg) == "smoke test required but no verdict recorded"

    def test_other_errors_pass_through_unchanged(self) -> None:
        """Conflict/CI errors reflect the outcome of the last real attempt —
        they must not be recomputed just because board/config are available."""
        cfg = self._config()
        entry = _q("w1")
        entry.error = "checks failed: build (failure)"
        board = self._board(completed=[
            self._work("w1"), self._review("w1", verdict="approve"),
        ])
        assert mq.display_error(entry, board, cfg) == "checks failed: build (failure)"

    def test_none_error_stays_none(self) -> None:
        cfg = self._config()
        entry = _q("w1")
        board = self._board()
        assert mq.display_error(entry, board, cfg) is None

    def test_falls_back_to_stored_error_without_board_or_config(self) -> None:
        """Can't safely recompute without both board and config — keep the
        stored string rather than silently dropping a real block."""
        entry = _q("w1")
        entry.error = "review required but not approved"
        assert mq.display_error(entry, None, None) == "review required but not approved"


# ── #1640: stale vs missing smoke verdict, and plan/only agreement ───────────

class TestStaleSmokeVerdictReporting:
    """#1640: a verdict that EXISTS but fails the #1479 freshness check must
    be reported as stale — never as "no verdict recorded" — and every reader
    must reach the same conclusion for the same entry.

    The scenario reproduced here is the one that made #1640 get filed as a
    lost DB write: a passing verdict is recorded, a sibling merge moves
    `main`, and the next merge attempt refuses. The verdict is intact on the
    board the whole time; only the base it was recorded against has moved.

    Nothing here relaxes the gate — every assertion below still expects the
    stale verdict to BLOCK. See the "no behaviour change" clause in #1640.
    """

    # ── helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _config(*, gates: list[str] | None = None):
        from dataclasses import dataclass as _dc, field as _f

        @_dc
        class _Reviews:
            enabled: bool = False

        @_dc
        class _Pipeline:
            default_gates: list[str] | None = None

        @_dc
        class _Cfg:
            reviews: _Reviews = _f(default_factory=_Reviews)
            pipeline: _Pipeline = _f(default_factory=_Pipeline)

        cfg = _Cfg()
        cfg.pipeline.default_gates = gates if gates is not None else ["test", "merge"]
        return cfg

    @staticmethod
    def _board(completed=None):
        from coord.models import Board
        return Board(active=[], completed=list(completed or []))

    @staticmethod
    def _tested_work(aid: str = "w1", *, base_sha: str = "base-old") -> Assignment:
        """A done work assignment carrying a PASSING verdict, anchored (per
        #1479) to the branch/base it was actually tested against."""
        return Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id=aid, type="work", status="done",
            branch=f"worker/{aid}",
            test_state="passed",
            test_head_sha="branch-sha",
            test_base_sha=base_sha,
            test_patch_id="patch-1",
        )

    @dataclass
    class _Gh(FakeGh):
        """FakeGh that answers the two freshness lookups. `base_sha` is what
        the target branch reads as *now* — set it different from the
        assignment's `test_base_sha` to simulate a sibling merge having moved
        main under an already-tested branch."""

        base_sha: str = "base-new"
        branch_sha: str = "branch-sha"
        # #1738: files the base-move compare (`test_base_sha`..`base_sha`)
        # reports as changed. None (default) means "compare unavailable" —
        # the inert-base check fails closed, same as the pre-#1738 behaviour.
        compare_files: list[str] | None = None
        # #1778: files the branch compare (`test_base_sha`..`test_head_sha`)
        # reports as changed — a distinct fixture from `compare_files` so a
        # test can make the base move non-inert while the branch itself is
        # (or vice versa). Discriminated by `head` below: the branch check
        # always asks for `head == branch_sha`, the base-move check for
        # whatever the current base SHA is.
        branch_compare_files: list[str] | None = None
        # #1847: every (base, head) pair passed to `get_compare_files`, in
        # call order — lets a test assert the fetch-once-per-side budget
        # (`_base_move_spared` must call this at most twice, regardless of
        # which #1479 escape hatch fires) without changing what the fake
        # returns.
        compare_files_calls: list[tuple[str, str]] = field(default_factory=list)

        def get_branch_sha(self, repo: str, branch: str) -> str | None:
            return self.base_sha if branch == "main" else self.branch_sha

        def get_branch_patch_id(self, repo: str, base: str, branch: str) -> str | None:
            return "patch-1"

        def get_compare_files(self, repo: str, base: str, head: str) -> list[str] | None:
            self.compare_files_calls.append((base, head))
            if head == self.branch_sha:
                return self.branch_compare_files
            return self.compare_files

    # ── defect 1: the message names the case ──────────────────────────────

    def test_moved_base_is_reported_as_stale_not_missing(self) -> None:
        board = self._board(completed=[self._tested_work()])
        entry = _q("w1", target="main")
        entry.target_branch_head_sha = "base-new"

        verdict = mq.evaluate_smoke_verdict(entry, board)

        assert verdict.ok is False, "a moved base must still BLOCK (#1479)"
        assert verdict.kind == mq.SMOKE_STALE
        assert verdict.anchor == "base"
        assert verdict.recorded_sha == "base-old"
        assert verdict.current_sha == "base-new"
        # The exact wording that mis-diagnosed #1640 must not appear.
        assert "no verdict recorded" not in (verdict.message or "")
        assert "stale" in (verdict.message or "")
        assert "base-old"[:7] in (verdict.message or "")
        assert "base-new"[:7] in (verdict.message or "")

    def test_no_verdict_at_all_is_still_reported_as_missing(self) -> None:
        """The genuine "never recorded" case keeps its original wording — the
        distinction is only useful if both halves are accurate."""
        work = self._tested_work()
        work.test_state = None
        board = self._board(completed=[work])

        verdict = mq.evaluate_smoke_verdict(_q("w1", target="main"), board)

        assert verdict.ok is False
        assert verdict.kind == mq.SMOKE_MISSING
        assert verdict.message == "smoke test required but no verdict recorded"

    def test_changed_branch_content_reports_the_branch_anchor(self) -> None:
        board = self._board(completed=[self._tested_work()])
        entry = _q("w1", target="main")
        entry.branch_head_sha = "branch-new"
        entry.branch_patch_id = "patch-2"  # content really did change

        verdict = mq.evaluate_smoke_verdict(entry, board)

        assert verdict.ok is False
        assert verdict.kind == mq.SMOKE_STALE
        assert verdict.anchor == "branch"
        assert "branch" in (verdict.message or "")

    def test_fresh_verdict_still_passes(self) -> None:
        """Guard against over-blocking: an unmoved base must still merge."""
        board = self._board(completed=[self._tested_work()])
        entry = _q("w1", target="main")
        entry.target_branch_head_sha = "base-old"

        verdict = mq.evaluate_smoke_verdict(entry, board)
        assert verdict.ok is True
        assert verdict.kind == mq.SMOKE_OK
        assert verdict.message is None

    # ── #1732: `skipped` is not subject to #1479 base-SHA freshness ───────
    #
    # `skipped` is a structural claim about the diff ("contract/fixture-only,
    # nothing to smoke-test" — #1076/#1152), not a measurement of code at a
    # SHA the way `passed` is. It must never be reported STALE just because
    # the base or branch moved — there is nothing to re-verify, and the only
    # way through used to be `--skip-smoke`, waiving a gate that had already
    # been correctly waived.

    def test_skipped_verdict_is_not_stale_when_base_moved(self) -> None:
        """The direct regression: a `skipped` verdict recorded against base X
        must not block when the base is now Y."""
        work = self._tested_work()
        work.test_state = "skipped"
        board = self._board(completed=[work])
        entry = _q("w1", target="main")
        entry.target_branch_head_sha = "base-new"  # base moved since recording

        verdict = mq.evaluate_smoke_verdict(entry, board)

        assert verdict.ok is True
        assert verdict.kind == mq.SMOKE_OK
        assert verdict.message is None

    def test_skipped_verdict_is_not_stale_when_branch_content_changed(self) -> None:
        """Same exemption applies to the branch-content-changed anchor, not
        just the base-moved one — `skipped` doesn't decay under either."""
        work = self._tested_work()
        work.test_state = "skipped"
        board = self._board(completed=[work])
        entry = _q("w1", target="main")
        entry.branch_head_sha = "branch-new"   # new commit pushed
        entry.branch_patch_id = "patch-2"       # content actually changed

        verdict = mq.evaluate_smoke_verdict(entry, board)

        assert verdict.ok is True
        assert verdict.kind == mq.SMOKE_OK

    def test_passed_verdict_still_goes_stale_when_base_moves(self) -> None:
        """#1479 must stay intact for `passed` — this fix must not over-reach
        into auto-waiving stale verdicts generally. Restates
        ``test_moved_base_is_reported_as_stale_not_missing`` side by side
        with the `skipped` exemption above so the two can't silently drift
        onto the same (wrong) behaviour."""
        board = self._board(completed=[self._tested_work()])  # test_state="passed"
        entry = _q("w1", target="main")
        entry.target_branch_head_sha = "base-new"

        verdict = mq.evaluate_smoke_verdict(entry, board)

        assert verdict.ok is False
        assert verdict.kind == mq.SMOKE_STALE

    # ── #1738: a content-irrelevant base move must not stale a verdict ────
    #
    # The base SHA moving is not, by itself, evidence that anything the test
    # suite could see actually changed — #1631's regression: a sibling merge
    # touched only `scripts/drive-batch.sh` and staled an otherwise-green
    # verdict, burning ~15 minutes of re-proof for nothing. When *gh_ops* can
    # confirm the base..base diff touches ONLY the #1738 inert allowlist
    # (docs/**, scripts/**, .github/ISSUE_TEMPLATE/**, top-level *.md), the
    # verdict stays fresh. Anything else — including a `.md` nested under a
    # non-allowlisted directory — stales exactly as before.

    def test_base_move_touching_only_scripts_stays_fresh(self) -> None:
        """The #1631 regression itself: base moved, diff is scripts-only."""
        board = self._board(completed=[self._tested_work()])
        entry = _q("w1", target="main")
        entry.target_branch_head_sha = "base-new"
        gh = self._Gh(compare_files=["scripts/drive-batch.sh"])

        verdict = mq.evaluate_smoke_verdict(entry, board, gh)

        assert verdict.ok is True
        assert verdict.kind == mq.SMOKE_OK

    def test_base_move_touching_coord_stays_stale(self) -> None:
        """Must not regress: a base move that touches `coord/**` is exactly
        the case #1479 exists to catch — stays stale."""
        board = self._board(completed=[self._tested_work()])
        entry = _q("w1", target="main")
        entry.target_branch_head_sha = "base-new"
        gh = self._Gh(compare_files=["coord/merge_queue.py"])

        verdict = mq.evaluate_smoke_verdict(entry, board, gh)

        assert verdict.ok is False
        assert verdict.kind == mq.SMOKE_STALE
        assert verdict.anchor == "base"

    def test_base_move_touching_docs_and_coord_stays_stale(self) -> None:
        """The allowlist is all-or-nothing — one non-inert file in the diff
        stales the whole move, even alongside inert ones."""
        board = self._board(completed=[self._tested_work()])
        entry = _q("w1", target="main")
        entry.target_branch_head_sha = "base-new"
        gh = self._Gh(compare_files=["docs/README.md", "coord/drive.py"])

        verdict = mq.evaluate_smoke_verdict(entry, board, gh)

        assert verdict.ok is False
        assert verdict.kind == mq.SMOKE_STALE

    def test_base_move_touching_nested_markdown_stays_stale(self) -> None:
        """Extension alone never qualifies: a `.md` nested under a
        non-allowlisted directory (e.g. a test contract, #1738) is NOT
        inert just because it ends in `.md` — only a top-level `*.md`
        (README.md, CONTRIBUTING.md, ...) is."""
        board = self._board(completed=[self._tested_work()])
        entry = _q("w1", target="main")
        entry.target_branch_head_sha = "base-new"
        gh = self._Gh(compare_files=["tests/acceptance/contract.md"])

        verdict = mq.evaluate_smoke_verdict(entry, board, gh)

        assert verdict.ok is False
        assert verdict.kind == mq.SMOKE_STALE

    def test_base_move_with_unreadable_compare_stays_stale(self) -> None:
        """`get_compare_files` returning None (compare unreadable) fails
        closed — #1738's "bias hard toward staling"."""
        board = self._board(completed=[self._tested_work()])
        entry = _q("w1", target="main")
        entry.target_branch_head_sha = "base-new"
        gh = self._Gh(compare_files=None)

        verdict = mq.evaluate_smoke_verdict(entry, board, gh)

        assert verdict.ok is False
        assert verdict.kind == mq.SMOKE_STALE

    def test_base_move_inert_but_branch_content_also_changed_stays_stale(
        self,
    ) -> None:
        """An inert base move does not short-circuit the branch-content
        check that follows it — both anchors must be fresh."""
        board = self._board(completed=[self._tested_work()])
        entry = _q("w1", target="main")
        entry.target_branch_head_sha = "base-new"
        entry.branch_head_sha = "branch-new"   # new commit pushed
        entry.branch_patch_id = "patch-2"       # content actually changed
        gh = self._Gh(compare_files=["scripts/drive-batch.sh"])

        verdict = mq.evaluate_smoke_verdict(entry, board, gh)

        assert verdict.ok is False
        assert verdict.kind == mq.SMOKE_STALE
        assert verdict.anchor == "branch"

    def test_has_smoke_verdict_still_returns_the_same_booleans(self) -> None:
        """The boolean seam every gate call site uses is unchanged."""
        board = self._board(completed=[self._tested_work()])
        stale_entry = _q("w1", target="main")
        stale_entry.target_branch_head_sha = "base-new"
        fresh_entry = _q("w1", target="main")
        fresh_entry.target_branch_head_sha = "base-old"

        assert mq.has_smoke_verdict(stale_entry, board) is False
        assert mq.has_smoke_verdict(fresh_entry, board) is True

    def test_process_error_string_names_the_moved_base(self) -> None:
        """`coord merge --only`'s wording — the string the operator reads."""
        cfg = self._config()
        board = self._board(completed=[self._tested_work()])
        items = [_q("w1", target="main", size=10)]

        events = process(items, self._Gh(), config=cfg, board=board)

        blocked = [e for e in events if e.kind == "smoke_required"]
        assert len(blocked) == 1
        assert "no verdict recorded" not in blocked[0].message
        assert "stale" in blocked[0].message
        assert items[0].error is not None and "stale" in items[0].error

    def test_dry_run_uses_the_same_stale_wording(self) -> None:
        cfg = self._config()
        board = self._board(completed=[self._tested_work()])
        items = [_q("w1", target="main", size=10)]

        events = process(
            items, self._Gh(), config=cfg, board=board, dry_run=True
        )

        blocked = [e for e in events if e.kind == "smoke_required"]
        assert len(blocked) == 1
        assert "stale" in blocked[0].message
        assert "no verdict recorded" not in blocked[0].message

    # ── defect 2: --plan and --only agree ─────────────────────────────────

    def test_plan_and_only_agree_after_the_base_moves(self, coord_db) -> None:
        """The #1640 acceptance sequence.

        Record a passing verdict, move the base, then ask both readers about
        the SAME entry: `plan()` (what `coord merge --plan` renders) and
        `process()` (what `coord merge --only` runs). Before #1640 the former
        said READY and the latter refused.
        """
        cfg = self._config()
        board = self._board(completed=[self._tested_work()])
        gh = self._Gh()  # main now reads base-new; the verdict says base-old
        save_queue([_q("w1", target="main", size=10)])

        planned = mq.plan(board, cfg, gh_ops=gh)
        assert len(planned) == 1
        assert planned[0].status == mq.PLAN_BLOCKED, (
            "--plan must not show READY for a verdict --only refuses"
        )
        assert "stale" in (planned[0].reason or "")
        assert "missing" not in (planned[0].reason or "")

        items = mq.load_queue()
        events = process(items, gh, config=cfg, board=board)
        refusals = [e for e in events if e.kind == "smoke_required"]
        assert len(refusals) == 1, "the gate must still block (#1479 unchanged)"
        assert "stale" in refusals[0].message

    def test_plan_and_only_agree_when_the_verdict_is_fresh(self, coord_db) -> None:
        """Same two readers, unmoved base → both say go. Agreement has to
        hold in the passing direction too, or the fix is just "block more"."""
        cfg = self._config()
        board = self._board(completed=[self._tested_work(base_sha="base-new")])
        gh = self._Gh()
        save_queue([_q("w1", target="main", size=10)])

        planned = mq.plan(board, cfg, gh_ops=gh)
        assert planned[0].status == mq.PLAN_READY
        assert planned[0].reason is None

        items = mq.load_queue()
        events = process(items, gh, config=cfg, board=board, dry_run=True)
        assert not [e for e in events if e.kind == "smoke_required"]

    def test_plan_gate_status_reason_names_staleness(self) -> None:
        board = self._board(completed=[self._tested_work()])
        entry = _q("w1", target="main")
        entry.target_branch_head_sha = "base-new"

        status, reason = mq._entry_gate_status(entry, board, self._config())

        assert status == mq.PLAN_BLOCKED
        assert reason is not None
        assert "stale" in reason and "missing" not in reason

    # ── the staging path (merge_queue.py's raw test_state read) ───────────

    def test_staging_item_blocks_on_a_stale_verdict(self, coord_db) -> None:
        """#1640 defect 2, staging half: the section used to read the raw
        `test_state` column with no freshness check and show READY."""
        from types import SimpleNamespace

        cfg = self._config()
        cfg.repo = lambda name: SimpleNamespace(  # type: ignore[attr-defined]
            github="acme/api", default_branch="main"
        )
        board = self._board(completed=[self._tested_work()])
        save_queue([])

        items = mq.staging_items(board, cfg, gh_ops=self._Gh())

        assert len(items) == 1
        assert items[0].status == mq.STAGING_BLOCKED
        assert "stale" in (items[0].reason or "")

    def test_staging_item_ready_when_verdict_is_fresh(self, coord_db) -> None:
        from types import SimpleNamespace

        cfg = self._config()
        cfg.repo = lambda name: SimpleNamespace(  # type: ignore[attr-defined]
            github="acme/api", default_branch="main"
        )
        board = self._board(completed=[self._tested_work(base_sha="base-new")])
        save_queue([])

        items = mq.staging_items(board, cfg, gh_ops=self._Gh())

        assert len(items) == 1
        assert items[0].status == mq.STAGING_READY
        assert items[0].reason is None

    def test_staging_without_gh_ops_makes_no_calls(self, coord_db) -> None:
        """The `/board` read path's no-live-I/O contract: gh_ops=None means
        the freshness anchors are simply unavailable, never a blind `gh` call."""
        from types import SimpleNamespace

        cfg = self._config()
        cfg.repo = lambda name: SimpleNamespace(  # type: ignore[attr-defined]
            github="acme/api", default_branch="main"
        )
        board = self._board(completed=[self._tested_work()])
        save_queue([])

        items = mq.staging_items(board, cfg)

        assert len(items) == 1
        assert items[0].status == mq.STAGING_READY

    # ── display_error must not clear a staleness refusal ──────────────────

    def test_display_error_keeps_a_stale_refusal(self) -> None:
        """`display_error` recomputes I/O-free, so it can see the terminal
        verdict but not the anchors. Clearing on that evidence would put the
        false green back on `coord status`."""
        cfg = self._config()
        board = self._board(completed=[self._tested_work()])
        entry = _q("w1", target="main")
        entry.error = (
            "smoke test verdict is stale: recorded against base base-ol, "
            "base is now base-ne — re-verify"
        )

        assert mq.display_error(entry, board, cfg) == entry.error

    def test_display_error_still_clears_a_satisfied_missing_verdict(self) -> None:
        """#420's original behaviour for the "never recorded" string is
        untouched: once a verdict lands, the stored string stops showing."""
        cfg = self._config()
        board = self._board(completed=[self._tested_work()])
        entry = _q("w1", target="main")
        entry.error = "smoke test required but no verdict recorded"

        assert mq.display_error(entry, board, cfg) is None

    def test_plan_through_the_daemon_gate_snapshot_also_blocks(
        self, coord_db
    ) -> None:
        """#1640 end-to-end for the daemon-fronted setup.

        `/board` (and therefore `coord merge --plan` against a daemon) passes
        the tick-refreshed `GateSnapshot` as gh_ops. It used not to implement
        `get_branch_sha` at all; `evaluate_smoke_verdict` swallowed the
        AttributeError and every staleness check became a no-op, so the plan
        rendered READY for the entry `--only` refused. The snapshot now
        serves the anchors from its own refreshed data.
        """
        from coord.gate_snapshot import GateSnapshot

        cfg = self._config()
        board = self._board(completed=[self._tested_work()])
        save_queue([_q("w1", target="main", size=10)])

        snapshot = GateSnapshot(
            branch_shas={
                ("acme/api", "main"): "base-new",       # a sibling merge landed
                ("acme/api", "worker/w1"): "branch-sha",
            },
            branch_patch_ids={("acme/api", "main", "worker/w1"): "patch-1"},
        )

        planned = mq.plan(board, cfg, gh_ops=snapshot)

        assert planned[0].status == mq.PLAN_BLOCKED
        assert "stale" in (planned[0].reason or "")

    def test_plan_through_an_empty_gate_snapshot_fails_open(self, coord_db) -> None:
        """A snapshot that hasn't refreshed yet knows no SHAs. That must read
        as "anchor unavailable" (fail open, today's behaviour for a `gh` that
        errors) — a cold daemon must not blanket-block every entry."""
        from coord.gate_snapshot import GateSnapshot

        cfg = self._config()
        board = self._board(completed=[self._tested_work()])
        save_queue([_q("w1", target="main", size=10)])

        planned = mq.plan(board, cfg, gh_ops=GateSnapshot())

        assert planned[0].status == mq.PLAN_READY

    # ── #1778: an inert BRANCH must not stale on a (possibly substantive)
    # base move — the mirror of #1738's inert-base-move rule. ──────────────
    #
    # `_base_move_is_inert` asks "did the base move through anything that
    # matters"; `_branch_is_inert` asks "does the branch touch anything that
    # matters" at all, independent of the base move's own content. Either
    # one being true is enough to skip staling the verdict on a base move.

    def test_inert_branch_survives_a_non_inert_base_move(self) -> None:
        """#1756's shape (PR #1774): a branch whose entire diff is
        docs/*.md/scripts content merges without revalidation even though
        the base moved through something substantive (`coord/**`) in the
        meantime — `deploy/**` deliberately omitted, see #1778's
        out-of-scope note; this exercises only what's already allowlisted."""
        board = self._board(completed=[self._tested_work()])
        entry = _q("w1", target="main")
        entry.target_branch_head_sha = "base-new"
        gh = self._Gh(
            compare_files=["coord/merge_queue.py"],  # base move: NOT inert
            branch_compare_files=[  # branch: entirely inert (#1756 shape)
                "CLAUDE.md",
                "docs/AGENT_OPERATIONS.md",
                "docs/DRIVE_QUEUE.md",
                "docs/OPERATING_GOTCHAS.md",
                "scripts/drive-batch.sh",
            ],
        )

        verdict = mq.evaluate_smoke_verdict(entry, board, gh)

        assert verdict.ok is True
        assert verdict.kind == mq.SMOKE_OK

    def test_branch_touching_coord_stays_stale_on_base_move(self) -> None:
        """The whole safety story: a branch that touches `coord/**` stales
        exactly as before a non-inert base move, regardless of #1778."""
        board = self._board(completed=[self._tested_work()])
        entry = _q("w1", target="main")
        entry.target_branch_head_sha = "base-new"
        gh = self._Gh(
            compare_files=["coord/merge_queue.py"],
            branch_compare_files=["coord/merge_queue.py", "docs/README.md"],
        )

        verdict = mq.evaluate_smoke_verdict(entry, board, gh)

        assert verdict.ok is False
        assert verdict.kind == mq.SMOKE_STALE
        assert verdict.anchor == "base"

    def test_branch_touching_tests_dir_stays_stale_on_base_move(self) -> None:
        """The branch's own diff also touches the base-moved file — #1847's
        disjointness escape hatch must not fire on an *overlapping* pair, so
        this is deliberately not disjoint from `compare_files` (that shape is
        covered separately by the #1847 tests below)."""
        board = self._board(completed=[self._tested_work()])
        entry = _q("w1", target="main")
        entry.target_branch_head_sha = "base-new"
        gh = self._Gh(
            compare_files=["coord/merge_queue.py"],
            branch_compare_files=["coord/merge_queue.py", "tests/test_merge_queue.py"],
        )

        verdict = mq.evaluate_smoke_verdict(entry, board, gh)

        assert verdict.ok is False
        assert verdict.kind == mq.SMOKE_STALE

    def test_branch_touching_tui_stays_stale_on_base_move(self) -> None:
        """Overlapping with the base move (see the docstring above) — #1847
        must not spare a branch that shares a file with the base's diff."""
        board = self._board(completed=[self._tested_work()])
        entry = _q("w1", target="main")
        entry.target_branch_head_sha = "base-new"
        gh = self._Gh(
            compare_files=["coord/merge_queue.py"],
            branch_compare_files=["coord/merge_queue.py", "tui/app.py"],
        )

        verdict = mq.evaluate_smoke_verdict(entry, board, gh)

        assert verdict.ok is False
        assert verdict.kind == mq.SMOKE_STALE

    def test_branch_touching_pyproject_stays_stale_on_base_move(self) -> None:
        """Overlapping with the base move (see the docstring above) — #1847
        must not spare a branch that shares a file with the base's diff."""
        board = self._board(completed=[self._tested_work()])
        entry = _q("w1", target="main")
        entry.target_branch_head_sha = "base-new"
        gh = self._Gh(
            compare_files=["coord/merge_queue.py"],
            branch_compare_files=["coord/merge_queue.py", "pyproject.toml"],
        )

        verdict = mq.evaluate_smoke_verdict(entry, board, gh)

        assert verdict.ok is False
        assert verdict.kind == mq.SMOKE_STALE

    def test_branch_editing_the_test_runner_is_not_inert(self) -> None:
        """The self-certification hole: a branch cannot point at the
        `scripts/` allowlist to declare its own edit of the composed test
        runner inert and skip its gate. Overlapping with the base move (see
        the docstring above `test_branch_touching_tui_stays_stale_on_base_move`)
        so #1847's disjointness hatch can't spare it either — the point of
        this test is the deny-list, not disjointness."""
        board = self._board(completed=[self._tested_work()])
        entry = _q("w1", target="main")
        entry.target_branch_head_sha = "base-new"
        gh = self._Gh(
            compare_files=["coord/merge_queue.py"],
            branch_compare_files=[
                "coord/merge_queue.py", "scripts/coord-test-runner.sh",
            ],
        )

        verdict = mq.evaluate_smoke_verdict(entry, board, gh)

        assert verdict.ok is False
        assert verdict.kind == mq.SMOKE_STALE

    def test_base_move_editing_the_test_runner_is_not_inert(self) -> None:
        """Same hole, base side: a base move consisting solely of an edit to
        the composed test runner must not be treated as inert either — it IS
        the thing the Test stage executes."""
        board = self._board(completed=[self._tested_work()])
        entry = _q("w1", target="main")
        entry.target_branch_head_sha = "base-new"
        gh = self._Gh(compare_files=["scripts/coord-test-runner.sh"])

        verdict = mq.evaluate_smoke_verdict(entry, board, gh)

        assert verdict.ok is False
        assert verdict.kind == mq.SMOKE_STALE
        assert verdict.anchor == "base"

    def test_inert_branch_with_no_verdict_at_all_is_still_missing(self) -> None:
        """#1778 refreshes an EXISTING verdict against a moved base — it must
        never manufacture a verdict from nothing, inert branch or not."""
        work = self._tested_work()
        work.test_state = None
        board = self._board(completed=[work])
        entry = _q("w1", target="main")
        entry.target_branch_head_sha = "base-new"
        gh = self._Gh(
            compare_files=["coord/merge_queue.py"],
            branch_compare_files=["docs/README.md"],
        )

        verdict = mq.evaluate_smoke_verdict(entry, board, gh)

        assert verdict.ok is False
        assert verdict.kind == mq.SMOKE_MISSING

    def test_branch_is_inert_fails_closed_without_gh_ops(self) -> None:
        assert mq._branch_is_inert(None, "acme/api", "base-old", "branch-sha") is False

    def test_branch_is_inert_fails_closed_without_repo_github(self) -> None:
        assert mq._branch_is_inert(self._Gh(), None, "base-old", "branch-sha") is False

    def test_branch_is_inert_fails_closed_on_unreadable_compare(self) -> None:
        gh = self._Gh(branch_compare_files=None)
        assert (
            mq._branch_is_inert(gh, "acme/api", "base-old", "branch-sha") is False
        )

    def test_branch_is_inert_fails_closed_when_compare_raises(self) -> None:
        class _Raising(self._Gh):
            def get_compare_files(self, repo, base, head):
                raise RuntimeError("gh api boom")

        assert (
            mq._branch_is_inert(_Raising(), "acme/api", "base-old", "branch-sha")
            is False
        )

    def test_branch_is_inert_true_for_allowlisted_paths(self) -> None:
        gh = self._Gh(branch_compare_files=["docs/README.md", "CONTRIBUTING.md"])
        assert mq._branch_is_inert(gh, "acme/api", "base-old", "branch-sha") is True

    def test_path_is_inert_denies_the_test_runner_despite_scripts_prefix(
        self,
    ) -> None:
        assert mq._path_is_inert("scripts/coord-test-runner.sh") is False
        assert mq._path_is_inert("scripts/drive-batch.sh") is True

    # ── black-box: seeded board, via `process()` ────────────────────────────

    def test_seeded_board_inert_branch_and_moved_base_merges(self) -> None:
        """The positive case: an inert branch merges through `process()`
        with no re-test dispatched, even though the base moved through
        something substantive."""
        cfg = self._config()
        board = self._board(completed=[self._tested_work()])
        items = [_q("w1", target="main", size=10)]
        gh = self._Gh(
            compare_files=["coord/merge_queue.py"],
            branch_compare_files=["docs/README.md"],
        )

        events = process(items, gh, config=cfg, board=board, dry_run=True)

        assert not [e for e in events if e.kind == "smoke_required"]

    def test_seeded_board_same_branch_plus_coord_file_blocks(self) -> None:
        """The negative case, which matters more: the same branch with one
        `coord/**` file added flips straight back to blocked."""
        cfg = self._config()
        board = self._board(completed=[self._tested_work()])
        items = [_q("w1", target="main", size=10)]
        gh = self._Gh(
            compare_files=["coord/merge_queue.py"],
            branch_compare_files=["docs/README.md", "coord/merge_queue.py"],
        )

        events = process(items, gh, config=cfg, board=board, dry_run=True)

        blocked = [e for e in events if e.kind == "smoke_required"]
        assert len(blocked) == 1
        assert "stale" in blocked[0].message

    # ── #1847: disjoint base-move and branch file sets ─────────────────────
    #
    # #1738 and #1778 are both allowlist-based: "is this one diff inert on
    # its own". Neither helps the common case that actually costs a human
    # intervention on a queue drain — a substantive base move and a
    # substantive branch that simply have nothing to do with each other. This
    # third escape hatch asks that question directly: do the two file sets
    # intersect at all.

    def test_disjoint_base_move_and_branch_spares_the_verdict(self) -> None:
        """The issue's acceptance case 1: base move touches `coord/state.py`,
        branch touches `tui/src/app/render.rs` — no overlap, neither side is
        on the #1738/#1778 allowlist, and the verdict still survives."""
        board = self._board(completed=[self._tested_work()])
        entry = _q("w1", target="main")
        entry.target_branch_head_sha = "base-new"
        gh = self._Gh(
            compare_files=["coord/state.py"],
            branch_compare_files=["tui/src/app/render.rs"],
        )

        verdict = mq.evaluate_smoke_verdict(entry, board, gh)

        assert verdict.ok is True
        assert verdict.kind == mq.SMOKE_OK
        assert verdict.spared_reason == (
            "base move and branch touch disjoint files (#1847)"
        )

    def test_overlapping_base_move_and_branch_stays_stale(self) -> None:
        """The issue's acceptance case 2: base move touches
        `coord/state.py`, branch touches `coord/state.py` and
        `coord/drive.py` — the shared file means the two diffs are NOT
        disjoint, so #1847 does not apply and the verdict stales exactly as
        today."""
        board = self._board(completed=[self._tested_work()])
        entry = _q("w1", target="main")
        entry.target_branch_head_sha = "base-new"
        gh = self._Gh(
            compare_files=["coord/state.py"],
            branch_compare_files=["coord/state.py", "coord/drive.py"],
        )

        verdict = mq.evaluate_smoke_verdict(entry, board, gh)

        assert verdict.ok is False
        assert verdict.kind == mq.SMOKE_STALE
        assert verdict.anchor == "base"

    def test_disjoint_check_fails_closed_without_gh_ops(self) -> None:
        """No *gh_ops* to ask ⇒ neither file list can be fetched ⇒ stale, not
        spared — the fail-closed posture #1738/#1778 already have."""
        board = self._board(completed=[self._tested_work()])
        entry = _q("w1", target="main")
        entry.target_branch_head_sha = "base-new"

        verdict = mq.evaluate_smoke_verdict(entry, board, None)

        assert verdict.ok is False
        assert verdict.kind == mq.SMOKE_STALE
        assert verdict.anchor == "base"

    def test_disjoint_check_fails_closed_when_compare_returns_none(self) -> None:
        """`get_compare_files` answering ``None`` (unreadable compare) on
        either side must not be read as "disjoint" — that would let an
        unproven combination merge."""
        board = self._board(completed=[self._tested_work()])
        entry = _q("w1", target="main")
        entry.target_branch_head_sha = "base-new"
        gh = self._Gh(compare_files=None, branch_compare_files=None)

        verdict = mq.evaluate_smoke_verdict(entry, board, gh)

        assert verdict.ok is False
        assert verdict.kind == mq.SMOKE_STALE

    def test_disjoint_check_fails_closed_when_compare_raises(self) -> None:
        """A raising compare call must degrade to stale, not to "disjoint by
        default"."""
        class _Raising(self._Gh):
            def get_compare_files(self, repo, base, head):
                raise RuntimeError("gh api boom")

        board = self._board(completed=[self._tested_work()])
        entry = _q("w1", target="main")
        entry.target_branch_head_sha = "base-new"

        verdict = mq.evaluate_smoke_verdict(entry, board, _Raising())

        assert verdict.ok is False
        assert verdict.kind == mq.SMOKE_STALE

    def test_base_move_disjoint_from_branch_is_a_pure_predicate(self) -> None:
        """Direct unit coverage of the new predicate, independent of the
        `evaluate_smoke_verdict` plumbing around it."""
        assert mq._base_move_disjoint_from_branch(
            ["coord/state.py"], ["tui/src/app/render.rs"]
        ) is True
        assert mq._base_move_disjoint_from_branch(
            ["coord/state.py"], ["coord/state.py", "coord/drive.py"]
        ) is False
        assert mq._base_move_disjoint_from_branch(None, ["coord/drive.py"]) is False
        assert mq._base_move_disjoint_from_branch(["coord/state.py"], None) is False
        assert mq._base_move_disjoint_from_branch(None, None) is False
        # Both empty (a compare that legitimately touched nothing) is
        # vacuously disjoint — there is nothing to overlap on.
        assert mq._base_move_disjoint_from_branch([], []) is True

    def test_get_compare_files_called_at_most_twice_when_disjoint_fires(
        self,
    ) -> None:
        """#1847's net-I/O promise: even though three disjuncts are now
        consulted (#1738, #1778, #1847), `get_compare_files` is called at
        most twice total — once per side — because the disjointness check
        reuses the two lists the first two checks already fetched."""
        board = self._board(completed=[self._tested_work()])
        entry = _q("w1", target="main")
        entry.target_branch_head_sha = "base-new"
        gh = self._Gh(
            compare_files=["coord/state.py"],
            branch_compare_files=["tui/src/app/render.rs"],
        )

        verdict = mq.evaluate_smoke_verdict(entry, board, gh)

        assert verdict.ok is True
        assert len(gh.compare_files_calls) <= 2

    def test_get_compare_files_called_once_when_base_move_alone_is_inert(
        self,
    ) -> None:
        """Ordering is preserved: the #1738 base-inert check is tried first
        and, when it alone settles the question, the branch side is never
        fetched at all — one call, not two."""
        board = self._board(completed=[self._tested_work()])
        entry = _q("w1", target="main")
        entry.target_branch_head_sha = "base-new"
        gh = self._Gh(compare_files=["docs/README.md"])

        verdict = mq.evaluate_smoke_verdict(entry, board, gh)

        assert verdict.ok is True
        assert verdict.spared_reason == "base move touches only inert paths (#1738)"
        assert len(gh.compare_files_calls) == 1

    def test_get_compare_files_called_at_most_twice_when_nothing_spares_it(
        self,
    ) -> None:
        """The plain-stale path (no disjunct fires) is still bounded at two
        calls — the pre-#1847 worst case, unchanged."""
        board = self._board(completed=[self._tested_work()])
        entry = _q("w1", target="main")
        entry.target_branch_head_sha = "base-new"
        gh = self._Gh(
            compare_files=["coord/state.py"],
            branch_compare_files=["coord/state.py", "coord/drive.py"],
        )

        verdict = mq.evaluate_smoke_verdict(entry, board, gh)

        assert verdict.ok is False
        assert len(gh.compare_files_calls) <= 2

    def test_disjoint_spare_does_not_short_circuit_the_branch_content_check(
        self,
    ) -> None:
        """#1847 only answers the base-move question. A branch that gains
        real content after its verdict was recorded is still caught by the
        separate patch-id-based branch-content check that follows,
        independent of whether the base move was disjoint from it."""
        board = self._board(completed=[self._tested_work()])
        entry = _q("w1", target="main")
        entry.target_branch_head_sha = "base-new"
        entry.branch_head_sha = "branch-new"   # new commit pushed
        entry.branch_patch_id = "patch-2"       # content actually changed
        gh = self._Gh(
            compare_files=["coord/state.py"],
            branch_compare_files=["tui/src/app/render.rs"],
        )

        verdict = mq.evaluate_smoke_verdict(entry, board, gh)

        assert verdict.ok is False
        assert verdict.kind == mq.SMOKE_STALE
        assert verdict.anchor == "branch"

    def test_dry_run_names_disjointness_distinctly_from_the_inert_reasons(
        self,
    ) -> None:
        """`coord merge --dry-run`'s "would merge" preview names *why* a
        base move didn't stale the verdict, and the #1847 wording is
        distinguishable from the #1738/#1778 inert wordings."""
        cfg = self._config()
        board = self._board(completed=[self._tested_work()])
        items = [_q("w1", target="main", size=10)]
        gh = self._Gh(
            compare_files=["coord/state.py"],
            branch_compare_files=["tui/src/app/render.rs"],
        )

        events = process(items, gh, config=cfg, board=board, dry_run=True)

        merged = [e for e in events if e.kind == "merged"]
        assert len(merged) == 1
        assert "disjoint" in merged[0].message
        assert "#1847" in merged[0].message
        assert "#1738" not in merged[0].message
        assert "#1778" not in merged[0].message

    def test_dry_run_names_the_1738_inert_reason_distinctly(self) -> None:
        """Same preview, but spared via the #1738 inert-base-move hatch
        instead — the wording must differ from the #1847 one above."""
        cfg = self._config()
        board = self._board(completed=[self._tested_work()])
        items = [_q("w1", target="main", size=10)]
        gh = self._Gh(compare_files=["docs/README.md"])

        events = process(items, gh, config=cfg, board=board, dry_run=True)

        merged = [e for e in events if e.kind == "merged"]
        assert len(merged) == 1
        assert "#1738" in merged[0].message
        assert "#1847" not in merged[0].message

    # ── #2704: a live probe that CONFIRMS it could not read GitHub (a rate
    # limit, auth failure, or network blip) must fail closed, distinctly from
    # both a genuine "no verdict recorded" and a genuine "confirmed stale" —
    # never fall through to SMOKE_OK on evidence this call never obtained.

    @dataclass
    class _TransientFailGh(FakeGh):
        """A gh_ops whose `get_branch_sha` supports the #2704 opt-in
        `raise_on_transient` flag (mirroring the real
        `coord.github_ops.get_branch_sha`) and raises `GhTransientError`
        for it — simulating a live client hitting a rate limit/auth/network
        failure, as opposed to `FakeGh`'s plain "no SHA tracking exercised"
        default."""

        def get_branch_sha(
            self, repo: str, branch: str, *, raise_on_transient: bool = False
        ) -> str | None:
            if raise_on_transient:
                from coord.github_ops import GhTransientError
                raise GhTransientError("HTTP 403: API rate limit exceeded")
            return None

    def test_confirmed_transient_failure_is_unknown_not_ok(self) -> None:
        board = self._board(completed=[self._tested_work()])
        entry = _q("w1", target="main")  # no live SHAs populated yet

        verdict = mq.evaluate_smoke_verdict(entry, board, self._TransientFailGh())

        assert verdict.ok is False, (
            "a verdict this call could never confirm fresh must not pass"
        )
        assert verdict.kind == mq.SMOKE_UNKNOWN
        assert verdict.message == mq.UNKNOWN_BRANCH_HEAD_REASON

    def test_confirmed_transient_failure_outranks_a_stale_verdict(self) -> None:
        """When BOTH conditions are present in the same chain, "cannot
        confirm" must win — it is the more honest statement (#2704)."""
        work = self._tested_work()
        board = self._board(completed=[work])
        entry = _q("w1", target="main")
        entry.branch_head_sha = "branch-sha"  # confirmed unchanged already

        verdict = mq.evaluate_smoke_verdict(entry, board, self._TransientFailGh())

        assert verdict.kind == mq.SMOKE_UNKNOWN

    def test_a_cache_miss_still_fails_open_not_unknown(self) -> None:
        """The #1640 `GateSnapshot` convention — a `get_branch_sha` that
        simply doesn't support `raise_on_transient` and returns `None` —
        must keep its EXACT pre-#2704 fail-open behaviour. Only a CONFIRMED
        transient failure (the test above) is SMOKE_UNKNOWN."""
        board = self._board(completed=[self._tested_work()])
        entry = _q("w1", target="main")

        verdict = mq.evaluate_smoke_verdict(entry, board, FakeGh())

        assert verdict.ok is True
        assert verdict.kind == mq.SMOKE_OK


# ══════════════════════════════════════════════════════════════════════════════
# #2705 Case 1: the refusal must name the WINNING (newest) row in a
# bounce/fix chain, never whichever stale row pool order surfaces first.
# ══════════════════════════════════════════════════════════════════════════════

class TestSmokeStaleReportsWinningRow:
    """quadraui#595, 2026-08-24: four work rows piled up on one branch across
    a bounce/fix chain. `branch_work` is built from `board.completed +
    board.active` in POOL order — never chronological — and the
    ``if stale is None:`` latch inside `evaluate_smoke_verdict`'s loop kept
    only the FIRST stale row it happened to iterate over. That was
    frequently the oldest, long-superseded round, so both the reported
    anchor and the `coord test <aid> --passed` remedy named an assignment
    nobody was merging.

    `_uat_branch_work` already sorts its branch-work population newest-first
    for exactly this reason ("a bounce/fix round's fresh work assignment is
    a new thing for the operator to look at, so an older sibling's stale
    verdict must not paper over it") — the smoke gate wants the same rule.
    """

    @staticmethod
    def _board(completed=None, active=None):
        from coord.models import Board
        return Board(active=list(active or []), completed=list(completed or []))

    @staticmethod
    def _work(
        aid: str,
        *,
        dispatched_at: float,
        base_sha: str = "base-old",
        head_sha: str = "branch-old",
        test_state: str | None = "passed",
    ) -> Assignment:
        return Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id=aid, type="work", status="done",
            branch="worker/issue-1", dispatched_at=dispatched_at,
            test_state=test_state,
            test_head_sha=head_sha,
            test_base_sha=base_sha,
            test_patch_id="patch-1",
        )

    def test_reports_the_newest_stale_row_not_the_first_in_pool_order(self) -> None:
        # Oldest round first, newest round last — i.e. `board.completed`'s
        # natural accumulation order, and exactly the ordering the fix must
        # NOT trust.
        oldest = self._work("round-1", dispatched_at=100.0, base_sha="base-ancient")
        mid = self._work("round-2", dispatched_at=200.0, base_sha="base-old")
        newest = self._work("round-3", dispatched_at=300.0, base_sha="base-old")
        board = self._board(completed=[oldest, mid, newest])

        entry = _q("round-3", branch="worker/issue-1", target="main")
        entry.target_branch_head_sha = "base-new"  # moved since every round

        verdict = mq.evaluate_smoke_verdict(entry, board)

        assert verdict.ok is False
        assert verdict.kind == mq.SMOKE_STALE
        assert verdict.assignment_id == "round-3", (
            "must name the newest (winning) row, not whichever stale row "
            "pool order happens to surface first"
        )

    def test_remedy_names_the_winning_assignment_not_the_superseded_one(self) -> None:
        oldest = self._work("round-1", dispatched_at=100.0)
        newest = self._work("round-2", dispatched_at=200.0)
        board = self._board(completed=[oldest, newest])
        entry = _q("round-2", branch="worker/issue-1", target="main")
        entry.target_branch_head_sha = "base-new"

        verdict = mq.evaluate_smoke_verdict(entry, board)

        assert "coord test round-2 --passed" in (verdict.message or "")
        assert "round-1" not in (verdict.message or "")

    def test_missing_verdict_fallback_also_names_the_newest_row(self) -> None:
        """No terminal verdict anywhere in the chain: SMOKE_MISSING must
        still report the most-recently-dispatched row, not whichever row
        happens to be first in pool order."""
        oldest = self._work("round-1", dispatched_at=100.0, test_state=None)
        newest = self._work("round-2", dispatched_at=200.0, test_state=None)
        board = self._board(completed=[oldest, newest])
        entry = _q("round-2", branch="worker/issue-1", target="main")

        verdict = mq.evaluate_smoke_verdict(entry, board)

        assert verdict.kind == mq.SMOKE_MISSING
        assert verdict.assignment_id == "round-2"

    def test_revalidation_candidate_names_the_winning_row(self) -> None:
        """`RevalidationCandidate.work_assignment_id` is populated straight
        from `evaluate_smoke_verdict`'s `assignment_id` — fixing the
        selection there fixes `--revalidate`'s eligibility for free, with no
        separate change needed."""
        from dataclasses import dataclass as _dc, field as _f

        @_dc
        class _Pipeline:
            default_gates: list = _f(default_factory=lambda: ["test", "merge"])

        @_dc
        class _Reviews:
            enabled: bool = False

        @_dc
        class _Cfg:
            reviews: _Reviews = _f(default_factory=_Reviews)
            pipeline: _Pipeline = _f(default_factory=_Pipeline)

        oldest = self._work("round-1", dispatched_at=100.0)
        newest = self._work("round-2", dispatched_at=200.0)
        board = self._board(completed=[oldest, newest])
        entry = _q("round-2", branch="worker/issue-1", target="main")
        entry.target_branch_head_sha = "base-new"

        candidates = mq.revalidation_candidates([entry], board, _Cfg())

        assert [c.work_assignment_id for c in candidates] == ["round-2"]


class TestRevalidationCandidatesComposeWithSkipReview:
    """#3107: ``coord merge --only X --skip-review --revalidate`` must waive
    the review gate *before* asking whether an entry is blocked solely on
    staleness — otherwise the one combination an operator actually needs (an
    entry that is both review-blocked and stale) is inexpressible, even
    though each flag alone handles its own gate.

    claude-coordinator#3083, 2026-09-04: a `request-changes` review verdict
    plus a stale-but-passed test verdict. `--skip-review` printed its waiver
    line, then `--revalidate` refused anyway because its "blocked solely on
    staleness" predicate was still evaluated against the unwaived gate set.
    """

    @staticmethod
    def _board(completed=None):
        from coord.models import Board
        return Board(active=[], completed=list(completed or []))

    @staticmethod
    def _work(aid: str = "w1") -> Assignment:
        return Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id=aid, type="work", status="done",
            branch=f"worker/{aid}", test_state="passed",
            test_head_sha="branch-sha", test_base_sha="base-old",
            test_patch_id="patch-1",
        )

    @staticmethod
    def _request_changes_review(aid: str, of: str) -> Assignment:
        return Assignment(
            machine_name="m2", repo_name="api", issue_number=1,
            issue_title="[review] t",
            assignment_id=aid, type="review", status="done",
            branch="worker/w1",
            review_of_assignment_id=of,
            review_verdict="request-changes",
        )

    @staticmethod
    def _config():
        from dataclasses import dataclass, field as dc_field
        @dataclass
        class _Reviews:
            enabled: bool = True
        @dataclass
        class _Pipeline:
            default_gates: list[str] | None = None
        @dataclass
        class _Cfg:
            reviews: _Reviews = dc_field(default_factory=_Reviews)
            pipeline: _Pipeline = dc_field(default_factory=_Pipeline)
        cfg = _Cfg()
        cfg.pipeline.default_gates = ["review", "test", "merge"]
        return cfg

    def _entry(self) -> QueuedMerge:
        entry = _q("w1", branch="worker/w1", target="main")
        entry.target_branch_head_sha = "base-new"  # moved since the test ran
        return entry

    def test_review_blocked_and_stale_entry_is_not_a_candidate_without_the_waiver(
        self,
    ) -> None:
        board = self._board(
            completed=[self._work(), self._request_changes_review("r1", "w1")],
        )
        candidates = mq.revalidation_candidates([self._entry()], board, self._config())

        assert candidates == []

    def test_skip_review_makes_the_same_entry_a_candidate(self) -> None:
        """The fix: pass `skip_review=True` (mirroring the CLI's own
        `--skip-review`) and the entry becomes eligible — it is now blocked
        solely on staleness, exactly as the workaround (a fresh `approve`
        review) would have left it."""
        board = self._board(
            completed=[self._work(), self._request_changes_review("r1", "w1")],
        )
        candidates = mq.revalidation_candidates(
            [self._entry()], board, self._config(), skip_review=True,
        )

        assert [c.work_assignment_id for c in candidates] == ["w1"]

    def test_skip_review_does_not_manufacture_candidates_for_other_blocks(self) -> None:
        """`--skip-review` only ever discards the review failure — an entry
        that is ALSO missing a smoke verdict (not merely stale) must stay
        out of scope, same as #1769's original "missing is not stale" rule."""
        work = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="w2", type="work", status="done",
            branch="worker/w2", test_state=None,
        )
        review = self._request_changes_review("r2", "w2")
        board = self._board(completed=[work, review])
        entry = _q("w2", branch="worker/w2", target="main")

        candidates = mq.revalidation_candidates(
            [entry], board, self._config(), skip_review=True,
        )

        assert candidates == []


# ══════════════════════════════════════════════════════════════════════════════
# #2705 Case 2: a post-merge refusal must never report a staleness the
# entry's own merge created.
# ══════════════════════════════════════════════════════════════════════════════

class TestSmokeGateSparesAnAlreadyMergedEntry:
    """quadraui#595, 2026-08-24: `coord merge --revalidate` recorded a fresh
    `passed` verdict and merged clean — moving `target_branch`'s head to the
    tip of the commits it had just landed. A LATER reader that hands this
    same, now-``MERGED`` queue row back to `evaluate_smoke_verdict` reads
    that base move as "the base moved out from under the verdict" and
    refuses a merge that already happened, naming a
    `coord test ... --passed` remedy for work with nothing left to verify.

    A `MERGED` entry's code is already in the base — no SHA comparison can
    produce a meaningful refusal for it, so the gate must short-circuit
    before ever looking at one.
    """

    @staticmethod
    def _board(completed=None):
        from coord.models import Board
        return Board(active=[], completed=list(completed or []))

    @staticmethod
    def _tested_work(aid: str = "w1") -> Assignment:
        return Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id=aid, type="work", status="done",
            branch=f"worker/{aid}", test_state="passed",
            test_head_sha="branch-sha", test_base_sha="base-old",
            test_patch_id="patch-1",
        )

    def test_merged_entry_is_never_reported_stale(self) -> None:
        board = self._board(completed=[self._tested_work()])
        entry = _q("w1", target="main", state=MERGED)
        # The quadraui#595 shape: `target_branch_head_sha` reads the LIVE
        # post-merge base — one commit past the base this entry's own fresh
        # verdict was recorded against, because the merge itself moved it.
        entry.target_branch_head_sha = "base-that-includes-this-merge"

        verdict = mq.evaluate_smoke_verdict(entry, board)

        assert verdict.ok is True
        assert verdict.kind == mq.SMOKE_OK
        assert verdict.message is None

    def test_pending_entry_with_the_same_stale_shas_still_blocks(self) -> None:
        """Guard against over-broadening: only `state == MERGED` spares the
        entry — an otherwise-identical PENDING entry blocks exactly as
        before."""
        board = self._board(completed=[self._tested_work()])
        entry = _q("w1", target="main")  # state defaults to PENDING
        entry.target_branch_head_sha = "base-that-includes-this-merge"

        verdict = mq.evaluate_smoke_verdict(entry, board)

        assert verdict.ok is False
        assert verdict.kind == mq.SMOKE_STALE

    def test_merge_gate_failures_also_spares_a_merged_entry(self) -> None:
        """`merge_gate_failures` — the shared reason-carrying reader
        `--revalidate` eligibility, the plan view, and `coord drive` all use
        — delegates straight to `evaluate_smoke_verdict`; confirm the
        short-circuit reaches it too."""
        from dataclasses import dataclass as _dc, field as _f

        @_dc
        class _Pipeline:
            default_gates: list = _f(default_factory=lambda: ["test", "merge"])

        @_dc
        class _Reviews:
            enabled: bool = False

        @_dc
        class _Cfg:
            reviews: _Reviews = _f(default_factory=_Reviews)
            pipeline: _Pipeline = _f(default_factory=_Pipeline)

        board = self._board(completed=[self._tested_work()])
        entry = _q("w1", target="main", state=MERGED)
        entry.target_branch_head_sha = "base-new"

        failures = mq.merge_gate_failures(entry, _Cfg(), board)

        assert failures == []


def _patched_time(now: float):
    """Freeze `coord.merge_queue`'s clock — the #1819 abandoned-marker check is
    the module's only wall-clock-dependent gate decision."""
    return patch("coord.merge_queue.time.time", return_value=now)


class TestAbandonedRunningMarkerIsStaleNotMissing:
    """#1819: a `test_state="running"` marker nobody is going to resolve.

    `running` is the #1395 transient dispatch marker. If the Test worker died,
    was reaped, or its verdict write was lost, nothing ever clears it and every
    gate reads "no verdict recorded" forever — and `coord merge --revalidate`,
    the ONE tool built for exactly this cascade, refuses to touch it because it
    only ever re-tests STALE entries:

        --revalidate: no entry is blocked solely on a stale test verdict —
                      nothing to revalidate

    That is the #1640 shape in its load-bearing form: the operator's escape
    hatch is unreachable from the state that most needs it (#1797, 2026-08-04).
    An abandoned marker is a staleness problem — re-run and re-record — so it
    is classified as one.
    """

    @staticmethod
    def _board(active=None, completed=None):
        from coord.models import Board
        return Board(active=list(active or []), completed=list(completed or []))

    @staticmethod
    def _work(aid: str = "w1", *, test_state: str | None = "running") -> Assignment:
        return Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id=aid, type="work", status="done",
            branch=f"worker/{aid}",
            test_state=test_state,
        )

    @staticmethod
    def _smoke(aid: str, of: str, *, dispatched_at: float, status: str = "running"):
        return Assignment(
            machine_name="m2", repo_name="api", issue_number=1,
            issue_title="[smoke] t",
            assignment_id=aid, type="smoke", status=status,
            branch="worker/w1",
            review_of_assignment_id=of,
            dispatched_at=dispatched_at,
        )

    def test_running_marker_with_a_live_worker_is_still_missing(self) -> None:
        """A Test run genuinely in flight must NOT be called stale — that would
        let `--revalidate` race a worker that is about to answer.

        Also covers the just-reaped case implicitly: the window is measured
        from the newest Test worker's dispatch, so a smoke that finished
        moments ago still buys the notify path time to land its verdict."""
        now = 10_000.0
        board = self._board(
            active=[self._smoke("s1", "w1", dispatched_at=now - 60)],
            completed=[self._work()],
        )
        with _patched_time(now):
            verdict = mq.evaluate_smoke_verdict(_q("w1", target="main"), board)

        assert verdict.kind == mq.SMOKE_MISSING

    def test_running_marker_that_outlived_its_worker_is_stale(self) -> None:
        """The reaped-worker case: the smoke row is gone from `board.active`
        and older than the window, but the parent row is still pinned at
        `running` — the verdict write was lost (or the worker died before
        making one) and nothing will ever clear it."""
        now = 10_000.0
        board = self._board(
            completed=[
                self._work(),
                self._smoke(
                    "s1", "w1",
                    dispatched_at=now - mq.RUNNING_MARKER_STALE_AFTER - 1,
                    status="failed",
                ),
            ],
        )
        with _patched_time(now):
            verdict = mq.evaluate_smoke_verdict(_q("w1", target="main"), board)

        assert verdict.ok is False
        assert verdict.kind == mq.SMOKE_STALE
        assert verdict.anchor == "run"
        assert verdict.assignment_id == "w1"
        # The wording the stale-vs-missing predicate keys off (#1769).
        assert mq.is_stale_smoke_reason(verdict.message)
        assert mq.is_stale_smoke_reason(verdict.short_reason)
        assert "no verdict recorded" not in (verdict.message or "")

    def test_running_marker_older_than_the_window_is_stale_even_if_active(self) -> None:
        """A smoke row still sitting in `board.active` long after any real run
        could have finished is a corpse, not a worker."""
        now = 10_000.0
        board = self._board(
            active=[
                self._smoke(
                    "s1", "w1",
                    dispatched_at=now - mq.RUNNING_MARKER_STALE_AFTER - 1,
                ),
            ],
            completed=[self._work()],
        )
        with _patched_time(now):
            verdict = mq.evaluate_smoke_verdict(_q("w1", target="main"), board)

        assert verdict.kind == mq.SMOKE_STALE
        assert verdict.anchor == "run"

    def test_running_marker_with_no_smoke_row_at_all_stays_missing(self) -> None:
        """The #1395 local-driver shape (`scripts/drive-issue.sh` sets the
        marker and runs the suite in-process) has no worker row whose age could
        tell a live run from a dead one — keep the old classification rather
        than risk resetting a driver mid-run."""
        board = self._board(completed=[self._work()])
        with _patched_time(10_000.0):
            verdict = mq.evaluate_smoke_verdict(_q("w1", target="main"), board)

        assert verdict.kind == mq.SMOKE_MISSING

    def test_no_marker_at_all_is_unaffected(self) -> None:
        board = self._board(
            completed=[
                self._work(test_state=None),
                self._smoke("s1", "w1", dispatched_at=0.0, status="failed"),
            ],
        )
        with _patched_time(10_000.0):
            verdict = mq.evaluate_smoke_verdict(_q("w1", target="main"), board)

        assert verdict.kind == mq.SMOKE_MISSING

    def test_revalidate_can_now_reach_the_wedged_entry(self) -> None:
        """The point of the classification: `coord merge --revalidate` — which
        only ever considers SMOKE_STALE entries — can finally clear it."""
        from dataclasses import dataclass as _dc, field as _f

        @_dc
        class _Pipeline:
            default_gates: list = _f(default_factory=lambda: ["test", "merge"])

            def gates_for(self, a=None):
                return list(self.default_gates)

        @_dc
        class _Reviews:
            enabled: bool = False

        @_dc
        class _Cfg:
            reviews: _Reviews = _f(default_factory=_Reviews)
            pipeline: _Pipeline = _f(default_factory=_Pipeline)

        now = 10_000.0
        board = self._board(
            completed=[
                self._work(),
                self._smoke(
                    "s1", "w1",
                    dispatched_at=now - mq.RUNNING_MARKER_STALE_AFTER - 1,
                    status="failed",
                ),
            ],
        )
        entry = _q("w1", target="main")

        with _patched_time(now):
            candidates = mq.revalidation_candidates([entry], board, _Cfg())

        assert [c.work_assignment_id for c in candidates] == ["w1"]


# ══════════════════════════════════════════════════════════════════════════════
# #2231: a conflict hiding behind a stale-verdict smoke block
# ══════════════════════════════════════════════════════════════════════════════

class TestStaleSmokeConflictProbe:
    """#2231: gate evaluation short-circuits on the smoke gate, so an entry
    whose branch does not merge AT ALL reports "test verdict stale" — the one
    remedy that cannot work — and never reaches the merge attempt that would
    have armed #241's conflict-fix.

    quadraui #306/#309 sat 11h each in exactly that position: `mergeable:
    false, mergeable_state: dirty`, four sibling branches appending to one
    file, two of them stranded once the first two merged. `--revalidate`
    diagnosed it perfectly and nothing acted; a human finally typed `coord fix
    --force`, which is #241's own job description.
    """

    @staticmethod
    def _config():
        return TestSmokeGate._config()

    @staticmethod
    def _stale_setup(*, mergeable: bool | None):
        """One PENDING entry whose passed verdict was staled by a base move,
        plus a `gh` stub with a definite answer about mergeability."""
        work = TestSmokeGate._work("w1", test_state="passed")
        work.test_head_sha = "branch-sha-1"
        work.test_patch_id = "patch-1"
        work.test_base_sha = "main-sha-old"
        board = TestSmokeGate._board(completed=[work])

        entry = _q("w1", size=10, pr=100)
        entry.branch_head_sha = "branch-sha-1"
        entry.branch_patch_id = "patch-1"
        entry.target_branch_head_sha = "main-sha-new"   # base moved under it
        gh = FakeGh(mergeable_results={100: mergeable})
        return entry, board, gh

    # ── the predicate ──

    def test_reason_names_the_conflict_when_github_says_not_mergeable(self) -> None:
        entry, board, gh = self._stale_setup(mergeable=False)
        smoke = mq.evaluate_smoke_verdict(entry, board)
        assert smoke.kind == mq.SMOKE_STALE

        reason = mq.stale_smoke_conflict_reason(entry, smoke, gh)

        assert reason is not None
        # The conflict leads; the stale verdict is named as downstream, not as
        # the blocker (the acceptance criterion's "not (only) a stale verdict").
        assert "merge conflict" in reason.lower()
        assert "test verdict" in reason.lower()

    def test_reason_classifies_as_rebaseable_and_not_as_stale_smoke(self) -> None:
        """The wording is load-bearing in both directions: `classify_conflict`
        decides whether #241 dispatches, and `is_stale_smoke_reason` decides
        whether `coord drive` answers with a re-test. Getting either wrong
        reproduces the bug with extra steps."""
        entry, board, gh = self._stale_setup(mergeable=False)
        reason = mq.stale_smoke_conflict_reason(
            entry, mq.evaluate_smoke_verdict(entry, board), gh,
        )

        assert mq.classify_conflict(reason) == "rebaseable"
        assert mq.is_stale_smoke_reason(reason) is False

    def test_silent_when_the_pr_is_mergeable(self) -> None:
        """Acceptance: a genuinely stale verdict on a cleanly-composing branch
        still takes the #1738 path, unchanged."""
        entry, board, gh = self._stale_setup(mergeable=True)
        assert mq.stale_smoke_conflict_reason(
            entry, mq.evaluate_smoke_verdict(entry, board), gh,
        ) is None

    def test_silent_when_mergeability_is_still_computing(self) -> None:
        """`None` is "GitHub hasn't decided", not "conflict". Upgrading an
        inconclusive read would point a rebase worker at a clean branch."""
        entry, board, gh = self._stale_setup(mergeable=None)
        assert mq.stale_smoke_conflict_reason(
            entry, mq.evaluate_smoke_verdict(entry, board), gh,
        ) is None

    def test_silent_when_the_probe_raises(self) -> None:
        entry, board, _gh = self._stale_setup(mergeable=False)

        class _Boom:
            def check_pr_mergeable(self, repo, number):
                raise RuntimeError("gh exploded")

        assert mq.stale_smoke_conflict_reason(
            entry, mq.evaluate_smoke_verdict(entry, board), _Boom(),
        ) is None

    def test_silent_without_a_probe_at_all(self) -> None:
        """The `/board` read path evaluates gates against a GateSnapshot, which
        implements no `gh` calls (#1336 Invariant 1). Duck-typed, like the
        #1877 CI-absent branch."""
        entry, board, _gh = self._stale_setup(mergeable=False)

        class _NoProbe:
            pass

        assert mq.stale_smoke_conflict_reason(
            entry, mq.evaluate_smoke_verdict(entry, board), _NoProbe(),
        ) is None

    def test_silent_for_a_missing_verdict(self) -> None:
        """SMOKE_MISSING is the #1640 lost-write shape and stays out of scope,
        exactly as it is for `revalidation_candidates`."""
        work = TestSmokeGate._work("w1", test_state=None)
        board = TestSmokeGate._board(completed=[work])
        entry = _q("w1", pr=100)
        smoke = mq.evaluate_smoke_verdict(entry, board)

        assert smoke.kind == mq.SMOKE_MISSING
        assert mq.stale_smoke_conflict_reason(
            entry, smoke, FakeGh(mergeable_results={100: False}),
        ) is None

    def test_silent_before_a_pr_exists(self) -> None:
        entry, board, gh = self._stale_setup(mergeable=False)
        entry.pr_number = None
        assert mq.stale_smoke_conflict_reason(
            entry, mq.evaluate_smoke_verdict(entry, board), gh,
        ) is None

    # ── process(): the event that arms #241 ──

    def test_process_emits_a_conflict_event_not_smoke_required(self) -> None:
        """The headline: the entry that used to park at PENDING/"stale" now
        parks at CONFLICT with a `conflict` event — which is precisely what
        `coord.commands.merge._dispatch_conflict_fixes` consumes."""
        entry, board, gh = self._stale_setup(mergeable=False)
        events = process([entry], gh, config=self._config(), board=board)

        kinds = [e.kind for e in events]
        assert "conflict" in kinds
        assert "smoke_required" not in kinds
        assert "merged" not in kinds
        assert gh.merge_calls == []
        assert entry.state == CONFLICT
        assert mq.classify_conflict(entry.error) == "rebaseable"

    def test_process_still_blocks_on_smoke_when_the_branch_is_clean(self) -> None:
        entry, board, gh = self._stale_setup(mergeable=True)
        events = process([entry], gh, config=self._config(), board=board)

        kinds = [e.kind for e in events]
        assert "smoke_required" in kinds
        assert "conflict" not in kinds
        assert entry.state == PENDING
        assert mq.is_stale_smoke_reason(entry.error) is True

    def test_process_unchanged_when_gh_reports_nothing(self) -> None:
        """Every pre-#2231 test uses a `gh` stub whose `check_pr_mergeable`
        answers `None`; none of them may change behaviour."""
        entry, board, _gh = self._stale_setup(mergeable=None)
        events = process([entry], FakeGh(), config=self._config(), board=board)

        assert [e.kind for e in events].count("smoke_required") == 1
        assert entry.state == PENDING

    # ── process(dry_run=True): the preview must not disagree with --plan ──

    def test_dry_run_reports_conflict_not_smoke_required(self) -> None:
        """Review finding on iteration 1: the smoke gate's own `--dry-run`
        preview block sat right next to the CI gate's #1877 dry-run conflict
        check but wasn't updated for #2231, so `coord merge --dry-run`
        (without `--revalidate`) kept printing the stale-verdict headline for
        an entry `--plan`/a real run both correctly report as `conflict` —
        the exact "two surfaces answer the same question differently"
        split-brain this issue exists to fix."""
        entry, board, gh = self._stale_setup(mergeable=False)
        events = process([entry], gh, config=self._config(), board=board, dry_run=True)

        kinds = [e.kind for e in events]
        assert "conflict" in kinds
        assert "smoke_required" not in kinds
        assert gh.merge_calls == []
        # dry-run never mutates state
        assert entry.state == PENDING
        conflict_event = next(e for e in events if e.kind == "conflict")
        assert mq.classify_conflict(conflict_event.message) == "rebaseable"

    def test_dry_run_still_shows_smoke_required_when_the_branch_is_clean(self) -> None:
        """Acceptance: a genuinely stale verdict on a cleanly-composing
        branch still takes the #1738 path, unchanged — including in the
        dry-run preview."""
        entry, board, gh = self._stale_setup(mergeable=True)
        events = process([entry], gh, config=self._config(), board=board, dry_run=True)

        kinds = [e.kind for e in events]
        assert "smoke_required" in kinds
        assert "conflict" not in kinds
        assert entry.state == PENDING

    # ── _entry_gate_status(): what the plan/board reports ──

    def test_plan_reason_names_the_conflict(self) -> None:
        """Acceptance: "An entry whose branch does not compose onto its base
        reports conflict as its blocking reason, not (only) a stale verdict."
        """
        entry, board, gh = self._stale_setup(mergeable=False)
        status, reason = mq.entry_gate_status(
            entry, board, self._config(), None, gh,
        )

        assert status == mq.PLAN_BLOCKED
        assert "merge conflict" in reason.lower()
        assert mq.is_stale_smoke_reason(reason) is False

    def test_plan_reason_unchanged_for_a_clean_branch(self) -> None:
        entry, board, gh = self._stale_setup(mergeable=True)
        status, reason = mq.entry_gate_status(
            entry, board, self._config(), None, gh,
        )

        assert status == mq.PLAN_BLOCKED
        assert mq.is_stale_smoke_reason(reason) is True


# ── #2246: post-merge sibling conflict sweep ─────────────────────────────────

class _SweepGh:
    """A `gh_ops` whose `check_pr_mergeable` answers per PR, per ROUND.

    `verdicts` maps PR number -> list of verdicts, consumed one per probe; the
    last element repeats once exhausted. That's the only shape `FakeGh` can't
    express and it's the one #2246 turns on: GitHub returns UNKNOWN (`None`)
    for a few seconds after a merge and only then settles to CONFLICTING, so a
    sweep that believes the first read is a sweep that reports nothing.
    """

    def __init__(self, verdicts: dict[int, list[bool | None]] | None = None,
                 raises: bool = False) -> None:
        self.verdicts = verdicts or {}
        self.raises = raises
        self.calls: list[tuple[str, int]] = []

    def check_pr_mergeable(self, repo: str, number: int) -> bool | None:
        self.calls.append((repo, number))
        if self.raises:
            raise RuntimeError("gh exploded")
        seq = self.verdicts.get(number)
        if not seq:
            return None
        return seq.pop(0) if len(seq) > 1 else seq[0]


def _sweep_entry(aid: str, *, pr: int, state: str = PENDING, issue: int = 1,
                 repo_github: str = "acme/api", target: str = "main",
                 error: str | None = None) -> QueuedMerge:
    e = _q(aid, repo_github=repo_github, target=target, state=state, pr=pr)
    e.issue_number = issue
    e.error = error
    return e


def _merged_event(entry: QueuedMerge) -> mq.MergeEvent:
    return mq.MergeEvent(entry, "merged", "merged via rebase")


class TestSweepSiblingConflicts:
    """#2246: nothing re-checked sibling PRs after a merge, so a branch that
    GitHub *already knew* was CONFLICTING presented as "smoke gate — test
    verdict stale" (quadraui #306/#309) or "checks_failed … (unknown)"
    (claude-coordinator #2234) and burned two drive attempts each."""

    def test_sibling_broken_by_the_merge_is_parked_as_a_conflict(self) -> None:
        """The black-box acceptance: two branches on one base, merge the
        first, the second is reported conflicted immediately — no drive
        attempt spent discovering it as some other gate's failure."""
        merged = _sweep_entry("m", pr=100, state=MERGED, issue=307)
        sibling = _sweep_entry("s", pr=101, issue=309)
        gh = _SweepGh({101: [False]})

        out = mq.sweep_sibling_conflicts(
            [_merged_event(merged)], [merged, sibling], gh, persist=False,
        )

        assert [ev.kind for ev in out] == ["conflict"]
        assert out[0].entry is sibling
        assert sibling.state == CONFLICT
        # The whole point of the error text: #241's classifier must route it.
        assert mq.classify_conflict(sibling.error) == "rebaseable"
        assert "CONFLICTING" in sibling.error
        # ...and it must name the merge that broke it, the fact a human had
        # to reconstruct by hand in both 2026-08-14 collisions.
        assert "#307" in sibling.error
        assert gh.calls == [("acme/api", 101)]

    def test_a_clean_sibling_is_left_alone(self) -> None:
        merged = _sweep_entry("m", pr=100, state=MERGED)
        sibling = _sweep_entry("s", pr=101, issue=2)
        gh = _SweepGh({101: [True]})

        out = mq.sweep_sibling_conflicts(
            [_merged_event(merged)], [merged, sibling], gh, persist=False,
        )

        assert out == []
        assert sibling.state == PENDING
        assert sibling.error is None

    def test_already_conflicting_entry_is_untouched(self) -> None:
        """#2246: "A PR already conflicting before the merge is untouched."
        CONFLICT is the queue's durable record of exactly that, so the entry
        is never even probed — otherwise every merge in the repo would
        re-dispatch a conflict-fix for it forever."""
        merged = _sweep_entry("m", pr=100, state=MERGED)
        parked = _sweep_entry("s", pr=101, issue=2, state=CONFLICT,
                              error="merge conflict from an earlier attempt")
        gh = _SweepGh({101: [False]})

        out = mq.sweep_sibling_conflicts(
            [_merged_event(merged)], [merged, parked], gh, persist=False,
        )

        assert out == []
        assert gh.calls == []
        assert parked.error == "merge conflict from an earlier attempt"

    def test_pending_entry_already_carrying_a_conflict_error_is_not_redispatched(
        self,
    ) -> None:
        """Belt-and-braces for the same transition rule: an entry can be
        PENDING while still carrying a conflict error (a re-enqueue, a #1477
        unpark that raced). Re-marking it on every sibling merge would be the
        re-dispatch loop #2246 explicitly forbids."""
        merged = _sweep_entry("m", pr=100, state=MERGED)
        sibling = _sweep_entry("s", pr=101, issue=2,
                              error="merge conflict: could not be rebased")
        gh = _SweepGh({101: [False]})

        out = mq.sweep_sibling_conflicts(
            [_merged_event(merged)], [merged, sibling], gh, persist=False,
        )

        assert out == []
        assert gh.calls == []

    def test_unknown_is_retried_until_github_settles(self) -> None:
        """#2246: "`mergeable` is not instant." GitHub computes it
        asynchronously and returns UNKNOWN until it settles — treating the
        first UNKNOWN as clean is the whole bug."""
        merged = _sweep_entry("m", pr=100, state=MERGED)
        sibling = _sweep_entry("s", pr=101, issue=2)
        gh = _SweepGh({101: [None, None, False]})
        slept: list[float] = []

        out = mq.sweep_sibling_conflicts(
            [_merged_event(merged)], [merged, sibling], gh,
            sleep=slept.append, persist=False,
        )

        assert [ev.kind for ev in out] == ["conflict"]
        assert sibling.state == CONFLICT
        assert len(gh.calls) == 3
        # One sleep per retry round, never before the first probe.
        assert len(slept) == 2

    def test_persistent_unknown_leaves_the_entry_pending(self) -> None:
        """An inconclusive read is not evidence of a conflict — the retry
        budget is bounded and running it out marks nothing (#1477's
        fail-closed posture, pointed the other way)."""
        merged = _sweep_entry("m", pr=100, state=MERGED)
        sibling = _sweep_entry("s", pr=101, issue=2)
        gh = _SweepGh({101: [None]})

        out = mq.sweep_sibling_conflicts(
            [_merged_event(merged)], [merged, sibling], gh,
            sleep=lambda _s: None, persist=False,
        )

        assert out == []
        assert sibling.state == PENDING
        assert len(gh.calls) == mq.SIBLING_SWEEP_ATTEMPTS

    def test_retry_budget_is_per_round_not_per_entry(self) -> None:
        """Rounds, not per-entry loops: N unresolved siblings cost N probes
        per round and ONE sleep, so total added wall-clock is bounded by the
        attempt budget however long the queue is."""
        merged = _sweep_entry("m", pr=100, state=MERGED)
        sibs = [_sweep_entry(f"s{i}", pr=200 + i, issue=10 + i) for i in range(4)]
        gh = _SweepGh({200 + i: [None] for i in range(4)})
        slept: list[float] = []

        mq.sweep_sibling_conflicts(
            [_merged_event(merged)], [merged, *sibs], gh,
            sleep=slept.append, persist=False,
        )

        assert len(slept) == mq.SIBLING_SWEEP_ATTEMPTS - 1
        assert len(gh.calls) == 4 * mq.SIBLING_SWEEP_ATTEMPTS

    def test_scoped_to_the_repo_and_base_that_actually_moved(self) -> None:
        """#2246: "Scope to the merged repo, and only to PRs whose base is
        the branch that just moved." """
        merged = _sweep_entry("m", pr=100, state=MERGED)
        other_repo = _sweep_entry("o", pr=101, issue=2, repo_github="acme/web")
        other_base = _sweep_entry("b", pr=102, issue=3, target="develop")
        same_base = _sweep_entry("s", pr=103, issue=4)
        gh = _SweepGh({101: [False], 102: [False], 103: [False]})

        out = mq.sweep_sibling_conflicts(
            [_merged_event(merged)],
            [merged, other_repo, other_base, same_base], gh, persist=False,
        )

        assert [ev.entry.assignment_id for ev in out] == ["s"]
        assert gh.calls == [("acme/api", 103)]
        assert other_repo.state == PENDING
        assert other_base.state == PENDING

    def test_nothing_merged_means_no_api_calls_at_all(self) -> None:
        """Cost is one call per open PR per MERGE, not per tick — the
        overwhelmingly common tick merges nothing and must cost nothing."""
        entry = _sweep_entry("a", pr=100)
        sibling = _sweep_entry("s", pr=101, issue=2)
        gh = _SweepGh({101: [False]})

        out = mq.sweep_sibling_conflicts(
            [mq.MergeEvent(entry, "smoke_required", "stale verdict")],
            [entry, sibling], gh, persist=False,
        )

        assert out == []
        assert gh.calls == []

    def test_entry_without_a_pr_is_skipped(self) -> None:
        merged = _sweep_entry("m", pr=100, state=MERGED)
        no_pr = _q("s", state=PENDING)
        gh = _SweepGh()

        out = mq.sweep_sibling_conflicts(
            [_merged_event(merged)], [merged, no_pr], gh, persist=False,
        )

        assert out == []
        assert gh.calls == []

    def test_a_read_error_fails_open(self) -> None:
        """#2246: "Fail open: a read error must not block the merge that
        triggered the sweep." """
        merged = _sweep_entry("m", pr=100, state=MERGED)
        sibling = _sweep_entry("s", pr=101, issue=2)
        gh = _SweepGh(raises=True)

        out = mq.sweep_sibling_conflicts(
            [_merged_event(merged)], [merged, sibling], gh,
            sleep=lambda _s: None, persist=False,
        )

        assert out == []
        assert sibling.state == PENDING

    def test_gh_ops_without_the_probe_is_inert(self) -> None:
        """`check_pr_mergeable` is duck-typed on GhOps — a stub predating it
        must not crash the merge that just succeeded."""
        class _Bare:
            pass

        merged = _sweep_entry("m", pr=100, state=MERGED)
        sibling = _sweep_entry("s", pr=101, issue=2)

        out = mq.sweep_sibling_conflicts(
            [_merged_event(merged)], [merged, sibling], _Bare(), persist=False,
        )

        assert out == []
        assert sibling.state == PENDING

    def test_sibling_the_caller_does_not_hold_is_persisted(self, coord_db) -> None:
        """The `--only` shape: `coord merge --only` writes back exactly one
        entry, so a sibling this sweep parks would be lost unless the sweep
        persists it itself."""
        merged = _sweep_entry("m", pr=100, state=PENDING, issue=307)
        sibling = _sweep_entry("s", pr=101, issue=309)
        save_queue([merged, sibling])
        merged.state = MERGED
        gh = _SweepGh({101: [False]})

        out = mq.sweep_sibling_conflicts(
            [_merged_event(merged)], [merged], gh,
        )

        assert [ev.entry.assignment_id for ev in out] == ["s"]
        reread = {x.assignment_id: x for x in load_queue()}
        assert reread["s"].state == CONFLICT
        assert mq.classify_conflict(reread["s"].error) == "rebaseable"

    def test_concurrent_write_during_the_retry_sleep_is_not_reverted(
        self, coord_db,
    ) -> None:
        """Reviewer repro (#2246 review): `rows` is snapshotted from disk
        near the top of the function, *before* the retry-sleep loop (up to
        ``(attempts - 1) * interval``, longer under real GitHub latency). If
        the final persist step built its override dict from that stale
        `rows` snapshot instead of from what THIS call actually mutated, an
        unrelated concurrent writer's change made during the sleep — a
        second `coord merge`, the daemon's own next tick, `coord
        reconcile-merges` — would be silently reverted back to its
        pre-sweep value. `unrelated` never enters `sibling_sweep_candidates`
        (different repo, no involvement in the merge that triggered this)
        and must read back exactly what the concurrent writer set."""
        merged = _sweep_entry("m", pr=100, state=PENDING, issue=307)
        sibling = _sweep_entry("s", pr=101, issue=309)
        unrelated = _sweep_entry(
            "u", pr=900, issue=5, repo_github="acme/other", target="develop",
        )
        save_queue([merged, sibling, unrelated])
        merged.state = MERGED
        # UNKNOWN on the first probe forces exactly one retry-sleep round.
        gh = _SweepGh({101: [None, False]})

        def fake_sleep(_seconds: float) -> None:
            # Simulate a concurrent writer (a second `coord merge`, the
            # daemon's own next tick) mutating an UNRELATED row while this
            # sweep is asleep between retry rounds.
            concurrent = {x.assignment_id: x for x in load_queue()}
            concurrent["u"].state = mq.HUMAN_REQUIRED
            concurrent["u"].error = "concurrent writer set this"
            save_queue(list(concurrent.values()))

        out = mq.sweep_sibling_conflicts(
            [_merged_event(merged)], [merged], gh, sleep=fake_sleep,
        )

        assert [ev.entry.assignment_id for ev in out] == ["s"]
        reread = {x.assignment_id: x for x in load_queue()}
        assert reread["s"].state == CONFLICT
        # The concurrent writer's change to the unrelated row must survive
        # the sweep's own final persist, not get clobbered back to PENDING.
        assert reread["u"].state == mq.HUMAN_REQUIRED
        assert reread["u"].error == "concurrent writer set this"

    def test_callers_own_object_is_mutated_not_a_second_copy(
        self, coord_db,
    ) -> None:
        """The caller saves its own `items` over the on-disk queue right
        after this runs. Mutating a freshly-loaded second copy here would be
        silently reverted by that save — the entry would read CONFLICT for
        exactly as long as it took to write it."""
        merged = _sweep_entry("m", pr=100, state=PENDING)
        sibling = _sweep_entry("s", pr=101, issue=2)
        save_queue([merged, sibling])
        merged.state = MERGED
        gh = _SweepGh({101: [False]})

        mq.sweep_sibling_conflicts(
            [_merged_event(merged)], [merged, sibling], gh,
        )

        assert sibling.state == CONFLICT
        # Simulate the caller's save-over-disk step and confirm it sticks.
        fresh = load_queue()
        by_id = {merged.assignment_id: merged, sibling.assignment_id: sibling}
        save_queue([by_id.get(x.assignment_id, x) for x in fresh])
        assert {x.assignment_id: x.state for x in load_queue()}["s"] == CONFLICT

    def test_error_text_never_reads_as_a_permission_problem(self) -> None:
        """`classify_conflict` checks `_HUMAN_SIGNALS` first — an error that
        happened to say "review required" or "permission" would escalate
        straight to a human instead of dispatching #241's worker."""
        merged = _sweep_entry("m", pr=100, state=MERGED, issue=307)
        sibling = _sweep_entry("s", pr=101, issue=309)

        text = mq.sibling_conflict_error(sibling, [_merged_event(merged)])

        assert mq.classify_conflict(text) == "rebaseable"
        assert mq.is_rebase_refusal(text) is False

    def test_merged_bases_only_counts_actual_merges(self) -> None:
        merged = _sweep_entry("m", pr=100, state=MERGED)
        conflicted = _sweep_entry("c", pr=102, issue=3, target="develop")

        bases = mq.merged_bases([
            _merged_event(merged),
            mq.MergeEvent(conflicted, "conflict", "nope"),
        ])

        assert bases == {("acme/api", "main")}


# ── #1896 Phase 0: forge-availability recording at the merge-gate seam ──────

class TestForgeAvailabilityRefusalRecording:
    """`process()` persists one `forge_availability`-category audit row per
    LIVE `checks_failed`/`checks_pending`/`checks_stale` refusal (#1896),
    and none at all for a `--dry-run` preview of the same refusal — a
    preview never actually blocked anything, so it is not a real
    observation of forge/CI availability."""

    @staticmethod
    def _forge_rows(coord_db) -> list:
        return coord_db.execute(
            "SELECT * FROM audit_log WHERE category='forge_availability' "
            "AND event_type='merge_gate_refusal' ORDER BY id"
        ).fetchall()

    def test_live_checks_failed_is_recorded(self, coord_db) -> None:
        from types import SimpleNamespace

        class _Ci:
            is_available = True
            def list_checks_for_pr(self, repo, number):
                return [SimpleNamespace(
                    name="build", status="completed", conclusion="failure",
                    run_id=None,
                )]

        items = [_q("w1", pr=99)]
        gh = FakeGh(mergeable_results={99: False})
        process(items, gh, ci_store=_Ci())

        rows = self._forge_rows(coord_db)
        assert len(rows) == 1
        assert rows[0]["repo"] == "api"
        assert rows[0]["issue"] == 1
        details = json.loads(rows[0]["details_json"])
        assert details["reason"] == "checks_failed"

    def test_live_checks_pending_is_recorded(self, coord_db) -> None:
        items = [_q("w1", pr=99)]
        gh = FakeGh()
        pending = _check("e2e", status="in_progress", conclusion=None)

        class _Ci:
            is_available = True
            def list_checks_for_pr(self, repo, number):
                return [pending]

        process(items, gh, ci_store=_Ci())

        rows = self._forge_rows(coord_db)
        assert len(rows) == 1
        assert json.loads(rows[0]["details_json"])["reason"] == "checks_pending"

    def test_dry_run_preview_of_checks_failed_is_not_recorded(self, coord_db) -> None:
        items = [_q("w1", pr=99)]
        gh = FakeGh(mergeable_results={99: False})
        real_failure = _check("build", conclusion="failure")

        class _Ci:
            is_available = True
            def list_checks_for_pr(self, repo, number):
                return [real_failure]

        events = process(items, gh, ci_store=_Ci(), dry_run=True)

        assert "checks_failed" in [e.kind for e in events]
        assert self._forge_rows(coord_db) == []

    def test_a_non_refusal_event_records_nothing(self, coord_db) -> None:
        """`opened`/`sized`/`merged`/etc. are not in MERGE_GATE_REFUSAL_KINDS
        — only the three named CI-gate refusal kinds get a forge-
        availability row."""
        items = [_q("w1")]
        gh = FakeGh()
        process(items, gh, ci_store=mq.NoOpCi())

        assert self._forge_rows(coord_db) == []

    def test_recording_failure_never_breaks_a_real_merge(self, coord_db, monkeypatch) -> None:
        """Acceptance bar: a forge-availability recording failure must never
        raise into `process()`, let alone block or undo a real refusal."""
        from types import SimpleNamespace

        def _boom(*a, **k):
            raise RuntimeError("audit_log write exploded")

        monkeypatch.setattr("coord.forge_availability.record_audit", _boom)

        class _Ci:
            is_available = True
            def list_checks_for_pr(self, repo, number):
                return [SimpleNamespace(
                    name="build", status="completed", conclusion="failure",
                    run_id=None,
                )]

        items = [_q("w1", pr=99)]
        gh = FakeGh(mergeable_results={99: False})
        events = process(items, gh, ci_store=_Ci())  # must not raise

        assert "checks_failed" in [e.kind for e in events]


# ── #2983: a missing merge_queue_archive must not poison the connection ─────
#
# `_archived_merged_issue_keys` catches "no such table" (the archive only
# exists once `housekeeping.sweep()` has archived a row) and returns
# `set()` -- but `conn` is the process-lived `get_connection()` singleton,
# and on Postgres the swallowed `UndefinedTable` aborted its transaction for
# every statement that came afterwards.


def _abort_conn_with_merged_row(monkeypatch: pytest.MonkeyPatch) -> AbortOnErrorConn:
    """The `get_connection()` singleton, replaced by an abort-simulating stub
    over a real schema-migrated SQLite connection holding one MERGED row.

    `merge_queue_archive` is created by `coord.housekeeping`, not
    `_ensure_schema`, so it is genuinely absent -- no drop needed.
    """
    real = schema_migrated_sqlite_connection()
    sql.execute(
        real,
        "INSERT INTO merge_queue (assignment_id, repo_name, repo_github, branch, "
        "target_branch, issue_number, issue_title, state) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("a-1", "demo", "o/demo", "issue-42-x", "main", 42, "Demo issue", MERGED),
    )
    real.commit()
    conn = abort_simulating_connection(monkeypatch, real)
    monkeypatch.setattr(db_mod, "_conn", conn)
    return conn


class TestArchivedMergedIssueKeysRollsBack:
    def test_returns_live_keys_and_leaves_the_connection_usable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = _abort_conn_with_merged_row(monkeypatch)

        assert mq.merged_issue_keys() == {("demo", 42)}
        assert conn.rollbacks == 1

        # The acceptance criterion: a further operation on the same
        # connection.  Pre-fix this raises "current transaction is aborted",
        # and since it is the shared singleton, so did every later caller —
        # `enqueue_approved_work()` / `staging_items()` are the real ones.
        assert sql.execute(conn, "SELECT 1 AS ok").fetchone()["ok"] == 1

    def test_a_second_lookup_through_the_same_singleton_still_works(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _abort_conn_with_merged_row(monkeypatch)

        mq.merged_issue_keys()

        assert mq.merged_issue_keys() == {("demo", 42)}


class TestArchivedMergedIssueKeysOnRealPostgres:
    """The same regression against an actual Postgres server, when one is
    reachable -- `psycopg.errors.UndefinedTable` then
    `InFailedSqlTransaction`, the real shapes the stub above simulates."""

    def test_missing_archive_table_does_not_poison_the_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        unavailable = backends.postgres_available()
        if unavailable:
            pytest.skip(f"no Postgres backend available: {unavailable}")

        session = backends.open_named_session(backends.BACKEND_POSTGRES)
        try:
            db_mod._ensure_schema(session.conn)
            sql.execute(
                session.conn,
                "INSERT INTO merge_queue (assignment_id, repo_name, repo_github, "
                "branch, target_branch, issue_number, issue_title, state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("a-1", "demo", "o/demo", "issue-42-x", "main", 42, "Demo issue", MERGED),
            )
            session.conn.commit()
            monkeypatch.setattr(db_mod, "_conn", session.conn)

            assert mq.merged_issue_keys() == {("demo", 42)}
            assert sql.execute(session.conn, "SELECT 1 AS ok").fetchone()["ok"] == 1
        finally:
            monkeypatch.undo()
            session.close()
