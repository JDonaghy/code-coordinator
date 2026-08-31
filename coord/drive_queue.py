"""Pure decision half of the ``coord drive`` queue (#1754, DQ-2).

Phase B of #1750.  DQ-1 gave the queue a home (``drive_queue``, one row per
``(repo, issue)``, dense 0-based ``position``, an ``after_json`` pre-req list
nothing interpreted yet).  This module is what interprets it: given the queue
rows, a typed projection of the board, and a concurrency ceiling, it returns a
:class:`TickPlan` — what to reconcile, the ONE entry to launch, what to block,
what merely deferred and why, and the single queue-level alert.

STRUCTURE — the split is copied verbatim from ``coord/drive.py``, for the
reason that module's docstring gives: *"every bug the bash version shipped was
in the decision half, which is why that half is where the tests are."*  Nothing
in this file runs a subprocess, opens a socket, touches the DB, or reads the
clock.  ``coord/commands/drive_queue.py`` is the thin I/O shell that fetches,
calls :func:`plan_tick`, and executes what comes back.  #1794 needs wall-clock
age, so the clock is *passed in* (``plan_tick(..., now=time.time())``) rather
than read here — the rule is "no ambient state", not "no time".

TWO RULES THIS FILE EXISTS TO ENFORCE, both learned the hard way:

1. **Capacity comes from BOARD STATE, not from a session count.**  ``coord
   drive`` returns ``EXIT_DEADLINE`` (3) when the *observer* gives up; the
   worker, test and review keep running on the fleet (#1660).  Such a drive is
   invisible to ``coord drive-sessions`` but is still occupying a machine.  So
   an entry occupies capacity when its tmux session is alive **or** it still
   has a live work-like assignment on the board — see
   :func:`_reconcile_running`.  Getting this wrong reproduces the 2026-08-01
   incident where a sequential batch became concurrent on the fleet.

2. **Typed state, never CLI prose** (#1523 §2).  Everything here reads dicts
   that came off ``GET /board`` and ``coord drive-sessions --json``.  Both bugs
   in the ad-hoc overnight sequencer were prose-parsing and both failed
   *silently*.

DELIBERATELY NOT HERE: auto-demotion.  A deferral increments a counter and
records a reason; it never reorders the queue (see #1750's design note).  The
head of the queue stays the head until an operator moves it.

#1757 (DEPLOY GATES) adds a third rule: **merged is not live.**  An entry may
be marked ``--hold-after``, and when the tick transitions THAT entry to
``done`` the gate fires and holds its DEPENDENTS — until a human deploys and
releases it — even though the board now shows the gated entry itself as
landed. That is not a niche case; it is the shape of every change here that
crosses a deploy lane (``docs/OPERATING_GOTCHAS.md`` opens with the matrix).
A queue that models merge but not deploy would confidently sequence work into
that trap overnight. The gate's decision half is :func:`plan_tick`'s hold
resolution below; running the optional ``resume_when`` probe is the shell's
job, and its result comes back in as data (:class:`ProbeResult`) so this file
stays pure.

#2186 (GATE SCOPE) narrows the blast radius of a fired gate to **its own
entry's dependents by default.**  Before this, ANY fired gate stopped the
ENTIRE tick — nothing else in the queue was even evaluated, whatever repo it
was in or however unrelated it was to the gated entry.  On 2026-08-13 that
turned one issue's deploy dependency into an 8-hour fleet-wide idle: three
machines, zero attempts on four unrelated entries, while the actual
successor of the gate (the one entry that legitimately had to wait) was the
only thing that needed to.  A gate now defaults to :data:`HOLD_SCOPE_ENTRY`:
it keeps holding any entry whose own ``after=`` names the gated key (via
:func:`_resolve_prereqs`), and every other entry in the queue is walked and
launched normally in the SAME tick.  The old whole-queue stop is still
expressible — :data:`HOLD_SCOPE_FLEET`, ``--scope=fleet`` at ``add`` time —
for the genuine case (a rename, a schema migration) where nothing anywhere
should launch until a human clears it, but it is opt-in, not the default.

#2273 (RETRY SPACING) is the DISPATCH-site sibling of #1891/#1892/#2252:
those taught the queue that a missing MERGE verdict is not a failed one; this
teaches it that two attempts fired minutes apart is not a retry policy
against a transient DISPATCH failure, it is two samples of the same short
window. On 2026-08-15 quadraui#508 and coord-portal#83 each burned their
entire attempt budget inside ~6 minutes, and a hand re-run of the identical
``coord assign`` command 18 minutes later succeeded first try. Before this, a
died launch's next attempt was paced ONLY by tick cadence — see
:func:`_retry_backoff_reason`, which now enforces real wall-clock spacing
(:data:`RETRY_BACKOFF_SECONDS`) before a `retry`-reconciled entry is eligible
to launch again, widened further (:data:`DISPATCH_FAILURE_MIN_BACKOFF_SECONDS`)
when the died launch never produced a board-visible assignment at all
(:func:`_dispatch_produced_nothing`) — the cheap, already-recorded
approximation of "this was infrastructure, not code" the full classification
(blocked on a stderr-capture prerequisite this queue does not have yet)
cannot give directly. See ``RETRY_BACKOFF_SECONDS``'s own comment for the
full incident writeup and why this is additive to #1794's grace window, not
a replacement for it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Sequence

from coord.drive_state import TERMINAL_STATUSES, WORK_LIKE
from coord.gate_a import is_gate_a_refusal_reason
from coord.github_ops import is_throttle_skip_reason, parse_throttle_skip_until
from coord.issues_sync_status import STALENESS_WARN_SECONDS as ISSUE_CACHE_STALE_CEILING_S
from coord.merge_queue import (
    CI_STALE_PREFIX,
    PLAN_READY,
    ci_rollup_all_clear,
    is_ci_flaky_reason,
    is_ci_infra_reason,
    is_ci_terminal_reason,
    is_ci_unreadable_reason,
    is_stale_smoke_reason,
)
from coord.models import is_merge_landed_reason, is_policy_refusal_reason

# ── queue states ─────────────────────────────────────────────────────────────
#
# `waiting` and `running` are the live states; `done`/`blocked`/`failed` are
# terminal and stay in the table until an operator removes them, so
# `coord drive-queue list` doubles as a short run history (coord/db.py's
# drive_queue comment states that contract).
#
# #2230 qualifies "terminal" for `blocked` specifically: it still means "no
# `coord drive` launches from this row again on its own" and "stays in the
# table for history" for a PERMANENT cause (#1844/#2019) or one the sweep has
# no evidence about. It no longer means "nothing ever writes to this row
# again" — a `blocked` entry whose cause was a re-evaluable gate reading may
# be moved straight back to `waiting` by `plan_tick`'s own reconcile pass,
# with no operator action, the moment that reading clears. See
# `is_permanent_block_reason` and `_reconcile_blocked` below for exactly
# which `blocked` rows still get the old, fully-terminal treatment.

STATE_WAITING = "waiting"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_BLOCKED = "blocked"
STATE_FAILED = "failed"
# #1891: a drive that died while ITS OWN issue's merge was refused for
# nothing stronger than "CI checks have not reported yet" (see
# `coord.merge_queue.is_ci_pending_reason`) — as opposed to genuinely dead,
# genuinely refused, or genuinely out of attempts. Deliberately NOT in
# `TERMINAL_QUEUE_STATES`: unlike `blocked`, this is not a state an operator
# must release — `plan_tick` re-checks it every tick (see the pre-pass in
# `plan_tick`) and flips it straight back to `waiting` — without spending an
# attempt — the moment the board shows the gate has cleared. The whole
# feature this state exists for is "one GitHub Actions outage costs zero
# interventions", so a queue read (`coord drive-queue list`/`status`) must
# render it distinctly from both `waiting` (nothing wrong) and `blocked`
# (needs a human) — see `_STATE_ORDER` in `coord/commands/drive_queue.py`.
#
# #1892 extends the SAME state to a second trigger:
# `coord.merge_queue.is_ci_infra_reason` — a CI verdict that DID arrive but
# said nothing about the code (never assigned a runner, or died before
# checkout). There the "more real time" that un-parks the entry is the
# in-flight auto-rerun (`MAX_CI_INFRA_RERUNS`) landing, not a verdict that
# simply hasn't shown up yet — but the queue-level treatment is identical:
# relaunching a fresh `coord drive` right now would just observe the same
# rerun-in-progress and wait again, so this parks instead of spending an
# attempt. See `build_board_view`'s population of `merge_ci_pending` below.
#
# #2252 extends the SAME state to a THIRD trigger:
# `coord.merge_queue.is_ci_flaky_reason` — a CI verdict that DID arrive AND
# said something real about the code, but has only been observed failing
# ONCE so far. The "more real time" that un-parks the entry is the one
# scoped re-run (`MAX_CI_FLAKY_RERUNS`) landing — a coin-flip flake clears
# and the entry resumes having spent zero attempts, a confirmed-real failure
# reverts to a plain `checks_failed` block (spends the attempt exactly like
# today) on the very next tick. Same reasoning as #1892 for why this parks
# rather than spends: relaunching right now would just observe the same
# re-run-in-progress and wait again.
#
# #2347 extends the SAME state to a FOURTH trigger:
# `coord.merge_queue.is_ci_unreadable_reason` — the check-list FETCH itself
# failed (GitHub unreachable: a transient `gh pr checks` HTTP 5xx, an auth
# blip), not a CI verdict of any shape. The "more real time" that un-parks
# the entry is simply GitHub answering again on a later read — there is no
# in-flight rerun to wait on, unlike #1892/#2252, only another attempt to
# read. Same queue-level treatment as the other three: relaunching right now
# would just observe the identical transport failure and wait again.
STATE_PARKED = "parked"

TERMINAL_QUEUE_STATES: frozenset[str] = frozenset(
    {STATE_DONE, STATE_BLOCKED, STATE_FAILED}
)

# #2158: how long a `parked` entry may hold a CI reading that CANNOT refresh
# itself before the tick stops believing it and resumes the entry to
# `waiting`.
#
# Only unrefreshable readings age out — `IssueFacts.merge_ci_pending_live`
# is the discriminator. A park founded on the live `merge_plan` row's own
# reason is re-derived on every board build and goes false by itself the
# moment CI reports; that one is held for as long as it keeps saying so, with
# no ceiling. A park founded ONLY on the raw `merge_queue` row's persisted
# `error` has no read-path writer at all: that string is written by a live
# `coord merge` attempt and by nothing else, and a parked entry by definition
# runs none. Left alone it is a permanent verdict — claude-coordinator#2138
# sat parked 7h25m on CI that had been green since 41 seconds BEFORE the park
# was written, and only moved when an unrelated merge happened to rewrite the
# board.
#
# Failing OPEN after the ceiling is the safe direction. Resuming spends no
# attempt (that is #1891's whole design point) and asserts nothing about CI —
# it just returns the entry to the walk, which re-checks every gate it owns.
# If CI really is still running, the relaunched drive observes that and waits
# on it exactly as the original one did, and the worst case is one relaunch
# per ceiling-length. The opposite failure — trusting a frozen string forever
# — costs an entry that is mergeable NOW and has no other actor: `coord merge`
# does not run itself (`merge.auto_drain` is off by design since the
# 2026-06-07 token-burn incident) and the drive that could have merged it has
# already exited.
#
# 45 minutes is several times the ~10-minute CI runtime on this fleet, so a
# genuinely-pending park is never cut short in practice; it is a backstop for
# the unrefreshable case, not a second CI timeout.
PARK_STALE_SECONDS = 45 * 60.0

# ── `blocked` reconciliation (#2230) ─────────────────────────────────────────
#
# `blocked` used to be genuinely terminal: nothing ever asked again whether
# the condition that blocked an entry had since cleared, even when it plainly
# had. quadraui#309 sat `blocked attempts=2` for ~11h while `coord gates
# quadraui 309` read `merge: READY` for most of that window — the driver had
# exhausted its attempts against a gate reading that was, by the time anyone
# looked, no longer true. #1616 names the general shape: a stage that stops on
# a transient condition and is never re-examined stays stopped forever.
#
# NOT EVERY `blocked` REASON IS RE-EVALUABLE, and a sweep that cannot tell the
# two apart is worse than the terminal state it replaces — it re-burns
# attempts on an entry that provably cannot change and buries the real,
# recoverable entries in churn. #1844 already drew this exact line for a
# DIFFERENT queue transition (a permanent pre-dispatch guard refusal skips
# straight to `blocked` WITHOUT spending an attempt, because nothing about
# waiting and relaunching can change a deterministic refusal); #2019 rides the
# same branch for a dead-end row. Both stamp a recognisable marker into their
# own `last_reason` — see `_reconcile_running`'s `refused`/`dead_end`
# branches — which is what :func:`is_permanent_block_reason` below reuses
# rather than inventing a second classification.
#
# Everything else that reaches `blocked` — overwhelmingly `exhausted`, a drive
# that died `max_attempts` times in a row for whatever reason, #309's shape
# exactly — is a CANDIDATE for re-checking, never a guarantee: the sweep only
# ever acts on POSITIVE evidence that the entry's own merge gate now reads
# clear (see `_reconcile_blocked` and the `live_blocked_gate` parameter of
# `plan_tick`). No evidence either way leaves the entry exactly as untouched
# as it was before this feature existed.
_PERMANENT_BLOCK_MARKERS: tuple[str, ...] = ("(#1844)", "(#2019)")


def is_permanent_block_reason(text: str | None) -> bool:
    """Whether *text* names a PERMANENT cause of `blocked` — #2230's sweep
    must never re-check these; relaunching cannot change either outcome.

    Marker-based, the same convention `coord.gate_a.is_gate_a_refusal_reason`
    uses for the analogous Gate-A classification: cheap, and correct even
    though neither `_reconcile_running` branch persists a typed "why" column
    of its own — the prose those two branches write into `last_reason` is the
    only durable record a later tick has to go on.
    """
    if not text:
        return False
    return any(marker in text for marker in _PERMANENT_BLOCK_MARKERS)


# How many times #2230's sweep may resume the SAME blocked entry back to
# `waiting` before it stops trying and leaves the entry blocked for an
# operator. Without a ceiling, an entry whose gate reading itself flaps (CI
# genuinely green, then genuinely red, then green again; a live re-check
# racing an in-flight `coord merge` attempt) would oscillate blocked/waiting
# forever — spending a fresh `coord drive` launch each cycle and burying any
# real signal in churn, exactly the failure mode the issue warns a naive
# "retry everything" sweep would create. 3 is deliberately small: a gate that
# clears and then reblocks three separate times is itself the interesting
# fact, not something more retries will resolve — see `QueueEntry.resumes`
# and the `oscillating` reconcile outcome `_reconcile_blocked` produces once
# the ceiling is reached.
MAX_BLOCKED_RESUMES = 3

# ── deploy-gate states (#1757) ───────────────────────────────────────────────
#
# `hold_state` is the gate's LIFECYCLE, orthogonal to the entry's queue
# `state`.  A gate is `armed` from the moment the operator declares it
# (`coord drive-queue add --hold-after`, written by `enqueue_drive_queue`),
# `fired` the tick the entry reaches `done`, and `released` once a human ran
# `coord drive-queue resume` or the entry's `resume_when` probe exited 0.
# `''` means the entry carries no gate at all.
#
# The queue is held for exactly as long as SOME entry sits at `fired` — the
# release, not the entry leaving the queue, is what unblocks the successors.
HOLD_NONE = ""
HOLD_ARMED = "armed"
HOLD_FIRED = "fired"
HOLD_RELEASED = "released"

# ── deploy-gate scope (#2186) ────────────────────────────────────────────────
#
# ORTHOGONAL to `hold_state` above: `hold_state` is WHETHER the gate is
# currently closed, `hold_scope` is WHAT it closes when it is.
#
# `entry` (the default) is the narrow, correct-by-default reading: the gate
# holds only entries that name the gated key in their OWN `after=` — the
# actual dependents, resolved by `_resolve_prereqs` below. Everything else in
# the queue is evaluated and can launch in the same tick.
#
# `fleet` is the pre-#2186 behaviour, kept available for the genuine
# whole-fleet case (a rename, a schema migration) where nothing anywhere may
# launch until a human clears it — declared explicitly (`--scope=fleet` at
# `add` time), never the default, because the default silently costing four
# unrelated repos a day of idle is exactly the incident #2186 closes.
HOLD_SCOPE_ENTRY = "entry"
HOLD_SCOPE_FLEET = "fleet"

# Wall-clock ceiling for one `resume_when` run.  The shell enforces it; it
# lives here so the CLI's help text, the alert prose and the test all quote one
# number.  A wedged probe must never wedge the tick (a tick that stops running
# is indistinguishable from a queue with nothing to do — #1616's lesson).
RESUME_PROBE_TIMEOUT_SECONDS = 5.0

# Launch attempts a single entry gets before it is blocked and escalated.  An
# attempt is only consumed when a launched drive DIED without landing the work
# — a deferral (pre-req not satisfied yet) never touches it, and neither does
# an unsatisfiable pre-req.
DEFAULT_MAX_ATTEMPTS = 2

# #2604: the `--max-fix-rounds` a TICK-LAUNCHED drive gets when neither the
# entry (`QueueEntry.max_fix_rounds`) nor `pipeline.max_fix_rounds` names one
# — deliberately LOWER than `coord drive`'s own interactive default of 3
# (`coord.commands.drive.drive`'s `--max-fix-rounds` Click option). The
# economics genuinely differ: an attended third round costs a human a few
# minutes of noticing nothing changed; an unattended one costs a queue slot
# for however long that round runs, with the model already escalated to opus
# by round two. The #2604 incident this closes was exactly a false-red
# confirmation-suite kill burning a 20-minute opus round on an already-green
# branch, unwatched, before a human happened to look. See
# :func:`effective_max_fix_rounds` for the full resolution order and
# `docs/DRIVE_QUEUE.md` for the operator-facing note on the divergence.
DEFAULT_TICK_MAX_FIX_ROUNDS = 2

# #2363: the WIDER ceiling for one specific drive-death signature — an
# acceptance-author or plain work session that exited DONE/ADVISORY claiming
# success while its branch carried zero commits (see
# `_is_empty_branch_death_reason`). Every recorded instance of this shape in
# `~/.coord/queue-block-log.jsonl` self-healed 0% of the time inside
# `DEFAULT_MAX_ATTEMPTS` (2) — `claude-coordinator#2283`, `#2348`;
# `coord-portal#74` (×2); `space-invaders#1`, `#3` — every one needed a
# by-hand `operator_removed` after exactly 2 attempts, and the by-hand
# recovery is `coord acceptance author <repo> <tracking> --issue <n>` (or a
# bare `coord drive`) — the exact command the queue already tried and could
# just try again itself. Unlike a CI signal (`is_ci_infra_reason` et al in
# `coord.merge_queue`), there is no free, passive re-check here — the only
# way to learn whether a retry would succeed is to actually spend one — so
# the fix is a WIDER attempt budget for this signature alone, not a global
# increase: every other death reason keeps `DEFAULT_MAX_ATTEMPTS` unchanged.
# Still finite: once THIS budget is also exhausted, the entry blocks with
# the same diagnosis-and-recovery instructions it has today (#1526/#2273
# discipline — no silent infinite retry).
EMPTY_BRANCH_MAX_ATTEMPTS = 6

# ── retry spacing (#2273) ────────────────────────────────────────────────────
#
# 2026-08-15: quadraui#508 and coord-portal#83 each spent their ENTIRE
# two-attempt budget inside a ~6-minute window (`launched_at` 16:19:21Z,
# gave-up 16:25:28Z) — a hand re-run of the identical `coord assign` command
# 18 minutes later succeeded first try. Nothing about the transient was
# unusually long; what failed was the queue's own pacing. Before this, the
# ONLY thing standing between a `retry` reconcile and the very next tick
# relaunching the SAME entry was #1794's startup grace window — and that
# guard exists to let a CONFIDENTLY-dead drive relaunch fast (see this
# module's docstring and `DRIVE_STARTUP_GRACE_SECONDS`'s comment), not to
# pace a plain `exit_code=1` with no distinguishing evidence at all. Two
# attempts fired minutes apart is not a retry policy against a transient —
# it is two samples of the same short window. #1894's asymmetry applies
# unchanged: a wasted retry costs latency, a prematurely parked entry costs a
# human.
#
# So a died entry's NEXT launch is now paced by wall-clock spacing, not just
# by tick cadence — see `_retry_backoff_reason`. Indexed by how many attempts
# have already died (1-based: the backoff BEFORE attempt N+1, spent after N
# attempts), clamped to the last entry for any `--max-attempts` larger than
# this table. 1 minute is comfortably longer than a single poll/tick cycle,
# so the same blip cannot be sampled twice in a row; 20 minutes is the same
# order of magnitude `PARK_STALE_SECONDS` uses for "give a transient more
# real time before trusting a frozen reading" — a related judgment call about
# how long is long enough to stop blaming the clock.
RETRY_BACKOFF_SECONDS: tuple[float, ...] = (60.0, 300.0, 1200.0)

# #2273 direction 2: a died launch that never produced a board-visible
# assignment for this issue at all gets AT LEAST this much backoff, whatever
# `RETRY_BACKOFF_SECONDS` alone would have said — see
# `_dispatch_produced_nothing`. This is the "assignment_id IS NULL" signal
# the issue names: a launch that never got as far as creating an assignment
# has no code-derived evidence behind its failure whatsoever (the
# transient-vs-real classification the issue also asks for needs stderr
# capture this queue does not have yet — a separate, harder prerequisite);
# widening the spacing is the cheap, available approximation of "treat this
# as infrastructure, not a work failure" that does not require it.
#
# Why widen the spacing instead of exempting this class from spending an
# attempt at all (which would let a pure dispatch failure retry forever,
# never reaching `blocked`/escalation)? Because the same "no code-derived
# evidence" fact that makes a dispatch failure LOOK transient also makes it
# indistinguishable from a persistently-broken entry — bad machine config, an
# unreachable host, a `coord assign` invocation that will fail identically
# every time — and exempting it outright would spin such an entry forever
# with no operator ever notified, which is exactly the "worse than nothing"
# failure mode #2230's own docstring warns a naive always-forgive rule
# produces. Widening the spacing buys a transient condition real time to
# clear while still letting the existing `max_attempts` ceiling catch a
# genuinely-broken entry and escalate it, same as any other death.
DISPATCH_FAILURE_MIN_BACKOFF_SECONDS = 300.0

# ── the per-repo ceiling (#1972) ─────────────────────────────────────────────
#
# `--max-parallel` is one GLOBAL counter, which makes the queue answer the
# wrong question.  The hazard that forced serialisation in the first place is
# strictly INTRA-repo: a merge stales the Test verdict of every other queued
# branch in that repo, because #1479's freshness keys on the base of the
# branch's own repo.  A vimcode merge cannot stale a quadraui branch.  So repo
# is precisely the boundary along which parallelism is safe — within a repo is
# the risky case, across repos is nearly free.
#
# Counting one global slot conflates the two.  With `--max-parallel 3` and a
# queue of 39 claude-coordinator entries followed by one quadraui entry, the
# tick launches claude-coordinator #2 and #3 — the two launches most likely to
# stale each other — and never reaches the quadraui entry that could have run
# alongside them for free.  Getting the wanted behaviour meant hand-chaining
# `--after` across 38 entries: tedious, fragile, and wrong the moment the queue
# is reordered.
#
# So occupancy is counted per repo as well as globally, and an entry whose repo
# is already at this ceiling DEFERS (position unchanged, no attempt consumed,
# no escalation — a "not yet", exactly like an unsatisfied `after`).  The walk
# then lands naturally on the first entry from a repo that still has headroom.
#
# The default is 1 — today's effective behaviour for the single-repo queues
# that are the common case, since `--max-parallel` itself defaults to 1.  It is
# configurable rather than hardcoded because #1715 (batch revalidation) closed,
# which makes intra-repo parallelism materially less punishing than it was; 0
# disables the per-repo ceiling entirely and restores the pre-#1972 behaviour.
#
# CAVEAT worth stating where the constant lives: per-repo occupancy inherits
# rule 1 above — it is counted from BOARD state, not live sessions (#1660).  A
# drive whose observer died still holds its repo's slot until something
# reconciles it.  That is strictly better than before (a wedged drive now
# blocks one repo instead of the whole queue) but it is also quieter, which is
# why `render_plan` prints the per-repo breakdown and says where it came from.
DEFAULT_MAX_PARALLEL_PER_REPO = 1

# ── the startup grace window (#1794) ─────────────────────────────────────────
#
# A drive is NOT established the instant `coord drive --tmux` exits 0.  #1606's
# verification proves a tmux session exists and its run log has been written
# to; it does NOT prove the drive has registered anywhere the tick can see it.
# Between the launch and the first dispatch there is a window in which the
# entry has:
#
#   * no live session in `board.live_sessions` — that snapshot is a
#     `tmux list-sessions` reading, and `list_drive_sessions()` returns `[]`
#     for "tmux unavailable" / "no server running" / "the call timed out"
#     exactly as it does for "no sessions", so one bad reading makes EVERY
#     running entry look dead at once;
#   * no `active_work` on the board — the drive has not dispatched yet.
#
# Before #1794 that fell straight through all three non-death branches of
# `_reconcile_running` into `retry`.  On 2026-08-03 a tick 40s after a launch
# declared a healthy drive dead, spent an attempt, and launched a SECOND
# `coord drive` for the same issue.  Left alone that walks the entry to
# `attempts=2/2` and `blocked`, i.e. an unattended queue parks healthy work and
# reports it as failed.  The two ticks were 40s apart because DRIVE_QUEUE.md §2's
# install sequence is `systemctl --user enable --now …timer` immediately
# followed by a verification `systemctl --user start …service` — i.e. the
# documented install reliably produces the back-to-back ticks that trigger it.
#
# So an entry launched within this window is `starting`, not dead: it OCCUPIES
# capacity and is never a retry candidate.  The measured startup on a loaded
# dellserver was ~2 minutes (19:13:09 launch → 19:15:22 `drive loop started`),
# and this is 5 — deliberately >2x that, and still well under the timer's
# 15-minute cadence so a genuinely dead drive is only ever delayed by ONE
# interval before the retry path sees it.
#
# The window is also applied to the LAUNCH decision (see `_startup_cooldown`),
# so no code path in the tick — not a retry, not a hand-edited row — can start
# a second `coord drive` for an issue whose last launch is this recent.
# `coord drive`'s per-issue flock stays the last line of defence; the queue no
# longer relies on it.
DRIVE_STARTUP_GRACE_SECONDS = 300.0

# ── #2587: roll-pending bounds ────────────────────────────────────────────────
#
# A "roll at the next inter-drive gap" marker (see `RollPending` below) must
# never hold the queue down indefinitely — the whole point of #2587 is to
# replace a drain that waited an unbounded 60 minutes for a window that never
# came. Two independent bounds, mirroring #2240's cordon-deferral ceiling
# (`coord/release_cordon.py`'s `max_deferrals`/`release_cooldown`) rather than
# inventing a third shape for "give up eventually":
#
# * `ROLL_PENDING_DEFAULT_TTL_SECONDS` — wall-clock. Same default as
#   `coord/release_window.py`'s (pre-#2587) `DEFAULT_DRAIN_WAIT_SECONDS`: an
#   hour leaves ample room inside a quiet-hours window even set as late as
#   03:00, and keeps the bound recognisable to an operator who already knows
#   that number from the old drain.
# * `ROLL_PENDING_DEFAULT_MAX_DEFERRALS` — a tick-count ceiling, independent of
#   the clock. `coord-drive-queue.timer` fires roughly every 3 minutes in
#   production, so 20 deferrals is ~an hour of ticks that each found the fleet
#   still busy — the same order of magnitude as the TTL, reached the other
#   way. Two independent measures catch different failure shapes: a wedged
#   clock defeats a TTL-only bound; a tick loop firing far faster than
#   expected defeats a deferral-only one.
#
# Whichever bound is hit first clears the marker and resumes normal launching
# — see `RollPending.expired` — and the shell (`coord.commands.drive_queue`)
# is responsible for reporting that loudly (#2587's "never silently held"
# requirement), the same posture #2240 forced onto an expired cordon.
ROLL_PENDING_DEFAULT_TTL_SECONDS = 3600.0
ROLL_PENDING_DEFAULT_MAX_DEFERRALS = 20

# ── the queue-level alert's synthetic escalation key ─────────────────────────
#
# #1754 asks for "one queue-level record per tick, written through the DQ-1
# seam OR `record_drive_escalation` with a synthetic issue key — pick one and
# state it in the code comment, don't leave both live".
#
# CHOSEN: `record_drive_escalation` under the synthetic key below.  Reasons:
# the alert is exactly the shape `drive_escalations` already stores (stage +
# reason + gate readings + a proposed command), that table's UNIQUE(repo_name,
# issue_number) + ON CONFLICT DO UPDATE gives "exactly one record, replaced
# each tick" for free, and `coord escalate list` / the TUI's escalation
# plumbing pick it up with no new wire type.  The alternative — a synthetic
# `drive_queue` row — would have to be filtered out of `list`, `move`,
# `plan_tick`, and the dense-position renumbering, i.e. a special case in
# every function in this file.  The DQ-1 seam stays strictly "real entries".
#
# The repo name is deliberately not a valid coordinator.yml repo, so this row
# can never collide with a real issue's escalation or match a Pipeline row.
QUEUE_ALERT_REPO = "(drive-queue)"
QUEUE_ALERT_ISSUE = 0
QUEUE_ALERT_STAGE = "drive-queue"


class QueueError(ValueError):
    """A queue mutation was refused before it was written.

    Carries a message naming the offending issue and the violated constraint,
    the same posture ``coord milestone write-order`` takes for ``## Work
    order`` (``coord.milestone_order.WorkOrderError``): validate, then write —
    never write and then discover.
    """


# ── keys ─────────────────────────────────────────────────────────────────────


def entry_key(repo: str, issue: int) -> str:
    """The fully-qualified queue/pre-req key for an issue: ``"repo#N"``.

    This is the on-disk form DQ-1 stores in ``after_json`` — one column
    carries a cross-repo queue with no second column.
    """
    return f"{repo}#{int(issue)}"


def parse_key(key: str) -> tuple[str, int] | None:
    """Inverse of :func:`entry_key`; ``None`` when *key* isn't ``repo#N``.

    Splits on the LAST ``#`` so a repo name containing one still parses, and
    requires the tail to be a bare number.
    """
    repo, sep, num = str(key).rpartition("#")
    if not sep or not repo or not num.isdigit():
        return None
    return repo, int(num)


def parse_after_spec(raw: str | Iterable[str], default_repo: str) -> list[str]:
    """Normalise a ``--after`` spec into fully-qualified ``repo#N`` keys.

    Accepts ``N`` or ``REPO#N``, comma-separated, and (for repeatable Click
    options) an iterable of either.  Bare numbers resolve against
    *default_repo* — the queue is usually single-repo, and typing the repo
    name twice is the kind of friction that gets a flag skipped.

    Raises :class:`QueueError` on anything that isn't one of those two forms,
    rather than silently dropping it (a dropped pre-req launches work early,
    which is the whole failure this feature exists to prevent).
    """
    chunks: list[str] = []
    items: Iterable[str] = [raw] if isinstance(raw, str) else raw
    for item in items:
        chunks.extend(str(item).split(","))

    keys: list[str] = []
    for chunk in chunks:
        text = chunk.strip()
        if not text:
            continue
        if text.isdigit():
            keys.append(entry_key(default_repo, int(text)))
            continue
        parsed = parse_key(text.lstrip("#"))
        if parsed is None:
            raise QueueError(
                f"malformed --after entry {text!r} (expected 'N' or 'REPO#N')"
            )
        keys.append(entry_key(*parsed))
    # De-duplicate, preserving declaration order.
    seen: set[str] = set()
    out: list[str] = []
    for key in keys:
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


# ── the queue row ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class QueueEntry:
    """One ``drive_queue`` row, typed.

    Built from the dicts DQ-1's ``list_drive_queue()`` returns — identical
    whether they came off the local DB or the daemon's ``/drive-queue``, which
    is what lets the whole tick run unchanged on a thin client.
    """

    repo: str
    issue: int
    position: int = 0
    machine: str = ""
    after: tuple[str, ...] = ()
    state: str = STATE_WAITING
    attempts: int = 0
    deferrals: int = 0
    last_reason: str = ""
    # #2133: wall-clock capture time of `last_reason`, stamped by
    # `coord.state._update_drive_queue_entry_local` every time `last_reason`
    # is written. `None` for a row predating the migration or whose
    # `last_reason` is still the '' default — never treated as "just now".
    # A rendering that shows `last_reason` without also showing (or at least
    # consulting) this is exactly the bug #2133 closes: a point-in-time
    # observation displayed as if it were current state.
    reason_at: float | None = None
    session_name: str = ""
    launched_at: float | None = None
    # #1870: the short hostname of the machine whose tick launched THIS
    # session — stamped alongside `session_name`/`launched_at` when the
    # launch succeeds.  '' for a row predating this column or hand-flipped to
    # `running`, which degrades to the pre-#1870 behaviour exactly (see
    # `_reconcile_running`).  Liveness (`list_drive_sessions`) is always a
    # LOCAL tmux read; this is what lets a tick tell "no session because it's
    # dead" apart from "no session because it's not MY session to see".
    launch_host: str = ""
    # #1757 deploy gate.  `hold_after`/`hold_reason`/`resume_when` are
    # operator-declared (written by `enqueue`); `hold_state`/`hold_probes` are
    # the tick's run state.
    hold_after: bool = False
    hold_reason: str = ""
    resume_when: str = ""
    hold_state: str = HOLD_NONE
    hold_probes: int = 0
    # #2186: WHAT a fired gate holds — see the constants' own comment above.
    # Operator-declared at enqueue time, same as `hold_after`/`hold_reason`.
    # Normalised to exactly `HOLD_SCOPE_ENTRY` or `HOLD_SCOPE_FLEET` by
    # `_normalize_hold_scope`; a row predating this column (or any other
    # unrecognised value) reads as `HOLD_SCOPE_ENTRY` — the narrower, safer
    # default — never as a silent fleet-wide stop.
    hold_scope: str = HOLD_SCOPE_ENTRY
    # #2230: count of times the `blocked`-reconciliation sweep has resumed
    # THIS entry from `blocked` back to `waiting` — see `MAX_BLOCKED_RESUMES`
    # and `_reconcile_blocked`. 0 for every row predating this column and for
    # any entry that has never been auto-resumed, which is the common case;
    # it is never reset by a normal launch/retry cycle, only by an operator's
    # `remove && add` (a fresh row), so a gate that keeps flapping across
    # several give-ups is still visible as a rising number rather than
    # restarting its count each time.
    resumes: int = 0
    # #2273 (post-review): wall-clock moment a `retry` reconcile recorded a
    # death — the anchor `_retry_backoff_reason` measures its window from.
    # Deliberately NOT `reason_at`: that field is re-stamped by every
    # `last_reason` write, including the backoff-deferral's own per-tick
    # status refresh, which made the backoff window's own clock reset on
    # every tick it was checked (the "moving target" bug — an entry whose
    # backoff exceeded the tick interval could never finish waiting). Written
    # ONLY by `_reconcile_running`'s `retry` branch and never touched again
    # until the next death, the same way `launched_at` stays fixed for
    # #1794's grace window. `None` for every row predating this column and
    # for an entry that has never died — treated identically to
    # `attempts <= 0` by `_retry_backoff_reason` (no backoff yet).
    retry_backoff_at: float | None = None
    # #2604: operator override of the `--max-fix-rounds` THIS entry's
    # tick-launched drive gets — see `effective_max_fix_rounds` for the full
    # resolution order (this field, then `pipeline.max_fix_rounds`, then
    # `DEFAULT_TICK_MAX_FIX_ROUNDS`). `None` for every row predating this
    # column and for any entry enqueued without `--max-fix-rounds` — reads
    # identically to "no override", never as "zero fix rounds".
    max_fix_rounds: int | None = None
    # #2589: operator opt-out of #1453's oracle-loop JIT slice authoring for
    # THIS entry's tick-launched drive — a per-entry `coord drive
    # --no-acceptance` passthrough, same shape as `max_fix_rounds` above.
    # Exists because a `blocked` row's own recorded reason can recommend
    # exactly this flag (the #2531 incident: "re-run coord drive with
    # --no-acceptance to skip JIT authoring") with no way to act on it
    # through the queue — the operator was left to bypass the queue (and its
    # `--max-parallel-per-repo` ceiling) entirely to follow the queue's own
    # advice. `False` for every row predating this column and for any entry
    # enqueued without `--no-acceptance` — reads identically to "no
    # override", the tick's pre-#2589 behaviour exactly.
    no_acceptance: bool = False

    @property
    def key(self) -> str:
        return entry_key(self.repo, self.issue)

    @property
    def gate_reason(self) -> str:
        """What to tell the operator when this entry's gate fires.

        Never empty: an operator who used ``--hold-after`` without a reason
        still gets a sentence naming the entry, because an alert that says
        only "HELD" is one the operator has to go and reconstruct.
        """
        return self.hold_reason or f"deploy gate declared on {self.key}"

    @staticmethod
    def _normalize_hold_scope(value: Any) -> str:
        """Fail closed to the NARROWER scope, never the wider one (#2186).

        Only the literal string ``"fleet"`` opts into the whole-queue stop.
        Anything else — a row from before this column existed (``None`` /
        ``''``), a hand-edited typo, a value a future version doesn't know —
        reads as ``HOLD_SCOPE_ENTRY``, so a malformed value can never
        silently escalate one entry's gate into a fleet-wide one.
        """
        return HOLD_SCOPE_FLEET if str(value or "") == HOLD_SCOPE_FLEET else HOLD_SCOPE_ENTRY

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "QueueEntry":
        """Type one raw queue row.

        ``after_json`` arrives as a real list over the wire (it is typed
        ``list[str]`` on ``coord.board_schema.BoardDriveQueueEntry``) but as
        a JSON *string* when a caller
        reads the table directly, so both are accepted; anything unparseable
        degrades to ``()`` rather than blowing up the whole tick.
        """
        raw_after: Any = row.get("after_json")
        if isinstance(raw_after, str):
            try:
                raw_after = json.loads(raw_after)
            except (TypeError, ValueError):
                raw_after = []
        if not isinstance(raw_after, list):
            raw_after = []
        launched_at = row.get("launched_at")
        return cls(
            repo=str(row.get("repo_name") or ""),
            issue=int(row.get("issue_number") or 0),
            position=int(row.get("position") or 0),
            machine=str(row.get("machine") or ""),
            after=tuple(str(a) for a in raw_after),
            state=str(row.get("state") or STATE_WAITING),
            attempts=int(row.get("attempts") or 0),
            deferrals=int(row.get("deferrals") or 0),
            last_reason=str(row.get("last_reason") or ""),
            reason_at=(
                None
                if row.get("reason_at") is None
                else float(row.get("reason_at"))
            ),
            session_name=str(row.get("session_name") or ""),
            launched_at=None if launched_at is None else float(launched_at),
            launch_host=str(row.get("launch_host") or ""),
            # SQLite hands `hold_after` back as 0/1; a JSON client may send a
            # real bool.  `bool(...)` accepts both and, for a row written
            # before #1757's migration ran, an absent key reads as False —
            # i.e. no gate, which is the pre-#1757 behaviour exactly.
            hold_after=bool(row.get("hold_after") or 0),
            hold_reason=str(row.get("hold_reason") or ""),
            resume_when=str(row.get("resume_when") or ""),
            hold_state=str(row.get("hold_state") or HOLD_NONE),
            hold_probes=int(row.get("hold_probes") or 0),
            hold_scope=cls._normalize_hold_scope(row.get("hold_scope")),
            resumes=int(row.get("resumes") or 0),
            retry_backoff_at=(
                None
                if row.get("retry_backoff_at") is None
                else float(row.get("retry_backoff_at"))
            ),
            max_fix_rounds=(
                None
                if row.get("max_fix_rounds") is None
                else int(row.get("max_fix_rounds"))
            ),
            # #2589: same 0/1-from-SQLite-or-real-bool-from-JSON acceptance
            # as `hold_after` above; absent (a row predating this column)
            # reads `False` — no passthrough, the pre-#2589 behaviour.
            no_acceptance=bool(row.get("no_acceptance") or 0),
        )


def entries_from_rows(rows: Iterable[Mapping[str, Any]]) -> list[QueueEntry]:
    """Type a whole queue read, in ``position`` order."""
    return sorted(
        (QueueEntry.from_row(r) for r in rows), key=lambda e: (e.position, e.key)
    )


def effective_max_fix_rounds(
    entry: QueueEntry, config_default: int | None
) -> int:
    """The ``--max-fix-rounds`` value the tick launches *entry* with (#2604).

    Resolution order, most specific wins:

    1. ``entry.max_fix_rounds`` — this entry's own ``coord drive-queue add
       --max-fix-rounds`` override.
    2. ``config_default`` — the fleet's ``pipeline.max_fix_rounds``, when set.
    3. :data:`DEFAULT_TICK_MAX_FIX_ROUNDS` — deliberately lower than
       ``coord drive``'s own interactive default (3): see that constant's
       docstring for why an unattended round costs more than an attended one.

    Never returns a value below 1 — a non-positive ``config_default`` (which
    :func:`coord.config._parse_pipeline` already rejects at load time, but a
    hand-built ``PipelineConfig`` in a test is not obligated to) falls back to
    :data:`DEFAULT_TICK_MAX_FIX_ROUNDS` rather than producing a drive that
    cannot spend even one fix round.
    """
    if entry.max_fix_rounds is not None and entry.max_fix_rounds >= 1:
        return entry.max_fix_rounds
    if config_default is not None and config_default >= 1:
        return config_default
    return DEFAULT_TICK_MAX_FIX_ROUNDS


# ── aggregate summary (#2428 DQW-1) ───────────────────────────────────────────
#
# Ported field-for-field from `tui/src/app/drive_queue.rs`'s
# `summarize_drive_queue`/`DriveQueueSummary` — see that module's own comments
# for the #2186 incident behind "blocked outranks a fleet-held gate, which
# outranks a stall" and for why `fleet_held` (not `held`) is what actually
# stops the queue. Kept here, next to `entries_from_rows`, rather than in
# `coord/dashboard/server.py`, so any future consumer (a CLI summary, another
# server) gets the exact same counts without re-deriving them.


def _entry_is_pending(entry: QueueEntry) -> bool:
    """Rows that still have work ahead — `done` entries are history."""
    return entry.state != STATE_DONE


def _entry_is_holding(entry: QueueEntry) -> bool:
    """Is this row's OWN deploy gate currently fired? Scope-agnostic."""
    return entry.hold_state == HOLD_FIRED


def _entry_stops_fleet(entry: QueueEntry) -> bool:
    """Does this row's fired gate stop the WHOLE queue (#2186)?"""
    return _entry_is_holding(entry) and entry.hold_scope == HOLD_SCOPE_FLEET


def _after_satisfied(entry: QueueEntry, all_entries: Sequence[QueueEntry]) -> bool:
    """Is *entry*'s `after=` list satisfied by the queue it sits in?

    Same conservative, local read as the Rust original: a pre-req not
    present in *all_entries* at all is treated as satisfied (it may have
    landed long ago); a pre-req whose OWN gate has fired is unsatisfied even
    though its row has by then reconciled to `done`.
    """
    for key in entry.after:
        unsatisfied = any(
            other.key == key
            and (other.state != STATE_DONE or _entry_is_holding(other))
            for other in all_entries
        )
        if unsatisfied:
            return False
    return True


@dataclass(frozen=True)
class DriveQueueSummary:
    """Aggregate reading over a whole (or `?repo=`-filtered) queue read.

    ``level`` is the same ascending-severity rank
    `tui/src/app/drive_queue.rs::DriveQueueLevel` computes — `"empty"` <
    `"normal"` < `"stalled"` < `"held"` < `"blocked"` — so a client can badge
    the panel sidebar without re-deriving the ranking rule.
    """

    level: str = "empty"
    # #2428: total non-`done` rows — the Rust original never materializes
    # this as a struct field (it's implicit in the loop's filter), but a web
    # sidebar wants a single "N pending" headline as much as it wants the
    # breakdown, so it is added here rather than left for the client to sum.
    pending: int = 0
    running: int = 0
    waiting: int = 0
    blocked: int = 0
    # Waiting rows whose in-queue pre-reqs are all satisfied — rows a tick
    # could plausibly pick up next. Zero-with-waiting-rows is the stall.
    eligible: int = 0
    # Rows whose OWN deploy gate has fired, any scope — purely informational.
    held: int = 0
    # Rows whose FLEET-scoped deploy gate has fired — non-zero means the tick
    # will launch nothing at all, whatever `eligible` says.
    fleet_held: int = 0


def summarize_drive_queue(entries: Sequence[QueueEntry]) -> DriveQueueSummary:
    """Summarise *entries* — pure, no clock, no DB. See :class:`DriveQueueSummary`."""
    pending = sum(1 for e in entries if _entry_is_pending(e))
    held = sum(1 for e in entries if _entry_is_holding(e))
    fleet_held = sum(1 for e in entries if _entry_stops_fleet(e))
    running = waiting = blocked = eligible = 0
    for e in entries:
        if not _entry_is_pending(e):
            continue
        if e.state == STATE_RUNNING:
            running += 1
        elif e.state == STATE_BLOCKED:
            blocked += 1
        elif e.state == STATE_WAITING:
            waiting += 1
            if _after_satisfied(e, entries):
                eligible += 1
        # An unrecognised state from a newer daemon: counted in `pending`
        # above (it is not `done`) but never as waiting/eligible/running/
        # blocked, so it can neither trigger nor mask a stall.

    if blocked > 0:
        # Blocked outranks stalled — a hard stop is worse news than a queue
        # that is merely waiting for capacity.
        level = "blocked"
    elif fleet_held > 0:
        # #1757/#2186: a FLEET-scoped fired gate outranks a stall, even a
        # real one — "3 waiting, none eligible" is a symptom here, and "you
        # have a deploy to do" is the cause.
        level = "held"
    elif waiting > 0 and eligible == 0 and running == 0:
        # Nothing running AND nothing that could start.
        level = "stalled"
    elif running > 0 or waiting > 0:
        level = "normal"
    else:
        level = "empty"

    return DriveQueueSummary(
        level=level,
        pending=pending,
        running=running,
        waiting=waiting,
        blocked=blocked,
        eligible=eligible,
        held=held,
        fleet_held=fleet_held,
    )


# ── the board projection ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class IssueFacts:
    """Everything the tick needs to know about one issue, and nothing else.

    All four fields come off ``GET /board`` — no ``gh`` call, no CLI prose.
    ``known=False`` means the board has never heard of this issue at all
    (unsynced, or a typo'd number), which is deliberately NOT the same as
    "open": an unknown pre-req is unsatisfiable, an open one merely defers.
    """

    known: bool = False
    issue_state: str = ""  # "open" / "closed" / "" when the board has no row
    # #2858: how long ago the `issues` cache row behind `issue_state` was
    # last refreshed by `coord.serve_app._sync_issues_tick` — `None` when
    # the board carried no `synced_at` for this issue at all (no issues row,
    # or a payload built before #2858). Populated by `build_board_view`;
    # see `issue_cache_stale` for the ONE thing this changes.
    issue_synced_at: float | None = None
    merged: bool = False  # a work-like assignment with status == 'merged'
    active_work: bool = False  # a NON-terminal work-like assignment
    # #1891: this issue's CURRENT merge-queue entry is refused for nothing
    # stronger than "CI checks have not reported yet" — see
    # `build_board_view`'s population of this field for exactly which board
    # sections it reads (and why it reads BOTH of them) and
    # `_reconcile_running`'s `parked` outcome for the one place it changes a
    # decision.
    merge_ci_pending: bool = False
    # The actual board/queue reason text `merge_ci_pending` was derived from
    # (e.g. ``"CI running: build, lint"``) — carried alongside the bool
    # purely for diagnostics, so a `parked` reconcile's `reason` can quote
    # the SAME text an operator would see on `IssueState.merge_reason`
    # instead of a generic synthesised sentence.
    merge_ci_pending_reason: str = ""
    # #2158: PROVENANCE of `merge_ci_pending` — `True` when it came from the
    # live `merge_plan` row's own reason (a fresh re-derivation performed at
    # board-render time, every board build), `False` when the ONLY witness
    # was the raw `merge_queue` row's persisted `error`.
    #
    # That distinction is the whole of #2158. The raw `error` is written by a
    # live `coord merge` attempt and by nothing else — so for a `parked`
    # entry, which by construction runs no merge, it is frozen at the very
    # attempt that parked it. The predicate that releases the park was
    # refreshed only by the action the park withholds, and an entry could sit
    # parked for hours (7h25m on claude-coordinator#2138, 2026-08-12) citing
    # CI that finished 41 seconds before the park was even written.
    #
    # A `True` reading is self-refreshing and can be trusted indefinitely: the
    # next board build re-derives it and it goes false the moment CI reports.
    # A `False` reading cannot refresh itself at all, so `plan_tick` ages it
    # out (:data:`PARK_STALE_SECONDS`) rather than trusting it forever.
    merge_ci_pending_live: bool = False
    # #2230: this issue's merge-plan STATUS — `coord.merge_queue.PLAN_READY`/
    # `PLAN_BLOCKED`/`PLAN_MERGING`/`PLAN_MERGED`/`PLAN_NEEDS_ATTENTION` — as
    # of the LIVE `merge_plan` section of a `/board` fetch, i.e. served off
    # the tick-refreshed gate snapshot (#1336 Invariant 1: no `gh` call on
    # this read path). `''` when the entry has no merge-queue row at all
    # right now (never enqueued, already drained out of PENDING, or this
    # board fetch has no `merge_plan` section — see
    # `_local_merge_queue_rows`'s docstring for the one lane that doesn't:
    # the daemon-host tick, which reads the local DB directly). Unlike
    # `merge_ci_pending`, which only ever says "still shut", this is the
    # general READY/BLOCKED reading `_reconcile_blocked` needs to release a
    # `blocked` entry whose gate cleared for a reason OTHER than CI — a
    # review approval, a smoke verdict, a staleness re-run — not just CI.
    merge_gate_status: str = ""
    # The plan's own `reason` alongside `merge_gate_status`, carried purely
    # for diagnostics — same posture as `merge_ci_pending_reason` above.
    merge_gate_reason: str = ""
    # #2273: the newest `dispatched_at` across EVERY work-like assignment
    # `build_board_view` has ever seen for this issue, whatever its current
    # `status` — unlike `merged`/`active_work` this is a HIGH-WATER MARK, not
    # a "right now" reading, because `_dispatch_produced_nothing` needs to
    # tell "nothing was ever dispatched during THIS launch" apart from "the
    # assignment this launch dispatched already went terminal" — the latter
    # still proves dispatch itself succeeded. `None` when this issue has no
    # assignment carrying a `dispatched_at` at all (never dispatched, or
    # every row predates that column).
    last_dispatched_at: float | None = None

    @property
    def open(self) -> bool:
        return self.issue_state == "open"

    @property
    def closed(self) -> bool:
        return self.issue_state == "closed"

    @property
    def landed(self) -> bool:
        """The work is done, by either witness.

        Both are checked because #611 leaves merged work with ``branch=None``
        rows the merge projection can miss, and quadraui-style repos can merge
        a PR into ``develop`` while the linked issue stays open — so neither
        signal alone is reliable.
        """
        return self.merged or self.closed


def _issue_cache_stale(facts: IssueFacts, now: float | None) -> bool:
    """True when *facts*' ``issue_synced_at`` is old enough (#2858) that a
    NEGATIVE ``landed`` reading — the board's ``issues`` cache still says
    "open" — should not be trusted as current, because the cache that fact
    came from may simply not have caught up with a merge/close yet (a
    starved :func:`coord.serve_app._sync_issues_tick`).

    ``now=None`` — the same "no clock, no age" convention every other
    staleness check in this module uses (see :func:`_park_reading_age`) —
    and a ``facts.issue_synced_at`` of ``None`` (no ``issues`` row at all, or
    a board payload built before #2858) both read as "not stale": the safe
    default is trusting the cache exactly as every caller did before this
    field existed, so every pre-#2858 board fixture / pure-logic call site is
    unaffected.

    Deliberately has NO bearing on a POSITIVE ``landed`` reading — once
    ``landed`` is ``True`` it is always trustworthy (see its own docstring:
    nothing ever un-lands an issue). This is only ever consulted where a
    negative reading is about to be treated as definitive enough to spend a
    retry/exhausted attempt on — see ``_reconcile_running``'s one call site.
    """
    if now is None or facts.issue_synced_at is None:
        return False
    return (now - facts.issue_synced_at) >= ISSUE_CACHE_STALE_CEILING_S


@dataclass(frozen=True)
class BoardView:
    """The whole board reduced to per-issue facts plus live drive sessions."""

    issues: Mapping[str, IssueFacts] = field(default_factory=dict)
    live_sessions: frozenset[str] = frozenset()

    def facts(self, key: str) -> IssueFacts:
        return self.issues.get(key, IssueFacts())


def build_board_view(
    payload: Mapping[str, Any],
    live_sessions: Iterable[Mapping[str, Any] | str] = (),
) -> BoardView:
    """Reduce a ``/board`` payload + ``drive-sessions --json`` to a :class:`BoardView`.

    Pure: *payload* is whatever ``coord.drive_state.BoardFetcher.fetch()``
    returned and *live_sessions* is whatever ``coord.drive.list_drive_sessions()``
    returned (dicts with ``repo``/``issue``), or a plain iterable of
    ``"repo#N"`` keys for tests.
    """
    facts: dict[str, dict[str, Any]] = {}

    def slot(key: str) -> dict[str, Any]:
        return facts.setdefault(
            key,
            {"known": True, "issue_state": "", "merged": False, "active_work": False},
        )

    for row in payload.get("assignments") or []:
        if (row.get("type") or "") not in WORK_LIKE:
            continue
        repo = row.get("repo_name") or ""
        number = row.get("issue_number")
        if not repo or number is None:
            continue
        entry = slot(entry_key(repo, int(number)))
        status = row.get("status") or ""
        if status == "merged":
            entry["merged"] = True
        if status not in TERMINAL_STATUSES:
            entry["active_work"] = True
        # #2273: high-water mark of `dispatched_at`, regardless of `status` —
        # see `IssueFacts.last_dispatched_at`'s docstring for why this is
        # deliberately not scoped to non-terminal rows the way `active_work`
        # is.
        dispatched_at = row.get("dispatched_at")
        if dispatched_at is not None:
            try:
                dispatched_at = float(dispatched_at)
            except (TypeError, ValueError):
                dispatched_at = None
        if dispatched_at is not None:
            prior = entry.get("last_dispatched_at")
            if prior is None or dispatched_at > prior:
                entry["last_dispatched_at"] = dispatched_at

    for row in payload.get("issues") or []:
        repo = row.get("repo_name") or ""
        number = row.get("number")
        if not repo or number is None:
            continue
        entry = slot(entry_key(repo, int(number)))
        entry["issue_state"] = str(row.get("state") or "").lower()
        # #2858: carried alongside `issue_state` so a stale cache read can be
        # told apart from a fresh one — see `_issue_cache_stale`. `BoardIssue.
        # synced_at` is already on the wire (coord/board_schema.py); this is
        # the first consumer that reads it off a `/board` `issues` row.
        synced_at = row.get("synced_at")
        if isinstance(synced_at, (int, float)):
            entry["issue_synced_at"] = float(synced_at)

    # #1891: `merge_ci_pending` — mirrors `drive_state._merge_entry`'s OWN
    # reason resolution exactly (live `merge_plan` reason, falling back to
    # the raw `merge_queue` row's persisted `error` when the plan's
    # re-evaluation comes back empty) rather than importing that per-issue
    # function and calling it once per queue entry: this is a single O(N)
    # pass over the SAME two board sections `_merge_entry` scans, building a
    # dict up front the way every other fact in this function already does.
    # See `coord.merge_queue.CI_PENDING_PREFIX`'s docstring for why the raw
    # row is a required second read, not a belt-and-braces extra one.
    plan_rows: dict[str, Mapping[str, Any]] = {}
    for row in payload.get("merge_plan") or []:
        repo = row.get("repo_name") or ""
        number = row.get("issue_number")
        if not repo or number is None:
            continue
        key = entry_key(repo, int(number))
        plan_rows[key] = row
        # #2230: the plan's own STATUS, stashed for EVERY entry with a
        # merge-plan row — not just a CI-pending one, unlike the
        # `merge_ci_pending` loop below. This is what lets `_reconcile_blocked`
        # tell "cleared" from "still shut" for a `blocked` entry on the cheap
        # lane (a live `/board` fetch): a plan reading `PLAN_READY` is
        # positive evidence the gate cleared for ANY reason (review, smoke,
        # CI, staleness), where `merge_ci_pending` can only ever confirm
        # "still shut on CI specifically".
        got = slot(key)
        got["merge_gate_status"] = str(row.get("status") or "")
        got["merge_gate_reason"] = str(row.get("reason") or "")

    for row in payload.get("merge_queue") or []:
        repo = row.get("repo_name") or ""
        number = row.get("issue_number")
        if not repo or number is None:
            continue
        key = entry_key(repo, int(number))
        plan_row = plan_rows.get(key)
        plan_reason = str((plan_row or {}).get("reason") or "")
        raw_reason = str(row.get("error") or "")
        reason = plan_reason or raw_reason
        # #2158: provenance of `reason`, tracked AT the point it is decided
        # rather than re-derived afterward as `bool(plan_reason)`. The two
        # are NOT equivalent once the #1892 override just below can replace
        # a non-empty `plan_reason` with the raw reading — `reason_is_live`
        # must follow `reason` itself, not the variable that lost the fight.
        reason_is_live = bool(plan_reason)
        # #1892: same recovery `drive_state._merge_entry` applies — the
        # plan's own reason is `_entry_gate_status`'s fresh re-derivation at
        # board-build time, which never computes the CI_INFRA_PREFIX
        # classification (it needs an extra `gh api .../jobs` call the
        # board *read* path must never make — see `coord.gate_snapshot`'s
        # Invariant 1). Only a LIVE `coord merge` attempt computes it and
        # persists it onto the raw row. Prefer the raw reading whenever it
        # carries the classification and the plan's fresher one doesn't —
        # otherwise a verdictless failure would never park here at all.
        #
        # This can fire even when `plan_reason` is non-empty (e.g. a live,
        # non-infra "CI failed: ..." reading that isn't itself the infra
        # classification) — the raw CI-infra string still wins. When it
        # does, `reason` ends up being the frozen raw string, not the live
        # plan one, so `reason_is_live` must flip to `False` too: otherwise
        # `merge_ci_pending_live` would be reporting on a `plan_reason` that
        # isn't what `reason` actually is, and `_park_reading_expired`
        # (which trusts `merge_ci_pending_live` to mean "self-refreshing, no
        # ceiling needed") would never age this park out.
        if is_ci_infra_reason(raw_reason) and not is_ci_infra_reason(plan_reason):
            reason = raw_reason
            reason_is_live = False
        # #2252: same recovery, for the sibling CI_FLAKY_PREFIX
        # classification — a `checks_failed` streak currently mid its one
        # #2252 re-run to rule out a flake. `_entry_gate_status` re-derives
        # this identically to a plain "checks failed: ..." block (it has no
        # notion of the raw row's `ci_flaky_reruns`/`ci_flaky_pending`
        # state), so the raw reading must win here too or this entry would
        # never park — it would instead sit `checks_failed` and burn a
        # drive-queue launch attempt for the exact transient #2252 exists
        # to catch. `elif`: the two classifications are mutually exclusive
        # per entry (see the identical `elif` in `drive_state._merge_entry`).
        elif is_ci_flaky_reason(raw_reason) and not is_ci_flaky_reason(plan_reason):
            reason = raw_reason
            reason_is_live = False
        # #2347: NO raw-row recovery needed here, unlike #1892/#2252 above —
        # `coord.merge_queue._ci_unreadable_reason` needs no extra `CiStore`
        # call (see that function's docstring), so `_entry_gate_status`
        # computes the CI_UNREADABLE_PREFIX classification directly at
        # board-build time. `plan_reason` already carries it whenever it
        # applies; the live plan reading and a live `coord merge` attempt's
        # raw reading can never disagree about this one.
        if is_ci_terminal_reason(reason):
            continue
        # #2158: the same plan row that came back with NO reason of its own
        # also carries `ci_summary` — `summarize_counts` over the very checks
        # `_entry_gate_status` just consulted, on the same board build. When
        # that rollup positively shows every check finished and none failed,
        # it is direct evidence AGAINST the raw row's frozen "CI running:" /
        # "CI infra:" / "CI re-checking:" string, which no read path ever
        # rewrites. Believing the write-path string over it is what wedged
        # claude-coordinator#2138 parked for 7h25m on CI that had gone green
        # 41s before the park was written.
        #
        # The override is deliberately POSITIVE-evidence-only, and only where
        # the plan itself is silent:
        #
        # * a non-empty `plan_reason` means the live gate still objects — it
        #   wins outright, untouched (this branch never runs);
        # * absence of a rollup (no `merge_plan` section at all — the
        #   daemon-host tick, see `_local_merge_queue_rows`; no `ci_store`;
        #   no PR; a gate snapshot that has not yet fetched this PR) is NOT
        #   evidence of anything, so the #1891 fallback stands unchanged and
        #   the entry still parks. That fail-closed half is what
        #   `PARK_STALE_SECONDS` ages out instead — see `plan_tick`.
        #
        # `failed == 0` is required as well as `running == 0`: a still-failing
        # check means the CI_INFRA_PREFIX classification the raw row carries
        # may well still be the true reading of that failure (the plan can
        # never re-derive it — #1892), so a rollup showing red is not evidence
        # the infra park has cleared.
        if not plan_reason and ci_rollup_all_clear((plan_row or {}).get("ci_summary")):
            continue
        got = slot(key)
        got["merge_ci_pending"] = True
        got["merge_ci_pending_reason"] = reason
        got["merge_ci_pending_live"] = reason_is_live

    sessions: set[str] = set()
    for item in live_sessions:
        if isinstance(item, str):
            sessions.add(item)
            continue
        repo = item.get("repo") or ""
        number = item.get("issue")
        if repo and number is not None:
            sessions.add(entry_key(repo, int(number)))

    return BoardView(
        issues={key: IssueFacts(**value) for key, value in facts.items()},
        live_sessions=frozenset(sessions),
    )


# ── the plan ─────────────────────────────────────────────────────────────────
#
# Every item carries an explicit `updates` mapping of DQ-1-whitelisted columns
# (see `_DRIVE_QUEUE_UPDATABLE` in coord/state.py).  The shell's apply loop is
# therefore a single uniform `update_drive_queue_entry(repo, issue, **updates)`
# per item — it never re-derives a decision, and a plan with no updates is
# provably a no-op, which is what makes `--dry-run` trustworthy.


@dataclass(frozen=True)
class Reconcile:
    """The resolved outcome for one ``running`` entry.

    ``outcome`` is one of:

    * ``alive``     — a live ``coord-drive-*`` tmux session.  Occupies.
    * ``starting``  — launched inside :data:`DRIVE_STARTUP_GRACE_SECONDS` and
      not yet visible anywhere else (#1794).  Occupies; never a death.
    * ``held``      — session gone but work still ACTIVE on the board (the
      #1660 observer-deadline case).  Occupies; never a death.
    * ``unknown``   — this entry's ``launch_host`` names a DIFFERENT machine
      than the one running this tick (#1870).  Liveness is always a LOCAL
      tmux read, so a foreign host's session is invisible here — that is not
      evidence of anything.  Occupies; never a death, never a retry.
    * ``done``      — merged, or the issue closed.
    * ``refused``   — #1844: the drive's own exit was a PERMANENT pre-dispatch
      guard refusal (``coord.drive.EXIT_DISPATCH_REFUSED``).  Goes straight to
      ``blocked``; costs NO attempt — pairs with a :class:`Blocked`.
    * ``dead_end``  — #2019: the drive's own exit was ``coord.drive.
      EXIT_DEAD_END`` — its dead-end predicate found the row terminal and
      unactionable (nothing active, every stage terminal, no gate transition
      available).  Same disposition as ``refused`` (straight to ``blocked``,
      NO attempt spent, pairs with a :class:`Blocked`); a distinct outcome
      only so the journal line names the right cause.
    * ``parked``    — #1891: no session, no active work, nothing landed — same
      evidence as ``retry`` — but the board's OWN current read of this
      entry's merge gate names nothing stronger than "CI checks have not
      reported yet" (``IssueFacts.merge_ci_pending``, sourced independently
      of whatever killed the drive). Goes straight to :data:`STATE_PARKED`;
      costs NO attempt — a missing verdict is not a failed one, and no
      number of relaunches changes it, only more real time. Re-checked
      every tick by the pre-pass in :func:`plan_tick`, which flips it back
      to ``waiting`` — no human, no escalation — the moment the board shows
      the gate has cleared.
    * ``reparked``  — #2347: an ALREADY-``parked`` entry, still CONFIRMED
      blocked by this tick's own fresh re-check — but that fresh check found
      the real cause has become "GitHub could not be reached", distinct from
      whatever reason the entry originally parked on. State does not change
      (stays :data:`STATE_PARKED`), no attempt spent — only ``last_reason``
      is rewritten, so an operator reading ``coord drive-queue list``/
      ``status`` sees the true cause instead of a frozen, misleading one.
    * ``retry``     — genuinely dead: no session, no active work, and past the
      startup grace window.  Costs one attempt.
    * ``exhausted`` — as ``retry``, but out of attempts; pairs with a
      :class:`Blocked`.
    * ``merge_only`` — #2350: a ``parked``/``blocked`` entry whose live gate
      re-check reads clear AND whose board-recorded Test/Review verdicts
      already show Merge is the only remaining gate (*merge_only_ready*).
      Writes NO state here — unlike ``resumed``, this does not fall through
      to step 4's launch walk at all. The key is instead carried on
      :attr:`TickPlan.merge_only` for the shell to attempt a direct
      ``coord merge --only`` from THIS tick, no relaunch, no capacity slot
      spent. The shell decides the entry's real next state from the live
      outcome of that attempt (straight to ``done`` on success; falls back
      to exactly today's ``resumed``-shaped ``STATE_WAITING`` on failure —
      see :func:`coord.commands.drive_queue._run_merge_only_candidates`),
      which is why this Reconcile's own ``updates`` stays empty: neither
      outcome is knowable at the moment :func:`plan_tick` returns.
    """

    key: str
    outcome: str  # alive | starting | held | unknown | done | refused | parked | retry | exhausted | merge_only | resumed | oscillating | gate_unreadable
    reason: str
    occupies: bool = False
    updates: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Blocked:
    """An entry to mark ``blocked`` and escalate."""

    key: str
    reason: str
    updates: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Deferral:
    """An entry whose pre-reqs aren't satisfied YET.  Position unchanged.

    ``counted=False`` marks a REPORT-ONLY deferral: an entry the walk reached
    only after a launch had already been chosen, so it never actually
    competed for a free slot.  Its ``updates`` are empty, so it mutates
    nothing — it exists purely so ``--dry-run`` can answer "and why isn't the
    rest of the queue going?" in the same breath.  Counting it would ruin the
    signal ``deferrals`` carries: *how many times this entry was passed over
    while a slot was actually available*.

    ``repo_limited=True`` marks the #1972 deferral — the entry was otherwise
    fully eligible and lost its turn only because its REPO was already at
    ``max_parallel_per_repo``.  It is flagged rather than string-matched
    because the tick has to tell that case apart from a genuine stall: a queue
    whose remaining entries are all waiting on their own repo's in-flight work
    is the queue working exactly as designed, so it raises no queue-level
    alert — the same posture the global at-capacity return takes.

    ``cordoned=True`` is #2101's twin of that flag: the entry is pinned to a
    machine that is draining for a release right now.  Same posture and same
    reason — a drain is the fleet working, lasts minutes, and ends by itself,
    so escalating it every tick is how an alert channel gets muted.  A
    separate flag rather than reusing ``repo_limited`` because the two produce
    different prose and different remedies, and a render that blames the
    repo limit for a cordon is a render that sends the operator to the wrong
    knob.

    ``backing_off=True`` is #2273's twin: the entry died at least once and is
    inside the post-death spacing :func:`_retry_backoff_reason` enforces
    before its next attempt.  Same posture again — nothing is wrong with the
    entry, no human can do anything about it faster than the clock can, and
    it ends by itself the moment the backoff elapses.  quadraui#508 spent its
    entire two-attempt budget six minutes apart, well inside a single tick
    cadence, because nothing paced the SECOND attempt; alerting on every tick
    of the pacing that now exists would just be a slower version of the same
    noise the repo-limit/cordon flags above already learned to suppress.
    """

    key: str
    reason: str
    updates: Mapping[str, Any] = field(default_factory=dict)
    counted: bool = True
    repo_limited: bool = False
    cordoned: bool = False
    backing_off: bool = False

    @property
    def benign(self) -> bool:
        """Is this a "the fleet is working" deferral rather than a stall?

        The single predicate both the queue-level alert and `render_plan`
        consult, so a future third benign cause cannot be added to one and
        forgotten in the other.
        """
        return self.repo_limited or self.cordoned or self.backing_off


@dataclass(frozen=True)
class ProbeResult:
    """The outcome of ONE ``resume_when`` run, handed back in by the shell.

    Exit 0 (``ok=True``) releases the gate; anything else — non-zero, a
    timeout, or a command that could not be spawned at all — keeps it held.
    Fail-CLOSED is the only safe default here: a gate that releases because
    its probe blew up is a gate that did not exist.
    """

    key: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class Hold:
    """One entry's deploy gate, as resolved by this tick (#1757).

    ``outcome``:

    * ``fired``    — the entry reached ``done`` on THIS tick and the gate has
      just closed the queue.  ``updates`` arms the run state.
    * ``held``     — the gate was already ``fired`` and is still closed
      (either no probe is declared, or the probe ran and failed).
    * ``released`` — the probe exited 0; the walk continues in this same tick.

    ``blocking`` is the single thing that says the gate is currently closed
    at all, so a future outcome can be added without every caller
    re-deriving the rule. ``scope`` (#2186) says how FAR a closed gate
    reaches: ``HOLD_SCOPE_ENTRY`` (the default) holds only entries whose own
    ``after=`` names this gate's key; ``HOLD_SCOPE_FLEET`` holds the entire
    tick, the pre-#2186 behaviour, kept for an explicitly-declared
    whole-fleet stop. ``stops_fleet`` is what :func:`plan_tick` actually acts
    on for the early-return branch — ``blocking`` alone is deliberately not
    enough, or an entry-scoped gate would still halt the whole queue.
    """

    key: str
    outcome: str
    reason: str
    resume_when: str = ""
    probes: int = 0
    probe_detail: str = ""
    updates: Mapping[str, Any] = field(default_factory=dict)
    scope: str = HOLD_SCOPE_ENTRY

    @property
    def blocking(self) -> bool:
        return self.outcome in ("fired", "held")

    @property
    def stops_fleet(self) -> bool:
        """Whether THIS hold, if closed, must stop the entire tick (#2186)."""
        return self.blocking and self.scope == HOLD_SCOPE_FLEET


@dataclass(frozen=True)
class QueueAlert:
    """The one queue-level record a tick may raise (see QUEUE_ALERT_REPO).

    ``command`` is the proposed fix written into the escalation record. It is
    carried here rather than derived in the shell so the alert's prose and its
    one-key remedy are decided together — a "HELD" alert whose command says
    ``coord drive-queue list`` teaches the operator to ignore the field.
    """

    reason: str
    details: tuple[str, ...] = ()
    command: str = "coord drive-queue list"


@dataclass(frozen=True)
class RollPending:
    """#2587: a fleet roll is queued for the next INTER-DRIVE GAP, not a drain.

    Set by `coord release propagate` / `coord release nightly-window` when the
    daemon host is behind and busy, instead of stopping
    `coord-drive-queue.timer` and polling a bounded drain for up to an hour
    (2026-08-22: that drain ran 60 minutes, drained nothing, rolled nothing —
    #2569's exact shape, and #2569 is *why* a timer must never be stopped to
    reach quiescence again). While this marker is live, `plan_tick` refuses to
    launch — see its ``roll_pending_reason`` parameter — but reconciliation
    (steps 1/1b) runs completely unaffected, exactly the posture
    `--reconcile-only` already gives a #2101 release cordon. The tick launches
    at most one drive per run, so the moment reconciliation leaves the queue
    with nothing occupying a slot (:attr:`TickPlan.occupied` ``== 0``) IS an
    inter-drive gap — which happens several times an hour on a busy queue, not
    never. The shell (`coord.commands.drive_queue`) is what notices that and
    fires the actual roll (``systemctl --user start --no-block
    coord-release-window.service``) — this dataclass only carries the
    decision-relevant facts, no I/O.

    Persisted as JSON by the shell (`coord.commands.drive_queue.
    write_roll_pending`) — never read or written from this module, which
    stays pure; see the module docstring.

    #2587 review: the tick that FIRES the roll (``systemctl --user start
    --no-block coord-release-window.service``) never clears this marker
    itself — a ``--no-block`` accept is proof the start request was queued,
    not that anything rolled. Only the spawned process
    (`coord.commands.release.release_nightly_window`, that unit's
    ``ExecStart=``) clears it, and only once it has actually confirmed the
    roll via `coord release propagate`. Clearing it from the tick's own
    process, in the same statement that fired the unit, raced the freshly
    spawned process out from under it on every real invocation: that process
    re-resolves its target from scratch (a PyPI lookup plus a fleet health
    gather) before it ever reads this marker, by which point the tick had
    already deleted it — so the spawned process always found nothing
    pending and just re-armed a fresh marker instead of ever propagating.
    """

    #: The version this roll is for — carried through to the alert/status
    #: prose and to the eventual `coord release propagate --target` call, so
    #: a marker set at 22:00 rolls the version resolved THEN even if a newer
    #: release lands on PyPI before the gap arrives.
    target_version: str
    #: `time.time()` when this marker was set — the TTL's epoch.
    set_at: float
    #: Free text naming who/why set it ("nightly-window" / "propagate"),
    #: threaded into the status line so an operator sees provenance, not just
    #: a bare version.
    reason: str = ""
    ttl_seconds: float = ROLL_PENDING_DEFAULT_TTL_SECONDS
    max_deferrals: int = ROLL_PENDING_DEFAULT_MAX_DEFERRALS
    #: Consecutive ticks that observed this marker still pending and the
    #: fleet still busy — bumped by the shell each tick it does NOT fire the
    #: roll. Never bumped by `plan_tick` itself (pure; no counting state).
    deferrals: int = 0
    #: #2870: the ``propagation.min_releases_behind``/``--min-behind``
    #: threshold this marker was ARMED at — `coord release nightly-window`/
    #: `coord.commands.release._ensure_roll_pending_marker` stamp the
    #: effective threshold THEIR OWN run resolved (override, else
    #: `coordinator.yml`, else 1) at the moment they write this marker.
    #: Before #2870, discharge (`_run_propagate`'s `coord release propagate`
    #: subprocess) always re-resolved its OWN threshold from scratch — the
    #: fleet default, never whatever `--min-behind` armed this marker at —
    #: so a marker armed below the fleet default (e.g. a
    #: `coord-release-window.service` ExecStart carrying `--min-behind 1`
    #: against a fleet configured `min_releases_behind: 5`) could never
    #: reach the threshold its own discharge path required and froze the
    #: queue forever. Carried through to `_run_propagate` as `--min-behind`
    #: so discharge is gated at the SAME threshold arm used. `None` for a
    #: marker written before this field existed (`from_dict`'s tolerant
    #: parse) or one whose arming run never evaluated the gate at all
    #: (`effective_min_behind <= 1`) — the discharge call then falls back to
    #: ITS OWN effective threshold, matching the pre-#2870 behaviour rather
    #: than guessing.
    min_releases_behind: int | None = None

    def expired(self, now: float) -> bool:
        """Has this marker outlived ITS OWN bound — TTL or deferral ceiling?

        Either bound alone can fail (a wedged clock defeats the TTL; a tick
        loop firing far faster than the production 3-minute cadence defeats
        the deferral count) — see the constants' own comment for why both are
        checked. `ttl_seconds <= 0` / `max_deferrals <= 0` disables that half
        of the check (unbounded), matching `--max-parallel-per-repo 0`'s
        "0 disables this ceiling" convention elsewhere in this module.
        """
        if self.ttl_seconds > 0 and (now - self.set_at) >= self.ttl_seconds:
            return True
        return self.max_deferrals > 0 and self.deferrals >= self.max_deferrals

    def describe(self) -> str:
        return f"roll pending: v{self.target_version}" + (
            f" ({self.reason})" if self.reason else ""
        )

    def to_dict(self) -> dict:
        return {
            "target_version": self.target_version,
            "set_at": self.set_at,
            "reason": self.reason,
            "ttl_seconds": self.ttl_seconds,
            "max_deferrals": self.max_deferrals,
            "deferrals": self.deferrals,
            "min_releases_behind": self.min_releases_behind,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RollPending":
        """Tolerant parse of :meth:`to_dict`'s shape — see `expired`'s note on
        why a malformed/missing bound reads as *unbounded* rather than
        raising: a marker this module cannot parse must still eventually
        clear via the shell's own fallback, never wedge the queue forever
        because a JSON file on disk got hand-edited.
        """
        target_version = str(data.get("target_version") or "").strip()
        if not target_version:
            raise ValueError("roll_pending record has no target_version")
        try:
            set_at = float(data["set_at"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("roll_pending record has no usable set_at") from None
        return cls(
            target_version=target_version,
            set_at=set_at,
            reason=str(data.get("reason") or ""),
            ttl_seconds=_as_float(data.get("ttl_seconds"), ROLL_PENDING_DEFAULT_TTL_SECONDS),
            max_deferrals=_as_int_default(
                data.get("max_deferrals"), ROLL_PENDING_DEFAULT_MAX_DEFERRALS
            ),
            deferrals=_as_int_default(data.get("deferrals"), 0),
            min_releases_behind=_as_optional_int(data.get("min_releases_behind")),
        )


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int_default(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_optional_int(value: Any) -> int | None:
    """Tolerant parse for a field that is legitimately absent (``None``),
    unlike :func:`_as_int_default`'s fields which always have a real
    fallback — see `RollPending.min_releases_behind`'s own docstring for why
    "not recorded" must stay `None` rather than silently becoming some
    default int a caller could mistake for a real threshold.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ── #2889: bound the RATE of fresh roll-pending markers ─────────────────────
#
# `RollPending.expired` bounds one marker's own lifetime, and #2607 made a
# RE-ARM of that SAME live marker preserve `set_at`/`deferrals` — so re-arming
# it for a moved target cannot dodge that bound. Neither one bounds what
# happens AFTER a marker actually reaches its bound and gets cleared (the
# tick's own expiry branch in `coord.commands.drive_queue`): the very next
# arm attempt (`coord.commands.release._ensure_roll_pending_marker` /
# `release nightly-window`'s own arm site) starts a BRAND NEW marker — a
# fresh `set_at`, `deferrals` back to 0 — with no memory of the last one.
# 2026-08-28: ten of those in ~15 hours, 49 ticks refused to launch, each
# individual marker perfectly well-behaved by its own TTL/deferral bound.
#
# `RollLedger` is the memory that survives a marker's own clear. Mirrors
# `RollPending`'s own split: this dataclass stays pure (no clock read, no
# I/O) — `coord.commands.drive_queue.read_roll_ledger`/`write_roll_ledger`/
# `reset_roll_ledger` own persistence, same as `roll_pending.json` one level
# up.
#: Past this much CUMULATIVE frozen time — summed across every marker
#: generation that expired unconfirmed since the ledger was last reset — stop
#: arming fresh markers outright and escalate loudly instead. 4x the single
#: marker TTL default: roughly "this has now failed to roll on its own four
#: separate times", clearly past "one unlucky busy night" and into "something
#: about this target cannot roll itself; an operator must look."
ROLL_LEDGER_CUMULATIVE_BOUND_SECONDS = 4 * ROLL_PENDING_DEFAULT_TTL_SECONDS
#: Minimum spacing between two FRESH markers (never between re-arms of an
#: already-live one — see `RollPending`'s own set_at-preservation, which
#: already covers that case). 15 minutes turns the observed "10 fresh arms
#: in 15h" pathology into at most one every 15 minutes even in the
#: worst case where every other guard (the cumulative bound, #2889 item 2's
#: queue-provably-busy refusal) somehow keeps missing.
ROLL_LEDGER_MIN_ARM_INTERVAL_SECONDS = 900.0


@dataclass(frozen=True)
class RollLedger:
    """#2889: cumulative bookkeeping for fresh `RollPending` markers, keyed
    to nothing but "the current roll campaign" — it persists across a
    marker's own clear (expiry) and is reset only on a CONFIRMED roll or
    explicit operator intervention (`coord drive-queue cancel-roll`). See
    the module comment just above for why this exists alongside
    `RollPending.expired`, not instead of it.
    """

    #: Sum of ``now - set_at`` for every marker generation that reached its
    #: OWN bound (TTL or deferral ceiling) without ever confirming a roll,
    #: accumulated since this ledger was last reset. A marker that WAS
    #: confirmed rolled never contributes here — see `reset_roll_ledger`.
    cumulative_frozen_seconds: float = 0.0
    #: How many DISTINCT fresh markers (never a re-arm of a still-live one)
    #: have expired unconfirmed since the ledger was last reset.
    marker_count: int = 0
    #: `time.time()` when the MOST RECENT marker generation reached its own
    #: bound and was cleared unconfirmed (`record_expiry`) — what
    #: `seconds_until_next_arm` measures against. Deliberately the CLEAR
    #: time, not the ARM time a first draft of this ledger used: a marker
    #: that lives out its own full TTL (3600s default) before expiring has,
    #: by the time it clears, already outlasted any reasonable rate-limit
    #: window measured from when it was ARMED — measuring from set_at would
    #: let the very re-arm this bound exists to catch (one right after
    #: natural TTL expiry) sail through every time. Never touched by an arm
    #: (successful or refused) — only an expiry moves this clock.
    last_expired_at: float = 0.0

    @property
    def escalated(self) -> bool:
        """Past the cumulative bound — refuse every further fresh arm until
        an operator clears this ledger (`coord drive-queue cancel-roll`)."""
        return self.cumulative_frozen_seconds >= ROLL_LEDGER_CUMULATIVE_BOUND_SECONDS

    def seconds_until_next_arm(self, now: float) -> float:
        """How much longer the rate limit holds, or ``0.0`` once it has
        cleared — never negative, so a caller can test ``<= 0`` with no
        separate "already elapsed" branch. ``last_expired_at <= 0`` (no
        marker has ever expired unconfirmed yet, or a ledger from before
        this field existed) reads as "no wait" — a ledger with no expiry
        history has nothing to be too close to."""
        if self.last_expired_at <= 0:
            return 0.0
        remaining = ROLL_LEDGER_MIN_ARM_INTERVAL_SECONDS - (now - self.last_expired_at)
        return max(0.0, remaining)

    def record_expiry(self, pending: "RollPending", *, now: float) -> "RollLedger":
        """New ledger folding in *pending*'s lived duration and bumping the
        rate-limit clock — called once, right before a marker that reached
        its own bound unconfirmed is cleared (never for one that rolled —
        that path resets instead, via `coord.commands.drive_queue.
        reset_roll_ledger`)."""
        return replace(
            self,
            cumulative_frozen_seconds=(
                self.cumulative_frozen_seconds + max(0.0, now - pending.set_at)
            ),
            marker_count=self.marker_count + 1,
            last_expired_at=now,
        )

    def to_dict(self) -> dict:
        return {
            "cumulative_frozen_seconds": self.cumulative_frozen_seconds,
            "marker_count": self.marker_count,
            "last_expired_at": self.last_expired_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RollLedger":
        """Tolerant parse, same posture as `RollPending.from_dict`: a ledger
        this cannot make sense of must read as "no history yet", never as a
        reason to wedge future arms forever over a hand-edited/corrupt file."""
        return cls(
            cumulative_frozen_seconds=_as_float(data.get("cumulative_frozen_seconds"), 0.0),
            marker_count=_as_int_default(data.get("marker_count"), 0),
            last_expired_at=_as_float(data.get("last_expired_at"), 0.0),
        )


@dataclass(frozen=True)
class TickPlan:
    """Everything one tick decided, and nothing it has done yet."""

    reconciles: tuple[Reconcile, ...] = ()
    launch: QueueEntry | None = None
    # #2350: entries the walk above marked ``merge_only`` — the shell attempts
    # `coord merge --only` for each directly, no relaunch, no capacity spent.
    # Unlike `launch` (one slot, one entry), every eligible entry gets one:
    # nothing here competes for capacity, so there is nothing to ration.
    merge_only: tuple[QueueEntry, ...] = ()
    blocked: tuple[Blocked, ...] = ()
    deferrals: tuple[Deferral, ...] = ()
    holds: tuple[Hold, ...] = ()
    alert: QueueAlert | None = None
    occupied: int = 0
    capacity: int = 0
    # #1972: the same occupancy, broken down by repo, plus the per-repo ceiling
    # it is measured against.  `repo_occupied` holds only repos that actually
    # occupy something (a repo with no live drive is simply absent, not 0), and
    # it is the PRE-launch reading — the same instant `occupied` is taken — so
    # the two never disagree.  `repo_capacity == 0` means no per-repo ceiling
    # was applied at all, which is what a `TickPlan` built by hand (or by a
    # pre-#1972 caller) gets, and what makes `render_plan` fall back to the
    # original single-line capacity render.
    repo_occupied: Mapping[str, int] = field(default_factory=dict)
    repo_capacity: int = 0
    # #2101: non-empty when THIS host is under a release cordon, in which case
    # nothing launched this tick and `launch` is guaranteed None.  Carried as
    # the cordon's own sentence ("cordoned: draining for v0.5.31") rather than
    # a bool, because a queue that stops with no stated reason is the failure
    # the cordon mechanism exists to stop repeating.
    cordon_reason: str = ""
    # #2314: non-empty when THIS host's own `coord` is an editable checkout
    # that has drifted off its default branch (see `editable_drift` on
    # `plan_tick` and `coord.cli._editable_checkout_drift`), in which case
    # nothing launched this tick and `launch` is guaranteed None — same
    # shape as `cordon_reason` and for the same reason: a queue that stops
    # must say why in a field the shell can render, not just a log line
    # nobody is watching an unattended tick for.
    drift_reason: str = ""
    # #2587: non-empty when a fleet roll is queued for the next inter-drive
    # gap (see `RollPending`), in which case nothing launched this tick and
    # `launch` is guaranteed None — same shape as `cordon_reason`/
    # `drift_reason` and for the same reason. Carries `RollPending.describe()`
    # ("roll pending: v0.5.230 (nightly-window)") rather than a bool so the
    # render names WHICH version, the same "say why or it reads as a mystery"
    # rule the other two follow. Reconciliation is unaffected by this field —
    # see `RollPending`'s own docstring for why that is the whole mechanism.
    roll_pending_reason: str = ""

    @property
    def free_slots(self) -> int:
        return max(0, self.capacity - self.occupied)

    @property
    def held(self) -> Hold | None:
        """The first currently-closed gate, if any (lowest position wins).

        Scope-agnostic (#2186) — this is "is SOME gate closed", not "is the
        queue stopped"; for the latter check ``.stops_fleet`` on the result,
        which is what :func:`render_plan` does before printing the whole-queue
        "no launch — HELD" line. An entry-scoped gate still shows up here even
        though it only holds its own dependents.
        """
        for item in self.holds:
            if item.blocking:
                return item
        return None

    def writes(self) -> list[tuple[str, Mapping[str, Any]]]:
        """``(key, updates)`` for every row this plan mutates, in apply order.

        The launch is NOT here: its row is written by the shell only after
        ``coord drive --tmux`` has confirmed a live session, so a launch that
        dies immediately is recorded as a failed attempt rather than as a
        running entry (#1606 makes that exit code trustworthy).

        Holds come straight after reconciles: the reconcile that moved an
        entry to ``done`` and the hold that fires off it touch the same row,
        and the gate's run state must land after the state that triggered it.
        """
        out: list[tuple[str, Mapping[str, Any]]] = []
        for item in (*self.reconciles, *self.holds, *self.blocked, *self.deferrals):
            if item.updates:
                out.append((item.key, dict(item.updates)))
        return out


# ── cycle detection ──────────────────────────────────────────────────────────


def find_cycle(edges: Mapping[str, Sequence[str]]) -> list[str] | None:
    """Return one cycle in *edges* (``key -> pre-req keys``), or ``None``.

    Same three-colour DFS as ``coord.milestone_order._check_cycles`` — the
    validation posture ``coord milestone write-order`` applies to ``## Work
    order``, applied to the same shape of graph.  Edges pointing outside
    *edges* (a pre-req that isn't itself queued) are ignored: they cannot
    close a loop.
    """
    white, gray, black = 0, 1, 2
    color = {key: white for key in edges}

    def visit(node: str, path: list[str]) -> list[str] | None:
        color[node] = gray
        path.append(node)
        for dep in edges.get(node, ()):  # noqa: SIM118 — Mapping, not dict
            if dep not in color:
                continue
            if color[dep] == gray:
                return path[path.index(dep):] + [dep]
            if color[dep] == white:
                found = visit(dep, path)
                if found is not None:
                    return found
        path.pop()
        color[node] = black
        return None

    for key in edges:
        if color[key] == white:
            found = visit(key, [])
            if found is not None:
                return found
    return None


def validate_enqueue(
    entries: Sequence[QueueEntry],
    repo: str,
    issue: int,
    after: Sequence[str],
) -> None:
    """Refuse an ``add`` that would be malformed, BEFORE anything is written.

    Checks, in the order an operator is most likely to hit them: a self-edge,
    then a cycle across the queue as it would look *after* this write (the
    entry being added replaces its own current edges, because ``enqueue``
    upserts).  Raises :class:`QueueError`; the caller writes nothing.

    A pre-req that isn't queued is NOT an error here — the point of `--after`
    is often "run this after that other thing merges", and that thing may
    never be queued at all.  Whether such an edge is satisfiable is a *tick*
    question (:func:`plan_tick`), answered against the board.
    """
    key = entry_key(repo, issue)
    normalised = [str(a) for a in after]
    if key in normalised:
        raise QueueError(f"{key} cannot depend on itself")

    edges: dict[str, list[str]] = {
        e.key: list(e.after) for e in entries if e.key != key
    }
    edges[key] = normalised
    cycle = find_cycle(edges)
    if cycle is not None:
        raise QueueError("dependency cycle: " + " -> ".join(cycle))


# ── pre-req resolution ───────────────────────────────────────────────────────


def _gate_defer_reason(dep: str, hold: Hold) -> str:
    """The per-tick reason a dependent defers on an entry-scoped gate (#2186).

    Mirrors ``_hold_alert``'s probe detail (attempt count, last failure) so
    an operator reading ``coord drive-queue list`` gets the SAME picture for
    an entry-scoped hold that the old fleet-wide alert gave for everything —
    just attributed to the one entry actually waiting on it. Built fresh
    every call, from THIS tick's ``Hold``, which is what keeps it live rather
    than a frozen snapshot (see the #2186 incident's stale ``last:`` text).
    """
    reason = f"waiting on {dep}'s deploy gate — {hold.reason}"
    if hold.resume_when:
        if hold.probes:
            reason += f" (resume-when attempt {hold.probes} failed"
            if hold.probe_detail:
                reason += f": {hold.probe_detail}"
            reason += ")"
        else:
            reason += " (resume-when not probed yet)"
    else:
        reason += " (no --resume-when probe: release manually)"
    return f"{reason} (#2186)"


@dataclass(frozen=True)
class _Verdict:
    satisfied: bool
    unsatisfiable: bool = False
    reason: str = ""


def _resolve_prereqs(
    entry: QueueEntry,
    board: BoardView,
    states: Mapping[str, str],
    cycle_keys: Mapping[str, str],
    held_gates: Mapping[str, Hold] | None = None,
    live_prereq_terminal: Mapping[str, bool] | None = None,
) -> _Verdict:
    """Decide whether *entry* may launch now.

    Three outcomes, and the difference between the last two is the whole
    point: an *unsatisfied* pre-req will plausibly clear on a later tick, so
    the entry defers and keeps its position; an *unsatisfiable* one never
    will, so waiting forever is the silent-stall failure mode this feature
    exists to remove — it blocks and escalates instead.

    *held_gates* (#2186) maps a key to its currently-closed :class:`Hold`,
    for every gate blocking this tick regardless of scope — an ENTRY-scoped
    gate never reaches this function's caller through `plan_tick`'s early
    return (only a FLEET-scoped one does), so by the time a caller is asking
    this question the only gates left standing are the entry-scoped ones this
    check exists for. Checked BEFORE `facts.landed`, deliberately: the whole
    point of a deploy gate is that its own entry reconciling to `done`
    (merged) is NOT the same fact as "safe to launch a dependent" (live) — an
    unconditional `facts.landed` short-circuit here would silently defeat the
    gate the instant its entry finished, independent of hold_state entirely.

    *live_prereq_terminal* (#2602) maps a dep key to ``True`` when a LIVE
    re-check taken THIS tick (``coord.commands.drive_queue.
    _fetch_live_prereq_terminal``, the same positive-liveness test
    ``coord.overlap_predict``'s ``terminal_checker`` uses for the sibling
    ``[branch]`` half of #2602) confirms the issue is closed or its PR
    merged. This is the recovery half: the cached ``board.facts(dep)`` this
    function checks first is a periodic ``/board`` build, not a live read,
    and a pre-req that has JUST left the queue (merged, issue closed) can
    outrun that cache — reading as "not queued, not merged, not open" even
    though the dep is, in fact, already done. Before this existed that
    verdict was unconditionally unsatisfiable and permanent
    (coord-portal#145/#149/#150, 2026-08-22): a dep absent here (no live
    check ran, or it ran and came back inconclusive) falls through to
    exactly that pre-#2602 behaviour — never a false "satisfied".

    #2850 widens WHERE this is consulted: originally only the final branch
    below (*dep* absent from the queue entirely) checked it. But a dep can
    just as easily be PRESENT in the queue under a stale or outright bogus
    row — the #2850 incident: a drive that exited 0 having merged got
    requeued into a `running` row that never dies again on its own — and
    that shape used to return "waiting"/"will never satisfy" straight out
    of the ``dep_state is not None`` branch, never reaching this check at
    all. So it is now ALSO consulted there, for every ``dep_state`` other
    than ``STATE_DONE``: a pre-req that demonstrably landed must satisfy
    the dependent whatever its own queue row happens to claim.
    """
    if entry.key in cycle_keys:
        return _Verdict(False, True, cycle_keys[entry.key])

    for dep in entry.after:
        hold = (held_gates or {}).get(dep)
        if hold is not None:
            return _Verdict(False, False, _gate_defer_reason(dep, hold))
        facts = board.facts(dep)
        if facts.landed:
            continue
        dep_state = states.get(dep)
        if dep_state is not None:
            if dep_state == STATE_DONE:
                # #2715: the queue's OWN row for *dep* already knows it
                # landed — every `STATE_DONE` write in this module
                # (`_reconcile_running`'s `facts.landed` branch, the
                # `parked`/`blocked`/`failed` #2055 re-check, and step 4's
                # own `facts.landed` short-circuit just below) is gated on a
                # landed fact from `board.facts`, and the one write outside
                # this module — `_run_merge_only_candidates` in
                # `coord.commands.drive_queue` — only fires after its own
                # `coord merge --only` attempt lands, confirmed by reading
                # the merge queue's row back as `MERGED`
                # (`_merge_only_landed`). `done` is therefore never written
                # speculatively, so it is as trustworthy as `facts.landed`
                # itself — and unlike `facts.landed`, it does not depend on
                # the `issues` cache's own refresh cadence (observed ~10m
                # behind) to reflect a merge the queue performed itself this
                # same tick. Treating it as satisfied here is what lets a
                # dependent resume on the tick right after its pre-req's row
                # flips to `done`, instead of the tick after the cache
                # independently rediscovers the same fact.
                continue
            if (live_prereq_terminal or {}).get(dep):
                # #2850: *dep*'s OWN queue row says it is still in play
                # (``running``, ``waiting``, ``blocked``, ...) — but a live
                # re-check taken THIS tick confirms it is, in fact, already
                # closed or merged. Before this, only a dep ABSENT from the
                # queue entirely ever reached the #2602 recovery check below;
                # a dep still SITTING in the queue under a stale/bogus row
                # (the #2850 incident: a drive that exited 0 having merged,
                # requeued into a bogus `running` row) short-circuited to
                # "waiting"/"will never satisfy" here and shadowed the very
                # recovery check meant to catch exactly this. A dependent
                # must never be blocked by a pre-req that demonstrably
                # landed, whatever its queue row claims.
                continue
            if dep_state in (STATE_BLOCKED, STATE_FAILED):
                return _Verdict(
                    False,
                    True,
                    f"pre-req {dep} is queued but {dep_state} — it will never satisfy",
                )
            return _Verdict(
                False, False, f"waiting on {dep} (queued, {dep_state})"
            )
        if facts.open:
            return _Verdict(
                False, False, f"waiting on {dep} (open, not queued)"
            )
        if facts.active_work:
            # No `issues` row (the standalone `serialize_board` payload ships
            # assignments only) but live work-like assignment rows — the issue
            # is demonstrably in flight, so this defers rather than blocking.
            return _Verdict(
                False, False, f"waiting on {dep} (work in flight, not queued)"
            )
        if (live_prereq_terminal or {}).get(dep):
            # #2602 recovery half: the cached board doesn't (yet) show *dep*
            # landed, but a live re-check just taken this tick confirms it
            # already is (issue closed or PR merged) — read as satisfied,
            # not blocked. An `--after` naming a pre-req that has already
            # left the queue must never wait on `/board`'s own refresh
            # cadence to catch up; see the docstring above.
            continue
        return _Verdict(
            False,
            True,
            f"pre-req {dep} is not queued, not merged and not open on the board "
            f"(unknown issue, or the board has not synced it — try `coord sync`)",
        )
    return _Verdict(True)


@dataclass(frozen=True)
class BlockedAfterDiagnosis:
    """How #2183's rendering should treat a terminal row's `after=` graph.

    ``unsatisfied`` is *entry*'s declared ``after=`` list with every
    now-landed pre-req dropped. ``dependency_reason`` is the CURRENT
    ``_resolve_prereqs`` verdict against that same graph — ``''`` when the
    graph is not (or no longer) why the entry is stuck.

    ``unsatisfiable`` (#2756) carries ``_Verdict.unsatisfiable`` through
    instead of collapsing it: ``dependency_reason`` alone cannot tell "a
    named pre-req is itself dead, or a cycle exists — this will never
    clear on its own" apart from "merely waiting on a pre-req that is
    still in flight — an ordinary, plausibly-temporary deferral". Both
    produce a non-empty ``dependency_reason``; only the former should keep
    a row `blocked`. ``False`` whenever ``dependency_reason`` is empty
    (nothing to be unsatisfiable about).
    """

    unsatisfied: tuple[str, ...] = ()
    dependency_reason: str = ""
    unsatisfiable: bool = False


def diagnose_blocked_after(
    entry: QueueEntry,
    board: BoardView,
    states: Mapping[str, str],
    cycle_keys: Mapping[str, str],
    live_prereq_terminal: Mapping[str, bool] | None = None,
) -> BlockedAfterDiagnosis:
    """Re-check a `blocked`/`failed` row's `after=` graph against the CURRENT
    board, fresh on every render (#2183).

    ``entry.after`` is what ``add`` declared, frozen at enqueue time — and a
    `blocked`/`failed` entry's own state never re-evaluates it again
    (:data:`TERMINAL_QUEUE_STATES` is exactly "the tick will not look at this
    row again"). By the time an operator reads the row, a pre-req that has
    since merged is stale information still rendered as if current — the
    quadraui#542 incident this closes: the row's `after=` named a pre-req
    that had merged an hour earlier, reading as "blocked on a dependency
    that is already satisfied" when the real cause was an unrelated red
    slice PR.

    Two things are recomputed, never read off the entry's own frozen
    ``last_reason``:

    * which of ``entry.after`` are still NOT landed — the merged ones drop
      out entirely; a satisfied ``after=`` entry displayed on a terminal row
      is not a dependency, it is a caption for a fact that no longer holds.
      #2715: a dep whose OWN queue row already reads :data:`STATE_DONE`
      counts as landed here too, same as ``board.facts(dep).landed`` — see
      :func:`_resolve_prereqs`'s matching case for why that row is trustworthy
      without waiting on the ``issues`` cache to independently confirm it;
    * whether the CURRENT pre-req graph is even a plausible cause —
      delegated to :func:`_resolve_prereqs`, the exact function a live tick
      uses to decide this, so a render can never disagree with what the tick
      itself would conclude. An empty ``dependency_reason`` means the
      answer is no: every named pre-req is now satisfied (or the entry
      declared none), so whatever originally blocked this row — a red build,
      an exhausted retry budget, a refused pre-dispatch guard — was NOT its
      ``after=`` list, and the row's own ``last_reason`` is the real story.
      A non-empty ``dependency_reason`` splits further via ``unsatisfiable``
      (#2756): a dep that is itself dead (`blocked`/`failed`), unknown to
      the board, or part of a cycle keeps the graph a genuine, permanent
      cause; a dep that is merely still queued/open/in-flight is an
      ordinary deferral that happens to be phrased the same way a terminal
      row's caption is — see :func:`_reconcile_blocked_after`, which acts
      on this distinction.

    *live_prereq_terminal* (#2602) is the same live-recheck mapping
    :func:`_resolve_prereqs` takes — a dep it confirms landed drops out of
    ``unsatisfied`` here too, for the identical reason: a pre-req that a live
    read just confirmed closed/merged is not a caption worth showing as
    still-pending just because the cached board hasn't caught up yet.
    """
    live_terminal = live_prereq_terminal or {}
    unsatisfied = tuple(
        dep for dep in entry.after
        if not board.facts(dep).landed
        and not live_terminal.get(dep)
        and states.get(dep) != STATE_DONE
    )
    if not entry.after:
        return BlockedAfterDiagnosis(unsatisfied)
    verdict = _resolve_prereqs(
        entry, board, states, cycle_keys, held_gates={},
        live_prereq_terminal=live_prereq_terminal,
    )
    return BlockedAfterDiagnosis(
        unsatisfied,
        "" if verdict.satisfied else verdict.reason,
        unsatisfiable=verdict.unsatisfiable,
    )


# Substrings unique to `_resolve_prereqs`'s two UNSATISFIABLE verdict shapes
# (the two f-strings a few lines up) — both are shapes #2362's blocked-sweep
# resume check (`_reconcile_blocked_after` below) may act on:
#
# * "it will never satisfy" — dep itself is queued and `blocked`/`failed`.
#   Undone by that dep later reaching `facts.landed` (a retry that lands).
# * "not queued, not merged and not open on the board" — dep is unknown to
#   the cached board RIGHT NOW. #2602: this used to be permanent — a cached
#   `/board` build lags a merge/close by however long its refresh cadence is,
#   and board facts alone cannot tell "genuinely bogus issue number" apart
#   from "just merged, cache hasn't caught up". Undone by a LIVE re-check
#   (`live_prereq_terminal`, threaded through `_reconcile_blocked_after` →
#   `diagnose_blocked_after` → `_resolve_prereqs`) confirming the dep closed
#   or merged — the same positive liveness test `coord.overlap_predict` uses
#   for the sibling `[branch]` half of #2602.
#
# Deliberately does NOT match the cycle verdict ("dependency cycle: ..."): a
# cycle is a structural fact about the queue's edges that no amount of
# re-checking — live or cached — can ever undo. Matching it would be exactly
# the "no evidence, guessing" mistake `_blocked_gate_reading` refuses to make.
_UNSATISFIABLE_PREREQ_MARKER = "it will never satisfy"
_UNKNOWN_PREREQ_MARKER = "not queued, not merged and not open on the board"


def _is_unsatisfiable_prereq_reason(text: str | None) -> bool:
    """Whether *text* is one of `_resolve_prereqs`'s two unsatisfiable
    verdict shapes — see :data:`_UNSATISFIABLE_PREREQ_MARKER` and
    :data:`_UNKNOWN_PREREQ_MARKER`.
    """
    if not text:
        return False
    return _UNSATISFIABLE_PREREQ_MARKER in text or _UNKNOWN_PREREQ_MARKER in text


def is_unsatisfiable_prereq_reason(text: str | None) -> bool:
    """Public alias for :func:`_is_unsatisfiable_prereq_reason`.

    `coord drive-queue list`'s rendering (#2404) needs to tell whether a
    `blocked` row's frozen ``last_reason`` is one of the shapes
    :func:`_reconcile_blocked_after` (#2362, widened #2602) auto-resumes —
    once every named pre-req lands, or a live re-check confirms it already
    has — so it can stop telling the operator the ``after=`` graph "is never
    re-checked on its own" for a row where that has stopped being true — see
    ``coord.commands.drive_queue._BLOCKED_AFTER_NOTE``.
    """
    return _is_unsatisfiable_prereq_reason(text)


# ── #2944: the guaranteed-false wait ─────────────────────────────────────────
#
# An entry with `attempts == 0` has never been dispatched: no `coord drive`
# ever ran for it, so it has no branch, no PR, and no merge-queue row — and
# never can have one, because nothing was ever built. A `blocked` or `parked`
# entry in that state is not waiting on a gate that MIGHT clear; it is
# waiting on a gate that structurally cannot exist. Whatever swept this row
# last (#2230's merge-gate probe, #2935's after= graph, anything future)
# cannot ever answer "yes" for it — the only exit is an operator's `remove` +
# `add`.
#
# Deliberately independent of #2935/#2230: those fix (or will fix) individual
# sweeps so they stop MISTAKING this shape for a real probe target. This
# predicate does not care which sweep produced the block, or whether one
# exists at all — it is the backstop that makes the NEXT undiscovered
# instance of "a sweep declines to guess and therefore never terminates"
# visible, instead of silent for another 32 hours (claude-coordinator#2900/
# #2907: 207 and 186 deferrals, 0 attempts, ~10h and 22.7h blocked
# respectively, `alert: (none)` the entire time — see #2944).
#
# `min_deferrals` is grace, not evidence: `deferrals` on a `blocked`/`parked`
# row is whatever it had accumulated before the transition (subsequent ticks
# do not bump it further — neither `_reconcile_blocked` nor
# `_reconcile_blocked_unreadable` touches `deferrals`), so a fresh entry that
# races straight from `waiting` to `blocked` on its very first tick reads
# `deferrals=0` here regardless of how long it then sits. ~5 ticks (~15
# minutes at the fleet's ~3-minute cadence) is enough that a row flagged here
# has genuinely been sitting, not merely landed there this instant.
UNREACHABLE_WAIT_MIN_DEFERRALS = 5


@dataclass(frozen=True)
class UnreachableWait:
    """One queue entry stuck in the #2944 guaranteed-false wait.

    See :func:`detect_unreachable_waits` for the predicate. Carries just
    enough for a caller to build an operator-facing message without
    re-deriving anything: the key names the entry, `deferrals` is the
    evidence a genuine transient wouldn't have, and `last_reason` is
    whatever the last sweep to touch this row recorded — useful context,
    never re-interpreted here.

    `dependents` (#2978) is the count of OTHER queue entries whose `after=`
    names this one — computed against the full entry set the caller passed
    to :func:`detect_unreachable_waits`, not just the flagged subset, so it
    is accurate even though those dependents themselves never appear in the
    result (see the predicate's own docstring for why). `0` for an entry
    nothing is chained behind.
    """

    key: str
    state: str
    deferrals: int
    last_reason: str
    dependents: int = 0


def detect_unreachable_waits(
    entries: Iterable[QueueEntry],
    *,
    min_deferrals: int = UNREACHABLE_WAIT_MIN_DEFERRALS,
) -> list[UnreachableWait]:
    """Entries sitting in the #2944 guaranteed-false wait.

    Two independent shapes, either of which qualifies a `blocked`/`parked`
    entry — both provably have no branch/PR/merge-queue row for any sweep to
    ever form an opinion about, which is the actual invariant this predicate
    is checking, not "attempts == 0" as a proxy for it (#2978 found the proxy
    wrong for the second shape):

    * ``attempts == 0`` and ``deferrals > min_deferrals`` — never dispatched
      at all. The original #2944 shape.
    * ``attempts > 0``, ``state == blocked``, and the entry's own
      `last_reason` carries #2273's "no assignment was ever created for this
      run" marker (:func:`_is_dispatch_failure_reason`) — every dispatch
      attempt died before `coord assign` produced a board-visible row, and
      the entry has now exhausted its retry budget (the only way to reach
      `blocked` with that reason: see `_reconcile_running`'s `exhausted`
      branch). Exhaustion is its own grace period — there is no next attempt
      left to wait out — so `min_deferrals` is not applied here.

    A `blocked`/`parked` entry whose `last_reason` is instead one of
    `_resolve_prereqs`'s unsatisfiable-`after=` verdict shapes
    (:func:`_is_unsatisfiable_prereq_reason`) is excluded UNCONDITIONALLY,
    regardless of attempts/deferrals — #2978: that shape is
    :func:`_reconcile_blocked_after`'s to resolve (#2362, widened #2756),
    not an operator's, and flagging it here directly contradicts that
    function's own documented behaviour. This is what keeps a chain's
    dependents (each blocked with "pre-req X is queued but blocked — it will
    never satisfy") out of the result entirely, leaving only the root that
    actually needs a human.

    Pure and cheap (a predicate over already-loaded rows, no clock, no I/O),
    so the same function backs both `coord drive-queue status`'s `alert:`
    line (`coord.commands.drive_queue._queue_alert`) and the `coord doctor`
    WARN (`coord.health.checks.wedged_drive_queue`) — one definition of
    "wedged", not two that could drift apart.
    """
    entries = list(entries)
    dependents_of: dict[str, int] = {}
    for e in entries:
        for dep in e.after:
            dependents_of[dep] = dependents_of.get(dep, 0) + 1

    waits: list[UnreachableWait] = []
    for e in entries:
        if e.state not in (STATE_BLOCKED, STATE_PARKED):
            continue
        if _is_unsatisfiable_prereq_reason(e.last_reason):
            continue
        if e.attempts == 0:
            if e.deferrals <= min_deferrals:
                continue
        elif not (e.state == STATE_BLOCKED and _is_dispatch_failure_reason(e.last_reason)):
            continue
        waits.append(
            UnreachableWait(
                key=e.key,
                state=e.state,
                deferrals=e.deferrals,
                last_reason=e.last_reason,
                dependents=dependents_of.get(e.key, 0),
            )
        )
    return waits


def unreachable_wait_alert(waits: Sequence[UnreachableWait]) -> QueueAlert:
    """The queue-level record #2944's predicate raises, in :func:`_hold_alert`'s
    shape — a message naming the entry (or entries), the evidence that
    qualified it, and the ONE remedy that actually clears it.

    ``add`` alone does not clear ``blocked`` (the queue list already
    documents this as a footgun elsewhere) — only ``remove`` then ``add``
    does, so that is the command surfaced here rather than the generic
    ``coord drive-queue list`` other alerts fall back on; naming the wrong
    remedy on a state this non-obvious defeats the point of alerting at all.
    Named INLINE in ``reason`` (not only in ``command``, which
    ``drive-queue status``'s plain-text rendering never echoes — only its
    ``gate_readings``/``details`` lines are printed) so the remedy is visible
    wherever this alert's ``reason`` is, not just to a caller that also reads
    the structured field.

    #2978: each name also carries its `dependents` count when non-zero — the
    entries chained behind a flagged root never appear in `waits` (see
    `detect_unreachable_waits`), so without this an operator has no way to
    know a fix to the one row named here will also resolve N others they
    never see mentioned. Explicitly says those dependents need no remedy of
    their own, since #2944's original wording ("for each entry named above")
    read, on a real incident, as an instruction to `remove`+`add` entries
    that were never named — but easy to mis-generalize to "the dependents
    too" by an operator who does not already know #2756 self-heals them.
    """
    def _name(w: UnreachableWait) -> str:
        base = f"{w.key} ({w.state}, {w.deferrals} deferrals)"
        if w.dependents:
            plural = "ies" if w.dependents != 1 else "y"
            base += (
                f" — {w.dependents} dependent entr{plural} chained behind it "
                "will self-heal automatically once this clears (#2756), no "
                "remedy needed on them"
            )
        return base

    names = ", ".join(_name(w) for w in waits[:5])
    more = ", ..." if len(waits) > 5 else ""
    plural = len(waits) != 1
    return QueueAlert(
        reason=(
            f"{len(waits)} queue entr{'ies' if plural else 'y'} stuck in a "
            "guaranteed-false wait — blocked/parked with no branch/PR/"
            "merge-queue row any sweep could ever act on (never dispatched, "
            "or every dispatch attempt died before creating an assignment); "
            f"no sweep can ever clear this on its own: {names}{more}. Fix: "
            "coord drive-queue remove <repo> <issue> && "
            "coord drive-queue add <repo> <issue> for each ROOT entry named "
            "above — never on a dependent chained behind one."
        ),
        details=tuple(
            f"{w.key}: {w.state}, {w.deferrals} deferrals, "
            f"dependents={w.dependents}, last_reason={w.last_reason!r}"
            for w in waits
        ),
        command=(
            "coord drive-queue remove <repo> <issue> && "
            "coord drive-queue add <repo> <issue> — for each ROOT entry "
            "named above; any dependents chained behind it self-heal on "
            "their own (#2756)"
        ),
    )


def _reconcile_blocked_after(
    entry: QueueEntry,
    board: BoardView,
    states: Mapping[str, str],
    cycle_keys: Mapping[str, str],
    live_prereq_terminal: Mapping[str, bool] | None = None,
) -> Reconcile | None:
    """#2362: resume a `blocked` entry whose ONLY cause was an unsatisfiable
    `after=` pre-req, once every named pre-req has since landed. #2756:
    ALSO resume it the moment the specific unsatisfiable condition clears
    even while OTHER named pre-reqs remain merely in flight — the entry
    returns to `waiting`, where step 4's ordinary deferral machinery takes
    over and produces a live, accurate reason instead of the frozen,
    now-false one this row was blocked with.

    `_resolve_prereqs` (called from step 4's launch walk) blocks a `waiting`
    entry the instant one of its pre-reqs is itself `blocked`/`failed`, OR
    (#2602) unknown to the cached board — correctly, since waiting forever
    on a dead or stale-cached pre-req is the silent-stall failure mode that
    check exists to prevent. But once blocked, the entry is terminal for
    dispatch (:data:`TERMINAL_QUEUE_STATES`) and drops out of step 4
    entirely — nothing calls `_resolve_prereqs` for it again, so even after
    the pre-req itself reaches `done` the entry stays `blocked` forever with
    a now-stale reason. This closes that gap by giving `blocked` entries the
    SAME re-check `_reconcile_blocked` gives a merge-gate block, scoped to
    the `after=` cause specifically.

    Returns ``None`` — nothing to report, nothing to write — unless ALL of:

    * *entry* declared a non-empty ``after=`` (nothing to re-derive
      otherwise);
    * *entry*'s own ``last_reason`` IS one of `_resolve_prereqs`'s two
      unsatisfiable verdict shapes (:func:`_is_unsatisfiable_prereq_reason`:
      "it will never satisfy", or #2602's "not queued, not merged and not
      open on the board") — never a permanent block
      (:func:`is_permanent_block_reason`: #1844's guard refusal or #2019's
      dead end), a dispatch-time failure, or any other cause. This is the
      guard against the false-resume the issue explicitly warns against: an
      entry that merely HAS an `after=` list but was blocked for an unrelated
      reason must be left exactly as `_reconcile_blocked` would leave it;
    * re-deriving the verdict fresh against the CURRENT board — via
      :func:`diagnose_blocked_after`, the SAME function #2183's `coord
      drive-queue list`/`status` rendering already uses, so this can never
      disagree with what an operator is shown — is NOT unsatisfiable
      (:attr:`BlockedAfterDiagnosis.unsatisfiable`). That covers two cases,
      both a legitimate resume back to `waiting`: every named pre-req now
      reads `facts.landed` (OR *live_prereq_terminal* confirms it live, OR
      the dep's own queue row already reads `STATE_DONE`, #2715) — the
      original #2362 case — AND #2756's partial case, where the SPECIFIC
      dep that made the verdict unsatisfiable has cleared while other named
      pre-reqs are still merely queued/open/in-flight. A verdict that is
      STILL unsatisfiable — a (possibly different) dep still `blocked`/
      `failed`, still unknown to the board, or a genuine dependency cycle
      (:data:`_UNSATISFIABLE_PREREQ_MARKER` never matches a cycle verdict,
      and `_resolve_prereqs` marks cycles unsatisfiable too, so this check
      naturally declines to resume one — no re-check can ever undo a cycle)
      — leaves the entry blocked.

    *live_prereq_terminal* (#2602) is threaded straight through to
    :func:`diagnose_blocked_after` / :func:`_resolve_prereqs` — see their
    docstrings. This is what actually closes the "not queued, not merged and
    not open" shape: without it, that verdict has no way to distinguish a
    genuinely bogus pre-req from one that merely left the queue faster than
    the cached board could record it, so a fresh `diagnose_blocked_after`
    call with no live evidence reaches the identical stale verdict and this
    function correctly declines to resume. Absent (no live check ran this
    tick) is exactly the pre-#2602 behaviour for this shape — a blocked entry
    that never CACHE-resolves needs an operator `remove` + `add`, same as
    always.

    On resume: `state` flips to `waiting`, `attempts` resets to 0 (a fresh
    start, exactly like #2230's gate-cleared resume — nothing about being
    blocked on a now-landed pre-req should cost this entry its launch
    budget), and `resumes` increments for the SAME `coord drive-queue
    list`/`status` "resumes=N/MAX" display #2230 already renders — though,
    unlike a merge-gate reading, "landed" cannot un-land, so this path does
    not gate on :data:`MAX_BLOCKED_RESUMES`: there is no oscillation risk to
    cap.
    """
    if not entry.after:
        return None
    if is_permanent_block_reason(entry.last_reason):
        return None
    if not _is_unsatisfiable_prereq_reason(entry.last_reason):
        return None
    diagnosis = diagnose_blocked_after(
        entry, board, states, cycle_keys, live_prereq_terminal
    )
    if diagnosis.unsatisfiable:
        return None
    if diagnosis.dependency_reason:
        # #2756: the verdict is no longer unsatisfiable, but not every
        # named pre-req has landed either — the specific dep that made
        # this row unsatisfiable has cleared while others are merely still
        # in flight. Resume to `waiting` with a live reason describing what
        # it is actually waiting on now, rather than the frozen "will never
        # satisfy" claim this row was blocked with.
        reason = (
            f"{entry.key}'s unsatisfiable pre-req condition cleared — "
            f"resuming from blocked to waiting on: {diagnosis.dependency_reason} "
            "(#2756)"
        )
    else:
        reason = (
            f"{entry.key}'s pre-req(s) ({', '.join(entry.after)}) now show "
            "facts.landed — resuming from blocked without an operator "
            "remove+add, attempt budget reset (#2362)"
        )
    return Reconcile(
        entry.key,
        "resumed",
        reason,
        occupies=False,
        updates={
            "state": STATE_WAITING,
            "attempts": 0,
            "resumes": entry.resumes + 1,
            "last_reason": reason,
        },
    )


# ── reconciliation ───────────────────────────────────────────────────────────


def _startup_age(entry: QueueEntry, now: float | None) -> float | None:
    """Seconds since *entry*'s drive was launched, or ``None`` when unknowable.

    ``None`` — meaning "no startup grace applies" — for three distinct cases,
    all of which must degrade to the pre-#1794 behaviour rather than to an
    entry that can never be retried:

    * the caller passed no clock (``now is None``): a pure-logic caller that
      does not care about the window, e.g. a test pinning pre-req resolution;
    * the row has no ``launched_at``: a row written before DQ-1 shipped the
      column, or one a human flipped to ``running`` by hand;
    * the stamp is in the FUTURE (negative age): a clock that jumped backwards
      must not be able to pin an entry inside the grace window indefinitely.
    """
    if now is None or entry.launched_at is None:
        return None
    age = now - entry.launched_at
    return age if age >= 0.0 else None


def _park_reading_age(entry: QueueEntry, now: float | None) -> float | None:
    """Seconds since *entry*'s park reason was written, or ``None`` when
    unknowable (#2158).

    ``reason_at`` is #2133's capture stamp, written by
    ``coord.state._update_drive_queue_entry_local`` on every ``last_reason``
    write — so for a ``parked`` entry, which by construction gets no further
    writes while the gate stays shut, it is exactly the moment the park was
    recorded.

    ``None`` for the same three cases :func:`_startup_age` returns ``None``
    for, and for the same reason — an unmeasurable age must degrade to
    today's behaviour (hold the park), never to a park that expires by
    accident: no clock was passed, the row predates the ``reason_at``
    migration (or its ``last_reason`` is still ``''``), or the stamp is in the
    future because a clock jumped backwards.
    """
    if now is None or entry.reason_at is None:
        return None
    age = now - entry.reason_at
    return age if age >= 0.0 else None


def _park_reading_expired(
    entry: QueueEntry, facts: IssueFacts, now: float | None
) -> float | None:
    """The park's age when its CI reading has BOTH gone stale and no way to
    refresh itself, else ``None`` (#2158).

    Two conditions, both required:

    * ``not facts.merge_ci_pending_live`` — the reading came only from the raw
      ``merge_queue`` row's persisted ``error``, which no read path rewrites.
      A live ``merge_plan`` reason re-derives itself on every board build and
      is therefore never stale; it is held with no ceiling.
    * the reading is older than :data:`PARK_STALE_SECONDS`.

    Returns the age (not a bare bool) so the caller can put the real number in
    the resume reason — same convention as :func:`_startup_cooldown`.
    """
    if not facts.merge_ci_pending or facts.merge_ci_pending_live:
        return None
    age = _park_reading_age(entry, now)
    if age is None or age < PARK_STALE_SECONDS:
        return None
    return age


def _startup_cooldown(
    entry: QueueEntry, now: float | None, grace_seconds: float
) -> float | None:
    """The entry's age when it is still inside the startup window, else ``None``.

    The age is returned (rather than a bare bool) so every caller can put the
    real number in its reason string — a journal line that says "launched 41s
    ago" is diagnosable; one that says "still starting" is not.
    """
    age = _startup_age(entry, now)
    if age is None or age >= grace_seconds:
        return None
    return age


def _dispatch_produced_nothing(entry: QueueEntry, facts: IssueFacts) -> bool:
    """#2273: did *entry*'s most recent launch ever get as far as creating a
    board-visible assignment?

    Compares ``facts.last_dispatched_at`` — the newest ``dispatched_at``
    across every assignment ``build_board_view`` has ever seen for this
    issue, whatever a PRIOR launch dispatched — against ``entry.launched_at``,
    which a `retry` reconcile does NOT clear (see `_reconcile_running`'s
    `retry`/`exhausted` branches), so it still names the launch that just
    died. ``True`` only when nothing was dispatched on or after that moment:
    the drive died somewhere before or during `coord assign` itself, never
    creating the row the rest of the fleet's machinery (merge, review, CI)
    would otherwise have consumed — the "assignment_id IS NULL" signal the
    issue names, read off data `build_board_view` already collects.

    ``False`` — never treated as evidence of a pure dispatch-layer failure —
    for a row with no `launched_at` at all (nothing to scope the comparison
    against), for `facts.known is False` (the board has never heard of this
    issue at all — absence of evidence about the ISSUE is not evidence about
    the DISPATCH, and guessing would be exactly the "worse than nothing"
    sweep #2230's own docstring warns a naive re-check would be), and for the
    common case, a launch that dispatched fine and died at a LATER stage
    (test, review, merge): that failure has real board state behind it,
    which is exactly what `RETRY_BACKOFF_SECONDS` alone already paces.
    """
    if entry.launched_at is None:
        return False
    if not facts.known:
        return False
    if facts.last_dispatched_at is None:
        return True
    return facts.last_dispatched_at < entry.launched_at


def _is_merge_gate_block_reason(reason: str | None) -> bool:
    """#2424: does *reason* already name a merge-gate block — a death whose
    real cause is a LATER stage (merge), not a dispatch-layer failure?

    `_dispatch_produced_nothing`'s own comparison (``facts.last_dispatched_at
    < entry.launched_at``) goes stale specifically for a relaunch whose only
    job is retrying the Merge stage: by design, that relaunch dispatches no
    new assignment (there is nothing left to dispatch — Work/Test/Review
    already completed), so the comparison reads exactly like a genuine
    dispatch failure even though three real assignments did real work. This
    is the textual escape hatch for that case: when *reason* itself already
    names a merge-gate block, the generic "#2273 no assignment was ever
    created" note must not be layered on top of it — see
    claude-coordinator#2405 (stale smoke verdict) and coord-web#2 (red CI),
    both live escalations where the dispatch-note actively misdirected an
    operator toward `drive-queue remove && add` (a wasted Work/Test/Review
    cycle) instead of the real one-line fix (`coord merge --revalidate`, or
    fixing the failing CI check).

    Four shapes, all written by `coord/drive.py`'s own merge-stage decision
    functions — none of them a "no assignment created" signal:

    * `_die`'s exhausted-merge-attempts wording ("merge attempted N times
      without landing") — `coord assign` succeeded and Work/Test/Review all
      completed; the LOOP inside a single drive session that kept retrying
      `coord merge --only` simply ran out of its own attempt budget
      (#1505/#2078).
    * `is_stale_smoke_reason` — the #1479 staleness race: the merge base
      moved out from under a still-valid smoke verdict.
    * "checks failed" — `coord.merge_queue`'s own `checks_failed` prose
      (``f"checks failed: {summary}"``): red CI on a PR that was dispatched,
      built, and reviewed fine.
    * `_escalate_merge`'s own gate-divergence/terminal-status wording
      ("smoke_required —" / "review_required —" / "merge_status=...") — the
      #1505/#1526 immediate-escalation path, which already recorded its own
      accurate `coord escalate record` before this drive exited.
    """
    if not reason:
        return False
    lowered = reason.lower()
    if "merge attempted" in lowered:
        return True
    if is_stale_smoke_reason(reason):
        return True
    if "checks failed" in lowered:
        return True
    if "smoke_required —" in lowered or "review_required —" in lowered:
        return True
    return "merge_status=" in lowered


def merge_plan_inspect_command(repo: str) -> str:
    """The read-only ``coord merge --plan`` fallback (#3016) — safe to
    propose whenever no specific, blind-runnable remedy is known, because it
    never mutates anything. Used both as :func:`merge_gate_remedy_command`'s
    own fallback and directly by callers escalating a #2806 ``gate_unreadable``
    outcome, whose reason text already says "the next tick re-probes" — the
    destructive `remove && add` requeue would directly contradict that.
    """
    return f"coord merge --plan --repo {repo}"


def merge_gate_remedy_command(reason: str | None, repo: str, issue: int) -> str:
    """The gate-specific one-line fix for a `_is_merge_gate_block_reason`
    block (#3016) — never the blanket ``drive-queue remove && add`` requeue
    a synthetic escalation writer would otherwise reach for by default.

    A requeue is not merely useless for a merge-gate block, it is actively
    destructive: it discards a completed Work/Test/Review cycle to re-run it
    from scratch, when the real fix is a single, targeted merge-lane command
    — exactly the misdirection `_is_merge_gate_block_reason`'s own docstring
    already names two live escalations (claude-coordinator#2405, coord-web#2)
    as having produced, and #2424 only fixed on the prose (``reason``) side,
    leaving this — the field a one-click "Run proposed fix" menu actually
    executes — untouched.

    Only the CI-stale / stale-but-passed-smoke shape has a remedy that is
    both KNOWN and SAFE to run blind: ``coord merge --revalidate --only
    <repo>#<issue>``, the exact command :func:`coord.merge_queue.
    ci_stale_reason`'s own prose already tells the operator to run (matched
    textually here — via :data:`coord.merge_queue.CI_STALE_PREFIX` and
    :func:`is_stale_smoke_reason` — rather than re-derived, so the two can
    never diverge on what "CI stale" means).

    Every other shape this matches — red CI (``checks failed``), a
    review/smoke gate divergence (``review_required —``/``smoke_required
    —``), an opaque terminal ``merge_status=`` — has no single command that
    is always both correct and safe to run without a human first reading
    the actual gate state (fixing a named CI check, recovering or re-running
    a review, deciding a UAT verdict). Guessing wrong there is worse than
    not guessing: the menu this feeds runs the command on one click. So
    every one of those falls back to the read-only inspect command instead
    — see #3016's design note ("a wrong 'Recommended' is worse than no
    recommendation").

    Callers are expected to gate on :func:`_is_merge_gate_block_reason`
    first; called on a reason that ISN'T a merge-gate block at all, this
    still returns the safe inspect fallback rather than raising — there is
    no reason this text-matching helper needs to enforce that precondition
    itself when a wrong answer here is never destructive.
    """
    if reason and (
        CI_STALE_PREFIX.lower() in reason.lower() or is_stale_smoke_reason(reason)
    ):
        return f"coord merge --revalidate --only {entry_key(repo, issue)}"
    return merge_plan_inspect_command(repo)


def is_merge_gate_block_reason(reason: str | None) -> bool:
    """Public alias for :func:`_is_merge_gate_block_reason` (#3016).

    Same convention as ``is_empty_branch_death_reason``/
    ``is_unsatisfiable_prereq_reason``: the classifier is shared with the
    synthetic escalation writers in ``coord.commands.drive_queue``, so it
    gets a non-underscored name rather than a second copy of the text match.
    """
    return _is_merge_gate_block_reason(reason)


def _is_empty_branch_death_reason(reason: str | None) -> bool:
    """#2363: does *reason* name the "claimed success, wrote nothing"
    signature — an acceptance-author or plain work session that exited
    DONE/ADVISORY while its branch carried zero commits?

    Text-matched against the drive's own `drive_exited` reason, the same
    convention `is_ci_infra_reason`/`is_ci_flaky_reason`/
    `is_ci_unreadable_reason` in `coord.merge_queue` use to classify a CI
    status string — not marker-based like `is_permanent_block_reason`,
    because neither `_die` call site this recognizes embeds a dedicated
    marker; today nothing reads their reason text for anything but display.

    Two independent shapes, both required to include "no commits" so a
    death that merely MENTIONS one of the other words in passing does not
    false-positive:

    * the acceptance-author JIT-slice death (`coord/drive.py`'s
      `_decide_acceptance_author`, the ADVISORY and DONE zero-commit
      branches — both bounded-retry via #2334 before reaching this) —
      "acceptance author ... exited (ADVISORY|DONE) ... no commits".
    * the plain work-row death (`coord/drive.py`'s `_decide_advisory`) —
      "work ... exited ADVISORY ... no commits ... nothing was pushed".

    Deliberately does NOT match the sibling "work ... finished with no
    branch — nothing was pushed" reason (`coord/drive.py`'s `_decide`, the
    'done'-status-with-no-branch arm): that shape never reaches
    `branch_has_commits` at all, is not one of the two shapes the issue's
    acceptance criteria name, and has no recorded evidence of its own in
    `queue-block-log.jsonl` — folding it in unasked would be exactly the
    un-scoped widening #2230's own docstring warns a naive rule invites.

    #2334: also read by `_reconcile_running` to SUPPRESS the "no assignment
    was ever created for this run (#2273): likely an infrastructure/
    dispatch-layer failure" note — the same additive-note contradiction
    `_is_merge_gate_block_reason` already guards against for a merge-gate
    death. A `coord drive` that dies on exactly this reason DECLINED to
    dispatch anything further for a terminal, already-observed board row
    (#2334's own bounded in-session retry already spent its dispatch
    attempts before reaching it); it did not fail to dispatch.

    #2635: audited for the SAME per-run/per-entry confusion #2273's sibling
    classifier (`is_dispatch_failure_reason`) turned out to have, and found
    NOT vulnerable — deliberately, so `is_pre_dispatch_block_reason`'s
    caller does not also need a live re-check for this shape. The "no
    commits" verdict this matches comes from `Driver.branch_has_commits`, a
    LIVE `git fetch` + `rev-list` against the branch's actual current state
    at the moment the drive exited — not a cached timestamp comparison. A
    retry reuses the SAME deterministic branch name and checks it out at
    the remote tip (`agent.py`'s `setup_interactive_worktree`), so if an
    earlier attempt had pushed real commits, THIS check would have seen
    them and never produced this reason at all. There is no run/entry gap
    to close here the way there was for `is_dispatch_failure_reason`.
    """
    if not reason:
        return False
    lowered = reason.lower()
    if "no commits" not in lowered:
        return False
    if "acceptance author" in lowered and (
        "exited advisory" in lowered or "exited done" in lowered
    ):
        return True
    return "exited advisory" in lowered and "nothing was pushed" in lowered


def is_empty_branch_death_reason(reason: str | None) -> bool:
    """Public alias for :func:`_is_empty_branch_death_reason` (#2339).

    Same convention as ``is_permanent_block_reason`` /
    ``is_unsatisfiable_prereq_reason``: the classifier is shared with the
    ``add``-side preflight in ``coord.commands.drive_queue``, so it gets a
    non-underscored name rather than a second copy of the text match.
    """
    return _is_empty_branch_death_reason(reason)


def _is_dispatch_failure_reason(reason: str | None) -> bool:
    """#2273: does *reason* carry the "no assignment was ever created for
    this run" marker `_reconcile_running` stamps onto a `retry`/`exhausted`
    reason when :func:`_dispatch_produced_nothing` fires?

    Text-matched, same convention as :func:`_is_empty_branch_death_reason`
    just above — neither call site embeds a dedicated typed column, only
    prose in `last_reason`.
    """
    if not reason:
        return False
    return "no assignment was ever created for this run" in reason.lower()


def is_pre_dispatch_block_reason(reason: str | None) -> bool:
    """#2589: does *reason* name a `blocked` cause with NOTHING for #2230's
    merge-gate sweep to re-check?

    Two shapes, both pre-dispatch by construction:

    * :func:`is_empty_branch_death_reason` — a work row or JIT
      acceptance-author session that exited DONE/ADVISORY with zero commits
      on its branch. `coord drive` never dispatched anything further; there
      is no branch, no PR, no merge-queue row for `_blocked_gate_reading` to
      have ANY opinion about.
    * :func:`is_dispatch_failure_reason` — the drive died before `coord
      assign` ever created a board-visible assignment. Same story: nothing
      downstream of dispatch ever existed.

    This is the exact gap claude-coordinator#2589 reports: `_BLOCKED_GATE_NOTE`
    (in `coord.commands.drive_queue`) fires for ANY non-permanent, non-
    unsatisfiable-`after=` `blocked` cause — including these two, where
    `_reconcile_blocked`'s own `_blocked_gate_reading` returns `None` ("no
    evidence either way", per its docstring) EVERY tick, forever. The note
    then tells the operator #2230 "IS re-checked against the merge gate
    automatically" for a row that has no merge gate to check — the reverse
    of the truth. `coord.commands.drive_queue`'s `list` rendering uses this
    predicate to swap that note for a plain "needs a human" one instead.

    Not itself a check of *whether* the gate sweep has evidence right now
    (that is `_blocked_gate_reading`'s job, and it needs a live board this
    module's pure text classifiers deliberately do not require) — this is
    the cheaper, always-available approximation: a `last_reason` naming
    either shape could ever ONLY have reached `blocked` via dispatch-time
    failure, so there is nothing for a later tick to have learned since.

    #2635: that approximation is scoped to the RUN this `last_reason` was
    stamped for, never widen it to the ENTRY. `is_dispatch_failure_reason`
    in particular can be true on a retry whose OWN launch dispatched
    nothing purely because an earlier attempt's work was still in flight
    (claim detection doing its job) — a board assignment or a pushed branch
    from that earlier attempt is real, positive evidence #2230's sweep has
    something to act on, even though this text match alone cannot see it. A
    caller with a live board MUST check for that evidence before treating a
    `True` here as "terminal, no operator escape" — see
    `coord.commands.drive_queue._fetch_live_dispatch_evidence`, the only
    caller today. `is_empty_branch_death_reason` does not carry the same
    risk (see its own docstring) so this note is deliberately not repeated
    there.
    """
    return is_empty_branch_death_reason(reason) or is_dispatch_failure_reason(reason)


def is_dispatch_failure_reason(reason: str | None) -> bool:
    """Public alias for :func:`_is_dispatch_failure_reason` (#2589).

    Same convention as ``is_empty_branch_death_reason``: the classifier is
    shared with `coord.commands.drive_queue`'s `list` rendering, so it gets a
    non-underscored name rather than a second copy of the text match.

    #2635: text-matched against ONE run's exit reason, so a `True` here
    means "this launch dispatched nothing" — never "nothing was ever
    dispatched for this issue, across every attempt this entry has made".
    The two coincide for a genuinely-never-dispatched entry (the case this
    was built for) and diverge the moment a retry follows a real, earlier
    dispatch — see `is_pre_dispatch_block_reason`'s docstring and
    `coord.commands.drive_queue._fetch_live_dispatch_evidence` for the
    caller-side check that tells them apart.
    """
    return _is_dispatch_failure_reason(reason)


# ── `add`-time preflight (#2339) ─────────────────────────────────────────────

#: Board statuses a queue attempt can never move on its own — every launch
#: re-reads the identical terminal row.  Only ``advisory`` today (#1606); a
#: ``failed`` work row IS re-dispatched automatically by ``coord drive``'s own
#: ``work_retries`` budget, so it is deliberately not in here.
TERMINAL_WORK_STATUSES: frozenset[str] = frozenset({"advisory"})

#: Queue states in which an ``add`` upsert changes NOTHING about whether the
#: entry will ever launch — ``enqueue`` writes the operator-declared columns
#: (machine / after / gate) and deliberately leaves run state (``state`` /
#: ``attempts`` / ``last_reason``) to the tick.
STUCK_QUEUE_STATES: frozenset[str] = frozenset({STATE_BLOCKED, STATE_FAILED})


def add_preflight_notice(
    repo: str,
    issue: int,
    previous: "QueueEntry | None",
    *,
    work_aid: str = "",
    work_status: str = "",
    work_machine: str = "",
) -> str:
    """#2339: what ``coord drive-queue add`` must SAY, or ``''`` for nothing.

    The incident this closes (space-invaders#3): an issue whose latest work
    assignment is a genuine zero-commit ADVISORY (#1606 — the worker exited
    0, its branch carries no commits) is sitting on a **terminal** board row.
    ``coord retry <aid>`` is the one command that clears it — it re-verifies
    the commit count against GitHub, refuses the #1357 false-positive shape
    where the branch does carry commits, and dispatches a fresh worker.
    ``coord drive-queue add`` knew none of that: it echoed ``queued repo#N``
    and stopped, so the entry drained its attempts against the identical
    unchanged row (five launches over ~8 hours, only the first of which ever
    created an assignment), and nothing anywhere named ``coord retry``. An
    operator found the cause only by reading the drive's run log by hand.

    Two independent notes, either of which can fire alone:

    * **the terminal work row** — *work_status* is one of
      :data:`TERMINAL_WORK_STATUSES`, or the entry's own recorded death is
      the zero-commit shape (:func:`is_empty_branch_death_reason`, the text
      ``coord drive``'s ``_decide_advisory`` writes). Names the assignment
      id and ``coord retry``.
    * **the no-op upsert** — *previous* is in a :data:`STUCK_QUEUE_STATES`
      state, where ``add`` updates order/flags only and therefore did NOT
      requeue anything. Names ``remove && add``, the documented reset.

    Deliberately advisory-only and never a refusal, the same posture #2247's
    overlap prediction takes: a false positive here costs an operator one
    paragraph of reading, a refusal costs them the queue. Every input is
    optional and unknown values simply produce fewer lines — the caller
    resolves the board fail-open, so an unreachable board degrades to exactly
    the pre-#2339 output rather than an error.
    """
    key = entry_key(repo, issue)
    lines: list[str] = []

    status = str(work_status or "").strip().lower()
    terminal_row = bool(work_aid) and status in TERMINAL_WORK_STATUSES
    empty_branch = previous is not None and _is_empty_branch_death_reason(
        previous.last_reason
    )

    if terminal_row:
        lines.append(
            f"warning: {key}'s latest work assignment ({work_aid}) is "
            f"{status.upper()} — a TERMINAL board row (#1606). Queuing does not "
            "clear it: every launch re-reads the same row, so the attempts drain "
            "with no new board state."
        )
        if empty_branch:
            lines.append(
                "  this entry's last drive died on exactly that shape "
                "(zero commits on the branch — nothing was pushed)."
            )
        lines.append(f"  clear it first:  coord retry {work_aid}")
        lines.append(
            "  `coord retry` re-verifies the branch's commit count against "
            "GitHub before dispatching a fresh worker, and refuses the #1357 "
            "false-positive shape where the branch DOES carry commits — use "
            "`coord drive --accept-advisory` for that one."
        )
        if work_machine:
            lines.append(
                f"  inspect first:   coord log {work_aid} --machine {work_machine}"
            )
    elif empty_branch:
        # No live terminal row to name — it was already cleared, or the board
        # could not be read — but the queue's OWN recorded death is the
        # zero-commit shape, which is still worth surfacing before the entry
        # spends another attempt reproducing it.
        lines.append(
            f"warning: {key}'s last drive died on the zero-commit "
            "DONE/ADVISORY shape (#1606) — the worker claimed success but "
            "pushed nothing."
        )
        lines.append(
            "  if its work row is still ADVISORY, `coord retry <assignment_id>` "
            "is what clears it; `coord drive-queue list` shows the recorded "
            "reason in full."
        )

    if previous is not None and previous.state in STUCK_QUEUE_STATES:
        lines.append(
            f"warning: {key} was already queued in state {previous.state!r} "
            f"({previous.attempts} attempt(s) spent). `add` updates order and "
            "flags only — never run state — so this add did NOT requeue it."
        )
        lines.append(
            f"  requeue it:      coord drive-queue remove {repo} {issue} && "
            f"coord drive-queue add {repo} {issue}"
        )

    return "\n".join(lines)


def _retry_backoff_reason(
    entry: QueueEntry,
    facts: IssueFacts,
    now: float | None,
    attempts: int,
    retry_backoff_at: float | None,
    own_reason: str | None = None,
) -> str:
    """#2273's launch-side guard: ``''`` unless *entry* is still inside its
    post-death backoff window.

    *attempts* and *retry_backoff_at* are passed in explicitly rather than
    read off *entry* because both can be STALE by the time the walk reaches
    this entry: `plan_tick`'s `by_key`/`ordered` are the pre-tick snapshot,
    so an entry reconciled `running` → `retry` earlier in this SAME tick
    still shows its pre-tick `attempts`/`retry_backoff_at` on `entry` itself,
    and an entry #2230 just resumed from `blocked` had its `attempts` reset
    to 0 by a write this function must not miss either. The caller
    (`plan_tick`'s `_backoff_reason` closure) resolves both against THIS
    tick's own reconciles before calling in — see its comment for exactly
    which two maps it consults.

    Deliberately keyed on ``retry_backoff_at`` — the wall-clock moment the
    death was RECORDED — not on ``launched_at``, the moment the drive that
    died was STARTED: the backoff paces the gap between attempts, not the
    drive's own runtime, and a long-running drive that eventually dies should
    not get LESS backoff before its retry just because it ran for longer
    first.

    ``retry_backoff_at`` is a SEPARATE field from ``entry.reason_at``
    (#2133) on purpose (post-review fix): ``reason_at`` is re-stamped by
    every ``last_reason`` write, including the backoff-deferral's own
    per-tick status refresh (``plan_tick`` writes a fresh ``last_reason``
    every tick an entry is still backing off, to keep the "next attempt
    permitted in Ns" text live) — keying the backoff window off a field the
    backoff mechanism itself rewrites made the window's own clock reset
    every tick it was checked, so an entry whose backoff exceeded the tick
    interval could never finish waiting (age computed at any later tick was
    always ~one tick interval, never the true elapsed time). Written ONLY by
    `_reconcile_running`'s `retry` branch, `retry_backoff_at` cannot be
    moved by the deferral that reads it.

    ``''`` for ``attempts <= 0`` (nothing has died yet — the very first
    launch is never paced, same as today) and for ``now is None`` or
    ``retry_backoff_at is None`` (a pure-logic caller with no clock, or a row
    predating this column — both degrade to the pre-#2273 behaviour exactly,
    the same posture every other clock-gated check in this module takes).

    *own_reason* (#2424 follow-up): the same text the launch-side dispatch
    note is gated on (see the comment above `dispatch_only` in
    `_reconcile_running`'s retry/exhausted branches). Passed through so the
    widened `DISPATCH_FAILURE_MIN_BACKOFF_SECONDS` spacing below answers the
    identical "was this actually a dispatch failure" question the launch-side
    note already answers, rather than recomputing it from
    `_dispatch_produced_nothing` alone — which, like the note before #2424,
    cannot tell a genuine pre-`coord assign` crash from a merge-only relaunch
    that dispatches no new assignment by design. Once `own_reason` already
    names a merge-gate block (`_is_merge_gate_block_reason`), the 300s floor
    would be pure mispacing: the rationale for widening it ("a transient
    dispatch failure cannot spend the whole retry budget inside one tick
    cadence") does not apply once the cause is known to be a merge-gate
    block, not a dispatch failure. ``None`` (the default) degrades to the
    pre-#2424-follow-up behaviour exactly, for callers that have not been
    updated to pass it.
    """
    if now is None or attempts <= 0 or retry_backoff_at is None:
        return ""
    age = now - retry_backoff_at
    if age < 0.0:
        return ""
    idx = min(attempts - 1, len(RETRY_BACKOFF_SECONDS) - 1)
    backoff = RETRY_BACKOFF_SECONDS[idx]
    if _dispatch_produced_nothing(entry, facts) and not _is_merge_gate_block_reason(
        own_reason
    ):
        backoff = max(backoff, DISPATCH_FAILURE_MIN_BACKOFF_SECONDS)
    if age >= backoff:
        return ""
    remaining = backoff - age
    return (
        f"retry backoff: the previous attempt failed {age:.0f}s ago, next "
        f"attempt permitted in {remaining:.0f}s ({backoff:.0f}s spacing "
        f"after {attempts} attempt(s) — #2273, so a transient dispatch "
        "failure cannot spend the whole retry budget inside one tick "
        "cadence)"
    )


def _augment_backoff_reason(previous_reason: str, backoff_text: str) -> str:
    """#2411: fold the real death cause into the mechanical backoff text.

    Before this, `plan_tick`'s waiting walk persisted `_retry_backoff_reason`'s
    return value as the entry's WHOLE `last_reason` on every tick it spent
    backing off — the purely mechanical "next attempt permitted in Ns"
    sentence, deliberately recomputed fresh each tick (see that function's
    docstring). That overwrote the rich `own_reason` (plus the #2273
    dispatch note and #2363 empty-branch note) `_reconcile_running`'s
    `retry` branch had just recorded at the moment of death — visible for
    exactly one tick, then gone. An operator reading `coord drive-queue
    list` — or anything else that reads `last_reason`, like `coord
    drive-queue diagnose` — saw only the spacing text and had no live way to
    tell WHY the entry kept dying, only that it would try again later.

    Fix: keep the FIRST LINE of whatever reason is already on record — the
    real cause, written once at death — and put the fresh backoff sentence
    on a second line under it. `last_reason` was already documented as
    "possibly multi-line" (see `_ROW_CAUSE_MAX_CHARS`'s comment in
    `coord.commands.drive_queue`) precisely so a summary render can show
    line one on the row and the full text on the `last:` continuation line
    — this reuses that convention rather than inventing a new field or
    column.

    Using only the first line (not the full previous text) keeps this
    idempotent across ticks: called again next tick against ITS OWN
    two-line output, the first line is still the original cause, never
    last tick's backoff sentence, so the stored text never grows past two
    lines no matter how many ticks the entry spends backing off.

    Falls back to *backoff_text* alone when there is nothing to keep — a
    row with no prior `last_reason` (a fresh entry, or one hand-built in a
    test) — which is exactly today's pre-#2411 text for that case.
    """
    stripped = previous_reason.strip()
    if not stripped:
        return backoff_text
    base = stripped.splitlines()[0]
    # 3-space continuation indent — the same convention `coord/drive.py`'s
    # own multi-line exit reasons already use for their "inspect: coord log
    # ..." follow-up lines, so a combined reason reads as one paragraph
    # rather than a mismatched second line flush against the left margin.
    return f"{base}\n   {backoff_text}"


def _reconcile_running(
    entry: QueueEntry,
    board: BoardView,
    max_attempts: int,
    *,
    now: float | None = None,
    grace_seconds: float = DRIVE_STARTUP_GRACE_SECONDS,
    local_host: str | None = None,
    exit_reasons: Mapping[str, str] | None = None,
    exit_refused: Mapping[str, bool] | None = None,
    exit_dead_end: Mapping[str, bool] | None = None,
    live_prereq_terminal: Mapping[str, bool] | None = None,
) -> tuple[Reconcile, Blocked | None]:
    """Resolve one ``running`` entry against the board.

    The branch ORDER is the contract; each non-death branch exists because a
    real incident proved the fall-through to ``retry`` was wrong:

    * ``held`` is rule 1 from this module's docstring and the reason capacity
      is not a session count: ``coord drive`` exits ``EXIT_DEADLINE`` (3) when
      the observer's budget runs out, but the worker/test/review it was
      watching keep running on the fleet (#1660).  Such an entry has no tmux
      session and no merge yet — counting it as free is exactly the 2026-08-01
      incident, where five expired drives were each stacked on top of.
    * ``refused``/``dead_end`` are #1844/#2019: a drive that exited on a
      PERMANENT pre-dispatch guard refusal — or, since #2019, on a
      terminal-and-unactionable board row — is definitively finished for this
      launch.  Both share one branch below; only the wording differs.
      Checked right after ``held``,
      BEFORE the #1870 cross-host guard and the #1794 startup grace window,
      because this evidence (the drive's own audit trail, scoped to this
      exact launch) is stronger than anything a local tmux read or the
      startup clock can offer; neither of those exists to protect a
      conclusion this certain. See the extended note below.
    * ``unknown`` is #1870: ``board.live_sessions`` is always a LOCAL tmux
      read, but the queue is fleet-global.  When *entry* was launched on a
      DIFFERENT host than *local_host*, an absent local session proves
      nothing — the drive may be 47 minutes into Test on the machine that
      actually launched it.  Checked AFTER ``held``/``refused`` (real
      evidence always wins) and BEFORE the grace window / death (neither of
      which may run on evidence this tick cannot see).
    * ``starting`` is #1794: a drive that has been launched but has not yet
      registered a session reading OR put work on the board is not dead, it is
      young.  See :data:`DRIVE_STARTUP_GRACE_SECONDS`.

    ``retry`` is therefore reachable only when the session is absent, no work
    is active, nothing landed, the drive's own exit was not a permanent
    refusal, the launch host is this host (or unrecorded), AND the launch is
    older than the grace window — i.e. when death is the only remaining
    explanation.

    *local_host* is the shell's identity for the machine THIS tick is running
    on (``None`` disables the check entirely — the pre-#1870 behaviour, same
    posture as ``now=None`` disabling the grace window).  An entry with no
    recorded ``launch_host`` (predates #1870, or a hand-edited row) is always
    treated as launched here, so it degrades to today's behaviour exactly.

    #1845/#1844: "no session, no active work, nothing landed" is also exactly
    what a drive that exited *deliberately* — a clean, non-crash exit after it
    diagnosed its own blocker and gave up — looks like from here. The drive
    already wrote the true reason to the audit trail (``drive_exited``,
    ``coord.drive.Driver._drive_exit_summary``); nothing downstream of that
    write used to read it, so every one of those orderly exits was reported
    as "drive session died" — a crash where there was none. *exit_reasons*
    (keyed by :attr:`QueueEntry.key`, fetched by the shell from
    :func:`coord.audit.query_audit_log` for the current run only — never a
    stale reason from a prior attempt on the same entry) is that write,
    threaded through as data so this function stays pure.

    *exit_refused* (same keying, same "this run only" scoping) is #1844's
    addition: ``True`` when that exit carried ``coord.drive.
    EXIT_DISPATCH_REFUSED`` rather than a generic non-zero code — i.e. a
    PERMANENT pre-dispatch guard refusal, not a transient death. That one
    boolean is the only thing that changes the state transition: an entry
    with ``exit_refused=True`` goes straight to ``blocked`` (the ``refused``
    branch above), attempts untouched, on the FIRST tick that observes it —
    never ``retry``, because nothing about waiting and relaunching can change
    a condition a retry cannot affect.

    *exit_dead_end* (#2019) is the SAME contract for a second permanent cause:
    ``True`` when the exit carried ``coord.drive.EXIT_DEAD_END`` — the drive's
    own dead-end predicate (``coord.dead_end.detect_dead_end``) found the row
    terminal and unactionable, with nothing active on the fleet and no gate
    transition available. Relaunching a drive against an unchanged dead-end
    row reproduces the dead end exactly, so it too blocks without spending an
    attempt; only the reason wording differs from ``exit_refused``'s. Before
    #2019 this shape did not even reach here — the drive never exited, it
    counted ``no state change`` against a held tmux session, a held queue slot
    and (since #1972) a whole repo's capacity lane for 140 minutes.

    Every other exit reason — present or
    absent, refused or not — only ever changes the WORDING below; whether the
    entry gets another attempt is otherwise unaffected by #1845/#1844 (still
    ``retry`` until ``max_attempts``, still ``exhausted`` → ``blocked`` after).
    The ONE exception is #2363: when ``own_reason`` matches
    :func:`_is_empty_branch_death_reason` — an acceptance-author or work
    session that exited DONE/ADVISORY claiming success with a zero-commit
    branch — the ceiling widens to :data:`EMPTY_BRANCH_MAX_ATTEMPTS` instead
    of the passed-in ``max_attempts``, because that signature's own recorded
    history (see the constant's comment) shows a 0% self-heal rate inside
    the default budget. Still finite, still ``exhausted`` → ``blocked`` with
    the same diagnosis-and-recovery instructions once ITS wider ceiling is
    also hit.

    #2850 adds TWO more "already landed" signals, both checked with the SAME
    priority as ``facts.landed`` above — right after it, before
    ``facts.active_work`` — because both are confirmations of terminal
    completion at least as strong as a cached board read, and a drive that
    has genuinely landed must never be requeued regardless of what
    ``active_work``/cross-host/grace-window evidence says next:

    * ``own_reason`` matching :func:`coord.models.is_merge_landed_reason` —
      the drive's OWN exit narrated a confirmed merge (``coord.drive.
      decide``'s "terminal: merged" branch, gated on a LIVE
      ``verifier.verify_merged`` call, not merely the board's cached
      ``merge_status``). Before this, ``exit_code=0`` plus a "✓ MERGED —
      … has landed" summary still fell all the way through to the generic
      "no session, no active work, nothing landed" retry logic below — the
      drive's own strongest evidence, sitting unread in ``own_reason``,
      never consulted — and got requeued and relaunched into dead air.
    * *live_prereq_terminal* (#2602's mechanism, reused): when the shell's
      bounded live re-check (:func:`coord.commands.drive_queue.
      _fetch_live_prereq_terminal`, widened by #2850 to also probe a
      ``running`` entry's OWN key, not just other entries' ``after=``
      pre-reqs) confirms THIS key is closed or merged, that outranks
      ``own_reason`` even existing — it catches every OTHER shape of "this
      entry's issue is actually done, but this run's own exit reason
      either says nothing about it or predates it" (a crash, a reap, a
      dead-end exit for an issue that merged out of band between the crash
      and this tick). Re-checking before ANY requeue — not only the
      literal MERGED-exit-text case — is what makes the class safe rather
      than patching just the one reported instance.
    """
    facts = board.facts(entry.key)

    if entry.key in board.live_sessions:
        return (
            Reconcile(entry.key, "alive", "drive session is live", occupies=True),
            None,
        )

    if facts.landed:
        witness = "merged" if facts.merged else "issue closed"
        return (
            Reconcile(
                entry.key,
                "done",
                f"drive finished ({witness})",
                occupies=False,
                updates={
                    "state": STATE_DONE,
                    "last_reason": f"done ({witness})",
                    "session_name": None,
                },
            ),
            None,
        )

    # Resolved here — before `facts.active_work` and everything after it —
    # so the #2850 merged-exit check just below can use it; every later
    # branch that reads `own_reason` (gate_a/policy park, refused/dead_end,
    # the retry/exhausted wording) is unaffected by hoisting this, since
    # none of them run before this point.
    own_reason = (exit_reasons or {}).get(entry.key)
    if own_reason and is_merge_landed_reason(own_reason):
        # #2850: the drive's own exit already confirmed — via a LIVE
        # `verify_merged` check, not the board's cached `merge_status` —
        # that this landed. Mark it done here, before anything below gets a
        # chance to read the absent session / absent active-work as a death
        # and requeue a launch that has nothing left to do.
        reason = f"{own_reason} — confirmed merged (#2850), not a death"
        return (
            Reconcile(
                entry.key,
                "done",
                reason,
                occupies=False,
                updates={
                    "state": STATE_DONE,
                    "last_reason": reason,
                    "session_name": None,
                },
            ),
            None,
        )

    if (live_prereq_terminal or {}).get(entry.key):
        # #2850 defence in depth: whatever THIS run's own exit reason says
        # (or doesn't — a crash leaves none at all), a live re-check taken
        # THIS tick confirms the issue is already closed or its PR merged.
        # That is stronger evidence than an absent tmux session and absent
        # board `active_work` could ever be on their own — mark done rather
        # than let the death-diagnosis logic below spend an attempt
        # relaunching a drive with nothing left to do.
        reason = (
            "drive finished — a live re-check this tick confirms the issue "
            "is already closed or its PR merged (#2850), independent of "
            "this run's own exit reason and the cached board"
        )
        return (
            Reconcile(
                entry.key,
                "done",
                reason,
                occupies=False,
                updates={
                    "state": STATE_DONE,
                    "last_reason": reason,
                    "session_name": None,
                },
            ),
            None,
        )

    if facts.active_work:
        return (
            Reconcile(
                entry.key,
                "held",
                "drive session is gone but work is still ACTIVE on the board "
                "(observer deadline, #1660) — still occupying a machine",
                occupies=True,
                updates={
                    "last_reason": "session gone, work still active on the board",
                },
            ),
            None,
        )

    # #1844: a drive that exited on a PERMANENT pre-dispatch guard refusal
    # (`coord.dispatch.enforce_oracle_readiness`, `enforce_epic_dispatch_
    # guard`, or any other check `coord assign`/`coord approve-plan`/`coord
    # fix` raises a plain `ValueError` for — see `coord.drive.
    # EXIT_DISPATCH_REFUSED`'s docstring) is definitively FINISHED for this
    # launch, not merely absent from this tick's evidence. Checked before the
    # #1870 cross-host guard and the #1794 startup grace window below — both
    # of which exist only to withhold judgement on WEAK evidence (an absent
    # local tmux session proves nothing about a foreign host, or about a
    # drive that has not had time to start yet). This is the strongest
    # evidence available: the drive's own audit trail, scoped to THIS launch
    # by the shell (`since=entry.launched_at`), naming its own exit code.
    # Retrying a deterministic refusal costs a full tick cycle and changes
    # nothing — the #1817 overnight incident this issue is named for spent
    # both of its attempts on an identical, guaranteed-to-fail dispatch
    # before exhausting to `blocked` anyway. So this goes straight to
    # `blocked`, WITHOUT incrementing `attempts` — there was never anything
    # to retry.
    #
    # #2019 rides the SAME branch with a second cause: `exit_dead_end`. The
    # evidence is identically strong (the drive's own audit trail, this launch,
    # naming its own exit code) and the conclusion is identical (relaunching
    # against an unchanged row reproduces the outcome exactly), so only the
    # wording and the reported outcome differ. `exit_refused` is checked FIRST
    # purely for stability — the two codes are mutually exclusive by
    # construction (`_drive_exit_summary` records exactly one), so the order
    # is never actually load-bearing.
    #
    # `own_reason` itself was already resolved above (#2850, before the
    # `facts.active_work` check) so the merged-exit short-circuit could use
    # it — reused here unchanged.

    # #2063 rides the SAME evidence as `refused` below but reaches the
    # OPPOSITE conclusion, so it is checked first. A Gate-A "no recorded
    # human sign-off" refusal is not permanent: it is an explicitly
    # operator-fixable condition with a one-command remedy (`coord gate-a
    # --approved`), and it self-clears the moment that verdict is recorded.
    # Landing it in terminal `blocked` — which nothing re-evaluates and
    # `coord drive-queue add` will not clear (#2040) — would leave the entry
    # dead AFTER the human approved, requiring an undocumented remove+add.
    # So it parks (#1891 semantics: re-checked every tick, no attempt spent),
    # and `plan_tick`'s pre-pass below un-parks it once the verdict exists.
    if own_reason and is_gate_a_refusal_reason(own_reason):
        reason = (
            f"{own_reason} — parking without spending an attempt; the queue "
            "resumes it automatically once a human records the verdict, no "
            "queue surgery needed (#2063)"
        )
        return (
            Reconcile(
                entry.key,
                "parked",
                reason,
                occupies=False,
                updates={
                    "state": STATE_PARKED,
                    "last_reason": reason,
                    "session_name": None,
                },
            ),
            None,
        )

    # #2234 rides the SAME evidence as `refused` below (the drive's own
    # `drive_exited` reason) but, like #2063 above, reaches the OPPOSITE
    # conclusion from a PLAIN `EXIT_TERMINAL_FAILURE` — `coord.drive.
    # _decide` marks a `refused_policy` work row with `POLICY_REFUSAL_
    # MARKER` (coord.models) rather than `EXIT_DISPATCH_REFUSED`, so this is
    # checked purely off `own_reason` text, independent of `exit_refused`.
    # Unlike #2063 this does not self-clear on its own timer — a policy
    # refusal names a STANDING rule, not a pending verdict — so it
    # deliberately does not get the "queue resumes it automatically" wording
    # Gate-A's park does; see `plan_tick`'s pre-pass below, which recognises
    # this same marker and leaves the entry parked rather than falling
    # through to the CI-park auto-resume.
    #
    # #2871: the precondition IS an operator action, but it is a RETARGET
    # (rewrite the issue so its deliverable isn't the coordinator-only thing
    # that got refused), not queue surgery. Before #2871, `coord.drive.
    # decide()` re-read the SAME stale `refused_policy` row on every relaunch
    # regardless of what the issue said by then, so `remove`+`add` looked
    # like the fix but did nothing — a fresh queue row, same blocking
    # assignment. `decide()` now compares that row's branch against the
    # issue's CURRENT title-derived slug (`_refused_policy_is_stale`) and
    # bypasses a stale refusal instead of dying on it again, so `remove`+
    # `add` (or any later relaunch) genuinely works — but only once the
    # retarget has actually happened.
    if own_reason and is_policy_refusal_reason(own_reason):
        reason = (
            f"{own_reason} — parking without spending an attempt (#2234); "
            "needs the coordinator, not a relaunch — retarget the issue "
            "(rewrite its title so the deliverable isn't coordinator-only), "
            "then `coord drive-queue remove`+`add` (or just relaunch "
            "`coord drive`) — it now detects the retarget and dispatches "
            "fresh work automatically (#2871)"
        )
        return (
            Reconcile(
                entry.key,
                "parked",
                reason,
                occupies=False,
                updates={
                    "state": STATE_PARKED,
                    "last_reason": reason,
                    "session_name": None,
                },
            ),
            None,
        )

    # #2977: `coord assign` exited because `_gh`'s pre-call guard found a
    # shared GitHub rate-limit backoff already active and skipped the call
    # entirely (`GhRateLimitError(from_cache=True)`) — no `gh` call was ever
    # attempted for this launch. That is neither a permanent refusal (the
    # `is_policy_refusal_reason`/`is_gate_a_refusal_reason` shapes above —
    # nothing about the ISSUE was rejected) nor a genuine dispatch failure
    # (the generic `_dispatch_produced_nothing` note below, which would
    # spend an attempt): it is a fleet-wide, known-duration condition this
    # entry had nothing to do with. Parks WITHOUT spending an attempt
    # (`Reconcile.updates` carries no `attempts` key, same as every other
    # park above) — the wall-clock `until=` timestamp
    # `github_ops.format_throttle_skip_reason` embedded in `own_reason` is
    # what lets `plan_tick`'s parked-entry sweep resume it the moment the
    # backoff clears, with no live re-check and no operator action, unlike
    # the gate_a/policy parks just above which need one.
    if own_reason and is_throttle_skip_reason(own_reason):
        reason = (
            f"{own_reason} — parking without spending an attempt; the queue "
            "resumes it automatically once the backoff clears, no operator "
            "needed (#2977)"
        )
        return (
            Reconcile(
                entry.key,
                "parked",
                reason,
                occupies=False,
                updates={
                    "state": STATE_PARKED,
                    "last_reason": reason,
                    "session_name": None,
                },
            ),
            None,
        )

    permanent: tuple[str, str] | None = None
    if own_reason and (exit_refused or {}).get(entry.key):
        permanent = (
            "refused",
            "refused by a pre-dispatch guard, which cannot change on retry "
            "(#1844); blocking without spending an attempt",
        )
    elif own_reason and (exit_dead_end or {}).get(entry.key):
        permanent = (
            "dead_end",
            "the board row is terminal and unactionable (nothing active, no "
            "gate transition available), which cannot change on retry "
            "(#2019); blocking without spending an attempt",
        )
    if permanent is not None:
        outcome, explanation = permanent
        reason = f"{own_reason} — {explanation}"
        # `Reconcile.updates` is deliberately EMPTY, same as `exhausted`
        # below — the paired `Blocked` carries every write, applied once by
        # `TickPlan.writes()`. `attempts` is absent from BOTH: there is
        # nothing to spend, unlike `exhausted`'s Blocked which stamps the
        # final attempt count.
        return (
            Reconcile(entry.key, outcome, reason, occupies=False),
            Blocked(
                entry.key,
                reason,
                updates={
                    "state": STATE_BLOCKED,
                    "last_reason": reason,
                    "session_name": None,
                },
            ),
        )

    if (
        local_host is not None
        and entry.launch_host
        and entry.launch_host.lower() != local_host.lower()
    ):
        # #1870.  This tick's tmux read is LOCAL; it cannot see a session on
        # the host that actually launched this entry, so its absence here is
        # not evidence of anything.  Fail-soft exactly like an unreachable
        # probe would: occupy the slot, touch neither `state` nor `attempts`,
        # and never relaunch — the same posture #1794 established for "tmux
        # unavailable" / "no server running" / "timed out".
        reason = (
            f"drive was launched on {entry.launch_host!r}, not this host "
            f"({local_host!r}) — liveness cannot be verified from here, so "
            f"this is UNKNOWN, not dead (#1870); still occupying a slot, no "
            f"attempt spent"
        )
        return (
            Reconcile(
                entry.key,
                "unknown",
                reason,
                occupies=True,
                updates={"last_reason": reason},
            ),
            None,
        )

    age = _startup_cooldown(entry, now, grace_seconds)
    if age is not None:
        # #1794.  Launched, but not yet visible as a session and not yet
        # visible as work.  A tick that fires inside this window sees exactly
        # what a dead drive looks like, so it must not be allowed to conclude
        # anything: the entry keeps its state, keeps its attempts, and keeps
        # its slot.
        reason = (
            f"drive is still starting — launched {age:.0f}s ago, inside the "
            f"{grace_seconds:.0f}s startup grace window (#1794); "
            f"not a death, still occupying a machine"
        )
        return (
            Reconcile(
                entry.key,
                "starting",
                reason,
                occupies=True,
                updates={"last_reason": reason},
            ),
            None,
        )

    # Past the grace window (or with no launch stamp to measure), with no
    # session, no active work and nothing landed: this entry did not land the
    # work by any board-visible path. #1845/#1844: that no longer means
    # "died" — the drive may have exited deliberately, with its own reason
    # already on the audit trail. Prefer that reason when one was recorded
    # for this run; fall back to the synthesised wording (with the launch age
    # quoted, so a journal reader can tell a genuine death from a grace
    # window that was set too short) when it wasn't — e.g. no audit row at
    # all, a crash that never reached the `drive_exited` write, or a shell
    # that failed to fetch it.
    since = _startup_age(entry, now)
    launched = f", launched {since:.0f}s ago" if since is not None else ""
    # `own_reason` was already resolved above (before the cross-host/startup
    # checks) so the `refused` branch could use it; reused here unchanged —
    # a non-refusal exit reason (a genuine death that still narrated why)
    # still wins over the synthesised wording, same as #1845.

    # #1891: checked BEFORE the retry/exhausted computation below, and
    # deliberately independent of `own_reason`/`exit_refused` — it does not
    # matter WHY this drive is no longer visible (a deadline, a crash, a
    # machine reboot mid-wait); what matters is whether the board's OWN
    # current read of this entry's issue still shows nothing stronger than
    # "CI checks have not reported yet". Relaunching a fresh `coord drive`
    # right now would just observe the identical silence and wait again — so
    # this parks instead, without spending an attempt (mirrors `refused`
    # just above: `Reconcile.updates` carries the whole transition, no paired
    # `Blocked`, because unlike `refused` this is not a terminal condition —
    # see `plan_tick`'s pre-pass, which is what un-parks it).
    if facts.merge_ci_pending:
        reason = (
            f"{facts.merge_ci_pending_reason or 'CI checks have not reported yet'}"
            f"{launched} — parking without spending an attempt; the queue "
            "resumes it automatically once they do, no operator needed (#1891)"
        )
        return (
            Reconcile(
                entry.key,
                "parked",
                reason,
                occupies=False,
                updates={
                    "state": STATE_PARKED,
                    "last_reason": reason,
                    "session_name": None,
                },
            ),
            None,
        )

    # #2858: the board's `issues` cache row behind `facts.landed`'s negative
    # half (`facts.closed`) can itself be stale — `coord.serve_app.
    # _sync_issues_tick` runs on a slow (300s default) cadence and can be
    # starved for tens of minutes by a shared `gh` rate-limit backoff
    # (:mod:`coord.github_throttle`) that faster pollers keep re-arming. Every
    # POSITIVE witness this function has (`facts.landed`, `own_reason`,
    # `live_prereq_terminal`) was already checked above and would have
    # returned `done` — reaching here means none of them confirmed a landing,
    # which is NOT the same as a fresh, trustworthy "still open". Spending a
    # retry/exhausted attempt on a false negative reproduces #2850's own
    # shape (a landed issue's queue entry gets churned instead of settling)
    # through a different door — the stale cache, not the requeue logic
    # #2850 fixed. Park instead: no attempt spent, re-checked every tick
    # exactly like the `merge_ci_pending` park just above, and it resolves
    # itself the moment the cache catches up (or a later tick's live re-check
    # succeeds).
    if _issue_cache_stale(facts, now):
        # `_issue_cache_stale` only returns True when both are set — see its
        # docstring — so this subtraction is safe without another None guard.
        age = now - facts.issue_synced_at
        reason = (
            f"issue cache is stale ({age / 60:.0f}m old) — cannot confirm "
            f"this issue is still open, so not spending an attempt on it "
            f"(#2858); parking until the next tick sees a fresher read"
        )
        return (
            Reconcile(
                entry.key,
                "parked",
                reason,
                occupies=False,
                updates={
                    "state": STATE_PARKED,
                    "last_reason": reason,
                    "session_name": None,
                },
            ),
            None,
        )

    # #2273: was there ever a board-visible assignment for THIS launch? A
    # `True` here does not change whether an attempt is spent (see
    # DISPATCH_FAILURE_MIN_BACKOFF_SECONDS's comment for why exempting this
    # class outright was considered and deferred) — it widens the SPACING
    # before the next attempt (`_retry_backoff_reason`, applied by
    # `plan_tick`'s waiting walk) and names the fact plainly in both the
    # `retry` and `exhausted` wording, so an operator reading `last_reason`
    # — or the escalation this produces once the budget IS exhausted — sees
    # "no assignment was ever created" instead of a bare exit code that
    # could mean anything.
    #
    # #2424: EXCEPT when `own_reason` already names a merge-gate block
    # ("merge attempted N times without landing", a stale smoke verdict, red
    # CI, ...) — a relaunch whose only job is retrying the Merge stage
    # dispatches no new assignment BY DESIGN (Work/Test/Review already
    # completed; there is nothing left to dispatch), so the comparison above
    # reads exactly like a dispatch failure even though it plainly is not.
    # The generic note is additive evidence for when nothing more specific is
    # known; it must never be layered on top of a reason that already
    # contradicts it — see `_is_merge_gate_block_reason` for the two live
    # escalations (claude-coordinator#2405, coord-web#2) this false-positive
    # actually produced.
    #
    # #2334: the SAME false-positive, a third shape — `own_reason` names an
    # empty-branch DONE/ADVISORY death (`_is_empty_branch_death_reason`: a
    # work row or a JIT acceptance-author row that exited with zero commits
    # on its branch). That is `coord drive` DECLINING to dispatch anything
    # further for a terminal board row it already read — a deliberate
    # choice, not an infrastructure/dispatch-layer failure — and #2334's own
    # bounded in-session retry (`DriveCounters.advisory_retries` /
    # `.acceptance_author_retries`) already spends its own dispatch attempts
    # before this reason is ever reached, so treating THIS exit as more
    # evidence of a broken dispatch layer is exactly backwards. The observed
    # incident (space-invaders#3, claude-coordinator#2531) is `own_reason`
    # itself pointing an operator at `coord retry`/`coord acceptance
    # author`, immediately followed by this note contradicting it with
    # "likely an infrastructure/dispatch-layer failure, not a code defect".
    dispatch_only = (
        _dispatch_produced_nothing(entry, facts)
        and not _is_merge_gate_block_reason(own_reason)
        and not _is_empty_branch_death_reason(own_reason)
    )
    if not dispatch_only:
        dispatch_note = ""
    elif own_reason:
        # A clean exit that still names no assignment — `own_reason` is
        # present and already ruled out as a merge-gate block above, so the
        # confident diagnosis is warranted: the drive itself narrated a
        # death, and that death happened before `coord assign` ever ran.
        dispatch_note = (
            " — no assignment was ever created for this run (#2273): likely "
            "an infrastructure/dispatch-layer failure, not a code defect"
        )
    else:
        # #2442: a REAP, not a clean exit — the session was killed/crashed
        # and left no `own_reason` text at all, so there is nothing for
        # `_is_merge_gate_block_reason` to match against. That is not
        # evidence the cause WAS a dispatch failure; it just means this
        # branch has no way to rule one in or out. #2286 is the live
        # escalation this produced: a session parked 2.3h inside a
        # legitimate, already-diagnosed JIT-slice wait loop (#2426/#2437)
        # looked, to `_dispatch_produced_nothing` alone, identical to one
        # that never got dispatched — and got the same confident-but-wrong
        # "infrastructure/dispatch-layer failure" wording #2424 already
        # established was unreliable for the merge-gate case. Name the
        # effect (no assignment) without asserting the unknowable cause.
        dispatch_note = (
            " — no assignment was ever created for this run (#2273), and "
            "the session left no exit reason to diagnose why"
        )

    # #2363: the "claimed success, wrote nothing" signature gets a WIDER
    # ceiling than every other death reason — see `EMPTY_BRANCH_MAX_ATTEMPTS`
    # and `_is_empty_branch_death_reason`. `max()` against the passed-in
    # `max_attempts` rather than an outright override so a caller-supplied
    # `--max-attempts` larger than the #2363 default is never narrowed.
    # Additive only: any other reason (including `dispatch_only` above) still
    # uses the plain `max_attempts` ceiling, unchanged.
    empty_branch = _is_empty_branch_death_reason(own_reason)
    effective_max_attempts = (
        max(max_attempts, EMPTY_BRANCH_MAX_ATTEMPTS) if empty_branch else max_attempts
    )
    empty_branch_note = (
        f" — empty-branch DONE/ADVISORY exit (#2363): widened retry budget, "
        f"{effective_max_attempts} attempts before blocking instead of the "
        f"usual {max_attempts}"
        if empty_branch
        else ""
    )

    attempts = entry.attempts + 1
    if attempts < effective_max_attempts:
        if own_reason:
            reason = (
                f"{own_reason} (attempt {attempts}/{effective_max_attempts}) — "
                f"requeued at position {entry.position}"
                f"{dispatch_note}{empty_branch_note}"
            )
        else:
            reason = (
                f"drive session died without landing the work"
                f"{launched} (attempt {attempts}/{effective_max_attempts}) — "
                f"requeued at position {entry.position}"
                f"{dispatch_note}{empty_branch_note}"
            )
        return (
            Reconcile(
                entry.key,
                "retry",
                reason,
                occupies=False,
                updates={
                    "state": STATE_WAITING,
                    "attempts": attempts,
                    "last_reason": reason,
                    "session_name": None,
                    # #2273 (post-review): the fixed backoff-window anchor —
                    # written HERE, at the moment the death is recorded, and
                    # nowhere else, so the deferral that paces the NEXT
                    # launch (`plan_tick`'s `_backoff_reason`) cannot move
                    # its own clock by re-persisting `last_reason` on a later
                    # tick. `None` when `now` is unavailable (a pure-logic
                    # caller with no clock) — `_retry_backoff_reason`
                    # degrades that identically to "no backoff" already.
                    "retry_backoff_at": now,
                },
            ),
            None,
        )

    if own_reason:
        reason = (
            f"{own_reason} ({attempts}/{effective_max_attempts} attempts) — "
            f"giving up{dispatch_note}{empty_branch_note}"
        )
    else:
        reason = (
            f"drive session died without landing the work"
            f"{launched} {attempts}/{effective_max_attempts} times — giving up"
            f"{dispatch_note}{empty_branch_note}"
        )
    return (
        Reconcile(entry.key, "exhausted", reason, occupies=False),
        Blocked(
            entry.key,
            reason,
            updates={
                "state": STATE_BLOCKED,
                "attempts": attempts,
                "last_reason": reason,
                "session_name": None,
            },
        ),
    )


# ── `blocked` reconciliation (#2230) ─────────────────────────────────────────


def _blocked_gate_reading(
    entry: QueueEntry,
    facts: IssueFacts,
    live_blocked_gate: Mapping[str, bool] | None,
) -> bool | None:
    """Whether #2230's sweep currently has evidence about *entry*'s gate.

    ``True``  — confirmed still shut, leave it alone.
    ``False`` — confirmed clear now, resume it.
    ``None``  — no evidence either way; leave it alone (never guess).

    Two sources, checked in order, the first one present wins:

    * *live_blocked_gate* — a FRESH, single-entry re-derivation the shell
      took THIS tick, via the same ``coord.merge_queue.entry_gate_status``
      ``coord merge --plan``/``--only`` call, against the SAME live backend
      (see ``coord.commands.drive_queue._fetch_live_blocked_gate``). This is
      what actually fires in production: the daemon-host tick — the only
      host `coord drive-queue tick` ever runs on — reads the local DB
      directly and never computes a ``merge_plan`` section at all (mirrors
      exactly the gap #2182 closed for ``parked``; see
      ``_fetch_live_ci_gate``'s docstring), so without this override the
      sweep would have no evidence whatsoever on the one lane that matters.
    * ``facts.merge_gate_status`` — the passive board reading, free on any
      lane that DOES serve a ``merge_plan`` section (a thin client's live
      ``/board``). ``PLAN_READY`` reads as cleared; any other non-empty
      status (``BLOCKED``/``MERGING``/``MERGED``/``NEEDS_ATTENTION``) reads
      as still shut; ``''`` (no merge-queue row at all right now) is no
      evidence.
    * ``facts.merge_ci_pending`` — #1891's narrower CI-only signal, consulted
      last as a final "still shut" fallback for a board that populated that
      field but not (yet) `merge_gate_status` (e.g. an older payload shape in
      a test, or a partial section). It can only ever confirm "still shut",
      never "cleared" — it was never designed to answer the general question.
    """
    live = (live_blocked_gate or {}).get(entry.key)
    if live is not None:
        return live
    if facts.merge_gate_status:
        return facts.merge_gate_status != PLAN_READY
    if facts.merge_ci_pending:
        return True
    return None


def _reconcile_blocked_unreadable(
    entry: QueueEntry, live_blocked_unreadable: Mapping[str, str] | None
) -> Reconcile | None:
    """#2806: the other half of a ``None`` :func:`_blocked_gate_reading` —
    "I could not read this entry's gate" as opposed to "I read it and it is
    still shut". Before this, both collapsed into the SAME silent ``None``
    from :func:`_reconcile_blocked`, indistinguishable to an operator: a
    `blocked` entry with a real branch/PR whose live probe simply failed
    this tick (an exception, a merge-queue row not enqueued yet — see
    :func:`coord.commands.drive_queue._fetch_live_blocked_gate`'s docstring
    for the mechanics) rendered EXACTLY like one the probe genuinely
    re-confirmed still shut, and — because neither case ever wrote anything
    — a run of bad luck across every tick looked identical to a gate that
    truly never cleared. vimcode#555 sat `blocked` across four ticks this
    way with its merge gate fully clear the whole time.

    Returns ``None`` (render exactly as before #2806) when
    *live_blocked_unreadable* carries no note for this key — the shell's
    probe was never even attempted for this entry this tick (not a
    re-evaluable target at all, or the WHOLE sweep failed closed before
    reaching any entry), OR the entry's OWN cause is #2589's pre-dispatch
    shape (:func:`is_pre_dispatch_block_reason` — no branch/PR was ever
    created, so there will never be anything for the live probe to read; a
    "could not read" note there would be actively misleading, implying a
    retry might help) AND the shell found no actual merge-queue evidence
    contradicting that text — the pre-#2806 shape, still silent exactly as
    before. That suppression is applied by the shell
    (:func:`coord.commands.drive_queue._fetch_live_blocked_gate`), not here:
    #2635 already established that this text classification is per-RUN, not
    per-ENTRY, and can be wrong (a retry's own launch dispatched nothing
    purely because an earlier attempt's work was still in flight, leaving a
    real branch/PR behind) — this function only ever sees whatever key the
    shell decided to hand it, so a `live_blocked_unreadable` entry present
    here has ALREADY survived that check.

    Otherwise the shell's probe WAS attempted, targeted this exact entry,
    and still came back with no key — carried here as a short human-readable
    reason (`"no merge-queue row yet"`, `"no PR number yet"`, an exception
    summary, …). That is new, actionable information: unlike a confirmed
    still-shut gate, a failed probe says nothing about whether the entry is
    actually landable — the very case #2350's `merge_only` fast path exists
    for might be sitting one probe retry away. So this writes `last_reason`
    (visible on `coord drive-queue list`/`status`) and escalates
    (:func:`coord.commands.drive_queue._escalate`, mirroring the
    `oscillating` outcome's own channel — #2230's "no second alert channel"
    posture) every tick the condition holds, the same posture `oscillating`
    already takes for its own distinct-from-silence signal.
    """
    note = (live_blocked_unreadable or {}).get(entry.key)
    if not note:
        return None
    reason = (
        f"{entry.key}'s merge gate could not be read this tick ({note}) — "
        "this is NOT a confirmed-still-shut gate, only a failed probe; "
        "#2230's sweep will try again next tick rather than guessing (#2806)"
    )
    return Reconcile(
        entry.key,
        "gate_unreadable",
        reason,
        occupies=False,
        updates={"last_reason": reason},
    )


def _reconcile_blocked(
    entry: QueueEntry,
    facts: IssueFacts,
    live_blocked_gate: Mapping[str, bool] | None,
    merge_only_ready: Mapping[str, bool] | None = None,
    live_blocked_unreadable: Mapping[str, str] | None = None,
) -> Reconcile | None:
    """Re-examine ONE `blocked` entry against the current gate reading.

    Returns ``None`` — nothing to report, nothing to write — in every case
    except a CONFIRMED-clear reading OR a probe that came back unreadable
    (#2806), which is deliberate: a `blocked` entry this sweep cannot say
    anything new about must render EXACTLY as it did before #2230 existed.
    Four ways to land there:

    * the block is PERMANENT (:func:`is_permanent_block_reason`) — #1844's
      guard refusal or #2019's dead end — neither of which any amount of
      re-checking can ever change;
    * the block is `_resolve_prereqs`'s own unsatisfiable ``after=`` verdict
      (:func:`_is_unsatisfiable_prereq_reason`, #2935) — a `waiting` entry
      that never reached `running` has no assignment and no branch, so
      there is categorically no merge-queue row for THIS sweep to have any
      opinion about; the entry's fate is `_reconcile_blocked_after`'s alone
      to decide, from the ``after=`` graph, on this or a later tick. Checked
      — and returned from — before :func:`_blocked_gate_reading` even runs,
      specifically so :func:`_reconcile_blocked_unreadable` never gets the
      chance to overwrite `last_reason` with a merge-gate "could not read"
      sentence and clobber the one marker `_reconcile_blocked_after` needs
      to recognise this row again next tick — see #2935 for the incident a
      missing version of this branch caused;
    * there is no evidence either way (:func:`_blocked_gate_reading` returns
      ``None``) AND :func:`_reconcile_blocked_unreadable` also has nothing
      to add — a dispatch-time failure (#2589's two per-run shapes) has
      nothing this sweep can cheaply re-check, and guessing would be
      exactly the "worse than nothing" sweep the issue warns a naive
      "retry everything" pass would be;
    * the gate is CONFIRMED still shut — the common, honest outcome for a
      `blocked` entry that has not in fact recovered yet.

    #2806: when there is no evidence either way BUT the shell's live probe
    was actually attempted against this entry and came back empty (as
    opposed to never having been asked at all), :func:`_reconcile_blocked_
    unreadable` reports THAT distinctly — "could not read", never silently
    folded into "still shut". See its own docstring for why the two must
    not render identically to an operator.

    Only a confirmed-clear reading does anything else, and even then only up
    to :data:`MAX_BLOCKED_RESUMES` — past that ceiling the entry stays
    `blocked`, but its `last_reason` is rewritten to say so out loud (the
    issue's explicit ask), which is also what feeds the oscillation signal
    into `coord drive-queue list`/`status` without inventing a second alert
    channel for it.

    *merge_only_ready* (#2350) is consulted only once a confirmed-clear
    reading has already survived the oscillation-ceiling check above — the
    ceiling's "stop giving this entry more chances" verdict applies whether
    the next chance would have been a relaunch or a direct merge attempt.
    A ``True`` entry there means Merge is the ONLY gate left (the board's
    own recorded Test/Review verdicts already show `passed`/`approve` — see
    :func:`coord.commands.drive_queue._fetch_merge_only_ready`), so this
    returns a ``merge_only`` :class:`Reconcile` instead of the ordinary
    ``resumed`` one below — see that outcome's docstring on :class:`Reconcile`
    for why it writes no state here.
    """
    if is_permanent_block_reason(entry.last_reason):
        return None
    if _is_unsatisfiable_prereq_reason(entry.last_reason):
        # #2935: this entry's ONLY `blocked` cause is `_resolve_prereqs`'s
        # own after= verdict (step 4's launch walk, or its #2362/#2756
        # sibling `_reconcile_blocked_after`, called immediately before this
        # function in the same per-tick loop) — a `waiting` entry that never
        # reached `running` at all (see `_resolve_prereqs`'s own comment:
        # "attempts is deliberately NOT incremented: nothing was ever
        # launched for this entry"). A never-dispatched entry has no
        # assignment and no branch, so there is no merge-queue row for
        # #2230's gate sweep to have ANY opinion about — a merge gate cannot
        # exist for something that was never built, and probing for one
        # anyway is a category error, not a failed probe.
        #
        # This must be checked BEFORE `_blocked_gate_reading`/
        # `_reconcile_blocked_unreadable` below, not folded into their "no
        # evidence" handling: `_reconcile_blocked_unreadable` WRITES
        # `last_reason` the instant the shell's live probe was attempted and
        # came back with no key (`live_blocked_unreadable`) — and doing that
        # here would CLOBBER this exact marker text, which is the ONLY
        # signal `_reconcile_blocked_after` uses (both here and on every
        # future tick) to recognise this row as an after=-caused block worth
        # re-diagnosing at all. The claude-coordinator#2935 incident: five
        # dependents whose prerequisite had already landed stayed `blocked`
        # for 178-207 consecutive ticks apiece, because the FIRST tick after
        # they became re-evaluable this way overwrote their own "will never
        # satisfy"/"not queued, not merged and not open" text with this
        # sweep's "no merge-queue row ... could not be read" sentence —
        # after which `_reconcile_blocked_after`'s guard could never
        # recognise the row again, even once the prerequisite genuinely
        # landed. Bailing out here, before any write, keeps the marker
        # intact for as many ticks as it takes the after= graph to actually
        # clear — the entry's fate stays entirely `_reconcile_blocked_
        # after`'s to decide, exactly as the issue's "Expected behaviour"
        # asks: answered from the after= graph, never from a merge-gate
        # probe.
        #
        # Deliberately a text match on the SAME two verdict shapes
        # `_reconcile_blocked_after` itself already trusts to gate its own
        # resume — not the #2589/#2635 per-run dispatch markers
        # (`is_dispatch_failure_reason`/`is_empty_branch_death_reason`),
        # which stay OUT of this function on purpose (see
        # `test_a_pre_dispatch_reason_text_does_not_suppress_a_real_
        # unreadable_note`): those two describe what ONE launch attempt
        # dispatched, which can go stale the moment an earlier attempt on
        # the SAME entry left real evidence behind, so only the shell's live
        # re-check (`live_blocked_unreadable`) is trusted to suppress them.
        # `_resolve_prereqs`'s unsatisfiable verdict carries no such
        # per-run ambiguity: it is only ever written for a `waiting` entry
        # that has NEVER reached `running`, so the "nothing was ever
        # dispatched" reading it encodes cannot be contradicted by an
        # earlier attempt the way a per-run marker can.
        return None
    reading = _blocked_gate_reading(entry, facts, live_blocked_gate)
    if reading is None:
        return _reconcile_blocked_unreadable(entry, live_blocked_unreadable)
    if reading:
        return None

    if entry.resumes >= MAX_BLOCKED_RESUMES:
        reason = (
            f"{entry.key}'s merge gate reads clear again "
            f"({facts.merge_gate_reason or facts.merge_ci_pending_reason or 'no gate objection'}), "
            f"but this entry has already been auto-resumed {entry.resumes} "
            "time(s) and reblocked every time — staying blocked rather than "
            "oscillating (#2230); look at what keeps reblocking it, not just "
            "the gate reading"
        )
        return Reconcile(
            entry.key,
            "oscillating",
            reason,
            occupies=False,
            updates={"last_reason": reason},
        )

    if (merge_only_ready or {}).get(entry.key):
        reason = (
            f"{entry.key}'s merge gate reads clear now "
            f"({facts.merge_gate_reason or facts.merge_ci_pending_reason or 'no gate objection'}) "
            "and the board already shows Test passed and Review approved — "
            "attempting `coord merge --only` directly from this tick instead "
            "of relaunching a fresh drive session (#2350)"
        )
        return Reconcile(entry.key, "merge_only", reason, occupies=False)

    reason = (
        f"{entry.key}'s merge gate reads clear now "
        f"({facts.merge_gate_reason or facts.merge_ci_pending_reason or 'no gate objection'}) "
        f"— resuming from blocked without an operator remove+add, attempt "
        f"budget reset (resume {entry.resumes + 1}/{MAX_BLOCKED_RESUMES}) (#2230)"
    )
    return Reconcile(
        entry.key,
        "resumed",
        reason,
        occupies=False,
        updates={
            "state": STATE_WAITING,
            "attempts": 0,
            "resumes": entry.resumes + 1,
            "last_reason": reason,
        },
    )


# ── deploy gates (#1757) ─────────────────────────────────────────────────────


def pending_probe_targets(entries: Sequence[QueueEntry]) -> list[QueueEntry]:
    """Entries whose ``resume_when`` the shell should run BEFORE this tick.

    Only an ALREADY-``fired`` gate is probed: a gate that fires during this
    tick's own reconcile holds unconditionally for one interval, which is the
    issue's rule ("a ``fired`` hold makes each SUBSEQUENT tick run the
    command") and also the honest one — the deploy cannot have happened in the
    microseconds since the merge was observed.

    Pure and position-ordered, so the shell has no decision left to make: it
    runs exactly this list, in this order, and hands the results back to
    :func:`plan_tick`.
    """
    return [
        e
        for e in sorted(entries, key=lambda e: (e.position, e.key))
        if e.hold_state == HOLD_FIRED and e.resume_when
    ]


def fired_holds(entries: Sequence[QueueEntry]) -> list[QueueEntry]:
    """Entries whose gate has fired and is still holding the queue shut.

    What ``coord drive-queue resume`` releases and what ``status`` reports.
    Position-ordered so "the hold" is always the same entry in both.
    """
    return [
        e
        for e in sorted(entries, key=lambda e: (e.position, e.key))
        if e.hold_state == HOLD_FIRED
    ]


def _resolve_holds(
    ordered: Sequence[QueueEntry],
    reconciled_states: Mapping[str, str],
    probes: Mapping[str, ProbeResult],
) -> list[Hold]:
    """Fire / probe / release every gate, in position order.

    *reconciled_states* is each entry's queue state AFTER step 1 of the tick,
    which is what makes "fires on ``done`` only" checkable here: a
    ``--hold-after`` entry that reconciled to ``blocked`` never reaches this
    branch, so it produces the existing escalation and NOT a second alert (the
    issue's explicit rule — two alerts for one condition is how an alert
    channel gets muted).
    """
    holds: list[Hold] = []
    for entry in ordered:
        if not entry.hold_after:
            continue

        # ARMED → FIRED, the tick the entry lands.  Nothing else fires a gate:
        # `blocked`/`failed` already stop the queue through the escalation
        # path, and `waiting`/`running` have not finished anything yet.
        if (
            entry.hold_state == HOLD_ARMED
            and reconciled_states.get(entry.key) == STATE_DONE
        ):
            holds.append(
                Hold(
                    key=entry.key,
                    outcome="fired",
                    reason=entry.gate_reason,
                    resume_when=entry.resume_when,
                    probes=0,
                    updates={"hold_state": HOLD_FIRED, "hold_probes": 0},
                    scope=entry.hold_scope,
                )
            )
            continue

        if entry.hold_state != HOLD_FIRED:
            # `''` (no gate yet armed), `armed` on an entry that has not
            # landed, or `released` — none of which hold anything.
            continue

        probe = probes.get(entry.key)
        if probe is None:
            # No probe declared, or the shell did not run one.  Manual resume
            # only; the count does not move, so a hold that nobody probes
            # never grows a fake attempt number.
            holds.append(
                Hold(
                    key=entry.key,
                    outcome="held",
                    reason=entry.gate_reason,
                    resume_when=entry.resume_when,
                    probes=entry.hold_probes,
                    scope=entry.hold_scope,
                )
            )
            continue

        if probe.ok:
            holds.append(
                Hold(
                    key=entry.key,
                    outcome="released",
                    reason=entry.gate_reason,
                    resume_when=entry.resume_when,
                    probes=entry.hold_probes,
                    probe_detail=probe.detail,
                    updates={"hold_state": HOLD_RELEASED, "hold_probes": 0},
                    scope=entry.hold_scope,
                )
            )
            continue

        attempts = entry.hold_probes + 1
        holds.append(
            Hold(
                key=entry.key,
                outcome="held",
                reason=entry.gate_reason,
                resume_when=entry.resume_when,
                probes=attempts,
                probe_detail=probe.detail,
                updates={"hold_probes": attempts},
                scope=entry.hold_scope,
            )
        )
    return holds


def _norm_host(name: str | None) -> str:
    """#2101: the one host-identity normalisation this module compares by.

    Machine names come from ``coordinator.yml`` (``dellserver``) and host
    identities from ``socket.gethostname()`` (``dellserver.local``), and a
    cordon that fails to match because of a domain suffix is a cordon that
    silently does nothing — the exact class of failure #1563 closed for
    pause. Same normalisation `coord/commands/drive_queue.py`'s
    ``_local_host_id`` already applies: short hostname, lowercased.
    """
    return str(name or "").split(".")[0].strip().lower()


def _normalized_cordons(cordons: Mapping[str, str] | None) -> dict[str, str]:
    return {
        _norm_host(name): str(reason)
        for name, reason in (cordons or {}).items()
        if _norm_host(name) and reason
    }


def _editable_drift_alert(repo_root: str, shown: str) -> QueueAlert:
    """The queue-level record for "this host's own coord has drifted" (#2314).

    Mirrors :func:`_cordon_alert`: a queue that stops must say why, in the
    same channel and with the same one-key remedy field. Unlike a cordon
    (an operator-declared, self-expiring pause) this is an *accident* —
    something (a Build, a `coord test`/smoke run, an interactive agent
    poking at the live checkout) git-checked-out this host's editable
    ``coord`` install onto a non-default branch, and every tick since has
    been reconciling and launching under whatever code that branch happens
    to carry. #2314: this used to be advisory-only (a warning at CLI
    startup nobody unattended is watching for); now it is a hard refusal —
    a worker branch that silently swaps out the tool driving it is exactly
    the #561/#601 failure class this whole module exists to avoid adding
    to.
    """
    return QueueAlert(
        reason=(
            f"no launch — this host's coord ({repo_root}) is an editable "
            f"checkout on {shown}, not its default branch — the tick would "
            "be reconciling and launching under whatever code that branch "
            "carries. Restore it before anything launches here again."
        ),
        details=(
            "reconciliation (steps 1/1b) still runs — this only refuses to "
            "launch NEW work, the same posture a release cordon takes",
        ),
        command=f"git -C {repo_root} checkout main",
    )


def _cordon_alert(host: str, reason: str) -> QueueAlert:
    """The queue-level record for "this host is cordoned" (#2101 trap E).

    Mirrors :func:`_hold_alert`: a queue that stops must say why, in the same
    channel and with the same one-key remedy field, or "stopped" and "wedged"
    look identical from the outside — which is how a fleet sits eleven
    releases behind for a day with every readout silent.
    """
    return QueueAlert(
        reason=(
            f"no launch — {host} is {reason}. In-flight drives are draining; "
            "the queue resumes automatically the moment this host is rolled "
            "and uncordoned (#2101)."
        ),
        details=(
            "release cordons expire on their own if the propagate run that "
            "set one dies, so this can never wedge the queue permanently",
        ),
        command=f"coord release cordon --clear {host}",
    )


def _hold_alert(hold: Hold) -> QueueAlert:
    """The one queue-level record a FLEET-scoped closed gate raises (#2186).

    Only ever built for ``hold.stops_fleet`` — an entry-scoped gate holds its
    dependents through ordinary deferrals instead (see
    :func:`_resolve_prereqs`), which already report per-entry, so this text
    is specifically "the WHOLE queue is stopped", not "one entry is".

    Carries the operator's own ``hold_reason`` verbatim in ``reason`` — that
    string is the entire point of the feature (it is the runbook line for the
    deploy the queue is waiting on), so it must survive into the alert without
    being summarised.
    """
    details = [
        f"held after {hold.key} (--scope=fleet) — nothing in ANY repo will "
        "launch until this is released"
    ]
    if hold.resume_when:
        outcome = (
            f"attempt {hold.probes} failed"
            if hold.probes
            else "not probed yet (fires on the next tick)"
        )
        if hold.probe_detail:
            outcome += f": {hold.probe_detail}"
        details.append(f"resume-when: {hold.resume_when} ({outcome})")
    else:
        details.append("no --resume-when probe: release manually")
    return QueueAlert(
        reason=f"QUEUE HELD — {hold.reason}",
        details=tuple(details),
        command="coord drive-queue resume",
    )


# ── the tick ─────────────────────────────────────────────────────────────────


def plan_tick(
    entries: Sequence[QueueEntry],
    board: BoardView,
    capacity: int,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    max_parallel_per_repo: int = DEFAULT_MAX_PARALLEL_PER_REPO,
    probes: Mapping[str, ProbeResult] | None = None,
    now: float | None = None,
    grace_seconds: float = DRIVE_STARTUP_GRACE_SECONDS,
    local_host: str | None = None,
    exit_reasons: Mapping[str, str] | None = None,
    exit_refused: Mapping[str, bool] | None = None,
    exit_dead_end: Mapping[str, bool] | None = None,
    gate_a_pending: Mapping[str, bool] | None = None,
    cordons: Mapping[str, str] | None = None,
    live_ci_gate: Mapping[str, bool] | None = None,
    live_ci_gate_reason: Mapping[str, str] | None = None,
    live_blocked_gate: Mapping[str, bool] | None = None,
    live_blocked_unreadable: Mapping[str, str] | None = None,
    editable_drift: tuple[str, str] | None = None,
    merge_only_ready: Mapping[str, bool] | None = None,
    roll_pending_reason: str = "",
    live_prereq_terminal: Mapping[str, bool] | None = None,
) -> TickPlan:
    """Decide one tick.  Pure; the caller executes the returned plan.

    *capacity* is the CEILING (``--max-parallel``), not the number of free
    slots — how many slots are already occupied is a decision (rule 1 above),
    and decisions live in here, not in the shell.

    *max_parallel_per_repo* is the SECOND ceiling (#1972), applied per repo
    after the global one: an entry whose repo already occupies this many slots
    defers, so the walk lands on the first entry from a repo with headroom.
    Both ceilings apply, global first.  ``0`` disables it (pre-#1972
    behaviour); the default of 1 is per-repo serialisation, which for a
    single-repo queue at ``--max-parallel 1`` is exactly what the queue did
    before.  See :data:`DEFAULT_MAX_PARALLEL_PER_REPO` for why repo is the
    right axis and what the board-derived counting means for a wedged drive.

    *probes* maps an entry key to the :class:`ProbeResult` the shell got from
    running that entry's ``resume_when`` (see :func:`pending_probe_targets`);
    an absent key simply means no probe ran.

    *exit_reasons* maps a ``running`` entry's key to the drive's own
    ``drive_exited`` audit summary for THIS launch (#1845/#1844) — see
    :func:`_reconcile_running` for what it changes (wording, and — when
    *exit_refused* also marks the entry — the ``retry``/``exhausted``
    decision itself) and why "for this launch" matters (a stale reason from a
    prior attempt on the same entry must never be replayed as if it explained
    the current one).

    *exit_refused* maps the same keys to ``True`` when that exit was a
    PERMANENT pre-dispatch guard refusal (``coord.drive.
    EXIT_DISPATCH_REFUSED``) rather than a transient death (#1844). Unlike
    *exit_reasons*, this DOES change the decision: such an entry reconciles
    straight to ``blocked`` with ``attempts`` unchanged, never ``retry`` —
    see :func:`_reconcile_running`'s ``refused`` branch.

    *exit_dead_end* is the #2019 twin: ``True`` when the exit was
    ``coord.drive.EXIT_DEAD_END`` (the row was terminal and unactionable).
    Same disposition, same branch, different wording — a relaunch against an
    unchanged dead-end row reproduces the dead end exactly, so it too costs no
    attempt.

    *now* is the shell's ``time.time()``, passed in rather than read here (see
    the module docstring).  It powers #1794's startup grace window on both
    sides of the tick: a recently-launched entry never reconciles to ``retry``
    (:func:`_reconcile_running`), and no entry is relaunched while its last
    launch is still that recent (step 4 below).  ``None`` disables the window
    entirely, which is the pre-#1794 behaviour — the production shell always
    passes a real clock.

    *local_host* is the shell's identity for the machine THIS tick is running
    on (#1870).  It powers the cross-host guard in :func:`_reconcile_running`:
    an entry whose ``launch_host`` names a DIFFERENT machine reconciles to
    ``unknown`` rather than ``retry``, because this tick's tmux read cannot
    see that host at all.  ``None`` disables the check entirely, the
    pre-#1870 behaviour — the production shell always passes its own
    hostname.

    *cordons* maps a machine name to the reason it is under a #2101 release
    cordon ("cordoned: draining for v0.5.31").  Two distinct effects, because
    a drive occupies two machines:

    * this tick's own host (*local_host*) cordoned ⇒ launch NOTHING at all,
      because ``coord drive --tmux`` starts its session HERE.  This is the
      hole #2101 names outright: `coord/drive.py` checks pause only when
      routing a *worker*, so before this the main launcher walked straight
      through the cordon and the fleet could never drain.
    * an entry pinned (``--machine``) to a cordoned host ⇒ that ENTRY defers,
      position unchanged, no attempt spent; the walk moves on to the next
      entry, which may well be launchable on an uncordoned machine.

    Reconciliation (steps 1/1b) still runs under a cordon, and that is the
    whole point: a cordon that also froze the queue's view of reality would
    leave a finished drive's `running` row pinning propagation forever — the
    #2110 deadlock, re-created by the very mechanism meant to end it.  A
    cordoned tick is exactly `--reconcile-only`.

    *editable_drift* is ``(repo_root, shown)`` (a rendered branch label —
    see :func:`coord.cli._editable_checkout_drift`) when THIS host's own
    ``coord`` is an editable checkout that has drifted off its default
    branch, or ``None`` when it hasn't (the overwhelming common case — a
    release install, or an editable one still cleanly on `main`). #2314:
    this used to be advisory-only — a warning printed at CLI startup
    (`coord.cli._warn_if_editable_checkout_moved`) that nothing unattended
    ever reads. Non-``None`` here launches NOTHING this tick, same posture
    and same position as a local cordon (checked immediately after it,
    before capacity) — reconciliation still runs, for the identical #2110
    reason a cordon doesn't freeze it either. Deliberately host-scoped, not
    fleet-scoped: a drifted checkout only makes THIS host's tick
    untrustworthy, not every host's.

    *live_ci_gate* maps a `parked` entry's key to whether a FRESH,
    single-entry re-derivation of its gate — computed by the shell THIS
    tick, via :func:`coord.merge_queue.entry_gate_status` with a live
    ``ci_store``/``gh_ops``, the same backend ``coord merge --plan`` builds
    — still finds it blocked (#2182). A key present here is authoritative
    over both the cached board's `merge_ci_pending` and the #2158 staleness
    ceiling below: there is no ceiling to apply to a reading taken just now.
    A key ABSENT (the entry isn't CI-parked, or the shell's live check
    itself couldn't be made — no PR yet, an unreadable config, a `ci_store`
    that failed to build) falls through unchanged to the pre-#2182 path,
    the #2158 ceiling included — see
    :func:`coord.commands.drive_queue._fetch_live_ci_gate` for the shell
    side, which computes this ONLY for the bounded set of entries currently
    `parked` on a CI reason, never the whole queue. `True` is NOT simply
    "stay parked" any more (#2556): it means the fresh read isn't
    ``PLAN_READY``, which is true both while checks are still genuinely in
    flight (or mid one of their own bounded self-refreshing windows —
    :func:`~coord.merge_queue.is_ci_pending_reason`/`is_ci_infra_reason`/
    `is_ci_flaky_reason`/`is_ci_unreadable_reason`, stay parked) AND once
    they have reported a TERMINAL verdict against the code (a plain "CI
    failed: ..." reading with none of those prefixes — resume instead, so a
    completed, failing run can never be indistinguishable from a slow one).
    See *live_ci_gate_reason* just below, which is what makes that
    distinction possible.

    *live_ci_gate_reason* (#2347, extended by #2556) is *live_ci_gate*'s
    companion: the reason text the SAME fresh `entry_gate_status` call
    returned, for entries whose reading is STILL not `PLAN_READY`. #2556
    gives it a first, decisive job: when this text carries none of the
    self-refreshing prefixes (`is_ci_pending_reason`/`is_ci_infra_reason`/
    `is_ci_flaky_reason`/`is_ci_unreadable_reason`), `live_ci_gate`'s `True`
    is overridden into a resume — see `plan_tick`'s body. When it DOES carry
    one of those prefixes, this reason is used the pre-#2556 way: only to
    rewrite a still-parked entry's `last_reason` when the fresh reading is
    `CI_UNREADABLE_PREFIX`-shaped and differs from what is currently stored,
    so `coord drive-queue list`/`status` shows "GitHub could not be reached"
    the moment that becomes the real cause, instead of silently keeping
    whatever reason the entry happened to park on originally (which
    #1891/#1892/#2252's "still shut ⇒ no reconcile, no write" rule would
    otherwise leave frozen indefinitely —
    see the issue for the observed incident: a stale "CI running: ..."
    reason surviving a run of transient GitHub API 503s for most of
    `PARK_STALE_SECONDS` with no operator-visible signal of the real cause).
    A key ABSENT here changes nothing — same as an absent `live_ci_gate` key.

    *live_blocked_gate* is #2230's counterpart, for `blocked` instead of
    `parked`: maps a RE-EVALUABLE `blocked` entry's key (never one
    :func:`is_permanent_block_reason` recognises — the shell never even
    computes a reading for those, see
    :func:`coord.commands.drive_queue._fetch_live_blocked_gate`) to whether a
    fresh single-entry `entry_gate_status` re-derivation, taken THIS tick,
    still finds it blocked. Same authority rule as *live_ci_gate*: present
    beats the cached board's `IssueFacts.merge_gate_status`; absent falls
    through to it. See :func:`_blocked_gate_reading`.

    *live_blocked_unreadable* (#2806) maps a `blocked` entry's key to a
    short human-readable reason the shell's live probe for THAT entry was
    attempted this tick but came back with no evidence at all — a key
    ABSENT from *live_blocked_gate* is ambiguous on its own (never asked, or
    asked and failed?); this dict disambiguates it. Only consulted once
    *live_blocked_gate* and the cached board both have nothing for this
    entry (:func:`_blocked_gate_reading` returns ``None``) — see
    :func:`_reconcile_blocked_unreadable` for the full decision and why "I
    could not read this gate" must render differently from "I read it and
    it is still shut".

    *merge_only_ready* (#2350) maps a key to whether — GIVEN this SAME tick
    already found its live gate clear (*live_ci_gate* reading `False` for a
    `parked` entry, *live_blocked_gate* reading `False` for a `blocked` one)
    — the board's own recorded pipeline state ALSO shows Test `passed` and
    Review `approve` already, i.e. Merge is the only gate that was ever
    still shut. `True` there swaps what would have been today's `resumed`
    reconcile (write `STATE_WAITING`, compete for next tick's launch slot)
    for a `merge_only` one instead (write nothing; the shell attempts
    `coord merge --only` directly, this tick, no relaunch, no capacity
    spent) — see :class:`Reconcile`'s `merge_only` outcome and
    :func:`coord.commands.drive_queue._fetch_merge_only_ready` for how the
    shell computes it. A key ABSENT or `False` changes nothing: the entry
    takes exactly the pre-#2350 `resumed` path, so an unreadable board, a
    missing PR, or Test/Review genuinely not both satisfied yet all degrade
    to today's behaviour, never to a wrongly-skipped relaunch.

    The algorithm, from #1754, plus #1757's step 2, #1891's step 1b, #2055's
    extension of it, #2230's further extension for `blocked`, and #2362's
    narrower extension of THAT:

    1. Reconcile every ``running`` entry (:func:`_reconcile_running`).
    1b. Re-check every ``parked``/``blocked``/``failed`` entry against the
        CURRENT board: landed ⇒ ``done`` (#1891 for ``parked``, #2055 for
        ``blocked``/``failed``). Not-yet-landed then also checks the gate,
        for ``parked`` and (#2230) for RE-EVALUABLE ``blocked`` entries
        alike: cleared ⇒ ``waiting`` (falls into step 4 on this SAME tick,
        `blocked`'s `attempts` reset to 0 — see :func:`_reconcile_blocked`);
        still shut, or no evidence either way ⇒ untouched, no write, nothing
        to report. A PERMANENTLY-blocked entry (#1844/#2019) and a `blocked`
        entry re-cleared and re-blocked :data:`MAX_BLOCKED_RESUMES` times
        already are never resumed — the former can never change, the latter
        is oscillation, a signal in its own right (see the `oscillating`
        reconcile outcome). ``failed`` gets the landed check only, same as
        before #2230 — this never resurrects an entry for dispatch on its
        own, only lets a finished one stop claiming to be unfinished. Never
        spends an attempt either way (beyond the reset above) — a missing
        verdict is not a failed one. Before #2230's gate re-check even runs,
        a `blocked` entry whose OWN `last_reason` is `_resolve_prereqs`'s
        "queued but blocked/failed — it will never satisfy" verdict gets ITS
        `after=` graph re-derived fresh instead (:func:`_reconcile_blocked_
        after`) — #2230's `_blocked_gate_reading` has no evidence at all for
        an entry that never reached the merge queue, so without this an
        entry blocked purely on a dead pre-req would never notice that
        pre-req later landing. Every named pre-req now `facts.landed` ⇒
        ``waiting``, attempts reset, same shape as #2230's resume; anything
        else (a different cause, a still-unsatisfied/unsatisfiable graph)
        falls straight through to #2230's check unchanged.
    2. Resolve deploy gates (:func:`_resolve_holds`).  A gate left closed with
       ``scope=fleet`` returns immediately with no launch and a HELD alert —
       before the capacity check, and regardless of how eligible the rest of
       the queue is.  The DEFAULT scope, ``entry`` (#2186), does not: it holds
       only entries whose own ``after=`` names the gated key, resolved inside
       step 4's ``_resolve_prereqs`` call, and the walk continues past it —
       an unrelated waiting entry launches in this same tick even while the
       gate stays shut. That "even with free capacity and an eligible
       successor" clause is the entire feature either way: the DEPENDENT is
       exactly the thing that must not run until the deploy lands, whether or
       not anything else in the queue also has to wait for it.
    3. ``free = capacity - occupied``; ``<= 0`` returns with no launch and no
       alert — being at capacity is the queue working, not a problem to
       report.
    4. Walk ``waiting`` by ``position``, FIRST ELIGIBLE WINS: an entry still
       inside its startup grace window defers (#1794); an entry whose own
       issue is already landed (merged or closed) reconciles straight to
       ``done`` without ever launching (#1873) — checked before its `after=`
       graph, so a landed entry is never blocked or deferred on account of its
       own now-irrelevant pre-reqs; unsatisfiable blocks and escalates,
       unsatisfied defers (position unchanged); an entry whose REPO is already
       at *max_parallel_per_repo* defers too (#1972, checked LAST — a broken
       pre-req is a permanent fact and must still escalate, whatever the
       repo's occupancy is doing this tick); the first eligible entry is the
       launch.  Everything after the launch is walked in REPORT-ONLY mode
       (``Deferral.counted=False``, no updates) so ``--dry-run`` can explain
       the rest of the queue — including against the launch's own repo, which
       the report-only pass counts as occupied.
    5. No launch with at least one entry STILL genuinely waiting (deferred or
       blocked — #1873 reconciliations do not count, see below) ⇒ exactly ONE
       queue-level alert.  #1972's repo-limit deferrals do not count either:
       a queue whose every remaining entry is waiting on its own repo's
       in-flight work is saturated, not stalled, and alerting on it every tick
       is how an alert channel gets muted (same reasoning as step 3).

    An entry reconciled from ``running`` back to ``waiting`` in step 1 IS
    walked in step 4 — its attempt was already consumed — but #2273 puts a
    SECOND bound on that same-tick relaunch, distinct from #1794's below: a
    ``retry`` this tick (or one still inside its window from a prior tick) is
    also checked against :func:`_retry_backoff_reason`, which now usually
    defers it rather than launching it immediately. Before #2273 that
    relaunch happened unconditionally the instant a death was confident
    enough to call ``retry`` at all — the entire point being "don't idle a
    whole interval" — which is exactly how quadraui#508 fit both of its
    attempts into six minutes on 2026-08-15 and gave up before a genuinely
    transient condition had time to clear. A gate RELEASED in step 2, or a
    ``parked``/``blocked`` entry step 1b resumes on POSITIVE evidence the
    gate cleared, still falls straight through into step 4 with NO backoff —
    :func:`_retry_backoff_reason` only fires for ``attempts > 0``, and #2230
    resets those to 0 on resume — because that class already has real
    evidence the condition is gone; only a plain ``retry``, which has none,
    needs pacing at all.

    #1794 puts a further bound on same-tick relaunch, and it is the reason
    the grace window is checked TWICE.  Step 1 can only produce a ``retry``
    for an entry whose launch is older than *grace_seconds*, so the relaunch
    is only ever of a drive the tick is confident is gone; and step 4 refuses
    the launch outright for anything launched more recently, whatever put it
    back in ``waiting``.  Between them, no single tick can start a second
    ``coord drive`` for an issue whose first one may still be coming up.

    *roll_pending_reason* (#2587) is non-empty when a fleet roll is queued for
    the next inter-drive gap (see :class:`RollPending`) — the shell's rendered
    ``RollPending.describe()``, e.g. ``"roll pending: v0.5.230
    (nightly-window)"``. Checked immediately after the drift check (step 2c)
    and, like both it and the cordon, BEFORE capacity: this tick launches
    NOTHING however many slots are free, however many entries are eligible.
    Reconciliation (steps 1/1b) still runs — same reasoning as the cordon and
    drift checks: the whole point of #2587 is that the tick keeps noticing
    reality even while it refuses to act on it, so :attr:`TickPlan.occupied`
    stays truthful and the shell can tell the moment it drops to ``0`` — the
    inter-drive gap this marker is waiting for. This function does not decide
    WHEN to fire the roll or how long the marker may stay pending; it merely
    plays the same "launch nothing, but say why" role the cordon and drift
    checks do. The shell owns the TTL/deferral-ceiling bound (:meth:`RollPending
    .expired`) and the actual ``systemctl`` call once ``occupied`` reaches 0.

    *live_prereq_terminal* (#2602) maps a dep key to ``True`` when
    ``coord.commands.drive_queue._fetch_live_prereq_terminal`` took a live
    ``github_ops.work_is_terminal`` read THIS tick and confirmed the issue is
    closed or its PR merged — passed straight through to every
    :func:`_resolve_prereqs` call (the launch walk's `waiting`-entry checks)
    and to :func:`_reconcile_blocked_after` (the `blocked`-entry self-heal).
    This is the recovery half of #2602: `board.facts(dep).landed`, which
    both of those already consult first, is a periodic `/board` build, not a
    live read, and a pre-req that has JUST left the queue (merged, issue
    closed) can outrun that cache — reading as unsatisfiable
    ("not queued, not merged and not open on the board") and permanently
    blocking every dependent chained `--after` it, recoverable before this
    only by an operator `remove` + `add` (coord-portal#145/#149/#150,
    2026-08-22). A dep ABSENT here (the shell's bounded live check never ran
    for it, or ran and came back inconclusive) leaves both call sites to
    their pre-#2602 behaviour — never a false "satisfied".
    """
    ordered = sorted(entries, key=lambda e: (e.position, e.key))
    states: dict[str, str] = {e.key: e.state for e in ordered}
    by_key = {e.key: e for e in ordered}
    # #2273: `states` above already follows "prefer this tick's fresh write
    # over the frozen `ordered` snapshot" for `state`; the backoff check
    # needs the SAME rule for `attempts` (a #2230 resume this tick resets it
    # to 0, and `entry.attempts` on the frozen snapshot would still show the
    # pre-reset value) and for the wall-clock moment the most recent death
    # was recorded (only known fresh for an entry THIS tick reconciled
    # `running` → `retry` — everything else falls back to the entry's own,
    # already-persisted `retry_backoff_at`). See `_retry_backoff_reason`'s
    # docstring for why both are passed in rather than read off `entry`, and
    # for why `retry_backoff_at` — not `reason_at` — is the persisted field
    # this falls back to (the post-review #2273 "moving target" fix).
    effective_attempts: dict[str, int] = {e.key: e.attempts for e in ordered}
    retry_backoff_at_map: dict[str, float] = {}
    # #2411: same "prefer this tick's fresh write" rule, this time for the
    # real death cause `_backoff_reason` below folds into its combined
    # `last_reason` — see `_augment_backoff_reason`. Without this, an entry
    # that dies and hits the backoff check in the SAME tick (step 1 above,
    # then step 4 below) would combine against its STALE pre-tick
    # `entry.last_reason` — one tick behind the death this walk just
    # recorded — instead of the `own_reason`-based text `reconcile.reason`
    # carries fresh, right below.
    effective_last_reason: dict[str, str] = {e.key: e.last_reason for e in ordered}

    reconciles: list[Reconcile] = []
    blocked: list[Blocked] = []
    deferrals: list[Deferral] = []
    # #2350: entries this tick decided to attempt `coord merge --only` on
    # directly rather than relaunch — see `Reconcile`'s `merge_only` outcome.
    merge_only_candidates: list[QueueEntry] = []
    occupied = 0
    # #1972: the same count, keyed by repo.  Populated from the SAME
    # `reconcile.occupies` verdict as `occupied` above — one source of truth, so
    # the per-repo view can never claim a slot the global view does not.
    repo_occupied: dict[str, int] = {}
    repo_capacity = max(0, int(max_parallel_per_repo))

    # Cycles are re-checked here, not just at `add` time: `remove` can leave
    # the surviving edges in a shape `add` never validated, and a hand-edited
    # DB row is always possible.  A cycle makes every member unsatisfiable.
    # Computed up front (not just before step 4's launch walk, which used to
    # be the only consumer) because #2362's `blocked`-entry re-check below
    # also needs it — both call sites want the SAME cycle read for THIS tick.
    cycle_keys: dict[str, str] = {}
    cycle = find_cycle({e.key: list(e.after) for e in ordered})
    if cycle is not None:
        message = "dependency cycle: " + " -> ".join(cycle)
        for key in cycle:
            cycle_keys[key] = message

    for entry in ordered:
        if entry.state != STATE_RUNNING:
            continue
        reconcile, block = _reconcile_running(
            entry,
            board,
            max_attempts,
            now=now,
            grace_seconds=grace_seconds,
            local_host=local_host,
            exit_reasons=exit_reasons,
            exit_refused=exit_refused,
            exit_dead_end=exit_dead_end,
            live_prereq_terminal=live_prereq_terminal,
        )
        reconciles.append(reconcile)
        if reconcile.occupies:
            occupied += 1
            repo_occupied[entry.repo] = repo_occupied.get(entry.repo, 0) + 1
        new_state = reconcile.updates.get("state")
        if new_state:
            states[entry.key] = str(new_state)
        new_attempts = reconcile.updates.get("attempts")
        if new_attempts is not None:
            effective_attempts[entry.key] = int(new_attempts)
        if reconcile.outcome == "retry" and now is not None:
            # #2273: this IS the moment the death got recorded — `reconcile
            # .updates` (below, via `_apply_writes`) persists `now` to the
            # new `retry_backoff_at` column, so `now` is the truthful value
            # for a backoff check reached later in this SAME tick, not the
            # entry's stale pre-tick `retry_backoff_at`.
            retry_backoff_at_map[entry.key] = now
        if reconcile.outcome == "retry":
            # #2411: `reconcile.reason` IS `reconcile.updates["last_reason"]`
            # (the retry branch of `_reconcile_running` builds both from the
            # same local `reason`) — the real death cause, `own_reason` plus
            # its #2273 dispatch-note/#2363 empty-branch-note. Recorded here
            # so a backoff check reached later in THIS tick combines against
            # it instead of the pre-tick `entry.last_reason`.
            effective_last_reason[entry.key] = reconcile.reason
        if block is not None:
            blocked.append(block)
            states[entry.key] = STATE_BLOCKED

    # #1891 step 1b: re-check every `parked` entry against the CURRENT board,
    # independent of capacity/holds below — mirrors step 1's own `done` check
    # (an entry can land while parked exactly as it can while running) and,
    # like step 1, never spends an attempt either way. `entry.landed` wins
    # unconditionally over "still gated", same ordering `_reconcile_running`
    # uses for a `running` entry. A gate that CLEARED flips `states` straight
    # to `waiting` here — not `by_key`, which stays whatever DQ-1 loaded — so
    # it falls into the SAME step-4 walk below, on the SAME tick, exactly
    # like a deploy gate released in step 2 (see this function's docstring
    # for why that same-tick fall-through matters). A gate that is STILL
    # shut is left alone entirely: no reconcile, no write, nothing to
    # report — the parked row itself, rendered by `coord drive-queue list`/
    # `status`, already answers "why isn't this launching".
    #
    # #2055 extends the SAME `landed` check to `blocked` and `failed`
    # entries. `blocked`/`failed` are terminal for dispatch — the queue gave
    # up on them, and this loop must NOT resurrect them for a relaunch, the
    # way the `parked` branch below resumes to `waiting` on cleared CI. But
    # "the queue gave up" and "the work is done" are independent facts: a
    # human fixes a blocked/failed issue by hand and merges it out of band
    # exactly as often as a parked one lands while its gate is still shut.
    # Without this, that merge is invisible forever — `blocked`/`failed`
    # have no other re-check, so the board keeps reporting finished work as
    # outstanding until someone notices and runs
    # `coord drive-queue remove`. See #1956 for a live instance.
    for entry in ordered:
        if entry.state not in (STATE_PARKED, STATE_BLOCKED, STATE_FAILED):
            continue
        facts = board.facts(entry.key)
        if facts.landed:
            witness = "merged" if facts.merged else "closed"
            reason = f"done — issue already {witness} while {entry.state} (#2055)"
            reconciles.append(
                Reconcile(
                    entry.key,
                    "done",
                    reason,
                    occupies=False,
                    updates={
                        "state": STATE_DONE,
                        "last_reason": reason,
                        "session_name": None,
                    },
                )
            )
            states[entry.key] = STATE_DONE
            continue
        if entry.state == STATE_FAILED:
            # `failed` is terminal for dispatch: the landed check above is
            # the only re-check it gets (#2055). #2230's sweep is scoped to
            # `blocked` only — `failed` is not a state anything in this
            # module writes any more; nothing here is entitled to invent a
            # gate re-check for it without a reason to believe it needs one.
            continue
        if entry.state == STATE_BLOCKED:
            # #2362: re-derive an unsatisfiable `after=` verdict against the
            # CURRENT board FIRST — before #2230's merge-gate re-check, which
            # has nothing to say about an entry that was blocked before ever
            # reaching the merge queue (`_blocked_gate_reading` returns
            # `None`, "no evidence", for exactly that entry, so
            # `_reconcile_blocked` alone would leave it blocked forever even
            # after its named pre-req lands). Scoped tightly enough that it
            # only ever fires for `_resolve_prereqs`'s two unsatisfiable
            # shapes ("it will never satisfy", and #2602's "not queued, not
            # merged and not open") — see :func:`_reconcile_blocked_after`.
            blocked_reconcile = _reconcile_blocked_after(
                entry, board, states, cycle_keys, live_prereq_terminal
            )
            if blocked_reconcile is None:
                # #2230: re-examine a `blocked` entry against the CURRENT gate
                # reading before falling through to the `parked`-only
                # machinery below, which must never run for `blocked` —
                # resurrecting a gave-up entry via the CI-pending resume was
                # explicitly not the #2055 fix and is not this one either;
                # see :func:`_reconcile_blocked` for the full decision.
                blocked_reconcile = _reconcile_blocked(
                    entry,
                    facts,
                    live_blocked_gate,
                    merge_only_ready,
                    live_blocked_unreadable,
                )
            if blocked_reconcile is not None:
                reconciles.append(blocked_reconcile)
                if blocked_reconcile.outcome == "merge_only":
                    # #2350: no state write — the shell decides the entry's
                    # real next state from the live merge attempt's outcome.
                    merge_only_candidates.append(entry)
                    continue
                new_state = blocked_reconcile.updates.get("state")
                if new_state:
                    states[entry.key] = str(new_state)
                # #2273: a `resumed` reconcile resets `attempts` to 0 (the
                # whole point of #2230's budget reset) — `effective_attempts`
                # must see that reset THIS tick, same reasoning as step 1's
                # own bookkeeping above, or a freshly-reset entry would be
                # backed off using the budget it no longer has.
                new_attempts = blocked_reconcile.updates.get("attempts")
                if new_attempts is not None:
                    effective_attempts[entry.key] = int(new_attempts)
            continue
        # entry.state == STATE_PARKED falls through to the #1891/#2182/
        # #2158/#2063 machinery below.
        # #2182: a FRESH, single-entry re-derivation of this exact entry's
        # gate, taken by the shell THIS tick (see the `live_ci_gate`
        # parameter doc above) — authoritative over both the cached board
        # reading below and the #2158 staleness ceiling, because there is no
        # staleness to a reading computed just now. Bypasses that whole
        # apparatus entirely rather than layering on top of it: a `True`
        # here means the SAME check `coord merge --plan` would run still
        # finds this entry blocked (stay parked, no ceiling needed — held
        # for as long as the live check keeps saying so, exactly like a
        # live `merge_plan` reason always was); a `False` means the plan
        # reads READY right now (resume immediately). A MISSING key — the
        # entry isn't CI-parked, or the shell's live check itself could not
        # be made — falls through unchanged to the pre-#2182 path.
        live_override = (live_ci_gate or {}).get(entry.key)
        if live_override is not None:
            if live_override:
                live_reason = (live_ci_gate_reason or {}).get(entry.key)
                # #2556: `live_override=True` only means the FRESH read
                # taken THIS tick isn't PLAN_READY — but "not ready" folds
                # together two very different facts. One is "checks are
                # genuinely still in flight" (`is_ci_pending_reason`), or one
                # of the other self-refreshing, no-verdict-yet classes
                # (`is_ci_infra_reason`'s verdictless failure mid its own
                # bounded auto-rerun, `is_ci_flaky_reason`'s one-shot re-run
                # to rule out a flake, `is_ci_unreadable_reason`'s bare
                # GitHub-unreachable read, handled specially just below) —
                # for all of these, "still shut ⇒ stay parked" (#1891/#2182)
                # is exactly right, because there is no fresh verdict to act
                # on yet. The other is "checks already reported a TERMINAL
                # verdict against the code" — a plain "CI failed: ..."
                # reading carrying none of those prefixes. This entry's own
                # park message promises "the queue resumes it automatically
                # once they do [report]" — a completed, failing run
                # satisfies that in every ordinary reading, so staying
                # parked on it forever (as every prior tick did — the #2158
                # staleness ceiling never even runs, because it only applies
                # when `live_override` is absent) is the bug: a red CI on a
                # parked row was indistinguishable from a slow CI on one,
                # unboundedly. Resume here exactly like the "reads READY"
                # branch below (no attempt spent — a fresh GitHub read is
                # not a failed launch attempt) so this falls straight into
                # the SAME `waiting` walk, on the SAME tick; whatever
                # relaunches `coord drive` finds the terminal CI reading and
                # routes it through `_decide_merge`'s existing checks_failed
                # handling (dispatch a fix, or block with a real reason) —
                # exactly the path any other red-CI entry already takes.
                if live_reason and is_ci_terminal_reason(live_reason):
                    reason = (
                        f"live re-check of {entry.key}'s gate this tick "
                        f"reads a terminal, non-pending result ({live_reason}) "
                        "— resuming from parked without spending an attempt "
                        "so the normal checks_failed handling can take over "
                        "(#2556)"
                    )
                    reconciles.append(
                        Reconcile(
                            entry.key,
                            "resumed",
                            reason,
                            occupies=False,
                            updates={
                                "state": STATE_WAITING,
                                "attempts": 0,
                                "last_reason": reason,
                            },
                        )
                    )
                    states[entry.key] = STATE_WAITING
                    effective_attempts[entry.key] = 0
                    continue
                # #2347: CONFIRMED still blocked — but if the FRESH reading
                # taken THIS tick says the real cause is "GitHub could not
                # be reached" (not a real CI verdict) and that differs from
                # whatever reason is currently stored, rewrite `last_reason`
                # so `coord drive-queue list`/`status` says so — rather than
                # silently re-confirming whatever reason this entry
                # happened to park on originally, unboundedly, the way
                # #1891/#1892/#2252's "still shut ⇒ no reconcile, no write"
                # rule does for every other still-blocked reading. State
                # stays `parked`, no attempt spent — this changes only what
                # the operator is told, never the decision.
                if (
                    live_reason
                    and is_ci_unreadable_reason(live_reason)
                    and live_reason != entry.last_reason
                ):
                    reason = (
                        f"{live_reason} — still parked; the live re-check "
                        "this tick could not reach GitHub either (#2347)"
                    )
                    reconciles.append(
                        Reconcile(
                            entry.key,
                            "reparked",
                            reason,
                            occupies=False,
                            updates={"last_reason": reason},
                        )
                    )
                continue
            if (merge_only_ready or {}).get(entry.key):
                # #2350: Merge is the only gate left — the board already
                # shows Test passed and Review approved. Attempt the merge
                # directly this tick instead of paying for a relaunch; see
                # `Reconcile`'s `merge_only` outcome for why no state is
                # written here.
                reason = (
                    f"live re-check of {entry.key}'s gate this tick reads "
                    "READY (coord merge --plan agrees) and the board already "
                    "shows Test passed and Review approved — attempting "
                    "`coord merge --only` directly from this tick instead of "
                    "relaunching a fresh drive session (#2350)"
                )
                reconciles.append(
                    Reconcile(entry.key, "merge_only", reason, occupies=False)
                )
                merge_only_candidates.append(entry)
                continue
            reason = (
                f"live re-check of {entry.key}'s gate this tick reads READY "
                "(coord merge --plan agrees) — resuming from parked without "
                "spending an attempt (#1891, #2182)"
            )
            reconciles.append(
                Reconcile(
                    entry.key,
                    "resumed",
                    reason,
                    occupies=False,
                    # #2273 (post-review): resets `attempts` to 0, mirroring
                    # #2230's blocked-resume — this is genuinely POSITIVE
                    # evidence the gate cleared, the same class `plan_tick`'s
                    # docstring says must fall through with NO backoff. A
                    # parked entry can carry `attempts` from an earlier
                    # death-retry cycle before it ever reached the merge
                    # queue; without this reset a fresh resume could still be
                    # paced against that stale attempt count.
                    updates={
                        "state": STATE_WAITING,
                        "attempts": 0,
                        "last_reason": reason,
                    },
                )
            )
            states[entry.key] = STATE_WAITING
            # #2273 (post-review): same-tick freshness — this entry falls
            # straight into step 4's `waiting` walk below; without this the
            # backoff check there would still see the STALE pre-tick
            # `attempts`, same reasoning as #2230's blocked-resume just
            # below.
            effective_attempts[entry.key] = 0
            continue
        # #2158: a park whose CI reading can still refresh itself is held for
        # as long as it keeps saying so. One that CANNOT — the reading came
        # only from the raw `merge_queue` row's frozen `error`, which is
        # written by a live `coord merge` attempt and by nothing else, and a
        # parked entry runs none — expires, because otherwise the predicate
        # that releases the park is refreshed only by the action the park
        # withholds. See `PARK_STALE_SECONDS`.
        park_expired = _park_reading_expired(entry, facts, now)
        if facts.merge_ci_pending and park_expired is None:
            continue
        # #2063: a Gate-A park is gated on a HUMAN, not on the board, so the
        # `merge_ci_pending` predicate above says nothing about it. Without
        # this branch such an entry would resume on the very next tick and
        # relaunch straight back into the identical refusal, forever — the
        # hot loop that "park, don't block" is supposed to avoid. The
        # shell resolves `gate_a_pending` by re-reading the recorded verdict
        # for the (repo, milestone) embedded in the park reason's marker (a
        # local board read, no `gh` call per entry); an entry it can't
        # resolve stays parked, which fails closed exactly like the guard.
        if is_gate_a_refusal_reason(entry.last_reason):
            if (gate_a_pending or {}).get(entry.key, True):
                continue
            reason = (
                f"Gate A sign-off recorded for {entry.key} — resuming from "
                "parked without spending an attempt (#2063)"
            )
            reconciles.append(
                Reconcile(
                    entry.key,
                    "resumed",
                    reason,
                    occupies=False,
                    # #2273 (post-review): see the #2182 branch's comment
                    # above — same positive-evidence resume, same reset.
                    updates={
                        "state": STATE_WAITING,
                        "attempts": 0,
                        "last_reason": reason,
                    },
                )
            )
            states[entry.key] = STATE_WAITING
            effective_attempts[entry.key] = 0  # #2273: see #2182 branch above
            continue
        # #2234: a policy-refusal park has NO external verdict to poll for —
        # the rule it names is standing, not pending — so unlike the Gate-A
        # branch just above, this one never resumes itself. Without this
        # check the entry would fall straight through to the CI-park
        # "resume" default below (`facts.merge_ci_pending` reads False for
        # it, exactly like a genuinely-cleared CI gate), flipping it back to
        # `waiting` on the very next tick and relaunching `coord drive`
        # straight into the identical refusal — the infinite park/relaunch
        # bounce this check exists to prevent. Stays parked until a human
        # clears it (`coord drive-queue remove`, same as `blocked`).
        if is_policy_refusal_reason(entry.last_reason):
            continue
        # #2977: a throttle-skip park (`github_ops.is_throttle_skip_reason`)
        # carries its OWN known wall-clock expiry (`until=<epoch>`, embedded
        # by `github_ops.format_throttle_skip_reason` at park time) — unlike
        # the CI-park default below, which needs a live re-check because it
        # has no way to know in advance when checks will report, this park
        # can resume on a plain clock comparison, no I/O at all. `now is
        # None` (a pure-logic caller with no clock) degrades to "never
        # resume this tick", same posture #1794's grace window uses for the
        # same input. An unparseable `until` (should not happen for text
        # this module itself wrote) or a wait that has run well past
        # `PARK_STALE_SECONDS` — far longer than any real backoff
        # (`coord.github_throttle.MAX_BACKOFF_S` is 900s) — resumes anyway
        # rather than parking indefinitely on a reading nothing can refresh,
        # the same finite-wait guarantee #2158 already gives the CI park.
        if is_throttle_skip_reason(entry.last_reason):
            if now is None:
                continue
            until = parse_throttle_skip_until(entry.last_reason)
            age = now - entry.reason_at if entry.reason_at is not None else None
            cleared = until is not None and now >= until
            stale = until is None or (age is not None and age > PARK_STALE_SECONDS)
            if not cleared and not stale:
                continue
            reason = (
                (
                    f"the GitHub rate-limit backoff that parked {entry.key} "
                    "has cleared — resuming without spending an attempt "
                    "(#2977)"
                )
                if cleared
                else (
                    f"{entry.last_reason} — this throttle-skip park has run "
                    f"{PARK_STALE_SECONDS / 60:.0f}m+ with no refreshable "
                    "expiry left to trust — resuming rather than parking "
                    "indefinitely (#2977)"
                )
            )
            reconciles.append(
                Reconcile(
                    entry.key,
                    "resumed",
                    reason,
                    occupies=False,
                    updates={
                        "state": STATE_WAITING,
                        "attempts": 0,
                        "last_reason": reason,
                    },
                )
            )
            states[entry.key] = STATE_WAITING
            effective_attempts[entry.key] = 0
            continue
        if park_expired is not None:
            # Deliberately does NOT claim CI has reported — nothing here knows
            # that. It says only that the reading this park rests on has no
            # writer left and has aged past the point of being worth
            # believing, which is a different (and honest) fact (#2158).
            reason = (
                f"park reason for {entry.key} has not been refreshable for "
                f"{park_expired / 60:.0f}m ({facts.merge_ci_pending_reason or 'CI'} "
                "— written by a merge attempt, and a parked entry runs none) "
                "— re-evaluating from waiting without spending an attempt "
                "(#2158)"
            )
        else:
            reason = (
                f"CI checks for {entry.key} have reported — resuming from "
                "parked without spending an attempt (#1891)"
            )
        reconciles.append(
            Reconcile(
                entry.key,
                "resumed",
                reason,
                occupies=False,
                # #2273 (post-review): see the #2182 branch's comment above —
                # both the #2158 stale-park-expiry and #1891 CI-cleared
                # resumes are the same positive-evidence class, same reset.
                updates={
                    "state": STATE_WAITING,
                    "attempts": 0,
                    "last_reason": reason,
                },
            )
        )
        states[entry.key] = STATE_WAITING
        effective_attempts[entry.key] = 0  # #2273: see #2182 branch above

    # #1757 step 2: deploy gates.  Resolved from the POST-reconcile states, so
    # a `--hold-after` entry that reconciled to `blocked` cannot also fire a
    # gate, and `released` falls through to the walk below in this same tick.
    holds = _resolve_holds(ordered, states, probes or {})

    # NOTE: "reconciles" is deliberately NOT in plan_base.  The waiting-entry
    # walk below (#1873) can append to `reconciles` too — a `waiting` entry
    # whose own issue already landed reconciles to `done` there — so every
    # return site passes `reconciles=tuple(reconciles)` explicitly, taken at
    # the point of that return rather than frozen here before the walk runs.
    plan_base = {
        "holds": tuple(holds),
        "occupied": occupied,
        "capacity": capacity,
        # A copy, not the live dict: the walk below mutates its own projection
        # of these counts (it charges the launch to its repo) and the plan must
        # report the reading that `occupied` was taken from.
        "repo_occupied": dict(repo_occupied),
        "repo_capacity": repo_capacity,
        # #2350: fixed by the time reconciliation (steps 1/1b) finishes —
        # nothing below this point (holds, capacity, the launch walk) ever
        # adds to it — so, unlike `reconciles`, every return site can share
        # this one tuple via `**plan_base` instead of re-taking it fresh.
        "merge_only": tuple(merge_only_candidates),
    }

    # #2186: every currently-closed gate, keyed by the entry it was declared
    # on — regardless of scope. `_resolve_prereqs` below consults this to
    # hold a dependent even though its `after=` pre-req has already reconciled
    # to `done` (merged is not live); an ENTRY-scoped gate stops there and
    # nowhere else. A FLEET-scoped one is handled separately, immediately
    # below, exactly as every gate was before #2186.
    held_gates: dict[str, Hold] = {h.key: h for h in holds if h.blocking}

    gate = next((h for h in holds if h.stops_fleet), None)
    if gate is not None:
        # Launch NOTHING — the pre-#2186 behaviour, still available but only
        # for a gate explicitly declared `--scope=fleet`. Not "launch if
        # there is spare capacity", not "launch anything whose pre-reqs don't
        # mention the held entry" — a fleet-scoped gate is a deliberate
        # whole-queue stop, invisible to the dependency graph by design.
        return TickPlan(
            **plan_base,
            reconciles=tuple(reconciles),
            blocked=tuple(blocked),
            deferrals=(),
            alert=_hold_alert(gate),
            launch=None,
        )

    # #2101 step 2b: is THIS host cordoned?  Checked after reconciliation (so
    # a cordoned host still drains its view of reality — see the docstring)
    # and after the deploy gate (which is the older, narrower stop and keeps
    # its own alert), but BEFORE capacity: a cordoned host launches nothing
    # however many slots are free, which is the entire mechanism.
    cordon_map = _normalized_cordons(cordons)
    local_cordon = cordon_map.get(_norm_host(local_host)) if local_host else None
    if local_cordon:
        return TickPlan(
            **plan_base,
            reconciles=tuple(reconciles),
            blocked=tuple(blocked),
            deferrals=(),
            alert=_cordon_alert(local_host or "this host", local_cordon),
            launch=None,
            cordon_reason=local_cordon,
        )

    # #2314 step 2c: is THIS host's own `coord` a drifted editable checkout?
    # Checked right after the cordon (same reasoning: reconciliation must
    # still run — see the docstring) and, like the cordon, before capacity —
    # a drifted checkout launches nothing however many slots are free. This
    # is the escalation of `coord.cli._warn_if_editable_checkout_moved` from
    # advisory-only to an actual refusal.
    if editable_drift is not None:
        drift_repo_root, drift_shown = editable_drift
        drift_text = f"drifted onto {drift_shown} ({drift_repo_root})"
        return TickPlan(
            **plan_base,
            reconciles=tuple(reconciles),
            blocked=tuple(blocked),
            deferrals=(),
            alert=_editable_drift_alert(drift_repo_root, drift_shown),
            launch=None,
            drift_reason=drift_text,
        )

    # #2587 step 2d: is a fleet roll queued for the next inter-drive gap?
    # Checked right after the drift check (same reasoning again: reconciliation
    # above already ran and must stay unaffected) and, like the cordon and
    # drift checks, before capacity — a pending roll launches nothing however
    # many slots are free. No alert here (unlike the cordon/drift branches):
    # #2587 is explicit that a held-for-a-roll queue must never read as
    # broken, and the alert channel is for exactly that — see
    # `coord.commands.drive_queue`'s `status` rendering of the marker itself
    # for the (non-alarming) visibility this state gets instead.
    if roll_pending_reason:
        return TickPlan(
            **plan_base,
            reconciles=tuple(reconciles),
            blocked=tuple(blocked),
            deferrals=(),
            alert=None,
            launch=None,
            roll_pending_reason=roll_pending_reason,
        )

    if capacity - occupied <= 0:
        return TickPlan(
            **plan_base,
            reconciles=tuple(reconciles),
            blocked=tuple(blocked),
            deferrals=(),
            alert=None,
            launch=None,
        )

    # `cycle_keys` is computed once, near the top of this function (see the
    # comment there) — #2362's `blocked`-entry re-check needs it before this
    # point, so it is no longer (re-)derived here.

    def _cooldown_reason(candidate: QueueEntry) -> str:
        """#1794's launch-side guard: '' unless this entry was just launched.

        A `waiting` row carrying a recent `launched_at` means SOMETHING put a
        drive up for this issue moments ago — a retry decided on stale
        evidence, a launch subprocess whose exit code lied, an operator's hand
        edit.  Whatever it was, starting a second `coord drive` now is the
        failure #1794 exists to prevent, so the entry defers and tries again
        on the next tick, by which point the reconcile branches above have
        real evidence to work with.
        """
        age = _startup_cooldown(candidate, now, grace_seconds)
        if age is None:
            return ""
        return (
            f"launched {age:.0f}s ago — inside the {grace_seconds:.0f}s startup "
            f"grace window, so a second `coord drive` is refused (#1794)"
        )

    def _backoff_reason(candidate: QueueEntry) -> str:
        """#2273's launch-side guard: '' unless *candidate* is still inside
        its post-death backoff window.

        Resolves the two "prefer this tick's fresh write" maps
        (`effective_attempts`/`retry_backoff_at_map`) against the entry's own
        persisted fields before delegating to :func:`_retry_backoff_reason` —
        see that function's docstring for why neither can be read straight
        off `candidate`, and why the fallback is `candidate.retry_backoff_at`
        rather than `candidate.reason_at`.

        #2411: the mechanical spacing text that comes back is never returned
        bare — :func:`_augment_backoff_reason` folds in the real death cause
        (resolved the same "prefer this tick's fresh write" way, off
        `effective_last_reason`) so both call sites below — the report-only
        dry-run pass and the persisted `last_reason` write — get the combined
        text for free, with no change needed at either site.

        #2424 follow-up: that same `effective_last_reason` resolution is also
        what `_retry_backoff_reason` needs to answer its own "was this
        actually a dispatch failure" question — see its `own_reason`
        parameter's docstring — so it is resolved once, here, and passed to
        both.
        """
        previous = effective_last_reason.get(candidate.key, candidate.last_reason)
        backoff = _retry_backoff_reason(
            candidate,
            board.facts(candidate.key),
            now,
            effective_attempts.get(candidate.key, candidate.attempts),
            retry_backoff_at_map.get(candidate.key, candidate.retry_backoff_at),
            own_reason=previous,
        )
        if not backoff:
            return ""
        return _augment_backoff_reason(previous, backoff)

    # #1972's projection of per-repo occupancy AS THE WALK SEES IT: the board
    # reading above, plus this tick's own launch once one is chosen.  Kept
    # separate from `repo_occupied` (reported in the plan) so the launch's own
    # slot is charged to the report-only pass — otherwise `--dry-run` would
    # cheerfully explain that the next same-repo entry is eligible, one line
    # under the launch that just took its repo's last slot.
    repo_slots: dict[str, int] = dict(repo_occupied)

    def _repo_limit_reason(candidate: QueueEntry) -> str:
        """#1972's per-repo ceiling: '' unless this entry's repo is full.

        A DEFER, never a block: nothing is wrong with the entry, its position
        does not move, no attempt is spent and nothing escalates.  It is the
        same "not yet" an unsatisfied `after` produces — the difference is only
        that what it is waiting on is its own repo's in-flight drive rather
        than a named pre-req.
        """
        if not repo_capacity:
            return ""
        used = repo_slots.get(candidate.repo, 0)
        if used < repo_capacity:
            return ""
        return (
            f"repo {candidate.repo} at its limit ({used}/{repo_capacity}) — "
            "deferring so a different repo can launch"
        )

    def _cordon_reason(candidate: QueueEntry) -> str:
        """#2101: '' unless this entry is PINNED to a cordoned machine.

        A DEFER, never a block: nothing is wrong with the entry, its position
        does not move and no attempt is spent — its destination is simply
        draining for a release right now and will take work again in minutes.
        Only an explicit ``--machine`` pin is checked here; an unpinned entry
        auto-picks its host at dispatch time, where `coord.drive_state`'s
        machine picker already skips paused machines (a cordon IS a routing
        pause — see `coord.machine_pause`), so guessing a destination here
        would be a second, weaker copy of that decision.
        """
        if not candidate.machine:
            return ""
        reason = cordon_map.get(_norm_host(candidate.machine))
        if not reason:
            return ""
        return (
            f"{candidate.machine} is {reason} — deferring rather than "
            "dispatching into a host that is draining for a release (#2101)"
        )

    launch: QueueEntry | None = None
    # #1873: keys that reconciled straight to `done` in the walk below —
    # landed under someone else's branch/PR, closed by hand as obsolete, or
    # picked up by `coord reconcile-merges` — WITHOUT this queue ever
    # launching them.  These must not count toward the queue-level alert
    # below: they were neither deferred nor blocked, so they have nothing to
    # show up in `details`, and counting them in "considered N" without a
    # matching detail line is exactly the "considered N, N-1 explained"
    # contradiction the "considered N" comment below warns about — see the
    # #1864 incident this branch exists to fix, where the ENTIRE queue was
    # this case and the tick has nothing to be stalled about.
    landed_keys: set[str] = set()
    waiting = [e for e in ordered if states.get(e.key) == STATE_WAITING]
    for entry in waiting:
        if launch is not None:
            # Report-only pass over the tail of the queue.  The launch above
            # already won this tick, so nothing here is mutated (see
            # Deferral.counted) — this exists so `--dry-run` explains the rest
            # of the queue instead of going silent after the first line.
            cooldown = _cooldown_reason(entry)
            if cooldown:
                deferrals.append(Deferral(entry.key, cooldown, counted=False))
                continue
            backoff = _backoff_reason(entry)
            if backoff:
                deferrals.append(
                    Deferral(entry.key, backoff, counted=False, backing_off=True)
                )
                continue
            verdict = _resolve_prereqs(
                entry, board, states, cycle_keys, held_gates, live_prereq_terminal
            )
            if not verdict.satisfied:
                deferrals.append(
                    Deferral(entry.key, verdict.reason, counted=False)
                )
                continue
            cordoned = _cordon_reason(entry)
            if cordoned:
                deferrals.append(
                    Deferral(entry.key, cordoned, counted=False, cordoned=True)
                )
                continue
            repo_limit = _repo_limit_reason(entry)
            if repo_limit:
                deferrals.append(
                    Deferral(
                        entry.key, repo_limit, counted=False, repo_limited=True
                    )
                )
            continue
        cooldown = _cooldown_reason(entry)
        if cooldown:
            deferrals.append(
                Deferral(
                    entry.key,
                    cooldown,
                    updates={
                        "deferrals": entry.deferrals + 1,
                        "last_reason": cooldown,
                    },
                )
            )
            continue
        backoff = _backoff_reason(entry)
        if backoff:
            deferrals.append(
                Deferral(
                    entry.key,
                    backoff,
                    updates={
                        "deferrals": entry.deferrals + 1,
                        "last_reason": backoff,
                    },
                    backing_off=True,
                )
            )
            continue
        # #1873: checked BEFORE `_resolve_prereqs`, not after.  The entry's
        # own board state is unconditional — if this issue is already landed,
        # its `after=` graph is irrelevant, including when that graph is
        # itself unsatisfiable (unknown pre-req, cycle, a pre-req that is
        # `blocked`/`failed`).  Checking prereqs first would route a landed
        # entry with a broken pre-req into the BLOCKED branch below, which
        # escalates and demands a manual `remove && add` for an entry that
        # needs neither — it is already done.  `_reconcile_running` catches
        # this same fact for entries that WERE launched (:813); a `waiting`
        # entry never enters that function at all, so nothing had checked the
        # board against the entry's own issue until now.
        facts = board.facts(entry.key)
        if facts.landed:
            witness = "merged" if facts.merged else "closed"
            reason = (
                f"done — issue already {witness}, never launched by this queue"
            )
            reconciles.append(
                Reconcile(
                    entry.key,
                    "done",
                    reason,
                    occupies=False,
                    # attempts is deliberately NOT incremented: nothing was
                    # ever launched for this entry, so charging it a retry
                    # would be charging it for work that landed elsewhere
                    # (same reasoning as the BLOCKED branch's "operator's
                    # typo" comment just below).
                    updates={
                        "state": STATE_DONE,
                        "last_reason": reason,
                    },
                )
            )
            states[entry.key] = STATE_DONE
            landed_keys.add(entry.key)
            continue
        verdict = _resolve_prereqs(
            entry, board, states, cycle_keys, held_gates, live_prereq_terminal
        )
        if verdict.unsatisfiable:
            blocked.append(
                Blocked(
                    entry.key,
                    verdict.reason,
                    # attempts is deliberately NOT incremented: nothing was
                    # ever launched for this entry, so charging it a retry
                    # would be charging it for the operator's typo.
                    updates={
                        "state": STATE_BLOCKED,
                        "last_reason": verdict.reason,
                    },
                )
            )
            states[entry.key] = STATE_BLOCKED
            continue
        if not verdict.satisfied:
            deferrals.append(
                Deferral(
                    entry.key,
                    verdict.reason,
                    updates={
                        "deferrals": entry.deferrals + 1,
                        "last_reason": verdict.reason,
                    },
                )
            )
            continue
        # #2101, checked with the same "facts about the ENTRY come first"
        # rule #1972 states below: a landed entry still reconciles to `done`
        # and a broken pre-req still blocks and escalates, whatever its
        # pinned machine's cordon is doing this tick.  Only the LAUNCH is
        # withheld.
        cordoned = _cordon_reason(entry)
        if cordoned:
            deferrals.append(
                Deferral(
                    entry.key,
                    cordoned,
                    updates={
                        "deferrals": entry.deferrals + 1,
                        "last_reason": cordoned,
                    },
                    # Same posture as #1972's repo limit: this is the fleet
                    # working as designed (a host draining so it can be
                    # rolled), not a stalled queue, so it must not raise the
                    # queue-level alert every tick for the duration of a
                    # drain.  The cordon has its OWN alert when it is THIS
                    # host that is stopped — see `_cordon_alert`.
                    cordoned=True,
                )
            )
            continue
        # #1972, checked LAST: everything above is a fact about the ENTRY (is
        # it still starting, has it already landed, are its pre-reqs sound),
        # and those verdicts must not change because some unrelated drive in
        # the same repo happens to be up.  In particular an unsatisfiable
        # pre-req still blocks and escalates here rather than hiding behind a
        # repo-limit deferral that would silently postpone it forever.
        repo_limit = _repo_limit_reason(entry)
        if repo_limit:
            deferrals.append(
                Deferral(
                    entry.key,
                    repo_limit,
                    updates={
                        "deferrals": entry.deferrals + 1,
                        "last_reason": repo_limit,
                    },
                    repo_limited=True,
                )
            )
            continue
        launch = by_key[entry.key]
        # The launch takes its repo's slot for the rest of THIS walk, so the
        # report-only tail explains the remaining same-repo entries correctly.
        repo_slots[launch.repo] = repo_slots.get(launch.repo, 0) + 1

    alert: QueueAlert | None = None
    # `waiting`, minus anything the walk above reconciled straight to `done`
    # (#1873) — those were never deferred or blocked, so they have no line in
    # `details` and must not be counted as "considered" either.  What is left
    # is exactly the set of entries that are genuinely still waiting: deferred
    # or blocked, each with a matching `details` entry.
    still_waiting = [e for e in waiting if e.key not in landed_keys]
    # #1972: minus anything whose ONLY reason for standing still is that its
    # own repo is busy.  That is the queue doing its job — the same condition
    # the global at-capacity return above answers with `alert=None` — and a
    # 39-entry single-repo queue would otherwise escalate on every tick for the
    # duration of the batch.  A MIXED tick still alerts: if even one entry is
    # deferred on a pre-req or blocked outright, something really is stuck and
    # the alert names all of it, repo-limit lines included.
    # #2101 adds the release cordon to that same set: an entry pinned to a
    # host that is draining for a release is waiting on the fleet working, not
    # on something wedged. See `Deferral.benign`.
    benign_keys = {item.key for item in deferrals if item.benign}
    stalled = [e for e in still_waiting if e.key not in benign_keys]
    if launch is None and stalled:
        details = [f"{item.key}: {item.reason}" for item in deferrals]
        details += [f"{item.key}: BLOCKED — {item.reason}" for item in blocked]
        alert = QueueAlert(
            # "considered N" rather than "N waiting": some of those entries are
            # blocked by the time this line is written, and an alert that
            # contradicts `coord drive-queue status` two lines below it is an
            # alert operators learn to distrust.
            reason=(
                f"nothing eligible to launch: considered {len(still_waiting)} "
                f"waiting entr{'y' if len(still_waiting) == 1 else 'ies'}, "
                f"{capacity - occupied} free slot(s)"
            ),
            details=tuple(details),
        )

    return TickPlan(
        **plan_base,
        reconciles=tuple(reconciles),
        blocked=tuple(blocked),
        deferrals=tuple(deferrals),
        alert=alert,
        launch=launch,
    )


# ── rendering (pure, so `--dry-run` is testable without a CLI) ───────────────


def render_plan(plan: TickPlan, *, dry_run: bool = False) -> list[str]:
    """The human-readable form of a :class:`TickPlan`, one line per element."""
    prefix = "would " if dry_run else ""
    lines = [
        f"capacity: {plan.occupied}/{plan.capacity} occupied, "
        f"{plan.free_slots} free"
    ]
    if plan.repo_capacity:
        # #1972: "1/3 occupied" alone cannot answer "so why didn't item 2 go?"
        # — the answer is per-repo, so print the breakdown rather than making
        # the operator read the code.  The provenance is spelled out because
        # this counter inherits rule 1 (board state, not live sessions): a
        # drive whose observer died still holds its repo's slot, and after
        # #1972 that wedges ONE repo instead of the whole queue, which is
        # better but also much quieter.
        detail = ", ".join(
            f"{repo} {count}/{plan.repo_capacity}"
            for repo, count in sorted(plan.repo_occupied.items())
        )
        lines.append(
            f"  per-repo: {detail or 'no repo occupied'} (limit "
            f"{plan.repo_capacity}/repo, counted from board state — a drive "
            "whose observer died still holds its repo's slot)"
        )
    for item in plan.reconciles:
        lines.append(f"  reconcile {item.key}: {item.outcome} — {item.reason}")
    # #2350: printed right after the reconciles, same reasoning as the hold
    # line below — "merge_only" and "attempting the merge directly" are one
    # thought, not two lines an operator has to correlate by key.
    for item in plan.merge_only:
        lines.append(f"  {prefix}merge --only {item.key}")
    # #1757: the gate line goes directly under its reconcile, because "1753
    # done" immediately followed by "and therefore nothing launches" is the
    # sentence an operator reading a timer log needs to read as one thought.
    for item in plan.holds:
        probe = ""
        if item.resume_when:
            probe = f" [resume-when: {item.resume_when}"
            if item.probes:
                probe += f", {item.probes} failed attempt(s)"
            if item.probe_detail:
                probe += f" — {item.probe_detail}"
            probe += "]"
        # #2186: only the non-default scope is worth a word — an unlabeled
        # hold line is, as always, entry-scoped.
        scope_tag = " [scope=fleet]" if item.stops_fleet else ""
        lines.append(
            f"  hold {item.key}: {item.outcome} — {item.reason}{probe}{scope_tag}"
        )
    for item in plan.blocked:
        lines.append(f"  {prefix}block {item.key}: {item.reason}")
    # Counted deferrals come BEFORE the launch line and report-only ones after,
    # so the output reads in the order the walk actually happened: these lost
    # their turn while a slot was free; that one took it; the rest were never
    # reached.
    for item in plan.deferrals:
        if item.counted:
            lines.append(f"  defer {item.key}: {item.reason}")
    if plan.launch is not None:
        target = plan.launch
        pinned = f" on {target.machine}" if target.machine else ""
        lines.append(f"  {prefix}launch {target.key}{pinned}")
    elif plan.held is not None and plan.held.stops_fleet:
        # #2186: only a FLEET-scoped hold explains "no launch" on its own —
        # an entry-scoped one may have let something else launch, or may have
        # left its dependent explained by an ordinary `defer` line above (and
        # the "nothing eligible" alert below), so it falls through to the
        # generic branches instead of this one.
        lines.append(
            f"  no launch — HELD by the fleet-wide deploy gate on "
            f"{plan.held.key} (release with `coord drive-queue resume`)"
        )
    elif plan.cordon_reason:
        # #2101 trap E: naming the cordon here is the difference between a
        # journal that reads "the fleet is upgrading itself" and one that
        # reads "the queue mysteriously stopped".
        lines.append(
            f"  no launch — this host is {plan.cordon_reason}; in-flight "
            "drives are draining and the queue resumes once it is rolled"
        )
    elif plan.drift_reason:
        # #2314: same "name it or it reads as a mystery" reasoning as the
        # cordon branch above — this host's own `coord` is untrustworthy
        # right now, not the queue.
        lines.append(f"  no launch — this host's coord is {plan.drift_reason}")
    elif plan.roll_pending_reason:
        # #2587: same "name it or it reads as a mystery" reasoning again —
        # this is the fleet deliberately holding for its own upcoming roll,
        # not a stall. Fires the instant `occupied` (just above, unaffected
        # by this branch) reads 0 — see `coord.commands.drive_queue`'s tick
        # shell for the actual trigger.
        lines.append(
            f"  no launch — {plan.roll_pending_reason} "
            f"({plan.occupied} entries still occupying a slot)"
        )
    elif plan.capacity and plan.free_slots == 0:
        # Naming the reason matters more here than anywhere else in this
        # render: #1794 was diagnosed entirely from a journal, and "no launch"
        # on its own is indistinguishable from a stalled queue.
        lines.append(
            f"  no launch — at capacity ({plan.occupied}/{plan.capacity} occupied)"
        )
    elif plan.deferrals and all(item.benign for item in plan.deferrals):
        # Same reasoning as the at-capacity line above: with free GLOBAL slots
        # and no launch, a bare "no launch" reads as a stalled queue in a
        # journal.  This one is saturated per repo (or draining for a release
        # — #2101, or pacing a retry — #2273), not stalled, and unlike the
        # global case it raises no alert, so this line is the only place it
        # is ever said.
        cordoned = any(item.cordoned for item in plan.deferrals)
        repo_limited = any(item.repo_limited for item in plan.deferrals)
        backing_off = any(item.backing_off for item in plan.deferrals)
        active = [c for c in (cordoned, repo_limited, backing_off) if c]
        if len(active) > 1:
            # Post-review fix: a MIXED benign set (e.g. one cordoned entry,
            # one backing off) used to have the cordon branch's message
            # stand in for the WHOLE line below, silently dropping the other
            # cause(s) even though the per-entry `defer` lines above still
            # named them individually — this summary line is the one place
            # an operator scanning a journal for "why no launch" would
            # otherwise miss it. Named separately from the single-cause
            # branches below so their common case keeps its exact wording.
            parts = []
            if cordoned:
                parts.append(
                    "pinned to a machine under a release cordon "
                    "(draining to be rolled)"
                )
            if repo_limited:
                parts.append(f"repo-limited ({plan.repo_capacity}/repo)")
            if backing_off:
                parts.append("pacing a retry after a recent failure (#2273)")
            lines.append(
                "  no launch — every waiting entry is deferred for a benign "
                "reason (mixed causes): " + "; ".join(parts)
            )
        elif cordoned:
            lines.append(
                "  no launch — every waiting entry is pinned to a machine "
                "under a release cordon (draining to be rolled)"
            )
        elif repo_limited:
            lines.append(
                f"  no launch — every waiting entry's repo is at its per-repo "
                f"limit ({plan.repo_capacity}/repo)"
            )
        else:
            lines.append(
                "  no launch — every waiting entry is pacing a retry after a "
                "recent failure (#2273); it resumes once the backoff elapses"
            )
    else:
        lines.append("  no launch")
    for item in plan.deferrals:
        if not item.counted:
            lines.append(
                f"  defer {item.key}: {item.reason} (not reached this tick)"
            )
    if plan.alert is not None:
        lines.append(f"  {prefix}alert: {plan.alert.reason}")
        lines.extend(f"    {detail}" for detail in plan.alert.details)
    return lines
