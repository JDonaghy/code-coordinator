"""Tests that the agent captures the worker's branch name after completion."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coord.agent import ADVISORY, DONE, FAILED, AgentServer, AssignmentSpec

from .conftest import NOOP_WORKER_ARGV


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo_clone(tmp_path: Path) -> Path:
    """A git repo on `main` with one commit. Worker runs in a worktree."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README").write_text("hi\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "initial")
    return repo


def test_branch_captured_from_worktree(
    tmp_path: Path, repo_clone: Path
) -> None:
    """The worktree is auto-created on issue-<N>-<slug>; branch is captured from it."""
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_clone)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: NOOP_WORKER_ARGV,
    )
    spec = AssignmentSpec(
        repo_name="api", repo_path=str(repo_clone),
        issue_number=42, issue_title="add feature X", briefing="b",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)
    # Worker makes no commits → advisory (#448)
    assert final.status == ADVISORY
    assert final.branch == "issue-42-add-feature-x"

    # /status exposes it too
    status = server.list_assignments()
    completed = status["completed"]
    assert completed[0]["branch"] == "issue-42-add-feature-x"
    server.shutdown()


def test_worktree_path_stored_on_assignment(
    tmp_path: Path, repo_clone: Path
) -> None:
    """The worktree path is set on the assignment for inspection."""
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_clone)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: NOOP_WORKER_ARGV,
    )
    spec = AssignmentSpec(
        repo_name="api", repo_path=str(repo_clone),
        issue_number=1, issue_title="t", briefing="b",
    )
    a = server.assign(spec)
    assert a.worktree_path is not None
    assert "worktrees" in a.worktree_path
    final = server.wait_for(a.id, timeout=10)
    # Worker makes no commits → advisory (#448)
    assert final.status == ADVISORY
    server.shutdown()


def test_main_repo_untouched_by_worker(
    tmp_path: Path, repo_clone: Path
) -> None:
    """The main repo clone should stay on its default branch after worker completes."""
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_clone)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: NOOP_WORKER_ARGV,
    )
    spec = AssignmentSpec(
        repo_name="api", repo_path=str(repo_clone),
        issue_number=1, issue_title="t", briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    server.wait_for(a.id, timeout=10)

    # Main repo should still be on main
    main_branch = _git(repo_clone, "rev-parse", "--abbrev-ref", "HEAD")
    assert main_branch == "main"
    server.shutdown()


def test_fresh_branch_ignores_existing_local_branch(
    tmp_path: Path, repo_clone: Path
) -> None:
    """With fresh_branch=True, a stale local branch is deleted and a new one is created from main."""
    # Create a stale branch with old content
    _git(repo_clone, "checkout", "-b", "issue-42-add-feature-x")
    (repo_clone / "stale.txt").write_text("old content\n")
    _git(repo_clone, "add", "stale.txt")
    _git(repo_clone, "commit", "-m", "stale work")
    stale_sha = _git(repo_clone, "rev-parse", "HEAD")
    _git(repo_clone, "checkout", "main")

    # Add new work to main after the stale branch
    (repo_clone / "new_file.txt").write_text("new\n")
    _git(repo_clone, "add", "new_file.txt")
    _git(repo_clone, "commit", "-m", "new main work")
    main_sha = _git(repo_clone, "rev-parse", "HEAD")

    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_clone)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: NOOP_WORKER_ARGV,
    )
    spec = AssignmentSpec(
        repo_name="api", repo_path=str(repo_clone),
        issue_number=42, issue_title="add feature X", briefing="b",
        fresh_branch=True,
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)
    # Worker makes no commits (fresh branch from main) → advisory (#448)
    assert final.status == ADVISORY
    assert final.branch == "issue-42-add-feature-x"

    # The branch tip in the main repo should match main, not the stale branch
    branch_sha = _git(repo_clone, "rev-parse", "issue-42-add-feature-x")
    assert branch_sha == main_sha
    assert branch_sha != stale_sha
    server.shutdown()


