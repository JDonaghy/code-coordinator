"""Tests for the reap-time push-failure signal (#1797).

#1797: every ephemeral Azure worker's git credential helper had its
`$GH_TOKEN` expanded (to empty) at image-bake time instead of at
push-invocation time, so `git push` failed with a GitHub auth error on
every single worker, every single time. The failure was recorded to the
worker's log and then dropped on the floor: nothing downstream ever saw
it, so a worker with real local commits that could not reach origin was
recorded exactly like a clean DONE, and a worker with nothing to push in
the first place was recorded exactly like an auth break — both
indistinguishable from each other via `assignment.status` alone.

These tests pin down that a reap-time push failure is its own outcome:
FAILED, with `push_failure_reason` carrying the raw git error, distinct
from the unrelated `zero_commit_reason` ADVISORY path (#448) covered by
test_agent_zero_commit.py.

#3869 extends the same signal to a rebase: a `conflict-fix` leg rewrites
the branch's history, so the reap-time safety-net push can no longer be a
plain `git push` (GitHub always rejects that as non-fast-forward once
history has actually been rewritten). `TestHistoryRewriteLegSafetyNetPush`
below covers the `--force-with-lease` push this file's `_HISTORY_REWRITE_
TYPES` leg now uses instead, and the promotion of a non-fast-forward/
stale-lease rejection to the same FAILED outcome as an auth break.

#3975: a size-triage leg (`coord.decomposition_chat.dispatch_size_triage_
review`) is read-only by its own system prompt and its branch is cut fresh
from the default branch's tip, so this reap-time safety-net push has
nothing of the leg's own to push in the first place — and pushing the
empty branch anyway hands #3376's `any_matching_branch_merged` fallback a
zero-commit `issue-N-size-triage-*` branch to mis-read as "already
merged", refusing the Work dispatch the triage verdict just unblocked
(quadraui#2304). `test_size_triage_leg_reap_skips_the_push` below pins
down that this reap-time push is never even attempted for that leg shape.
"""

from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path

import pytest

