"""Agent server core: spawns `claude -p` subprocesses for assignments.

The HTTP layer is in `coord.agent_app`. This module is transport-agnostic and
tests can drive it directly without standing up a real server.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable

from coord import cargo_cache
from coord.config_reload import reload_config_if_stale
from coord.models import DELIVERABLE_ANALYSIS_LABEL
from coord.platform_paths import default_coord_dir

if TYPE_CHECKING:
    # Type-only import to give `_spawn_pty` a precise annotation without
    # eagerly triggering the import cycle (coord.providers.claude_pty imports
    # from coord.agent at runtime via deferred imports).  Runtime callers
    # still rely on the `isinstance(provider_obj, ClaudePtyProvider)` guard
    # in `_spawn` so the attribute access in `_spawn_pty` is safe.
    from coord.providers.claude_pty import ClaudePtyProvider


DEFAULT_WORKER_BINARY = "claude"

# DEFAULT_STATE_DIR is resolved lazily via __getattr__ below (#2781), not
# bound here at import time -- see that function's docstring.


def __getattr__(name: str) -> Path:
    """PEP 562 lazy fallback for ``DEFAULT_STATE_DIR`` (#2781).

    Pre-#2781 this was bound eagerly at import time, so ``$COORD_DIR`` set
    *after* this module was first imported -- e.g. by a pytest fixture --
    never reached it, unlike :func:`default_coord_dir` itself which is
    "computed fresh on every call" by design. This only engages when the
    name hasn't been bound directly in this module's namespace, so
    ``monkeypatch.setattr(coord.agent, "DEFAULT_STATE_DIR", ...)`` (used
    throughout the test suite) still takes priority exactly as before:
    Python calls ``__getattr__`` only when normal attribute lookup fails.
    """
    if name == "DEFAULT_STATE_DIR":
        return default_coord_dir()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Module logger — surfaced in the daemon / agent server logs so agent-side
# events (e.g. #1295 "sweep would touch a live worktree", "stash copied 0
# files") are visible without having to open the per-assignment log file.
_log = logging.getLogger(__name__)


def _dir_size(path: Path) -> int:
    """Return total bytes consumed by all regular files under *path*.

    Silently skips entries that can't be stat'd (deleted mid-walk, permission
    errors, etc.).  Returns 0 when *path* does not exist.
    """
    total = 0
    try:
        for p in path.rglob("*"):
            try:
                st = p.stat()
                if stat.S_ISREG(st.st_mode):
                    total += st.st_size
            except OSError:
                pass
    except OSError:
        pass
    return total

# Stamp captured at module import so `health()` can report when THIS
# process started. exec_restart() replaces the image, so the new
# process re-imports this module and the stamp updates — letting the
# CLI detect a real restart vs the old agent still answering.
_PROCESS_STARTED_AT: float = time.time()

# Statuses
PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
# #448: exit_code==0 but 0 commits pushed. Not a hard failure (auto_reassign
# would loop forever on "already implemented" reports); not a clean DONE
# either. Human should review and decide whether to re-dispatch or close.
ADVISORY = "advisory"
# #2234: a NARROWER shape of the #448 zero-commit exit — the worker's own
# final message shows it stopped because a STANDING repo rule (CLAUDE.md's
# "only the coordinator writes docs", or similar) makes the dispatched
# deliverable something no worker can complete, ever, on any attempt (see
# `_looks_like_policy_refusal`). This is deliberately a status distinct from
# ADVISORY, not a sub-case reported through it: every downstream consumer of
# ADVISORY (`coord drive`'s bounded retry, the queue's attempt budget, the
# terminal `blocked` bucket a human must `remove`) exists to handle "the
# worker got stuck or found nothing to do" — a condition retrying might fix.
# A policy refusal is the OPPOSITE: the worker did exactly the right thing,
# and retrying is guaranteed to reproduce the identical, correct refusal.
# Landing it in ADVISORY's bucket is what #2234 exists to stop — the #2195
# incident spent two drive attempts ($0.11 x 2, plus the overnight slot) and
# a terminal `blocked` rediscovering a rule that was never going to change.
REFUSED_POLICY = "refused_policy"

# #448: spec types that are expected to push commits, so a clean exit with
# zero commits is interesting (advisory).  Review/smoke workers commit
# nothing by design and must NOT be flagged advisory.
# #784: conflict-fix is EXCLUDED from the advisory check.  A successful
# rebase + force-push leaves the worktree 0 commits ahead of
# origin/<branch> by design (local and remote are in sync after the push).
# The briefing instructs the worker to exit non-zero on any failure, so
# exit_code==0 unambiguously means the fix landed — marking it advisory
# would block the auto re-enqueue and inflate the retry cap.
#
# Also used (#1323, revised #1357) to gate which assignment types get a
# `stash_unmatched_globs` diagnostic recorded when an artifact_paths glob
# misses — review/smoke/test/merge/conflict-fix routinely finish DONE
# without (re)producing every configured glob, so the diagnostic is only
# meaningful for "work" assignments.  Note that gate no longer affects
# `status` for the stash case (see `AgentServer._reap`) — only for the
# zero-commit case above.
_ADVISORY_TYPES = ("work",)

# #1534: spec types that MUST move their branch for the assignment to mean
# anything.  This is the zero-commit half of `_ADVISORY_TYPES` widened to the
# full `coord.models.WORK_LIKE_TYPES` set — a `test-author` or `mock-author`
# that exits cleanly having pushed nothing is exactly as much of a
# contradiction as a `work` one, and until #1534 those two types were silently
# excluded: the observed incident was a `test-author` killed by the Claude
# session usage limit that landed on the board as a clean `done` with zero
# commits, which then auto-dispatched a metered review against an empty diff.
#
# Deliberately a SEPARATE constant from `_ADVISORY_TYPES` rather than a
# widening of it: `_ADVISORY_TYPES` also gates the #1357 diagnostic-only
# `stash_unmatched_globs` note, which is about artifact globs, not commit
# counts, and whose "work"-only scoping was chosen for its own reasons.
# Kept as a literal tuple (not an import of WORK_LIKE_TYPES) because
# `coord/agent.py` is the agent-side module and must stay importable on a
# fleet machine without the coordinator's model layer.
#
# ``epic-decompose`` (#3132) gets a real worktree + branch and is documented
# (see WRITE_CAPABLE_SPEC_TYPES above) to commit/push the first slice's
# implementation — same mutation shape as `work`/`mock-author`, so a clean
# exit with zero commits ahead of base is exactly as much a contradiction
# here as it is for those two, and must not be trusted as a real "done"
# (the #1534/#2316 truncation incident this constant exists to catch).
_ZERO_COMMIT_TYPES = ("work", "mock-author", "test-author", "epic-decompose")

# #1394: assignment types whose worktree, when left dirty, holds real source
# the worker meant to ship — so an automatic WIP commit on the assignment
# branch is strictly better than deleting it.  Everything else is excluded on
# purpose:
#   * review / smoke / test / plan / *-chat are read-only by design, so their
#     dirt is build or test scratch and committing it would pollute the PR.
#   * conflict-fix / merge can die mid-rebase, where the "dirt" is conflict
#     markers — committing those to the branch would be actively harmful.
# Excluded types are still never force-deleted while dirty; they're preserved
# by KEEPING the worktree (see `AgentServer._rescue_uncommitted_work`).
#
# ``epic-decompose`` (#3132) is included for the same reason as `work`/
# `test-author`/`mock-author`: it leaves real, worth-keeping source in the
# worktree (the first slice's implementation), so a dirty exit deserves a
# rescue commit rather than force-deletion.
_WIP_RESCUE_TYPES = ("work", "fix", "test-author", "mock-author", "epic-decompose")

# #1394: subject prefix for the coordinator's rescue commit.  Deliberately
# loud — it lands on the assignment branch and a human (or the adversarial
# reviewer) must be able to tell at a glance that the worker did not author it.
_WIP_COMMIT_PREFIX = "WIP [coord-rescue]"

# #1394: above this many dirty paths, assume the worktree picked up something
# that should have been gitignored (a venv, `node_modules`, a build tree) in a
# repo whose .gitignore doesn't cover it, and do NOT commit it to the branch.
# The worktree is kept instead — still never deleted, just not auto-committed.
_WIP_RESCUE_MAX_FILES = 200

# Maximum number of terminal (done/failed/cancelled) assignments retained in
# memory and persisted to agent_state.json (#452).  Oldest entries (by
# finished_at, falling back to started_at) are dropped once this limit is
# exceeded.  Active (pending/running) assignments are never pruned.
# #715: lowered 50 -> 25.  Count-based capping alone wasn't enough — 50
# terminal entries x a full briefing each still serialized to ~0.9MB and
# took ~3s, tripping the coordinator's 3s health-poll timeout.  Terminal
# entries now also drop their briefing/system_prompt text (see
# AgentAssignment.to_status_dict()), so the smaller cap here is belt-and-
# suspenders on top of the real, size-based fix.
_COMPLETED_HISTORY_CAP = 25

# #1492: minimum interval between GitHub terminality sweeps of ADVISORY
# assignments (see `AgentServer._prune_terminal_advisory`). That sweep runs
# from the `/status` hot path (`list_assignments`), so a bare per-call check
# would put a `gh` round-trip per distinct advisory (repo, issue, branch) on
# every poll — the same "fail-open cost" #1472 accepted in `coord status`'s
# render-time filter, just moved one hop earlier. Gating it behind a cooldown
# means the coordinator-visible cost is one sweep per cooldown window,
# regardless of poll frequency, while still clearing a settled advisory
# entry agent-side (see module docstring for `_COMPLETED_HISTORY_CAP`) well
# within an operator's normal polling cadence.
_ADVISORY_TERMINAL_CHECK_COOLDOWN_S = 300.0

# #2234: markers that show up when a worker's OWN final message explains
# that it stopped because of a STANDING repo rule — not because it got stuck
# or ran out of turns. Matched only against the worker's own account (see
# `_looks_like_policy_refusal`), never against the briefing or issue body,
# which quote the same rules on every single dispatch and would false-
# positive on every assignment if matched directly. `claude.md` is the one
# filename standing worker-facing rules live in across this repo (see
# CLAUDE.md's own "Rules for workers" section, "Only the coordinator writes
# docs"); the rest are lifted verbatim from that same rule's text, which is
# the shape that triggered this issue (claude-coordinator#2195: an issue
# whose entire deliverable was a doc edit, dispatched to a worker that
# correctly refused and stopped). Deliberately permissive rather than
# requiring an exact quote — a weak/cheap model's terse refusal ("Confirmed:
# the rule exists verbatim at CLAUDE.md line 156...", the actual #2195
# transcript) may cite the rule without repeating "must not"/"forbidden"
# verbatim.
_POLICY_REFUSAL_MARKERS = (
    "claude.md",
    "files_forbidden",
    "repo rule",
    "repo's rule",
    "coordinator work",
    "only the coordinator",
    "coordinator-only",
    "should never have been dispatched",
)


def _looks_like_policy_refusal(text: str | None) -> bool:
    """#2234: does *text* — the worker's own final message — read as a
    refusal grounded in a STANDING repo-rule prohibition, rather than a
    stuck/incomplete session?

    Only ever consulted on the zero-commit/clean-exit shape (`_reap` calls
    this exactly where it would otherwise set `zero_commit_reason` — see
    below): a worker that pushed real commits never reaches this check, so a
    successful run that happens to mention `CLAUDE.md` in passing (e.g.
    "per CLAUDE.md I ran the build before committing") carries no risk of
    being reclassified. Within that already-narrow shape, an ordinary
    `STUCK:`-style report (out of turns, couldn't find the code, a flaky
    test) mentions none of :data:`_POLICY_REFUSAL_MARKERS`, so it is left
    exactly as ADVISORY as it always was.
    """
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _POLICY_REFUSAL_MARKERS)


# #2316: `WorkerSummary.stop_reason` values that mean the model was CUT OFF
# by an output-token ceiling before it could finish — as opposed to a normal
# stop (`end_turn`/`stop_sequence`, see `coord.progress`'s identical "unusual
# stop" allowlist). `"length"` is opencode's `step_finish` reason
# (`coord.providers.opencode.OpenCodeProvider.parse_log`, gated on opencode's
# `OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX`); `"max_tokens"` is Anthropic's own
# `stop_reason` for the same shape. Either one on a 0-commit clean exit means
# the worker never got a turn to write anything — the space-invaders#1
# incident (13 successful tool calls, then one reasoning block burned the
# entire 32k-token budget and exited 0 with nothing on disk) is exactly this:
# `exit_code == 0` looked like "worker exited cleanly but pushed 0 commits"
# (the #448 ADVISORY reading) when it was actually a truncation nobody could
# have acted on differently. See `_reap`, which checks this BEFORE the #448
# zero-commit downgrade.
_TRUNCATION_STOP_REASONS = frozenset({"length", "max_tokens"})

# #2321: the opencode-specific knob named in the GitHub comment so an
# operator reading "cut off at its output limit" doesn't have to go
# rediscover — as the #2316 investigation had to — that the ceiling is
# adjustable per provider definition.
_OPENCODE_OUTPUT_TOKEN_MAX_ENV = "OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"


def _format_truncation_reason(stop_reason: str, provider_name: str | None) -> str:
    """#2316: human-readable diagnostic for a 0-commit run truncated by an
    output-token ceiling — used for both `AgentAssignment.truncation_reason`
    (persisted `failure_reason`) and `AgentAssignment.error` (the GitHub
    completion comment's `error=` text, see `coord.notify`'s FAILED arms).

    Deliberately says "cut off" rather than "exited cleanly" — `exit_code ==
    0` is what a truncated run looks like, not evidence the worker finished.
    Names :data:`_OPENCODE_OUTPUT_TOKEN_MAX_ENV` for an opencode worker
    (#2321: the ceiling is raisable per provider definition) so an operator
    doesn't have to rediscover that from scratch.
    """
    base = "the model was cut off at its output limit before writing anything"
    if provider_name == "opencode":
        return (
            f"{base} (stop_reason={stop_reason!r}; opencode's output-token "
            f"ceiling is set by {_OPENCODE_OUTPUT_TOKEN_MAX_ENV} and is "
            "raisable per provider definition — #2321)"
        )
    return f"{base} (stop_reason={stop_reason!r})"


# ── Reap tuning ───────────────────────────────────────────────────────────────
# claude-cli sometimes does not exit after emitting its final
# `{"type":"result"}` message — a child process (MCP server, tool subprocess)
# holds the session's process group open and proc.wait() blocks indefinitely.
# The reap thread detects logical completion from the log and force-kills the
# group after a grace period. See #228 for the underlying bug.
_REAP_POLL_INTERVAL = 5.0        # seconds between proc.wait timeout attempts
_REAP_GRACE_AFTER_RESULT = 30.0  # grace period after result line before SIGTERM
_REAP_MAX_WAIT = 2 * 60 * 60.0   # absolute max wait (2 hours) — last-resort safety net
_RESULT_LINE_MARKER = b'"type":"result"'
# PTY workers (ClaudePtyProvider) never emit stream-json, so the pump thread
# stamps this sentinel after the subprocess exits.  MUST stay byte-equal to
# ``coord.providers.claude_pty.PTY_RESULT_MARKER`` (kept as bytes here to
# avoid a module-level import cycle with coord.providers.claude_pty).  The
# sync is asserted in ``tests/test_agent_reap.py::
# test_pty_marker_bytes_sync_with_provider_string``.
_PTY_RESULT_LINE_MARKER = b"# pty: worker exited"

# #425 (now retired in #437) used to define ``_PTY_SUBMIT_SETTLE_S`` here for
# auto-submitting the briefing after the bracketed paste.  Auto-submit was
# removed for ToS §3.7 compliance (#437): the human-attended interactive path
# PRE-FILLS the input box and the operator presses Enter themselves.  No
# coordinator-side submit means no settle delay is needed.

# #425 PTY readiness: ESC[?2004h (bracketed-paste enable) is emitted EARLY,
# while the TUI is still drawing its first frame — a briefing pasted at that
# instant is silently dropped.  After seeing the enable marker we additionally
# wait for the init render to go quiet (log size stable for _PTY_READY_QUIESCE_S,
# overall cap _PTY_READY_QUIESCE_CAP_S) before pasting.  Verified reliable
# against interactive `claude` (#425 smoke: pasting on the enable marker alone
# fails; quiescence-gated pasting succeeds).
_PTY_READY_QUIESCE_S = 0.8
_PTY_READY_QUIESCE_CAP_S = 8.0

# #865: requiring INPUT_BOX_MARKER_BYTES is a STRONGER signal than bare
# quiescence, but making it a hard requirement for the fast quiescence exit
# would regress the case where the render never emits a recognisable marker
# at all (an older CLI, an unusual terminal, or — as caught by the test
# suite — a worker that exits before drawing anything): without this
# fallback that case spins for the FULL _PTY_READY_QUIESCE_CAP_S instead of
# exiting once the (marker-less) screen has simply gone quiet.  So: exit on
# quiescence alone after the longer _PTY_READY_QUIESCE_NO_MARKER_S window if
# the marker still hasn't shown up by then; exit sooner, after the shorter
# _PTY_READY_QUIESCE_S, once it has.  The overall cap remains the ultimate
# backstop either way.
_PTY_READY_QUIESCE_NO_MARKER_S = 1.6

# #865: the pre-#865 pre-fill was fire-and-forget — a single ``os.write`` with
# no verification that the briefing actually landed, which a mid-startup
# repaint (promo banner, MCP/auth notice) could silently discard.  After
# pasting we re-read the log tail (the pump thread is already writing PTY
# master output there) and check for a fingerprint of the briefing, retrying
# up to _PTY_INJECT_MAX_ATTEMPTS times with a short backoff.  Mirrors the
# tmux-path constants in ``coord.interactive`` (``_INJECT_MAX_ATTEMPTS`` /
# ``_INJECT_VERIFY_SETTLE_S`` / ``_INJECT_RETRY_BACKOFF_S``) — kept as a
# separate copy here rather than a shared import to dodge the
# coord.agent <-> coord.interactive import cycle both modules already avoid.
_PTY_INJECT_MAX_ATTEMPTS = 3
_PTY_INJECT_VERIFY_SETTLE_S = 0.5
_PTY_INJECT_RETRY_BACKOFF_S = 0.4

# First-output (TTFT) watchdog default and the distinct exit code used when it
# fires, so `_reap` records the assignment as FAILED (any non-zero exit) and the
# `concurrency.auto_reassign` path re-dispatches it. See #299 and the upstream
# daemon-spawn stall report (anthropics/claude-code#56268).
_FIRST_OUTPUT_TIMEOUT = 600.0    # seconds of zero output before the watchdog kills
NO_FIRST_OUTPUT_EXIT = 124       # exit code reported when the TTFT watchdog fires

# #3145: the pre-stash `build_command`'s own ceiling (`_run_pre_stash_build`
# below), named here rather than inlined at its `subprocess.run` call because a
# SECOND module is sized against it: `coord/drive_queue.py`'s
# `DISPATCH_FAILURE_MIN_BACKOFF_SECONDS` is deliberately wider than this, so a
# dispatch-only death's one remaining retry fires only AFTER any single such
# stall has ended rather than landing inside the very stall it is retrying
# (the 2026-09-05 vimcode#821 shape). Two hand-copied literals in two modules
# answering one question is the split-brain #2085 warns about, so the ordering
# between them is asserted directly, in
# `tests/test_drive_queue.py::test_the_dispatch_failure_backoff_outlasts_a_pre_stash_build_stall`
# — raising this ceiling (or lowering that floor) without moving the other
# fails that test instead of silently re-opening the incident.
PRE_STASH_BUILD_TIMEOUT_SECONDS = 600.0

# #2131: distinct exit code for a leg killed by the per-leg spend ceiling. It
# must NOT collide with NO_FIRST_OUTPUT_EXIT (or any plausible worker exit
# code) — `_reap` keys the `spend_ceiling_reason` stamp off it, and that stamp
# is the ONLY thing that makes a ceiling kill distinguishable from a crash for
# `coord retry` and the auto-reassign skip. Any non-zero value still lands the
# assignment on FAILED via the existing reap branch; the value only selects
# which diagnostic gets attached.
SPEND_CEILING_EXIT = 125

# #2638: a suspended worker (`systemctl suspend`, a laptop lid closed, a VM
# the hypervisor pauses) held its assignment `running` for 10.5h with nothing
# in the fleet noticing — the only watchdogs above are TTFT (disarms
# permanently on first output) and the per-leg spend ceiling (opt-in, and
# only ever armed for headless legs the coordinator gave a `cost_ceiling_usd`
# to). Neither measures *how long the leg has been alive*. Two independent
# nets close that gap, both driven off `wall_clock` (`time.time()` by
# default) rather than `clock` (`time.monotonic()`): Linux's
# `CLOCK_MONOTONIC` does not advance across a suspend (s2idle), so a
# monotonic-only ceiling silently fails to fire for exactly the case it
# exists to catch — this incident's own worker-side `timeout 590` wrapper
# proved it, firing 28s *after* the wake, having counted only awake seconds.
#
# 1. **Wall-clock runtime ceiling** (`RUNTIME_CEILING_EXIT`): a generous
#    (hours, not minutes) cap on total wall-clock runtime, exactly as
#    "the worker has been running way too long" reads to an operator —
#    genuinely long legs are common, so this is a last-resort net, not a
#    scheduler.
# 2. **Host-sleep detection** (`HOST_SLEEP_EXIT`): a suspend produces an
#    unambiguous signature — wall-clock elapsed diverges sharply from
#    monotonic elapsed over the SAME poll interval, because monotonic time
#    only advances while the host is actually running. This fires almost
#    immediately on wake, well before any runtime ceiling would, and is
#    stamped with its own reason: a leg that slept through a 10-hour
#    suspend is not a result anyone should trust resumed, whatever it
#    does next.
#
# Distinct exit codes (must not collide with NO_FIRST_OUTPUT_EXIT/
# SPEND_CEILING_EXIT or any plausible worker exit code) so `_reap` can key
# its own distinguishing `failure_reason` off each, the same way it already
# does for `SPEND_CEILING_EXIT` — see `RUNTIME_CEILING_REASON_PREFIX`/
# `HOST_SLEEP_REASON_PREFIX` below.
RUNTIME_CEILING_EXIT = 126
HOST_SLEEP_EXIT = 127

# Generous default: some legs legitimately run for hours. `None`/`<= 0`
# disables the ceiling entirely — a leg with no ceiling configured (either
# because the coordinator resolved `AssignmentSpec.runtime_ceiling_s` to a
# non-positive value, or because a caller of `_wait_for_proc_or_result`
# passes `runtime_ceiling_s=None` directly) behaves exactly as it did before
# #2638.
_DEFAULT_RUNTIME_CEILING_S = 6.0 * 60.0 * 60.0  # 6 hours

# Minimum wall-vs-monotonic divergence measured over a SINGLE poll interval
# that is unambiguously a host suspend rather than ordinary thread-scheduling
# jitter, a GC pause, or a loaded box briefly starving this thread. Comfortably
# larger than `_REAP_POLL_INTERVAL` so an awake-but-slow poll loop can never
# false-positive; tailscaled's own "time jump detected" log line (this
# incident's actual wake signal) is the identical wall-vs-monotonic
# comparison.
_HOST_SLEEP_DIVERGENCE_S = 60.0

# #2638: stable, greppable `failure_reason` prefixes — mirror
# `coord.spend_ceiling.SPEND_CEILING_REASON_PREFIX`'s contract exactly. This
# is the ONLY thing that lets `coord retry`, the auto-reassign skip, and
# `coord status`/`coord health` tell a runtime-ceiling or host-sleep kill
# apart from an ordinary crash. NEVER change these strings without updating
# every `is_runtime_ceiling_reason`/`is_host_sleep_reason` caller.
RUNTIME_CEILING_REASON_PREFIX = "runtime ceiling — "
HOST_SLEEP_REASON_PREFIX = "host sleep detected — "


def format_runtime_ceiling_reason(wall_elapsed_s: float, ceiling_s: float) -> str:
    """Render the one-liner stamped onto `failure_reason` for a runtime-
    ceiling kill. Example: ``"runtime ceiling — ran 6.02h, past the 6.00h
    ceiling (#2638)"``."""
    return (
        f"{RUNTIME_CEILING_REASON_PREFIX}ran {wall_elapsed_s / 3600.0:.2f}h, "
        f"past the {ceiling_s / 3600.0:.2f}h ceiling (#2638)"
    )


def is_runtime_ceiling_reason(reason: str | None) -> bool:
    """True iff *reason* is a `failure_reason` stamped by the runtime ceiling."""
    return bool(reason) and reason.startswith(RUNTIME_CEILING_REASON_PREFIX)


def format_host_sleep_reason(wall_delta_s: float, mono_delta_s: float) -> str:
    """Render the one-liner stamped onto `failure_reason` for a host-sleep
    kill. Example: ``"host sleep detected — wall clock advanced 37800s while
    only 5s of monotonic time elapsed; the host likely suspended mid-leg
    (#2638)"``."""
    return (
        f"{HOST_SLEEP_REASON_PREFIX}wall clock advanced {wall_delta_s:.0f}s "
        f"while only {mono_delta_s:.0f}s of monotonic time elapsed; the host "
        "likely suspended mid-leg (#2638)"
    )


def is_host_sleep_reason(reason: str | None) -> bool:
    """True iff *reason* is a `failure_reason` stamped by host-sleep detection."""
    return bool(reason) and reason.startswith(HOST_SLEEP_REASON_PREFIX)


def _append_log_line(log_path: str, line: str) -> None:
    """Best-effort append of a single line to the assignment log. Never raises."""
    try:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def _killpg_safe(pid: int, sig: int) -> None:
    """`os.killpg` that swallows already-gone/permission errors."""
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _log_has_result(log_path: str) -> bool:
    """Return True if the log contains a final result event.

    Two completion markers are recognised:

    * the stream-json ``{"type":"result"}`` event written by ``claude -p`` /
      :class:`ClaudeProvider`, and
    * the PTY sentinel ``# pty: worker exited`` (stamped by the PTY pump
      thread after the interactive ``claude`` subprocess exits — see
      :data:`coord.providers.claude_pty.PTY_RESULT_MARKER`).

    Both are matched **per line, structurally** rather than as a raw substring
    over the whole file. A naive substring scan false-positives whenever a
    worker merely *reads* a file that contains the marker text — e.g. a task
    touching ``coord/agent.py`` or ``coord/providers/claude*.py`` echoes the
    literal back inside a ``tool_result`` payload — which reaps the worker
    mid-task and records it as a clean ``done`` (the #324/#325 no-op
    completions, 2026-06-06). So:

    * the stream-json marker counts only when the line parses as a JSON object
      whose **top-level** ``type`` is ``"result"`` (a ``tool_result`` carrying
      the string is a top-level ``"type":"user"`` line and is ignored), and
    * the PTY sentinel counts only as a standalone log line (it is a
      ``#``-comment the coordinator writes itself; a worker reading the source
      sees it embedded in a ``{...}`` JSON line, never as a bare line).
    """
    try:
        with open(log_path, "rb") as f:
            for raw in f:
                if raw.lstrip().startswith(_PTY_RESULT_LINE_MARKER):
                    return True
                if _RESULT_LINE_MARKER not in raw:
                    continue
                try:
                    event = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if isinstance(event, dict) and event.get("type") == "result":
                    return True
            return False
    except OSError:
        return False


def _log_has_output(log_path: str) -> bool:
    """Return True once the worker has produced any output beyond the spawn header.

    `_spawn` writes `# ...` comment lines (the argv header and any pull notes)
    before the worker starts; the worker's stream-json output is never a
    `#`-comment. So the watchdog considers the worker to have produced output
    as soon as the log contains any non-blank, non-`#`-comment line. A
    rate-limited worker emits turn / `[rate_limit]` events, so it trips this
    check and is never killed by the TTFT watchdog — only truly silent (zero
    output) hangs are caught.
    """
    try:
        with open(log_path, "rb") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                if line.startswith(b"#"):
                    continue
                return True
    except OSError:
        return False
    return False


def _maybe_bash_wrap(argv: list[str], enabled: bool) -> list[str]:
    """Optionally wrap *argv* in a transient `bash -c 'exec ...'` parent.

    When enabled, the immediate parent of `claude` is a short-lived bash that
    `exec`s into claude — same PID, so `start_new_session`, `proc.pid`, the
    stdin pipe, and process-group kills all behave identically to a bare
    spawn. This is the upstream headline fix for the daemon-spawn freeze
    (anthropics/claude-code#56268). When disabled, the bare argv is returned.
    """
    if not enabled:
        return argv
    return ["bash", "-c", "exec " + shlex.join(argv)]


def _wait_for_proc_or_result(
    proc: subprocess.Popen,
    log_path: str,
    *,
    poll_interval: float = _REAP_POLL_INTERVAL,
    grace_after_result: float = _REAP_GRACE_AFTER_RESULT,
    max_wait: float = _REAP_MAX_WAIT,
    first_output_timeout: float = _FIRST_OUTPUT_TIMEOUT,
    cost_ceiling_usd: float | None = None,
    read_cost_usd: "Callable[[], float | None] | None" = None,
    runtime_ceiling_s: float | None = _DEFAULT_RUNTIME_CEILING_S,
    sleep_divergence_s: float = _HOST_SLEEP_DIVERGENCE_S,
    killpg: Callable[[int, int], None] = _killpg_safe,
    log_has_result: Callable[[str], bool] = _log_has_result,
    log_has_output: Callable[[str], bool] = _log_has_output,
    clock: Callable[[], float] = time.monotonic,
    wall_clock: Callable[[], float] = time.time,
) -> int:
    """Wait for `proc` to exit; force-kill its process group if it hangs after
    the worker emitted its final result event.

    Returns the worker's exit code. Always returns within roughly `max_wait`
    seconds even if the process group refuses to die. If the worker's result
    line was observed before we killed it, returns 0 — the work is logically
    complete, only the runtime is being torn down.

    First-output (TTFT) watchdog: if ``first_output_timeout > 0`` and the
    worker produces no output at all within that many seconds, its process
    group is killed and :data:`NO_FIRST_OUTPUT_EXIT` is returned so `_reap`
    marks the assignment FAILED. Once any output is seen the watchdog is
    satisfied permanently — it never re-arms — so a slow-but-emitting (e.g.
    rate-limited) worker is never killed by it. See #299.

    Per-leg spend ceiling (#2131): when ``cost_ceiling_usd`` is a positive
    number and ``read_cost_usd`` is supplied, the worker's live spend is
    sampled on every poll.  At :data:`coord.spend_ceiling.WARN_FRACTION` of
    the ceiling a one-shot ``STATUS:`` warning is written to the worker's own
    log — so a watching operator gets a chance to intervene, and the kill is
    legible after the fact.  At or above the ceiling the process group is
    killed and :data:`SPEND_CEILING_EXIT` is returned so ``_reap`` records a
    ceiling kill rather than a generic failure.

    Three properties of that check matter and are deliberate:

    * **Fail open.** ``read_cost_usd`` returning ``None`` (unreadable log,
      not stream-json, nothing priceable yet) never kills anything — killing
      real work over a parse failure is worse than the overspend.
    * **Never after the result event.** Once the worker has emitted its final
      ``result`` the leg is logically finished and the money is already
      spent; killing then would only mislabel a completed leg as a ceiling
      kill. So the check is skipped once ``result_seen_at`` is set.
    * **SIGKILL, not SIGTERM.** The whole point is to stop spending now; a
      graceful shutdown that keeps talking to the API defeats it. Whatever
      the worker committed is still on disk, and ``_reap``'s existing
      safety-net push still runs afterwards.

    Wall-clock runtime ceiling + host-sleep detection (#2638): two more
    watchdogs, both driven off ``wall_clock`` (real time, default
    ``time.time``) rather than ``clock`` (monotonic, default
    ``time.monotonic``) — a suspended host freezes ``CLOCK_MONOTONIC`` but
    not the wall clock, so a monotonic-only ceiling never fires for exactly
    the case it exists to catch.

    * If ``runtime_ceiling_s`` is a positive number, the process group is
      killed and :data:`RUNTIME_CEILING_EXIT` returned once wall-clock
      elapsed since this call started reaches it. Generous default (hours);
      ``None``/``<= 0`` disables it — a leg with no ceiling configured
      behaves exactly as it did pre-#2638.
    * On every poll, this poll interval's wall-clock delta is compared to its
      monotonic delta. A real, running process advances both at the same
      rate, so the two track each other tightly; only a suspend/resume
      produces a gap this large over one interval (the same signal
      tailscaled's own "time jump detected" line reports). When the
      divergence reaches ``sleep_divergence_s`` the process group is killed
      and :data:`HOST_SLEEP_EXIT` returned — independent of, and typically
      well before, the runtime ceiling above: a leg that slept through a
      multi-hour suspend should never be trusted to resume cleanly, whatever
      its eventual wall-clock age would have been.

    The keyword-only parameters exist for tests to inject short timeouts and
    mock kill/clock/cost/wall-clock behavior.
    """
    start = clock()
    wall_start = wall_clock()
    last_mono = start
    last_wall = wall_start
    result_seen_at: float | None = None
    output_seen = False
    cost_warned = False
    ceiling_armed = bool(
        cost_ceiling_usd and cost_ceiling_usd > 0 and read_cost_usd is not None
    )
    if ceiling_armed:
        # Hoisted out of the poll loop — this runs every `poll_interval`.
        from coord.spend_ceiling import WARN_FRACTION  # noqa: PLC0415

        warn_at = cost_ceiling_usd * WARN_FRACTION

    while True:
        try:
            return proc.wait(timeout=poll_interval)
        except subprocess.TimeoutExpired:
            pass

        now_mono = clock()
        now_wall = wall_clock()
        elapsed = now_mono - start

        # #2638: host-sleep detection — compare THIS poll interval's
        # monotonic delta to its wall-clock delta, not the cumulative
        # elapsed-since-start (a long-but-genuinely-running leg has both
        # deltas large and roughly equal the whole way through; only a
        # suspend produces a one-interval gap this size). Checked before
        # every other watchdog: it is the more specific diagnosis, and
        # explains why a leg might otherwise look TTFT-silent or ceiling-
        # breached.
        #
        # Gated on `result_seen_at is None` for the identical reason the
        # runtime ceiling and #2131's spend ceiling both are: once the
        # worker has logically finished, the grace-period teardown below
        # owns the outcome. Without this gate, a suspend/resume straddling
        # that short post-result teardown window would kill and mislabel an
        # already-finished leg as a host-sleep kill instead of letting it
        # land DONE.
        mono_delta = now_mono - last_mono
        wall_delta = now_wall - last_wall
        last_mono, last_wall = now_mono, now_wall
        if (
            result_seen_at is None
            and wall_delta - mono_delta >= sleep_divergence_s
        ):
            _append_log_line(
                log_path,
                "# reap: SIGKILL — host sleep detected (wall clock advanced "
                f"{wall_delta:.0f}s vs {mono_delta:.0f}s monotonic over one "
                "poll interval) (#2638)\n",
            )
            killpg(proc.pid, signal.SIGKILL)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            return HOST_SLEEP_EXIT

        # First-output / TTFT watchdog: catch a worker that emits zero bytes.
        # Once any output is seen the watchdog is satisfied forever (never
        # re-armed) so slow-but-emitting workers pass.
        if first_output_timeout > 0 and not output_seen:
            if log_has_output(log_path):
                output_seen = True
            elif elapsed >= first_output_timeout:
                _append_log_line(
                    log_path,
                    f"# reap: no first output in {first_output_timeout:.0f}s — "
                    "killing process group (suspected daemon-spawn stall)\n",
                )
                killpg(proc.pid, signal.SIGKILL)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                return NO_FIRST_OUTPUT_EXIT

        # Detect logical completion: worker emitted its final result event.
        if result_seen_at is None and log_has_result(log_path):
            result_seen_at = clock()
            _append_log_line(
                log_path,
                "# reap: worker emitted result; awaiting clean exit\n",
            )

        # #2638: wall-clock runtime ceiling — measured from `wall_start`, NOT
        # from the monotonic `start` above, so a suspend that stalls
        # monotonic time cannot silently hold this open forever. Gated on
        # `result_seen_at is None` for the identical reason #2131's spend
        # ceiling is: once the worker has logically finished, the grace-
        # period teardown below owns the outcome — killing here instead
        # would mislabel a completed leg as a ceiling kill merely because
        # its teardown straddled the ceiling.
        if runtime_ceiling_s and runtime_ceiling_s > 0 and result_seen_at is None:
            wall_elapsed = now_wall - wall_start
            if wall_elapsed >= runtime_ceiling_s:
                _append_log_line(
                    log_path,
                    "# reap: SIGKILL — wall-clock runtime ceiling breached "
                    f"({wall_elapsed:.0f}s of {runtime_ceiling_s:.0f}s) "
                    "(#2638)\n",
                )
                killpg(proc.pid, signal.SIGKILL)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                return RUNTIME_CEILING_EXIT

        # #2131: per-leg spend ceiling. Only while the leg is still running
        # (see the docstring for why a post-`result` kill is never useful),
        # and only ever acting on a value the meter could actually read.
        if ceiling_armed and result_seen_at is None:
            try:
                spent = read_cost_usd()
            except Exception:  # noqa: BLE001 — fail open, never break the reap
                spent = None
            if spent is not None:
                if not cost_warned and spent >= warn_at:
                    cost_warned = True
                    _append_log_line(
                        log_path,
                        f"STATUS: spend ${spent:.2f} has passed "
                        f"{WARN_FRACTION:.0%} of the ${cost_ceiling_usd:.2f} "
                        "per-leg ceiling → the leg will be killed if it "
                        "reaches the ceiling → confidence: high (#2131)\n",
                    )
                if spent >= cost_ceiling_usd:
                    _append_log_line(
                        log_path,
                        f"# reap: SIGKILL — spend ceiling breached "
                        f"(${spent:.2f} of ${cost_ceiling_usd:.2f}) (#2131)\n",
                    )
                    killpg(proc.pid, signal.SIGKILL)
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                    return SPEND_CEILING_EXIT

        if result_seen_at is not None and clock() - result_seen_at >= grace_after_result:
            # Worker logically done but process group still alive — force-kill.
            _append_log_line(
                log_path,
                f"# reap: SIGTERM process group after {grace_after_result:.0f}s grace\n",
            )
            killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _append_log_line(
                    log_path,
                    "# reap: SIGKILL process group (SIGTERM ignored)\n",
                )
                killpg(proc.pid, signal.SIGKILL)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    _append_log_line(
                        log_path,
                        "# reap: process group survived SIGKILL; abandoning wait\n",
                    )
            return 0  # Worker's work was complete before we killed the runtime.

        if elapsed >= max_wait:
            # Absolute safety net: worker never emitted a result and ran past
            # the max-wait cap. Treat as failed and kill the group.
            _append_log_line(
                log_path,
                f"# reap: SIGKILL after {max_wait:.0f}s max-wait without result line\n",
            )
            killpg(proc.pid, signal.SIGKILL)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            return 137  # SIGKILL convention


@dataclass
class AssignmentSpec:
    """What the coordinator hands to an agent. Stable shape on the wire."""

    repo_name: str
    repo_path: str
    issue_number: int
    issue_title: str
    briefing: str
    files_allowed: list[str] = field(default_factory=list)
    files_forbidden: list[str] = field(default_factory=list)
    branch: str | None = None
    pull_repos: list[str] = field(default_factory=list)
    artifact_paths: list[str] = field(default_factory=list)
    # #352: per-repo new-issue guidance (only for type="new-issue-chat").
    new_issue_guidance: str = ""
    # "work" (default) or "review". The agent treats both the same — what
    # differs is the briefing and (for reviewers) the system prompt.
    type: str = "work"
    # Optional override of WORKER_SYSTEM_PROMPT. Reviewers need a different
    # system prompt because they're allowed to run `gh pr review` while
    # workers are not.
    system_prompt: str | None = None
    # PR number being reviewed (only set for type="review").
    review_target: str | None = None
    # Command patterns the worker must not run (prompt-level enforcement).
    deny_commands: list[str] = field(default_factory=list)
    # Claude model tier alias (e.g. "haiku", "sonnet", "opus"). When None,
    # the worker command omits --model so claude -p picks its default.
    model: str | None = None
    # When True, ignore existing issue-N-* branches and create a fresh branch
    # from the default branch. Used by --force dispatch to avoid stale branches.
    fresh_branch: bool = False
    # #target_branch: override the slugified-title-derived branch name with
    # an explicit existing branch.  Used by the auto-loop's fix dispatch so
    # the fix worker pushes commits to the ORIGINAL work's branch (and the
    # same PR gets the fix) instead of creating a new orphan branch from
    # the `[fix-N]` issue-title prefix.  When set, the agent checks out
    # this branch directly instead of deriving from issue_number + title.
    target_branch: str | None = None
    # #315: when set, pass `--resume <session_id>` to claude -p so it loads
    # the prior conversation and continues it.  The `briefing` field IS the
    # new user message; claude reads the prior conversation via --resume and
    # then sees this as the next user turn.  Only set for chat-continue
    # re-dispatches; regular work/plan/review dispatches leave this None.
    resume_session_id: str | None = None
    # #425: optional provider override naming a provider in the agent's
    # configured providers registry.  When None the agent uses its default
    # ``claude -p`` spawn path (byte-identical to pre-#425 behaviour) — no
    # provider lookup is performed and no safety gate runs.  When set, the
    # named provider's :meth:`~coord.providers.base.Provider.capabilities`
    # are inspected by :meth:`AgentServer.assign` and the spawn is routed
    # through :meth:`AgentServer._spawn_pty` if the provider is a
    # :class:`~coord.providers.claude_pty.ClaudePtyProvider`.
    #
    # #1796: when ``provider`` is set but does not match a key in this
    # agent's local ``providers`` registry — always true for a config-free
    # agent, which has none — resolution now falls to ``provider_def``
    # below rather than silently degrading to the legacy claude path.
    provider: str | None = None
    # #1796: the resolved provider's definition (type/binary/model/env/
    # extra_args), serialized by ``coord.providers.provider_def_to_wire`` and
    # carried alongside ``provider`` so a config-free agent (no local
    # ``providers.definitions`` to look ``provider`` up in — see
    # docs/EPHEMERAL_WORKERS.md) can still build the right
    # :class:`~coord.providers.base.Provider` instance, via
    # :func:`coord.providers.build_provider_from_wire`. ``None`` when the
    # coordinator itself had no ``ProviderDef`` for the resolved name, or
    # when ``provider`` itself is ``None``.  See
    # :meth:`AgentServer._resolve_provider` for the full resolution chain —
    # this field existing does not, on its own, get honoured anywhere else.
    provider_def: "dict[str, Any] | None" = None
    # #2131: per-leg spend ceiling in USD, resolved coordinator-side from
    # `budget.ceiling_for(type)` (coord/config.py) and carried here rather
    # than read from the agent's own config — a config-free agent
    # (docs/EPHEMERAL_WORKERS.md) has none to read. `None` (the default, and
    # what every agent sees when the coordinator has no `budget:` block)
    # means NO CEILING: the reap's watchdog never samples cost and behaves
    # exactly as it did pre-#2131.
    #
    # `coord/dispatch.py` omits the key entirely when there is no ceiling, so
    # an agent predating this field never sees an unrecognized kwarg (
    # `AssignmentSpec(**body)` in agent_app.py 400s on those) unless the
    # operator has actually configured one.
    cost_ceiling_usd: float | None = None
    # #2188: the issue's GitHub label names, mirroring `Proposal.issue_labels`
    # (#1430) — carried on the wire so a reap running on a config-free agent
    # (docs/EPHEMERAL_WORKERS.md; no local DB or GitHub token to ask) can
    # still see `coord.models.DELIVERABLE_ANALYSIS_LABEL` without an extra
    # round-trip. Only `coord/dispatch.py`'s `dispatch()` populates this (from
    # `proposal.issue_labels`, itself only set for `type="work"` dispatches —
    # see #1430's `model_for_labels`/`provider_for_labels` gating), and only
    # when non-empty — an agent predating this field 400s on an unrecognized
    # kwarg (`AssignmentSpec(**body)` in agent_app.py), same discipline as
    # every other optional wire field above. Empty by default: a spec with no
    # labels (or dispatched by a caller that never populated
    # `Proposal.issue_labels`) behaves exactly as before this field existed.
    issue_labels: list[str] = field(default_factory=list)
    # #2638: per-assignment override of the wall-clock runtime ceiling (see
    # `_DEFAULT_RUNTIME_CEILING_S`). `None` (the default) means "use this
    # agent's own configured default" — unlike `cost_ceiling_usd`, that
    # default is generous-but-ON by default (mirrors the TTFT watchdog's
    # always-on `first_output_timeout`, not the opt-in spend ceiling), so an
    # agent predating this field is not silently uncapped; only an explicit
    # non-positive value here disables the ceiling for THIS leg.  Carried on
    # the wire the same way `cost_ceiling_usd` is, for the same config-free-
    # agent reason (docs/EPHEMERAL_WORKERS.md).
    runtime_ceiling_s: float | None = None


class _GitError(RuntimeError):
    pass


# #1797: substrings git/GitHub emit for a credential/auth-shaped push
# rejection (case-insensitive match against the combined stderr of a failed
# `git push`). Deliberately narrow — matched only against the *reap-time*
# safety-net push (see `_reap` below) to decide whether a push failure is
# surfaced as its own FAILED outcome (`push_failure_reason`) or treated as
# before (logged, non-fatal).
#
# The distinction matters because that push is attempted unconditionally,
# including against repos with no `origin` configured at all — every repo
# fixture in this test suite, and any genuinely local-only/airgapped
# deployment (see `_commits_ahead`'s docstring). That failure mode ("fatal:
# 'origin' does not appear to be a git repository") is expected and must
# stay non-fatal. An auth failure against a real, configured remote — e.g.
# the credential-helper quoting bug that motivated this constant, where
# cloud-init baked an empty `$GH_TOKEN` into `~/.gitconfig` at image-bake
# time instead of leaving it to expand at push time — is not: it means a
# worker's real commits may be stuck in a worktree that never reached
# origin, and silently landing on DONE (or the unrelated "0 commits"
# ADVISORY) would hide exactly that.
_AUTH_PUSH_FAILURE_MARKERS = (
    "authentication failed",
    "invalid username or token",
    "password authentication is not supported",
    "could not read username",
    "could not read password",
    "permission denied (publickey)",
    "terminal prompts disabled",
)


def _is_auth_push_failure(message: str) -> bool:
    """True when *message* (a failed push's error text) looks like a
    credential/auth rejection rather than e.g. "no remote configured" or a
    network blip. See `_AUTH_PUSH_FAILURE_MARKERS` for why this is scoped
    narrowly rather than firing on any push failure."""
    lowered = message.lower()
    return any(marker in lowered for marker in _AUTH_PUSH_FAILURE_MARKERS)


def _git(cwd: Path, *args: str, timeout: float = 15.0) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )
    if result.returncode != 0:
        raise _GitError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _remote_already_has_head(
    wt_path: Path, remote: str, branch: str, *, timeout: float = 30.0
) -> bool:
    """True when *remote*'s copy of *branch* already contains the worktree's
    current ``HEAD`` as an ancestor (#2356).

    Used by `_reap`'s belt-and-suspenders push-failure handling: an
    auth-shaped failure pushing to *remote* does NOT necessarily mean the
    worker's commits never reached it — a worker that hit the same broken
    write-credential wall (e.g. an HTTPS credential helper with nothing to
    expand in a non-interactive shell) may have already worked around it by
    pushing the same commit over a different remote/protocol, most commonly
    an explicit ``git@github.com:...`` SSH URL (the #2269 incident). That
    push landed on the SAME GitHub-hosted branch this function now checks —
    it never touches a second remote name in the worktree's own git config.

    Fetching *branch* is a READ operation (``git fetch`` invokes
    upload-pack, not receive-pack), which commonly still succeeds — via
    anonymous access on a public repo, or a still-valid read-scoped
    credential — even when the WRITE credential that just failed the push
    is broken or missing entirely. So this is a meaningfully independent
    signal from the push that just failed, not a re-run of the same failing
    operation.

    Returns ``False`` — never raises — on any git failure: the branch
    missing on the remote, a network error, or `merge-base` reporting "not
    an ancestor" all resolve to "unknown/genuinely behind", and an unknown
    remote state must never suppress the real #1797 failure signal (nothing
    ever reached the remote).
    """
    try:
        _git(wt_path, "fetch", remote, branch, timeout=timeout)
        _git(wt_path, "merge-base", "--is-ancestor", "HEAD", "FETCH_HEAD")
        return True
    except (_GitError, subprocess.TimeoutExpired):
        return False


def _is_linked_worktree(repo_path: Path) -> bool:
    """True when *repo_path* is a linked ``git worktree`` rather than the
    checkout that owns its ``.git`` directory (#1729, H-6's guard 2).

    Ports ``.githooks/_lib.sh``'s ``gfy_is_linked_worktree`` predicate to
    Python instead of re-deriving it: a linked worktree's own ``--git-dir``
    (a ``.git/worktrees/<name>`` subdirectory of the *common* dir) differs
    from ``--git-common-dir``; the base checkout's are the same path. The
    self-heal rebuild must never run here — a linked worktree's
    ``graphify-out/`` entries are symlinks to the shared base graph (see
    ``coord.graph_health``'s module docstring), so a rebuild in the
    worktree would overwrite the base graph from a feature-branch tree,
    and the worktree itself can be reaped mid-rebuild.

    Best-effort like the rest of this module's git helpers: a *repo_path*
    git can't read (already gone, not a repo at all) reports ``False`` so
    the caller's "skip it" branch never fires on a wrong answer either way
    — the caller separately requires the path to exist and be a real
    checkout before it gets here.
    """
    try:
        git_dir = _git(repo_path, "rev-parse", "--git-dir")
        common_dir = _git(repo_path, "rev-parse", "--git-common-dir")
    except (_GitError, FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False

    def _abs(raw: str) -> Path:
        p = Path(raw)
        try:
            return (repo_path / p).resolve() if not p.is_absolute() else p.resolve()
        except OSError:
            return repo_path / p

    return _abs(git_dir) != _abs(common_dir)


def _graphify_update(repo_path: Path, *, timeout: float = 600.0) -> tuple[bool, str]:
    """Run ``graphify update .`` in *repo_path* (#1729, H-6's self-heal).

    Thin delegate to :func:`coord.graph_health.run_graphify_update` (#2237),
    which is the single place that decides what command coord shells out to
    and with which flags — ``coord repo doctor --fix`` repairs checkouts on
    every machine through the same function, and two copies of "the command
    graphify's hooks run, never ``--force``" is exactly the split-brain that
    would let one of them quietly acquire the flag that caused the
    2026-08-02 incident.

    Kept as a module-level name here because it is the seam the self-heal
    tests patch. Returns ``(ok, detail)``.
    """
    from coord.graph_health import run_graphify_update  # noqa: PLC0415

    return run_graphify_update(repo_path, timeout=timeout)


# #2212: count how many Bash commands in a worker's leg invoked `graphify` —
# the measurable half of the graph-first navigation rule in
# WORKER_SYSTEM_PROMPT above. The rule has lapsed into disuse before (see
# #2212's issue body) because nothing tracked whether workers actually
# queried the graph; this is written into every reaped worker's log (see
# the "# reap: done" line in `_reap`) as a plain `grep`-able counter — zero
# across N consecutive legs means the prompt instruction isn't landing.
# Deliberately NOT a DB column: this is a cheap, best-effort signal, not a
# gate anything blocks on, so it stays out of the Assignment schema/migrations.
# #2236: the pattern itself now lives in `coord.worker_events`, which grew a
# need for it when the summary parser started recording each query's OUTCOME
# and not just the count. Imported lazily (like every other
# `coord.worker_events` use in this module) rather than duplicated, so the two
# halves of the measurement can never drift apart on what counts as a query.


def _count_graphify_invocations(bash_commands: list[str]) -> int:
    """Count *bash_commands* entries that invoke the ``graphify`` CLI.

    Matches ``graphify`` as a command token — at the start of the string,
    after a shell separator (``;``, ``&&``, ``||``, ``|``), or after a bare
    newline (multi-line Bash tool calls, e.g. ``cd repo\\ngraphify query
    "..."``) — so ``graphify query "..."`` counts but a path or string that
    merely contains the substring ``graphify`` (e.g. ``cat
    graphify-out/graph.json``, where ``graphify`` is a path component rather
    than a command) does not.

    Note this counts matching *entries* in ``bash_commands``, not individual
    invocations: a single command like ``"graphify query a && graphify query
    b"`` counts once, not twice. Treat the result as an "at least one graph
    query this leg" signal, not an exact invocation tally.
    """
    from coord.worker_events import is_graphify_command  # noqa: PLC0415

    return sum(1 for cmd in bash_commands if is_graphify_command(cmd))


def _worktree_graph_present(
    worktree_path: str | None, repo_path_fallback: str | None = None
) -> bool:
    """True iff the worker's checkout has a **resolvable** ``graphify-out/graph.json``.

    #2236: ``graphify_invocations=0`` is ambiguous on its own. Two of five
    repos ship no graph and no ``.githooks/post-checkout``, so their worktrees
    get an empty ``graphify-out/`` — and the worker prompt's own escape hatch
    ("no graph? skip straight to grep, silently") then fires correctly and
    says nothing. Those workers were *obeying* the rule, not ignoring it, and
    the counter cannot tell them apart from a worker that had a graph and
    never asked. Recording this alongside the count is what disambiguates.

    *repo_path_fallback* mirrors the branch-capture logic a few hundred
    lines up in ``_reap`` ("for legacy assignments (no worktree_path) we
    fall back to the main repo clone"): a legacy/non-worktree assignment has
    no ``worktree_path`` at all, but its worker still ran against
    ``spec.repo_path`` directly, which may have a perfectly good graph.
    Without this fallback, every legacy assignment logs ``graph_present=0``
    regardless — conflating "no worktree" with "no graph".

    ``exists()`` follows symlinks deliberately: a linked worktree's
    ``graph.json`` is a symlink into the base checkout (see
    ``coord/graph_health.py``), and a *dangling* symlink is exactly as
    graph-blind as no file at all — so both must read as absent.
    """
    path = worktree_path or repo_path_fallback
    if not path:
        return False
    try:
        return (Path(path) / "graphify-out" / "graph.json").exists()
    except OSError:
        return False


def _format_graphify_query_lines(queries: Iterable[Any]) -> list[str]:
    """Render per-query ``# graphify_query …`` log lines (#2236).

    ``graphify_invocations=N`` counts attempts but cannot distinguish
    "queried and got a useful answer" from "queried, got nothing, fell back to
    grep" — and those imply opposite fixes (a habit problem the prompt can
    move, vs. a graph coverage/quality problem no amount of prompting helps).
    One line per query, ``grep``-able on ``outcome=``, with the command text
    last because it is the only free-form field.
    """
    lines: list[str] = []
    for q in queries:
        results = getattr(q, "results", None)
        # `worker_events._truncate` already newline-strips and trims this
        # when the `GraphifyQuery` entry is built — no need to redo it here.
        command = str(getattr(q, "command", ""))
        lines.append(
            f"# graphify_query: outcome={getattr(q, 'outcome', 'unknown')} "
            f"results={results if results is not None else '?'} "
            f"cmd={command!r}\n"
        )
    return lines


def _infer_repo_github_slug(repo_path: str) -> str | None:
    """Best-effort ``owner/repo`` slug for *repo_path*'s ``origin`` remote (#1492).

    The agent never learns the GitHub slug configured in the coordinator's
    ``coordinator.yml`` — :class:`AssignmentSpec` (the wire shape a worker is
    dispatched with) carries only ``repo_name``/``repo_path``; the
    coordinator owns config, the agent is a dumb dispatcher. Checking whether
    an ADVISORY assignment's work has gone terminal on GitHub needs the slug
    anyway (:func:`coord.github_ops.work_is_terminal` takes one directly), so
    this derives it from the checkout's own ``origin`` remote URL instead of
    threading a new field through every assignment spec on the wire.

    Handles both the SSH (``git@github.com:owner/repo.git``) and HTTPS
    (``https://github.com/owner/repo.git``) remote forms. Deliberately does
    NOT use a regex with a lazy repo-name match up to an optional ``.git``
    suffix — repo names may themselves contain dots (e.g. ``repo.js``),
    which a naive ``(?:\\.git)?$`` pattern would truncate. Stripping a
    literal trailing ``.git`` instead handles that correctly.

    Returns ``None`` on any failure (no ``origin`` remote, non-GitHub
    remote, missing/deleted worktree, git timeout) — callers must treat that
    as "can't check, leave the entry alone", matching ``work_is_terminal``'s
    own fail-open convention.
    """
    try:
        url = _git(Path(repo_path), "remote", "get-url", "origin").strip()
    except (_GitError, OSError, subprocess.TimeoutExpired):
        return None
    if "github.com" not in url:
        return None
    tail = url.split("github.com", 1)[1].lstrip(":/")
    tail = tail.removesuffix(".git").rstrip("/")
    parts = tail.split("/")
    if len(parts) != 2 or not all(parts):
        return None
    return "/".join(parts)


def _worktree_dirt(wt_path: Path) -> tuple[int, int] | None:
    """Return ``(tracked_changes, untracked_files)`` for *wt_path* (#1394).

    ``tracked_changes`` counts staged/unstaged modifications, additions and
    deletions of files git already knows about; ``untracked_files`` counts new
    files git does not track yet (``??`` entries).  ``git status --porcelain``
    honours ``.gitignore``, so build output and virtualenvs never show up.

    ``--untracked-files=all`` is required, not cosmetic: the default
    (``normal``) collapses an entire untracked directory into a single ``??
    dir/`` line, so a 5000-file ``node_modules`` would count as one file and
    slip under ``_WIP_RESCUE_MAX_FILES``.

    Returns ``None`` when git could not be asked (not a worktree, git missing,
    timeout).  Callers must treat ``None`` as "possibly dirty" and refuse to
    force-delete — guessing "clean" is what destroys work.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=str(wt_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30.0,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    tracked = 0
    untracked = 0
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        if line.startswith("??"):
            untracked += 1
        else:
            tracked += 1
    return tracked, untracked


def _commits_ahead(wt_path: Path, base: str) -> int | None:
    """Number of commits HEAD is ahead of *base* in the worktree.

    Tries ``origin/<base>..HEAD`` first (the authoritative check when a
    remote is configured); falls back to ``<base>..HEAD`` for local-only
    repos (test fixtures and airgapped machines).  Returns ``None`` when
    both lookups fail (e.g. detached HEAD, base branch missing entirely).

    Callers should treat ``None`` as "unknown, assume non-zero" so a git
    failure never triggers a false advisory.

    This is the shared primitive used by both :meth:`AgentServer._commits_ahead`
    and :func:`coord.interactive.finalize_interactive_exit` (#466).
    """
    for ref in (f"origin/{base}", base):
        try:
            raw = _git(wt_path, "rev-list", "--count", f"{ref}..HEAD")
            return int(raw.strip())
        except (_GitError, ValueError):
            continue
    return None


def _pushed_commits_ahead(repo_path: Path, base: str, branch: str) -> int | None:
    """Commits ``origin/<branch>`` is ahead of *base*, checked from *repo_path*.

    #2552: used by ``_reap``'s post-cleanup recheck, run AFTER
    ``_cleanup_worktree`` has possibly already torn the worktree down — so,
    unlike :func:`_commits_ahead`, this never touches the (possibly gone)
    worktree directory. It reads from *repo_path* instead, which stays valid
    for the whole reap: branch refs (including ``refs/remotes/origin/*``)
    live in the shared ``.git`` directory, not per-worktree.

    Deliberately compares against the REMOTE-tracking ref ``origin/<branch>``
    rather than the local branch ref. A commit that only exists locally on
    this one agent (the push in ``_rescue_uncommitted_work`` can fail, same
    as any push) isn't yet something Test/Review/Merge — which all operate
    against GitHub — can act on, so it must not flip a status that implies
    "ready for the pipeline". Returns ``None`` when the remote ref can't be
    resolved at all (no ``origin`` configured, or the push never landed) —
    callers should treat that exactly like :func:`_commits_ahead`'s ``None``:
    unknown, don't act on it.
    """
    for base_ref in (f"origin/{base}", base):
        try:
            raw = _git(
                repo_path, "rev-list", "--count", f"{base_ref}..origin/{branch}"
            )
            return int(raw.strip())
        except (_GitError, ValueError):
            continue
    return None


_ISSUE_REF_RE = re.compile(r"#(\d+)")


def _foreign_issue_refs(
    subject: str,
    issue_number: int,
    extra_allowed: frozenset[int] = frozenset(),
) -> frozenset[int]:
    """Return the set of *foreign* issue numbers referenced in *subject* —
    those that are neither *issue_number* nor in *extra_allowed*.  Empty if
    the subject doesn't reference any foreign issue.

    *extra_allowed* (#2545): for a ``type="test-author"``/``"mock-author"``
    merge entry, *issue_number* is the milestone's TRACKING issue (every JIT
    slice for one milestone shares a single branch/PR — see
    ``Assignment.for_issue_number``'s docstring), but the slice's own commit
    correctly and necessarily cites its OWN child issue
    (``test(ms-4): ... slice #132``). Without this, every such commit was
    flagged FOREIGN on every single verify, regardless of whether the rebase
    was actually clean. Callers resolve the slice's own issue number (e.g.
    via ``coord.models.effective_issue_number``) and pass it here so a
    reference to *either* the tracking issue or the resolved slice issue
    counts as home, not foreign. Defaults to empty, preserving the original
    all-blocking-except-``issue_number`` behaviour for ordinary ``work``
    assignments.
    """
    refs = {int(m) for m in _ISSUE_REF_RE.findall(subject)}
    if not refs:
        return frozenset()
    home = {issue_number} | extra_allowed
    if refs & home:
        return frozenset()
    return frozenset(refs - home)


def _resolve_base_ref(wt_path: Path, base: str) -> str | None:
    """First of ``origin/<base>`` / ``<base>`` that resolves, else ``None``.

    Mirrors the :func:`_commits_ahead` fallback so the merge verification works
    against both real remotes and the local-only git fixtures the test-suite
    builds.  Resolving the ref ONCE (rather than per-query) keeps every count in
    :func:`verify_merge_branch` consistent with the same base.
    """
    for ref in (f"origin/{base}", base):
        try:
            _git(wt_path, "rev-parse", "--verify", "--quiet", ref)
            return ref
        except _GitError:
            continue
    return None


@dataclass
class MergeVerify:
    """Result of verifying a merge-prep branch against its target (#604).

    Attributes:
        default_ahead: Commits present in ``<base>`` but **missing** from
            ``HEAD`` (``git rev-list --count HEAD..<base>``).  MUST be 0 — the
            branch has to contain current ``<base>`` (the rebase actually
            happened and is up to date).  ``None`` when git couldn't determine
            it (base ref missing, detached HEAD) → treated as NOT ``ok``: an
            unverifiable merge branch must never pass as clean.
        added: ``(sha, subject)`` the branch contributes over ``<base>``
            (``<base>..HEAD``), newest first.  Captured for forensics — the
            worktree + reflog are removed on session exit, so this is the only
            post-hoc record of exactly what would merge.
        foreign: The subset of *added* whose subject references a *different*
            ``#NNN`` than the issue being merged (see :func:`_foreign_issue_refs`)
            **and** whose referenced issues are not known-closed — the
            heuristic for the #494 pollution.
            A non-empty ``foreign`` makes :attr:`ok` ``False`` (blocking).
        advisory_foreign: Like ``foreign`` but downgraded to advisory (#1279):
            commits whose subject references a foreign ``#NNN`` that is
            confirmed-closed (passed via ``closed_issue_numbers`` to
            :func:`verify_merge_branch`).  A worker typo-ing a *closed* issue
            in the commit subject cannot be live rebase-pollution — these are
            recorded for the operator but do **not** block the merge.
    """

    default_ahead: int | None
    added: list[tuple[str, str]]
    foreign: list[tuple[str, str]]
    advisory_foreign: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Clean to record ``done``: base fully contained AND no *blocking*
        foreign commits.  Advisory-only foreign commits do not block."""
        return self.default_ahead == 0 and not self.foreign

    def block_summary(self, base: str) -> str:
        """Operator-facing reason this branch is NOT merge-ready (for the
        ``blocked`` summary posted on the issue).  Only called when not ok."""
        parts: list[str] = []
        if self.default_ahead is None:
            parts.append(
                f"could not confirm the branch contains current `{base}` "
                "(git check failed)"
            )
        elif self.default_ahead != 0:
            parts.append(
                f"branch is missing {self.default_ahead} commit(s) from `{base}` "
                "— the rebase did not bring it up to date (run `git fetch` and "
                f"rebase onto `origin/{base}`)"
            )
        if self.foreign:
            flist = "; ".join(f"{sha[:9]} {subj}" for sha, subj in self.foreign)
            parts.append(
                f"{len(self.foreign)} foreign commit(s) that do not belong to "
                f"this issue: {flist}"
            )
        reason = "; ".join(parts) or "merge verification failed"
        return f"Merge-prep blocked (#604): {reason}."

    def advisory_note(self) -> str | None:
        """Return a human-readable note about advisory (non-blocking) foreign
        commits, or ``None`` if there are none.  Surfaced by
        ``coord verify-merge`` alongside the ✓ line when ``ok`` is ``True``."""
        if not self.advisory_foreign:
            return None
        flist = "; ".join(f"{sha[:9]} {subj}" for sha, subj in self.advisory_foreign)
        return (
            f"advisory: {len(self.advisory_foreign)} commit(s) reference "
            f"other (closed) issues — confirmed safe, not blocking: {flist}"
        )


def verify_merge_branch(
    wt_path: Path,
    *,
    base: str,
    issue_number: int,
    extra_allowed_issue_numbers: frozenset[int] = frozenset(),
    closed_issue_numbers: frozenset[int] = frozenset(),
) -> MergeVerify:
    """Verify a ``--merge-of`` branch before its terminal ``done`` is recorded.

    A merge-prep agent's job is to rebase the approved work branch onto the
    current default branch and force-push it, ready to merge.  It can get this
    wrong — rebase onto a stale base, or force-push a polluted history that
    drags in unrelated, already-merged commits — and still self-report ``done``
    (vimcode #494, 2026-06-15).  This pure-git check is the floor that catches
    that: see :class:`MergeVerify` for the fields.

    Pure ``git rev-list`` / ``rev-parse`` plumbing — no network, no subprocess
    ``claude``, no PTY — so the gate runs as a fast local-only test fixture.

    Args:
        extra_allowed_issue_numbers: Additional issue numbers, besides
            *issue_number*, that a commit subject may legitimately reference
            without being flagged foreign (#2545) — see
            :func:`_foreign_issue_refs`'s ``extra_allowed`` for why a
            ``test-author``/``mock-author`` merge entry needs this (its own
            slice issue, not just the milestone's tracking issue).
        closed_issue_numbers: Issue numbers that are known to be closed/merged
            in the upstream repo.  When a commit's subject references *only*
            issues in this set (and not ``issue_number`` or
            ``extra_allowed_issue_numbers``), it is downgraded from a
            **blocking** foreign finding to an **advisory** one (#1279).
            The rationale: a commit referencing a *closed* issue cannot be
            live rebase-pollution — it is almost certainly a worker typo in the
            commit subject.  Pass the empty frozenset (default) to preserve the
            original all-blocking behaviour when the caller has no closed-issue
            data available.
    """
    ref = _resolve_base_ref(wt_path, base)
    if ref is None:
        # Base ref missing entirely — can't verify anything.  default_ahead=None
        # makes this NOT ok (conservative): we won't pass an unverifiable branch.
        return MergeVerify(default_ahead=None, added=[], foreign=[])

    default_ahead: int | None
    try:
        raw = _git(wt_path, "rev-list", "--count", f"HEAD..{ref}")
        default_ahead = int(raw.strip())
    except (_GitError, ValueError):
        default_ahead = None

    added: list[tuple[str, str]] = []
    try:
        out = _git(wt_path, "log", "--format=%H%x09%s", f"{ref}..HEAD")
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            sha, _, subject = line.partition("\t")
            added.append((sha, subject))
    except _GitError:
        added = []

    foreign: list[tuple[str, str]] = []
    advisory_foreign: list[tuple[str, str]] = []
    for sha, subj in added:
        foreign_refs = _foreign_issue_refs(
            subj, issue_number, extra_allowed=extra_allowed_issue_numbers
        )
        if not foreign_refs:
            continue  # not flagged — own work or bare message
        # #1279: downgrade to advisory when ALL referenced foreign issues are
        # known-closed.  A commit that only references closed issues cannot be
        # live rebase-pollution dragged in from an open branch.
        if closed_issue_numbers and foreign_refs.issubset(closed_issue_numbers):
            advisory_foreign.append((sha, subj))
        else:
            foreign.append((sha, subj))
    return MergeVerify(
        default_ahead=default_ahead,
        added=added,
        foreign=foreign,
        advisory_foreign=advisory_foreign,
    )


def resolve_closed_issue_numbers(
    repo_github: str | None,
    foreign_commits: Iterable[tuple[str, str]],
    issue_number: int,
) -> frozenset[int]:
    """Resolve the ``closed_issue_numbers`` corroboration set (#1279) for
    :func:`verify_merge_branch` / the remote analogue, given the commits a
    first verify pass already flagged as (blocking) ``foreign``.

    Callers run a cheap pure-git verify pass first; only when it comes back
    with a non-empty ``foreign`` list is it worth spending a ``gh`` round-trip
    per referenced issue to see whether any of them are closed (and therefore
    downgradeable to advisory).  This keeps the common case — zero foreign
    commits — free of any GitHub call.

    Uses :func:`coord.github_ops.issue_is_closed`, which is best-effort and
    **fail-open** (returns ``False`` on any ``gh`` error) — a transient
    GitHub/CLI failure never silently downgrades a genuinely-foreign commit;
    it just stays blocking, same as before #1279.

    Returns an empty frozenset immediately when *repo_github* is falsy or
    *foreign_commits* is empty, so callers can skip the second verify pass
    entirely (``if closed: ...``) without an extra branch.

    Deliberately does **not** accept an ``extra_allowed_issue_numbers``
    parameter to thread through to :func:`_foreign_issue_refs` (#2545
    review): *foreign_commits* here is always the ``.foreign`` list a prior,
    ``extra_allowed``-aware :func:`verify_merge_branch` pass already
    produced, and ``_foreign_issue_refs`` treats any overlap with the "home"
    set (``{issue_number} | extra_allowed``) as voiding a commit's foreign
    status entirely — so no subject surviving into *foreign_commits* can
    reference an ``extra_allowed`` issue in the first place. If a future
    caller ever feeds this something other than a prior pass's ``.foreign``,
    re-check this assumption before relying on it.
    """
    foreign_commits = list(foreign_commits)
    if not repo_github or not foreign_commits:
        return frozenset()

    from coord import github_ops  # noqa: PLC0415

    candidates: set[int] = set()
    for _sha, subj in foreign_commits:
        candidates |= _foreign_issue_refs(subj, issue_number)
    closed = {n for n in candidates if github_ops.issue_is_closed(repo_github, n)}
    return frozenset(closed)


def _safe_realpath(path: str) -> str:
    try:
        return os.path.realpath(path)
    except OSError:
        return path


# #2569: the fleet's ONE pinned, non-editable venv (docs/AGENT_OPERATIONS.md).
# ``~/.coord-venv`` is a symlink an operator atomically repoints at a
# ``.blue``/``.green`` real directory on release — see `_pinned_venv_bin_dirs`
# for why realpath (not the literal name) is what actually identifies it.
_PINNED_VENV_DIRNAME = ".coord-venv"


def _pinned_venv_bin_dirs(env: dict[str, str]) -> set[str]:
    """Realpath(s) of the fleet's pinned venv ``bin`` dir that must never
    appear on a worker's PATH (#2569: a worker's bare ``pip install -e .``
    landed in the LIVE ``~/.coord-venv``, crash-looping the whole fleet for
    ~11h — see the incident writeup on #2569).

    Resolved from *env*'s own ``HOME`` — the environment actually being
    built for the worker — not this process's, since the two can diverge
    once a provider's ``env:`` overrides are merged in.  Falls back to this
    process's home when *env* carries no ``HOME`` (matches
    ``os.path.expanduser``'s own fallback).

    ``~/.coord-venv`` is a symlink an operator atomically repoints at a
    ``.blue``/``.green`` real directory on every release (deploy runbook,
    docs/AGENT_OPERATIONS.md).  Realpath is what makes this match regardless
    of which side is currently live, and regardless of whether ``PATH``
    carries the symlink name or an already-resolved path.
    """
    home = env.get("HOME") or os.path.expanduser("~")
    return {_safe_realpath(os.path.join(home, _PINNED_VENV_DIRNAME, "bin"))}


def _strip_venv_bins_from_path(env: dict[str, str], venv_bins: set[str]) -> None:
    """Remove any ``PATH`` entry whose realpath is in *venv_bins*, in place.

    #2569: called both inside :func:`_worker_subprocess_env` and again at
    each spawn call site AFTER every later ``env.update(...)`` (cargo-cache
    overlay, ``provider.env()``) — a provider's own ``env:`` override
    (operator-authored in ``coordinator.yml``) could otherwise reintroduce a
    pinned venv bin dir onto ``PATH`` and silently undo the strip performed
    here. No-ops when *venv_bins* is empty or ``PATH`` is unset.
    """
    path = env.get("PATH", "")
    if not path or not venv_bins:
        return
    kept = [
        part
        for part in path.split(os.pathsep)
        if part and _safe_realpath(part) not in venv_bins
    ]
    env["PATH"] = os.pathsep.join(kept)


def _worker_subprocess_env(
    base_env: dict[str, str] | None = None,
    *,
    prefix: str | None = None,
    base_prefix: str | None = None,
    cwd: "str | Path | None" = None,
    assignment_id: str | None = None,
) -> dict[str, str]:
    """Environment for worker `claude -p` subprocesses, with the agent's own
    venv removed (#402).

    The agent runs from a venv whose ``bin`` is first on PATH (systemd unit),
    and workers are spawned with ``cwd`` inside an ephemeral
    ``~/.coord/worktrees/<id>`` checkout. Without sanitizing the environment, a
    worker that runs ``pip install -e .`` (e.g. following the repo's CLAUDE.md
    dev step) resolves ``pip`` to the *agent's* venv and pins its editable
    finder to the worktree. When the worktree is reaped the agent crash-loops
    with ``ModuleNotFoundError: No module named 'coord'``.

    Dropping the agent's venv ``bin`` from PATH (and clearing ``VIRTUAL_ENV`` /
    ``PYTHONHOME``) forces a worker's ``pip``/``python`` to its own venv instead
    of the agent's. The ``prefix != base_prefix`` check strips whenever THIS
    process detects itself as running inside a venv, so a system-Python agent
    never loses ``/usr/bin`` & co.

    #2569: that detection is a heuristic about *this* process, and it can be
    wrong, stale, or simply not fire for the process that ends up building a
    given worker's env — exactly what happened in the incident that gave rise
    to this note (an 11h fleet outage after a drive-launched worker's bare
    ``pip install -e .`` landed in the live ``~/.coord-venv``). So on top of
    the heuristic, this function ALSO strips the fleet's pinned venv by its
    well-known name (``~/.coord-venv``, resolved via realpath so the
    blue/green symlink swap doesn't matter — see `_pinned_venv_bin_dirs`).
    That second strip does not depend on this process's own venv detection at
    all, so it holds even when the heuristic above doesn't fire. It also sets
    ``PIP_REQUIRE_VIRTUALENV=true`` as an independent, second-layer guard: a
    worker that never creates its own venv (skips CLAUDE.md's Development
    recipe) gets a hard ``pip`` refusal instead of a silent install into
    whatever unstripped ``python``/``pip`` its PATH happens to resolve to.

    #1783: ``base_env`` is a straight copy of the agent daemon's environment,
    and the daemon's own ``PWD`` passes through untouched by default.
    ``Popen(..., cwd=...)`` sets the worker's *real* cwd correctly regardless,
    but some providers (e.g. opencode) resolve their working directory from
    the inherited ``PWD`` env var rather than ``getcwd()`` — so a stale
    ``PWD`` can point such a provider at the wrong checkout even though the
    OS-level process is confined to the worktree. When *cwd* is given, we set
    ``PWD`` to match it explicitly, so worktree confinement for those
    providers no longer depends on the daemon's ambient ``PWD`` (or on
    ``bash_wrap_spawn`` recomputing it as a side effect of routing through a
    bash parent — see ``_maybe_bash_wrap``). Callers that merge
    ``provider.env()`` on top of this result may still override ``PWD``
    deliberately; that ordering is preserved since we only set it here, not
    after.

    #2217: ``assignment_id``, when given, is written to
    ``COORD_ASSIGNMENT_ID``. The headless worker prompt (built in
    ``review.py`` and elsewhere) tells every worker that if this variable is
    set it should call ``coord report-result --assignment
    "$COORD_ASSIGNMENT_ID" ...`` directly as the *authoritative* verdict
    path, with the transcript-parsed ``END_REVIEW`` block as a fallback only.
    Before this, nothing in the headless ``claude -p`` dispatch lane (this
    function is the only place that lane's subprocess environment is built)
    ever set the variable, so that "primary" path was dead on arrival for
    every headless review — the fragile transcript parse was silently the
    *only* path, not a backup. Both spawn call sites below pass
    ``assignment_id=assignment.id`` so it's set for every worker type (review,
    smoke, work), matching what ``coord/commands/review.py`` already reads as
    its ``--assignment`` default.
    """
    env = dict(os.environ if base_env is None else base_env)
    pfx = sys.prefix if prefix is None else prefix
    base_pfx = sys.base_prefix if base_prefix is None else base_prefix

    # #2569: name-based strip of the fleet's pinned venv — unconditional,
    # independent of the prefix heuristic below. See _pinned_venv_bin_dirs.
    venv_bins = _pinned_venv_bin_dirs(env)
    if pfx and base_pfx and _safe_realpath(pfx) != _safe_realpath(base_pfx):
        venv_bins.add(_safe_realpath(os.path.join(pfx, "bin")))
    _strip_venv_bins_from_path(env, venv_bins)

    # #2569: second, independent layer — a worker that skips CLAUDE.md's
    # venv-creation step gets a hard `pip` refusal instead of a silent
    # install into whatever unstripped python/pip its PATH resolves to. A
    # worker that DOES create+activate its own `.venv` first satisfies this
    # normally (activation sets VIRTUAL_ENV in that shell).
    env["PIP_REQUIRE_VIRTUALENV"] = "true"

    env.pop("VIRTUAL_ENV", None)
    env.pop("PYTHONHOME", None)

    if cwd is not None:
        env["PWD"] = str(cwd)

    if assignment_id is not None:
        env["COORD_ASSIGNMENT_ID"] = assignment_id

    return env


def worker_coord_reachable(base_env: dict[str, str] | None = None) -> tuple[bool, str]:
    """#2936: does ``coord`` resolve on a WORKER's PATH — not this agent
    process's own?

    Built on the exact env a real worker is spawned into
    (:func:`_worker_subprocess_env`, whose #402/#2569 strip removes the
    fleet's pinned ``~/.coord-venv/bin`` from PATH). A worker that cannot
    resolve ``coord`` can run its entire suite, PASS, and be structurally
    unable to record that verdict (``coord test <id> --passed``) — the
    missing verdict then reads as a TEST FAILURE and walks the
    ``models.escalation`` ladder, re-dispatching already-correct work on a
    more expensive model for a PATH gap, not a weak one (#2897 cost one
    instance of this an extra opus rerun on top of an already-passing
    sonnet leg).

    Before this, the gap was discovered only when a smoke worker's own
    final-turn transcript happened to mention it (as #2897's did) — this
    function exists so the agent can say so ITSELF, at startup, in
    ``journalctl --user -u coord-agent``, mirroring #1671's PATH diagnostics
    for capability probes.

    Returns ``(ok, message)`` rather than logging directly, so the check is
    testable with a synthetic *base_env* with no mocking of ``shutil.which``
    or the logging module required. ``base_env=None`` (the production
    default) resolves against this process's real ``os.environ``, exactly
    like :func:`_worker_subprocess_env` itself.
    """
    worker_path = _worker_subprocess_env(base_env).get("PATH", "")
    resolved = shutil.which("coord", path=worker_path)
    if resolved:
        return True, (
            f"coord agent: worker PATH check OK — 'coord' resolves at {resolved} "
            "(a smoke/test worker on this machine can record its verdict, #2936)"
        )
    return False, (
        "coord agent: WARNING 'coord' does NOT resolve on a WORKER's PATH — "
        f"searched: {worker_path!r}. A smoke/test worker on this machine can "
        "run its whole suite, PASS, and be structurally unable to record the "
        "verdict via `coord test <id> --passed`; the missing verdict then "
        "reads as a TEST FAILURE and escalates the model for an "
        "infrastructure gap, not a weak one (#2936). Fix: re-run "
        "install-agent.sh on this machine so ~/.local/bin/coord exists and "
        "symlinks to $HOME/.coord-venv/bin/coord (the blue/green symlink "
        "itself, never a resolved .blue/.green path)."
    )


def _slugify(text: str, max_len: int = 40) -> str:
    """Convert *text* to a URL/branch-safe slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-")


def _sanitize_branch(branch: str) -> str:
    """Sanitize a git branch name for use as a filesystem / URL path component.

    Replaces any character that isn't alphanumeric, ``-``, ``_``, or ``.``
    with a dash.  This converts slashes (``feature/my-thing`` →
    ``feature-my-thing``) and any other URL-unsafe characters.  The result
    is safe to use as a single path segment (no embedded ``/``).
    """
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", branch).strip("-")


# Regex that matches git's "branch/path already used by worktree at '<path>'"
# error, which fires when the requested branch is already checked out in another
# worktree.  The captured group is the conflicting worktree's path.
_WT_COLLISION_RE = re.compile(r"already used by worktree at '([^']+)'")

# Build-intermediate file suffixes that are never useful stash artifacts.
# Cargo produces dozens of *.rcgu.o incremental-codegen objects per build
# (e.g. `tui_selection-<hash>.<unit>.rcgu.o`) — Path.suffix on these returns
# ".o", which is caught by the ".o" entry.  ".rcgu" catches any bare codegen
# unit files.  All of these would inflate the stash with hundreds of MB of
# throwaway data.  ".d" was already skipped by the original loop; it is
# included here so the full skip-set lives in one place.
_STASH_SKIP_SUFFIXES: frozenset[str] = frozenset(
    {".d", ".o", ".rlib", ".rmeta", ".rcgu"}
)

# Matches Cargo's hash-stamped duplicate binaries: `<name>-<16 lowercase hex>`.
# Cargo emits both the canonical `tui_app` AND `tui_app-abcdef0123456789`
# (build-id stamped).  A glob like `tui_*` matches both, so every binary would
# be stashed twice.  When the canonical sibling is present we skip the
# hash-suffixed copy; when ONLY the hash-suffixed form exists we keep it.
_BUILD_HASH_SUFFIX_RE = re.compile(r"^(.+)-[0-9a-f]{16}$")

# Warn (but do not block) when a single branch's stash exceeds this size —
# a signal that artifact_paths is too broad for the repo (#940: 72
# unstripped debug example binaries at ~103MB each ballooned a single
# quadraui stash to 7.2GB, even though the #436 junk filter was working
# correctly — the glob itself was just too wide).
_STASH_WARN_BYTES = 1 * 1024**3  # 1 GB


def _strip_debug_symbols(path: Path) -> bool:
    """Best-effort strip of debug symbols from a stashed binary (#940).

    Runs ``strip -S`` (strip the debug-symbol table only) on *path* in
    place. ``-S`` is supported by both GNU binutils and macOS/LLVM strip
    — a bare ``strip`` with no flags behaves differently across those two,
    so ``-S`` is the portable choice. A Rust debug binary commonly shrinks
    5-10x with no functional change: only DWARF debug info is removed, not
    the binary's code or its dynamic symbol table, so a stripped example
    still runs identically for smoke testing.

    Silently no-ops if the ``strip`` binary isn't on ``PATH``, or if the
    file isn't something ``strip`` understands (a script, a non-binary
    asset an artifact_paths glob happened to match, an already-stripped
    binary) — a failed strip just leaves the original copy intact rather
    than losing the artifact.

    Returns True if strip ran and exited successfully, False otherwise
    (caller does not need to branch on this today, but it keeps the
    function testable in isolation).
    """
    strip_bin = shutil.which("strip")
    if strip_bin is None:
        return False
    try:
        result = subprocess.run(
            [strip_bin, "-S", str(path)],
            capture_output=True,
            timeout=30,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def narrow_artifact_paths(
    artifact_paths: list[str],
    smoke_tests: list[str] | None,
    worktree: Path | None = None,
) -> list[str]:
    """Narrow glob-containing *artifact_paths* to the examples named in *smoke_tests*.

    For each glob-containing entry in *artifact_paths* (e.g.
    ``target/debug/examples/tui_*``), extracts candidate binary names from the
    *smoke_tests* bullets that match the filename glob and replaces the glob
    with those specific paths (e.g. ``target/debug/examples/tui_submenu``).
    Globs with no matching candidate are left unchanged.

    Falls back to the original list (unchanged) when:

    * *smoke_tests* is ``None`` or empty — no smoke tests emitted or change
      is internal (``SMOKE_TESTS: (none — change is internal)``).
    * No candidate name extracted from *smoke_tests* matches any glob in the
      list — nothing is named, so narrowing would be wrong.

    When *worktree* is provided, each text-matched candidate path is verified
    to exist on disk inside *worktree* before narrowing is accepted.  If none
    of the matched names are present on disk for a given glob, the original
    broad glob is kept unchanged for that entry — this prevents the stash from
    being pinned to non-existent paths when the SMOKE_TESTS block names
    binaries that the build hasn't produced yet (or under a different name).
    When *worktree* is ``None``, the text-only matching behaviour is preserved
    for backward compatibility (interactive/remote backstop callers that do
    not have access to the worktree path).

    Token extraction: every word-boundary-delimited token starting with a
    letter (``[a-zA-Z][a-zA-Z0-9_-]*[a-zA-Z0-9]``) is a candidate.  Only
    tokens that *match* a filename glob (via :func:`fnmatch.fnmatch`) are
    used, so common prose words like "run", "it", "see" are silently
    discarded when they don't fit the glob pattern.

    #982: called centrally from :meth:`AgentServer._stash_artifacts` for
    every headless ``coord assign`` Work dispatch — the dominant case, since
    the worker's own ``SMOKE_TESTS:`` block is only available in its log
    *after* the session ends, right when ``_stash_artifacts`` runs.  Also
    called at four interactive-dispatch backstop sites in
    :mod:`coord.commands.dispatch_workers` (``--fix-of`` and ``--rework-of``,
    local + remote), which narrow using the *original* work assignment's
    already-captured ``smoke_tests``.  A fresh ``--interactive`` work
    session has no such backstop — no smoke tests exist before that session
    runs, and its output isn't captured to a log this code can parse — so
    it always stashes the full glob and relies on this function's
    centralized call for any later narrowing.  Either way the effect is the
    same: the stash captures 1-2 relevant example binaries instead of every
    file matching a broad glob (e.g. ``tui_*`` matching ~72 Cargo debug
    examples).

    #1248: the *worktree* parameter guards against pinning the stash to paths
    that exist only in SMOKE_TESTS prose but not yet on disk, which caused
    ``stash_artifacts_for_branch`` to copy 0 files silently.
    """
    if not smoke_tests or not artifact_paths:
        return list(artifact_paths)

    # Short-circuit when no entry in the list is a glob.
    if not any("*" in p or "?" in p or "[" in p for p in artifact_paths):
        return list(artifact_paths)

    # Extract candidate binary names from smoke-test bullets.  We pull every
    # contiguous word-token (letters, digits, underscores, hyphens) that
    # starts and ends with a letter-or-digit.  These are then matched against
    # the filename part of each glob using fnmatch, so only tokens that *fit*
    # the pattern contribute ("tui_submenu" matches "tui_*"; "run", "it",
    # "see", "above" do not).
    _token_re = re.compile(r"\b([a-zA-Z][a-zA-Z0-9_-]*[a-zA-Z0-9])\b")
    candidate_names: set[str] = set()
    for bullet in smoke_tests:
        for m in _token_re.finditer(bullet):
            candidate_names.add(m.group(1))

    if not candidate_names:
        return list(artifact_paths)

    narrowed: list[str] = []
    any_narrowed = False

    for path_glob in artifact_paths:
        if "*" not in path_glob and "?" not in path_glob and "[" not in path_glob:
            # Literal path — keep unchanged regardless of smoke tests.
            narrowed.append(path_glob)
            continue

        # Separate the directory prefix from the filename glob.
        slash = path_glob.rfind("/")
        if slash >= 0:
            dir_part = path_glob[:slash]
            name_glob = path_glob[slash + 1 :]
        else:
            dir_part = ""
            name_glob = path_glob

        matches = sorted(
            name for name in candidate_names if fnmatch.fnmatch(name, name_glob)
        )

        if matches:
            if worktree is not None:
                # #1248: disk-verify each text-matched name.  Only accept the
                # narrowed set when at least one file actually exists on disk;
                # if none do, fall back to the broad glob so the stash doesn't
                # end up empty because of a SMOKE_TESTS name that hasn't been
                # built yet (wrong spelling, renamed binary, etc.).
                on_disk = [
                    name
                    for name in matches
                    if (worktree / (f"{dir_part}/{name}" if dir_part else name)).exists()
                ]
                if on_disk:
                    any_narrowed = True
                    for m in on_disk:
                        narrowed.append(f"{dir_part}/{m}" if dir_part else m)
                else:
                    # No text-matched name exists on disk → keep original glob.
                    narrowed.append(path_glob)
            else:
                # No worktree provided — text-only matching (backward compat).
                any_narrowed = True
                for m in matches:
                    narrowed.append(f"{dir_part}/{m}" if dir_part else m)
        else:
            # No candidate matched this glob — leave it unchanged so the
            # fallback stashes all files for unscoped patterns.
            narrowed.append(path_glob)

    return narrowed if any_narrowed else list(artifact_paths)


def cargo_relative_pattern(pattern: str) -> str | None:
    """Rewrite an ``artifact_paths`` glob so it resolves inside the shared
    cargo target dir (#1402).

    ``artifact_paths`` are worktree-relative and, for Rust repos, point through
    cargo's default in-tree target dir — e.g.
    ``tui/target/debug/coord-tui``.  Once a worker builds with
    ``CARGO_TARGET_DIR=~/.coord/cargo-target/<repo>``, that binary lands at
    ``<cache>/debug/coord-tui`` and the in-worktree glob matches nothing —
    which is exactly the silent stash-miss that downgraded good work in #1357.

    Returns the portion of *pattern* after its last ``target`` path component
    (``"debug/coord-tui"`` for the example above), or ``None`` when the
    pattern has no ``target`` component (nothing to rewrite) or nothing
    follows it.
    """
    parts = Path(pattern).parts
    if ".." in parts:
        return None
    try:
        idx = len(parts) - 1 - parts[::-1].index("target")
    except ValueError:
        return None
    rest = parts[idx + 1 :]
    if not rest:
        return None
    return str(Path(*rest))


def stash_artifacts_for_branch(
    worktree_path: Path,
    branch: str,
    repo_name: str,
    patterns: list[str],
    state_dir: Path,
    assignment_id: str | None = None,
    log_path: str | None = None,
    unmatched_out: "list[str] | None" = None,
) -> int:
    """Copy build artifacts from *worktree_path* into the persistent stash.

    Standalone helper shared by :meth:`AgentServer._stash_artifacts` (worker
    path) and :func:`coord.interactive.finalize_interactive_exit` (interactive
    path, #562).  Both call this function so the same filter/copy/GC logic
    applies regardless of how the session was launched.

    Files matching *patterns* (glob strings relative to *worktree_path*) are
    copied to ``<state_dir>/artifacts/<repo_name>/<sanitized_branch>/``.
    Build-intermediate suffixes (.d, .o, .rlib, .rmeta, .rcgu) and files
    smaller than 100 bytes are skipped.  Cargo hash-stamped duplicates
    (``<name>-<16 hex>``) are de-duplicated when the canonical sibling also
    matches.  Each copy is passed through :func:`_strip_debug_symbols`
    (#940) to shrink debug binaries before they hit the stash.

    #982: any file already present in the stash directory that does *not*
    match this run's *patterns* is removed (dotfile markers excepted) — a
    re-stash with a narrower pattern set (e.g. after
    :func:`narrow_artifact_paths` scopes down to the example(s) actually
    under test) shrinks an existing stash instead of only ever growing it.

    A ``.assignment_id`` marker is written when *assignment_id* is provided so
    the manifest endpoint can surface which build produced the stash.  When
    the stash directory's total size exceeds ``_STASH_WARN_BYTES`` after
    this run, a warning line is appended to *log_path* (#940) — a signal
    that ``artifact_paths`` for this repo is too broad.

    #1323: when *unmatched_out* is a list, each pattern that resolved to zero
    files on disk is appended to it.  The caller can use this to surface
    per-glob misses even when the overall copy count is positive (i.e. some
    patterns matched but at least one didn't).  When not provided the
    per-pattern tracking is skipped.

    Returns the number of files copied (0 for a no-op).
    """
    if not patterns:
        return 0
    sanitized = _sanitize_branch(branch)
    stash_dir = state_dir / "artifacts" / repo_name / sanitized

    # #1295: do NOT create the stash directory before we know there's
    # something to put in it.  The previous unconditional
    # ``mkdir(parents=True, exist_ok=True)`` up top left an empty stash
    # dir on disk whenever the worktree was missing (or a re-stash of a
    # cleaned-up branch was attempted) — which then fooled
    # ``stash_dir.exists()``-style checks into thinking a stash existed
    # even though ``_stash_has_content`` would report False, and it
    # counted against ``artifact_bytes`` inode overhead for nothing.
    # Bail out before we create anything if the source worktree is gone.
    if not worktree_path.exists():
        return 0

    copied = 0
    # #1402: when the worker built against the shared per-repo cargo cache,
    # ``<worktree>/**/target/`` no longer exists — fall back to the cache for
    # any pattern that misses in the worktree, so a Rust repo's
    # ``artifact_paths`` keeps resolving (and #1357's silent stash-miss does
    # not come back through the front door).
    _cargo_dir = cargo_cache.target_dir_for_repo(repo_name, state_dir)
    if _cargo_dir is not None and not _cargo_dir.is_dir():
        _cargo_dir = None

    # #1323: collect per-pattern misses (patterns whose glob returned 0 files)
    # so we can report them individually even when other patterns matched.
    _unmatched_patterns: list[str] = []
    # Collect every candidate path up-front so we can identify which canonical
    # names are present before deciding whether to skip hash-suffixed duplicates.
    candidates: list[Path] = []
    for pattern in patterns:
        # Reject patterns containing ".." — Path.glob("../foo") succeeds in
        # Python 3.12+ and can escape the worktree.  artifact_paths comes from
        # trusted config, but an explicit check is cheap insurance.
        if ".." in Path(pattern).parts:
            _unmatched_patterns.append(pattern)
            continue
        try:
            matches = list(worktree_path.glob(pattern))
        except (ValueError, OSError):
            _unmatched_patterns.append(pattern)
            continue
        file_matches = [src for src in matches if src.is_file()]
        if not file_matches and _cargo_dir is not None:
            cargo_pattern = cargo_relative_pattern(pattern)
            if cargo_pattern:
                try:
                    file_matches = [
                        src for src in _cargo_dir.glob(cargo_pattern) if src.is_file()
                    ]
                except (ValueError, OSError):
                    file_matches = []
        if not file_matches:
            _unmatched_patterns.append(pattern)
        candidates.extend(file_matches)

    # Build the set of canonical file names: files whose stem does NOT look
    # like `<name>-<16 hex>`.  Used below to skip hash-stamped duplicates.
    canonical_names: set[str] = {
        src.name
        for src in candidates
        if _BUILD_HASH_SUFFIX_RE.match(src.stem) is None
    }

    # Names this run intends to keep in the stash — used below (#982) to
    # prune anything left over from a prior, broader stash of this branch.
    kept_names: set[str] = set()

    # #1295: create the stash directory lazily.  If nothing matches the
    # globs (0 candidates, or every candidate skipped by the intermediate/
    # tiny-file filters below), we never touch the filesystem — no empty
    # stash dir left behind for ``_stash_has_content`` to report as
    # "present but empty" and no phantom entry under
    # ``state_dir/artifacts/<repo>/<branch>/``.
    stash_dir_created = stash_dir.exists()

    for src in candidates:
        # Skip build-intermediate files (.d, .o, .rlib, .rmeta, .rcgu).
        if src.suffix in _STASH_SKIP_SUFFIXES:
            continue
        try:
            st = src.stat()
        except OSError:
            continue
        # Skip tiny files (< 100 bytes — not a real binary).
        if st.st_size < 100:
            continue
        # De-duplicate hash-suffixed binaries: Cargo emits both `tui_app` and
        # `tui_app-abcdef0123456789`.  Skip the hash-stamped copy when the
        # canonical sibling is also present in the match set.  If ONLY the
        # hash-suffixed form exists (no canonical sibling), keep it — never
        # drop the only copy of a binary.
        m = _BUILD_HASH_SUFFIX_RE.match(src.stem)
        if m is not None:
            canonical_name = m.group(1) + src.suffix
            if canonical_name in canonical_names:
                continue
        kept_names.add(src.name)
        if not stash_dir_created:
            try:
                stash_dir.mkdir(parents=True, exist_ok=True)
                stash_dir_created = True
            except OSError:
                continue
        dst = stash_dir / src.name
        try:
            shutil.copy2(src, dst)
            copied += 1
        except (OSError, shutil.Error):
            continue
        _strip_debug_symbols(dst)

    # #982: prune stash files that no longer match the current pattern set.
    # A narrowed re-stash (e.g. a fix-of/rework-of session that only names
    # 1-2 examples in its smoke tests) is otherwise purely additive — it
    # copies the named files on top of whatever an earlier, broader stash
    # (e.g. the first headless Work dispatch, before narrowing) already
    # left behind, so the stash never actually shrinks.  Anything already
    # in the stash directory that isn't among this run's kept names is
    # stale for the *current* pattern set and gets removed.  Marker files
    # (dotfiles, e.g. ``.assignment_id``) are left alone.
    if stash_dir_created:
        try:
            for existing in stash_dir.iterdir():
                if existing.name.startswith("."):
                    continue
                if not existing.is_file():
                    continue
                if existing.name not in kept_names:
                    try:
                        existing.unlink()
                    except OSError:
                        pass
        except OSError:
            pass

        # Touch the stash directory so its mtime reflects this stash run.
        # mkdir(exist_ok=True) is a no-op when the directory already exists,
        # so a re-stash would leave the original Day-1 mtime — causing
        # _gc_artifacts to evict the refreshed stash prematurely.
        try:
            stash_dir.touch()
        except OSError:
            pass

    # Write the assignment_id marker so the manifest endpoint can surface
    # which build produced this stash without iterating all assignments.
    # #1248: skip the marker when nothing was copied — an empty stash must
    # not be recorded as a valid build artifact set.
    if assignment_id is not None and copied > 0:
        try:
            (stash_dir / ".assignment_id").write_text(assignment_id, encoding="utf-8")
        except OSError:
            pass

    # #1295: if the pruning above left the directory with no real content
    # (only dotfiles at most), remove it so ``_stash_has_content`` /
    # manifest checks see a clean absence rather than "present but empty".
    # A prior stash for this branch may have existed and just been fully
    # pruned by this narrower run — dropping the empty shell makes the
    # sweep idempotent.
    if stash_dir_created and copied == 0:
        try:
            leftover = [
                p for p in stash_dir.iterdir()
                if p.is_file() and not p.name.startswith(".")
            ]
        except OSError:
            leftover = []
        if not leftover:
            # rmtree tolerates dotfile-only content; safe even if a stale
            # ``.assignment_id`` marker was left over from a prior run.
            try:
                shutil.rmtree(stash_dir, ignore_errors=True)
            except OSError:
                pass

    # #1323: populate the caller-supplied list with patterns that matched
    # 0 files.  When all patterns missed (copied == 0) the existing warning
    # below is sufficient; when SOME matched but at least one didn't, the
    # caller can surface the specific unmatched glob(s) separately.
    if unmatched_out is not None:
        unmatched_out.extend(_unmatched_patterns)

    # #1295: a 0-copy stash is loud regardless of whether a per-assignment
    # log_path is available.  Previously the warning only reached the
    # assignment log — an hourly sweep that stashed nothing and then
    # removed the worktree left no trace anywhere the operator could see.
    # Now we also emit through Python logging so the daemon/agent log
    # captures it, which is the surface a human actually consults when
    # diagnosing "the worktree is gone and there are no artifacts".
    if copied == 0:
        _log.warning(
            "stash: 0 files matched %r in %s (repo=%s branch=%s aid=%s) — "
            "check artifact_paths config and that the build actually "
            "produced the expected outputs",
            patterns,
            worktree_path,
            repo_name,
            branch,
            assignment_id,
        )
    elif _unmatched_patterns:
        # #1323: partial miss — some globs matched but at least one didn't.
        # Log at WARNING so it's visible in the daemon log even without a
        # log_path.
        _log.warning(
            "stash: %d glob(s) matched 0 files in %s (repo=%s branch=%s "
            "aid=%s): %r — check artifact_paths config and that the build "
            "produced all expected outputs",
            len(_unmatched_patterns),
            worktree_path,
            repo_name,
            branch,
            assignment_id,
            _unmatched_patterns,
        )

    if log_path:
        _append_log_line(
            log_path,
            f"# stash: {copied} artifact(s) → {stash_dir}\n",
        )
        # #1248: a 0-copy stash is suspicious — the patterns resolved to
        # nothing.  Emit a loud WARNING so it's visible in the assignment log
        # (mirror the oversized-stash WARNING that already lives below).
        if copied == 0:
            _append_log_line(
                log_path,
                f"# stash WARNING: 0 files matched {patterns!r} in "
                f"{worktree_path} — check artifact_paths config and that "
                "the build actually produced the expected outputs.\n",
            )
        elif _unmatched_patterns:
            # #1323: partial miss — name the specific unmatched glob(s) so
            # the operator can diagnose without guessing which pattern failed.
            _missed_str = ", ".join(repr(p) for p in _unmatched_patterns)
            _append_log_line(
                log_path,
                f"# stash WARNING: {len(_unmatched_patterns)} glob(s) "
                f"matched 0 files in {worktree_path}: {_missed_str} — "
                "check artifact_paths config and that the build produced "
                "all expected outputs.\n",
            )
        try:
            total_bytes = sum(
                f.stat().st_size
                for f in stash_dir.iterdir()
                if f.is_file() and not f.name.startswith(".")
            )
        except OSError:
            total_bytes = 0
        if total_bytes > _STASH_WARN_BYTES:
            _append_log_line(
                log_path,
                f"# stash WARNING: {stash_dir} is "
                f"{total_bytes / (1024**3):.1f} GB "
                f"(> {_STASH_WARN_BYTES / (1024**3):.0f} GB) — "
                f"consider narrowing artifact_paths for {repo_name!r} to "
                "the example(s) actually under test, or pull selectively "
                "with `coord pull-artifact --only <glob>` (#940).\n",
            )

    return copied


def _run_pre_stash_build(
    build_command: str,
    worktree: Path,
    log_path: str | None,
) -> bool:
    """Run *build_command* in *worktree* before artifact stash (#1323, fix #3).

    Ensures the configured ``build_command`` from ``coordinator.yml`` is
    executed so that build artifacts exist in the worktree regardless of which
    feature flags the worker itself used during development.

    The command is run via ``/bin/sh -c`` in *worktree* with a
    ``PRE_STASH_BUILD_TIMEOUT_SECONDS`` (10-minute) timeout.  stdout and
    stderr are captured and appended to *log_path* (if set) so the operator
    can diagnose build failures.  Returns ``True`` on exit code 0, ``False``
    on any failure.  Never raises — this is best-effort pre-stash
    housekeeping.
    """
    try:
        result = subprocess.run(
            ["/bin/sh", "-c", build_command],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PRE_STASH_BUILD_TIMEOUT_SECONDS,
        )
        ok = result.returncode == 0
        if log_path:
            output = (result.stdout + result.stderr).strip()
            _append_log_line(
                log_path,
                f"# pre-stash build: {build_command!r} "
                f"(exit={result.returncode})"
                + (f"\n{output}" if output else "")
                + "\n",
            )
        if not ok:
            _log.warning(
                "pre-stash build command %r exited %d in %s",
                build_command,
                result.returncode,
                worktree,
            )
        return ok
    except Exception as exc:  # noqa: BLE001
        if log_path:
            _append_log_line(
                log_path,
                f"# pre-stash build: {build_command!r} FAILED ({exc})\n",
            )
        _log.warning(
            "pre-stash build command %r failed in %s: %s",
            build_command,
            worktree,
            exc,
        )
        return False


def _parse_worktree_porcelain(output: str) -> list[dict[str, str]]:
    """Parse ``git worktree list --porcelain`` output into a list of dicts.

    Each dict may have keys: ``worktree`` (absolute path), ``HEAD`` (SHA),
    ``branch`` (short name with ``refs/heads/`` prefix stripped), and
    ``bare`` (literal string ``"true"`` when the entry is a bare worktree).
    Missing fields (e.g. ``branch`` in detached-HEAD state) are simply absent.
    """
    worktrees: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in output.splitlines():
        if not line.strip():
            if current:
                worktrees.append(current)
                current = {}
        elif line.startswith("worktree "):
            current["worktree"] = line[len("worktree "):]
        elif line.startswith("HEAD "):
            current["HEAD"] = line[len("HEAD "):]
        elif line.startswith("branch "):
            raw = line[len("branch "):]
            current["branch"] = (
                raw[len("refs/heads/"):] if raw.startswith("refs/heads/") else raw
            )
        elif line.strip() == "bare":
            current["bare"] = "true"
    if current:
        worktrees.append(current)
    return worktrees


def _is_same_path(candidate: str, resolved_target: Path) -> bool:
    """True when *candidate* points at the same directory as *resolved_target*.

    #1659: used to keep :func:`_free_branch_in_worktrees` from ever treating
    the base checkout as a removable worktree.  Both sides are resolved so a
    symlinked path (``~/.coord/worktrees/<repo> -> ~/src/<repo>`` is an
    established convention on this fleet) compares equal to its target rather
    than sliding past a string comparison.  Resolution failures fall back to a
    plain comparison and are never fatal — a wrong answer here costs a
    checkout, so the only acceptable failure mode is "skip it anyway".
    """
    if not candidate:
        return False
    try:
        return Path(candidate).resolve() == resolved_target
    except OSError:
        return candidate == str(resolved_target)


# ── #1693: the single chokepoint for deleting a worktree directory ──────────
#
# #1659 removed the `shutil.rmtree` fallback from `_free_branch_in_worktrees`
# and added a base-checkout identity check — but the *sibling* deleter 60
# lines below (`_git_worktree_add`'s collision retry) kept both, and it was
# the one that recursively deleted `~/src/claude-coordinator` on dellserver on
# 2026-08-02.  Four call sites had grown the same "`git worktree remove`
# refused → rmtree it anyway" idiom independently; fixing one and missing the
# others is exactly how #1659 became #1693.
#
# So: `_safe_remove_worktree` is now the ONLY place in coord that may call
# `shutil.rmtree` on a worktree path.  `tests/test_worktree_rmtree_chokepoint.py`
# enforces that at the source level so a fifth site cannot quietly appear.
#
# The invariants it enforces, in order:
#   1. Never the main worktree.  `repo_path` and the candidate are both
#      `resolve()`d before comparison, because `~/src/<repo>` can itself be a
#      symlink (dellserver has `~/src/quadraui.broken-backup-… ->
#      ~/.coord/worktrees/<id>`), and `git worktree list --porcelain` always
#      reports the main worktree first.
#   2. A path git does not report as a *linked* worktree is only removable
#      when it lives strictly inside an explicit *sandbox_root* (always
#      `<state_dir>/worktrees/`).  Resolving both sides means a
#      `worktrees/<id> -> ~/src/<repo>` symlink resolves outside the sandbox
#      and is refused rather than followed.
#   3. `git worktree remove --force` is always tried first; `shutil.rmtree`
#      only runs after git declines, and only for a path that already cleared
#      (1) and (2).

_WT_KIND_MAIN = "main"
_WT_KIND_LINKED = "linked"
_WT_KIND_UNREGISTERED = "unregistered"


def _resolve_path(candidate: str | Path) -> Path:
    """``Path(candidate).resolve()``, degrading to an unresolved Path on error.

    Resolution failures must never be fatal here — every caller is about to
    make a *safety* decision, and the only acceptable failure mode is "compare
    the literal path", never "raise out of a cleanup routine".
    """
    try:
        return Path(candidate).resolve()
    except OSError:
        return Path(candidate)


def _classify_worktree(repo_path: Path | None, target: str | Path) -> str:
    """Return ``"main"``/``"linked"``/``"unregistered"`` for *target*.

    ``"main"`` means *target* is the base checkout — the thing #1693 deleted.
    It is decided by a direct ``repo_path`` comparison FIRST (so it holds even
    when git is unavailable) and then by position in ``git worktree list
    --porcelain``, which documents the main worktree as entry 0.

    With *repo_path* ``None`` there is no repository to interrogate, so every
    path is ``"unregistered"`` and the caller's sandbox check is the only gate.
    """
    resolved = _resolve_path(target)
    if repo_path is None:
        return _WT_KIND_UNREGISTERED
    if resolved == _resolve_path(repo_path):
        return _WT_KIND_MAIN
    try:
        output = _git(repo_path, "worktree", "list", "--porcelain")
    except (_GitError, OSError, subprocess.SubprocessError):
        # Can't ask git.  Fall back to "unregistered" — the strictest answer
        # short of "main", which the comparison above has already ruled out.
        return _WT_KIND_UNREGISTERED
    for index, wt in enumerate(_parse_worktree_porcelain(output)):
        wt_path = wt.get("worktree", "")
        if not wt_path or _resolve_path(wt_path) != resolved:
            continue
        return _WT_KIND_MAIN if index == 0 else _WT_KIND_LINKED
    return _WT_KIND_UNREGISTERED


def _is_strictly_inside(candidate: str | Path, root: str | Path) -> bool:
    """True when *candidate* resolves to a proper descendant of *root*.

    Both sides are resolved, so a symlink pointing out of the sandbox (the
    `worktrees/<id> -> ~/src/<repo>` shape) lands outside and returns False.
    ``candidate == root`` is False on purpose: the sweep root itself is never
    a removable worktree.
    """
    resolved_root = _resolve_path(root)
    resolved = _resolve_path(candidate)
    return resolved != resolved_root and resolved_root in resolved.parents


def _safe_remove_worktree(
    repo_path: Path | None,
    worktree_path: str | Path,
    *,
    log_path: str | None = None,
    sandbox_root: str | Path | None = None,
    prune: bool = True,
) -> bool:
    """Remove a git worktree directory, refusing anything that isn't one (#1693).

    This is the only function in coord permitted to ``shutil.rmtree`` a
    worktree path; see the block comment above for the invariants and why they
    exist.

    Args:
        repo_path: The base checkout the worktree belongs to, or ``None`` when
            the caller could not resolve one (a sweep over orphaned
            directories).  ``None`` disables the git-level checks and makes
            *sandbox_root* mandatory.
        worktree_path: The directory to remove.
        log_path: Optional assignment log to append refusal/removal notes to.
        sandbox_root: Directory that a git-*unregistered* path must live
            strictly inside to be removable — always ``<state_dir>/worktrees``
            in practice.  ``None`` means unregistered paths are refused
            outright.
        prune: Run ``git worktree prune`` afterwards (needs *repo_path*).

    Returns:
        ``True`` when the directory is gone (including "was already absent"),
        ``False`` when removal was refused or failed.  Never raises.

    Note:
        This function deliberately does NOT implement the #1394 uncommitted-work
        gate — that lives in :meth:`AgentServer._rescue_uncommitted_work` and
        still runs at its existing call site, *before* the destructive step.
        Duplicating it here would need an ``AgentAssignment`` the sweep paths
        do not have.
    """
    target = Path(worktree_path)

    def _log(msg: str) -> None:
        if log_path:
            _append_log_line(log_path, msg if msg.endswith("\n") else msg + "\n")

    kind = _classify_worktree(repo_path, target)
    if kind == _WT_KIND_MAIN:
        _log(
            f"# worktree-remove: REFUSING {str(target)!r} — it is the base "
            f"checkout (main worktree), not a linked worktree (#1693). "
            f"Nothing was deleted."
        )
        return False
    if kind == _WT_KIND_UNREGISTERED and (
        sandbox_root is None or not _is_strictly_inside(target, sandbox_root)
    ):
        _log(
            f"# worktree-remove: REFUSING {str(target)!r} — git does not "
            f"report it as a linked worktree and it is not inside "
            f"{str(sandbox_root)!r} (#1693). Nothing was deleted."
        )
        return False

    try:
        exists = target.exists()
    except OSError:  # pragma: no cover - defensive
        exists = True

    removed = True
    if exists:
        removed = False
        if repo_path is not None:
            try:
                _git(repo_path, "worktree", "remove", str(target), "--force")
                removed = True
            except (_GitError, OSError, subprocess.SubprocessError):
                removed = False
        if not removed:
            # #1693: the one sanctioned recursive delete.  The path has already
            # been proven to be either a linked worktree or a sandboxed orphan.
            try:
                shutil.rmtree(target, ignore_errors=True)
            except OSError:  # pragma: no cover - ignore_errors makes this rare
                pass
            try:
                removed = not target.exists()
            except OSError:  # pragma: no cover - defensive
                removed = False
            if not removed:
                _log(
                    f"# worktree-remove: {str(target)!r} could not be removed "
                    f"by git or by rmtree — leaving it on disk."
                )

    if prune and repo_path is not None:
        try:
            _git(repo_path, "worktree", "prune")
        except (_GitError, OSError, subprocess.SubprocessError):
            pass
    return removed


def _free_branch_in_worktrees(
    repo_path: Path,
    branch_name: str,
    exclude_path: str,
    *,
    log_path: str | None = None,
) -> None:
    """Remove any worktree that has *branch_name* checked out, except *exclude_path*.

    Runs ``git worktree list --porcelain`` to find conflicting worktrees, then
    force-removes each one and prunes the git admin entries.  Called immediately
    before every ``git worktree add`` in :meth:`AgentServer._setup_worktree` so
    that a stale prior-assignment worktree (e.g. a crashed worker whose
    ``_cleanup_worktree`` never ran) does not block the next dispatch on the
    same branch.

    Silently tolerates git errors — if the list or removal fails, the
    subsequent ``worktree add`` will still surface a clear error.

    #1659: this function must NEVER remove the **main** worktree (the base
    checkout, ``repo_path``).  ``git worktree list --porcelain`` always lists
    it first, and it matches the branch filter like any other entry whenever
    the base happens to be parked on *branch_name* — which #1623 makes routine
    and #1636's ``branch=failed.branch`` re-dispatch makes near-certain on a
    retry.  It is skipped explicitly below.

    #1659: there is also no ``shutil.rmtree`` fallback.  ``git worktree
    remove`` refusing a path is git *protecting* something — most often with
    ``fatal: '<path>' is a main working tree`` — and overriding that refusal
    with ``rm -rf`` destroyed precision's base checkout on 2026-07-31.  A
    failed removal is logged and left alone, which is exactly what this
    docstring already promised: the subsequent ``worktree add`` surfaces it.

    #1693: this function deliberately does NOT route through
    :func:`_safe_remove_worktree`.  The chokepoint still falls back to
    ``shutil.rmtree`` for a genuine linked worktree; this function refuses
    even that, which is strictly more conservative.  What #1693 fixed is the
    sibling that was *less* conservative — see :func:`_git_worktree_add`.
    """
    try:
        output = _git(repo_path, "worktree", "list", "--porcelain")
    except _GitError:
        return

    # #1659: the base checkout, resolved so a symlinked ~/src/<repo> (see the
    # `worktrees/<repo>` symlink convention) can't slip past the comparison.
    try:
        main_path = repo_path.resolve()
    except OSError:
        main_path = repo_path

    removed = 0
    for index, wt in enumerate(_parse_worktree_porcelain(output)):
        wt_path = wt.get("worktree", "")
        if wt.get("branch", "") != branch_name:
            continue
        if wt_path == exclude_path:
            continue
        # #1659: never the main worktree.  Belt and braces — `git worktree
        # list --porcelain` documents the main worktree as the first entry,
        # and it is also `repo_path` itself; either check alone is sufficient,
        # and the cost of a false negative here is a deleted base checkout.
        if index == 0 or _is_same_path(wt_path, main_path):
            if log_path:
                _append_log_line(
                    log_path,
                    f"# worktree-free: NOT removing {wt_path!r} — it is the "
                    f"base checkout, not a linked worktree, even though it "
                    f"holds branch {branch_name!r} (#1659). The `worktree "
                    f"add` below will try to move this checkout back to the "
                    f"default branch non-destructively (#1694), and raise "
                    f"naming it if that is not safe (#1693) — either way it "
                    f"will NOT delete it.\n",
                )
            continue
        # Found a conflicting worktree — force-remove it.
        if log_path:
            _append_log_line(
                log_path,
                f"# worktree-free: removing worktree {wt_path!r} "
                f"holding branch {branch_name!r} before new add\n",
            )
        try:
            _git(repo_path, "worktree", "remove", wt_path, "--force")
            removed += 1
        except _GitError as exc:
            # #1659: NO rmtree fallback.  Report and move on.
            if log_path:
                _append_log_line(
                    log_path,
                    f"# worktree-free: `git worktree remove {wt_path}` failed "
                    f"({exc}) — leaving it on disk (#1659). If the branch is "
                    f"still held, the `worktree add` below will say so.\n",
                )

    if removed:
        try:
            _git(repo_path, "worktree", "prune")
        except _GitError:
            pass


# ── #1694: putting the base checkout back on its default branch ─────────────
#
# The base checkout's steady state is the repo's default branch.  It gets
# parked on a feature branch when something operates in ``~/src/<repo>``
# directly instead of in a worktree — the #1642 family, plus every stage whose
# briefing tells a worker to `git checkout <branch>` without saying *where*
# (`coord.smoke`, `coord.conflict_fix`).  Once parked, `git worktree add` for
# that branch collides forever: #1693 made the collision non-destructive, but
# a refusal is not a resolution, so every retry against that branch on that
# machine is a permanent dispatch failure until a human runs `git checkout`.
#
# Two users of the helpers below:
#   * Part A — `_cleanup_worktree_locked` puts the base back at teardown when
#     THIS assignment's branch is what it is parked on.  Scoped that tightly
#     on purpose: an operator's own checkout, parked on their own branch, is
#     none of the agent's business and is left alone.
#   * Part B — `_git_worktree_add` clears the collision in-line and retries
#     the add exactly once.
#
# Both refuse unless the base is *genuinely* clean.  Never discard or strand
# work to unblock a dispatch: a failed dispatch is recoverable, lost work is
# not (#1693's whole lesson).  The one thing ignored is untracked test output.

# Untracked files that never count as "the base checkout has work in it".
# These are test-runner droppings the fleet writes into checkouts routinely.
_BASE_RESTORE_IGNORABLE_UNTRACKED: frozenset[str] = frozenset({
    ".pytest.out",
    ".cargo.out",
})


def _current_branch(repo_path: Path) -> str | None:
    """The branch checked out at *repo_path*, or ``None``.

    ``None`` covers detached HEAD and every error (not a repo, git missing,
    timeout) — callers treat it as "not parked on a branch", which is the
    safe reading in both directions: nothing to restore, nothing to clear.
    """
    try:
        ref = _git(repo_path, "symbolic-ref", "--quiet", "HEAD").strip()
    except (_GitError, OSError, subprocess.SubprocessError):
        return None
    if not ref.startswith("refs/heads/"):
        return None
    return ref[len("refs/heads/"):] or None


def _base_checkout_move_blockers(repo_path: Path, branch: str) -> list[str]:
    """Reasons *repo_path* must NOT be moved off *branch*.  Empty means safe.

    Every check fails **closed**: a git command that errors out contributes a
    blocker rather than being skipped, because "I could not tell whether there
    was work here" and "there was no work here" must never collapse into the
    same answer.  That asymmetry is the entire safety property.

    The checks, and why each one is load-bearing:

    1. *The base really is the main worktree and really holds the branch.*
       Guards against moving something that is actually a linked worktree
       (git would refuse anyway, but a clear refusal beats a git error) and
       against a stale caller belief about who holds *branch*.
    2. *No uncommitted changes.*  ``--untracked-files=all`` so an untracked
       directory cannot hide a thousand files behind one ``?? dir/`` line.
       Untracked test output (:data:`_BASE_RESTORE_IGNORABLE_UNTRACKED`) is
       the one allowed exception.
    3. *No unpushed commits.*  ``origin/<branch>..HEAD`` when the branch has a
       remote counterpart; otherwise "is HEAD contained in *any* remote ref".
       A branch that exists nowhere on origin is refused — that is somebody's
       only copy of something.
    4. *No stash entries.*  The stash is repo-wide (shared by every worktree)
       and a stash pop after a branch switch is a merge conflict at best, so
       any stash at all blocks the move.
    """
    blockers: list[str] = []

    # 1. Identity: main worktree, and it is the holder of `branch`.
    try:
        porcelain = _git(repo_path, "worktree", "list", "--porcelain")
    except (_GitError, OSError, subprocess.SubprocessError) as exc:
        return [f"could not read `git worktree list` ({exc})"]
    resolved_base = _resolve_path(repo_path)
    holder_index: int | None = None
    for index, wt in enumerate(_parse_worktree_porcelain(porcelain)):
        if wt.get("branch", "") != branch:
            continue
        if _resolve_path(wt.get("worktree", "")) == resolved_base:
            holder_index = index
        else:
            blockers.append(
                f"branch {branch!r} is held by the linked worktree "
                f"{wt.get('worktree', '')!r}, not by the base checkout"
            )
    if holder_index is None:
        blockers.append(
            f"the base checkout is not the worktree holding branch {branch!r}"
        )
    elif holder_index != 0:
        blockers.append(
            f"{str(repo_path)!r} is not the main worktree "
            f"(`git worktree list` entry {holder_index})"
        )

    # 2. Uncommitted changes.
    try:
        status = _git(
            repo_path, "status", "--porcelain", "--untracked-files=all",
            timeout=60.0,
        )
    except (_GitError, OSError, subprocess.SubprocessError) as exc:
        blockers.append(f"could not read `git status` ({exc})")
    else:
        dirty: list[str] = []
        for line in status.splitlines():
            if not line.strip():
                continue
            path = line[3:].strip().strip('"')
            if line.startswith("??") and path in _BASE_RESTORE_IGNORABLE_UNTRACKED:
                continue
            dirty.append(path)
        if dirty:
            shown = ", ".join(dirty[:5])
            more = f" (+{len(dirty) - 5} more)" if len(dirty) > 5 else ""
            blockers.append(f"uncommitted changes: {shown}{more}")

    # 3. Unpushed commits.
    pushed = False
    try:
        ahead = _git(
            repo_path, "rev-list", "--count", f"origin/{branch}..HEAD"
        ).strip()
        pushed = ahead == "0"
        if not pushed:
            blockers.append(
                f"{ahead} commit(s) on {branch!r} are not on origin/{branch}"
            )
    except (_GitError, OSError, subprocess.SubprocessError):
        # No `origin/<branch>` (never pushed, or no remote at all).  HEAD may
        # still be published under some other remote ref — check before
        # declaring the commits unpublished.
        try:
            contained = _git(
                repo_path, "for-each-ref", "--contains", "HEAD",
                "--format=%(refname)", "refs/remotes/", timeout=60.0,
            ).strip()
        except (_GitError, OSError, subprocess.SubprocessError) as exc:
            blockers.append(f"could not check for unpushed commits ({exc})")
        else:
            if contained:
                pushed = True
            else:
                blockers.append(
                    f"branch {branch!r} has no counterpart on origin and its "
                    "HEAD is on no remote ref — its commits exist only here"
                )

    # 4. Stash.
    try:
        stash = _git(repo_path, "stash", "list").strip()
    except (_GitError, OSError, subprocess.SubprocessError) as exc:
        blockers.append(f"could not read `git stash list` ({exc})")
    else:
        if stash:
            blockers.append(
                f"{len(stash.splitlines())} stash entr(ies) present"
            )

    return blockers


def _restore_base_checkout_branch(
    repo_path: Path,
    branch: str,
    default_branch: str,
    *,
    log_path: str | None = None,
    context: str = "base-restore",
) -> str | None:
    """Move the base checkout off *branch*, non-destructively.  (#1694)

    Returns the ref it ended up on (``default_branch``, or ``"HEAD (detached)"``
    when the default branch could not be checked out), or ``None`` when the
    move was refused or failed.  Never raises and never deletes or discards
    anything — the tracked files ARE rewritten to match ``default_branch``'s
    tree (that's the point: HEAD moves off *branch*), but only from a state
    :func:`_base_checkout_move_blockers` has proven clean first, so nothing
    uncommitted, unpushed, or stashed is ever at risk.

    *default_branch* is preferred over ``--detach`` deliberately: a human who
    later opens ``~/src/<repo>`` expects ``main``/``develop``, which is also
    the state every other recovery path in coord restores to (``coord test``'s
    #271 restore, ``coord diagnose``'s base-checkout unblock).  A detached
    HEAD would be the more surprising of the two.  ``--detach`` is only the
    fallback for when the default branch itself cannot be checked out (it does
    not exist locally, or a linked worktree holds it) — freeing the branch
    still beats leaving the collision in place.

    Every outcome is logged.  A base checkout silently changing branch under
    an operator is exactly the kind of magic that makes a fleet unreadable.
    """

    def _log(msg: str) -> None:
        if log_path:
            _append_log_line(log_path, msg if msg.endswith("\n") else msg + "\n")

    if branch == default_branch:
        _log(
            f"# {context}: base checkout {str(repo_path)!r} is on "
            f"{branch!r}, which IS the default branch — nothing to do."
        )
        return None

    blockers = _base_checkout_move_blockers(repo_path, branch)
    if blockers:
        _log(
            f"# {context}: REFUSING to move base checkout {str(repo_path)!r} "
            f"off branch {branch!r} (#1694) — {'; '.join(blockers)}. "
            f"Nothing was changed; free it by hand with "
            f"`git -C {repo_path} checkout {default_branch}` once the work "
            f"above is safe."
        )
        return None

    try:
        _git(repo_path, "checkout", default_branch, timeout=60.0)
    except (_GitError, OSError, subprocess.SubprocessError) as exc:
        _log(
            f"# {context}: `git -C {repo_path} checkout {default_branch}` "
            f"failed ({exc}) — falling back to a detached HEAD, which frees "
            f"branch {branch!r} just as well."
        )
        try:
            _git(repo_path, "checkout", "--detach", timeout=60.0)
        except (_GitError, OSError, subprocess.SubprocessError) as exc2:
            _log(
                f"# {context}: could not move base checkout "
                f"{str(repo_path)!r} off {branch!r} at all ({exc2}) — it is "
                f"unchanged and still parked."
            )
            return None
        return "HEAD (detached)"
    return default_branch


def _git_worktree_add(
    repo_path: Path,
    add_args: list[str],
    *,
    log_path: str | None = None,
    default_branch: str | None = None,
) -> None:
    """Run ``git worktree add <add_args>``, retrying once on a branch collision.

    If the first attempt fails with git's "already used by worktree at '<path>'"
    message (the branch is checked out in a stale worktree that slipped past the
    proactive :func:`_free_branch_in_worktrees` call — e.g. a race), the
    conflicting worktree is force-removed, git prunes the stale admin entry, and
    the add is retried exactly once.  Any other git error, or a failure after the
    single retry, is re-raised so the caller sees a clear ``_GitError``.

    #1693: the conflicting path is *scraped out of git's error text*, so it can
    name anything git happens to mention — including the **base checkout**,
    which is the common case when the operator's own ``~/src/<repo>`` is parked
    on the branch (#1623 makes that routine; ``coord fix``'s same-branch
    re-dispatch makes it likely).  ``git worktree remove`` always refuses the
    main working tree, so the old ``except _GitError: shutil.rmtree(...)``
    fallback was not a rare safety net for that input — it was the guaranteed
    outcome, and it recursively deleted ``~/src/claude-coordinator`` on
    dellserver.  Removal now goes through :func:`_safe_remove_worktree`, and a
    collision naming the base checkout is re-raised with the branch and the
    checkout path named, never removed and never retried.

    #1694: refusing is safe but it is not a *resolution* — the base is still
    parked on the branch, so every subsequent dispatch against that branch on
    this machine fails identically, forever, until a human intervenes.  When
    *default_branch* is supplied the collision is now cleared
    **non-destructively** first: the base checkout is moved back to its
    default branch (see :func:`_restore_base_checkout_branch` for the safety
    gate — clean tree, nothing unpushed, no stash, or it refuses) and the add
    is retried exactly once.  Nothing is ever deleted on this path.

    *default_branch* is deliberately optional and the remedy is skipped
    without it: the helper has to know where to put the base, and a caller
    that cannot say keeps #1693's behaviour unchanged (refuse and re-raise).
    """
    try:
        _git(repo_path, "worktree", "add", *add_args)
        return
    except _GitError as exc:
        m = _WT_COLLISION_RE.search(str(exc))
        if not m:
            raise  # unrelated error — propagate unchanged
        conflicting_path = m.group(1)
        original = exc

    # #1693: refuse before removing.  The base checkout is not a recoverable
    # collision — deleting it breaks every future dispatch for the repo on
    # this machine — so report it instead of "fixing" it.
    if _classify_worktree(repo_path, conflicting_path) == _WT_KIND_MAIN:
        branch = _branch_from_add_args(add_args)

        # #1694: try to clear the collision instead of only reporting it.  The
        # base checkout has no business sitting on a feature branch, so moving
        # it back to the default branch both fixes this dispatch and restores
        # the invariant.  `_restore_base_checkout_branch` refuses outright
        # unless the base is genuinely clean, so this can never trade a failed
        # dispatch for lost work.
        if default_branch:
            parked_on = _current_branch(Path(conflicting_path))
            if parked_on == branch:
                moved_to = _restore_base_checkout_branch(
                    Path(conflicting_path),
                    branch,
                    default_branch,
                    log_path=log_path,
                    context="worktree-add",
                )
                if moved_to is not None:
                    if log_path:
                        _append_log_line(
                            log_path,
                            f"# worktree-add: base checkout was parked on "
                            f"{branch!r}; moved it to {moved_to} to clear the "
                            f"collision (#1694). Nothing was deleted. "
                            f"Retrying the add once.\n",
                        )
                    # One retry only — raises if it fails again.
                    _git(repo_path, "worktree", "add", *add_args)
                    return

        if log_path:
            _append_log_line(
                log_path,
                f"# worktree-add: collision on {conflicting_path!r}, which is "
                f"the BASE CHECKOUT — not removing, not retrying (#1693). "
                f"Move it off branch {branch!r} and re-dispatch.\n",
            )
        raise _GitError(
            f"cannot create a worktree for branch {branch!r}: the base "
            f"checkout {conflicting_path!r} is itself parked on that branch. "
            f"Refusing to remove it (#1693) — run "
            f"`git -C {conflicting_path} switch <default-branch>` and "
            f"re-dispatch."
        ) from original

    # Retry path: free the conflicting worktree and try again once.
    if log_path:
        _append_log_line(
            log_path,
            f"# worktree-add: collision on {conflicting_path!r}; "
            "force-removing and retrying\n",
        )
    _safe_remove_worktree(
        repo_path,
        conflicting_path,
        log_path=log_path,
        # No sandbox_root: a collision path must be a genuine LINKED worktree
        # to be removable here.  Anything else is refused and the retry below
        # raises the real error.
    )
    # One retry only — raises if it fails again.
    _git(repo_path, "worktree", "add", *add_args)


def _branch_from_add_args(add_args: list[str]) -> str:
    """Best-effort branch name out of a ``git worktree add`` argv (#1693).

    Only used to make the base-checkout refusal message actionable, so an
    unrecognised shape degrades to the raw argv rather than raising.  Covers
    the three forms coord builds: ``-b/-B <branch> <path> <start>`` and the
    bare ``<path> <branch>``.
    """
    for index, arg in enumerate(add_args):
        if arg in ("-b", "-B") and index + 1 < len(add_args):
            return add_args[index + 1]
    if len(add_args) == 2:
        return add_args[1]
    return " ".join(add_args)


def _set_worktree_pull_rebase(repo_path: Path, worktree_path: Path) -> None:
    """Default *worktree_path*'s ``pull.rebase`` to true, scoped to that
    worktree only (#1468).

    If this worktree's own push later gets rejected non-fast-forward — most
    commonly because a ``coord-rescue`` WIP commit (see
    :meth:`AgentServer._rescue_uncommitted_work`) landed on the branch from a
    prior, killed assignment — and it reaches for a plain ``git pull``, git
    rebases onto the remote tip instead of creating a merge commit. That
    merge commit is the only reason #1454/#1456 needed a human: GitHub
    refuses to rebase-merge a branch that already contains one (#1467).

    A linked worktree has no config file of its own by default — a plain
    ``git config`` run with ``cwd=worktree_path`` resolves to the *shared*
    ``$GIT_DIR/config``, the same file the base checkout (``repo_path``)
    reads. Setting ``pull.rebase`` that way would leak it onto the operator's
    own checkout as a side effect of dispatching work, which the issue
    explicitly rules out. ``git config --worktree`` writes to a genuinely
    per-worktree file (``$GIT_DIR/worktrees/<id>/config.worktree``) instead —
    but that file is only consulted once ``extensions.worktreeConfig`` is
    enabled on the repo, so that has to be turned on first (idempotent; a
    repeat call from a later worktree setup is a no-op).

    Best-effort throughout: any failure here just means a future ``git
    pull`` in this worktree merges as before, same as pre-#1468 behavior —
    never worth failing the whole worktree setup over.
    """
    try:
        _git(repo_path, "config", "extensions.worktreeConfig", "true")
        _git(worktree_path, "config", "--worktree", "pull.rebase", "true")
    except _GitError:
        pass


def setup_interactive_worktree(
    repo_path: Path,
    issue_number: int,
    issue_title: str,
    assignment_id: str,
    *,
    default_branch: str = "main",
    state_dir: Path | None = None,
    log_path: str | None = None,
    existing_branch: str | None = None,
) -> tuple[Path, str]:
    """Set up a git worktree for an interactive ``coord assign --interactive`` session.

    Mirrors :meth:`AgentServer._setup_worktree` so the interactive launcher gets
    the same branch isolation as agent-dispatched workers: the session runs in a
    fresh worktree on a feature branch derived from the issue number and title,
    never in the live checkout.

    The worktree is placed at ``<state_dir>/worktrees/<assignment_id>/`` —
    the same layout the agent server uses, so
    :func:`coord.interactive.finalize_interactive_exit` can locate and remove it
    by ``assignment_id``.

    Args:
        repo_path: Absolute path to the main repository checkout.
        issue_number: GitHub issue number (used in the branch name).
        issue_title: Issue title (slugified into the branch name).
        assignment_id: Unique ID for this interactive session (used as the
            worktree directory name).
        default_branch: The repo's default branch to branch from.  Resolved
            from ``origin/<default_branch>`` when a remote is configured so
            local unpushed commits on ``<default_branch>`` can't ride into the
            worker branch (#255).
        state_dir: Root state directory; defaults to ``~/.coord``.
        log_path: Optional log file path for diagnostic comments (same as the
            ``log_path`` parameter in :meth:`AgentServer._setup_worktree`).

    Returns:
        ``(worktree_path, branch_name)`` — the :class:`~pathlib.Path` of the
        newly created worktree directory, and the feature branch name checked
        out inside it (``issue-{N}-{slug}``).

    Raises:
        :class:`_GitError`: when the worktree cannot be created (e.g. the
            remote ref for *default_branch* is unreachable, or a git
            command fails).
        :class:`OSError`: when the worktree base directory cannot be created.
    """
    if state_dir is None:
        state_dir = sys.modules[__name__].DEFAULT_STATE_DIR

    worktree_base = state_dir / "worktrees"
    worktree_path = worktree_base / assignment_id

    # Clean up stale worktree if it exists from a prior (crashed) run.
    # #1693: via the single chokepoint — `worktree_base` is the sandbox that
    # makes an unregistered leftover removable and anything else refused.
    if worktree_path.exists():
        _safe_remove_worktree(
            repo_path,
            worktree_path,
            log_path=log_path,
            sandbox_root=worktree_base,
            prune=False,
        )

    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    # Prune stale git admin entries so they don't block worktree add (#389).
    try:
        _git(repo_path, "worktree", "prune")
    except _GitError:
        pass

    # Determine if origin is configured.  In production it always is; only
    # test fixtures and local-only repos lack a remote.
    try:
        _git(repo_path, "remote", "get-url", "origin")
        has_origin = True
    except _GitError:
        has_origin = False

    # Fetch latest only when a remote exists — keeps offline / test path silent.
    if has_origin:
        try:
            _git(repo_path, "fetch", "origin", "--prune")
        except _GitError:
            pass

    # Resolve start point (#255): branch from origin/<default> SHA to prevent
    # unpushed local commits on <default> from riding into the worker's branch.
    if has_origin:
        try:
            start_point = _git(
                repo_path, "rev-parse", f"origin/{default_branch}",
            ).strip()
        except _GitError as exc:
            raise _GitError(
                f"setup_interactive_worktree: cannot resolve origin/{default_branch} "
                f"in {repo_path}. The remote is configured but the ref is missing — "
                "check network connectivity and that default_branch in coordinator.yml "
                f"matches the actual branch on origin. ({exc})"
            ) from exc
    else:
        start_point = default_branch

    # Leg 3 (#517): an explicit existing_branch (e.g. --fix-of continuing the
    # reviewed work's branch) overrides the derived name so the fix lands on the
    # SAME branch and updates the same PR.  When it already exists on origin the
    # continuation path below checks it out at the remote tip.
    branch_name = existing_branch or f"issue-{issue_number}-{_slugify(issue_title)}"

    # Check whether origin or local already has this branch (retry / continuation).
    origin_has_branch = False
    local_has_branch = False
    if has_origin:
        try:
            _git(repo_path, "rev-parse", "--verify", f"refs/remotes/origin/{branch_name}")
            origin_has_branch = True
        except _GitError:
            pass
        # #412 guard: confirm the remote-tracking ref is still live on the actual remote.
        if origin_has_branch:
            try:
                remote_heads = _git(
                    repo_path, "ls-remote", "--heads", "origin", branch_name
                )
                if not remote_heads.strip():
                    origin_has_branch = False
            except _GitError:
                pass  # network hiccup — trust the (pruned) local ref
    try:
        _git(repo_path, "rev-parse", "--verify", f"refs/heads/{branch_name}")
        local_has_branch = True
    except _GitError:
        pass

    # Evict any conflicting worktree that already has branch_name checked out.
    _free_branch_in_worktrees(repo_path, branch_name, str(worktree_path), log_path=log_path)

    if origin_has_branch:
        # Continuation / retry — force the worktree's branch to the remote tip
        # (#389), discarding any divergent local copy.
        _git_worktree_add(
            repo_path,
            ["-B", branch_name, str(worktree_path), f"origin/{branch_name}"],
            log_path=log_path,
            default_branch=default_branch,
        )
    elif local_has_branch and not has_origin:
        # Local-only repo (no remote) — reuse the local branch.
        _git_worktree_add(
            repo_path,
            [str(worktree_path), branch_name],
            log_path=log_path,
            default_branch=default_branch,
        )
    else:
        # Fresh branch, or an untrusted local-only leftover in a repo that has a
        # remote (#389).  Delete any colliding local branch first.
        try:
            _git(repo_path, "branch", "-D", branch_name)
        except _GitError:
            pass
        _git_worktree_add(
            repo_path,
            ["-b", branch_name, str(worktree_path), start_point],
            log_path=log_path,
            default_branch=default_branch,
        )

    # #1468: same worktree-scoped `pull.rebase` default as
    # `AgentServer._setup_worktree` — see `_set_worktree_pull_rebase` for why.
    # An interactive session's worktree can hit the same rejected-push-then-
    # `git pull` sequence as a headless worker's.
    _set_worktree_pull_rebase(repo_path, worktree_path)

    return worktree_path, branch_name


@dataclass
class AgentAssignment:
    """Server-side record. Carries the spec plus runtime metadata."""

    id: str
    spec: AssignmentSpec
    status: str = PENDING
    pid: int | None = None
    started_at: float | None = None
    finished_at: float | None = None
    exit_code: int | None = None
    log_path: str | None = None
    error: str | None = None
    branch: str | None = None
    worktree_path: str | None = None
    # #315: claude session ID captured from the `system.init` event in the
    # worker log.  Set by `_reap` after the worker exits.  Exposed via
    # `/status` and persisted in the agent state JSON so it survives agent
    # restart.  The coordinator reads it from the `/status` response and
    # writes it to the coordinator DB (see coord/notify.py).
    claude_session_id: str | None = None
    # #448: advisory reason when the worker exited cleanly (exit_code==0) but
    # pushed 0 commits.  None on all other status values.
    #
    # #2188: also None (never set) for an issue labelled `deliverable:
    # analysis` — that shape lands on `status == DONE`/`analysis_deliverable
    # = True` below instead, since 0 commits is its SUCCESS condition, not
    # something advisory.
    #
    # #1323 used to *also* reuse this field (for "work"-type assignments,
    # see _ADVISORY_TYPES) to hold a reason when a configured artifact_paths
    # glob matched 0 files, downgrading DONE -> ADVISORY in the process.
    # That was reverted by #1357: most work in a repo is unrelated to any
    # one artifact_paths glob (e.g. claude-coordinator's only glob is a Rust
    # `tui/` binary, which every Python-only change is guaranteed to miss),
    # so a glob miss is not evidence the assignment failed — it false-failed
    # the overwhelming majority of headless work in this repo. `stash_
    # unmatched_globs` below is the diagnostic-only replacement; this field
    # is once again *only* about commit count.
    zero_commit_reason: str | None = None
    # #1357 (revert of the #1323 downgrade): diagnostic-only note recording
    # which configured artifact_paths glob(s) matched 0 files for a "work"
    # assignment's stash. Deliberately kept separate from zero_commit_reason
    # — a stash miss is not a commit count, and conflating the two on a
    # single opaque string is what made a healthy, pushed, exit-0 assignment
    # land on the board as a false Red failure. Setting this field NEVER
    # changes `status`; it exists purely so a human (or `coord pull-artifact`
    # /the Test stage) can see "no stashed artifact for this glob, rebuild
    # from source" without having to dig through the raw worker log.
    stash_unmatched_globs: list[str] | None = None
    # #1394: set when the assignment finished with uncommitted changes in its
    # worktree.  Records what happened to that work — rescued as a WIP commit
    # on the assignment branch (and whether the push landed), or left in place
    # with the worktree deliberately kept.  Never None-but-deleted: if this
    # field is set, the work still exists somewhere, and the string says where.
    #
    # This is the "wrote something we could not commit" signal that #1394 was
    # about.  A bare ADVISORY ("wrote nothing") leaves it None; when the reap
    # already flagged ADVISORY, `zero_commit_reason` is rewritten to carry the
    # same message so the board, `coord status` and the GitHub advisory comment
    # all stop claiming "0 commits pushed" when work was in fact rescued.
    dirty_worktree_reason: str | None = None
    # #1461: set when the worker's transcript shows it was killed by hitting
    # the account's Max/Pro *session* usage limit rather than crashing on a
    # real defect — see coord.worker_events.detect_usage_limit_kill_in_log.
    # Formatted with coord.worker_events.format_usage_limit_reason (stable
    # "usage limit — resets <time>" prefix). None on every other outcome.
    # Set regardless of whether `_reap` landed on FAILED or ADVISORY — a kill
    # has been observed producing either, depending on whether the CLI ended
    # the turn before or after committing.  The coordinator (reconcile.py)
    # reads this live field and stamps it onto the board's persisted
    # `failure_reason` column so `coord status` and `coord drive` (#1392) can
    # both recognise it without re-parsing the log themselves.
    usage_limit_reason: str | None = None
    # #1584: set when the worker's LAST `result` event carried `is_error:
    # true` — e.g. a transient upstream 529/500 that killed the session
    # before it did anything.  Formatted with
    # coord.worker_events.format_api_error_reason (e.g. "529 Overloaded").
    # Only ever set alongside `status == FAILED`; `None` on every other
    # outcome, including a worker that hit the same transient error, retried
    # internally, and finished cleanly (its LAST `result` event carries no
    # `is_error`, so this never fires for it).
    api_error_reason: str | None = None
    # #1797: set when `_reap`'s belt-and-suspenders `git push` raised an
    # AUTH-SHAPED error (see `_is_auth_push_failure`) — captures the raw
    # error text, e.g. "remote: Invalid username or token. Password
    # authentication is not supported for Git operations." Only ever set
    # alongside `status == FAILED`; `None` on every other outcome —
    # including a push failure for an unrelated reason (no `origin`
    # configured, network blip), which stays non-fatal exactly as before
    # this field was introduced, AND (#2356) including an auth-shaped
    # failure on THIS push that turns out to be superseded: when
    # `_remote_already_has_head` confirms the content already reached
    # `origin` via some other remote/protocol (e.g. the #2269 SSH-
    # workaround case), the push failure is logged but this field stays
    # `None` and the assignment keeps its normal (non-FAILED) outcome —
    # the work is where it needs to be, so the failed push here is a
    # non-event, not a signal.
    #
    # Exists to keep an auth break from being silently absorbed into either
    # DONE (worker had local commits that never reached origin) or the
    # unrelated `zero_commit_reason` ADVISORY ("nothing to push" reads very
    # differently from "had something to push and couldn't"). Before this
    # field existed, a broken credential helper (#1797 — cloud-init baked an
    # empty `$GH_TOKEN` into the git credential helper at image-bake time
    # instead of leaving it to expand at push time) surfaced only as a line
    # in the worker log that nothing downstream ever read.
    push_failure_reason: str | None = None
    # #2131: set when the reap's watchdog killed this leg because its live
    # spend crossed `spec.cost_ceiling_usd`. Formatted with
    # `coord.spend_ceiling.format_spend_ceiling_reason` (stable "spend
    # ceiling — " prefix, recognised by `is_spend_ceiling_reason`). Only ever
    # set alongside `status == FAILED` (the kill returns the non-zero
    # `SPEND_CEILING_EXIT`), and `None` on every other outcome.
    #
    # This field is the WHOLE POINT of #2131's "not a generic failure"
    # requirement: the coordinator stamps it onto the board's persisted
    # `failure_reason` (coord/reconcile.py) so `coord retry` can refuse to
    # silently re-spend, `auto_reassign` can decline to re-dispatch it, and
    # the escalation record can name what actually happened. Without it a
    # ceiling kill is indistinguishable from a crash and the money is spent
    # again on the next pass.
    spend_ceiling_reason: str | None = None
    # #2638: set when the reap's watchdog killed this leg because its
    # WALL-CLOCK runtime crossed the resolved runtime ceiling (either
    # `spec.runtime_ceiling_s` or, absent that, this agent's own configured
    # default — see `_DEFAULT_RUNTIME_CEILING_S`). Formatted with
    # `format_runtime_ceiling_reason` (stable "runtime ceiling — " prefix,
    # recognised by `is_runtime_ceiling_reason`). Only ever set alongside
    # `status == FAILED` (the kill returns the non-zero
    # `RUNTIME_CEILING_EXIT`), and `None` on every other outcome — including
    # a leg that ran long but finished before the ceiling (nothing here) and
    # one killed instead by host-sleep detection (`host_sleep_reason` below;
    # mutually exclusive with this field by construction — see
    # `_wait_for_proc_or_result`).
    #
    # Distinguishable-from-a-crash is the whole point, same as #2131's
    # `spend_ceiling_reason`: without it a suspended-host kill reads exactly
    # like a generic FAILED, `coord retry` cheerfully re-spends the same
    # multi-hour timeout, and nothing tells the operator the leg simply ran
    # too long rather than crashing on a real defect.
    runtime_ceiling_reason: str | None = None
    # #2638: set when the reap's watchdog detected the host suspending mid-
    # leg — wall-clock elapsed diverged sharply from monotonic elapsed over
    # one poll interval, a signature only a suspend/resume produces (see
    # `_wait_for_proc_or_result`'s host-sleep check). Formatted with
    # `format_host_sleep_reason` (stable "host sleep detected — " prefix,
    # recognised by `is_host_sleep_reason`). Only ever set alongside `status
    # == FAILED` (the kill returns the non-zero `HOST_SLEEP_EXIT`), and
    # `None` on every other outcome.
    #
    # This is the #2638 incident's actual root cause made visible: a leg
    # that slept through a multi-hour suspend is not a result anyone should
    # trust resumed — `coord status`/`coord health` should say so instead of
    # rendering it identically to healthy work, which is exactly what let a
    # suspended worker hold its assignment `running` for 10.5h unnoticed.
    host_sleep_reason: str | None = None
    # #2188: True when `_reap` classified a clean (exit_code==0), 0-commit
    # exit as a DELIVERABLE — the issue was labelled
    # `coord.models.DELIVERABLE_ANALYSIS_LABEL` (`spec.issue_labels`) — rather
    # than the #448 "worker did nothing" ADVISORY. Only ever set alongside
    # `status == DONE`; `False` on every other outcome, including a normal
    # zero-commit ADVISORY (the label wasn't present) and a labelled issue
    # whose worker actually pushed commits (that's an ordinary DONE, handled
    # by the ordinary Test/Review/Merge pipeline — this flag is specifically
    # "the deliverable IS the message, there is no diff").
    analysis_deliverable: bool = False
    # #2188: the worker's own final message — `result` off the last
    # stream-json `result` event (`coord.worker_events.WorkerSummary.
    # result_text`), captured here so the coordinator can post the actual
    # deliverable (a diagnosis/audit's prose) to the issue instead of a bare
    # "assignment complete" comment. The worker itself has no `gh` access to
    # post this — see coord.notify.post_transition's EVENT_COMPLETION arm.
    # Populated only when `analysis_deliverable` is True; `None` otherwise
    # (a normal work assignment's deliverable is the diff itself, already
    # visible on the PR — restating the transcript there would be noise).
    result_text: str | None = None
    # #2234: the worker's own final message when `_reap` classifies a clean
    # (exit_code==0), 0-commit exit as a policy refusal (`status ==
    # REFUSED_POLICY`) rather than the #448 ADVISORY default — see
    # `_looks_like_policy_refusal`. `None` on every other outcome, including
    # an ordinary zero-commit ADVISORY (no marker matched) and the #2188
    # analysis-deliverable success shape (mutually exclusive with this one:
    # both are decided in the same `_ahead == 0` branch of `_reap`, and only
    # one of `analysis_deliverable`/`zero_commit_reason`/this field is ever
    # set for a given assignment).
    policy_refusal_reason: str | None = None
    # #2316: set when `_reap` classifies a clean (exit_code==0), 0-commit
    # exit as a TRUNCATION — the worker's last `result`/`step_finish` event
    # carries a `stop_reason` in `_TRUNCATION_STOP_REASONS` (opencode's
    # `"length"`, claude's `"max_tokens"`) — rather than the #448 ADVISORY
    # default. Formatted with `_format_truncation_reason`. Only ever set
    # alongside `status == FAILED` (never ADVISORY: a truncated run is not a
    # "worker looked and found nothing to do", it is a cut-off nobody chose),
    # and `None` on every other outcome — including a genuine clean-exit-no-
    # commits run (no truncation marker matched) and a truncated run that DID
    # push commits (`_ahead != 0` never reaches this check at all). Mutually
    # exclusive with `zero_commit_reason`/`analysis_deliverable`/
    # `policy_refusal_reason`: all four are decided in the SAME `_ahead == 0`
    # branch of `_reap`, and this one is checked first — see that method.
    truncation_reason: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    def to_status_dict(self) -> dict:
        """Serialize for `/status` and for on-disk persistence (#715).

        For **terminal** assignments (done/failed/cancelled/advisory), strips
        the heavy `spec.briefing`/`spec.system_prompt` text — a full briefing
        can be tens of KB, and no coordinator reader consumes it from a
        terminal `/status` entry (the briefing already lives on the board /
        GitHub; `coord/notify.py` only reads small scalar fields like
        `status`, `exit_code`, `branch`, `claude_session_id`, cost/tokens).
        `_COMPLETED_HISTORY_CAP` terminal entries × a full briefing each is
        what made `/status` slow enough to trip the coordinator's 3s
        health-poll timeout (#452 capped by *count*; this caps by *size*).

        Active (pending/running) assignments are returned unchanged — some
        callers (`coord status`) still read `spec.type`/`spec.review_target`
        off in-flight entries to label what's currently running.
        """
        d = asdict(self)
        if self.status not in (PENDING, RUNNING):
            spec = d.get("spec")
            if spec is not None:
                spec["briefing"] = ""
                spec["system_prompt"] = None
        return d


WORKER_SYSTEM_PROMPT = """\
You are a Claude Code worker executing an assignment from the coordinator.

Rules:
- Do NOT run gh commands. The coordinator owns all GitHub interactions \
(issues, PRs, comments). Use regular git commands only.
- Stay within the files listed in your briefing. If you need to touch \
other files, do so only if strictly necessary and note it.
- If the briefing lists forbidden files, do NOT read or modify them. \
They are managed by the coordinator.
- You are already on a feature branch. Commit your work to this branch. \
Push with `git push origin HEAD`. \
NEVER commit or push to main or develop directly. \
Do NOT open a PR — the coordinator handles that.
- Work only inside your current working directory. It is your own git \
worktree, checked out from a repo that also lives at `~/src/<repo>` on this \
machine — never read or write anything under `~/src/<repo>` (or any other \
absolute path outside your cwd). That shared checkout is not yours: edits \
there are lost, or collide with other workers running at the same time. If \
your worktree looks unexpectedly empty or unwritable, STOP and report it — \
do not fall back to editing the base checkout and copying files over.
- For "where is X handled" / "what calls this" / architecture questions, \
query the codebase graph first: `graphify query "<question>"` via Bash. \
Grep/Read are for confirming an exact string or line, not for discovering \
structure. No `graphify-out/graph.json` in this worktree? Skip straight to \
grep, silently — do not stop to build one (#2212).

This session is ONE-SHOT and non-interactive (#1394):
- There is no next turn and no human to reply to you. Background-task \
completion notifications will NEVER reach you — nothing wakes you up.
- NEVER start a long-running command in the background and then end your \
turn waiting for it (no `run_in_background`, no `&`, no "I'll wait for the \
test run to finish") — there is no next turn for a notification to land \
in, so ending your turn that way throws the work away. Prefer running it \
in the FOREGROUND and blocking until it returns. If it genuinely cannot \
finish inside one Bash call, you may background it and poll it yourself, \
in bounded steps, from THIS SAME still-running session — `Monitor`, or \
repeated `TaskOutput` calls with a bounded `timeout`, never a foreground \
loop that blocks past the ceiling (`until ! pgrep ...; do sleep ...; done` \
hits the identical 600s wall) and never a final message that leaves it \
running unattended and relies on being woken back up. Otherwise, raise the \
timeout or skip it and say so. If you end your turn waiting, the session \
is over and your work is thrown away.
- ALWAYS `git add`, `git commit`, and `git push origin HEAD` BEFORE your \
final message — even if the build is broken, the tests are failing, or you \
ran out of time. Uncommitted changes are destroyed when the session ends. \
A committed work-in-progress with an honest final message is strictly \
better than a perfect uncommitted diff, which is worth nothing.
- Your final message is the LAST thing you will ever say. Never end it with \
"I'll continue", "waiting for X", or "will follow up" — finish or report \
the blocker.

Before writing any code, verify the feature or fix isn't already implemented. \
Grep for relevant function names, check existing modules, and read related files. \
If it already exists, report back instead of reimplementing.

Progress reporting:
- After each significant step (first build, test run, approach change), \
output a status line in exactly this format:
  STATUS: [what you just did] → [what you're about to do] → [confidence: high/medium/low]
- If you've tried 2 approaches and neither worked, STOP and output:
  STUCK: [what you tried] [why it failed] [what you think the blocker is]
  Then wait for guidance rather than trying a third approach.

Before declaring done:
- Run the project's build command (detect it from the repo: \
`cargo build` for Cargo.toml, `pytest` for pyproject.toml with pytest, \
`make` for Makefile, `npm run build` for package.json, etc.).
- If the build emits warnings — unused vars, dead code, deprecated APIs, \
ambiguous lifetimes, missing docs on public items — FIX THEM. \
Compiler warnings are part of the diff you're shipping; the human \
shouldn't have to clean up after you. Treat warnings as failures for \
the purposes of "done".
- If a warning genuinely can't be fixed in scope (third-party crate, \
intentional `#[allow]` with reason, a deferred refactor flagged \
elsewhere), explicitly call it out in your final message with the \
reason. Don't silently ship warnings.
- Re-run the build after fixes to confirm clean output.
- Run only the tests that cover your diff — never the project's whole \
test suite. A full suite routinely exceeds this tool's 600-second Bash \
ceiling, and a worker that tries to shepherd it past that wall (splitting \
it into chunks, backgrounding it and blocking on a poll loop, ...) burns \
most of a session on that instead of the fix, for no net-new signal: the \
Test stage and CI both re-run the FULL suite against your pushed SHA right \
after you, on a clean checkout, which is strictly better evidence than a \
worker's own partial run (#2169). Find the test file(s)/module(s) whose \
name or path mirrors what you changed (e.g. `tests/test_<module>.py` for \
`coord/<module>.py`, the crate-local `#[cfg(test)]` block for a Rust file) \
and run just those — confirm they pass before declaring done.
- Exception: if this assignment IS an oracle-loop acceptance round (you \
were told to run `coord acceptance run --issue N`), keep running that \
sealed slice as many times as it takes to go green. It is the loop's \
convergence signal, not duplicated effort, and the scoping rule above does \
not apply to it.
- If even the scoped run genuinely cannot finish in time, follow the \
background-and-poll pattern in the ONE-SHOT section above — do not invent \
a chunk-and-loop workaround.
- #2192: check your own diff now — does it change user-visible behavior \
AND add/modify zero test files? That exact pattern was 18.5% of this \
repo's blocking reviews (#2132): the code was correct, but CLAUDE.md's \
"Testing — black-box coverage is the acceptance bar" rule was skipped, so \
the adversarial reviewer rejected it — costing a paid review leg plus a \
fix + re-review round trip to catch something you can see for free right \
now, in this same already-running session. If it fires, add the test \
before declaring done. If this genuinely is a pure refactor / \
internal-only change (CLAUDE.md's existing exemption), say so explicitly \
in your final message instead — that already satisfies the reviewer, no \
test required.

#252: before exiting, emit a SMOKE_TESTS block telling the human what to \
manually verify.  You changed the code; you know what's worth poking.

  SMOKE_TESTS:
  - [scenario] — [how to trigger] — [what to look for]
  - [scenario] — [how to trigger] — [what to look for]
  END_SMOKE_TESTS

Keep it to 2-5 items, one bullet per line.  Each bullet has three \
em-dash-separated parts: the scenario, the trigger, and the success \
signal.  Prefer scenarios that exercise the changed code paths, not \
generic app sanity.  Include any commands the human should re-run on \
their hardware (e.g. `cargo test --features gtk` when only that build \
exercises the changed delegation).

For coord-tui changes: any behaviour reachable through the TuiDriver \
harness (``driver_with_shell`` + ``TestBackend``) belongs in an \
**in-crate ``#[cfg(test)]`` test**, not a SMOKE_TESTS bullet — the \
automated headless smoke is the gate.  Reserve SMOKE_TESTS for the \
quadraui#302 blind spot: raw-mode, SGR mouse, and the embedded claude \
PTY pane that TuiDriver cannot reach.

If the change is purely internal — no user-visible behaviour, no new \
codepaths the existing test suite already covered — emit exactly:

  SMOKE_TESTS: (none — change is internal)
  END_SMOKE_TESTS\
"""

WORKER_PLAN_PROMPT = """\
You are a Claude Code planning worker. Read the codebase and produce a \
structured implementation plan. Do NOT write code, create files, or modify \
anything — read and analyse only.

Output your plan using exactly these headings:

FILES_READ: <comma-separated list of every file you examined>
FILES_MODIFY: <comma-separated list of files that would need to change>
APPROACH: <concise description of the implementation approach (3-5 sentences)>
RISKS: <potential blockers, conflicts, or tricky areas>
ESTIMATE: <rough complexity: trivial | small | medium | large>

Then emit a SMOKE_TESTS block — what the human should manually verify after \
the work lands. You know the intent at planning time; you don't yet know \
which diff lines will exist, but you do know which user-visible behaviours \
this change is meant to affect. Author smoke tests against intent, not \
mechanism.

  SMOKE_TESTS:
  - [scenario] — [how to trigger] — [what to look for]
  - [scenario] — [how to trigger] — [what to look for]
  END_SMOKE_TESTS

Keep it to 2-5 items, one bullet per line. Each bullet has three \
em-dash-separated parts: the scenario, the trigger, and the success signal. \
Include any commands the human should re-run on their hardware (e.g. \
`cargo test --features gtk` when only that build exercises the change).

For coord-tui changes: any behaviour reachable through the TuiDriver \
harness (``driver_with_shell`` + ``TestBackend``) belongs in an \
in-crate ``#[cfg(test)]`` test, not a SMOKE_TESTS bullet — the \
automated headless smoke is the gate. Reserve SMOKE_TESTS for the \
quadraui#302 blind spot: raw-mode, SGR mouse, and the embedded claude \
PTY pane that TuiDriver cannot reach.

If the change is purely internal — no user-visible behaviour, automated \
tests already cover the affected paths — emit exactly:

  SMOKE_TESTS: (none — change is internal)
  END_SMOKE_TESTS

Rules:
- Do NOT run gh commands.
- Do NOT write, edit, or create any files.
- Do NOT commit or push anything.
- Use Read and Bash (read-only commands like grep, find, cat) only.
- After reading the issue body and relevant code, output the plan and stop.\
"""

REFINEMENT_SYSTEM_PROMPT = """\
You are a refinement assistant helping a developer scope a GitHub issue \
before any code is written. You are NOT a worker — you do not implement, \
edit, or create files. Your job is to clarify intent.

The first user message contains the issue body, recent comments, the repo's \
CLAUDE.md, and a top-level file-tree snapshot. Use the Read tool to inspect \
specific files when the conversation calls for it.

In each reply:
- Ask focused clarifying questions about scope, acceptance, and edge cases \
the issue doesn't yet pin down. One or two questions per turn — do not flood.
- When you propose files or modules the change would touch, name them \
explicitly so the developer can confirm or correct.
- Surface unknowns: behaviours that depend on context the issue doesn't \
mention, places where existing code could conflict, follow-up work the \
change might imply.
- Keep replies short. The developer is typing live; long monologues slow \
the loop.

Rules:
- Do NOT run gh, git, npm, cargo, or any tool that mutates the repository or \
the GitHub state. Use Read only.
- Do NOT write or edit files. Do NOT propose a diff.
- Do NOT decide the issue is ready on the developer's behalf. They mark it \
ready by closing the chat with Done.
- If asked to write code, decline politely and reframe as "what behaviour \
should that code produce?" — refinement is about intent, not implementation.\
"""

TEST_CHAT_SYSTEM_PROMPT = """\
You are a test-stage assistant helping a developer validate a code change \
before it moves to review. You are NOT a code-writing worker — you do not \
implement, commit, or push. Your job is to help the developer understand \
what to test and why.

The first user message contains the PR diff, the most recent build log, \
the worker's SMOKE_TESTS block, the repo's run command (if any), and the \
repo's CLAUDE.md. Use the Read tool to inspect specific files and the Bash \
tool to run read-only diagnostic commands (builds, tests, lint) when the \
conversation calls for it.

In each reply:
- Explain what the diff changes and which behaviours to verify.
- Surface which smoke-test bullets are highest-risk given the diff.
- Suggest specific manual steps or automated checks (commands, test filters).
- If a build or test command fails, help the developer diagnose the root cause.
- Keep replies focused. The developer is validating live; long walls of \
text slow the loop.

Rules:
- Do NOT run gh commands. The coordinator owns all GitHub interactions.
- Do NOT run git push, git commit, or any command that writes to the repo.
- Do NOT write or edit files.
- Do NOT call coord sub-commands.
- Do NOT decide the change is ready on the developer's behalf — they \
record Pass/Fail via the TUI (P=pass / F=fail).\
"""

NEW_ISSUE_CHAT_SYSTEM_PROMPT = """\
You are a new-issue assistant helping a developer draft a well-structured \
GitHub issue before it is filed. You are NOT a worker — you do not \
implement, edit, or create files. Your job is to help articulate what \
should be built or fixed.

The first user message contains:
- The repo's CLAUDE.md (project conventions and rules)
- Per-repo issue guidance (required sections, style rules)
- A list of recently open issues (for near-duplicate detection)

Your goal is to guide the developer through a focused conversation and \
produce a finished issue draft. When the draft is ready, present it in \
this exact format:

  TITLE: <active-voice title, ≤80 chars>
  ---
  <full issue body in Markdown>

In each reply:
- Ask ONE or TWO focused questions per turn — do not flood with a wall \
of questions.
- Flag if the described issue closely resembles an existing open issue.
- Keep replies short. The developer is typing live.

Rules:
- Do NOT call `gh issue create`, `gh pr`, or any mutating `gh` command. \
The developer's client handles submission — your job is to produce the draft.
- Do NOT write, edit, or commit any files.
- Do NOT implement the feature described in the issue.
- Use `Read` and read-only `Bash` commands (e.g. `grep`, `find`, `cat`) \
to look up relevant code context when the conversation calls for it.\
"""

MILESTONE_CHAT_SYSTEM_PROMPT = """\
You are a milestone steward helping an operator author and shape a GitHub \
milestone. You are a widened (#1009) but still deliberately scoped slice of \
the broader "milestone-steward" chat (#645): your job is to propose — and, \
once the operator explicitly confirms — write any of: a new milestone's \
metadata, an existing milestone's title/description/due date, an issue's \
milestone assignment, or the `## Work order` block that `coord milestone \
order` (#768) parses into a dispatch DAG. Every write below follows the \
SAME discipline: present the exact proposed change, wait for explicit \
confirmation, then run exactly one command.

The first user message tells you which of these you're seeding for:
- An EXISTING milestone: its tracking issue (current body, which may already \
carry a `## Work order` block from a prior run) and the open issues filed \
under it (title + body), plus the milestone's current title/description/due \
date when available.
- A NEW milestone (no tracking issue exists yet): the target repo and, if \
the operator supplied one when starting this chat, a seed title and/or a \
short prompt describing what the milestone should cover.

In each reply:
- Discuss the milestone and its issues with the operator as needed.
- When asked — or when it's clearly useful — infer a proposed work order: \
which issues could run in PARALLEL (`group: <label>`; any issues sharing a \
label may dispatch concurrently) and which have a HARD dependency \
(`after: #N[,#M...]`; wait until N and M are both done). Ground this in \
explicit signals in the issue bodies — references to other issue numbers, \
"depends on" / "blocks" / "after" phrasing, clearly overlapping files or \
components — do not invent a dependency the text doesn't support.
- Present the proposed block to the operator in this exact checklist shape \
before writing anything, and explain your reasoning briefly so they can \
correct it:

    - [ ] #762  {group: A}
    - [ ] #763  {group: A}
    - [ ] #765  {after: #762,#763}

- Only AFTER the operator explicitly confirms (e.g. "yes", "write it", \
"looks good"), write the block with exactly this command, piping the \
confirmed checklist lines via stdin:

    coord milestone write-order <repo> <tracking_issue> <<'EOF'
    - [ ] #762  {group: A}
    ...
    EOF

  `coord milestone write-order` re-validates the block (parses it, checks \
for cycles, unknown `after` targets, and milestone membership) before \
writing, and idempotently replaces any existing `## Work order` section \
rather than duplicating it — safe to re-run after the operator asks for \
changes.

You may ALSO propose and (once confirmed) run these four milestone-authoring \
commands — each is a single, explicit write, never chained, never run \
speculatively:
- Creating the milestone (only when the first user message says none exists \
yet): discuss goal/scope/rough due date with the operator, present the exact \
title/description/due-on you'll use, and once confirmed:

    coord milestone create <repo> --title '<title>' [--description '<desc>'] [--due-on <iso8601>]

  Report the printed milestone number back to the operator — there is no \
tracking issue yet, so there is nothing to write a work order to until one \
is filed separately.
- Editing the milestone's title, description, or due date: present the \
exact new value(s), and once confirmed:

    coord milestone edit <repo> <number> [--title '<title>'] [--description '<desc>'] [--due-on <iso8601>]

  Pass only the field(s) that changed.
- Assigning an issue to this milestone: present which issue and which \
milestone, and once confirmed:

    coord milestone assign <repo> <issue> '<milestone_number_or_title>'

- Splicing a child issue onto an epic's `## Sub-issues` checklist (#1008) — \
when the operator wants to add (or remove) a sub-issue of the tracking \
issue you're discussing: present the candidate issue, and — when adding — \
any `group`/`after` annotation you're inferring the same way you would for \
a `## Work order` entry, then once confirmed:

    coord milestone add-child <repo> <epic> <issue> [--group '<group>'] [--after <N>[,<M>...]]
    coord milestone add-child <repo> <epic> <issue> --remove

  `add-child` is idempotent (re-adding with identical annotations is a \
no-op; adding again with different ones updates the line in place) and \
leaves the epic's `## Work order` section untouched — safe to re-run.
- Shell-quote every title/description/milestone value you fill into the \
four templates above with SINGLE quotes, never double quotes — double \
quotes let the shell expand `$(...)`, backticks, and `$VAR` in the value \
before `coord` ever sees it, which is unsafe for arbitrary operator-\
supplied text (this is exactly why the `write-order` heredoc above uses \
the quoted `<<'EOF'` delimiter instead of a bare `<<EOF`). If a value \
itself contains a single quote, close the quote, insert `\'`, and reopen \
it — e.g. "operator's plan" becomes `'operator'\''s plan'`. Never leave a \
value unquoted or wrapped in double quotes.
- If the operator asks for changes after you've proposed anything above, \
revise and re-present before writing — never write on the first pass \
without confirmation, and never write more than the one thing that was just \
confirmed.

Rules:
- Do NOT run mutating `gh` commands (issue edit/create/close, pr *, api -X \
PATCH/POST/DELETE, milestone *) — the write paths are `coord milestone \
write-order`, `coord milestone create`, `coord milestone edit`, `coord \
milestone assign`, and `coord milestone add-child`; never raw `gh`.
- Do NOT run `git push`, `git commit`, or any command that writes to the repo.
- Do NOT run `coord approve`, `coord merge`, or the top-level `coord assign \
<machine> <repo> <issue>` (fleet work dispatch — a different, much \
bigger-blast-radius command than `coord milestone assign` above) — all \
outside this session's job.
- Do NOT write ANYTHING (work order, milestone create/edit/assign, or \
add-child) until the operator has explicitly confirmed that specific change \
in this conversation.
- Use `Read` and read-only `Bash` (e.g. `coord milestone order <repo> \
<tracking_issue>` to preview the live frontier, `gh issue view`/`gh api \
GET` to look things up) to ground your proposal in the current board state \
when useful.\
"""

# Deny list applied to milestone-chat workers.  The write actions permitted
# are `coord milestone write-order` / `create` / `edit` / `assign` /
# `add-child` (allowed by omission — this list only blocks); raw `gh`
# mutations and unrelated `coord` write commands are blocked so GitHub
# milestones/issues are the only thing this session can touch, and only via
# the validated coord path. `coord milestone add-child` (#1008) was denied
# until #1008 merged; #1017 lifts that restriction now that it has.
MILESTONE_CHAT_DENY_COMMANDS: list[str] = [
    "Bash(gh issue edit *)",
    "Bash(gh issue create *)",
    "Bash(gh issue delete *)",
    "Bash(gh issue close *)",
    "Bash(gh api -X PATCH *)",
    "Bash(gh api -X POST *)",
    "Bash(gh api -X DELETE *)",
    "Bash(gh pr *)",
    "Bash(gh repo *)",
    "Bash(gh milestone *)",
    "Bash(git push *)",
    "Bash(git commit *)",
    "Bash(git reset --hard *)",
    "Bash(git reset * --hard *)",
    "Bash(git branch -D *)",
    "Bash(git branch * -D *)",
    "Bash(git checkout -- .)",
    "Bash(git clean -f *)",
    "Bash(git clean * -f *)",
    "Bash(rm -rf *)",
    "Bash(rm -fr *)",
    "Bash(coord approve *)",
    "Bash(coord merge *)",
    "Bash(coord assign *)",
]

DECOMPOSITION_CHAT_SYSTEM_PROMPT = """\
You are a decomposition steward for a customer's APPROVED portal submission \
that an operator just pulled into this session (#2533, ms-67 contract §4; \
#2750, IL-4 — "the intake session"). This is ONE ITERATION of a possibly \
multi-day, multi-session intake — a client round-trip takes days, so no \
single session can sit and wait for one. Every iteration is disposable; the \
`coord portal` ledger (Q&A, decisions, narrative) is what's durable, and it \
is what makes it safe for a LATER iteration on a DIFFERENT machine to pick \
up exactly where this one left off with no memory of this conversation.

The first user message carries, in order: a MODE line (`MODE: FILE` or \
`MODE: DISCUSS`) naming which posture this iteration runs in and WHY it was \
picked — say so yourself, verbatim, as the first line of your own final \
response (a session that silently chose to file instead of asking is \
exactly the failure this design exists to prevent); the submission's \
OUTCOME, AUDIENCE, DONE DEFINITION, and CONSTRAINTS; its mapped repo(s); \
`coordinator.yml` topology context for those repo(s) (depends_on, which \
machines claim each repo); a HOUSE STACK section (#2997) naming what the \
REST of this org's registered repos already run and deploy on, which \
managed services are already in use and already paid for, and any coord \
gate that assumes a particular host; and a RUNNING CONTEXT section \
rendering the submission's full ledger so far — every question asked and \
(if answered) its answer, every decision on record (current and archived, \
archived ones carrying WHY they were ruled out), and the current \
narrative. Read RUNNING CONTEXT before doing anything else: never re-ask a \
question already answered there, and never re-propose a decision already \
archived there without new information that changes the calculus — cite \
the existing entry's seq/reason if the operator or a re-briefed fact \
brings it up again.

**HOUSE STACK is context, not a mandate** — you may still propose \
something outside it when it genuinely fits better (a greenfield repo is \
not required to inherit the fleet's stack). But you may never propose a \
stack/architecture/hosting/vendor decision in SILENCE about it: if HOUSE \
STACK names a service that's a plausible fit and you're proposing \
something else, you MUST record the house-stack option itself as a \
considered-and-rejected alternative (`coord portal decision propose` \
naming it, then `coord portal decision reject ... "<why it loses>"`) in \
the SAME iteration you propose your own recommendation — the same \
"record every alternative you seriously considered and ruled OUT" rule the \
PROPOSE terminal move already requires below applies to the house stack \
first, not last. The failure this closes (#2997, SUB-1EA1D3) was a session \
proposing a brand-new vendor for a greenfield repo with FOUR rejected \
alternatives on record and Cloudflare — the stack the rest of the org \
already runs and pays for — never mentioned in any of them. Silence, not \
disagreement, was the bug; a reasoned rejection is a completely fine \
outcome.

You can always re-fetch the live ledger mid-session with:

    coord portal ledger <submission_id>

(add `--json` for the raw structured form) — useful in a long-running \
`--interactive` conversation where the operator may answer something out of \
band while you're mid-thought.

── Filing a decomposition (MODE: FILE, or MODE: DISCUSS's "Decompose" exit) ──

1. Decide whether this is ORACLE-LOOP-SHAPED work (docs/ORACLE_LOOP.md) — \
big or cross-cutting enough to warrant a Gate-A contract + independent \
test-author, or small enough to skip straight to normal dispatch. Flag your \
reasoning even when it's obvious.
2. Produce one or more GitHub issues describing the work, using:

    coord issue create <repo> --title '<title>' --body '<body>'

   (or `--body-file` for long markdown — never raw `gh issue create`, this \
repo's own house rule). If the decomposition is milestone-shaped, file an \
epic/tracking issue first (`coord milestone create <repo> --title '<title>'`) \
and file the sub-issues, then `coord milestone add-child <repo> <epic> \
<issue>` each of them onto it.
3. Queue every filed issue via:

    coord drive-queue add <repo> <issue>

   — NEVER `coord assign` or `coord drive --tmux` (this repo's own standing \
preference: always go through the drive queue, never ad-hoc dispatch).
4. Record the portal link so downstream tooling can find this submission's \
work again (2026-08-22 briefing amendment — this is NOT optional):

    coord portal link <repo> <milestone_number> <submission_id>

   Use the SUBMISSION_ID from your first user message and the milestone \
number `coord milestone create` printed. If `coord portal link` fails (bad \
repo, bad milestone number, daemon-host guard), that is a FAILURE of this \
step — say so explicitly to the operator, do not silently move on.

   **If your decomposition produced a single one-off issue with no \
milestone/epic** (not a milestone-shaped decomposition): `coord portal \
link` has NO non-milestone form today — it requires a `milestone_number`, \
which a one-off issue doesn't have. Do NOT invent one or skip the link \
silently. Say so explicitly in your final summary to the operator (e.g. \
"this submission has no recorded portal link — coord portal link only \
covers milestones, and #2533's own dispatcher found no equivalent for a \
one-off issue"), so the gap is visible rather than silently lost.
5. Archive the decision trail onto the epic (#2750 — "every iteration must \
record its rejections with reasons", surfaced where the next human to read \
the epic will actually see it, not just in `coord portal ledger`). When a \
milestone/epic was filed in step 2: run `coord portal ledger <submission_id>` \
(read-only), take its "## Decisions" and "## Archive (superseded / \
rejected)" sections verbatim, and splice them onto the epic's body under a \
`## Decisions` heading — read the epic's current body (e.g. `gh issue view \
<epic> --json body -q .body`), append the section (or replace a pre-existing \
`## Decisions` heading in place — do not duplicate it), and write it back \
with `coord issue edit <repo> <epic> --body-file <tmpfile>` (never raw `gh \
issue edit`). Skip this step only when the ledger has no decisions at all \
(a submission that never went through a MODE: DISCUSS iteration) — say so \
rather than writing an empty section.

── MODE: DISCUSS — ask / propose / decompose ──

Your first user message said `MODE: DISCUSS` because the submission is \
under-specified or the mapped repo is greenfield (#2750's own mechanical \
triggers — missing done-definition/audience, or no commits/no CLAUDE.md on \
the mapped repo yet). Filing straight through here means inventing an \
architecture or a done-definition on the client's behalf and queuing real \
work against the guess. Instead, THIS iteration ends in EXACTLY ONE of \
three terminal moves — never zero, never more than one:

1. ASK — when what's missing can only come from the CLIENT (not from the \
operator, not from repo history, not from a reasonable judgment call you \
could make yourself). Prefer ONE well-formed question: the portal's \
composer on the client's side pauses on the most recent question only, so \
a scattershot list gets a partial answer at best. Then:

    coord portal enqueue-question <submission_id> "<question>"

   This one command queues the question AND announces it to the customer \
(#2901) — `enqueue-question` queues its own `needs-input` status right \
behind the question, so there is no separate announcement step to remember \
or forget.

   and STOP — end your final turn summarizing the question and why, and run \
nothing else this iteration (no filing, no decisions). IL-3's consumer \
wakes the next iteration once the client answers.

2. PROPOSE — when you CAN make a reasonable judgment call on the client's \
behalf (an architecture/stack/framework choice, a scope cut, anything a \
competent operator could just decide) but it deserves OPERATOR sign-off \
before it becomes load-bearing — this is OPERATOR-facing, never portal-\
facing; the client is never asked to adjudicate a framework choice. Record \
your recommendation:

    coord portal decision propose <submission_id> "<decision text>"

   Then, in the SAME iteration, record every alternative you seriously \
considered and ruled OUT — with a reason — so a later iteration doesn't \
re-litigate it (#2750: "without recorded rejections you re-litigate"):

    coord portal decision propose <submission_id> "<alternative text>"
    coord portal decision reject <submission_id> <alt_seq> "<why ruled out>"

   If you're REVISING a decision an earlier iteration already proposed or \
confirmed (not a same-iteration alternative), use supersede instead of \
reject so the earlier text stays on record rather than reading as an \
outright mistake:

    coord portal decision supersede <submission_id> <old_seq> <new_seq>

   Then STOP — summarize every proposal and rejection for the operator and \
run nothing else. Do NOT confirm your own proposal (`coord portal decision \
confirm` is the operator's move, taken outside this session) and do NOT \
file or queue anything until a later iteration is briefed with a confirmed \
decision. Confirmation is what starts that next iteration.

3. DECOMPOSE — when done_definition/audience are captured (directly, or \
settled via decisions now CONFIRMED in RUNNING CONTEXT) and every mapped \
repo has something to decompose against. Follow "Filing a decomposition" \
above in full, including step 5's decision archive.

If genuinely nothing has changed since the last iteration (re-briefed with \
no new answer and no new information), say so explicitly and re-Ask or \
re-Propose rather than inventing new work to look productive.

Rules:
- Do NOT run raw `gh issue create`/`gh issue edit`/`gh pr *`/`gh api -X \
POST|PATCH|DELETE` or any other mutating `gh` command — the write paths \
above (`coord issue create`, `coord issue edit --body-file`, `coord \
milestone create/add-child`, `coord drive-queue add`, `coord portal link`, \
`coord portal enqueue-question`, `coord portal enqueue-status`, `coord \
portal decision propose/reject/supersede`) are the only ones you may use. \
`gh issue view` and other read-only `gh`/`coord` lookups are fine.
- Do NOT run `coord assign <machine> <repo> <issue>` or `coord drive \
--tmux` — always `coord drive-queue add`.
- Do NOT run `coord approve` or `coord merge` — outside this session's job.
- Do NOT run `coord portal decision confirm` — that is the OPERATOR's move, \
never this session's own.
- Do NOT run `git push`, `git commit`, or any command that writes to a repo \
checkout — this session files issues and queues work, it does not touch code.
- If an `enqueue-question`/`enqueue-status`/`decision` command refuses with \
a thin-client error (this machine does not claim every repo the submission \
maps to — #2995/#2751; it should not happen, since you were only dispatched \
here because it does), say so explicitly to the operator rather than \
silently dropping the Ask/Propose; do not retry it as some other command.
- Keep the operator informed: summarize your plan before writing anything, \
and report back what you asked/proposed/filed/queued/linked (and any gap) \
when done.\
"""

# #2867: the ATTENDED posture. Appended to (never merged into)
# DECOMPOSITION_CHAT_SYSTEM_PROMPT by `coord portal decompose-chat
# --interactive` only — the headless dispatch
# (`default_worker_command`'s `spec.type == "decomposition-chat"` branch)
# must keep receiving the base prompt byte-for-byte, because it genuinely
# has nobody to ask and its one-turn fire-and-forget shape is correct there.
#
# WHY this exists at all: #2750 shipped `--interactive` reusing the headless
# prompt verbatim, so the attended session was written for an agent with
# nobody to ask ("THIS iteration ends in EXACTLY ONE of three terminal
# moves", each a command to run) and duly read the ledger, chose ASK, and
# ran `enqueue-question` in the SAME turn — its whole output to the human
# sitting there was a report of what it had already done. On a paid client
# engagement where a round-trip takes days, that spent a day on questions
# the operator may already have had answers to. The base prompt's
# "summarize your plan before writing anything" was not enough: nothing
# made it a STOP, and without a turn boundary a compliant session still
# writes.
DECOMPOSITION_CHAT_ATTENDED_ADDENDUM = """\

── ATTENDED SESSION (`--interactive`) — OVERRIDES THE ABOVE ──

An operator is sitting at this terminal RIGHT NOW, watching you type. That \
changes the shape of this iteration, and where this section conflicts with \
anything above, THIS SECTION WINS.

**YOUR FIRST TURN WRITES NOTHING.** Not `coord portal enqueue-question`, not \
`coord portal enqueue-status`, not `coord portal decision propose/reject/\
supersede`, not `coord issue create`, not `coord milestone create`, not \
`coord drive-queue add`, not `coord portal link`, not `coord issue edit`. \
Read-only commands (`coord portal ledger`, `gh issue view`, reading files) \
are fine and expected. The "EXACTLY ONE of three terminal moves" rule above \
still decides WHICH exit this iteration takes — it just does not get to \
happen until the operator has answered you.

So your first turn is:

1. State your READ — the MODE line verbatim (as always), then what RUNNING \
CONTEXT already tells you, explicitly including any operator-supplied \
background recorded there.
2. State your PROPOSED EXIT — which of Ask / Propose / Decompose you intend, \
the exact command(s) you would run (question text, decision text, issue \
titles), and WHY that exit and not the other two.
3. State what you are ASSUMING or MISSING — above all, anything you were \
about to ask the CLIENT that the OPERATOR may simply know. The operator may \
have spoken to the client since the last iteration; a client round-trip \
costs days and an operator answer costs one line, so ask the human in front \
of you FIRST.
4. END YOUR TURN AND WAIT. Do not narrate a plan and then execute it in the \
same turn — the turn boundary IS the confirmation.

Then act on what the operator says: carry out the exit as proposed, or the \
revised one they steer you to. If they confirm with no changes, run the \
commands exactly as you stated them and report back. If they redirect you \
into a different exit, take that one — it is still exactly one terminal \
move per iteration.

**Recording what the operator tells you.** Background the operator relays \
("I spoke to her — it's just the two of them, and the calendar is a \
nice-to-have") is durable, ledger-class context that every FUTURE session on \
every machine should see. It is not a decision, so do NOT push it through \
`coord portal decision propose`. Record it verbatim with:

    coord portal note <submission_id> "<what the operator told you>"

This is an ADDITIONAL write path you are permitted, on top of the ones the \
Rules section above enumerates — and, being a write, it is also subject to \
the first-turn rule: offer it, then record it once the operator agrees. \
OFFER IT PROACTIVELY: the operator should not have to know this command \
exists. Any time the operator gives you a substantive fact about the \
client, the users, or the scope, say back what you would record and ask \
whether to record it. Keep the wording theirs, not your paraphrase — the \
ledger is verbatim by design, and the narrative (which is regenerable) is \
where summarizing belongs.

**Confirming a decision on the operator's instruction (#2998).** The base \
prompt above says never to run `coord portal decision confirm` — that rule \
was written for a session with nobody to ask. You have someone to ask: the \
operator sitting at this terminal, who is exactly who that command is \
reserved for. What never changes, headless or attended, is that YOU may \
never confirm your own proposal on your own initiative — inferring consent, \
or treating agreement about something else as consent to confirm, is \
exactly the self-approval the base rule exists to prevent.

But when the operator gives you an EXPLICIT, PRESENT-TURN instruction to \
confirm a specific decision ("confirm decision 8", "yes, confirm that"), you \
MAY act on it, in this order:

1. Quote the operator's instruction back, verbatim, so the transcript makes \
unambiguous that this was ordered, not inferred.
2. Record the attribution on the ledger BEFORE confirming, so a later \
session (or a human reading the ledger) can see this was operator-\
instructed rather than session-initiated:

    coord portal note <submission_id> "Operator instructed: confirm decision #<seq> (\"<their exact words>\")"

3. Only then run:

    coord portal decision confirm <submission_id> <seq>

Do not collapse this into one step, and do not run it on a vague or implied \
go-ahead — if you are not sure which decision the operator means, ask for \
the seq before running anything. This carve-out covers `decision confirm` \
ONLY. Every other entry on the deny list stays forbidden in this session \
exactly as it is headlessly (raw `gh` mutations, `git push`, `git commit`, \
destructive git, `coord approve`, `coord merge`, `coord assign`) — an \
operator being present changes who may authorize a RESERVED command, it \
does not change whether a DANGEROUS one is safe.\
"""

# Deny list applied to decomposition-chat workers (#2533; extended #2750
# IL-4 for the ask/propose/decompose intake loop). Unlike milestone-chat,
# this type's WHOLE job is to write (issue create / issue edit / milestone
# create+add-child / drive-queue add / portal link / enqueue-question /
# enqueue-status / decision propose|reject|supersede), so this list is
# deliberately narrow — it blocks raw `gh` mutations, repo-write git
# commands, destructive git ops, the three unrelated `coord` write commands
# (approve/merge/assign) explicitly out of scope, and `coord portal decision
# confirm` (the OPERATOR's move, never this session's own — #2750's "Propose"
# terminal move must not self-confirm), while every write path this session
# actually needs is allowed by omission.
#
# This is the HEADLESS list — used verbatim by `default_worker_command`'s
# `spec.type == "decomposition-chat"` branch below, where the session
# genuinely has nobody to ask, so `decision confirm` stays hard-denied. The
# `--interactive` posture has an operator in the room; see
# DECOMPOSITION_CHAT_ATTENDED_DENY_COMMANDS below (#2998) for its own,
# narrower carve-out of exactly this one entry.
DECOMPOSITION_CHAT_DENY_COMMANDS: list[str] = [
    "Bash(gh issue edit *)",
    "Bash(gh issue create *)",
    "Bash(gh issue delete *)",
    "Bash(gh issue close *)",
    "Bash(gh api -X PATCH *)",
    "Bash(gh api -X POST *)",
    "Bash(gh api -X DELETE *)",
    "Bash(gh pr *)",
    "Bash(gh repo *)",
    "Bash(gh milestone *)",
    "Bash(git push *)",
    "Bash(git commit *)",
    "Bash(git reset --hard *)",
    "Bash(git reset * --hard *)",
    "Bash(git branch -D *)",
    "Bash(git branch * -D *)",
    "Bash(git checkout -- .)",
    "Bash(git clean -f *)",
    "Bash(git clean * -f *)",
    "Bash(rm -rf *)",
    "Bash(rm -fr *)",
    "Bash(coord approve *)",
    "Bash(coord merge *)",
    "Bash(coord assign *)",
    "Bash(coord portal decision confirm *)",
]

# #2998: the ATTENDED counterpart to the list above — every entry the same
# EXCEPT `coord portal decision confirm`. An attended session has an
# operator sitting at the terminal, and `decision confirm` is RESERVED for
# the operator (not DANGEROUS the way `git push`/`coord merge`/destructive
# git are) — the question in that posture is who authorised it, not whether
# it's safe. DECOMPOSITION_CHAT_ATTENDED_ADDENDUM spells out the condition
# under which the session may actually run it (an explicit, present-turn
# operator instruction, quoted back and attributed on the ledger via `coord
# portal note` before confirming) — this list only stops blanket-denying it
# so that addendum isn't fighting a FORBIDDEN COMMANDS entry telling the
# session the opposite. Every genuinely dangerous entry stays denied,
# unchanged, in both postures.
DECOMPOSITION_CHAT_ATTENDED_DENY_COMMANDS: list[str] = [
    cmd for cmd in DECOMPOSITION_CHAT_DENY_COMMANDS
    if cmd != "Bash(coord portal decision confirm *)"
]

MOCK_AUTHOR_SYSTEM_PROMPT = """\
You are an independent mock-author agent for Gate A of a milestone \
(#930, docs/ORACLE_LOOP.md) — the pre-work architecture gate. You have \
ZERO context from whichever workers will later implement this milestone's \
issues; that independence is the whole point, mirroring the adversarial \
code reviewer.

The first user message names the milestone (tracking issue + open issues \
filed under it) and the exact `tests/acceptance/ms-NN/` directory + mock \
format (e.g. `.screen` text grids for a TUI driver, a self-contained \
`.html` wireframe for web/Electron) declared for this repo.

Your job, and ONLY your job:
1. Read the milestone's tracking issue and open issues to understand what's \
being built.
2. Render a VIEWABLE MOCK of the milestone's user-facing surface in the \
declared medium, under `tests/acceptance/ms-NN/mocks/` — something the \
operator can look at and react to, not a text description. For a \
`web-playwright` driver: one hand-authored, self-contained `.html` file \
PER SCREEN STATE (not one giant multi-state file)‹INTERACTIVE_CARVEOUT› \
Each file must OPEN IN A \
BROWSER AND LOOK LIKE THE SCREEN — inline `<style>` CSS is expected and \
encouraged, since the mock is the visual contract as well as the \
structural one. Do not ship a bare DOM skeleton with no styling and call \
it a mock. Use real, semantic markup (roles, labels, `data-testid` \
attributes on anything a test would need to target) — the independent \
test-author writes DOM assertions against exactly this markup.
3. Write `tests/acceptance/ms-NN/contract.md` pinning the exact black-box \
surface the mock implies: CLI command names, key screen text, API field \
shapes — whatever the milestone's workers and the independent test-author \
must agree on without a shared session.

Rules:
- Do NOT touch any file outside `tests/acceptance/ms-NN/`. You are not \
implementing the milestone — you are pinning its contract. If the \
milestone's own issue bodies are unclear or contradictory, say so in the \
contract's notes rather than silently resolving it yourself.
- Do NOT run gh commands. The coordinator owns all GitHub interactions.
- You are already on a feature branch. Commit your work to this branch. \
Push with `git push origin HEAD`. NEVER commit or push to the repo's \
default branch directly. Do NOT open a PR — the coordinator handles that.
- Work only inside your current working directory (your own git worktree). \
Never read or write anything under `~/src/<repo>` (or any other absolute \
path outside your cwd) — that is the shared base checkout, not yours.
- If `contract.md` already exists (you are AMENDING an existing Gate A, not \
authoring one from scratch), read it first and edit it in place — do not \
start over or duplicate sections.

Before declaring done, re-read `contract.md` once and check it names \
concrete, checkable surface (exact command names / screen strings / field \
names) rather than vague prose — that is what makes it a contract and not \
a spec.

#252: before exiting, emit a SMOKE_TESTS block telling the human what to \
manually verify in the rendered mock.

  SMOKE_TESTS:
  - [scenario] — [how to trigger] — [what to look for]
  END_SMOKE_TESTS
"""

# #3131 review: the CSS-only `:target` interactive-walkthrough carve-out is
# spliced into MOCK_AUTHOR_SYSTEM_PROMPT (replacing the ‹INTERACTIVE_CARVEOUT›
# sentinel below, at dispatch time — see the `mock-author` branch further
# down) ONLY when `coord.mock_author.INTERACTIVE_MOCK_WALKTHROUGHS_ENABLED`
# is true. Before this, the sentence was unconditional prose in the constant
# itself: every mock-author session — including one dispatched via an
# operator's own free-text `coord acceptance mock ... --amend "make this a
# :target interactive walkthrough"` — was told the technique was permitted,
# regardless of the flag. That's the exact pre-coord-portal#314 CSP degrade
# the flag exists to prevent (`style-src` falls back to `default-src
# 'self'`, so the mock's own inline `<style>` is dropped: every `.screen`
# renders stacked at once with the nav inert). Gating the sentinel closes
# that path without needing `dispatch_acceptance_mock` to pattern-match
# amend text for the technique's name — a worker that is never told the
# exception exists has no instruction to act on even if asked.
MOCK_AUTHOR_INTERACTIVE_CARVEOUT = (
    " — UNLESS the seed briefing explicitly calls for a CSS-only `:target` "
    "interactive walkthrough (#3131), in which case the screens it covers "
    "belong TOGETHER in that one file by design: `:target` needs them "
    "sharing a single document and stylesheet to switch which one is "
    "visible."
)

#: Spliced in when the flag above is false — closes the sentence the
#: sentinel sits inside without mentioning the exception at all.
MOCK_AUTHOR_INTERACTIVE_CARVEOUT_DISABLED = "."

# Deny list applied to mock-author workers.  Unlike milestone-chat this type
# DOES commit/push (it's authoring real files under tests/acceptance/), so
# git commit/push stay allowed; gh and destructive git/coord commands don't.
MOCK_AUTHOR_DENY_COMMANDS: list[str] = [
    "Bash(gh *)",
    "Bash(git push --force*)",
    # #2314: the entry above only matches `--force` IMMEDIATELY after
    # `push` — a `git push --quiet --force ...` with another flag pushed
    # in first would evade it.
    "Bash(git push * --force*)",
    "Bash(git reset --hard *)",
    "Bash(git reset * --hard *)",
    "Bash(git branch -D *)",
    "Bash(git branch * -D *)",
    "Bash(git checkout -- .)",
    "Bash(git clean -f *)",
    "Bash(git clean * -f *)",
    "Bash(rm -rf *)",
    "Bash(rm -fr *)",
    "Bash(coord approve *)",
    "Bash(coord merge *)",
    "Bash(coord assign *)",
]

# Deny list applied to new-issue-chat workers.  Allows read-only gh
# (e.g. `gh issue list`, `gh issue view`) while blocking all mutations.
NEW_ISSUE_CHAT_DENY_COMMANDS: list[str] = [
    "Bash(gh issue create *)",
    "Bash(gh issue delete *)",
    "Bash(gh issue edit *)",
    "Bash(gh pr create *)",
    "Bash(gh pr merge *)",
    "Bash(gh pr close *)",
    "Bash(gh pr edit *)",
    "Bash(gh repo *)",
    "Bash(git push *)",
    "Bash(git commit *)",
    "Bash(git reset --hard *)",
    "Bash(git reset * --hard *)",
    "Bash(git branch -D *)",
    "Bash(git branch * -D *)",
    "Bash(git checkout -- .)",
    "Bash(git clean -f *)",
    "Bash(git clean * -f *)",
    "Bash(rm -rf *)",
    "Bash(rm -fr *)",
]


# Deny list applied to review workers (#2461). Unlike every other member of
# WRITE_CAPABLE_SPEC_TYPES, a reviewer has ZERO legitimate mutations of its
# own — coord.review.REVIEWER_SYSTEM_PROMPT already tells it "You are NOT
# allowed to push commits or modify the PR's code. You only review" and "You
# are NOT allowed to run any `gh` commands" (the coordinator posts the
# review on its behalf after the session ends). Before #2461 that was
# enforced by the PROMPT ALONE: `"review"` wasn't one of the explicit
# branches in `default_worker_command`, so it fell through to the generic
# `else` below and got the exact same `Read,Edit,Write,Bash,Monitor` grant as
# a real work leg — nothing but the model's own good behaviour stood between
# "read the diff" and "silently commit a fix and push it". This list is
# wired into BOTH the system prompt (via `build_deny_prompt`, soft — a
# reminder) AND `--disallowedTools` (CLI-enforced, hard — see the
# `spec.type == "review"` branch below) so a reviewer that ignores its own
# prompt still cannot shell out to a mutating git/gh command. The read-only
# git the briefing actually tells reviewers to run (`git fetch`, `git diff`,
# `git log`) stays allowed by omission.
REVIEW_DENY_COMMANDS: list[str] = [
    "Bash(gh *)",
    "Bash(git push *)",
    "Bash(git commit *)",
    "Bash(git add *)",
    "Bash(git merge *)",
    "Bash(git rebase *)",
    "Bash(git cherry-pick *)",
    "Bash(git reset *)",
    "Bash(git checkout *)",
    "Bash(git switch *)",
    "Bash(git branch *)",
    "Bash(git stash *)",
    "Bash(git clean *)",
    "Bash(git rm *)",
    "Bash(git apply *)",
    "Bash(git am *)",
    "Bash(git tag *)",
    "Bash(rm -rf *)",
    "Bash(rm -fr *)",
    "Bash(coord approve *)",
    "Bash(coord merge *)",
    "Bash(coord assign *)",
]


WorkerCommandBuilder = Callable[[AssignmentSpec], list[str]]


def build_deny_prompt(deny_commands: list[str]) -> str:
    """Format a deny-list into a system prompt section.

    Returns an empty string when *deny_commands* is empty so callers can
    unconditionally append the result.
    """
    if not deny_commands:
        return ""

    # Strip the "Bash(...)" wrapper for readability in the prompt while
    # keeping the original pattern for reference.
    lines: list[str] = []
    for pattern in deny_commands:
        # Show the human-friendly command inside Bash(...)
        inner = pattern
        if inner.startswith("Bash(") and inner.endswith(")"):
            inner = inner[5:-1]
        lines.append(f"- {inner}")

    return (
        "\n\nFORBIDDEN COMMANDS — you must NEVER run these:\n"
        + "\n".join(lines)
        + "\n"
        + "If you need to do something that resembles a forbidden command, STOP and output:\n"
        + "  STUCK: need to run [command] but it's on the deny-list"
    )


def bash_deny_pattern_matches(pattern: str, command: str) -> bool:
    """Whether *command* (a raw shell command string) is caught by a single
    ``Bash(...)`` deny *pattern*, using the same shell-glob semantics those
    patterns are written in throughout this module (``*`` matches any run of
    characters, including none, and including across what would look like an
    argv token boundary — this is a whole-string glob, not an argv-aware
    parse).

    #2314: a worker evaded ``Bash(pip install -e *)`` simply by inserting
    another flag first (``pip install --user -e .``) — the pattern only
    matched ``-e`` IMMEDIATELY after ``install``. This function exists so
    that claim ("this deny list actually catches that evasion") is a
    testable fact rather than something read off the pattern text by eye —
    see the ``Bash(...)`` entries :data:`coord.config.DEFAULT_DENY_COMMANDS`
    pairs for exactly this reason, and ``tests/test_worker_safety.py`` for
    the regression tests built on top of this function.

    Returns ``False`` for a *pattern* that isn't a ``Bash(...)`` rule at all
    (e.g. an ``Edit(...)``/``Write(...)`` path rule) — those constrain a
    different tool and never match a shell command string.
    """
    if not (pattern.startswith("Bash(") and pattern.endswith(")")):
        return False
    inner = pattern[5:-1]
    return fnmatch.fnmatchcase(command.strip(), inner)


def find_denying_bash_pattern(command: str, deny_commands: list[str]) -> str | None:
    """The first pattern in *deny_commands* that blocks *command*, or ``None``.

    Thin fold over :func:`bash_deny_pattern_matches` — a worker's own deny
    list is small (single digits to low tens of entries), so a linear scan
    needs no index.
    """
    for pattern in deny_commands:
        if bash_deny_pattern_matches(pattern, command):
            return pattern
    return None


# #1315: sealed-oracle path prefix that only an independent authoring type
# (``"mock-author"``/a future ``"test-author"``) may ever write to
# (docs/ORACLE_LOOP.md sealing v1). ``coord.dispatch.dispatch`` already
# auto-adds this exact string to ``AssignmentSpec.files_forbidden`` for
# every non-``"mock-author"`` type dispatched against a repo with an
# acceptance driver configured (#944) — but until now that signal was
# advisory-only: prompt text in ``WORKER_SYSTEM_PROMPT`` ("If the briefing
# lists forbidden files, do NOT read or modify them") that a worker could
# still be talked past by its own briefing. #1314 hit exactly that gap: a
# ``type="work"`` session whose briefing explicitly directed it to correct
# an already-merged Gate-A contract went ahead and edited
# ``tests/acceptance/**`` anyway — caught only after the fact by the
# adversarial reviewer's tamper check (docs/ORACLE_LOOP.md), which a human
# then chose to override. ``_sealed_write_guard_tools`` turns the same
# ``files_forbidden`` signal into a real ``--disallowedTools`` restriction
# on the ``claude -p`` invocation itself, so a non-independent worker
# literally cannot call Edit/Write under the sealed tree, regardless of
# what its own briefing says.
_SEALED_ORACLE_PREFIX = "tests/acceptance/"


def _sealed_write_guard_tools(files_forbidden: list[str]) -> list[str]:
    """Return ``--disallowedTools`` patterns blocking Edit/Write under any
    sealed-oracle prefix present in *files_forbidden*.

    Pure function, easy to test in isolation. Deliberately scoped to just
    the sealed-oracle prefix (``tests/acceptance/``) rather than every
    ``files_forbidden`` entry — coordinator-only files unrelated to the
    oracle loop (e.g. a doc a worker may legitimately need to *read*) stay
    advisory-only, same as before #1315; turning every forbidden path into
    a hard technical block is a separate, larger change with its own risk.
    """
    patterns: list[str] = []
    for f in files_forbidden:
        if f == _SEALED_ORACLE_PREFIX or f.startswith(_SEALED_ORACLE_PREFIX):
            prefix = f if f.endswith("/") else f"{f}/"
            for pattern in (f"Edit({prefix}**)", f"Write({prefix}**)"):
                if pattern not in patterns:
                    patterns.append(pattern)
    return patterns


# #1642: worktree isolation was enforced by cwd ALONE — nothing stopped a
# worker from constructing an absolute path back into the shared base
# checkout (``spec.repo_path``, the very checkout ``_setup_worktree``
# branched the worker's worktree from) and editing it directly. Observed on
# a haiku-routed worker: its first tool call was an absolute-path ``Read``
# into the base checkout, never named anywhere in its briefing or prior
# output, and it stayed there the whole session — re-``cd``-ing out every
# time Claude Code reset its shell cwd back to the worktree, then ``cp``-ing
# files into the worktree at the end to make the commit look clean. A prompt
# rule (see WORKER_SYSTEM_PROMPT) is advisory and a weak model can talk
# itself past it, the same way this worker narrated its way around the
# shell-cwd reset. ``_base_checkout_write_guard_tools`` turns the base
# checkout into a real ``--disallowedTools`` restriction — mirroring
# ``_sealed_write_guard_tools`` above, but for an absolute path OUTSIDE the
# worker's cwd rather than a relative one inside it, so it uses Claude
# Code's ``//<abs-path>`` absolute-path permission marker (the same syntax
# ``_DENY_PATTERN_RE`` below already recognises in settings.json deny
# rules).
def _base_checkout_write_guard_tools(repo_path: str) -> list[str]:
    """Return ``--disallowedTools`` patterns blocking Edit/Write anywhere
    under the shared base checkout *repo_path*.

    Pure function, easy to test in isolation. Returns ``[]`` for an empty or
    non-absolute (after expansion) *repo_path* (defensive — every real spec
    resolves to an absolute one) so callers can unconditionally combine the
    result with ``_sealed_write_guard_tools``.

    *repo_path* is expanded with ``Path.expanduser()`` before the
    absolute-path check: production ``spec.repo_path`` is the raw string
    straight from ``coordinator.yml``'s ``machines[].repo_paths``, and this
    project's own ``coordinator.example.yml`` documents that field with
    tilde-shorthand (e.g. ``~/src/claude-coordinator``) — ``dispatch.py``
    sends it over the wire unexpanded, and neither ``AssignmentSpec`` nor the
    ``/assign`` handler normalizes it. Without expansion here, a tilde-form
    *repo_path* silently produces no patterns at all, reproducing the exact
    silent-escape failure #1642 exists to close, inside the fix itself.
    Every other real filesystem use of ``spec.repo_path`` in this module
    (``AgentServer.assign``, ``_cleanup_worktree_locked``, and others) already
    calls ``Path(...).expanduser()`` first — this mirrors that.

    Claude Code's ``//<abs-path>`` marker already embeds the path's leading
    ``/`` in the doubled slash — i.e. absolute path ``/home/john/src/api``
    becomes ``Edit(//home/john/src/api/**)``, NOT
    ``Edit(///home/john/src/api/**)`` — so the leading ``/`` is stripped
    before splicing *repo_path* into the pattern (mirrors
    ``_deny_pattern_blocks_path``'s inverse: ``raw = "/" + m.group(2)``).
    """
    if not repo_path:
        return []
    normalized = str(Path(repo_path).expanduser()).rstrip("/")
    if not normalized.startswith("/"):
        return []
    body = normalized[1:]
    if not body:
        return []
    return [f"Edit(//{body}/**)", f"Write(//{body}/**)"]


# ── #1445: worktree-writability preflight ───────────────────────────────────
#
# A worker's ability to write into its own worktree is a fleet invariant, not
# a per-machine preference — see #1445 for the incident this guards against:
# a host-local `.claude/settings.local.json` deny rule blanketing
# `~/.coord/**` (added to stop an OPERATOR's interactive session from
# editing coordinator.yml/coord.db) silently also blocked every worker's own
# worktree under `~/.coord/worktrees/<id>/`, burning a full $5.23 session
# that reasoned, designed, and only discovered at the very end that it could
# not save anything. `default_worker_command`'s `--setting-sources user`
# (above) closes off the specific mechanism (workers no longer load
# project/local settings at all), but this preflight check is kept as a
# defense-in-depth: it also catches a blanket deny rule living in the
# machine's *user*-level settings (`~/.claude/settings.json`, still loaded)
# and plain OS-level write failures (read-only mount, wrong ownership, full
# disk) that have nothing to do with Claude Code at all.
#
# #2462 EMERGENCY REVERT (2026-08-20): #2462 briefly switched this to
# `--bare` to also close the hooks/.mcp.json leak (a never-trusted repo's
# `.claude/hooks/` and `.mcp.json` still loading/connecting under
# `--setting-sources user`, since that flag only governs settings.json).
# `--bare` also unconditionally disables reading OAuth credentials / the
# system keychain — this fleet authenticates headless dispatch via
# `claude login`/OAuth (CLAUDE.md: "No API key needed... runs on a Max/Pro
# subscription via OAuth"), not `ANTHROPIC_API_KEY`, so every worker
# dispatch on every machine started failing at turn 1 with "Not logged in"
# within the hour it went live (misreported fleet-wide as generic
# `api_error`). Reverted back to `--setting-sources user` to restore
# dispatch immediately. The hooks/.mcp.json leak this was meant to close was
# real; #2820 closed the `.mcp.json`/MCP-server half of it via
# `--strict-mcp-config` in `default_worker_command` (no OAuth side-effect —
# it only strips MCP server definitions, not hooks). The hooks half is
# still open — re-closing it needs a fix that doesn't require
# `ANTHROPIC_API_KEY`/an `apiKeyHelper` (e.g. explicit `--settings '{}'`
# plus verifying whether hooks can be suppressed without full `--bare`),
# tracked as a fresh follow-up rather than re-landing this as-is.
_DENY_PATTERN_RE = re.compile(r"^(Edit|Write)\(//(.+)\)$")


def _deny_pattern_blocks_path(pattern: str, path: Path) -> bool:
    """True if permission *pattern* (an Edit(...)/Write(...) deny entry) would
    block Claude Code's Edit/Write tools somewhere under *path*.

    Only recognises the specific shape this issue is about: an absolute-path
    pattern using Claude Code's ``//<abs-path>`` marker, optionally with a
    trailing ``/**`` (or ``/*``) wildcard blanketing a whole subtree — e.g.
    ``Write(//home/john/.coord/**)``. Narrower, relative, or mid-pattern-glob
    entries are deliberately not matched: a false negative here just means
    the plain OS-level write probe (which still runs) is the only guard,
    whereas a false positive would incorrectly refuse a machine that is
    actually fine. *path* must be absolute.
    """
    m = _DENY_PATTERN_RE.match(pattern)
    if not m:
        return False
    raw = "/" + m.group(2)
    if raw.endswith("/**"):
        base = raw[: -len("/**")]
    elif raw.endswith("/*"):
        base = raw[: -len("/*")]
    else:
        base = raw
    base_path = Path(base)
    try:
        path.relative_to(base_path)
        return True
    except ValueError:
        return False


def _iter_deny_patterns(settings: dict) -> list[str]:
    """Return the string entries of ``settings["permissions"]["deny"]``."""
    perms = settings.get("permissions")
    if not isinstance(perms, dict):
        return []
    deny = perms.get("deny")
    if not isinstance(deny, list):
        return []
    return [d for d in deny if isinstance(d, str)]


def _default_deny_settings_files() -> list[Path]:
    """Production default for :func:`find_blocking_deny_rule`'s
    *settings_files*: the real ``~/.claude/settings.json`` of whoever's
    machine this process runs on.

    Factored into its own function (rather than inlining ``Path.home()`` in
    the signature) so it is a single monkeypatchable seam: every caller that
    doesn't pass an explicit ``settings_files`` — :func:`check_worktree_writable`,
    ``AgentServer.assign()``, and ``coord diagnose --orphan-worktrees`` alike —
    funnels through here. Tests patch this one function (see
    ``tests/conftest.py``'s ``_no_worktree_writable_deny_scan``) instead of
    threading an override through every call site, mirroring how
    ``_no_agent_health_probe`` stubs ``_fetch_agent_advertised_repos`` rather
    than parameterizing every ``dispatch_review`` call.
    """
    return [Path.home() / ".claude" / "settings.json"]


def find_blocking_deny_rule(
    worktree_path: Path, *, settings_files: Iterable[Path] | None = None
) -> str | None:
    """Scan Claude Code settings files for a deny rule blocking *worktree_path*.

    Defaults to scanning just the machine's user-level settings
    (``~/.claude/settings.json``) — the one settings source a worker still
    loads after the ``--setting-sources user`` restriction in
    :func:`default_worker_command`; a checkout-local
    ``.claude/settings.local.json`` can no longer reach a worker at all, so
    it is intentionally not scanned here.

    Returns a human-readable ``"'<pattern>' in <file>"`` message for the
    first matching rule found, or ``None`` when no scanned file carries one
    (including when a file is absent or fails to parse as JSON — this is a
    best-effort advisory check, not a security boundary).
    """
    if settings_files is None:
        settings_files = _default_deny_settings_files()

    worktree_path = worktree_path.expanduser()
    for settings_path in settings_files:
        try:
            raw = settings_path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        for pattern in _iter_deny_patterns(data):
            if _deny_pattern_blocks_path(pattern, worktree_path):
                return f"{pattern!r} in {settings_path}"
    return None


def check_worktree_writable(
    worktree_path: Path, *, settings_files: Iterable[Path] | None = None
) -> str | None:
    """Preflight probe (#1445): can a worker actually write into its own worktree?

    Two checks, catching two different failure classes:

    1. A plain OS-level create/delete probe — catches real filesystem issues
       (read-only mount, wrong ownership, full disk) that would block ANY
       process, Claude Code or not.
    2. :func:`find_blocking_deny_rule` — catches a Claude Code permission
       rule that blocks only Edit/Write *tool calls* while the OS-level
       probe above succeeds fine (the #1445 incident itself).

    Returns ``None`` when both checks are clear, or a human-readable message
    identifying the failure — naming the path and, for a deny-rule hit, the
    exact rule and file — suitable for an assignment error or a `coord
    diagnose` line. Call this **before** spawning a worker into
    *worktree_path*, not after.

    *settings_files* overrides which settings file(s) :func:`find_blocking_deny_rule`
    scans (default: the machine's ``~/.claude/settings.json``) — mainly for
    tests; production callers should leave it unset.
    """
    worktree_path = worktree_path.expanduser()
    try:
        worktree_path.mkdir(parents=True, exist_ok=True)
        probe = worktree_path / f".coord-write-probe-{os.getpid()}"
        probe.write_text("probe", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        return f"cannot write to {worktree_path}: {e}"

    blocked_by = find_blocking_deny_rule(worktree_path, settings_files=settings_files)
    if blocked_by is not None:
        return (
            f"a Claude Code permission rule denies Edit/Write under "
            f"{worktree_path}: {blocked_by}"
        )
    return None


def _claude_md_system_prompt_suffix(repo_path: str) -> str:
    """Return a system-prompt suffix embedding the target repo's CLAUDE.md.

    Worker dispatch below passes ``--setting-sources user`` — **not**
    ``--bare``, which #2462 tried and reverted same-day (see the long
    comment in :func:`default_worker_command`). ``--setting-sources user``
    suppresses Claude Code's own project-level CLAUDE.md auto-discovery
    (along with project/local settings.json) just as thoroughly as
    ``--bare`` would have, for this one mechanism. #2820 measured it
    directly with four controlled probes: an empty dir and a real repo
    checkout, both under ``--setting-sources user``, differ by only ~200
    prompt-framing tokens (22,489 vs 22,686) — essentially no CLAUDE.md
    content leaking through. The same repo under Claude Code's *default*
    setting sources adds ~7,675 tokens (30,361) for the auto-discovered
    file. So this function is **not** defense-in-depth alongside some
    still-running ambient auto-discovery — it is the **only** mechanism
    that delivers the target repo's CLAUDE.md to a work-shaped ``-p`` leg.
    Nothing else in a work-shaped leg's briefing embeds the target repo's
    actual CLAUDE.md text, so without this a worker would silently lose
    every per-repo convention, testing rule, and sealed-path note that
    isn't hardcoded into the repo-agnostic ``WORKER_SYSTEM_PROMPT`` — with
    no error and no test failing to surface the loss.

    Mirrors :func:`coord.review.read_repo_claude_md`, which the review leg
    already uses for the same reason (a review must never run blind to
    CLAUDE.md) — this repo's own CLAUDE.md says it is "loaded into every
    worker leg, every review leg, and every coordinator session", so the
    work-shaped legs need the identical defensive read now that ambient
    discovery is gone. Returns ``""`` (a no-op suffix) when the repo has no
    CLAUDE.md or it can't be read.
    """
    from coord.review import read_repo_claude_md  # noqa: PLC0415

    claude_md = read_repo_claude_md(Path(repo_path).expanduser())
    if not claude_md:
        return ""
    return "\n\n## Project rules (from CLAUDE.md)\n\n" + claude_md.strip() + "\n"


def default_worker_command(spec: AssignmentSpec, *, binary: str = DEFAULT_WORKER_BINARY) -> list[str]:
    """Build the argv for invoking the worker on this assignment.

    Uses ``--output-format stream-json --verbose`` for structured one-event-
    per-line log output that :mod:`coord.worker_events` parses for real-time
    observability.  Also uses ``--input-format stream-json`` so the worker
    reads turn-by-turn user messages from stdin — the orchestrator writes
    the initial briefing as a JSON line in :meth:`AgentServer._spawn`, and
    can later inject additional messages via :meth:`AgentServer.inject_message`.

    For ``type="plan"`` specs the worker gets :data:`WORKER_PLAN_PROMPT` as
    its system prompt and only ``Read,Bash`` in ``--allowedTools`` — no
    Edit/Write tools so it cannot modify the repository.

    For ``type="smoke"`` specs the worker gets ``Read,Bash`` only — no
    Edit/Write, and deliberately no ``Monitor`` (#2301): a smoke leg is a
    one-shot ``claude -p`` session, and ``Monitor`` ends the turn to await a
    notification that can never arrive in time to resume it, which silently
    kills a backgrounded smoke suite mid-run and leaves no verdict printed.
    """
    if spec.type == "plan":
        system_prompt = spec.system_prompt if spec.system_prompt else WORKER_PLAN_PROMPT
        # `--setting-sources user` (below) drops CLAUDE.md auto-discovery; a
        # plan leg still needs the target repo's conventions to plan
        # against. See _claude_md_system_prompt_suffix for what the flag
        # actually does and the measured numbers behind it.
        system_prompt += _claude_md_system_prompt_suffix(spec.repo_path)
        allowed_tools = "Read,Bash"
    elif spec.type == "refinement":
        # #264: refinement is a developer-driven chat for scoping an issue.
        # Read-only — no Edit/Write/Bash, since this session must not mutate
        # the repo or shell out to gh.  The developer drives the conversation
        # via inject_message; the worker just asks clarifying questions.
        system_prompt = spec.system_prompt if spec.system_prompt else REFINEMENT_SYSTEM_PROMPT
        allowed_tools = "Read"
    elif spec.type == "test-chat":
        # #314 Phase B: test-stage chat for validating a completed work
        # assignment.  Allows Read + Bash for read-only diagnostics (builds,
        # tests, lint) but blocks write-side commands via deny_commands.
        system_prompt = spec.system_prompt if spec.system_prompt else TEST_CHAT_SYSTEM_PROMPT
        system_prompt += build_deny_prompt(spec.deny_commands)
        allowed_tools = "Read,Bash"
    elif spec.type == "new-issue-chat":
        # #316: new-issue-chat helps the developer draft a new GitHub issue.
        # Read + Bash allowed (read-only lookups like grep/find/gh issue list);
        # a deny list blocks all mutations (gh issue create, git push, etc.)
        # so the coordinator's TUI handles the actual gh submission.
        system_prompt = spec.system_prompt if spec.system_prompt else NEW_ISSUE_CHAT_SYSTEM_PROMPT
        system_prompt += build_deny_prompt(NEW_ISSUE_CHAT_DENY_COMMANDS)
        # #352: append per-repo new-issue guidance when provided.
        if spec.new_issue_guidance:
            system_prompt += (
                "\n\nThe user's repo has the following guidance for new-issue drafts. "
                "Follow it: ask focused questions matched to the required sections, "
                "then produce a finalised issue body using the same structure. "
                "Do not invent sections that aren't there; do not omit required sections "
                "(mark them `(TBD)` if the conversation hasn't covered them yet).\n\n"
                + spec.new_issue_guidance
            )
        allowed_tools = "Read,Bash"
    elif spec.type == "milestone-chat":
        # #770 (Phase 2 of #767): milestone-steward chat that proposes and
        # (once confirmed) writes the tracking issue's `## Work order`
        # block. Read+Bash so it can run `coord milestone order` /
        # `write-order`; a deny list blocks raw `gh` mutations and
        # unrelated `coord` write commands — see WRITE_CAPABLE_SPEC_TYPES,
        # this is a mutating type unlike the other chats above.
        system_prompt = spec.system_prompt if spec.system_prompt else MILESTONE_CHAT_SYSTEM_PROMPT
        system_prompt += build_deny_prompt(MILESTONE_CHAT_DENY_COMMANDS)
        allowed_tools = "Read,Bash"
    elif spec.type == "decomposition-chat":
        # #2533 (ms-67 contract §4c): pull an approved portal submission
        # into a briefed session that decides oracle-loop-shaped-or-not,
        # files issue(s) via `coord issue create`, queues them via `coord
        # drive-queue add`, and records `coord portal link` — a mutating
        # chat type like milestone-chat above, with its own narrow deny
        # list (see DECOMPOSITION_CHAT_DENY_COMMANDS) rather than
        # new-issue-chat's blanket one, since this session's whole job is
        # to write via those specific `coord` commands.
        system_prompt = (
            spec.system_prompt if spec.system_prompt else DECOMPOSITION_CHAT_SYSTEM_PROMPT
        )
        system_prompt += build_deny_prompt(DECOMPOSITION_CHAT_DENY_COMMANDS)
        allowed_tools = "Read,Bash"
    elif spec.type == "mock-author":
        # #930 (docs/ORACLE_LOOP.md, Gate A): an independent mock-author
        # agent renders a viewable mock + writes `contract.md` under
        # `tests/acceptance/ms-NN/` — the one type allowed to write there
        # (dispatch.py exempts it from the acceptance-dir auto-forbid).
        # Needs Edit/Write (create the mock/contract files) + Bash (commit,
        # push) like a normal worker, but with its own scoped system prompt
        # and deny list instead of the generic WORKER_SYSTEM_PROMPT.
        if spec.system_prompt:
            system_prompt = spec.system_prompt
        else:
            from coord import mock_author  # noqa: PLC0415

            # #3131 review: only mention the `:target` interactive-walkthrough
            # exception to a mock-author session when the flag that gates
            # the technique itself is on — see
            # MOCK_AUTHOR_INTERACTIVE_CARVEOUT's docstring above for why an
            # unconditional mention here is the one artifact that would ship
            # the pre-#314 CSP degrade even while the flag stays off.
            carveout = (
                MOCK_AUTHOR_INTERACTIVE_CARVEOUT
                if mock_author.INTERACTIVE_MOCK_WALKTHROUGHS_ENABLED
                else MOCK_AUTHOR_INTERACTIVE_CARVEOUT_DISABLED
            )
            system_prompt = MOCK_AUTHOR_SYSTEM_PROMPT.replace(
                "‹INTERACTIVE_CARVEOUT›", carveout
            )
        system_prompt += build_deny_prompt(MOCK_AUTHOR_DENY_COMMANDS)
        # `--setting-sources user` (below) drops CLAUDE.md auto-discovery —
        # see _claude_md_system_prompt_suffix.
        system_prompt += _claude_md_system_prompt_suffix(spec.repo_path)
        allowed_tools = "Read,Edit,Write,Bash"
    elif spec.type == "smoke":
        # #2301: smoke gets its own branch instead of falling through to the
        # generic `else` below (which is where it used to land, and where
        # it inherited the #2169 `Monitor` grant meant for *work* legs).
        # A smoke runner's whole job is "pull the branch, run the smoke
        # command, report pass/fail" (see SMOKE_SYSTEM_PROMPT) — it edits
        # nothing and pushes nothing, so it has no business holding
        # Edit/Write either.
        #
        # `Monitor` is deliberately withheld: it is an await-a-notification
        # tool — calling it ends the current turn so the harness can wake
        # the model back up once the condition it's watching fires. That
        # only resumes anything in an INTERACTIVE session. A smoke leg is a
        # one-shot `claude -p` session like every coord leg (#1394): ending
        # the turn ends the session itself, permanently, before any
        # notification can arrive. The agent was observed reaching for
        # `Monitor` via ToolSearch to poll a backgrounded smoke suite, then
        # ending its turn to "wait" — the session (and the backgrounded
        # suite, reaped ~30s later) died silently with no verdict printed,
        # burning a Test dispatch for zero signal every single retry.
        # `BashOutput`/`TaskOutput` (already reachable without an explicit
        # grant — see the #2169 comment above) return synchronously and are
        # the correct way to poll a backgrounded task from here.
        from coord.smoke import SMOKE_SYSTEM_PROMPT  # noqa: PLC0415
        system_prompt = spec.system_prompt if spec.system_prompt else SMOKE_SYSTEM_PROMPT
        system_prompt += build_deny_prompt(spec.deny_commands)
        allowed_tools = "Read,Bash"
    elif spec.type == "review":
        # #2461: give review its own branch instead of falling through to
        # the generic `else` below — see REVIEW_DENY_COMMANDS for the full
        # story. A reviewer reads the diff and reports a verdict; it edits
        # and pushes nothing, so it gets no Edit/Write. `Monitor` is
        # withheld too, for the same #1394/#2301 reason the `smoke` branch
        # above withholds it: a review leg is a one-shot `claude -p`
        # session, and an await-a-notification tool that only resumes an
        # INTERACTIVE session would silently kill it mid-run.
        #
        # A scratch/tmp path outside the worktree (e.g. `/tmp/...`) stays
        # writable for a reviewer that wants to stage its findings before
        # the final `coord report-result --body-file` call — Bash retains
        # ordinary shell redirection (`cat >`, heredocs) even with no Write
        # tool grant; only the Edit/Write *tools* (which target the
        # worktree/repo) and the mutating git/gh commands below are blocked.
        from coord.review import REVIEWER_SYSTEM_PROMPT  # noqa: PLC0415
        system_prompt = spec.system_prompt if spec.system_prompt else REVIEWER_SYSTEM_PROMPT
        system_prompt += build_deny_prompt(REVIEW_DENY_COMMANDS)
        allowed_tools = "Read,Bash"
    else:
        system_prompt = spec.system_prompt if spec.system_prompt else WORKER_SYSTEM_PROMPT
        system_prompt += build_deny_prompt(spec.deny_commands)
        # `--setting-sources user` (below) drops CLAUDE.md auto-discovery
        # for this catch-all branch too — it covers "work", "fix",
        # "conflict-fix", and "test-author", every one of which edits code
        # and needs the target repo's conventions. See
        # _claude_md_system_prompt_suffix.
        system_prompt += _claude_md_system_prompt_suffix(spec.repo_path)
        # #2169: `Monitor` is the sanctioned way to poll a backgrounded
        # long-running command in bounded steps (see the ONE-SHOT section of
        # WORKER_SYSTEM_PROMPT) instead of a foreground loop that blocks
        # past the 600s Bash ceiling. `TaskOutput`/`TaskStop` were already
        # reachable without being in this list; `Monitor` was the one
        # observed denied.
        #
        # #2301: this grant is for *work*-shaped legs (work/review/fix/
        # conflict-fix) whose system prompt (WORKER_SYSTEM_PROMPT, the
        # ONE-SHOT section) explicitly teaches the bounded-poll pattern and
        # explains why an await-a-notification tool is safe to reach for
        # ONLY with that guidance attached. `smoke` used to fall through to
        # this branch and inherit `Monitor` with none of that context — see
        # the dedicated `elif spec.type == "smoke"` branch above, which
        # deliberately withholds it instead.
        allowed_tools = "Read,Edit,Write,Bash,Monitor"

    # NOTE: briefing is NOT passed as a positional arg — it is written to
    # stdin as the first stream-json user message by ``_spawn``.
    argv = [
        binary, "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--system-prompt", system_prompt,
        "--allowedTools", allowed_tools,
        "--permission-mode", "acceptEdits",
        # #1445 / #2462: a worker must not inherit whatever project/local
        # Claude Code settings the host's checkout happens to carry — e.g. a
        # `.claude/settings.local.json` deny rule the OPERATOR added for
        # their own interactive session in that checkout (meant to protect
        # ~/.coord/coordinator.yml etc from an interactive session, but
        # written broadly enough to blanket ~/.coord/** including every
        # worker's own worktree under ~/.coord/worktrees/<id>/). Confirmed
        # empirically: Claude Code resolves a linked git worktree's project
        # settings back to the MAIN checkout even though the worktree itself
        # has no .claude/ dir of its own (it's untracked/gitignored there),
        # so a deny rule that never touches the worktree's working directory
        # still blocked Edit/Write calls from a `claude -p` session cwd'd
        # into that worktree.
        # into that worktree. Restricting to "user" means a worker's
        # permission profile is fully controlled by the --allowedTools /
        # --disallowedTools / --permission-mode flags in this function (and
        # its own hard-coded system prompt), not by the checkout's
        # settings.json / settings.local.json. This is the headless/
        # `claude -p` path only — the human-attended interactive PTY path
        # (coord.providers.claude_pty.ClaudePtyProvider) intentionally keeps
        # the default sources so an operator's own `coord init`-configured
        # convenience allow-list still applies to a session they're
        # attached to and watching. #2821: that tradeoff also means the PTY
        # path pays for CLAUDE.md via ambient auto-discovery (~7.7k tok)
        # instead of the explicit `_claude_md_system_prompt_suffix` embed
        # below (~5.2k tok) — a deliberately accepted, documented cost; see
        # the argv comment in ClaudePtyProvider.build_command.
        #
        # #2462 tried `--bare` here (closes settings + hooks + .mcp.json +
        # CLAUDE.md auto-discovery at once, vs. this flag's settings.json-only
        # scope) but `--bare` also unconditionally disables reading OAuth
        # credentials / the system keychain, and this fleet authenticates
        # headless dispatch via `claude login`/OAuth, not `ANTHROPIC_API_KEY`
        # (see CLAUDE.md: "No API key needed... Max/Pro subscription via
        # OAuth"). It went live and broke every worker dispatch fleet-wide
        # within the hour ("Not logged in · Please run /login", misreported
        # as generic `api_error`) — reverted back to `--setting-sources user`
        # same-day. The hooks/.mcp.json leak `--bare` would have closed was
        # real; #2820 closed the `.mcp.json`/MCP-server half of it below
        # with `--strict-mcp-config`, which has no OAuth side-effect (it
        # only strips project/user-scope MCP server definitions, nothing
        # else `--bare` touched). The hooks half is still open; re-closing
        # it needs an approach that doesn't require `ANTHROPIC_API_KEY`/an
        # `apiKeyHelper`, tracked as a fresh follow-up rather than
        # re-landing `--bare` as-is.
        #
        # `_claude_md_system_prompt_suffix` (see the "plan", "mock-author",
        # and catch-all "else" branches above) explicitly embeds the target
        # repo's CLAUDE.md into --system-prompt — this is NOT defense-in-
        # depth alongside some still-running ambient auto-discovery.
        # #2820 measured that `--setting-sources user` *suppresses* Claude
        # Code's own project CLAUDE.md auto-discovery: a real repo checkout
        # under `--setting-sources user` costs ~22,686 prompt tokens vs.
        # ~30,361 under Claude Code's *default* setting sources — a
        # ~7,675-token gap that is almost entirely the auto-discovered
        # file. So `_claude_md_system_prompt_suffix` is the ONLY mechanism
        # that delivers the target repo's CLAUDE.md to a work-shaped `-p`
        # leg; treating it as redundant and dropping it would silently
        # strip every per-repo convention, with no error and no test
        # failing to surface the loss.
        "--setting-sources", "user",
        # #2820: every `-p` leg otherwise also loads the OPERATOR's
        # personal user-scope MCP servers — observed across the 120 most
        # recent worker sessions as 20 Google Drive/Calendar tool defs plus
        # a Gmail server on 111/120 legs, a smaller subset on 3 more, and 0
        # on the remaining 6 (i.e. non-deterministic: the same dispatch
        # shape gets a different tool surface depending on whether the
        # operator's MCP servers happened to connect by the time the
        # session started). No worker can ever use any of those tools.
        # `--strict-mcp-config` ignores all project/user `.mcp.json`
        # servers for this invocation (with no `--mcp-config` passed, that
        # means none load) — `--setting-sources user` above does NOT cover
        # this, it only gates settings.json. Measured cost of leaving this
        # open is small — two controlled `claude -p` probes with the
        # production flags went from 47 tools / 23,096 prompt tokens
        # without this flag to 27 tools / 22,483 tokens with it, i.e.
        # ~600 tokens, not the several-thousand a raw tool-count drop
        # suggests (MCP tool schemas are deferred behind ToolSearch in
        # current Claude Code, so an unused server costs a name, not a
        # schema). Worth doing anyway: one flag, no OAuth side-effect, and
        # it makes the worker tool surface deterministic — not worth doing
        # as a cost measure on its own.
        "--strict-mcp-config",
    ]
    if spec.model:
        argv.extend(["--model", spec.model])
    # #1315: structural sealing enforcement — see _sealed_write_guard_tools.
    disallowed_tools = _sealed_write_guard_tools(spec.files_forbidden)
    # #1642: block Edit/Write on the shared base checkout for any spec.type
    # that actually gets Edit/Write in --allowedTools — the Read/Bash-only
    # chat types above can't touch files regardless, and adding the guard
    # there would be a no-op cluttering their argv for nothing.
    if "Edit" in allowed_tools:
        for pattern in _base_checkout_write_guard_tools(spec.repo_path):
            if pattern not in disallowed_tools:
                disallowed_tools.append(pattern)
    # #2461: review gets its mutating-command deny list wired into
    # --disallowedTools too, not just the soft system-prompt reminder from
    # build_deny_prompt above — the CLI enforces this one even if the model
    # decides to ignore its own prompt.
    if spec.type == "review":
        for pattern in REVIEW_DENY_COMMANDS:
            if pattern not in disallowed_tools:
                disallowed_tools.append(pattern)
    if disallowed_tools:
        argv.extend(["--disallowedTools", ",".join(disallowed_tools)])
    # #315: when resuming a prior chat session, load the prior conversation so
    # the model has full context.  The briefing field IS the new user message;
    # claude sees it as the next user turn after the restored history.
    if spec.resume_session_id:
        argv.extend(["--resume", spec.resume_session_id])
    return argv


def _user_message_line(text: str) -> bytes:
    """Encode a user message as a single stream-json line (with newline)."""
    payload = {"type": "user", "message": {"role": "user", "content": text}}
    return (json.dumps(payload) + "\n").encode("utf-8")


# #425: assignment types that **mutate** the repo or external state.  The
# safety gate in :meth:`AgentServer.assign` refuses to start these on any
# provider whose ``capabilities().enforces_deny_list`` is False — i.e. a
# provider that has NOT been verified to honour the worker deny-list.
# Non-mutating types (``plan``, ``refinement``, ``test-chat``,
# ``new-issue-chat``) are read-only chats and may run on unverified
# providers without risk. ``milestone-chat`` (#770) is a chat too — no
# git worktree — but it CAN mutate GitHub (the tracking issue body via
# `coord milestone write-order`), so it belongs here, not above.
# ``mock-author`` (#930) gets a real worktree + branch and commits/pushes
# files, same mutation risk as ``work``. ``test-author`` (#931,
# docs/ORACLE_LOOP.md) writes the sealed acceptance suite under
# ``tests/acceptance/`` and pushes a branch — a real git worktree + a
# push, same mutation shape as ``work``/``smoke``/``conflict-fix``, so it
# must be gated the same way. ``pr-helper`` (#1142, see
# ``coord.models.PR_HELPER_TYPE``) is `coord pr`'s PR-opening follow-up for
# a non-closes-issue original (test-author/mock-author/...) — it gets a
# worktree and runs `gh pr create`, the same GitHub-mutating shape as
# `type="work"` had before #1142 gave it its own type, so it must stay here.
# ``decomposition-chat`` (#2533) is a chat too — no git worktree — but its
# whole job is to mutate GitHub (`coord issue create` / `coord milestone
# create`/`add-child`) and the drive queue (`coord drive-queue add`), the
# same "no worktree, but real mutation" shape ``milestone-chat`` has above.
# ``epic-decompose`` (#3132, ``coord.models.EPIC_DECOMPOSE_TYPE``) gets a
# real worktree + branch and commits/pushes the first slice's implementation
# — same mutation shape as ``work``/``mock-author`` — AND files child issues
# / queues them (`coord issue create`, `coord milestone add-child`, `coord
# drive-queue add`), the same GitHub-mutation shape ``decomposition-chat``
# has, so it belongs here on both counts.
WRITE_CAPABLE_SPEC_TYPES: frozenset[str] = frozenset({
    "work",
    "review",
    "smoke",
    "conflict-fix",
    "milestone-chat",
    "decomposition-chat",
    "mock-author",
    "test-author",
    "pr-helper",
    "epic-decompose",
})


def _config_mtime_of(config: "Any | None") -> float | None:
    """mtime of *config*'s backing ``coordinator.yml``, or None (#2299).

    None whenever there is nothing to watch: config-free mode (no config at
    all), thin-client mode (the config came from the daemon's ``GET /config``
    and has no local ``path``), or a file that vanished between load and now.
    Every one of those cases makes :meth:`AgentServer._maybe_reload_config` a
    no-op, which is the correct degradation — an agent with no local file has
    nothing to re-read.
    """
    path = getattr(config, "path", None) if config is not None else None
    if path is None:
        return None
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return None


class AgentServer:
    """Owns assignment state and subprocesses. Thread-safe."""

    def __init__(
        self,
        *,
        machine_name: str,
        capabilities: Iterable[str] = (),
        repos: Iterable[str] = (),
        state_dir: Path | None = None,
        worker_command: WorkerCommandBuilder | None = None,
        repo_paths: dict[str, str] | None = None,
        # RESTART-ONLY (#2299). These two are read by `_spawn` for every
        # worker, but they are *process* tuning (how the daemon forks and how
        # long it waits for first output), not fleet topology — they are NOT
        # refreshed by the config reload below. Changing
        # `concurrency.bash_wrap_spawn` / `concurrency.first_output_timeout`
        # in coordinator.yml still requires `systemctl --user restart
        # coord-agent`, as do the bind host/port (owned by uvicorn, which has
        # already bound the socket by the time any reload could run).
        bash_wrap_spawn: bool = True,
        first_output_timeout: float = _FIRST_OUTPUT_TIMEOUT,
        # #2638: wall-clock (NOT monotonic) ceiling on how long a single
        # leg's process may run — see `_DEFAULT_RUNTIME_CEILING_S`. Same
        # RESTART-ONLY tuning class as the two above: `_spawn`'s reap thread
        # reads it once per leg via `self.runtime_ceiling_s`.
        # `None`/`<= 0` disables it fleet-wide (pre-#2638 behaviour); a
        # per-assignment `AssignmentSpec.runtime_ceiling_s` overrides it.
        runtime_ceiling_s: float | None = _DEFAULT_RUNTIME_CEILING_S,
        # #305: per-repo artifact glob patterns; repo_name → list of globs.
        # Populated from coordinator.yml Repo.artifact_paths at startup.
        artifact_paths: dict[str, list[str]] | None = None,
        # #1323 (fix #3): per-repo pre-stash build command; repo_name → shell
        # command string.  When set for a repo, the command is run via
        # ``/bin/sh -c`` in the worktree BEFORE the artifact glob is evaluated,
        # so the expected binary exists even when the worker only exercised a
        # feature-gated subset of the build (e.g. TUI-only work on a repo that
        # also ships a GUI binary).  Populated from coordinator.yml
        # Repo.build_command at startup.
        build_commands: dict[str, str] | None = None,
        # #425: opt-in provider registry.  Maps provider name → concrete
        # :class:`~coord.providers.base.Provider` instance.  Looked up by
        # :class:`AssignmentSpec.provider`.  When None (or empty), the
        # agent's behaviour is byte-identical to pre-#425: every spawn
        # uses ``self.worker_command`` and routes through the existing
        # ``_spawn`` path.  Only specs with an explicit
        # ``spec.provider`` matching a key in this dict take a different
        # path.
        providers: "dict[str, object] | None" = None,
        # #1445 review: override for the settings file(s)
        # :func:`check_worktree_writable` scans for a blocking deny rule.
        # ``None`` (default) means production behavior — scan the real
        # ``~/.claude/settings.json`` of whoever's machine this runs on.
        # Tests must pass an explicit (e.g. empty) list so the suite's
        # default behavior never depends on the machine's real home-directory
        # settings — mirrors the ``worker_command`` injection seam above and
        # the ``_no_board_service``/``_no_agent_health_probe`` hermeticity
        # fixtures in conftest.py, which exist to prevent exactly this class
        # of bug.
        worktree_writable_settings_files: "Iterable[Path] | None" = None,
        # #1630: the loaded coordinator.yml (or None in config-free mode),
        # kept ONLY so /health's periodic local check run
        # (`_cached_local_health`) can resolve this machine's checkouts the
        # same way `coord health` does (`coord.health.context.build_context`).
        # Never used for dispatch/assignment logic — that stays on
        # `repo_paths`/`capabilities`/`repos` above, exactly as before.
        health_config: "Any | None" = None,
        # #1712: why this agent is running with no config, or None on the
        # normal path.  Published in /health so a *legitimately* config-free
        # ephemeral worker (docs/EPHEMERAL_WORKERS.md — no local
        # coordinator.yml AND no board service) is distinguishable from a
        # machine whose declared capabilities silently vanished.  An empty
        # `capabilities` list on its own cannot tell those apart, which is
        # precisely how #1673 stayed "unexplained".
        config_free_reason: str | None = None,
    ) -> None:
        self.machine_name = machine_name
        self.capabilities = list(capabilities)
        self.repos = list(repos)
        self.repo_paths = dict(repo_paths or {})
        self.artifact_paths: dict[str, list[str]] = dict(artifact_paths or {})
        self.build_commands: dict[str, str] = dict(build_commands or {})
        self.state_dir = Path(
            state_dir if state_dir is not None else sys.modules[__name__].DEFAULT_STATE_DIR
        )
        self.log_dir = self.state_dir / "logs"
        self.state_path = self.state_dir / "agent_state.json"
        self.worker_command = worker_command or default_worker_command
        # Daemon-spawn stall mitigations (#299). bash_wrap_spawn routes the
        # spawn through a transient `bash -c 'exec ...'` parent; the TTFT
        # watchdog kills workers that emit zero output within the timeout.
        self.bash_wrap_spawn = bash_wrap_spawn
        self.first_output_timeout = first_output_timeout
        self.runtime_ceiling_s = runtime_ceiling_s
        # #425: optional provider registry (see constructor docstring).
        # Typed as ``dict[str, object]`` to avoid an import cycle with
        # :mod:`coord.providers` at module load time — concrete instances
        # are duck-typed (``build_command``, ``initial_input``,
        # ``capabilities``, ``env``) at call sites.
        #
        # RESTART-ONLY (#2299): the registry is deliberately NOT refreshed by
        # the config reload. A running worker holds a provider resolved at
        # dispatch time (`_resolve_provider`), and `_reap` re-resolves the
        # SAME spec afterwards to pick a log parser — swapping the registry
        # underneath would let a live session's provider silently change
        # identity mid-flight (retargeting its model / log format), which is
        # exactly the "never mutate state a running worker depends on"
        # invariant this feature is bounded by. Adding or editing a provider
        # still needs a restart.
        self._providers: dict[str, object] = dict(providers or {})
        self._worktree_writable_settings_files = worktree_writable_settings_files
        self._health_config = health_config
        self.config_free_reason = config_free_reason

        # ── #2299: hot config reload ────────────────────────────────────────
        # The agent used to freeze every config-derived field above at process
        # start, so adding a repo to coordinator.yml required `systemctl --user
        # restart coord-agent` on every machine that should serve it — the one
        # action that also kills live workers, which in practice meant waiting
        # for the whole fleet to go quiet before a repo could be onboarded at
        # all. Worse, the skew was silent and asymmetric: `coord config`,
        # `coord status` and `coord assign --dry-run` all read the *file* and
        # reported the repo as supported while `assign()` refused every single
        # dispatch for it.
        #
        # `_maybe_reload_config` closes that, driven off the existing /health
        # poll and the dispatch path (no new timer, no background thread).
        # `_config_mtime` is seeded here from the file we were built from so
        # the first poll after startup is a no-op stat(), not a redundant
        # reparse.
        self._config_mtime: float | None = _config_mtime_of(health_config)
        # Serializes reload attempts so two concurrent /health polls (or a
        # poll racing a dispatch) can't both parse the file and apply
        # overlapping swaps. Held across stat + parse + apply. Lock order is
        # always `_config_reload_lock` → `_lock`; nothing takes them the other
        # way round.
        self._config_reload_lock = threading.Lock()
        # Observability for /health: how many times the on-disk config was
        # successfully re-read into this process, and when. In-memory only —
        # it exists to answer "did this agent pick up my edit?" without an
        # SSH + journalctl, which is the question #2299 was really about.
        self._config_reloads: int = 0
        self._config_reloaded_at: float | None = None

        self._lock = threading.Lock()
        self._assignments: dict[str, AgentAssignment] = {}
        self._processes: dict[str, subprocess.Popen] = {}
        self._threads: dict[str, threading.Thread] = {}
        # #1424: per-assignment lock serializing `_cleanup_worktree`. Both
        # `cancel()` (synchronously, right after marking CANCELLED) and the
        # `_reap` background thread (unblocked by the same SIGTERM-driven
        # process exit, at the end of its own teardown) can reach
        # `_cleanup_worktree` for the SAME assignment. Without serialization
        # the two calls race through `wt_path.exists()` → rescue → `git
        # worktree remove` on the same directory, and one thread's `git add`/
        # `commit` can start after the other has already removed it — a
        # TOCTOU that surfaces as an unhandled `FileNotFoundError`. Created
        # lazily per assignment id; pruned alongside `_assignments` in
        # `_prune_completed_history`.
        self._cleanup_locks: dict[str, threading.Lock] = {}
        # #1424: signals that `_reap` (and therefore `_cleanup_worktree`,
        # which it calls near the end) has fully finished for an assignment.
        # `_reap` flips `assignment.status` out of RUNNING/PENDING partway
        # through — well BEFORE the worktree teardown/WIP-rescue-commit/push
        # that follows it — so a caller (originally `wait_for`, a test
        # helper, but the same trap would bite any real caller) that treats
        # "status is terminal" as "fully done" can observe the assignment
        # before its uncommitted work has actually been rescued onto the
        # branch. One `threading.Event` per assignment id, created when the
        # reap thread is spawned and set in `_reap_guarded`'s `finally` once
        # `_reap` returns (success or exception) — see `wait_for`.
        self._reap_complete: dict[str, threading.Event] = {}

        # #1492: last time `_prune_terminal_advisory` actually ran a GitHub
        # terminality sweep over ADVISORY assignments. 0.0 means "never" so
        # the very first `/status` poll after startup always sweeps once.
        self._last_advisory_terminal_check: float = 0.0

        # Cache for /health worktree_bytes — recomputing it walks every
        # file under ~/.coord/worktrees on every /health call, which is
        # tens or hundreds of thousands of stat syscalls when worktrees
        # contain node_modules / target / etc.  Cache for a few seconds
        # so polling clients don't pin the agent in an rglob.
        self._worktree_bytes_cache: tuple[float, int] | None = None  # (computed_at, bytes)
        self._worktree_bytes_ttl: float = 30.0  # seconds
        # #305: cache for /health artifact_bytes — artifact dirs are smaller
        # than worktrees but still warrant a short TTL to avoid hammering
        # the filesystem on every health poll.
        self._artifact_bytes_cache: tuple[float, int] | None = None  # (computed_at, bytes)
        self._artifact_bytes_ttl: float = 30.0  # seconds

        # #1570 B: cache for /health tool_versions — each probe shells out
        # (`git --version`, `gh --version`, ...), so a naive per-poll probe
        # would spawn a handful of subprocesses on every TUI health tick.
        # Tool versions only change on an upgrade + restart, which resets
        # this cache anyway (fresh process), so a long TTL is safe.
        self._tool_versions_cache: tuple[float, dict] | None = None  # (computed_at, summary)
        self._tool_versions_ttl: float = 300.0  # seconds

        # #1630: cache for /health's "health" block — the H-1 check-registry
        # run against this machine. Running the full registry (disk/worktree/
        # cargo-target/repo-state/... probes, each doing real stat/subprocess
        # work) on every /health poll would repeat #1570 B's mistake at a
        # larger scale, so this is computed "on a timer" the same way
        # `_cached_tool_versions` already is: lazily, the first /health poll
        # after the TTL expires pays for the run and every poll inside the
        # TTL reads the cached report. Default TTL is 5 minutes — checks are
        # about slow-moving headroom (disk, staleness), not something that
        # needs sub-minute freshness, and #1630 explicitly calls out a
        # multi-hour-old check as the failure mode to make visible, not
        # something to eliminate by polling harder.
        self._local_health_cache: tuple[float, dict] | None = None  # (computed_at, payload)
        self._local_health_ttl: float = float(
            os.environ.get("COORD_AGENT_HEALTH_INTERVAL", "300")
        )

        # #1729 (H-6): self-healing graph rebuild, riding the same cached
        # health-check tick as `_local_health_cache` above. `path -> (HEAD
        # sha, reason)` for the last checkout whose automatic `graphify
        # update .` *failed* — guard 3's "once per HEAD, never a retry
        # loop". The 2026-08-02 incident this issue closes out was a
        # rebuild that ran to completion and then lost to graphify's own
        # node-count guard; without this a naive reconciler re-runs the
        # full AST pass every `_local_health_ttl` forever against that
        # same refusal. The reason is kept (not just the sha) so every
        # poll on this HEAD keeps surfacing *why* — not just the one poll
        # that made the attempt — per guard 4's "fail loud". Cleared for a
        # path as soon as a rebuild against it succeeds, so a fresh bout of
        # drift on the same sha (should that ever happen) still gets one
        # fresh attempt rather than being suppressed forever. In-memory
        # only, by design: it resets on agent restart, and #404 means an
        # `/update` self-restart never applies without a manual
        # `systemctl --user restart coord-agent` anyway.
        self._graph_rebuild_failed: dict[str, tuple[str, str]] = {}

        # #1729 fix iteration 1: `/health` is served via
        # `asyncio.to_thread(server.health)` (`coord/agent_app.py`), so
        # concurrent `/health` requests routinely land on separate threads.
        # `_cached_local_health`'s cache check-then-recompute is
        # deliberately unlocked (see its docstring — locking it would
        # serialize /health itself), so once the cache goes stale, every
        # poller that lands during the recompute window independently
        # decides "stale, not yet attempted" and would otherwise each
        # launch its own `graphify update .` against the same checkout —
        # two `update` processes racing to write the same
        # `graphify-out/graph.json`/`manifest.json` is a real corruption
        # vector (guard 4), not just wasted CPU (guard 1). This set of
        # checkout paths with a rebuild currently in flight closes that gap
        # without serializing `/health` itself: whichever thread claims a
        # path first proceeds, every other thread skips that path this poll
        # rather than starting a second `graphify update .`. Guarded by
        # `self._lock`, held only for the add/remove — never across the
        # subprocess call itself, same "never block a dispatch" rule guard 1
        # already follows.
        self._graph_rebuild_in_progress: set[str] = set()

        # #2237 item 7: guard 1 (the idle-gate) skips the whole self-heal
        # pass whenever this machine has a RUNNING assignment. Sensible —
        # a rebuild during a worker's leg fights it for CPU — but it means
        # the BUSIEST machine in the fleet gets the FEWEST heal windows,
        # which is the opposite of where drift accumulates. Nothing recorded
        # that, so "this machine never gets an idle window" was
        # indistinguishable from "this machine never needed a heal".
        # Measure before changing the guard: these two counters ride
        # /health's `graph_self_heal` block so the ratio is observable, and
        # only if it turns out to be common does the guard need replacing
        # with a heal window.
        self._graph_heal_skipped_active: int = 0
        self._graph_heal_last_skip_at: float | None = None
        self._graph_heal_passes: int = 0

        # Skills self-heal (#319 follow-up): `coord install-skills` was a
        # real fix (coord/skills/*/SKILL.md) for a real problem, but was a
        # 100%-manual step nothing in provisioning ever ran — so a skill
        # added or updated in a coordinator release could sit uninstalled on
        # a worker machine indefinitely, unnoticed, the same "silent gap"
        # shape already known from the graphify hooks and the browser
        # capability probe. Rides the same cached health-check tick as the
        # graph self-heal above rather than adding a new timer — syncing a
        # handful of small text files is cheap enough to need none of that
        # pass's guards (no idle-gate, no in-flight dedup, no retry budget).
        # In-memory only, resets on agent restart, same rationale as
        # `_graph_heal_passes` above: it exists to answer "is this machine's
        # skill set current", not to be a durable metric.
        self._skills_heal_passes: int = 0
        self._skills_heal_last_run_at: float | None = None
        self._skills_heal_last_synced: list[dict[str, str]] = []
        self._skills_heal_last_error: str | None = None

        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._load_state()

        # #2936: verify AT STARTUP — not a suite-run later — that a worker
        # this agent spawns will be able to resolve `coord` on its own PATH
        # to record a test/smoke verdict. Only the failure case is logged:
        # this file configures no logging handler of its own (`coord agent`
        # never calls `configure_daemon_logging`, unlike `coord serve` —
        # #2862), so a bare `_log.info` here would be a silent no-op on the
        # real daemon while `_log.warning` reaches `journalctl` via Python's
        # stderr handler-of-last-resort with zero configuration needed.
        _coord_reachable, _coord_reachable_msg = worker_coord_reachable()
        if not _coord_reachable:
            _log.warning(_coord_reachable_msg)

    # ── #2299: hot config reload ────────────────────────────────────────────

    #: Config-derived state that a reload refreshes, versus the state it must
    #: leave alone. Kept as a docstring-adjacent constant so the *decision*
    #: (issue #2299's "needs a decision, not an assumption" list) is recorded
    #: in the code rather than only in the issue thread.
    #:
    #: HOT (refreshed on the next health poll / dispatch):
    #:   * ``repos``          — the point of the feature; gates ``assign()``
    #:                          and is what ``/health`` advertises.
    #:   * ``repo_paths``     — resolved per dispatch.
    #:   * ``artifact_paths`` — read when a finished worker's artifacts are
    #:                          stashed.
    #:   * ``build_commands`` — read just before that stash.
    #:   * ``capabilities``   — published in ``/health``; the coordinator does
    #:                          smoke/review routing from that published list,
    #:                          and nothing in the *worker* path reads it, so a
    #:                          capability that disappears simply stops
    #:                          attracting new work (degrade) instead of
    #:                          stranding anything in flight.
    #:   * ``providers``      — #2326: rebuilt from ``providers.definitions``
    #:                          at DISPATCH time (``assign()``, before
    #:                          ``_resolve_provider``), not on a timer — that
    #:                          is the only moment the answer matters. Before
    #:                          #2326 this dict was parsed once at startup and
    #:                          never invalidated, so an agent with its own
    #:                          local ``coordinator.yml`` silently ran on a
    #:                          days-stale provider definition (wrong model,
    #:                          missing env) even though every coordinator-
    #:                          side surface (``coord config``, the daemon's
    #:                          own ``build_provider(...)``) showed the edit
    #:                          as live. Any provider name in use by a
    #:                          PENDING/RUNNING assignment is pinned to its
    #:                          pre-reload entry (present or absent) —
    #:                          swapping a live worker's provider identity
    #:                          mid-flight (model, env, log-parser shape)
    #:                          remains forbidden; only the NEXT dispatch
    #:                          sees the new definition.
    #:
    #: RESTART-ONLY (documented at their assignment sites in ``__init__``):
    #:   * ``bash_wrap_spawn`` / ``first_output_timeout`` — process tuning.
    #:   * bind host/port     — uvicorn has already bound the socket.
    _RELOADABLE_FIELDS = (
        "repos",
        "repo_paths",
        "artifact_paths",
        "build_commands",
        "capabilities",
        "providers",
    )

    def _maybe_reload_config(self) -> bool:
        """Re-read ``coordinator.yml`` if it changed on disk; return True if applied.

        This is #2299's whole mechanism. It reuses the board daemon's #1081
        helper (:func:`coord.config_reload.reload_config_if_stale`) rather than
        inventing a second one, so the malformed-edit behaviour is identical by
        construction: a bad hand-edit is logged and swallowed, the agent keeps
        running on the last-good config, and the tracked mtime still advances
        so the bad edit is not re-parsed on every subsequent poll.

        Cheap enough to call on the hot paths that already exist (``health()``
        and ``assign()``): the steady-state cost is one ``stat()`` under an
        uncontended lock — no new timer and no background thread.

        Fail-soft in every direction. A missing/thin-client config, a vanished
        file, a malformed edit, or a config whose ``machines:`` list no longer
        contains this machine all leave the agent exactly as it was.
        """
        cfg = self._health_config
        if cfg is None or getattr(cfg, "path", None) is None:
            # Config-free (docs/EPHEMERAL_WORKERS.md) or thin-client mode —
            # there is no local file to watch. Both are legitimate; neither
            # can drift against a file it doesn't have.
            return False

        with self._config_reload_lock:
            # Re-read under the lock: a concurrent caller may already have
            # applied this same edit while we waited, in which case
            # `_config_mtime` has moved past it and the helper no-ops.
            cfg = self._health_config
            reloaded, mtime = reload_config_if_stale(
                cfg,
                self._config_mtime,
                log_name=__name__,
                label="coord agent",
            )
            self._config_mtime = mtime
            if reloaded is cfg:
                return False

            machine = next(
                (m for m in reloaded.machines if m.name == self.machine_name), None
            )
            if machine is None:
                # The edit removed/renamed this machine. Adopting a config
                # that doesn't describe us would mean publishing an empty repo
                # list and refusing every dispatch — a far worse outcome than
                # staying on the last-good snapshot until an operator notices.
                # `_config_mtime` has still advanced, so this logs once per
                # edit rather than once per poll.
                _log.warning(
                    "coord agent: %s reloaded but no longer declares machine "
                    "%r (has: %s); keeping the previous config",
                    reloaded.path,
                    self.machine_name,
                    [m.name for m in reloaded.machines],
                )
                return False

            self._apply_reloaded_config(reloaded, machine)
            return True

    def _rebuild_providers(self, cfg: "Any") -> "dict[str, object] | None":
        """Build a fresh provider registry from *cfg*'s ``providers.definitions``.

        Mirrors the startup-time loop in
        ``coord.commands.agent_ops._resolve_agent_startup`` (``build_provider``
        for every entry), but — unlike startup — must never raise. A typo'd
        ``providers.definitions`` entry reaching this agent via a hot reload
        must not take a running daemon down; ``reload_config_if_stale``
        already applies that same fail-soft contract to a malformed YAML
        edit, and a config that parses fine but names an unknown provider
        ``type`` deserves the identical treatment. Returns ``None`` (rather
        than a partial dict) on any failure, so the caller keeps the
        entire previous registry until the edit is fixed — matching the
        "keep last-good config" behaviour for a bad ``coordinator.yml``.
        """
        from coord.providers import build_provider  # noqa: PLC0415

        fresh: dict[str, object] = {}
        try:
            for name, defn in cfg.providers.definitions.items():
                fresh[name] = build_provider(name, defn, cfg.models)
        except Exception as exc:  # noqa: BLE001 — never let a bad edit kill the agent
            _log.warning(
                "coord agent: %s's providers.definitions failed to rebuild "
                "(%s: %s); keeping the previous provider registry (%s) "
                "until the edit is fixed",
                cfg.path,
                type(exc).__name__,
                exc,
                sorted(self._providers) if self._providers else "none configured",
            )
            return None
        return fresh

    def _apply_reloaded_config(self, cfg: "Any", machine: "Any") -> None:
        """Swap the reloadable fields in place, pinning anything in flight.

        The invariant from #2299: *a reload must never mutate state a running
        worker depends on.* In-flight assignments keep the values they started
        with; the new config governs the next dispatch onward.

        Concretely, any repo with a PENDING or RUNNING assignment is "pinned":
        its ``repo_paths`` / ``artifact_paths`` / ``build_commands`` entries
        keep their pre-reload values (including *absence* — a repo that had no
        build command before must not acquire one halfway through a worker's
        leg, or the pre-stash build would run a command the worker never
        expected). The pin lifts as soon as that assignment reaches a terminal
        state and the next reload runs.

        ``repos`` and ``capabilities`` are replaced wholesale: they gate and
        advertise *new* work only, so removing a repo correctly means "take no
        more dispatches for it", not "abandon the worker already running".

        #2326: ``providers`` gets the exact same pin treatment, keyed by
        provider *name* instead of repo name. A provider name in use by a
        PENDING/RUNNING assignment keeps its pre-reload instance (including
        absence) — ``_reap`` re-resolves the same ``spec`` after the worker
        exits (to pick a log parser), and it must get back the SAME provider
        identity ``assign()``/``_spawn()`` resolved, not one a reload swapped
        underneath it (different model/env/log-parser shape). Any provider
        name with no in-flight assignment is free to pick up the new
        definition immediately — that is the entire point of #2326: the next
        *dispatch*, not the next restart, sees a config edit.

        Every field is *rebound* to a freshly-built list/dict rather than
        mutated in place, so the many unsynchronized readers elsewhere in this
        class (``_servable_repos``, ``_stash_artifacts``, ``assign``, ...) see
        either the whole old value or the whole new one — never a half-updated
        dict.
        """
        new_repos = list(machine.repos)
        new_capabilities = list(machine.capabilities)
        new_repo_paths = dict(machine.repo_paths or {})
        new_artifact_paths: dict[str, list[str]] = {
            r.name: list(r.artifact_paths) for r in cfg.repos if r.artifact_paths
        }
        new_build_commands: dict[str, str] = {
            r.name: r.build_command for r in cfg.repos if r.build_command
        }
        # #2326: built outside the lock — `build_provider` is pure
        # construction (no I/O), but there is no reason to hold `self._lock`
        # across it. `None` means "rebuild failed, keep the old registry
        # verbatim" (see `_rebuild_providers`'s docstring).
        new_providers = self._rebuild_providers(cfg)

        def _pin(old: dict, new: dict, keys: "Iterable[str]") -> None:
            """Restore *old*'s entry for each pinned key — including absence."""
            for key in keys:
                if key in old:
                    new[key] = old[key]
                else:
                    new.pop(key, None)

        with self._lock:
            in_flight = {
                a.spec.repo_name
                for a in self._assignments.values()
                if a.status in (PENDING, RUNNING)
            }
            _pin(self.repo_paths, new_repo_paths, in_flight)
            _pin(self.artifact_paths, new_artifact_paths, in_flight)
            _pin(self.build_commands, new_build_commands, in_flight)

            added = [r for r in new_repos if r not in self.repos]
            removed = [r for r in self.repos if r not in new_repos]
            caps_changed = new_capabilities != self.capabilities

            self.repos = new_repos
            self.capabilities = new_capabilities
            self.repo_paths = new_repo_paths
            self.artifact_paths = new_artifact_paths
            self.build_commands = new_build_commands

            providers_added: list[str] = []
            providers_removed: list[str] = []
            providers_pinned: list[str] = []
            if new_providers is not None:
                in_flight_providers = {
                    a.spec.provider
                    for a in self._assignments.values()
                    if a.status in (PENDING, RUNNING) and a.spec.provider is not None
                }
                old_providers = self._providers
                _pin(old_providers, new_providers, in_flight_providers)
                # Deliberately NOT trying to report "changed vs unchanged" per
                # provider: `build_provider` mints a fresh instance every call
                # and the provider classes carry no value equality, so any such
                # comparison is really just "was it rebuilt", which is always
                # true for every non-pinned entry. Report what IS knowable —
                # the resulting registry, which names appeared/disappeared, and
                # which kept their pre-reload instance because a PENDING/
                # RUNNING assignment pinned them.
                providers_added = [p for p in new_providers if p not in old_providers]
                providers_removed = [p for p in old_providers if p not in new_providers]
                providers_pinned = sorted(
                    p for p in in_flight_providers if p in old_providers
                )
                self._providers = new_providers

            self._health_config = cfg
            self._config_reloads += 1
            self._config_reloaded_at = time.time()

        # The H-1 block in /health is built from `_health_config`'s checkouts,
        # so a stale cache would keep reporting the pre-reload repo set for up
        # to a full TTL after the swap — exactly the skew this issue is about,
        # just moved one layer down.
        self._local_health_cache = None

        _log.info(
            "coord agent: applied config reload from %s — repos=%s "
            "(added=%s removed=%s) capabilities=%s%s%s",
            getattr(cfg, "path", None),
            new_repos,
            added or "[]",
            removed or "[]",
            new_capabilities if caps_changed else "unchanged",
            (
                f" (pinned in-flight repos: {sorted(in_flight)})"
                if in_flight
                else ""
            ),
            (
                # #2326: separate clause so a provider-only edit (the #2321
                # incident this issue describes) shows up even when no repo
                # changed at all.
                f" providers={sorted(self._providers)}"
                f"{f' (added={providers_added})' if providers_added else ''}"
                f"{f' (removed={providers_removed})' if providers_removed else ''}"
                f"{f' (pinned in-flight={providers_pinned})' if providers_pinned else ''}"
                if new_providers is not None
                else " providers=rebuild failed, kept previous registry"
            ),
        )

    # ── Public API ──────────────────────────────────────────────────────────

    def health(self) -> dict:
        # #2299: the health poll IS the reload tick. The coordinator polls
        # every agent's /health continuously, so piggybacking here means a
        # coordinator.yml edit lands within one poll with no new timer, and —
        # critically — the `repos` list published below is the post-reload one,
        # so `coord repo doctor`'s `machines.agent_repo_skew` clears itself
        # instead of instructing an operator to restart a busy agent.
        self._maybe_reload_config()
        with self._lock:
            active = sum(1 for a in self._assignments.values() if a.status == RUNNING)
            completed = sum(
                1
                for a in self._assignments.values()
                if a.status in (DONE, FAILED, CANCELLED, ADVISORY, REFUSED_POLICY)
            )
        worktree_bytes = self._cached_worktree_bytes()
        artifact_bytes = self._cached_artifact_bytes()
        servable_repos, degraded_repos = self._servable_repos()
        # #2299: the coordinator.yml this agent re-reads on every poll, or
        # None when there is nothing local to watch (config-free / thin-client).
        watched_config = getattr(self._health_config, "path", None)
        return {
            "machine": self.machine_name,
            "capabilities": self.capabilities,
            # #1712: None on the normal path; a human-readable reason when
            # this agent came up with no config at all (no local
            # coordinator.yml AND no board service — the ephemeral-worker
            # case).  Lets `coord doctor` say "legitimately config-free"
            # instead of treating every `capabilities: []` the same, and
            # makes the opposite case — config declares capabilities, agent
            # publishes none — a reportable misconfiguration rather than an
            # indistinguishable absence.
            "config_free": self.config_free_reason,
            # #1527: only repos whose `repo_path` actually exists on this
            # machine — a repo this agent cannot serve must not be
            # advertised as servable (the router picks "least loaded"
            # among `repos`, so a stale/missing checkout otherwise turns
            # into silent 400s on every dispatch while `coord status`
            # still shows the machine green). See `degraded` below for why.
            "repos": servable_repos,
            # #1527: repo_name -> reason, for every configured repo that
            # was dropped from `repos` above (no `repo_paths` entry, or the
            # path doesn't exist on disk). Empty dict when nothing is
            # degraded. `coord status` renders this so the operator sees
            # "dellserver: claude-coordinator path missing" instead of a
            # quietly-shrunk fleet.
            "degraded": degraded_repos,
            "active": active,
            "completed": completed,
            # Monotonic-ish stamp of when THIS Python process started.
            # exec_restart replaces the image so this changes across an
            # /update — letting the CLI distinguish "old agent still
            # responding" from "new agent has come back online".
            "agent_started_at": _PROCESS_STARTED_AT,
            # Total disk usage of all git worktrees managed by this agent.
            "worktree_bytes": worktree_bytes,
            # #305: total disk usage of all stashed artifact directories.
            "artifact_bytes": artifact_bytes,
            # #1570 B: resolved versions of the external tools coord shells
            # out to on this machine — baseline (git, gh) plus whatever this
            # machine's declared `capabilities` claim (cargo for rust, GTK4
            # for gtk, ...). Makes version skew observable fleet-wide
            # (`coord doctor`) instead of only discoverable by SSHing in
            # after a mysterious failure, the way #1564's gh skew was.
            "tool_versions": self._cached_tool_versions(),
            # #1630: this machine's own H-1 check-registry results (disk,
            # worktrees, cargo target dirs, repo state, agent venv, ...),
            # cache-refreshed on a timer (see `_local_health_ttl`) rather than
            # computed inline on every poll. Every result — and the block as
            # a whole — carries a `checked_at` epoch-seconds stamp so a
            # renderer (or the daemon aggregating this into board state) can
            # tell "OK, just measured" from "OK, last measured hours ago"
            # instead of the two being indistinguishable. Never absent: even
            # a health engine that raises produces an `unknown`-severity
            # block (`_cached_local_health`'s own try/except) rather than
            # omitting the key, so an old/new client can always find it.
            "health": self._cached_local_health(),
            # #2237 item 7: how often the graph self-heal pass has actually
            # run on this machine, and how often guard 1 (the idle-gate)
            # turned it away because an assignment was RUNNING. The busiest
            # host in the fleet is the one most likely to drift AND the least
            # likely to be idle, so "never healed" and "never needed healing"
            # were indistinguishable until this. Cheap, in-memory, resets on
            # agent restart — it exists to answer "is the idle-gate starving
            # this machine", not to be a durable metric.
            "graph_self_heal": {
                "passes": self._graph_heal_passes,
                "skipped_active": self._graph_heal_skipped_active,
                "last_skip_at": self._graph_heal_last_skip_at,
            },
            # Same idea as `graph_self_heal` above, for the bundled
            # `coord/skills/*/SKILL.md` sync: `coord install-skills` existed
            # as a real fix but was a 100%-manual step nothing in
            # provisioning ever ran, so a skill added in a coordinator
            # release could sit uninstalled on a worker machine
            # indefinitely, unnoticed. `last_synced` names whichever skills
            # actually changed on the most recent pass (empty on the common
            # "already current" tick) so `coord doctor` can show something
            # more useful than a bare pass count.
            "skills_self_heal": {
                "passes": self._skills_heal_passes,
                "last_run_at": self._skills_heal_last_run_at,
                "last_synced": self._skills_heal_last_synced,
                "last_error": self._skills_heal_last_error,
            },
            # #2299: is this agent actually watching a coordinator.yml, and
            # has it picked anything up? `watching` is None for a
            # config-free/thin-client agent (nothing local to re-read), which
            # is the one case where a restart IS still the only way to change
            # the repo list. Lets an operator answer "did my edit land?"
            # from `coord doctor` instead of SSH + journalctl — the whole
            # complaint in #2299 was that the skew was invisible from outside.
            "config_reload": {
                "watching": str(watched_config) if watched_config else None,
                "reloads": self._config_reloads,
                "last_reload_at": self._config_reloaded_at,
                # #2326: this agent's own last-observed mtime of `watching`
                # (seeded from the file at startup, advanced on every
                # reload attempt — including a swallowed bad edit, see
                # `reload_config_if_stale`). `coord status` can diff this
                # against the coordinator's own mtime for the SAME path to
                # flag an agent whose config — and therefore whose
                # `provider_names` below — predates the edit, without
                # needing to probe `/proc/<pid>/environ` on the worker to
                # notice (the failure mode that made #2326 a live-process
                # investigation instead of a one-line `coord status` read).
                # `None` for a config-free/thin-client agent, same as
                # `watching`.
                "config_mtime": self._config_mtime,
                # #2326: the provider names THIS process would actually
                # resolve `spec.provider` against right now — i.e. the keys
                # of `self._providers`, refreshed at dispatch time (see
                # `_apply_reloaded_config`). Lets `coord status` compare this
                # list (or `config_mtime` above) against the coordinator's
                # `providers.definitions` and flag drift directly, instead of
                # every surface short of the worker's own process agreeing
                # while the running registry is actually stale — exactly the
                # split-brain #2326 describes.
                "provider_names": sorted(self._providers) if self._providers else [],
            },
        }

    def _cached_local_health(self) -> dict:
        """Return `/health`'s `health` block: this machine's H-1 report,
        cache-refreshed on `_local_health_ttl` (see its docstring).

        Fail-soft like every other H-1 entry point: a raised exception
        anywhere in the registry (or in building its HealthContext) becomes
        an `unknown`-severity block carrying the error, never a missing key
        or a crashed /health poll.
        """
        now = time.time()
        cached = self._local_health_cache
        if cached is not None and (now - cached[0]) < self._local_health_ttl:
            return cached[1]

        try:
            from coord.health.context import build_context
            from coord.health.registry import run_all

            ctx = build_context(self._health_config, allow_network=False, now=now)
            report = run_all(ctx, scopes=("machine", "checkout"))
            try:
                # #1729 (H-6): best-effort and deliberately its own
                # try/except — a bug in the self-heal pass must never
                # blind this whole health block (the outer except below
                # would turn a perfectly good report into "unknown").
                self._self_heal_stale_graphs(ctx, report)
            except Exception as exc:  # noqa: BLE001 — self-heal is best-effort
                _log.warning("graph self-heal pass failed: %s", exc)
            try:
                # Same isolation as the graph pass above: a skills-sync bug
                # must never blind this whole health block.
                self._self_heal_missing_skills()
            except Exception as exc:  # noqa: BLE001 — self-heal is best-effort
                _log.warning("skills self-heal pass failed: %s", exc)
            report_dict = report.to_dict()
            payload = {
                "schema": report_dict["schema"],
                "checked_at": now,
                "severity": report_dict["severity"],
                "counts": report_dict["counts"],
                "skipped": report_dict["skipped"],
                # #1630: "per-check timestamp" — each result also carries the
                # batch's `checked_at` (every check in one run shares a
                # measurement time; this is a wire-layer addition, not a
                # change to CheckResult.to_dict()'s own contract, which
                # coord.health.cli's exact-key-set tests pin down).
                "results": [
                    {**r, "checked_at": now} for r in report_dict["results"]
                ],
            }
        except Exception as exc:  # noqa: BLE001 — fail soft, never break /health
            payload = {
                "schema": 1,
                "checked_at": now,
                "severity": "unknown",
                "counts": {},
                "skipped": [],
                "results": [],
                "error": f"{type(exc).__name__}: {exc}",
            }

        self._local_health_cache = (now, payload)
        return payload

    def _self_heal_stale_graphs(self, ctx: Any, report: Any) -> None:
        """React to the ``graph`` check's STATE verdict instead of chasing
        the git events that produced it (#1729, H-6).

        Covers a **missing** graph as well as a stale one (#2237 item 5).
        Ongoing drift was already handled here; absence was not, because
        ``graph_status`` returns early for a checkout with no
        ``graphify-out/graph.json`` and never computes ``stale`` — so this
        pass skipped it, and so did the graphify hooks' own
        ``[ ! -f graphify-out/graph.json ] && exit 0``. Two independent
        mechanisms declining to rebuild a graph that is not there made a
        single ``rm -rf graphify-out/`` (or a fresh clone on a machine that
        never had one) permanent until a human intervened. The guards below
        are unchanged and cover the absent case as-is — in particular guard
        3, which is what stops a repo where ``graphify update .`` genuinely
        cannot succeed from retrying every poll forever.

        The git hooks (`.githooks/`) are event-driven and structurally
        cannot cover every ref-moving operation — rebase/merge/cherry-pick
        `exit 0`, `git reset --hard` fires no hook at all, and every hook
        failure path is a silent `exit 0` behind a detached background
        process (see `coord.graph_health`'s module docstring). The `stale`
        predicate H-5 already computes, by contrast, is total: it compares
        the graph's stamp against HEAD and does not care how the drift
        happened. So this runs on the same TTL tick as the rest of the
        cached local-health report (`_cached_local_health`) and, for every
        `graph`-check result the just-completed *report* calls stale, runs
        `graphify update .` right there — subject to four load-bearing
        guards:

        1. **Idle-gate.** Only when this machine has no RUNNING assignment.
           A rebuild is hundreds of files across many workers; running it
           while a worker is mid-build steals the CPU that worker was
           dispatched for. Every skip is counted
           (`_graph_heal_skipped_active`, surfaced as /health's
           `graph_self_heal` block) because the guard's cost lands hardest
           on the busiest machine — the one most likely to drift — and
           #2237 item 7 asks for that to be measured before the guard is
           traded for a heal window. This reads `self._assignments` under a
           brief lock and releases it before the (possibly slow) rebuild runs —
           load-bearing for guard-adjacent requirement #1625 decision 3:
           health must stay advisory, so a dispatch landing mid-rebuild
           must never be delayed by it (see `_graphify_update`, called
           with the lock already released).
        2. **Base checkouts only.** Never a linked worktree — its
           `graphify-out/` entries are symlinks to the shared base graph
           (a rebuild there would clobber the base graph from a
           feature-branch tree), and the worktree can be reaped mid-
           rebuild. Belt (`values["is_symlink"]`, the graph-level signal)
           and suspenders (`_is_linked_worktree`, ported straight from
           `.githooks/_lib.sh`'s `gfy_is_linked_worktree`).
        3. **Once per HEAD, never a retry loop.** `_graph_rebuild_failed`
           remembers the last HEAD a rebuild failed against (and why), per
           checkout path; this tick skips re-running it until HEAD moves,
           while still surfacing the same reason every poll. This is the guard
           the 2026-08-02 incident demands — a reconciler that just
           retries on failure re-runs a full AST pass every tick forever
           against the same node-count refusal.
        4. **Fail loud, never `--force`.** A refusal or error rewrites this
           checkout's `graph` result to WARN with the real reason attached
           (`report.results` is mutated in place, so this same /health
           poll reflects it — H-4, #1631, is what an operator actually
           reads) — never re-run with `--force`, which exists to defeat
           the very node-count guard that caused the 2026-08-02 incident.

        Mutates *report* in place: a checkout that rebuilds successfully
        gets its `graph` result replaced with a freshly-probed one (so a
        poll that triggers the fix also reports it fixed, rather than
        "stale" for one more TTL window); a checkout whose rebuild fails
        gets the WARN replacement described above. Never raises — the
        caller (`_cached_local_health`) wraps this in its own try/except
        so a bug here can never blind the rest of the health block.

        Concurrent-poller note (#1729 fix iteration 1): `_cached_local_health`
        recomputes without a lock, so more than one thread can reach this
        method at once, each having independently decided the same checkout
        is stale and unattempted. `self._graph_rebuild_in_progress` (see its
        docstring in `__init__`) is what stops that from becoming two
        concurrent `graphify update .` runs against one checkout — claimed
        right before the subprocess call, released in a `finally` around it.

        The per-checkout loop below is sequential: a slow or hung
        `graphify update .` (up to `_graphify_update`'s 600s timeout) on one
        stale checkout delays this poll's report for every other stale
        checkout considered after it. Acceptable for the common
        one-repo-per-machine case this issue targets; a multi-repo machine
        with more than one simultaneously-stale checkout would notice one
        checkout's rebuild holding up another's — worth revisiting (e.g.
        running rebuilds concurrently, bounded by the in-progress set above)
        if that setup becomes common.
        """
        with self._lock:
            active = sum(1 for a in self._assignments.values() if a.status == RUNNING)
        if active:
            # #2237 item 7: count the skip instead of returning silently, so
            # "the busiest machine never heals" is a number an operator can
            # read off /health rather than an inference from a graph that
            # stays stale.
            self._graph_heal_skipped_active += 1
            self._graph_heal_last_skip_at = time.time()
            return
        self._graph_heal_passes += 1

        for i, result in enumerate(report.results):
            if result.check_id != "graph":
                continue
            values = result.values or {}
            # #2237 item 5: ABSENT counts, not just stale. `graph_status`
            # returns early with `unknown_reason` when there is no
            # graphify-out/graph.json, so `stale` is never even computed for a
            # never-built (or `rm -rf`'d) checkout — which meant two
            # independent mechanisms declined to rebuild a graph that is not
            # there: this gate, and the graphify hooks' own
            # `[ ! -f graphify-out/graph.json ] && exit 0`. Between them, a
            # single `rm -rf graphify-out/` was permanent. Guard 3's
            # once-per-HEAD bookkeeping below keys on `head_sha`, which a
            # never-built checkout now carries (#2237 in `graph_health`), so a
            # repo where the build genuinely cannot succeed still gets exactly
            # one attempt per HEAD rather than one per poll.
            if not values.get("stale") and values.get("present", True):
                continue
            path_str = values.get("path")
            if not path_str:
                continue
            repo_path = Path(path_str)

            # Guard 2.
            if values.get("is_symlink") or _is_linked_worktree(repo_path):
                continue

            head_sha = values.get("head_sha")
            # Guard 3. A HEAD this checkout already failed against keeps
            # surfacing that failure (guard 4) on every poll, not just the
            # one that made the attempt — but never attempts again until
            # HEAD moves.
            previously_failed = self._graph_rebuild_failed.get(path_str)
            if head_sha and previously_failed and previously_failed[0] == head_sha:
                report.results[i] = self._graph_failure_result(result, values, previously_failed[1])
                continue

            # Concurrent-poller guard (see this method's docstring and
            # `self._graph_rebuild_in_progress`'s docstring in `__init__`):
            # claim this path before starting the subprocess, release it
            # unconditionally afterwards. A thread that finds the path
            # already claimed skips it this poll rather than launching a
            # second `graphify update .` against the same checkout — the
            # lock is held only for the set add/remove, never across
            # `_graphify_update` itself, so this never blocks a dispatch.
            with self._lock:
                if path_str in self._graph_rebuild_in_progress:
                    continue
                self._graph_rebuild_in_progress.add(path_str)

            try:
                ok, detail = _graphify_update(repo_path)
            finally:
                with self._lock:
                    self._graph_rebuild_in_progress.discard(path_str)

            if not ok:
                # Guard 4.
                if head_sha:
                    self._graph_rebuild_failed[path_str] = (head_sha, detail)
                report.results[i] = self._graph_failure_result(result, values, detail)
                continue

            # Success: HEAD hasn't moved, so any earlier failure recorded
            # against it no longer applies.
            self._graph_rebuild_failed.pop(path_str, None)

            from coord.health.registry import run_all as _run_graph_only  # noqa: PLC0415

            # Probe only the checkout that just healed, not every checkout
            # `ctx` knows about — the check itself is cheap, but there's no
            # reason to re-run it fleet-wide just to pick one result out.
            healed_ctx = replace(
                ctx, checkouts=tuple(c for c in ctx.checkouts if str(c.path) == path_str)
            )
            refreshed = _run_graph_only(healed_ctx, scopes=("checkout",), only=("graph",))
            replacement = next(
                (r for r in refreshed.results if (r.values or {}).get("path") == path_str),
                None,
            )
            if replacement is not None:
                report.results[i] = replacement

    def _self_heal_missing_skills(self) -> None:
        """Sync bundled `coord/skills/*/SKILL.md` into ``~/.claude/skills/``
        on this machine, riding the same cached health-check tick as
        `_self_heal_stale_graphs` above.

        `coord install-skills` (#319) was a real fix for a real problem, but
        it was a 100%-manual step — nothing in agent provisioning or this
        health tick ever ran it, so a skill added or updated in a
        coordinator release could sit uninstalled on a worker machine
        indefinitely, unnoticed. That is the same "silent gap" shape as the
        graphify hooks and the browser-capability probe this codebase
        already knows about.

        Unlike the graph rebuild, this needs none of that pass's four
        guards: syncing a handful of small text files is cheap file I/O, not
        a CPU-heavy subprocess, so there is no idle-gate, no in-flight
        dedup, and no retry budget — `sync_bundled_skills` is already
        idempotent (content-identical files are left untouched, so a
        no-op tick costs a handful of reads, nothing more).
        """
        from coord.commands.setup import (  # noqa: PLC0415
            list_bundled_skill_dirs,
            sync_bundled_skills,
        )

        target_root = Path.home() / ".claude" / "skills"
        try:
            skill_dirs = list_bundled_skill_dirs()
        except Exception as exc:  # noqa: BLE001 — recorded, never raised further
            self._skills_heal_last_error = f"{type(exc).__name__}: {exc}"
            return

        self._skills_heal_passes += 1
        self._skills_heal_last_run_at = time.time()
        self._skills_heal_last_error = None
        if not skill_dirs:
            self._skills_heal_last_synced = []
            return

        target_root.mkdir(parents=True, exist_ok=True)
        results = sync_bundled_skills(target_root, skill_dirs)
        changed = [
            {"skill": name, "action": action}
            for name, action in results
            if action != "unchanged"
        ]
        self._skills_heal_last_synced = changed
        if changed:
            _log.info(
                "skills self-heal: %s",
                ", ".join(f"{c['skill']} {c['action']}" for c in changed),
            )

    @staticmethod
    def _graph_failure_result(result: Any, values: dict, detail: str) -> Any:
        """Guard 4's WARN replacement, shared by the attempt and the
        already-attempted-this-HEAD skip path — so an operator reading H-4
        (#1631) sees the same reason on the poll that tried the rebuild and
        on every poll after it, until HEAD moves.  Never `--force`: the
        fix-by-hand suggestion below is the same plain command the agent
        itself just ran.
        """
        from coord.health.models import Severity  # noqa: PLC0415

        return replace(
            result,
            severity=Severity.WARN,
            headroom=f"self-heal failed: {detail}",
            detail=(
                f"agent's automatic `graphify update .` refused/failed "
                f"— {detail}. Fix by hand: graphify update {values.get('path')}"
            ),
            values={**values, "self_heal_failed_reason": detail},
        )

    def _cached_tool_versions(self) -> dict:
        """Return `/health`'s `tool_versions` with a long TTL cache.

        See `_tool_versions_ttl` for why: probing shells out per tool, and
        `/health` is polled frequently.

        #2913: `probe_all()` normally restricts probing to `self.capabilities`
        so a plain CLI-only machine never pays to probe a browser or GTK4 it
        never claimed. A config-free agent's `self.capabilities` is `[]` by
        construction — it has nothing of its own to declare; the coordinator
        supplies capabilities from its OWN `coordinator.yml` at dispatch time
        (docs/EPHEMERAL_WORKERS.md). Probing only `self.capabilities` there
        means `/health.tool_versions` never covers cargo, python3, or any
        other capability-gated tool, so `unmet_capabilities()` — the #1570 D
        cross-check `dispatch_smoke` relies on before routing capability-
        gated work here — finds nothing to compare and fails OPEN, not
        closed, exactly backwards from what #1570 D exists to guarantee.
        Probe every known capability instead for a config-free agent: it has
        no legitimate narrower set to restrict to anyway, since it doesn't
        know what the coordinator is about to route to it.
        """
        now = time.time()
        cached = self._tool_versions_cache
        if cached is not None and (now - cached[0]) < self._tool_versions_ttl:
            return cached[1]
        from coord.prereqs import ALL_CAPABILITY_NAMES, probe_all, tool_versions_summary

        probe_caps = ALL_CAPABILITY_NAMES if self.config_free_reason is not None else self.capabilities
        summary = tool_versions_summary(probe_all(probe_caps))
        self._tool_versions_cache = (now, summary)
        return summary

    def _cached_worktree_bytes(self) -> int:
        """Return total worktree disk usage with a short TTL cache.

        Recomputing on every /health call is too expensive — a real worktree
        with ``node_modules`` / ``target`` / build outputs can need hundreds
        of thousands of stat syscalls per call, and the TUI polls /health
        with a 2 s timeout (see ``tui/src/app.rs`` health refresh).  A short
        TTL keeps the number trustworthy without pinning the agent in an
        rglob.
        """
        worktree_base = self.state_dir / "worktrees"
        now = time.time()
        cached = self._worktree_bytes_cache
        if cached is not None and (now - cached[0]) < self._worktree_bytes_ttl:
            return cached[1]
        size = _dir_size(worktree_base)
        # Single-writer assignment is atomic in CPython; no lock needed.
        self._worktree_bytes_cache = (now, size)
        return size

    def _cached_artifact_bytes(self) -> int:
        """Return total artifact disk usage with a short TTL cache.

        Mirrors :meth:`_cached_worktree_bytes` — keeps /health polling
        cheap even when artifacts accumulate many small files.
        """
        artifacts_base = self.state_dir / "artifacts"
        now = time.time()
        cached = self._artifact_bytes_cache
        if cached is not None and (now - cached[0]) < self._artifact_bytes_ttl:
            return cached[1]
        size = _dir_size(artifacts_base)
        self._artifact_bytes_cache = (now, size)
        return size

    def _stash_artifacts(self, assignment: AgentAssignment) -> list[str]:
        """Copy build artifacts from a worktree into the persistent stash.

        Called immediately before worktree removal so the compiled outputs
        survive the cleanup.  Only acts on DONE assignments (successful
        workers) with a recorded branch and at least one configured glob
        pattern for the repo.

        Delegates to the module-level :func:`stash_artifacts_for_branch`
        (#562) so the same logic is reachable from the interactive finalize
        path without importing the full AgentServer graph.

        #982: this is the one call site reached by a normal headless
        ``coord assign`` Work dispatch — ``_dispatch_headless`` sends the
        repo's full ``artifact_paths`` glob unmodified in the ``/assign``
        payload, and that glob can't be narrowed any earlier because the
        worker doesn't emit its ``SMOKE_TESTS:`` block until the very end of
        its own session.  By the time this method runs (the DONE
        transition, after the subprocess has exited) that block is already
        in ``assignment.log_path`` on this same host, so narrow the resolved
        pattern list against it before stashing.  This is what actually
        shrinks the *first* stash for a repo like quadraui, where a broad
        ``target/debug/examples/tui_*`` glob matched ~72 example binaries
        (issue-406/issue-411, 7.2 GB / 5.2 GB stashes).

        #1323 (fix #3): when a ``build_command`` is configured for this repo,
        it is run in the worktree BEFORE the artifact glob is evaluated so
        the expected binary exists regardless of which feature flags the
        worker itself used during development.

        Returns a (possibly empty) list of glob patterns that matched 0 files
        on disk so the caller can record a per-glob diagnostic (#1323,
        diagnostic-only as of #1357 — it no longer affects `status`).
        """
        if assignment.status != DONE:
            return []
        if not assignment.worktree_path:
            return []
        repo_name = assignment.spec.repo_name
        patterns = assignment.spec.artifact_paths or self.artifact_paths.get(repo_name, [])
        if not patterns:
            return []
        branch = assignment.branch or assignment.spec.branch
        if not branch:
            return []

        wt_path = Path(assignment.worktree_path)

        # #1323 fix #3: run build_command before globbing so the artifact
        # exists even when the worker's own dev loop only built a subset.
        build_cmd = self.build_commands.get(repo_name)
        if build_cmd and wt_path.exists():
            _run_pre_stash_build(build_cmd, wt_path, assignment.log_path)

        smoke_tests: list[str] | None = None
        if assignment.log_path:
            from coord.progress import parse_smoke_tests_from_log  # noqa: PLC0415

            smoke_tests = parse_smoke_tests_from_log(assignment.log_path)
        patterns = narrow_artifact_paths(
            patterns, smoke_tests, worktree=wt_path
        )

        unmatched: list[str] = []
        copied = stash_artifacts_for_branch(
            worktree_path=wt_path,
            branch=branch,
            repo_name=repo_name,
            patterns=patterns,
            state_dir=self.state_dir,
            assignment_id=assignment.id,
            log_path=assignment.log_path,
            unmatched_out=unmatched,
        )

        # Invalidate the artifact_bytes cache so health() picks up the new files.
        if copied > 0:
            self._artifact_bytes_cache = None

        return unmatched

    def _stash_orphaned_worktree(self, entry: Path, assignment_id: str) -> None:
        """Best-effort stash for a worktree with no assignment record (#1295).

        :meth:`clean_worktrees` reaches this when ``a is None`` — the tmux
        and ``protect`` guards already ruled out a session that is *still*
        alive, and this is the narrower remaining case: an interactive
        session that ended without ever running finalize (crash, ``tmux
        kill-session``, a network drop before ``coord done``).  There is no
        ``AgentAssignment`` to hand to :meth:`_stash_artifacts`, so the
        branch and repo are derived straight from git on *entry* itself:

        - The repo is identified by resolving ``git rev-parse
          --git-common-dir`` (the linked worktree's shared ``.git``) back
          to one of ``self.repo_paths``' configured checkouts.
        - The branch is ``git rev-parse --abbrev-ref HEAD``; a detached
          HEAD (bare ``"HEAD"``) has no stable name to stash under and is
          skipped.

        Silently no-ops when *entry* isn't a git worktree at all (plain
        orphaned directories, as created by tests / races), when its repo
        isn't one this agent knows about, or when the repo has no
        ``artifact_paths`` configured — none of those are errors worth
        logging. Never raises: this runs inline in the hourly sweep and
        must not abort cleanup of the remaining entries.
        """
        try:
            common_dir = _git(entry, "rev-parse", "--git-common-dir")
        except (_GitError, FileNotFoundError, OSError):
            return
        try:
            repo_root = (entry / common_dir).resolve().parent
        except OSError:
            return

        repo_name: str | None = None
        for name, path_str in self.repo_paths.items():
            try:
                if Path(path_str).expanduser().resolve() == repo_root:
                    repo_name = name
                    break
            except OSError:
                continue
        if repo_name is None:
            return

        patterns = self.artifact_paths.get(repo_name, [])
        if not patterns:
            return

        try:
            branch = _git(entry, "rev-parse", "--abbrev-ref", "HEAD")
        except (_GitError, FileNotFoundError, OSError):
            return
        if not branch or branch == "HEAD":
            # Detached HEAD — no stable branch name to stash artifacts under.
            return

        try:
            copied = stash_artifacts_for_branch(
                worktree_path=entry,
                branch=branch,
                repo_name=repo_name,
                patterns=patterns,
                state_dir=self.state_dir,
                assignment_id=assignment_id,
            )
        except Exception:  # noqa: BLE001 — never abort the sweep
            _log.warning(
                "clean_worktrees: best-effort stash of orphaned worktree "
                "%s (repo=%s branch=%s) failed",
                entry, repo_name, branch, exc_info=True,
            )
        else:
            if copied > 0:
                self._artifact_bytes_cache = None

    def _gc_artifacts(self, ttl_days: float = 3.0) -> int:
        """Remove artifact stash directories older than *ttl_days* days.

        Uses the stash directory's ``mtime`` as the age proxy — each
        successful ``_stash_artifacts`` call touches the stash directory
        explicitly after copying, so the TTL is effectively a "last-written"
        window even when re-stashing an existing branch.

        Returns the count of directories removed.
        """
        artifacts_base = self.state_dir / "artifacts"
        if not artifacts_base.exists():
            return 0

        cutoff = time.time() - ttl_days * 86400
        removed = 0

        try:
            repo_dirs = list(artifacts_base.iterdir())
        except OSError:
            return 0

        for repo_dir in repo_dirs:
            if not repo_dir.is_dir():
                continue
            try:
                branch_dirs = list(repo_dir.iterdir())
            except OSError:
                continue
            for branch_dir in branch_dirs:
                if not branch_dir.is_dir():
                    continue
                try:
                    mtime = branch_dir.stat().st_mtime
                except OSError:
                    continue
                if mtime < cutoff:
                    try:
                        shutil.rmtree(branch_dir, ignore_errors=True)
                        removed += 1
                    except OSError:
                        pass

        if removed:
            # Invalidate the artifact_bytes cache after GC.
            self._artifact_bytes_cache = None

        return removed

    # Accepted character set for HTTP path parameters forwarded to the
    # filesystem.  Must not contain ``..``, ``/``, or any shell-special
    # characters.  Both repo names and sanitized branch names satisfy this
    # pattern in practice.
    _SAFE_PATH_COMPONENT = re.compile(r"^[a-zA-Z0-9._-]+$")

    def _find_live_worktree(self, repo: str, branch: str) -> Path | None:
        """Locate a live git worktree of *repo* still checked out to *branch* (#914).

        Reads ``git worktree list --porcelain`` on the repo's main checkout
        (``self.repo_paths[repo]``) — the authoritative source for which
        worktrees belong to this repo and what branch each has checked out —
        rather than guessing from directory names under
        ``state_dir/worktrees/``. *branch* is compared in its sanitized form
        (matching the manifest endpoint's path component), so
        ``refs/heads/issue-914-foo`` and the already-sanitized ``branch``
        argument line up regardless of slashes.

        Returns the worktree path, or ``None`` when the repo is unknown to
        this agent, git fails, or no worktree matches.
        """
        repo_path_str = self.repo_paths.get(repo)
        if not repo_path_str:
            return None
        try:
            output = _git(Path(repo_path_str).expanduser(), "worktree", "list", "--porcelain")
        except (_GitError, FileNotFoundError, OSError):
            return None

        current_path: Path | None = None
        for line in output.splitlines():
            if line.startswith("worktree "):
                current_path = Path(line[len("worktree "):])
            elif line.startswith("branch ") and current_path is not None:
                ref = line[len("branch "):]
                name = ref.removeprefix("refs/heads/")
                if _sanitize_branch(name) == branch:
                    return current_path
        return None

    @staticmethod
    def _tmux_session_alive(assignment_id: str) -> bool:
        """Return True when a ``coord-<assignment_id>`` tmux session is
        genuinely still in use — alive AND its pane is not dead.

        Load-bearing guard for :meth:`clean_worktrees` (#1295): an
        interactive Test/Review/Merge/Work pane keeps its tmux session
        alive for as long as the operator is attached, but the
        ``AgentAssignment`` record can already be in a terminal state
        (or absent, if the agent restarted).  Consulting tmux directly
        is the only reliable "someone is still using this worktree"
        signal.

        Delegates to :func:`coord.interactive.tmux_session_running` (a bare
        ``tmux has-session`` alone is NOT enough — #2541 set
        ``remain-on-exit on`` on every freshly-created coord tmux session so
        a crashed pane's screen stays inspectable, which means
        ``has-session`` now stays ``True`` after ANY pane exit, clean
        success or crash, until a reaper notices and kills it. Without the
        pane-dead check this guard would keep protecting — and thus never
        cleaning up — a worktree whose interactive session has already
        finished, for as long as the now-longer-lived dead session
        lingers). When ``tmux`` is not installed / not running / the
        subprocess errors, we return ``False`` — "no live session, keep
        sweeping" — rather than raising, so one broken tmux install
        never aborts a fleet sweep across the rest of an agent's
        worktrees.
        """
        # Deferred import to keep AgentServer's top-level import graph
        # free of coord.interactive (which pulls in curses/tty helpers
        # the agent process may not want at import time).
        try:
            from coord.interactive import (  # noqa: PLC0415
                tmux_available,
                tmux_session_name,
                tmux_session_running,
            )
        except Exception:  # noqa: BLE001 — defensive; module missing → no guard
            return False
        try:
            if not tmux_available():
                return False
            return tmux_session_running(tmux_session_name(assignment_id))
        except Exception:  # noqa: BLE001 — never propagate out of the sweep
            return False

    @staticmethod
    def _stash_has_content(stash_dir: Path) -> bool:
        """True when *stash_dir* holds at least one real (non-dotfile) file.

        ``stash_artifacts_for_branch`` calls ``mkdir(parents=True,
        exist_ok=True)`` unconditionally, before it knows whether any file
        actually matched a glob pattern (#914 review) — so a bare
        ``stash_dir.exists()`` is true even for a stash that copied zero
        files. Checking for real content instead of mere directory
        existence keeps ``artifact_manifest`` from treating an empty,
        just-created directory as "stash present" (which would return a
        misleading 200 with an empty file list) and keeps the lazy-stash
        retry from self-poisoning: as long as no real content lands, later
        calls keep re-attempting the stash instead of short-circuiting on
        the empty directory forever (until the 3-day GC evicts it).
        """
        if not stash_dir.exists():
            return False
        try:
            return any(
                f.is_file() and not f.name.startswith(".") for f in stash_dir.iterdir()
            )
        except OSError:
            return False

    def artifact_absence_reason(self, repo: str, branch: str) -> str:
        """Ground-truth explanation for why a stash is missing (#914).

        Called by the manifest 404 path once :meth:`artifact_manifest` has
        already tried (and failed) to lazily stash from a live worktree.
        Distinguishes "built but never stashed" — a live worktree for
        *branch* still exists on this host, so the interactive session's
        finalize/``coord report-result`` simply never ran (crash, tmux
        killed, ``coord done`` skipped) — from genuinely absent, so
        ``coord pull-artifact`` stops blaming GC / glob mismatches / missing
        config when none of those is the real cause.
        """
        if (
            not self._SAFE_PATH_COMPONENT.match(repo)
            or not self._SAFE_PATH_COMPONENT.match(branch)
        ):
            return "invalid repo/branch name"
        wt = self._find_live_worktree(repo, branch)
        if wt is not None and wt.exists():
            if not self.artifact_paths.get(repo):
                return (
                    f"a live worktree for branch {branch!r} exists on this host "
                    f"({wt}), but repo {repo!r} has no artifact_paths configured "
                    "— there is nothing to stash."
                )
            return (
                f"a live worktree for branch {branch!r} exists on this host "
                f"({wt}), but the build did not produce any files matching "
                "artifact_paths — the session likely wasn't finalized (crash, "
                "tmux killed, or `coord done` never ran), and re-running the "
                "build or checking the artifact_paths globs may help."
            )
        # #1295 fix item #5: distinguish "a stash directory exists but is
        # empty" from "no stash directory at all".  An empty directory
        # means some stash attempt DID run (the worker path, the
        # interactive finalize path, or the sweep's own best-effort stash
        # of an orphaned worktree) and found 0 matching files — that's a
        # config/build problem (`artifact_paths` doesn't match what got
        # built), not a "nothing ever ran" problem, and the two point the
        # operator in very different directions.
        stash_dir = self.state_dir / "artifacts" / repo / branch
        if stash_dir.exists() and not self._stash_has_content(stash_dir):
            return (
                f"the stash directory for branch {branch!r} exists "
                f"({stash_dir}) but is empty — a stash was attempted (by a "
                "worker's DONE transition, an interactive finalize, or the "
                "hourly worktree sweep) but 0 files matched artifact_paths. "
                f"Check that artifact_paths for repo {repo!r} actually "
                "matches what the build produces, and look for a `stash: 0 "
                "files matched` warning in the agent log around the time "
                "the branch's worktree was last active."
            )
        # #1295: be honest about the possibilities.  The previous wording
        # ("merged and pruned, or nothing was ever built here") quietly
        # skipped the case that motivated this fix: the hourly worktree
        # sweep may have removed a worktree that was still live in a tmux
        # session, in which case the branch was never merged and something
        # WAS being built.  Surface all three plausible causes so
        # `coord pull-artifact`'s error message stops implying a single
        # explanation the agent can't actually distinguish.
        return (
            f"no stash and no live worktree for branch {branch!r} on this host. "
            "possible causes: (1) the branch was already merged and its "
            "worktree pruned; (2) nothing was ever built for this branch on "
            "this host; or (3) the worktree-clean sweep removed a worktree "
            "whose interactive session was still up (see #1295) — check "
            f"`tmux ls | grep coord-` for a live session, and the agent log "
            "for any recent `stash: 0 files matched` warning."
        )

    def artifact_manifest(self, repo: str, branch: str) -> dict | None:
        """Return the artifact manifest for a stash, or ``None`` if missing.

        *branch* must already be sanitized (i.e. the path component form,
        no slashes).  Returns a dict with keys ``files``, ``total_bytes``,
        and ``built_by_assignment_id``, or ``None`` when no stash exists.

        Returns ``None`` (→ 404) when *repo* or *branch* contain path-traversal
        sequences (``..``, ``/``, or characters outside ``[a-zA-Z0-9._-]``).
        The agent server is Tailscale-only, not internet-facing, but rejecting
        malformed params is cheap and prevents any node from probing the
        artifacts directory structure.

        Lazy stash-on-pull safety net (#914): when the stash is empty, a
        missed interactive finalize (the session ended without a clean
        ``coord done`` — crash, tmux killed directly, or finalize skipped)
        can still leave the built worktree sitting on disk. Before reporting
        absence, look for a live worktree still checked out to *branch* and
        stash it now, so `coord pull-artifact` self-heals instead of 404ing
        with a misleading "GC'd / glob mismatch / not configured" guess.
        """
        if (
            not self._SAFE_PATH_COMPONENT.match(repo)
            or not self._SAFE_PATH_COMPONENT.match(branch)
        ):
            return None
        stash_dir = self.state_dir / "artifacts" / repo / branch
        if not self._stash_has_content(stash_dir):
            patterns = self.artifact_paths.get(repo, [])
            wt = self._find_live_worktree(repo, branch) if patterns else None
            if wt is not None and wt.exists():
                copied = stash_artifacts_for_branch(
                    worktree_path=wt,
                    branch=branch,
                    repo_name=repo,
                    patterns=patterns,
                    state_dir=self.state_dir,
                    assignment_id=wt.name,
                )
                if copied > 0:
                    self._artifact_bytes_cache = None
            if not self._stash_has_content(stash_dir):
                return None

        files = []
        for f in sorted(stash_dir.iterdir()):
            if not f.is_file() or f.name.startswith("."):
                continue
            try:
                st = f.stat()
                files.append({"name": f.name, "size": st.st_size, "mtime": st.st_mtime})
            except OSError:
                pass

        aid_path = stash_dir / ".assignment_id"
        built_by: str | None = None
        try:
            built_by = aid_path.read_text(encoding="utf-8").strip()
        except OSError:
            pass

        total_bytes = sum(item["size"] for item in files)
        return {"files": files, "total_bytes": total_bytes, "built_by_assignment_id": built_by}

    def clean_worktrees(
        self,
        *,
        recent_secs: float = 300.0,
        protect: Iterable[str] | None = None,
    ) -> dict:
        """Remove git worktrees for assignments in terminal states.

        Idempotent — safe to call multiple times.  Skips worktrees for:
        - Running or pending assignments (still in use by a worker).
        - Assignments whose ``finished_at`` timestamp is within
          *recent_secs* seconds of now (default 5 min) — protects against
          racing with a worker that just finished/was cancelled.
        - Directories whose ``mtime`` is within *recent_secs* of now —
          this catches the window between ``_setup_worktree`` creating
          the directory and ``assign()`` registering the assignment in
          ``self._assignments``.  Without it, a ``clean_worktrees`` call
          that snapshots ``_assignments`` mid-spawn would treat the
          freshly-created tree as orphaned and ``git worktree remove`` it
          out from under the worker.
        - Worktrees whose ``coord-<assignment_id>`` tmux session is still
          alive on this host (#1295, agent-local live-session guard).  An
          interactive session (Test/Review/Merge/Work) can outlive its
          assignment record in ``self._assignments`` — the record's
          ``finished_at`` reflects the *dispatch subprocess* finishing,
          not the operator detaching — so ``finished_at`` alone is not a
          reliable "worker is done with this tree" signal.  The tmux
          session, on the other hand, is only up while the pane is being
          used; querying it is ground truth.  If ``tmux`` is not
          installed or ``has-session`` fails, we treat that as "no live
          session" and continue the sweep — the check must never abort
          the sweep for other entries.
        - Assignment IDs in the optional *protect* iterable (#1295,
          coordinator-supplied second-tier guard).  The board daemon
          passes a snapshot of every non-terminal assignment_id it knows
          about so a live worker whose record has been lost from
          ``self._assignments`` (agent restart before reload,
          coord.db-only record) is still preserved on the agent side.
          The tmux guard above is the primary defence; *protect* is
          belt-and-braces for cases the local check can't see.
        - Directories that are symlinks (#1295) — we never chase a
          symlink out of the worktree base and delete something else on
          disk.  A symlink in ``state_dir/worktrees/`` is not something
          the agent creates; treat it as opaque and skip.

        Returns ``{"cleaned": N, "kept": M, "bytes_freed": B}`` plus the
        ``cargo_*`` keys from :func:`coord.cargo_cache.sweep` (#1402).  A
        protected entry counts as ``kept`` — the three original keys are
        unchanged so existing callers keep working.

        #1402: the same pass GCs the shared cargo target cache.  It runs on
        both exit paths (a machine may hold a multi-GiB cache with no
        worktrees at all) and never evicts a cache belonging to a repo with
        a live assignment here.
        """
        worktree_base = self.state_dir / "worktrees"
        if not worktree_base.exists():
            return {
                "cleaned": 0,
                "kept": 0,
                "bytes_freed": 0,
                **self._gc_cargo_cache(),
            }

        now = time.time()
        protect_set: set[str] = set(protect) if protect else set()

        with self._lock:
            assignments = dict(self._assignments)

        # #460 (Part 3): collect branches that PENDING assignments will need so
        # the 300 s recent-skip is bypassed for terminal worktrees that hold one
        # of those branches.  Without this, a terminal worktree from a just-
        # failed assignment would block the next dispatch on the same branch
        # until the cooldown expires, causing _setup_worktree to collide even
        # after the proactive _free_branch_in_worktrees call.
        pending_branches: set[str] = set()
        for _a in assignments.values():
            if _a.status == PENDING:
                b = _a.spec.target_branch or (
                    f"issue-{_a.spec.issue_number}-{_slugify(_a.spec.issue_title)}"
                )
                pending_branches.add(b)

        cleaned = 0
        kept = 0
        bytes_freed = 0

        for entry in worktree_base.iterdir():
            # #1295: never chase symlinks out of state_dir/worktrees/.
            # `Path.is_dir()` follows symlinks, so a bare `not is_dir()`
            # would still admit a symlink-to-directory into the sweep and
            # the eventual `_safe_remove_worktree(...)` could touch whatever
            # the symlink pointed at.  Excluding symlinks up front is the
            # simplest correct guard — the agent never creates them here.
            # (#1693 resolves both sides of the sandbox check as a second
            # line of defence, so a symlink out of `worktrees/` is now
            # refused there too.)
            if entry.is_symlink():
                kept += 1
                continue
            if not entry.is_dir():
                continue
            assignment_id = entry.name
            a = assignments.get(assignment_id)

            # #1295: coordinator-supplied second-tier guard.  Any
            # assignment id the board considers non-terminal is off-limits
            # regardless of what the local assignments dict says — the
            # agent may have restarted and lost its in-memory record for a
            # worker whose interactive session is still up.
            if assignment_id in protect_set:
                kept += 1
                continue

            # #1295: agent-local live-session guard.  If a tmux session
            # named `coord-<assignment_id>` exists on this host, someone
            # is (still) interactively using this worktree — an operator
            # in a Test/Review/Merge/Work pane whose session outlived the
            # dispatch subprocess.  The tmux probe is ground truth and
            # cheap; consult it BEFORE any other decision so a stale
            # `finished_at`/absent record can't sweep out a live pane.
            # Failures inside `_tmux_session_alive` (tmux not installed,
            # server not running, subprocess/OS errors) collapse to
            # False so the check never raises out of this loop.
            if self._tmux_session_alive(assignment_id):
                kept += 1
                continue

            # Never touch worktrees for running/pending assignments.
            if a is not None and a.status in (RUNNING, PENDING):
                kept += 1
                continue

            # Skip recently-finished assignments — the worker process may
            # still be tearing down and have open file handles in the tree.
            # Exception: if a PENDING assignment needs the same branch, clean
            # it up now so the next _setup_worktree doesn't collide (#460).
            if a is not None and a.finished_at is not None:
                age = now - a.finished_at
                if age < recent_secs:
                    terminal_branch = a.branch or (
                        a.spec.target_branch or
                        f"issue-{a.spec.issue_number}-{_slugify(a.spec.issue_title)}"
                    )
                    if terminal_branch not in pending_branches:
                        kept += 1
                        continue
                    # Fall through to cleanup — a pending assignment needs
                    # this branch and we must not let the cooldown block it.

            # Skip directories that were created very recently even when
            # we don't (yet) have an assignment record.  This closes the
            # race window between `_setup_worktree` (which makes the dir)
            # and the `with self._lock: self._assignments[id] = …` insert
            # in `assign()` — if `clean_worktrees` snapshots _assignments
            # in that window, the worktree looks orphaned but the worker
            # is still inside `git worktree add`.
            if a is None:
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    mtime = None
                if mtime is not None and (now - mtime) < recent_secs:
                    kept += 1
                    continue

            # Compute size before removal so the caller knows bytes freed.
            dir_size = _dir_size(entry)

            # #305: stash any configured artifacts before removing the
            # worktree.  Idempotent — if the reap thread already stashed
            # these files, _stash_artifacts is a no-op (the worktree won't
            # exist or the stash dir is simply overwritten).
            if a is not None:
                self._stash_artifacts(a)
            else:
                # #1295 fix item #2: no assignment record at all — the
                # tmux/protect guards above only catch a session that is
                # STILL alive; this is the narrower case they explicitly
                # don't cover — an interactive session that ended without
                # ever running finalize (crash, `tmux kill-session`,
                # network drop before `coord done`).  There is no
                # AgentAssignment to hand to `_stash_artifacts`, so derive
                # the branch/repo straight from git on the worktree itself
                # and attempt a best-effort stash before the tree is
                # destroyed below.
                self._stash_orphaned_worktree(entry, assignment_id)

            # Try a proper git worktree remove first (updates the main
            # repo's worktree bookkeeping).  Fall back to brute-force rmtree
            # if git isn't available or the main repo has moved — but only
            # inside `worktree_base` (#1693): an orphaned sweep entry whose
            # admin record git has already pruned is still removable, while
            # anything resolving outside the sandbox (or onto the base
            # checkout) is refused and counted as kept.
            repo_path_str: str | None = None
            if a is not None:
                repo_path_str = self.repo_paths.get(a.spec.repo_name)
            removed = _safe_remove_worktree(
                Path(repo_path_str) if repo_path_str else None,
                entry,
                sandbox_root=worktree_base,
                prune=False,
            )

            if removed:
                bytes_freed += dir_size
                cleaned += 1
            else:
                kept += 1

        # #305: GC old artifact stashes in the same pass so callers don't
        # need a separate endpoint.  Default TTL is 3 days.
        self._gc_artifacts()

        return {
            "cleaned": cleaned,
            "kept": kept,
            "bytes_freed": bytes_freed,
            # #1402: bound the shared cargo cache in the same sweep.
            **self._gc_cargo_cache(),
        }

    def _gc_cargo_cache(self) -> dict:
        """Bound the shared cargo target cache (#1402).

        Repos with a pending/running assignment on this agent are protected
        from *eviction* so the GC can never delete a target dir out from
        under a build in flight.  #2137: they are still eligible for
        intra-repo *pruning* (incremental dirs, stale profile dirs) when
        nothing is actually compiling against them — otherwise the busiest
        repo on the machine is also the one whose cache can never be
        reclaimed, which is how ``cargo-target/quadraui`` reached 38G against
        a 20 GiB cap and filled ``/home``.

        Also passes the absolute free-disk floor (#2137 item 4) and, since
        #2919, every local checkout's own ``target/`` dir
        (``checkout_target_dirs``) — without this the floor can only ever
        reclaim from the shared cache, which is precisely the 2026-08-28
        incident: a stale ``~/src/quadraui/target`` sat untouched two
        directories away while the sweep evicted the whole cache to
        compensate for bytes it structurally could not reach.  Mirrors
        ``fix_cargo_targets``'s own ``_checkout_target_dirs`` in
        ``coord/health/checks/cargo_targets.py``.  Also parks the sweep's
        verdict where the ``cargo_targets`` health check can read it so
        ``cargo_over_cap`` reaches an operator instead of dead-ending in this
        dict.  Best-effort: any failure degrades to an empty dict rather than
        aborting the worktree sweep that calls it.
        """
        with self._lock:
            live_repos = {
                a.spec.repo_name
                for a in self._assignments.values()
                if a.status in (PENDING, RUNNING)
            }
        try:
            from coord.health.context import local_checkouts  # noqa: PLC0415

            checkout_target_dirs = [
                c.path / "target" for c in local_checkouts(self._health_config)
            ]
            result = cargo_cache.sweep(
                self.state_dir,
                protect_repos=live_repos,
                free_floor=cargo_cache.free_floor_bytes(),
                checkout_target_dirs=checkout_target_dirs,
            )
        except OSError as e:  # pragma: no cover - defensive
            _log.warning("cargo cache GC failed: %s", e)
            return {}
        if result.get("cargo_over_cap"):
            _log.warning(
                "cargo cache still over cap after GC: %s",
                result.get("cargo_over_cap_reason") or "unknown reason",
            )
        cargo_cache.write_gc_status(self.state_dir, result)
        return result

    def list_assignments(self) -> dict:
        from coord.worker_events import is_stream_json, parse_log

        # #1492: clear any ADVISORY entry that's gone terminal on GitHub
        # before serving the completed list — see `_prune_terminal_advisory`
        # docstring for the rate limiting and fail-open behavior.
        try:
            self._prune_terminal_advisory()
        except Exception:  # noqa: BLE001 — must never break /status
            _log.warning("_prune_terminal_advisory failed", exc_info=True)

        # #1468: clear any ADVISORY entry superseded by a later DONE retry
        # for the same issue — see `_prune_superseded_advisory` docstring.
        try:
            self._prune_superseded_advisory()
        except Exception:  # noqa: BLE001 — must never break /status
            _log.warning("_prune_superseded_advisory failed", exc_info=True)

        with self._lock:
            assignments = list(self._assignments.values())
        active = []
        completed = []
        for a in assignments:
            d = a.to_status_dict()
            if a.status == RUNNING:
                try:
                    prog = self.progress(a.id)
                except Exception:
                    prog = None
                if prog:
                    d["progress"] = prog
                # #1632: when this worker last SAID anything, as Unix
                # seconds.  The output-silence probe is the one that catches
                # failures with no symptom except duration, and only this
                # machine can see its own log file — the coordinator cannot
                # stat a remote path.  A single `stat` on a file that is
                # already open for append, wrapped so a vanished log can
                # never break `/status`.
                if a.log_path:
                    try:
                        d["last_output_at"] = os.path.getmtime(a.log_path)
                    except OSError:
                        pass
                # Tail-read stream-json log for live summary fields.
                if a.log_path and is_stream_json(a.log_path):
                    try:
                        summary = parse_log(a.log_path)
                    except Exception:
                        summary = None
                    if summary is not None:
                        d["model_used"] = summary.model_used
                        d["turns"] = summary.num_turns
                        d["cost_so_far"] = summary.total_cost_usd
                        d["last_tool"] = summary.last_tool
                        d["rate_limited"] = summary.rate_limited
                active.append(d)
            else:
                # For terminal assignments, parse the whole log (tail_bytes=0)
                # so we can report final totals reliably.
                if a.log_path and is_stream_json(a.log_path):
                    try:
                        summary = parse_log(a.log_path, tail_bytes=0)
                    except Exception:
                        summary = None
                    if summary is not None:
                        d["model_used"] = summary.model_used
                        d["total_cost_usd"] = summary.total_cost_usd
                        d["num_turns"] = summary.num_turns
                        d["stop_reason"] = summary.stop_reason
                        # #667: expose token counts so the coordinator can
                        # persist them even when the log is only on this machine.
                        d["input_tokens"] = summary.input_tokens
                        d["output_tokens"] = summary.output_tokens
                        d["cache_creation_tokens"] = summary.cache_creation_tokens
                        d["cache_read_tokens"] = summary.cache_read_tokens
                completed.append(d)
        return {"active": active, "completed": completed}

    def _servable_repos(self) -> tuple[list[str], dict[str, str]]:
        """#1527: split ``self.repos`` into servable vs degraded.

        A repo is servable only when it has a ``repo_paths`` entry AND that
        path exists on disk right now — mirrors the same two checks
        ``list_repos`` below already makes per-repo, just collapsed to a
        reason string instead of a full git-status dict (``/health`` is
        polled far more often than ``/repos`` and must stay cheap — no
        ``git rev-parse`` here, just a ``Path.exists()`` stat per repo).

        Returns ``(servable_repo_names, {repo_name: reason})`` — the second
        dict is empty when every configured repo is servable.
        """
        servable: list[str] = []
        degraded: dict[str, str] = {}
        for repo_name in self.repos:
            path_str = self.repo_paths.get(repo_name)
            if not path_str:
                degraded[repo_name] = "no repo_path configured for this machine"
                continue
            path = Path(path_str).expanduser()
            if not path.exists():
                degraded[repo_name] = f"repo_path does not exist: {path}"
                continue
            servable.append(repo_name)
        return servable, degraded

    def fix_graph(self, repo_name: str, *, timeout: float = 600.0) -> dict:
        """Repair the machine-local half of graphify for *repo_name* here
        (#2237 item 1 — the agent side of ``coord repo doctor --fix``).

        The whole point of routing this through the agent is that the
        operator's laptop is *not* where the workers run: layer 5 was the one
        onboarding layer probed only on the machine running the command, so a
        repo with a graph on the operator's box and none on dellserver
        reported clean. Repairing it has the same shape — a fix that only
        ever repairs the local clone fixes the machine that needed it least.

        Idempotent and never touches a tracked file:
        :func:`coord.graph_health.apply_local_graph_fix` sets
        ``core.hooksPath`` and builds a missing graph, and refuses outright
        when the repo does not ship ``.githooks/post-checkout`` (a versioned
        change, which is a PR against that repo and must never be automated).

        Returns the :meth:`~coord.graph_health.GraphFixResult.to_dict` shape
        plus ``repo``. An unknown repo, or one with no checkout on this
        machine, comes back as a ``refused`` result rather than an error —
        the caller is sweeping every machine and wants a per-machine answer,
        not an exception that hides the other machines' results.
        """
        from coord.graph_health import GraphFixResult, apply_local_graph_fix  # noqa: PLC0415

        path_str = self.repo_paths.get(repo_name)
        if not path_str:
            result = GraphFixResult(
                repo_path="",
                refused=f"no repo_path configured for {repo_name!r} on this machine",
            )
            return {**result.to_dict(), "repo": repo_name}

        repo_path = Path(path_str).expanduser()
        result = apply_local_graph_fix(repo_path, timeout=timeout)
        # A rebuild that succeeds here invalidates whatever this HEAD was
        # last recorded as failing (guard 3's bookkeeping) — otherwise the
        # self-heal would keep replaying a stale failure reason at an
        # operator who just fixed it by hand.
        if result.ok:
            with self._lock:
                self._graph_rebuild_failed.pop(str(repo_path), None)
            self._local_health_cache = None
        return {**result.to_dict(), "repo": repo_name}

    def reconcile_drive_queue(self, *, timeout: float = 120.0) -> dict:
        """``coord drive-queue tick --reconcile-only`` — run on THIS machine
        (#2373 — ``coord release propagate``'s drain-deadline escalation
        fans this out to a host it has cordoned but that will not drain, via
        `_reconcile_launch_host` in `coord/commands/release.py`).

        Only the machine that launched a `running` drive-queue entry can
        resolve it (the #1870 cross-host guard in
        `coord.drive_queue._reconcile_running` is keyed on a LOCAL tmux
        read); this method is that resolution, reachable over this agent's
        HTTP API instead of a human's SSH session. Full incident writeup
        (claude-coordinator#2360) and rationale: `_reconcile_launch_host`'s
        docstring and docs/AGENT_OPERATIONS.md's #2373 section.

        Delegates to :func:`coord.reconcile_tick.run_reconcile_tick` — the
        same shared implementation `coord release propagate`'s own
        `_run_reconcile_tick` (`coord/commands/release.py`) calls, so there
        is exactly one implementation of "what a reconcile-only tick does",
        not two that can drift apart.

        Returns ``{"ok": bool, "detail": str}``. ``ok=False`` with a
        ``no local coordinator.yml`` detail when this agent has no on-disk
        config to pass as ``--config`` — thin-client/config-free mode
        (docs/EPHEMERAL_WORKERS.md) has nothing for a queue tick to resolve
        against here. Never raises: the caller (`/drive-queue-reconcile`) is
        a best-effort self-heal step inside a larger escalation, not a gate
        that may take propagation down with it.
        """
        from coord.reconcile_tick import run_reconcile_tick  # noqa: PLC0415

        config_path = getattr(self._health_config, "path", None)
        if not config_path:
            return {
                "ok": False,
                "detail": (
                    "no local coordinator.yml on this agent — cannot resolve "
                    "a --config path for the tick"
                ),
            }
        ok, detail = run_reconcile_tick(config_path, timeout=timeout, detail_limit=2000)
        return {"ok": ok, "detail": detail}

    def list_repos(self) -> dict[str, dict]:
        """Return local HEAD / branch / dirty flag for each configured repo.

        Per-repo errors (missing path, not a git repo, etc.) come back as an
        `error` field rather than failing the whole call — the coordinator
        wants a complete picture across machines even when one is broken.
        """
        result: dict[str, dict] = {}
        for repo_name in self.repos:
            path_str = self.repo_paths.get(repo_name)
            if not path_str:
                result[repo_name] = {"error": "no repo_path configured for this machine"}
                continue
            path = Path(path_str).expanduser()
            if not path.exists():
                result[repo_name] = {"error": f"path does not exist: {path}"}
                continue
            try:
                sha = _git(path, "rev-parse", "HEAD")
                branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
                porcelain = _git(path, "status", "--porcelain")
            except _GitError as e:
                result[repo_name] = {"error": str(e), "path": str(path)}
                continue
            result[repo_name] = {
                "sha": sha,
                "branch": branch,
                "dirty": bool(porcelain.strip()),
                "path": str(path),
            }
        return result

    def _resolve_provider(self, spec: AssignmentSpec) -> "object | None":
        """Resolve ``spec.provider`` to a live provider instance (#1796).

        Precedence:

        1. ``spec.provider is None`` → returns ``None``, the legacy "no
           provider requested" case.  Every call site treats ``None`` as
           "run the pre-#324 default claude path" — unchanged, and the
           ONLY case that still does so silently (no-config parity
           requirement, #324).
        2. ``spec.provider in self._providers`` → the locally configured
           instance (this agent's own ``coordinator.yml`` /
           ``providers.definitions``) wins.  Preserved from pre-#1796
           behaviour: an agent with its own local override of a provider's
           binary/env/extra_args should use ITS OWN definition, not the
           coordinator's wire-carried one.
        3. ``spec.provider_def is not None`` → build a fresh provider from
           the wire-carried definition (#1796's fix).  This is the path a
           config-free agent (docs/EPHEMERAL_WORKERS.md — no local
           ``coordinator.yml``, no board service) takes for every named
           provider, since it has no local ``providers.definitions``
           registry to look ``spec.provider`` up in at all.
        4. Otherwise → raise :class:`ValueError`.  Before #1796 this case
           silently fell through to the legacy claude path — the exact bug
           #1796 exists to close: an explicitly requested provider that
           cannot be honoured must be refused, never silently substituted.
           Every caller of this method is expected to let the ValueError
           propagate as a refused assignment (``assign()``'s HTTP handler
           turns it into a 400), not swallow it — that ``except ValueError``
           handler in ``agent_app.py``'s ``assign`` route already existed
           before #1796 (e.g. for the #425/#437 capability gates below);
           this method just reuses it, it doesn't add it.

        Returns:
            ``None`` for the legacy no-provider case, otherwise a
            duck-typed provider object (``build_command``/``initial_input``/
            ``capabilities``/``env``/``result_marker``/``parse_log``).

        Raises:
            ValueError: ``spec.provider`` is set but cannot be resolved from
                either the local registry or ``spec.provider_def``, or
                ``spec.provider_def`` itself is malformed / names an unknown
                provider type.
        """
        if spec.provider is None:
            return None
        if spec.provider in self._providers:
            local = self._providers[spec.provider]
            if spec.provider_def is not None:
                # #2326: step 2 (local override) is winning over a wire
                # provider_def the coordinator DID send — the precedence is
                # deliberate (see docstring), but before this the decision
                # left no trace, which is why a days-stale local registry
                # silently shadowing a fresh coordinator edit took a live
                # `/proc/<pid>/environ` probe to catch instead of a log
                # line. Env keys only (not values) — provider env commonly
                # carries API keys/tokens, and the point here is "does this
                # instance carry the key the coordinator's does", not the
                # secret itself.
                try:
                    local_env_keys = sorted(local.env())  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001 — logging must never break dispatch
                    local_env_keys = None
                wire_env_keys = sorted((spec.provider_def or {}).get("env") or {})
                _log.info(
                    "coord agent: provider %r resolved from this agent's "
                    "local providers.definitions (env keys=%s), overriding "
                    "the coordinator's wire-carried provider_def (env "
                    "keys=%s) — local-wins-over-wire is deliberate "
                    "precedence (#1796); if that's not what you expected, "
                    "check this agent's own coordinator.yml for a stale "
                    "definition (#2326)",
                    spec.provider,
                    local_env_keys,
                    wire_env_keys,
                )
            return local
        if spec.provider_def is not None:
            from coord.providers import build_provider_from_wire  # noqa: PLC0415

            try:
                return build_provider_from_wire(spec.provider, spec.provider_def)
            except ValueError as e:
                raise ValueError(
                    f"refusing assignment: provider {spec.provider!r} could "
                    f"not be constructed from its dispatch-payload "
                    f"provider_def: {e}"
                ) from e
        raise ValueError(
            f"refusing assignment: provider {spec.provider!r} could not be "
            f"resolved — it is not in this agent's local providers registry "
            f"({sorted(self._providers) if self._providers else 'none configured'}) "
            f"and the dispatch payload carried no provider_def to construct "
            f"it from (an older coordinator that predates #1796, or a "
            f"provider name with no matching providers.definitions entry "
            f"coordinator-side).  Refusing rather than silently falling "
            f"back to the legacy claude path (#1796): the recorded "
            f"provider must match the binary actually executed.  Configure "
            f"this provider in this agent's own coordinator.yml, or "
            f"redeploy a coordinator new enough to send provider_def."
        )

    def _resolve_provider_best_effort(self, spec: AssignmentSpec) -> "object | None":
        """Non-raising variant of :meth:`_resolve_provider` for post-spawn,
        log-parsing-only call sites (``_reap``) (#1796).

        By the time ``_reap`` runs, this assignment already spawned
        successfully — ``assign()``/``_spawn()`` already resolved the SAME
        ``spec`` via :meth:`_resolve_provider` without raising.  A failure
        here would only mean a redundant re-resolution went wrong for some
        unexpected reason; #1796 mandates refusal at DISPATCH time, not a
        crash of the best-effort reap/log-parsing path for an
        already-running (or already-finished) worker.  Degrades to
        ``None`` (the caller's existing "use the default claude-shaped
        parser" fallback) on any :class:`ValueError`.
        """
        try:
            return self._resolve_provider(spec)
        except ValueError:
            return None

    def assign(self, spec: AssignmentSpec) -> AgentAssignment:
        """Accept an assignment and spawn the worker. Returns immediately."""
        # #2299: refresh from coordinator.yml before the repo gate below.
        # /health already reloads on every poll, but gating dispatch on
        # "someone happened to poll first" would make repo onboarding depend
        # on poll timing; this costs one stat() in the steady state and makes
        # the guarantee unconditional — the dispatch that arrives after the
        # edit is served, full stop.
        self._maybe_reload_config()
        if self.repos and spec.repo_name not in self.repos:
            raise ValueError(
                f"this agent does not handle repo {spec.repo_name!r} "
                f"(supported: {self.repos})"
            )

        repo_path = Path(spec.repo_path).expanduser()
        if not repo_path.exists():
            raise ValueError(f"repo path does not exist: {repo_path}")

        if spec.pull_repos:
            unknown = [r for r in spec.pull_repos if r not in self.repo_paths]
            if unknown:
                raise ValueError(
                    f"pull_repos references repos with no repo_path on this agent: {unknown}"
                )

        # #425/#324/#1796 capability gates: only run when spec.provider is
        # set.  `_resolve_provider` implements the full resolution chain
        # (local registry → wire-carried provider_def → refuse — see its
        # docstring) and raises ValueError when spec.provider cannot be
        # resolved at all, which propagates out of `assign()` as a refused
        # assignment.  When spec.provider is None, resolution returns None
        # and both gates below are no-ops, so the default ``claude -p`` path
        # runs unchanged (no-config parity requirement, #324).
        if spec.provider is not None:
            provider_obj = self._resolve_provider(spec)
            caps = provider_obj.capabilities()  # type: ignore[attr-defined]

            # Safety gate (#425): refuse write-capable assignment types on any
            # provider that does NOT enforce the deny-list.  Non-mutating
            # types (plan / refinement / test-chat / new-issue-chat) may still
            # use unverified providers because they can't push code or open PRs.
            if (
                not caps.enforces_deny_list
                and spec.type in WRITE_CAPABLE_SPEC_TYPES
            ):
                raise ValueError(
                    f"refusing to spawn spec.type={spec.type!r} on provider "
                    f"{spec.provider!r}: this provider reports "
                    "capabilities().enforces_deny_list=False — its deny-list "
                    "enforcement has not been verified, so the agent will not "
                    "run write-capable assignment types on it.  Use a "
                    "non-mutating type (plan, refinement, test-chat, "
                    "new-issue-chat) or switch to a provider that enforces "
                    "the deny-list (e.g. 'claude')."
                )

            # Resume gate (#324): refuse session-resume when the provider
            # doesn't support the --resume flag.  A provider with
            # capabilities().resume=False has no concept of session continuity;
            # passing resume_session_id would be silently ignored, misleading
            # the coordinator into thinking the worker loaded the prior context.
            if spec.resume_session_id and not caps.resume:
                raise ValueError(
                    f"refusing to resume session {spec.resume_session_id!r} "
                    f"on provider {spec.provider!r}: "
                    "capabilities().resume=False — this provider does not "
                    "support session resume via --resume.  Use a provider "
                    "with resume=True (e.g. 'claude') or dispatch without "
                    "resume_session_id."
                )

        assignment = AgentAssignment(
            id=uuid.uuid4().hex[:12],
            spec=spec,
            status=PENDING,
        )
        assignment.log_path = str(self.log_dir / f"{assignment.id}.log")

        if spec.type in ("plan", "refinement", "test-chat", "new-issue-chat", "milestone-chat"):
            # No-worktree run: none of these touch git (no branch is created
            # or modified) — plan/refinement/test-chat/new-issue-chat are
            # additionally read-only everywhere; milestone-chat (#770) is
            # the one exception that CAN mutate GitHub (the tracking issue
            # body, via `coord milestone write-order`) despite needing no
            # worktree — see WRITE_CAPABLE_SPEC_TYPES for that axis. For
            # chat sessions (#315 / #314 / #316 / #770), the stable cwd is
            # also required so claude-cli's `--resume <session_id>` finds
            # the prior session file on subsequent turns: claude scopes
            # sessions by cwd (mangled into ~/.claude/projects/<cwd-key>/),
            # and a per-assignment worktree gives every turn a different cwd.
            with self._lock:
                self._assignments[assignment.id] = assignment
            self._persist()

            if spec.pull_repos:
                thread = threading.Thread(
                    target=self._pull_then_spawn,
                    args=(assignment, repo_path),
                    daemon=True,
                    name=f"agent-pull-{assignment.id}",
                )
                thread.start()
            else:
                self._spawn(assignment, repo_path)
            return assignment

        # Create worktree for isolation
        try:
            worktree_path = self._setup_worktree(assignment, repo_path)
        except (_GitError, OSError) as e:
            assignment.status = FAILED
            assignment.error = f"worktree setup failed: {e}"
            assignment.finished_at = time.time()
            with self._lock:
                self._assignments[assignment.id] = assignment
            self._persist()
            return assignment  # Don't raise — let coordinator see the failure

        assignment.worktree_path = str(worktree_path)

        # #1445: refuse to spawn a worker into a worktree it can't actually
        # write to — cheap to catch here (a stat + a JSON parse) vs. a full
        # session that reasons, designs, and only discovers at the very end
        # that it had nowhere to save its work.
        write_issue = check_worktree_writable(
            worktree_path, settings_files=self._worktree_writable_settings_files
        )
        if write_issue is not None:
            assignment.status = FAILED
            assignment.error = f"worktree not writable: {write_issue}"
            assignment.finished_at = time.time()
            with self._lock:
                self._assignments[assignment.id] = assignment
            self._persist()
            # #1445 review: _setup_worktree() above already ran `git worktree
            # add` and created a real branch before this check ran. Leaving
            # that in place on every failed dispatch would leak a fresh
            # worktree per retry — self-defeating for a check whose whole
            # point is catching failures cheaply (and worse, one of the OS-
            # level failures this check catches is a full disk). Tear it down
            # the same way cancel() does.
            self._cleanup_worktree(assignment)
            return assignment  # Don't raise — let coordinator see the failure

        with self._lock:
            self._assignments[assignment.id] = assignment
        self._persist()

        if spec.pull_repos:
            thread = threading.Thread(
                target=self._pull_then_spawn,
                args=(assignment, worktree_path),
                daemon=True,
                name=f"agent-pull-{assignment.id}",
            )
            thread.start()
        else:
            self._spawn(assignment, worktree_path)
        return assignment

    def cancel(
        self,
        assignment_id: str,
        *,
        rescue: bool = False,
        push_mode: str | None = None,
    ) -> AgentAssignment:
        """Terminate a running assignment. Idempotent for already-finished work.

        #1567: any uncommitted work is still committed locally so it is never
        silently destroyed, but by default it is NOT pushed anywhere — an
        operator stopping an assignment has usually decided its in-progress
        work is unwanted, so the old behaviour of publishing it straight onto
        the worker's own branch (replacing the remote tip with exactly the
        thing being stopped) was backwards. Pass ``rescue=True`` (``coord
        stop --rescue``) to push the WIP commit to a disposable
        ``rescue/<assignment_id>`` ref instead — the worker's branch is never
        touched either way.

        *push_mode* is an internal-only escape hatch for callers that are NOT
        an operator-initiated `coord stop` — currently only the agent's own
        ``/restart`` handler, which cancels still-running workers as part of
        a graceful self-restart and should keep publishing their WIP onto the
        worker's own branch exactly as before #1567 (nobody decided that
        work was unwanted; the agent process just needs to come back up).
        When given, it overrides the ``rescue``-derived default.
        """
        with self._lock:
            assignment = self._assignments.get(assignment_id)
            if assignment is None:
                raise KeyError(assignment_id)
            proc = self._processes.get(assignment_id)

        if assignment.status not in (PENDING, RUNNING):
            return assignment

        if proc is not None and proc.poll() is None:
            # Kill the whole process group (proc was spawned with
            # start_new_session=True so proc.pid is the pgid). proc.terminate()
            # alone leaves MCP subprocess children alive and the cancel hangs.
            _killpg_safe(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _killpg_safe(proc.pid, signal.SIGKILL)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass

        with self._lock:
            assignment.status = CANCELLED
            assignment.finished_at = time.time()
        self._persist()

        # Clean up worktree after cancellation. #1567: default push_mode is
        # "none" — commit locally only, do not touch the remote — unless the
        # caller explicitly asked to rescue the work onto a disposable ref,
        # or passed an explicit push_mode (see the /restart escape hatch
        # documented above).
        resolved_push_mode = push_mode or ("rescue" if rescue else "none")
        self._cleanup_worktree(assignment, push_mode=resolved_push_mode)

        return assignment

    def inject_message(self, assignment_id: str, text: str) -> None:
        """Inject a new user message into a running worker via its stdin.

        Raises :class:`KeyError` when the assignment doesn't exist on this
        agent, :class:`RuntimeError` when the worker isn't running or when the
        assignment's provider does not support message injection
        (``capabilities().inject=False``), and :class:`BrokenPipeError` when
        the worker closed its stdin (e.g. already finished or crashed).

        The worker picks up the message at its next turn boundary — between
        tool calls, not mid-tool.  Each injection appends a `# inject:`
        marker to the assignment log for traceability.
        """
        with self._lock:
            assignment = self._assignments.get(assignment_id)
            if assignment is None:
                raise KeyError(assignment_id)
            if assignment.status != RUNNING:
                raise RuntimeError(
                    f"assignment {assignment_id} is {assignment.status!r}, not running"
                )
            # #324/#1796: capability gate — refuse injection when the
            # provider reports capabilities().inject=False.  A provider
            # without stdin-injection support (e.g. a PTY-only backend) must
            # opt out here so callers get a clear error rather than silently
            # writing bytes to a stdin pipe that the provider may not even
            # expose.  Uses the best-effort resolver (#1796): this
            # assignment already spawned successfully, so `assign()`/
            # `_spawn()` already resolved this SAME spec once without
            # raising — a resolution failure here would only be a redundant
            # re-resolution going wrong, and must not crash message
            # injection while holding `self._lock`.
            spec = assignment.spec
            if spec.provider is not None:
                _inject_provider = self._resolve_provider_best_effort(spec)
                if _inject_provider is not None and not _inject_provider.capabilities().inject:
                    raise RuntimeError(
                        f"provider {spec.provider!r} does not support message injection "
                        f"(capabilities().inject=False)"
                    )
            proc = self._processes.get(assignment_id)
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise BrokenPipeError(
                f"worker for {assignment_id} has no open stdin (process exited?)"
            )
        try:
            proc.stdin.write(_user_message_line(text))
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise BrokenPipeError(str(e)) from e
        # Trace the injection in the log so users can correlate later.
        if assignment.log_path:
            try:
                with open(assignment.log_path, "a", encoding="utf-8") as fh:
                    fh.write(f"# inject: {text}\n")
            except OSError:
                pass

    def get(self, assignment_id: str) -> AgentAssignment | None:
        with self._lock:
            return self._assignments.get(assignment_id)

    def progress(self, assignment_id: str) -> dict | None:
        """Parse progress signals from the worker's log file."""
        from coord.progress import parse_progress

        a = self.get(assignment_id)
        if a is None or a.log_path is None:
            return None
        return parse_progress(a.log_path).to_dict()

    def wait_for(self, assignment_id: str, timeout: float = 10.0) -> AgentAssignment:
        """Block until an assignment leaves RUNNING *and* its teardown is done.

        Test helper.

        #1424: `_reap` flips `assignment.status` out of RUNNING/PENDING well
        before it finishes — worktree cleanup, the WIP-rescue commit for
        uncommitted work, and its push all happen afterward, in the same
        background thread. A caller that returns as soon as `status` looks
        terminal can observe the assignment mid-teardown: exactly the flake
        behind `test_end_to_end_worker_exits_with_uncommitted_work`, whose
        assertion on the *remote* branch raced the rescue commit's push. So
        this also waits for `_reap_complete[assignment_id]` (set in
        `_reap_guarded`'s `finally`) when one was registered — i.e. when a
        reap thread was actually spawned for this assignment. Some failure
        paths (e.g. `_pull_then_spawn` failing before `_spawn` ever runs)
        mark an assignment FAILED without ever starting a reap thread; for
        those there is nothing to wait for, so a missing event is treated as
        "already done" rather than blocking until the timeout.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                a = self._assignments.get(assignment_id)
                event = self._reap_complete.get(assignment_id)
            if a is None:
                raise KeyError(assignment_id)
            if a.status != RUNNING and a.status != PENDING:
                if event is None or event.is_set():
                    return a
            time.sleep(0.05)
        raise TimeoutError(f"assignment {assignment_id} still {a.status} after {timeout}s")

    # ── Internals ──────────────────────────────────────────────────────────

    def _setup_worktree(self, assignment: AgentAssignment, repo_path: Path) -> Path:
        """Create a git worktree for this assignment. Returns the worktree path."""
        worktree_base = self.state_dir / "worktrees"
        worktree_path = worktree_base / assignment.id

        # Clean up stale worktree if it exists.  #1693: via the single
        # chokepoint, sandboxed to `<state_dir>/worktrees`.
        if worktree_path.exists():
            _safe_remove_worktree(
                repo_path,
                worktree_path,
                log_path=assignment.log_path,
                sandbox_root=worktree_base,
                prune=False,
            )

        worktree_path.parent.mkdir(parents=True, exist_ok=True)

        # Prune administrative entries for worktrees whose directories were
        # removed out-of-band (e.g. a crash before clean_worktrees ran) so a
        # stale entry can't block `worktree add` below (#389 hygiene).
        try:
            _git(repo_path, "worktree", "prune")
        except _GitError:
            pass

        # Determine if `origin` is configured.  In production it always is;
        # only test fixtures + local-only repos lack a remote.  When origin
        # is present we MUST branch from a concrete `origin/<default>` SHA
        # to prevent unpushed local commits on `<default>` from riding into
        # the worker's branch (issue #255).
        try:
            _git(repo_path, "remote", "get-url", "origin")
            has_origin = True
        except _GitError:
            has_origin = False

        # Fetch latest only when we have a remote — keeps the offline /
        # test path silent.
        if has_origin:
            try:
                # --prune (#412): drop stale refs/remotes/origin/<branch> for
                # branches deleted on origin. Without it, a deleted branch's
                # remote-tracking ref lingers at its old SHA and the
                # origin_has_branch check below branches a "fresh" worker off
                # that dead ref — silently reimplementing on stale code.
                _git(repo_path, "fetch", "origin", "--prune")
            except _GitError:
                pass  # transient — origin_has_branch is cross-checked via ls-remote

        default_branch = assignment.spec.branch or "main"
        if has_origin:
            # #255: resolve to a concrete SHA from origin so unpushed local
            # commits on `<default>` can't sneak into the worker's branch.
            # If fetch failed AND origin/<default> isn't already known
            # locally, this raises — surfacing a real "couldn't reach
            # origin" condition rather than papering over it.
            try:
                start_point = _git(
                    repo_path, "rev-parse", f"origin/{default_branch}",
                ).strip()
            except _GitError as exc:
                raise _GitError(
                    f"_setup_worktree: cannot resolve origin/{default_branch} "
                    f"in {repo_path}. The remote is configured but the ref "
                    f"is missing — check network connectivity and that the "
                    f"repo's default_branch in coordinator.yml matches the "
                    f"actual branch on origin. ({exc})"
                ) from exc
        else:
            # No remote — fall back to the local branch (test fixtures, etc.)
            start_point = default_branch

        # #255: warn (in the assignment log) if local `<default>` has commits
        # that aren't on origin.  Those commits are NOT in the worker's
        # branch — that's the whole point of #255 — but the user should know
        # they have unpushed WIP sitting on this machine so they don't lose it.
        if has_origin and assignment.log_path:
            try:
                ahead = _git(
                    repo_path, "rev-list", "--count",
                    f"origin/{default_branch}..{default_branch}",
                ).strip()
                if ahead and ahead != "0":
                    msg = (
                        f"# warning: {default_branch} on this machine has {ahead} "
                        f"commit(s) ahead of origin/{default_branch}.  Those "
                        f"commits are NOT in the worker's branch (#255).  "
                        f"Push them when convenient so they aren't lost.\n"
                    )
                    try:
                        with open(assignment.log_path, "a", encoding="utf-8") as fh:
                            fh.write(msg)
                    except OSError:
                        pass
            except _GitError:
                # Local `<default>` may not exist (fresh clone) — silent.
                pass

        # Branch name for this assignment.  When `target_branch` is set
        # (auto-loop fix dispatch path), use it verbatim — the caller
        # knows the exact branch they want the worker to check out, and
        # we must NOT derive a new name from the (possibly `[fix-N]`-
        # prefixed) issue title or the fix would land on an orphan
        # branch instead of the original PR's branch.
        if assignment.spec.target_branch:
            branch_name = assignment.spec.target_branch
        else:
            branch_name = (
                f"issue-{assignment.spec.issue_number}-"
                f"{_slugify(assignment.spec.issue_title)}"
            )

        # Decide the base for the worker's branch.  Trusted sources, in order
        # (#389 — a leftover LOCAL branch from a prior failed assignment on
        # this machine must never be reused: branching a new worker off it
        # silently reverts merged work, as happened to #357/#319 when
        # precision was parked on a stale `issue-194` branch):
        #   1. origin/<branch> — a real remote branch (retry/continuation).
        #      Check it out and hard-reset to the remote tip so a divergent
        #      local copy of the branch can't ride in.
        #   2. local <branch>, but ONLY when this repo has no remote (test
        #      fixtures / local-only repos) — nothing more authoritative exists.
        #   3. otherwise branch fresh from `start_point` (origin/<default>),
        #      deleting any untrusted local leftover with the same name first.
        origin_has_branch = False
        local_has_branch = False
        if not assignment.spec.fresh_branch:
            if has_origin:
                try:
                    _git(
                        repo_path, "rev-parse", "--verify",
                        f"refs/remotes/origin/{branch_name}",
                    )
                    origin_has_branch = True
                except _GitError:
                    pass
                # #412 guard: a local refs/remotes/origin/<branch> can be stale
                # (deleted on origin but not pruned). Confirm against the actual
                # remote so a "fresh" worker can't branch off a dead ref's SHA.
                if origin_has_branch:
                    try:
                        remote_heads = _git(
                            repo_path, "ls-remote", "--heads",
                            "origin", branch_name,
                        )
                        if not remote_heads.strip():
                            origin_has_branch = False
                    except _GitError:
                        pass  # network hiccup — trust the (pruned) local ref
            try:
                _git(
                    repo_path, "rev-parse", "--verify",
                    f"refs/heads/{branch_name}",
                )
                local_has_branch = True
            except _GitError:
                pass

        # #460 (Part 1): proactively evict any other worktree that has
        # branch_name checked out before the add.  This handles serial
        # fix/retry/PR-worker dispatches on the same branch where the prior
        # assignment's worktree was not yet cleaned up (e.g. crash before
        # _cleanup_worktree ran, or clean_worktrees held back by the 300 s
        # recent-skip).  The call is a no-op when no conflicting worktree exists.
        _free_branch_in_worktrees(
            repo_path, branch_name, str(worktree_path),
            log_path=assignment.log_path,
        )

        if origin_has_branch:
            # Continuation/retry — force the worktree's branch to the remote
            # tip (#389), discarding any divergent local copy of the branch.
            # #460 (Part 2): _git_worktree_add retries once on collision.
            _git_worktree_add(
                repo_path,
                ["-B", branch_name, str(worktree_path), f"origin/{branch_name}"],
                log_path=assignment.log_path,
                default_branch=default_branch,
            )
        elif local_has_branch and not has_origin:
            # Local-only repo (no remote) — reuse the local branch as before.
            _git_worktree_add(
                repo_path,
                [str(worktree_path), branch_name],
                log_path=assignment.log_path,
                default_branch=default_branch,
            )
        else:
            # Fresh branch, OR an untrusted local-only leftover in a repo that
            # has a remote (#389).  Delete any colliding local branch so `-b`
            # won't fail and so the worker starts from origin/<default>.
            if local_has_branch and assignment.log_path:
                try:
                    with open(assignment.log_path, "a", encoding="utf-8") as fh:
                        fh.write(
                            f"# warning: discarding leftover local branch "
                            f"{branch_name!r} (not on origin) and branching "
                            f"fresh from {start_point[:12]} (#389)\n"
                        )
                except OSError:
                    pass
            try:
                _git(repo_path, "branch", "-D", branch_name)
            except _GitError:
                pass
            _git_worktree_add(
                repo_path,
                ["-b", branch_name, str(worktree_path), start_point],
                log_path=assignment.log_path,
                default_branch=default_branch,
            )

        # #1468: default this worktree's `pull.rebase` to true — see
        # `_set_worktree_pull_rebase` for why and how it's scoped so
        # `repo_path` (the operator's own checkout) is never touched.
        _set_worktree_pull_rebase(repo_path, worktree_path)

        return worktree_path

    def _log_line(self, assignment: AgentAssignment, text: str) -> None:
        """Append one line to the assignment log. Never raises (#1394)."""
        try:
            with open(assignment.log_path, "a", encoding="utf-8") as fh:
                fh.write(text if text.endswith("\n") else text + "\n")
        except (OSError, AttributeError, TypeError):
            pass

    def _record_dirty_worktree(
        self, assignment: AgentAssignment, reason: str
    ) -> None:
        """Persist *reason* on the assignment so the board can surface it.

        #1394: a dirty worktree at teardown means the worker wrote something it
        never committed.  That is a materially different outcome from "the
        worker wrote nothing", and until now the two were indistinguishable —
        both landed as a bare ADVISORY reading "0 commits pushed" while the
        code was silently deleted.  Recording the reason (and rewriting
        ``zero_commit_reason`` when the reap already flagged ADVISORY, since
        that is the string `coord status`, the dashboard and the GitHub
        advisory comment all render) is what makes the loss visible.

        #2234 fix-1: mirrors the same rewrite for a REFUSED_POLICY reap —
        ``policy_refusal_reason`` (not ``zero_commit_reason``) is the string
        the refused_policy GitHub comment and `coord status` render for that
        status, so a dirty worktree left behind by an otherwise-correct
        policy refusal needs the same visibility, on the field that surface
        actually reads.
        """
        with self._lock:
            live = self._assignments.get(assignment.id, assignment)
            live.dirty_worktree_reason = reason
            if live.status == ADVISORY:
                live.zero_commit_reason = reason
            elif live.status == REFUSED_POLICY:
                live.policy_refusal_reason = reason
            # Keep the caller's object in sync when it isn't the live one, so
            # a caller holding a detached copy still sees the outcome.
            assignment.dirty_worktree_reason = reason
            if assignment.status == ADVISORY:
                assignment.zero_commit_reason = reason
            elif assignment.status == REFUSED_POLICY:
                assignment.policy_refusal_reason = reason
        self._persist()

    def _rescue_uncommitted_work(
        self,
        assignment: AgentAssignment,
        wt_path: Path,
        *,
        push_mode: str = "branch",
    ) -> bool:
        """Preserve uncommitted work in *wt_path*. Returns "safe to remove".

        *push_mode* controls what happens to the remote AFTER the WIP commit
        is made locally (#1567):

        * ``"branch"`` — push straight to the worker's own branch (``origin
          HEAD``), same as before #1567. Used by the natural-completion /
          crash reap path, where nobody has decided the work is unwanted —
          rescuing it onto the branch it was headed for is the right default.
        * ``"none"`` — commit locally only, never touch the remote. This is
          the ``coord stop`` default: an operator reaching for ``stop`` has
          usually decided the in-progress work is NOT wanted, so publishing
          it — worse, replacing the remote branch tip with it — is the wrong
          thing to do by default. The commit still lands on the local branch
          ref (shared with the parent repo's git dir), so it survives the
          worktree being removed and is recoverable with plain git commands.
        * ``"rescue"`` — commit locally, then push to a dedicated
          ``rescue/<assignment.id>`` ref instead of the worker's branch.
          Used by ``coord stop --rescue``. Never force-pushes — a rescue ref
          is disposable but still shouldn't silently clobber a same-named ref
          from a previous rescue attempt.

        #1394.  ``_cleanup_worktree`` used to force-remove and ``rmtree`` the
        worktree with no dirty check at all, so a worker that ended its turn
        mid-edit — backgrounded its test suite and waited for a notification
        that a one-shot ``claude -p`` session can never receive, crashed,
        timed out, or was reaped — had its only copy of the work deleted.  The
        ``--orphan-worktrees`` sweep has guarded against exactly this since
        #618 ("Dirty worktrees are reported but never auto-deleted"); the
        synchronous per-assignment path simply never got the same guard.

        Returns ``True`` when the caller may proceed with removal, i.e. either
        the worktree is clean or the work now lives in a commit on the
        assignment branch.  Branch refs are shared with the parent repo, so
        once the commit exists it survives the worktree being removed — even
        if the subsequent push fails.

        Returns ``False`` to mean "do not delete": the worktree is the only
        copy of the work and must be kept for a human to recover.
        """
        dirt = _worktree_dirt(wt_path)
        if dirt is None:
            # Could not ask git.  Refuse to delete — guessing "clean" here is
            # precisely the bug.  A leaked worktree is recoverable; the work
            # is not.
            reason = (
                f"could not determine whether worktree {wt_path} has "
                "uncommitted changes; kept it rather than risk deleting work"
            )
            self._log_line(assignment, f"# cleanup: {reason} (#1394)")
            self._record_dirty_worktree(assignment, reason)
            return False

        tracked, untracked = dirt
        if tracked == 0 and untracked == 0:
            return True  # clean — unchanged behaviour

        if assignment.spec.type not in _WIP_RESCUE_TYPES:
            # Read-only or mid-rebase worker.  Untracked-only dirt is build or
            # test scratch (`.pytest_cache`, stray logs) and deleting it is
            # correct — keeping the worktree for every smoke run would leak
            # one per assignment.  Tracked modifications, though, mean real
            # edits to real files, so keep the worktree.
            if tracked == 0:
                return True
            reason = (
                f"{assignment.spec.type!r} worker left {tracked} uncommitted "
                f"change(s) to tracked files; worktree {wt_path} kept (not "
                "auto-committed — a WIP commit from this assignment type "
                "would pollute the branch)"
            )
            self._log_line(assignment, f"# cleanup: {reason} (#1394)")
            self._record_dirty_worktree(assignment, reason)
            return False

        total = tracked + untracked
        if total > _WIP_RESCUE_MAX_FILES:
            reason = (
                f"worker left {total} uncommitted file(s) — too many to commit "
                "safely (looks like un-gitignored build output); worktree "
                f"{wt_path} kept so nothing is lost"
            )
            self._log_line(assignment, f"# cleanup: {reason} (#1394)")
            self._record_dirty_worktree(assignment, reason)
            return False

        branch = assignment.branch or assignment.spec.target_branch or "HEAD"
        subject = (
            f"{_WIP_COMMIT_PREFIX} #{assignment.spec.issue_number}: "
            f"uncommitted worker changes preserved by the coordinator"
        )
        body = (
            "The worker finished (or died) with uncommitted changes in its "
            "worktree.\nThe coordinator committed them verbatim so they "
            "would not be destroyed by\nworktree teardown. This commit was "
            "NOT authored by the worker and has not\nbeen built, tested or "
            "reviewed — treat it as a recovery snapshot.\n\n"
            f"assignment: {assignment.id}\nSee #1394."
        )
        self._log_line(
            assignment,
            f"# cleanup: worktree dirty ({tracked} tracked, {untracked} "
            f"untracked) — rescuing as a WIP commit on {branch} (#1394)",
        )

        try:
            _git(wt_path, "add", "-A", timeout=60.0)
        except (_GitError, subprocess.TimeoutExpired, OSError) as e:
            # #1424: OSError (typically FileNotFoundError on *cwd*) fires when
            # the worktree directory vanishes between the `_worktree_dirt`
            # check above and this call — e.g. a concurrent `_cleanup_worktree`
            # for the same assignment (cancel() + the reap thread can both
            # reach here). `subprocess.run(cwd=wt_path)` raises OSError, not
            # `_GitError`, so it has to be caught here explicitly or it
            # escapes as an unhandled exception on a background thread and
            # the work is lost with no advisory at all.
            reason = (
                f"{total} uncommitted file(s) could not be staged ({e}); "
                f"worktree {wt_path} kept — recover the work manually"
            )
            self._log_line(assignment, f"# cleanup: {reason} (#1394) (#1424)")
            self._record_dirty_worktree(assignment, reason)
            return False

        commit_args = ["commit", "--no-verify", "-m", subject, "-m", body]
        committed = False
        try:
            _git(wt_path, *commit_args, timeout=60.0)
            committed = True
        except (_GitError, subprocess.TimeoutExpired, OSError):
            # Most likely no committer identity configured on this agent.
            # Retry with a fallback identity rather than lose the work; `-c`
            # overrides only this invocation and never touches repo config.
            # #1424: also reached if the worktree vanished mid-add/commit
            # (OSError) — the retry below will hit the same OSError and fall
            # through to the handler that records the dirty-worktree reason.
            try:
                _git(
                    wt_path,
                    "-c", "user.name=coord",
                    "-c", "user.email=coord@localhost",
                    *commit_args,
                    timeout=60.0,
                )
                committed = True
            except (_GitError, subprocess.TimeoutExpired, OSError) as e2:
                reason = (
                    f"{total} uncommitted file(s) could not be committed "
                    f"({e2}); worktree {wt_path} kept — recover the work "
                    "manually"
                )
                self._log_line(assignment, f"# cleanup: {reason} (#1394) (#1424)")
                self._record_dirty_worktree(assignment, reason)
                return False

        if not committed:  # pragma: no cover - defensive
            return False

        # The commit now lives on the branch ref in the shared object store,
        # so the worktree is expendable from here on regardless of what
        # happens next — removal is safe even if push_mode is "none" or the
        # push below fails.
        if push_mode == "none":
            # #1567: `coord stop`'s default. Do NOT touch the remote — the
            # operator stopping the assignment has typically decided this
            # work is unwanted, and pushing it would publish (and, if the
            # remote tip has since moved, replace) exactly what they meant to
            # stop. The commit is still safe: it's on the local branch ref,
            # which survives worktree removal and is recoverable with plain
            # git commands even after the worktree is gone.
            where = (
                f"committed to local branch {branch} only — NOT pushed "
                f"(coord stop default, #1567); the remote branch is "
                "unchanged. Recover with `git log " + branch + "` in the "
                "repo, or re-run with `--rescue` to publish it"
            )
            self._log_line(assignment, f"# cleanup: {where} (#1394) (#1567)")
            reason = (
                f"worker left {total} uncommitted file(s) ({tracked} "
                f"tracked, {untracked} new); {where} as a "
                f"{_WIP_COMMIT_PREFIX} commit. The work is UNVERIFIED — "
                "review it before testing or merging."
            )
            self._record_dirty_worktree(assignment, reason)
            return True

        if push_mode == "rescue":
            rescue_ref = f"rescue/{assignment.id}"
            push_spec = f"HEAD:refs/heads/{rescue_ref}"
            push_target_desc = f"{rescue_ref} (worker branch {branch} left untouched)"
        else:
            push_spec = "HEAD"
            push_target_desc = branch

        # Push is best-effort: its failure downgrades the message, not the
        # safety of the work. Never force — a rejected push just means the
        # rescue stays local-only rather than clobbering whatever is already
        # on the remote ref (#1567).
        pushed = False
        try:
            if push_mode == "rescue":
                _git(wt_path, "push", "origin", push_spec, timeout=60.0)
            else:
                _git(wt_path, "push", "-u", "origin", push_spec, timeout=60.0)
            pushed = True
        except (_GitError, subprocess.TimeoutExpired, OSError) as e:
            self._log_line(
                assignment, f"# cleanup: WIP rescue push failed ({e}) (#1394)"
            )

        where = (
            f"committed to {branch} and pushed to {push_target_desc}"
            if pushed
            else f"committed to local branch {branch} (push to "
            f"{push_target_desc} failed — the commit exists only on this "
            "agent)"
        )
        reason = (
            f"worker left {total} uncommitted file(s) ({tracked} tracked, "
            f"{untracked} new); {where} as a {_WIP_COMMIT_PREFIX} commit. "
            "The work is UNVERIFIED — review it before testing or merging."
        )
        self._log_line(assignment, f"# cleanup: {reason} (#1394)")
        self._record_dirty_worktree(assignment, reason)
        return True

    def _cleanup_lock_for(self, assignment_id: str) -> threading.Lock:
        """Return the (lazily-created) teardown lock for *assignment_id*.

        #1424: see the `_cleanup_locks` comment in `__init__` for why this
        exists — it serializes `cancel()` and `_reap()` when both race to
        clean up the same assignment's worktree.
        """
        with self._lock:
            lock = self._cleanup_locks.get(assignment_id)
            if lock is None:
                lock = threading.Lock()
                self._cleanup_locks[assignment_id] = lock
            return lock

    def _reap_complete_event(self, assignment_id: str) -> threading.Event:
        """Return the (lazily-created) reap-completion event for *assignment_id*.

        #1424: see the `_reap_complete` comment in `__init__`.
        """
        with self._lock:
            event = self._reap_complete.get(assignment_id)
            if event is None:
                event = threading.Event()
                self._reap_complete[assignment_id] = event
            return event

    def _cleanup_worktree(
        self, assignment: AgentAssignment, *, push_mode: str = "branch"
    ) -> None:
        """Remove the worktree for a finished assignment. Best-effort.

        #460 (Part 3 — synchronous teardown): always ensures git's worktree
        admin entries are pruned, even when the physical directory was already
        removed out-of-band.  Without the prune step a stale admin entry would
        keep the branch "checked out" from git's perspective, causing the next
        ``_setup_worktree`` to fail with a collision error until a ``prune``
        ran separately.

        #1394: never destroys uncommitted work.  A dirty worktree is either
        rescued into a WIP commit on the assignment branch (work-authoring
        assignment types) or kept on disk, and the outcome is recorded on the
        assignment — see :meth:`_rescue_uncommitted_work`.

        #1424: the whole body runs under a per-assignment lock (see
        `_cleanup_lock_for`) because `cancel()` and the `_reap` thread can
        both call this for the same assignment — without serialization the
        `wt_path.exists()` check here is a TOCTOU against the other thread's
        `git worktree remove`/`rmtree`.

        #1567: *push_mode* is forwarded to :meth:`_rescue_uncommitted_work`
        verbatim — see there for the "branch" / "none" / "rescue" meanings.
        Callers other than `cancel()` (the natural-completion `_reap` path,
        and the worktree-not-writable dispatch failure) keep the original
        "branch" default; only an explicit `coord stop` changes it.
        """
        if not assignment.worktree_path:
            return
        with self._cleanup_lock_for(assignment.id):
            self._cleanup_worktree_locked(assignment, push_mode=push_mode)

    def _cleanup_worktree_locked(
        self, assignment: AgentAssignment, *, push_mode: str = "branch"
    ) -> None:
        """Body of `_cleanup_worktree`, run under the assignment's lock."""
        wt_path = Path(assignment.worktree_path)
        repo_path = Path(assignment.spec.repo_path).expanduser()

        # #1694 (Part A): read the worktree's branch BEFORE it is removed.  A
        # stage that escaped its worktree and checked this branch out in the
        # base checkout is what leaves `~/src/<repo>` parked, and after the
        # removal there is no way left to tell which branch was ours.
        owned_branches = self._assignment_branches(assignment, wt_path)

        # #1394: check for uncommitted work BEFORE any destructive step.  The
        # removal below can still fall through to `shutil.rmtree` (inside
        # `_safe_remove_worktree`), which no amount of git-level care would
        # survive, so the gate has to be here — `_safe_remove_worktree`
        # deliberately does not duplicate it (#1693).
        if wt_path.exists() and not self._rescue_uncommitted_work(
            assignment, wt_path, push_mode=push_mode
        ):
            # Work preserved only inside this worktree — keep it.  Prune stale
            # admin entries for OTHER worktrees; this one stays registered on
            # purpose so `git worktree list` still shows where the work is.
            try:
                _git(repo_path, "worktree", "prune")
            except _GitError:
                pass
            self._restore_base_checkout(assignment, repo_path, owned_branches)
            return

        if wt_path.exists():
            # #1693: removal (including any rmtree fallback) goes through the
            # single chokepoint, sandboxed to `<state_dir>/worktrees`.  A
            # refusal is logged there and simply leaves the tree on disk.
            _safe_remove_worktree(
                repo_path,
                wt_path,
                log_path=assignment.log_path,
                sandbox_root=self.state_dir / "worktrees",
            )
        else:
            # Directory already gone (crash / rmtree before prune) — prune
            # the stale git admin entry so the branch is freed immediately.
            try:
                _git(repo_path, "worktree", "prune")
            except _GitError:
                pass

        self._restore_base_checkout(assignment, repo_path, owned_branches)

    @staticmethod
    def _assignment_branches(
        assignment: AgentAssignment, wt_path: Path
    ) -> set[str]:
        """Every branch name this assignment can legitimately claim (#1694).

        Used to scope the base-checkout restore below: the agent puts the base
        back only when it is parked on a branch that belongs to the assignment
        it just finished.  An operator's own checkout, parked on the
        operator's own branch, must be left exactly where they left it —
        switching it out from under them would be a different bug, not a fix.

        Sources, most to least authoritative: the worktree's live HEAD (read
        before teardown removes it), the branch the reap captured, and the
        explicit ``target_branch`` from the dispatch.
        """
        candidates = {
            _current_branch(wt_path),
            assignment.branch,
            assignment.spec.target_branch,
        }
        return {b for b in candidates if b and b != "HEAD"}

    def _restore_base_checkout(
        self,
        assignment: AgentAssignment,
        repo_path: Path,
        owned_branches: set[str],
    ) -> None:
        """Put the base checkout back on its default branch (#1694, Part A).

        The base checkout's steady state is the repo's default branch.  When a
        stage operates in ``~/src/<repo>`` directly rather than in its
        worktree — the #1642 family, and every briefing that says
        ``git checkout <branch>`` without saying where — the base is left
        parked on the work branch.  From then on ``git worktree add`` for that
        branch collides on this machine forever (#1693 refuses; #1659's
        docstring calls the state "routine", and a ``coord fix`` retry against
        the same branch makes it "near-certain").

        So: whatever checked a branch out in the base puts it back.  Scoped to
        branches this assignment owns, gated on the base being genuinely clean
        (:func:`_base_checkout_move_blockers`), and always logged.  A refusal
        is a no-op — never a deletion, never a discarded change.
        """
        if not owned_branches:
            return
        parked_on = _current_branch(repo_path)
        if parked_on is None or parked_on not in owned_branches:
            return  # detached, on the default branch, or somebody else's work
        default_branch = assignment.spec.branch or "main"
        moved_to = _restore_base_checkout_branch(
            repo_path,
            parked_on,
            default_branch,
            log_path=assignment.log_path,
            context="base-restore",
        )
        if moved_to is not None:
            self._log_line(
                assignment,
                f"# base-restore: base checkout {str(repo_path)!r} was left "
                f"parked on this assignment's branch {parked_on!r}; moved it "
                f"to {moved_to} (#1694). Nothing was deleted — the branch and "
                f"its commits are untouched.",
            )

    def _pull_then_spawn(self, assignment: AgentAssignment, repo_path: Path) -> None:
        """Pull each dep before spawning the worker. Logs to the assignment log.

        On any failure: mark the assignment FAILED and skip spawn. The HTTP
        client polls status to discover this.
        """
        with open(assignment.log_path, "w", encoding="utf-8") as log_fh:
            log_fh.write(
                f"# pulling dependencies: {assignment.spec.pull_repos}\n"
            )
            for dep_name in assignment.spec.pull_repos:
                dep_path_str = self.repo_paths.get(dep_name)
                if not dep_path_str:
                    msg = f"no repo_path configured for dependency {dep_name!r}"
                    log_fh.write(f"# pull failed: {msg}\n")
                    # Flush before flipping status: callers synchronize on
                    # status == FAILED as the barrier for "the log is
                    # complete" (#1343), so the write must be durable first.
                    log_fh.flush()
                    self._fail(assignment, msg)
                    return
                dep_path = Path(dep_path_str).expanduser()
                log_fh.write(f"# git -C {dep_path} pull --ff-only\n")
                log_fh.flush()
                try:
                    output = _git(dep_path, "pull", "--ff-only")
                except _GitError as e:
                    log_fh.write(f"# pull failed for {dep_name}: {e}\n")
                    # Same ordering requirement as above: flush before the
                    # status flip so status == FAILED is a valid barrier.
                    log_fh.flush()
                    self._fail(assignment, f"pull failed for {dep_name}: {e}")
                    return
                log_fh.write(output + "\n")
            log_fh.write("# all pulls succeeded; starting worker\n")
        self._spawn(assignment, repo_path)

    def _fail(self, assignment: AgentAssignment, error: str) -> None:
        with self._lock:
            assignment.status = FAILED
            assignment.error = error
            assignment.finished_at = time.time()
        self._persist()

    def _spawn(self, assignment: AgentAssignment, repo_path: Path) -> None:
        # #324/#425/#1796: provider-layer routing.
        #
        # `_resolve_provider` reproduces the SAME resolution `assign()`
        # already performed (without raising) before accepting this
        # assignment: local registry first, then the wire-carried
        # `provider_def` (#1796) for a config-free agent.  When it returns a
        # provider object, route through the provider seam:
        #   - PTY providers → background thread via ``_spawn_pty``.
        #   - All other providers (e.g. ClaudeProvider) → ``build_command``
        #     and ``initial_input`` instead of the legacy helpers.
        #
        # ``spec.provider is None`` is the ONLY case that still runs the
        # legacy code path below **unchanged** (byte-for-byte identical to
        # pre-#324) — #1796 closed the other silent-fallback case (a named
        # but unresolvable provider), which `assign()` now refuses before
        # `_spawn` is ever reached.
        spec = assignment.spec
        provider_obj = self._resolve_provider(spec)
        if provider_obj is not None:
            # Deferred import keeps the cycle latent at module load time.
            from coord.providers.claude_pty import ClaudePtyProvider  # noqa: PLC0415

            if isinstance(provider_obj, ClaudePtyProvider):
                # ``_spawn_pty`` polls for worker readiness for up to 5
                # seconds before writing the briefing.  ``_spawn`` is called
                # synchronously from the async HTTP ``assign`` handler in
                # ``agent_app.py`` (no run_in_executor), so a blocking call
                # here freezes the uvicorn event loop — status polls, cancel
                # calls, health checks all time out.  Mirror the
                # ``_pull_then_spawn`` pattern: run the PTY spawn on a
                # background daemon thread and return immediately.  The
                # assignment is already in ``self._assignments`` (PENDING)
                # by the time the HTTP handler responds; the thread flips
                # it to RUNNING once the child is up.
                thread = threading.Thread(
                    target=self._spawn_pty,
                    args=(assignment, repo_path, provider_obj),
                    daemon=True,
                    name=f"agent-pty-spawn-{assignment.id}",
                )
                thread.start()
                return

            # #324: Non-PTY provider path (e.g. ClaudeProvider).
            # Use the provider seam for both argv and initial stdin.
            # ``ClaudeProvider.build_command(spec)`` produces the same argv as
            # ``default_worker_command(spec)`` (parity enforced by test_providers.py),
            # so the agent output is byte-for-byte identical when the provider
            # is a ClaudeProvider.
            argv: list[str] = provider_obj.build_command(  # type: ignore[attr-defined]
                spec, resolved_model=spec.model
            )
            initial_input: bytes = provider_obj.initial_input(spec)  # type: ignore[attr-defined]
        else:
            # Legacy / no-config path — byte-identical to pre-#324.  Used
            # only when spec.provider is None (#1796) so that deployments
            # without a providers block in coordinator.yml are completely
            # unaffected by this change.
            argv = self.worker_command(assignment.spec)
            initial_input = _user_message_line(assignment.spec.briefing)

        log_fh = open(assignment.log_path, "a", encoding="utf-8")  # noqa: SIM115 — handle closed in _reap

        argv_oneline = shlex.join(argv).replace("\n", "\\n")
        header = (
            f"# agent={self.machine_name} repo={assignment.spec.repo_name} "
            f"issue=#{assignment.spec.issue_number} "
            f"argv={argv_oneline}\n"
        )
        log_fh.write(header)
        log_fh.flush()

        # Optionally route the spawn through a transient `bash -c 'exec ...'`
        # parent (#299). `exec` keeps the PID, so start_new_session, proc.pid,
        # the stdin pipe, and process-group kills all behave as for a bare
        # spawn — only the immediate parent of claude changes.
        spawn_argv = _maybe_bash_wrap(argv, self.bash_wrap_spawn)

        # #324: Merge provider.env() on top of the base worker env.  The PTY
        # path (_spawn_pty) does the equivalent at its own Popen site; both
        # paths must stay in sync.  For the legacy / no-provider path,
        # env() is effectively {} so the result is identical to calling
        # _worker_subprocess_env() directly (no-config parity preserved).
        #
        # #1783: pass `cwd=repo_path` so PWD is set to the worktree
        # explicitly rather than inherited from the agent daemon's own
        # environment.  Popen's `cwd=` below already confines the real
        # process, but a provider that trusts $PWD over getcwd() (e.g.
        # opencode) would otherwise resolve against whatever directory the
        # daemon happened to start in — worktree confinement for those
        # providers must not depend on `bash_wrap_spawn` recomputing PWD as
        # a side effect (see `_maybe_bash_wrap`).
        _spawn_env = _worker_subprocess_env(cwd=repo_path, assignment_id=assignment.id)
        # #1402: shared per-repo cargo target dir (see coord.cargo_cache).
        # Must stay in sync with the PTY path in ``_spawn_pty``.
        _spawn_env.update(
            cargo_cache.cargo_env(
                assignment.spec.repo_name, self.state_dir, _spawn_env
            )
        )
        if provider_obj is not None:
            _spawn_env.update(provider_obj.env())

        # #2569: re-strip AFTER every env.update() above — a provider's own
        # `env:` override (operator-authored in coordinator.yml) could
        # otherwise reintroduce the pinned venv's bin dir onto PATH and
        # silently undo the strip _worker_subprocess_env already performed.
        _strip_venv_bins_from_path(_spawn_env, _pinned_venv_bin_dirs(_spawn_env))

        try:
            proc = subprocess.Popen(
                spawn_argv,
                cwd=str(repo_path),
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE,
                start_new_session=True,
                # #402: strip the agent's own venv from the worker's PATH so a
                # worker's `pip install -e .` can't clobber the agent's runtime
                # venv from a soon-to-be-reaped worktree.
                env=_spawn_env,
            )
        except (FileNotFoundError, OSError) as e:
            log_fh.write(f"\n# spawn failed: {e}\n")
            log_fh.close()
            with self._lock:
                assignment.status = FAILED
                assignment.error = str(e)
                assignment.finished_at = time.time()
            self._persist()
            return

        # Send the initial briefing as the first stream-json user message.
        # If this fails (worker exited immediately), let `_reap` capture the
        # exit code — we just stop trying to write.
        #
        # #315: this line does double duty — for a regular dispatch it IS the
        # initial briefing; for a --resume re-dispatch (`spec.resume_session_id`
        # set) it is the next user turn written into the restored conversation.
        # Either way the worker sees it as a stream-json user message.
        #
        # #324: ``initial_input`` is either from ``provider.initial_input(spec)``
        # (provider path) or ``_user_message_line(spec.briefing)`` (legacy path).
        # Both produce the same bytes for ClaudeProvider — parity maintained.
        try:
            assert proc.stdin is not None
            proc.stdin.write(initial_input)
            proc.stdin.flush()
            # #2306: a provider that does not support stdin message injection
            # (``capabilities().inject == False``, e.g. OpenCodeProvider,
            # which takes its briefing on argv) never writes to or closes
            # this pipe again.  Leaving it open blocks the worker forever —
            # it never sees an EOF on stdin — until the 600s TTFT watchdog
            # kills it having emitted zero bytes.  Close it here so such a
            # worker gets its EOF immediately after the (empty) initial
            # write.  ``provider_obj`` is ``None`` on the legacy path
            # (``spec.provider is None``, pre-#324 behaviour) — treat that
            # as claude/inject-capable and leave stdin open, matching
            # ``inject_message``'s existing "no provider info => allow"
            # fallback.
            if provider_obj is not None and not provider_obj.capabilities().inject:  # type: ignore[attr-defined]
                proc.stdin.close()
        except (BrokenPipeError, OSError) as e:
            log_fh.write(f"\n# failed to send initial briefing: {e}\n")

        with self._lock:
            assignment.status = RUNNING
            assignment.pid = proc.pid
            assignment.started_at = time.time()
            self._processes[assignment.id] = proc

        # #1424: register (unset) the reap-completion event BEFORE starting
        # the thread, not lazily inside `_reap_guarded`. `wait_for` treats a
        # missing event as "no reap thread was ever spawned for this
        # assignment, nothing to wait for" — if the entry only appeared once
        # the thread reached its `finally` clause, a `wait_for` call that
        # raced the thread start would misread "hasn't gotten there yet" as
        # "never going to happen" and return before teardown/rescue actually
        # finished, which is the exact race this event exists to close.
        self._reap_complete_event(assignment.id)
        thread = threading.Thread(
            target=self._reap_guarded,
            args=(assignment.id, proc, log_fh, assignment.log_path),
            daemon=True,
            name=f"agent-reap-{assignment.id}",
        )
        with self._lock:
            self._threads[assignment.id] = thread
        thread.start()
        self._persist()

    def _spawn_pty(
        self,
        assignment: AgentAssignment,
        repo_path: Path,
        provider: "ClaudePtyProvider",
    ) -> None:
        """ADDITIVE PTY spawn path for :class:`ClaudePtyProvider` (#425).

        Spawns the interactive ``claude`` CLI attached to a pseudo-terminal
        (via :mod:`pty` from the Python standard library), streams the PTY
        master fd's byte output to the same log file the legacy
        ``claude -p`` path writes, waits briefly for the worker to emit its
        first bytes (a sign the TUI has finished initialising), then writes
        :meth:`Provider.initial_input` to the PTY master — exactly the bytes
        a human would type at the prompt followed by ``\\n``.

        The reap logic (:meth:`_reap`) is reused unchanged: it watches the
        process group, captures branch state, and pushes the worker's
        commits.  Logical completion is left to follow-up issue #426; this
        PR only wires the spawn side.

        Note: ``self.bash_wrap_spawn`` (the daemon-spawn stall mitigation
        for anthropics/claude-code#56268) is **deliberately NOT applied**
        on the PTY path.  The bash-wrap inserts a transient
        ``bash -c 'exec <argv>'`` parent, but that only behaves correctly
        when the child inherits regular pipes from its parent — wrapping
        an interactive ``claude`` whose stdio is a PTY slave fd breaks the
        TTY allocation and the worker either fails to start its TUI or
        loses its line-discipline.  PTY workers are also currently gated
        to non-mutating spec types (the safety gate in :meth:`assign`
        refuses write-capable types on any provider whose capabilities
        report ``enforces_deny_list=False``), so the daemon-spawn stall
        risk profile is narrower than for ``claude -p`` workers.
        """
        spec = assignment.spec
        # Build the worker argv through the provider seam.  The PTY argv
        # has no -p / stream-json flags — interactive claude reads from
        # the TTY and renders TUI output.
        argv = provider.build_command(
            spec, resolved_model=spec.model
        )

        log_fh = open(assignment.log_path, "a", encoding="utf-8")  # noqa: SIM115 — closed in _reap
        argv_oneline = shlex.join(argv).replace("\n", "\\n")
        header = (
            f"# agent={self.machine_name} repo={spec.repo_name} "
            f"issue=#{spec.issue_number} provider={spec.provider} "
            f"argv={argv_oneline}\n"
        )
        log_fh.write(header)
        log_fh.flush()

        # Open the PTY.  ``master_fd`` stays in this (parent) process; the
        # ``slave_fd`` is dup'd into the child's stdin/stdout/stderr and
        # closed in the parent immediately after Popen returns.  The child
        # sees a real TTY so interactive ``claude`` enables its TUI path.
        # We deliberately use the Python stdlib ``pty`` module rather than
        # the ``portable-pty`` Rust crate referenced in the #425 issue
        # description (the crate powers quadraui #279 / vimcode's engine,
        # which served only as a conceptual reference).  Rationale: no new
        # runtime dependency, fewer moving parts, and the coordinator
        # already targets Linux/macOS agent machines.  The stdlib module
        # is Unix-only — Windows agents are not in scope for #425; if
        # they ever are, the import will fail loudly at this site rather
        # than at module load (the import is deferred to keep
        # ``import coord.agent`` working on non-Unix during static
        # analysis / docs builds).
        import pty  # stdlib, Unix-only — deferred for platform safety  # noqa: PLC0415
        master_fd, slave_fd = pty.openpty()

        # Build the worker environment and spawn the child.  The fd pair
        # is allocated above, so any failure between ``openpty()`` and a
        # successful ``Popen()`` would leak both descriptors unless we
        # guard the entire setup with ``try``.  ``_worker_subprocess_env``
        # and ``provider.env()`` should not raise in practice (the former
        # just copies ``os.environ``; the latter returns a copy of the
        # provider definition's ``env:`` overrides, merged on top of the
        # base environment below — per #1706, no longer always ``{}`` for
        # the PTY provider; it's only ``{}`` when the definition sets no
        # ``env:`` entries), but a defensive wrap costs nothing and
        # survives future provider implementations.  We track ``proc``
        # explicitly so the ``BaseException`` guard below can tell whether
        # the child has taken ownership of the fds yet.
        proc: subprocess.Popen | None = None
        try:
            # ``TERM`` may be missing in systemd-managed agent processes
            # — default to ``xterm-256color`` so the TUI renders.
            # Provider env() entries take precedence.
            #
            # #1783: `cwd=repo_path` sets PWD to the worktree explicitly
            # instead of inheriting the agent daemon's own PWD.  The PTY
            # path never routes through `_maybe_bash_wrap` (see the
            # docstring above), so unlike the headless path it never got an
            # incidental PWD correction — confinement here must not depend
            # on a provider trusting $PWD over getcwd() by accident.
            env = _worker_subprocess_env(cwd=repo_path, assignment_id=assignment.id)
            env.setdefault("TERM", "xterm-256color")
            # #1402: point cargo at this machine's shared per-repo target dir
            # so the build cache survives worktree cleanup and is reused by
            # the next worker.  Applied before ``provider.env()`` so a
            # provider that sets CARGO_TARGET_DIR explicitly still wins.
            env.update(
                cargo_cache.cargo_env(assignment.spec.repo_name, self.state_dir, env)
            )
            env.update(provider.env())
            # #2569: see the headless spawn path's identical re-strip for
            # the rationale — a provider's own env: override could
            # otherwise reintroduce the pinned venv's bin dir onto PATH.
            _strip_venv_bins_from_path(env, _pinned_venv_bin_dirs(env))

            proc = subprocess.Popen(
                argv,
                cwd=str(repo_path),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
                close_fds=True,
                env=env,
            )
        except (FileNotFoundError, OSError) as e:
            os.close(master_fd)
            os.close(slave_fd)
            log_fh.write(f"\n# pty spawn failed: {e}\n")
            log_fh.close()
            with self._lock:
                assignment.status = FAILED
                assignment.error = str(e)
                assignment.finished_at = time.time()
            self._persist()
            return
        except BaseException:
            # Defensive catch-all for non-OSError failures from
            # ``provider.env()`` or future setup steps (e.g. a misbehaving
            # provider raising ``ValueError`` / ``RuntimeError``).  Without
            # this clause the fds leak and the process accumulates orphan
            # PTY pairs.  We only close the fds if the child has not yet
            # been launched — once ``Popen`` returns successfully the child
            # owns the slave fd and the parent's master fd will be cleaned
            # up by the pump thread.
            if proc is None:
                for fd in (master_fd, slave_fd):
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                try:
                    log_fh.close()
                except OSError:
                    pass
            raise

        # The child owns ``slave_fd`` now — close the parent's copy or the
        # pump thread will block forever on EOF.
        try:
            os.close(slave_fd)
        except OSError:
            pass

        # PTY pump: read bytes from the master fd and append them to the
        # log file.  Opens its own append handle so it does not race with
        # the ``log_fh`` writes from this method or from ``_reap``.  On
        # EOF (child closed all copies of slave) the thread exits, closes
        # the master fd, and stamps the provider's result marker so
        # ``_log_has_result`` in the reap thread sees logical completion.
        log_path = assignment.log_path
        result_marker = provider.result_marker()

        def _pump() -> None:
            try:
                with open(log_path, "ab") as pump_fh:
                    while True:
                        try:
                            data = os.read(master_fd, 4096)
                        except OSError:
                            break
                        if not data:
                            break
                        try:
                            pump_fh.write(data)
                            pump_fh.flush()
                        except OSError:
                            break
                    # Stamp the result marker AFTER the PTY closes so the
                    # reap thread's _log_has_result poll observes logical
                    # completion.  Best-effort: an OSError here just means
                    # the reap thread will fall back to its max-wait timer.
                    try:
                        pump_fh.write(b"\n" + result_marker.encode("utf-8") + b"\n")
                        pump_fh.flush()
                    except OSError:
                        pass
            finally:
                try:
                    os.close(master_fd)
                except OSError:
                    pass

        pump_thread = threading.Thread(
            target=_pump,
            daemon=True,
            name=f"agent-pty-pump-{assignment.id}",
        )
        pump_thread.start()

        # Readiness + briefing PRE-FILL for the interactive TUI.  Unlike the
        # stream-json path we cannot just write the briefing to stdin:
        # interactive ``claude`` only accepts a (multi-line) briefing as a
        # bracketed paste, and only AFTER it has both enabled bracketed-paste
        # input AND finished drawing its first frame.  So:
        #   (1) wait (≤5s) for the bracketed-paste-enable DECSET (ESC[?2004h)
        #       to appear in the log;
        #   (2) wait for the init render to go quiet (log size stable) — the
        #       enable marker fires while the TUI is still drawing, and a
        #       paste sent at that instant is silently dropped;
        #   (3) paste the briefing (``initial_input`` returns the
        #       bracketed-paste block — NO submit key).
        # The pre-fill steps were verified live against interactive ``claude``
        # (#425 smoke); see ClaudePtyProvider for the byte-level rationale.
        #
        # #437: ToS-COMPLIANCE — we deliberately do NOT submit the briefing
        # on the operator's behalf.  The human launching this session via
        # ``coord assign --interactive`` sees the briefing PRE-FILLED in the
        # input box and presses Enter themselves.  No coordinator-side
        # auto-submit, no content-based completion detection, no
        # auto-termination on output, no TTY scraping to advance pipeline
        # state.  The session is HUMAN-CLOSED.  This is the structural
        # difference from #425's automated submit path that was retired
        # alongside #426 for Anthropic ToS §3.7 compliance.
        #
        # We poll the log rather than the master fd directly to avoid stealing
        # bytes from the pump thread.  If a marker never appears we paste
        # anyway after the cap — degraded, but no worse than blind pasting.
        #
        # KNOWN LIMITATION: if the interactive `claude` process exits before
        # the readiness window (auth failure, crash, immediate misconfig), the
        # pump thread sees EIO on ``master_fd``, stamps the result marker, and
        # closes ``master_fd``; the ``os.write(master_fd, …)`` here then raises
        # ``OSError`` and the pre-fill is silently lost.  The assignment is
        # reaped quickly — ``_reap`` calls ``proc.wait()`` and the child has
        # already exited, so status flips to FAILED in seconds rather than
        # waiting for ``_REAP_MAX_WAIT``.  The safety gate in :meth:`assign`
        # limits PTY workers to non-mutating spec types, so a silently-lost
        # pre-fill is a nuisance rather than a correctness problem.
        from coord.providers.claude_pty import (  # noqa: PLC0415
            BRACKETED_PASTE_ENABLE,
            INPUT_BOX_MARKER_BYTES,
            briefing_fingerprint,
            paste_landed_bytes,
        )

        initial_input = provider.initial_input(spec)
        if initial_input:
            # (1) wait for bracketed-paste input to be enabled.
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                try:
                    with open(log_path, "rb") as _rf:
                        if BRACKETED_PASTE_ENABLE in _rf.read():
                            break
                except OSError:
                    pass
                time.sleep(0.05)
            # (2) wait for the *input box* to render AND the init render to
            # go quiet before pasting (#865).  Bare quiescence let an async
            # startup banner ("Fable 5 is back", MCP-auth notices) that goes
            # briefly static mid-paint pass for "ready" — requiring
            # INPUT_BOX_MARKER_BYTES too means we only call it ready once
            # the actual prompt box has been drawn.  If the marker never
            # appears (older CLI, unusual render) the cap below still
            # fires, so this degrades to the pre-#865 behaviour rather than
            # hanging.
            quiet_cap = time.monotonic() + _PTY_READY_QUIESCE_CAP_S
            last_size = -1
            last_change = time.monotonic()
            while time.monotonic() < quiet_cap:
                try:
                    with open(log_path, "rb") as _rf:
                        _tail = _rf.read()
                except OSError:
                    _tail = b""
                size = len(_tail)
                marker_seen = INPUT_BOX_MARKER_BYTES in _tail
                if size != last_size:
                    last_size = size
                    last_change = time.monotonic()
                else:
                    quiet_for = time.monotonic() - last_change
                    threshold = (
                        _PTY_READY_QUIESCE_S
                        if marker_seen
                        else _PTY_READY_QUIESCE_NO_MARKER_S
                    )
                    if quiet_for >= threshold:
                        break
                time.sleep(0.05)

            # (3) PRE-FILL, THEN VERIFY — retry on a miss (#865 / #896).
            # #865 root defect: fire-and-forget with no verification at all
            # let the ~30% drop-rate through.  #896 root defect: the bare
            # fingerprint check gave false negatives when Claude Code
            # collapsed a large paste into a chip ("❯ [Pasted text #1 +NNN
            # lines]") or scrolled the input box to the tail (fingerprint
            # from the start not visible).  Each attempt writes the
            # bracketed-paste block (populates the TUI's input box; the
            # operator presses Enter to submit — #437 explicitly does NOT
            # write a trailing carriage return, the structural change that
            # makes this path human-attended rather than agentic), then
            # re-reads the log tail and checks via the broadened
            # ``paste_landed_bytes`` predicate.  On a miss, Escape + Ctrl-U
            # are written first to clear any stacked chips before retrying.
            # No content-based completion detection follows; no scraper
            # inspects the TTY for sentinels; the session terminates when
            # the human exits ``claude``.
            fingerprint = briefing_fingerprint(spec.briefing)
            landed = False
            write_failed = False
            for _attempt in range(1, _PTY_INJECT_MAX_ATTEMPTS + 1):
                if _attempt > 1:
                    # Idempotent retry (#896): clear the input box before
                    # re-pasting so stacked chips don't accumulate if a
                    # false-negative slips through.
                    try:
                        os.write(master_fd, b"\x1b\x15")  # Escape + Ctrl-U
                    except OSError:
                        pass
                    time.sleep(0.05)
                try:
                    os.write(master_fd, initial_input)
                except OSError as e:
                    write_failed = True
                    try:
                        log_fh.write(f"\n# pty: failed to pre-fill briefing: {e}\n")
                        log_fh.flush()
                    except OSError:
                        pass
                    break
                time.sleep(_PTY_INJECT_VERIFY_SETTLE_S)
                try:
                    with open(log_path, "rb") as _rf:
                        _tail = _rf.read()
                except OSError:
                    _tail = b""
                if paste_landed_bytes(_tail, fingerprint):
                    landed = True
                    break
                if _attempt < _PTY_INJECT_MAX_ATTEMPTS:
                    time.sleep(_PTY_INJECT_RETRY_BACKOFF_S)

            if not landed and not write_failed and fingerprint:
                try:
                    # #865 (review follow-up): a bare "# pty: ..." comment is
                    # only visible to someone who manually tails this exact
                    # log — it does NOT match the STATUS:/STUCK: convention
                    # coord.progress actually scans for (see
                    # coord/progress.py's STUCK_RE), so an exhausted retry on
                    # the remote PTY relay path was invisible to `coord
                    # status`, the dashboard, and the board. Emit a STUCK:
                    # line so this surfaces the same way any other worker
                    # stall does, in addition to the raw comment for anyone
                    # reading the log directly.
                    log_fh.write(
                        "\nSTUCK: briefing injection unverified after "
                        f"{_PTY_INJECT_MAX_ATTEMPTS} attempt(s) — the "
                        "briefing may not have landed in the input box; "
                        "the operator should check the session and paste "
                        "the briefing manually if it's empty\n"
                    )
                    log_fh.write(
                        f"\n# pty: briefing injection unverified after "
                        f"{_PTY_INJECT_MAX_ATTEMPTS} attempt(s) — the "
                        f"briefing may not have landed in the input box\n"
                    )
                    log_fh.flush()
                except OSError:
                    pass

        with self._lock:
            assignment.status = RUNNING
            assignment.pid = proc.pid
            assignment.started_at = time.time()
            self._processes[assignment.id] = proc

        # #1424: register (unset) the reap-completion event BEFORE starting
        # the thread, not lazily inside `_reap_guarded`. `wait_for` treats a
        # missing event as "no reap thread was ever spawned for this
        # assignment, nothing to wait for" — if the entry only appeared once
        # the thread reached its `finally` clause, a `wait_for` call that
        # raced the thread start would misread "hasn't gotten there yet" as
        # "never going to happen" and return before teardown/rescue actually
        # finished, which is the exact race this event exists to close.
        self._reap_complete_event(assignment.id)
        thread = threading.Thread(
            target=self._reap_guarded,
            args=(assignment.id, proc, log_fh, assignment.log_path),
            daemon=True,
            name=f"agent-reap-{assignment.id}",
        )
        with self._lock:
            self._threads[assignment.id] = thread
        thread.start()
        self._persist()

    def _commits_ahead(self, wt_path: Path, base: str) -> int | None:
        """Number of commits HEAD is ahead of *base* in the worktree.

        Delegates to the module-level :func:`_commits_ahead` primitive (#466)
        which is also used by :func:`coord.interactive.finalize_interactive_exit`
        so the two callers share a single implementation.
        """
        return _commits_ahead(wt_path, base)

    def _reap_guarded(
        self,
        assignment_id: str,
        proc: subprocess.Popen,
        log_fh,
        log_path: str,
    ) -> None:
        """Run `_reap`, logging (never silently swallowing) any exception.

        #1424: `_reap` runs on a daemon thread with no caller to propagate
        an exception to. An uncaught exception there (e.g. the
        `FileNotFoundError` from `_rescue_uncommitted_work` racing a
        worktree that vanished mid-teardown) previously surfaced only via
        Python's default `threading.excepthook` dump to stderr — in tests
        that shows up as a `PytestUnhandledThreadExceptionWarning`; in
        production, with nothing watching that stderr, it is simply gone.
        Logging at ERROR here routes it through this process's normal
        logging config instead, without changing `_reap`'s behaviour on the
        happy path.

        The `finally` also sets this assignment's reap-completion event —
        regardless of whether `_reap` returned normally or raised — so
        `wait_for` can tell "status flipped terminal" apart from "teardown
        (worktree cleanup + WIP rescue commit/push) has actually finished".
        """
        try:
            self._reap(assignment_id, proc, log_fh, log_path)
        except Exception:
            _log.error(
                "agent-reap-%s: unhandled exception in _reap",
                assignment_id,
                exc_info=True,
            )
        finally:
            self._reap_complete_event(assignment_id).set()

    def _reap(
        self,
        assignment_id: str,
        proc: subprocess.Popen,
        log_fh,
        log_path: str,
    ) -> None:
        # #324/#1796: Resolve the provider for this assignment so
        # _wait_for_proc_or_result uses provider.result_marker() and the
        # session-id parse below uses provider.parse_log().  Look up BEFORE
        # the wait so the marker check uses the right sentinel from the
        # start.  For ClaudeProvider these calls delegate to the same
        # functions used previously — behavior is byte-identical; the seam
        # is now complete for future providers.  Uses the best-effort
        # resolver (#1796): by the time `_reap` runs, this assignment
        # already spawned successfully via the SAME spec, so a resolution
        # failure here degrades to the default claude-shaped marker/parser
        # rather than raising out of the reap thread.
        with self._lock:
            _reap_start = self._assignments.get(assignment_id)
        _reap_provider = None
        if _reap_start is not None:
            _reap_provider = self._resolve_provider_best_effort(_reap_start.spec)

        # Build a log_has_result callable from the provider's result_marker.
        # When the provider's marker matches the built-in default we reuse
        # _log_has_result (which adds per-line JSON validation to avoid false
        # positives).  A provider with a different marker gets a simple byte-
        # substring check that also recognises the PTY sentinel so PTY-spawned
        # assignments are reaped correctly regardless of provider.
        if _reap_provider is not None:
            _marker_bytes = _reap_provider.result_marker().encode()
            if _marker_bytes == _RESULT_LINE_MARKER:
                # Default marker — reuse the JSON-validating checker.
                _log_has_result_fn = _log_has_result
            else:
                # Non-default marker — plain substring check per line.
                def _log_has_result_fn(  # type: ignore[misc]
                    lp: str, *, _m: bytes = _marker_bytes
                ) -> bool:
                    try:
                        with open(lp, "rb") as _f:
                            for _line in _f:
                                if _line.lstrip().startswith(_PTY_RESULT_LINE_MARKER):
                                    return True
                                if _m in _line:
                                    return True
                        return False
                    except OSError:
                        return False
        else:
            _log_has_result_fn = _log_has_result

        # #2131: arm the per-leg spend ceiling when the coordinator sent one.
        # The meter is built here (not inside the wait loop) so the same
        # instance survives the whole run and can be re-read afterwards for
        # the reason string — it reads the log incrementally, so a full
        # re-parse never happens on any poll.
        _cost_ceiling: float | None = None
        _cost_meter = None
        if _reap_start is not None:
            _raw_ceiling = getattr(_reap_start.spec, "cost_ceiling_usd", None)
            if isinstance(_raw_ceiling, (int, float)) and _raw_ceiling > 0:
                try:
                    from coord.spend_ceiling import LiveCostMeter  # noqa: PLC0415

                    _cost_ceiling = float(_raw_ceiling)
                    _cost_meter = LiveCostMeter(log_path)
                except Exception:  # noqa: BLE001 — fail open, never break reap
                    _cost_ceiling = None
                    _cost_meter = None

        # #2638: resolve the wall-clock runtime ceiling for this leg —
        # `AssignmentSpec.runtime_ceiling_s` (an explicit per-assignment
        # override; a non-positive value there disables the ceiling for
        # THIS leg only) wins over this agent's own configured default
        # (`self.runtime_ceiling_s`, generous-but-on unless the operator set
        # it to 0/None).
        _runtime_ceiling: float | None = self.runtime_ceiling_s
        if _reap_start is not None:
            _raw_runtime_ceiling = getattr(_reap_start.spec, "runtime_ceiling_s", None)
            if isinstance(_raw_runtime_ceiling, (int, float)):
                _runtime_ceiling = (
                    float(_raw_runtime_ceiling) if _raw_runtime_ceiling > 0 else None
                )

        # Use a polling wait that handles claude-cli's well-known habit of
        # not exiting after emitting its final result event (a child of the
        # process group keeps the session alive). See #228.
        exit_code = _wait_for_proc_or_result(
            proc, log_path,
            first_output_timeout=self.first_output_timeout,
            log_has_result=_log_has_result_fn,
            cost_ceiling_usd=_cost_ceiling,
            read_cost_usd=_cost_meter.read if _cost_meter is not None else None,
            runtime_ceiling_s=_runtime_ceiling,
        )
        log_fh.close()

        # #2131: turn the ceiling kill into the stable, greppable diagnostic
        # every downstream consumer keys off. Uses the meter's LAST observed
        # value rather than re-reading, so the number in the reason is
        # exactly the one the kill decision was made on.
        _spend_ceiling_reason: str | None = None
        if exit_code == SPEND_CEILING_EXIT and _cost_ceiling is not None:
            try:
                from coord.spend_ceiling import (  # noqa: PLC0415
                    format_spend_ceiling_reason,
                )

                _observed = (_cost_meter.last if _cost_meter is not None else None)
                _spend_ceiling_reason = format_spend_ceiling_reason(
                    _observed if _observed is not None else _cost_ceiling,
                    _cost_ceiling,
                    getattr(_reap_start.spec, "type", None) if _reap_start else None,
                )
            except Exception:  # noqa: BLE001 — best-effort, never break reap
                _spend_ceiling_reason = None

        # #2638: same stable-diagnostic treatment for a wall-clock runtime-
        # ceiling kill. `assignment.started_at` (stamped at spawn time with
        # `time.time()` — real wall clock) lets this report the actual
        # overshoot rather than just repeating the ceiling value.
        _runtime_ceiling_reason: str | None = None
        if exit_code == RUNTIME_CEILING_EXIT:
            try:
                _ceiling_for_reason = (
                    _runtime_ceiling
                    if _runtime_ceiling is not None
                    else _DEFAULT_RUNTIME_CEILING_S
                )
                _wall_elapsed_for_reason = _ceiling_for_reason
                if _reap_start is not None and _reap_start.started_at is not None:
                    _wall_elapsed_for_reason = max(
                        _ceiling_for_reason, time.time() - _reap_start.started_at
                    )
                _runtime_ceiling_reason = format_runtime_ceiling_reason(
                    _wall_elapsed_for_reason, _ceiling_for_reason
                )
            except Exception:  # noqa: BLE001 — best-effort, never break reap
                _runtime_ceiling_reason = None

        # #2638: host-sleep detection has no single "ceiling" value to
        # report against — the diagnostic IS the divergence itself. The wait
        # loop already wrote the exact wall/monotonic deltas it killed on
        # into the worker's own log; re-read them here rather than re-derive
        # (nothing else in `_reap`'s scope has them). Best-effort: if the
        # line can't be found/parsed for any reason, fall back to the
        # threshold itself — still distinguishable from a generic crash,
        # only less precise.
        _host_sleep_reason: str | None = None
        if exit_code == HOST_SLEEP_EXIT:
            _wall_delta_for_reason = _HOST_SLEEP_DIVERGENCE_S
            _mono_delta_for_reason = 0.0
            try:
                with open(log_path, "rb") as _f:
                    _f.seek(max(0, os.path.getsize(log_path) - 4096))
                    _tail = _f.read()
                # `-?` on both groups: an NTP backward step could in theory
                # make either delta format as negative (`f"{-1.0:.0f}"` ==
                # `"-1"`) — match it anyway rather than silently falling back
                # to the less-precise defaults below.
                _match = re.search(
                    rb"host sleep detected \(wall clock advanced (-?\d+)s vs "
                    rb"(-?\d+)s monotonic",
                    _tail,
                )
                if _match:
                    _wall_delta_for_reason = float(_match.group(1))
                    _mono_delta_for_reason = float(_match.group(2))
            except OSError:
                pass
            try:
                _host_sleep_reason = format_host_sleep_reason(
                    _wall_delta_for_reason, _mono_delta_for_reason
                )
            except Exception:  # noqa: BLE001 — best-effort, never break reap
                _host_sleep_reason = None

        # #1461: detect a usage-limit kill from the tail of the transcript.
        # Done HERE — immediately after the worker's own process has exited
        # and BEFORE any coordinator bookkeeping (push attempts, advisory
        # diagnostics, etc.) appends its own lines to the same log file —
        # so the "last line" the detector sees is genuinely the worker's own
        # last line, never a later git-error/comment the coordinator wrote.
        # Also gated on the log lacking its own terminating `result` event
        # (the same `_log_has_result_fn` used by the wait loop above) so this
        # can NEVER fire on a normal completion that merely *discusses* usage
        # limits somewhere mid-conversation (this very issue's own worker
        # transcript, for instance) — a real kill truncates before any
        # `result` line, whereas every normal DONE/ADVISORY transcript ends
        # with one.
        _usage_limit_reason: str | None = None
        if not _log_has_result_fn(log_path):
            try:
                from coord.worker_events import (  # noqa: PLC0415
                    detect_usage_limit_kill_in_log,
                    format_usage_limit_reason,
                )
                _kill = detect_usage_limit_kill_in_log(log_path)
                if _kill is not None:
                    _usage_limit_reason = format_usage_limit_reason(_kill)
                    try:
                        with open(log_path, "a", encoding="utf-8") as reopen:
                            reopen.write(
                                "# reap: usage-limit kill detected — "
                                f"{_usage_limit_reason} (#1461)\n"
                            )
                    except OSError:
                        pass
            except Exception:  # noqa: BLE001 — best-effort, never break reap
                pass

        # #1584: parse the worker's full terminal log ONCE here and share the
        # result with the claude_session_id capture further down (both used
        # to run an independent `tail_bytes=0` full-transcript parse of the
        # SAME log on every single reap — wasteful, and worth avoiding since
        # transcripts can be large). Uses the same provider resolved for the
        # wait loop above (`_reap_provider`, falling back to the plain
        # `coord.worker_events.parse_log`) so a non-default provider's own
        # log shape is honoured consistently for both consumers.
        # `WorkerSummary.is_error`/`.terminal_reason`/`.api_error_status`/
        # `.result_text` are overwritten on every `result` event a full parse
        # walks through (see `update_summary`), so `is_error` below reflects
        # only the LAST one: a worker that hit a transient error, retried
        # internally, and finished cleanly is unaffected — its final
        # `result` event carries no `is_error`. Best-effort throughout; any
        # parse failure (or a non-stream-json log) just leaves this `None`,
        # and each consumer falls back to its own pre-#1584 behaviour.
        _worker_summary = None
        if log_path:
            try:
                from coord.worker_events import is_stream_json  # noqa: PLC0415
                if is_stream_json(log_path):
                    if _reap_provider is not None:
                        _worker_summary = _reap_provider.parse_log(log_path, tail_bytes=0)
                    else:
                        from coord.worker_events import parse_log  # noqa: PLC0415
                        _worker_summary = parse_log(log_path, tail_bytes=0)
            except Exception:  # noqa: BLE001 — best-effort, never break reap
                _worker_summary = None

        # Treat `is_error: true` on that LAST `result` event as authoritative
        # — a transient upstream failure (529 Overloaded, 500, a network
        # drop) that killed the worker is NEVER `done`, whatever the
        # wrapper's own exit code says.
        _result_is_error = False
        _api_error_reason: str | None = None
        if _worker_summary is not None and _worker_summary.is_error:
            try:
                from coord.worker_events import format_api_error_reason  # noqa: PLC0415
                _result_is_error = True
                _api_error_reason = format_api_error_reason(
                    terminal_reason=_worker_summary.terminal_reason,
                    api_error_status=_worker_summary.api_error_status,
                    result_text=_worker_summary.result_text,
                )
                try:
                    with open(log_path, "a", encoding="utf-8") as reopen:
                        reopen.write(
                            "# reap: terminal API error detected — "
                            f"{_api_error_reason} (#1584)\n"
                        )
                except OSError:
                    pass
            except Exception:  # noqa: BLE001 — best-effort, never break reap
                pass

        # Capture the branch the worker left the repo on. For worktree-based
        # assignments we read from the worktree; for legacy assignments (no
        # worktree_path) we fall back to the main repo clone.
        captured_branch: str | None = None
        # #1797: set below iff the belt-and-suspenders push at the end of
        # this reap raised. Declared here (not inside the `if` that attempts
        # the push) so the status decision further down can reference it
        # unconditionally, mirroring `_zero_commit_reason`.
        _push_failure_reason: str | None = None
        with self._lock:
            assignment = self._assignments.get(assignment_id)
        if assignment is not None:
            # Determine where to read branch info from
            if assignment.worktree_path:
                check_path = Path(assignment.worktree_path)
            else:
                check_path = Path(assignment.spec.repo_path).expanduser()

            if check_path.exists():
                try:
                    head = _git(check_path, "rev-parse", "--abbrev-ref", "HEAD")
                except _GitError:
                    head = ""
                if head and head != "HEAD":
                    # `HEAD` here means detached; ignore.
                    spec_default = assignment.spec.branch
                    if spec_default is None or head != spec_default:
                        captured_branch = head

            # Best-effort push of the worktree branch.  The worker is
            # responsible for pushing per its briefing, so this is a
            # belt-and-suspenders safety net only.  We use a generous
            # timeout (60 s) but MUST NOT let a hung push block the
            # status update — so we catch both _GitError *and*
            # subprocess.TimeoutExpired and treat both as non-fatal.
            if assignment.worktree_path:
                wt_path = Path(assignment.worktree_path)
                if wt_path.exists() and exit_code == 0:
                    try:
                        with open(assignment.log_path, "a", encoding="utf-8") as reopen:
                            reopen.write("\n# reap: push starting\n")
                        _git(wt_path, "push", "-u", "origin", "HEAD", timeout=60.0)
                        try:
                            with open(assignment.log_path, "a", encoding="utf-8") as reopen:
                                reopen.write("# reap: push completed\n")
                        except OSError:
                            pass
                    except (_GitError, subprocess.TimeoutExpired) as e:
                        # #1797: an auth-shaped failure here used to be
                        # logged and then forgotten — nothing downstream
                        # ever saw it, so a broken credential (e.g. an empty
                        # credential-helper password) silently landed on
                        # DONE whenever the worker had made real local
                        # commits, or on the generic "0 commits" ADVISORY
                        # whenever it hadn't — either way indistinguishable
                        # from "nothing to push". Only auth-shaped failures
                        # are promoted (see `_is_auth_push_failure`): this
                        # push is attempted unconditionally, including
                        # against repos with no `origin` at all (every test
                        # fixture, and any local-only deployment), and that
                        # failure mode must stay non-fatal exactly as before.
                        #
                        # #2356: an auth-shaped failure on THIS push doesn't
                        # necessarily mean the content never reached origin
                        # — a worker that hit the same broken-credential
                        # wall may have already landed the same commit via
                        # an alternate remote/protocol (#2269: an explicit
                        # SSH URL, worked around a missing HTTPS
                        # credential). Before promoting to FAILED, check
                        # whether origin already has local HEAD as an
                        # ancestor (`_remote_already_has_head` fetches — a
                        # read, which commonly still works even when the
                        # write credential just failed). Only genuinely
                        # missing content — origin behind local HEAD, or the
                        # check itself failing — is promoted, so #1797's
                        # original "nothing ever reached origin" case is
                        # unaffected.
                        _reason = str(e)
                        _suppressed_as_already_pushed = False
                        if _is_auth_push_failure(_reason):
                            _branch_for_check = (
                                head if head and head != "HEAD" else None
                            )
                            if _branch_for_check and _remote_already_has_head(
                                wt_path, "origin", _branch_for_check
                            ):
                                _suppressed_as_already_pushed = True
                            else:
                                _push_failure_reason = _reason
                        try:
                            with open(assignment.log_path, "a", encoding="utf-8") as reopen:
                                if _suppressed_as_already_pushed:
                                    # Deliberately NOT the generic "push
                                    # failed" line below: that line reads as
                                    # a live failure signal, which this
                                    # isn't — origin already has HEAD, so
                                    # the failed push here is a non-event
                                    # (#2356).
                                    reopen.write(
                                        "# reap: push failed but origin "
                                        "already has HEAD as an ancestor "
                                        "(pushed via another remote/"
                                        "protocol?) — treating as success "
                                        f"({_reason}) (#2356)\n"
                                    )
                                else:
                                    reopen.write(
                                        f"# reap: push failed ({_reason})\n"
                                    )
                        except OSError:
                            pass

        # #448: compute commits-ahead OUTSIDE the lock (git I/O) so the
        # advisory check doesn't stall other threads.  Only runs when
        # exit_code==0 and a worktree exists to inspect.  None → unknown
        # (git failed) → treat as non-zero to avoid false advisories.
        # _ZERO_COMMIT_TYPES (module constant) gates this on spec.type so that
        # review/smoke workers — which commit nothing by design — are
        # never falsely flagged as advisory.  #1534 widened that constant from
        # ("work",) to the full work-like set so `test-author`/`mock-author`
        # get the same downgrade; before that they could land on `done` with
        # an empty branch.
        # #2188: an issue labelled `deliverable:analysis` inverts the #448
        # reading — for it, a clean exit with 0 commits is the SUCCESS
        # condition (the deliverable is the worker's own final message, not
        # a diff), not the "worker did nothing" anomaly. Detected off
        # `assignment.spec.issue_labels` (wire field, #2188) so this works on
        # a config-free agent too — no DB or GitHub read required. Resolved
        # in the SAME `_ahead == 0` branch as the advisory check below so
        # the two can never disagree about what "0 commits" means for this
        # assignment.
        _analysis_deliverable = False
        _zero_commit_reason: str | None = None
        # #2234: set alongside (never together with) `_zero_commit_reason` —
        # both are decided in the SAME `_ahead == 0` branch below so the two
        # readings of "0 commits" can never disagree about which one applies
        # to this assignment.
        _policy_refusal_reason: str | None = None
        # #2316: checked FIRST in the `_ahead == 0` branch below, ahead of
        # `_analysis_deliverable`/`_policy_refusal_reason`/`_zero_commit_
        # reason` — a truncated run's `result_text` is whatever the model
        # managed to emit before the ceiling cut it off, not a considered
        # final message, so it must not be read as a positive "deliverable"
        # or "policy refusal" signal just because it happens to match one.
        _truncation_reason: str | None = None
        if (exit_code == 0 and assignment is not None
                and assignment.worktree_path
                and assignment.spec.type in _ZERO_COMMIT_TYPES):
            _wt_advisory = Path(assignment.worktree_path)
            if _wt_advisory.exists():
                _base = assignment.spec.branch or "main"
                _ahead = self._commits_ahead(_wt_advisory, _base)
                if _ahead == 0:
                    if (_worker_summary is not None
                            and _worker_summary.stop_reason in _TRUNCATION_STOP_REASONS):
                        # #2316: the model was guillotined by its own
                        # output-token ceiling before it could act — exit
                        # code 0 here is what a truncated run looks like, NOT
                        # evidence of a clean "nothing to do" finish. Must be
                        # re-driven, not parked in the advisory bucket
                        # nobody re-drives (space-invaders#1).
                        _truncation_reason = _format_truncation_reason(
                            _worker_summary.stop_reason, assignment.spec.provider
                        )
                        try:
                            with open(assignment.log_path, "a", encoding="utf-8") as reopen:
                                reopen.write(
                                    "# reap: truncated — 0 commits ahead of "
                                    f"{_base}; stop_reason="
                                    f"{_worker_summary.stop_reason!r}, status "
                                    "set to failed (#2316)\n"
                                )
                        except OSError:
                            pass
                    elif DELIVERABLE_ANALYSIS_LABEL in (assignment.spec.issue_labels or []):
                        _analysis_deliverable = True
                        try:
                            with open(assignment.log_path, "a", encoding="utf-8") as reopen:
                                reopen.write(
                                    "# reap: analysis deliverable — 0 commits "
                                    f"ahead of {_base}; issue labelled "
                                    f"{DELIVERABLE_ANALYSIS_LABEL!r}, status "
                                    "set to done (#2188)\n"
                                )
                        except OSError:
                            pass
                    elif _worker_summary is not None and _looks_like_policy_refusal(
                        _worker_summary.result_text
                    ):
                        # #2234: the worker's own final message cites a
                        # standing repo-rule prohibition as the reason it
                        # stopped — the #2195 shape. Distinct from the
                        # ADVISORY fallback below: retrying this assignment
                        # cannot change a rule that isn't going anywhere, so
                        # it must not consume the queue's attempt budget or
                        # land in the same bucket as a genuinely stuck
                        # worker.
                        _policy_refusal_reason = _worker_summary.result_text or (
                            "worker cited a standing repo-rule prohibition"
                        )
                        try:
                            with open(assignment.log_path, "a", encoding="utf-8") as reopen:
                                reopen.write(
                                    "# reap: refused_policy — 0 commits ahead "
                                    f"of {_base}; worker's final message cites "
                                    "a standing repo-rule prohibition, status "
                                    "set to refused_policy (#2234)\n"
                                )
                        except OSError:
                            pass
                    else:
                        _zero_commit_reason = (
                            "worker exited cleanly but pushed 0 commits"
                        )
                        try:
                            with open(assignment.log_path, "a", encoding="utf-8") as reopen:
                                reopen.write(
                                    "# reap: advisory — 0 commits ahead of "
                                    f"{_base}; status set to advisory\n"
                                )
                        except OSError:
                            pass

        # This block MUST always run regardless of push outcome so that
        # the assignment transitions out of 'running'.
        try:
            with open(assignment.log_path, "a", encoding="utf-8") as reopen:
                reopen.write("# reap: updating status\n")
        except (OSError, AttributeError):
            pass

        with self._lock:
            assignment = self._assignments.get(assignment_id)
            if assignment is None:
                return
            assignment.exit_code = exit_code
            assignment.finished_at = time.time()
            if captured_branch is not None:
                assignment.branch = captured_branch
            # Cancel sets status before this runs; respect it.
            if assignment.status == RUNNING:
                if exit_code == 0:
                    if _usage_limit_reason is not None:
                        # #1534: a worker killed by the account's Claude
                        # session/weekly usage limit is NEVER `done`, whatever
                        # its exit code says.  The transcript ends mid-task
                        # ("You've hit your session limit · resets <time>") and
                        # the wrapper can still exit 0 — that combination used
                        # to record a clean, unmarked completion, which every
                        # downstream gate then read as "the work is finished".
                        # FAILED (not ADVISORY) because per #1461 a usage-limit
                        # kill is the one terminal state known safe to
                        # re-dispatch unchanged once the window resets, whereas
                        # ADVISORY means "a human needs to look at this".
                        assignment.status = FAILED
                    elif _result_is_error:
                        # #1584: `is_error: true` on the transcript's LAST
                        # `result` event is authoritative, even on a clean
                        # exit_code — a transient upstream failure (529/500/a
                        # network drop) that killed the worker at turn 1
                        # used to fall through to the `else: DONE` below,
                        # silently recording a $0.03, zero-work session as a
                        # clean success. Checked BEFORE the zero-commit
                        # advisory downgrade: a real error is worse than "no
                        # commits", not merely equivalent to it.
                        assignment.status = FAILED
                    elif _push_failure_reason is not None:
                        # #1797: the belt-and-suspenders push raised an
                        # auth-shaped error (see `_is_auth_push_failure`).
                        # Checked BEFORE the zero-commit advisory downgrade
                        # and takes priority over it: a push failure is a
                        # distinct, worse outcome than "nothing to push"
                        # even when the worktree also happens to be 0
                        # commits ahead (a broken credential fails the push
                        # regardless of commit count — see the #1797
                        # evidence, where both conditions were true at once
                        # and the advisory message alone made the auth break
                        # invisible). FAILED, not ADVISORY: work may exist
                        # locally in the worktree that never reached origin
                        # — that needs a human/retry, not a "nothing to do"
                        # shrug.
                        assignment.status = FAILED
                        assignment.push_failure_reason = _push_failure_reason
                    elif _truncation_reason is not None:
                        # #2316: the worker's own output-token ceiling cut it
                        # off before it committed anything — checked BEFORE
                        # `_policy_refusal_reason`/`_zero_commit_reason` (the
                        # `_reap` computation above already guarantees the
                        # four are mutually exclusive; kept parallel here for
                        # readability). FAILED, not ADVISORY: exit_code==0 is
                        # not evidence of a clean finish for a truncated run,
                        # and nobody re-drives an advisory. `error` is set
                        # too (not just `truncation_reason`) so the existing
                        # generic `entry.get("error")` fallback in
                        # `coord.notify`'s FAILED comment arms carries this
                        # text without needing its own dedicated wiring.
                        assignment.status = FAILED
                        assignment.truncation_reason = _truncation_reason
                        assignment.error = _truncation_reason
                    elif _policy_refusal_reason is not None:
                        # #2234: clean exit, no commits, and the worker's own
                        # final message cites a standing repo-rule
                        # prohibition — a distinct, permanent shape of "0
                        # commits", not the generic #448 ADVISORY. Checked
                        # BEFORE `_zero_commit_reason` (the two are mutually
                        # exclusive by construction — see the `_reap`
                        # computation above — so the order is never actually
                        # load-bearing, kept parallel to it for readability).
                        assignment.status = REFUSED_POLICY
                        assignment.policy_refusal_reason = _policy_refusal_reason
                    elif _zero_commit_reason is not None:
                        # #448: clean exit but no commits → advisory, not done.
                        assignment.status = ADVISORY
                        assignment.zero_commit_reason = _zero_commit_reason
                    else:
                        assignment.status = DONE
                        if _analysis_deliverable:
                            # #2188: 0 commits on a `deliverable:analysis`
                            # issue is the success shape — capture the
                            # worker's own final message (already parsed
                            # into `_worker_summary` above) so the
                            # coordinator can post the actual deliverable to
                            # the issue instead of a bare "complete" comment.
                            # Best-effort: a non-stream-json log (or one with
                            # no terminal `result` event) just leaves
                            # `result_text` `None` — the assignment is still
                            # correctly `done`, only the auto-posted prose is
                            # missing, same degradation as every other
                            # best-effort log parse in this function.
                            assignment.analysis_deliverable = True
                            if _worker_summary is not None and _worker_summary.result_text:
                                assignment.result_text = _worker_summary.result_text
                else:
                    assignment.status = FAILED
                # #1461: only ever attaches to a FAILED/ADVISORY transition —
                # the branch above now guarantees that whenever
                # `_usage_limit_reason` is set, so this is belt-and-braces.
                if (_usage_limit_reason is not None
                        and assignment.status in (FAILED, ADVISORY)):
                    assignment.usage_limit_reason = _usage_limit_reason
                # #1584: only ever attaches to a FAILED transition. A non-zero
                # exit_code with `_result_is_error` also lands here (the
                # `else: assignment.status = FAILED` above), so this is
                # checked independently of which branch set FAILED.
                if _result_is_error and assignment.status == FAILED:
                    assignment.api_error_reason = _api_error_reason
                # #2131: only ever attaches to a FAILED transition —
                # `SPEND_CEILING_EXIT` is non-zero, so the `else` above has
                # already set FAILED whenever `_spend_ceiling_reason` is set.
                # Guarded anyway so a `POST /cancel` that raced the kill (and
                # set CANCELLED before this block ran) is never relabelled.
                if _spend_ceiling_reason is not None and assignment.status == FAILED:
                    assignment.spend_ceiling_reason = _spend_ceiling_reason
                # #2638: same belt-and-braces guard — `RUNTIME_CEILING_EXIT`/
                # `HOST_SLEEP_EXIT` are both non-zero, so the `else` above
                # has already set FAILED whenever either reason is set;
                # re-checked here only so a race with `POST /cancel` is
                # never relabelled.
                if _runtime_ceiling_reason is not None and assignment.status == FAILED:
                    assignment.runtime_ceiling_reason = _runtime_ceiling_reason
                if _host_sleep_reason is not None and assignment.status == FAILED:
                    assignment.host_sleep_reason = _host_sleep_reason
            self._processes.pop(assignment_id, None)

        # #315/#324: parse the log for the worker's claude session_id (from the
        # `system.init` event emitted by `claude -p --output-format stream-json`).
        # Done OUTSIDE the lock so the log parse (I/O + JSON) doesn't stall
        # other threads; the field write is the only mutation, and assignment
        # objects are only dropped under the lock so the reference is safe.
        # #324: route through provider.parse_log() when a provider is registered
        # so future providers can customise log parsing.  ClaudeProvider delegates
        # to coord.worker_events.parse_log — byte-identical to the old path.
        # #1584: reuse `_worker_summary` (parsed once, above) instead of
        # parsing the same full transcript a second time — it was built from
        # `log_path`, which is this same assignment's log for the run just
        # reaped, via the identical provider-resolution rule. Only re-parses
        # (the pre-#1584 behaviour) when that parse is unavailable — e.g. a
        # non-stream-json log, or a parse failure best-effort-swallowed above
        # — so this never becomes LESS reliable than before, only less
        # redundant on the common path.
        if assignment is not None and assignment.claude_session_id is None:
            try:
                summary = _worker_summary
                if summary is None:
                    from coord.worker_events import is_stream_json  # noqa: PLC0415
                    lp = assignment.log_path
                    if lp and is_stream_json(lp):
                        # #1796: reuse `_reap_provider` — resolved once,
                        # above, from this SAME assignment's spec (local
                        # registry → wire-carried provider_def → best-effort
                        # None) — instead of re-deriving it from
                        # `self._providers` alone, which would miss a
                        # config-free agent's wire-resolved provider.
                        if _reap_provider is not None:
                            summary = _reap_provider.parse_log(lp, tail_bytes=0)
                        else:
                            from coord.worker_events import parse_log  # noqa: PLC0415
                            summary = parse_log(lp, tail_bytes=0)
                if summary is not None and summary.session_id:
                    assignment.claude_session_id = summary.session_id
            except Exception:  # noqa: BLE001
                pass  # best-effort; a missing session_id just means chat-continue will refuse

        self._persist()
        try:
            with open(assignment.log_path, "a", encoding="utf-8") as reopen:
                final_status = assignment.status if assignment else "unknown"
                # #2212: log a plain `graphify_invocations=N` counter alongside
                # the existing reap line — this is the measurable half of the
                # graph-first navigation rule (see WORKER_SYSTEM_PROMPT and
                # `_count_graphify_invocations`). `_worker_summary` is `None`
                # for a non-stream-json log or a parse failure; counting 0 in
                # that case is the honest answer, not a lie — best-effort like
                # everything else in this function.
                _graphify_invocations = _count_graphify_invocations(
                    _worker_summary.bash_commands if _worker_summary is not None else []
                )
                # #2236: the count alone can't separate "didn't try" from
                # "couldn't try" (no graph in this worktree — the prompt's own
                # escape hatch fires silently) or from "tried, got nothing".
                # `graph_present` answers the first; the per-query lines below
                # answer the second. Read while the worktree still exists —
                # `_cleanup_worktree` runs further down this same function.
                _graph_present = _worktree_graph_present(
                    getattr(assignment, "worktree_path", None) if assignment else None,
                    getattr(assignment.spec, "repo_path", None) if assignment else None,
                )
                reopen.write(
                    f"# reap: done (exit_code={exit_code} status={final_status} "
                    f"graphify_invocations={_graphify_invocations} "
                    f"graph_present={int(_graph_present)})\n"
                )
                reopen.writelines(
                    _format_graphify_query_lines(
                        _worker_summary.graphify_queries
                        if _worker_summary is not None
                        else []
                    )
                )
        except (OSError, AttributeError):
            pass

        # #305: stash artifacts BEFORE removing the worktree so the compiled
        # outputs survive cleanup.  Only runs for DONE assignments (workers
        # that exited cleanly) with configured artifact_paths for this repo.
        # #1323/#1357: capture unmatched globs so we can record a per-glob
        # diagnostic (see below — it no longer changes status, see #1357).
        _stash_unmatched: list[str] = []
        if assignment is not None:
            _stash_unmatched = self._stash_artifacts(assignment)

        # #1357 (revert of the #1323 downgrade): a configured artifact_paths
        # glob matching 0 files is NOT evidence the work is bad. Most work in
        # this repo is unrelated to any single glob — e.g. claude-coordinator's
        # only glob is the Rust `tui/target/debug/coord-tui` binary, which
        # every Python-only change is guaranteed to miss — so #1323's blanket
        # DONE -> ADVISORY downgrade false-failed the overwhelming majority of
        # headless work assignments in this repo (real commits, exit 0,
        # pushed branch, all reported Red).
        #
        # Record the unmatched glob(s) as a diagnostic ONLY: it never mutates
        # `status` or `zero_commit_reason` (a stash miss is not a commit
        # count). `coord pull-artifact`'s 404 path already has its own
        # ground-truth reason (`artifact_absence_reason`); this field just
        # lets a human — or a future Test-stage message — say "no stashed
        # artifact for this glob, rebuilding from source" without digging
        # through the raw worker log. Still gated on _ADVISORY_TYPES so
        # review/smoke/test/merge/conflict-fix assignments, which routinely
        # finish DONE without reproducing every glob, don't get a spurious
        # diagnostic either.
        if (
            _stash_unmatched
            and assignment is not None
            and assignment.spec.type in _ADVISORY_TYPES
        ):
            _missed_str = ", ".join(repr(g) for g in _stash_unmatched)
            _stash_diag_reason = (
                f"stash: 0 files matched "
                + (repr(_stash_unmatched[0]) if len(_stash_unmatched) == 1
                   else f"{len(_stash_unmatched)} glob(s): {_missed_str}")
            )
            with self._lock:
                _sa = self._assignments.get(assignment_id)
                if _sa is not None:
                    _sa.stash_unmatched_globs = list(_stash_unmatched)
            self._persist()
            try:
                with open(assignment.log_path, "a", encoding="utf-8") as _lf:
                    _lf.write(
                        f"# reap: stash diagnostic — {_stash_diag_reason}; "
                        "status left unchanged (see #1357)\n"
                    )
            except (OSError, AttributeError):
                pass

        # Clean up worktree AFTER updating status
        if assignment is not None:
            self._cleanup_worktree(assignment)

        # #2552: `_cleanup_worktree` above may have just rescued a dirty
        # worktree into a WIP commit and pushed it (`_rescue_uncommitted_
        # work`) — AFTER the "0 commits ahead" status decision earlier in
        # this method already ran. That decision (ADVISORY's `zero_commit_
        # reason`, or REFUSED_POLICY's `policy_refusal_reason`) was computed
        # from a branch that was genuinely empty at the moment of judgement;
        # a rescue that landed a real, pushed commit on it makes the verdict
        # stale by construction — reporting "nothing was authored" about a
        # branch that, four log lines later, carries the worker's actual
        # output. Recompute from `repo_path` (not the worktree, which may
        # already be gone) whether the rescue's push actually reached
        # origin, and correct the status when it did. Only ADVISORY/
        # REFUSED_POLICY are ever reachable here (both are set exclusively
        # in the `_ahead == 0` branch above, itself gated on `spec.type in
        # _ZERO_COMMIT_TYPES`) — DONE, FAILED-for-other-reasons, CANCELLED
        # etc. are untouched.
        if (
            assignment is not None
            and assignment.status in (ADVISORY, REFUSED_POLICY)
            and assignment.dirty_worktree_reason is not None
        ):
            _rescue_branch = assignment.branch or assignment.spec.target_branch
            if _rescue_branch:
                _rescue_repo_path = Path(assignment.spec.repo_path).expanduser()
                _rescue_base = assignment.spec.branch or "main"
                _post_ahead = _pushed_commits_ahead(
                    _rescue_repo_path, _rescue_base, _rescue_branch
                )
                if _post_ahead is not None and _post_ahead > 0:
                    _prior_status = assignment.status
                    with self._lock:
                        _live = self._assignments.get(assignment_id)
                        if _live is not None and _live.status == _prior_status:
                            _live.status = DONE
                        if assignment is not _live:
                            assignment.status = DONE
                    self._persist()
                    self._log_line(
                        assignment,
                        "# reap: post-cleanup recheck — "
                        f"{_post_ahead} commit(s) pushed to origin/"
                        f"{_rescue_branch} ahead of {_rescue_base} after "
                        f"rescue (was {_prior_status!r}); status corrected "
                        "to done (#2552)",
                    )

    def _prune_completed_history(self) -> None:
        """Drop oldest terminal assignments over _COMPLETED_HISTORY_CAP (#452).

        Caller must hold self._lock (or call from __init__ before threads
        start).  Active (pending/running) assignments are never pruned.

        #1424: also drops the pruned ids' `_cleanup_locks` and
        `_reap_complete` entries, so neither per-assignment dict grows
        unbounded over a long-running agent's lifetime. Skips any lock
        still held — pruning can race with `_cleanup_worktree` if a
        `_reap`/`cancel()` teardown is mid-flight for an old id — leaving it
        for the next prune pass rather than risk dropping a lock a thread is
        currently using for mutual exclusion. The reap-completion event has
        no "still in use" check (an `Event` can't report waiters) but by the
        time an id is old enough to be pruned its reap thread has long since
        finished and set it, so dropping it is safe.
        """
        terminal = [
            a for a in self._assignments.values()
            if a.status not in (PENDING, RUNNING)
        ]
        if len(terminal) > _COMPLETED_HISTORY_CAP:
            terminal.sort(
                key=lambda a: (
                    a.finished_at if a.finished_at is not None
                    else a.started_at if a.started_at is not None
                    else 0.0
                ),
                reverse=True,
            )
            for old in terminal[_COMPLETED_HISTORY_CAP:]:
                self._assignments.pop(old.id, None)
                lock = self._cleanup_locks.get(old.id)
                if lock is not None and not lock.locked():
                    self._cleanup_locks.pop(old.id, None)
                self._reap_complete.pop(old.id, None)

    def _prune_terminal_advisory(self) -> None:
        """Drop ADVISORY assignments whose work is already terminal on GitHub (#1492).

        #1472 added a render-time filter in `coord status`
        (`_live_advisory_entries`) that *hides* an advisory entry once
        GitHub shows the issue closed or the PR merged — but the agent
        itself never stopped *serving* that entry, so every other consumer
        of the completed-assignment map (the dashboard API, the TUI, any
        future client) has to reimplement the same filter or keep showing a
        stale "UNVERIFIED — review it" nag forever. `_prune_completed_history`
        above only drops terminal entries by *count*
        (`_COMPLETED_HISTORY_CAP`), never by GitHub outcome, so a settled
        advisory can sit well within the cap and keep being served
        indefinitely.

        This is the shared-seam fix: once the agent itself confirms an
        advisory entry's work is terminal, it drops the entry from its own
        state, so nothing downstream — present or future — ever has to
        check again. `coord status`'s render-time filter stays in place as a
        fail-open backstop for entries served by an agent that predates this
        method.

        Rate-limited to once per `_ADVISORY_TERMINAL_CHECK_COOLDOWN_S` via
        `self._last_advisory_terminal_check` — this runs from the `/status`
        hot path (`list_assignments`), so an unthrottled check would put a
        `gh` round-trip per distinct (repo, issue, branch) on every poll,
        exactly the "fail-open cost" #1472 already accepted once in
        `coord status`'s render-time filter.

        Best-effort and fail-open throughout, mirroring
        `github_ops.work_is_terminal`'s own convention: a missing/unreadable
        checkout, an unresolvable (non-GitHub, or gone) `origin` remote, or a
        `gh` failure all just leave the entry in place for the next pass —
        never treated as evidence the work is or isn't done.
        """
        now = time.time()
        with self._lock:
            if (
                now - self._last_advisory_terminal_check
                < _ADVISORY_TERMINAL_CHECK_COOLDOWN_S
            ):
                return
            self._last_advisory_terminal_check = now
            # #2234: REFUSED_POLICY joins ADVISORY here for the same reason
            # — both are terminal, zero-commit statuses the agent would
            # otherwise keep serving indefinitely from its own `/status`
            # feed once GitHub confirms the work is done, forcing every
            # downstream consumer (dashboard, TUI, any future client) to
            # reimplement the same GitHub-terminality filter this method
            # exists to spare them.
            candidates = [
                a for a in self._assignments.values()
                if a.status in (ADVISORY, REFUSED_POLICY)
            ]
        if not candidates:
            return

        from coord import github_ops  # noqa: PLC0415
        from coord.models import trust_issue_closed_for  # noqa: PLC0415

        terminal_cache: dict = {}
        slug_cache: dict[str, str | None] = {}
        terminal_ids: list[str] = []
        for a in candidates:
            repo_path = a.spec.repo_path
            if repo_path not in slug_cache:
                slug_cache[repo_path] = _infer_repo_github_slug(repo_path)
            repo_github = slug_cache[repo_path]
            if not repo_github:
                continue
            try:
                # #2639: trust_issue_closed_for(a.spec.type) — an
                # ADVISORY/REFUSED_POLICY row for a test-author/mock-author
                # spec carries the milestone tracking issue in
                # issue_number, not its own deliverable, so a closed
                # tracking epic must not read as "this row is terminal"
                # here either.
                terminal = github_ops.work_is_terminal(
                    repo_github, a.spec.issue_number, a.branch,
                    cache=terminal_cache,
                    trust_issue_closed=trust_issue_closed_for(a.spec.type),
                )
            except Exception:  # noqa: BLE001 — fail-open, never crash /status
                terminal = False
            if terminal:
                terminal_ids.append(a.id)

        if not terminal_ids:
            return

        with self._lock:
            for aid in terminal_ids:
                self._assignments.pop(aid, None)
                lock = self._cleanup_locks.get(aid)
                if lock is not None and not lock.locked():
                    self._cleanup_locks.pop(aid, None)
                self._reap_complete.pop(aid, None)
        self._persist()

    def _prune_superseded_advisory(self) -> None:
        """Drop ADVISORY assignments superseded by a later DONE retry (#1468).

        `_prune_terminal_advisory` above clears an advisory once the work
        goes terminal **on GitHub** (issue closed / PR merged) — but that's
        a post-merge signal. #1468's rescued-WIP-commit chain exposes a gap
        strictly earlier than that: assignment A dies mid-flight, its
        uncommitted work gets rescued into a WIP commit and the assignment
        lands on ADVISORY ("UNVERIFIED — review before merging"); assignment
        B is then dispatched for the *same issue*, reimplements cleanly, and
        reaches DONE. Nothing on GitHub has closed or merged yet — the PR is
        sitting there waiting for review/merge — so `_prune_terminal_advisory`
        leaves A's advisory in place, still reading as a live warning against
        exactly the branch B just finished.

        Matches on ``(repo_name, issue_number)`` rather than branch, since a
        retry may deliberately use a *different* branch (e.g.
        ``fresh_branch``) than the assignment it supersedes — the rescue
        banner is scoped to the issue, not to one branch name.

        "Later" is determined by position in ``self._assignments``, which
        (being a plain dict) preserves insertion order and is only ever
        appended to at dispatch time (see ``assign()``) — never reordered or
        re-inserted. So a later dict position is unambiguously a later
        dispatch on this agent, with no wall-clock or cross-machine
        assumption required. Unlike `_prune_terminal_advisory` this makes no
        GitHub round-trip (pure in-memory comparison), so it needs no rate
        limiting and can run on every `/status` poll.
        """
        with self._lock:
            ordered = list(self._assignments.values())
            superseded_ids: list[str] = []
            for i, a in enumerate(ordered):
                # #2234: REFUSED_POLICY joins ADVISORY here too — a refusal
                # for issue X can be superseded the same way an advisory can
                # (e.g. the coordinator re-scopes the issue so a later
                # dispatch's deliverable is no longer coordinator-only, and
                # that later attempt reaches DONE). Same in-memory
                # comparison, same root cause as `_prune_terminal_advisory`
                # above.
                if a.status not in (ADVISORY, REFUSED_POLICY):
                    continue
                key = (a.spec.repo_name, a.spec.issue_number)
                for later in ordered[i + 1 :]:
                    if (
                        later.status == DONE
                        and (later.spec.repo_name, later.spec.issue_number) == key
                    ):
                        superseded_ids.append(a.id)
                        break

            if not superseded_ids:
                return

            for aid in superseded_ids:
                self._assignments.pop(aid, None)
                lock = self._cleanup_locks.get(aid)
                if lock is not None and not lock.locked():
                    self._cleanup_locks.pop(aid, None)
                self._reap_complete.pop(aid, None)
        self._persist()

    def _persist(self) -> None:
        with self._lock:
            # Cap terminal assignments to keep both in-memory state and the
            # persisted file bounded (#452).  Active (pending/running)
            # assignments are never touched so in-flight work is safe.
            self._prune_completed_history()
            data = {
                "machine": self.machine_name,
                "capabilities": self.capabilities,
                "repos": self.repos,
                # #715: to_status_dict() strips spec.briefing/system_prompt
                # from terminal entries so agent_state.json can't refill back
                # up to the multi-hundred-KB sizes that caused this issue —
                # even if a future change relaxes _COMPLETED_HISTORY_CAP.
                "assignments": [a.to_status_dict() for a in self._assignments.values()],
            }
        # #1421: _persist() is called from many worker/monitor threads with
        # only the snapshot above under self._lock — the file write itself
        # happens unlocked so concurrent persists can overlap.  A *shared*
        # fixed tmp filename let one thread's write_text() truncate the file
        # mid-write by another thread, and os.replace() would then promote
        # that truncated/empty file into place, silently wiping
        # agent_state.json.  Staging through a unique tempfile.mkstemp() name
        # in the same directory means concurrent persists can never collide:
        # each thread renames its own file into place atomically.
        tmp_path: Path | None = None
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=str(self.state_dir), prefix="agent_state.", suffix=".tmp"
            )
            tmp_path = Path(tmp_name)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(data, indent=2))
            os.replace(tmp_path, self.state_path)
        except (FileNotFoundError, OSError) as e:
            _log.error("failed to persist agent state to %s: %s", self.state_path, e)
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            # #1421: a corrupt/truncated state file used to be discarded
            # silently here, so an agent restart after the _persist() race
            # (or any other on-disk corruption) would come back believing it
            # had zero assignments — no log, no error, just invisible state
            # loss. Log loudly and move the bad file aside so it's
            # recoverable/diagnosable instead of being clobbered by the next
            # _persist().
            _log.error(
                "corrupt agent state file %s (%s: %s) — starting with no "
                "recovered assignments; original moved aside",
                self.state_path, type(e).__name__, e,
            )
            try:
                corrupt_path = self.state_path.with_name(
                    f"{self.state_path.name}.corrupt-{int(time.time())}"
                )
                os.replace(self.state_path, corrupt_path)
            except OSError:
                pass
            return
        for entry in data.get("assignments", []):
            spec_data = entry.pop("spec", None)
            if spec_data is None:
                continue
            spec = AssignmentSpec(**spec_data)
            a = AgentAssignment(spec=spec, **entry)
            # Any process running pre-restart is gone.
            if a.status in (PENDING, RUNNING):
                a.status = FAILED
                a.error = "agent restarted; subprocess lost"
                if a.finished_at is None:
                    a.finished_at = time.time()
            self._assignments[a.id] = a

        # Cap terminal history immediately on load so the first /status poll
        # after a restart with a bloated state file is already bounded (#452).
        # No lock needed here — __init__ hasn't started any threads yet.
        self._prune_completed_history()

        # Prune stale worktrees on startup
        self._prune_worktrees()

    def _prune_worktrees(self) -> None:
        """Ask git to prune stale worktree bookkeeping for each known repo.

        Tolerates missing or inaccessible repo directories — ``subprocess.run``
        raises ``FileNotFoundError`` (not ``_GitError``) when its *cwd* doesn't
        exist, so we catch ``(FileNotFoundError, OSError)`` as well.  This
        prevents a stale worktree entry from crashing the agent on startup
        (e.g. after ``exec_restart`` when one of the repo paths has gone away).
        """
        seen_paths: set[str] = set()
        for path_str in self.repo_paths.values():
            if path_str in seen_paths:
                continue
            seen_paths.add(path_str)
            try:
                _git(Path(path_str).expanduser(), "worktree", "prune")
            except (_GitError, FileNotFoundError, OSError):
                pass

    def shutdown(self, *, kill_running: bool = False) -> None:
        """Best-effort cleanup. Used by tests and graceful shutdown."""
        with self._lock:
            procs = list(self._processes.items())
        for aid, proc in procs:
            if proc.poll() is None:
                if kill_running:
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except (ProcessLookupError, PermissionError):
                        pass
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
        with self._lock:
            for aid, thread in list(self._threads.items()):
                thread.join(timeout=1)
            self._threads.clear()
