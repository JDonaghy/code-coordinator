"""Tests for ClaudePtyProvider and the agent-side PTY spawn path (#425).

Covers:
* :class:`ClaudePtyProvider` unit tests: argv shape (interactive — no ``-p``),
  capabilities (``billing_mode='subscription'``, ``enforces_deny_list=False``),
  initial_input (plain text + newline), parse_log delegation, env, registry
  registration.
* Safety gate: :meth:`AgentServer.assign` refuses write-capable assignment
  types on providers whose ``capabilities().enforces_deny_list`` is False
  but accepts non-mutating types.
* Default-path-unchanged: with no providers configured, the agent still
  uses :func:`default_worker_command` and the legacy ``claude -p`` spawn.

All tests **mock** the PTY/claude binary — no real ``claude`` session is
launched and no network or subscription call is made.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from coord.agent import (
    AgentServer,
    AssignmentSpec,
    WRITE_CAPABLE_SPEC_TYPES,
    default_worker_command,
)
from coord.config import ProviderDef
from coord.providers import (
    ClaudeProvider,
    ClaudePtyProvider,
    build_provider,
    resolve_provider_name,
)
from coord.providers.base import Capabilities
from coord.providers.claude_pty import PTY_RESULT_MARKER


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_spec(**kwargs) -> AssignmentSpec:
    defaults: dict = {
        "repo_name": "myrepo",
        "repo_path": "/some/path",
        "issue_number": 42,
        "issue_title": "Add something",
        "briefing": "Please do the thing.",
    }
    defaults.update(kwargs)
    return AssignmentSpec(**defaults)


def _init_repo(path: Path) -> Path:
    """Create a minimal git repo with one commit so worktrees can be created."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=str(path), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )
    (path / "README").write_text("init\n")
    subprocess.run(
        ["git", "add", "README"], cwd=str(path), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )
    return path


# ── ClaudePtyProvider: argv shape ─────────────────────────────────────────────


def test_pty_build_command_is_interactive_no_dash_p() -> None:
    """Interactive argv: no ``-p``, no stream-json flags."""
    spec = _make_spec(type="work")
    argv = ClaudePtyProvider().build_command(spec)
    assert argv[0] == "claude"
    assert "-p" not in argv
    assert "--input-format" not in argv
    assert "--output-format" not in argv
    assert "--verbose" not in argv


def test_pty_build_command_passes_safety_flags() -> None:
    """``--allowedTools`` / ``--permission-mode`` are still passed (safety hedge)."""
    spec = _make_spec(type="work")
    argv = ClaudePtyProvider().build_command(spec)
    assert "--allowedTools" in argv
    assert "--permission-mode" in argv
    assert "--system-prompt" in argv
    pm_idx = argv.index("--permission-mode")
    assert argv[pm_idx + 1] == "acceptEdits"


def test_pty_build_command_honours_resolved_model() -> None:
    """resolved_model is reflected as ``--model <value>`` in the argv."""
    spec = _make_spec(type="work", model="sonnet")
    argv = ClaudePtyProvider().build_command(spec, resolved_model="opus")
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "opus"


def test_pty_build_command_resolved_model_none_uses_spec_model() -> None:
    """When resolved_model is None, spec.model is used."""
    argv = ClaudePtyProvider().build_command(_make_spec(model="haiku"))
    assert argv[argv.index("--model") + 1] == "haiku"


def test_pty_build_command_no_model_suppresses_flag() -> None:
    """When neither resolved_model nor spec.model is set, no ``--model`` flag."""
    argv = ClaudePtyProvider().build_command(_make_spec(model=None))
    assert "--model" not in argv


def test_pty_build_command_system_prompt_override() -> None:
    """Explicit system_prompt kwarg wins over computed one."""
    argv = ClaudePtyProvider().build_command(
        _make_spec(type="work"), system_prompt="custom"
    )
    assert argv[argv.index("--system-prompt") + 1] == "custom"


def test_pty_build_command_allowed_tools_override() -> None:
    """Explicit allowed_tools kwarg wins over computed one."""
    argv = ClaudePtyProvider().build_command(
        _make_spec(type="work"), allowed_tools="Read"
    )
    assert argv[argv.index("--allowedTools") + 1] == "Read"


def test_pty_build_command_permission_mode_override() -> None:
    """Explicit permission_mode kwarg is reflected in the argv."""
    argv = ClaudePtyProvider().build_command(
        _make_spec(type="work"), permission_mode="bypassPermissions"
    )
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"


def test_pty_build_command_custom_binary() -> None:
    """ClaudePtyProvider(binary='x') uses the custom binary."""
    argv = ClaudePtyProvider(binary="my-claude").build_command(_make_spec())
    assert argv[0] == "my-claude"


def test_pty_build_command_definition_model_is_lowest_precedence() -> None:
    """#1706: the provider-definition model is used only when neither
    resolved_model nor spec.model is set."""
    argv = ClaudePtyProvider(model="glm-4.6").build_command(_make_spec(model=None))
    assert argv[argv.index("--model") + 1] == "glm-4.6"