from coord.agent import (
    ADVISORY,
    DONE,
    FAILED,
    AgentServer,
    AssignmentSpec,
    _is_auth_push_failure,
    _is_non_fast_forward_push_failure,
    _remote_already_has_head,
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()


def _write_always_failing_receive_pack(tmp_path: Path) -> Path:
    """A `receive-pack` replacement that unconditionally fails every push
    with an auth-shaped error, regardless of whether the push has anything
    new to send (#2356 review).

    A `pre-receive` hook is the WRONG tool for simulating "reap's own push
    fails while the remote already has HEAD": git's client-side ref
    negotiation short-circuits to `Everything up-to-date` (exit 0) whenever
    local HEAD already matches what the remote advertises, and that
    short-circuit happens BEFORE a pack is ever sent — so a `pre-receive`
    hook, which only runs after `receive-pack` unpacks an incoming pack,
    never gets invoked at all in that case. Verified independently: a
    `pre-receive`-based rejecting remote lets a same-SHA `git push -u
    origin HEAD` succeed silently, hook untouched.

    Overriding `remote.<name>.receivepack` instead replaces the
    `receive-pack` PROGRAM itself, which git invokes unconditionally at the
    start of every `git push` (for ref advertisement) — before any
    comparison against what the client already has. It fires every time,
    including when there is nothing new to send, which is exactly the
    shape this scenario needs. Deliberately leaves `uploadpack` untouched,
    so `git fetch` (what `_remote_already_has_head` uses) keeps working
    normally.
    """
    script = tmp_path / "fail_receive_pack.sh"
    script.write_text(
        "#!/bin/sh\n"
        "echo 'remote: Invalid username or token.' >&2\n"
        "echo 'remote: Password authentication is not supported for "
        "Git operations.' >&2\n"
        "exit 1\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


class TestIsAuthPushFailure:
    """Direct unit tests for `_is_auth_push_failure` (#1797 review nit): the
    three FAILED-vs-non-fatal tests above only exercise it indirectly
    through a full `AgentServer` round-trip. These pin the marker-matching
    behaviour precisely — case sensitivity, partial matches, and the
    false-positive risk of an unrelated network blip or "no origin"
    message accidentally containing one of the markers.
    """

    @pytest.mark.parametrize(
        "message",
        [
            "remote: Invalid username or token.",
            "remote: Password authentication is not supported for Git "
            "operations.",
            "fatal: Authentication failed for 'https://github.com/x/y.git/'",
            "fatal: could not read Username for 'https://github.com': "
            "terminal prompts disabled",
            "fatal: could not read Password for 'https://github.com': "
            "terminal prompts disabled",
            "git@github.com: Permission denied (publickey).",
        ],
    )
    def test_matches_known_auth_failure_shapes(self, message: str) -> None:
        assert _is_auth_push_failure(message) is True

    def test_matches_case_insensitively(self) -> None:
        assert _is_auth_push_failure(
            "REMOTE: INVALID USERNAME OR TOKEN."
        ) is True

    def test_matches_as_substring_within_a_larger_message(self) -> None:
        # `_GitError`/`TimeoutExpired` messages wrap the raw git stderr with
        # extra context (command, exit code); the marker just needs to
        # appear somewhere in the combined text, not be the whole message.
        assert _is_auth_push_failure(
            "git push failed (exit 128):\n"
            "remote: Invalid username or token.\n"
            "fatal: unable to access 'https://github.com/x/y.git/'"
        ) is True

    @pytest.mark.parametrize(
        "message",
        [
            "fatal: 'origin' does not appear to be a git repository",
            "fatal: 'origin' does not appear to be a git repository\n"
            "fatal: Could not read from remote repository.",
            "ssh: connect to host github.com port 22: Connection timed out",
            "fatal: unable to access 'https://github.com/x/y.git/': "
            "Could not resolve host: github.com",
            "error: failed to push some refs (non-fast-forward)",
            "",
        ],
    )
    def test_does_not_match_unrelated_push_failures(self, message: str) -> None:
        assert _is_auth_push_failure(message) is False


class TestIsNonFastForwardPushFailure:
    """Direct unit tests for `_is_non_fast_forward_push_failure` (#3869) —
    mirrors `TestIsAuthPushFailure`'s role for its sibling marker-matcher.
    """

    @pytest.mark.parametrize(
        "message",
        [
            "! [rejected]        HEAD -> feature (non-fast-forward)\n"
            "error: failed to push some refs to 'origin'",
            "! [rejected]        HEAD -> feature (stale info)\n"
            "error: failed to push some refs to 'origin'",
            "! [rejected]        HEAD -> feature (fetch first)\n"
            "error: failed to push some refs to 'origin'",
        ],
    )
    def test_matches_known_non_fast_forward_shapes(self, message: str) -> None:
        assert _is_non_fast_forward_push_failure(message) is True

    def test_matches_case_insensitively(self) -> None:
        assert _is_non_fast_forward_push_failure(
            "! [REJECTED] HEAD -> feature (STALE INFO)"
        ) is True

    @pytest.mark.parametrize(
        "message",
        [
            "fatal: 'origin' does not appear to be a git repository",
            "remote: Invalid username or token.",
            "ssh: connect to host github.com port 22: Connection timed out",
            "",
        ],
    )
    def test_does_not_match_unrelated_push_failures(self, message: str) -> None:
        assert _is_non_fast_forward_push_failure(message) is False


def _init_repo(d: Path) -> None:
    d.mkdir()
    _git(d, "init", "-b", "main")
    _git(d, "config", "user.email", "t@t.com")
    _git(d, "config", "user.name", "Test")


def _commit_file(d: Path, name: str, contents: str, message: str) -> None:
    (d / name).write_text(contents)
    _git(d, "add", name)
    _git(d, "commit", "-m", message)


class TestRemoteAlreadyHasHead:
    """Direct unit tests for `_remote_already_has_head` (#2356 review nit):
    the full-round-trip tests only exercise it indirectly through an entire
    `AgentServer`/`_reap` cycle, which proves the wiring but not the
    ancestor-check logic itself in isolation. These pin down the four cases
    the function's docstring promises: caught up, genuinely behind, branch
    missing on the remote, and the remote unreachable outright — mirroring
    `TestIsAuthPushFailure`'s role for `_is_auth_push_failure`.
    """

    def test_true_when_remote_already_has_head_as_ancestor(
        self, tmp_path: Path
    ) -> None:
        origin = tmp_path / "origin.git"
        origin.mkdir()
        _git(origin, "init", "--bare", "-b", "main")

        clone = tmp_path / "clone"
        _init_repo(clone)
        _git(clone, "remote", "add", "origin", str(origin))
        _commit_file(clone, "f", "x\n", "c1")
        _git(clone, "push", "-u", "origin", "main")

        assert _remote_already_has_head(clone, "origin", "main") is True

    def test_false_when_local_head_is_ahead_of_remote(
        self, tmp_path: Path
    ) -> None:
        """The genuine #1797 shape: local HEAD moved on past whatever the
        remote has, so the remote does NOT already have this content —
        must return False so the real failure signal is never suppressed.
        """
        origin = tmp_path / "origin.git"
        origin.mkdir()
        _git(origin, "init", "--bare", "-b", "main")

        clone = tmp_path / "clone"
        _init_repo(clone)
        _git(clone, "remote", "add", "origin", str(origin))
        _commit_file(clone, "f", "x\n", "c1")
        _git(clone, "push", "-u", "origin", "main")

        # A second local commit that never reaches origin.
        _commit_file(clone, "f", "y\n", "c2 (never reaches origin)")

        assert _remote_already_has_head(clone, "origin", "main") is False

    def test_false_when_remote_branch_does_not_exist(
        self, tmp_path: Path
    ) -> None:
        origin = tmp_path / "origin.git"
        origin.mkdir()
        _git(origin, "init", "--bare", "-b", "main")

        clone = tmp_path / "clone"
        _init_repo(clone)
        _git(clone, "remote", "add", "origin", str(origin))
        _commit_file(clone, "f", "x\n", "c1")
        # Never pushed anywhere — "main" doesn't exist on origin at all.

        assert _remote_already_has_head(clone, "origin", "main") is False

    def test_false_when_remote_is_unreachable(self, tmp_path: Path) -> None:
        clone = tmp_path / "clone"
        _init_repo(clone)
        _git(clone, "remote", "add", "origin", str(tmp_path / "does-not-exist"))
        _commit_file(clone, "f", "x\n", "c1")

        assert _remote_already_has_head(clone, "origin", "main") is False


@pytest.fixture
def repo_with_rejecting_remote(tmp_path: Path) -> tuple[Path, Path]:
    """A working clone whose `origin` rejects every push with a git
    auth-style error, via a `pre-receive` hook — the same shape of failure
    #1797 hit in production (`remote: Invalid username or token. Password
    authentication is not supported for Git operations.`), reproduced
    deterministically with no network and no real credentials involved.

    #2684: POSIX-only. This fixture toggles the hook on and off with
    ``chmod``, and the worker commands driven by it are ``#!/bin/sh``
    scripts run through ``/bin/sh -c`` — none of which is portable. Worse,
    NTFS has no POSIX executable bit, so the chmod-based "briefly disable
    the hook to land the initial commit" trick is a no-op on Windows: the
    hook stays live throughout, and even the *setup* push above fails,
    which is exactly the "ERROR at setup" shape from the #2684 Windows
    job. No Windows port yet.
    """
    if sys.platform == "win32":
        pytest.skip(
            "POSIX-only: pre-receive hook + chmod-based enable/disable "
            "has no NTFS equivalent (#2684)"
        )
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "-b", "main")

    hook = origin / "hooks" / "pre-receive"
    hook.write_text(
        "#!/bin/sh\n"
        "echo 'remote: Invalid username or token.' >&2\n"
        "echo 'remote: Password authentication is not supported for "
        "Git operations.' >&2\n"
        "exit 1\n"
    )
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC)

    clone = tmp_path / "clone"
    clone.mkdir()
    _git(clone, "init", "-b", "main")
    _git(clone, "config", "user.email", "t@t.com")
    _git(clone, "config", "user.name", "Test")
    _git(clone, "remote", "add", "origin", str(origin))
    (clone / "README").write_text("init\n")
    _git(clone, "add", "README")
    _git(clone, "commit", "-m", "initial")

    # Temporarily disable the hook to land the initial commit so origin/main
    # exists for the commits-ahead check, then re-enable it for the real test.
    hook.chmod(hook.stat().st_mode & ~stat.S_IEXEC)
    _git(clone, "push", "-u", "origin", "main")
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC)

    return clone, origin


