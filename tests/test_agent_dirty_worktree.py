"""Regression tests for #1394 — a finished assignment's uncommitted work must
never be silently destroyed by worktree teardown.

The reported failure: a headless worker made its edits, launched the test suite
in the *background*, and ended its turn waiting for a completion notification.
``claude -p`` is one-shot, so no notification could ever arrive; the session
exited ``stop=end_turn`` with the edits uncommitted.  ``_cleanup_worktree`` then
ran ``git worktree remove --force`` + ``shutil.rmtree`` with no dirty check, and
the only copy of the code was gone.  The board showed a bare ``advisory``
reading "0 commits pushed" — indistinguishable from a worker that wrote nothing.

Two halves are tested here:

* **Part 1** — ``WORKER_SYSTEM_PROMPT`` forbids backgrounding-and-waiting and
  requires commit + push before the final message.
* **Part 2** — ``_cleanup_worktree`` preserves dirty worktrees (WIP commit for
  work-authoring types, keep-the-directory otherwise) and records a reason that
  distinguishes "wrote nothing" from "wrote something we could not commit".
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

import pytest

import coord.agent as agent_mod
from coord.agent import (
    ADVISORY,
    DONE,
    REFUSED_POLICY,
    REFUSED_PREMISE,
    WORKER_SYSTEM_PROMPT,
    AgentAssignment,
    AgentServer,
    AssignmentSpec,
    _WIP_COMMIT_PREFIX,
    _worktree_dirt,
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()


def _init_local_repo(path: Path) -> Path:
    """Minimal local-only git repo (no remote) with one commit."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "t@t.com")
    _git(path, "config", "user.name", "Test")
    (path / "README").write_text("init\n")
    _git(path, "add", "README")
    _git(path, "commit", "-m", "initial")
    return path


@pytest.fixture
def repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    """Bare remote + clone with `origin` configured, on `main`."""
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(remote)],
        check=True, capture_output=True,
    )
    clone = tmp_path / "clone"
    clone.mkdir()
    _git(clone, "init", "-b", "main")
    _git(clone, "config", "user.email", "t@t.com")
    _git(clone, "config", "user.name", "Test")
    (clone / "README").write_text("v1\n")
    _git(clone, "add", "README")
    _git(clone, "commit", "-m", "initial")
    _git(clone, "remote", "add", "origin", str(remote))
    _git(clone, "push", "-u", "origin", "main")
    return clone, remote


def _make_assignment(
    repo: Path, wt: Path, *, atype: str = "work", status: str = DONE,
    branch: str | None = None, log_path: Path | None = None,
) -> AgentAssignment:
    spec = AssignmentSpec(
        repo_name="api", repo_path=str(repo),
        issue_number=1394, issue_title="dirty", briefing="b",
        branch="main", type=atype,
    )
    return AgentAssignment(
        id=uuid.uuid4().hex[:12],
        spec=spec,
        status=status,
        branch=branch,
        worktree_path=str(wt),
        log_path=str(log_path) if log_path else None,
    )


