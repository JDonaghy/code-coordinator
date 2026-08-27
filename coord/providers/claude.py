"""ClaudeProvider: the ``claude -p`` concrete provider.

Parity requirement: ``ClaudeProvider().build_command(spec)`` produces the
**same argv** as ``coord.agent.default_worker_command(spec)`` for the same
inputs.  The logic is a direct transcription of that function's body with
the ``resolved_model`` / ``system_prompt`` / ``allowed_tools`` /
``permission_mode`` kwargs spliced in; the parity tests in
``tests/test_providers.py`` enforce this mechanically.

Imports from ``coord.agent`` are **deferred** (inside method bodies) to keep
the import cycle latent until the wiring issue lands.  At that point
``AgentServer`` will consume ``ClaudeProvider``, creating a two-way link;
until then the one-way ``claude → agent`` direction is safe because
``coord.agent`` does not import from ``coord.providers``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from coord.providers.base import Capabilities, Provider, WorkerSummary

if TYPE_CHECKING:
    from coord.agent import AssignmentSpec


class ClaudeProvider(Provider):
    """Concrete provider for ``claude -p`` (Anthropic Claude Code workers).

    This is the **reference backend** — all :class:`~.base.Capabilities`
    flags are ``True`` because ``claude -p`` supports every feature the
    coordinator relies on.

    Args:
        binary: Override the worker binary name/path.  ``None`` falls back to
            :data:`coord.agent.DEFAULT_WORKER_BINARY` (``"claude"``).
        model: Fallback model id/alias from the provider definition
            (``ProviderDef.model``).  Used only when neither an explicit
            ``resolved_model`` nor ``spec.model`` is set — see
            :meth:`build_command`.
        env: Extra environment variables from the provider definition
            (``ProviderDef.env``, already ``${VAR}``-expanded by config
            parsing).  Returned verbatim by :meth:`env`.
        extra_args: Additional argv entries from the provider definition
            (``ProviderDef.extra_args``).  Appended to the end of the argv
            built by :meth:`build_command`.
    """

    def __init__(
        self,
        binary: str | None = None,
        *,
        model: str | None = None,
        env: dict[str, str] | None = None,
        extra_args: list[str] | None = None,
    ) -> None:
        self._binary = binary
        self._model = model
        self._env = dict(env) if env else {}
        self._extra_args = list(extra_args) if extra_args else []

    # ── Capabilities ──────────────────────────────────────────────────────────

    def capabilities(self) -> Capabilities:
        """All capabilities enabled — claude -p is the reference backend.

        ``billing_mode="metered"`` reflects the 2026-06-15 Anthropic
        change: ``claude -p`` (and Agent SDK) sessions are billed at full
        API rates against a small non-rolling credit pool, not against
        the Max/Pro subscription.  Downstream code uses this flag to
        prefer a non-metered backend when one is available (#322).
        """
        return Capabilities(
            resume=True,
            inject=True,
            cost_reporting=True,
            true_system_prompt=True,
            enforces_deny_list=True,
            billing_mode="metered",
            # #437: claude -p is an unattended/headless backend — billed
            # per-token and intended for autonomous dispatch.  This is the
            # COMPLIANT automatable path and must NOT be flagged as
            # human-attended.  Set explicitly (not by default) to make the
            # intent visible in code.
            human_attended_only=False,
        )

    # ── Core methods ──────────────────────────────────────────────────────────

    def build_command(  # noqa: PLR0912  (many spec.type branches, matches legacy)
        self,
        spec: "AssignmentSpec",
        *,
        resolved_model: str | None = None,
        system_prompt: str | None = None,
        allowed_tools: str | None = None,
        permission_mode: str = "acceptEdits",
    ) -> list[str]:
        """Build the ``claude -p`` argv for *spec*.

        Produces the **same argv** as ``default_worker_command(spec)`` when
        called with ``resolved_model=spec.model`` and without overriding
        ``system_prompt`` / ``allowed_tools`` / ``permission_mode``.
        """
        # Deferred import — keeps the cycle latent until wiring is done.
        from coord.agent import (  # noqa: PLC0415
            DEFAULT_WORKER_BINARY,
            MILESTONE_CHAT_DENY_COMMANDS,
            MILESTONE_CHAT_SYSTEM_PROMPT,
            MOCK_AUTHOR_DENY_COMMANDS,
            MOCK_AUTHOR_SYSTEM_PROMPT,
            NEW_ISSUE_CHAT_DENY_COMMANDS,
            NEW_ISSUE_CHAT_SYSTEM_PROMPT,
            REFINEMENT_SYSTEM_PROMPT,
            REVIEW_DENY_COMMANDS,
            TEST_CHAT_SYSTEM_PROMPT,
            WORKER_PLAN_PROMPT,
            WORKER_SYSTEM_PROMPT,
            _base_checkout_write_guard_tools,
            _claude_md_system_prompt_suffix,
            _sealed_write_guard_tools,
            build_deny_prompt,
        )
        from coord.review import REVIEWER_SYSTEM_PROMPT  # noqa: PLC0415
        from coord.smoke import SMOKE_SYSTEM_PROMPT  # noqa: PLC0415

        binary = self._binary if self._binary is not None else DEFAULT_WORKER_BINARY

        # Precedence: explicit resolved_model > spec.model > provider-
        # definition model (ProviderDef.model, threaded in via __init__).
        # A plain ClaudeProvider().build_command(spec) with no definition
        # model still matches default_worker_command (self._model is None).
        if resolved_model is not None:
            effective_model = resolved_model
        elif spec.model is not None:
            effective_model = spec.model
        else:
            effective_model = self._model

        # Compute system_prompt / allowed_tools from spec.type when not
        # provided — direct transcription of default_worker_command's logic.
        if system_prompt is None or allowed_tools is None:
            if spec.type == "plan":
                _sp = spec.system_prompt if spec.system_prompt else WORKER_PLAN_PROMPT
                # Keep in sync with default_worker_command's identical
                # branch — `--setting-sources user` below drops CLAUDE.md
                # auto-discovery, so a plan leg needs the target repo's
                # conventions embedded here. See _claude_md_system_prompt_suffix
                # for what the flag actually does and the measured numbers.
                _sp += _claude_md_system_prompt_suffix(spec.repo_path)
                _at = "Read,Bash"
            elif spec.type == "refinement":
                _sp = spec.system_prompt if spec.system_prompt else REFINEMENT_SYSTEM_PROMPT
                _at = "Read"
            elif spec.type == "test-chat":
                _sp = spec.system_prompt if spec.system_prompt else TEST_CHAT_SYSTEM_PROMPT
                _sp += build_deny_prompt(spec.deny_commands)
                _at = "Read,Bash"
            elif spec.type == "new-issue-chat":
                _sp = spec.system_prompt if spec.system_prompt else NEW_ISSUE_CHAT_SYSTEM_PROMPT
                _sp += build_deny_prompt(NEW_ISSUE_CHAT_DENY_COMMANDS)
                if spec.new_issue_guidance:
                    _sp += (
                        "\n\nThe user's repo has the following guidance for "
                        "new-issue drafts. Follow it: ask focused questions "
                        "matched to the required sections, then produce a "
                        "finalised issue body using the same structure. Do not "
                        "invent sections that aren't there; do not omit required "
                        "sections (mark them `(TBD)` if the conversation hasn't "
                        "covered them yet).\n\n"
                        + spec.new_issue_guidance
                    )
                _at = "Read,Bash"
            elif spec.type == "milestone-chat":
                _sp = spec.system_prompt if spec.system_prompt else MILESTONE_CHAT_SYSTEM_PROMPT
                _sp += build_deny_prompt(MILESTONE_CHAT_DENY_COMMANDS)
                _at = "Read,Bash"
            elif spec.type == "mock-author":
                _sp = spec.system_prompt if spec.system_prompt else MOCK_AUTHOR_SYSTEM_PROMPT
                _sp += build_deny_prompt(MOCK_AUTHOR_DENY_COMMANDS)
                # Keep in sync with default_worker_command — `--setting-
                # sources user` drops CLAUDE.md auto-discovery.
                _sp += _claude_md_system_prompt_suffix(spec.repo_path)
                _at = "Read,Edit,Write,Bash"
            elif spec.type == "smoke":
                # #2301: keep in sync with default_worker_command's identical
                # branch — smoke gets Read,Bash only, deliberately WITHOUT
                # Monitor (an await-a-notification tool that ends a smoke
                # leg's one-shot session before any wake-up can arrive) or
                # Edit/Write (a smoke leg validates; it never mutates).
                _sp = spec.system_prompt if spec.system_prompt else SMOKE_SYSTEM_PROMPT
                _sp += build_deny_prompt(spec.deny_commands)
                _at = "Read,Bash"
            elif spec.type == "review":
                # #2461: keep in sync with default_worker_command's identical
                # branch — a reviewer reads the diff and reports a verdict,
                # it edits and pushes nothing, so Read,Bash only (no
                # Edit/Write, no Monitor — same one-shot-session reasoning as
                # the `smoke` branch above). See REVIEW_DENY_COMMANDS for why
                # its mutating-command deny list also lands in
                # --disallowedTools below, not just this prompt text.
                _sp = spec.system_prompt if spec.system_prompt else REVIEWER_SYSTEM_PROMPT
                _sp += build_deny_prompt(REVIEW_DENY_COMMANDS)
                _at = "Read,Bash"
            else:
                _sp = spec.system_prompt if spec.system_prompt else WORKER_SYSTEM_PROMPT
                _sp += build_deny_prompt(spec.deny_commands)
                # Keep in sync with default_worker_command's identical
                # catch-all branch — it covers "work", "fix", "conflict-fix"
                # and "test-author", every one of which edits code and needs
                # the target repo's CLAUDE.md conventions. This is NOT
                # defense-in-depth: #2820 measured that `--setting-sources
                # user` (below) *suppresses* CLAUDE.md's own project-level
                # auto-discovery, so this is the only mechanism that
                # delivers it. See _claude_md_system_prompt_suffix.
                _sp += _claude_md_system_prompt_suffix(spec.repo_path)
                # #2169: keep in sync with default_worker_command's identical
                # branch — Monitor is the sanctioned bounded-poll tool for a
                # backgrounded long-running command. #2301: this grant is for
                # work-shaped legs only — smoke has its own branch above.
                _at = "Read,Edit,Write,Bash,Monitor"

            if system_prompt is None:
                system_prompt = _sp
            if allowed_tools is None:
                allowed_tools = _at

        argv: list[str] = [
            binary, "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--system-prompt", system_prompt,
            "--allowedTools", allowed_tools,
            "--permission-mode", permission_mode,
            # #1445: see the matching comment in default_worker_command —
            # workers must not inherit the host checkout's project/local
            # Claude Code settings.
            #
            # #2462 EMERGENCY REVERT (2026-08-20): tried `--bare` here (also
            # closes hooks/.mcp.json), but `--bare` disables OAuth/keychain
            # auth and this fleet authenticates headless dispatch via OAuth,
            # not ANTHROPIC_API_KEY — broke every dispatch fleet-wide within
            # the hour. See the longer comment in default_worker_command.
            # #2820 closed the `.mcp.json` half of that leak separately,
            # below, via `--strict-mcp-config` (no OAuth side-effect). The
            # hooks half is still open.
            "--setting-sources", "user",
            # #2820: without this, every `-p` leg also loads the OPERATOR's
            # personal user-scope MCP servers (Google Drive/Calendar/Gmail —
            # observed non-deterministically across 120 recent worker
            # sessions) that no worker can ever use. `--setting-sources
            # user` above only gates settings.json, not `.mcp.json`/MCP
            # servers. Measured cost of this flag: 27 tools / 22,483 prompt
            # tokens vs. 47 tools / 23,096 tokens without it — ~600 tokens,
            # not the several-thousand a raw tool-count drop suggests (MCP
            # tool schemas are deferred behind ToolSearch). See the longer
            # comment in default_worker_command for the full measurement.
            "--strict-mcp-config",
        ]
        if effective_model:
            argv.extend(["--model", effective_model])
        # #1315 / #1642: same structural write guards as
        # default_worker_command — sealed-oracle prefix plus (for any type
        # with Edit in --allowedTools) the shared base checkout.
        disallowed_tools = _sealed_write_guard_tools(spec.files_forbidden)
        if "Edit" in allowed_tools:
            for pattern in _base_checkout_write_guard_tools(spec.repo_path):
                if pattern not in disallowed_tools:
                    disallowed_tools.append(pattern)
        # #2461: same CLI-enforced hard block as default_worker_command —
        # see REVIEW_DENY_COMMANDS.
        if spec.type == "review":
            for pattern in REVIEW_DENY_COMMANDS:
                if pattern not in disallowed_tools:
                    disallowed_tools.append(pattern)
        if disallowed_tools:
            argv.extend(["--disallowedTools", ",".join(disallowed_tools)])
        if spec.resume_session_id:
            argv.extend(["--resume", spec.resume_session_id])
        # #1706: provider-definition extra_args go last, after every flag
        # this method itself constructs.
        if self._extra_args:
            argv.extend(self._extra_args)
        return argv

    def oneshot_command(
        self,
        *,
        system_prompt: str,
        output_format: str | None = "json",
    ) -> list[str]:
        """Build the argv for a one-shot ``claude -p`` call.

        Returns ``[binary, "-p", "--system-prompt", system_prompt]`` plus
        ``["--output-format", output_format]`` when *output_format* is
        non-``None``.

        Unlike :meth:`build_command`, this does **not** add
        ``--input-format stream-json``, ``--verbose``,
        ``--allowedTools``, or ``--permission-mode`` — those flags are
        only meaningful for streaming worker sessions.

        The user message is expected to arrive via *stdin* (the caller
        passes ``input=`` to :func:`subprocess.run` or writes to
        ``proc.stdin`` in the async path).

        Note: unlike :meth:`build_command`, this does **not** thread in
        ``self._model`` / ``self._extra_args`` / ``self._env`` (#1706) —
        oneshot calls (brain planning, dashboard assistant) don't take an
        ``AssignmentSpec`` and sit outside the assignment spawn path that
        #1706 wires provider-definition config into. A future reader
        should not assume oneshot calls honour ``providers.definitions.
        <name>.{model,extra_args,env}``.

        Args:
            system_prompt: The system prompt for the one-shot call.
            output_format: Output format flag value.  ``"json"`` produces
                ``--output-format json`` (brain planning expects this so
                it can extract the ``result`` field from the outer JSON
                object).  ``None`` omits the flag (dashboard assistant
                streams the raw text response line-by-line).
        """
        from coord.agent import DEFAULT_WORKER_BINARY  # noqa: PLC0415
        binary = self._binary if self._binary is not None else DEFAULT_WORKER_BINARY
        cmd = [binary, "-p", "--system-prompt", system_prompt]
        if output_format is not None:
            cmd.extend(["--output-format", output_format])
        return cmd

    def initial_input(self, spec: "AssignmentSpec") -> bytes:
        """Return the briefing encoded as a stream-json user message."""
        from coord.agent import _user_message_line  # noqa: PLC0415
        return _user_message_line(spec.briefing)

    def result_marker(self) -> str:
        """String whose presence in the log signals logical completion."""
        return '"type":"result"'

    def env(self) -> dict[str, str]:
        """Extra environment variables from the provider definition (#1706).

        Returns a copy of ``ProviderDef.env`` (already ``${VAR}``-expanded
        by config parsing) threaded in via ``__init__``.  Empty dict when
        the provider was constructed with no ``env`` (matches pre-#1706
        behaviour for no-config deployments).
        """
        return dict(self._env)

    def parse_log(
        self, log_path: str | Path, tail_bytes: int = 65536
    ) -> WorkerSummary:
        """Delegate to :func:`coord.worker_events.parse_log`."""
        from coord.worker_events import parse_log  # noqa: PLC0415
        return parse_log(log_path, tail_bytes=tail_bytes)