def test_push_failure_with_real_commits_is_failed_not_done(
    tmp_path: Path, repo_with_rejecting_remote: tuple[Path, Path]
) -> None:
    """A worker that commits real work but can't push (auth broken) must be
    FAILED, not DONE — a silent DONE would mean the work is recorded as
    landed when it never left the worktree."""
    clone, _origin = repo_with_rejecting_remote

    worker_sh = (
        "git config user.email w@w.com && "
        "git config user.name Worker && "
        "echo change > change.txt && "
        "git add change.txt && "
        "git commit -m 'real work'"
    )
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(clone)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/sh", "-c", worker_sh],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(clone),
        issue_number=1,
        issue_title="real work, broken credential",
        briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=15)

    assert final.status == FAILED, (
        f"expected FAILED when the reap-time push fails, got {final.status!r}"
    )
    assert final.exit_code == 0, "the worker itself exited cleanly"
    assert final.push_failure_reason is not None, "reason string must be set"
    assert "Invalid username or token" in final.push_failure_reason
    # Distinct signal from #448's zero-commit advisory — must stay unset.
    assert final.zero_commit_reason is None, (
        "push failure must not be conflated with the zero-commit advisory path"
    )
    server.shutdown()


def test_push_failure_with_zero_commits_is_failed_not_advisory(
    tmp_path: Path, repo_with_rejecting_remote: tuple[Path, Path]
) -> None:
    """The #1797 evidence case: a worker with 0 local commits AND a broken
    push. Before this fix, the zero-commit ADVISORY ("worker exited cleanly
    but pushed 0 commits") masked the auth break entirely. The push failure
    must win and be recorded as FAILED with a push_failure_reason — never
    silently downgraded to the generic "nothing to do" advisory."""
    clone, _origin = repo_with_rejecting_remote
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(clone)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/true"],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(clone),
        issue_number=2,
        issue_title="already implemented, broken credential",
        briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=15)

    assert final.status == FAILED, (
        "push failure must take priority over the zero-commit advisory, got "
        f"{final.status!r}"
    )
    assert final.status != ADVISORY
    assert final.push_failure_reason is not None
    assert "Invalid username or token" in final.push_failure_reason
    server.shutdown()