def _server(tmp_path: Path, repo: Path) -> AgentServer:
    return AgentServer(
        machine_name="t", repos=["api"],
        repo_paths={"api": str(repo)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/true"],
    )


# ── Part 1: the prompt forbids background-and-wait ───────────────────────────

def test_prompt_forbids_backgrounding_and_waiting() -> None:
    """#1394 Part 1: the worker must be told the session is one-shot and that
    background completion notifications never arrive."""
    p = WORKER_SYSTEM_PROMPT
    assert "ONE-SHOT" in p
    assert "run_in_background" in p
    assert "no next turn" in p.lower()
    # The specific failure mode: ending the turn to wait for a background run.
    assert "background" in p.lower()
    assert "notification" in p.lower()


def test_prompt_requires_commit_and_push_before_final_message() -> None:
    """#1394 Part 1: commit+push is required BEFORE the final message, even
    when tests are unfinished or failing."""
    p = WORKER_SYSTEM_PROMPT
    # The prompt is a line-continued literal, so normalise whitespace before
    # asserting on phrases that wrap.
    flat = " ".join(p.split())
    assert "git push origin HEAD` BEFORE your final message" in flat
    assert "even if the build is broken, the tests are failing" in flat
    assert "Uncommitted changes are destroyed when the session ends" in flat


# ── _worktree_dirt ───────────────────────────────────────────────────────────

def test_worktree_dirt_counts_tracked_and_untracked(tmp_path: Path) -> None:
    repo = _init_local_repo(tmp_path / "repo")
    assert _worktree_dirt(repo) == (0, 0)

    (repo / "README").write_text("modified\n")
    assert _worktree_dirt(repo) == (1, 0)

    (repo / "new.py").write_text("x = 1\n")
    assert _worktree_dirt(repo) == (1, 1)

    # An untracked DIRECTORY must count each file, not collapse to one `??`
    # entry — otherwise a 5000-file node_modules reads as a single file.
    pkg = repo / "pkg"
    pkg.mkdir()
    for i in range(4):
        (pkg / f"m{i}.py").write_text("y\n")
    assert _worktree_dirt(repo) == (1, 5)


def test_worktree_dirt_ignores_gitignored_files(tmp_path: Path) -> None:
    """Build output must not read as dirt, or every smoke run leaks a worktree."""
    repo = _init_local_repo(tmp_path / "repo")
    (repo / ".gitignore").write_text("build/\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore build")
    (repo / "build").mkdir()
    (repo / "build" / "out.o").write_text("junk")
    assert _worktree_dirt(repo) == (0, 0)


def test_worktree_dirt_returns_none_when_not_a_repo(tmp_path: Path) -> None:
    """Unknown must be None so callers refuse to delete rather than guess."""
    plain = tmp_path / "notarepo"
    plain.mkdir()
    assert _worktree_dirt(plain) is None


# ── Part 2: dirty worktrees survive teardown ─────────────────────────────────

def test_dirty_work_worktree_is_wip_committed_not_deleted(tmp_path: Path) -> None:
    """#1394 core: a 'work' assignment whose worktree holds uncommitted edits
    gets them committed to its branch instead of force-deleted."""
    repo = _init_local_repo(tmp_path / "repo")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-b", "issue-1394-x", str(wt), "HEAD")

    # The worker's edits: one modification, one brand-new file.
    (wt / "README").write_text("worker edited this\n")
    (wt / "fix.py").write_text("def fixed():\n    return 1\n")

    log = tmp_path / "a.log"
    log.write_text("")
    server = _server(tmp_path, repo)
    a = _make_assignment(repo, wt, branch="issue-1394-x", log_path=log)
    server._assignments[a.id] = a

    server._cleanup_worktree(a)

    # The work survives on the branch ref, which outlives the worktree.
    subjects = _git(repo, "log", "--format=%s", "issue-1394-x")
    assert _WIP_COMMIT_PREFIX in subjects, (
        f"no rescue commit on the branch: {subjects!r}"
    )
    assert _git(repo, "show", "issue-1394-x:fix.py") == "def fixed():\n    return 1"
    assert _git(repo, "show", "issue-1394-x:README") == "worker edited this"

    # And the outcome is recorded, not silent.
    assert a.dirty_worktree_reason is not None
    assert "2 uncommitted file(s)" in a.dirty_worktree_reason
    assert "#1394" in log.read_text()


def test_dirty_advisory_is_not_a_bare_advisory(tmp_path: Path) -> None:
    """#1394 acceptance: the board must distinguish 'wrote nothing' from
    'wrote something we could not commit'."""
    repo = _init_local_repo(tmp_path / "repo")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-b", "issue-1394-adv", str(wt), "HEAD")
    (wt / "fix.py").write_text("real work\n")

    server = _server(tmp_path, repo)
    a = _make_assignment(repo, wt, status=ADVISORY, branch="issue-1394-adv")
    # What the reap writes for a clean-exit / zero-commit worker today.
    a.zero_commit_reason = "worker exited cleanly but pushed 0 commits"
    server._assignments[a.id] = a

    server._cleanup_worktree(a)

    assert a.dirty_worktree_reason is not None
    # The stale, now-false "0 commits" line must not be what the operator sees.
    assert a.zero_commit_reason == a.dirty_worktree_reason
    assert "0 commits" not in a.zero_commit_reason
    assert "UNVERIFIED" in a.zero_commit_reason


def test_dirty_refused_policy_is_recorded_on_policy_refusal_reason(tmp_path: Path) -> None:
    """#2234 fix-1 non-blocking finding: `_record_dirty_worktree` special-cased
    ADVISORY to also rewrite `zero_commit_reason` (the field every surface
    renders for that status) but had no equivalent for REFUSED_POLICY, whose
    equivalent surfaced field is `policy_refusal_reason` — a dirty worktree
    left behind by an otherwise-correct policy refusal must be visible on the
    field `coord status` / the GitHub refused_policy comment actually read."""
    repo = _init_local_repo(tmp_path / "repo")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-b", "issue-1394-rp", str(wt), "HEAD")
    (wt / "fix.py").write_text("real work\n")

    server = _server(tmp_path, repo)
    a = _make_assignment(repo, wt, status=REFUSED_POLICY, branch="issue-1394-rp")
    # What the reap writes for a worker that correctly refused per #2234.
    a.policy_refusal_reason = "CLAUDE.md: only the coordinator writes docs"
    server._assignments[a.id] = a

    server._cleanup_worktree(a)

    assert a.dirty_worktree_reason is not None
    assert a.policy_refusal_reason == a.dirty_worktree_reason
    # The stale, now-superseded refusal-only line must not be what the
    # operator sees once there's an unrecorded dirty worktree to explain.
    assert "only the coordinator writes docs" not in a.policy_refusal_reason


def test_dirty_refused_premise_is_recorded_on_premise_refusal_reason(tmp_path: Path) -> None:
    """#3164: sibling of
    `test_dirty_refused_policy_is_recorded_on_policy_refusal_reason` above
    for the REFUSED_PREMISE verdict — its equivalent surfaced field is
    `premise_refusal_reason`, and a dirty worktree left behind by an
    otherwise-correct premise refusal must be visible there, on the field
    `coord status` / the GitHub refused_premise comment actually read."""
    repo = _init_local_repo(tmp_path / "repo")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-b", "issue-1394-rpr", str(wt), "HEAD")
    (wt / "fix.py").write_text("real work\n")

    server = _server(tmp_path, repo)
    a = _make_assignment(repo, wt, status=REFUSED_PREMISE, branch="issue-1394-rpr")
    # What the reap writes for a worker that correctly refused per #3164.
    a.premise_refusal_reason = "Phase 2 is ~8% complete; the dependency doesn't exist yet"
    server._assignments[a.id] = a

    server._cleanup_worktree(a)

    assert a.dirty_worktree_reason is not None
    assert a.premise_refusal_reason == a.dirty_worktree_reason
    # The stale, now-superseded refusal-only line must not be what the
    # operator sees once there's an unrecorded dirty worktree to explain.
    assert "the dependency doesn't exist yet" not in a.premise_refusal_reason


def test_wip_rescue_is_pushed_when_a_remote_exists(
    tmp_path: Path, repo_with_remote: tuple[Path, Path]
) -> None:
    """The rescue commit reaches origin so it is recoverable off-machine."""
    clone, remote = repo_with_remote
    wt = tmp_path / "wt"
    _git(clone, "worktree", "add", "-b", "issue-1394-push", str(wt), "HEAD")
    (wt / "fix.py").write_text("rescued\n")

    server = _server(tmp_path, clone)
    a = _make_assignment(clone, wt, branch="issue-1394-push")
    server._assignments[a.id] = a

    server._cleanup_worktree(a)

    remote_subjects = _git(remote, "log", "--format=%s", "issue-1394-push")
    assert _WIP_COMMIT_PREFIX in remote_subjects
    assert "and pushed" in (a.dirty_worktree_reason or "")


def test_clean_worktree_is_still_removed(tmp_path: Path) -> None:
    """No regression: a clean worktree is torn down exactly as before, and no
    spurious rescue commit or reason is produced."""
    repo = _init_local_repo(tmp_path / "repo")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-b", "issue-1394-clean", str(wt), "HEAD")

    server = _server(tmp_path, repo)
    a = _make_assignment(repo, wt, branch="issue-1394-clean")
    server._assignments[a.id] = a

    server._cleanup_worktree(a)

    assert not wt.exists(), "clean worktree should have been removed"
    assert a.dirty_worktree_reason is None
    subjects = _git(repo, "log", "--format=%s", "issue-1394-clean")
    assert _WIP_COMMIT_PREFIX not in subjects


def test_review_worktree_with_tracked_edits_is_kept_not_committed(
    tmp_path: Path,
) -> None:
    """A read-only assignment type must never have its dirt committed onto the
    branch under review — but it must not be deleted either."""
    repo = _init_local_repo(tmp_path / "repo")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-b", "issue-1394-rev", str(wt), "HEAD")
    (wt / "README").write_text("reviewer touched this\n")

    server = _server(tmp_path, repo)
    a = _make_assignment(repo, wt, atype="review", branch="issue-1394-rev")
    server._assignments[a.id] = a

    server._cleanup_worktree(a)

    assert wt.exists(), "dirty review worktree was deleted"
    assert (wt / "README").read_text() == "reviewer touched this\n"
    subjects = _git(repo, "log", "--format=%s", "issue-1394-rev")
    assert _WIP_COMMIT_PREFIX not in subjects, "review dirt polluted the branch"
    assert a.dirty_worktree_reason is not None
    assert "kept" in a.dirty_worktree_reason


def test_review_worktree_with_only_scratch_files_is_removed(
    tmp_path: Path,
) -> None:
    """Untracked-only dirt in a read-only worker is build/test scratch —
    deleting it is correct, and keeping it would leak a worktree per smoke run."""
    repo = _init_local_repo(tmp_path / "repo")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-b", "issue-1394-scratch", str(wt), "HEAD")
    (wt / ".pytest_cache").mkdir()
    (wt / ".pytest_cache" / "v").write_text("junk")

    server = _server(tmp_path, repo)
    a = _make_assignment(repo, wt, atype="smoke", branch="issue-1394-scratch")
    server._assignments[a.id] = a

    server._cleanup_worktree(a)

    assert not wt.exists(), "scratch-only smoke worktree should be removed"
    assert a.dirty_worktree_reason is None


def test_huge_dirt_is_kept_rather_than_committed(tmp_path: Path) -> None:
    """An un-gitignored venv/node_modules must not be auto-committed onto the
    branch — but it must not be deleted either, since real edits hide in there."""
    from coord.agent import _WIP_RESCUE_MAX_FILES

    repo = _init_local_repo(tmp_path / "repo")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-b", "issue-1394-huge", str(wt), "HEAD")
    junk = wt / "node_modules"
    junk.mkdir()
    for i in range(_WIP_RESCUE_MAX_FILES + 5):
        (junk / f"f{i}.js").write_text("x")

    server = _server(tmp_path, repo)
    a = _make_assignment(repo, wt, branch="issue-1394-huge")
    server._assignments[a.id] = a

    server._cleanup_worktree(a)

    assert wt.exists(), "oversized dirty worktree was deleted"
    subjects = _git(repo, "log", "--format=%s", "issue-1394-huge")
    assert _WIP_COMMIT_PREFIX not in subjects
    assert "too many to commit safely" in (a.dirty_worktree_reason or "")


def test_unknown_dirtiness_keeps_the_directory(tmp_path: Path) -> None:
    """When git can't be asked, refuse to delete — guessing 'clean' is the bug."""
    repo = _init_local_repo(tmp_path / "repo")
    not_a_worktree = tmp_path / "mystery"
    not_a_worktree.mkdir()
    (not_a_worktree / "precious.py").write_text("work\n")

    server = _server(tmp_path, repo)
    a = _make_assignment(repo, not_a_worktree)
    server._assignments[a.id] = a

    server._cleanup_worktree(a)

    assert not_a_worktree.exists()
    assert (not_a_worktree / "precious.py").read_text() == "work\n"
    assert "could not determine" in (a.dirty_worktree_reason or "")


def test_cancelled_mid_edit_worker_keeps_its_work(tmp_path: Path) -> None:
    """The same protection applies to the cancel/reap path, not just a clean
    exit — a worker killed mid-edit is the other half of #1394."""
    repo = _init_local_repo(tmp_path / "repo")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-b", "issue-1394-cancel", str(wt), "HEAD")
    (wt / "half_done.py").write_text("interrupted\n")

    server = _server(tmp_path, repo)
    spec = AssignmentSpec(
        repo_name="api", repo_path=str(repo),
        issue_number=1394, issue_title="cancel", briefing="b", branch="main",
    )
    a = AgentAssignment(
        id=uuid.uuid4().hex[:12], spec=spec, status="running",
        branch="issue-1394-cancel", worktree_path=str(wt),
    )
    server._assignments[a.id] = a

    server.cancel(a.id)

    assert _git(repo, "show", "issue-1394-cancel:half_done.py") == "interrupted"


# ── #1424: worktree vanishes mid-rescue / concurrent teardown ────────────────

def test_worktree_vanishing_mid_rescue_is_recorded_not_raised(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """#1424: if the worktree directory disappears between the dirt check and
    the `git add` that stages it for the WIP rescue commit, `subprocess.run`
    raises `FileNotFoundError` on the vanished *cwd* — not `_GitError` or
    `subprocess.TimeoutExpired`. Before the fix this escaped
    `_rescue_uncommitted_work`'s except clause entirely and surfaced as an
    unhandled exception on the reap thread (observed in CI as
    `PytestUnhandledThreadExceptionWarning`), losing the work with no
    advisory recorded at all — see the traceback quoted in #1424.

    Reproduces the vanishing-directory race deterministically by having a
    patched `_worktree_dirt` (the first thing `_rescue_uncommitted_work`
    calls) remove the directory as a side effect right after computing the
    real dirt count — simulating a concurrent teardown winning the TOCTOU
    window between the dirt check and the `git add`.
    """
    repo = _init_local_repo(tmp_path / "repo")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-b", "issue-1424-vanish", str(wt), "HEAD")
    (wt / "important_fix.py").write_text("the fix\n")

    real_worktree_dirt = agent_mod._worktree_dirt

    def _dirt_then_vanish(path: Path) -> tuple[int, int] | None:
        result = real_worktree_dirt(path)
        shutil.rmtree(path, ignore_errors=True)
        return result

    monkeypatch.setattr(agent_mod, "_worktree_dirt", _dirt_then_vanish)

    server = _server(tmp_path, repo)
    a = _make_assignment(repo, wt, branch="issue-1424-vanish")
    server._assignments[a.id] = a

    server._cleanup_worktree(a)  # must not raise

    assert a.dirty_worktree_reason is not None
    assert "could not be staged" in a.dirty_worktree_reason
    assert "kept" in a.dirty_worktree_reason


def test_concurrent_cleanup_calls_for_same_assignment_are_serialized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """#1424: `cancel()` (synchronous) and the `_reap` thread can both reach
    `_cleanup_worktree` for the SAME assignment — cancel() kills the worker
    process and tears down immediately, while the reap thread (unblocked by
    that same process exit) runs its own teardown moments later. Without
    serialization the two race through `wt_path.exists()` -> rescue -> `git
    worktree remove`/`rmtree` on the same directory — the TOCTOU behind this
    issue. This drives two threads through `_cleanup_worktree` concurrently
    for one assignment (widening the race window via a patched
    `_worktree_dirt`) and asserts the rescue never runs from both threads at
    once, and that neither call raises.
    """
    repo = _init_local_repo(tmp_path / "repo")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-b", "issue-1424-race", str(wt), "HEAD")
    (wt / "important_fix.py").write_text("the fix\n")

    server = _server(tmp_path, repo)
    a = _make_assignment(repo, wt, branch="issue-1424-race")
    server._assignments[a.id] = a

    real_worktree_dirt = agent_mod._worktree_dirt
    state_lock = threading.Lock()
    active = 0
    max_active = 0

    def _slow_dirt(path: Path) -> tuple[int, int] | None:
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        try:
            # Widen the race window so a second, unserialized call to
            # `_cleanup_worktree` would reliably land inside this one's
            # rescue — reproducing the TOCTOU without the fix.
            time.sleep(0.1)
            if path.exists():
                return real_worktree_dirt(path)
            return None
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(agent_mod, "_worktree_dirt", _slow_dirt)

    errors: list[BaseException] = []

    def _run() -> None:
        try:
            server._cleanup_worktree(a)
        except BaseException as e:  # noqa: BLE001 - captured for assertion
            errors.append(e)

    threads = [threading.Thread(target=_run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not any(t.is_alive() for t in threads), "cleanup thread hung"
    assert not errors, f"_cleanup_worktree raised: {errors!r}"
    assert max_active == 1, (
        "both threads ran the dirty-worktree rescue concurrently — "
        "_cleanup_worktree is not serialized per assignment"
    )

    # Exactly one WIP rescue commit landed — not zero (work lost) and not a
    # duplicate/corrupt state from two threads racing the same git ops.
    subjects = _git(repo, "log", "--format=%s", "issue-1424-race").splitlines()
    wip_commits = [s for s in subjects if _WIP_COMMIT_PREFIX in s]
    assert len(wip_commits) == 1, f"expected exactly 1 WIP commit, got: {subjects!r}"


# ── End-to-end: the exact #1394 scenario through assign() → reap ─────────────

def test_end_to_end_worker_exits_with_uncommitted_work(
    tmp_path: Path, repo_with_remote: tuple[Path, Path]
) -> None:
    """Full path: a worker edits files, never commits, exits 0 — reproducing
    fb5fdc7a1478.  The work must reach origin and the advisory must say so."""
    clone, remote = repo_with_remote
    server = AgentServer(
        machine_name="t", repos=["api"],
        repo_paths={"api": str(clone)},
        state_dir=tmp_path / "state",
        # The worker "makes its fixes" and ends its turn without committing.
        worker_command=lambda spec: [
            "sh", "-c", "printf 'the fix\\n' > important_fix.py",
        ],
    )
    spec = AssignmentSpec(
        repo_name="api", repo_path=str(clone),
        issue_number=1394, issue_title="lost work", briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=30)

    assert final.exit_code == 0
    branch = final.branch
    assert branch, "no branch captured"

    # The code the worker wrote is on the remote, not in a deleted directory.
    remote_subjects = _git(remote, "log", "--format=%s", branch)
    assert _WIP_COMMIT_PREFIX in remote_subjects, (
        f"worker's uncommitted work was lost; remote log: {remote_subjects!r}"
    )
    assert _git(remote, "show", f"{branch}:important_fix.py") == "the fix"

    # And it is NOT reported as a bare "wrote nothing" advisory.
    assert final.dirty_worktree_reason is not None
    if final.status == ADVISORY:
        assert "0 commits" not in (final.zero_commit_reason or "")
