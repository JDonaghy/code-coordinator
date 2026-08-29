"""Tests for the agent server core (no HTTP)."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import pytest

from coord.agent import (
    ADVISORY,
    CANCELLED,
    DONE,
    FAILED,
    PENDING,
    REFUSED_POLICY,
    REVIEW_DENY_COMMANDS,
    RUNNING,
    RUNTIME_CEILING_EXIT,
    MOCK_AUTHOR_SYSTEM_PROMPT,
    WORKER_SYSTEM_PROMPT,
    AgentAssignment,
    AgentServer,
    AssignmentSpec,
    _COMPLETED_HISTORY_CAP,
    _base_checkout_write_guard_tools,
    _git,
    _sealed_write_guard_tools,
    _worker_subprocess_env,
    default_worker_command,
    is_runtime_ceiling_reason,
)

from .conftest import noop_default_worker_command


def _init_repo(path: Path) -> Path:
    """Create a minimal git repo with one commit so worktrees can be created."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), check=True, capture_output=True)
    (path / "README").write_text("init\n")
    subprocess.run(["git", "add", "README"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=str(path), check=True, capture_output=True)
    return path


def _spec(repo_path: Path, **overrides) -> AssignmentSpec:
    base = dict(
        repo_name="api",
        repo_path=str(repo_path),
        issue_number=1,
        issue_title="t",
        briefing="b",
        files_allowed=[],
        files_forbidden=[],
        branch="main",
    )
    base.update(overrides)
    return AssignmentSpec(**base)


def _server(tmp_path: Path, *, argv: list[str] | None = None, repo_path: Path | None = None, **kwargs) -> AgentServer:
    if argv is None:
        argv = [sys.executable, "-c", "print('worker-output')"]
    # Ensure we have a git repo for worktree support
    rp = repo_path or _init_repo(tmp_path / "repo")
    return AgentServer(
        machine_name="test",
        capabilities=["python"],
        repos=["api"],
        state_dir=tmp_path / "state",
        worker_command=lambda spec: argv,
        repo_paths={"api": str(rp)},
        **kwargs,
    )


def test_worker_env_strips_agent_venv_from_path() -> None:
    # #402: when the agent runs inside a venv, the venv's bin must not be on
    # the worker's PATH (else a worker `pip install -e .` clobbers the agent).
    env = _worker_subprocess_env(
        {"PATH": "/venv/bin:/home/u/.local/bin:/usr/bin:/bin"},
        prefix="/venv",
        base_prefix="/usr",
    )
    parts = env["PATH"].split(os.pathsep)
    assert "/venv/bin" not in parts
    assert parts == ["/home/u/.local/bin", "/usr/bin", "/bin"]


def test_worker_env_clears_virtualenv_markers() -> None:
    env = _worker_subprocess_env(
        {"PATH": "/usr/bin", "VIRTUAL_ENV": "/venv", "PYTHONHOME": "/venv"},
        prefix="/venv",
        base_prefix="/usr",
    )
    assert "VIRTUAL_ENV" not in env
    assert "PYTHONHOME" not in env


def test_worker_env_preserves_path_when_not_in_venv() -> None:
    # System-Python agent (prefix == base_prefix): never strip /usr/bin & co.
    original = "/usr/local/bin:/usr/bin:/bin"
    env = _worker_subprocess_env(
        {"PATH": original},
        prefix="/usr",
        base_prefix="/usr",
    )
    assert env["PATH"] == original


def test_worker_env_keeps_unrelated_entries() -> None:
    env = _worker_subprocess_env(
        {"PATH": "/venv/bin:/opt/cargo/bin:/usr/bin", "EDITOR": "vim"},
        prefix="/venv",
        base_prefix="/usr",
    )
    assert env["PATH"] == "/opt/cargo/bin:/usr/bin"
    assert env["EDITOR"] == "vim"


def test_worker_env_cwd_sets_pwd() -> None:
    """#1783: passing cwd= sets PWD to that path explicitly, regardless of
    whatever PWD the base_env carried in from the daemon's own environment."""
    env = _worker_subprocess_env(
        {"PATH": "/usr/bin", "PWD": "/some/stale/daemon/cwd"},
        prefix="/usr",
        base_prefix="/usr",
        cwd="/worktrees/abc123",
    )
    assert env["PWD"] == "/worktrees/abc123"


def test_worker_env_no_cwd_leaves_pwd_untouched() -> None:
    """Without an explicit cwd=, _worker_subprocess_env must not touch PWD —
    callers that don't pass cwd (there are none left at the two spawn sites,
    but the helper is also usable standalone) get the pre-existing value."""
    env = _worker_subprocess_env(
        {"PATH": "/usr/bin", "PWD": "/whatever"},
        prefix="/usr",
        base_prefix="/usr",
    )
    assert env["PWD"] == "/whatever"


def test_worker_env_sets_coord_assignment_id() -> None:
    """#2217: the headless worker prompt (review.py) tells every worker that
    if $COORD_ASSIGNMENT_ID is set, it must report its verdict straight to
    the coordinator board via `coord report-result` as the *authoritative*
    path — with the transcript-parsed END_REVIEW block as a fallback only.
    Nothing set the variable for the headless `claude -p` dispatch lane, so
    that "primary" instruction was dead on arrival for every review and the
    fragile transcript parse was silently the only path. Both `_spawn` call
    sites must pass assignment_id= so this is set for every worker type."""
    env = _worker_subprocess_env(
        {"PATH": "/usr/bin"},
        prefix="/usr",
        base_prefix="/usr",
        assignment_id="abc123",
    )
    assert env["COORD_ASSIGNMENT_ID"] == "abc123"


def test_worker_env_no_assignment_id_leaves_var_unset() -> None:
    """When assignment_id isn't passed, COORD_ASSIGNMENT_ID must not appear
    at all (not even empty) — callers that don't have an assignment yet
    (e.g. standalone helper use) shouldn't leak a stale/empty value."""
    env = _worker_subprocess_env(
        {"PATH": "/usr/bin"},
        prefix="/usr",
        base_prefix="/usr",
    )
    assert "COORD_ASSIGNMENT_ID" not in env


def test_health_reports_machine(tmp_path: Path) -> None:
    server = _server(tmp_path)
    h = server.health()
    assert h["machine"] == "test"
    assert h["repos"] == ["api"]
    assert h["active"] == 0
    assert h["completed"] == 0


def test_health_includes_tool_versions_for_baseline_and_capabilities(
    tmp_path: Path,
) -> None:
    """#1570 B: /health publishes resolved tool versions — baseline (git,
    gh) plus whatever this machine's declared capabilities gate. `_server`
    declares `capabilities=["python"]`, so `python3` should show up
    alongside the baseline tools; `cargo`/`gtk4` (gated on capabilities this
    fixture doesn't declare) should not."""
    server = _server(tmp_path)
    tool_versions = server.health()["tool_versions"]
    assert "git" in tool_versions
    assert "gh" in tool_versions
    assert "python3" in tool_versions
    assert "cargo" not in tool_versions
    assert "gtk4" not in tool_versions
    # git is virtually guaranteed present in any dev/CI environment this
    # test suite runs in.
    assert tool_versions["git"]["found"] is True


def test_health_tool_versions_is_cached(tmp_path: Path) -> None:
    """Probing shells out per tool — /health must not re-probe on every
    call (mirrors worktree_bytes/artifact_bytes caching just above)."""
    server = _server(tmp_path)
    first = server.health()["tool_versions"]
    # Corrupt the cache's cached value in place to prove the second call
    # reused it rather than re-probing.
    sentinel = {"git": {"found": False, "version": "sentinel"}}
    server._tool_versions_cache = (server._tool_versions_cache[0], sentinel)
    second = server.health()["tool_versions"]
    assert second == sentinel

    # Force-expire the cache and a fresh probe runs.
    server._tool_versions_cache = None
    third = server.health()["tool_versions"]
    assert third == first


def test_health_tool_versions_probes_everything_when_config_free(
    tmp_path: Path,
) -> None:
    """#2913: a config-free agent declares `capabilities=[]` by design
    (docs/EPHEMERAL_WORKERS.md) — the coordinator's own `coordinator.yml`
    supplies capabilities at dispatch time, not this process. Restricting
    `/health.tool_versions` to `self.capabilities` (empty) meant the #1570 D
    cross-check in `dispatch_smoke` never had cargo/python3/etc. to compare
    against and silently failed OPEN for exactly the machines it exists to
    protect. A config-free agent must now probe every known capability
    regardless of its own (empty) declared list."""
    from coord.prereqs import ALL_CAPABILITY_NAMES

    server = AgentServer(
        machine_name="ephemeral-1",
        capabilities=[],
        repos=[],
        state_dir=tmp_path / "state",
        worktree_writable_settings_files=[],
        config_free_reason="no local coordinator.yml and no board service",
    )
    tool_versions = server.health()["tool_versions"]
    assert "cargo" in tool_versions
    assert "python3" in tool_versions
    assert "gtk4" in tool_versions
    assert "node" in tool_versions
    # Sanity: the probed set really does cover every capability-gated tool,
    # not just a hardcoded couple of names.
    probed_capabilities = {
        info["capability"] for info in tool_versions.values() if info["capability"]
    }
    assert probed_capabilities == set(ALL_CAPABILITY_NAMES)


def test_health_tool_versions_stays_restricted_when_not_config_free(
    tmp_path: Path,
) -> None:
    """A normal, fully-configured agent (`config_free_reason=None`, the
    default) must NOT gain the config-free agent's wider probe — `_server`
    declares only `capabilities=["python"]`, so `cargo`/`gtk4` stay absent,
    same as before #2913."""
    server = _server(tmp_path)
    tool_versions = server.health()["tool_versions"]
    assert "python3" in tool_versions
    assert "cargo" not in tool_versions
    assert "gtk4" not in tool_versions


def test_assign_success(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    server = _server(tmp_path, repo_path=repo)
    a = server.assign(_spec(repo))
    final = server.wait_for(a.id)
    # Worker makes no commits → advisory (#448)
    assert final.status == ADVISORY
    assert final.exit_code == 0
    assert final.worktree_path is not None
    log = Path(final.log_path).read_text()
    assert "worker-output" in log
    server.shutdown()


def test_assign_failure_marks_failed(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    server = _server(tmp_path, argv=[sys.executable, "-c", "import sys; print('nope'); sys.exit(7)"], repo_path=repo)
    a = server.assign(_spec(repo))
    final = server.wait_for(a.id)
    assert final.status == FAILED
    assert final.exit_code == 7
    server.shutdown()


def test_running_agent_kills_and_marks_a_leg_that_outlives_the_runtime_ceiling(
    tmp_path: Path,
) -> None:
    """#2638 acceptance, end to end through the real `AgentServer`.

    A leg whose wall-clock runtime crosses the configured ceiling would
    otherwise run forever (this worker never exits and never emits a byte).
    It must come back FAILED with a reason that says *runtime ceiling* — a
    generic failure here is the bug, because that is what let a suspended
    worker hold its assignment `running` for 10.5h with nothing in the
    fleet noticing.
    """
    repo = _init_repo(tmp_path / "repo")
    server = _server(
        tmp_path,
        argv=[sys.executable, "-c", "import time; time.sleep(300)"],
        repo_path=repo,
        runtime_ceiling_s=3.0,  # comfortably under one 5s reap poll interval
    )
    a = server.assign(_spec(repo))
    final = server.wait_for(a.id, timeout=30)

    assert final.status == FAILED
    assert final.exit_code == RUNTIME_CEILING_EXIT
    assert is_runtime_ceiling_reason(final.runtime_ceiling_reason), (
        "a ceiling kill must be distinguishable from a crash — got "
        f"{final.runtime_ceiling_reason!r}"
    )
    # The other reason fields sharing this "why FAILED" slot must stay clear.
    assert final.host_sleep_reason is None
    assert final.spend_ceiling_reason is None
    assert final.usage_limit_reason is None
    # And the kill is narrated in the worker's own log.
    log_text = Path(final.log_path).read_text()
    assert "runtime ceiling breached" in log_text
    server.shutdown()


def test_running_agent_with_a_generous_ceiling_finishes_untouched(tmp_path: Path) -> None:
    """The overwhelming majority of legs the ceiling must never touch: a
    quick worker finishes on its own terms, well under a generous ceiling,
    and nothing about it appears anywhere."""
    repo = _init_repo(tmp_path / "repo")
    server = _server(
        tmp_path,
        argv=[sys.executable, "-c", "print('hi')"],
        repo_path=repo,
        runtime_ceiling_s=6.0 * 3600.0,
    )
    a = server.assign(_spec(repo))
    final = server.wait_for(a.id, timeout=15)

    assert final.exit_code == 0
    assert final.runtime_ceiling_reason is None
    assert final.host_sleep_reason is None
    server.shutdown()


def test_assign_unknown_binary_marks_failed(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    # bash_wrap_spawn=False so the unknown binary surfaces as a FileNotFoundError
    # at Popen time (the spawn-failed path sets assignment.error). With the
    # bash-wrap on, bash spawns fine and `exec` fails inside the child, which is
    # covered separately as a non-zero exit → FAILED.
    server = _server(
        tmp_path, argv=["/no/such/binary"], repo_path=repo, bash_wrap_spawn=False
    )
    a = server.assign(_spec(repo))
    final = server.wait_for(a.id)
    assert final.status == FAILED
    assert final.error is not None


def test_assign_unknown_binary_bash_wrapped_marks_failed(tmp_path: Path) -> None:
    """With the bash-wrap on, an unknown binary fails via bash exec's non-zero
    exit (#299) — the assignment still ends up FAILED."""
    repo = _init_repo(tmp_path / "repo")
    server = _server(
        tmp_path, argv=["/no/such/binary"], repo_path=repo, bash_wrap_spawn=True
    )
    a = server.assign(_spec(repo))
    final = server.wait_for(a.id)
    assert final.status == FAILED
    assert final.exit_code not in (0, None)


def test_initial_briefing_is_written_to_worker_stdin(tmp_path: Path) -> None:
    """The briefing must reach the worker via stdin as a stream-json line."""
    repo = _init_repo(tmp_path / "repo")
    # Read exactly one line from stdin into the log, then exit.
    server = _server(
        tmp_path,
        argv=[sys.executable, "-c", "import sys; print(sys.stdin.readline().rstrip(chr(10)))"],
        repo_path=repo,
    )
    a = server.assign(_spec(repo, briefing="hello world"))
    final = server.wait_for(a.id)
    log = Path(final.log_path).read_text()
    assert '"type": "user"' in log, "stream-json envelope missing from stdin echo"
    assert "hello world" in log, "briefing text missing from stdin echo"


def test_inject_message_writes_to_worker_stdin(tmp_path: Path) -> None:
    """inject_message writes a stream-json user line to the worker's stdin."""
    import time as _time
    repo = _init_repo(tmp_path / "repo")
    # Worker reads two lines (initial briefing + injection) then exits.
    server = _server(
        tmp_path,
        argv=[sys.executable, "-c", "import sys; a = sys.stdin.readline().rstrip(chr(10)); print('got1=' + a); b = sys.stdin.readline().rstrip(chr(10)); print('got2=' + b)"],
        repo_path=repo,
    )
    a = server.assign(_spec(repo, briefing="first"))
    # Give Popen a moment to wire stdin and consume the first line.
    _time.sleep(0.3)
    server.inject_message(a.id, "second message")
    final = server.wait_for(a.id, timeout=5.0)
    log = Path(final.log_path).read_text()
    assert "got1=" in log and "first" in log
    assert "got2=" in log and "second message" in log
    assert "# inject: second message" in log, "inject marker missing from log"


def test_maybe_bash_wrap_helper() -> None:
    """The pure wrap helper produces bash -c 'exec ...' when enabled (#299)."""
    from coord.agent import _maybe_bash_wrap

    argv = ["claude", "-p", "--allowedTools", "Read,Bash"]
    assert _maybe_bash_wrap(argv, enabled=False) == argv
    wrapped = _maybe_bash_wrap(argv, enabled=True)
    assert wrapped == ["bash", "-c", "exec claude -p --allowedTools Read,Bash"]


def test_spawn_bash_wrap_enabled_routes_through_bash(tmp_path: Path) -> None:
    """With bash_wrap_spawn=True, _spawn launches via bash -c 'exec ...'."""
    import coord.agent as agent_mod

    repo = _init_repo(tmp_path / "repo")
    server = _server(
        tmp_path,
        argv=[sys.executable, "-c", "print('worker-output')"],
        repo_path=repo,
        bash_wrap_spawn=True,
    )
    captured: list[list[str]] = []
    real_popen = agent_mod.subprocess.Popen

    def recording_popen(spawn_argv, *args, **kwargs):
        # Only record the worker spawn (started in its own session); the
        # assign flow also runs git via Popen-backed subprocess.run.
        if kwargs.get("start_new_session"):
            captured.append(spawn_argv)
        return real_popen(spawn_argv, *args, **kwargs)

    agent_mod.subprocess.Popen = recording_popen  # type: ignore[assignment]
    try:
        a = server.assign(_spec(repo))
        final = server.wait_for(a.id)
    finally:
        agent_mod.subprocess.Popen = real_popen  # type: ignore[assignment]
    # Worker makes no commits → advisory (#448)
    assert final.status == ADVISORY
    assert captured, "Popen was not called"
    assert captured[0][:2] == ["bash", "-c"]
    # #2725: the inner argv is now the portable `sys.executable -c '<script>'`
    # form rather than `/bin/sh -c '<script>'`; compute the expected
    # shlex-quoted form rather than hardcoding it, since the exact quoting
    # depends on `sys.executable`'s path.
    assert captured[0][2] == "exec " + shlex.join(
        [sys.executable, "-c", "print('worker-output')"]
    )
    # The wrapped command still produced the worker's output.
    assert "worker-output" in Path(final.log_path).read_text()
    server.shutdown()


def test_spawn_bash_wrap_disabled_uses_bare_argv(tmp_path: Path) -> None:
    """With bash_wrap_spawn=False, _spawn launches the bare argv."""
    import coord.agent as agent_mod

    repo = _init_repo(tmp_path / "repo")
    server = _server(
        tmp_path,
        argv=[sys.executable, "-c", "print('worker-output')"],
        repo_path=repo,
        bash_wrap_spawn=False,
    )
    captured: list[list[str]] = []
    real_popen = agent_mod.subprocess.Popen

    def recording_popen(spawn_argv, *args, **kwargs):
        # Only record the worker spawn (started in its own session); the
        # assign flow also runs git via Popen-backed subprocess.run.
        if kwargs.get("start_new_session"):
            captured.append(spawn_argv)
        return real_popen(spawn_argv, *args, **kwargs)

    agent_mod.subprocess.Popen = recording_popen  # type: ignore[assignment]
    try:
        a = server.assign(_spec(repo))
        final = server.wait_for(a.id)
    finally:
        agent_mod.subprocess.Popen = real_popen  # type: ignore[assignment]
    # Worker makes no commits → advisory (#448)
    assert final.status == ADVISORY
    assert captured and captured[0] == [sys.executable, "-c", "print('worker-output')"]
    server.shutdown()


def test_spawn_sets_pwd_to_worktree_bash_wrap_disabled(tmp_path: Path) -> None:
    """#1783: with bash_wrap_spawn=False, the worker's PWD env var must equal
    its worktree path.

    This is the regression the issue exists for: without bash_wrap_spawn's
    incidental `bash -c 'exec ...'` respawn (which happens to recompute PWD
    as a side effect of #299), nothing else corrected a stale PWD inherited
    from the agent daemon's own environment — confinement for a provider
    that trusts $PWD over getcwd() held only by accident. This test must
    fail against pre-#1783 code.
    """
    import coord.agent as agent_mod

    repo = _init_repo(tmp_path / "repo")
    server = _server(tmp_path, repo_path=repo, bash_wrap_spawn=False)

    captured_env: list[dict[str, str]] = []
    real_popen = agent_mod.subprocess.Popen

    def recording_popen(spawn_argv, *args, **kwargs):
        if kwargs.get("start_new_session"):
            captured_env.append(dict(kwargs.get("env") or {}))
        return real_popen(spawn_argv, *args, **kwargs)

    agent_mod.subprocess.Popen = recording_popen  # type: ignore[assignment]
    try:
        a = server.assign(_spec(repo))
        final = server.wait_for(a.id)
    finally:
        agent_mod.subprocess.Popen = real_popen  # type: ignore[assignment]

    assert final.status == ADVISORY
    assert captured_env, "Popen was not called"
    assert final.worktree_path is not None
    assert captured_env[0]["PWD"] == final.worktree_path
    server.shutdown()


def test_spawn_sets_pwd_to_worktree_bash_wrap_enabled(tmp_path: Path) -> None:
    """#1783: the same PWD==worktree assertion holds with bash_wrap_spawn=True
    (the default) — no behaviour change on the default path. PWD is now set
    explicitly rather than relying on bash recomputing it as a side effect."""
    import coord.agent as agent_mod

    repo = _init_repo(tmp_path / "repo")
    server = _server(tmp_path, repo_path=repo, bash_wrap_spawn=True)

    captured_env: list[dict[str, str]] = []
    real_popen = agent_mod.subprocess.Popen

    def recording_popen(spawn_argv, *args, **kwargs):
        if kwargs.get("start_new_session"):
            captured_env.append(dict(kwargs.get("env") or {}))
        return real_popen(spawn_argv, *args, **kwargs)

    agent_mod.subprocess.Popen = recording_popen  # type: ignore[assignment]
    try:
        a = server.assign(_spec(repo))
        final = server.wait_for(a.id)
    finally:
        agent_mod.subprocess.Popen = real_popen  # type: ignore[assignment]

    assert final.status == ADVISORY
    assert captured_env, "Popen was not called"
    assert final.worktree_path is not None
    assert captured_env[0]["PWD"] == final.worktree_path
    server.shutdown()


def test_spawn_sets_coord_assignment_id(tmp_path: Path) -> None:
    """#2217: the headless `_spawn` path must set COORD_ASSIGNMENT_ID to the
    assignment's own id — this is the variable the review prompt tells every
    worker to use for `coord report-result --assignment "$COORD_ASSIGNMENT_ID"
    ...`, the "authoritative" primary verdict path. Before #2217 nothing set
    it anywhere in this dispatch lane, so that instruction was silently
    unreachable for every headless review; only the fragile transcript-parsed
    END_REVIEW fallback ever ran. This test must fail against pre-#2217 code.
    """
    import coord.agent as agent_mod

    repo = _init_repo(tmp_path / "repo")
    server = _server(tmp_path, repo_path=repo)

    captured_env: list[dict[str, str]] = []
    real_popen = agent_mod.subprocess.Popen

    def recording_popen(spawn_argv, *args, **kwargs):
        if kwargs.get("start_new_session"):
            captured_env.append(dict(kwargs.get("env") or {}))
        return real_popen(spawn_argv, *args, **kwargs)

    agent_mod.subprocess.Popen = recording_popen  # type: ignore[assignment]
    try:
        a = server.assign(_spec(repo, type="review"))
        final = server.wait_for(a.id)
    finally:
        agent_mod.subprocess.Popen = real_popen  # type: ignore[assignment]

    assert captured_env, "Popen was not called"
    assert captured_env[0]["COORD_ASSIGNMENT_ID"] == final.id == a.id
    server.shutdown()


def test_pty_spawn_sets_pwd_to_repo_path(tmp_path: Path) -> None:
    """#1783: the PTY spawn path (_spawn_pty) gets the same explicit-PWD
    treatment as the headless path.

    Unlike headless, PTY spawns never route through bash_wrap_spawn — see
    ``_spawn_pty``'s docstring, wrapping an interactive ``claude`` in
    ``bash -c 'exec ...'`` breaks TTY allocation — so this path never got
    even an *incidental* PWD correction. It needs the explicit fix on its
    own merits, independent of any config flag.

    Every spec type a ``ClaudePtyProvider`` may run under (the safety gate
    refuses write-capable types on providers with
    ``enforces_deny_list=False``) is also a no-worktree type, so the cwd
    ``_spawn_pty`` receives is the shared repo checkout, not a
    per-assignment worktree — the assertion is against that path.
    """
    import coord.agent as agent_mod
    from coord.providers.claude_pty import ClaudePtyProvider

    class _QuickExitPtyProvider(ClaudePtyProvider):
        def build_command(self, spec, *, resolved_model=None, **_kwargs):
            return [sys.executable, "-c", "import sys; sys.exit(0)"]

        def initial_input(self, spec):
            # Falsy → _spawn_pty skips the readiness-wait + paste dance
            # entirely; this test only cares about the env Popen got.
            return b""

    repo = _init_repo(tmp_path / "repo")
    captured_env: list[dict[str, str]] = []
    real_popen = agent_mod.subprocess.Popen

    def recording_popen(spawn_argv, *args, **kwargs):
        if kwargs.get("start_new_session"):
            captured_env.append(dict(kwargs.get("env") or {}))
        return real_popen(spawn_argv, *args, **kwargs)

    server = AgentServer(
        machine_name="test",
        capabilities=["python"],
        repos=["api"],
        state_dir=tmp_path / "state",
        worker_command=lambda spec: [sys.executable, "-c", "print('unused')"],
        repo_paths={"api": str(repo)},
        providers={"claude-pty": _QuickExitPtyProvider()},
    )
    agent_mod.subprocess.Popen = recording_popen  # type: ignore[assignment]
    try:
        spec = _spec(repo, provider="claude-pty", type="plan")
        a = server.assign(spec)
        final = server.wait_for(a.id, timeout=10.0)
    finally:
        agent_mod.subprocess.Popen = real_popen  # type: ignore[assignment]

    assert captured_env, "Popen was not called"
    assert captured_env[0]["PWD"] == str(repo)
    server.shutdown()
    assert final is not None  # spawned + reaped without raising


def test_agent_server_defaults_bash_wrap_and_timeout(tmp_path: Path) -> None:
    """AgentServer defaults: bash_wrap_spawn on, first_output_timeout 600 (#299)."""
    server = _server(tmp_path)
    assert server.bash_wrap_spawn is True
    assert server.first_output_timeout == 600.0
    # #2638: generous-but-on by default, mirroring the TTFT watchdog above —
    # a leg is never silently uncapped just because nobody configured one.
    assert server.runtime_ceiling_s == 6.0 * 60.0 * 60.0
    server.shutdown()


def test_inject_message_unknown_id_raises(tmp_path: Path) -> None:
    server = _server(tmp_path)
    with pytest.raises(KeyError):
        server.inject_message("does-not-exist", "hi")


def test_inject_message_on_finished_assignment_raises(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    server = _server(tmp_path, repo_path=repo)
    a = server.assign(_spec(repo))
    server.wait_for(a.id)  # let it finish
    with pytest.raises((RuntimeError, BrokenPipeError)):
        server.inject_message(a.id, "too late")


def test_cancel_running_assignment(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    server = _server(tmp_path, argv=[sys.executable, "-c", "import time; time.sleep(30)"], repo_path=repo)
    a = server.assign(_spec(repo))
    # Wait until it's actually running so cancel has something to terminate.
    for _ in range(50):
        if server.get(a.id).status == RUNNING:
            break
        time.sleep(0.02)
    server.cancel(a.id)
    final = server.get(a.id)
    assert final.status == CANCELLED
    server.shutdown()


def test_cancel_unknown_id_raises(tmp_path: Path) -> None:
    server = _server(tmp_path)
    with pytest.raises(KeyError):
        server.cancel("nope")


def test_rejects_unhandled_repo(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    server = _server(tmp_path, repo_path=repo)
    with pytest.raises(ValueError, match="does not handle repo"):
        server.assign(_spec(repo, repo_name="other"))


def test_rejects_missing_repo_path(tmp_path: Path) -> None:
    server = _server(tmp_path)
    with pytest.raises(ValueError, match="repo path does not exist"):
        server.assign(_spec(tmp_path / "missing"))


def test_state_persists_to_disk(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    server = _server(tmp_path, repo_path=repo)
    a = server.assign(_spec(repo))
    server.wait_for(a.id)
    state = json.loads((tmp_path / "state" / "agent_state.json").read_text())
    ids = [entry["id"] for entry in state["assignments"]]
    assert a.id in ids
    # worktree_path should be persisted
    entry = next(e for e in state["assignments"] if e["id"] == a.id)
    assert entry["worktree_path"] is not None
    server.shutdown()


def test_orphaned_running_assignments_marked_failed_on_load(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "agent_state.json").write_text(
        json.dumps(
            {
                "machine": "test",
                "capabilities": [],
                "repos": ["api"],
                "assignments": [
                    {
                        "id": "abc123",
                        "status": "running",
                        "pid": 99999,
                        "started_at": 1.0,
                        "finished_at": None,
                        "exit_code": None,
                        "log_path": str(tmp_path / "abc123.log"),
                        "error": None,
                        "branch": None,
                        "worktree_path": None,
                        "spec": {
                            "repo_name": "api",
                            "repo_path": str(tmp_path),
                            "issue_number": 1,
                            "issue_title": "t",
                            "briefing": "b",
                            "files_allowed": [],
                            "files_forbidden": [],
                            "branch": "main",
                        },
                    }
                ],
            }
        )
    )
    server = AgentServer(
        machine_name="test", repos=["api"], state_dir=state_dir
    )
    recovered = server.get("abc123")
    assert recovered is not None
    assert recovered.status == FAILED
    assert "restarted" in recovered.error


# ── Tests for health().worktree_bytes and clean_worktrees() ──────────────────

def test_health_includes_worktree_bytes(tmp_path: Path) -> None:
    """health() always includes worktree_bytes (0 when no worktrees exist)."""
    server = _server(tmp_path)
    h = server.health()
    assert "worktree_bytes" in h
    assert h["worktree_bytes"] == 0


def test_health_worktree_bytes_reflects_disk_usage(tmp_path: Path) -> None:
    """health() worktree_bytes increases when files exist under worktrees/."""
    server = _server(tmp_path)
    wt_dir = server.state_dir / "worktrees" / "fake-id"
    wt_dir.mkdir(parents=True)
    (wt_dir / "big.bin").write_bytes(b"X" * 4096)

    h = server.health()
    assert h["worktree_bytes"] >= 4096


def test_clean_worktrees_empty_base(tmp_path: Path) -> None:
    """clean_worktrees returns zero counts when no worktrees directory exists."""
    server = _server(tmp_path)
    result = server.clean_worktrees()
    assert result["cleaned"] == 0
    assert result["kept"] == 0
    assert result["bytes_freed"] == 0
    # #1402: the cargo-cache GC runs on this exit path too (a machine can
    # hold a multi-GiB cache with no worktrees at all), reporting an empty
    # cache rather than being skipped.
    assert result["cargo_cache_bytes"] == 0
    assert result["cargo_caches_evicted"] == 0


def test_clean_worktrees_removes_orphan(tmp_path: Path) -> None:
    """Orphaned worktrees (no matching assignment) are removed.

    Uses ``recent_secs=0`` to bypass the race-window mtime guard that
    normally protects just-created directories from being deleted out
    from under a still-spawning worker.
    """
    server = _server(tmp_path)
    orphan = server.state_dir / "worktrees" / "no-such-assignment"
    orphan.mkdir(parents=True)
    (orphan / "file.txt").write_text("data")

    result = server.clean_worktrees(recent_secs=0)
    assert result["cleaned"] == 1
    assert result["bytes_freed"] > 0
    assert not orphan.exists()


def test_clean_worktrees_keeps_fresh_orphan(tmp_path: Path) -> None:
    """Race protection: an orphan whose mtime is within recent_secs is kept.

    Closes the window where ``_setup_worktree`` has created the
    directory but ``assign()`` hasn't yet inserted the assignment into
    ``self._assignments`` — without this guard a concurrent
    ``clean_worktrees`` would ``git worktree remove`` the freshly-made
    tree out from under the spawning worker.
    """
    server = _server(tmp_path)
    # mtime is "now" — within the default 5-minute recent_secs window.
    fresh = server.state_dir / "worktrees" / "racing-id"
    fresh.mkdir(parents=True)
    (fresh / "file.txt").write_text("partial")

    result = server.clean_worktrees(recent_secs=300)
    assert result["cleaned"] == 0
    assert result["kept"] == 1
    assert fresh.exists()


def test_clean_worktrees_removes_aged_orphan(tmp_path: Path) -> None:
    """An orphan with old mtime is removed under the default recent_secs."""
    server = _server(tmp_path)
    aged = server.state_dir / "worktrees" / "old-orphan"
    aged.mkdir(parents=True)
    (aged / "leftover.txt").write_text("stale")
    # Back-date the directory mtime to simulate an orphan from a
    # previous agent session.
    old = time.time() - 3600  # 1 hour ago
    os.utime(aged, (old, old))

    result = server.clean_worktrees(recent_secs=300)
    assert result["cleaned"] == 1
    assert not aged.exists()


def test_clean_worktrees_keeps_recently_finished(tmp_path: Path) -> None:
    """Recently-finished assignments are kept (worker may still be tearing down)."""
    repo = _init_repo(tmp_path / "repo")
    server = _server(tmp_path, repo_path=repo)
    a = server.assign(_spec(repo))
    final = server.wait_for(a.id)

    # Re-create the worktree dir so we have something to potentially clean.
    stale_wt = server.state_dir / "worktrees" / final.id
    stale_wt.mkdir(parents=True, exist_ok=True)
    (stale_wt / "leftover.txt").write_text("stale data")
    # The assignment record's finished_at is "now-ish"; default
    # recent_secs=300 should keep the worktree.
    result = server.clean_worktrees(recent_secs=300)
    assert result["kept"] >= 1
    assert result["cleaned"] == 0
    assert stale_wt.exists()
    server.shutdown()


def test_health_worktree_bytes_is_cached(tmp_path: Path) -> None:
    """health()'s worktree_bytes is cached so /health doesn't rglob every call.

    Files added after the first call should not be visible until the
    cache TTL expires (or is invalidated).
    """
    server = _server(tmp_path)
    wt_dir = server.state_dir / "worktrees" / "cache-test"
    wt_dir.mkdir(parents=True)
    (wt_dir / "a.bin").write_bytes(b"X" * 1024)

    first = server.health()["worktree_bytes"]
    assert first >= 1024

    # Add a much bigger file after the cache has been populated.
    (wt_dir / "b.bin").write_bytes(b"Y" * 8192)
    second = server.health()["worktree_bytes"]
    # Cache TTL is ~30 s by default — the new file should not be visible yet.
    assert second == first

    # Force-expire the cache and the new size becomes visible.
    server._worktree_bytes_cache = None
    third = server.health()["worktree_bytes"]
    assert third >= first + 8192


def test_clean_worktrees_keeps_running(tmp_path: Path) -> None:
    """Worktrees for running assignments are never touched."""
    repo = _init_repo(tmp_path / "repo")
    # Use a worker that sleeps long enough for us to inspect state.
    server = _server(tmp_path, argv=[sys.executable, "-c", "import time; time.sleep(10)"], repo_path=repo)
    a = server.assign(_spec(repo))

    # Give the worker a moment to start and create its worktree.
    time.sleep(0.5)

    result = server.clean_worktrees()
    assert result["kept"] >= 1
    assert result["cleaned"] == 0
    server.shutdown()


def test_clean_worktrees_removes_stale_done(tmp_path: Path) -> None:
    """Worktrees whose assignment is done and old (> recent_secs) are removed.

    Simulates a crash-recovery scenario: the agent recorded the assignment
    as done but the worktree directory was not cleaned up before the crash.
    """
    repo = _init_repo(tmp_path / "repo")
    server = _server(tmp_path, repo_path=repo)
    a = server.assign(_spec(repo))
    final = server.wait_for(a.id)

    # The agent's normal cleanup already removed the worktree.  Re-create it
    # to simulate an unclean shutdown where cleanup didn't run.
    stale_wt = server.state_dir / "worktrees" / final.id
    stale_wt.mkdir(parents=True, exist_ok=True)
    (stale_wt / "leftover.txt").write_text("stale data")

    # recent_secs=0 means even a just-finished assignment is eligible.
    result = server.clean_worktrees(recent_secs=0)
    assert result["cleaned"] >= 1
    assert not stale_wt.exists()
    server.shutdown()


# ── #1295: hourly worktree sweep must not delete live worktrees ─────────────


def test_clean_worktrees_skips_when_tmux_session_alive(
    tmp_path: Path, monkeypatch
) -> None:
    """#1295 (load-bearing): a worktree with a live ``coord-<aid>`` tmux session
    on this host must NOT be swept, even when the AgentServer's local
    ``self._assignments`` map has no record of it (agent restart, interactive
    session outliving the dispatch subprocess, etc.).

    Without this guard the hourly sweep would ``git worktree remove`` an
    interactive Test/Review/Merge pane out from under the operator (the
    incident that motivated the issue).
    """
    server = _server(tmp_path)
    live_id = "live-interactive-aid"
    live_wt = server.state_dir / "worktrees" / live_id
    live_wt.mkdir(parents=True)
    (live_wt / "artifact.bin").write_bytes(b"X" * 4096)
    # Back-date so the recent_secs guard is NOT what saves it — we want to
    # prove the tmux guard is what's doing the work.
    old = time.time() - 3600
    os.utime(live_wt, (old, old))

    # Simulate a live tmux session for this assignment_id by monkey-patching
    # the class-level probe.  Real tmux is not available in CI; the guard's
    # contract is "consult _tmux_session_alive and skip when it says yes".
    called_with: list[str] = []

    def fake_alive(aid: str) -> bool:
        called_with.append(aid)
        return aid == live_id

    monkeypatch.setattr(
        AgentServer, "_tmux_session_alive", staticmethod(fake_alive)
    )

    result = server.clean_worktrees(recent_secs=0)
    assert live_id in called_with
    assert result["cleaned"] == 0
    assert result["kept"] == 1
    assert live_wt.exists()


def test_clean_worktrees_tmux_probe_failure_does_not_abort_sweep(
    tmp_path: Path, monkeypatch
) -> None:
    """#1295: when the tmux probe raises (tmux not installed / server dead /
    subprocess crash) the guard degrades to ``False`` and the sweep continues
    on OTHER worktrees — a broken tmux install must not orphan disk fleet-wide.
    """
    from coord import interactive as _interactive  # noqa: PLC0415

    server = _server(tmp_path)
    old = time.time() - 3600
    for name in ("aid-a", "aid-b"):
        wt = server.state_dir / "worktrees" / name
        wt.mkdir(parents=True)
        (wt / "leftover.txt").write_text("x")
        os.utime(wt, (old, old))

    # Make the underlying probe raise as if tmux itself blew up.  The
    # ``_tmux_session_alive`` wrapper on AgentServer must catch this and
    # return False so the sweep proceeds normally on both orphans.
    def _boom(*args, **kwargs):
        raise RuntimeError("tmux server not responding")

    monkeypatch.setattr(_interactive, "tmux_available", lambda: True)
    monkeypatch.setattr(_interactive, "tmux_session_alive", _boom)

    # Wrapper itself must not raise.
    assert server._tmux_session_alive("any-aid") is False

    # And the sweep must complete for BOTH entries even though every probe
    # would raise if the exception weren't swallowed.
    result = server.clean_worktrees(recent_secs=0)
    assert result["cleaned"] == 2
    assert result["kept"] == 0
    assert not (server.state_dir / "worktrees" / "aid-a").exists()
    assert not (server.state_dir / "worktrees" / "aid-b").exists()


def test_tmux_session_alive_treats_dead_pane_as_not_alive(monkeypatch) -> None:
    """#2541: ``remain-on-exit on`` keeps ``has-session`` True after ANY
    pane exit (clean success or crash) until a reaper notices — so the
    ``clean_worktrees`` load-bearing guard must consult
    ``tmux_session_running`` (alive AND pane not dead), not the bare
    ``has-session`` check, or it would keep protecting a worktree whose
    interactive session has already finished, indefinitely.
    """
    from coord import interactive as _interactive  # noqa: PLC0415

    monkeypatch.setattr(_interactive, "tmux_available", lambda: True)

    # A session that "exists" (has-session succeeds) but whose pane already
    # exited must be reported as NOT alive by the AgentServer wrapper.
    monkeypatch.setattr(_interactive, "tmux_session_running", lambda *a, **k: False)
    assert AgentServer._tmux_session_alive("dead-pane-aid") is False

    # A session whose pane is genuinely still running is reported alive.
    monkeypatch.setattr(_interactive, "tmux_session_running", lambda *a, **k: True)
    assert AgentServer._tmux_session_alive("running-aid") is True


def test_clean_worktrees_respects_protect_list(tmp_path: Path) -> None:
    """#1295: a coordinator-supplied ``protect`` list of assignment_ids is
    honoured even when the agent has no local record of them and no tmux
    session is up.  Belt-and-braces guard against agent-restart-lost state.
    """
    server = _server(tmp_path)
    protected_id = "coord-known-live"
    other_id = "unrelated-orphan"
    for name in (protected_id, other_id):
        wt = server.state_dir / "worktrees" / name
        wt.mkdir(parents=True)
        (wt / "data.txt").write_text("x")
        old = time.time() - 3600
        os.utime(wt, (old, old))

    result = server.clean_worktrees(recent_secs=0, protect=[protected_id])
    assert result["cleaned"] == 1
    assert result["kept"] == 1
    # Return shape is additive only: the original three keys are unchanged
    # (#1402 adds the cargo-cache GC counters alongside them).
    assert {"cleaned", "kept", "bytes_freed"} <= set(result)
    assert (server.state_dir / "worktrees" / protected_id).exists()
    assert not (server.state_dir / "worktrees" / other_id).exists()


def test_clean_worktrees_skips_symlinks(tmp_path: Path) -> None:
    """#1295: a symlink under ``state_dir/worktrees/`` is never followed or
    removed by the sweep.  A real orphan sitting next to it must still be
    cleaned up — one symlink can't stop the whole sweep.
    """
    server = _server(tmp_path)
    worktree_base = server.state_dir / "worktrees"
    worktree_base.mkdir(parents=True, exist_ok=True)

    # Create a real target directory well OUTSIDE the worktree base — if
    # the sweep follows the symlink and rmtrees, this loses data.
    target = tmp_path / "somewhere-else"
    target.mkdir()
    (target / "precious.txt").write_text("do not delete")

    sym = worktree_base / "sneaky-symlink"
    sym.symlink_to(target, target_is_directory=True)

    # A genuine orphan sibling.
    orphan = worktree_base / "real-orphan"
    orphan.mkdir()
    (orphan / "junk.bin").write_text("junk")
    old = time.time() - 3600
    os.utime(orphan, (old, old))

    result = server.clean_worktrees(recent_secs=0)
    # Symlink counted as kept (skipped), orphan cleaned.
    assert result["cleaned"] == 1
    assert not orphan.exists()
    # Symlink still there, target untouched.
    assert sym.is_symlink()
    assert target.exists()
    assert (target / "precious.txt").read_text() == "do not delete"


def test_clean_worktrees_stashes_orphaned_worktree_with_no_assignment_record(
    tmp_path: Path,
) -> None:
    """#1295 fix item #2: a worktree with NO assignment record at all — the
    interactive session that built it ended without ever running finalize
    (crash, `tmux kill-session`, network drop before `coord done`) — must
    still get its configured artifacts stashed before the sweep destroys it.

    The tmux/protect guards (tested above) only catch a session that is
    STILL alive; this is the narrower "already dead, never finalized" gap
    the issue's fix item #2 explicitly called out, and which the plain
    ``if a is not None: self._stash_artifacts(a)`` guard left unhandled
    (there's no ``AgentAssignment`` to hand to it) before this fix.
    """
    repo = _init_repo(tmp_path / "repo")
    server = _server(tmp_path, repo_path=repo, artifact_paths={"api": ["built.bin"]})

    assignment_id = "orphaned-no-record"
    wt_dir = server.state_dir / "worktrees" / assignment_id
    subprocess.run(
        ["git", "worktree", "add", "-b", "issue-1295-orphan", str(wt_dir)],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    (wt_dir / "built.bin").write_bytes(b"X" * 4096)
    old = time.time() - 3600
    os.utime(wt_dir, (old, old))

    result = server.clean_worktrees(recent_secs=0)

    assert result["cleaned"] == 1
    assert not wt_dir.exists()
    stash_dir = server.state_dir / "artifacts" / "api" / "issue-1295-orphan"
    assert (stash_dir / "built.bin").exists()


def test_clean_worktrees_orphan_stash_noop_for_plain_directory(
    tmp_path: Path,
) -> None:
    """A plain (non-git) orphaned directory — as created by races/tests —
    must still be removed cleanly; the new orphan-stash attempt degrades
    to a silent no-op (``git rev-parse`` fails) rather than raising and
    aborting the sweep.
    """
    server = _server(tmp_path, artifact_paths={"api": ["*.bin"]})
    orphan = server.state_dir / "worktrees" / "not-a-git-dir"
    orphan.mkdir(parents=True)
    (orphan / "file.txt").write_text("data")
    old = time.time() - 3600
    os.utime(orphan, (old, old))

    result = server.clean_worktrees(recent_secs=0)
    assert result["cleaned"] == 1
    assert not orphan.exists()


def test_stash_against_nonexistent_worktree_creates_no_directory(
    tmp_path: Path,
) -> None:
    """#1295 (current live bug): stashing artifacts against a worktree that
    no longer exists must NOT leave an empty stash directory behind.

    Before the fix, ``stash_artifacts_for_branch`` called
    ``stash_dir.mkdir(parents=True, exist_ok=True)`` before checking whether
    the source worktree existed — so a missed race (worktree removed just
    before the stash step) left ``state_dir/artifacts/<repo>/<branch>/`` as
    a phantom entry that fooled downstream ``.exists()`` checks.
    """
    from coord.agent import stash_artifacts_for_branch  # noqa: PLC0415

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    missing_worktree = tmp_path / "no-such-worktree"
    assert not missing_worktree.exists()

    copied = stash_artifacts_for_branch(
        worktree_path=missing_worktree,
        branch="issue-1295-nothing-here",
        repo_name="myrepo",
        patterns=["target/**/foo"],
        state_dir=state_dir,
    )
    assert copied == 0

    stash_dir = state_dir / "artifacts" / "myrepo" / "issue-1295-nothing-here"
    assert not stash_dir.exists(), (
        f"stash directory should NOT be created when the source worktree is "
        f"missing, but {stash_dir} was left on disk"
    )


def test_artifact_absence_reason_names_worktree_sweep_possibility(
    tmp_path: Path,
) -> None:
    """#1295: when there's no stash and no live worktree, the absence-reason
    string must mention the worktree-sweep-during-live-session possibility
    alongside "already merged" and "never built here".  Previously the
    wording implied only two causes, hiding the exact bug the issue fixes.
    """
    server = _server(tmp_path)
    reason = server.artifact_absence_reason("api", "issue-1295-honest-wording")
    lower = reason.lower()
    # It must at least reference the sweep as one of the possibilities.
    assert (
        "sweep" in lower or "worktree-clean" in lower or "#1295" in reason
    ), reason
    # And still name the pre-existing hypotheses so we haven't regressed.
    assert "merged" in lower or "pruned" in lower, reason


# ── #315: resume_session_id / claude_session_id ────────────────────────────


def test_default_worker_command_resume_flag_absent_by_default() -> None:
    """No --resume flag when resume_session_id is not set."""
    spec = AssignmentSpec(
        repo_name="api",
        repo_path="/tmp/repo",
        issue_number=1,
        issue_title="t",
        briefing="b",
    )
    argv = default_worker_command(spec)
    assert "--resume" not in argv


def test_default_worker_command_resume_flag_present() -> None:
    """--resume <session_id> appended when resume_session_id is set."""
    spec = AssignmentSpec(
        repo_name="api",
        repo_path="/tmp/repo",
        issue_number=1,
        issue_title="t",
        briefing="b",
        resume_session_id="ses-abc123",
    )
    argv = default_worker_command(spec)
    assert "--resume" in argv
    idx = argv.index("--resume")
    assert argv[idx + 1] == "ses-abc123"


# ── #2301: smoke legs must not hold Monitor (or Edit/Write) ────────────────
#
# Root cause: smoke fell through to the generic `else` branch and inherited
# the #2169 `Monitor` grant meant for *work*-shaped legs. `Monitor` is an
# await-a-notification tool — calling it ends the current turn to wait for a
# wake-up condition, which only resumes anything in an INTERACTIVE session.
# Every coord leg, smoke included, is a one-shot `claude -p` session (#1394):
# ending the turn ends the session itself, permanently, before any
# notification can arrive. Two real smoke legs did exactly this — reached
# `Monitor` via ToolSearch to poll a backgrounded `cargo test`, ended their
# turn to "wait" for it, and died 16-24s later with no verdict printed, while
# the backgrounded suite was reaped out from under them ~30s after that.


def _smoke_spec(**overrides) -> AssignmentSpec:
    base = dict(
        repo_name="api",
        repo_path="/tmp/repo",
        issue_number=2301,
        issue_title="[smoke] t",
        briefing="b",
        branch="main",
        type="smoke",
    )
    base.update(overrides)
    return AssignmentSpec(**base)


def test_default_worker_command_smoke_type_does_not_grant_monitor() -> None:
    argv = default_worker_command(_smoke_spec())
    allowed = argv[argv.index("--allowedTools") + 1].split(",")
    assert "Monitor" not in allowed


def test_default_worker_command_smoke_type_gets_read_bash_only() -> None:
    """A smoke leg validates; it never mutates — no Edit/Write either."""
    argv = default_worker_command(_smoke_spec())
    allowed = argv[argv.index("--allowedTools") + 1]
    assert allowed == "Read,Bash"


def test_default_worker_command_smoke_type_uses_smoke_system_prompt() -> None:
    """Confirms the smoke branch's default system prompt is
    coord.smoke.SMOKE_SYSTEM_PROMPT, not the generic WORKER_SYSTEM_PROMPT
    the old fall-through `else` branch would have used."""
    from coord.smoke import SMOKE_SYSTEM_PROMPT

    argv = default_worker_command(_smoke_spec())
    system_prompt = argv[argv.index("--system-prompt") + 1]
    assert system_prompt.startswith(SMOKE_SYSTEM_PROMPT)


def test_default_worker_command_smoke_type_honours_explicit_system_prompt() -> None:
    argv = default_worker_command(_smoke_spec(system_prompt="custom smoke prompt"))
    system_prompt = argv[argv.index("--system-prompt") + 1]
    assert system_prompt.startswith("custom smoke prompt")


# ── #2461: review legs must be read-only ─────────────────────────────────
#
# Root cause: `"review"` wasn't one of the explicit branches in
# `default_worker_command`, so it fell through to the generic `else` and got
# the exact same `Read,Edit,Write,Bash,Monitor` grant as a real work leg —
# REVIEWER_SYSTEM_PROMPT's "you only review, you are NOT allowed to push
# commits or modify the PR's code" was enforced by the prompt text alone,
# with nothing stopping a review worker from calling Edit/Write or shelling
# out to `git push`/`gh` if it decided to.


def _review_spec(**overrides) -> AssignmentSpec:
    base = dict(
        repo_name="api",
        repo_path="/tmp/repo",
        issue_number=2461,
        issue_title="[review] t",
        briefing="b",
        branch="main",
        type="review",
    )
    base.update(overrides)
    return AssignmentSpec(**base)


def test_default_worker_command_review_type_gets_read_bash_only() -> None:
    """A review leg only reads the diff and reports a verdict — no
    Edit/Write (it must not modify the PR's code), and (mirroring the
    `smoke` branch's #1394/#2301 reasoning) no Monitor either — a review leg
    is a one-shot `claude -p` session, so an await-a-notification tool can
    never resume it."""
    argv = default_worker_command(_review_spec())
    allowed = argv[argv.index("--allowedTools") + 1]
    assert allowed == "Read,Bash"


def test_default_worker_command_review_type_uses_reviewer_system_prompt() -> None:
    """Confirms the review branch's default system prompt is
    coord.review.REVIEWER_SYSTEM_PROMPT, not the generic WORKER_SYSTEM_PROMPT
    the old fall-through `else` branch would have used."""
    from coord.review import REVIEWER_SYSTEM_PROMPT

    argv = default_worker_command(_review_spec())
    system_prompt = argv[argv.index("--system-prompt") + 1]
    assert system_prompt.startswith(REVIEWER_SYSTEM_PROMPT)


def test_default_worker_command_review_type_honours_explicit_system_prompt() -> None:
    argv = default_worker_command(_review_spec(system_prompt="custom review prompt"))
    system_prompt = argv[argv.index("--system-prompt") + 1]
    assert system_prompt.startswith("custom review prompt")


def test_default_worker_command_review_type_blocks_mutating_git_and_gh() -> None:
    """The deny list must be wired into the CLI-enforced --disallowedTools,
    not just the soft system-prompt reminder text — a reviewer that ignores
    its own prompt must still be structurally unable to push, commit, or
    touch GitHub. This is the actual "tool scope" enforcement #2461 asks
    for, as opposed to the prompt-only status quo."""
    argv = default_worker_command(_review_spec())
    idx = argv.index("--disallowedTools")
    disallowed = argv[idx + 1].split(",")
    for pattern in REVIEW_DENY_COMMANDS:
        assert pattern in disallowed
    # And Edit/Write themselves are denied by simple omission from
    # --allowedTools (Claude Code only grants tools it's explicitly told to).
    allowed = argv[argv.index("--allowedTools") + 1].split(",")
    assert "Edit" not in allowed
    assert "Write" not in allowed


def test_default_worker_command_review_type_deny_list_also_in_prompt() -> None:
    """Belt-and-braces: the same deny list also lands in the system prompt
    text (build_deny_prompt) so a reasoning model sees an explicit reminder,
    not just a silent tool-call rejection."""
    argv = default_worker_command(_review_spec())
    system_prompt = argv[argv.index("--system-prompt") + 1]
    assert "FORBIDDEN COMMANDS" in system_prompt
    assert "git push *" in system_prompt
    assert "gh *" in system_prompt


def test_assign_wires_the_review_deny_list_into_the_real_dispatch(tmp_path: Path) -> None:
    """End-to-end (mirrors test_assign_wires_the_base_checkout_guard_into_the_real_dispatch
    for #1642): dispatch a real review-type assignment through
    default_worker_command via AgentServer.assign() and confirm the argv the
    agent actually launched carries the hard --disallowedTools guard, not
    just a spec built by hand in isolation."""
    repo = _init_repo(tmp_path / "repo")
    server = AgentServer(
        machine_name="test",
        capabilities=["python"],
        repos=["api"],
        state_dir=tmp_path / "state",
        worker_command=noop_default_worker_command,
        repo_paths={"api": str(repo)},
    )
    spec = _spec(repo, type="review")
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=30)
    assert final.exit_code == 0

    log = Path(final.log_path).read_text()
    header = log.splitlines()[0]
    assert "--disallowedTools" in header
    for pattern in REVIEW_DENY_COMMANDS:
        assert pattern in header
    assert "--allowedTools Read,Bash" in header


# ── #1315: structural sealing enforcement (write-guard) ─────────────────────


def test_sealed_write_guard_tools_empty_when_not_forbidden() -> None:
    assert _sealed_write_guard_tools([]) == []
    assert _sealed_write_guard_tools(["coord/secrets.py"]) == []


def test_sealed_write_guard_tools_blocks_edit_and_write():
    patterns = _sealed_write_guard_tools(["tests/acceptance/"])
    assert "Edit(tests/acceptance/**)" in patterns
    assert "Write(tests/acceptance/**)" in patterns


def test_sealed_write_guard_tools_dedupes():
    patterns = _sealed_write_guard_tools(["tests/acceptance/", "tests/acceptance/"])
    assert patterns.count("Edit(tests/acceptance/**)") == 1
    assert patterns.count("Write(tests/acceptance/**)") == 1


# ── #1642: base-checkout write guard ─────────────────────────────────────────

# #2684: `_base_checkout_write_guard_tools` runs its input through
# `Path(repo_path).expanduser()`, and these tests feed it POSIX-style
# absolute paths (`/home/john/...`) — the only shape a real spec.repo_path
# ever takes today (every documented deployment is Linux/macOS/WSL). On a
# native-Windows `pathlib.Path`, a leading-`/` string is NOT treated as
# absolute-from-root the way it is on POSIX (`str(WindowsPath("/home/john"))`
# renders as `\home\john`, which fails the function's own
# `normalized.startswith("/")` check), so the function harmlessly returns
# `[]` instead of the expected guard patterns. That's a mismatch between the
# test's POSIX-shaped fixture input and the host running it, not a
# production bug reachable from any real deployment — no Windows port yet.
_posix_repo_path_skip = pytest.mark.skipif(
    sys.platform == "win32",
    reason="feeds a POSIX-absolute repo_path into a native pathlib.Path — "
    "POSIX-only fixture shape, no Windows port yet (#2684)",
)

# #2725: a handful of tests point `ProviderDef.binary` (and so
# `ClaudeProvider`/`OpenCodeProvider.build_command`'s `argv[0]`) at a stub
# script and spawn it for REAL through `AgentServer.assign()`, to prove the
# wire-resolved binary path actually reaches the spawned process — not just
# a `build_command()` unit check. Unlike the fake-worker shapes this issue
# fixes elsewhere in this file (`/bin/true`, `/bin/sh -c script`,
# `#!/bin/sh` scripts invoked via `[sys.executable, str(script)]`),
# `argv[0]` here is fixed by *production* code
# (`coord/providers/{claude,opencode}.py`: `argv = [binary, ...]`) to a
# single string with no room to prepend `sys.executable` — so there is no
# portable stand-in reachable from `tests/` alone. A bare `.sh` shebang
# script fails on Windows with `[WinError 193] %1 is not a valid Win32
# application` (the exact failure this issue reports); a `.bat`/`.cmd`
# alternative does NOT help either — `CreateProcess` (what
# `subprocess.Popen` calls without `shell=True`, exactly as
# `AgentServer._spawn` invokes it) does not run batch files directly, only
# `cmd.exe /c` does, so that would fail with the same WinError. Making this
# portable needs a `coord/providers` change (e.g. an interpreter-aware
# binary resolution), which is out of this ticket's `tests/`-only scope —
# noted here rather than silently worked around.
_posix_binary_spawn_skip = pytest.mark.skipif(
    sys.platform == "win32",
    reason="spawns a stub script directly as ProviderDef.binary (argv[0]) — "
    "no portable stand-in without a coord/providers change, no Windows "
    "port yet (#2725)",
)


@pytest.mark.posix_only
@_posix_repo_path_skip
def test_base_checkout_write_guard_tools_blocks_edit_and_write():
    patterns = _base_checkout_write_guard_tools("/home/john/src/api")
    assert "Edit(//home/john/src/api/**)" in patterns
    assert "Write(//home/john/src/api/**)" in patterns


@pytest.mark.posix_only
@_posix_repo_path_skip
def test_base_checkout_write_guard_tools_strips_trailing_slash():
    """A trailing slash on repo_path must not produce a double-slash pattern
    (Edit(//home/john/src/api//**)) that would fail to match anything."""
    patterns = _base_checkout_write_guard_tools("/home/john/src/api/")
    assert "Edit(//home/john/src/api/**)" in patterns
    assert "Write(//home/john/src/api/**)" in patterns


def test_base_checkout_write_guard_tools_empty_for_empty_path():
    assert _base_checkout_write_guard_tools("") == []


def test_base_checkout_write_guard_tools_empty_for_relative_path():
    """The //<abs-path> marker only makes sense for an absolute path — a
    relative repo_path (should never happen in production) must not produce
    a malformed pattern that silently matches nothing."""
    assert _base_checkout_write_guard_tools("relative/repo") == []


@pytest.mark.posix_only
@_posix_repo_path_skip
def test_base_checkout_write_guard_tools_expands_tilde(monkeypatch):
    """Regression for the #1642 fix-review finding: production
    spec.repo_path is the raw, un-expanded string straight from
    coordinator.yml's machines[].repo_paths, and this project's own
    coordinator.example.yml documents that field with tilde-shorthand (e.g.
    ``~/src/claude-coordinator``). dispatch.py sends that raw string over
    the wire unexpanded and nothing downstream normalizes it before it
    reaches this function, so a tilde-form repo_path must still produce a
    real guard rather than silently returning [] (the exact silent-escape
    failure #1642 exists to close)."""
    monkeypatch.setenv("HOME", "/home/john")
    patterns = _base_checkout_write_guard_tools("~/src/claude-coordinator")
    assert "Edit(//home/john/src/claude-coordinator/**)" in patterns
    assert "Write(//home/john/src/claude-coordinator/**)" in patterns


def test_worker_system_prompt_forbids_the_base_checkout():
    """#1642: a haiku-routed worker given a correct worktree still edited
    the shared base checkout by absolute path — the cwd convention was
    never actually stated as a rule. Guard the rule against being silently
    dropped in a future prompt edit (mirrors
    test_smoke_briefing_scopes_its_checkout_to_the_worktree for the smoke
    prompt)."""
    assert "current working directory" in WORKER_SYSTEM_PROMPT
    assert "~/src/<repo>" in WORKER_SYSTEM_PROMPT


def test_mock_author_system_prompt_forbids_the_base_checkout():
    assert "current working directory" in MOCK_AUTHOR_SYSTEM_PROMPT
    assert "~/src/<repo>" in MOCK_AUTHOR_SYSTEM_PROMPT


def test_assign_wires_the_base_checkout_guard_into_the_real_dispatch(tmp_path: Path) -> None:
    """#1642 end-to-end: dispatch a real ``work`` assignment (not a fake
    fixed argv — this uses the real ``default_worker_command`` so the wiring
    from ``AgentServer.assign()`` through ``spec.repo_path`` is exercised for
    real) and assert the argv the agent actually launched carries a
    --disallowedTools guard naming THIS run's base checkout — the assertion
    this bug would have failed, since before #1642 no such guard existed at
    all and a worker's only constraint was its subprocess cwd.

    The base checkout is asserted clean afterwards too: the worker here is
    a portable no-op (``noop_default_worker_command``, #2725 — never touches
    the filesystem), so this also pins that a normal, well-behaved dispatch
    leaves the base checkout untouched.
    """
    repo = _init_repo(tmp_path / "repo")
    server = AgentServer(
        machine_name="test",
        capabilities=["python"],
        repos=["api"],
        state_dir=tmp_path / "state",
        worker_command=noop_default_worker_command,
        repo_paths={"api": str(repo)},
    )
    spec = _spec(repo)
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=30)
    assert final.exit_code == 0

    log = Path(final.log_path).read_text()
    header = log.splitlines()[0]
    assert "--disallowedTools" in header
    for pattern in _base_checkout_write_guard_tools(str(repo)):
        assert pattern in header

    # And the base checkout itself — the shared, non-worktree, non-disposable
    # checkout this guard exists to protect — is untouched.
    status = subprocess.run(
        ["git", "status", "--short"], cwd=str(repo), capture_output=True, text=True, check=True
    )
    assert status.stdout == ""


def test_default_worker_command_omits_sealed_guard_when_not_sealed() -> None:
    """A normal work spec with nothing forbidden gets no sealed-oracle
    --disallowedTools entries — that guard must not fire unconditionally
    for every worker. (#1642's base-checkout guard below DOES fire
    unconditionally for any Edit-capable worker, so --disallowedTools
    itself is still present — see
    test_default_worker_command_blocks_base_checkout_writes.)"""
    spec = AssignmentSpec(
        repo_name="api",
        repo_path="/tmp/repo",
        issue_number=1,
        issue_title="t",
        briefing="b",
        files_forbidden=[],
    )
    argv = default_worker_command(spec)
    assert "--disallowedTools" in argv
    idx = argv.index("--disallowedTools")
    disallowed = argv[idx + 1]
    assert "tests/acceptance/" not in disallowed


@pytest.mark.posix_only
@_posix_repo_path_skip
def test_default_worker_command_blocks_base_checkout_writes() -> None:
    """#1642: a worker given a correct worktree can still construct an
    absolute path back into the shared base checkout (spec.repo_path) and
    edit it directly — worktree isolation was enforced by cwd alone. Any
    spec.type that gets Edit in --allowedTools must get a real
    --disallowedTools guard blocking Edit/Write anywhere under
    spec.repo_path, regardless of files_forbidden."""
    spec = AssignmentSpec(
        repo_name="api",
        repo_path="/tmp/repo",
        issue_number=1,
        issue_title="t",
        briefing="b",
        files_forbidden=[],
    )
    argv = default_worker_command(spec)
    assert "--disallowedTools" in argv
    idx = argv.index("--disallowedTools")
    disallowed = argv[idx + 1]
    assert "Edit(//tmp/repo/**)" in disallowed
    assert "Write(//tmp/repo/**)" in disallowed
    # Read-only spec types (no Edit in --allowedTools) get no guard at all —
    # they can't write anywhere so the flag would be a no-op.
    plan_spec = AssignmentSpec(
        repo_name="api",
        repo_path="/tmp/repo",
        issue_number=1,
        issue_title="t",
        briefing="b",
        type="plan",
    )
    assert "--disallowedTools" not in default_worker_command(plan_spec)


def test_default_worker_command_blocks_sealed_oracle_writes() -> None:
    """#1315: a type="work" spec whose files_forbidden carries the sealed
    oracle prefix (coord/dispatch.py's #944 auto-forbid) gets a real
    --disallowedTools guard, not just advisory prompt text — the worker
    literally cannot Edit/Write under tests/acceptance/** regardless of
    what its briefing says (the gap #1314 hit)."""
    spec = AssignmentSpec(
        repo_name="api",
        repo_path="/tmp/repo",
        issue_number=1120,
        issue_title="t",
        briefing="please fix tests/acceptance/ms-38/contract.md",
        files_forbidden=["tests/acceptance/"],
    )
    argv = default_worker_command(spec)
    assert "--disallowedTools" in argv
    idx = argv.index("--disallowedTools")
    disallowed = argv[idx + 1]
    assert "Edit(tests/acceptance/**)" in disallowed
    assert "Write(tests/acceptance/**)" in disallowed
    # The worker still has Edit/Write in --allowedTools generally — this is
    # a path-scoped refinement, not a wholesale removal of edit capability.
    allowed_idx = argv.index("--allowedTools")
    assert "Edit" in argv[allowed_idx + 1]


@pytest.mark.posix_only
@_posix_repo_path_skip
def test_default_worker_command_mock_author_not_sealed() -> None:
    """mock-author's entire job is writing under tests/acceptance/ — dispatch.py
    never adds it to files_forbidden for that type, so it must get no
    sealed-oracle write guard (mirrors the dispatch-time exemption, not
    re-derived here). It still gets the #1642 base-checkout guard, same as
    every other Edit-capable worker type."""
    spec = AssignmentSpec(
        repo_name="api",
        repo_path="/tmp/repo",
        issue_number=1120,
        issue_title="[gate-a] t",
        briefing="b",
        type="mock-author",
        files_forbidden=[],
    )
    argv = default_worker_command(spec)
    assert "--disallowedTools" in argv
    idx = argv.index("--disallowedTools")
    disallowed = argv[idx + 1]
    assert "tests/acceptance/" not in disallowed
    assert "Edit(//tmp/repo/**)" in disallowed
    assert "Write(//tmp/repo/**)" in disallowed


# ── #1445: worktree-writability preflight ───────────────────────────────────


def test_default_worker_command_uses_setting_sources_user() -> None:
    """A worker must not inherit the host checkout's project/local Claude
    Code settings — see #1445. #2462 briefly tried `--bare` (also closes
    hooks/.mcp.json) but that disables OAuth/keychain auth and broke every
    worker dispatch fleet-wide within the hour on this OAuth-authenticated
    fleet; reverted same-day back to the narrower `--setting-sources user`."""
    spec = AssignmentSpec(
        repo_name="api",
        repo_path="/tmp/repo",
        issue_number=1,
        issue_title="t",
        briefing="b",
    )
    argv = default_worker_command(spec)
    assert "--setting-sources" in argv
    idx = argv.index("--setting-sources")
    assert argv[idx + 1] == "user"
    assert "--bare" not in argv


def test_default_worker_command_uses_setting_sources_user_for_plan_type() -> None:
    """Same restriction applies to every spec.type, not just 'work' — all of
    them are headless dispatches that must not depend on host checkout
    state."""
    spec = AssignmentSpec(
        repo_name="api",
        repo_path="/tmp/repo",
        issue_number=1,
        issue_title="t",
        briefing="b",
        type="plan",
    )
    argv = default_worker_command(spec)
    assert "--setting-sources" in argv
    idx = argv.index("--setting-sources")
    assert argv[idx + 1] == "user"
    assert "--bare" not in argv


def test_default_worker_command_passes_strict_mcp_config() -> None:
    """#2820: a worker must not load the operator's personal user-scope MCP
    servers (Google Drive/Calendar/Gmail) — no worker can ever use them, and
    their presence/absence made the tool surface non-deterministic.
    `--setting-sources user` only gates settings.json, not `.mcp.json` / MCP
    servers, so `--strict-mcp-config` is required as a separate flag."""
    spec = AssignmentSpec(
        repo_name="api",
        repo_path="/tmp/repo",
        issue_number=1,
        issue_title="t",
        briefing="b",
    )
    argv = default_worker_command(spec)
    assert "--strict-mcp-config" in argv


def test_default_worker_command_passes_strict_mcp_config_for_every_type() -> None:
    """Same as above, for every spec.type — a plan/mock-author/smoke/review
    leg has just as little use for the operator's Drive/Calendar/Gmail
    tools as a work leg does."""
    for spec_type in ("plan", "mock-author", "smoke", "review", "work"):
        spec = AssignmentSpec(
            repo_name="api",
            repo_path="/tmp/repo",
            issue_number=1,
            issue_title="t",
            briefing="b",
            type=spec_type,
        )
        argv = default_worker_command(spec)
        assert "--strict-mcp-config" in argv, f"missing for type={spec_type!r}"


def test_default_worker_command_embeds_claude_md_for_work_type(tmp_path: Path) -> None:
    """#2462: added a CLAUDE.md embed into --system-prompt for work-shaped
    legs (originally to compensate for `--bare` disabling ambient
    auto-discovery; kept as defense-in-depth after the `--bare` emergency
    revert), mirroring the review leg's read_repo_claude_md defensive read
    (coord/review.py)."""
    (tmp_path / "CLAUDE.md").write_text("# Project rules\n\nAlways use tabs.\n")
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(tmp_path),
        issue_number=1,
        issue_title="t",
        briefing="b",
    )
    argv = default_worker_command(spec)
    idx = argv.index("--system-prompt")
    system_prompt = argv[idx + 1]
    assert "Always use tabs." in system_prompt
    assert "Project rules (from CLAUDE.md)" in system_prompt


def test_default_worker_command_embeds_claude_md_for_plan_type(tmp_path: Path) -> None:
    """Same defensive read applies to type='plan', which gets its own
    branch in default_worker_command rather than falling into the
    catch-all 'work' else-branch."""
    (tmp_path / "CLAUDE.md").write_text("# Project rules\n\nNo emoji.\n")
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(tmp_path),
        issue_number=1,
        issue_title="t",
        briefing="b",
        type="plan",
    )
    argv = default_worker_command(spec)
    idx = argv.index("--system-prompt")
    assert "No emoji." in argv[idx + 1]


def test_default_worker_command_embeds_claude_md_for_mock_author_type(tmp_path: Path) -> None:
    """Same defensive read applies to type='mock-author'."""
    (tmp_path / "CLAUDE.md").write_text("# Project rules\n\nGate A first.\n")
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(tmp_path),
        issue_number=1,
        issue_title="[gate-a] t",
        briefing="b",
        type="mock-author",
    )
    argv = default_worker_command(spec)
    idx = argv.index("--system-prompt")
    assert "Gate A first." in argv[idx + 1]


def test_default_worker_command_no_claude_md_is_a_noop(tmp_path: Path) -> None:
    """A repo with no CLAUDE.md must not blow up or inject an empty section
    header into the system prompt."""
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(tmp_path),
        issue_number=1,
        issue_title="t",
        briefing="b",
    )
    argv = default_worker_command(spec)
    idx = argv.index("--system-prompt")
    assert "Project rules (from CLAUDE.md)" not in argv[idx + 1]


def test_deny_pattern_blocks_path_matches_blanket_prefix() -> None:
    from coord.agent import _deny_pattern_blocks_path

    # The exact shape from the #1445 incident report.
    pattern = "Write(//home/john/.coord/**)"
    assert _deny_pattern_blocks_path(
        pattern, Path("/home/john/.coord/worktrees/a1860bb9f9f8")
    )
    assert _deny_pattern_blocks_path(pattern, Path("/home/john/.coord"))


def test_deny_pattern_blocks_path_does_not_match_unrelated_path() -> None:
    from coord.agent import _deny_pattern_blocks_path

    pattern = "Write(//home/john/.coord/**)"
    assert not _deny_pattern_blocks_path(pattern, Path("/home/john/src/other-repo"))


def test_deny_pattern_blocks_path_ignores_non_absolute_patterns() -> None:
    from coord.agent import _deny_pattern_blocks_path

    # Relative/tool-bare patterns aren't the shape this check targets —
    # must not raise, must not false-positive.
    assert not _deny_pattern_blocks_path("Bash(git push --force *)", Path("/x"))
    assert not _deny_pattern_blocks_path("Edit(src/**)", Path("/x"))


def test_find_blocking_deny_rule_detects_blanket_deny(tmp_path: Path) -> None:
    from coord.agent import find_blocking_deny_rule

    worktree = tmp_path / "coord" / "worktrees" / "abc123"
    settings = tmp_path / "settings.json"
    coord_root_nolead = str(tmp_path / "coord")[1:]  # strip leading "/"
    settings.write_text(json.dumps({
        "permissions": {"deny": [f"Write(//{coord_root_nolead}/**)", "Edit(//" + coord_root_nolead + "/**)"]}
    }))

    result = find_blocking_deny_rule(worktree, settings_files=[settings])
    assert result is not None
    assert "Write" in result
    assert str(settings) in result


def test_find_blocking_deny_rule_clean_when_no_match(tmp_path: Path) -> None:
    from coord.agent import find_blocking_deny_rule

    worktree = tmp_path / "coord" / "worktrees" / "abc123"
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"permissions": {"deny": ["Bash(rm -rf *)"]}}))

    assert find_blocking_deny_rule(worktree, settings_files=[settings]) is None