def test_push_failure_when_already_on_origin_via_other_route_is_not_failed(
    tmp_path: Path, repo_with_rejecting_remote: tuple[Path, Path]
) -> None:
    """#2356: a worker that hits the same broken-write-credential wall as
    #1797 may work around it by landing the same commit on origin via some
    other route (the #2269 incident: pushing over an explicit SSH URL when
    the worktree's configured HTTPS remote had no usable credentials in a
    non-interactive shell). Reap's own belt-and-suspenders push against
    `origin` still fails with the exact same auth-shaped error in that case
    — but the content is already on the remote branch.

    The worker lands its own commit on `origin` first (hook briefly
    lifted — standing in for "reached origin via another path"), THEN we
    install `_write_always_failing_receive_pack`'s override so reap's own
    subsequent push — which has nothing new to send, since the worker
    already pushed this exact SHA — still fails with an auth-shaped error.
    (A `pre-receive` hook cannot do this: see that helper's docstring for
    why git's own "nothing to push" short-circuit bypasses it here.) The
    assignment must land on DONE, not FAILED: the work is exactly where it
    needs to be, however it got there.
    """
    clone, origin = repo_with_rejecting_remote
    hook = origin / "hooks" / "pre-receive"
    fail_receive_pack = _write_always_failing_receive_pack(tmp_path)

    worker_sh = (
        "git config user.email w@w.com && "
        "git config user.name Worker && "
        "echo change > change.txt && "
        "git add change.txt && "
        "git commit -m 'real work' && "
        f"chmod -x {hook} && "
        "git push origin HEAD && "
        f"chmod +x {hook} && "
        f"git config remote.origin.receivepack {fail_receive_pack}"
    )
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(clone)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/sh", "-c", worker_sh],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(clone),
        issue_number=4,
        issue_title="already pushed elsewhere, origin push still broken",
        briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=15)

    assert final.status == DONE, (
        "origin already had HEAD (however it got there) — reap's own push "
        f"failing on top of that must not be FAILED, got {final.status!r}"
    )
    assert final.push_failure_reason is None, (
        "the auth-shaped failure must be suppressed once the fetch-based "
        "check confirms origin already has HEAD as an ancestor"
    )
    server.shutdown()


def test_successful_push_leaves_push_failure_reason_none(
    tmp_path: Path, repo_with_rejecting_remote: tuple[Path, Path]
) -> None:
    """Sanity/regression check: a healthy push (hook disabled) must NOT set
    push_failure_reason, and status must be the ordinary DONE."""
    clone, origin = repo_with_rejecting_remote
    hook = origin / "hooks" / "pre-receive"
    hook.chmod(hook.stat().st_mode & ~stat.S_IEXEC)  # disable the rejection

    worker_sh = (
        "git config user.email w@w.com && "
        "git config user.name Worker && "
        "echo change > change.txt && "
        "git add change.txt && "
        "git commit -m 'real work'"
    )
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(clone)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/sh", "-c", worker_sh],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(clone),
        issue_number=3,
        issue_title="real work, healthy credential",
        briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=15)

    assert final.status == DONE
    assert final.push_failure_reason is None
    server.shutdown()