def test_pty_build_command_spec_model_beats_definition_model() -> None:
    argv = ClaudePtyProvider(model="glm-4.6").build_command(_make_spec(model="haiku"))
    assert argv[argv.index("--model") + 1] == "haiku"


def test_pty_build_command_resolved_model_beats_definition_model() -> None:
    argv = ClaudePtyProvider(model="glm-4.6").build_command(
        _make_spec(model="haiku"), resolved_model="opus"
    )
    assert argv[argv.index("--model") + 1] == "opus"


def test_pty_build_command_extra_args_appended_last() -> None:
    """#1706: extra_args land at the end of the argv."""
    argv = ClaudePtyProvider(extra_args=["--foo", "bar"]).build_command(_make_spec())
    assert argv[-2:] == ["--foo", "bar"]


def test_pty_build_command_branches_match_claude_for_plan_type() -> None:
    """spec.type=plan still gets the plan system prompt + Read,Bash tools."""
    spec = _make_spec(type="plan")
    argv = ClaudePtyProvider().build_command(spec)
    # Same spec-type branch logic as ClaudeProvider — easiest cross-check
    # is via the legacy default_worker_command's computed values.
    legacy = default_worker_command(spec)
    legacy_sp = legacy[legacy.index("--system-prompt") + 1]
    legacy_at = legacy[legacy.index("--allowedTools") + 1]
    assert argv[argv.index("--system-prompt") + 1] == legacy_sp
    assert argv[argv.index("--allowedTools") + 1] == legacy_at


@pytest.mark.parametrize("spec_type", ["work", "conflict-fix", "fix"])
def test_pty_build_command_branches_match_claude_for_work_type(spec_type: str) -> None:
    """#2169: the generic-worker (``else``) branch must stay in parity too.

    ``default_worker_command`` and ``ClaudeProvider.build_command`` both
    grant ``Monitor`` alongside ``Read,Edit,Write,Bash`` for write-capable
    spec types so a worker can bound-poll a backgrounded long-running
    command. ``ClaudePtyProvider.build_command`` shares the same
    system-prompt/allowed-tools logic and must not drift from it — this
    regression-guards the drift where only two of the three call sites
    were updated to add ``Monitor``.

    ``smoke`` used to be parametrized here because it fell through the same
    ``else`` branch; #2301 gave it a branch of its own, so its parity is
    guarded by
    :func:`test_pty_build_command_smoke_branch_matches_claude_and_withholds_monitor`
    below instead. ``review`` likewise used to fall through here; #2461 gave
    it its own branch (read-only, no Edit/Write/Monitor — see
    ``coord.agent.REVIEW_DENY_COMMANDS``), guarded by
    :func:`test_pty_build_command_review_branch_matches_claude_and_withholds_monitor`.
    """
    spec = _make_spec(type=spec_type)
    argv = ClaudePtyProvider().build_command(spec)
    legacy = default_worker_command(spec)
    legacy_sp = legacy[legacy.index("--system-prompt") + 1]
    legacy_at = legacy[legacy.index("--allowedTools") + 1]
    assert argv[argv.index("--system-prompt") + 1] == legacy_sp
    assert argv[argv.index("--allowedTools") + 1] == legacy_at
    assert "Monitor" in argv[argv.index("--allowedTools") + 1]


def test_pty_build_command_smoke_branch_matches_claude_and_withholds_monitor() -> None:
    """#2301: the smoke branch is the *third* call site of the same logic.

    ``default_worker_command`` and ``ClaudeProvider.build_command`` both give
    ``spec.type == "smoke"`` its own branch — ``SMOKE_SYSTEM_PROMPT`` plus
    ``Read,Bash`` only, deliberately withholding ``Monitor`` (an await-a-
    notification tool that ends a one-shot ``claude -p`` leg's session before
    any wake-up can arrive) and Edit/Write (a smoke leg validates; it never
    mutates). ``ClaudePtyProvider.build_command`` must not drift from them —
    this regression-guards exactly the drift #2169 hit, where only two of the
    three call sites were updated.
    """
    from coord.smoke import SMOKE_SYSTEM_PROMPT

    spec = _make_spec(type="smoke")
    argv = ClaudePtyProvider().build_command(spec)
    legacy = default_worker_command(spec)
    legacy_sp = legacy[legacy.index("--system-prompt") + 1]
    legacy_at = legacy[legacy.index("--allowedTools") + 1]
    pty_sp = argv[argv.index("--system-prompt") + 1]
    pty_at = argv[argv.index("--allowedTools") + 1]
    assert pty_sp == legacy_sp
    assert pty_at == legacy_at
    # Pin the values too, so a *matched* regression on both sides still fails.
    assert pty_sp.startswith(SMOKE_SYSTEM_PROMPT)
    assert pty_at == "Read,Bash"
    assert "Monitor" not in pty_at.split(",")