def test_find_blocking_deny_rule_tolerates_missing_or_bad_settings_file(tmp_path: Path) -> None:
    from coord.agent import find_blocking_deny_rule

    worktree = tmp_path / "coord" / "worktrees" / "abc123"
    missing = tmp_path / "does-not-exist.json"
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("not json{{{")

    assert find_blocking_deny_rule(worktree, settings_files=[missing, bad_json]) is None


def test_check_worktree_writable_clean(tmp_path: Path) -> None:
    from coord.agent import check_worktree_writable

    worktree = tmp_path / "worktrees" / "abc123"
    assert check_worktree_writable(worktree, settings_files=[]) is None
    # The probe file is cleaned up, not left behind.
    assert list(worktree.iterdir()) == []


def test_check_worktree_writable_detects_os_level_failure(tmp_path: Path) -> None:
    """A parent path component that is a plain file (not a directory) makes
    mkdir(parents=True) fail with a deterministic OSError regardless of
    the test runner's uid (avoids relying on chmod, which root ignores)."""
    from coord.agent import check_worktree_writable

    blocker = tmp_path / "not-a-directory"
    blocker.write_text("i am a file")
    worktree = blocker / "worktrees" / "abc123"

    result = check_worktree_writable(worktree, settings_files=[])
    assert result is not None
    assert "cannot write" in result


