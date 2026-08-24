"""Tests for git worktree isolation in the agent server."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from coord.agent import (
    ADVISORY,
    DONE,
    FAILED,
    PENDING,
    RUNNING,
    AgentAssignment,
    AgentServer,
    AssignmentSpec,
    _slugify,
)

from .conftest import NOOP_WORKER_ARGV


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A minimal git repo on `main` with one commit."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-b", "main")
    _git(r, "config", "user.email", "t@t.com")
    _git(r, "config", "user.name", "Test")
    (r / "README").write_text("init\n")
    _git(r, "add", "README")
    _git(r, "commit", "-m", "initial")
    return r


@pytest.fixture
def repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    """A git repo with a bare remote. Returns (local, remote)."""
    remote = tmp_path / "remote"
    remote.mkdir()
    _git(remote, "init", "--bare", "-b", "main")

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    _git(seed, "config", "user.email", "t@t.com")
    _git(seed, "config", "user.name", "Test")
    (seed / "README").write_text("init\n")
    _git(seed, "add", "README")
    _git(seed, "commit", "-m", "initial")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "main")

    local = tmp_path / "local"
    _git(tmp_path, "clone", str(remote), str(local))
    _git(local, "config", "user.email", "t@t.com")
    _git(local, "config", "user.name", "Test")
    return local, remote


def _server(tmp_path: Path, repo_path: Path, *, argv: list[str] | None = None) -> AgentServer:
    if argv is None:
        argv = NOOP_WORKER_ARGV
    return AgentServer(
        machine_name="t",
        repos=["api"],
        state_dir=tmp_path / "state",
        worker_command=lambda spec: argv,
        repo_paths={"api": str(repo_path)},
    )


def _spec(repo_path: Path, **overrides) -> AssignmentSpec:
    base = dict(
        repo_name="api",
        repo_path=str(repo_path),
        issue_number=1,
        issue_title="fix the bug",
        briefing="b",
        branch="main",
    )
    base.update(overrides)
    return AssignmentSpec(**base)


# Portable replacement for the POSIX-only
# `"echo <line> >> README && git add README && git commit -m '<msg>'"`
# shell one-liner (#2725): appends a line to README then commits it, via
# real `git` subprocess calls rather than shell redirection/chaining.
_COMMIT_ONE_LINE_SCRIPT = (
    "import subprocess\n"
    "open('README', 'a').write({line!r} + chr(10))\n"
    "subprocess.run(['git', 'add', 'README'], check=True)\n"
    "subprocess.run(['git', 'commit', '-m', {msg!r}], check=True)\n"
)


# ── _slugify tests ────────────────────────────────────────────────────────


class TestSlugify:
    def test_basic(self):
        assert _slugify("Fix the Bug") == "fix-the-bug"

    def test_special_chars(self):
        assert _slugify("Add feature: X & Y!") == "add-feature-x-y"

    def test_truncation(self):
        long_title = "a" * 60
        result = _slugify(long_title)
        assert len(result) <= 40

    def test_trailing_dash_stripped(self):
        # After truncation, trailing dashes should be removed
        result = _slugify("a" * 39 + "-b")
        assert not result.endswith("-")


# ── Worktree lifecycle ────────────────────────────────────────────────────


class TestWorktreeCreation:
    def test_worktree_created_before_spawn(self, tmp_path: Path, repo: Path) -> None:
        """Worktree should exist when the worker command runs."""
        canary = tmp_path / "canary.txt"
        server = _server(
            tmp_path, repo,
            # Worker checks it's in a worktree (not the main repo) and writes canary
            argv=[
                sys.executable, "-c",
                "import os, subprocess\n"
                "if os.path.exists('README'):\n"
                "    r = subprocess.run(\n"
                "        ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],\n"
                "        capture_output=True, text=True, check=True,\n"
                "    )\n"
                f"    open({str(canary)!r}, 'w').write(r.stdout)\n",
            ],
        )
        a = server.assign(_spec(repo))
        final = server.wait_for(a.id, timeout=10)
        assert final.status in (DONE, ADVISORY)  # no commits → advisory (#448)
        assert canary.exists()
        branch = canary.read_text().strip()
        assert branch.startswith("issue-1-")
        server.shutdown()

    def test_worktree_path_under_state_dir(self, tmp_path: Path, repo: Path) -> None:
        server = _server(tmp_path, repo)
        a = server.assign(_spec(repo))
        assert a.worktree_path is not None
        assert str(tmp_path / "state" / "worktrees") in a.worktree_path
        server.wait_for(a.id, timeout=10)
        server.shutdown()

    def test_worktree_branch_name_includes_issue_number(
        self, tmp_path: Path, repo: Path
    ) -> None:
        server = _server(tmp_path, repo)
        a = server.assign(_spec(repo, issue_number=42, issue_title="Add widget"))
        final = server.wait_for(a.id, timeout=10)
        assert final.status in (DONE, ADVISORY)  # no commits → advisory (#448)
        assert final.branch == "issue-42-add-widget"
        server.shutdown()

    def test_worker_runs_in_worktree_not_main_repo(
        self, tmp_path: Path, repo: Path
    ) -> None:
        """Worker cwd should be the worktree, not the main repo."""
        cwd_file = tmp_path / "cwd.txt"
        server = _server(
            tmp_path, repo,
            argv=[
                sys.executable, "-c",
                f"import os; open({str(cwd_file)!r}, 'w').write(os.getcwd())",
            ],
        )
        a = server.assign(_spec(repo))
        final = server.wait_for(a.id, timeout=10)
        assert final.status in (DONE, ADVISORY)  # no commits → advisory (#448)
        worker_cwd = cwd_file.read_text().strip()
        # Worker should NOT be in the main repo
        assert worker_cwd != str(repo)
        # Worker should be in the worktree path
        assert a.worktree_path is not None
        assert worker_cwd == a.worktree_path
        server.shutdown()


class TestWorktreeCleanup:
    def test_worktree_removed_after_success(self, tmp_path: Path, repo: Path) -> None:
        server = _server(tmp_path, repo)
        a = server.assign(_spec(repo))
        final = server.wait_for(a.id, timeout=10)
        assert final.status in (DONE, ADVISORY)  # no commits → advisory (#448)
        # Worktree should be cleaned up
        assert not Path(final.worktree_path).exists()
        server.shutdown()

    def test_worktree_removed_after_failure(self, tmp_path: Path, repo: Path) -> None:
        server = _server(
            tmp_path, repo,
            argv=[sys.executable, "-c", "import sys; sys.exit(1)"],
        )
        a = server.assign(_spec(repo))
        final = server.wait_for(a.id, timeout=10)
        assert final.status == FAILED
        # Worktree should still be cleaned up even on failure
        assert not Path(final.worktree_path).exists()
        server.shutdown()

    def test_worktree_removed_on_cancel(self, tmp_path: Path, repo: Path) -> None:
        import time
        server = _server(tmp_path, repo, argv=["/bin/sh", "-c", "sleep 30"])
        a = server.assign(_spec(repo))
        # Wait until running
        for _ in range(50):
            if server.get(a.id).status == RUNNING:
                break
            time.sleep(0.02)
        wt_path = a.worktree_path
        server.cancel(a.id)
        assert not Path(wt_path).exists()
        server.shutdown()

    def test_main_repo_stays_on_default_branch(
        self, tmp_path: Path, repo: Path
    ) -> None:
        server = _server(tmp_path, repo)
        a = server.assign(_spec(repo, branch="main"))
        server.wait_for(a.id, timeout=10)
        # Main repo should remain on main
        main_branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        assert main_branch == "main"
        server.shutdown()


class TestWorktreeWithRemote:
    def test_push_on_success(
        self, tmp_path: Path, repo_with_remote: tuple[Path, Path]
    ) -> None:
        """On success, _reap pushes the branch to origin."""
        local, remote = repo_with_remote
        server = _server(
            tmp_path, local,
            argv=[sys.executable, "-c", _COMMIT_ONE_LINE_SCRIPT.format(line="change", msg="work")],
        )
        a = server.assign(_spec(local, issue_number=5, issue_title="test push"))
        final = server.wait_for(a.id, timeout=10)
        assert final.status == DONE
        # The branch should exist on the remote
        refs = _git(remote, "branch", "--list")
        assert "issue-5-test-push" in refs
        server.shutdown()

    def test_no_push_on_failure(
        self, tmp_path: Path, repo_with_remote: tuple[Path, Path]
    ) -> None:
        """On failure, _reap should NOT push the branch."""
        local, remote = repo_with_remote
        server = _server(
            tmp_path, local,
            argv=[sys.executable, "-c", "import sys; sys.exit(1)"],
        )
        a = server.assign(_spec(local, issue_number=6, issue_title="fail no push"))
        final = server.wait_for(a.id, timeout=10)
        assert final.status == FAILED
        # The branch should NOT exist on the remote
        refs = _git(remote, "branch", "--list")
        assert "issue-6-fail-no-push" not in refs
        server.shutdown()

    def test_push_timeout_does_not_block_status_update(
        self, tmp_path: Path, repo_with_remote: tuple[Path, Path]
    ) -> None:
        """If the reap-time push times out, the assignment must still reach DONE.

        This is the regression test for the hang described in issue #204: a
        subprocess.TimeoutExpired raised by _git was not caught by the
        ``except _GitError`` handler, killing the reap thread before the status
        update ran and leaving the assignment permanently stuck in 'running'.
        """
        import unittest.mock as mock
        from coord import agent as agent_mod

        local, _remote = repo_with_remote
        original_git = agent_mod._git

        def _git_push_timeout(cwd: Path, *args: str, **kwargs) -> str:
            if "push" in args:
                raise subprocess.TimeoutExpired(["git", "push"], 60.0)
            return original_git(cwd, *args, **kwargs)

        server = _server(
            tmp_path, local,
            argv=[sys.executable, "-c", _COMMIT_ONE_LINE_SCRIPT.format(line="change", msg="work")],
        )
        with mock.patch.object(agent_mod, "_git", side_effect=_git_push_timeout):
            a = server.assign(_spec(local, issue_number=8, issue_title="push timeout"))
            final = server.wait_for(a.id, timeout=10)

        assert final.status == DONE, (
            f"Assignment stuck in '{final.status}' after push timeout — "
            "reap thread did not complete status update"
        )
        server.shutdown()

    def test_retry_reuses_existing_remote_branch(
        self, tmp_path: Path, repo_with_remote: tuple[Path, Path]
    ) -> None:
        """If a branch already exists on remote, worktree checks it out instead of creating."""
        local, remote = repo_with_remote
        # First run: create and push a branch
        server1 = _server(
            tmp_path, local,
            argv=[sys.executable, "-c", _COMMIT_ONE_LINE_SCRIPT.format(line="v1", msg="v1")],
        )
        a1 = server1.assign(_spec(local, issue_number=7, issue_title="retry test"))
        final1 = server1.wait_for(a1.id, timeout=10)
        assert final1.status == DONE
        assert final1.branch == "issue-7-retry-test"
        server1.shutdown()

        # Second run: should pick up the existing branch
        server2 = AgentServer(
            machine_name="t",
            repos=["api"],
            state_dir=tmp_path / "state2",
            worker_command=lambda spec: [
                sys.executable, "-c", _COMMIT_ONE_LINE_SCRIPT.format(line="v2", msg="v2")
            ],
            repo_paths={"api": str(local)},
        )
        a2 = server2.assign(_spec(local, issue_number=7, issue_title="retry test"))
        final2 = server2.wait_for(a2.id, timeout=10)
        assert final2.status == DONE
        assert final2.branch == "issue-7-retry-test"
        server2.shutdown()


class TestWorktreeSetupFailure:
    def test_non_git_directory_fails(self, tmp_path: Path) -> None:
        not_a_repo = tmp_path / "not-git"
        not_a_repo.mkdir()
        server = AgentServer(
            machine_name="t",
            repos=["api"],
            state_dir=tmp_path / "state",
            worker_command=lambda spec: NOOP_WORKER_ARGV,
            repo_paths={"api": str(not_a_repo)},
        )
        a = server.assign(_spec(not_a_repo))
        assert a.status == FAILED
        assert "worktree setup failed" in a.error
        server.shutdown()

    def test_worker_not_spawned_on_setup_failure(self, tmp_path: Path) -> None:
        not_a_repo = tmp_path / "not-git"
        not_a_repo.mkdir()
        canary = tmp_path / "canary.txt"
        server = AgentServer(
            machine_name="t",
            repos=["api"],
            state_dir=tmp_path / "state",
            worker_command=lambda spec: [
                sys.executable, "-c", f"open({str(canary)!r}, 'w').close()"
            ],
            repo_paths={"api": str(not_a_repo)},
        )
        a = server.assign(_spec(not_a_repo))
        assert a.status == FAILED
        assert not canary.exists()
        server.shutdown()


class TestWorktreePersistence:
    def test_worktree_path_persisted_in_state(
        self, tmp_path: Path, repo: Path
    ) -> None:
        server = _server(tmp_path, repo)
        a = server.assign(_spec(repo))
        server.wait_for(a.id, timeout=10)

        state = json.loads((tmp_path / "state" / "agent_state.json").read_text())
        entry = next(e for e in state["assignments"] if e["id"] == a.id)
        assert entry["worktree_path"] is not None
        assert a.id in entry["worktree_path"]
        server.shutdown()

    def test_backward_compat_no_worktree_path(self, tmp_path: Path) -> None:
        """Old state files without worktree_path should load fine."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "agent_state.json").write_text(
            json.dumps({
                "machine": "t",
                "capabilities": [],
                "repos": ["api"],
                "assignments": [{
                    "id": "old123",
                    "status": "done",
                    "pid": None,
                    "started_at": 1.0,
                    "finished_at": 2.0,
                    "exit_code": 0,
                    "log_path": None,
                    "error": None,
                    "branch": "issue-1-old",
                    "worktree_path": None,
                    "spec": {
                        "repo_name": "api",
                        "repo_path": str(tmp_path),
                        "issue_number": 1,
                        "issue_title": "old",
                        "briefing": "b",
                        "files_allowed": [],
                        "files_forbidden": [],
                        "branch": "main",
                    },
                }],
            })
        )
        server = AgentServer(
            machine_name="t", repos=["api"], state_dir=state_dir
        )
        recovered = server.get("old123")
        assert recovered is not None
        assert recovered.status == DONE
        assert recovered.worktree_path is None
        server.shutdown()


