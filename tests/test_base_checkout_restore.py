"""#1694 — a base checkout parked on a work branch must be cleared, not just
refused.

#1693 stopped ``_git_worktree_add`` from *deleting* the base checkout when the
collision path named it.  That is correct and had to ship first, but a refusal
is not a resolution: the base is still parked on the branch, so every later
dispatch against that branch on that machine fails identically until a human
runs ``git checkout``.  #1659's own docstring calls the parked state "routine",
and ``coord fix``'s same-branch re-dispatch makes it "near-certain on a retry".

Two halves are tested here:

* **Part A** — nothing should leave the base parked in the first place.  The
  deterministic offender was ``coord.conflict_fix``'s briefing, which told the
  worker to ``cd <base checkout>`` and ``git checkout <work branch>`` there
  even though the agent had already handed it a worktree on exactly that
  branch.  Belt and braces, the agent now puts the base back at teardown when
  it is parked on the finished assignment's own branch.
* **Part B** — when the collision happens anyway, ``_git_worktree_add`` clears
  it non-destructively and retries once.

Every "refuses" test asserts the base is left **byte-identical**: same branch,
same working tree, same commits.  Nothing here may ever delete or discard.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import coord.agent as agent_mod
from coord.agent import (
    AgentAssignment,
    AgentServer,
    AssignmentSpec,
    _base_checkout_move_blockers,
    _current_branch,
    _GitError,
    _git_worktree_add,
    _restore_base_checkout_branch,
)


def _run(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo_with_remote(tmp_path: Path) -> Path:
    """A clone with a real `origin`, on `main`, one pushed commit."""
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(remote)],
        check=True, capture_output=True,
    )
    clone = tmp_path / "base"
    clone.mkdir()
    _run(clone, "init", "-b", "main")
    _run(clone, "config", "user.email", "t@t.com")
    _run(clone, "config", "user.name", "T")
    (clone / "README").write_text("v1\n")
    _run(clone, "add", "README")
    _run(clone, "commit", "-m", "initial")
    _run(clone, "remote", "add", "origin", str(remote))
    _run(clone, "push", "-u", "origin", "main")
    return clone


def _park(base: Path, branch: str, *, push: bool = True) -> None:
    """Put *base* on *branch* — the exact state #1694 has to survive."""
    _run(base, "checkout", "-b", branch)
    if push:
        _run(base, "push", "-u", "origin", branch)


# ── Part B: the collision is cleared, not just refused ──────────────────────


def test_parked_and_clean_base_is_freed_and_the_add_succeeds(
    repo_with_remote: Path, tmp_path: Path
) -> None:
    """The headline case: remedy runs, add succeeds, nothing is deleted."""
    base = repo_with_remote
    _park(base, "issue-1694-parked")
    sentinel_sha = _run(base, "rev-parse", "HEAD")
    log = tmp_path / "worker.log"
    log.write_text("")

    new_wt = tmp_path / "new-wt"
    _git_worktree_add(
        base,
        ["-B", "issue-1694-parked", str(new_wt), "origin/issue-1694-parked"],
        log_path=str(log),
        default_branch="main",
    )

    assert new_wt.exists(), "the worktree add did not succeed after the remedy"
    assert _current_branch(base) == "main", "base was not moved to the default branch"
    # Nothing deleted, nothing lost.
    assert (base / ".git").exists()
    assert (base / "README").read_text() == "v1\n"
    assert _run(base, "rev-parse", "issue-1694-parked") == sentinel_sha
    text = log.read_text()
    assert "base checkout was parked on 'issue-1694-parked'" in text, text
    assert "moved it to main" in text, text


def test_dirty_base_is_refused_and_left_exactly_as_it_was(
    repo_with_remote: Path, tmp_path: Path
) -> None:
    """Uncommitted work in the base outranks the dispatch, always."""
    base = repo_with_remote
    _park(base, "issue-1694-dirty")
    (base / "README").write_text("edited by a human\n")
    log = tmp_path / "worker.log"
    log.write_text("")

    with pytest.raises(_GitError) as excinfo:
        _git_worktree_add(
            base,
            ["-B", "issue-1694-dirty", str(tmp_path / "wt"), "HEAD"],
            log_path=str(log),
            default_branch="main",
        )

    message = str(excinfo.value)
    assert "issue-1694-dirty" in message, message
    assert str(base) in message, message
    assert _current_branch(base) == "issue-1694-dirty", "base was moved anyway"
    assert (base / "README").read_text() == "edited by a human\n"
    assert "REFUSING" in log.read_text()
    assert "uncommitted changes" in log.read_text()