def test_check_worktree_writable_detects_deny_rule(tmp_path: Path) -> None:
    """Reproduces the #1445 incident: the OS-level probe succeeds (the
    worktree directory itself is perfectly writable) but a deny rule
    blanketing the worktree's ancestor is present in the scanned settings
    file — this must still be reported, not silently missed."""
    from coord.agent import check_worktree_writable

    worktree = tmp_path / "coord" / "worktrees" / "abc123"
    settings = tmp_path / "settings.json"
    coord_root_nolead = str(tmp_path / "coord")[1:]
    settings.write_text(json.dumps({
        "permissions": {"deny": [f"Write(//{coord_root_nolead}/**)"]}
    }))

    result = check_worktree_writable(worktree, settings_files=[settings])
    assert result is not None
    assert str(worktree) in result
    assert "permission rule" in result
    # The directory creation half of the probe still ran fine — confirms
    # this is the deny-rule branch, not the OS-error branch.
    assert worktree.exists()


def test_assign_refuses_dispatch_when_worktree_not_writable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#1445 acceptance: AgentServer.assign() must refuse to spawn a worker
    into a worktree the preflight probe reports as blocked — failing fast
    at dispatch time (assignment.status == FAILED with a message naming the
    path/rule) instead of burning a full session that discovers this at the
    very end."""
    import coord.agent as agent_module

    repo = _init_repo(tmp_path / "repo")
    server = _server(tmp_path, repo_path=repo)

    def _fake_check(worktree_path: Path, **kwargs) -> str | None:
        return f"a Claude Code permission rule denies Edit/Write under {worktree_path}: 'Write(//home/john/.coord/**)' in /home/john/.claude/settings.json"

    monkeypatch.setattr(agent_module, "check_worktree_writable", _fake_check)

    a = server.assign(_spec(repo))
    assert a.status == FAILED
    assert a.error is not None
    assert "not writable" in a.error
    assert "permission rule" in a.error
    # No worker subprocess should have been spawned for this assignment.
    assert a.id not in server._processes
    server.shutdown()


def test_assign_cleans_up_worktree_when_not_writable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#1445 review finding 1: a dispatch refused by the writability preflight
    must not leak the git worktree + branch `_setup_worktree()` already
    created before the check ran — every retry on a genuinely-blocked machine
    would otherwise accumulate an abandoned worktree per attempt, undercutting
    the whole point of a cheap preflight (and making a full-disk condition,
    one of the OS-level failures this check catches, actively worse)."""
    import coord.agent as agent_module

    repo = _init_repo(tmp_path / "repo")
    server = _server(tmp_path, repo_path=repo)

    def _fake_check(worktree_path: Path, **kwargs) -> str | None:
        return f"a Claude Code permission rule denies Edit/Write under {worktree_path}: 'Write(//home/john/.coord/**)' in /home/john/.claude/settings.json"

    monkeypatch.setattr(agent_module, "check_worktree_writable", _fake_check)

    a = server.assign(_spec(repo))
    assert a.status == FAILED
    assert a.worktree_path is not None
    wt_path = Path(a.worktree_path)
    assert not wt_path.exists(), "leaked worktree directory after refused dispatch"

    # The branch must also be freed at the git level (no stale admin entry) —
    # otherwise the *next* dispatch attempt on this same branch fails with a
    # worktree-collision error instead of hitting the same writability refusal.
    listing = _git(repo, "worktree", "list", "--porcelain")
    assert str(wt_path) not in listing
    server.shutdown()


def test_assign_worktree_writable_settings_files_override(tmp_path: Path) -> None:
    """#1445 review finding 2: AgentServer accepts an explicit
    `worktree_writable_settings_files` override (mirroring the existing
    `worker_command` injection seam), so a test wanting to exercise the real
    deny-rule scan doesn't have to touch — or depend on — the real
    `~/.claude/settings.json` of whatever machine runs the suite."""
    repo = _init_repo(tmp_path / "repo")
    settings = tmp_path / "fake-settings.json"
    # `_server()` puts worktrees under `<tmp_path>/state/worktrees/<aid>` —
    # deny the whole `state/` subtree so it matches regardless of the
    # per-assignment uuid.
    state_root_nolead = str(tmp_path / "state")[1:]
    settings.write_text(json.dumps({
        "permissions": {"deny": [f"Write(//{state_root_nolead}/**)"]}
    }))

    server = _server(
        tmp_path,
        repo_path=repo,
        worktree_writable_settings_files=[settings],
    )
    a = server.assign(_spec(repo))
    assert a.status == FAILED
    assert a.error is not None
    assert "permission rule" in a.error
    server.shutdown()


def test_reap_captures_claude_session_id(tmp_path: Path) -> None:
    """_reap populates AgentAssignment.claude_session_id from a system.init log line."""
    repo = _init_repo(tmp_path / "repo")
    session_id = "ses-xyz-test"

    # Worker emits a stream-json system.init line with the session_id then exits.
    init_line = json.dumps({
        "type": "system",
        "subtype": "init",
        "session_id": session_id,
        "apiKeySource": "test",
    })
    worker_py = f"print({init_line!r})"
    server = AgentServer(
        machine_name="test",
        repos=["api"],
        repo_paths={"api": str(repo)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: [sys.executable, "-c", worker_py],
    )

    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo),
        issue_number=42,
        issue_title="t",
        briefing="b",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)
    # Worker makes no commits → advisory (#448)
    assert final.status == ADVISORY
    assert final.claude_session_id == session_id

    # Also visible in the /status serialisation (to_dict)
    status = server.list_assignments()
    completed = status["completed"]
    assert any(c["claude_session_id"] == session_id for c in completed)
    server.shutdown()


def test_reap_logs_graphify_invocation_count(tmp_path: Path) -> None:
    """#2212: `_reap` writes a plain `graphify_invocations=N` counter into
    the worker's log alongside the existing "# reap: done" line — the
    measurable half of the graph-first navigation rule in
    WORKER_SYSTEM_PROMPT. A worker whose transcript shows one `graphify
    query ...` Bash call and one unrelated Bash call must be counted 1, not
    2 or 0, end-to-end through the real reap path (not just the
    `_count_graphify_invocations` unit)."""
    repo = _init_repo(tmp_path / "repo")

    graphify_call = json.dumps({
        "type": "assistant",
        "message": {
            "model": "claude-sonnet-4-6",
            "content": [{
                "type": "tool_use",
                "name": "Bash",
                "id": "tu_1",
                "input": {"command": 'graphify query "where is X handled"'},
            }],
        },
    })
    unrelated_call = json.dumps({
        "type": "assistant",
        "message": {
            "model": "claude-sonnet-4-6",
            "content": [{
                "type": "tool_use",
                "name": "Bash",
                "id": "tu_2",
                "input": {"command": "grep -rn foo ."},
            }],
        },
    })
    result_line = json.dumps({"type": "result", "subtype": "success", "is_error": False})
    worker_py = "; ".join(
        f"print({line!r})" for line in (graphify_call, unrelated_call, result_line)
    )
    server = AgentServer(
        machine_name="test",
        repos=["api"],
        repo_paths={"api": str(repo)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: [sys.executable, "-c", worker_py],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo),
        issue_number=2212,
        issue_title="t",
        briefing="b",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    log_text = Path(final.log_path).read_text()
    assert "graphify_invocations=1" in log_text
    server.shutdown()


def test_reap_logs_graphify_query_outcome(tmp_path: Path) -> None:
    """#2236: the count alone cannot separate "queried and got a useful
    answer" from "queried, got nothing, fell back to grep" — opposite fixes.
    `_reap` must therefore also write one `# graphify_query:` line per call,
    carrying the outcome, the result count and the command text, plus a
    `graph_present=` flag on the reap line so a leg with no graph to query
    (the prompt's own escape hatch, which fires silently) is distinguishable
    from a leg that had one and never asked."""
    repo = _init_repo(tmp_path / "repo")

    hit_call = json.dumps({
        "type": "assistant",
        "message": {
            "model": "claude-sonnet-4-6",
            "content": [{
                "type": "tool_use",
                "name": "Bash",
                "id": "tu_hit",
                "input": {"command": 'graphify query "where is X handled"'},
            }],
        },
    })
    hit_result = json.dumps({
        "type": "user",
        "message": {
            "content": [{
                "type": "tool_result",
                "tool_use_id": "tu_hit",
                # Single line on purpose: the fake worker prints these one
                # per line, so an embedded `\n` would split the JSON.
                "content": "Traversal: BFS depth=2 | Start: [x] | 7 nodes found",
            }],
        },
    })
    empty_call = json.dumps({
        "type": "assistant",
        "message": {
            "model": "claude-sonnet-4-6",
            "content": [{
                "type": "tool_use",
                "name": "Bash",
                "id": "tu_empty",
                "input": {"command": "graphify query nothing-here"},
            }],
        },
    })
    empty_result = json.dumps({
        "type": "user",
        "message": {
            "content": [{
                "type": "tool_result",
                "tool_use_id": "tu_empty",
                "content": "",
            }],
        },
    })
    result_line = json.dumps({"type": "result", "subtype": "success", "is_error": False})
    worker_py = "; ".join(
        f"print({line!r})"
        for line in (hit_call, hit_result, empty_call, empty_result, result_line)
    )

    server = AgentServer(
        machine_name="test",
        repos=["api"],
        repo_paths={"api": str(repo)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: [sys.executable, "-c", worker_py],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo),
        issue_number=2236,
        issue_title="t",
        briefing="b",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    log_text = Path(final.log_path).read_text()
    assert "graphify_invocations=2" in log_text
    # No graph was ever built in this throwaway repo, so the worktree has none
    # — exactly the coord-portal/stick-demo shape #2236 is about.
    assert "graph_present=0" in log_text
    assert "# graphify_query: outcome=hit results=7 cmd=" in log_text
    assert "# graphify_query: outcome=empty results=0 cmd=" in log_text
    assert "where is X handled" in log_text
    server.shutdown()


def test_worktree_graph_present_follows_symlinks(tmp_path: Path) -> None:
    """#2236: a linked worktree borrows the base checkout's graph by symlink,
    so `graph_present` must follow links — and a DANGLING link is as
    graph-blind as no file at all, so it must read as absent."""
    from coord.agent import _worktree_graph_present

    assert _worktree_graph_present(None) is False
    assert _worktree_graph_present(str(tmp_path / "nope")) is False

    # Bare worktree with an empty graphify-out/ (the coord-portal case).
    blind = tmp_path / "blind"
    (blind / "graphify-out").mkdir(parents=True)
    assert _worktree_graph_present(str(blind)) is False

    base = tmp_path / "base" / "graphify-out"
    base.mkdir(parents=True)
    (base / "graph.json").write_text("{}")

    linked = tmp_path / "linked"
    (linked / "graphify-out").mkdir(parents=True)
    (linked / "graphify-out" / "graph.json").symlink_to(base / "graph.json")
    assert _worktree_graph_present(str(linked)) is True

    dangling = tmp_path / "dangling"
    (dangling / "graphify-out").mkdir(parents=True)
    (dangling / "graphify-out" / "graph.json").symlink_to(tmp_path / "gone.json")
    assert _worktree_graph_present(str(dangling)) is False


def test_worktree_graph_present_falls_back_to_repo_path_for_legacy_assignments(
    tmp_path: Path,
) -> None:
    """#2236 review: a legacy/non-worktree assignment has no `worktree_path`
    at all, but its worker still ran against `spec.repo_path` directly — which
    may have a fully built graph. Mirrors the branch-capture fallback a few
    hundred lines up in `_reap` ("for legacy assignments (no worktree_path) we
    fall back to the main repo clone")."""
    from coord.agent import _worktree_graph_present

    repo = tmp_path / "main_checkout" / "graphify-out"
    repo.mkdir(parents=True)
    (repo / "graph.json").write_text("{}")
    repo_path = str(tmp_path / "main_checkout")

    # No worktree_path at all: falls back to repo_path_fallback.
    assert _worktree_graph_present(None, repo_path) is True
    # No worktree_path and no graph at the fallback either.
    assert _worktree_graph_present(None, str(tmp_path / "no_graph_here")) is False
    # A real worktree_path still takes priority over the fallback.
    blind_worktree = tmp_path / "blind_worktree" / "graphify-out"
    blind_worktree.mkdir(parents=True)
    assert _worktree_graph_present(str(blind_worktree.parent), repo_path) is False


def test_assignment_spec_accepts_resume_session_id() -> None:
    """AssignmentSpec round-trips resume_session_id through to_dict / from dict."""
    spec = AssignmentSpec(
        repo_name="api",
        repo_path="/tmp/repo",
        issue_number=1,
        issue_title="t",
        briefing="b",
        resume_session_id="ses-resume",
    )
    assert spec.resume_session_id == "ses-resume"


def test_claude_session_id_survives_persist_load(tmp_path: Path) -> None:
    """claude_session_id round-trips through the agent state JSON."""
    from dataclasses import asdict
    from coord.agent import AgentAssignment

    a = AgentAssignment(
        id="test-123",
        spec=AssignmentSpec(
            repo_name="api",
            repo_path="/tmp",
            issue_number=1,
            issue_title="t",
            briefing="b",
        ),
        claude_session_id="ses-persist",
    )
    d = a.to_dict()
    assert d["claude_session_id"] == "ses-persist"

    # Reconstruct from dict (mirrors _load_state logic).
    spec_data = d.pop("spec")
    spec = AssignmentSpec(**spec_data)
    a2 = AgentAssignment(spec=spec, **d)
    assert a2.claude_session_id == "ses-persist"


# ── Artifact stash (#305) ───────────────────────────────────────────────────


def _make_done_assignment(
    tmp_path: Path,
    *,
    repo_name: str = "api",
    branch: str = "issue-1-my-feature",
) -> tuple[AgentServer, AgentAssignment, Path]:
    """Create a server + a fake DONE assignment with a real worktree directory."""
    from coord.agent import DONE, AgentAssignment, AgentServer, AssignmentSpec

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    # Fake worktree with some files
    wt_path = state_dir / "worktrees" / "asgn-abc123"
    wt_path.mkdir(parents=True, exist_ok=True)

    server = AgentServer(
        machine_name="test",
        repos=[repo_name],
        state_dir=state_dir,
        worker_command=lambda spec: [sys.executable, "-c", "print('ok')"],
        repo_paths={repo_name: str(tmp_path / "repo")},
        artifact_paths={repo_name: ["target/debug/mybinary*", "*.d"]},
    )

    spec = AssignmentSpec(
        repo_name=repo_name,
        repo_path=str(tmp_path / "repo"),
        issue_number=1,
        issue_title="my feature",
        briefing="b",
        branch="main",
    )
    a = AgentAssignment(id="asgn-abc123", spec=spec, status=DONE, branch=branch)
    a.worktree_path = str(wt_path)

    return server, a, wt_path


def test_stash_artifacts_copies_matching_files(tmp_path: Path) -> None:
    """Matching files over 100B should be copied to the stash dir."""
    server, a, wt_path = _make_done_assignment(tmp_path)

    # Create a file that matches the glob and is large enough
    target_dir = wt_path / "target" / "debug"
    target_dir.mkdir(parents=True)
    bin_file = target_dir / "mybinary"
    bin_file.write_bytes(b"\x7fELF" + b"\x00" * 200)  # fake ELF, 204 bytes

    server._stash_artifacts(a)

    stash_dir = server.state_dir / "artifacts" / "api" / "issue-1-my-feature"
    assert (stash_dir / "mybinary").exists(), "binary not copied to stash"
    assert (stash_dir / ".assignment_id").read_text() == "asgn-abc123"


def test_stash_artifacts_skips_small_files(tmp_path: Path) -> None:
    """Files under 100 bytes should be skipped (not real binaries)."""
    server, a, wt_path = _make_done_assignment(tmp_path)

    target_dir = wt_path / "target" / "debug"
    target_dir.mkdir(parents=True)
    tiny = target_dir / "mybinary"
    tiny.write_bytes(b"hi")  # only 2 bytes

    server._stash_artifacts(a)

    stash_dir = server.state_dir / "artifacts" / "api" / "issue-1-my-feature"
    assert not (stash_dir / "mybinary").exists(), "tiny file should have been skipped"


def test_stash_artifacts_skips_dot_d_files(tmp_path: Path) -> None:
    """.d suffix files (compiler dependency files) should always be skipped."""
    server, a, wt_path = _make_done_assignment(tmp_path)

    target_dir = wt_path / "target" / "debug"
    target_dir.mkdir(parents=True)
    dep_file = target_dir / "mybinary.d"
    dep_file.write_bytes(b"dep " + b"x" * 200)  # large enough but .d suffix

    server._stash_artifacts(a)

    stash_dir = server.state_dir / "artifacts" / "api" / "issue-1-my-feature"
    assert not (stash_dir / "mybinary.d").exists(), ".d file should have been skipped"


def test_stash_artifacts_noop_for_failed_assignment(tmp_path: Path) -> None:
    """FAILED assignments should not trigger any stash activity."""
    from coord.agent import FAILED

    server, a, wt_path = _make_done_assignment(tmp_path)
    a.status = FAILED

    target_dir = wt_path / "target" / "debug"
    target_dir.mkdir(parents=True)
    (target_dir / "mybinary").write_bytes(b"\x7fELF" + b"\x00" * 200)

    server._stash_artifacts(a)

    stash_dir = server.state_dir / "artifacts" / "api" / "issue-1-my-feature"
    assert not stash_dir.exists(), "stash dir should not have been created for FAILED"


def test_stash_artifacts_noop_when_no_patterns(tmp_path: Path) -> None:
    """No-op when the repo has no artifact_paths configured."""
    from coord.agent import DONE, AgentAssignment, AgentServer, AssignmentSpec

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    wt_path = state_dir / "worktrees" / "asgn-xyz"
    wt_path.mkdir(parents=True)

    server = AgentServer(
        machine_name="test",
        repos=["api"],
        state_dir=state_dir,
        worker_command=lambda spec: [sys.executable, "-c", "print('ok')"],
        repo_paths={"api": str(tmp_path / "repo")},
        artifact_paths={},  # empty
    )

    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(tmp_path / "repo"),
        issue_number=1,
        issue_title="t",
        briefing="b",
        branch="main",
    )
    a = AgentAssignment(id="asgn-xyz", spec=spec, status=DONE, branch="issue-1-t")
    a.worktree_path = str(wt_path)

    server._stash_artifacts(a)

    stash_base = server.state_dir / "artifacts"
    assert not stash_base.exists(), "no stash dir should be created when no patterns"


def test_stash_artifacts_prefers_spec_over_server_config(tmp_path: Path) -> None:
    """_stash_artifacts should prefer spec's artifact_paths over server
    self.artifact_paths (the local-dev config fallback).  #305."""
    from coord.agent import DONE, AgentAssignment, AgentServer, AssignmentSpec

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    wt_path = state_dir / "worktrees" / "asgn-spec-override"
    wt_path.mkdir(parents=True, exist_ok=True)

    # Server has self.artifact_paths configured (local-dev case)
    server = AgentServer(
        machine_name="test",
        repos=["api"],
        state_dir=state_dir,
        worker_command=lambda spec: [sys.executable, "-c", "print('ok')"],
        repo_paths={"api": str(tmp_path / "repo")},
        artifact_paths={"api": ["old_pattern/*.txt"]},  # Server's fallback config
    )

    # But the spec has its own artifact_paths (from coordinator dispatch)
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(tmp_path / "repo"),
        issue_number=1,
        issue_title="t",
        briefing="b",
        branch="main",
        artifact_paths=["new_pattern/*.bin"],  # Spec overrides server config
    )
    a = AgentAssignment(id="asgn-spec-override", spec=spec, status=DONE, branch="issue-1-t")
    a.worktree_path = str(wt_path)

    # Create a file that matches the SPEC's pattern (not server's pattern)
    target_dir = wt_path / "new_pattern"
    target_dir.mkdir(parents=True)
    bin_file = target_dir / "test.bin"
    bin_file.write_bytes(b"\x00" * 200)

    server._stash_artifacts(a)

    # Should copy the file matched by spec's pattern
    stash_dir = server.state_dir / "artifacts" / "api" / "issue-1-t"
    assert (stash_dir / "test.bin").exists(), (
        "spec's artifact_paths should be used, not server's self.artifact_paths"
    )


