"""``coord drive`` — drive ONE issue from dispatch to merge, unattended (#1392).

The Python port of ``scripts/drive-issue.sh`` (742 lines of bash, deleted in
the same change).  The port buys **testability and shippability**, not fewer
processes: every ``coord`` invocation is still a subprocess, deliberately.

  THE CLI IS THE CONTRACT; INTERNAL FUNCTIONS ARE NOT.  The obvious "win" of
  a Python port is to stop shelling out — call ``record_test_verdict()``
  instead of ``coord test --passed``.  Do NOT do this.  It is exactly #1384:
  the ``coord test`` CLI mirrors ``test_state`` → the legacy ``smoke_test``
  field and ``record_test_verdict()`` alone does not, so calling the function
  directly silently reintroduces the bug that makes ``coord fix`` refuse to
  dispatch.  Every board mutation this driver performs goes through the CLI.

WHAT IT IS.  The pipeline is Work → Test → Review → Merge
(``pipeline.default_gates``).  coord automates all of it (#1426): the ``coord
serve`` tick loop reconciles and enqueues, and the ``coord-notify.timer``
(5 min, on the daemon host) posts completions, auto-dispatches the Test-stage
smoke assignment (``dispatch_pending_smoke``), dispatches reviews, and runs the
review → fix → re-review auto-loop.  One thing is still missing, and this
supplies it:

  NOTHING SEQUENCES THE STAGES FOR A SINGLE ISSUE.  ``coord wait`` is
  per-assignment (and reads the LOCAL dispatched ledger, so it does not work
  from a thin client at all).  → This is a resumable state machine over the
  daemon's board: it dispatches the WORK assignment, then OBSERVES
  Test/Review/Merge — coord dispatches all three itself — nudging ``coord
  notify`` (``--notify``) when nothing has changed for ``--stall`` minutes
  (per-assignment-type overrides — #2649,
  ``pipeline.stall_thresholds``/``PipelineConfig.stall_threshold_secs`` —
  win over the flat ``--stall`` value for a type they cover).

A FAILING TEST IS A LOOP ITERATION, NOT A DEAD END.  On a genuine test failure
this runs ``coord fix``, which dispatches a headless follow-up worker on the
SAME branch with the model escalated (sonnet → opus, every round) and the
failure quoted in its briefing.  The loop re-tests and repeats, bounded by
``--max-fix-rounds``.  A fix round that legitimately changes nothing exits
``done``, not ``advisory`` — the zero-commit heuristic is per-branch and the
branch already carries the original work's commit — so a no-op fix does not
wedge the pipeline (observed on #1445).

Everywhere coord ALREADY has a path, this observes rather than acts — in
particular it never dispatches the Test-stage smoke assignment (coord's own
``dispatch_pending_smoke`` does) or a REVIEW fix (the notify timer's auto-loop
does) — two drivers racing to dispatch the same thing is exactly the
2026-06-07 duplicate-fix-worker incident (#476/#477).

Re-running it on the same issue is safe and resumes from wherever the board
actually is.

THE ORACLE LOOP (#1453, docs/ORACLE_LOOP.md).  When this issue's milestone
already has a merged Gate-A contract and the repo has an acceptance driver
configured, dispatching ``coord assign`` straight away would just hit the
#1138 hard gate (``coord.dispatch.enforce_oracle_readiness``) and refuse —
the issue's JIT acceptance slice hasn't been authored yet.  Rather than dead-
end there, :func:`resolve_oracle_decision` (resolved ONCE, at preflight —
mirrors ``tui/src/app/pipeline.rs``'s ``gate_a_contract_exists_for`` and
``coord.milestone_dispatch.gate_a_status``, all three keyed on
:func:`coord.acceptance.gate_a_contract_path`) puts this run into "oracle
drive" mode: :func:`_dispatch_work_stage` authors the slice first (``coord
acceptance author <repo> <tracking_issue> --issue <N>``, plus ``--for-path``
when the repo's driver is routed — resolved from the milestone's Gate-A
mock kind via the SHARED :func:`coord.acceptance.resolve_for_path`, so this
never drifts from whatever eventually resolves it for the TUI's own menu,
#1460) and :func:`_decide_acceptance_author` drives it through to a landed
merge (``status='merged'``, #609) before ever calling ``coord assign``.
The slice's Test and Review stages are dispatched by coord's own passive tick
exactly like a normal work row's, so this only observes those; its MERGE is
not (``serve_app._auto_drain_tick`` is gated on ``merge.auto_drain``, which is
off by default and off in the standing fleet config), so
:func:`_decide_acceptance_landing` performs it — the same bounded ``coord
merge --only <aid>`` this driver already runs for the work row (#2079: before
that, every oracle issue idled through ``2 × --deadline`` waiting for a drain
loop that was switched off, then landed in a terminal ``blocked`` state).
An ``advisory`` JIT-slice exit is handled exactly like the main work row's
(``--accept-advisory``, #1357) rather than waited on forever.
``--no-acceptance`` opts out back to the pre-#1453 behaviour.

STRUCTURE.  All decision logic lives in :func:`decide` and :func:`preflight`,
which are pure functions over an :class:`~coord.drive_state.IssueState` plus
injected verifiers.  :class:`Driver` is the thin I/O shell: poll, execute the
returned :class:`Action`, sleep.  Every bug the bash version shipped was in the
decision half, which is why that half is where the tests are.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, NamedTuple, Protocol, Sequence

from coord.filelock import FileLock, LockBusy, notify_lock_path
from coord.drive_state import (
    BoardFetcher,
    DriveStateError,
    IssueState,
    project,
    scratch_dir,
)
from coord.interactive import (
    DRIVE_SESSION_PREFIX,
    TmuxHost,
    tmux_available,
    tmux_session_alive,
)
from coord.dead_end import DeadEnd, detect_dead_end
from coord.failure_class import (
    classify_failure,
    environmental_backoff_secs,
    plan_usage_limit_resume,
)
from coord.models import (
    DELIVERABLE_ANALYSIS_LABEL,
    MERGE_LANDED_MARKER,
    POLICY_REFUSAL_MARKER,
)
from coord.self_health import self_freshness
from coord.usage_limits import PlanLimits, evaluate_usage_gate, get_plan_limits
# Lost in the #1584-onto-#1590 rebase: _decide_review() calls this, but the
# import lived in a hunk #1590 rewrote, so the merge came out textually clean
# and semantically broken (NameError at coord/drive.py:1162). Same symbol and
# same source as coord/notify.py:53 — deliberately NOT re-pointed at #1590's
# newer classify_failure(), which would change reviewed behaviour during a
# conflict resolution.
#
# #1710 inventory: kept as a direct import — same trivial-predicate reasoning
# as coord/notify.py's identical import: `is_usage_limit_reason` is a
# string-prefix check over `Assignment.failure_reason`/`review_failure_reason`
# (a coordinator-authored value stamped by `format_usage_limit_reason`), not a
# per-provider log-format parse. Any provider's failure_reason is checked the
# same way, so there is no `provider.parse_log()` equivalent to route through.
from coord.worker_events import is_usage_limit_reason
# #1769: the stale-vs-missing smoke-verdict predicate has exactly ONE
# implementation, in the module that emits both of the wordings it matches.
# See `_STALE_SMOKE_MARKERS` / `_is_stale_smoke_reason` below.
from coord.merge_queue import (
    STALE_SMOKE_MARKERS as _mq_stale_smoke_markers,
    UNKNOWN_BRANCH_HEAD_REASON as _mq_unknown_branch_head_reason,
    is_ci_flaky_reason,
    is_ci_infra_reason,
    is_ci_pending_reason,
    is_ci_unreadable_reason,
    is_stale_smoke_reason as _mq_is_stale_smoke_reason,
)

# ── exit codes (unchanged from drive-issue.sh) ───────────────────────────────

EXIT_OK = 0
EXIT_TERMINAL_FAILURE = 1
EXIT_USAGE = 2
EXIT_DEADLINE = 3
# #1505: distinct from EXIT_TERMINAL_FAILURE so `coord drive`'s exit code
# alone tells a wrapper/notify path "a human decision is waiting on the
# board" apart from "something actually broke" — see `_escalate_merge`.
EXIT_ESCALATED = 4
# #1844: distinct from EXIT_TERMINAL_FAILURE for the ONE failure shape that
# is deterministic rather than transient — `coord.dispatch.DispatchRefused`
# (raised by `enforce_oracle_readiness`/`enforce_epic_dispatch_guard`, a
# `ValueError` subclass) reaching `coord assign`/`coord approve-plan`/`coord
# fix`'s own dispatch call, refusing the exact dispatch this run just
# attempted.
# Nothing in a retry changes the condition that caused the refusal — no
# acceptance slice appears, no label gets added — so retrying costs a full
# tick cycle and changes nothing. `coord drive`'s own subprocess call
# (`Driver._spawn`) is what SEES this code on the `coord assign`/
# `approve-plan` child process; `_loop`'s RUN-action handling then re-raises
# with this SAME code (not EXIT_TERMINAL_FAILURE) so the distinction survives
# into `_drive_exit_summary`'s `drive_exited` audit row, which is the one
# thing `coord/drive_queue.py`'s tick can actually read after the process is
# gone. See the 2026-08-04/05 overnight run (#1817): two identical, fully
# actionable refusals were retried and exhausted as "drive session died",
# discarding the guard's own remedy in the process.
EXIT_DISPATCH_REFUSED = 5
# #2019: the row is TERMINAL AND UNACTIONABLE — every stage finished, nothing
# is active on the fleet, and no gate transition is available to any amount of
# polling. Same *class* as EXIT_DISPATCH_REFUSED above (a condition retrying
# cannot change) but a different *cause*: nothing refused a dispatch here;
# the board simply came to rest in a shape with no legal move. Kept distinct
# so `coord/drive_queue.py`'s tick can block the entry with the RIGHT reason
# rather than the pre-dispatch-guard wording, and so an operator reading an
# exit code alone can tell "a guard said no" from "the pipeline dead-ended".
# See `coord.dead_end.detect_dead_end` for what qualifies, and #1956 /
# vimcode#635 for the two live incidents (140 minutes and ~25 minutes of a
# held queue slot, respectively, producing nothing).
EXIT_DEAD_END = 6
# #2443: a deliberate SELF-HEAL exit, not a failure — the drive loop noticed
# it was stuck repeating the identical WAIT reason (see `_SELF_HEAL_WAIT_
# STREAK`) while its OWN on-disk `coord` install had moved since this
# session started. Python never reloads an already-imported module, so a
# fix landing mid-run is otherwise invisible to a live session for as long
# as `--deadline` allows — the claude-coordinator#2286 incident this closes
# polled one unfetchable path every ~60s for 2+ hours after the fix was
# already on disk. Distinct from EXIT_DISPATCH_REFUSED/EXIT_DEAD_END on
# purpose: this condition is NOT permanent — relaunching is exactly the fix
# — so it must fall through `coord.drive_queue._reconcile_running`'s
# ordinary `retry` branch rather than either of those two `blocked`
# branches, which is why nothing in drive_queue.py needs to special-case
# this code at all.
EXIT_SELF_STALE = 7


class DriveError(Exception):
    """A configuration/usage problem — reported, never polled through."""

    def __init__(self, message: str, exit_code: int = EXIT_USAGE) -> None:
        super().__init__(message)
        self.exit_code = exit_code


# #2274: bound on `Driver._last_run_output` — the captured stdout+stderr of
# whatever `coord` subcommand `_spawn` just ran. Before this, a non-refusal
# RUN-action failure (e.g. `coord assign` dying for a reason that is not
# EXIT_DISPATCH_REFUSED — an API blip, a bad briefing file, a stack trace)
# discarded that text entirely and raised a bare "coord assign ... exited 1",
# which is the SAME string that ends up in `drive_queue.last_reason` and
# `drive_escalations.reason` — the two places an operator goes to diagnose a
# parked/blocked entry once the tmux session and its scrollback are gone
# (quadraui#508, coord-portal#83). A FEW KB is plenty to answer "why", and
# keeping this bounded matters because it lands in a DB column the tick reads
# on every poll, not a log file — `_append_run_log` still gets the full,
# untruncated bytes for local, on-host debugging.
_CAPTURED_OUTPUT_LIMIT = 4000

# #2360: attempt budget for a WORK-stage failure `coord.failure_class.
# classify_failure` calls environmental (a Claude API 5xx, a dropped
# connection, an overload — the module's own allow-listed signals), as
# opposed to `DriveOptions.max_work_retries` (default 1: one retry, two
# attempts total), which stays the tight budget for a genuine code defect.
# #2335: a work assignment died on an auth hiccup, its one flat retry died
# too, and the drive-queue entry sat `blocked` for ~19h — confirmed
# transient after the fact (the same machine ran dozens of clean sessions in
# that window). `max()` against `opts.max_work_retries` at the call site
# means a run configured with a bigger flat budget than this default is
# never *tightened* for the environmental case.
_ENVIRONMENTAL_WORK_RETRY_BUDGET = 5

# #2443: how many CONSECUTIVE polls of the IDENTICAL `Action.label` to
# tolerate before `Driver._loop` checks whether its own on-disk `coord`
# install has moved since this session started. Below this, a same-label
# repeat is indistinguishable from an ordinary slow-but-progressing wait (a
# 10-20 minute test, a slow CI check) and checking would just be wasted
# subprocess calls; at/above it, the check itself is a single local `git
# rev-parse HEAD` (no network — see `coord.self_health.self_freshness
# (fetch=False)`), cheap enough to run on every remaining poll of the
# streak. At the default `--poll` of 60s this is ~10 minutes — fast enough
# to matter against a multi-hour `--deadline`, slow enough that a routine
# wait is never mistaken for the stuck case this exists to catch.
_SELF_HEAL_WAIT_STREAK = 10


def _current_self_head() -> str | None:
    """Real default for `Driver.self_head_probe` (#2443): THIS process's own
    on-disk `coord` install's current HEAD, or ``None`` when it can't be
    determined (not a git checkout, unreadable HEAD — see
    :func:`coord.self_health.self_freshness`). ``fetch=False``: this only
    needs to notice code that has ALREADY landed locally (e.g. a prior `git
    pull`), never reaches the network, and therefore never blocks the poll
    loop on a slow/unreachable origin.
    """
    return self_freshness(fetch=False).head_sha


def _bounded_tail(text: str, limit: int = _CAPTURED_OUTPUT_LIMIT) -> str:
    """*text*, or its last *limit* characters if longer — the tail, because
    the actionable line (a traceback's final "raise ...", a guard's remedy)
    is overwhelmingly the END of stdout+stderr, not the start."""
    if len(text) <= limit:
        return text
    dropped = len(text) - limit
    return f"…[{dropped} chars truncated]…{text[-limit:]}"


# ── #2618: surviving a `coord-serve` restart mid-drive ──────────────────────
#
# Investigated before building anything, per the issue: is a `coord-serve`
# restart actually unsurvivable for an in-flight `coord drive`, or merely
# unretried? Answer is split by call shape.
#
# The READ path (`BoardFetcher.fetch()`, one `httpx.get` per poll) was
# already fine — `Driver.read_state()` catches every exception, including a
# connection refusal, and returns `None`; `_loop` just sleeps `--poll` and
# tries again next cycle. A `coord-serve` restart (a few seconds) sitting
# inside one poll interval is invisible there.
#
# The WRITE path was not: every RUN action (`coord assign`/`retry`/`fix`/
# `test`/`merge --only`/`review`/`escalate record`/`acceptance author`/
# `record`) is a `coord` subprocess that POSTs to the daemon exactly once,
# through `coord.client`'s bare `httpx` calls — no retry anywhere in that
# chain. Land one of those in the restart's connection-refused window and
# `_spawn` returned it to `_loop` as an ordinary failure, which (`on_error`
# defaulting to "die") raised `DriveError` and killed the WHOLE drive —
# not a blip survived, a drive lost. That is what forced `coord release
# propagate` to hold the daemon-host roll for FLEET-WIDE quiescence rather
# than the daemon host's own busy state (`release_propagate.py`'s "if the
# daemon host itself is occupied" rule): every other in-flight drive
# anywhere had to be presumed unable to survive the restart, because until
# now none of them could.
#
# The fix below closes exactly that gap, and only that gap: retry a RUN
# action's subprocess when its captured output carries a clean "connection
# refused" signature — the OS rejecting the connection because nothing is
# listening yet, which means the request never reached the daemon at all.
# Deliberately NOT retried for a reset or timeout mid-request: those are
# genuinely ambiguous (the daemon may have already processed the request
# before the restart killed it), and blindly retrying an ambiguous `coord
# assign`/`coord retry` reproduces the #476/#477 duplicate-dispatch shape.
# A refusal carries no such ambiguity, so retrying it is safe.
_DAEMON_CONN_REFUSED_MARKERS = (
    "connection refused",
    "failed to establish a new connection",
    # httpx's own wording for the same underlying `ConnectionRefusedError`
    # (seen wrapped in `httpx.ConnectError`'s `str()`).
    "connecterror",
)

#: Bounded — this is riding out a systemd restart (single-digit seconds),
#: not standing in for a real outage. `_DAEMON_CONN_REFUSED_DELAY_SECS *
#: _DAEMON_CONN_REFUSED_RETRIES` comfortably covers a `coord-serve` restart
#: while still giving up (and reporting a real failure) against a daemon
#: that is genuinely down.
_DAEMON_CONN_REFUSED_RETRIES = 4
_DAEMON_CONN_REFUSED_DELAY_SECS = 5.0


def _looks_like_daemon_connection_refused(output: str) -> bool:
    """Does *output* carry the OS-level "nothing is listening" signature —
    as opposed to a timeout, a reset, or an ordinary command failure?

    Matched on lowercased substring against a short, specific allow-list
    (mirrors `coord.failure_class`'s own "positive, specific signal, never a
    blanket 'any error' catch" posture) — a bare non-zero exit with an
    unrelated traceback must never be mistaken for a restart window, or a
    genuine bug would get silently retried and its evidence discarded.
    """
    lowered = output.lower()
    return any(marker in lowered for marker in _DAEMON_CONN_REFUSED_MARKERS)


# ── options ──────────────────────────────────────────────────────────────────


@dataclass
class DriveOptions:
    """Resolved flags.  Field names mirror the bash variables one-for-one."""

    machine: str = ""
    model: str = ""
    briefing_file: str = ""
    do_plan: bool = False
    max_fix_rounds: int = 3
    skip_test: bool = False
    repo_path: str = ""
    poll: float = 60.0
    max_work_retries: int = 1
    deadline_mins: float = 240.0
    stall_mins: float = 20.0
    notify: bool = False
    do_merge: bool = True
    merge_method: str = "rebase"
    accept_advisory: bool = False
    force_review: bool = False
    dry_run: bool = False
    max_merge_attempts: int = 3
    # #1453: skip the oracle-loop JIT slice authoring step below even when
    # this issue's milestone has a merged Gate-A contract — an escape hatch
    # for "the contract is stale/wrong for this issue" or "I want a plain
    # run", matching the opt-out every other oracle-loop gate offers
    # (`oracle:exempt` label, `exempt:` manifest list).
    no_acceptance: bool = False
    # Threaded onto every `coord` subprocess so a `coord drive --config X` run
    # cannot dispatch against a *different* config than it is reading.  The
    # bash driver ran a bare `coord` and silently had this gap.  Empty means
    # "let each subprocess resolve the default" ($COORD_CONFIG →
    # ~/.coord/coordinator.yml → ./coordinator.yml), i.e. today's behaviour.
    config_path: str = ""

    @property
    def stall_secs(self) -> float:
        return self.stall_mins * 60.0

    @property
    def deadline_secs(self) -> float:
        return self.deadline_mins * 60.0


@dataclass
class DriveCounters:
    """Bounds on every retry loop.  Unbounded merge retries was a real bug."""

    work_retries: int = 0
    # #2360: one-shot latch for the environmental WORK-stage backoff — the
    # 1-based attempt number (`work_retries + 1`) `_decide_work` has already
    # returned a backoff WAIT for. `-1` means "no backoff pending". Mirrors
    # `review_fix_dispatched_for`'s shape below: without it, a board that
    # hasn't changed yet (nothing was dispatched during the wait) would make
    # `decide()` re-issue the SAME backoff WAIT forever instead of, once the
    # sleep it already returned has elapsed, falling through to the actual
    # `coord retry` for that attempt.
    work_environmental_backoff_attempt: int = -1
    # ONE budget for BOTH fix arms (#1692): a failed test and a
    # request-changes review are two shapes of the same "the work needs
    # another round" loop, and a drive that spends three rounds bouncing
    # between them has spent three rounds. Bounded by `opts.max_fix_rounds`.
    fix_rounds: int = 0
    merge_attempts: int = 0
    review_dispatches: int = 0
    # #1584: bounded retry for a review WORKER that died (transient API
    # error, network drop, ...) before producing a verdict — the review-side
    # analogue of `work_retries`, bounded the same way (`opts.max_work_retries`).
    review_retries: int = 0
    # #1692: NOT a second budget — `fix_rounds` above is the budget. This is a
    # de-duplication latch: the assignment id of the review this driver has
    # already spent a fix round on. `coord fix` returns as soon as the fix
    # worker is dispatched, but the board this driver polls needs a beat to
    # show the new row; until it does, the state is byte-for-byte identical to
    # the one that triggered the dispatch. Without this latch the next poll
    # re-fires `coord fix` against the same review and spawns a SECOND fix
    # worker on the same branch — the #476/#477 shape, in a new dispatcher.
    # Cleared implicitly rather than explicitly: the next review round is a
    # different review row (`drive_state.project` keys the review on the
    # current work id), so its id simply doesn't match this one.
    review_fix_dispatched_for: str = ""
    # #2078: the combined stdout+stderr of the MOST RECENT `coord merge
    # --only <aid>` attempt (`Driver._loop` captures it via `run_coord`'s own
    # `_last_run_output`, right after each merge Action runs). When the board
    # carries no merge-queue entry at all (`merge_status == ""`), this is the
    # ONLY place the real reason lives: `coord merge --only` prints
    # `_explain_missing_only_entry`'s diagnosis (naming the blocking
    # review/smoke gate, or "identifier didn't resolve", or "all gates
    # pass — not enqueued yet") on every such attempt, but the driver used to
    # discard it and echo the board's empty fields instead. Threaded through
    # `DriveCounters` (not returned some other way) because `_decide_merge`
    # is pure and only ever sees this value on the NEXT poll after the
    # attempt that produced it — one poll's staleness is the price of
    # keeping the I/O boundary in `Driver`, not in `decide()`.
    last_merge_diagnostic: str = ""
    # #2149: consecutive polls spent waiting on `last_merge_diagnostic`'s
    # CACHED gate reason (the `status == ""` arm of `_decide_merge`) without
    # a fresh `coord merge --only` attempt in between. That cached text is a
    # snapshot of the LAST real attempt — nothing re-validates it while this
    # counter is the only thing advancing, so a gate that clears on its own
    # is structurally invisible for as long as the wait continues unbounded.
    # `_decide_merge` bounds the streak at `_MAX_GATE_WAIT_ROUNDS`: once
    # reached, it resets this to 0 and falls through to a REAL attempt
    # instead of reprinting the same frozen reason, which is what actually
    # notices a cleared gate (coord-portal#50: 2h33m spent re-printing an
    # identical "review required" line for a review that was never real).
    # Reset to 0 whenever a real attempt refreshes the diagnostic — a fresh
    # capture deserves its own full budget of cheap waits before the next
    # forced retry.
    gate_wait_rounds: int = 0
    # #2229: the value `merge_attempts` held when the #1738 stale-smoke
    # re-test last fired off a reason read out of `last_merge_diagnostic`
    # rather than off the board (`-1` = never). A de-duplication latch in the
    # same spirit as `review_fix_dispatched_for` above, for the same reason:
    # `coord diagnose --stage test --reset` clears `test_state`, but the board
    # this driver polls needs a beat to show it, and until it does the state
    # is byte-for-byte identical to the one that triggered the re-test —
    # except that, unlike a board `merge_reason`, the captured diagnostic is
    # frozen and CANNOT change on its own. Without the latch a lagging board
    # would burn the whole `max_fix_rounds` budget re-testing off one stale
    # snapshot. Releases when `merge_attempts` advances, i.e. when a real
    # `coord merge --only` attempt has re-captured the diagnostic — which is
    # #2149's rule (act on the snapshot, then go re-validate it for real)
    # expressed as a latch rather than a wait counter.
    stale_smoke_diagnostic_attempt: int = -1
    # #2199: bounded retry for the trust gate's OWN dispatch — distinct from
    # `fix_rounds` (which bounds "the work needs another round" once a
    # verdict, passed or failed, actually landed on the board). This counts
    # attempts where `coord acceptance record` itself never got that far —
    # a missing local checkout, a driver crash, GitHub unreachable — so a
    # persistently broken environment reaches a named `_die()` instead of
    # `coord drive` re-running a full git-worktree + suite invocation every
    # poll forever. Bounded by `opts.max_work_retries`, the same "how many
    # times do we retry a flaky infra thing" budget `work_retries` uses.
    #
    # #2199 review (blocking finding 1): scoped to ONE sha via
    # `acceptance_gate_attempts_sha` below — NOT a lifetime total. A fresh
    # SHA (a fix round pushing a new commit) legitimately needs its own
    # dispatch, and that dispatch is the entire point of the "same shape as
    # `_decide_test`'s fix loop" design a few lines up in
    # `_decide_acceptance_gate`; sharing one un-reset counter across every
    # SHA a drive ever sees meant the SECOND round after the FIRST fix round
    # already exhausted the default `max_work_retries=1` budget and died
    # with a false "environment broken" diagnosis — even though the trust
    # gate was working exactly as designed (quadraui#542's actual shape:
    # one fix round, then this).
    acceptance_gate_attempts: int = 0
    # #2199 review: the SHA `acceptance_gate_attempts` above is counting
    # dispatch attempts FOR. `_decide_acceptance_gate` resets the counter to
    # 0 whenever it sees a SHA that doesn't match this — a fresh commit (a
    # fix round, a rebase) always gets its own full attempt budget, and only
    # repeated failures to produce ANY verdict for the SAME commit reach
    # `_die()`.
    acceptance_gate_attempts_sha: str = ""
    # #2079: a SECOND, independent budget of exactly the same shape, spent
    # only on landing the oracle-mode JIT acceptance slice
    # (`_decide_acceptance_landing`). Separate rather than shared because the
    # slice and the issue's own work row are two different PRs with two
    # different merge queues and two different `coord merge --only` targets:
    # three attempts spent landing the slice must not silently leave the work
    # row's own merge with zero. Lazily created (`slice_budget`) so the
    # overwhelming majority of drives — non-oracle ones — never allocate it,
    # and `--dry-run`'s counter snapshot stays unchanged for them.
    acceptance: "DriveCounters | None" = None
    # #2416: bounded retry for an ADVISORY work row whose branch carries zero
    # commits — `_decide_advisory`'s dead-end signature. Before this, that
    # branch went straight to `_die()` on every observation, so the only
    # thing that could ever supersede the terminal row was a human running
    # `coord retry <aid>` by hand (`coord diagnose --stage work --reset` is
    # NOT the escape hatch here — it never sets `needs_reset` for this
    # shape). `drive-queue`'s own automatic retry does not take that path
    # either: it just relaunches a brand new `coord drive` process, which
    # re-observes the SAME terminal row and dies again in seconds, burning a
    # queue attempt+backoff cycle for nothing (coord-portal#119, drive-queue
    # entry 432). Bounded the same way `work_retries` is (`opts.
    # max_work_retries`) so a genuinely unactionable row (the issue's real
    # resolution is a sibling task, not another attempt) still reaches a
    # terminal `_die()` — never an unbounded loop — once the budget is
    # spent.
    advisory_retries: int = 0
    # #2334: the acceptance-author arm's own copy of `advisory_retries` —
    # `_decide_acceptance_author`'s ADVISORY and DONE branches both hit the
    # identical "terminal row, zero commits on its branch" dead end
    # `_decide_advisory` mirrors above, and until now both were a deliberate
    # COPY of the pre-#2416 `_die()`-on-first-look behaviour (their own
    # comments said so: "Mirror `_decide_advisory` exactly"). A bounded
    # number of FRESH `coord acceptance author` dispatches (`opts.
    # max_work_retries`) before finally giving up — a new author, not
    # `coord retry`, because the #1606 zero-commit-advisory reassignment
    # path `coord retry` uses is a WORK-row concept; `type="test-author"`
    # has no analogous CLI verb of its own. Separate counter, not a shared
    # one with `advisory_retries`, because a single drive run can retry the
    # slice's author AND the issue's own work row in the same session —
    # sharing a counter would let one budget silently starve the other.
    acceptance_author_retries: int = 0

    def slice_budget(self) -> "DriveCounters":
        """This run's slice-landing budget, created on first use (#2079)."""
        if self.acceptance is None:
            self.acceptance = DriveCounters()
        return self.acceptance


# ── actions ──────────────────────────────────────────────────────────────────

WAIT = "wait"
RUN = "run"
EXIT = "exit"


@dataclass(frozen=True)
class Action:
    """What the loop should do next.  The only thing :func:`decide` returns.

    ``command`` is the ``coord`` subcommand argv **without** the ``coord``
    binary itself — the driver prepends it.  Keeping it here (rather than
    building argv inside the executor) is what lets a unit test assert the
    exact CLI contract, e.g. that a skipped Test gate really is
    ``coord test --skipped --reason ... <aid>`` and not a direct
    ``record_test_verdict()`` call (#1384).
    """

    kind: str
    label: str = ""
    message: str = ""
    exit_code: int = 0
    command: tuple[str, ...] = ()
    sleep_after: float | None = None  # None → the poll interval
    on_error: str = "die"  # "die" | "warn"
    error_message: str = ""
    serialize_merge: bool = False
    warnings: tuple[str, ...] = ()
    # #2079: which merge this Action's `coord merge --only` attempt belongs
    # to — the issue's own work row ("work", the default and the only value
    # before #2079) or the oracle-mode JIT acceptance slice ("acceptance").
    # Read by `Driver._loop` to file the captured diagnostic against the
    # matching `DriveCounters` (see `DriveCounters.acceptance`); a slice
    # attempt's `_explain_missing_only_entry` output must not overwrite what
    # the work row's own last attempt reported, or `_decide_merge` would
    # diagnose one PR using the other PR's gates.
    merge_scope: str = "work"
    # #2871: an explicit ``(event_type, summary, details)`` for `Driver._loop`
    # to write via `_record_drive_audit`, independent of whatever this Action
    # otherwise does (WAIT/RUN/EXIT). `decide()` stays a pure function of its
    # inputs — it hands back *what happened*, never writes the audit log
    # itself — while still letting a poll that neither dispatches nor exits
    # (e.g. bypassing a stale pre-dispatch refusal) leave a durable trail
    # instead of only a run-log line nobody queries later.
    audit_event: tuple[str, str, dict[str, Any]] | None = None

    @property
    def is_exit(self) -> bool:
        return self.kind == EXIT


def _wait(sleep_after: float | None = None, label: str = "") -> Action:
    return Action(kind=WAIT, label=label, sleep_after=sleep_after)


def _succeed(message: str) -> Action:
    return Action(kind=EXIT, message=message, exit_code=EXIT_OK)


def _die(message: str, exit_code: int = EXIT_TERMINAL_FAILURE) -> Action:
    return Action(kind=EXIT, message=message, exit_code=exit_code)


def _format_age(seconds: float) -> str:
    """Coarse human age ('38m', '15h', '1.2d') for a pre-dispatch audit note.

    Deliberately not the CLI's full relative-time renderer — this is one
    field inside an audit `summary`/`details`, not a UI surface, so a small
    self-contained formatter is enough (#2871).
    """
    seconds = max(0.0, seconds)
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{seconds / 3600:.0f}h"
    return f"{seconds / 86400:.1f}d"


class RefusedPolicyStaleness(NamedTuple):
    """The verdict :func:`_refused_policy_staleness` reaches, plus WHY —
    #2881: collapsing "no title resolved" and "title unchanged" to the same
    ``False`` (as the original #2871 boolean-only version did) is what let
    the #2881 bug (``title`` missing from the daemon-host payload,
    ``coord/drive_state.py``'s ``_local_issue_rows``) hide for a whole
    release: every real drive landed in ``uncertain`` silently, read
    identically to a confidently-checked ``unchanged``, and the operator-
    facing message could not tell the two apart either.
    """

    is_stale: bool
    uncertain: bool
    detail: str


def _refused_policy_is_stale(state: IssueState) -> bool:
    """True when a terminal ``refused_policy`` work row no longer describes
    the issue as it stands now (#2871). Thin bool wrapper around
    :func:`_refused_policy_staleness` — see that function for the reasoning
    and for the ``uncertain``/``detail`` distinction #2881 added.
    """
    return _refused_policy_staleness(state).is_stale


def _refused_policy_staleness(state: IssueState) -> RefusedPolicyStaleness:
    """Does a terminal ``refused_policy`` work row still describe the issue
    as it stands now (#2871), and how confident is that answer (#2881)?

    A worker refuses pre-dispatch work exactly once, correctly, for the ask
    it was shown — the refusal says nothing about a DIFFERENT ask on the same
    issue number. If the issue was retargeted (title rewritten) after that
    row finished, the branch it would have produced (workers name branches
    ``issue-{N}-{slugify(title)}`` at dispatch time, see
    ``coord.agent``'s ``branch_name`` construction) no longer matches what a
    fresh dispatch would name — that mismatch is the signal a retarget
    happened.

    Conservative on every uncertainty (no branch recorded, no title
    resolved): ``is_stale=False`` — i.e. still treated as blocking — rather
    than risk waving a genuinely-unresolved refusal through. #2881: those two
    uncertain cases are NOT the same situation as a confidently-resolved
    "title unchanged", so ``uncertain=True`` and a specific ``detail`` come
    back for the caller to surface — a die()/audit message that confidently
    tells the operator "retarget the issue" is actively wrong when the code
    could not even check, e.g. because the daemon-host board payload dropped
    ``title`` again.
    """
    if not state.work_branch:
        return RefusedPolicyStaleness(
            is_stale=False,
            uncertain=True,
            detail=(
                "no branch was recorded on the refused_policy row, so "
                "whether the issue was retargeted since could not be checked"
            ),
        )
    if not state.issue_title:
        return RefusedPolicyStaleness(
            is_stale=False,
            uncertain=True,
            detail=(
                "the issue's current title did not resolve from this "
                "drive's board payload, so whether it was retargeted since "
                "could not be checked (#2881 — if this fires in production, "
                "suspect the daemon-host local-DB payload again)"
            ),
        )
    from coord.agent import _slugify  # noqa: PLC0415

    expected_branch = f"issue-{state.issue}-{_slugify(state.issue_title)}"
    if state.work_branch != expected_branch:
        return RefusedPolicyStaleness(
            is_stale=True, uncertain=False, detail="retargeted since the refusal",
        )
    return RefusedPolicyStaleness(
        is_stale=False,
        uncertain=False,
        detail="the issue's title is unchanged since the refusal",
    )


# ── oracle-loop JIT slice authoring (#1453) ─────────────────────────────────


class AcceptanceGateChecker(Protocol):
    """The GitHub questions :func:`resolve_oracle_decision` and
    :func:`_decide_acceptance_author` cannot answer from the board payload
    alone: has Gate A's contract actually merged, and (for a routed repo)
    which subtree does this milestone's slice belong to?"""

    def contract_exists(self, repo_name: str, milestone_number: int) -> bool: ...

    def resolve_for_path(self, repo_name: str, milestone_number: int) -> str | None: ...

    def is_issue_exempt(
        self,
        repo_name: str,
        milestone_number: int,
        issue_number: int,
        issue_labels: tuple[str, ...],
    ) -> bool: ...

    def has_authored_slice(
        self, repo_name: str, milestone_number: int, issue_number: int,
    ) -> bool: ...


@dataclass
class GitHubAcceptanceGateChecker:
    """Real implementation: reuses ``coord.milestone_dispatch.gate_a_status``
    — the SAME check ``coord milestone dispatch``'s Gate A gate and the
    #1138 ``issue_oracle_ready`` hard gate already run — rather than
    re-deriving the ``tests/acceptance/ms-NN/contract.md`` path here. That
    function returns ``None`` for two different reasons ("no driver
    configured" or "contract exists"); callers of this checker have already
    confirmed ``config.acceptance.has_driver(repo_name)`` themselves
    (:func:`resolve_oracle_decision` does), so ``None`` is unambiguous here.
    """

    config: Any

    def contract_exists(self, repo_name: str, milestone_number: int) -> bool:
        from coord.milestone_dispatch import gate_a_status  # noqa: PLC0415

        repo_cfg = self.config.repo(repo_name)
        if repo_cfg is None:
            return False
        return gate_a_status(repo_cfg, self.config, milestone_number) is None

    def resolve_for_path(self, repo_name: str, milestone_number: int) -> str | None:
        """#1453 review finding 1: the ``--for-path`` a routed repo's JIT
        acceptance-author dispatch needs. Delegates to
        :func:`coord.acceptance.resolve_for_path` (the SHARED derivation —
        see its docstring); raises :class:`coord.acceptance.
        ForPathResolutionError` unchanged so callers report it verbatim.
        """
        from coord.acceptance import resolve_for_path  # noqa: PLC0415

        repo_cfg = self.config.repo(repo_name)
        if repo_cfg is None:
            return None
        return resolve_for_path(self.config, repo_cfg, milestone_number)

    def is_issue_exempt(
        self,
        repo_name: str,
        milestone_number: int,
        issue_number: int,
        issue_labels: tuple[str, ...],
    ) -> bool:
        """#2199: does *issue_number* opt out of the sealed suite — the
        SAME ``manifest.exempt`` list / ``oracle:exempt`` label the #1138
        hard gate (:func:`coord.milestone_dispatch.issue_oracle_ready`)
        already reads. Reused rather than re-derived so the JIT-authoring
        gate and the #2199 trust gate can never disagree about which
        issues the oracle loop covers — see that function's own docstring
        for why ``exempt`` says "doesn't consume the sealed suite" and
        nothing about Gate A sign-off (already established True by the
        time :func:`resolve_oracle_decision` calls this).
        """
        from coord.milestone_dispatch import issue_oracle_ready  # noqa: PLC0415

        repo_cfg = self.config.repo(repo_name)
        if repo_cfg is None:
            return False
        readiness = issue_oracle_ready(
            repo_cfg, self.config, milestone_number, issue_number,
            issue_labels=issue_labels,
        )
        return readiness.exempt

    def has_authored_slice(
        self, repo_name: str, milestone_number: int, issue_number: int,
    ) -> bool:
        """#2061: the AUTHORITATIVE answer to "has this issue's JIT slice
        landed?" — the manifest on the repo's default branch, read via the
        identical ``test_ids_for_issue`` check the #1138 hard gate
        (:func:`coord.milestone_dispatch.issue_oracle_ready`) already
        performs, exposed here as ``OracleReadiness.has_slice``.

        :func:`_decide_acceptance_author` calls this — instead of trusting
        the `type="test-author"` assignment row alone — at every point
        where the row is about to be read as "nothing was authored": no
        row yet, or a terminal (``failed``/``cancelled``/``advisory``/
        ``done``) status with no branch commits. The assignment row is a
        PROXY for this question; it can be stale (a drive that died
        mid-run, a retry, or a #2020-mangled row) while an EARLIER attempt
        already merged the slice. The manifest cannot lie about that: if
        the slice is there, it landed, whatever the row says.
        """
        from coord.milestone_dispatch import issue_oracle_ready  # noqa: PLC0415

        repo_cfg = self.config.repo(repo_name)
        if repo_cfg is None:
            return False
        readiness = issue_oracle_ready(
            repo_cfg, self.config, milestone_number, issue_number,
        )
        return readiness.has_slice


@dataclass(frozen=True)
class OracleDecision:
    """Resolved ONCE per run (at preflight time, alongside machine
    resolution) — never recomputed per poll, since *gate_checker* costs a
    GitHub fetch and a milestone's Gate-A status does not change mid-run.

    ``active`` gates the JIT-authoring branch in :func:`_dispatch_work_stage`;
    ``reason`` is what the preflight banner prints so an operator never has
    to guess which mode a run is in. ``tracking_issue`` is set iff ``active``
    — the argument :func:`_decide_acceptance_author` needs to build ``coord
    acceptance author <repo> <tracking_issue> --issue <N>``.

    #2199: ``issue_exempt`` — resolved alongside ``active`` (same one-shot
    GitHub fetch budget: ``AcceptanceGateChecker.is_issue_exempt`` reuses the
    #1138 hard gate's own manifest read) — is *this issue*'s
    ``manifest.exempt``/``oracle:exempt`` opt-out from the sealed suite.
    :func:`_decide_acceptance_gate` (the trust gate) skips entirely when
    this is ``True``: an exempt issue has no authored slice for `coord
    acceptance record` to re-run, so treating its absence as a red verdict
    would be exactly the "acquire a new blocking gate it never opted into"
    regression #2199's acceptance criteria rule out. Always ``False`` when
    ``active`` is ``False`` — meaningless outside the oracle loop.
    """

    active: bool
    reason: str
    tracking_issue: int | None = None
    issue_exempt: bool = False


def resolve_oracle_decision(
    state: IssueState,
    opts: DriveOptions,
    config: Any,
    gate_checker: AcceptanceGateChecker,
) -> OracleDecision:
    """The #1453 gate: does this issue's Work dispatch get preceded by an
    independent JIT acceptance-slice authoring session?

    Mirrors — and must never drift from — the same rule the TUI's
    ``gate_a_contract_exists_for`` (``tui/src/app/pipeline.rs``) and
    ``coord.milestone_dispatch.gate_a_status`` already enforce, both via
    :func:`coord.acceptance.gate_a_contract_path`: a repo with a configured
    acceptance driver, an issue that resolves to a milestone with a tracking
    issue, and a Gate-A contract already merged for that milestone. This
    complements (does not replace) the #1138 hard gate
    (``coord.dispatch.enforce_oracle_readiness``), which would otherwise
    just refuse the eventual ``coord assign``/``coord approve-plan`` with no
    explanation once an oracle-opted-in milestone's issue reaches it — this
    proactively drives the authoring + merge to completion FIRST so a plain
    ``coord drive`` doesn't dead-end on that refusal.

    #2079: "drives the merge to completion" is now literally true. Until
    #2079 this module said so here and said the opposite in
    :func:`_decide_acceptance_author` ("this only observes"), and the
    observing version was the one that shipped — so every oracle issue burned
    ``2 × --deadline`` waiting for ``serve_app._auto_drain_tick``, which is
    off (``merge.auto_drain: false``) in the standing fleet config and merges
    nothing, ever. :func:`_decide_acceptance_landing` is the reconciliation.
    """
    if opts.no_acceptance:
        return OracleDecision(False, "--no-acceptance set — normal drive")
    if not config.acceptance.has_driver(state.repo):
        return OracleDecision(
            False, f"{state.repo!r} has no acceptance.drivers entry — normal drive"
        )
    if state.milestone_number is None:
        return OracleDecision(
            False, f"#{state.issue} has no GitHub milestone — normal drive"
        )
    if state.milestone_tracking_issue is None:
        return OracleDecision(
            False,
            f"#{state.issue} isn't a member of a tracked milestone work order — "
            "normal drive",
        )
    if not gate_checker.contract_exists(state.repo, state.milestone_number):
        from coord.acceptance import gate_a_contract_candidates  # noqa: PLC0415

        # #2896: name every root this milestone's contract could live under
        # (shared repo-root tree, or an entrypoint-linked driver's own
        # relocated sibling dir) — `contract_exists` above already searched
        # all of them and found none, so the message should too rather than
        # naming only the legacy repo-root candidate.
        candidates = gate_a_contract_candidates(config, state.repo, state.milestone_number)
        named = " or ".join(repr(p) for p in candidates)
        return OracleDecision(
            False,
            f"Gate A contract {named} not merged yet on "
            f"{state.repo_default_branch!r} — normal drive (run `coord "
            f"acceptance mock {state.repo} {state.milestone_tracking_issue}` "
            "first for the oracle loop, docs/ORACLE_LOOP.md)",
        )
    # #2199: resolved here, alongside everything else `resolve_oracle_decision`
    # already settles once per run — see `OracleDecision.issue_exempt`'s
    # docstring for why the trust gate needs this and why it must reuse
    # (not re-derive) the #1138 hard gate's own exemption check.
    issue_exempt = gate_checker.is_issue_exempt(
        state.repo, state.milestone_number, state.issue, state.issue_labels,
    )
    return OracleDecision(
        True,
        f"ORACLE DRIVE — ms-{state.milestone_number}'s Gate-A contract is "
        f"merged: authoring the sealed JIT slice for #{state.issue} "
        f"(`coord acceptance author {state.repo} "
        f"{state.milestone_tracking_issue} --issue {state.issue}`) before "
        "dispatching work",
        tracking_issue=state.milestone_tracking_issue,
        issue_exempt=issue_exempt,
    )


def _dispatch_fresh_acceptance_author(
    state: IssueState,
    oracle: OracleDecision,
    gate_checker: AcceptanceGateChecker,
    *,
    label_suffix: str = "",
) -> Action:
    """Build the ``coord acceptance author ...`` RUN action for *state*'s
    issue — the ONE command that can put a new, non-terminal ``test-author``
    row on the board.

    Shared by two call sites in :func:`_decide_acceptance_author`: the
    first-ever dispatch (``aid`` empty) and the #2334 bounded retry (a
    terminal row whose branch carried zero commits — the mirror of
    ``_decide_advisory``'s own #2416 fix). Both need the identical
    ``--for-path`` resolution and error handling; before this helper existed
    only the first dispatch had it; the retry path had no dispatch at all.
    """
    command = [
        "acceptance", "author", state.repo, str(oracle.tracking_issue),
        "--issue", str(state.issue),
    ]
    # #1453 review finding 1: a ROUTED repo's `coord acceptance author`
    # hard-refuses with no --for-path (coord.test_author.
    # dispatch_test_author's "no route matched" RuntimeError) — resolve
    # it from the milestone's Gate-A mock kind (the SHARED
    # coord.acceptance.resolve_for_path helper) before ever dispatching,
    # so a routed repo's very first JIT-authoring attempt doesn't die.
    from coord.acceptance import ForPathResolutionError  # noqa: PLC0415

    try:
        for_path = gate_checker.resolve_for_path(state.repo, state.milestone_number)
    except ForPathResolutionError as exc:
        return _die(
            f"could not resolve --for-path for {state.repo}'s JIT "
            f"acceptance slice on #{state.issue}: {exc}"
        )
    if for_path:
        command += ["--for-path", for_path]

    return Action(
        kind=RUN,
        label=(
            "ACCEPTANCE: authoring sealed JIT slice → coord acceptance "
            f"author {state.repo} {oracle.tracking_issue} --issue "
            f"{state.issue}"
            + (f" --for-path {for_path}" if for_path else "")
            + label_suffix
        ),
        command=tuple(command),
        error_message=(
            f"coord acceptance author failed to dispatch for #{state.issue}. "
            "Check coordinator.yml's acceptance.drivers entry for "
            f"{state.repo!r}, or re-run coord drive with --no-acceptance "
            "to skip JIT authoring."
        ),
    )


def _decide_acceptance_author(
    state: IssueState,
    oracle: OracleDecision,
    opts: DriveOptions,
    counters: DriveCounters,
    machine: str,
    gate_checker: AcceptanceGateChecker,
    verifier: MergeVerifier,
) -> Action | None:
    """The #1453 JIT-slice gate itself. ``None`` means "landed — fall
    through to dispatching work normally" (only ever called when
    ``oracle.active``).

    Drives a `type="test-author"` assignment scoped to THIS issue
    (``for_issue_number == state.issue`` — #1171/#1138 key the JIT slice's
    row on the milestone's TRACKING issue via `issue_number`, so it never
    shows up as this issue's own ``work_aid``; see ``IssueState``'s
    docstring) all the way to ``status='merged'`` (#609) — the identical
    terminal signal :func:`decide`'s own merged check uses for the real work
    row.

    **#2079 — how much of that landing is actually somebody else's job.**
    The slice row is `WORK_LIKE` (``coord.models.WORK_LIKE_TYPES`` contains
    ``"test-author"``), so the daemon's passive tick really does run its
    Test and Review stages and really does enqueue it
    (``dispatch_pending_smoke`` / ``dispatch_pending_reviews`` /
    ``merge_queue.enqueue_approved_work``) with zero help from this driver.
    Every one of those steps runs unconditionally. Exactly ONE does not: the
    final drain, ``serve_app._auto_drain_tick``, gated on
    ``merge.auto_drain`` — ``false`` by default and ``false`` in the standing
    fleet config. So the pre-#2079 comment here ("this only observes") was
    describing a pipeline with its last stage switched off: the slice reached
    READY with a green, ``MERGEABLE``/``CLEAN`` PR and then nothing merged
    it, ever, while this driver idled to ``--deadline`` twice and left the
    issue ``blocked`` (terminal — a manual ``remove`` + ``add`` to clear).
    Landing that last step is what :func:`_decide_acceptance_landing` does,
    with the same bounded ``coord merge --only <aid>`` call
    :func:`_decide_merge` already makes for the work row.

    **#2061 — the assignment row is a proxy, the manifest is the answer.**
    Every branch below that would otherwise treat "no row" or "a terminal
    row with no branch commits" as "nothing was authored" first asks
    :func:`_slice_already_landed`, which reads the manifest on the repo's
    default branch — the same ``test_ids_for_issue`` check the #1138 hard
    gate performs. A drive retry re-dispatches this gate without knowing
    whether an EARLIER attempt already merged the slice; the second author
    then correctly does nothing (``DONE`` with zero commits, or a stale
    row that never even started) and, pre-#2061, that correct no-op was
    read as a terminal failure — killing a run whose slice was already
    landed and whose real work was ready to dispatch (coord-portal#13).
    """
    aid = state.acceptance_author_aid
    status = state.acceptance_author_status

    def _slice_already_landed() -> bool:
        """#2061: the `type="test-author"` row above is a PROXY for "has
        this issue's slice landed?" — a drive that died mid-run, a retry,
        or a #2020-mangled row can leave it pointing at nothing (or at a
        genuine failure) while an EARLIER attempt already merged the slice
        from a previous run. Ask the AUTHORITATIVE question — the manifest
        on the default branch, via :meth:`AcceptanceGateChecker.
        has_authored_slice` — whenever the row is about to be read as
        "nothing was authored": right before dispatching a fresh author,
        and right before declaring a terminal, commit-less row a failure.
        Never consulted for "" / "running" (still authoring — the slice
        cannot be on the default branch yet) or "merged" (already the
        strongest signal there is), so this costs one extra fetch at a
        decision point, not one per poll tick.
        """
        return gate_checker.has_authored_slice(
            state.repo, state.milestone_number, state.issue,
        )

    if not aid:
        # #2061: a retry (or a drive resuming after this run's own board
        # row went missing/stale) must not re-dispatch an author for a
        # slice that already landed from an earlier attempt.
        if _slice_already_landed():
            return None
        return _dispatch_fresh_acceptance_author(state, oracle, gate_checker)

    if status == "merged":
        return None

    if status == "failed":
        # #2061: this row's FAILED status describes what happened to THIS
        # author, not whether the issue's slice exists — a retry's row can
        # fail (or simply be stale) while an earlier attempt already merged
        # the slice it was about to re-author.
        if _slice_already_landed():
            return None
        return _die(
            f"acceptance author {aid} failed — inspect: coord log {aid} "
            f"--machine {state.acceptance_author_machine or machine}\n"
            "   Continue by hand, or re-run coord drive with "
            "--no-acceptance to skip JIT authoring."
        )

    if status == "cancelled":
        if _slice_already_landed():
            return None
        return _die(
            f"acceptance author {aid} was cancelled — re-dispatch by hand: "
            f"coord acceptance author {state.repo} {oracle.tracking_issue} "
            f"--issue {state.issue}\n"
            "   or re-run coord drive with --no-acceptance."
        )

    if status == "refused_policy":
        # #2234 fix-1: `type="test-author"` is one of `_ZERO_COMMIT_TYPES`
        # (coord.agent), so a JIT acceptance-author row goes through the
        # identical `_looks_like_policy_refusal` classification as a plain
        # `work` row and can reap `status="refused_policy"` exactly like
        # `decide()`'s own `state.work_status == "refused_policy"` branch
        # above handles. Before this branch existed, this status fell
        # straight through every check here into the final catch-all
        # `_wait(...)` below, treating a terminal, correctly-refused
        # test-author row as "still authoring" — spinning until
        # `--deadline` instead of parking. Mirror the `advisory` branch's
        # #2061 check (a stale/retried refusal can still coexist with an
        # earlier attempt's slice already landed) before dying, and embed
        # `POLICY_REFUSAL_MARKER` so `coord/drive_queue.py`'s
        # `_reconcile_running` parks the queue entry without spending an
        # attempt, same as the work-status branch.
        if _slice_already_landed():
            return None
        return _die(
            f"acceptance author {aid} refused on a standing repo-rule "
            "prohibition rather than authoring the JIT slice — the worker "
            "did the CORRECT thing (#2234). Needs the coordinator: author "
            "the slice directly (or re-scope so its deliverable isn't "
            "coordinator-only), then `coord drive-queue remove "
            f"{state.repo} {state.issue}` once handled.\n"
            f"   inspect: coord log {aid} --machine "
            f"{state.acceptance_author_machine or machine}\n"
            f"   {POLICY_REFUSAL_MARKER}"
        )

    if status == "advisory":
        # #1453 review finding 2: this is the #1386 bug class reborn — an
        # ``advisory`` row is TERMINAL (drive_state.TERMINAL_STATUSES) and
        # is explicitly excluded from coord's Test/Review/Merge auto-loop
        # (coord.reconcile's "review_state = 'advisory'" skip), so it will
        # NEVER transition to 'merged' on its own — treating it as
        # "still landing" below would spin forever. Mirrors
        # `_decide_advisory`: a real 0-commit exit gets the same #2334
        # bounded-retry-then-die treatment #2416 gave the work row (a fresh
        # `coord acceptance author`, not `coord retry` — see
        # `DriveCounters.acceptance_author_retries`); a #1357-style false
        # positive (commits present) needs the same `--accept-advisory`
        # opt-in the main work row uses, not a silent pass-through.
        branch = state.acceptance_author_branch
        probe = replace(state, work_branch=branch) if branch else state
        commits = verifier.branch_has_commits(probe) if branch else False
        if commits is None:
            # #2426: `None` means the check could not be completed (e.g. a
            # transient `git fetch` failure) — NOT evidence the branch is
            # empty. Wait for the next poll to retry rather than
            # misdiagnosing a verification failure as the terminal
            # zero-commit dead end below.
            return _wait(
                label=(
                    f"ACCEPTANCE: JIT slice {aid} branch {branch!r} — could "
                    "not verify commits (git fetch failed), retrying"
                )
            )
        if not commits:
            # #2061: a commit-less ADVISORY row IS what an earlier-landed
            # slice's stale/retried author row looks like — check the
            # manifest before declaring failure.
            if _slice_already_landed():
                return None
            # #2334: this used to be an immediate, unconditional `_die()` —
            # the SAME dead end #2416 fixed for `_decide_advisory`'s work
            # row, left un-mirrored here despite this branch's own comment
            # saying "Mirror `_decide_advisory` exactly". Nothing could ever
            # supersede the terminal row automatically: `drive-queue`'s own
            # retry just relaunches a brand new `coord drive`, which
            # re-observes the identical row and dies again in seconds
            # (claude-coordinator#2531: six attempts, same wall, every
            # time). Dispatch a FRESH `coord acceptance author`, bounded by
            # `opts.max_work_retries` via `counters.acceptance_author_retries`
            # — the same budget/shape as the work-row fix — before finally
            # giving up for a human.
            budget = opts.max_work_retries
            if counters.acceptance_author_retries >= budget:
                return _die(
                    f"acceptance author {aid} exited ADVISORY with no "
                    f"commits on its branch {counters.acceptance_author_retries} "
                    f"time(s) in a row (budget {budget}, #2334) — nothing "
                    "was authored, so there is no slice to land, and "
                    "retrying has not produced a different outcome.\n"
                    f"   inspect: coord log {aid} --machine "
                    f"{state.acceptance_author_machine or machine}\n"
                    "   this needs an operator decision: re-author by hand "
                    f"(coord acceptance author {state.repo} "
                    f"{oracle.tracking_issue} --issue {state.issue}), or "
                    "re-run coord drive with --no-acceptance to skip JIT "
                    "authoring."
                )
            counters.acceptance_author_retries += 1
            return _dispatch_fresh_acceptance_author(
                state, oracle, gate_checker,
                label_suffix=(
                    f" (attempt {counters.acceptance_author_retries}/{budget}"
                    ", #2334 retry after zero-commit ADVISORY)"
                ),
            )
        if not opts.accept_advisory:
            return _die(
                f"acceptance author {aid} is ADVISORY, but its branch carries "
                "real commits (the #1357 signature — see _decide_advisory).\n"
                "   Proceed anyway with --accept-advisory, or re-run coord "
                "drive with --no-acceptance."
            )
        # #2079: "proceeding per --accept-advisory" now proceeds. This used
        # to be a bare WAIT, which for an ADVISORY row is unreachable-by-
        # construction: `coord.reconcile` explicitly skips advisory rows in
        # the Test/Review/Merge auto-loop (the very fact the comment above
        # cites), so the thing being waited for could not happen even with
        # `merge.auto_drain` on.
        return replace(
            _decide_acceptance_landing(state, oracle, opts, counters, machine),
            warnings=(
                f"ACCEPTANCE: JIT slice {aid} is ADVISORY with commits present "
                "— proceeding per --accept-advisory (#1357)",
            ),
        )

    if status == "done":
        # #1535: `done` is TERMINAL (drive_state.TERMINAL_STATUSES) exactly
        # like `advisory` — it will never transition to `merged` on its own
        # if nothing was ever pushed (a #1534-style false "done", a reap, or
        # a worker that forgot to push, the recurring shape). Re-polling
        # can't help a terminal status, so waiting to `--deadline` with no
        # diagnosis is the #1526 merge-gate defect reborn here. Mirror the
        # `advisory` branch's probe exactly.
        branch = state.acceptance_author_branch
        probe = replace(state, work_branch=branch) if branch else state
        commits = verifier.branch_has_commits(probe) if branch else False
        if commits is None:
            # #2426: same distinction as the ADVISORY branch above — a
            # verification failure is not proof of an empty branch. This is
            # exactly the claude-coordinator#2286 incident: a real pushed
            # commit sat on the remote while a transient `git fetch` failure
            # made this arm declare the branch commit-less and terminal.
            return _wait(
                label=(
                    f"ACCEPTANCE: JIT slice {aid} branch {branch!r} — could "
                    "not verify commits (git fetch failed), retrying"
                )
            )
        if not commits:
            # #2061 (coord-portal#13): a re-dispatched author that lands in
            # a world where the slice is ALREADY on the default branch
            # correctly does nothing — DONE with zero commits — and that is
            # not a failure to re-diagnose, it's the terminal case this
            # whole gate exists to detect. Check the manifest before dying.
            if _slice_already_landed():
                return None
            # #2334: the third copy of the `_decide_advisory` dead end — see
            # the ADVISORY branch above's own #2334 comment. Same bounded
            # retry, same shared `counters.acceptance_author_retries`
            # budget: a work session that hits BOTH an empty-branch ADVISORY
            # and an empty-branch DONE for the same slice (a retried author
            # flapping between the two) still spends from one pool, not two.
            branch_display = repr(branch) if branch else "(none)"
            budget = opts.max_work_retries
            if counters.acceptance_author_retries >= budget:
                return _die(
                    f"acceptance author {aid} exited DONE, but its branch "
                    f"{branch_display} carries no commits, "
                    f"{counters.acceptance_author_retries} time(s) in a row "
                    f"(budget {budget}, #2334) — nothing was authored, so "
                    "there is no slice to land, and retrying has not "
                    "produced a different outcome.\n"
                    f"   inspect: coord log {aid} --machine "
                    f"{state.acceptance_author_machine or machine}\n"
                    "   this needs an operator decision: re-author by hand "
                    f"(coord acceptance author {state.repo} "
                    f"{oracle.tracking_issue} --issue {state.issue}), or "
                    "re-run coord drive with --no-acceptance to skip JIT "
                    "authoring."
                )
            counters.acceptance_author_retries += 1
            return _dispatch_fresh_acceptance_author(
                state, oracle, gate_checker,
                label_suffix=(
                    f" (attempt {counters.acceptance_author_retries}/{budget}"
                    ", #2334 retry after zero-commit DONE)"
                ),
            )
        # Authoring finished and the branch carries commits: hand over to the
        # landing driver, which observes the daemon-driven Test/Review stages
        # and performs the one step the daemon will not (#2079 — the merge).
        return _decide_acceptance_landing(state, oracle, opts, counters, machine)

    # "" / running: still authoring — nothing to drive yet.
    return _wait(
        label=(
            f"ACCEPTANCE: JIT slice {aid} status={status or '(none)'} — authoring"
        )
    )


def _decide_acceptance_landing(
    state: IssueState,
    oracle: OracleDecision,
    opts: DriveOptions,
    counters: DriveCounters,
    machine: str,
) -> Action:
    """Land the authored JIT acceptance slice (#2079).

    Reached only once the slice's own ``test-author`` row is terminal WITH
    commits on its branch — i.e. there is a real PR to land. From here the
    slice walks the identical Test → Review → Merge path a work row does, and
    this function takes the identical posture :func:`decide` takes for the
    work row: **observe the stages the daemon dispatches, perform the merge
    itself.**

    The split is not a style choice, it is where the daemon's tick actually
    stops. ``dispatch_pending_smoke``, ``dispatch_pending_reviews`` and
    ``merge_queue.enqueue_approved_work`` all run unconditionally on
    ``serve_app._passive_tick``; ``_auto_drain_tick`` — the step that turns a
    READY queue entry into a merged PR — runs only when ``merge.auto_drain``
    is on, and it is off. So waiting for the first three is waiting for
    something that will happen, and waiting for the fourth is the #1526
    defect: an unbounded wait for an event that cannot occur.

    Two shapes get an immediate, actionable exit instead of a wait, because
    for each of them the corrective action belongs to a loop that will never
    run for this row:

    * a FAILED slice test — the work row's equivalent dispatches
      ``coord fix``, but nothing dispatches one for a ``test-author`` row
      whose Test stage failed;
    * ``--no-merge`` — the slice merge is a hard prerequisite for
      dispatching any work at all (#1138), so with merging switched off the
      run cannot progress, and saying so beats idling to the deadline.

    A third shape — a ``request-changes`` slice review — used to be a fourth
    immediate exit for the identical reason (``auto_loop`` accepts a
    ``test-author`` fix via ``FIX_DISPATCH_TYPES``, but the daemon drain
    deliberately excludes fix dispatch — #476/#477, and #1692's own
    analysis — so for the work row it is THIS driver that runs
    ``coord fix``, and there was no such arm for the slice). #2425: it no
    longer is. Every retry of this lane re-dispatched a fresh author on the
    SAME branch name instead of fixing the one review blocking it
    (claude-coordinator#2286 — 11 re-authoring attempts over ~11h with the
    review's findings never once addressed), because re-authoring was the
    only path this lane had. Below, ``request-changes`` now gets the same
    bounded ``coord fix <acceptance_review_aid>`` arm :func:`decide` already
    runs for the work row's own review (the ``REVIEW: request-changes → fix
    round`` arm), spending :attr:`DriveCounters.acceptance`'s OWN
    ``fix_rounds`` — not the work row's — for the same #2079 reason the
    slice's merge attempts get their own budget: a struggling slice review
    must not silently leave the issue's real fix budget at zero.

    The merge itself reuses :func:`_decide_merge` verbatim against a shadow
    :class:`~coord.drive_state.IssueState` whose "work row" IS the slice —
    that is what carries #1891 (CI not reported → wait, don't retry), #1892
    (CI infra failure → wait), #1505 (a status no retry can fix →
    escalate), #1526 (driver/gate divergence) and #2078 (quote the real
    ``coord merge --only`` diagnostic) into the slice lane without a second
    implementation of any of them.
    """
    aid = state.acceptance_author_aid
    merge_status = (state.acceptance_merge_status or "").upper()

    # Already landed on GitHub; the board row just hasn't been reconciled to
    # `status='merged'` yet. Checked FIRST: MERGED is not in
    # `_RETRYABLE_MERGE_STATUSES`, so handing it to `_decide_merge` would
    # escalate a success.
    if merge_status == "MERGED":
        return _wait(
            label=(
                f"ACCEPTANCE: JIT slice {aid} PR is MERGED — waiting for the "
                "board row to reconcile to status='merged'"
            )
        )

    test_state = state.acceptance_author_test_state
    if test_state == "failed":
        return _die(
            f"the JIT acceptance slice {aid} FAILED its Test stage — nothing "
            "will fix it on its own (the review→fix loop that covers a work "
            "row is not dispatched for a test-author row).\n"
            f"   inspect: coord log {aid} --machine "
            f"{state.acceptance_author_machine or machine}\n"
            f"   Fix it: coord fix {aid}\n"
            "   or re-run coord drive with --no-acceptance to skip JIT "
            "authoring."
        )

    if state.acceptance_review_verdict == "request-changes":
        # #2425: the slice's own twin of `decide()`'s `REVIEW: request-
        # changes → fix round` arm — see the docstring above for why this
        # used to be a bare `_die()`. `coord fix` already accepts a
        # `type="test-author"` review id (#1622/#1692:
        # `auto_loop.FIX_DISPATCH_TYPES` includes `SEALED_PATH_AUTHOR_TYPES`,
        # which is where "test-author" lives), so this is the SAME command
        # the work-row arm dispatches, not a second implementation of it.
        slice_counters = counters.slice_budget()
        if not state.auto_loop:
            return _die(
                "the JIT acceptance slice's review requested changes, but "
                "pipeline.auto_loop is OFF — the review→fix path is "
                "switched off in coordinator.yml, so no fix can be "
                "dispatched.\n"
                f"   Findings: coord log {state.acceptance_review_aid or aid}\n"
                "   Continue by hand: coord assign --interactive --fix-of "
                f"{state.acceptance_review_aid or aid}"
            )
        # Belt-and-braces, mirroring the work-row arm: `acceptance_review_
        # verdict` and `acceptance_review_aid` are read off the SAME board
        # row (drive_state.project), so a verdict without an id is
        # impossible today. Refuse to guess rather than dispatch `coord fix`
        # against the wrong assignment.
        if not state.acceptance_review_aid:
            return _die(
                "the JIT acceptance slice's review verdict is "
                "'request-changes' but no review assignment id is on the "
                "board — refusing to guess which review to fix. Inspect: "
                f"coord gates {state.repo} {state.issue}"
            )
        # Same de-duplication latch as `review_fix_dispatched_for` on the
        # work row's own arm, just on the slice's own counters: `coord fix`
        # returns as soon as the fix worker is dispatched, and the board
        # this driver polls needs a beat to show the new row.
        if slice_counters.review_fix_dispatched_for == state.acceptance_review_aid:
            return _wait(
                label=(
                    "ACCEPTANCE: fix already dispatched for "
                    f"{state.acceptance_review_aid} — waiting for the fix "
                    "row to appear on the board"
                )
            )
        if slice_counters.fix_rounds >= opts.max_fix_rounds:
            return _die(
                f"the JIT acceptance slice {aid} was reviewed REQUEST-"
                f"CHANGES (review {state.acceptance_review_aid}) after "
                f"{slice_counters.fix_rounds} fix round(s) this drive spent "
                "landing the slice — stopping.\n"
                f"   Findings: coord log {state.acceptance_review_aid}\n"
                f"   Continue by hand: coord fix {state.acceptance_review_aid}\n"
                "   or re-run coord drive with --no-acceptance to skip JIT "
                "authoring."
            )
        slice_counters.fix_rounds += 1
        slice_counters.review_fix_dispatched_for = state.acceptance_review_aid
        return Action(
            kind=RUN,
            label=(
                "ACCEPTANCE: review request-changes → fix round "
                f"{slice_counters.fix_rounds}/{opts.max_fix_rounds} "
                f"(coord fix {state.acceptance_review_aid})"
            ),
            command=("fix", state.acceptance_review_aid),
            error_message=(
                f"coord fix {state.acceptance_review_aid} failed to "
                "dispatch a fix for the JIT acceptance slice's review.\n"
                "   Its refusals are all guards doing their job: auto_loop "
                "disabled, no structured\n"
                "   findings, approve-with-nits (#476), max_review_"
                "iterations, or the #522\n"
                "   terminal-work guard — the message above names which.\n"
                f"   Check: coord log {state.acceptance_review_aid}   /   "
                "continue by hand: coord assign --interactive --fix-of "
                f"{state.acceptance_review_aid}"
            ),
        )

    if not opts.do_merge:
        pr = state.acceptance_merge_pr_url or "(no PR recorded yet)"
        return _die(
            f"the JIT acceptance slice {aid} is authored but NOT landed, and "
            "--no-merge is set.\n"
            f"   Its PR: {pr}\n"
            "   #1138 refuses to dispatch work for this issue until the slice "
            "is merged, so this run cannot progress.\n"
            f"   Land it by hand: coord merge --only {aid} --method "
            f"{opts.merge_method}\n"
            "   or re-run coord drive without --no-merge (or with "
            "--no-acceptance to skip JIT authoring)."
        )

    # The shadow state: the slice IS the work row. `issue` becomes the
    # milestone's TRACKING issue because that is what the slice's own board
    # row and merge-queue entry are keyed on — so every `coord diagnose
    # <repo> <issue>` / `coord escalate record <repo> <issue>` command
    # `_decide_merge` and `_escalate_merge` compose points at the row a human
    # would actually have to fix.
    shadow = replace(
        state,
        issue=oracle.tracking_issue or state.issue,
        work_aid=aid,
        work_branch=state.acceptance_author_branch,
        work_machine=state.acceptance_author_machine,
        work_test_state=test_state,
        work_test_reason="",
        review_aid=state.acceptance_review_aid,
        review_verdict=state.acceptance_review_verdict,
        merge_status=state.acceptance_merge_status,
        merge_reason=state.acceptance_merge_reason,
        merge_aid=state.acceptance_merge_aid,
        merge_pr_url=state.acceptance_merge_pr_url,
    )
    action = _decide_merge(shadow, opts, counters.slice_budget())
    return replace(
        action,
        label=_acceptance_label(action.label),
        message=_acceptance_message(action.message, state),
        merge_scope="acceptance",
    )


def _acceptance_label(label: str) -> str:
    """Re-badge a :func:`_decide_merge` label as a slice-lane one (#2079).

    Without this the pane prints ``MERGE: attempt 1/3`` for the SLICE's merge
    while the issue's own work row does not exist yet — the single most
    confusing line this driver could emit.
    """
    if not label:
        return ""
    return "ACCEPTANCE/" + label


def _acceptance_message(message: str, state: IssueState) -> str:
    """The same re-badging for an EXIT action's message (#2079).

    The exit message is what reaches the issue comment
    (``Driver._post_escalation_comment``) and the drive-queue's stop reason —
    i.e. the only two places a human reads it after the tmux pane is gone. A
    bare "merge attempted 3 times without landing" there points them at the
    issue's own PR, which does not exist yet.
    """
    if not message:
        return ""
    return (
        f"the JIT acceptance slice for #{state.issue} could not be landed "
        f"(slice {state.acceptance_author_aid}, branch "
        f"{state.acceptance_author_branch or '(none)'}) — no work can be "
        "dispatched for this issue until it merges (#1138/#2079):\n"
        f"{message}"
    )


# ── merge verification ───────────────────────────────────────────────────────


class MergeVerifier(Protocol):
    """The git/GitHub questions the state machine cannot answer itself."""

    def branch_has_commits(self, state: IssueState) -> bool | None: ...

    def verify_merged(self, state: IssueState) -> bool: ...

    def branch_head_sha(self, state: IssueState) -> str | None: ...


def _remote_matches_repo(remote_url: str, repo_github: str) -> bool:
    """Loosely compare a ``git remote get-url origin`` URL against the
    ``owner/repo`` GitHub identifier it should point at (#2437).

    Tolerant of the URL shapes git actually produces: ``https://github.com/
    owner/repo(.git)``, ``git@github.com:owner/repo(.git)``, and
    ``ssh://git@github.com/owner/repo(.git)``. Anything else (a bare path, a
    non-GitHub host) simply won't match — which is the correct, fail-closed
    outcome: this is a sanity check that the checkout points at the expected
    project, not a general-purpose git URL parser.
    """
    url = remote_url.strip()
    if url.endswith(".git"):
        url = url[:-4]
    if "://" in url:
        _scheme, _, rest = url.partition("://")
        _host, _, path = rest.partition("/")
        url = path
    elif "@" in url and ":" in url:
        _user_host, _, path = url.partition(":")
        url = path
    return url.strip("/").lower() == repo_github.strip("/").lower()


@dataclass
class GitMergeVerifier:
    """Real implementation: ``git`` for commits, ``gh`` for merge state.

    ``repo_path`` is the local checkout used for fetches; defaults to
    ``~/src/<repo>``.
    """

    repo_path: str = ""
    warn: Callable[[str], None] = lambda msg: None
    # #2437: memoizes which (base, reason) pairs have already been warned
    # about, so a checkout that stays broken across many drive-loop polls
    # gets exactly one loud warning instead of one per poll. Keyed on the
    # reason text too, so a checkout that later fails a *different* check
    # (e.g. it grows a valid `.git` but the wrong `origin`) still warns once
    # for the new problem.
    _warned: set[tuple[str, str]] = field(default_factory=set, init=False, repr=False)

    def _base(self, state: IssueState) -> Path | None:
        base = Path(self.repo_path).expanduser() if self.repo_path else (
            Path.home() / "src" / state.repo
        )
        if not (base / ".git").exists():
            return None
        reason = self._unusable_reason(base, state)
        if reason is None:
            return base
        key = (str(base), reason)
        if key not in self._warned:
            self._warned.add(key)
            self.warn(
                f"local checkout {base} cannot be used for merge "
                f"verification of {state.repo} — {reason} (#2437). Merge "
                "verification will keep returning \"could not verify\" "
                "(never a false empty-branch) until this is fixed: replace "
                "it with a real clone of the repo, or point `coord drive` "
                "at one via --repo-path."
            )
        return None

    def _unusable_reason(self, base: Path, state: IssueState) -> str | None:
        """Why *base* can't be trusted for merge verification, or ``None`` if
        it can.

        A ``.git`` directory existing is necessary but not sufficient
        (claude-coordinator#2437): an interrupted/stub `git init` leaves a
        `.git` directory with no `HEAD`/`objects`/`refs`, and a checkout of
        the wrong project entirely leaves a perfectly valid `.git` that
        still can't answer questions about *this* repo. Both must be caught
        here rather than surfacing as an unexplained, indefinitely-retried
        ``None`` from :meth:`branch_has_commits`.
        """
        if self._git(base, "rev-parse", "--is-inside-work-tree").returncode != 0:
            return "not a usable git working tree (`git rev-parse --is-inside-work-tree` failed)"
        remote = self._git(base, "remote", "get-url", "origin")
        url = (remote.stdout or "").strip()
        if remote.returncode != 0 or not url:
            return "has no 'origin' remote configured"
        if state.repo_github and not _remote_matches_repo(url, state.repo_github):
            return (
                f"'origin' remote ({url!r}) does not match the expected "
                f"repo {state.repo_github!r}"
            )
        return None

    @staticmethod
    def _git(base: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(base), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

    def branch_has_commits(self, state: IssueState) -> bool | None:
        """``True``/``False`` when *branch* was actually checked against the
        default branch; ``None`` when the check could not be completed.

        ``False`` means VERIFIED empty: the fetches and the ``rev-list``
        count all succeeded, and the count was exactly 0. ``None`` means no
        such evidence exists — a transient ``git fetch`` failure (network
        blip, GitHub hiccup, SSH auth glitch — see ``is_ci_unreadable_reason``
        for the CI-side equivalent of this exact distinction), a missing
        local checkout, or an unparsable ``rev-list`` count. Before #2426
        every one of those collapsed into ``False``, indistinguishable from a
        genuinely empty branch — and callers treated ``False`` as proof of a
        TERMINAL dead end, misdiagnosing a fetch hiccup as "nothing was ever
        authored" (claude-coordinator#2286: a real, pushed commit sat on the
        remote while `coord drive` declared the branch commit-less and burned
        11 retry cycles re-authoring from scratch). ``None`` must never be
        treated as "no commits" — callers wait and let the next poll retry.

        Used to tell a REAL zero-commit advisory apart from the #1357 false
        positive, where the agent downgrades a good DONE over an artifact glob
        that matched nothing.
        """
        branch = state.work_branch
        if not branch:
            return False
        base = self._base(state)
        if base is None:
            return None
        target = state.repo_default_branch or "main"
        if self._git(base, "fetch", "--quiet", "origin", target).returncode != 0:
            return None
        if self._git(base, "fetch", "--quiet", "origin", branch).returncode != 0:
            return None
        proc = self._git(base, "rev-list", "--count", f"origin/{target}..FETCH_HEAD")
        if proc.returncode != 0:
            return None
        try:
            return int((proc.stdout or "0").strip() or 0) > 0
        except ValueError:
            return None

    def verify_merged(self, state: IssueState) -> bool:
        """Confirm the branch actually landed on the target.

        NOTE: ``merge-base --is-ancestor`` is the WRONG test here.  ``coord
        merge`` defaults to ``--method rebase`` (and supports squash), both of
        which rewrite the commits — so a fully-merged branch's tip SHA is never
        an ancestor of the target.  Verified against #1344: merged via PR
        #1355, two commits on main, and ``--is-ancestor`` still says no.
        """
        branch = state.work_branch
        target = state.repo_default_branch or "main"
        if not branch:
            return False

        # Primary: ask GitHub.  Authoritative for every merge method, and still
        # correct after the merged branch has been deleted from the remote.
        # Routed through the github_ops seam (#1483) rather than shelling out
        # to `gh` directly here — no `shutil.which` probe, so behaviour never
        # silently varies with whether `gh` happens to be on this host's PATH.
        if state.repo_github:
            from coord import github_ops  # noqa: PLC0415

            pr_state = github_ops.get_pr_state_for_branch(state.repo_github, branch) or ""
            if pr_state == "MERGED":
                return True
            if pr_state:
                self.warn(f"PR for {branch} is {pr_state}, not MERGED")
                return False

        # Fallback: patch-equivalence.  Every commit of a landed branch has an
        # equivalent upstream, which is exactly what `git cherry` marks with
        # '-'; a '+' means that commit is genuinely not on the target yet.
        base = self._base(state)
        if base is None:
            return False
        vref = f"refs/remotes/coord-verify/{branch}"
        if self._git(base, "fetch", "--quiet", "origin", target).returncode != 0:
            return False
        fetched = self._git(
            base, "fetch", "--quiet", "origin", f"refs/heads/{branch}:{vref}"
        )
        if fetched.returncode != 0:
            return False
        try:
            proc = self._git(base, "cherry", f"origin/{target}", f"coord-verify/{branch}")
            if proc.returncode != 0:
                return False
            unmerged = [
                line for line in (proc.stdout or "").splitlines() if line.startswith("+")
            ]
            return not unmerged
        finally:
            self._git(base, "update-ref", "-d", vref)

    def branch_head_sha(self, state: IssueState) -> str | None:
        """The exact commit `coord acceptance record --sha` (#2199) must be
        pointed at — the trust gate re-runs the sealed suite against a
        precise SHA, never a branch name that could move under a slow
        drive loop. GitHub API, not a local checkout: same reasoning as
        :meth:`verify_merged`'s primary path — authoritative for every
        machine this driver might run on, no ``~/src/<repo>`` clone
        required. Returns ``None`` (never raises) when the branch is gone
        or GitHub is unreachable; callers treat that as "try again next
        poll", never as a verdict.
        """
        if not state.work_branch or not state.repo_github:
            return None
        from coord import github_ops  # noqa: PLC0415

        return github_ops.get_branch_sha(state.repo_github, state.work_branch)


# ── preflight (pure) ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Preflight:
    """The resolved machine plus anything worth warning about before looping."""

    machine: str
    warnings: tuple[str, ...] = ()


def preflight(
    state: IssueState,
    opts: DriveOptions,
    config: Any = None,
    *,
    usage_limits: PlanLimits | None = None,
) -> Preflight:
    """Resolve the machine and refuse the runs that can never win.

    Raises :class:`DriveError` for a configuration problem or for interactive
    work with no review (see below).

    *usage_limits* (#1466) is the ALREADY-PROBED Max-plan 5h/weekly usage
    snapshot — this function stays pure and never shells out itself, mirroring
    the *verifier*/*gate_checker* injection pattern used elsewhere in this
    module. ``None`` (every pre-#1466 caller, and any caller that skips the
    probe) is treated exactly like an unavailable probe: the gate silently
    lets the run proceed. *config* is likewise optional — ``None`` skips the
    usage gate entirely (no ``usage_gate`` section to consult), which is what
    every pre-#1466 test in this file's suite still passes.

    #1906: ``state.picked_machine`` is now itself provider-capability-aware
    (:func:`coord.drive_state.pick_machine_choice`, resolved once in
    :func:`project`) whenever an auto-pick happens — this function no
    longer needs to (and does not) resolve the provider itself; it only
    reports the *result*, including the distinct "no capable machine" vs.
    "no host at all" failure state on ``state``.
    """
    machine = opts.machine or state.picked_machine
    if not machine:
        # #1906: an explicit `--machine` always wins (never reaches here —
        # `opts.machine` short-circuits `or` above) and hits #1711's own
        # refusal downstream in `coord.dispatch.dispatch()` instead, exactly
        # like today. This branch is only the AUTO-pick failing, and it now
        # has two distinct causes that must not collapse into one message:
        # no unpaused machine hosts the repo at all, vs. at least one does
        # but none advertise the resolved provider (`state.picked_machine`'s
        # own `coord.drive_state.pick_machine_choice` already did the
        # capability filtering — see `IssueState.picked_machine_no_capable`).
        if state.picked_machine_no_capable:
            raise DriveError(
                f"no unpaused machine advertises provider "
                f"{state.picked_machine_provider!r} for {state.repo} — pass "
                "--machine, or add the capability to a machine's "
                "coordinator.yml machines[].capabilities",
                EXIT_USAGE,
            )
        raise DriveError(
            f"no unpaused machine hosts {state.repo} — pass --machine",
            EXIT_USAGE,
        )

    warnings: list[str] = []

    if config is not None:
        gate_cfg = config.usage_gate
        limits = usage_limits if usage_limits is not None else PlanLimits(status="unknown")
        gate_result = evaluate_usage_gate(limits, gate_cfg)
        if gate_result.action == "block":
            raise DriveError(
                f"{gate_result.message} (usage_gate.mode: block) — refusing to "
                "dispatch. Wait for the window to reset, or lower urgency by "
                "raising the threshold / setting usage_gate.mode: warn in "
                "coordinator.yml.",
                EXIT_USAGE,
            )
        if gate_result.action == "warn":
            warnings.append(f"{gate_result.message} (usage_gate.mode: warn — proceeding anyway)")

    if not state.auto_loop:
        warnings.append(
            "pipeline.auto_loop is OFF — the review→fix path is switched off."
        )
        warnings.append(
            "A request-changes verdict will be REPORTED and this run will stop "
            "(#1692): `coord fix` refuses while auto_loop is off, so there is "
            "no fix to dispatch."
        )

    # INTERACTIVE WORK NEVER GETS AN AUTOMATIC REVIEW.
    #
    # `dispatch_pending_reviews` carries `and c.provider_name != "claude-pty"`
    # (#555): a metered headless review must never silently follow a
    # human-attended session.  So for work done interactively, the review is
    # not "late" — it is never coming, and waiting for it is an infinite stall.
    #
    # Checked HERE, at preflight, rather than at the review gate: otherwise a
    # run burns the full test suite (~6 min) before parking on a wait it can
    # never win.  That is exactly what happened driving #1357 — test gate
    # passed at 4642/4642, then 90 minutes of nothing.
    if state.work_aid and state.work_provider == "claude-pty" and not state.review_aid:
        if opts.force_review:
            warnings.append(
                f"work {state.work_aid} is INTERACTIVE (claude-pty) — no "
                "automatic review (#555)."
            )
            warnings.append(
                "--force-review set: this run will request the review explicitly."
            )
        else:
            raise DriveError(
                f"work {state.work_aid} was completed INTERACTIVELY "
                "(provider=claude-pty).\n"
                "   coord's #555 guard permanently excludes interactive work from "
                "automatic\n"
                "   review dispatch, so waiting for one would stall forever.\n\n"
                "   Either drive it unattended:      re-run with --force-review\n"
                "   or review it human-attended:     coord assign --interactive "
                f"--review-of {state.work_aid}",
                EXIT_USAGE,
            )

    return Preflight(machine=machine, warnings=tuple(warnings))


# ── the state machine (pure) ─────────────────────────────────────────────────


def _escalate_dead_end(state: IssueState, dead_end: DeadEnd) -> Action:
    """Build the EXIT action for a terminal-and-unactionable row (#2019).

    Structurally identical to :func:`_escalate_merge` — same ``coord escalate
    record`` argv, same "this function stays pure, the write happens in
    :meth:`Driver._loop`'s exit handling" split — because the operator-facing
    outcome is the same: a board-visible record naming the blocker and the
    command that clears it, instead of a counter ticking against an event that
    can never happen.

    The exit code is what differs.  :data:`EXIT_ESCALATED` means "a human
    decision is waiting"; :data:`EXIT_DEAD_END` additionally means "and no
    relaunch of this drive can change it", which is the fact
    ``coord/drive_queue.py``'s tick needs to block the entry WITHOUT spending
    an attempt (the #1844 posture, applied to a second cause).
    """
    gates_summary = " | ".join(f"{k}={v}" for k, v in dead_end.gates)

    command: list[str] = [
        "escalate", "record", state.repo, str(state.issue),
        "--stage", dead_end.stage,
        "--reason", f"{dead_end.kind}: {dead_end.reason}",
    ]
    for key, value in dead_end.gates:
        command += ["--gate", f"{key}={value}"]
    command += ["--command", dead_end.recovery]
    if dead_end.assignment_id:
        command += ["--assignment", dead_end.assignment_id]

    return Action(
        kind=EXIT,
        exit_code=EXIT_DEAD_END,
        message=(
            f"DEAD END [{dead_end.kind}] — this row is terminal and "
            "unactionable; exiting instead of polling (#2019).\n"
            f"   {dead_end.reason}\n"
            f"   gates: {gates_summary}\n"
            f"   Recover: {dead_end.recovery}\n"
            f"   Recorded on the board — see: coord escalate list --repo "
            f"{state.repo}"
        ),
        command=tuple(command),
        error_message=(
            "failed to record the dead-end escalation on the board (exiting "
            f"anyway — resolve by hand: {dead_end.recovery})"
        ),
    )


def decide(
    state: IssueState,
    opts: DriveOptions,
    counters: DriveCounters,
    verifier: MergeVerifier,
    *,
    machine: str = "",
    oracle: OracleDecision | None = None,
    gate_checker: AcceptanceGateChecker | None = None,
) -> Action:
    """One step of the state machine: given the board, what next?

    Pure apart from the injected *verifier* (git/GitHub) and the bounded
    counters it increments.  Every branch here was a bash ``case`` arm; the
    ordering is identical, and — critically — **no terminal status falls
    through to a bare wait**.  An ``advisory`` work row doing exactly that was
    a silent 240-minute spin (fixed in PR #1386, and now unit-tested).

    *oracle* (#1453) is resolved ONCE per run by :func:`resolve_oracle_decision`
    and threaded through unchanged on every call — ``None`` (the default,
    every pre-#1453 caller) behaves exactly as before: no JIT slice, straight
    to ``coord assign``. *gate_checker* is only consulted when *oracle* is
    active (to resolve a routed repo's ``--for-path``, #1453 review finding
    1) — unused, like *oracle*, on every pre-#1453 call site.
    """
    machine = machine or opts.machine or state.picked_machine

    # ---- terminal: merged ---------------------------------------------------
    if state.work_status == "merged" or state.merge_status == "MERGED":
        target = state.repo_default_branch or "main"
        if state.work_branch and verifier.verify_merged(state):
            return _succeed(
                f"✓ MERGED — {state.work_branch} has landed on {target}\n"
                f"   {MERGE_LANDED_MARKER}"
            )
        base = opts.repo_path or f"~/src/{state.repo}"
        return _die(
            f"board says merged but {state.work_branch} has NOT landed on {target}\n"
            f"   verify by hand: git -C {base} log --oneline origin/{target}"
        )

    # ---- something is running: just wait -----------------------------------
    if state.active_count > 0:
        return _wait()

    # ---- no work yet: plan and/or dispatch ---------------------------------
    if not state.work_aid:
        return _dispatch_work_stage(
            state, opts, counters, machine, oracle, gate_checker, verifier
        )

    # ---- work died from hitting the account's usage limit: wait -----------
    #
    # #1461: a usage-limit kill is NOT a defect and NOT the #1357 zero-commit
    # advisory signature — it is the ONE terminal state known safe to
    # re-dispatch unchanged, once the reset time passes. Falling into the
    # bounded-retry branch below (or `coord fix`'s model escalation, on the
    # advisory side) would just re-dispatch straight into the same exhausted
    # budget and fail again for no diagnostic reason — exactly the confusion
    # the issue is about. Detected via the `usage limit — resets ...` prefix
    # that `coord.worker_events.format_usage_limit_reason` stamps onto
    # `failure_reason` regardless of whether the agent's own reap landed on
    # FAILED or ADVISORY (#1461's own worked example hit both in one
    # session). Deliberately does NOT auto-retry here — retrying before the
    # reset only produces more of the same; a human (or a future reset-aware
    # auto-retry) re-runs `coord retry` once the window reopens.
    #
    # #1590: routed through `coord.failure_class` so this branch and the
    # sequencer's budget agree on what "environmental" means, and the surfaced
    # warning now names *when* the node could resume — the `reset_at_raw` the
    # detector has always parsed and nobody ever used.
    if state.work_status in ("failed", "advisory"):
        classification = classify_failure(
            failure_reason=state.work_failure_reason or None
        )
        if classification.is_usage_limit:
            resume = plan_usage_limit_resume(
                reset_at_raw=classification.reset_at_raw
            )
            when = (
                resume.resume_at.isoformat(timespec="minutes")
                if resume.from_reset_time
                else "unknown (reset time not parseable)"
            )
            return Action(
                kind=WAIT,
                label=(
                    f"WORK: {state.work_aid} killed by the usage limit — waiting "
                    "for the reset, not retrying"
                ),
                warnings=(
                    f"usage-limit kill detected on {state.work_aid}: "
                    f"{state.work_failure_reason} — waiting for the reset instead "
                    "of retrying (#1461)",
                    f"{classification.reason}; earliest resume {when} (#1590)",
                ),
            )

    # ---- work failed: bounded retry ----------------------------------------
    if state.work_status == "failed":
        # #1590 part 6: name the actual cause. "failed 3 retries in: <prose>"
        # sent the morning triage looking at the work even when the provider
        # was the problem; the class is now stated up front.
        classification = classify_failure(
            failure_reason=state.work_failure_reason or None
        )
        # #2360: an environmental failure (already established NOT a usage
        # limit — that branch returned above) gets a wider budget than a
        # genuine code defect, reusing the same classifier the usage-limit
        # check just above already trusts. A NON-environmental failure keeps
        # today's flat `opts.max_work_retries` budget completely unchanged —
        # this is not a general increase to retry counts, only a widening
        # scoped to `classification.is_environmental`.
        budget = (
            max(opts.max_work_retries, _ENVIRONMENTAL_WORK_RETRY_BUDGET)
            if classification.is_environmental
            else opts.max_work_retries
        )
        if counters.work_retries >= budget:
            return _die(
                f"work {state.work_aid} failed {counters.work_retries} retr(ies) in: "
                f"{state.work_failure_reason or 'no reason recorded'}\n"
                f"   cause: {classification.reason}\n"
                f"   inspect: coord log {state.work_aid} --machine "
                f"{state.work_machine or machine}"
            )
        attempt = counters.work_retries + 1
        if (
            classification.is_environmental
            and counters.work_environmental_backoff_attempt != attempt
        ):
            # Back off ONCE per attempt number before spending it (the same
            # `environmental_backoff_secs` exponential curve #1590/#2275
            # proved for the Test-stage worker-death path) — the latch means
            # the NEXT poll, once this sleep has actually elapsed, falls
            # through to the real `coord retry` below instead of returning
            # the same backoff WAIT forever against a board that has not
            # changed (nothing was dispatched during the wait).
            counters.work_environmental_backoff_attempt = attempt
            wait = environmental_backoff_secs(attempt)
            return Action(
                kind=WAIT,
                label=(
                    f"WORK: {state.work_aid} failed environmentally — "
                    f"backing off {int(wait)}s before retry {attempt}/{budget}"
                ),
                sleep_after=wait,
                warnings=(f"{classification.reason} (#2360)",),
            )
        counters.work_retries = attempt
        return Action(
            kind=RUN,
            label=(
                f"WORK: failed → coord retry {state.work_aid} "
                f"(attempt {counters.work_retries}/{budget})"
            ),
            command=("retry", state.work_aid),
            error_message=f"coord retry failed for {state.work_aid}",
        )

    # ---- work reached a terminal state that is not 'done' ------------------
    #
    # Every status here is TERMINAL (a non-terminal row would have been caught
    # by the active_count wait above), so none of them may fall through to a
    # bare wait — that spins silently until the deadline instead of reporting
    # anything.
    warnings: tuple[str, ...] = ()
    if state.work_status == "done":
        pass
    elif state.work_status == "advisory":
        advisory = _decide_advisory(state, opts, counters, machine, verifier)
        # #2416: `_decide_advisory` returns a RUN action (a bounded `coord
        # retry` dispatch) for the zero-commit case, not only an EXIT —
        # `is_exit` alone would let a RUN action fall through to the
        # Test/Review/Merge logic below as though the row were validated
        # work. #2426: it returns an explicit WAIT for the "could not verify
        # — fetch failed" case, which must ALSO be this poll's whole answer,
        # not folded into the fall-through below (that fall-through means
        # "commits confirmed present, proceed as normal work", which a
        # verification failure has not established either way). Only `None`
        # — "proceeding under --accept-advisory", commits confirmed present
        # — falls through, carrying its warning.
        if advisory is not None:
            return advisory
        warnings = (
            "ADVISORY with commits present — proceeding per --accept-advisory (#1357)",
        )
    elif state.work_status == "refused_policy":
        # #2234: the worker exited cleanly, pushed 0 commits, and its own
        # final message cited a standing repo-rule prohibition (`coord.agent.
        # REFUSED_POLICY` — the #2195 shape: an issue whose entire
        # deliverable was a doc edit, which CLAUDE.md itself tells a worker
        # to refuse and stop rather than attempt). Unlike an ADVISORY 0-commit
        # exit, retrying changes nothing — the rule this worker cited is not
        # going anywhere — UNLESS the issue itself has since changed.
        #
        # #2871: a refusal is a verdict on the ASK the worker was shown, not
        # a standing veto on the issue NUMBER forever. `coord/claim.py`'s
        # `find_work_claim` never even sees this row (it's terminal, so it
        # lives in `board.completed`) — the reason a stale refusal blocks
        # every later drive is entirely HERE: `work_aid`/`work_status` above
        # are the latest work-like row for this issue regardless of how old
        # or how differently-scoped it is, so a fresh `coord drive` launch
        # reads the fossil row as "this run's" state before ever reaching
        # `_dispatch_work_stage`. If the issue was retargeted (title
        # rewritten) after this row finished, `_refused_policy_is_stale`
        # detects it — the old refusal no longer describes what a fresh
        # dispatch would even be asked to do — and this bypasses the veto
        # instead of dying again on the SAME prose a fresh worker never
        # produced this run (CC#916).
        age_seconds = (
            time.time() - state.work_finished_at
            if state.work_finished_at is not None
            else None
        )
        age = _format_age(age_seconds) if age_seconds is not None else "unknown age"
        staleness = _refused_policy_staleness(state)
        if staleness.is_stale:
            bypass_summary = (
                f"pre-dispatch: bypassing stale refused_policy assignment "
                f"{state.work_aid} ({age} old) on issue {state.repo}#{state.issue} "
                f"— retargeted since (branch {state.work_branch!r} no longer "
                "matches the issue's current title); dispatching fresh work (#2871)"
            )
            dispatch = _dispatch_work_stage(
                state, opts, counters, machine, oracle, gate_checker, verifier
            )
            return replace(
                dispatch,
                audit_event=(
                    "refused_policy_stale",
                    bypass_summary,
                    {
                        "stale_assignment_id": state.work_aid,
                        "stale_branch": state.work_branch,
                        "age_seconds": age_seconds,
                    },
                ),
            )
        # Still genuinely blocking: `_die()` exactly like every other
        # terminal branch here, but the message now says explicitly that
        # THIS run refused pre-dispatch on an OLD row (naming it and its
        # age) rather than reading as a fresh worker refusing again — and
        # names the remedy that actually clears it (#2871: retargeting is
        # what makes the branch above fire on the next drive; `coord
        # drive-queue remove`+`add` alone does nothing, since the queue row
        # was never what was blocking). The message still embeds
        # `POLICY_REFUSAL_MARKER`: `coord/drive_queue.py`'s
        # `_reconcile_running` recognises that marker in this run's own
        # `drive_exited` audit summary and parks the queue entry
        # (`STATE_PARKED`) WITHOUT spending an attempt, rather than treating
        # this exit as a transient death — the same "cannot change on retry"
        # principle #1844 already applies pre-dispatch, applied here
        # post-dispatch.
        #
        # #2881: `staleness.uncertain` distinguishes "checked, and the title
        # really is unchanged" from "could not check at all" — the original
        # #2871 message collapsed both to the same confident "retarget the
        # issue, the next drive will see it" prose, which is actively wrong
        # advice in the uncertain case (the operator may have ALREADY
        # retargeted, and the driver simply couldn't tell).
        if staleness.uncertain:
            remedy = (
                "Needs the coordinator: do the work directly. Retargeting "
                "(rewriting the issue's title) SHOULD make the next `coord "
                f"drive` dispatch fresh work automatically (#2871), but "
                f"{staleness.detail} — so THIS run could not confirm that "
                "either way. If a retarget was already tried and this is "
                "still blocking, that's worth investigating on its own "
                "(#2881) rather than assuming the retarget silently failed."
            )
        else:
            remedy = (
                "Needs the coordinator: do the work directly, OR retarget "
                "the issue (rewrite its title so the deliverable matches "
                "what should actually be dispatched) — the next `coord "
                "drive` detects the retarget and dispatches fresh work "
                f"automatically (#2871), so `coord drive-queue remove "
                f"{state.repo} {state.issue}` + `add` then genuinely clears "
                "this once the issue is retargeted."
            )
        return _die(
            f"pre-dispatch refusal on assignment {state.work_aid} "
            f"(refused_policy, {age} old) — work refused on a standing "
            "repo-rule prohibition rather than doing the dispatched work; "
            f"the worker did the CORRECT thing (#2234). {remedy}\n"
            f"   inspect: coord log {state.work_aid} --machine "
            f"{state.work_machine or machine}\n"
            f"   {POLICY_REFUSAL_MARKER}"
        )
    elif state.work_status == "cancelled":
        return _die(
            f"work {state.work_aid} was cancelled — re-dispatch with: "
            f"coord assign {machine} {state.repo} {state.issue} --force"
        )
    else:
        return _die(
            f"unexpected terminal work status '{state.work_status}' for "
            f"{state.work_aid} —\n"
            f"   refusing to guess. Inspect: coord log {state.work_aid} --machine "
            f"{state.work_machine or machine}"
        )

    # ---- analysis deliverable: done + 0 commits is the SUCCESS shape (#2188)
    #
    # An issue labelled `deliverable:analysis` inverts the read on a 0-commit
    # exit: the deliverable is the worker's own final message (already
    # posted to the issue by the coordinator — see coord.notify.
    # post_transition's EVENT_COMPLETION arm), not a diff. `coord.agent.
    # AgentServer._reap` only ever produces this shape on `status == "done"`
    # (never "advisory" — see `AgentAssignment.analysis_deliverable`), so
    # this only needs to guard the `done` arm above, and it runs BEFORE the
    # "no branch"/dead-end/Test/Review/Merge machinery below: none of that
    # applies when there is nothing to test, review, or merge. A labelled
    # issue whose worker DID push commits (the label describes the common
    # case, not a hard rule) falls through unchanged — `branch_has_commits`
    # is False only for the genuine 0-commit shape; `None` (#2426 — the
    # check could not be completed, e.g. a `git fetch` failure) is neither
    # "done" nor "push commits", so it waits below instead of guessing.
    if state.work_status == "done" and DELIVERABLE_ANALYSIS_LABEL in state.issue_labels:
        commits = verifier.branch_has_commits(state) if state.work_branch else False
        if commits is None:
            return _wait(
                label=(
                    f"ANALYSIS: {state.work_aid} — could not verify branch "
                    "commits (git fetch failed), retrying"
                )
            )
        if not commits:
            return _succeed(
                f"✓ ANALYSIS DELIVERABLE — {state.work_aid} completed with 0 "
                f"commits (issue labelled `{DELIVERABLE_ANALYSIS_LABEL}`); the "
                "worker's final message is the deliverable and was posted to "
                "the issue automatically — nothing to test, review, or merge."
            )

    # A 'done' row with no branch never pushed anything either.
    if not state.work_branch:
        return _die(
            f"work {state.work_aid} finished with no branch — nothing was pushed "
            "(0-commit advisory).\n"
            f"   inspect: coord log {state.work_aid} --machine "
            f"{state.work_machine or machine}"
        )

    # ---- the dead-end predicate (#2019) ------------------------------------
    #
    # Positioned HERE — after the merged/active/work-status arms above have
    # all had their say, before the Test and Review gates — on purpose:
    #
    #  * everything above it is either terminal-and-already-reported (merged,
    #    cancelled, an unexpected status) or genuinely actionable (a bounded
    #    work retry, a usage-limit wait, an advisory), so the predicate can
    #    never steal a live move from them;
    #  * everything below it is a gate that, on the shapes the predicate
    #    recognises, would return a bare `_wait()` and spin — which is the
    #    entire bug (#1956: 140 minutes of `no state change`, with `active=0`
    #    printed on every line).
    #
    # `detect_dead_end` itself refuses to fire while `active_count > 0`, so a
    # healthy long-running stage is structurally incapable of reaching this.
    # #2024: `--skip-test` is a live Test-stage move (`_decide_test` records
    # `skipped`), so the human-attended-Test shape must not escalate past it.
    dead_end = detect_dead_end(state, can_waive_test_gate=opts.skip_test)
    if dead_end is not None:
        return replace(
            _escalate_dead_end(state, dead_end), warnings=warnings
        )

    # #2199: the oracle loop's TRUST GATE — docs/ORACLE_LOOP.md Phase-1 step
    # 6, right here between the dead-end predicate and the Test gate,
    # because this is the first point `coord drive` has observed the work
    # assignment's branch head AFTER push. Before this, nothing ever called
    # `coord acceptance record`: an issue driven end-to-end by `coord drive`
    # completed with `acceptance_state = None` forever, so the sealed suite
    # was never re-run externally and `_maybe_clear_expected_red` could
    # never clear (quadraui#542).
    acceptance_gate = _decide_acceptance_gate(
        state, opts, counters, machine, oracle, verifier, gate_checker
    )
    if acceptance_gate is not None:
        return replace(
            acceptance_gate, warnings=warnings + acceptance_gate.warnings
        )

    test = _decide_test(state, opts, counters, machine)
    if test is not None:
        return replace(test, warnings=warnings + test.warnings)

    review = _decide_review(state, opts, counters, machine)
    if review is not None:
        return replace(review, warnings=warnings + review.warnings)

    if not opts.do_merge:
        return replace(
            _succeed(
                "✓ review approved — stopping here (--no-merge)\n"
                f"  merge with: coord merge --only {state.work_aid}"
            ),
            warnings=warnings,
        )

    merge = _decide_merge(state, opts, counters)
    return replace(merge, warnings=warnings + merge.warnings)


def _dispatch_work_stage(
    state: IssueState,
    opts: DriveOptions,
    counters: DriveCounters,
    machine: str,
    oracle: OracleDecision | None = None,
    gate_checker: AcceptanceGateChecker | None = None,
    verifier: MergeVerifier | None = None,
) -> Action:
    """No work row yet: run the optional plan stage, then dispatch the work.

    #1453: when *oracle* is active, the sealed JIT acceptance slice for this
    issue is authored — and driven through to a landed merge (#2079) — BEFORE
    either the plan or the direct-assign path below. Otherwise the #1138
    hard gate (``coord.dispatch.enforce_oracle_readiness``) would simply
    refuse the eventual ``coord assign``/``coord approve-plan`` once an
    oracle-opted-in milestone's issue reaches it, with this driver never
    having explained why.

    *counters* is threaded in for the slice's own merge budget (#2079 —
    ``DriveCounters.acceptance``); every other decision here is stateless.
    """
    if oracle is not None and oracle.active:
        assert gate_checker is not None and verifier is not None, (
            "oracle.active implies resolve_oracle_decision ran with a real "
            "gate_checker; decide()/Driver always thread one through"
        )
        gate = _decide_acceptance_author(
            state, oracle, opts, counters, machine, gate_checker, verifier
        )
        if gate is not None:
            return gate

    # #1499: durable provenance stamped on every assignment this driver
    # dispatches via `coord assign` — the piece that survives the driver
    # process exiting (see coord.models.Assignment.driven_by / Proposal.driven_by).
    driven_by = f"drive:{state.repo}#{state.issue}"

    if opts.do_plan:
        if not state.plan_aid:
            args = [
                "assign", "--plan-only", machine, state.repo, str(state.issue),
                "--driven-by", driven_by,
            ]
            if opts.model:
                args += ["--model", opts.model]
            return Action(
                kind=RUN,
                label=(
                    f"PLAN: coord assign --plan-only {machine} {state.repo} "
                    f"{state.issue}"
                ),
                command=tuple(args),
            )
        if state.plan_status == "done":
            return Action(
                kind=RUN,
                label=f"PLAN: approved → coord approve-plan {state.plan_aid}",
                command=("approve-plan", state.plan_aid),
            )
        if state.plan_status == "failed":
            return _die(
                f"plan assignment {state.plan_aid} failed — inspect: "
                f"coord log {state.plan_aid} --machine {machine}"
            )
        return _wait()

    args = ["assign", machine, state.repo, str(state.issue), "--driven-by", driven_by]
    if opts.model:
        args += ["--model", opts.model]
    if opts.briefing_file:
        args += ["--briefing-file", opts.briefing_file]
    return Action(
        kind=RUN,
        label=f"WORK: coord assign {machine} {state.repo} {state.issue}",
        command=tuple(args),
    )


def _decide_advisory(
    state: IssueState,
    opts: DriveOptions,
    counters: DriveCounters,
    machine: str,
    verifier: MergeVerifier,
) -> Action | None:
    """The #448 downgrade: the agent flagged a zero-commit / stash-miss exit.

    #1357 makes this a FALSE POSITIVE for every Python-only headless assignment
    in claude-coordinator — its only artifact glob is
    ``tui/target/debug/coord-tui``, which a Python diff never produces, so
    #1323's stash-miss check downgrades a perfectly good DONE.  Ask git which
    case this actually is rather than trusting the status.

    #2416: a GENUINE zero-commit advisory (no branch, or a branch verified to
    carry no commits) used to be an immediate, unconditional `_die()` — the
    ONE thing that can actually change this row, `coord retry <aid>`'s
    zero-commit-advisory path (#1606: `ahead == 0` → reassign a fresh
    worker), was never invoked automatically. That made every automatic
    retry — `drive-queue`'s own included — a no-op: a brand new `coord
    drive` process just re-observes the identical terminal row and dies
    again in seconds, spending a queue attempt+backoff cycle for nothing.
    Mirrors the `work_status == "failed"` branch above: a bounded number of
    `coord retry` dispatches (`opts.max_work_retries`, via `counters.
    advisory_retries`) before finally giving up, so a row whose correct
    resolution really is "dispatch something else" still reaches a terminal
    `_die()` naming `coord retry` for a human — never an unbounded loop.

    Returns ``None`` only for the "commits confirmed present, proceed under
    --accept-advisory" case — the caller (`decide`) reads ``None`` as "fall
    through to the normal Test/Review/Merge machinery", the same "nothing to
    report, proceed" convention `_decide_test`/`_decide_review`/
    `_decide_acceptance_gate` already use. Every other outcome, including the
    #2426 "could not verify" case below, returns a real ``Action`` that IS
    this poll's whole answer.
    """
    branch = state.work_branch
    commits = verifier.branch_has_commits(state) if branch else False
    if commits is None:
        # #2426: `git fetch` failing does not mean the branch is empty — it
        # means nothing was learned. Wait for the next poll to retry the
        # check itself; do NOT spend an `advisory_retries` budget attempt (or
        # worse, `_die()`) on a verification failure that says nothing about
        # what the branch actually contains.
        return _wait(
            label=(
                f"WORK: {state.work_aid} ADVISORY — could not verify branch "
                "commits (git fetch failed), retrying"
            )
        )
    if not commits:
        budget = opts.max_work_retries
        if counters.advisory_retries >= budget:
            return _die(
                f"work {state.work_aid} exited ADVISORY with no commits on its "
                f"branch {counters.advisory_retries} time(s) in a row (budget "
                f"{budget}, #2416) — nothing was pushed, so there is nothing to "
                "test, review, or merge, and retrying has not produced a "
                "different outcome.\n"
                f"   inspect: coord log {state.work_aid} --machine "
                f"{state.work_machine or machine}\n"
                f"   this needs an operator decision: `coord retry "
                f"{state.work_aid}` by hand once the underlying blocker is "
                "understood, or dispatch an independent follow-up issue if "
                "another attempt at this one is not the right fix."
            )
        counters.advisory_retries += 1
        return Action(
            kind=RUN,
            label=(
                f"WORK: {state.work_aid} exited ADVISORY with no commits → "
                f"coord retry {state.work_aid} (attempt "
                f"{counters.advisory_retries}/{budget}, #2416)"
            ),
            command=("retry", state.work_aid),
            error_message=f"coord retry failed for {state.work_aid}",
        )
    if not opts.accept_advisory:
        return _die(
            f"work {state.work_aid} is ADVISORY, but its branch carries real "
            "commits.\n"
            "   This is the #1357 signature: since v0.4.75 every Python-only "
            "headless\n"
            "   assignment in this repo is downgraded DONE→ADVISORY by an "
            "artifact glob\n"
            "   that a Python diff can never match.\n"
            "   Proceed anyway with --accept-advisory (and fix #1357 to stop "
            "needing it)."
        )
    # Commits confirmed present, --accept-advisory set: fall through to the
    # normal Test/Review/Merge machinery (the caller attaches the warning).
    return None


def _decide_acceptance_gate(
    state: IssueState,
    opts: DriveOptions,
    counters: DriveCounters,
    machine: str,
    oracle: OracleDecision | None,
    verifier: MergeVerifier,
    gate_checker: AcceptanceGateChecker | None,
) -> Action | None:
    """The #2199 oracle-loop TRUST GATE — ``coord acceptance record --repo
    R --issue N --sha <pushed sha>``, run by the COORDINATOR (this process,
    never inside the worker's own session) against the exact commit the
    work assignment pushed. docs/ORACLE_LOOP.md's Phase-1 step 6, and the
    reason it exists: a headless worker's in-session "green" claim
    (``coord acceptance run``, Phase-1 step 5) can lie; it cannot fake the
    coordinator re-running the sealed suite itself. ``None`` means "not
    applicable, or already resolved for this SHA — fall through to Test".

    Before #2199 NOTHING ever called this — an issue driven end-to-end by
    ``coord drive`` completed with ``acceptance_state = None`` forever, so
    ``coord.merge_queue._maybe_clear_expected_red`` could never clear an
    ``expected_red`` entry either (quadraui#542, quadraui#492).

    Only fires when ``oracle.active`` (a driver-configured repo, an issue
    in a milestone whose Gate-A contract is merged — the same condition
    that gated JIT-slice authoring) AND this issue is not
    ``oracle.issue_exempt`` (``manifest.exempt`` / the ``oracle:exempt``
    label — an issue that never consumed the sealed suite has nothing for
    ``record`` to re-run, so gating on its absence would be a NEW blocking
    gate an exempt issue never opted into, exactly what #2199's acceptance
    criteria rule out). Every other issue — no driver, no milestone,
    ``--no-acceptance`` — passes straight through unchanged.

    *gate_checker* (#2199 review, blocking finding 3): resolves
    ``--for-path`` for a ROUTED repo (``acceptance.drivers.<repo>.routes``)
    exactly like :func:`_decide_acceptance_author` already does — without
    it, ``coord commands/acceptance.py``'s ``_resolve_driver`` hard-refuses
    every dispatch on a routed repo with "no route matched", so the trust
    gate silently never functioned there at all.
    """
    if oracle is None or not oracle.active or oracle.issue_exempt:
        return None
    assert gate_checker is not None, (
        "oracle.active implies resolve_oracle_decision ran with a real "
        "gate_checker; decide() always threads one through"
    )

    sha = verifier.branch_head_sha(state)
    if sha is None:
        # GitHub unreachable or the branch vanished between polls — don't
        # let a transient lookup block a whole drive run; retry next poll.
        return _wait(label="ACCEPTANCE: waiting to resolve the pushed SHA")

    if state.work_acceptance_sha == sha:
        if state.work_acceptance_state == "passed":
            return None  # trust gate already green for this exact commit
        if state.work_acceptance_state == "failed":
            # Same shape as a failed Test verdict (#2199 acceptance: "a
            # failed trust gate must block, not warn ... at the same place
            # a failed Test verdict does") — a bounded fix-round retry,
            # sharing the ONE fix budget `_decide_test` also spends from
            # (#1692: a failed test and a failed trust gate are two shapes
            # of the same "the work needs another round" loop).
            if counters.fix_rounds >= opts.max_fix_rounds:
                return _die(
                    f"acceptance trust gate still failing after "
                    f"{counters.fix_rounds} fix round(s) at {sha} — "
                    "stopping.\n"
                    f"   Reason: {state.work_acceptance_reason or 'none recorded'}\n"
                    f"   Inspect: coord log {state.work_aid} --machine "
                    f"{state.work_machine or machine}"
                )
            counters.fix_rounds += 1
            return Action(
                kind=RUN,
                label=(
                    "ACCEPTANCE: trust gate failed → fix round "
                    f"{counters.fix_rounds}/{opts.max_fix_rounds} "
                    f"(coord fix {state.work_aid})"
                ),
                command=("fix", state.work_aid),
                error_message=(
                    f"coord fix {state.work_aid} failed to dispatch after a "
                    "failed acceptance trust gate."
                ),
            )

    # No verdict recorded yet for this exact SHA — never run, or run
    # against a now-stale one (a fix round, a rebase). Dispatch it.
    #
    # #2199 review (blocking finding 1): `acceptance_gate_attempts` is
    # scoped to THIS sha, not a lifetime total — a fresh SHA (this branch
    # runs again after a fix round pushed a new commit) gets its own full
    # attempt budget. Without this reset, the legitimate re-dispatch for
    # SHA2 inherited SHA1's already-spent count and died immediately with a
    # false "environment broken" diagnosis on exactly the second round the
    # docstring above says this mirrors `_decide_test` for.
    if counters.acceptance_gate_attempts_sha != sha:
        counters.acceptance_gate_attempts = 0
        counters.acceptance_gate_attempts_sha = sha

    # `on_error="warn"` is deliberate: `coord acceptance record` exits
    # non-zero BOTH when it successfully records a red verdict (the board
    # write already happened before it exits — the next poll's
    # `work_acceptance_state == "failed"` branch above is what actually
    # bounces to Fix) and when it errors out before ever reaching a verdict
    # (no local checkout, a driver crash, a git fetch failure). Raising
    # `DriveError` here on either would turn a routine red trust-gate round
    # into a terminal drive failure — exactly the "must block [via Fix],
    # not warn" the acceptance criteria ask for, achieved one poll later by
    # observation rather than by inspecting this exit code directly.
    if counters.acceptance_gate_attempts >= opts.max_work_retries:
        return _die(
            f"acceptance trust gate never produced a verdict for {sha} "
            f"after {counters.acceptance_gate_attempts} attempt(s) — "
            "`coord acceptance record` keeps failing before recording "
            "anything (checkout/driver/environment problem, not a red "
            "suite).\n"
            f"   Inspect: coord acceptance record --repo {state.repo} "
            f"--issue {state.issue} --sha {sha}"
        )
    counters.acceptance_gate_attempts += 1

    # #2199 review (blocking finding 3): resolve `--for-path` exactly like
    # `_decide_acceptance_author` does — a routed repo's `_resolve_driver`
    # (coord/commands/acceptance.py) hard-refuses with "no route matched"
    # when it's omitted, which made the trust gate structurally unable to
    # ever record a verdict for such a repo.
    from coord.acceptance import ForPathResolutionError  # noqa: PLC0415

    try:
        for_path = gate_checker.resolve_for_path(state.repo, state.milestone_number)
    except ForPathResolutionError as exc:
        return _die(
            f"could not resolve --for-path for {state.repo}'s acceptance "
            f"trust gate on #{state.issue}: {exc}"
        )
    command = [
        "acceptance", "record", "--repo", state.repo, "--issue",
        str(state.issue), "--sha", sha,
    ]
    if for_path:
        command += ["--for-path", for_path]

    return Action(
        kind=RUN,
        label=(
            "ACCEPTANCE: trust gate → coord acceptance record --sha "
            f"{sha[:12]}" + (f" --for-path {for_path}" if for_path else "")
        ),
        command=tuple(command),
        on_error="warn",
    )


def _decide_test(
    state: IssueState,
    opts: DriveOptions,
    counters: DriveCounters,
    machine: str,
) -> Action | None:
    """The TEST gate.  ``None`` means "passed/skipped, fall through".

    #1426: coord dispatches this stage itself (``dispatch_smoke`` via the
    ``coord serve`` tick loop or ``coord notify``) onto a capability-matched
    machine; this only OBSERVES ``test_state``, exactly like the review gate
    below.  ``--skip-test`` is the one Test-stage action taken here, and it is
    a ``coord test --skipped`` CLI call — never a direct
    ``record_test_verdict()`` (#1384).
    """
    test_state = state.work_test_state
    if test_state in ("passed", "skipped"):
        return None

    # #1605: the Test-stage CHILD assignment (`type="smoke"`) itself reached
    # a terminal FAILED/cancelled status — a dead agent, a killed process
    # group, a terminal API error, anything short of the worker actually
    # printing `SMOKE: pass`/`SMOKE: fail` — without ever producing a
    # verdict. Before this, `test_state` could be left at `"running"`
    # (`dispatch_smoke`'s own marker, #1426) forever: every gate treats
    # `"running"` as "no verdict yet" (#1395), so nothing downstream ever
    # resolves it and this function prints "TEST: in progress" every poll,
    # unbounded — the exact #1598 incident this closes (2.5 hours against
    # three idle machines). `reconcile_completed_assignments` /
    # `coord diagnose --stage test` normally resolve this from the daemon's
    # own tick, but a live `coord drive` loop must not depend on that
    # timing — detect the contradiction directly from the child's own board
    # fields and stop with an actionable message rather than poll forever.
    # Scoped to smoke FAILED/cancelled (not "done"): a fresh `done` smoke
    # completion has an expected, bounded propagation lag before `coord
    # notify` records its verdict — that is NOT this bug.
    if state.smoke_status in ("failed", "cancelled") and test_state == "running":
        return _die(
            "test stage is stuck: work.test_state='running' but its "
            f"Test-stage worker {state.smoke_aid} already finished "
            f"(status={state.smoke_status!r}, reason="
            f"{state.smoke_failure_reason or 'none recorded'!r}) — the "
            "parent verdict was never resolved (#1605).\n"
            f"   Recover: coord diagnose {state.repo} {state.issue} --stage "
            "test\n"
            "   (add --reset if the diagnosis alone doesn't clear it)"
        )

    if test_state == "":
        if opts.skip_test:
            return Action(
                kind=RUN,
                label="TEST: --skip-test → recording 'skipped'",
                command=(
                    "test",
                    "--skipped",
                    "--reason",
                    "coord drive --skip-test",
                    state.work_aid,
                ),
                sleep_after=5.0,
            )
        # Waiting for coord to dispatch the Test stage itself.  The stall
        # detector nudges `coord notify` (--notify) after --stall minutes of no
        # state change — no need to force it here on every poll.
        return _wait()

    if test_state == "running":
        return _wait(label="TEST: in progress on a capability-matched machine")

    if test_state == "failed":
        # #2596: `test_state == "failed"` with an EMPTY reason is not a
        # graded failure — every genuine failure path populates
        # `test_reason` (a worker's own `SMOKE: fail` explanation, or
        # `coord.confirm_test`'s "REFUTED by an independent re-run" wording,
        # both non-empty by construction). An empty reason here means
        # something upstream flipped the gate to red without ever
        # extracting WHAT failed — an infrastructure signal wearing a test
        # verdict, the same shape #2532's acceptance driver hit (a bare
        # non-zero exit folded into a false-red trust gate with an empty
        # reason string). The 2026-08-22 incident this issue is named for
        # cost two dispatched workers — one escalated to opus — burning 20+
        # minutes finding nothing to fix, because nothing was there to find.
        # Refuse to dispatch and surface it instead of repeating that.
        if not (state.work_test_reason or "").strip():
            return _die(
                "test stage reports test_state='failed' with NO reason "
                "recorded — there is no extracted failure to hand a fix "
                "worker, which means the verdict itself is suspect (an "
                "infrastructure signal misread as a test failure, #2596) "
                "rather than a real one. Dispatching `coord fix` here would "
                "spend a round (and, on retry, a model escalation) finding "
                "nothing.\n"
                f"   Inspect: coord log {state.work_aid} --machine "
                f"{state.work_machine or machine}\n"
                f"   Recover: coord diagnose {state.repo} {state.issue} "
                "--stage test --reset"
            )
        if counters.fix_rounds >= opts.max_fix_rounds:
            return _die(
                f"test still failing after {counters.fix_rounds} fix round(s) — "
                "stopping.\n"
                f"   Reason: {state.work_test_reason or 'none recorded'}\n"
                f"   Inspect: coord log {state.work_aid} --machine "
                f"{state.work_machine or machine}\n"
                f"   Continue by hand: coord assign --interactive --fix-of "
                f"{state.work_aid}"
            )
        counters.fix_rounds += 1
        # `coord fix` gates on the assignment's legacy `smoke_test == "fail"`
        # field — which `coord test --fail` mirrors from `test_state` — and
        # dispatches a follow-up worker with `inherit_branch=True`, so the fix
        # continues the SAME branch rather than orphaning it on a fresh one. It
        # also escalates the model (sonnet → opus) and quotes the stored test
        # output in the briefing.  This is why a test failure is a loop
        # iteration and not a dead end.  (The interactive `--fix-of` and `coord
        # bounce` paths are NOT usable here: `--fix-of` requires --interactive,
        # and `bounce` needs a request-changes REVIEW id, not a failed test.)
        return Action(
            kind=RUN,
            label=(
                f"TEST: failed → fix round {counters.fix_rounds}/"
                f"{opts.max_fix_rounds} (coord fix {state.work_aid})"
            ),
            command=("fix", state.work_aid),
            error_message=(
                f"coord fix {state.work_aid} failed to dispatch.\n"
                "   Most likely the assignment's legacy smoke_test field is not "
                "'fail' — that is\n"
                "   what `coord fix` gates on, and only `coord test --fail` sets "
                "it.\n"
                f"   Check: coord log {state.work_aid}   /   continue by hand: "
                f"coord assign --interactive --fix-of {state.work_aid}"
            ),
        )

    return Action(kind=WAIT, warnings=(f"unexpected test_state '{test_state}'",))


def _decide_review(
    state: IssueState,
    opts: DriveOptions,
    counters: DriveCounters,
    machine: str,
) -> Action | None:
    """The REVIEW gate.  ``None`` means "approved, fall through to merge".

    coord dispatches the review itself once the test verdict lands (the notify
    timer's ``dispatch_pending_reviews``), so this mostly observes — the
    exceptions are the #555 interactive case (one explicit request), the #1584
    dead-reviewer retry, and the #1692 request-changes fix round.

    **#1692 — a request-changes verdict dispatches ``coord fix`` here.** It
    used to ``_wait()`` on a comment that read "the auto-loop dispatches the
    fix", which stopped being true when #1616 replaced the ``coord notify``
    timer with the daemon drain: the drain's responsibility table deliberately
    excludes fix dispatch (#476/#477), ``run_for_review_transition`` never sees
    the transition because the drain already consumed it, and the #1478 stalled
    sweeper is off by default. Three mechanisms, three defensible declines, one
    hole — a 50-minute park to the deadline with nothing dispatched. This arm
    now mirrors the test arm one-for-one: same ``coord fix`` command, same
    ``counters.fix_rounds`` budget, one extra de-duplication latch because a
    review row (unlike a failed test) is not re-created by the fix it triggers.
    """
    verdict = state.review_verdict
    if verdict == "approve":
        return None

    # #1584: the review WORKER itself died (transient API error, network
    # drop, ...) before ever producing a verdict. Before #1584 that worker
    # was mislabelled `done` with `review_verdict == ""`, which fell through
    # to the `verdict == ""` branch below and either waited for a dispatch
    # that would never come or (if `state.work_review_state == "done"`)
    # died with a "REVIEW_VERDICT block failed to parse" message that no
    # longer applies now that the review is correctly `failed`. Checked
    # BEFORE `verdict == ""` so it can never fall through to that stale
    # message or to a silent `_wait()` — the exact regression this issue's
    # own evidence (#1563) was filed over.
    #
    # Re-dispatch via ``coord review <work_aid>`` — NOT ``coord retry
    # <review_aid>``. ``coord retry``'s underlying `_reassign` (coord/
    # reconcile.py) hardcodes `type="work"` on every re-dispatch regardless
    # of the failed assignment's own type (it exists solely to retry WORK
    # rows); pointing it at a review assignment id would silently create a
    # bogus fresh `type="work"` assignment on this issue instead of a
    # review. ``coord review`` is the existing #555 escape-hatch command
    # (already used a few lines below for the interactive case) — a thin,
    # type-correct wrapper over `coord.review.dispatch_review` keyed on the
    # WORK row, which is still `status="done"` (only the review it spawned
    # failed). Bounded the same way as the WORK failed-retry loop in
    # `decide()` (usage-limit-aware wait, then a bounded re-dispatch).
    if state.review_status == "failed":
        if is_usage_limit_reason(state.review_failure_reason):
            return Action(
                kind=WAIT,
                label=(
                    f"REVIEW: {state.review_aid} killed by the usage limit — "
                    "waiting for the reset, not retrying"
                ),
                warnings=(
                    f"usage-limit kill detected on {state.review_aid}: "
                    f"{state.review_failure_reason} — waiting for the reset "
                    "instead of retrying (#1461/#1584)",
                ),
            )
        if counters.review_retries >= opts.max_work_retries:
            return _die(
                f"review {state.review_aid} failed "
                f"{counters.review_retries} retr(ies) in: "
                f"{state.review_failure_reason or 'no reason recorded'}\n"
                f"   inspect: coord log {state.review_aid}"
            )
        counters.review_retries += 1
        return Action(
            kind=RUN,
            label=(
                f"REVIEW: failed → coord review {state.work_aid} "
                f"(attempt {counters.review_retries}/{opts.max_work_retries})"
            ),
            command=("review", state.work_aid),
            error_message=f"coord review failed for {state.work_aid}",
        )

    if verdict == "request-changes":
        # The OUTER cap, and it stays FIRST: an exhausted review loop is
        # terminal no matter how much of this drive's own fix budget is left,
        # and `_dispatch_fix_for_review` would refuse the dispatch anyway
        # (`next_iteration > max_review_iterations` → `max_iterations`, which
        # `coord fix` turns into a non-zero exit). Dying here reports the cap
        # instead of reporting a subprocess failure.
        if state.work_review_iter >= state.max_review_iterations:
            return _die(
                "review requested changes and the fix loop is exhausted\n"
                f"   ({state.work_review_iter} rounds, cap "
                f"{state.max_review_iterations}).\n"
                f"   Findings: coord log {state.review_aid}\n"
                f"   Continue by hand: coord assign --interactive --fix-of "
                f"{state.review_aid}"
            )
        # #1692: `coord fix` routes through `auto_loop.process_review_completion`,
        # whose very first line refuses when `pipeline.auto_loop` is off. Say so
        # here rather than dispatching a subprocess that can only fail — and
        # rather than the pre-#1692 infinite wait, which is what the preflight
        # warning ("this run will report the verdict and stop") already promised
        # not to do.
        if not state.auto_loop:
            return _die(
                "review requested changes but pipeline.auto_loop is OFF — the "
                "review→fix\n"
                "   path is switched off in coordinator.yml, so no fix can be "
                "dispatched.\n"
                f"   Findings: coord log {state.review_aid}\n"
                f"   Continue by hand: coord assign --interactive --fix-of "
                f"{state.review_aid}"
            )
        # Belt-and-braces: `review_verdict` and `review_aid` are read off the
        # SAME board row (`drive_state.project`), so a verdict without an id is
        # impossible today. Assert it rather than assume it — everything below
        # spends money keyed on that id, and `coord fix ""` is not a refusal
        # this arm should ever have to interpret.
        if not state.review_aid:
            return _die(
                "review verdict is 'request-changes' but no review assignment "
                "id is on the board —\n"
                "   refusing to guess which review to fix. Inspect: coord "
                f"gates {state.repo} {state.issue}"
            )
        # Already spent a round on THIS review row and the board hasn't caught
        # up yet. Waiting is the only safe answer: dispatching again would put
        # a second fix worker on the same branch (#476/#477). Once the fix row
        # lands, `decide()`'s `active_count > 0` guard takes over, and when it
        # completes the review row changes id and this latch stops matching.
        if counters.review_fix_dispatched_for == state.review_aid:
            return _wait(
                label=(
                    f"REVIEW: fix already dispatched for {state.review_aid} — "
                    "waiting for the fix row to appear on the board"
                )
            )
        # The driver-side twin of the test arm's bound, sharing ONE budget with
        # it (see `DriveCounters.fix_rounds`): `max_review_iterations` bounds
        # the *issue's* review loop across every drive that ever touches it,
        # `max_fix_rounds` bounds what THIS drive is willing to spend.
        if counters.fix_rounds >= opts.max_fix_rounds:
            return _die(
                f"review requested changes after {counters.fix_rounds} fix "
                "round(s) this drive — stopping.\n"
                f"   (review iteration {state.work_review_iter}/"
                f"{state.max_review_iterations} is NOT exhausted; this drive's "
                "own --max-fix-rounds is.)\n"
                f"   Findings: coord log {state.review_aid}\n"
                f"   Continue by hand: coord assign --interactive --fix-of "
                f"{state.review_aid}"
            )
        counters.fix_rounds += 1
        counters.review_fix_dispatched_for = state.review_aid
        # #1622 widened `coord fix` to take a REVIEW id whose verdict was
        # request-changes; #1692 is the review arm finally walking through that
        # door. The REVIEW id, not `state.work_aid`: the work-id form gates on
        # the legacy `smoke_test == "fail"` field and would be refused here.
        # It is not a second implementation of fix dispatch — it hands the row
        # to `auto_loop.process_review_completion` → the single `_dispatch_fix`
        # chokepoint, so `pipeline.auto_loop`, the #476/#1456
        # approve-with-nits gate, the #522 terminal-work guard and
        # `max_review_iterations` all still apply. No `sleep_after`: the test
        # arm above dispatches the same command on the same plain poll
        # interval, and the latch — not a timing guess — is what makes the
        # next poll safe.
        return Action(
            kind=RUN,
            label=(
                f"REVIEW: request-changes → fix round {counters.fix_rounds}/"
                f"{opts.max_fix_rounds} (coord fix {state.review_aid}, review "
                f"iteration {state.work_review_iter + 1}/"
                f"{state.max_review_iterations})"
            ),
            command=("fix", state.review_aid),
            error_message=(
                f"coord fix {state.review_aid} failed to dispatch the review "
                "fix.\n"
                "   Its refusals are all guards doing their job: auto_loop "
                "disabled, no structured\n"
                "   findings, approve-with-nits (#476), max_review_iterations, "
                "or the #522\n"
                "   terminal-work guard — the message above names which.\n"
                f"   Check: coord log {state.review_aid}   /   continue by hand: "
                f"coord assign --interactive --fix-of {state.review_aid}"
            ),
        )

    if verdict == "":
        if state.work_review_state == "done":
            return _die(
                f"review {state.review_aid} finished but recorded NO verdict — the\n"
                "   REVIEW_VERDICT block failed to parse (#1346/#1348 class).\n"
                "   Recover: coord post-pending-reviews, or read the transcript "
                "directly."
            )
        # No review row at all.  For interactive work that is terminal, not
        # transient (#555) — request one explicitly, once, rather than waiting
        # on a dispatch that will never happen.  Preflight already refused this
        # case unless --force-review was given.
        if not state.review_aid and state.work_provider == "claude-pty":
            if not opts.force_review:
                return _die(
                    f"no review for interactive work {state.work_aid} and "
                    "--force-review not set (#555)."
                )
            if counters.review_dispatches >= 1:
                return _die(
                    f"requested a review for {state.work_aid} but none appeared "
                    "on the board.\n"
                    "   Check for an eligible reviewer machine: coord status"
                )
            counters.review_dispatches += 1
            return Action(
                kind=RUN,
                label="REVIEW: requesting explicitly (interactive work, #555)",
                command=("review", state.work_aid),
                error_message=(
                    f"explicit review dispatch failed for {state.work_aid}"
                ),
            )
        return _wait()

    return Action(kind=WAIT, warnings=(f"unexpected review verdict '{verdict}'",))


# #1505: merge statuses a bounded `coord merge --only` retry can actually
# change. "" / PENDING / READY / MERGING are normal in-flight states — the
# next `coord merge` tick is expected to move them forward. CONFLICT is
# retried too, deliberately: that's what runs `classify_conflict` +
# `dispatch_conflict_fix` (#1474, see this function's docstring). Everything
# else — most commonly NEEDS_ATTENTION, or any status this driver has never
# seen before — cannot be resolved by retrying, so it escalates instead of
# spinning the attempt cap down to zero on a no-op.
#
# #1505 review fix: `merge_queue.plan()`'s `_state_to_plan_status` collapses
# CONFLICT, HUMAN_REQUIRED, and SKIPPED into a single "NEEDS_ATTENTION"
# status for operator display, and `merge_plan` (not the raw `merge_queue`
# table) is what a normal daemon-backed `/board` build actually populates —
# so a literal `status == "CONFLICT"` almost never reaches this function
# without help. `drive_state._merge_entry` is where that help lives: it
# cross-checks the raw `merge_queue` row and reports its un-collapsed state
# whenever the plan says NEEDS_ATTENTION, so a fresh, still-auto-fixable
# conflict lands here as "CONFLICT" (retried) rather than "NEEDS_ATTENTION"
# (escalated on sight). See `_merge_entry`'s docstring for the full story.
_RETRYABLE_MERGE_STATUSES = frozenset({"", "PENDING", "READY", "MERGING", "CONFLICT"})

# #2078: matches the "enqueue blocked by <gate> gate — <reason> (waive with
# <flag>)" line `coord.commands.merge._explain_missing_only_entry` prints for
# each board row a failed `coord merge --only <aid>` matched but could not
# enqueue. `re.MULTILINE` + `$` (not the whole string) so it still finds the
# line even when `_explain_missing_only_entry` reports several matching rows
# (rare — normally there is exactly one board row per (repo, issue)).
_ENQUEUE_BLOCKED_RE = re.compile(r"enqueue blocked by (.+)$", re.MULTILINE)

# #2149: how many consecutive polls `_decide_merge`'s `status == ""` arm may
# wait on a CACHED gate reason (`counters.last_merge_diagnostic`, refreshed
# only by a real `coord merge --only` attempt) before it is forced to spend
# a real attempt instead. Keeps the cheap-wait behaviour #2078 wanted (don't
# hammer a known-blocked gate every single poll) while guaranteeing the
# reason is re-validated on a bounded cadence rather than echoed verbatim
# until the drive's whole `--deadline` expires — the coord-portal#50
# incident, where a review requirement that had already cleared was
# reprinted ~145 times over 2h33m because nothing ever re-checked it.
_MAX_GATE_WAIT_ROUNDS = 5

_PR_NUMBER_RE = re.compile(r"/pull/(\d+)")


def _extract_gate_block_reason(diagnostic: str) -> str | None:
    """Pull the named gate failure out of a captured `coord merge --only`
    diagnostic (#2078).

    `coord merge --only <aid>`, when it finds no queue entry for *aid*,
    prints `_explain_missing_only_entry`'s diagnosis (commands/merge.py) —
    one line per matching board row, each either naming the blocking
    review/smoke gate (what this extracts) or reporting that every gate
    already passes / that no board row matched at all (neither of which this
    matches). `_decide_merge`'s empty-status arm uses a non-``None`` result
    to tell "a real, persistent gate failure" — worth waiting on, like a
    BLOCKED board entry — apart from "not enqueued yet" or "identifier
    didn't resolve", which are still worth retrying. Returns ``None`` for an
    empty *diagnostic* (no attempt has run yet this drive) or one with no
    matching line.
    """
    match = _ENQUEUE_BLOCKED_RE.search(diagnostic)
    return match.group(1).strip() if match else None


# #2157: the two wordings `coord merge --only <aid>` uses for "that entry has
# ALREADY merged". The first is the post-#2157 success line (exit 0); the
# second is the pre-#2157 failure line, still matched because the `coord`
# binary this driver shells out to is whatever is installed on the box — a
# drive running against an older install must reach the same conclusion, and
# the fix is worthless if a version skew reintroduces the exact incident.
_ALREADY_MERGED_RE = re.compile(
    r"merge-queue: entry '(?P<aid>[^']*)' (?:"
    r"already merged(?: \(PR #(?P<pr>\d+)\))?"
    r"|is in state 'merged'"
    r")"
)


def _extract_already_merged(diagnostic: str) -> str | None:
    """Return a human-readable note when a captured `coord merge --only`
    diagnostic says the entry has already merged, else ``None`` (#2157).

    This is the driver's own most recent, first-hand read of the merge queue
    — fresher than the board projection `IssueState.merge_status` comes from,
    which is exactly the gap coord-portal#51 fell into: the slice's queue
    entry read ``merged`` while the board still projected ``status=''``, so
    :func:`_decide_merge` classified a landed merge as a retryable
    empty-status miss, burned all three attempts on it, and died.

    Returns a note (``"PR #60"`` when the wording carried one, ``""``
    otherwise) rather than a bare bool so the wait label can name the PR;
    callers must test against ``None``, never for truthiness.
    """
    match = _ALREADY_MERGED_RE.search(diagnostic or "")
    if match is None:
        return None
    pr = match.group("pr")
    return f"PR #{pr}" if pr else ""


def _extract_pr_number(pr_url: str) -> int | None:
    """Best-effort PR number out of a GitHub PR URL, or ``None``."""
    if not pr_url:
        return None
    m = _PR_NUMBER_RE.search(pr_url)
    return int(m.group(1)) if m else None


# ── #1526: driver/gate divergence ───────────────────────────────────────────
#
# `coord drive` decides `test=`/`review=` are satisfied from
# `work_test_state`/`review_verdict` — the same fields `_decide_test`/
# `_decide_review` above already let through (that is WHY `decide()` ever
# reaches `_decide_merge` at all). `coord merge` enforces a DIFFERENT, fresher
# check (`merge_queue.has_smoke_verdict`/`has_approved_review` — SHA/patch-id
# -anchored freshness for smoke, patch-id voiding for review) and can refuse
# for a reason this driver's own view never sees coming. When it does, the
# refusal text is left on the board as `merge_reason` — persisted on the raw
# queue row's `.error` by `merge_queue.process()` — even while `merge_status`
# itself often still reads a RETRYABLE value like READY, because the
# board-render gate check in `merge_queue.plan()`'s `_entry_gate_status`
# doesn't have the live SHA data a real merge attempt fetches (see that
# function's docstring). Retrying `coord merge` unchanged cannot resolve two
# READS of the board disagreeing with each other; only a human (or a fresh
# verdict) can — see `_merge_gate_divergence`.
# #1640 added two more wordings for the SAME smoke gate: "smoke test verdict
# is stale: …" (merge_queue.process) and "test verdict stale (…)"
# (merge_queue.plan / staging).  Both name a recorded-but-stale verdict, which
# is still the smoke gate — and still the case _merge_gate_divergence exists
# to catch, since `work_test_state` reads "passed" while the merge refuses.
_SMOKE_GATE_MARKERS = (
    "smoke test required",
    "test verdict missing",
    "smoke test verdict is stale",
    "test verdict stale",
)
_REVIEW_GATE_MARKERS = ("review required", "review not approved")

# #2947 (follow-up to #2687): the UAT gate — a human-attended block that only
# `coord uat <id> --passed` (never a `coord merge` retry) can clear.
# `evaluate_uat_verdict` (coord.merge_queue) always opens its message with
# "uat verdict " — "uat verdict missing", "uat verdict FAILED: …", or the
# board-unavailable stand-in "uat verdict required but board unavailable to
# confirm" — so that one prefix covers every wording both `process()` (live
# merge attempt) and `_entry_gate_status` (board/plan render) produce, the
# same "both callers, one string" guarantee `_SMOKE_GATE_MARKERS`/
# `_REVIEW_GATE_MARKERS` document above.
_UAT_GATE_MARKERS = ("uat verdict",)

# #2704: the branch-head-unknown condition
# (`coord.merge_queue.UNKNOWN_BRANCH_HEAD_REASON`) is its OWN gate kind —
# neither "smoke" nor "review" — even though `merge_gate_failures` reports it
# under `gate="review"`. Matching it as "review" here would let
# `_merge_gate_divergence` fire whenever this driver's own cached
# `review_verdict == "approve"` (routinely true: the #2704 incident's
# approval WAS for the current head, coord merge just couldn't confirm it),
# which would escalate proposing `coord review-reaffirm` — asking a human to
# re-bless a review that was never actually refused. The correct response is
# the same as the CI-unreadable wait just below: this driver's cached
# review/test verdicts say nothing usable about a gate that was never
# actually evaluated, so WAIT for the next live GitHub read to succeed
# rather than spend a merge attempt (or a human) on a fabricated refusal.
_UNKNOWN_BRANCH_HEAD_MARKER = _mq_unknown_branch_head_reason.lower()


def _merge_gate_kind(reason: str) -> str | None:
    """Classify a merge-queue block *reason* as the gate it names, or
    ``None`` when it isn't one this module knows a corrective action for.

    Matches both `merge_queue.process()`'s live-attempt wording ("smoke test
    required but no verdict recorded" / "review required but not approved")
    and `merge_queue.plan()`'s board-render wording ("test verdict missing" /
    "review not approved") — the two functions describe the identical gates
    in different words.

    #2704: checked BEFORE the review marker below — `UNKNOWN_BRANCH_HEAD_
    REASON` names its own condition (`"unknown_head"`), never "review" or
    "smoke", regardless of which gate's refusal carried it.

    #2947: `"uat"` is likewise its own kind, never folded into "review" or
    "smoke" — it is a human-attended gate with no re-runnable measurement
    behind it (see `_UAT_GATE_MARKERS`), so callers must route it to a
    bare wait for a human verdict, never a `coord merge` retry or an
    automated re-test/re-review escalation.
    """
    r = (reason or "").lower()
    if _UNKNOWN_BRANCH_HEAD_MARKER in r:
        return "unknown_head"
    if any(marker in r for marker in _SMOKE_GATE_MARKERS):
        return "smoke"
    if any(marker in r for marker in _REVIEW_GATE_MARKERS):
        return "review"
    if any(marker in r for marker in _UAT_GATE_MARKERS):
        return "uat"
    return None


# #1738: the two wordings that name a verdict recorded-but-STALE specifically
# (`merge_queue.process`'s live-attempt text and `merge_queue.plan`'s
# board-render text — see the module comment above `_SMOKE_GATE_MARKERS`) —
# a strict subset of `_SMOKE_GATE_MARKERS`, which also matches "no verdict at
# all" ("smoke test required"/"test verdict missing"). Only the stale case
# has a safe, bounded, fully-automatable fix: re-run the Test stage against
# the CURRENT base and let a fresh verdict land. A missing-verdict divergence
# is the #1640 lost-write shape instead — driver and gate disagree about
# whether a verdict exists at all, which a re-test can't safely paper over —
# so that one still escalates to a human on first encounter, unchanged.
#
# #1769: this used to be defined HERE, and #1769 added a second consumer in
# the merge lane (`coord merge --revalidate`). Rather than let a second copy
# of the same string matching drift silently apart from the code that emits
# the strings, the definition was lifted to `coord.merge_queue` — which is
# where both wordings are actually produced (`SmokeVerdictStatus.message` /
# `.short_reason`) and which both lanes already depend on. These are aliases,
# not copies: `tests/test_merge_queue.py` asserts identity.
_STALE_SMOKE_MARKERS = _mq_stale_smoke_markers
_is_stale_smoke_reason = _mq_is_stale_smoke_reason

# #2229: `coord merge --only` prints a gate line per REFUSED gate, and the
# same run prints a "waived by this run" variant of that line for a gate the
# invocation's own `--skip-review`/`--skip-smoke` disarmed
# (`coord.commands.merge`, the `_status` ternary). A waived gate did not
# refuse anything, so its line must never be read back as a refusal.
_GATE_WAIVED_MARKER = "waived by this run"


def _extract_gate_refusal_reason(diagnostic: str | None) -> str:
    """The first smoke/review/uat gate refusal named anywhere in a captured
    `coord merge --only` *diagnostic*, or ``""`` (#2229; uat added #2947).

    Unlike :func:`_extract_gate_block_reason` — which matches only
    `_explain_missing_only_entry`'s "enqueue blocked by <gate>" wording, i.e.
    the NOT-YET-ENQUEUED shape — this reads the refusal out of a run where
    the entry *did* resolve. `coord merge --only` echoes one
    ``  gate <name>: <reason> — will block this merge`` line per failing gate
    before `process()` runs, and `process()`'s own events (``smoke_required
    — …``) name it again; both are ordinary lines carrying the same gate
    vocabulary :func:`_merge_gate_kind` already classifies, so this scans
    lines rather than pinning one exact format that would silently stop
    matching the day either message is reworded.

    quadraui#309: that text is the ONLY place the refusal existed. The board
    plan reported ``READY`` with an empty reason (``merge_queue.plan()``'s
    `_entry_gate_status` degrades to a no-op without live SHA data — the
    #1640 door, the #1566 "plan says ready, ``--only`` refuses" split), so
    `_merge_gate_divergence` saw nothing to classify and the drive spent
    three blind retries and died — printing this very text in its own death
    message.
    """
    for line in (diagnostic or "").splitlines():
        if _GATE_WAIVED_MARKER in line.lower():
            continue
        if _merge_gate_kind(line) is not None:
            return line.strip()
    return ""


def _effective_merge_gate_reason(
    state: IssueState, counters: DriveCounters
) -> tuple[str, bool]:
    """The reason to classify the merge gate from, and whether it came from
    the captured diagnostic rather than the board (#2229).

    Prefers `state.merge_reason` — live, re-read on every poll. Falls back to
    `counters.last_merge_diagnostic` only when the board reason names no gate
    this module knows an action for, because that is exactly the quadraui#309
    shape: the board is silent while this driver's OWN last `coord merge
    --only` attempt already captured the refusal verbatim.

    The fallback is a LAST resort, taken only when the board offers no
    competing live signal — a snapshot must never outrank something being
    re-read every poll. Three cases keep the board's own reading:

    - ``merge_status == ""`` — "no queue entry at all" is #2078's shape, with
      its own `_extract_gate_block_reason` bounded-wait arm in
      :func:`_decide_merge`. #2229 is about the case #2078 skips wholesale:
      an entry that IS enqueued (READY, PENDING, …) whose refusal only a
      live attempt ever saw.
    - ``merge_status == "CONFLICT"`` — a live merge-mechanics block with its
      own resolution path (#1474/#241's conflict-fix dispatch, reached
      through the bounded retry below). A re-test does not rebase a branch,
      and diverting to one is the same stall this issue is about, inverted.
    - a CI reason (#1891/#1892/#2252/#2347) — `merge_reason` already carries
      a live, recognised signal whose only resolution is more real time
      (#1891/#2814's own arm now spends that time on a real, budget-exempt
      `coord merge --only` re-check rather than a bare wait — see that
      branch's docstring — but the "board's own reading wins over the
      diagnostic fallback" contract here is unchanged). These CI-reason
      checks sit right after the divergence check and must keep winning.

    The bool is the caller's warning label: a diagnostic-derived reason is a
    SNAPSHOT of the last real attempt, refreshed by nothing but another
    attempt (#2149's lesson — coord-portal#50 waited 2h33m on a cached
    "review required" that had already cleared). It is strictly fresher than
    an empty board reason, but it must not be re-acted on indefinitely; see
    `DriveCounters.stale_smoke_diagnostic_attempt`.
    """
    if _merge_gate_kind(state.merge_reason) is not None:
        return state.merge_reason, False
    if state.merge_status == "" or state.merge_status.upper() == "CONFLICT":
        return state.merge_reason, False
    if (
        is_ci_pending_reason(state.merge_reason)
        or is_ci_infra_reason(state.merge_reason)
        or is_ci_flaky_reason(state.merge_reason)
        or is_ci_unreadable_reason(state.merge_reason)
    ):
        return state.merge_reason, False
    fallback = _extract_gate_refusal_reason(counters.last_merge_diagnostic)
    if fallback:
        return fallback, True
    return state.merge_reason, False


def _merge_gate_divergence(state: IssueState, reason: str | None = None) -> str | None:
    """``"smoke"``/``"review"`` when *state* shows the #1526 divergence,
    else ``None``.

    The divergence: the merge gate's *reason* names a smoke/review block
    while this SAME state's `work_test_state`/`review_verdict` says the
    opposite. That contradiction can only come from `coord merge` checking
    something this driver's view does not (freshness against the CURRENT
    branch/base, not just the terminal verdict) — never from a retry, since
    neither input changes by running `coord merge` again unchanged.

    *reason* (#2229) defaults to `state.merge_reason`, the board's own text.
    :func:`_decide_merge` passes :func:`_effective_merge_gate_reason`'s
    result instead so a refusal that exists ONLY in the last captured
    `coord merge --only` diagnostic still classifies.

    #2947: deliberately no `"uat"` arm here. A smoke/review divergence means
    this driver's OWN cached verdict contradicts what the merge gate found —
    evidence a re-test/re-review can resolve. A missing/failed UAT verdict is
    never a contradiction of anything this driver tracks (there is no cached
    `work_uat_state` this function reads) — it is the gate working exactly as
    designed, waiting on a human who has not looked yet. `_decide_merge`
    intercepts `"uat"` before this function is ever consulted, the same way
    it already does for `"unknown_head"`.
    """
    kind = _merge_gate_kind(state.merge_reason if reason is None else reason)
    if kind == "smoke" and state.work_test_state in ("passed", "skipped"):
        return "smoke"
    if kind == "review" and state.review_verdict == "approve":
        return "review"
    return None


def _escalate_merge(
    state: IssueState,
    status: str,
    *,
    gate_kind: str | None = None,
    gate_reason: str | None = None,
) -> Action:
    """Build the EXIT action for a merge status retrying cannot fix (#1505).

    Escalates on the FIRST encounter rather than after exhausting
    ``max_merge_attempts`` — a merge attempt is expensive (a whole
    ``coord merge`` run) and, for these statuses, guaranteed to be a no-op;
    an escalation record is cheap and actionable instead.

    This function stays pure like every other decision in this module (see
    the "STRUCTURE" section of the module docstring) — it only *describes*
    the escalation via the returned :class:`Action`'s ``command``.  The
    actual write happens in :meth:`Driver.run`'s exit handling, which runs
    that command through the CLI exactly like any other board mutation this
    driver performs (``coord escalate record ...``, never a direct
    ``coord.state`` call).

    *gate_kind* (#1526) is set by :func:`_decide_merge` when
    :func:`_merge_gate_divergence` fired — a smoke/review gate refusal this
    driver's own ``work_test_state``/``review_verdict`` view contradicts. The
    proposed command in that case names the specific, safe corrective action
    (re-confirm the test verdict, or a scoped/full re-review) rather than the
    generic "inspect the plan" fallback below.

    *gate_reason* (#2229) is the text that gate_kind was classified from,
    when it is NOT `state.merge_reason` — i.e. when the board carried no
    reason and :func:`_effective_merge_gate_reason` recovered the refusal
    from the last captured `coord merge --only` diagnostic instead. The
    escalation narrative quotes it, because ``reports ''`` is exactly the
    unactionable escalation quadraui#309 would otherwise have produced. The
    recorded ``--gate merge_reason=…`` pair still reports the BOARD's own
    (empty) value — the gates block is a snapshot of board state, and
    overwriting it here would hide the very divergence being escalated.

    Otherwise, the proposed command mirrors the #1477 resolution this issue
    was opened over: when a PR is known, ``gh pr merge --rebase`` + ``coord
    reconcile-merges`` is the sanctioned escape hatch (also documented in
    docs/OPERATING_GOTCHAS.md). Without a known PR number there is nothing
    concrete to propose beyond pointing at the plan for a human to read.
    """
    pr_number = _extract_pr_number(state.merge_pr_url)
    if gate_kind == "smoke":
        # #1738: lead with re-dispatching the Test stage, not with the
        # hand-recorded `coord test --passed` — that command records a
        # verdict for a run that never happened if pasted without actually
        # re-running the suite, and it was the path of least resistance at
        # 2am on an issue everyone already believed was green. This
        # escalation only fires once the automated re-test arm in
        # `_decide_merge` has already spent its `fix_rounds` budget (or hit
        # the "missing verdict" divergence that arm deliberately doesn't
        # touch), so the safe, verified remedy is offered FIRST; the
        # hand-recorded form is still here as the explicit fallback for a
        # human who has actually re-run the suite themselves.
        proposed = (
            f"coord diagnose {state.repo} {state.issue} --stage test "
            "--reset   # re-run the Test stage against the CURRENT base "
            "(preferred) — or, ONLY if you have personally just re-run the "
            f"suite against the current base yourself: coord test "
            f"{state.work_aid} --passed"
        )
    elif gate_kind == "review":
        proposed = (
            f"coord review-reaffirm {state.work_aid} --reason '<why this "
            f"delta is safe>'   # or a full re-review: coord review "
            f"{state.work_aid}"
        )
    elif pr_number is not None:
        proposed = f"gh pr merge {pr_number} --rebase && coord reconcile-merges"
    else:
        proposed = (
            f"coord merge --plan --repo {state.repo}   "
            "# inspect the gates, then decide"
        )

    if gate_kind is not None:
        driver_view = (
            f"test_state={state.work_test_state!r}"
            if gate_kind == "smoke"
            else f"review_verdict={state.review_verdict!r}"
        )
        reported = state.merge_reason if gate_reason is None else gate_reason
        reason = (
            f"{gate_kind}_required — coord merge's own gate reports "
            f"{reported!r}, but this driver's OWN view already "
            f"shows {driver_view} — the two cannot converge by retrying the "
            "identical `coord merge` command (#1526); a human must "
            "reconcile them"
        )
    else:
        reason = (
            f"merge_status={status or '(empty)'} — no number of retries changes "
            "this; escalating on first encounter instead of burning the "
            "merge-attempt budget (#1505)"
        )
    gate_pairs = (
        ("merge_status", status or "(empty)"),
        ("merge_reason", state.merge_reason or "(none)"),
        ("review_verdict", state.review_verdict or "(none)"),
        ("test_state", state.work_test_state or "(none)"),
        ("pr_url", state.merge_pr_url or "(none)"),
    )
    gates_summary = " | ".join(f"{k}={v}" for k, v in gate_pairs)
    aid = state.merge_aid or state.work_aid

    command: list[str] = [
        "escalate", "record", state.repo, str(state.issue),
        "--stage", "merge",
        "--reason", reason,
    ]
    for k, v in gate_pairs:
        command += ["--gate", f"{k}={v}"]
    command += ["--command", proposed]
    if aid:
        command += ["--assignment", aid]

    return Action(
        kind=EXIT,
        exit_code=EXIT_ESCALATED,
        message=(
            f"merge escalated: {reason}\n"
            f"   gates: {gates_summary}\n"
            f"   proposed: {proposed}\n"
            f"   Recorded on the board — see: coord escalate list --repo {state.repo}"
        ),
        command=tuple(command),
        error_message=(
            "failed to record the escalation on the board (exiting anyway — "
            f"resolve by hand: coord escalate record {state.repo} {state.issue} "
            "--reason ... --command ...)"
        ),
    )


def _decide_merge(
    state: IssueState, opts: DriveOptions, counters: DriveCounters
) -> Action:
    """The MERGE stage.

    #1474: a CONFLICT status must NOT be a bare wait.  ``dispatch_conflict_fix``
    (coord.conflict_fix) has exactly two sanctioned callers — inside an actual
    ``coord merge`` run, and the semantic-escalation variant behind
    ``pipeline.escalate_semantic_conflicts`` that only ``coord resume``
    (human-invoked) reaches — so nothing ever dispatches the fix worker while
    this function just parks on ``_wait()``.  That was the exact deadlock that
    stalled #1453/#1461 for ~14 hours despite ``classify_conflict()`` correctly
    saying ``rebaseable`` and a capable machine being idle: the coordinator's
    own #241 auto-rebase machinery was never invoked.

    The fix is to fall through to the same bounded ``coord merge --only <aid>``
    retry below every other non-terminal status already uses — that call is
    what runs ``classify_conflict`` + ``dispatch_conflict_fix`` (or discovers
    one is already in flight / already failed and escalates to
    ``HUMAN_REQUIRED``, which stays terminal via the check above). Once a
    conflict-fix is actually dispatched, it shows up as a `type="conflict-fix"`
    row for this same issue, so the very first check in :func:`decide`
    (``state.active_count > 0`` → wait) parks the run there on the next poll —
    :func:`decide` never even reaches this function while it is running. That
    is what keeps this from double-dispatching or fighting an in-flight fix;
    :func:`coord.conflict_fix.has_prior_conflict_fix` /
    :func:`~coord.conflict_fix._has_active_conflict_fix` are the belt-and-
    braces guard inside ``dispatch_conflict_fix`` itself.

    #1526: the driver/gate divergence (:func:`_merge_gate_divergence`) is
    checked FIRST, before the status switch below — it can hide behind
    EITHER a nominally-blocking status (BLOCKED, if ``merge_queue.plan()``'s
    own render-time gate check caught the same disagreement) or a
    nominally-retryable one (READY/PENDING/"", if only a live ``coord
    merge`` attempt caught it and left its reason on the board — see that
    function's docstring for why the two checks can disagree). Escalating
    here, before either branch runs, is what stops the driver from spending
    its whole ``--max-merge-attempts`` budget retrying a merge that cannot
    succeed until a human — or a fresh verdict — reconciles the two
    readings; retrying the identical ``coord merge`` command changes neither
    side of the disagreement.
    """
    # #2229: classify from the board's `merge_reason` when it names a gate,
    # and otherwise from this driver's OWN last `coord merge --only` output.
    # quadraui#309 sat blocked for 11h on a merge that landed first try by
    # hand because the board reason was empty (#1566/#1640) while the
    # captured diagnostic said, in as many words, "smoke test verdict is
    # stale" — the exact string the #1738 auto-repair arm below keys on. It
    # was used for one thing: being printed into the give-up message.
    gate_reason, gate_reason_from_diagnostic = _effective_merge_gate_reason(
        state, counters
    )
    # #2704: the branch head itself could not be read (GitHub unreachable,
    # `gh` unauthenticated, or a rate limit) — checked before the divergence
    # classification below, which would otherwise fire on this driver's own
    # cached review_verdict/work_test_state (routinely still "approve"/
    # "passed" — the gate was never actually re-evaluated, not contradicted)
    # and escalate a re-review or Test re-run for a refusal nothing here
    # confirmed. No retry or fix this driver can take changes GitHub's
    # answer; wait, exactly like the CI-unreadable case below, and never
    # spend a merge attempt re-observing the identical unreadable probe.
    if _merge_gate_kind(gate_reason) == "unknown_head":
        return _wait(
            label=(
                "MERGE: branch head unknown — GitHub read failed (rate "
                "limit, auth, or network); waiting, not retrying (#2704): "
                f"{gate_reason}"
            )
        )
    # #2947 (follow-up to #2687): the UAT gate is a human-attended block — no
    # `coord merge` retry, re-test, or re-review can clear it, only an
    # operator recording `coord uat <id> --passed|--failed` after clicking
    # through the deployed preview. Before this check, `_merge_gate_kind`
    # returned `None` for every UAT wording (`_UAT_GATE_MARKERS` did not
    # exist), so this fell through to the same bounded retry every other
    # retryable status uses — burning the whole `--max-merge-attempts`
    # budget against a gate that cannot change no matter how many times
    # `coord merge --only` is retried, then dying with a terminal `blocked`
    # drive-queue entry nothing re-evaluates (`coord/drive_queue.py`'s
    # `blocked` state). Checked here, before the divergence classification
    # (which deliberately has no `"uat"` arm — see
    # `_merge_gate_divergence`'s docstring) and before the status switch
    # below, mirrors the #2704 `unknown_head` arm immediately above: wait for
    # a human to act rather than spend an attempt or escalate. `gate_reason`
    # already carries `evaluate_uat_verdict`'s full message — the missing/
    # failed verdict, the resolved preview URL (or why none resolved), and
    # the exact `coord uat` command — so surfacing it verbatim here is what
    # gets it into this driver's `STATUS:`/`coord status` output next to the
    # command that clears it, per #2687's own filing.
    if _merge_gate_kind(gate_reason) == "uat":
        return _wait(
            label=(
                "MERGE: blocked on UAT — a human must record a verdict; "
                f"waiting, not retrying (#2947): {gate_reason}"
            )
        )
    divergence = _merge_gate_divergence(state, reason=gate_reason)
    if (
        gate_reason_from_diagnostic
        and divergence == "smoke"
        and _is_stale_smoke_reason(gate_reason)
        and counters.stale_smoke_diagnostic_attempt == counters.merge_attempts
    ):
        # A re-test already fired off THIS snapshot and no real attempt has
        # re-captured it since, so re-firing would spend a second fix round
        # on evidence that literally cannot have changed. Drop to the bounded
        # retry below instead: that attempt refreshes the diagnostic, which
        # either lands the merge or re-arms this arm against fresh evidence.
        divergence = None
    if divergence == "smoke" and _is_stale_smoke_reason(gate_reason):
        # #1738: a STALE (not missing) smoke verdict has a safe, bounded
        # self-service fix this driver can take without a human — re-run the
        # Test stage against the current base via the same non-destructive
        # reset `coord diagnose --stage test --reset` already performs
        # (clears `test_state` so `dispatch_pending_smoke` picks the work
        # back up on its own next tick; the branch/commits are untouched).
        # Bounded by the SAME `fix_rounds` budget the test-failed and
        # review-request-changes arms already share, so a verdict that keeps
        # going stale (e.g. a base that keeps moving under it) still
        # converges to an escalation instead of spinning forever.
        if counters.fix_rounds >= opts.max_fix_rounds:
            return _escalate_merge(
                state,
                state.merge_status,
                gate_kind=divergence,
                gate_reason=gate_reason,
            )
        counters.fix_rounds += 1
        if gate_reason_from_diagnostic:
            # #2229: arm the latch above — one re-test per captured
            # diagnostic, then a real attempt has to re-validate it.
            counters.stale_smoke_diagnostic_attempt = counters.merge_attempts
        return Action(
            kind=RUN,
            label=(
                "MERGE: smoke verdict stale → re-test round "
                f"{counters.fix_rounds}/{opts.max_fix_rounds} "
                f"(coord diagnose {state.repo} {state.issue} --stage test --reset)"
            ),
            command=(
                "diagnose", state.repo, str(state.issue),
                "--stage", "test", "--reset",
            ),
            error_message=(
                f"coord diagnose {state.repo} {state.issue} --stage test "
                "--reset failed to clear the stale verdict.\n"
                f"   Continue by hand: coord test {state.work_aid} --passed   "
                "# ONLY if the suite genuinely still passes against the "
                "CURRENT base — otherwise dispatch a fresh smoke test"
            ),
        )
    if divergence is not None:
        return _escalate_merge(
            state, state.merge_status, gate_kind=divergence, gate_reason=gate_reason
        )

    # #1891: a CI verdict that has not arrived is not a CI verdict of "no" —
    # checked BEFORE the `status` switch below (and regardless of what
    # `status` itself reads) because `merge_reason` is the more robust of the
    # two: `drive_state._merge_entry` falls back to the raw queue row's own
    # *persisted* `error` whenever the live plan's re-evaluation comes back
    # empty (e.g. `_gate_refresher`'s periodic snapshot lagging or gapping a
    # real `coord merge` attempt's fresher read — see
    # `coord.merge_queue.CI_PENDING_PREFIX`'s docstring), while `merge_status`
    # has no such fallback and can still read `""`/`"PENDING"`/`"READY"` in
    # exactly that gap. #1891's incident was a drive burning its whole
    # `--max-merge-attempts` budget (and then a drive-queue launch attempt)
    # retrying a merge that only more real time — never another retry —
    # could resolve. No number of retries makes a check that hasn't reported
    # yet report sooner, so none of the four blocks below ever spends
    # `counters.merge_attempts`, exactly like BLOCKED further down.
    #
    # #2814: that guarantee used to be delivered as a BARE `_wait()` for all
    # four of these CI-outcome siblings — no `coord merge` dispatch at all —
    # trusting that *something else* would notice CI changing state and
    # refresh the board out from under it (the daemon's `_gate_refresher`
    # tick, surfaced via `drive_state._merge_entry`'s #2808
    # `ci_rollup_all_clear` recovery, or a live `coord merge`/auto-drain
    # invocation elsewhere incrementing `ci_infra_reruns`/`ci_flaky_reruns`/
    # `ci_unreadable_reruns` in `coord.merge_queue`). That holds on the HTTP
    # `/board` path, but `BoardFetcher._fetch_local` — the daemon-host
    # standalone path `coord drive` uses whenever no `board_service` is
    # configured, see its docstring — deliberately never computes a
    # `merge_plan` at all, so the #2808 recovery has no `ci_summary` to read;
    # the raw queue row's persisted `error` is the ONLY signal available, and
    # the ONLY thing that ever rewrites it (or advances any of the
    # `ci_*_reruns` budgets) is a live `coord merge` attempt. Nothing else
    # periodic makes one on this path — `serve_app._auto_drain_tick` only
    # ever touches `PLAN_READY` entries, and `_gate_refresher` only feeds the
    # HTTP path, not `_fetch_local` — so a standalone drive parked on ANY of
    # these four reasons could park on a byte-identical line until the 240m
    # deadline, hours after the real CI state had already moved on
    # (claude-coordinator#2804/#2813, 2026-08-27, first observed for
    # CI-pending; the identical gap equally afflicts CI-unreadable,
    # CI-infra, and CI-flaky since none of them has any other trigger for
    # its rerun either) — `merge_queue.error` outliving the CI run it
    # described.
    #
    # The fix: all four blocks below now keep dispatching a real (non-dry-run)
    # `coord merge --only <aid>` retry every poll while their reason holds,
    # instead of a bare `_wait()`. That is the only thing that rewrites the
    # persisted `error` (or advances the relevant `ci_*_reruns` counter) —
    # `--dry-run` evaluates the same gates but its `save_queue()` call is
    # skipped entirely (`coord/commands/merge.py`), so it would leave the DB
    # row exactly as stale as a bare wait did. Every one of these four
    # entries' gate evaluation is read-only up to and including the
    # `continue`/`return` that skips it (no PR creation, no rebase, no `gh pr
    # merge` — see `merge_queue.process`'s docstring), so retrying costs one
    # `gh pr checks`-shaped round trip (plus, for the infra/flaky/unreadable
    # cases, whatever bounded rerun `process()` itself decides to trigger),
    # not a mechanical operation, and is safe to repeat every poll. Each
    # stays budget-SAFE the same way the #2157 "already merged" wait below
    # does: `counters.merge_attempts` is deliberately never incremented in
    # any of the four, so an indefinitely-pending/unreadable/rerunning check
    # can never exhaust `--max-merge-attempts` and die — only the outer
    # `deadline_mins` bounds this, same as before. Once the underlying CI
    # state actually resolves, the SAME call either lands the merge outright
    # or rewrites `error`/advances the rerun budget to whatever it resolved
    # to, within one poll interval — see
    # `test_checks_pending_retries_for_real_without_spending_an_attempt` and
    # its #2347/#1892/#2252 siblings below.
    #
    # (Non-blocking, flagged in review: this does mean every poll for the
    # whole time an entry sits in one of these four states now takes the
    # host-wide `merge.lock` and makes a live GitHub round trip, where before
    # three of the four made none at all. Each call is bounded and
    # `on_error="warn"` degrades gracefully, but multiple sibling entries
    # simultaneously parked on these reasons on one host will serialize
    # through that same lock every 60s poll — worth watching under heavy
    # concurrent-CI-pending load given #2809's rate-limit-backoff history,
    # not a reason to leave three of the four gaps unplugged.)
    if is_ci_pending_reason(state.merge_reason):
        aid = state.merge_aid or state.work_aid
        return Action(
            kind=RUN,
            label=(
                "MERGE: CI checks have not reported yet — re-checking, not "
                f"spending an attempt (#1891/#2814): {state.merge_reason}"
            ),
            command=("merge", "--only", aid, "--method", opts.merge_method),
            on_error="warn",
            error_message=(
                "coord merge returned non-zero (or the merge lock timed out) "
                "while re-checking pending CI — re-checking next poll"
            ),
            serialize_merge=True,
        )

    # #2347: the sibling case checked right after #1891 — the check-list
    # FETCH itself failed (GitHub unreachable: a transient `gh pr checks`
    # HTTP 5xx, an auth blip), so there is no CI verdict of ANY shape yet,
    # not even a real "still running" one. `coord merge`'s own live attempt
    # already tracks a bounded count of consecutive fetch failures for this
    # (`coord.merge_queue.MAX_CI_UNREADABLE_RERUNS`) and keeps waiting
    # either way — there is no CI to rerun and no gate to re-test, only more
    # real time (GitHub answering again). #2814: on the standalone path
    # nothing else ever makes that live attempt (see the module comment
    # above), so this must dispatch the same real, attempt-exempt
    # `coord merge --only` re-check as CI-pending, not a bare wait — each
    # call either observes GitHub answering again (and rewrites `error`
    # accordingly) or re-observes the identical transport failure for free.
    if is_ci_unreadable_reason(state.merge_reason):
        aid = state.merge_aid or state.work_aid
        return Action(
            kind=RUN,
            label=(
                "MERGE: GitHub could not be reached to read CI status — "
                f"re-checking, not spending an attempt (#2347/#2814): "
                f"{state.merge_reason}"
            ),
            command=("merge", "--only", aid, "--method", opts.merge_method),
            on_error="warn",
            error_message=(
                "coord merge returned non-zero (or the merge lock timed out) "
                "while re-checking unreadable CI — re-checking next poll"
            ),
            serialize_merge=True,
        )

    # #1892: the sibling case — a CI verdict DID arrive, but every failing
    # check said nothing about the code (never assigned a runner, or died
    # before checkout). `coord merge`'s own live attempt is already
    # auto-rerunning CI for this (see `coord.merge_queue.MAX_CI_INFRA_RERUNS`)
    # — retrying `coord merge` here would just re-observe the same in-flight
    # rerun and spend an attempt for nothing... but only if some live attempt
    # is actually happening. #2814: on the standalone path nothing else makes
    # one (see the module comment above), so `ci_infra_reruns` would never
    # advance and this would park exactly like the CI-pending case used to.
    # This now dispatches the same real, attempt-exempt `coord merge --only`
    # re-check every poll — it either drives the rerun forward itself or
    # observes one already in flight from elsewhere, for free either way.
    if is_ci_infra_reason(state.merge_reason):
        aid = state.merge_aid or state.work_aid
        return Action(
            kind=RUN,
            label=(
                "MERGE: CI failed with no verdict about the code — "
                f"auto-rerunning, not spending an attempt (#1892/#2814): "
                f"{state.merge_reason}"
            ),
            command=("merge", "--only", aid, "--method", opts.merge_method),
            on_error="warn",
            error_message=(
                "coord merge returned non-zero (or the merge lock timed out) "
                "while re-checking CI infra failure — re-checking next poll"
            ),
            serialize_merge=True,
        )

    # #2252: the OTHER sibling case — a CI verdict DID arrive AND said
    # something real about the code, but `coord merge`'s own live attempt
    # has only observed it fail ONCE so far and is already re-running the
    # failed job(s) to rule out a flake (see
    # `coord.merge_queue.MAX_CI_FLAKY_RERUNS`) before spending a drive
    # attempt on it. #2814: same standalone-path gap as the other three —
    # nothing else makes that live attempt on `BoardFetcher._fetch_local`, so
    # this now dispatches the same real, attempt-exempt `coord merge --only`
    # re-check every poll instead of a bare wait. Once the re-run's answer is
    # in, this reason either clears (flake — merge proceeds, zero attempts
    # spent) or reverts to the plain "checks failed" wording (confirmed real
    # — spends the attempt exactly like today), so this stays bounded by
    # construction, never open-ended.
    if is_ci_flaky_reason(state.merge_reason):
        aid = state.merge_aid or state.work_aid
        return Action(
            kind=RUN,
            label=(
                "MERGE: CI failed — re-running once to rule out a flake, "
                f"not spending an attempt (#2252/#2814): {state.merge_reason}"
            ),
            command=("merge", "--only", aid, "--method", opts.merge_method),
            on_error="warn",
            error_message=(
                "coord merge returned non-zero (or the merge lock timed out) "
                "while re-checking a possible CI flake — re-checking next "
                "poll"
            ),
            serialize_merge=True,
        )

    status = state.merge_status
    if status.upper() == "HUMAN_REQUIRED":
        return _die(
            f"merge entry is HUMAN_REQUIRED: {state.merge_reason or 'no reason recorded'}\n"
            "   An automated conflict-fix already gave up. Resolve by hand, or "
            "override:\n"
            f"     coord merge --only {state.merge_aid or state.work_aid} "
            "--override-human-required '<reason>'"
        )
    if status.upper() == "BLOCKED":
        return _wait(
            label=(
                "MERGE: blocked — "
                f"{state.merge_reason or 'gate not satisfied'}; re-checking"
            )
        )

    # #2078: an EMPTY status means "the board has no merge-queue entry for
    # this issue at all" — a fundamentally different fact from a real
    # PENDING/READY/MERGING/CONFLICT status, even though `""` sits in
    # `_RETRYABLE_MERGE_STATUSES` alongside them and used to retry
    # identically. `coord merge --only <aid>`, when it finds no entry, prints
    # exactly why (`_explain_missing_only_entry`, #1695) — every attempt
    # below captures that text into `counters.last_merge_diagnostic`
    # (`Driver._loop`), so by the SECOND empty-status poll this driver
    # already knows, from its own prior attempt, whether the row is simply
    # not enqueued yet (self-heals — fall through to the bounded retry) or
    # apparently blocked on a review/smoke gate that an IMMEDIATE identical
    # `--only` cannot change (wait a few cheap polls instead of spending
    # another attempt on a likely no-op). A CI-only block stays invisible
    # here — CI is only evaluated once a row is enqueued — and falls through
    # to the retry below, where the die message now quotes whatever the
    # driver actually observed instead of echoing the board's empty fields.
    #
    # #2149: "apparently" and "likely", not "genuinely" and certain — unlike
    # the live BLOCKED arm above, `gate_reason` here is a SNAPSHOT of the
    # LAST real attempt, and nothing refreshes it while this arm just waits.
    # coord-portal#50 spent 2h33m re-printing an identical "review required"
    # reason for a review that had already cleared, because the wait never
    # re-checked and never counted against any budget. So the wait is capped
    # at `_MAX_GATE_WAIT_ROUNDS`: once reached, fall through to the SAME
    # bounded retry every other retryable status uses below — a real
    # attempt, which either lands (the gate cleared) or refreshes the
    # diagnostic for the next `_MAX_GATE_WAIT_ROUNDS`-round wait.
    if status == "":
        gate_reason = _extract_gate_block_reason(counters.last_merge_diagnostic)
        if gate_reason:
            if counters.gate_wait_rounds < _MAX_GATE_WAIT_ROUNDS:
                counters.gate_wait_rounds += 1
                rounds_left = _MAX_GATE_WAIT_ROUNDS - counters.gate_wait_rounds
                return _wait(
                    label=(
                        "MERGE: blocked (not yet enqueued) — "
                        f"{gate_reason} (as of "
                        f"{counters.gate_wait_rounds} check"
                        f"{'s' if counters.gate_wait_rounds != 1 else ''} ago; "
                        f"re-attempting for real in {rounds_left} more if "
                        "still blocked then); re-checking"
                    )
                )
            counters.gate_wait_rounds = 0

    # #2157: this driver's OWN last `coord merge --only <aid>` reported the
    # entry has already merged. That is the postcondition this whole stage is
    # waiting for, observed first-hand — and it is strictly fresher than
    # `status`, which comes from the board projection and can still read `''`
    # (coord-portal#51: the queue entry read `merged` while the board row had
    # not reconciled yet). Treated exactly like the #1891/#1892 waits: the
    # only thing that resolves it is more real time — the board catching up,
    # after which `decide()`'s own terminal-merged arm (or, in the slice lane,
    # `_decide_acceptance_author`'s `status == "merged"` pass-through) takes
    # over — never another `coord merge` retry. So it costs ZERO attempts:
    # counting a landed merge against `--max-merge-attempts` is what turned a
    # success into an `exhausted` exit 1 and blocked the drive-queue entry.
    #
    # Checked BEFORE the `_RETRYABLE_MERGE_STATUSES` escalation below so a
    # stale terminal-looking status (say NEEDS_ATTENTION left over from a
    # conflict the merge then resolved) cannot escalate a merge that landed.
    already_merged = _extract_already_merged(counters.last_merge_diagnostic)
    if already_merged is not None:
        detail = f" ({already_merged})" if already_merged else ""
        return _wait(
            label=(
                f"MERGE: entry {state.merge_aid or state.work_aid} has ALREADY "
                f"merged{detail} — waiting for the board to reconcile, not "
                "retrying (#2157)"
            )
        )

    # #1505: a status no retry can fix (most commonly NEEDS_ATTENTION)
    # escalates immediately rather than falling into the bounded retry below
    # — see `_RETRYABLE_MERGE_STATUSES` and `_escalate_merge`.
    if status.upper() not in _RETRYABLE_MERGE_STATUSES:
        return _escalate_merge(state, status)

    # Cap the attempts: without this, a merge that fails for a reason the board
    # never reflects (so merge_status stays empty) would re-run `coord merge`
    # on every poll until the deadline. The same cap bounds the CONFLICT case
    # (#1474) — a `coord merge --only` that keeps landing back on CONFLICT
    # (e.g. a fresh conflict on every rebase attempt) must still terminate
    # rather than spin forever.
    if counters.merge_attempts >= opts.max_merge_attempts:
        # #2078: quote the last captured `coord merge --only` diagnostic
        # instead of just the board's (possibly still-empty) status/reason
        # fields — `coord merge --plan` was never actually run here, but the
        # SAME underlying gate check (`_explain_missing_only_entry`) already
        # ran, on every attempt above, and its output is exactly the
        # diagnosis a human would otherwise have to go fetch by hand.
        diagnostic = counters.last_merge_diagnostic.strip()
        diagnostic_block = (
            "\n".join(f"     {line}" for line in diagnostic.splitlines())
            if diagnostic
            else "     (no output captured from the merge attempts)"
        )
        return _die(
            f"merge attempted {counters.merge_attempts} times without landing.\n"
            f"   Last board state: status='{status or 'none'}' "
            f"reason='{state.merge_reason or 'none'}'\n"
            f"   Last `coord merge --only` diagnostic:\n"
            f"{diagnostic_block}\n"
            f"   Inspect the gates: coord merge --plan --repo {state.repo}"
        )
    counters.merge_attempts += 1
    aid = state.merge_aid or state.work_aid
    # Tolerant on purpose: the first attempt often lands before the daemon's
    # tick has run `enqueue_approved_work`, so `--only <aid>` finds no queue
    # entry.  That is a "try again next poll", not a reason to abort the run —
    # the attempt cap above is what bounds it. A CONFLICT entry that is
    # already CONFLICT (not PENDING) similarly errors out of `--only`
    # (`coord merge --only` refuses a non-PENDING entry) rather than
    # reclassifying it — that message still counts against the same cap, so
    # a genuinely stuck entry dies with a clear pointer instead of spinning.
    conflict_note = (
        " — retrying via coord merge's own conflict-fix dispatch (#241)"
        if status.upper() == "CONFLICT"
        else ""
    )
    return Action(
        kind=RUN,
        label=(
            f"MERGE: attempt {counters.merge_attempts}/{opts.max_merge_attempts} "
            f"(coord merge --only {aid} --method {opts.merge_method})"
            f"{conflict_note}"
        ),
        command=("merge", "--only", aid, "--method", opts.merge_method),
        on_error="warn",
        error_message=(
            "coord merge returned non-zero (or the merge lock timed out) — "
            "re-checking next poll"
        ),
        serialize_merge=True,
    )


# ── locking ──────────────────────────────────────────────────────────────────

# #1616: ``FileLock``/``LockBusy`` moved to :mod:`coord.filelock` so the daemon's
# pipeline-clock drain (``coord.notify.run_drain``) takes literally the same lock
# class this module's ``run_notify()`` takes on ``~/.coord/notify.lock`` — a
# second implementation agreeing on a filename is not mutual exclusion, it is a
# coincidence.  Re-exported here so every existing ``from coord.drive import
# FileLock`` (tests included) keeps working unchanged.
_ = (FileLock, LockBusy, notify_lock_path)  # re-export; see coord.filelock


# ── the driver (I/O) ─────────────────────────────────────────────────────────


def coord_argv() -> list[str]:
    """The ``coord`` invocation prefix.

    Prefers the installed console script (the same thing a human types).  Falls
    back to ``python -m coord.cli`` when it is not on PATH — which happens under
    a venv whose ``bin`` is not exported, e.g. a worker with the agent venv
    stripped (#402).  Overridable with ``$COORD_DRIVE_COORD_BIN`` for tests.

    #2569: this process's own PATH (this module runs inside ``coord drive`` /
    ``coord drive-queue tick``, whose systemd unit — deploy/coord-drive-queue.
    service — puts ``~/.coord-venv/bin`` FIRST, deliberately, so `cargo`/`git`
    resolve consistently for the driver's own subprocess calls) is NEVER a
    worker's PATH. Every ``coord assign``/``coord fix``/etc. subprocess this
    module launches is a CLI invocation that talks to the target machine's
    agent daemon over HTTP (``coord.dispatch.dispatch`` → ``httpx.post`` to
    ``/assign``); the actual worker `claude -p` subprocess is spawned by that
    OTHER, already-running process (``coord-agent.service``), which builds its
    env fresh via ``coord.agent._worker_subprocess_env`` — this module's own
    PATH is never inherited into it. The two processes' PATH policies are
    independent by construction; do not "fix" one by changing the other. See
    ``_worker_subprocess_env``'s docstring for the worker side of this.
    """
    override = os.environ.get("COORD_DRIVE_COORD_BIN")
    if override:
        return override.split()
    found = shutil.which("coord")
    if found:
        return [found]
    return [sys.executable, "-m", "coord.cli"]


# ── tmux launch (`coord drive --tmux`, #1398) ─────────────────────────────────
#
# A drive runs 60-90 minutes. `--tmux` launches it DETACHED in a
# `coord-drive-<repo>-<issue>` tmux session instead of running inline, so the
# run survives the launching terminal closing, a TUI restart, or an ssh drop
# — the same rationale, and the same `TmuxHost`/`tmux_available`/
# `tmux_session_alive` seam, as the `coord-<assignment_id>` interactive
# sessions in `coord/interactive.py` and the free-floating `coord-term-*`
# terminals in `coord/commands/terminal.py`. Unlike both of those, a drive
# session is LOCAL ONLY — the driver runs on the operator's machine, reading
# the daemon's board over the network, so there is no remote/ssh variant
# here (see the class docstring's "Out of scope" note in #1398).
#
# Killing the tmux session IS Stop: the per-issue `flock` in `Driver.run()`
# is released when the process's file descriptor closes (the OS does this on
# any process exit, including SIGHUP from a killed tmux pane) — no separate
# cleanup code is needed for cancellation to be correct.


def drive_session_name(repo: str, issue: int) -> str:
    """Return the canonical tmux session name for a ``coord drive --tmux`` run."""
    return f"{DRIVE_SESSION_PREFIX}{repo}-{issue}"


def parse_drive_session_name(session_name: str) -> tuple[str, int] | None:
    """Parse a ``coord-drive-<repo>-<issue>`` session name back to ``(repo, issue)``.

    Returns ``None`` when *session_name* doesn't carry the drive prefix, or
    the segment after the LAST hyphen isn't a bare issue number (repo names
    may themselves contain hyphens, so the issue number — always numeric —
    is what anchors the split).
    """
    if not session_name.startswith(DRIVE_SESSION_PREFIX):
        return None
    rest = session_name[len(DRIVE_SESSION_PREFIX):]
    repo, sep, issue_str = rest.rpartition("-")
    if not sep or not repo or not issue_str.isdigit():
        return None
    return repo, int(issue_str)


def list_drive_sessions(*, host: TmuxHost = TmuxHost(None)) -> list[dict[str, Any]]:
    """Return live ``coord-drive-*`` tmux sessions on *host* as parsed dicts.

    Each entry: ``{"repo": str, "issue": int, "session_name": str, "attached": bool}``.
    Mirrors :func:`coord.commands.terminal.list_tmux_terminal_sessions` — a
    single ``tmux list-sessions`` call; returns ``[]`` when tmux is
    unavailable, has no server running, or has no matching sessions.
    """
    try:
        result = subprocess.run(
            host.cmd([
                "list-sessions", "-F",
                "#{session_name}\t#{session_attached}",
            ]),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5.0,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    if result.returncode != 0:
        return []

    sessions: list[dict[str, Any]] = []
    for raw_line in result.stdout.splitlines():
        parts = raw_line.split("\t")
        if len(parts) < 2:
            continue
        name, attached_raw = parts[0].strip(), parts[1].strip()
        parsed = parse_drive_session_name(name)
        if parsed is None:
            continue
        repo, issue = parsed
        sessions.append({
            "repo": repo,
            "issue": issue,
            "session_name": name,
            "attached": attached_raw not in ("", "0"),
        })
    return sessions


def launch_drive_in_tmux(
    cmd: Sequence[str],
    *,
    repo: str,
    issue: int,
    host: TmuxHost = TmuxHost(None),
    verify_checks: int = 16,
    verify_interval: float = 0.5,
    sleeper: Callable[[float], None] = time.sleep,
) -> str:
    """Create a detached tmux session named for *(repo, issue)* running *cmd*.

    *cmd* is a full argv (e.g. ``coord_argv() + ["drive", repo, str(issue),
    ...]``) — each element is passed to tmux as a SEPARATE argument, which
    tmux hands to ``execve`` unmodified (no shell re-splitting), so a path
    containing spaces (``--briefing-file``, ``--config``) survives intact.

    Returns the session name.  Raises :class:`DriveError` when tmux is
    unavailable, a session for this *(repo, issue)* is already alive (the
    CLI checks aliveness first for a friendlier message, but this guards
    direct/test callers too), or — #1606 — ``tmux new-session`` succeeded
    but the launched process never actually got a drive loop running.

    #1606: ``tmux new-session`` returning 0 only proves tmux itself started
    a process; it says nothing about whether *that* process stayed up. The
    observed failure (drive dispatched with ``--accept-advisory`` onto a
    zero-commit advisory, decided there was nothing to do, and exited
    immediately) left the session dead and ``Driver.run()``'s own log
    untouched — while this function still returned success and the CLI
    printed the "driving ... in tmux session" banner. ``~/.coord/drive-
    epic.py`` treats *any* zero-exit ``coord drive --tmux`` as a live
    attempt and increments its ledger, so an unreported instant-death here
    silently burns a retry budget without ever running the issue once.
    After the session is created, poll (up to ``verify_checks *
    verify_interval`` seconds, default 8s) for the session to still be
    alive AND ``Driver.run()``'s own run log (``scratch_dir()/<repo>-
    <issue>.log`` — the same path ``Driver.run()`` computes) to have grown
    past whatever it held before this launch. Either check failing raises
    :class:`DriveError` instead of returning a session name — the caller
    must then report failure, not the success banner.

    The growth check relies on ``Driver.run()`` writing a start marker to
    that log the instant its per-issue lock is acquired (see the
    ``drive loop started`` line in ``Driver.run()``) — independent of
    whether a ``RUN`` action (the *only other* writer of this file, via
    ``Driver._spawn``) ever actually fires. Without that marker, "log
    grew" would really mean "a subprocess happened to run first", which is
    false for the ordinary, majority-case launch of attaching to an issue
    that already has another assignment active: ``decide()``'s very first
    branch after "merged" is a pure ``WAIT`` with no command whenever
    ``state.active_count > 0``, so that loop could legitimately sit
    alive-but-log-silent for a full ``--poll`` interval (default 60s) —
    far longer than this function's ~8s verification window — and get
    misdiagnosed as stuck.
    """
    if not tmux_available():
        raise DriveError("tmux is not available on this machine.", EXIT_USAGE)
    session = drive_session_name(repo, issue)
    if tmux_session_alive(session, host=host):
        raise DriveError(
            f"already driving {repo} #{issue} (tmux session {session!r} is live).\n"
            f"   attach with: coord drive-attach {repo} {issue}",
            EXIT_USAGE,
        )
    log_path = scratch_dir() / f"{repo}-{issue}.log"
    try:
        before_mtime = log_path.stat().st_mtime
    except OSError:
        before_mtime = None
    try:
        result = subprocess.run(
            host.cmd(["new-session", "-d", "-s", session, *cmd]),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15.0,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise DriveError(f"failed to create tmux session: {exc}", EXIT_USAGE) from exc
    if result.returncode != 0:
        raise DriveError(
            f"tmux new-session failed: {(result.stderr or '').strip()}", EXIT_USAGE
        )

    alive = True
    grew = False
    for _ in range(max(verify_checks, 1)):
        sleeper(verify_interval)
        alive = tmux_session_alive(session, host=host)
        try:
            grew = log_path.stat().st_mtime > (before_mtime or 0)
        except OSError:
            grew = False
        if grew or not alive:
            break
    if not alive:
        detail = (
            "it did write to its log before exiting" if grew
            else "it never wrote anything to its log"
        )
        raise DriveError(
            f"tmux session {session!r} for {repo} #{issue} already exited "
            f"({detail}) — the drive loop did not stay running, so this is "
            f"not a live background run. Check the log: {log_path}\n"
            "   Re-run without --tmux to see the failure inline instead.",
            EXIT_USAGE,
        )
    if not grew:
        raise DriveError(
            f"tmux session {session!r} for {repo} #{issue} is running but its "
            f"log ({log_path}) was never written to within "
            f"{verify_checks * verify_interval:.0f}s — the drive loop may be "
            f"stuck before its first log line. Attach to inspect: coord "
            f"drive-attach {repo} {issue}",
            EXIT_USAGE,
        )
    return session


def _publish_stall_nudge(repo: str, issue: int, *, stalled_for: float) -> None:
    """Tell the #1632 notifier that this drive just nudged a stalled stage.

    Strictly one-way and strictly advisory: `drive` remains the single
    definition of "stalled" (#1593) and the notifier is a reader of that
    decision, never a second author of it. Import is function-local and the
    whole call is swallowed on failure so a missing/renamed notifier cannot
    perturb the drive loop.

    Published unconditionally, regardless of ``--notify`` — that flag only
    governs whether THIS process also shells out to `coord notify` itself
    (see ``run_notify``); the record below is what the 5-min
    ``coord-notify.timer`` (which runs `coord notify` on its own cadence
    either way) reads to know a stage was nudged at all. A drive run with
    ``--notify`` off still needs the notifier to see its stalls.
    """
    try:
        from coord.notifier.store import record_nudge  # noqa: PLC0415

        record_nudge(repo, issue, at=time.time(), stalled_for=stalled_for)
    except Exception:  # noqa: BLE001 — advisory channel, never breaks a drive
        pass


def _clear_stall_nudge(repo: str, issue: int) -> None:
    """Retract a previously published stall nudge (#2648).

    Called from `_loop` the moment the board fingerprint changes — the
    pipeline advancing past the stage `_publish_stall_nudge` recorded is
    proof that record no longer describes reality. Without this,
    `nudged_at` is per-ISSUE and outlives the assignment that earned it, so
    every later leg of the same issue re-reads the one stale record and the
    notifier's stall-survived-its-nudge condition fires again on each new
    leg's own subject, for as long as the record's TTL allows. Advisory and
    swallowed exactly like the publish half — a missing/renamed notifier
    must not perturb the drive loop.
    """
    try:
        from coord.notifier.store import clear_nudge  # noqa: PLC0415

        clear_nudge(repo, issue)
    except Exception:  # noqa: BLE001 — advisory channel, never breaks a drive
        pass


@dataclass
class Driver:
    """The resumable state machine's I/O shell: poll → decide → execute → sleep."""

    repo: str
    issue: int
    opts: DriveOptions
    config: Any
    fetcher: BoardFetcher = field(default_factory=BoardFetcher)
    verifier: MergeVerifier | None = None
    oracle_gate: AcceptanceGateChecker | None = None
    out: Any = None
    err: Any = None
    sleeper: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    # #1466: injected so tests can stub the Max-plan usage probe without a
    # real `claude -p "/usage"` subprocess — mirrors *verifier*/*oracle_gate*
    # above. Defaults to the real (cached, ~60s) probe.
    usage_prober: Callable[[], PlanLimits] = get_plan_limits
    # #2443: injected so tests can script a fake on-disk HEAD sequence
    # without a real git checkout — same shape as *usage_prober* above.
    # Defaults to the real, local-only (`fetch=False`) probe.
    self_head_probe: Callable[[], str | None] = _current_self_head

    _run_log: Path | None = field(default=None, init=False, repr=False)
    # #2443: this session's own on-disk `coord` HEAD, captured once by
    # `_loop()` right as the poll loop begins — the baseline every later
    # `_self_heal_drift_message` check compares against. `None` when it
    # could not be determined (not a git checkout, unreadable HEAD), which
    # keeps the self-heal check permanently a no-op for this run rather than
    # ever comparing against a placeholder.
    _start_head_sha: str | None = field(default=None, init=False, repr=False)
    # #1499: the terminating Action's own message, captured by `_loop()` right
    # before it returns — `run()` folds this into the `drive_exited` audit
    # summary/details so a non-exceptional terminal exit (e.g. `decide()`
    # returning a `_die(...)` Action for a genuinely failed work assignment,
    # as opposed to a raised DriveError) still narrates WHY, not just the
    # bare exit code.
    _last_exit_message: str = field(default="", init=False, repr=False)
    # #1844: the most recent `_spawn`ed subprocess's combined stdout+stderr —
    # the ONLY place the real text of a `coord assign`/`approve-plan`
    # refusal exists once the subprocess has exited (`_append_run_log`
    # writes the same bytes to disk, but nothing downstream re-reads that
    # file). `_loop`'s RUN-action handling reaches for this when the child
    # exits `EXIT_DISPATCH_REFUSED`, so the guard's own message — remedy
    # included — becomes the raised `DriveError`'s message instead of a
    # generic "coord assign ... exited 5". Overwritten on every `_spawn`
    # call, so it is only ever trustworthy read immediately after one, which
    # is exactly how `_loop` uses it.
    _last_run_output: str = field(default="", init=False, repr=False)

    def __post_init__(self) -> None:
        self.out = self.out or sys.stdout
        self.err = self.err or sys.stderr
        if self.verifier is None:
            self.verifier = GitMergeVerifier(
                repo_path=self.opts.repo_path, warn=self.warn
            )
        if self.oracle_gate is None:
            self.oracle_gate = GitHubAcceptanceGateChecker(config=self.config)

    # ── logging ─────────────────────────────────────────────────────────
    @staticmethod
    def _stamp() -> str:
        return time.strftime("%H:%M:%S")

    def log(self, message: str) -> None:
        print(f"{self._stamp()}  {message}", file=self.out, flush=True)

    def warn(self, message: str) -> None:
        print(f"{self._stamp()}  !! {message}", file=self.err, flush=True)

    def _append_run_log(self, text: str) -> None:
        if self._run_log is None or not text:
            return
        try:
            with self._run_log.open("a", encoding="utf-8") as fh:
                fh.write(text)
        except OSError:
            pass

    # ── state ───────────────────────────────────────────────────────────
    def read_state(self) -> IssueState | None:
        """Project the current board, or ``None`` on a transport blip.

        A blip must never be a traceback: the loop just retries next poll.
        """
        try:
            payload = self.fetcher.fetch()
        except Exception as exc:  # noqa: BLE001 — transport, not logic
            self.warn(f"state read failed: {exc}")
            return None
        try:
            return project(payload, self.repo, self.issue, self.config)
        except DriveStateError as exc:
            raise DriveError(str(exc), EXIT_USAGE) from exc

    # ── execution ───────────────────────────────────────────────────────
    def run_coord(self, args: tuple[str, ...], *, serialize_merge: bool = False) -> int:
        """Run a ``coord`` subcommand, echoing its output and appending to the log.

        Output is captured and echoed after the process exits rather than
        streamed through a pipe.  The bash version used ``tee``, whose exit
        code masks the command's (one of the sharp edges #1392 set out to
        remove) — here the return code is unambiguous, and the run log still
        gets every byte.
        """
        argv = [*coord_argv(), *args]
        if self.opts.config_path:
            # Click parses options interspersed with arguments, so appending is
            # safe for every subcommand this driver invokes (all of which carry
            # the shared --config option).
            argv += ["--config", self.opts.config_path]
        if serialize_merge:
            # Merges are serialized on THIS HOST even when the runs themselves
            # are parallel.  #1400 fixed the daemon-side cross-talk (the
            # process-global `redirect_stdout` in POST /merge), so this is now
            # belt-and-braces for same-host callers rather than the only thing
            # preventing fleet-wide cross-talk; it still earns its keep by
            # keeping this host's own queue submissions ordered (two branches
            # rebasing onto a moving main at once is how pile-ups start) and by
            # failing fast locally instead of piling up blocked daemon requests.
            lock = FileLock(scratch_dir() / "merge.lock")
            try:
                lock.acquire(timeout=1800.0)
            except LockBusy:
                self.warn("merge lock timed out after 30m — re-checking next poll")
                return 1
            try:
                return self._spawn(argv)
            finally:
                lock.release()
        return self._spawn(argv)

    def _spawn(self, argv: list[str]) -> int:
        attempt = 1
        while True:
            proc = subprocess.run(
                argv, capture_output=True, text=True, encoding="utf-8", check=False
            )
            combined = (proc.stdout or "") + (proc.stderr or "")
            # #2618: a clean connection refusal (nothing was listening —
            # never a partially-processed request) is the one failure shape
            # safe to retry transparently. Bounded and logged, not silent:
            # an operator reading the pane sees exactly why this command
            # took an extra 20s instead of a drive dying for no visible
            # reason.
            if (
                proc.returncode != 0
                and attempt <= _DAEMON_CONN_REFUSED_RETRIES
                and _looks_like_daemon_connection_refused(combined)
            ):
                self.warn(
                    f"daemon connection refused (attempt {attempt}/"
                    f"{_DAEMON_CONN_REFUSED_RETRIES + 1}, likely a "
                    "coord-serve restart, #2618) — retrying "
                    f"{argv[-1] if argv else '<coord>'} in "
                    f"{_DAEMON_CONN_REFUSED_DELAY_SECS:g}s"
                )
                self.sleeper(_DAEMON_CONN_REFUSED_DELAY_SECS)
                attempt += 1
                continue
            break
        if combined:
            print(combined.rstrip("\n"), file=self.out, flush=True)
        self._append_run_log(combined)  # unbounded — the full bytes, on disk
        # #2274: bounded — this copy is what ends up in a DB column
        # (`drive_queue.last_reason`/`drive_escalations.reason`) via
        # `_drive_exit_summary`, not a log file.
        self._last_run_output = _bounded_tail(combined.strip())
        return proc.returncode

    def run_notify(self) -> None:
        """Nudge ``coord notify`` under the shared lock, to cut timer latency."""
        if not self.opts.notify:
            return
        self.log("nudging: coord notify (flock ~/.coord/notify.lock)")
        lock = FileLock(notify_lock_path())
        try:
            lock.acquire(timeout=300.0)
        except LockBusy:
            self.warn("could not take ~/.coord/notify.lock within 5m — skipping nudge")
            return
        try:
            if self.run_coord(("notify",)) != 0:
                self.warn("coord notify returned non-zero")
        finally:
            lock.release()

    # ── self-heal (#2443) ──────────────────────────────────────────────
    def _self_heal_drift_message(self, label: str, streak: int) -> str | None:
        """Has THIS process's own on-disk ``coord`` install moved since this
        drive session started? ``None`` when it hasn't — or when freshness
        can't be determined at all, which must never be misread as drift —
        the overwhelmingly common case, so the caller just keeps waiting
        exactly as before.

        Only ever consulted once *label* (a WAIT ``Action``'s own reason)
        has repeated *streak* times in a row (``_loop``'s
        ``_SELF_HEAL_WAIT_STREAK`` gate) — this is specifically the #2286
        shape: ``_decide_acceptance_author``'s "could not verify commits
        (git fetch failed), retrying" arm returned the IDENTICAL label every
        ~60s for 2+ hours after the fix that would have resolved it had
        already landed on disk, because Python never reloads an
        already-imported module. A stale in-memory `coord/drive.py` has no
        way to notice that on its own — this is the bounded check that lets
        it notice anyway.
        """
        current = self.self_head_probe()
        if not current or not self._start_head_sha or current == self._start_head_sha:
            return None
        return (
            "code changed underneath this session — on-disk coord moved "
            f"from {self._start_head_sha[:8]} to {current[:8]} while stuck "
            f"repeating the same wait {streak}x: {label!r}. Exiting "
            "(EXIT_SELF_STALE) so coord drive-queue relaunches with "
            f"current code instead of continuing to wait on stale logic (#2443)."
        )

    def _post_escalation_comment(self, state: IssueState, message: str) -> None:
        """#1526: durably surface an escalation's reason onto the issue
        itself, not just this run's tmux pane and the ``coord escalate``
        board row.

        Best-effort and never raises: a failed post must not mask the
        escalation that already happened via the exit code (``EXIT_
        ESCALATED``) and the board record `action.command` just wrote — the
        two durable channels this already has. This is a THIRD channel, not
        a replacement for either.
        """
        if not state.repo_github:
            return
        try:
            from coord import github_ops  # noqa: PLC0415

            github_ops.post_issue_comment(
                state.repo_github,
                state.issue,
                "🚧 **`coord drive` escalated — a human decision is needed.**\n\n"
                f"{message}\n\n"
                f"Run it: `coord escalate run {state.repo} {state.issue}`\n"
                f"Dismiss it: `coord escalate dismiss {state.repo} {state.issue}`",
            )
        except Exception as exc:  # noqa: BLE001 — best-effort, never mask the exit
            self.warn(f"could not post the escalation comment to GitHub: {exc}")

    # ── audit boundaries (#1499) ────────────────────────────────────────
    def _record_drive_audit(
        self,
        event_type: str,
        summary: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Emit a ``category="drive"``, ``actor="drive"`` audit row.

        ``coord.audit.record_audit`` is itself best-effort (never raises into
        the caller — disk-full/locked-DB/schema-drift are swallowed there),
        so this needs no try/except of its own: a broken audit_log must never
        take down the drive loop.
        """
        from coord.audit import record_audit  # noqa: PLC0415

        record_audit(
            tier="business",
            category="drive",
            event_type=event_type,
            actor="drive",
            summary=summary,
            repo=self.repo,
            issue=self.issue,
            details=details,
        )

    def _drive_exit_summary(
        self, exit_code: int | None, exc: BaseException | None
    ) -> tuple[str, dict[str, Any]]:
        """Human summary + machine details for the terminating ``drive_exited``
        row — the piece that answers "what did the driver do, and why did it
        stop?" retroactively, after the tmux session (if any) is long gone."""
        ident = f"{self.repo}#{self.issue}"
        if exc is not None:
            if isinstance(exc, DriveError):
                summary = f"drive exited for {ident}: {exc} (exit_code={exc.exit_code})"
                return summary, {"exit_code": exc.exit_code, "error": str(exc)}
            summary = f"drive exited for {ident}: unexpected error ({exc!r})"
            return summary, {"exit_code": None, "error": repr(exc)}
        # Non-exceptional terminal exit — decide() returned a `_die(...)` (or
        # `_succeed(...)`) Action directly (`_loop`'s `action.is_exit` branch
        # returns the code without raising). `_last_exit_message` carries that
        # Action's own `message` — the same text this run's log already
        # printed via self.log/self.warn — so the audit row narrates WHY,
        # not just the bare exit code.
        reason = self._last_exit_message.strip()
        if not reason:
            reason = {EXIT_OK: "ok", EXIT_DEADLINE: "deadline exceeded"}.get(
                exit_code, f"exit_code={exit_code}"
            )
        summary = f"drive exited for {ident} (exit_code={exit_code}): {reason}"
        return summary, {"exit_code": exit_code, "reason": reason}

    # ── the loop ────────────────────────────────────────────────────────
    def run(self) -> int:
        scratch = scratch_dir()
        self._run_log = scratch / f"{self.repo}-{self.issue}.log"

        # PER-ISSUE lock.  Two drivers on DIFFERENT issues are fine; two on the
        # SAME issue are not — they would double-dispatch work and
        # double-record verdicts.
        lock = FileLock(scratch / f"lock-{self.repo}-{self.issue}")
        holder = scratch / f"holder-{self.repo}-{self.issue}"
        try:
            lock.acquire(timeout=0.0)
        except LockBusy:
            try:
                who = holder.read_text(encoding="utf-8").strip()
            except OSError:
                who = "another run"
            # No `drive_started`/`drive_exited` pair here — this run never
            # actually started (another driver already holds the per-issue
            # lock), so there is nothing new to narrate in the audit log.
            raise DriveError(
                f"already driving {self.repo} #{self.issue} ({who}).\n"
                "   A second driver on the SAME issue would double-dispatch work.\n"
                "   Other issues can be driven concurrently.\n"
                f"   Lock file: {lock.path}",
                EXIT_USAGE,
            ) from None
        try:
            holder.write_text(
                f"{self.repo} #{self.issue} (pid {os.getpid()})\n", encoding="utf-8"
            )
        except OSError:
            pass
        self._record_drive_audit(
            "drive_started", f"drive started for {self.repo}#{self.issue}"
        )
        # #1606: a start marker, written the instant the loop legitimately
        # begins — independent of whether a RUN action (`_spawn`, the only
        # other writer of this file) ever fires. `decide()`'s very first
        # branch after "merged" is a pure WAIT with no command whenever
        # another assignment is already active (coord/drive.py `decide()`),
        # so a drive attached to healthy in-flight work could sit
        # alive-but-log-silent for its entire first `--poll` interval
        # (default 60s). `launch_drive_in_tmux`'s post-launch verification
        # only waits ~8s for the log to grow, so without this marker it
        # would misdiagnose that ordinary, majority-case attach as a stuck
        # loop and kill a perfectly healthy session. This line makes "the
        # log grew" mean "the loop started", not "a subprocess happened to
        # run first".
        self._append_run_log(
            f"{self._stamp()}  drive loop started for {self.repo}#{self.issue}\n"
        )
        try:
            exit_code = self._loop()
        except BaseException as exc:  # noqa: BLE001 — narrate every exit, then re-raise unchanged
            summary, details = self._drive_exit_summary(None, exc)
            self._record_drive_audit("drive_exited", summary, details=details)
            raise
        else:
            summary, details = self._drive_exit_summary(exit_code, None)
            self._record_drive_audit("drive_exited", summary, details=details)
            return exit_code
        finally:
            try:
                holder.unlink()
            except OSError:
                pass
            lock.release()

    def _loop(self) -> int:
        state = self.read_state()
        if state is None:
            raise DriveError("could not read board state", EXIT_USAGE)

        # #1466: probe ONCE here (not per-poll) — the underlying `claude -p
        # "/usage"` call is itself cached ~60s (coord.usage_limits), but
        # there's no reason to re-shell-out every loop iteration for a
        # decision only made at the top of the run. Skipped entirely when
        # the gate is off, so a `disabled` config never pays the subprocess
        # cost.
        usage_limits = (
            self.usage_prober() if self.config.usage_gate.mode != "disabled" else None
        )
        pre = preflight(state, self.opts, self.config, usage_limits=usage_limits)
        machine = pre.machine

        # #1453: resolved ONCE here (not per-poll) — the gate_checker inside
        # costs a GitHub fetch and a milestone's Gate-A status can't change
        # mid-run. Threaded unchanged into every decide() call below.
        oracle = resolve_oracle_decision(state, self.opts, self.config, self.oracle_gate)

        self.log(f"driving {self.repo} #{self.issue}")
        self.log(f"  machine        : {machine}")
        if not self.opts.machine and state.picked_machine_provider_reason:
            # #1906: only meaningful for an AUTO-picked machine — an
            # explicit `--machine` never ran this selection's provider
            # resolution (it wins outright; #1711's dispatch-time guard is
            # the enforcement for it). Mirrors `coord assign --dry-run`'s
            # own "provider: ..." line (`describe_provider_choice`).
            self.log(f"  provider       : {state.picked_machine_provider_reason}")
        if not self.opts.machine and state.picked_machine_pause_error:
            # #2807: the pause set couldn't be read, so the auto-pick above
            # fell back to treating it as "nothing is paused" (fail-open,
            # matching every other `coord.machine_pause.paused_set()`
            # reader) — surfaced loudly here rather than an operator's
            # `coord pause` silently stopping enforcing.
            self.warn(
                "pause set unreadable, routing may include a paused "
                f"machine: {state.picked_machine_pause_error}"
            )
        self.log(f"  acceptance     : {oracle.reason}")
        self.log(
            f"  test command   : {state.repo_test_command or '<none configured>'} "
            "(coord dispatches this itself — #1426; this observes)"
        )
        self.log(
            "  merge          : "
            + (f"yes ({self.opts.merge_method})" if self.opts.do_merge else "no")
        )
        self.log(
            "  auto-loop      : "
            + (
                "on (request-changes → this driver runs coord fix, #1692)"
                if state.auto_loop
                else "off (a request-changes verdict stops this run)"
            )
        )
        self.log(
            f"  fix rounds     : {self.opts.max_fix_rounds} this run, shared by "
            "the test and review arms (via coord fix)"
        )
        self.log(
            f"  review fix cap : {state.max_review_iterations} "
            "(pipeline.max_review_iterations — per issue, across every drive)"
        )
        self.log(
            "  notify shellout: "
            + (
                "on (this drive also calls `coord notify` on a stall)"
                if self.opts.notify
                else "off (relying on the 5-min coord-notify.timer; stall "
                "state is still recorded for it either way)"
            )
        )
        self.log(f"  log            : {self._run_log}")
        for warning in pre.warnings:
            self.warn(warning)

        if self.opts.dry_run:
            self.log("current state:")
            print(
                json.dumps(state.as_flat_dict(), indent=2, default=str),
                file=self.out,
                flush=True,
            )
            return EXIT_OK

        counters = DriveCounters()
        start = self.clock()
        deadline = start + self.opts.deadline_secs
        last_fingerprint = ""
        last_change = start
        # #1593: the nudge cadence is tracked SEPARATELY from `last_change`.
        # The one-shot latch (`nudged = False`/`True`, cleared only on a
        # fingerprint change) let a stage that stalls for 30-40 real minutes
        # get exactly one nudge near the start, then go completely silent —
        # `coord notify` correctly finds nothing to settle while the worker
        # is still running, and nothing ever re-checks after that. Re-nudging
        # every `stall_secs` while the fingerprint stays put, without
        # resetting `last_change`, keeps the staleness clock honest (so
        # `--stall` measures real elapsed idle time, not time-since-last-
        # nudge) while guaranteeing a stalled stage is never more than one
        # `stall_secs` window away from a fresh check.
        last_nudge: float | None = None
        # #2443: this session's own on-disk `coord` HEAD, captured once,
        # right as the poll loop begins — see `_self_heal_drift_message`.
        self._start_head_sha = self.self_head_probe()
        # A distinct object (never equal to any real `Action.label`, empty
        # string included) so the very first WAIT of the run always starts
        # its streak at 1 rather than accidentally matching an unset
        # sentinel.
        last_wait_label: object = object()
        same_wait_streak = 0

        while True:
            now = self.clock()
            if now > deadline:
                self._last_exit_message = (
                    f"deadline of {self.opts.deadline_mins:g}m exceeded"
                )
                self.warn(self._last_exit_message)
                if state is not None:
                    print(
                        json.dumps(state.as_flat_dict(), indent=2, default=str),
                        file=self.err,
                        flush=True,
                    )
                return EXIT_DEADLINE

            state = self.read_state()
            if state is None:
                self.sleeper(self.opts.poll)
                continue

            fingerprint = state.fingerprint
            # #2649: the flat `--stall` value is a false-positive magnet for
            # stages (`work`, the `type="smoke"` Test-stage) whose normal
            # duration on this fleet routinely exceeds it — see
            # `PipelineConfig.stall_threshold_secs`/`_DEFAULT_STALL_THRESHOLDS`
            # for the measured evidence. Recomputed every tick (not cached)
            # because `state.active_types` changes as the pipeline advances
            # through stages, each with its own threshold.
            stall_secs = self.config.pipeline.stall_threshold_secs(
                state.active_types, default_secs=self.opts.stall_secs
            )
            if fingerprint != last_fingerprint:
                last_fingerprint = fingerprint
                last_change = now
                last_nudge = None
                # #2648: the pipeline just advanced, so any nudge published
                # for the PREVIOUS state is stale — retract it so a later
                # leg's own notifier probe (a new assignment, a new ledger
                # subject) does not inherit an alarm that belongs to a stage
                # which already finished. A no-op if nothing was ever
                # nudged; advisory either way.
                _clear_stall_nudge(self.repo, self.issue)
                self.log(
                    f"state: work={state.work_status or '-'} "
                    f"test={state.work_test_state or '-'} "
                    f"review={state.review_status or '-'}/"
                    f"{state.review_verdict or '-'} "
                    f"iter={state.work_review_iter} "
                    f"merge={state.merge_status or '-'}"
                    # #1526: print the merge gate's OWN reason right next to
                    # its status — this is the line the 2026-07-27/28 stalls
                    # never carried, so a "test=passed" operator watching the
                    # pane had no way to see `coord merge` disagreeing until
                    # it had already burned the whole retry budget.
                    + (f" ({state.merge_reason})" if state.merge_reason else "")
                    + f" active={state.active_count}"
                    # #2079: while the oracle slice is landing, every field
                    # above is empty by construction (the work row does not
                    # exist yet) — so without this the one line that is
                    # supposed to narrate progress narrated nothing at all
                    # for hours. Only printed when a slice row exists, so a
                    # normal drive's line is byte-for-byte unchanged.
                    + (
                        f" | slice={state.acceptance_author_status or '-'}"
                        f" test={state.acceptance_author_test_state or '-'}"
                        f" review={state.acceptance_review_verdict or '-'}"
                        f" merge={state.acceptance_merge_status or '-'}"
                        + (
                            f" ({state.acceptance_merge_reason})"
                            if state.acceptance_merge_reason
                            else ""
                        )
                        if state.acceptance_author_aid
                        else ""
                    )
                )
            elif now - last_change > stall_secs and (
                last_nudge is None or now - last_nudge > stall_secs
            ):
                # #1593: re-nudge on a `stall_secs` cadence for as long as the
                # fingerprint stays put, instead of firing once and going
                # silent. `last_change` is deliberately left untouched here —
                # only `last_nudge` advances — so the elapsed time below (and
                # `--stall`'s own monotonicity: smaller stall never means
                # FEWER nudges) keeps reflecting genuine staleness rather than
                # resetting every time this branch fires.
                self.warn(
                    f"no state change in {(now - last_change) / 60.0:g}m "
                    f"({','.join(state.active_types) or 'nothing'} active)"
                )
                self.run_notify()
                last_nudge = now
                # #1632: publish the nudge so the phone notifier can ask
                # whether the stall SURVIVED it, without defining "stalled"
                # a second time. `now` here is `self.clock` (monotonic and
                # process-local), so stamp the record with wall-clock time —
                # a different process reads this file. Advisory: a failure
                # to record must never affect the drive, which is why
                # `record_nudge` swallows everything internally.
                _publish_stall_nudge(
                    self.repo, self.issue, stalled_for=now - last_change
                )

            action = decide(
                state, self.opts, counters, self.verifier,
                machine=machine, oracle=oracle, gate_checker=self.oracle_gate,
            )
            for warning in action.warnings:
                self.warn(warning)

            # #2871: `decide()` stays a pure function of its inputs — it
            # cannot write the audit log itself — so an Action that carries
            # its own audit note (e.g. bypassing a stale pre-dispatch
            # refusal) gets recorded here, the one place this loop already
            # writes durable `drive_*` audit rows.
            if action.audit_event is not None:
                event_type, summary, details = action.audit_event
                self._record_drive_audit(event_type, summary, details=details)

            # #2443: self-heal — see `_self_heal_drift_message`. Tracked off
            # the Action's own `label`, NOT `fingerprint` above: a WAIT can
            # (and, in the #2286 shape, does) repeat the identical reason
            # poll after poll while the board fingerprint itself is static
            # for the same underlying reason, so this is really the same
            # signal — but keying directly on what the loop is ABOUT TO DO
            # keeps this correct even for a WAIT arm whose label happens to
            # be static text unrelated to fingerprint (e.g. "TEST: in
            # progress on a capability-matched machine") without needing to
            # reason about the two staying in lockstep. A non-WAIT action
            # (RUN or EXIT) always resets the streak: dispatching something
            # is progress by definition.
            if action.kind == WAIT:
                if action.label == last_wait_label:
                    same_wait_streak += 1
                else:
                    last_wait_label = action.label
                    same_wait_streak = 1
                if same_wait_streak >= _SELF_HEAL_WAIT_STREAK:
                    drift = self._self_heal_drift_message(
                        action.label, same_wait_streak
                    )
                    if drift:
                        self._last_exit_message = drift
                        self.warn(drift)
                        return EXIT_SELF_STALE
            else:
                last_wait_label = object()
                same_wait_streak = 0

            if action.is_exit:
                # #1499: capture the exit reason for the audit boundary before
                # anything below can fail — the escalation write is explicitly
                # best-effort, so it must not be able to cost us the reason.
                self._last_exit_message = action.message
                # #1505: an escalation exit still carries a `command` — the
                # `coord escalate record ...` write that makes the stop
                # reason board-visible after this process is gone. Run it
                # HERE (the I/O shell), not inside `decide()`, which stays a
                # pure function like every other decision in this module.
                # Best-effort: a failed write must never block the exit
                # itself (there is nothing left to retry), so this only
                # warns, never raises.
                if action.command:
                    rc = self.run_coord(action.command)
                    if rc != 0:
                        self.warn(
                            action.error_message
                            or f"coord {' '.join(action.command)} exited {rc}"
                        )
                # #1526: an escalation's reason must reach the issue itself,
                # not just this tmux pane (gone the moment the session ends)
                # and the `coord escalate` board row (invisible unless an
                # operator thinks to run `coord escalate list`). This is what
                # turned "drive died without closing the issue" into three
                # unexplained deaths during the 2026-07-27/28 overnight run.
                # #2019 rides the same rail: a dead end is exactly the
                # "nobody is coming" case this comment exists for, and the
                # tmux pane it would otherwise be trapped in dies with the
                # session.
                if action.exit_code in (EXIT_ESCALATED, EXIT_DEAD_END):
                    self._post_escalation_comment(state, action.message)
                # #2712: `self.log`/`self.warn` only reach `self.out`/
                # `self.err` (the tmux pane for a `--tmux` drive), which is
                # destroyed the instant the session exits — the exact moment
                # this branch runs. Without also appending to `_run_log`
                # (`scratch_dir()/<repo>-<issue>.log`, the file that survives
                # the pane), a `_die(...)` exit's explanation — including
                # `merge attempted N times without landing` and its captured
                # diagnostic — is recorded nowhere: the log simply stops
                # after the last narrated action with no reason given, which
                # reads as "stalled" rather than "failed".
                prefix = "" if action.exit_code == EXIT_OK else "!! "
                self._append_run_log(
                    "".join(
                        f"{self._stamp()}  {prefix}{line}\n"
                        for line in action.message.splitlines()
                    )
                )
                if action.exit_code == EXIT_OK:
                    for line in action.message.splitlines():
                        self.log(line)
                else:
                    for line in action.message.splitlines():
                        self.warn(line)
                return action.exit_code

            if action.label:
                self.log(action.label)

            if action.kind == RUN:
                rc = self.run_coord(
                    action.command, serialize_merge=action.serialize_merge
                )
                if action.serialize_merge:
                    # #2078: `serialize_merge` marks a real `coord merge
                    # --only <aid>` attempt — `_decide_merge`'s bounded
                    # bottom-of-function retry, and (#2814) its budget-exempt
                    # CI-pending re-check — so this is the merge attempt's
                    # own diagnostic, captured for `_decide_merge` to read
                    # back (via `counters.last_merge_diagnostic`) on the NEXT
                    # poll: to avoid a blind retry once a real gate block is
                    # already known, and to name it in the give-up message
                    # instead of the board's empty fields.
                    #
                    # #2079: `_decide_merge` now has two callers — the work
                    # row and the oracle JIT slice — with one budget each, so
                    # the diagnostic is filed against the budget that actually
                    # spent the attempt. Cross-filing it would diagnose one PR
                    # using the other PR's gates.
                    budget = (
                        counters.slice_budget()
                        if action.merge_scope == "acceptance"
                        else counters
                    )
                    budget.last_merge_diagnostic = self._last_run_output
                if rc != 0:
                    # #1844: `coord assign`/`coord approve-plan` exits this
                    # SAME code (see EXIT_DISPATCH_REFUSED's docstring) only
                    # when a pre-dispatch guard refused deterministically —
                    # never for a transient failure. That refusal's own
                    # message (the guard's remedy, verbatim) is what the
                    # child just printed to stdout/stderr, captured above by
                    # `_spawn` into `_last_run_output`; `action.error_message`
                    # is a STATIC string chosen when the Action was built and
                    # cannot carry it. Re-raising with the SAME exit code (not
                    # EXIT_TERMINAL_FAILURE) is what lets `_drive_exit_summary`
                    # and, downstream, `coord/drive_queue.py`'s tick tell this
                    # refusal apart from a genuine crash.
                    if rc == EXIT_DISPATCH_REFUSED:
                        msg = self._last_run_output or action.error_message or (
                            f"coord {' '.join(action.command)} refused "
                            f"(exit {rc})"
                        )
                        raise DriveError(msg, EXIT_DISPATCH_REFUSED)
                    # #2274: a non-refusal failure used to discard the
                    # child's captured stdout+stderr entirely — the message
                    # was `action.error_message` (a static string chosen
                    # when the Action was built) or, absent that, a bare
                    # "coord assign ... exited 1" with zero diagnostic
                    # content. `_last_run_output` (bounded, see
                    # `_bounded_tail`) is the ONLY place the real reason a
                    # subprocess like `coord assign` failed still exists
                    # once it has exited — append it so the DriveError's own
                    # message (what `_drive_exit_summary` folds into the
                    # `drive_exited` audit summary, and what
                    # `coord/drive_queue.py`'s tick copies verbatim into
                    # `last_reason`/`drive_escalations.reason`) is actually
                    # actionable rather than a status with no reason.
                    base_msg = action.error_message or (
                        f"coord {' '.join(action.command)} exited {rc}"
                    )
                    msg = (
                        f"{base_msg}\n   output: {self._last_run_output}"
                        if self._last_run_output
                        else base_msg
                    )
                    if action.on_error == "warn":
                        self.warn(msg)
                    else:
                        raise DriveError(msg, EXIT_TERMINAL_FAILURE)

            self.sleeper(
                self.opts.poll if action.sleep_after is None else action.sleep_after
            )