class TestWorktreeStartupPrune:
    def test_prune_runs_on_init(self, tmp_path: Path, repo: Path) -> None:
        """AgentServer.__init__ should call git worktree prune without error."""
        # Just ensure no exception is raised when repo_paths has entries
        server = AgentServer(
            machine_name="t",
            repos=["api"],
            state_dir=tmp_path / "state",
            worker_command=lambda spec: NOOP_WORKER_ARGV,
            repo_paths={"api": str(repo)},
        )
        # If prune failed silently, we still succeed
        assert server is not None
        server.shutdown()

    def test_prune_tolerates_missing_repo_path(self, tmp_path: Path) -> None:
        """_prune_worktrees must not crash when a configured repo_path doesn't exist.

        subprocess.run raises FileNotFoundError (not _GitError) when its cwd
        argument points to a non-existent directory.  A stale editable install
        or a deleted worktree used as a source directory can trigger this on
        agent startup (e.g. after exec_restart following /update).  Regression
        test for issue #280.

        Calls ``_prune_worktrees`` directly — relying on ``_load_state`` to
        invoke it is unreliable because ``_load_state`` returns early when
        ``state_path`` doesn't exist (which it doesn't, in this test).
        """
        nonexistent = str(tmp_path / "repo_that_was_deleted")
        # Path deliberately does NOT exist — prune must survive it.
        server = AgentServer(
            machine_name="t",
            repos=["api"],
            state_dir=tmp_path / "state",
            worker_command=lambda spec: NOOP_WORKER_ARGV,
            repo_paths={"api": nonexistent},
        )
        # Direct call — this is what's actually being regression-tested.
        # Without the (_GitError, FileNotFoundError, OSError) catch in
        # _prune_worktrees, this raises FileNotFoundError.
        server._prune_worktrees()
        server.shutdown()

    def test_prune_continues_after_one_missing_path(self, tmp_path: Path, repo: Path) -> None:
        """When one repo_path is missing, _prune_worktrees should still prune the rest.

        Direct call (see test_prune_tolerates_missing_repo_path) — going
        through ``__init__`` doesn't reach ``_prune_worktrees`` when the state
        file doesn't yet exist.
        """
        nonexistent = str(tmp_path / "gone")
        # Two repos: one valid, one missing.  Both should be attempted; neither
        # should abort the loop.
        server = AgentServer(
            machine_name="t",
            repos=["api", "sdk"],
            state_dir=tmp_path / "state",
            worker_command=lambda spec: NOOP_WORKER_ARGV,
            repo_paths={"api": str(repo), "sdk": nonexistent},
        )
        # Direct call — must not raise even though "sdk" path is missing.
        server._prune_worktrees()
        server.shutdown()