def test_stash_artifacts_falls_back_to_server_config_when_spec_empty(
    tmp_path: Path,
) -> None:
    """_stash_artifacts should fall back to server self.artifact_paths when
    spec's artifact_paths is empty.  #305: local-dev backward compat."""
    from coord.agent import DONE, AgentAssignment, AgentServer, AssignmentSpec

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    wt_path = state_dir / "worktrees" / "asgn-fallback"
    wt_path.mkdir(parents=True, exist_ok=True)

    # Server has self.artifact_paths configured
    server = AgentServer(
        machine_name="test",
        repos=["api"],
        state_dir=state_dir,
        worker_command=lambda spec: [sys.executable, "-c", "print('ok')"],
        repo_paths={"api": str(tmp_path / "repo")},
        artifact_paths={"api": ["fallback_pattern/*.txt"]},
    )

    # Spec has empty artifact_paths (old dispatch or local-dev)
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(tmp_path / "repo"),
        issue_number=2,
        issue_title="t2",
        briefing="b",
        branch="main",
        artifact_paths=[],  # Empty: should fall back to server config
    )
    a = AgentAssignment(id="asgn-fallback", spec=spec, status=DONE, branch="issue-2-t2")
    a.worktree_path = str(wt_path)

    # Create a file that matches the SERVER's fallback pattern
    fallback_dir = wt_path / "fallback_pattern"
    fallback_dir.mkdir(parents=True)
    txt_file = fallback_dir / "data.txt"
    txt_file.write_bytes(b"x" * 200)

    server._stash_artifacts(a)

    # Should copy the file matched by server's fallback pattern
    stash_dir = server.state_dir / "artifacts" / "api" / "issue-2-t2"
    assert (stash_dir / "data.txt").exists(), (
        "should fall back to server's self.artifact_paths when spec is empty"
    )


def test_stash_artifacts_skips_build_intermediates_and_dedupes_hash_copies(
    tmp_path: Path,
) -> None:
    """#436: object files, rlibs, rmeta, and hash-stamped duplicate binaries
    must be excluded from the stash.  Only the canonical binary survives."""
    from coord.agent import DONE, AgentAssignment, AgentServer, AssignmentSpec

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    wt_path = state_dir / "worktrees" / "asgn-436"
    wt_path.mkdir(parents=True, exist_ok=True)

    examples_dir = wt_path / "target" / "debug" / "examples"
    examples_dir.mkdir(parents=True)

    payload = b"\x7fELF" + b"\x00" * 200  # fake ELF, 204 bytes

    # Canonical binary — should be kept
    (examples_dir / "tui_app").write_bytes(payload)
    # Hash-stamped duplicate — should be skipped (canonical sibling present)
    (examples_dir / "tui_app-abcdef0123456789").write_bytes(payload)
    # Incremental-codegen object — should be skipped (.o suffix)
    (examples_dir / "tui_app-abc123.rcgu.o").write_bytes(payload)
    # Compiler dependency file — should be skipped (.d suffix)
    (examples_dir / "tui_app.d").write_bytes(payload)
    # Tiny file — should be skipped (< 100 bytes)
    (examples_dir / "tui_app-tiny").write_bytes(b"hi")

    server = AgentServer(
        machine_name="test",
        repos=["quadraui"],
        state_dir=state_dir,
        worker_command=lambda spec: [sys.executable, "-c", "print('ok')"],
        repo_paths={"quadraui": str(tmp_path / "repo")},
        artifact_paths={"quadraui": ["target/debug/examples/tui_*"]},
    )
    spec = AssignmentSpec(
        repo_name="quadraui",
        repo_path=str(tmp_path / "repo"),
        issue_number=436,
        issue_title="artifact stash junk",
        briefing="b",
        branch="main",
    )
    a = AgentAssignment(id="asgn-436", spec=spec, status=DONE, branch="issue-436-fix")
    a.worktree_path = str(wt_path)

    server._stash_artifacts(a)

    stash_dir = state_dir / "artifacts" / "quadraui" / "issue-436-fix"
    stashed = {p.name for p in stash_dir.iterdir() if not p.name.startswith(".")}

    assert stashed == {"tui_app"}, (
        f"expected only the canonical binary; got {stashed!r}"
    )


def test_stash_artifacts_keeps_lone_hash_suffixed_binary(tmp_path: Path) -> None:
    """#436: when ONLY the hash-stamped form exists (no canonical sibling),
    it must be kept — never silently drop the only copy of a binary."""
    from coord.agent import DONE, AgentAssignment, AgentServer, AssignmentSpec

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    wt_path = state_dir / "worktrees" / "asgn-436b"
    wt_path.mkdir(parents=True, exist_ok=True)

    examples_dir = wt_path / "target" / "debug" / "examples"
    examples_dir.mkdir(parents=True)

    payload = b"\x7fELF" + b"\x00" * 200

    # Only the hash-stamped form exists — no canonical sibling
    (examples_dir / "tui_app-abcdef0123456789").write_bytes(payload)

    server = AgentServer(
        machine_name="test",
        repos=["quadraui"],
        state_dir=state_dir,
        worker_command=lambda spec: [sys.executable, "-c", "print('ok')"],
        repo_paths={"quadraui": str(tmp_path / "repo")},
        artifact_paths={"quadraui": ["target/debug/examples/tui_*"]},
    )
    spec = AssignmentSpec(
        repo_name="quadraui",
        repo_path=str(tmp_path / "repo"),
        issue_number=436,
        issue_title="lone hash binary",
        briefing="b",
        branch="main",
    )
    a = AgentAssignment(id="asgn-436b", spec=spec, status=DONE, branch="issue-436b-fix")
    a.worktree_path = str(wt_path)

    server._stash_artifacts(a)

    stash_dir = state_dir / "artifacts" / "quadraui" / "issue-436b-fix"
    stashed = {p.name for p in stash_dir.iterdir() if not p.name.startswith(".")}

    assert "tui_app-abcdef0123456789" in stashed, (
        "lone hash-stamped binary must be kept when no canonical sibling exists"
    )


# ── #982: narrow_artifact_paths unit tests ───────────────────────────────────

def test_narrow_artifact_paths_replaces_glob_with_matching_name() -> None:
    """Smoke test names a specific example → glob is replaced with that path."""
    from coord.agent import narrow_artifact_paths

    result = narrow_artifact_paths(
        ["target/debug/examples/tui_*"],
        ["tui_submenu — run it — menu should appear"],
    )
    assert result == ["target/debug/examples/tui_submenu"]


def test_narrow_artifact_paths_multiple_examples_in_one_bullet() -> None:
    """Two example names in the same bullet → both specific paths in result."""
    from coord.agent import narrow_artifact_paths

    result = narrow_artifact_paths(
        ["target/debug/examples/tui_*", "target/debug/examples/gtk_*"],
        ["tui_submenu and gtk_scrollbar — run them — should render"],
    )
    assert sorted(result) == sorted([
        "target/debug/examples/tui_submenu",
        "target/debug/examples/gtk_scrollbar",
    ])


def test_narrow_artifact_paths_fallback_when_no_smoke_tests_none() -> None:
    """smoke_tests=None → original artifact_paths returned unchanged."""
    from coord.agent import narrow_artifact_paths

    paths = ["target/debug/examples/tui_*", "target/debug/coord-tui"]
    result = narrow_artifact_paths(paths, None)
    assert result == paths


def test_narrow_artifact_paths_fallback_when_smoke_tests_empty_list() -> None:
    """smoke_tests=[] (internal change) → original list returned unchanged."""
    from coord.agent import narrow_artifact_paths

    paths = ["target/debug/examples/tui_*"]
    result = narrow_artifact_paths(paths, [])
    assert result == paths


def test_narrow_artifact_paths_fallback_when_no_name_matches_glob() -> None:
    """No candidate name matches the glob → return original list unchanged."""
    from coord.agent import narrow_artifact_paths

    # Words like "run", "the", "tests", "check" don't match "tui_*"
    result = narrow_artifact_paths(
        ["target/debug/examples/tui_*"],
        ["run the tests and check output carefully"],
    )
    assert result == ["target/debug/examples/tui_*"]


def test_narrow_artifact_paths_preserves_literal_paths() -> None:
    """Literal (non-glob) paths are always preserved unchanged."""
    from coord.agent import narrow_artifact_paths

    result = narrow_artifact_paths(
        ["target/debug/examples/tui_*", "target/debug/coord-tui"],
        ["tui_submenu — run it — check submenu"],
    )
    assert "target/debug/examples/tui_submenu" in result
    assert "target/debug/coord-tui" in result
    assert "target/debug/examples/tui_*" not in result


def test_narrow_artifact_paths_unmatched_glob_kept_unchanged() -> None:
    """A glob with no matching candidates is left in the list unchanged."""
    from coord.agent import narrow_artifact_paths

    # Only tui_* has a match; gtk_* has none — gtk glob stays
    result = narrow_artifact_paths(
        ["target/debug/examples/tui_*", "target/debug/examples/gtk_*"],
        ["tui_submenu — run it — check menu"],
    )
    assert "target/debug/examples/tui_submenu" in result
    # glob narrowed
    assert "target/debug/examples/tui_*" not in result
    # unmatched glob kept (no gtk name in smoke tests)
    assert "target/debug/examples/gtk_*" in result


def test_narrow_artifact_paths_no_glob_in_list_returns_unchanged() -> None:
    """When artifact_paths contains no globs, return list unchanged."""
    from coord.agent import narrow_artifact_paths

    paths = ["target/debug/coord-tui", "target/debug/mybinary"]
    result = narrow_artifact_paths(paths, ["tui_submenu — run — check"])
    assert result == paths


def test_narrow_artifact_paths_empty_artifact_paths_returns_empty() -> None:
    """Empty artifact_paths → empty list returned."""
    from coord.agent import narrow_artifact_paths

    result = narrow_artifact_paths([], ["tui_submenu — run — check"])
    assert result == []


# ── #1248: narrow_artifact_paths disk-verification tests ─────────────────────


def test_narrow_artifact_paths_worktree_falls_back_when_absent(
    tmp_path: Path,
) -> None:
    """When named binary is absent on disk, the original broad glob is kept.

    #1248: text-matching alone is insufficient — if tui_submenu appears in
    SMOKE_TESTS but hasn't been built yet, pinning the stash to that path
    produces a 0-copy stash silently.  Passing worktree= forces a disk check.
    """
    from coord.agent import narrow_artifact_paths

    # worktree exists but tui_submenu was never built
    worktree = tmp_path / "worktree"
    (worktree / "target" / "debug" / "examples").mkdir(parents=True)

    result = narrow_artifact_paths(
        ["target/debug/examples/tui_*"],
        ["tui_submenu — run it — menu should appear"],
        worktree=worktree,
    )
    # name matches text but missing on disk → keep broad glob
    assert result == ["target/debug/examples/tui_*"]


def test_narrow_artifact_paths_worktree_narrows_when_present(
    tmp_path: Path,
) -> None:
    """When named binary exists on disk, the glob IS narrowed to that path.

    #1248: the disk check must not block narrowing when the binary is present.
    """
    from coord.agent import narrow_artifact_paths

    worktree = tmp_path / "worktree"
    examples = worktree / "target" / "debug" / "examples"
    examples.mkdir(parents=True)
    # build the binary so it's present on disk
    (examples / "tui_submenu").write_bytes(b"\x7fELF" + b"\x00" * 200)

    result = narrow_artifact_paths(
        ["target/debug/examples/tui_*"],
        ["tui_submenu — run it — menu should appear"],
        worktree=worktree,
    )
    assert result == ["target/debug/examples/tui_submenu"]
    assert "target/debug/examples/tui_*" not in result


def test_narrow_artifact_paths_worktree_partial_on_disk(
    tmp_path: Path,
) -> None:
    """Only on-disk names are used when the smoke tests name multiple binaries.

    #1248: if SMOKE_TESTS names tui_submenu and tui_colors but only tui_submenu
    was actually built, the narrowed result contains only tui_submenu (not the
    absent tui_colors and not the broad glob).
    """
    from coord.agent import narrow_artifact_paths

    worktree = tmp_path / "worktree"
    examples = worktree / "target" / "debug" / "examples"
    examples.mkdir(parents=True)
    (examples / "tui_submenu").write_bytes(b"\x7fELF" + b"\x00" * 200)
    # tui_colors intentionally NOT created on disk

    result = narrow_artifact_paths(
        ["target/debug/examples/tui_*"],
        ["tui_submenu and tui_colors — run them — should render"],
        worktree=worktree,
    )
    # only the on-disk binary is in the result
    assert result == ["target/debug/examples/tui_submenu"]
    assert "target/debug/examples/tui_colors" not in result
    assert "target/debug/examples/tui_*" not in result


def test_narrow_artifact_paths_no_worktree_preserves_text_only_behaviour() -> None:
    """worktree=None (default) keeps the original text-only matching.

    #1248: backward compat — interactive/remote callers that pass no worktree
    must not be broken by the new parameter.
    """
    from coord.agent import narrow_artifact_paths

    # No worktree → text match wins even though no real files exist
    result = narrow_artifact_paths(
        ["target/debug/examples/tui_*"],
        ["tui_submenu — run it — check menu"],
    )
    assert result == ["target/debug/examples/tui_submenu"]


# ── #982: stash integration tests ────────────────────────────────────────────

def test_stash_artifacts_scoped_spec_stashes_only_named_binary(
    tmp_path: Path,
) -> None:
    """spec.artifact_paths with a specific binary name stashes only that
    binary, not all files matching the repo-wide glob.  #982."""
    from coord.agent import DONE, AgentAssignment, AgentServer, AssignmentSpec

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    wt_path = state_dir / "worktrees" / "asgn-982-scoped"
    examples_dir = wt_path / "target" / "debug" / "examples"
    examples_dir.mkdir(parents=True)

    payload = b"\x7fELF" + b"\x00" * 200
    for name in ["tui_submenu", "tui_scrollbar", "tui_colors"]:
        (examples_dir / name).write_bytes(payload)

    server = AgentServer(
        machine_name="test",
        repos=["quadraui"],
        state_dir=state_dir,
        worker_command=lambda spec: [sys.executable, "-c", "print('ok')"],
        repo_paths={"quadraui": str(tmp_path / "repo")},
        # Server-wide config: the broad glob
        artifact_paths={"quadraui": ["target/debug/examples/tui_*"]},
    )

    # Spec carries a narrowed list (as if dispatch used narrow_artifact_paths)
    spec = AssignmentSpec(
        repo_name="quadraui",
        repo_path=str(tmp_path / "repo"),
        issue_number=982,
        issue_title="submenu scoped",
        briefing="b",
        branch="main",
        # Override: only stash tui_submenu
        artifact_paths=["target/debug/examples/tui_submenu"],
    )
    a = AgentAssignment(
        id="asgn-982-scoped",
        spec=spec,
        status=DONE,
        branch="issue-982-submenu-scoped",
    )
    a.worktree_path = str(wt_path)

    server._stash_artifacts(a)

    stash_dir = (
        state_dir / "artifacts" / "quadraui" / "issue-982-submenu-scoped"
    )
    stashed = {p.name for p in stash_dir.iterdir() if not p.name.startswith(".")}
    assert stashed == {"tui_submenu"}, (
        f"scoped spec should stash only tui_submenu; got {stashed!r}"
    )


def test_stash_artifacts_no_spec_override_uses_repo_wide_glob(
    tmp_path: Path,
) -> None:
    """With no spec.artifact_paths override, the server's repo-wide glob
    stashes all matching files.  #982: fallback path preserved."""
    from coord.agent import DONE, AgentAssignment, AgentServer, AssignmentSpec

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    wt_path = state_dir / "worktrees" / "asgn-982-fallback"
    examples_dir = wt_path / "target" / "debug" / "examples"
    examples_dir.mkdir(parents=True)

    payload = b"\x7fELF" + b"\x00" * 200
    for name in ["tui_submenu", "tui_scrollbar"]:
        (examples_dir / name).write_bytes(payload)

    server = AgentServer(
        machine_name="test",
        repos=["quadraui"],
        state_dir=state_dir,
        worker_command=lambda spec: [sys.executable, "-c", "print('ok')"],
        repo_paths={"quadraui": str(tmp_path / "repo")},
        artifact_paths={"quadraui": ["target/debug/examples/tui_*"]},
    )

    # Spec has no artifact_paths override → falls back to server-wide glob
    spec = AssignmentSpec(
        repo_name="quadraui",
        repo_path=str(tmp_path / "repo"),
        issue_number=983,
        issue_title="fallback glob",
        briefing="b",
        branch="main",
        artifact_paths=[],  # empty → use server config
    )
    a = AgentAssignment(
        id="asgn-982-fallback",
        spec=spec,
        status=DONE,
        branch="issue-983-fallback-glob",
    )
    a.worktree_path = str(wt_path)

    server._stash_artifacts(a)

    stash_dir = (
        state_dir / "artifacts" / "quadraui" / "issue-983-fallback-glob"
    )
    stashed = {p.name for p in stash_dir.iterdir() if not p.name.startswith(".")}
    assert stashed == {"tui_submenu", "tui_scrollbar"}, (
        f"fallback should stash all tui_* files; got {stashed!r}"
    )


def test_stash_artifacts_narrows_using_worker_own_smoke_tests_log(
    tmp_path: Path,
) -> None:
    """#982: _stash_artifacts must narrow the repo-wide glob using the
    worker's OWN just-completed SMOKE_TESTS block, parsed from
    assignment.log_path — this is the headless Work dispatch path
    (_dispatch_headless sends the full glob unmodified; narrowing has to
    happen here, since smoke tests don't exist until the worker's session
    ends). Regression test for the review finding that no call site
    actually narrowed the path that produces the reported bloat."""
    from coord.agent import DONE, AgentAssignment, AgentServer, AssignmentSpec

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    wt_path = state_dir / "worktrees" / "asgn-982-headless"
    examples_dir = wt_path / "target" / "debug" / "examples"
    examples_dir.mkdir(parents=True)

    payload = b"\x7fELF" + b"\x00" * 200
    for name in ["tui_submenu", "tui_scrollbar", "tui_colors"]:
        (examples_dir / name).write_bytes(payload)

    log_dir = state_dir / "logs"
    log_dir.mkdir(parents=True)
    log_path = log_dir / "asgn-982-headless.log"
    log_path.write_text(
        "worker output...\n"
        "SMOKE_TESTS:\n"
        "- submenu opens — run tui_submenu — submenu renders\n"
        "END_SMOKE_TESTS\n"
    )

    server = AgentServer(
        machine_name="test",
        repos=["quadraui"],
        state_dir=state_dir,
        worker_command=lambda spec: [sys.executable, "-c", "print('ok')"],
        repo_paths={"quadraui": str(tmp_path / "repo")},
        artifact_paths={"quadraui": ["target/debug/examples/tui_*"]},
    )

    # Mimics _dispatch_headless: the /assign payload carries the repo's
    # full, unmodified glob as spec.artifact_paths — nothing narrows it
    # before dispatch.
    spec = AssignmentSpec(
        repo_name="quadraui",
        repo_path=str(tmp_path / "repo"),
        issue_number=982,
        issue_title="headless narrow",
        briefing="b",
        branch="main",
        artifact_paths=["target/debug/examples/tui_*"],
    )
    a = AgentAssignment(
        id="asgn-982-headless",
        spec=spec,
        status=DONE,
        branch="issue-982-headless-narrow",
    )
    a.worktree_path = str(wt_path)
    a.log_path = str(log_path)

    server._stash_artifacts(a)

    stash_dir = (
        state_dir / "artifacts" / "quadraui" / "issue-982-headless-narrow"
    )
    stashed = {p.name for p in stash_dir.iterdir() if not p.name.startswith(".")}
    assert stashed == {"tui_submenu"}, (
        f"headless dispatch should narrow to the smoke-tested binary "
        f"named in the worker's own log; got {stashed!r}"
    )


def test_stash_artifacts_no_log_path_falls_back_to_full_glob(
    tmp_path: Path,
) -> None:
    """#982: with no log_path recorded on the assignment, narrowing is
    skipped entirely (nothing to parse) and the full glob is stashed —
    same behavior as before this fix, just guarding against AttributeError
    or a crash when log_path is unset."""
    from coord.agent import DONE, AgentAssignment, AgentServer, AssignmentSpec

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    wt_path = state_dir / "worktrees" / "asgn-982-nolog"
    examples_dir = wt_path / "target" / "debug" / "examples"
    examples_dir.mkdir(parents=True)

    payload = b"\x7fELF" + b"\x00" * 200
    for name in ["tui_submenu", "tui_scrollbar"]:
        (examples_dir / name).write_bytes(payload)

    server = AgentServer(
        machine_name="test",
        repos=["quadraui"],
        state_dir=state_dir,
        worker_command=lambda spec: [sys.executable, "-c", "print('ok')"],
        repo_paths={"quadraui": str(tmp_path / "repo")},
        artifact_paths={"quadraui": ["target/debug/examples/tui_*"]},
    )

    spec = AssignmentSpec(
        repo_name="quadraui",
        repo_path=str(tmp_path / "repo"),
        issue_number=982,
        issue_title="no log path",
        briefing="b",
        branch="main",
        artifact_paths=["target/debug/examples/tui_*"],
    )
    a = AgentAssignment(
        id="asgn-982-nolog",
        spec=spec,
        status=DONE,
        branch="issue-982-nolog",
    )
    a.worktree_path = str(wt_path)
    assert a.log_path is None

    server._stash_artifacts(a)

    stash_dir = state_dir / "artifacts" / "quadraui" / "issue-982-nolog"
    stashed = {p.name for p in stash_dir.iterdir() if not p.name.startswith(".")}
    assert stashed == {"tui_submenu", "tui_scrollbar"}


def test_stash_artifacts_for_branch_prunes_stale_files_on_narrowed_restash(
    tmp_path: Path,
) -> None:
    """#982: a re-stash with a narrower pattern set must shrink an existing
    oversized stash, not just avoid growing it. First stash with the full
    glob (simulating the unnarrowed first headless Work dispatch), then
    re-stash the same branch with only one file named — the other files
    left over from the first stash must be pruned."""
    from coord.agent import stash_artifacts_for_branch

    state_dir = tmp_path / "state"
    wt_path = tmp_path / "worktree"
    examples_dir = wt_path / "target" / "debug" / "examples"
    examples_dir.mkdir(parents=True)

    payload = b"\x7fELF" + b"\x00" * 200
    for name in ["tui_submenu", "tui_scrollbar", "tui_colors"]:
        (examples_dir / name).write_bytes(payload)

    # First stash: broad glob, all three files land in the stash.
    count1 = stash_artifacts_for_branch(
        worktree_path=wt_path,
        branch="issue-982-prune",
        repo_name="quadraui",
        patterns=["target/debug/examples/tui_*"],
        state_dir=state_dir,
        assignment_id="asgn-1",
    )
    assert count1 == 3

    stash_dir = state_dir / "artifacts" / "quadraui" / "issue-982-prune"
    assert {p.name for p in stash_dir.iterdir() if not p.name.startswith(".")} == {
        "tui_submenu",
        "tui_scrollbar",
        "tui_colors",
    }

    # Re-stash the same branch, narrowed to a single named binary (as if a
    # later fix-of/rework-of session narrowed against smoke tests). The
    # stale tui_scrollbar / tui_colors copies must be pruned, not just left
    # in place alongside the freshly re-copied tui_submenu.
    count2 = stash_artifacts_for_branch(
        worktree_path=wt_path,
        branch="issue-982-prune",
        repo_name="quadraui",
        patterns=["target/debug/examples/tui_submenu"],
        state_dir=state_dir,
        assignment_id="asgn-2",
    )
    assert count2 == 1

    stashed_after = {
        p.name for p in stash_dir.iterdir() if not p.name.startswith(".")
    }
    assert stashed_after == {"tui_submenu"}, (
        f"narrowed re-stash should prune stale files; got {stashed_after!r}"
    )
    # The assignment_id marker (a dotfile) must survive the prune.
    assert (stash_dir / ".assignment_id").read_text() == "asgn-2"


def test_gc_artifacts_removes_old_directories(tmp_path: Path) -> None:
    """_gc_artifacts should remove stash dirs older than ttl_days."""
    import os
    import time as _time

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    server = AgentServer(
        machine_name="test",
        repos=[],
        state_dir=state_dir,
        worker_command=lambda spec: [],
    )

    # Create an artifact stash dir and manually backdate its mtime
    stash = state_dir / "artifacts" / "api" / "old-branch"
    stash.mkdir(parents=True)
    (stash / "mybinary").write_bytes(b"\x7fELF" + b"\x00" * 200)

    # Age the directory to 4 days ago (past the 3-day default TTL)
    old_time = _time.time() - 4 * 86400
    os.utime(stash, (old_time, old_time))

    removed = server._gc_artifacts(ttl_days=3.0)
    assert removed == 1
    assert not stash.exists()


def test_gc_artifacts_keeps_recent_directories(tmp_path: Path) -> None:
    """_gc_artifacts must not remove stash dirs within the TTL window."""
    import os
    import time as _time

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    server = AgentServer(
        machine_name="test",
        repos=[],
        state_dir=state_dir,
        worker_command=lambda spec: [],
    )

    stash = state_dir / "artifacts" / "api" / "recent-branch"
    stash.mkdir(parents=True)
    (stash / "mybinary").write_bytes(b"\x7fELF" + b"\x00" * 200)

    # Age the directory to only 1 day ago (well within the 3-day TTL)
    recent_time = _time.time() - 1 * 86400
    os.utime(stash, (recent_time, recent_time))

    removed = server._gc_artifacts(ttl_days=3.0)
    assert removed == 0
    assert stash.exists()


def test_health_includes_artifact_bytes(tmp_path: Path) -> None:
    """health() should include an artifact_bytes key."""
    server = _server(tmp_path)
    h = server.health()
    assert "artifact_bytes" in h
    assert isinstance(h["artifact_bytes"], int)
    assert h["artifact_bytes"] == 0  # no stash yet


def test_artifact_manifest_returns_none_when_missing(tmp_path: Path) -> None:
    """artifact_manifest returns None when no stash dir exists."""
    server = _server(tmp_path)
    result = server.artifact_manifest("api", "issue-1-nonexistent")
    assert result is None


