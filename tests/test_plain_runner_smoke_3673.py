"""#3673: Test stage as a plain runner — unit tests for the agent-side
executor (:mod:`coord.agent`).

Acceptance bar this file targets directly: "unit tests assert that a
plain-runner smoke records pass/fail from the exit code without spawning
claude, and that a failure spawns a summariser." The low-level tests drive
:func:`coord.agent.execute_plain_runner_smoke` / :func:`run_plain_runner_
command` directly with an injected ``run`` callable — no real subprocess,
no real `claude -p`. The end-to-end tests drive the real
:class:`coord.agent.AgentServer` (reusing the ``_server``/``_spec``/
``_init_repo`` fixtures from :mod:`tests.test_agent`) through
``server.assign(...)`` and confirm no `claude` binary is ever invoked for a
plain-runner leg's verdict, while a judgement-needing leg still gets the
ordinary `claude -p` path unchanged.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from coord.agent import (
    DONE,
    AssignmentSpec,
    PlainRunnerVerdict,
    execute_plain_runner_smoke,
    plain_runner_smoke_marker_lines,
    run_plain_runner_command,
)

from tests.test_agent import _init_repo, _server, _spec


# ── run_plain_runner_command: exit-code-only verdicts, no LLM ──────────────


def _fake_completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["x"], returncode=returncode, stdout=stdout, stderr=stderr)


def test_run_plain_runner_command_pass_from_exit_code_zero() -> None:
    calls: list[tuple] = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return _fake_completed(0, stdout="all tests passed\n")

    verdict = run_plain_runner_command("pytest", "/tmp/repo", timeout=30, run=fake_run)

    assert verdict.passed is True
    assert verdict.exit_code == 0
    assert "all tests passed" in verdict.log_tail
    assert verdict.baseline_red is False
    # The command actually ran with shell=True, in the given cwd — no `-p`,
    # no `claude` binary anywhere in the call.
    (args, kwargs) = calls[0]
    assert args[0] == "pytest"
    assert kwargs["cwd"] == "/tmp/repo"
    assert kwargs["shell"] is True


def test_run_plain_runner_command_fail_from_nonzero_exit_code() -> None:
    def fake_run(*args, **kwargs):
        return _fake_completed(1, stdout="", stderr="AssertionError: boom\n")

    verdict = run_plain_runner_command("pytest", "/tmp/repo", timeout=30, run=fake_run)

    assert verdict.passed is False
    assert verdict.exit_code == 1
    assert "AssertionError: boom" in verdict.log_tail
    assert verdict.baseline_red is False


def test_run_plain_runner_command_baseline_red_is_not_a_plain_failure() -> None:
    from coord.revalidate import BASELINE_RED_OUTPUT_MARKER, RUNNER_BASELINE_RED_EXIT

    def fake_run(*args, **kwargs):
        return _fake_completed(
            RUNNER_BASELINE_RED_EXIT,
            stdout=f"{BASELINE_RED_OUTPUT_MARKER} every failure also fails on main\n",
        )

    verdict = run_plain_runner_command("pytest", "/tmp/repo", timeout=30, run=fake_run)

    assert verdict.passed is False
    assert verdict.baseline_red is True
    assert verdict.exit_code == RUNNER_BASELINE_RED_EXIT


def test_run_plain_runner_command_timeout_is_a_failure_not_a_crash() -> None:
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="pytest", timeout=30, output="still running\n")

    verdict = run_plain_runner_command("pytest", "/tmp/repo", timeout=30, run=fake_run)

    assert verdict.passed is False
    assert verdict.exit_code == -1
    assert "timed out after 30s" in verdict.log_tail


def test_run_plain_runner_command_never_calls_the_real_runner_when_injected() -> None:
    """The injected `run` is the ONLY thing `run_plain_runner_command` calls
    to get an exit code — never `subprocess.Popen`, never the `claude`
    binary, confirming the verdict path has no LLM anywhere in it."""
    sentinel_calls: list = []

    def poisoned_popen(*a, **kw):
        sentinel_calls.append((a, kw))
        raise AssertionError("must never Popen a real subprocess here")

    original_popen = subprocess.Popen
    subprocess.Popen = poisoned_popen  # type: ignore[assignment]
    try:
        verdict = run_plain_runner_command(
            "pytest", "/tmp/repo", timeout=30, run=lambda *a, **kw: _fake_completed(0),
        )
    finally:
        subprocess.Popen = original_popen  # type: ignore[assignment]

    assert verdict.passed is True
    assert sentinel_calls == []


# ── execute_plain_runner_smoke: the failure->summariser seam ───────────────


def _smoke_spec(**overrides) -> AssignmentSpec:
    base = dict(
        repo_name="api", repo_path="/tmp/repo", issue_number=1, issue_title="t",
        briefing="b", files_allowed=[], files_forbidden=[], branch="main",
        type="smoke", plain_runner=True, smoke_command="pytest",
    )
    base.update(overrides)
    return AssignmentSpec(**base)


def test_execute_plain_runner_smoke_pass_never_spawns_a_summariser() -> None:
    summary_calls: list = []
    verdict = execute_plain_runner_smoke(
        _smoke_spec(),
        "/tmp/repo",
        timeout=30,
        run=lambda *a, **kw: _fake_completed(0, stdout="ok\n"),
        spawn_failure_summary=lambda s, v: summary_calls.append((s, v)),
    )
    assert verdict.passed is True
    assert summary_calls == []


def test_execute_plain_runner_smoke_failure_spawns_exactly_one_summariser_call() -> None:
    summary_calls: list = []
    verdict = execute_plain_runner_smoke(
        _smoke_spec(),
        "/tmp/repo",
        timeout=30,
        run=lambda *a, **kw: _fake_completed(1, stderr="nope\n"),
        spawn_failure_summary=lambda s, v: summary_calls.append((s, v)),
    )
    assert verdict.passed is False
    assert len(summary_calls) == 1
    spec_arg, verdict_arg = summary_calls[0]
    assert spec_arg.smoke_command == "pytest"
    assert verdict_arg.exit_code == 1


def test_execute_plain_runner_smoke_baseline_red_does_not_spawn_a_summariser() -> None:
    """A baseline-red verdict is a statement about the machine/merge-base,
    not a code defect — it gets no failure summary, same as a genuine pass."""
    from coord.revalidate import BASELINE_RED_OUTPUT_MARKER, RUNNER_BASELINE_RED_EXIT

    summary_calls: list = []
    verdict = execute_plain_runner_smoke(
        _smoke_spec(),
        "/tmp/repo",
        timeout=30,
        run=lambda *a, **kw: _fake_completed(
            RUNNER_BASELINE_RED_EXIT, stdout=f"{BASELINE_RED_OUTPUT_MARKER} red on main too\n",
        ),
        spawn_failure_summary=lambda s, v: summary_calls.append((s, v)),
    )
    assert verdict.baseline_red is True
    assert summary_calls == []


def test_execute_plain_runner_smoke_missing_command_fails_without_raising() -> None:
    verdict = execute_plain_runner_smoke(
        _smoke_spec(smoke_command=None), "/tmp/repo", timeout=30,
        run=lambda *a, **kw: _fake_completed(0),
    )
    assert verdict.passed is False
    assert verdict.exit_code == -1


def test_plain_runner_smoke_marker_lines_matches_claude_worker_vocabulary() -> None:
    """#3673: downstream verdict parsing (`coord.notify._record_smoke_
    verdict`) must read a plain-runner leg's log identically to a
    `claude -p` smoke worker's own printed marker — same `SMOKE:` prefix."""
    passed = PlainRunnerVerdict(passed=True, exit_code=0, log_tail="all good")
    assert "SMOKE: pass" in plain_runner_smoke_marker_lines(_smoke_spec(), passed)

    failed = PlainRunnerVerdict(passed=False, exit_code=1, log_tail="boom")
    text = plain_runner_smoke_marker_lines(_smoke_spec(), failed)
    assert "SMOKE: fail" in text
    assert "boom" in text

    baseline_red = PlainRunnerVerdict(passed=False, exit_code=42, log_tail="x", baseline_red=True)
    assert "SMOKE: baseline-red" in plain_runner_smoke_marker_lines(_smoke_spec(), baseline_red)


# ── End-to-end through the real AgentServer: no claude Popen for a plain
#    runner leg; the ordinary claude path is untouched when judgement is
#    needed. ───────────────────────────────────────────────────────────────


def test_agent_plain_runner_leg_passes_without_spawning_claude(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    server = _server(
        tmp_path, repo_path=repo,
        argv=[sys.executable, "-c", "raise SystemExit('claude must never run for this leg')"],
    )
    spec = _spec(
        repo, type="smoke", plain_runner=True,
        smoke_command=f"{sys.executable} -c \"print('leg-ran'); import sys; sys.exit(0)\"",
        smoke_timeout_s=30,
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    assert final.status == DONE
    assert final.exit_code == 0
    log = Path(final.log_path).read_text()
    assert "leg-ran" in log
    assert "SMOKE: pass" in log
    assert "plain-runner (#3673)" in log
    server.shutdown()


def test_agent_plain_runner_leg_failure_spawns_a_claude_summariser(
    tmp_path: Path, monkeypatch,
) -> None:
    import coord.agent as agent_mod

    repo = _init_repo(tmp_path / "repo")
    server = _server(tmp_path, repo_path=repo)

    summariser_calls: list = []
    real_run = subprocess.run

    # #3673 review round 1: the summariser now routes through
    # `coord.brain.call_claude` -> `provider.oneshot_command()` (the SAME
    # provider-routing seam brain planning uses) rather than a hardcoded
    # `[DEFAULT_WORKER_BINARY, "-p", "--output-format", "text"]` argv — see
    # `AgentServer._spawn_plain_runner_failure_summary`. With no provider
    # configured on this spec, that still resolves to `ClaudeProvider()`,
    # whose `oneshot_command()` still names `DEFAULT_WORKER_BINARY` as
    # argv[0] (no-config parity), just with `--output-format json` instead
    # of the old hardcoded `text` — `call_claude` falls back to raw stdout
    # when that isn't valid JSON, so a plain-text fake response still works.
    def fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd and cmd[0] == agent_mod.DEFAULT_WORKER_BINARY:
            summariser_calls.append(cmd)
            return subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout="it failed because the assertion didn't hold", stderr="",
            )
        return real_run(cmd, *args, **kwargs)

    # `coord.brain` imports `subprocess` as its own module-level name, but
    # it is the SAME stdlib module object `coord.agent` imports — patching
    # either module's `subprocess.run` attribute patches the one underlying
    # module, so this single patch still covers the call now made from
    # `coord.brain.call_claude` instead of directly from `coord.agent`.
    monkeypatch.setattr(agent_mod.subprocess, "run", fake_run)

    spec = _spec(
        repo, type="smoke", plain_runner=True,
        smoke_command=f"{sys.executable} -c \"import sys; sys.exit(5)\"",
        smoke_timeout_s=30,
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    assert final.status == DONE  # a failing smoke command is a normal verdict, not an infra FAILED
    assert final.exit_code == 5
    log = Path(final.log_path).read_text()
    assert "SMOKE: fail" in log
    assert "it failed because the assertion didn't hold" in log
    assert len(summariser_calls) == 1
    server.shutdown()


def test_agent_smoke_needing_judgement_falls_through_to_the_claude_path(
    tmp_path: Path,
) -> None:
    """``smoke_needs_judgement=True`` is the opt-out — even with
    ``plain_runner=True``, this leg must get the ordinary `claude -p`
    chat session, not the plain-runner subprocess path (#3673)."""
    repo = _init_repo(tmp_path / "repo")
    server = _server(
        tmp_path, repo_path=repo,
        argv=[sys.executable, "-c", "print('claude-path-ran')"],
    )
    spec = _spec(
        repo, type="smoke", plain_runner=True, smoke_needs_judgement=True,
        smoke_command="exit 0",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    log = Path(final.log_path).read_text()
    assert "claude-path-ran" in log
    assert "plain-runner (#3673)" not in log
    server.shutdown()