class TestParallelWorktrees:
    def test_two_assignments_same_repo_different_issues(
        self, tmp_path: Path, repo: Path
    ) -> None:
        """Two assignments on the same repo with different issues should work in parallel."""
        import time

        server = _server(
            tmp_path, repo,
            argv=[sys.executable, "-c", "import time; time.sleep(0.5)"],
        )
        a1 = server.assign(_spec(repo, issue_number=10, issue_title="first"))
        a2 = server.assign(_spec(repo, issue_number=11, issue_title="second"))

        # Both should get different worktree paths
        assert a1.worktree_path != a2.worktree_path

        # Both should eventually complete
        final1 = server.wait_for(a1.id, timeout=10)
        final2 = server.wait_for(a2.id, timeout=10)
        assert final1.status in (DONE, ADVISORY)  # no commits → advisory (#448)
        assert final2.status in (DONE, ADVISORY)  # no commits → advisory (#448)
        assert final1.branch == "issue-10-first"
        assert final2.branch == "issue-11-second"
        server.shutdown()


# ── #1468: rescued WIP commit must not poison the branch with a merge ──────


class TestPullRebaseDefault:
    """#1468: a rescued WIP commit (coordinator-authored, pushed to the
    branch by `_rescue_uncommitted_work` when a worker dies mid-flight) can
    leave the remote branch ahead of a fresh worktree's local branch. If the
    next worker's own push is then rejected non-fast-forward and it reaches
    for a plain `git pull`, git's default `pull.rebase=false` merges —
    producing a two-parent commit that GitHub refuses to rebase-merge
    forever (#1467). Setting `pull.rebase=true` on the worktree at creation
    time makes that same `git pull` rebase instead, keeping history linear.
    """

    def _assignment(self, spec: AssignmentSpec, log_path: Path) -> AgentAssignment:
        a = AgentAssignment(id="a-" + spec.branch, spec=spec, status=PENDING)
        a.log_path = str(log_path)
        return a

    def test_pull_rebase_set_on_worktree_not_on_base_checkout(
        self, tmp_path: Path, repo_with_remote: tuple[Path, Path]
    ) -> None:
        local, _remote = repo_with_remote
        server = _server(tmp_path, local)
        spec = _spec(local, issue_number=1468, issue_title="pull rebase config")
        assignment = self._assignment(spec, tmp_path / "a.log")

        wt = server._setup_worktree(assignment, local)

        assert _git(wt, "config", "--get", "pull.rebase") == "true"
        # `local` is the operator's own checkout (the `repo_path` argument) —
        # its config must be left alone, never mutated as a side effect of
        # dispatching work into a worktree of it.
        base_cfg = subprocess.run(
            ["git", "config", "--get", "pull.rebase"],
            cwd=str(local), capture_output=True, text=True,
        )
        assert base_cfg.returncode != 0, (
            f"pull.rebase leaked onto the base checkout: {base_cfg.stdout!r}"
        )

    def test_rescued_wip_commit_then_pull_stays_linear(
        self, tmp_path: Path, repo_with_remote: tuple[Path, Path]
    ) -> None:
        """Drives the exact step-3-to-5 chain from #1468: a rescue commit is
        already on the remote branch; a second, freshly-branched assignment
        makes its own commit, its push is rejected non-fast-forward, and it
        `git pull`s. With `pull.rebase=true` on the worktree, the resulting
        history has no merge commit."""
        local, remote = repo_with_remote
        branch_name = "issue-1468-fix-poison"

        # Step 1-2: the coordinator's rescue path committed and pushed a WIP
        # snapshot to the branch, from a worktree that has since been torn
        # down. Simulate that directly on `local` without going through
        # `_setup_worktree`/`_rescue_uncommitted_work` — this test is about
        # what the *next* worktree does when it meets that commit on origin.
        _git(local, "checkout", "-b", branch_name)
        (local / "rescued.txt").write_text("rescued work\n")
        _git(local, "add", "rescued.txt")
        _git(
            local, "commit", "-m",
            "WIP [coord-rescue] #1468: uncommitted worker changes preserved "
            "by the coordinator",
        )
        _git(local, "push", "-u", "origin", branch_name)
        _git(local, "checkout", "main")
        _git(local, "branch", "-D", branch_name)

        # Step 3: a second assignment is dispatched fresh off main (as the
        # coordinator's retry does) for the SAME branch name.
        server = _server(tmp_path, local)
        spec = _spec(
            local, issue_number=1468, issue_title="fix poison",
            fresh_branch=True,
        )
        assert spec.branch != branch_name  # sanity: default_branch, not target
        assignment = self._assignment(spec, tmp_path / "a2.log")
        wt = server._setup_worktree(assignment, local)
        assert _git(wt, "rev-parse", "--abbrev-ref", "HEAD") == branch_name
        # Freshly branched off main — must NOT already contain the rescue
        # commit, or the push below would be a fast-forward and prove nothing.
        assert not (wt / "rescued.txt").exists()
        assert _git(wt, "config", "--get", "pull.rebase") == "true"

        # The worker "reimplements cleanly" — its own, divergent commit.
        (wt / "clean.py").write_text("def clean():\n    return 2\n")
        _git(wt, "add", "clean.py")
        _git(wt, "commit", "-m", "Fix #1468: clean reimplementation")

        # Step 4: push is rejected — origin/branch_name already moved (the
        # rescue commit) past this worktree's start point.
        push = subprocess.run(
            ["git", "push", "-u", "origin", "HEAD"],
            cwd=str(wt), capture_output=True, text=True,
        )
        assert push.returncode != 0, f"expected non-fast-forward rejection: {push.stdout}"

        # Step 5: the worker reaches for `git pull`. This fresh branch has no
        # upstream tracking configured (it was never pushed successfully), so
        # a *bare* `git pull` would itself fail with "no tracking
        # information" rather than merge — the worker's realistic next move
        # is the explicit form, `git pull origin <branch>`, which needs no
        # tracking and is exactly what still respects `pull.rebase`.
        pull = subprocess.run(
            ["git", "pull", "origin", branch_name],
            cwd=str(wt), capture_output=True, text=True,
        )
        assert pull.returncode == 0, f"git pull failed: {pull.stderr}"

        # Acceptance: linear history — no merge (two-parent) commit.
        merges = _git(wt, "log", "--merges", "--oneline")
        assert merges == "", f"merge commit created: {merges!r}"
        parents = _git(wt, "log", "-1", "--format=%P").split()
        assert len(parents) == 1, f"HEAD has {len(parents)} parents (expected 1, i.e. no merge)"

        # Both commits' content survived the rebase.
        subjects = _git(wt, "log", "--format=%s")
        assert "coord-rescue" in subjects
        assert "clean reimplementation" in subjects
        assert (wt / "rescued.txt").exists()
        assert (wt / "clean.py").exists()

        # The rebased branch now pushes cleanly (fast-forward).
        final_push = subprocess.run(
            ["git", "push", "-u", "origin", "HEAD"],
            cwd=str(wt), capture_output=True, text=True,
        )
        assert final_push.returncode == 0, f"final push failed: {final_push.stderr}"
        remote_merges = _git(remote, "log", "--merges", "--oneline", branch_name)
        assert remote_merges == ""