def test_artifact_manifest_returns_file_list(tmp_path: Path) -> None:
    """artifact_manifest returns the correct manifest dict when files exist."""
    server = _server(tmp_path)

    # Manually create a stash directory
    stash = server.state_dir / "artifacts" / "api" / "issue-1-my-feature"
    stash.mkdir(parents=True)
    (stash / "mybinary").write_bytes(b"\x7fELF" + b"\x00" * 200)
    (stash / ".assignment_id").write_text("asgn-123")

    manifest = server.artifact_manifest("api", "issue-1-my-feature")
    assert manifest is not None
    assert manifest["built_by_assignment_id"] == "asgn-123"
    assert len(manifest["files"]) == 1
    assert manifest["files"][0]["name"] == "mybinary"
    assert manifest["total_bytes"] == manifest["files"][0]["size"]


# ── #914: _find_live_worktree + artifact_absence_reason ────────────────────────


def test_find_live_worktree_matches_by_current_branch(tmp_path: Path) -> None:
    """_find_live_worktree locates a real `git worktree add` checkout by
    its current (sanitized) branch name, independent of directory naming."""
    rp = _init_repo(tmp_path / "repo")
    server = _server(tmp_path, repo_path=rp)

    wt_path = tmp_path / "state" / "worktrees" / "asgn-xyz"
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "-b", "issue-99-fix", str(wt_path)],
        cwd=str(rp),
        check=True,
        capture_output=True,
    )

    found = server._find_live_worktree("api", "issue-99-fix")
    assert found == wt_path


def test_find_live_worktree_returns_none_for_unknown_repo(tmp_path: Path) -> None:
    """No repo_paths entry for the requested repo → no crash, just None."""
    server = _server(tmp_path)
    assert server._find_live_worktree("no-such-repo", "issue-1-x") is None


def test_find_live_worktree_returns_none_when_branch_not_checked_out(
    tmp_path: Path,
) -> None:
    """A configured repo with no worktree on the requested branch → None."""
    server = _server(tmp_path)
    assert server._find_live_worktree("api", "issue-404-nonexistent") is None


def test_find_live_worktree_expands_tilde_repo_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#939: repo_paths entries configured with a literal ``~`` (as in
    coordinator.yml on hosts that keep repos under the home directory) must
    resolve the same way every other repo_paths consumer in this module
    does (see the ``.expanduser()`` calls elsewhere in agent.py). Before the
    fix, ``_find_live_worktree`` passed the raw ``~``-prefixed string straight
    to ``git``, which cannot resolve ``~`` itself, so a live worktree was
    silently reported as missing.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    rp = _init_repo(fake_home / "repo")
    server = AgentServer(
        machine_name="test",
        capabilities=["python"],
        repos=["api"],
        state_dir=tmp_path / "state",
        worker_command=lambda spec: [sys.executable, "-c", "print('worker-output')"],
        repo_paths={"api": "~/repo"},
    )

    wt_path = tmp_path / "state" / "worktrees" / "asgn-xyz"
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "-b", "issue-939-fix", str(wt_path)],
        cwd=str(rp),
        check=True,
        capture_output=True,
    )

    found = server._find_live_worktree("api", "issue-939-fix")
    assert found == wt_path


def test_artifact_absence_reason_worktree_present_no_patterns(
    tmp_path: Path,
) -> None:
    """Reason names 'no artifact_paths configured' when a live worktree
    exists but the repo isn't configured to stash anything."""
    rp = _init_repo(tmp_path / "repo")
    server = _server(tmp_path, repo_path=rp)  # no artifact_paths kwarg

    wt_path = tmp_path / "state" / "worktrees" / "asgn-noconf"
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "-b", "issue-1-noconf", str(wt_path)],
        cwd=str(rp),
        check=True,
        capture_output=True,
    )

    reason = server.artifact_absence_reason("api", "issue-1-noconf")
    assert "no artifact_paths configured" in reason


def test_artifact_absence_reason_genuinely_absent(tmp_path: Path) -> None:
    """Reason correctly reports 'genuinely absent' when no worktree matches."""
    server = _server(tmp_path, artifact_paths={"api": ["target/debug/foo"]})
    reason = server.artifact_absence_reason("api", "issue-1-never-existed")
    assert "already merged" in reason or "nothing was ever built" in reason


def test_artifact_absence_reason_distinguishes_empty_stash_dir(
    tmp_path: Path,
) -> None:
    """#1295 fix item #5: an existing-but-EMPTY stash directory must get
    DIFFERENT wording than "no stash and no live worktree at all" — the
    former means a stash attempt DID run (worker, interactive finalize, or
    the sweep's own orphan-stash) and matched 0 files (an artifact_paths /
    build problem); the latter means nothing ever tried.  Before this fix
    both cases produced the identical generic message, and the only test
    covering the string (`..._names_worktree_sweep_possibility`) never
    exercised a stash directory that actually existed.
    """
    server = _server(tmp_path)
    stash_dir = server.state_dir / "artifacts" / "api" / "issue-1295-empty"
    stash_dir.mkdir(parents=True)
    # Directory exists but holds no real (non-dotfile) content.

    reason = server.artifact_absence_reason("api", "issue-1295-empty")
    assert "empty" in reason.lower()
    assert str(stash_dir) in reason


def test_artifact_absence_reason_empty_vs_no_stash_dir_are_distinct(
    tmp_path: Path,
) -> None:
    """Sanity check the two wordings introduced by fix item #5 don't collapse
    back into the same generic string for two genuinely different states.
    """
    server = _server(tmp_path)
    empty_dir = server.state_dir / "artifacts" / "api" / "issue-1295-empty2"
    empty_dir.mkdir(parents=True)

    reason_empty = server.artifact_absence_reason("api", "issue-1295-empty2")
    reason_absent = server.artifact_absence_reason("api", "issue-1295-truly-absent")
    assert reason_empty != reason_absent
    assert "empty" in reason_empty.lower()
    assert "empty" not in reason_absent.lower()


def test_artifact_absence_reason_rejects_bad_path_components(
    tmp_path: Path,
) -> None:
    """repo/branch names outside the safe path-component charset get the
    same guard as artifact_manifest, not a crash from a bad path lookup."""
    server = _server(tmp_path)
    assert server.artifact_absence_reason("a/b", "issue-1-x") == "invalid repo/branch name"
    assert server.artifact_absence_reason("api", "a/b") == "invalid repo/branch name"


def test_artifact_manifest_lazy_stashes_from_live_worktree(tmp_path: Path) -> None:
    """artifact_manifest() self-heals: a live worktree still on the requested
    branch gets stashed on demand when the persistent stash is empty (#914),
    mirroring the vimcode #552 'missed finalize' scenario end-to-end."""
    rp = _init_repo(tmp_path / "repo")
    server = _server(
        tmp_path, repo_path=rp, artifact_paths={"api": ["target/debug/mybinary"]}
    )

    wt_path = tmp_path / "state" / "worktrees" / "asgn-552"
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "-b", "issue-552-fix", str(wt_path)],
        cwd=str(rp),
        check=True,
        capture_output=True,
    )
    (wt_path / "target" / "debug").mkdir(parents=True)
    (wt_path / "target" / "debug" / "mybinary").write_bytes(b"\x7fELF" + b"\x00" * 200)

    stash_dir = server.state_dir / "artifacts" / "api" / "issue-552-fix"
    assert not stash_dir.exists()

    manifest = server.artifact_manifest("api", "issue-552-fix")
    assert manifest is not None
    assert [f["name"] for f in manifest["files"]] == ["mybinary"]
    assert manifest["built_by_assignment_id"] == "asgn-552"
    assert (stash_dir / "mybinary").exists()


def test_artifact_manifest_none_when_worktree_present_but_no_files_match(
    tmp_path: Path,
) -> None:
    """artifact_manifest() returns None (→ 404), not an empty-but-200
    manifest, when a live worktree exists and artifact_paths is configured
    but nothing on disk matches the glob yet (#914 review regression case).

    stash_artifacts_for_branch's mkdir(parents=True, exist_ok=True) is
    unconditional, so a naive `stash_dir.exists()` success check would
    treat the freshly-created empty directory as "stashed" and both return
    a misleading 200 with zero files AND permanently block future retries
    for this branch. This asserts the fix: no content → None, and the
    empty directory doesn't poison a subsequent successful stash attempt.
    """
    rp = _init_repo(tmp_path / "repo")
    server = _server(
        tmp_path, repo_path=rp, artifact_paths={"api": ["target/debug/mybinary"]}
    )

    wt_path = tmp_path / "state" / "worktrees" / "asgn-nomatch"
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "-b", "issue-914-nomatch", str(wt_path)],
        cwd=str(rp),
        check=True,
        capture_output=True,
    )
    # No target/debug/mybinary in the worktree — the glob matches nothing.

    assert server.artifact_manifest("api", "issue-914-nomatch") is None

    # A later, successful build must still self-heal (no self-poisoning).
    (wt_path / "target" / "debug").mkdir(parents=True)
    (wt_path / "target" / "debug" / "mybinary").write_bytes(b"\x7fELF" + b"\x00" * 200)

    manifest = server.artifact_manifest("api", "issue-914-nomatch")
    assert manifest is not None
    assert [f["name"] for f in manifest["files"]] == ["mybinary"]


# ── stash_artifacts_for_branch standalone function (#562) ─────────────────────


def test_stash_artifacts_for_branch_copies_file(tmp_path: Path) -> None:
    """stash_artifacts_for_branch (module-level) copies matching files."""
    from coord.agent import stash_artifacts_for_branch

    wt = tmp_path / "worktree"
    (wt / "target" / "debug").mkdir(parents=True)
    binary = wt / "target" / "debug" / "coord-tui"
    binary.write_bytes(b"\x7fELF" + b"\x00" * 200)

    state_dir = tmp_path / "state"
    count = stash_artifacts_for_branch(
        worktree_path=wt,
        branch="issue-562-fix",
        repo_name="coord-tui",
        patterns=["target/debug/coord-tui"],
        state_dir=state_dir,
        assignment_id="aid-test",
    )

    stash = state_dir / "artifacts" / "coord-tui" / "issue-562-fix"
    assert count == 1
    assert (stash / "coord-tui").exists()
    assert (stash / ".assignment_id").read_text() == "aid-test"


def test_stash_artifacts_for_branch_noop_empty_patterns(tmp_path: Path) -> None:
    """Returns 0 immediately when patterns list is empty."""
    from coord.agent import stash_artifacts_for_branch

    count = stash_artifacts_for_branch(
        worktree_path=tmp_path / "wt",
        branch="some-branch",
        repo_name="myrepo",
        patterns=[],
        state_dir=tmp_path / "state",
    )
    assert count == 0
    assert not (tmp_path / "state" / "artifacts").exists()


def test_stash_artifacts_for_branch_noop_missing_worktree(tmp_path: Path) -> None:
    """Returns 0 when the worktree directory doesn't exist."""
    from coord.agent import stash_artifacts_for_branch

    count = stash_artifacts_for_branch(
        worktree_path=tmp_path / "nonexistent",
        branch="some-branch",
        repo_name="myrepo",
        patterns=["target/debug/foo"],
        state_dir=tmp_path / "state",
    )
    assert count == 0


def test_agent_stash_artifacts_delegates_to_standalone(tmp_path: Path) -> None:
    """AgentServer._stash_artifacts delegates to stash_artifacts_for_branch (#562).

    Verify the refactored wrapper still produces the correct stash so we
    haven't broken the existing worker path while extracting the function.
    """
    server, a, wt_path = _make_done_assignment(tmp_path)

    target_dir = wt_path / "target" / "debug"
    target_dir.mkdir(parents=True)
    (target_dir / "mybinary").write_bytes(b"\x7fELF" + b"\x00" * 200)

    server._stash_artifacts(a)

    stash_dir = server.state_dir / "artifacts" / "api" / "issue-1-my-feature"
    assert (stash_dir / "mybinary").exists(), "delegation broke the stash"
    assert (stash_dir / ".assignment_id").read_text() == "asgn-abc123"


# ── Debug-symbol stripping + oversize warning (#940) ─────────────────────────


def test_strip_debug_symbols_runs_strip_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_strip_debug_symbols shells out to `strip -S <file>` when on PATH."""
    from coord import agent as agent_mod

    calls: list[list[str]] = []

    def fake_which(name: str) -> str | None:
        return "/usr/bin/strip" if name == "strip" else None

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode=0)

    monkeypatch.setattr(agent_mod.shutil, "which", fake_which)
    monkeypatch.setattr(agent_mod.subprocess, "run", fake_run)

    target = tmp_path / "mybinary"
    target.write_bytes(b"\x7fELF" + b"\x00" * 200)

    assert agent_mod._strip_debug_symbols(target) is True
    assert calls == [["/usr/bin/strip", "-S", str(target)]]


def test_strip_debug_symbols_noop_when_strip_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `strip` on PATH: skip silently, never shell out."""
    from coord import agent as agent_mod

    monkeypatch.setattr(agent_mod.shutil, "which", lambda name: None)

    def fail_if_called(cmd, **kwargs):
        raise AssertionError("subprocess.run should not be called when strip is missing")

    monkeypatch.setattr(agent_mod.subprocess, "run", fail_if_called)

    target = tmp_path / "mybinary"
    target.write_bytes(b"\x7fELF" + b"\x00" * 200)

    assert agent_mod._strip_debug_symbols(target) is False


def test_strip_debug_symbols_returns_false_on_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed/erroring strip is swallowed — the original copy is kept."""
    from coord import agent as agent_mod

    monkeypatch.setattr(agent_mod.shutil, "which", lambda name: "/usr/bin/strip")

    def raising_run(cmd, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(agent_mod.subprocess, "run", raising_run)

    target = tmp_path / "mybinary"
    target.write_bytes(b"\x7fELF" + b"\x00" * 200)

    assert agent_mod._strip_debug_symbols(target) is False
    assert target.exists(), "file must survive a failed strip attempt"


def test_stash_artifacts_for_branch_strips_each_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every copied file is passed through _strip_debug_symbols (#940)."""
    from coord import agent as agent_mod

    stripped: list[Path] = []
    monkeypatch.setattr(
        agent_mod, "_strip_debug_symbols", lambda p: stripped.append(p) or True
    )

    wt = tmp_path / "worktree"
    (wt / "target" / "debug").mkdir(parents=True)
    (wt / "target" / "debug" / "tui_a").write_bytes(b"\x7fELF" + b"\x00" * 200)
    (wt / "target" / "debug" / "tui_b").write_bytes(b"\x7fELF" + b"\x00" * 200)

    state_dir = tmp_path / "state"
    count = agent_mod.stash_artifacts_for_branch(
        worktree_path=wt,
        branch="issue-940-strip",
        repo_name="quadraui",
        patterns=["target/debug/tui_*"],
        state_dir=state_dir,
    )

    assert count == 2
    stash_dir = state_dir / "artifacts" / "quadraui" / "issue-940-strip"
    assert sorted(p.name for p in stripped) == ["tui_a", "tui_b"]
    assert all(p.parent == stash_dir for p in stripped), "must strip the STASHED copy, not the source"


def test_stash_artifacts_for_branch_logs_oversize_warning(tmp_path: Path) -> None:
    """A stash over _STASH_WARN_BYTES appends a WARNING line to the log (#940)."""
    from coord import agent as agent_mod

    wt = tmp_path / "worktree"
    (wt / "target" / "debug").mkdir(parents=True)
    (wt / "target" / "debug" / "bigbin").write_bytes(b"\x00" * 500)

    log_path = tmp_path / "assignment.log"
    log_path.write_text("")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(agent_mod, "_STASH_WARN_BYTES", 100)  # force the warning path
        count = agent_mod.stash_artifacts_for_branch(
            worktree_path=wt,
            branch="issue-940-warn",
            repo_name="quadraui",
            patterns=["target/debug/bigbin"],
            state_dir=tmp_path / "state",
            log_path=str(log_path),
        )

    assert count == 1
    log_text = log_path.read_text()
    assert "# stash WARNING" in log_text
    assert "quadraui" in log_text
    assert "--only" in log_text  # points the reader at the escape hatch


def test_stash_artifacts_for_branch_no_warning_under_threshold(tmp_path: Path) -> None:
    """A small stash produces no WARNING line."""
    from coord import agent as agent_mod

    wt = tmp_path / "worktree"
    (wt / "target" / "debug").mkdir(parents=True)
    (wt / "target" / "debug" / "smallbin").write_bytes(b"\x00" * 500)

    log_path = tmp_path / "assignment.log"
    log_path.write_text("")

    count = agent_mod.stash_artifacts_for_branch(
        worktree_path=wt,
        branch="issue-940-nowarn",
        repo_name="quadraui",
        patterns=["target/debug/smallbin"],
        state_dir=tmp_path / "state",
        log_path=str(log_path),
    )

    assert count == 1
    assert "WARNING" not in log_path.read_text()


# ── #1248: stash 0-copy robustness tests ─────────────────────────────────────


def test_stash_artifacts_for_branch_zero_copy_no_marker(tmp_path: Path) -> None:
    """A 0-copy stash must not write the .assignment_id marker.

    #1248: writing a marker on an empty stash is misleading — the manifest
    endpoint would surface a build that copied nothing.
    """
    from coord.agent import stash_artifacts_for_branch

    wt = tmp_path / "worktree"
    wt.mkdir(parents=True)
    # pattern resolves to nothing — no files in worktree match

    state_dir = tmp_path / "state"
    count = stash_artifacts_for_branch(
        worktree_path=wt,
        branch="issue-1248-zero",
        repo_name="myrepo",
        patterns=["target/debug/nonexistent_binary"],
        state_dir=state_dir,
        assignment_id="aid-zero",
    )

    assert count == 0
    stash_dir = state_dir / "artifacts" / "myrepo" / "issue-1248-zero"
    # stash dir was created by mkdir(parents=True, exist_ok=True) — that's fine
    assert not (stash_dir / ".assignment_id").exists(), (
        ".assignment_id must not be written on a 0-copy stash"
    )


def test_stash_artifacts_for_branch_zero_copy_warning_logged(tmp_path: Path) -> None:
    """A 0-copy stash appends a '# stash WARNING: 0 files matched' line.

    #1248: the worker log should be loud about a missed stash so the operator
    can diagnose a mis-configured artifact_paths without digging through the
    stash directory.
    """
    from coord.agent import stash_artifacts_for_branch

    wt = tmp_path / "worktree"
    wt.mkdir(parents=True)

    log_path = tmp_path / "assignment.log"
    log_path.write_text("")

    count = stash_artifacts_for_branch(
        worktree_path=wt,
        branch="issue-1248-warn",
        repo_name="myrepo",
        patterns=["target/debug/ghost_binary"],
        state_dir=tmp_path / "state",
        assignment_id="aid-warn",
        log_path=str(log_path),
    )

    assert count == 0
    log_text = log_path.read_text()
    assert "# stash WARNING" in log_text
    assert "0 files matched" in log_text
    assert "ghost_binary" in log_text


def test_stash_artifacts_for_branch_nonzero_copy_marker_written(tmp_path: Path) -> None:
    """When files ARE copied the .assignment_id marker is still written.

    #1248: the >0-copy path must be byte-for-byte identical to before.
    """
    from coord.agent import stash_artifacts_for_branch

    wt = tmp_path / "worktree"
    (wt / "target" / "debug").mkdir(parents=True)
    (wt / "target" / "debug" / "mybin").write_bytes(b"\x7fELF" + b"\x00" * 200)

    state_dir = tmp_path / "state"
    count = stash_artifacts_for_branch(
        worktree_path=wt,
        branch="issue-1248-ok",
        repo_name="myrepo",
        patterns=["target/debug/mybin"],
        state_dir=state_dir,
        assignment_id="aid-ok",
    )

    assert count == 1
    stash_dir = state_dir / "artifacts" / "myrepo" / "issue-1248-ok"
    assert (stash_dir / ".assignment_id").read_text() == "aid-ok"


def test_stash_artifacts_for_branch_zero_copy_no_warning_without_log(
    tmp_path: Path,
) -> None:
    """A 0-copy stash with no log_path provided must not raise.

    The warning path is only exercised when log_path is set; without it the
    function should return 0 silently.
    """
    from coord.agent import stash_artifacts_for_branch

    wt = tmp_path / "worktree"
    wt.mkdir(parents=True)

    count = stash_artifacts_for_branch(
        worktree_path=wt,
        branch="issue-1248-nolog",
        repo_name="myrepo",
        patterns=["target/debug/nobody"],
        state_dir=tmp_path / "state",
        assignment_id="aid-nolog",
    )

    assert count == 0
    # no exception, no marker
    stash_dir = tmp_path / "state" / "artifacts" / "myrepo" / "issue-1248-nolog"
    assert not (stash_dir / ".assignment_id").exists()


# ── #1323: per-glob zero-match advisory (fix #2) ─────────────────────────────


def test_stash_artifacts_for_branch_unmatched_out_all_miss(tmp_path: Path) -> None:
    """unmatched_out is populated when all patterns match 0 files (#1323).

    When every configured glob resolves to nothing, the caller should receive
    the full list of unmatched patterns so it can surface a per-glob advisory
    instead of just a generic "0 files copied" message.
    """
    from coord.agent import stash_artifacts_for_branch

    wt = tmp_path / "worktree"
    wt.mkdir(parents=True)
    # No files created — all globs will miss.

    unmatched: list[str] = []
    count = stash_artifacts_for_branch(
        worktree_path=wt,
        branch="issue-1323-all-miss",
        repo_name="vimcode",
        patterns=["target/debug/vimcode", "target/debug/vcd"],
        state_dir=tmp_path / "state",
        assignment_id="aid-1323",
        unmatched_out=unmatched,
    )

    assert count == 0
    assert "target/debug/vimcode" in unmatched
    assert "target/debug/vcd" in unmatched


def test_stash_artifacts_for_branch_unmatched_out_partial_miss(
    tmp_path: Path,
) -> None:
    """unmatched_out names only the patterns that matched 0 files (#1323).

    When some globs match files but others don't, only the unmatched ones
    should appear in unmatched_out; the function should still return the
    count of copied files (> 0).
    """
    from coord.agent import stash_artifacts_for_branch

    wt = tmp_path / "worktree"
    (wt / "target" / "debug").mkdir(parents=True)
    # Only the TUI binary exists — the GUI binary is absent.
    (wt / "target" / "debug" / "vimcode-tui").write_bytes(b"\x7fELF" + b"\x00" * 200)

    log_path = tmp_path / "assignment.log"
    log_path.write_text("")

    unmatched: list[str] = []
    count = stash_artifacts_for_branch(
        worktree_path=wt,
        branch="issue-1323-partial",
        repo_name="vimcode",
        patterns=["target/debug/vimcode-tui", "target/debug/vimcode"],
        state_dir=tmp_path / "state",
        assignment_id="aid-1323b",
        log_path=str(log_path),
        unmatched_out=unmatched,
    )

    assert count == 1  # vimcode-tui was copied
    assert unmatched == ["target/debug/vimcode"]  # GUI binary missing

    log_text = log_path.read_text()
    assert "1 glob(s)" in log_text or "glob(s) matched 0 files" in log_text
    assert "target/debug/vimcode" in log_text


def test_stash_artifacts_for_branch_unmatched_out_none_does_not_raise(
    tmp_path: Path,
) -> None:
    """When unmatched_out is None (default), per-pattern tracking is skipped.

    The existing callers that don't pass unmatched_out must continue to
    work unmodified.
    """
    from coord.agent import stash_artifacts_for_branch

    wt = tmp_path / "worktree"
    wt.mkdir(parents=True)

    # Should not raise; no unmatched_out to populate.
    count = stash_artifacts_for_branch(
        worktree_path=wt,
        branch="issue-1323-noout",
        repo_name="myrepo",
        patterns=["target/debug/ghost"],
        state_dir=tmp_path / "state",
    )
    assert count == 0


def test_stash_artifacts_for_branch_partial_miss_logs_named_glob(
    tmp_path: Path,
) -> None:
    """A partial stash miss writes a WARNING naming the specific unmatched glob.

    Previously the per-glob detail was only emitted when ALL patterns missed;
    this test pins the partial-miss path (#1323).
    """
    from coord.agent import stash_artifacts_for_branch

    wt = tmp_path / "worktree"
    (wt / "target" / "debug").mkdir(parents=True)
    (wt / "target" / "debug" / "coord-tui").write_bytes(b"\x7fELF" + b"\x00" * 200)

    log_path = tmp_path / "run.log"
    log_path.write_text("")

    stash_artifacts_for_branch(
        worktree_path=wt,
        branch="issue-1323-partlog",
        repo_name="myrepo",
        patterns=["target/debug/coord-tui", "target/debug/nonexistent"],
        state_dir=tmp_path / "state",
        log_path=str(log_path),
    )

    log_text = log_path.read_text()
    assert "stash WARNING" in log_text
    assert "nonexistent" in log_text
    # The matched file should NOT be in the warning (not a false positive).
    assert "coord-tui" not in log_text.split("stash WARNING")[-1]


# ── #1323: build_command runs before stash (fix #3) ──────────────────────────


def test_stash_artifacts_build_command_runs_before_glob(tmp_path: Path) -> None:
    """build_command is run in the worktree before the artifact glob (#1323 fix #3).

    Simulates the vimcode#600 scenario: the worker built only the TUI binary
    but the repo's build_command also builds the GUI binary.  The stash should
    capture both after the pre-stash build runs.
    """
    from coord.agent import DONE, AgentAssignment, AgentServer, AssignmentSpec

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    wt_path = state_dir / "worktrees" / "asgn-1323"
    wt_path.mkdir(parents=True, exist_ok=True)

    # Pre-create only the TUI binary (simulating what the worker produced).
    (wt_path / "target" / "debug").mkdir(parents=True)
    (wt_path / "target" / "debug" / "myapp-tui").write_bytes(b"\x7fELF" + b"\x00" * 200)

    # build_command writes a second binary — simulating a full `cargo build`.
    gui_binary = wt_path / "target" / "debug" / "myapp-gui"
    build_script = f'printf "\\x7fELF%0200d" 0 > {gui_binary}'

    server = AgentServer(
        machine_name="test",
        repos=["myapp"],
        state_dir=state_dir,
        worker_command=lambda spec: [sys.executable, "-c", "print('ok')"],
        repo_paths={"myapp": str(tmp_path / "repo")},
        artifact_paths={"myapp": ["target/debug/myapp-tui", "target/debug/myapp-gui"]},
        build_commands={"myapp": build_script},
    )

    log_path = state_dir / "logs" / "asgn-1323.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("")

    spec = AssignmentSpec(
        repo_name="myapp",
        repo_path=str(tmp_path / "repo"),
        issue_number=1323,
        issue_title="build command test",
        briefing="b",
        branch="main",
    )
    a = AgentAssignment(
        id="asgn-1323",
        spec=spec,
        status=DONE,
        branch="issue-1323-build-cmd",
    )
    a.worktree_path = str(wt_path)
    a.log_path = str(log_path)

    server._stash_artifacts(a)

    stash_dir = state_dir / "artifacts" / "myapp" / "issue-1323-build-cmd"
    assert (stash_dir / "myapp-tui").exists(), "TUI binary not stashed"
    assert (stash_dir / "myapp-gui").exists(), "GUI binary not stashed (build_command may not have run)"

    log_text = log_path.read_text()
    assert "pre-stash build" in log_text


def test_stash_artifacts_build_command_logged_on_failure(tmp_path: Path) -> None:
    """A failing build_command is logged but does not abort the stash (#1323 fix #3).

    Best-effort: if the build command exits non-zero, the stash still runs
    (it may capture whatever the worker already built) and the failure is
    written to the assignment log.
    """
    from coord.agent import DONE, AgentAssignment, AgentServer, AssignmentSpec

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    wt_path = state_dir / "worktrees" / "asgn-1323fail"
    wt_path.mkdir(parents=True, exist_ok=True)
    (wt_path / "target" / "debug").mkdir(parents=True)
    (wt_path / "target" / "debug" / "mybin").write_bytes(b"\x7fELF" + b"\x00" * 200)

    server = AgentServer(
        machine_name="test",
        repos=["myapp"],
        state_dir=state_dir,
        worker_command=lambda spec: [sys.executable, "-c", "print('ok')"],
        repo_paths={"myapp": str(tmp_path / "repo")},
        artifact_paths={"myapp": ["target/debug/mybin"]},
        build_commands={"myapp": "exit 42"},  # Deliberate failure.
    )

    log_path = state_dir / "logs" / "asgn-1323fail.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("")

    spec = AssignmentSpec(
        repo_name="myapp",
        repo_path=str(tmp_path / "repo"),
        issue_number=1323,
        issue_title="build command failure",
        briefing="b",
        branch="main",
    )
    a = AgentAssignment(
        id="asgn-1323fail",
        spec=spec,
        status=DONE,
        branch="issue-1323-fail",
    )
    a.worktree_path = str(wt_path)
    a.log_path = str(log_path)

    # Should not raise, stash should still work.
    server._stash_artifacts(a)

    stash_dir = state_dir / "artifacts" / "myapp" / "issue-1323-fail"
    assert (stash_dir / "mybin").exists(), "pre-existing binary should still be stashed"

    log_text = log_path.read_text()
    assert "pre-stash build" in log_text
    assert "exit=42" in log_text


def test_reap_worker_stash_miss_stays_done_with_diagnostic(tmp_path: Path) -> None:
    """A stash glob matching 0 files records a diagnostic but stays DONE (#1357).

    #1323 downgraded DONE -> ADVISORY here, which false-failed the
    overwhelming majority of headless work assignments in a repo whose only
    artifact glob is unrelated to most changes (the motivating case:
    claude-coordinator's sole glob is a Rust `tui/` binary that Python-only
    work can never produce). #1357 reverts the status change: the assignment
    stays DONE, `zero_commit_reason` stays None (a stash miss is not a commit
    count), and the miss is recorded on the separate, diagnostic-only
    `stash_unmatched_globs` field instead.

    This drives the real `_reap` path via `assign()`/`wait_for()` (rather than
    re-implementing the logic inline) so a regression in `_reap`'s actual
    logic would be caught here.  The worker makes an empty commit so it clears
    the *separate* zero-commit advisory check first — isolating this test to
    the stash-miss path specifically.  `type` defaults to "work", which is in
    `_ADVISORY_TYPES`, so the diagnostic is recorded.
    """
    repo = _init_repo(tmp_path / "repo")
    server = _server(
        tmp_path,
        argv=[sys.executable, "-c", "import subprocess; subprocess.run(['git', 'commit', '--allow-empty', '-m', 'onward'], stdout=subprocess.DEVNULL)"],
        repo_path=repo,
        artifact_paths={"api": ["target/debug/missing-gui"]},
    )

    a = server.assign(_spec(repo))
    final = server.wait_for(a.id, timeout=10)

    assert final.status == DONE
    assert final.zero_commit_reason is None
    assert final.stash_unmatched_globs is not None
    assert any("missing-gui" in g for g in final.stash_unmatched_globs)
    server.shutdown()