def test_pty_build_command_review_branch_matches_claude_and_withholds_monitor() -> None:
    """#2461: the review branch is another call site of the same logic.

    ``default_worker_command`` and ``ClaudeProvider.build_command`` both give
    ``spec.type == "review"`` its own branch — ``REVIEWER_SYSTEM_PROMPT``
    plus ``Read,Bash`` only, deliberately withholding ``Monitor`` (a review
    leg is a one-shot ``claude -p`` session per #1394) and Edit/Write (a
    reviewer reads the diff and reports a verdict; it must not modify the
    PR's code). ``ClaudePtyProvider.build_command`` must not drift from
    them — mirrors
    :func:`test_pty_build_command_smoke_branch_matches_claude_and_withholds_monitor`.
    """
    from coord.review import REVIEWER_SYSTEM_PROMPT

    spec = _make_spec(type="review", review_target="123")
    argv = ClaudePtyProvider().build_command(spec)
    legacy = default_worker_command(spec)
    legacy_sp = legacy[legacy.index("--system-prompt") + 1]
    legacy_at = legacy[legacy.index("--allowedTools") + 1]
    pty_sp = argv[argv.index("--system-prompt") + 1]
    pty_at = argv[argv.index("--allowedTools") + 1]
    assert pty_sp == legacy_sp
    assert pty_at == legacy_at
    # Pin the values too, so a *matched* regression on both sides still fails.
    assert pty_sp.startswith(REVIEWER_SYSTEM_PROMPT)
    assert pty_at == "Read,Bash"
    assert "Monitor" not in pty_at.split(",")


def test_pty_build_command_smoke_honours_explicit_system_prompt() -> None:
    """#2301: the new smoke branch must still respect ``spec.system_prompt``
    (the dispatcher's per-run briefing prompt) rather than always forcing
    ``SMOKE_SYSTEM_PROMPT`` — same precedence as every other branch."""
    argv = ClaudePtyProvider().build_command(
        _make_spec(type="smoke", system_prompt="custom smoke prompt")
    )
    assert argv[argv.index("--system-prompt") + 1].startswith("custom smoke prompt")


# ── ClaudePtyProvider: capabilities ──────────────────────────────────────────


def test_pty_capabilities_billing_mode_subscription() -> None:
    """billing_mode must be 'subscription' — the whole point of the provider."""
    caps = ClaudePtyProvider().capabilities()
    assert isinstance(caps, Capabilities)
    assert caps.billing_mode == "subscription"


def test_pty_capabilities_enforces_deny_list_false() -> None:
    """enforces_deny_list is False until verified — gates the safety guard."""
    caps = ClaudePtyProvider().capabilities()
    assert caps.enforces_deny_list is False


def test_pty_capabilities_other_flags_documented() -> None:
    """resume/inject/cost_reporting are False; true_system_prompt is True."""
    caps = ClaudePtyProvider().capabilities()
    assert caps.resume is False
    assert caps.inject is False
    assert caps.cost_reporting is False
    assert caps.true_system_prompt is True


def test_pty_supports_inject_agrees_with_capabilities() -> None:
    p = ClaudePtyProvider()
    assert p.supports_inject() == p.capabilities().inject == False  # noqa: E712


# ── ClaudePtyProvider: initial_input / result_marker / env / parse_log ────────


def test_pty_initial_input_is_bracketed_paste() -> None:
    """initial_input wraps the briefing in a bracketed-paste block.

    A multi-line briefing must arrive as one bracketed paste or the TUI
    swallows the embedded newlines; the submitting carriage return is sent
    SEPARATELY by ``_spawn_pty`` so it is NOT part of this payload.
    """
    from coord.providers.claude_pty import (
        BRACKETED_PASTE_END,
        BRACKETED_PASTE_START,
    )

    spec = _make_spec(briefing="Hello, worker!")
    data = ClaudePtyProvider().initial_input(spec)
    assert isinstance(data, bytes)
    assert data == BRACKETED_PASTE_START + b"Hello, worker!" + BRACKETED_PASTE_END
    # No trailing newline and no carriage return — submit is separate.
    assert not data.endswith(b"\n")
    assert b"\r" not in data


def test_pty_initial_input_strips_trailing_newlines() -> None:
    """A briefing with trailing newlines is normalised before wrapping."""
    from coord.providers.claude_pty import (
        BRACKETED_PASTE_END,
        BRACKETED_PASTE_START,
    )

    spec = _make_spec(briefing="Hello\n\n")
    assert (
        ClaudePtyProvider().initial_input(spec)
        == BRACKETED_PASTE_START + b"Hello" + BRACKETED_PASTE_END
    )


def test_pty_initial_input_preserves_multiline_body() -> None:
    """Embedded newlines inside the briefing are kept (only the paste markers
    frame them) so the whole multi-line briefing submits as one message."""
    spec = _make_spec(briefing="line one\nline two\nline three")
    data = ClaudePtyProvider().initial_input(spec)
    assert b"line one\nline two\nline three" in data


def test_pty_initial_input_is_not_stream_json() -> None:
    """initial_input must not be a stream-json envelope (no JSON object)."""
    spec = _make_spec(briefing="some text")
    data = ClaudePtyProvider().initial_input(spec)
    assert not data.startswith(b"{"), "PTY initial_input must be raw bytes, not JSON"


def test_pty_result_marker_is_documented_sentinel() -> None:
    """result_marker returns the PTY-specific sentinel."""
    assert ClaudePtyProvider().result_marker() == PTY_RESULT_MARKER
    assert PTY_RESULT_MARKER.startswith("#")


