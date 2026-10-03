"""Tests for usage-limit-kill detection at reap time (#1461).

A worker (claude -p) that hits the account's Max/Pro *session* usage limit
mid-flight prints a terminal line like:

    "You've hit your session limit · resets 8:30pm (America/Chicago)"

and exits, with no structured event marking what happened. Before this fix,
`_reap` recorded a bare FAILED/ADVISORY with `usage_limit_reason=None` —
indistinguishable from a real defect. This regression-guards the reap-time
detection wired in `AgentServer._reap` (coord/agent.py).
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
    AgentServer,
    AssignmentSpec,
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo_local_only(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README").write_text("init\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "initial")
    return repo


_KILL_MESSAGE = "You’ve hit your session limit · resets 8:30pm (America/Chicago)"


# Portable replacements for the POSIX-only `["/bin/sh", "-c", script]` fake
# worker shape (#2725/#2843): `script` below is inline PYTHON, run via
# `sys.executable -c`, never a shell — no `/bin/sh` dependency, which is
# absent on Windows and was failing every test in this file at spawn time
# regardless of the `_maybe_bash_wrap` default (#2843's OTHER root cause,
# independent of the bash-wrap-default fix in `coord/agent.py`).
def _kill_message_argv(*, exit_code: int = 0) -> list[str]:
    """`printf '%s\\n' <msg>[; exit N]`, as a portable argv."""
    script = f"print({_KILL_MESSAGE!r})\n"
    if exit_code:
        script += f"import sys; sys.exit({exit_code})\n"
    return [sys.executable, "-c", script]


_PARTIAL_COMMIT_THEN_KILL_SCRIPT = (
    "import subprocess\n"
    "open('partial.txt', 'w').write('partial\\n')\n"
    "subprocess.run(['git', 'add', 'partial.txt'], check=True)\n"
    "subprocess.run(['git', '-c', 'user.email=t@t.com', '-c', 'user.name=T', "
    "'commit', '-q', '-m', 'partial'], check=True)\n"
    f"print({_KILL_MESSAGE!r})\n"
)


def test_usage_limit_kill_detected_on_nonzero_exit(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """The #1454 shape from the issue: worker dies non-zero, transcript ends
    with the kill message, no terminating result event."""
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: _kill_message_argv(exit_code=1),
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=1454,
        issue_title="killed mid-flight",
        briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    assert final.status == FAILED
    assert final.usage_limit_reason is not None
    assert final.usage_limit_reason == "usage limit — resets 8:30pm (America/Chicago)"
    server.shutdown()


def test_usage_limit_kill_detected_on_clean_exit_zero_commits(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """The #1456 shape from the issue: the CLI ends the turn gracefully (exit
    0) before committing anything.

    #1534 changed the recorded status here from ADVISORY to FAILED. ADVISORY
    means "a human needs to look at this"; a usage-limit kill is instead the
    one terminal state known safe to re-dispatch unchanged once the window
    resets, which is what FAILED already means to ``coord/drive.py``. The
    coordinator was ALREADY normalising this row to ``failed`` on arrival
    (``coord.reconcile._record_usage_limit_reason``), so recording FAILED at
    the source just removes a disagreement between the two layers. Either way
    the usage-limit reason must be carried, not a bare '0 commits pushed'.
    """
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: _kill_message_argv(),
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=1456,
        issue_title="killed mid-flight, clean exit",
        briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    assert final.status == FAILED
    assert final.usage_limit_reason is not None
    assert final.usage_limit_reason == "usage limit — resets 8:30pm (America/Chicago)"
    server.shutdown()


# ── #1534: a usage-limit kill is NEVER `done` ───────────────────────────────


@pytest.mark.parametrize("spec_type", ["work", "test-author", "mock-author"])
def test_usage_limit_kill_never_recorded_done_even_with_commits(
    tmp_path: Path, repo_local_only: Path, spec_type: str
) -> None:
    """#1534, the core regression.

    The observed incident (`b2d6b331616e`, a `test-author`) was recorded
    ``status=done`` with ``exit_code=null`` and ``failure_reason=null`` — the
    board, the TUI and every downstream gate read it as a clean success.

    The zero-commit downgrade alone is NOT sufficient to catch this class: a
    worker can commit *something* and then be killed part-way through, at
    which point ``_zero_commit_reason`` is None and the pre-#1534 code
    recorded DONE.  So the kill itself — not just its usual symptom — has to
    veto the DONE transition.  Here the worker commits a file AND exits 0,
    leaving the kill message as the only signal.
    """
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: [
            sys.executable, "-c", _PARTIAL_COMMIT_THEN_KILL_SCRIPT,
        ],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=1534,
        issue_title="killed after a partial commit",
        briefing="b",
        branch="main",
        type=spec_type,
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=20)

    assert final.status == FAILED, (
        f"a usage-limit kill on type={spec_type!r} must never be recorded "
        f"`done` — got {final.status!r}"
    )
    assert final.usage_limit_reason == "usage limit — resets 8:30pm (America/Chicago)"
    # Guard the guard: prove the commit really landed, so this test is
    # exercising the "kill vetoes DONE" path and not passing for free via the
    # zero-commit downgrade (which logs this line when it fires).
    assert "0 commits ahead" not in Path(final.log_path).read_text()
    server.shutdown()


@pytest.mark.parametrize("spec_type", ["test-author", "mock-author"])
def test_zero_commit_clean_exit_is_advisory_for_all_work_like_types(
    tmp_path: Path, repo_local_only: Path, spec_type: str
) -> None:
    """#1534: the cheap backstop, independent of *why* nothing was produced.

    Before #1534 the #448 zero-commit downgrade was gated on
    ``_ADVISORY_TYPES = ("work",)``, so a ``test-author``/``mock-author`` that
    exited cleanly having pushed nothing landed on the board as DONE. Both
    types must move their branch for the assignment to mean anything, so a
    clean exit with 0 commits is a contradiction and must be refused —
    exactly as it already was for ``work``.

    No kill message here: this must hold for ANY cause of an empty branch.
    """
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: [
            sys.executable, "-c", "print('already implemented')",
        ],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=1124,
        issue_title="ms-38 slice",
        briefing="b",
        branch="main",
        type=spec_type,
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=20)

    assert final.status == ADVISORY, (
        f"a clean exit with 0 commits on type={spec_type!r} must not be `done`"
    )
    assert final.zero_commit_reason is not None
    assert final.usage_limit_reason is None
    server.shutdown()


def test_review_type_zero_commits_still_done(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """The other side of the #1534 widening: review/smoke workers commit
    nothing by design, so widening the zero-commit gate to the work-like set
    must NOT start false-flagging them."""
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: [sys.executable, "-c", "print('LGTM')"],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=1534,
        issue_title="review of something",
        briefing="b",
        branch="main",
        type="review",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=20)

    assert final.status == DONE
    assert final.zero_commit_reason is None
    server.shutdown()


def test_normal_failure_has_no_usage_limit_reason(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """A real defect (no kill message anywhere) must not get a
    usage_limit_reason — the field stays None so it's never confused with a
    genuine limit kill."""
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: [
            sys.executable, "-c",
            "import sys; print('boom', file=sys.stderr); sys.exit(1)",
        ],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=2,
        issue_title="a real bug",
        briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    assert final.status == FAILED
    assert final.usage_limit_reason is None
    server.shutdown()


def test_usage_limit_reason_appears_in_log(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """The detection is written to the assignment log so operators can find
    it in `coord log <id>` without querying the agent."""
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: _kill_message_argv(exit_code=1),
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=3,
        issue_title="killed mid-flight",
        briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    assert final.log_path is not None
    log_text = Path(final.log_path).read_text()
    assert "usage-limit kill detected" in log_text
    server.shutdown()
