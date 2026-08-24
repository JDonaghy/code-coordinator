"""Tests for the reap-honesty / zero-commit advisory state (#448).

A worker that exits cleanly (exit_code==0) but pushes 0 commits must be
recorded as ADVISORY rather than DONE.  This distinguishes "already
implemented — nothing to do" from "wrote code, tests pass, branch pushed".

A hard FAILED would trigger auto_reassign loops on legitimate no-op reports.
A clean DONE would feed the merge queue with an empty branch.  ADVISORY is
the safe middle ground: the operator decides whether to re-dispatch or close.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from coord.agent import (
    ADVISORY,
    DONE,
    FAILED,
    REFUSED_POLICY,
    AgentServer,
    AssignmentSpec,
    _looks_like_policy_refusal,
)

from .conftest import NOOP_WORKER_ARGV


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    """A working clone whose `origin` is a local bare repo.

    Returns (clone, origin).  The initial commit is pushed so origin/main is
    populated — the commits-ahead check can then distinguish 0 from non-zero.
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
    (clone / "README").write_text("init\n")
    _git(clone, "add", "README")
    _git(clone, "commit", "-m", "initial")
    _git(clone, "push", "-u", "origin", "main")
    return clone, origin


@pytest.fixture
def repo_local_only(tmp_path: Path) -> Path:
    """A local-only repo with no remote — mirrors the test-fixture pattern."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README").write_text("init\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "initial")
    return repo


# Portable replacement for the POSIX-only
# `"git config ... && git config ... && echo <content> > <filename> && git add
# <filename> && git commit -m '<message>'"` shell chain (#2725): writes a
# file and commits it via real `git` subprocess calls rather than shell
# redirection/chaining.
_COMMIT_FILE_SCRIPT = (
    "import subprocess\n"
    "subprocess.run(['git', 'config', 'user.email', 'w@w.com'], check=True)\n"
    "subprocess.run(['git', 'config', 'user.name', 'Worker'], check=True)\n"
    "open({filename!r}, 'w').write({content!r} + chr(10))\n"
    "subprocess.run(['git', 'add', {filename!r}], check=True)\n"
    "subprocess.run(['git', 'commit', '-m', {message!r}], check=True)\n"
)


# ── 0-commit exits → ADVISORY ─────────────────────────────────────────────────


def test_zero_commit_clean_exit_is_advisory_with_remote(
    tmp_path: Path, repo_with_remote: tuple[Path, Path]
) -> None:
    """exit_code==0 + 0 commits ahead of origin/main → ADVISORY, not DONE.

    This is the core regression guard for #448.
    """
    clone, _origin = repo_with_remote
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(clone)},
        state_dir=tmp_path / "state",
        # Worker exits cleanly but makes no git commits.
        worker_command=lambda spec: NOOP_WORKER_ARGV,
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(clone),
        issue_number=1,
        issue_title="already implemented",
        briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    assert final.status == ADVISORY, (
        f"expected ADVISORY for 0-commit clean exit, got {final.status!r}"
    )
    assert final.exit_code == 0, "exit_code must still be 0 for advisory"
    assert final.zero_commit_reason is not None, "reason string must be set"
    assert "0 commits" in final.zero_commit_reason
    server.shutdown()


def test_zero_commit_clean_exit_is_advisory_local_fallback(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """The fallback to <base>..HEAD (no origin) also triggers ADVISORY on 0 commits.

    Local-only repos are common in test fixtures and airgapped machines.  The
    commits-ahead check must work without a remote.
    """
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: NOOP_WORKER_ARGV,
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=2,
        issue_title="noop work",
        briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    assert final.status == ADVISORY, (
        f"expected ADVISORY via local fallback, got {final.status!r}"
    )
    assert final.zero_commit_reason is not None
    server.shutdown()


def test_advisory_reason_appears_in_log(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """The advisory diagnosis is written to the assignment log so operators
    can find it in `coord log <id>` without querying the agent."""
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: NOOP_WORKER_ARGV,
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=3,
        issue_title="noop",
        briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    assert final.status == ADVISORY
    assert final.log_path is not None
    log_text = Path(final.log_path).read_text()
    assert "advisory" in log_text.lower(), (
        f"expected 'advisory' in log, got:\n{log_text}"
    )
    server.shutdown()


def test_advisory_survives_persist_load(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """ADVISORY status and zero_commit_reason round-trip through the agent
    state JSON (persist → load) so a restarted agent still shows the advisory."""
    from coord.agent import AgentAssignment  # noqa: PLC0415

    a = AgentAssignment(
        id="advisory-001",
        spec=AssignmentSpec(
            repo_name="api",
            repo_path="/tmp",
            issue_number=1,
            issue_title="t",
            briefing="b",
        ),
        status=ADVISORY,
        zero_commit_reason="worker exited cleanly but pushed 0 commits",
        exit_code=0,
        finished_at=1234567890.0,
    )
    d = a.to_dict()
    assert d["status"] == ADVISORY
    assert d["zero_commit_reason"] == "worker exited cleanly but pushed 0 commits"

    # Reconstruct (mirrors _load_state logic)
    spec_data = d.pop("spec")
    spec = AssignmentSpec(**spec_data)
    a2 = AgentAssignment(spec=spec, **d)
    assert a2.status == ADVISORY
    assert a2.zero_commit_reason == "worker exited cleanly but pushed 0 commits"


# ── non-zero commits → DONE ───────────────────────────────────────────────────


def test_nonzero_commit_clean_exit_is_done_with_remote(
    tmp_path: Path, repo_with_remote: tuple[Path, Path]
) -> None:
    """A worker that exits cleanly AND pushes ≥1 commit must still be DONE.

    This is the primary regression guard: the advisory path must not affect
    real work that made actual code changes.
    """
    clone, _origin = repo_with_remote

    # Worker script: add a file, commit it, then exit.
    worker_py = _COMMIT_FILE_SCRIPT.format(
        filename="change.txt", content="change", message="real work"
    )
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(clone)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: [sys.executable, "-c", worker_py],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(clone),
        issue_number=4,
        issue_title="real work",
        briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=15)

    assert final.status == DONE, (
        f"expected DONE for non-zero commit exit, got {final.status!r}"
    )
    assert final.exit_code == 0
    assert final.zero_commit_reason is None, (
        "zero_commit_reason must be None when commits exist"
    )
    server.shutdown()


def test_nonzero_commit_clean_exit_is_done_local_fallback(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """Non-zero commits on a local-only repo → DONE via local branch fallback."""
    worker_py = _COMMIT_FILE_SCRIPT.format(
        filename="fix.txt", content="fix", message="local fix"
    )
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: [sys.executable, "-c", worker_py],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=5,
        issue_title="local fix",
        briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=15)

    assert final.status == DONE, (
        f"expected DONE for non-zero local commit, got {final.status!r}"
    )
    assert final.zero_commit_reason is None
    server.shutdown()


# ── non-zero exit always stays FAILED regardless of commits ───────────────────


def test_nonzero_exit_is_failed_regardless_of_commits(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """A worker that exits with a non-zero code is FAILED — never ADVISORY.

    Even if it somehow pushed commits before crashing, exit_code != 0 → FAILED.
    """
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: [sys.executable, "-c", "import sys; sys.exit(1)"],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=6,
        issue_title="failing worker",
        briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    assert final.status == FAILED
    assert final.zero_commit_reason is None
    server.shutdown()


# ── conflict-fix type is exempt from advisory check (#784) ───────────────────


def test_conflict_fix_zero_commits_is_done_not_advisory(
    tmp_path: Path, repo_with_remote: tuple[Path, Path],
) -> None:
    """A conflict-fix worker that exits cleanly with 0 commits ahead of
    origin/<branch> must be marked DONE, NOT ADVISORY.

    A rebase + force-push leaves the worktree 0 commits ahead of the remote
    branch by design (local and remote are in sync after the push).  Without
    this exemption the reap would mismark a successful fix as advisory, block
    the auto re-enqueue, and inflate the retry cap (#784).
    """
    clone, origin = repo_with_remote

    # Simulate the rebase scenario: create a feature branch that is
    # force-pushed so local == remote (0 commits ahead of origin/branch).
    def _git_run(*args: str, cwd: Path) -> None:
        subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)

    _git_run("checkout", "-b", "issue-784-fix", cwd=clone)
    (clone / "fix.txt").write_text("fix\n")
    _git_run("add", "fix.txt", cwd=clone)
    _git_run("config", "user.email", "w@w.com", cwd=clone)
    _git_run("config", "user.name", "Worker", cwd=clone)
    _git_run("commit", "-m", "fix(#784): rebase", cwd=clone)
    _git_run("push", "-u", "origin", "issue-784-fix", cwd=clone)
    # Now local == remote → 0 commits ahead of origin/issue-784-fix.

    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(clone)},
        state_dir=tmp_path / "state",
        # Worker exits cleanly without adding new commits (simulates a
        # conflict-fix that already ran git push --force-with-lease itself).
        worker_command=lambda spec: NOOP_WORKER_ARGV,
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(clone),
        issue_number=784,
        issue_title="[conflict-fix] rebase test",
        briefing="b",
        branch="issue-784-fix",
        type="conflict-fix",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=15)

    assert final.status == DONE, (
        f"conflict-fix with exit_code=0 must be DONE not {final.status!r} — "
        "a rebase+force-push is 0 commits ahead of origin/branch by design (#784)"
    )
    assert final.exit_code == 0
    assert final.zero_commit_reason is None, (
        "conflict-fix exempt from advisory: zero_commit_reason must be None"
    )
    server.shutdown()


# ── ADVISORY is terminal — counted as completed, not active ──────────────────


def test_advisory_counted_as_completed_in_health(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """health() must count ADVISORY assignments as completed, not active."""
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: NOOP_WORKER_ARGV,
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=7,
        issue_title="noop",
        briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)
    assert final.status == ADVISORY

    h = server.health()
    assert h["active"] == 0, "ADVISORY must not count as active"
    assert h["completed"] >= 1, "ADVISORY must count as completed"
    server.shutdown()


# ── `deliverable:analysis` inverts the 0-commit reading (#2188) ──────────────
#
# An issue whose deliverable is a written artifact (a diagnosis, an audit, a
# spike) — not a diff — legitimately ends with 0 commits: that is the SUCCESS
# condition, not the #448 "worker did nothing" anomaly. Labelling the issue
# `deliverable:analysis` tells the reap to record `done`, not `advisory`, for
# exactly that shape. An unlabelled issue with 0 commits must be completely
# unaffected — this is the other half of the acceptance test the issue asks
# for ("a normal (code) issue with zero commits still reports advisory
# exactly as it does today").

_RESULT_LINE = (
    '{"type": "result", "subtype": "success", "is_error": false, '
    '"result": "Diagnosis: 74 percent of blocking review findings were '
    'real defects; zero were false positives or style nits."}'
)


def test_analysis_deliverable_zero_commit_is_done_not_advisory(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """#2188: `deliverable:analysis` + 0 commits + clean exit → DONE, not
    ADVISORY — the core regression guard for the issue's acceptance
    criteria."""
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        # Worker exits cleanly, writes nothing, but DOES emit a stream-json
        # `result` event carrying its final diagnosis — exactly what
        # `claude -p --output-format stream-json` produces.
        worker_command=lambda spec: [sys.executable, "-c", f"print({_RESULT_LINE!r})"],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=2132,
        issue_title="Diagnose the 29% request-changes rate",
        briefing="b",
        branch="main",
        issue_labels=["deliverable:analysis"],
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    assert final.status == DONE, (
        f"expected DONE for a labelled analysis deliverable, got {final.status!r}"
    )
    assert final.exit_code == 0
    assert final.zero_commit_reason is None, (
        "must not carry the #448 advisory reason once relabelled done"
    )
    assert final.analysis_deliverable is True
    assert final.result_text is not None
    assert "74 percent" in final.result_text
    server.shutdown()


def test_analysis_deliverable_label_appears_in_log(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """The analysis-deliverable diagnosis is written to the assignment log,
    mirroring the existing advisory-reason log guarantee."""
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: NOOP_WORKER_ARGV,
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=2133,
        issue_title="noop analysis",
        briefing="b",
        branch="main",
        issue_labels=["deliverable:analysis"],
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    assert final.status == DONE
    assert final.log_path is not None
    log_text = Path(final.log_path).read_text()
    assert "analysis deliverable" in log_text.lower(), (
        f"expected 'analysis deliverable' in log, got:\n{log_text}"
    )
    server.shutdown()


def test_unlabelled_zero_commit_is_still_advisory_exactly_as_before(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """#2188 acceptance: an ORDINARY (unlabelled) issue with 0 commits must
    be completely unaffected by the new label — same ADVISORY status, same
    reason, same `analysis_deliverable=False`/`result_text=None` as every
    zero-commit work assignment before this feature existed."""
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: [sys.executable, "-c", f"print({_RESULT_LINE!r})"],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=2134,
        issue_title="ordinary no-op work",
        briefing="b",
        branch="main",
        # No issue_labels at all — the common case.
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    assert final.status == ADVISORY, (
        f"unlabelled 0-commit issue must stay ADVISORY, got {final.status!r}"
    )
    assert final.zero_commit_reason is not None
    assert "0 commits" in final.zero_commit_reason
    assert final.analysis_deliverable is False
    assert final.result_text is None
    server.shutdown()


def test_analysis_deliverable_survives_persist_load(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """`analysis_deliverable`/`result_text` round-trip through the agent
    state JSON (persist → load), mirroring the existing advisory round-trip
    guarantee."""
    from coord.agent import AgentAssignment  # noqa: PLC0415

    a = AgentAssignment(
        id="analysis-001",
        spec=AssignmentSpec(
            repo_name="api",
            repo_path="/tmp",
            issue_number=1,
            issue_title="t",
            briefing="b",
            issue_labels=["deliverable:analysis"],
        ),
        status=DONE,
        analysis_deliverable=True,
        result_text="the diagnosis",
        exit_code=0,
        finished_at=1234567890.0,
    )
    d = a.to_dict()
    assert d["status"] == DONE
    assert d["analysis_deliverable"] is True
    assert d["result_text"] == "the diagnosis"

    spec_data = d.pop("spec")
    spec = AssignmentSpec(**spec_data)
    a2 = AgentAssignment(spec=spec, **d)
    assert a2.status == DONE
    assert a2.analysis_deliverable is True
    assert a2.result_text == "the diagnosis"
    assert a2.spec.issue_labels == ["deliverable:analysis"]


# ── a policy refusal is its own status, not ADVISORY (#2234) ─────────────────
#
# A worker that exits cleanly, pushes 0 commits, AND whose own final message
# cites a standing repo-rule prohibition (the #2195 shape — CLAUDE.md's
# "only the coordinator writes docs") did the CORRECT thing: retrying it
# reproduces the identical, correct refusal every time, because the rule it
# cited is not going anywhere. Landing that in the same ADVISORY bucket as a
# genuinely stuck worker is what burned two drive attempts and a terminal
# `blocked` rediscovering a rule that could never change (#2234's incident).

_POLICY_REFUSAL_RESULT_LINE = (
    '{"type": "result", "subtype": "success", "is_error": false, '
    '"result": "Confirmed: the rule exists verbatim at CLAUDE.md line 156, '
    'and the issue itself explicitly says this. Only the coordinator '
    'writes docs, so I am stopping rather than editing the doc."}'
)

_STUCK_RESULT_LINE = (
    '{"type": "result", "subtype": "success", "is_error": false, '
    '"result": "STUCK: could not locate the relevant module after two '
    'approaches (grep for the symbol, then a graphify query); ran out of '
    'turns before finding it."}'
)


def test_policy_refusal_zero_commit_is_refused_policy_not_advisory(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """#2234's core regression guard: 0 commits + clean exit + a final
    message citing CLAUDE.md → REFUSED_POLICY, not ADVISORY."""
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: [
            sys.executable, "-c", f"print({_POLICY_REFUSAL_RESULT_LINE!r})"
        ],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=2195,
        issue_title="Split CLAUDE.md by audience",
        briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    assert final.status == REFUSED_POLICY, (
        f"expected REFUSED_POLICY for a CLAUDE.md-citing 0-commit exit, got "
        f"{final.status!r}"
    )
    assert final.exit_code == 0
    assert final.zero_commit_reason is None, (
        "must not ALSO carry the #448 advisory reason — mutually exclusive"
    )
    assert final.policy_refusal_reason is not None
    assert "CLAUDE.md" in final.policy_refusal_reason
    server.shutdown()


def test_policy_refusal_reason_appears_in_log(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """The policy-refusal diagnosis is written to the assignment log,
    mirroring the existing advisory-reason log guarantee."""
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: [
            sys.executable, "-c", f"print({_POLICY_REFUSAL_RESULT_LINE!r})"
        ],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=2196,
        issue_title="doc-only issue",
        briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    assert final.status == REFUSED_POLICY
    assert final.log_path is not None
    log_text = Path(final.log_path).read_text()
    assert "refused_policy" in log_text.lower(), (
        f"expected 'refused_policy' in log, got:\n{log_text}"
    )
    server.shutdown()


def test_stuck_zero_commit_without_policy_markers_is_still_advisory(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """#2234 acceptance: a worker that genuinely got stuck (out of turns, no
    repo-rule citation) must be COMPLETELY unaffected — same ADVISORY status
    as before this feature existed. This is the regression guard against
    `_looks_like_policy_refusal` over-matching."""
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: [
            sys.executable, "-c", f"print({_STUCK_RESULT_LINE!r})"
        ],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=2197,
        issue_title="genuinely hard issue",
        briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    assert final.status == ADVISORY, (
        f"a stuck (non-policy) 0-commit exit must stay ADVISORY, got "
        f"{final.status!r}"
    )
    assert final.zero_commit_reason is not None
    assert final.policy_refusal_reason is None
    server.shutdown()


def test_nonzero_commit_with_claude_md_mention_is_still_done(
    tmp_path: Path, repo_with_remote: tuple[Path, Path],
) -> None:
    """A worker that pushed real commits AND happens to mention CLAUDE.md in
    its final message (e.g. "per CLAUDE.md I ran the build first") must
    never be reclassified — this check only ever fires on the 0-commit
    shape."""
    clone, _origin = repo_with_remote
    worker_py = (
        _COMMIT_FILE_SCRIPT.format(
            filename="change.txt", content="change", message="real work"
        )
        + f"print({_POLICY_REFUSAL_RESULT_LINE!r})\n"
    )
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(clone)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: [sys.executable, "-c", worker_py],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(clone),
        issue_number=2198,
        issue_title="real work that happens to cite CLAUDE.md",
        briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=15)

    assert final.status == DONE, (
        f"expected DONE for a non-zero commit exit regardless of message "
        f"content, got {final.status!r}"
    )
    assert final.policy_refusal_reason is None
    server.shutdown()


def test_policy_refusal_survives_persist_load(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """`policy_refusal_reason` round-trips through the agent state JSON
    (persist → load), mirroring the existing advisory round-trip guarantee."""
    from coord.agent import AgentAssignment  # noqa: PLC0415

    a = AgentAssignment(
        id="refused-policy-001",
        spec=AssignmentSpec(
            repo_name="api",
            repo_path="/tmp",
            issue_number=2195,
            issue_title="t",
            briefing="b",
        ),
        status=REFUSED_POLICY,
        policy_refusal_reason="Confirmed: the rule exists verbatim at CLAUDE.md line 156",
        exit_code=0,
        finished_at=1234567890.0,
    )
    d = a.to_dict()
    assert d["status"] == REFUSED_POLICY
    assert "CLAUDE.md" in d["policy_refusal_reason"]

    spec_data = d.pop("spec")
    spec = AssignmentSpec(**spec_data)
    a2 = AgentAssignment(spec=spec, **d)
    assert a2.status == REFUSED_POLICY
    assert a2.policy_refusal_reason == a.policy_refusal_reason


def test_health_counts_refused_policy_as_completed(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """health() must count REFUSED_POLICY assignments as completed, not
    active — mirrors the existing ADVISORY guarantee."""
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: [
            sys.executable, "-c", f"print({_POLICY_REFUSAL_RESULT_LINE!r})"
        ],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=2199,
        issue_title="doc-only issue",
        briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)
    assert final.status == REFUSED_POLICY

    h = server.health()
    assert h["active"] == 0, "REFUSED_POLICY must not count as active"
    assert h["completed"] >= 1, "REFUSED_POLICY must count as completed"
    server.shutdown()


# ── `_looks_like_policy_refusal` unit tests — the pure detector itself ───────


@pytest.mark.parametrize(
    "text",
    [
        "Confirmed: the rule exists verbatim at CLAUDE.md line 156.",
        "This is coordinator work per files_forbidden in the briefing.",
        "Only the coordinator writes docs — stopping here.",
        "This issue should never have been dispatched to a worker.",
    ],
)
def test_looks_like_policy_refusal_matches_rule_citations(text: str) -> None:
    assert _looks_like_policy_refusal(text) is True


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "STUCK: could not find the relevant module after two approaches.",
        "Implemented the feature and pushed the branch.",
    ],
)
def test_looks_like_policy_refusal_does_not_match_ordinary_text(text) -> None:
    assert _looks_like_policy_refusal(text) is False


# ── a truncated run is FAILED, not ADVISORY (#2316) ──────────────────────────
#
# A worker cut off by its own output-token ceiling mid-turn (opencode's
# `stop_reason: "length"`, claude's `"max_tokens"`) can still exit 0 with 0
# commits pushed — the wrapper never sees an error, it just never got another
# turn. Before this, that shape landed in the same #448 ADVISORY bucket as a
# worker that correctly looked and found nothing to do, and nobody re-drives
# an advisory (space-invaders#1: 13 successful tool calls, then the entire
# 32k-token output budget spent on one reasoning block).

_TRUNCATED_RESULT_LINE = (
    '{"type": "result", "subtype": "success", "is_error": false, '
    '"stop_reason": "%s", "result": ""}'
)


def test_truncated_zero_commit_is_failed_not_advisory(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """The core #2316 regression guard: exit_code==0, 0 commits, and a
    `stop_reason` of `max_tokens` must record FAILED with a
    `truncation_reason`, never ADVISORY."""
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: [
            sys.executable, "-c",
            f"print({_TRUNCATED_RESULT_LINE % 'max_tokens'!r})",
        ],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=2316,
        issue_title="reasoning burned the whole output budget",
        briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    assert final.status == FAILED, (
        f"expected FAILED for a truncated 0-commit exit, got {final.status!r}"
    )
    assert final.exit_code == 0, "exit_code must still be 0 for a truncated run"
    assert final.zero_commit_reason is None, (
        "must not ALSO carry the #448 advisory reason — mutually exclusive"
    )
    assert final.truncation_reason is not None
    assert "cut off at its output limit" in final.truncation_reason
    assert "max_tokens" in final.truncation_reason
    assert final.error == final.truncation_reason, (
        "error must mirror truncation_reason so the existing generic "
        "entry.get('error') fallback carries it to the GitHub comment"
    )
    server.shutdown()


def test_truncation_reason_appears_in_log(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """The truncation diagnosis is written to the assignment log so operators
    can find it in `coord log <id>` without querying the agent."""
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: [
            sys.executable, "-c",
            f"print({_TRUNCATED_RESULT_LINE % 'length'!r})",
        ],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=2317,
        issue_title="truncated",
        briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    assert final.status == FAILED
    assert final.log_path is not None
    log_text = Path(final.log_path).read_text()
    assert "truncated" in log_text.lower(), (
        f"expected 'truncated' in log, got:\n{log_text}"
    )
    assert "length" in log_text
    server.shutdown()


def test_opencode_truncation_names_the_env_var() -> None:
    """#2321: for an opencode worker specifically, the reason must name the
    ceiling's env var so an operator doesn't have to go rediscover that it's
    adjustable — the whole point of the #2316 investigation's own friction.

    Unit-tests `_format_truncation_reason` directly (mirroring the
    `_looks_like_policy_refusal` unit tests below) rather than driving a full
    `AgentServer.assign()` — spawning a real "opencode" worker would require
    registering a resolvable provider (#1796's refuse-not-substitute rule),
    which is orthogonal to what this reason-formatting helper does."""
    from coord.agent import _format_truncation_reason  # noqa: PLC0415

    generic = _format_truncation_reason("max_tokens", None)
    assert "cut off at its output limit" in generic
    assert "OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX" not in generic

    opencode = _format_truncation_reason("length", "opencode")
    assert "cut off at its output limit" in opencode
    assert "OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX" in opencode
    assert "#2321" in opencode
    assert "length" in opencode


def test_nonzero_commit_with_truncated_stop_reason_is_still_done(
    tmp_path: Path, repo_with_remote: tuple[Path, Path],
) -> None:
    """A worker that pushed real commits despite ending on a truncated
    `stop_reason` (e.g. it committed early, then got cut off summarising)
    must never be reclassified — this check only ever fires on the 0-commit
    shape, exactly like the policy-refusal and analysis-deliverable checks."""
    clone, _origin = repo_with_remote
    worker_py = (
        _COMMIT_FILE_SCRIPT.format(
            filename="change.txt", content="change", message="real work"
        )
        + f"print({_TRUNCATED_RESULT_LINE % 'max_tokens'!r})\n"
    )
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(clone)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: [sys.executable, "-c", worker_py],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(clone),
        issue_number=2,
        issue_title="real work that happens to end truncated",
        briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=15)

    assert final.status == DONE, (
        f"expected DONE for a non-zero commit exit regardless of stop_reason, "
        f"got {final.status!r}"
    )
    assert final.truncation_reason is None
    server.shutdown()


def test_truncation_survives_persist_load(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """`truncation_reason` round-trips through the agent state JSON
    (persist → load), mirroring the existing advisory round-trip guarantee."""
    from coord.agent import AgentAssignment  # noqa: PLC0415

    a = AgentAssignment(
        id="truncated-001",
        spec=AssignmentSpec(
            repo_name="api",
            repo_path="/tmp",
            issue_number=1,
            issue_title="t",
            briefing="b",
        ),
        status=FAILED,
        truncation_reason=(
            "the model was cut off at its output limit before writing "
            "anything (stop_reason='max_tokens')"
        ),
        error=(
            "the model was cut off at its output limit before writing "
            "anything (stop_reason='max_tokens')"
        ),
        exit_code=0,
        finished_at=1234567890.0,
    )
    d = a.to_dict()
    assert d["status"] == FAILED
    assert "cut off at its output limit" in d["truncation_reason"]

    spec_data = d.pop("spec")
    spec = AssignmentSpec(**spec_data)
    a2 = AgentAssignment(spec=spec, **d)
    assert a2.status == FAILED
    assert a2.truncation_reason == a.truncation_reason