def test_pty_env_is_empty() -> None:
    """env() returns an empty dict for ClaudePtyProvider."""
    assert ClaudePtyProvider().env() == {}


def test_pty_env_returns_definition_env() -> None:
    """#1706: env() returns a copy of the definition's env dict."""
    provider = ClaudePtyProvider(env={"FOO": "bar"})
    assert provider.env() == {"FOO": "bar"}


def test_pty_parse_log_handles_non_json_bytes(tmp_path: Path) -> None:
    """parse_log degrades gracefully on raw TTY bytes (no stream-json)."""
    log = tmp_path / "worker.log"
    # Raw TTY-style content: ANSI escapes and plain text — not JSON.
    log.write_text("\x1b[1mclaude>\x1b[0m hello world\n")
    summary = ClaudePtyProvider().parse_log(log)
    # Should not crash; returns a blank-ish summary.
    assert summary.num_turns == 0
    assert summary.total_cost_usd == 0.0


# ── ClaudePtyProvider: oneshot_command ───────────────────────────────────────


def test_pty_oneshot_command_returns_list() -> None:
    """oneshot_command returns a non-empty list (valid argv)."""
    cmd = ClaudePtyProvider().oneshot_command(system_prompt="sys")
    assert isinstance(cmd, list)
    assert len(cmd) > 0


def test_pty_oneshot_command_starts_with_binary() -> None:
    """First element is the claude binary."""
    cmd = ClaudePtyProvider().oneshot_command(system_prompt="sys")
    assert cmd[0] == "claude"


def test_pty_oneshot_command_custom_binary() -> None:
    """Binary override is respected in oneshot_command."""
    cmd = ClaudePtyProvider(binary="my-claude").oneshot_command(system_prompt="sys")
    assert cmd[0] == "my-claude"


def test_pty_oneshot_command_includes_dash_p() -> None:
    """Fallback argv uses the claude -p style (belt-and-suspenders for unguarded calls)."""
    cmd = ClaudePtyProvider().oneshot_command(system_prompt="sys")
    assert "-p" in cmd


def test_pty_oneshot_command_includes_system_prompt() -> None:
    """system_prompt is passed to the argv."""
    cmd = ClaudePtyProvider().oneshot_command(system_prompt="my system")
    assert "--system-prompt" in cmd
    assert cmd[cmd.index("--system-prompt") + 1] == "my system"


def test_pty_oneshot_command_includes_output_format_when_set() -> None:
    """output_format is included when not None."""
    cmd = ClaudePtyProvider().oneshot_command(system_prompt="sys", output_format="json")
    assert "--output-format" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"


def test_pty_oneshot_command_omits_output_format_when_none() -> None:
    """output_format=None suppresses the flag (used by dashboard streaming)."""
    cmd = ClaudePtyProvider().oneshot_command(system_prompt="sys", output_format=None)
    assert "--output-format" not in cmd


# ── Registry registration ────────────────────────────────────────────────────


def test_build_provider_claude_pty_type() -> None:
    """build_provider with type='claude-pty' returns a ClaudePtyProvider."""
    defn = ProviderDef(type="claude-pty")
    provider = build_provider("claude-pty", defn, None)
    assert isinstance(provider, ClaudePtyProvider)


def test_build_provider_claude_pty_threads_model_env_extra_args() -> None:
    """#1706: build_provider threads model / env / extra_args into
    ClaudePtyProvider the same way as the "claude" backend."""
    defn = ProviderDef(
        type="claude-pty",
        model="glm-4.6",
        env={"FOO": "bar"},
        extra_args=["--foo"],
    )
    provider = build_provider("claude-pty", defn, None)
    assert isinstance(provider, ClaudePtyProvider)
    assert provider.env() == {"FOO": "bar"}
    argv = provider.build_command(_make_spec(model=None))
    assert argv[argv.index("--model") + 1] == "glm-4.6"
    assert argv[-1] == "--foo"


def test_build_provider_claude_pty_with_binary() -> None:
    """build_provider passes the binary override to ClaudePtyProvider."""
    defn = ProviderDef(type="claude-pty", binary="my-claude")
    provider = build_provider("claude-pty", defn, None)
    argv = provider.build_command(_make_spec(type="work"))
    assert argv[0] == "my-claude"


def test_resolve_provider_name_picks_claude_pty_when_set() -> None:
    """The precedence chain returns 'claude-pty' when the spec sets it."""
    from coord.config import ProvidersConfig

    name = resolve_provider_name("claude-pty", None, ProvidersConfig())
    assert name == "claude-pty"


# ── Safety gate ──────────────────────────────────────────────────────────────


def _server_with_pty(tmp_path: Path, repo: Path) -> AgentServer:
    """Build an AgentServer with a ClaudePtyProvider in its providers dict."""
    return AgentServer(
        machine_name="test",
        capabilities=["python"],
        repos=["myrepo"],
        state_dir=tmp_path / "state",
        # Default worker_command isn't used when the PTY path triggers, but
        # we still need a callable for the back-compat path.
        worker_command=lambda spec: ["/bin/sh", "-c", "echo unused"],
        repo_paths={"myrepo": str(repo)},
        providers={"claude-pty": ClaudePtyProvider(binary="/bin/false")},
    )