def test_reap_review_type_gets_no_stash_diagnostic(tmp_path: Path) -> None:
    """A type="review" assignment gets no stash diagnostic on a glob miss (#1323 review finding #1).

    review/smoke/test/merge/conflict-fix assignments routinely finish DONE
    without (re)producing every configured artifact_paths glob (e.g. a review
    session that never runs a full build).  Only "work" assignments (in
    _ADVISORY_TYPES) get the `stash_unmatched_globs` diagnostic recorded —
    and per #1357, no assignment type ever has its status changed for this.
    """
    repo = _init_repo(tmp_path / "repo")
    server = _server(
        tmp_path,
        argv=[sys.executable, "-c", "import sys; sys.exit(0)"],
        repo_path=repo,
        artifact_paths={"api": ["target/debug/missing-gui"]},
    )

    a = server.assign(_spec(repo, type="review"))
    final = server.wait_for(a.id, timeout=10)

    assert final.status == DONE
    assert final.zero_commit_reason is None
    assert final.stash_unmatched_globs is None
    server.shutdown()


def _init_repo_with_remote(path: Path) -> Path:
    """Like `_init_repo`, but with a real (local, bare) `origin` configured
    and `main` already pushed.

    #2552's post-cleanup recheck (`AgentServer._pushed_commits_ahead`)
    deliberately reads the REMOTE-tracking ref (`origin/<branch>`), not the
    local branch — a rescue commit that never reached origin must not flip
    status. `_init_repo` (no remote) can't exercise that path at all, since
    `origin/<branch>` never exists there.
    """
    remote = path.parent / f"{path.name}-remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(remote)],
        check=True, capture_output=True,
    )
    _init_repo(path)
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote)],
        cwd=str(path), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "push", "-u", "origin", "main"],
        cwd=str(path), check=True, capture_output=True,
    )
    return path


def test_reap_status_reflects_post_rescue_branch_state(tmp_path: Path) -> None:
    """#2552: a rescued WIP commit that reaches origin must correct the
    pre-cleanup zero-commit verdict instead of leaving it standing.

    Reported bug: `_reap` measures "0 commits ahead of main" (true at that
    instant, since the worker left a tracked-file edit uncommitted) and sets
    ADVISORY with `zero_commit_reason` — BEFORE `_cleanup_worktree` runs.
    Cleanup then rescues the dirty worktree into a `WIP [coord-rescue]`
    commit and pushes it (`_rescue_uncommitted_work`), landing a real commit
    on the very branch the ADVISORY verdict was judged empty one step
    earlier. Nothing ever revisited the verdict, so a successful #1394
    rescue was always scored as "nothing was authored".

    Drives the real `_reap` path end to end (`assign()`/`wait_for()`,
    exactly like the neighboring stash-diagnostic tests above) rather than
    re-implementing the ordering inline, so a regression in the actual
    `_reap`/`_cleanup_worktree` sequence is what this test would catch.
    """
    repo = _init_repo_with_remote(tmp_path / "repo")
    server = _server(
        tmp_path,
        argv=[sys.executable, "-c", "open('README', 'a').write('dirty' + chr(10))"],
        repo_path=repo,
    )

    a = server.assign(_spec(repo))
    final = server.wait_for(a.id, timeout=15)

    assert final.status == DONE
    assert final.dirty_worktree_reason is not None
    assert "coord-rescue" in final.dirty_worktree_reason
    assert "UNVERIFIED" in final.dirty_worktree_reason
    server.shutdown()


def test_reap_stays_advisory_when_rescue_commit_never_reaches_origin(
    tmp_path: Path,
) -> None:
    """#2552 control: a rescue commit that stays LOCAL ONLY must NOT flip
    status to done.

    Same dirty-worktree shape as the test above, but the repo has no
    `origin` at all — the WIP-rescue push has nothing to push to, so the
    commit only ever exists on this one agent. Test/Review/Merge all operate
    against GitHub; a branch nobody but this agent can see is not "ready for
    the pipeline", so the post-cleanup recheck must leave the pre-existing
    ADVISORY verdict alone (the `dirty_worktree_reason` still records that
    real work exists — see `_record_dirty_worktree` — just not that it is
    live anywhere the pipeline can consume it).
    """
    repo = _init_repo(tmp_path / "repo")  # no `origin` remote configured
    server = _server(
        tmp_path,
        argv=[sys.executable, "-c", "open('README', 'a').write('dirty' + chr(10))"],
        repo_path=repo,
    )

    a = server.assign(_spec(repo))
    final = server.wait_for(a.id, timeout=15)

    assert final.status == ADVISORY
    assert final.dirty_worktree_reason is not None
    assert "coord-rescue" in final.dirty_worktree_reason
    server.shutdown()


def test_sanitize_branch_replaces_slashes(tmp_path: Path) -> None:
    """_sanitize_branch should replace slashes with dashes."""
    from coord.agent import _sanitize_branch

    assert _sanitize_branch("feature/my-thing") == "feature-my-thing"
    assert _sanitize_branch("issue-305-artifact-pull") == "issue-305-artifact-pull"
    assert _sanitize_branch("refs/heads/main") == "refs-heads-main"


def test_sanitize_branch_agrees_with_rust() -> None:
    """Pin Python _sanitize_branch against every case tested in tui/src/app.rs.

    The Python sanitizer (agent stash path) and the Rust sanitizer (TUI manifest
    lookup) must produce identical output for the same input; a divergence means
    the TUI fetches the wrong URL and the [a] badge never appears (#433).
    """
    from coord.agent import _sanitize_branch

    cases = [
        # clean inputs — no change
        ("issue-305", "issue-305"),
        ("feature_foo.bar", "feature_foo.bar"),
        ("abc123", "abc123"),
        # slashes → dashes (single per run)
        ("feature/my-thing", "feature-my-thing"),
        ("a//b", "a-b"),
        # refs/heads/<name> — the typical fallback branch name
        ("refs/heads/main", "refs-heads-main"),
        # leading/trailing separators stripped
        ("/leading", "leading"),
        ("trailing/", "trailing"),
        ("/both/", "both"),
        # spaces
        ("my branch name", "my-branch-name"),
        # long real-world name — all allowed chars, unchanged
        (
            "issue-305-artifact-pull-rsync-built-binaries-from",
            "issue-305-artifact-pull-rsync-built-binaries-from",
        ),
    ]
    for raw, expected in cases:
        result = _sanitize_branch(raw)
        assert result == expected, (
            f"_sanitize_branch({raw!r}) == {result!r}, want {expected!r} "
            "(Rust and Python sanitizers disagree — tui/src/app.rs sanitize_branch "
            "has a matching test; fix both together)"
        )


# ── #324: Provider-layer routing and capability gates ─────────────────────────


def _make_provider(
    *,
    enforces_deny_list: bool = True,
    resume: bool = True,
    inject: bool = True,
    build_argv: list[str] | None = None,
    initial_input_bytes: bytes | None = None,
    env_overrides: dict[str, str] | None = None,
):
    """Create a minimal duck-typed provider object for testing.

    Returns an object with the same interface as coord.providers.base.Provider
    without importing from coord.providers (keeps the test free of the cycle).
    """
    from coord.providers.base import Capabilities

    class _FakeProvider:
        def capabilities(self):
            return Capabilities(
                resume=resume,
                inject=inject,
                cost_reporting=False,
                true_system_prompt=True,
                enforces_deny_list=enforces_deny_list,
                billing_mode="unknown",
            )

        def build_command(self, spec, *, resolved_model=None, **_kwargs):
            if build_argv is not None:
                return list(build_argv)
            return [sys.executable, "-c", "print('provider-argv')"]

        def initial_input(self, spec):
            if initial_input_bytes is not None:
                return initial_input_bytes
            import json as _json
            payload = {"type": "user", "message": {"role": "user", "content": spec.briefing}}
            return (_json.dumps(payload) + "\n").encode()

        def result_marker(self):
            return '"type":"result"'

        def env(self):
            return dict(env_overrides) if env_overrides is not None else {}

        def parse_log(self, log_path, tail_bytes=65536):
            pass

    return _FakeProvider()


class TestProviderLayerDispatch:
    """#324: _spawn() routes through the provider layer for non-PTY providers."""

    def test_no_config_parity_uses_worker_command(self, tmp_path: Path) -> None:
        """When spec.provider is None, _spawn uses self.worker_command — the
        legacy path.  The argv captured at Popen time must be identical to
        what worker_command returns (no-config parity, #324 requirement #1).

        #1796 regression guard: this is the ONE case that must still take
        the silent legacy path after #1796 — only a *named* provider that
        cannot be resolved is now refused (see
        TestProviderLayerDispatch.test_provider_unknown_name_refused_not_silently_legacy);
        `spec.provider is None` itself must behave byte-identically to
        before."""
        import coord.agent as agent_mod

        repo = _init_repo(tmp_path / "repo")
        sentinel_argv = [sys.executable, "-c", "print('legacy-path')"]

        captured: list[list[str]] = []
        real_popen = agent_mod.subprocess.Popen

        def recording_popen(spawn_argv, *args, **kwargs):
            if kwargs.get("start_new_session"):
                captured.append(spawn_argv)
            return real_popen(spawn_argv, *args, **kwargs)

        server = _server(
            tmp_path, argv=sentinel_argv, repo_path=repo, bash_wrap_spawn=False
        )
        agent_mod.subprocess.Popen = recording_popen  # type: ignore[assignment]
        try:
            # spec.provider is None → no provider in registry → legacy path
            a = server.assign(_spec(repo))
            final = server.wait_for(a.id, timeout=5)
        finally:
            agent_mod.subprocess.Popen = real_popen  # type: ignore[assignment]

        # Worker makes no commits → advisory (#448)
        assert final.status == ADVISORY
        assert captured, "Popen was not called"
        # The legacy path must use sentinel_argv directly (no provider seam).
        assert captured[0] == sentinel_argv, (
            f"no-config parity: expected {sentinel_argv!r}, got {captured[0]!r}"
        )
        server.shutdown()

    def test_no_config_parity_stdin_is_user_message_line(self, tmp_path: Path) -> None:
        """With spec.provider=None, the initial stdin must be _user_message_line
        of the briefing — byte-identical to the pre-#324 path."""
        repo = _init_repo(tmp_path / "repo")
        server = _server(
            tmp_path,
            argv=[sys.executable, "-c", "import sys; print(sys.stdin.readline().rstrip(chr(10)))"],
            repo_path=repo,
        )
        # No provider set → legacy path
        a = server.assign(_spec(repo, briefing="parity-check"))
        final = server.wait_for(a.id, timeout=5)
        log = Path(final.log_path).read_text()
        # The stdin line must be a stream-json user message (same as _user_message_line)
        assert '"type": "user"' in log or '"type":"user"' in log
        assert "parity-check" in log
        server.shutdown()

    def test_provider_in_registry_uses_build_command(self, tmp_path: Path) -> None:
        """When spec.provider names a provider in the registry, _spawn calls
        provider.build_command() instead of self.worker_command()."""
        import coord.agent as agent_mod

        repo = _init_repo(tmp_path / "repo")
        provider_argv = [sys.executable, "-c", "print('provider-path')"]
        legacy_argv = [sys.executable, "-c", "print('legacy-SHOULD-NOT-APPEAR')"]
        fake_provider = _make_provider(build_argv=provider_argv)

        captured: list[list[str]] = []
        real_popen = agent_mod.subprocess.Popen

        def recording_popen(spawn_argv, *args, **kwargs):
            if kwargs.get("start_new_session"):
                captured.append(spawn_argv)
            return real_popen(spawn_argv, *args, **kwargs)

        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=tmp_path / "state",
            worker_command=lambda spec: legacy_argv,
            repo_paths={"api": str(repo)},
            providers={"myprovider": fake_provider},
            bash_wrap_spawn=False,
        )
        agent_mod.subprocess.Popen = recording_popen  # type: ignore[assignment]
        try:
            spec = _spec(repo, provider="myprovider")
            a = server.assign(spec)
            final = server.wait_for(a.id, timeout=5)
        finally:
            agent_mod.subprocess.Popen = real_popen  # type: ignore[assignment]

        # Worker makes no commits → advisory (#448)
        assert final.status == ADVISORY
        assert captured, "Popen was not called"
        assert captured[0] == provider_argv, (
            f"expected provider argv {provider_argv!r}, got {captured[0]!r}"
        )
        log = Path(final.log_path).read_text()
        assert "legacy-SHOULD-NOT-APPEAR" not in log
        assert "provider-path" in log
        server.shutdown()

    def test_provider_env_pwd_override_wins_over_worktree(self, tmp_path: Path) -> None:
        """#1783: provider.env() is merged on top of the worktree PWD, so a
        provider definition that deliberately sets PWD still overrides it."""
        import coord.agent as agent_mod

        repo = _init_repo(tmp_path / "repo")
        fake_provider = _make_provider(env_overrides={"PWD": "/deliberate/override"})

        captured_env: list[dict[str, str]] = []
        real_popen = agent_mod.subprocess.Popen

        def recording_popen(spawn_argv, *args, **kwargs):
            if kwargs.get("start_new_session"):
                captured_env.append(dict(kwargs.get("env") or {}))
            return real_popen(spawn_argv, *args, **kwargs)

        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=tmp_path / "state",
            worker_command=lambda spec: [sys.executable, "-c", "print('unused')"],
            repo_paths={"api": str(repo)},
            providers={"myprovider": fake_provider},
            bash_wrap_spawn=False,
        )
        agent_mod.subprocess.Popen = recording_popen  # type: ignore[assignment]
        try:
            spec = _spec(repo, provider="myprovider")
            a = server.assign(spec)
            final = server.wait_for(a.id, timeout=5)
        finally:
            agent_mod.subprocess.Popen = real_popen  # type: ignore[assignment]

        assert final.status == ADVISORY
        assert captured_env, "Popen was not called"
        assert captured_env[0]["PWD"] == "/deliberate/override"
        server.shutdown()

    def test_provider_unknown_name_refused_not_silently_legacy(
        self, tmp_path: Path
    ) -> None:
        """#1796: when spec.provider names a provider NOT in the registry AND
        the dispatch payload carries no provider_def, assign() must REFUSE
        the assignment — never silently substitute the legacy claude path.

        This is the exact bug #1796 exists to close: an explicitly requested
        provider that can't be honoured must fail loudly, not run a
        different backend while every surface (coordinator log, board,
        assignment record) still reports the requested name.  Before the
        fix this scenario silently ran `worker_command` (the legacy path) —
        this test would have FAILED against pre-#1796 code (it asserted the
        opposite outcome, see git history of this test).
        """
        import coord.agent as agent_mod

        repo = _init_repo(tmp_path / "repo")
        legacy_argv = [sys.executable, "-c", "print('legacy-fallback')"]
        captured: list[list[str]] = []
        real_popen = agent_mod.subprocess.Popen

        def recording_popen(spawn_argv, *args, **kwargs):
            if kwargs.get("start_new_session"):
                captured.append(spawn_argv)
            return real_popen(spawn_argv, *args, **kwargs)

        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=tmp_path / "state",
            worker_command=lambda spec: legacy_argv,
            repo_paths={"api": str(repo)},
            providers={},  # empty registry — config-free agent shape
            bash_wrap_spawn=False,
        )
        agent_mod.subprocess.Popen = recording_popen  # type: ignore[assignment]
        try:
            # No provider_def on the wire either — an older coordinator, or
            # a name with no matching providers.definitions entry.
            spec = _spec(repo, provider="nonexistent-provider")
            with pytest.raises(ValueError, match="nonexistent-provider"):
                server.assign(spec)
        finally:
            agent_mod.subprocess.Popen = real_popen  # type: ignore[assignment]

        # Nothing was ever spawned — the legacy binary must NEVER run in
        # place of an unresolvable named provider.
        assert not captured, (
            f"an unresolvable named provider must never fall back to "
            f"spawning the legacy path, but Popen was called with {captured!r}"
        )
        server.shutdown()

    @pytest.mark.posix_only
    @_posix_binary_spawn_skip
    def test_config_free_agent_executes_opencode_via_wire_provider_def(
        self, tmp_path: Path
    ) -> None:
        """#1796 acceptance: a config-free agent (empty local providers
        registry — docs/EPHEMERAL_WORKERS.md) can execute
        `--provider <opencode-type>` end-to-end, actually running the
        `opencode` binary, when the dispatch payload carries `provider_def`
        (what `coord.dispatch.dispatch` now sends — see
        `coord.providers.provider_def_to_wire`).

        Stands in for the real `opencode` binary with a stub script so the
        test has no external dependency, mirroring
        `test_provider_definition_env_reaches_actual_spawn_env`'s pattern for
        the claude provider.
        """
        import coord.agent as agent_mod

        repo = _init_repo(tmp_path / "repo")
        stub = tmp_path / "fake-opencode.sh"
        stub.write_text('#!/bin/sh\necho "ran: $0 $@"\n')
        stub.chmod(0o755)

        captured: list[list[str]] = []
        real_popen = agent_mod.subprocess.Popen

        def recording_popen(spawn_argv, *args, **kwargs):
            if kwargs.get("start_new_session"):
                captured.append(spawn_argv)
            return real_popen(spawn_argv, *args, **kwargs)

        # No `providers=` kwarg at all — config-free agent shape: an
        # ephemeral Azure worker with no local coordinator.yml has an EMPTY
        # local registry, exactly like the default here.
        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=tmp_path / "state",
            worker_command=lambda spec: [sys.executable, "-c", "print('legacy-SHOULD-NOT-RUN')"],
            repo_paths={"api": str(repo)},
            bash_wrap_spawn=False,
        )
        agent_mod.subprocess.Popen = recording_popen  # type: ignore[assignment]
        try:
            spec = _spec(
                repo,
                provider="oc-mid",
                provider_def={
                    "type": "opencode",
                    "binary": str(stub),
                    "model": None,
                    "attach_url": None,
                    "env": {},
                    "extra_args": [],
                },
            )
            a = server.assign(spec)
            final = server.wait_for(a.id, timeout=5)
        finally:
            agent_mod.subprocess.Popen = real_popen  # type: ignore[assignment]

        assert captured, "Popen was not called"
        # The binary actually executed is the opencode stub, not claude.
        assert captured[0][0] == str(stub), (
            f"expected the wire-resolved opencode binary {str(stub)!r} to be "
            f"the argv[0] actually executed, got {captured[0]!r}"
        )
        assert captured[0][1] == "run", "opencode build_command must use the 'run' subcommand"
        log = Path(final.log_path).read_text()
        assert "legacy-SHOULD-NOT-RUN" not in log, (
            "a resolvable named provider must never fall back to the legacy "
            "claude path"
        )
        # The recorded assignment's provider name matches the binary that
        # actually ran — never claude, never silently something else.
        assert a.spec.provider == "oc-mid"
        assert final.spec.provider == "oc-mid"
        server.shutdown()

    def test_provider_initial_input_reaches_worker_stdin(self, tmp_path: Path) -> None:
        """initial_input() from the provider is written to the worker's stdin."""
        repo = _init_repo(tmp_path / "repo")

        # The worker echoes its first stdin line to stdout; we capture via log.
        import json as _json
        custom_briefing = "provider-briefing-text"
        custom_bytes = (
            _json.dumps({
                "type": "user",
                "message": {"role": "user", "content": custom_briefing},
            }) + "\n"
        ).encode()

        fake_provider = _make_provider(
            build_argv=[sys.executable, "-c", "import sys; print(sys.stdin.readline().rstrip(chr(10)))"],
            initial_input_bytes=custom_bytes,
        )
        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=tmp_path / "state",
            worker_command=lambda spec: [sys.executable, "-c", "import sys; print(sys.stdin.readline().rstrip(chr(10)))"],
            repo_paths={"api": str(repo)},
            providers={"myprovider": fake_provider},
        )
        spec = _spec(repo, provider="myprovider", briefing="should-not-appear")
        a = server.assign(spec)
        final = server.wait_for(a.id, timeout=5)
        log = Path(final.log_path).read_text()
        assert custom_briefing in log, f"provider.initial_input bytes not in log: {log!r}"
        server.shutdown()

    @pytest.mark.posix_only
    @_posix_binary_spawn_skip
    def test_provider_definition_env_reaches_actual_spawn_env(self, tmp_path: Path) -> None:
        """#1706: ProviderDef.env, threaded through build_provider() into a
        real ClaudeProvider, must land in the ACTUAL spawned worker
        subprocess's environment — not just be returned by env() in
        isolation. The worker binary is a stub script that ignores its argv
        (ClaudeProvider.build_command builds a real `claude -p ...` argv the
        stub can't parse) and just echoes the env var it cares about."""
        from coord.config import ProviderDef
        from coord.providers import build_provider

        repo = _init_repo(tmp_path / "repo")
        stub = tmp_path / "fake-claude.sh"
        stub.write_text('#!/bin/sh\necho "SPAWN_ENV_FOO=$SPAWN_ENV_FOO"\n')
        stub.chmod(0o755)

        defn = ProviderDef(
            type="claude", binary=str(stub), env={"SPAWN_ENV_FOO": "bar"}
        )
        provider = build_provider("myprovider", defn, None)

        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=tmp_path / "state",
            worker_command=lambda spec: [sys.executable, "-c", "print('legacy-SHOULD-NOT-RUN')"],
            repo_paths={"api": str(repo)},
            providers={"myprovider": provider},
            bash_wrap_spawn=False,
        )
        spec = _spec(repo, provider="myprovider")
        a = server.assign(spec)
        final = server.wait_for(a.id, timeout=5)
        log = Path(final.log_path).read_text()
        assert "SPAWN_ENV_FOO=bar" in log, (
            f"provider-definition env did not reach the spawned subprocess: {log!r}"
        )
        assert "legacy-SHOULD-NOT-RUN" not in log
        server.shutdown()

    def test_local_wins_over_wire_provider_def_is_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """#2326 item 3: when `_resolve_provider` takes the local-registry
        branch over an available `spec.provider_def`, it must say so in the
        agent log, naming both — this decision used to leave no trace,
        which is why a stale local override took a live-process probe to
        catch instead of a `journalctl` grep."""
        from coord.config import ProviderDef
        from coord.providers import build_provider

        repo = _init_repo(tmp_path / "repo")
        defn = ProviderDef(type="claude", env={"LOCAL_ONLY": "1"})
        provider = build_provider("myprovider", defn, None)

        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=tmp_path / "state",
            worker_command=lambda spec: [sys.executable, "-c", "print('legacy-SHOULD-NOT-RUN')"],
            repo_paths={"api": str(repo)},
            providers={"myprovider": provider},
            bash_wrap_spawn=False,
        )
        spec = _spec(
            repo,
            provider="myprovider",
            provider_def={
                "type": "claude",
                "binary": None,
                "model": None,
                "attach_url": None,
                "env": {"FROM_WIRE": "1"},
                "extra_args": [],
            },
        )
        try:
            with caplog.at_level("INFO", logger="coord.agent"):
                resolved = server._resolve_provider(spec)
        finally:
            server.shutdown()

        assert resolved is provider
        assert "myprovider" in caplog.text
        assert "local providers.definitions" in caplog.text
        assert "wire-carried provider_def" in caplog.text
        # Env KEYS only — never the values (secrets commonly live here).
        assert "LOCAL_ONLY" in caplog.text
        assert "FROM_WIRE" in caplog.text


class TestCapabilityGates:
    """#324/#425: assign() enforces capability gates before spawning."""

    def test_deny_list_gate_refuses_work_on_unverified_provider(
        self, tmp_path: Path
    ) -> None:
        """work type on enforces_deny_list=False provider must raise ValueError."""
        repo = _init_repo(tmp_path / "repo")
        unsafe_provider = _make_provider(enforces_deny_list=False)
        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=tmp_path / "state",
            repo_paths={"api": str(repo)},
            providers={"unsafe": unsafe_provider},
        )
        with pytest.raises(ValueError, match="enforces_deny_list=False"):
            server.assign(_spec(repo, type="work", provider="unsafe"))

    def test_deny_list_gate_refuses_review_on_unverified_provider(
        self, tmp_path: Path
    ) -> None:
        """review type on enforces_deny_list=False provider must raise ValueError."""
        repo = _init_repo(tmp_path / "repo")
        unsafe_provider = _make_provider(enforces_deny_list=False)
        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=tmp_path / "state",
            repo_paths={"api": str(repo)},
            providers={"unsafe": unsafe_provider},
        )
        with pytest.raises(ValueError, match="enforces_deny_list=False"):
            server.assign(_spec(repo, type="review", provider="unsafe"))

    def test_deny_list_gate_allows_plan_on_unverified_provider(
        self, tmp_path: Path
    ) -> None:
        """plan type is non-mutating; unverified provider is allowed."""
        repo = _init_repo(tmp_path / "repo")
        # plan type is read-only — safe even on providers that don't enforce deny list
        unsafe_provider = _make_provider(
            enforces_deny_list=False,
            build_argv=[sys.executable, "-c", "import sys; sys.exit(0)"],
        )
        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=tmp_path / "state",
            repo_paths={"api": str(repo)},
            providers={"unsafe": unsafe_provider},
        )
        # Must NOT raise — plan is in non-WRITE_CAPABLE_SPEC_TYPES
        a = server.assign(_spec(repo, type="plan", provider="unsafe"))
        server.wait_for(a.id, timeout=5)
        server.shutdown()

    def test_resume_gate_refuses_when_provider_lacks_resume(
        self, tmp_path: Path
    ) -> None:
        """resume_session_id on a provider with capabilities().resume=False must raise."""
        repo = _init_repo(tmp_path / "repo")
        no_resume_provider = _make_provider(resume=False)
        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=tmp_path / "state",
            repo_paths={"api": str(repo)},
            providers={"no-resume": no_resume_provider},
        )
        with pytest.raises(ValueError, match="resume=False"):
            server.assign(
                _spec(repo, provider="no-resume", resume_session_id="ses-123")
            )

    def test_resume_gate_passes_when_provider_supports_resume(
        self, tmp_path: Path
    ) -> None:
        """resume_session_id on a provider with capabilities().resume=True is allowed."""
        repo = _init_repo(tmp_path / "repo")
        resumable_provider = _make_provider(
            resume=True,
            build_argv=[sys.executable, "-c", "import sys; sys.exit(0)"],
        )
        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=tmp_path / "state",
            repo_paths={"api": str(repo)},
            providers={"resumable": resumable_provider},
        )
        # Must NOT raise — provider supports resume
        a = server.assign(
            _spec(repo, provider="resumable", resume_session_id="ses-abc")
        )
        server.wait_for(a.id, timeout=5)
        server.shutdown()

    def test_resume_gate_no_op_when_no_provider(self, tmp_path: Path) -> None:
        """With spec.provider=None the resume gate is a no-op (legacy path)."""
        repo = _init_repo(tmp_path / "repo")
        server = _server(tmp_path, repo_path=repo)
        # resume_session_id set but no named provider → no gate, runs the legacy path
        a = server.assign(_spec(repo, resume_session_id="ses-no-gate"))
        final = server.wait_for(a.id, timeout=5)
        # No exception raised, assignment completes; no commits → advisory (#448)
        assert final.status == ADVISORY
        server.shutdown()

    def test_provider_not_in_registry_and_no_wire_def_refused(
        self, tmp_path: Path
    ) -> None:
        """#1796: when spec.provider is set but neither in the local registry
        nor resolvable from a wire-carried provider_def, assign() refuses the
        assignment outright — it never reaches (or skips) the resume gate,
        because there is no legacy fallback to reach it through anymore.

        Supersedes the old `test_resume_gate_no_op_when_provider_not_in_registry`,
        which asserted the opposite (silent no-gate legacy fallback) — that
        was the #1796 bug, not a documented feature."""
        repo = _init_repo(tmp_path / "repo")
        server = _server(tmp_path, repo_path=repo)  # no providers registry
        with pytest.raises(ValueError, match="unknown"):
            server.assign(
                _spec(repo, provider="unknown", resume_session_id="ses-no-gate")
            )
        server.shutdown()

    def test_inject_message_refused_when_provider_inject_is_false(
        self, tmp_path: Path
    ) -> None:
        """inject_message raises RuntimeError when provider.capabilities().inject=False.

        This gates stdin-injection on providers that don't expose it (e.g.
        PTY-only or batch backends) so callers get a clear error rather than
        silently writing to an unresponsive pipe (#324).
        """
        import time as _time

        repo = _init_repo(tmp_path / "repo")
        # Provider with inject=False; worker blocks on stdin so the assignment
        # stays RUNNING long enough for inject_message to be called.
        no_inject_provider = _make_provider(
            inject=False,
            build_argv=[sys.executable, "-c", "import sys; sys.stdin.readline(); print('done')"],
        )
        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=tmp_path / "state",
            repo_paths={"api": str(repo)},
            providers={"no-inject": no_inject_provider},
        )
        a = server.assign(_spec(repo, provider="no-inject"))
        # Wait until running
        for _ in range(50):
            if server.get(a.id).status == RUNNING:
                break
            _time.sleep(0.02)
        assert server.get(a.id).status == RUNNING, "assignment never reached RUNNING"
        with pytest.raises(RuntimeError, match="inject=False"):
            server.inject_message(a.id, "should be refused")
        # Clean shutdown: assignment will finish once we unblock or the server stops
        server.shutdown()

    def test_inject_message_allowed_when_provider_inject_is_true(
        self, tmp_path: Path
    ) -> None:
        """inject_message succeeds when provider.capabilities().inject=True."""
        import time as _time

        repo = _init_repo(tmp_path / "repo")
        # Provider with inject=True (the default); worker reads two lines.
        inject_provider = _make_provider(
            inject=True,
            build_argv=[sys.executable, "-c", "import sys; sys.stdin.readline(); print(sys.stdin.readline().rstrip(chr(10)))"],
        )
        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=tmp_path / "state",
            repo_paths={"api": str(repo)},
            providers={"yes-inject": inject_provider},
        )
        a = server.assign(_spec(repo, provider="yes-inject"))
        # Wait until running
        for _ in range(50):
            if server.get(a.id).status == RUNNING:
                break
            _time.sleep(0.02)
        # Should NOT raise
        server.inject_message(a.id, "injected")
        final = server.wait_for(a.id, timeout=5)
        # Worker makes no commits → advisory (#448)
        assert final.status == ADVISORY
        server.shutdown()

    def test_stdin_closed_after_spawn_when_provider_inject_is_false(
        self, tmp_path: Path
    ) -> None:
        """#2306: a provider with capabilities().inject=False (e.g. opencode,
        which takes its briefing on argv, not stdin) must have its stdin
        pipe closed right after the initial (possibly empty) write.

        Before the fix, stdin was held open for the process's entire
        lifetime regardless of provider.  A worker that never writes to or
        reads more from stdin itself (opencode) then blocked forever on its
        own read of an unclosed pipe that would never see EOF, and was
        killed by the 600s TTFT watchdog having emitted zero bytes.  ``cat``
        reproduces that shape exactly: it blocks reading stdin until EOF.
        With stdin closed after the initial write, ``cat`` sees EOF
        immediately and the assignment finishes well within the test
        timeout instead of hanging.
        """
        repo = _init_repo(tmp_path / "repo")
        no_inject_provider = _make_provider(
            inject=False,
            # argv-only briefing (like opencode) — nothing more is ever
            # written to stdin by the harness for this provider.
            build_argv=[sys.executable, "-c", "import sys; sys.stdin.read(); print('done')"],
            initial_input_bytes=b"",
        )
        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=tmp_path / "state",
            repo_paths={"api": str(repo)},
            providers={"no-inject": no_inject_provider},
        )
        a = server.assign(_spec(repo, provider="no-inject"))
        # The Popen object is available synchronously once assign() returns
        # for the non-PTY, no-pull path used here.
        proc = server._processes.get(a.id)
        assert proc is not None, "process not recorded"
        assert proc.stdin is not None and proc.stdin.closed, (
            "stdin must be closed immediately after spawn for a provider "
            "with capabilities().inject=False"
        )
        # And the worker actually completes — proof the closed pipe gave it
        # an EOF rather than leaving it to hang until the watchdog kills it.
        final = server.wait_for(a.id, timeout=5)
        assert final.status == ADVISORY  # no commits made
        server.shutdown()

    def test_stdin_left_open_after_spawn_when_provider_inject_is_true(
        self, tmp_path: Path
    ) -> None:
        """#2306 regression guard: a provider with capabilities().inject=True
        (e.g. ClaudeProvider) must still have its stdin left open after
        spawn — this is the behaviour ``inject_message`` depends on, and it
        must be bit-for-bit unchanged by the #2306 fix."""
        repo = _init_repo(tmp_path / "repo")
        inject_provider = _make_provider(
            inject=True,
            build_argv=[sys.executable, "-c", "import sys; sys.stdin.readline(); print(sys.stdin.readline().rstrip(chr(10)))"],
        )
        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=tmp_path / "state",
            repo_paths={"api": str(repo)},
            providers={"yes-inject": inject_provider},
        )
        a = server.assign(_spec(repo, provider="yes-inject"))
        proc = server._processes.get(a.id)
        assert proc is not None, "process not recorded"
        assert proc.stdin is not None and not proc.stdin.closed, (
            "stdin must stay open after spawn for a provider with "
            "capabilities().inject=True"
        )
        # inject_message must still work over that open pipe.
        server.inject_message(a.id, "injected")
        final = server.wait_for(a.id, timeout=5)
        assert final.status == ADVISORY
        server.shutdown()

    def test_stdin_left_open_after_spawn_on_legacy_no_provider_path(
        self, tmp_path: Path
    ) -> None:
        """#2306: when spec.provider is None, ``_spawn`` takes the legacy
        path where ``provider_obj`` is ``None``.  That must be treated as
        claude/inject-capable (stdin left open), matching
        ``inject_message``'s existing "no provider info => allow" fallback
        — not closed, which would regress every no-config deployment."""
        repo = _init_repo(tmp_path / "repo")
        server = _server(
            tmp_path,
            argv=[sys.executable, "-c", "import sys; sys.stdin.readline(); print(sys.stdin.readline().rstrip(chr(10)))"],
            repo_path=repo,
        )
        a = server.assign(_spec(repo))  # no provider → legacy path
        proc = server._processes.get(a.id)
        assert proc is not None, "process not recorded"
        assert proc.stdin is not None and not proc.stdin.closed, (
            "stdin must stay open after spawn on the legacy (provider_obj "
            "is None) path"
        )
        server.inject_message(a.id, "injected")
        final = server.wait_for(a.id, timeout=5)
        assert final.status == ADVISORY
        server.shutdown()