def test_unpushed_commits_in_the_base_are_refused(
    repo_with_remote: Path, tmp_path: Path
) -> None:
    """A committed-but-unpushed base is still the only copy of that work."""
    base = repo_with_remote
    _park(base, "issue-1694-unpushed", push=False)
    (base / "new.txt").write_text("local only\n")
    _run(base, "add", "new.txt")
    _run(base, "commit", "-m", "unpushed")
    head = _run(base, "rev-parse", "HEAD")
    log = tmp_path / "worker.log"
    log.write_text("")

    with pytest.raises(_GitError):
        _git_worktree_add(
            base,
            ["-B", "issue-1694-unpushed", str(tmp_path / "wt"), "HEAD"],
            log_path=str(log),
            default_branch="main",
        )

    assert _current_branch(base) == "issue-1694-unpushed"
    assert _run(base, "rev-parse", "HEAD") == head
    assert (base / "new.txt").exists()
    assert "REFUSING" in log.read_text()


def test_unpushed_check_accepts_a_head_published_under_another_remote_ref(
    repo_with_remote: Path, tmp_path: Path
) -> None:
    """"No `origin/<branch>`" is not the same as "these commits are unpublished".

    A worker that pushed nothing leaves the base parked on a branch that has
    no remote counterpart but whose HEAD *is* ``origin/main``.  Refusing that
    would make the common zero-commit case permanently unrecoverable.
    """
    base = repo_with_remote
    _park(base, "issue-1694-zero-commits", push=False)
    assert not _base_checkout_move_blockers(base, "issue-1694-zero-commits")

    _git_worktree_add(
        base,
        ["-B", "issue-1694-zero-commits", str(tmp_path / "wt"), "HEAD"],
        default_branch="main",
    )
    assert _current_branch(base) == "main"


def test_a_stash_in_the_base_is_refused(
    repo_with_remote: Path, tmp_path: Path
) -> None:
    """The stash is repo-wide and survives no branch switch cleanly."""
    base = repo_with_remote
    _park(base, "issue-1694-stash")
    (base / "README").write_text("stashed\n")
    _run(base, "stash", "push", "-m", "operator wip")
    log = tmp_path / "worker.log"
    log.write_text("")

    with pytest.raises(_GitError):
        _git_worktree_add(
            base,
            ["-B", "issue-1694-stash", str(tmp_path / "wt"), "HEAD"],
            log_path=str(log),
            default_branch="main",
        )

    assert _current_branch(base) == "issue-1694-stash"
    assert _run(base, "stash", "list").strip(), "the stash was dropped"
    assert "stash" in log.read_text()


def test_untracked_test_output_does_not_block_the_remedy(
    repo_with_remote: Path, tmp_path: Path
) -> None:
    """`.pytest.out` / `.cargo.out` are droppings, not work."""
    base = repo_with_remote
    _park(base, "issue-1694-testout")
    (base / ".pytest.out").write_text("=== 3 passed ===\n")
    (base / ".cargo.out").write_text("Finished\n")

    _git_worktree_add(
        base,
        ["-B", "issue-1694-testout", str(tmp_path / "wt"), "HEAD"],
        default_branch="main",
    )
    assert _current_branch(base) == "main"
    assert (base / ".pytest.out").exists(), "test output was deleted"


def test_base_already_on_the_default_branch_is_untouched(
    repo_with_remote: Path, tmp_path: Path
) -> None:
    """The normal case: no collision, no remedy, no behaviour change."""
    base = repo_with_remote
    calls: list[tuple[str, ...]] = []
    real_git = agent_mod._git

    def _spy(path: Path, *args: str, **kwargs: object) -> str:
        calls.append(args)
        return real_git(path, *args, **kwargs)

    agent_mod._git = _spy  # type: ignore[assignment]
    try:
        _git_worktree_add(
            base,
            ["-b", "issue-1694-fresh", str(tmp_path / "wt"), "origin/main"],
            default_branch="main",
        )
    finally:
        agent_mod._git = real_git  # type: ignore[assignment]

    assert _current_branch(base) == "main"
    assert not any(a[:1] == ("checkout",) for a in calls), (
        f"a checkout ran on the happy path: {calls}"
    )