@pytest.mark.parametrize("spec_type", sorted(WRITE_CAPABLE_SPEC_TYPES))
def test_safety_gate_refuses_write_capable_types_on_unverified_provider(
    tmp_path: Path, spec_type: str
) -> None:
    """Write-capable types must NOT spawn when enforces_deny_list is False."""
    repo = _init_repo(tmp_path / "repo")
    server = _server_with_pty(tmp_path, repo)
    spec = _make_spec(
        repo_name="myrepo",
        repo_path=str(repo),
        type=spec_type,
        provider="claude-pty",
        # type="review" / "smoke" assignments in production carry a
        # review_target / branch — supply harmless values so AssignmentSpec
        # construction is consistent.
        review_target="123" if spec_type == "review" else None,
    )
    with pytest.raises(ValueError, match="enforces_deny_list=False"):
        server.assign(spec)
    server.shutdown()


@pytest.mark.posix_only
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="reaches AgentServer._spawn_pty, which lazily `import pty` "
    "(stdlib, Unix-only) — POSIX-only, no Windows port yet (#2684)",
)
@pytest.mark.parametrize(
    "spec_type", ["plan", "refinement", "test-chat", "new-issue-chat"]
)
def test_safety_gate_allows_non_mutating_types_on_unverified_provider(
    tmp_path: Path, spec_type: str
) -> None:
    """Read-only types may use an unverified-deny-list provider.

    We let the assign call proceed past the gate and then immediately shut
    the server down — the PTY spawn itself will fail/exit cleanly because
    we configured binary=/bin/false, but the gate must NOT have raised.
    """
    repo = _init_repo(tmp_path / "repo")
    server = _server_with_pty(tmp_path, repo)
    spec = _make_spec(
        repo_name="myrepo",
        repo_path=str(repo),
        type=spec_type,
        provider="claude-pty",
    )
    # The gate is what we're testing; spawn may fail since /bin/false exits
    # immediately, but that failure is reported on the assignment record
    # (status=FAILED) — assign() itself returns a record without raising.
    record = server.assign(spec)
    assert record is not None
    assert record.spec.type == spec_type
    # Wait briefly for the spawn/reap to complete so shutdown is clean.
    server.wait_for(record.id, timeout=10.0)
    server.shutdown()


def test_safety_gate_no_provider_is_a_no_op(tmp_path: Path) -> None:
    """spec.provider=None never raises the gate even with no registry."""
    repo = _init_repo(tmp_path / "repo")
    server = AgentServer(
        machine_name="test",
        capabilities=["python"],
        repos=["myrepo"],
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/sh", "-c", "echo ok"],
        repo_paths={"myrepo": str(repo)},
        # No providers dict — back-compat mode.
    )
    spec = _make_spec(
        repo_name="myrepo", repo_path=str(repo), type="work", provider=None
    )
    record = server.assign(spec)
    final = server.wait_for(record.id, timeout=10.0)
    # spawn succeeded, no gate raised; no commits → advisory (#448)
    assert final.status in ("done", "failed", "advisory")
    server.shutdown()


def test_safety_gate_unknown_provider_is_refused_not_default(tmp_path: Path) -> None:
    """#1796: spec.provider naming a key not in the registry (and carrying
    no wire provider_def) must REFUSE the assignment, never silently fall
    through to ``self.worker_command`` (the legacy ``claude -p`` path).

    Before #1796 this fell through silently — the coordinator/board would
    record ``provider="not-registered"`` while claude actually ran, with no
    error anywhere.  See ``coord.agent.AgentServer._resolve_provider``.
    """
    repo = _init_repo(tmp_path / "repo")
    server = AgentServer(
        machine_name="test",
        capabilities=["python"],
        repos=["myrepo"],
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/sh", "-c", "echo ok"],
        repo_paths={"myrepo": str(repo)},
        providers={},  # empty registry; nothing to look up.
    )
    spec = _make_spec(
        repo_name="myrepo",
        repo_path=str(repo),
        type="work",
        provider="not-registered",
    )
    with pytest.raises(ValueError, match="not-registered"):
        server.assign(spec)
    server.shutdown()


# ── Default-path-unchanged ───────────────────────────────────────────────────