def test_size_triage_leg_reap_skips_the_push(
    tmp_path: Path, repo_with_rejecting_remote: tuple[Path, Path]
) -> None:
    """#3975: a size-triage leg's reap must never attempt the
    belt-and-suspenders push at all — not even one that would otherwise
    fail loudly. The rejecting remote proves the point: if reap attempted
    the push, this leg would come back FAILED with an auth-shaped
    `push_failure_reason`; since the leg is read-only, it must instead come
    back DONE with no push attempted and no branch created on origin."""
    clone, origin = repo_with_rejecting_remote
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(clone)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/sh", "-c", "true"],  # read-only, no commits
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(clone),
        issue_number=2304,
        issue_title="[size-triage] shared app layer 2a widgetid",
        briefing="b",
        branch="main",
        type="review",
        review_target="size-triage-2304",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=15)

    assert final.status == DONE
    assert final.push_failure_reason is None
    log_text = Path(final.log_path).read_text()
    assert "# reap: push starting" not in log_text
    assert "# reap: push completed" not in log_text
    # The branch the agent would have minted (`issue-2304-size-triage-...`)
    # must never reach origin — the whole bug was that empty branch landing
    # there and being misread as "this issue's work already merged".
    refs = subprocess.run(
        ["git", "ls-remote", "--heads", str(origin)],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "size-triage" not in refs
    server.shutdown()


@pytest.fixture
def repo_with_existing_branch(tmp_path: Path) -> tuple[Path, Path]:
    """A clone with `origin` configured and a `feature` branch already
    pushed there — the shape `conflict-fix` operates on (rebasing an
    EXISTING PR branch), unlike every fixture above, where the worker
    branches fresh off `origin/main` inside `_setup_worktree`."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "-b", "main")

    clone = tmp_path / "clone"
    _init_repo(clone)
    _git(clone, "remote", "add", "origin", str(origin))
    _commit_file(clone, "README", "init\n", "initial")
    _git(clone, "push", "-u", "origin", "main")

    _git(clone, "checkout", "-b", "feature")
    _commit_file(clone, "feature.txt", "feat\n", "feat1")
    _git(clone, "push", "-u", "origin", "feature")
    _git(clone, "checkout", "main")

    return clone, origin


class TestHistoryRewriteLegSafetyNetPush:
    """#3869: `conflict-fix` (the one `_HISTORY_REWRITE_TYPES` member
    today) rewrites the history already on its branch — a plain `git push`
    is then always rejected non-fast-forward, because origin still has the
    PRE-rebase history. These pin down that the reap-time safety net
    leases against the SHA origin had when the leg started instead, and
    that a genuine conflicting push during the leg is promoted to FAILED
    rather than silently recorded as success.
    """

    def test_worker_rewrites_history_without_pushing_safety_net_lands_it(
        self, tmp_path: Path, repo_with_existing_branch: tuple[Path, Path]
    ) -> None:
        """The exact #3869 evidence shape: the worker rebases the branch
        (here simulated with an amend, which equally invalidates the old
        SHA) and reports success without pushing itself. The reap-time
        safety net must still land it on origin — a plain push would be
        rejected non-fast-forward the instant it tries."""
        clone, origin = repo_with_existing_branch
        worker_sh = (
            "git config user.email w@w.com && "
            "git config user.name Worker && "
            "git commit --amend --allow-empty -m 'rebased onto main'"
        )
        server = AgentServer(
            machine_name="t",
            repos=["api"],
            repo_paths={"api": str(clone)},
            state_dir=tmp_path / "state",
            worker_command=lambda spec: ["/bin/sh", "-c", worker_sh],
        )
        spec = AssignmentSpec(
            repo_name="api",
            repo_path=str(clone),
            issue_number=10,
            issue_title="conflict fix",
            briefing="b",
            branch="main",
            target_branch="feature",
            type="conflict-fix",
        )
        a = server.assign(spec)
        final = server.wait_for(a.id, timeout=15)

        assert final.status == DONE, (
            "expected DONE once the safety-net push lands the rebase, got "
            f"{final.status!r} ({final.push_failure_reason})"
        )
        assert final.push_failure_reason is None

        _git(clone, "fetch", "origin", "feature")
        subject = _git(clone, "log", "-1", "--format=%s", "FETCH_HEAD")
        assert subject == "rebased onto main", (
            "origin/feature must have moved to the worker's rebased commit "
            "via the reap's own lease-protected push"
        )
        server.shutdown()

    def test_concurrent_push_during_leg_is_failed_not_silent_success(
        self, tmp_path: Path, repo_with_existing_branch: tuple[Path, Path]
    ) -> None:
        """A genuinely concurrent push to the same branch between
        `_setup_worktree` capturing the lease SHA and the reap's own push
        must still be refused — `--force-with-lease` exists precisely for
        this — and that refusal must be recorded as FAILED with
        `push_failure_reason`, never as a silent DONE."""
        clone, origin = repo_with_existing_branch
        concurrent_clone = tmp_path / "concurrent"
        worker_sh = (
            "git config user.email w@w.com && "
            "git config user.name Worker && "
            "git commit --amend --allow-empty -m 'rebased onto main' && "
            f"git clone --branch feature {origin} {concurrent_clone} && "
            f"cd {concurrent_clone} && "
            "git config user.email c@c.com && "
            "git config user.name Concurrent && "
            "echo z > conflict.txt && "
            "git add conflict.txt && "
            "git commit -m 'concurrent unrelated change' && "
            "git push origin feature"
        )
        server = AgentServer(
            machine_name="t",
            repos=["api"],
            repo_paths={"api": str(clone)},
            state_dir=tmp_path / "state",
            worker_command=lambda spec: ["/bin/sh", "-c", worker_sh],
        )
        spec = AssignmentSpec(
            repo_name="api",
            repo_path=str(clone),
            issue_number=11,
            issue_title="conflict fix racing a concurrent push",
            briefing="b",
            branch="main",
            target_branch="feature",
            type="conflict-fix",
        )
        a = server.assign(spec)
        final = server.wait_for(a.id, timeout=15)

        assert final.status == FAILED, (
            "a lease mismatch from a genuinely concurrent push must not be "
            f"recorded as success, got {final.status!r}"
        )
        assert final.push_failure_reason is not None
        assert _is_non_fast_forward_push_failure(final.push_failure_reason)

        _git(clone, "fetch", "origin", "feature")
        subject = _git(clone, "log", "-1", "--format=%s", "FETCH_HEAD")
        assert subject == "concurrent unrelated change", (
            "the concurrent push must win — the reap's own push must have "
            "been refused, not silently overwritten it"
        )
        server.shutdown()

    def test_non_history_rewrite_leg_keeps_old_non_fatal_behaviour(
        self, tmp_path: Path, repo_with_existing_branch: tuple[Path, Path]
    ) -> None:
        """Regression guard: a leg type NOT in `_HISTORY_REWRITE_TYPES`
        (the default `type="work"`) must behave exactly as before #3869 —
        a non-fast-forward rejection on the plain safety-net push stays
        logged and non-fatal, never promoted to FAILED. Only `conflict-fix`
        (and any future history-rewrite type) gets the new lease/promotion
        behaviour."""
        clone, origin = repo_with_existing_branch
        worker_sh = (
            "git config user.email w@w.com && "
            "git config user.name Worker && "
            "git commit --amend --allow-empty -m 'amended, never pushed'"
        )
        server = AgentServer(
            machine_name="t",
            repos=["api"],
            repo_paths={"api": str(clone)},
            state_dir=tmp_path / "state",
            worker_command=lambda spec: ["/bin/sh", "-c", worker_sh],
        )
        spec = AssignmentSpec(
            repo_name="api",
            repo_path=str(clone),
            issue_number=12,
            issue_title="ordinary continuation work",
            briefing="b",
            branch="main",
            target_branch="feature",
        )
        a = server.assign(spec)
        final = server.wait_for(a.id, timeout=15)

        assert final.status == DONE, (
            "a non-history-rewrite leg's plain-push non-fast-forward "
            f"rejection must stay non-fatal, got {final.status!r}"
        )
        assert final.push_failure_reason is None

        _git(clone, "fetch", "origin", "feature")
        subject = _git(clone, "log", "-1", "--format=%s", "FETCH_HEAD")
        assert subject == "feat1", (
            "the plain push must have failed as before — origin/feature "
            "unchanged"
        )
        server.shutdown()