def test_no_default_branch_keeps_the_1693_refusal_verbatim(
    repo_with_remote: Path, tmp_path: Path
) -> None:
    """The remedy needs a target; without one #1693's behaviour is unchanged.

    This is what keeps every #1693 test honest — a caller that cannot say
    where the base belongs must still refuse rather than guess.
    """
    base = repo_with_remote
    _park(base, "issue-1694-nodefault")

    with pytest.raises(_GitError):
        _git_worktree_add(
            base, ["-B", "issue-1694-nodefault", str(tmp_path / "wt"), "HEAD"]
        )
    assert _current_branch(base) == "issue-1694-nodefault"


def test_remedy_never_targets_a_linked_worktree(
    repo_with_remote: Path, tmp_path: Path
) -> None:
    """Only the main worktree is ever moved — a linked one is refused."""
    base = repo_with_remote
    linked = tmp_path / "linked"
    _run(base, "worktree", "add", "-b", "issue-1694-linked", str(linked))

    blockers = _base_checkout_move_blockers(linked, "issue-1694-linked")
    assert blockers, "a linked worktree was treated as movable"

    assert _restore_base_checkout_branch(
        linked, "issue-1694-linked", "main"
    ) is None
    assert _current_branch(linked) == "issue-1694-linked"


def test_the_collision_retry_for_a_real_linked_worktree_still_works(
    repo_with_remote: Path, tmp_path: Path
) -> None:
    """#460 Part 2 / #1693 must survive #1694: linked worktrees still evict."""
    base = repo_with_remote
    conflict = tmp_path / "conflict"
    _run(base, "worktree", "add", "-b", "issue-1694-evict", str(conflict))

    new_wt = tmp_path / "new-wt"
    _git_worktree_add(
        base,
        ["-B", "issue-1694-evict", str(new_wt), "HEAD"],
        default_branch="main",
    )
    assert new_wt.exists()
    assert not conflict.exists()
    assert _current_branch(base) == "main", "the base was moved for no reason"


# ── #3704: git's *other* collision message — "a branch named '<branch>' ──────
# already exists" — raised by the plain ``-b`` form whenever the branch ref
# exists at all, checked out anywhere or not.  A retried smoke-fix/test-fix
# dispatch picks the same branch name as its failed predecessor, so if that
# predecessor's worktree was already torn down (which never deletes the
# branch), the retry hits this exact message instead of "already used by
# worktree at ...".  All three sub-cases below funnel into the identical
# recovery machinery above; the branch ref is deleted once nothing has it
# checked out, since `-b` always means "I want a fresh branch here".


def test_branch_exists_but_checked_out_nowhere_is_freed_and_retried(
    repo_with_remote: Path, tmp_path: Path
) -> None:
    """The common case: a prior attempt's worktree was cleaned up, but `git
    worktree remove` never deletes the branch it held — so the stale ref is
    all that is left to collide with the retry."""
    base = repo_with_remote
    stale = tmp_path / "stale"
    _run(base, "worktree", "add", "-b", "issue-3704-stale", str(stale))
    _run(base, "worktree", "remove", str(stale))
    assert _run(base, "branch", "--list", "issue-3704-stale"), "branch should linger"

    log = tmp_path / "worker.log"
    log.write_text("")
    new_wt = tmp_path / "new-wt"
    _git_worktree_add(
        base,
        ["-b", "issue-3704-stale", str(new_wt)],
        log_path=str(log),
    )

    assert new_wt.exists(), "the worktree add did not succeed after the remedy"
    assert _current_branch(new_wt) == "issue-3704-stale"
    assert "not checked out in any worktree" in log.read_text()


def test_branch_exists_in_a_linked_worktree_is_evicted_and_retried(
    repo_with_remote: Path, tmp_path: Path
) -> None:
    """The branch is live in a linked worktree — evict it, same as #460/#1693,
    then delete the now-unchecked-out ref so the `-b` retry can recreate it."""
    base = repo_with_remote
    linked = tmp_path / "linked"
    _run(base, "worktree", "add", "-b", "issue-3704-linked", str(linked))

    new_wt = tmp_path / "new-wt"
    _git_worktree_add(
        base,
        ["-b", "issue-3704-linked", str(new_wt)],
    )

    assert new_wt.exists(), "the worktree add did not succeed after the remedy"
    assert not linked.exists(), "the stale linked worktree should have been evicted"
    assert _current_branch(new_wt) == "issue-3704-linked"