def test_default_path_uses_legacy_worker_command_when_no_providers(
    tmp_path: Path,
) -> None:
    """No-config: AgentServer routes through ``self.worker_command`` and
    NOT through the PTY branch."""
    repo = _init_repo(tmp_path / "repo")
    captured: list[list[str]] = []

    def recording_builder(spec: AssignmentSpec) -> list[str]:
        # A real, portable child process rather than `/bin/sh -c echo` —
        # this exercises actual routing/spawn, and `/bin/sh` doesn't exist
        # on Windows (#2684). `sys.executable` is guaranteed present.
        argv = [sys.executable, "-c", "print('legacy-output')"]
        captured.append(argv)
        return argv

    server = AgentServer(
        machine_name="test",
        capabilities=["python"],
        repos=["myrepo"],
        state_dir=tmp_path / "state",
        worker_command=recording_builder,
        repo_paths={"myrepo": str(repo)},
        # No providers dict at all — the default path must run.
    )

    # Sentinel: if the PTY branch were taken it would import and call
    # ClaudePtyProvider.build_command, NOT recording_builder.  We verify
    # that recording_builder was the one invoked.
    spec = _make_spec(
        repo_name="myrepo", repo_path=str(repo), type="work", provider=None
    )
    record = server.assign(spec)
    final = server.wait_for(record.id, timeout=10.0)
    # Worker makes no commits → advisory (#448)
    assert final.status in ("done", "advisory")
    assert captured == [[sys.executable, "-c", "print('legacy-output')"]]
    log = Path(final.log_path).read_text()
    assert "legacy-output" in log
    # And the log header is the legacy header (no `provider=` field).
    assert "provider=" not in log.splitlines()[0]
    server.shutdown()


def test_default_provider_selection_is_claude(tmp_path: Path) -> None:
    """ProvidersConfig() default resolves to 'claude' (not 'claude-pty').

    A redundant cross-check that the registry default is unchanged so the
    no-config dispatch path keeps using ClaudeProvider semantics.
    """
    from coord.config import ProvidersConfig

    cfg = ProvidersConfig()
    assert cfg.default == "claude"
    assert resolve_provider_name(None, None, cfg) == "claude"
    # And build_provider on the implicit definition returns a ClaudeProvider.
    provider = build_provider("claude", cfg.definitions["claude"], None)
    assert isinstance(provider, ClaudeProvider)


# ── PTY spawn smoke test (mocked claude binary) ──────────────────────────────


@pytest.mark.posix_only
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="reaches AgentServer._spawn_pty, which lazily `import pty` "
    "(stdlib, Unix-only) — POSIX-only, no Windows port yet (#2684)",
)
def test_pty_spawn_routes_through_pty_path(tmp_path: Path) -> None:
    """The PTY branch actually runs when spec.provider names a ClaudePtyProvider.

    Uses a tiny python script as a stand-in for the ``claude`` binary — it
    prints a banner (so the readiness loop sees output), reads a fixed-size
    chunk from stdin (the PRE-FILLED briefing — no CR is written by the
    agent under #437), echoes it back, and exits.  This exercises:
        * the PTY-branch dispatch in ``_spawn``;
        * the PTY pump thread (PTY master → log file);
        * the readiness wait + initial_input PRE-FILL to the PTY master
          (no auto-submit — see #437);
        * the result-marker stamping after the worker exits.
    """
    repo = _init_repo(tmp_path / "repo")
    server = AgentServer(
        machine_name="test",
        capabilities=["python"],
        repos=["myrepo"],
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/sh", "-c", "echo unused"],
        repo_paths={"myrepo": str(repo)},
        providers={"claude-pty": _ScriptedPtyProvider()},
    )
    # Use a non-mutating type so the safety gate doesn't refuse.
    spec = _make_spec(
        repo_name="myrepo",
        repo_path=str(repo),
        type="plan",
        provider="claude-pty",
        briefing="ECHO_ME",
    )
    record = server.assign(spec)
    final = server.wait_for(record.id, timeout=15.0)
    log = Path(final.log_path).read_text(errors="replace")
    # The PTY worker emitted its ready banner (proves the pump runs).
    assert "READY_BANNER" in log, f"worker banner missing from log: {log!r}"
    # The pump captured the worker's echo of our briefing — proves the
    # bracketed-paste initial_input was written to the PTY master after
    # readiness.  Under #437 the agent does NOT write a trailing CR (no
    # auto-submit), so the mock reads a fixed byte count rather than
    # waiting for a newline.  Assert the briefing body survived the
    # round-trip.
    assert any("got=" in ln and "ECHO_ME" in ln for ln in log.splitlines()), (
        f"briefing not echoed back: {log!r}"
    )
    # The pump stamped the result marker after the worker exited.
    assert PTY_RESULT_MARKER in log
    # And the spawn header named the provider.
    assert "provider=claude-pty" in log.splitlines()[0]
    server.shutdown()


# Python script body used by :class:`_ScriptedPtyProvider`.  The mock emits the
# bracketed-paste-enable DECSET (``\x1b[?2004h``) so the readiness loop in
# ``_spawn_pty`` proceeds to write the briefing, prints ``READY_BANNER``, reads
# a fixed-size byte chunk from stdin (the pre-fill — the agent no longer writes
# a CR), echoes ``got=<bytes>``, and exits.  A 1.5 s timeout backstops the read
# so the test fails fast if the pre-fill never arrives.
_SCRIPTED_PTY_MOCK = (
    "import sys, os, select; "
    "sys.stdout.write('\\x1b[?2004h'); "
    "sys.stdout.write('READY_BANNER\\n'); "
    "sys.stdout.flush(); "
    "buf = b''; "
    "deadline = __import__('time').monotonic() + 1.5; "
    "tlen = 6 + len('ECHO_ME') + 6; "  # ESC[200~ + body + ESC[201~
    "import time as _t\n"
    "while len(buf) < tlen and _t.monotonic() < deadline:\n"
    "    r, _, _ = select.select([0], [], [], 0.05)\n"
    "    if r:\n"
    "        chunk = os.read(0, tlen - len(buf))\n"
    "        if not chunk: break\n"
    "        buf += chunk\n"
    "sys.stdout.write('got=' + buf.decode('utf-8', errors='replace') + '\\n')\n"
    "sys.stdout.flush()\n"
)