# ── #452: Completed-assignment history cap ─────────────────────────────────────


class TestCompletedHistoryCap:
    """Verify that terminal assignments are pruned to _COMPLETED_HISTORY_CAP (#452)."""

    def _make_spec(self, repo_path: Path) -> AssignmentSpec:
        return AssignmentSpec(
            repo_name="api",
            repo_path=str(repo_path),
            issue_number=1,
            issue_title="t",
            briefing="b",
            branch="main",
        )

    def test_persist_caps_terminal_assignments(self, tmp_path: Path) -> None:
        """2x-cap terminal assignments → _persist() keeps only the most
        recent _COMPLETED_HISTORY_CAP in both memory and on disk; oldest
        entries are evicted."""
        N = _COMPLETED_HISTORY_CAP * 2
        repo = _init_repo(tmp_path / "repo")
        server = _server(tmp_path, repo_path=repo)

        # Inject N synthetic terminal assignments directly (bypasses worker spawn).
        # Use monotonically increasing finished_at so "recent" is well-defined.
        spec = self._make_spec(repo)
        for i in range(N):
            a = AgentAssignment(
                id=f"cap{i:04d}",
                spec=spec,
                status=DONE,
                started_at=float(i),
                finished_at=float(i),
                exit_code=0,
            )
            server._assignments[a.id] = a

        server._persist()

        # ── In-memory state must be bounded ──────────────────────────────────
        assert len(server._assignments) <= _COMPLETED_HISTORY_CAP, (
            f"in-memory assignments not capped: {len(server._assignments)} > "
            f"{_COMPLETED_HISTORY_CAP}"
        )

        # The most recent (N - cap) entries (highest finished_at) must survive.
        kept_ids = {a.id for a in server._assignments.values()}
        for i in range(N - _COMPLETED_HISTORY_CAP, N):
            assert f"cap{i:04d}" in kept_ids, (
                f"recent assignment cap{i:04d} was incorrectly dropped"
            )

        # The oldest entries must be gone.
        for i in range(N - _COMPLETED_HISTORY_CAP):
            assert f"cap{i:04d}" not in kept_ids, (
                f"old assignment cap{i:04d} was incorrectly retained"
            )

        # ── Persisted file must be bounded ────────────────────────────────────
        state = json.loads(server.state_path.read_text())
        assert len(state["assignments"]) <= _COMPLETED_HISTORY_CAP, (
            f"persisted assignments not capped: {len(state['assignments'])} > "
            f"{_COMPLETED_HISTORY_CAP}"
        )
        file_ids = {a["id"] for a in state["assignments"]}
        for i in range(N - _COMPLETED_HISTORY_CAP, N):
            assert f"cap{i:04d}" in file_ids, (
                f"recent assignment cap{i:04d} missing from persisted state"
            )
        for i in range(N - _COMPLETED_HISTORY_CAP):
            assert f"cap{i:04d}" not in file_ids, (
                f"old assignment cap{i:04d} should not be in persisted state"
            )

    def test_active_assignments_are_never_pruned(self, tmp_path: Path) -> None:
        """Active (pending/running) assignments must not be touched by the cap,
        even when terminal count is below the cap."""
        repo = _init_repo(tmp_path / "repo")
        server = _server(tmp_path, repo_path=repo)
        spec = self._make_spec(repo)

        # Inject a mix of terminal and active assignments.
        for i in range(30):
            a = AgentAssignment(
                id=f"done{i:03d}",
                spec=spec,
                status=DONE,
                finished_at=float(i),
                exit_code=0,
            )
            server._assignments[a.id] = a

        active_id = "active-sentinel"
        server._assignments[active_id] = AgentAssignment(
            id=active_id,
            spec=spec,
            status=RUNNING,
            started_at=999.0,
        )

        server._persist()

        # Active assignment must still be in memory and on disk.
        assert active_id in server._assignments, "active assignment was pruned"
        state = json.loads(server.state_path.read_text())
        assert any(a["id"] == active_id for a in state["assignments"]), (
            "active assignment missing from persisted state"
        )

    def test_load_state_prunes_bloated_file(self, tmp_path: Path) -> None:
        """A pre-existing state file with > _COMPLETED_HISTORY_CAP terminal
        assignments is pruned in-memory immediately on load, so the first
        /status poll after restart is already bounded (#452)."""
        N = 80  # above the cap
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        # Write a bloated state file directly (simulates a pre-fix agent).
        spec_dict = {
            "repo_name": "api",
            "repo_path": str(tmp_path),
            "issue_number": 1,
            "issue_title": "t",
            "briefing": "b",
            "files_allowed": [],
            "files_forbidden": [],
            "branch": "main",
        }
        assignments = []
        for i in range(N):
            assignments.append({
                "id": f"old{i:04d}",
                "status": DONE,
                "pid": None,
                "started_at": float(i),
                "finished_at": float(i),
                "exit_code": 0,
                "log_path": None,
                "error": None,
                "branch": None,
                "worktree_path": None,
                "claude_session_id": None,
                "spec": dict(spec_dict),
            })
        (state_dir / "agent_state.json").write_text(
            json.dumps({"machine": "test", "capabilities": [], "repos": ["api"],
                        "assignments": assignments})
        )

        # Instantiate the server — _load_state() should prune immediately.
        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=state_dir,
        )

        assert len(server._assignments) <= _COMPLETED_HISTORY_CAP, (
            f"_load_state() did not prune bloated file: "
            f"{len(server._assignments)} > {_COMPLETED_HISTORY_CAP}"
        )
        # The most recent entries (highest finished_at) must be kept.
        kept_ids = set(server._assignments.keys())
        for i in range(N - _COMPLETED_HISTORY_CAP, N):  # old0030 … old0079
            assert f"old{i:04d}" in kept_ids, (
                f"recent assignment old{i:04d} was incorrectly dropped on load"
            )

    def test_list_assignments_completed_is_bounded(self, tmp_path: Path) -> None:
        """list_assignments()['completed'] must not exceed the cap after _persist."""
        repo = _init_repo(tmp_path / "repo")
        server = _server(tmp_path, repo_path=repo)
        spec = self._make_spec(repo)

        for i in range(70):
            a = AgentAssignment(
                id=f"la{i:04d}",
                spec=spec,
                status=DONE,
                finished_at=float(i),
                exit_code=0,
            )
            server._assignments[a.id] = a

        server._persist()  # triggers in-memory prune

        listing = server.list_assignments()
        assert len(listing["completed"]) <= _COMPLETED_HISTORY_CAP, (
            f"list_assignments() returned {len(listing['completed'])} completed items, "
            f"expected ≤ {_COMPLETED_HISTORY_CAP}"
        )


# ── #1421: _persist() must not race on a shared tmp file, and a corrupt
# agent_state.json must be surfaced rather than silently discarded ───────────