def test_branch_exists_in_the_base_checkout_is_restored_and_retried(
    repo_with_remote: Path, tmp_path: Path
) -> None:
    """The branch is checked out in the *base* checkout itself — move the base
    off it non-destructively (#1694), then delete the ref it leaves behind so
    the `-b` retry can recreate it fresh."""
    base = repo_with_remote
    _park(base, "issue-3704-parked")
    log = tmp_path / "worker.log"
    log.write_text("")

    new_wt = tmp_path / "new-wt"
    _git_worktree_add(
        base,
        ["-b", "issue-3704-parked", str(new_wt)],
        log_path=str(log),
        default_branch="main",
    )

    assert new_wt.exists(), "the worktree add did not succeed after the remedy"
    assert _current_branch(base) == "main", "base was not moved to the default branch"
    assert _current_branch(new_wt) == "issue-3704-parked"
    text = log.read_text()
    assert "base checkout was parked on 'issue-3704-parked'" in text, text
    assert "moved it to main" in text, text


# ── The helper's own contract ───────────────────────────────────────────────


def test_restore_is_a_no_op_when_the_branch_is_the_default_branch(
    repo_with_remote: Path, tmp_path: Path
) -> None:
    log = tmp_path / "l.log"
    log.write_text("")
    assert _restore_base_checkout_branch(
        repo_with_remote, "main", "main", log_path=str(log)
    ) is None
    assert _current_branch(repo_with_remote) == "main"
    assert "nothing to do" in log.read_text()


def test_restore_detaches_when_the_default_branch_is_unavailable(
    repo_with_remote: Path, tmp_path: Path
) -> None:
    """Freeing the branch still beats leaving the collision in place."""
    base = repo_with_remote
    # `main` is held by a linked worktree, so it cannot be checked out here.
    _run(base, "checkout", "-b", "issue-1694-detach")
    _run(base, "push", "-u", "origin", "issue-1694-detach")
    _run(base, "worktree", "add", str(tmp_path / "holds-main"), "main")

    log = tmp_path / "l.log"
    log.write_text("")
    result = _restore_base_checkout_branch(
        base, "issue-1694-detach", "main", log_path=str(log)
    )
    assert result == "HEAD (detached)", log.read_text()
    assert _current_branch(base) is None, "expected a detached HEAD"
    assert _run(base, "rev-parse", "--verify", "issue-1694-detach")


def test_current_branch_reports_none_for_detached_and_non_repos(
    repo_with_remote: Path, tmp_path: Path
) -> None:
    _run(repo_with_remote, "checkout", "--detach")
    assert _current_branch(repo_with_remote) is None
    assert _current_branch(tmp_path / "does-not-exist") is None


# ── Part A: nothing should park the base in the first place ─────────────────


def test_conflict_fix_briefing_does_not_send_the_worker_to_the_base_checkout(
) -> None:
    """The deterministic root cause: `cd <base>` + `git checkout <branch>`.

    The conflict-fix worker is dispatched with ``target_branch=entry.branch``,
    so the agent already hands it a worktree checked out on exactly that
    branch.  The briefing's old steps 1 and 3 were therefore not merely
    redundant — they were an instruction to park ``~/src/<repo>`` on the work
    branch and never put it back, which is precisely the state #1693 has to
    refuse and #1694 has to clear.
    """
    from coord.conflict_fix import (
        build_conflict_fix_briefing,
        build_semantic_conflict_briefing,
    )
    from coord.merge_queue import QueuedMerge

    entry = QueuedMerge(
        assignment_id="a1",
        repo_name="api",
        repo_github="o/api",
        branch="issue-77-thing",
        target_branch="main",
        issue_number=77,
        issue_title="thing",
        state="conflict",
    )
    for briefing in (
        build_conflict_fix_briefing(
            entry=entry, repo_path="/home/u/src/api", test_command="pytest"
        ),
        build_semantic_conflict_briefing(
            entry=entry, repo_path="/home/u/src/api", test_command="pytest"
        ),
    ):
        # No *instruction* to enter the base checkout or switch branches.  The
        # match is anchored on the step form ("1. `cd …`", "3. `git checkout
        # …`") so the explicit "do NOT cd there" prose — which necessarily
        # names the path — does not count as one.
        steps = [
            ln.strip() for ln in briefing.splitlines()
            if ln.strip()[:2].rstrip(".").isdigit()
        ]
        for step in steps:
            assert "cd /home/u/src/api" not in step, step
            assert "git checkout" not in step, step
            assert "git switch" not in step, step
        # …and it says so out loud rather than merely omitting it.
        assert "do NOT `git checkout`" in briefing, briefing
        assert "base checkout" in briefing
        assert "worktree" in briefing