class _ScriptedPtyProvider(ClaudePtyProvider):
    """Test-only provider that mocks ``claude`` with a tiny python script.

    Under #437 the agent's PTY path PRE-FILLS the input box but does NOT
    submit, so the mock can't use a line-oriented ``read`` to capture the
    briefing — instead it reads a fixed byte count off stdin and echoes
    it.  This proves the round-trip from initial_input → PTY master →
    child stdin → child stdout → PTY master → log without depending on
    a real claude install AND without depending on the (now-removed)
    auto-submit.

    Everything else (capabilities, initial_input, result_marker,
    parse_log) is inherited unchanged from :class:`ClaudePtyProvider`.
    """

    def build_command(
        self,
        spec: AssignmentSpec,
        *,
        resolved_model=None,
        system_prompt=None,
        allowed_tools=None,
        permission_mode="acceptEdits",
    ) -> list[str]:
        return [sys.executable, "-c", _SCRIPTED_PTY_MOCK]


class _SlowReadinessPtyProvider(ClaudePtyProvider):
    """Mock provider whose ``claude`` mock blocks for the full 5-second
    readiness window before emitting any output.

    Used to prove the fix for the iter-4 review: the PTY spawn (including
    the readiness poll) must run on a background thread so the HTTP
    handler in ``agent_app.py`` does not block the uvicorn event loop.
    With the fix in place, ``server.assign(spec)`` returns in milliseconds
    even though the worker won't be ready for ~5 s.
    """

    def build_command(
        self,
        spec: AssignmentSpec,
        *,
        resolved_model=None,
        system_prompt=None,
        allowed_tools=None,
        permission_mode="acceptEdits",
    ) -> list[str]:
        # Sleep past the readiness window, then exit.  No output until
        # the sleep elapses — so the readiness loop in ``_spawn_pty``
        # spins its full 5-second deadline before giving up.
        return ["/bin/sh", "-c", "sleep 6"]


@pytest.mark.posix_only
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="reaches AgentServer._spawn_pty, which lazily `import pty` "
    "(stdlib, Unix-only) — POSIX-only, no Windows port yet (#2684)",
)
def test_pty_assign_returns_immediately_without_blocking_on_readiness(
    tmp_path: Path,
) -> None:
    """Regression test for the iter-4 review: PTY ``assign()`` must not
    block the HTTP event loop while the readiness loop polls for output.

    Setup: a mock provider whose argv sleeps for 6 seconds before exiting
    (no output, so ``_spawn_pty`` would spin its full 5 s readiness budget
    on the synchronous path).  With the fix in place, ``server.assign()``
    must return in well under that budget — we assert under 1 s, leaving
    ample headroom for slow CI runners.
    """
    import time

    repo = _init_repo(tmp_path / "repo")
    server = AgentServer(
        machine_name="test",
        capabilities=["python"],
        repos=["myrepo"],
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/sh", "-c", "echo unused"],
        repo_paths={"myrepo": str(repo)},
        providers={"claude-pty": _SlowReadinessPtyProvider()},
    )
    spec = _make_spec(
        repo_name="myrepo",
        repo_path=str(repo),
        type="plan",
        provider="claude-pty",
        briefing="briefing",
    )
    start = time.monotonic()
    record = server.assign(spec)
    elapsed = time.monotonic() - start
    # The fix: assign() must return in milliseconds, not seconds.  Without
    # the fix, the 5-second readiness wait blocks here.  We assert < 1 s
    # to stay well clear of CI jitter while still proving the call did
    # not synchronously block on the readiness loop.
    assert elapsed < 1.0, (
        f"server.assign() blocked the caller for {elapsed:.3f}s — "
        "PTY readiness wait should run on a background thread"
    )
    # Wait for the worker to actually finish before tearing down.
    server.wait_for(record.id, timeout=15.0)
    server.shutdown()


# ── #865: banner-interrupted startup + paste-verify-retry (black-box) ────────