def test_fresh_branch_false_reuses_existing_branch(
    tmp_path: Path, repo_clone: Path
) -> None:
    """Without fresh_branch, an existing branch is reused (default behavior)."""
    _git(repo_clone, "checkout", "-b", "issue-42-add-feature-x")
    (repo_clone / "existing.txt").write_text("existing work\n")
    _git(repo_clone, "add", "existing.txt")
    _git(repo_clone, "commit", "-m", "existing work")
    existing_sha = _git(repo_clone, "rev-parse", "HEAD")
    _git(repo_clone, "checkout", "main")

    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_clone)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: NOOP_WORKER_ARGV,
    )
    spec = AssignmentSpec(
        repo_name="api", repo_path=str(repo_clone),
        issue_number=42, issue_title="add feature X", briefing="b",
        fresh_branch=False,
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)
    assert final.status == DONE

    # Branch tip should match the existing branch, not main
    branch_sha = _git(repo_clone, "rev-parse", "issue-42-add-feature-x")
    assert branch_sha == existing_sha
    server.shutdown()


# ── #389: leftover-branch hygiene (repos WITH a remote) ─────────────────────


@pytest.fixture
def repo_with_origin(tmp_path: Path) -> tuple[Path, Path]:
    """A working clone on `main` whose `origin` is a bare repo.

    Returns (clone, origin).  This mirrors production, where every repo has a
    remote — unlike :func:`repo_clone`, which is local-only.
    """
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "-b", "main")

    clone = tmp_path / "clone"
    clone.mkdir()
    _git(clone, "init", "-b", "main")
    _git(clone, "config", "user.email", "t@t.com")
    _git(clone, "config", "user.name", "Test")
    _git(clone, "remote", "add", "origin", str(origin))
    (clone / "README").write_text("hi\n")
    _git(clone, "add", "README")
    _git(clone, "commit", "-m", "initial")
    _git(clone, "push", "-u", "origin", "main")
    return clone, origin


def test_local_only_leftover_branch_not_reused_when_origin_exists(
    tmp_path: Path, repo_with_origin: tuple[Path, Path]
) -> None:
    """#389: a local `issue-N` branch that is NOT on origin is an untrusted
    leftover — the worker must branch off origin/main, not the leftover."""
    clone, _origin = repo_with_origin
    main_sha = _git(clone, "rev-parse", "main")

    # A leftover local branch from a prior failed assignment — never pushed.
    _git(clone, "checkout", "-b", "issue-42-add-feature-x")
    (clone / "stale.txt").write_text("reverted merged work\n")
    _git(clone, "add", "stale.txt")
    _git(clone, "commit", "-m", "stale leftover")
    stale_sha = _git(clone, "rev-parse", "HEAD")
    _git(clone, "checkout", "main")

    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(clone)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: NOOP_WORKER_ARGV,
    )
    spec = AssignmentSpec(
        repo_name="api", repo_path=str(clone),
        issue_number=42, issue_title="add feature X", briefing="b",
        fresh_branch=False,
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)
    # Worker makes no commits (fresh from origin/main) → advisory (#448)
    assert final.status == ADVISORY
    assert final.branch == "issue-42-add-feature-x"

    # The worker's branch must start from origin/main — NOT the stale leftover.
    branch_sha = _git(clone, "rev-parse", "issue-42-add-feature-x")
    assert branch_sha == main_sha
    assert branch_sha != stale_sha
    server.shutdown()


def test_origin_branch_reset_to_remote_tip_on_continuation(
    tmp_path: Path, repo_with_origin: tuple[Path, Path]
) -> None:
    """#389: when origin HAS the branch, the worktree is hard-reset to the
    remote tip so a divergent local copy of the branch can't ride in."""
    clone, _origin = repo_with_origin

    # Real remote work on the branch (origin/issue-42 ahead of main).
    _git(clone, "checkout", "-b", "issue-42-add-feature-x")
    (clone / "feature.txt").write_text("real remote work\n")
    _git(clone, "add", "feature.txt")
    _git(clone, "commit", "-m", "remote work")
    origin_sha = _git(clone, "rev-parse", "HEAD")
    _git(clone, "push", "-u", "origin", "issue-42-add-feature-x")

    # Corrupt the LOCAL copy of the branch so it diverges from origin (the
    # #389 failure mode: a stale local branch shadowing the real remote one).
    # Leave the branch before force-updating it — git refuses to -f a
    # currently checked-out branch.
    _git(clone, "checkout", "main")
    _git(clone, "branch", "-f", "issue-42-add-feature-x", "main")
    stale_local_sha = _git(clone, "rev-parse", "issue-42-add-feature-x")
    assert stale_local_sha != origin_sha

    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(clone)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: NOOP_WORKER_ARGV,
    )
    spec = AssignmentSpec(
        repo_name="api", repo_path=str(clone),
        issue_number=42, issue_title="add feature X", briefing="b",
        fresh_branch=False,
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)
    assert final.status == DONE

    # The branch must be reset to the remote tip, not the stale local copy.
    branch_sha = _git(clone, "rev-parse", "issue-42-add-feature-x")
    assert branch_sha == origin_sha
    server.shutdown()