def test_smoke_briefing_scopes_its_checkout_to_the_worktree() -> None:
    """The smoke worker genuinely needs a checkout — but not in the base."""
    from coord.smoke import SMOKE_SYSTEM_PROMPT, build_smoke_briefing

    briefing = build_smoke_briefing(
        repo_github="o/api", repo_name="api", branch="issue-88-b",
        issue_number=88, issue_title="t", smoke_command="make smoke",
        required_caps=[], timeout_seconds=60, is_worker=False,
    )
    assert "worktree" in briefing
    assert "base checkout" in briefing
    assert "base checkout" in SMOKE_SYSTEM_PROMPT
    assert "worktree" in SMOKE_SYSTEM_PROMPT


def test_agent_puts_the_base_back_when_a_worker_parked_it(
    repo_with_remote: Path, tmp_path: Path
) -> None:
    """Part A end-to-end: a worker that escapes its worktree is cleaned up.

    Simulates the #1642 shape directly — the worker runs ``git -C <base>
    checkout <its own branch>``.  At teardown the agent must notice the base
    is parked on the branch it just finished with and put it back.

    ``AgentServer.assign()`` runs ``_setup_worktree`` synchronously *before*
    spawning the worker, so by the time this shell command runs, the
    assignment's own linked worktree already holds
    ``issue-1694-escape-1`` — an ordinary ``git checkout`` of that branch
    anywhere else in the repo is refused by git ("already used by worktree
    at ...", exit 128). A prior version of this test masked that failure
    with ``|| true``, so the base checkout was never actually parked and
    this test passed unconditionally regardless of whether Part A's restore
    wiring even ran. ``--ignore-other-worktrees`` bypasses that safety check
    exactly the way a real worker's stray ``cd ~/src/<repo> && git checkout
    <branch>`` would if the branch happened to already be checked out
    elsewhere — reproducing the actual #1642 parked-base state so this test
    exercises the restore for real.
    """
    base = repo_with_remote
    server = AgentServer(
        machine_name="t", repos=["api"],
        repo_paths={"api": str(base)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: [
            "sh", "-c",
            f"git -C {base} checkout --ignore-other-worktrees "
            f"issue-1694-escape-{spec.issue_number}",
        ],
    )
    spec = AssignmentSpec(
        repo_name="api", repo_path=str(base),
        issue_number=1, issue_title="escape", briefing="b",
        branch="main",
        target_branch="issue-1694-escape-1",
    )
    a = server.assign(spec)
    server.wait_for(a.id, timeout=30)

    assert a.exit_code == 0, (
        "the worker's own `git checkout --ignore-other-worktrees` must have "
        "succeeded — otherwise the base was never actually parked and this "
        "test would pass without exercising the restore at all"
    )
    assert _current_branch(base) == "main", (
        "the agent left the base checkout parked on the worker's branch"
    )
    assert _run(base, "rev-parse", "--verify", "issue-1694-escape-1"), (
        "the branch itself was destroyed — the restore must be non-destructive"
    )


def test_agent_leaves_an_unrelated_parked_branch_alone(
    repo_with_remote: Path, tmp_path: Path
) -> None:
    """An operator's own checkout, on their own branch, is none of our business."""
    base = repo_with_remote
    server = AgentServer(
        machine_name="t", repos=["api"],
        repo_paths={"api": str(base)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["sh", "-c", "true"],
    )
    spec = AssignmentSpec(
        repo_name="api", repo_path=str(base),
        issue_number=2, issue_title="unrelated", briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    server.wait_for(a.id, timeout=30)

    # The operator parks their own checkout after the worktree was made.
    _run(base, "checkout", "-b", "operators-own-branch")
    assignment = AgentAssignment(id=a.id, spec=spec)
    assignment.branch = "issue-2-unrelated"
    server._restore_base_checkout(
        assignment, base, {"issue-2-unrelated"}
    )
    assert _current_branch(base) == "operators-own-branch"