class TestPersistRobustness:
    def _make_spec(self, repo_path: Path) -> AssignmentSpec:
        return AssignmentSpec(
            repo_name="api",
            repo_path=str(repo_path),
            issue_number=1,
            issue_title="t",
            briefing="b",
            branch="main",
        )

    def test_concurrent_persist_never_corrupts_state_file(self, tmp_path: Path) -> None:
        """Many threads calling _persist() concurrently must never leave
        agent_state.json unparseable.

        The pre-fix code staged every write through one shared, fixed
        ``agent_state.json.tmp`` name outside the lock: thread A's
        ``write_text()`` could be truncated mid-write by thread B opening the
        *same* tmp path, and thread A's subsequent ``os.replace()`` would then
        promote that truncated file into place. A reader polling the file
        concurrently would occasionally see exactly the
        ``JSONDecodeError: Expecting value: line 1 column 1 (char 0)`` from
        the bug report. Looped heavily so it would have caught the race
        reliably pre-fix; with the unique-tempfile fix each thread stages its
        own file, so ``os.replace()`` is always promoting a complete write.
        """
        repo = _init_repo(tmp_path / "repo")
        server = _server(tmp_path, repo_path=repo)
        spec = self._make_spec(repo)
        for i in range(5):
            a = AgentAssignment(
                id=f"seed{i}", spec=spec, status=DONE, finished_at=float(i), exit_code=0,
            )
            server._assignments[a.id] = a
        server._persist()  # file exists before concurrent access starts

        stop = threading.Event()
        errors: list[str] = []
        errors_lock = threading.Lock()

        def persister() -> None:
            for _ in range(60):
                server._persist()

        def reader() -> None:
            while not stop.is_set():
                try:
                    json.loads(server.state_path.read_text())
                except json.JSONDecodeError as e:
                    with errors_lock:
                        errors.append(str(e))
                except FileNotFoundError:
                    # Not the bug under test: os.replace() is atomic, so the
                    # destination always resolves to a complete file once it
                    # first exists (guaranteed by the _persist() call above).
                    pass

        persisters = [threading.Thread(target=persister) for _ in range(8)]
        readers = [threading.Thread(target=reader) for _ in range(4)]
        for t in readers:
            t.start()
        for t in persisters:
            t.start()
        for t in persisters:
            t.join()
        stop.set()
        for t in readers:
            t.join()

        assert not errors, (
            f"agent_state.json failed to parse under concurrent _persist() "
            f"(the #1421 race): {errors}"
        )
        # The file must still be intact and complete afterwards too.
        final = json.loads(server.state_path.read_text())
        assert len(final["assignments"]) == 5

    def test_persist_failure_is_logged_not_silently_swallowed(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A _persist() that can't write (state_dir gone) must log at ERROR
        instead of the old bare ``except (FileNotFoundError, OSError): pass``
        — a persist that silently fails is the other half of the #1421 blind
        spot."""
        repo = _init_repo(tmp_path / "repo")
        server = _server(tmp_path, repo_path=repo)
        shutil.rmtree(server.state_dir)

        with caplog.at_level("ERROR", logger="coord.agent"):
            server._persist()  # must not raise

        assert "failed to persist" in caplog.text.lower()

    def test_corrupt_state_file_is_logged_and_moved_aside(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A corrupt agent_state.json (e.g. left behind by the #1421 race, or
        any other on-disk corruption) must be logged loudly and moved aside on
        load — not silently discarded, which is what converted the original
        race into invisible assignment-state loss on restart."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        state_path = state_dir / "agent_state.json"
        garbage = "not valid json{"
        state_path.write_text(garbage)

        with caplog.at_level("ERROR", logger="coord.agent"):
            server = AgentServer(machine_name="test", repos=["api"], state_dir=state_dir)

        assert "corrupt" in caplog.text.lower()
        assert server._assignments == {}

        # The garbage must not still be sitting at the canonical path (ready
        # to be silently clobbered by the next _persist()) — it must have
        # been moved aside, recoverable and diagnosable.
        assert not state_path.exists()
        corrupt_files = list(state_dir.glob("agent_state.json.corrupt-*"))
        assert len(corrupt_files) == 1, (
            f"expected exactly one moved-aside corrupt file, found {corrupt_files}"
        )
        assert corrupt_files[0].read_text() == garbage


# ── #715: /status payload stays lean regardless of history size ───────────────


class TestStatusPayloadSize:
    """`/status` latency must be decoupled from history size — terminal
    entries drop their (potentially huge) briefing/system_prompt text, since
    no coordinator reader consumes it from a terminal entry (#715)."""

    def _make_spec(self, repo_path: Path, *, briefing: str) -> AssignmentSpec:
        return AssignmentSpec(
            repo_name="api",
            repo_path=str(repo_path),
            issue_number=1,
            issue_title="t",
            briefing=briefing,
            system_prompt="x" * 2_000,
            branch="main",
        )

    def test_to_status_dict_strips_briefing_for_terminal_status(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        big_briefing = "B" * 20_000
        spec = self._make_spec(repo, briefing=big_briefing)
        for status in (DONE, FAILED, CANCELLED, ADVISORY):
            a = AgentAssignment(
                id=f"term-{status}", spec=spec, status=status,
                finished_at=1.0, exit_code=0,
            )
            d = a.to_status_dict()
            assert d["spec"]["briefing"] == "", f"briefing not stripped for status={status}"
            assert d["spec"]["system_prompt"] is None, (
                f"system_prompt not stripped for status={status}"
            )
            # The live in-memory object must be untouched — only the
            # serialized copy is slimmed.
            assert a.spec.briefing == big_briefing

    def test_to_status_dict_keeps_briefing_for_active_status(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        big_briefing = "B" * 20_000
        spec = self._make_spec(repo, briefing=big_briefing)
        for status in (PENDING, RUNNING):
            a = AgentAssignment(id=f"active-{status}", spec=spec, status=status, started_at=1.0)
            d = a.to_status_dict()
            assert d["spec"]["briefing"] == big_briefing, (
                f"briefing unexpectedly stripped for status={status}"
            )

    def test_status_payload_bounded_with_large_history(self, tmp_path: Path) -> None:
        """200 terminal assignments with a large (20KB) briefing each — the
        real-world trigger was 50 entries x a full briefing at ~0.9MB / ~3s
        (#715) — must serialize small and fast now that terminal entries are
        slimmed, independent of _COMPLETED_HISTORY_CAP (tested separately
        above; here every entry is injected directly, bypassing _persist(),
        so this isolates the per-entry size fix)."""
        N = 200
        big_briefing = "B" * 20_000
        repo = _init_repo(tmp_path / "repo")
        server = _server(tmp_path, repo_path=repo)
        spec = self._make_spec(repo, briefing=big_briefing)

        for i in range(N):
            a = AgentAssignment(
                id=f"big{i:04d}",
                spec=spec,
                status=DONE,
                started_at=float(i),
                finished_at=float(i),
                exit_code=0,
            )
            server._assignments[a.id] = a

        start = time.perf_counter()
        listing = server.list_assignments()
        payload = json.dumps(listing)
        elapsed = time.perf_counter() - start

        assert len(listing["completed"]) == N
        assert big_briefing not in payload, "a full briefing leaked into the /status payload"
        assert len(payload) < 300_000, (
            f"/status payload too large: {len(payload)} bytes for {N} terminal "
            "entries — briefing stripping appears to have regressed"
        )
        assert elapsed < 1.0, (
            f"list_assignments() + json.dumps() took {elapsed:.2f}s for {N} "
            "terminal entries — should be well under the coordinator's poll timeout"
        )


# ── #667: list_assignments includes token counts in completed entries ──────────


class TestListAssignmentsTokens:
    """list_assignments()['completed'] entries include token counts parsed from
    the worker log so the coordinator can capture them without the log file."""

    def _make_spec(self, repo_path: Path) -> AssignmentSpec:
        return AssignmentSpec(
            repo_name="api",
            repo_path=str(repo_path),
            issue_number=1,
            issue_title="t",
            briefing="b",
            branch="main",
        )

    def _write_stream_json_log(
        self,
        log_path: Path,
        *,
        cost: float = 0.10,
        input_tokens: int = 500,
        output_tokens: int = 100,
        cache_creation_tokens: int = 20,
        cache_read_tokens: int = 80,
    ) -> None:
        """Write a minimal stream-json result event so parse_log picks it up."""
        import json as _json

        payload = {
            "type": "result",
            "subtype": "success",
            "result": "done",
            "total_cost_usd": cost,
            "num_turns": 2,
            "duration_ms": 5000,
            "session_id": "test-session",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_tokens": cache_creation_tokens,
            "cache_read_tokens": cache_read_tokens,
        }
        log_path.write_text(_json.dumps(payload) + "\n", encoding="utf-8")

    def test_token_counts_appear_in_completed_entry(self, tmp_path: Path) -> None:
        """When a completed assignment's stream-json log has token counts,
        list_assignments() includes them in the completed entry dict."""
        repo = _init_repo(tmp_path / "repo")
        server = _server(tmp_path, repo_path=repo)
        spec = self._make_spec(repo)

        log_path = tmp_path / "tok1.log"
        self._write_stream_json_log(
            log_path,
            input_tokens=1234,
            output_tokens=567,
            cache_creation_tokens=89,
            cache_read_tokens=321,
        )

        a = AgentAssignment(
            id="tok1",
            spec=spec,
            status=DONE,
            finished_at=1.0,
            exit_code=0,
            log_path=str(log_path),
        )
        server._assignments[a.id] = a

        listing = server.list_assignments()
        completed = listing["completed"]
        assert len(completed) == 1
        entry = completed[0]
        assert entry.get("input_tokens") == 1234
        assert entry.get("output_tokens") == 567
        assert entry.get("cache_creation_tokens") == 89
        assert entry.get("cache_read_tokens") == 321

    def test_no_tokens_when_log_absent(self, tmp_path: Path) -> None:
        """When the log path is absent, completed entry has no token fields
        (the coordinator handles missing keys gracefully)."""
        repo = _init_repo(tmp_path / "repo")
        server = _server(tmp_path, repo_path=repo)
        spec = self._make_spec(repo)

        a = AgentAssignment(
            id="tok2",
            spec=spec,
            status=DONE,
            finished_at=1.0,
            exit_code=0,
            log_path=None,
        )
        server._assignments[a.id] = a

        listing = server.list_assignments()
        completed = listing["completed"]
        assert len(completed) == 1
        entry = completed[0]
        # No token keys expected when log is absent — coordinator should
        # treat missing keys as 0, same as older agents.
        assert entry.get("input_tokens", 0) == 0


# ── #2316: stop_reason / truncation_reason reach /status ───────────────────────


class TestStopReasonInCompletedEntry:
    """`list_assignments()['completed']` must carry `stop_reason` (parsed
    fresh from the log on every /status call, same as `num_turns`/
    `total_cost_usd` above) so `coord.reconcile._capture_stop_reason_best_
    effort` has something to persist — this is the wire half of #2316's
    "the information is already there and is thrown away" gap."""

    def _make_spec(self, repo_path: Path) -> AssignmentSpec:
        return AssignmentSpec(
            repo_name="api",
            repo_path=str(repo_path),
            issue_number=2316,
            issue_title="t",
            briefing="b",
            branch="main",
        )

    def test_stop_reason_appears_in_completed_entry(self, tmp_path: Path) -> None:
        import json as _json

        repo = _init_repo(tmp_path / "repo")
        server = _server(tmp_path, repo_path=repo)
        spec = self._make_spec(repo)

        log_path = tmp_path / "trunc1.log"
        log_path.write_text(
            _json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "stop_reason": "max_tokens",
                    "result": "",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        a = AgentAssignment(
            id="trunc1",
            spec=spec,
            status=FAILED,
            finished_at=1.0,
            exit_code=0,
            log_path=str(log_path),
            truncation_reason=(
                "the model was cut off at its output limit before writing "
                "anything (stop_reason='max_tokens')"
            ),
            error=(
                "the model was cut off at its output limit before writing "
                "anything (stop_reason='max_tokens')"
            ),
        )
        server._assignments[a.id] = a

        listing = server.list_assignments()
        completed = listing["completed"]
        assert len(completed) == 1
        entry = completed[0]
        # `stop_reason`: freshly parsed from the log on every /status call.
        assert entry.get("stop_reason") == "max_tokens"
        # `truncation_reason`/`error`: plain dataclass fields, already on the
        # assignment — `to_status_dict()`'s `asdict` carries them through
        # exactly like `usage_limit_reason`/`api_error_reason` do.
        assert entry.get("truncation_reason") is not None
        assert "cut off at its output limit" in entry["truncation_reason"]
        assert entry.get("error") == entry["truncation_reason"]


# ── #1492: agent-side clearing of terminal ADVISORY entries ────────────────────


class TestAdvisoryTerminalPrune:
    """#1472 filtered a stale ADVISORY entry (0-commit work whose issue later
    closed / branch merged out of band) at render time in `coord status`, but
    the agent itself kept re-serving it on every `/status` poll forever —
    every other reader (dashboard, TUI, any future client) had to reimplement
    the same filter. `AgentServer._prune_terminal_advisory` fixes this at the
    source: the agent drops the entry from its own state once it confirms the
    work is terminal on GitHub, so nothing downstream ever sees it again.
    """

    def _make_spec(self, repo_path: Path, **overrides) -> AssignmentSpec:
        base = dict(
            repo_name="api",
            repo_path=str(repo_path),
            issue_number=1492,
            issue_title="t",
            briefing="b",
            branch="issue-1492-fix",
        )
        base.update(overrides)
        return AssignmentSpec(**base)

    def _add_github_remote(self, repo_path: Path, slug: str = "acme/widgets") -> None:
        subprocess.run(
            ["git", "remote", "add", "origin", f"git@github.com:{slug}.git"],
            cwd=str(repo_path), check=True, capture_output=True,
        )

    def test_dropped_when_work_is_terminal(self, tmp_path: Path, monkeypatch) -> None:
        """An advisory entry whose issue/branch is confirmed terminal on
        GitHub is removed from the agent's own state — not just hidden."""
        repo = _init_repo(tmp_path / "repo")
        self._add_github_remote(repo)
        server = _server(tmp_path, repo_path=repo)
        spec = self._make_spec(repo)

        a = AgentAssignment(
            id="adv-1", spec=spec, status=ADVISORY, finished_at=1.0, exit_code=0,
            branch="issue-1492-fix",
        )
        server._assignments[a.id] = a

        from coord import github_ops
        monkeypatch.setattr(github_ops, "work_is_terminal", lambda *args, **kwargs: True)

        server._prune_terminal_advisory()

        assert "adv-1" not in server._assignments
        listing = server.list_assignments()
        assert listing["completed"] == []

    def test_kept_when_work_still_live(self, tmp_path: Path, monkeypatch) -> None:
        """A genuine, still-live advisory (issue open, branch unmerged) must
        not be dropped."""
        repo = _init_repo(tmp_path / "repo")
        self._add_github_remote(repo)
        server = _server(tmp_path, repo_path=repo)
        spec = self._make_spec(repo)

        a = AgentAssignment(
            id="adv-2", spec=spec, status=ADVISORY, finished_at=1.0, exit_code=0,
            branch="issue-1492-fix",
        )
        server._assignments[a.id] = a

        from coord import github_ops
        monkeypatch.setattr(github_ops, "work_is_terminal", lambda *args, **kwargs: False)

        server._prune_terminal_advisory()

        assert "adv-2" in server._assignments
        listing = server.list_assignments()
        assert len(listing["completed"]) == 1

    def test_kept_when_no_github_remote(self, tmp_path: Path, monkeypatch) -> None:
        """Fail-open: a repo with no (or a non-GitHub) origin remote can't be
        checked, so the entry is left in place rather than guessed away."""
        repo = _init_repo(tmp_path / "repo")  # no `origin` remote configured
        server = _server(tmp_path, repo_path=repo)
        spec = self._make_spec(repo)

        a = AgentAssignment(
            id="adv-3", spec=spec, status=ADVISORY, finished_at=1.0, exit_code=0,
            branch="issue-1492-fix",
        )
        server._assignments[a.id] = a

        from coord import github_ops
        calls = []
        monkeypatch.setattr(
            github_ops, "work_is_terminal",
            lambda *args, **kwargs: calls.append((args, kwargs)) or True,
        )

        server._prune_terminal_advisory()

        assert "adv-3" in server._assignments
        assert calls == [], "work_is_terminal must not be called without a resolvable slug"

    def test_rate_limited_across_polls(self, tmp_path: Path, monkeypatch) -> None:
        """Two `/status` polls in quick succession cost exactly one GitHub
        terminality sweep, not one per poll (#1492's #1472-mirrored fail-open
        cost concern) — verified here via `list_assignments()`, the actual
        `/status` handler entry point."""
        repo = _init_repo(tmp_path / "repo")
        self._add_github_remote(repo)
        server = _server(tmp_path, repo_path=repo)
        spec = self._make_spec(repo)

        a = AgentAssignment(
            id="adv-4", spec=spec, status=ADVISORY, finished_at=1.0, exit_code=0,
            branch="issue-1492-fix",
        )
        server._assignments[a.id] = a

        from coord import github_ops
        calls = []

        def _fake_terminal(*args, **kwargs):
            calls.append((args, kwargs))
            return False

        monkeypatch.setattr(github_ops, "work_is_terminal", _fake_terminal)

        server.list_assignments()
        server.list_assignments()

        assert len(calls) == 1, (
            f"expected exactly one GitHub sweep across two polls within the "
            f"cooldown window, got {len(calls)}"
        )

    def test_persisted_state_no_longer_carries_settled_advisory(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """The drop is written through to agent_state.json too — a restarted
        agent must not resurrect the settled advisory entry from disk."""
        repo = _init_repo(tmp_path / "repo")
        self._add_github_remote(repo)
        server = _server(tmp_path, repo_path=repo)
        spec = self._make_spec(repo)

        a = AgentAssignment(
            id="adv-5", spec=spec, status=ADVISORY, finished_at=1.0, exit_code=0,
            branch="issue-1492-fix",
        )
        server._assignments[a.id] = a
        server._persist()

        from coord import github_ops
        monkeypatch.setattr(github_ops, "work_is_terminal", lambda *args, **kwargs: True)

        server._prune_terminal_advisory()

        state = json.loads(server.state_path.read_text())
        assert all(entry["id"] != "adv-5" for entry in state["assignments"])


# ── #1468: agent-side clearing of an advisory superseded by a later retry ──


class TestSupersededAdvisoryPrune:
    """#1468: a rescued WIP commit (see `_rescue_uncommitted_work`) can leave
    an assignment ADVISORY ("UNVERIFIED — review before merging"). When a
    *later* assignment for the same issue reaches DONE, that advisory is
    stale, but `_prune_terminal_advisory` (#1492) only clears it once GitHub
    shows the issue closed or the PR merged — a signal that doesn't exist yet
    in the window between "retry finished" and "PR merged". `_prune_
    superseded_advisory` closes exactly that gap, purely from in-agent state
    (no GitHub round-trip).
    """

    def _make_spec(self, repo_path: Path, *, issue_number: int = 1468, **overrides) -> AssignmentSpec:
        base = dict(
            repo_name="api",
            repo_path=str(repo_path),
            issue_number=issue_number,
            issue_title="t",
            briefing="b",
            branch="issue-1468-fix",
        )
        base.update(overrides)
        return AssignmentSpec(**base)

    def test_dropped_when_superseded_by_later_done_same_issue(self, tmp_path: Path) -> None:
        """A later DONE assignment for the same issue clears the earlier
        ADVISORY entry, even on a different branch (a retry may use
        `fresh_branch`)."""
        repo = _init_repo(tmp_path / "repo")
        server = _server(tmp_path, repo_path=repo)

        adv = AgentAssignment(
            id="adv-1", spec=self._make_spec(repo, branch="issue-1468-fix"),
            status=ADVISORY, finished_at=1.0, exit_code=0,
            branch="issue-1468-fix",
        )
        server._assignments[adv.id] = adv

        done = AgentAssignment(
            id="done-1", spec=self._make_spec(repo, branch="issue-1468-fix-retry"),
            status=DONE, finished_at=2.0, exit_code=0,
            branch="issue-1468-fix-retry",
        )
        server._assignments[done.id] = done

        server._prune_superseded_advisory()

        assert "adv-1" not in server._assignments
        assert "done-1" in server._assignments
        listing = server.list_assignments()
        ids = {e["id"] for e in listing["completed"]}
        assert "adv-1" not in ids
        assert "done-1" in ids

    def test_kept_when_no_later_done(self, tmp_path: Path) -> None:
        """A still-live advisory with no later DONE for its issue survives."""
        repo = _init_repo(tmp_path / "repo")
        server = _server(tmp_path, repo_path=repo)

        adv = AgentAssignment(
            id="adv-2", spec=self._make_spec(repo), status=ADVISORY,
            finished_at=1.0, exit_code=0, branch="issue-1468-fix",
        )
        server._assignments[adv.id] = adv

        server._prune_superseded_advisory()

        assert "adv-2" in server._assignments

    def test_kept_when_done_precedes_advisory(self, tmp_path: Path) -> None:
        """Order matters: a DONE entry dispatched BEFORE the advisory does
        not count as "later" — it can't have superseded a failure that
        hadn't happened yet."""
        repo = _init_repo(tmp_path / "repo")
        server = _server(tmp_path, repo_path=repo)

        done = AgentAssignment(
            id="done-2", spec=self._make_spec(repo), status=DONE,
            finished_at=1.0, exit_code=0, branch="issue-1468-fix",
        )
        server._assignments[done.id] = done

        adv = AgentAssignment(
            id="adv-3", spec=self._make_spec(repo), status=ADVISORY,
            finished_at=2.0, exit_code=0, branch="issue-1468-fix-2",
        )
        server._assignments[adv.id] = adv

        server._prune_superseded_advisory()

        assert "adv-3" in server._assignments

    def test_kept_when_different_issue(self, tmp_path: Path) -> None:
        """A later DONE for a DIFFERENT issue must not clear this advisory."""
        repo = _init_repo(tmp_path / "repo")
        server = _server(tmp_path, repo_path=repo)

        adv = AgentAssignment(
            id="adv-4", spec=self._make_spec(repo, issue_number=1468),
            status=ADVISORY, finished_at=1.0, exit_code=0,
            branch="issue-1468-fix",
        )
        server._assignments[adv.id] = adv

        done = AgentAssignment(
            id="done-3", spec=self._make_spec(repo, issue_number=9999),
            status=DONE, finished_at=2.0, exit_code=0,
            branch="issue-9999-other",
        )
        server._assignments[done.id] = done

        server._prune_superseded_advisory()

        assert "adv-4" in server._assignments

    def test_kept_when_different_repo_same_issue_number(self, tmp_path: Path) -> None:
        """Issue numbers are only unique per-repo — a DONE for the same
        issue *number* in a different repo must not clear this advisory."""
        repo = _init_repo(tmp_path / "repo")
        server = _server(tmp_path, repo_path=repo)

        adv = AgentAssignment(
            id="adv-5", spec=self._make_spec(repo, repo_name="api"),
            status=ADVISORY, finished_at=1.0, exit_code=0,
            branch="issue-1468-fix",
        )
        server._assignments[adv.id] = adv

        done = AgentAssignment(
            id="done-4", spec=self._make_spec(repo, repo_name="other"),
            status=DONE, finished_at=2.0, exit_code=0,
            branch="issue-1468-fix",
        )
        server._assignments[done.id] = done

        server._prune_superseded_advisory()

        assert "adv-5" in server._assignments

    def test_persisted_state_no_longer_carries_superseded_advisory(self, tmp_path: Path) -> None:
        """The drop is written through to agent_state.json too — a restarted
        agent must not resurrect the superseded advisory entry from disk."""
        repo = _init_repo(tmp_path / "repo")
        server = _server(tmp_path, repo_path=repo)

        adv = AgentAssignment(
            id="adv-6", spec=self._make_spec(repo), status=ADVISORY,
            finished_at=1.0, exit_code=0, branch="issue-1468-fix",
        )
        server._assignments[adv.id] = adv
        server._persist()

        done = AgentAssignment(
            id="done-5", spec=self._make_spec(repo), status=DONE,
            finished_at=2.0, exit_code=0, branch="issue-1468-fix-retry",
        )
        server._assignments[done.id] = done

        server._prune_superseded_advisory()

        state = json.loads(server.state_path.read_text())
        assert all(entry["id"] != "adv-6" for entry in state["assignments"])


# ── #2234: REFUSED_POLICY gets the same agent-side pruning as ADVISORY ──────


class TestRefusedPolicyPrune:
    """`_prune_terminal_advisory`/`_prune_superseded_advisory` only ever
    scanned `status == ADVISORY` — a `refused_policy` entry (`coord.agent.
    REFUSED_POLICY`, the #2234 shape) was never pruned, so the agent would
    keep serving it indefinitely on `/status` even after it went terminal
    on GitHub or was superseded by a later retry, same root cause as the
    two ADVISORY prune bugs above.
    """

    def _make_spec(self, repo_path: Path, **overrides) -> AssignmentSpec:
        base = dict(
            repo_name="api",
            repo_path=str(repo_path),
            issue_number=2234,
            issue_title="t",
            briefing="b",
            branch="issue-2234-fix",
        )
        base.update(overrides)
        return AssignmentSpec(**base)

    def _add_github_remote(self, repo_path: Path, slug: str = "acme/widgets") -> None:
        subprocess.run(
            ["git", "remote", "add", "origin", f"git@github.com:{slug}.git"],
            cwd=str(repo_path), check=True, capture_output=True,
        )

    def test_terminal_prune_drops_refused_policy_when_work_is_terminal(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """`_prune_terminal_advisory` must also drop a REFUSED_POLICY entry
        once GitHub confirms the work is terminal."""
        repo = _init_repo(tmp_path / "repo")
        self._add_github_remote(repo)
        server = _server(tmp_path, repo_path=repo)
        spec = self._make_spec(repo)

        a = AgentAssignment(
            id="rp-1", spec=spec, status=REFUSED_POLICY, finished_at=1.0,
            exit_code=0, branch="issue-2234-fix",
        )
        server._assignments[a.id] = a

        from coord import github_ops
        monkeypatch.setattr(github_ops, "work_is_terminal", lambda *args, **kwargs: True)

        server._prune_terminal_advisory()

        assert "rp-1" not in server._assignments
        listing = server.list_assignments()
        assert listing["completed"] == []

    def test_superseded_prune_drops_refused_policy_when_later_done_same_issue(
        self, tmp_path: Path,
    ) -> None:
        """`_prune_superseded_advisory` must also drop a REFUSED_POLICY entry
        superseded by a later DONE retry for the same issue — e.g. the
        coordinator re-scopes the issue so a later dispatch's deliverable is
        no longer coordinator-only, and that later attempt reaches DONE."""
        repo = _init_repo(tmp_path / "repo")
        server = _server(tmp_path, repo_path=repo)

        refused = AgentAssignment(
            id="rp-2", spec=self._make_spec(repo, branch="issue-2234-fix"),
            status=REFUSED_POLICY, finished_at=1.0, exit_code=0,
            branch="issue-2234-fix",
        )
        server._assignments[refused.id] = refused

        done = AgentAssignment(
            id="done-rp", spec=self._make_spec(repo, branch="issue-2234-fix-retry"),
            status=DONE, finished_at=2.0, exit_code=0,
            branch="issue-2234-fix-retry",
        )
        server._assignments[done.id] = done

        server._prune_superseded_advisory()

        assert "rp-2" not in server._assignments
        assert "done-rp" in server._assignments


# ── #2299: hot config reload (adding a repo must not need a restart) ─────────

def _write_config(
    path: Path,
    *,
    repos: list[str],
    repo_paths: dict[str, str],
    capabilities: list[str] | None = None,
    artifact_paths: dict[str, list[str]] | None = None,
    build_commands: dict[str, str] | None = None,
    machine_name: str = "test",
) -> Path:
    """Write a minimal coordinator.yml declaring *machine_name* with *repos*."""
    artifact_paths = artifact_paths or {}
    build_commands = build_commands or {}
    lines = ["repos:"]
    for name in repos:
        lines.append(f"  - name: {name}")
        lines.append(f"    github: acme/{name}")
        if name in build_commands:
            lines.append(f"    build_command: {build_commands[name]!r}")
        if name in artifact_paths:
            lines.append("    artifact_paths:")
            lines.extend(f"      - {p!r}" for p in artifact_paths[name])
    lines.append("")
    lines.append("machines:")
    lines.append(f"  - name: {machine_name}")
    lines.append(f"    host: {machine_name}.tailnet")
    lines.append(f"    capabilities: [{', '.join(capabilities or ['python'])}]")
    lines.append(f"    repos: [{', '.join(repos)}]")
    lines.append("    repo_paths:")
    for name, p in repo_paths.items():
        lines.append(f"      {name}: {p!r}")
    path.write_text("\n".join(lines) + "\n")
    return path


def _bump_mtime(path: Path, seconds_ahead: float = 5.0) -> None:
    """Force the on-disk mtime forward so a same-second rewrite is still seen.

    Some filesystems have 1s mtime resolution, so a write immediately followed
    by another write inside one test can produce an identical mtime — which
    the reload guard would (correctly) treat as unchanged. Mirrors the helper
    of the same name in ``tests/test_serve.py`` for #1081.
    """
    new_time = path.stat().st_mtime + seconds_ahead
    os.utime(path, (new_time, new_time))


def _write_config_with_provider(
    path: Path,
    *,
    repos: list[str],
    repo_paths: dict[str, str],
    provider_binary: str,
    provider_env: dict[str, str] | None = None,
    machine_name: str = "test",
) -> Path:
    """Write a minimal coordinator.yml with one `providers.definitions`
    entry named "myprovider" (#2326 hot-reload coverage)."""
    provider_env = provider_env or {}
    lines = ["repos:"]
    for name in repos:
        lines.append(f"  - name: {name}")
        lines.append(f"    github: acme/{name}")
    lines.append("")
    lines.append("providers:")
    lines.append("  definitions:")
    lines.append("    myprovider:")
    lines.append("      type: claude")
    lines.append(f"      binary: {provider_binary!r}")
    if provider_env:
        lines.append("      env:")
        for k, v in provider_env.items():
            lines.append(f"        {k}: {v!r}")
    lines.append("")
    lines.append("machines:")
    lines.append(f"  - name: {machine_name}")
    lines.append(f"    host: {machine_name}.tailnet")
    lines.append("    capabilities: [python]")
    lines.append(f"    repos: [{', '.join(repos)}]")
    lines.append("    repo_paths:")
    for name, p in repo_paths.items():
        lines.append(f"      {name}: {p!r}")
    path.write_text("\n".join(lines) + "\n")
    return path


class TestConfigHotReload:
    """#2299: `coordinator.yml` edits land within one health tick, no restart.

    The pre-#2299 agent froze `repos`/`repo_paths`/`capabilities`/... at
    process start, so adding a repo to the fleet required `systemctl --user
    restart coord-agent` on every machine that should serve it — the same
    action that kills live workers. Worse, the resulting skew was invisible
    from the operator's side: `coord config`, `coord status` and `coord assign
    --dry-run` all read the file and reported the repo as supported while
    `assign()` refused every dispatch for it.
    """

    def _build(self, tmp_path: Path, **kwargs):
        """A server whose config declares only `api`, plus a `web` repo on disk."""
        from coord.config import load as load_config

        api = _init_repo(tmp_path / "api")
        web = _init_repo(tmp_path / "web")
        cfg_path = _write_config(
            tmp_path / "coordinator.yml",
            repos=["api"],
            repo_paths={"api": str(api)},
        )
        cfg = load_config(cfg_path)
        server = AgentServer(
            machine_name="test",
            capabilities=["python"],
            repos=["api"],
            state_dir=tmp_path / "state",
            worker_command=lambda spec: [sys.executable, "-c", "print('worker-output')"],
            repo_paths={"api": str(api)},
            health_config=cfg,
            worktree_writable_settings_files=[],
            **kwargs,
        )
        return server, cfg_path, api, web

    # ── the acceptance case ─────────────────────────────────────────────────

    def test_repo_added_on_disk_is_dispatchable_without_restart(
        self, tmp_path: Path
    ) -> None:
        """Black-box acceptance: agent serving {api}, config edited to
        {api, web} on disk, dispatch for `web` succeeds — no restart."""
        server, cfg_path, api, web = self._build(tmp_path)
        try:
            with pytest.raises(ValueError, match="does not handle repo 'web'"):
                server.assign(_spec(web, repo_name="web"))

            _write_config(
                cfg_path,
                repos=["api", "web"],
                repo_paths={"api": str(api), "web": str(web)},
            )
            _bump_mtime(cfg_path)

            a = server.assign(_spec(web, repo_name="web"))
            final = server.wait_for(a.id)
            assert final.exit_code == 0
            assert server.repos == ["api", "web"]
            assert server.repo_paths["web"] == str(web)
        finally:
            server.shutdown()

    def test_health_advertises_new_repo_after_reload(self, tmp_path: Path) -> None:
        """`/health` must publish the post-reload repo list, so `coord repo
        doctor` stops reporting `machines.agent_repo_skew` on its own."""
        server, cfg_path, api, web = self._build(tmp_path)
        try:
            assert server.health()["repos"] == ["api"]

            _write_config(
                cfg_path,
                repos=["api", "web"],
                repo_paths={"api": str(api), "web": str(web)},
            )
            _bump_mtime(cfg_path)

            health = server.health()
            assert health["repos"] == ["api", "web"]
            assert health["degraded"] == {}
            assert health["config_reload"]["reloads"] == 1
            assert health["config_reload"]["watching"] == str(cfg_path)
            assert health["config_reload"]["last_reload_at"] is not None
        finally:
            server.shutdown()

    def test_unchanged_config_is_not_reparsed(self, tmp_path: Path) -> None:
        """Steady state is a single stat(): no on-disk change → no reparse."""
        server, cfg_path, _api, _web = self._build(tmp_path)
        try:
            import coord.config as coord_config_module

            calls: list[Path] = []
            original = coord_config_module.load

            def _counting_load(path):
                calls.append(path)
                return original(path)

            coord_config_module.load = _counting_load
            try:
                for _ in range(3):
                    server.health()
            finally:
                coord_config_module.load = original

            assert calls == []
            assert server.health()["config_reload"]["reloads"] == 0
        finally:
            server.shutdown()

    # ── malformed edits ─────────────────────────────────────────────────────

    def test_malformed_config_keeps_last_good_and_does_not_loop(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A broken hand-edit must leave the agent running on the last-good
        config, and must not be re-parsed (or re-logged) on every poll —
        matching the board daemon's #1081 behaviour."""
        server, cfg_path, api, _web = self._build(tmp_path)
        try:
            cfg_path.write_text("machines: [[[ not: valid: yaml")
            _bump_mtime(cfg_path)

            with caplog.at_level("WARNING", logger="coord.agent"):
                first = server.health()
                second = server.health()
                third = server.health()

            # Still serving what it served before the bad edit.
            assert first["repos"] == ["api"]
            assert third["repos"] == ["api"]
            assert server.repo_paths == {"api": str(api)}
            assert third["config_reload"]["reloads"] == 0
            assert second["config_reload"]["reloads"] == 0

            # Logged exactly once across three polls — the tracked mtime
            # advances past a bad edit so it isn't retried in a loop.
            assert caplog.text.count("failed to reload") == 1

            # ...and a *fixed* edit is picked up on the next poll.
            _write_config(
                cfg_path,
                repos=["api", "web"],
                repo_paths={"api": str(api), "web": str(_web)},
            )
            _bump_mtime(cfg_path)
            assert server.health()["repos"] == ["api", "web"]
        finally:
            server.shutdown()

    def test_config_dropping_this_machine_keeps_last_good(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An edit that removes/renames this machine must not be adopted —
        doing so would publish an empty repo list and refuse every dispatch."""
        server, cfg_path, api, web = self._build(tmp_path)
        try:
            _write_config(
                cfg_path,
                repos=["api", "web"],
                repo_paths={"api": str(api), "web": str(web)},
                machine_name="someone-else",
            )
            _bump_mtime(cfg_path)

            with caplog.at_level("WARNING", logger="coord.agent"):
                health = server.health()

            assert health["repos"] == ["api"]
            assert health["config_reload"]["reloads"] == 0
            assert "no longer declares machine 'test'" in caplog.text
        finally:
            server.shutdown()

    def test_no_local_config_is_a_no_op(self, tmp_path: Path) -> None:
        """Config-free / thin-client agents have no local file to watch —
        the reload must degrade to nothing rather than raising."""
        repo = _init_repo(tmp_path / "repo")
        server = _server(tmp_path, repo_path=repo)  # health_config=None
        try:
            assert server._maybe_reload_config() is False
            health = server.health()
            assert health["config_reload"]["watching"] is None
            assert health["config_reload"]["reloads"] == 0
            assert health["repos"] == ["api"]
        finally:
            server.shutdown()

    # ── the in-flight invariant ─────────────────────────────────────────────

    def test_reload_does_not_disturb_a_live_worker(self, tmp_path: Path) -> None:
        """A reload must never mutate state a *running* worker depends on:
        in-flight assignments keep the values they started with, and the new
        config governs the next dispatch onward."""
        from coord.config import load as load_config

        api = _init_repo(tmp_path / "api")
        web = _init_repo(tmp_path / "web")
        cfg_path = _write_config(
            tmp_path / "coordinator.yml",
            repos=["api"],
            repo_paths={"api": str(api)},
            artifact_paths={"api": ["target/release/api"]},
            build_commands={"api": "make api"},
        )
        cfg = load_config(cfg_path)
        server = AgentServer(
            machine_name="test",
            capabilities=["python"],
            repos=["api"],
            state_dir=tmp_path / "state",
            # Blocks until the sentinel appears, so the assignment is
            # genuinely RUNNING while the config changes underneath it.
            worker_command=lambda spec: [
                sys.executable, "-c",
                "import os, time\n"
                f"while not os.path.exists({str(tmp_path / 'go')!r}): "
                "time.sleep(0.05)\n"
                "print('done')",
            ],
            repo_paths={"api": str(api)},
            artifact_paths={"api": ["target/release/api"]},
            build_commands={"api": "make api"},
            health_config=cfg,
            worktree_writable_settings_files=[],
        )
        try:
            a = server.assign(_spec(api))
            deadline = time.time() + 10
            while server.get(a.id).status != RUNNING and time.time() < deadline:
                time.sleep(0.05)
            assert server.get(a.id).status == RUNNING
            worktree_before = server.get(a.id).worktree_path

            # Hostile edit: repoint api's checkout, swap its build command and
            # artifact globs, drop it from this machine, and add web.
            moved = _init_repo(tmp_path / "api-moved")
            _write_config(
                cfg_path,
                repos=["web"],
                repo_paths={"web": str(web)},
                artifact_paths={"web": ["target/release/web"]},
                build_commands={"web": "make web"},
            )
            _bump_mtime(cfg_path)
            assert server._maybe_reload_config() is True

            # The live worker's repo keeps every value it started with...
            assert server.repo_paths["api"] == str(api)
            assert server.artifact_paths["api"] == ["target/release/api"]
            assert server.build_commands["api"] == "make api"
            assert str(moved) not in server.repo_paths.values()
            # ...its process and worktree are untouched...
            assert server.get(a.id).status == RUNNING
            assert server.get(a.id).worktree_path == worktree_before
            # ...while the *next* dispatch follows the new config.
            assert server.repos == ["web"]
            assert server.repo_paths["web"] == str(web)
            assert server.build_commands["web"] == "make web"

            (tmp_path / "go").write_text("go\n")
            final = server.wait_for(a.id)
            assert final.exit_code == 0
            assert "done" in Path(final.log_path).read_text()

            # Once the pin lifts (assignment terminal), the next reload drops
            # the stale api entries entirely.
            _bump_mtime(cfg_path)
            server._config_mtime = None
            assert server._maybe_reload_config() is True
            assert "api" not in server.repo_paths
            assert "api" not in server.build_commands
            assert "api" not in server.artifact_paths
        finally:
            (tmp_path / "go").write_text("go\n")
            server.shutdown()

    def test_pin_restores_absence_not_just_values(self, tmp_path: Path) -> None:
        """A repo that had NO build command must not acquire one mid-flight:
        the pre-stash build would run a command the live worker never saw."""
        server, cfg_path, api, web = self._build(tmp_path)
        try:
            a = AgentAssignment(
                id="live-1", spec=_spec(api), status=RUNNING, branch="main",
            )
            server._assignments[a.id] = a

            _write_config(
                cfg_path,
                repos=["api", "web"],
                repo_paths={"api": str(api), "web": str(web)},
                artifact_paths={"api": ["target/release/api"]},
                build_commands={"api": "make api"},
            )
            _bump_mtime(cfg_path)
            assert server._maybe_reload_config() is True

            assert "api" not in server.build_commands
            assert "api" not in server.artifact_paths
            assert server.repos == ["api", "web"]
        finally:
            server._assignments.clear()
            server.shutdown()

    # ── hot vs restart-only ─────────────────────────────────────────────────

    def test_capabilities_are_hot(self, tmp_path: Path) -> None:
        """`capabilities` gates coordinator-side smoke/review routing off the
        published /health list; nothing in the worker path reads it, so a
        capability that disappears simply stops attracting new work."""
        server, cfg_path, api, _web = self._build(tmp_path)
        try:
            _write_config(
                cfg_path,
                repos=["api"],
                repo_paths={"api": str(api)},
                capabilities=["python", "rust"],
            )
            _bump_mtime(cfg_path)

            assert server.health()["capabilities"] == ["python", "rust"]
        finally:
            server.shutdown()

    def test_restart_only_fields_are_not_reloaded(self, tmp_path: Path) -> None:
        """`bash_wrap_spawn` and `first_output_timeout` are documented
        restart-only process tuning that uvicorn/`_spawn` already committed
        to at startup. A reload must leave both exactly as they were.

        #2326: `providers` used to be on this list too — a sibling test,
        `test_provider_registry_is_hot_reloaded_when_not_in_flight` below,
        now covers that it IS reloaded (a live worker's OWN resolved
        provider is still protected, just via in-flight pinning rather than
        blanket restart-only-ness — see `_apply_reloaded_config`)."""
        server, cfg_path, api, web = self._build(
            tmp_path,
            bash_wrap_spawn=False,
            first_output_timeout=12.5,
        )
        try:
            _write_config(
                cfg_path,
                repos=["api", "web"],
                repo_paths={"api": str(api), "web": str(web)},
            )
            _bump_mtime(cfg_path)
            assert server._maybe_reload_config() is True

            assert server.bash_wrap_spawn is False
            assert server.first_output_timeout == 12.5
        finally:
            server.shutdown()

    # ── #2326: providers.definitions is hot too ─────────────────────────────

    @pytest.mark.posix_only
    @_posix_binary_spawn_skip
    def test_provider_registry_is_hot_reloaded_when_not_in_flight(
        self, tmp_path: Path
    ) -> None:
        """An agent with its own local `coordinator.yml` must not pin
        `providers.definitions` at startup (#2326) — a provider config
        change (new env, new binary, ...) must apply on the NEXT dispatch,
        no restart, exactly like `repos`/`capabilities`/etc already do.

        Verifies the resolution itself: the env dict a fresh dispatch's
        provider carries, not a re-read of the config file."""
        from coord.config import load as load_config
        from coord.providers import build_provider

        api = _init_repo(tmp_path / "api")
        stub = tmp_path / "fake-claude.sh"
        stub.write_text('#!/bin/sh\necho "SPAWN_ENV_FOO=$SPAWN_ENV_FOO"\n')
        stub.chmod(0o755)

        cfg_path = _write_config_with_provider(
            tmp_path / "coordinator.yml",
            repos=["api"],
            repo_paths={"api": str(api)},
            provider_binary=str(stub),
        )
        cfg = load_config(cfg_path)

        server = AgentServer(
            machine_name="test",
            capabilities=["python"],
            repos=["api"],
            state_dir=tmp_path / "state",
            worker_command=lambda spec: [
                sys.executable, "-c", "print('legacy-SHOULD-NOT-RUN')"
            ],
            repo_paths={"api": str(api)},
            providers={
                "myprovider": build_provider(
                    "myprovider",
                    cfg.providers.definitions["myprovider"],
                    cfg.models,
                )
            },
            health_config=cfg,
            worktree_writable_settings_files=[],
            bash_wrap_spawn=False,
        )
        try:
            a1 = server.assign(_spec(api, provider="myprovider"))
            final1 = server.wait_for(a1.id, timeout=5)
            log1 = Path(final1.log_path).read_text()
            assert "SPAWN_ENV_FOO=bar" not in log1
            assert "legacy-SHOULD-NOT-RUN" not in log1

            _write_config_with_provider(
                cfg_path,
                repos=["api"],
                repo_paths={"api": str(api)},
                provider_binary=str(stub),
                provider_env={"SPAWN_ENV_FOO": "bar"},
            )
            _bump_mtime(cfg_path)

            a2 = server.assign(_spec(api, provider="myprovider"))
            final2 = server.wait_for(a2.id, timeout=5)
            log2 = Path(final2.log_path).read_text()
            assert "SPAWN_ENV_FOO=bar" in log2, (
                f"providers.definitions edit did not reach the next "
                f"dispatch: {log2!r}"
            )
            # An implicit "claude" entry is always materialised alongside
            # whatever's explicitly configured (see `ProvidersConfig`).
            assert server.health()["config_reload"]["provider_names"] == [
                "claude",
                "myprovider",
            ]
        finally:
            server.shutdown()

    @pytest.mark.posix_only
    @_posix_binary_spawn_skip
    def test_in_flight_provider_is_pinned_across_a_reload(
        self, tmp_path: Path
    ) -> None:
        """The #2299 in-flight invariant applies to providers too (#2326): a
        RUNNING assignment must keep resolving to the provider instance it
        started with, even though a reload landed while it was running —
        `_reap` re-resolves the SAME spec afterwards to pick a log parser,
        and must get back the SAME identity `_spawn` used, not one a reload
        swapped underneath it."""
        from coord.config import load as load_config
        from coord.providers import build_provider

        api = _init_repo(tmp_path / "api")
        go = tmp_path / "go"
        stub = tmp_path / "fake-claude.sh"
        stub.write_text(
            "#!/bin/sh\n"
            f"while [ ! -f {go} ]; do sleep 0.05; done\n"
            'echo "SPAWN_ENV_FOO=$SPAWN_ENV_FOO"\n'
        )
        stub.chmod(0o755)

        cfg_path = _write_config_with_provider(
            tmp_path / "coordinator.yml",
            repos=["api"],
            repo_paths={"api": str(api)},
            provider_binary=str(stub),
        )
        cfg = load_config(cfg_path)
        server = AgentServer(
            machine_name="test",
            capabilities=["python"],
            repos=["api"],
            state_dir=tmp_path / "state",
            worker_command=lambda spec: [sys.executable, "-c", "print('legacy')"],
            repo_paths={"api": str(api)},
            providers={
                "myprovider": build_provider(
                    "myprovider",
                    cfg.providers.definitions["myprovider"],
                    cfg.models,
                )
            },
            health_config=cfg,
            worktree_writable_settings_files=[],
            bash_wrap_spawn=False,
        )
        try:
            a = server.assign(_spec(api, provider="myprovider"))
            deadline = time.time() + 10
            while server.get(a.id).status != RUNNING and time.time() < deadline:
                time.sleep(0.05)
            assert server.get(a.id).status == RUNNING
            pre_reload_provider = server._providers["myprovider"]

            # Edit lands WHILE the worker above is running.
            _write_config_with_provider(
                cfg_path,
                repos=["api"],
                repo_paths={"api": str(api)},
                provider_binary=str(stub),
                provider_env={"SPAWN_ENV_FOO": "bar"},
            )
            _bump_mtime(cfg_path)
            assert server._maybe_reload_config() is True

            # The RUNNING assignment's own provider identity must survive
            # the reload untouched — asserted directly (object identity),
            # not just inferred from behaviour.
            in_flight_provider = server._resolve_provider(server.get(a.id).spec)
            assert in_flight_provider is pre_reload_provider
            assert in_flight_provider.env().get("SPAWN_ENV_FOO") is None

            go.write_text("done")
            final = server.wait_for(a.id, timeout=10)
            log = Path(final.log_path).read_text()
            assert "SPAWN_ENV_FOO=bar" not in log, (
                "a reload must not retarget an in-flight worker's provider "
                f"mid-run: {log!r}"
            )

            # The pin lifts once the assignment is terminal AND the next
            # reload runs (a pin is only re-evaluated when a reload actually
            # happens — an unchanged file triggers no reload at all, so it
            # takes one more on-disk change, exactly like a genuine second
            # `coordinator.yml` edit would).
            _bump_mtime(cfg_path)
            assert server._maybe_reload_config() is True
            a2 = server.assign(_spec(api, provider="myprovider"))
            final2 = server.wait_for(a2.id, timeout=10)
            log2 = Path(final2.log_path).read_text()
            assert "SPAWN_ENV_FOO=bar" in log2
        finally:
            server._assignments.clear()
            server.shutdown()

    def test_reload_busts_the_local_health_cache(self, tmp_path: Path) -> None:
        """The H-1 block in /health is built from the loaded Config's
        checkouts — a cache left in place would keep republishing the
        pre-reload repo set for a full TTL, i.e. the same skew one layer
        down."""
        server, cfg_path, api, web = self._build(tmp_path)
        try:
            server._local_health_cache = (time.time(), {"stale": True})
            _write_config(
                cfg_path,
                repos=["api", "web"],
                repo_paths={"api": str(api), "web": str(web)},
            )
            _bump_mtime(cfg_path)
            assert server._maybe_reload_config() is True
            assert server._local_health_cache is None
            assert server._health_config.path == cfg_path
        finally:
            server.shutdown()