# Mock ``claude`` mimicking the #865 live capture: emits the bracketed-paste
# enable DECSET and the rendered input box (INPUT_BOX_MARKER) immediately,
# then — after the box is already up — an ASYNC banner/notification line
# arrives a few hundred ms later (the "Fable 5 is back" / MCP-auth-notice
# behaviour described in #865).  ``tty.setraw`` disables the pty's kernel
# line-echo so the only bytes that reach the log are ones this script
# explicitly writes — matching a real TUI, which renders everything itself
# rather than relying on line-discipline echo.
#
# The script then reads the pre-fill off stdin TWICE: the first receipt is
# deliberately DISCARDED (never echoed) to simulate a paste that lands
# mid-repaint and gets silently dropped — the pre-#865 failure mode. Only
# the second receipt is echoed back (``got=...``). This proves the paste
# was VERIFIED as missing and RETRIED — the core #865 fix — rather than the
# briefing simply vanishing.
_BANNER_INTERRUPT_PTY_MOCK = (
    "import sys, os, select, time, tty\n"
    "tty.setraw(0)\n"
    "sys.stdout.write('\\x1b[?2004h')\n"
    "sys.stdout.write('\\u276f placeholder\\n')\n"  # INPUT_BOX_MARKER rendered
    "sys.stdout.flush()\n"
    "time.sleep(0.3)\n"
    # Async startup content arriving AFTER the input box already rendered —
    # the #865 scenario.  This changes the log's size, so a purely
    # size-based quiescence check (pre- and post-#865) correctly keeps
    # waiting past it rather than pasting into the mid-repaint window.
    "sys.stdout.write('late banner notice\\n')\n"
    "sys.stdout.flush()\n"
    # tlen is hardcoded (NOT computed from the literal briefing text) so the
    # briefing's fingerprint ("ECHO_ME") never appears anywhere in this
    # script's OWN source — which is itself logged verbatim in the
    # assignment log's ``argv=...`` header line.  If it appeared there, the
    # production fingerprint check would find a false match against the
    # header and report "landed" before any real paste happened, defeating
    # the whole point of this fixture.  19 = len(ESC[200~) + len('ECHO_ME')
    # + len(ESC[201~) = 6 + 7 + 6.
    "tlen = 19\n"
    "def _read_chunk(deadline):\n"
    "    buf = b''\n"
    "    while len(buf) < tlen and time.monotonic() < deadline:\n"
    "        r, _, _ = select.select([0], [], [], 0.05)\n"
    "        if r:\n"
    "            chunk = os.read(0, tlen - len(buf))\n"
    "            if not chunk:\n"
    "                break\n"
    "            buf += chunk\n"
    "    return buf\n"
    "first = _read_chunk(time.monotonic() + 4.0)\n"
    "sys.stdout.write('DISCARDED first_len=' + str(len(first)) + '\\n')\n"
    "sys.stdout.flush()\n"
    "second = _read_chunk(time.monotonic() + 4.0)\n"
    "sys.stdout.write('got=' + second.decode('utf-8', errors='replace') + '\\n')\n"
    "sys.stdout.flush()\n"
)


class _BannerInterruptPtyProvider(ClaudePtyProvider):
    """Mock ``claude`` that renders the input box, THEN an async banner
    arrives, THEN silently drops the first pre-fill attempt before finally
    accepting the second (see :data:`_BANNER_INTERRUPT_PTY_MOCK`)."""

    def build_command(
        self,
        spec: AssignmentSpec,
        *,
        resolved_model=None,
        system_prompt=None,
        allowed_tools=None,
        permission_mode="acceptEdits",
    ) -> list[str]:
        return [sys.executable, "-c", _BANNER_INTERRUPT_PTY_MOCK]


@pytest.mark.posix_only
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="black-box fixture spawns coord.agent._spawn_pty for real (real "
    "pty.fork() + real subprocess, no mocking) — POSIX-only, no Windows port yet",
)
def test_pty_spawn_retries_after_banner_interrupted_dropped_paste(
    tmp_path: Path,
) -> None:
    """#865 black-box fixture: a delayed startup banner arrives after the
    input box renders, and the FIRST pre-fill attempt is silently dropped —
    the briefing must still end up verified in the input box via retry.

    This is the acceptance-criterion fixture from #865: "a fixture that
    feeds a delayed/banner-interrupted startup render ... and asserts the
    briefing still ends up in the input box (and that a paste-miss triggers
    a retry)."  Uses a REAL pty + REAL subprocess (no mocked ``os.write`` /
    ``subprocess.run``) — genuinely black-box against the agent's spawn path.
    """
    repo = _init_repo(tmp_path / "repo")
    server = AgentServer(
        machine_name="test",
        capabilities=["python"],
        repos=["myrepo"],
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/sh", "-c", "echo unused"],
        repo_paths={"myrepo": str(repo)},
        providers={"claude-pty": _BannerInterruptPtyProvider()},
    )
    spec = _make_spec(
        repo_name="myrepo",
        repo_path=str(repo),
        type="plan",
        provider="claude-pty",
        briefing="ECHO_ME",
    )
    record = server.assign(spec)
    final = server.wait_for(record.id, timeout=20.0)
    log = Path(final.log_path).read_text(errors="replace")

    # The banner arrived and was captured (proves the pump ran through the
    # async-content window rather than pasting blind before it).
    assert "late banner notice" in log, f"banner line missing from log: {log!r}"
    # The first pre-fill attempt was received by the mock and discarded —
    # proves a real write reached the child before the eventual success.
    assert "DISCARDED" in log, f"first (dropped) attempt never arrived: {log!r}"
    # And verification caught the miss and retried: the SECOND attempt was
    # echoed back containing the briefing body.
    assert any("got=" in ln and "ECHO_ME" in ln for ln in log.splitlines()), (
        f"briefing never landed after retry: {log!r}"
    )
    server.shutdown()
