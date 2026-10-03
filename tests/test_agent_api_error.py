"""Tests for terminal-API-error classification at reap time (#1584).

Before this fix, `is_error: true` on a worker's terminal `result` event was
read in exactly one place (`format_important_event`, `coord/worker_events.py`)
and only to build a `coord watch` display string — nothing mapped it to
assignment status. A worker that died on a transient API error (a 529, a
500, a network drop) at turn 1 was recorded `status='done'`, indistinguishable
from a real success, silently ending an unattended drive.

This regression-guards the reap-time classification wired in
`AgentServer._reap` (coord/agent.py) and its `WorkerSummary.is_error` /
`format_api_error_reason` plumbing (coord/worker_events.py).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from coord.agent import (
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


# The #1563 evidence, verbatim shape.
_ERROR_RESULT_EVENT = {
    "type": "result",
    "is_error": True,
    "num_turns": 1,
    "stop_reason": "stop_sequence",
    "terminal_reason": "api_error",
    "api_error_status": 529,
    "result": (
        "API Error: 529 Overloaded. This is a server-side issue, usually "
        "temporary…"
    ),
    "total_cost_usd": 0.026247,
}

_INIT_EVENT = {
    "type": "system",
    "subtype": "init",
    "session_id": "sess-1",
    "model": "claude-haiku-4-5",
}

_OK_RESULT_EVENT = {
    "type": "result",
    "total_cost_usd": 0.05,
    "stop_reason": "end_turn",
    "num_turns": 2,
}


# Portable replacement for the POSIX-only `["/bin/sh", "-c", "printf ..."]`
# fake worker shape (#2725/#2843): `_print_lines_argv` runs the equivalent via
# `sys.executable -c` instead of a shell — no `/bin/sh` dependency, which is
# absent on Windows and was failing every test in this file at spawn time
# regardless of the `_maybe_bash_wrap` default (#2843's OTHER root cause,
# independent of the bash-wrap-default fix in `coord/agent.py`).
def _print_lines_argv(*events: dict, exit_code: int = 0) -> list[str]:
    """One NDJSON line per *event*, printed via a real python child process."""
    script = "".join(f"print({json.dumps(e)!r})\n" for e in events)
    if exit_code:
        script += f"import sys; sys.exit({exit_code})\n"
    return [sys.executable, "-c", script]


def test_terminal_api_error_recorded_failed(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """The #1563/#1584 core regression: a terminal `result` event carrying
    `is_error: true` must record the assignment FAILED — with the API status
    surfaced in `api_error_reason` — even though the wrapper process itself
    exits 0."""
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: _print_lines_argv(_INIT_EVENT, _ERROR_RESULT_EVENT),
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=1563,
        issue_title="review that never ran",
        briefing="b",
        branch="main",
        type="review",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    assert final.status == FAILED, (
        f"a terminal is_error:true result must never be recorded `done` — "
        f"got {final.status!r}"
    )
    assert final.api_error_reason == "529 Overloaded"
    assert final.usage_limit_reason is None
    server.shutdown()


def test_terminal_api_error_appears_in_log(
    tmp_path: Path, repo_local_only: Path
) -> None:
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: _print_lines_argv(_INIT_EVENT, _ERROR_RESULT_EVENT),
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=1,
        issue_title="review that never ran",
        briefing="b",
        branch="main",
        type="review",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    assert final.log_path is not None
    log_text = Path(final.log_path).read_text()
    assert "terminal API error detected" in log_text
    assert "529 Overloaded" in log_text
    server.shutdown()


def test_normal_successful_result_still_done(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """Regression: a normal successful `result` (`is_error` absent) must
    still be recorded DONE, with `api_error_reason` left None."""
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: _print_lines_argv(_INIT_EVENT, _OK_RESULT_EVENT),
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=2,
        issue_title="a clean review",
        briefing="b",
        branch="main",
        type="review",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    assert final.status == DONE
    assert final.api_error_reason is None
    server.shutdown()


def test_transient_error_then_internal_retry_success_still_done(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """The pinned regression risk: a worker that hits a transient API error,
    retries internally, and finishes successfully must NOT be marked failed.
    Its transcript has an earlier `result` line with `is_error: true`
    followed by a later, final one without it — only the LAST result event
    may decide the outcome."""
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: _print_lines_argv(_INIT_EVENT, _ERROR_RESULT_EVENT, _OK_RESULT_EVENT),
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=3,
        issue_title="retried internally then succeeded",
        briefing="b",
        branch="main",
        type="review",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    assert final.status == DONE, (
        "a worker whose FINAL result event succeeded must not be failed "
        f"just because an earlier one carried is_error — got {final.status!r}"
    )
    assert final.api_error_reason is None
    server.shutdown()


def test_terminal_api_error_with_nonzero_exit(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """The classification must not depend on the wrapper's own exit code —
    a non-zero exit alongside is_error must also land on FAILED with the
    reason recorded (not just a bare exit-code FAILED)."""
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: _print_lines_argv(_INIT_EVENT, _ERROR_RESULT_EVENT, exit_code=1),
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=4,
        issue_title="killed and exited non-zero",
        briefing="b",
        branch="main",
        type="review",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    assert final.status == FAILED
    assert final.api_error_reason == "529 Overloaded"
    server.shutdown()


def test_terminal_api_error_still_captures_claude_session_id(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """#1584 review (non-blocking perf finding): the is_error check and the
    claude_session_id capture now share a single full-log parse instead of
    each parsing the transcript independently — this regression-guards that
    the merge didn't drop the session_id capture for a FAILED/api-error
    assignment (a plausible way to break it: only wiring the shared parse's
    result into the is_error branch and forgetting the second consumer)."""
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: _print_lines_argv(_INIT_EVENT, _ERROR_RESULT_EVENT),
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=5,
        issue_title="review that never ran",
        briefing="b",
        branch="main",
        type="review",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    assert final.status == FAILED
    assert final.api_error_reason == "529 Overloaded"
    assert final.claude_session_id == "sess-1"
    server.shutdown()


def test_reap_parses_the_terminal_log_only_once(
    tmp_path: Path, repo_local_only: Path, monkeypatch
) -> None:
    """#1584 review (non-blocking perf finding): before this fix, `_reap`
    ran an independent `tail_bytes=0` full-transcript parse for the
    is_error check AND another one for the claude_session_id capture — two
    full parses of the same (potentially large) log on every single reap.
    They must now share one `coord.worker_events.parse_log` call."""
    import coord.worker_events as worker_events_mod

    calls: list[str] = []
    real_parse_log = worker_events_mod.parse_log

    def counting_parse_log(log_path, **kwargs):
        calls.append(log_path)
        return real_parse_log(log_path, **kwargs)

    monkeypatch.setattr(worker_events_mod, "parse_log", counting_parse_log)
    # coord.agent imports `parse_log` lazily (function-local `from coord.worker_events
    # import parse_log`), so patching the module attribute above is enough —
    # it's resolved at call time, not import time.

    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: _print_lines_argv(_INIT_EVENT, _ERROR_RESULT_EVENT),
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=6,
        issue_title="review that never ran",
        briefing="b",
        branch="main",
        type="review",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)
    assert final.status == FAILED

    assert len(calls) == 1, (
        f"expected exactly one full-transcript parse per reap, got {len(calls)}: {calls}"
    )
    server.shutdown()