def test_worktree_setup_failure_marks_assignment_failed(tmp_path: Path) -> None:
    """If worktree setup fails (e.g. not a git repo), the assignment goes to FAILED."""
    not_a_repo = tmp_path / "not-git"
    not_a_repo.mkdir()
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(not_a_repo)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: NOOP_WORKER_ARGV,
    )
    spec = AssignmentSpec(
        repo_name="api", repo_path=str(not_a_repo),
        issue_number=1, issue_title="t", briefing="b",
    )
    a = server.assign(spec)
    assert a.status == FAILED
    assert "worktree setup failed" in a.error
    server.shutdown()


# ── #target_branch: explicit branch override ────────────────────────────────


def test_target_branch_overrides_slugified_title(
    tmp_path: Path, repo_clone: Path
) -> None:
    """When `AssignmentSpec.target_branch` is set, the agent checks
    that branch out instead of deriving a new one from the slugified
    issue title.  This is the auto-loop's fix-dispatch path — fix
    workers must push to the original work's branch so the existing
    PR gets the fix commits."""
    # First worker creates the original branch (status:ready style).
    server = AgentServer(
        machine_name="t", repos=["api"],
        repo_paths={"api": str(repo_clone)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: NOOP_WORKER_ARGV,
    )
    spec1 = AssignmentSpec(
        repo_name="api", repo_path=str(repo_clone),
        issue_number=42, issue_title="add feature X", briefing="b",
    )
    a1 = server.assign(spec1)
    final1 = server.wait_for(a1.id, timeout=10)
    # Worker makes no commits → advisory (#448)
    assert final1.status == ADVISORY
    original_branch = final1.branch  # `issue-42-add-feature-x`
    assert original_branch == "issue-42-add-feature-x"

    # Second worker (fix iteration) — different issue title that would
    # normally produce a NEW branch, but target_branch pins it to the
    # original.
    spec2 = AssignmentSpec(
        repo_name="api", repo_path=str(repo_clone),
        issue_number=42, issue_title="[fix-1] add feature X",
        briefing="b",
        target_branch=original_branch,
    )
    a2 = server.assign(spec2)
    final2 = server.wait_for(a2.id, timeout=10)
    # Worker makes no commits → advisory (#448)
    assert final2.status == ADVISORY
    # Captured branch must be the original — not a `[fix-1]` derivation.
    assert final2.branch == original_branch, (
        f"target_branch override failed: expected {original_branch!r}, "
        f"got {final2.branch!r} (the fix would push to an orphan branch)"
    )
    server.shutdown()


def test_no_target_branch_uses_slugified_title(
    tmp_path: Path, repo_clone: Path
) -> None:
    """Without target_branch, the agent derives the branch from
    `issue-{N}-{slug(title)}` as before — backwards compatible."""
    server = AgentServer(
        machine_name="t", repos=["api"],
        repo_paths={"api": str(repo_clone)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: NOOP_WORKER_ARGV,
    )
    spec = AssignmentSpec(
        repo_name="api", repo_path=str(repo_clone),
        issue_number=99, issue_title="my cool change", briefing="b",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)
    # Worker makes no commits → advisory (#448)
    assert final.status == ADVISORY
    assert final.branch == "issue-99-my-cool-change"
    server.shutdown()
