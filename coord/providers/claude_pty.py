"""ClaudePtyProvider: interactive ``claude`` driven through a pseudo-terminal.

This is the **metering escape hatch** introduced for #425.  After 2026-06-15,
``claude -p`` (the :class:`~.claude.ClaudeProvider` backend) is billed at full
API rates against a non-rolling credit pool — see #322.  Interactive
``claude`` (no ``-p``) is still covered by the Max/Pro subscription, so the
coordinator exposes it as a separate provider that **opt-in** users can
select via ``coordinator.yml`` ``providers`` config or per-spec
``provider="claude-pty"``.

Differences from :class:`~.claude.ClaudeProvider`:

* ``build_command()`` does **not** pass ``-p`` and does **not** use
  ``stream-json`` formats.  Interactive ``claude`` reads typed input from a
  TTY and renders TUI output — the agent attaches the worker to a PTY
  (:mod:`pty`) in :meth:`coord.agent.AgentServer._spawn_pty`.
* ``initial_input()`` returns the briefing as **plain text + newline** (the
  bytes typed into the TTY), not a stream-json envelope.
* ``capabilities()`` reports ``billing_mode="subscription"`` (this is the
  whole point) and conservatively reports ``enforces_deny_list=False`` —
  the interactive PTY path has **not been verified** to honour
  ``--allowedTools`` / ``--permission-mode`` in this PR, so the agent's
  safety gate refuses to run write-capable assignment types (``work``,
  ``review``, ``smoke``, ``conflict-fix``) on this provider until that
  verification lands.  Non-mutating assignment types (``plan``,
  ``refinement``, ``test-chat``, ``new-issue-chat``) may still use it.
* ``parse_log()`` delegates to :func:`coord.worker_events.parse_log` so it
  degrades gracefully when the log contains raw TTY bytes rather than
  stream-json — :func:`~coord.worker_events.parse_log` silently skips
  non-JSON lines and returns a blank :class:`WorkerSummary`.

Imports from :mod:`coord.agent` are deferred (inside method bodies) to keep
the import cycle latent, matching the pattern in
:class:`~.claude.ClaudeProvider`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from coord.providers.base import Capabilities, Provider, WorkerSummary

if TYPE_CHECKING:
    from coord.agent import AssignmentSpec


# Sentinel string written to the log file by the PTY spawn path when the
# worker exits.  Used as :meth:`ClaudePtyProvider.result_marker` because the
# interactive ``claude`` CLI does NOT emit a stream-json ``result`` event,
# so the standard ``"type":"result"`` marker never appears.
#
# MUST stay byte-equal to ``coord.agent._PTY_RESULT_LINE_MARKER`` (which is
# the bytes form of this constant — kept separate there to dodge a
# module-level import cycle).  The sync is asserted in
# ``tests/test_agent_reap.py::test_pty_marker_bytes_sync_with_provider_string``.
PTY_RESULT_MARKER = "# pty: worker exited"

# Briefing-submission control bytes for the interactive TUI (#425).
#
# A multi-line briefing MUST be delivered inside a bracketed-paste block
# (``ESC[200~`` ... ``ESC[201~``) — otherwise the TUI treats each embedded
# newline as a soft line break and the message is never submitted.  The
# submitting Enter is a CARRIAGE RETURN (``\r``); ``\n`` does NOT submit under
# the TUI's enhanced (kitty) keyboard protocol.  The CR must be written
# *separately*, after a short settle delay, by
# :meth:`coord.agent.AgentServer._spawn_pty` — a CR glued onto ``ESC[201~`` in
# the same write is swallowed by paste-end processing.  ``initial_input``
# returns only the bracketed-paste block; the spawn path appends the CR.
# Verified live against interactive ``claude`` v2.1.165 (#425 smoke: raw
# multi-line never submits; bracketed-paste + separate CR reliably does).
BRACKETED_PASTE_START = b"\x1b[200~"
BRACKETED_PASTE_END = b"\x1b[201~"

# DECSET emitted by the TUI once it has ENABLED bracketed-paste input — the
# spawn path waits for this (then for render quiescence) before pasting, so the
# briefing is never sent before the TUI can accept it (the enable arrives
# ~0.85s after first output, while the TUI is still drawing; pasting then is
# silently dropped).
BRACKETED_PASTE_ENABLE = b"\x1b[?2004h"

# ── #865: readiness-anchor + paste verification primitives ───────────────────
#
# Root cause of #865: both injection paths (the tmux path in
# ``coord.interactive`` and the PTY-relay paths in ``coord.interactive`` /
# ``coord.agent``) used render QUIESCENCE as their only proxy for "the input
# box is ready", then pasted once and never checked whether the text actually
# landed.  Claude Code's TUI paints async startup content (promo banners,
# MCP/auth notices, update notices) over several seconds — quiescence alone
# cannot tell "static banner mid-startup" from "input box settled", and a
# late repaint arriving right around the paste can silently discard it.
#
# The fix has two parts, both anchored on primitives defined ONCE here so the
# three call-sites (tmux capture-pane text, the interactive PTY relay's
# in-memory screen buffer, and the agent's PTY log tail) can't drift out of
# sync the way the pre-#865 "twin implementations" did:
#
# 1. ``INPUT_BOX_MARKER`` — the rendered left border of the TUI's input box
#    (``"❯ Try ..."`` in the #865 capture).  Callers require this to be
#    present (in addition to quiescence) before treating the screen as
#    "ready" — so we don't paste into a still-loading/blank frame that
#    merely *looks* stable.  If the marker never appears (older CLI, unusual
#    render), callers still fall back to their existing timeout/cap so a
#    mismatch degrades to the pre-#865 behaviour rather than hanging.
#
# 2. ``briefing_fingerprint`` / ``fingerprint_in_text`` / ``fingerprint_in_bytes``
#    — after pasting, re-observe the screen and confirm a snippet of the
#    briefing actually rendered.  Whitespace is collapsed on both sides
#    because the terminal re-wraps pasted text at arbitrary column widths,
#    so an exact match against the original briefing would spuriously fail.
#    Callers retry the paste (bounded, with backoff) on a miss and log a
#    hard failure if every attempt misses — never silent.
INPUT_BOX_MARKER = "❯"
INPUT_BOX_MARKER_BYTES = INPUT_BOX_MARKER.encode("utf-8")

# ── #2541: first-run / permission prompt detection ──────────────────────────
#
# Claude Code's TUI renders BOTH its normal chat input box AND any blocking,
# human-answered confirmation dialog (the first-run "do you trust the files
# in this folder?" prompt, a per-tool/MCP permission question, the
# "bypass permissions" warning, ...) with the same ``INPUT_BOX_MARKER`` ("❯")
# cursor glyph. The #865 readiness heuristic only checked for that glyph, so
# it could not tell "the chat box is ready for a paste" apart from "a
# confirmation dialog is waiting for a keypress" — and pasting the full,
# multi-line briefing text into a single-choice menu (which handles single
# keystrokes, not a bracketed-paste block) is the leading suspect for
# #2541's "claude exited with status 1" crash on a fresh worktree's
# first-run prompt.
#
# The distinguishing signal: the chat box's own render always follows "❯ "
# with free-form placeholder/typed text (e.g. "❯ Try a task, ask a question,
# or type /help", confirmed live in the #865 capture) — never a bare
# "<digit>.". A confirmation dialog's selected option, by contrast, is
# rendered "❯ 1. Yes" / "❯ 2. No, ..." — Claude Code's numbered-choice
# convention for every confirmation prompt it has (trust dialog, tool/MCP
# permission, bypass-permissions warning, model picker, ...). Matching that
# shape lets injection callers hold off on pasting into ANY such dialog
# without having to enumerate their exact wording, which the #2541 issue
# itself could not pin down ("the exact interaction is unconfirmed").
BLOCKING_PROMPT_SELECTION_RE = re.compile(
    rf"{re.escape(INPUT_BOX_MARKER)}\s*\d+\.\s"
)

#: Known first-run/onboarding dialog text, matched case-insensitively as a
#: belt-and-suspenders check alongside :data:`BLOCKING_PROMPT_SELECTION_RE`
#: — catches the dialog even mid-render, before its "❯ 1." cursor line has
#: painted yet.
BLOCKING_PROMPT_TEXT_MARKERS = (
    "do you trust the files in this folder",
    "trust the files in this folder",
    "bypass permissions",
)


def pane_shows_blocking_prompt(text: str) -> bool:
    """True when *text* (a captured pane/screen) shows a human-answered
    confirmation dialog rather than the normal chat input box (#2541).

    See :data:`BLOCKING_PROMPT_SELECTION_RE` for the detection rationale.
    Callers use this to withhold a briefing paste while such a dialog is on
    screen — pasting free text into a single-choice menu (rather than the
    free-text chat box) is the leading suspect for the #2541 "claude exited
    with status 1" crash on a fresh worktree's first-run prompt.
    """
    if BLOCKING_PROMPT_SELECTION_RE.search(text):
        return True
    lowered = text.lower()
    return any(marker in lowered for marker in BLOCKING_PROMPT_TEXT_MARKERS)


_WS_RE_STR = re.compile(r"\s+")
_WS_RE_BYTES = re.compile(rb"\s+")

# ── #896: paste-chip / box-changed detection ──────────────────────────────────
#
# Root cause of #896: ``fingerprint_in_text`` / ``fingerprint_in_bytes`` both
# check only for the *literal* first-40-chars of the briefing in the captured
# pane/bytes.  Two scenarios produce false negatives even though the paste
# landed:
#
# 1. Claude Code **collapses** a large/multi-line paste into a placeholder
#    chip ("❯ [Pasted text #1 +NNN lines]") — the literal briefing text never
#    appears in ``capture-pane -p`` or the PTY log.
#
# 2. The input box is bounded-height and scrolls to show the **tail** of the
#    pasted text (cursor at end of content), so the fingerprint — taken from
#    the START — has scrolled out of the visible/captured region.
#
# ``_PASTE_CHIP_RE`` / ``_PASTE_CHIP_BYTES_RE`` detect the collapsed chip.
# ``paste_landed`` / ``paste_landed_bytes`` are the broadened predicates used
# in place of the bare ``fingerprint_in_*`` checks in ``coord.interactive``
# and ``coord.agent``.  They are defined here — once — so all three call-sites
# stay in sync.
#
# LIVE-VERIFIED (#896 review follow-up): the inferred chip wording above was
# confirmed against a real render rather than left as a guess.  Launched
# interactive ``claude`` v2.1.198 in a scratch tmux session, pasted a 58-line
# buffer via ``tmux load-buffer`` + ``paste-buffer -p``, and captured the
# collapsed input box with ``tmux capture-pane -p``. Literal rendered line:
#
#     ❯ [Pasted text #1 +58 lines]
#
# — confirming the ``[Pasted text #N +NNN lines]`` form (with a trailing
# "paste again to expand" hint on the line below, not matched here). The
# pattern below requires both fragments to co-occur as a single chip
# (``[^\]]*`` bridges the "#1 " counter between them) rather than matching
# either fragment independently — narrower than the original OR-of-two-
# alternatives version, so unrelated "+N lines]" text elsewhere in the
# input-box region (e.g. briefing prose describing a diff) can't false-
# positive.
_PASTE_CHIP_RE = re.compile(r"\[Pasted text[^\]]*\+\s*\d+\s*lines\]")
_PASTE_CHIP_BYTES_RE = re.compile(rb"\[Pasted text[^\]]*\+\s*\d+\s*lines\]")


def briefing_fingerprint(briefing: str, length: int = 40) -> str:
    """Return a whitespace-normalized snippet of *briefing* for paste verification.

    Collapsing runs of whitespace to a single space and taking a short prefix
    makes the check robust to the terminal re-wrapping the pasted text across
    lines, while still being specific enough that it won't spuriously match
    unrelated screen content (banner text, help hints, etc.).
    """
    return _WS_RE_STR.sub(" ", briefing).strip()[:length]


def fingerprint_in_text(text: str, fingerprint: str) -> bool:
    """True when *fingerprint* (see :func:`briefing_fingerprint`) appears in *text*.

    Used against tmux's ``capture-pane -p`` output, which is already
    fully-rendered plain text (no ANSI escapes to worry about).  An empty
    fingerprint (e.g. a briefing shorter than nothing) trivially matches —
    there's nothing to verify.
    """
    if not fingerprint:
        return True
    return fingerprint in _WS_RE_STR.sub(" ", text)


def fingerprint_in_bytes(raw: bytes, fingerprint: str) -> bool:
    """Byte-oriented twin of :func:`fingerprint_in_text` for the PTY paths.

    The PTY log/screen buffer contains raw TTY bytes — ANSI escapes
    interleaved with the rendered text — so this is a best-effort heuristic
    rather than the exact match ``fingerprint_in_text`` gets from tmux's
    pre-rendered ``capture-pane`` output.  A redraw that splits the pasted
    text across escape sequences can still produce a false negative, which
    just triggers a (harmless, bounded) retry.
    """
    if not fingerprint:
        return True
    normalized = _WS_RE_BYTES.sub(b" ", raw)
    return fingerprint.encode("utf-8") in normalized


def paste_landed(
    pane_text: str,
    *,
    fingerprint: str,
    baseline: str | None = None,
) -> bool:
    """True when a paste of the briefing (identified by *fingerprint*) has landed.

    Broadened over the bare :func:`fingerprint_in_text` check (#896) to
    handle the two scenarios where ``fingerprint_in_text`` gives false
    negatives even though the paste actually arrived:

    * **Paste-chip**: Claude Code collapses a large/multi-line paste into a
      placeholder chip (``❯ [Pasted text #1 +NNN lines]``) — the literal
      briefing text never appears in ``capture-pane -p``.
    * **Scrolled tail**: the input box is bounded-height and renders the
      **tail** of the pasted text (cursor at end), so the fingerprint —
      taken from the *start* — has scrolled out of the visible region.

    The chip and box-changed checks are deliberately scoped to the
    **input-box region** (characters at or after the ``❯`` INPUT_BOX_MARKER)
    so that async startup banners mutating the top of the pane do NOT count
    as evidence of a paste — this was the #865 trap.

    Args:
        pane_text: The current ``tmux capture-pane -p`` output (plain text,
            no ANSI escapes).
        fingerprint: The whitespace-normalised briefing snippet returned by
            :func:`briefing_fingerprint`.
        baseline: The pane text captured *before* the paste was sent.
            When provided, a non-empty input-box region that differs from the
            corresponding region in *baseline* is treated as evidence that the
            paste arrived (box went from empty placeholder to holding content).

    Returns:
        ``True`` when any of the following holds:

        * :func:`fingerprint_in_text` matches (fast path, no scoping needed).
        * :data:`_PASTE_CHIP_RE` matches somewhere in the input-box region.
        * The input-box region is non-empty and differs from the same region
          in *baseline* (box changed after the paste).
    """
    if not fingerprint:
        return True  # nothing to verify

    # Fast path: literal fingerprint visible anywhere in the pane.
    if fingerprint_in_text(pane_text, fingerprint):
        return True

    # Locate the input-box region so the broader checks don't count async
    # startup banners at the top of the pane.
    marker_idx = pane_text.find(INPUT_BOX_MARKER)
    if marker_idx == -1:
        # No input box visible — can't safely scope the chip/changed checks,
        # so fall back to "not yet landed" and let the caller retry/time out.
        return False

    region = pane_text[marker_idx:]

    # Paste-chip: Claude Code collapses large pastes to a chip.
    if _PASTE_CHIP_RE.search(region):
        return True

    # Box-changed from the pre-paste baseline: the input-box region is
    # non-empty and differs from what it looked like before the paste.
    if baseline is not None:
        baseline_marker_idx = baseline.find(INPUT_BOX_MARKER)
        if baseline_marker_idx != -1:
            baseline_region = baseline[baseline_marker_idx:]
            if region.strip() and region != baseline_region:
                return True

    return False


def paste_landed_bytes(raw: bytes, fingerprint: str) -> bool:
    """Byte-oriented twin of :func:`paste_landed` for the PTY relay paths.

    The PTY log/screen buffer contains raw TTY bytes (ANSI escapes
    interleaved with rendered text), so this is more heuristic than
    :func:`paste_landed`.  Checks in priority order:

    1. :func:`fingerprint_in_bytes` — literal fingerprint present (fast path).
    2. :data:`_PASTE_CHIP_BYTES_RE` after :data:`INPUT_BOX_MARKER_BYTES` —
       Claude Code collapsed the large paste to a chip.

    The baseline / box-changed check is omitted here: the PTY log is an
    append-only stream of all terminal output rather than a snapshot of the
    current screen, so "the log grew" is too coarse a signal (the log always
    grows as ``claude`` runs).

    Args:
        raw:  The raw PTY log bytes (may contain ANSI escape sequences).
        fingerprint:  The whitespace-normalised briefing snippet returned by
            :func:`briefing_fingerprint`.

    Returns:
        ``True`` when the fingerprint or paste-chip marker is found.
    """
    if not fingerprint:
        return True

    # Fast path
    if fingerprint_in_bytes(raw, fingerprint):
        return True

    # Paste-chip scoped to after the input-box marker (reduces false positives
    # from any "[Pasted text" that might appear in the system prompt or header).
    marker_idx = raw.find(INPUT_BOX_MARKER_BYTES)
    if marker_idx != -1:
        region = raw[marker_idx:]
        if _PASTE_CHIP_BYTES_RE.search(region):
            return True

    return False


class ClaudePtyProvider(Provider):
    """Interactive ``claude`` (no ``-p``) attached to a PTY.

    This provider is **opt-in** — operators select it by adding a definition
    to ``coordinator.yml`` ``providers.definitions`` with
    ``type: claude-pty``, then either:

    * setting it as a per-repo ``provider:`` override, or
    * setting ``providers.default: <name>`` to make it the global default.

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
        """Subscription-billed interactive worker.

        ``billing_mode="subscription"``: interactive ``claude`` sessions are
        covered by the operator's Max/Pro plan and do not consume the
        ``claude -p`` metered credit pool — this is the entire reason the
        provider exists (#425).

        ``enforces_deny_list=False`` is set **conservatively**: this PR does
        not verify that the interactive PTY mode actually enforces
        ``--allowedTools`` / ``--permission-mode`` (the agent passes those
        flags as a safety hedge, but the runtime behaviour is unverified).
        The safety gate in :meth:`coord.agent.AgentServer.assign` keys off
        this flag and refuses write-capable assignment types on
        unverified-deny-list providers.  Flip to ``True`` only after the
        deny-list enforcement is demonstrably exercised in the PTY path.

        Other flags:
        * ``resume=False`` — no ``--resume`` flag wired in the PTY argv.
        * ``inject=False`` — no mid-session injection path exists for PTY.
        * ``cost_reporting=False`` — interactive ``claude`` does not emit a
          per-run cost figure in the TTY stream.
        * ``true_system_prompt=True`` — the argv still passes
          ``--system-prompt`` so the worker honours it the same way
          ``claude -p`` does.
        """
        return Capabilities(
            resume=False,
            inject=False,
            cost_reporting=False,
            true_system_prompt=True,
            enforces_deny_list=False,
            billing_mode="subscription",
            # #437: STRUCTURAL TOS-COMPLIANCE GATE.  Interactive Claude
            # Code on a Max/Pro subscription is covered ONLY for
            # human-attended use — running it unsupervised would violate
            # Anthropic ToS §3.7.  Flagging the provider here means the
            # coordinator's unattended dispatch paths (``coord plan`` /
            # ``coord approve`` / auto-review / auto-reassign /
            # reconciliation) refuse to select this provider at all.
            # The only path that may launch this backend is
            # ``coord assign --interactive``, which attaches the worker
            # to the operator's local TTY and is HUMAN-CLOSED.  See
            # :class:`~coord.providers.base.Capabilities`.
            human_attended_only=True,
        )

    # ── Core methods ──────────────────────────────────────────────────────────

    def build_command(  # noqa: PLR0912  (mirrors ClaudeProvider's spec.type branches)
        self,
        spec: "AssignmentSpec",
        *,
        resolved_model: str | None = None,
        system_prompt: str | None = None,
        allowed_tools: str | None = None,
        permission_mode: str = "acceptEdits",
    ) -> list[str]:
        """Build the interactive ``claude`` argv for *spec*.

        Unlike :meth:`ClaudeProvider.build_command` this:

        * Does **not** pass ``-p``.
        * Does **not** pass ``--input-format`` / ``--output-format`` /
          ``--verbose`` (interactive ``claude`` writes TUI output).
        * Still passes ``--system-prompt``, ``--allowedTools``,
          ``--permission-mode``, and ``--model`` so the operator's safety
          posture and the configured model are honoured even though
          enforcement is unverified.
        """
        # Deferred import — keeps the cycle latent.
        from coord.agent import (  # noqa: PLC0415
            DEFAULT_WORKER_BINARY,
            MILESTONE_CHAT_DENY_COMMANDS,
            MILESTONE_CHAT_SYSTEM_PROMPT,
            NEW_ISSUE_CHAT_DENY_COMMANDS,
            NEW_ISSUE_CHAT_SYSTEM_PROMPT,
            REFINEMENT_SYSTEM_PROMPT,
            REVIEW_DENY_COMMANDS,
            TEST_CHAT_SYSTEM_PROMPT,
            WORKER_PLAN_PROMPT,
            WORKER_SYSTEM_PROMPT,
            build_deny_prompt,
        )

        binary = self._binary if self._binary is not None else DEFAULT_WORKER_BINARY

        # Precedence: explicit resolved_model > spec.model > provider-
        # definition model (ProviderDef.model, threaded in via __init__) —
        # a plain ClaudePtyProvider().build_command(spec) with no definition
        # model stays self-consistent (self._model is None).
        if resolved_model is not None:
            effective_model = resolved_model
        elif spec.model is not None:
            effective_model = spec.model
        else:
            effective_model = self._model

        # Compute system_prompt / allowed_tools from spec.type when not
        # provided — same logic as ClaudeProvider so the safety hedge flags
        # carry the same content for the same spec.
        if system_prompt is None or allowed_tools is None:
            if spec.type == "plan":
                _sp = spec.system_prompt if spec.system_prompt else WORKER_PLAN_PROMPT
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
            elif spec.type == "smoke":
                # #2301: keep in sync with default_worker_command's and
                # ClaudeProvider.build_command's identical branch — smoke
                # gets SMOKE_SYSTEM_PROMPT and Read,Bash only, deliberately
                # WITHOUT `Monitor` (an await-a-notification tool: calling it
                # ends the turn, which ends a one-shot leg's session before
                # any wake-up can arrive) and without Edit/Write (a smoke leg
                # validates; it never mutates).
                from coord.smoke import SMOKE_SYSTEM_PROMPT  # noqa: PLC0415
                _sp = spec.system_prompt if spec.system_prompt else SMOKE_SYSTEM_PROMPT
                _sp += build_deny_prompt(spec.deny_commands)
                _at = "Read,Bash"
            elif spec.type == "review":
                # #2461: keep in sync with default_worker_command's identical
                # branch — a reviewer reads the diff and reports a verdict,
                # it edits and pushes nothing, so Read,Bash only (no
                # Edit/Write, no Monitor — same one-shot-session reasoning as
                # the `smoke` branch above).
                from coord.review import REVIEWER_SYSTEM_PROMPT  # noqa: PLC0415
                _sp = spec.system_prompt if spec.system_prompt else REVIEWER_SYSTEM_PROMPT
                _sp += build_deny_prompt(REVIEW_DENY_COMMANDS)
                _at = "Read,Bash"
            else:
                _sp = spec.system_prompt if spec.system_prompt else WORKER_SYSTEM_PROMPT
                _sp += build_deny_prompt(spec.deny_commands)
                # #2169: keep in sync with default_worker_command's and
                # ClaudeProvider.build_command's identical branch — Monitor
                # is the sanctioned bounded-poll tool for a backgrounded
                # long-running command. #2301: that grant is for *work*-shaped
                # legs only — smoke has its own branch above.
                _at = "Read,Edit,Write,Bash,Monitor"

            if system_prompt is None:
                system_prompt = _sp
            if allowed_tools is None:
                allowed_tools = _at

        # NOTE: no -p, no stream-json flags.  --system-prompt /
        # --allowedTools / --permission-mode are still passed as a safety
        # hedge even though enforcement is unverified in the PTY path
        # (capabilities().enforces_deny_list == False).
        #
        # #2821: also deliberately NO `--setting-sources user` here, and no
        # `_claude_md_system_prompt_suffix` embed — both of which
        # `default_worker_command` (coord/agent.py) DOES use for the `-p`
        # path. That's not an oversight; it was checked and rejected:
        #
        # 1. `--setting-sources user` would exclude the LOCAL settings
        #    scope, which is exactly where `coord init` (coord/commands/
        #    setup.py) writes the operator's own `.claude/settings.local.json`
        #    convenience allow-list (`Bash(coord *)` etc) for the repo
        #    they're interactively attached to. Restricting sources here
        #    would silently drop that allow-list for a session the operator
        #    is watching and expects to behave like their normal `claude`
        #    session — see coord/agent.py:~4919's identical tradeoff
        #    written up for the `-p` path (which deliberately keeps the
        #    default sources reasoning in reverse: it EXCLUDES project/local
        #    because a worker must NOT inherit host-local settings).
        # 2. `--bare` would close the CLAUDE.md-ambient-discovery gap
        #    without touching settings sources, but it also unconditionally
        #    disables OAuth/keychain reads — this fleet authenticates via
        #    `claude login`/OAuth, not `ANTHROPIC_API_KEY` (see #2462's
        #    emergency revert in coord/agent.py for the fleet-wide outage
        #    that caused). Not usable here either.
        #
        # Net effect, measured (#2821): this path pays ~7.7k ambient-
        # discovery tokens for CLAUDE.md vs. ~5.2k for the `-p` path's
        # explicit embed — a known, accepted cost of preserving the
        # operator's own interactive settings on a human-attended session,
        # not an accident. Revisit if Claude Code ever exposes a way to
        # suppress CLAUDE.md auto-discovery independent of settings-sources
        # and `--bare`'s OAuth side effect.
        argv: list[str] = [
            binary,
            "--system-prompt", system_prompt,
            "--allowedTools", allowed_tools,
            "--permission-mode", permission_mode,
        ]
        if effective_model:
            argv.extend(["--model", effective_model])
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
        """Best-effort one-shot argv for the PTY provider.

        Interactive ``claude`` does not support the non-interactive ``-p``
        mode one-shot pattern.

        The coordinator's unattended oneshot paths (brain planning,
        dashboard assistant) guard against human-attended-only providers
        via :func:`coord.providers.resolve_default_provider`, which raises
        :class:`ValueError` before ``oneshot_command()`` is reached when
        ``capabilities().human_attended_only=True``.  This method is
        therefore a belt-and-suspenders fallback: if it is somehow invoked
        despite the guard, it returns a valid ``claude -p`` style argv (the
        same shape as :class:`~.claude.ClaudeProvider`) rather than raising,
        so callers still get a workable command list.

        Note: like :class:`~.claude.ClaudeProvider`'s ``oneshot_command``,
        this does **not** thread in ``self._model`` / ``self._extra_args``
        / ``self._env`` (#1706) — oneshot calls don't take an
        ``AssignmentSpec`` and sit outside the assignment spawn path.

        Args:
            system_prompt: The system prompt for the call.
            output_format: Forwarded to the argv exactly as for
                :class:`~.claude.ClaudeProvider`.
        """
        from coord.agent import DEFAULT_WORKER_BINARY  # noqa: PLC0415
        binary = self._binary if self._binary is not None else DEFAULT_WORKER_BINARY
        cmd = [binary, "-p", "--system-prompt", system_prompt]
        if output_format is not None:
            cmd.extend(["--output-format", output_format])
        return cmd

    def initial_input(self, spec: "AssignmentSpec") -> bytes:
        """Return the briefing wrapped in a bracketed-paste block.

        Interactive ``claude`` reads typed input from the TTY, NOT stream-json
        envelopes.  A real briefing is multi-line, and the TUI swallows
        embedded newlines unless the whole block arrives as a bracketed paste
        (:data:`BRACKETED_PASTE_START` ... :data:`BRACKETED_PASTE_END`).  This
        returns only that paste block — **no trailing newline and no submit
        key**.  The submitting carriage return (``\\r``; ``\\n`` does NOT
        submit under the TUI's enhanced keyboard protocol) is written
        *separately* by :meth:`coord.agent.AgentServer._spawn_pty` after a
        short settle delay, because a CR glued onto ``ESC[201~`` in the same
        write is consumed by paste-end processing.  Verified live against
        interactive ``claude`` (#425 smoke).
        """
        body = spec.briefing.rstrip("\n")
        return BRACKETED_PASTE_START + body.encode("utf-8") + BRACKETED_PASTE_END

    def result_marker(self) -> str:
        """Sentinel written by the PTY spawn path after the worker exits.

        Interactive ``claude`` does not emit a stream-json ``result`` event,
        so the standard marker never appears.  :mod:`coord.agent` writes
        :data:`PTY_RESULT_MARKER` to the log after the subprocess exits so
        downstream code that polls for completion (the reap thread's
        ``_log_has_result`` helper) still has something to look for in the
        PTY case.
        """
        return PTY_RESULT_MARKER

    def env(self) -> dict[str, str]:
        """Extra environment variables from the provider definition (#1706).

        The agent already forces ``TERM`` via the PTY setup (see
        ``AgentServer._spawn_pty``); this returns a copy of
        ``ProviderDef.env`` (already ``${VAR}``-expanded by config parsing)
        threaded in via ``__init__``, merged on top by the spawn path. Empty
        dict when the provider was constructed with no ``env`` (matches
        pre-#1706 behaviour for no-config deployments).
        """
        return dict(self._env)

    def parse_log(
        self, log_path: str | Path, tail_bytes: int = 65536
    ) -> WorkerSummary:
        """Best-effort parse of the worker log.

        Delegates to :func:`coord.worker_events.parse_log`, which silently
        skips non-JSON lines.  Interactive ``claude`` writes raw TTY bytes
        (ANSI escape sequences, prompts, model text) rather than
        stream-json, so the returned :class:`WorkerSummary` will usually be
        mostly blank — that is the correct minimal-tail-parser behaviour
        for this provider until the PTY format gets its own structured
        log channel.
        """
        from coord.worker_events import parse_log  # noqa: PLC0415
        return parse_log(log_path, tail_bytes=tail_bytes)
