"""Board-doctor sweep: the scheduled harness, not the per-cause actions (#3758).

#3745's epic asks for a hourly sweep over the drive-queue and merge-queue
that, for every stuck/blocked/stalled row: (1) classifies its cause+tier via
the existing #2276 Phase 1 classifier (:mod:`coord.queue_diagnose`), (2)
hands auto-actionable causes to a C3 action dispatcher and alert-only causes
to a C4 escalation path, and (3) records every decision with provenance, in
the spirit of #1478's board comments. This module is the harness that does
exactly that loop — and *only* that loop. The permission model (#3745's C2:
which cause gets which tier) and the per-cause action dispatchers (C3) and
escalation surface (C4) live in :mod:`coord.board_doctor_policy`, a sibling
module; this harness does not invent their policy. :func:`default_tier_of`
delegates to that module's :func:`coord.board_doctor_policy.policy_tier_of`
so the two never drift apart, with `tier_of`/`auto_dispatchers`/
`alert_dispatcher` left as the seams the CLI wires in without touching
:func:`sweep`'s control flow.

:func:`sweep` is the pure, fully-testable core: given already-classified
rows and dispatcher callbacks, it decides tier, tracks convergence, dispatches
and logs — no board connection, no `gh`, no network. :func:`collect_rows`
is the impure production walk (drive-queue via
:func:`coord.queue_diagnose.run_pass`, mirroring
``coord drive-queue diagnose``'s own wiring exactly rather than a second
read of the same queue; merge-queue via a local, `gh`-free cause mapping,
since :mod:`coord.queue_diagnose` only understands drive-queue rows).
:func:`run_sweep` is the production entry point the CLI calls, composing
the two.

Convergence (load-bearing)
----------------------------------
A row stuck on the SAME cause across :data:`CONVERGENCE_LIMIT` (default 3)
consecutive sweeps stops being re-acted on and falls back to the alert
tier. Per-(queue, key) sweep counts persist in `board_meta` via
:mod:`coord.state`'s ``record_board_doctor_observation`` /
``mark_board_doctor_escalated`` — the same one-row-of-truth pattern
``baseline_red_streaks`` uses for its own streak counter (one question,
one answer; convergence is decided by reading that ONE counter, never
re-derived from the decision log).

Converging never means going silent: a still-unresolved cause keeps
getting re-alerted on an exponential backoff
(``coord.state.BOARD_DOCTOR_BACKOFF_INITIAL_HOURS`` doubling each firing
up to ``BOARD_DOCTOR_BACKOFF_CAP_HOURS``,
``coord.state.board_doctor_alert_due``) rather than escalating exactly
once and then being ignored forever — a row that never gets better is
exactly the one that must not go quiet.

"CONSECUTIVE" is only honest because :func:`sweep` also clears a
(queue, key)'s counter the moment that row stops appearing in the current
*rows* — ``coord.state.clear_board_doctor_row``, driven by the
*clear_absent_rows* parameter (default on). Without it a resolved-then-recurring
row would resume its stale count instead of starting over, and a row that
escalated once could never restart its backoff clock after it later
resolves and recurs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Collection, Mapping, Sequence

from coord import queue_diagnose
from coord.models import Board

_log = logging.getLogger(__name__)

TIER_AUTO = "auto"
TIER_ALERT = "alert"

#: Third state an ``alert_dispatcher`` may return, alongside truthy
#: (delivered) and falsy (not delivered): the dispatcher deliberately
#: filed nothing for this row — e.g. a more specific alert from another
#: writer already covers the same issue. :func:`sweep` records it as
#: ``action="alert-skipped"`` and leaves the row UN-escalated, so once
#: that cover goes away a later sweep attempts the alert again.
ALERT_SKIPPED = "skipped"

#: Sweeps a row may stay on the same cause before the harness stops
#: re-acting on it and falls back to the (backoff-gated) alert tier.
CONVERGENCE_LIMIT = 3

#: Merge-queue states this sweep treats as "stuck". A row merely waiting
#: its turn (``pending``/``merging``) or past (``merged``/``skipped``) is
#: not this doctor's problem — only a row that cannot progress on its own.
_MERGE_QUEUE_STUCK_CAUSE: dict[str, str] = {
    "conflict": queue_diagnose.CAUSE_MERGE_CONFLICT,
    "human_required": queue_diagnose.CAUSE_GATE_BLOCKED,
}

#: #3760 Action 1: a drive-queue row whose *live* evidence reads completely
#: clear — review-approved (``gate_ready``), every check green, and PR
#: mergeable — but whose entry is sitting in ``blocked`` only because it
#: already gave up on #2972's fix-round ceiling. Deliberately just
#: ``CAUSE_NOTHING_BLOCKING``, not ``CAUSE_CI_GREEN`` too:
#: ``queue_diagnose._rule_ci_green`` fires on green checks alone, and sits
#: BELOW ``_rule_nothing_blocking`` in rule order, so it is only ever
#: reached when ``gate_ready``/``mergeable`` were NOT both confirmed true —
#: i.e. exactly the rows this set must exclude. ``queue_diagnose.diagnose``
#: itself must never be handed the entry's own stated reason to pick a
#: cause — it reads live evidence only.
_LIVE_CLEAR_CAUSES = frozenset({queue_diagnose.CAUSE_NOTHING_BLOCKING})

#: A narrow cause minted by :func:`_reclassify_ceiling_blocked`, never by
#: `queue_diagnose` itself. Keeping it separate from `_LIVE_CLEAR_CAUSES`
#: is the whole point: promoting either of those two causes to auto-tier
#: directly would hand every "nothing blocking"/"ci green" row — including
#: ones that are not ceiling-blocked at all — to an auto-dispatcher, which
#: would permanently cut them off from C4 escalation. Only a row this
#: reclassifier has independently confirmed IS ceiling-blocked gets
#: relabelled, so the broad causes stay alert-eligible for everything else.
CAUSE_CEILING_BLOCKED_MERGEABLE = "ceiling-blocked-mergeable"

#: #3760 Action 5: a Test-stage smoke leg that ended its `claude -p` session
#: without printing a verdict marker line often enough to exhaust
#: `coord.smoke.MUTE_SMOKE_LEG_BUDGET` — `coord.notify._record_smoke_verdict`
#: parks the assignment at `test_state="blocked"` instead of re-dispatching
#: it forever (#2272), so without this cause the row sits there until a
#: human notices. Registered in `coord.board_doctor_policy.ALLOWLIST`: the
#: safe auto-action (`coord.board_doctor_permissions.sweep_smoke_requeue`,
#: via the `smoke_requeue` sweep action) clears the parked `test_state` so
#: the assignment re-enters the normal Test-stage dispatch scan, bounded by
#: its own `coord.smoke.BOARD_DOCTOR_REQUEUE_BUDGET` (#3810) — a row that
#: keeps muting after each requeue is NOT cleared again once that budget is
#: spent, so it keeps matching this cause's detection predicate every
#: sweep and its convergence counter escalates to a human instead of the
#: requeue repeating forever.
CAUSE_SMOKE_NO_VERDICT = "smoke-no-verdict"

#: A repo whose merge base has hit `coord.state.BASELINE_RED_STREAK_LIMIT`
#: consecutive confirmed-red classifications (#3386) poisons every row in
#: both queues for that repo — the rows are not individually stuck, the
#: base is red. Never registered in `coord.board_doctor_policy.ALLOWLIST`:
#: there is no safe auto-action for "the base is broken", only a human
#: decision to go fix it. :func:`baseline_red_rows` collapses every
#: affected repo to exactly ONE such row rather than one per queue entry,
#: so a repo with ten blocked PRs produces one alert, not ten.
CAUSE_BASELINE_RED = "baseline-red"

#: A narrow cause minted by :func:`_reclassify_checks_stale_race`, never by
#: `queue_diagnose` itself (#3859) — mirrors :data:`CAUSE_CEILING_BLOCKED_
#: MERGEABLE`'s own "promote only an independently-confirmed subset of
#: :data:`_LIVE_CLEAR_CAUSES`" shape. A drive-queue row whose live evidence
#: reads completely clear (review approved, Test passed, PR mergeable) but
#: whose owning `coord drive` session already gave up because it kept
#: losing a race against sibling PRs merging into the same repo faster than
#: its own re-run CI could finish (quadraui#1961) — never a problem with
#: the branch itself. See `coord.drive_queue.is_checks_stale_race_giveup_
#: reason` for the exact marker this reclassifier keys on.
CAUSE_CHECKS_STALE_RACE = "checks-stale-race"

#: #3887 (#3875 ask A): a narrow cause minted by :func:`_reclassify_stale_latch`,
#: never by `queue_diagnose` itself — the same "promote only an
#: independently-confirmed subset of a broader cause" shape as
#: :data:`CAUSE_CEILING_BLOCKED_MERGEABLE`. A merge-queue row latched
#: ``HUMAN_REQUIRED`` (:data:`_MERGE_QUEUE_STUCK_CAUSE`'s
#: ``CAUSE_GATE_BLOCKED`` — never ``CAUSE_MERGE_CONFLICT``: that cause is
#: GitHub's own live ``mergeable is False`` reading, which approval alone
#: cannot override, see :func:`_reclassify_stale_latch`'s docstring)
#: whose approval is ALSO confirmed, live, bound to the branch's current
#: content (`coord.merge_queue.has_approved_review`) — quadraui#2034's
#: shape: the latch is stale archaeology, not a live blocker, but nothing
#: re-tests it until a human runs `coord merge --only
#: --override-human-required` or `--drop` + re-enqueue. Registered in
#: `coord.board_doctor_policy.ALLOWLIST` against the `clear_latch` action
#: (`coord.board_doctor_permissions.sweep_clear_latch`, via #3881's
#: investigator wrapper), whose own three gates (latched, approval bound
#: to current content, live mergeability) are the real precondition —
#: this cause only decides which rows are worth the attempt, same
#: division of labor as every other allowlisted cause.
CAUSE_LATCH_STALE_MERGEABLE = "latch-stale-mergeable"

#: #3887: a `PENDING` merge-queue row `_MERGE_QUEUE_STUCK_CAUSE` otherwise
#: never names (a row merely waiting its turn is not this doctor's
#: problem) — ``entry.last_attempt`` being set AND
#: :data:`MERGE_PENDING_STALE_AFTER_SECONDS` old is only the CHEAP,
#: no-`gh` pre-filter :func:`merge_queue_rows` applies; staleness alone is
#: NOT proof every gate reads clear (a conflict-fix rebase just returning
#: the entry to `PENDING` leaves `last_attempt` at its old, pre-conflict
#: stamp while CI re-runs on the new head; a #3886 per-repo merge-gate
#: reservation leaves `last_attempt` stale for as long as it holds — #3887
#: review, two reachable false positives). :func:`_confirm_merge_pending_
#: stuck` re-confirms, LIVE, via `coord.merge_queue.entry_gate_status`
#: (review/smoke/CI/epic-keyword/reservation, the SAME order `process()`
#: evaluates), and drops the row entirely unless that read is
#: `coord.merge_queue.PLAN_READY` — only THEN does this cause survive to
#: the `merge` action (`coord.merge_queue.process()` via #3881's
#: investigator wrapper, `coord.board_doctor_permissions.
#: investigator_merge`), which re-confirms every gate live a second time
#: right before merging anything.
CAUSE_MERGE_PENDING_STUCK = "merge-pending-not-progressing"

#: #3887, pairs with #3871: a `HUMAN_REQUIRED` merge-queue row whose
#: `entry.error` carries `coord.merge_queue.CONFLICT_FIX_HEAD_UNMOVED_
#: SIGNAL` — a conflict-fix worker completed cleanly but never actually
#: pushed, and #3871's own completion-time redispatch
#: (`coord.reconcile._try_ordinary_escalation_after_head_unmoved`) could
#: not find a capable machine right then. That redispatch is a one-shot
#: synchronous attempt; this cause is the periodic net that retries it on
#: a later sweep, still within the SAME retry cap #3871 already enforces
#: (`coord.conflict_fix.dispatch_conflict_fix`'s own `after_head_unmoved_
#: fix_assignment_id` carve-out) — never a fresh budget, never the
#: ordinary give-up escalation a real conflict would trigger. Registered
#: against the `conflict_fix_redispatch` action (`coord.board_doctor_
#: permissions.sweep_redispatch_head_unmoved_conflict_fix`).
CAUSE_CONFLICT_FIX_HEAD_UNMOVED = "conflict-fix-head-unmoved"

#: #3887: how long a `PENDING` merge-queue entry must sit past its last
#: merge attempt before :data:`CAUSE_MERGE_PENDING_STUCK` fires — long
#: enough that a row merely waiting a few ordinary ticks for its turn
#: never matches, short enough that a genuinely stuck green/approved PR
#: does not wait indefinitely for a human to notice.
MERGE_PENDING_STALE_AFTER_SECONDS = 1800.0

#: #3901 (#3887 case 4, the remainder #3887 itself claimed but never
#: wired): a `blocked`/`parked` drive-queue row that was DISPATCHED at
#: least once (it has at least one `coord.gates.assignments_for_issue`
#: board assignment behind it, which is NOT necessarily a branch or a PR —
#: not the #3874 never-dispatched shape) whose stored `last_reason` is
#: still `coord.drive_queue._resolve_prereqs`'s own unsatisfiable-`after=`
#: verdict, even though every named pre-req now reads terminal.
#:
#: `coord.drive_queue._reconcile_blocked_after` already resumes a LIVE
#: `blocked` row matching this exact shape on every ordinary drive-queue
#: tick (#2362/#2756) — but only while the periodic tick is still walking
#: that row at all. A row #3888's `no_progress_rows` surfaces here reached
#: this sweep precisely because the episode/no-progress walk, not the
#: ordinary tick, is the thing still looking at it (a budget-exhausted
#: `CAUSE_UNKNOWN` episode `_episode_stuck_row` would otherwise silently
#: drop — see that function's own docstring), so this is the belt the
#: ordinary tick's suspenders do not reach, never a second, competing
#: implementation of the same resume: see
#: :func:`_reclassify_after_prereqs_landed`.
CAUSE_AFTER_PREREQS_LANDED = "after-prereqs-landed"

#: The ONLY incoming `queue_diagnose` causes :func:`_reclassify_after_
#: prereqs_landed` may promote away from (#3901 review) — both mean the
#: live diagnosis found nothing actionable about THIS entry's own
#: PR/branch lifecycle (`CAUSE_UNKNOWN`: no rule matched, or the probe
#: read nothing at all; `CAUSE_NO_PR`: the PR probe ran and confirmed no
#: PR exists). Every other named cause (`CAUSE_PR_MERGED`,
#: `CAUSE_MERGE_CONFLICT`, `CAUSE_CI_RED`, `CAUSE_CI_PENDING`, ...) is live
#: evidence about a DIFFERENT, already-actionable blocker on the entry's
#: own PR — `ALLOWLIST`/`INVESTIGATE_CAUSES` already route some of those
#: to their own real fix (e.g. `CAUSE_PR_MERGED` -> `mark_done`, freeing
#: the stale assignment slot) and this reclassifier must never relabel one
#: of those into a blunt `drive_queue_requeue` remove+re-add instead,
#: losing the dispatch that cause exists to trigger — the same reasoning
#: :func:`_reclassify_stale_latch` spells out for why it excludes
#: `CAUSE_MERGE_CONFLICT`. Same shape as the other three reclassifiers in
#: this module (:data:`_LIVE_CLEAR_CAUSES`, `_reclassify_stale_latch`'s own
#: `CAUSE_GATE_BLOCKED` check): require a specific incoming cause, never
#: "any cause at all".
_AFTER_PREREQS_LANDED_SAFE_CAUSES = frozenset(
    {queue_diagnose.CAUSE_UNKNOWN, queue_diagnose.CAUSE_NO_PR}
)


def default_tier_of(cause: str) -> str:
    """Default `tier_of`: delegates to the real C2 policy.

    This harness owns the loop, not the per-cause tier decision — that
    policy lives in :func:`coord.board_doctor_policy.policy_tier_of`, and
    this function exists only so `sweep`/`run_sweep` have a safe default
    that can never silently diverge from it. Deferred import: by the time
    any caller actually invokes a `tier_of`, this module has finished
    loading, so there is no import cycle to worry about.
    """
    from coord.board_doctor_policy import policy_tier_of  # noqa: PLC0415

    return policy_tier_of(cause)


def default_investigate_capacity(config) -> Callable[[StuckRow], bool]:
    """Default `capacity_check` builder: delegates to the real per-repo cap.

    Mirrors :func:`default_tier_of`'s own rationale exactly: `sweep`/
    `run_sweep` need a safe default that can never silently diverge from
    the real policy, which lives in
    :func:`coord.board_doctor_policy.make_investigate_capacity_check`
    (#3770). Deferred import for the same reason as `default_tier_of` —
    no import cycle once this module has finished loading.
    """
    from coord.board_doctor_policy import make_investigate_capacity_check  # noqa: PLC0415

    return make_investigate_capacity_check(config)


class _CapacityCheckUnset:
    """Sentinel TYPE distinguishing "caller did not pass `capacity_check` at
    all" (the default: `run_sweep` builds the real per-repo cap via
    :func:`default_investigate_capacity`) from "caller explicitly passed
    `capacity_check=None`" (disable capacity-gating entirely — e.g. a test
    that wants `sweep`'s other behaviour with no cap in play). A plain
    ``None`` default could not tell these two cases apart. A dedicated
    class (rather than a bare ``object()``) lets `run_sweep`'s own
    *capacity_check* parameter name this sentinel's type honestly in its
    annotation instead of silently accepting a value outside it.
    """


_CAPACITY_CHECK_UNSET = _CapacityCheckUnset()


@dataclass(frozen=True)
class StuckRow:
    """One drive-queue or merge-queue row this sweep looked at."""

    queue: str  # "drive-queue", "merge-queue", "baseline-red", or "smoke-no-verdict"
    key: str
    state: str
    cause: str
    confidence: str
    evidence: tuple[str, ...] = ()
    #: #3761: True for a real per-row entry :func:`collect_rows` is folding
    #: into a repo-wide :func:`baseline_red_rows` alert rather than acting
    #: on individually. :func:`sweep` must still count it as present (so
    #: `clear_absent_rows` does not treat the fold as "resolved" and wipe
    #: its convergence counter / dismiss its live escalation — the row's
    #: own cause is untouched, only superseded for THIS sweep) while
    #: skipping its own dispatch/counter update entirely — the collapsed
    #: baseline-red row is what alerts, once per repo, not this one.
    suppressed: bool = False
    #: #3777: True only for a row whose cause/confidence this SWEEP itself
    #: just computed from live evidence (:func:`drive_queue_rows`,
    #: :func:`merge_queue_rows`, :func:`baseline_red_rows`,
    #: :func:`_refresh_ceiling_blocked_settled_rows`) — never for
    #: :func:`_episode_stuck_row`'s reconstruction of a *stored* diagnosis,
    #: which can be arbitrarily old and is never re-confirmed by that path.
    #: `_fixit_rebudget_environmental` (`coord/board_doctor_policy.py`)
    #: requires this before it will let a high-confidence cause read as
    #: environmental and skip the #2972 ceiling: bypassing the ceiling on a
    #: stale stored verdict (e.g. a `merge-conflict`/`high` row whose PR has
    #: since merged) would spend this issue's one lifetime fix-it
    #: auto-rebudget leg (#3847) against evidence that is no longer true,
    #: which is exactly what the ceiling exists to stop.
    fresh: bool = False


@dataclass(frozen=True)
class Decision:
    """One row's outcome from a single sweep — the unit both the test
    suite and the decision log assert on."""

    queue: str
    key: str
    cause: str
    tier: str
    #: "auto-dispatch" | "auto-dispatch-noop" | "alert" | "would-alert" |
    #: "alert-failed" | "alert-skipped" | "no-dispatcher" | "converged-noop" |
    #: "suppressed-baseline-red" | "investigate-deferred"
    action: str
    count: int
    escalated: bool
    dry_run: bool


@dataclass
class SweepReport:
    """What one call to :func:`sweep` did, for the CLI and for tests."""

    sweep_id: str
    dry_run: bool = False
    decisions: list[Decision] = field(default_factory=list)
    #: ``len(rows)`` this sweep classified — including rows that needed
    #: no decision (e.g. a cause with no dispatcher wired yet). Distinct
    #: from ``len(decisions)``, which is always equal to this today but
    #: is kept separate so a future CLI reader never has to assume the
    #: two stay in lockstep. Lets the CLI's empty-case message say what
    #: was actually observed ("walked N row(s), 0 needed a decision")
    #: instead of asserting nothing is stuck when rows were simply
    #: filtered out upstream of :func:`sweep` (#3758).
    rows_observed: int = 0


def merge_queue_rows(entries: Sequence[Any], *, now: float | None = None) -> list[StuckRow]:
    """Local cause mapping for merge-queue rows (#3758).

    :func:`coord.queue_diagnose.run_pass` only understands drive-queue
    ``QueueEntry`` rows (#2276 Phase 1); the merge-queue's own state
    vocabulary is already thin (``pending``/``merging``/``merged``/
    ``conflict``/``skipped``/``human_required``), so this reuses
    ``queue_diagnose``'s ``CAUSE_*`` vocabulary rather than inventing a
    second one, and needs no `gh` call — the already-refreshed
    ``QueuedMerge.state``/``.error`` is evidence enough for a cause this
    coarse.

    Two #3887 refinements on top of the plain state->cause table
    (:data:`_MERGE_QUEUE_STUCK_CAUSE`):

    * A ``human_required`` entry whose ``error`` carries
      `coord.merge_queue.CONFLICT_FIX_HEAD_UNMOVED_SIGNAL` is relabelled
      :data:`CAUSE_CONFLICT_FIX_HEAD_UNMOVED` instead of the generic
      :data:`coord.queue_diagnose.CAUSE_GATE_BLOCKED` — this is a sharper
      reading of the SAME live evidence `_MERGE_QUEUE_STUCK_CAUSE` already
      has in hand, not a reclassification of a settled verdict, so it
      belongs here rather than in a post-hoc ``_reclassify_*`` pass.
    * A ``pending`` entry (otherwise invisible to this function entirely —
      waiting its turn is not stuck) whose ``last_attempt`` is set and at
      least :data:`MERGE_PENDING_STALE_AFTER_SECONDS` old is labelled
      :data:`CAUSE_MERGE_PENDING_STUCK` — but only as a cheap candidate
      filter. Staleness alone is not proof the row is actually stuck
      (see :func:`_confirm_merge_pending_stuck`'s docstring), so this
      label never reaches a caller un-confirmed: :func:`collect_rows`
      always runs it through that live re-check before the cause is
      acted on or escalated. #3918 review: an entry whose ``last_attempt``
      is ``None`` (the merge gate has never actually been evaluated at
      all) is deliberately NOT folded into this filter even though it
      looks like the same shape aged off ``enqueued_at`` instead — this
      function's own ``cause`` becomes the caller's ``covered_keys``
      BEFORE :func:`_confirm_merge_pending_stuck` gets a chance to drop an
      unconfirmed candidate, so minting a row here that confirmation then
      silently drops (its own documented, correct behavior for a row that
      is merely waiting on something ordinary — CI still running, a
      #3886 reservation held) would make the row invisible to
      :func:`no_progress_rows` too, for the one state that caller's own
      nudge-based `running` shape (#3918) is positioned to catch instead:
      the owning drive is very likely still alive and polling, so leaving
      this entry OFF this filter is what lets that shape see it.

    *now* defaults to :func:`time.time` — overridable so a replay test can
    assert on a fixed, reproducible staleness window.
    """
    import time as _time  # noqa: PLC0415

    from coord.merge_queue import CONFLICT_FIX_HEAD_UNMOVED_SIGNAL, PENDING  # noqa: PLC0415

    moment = _time.time() if now is None else now
    rows: list[StuckRow] = []
    for entry in entries:
        cause = _MERGE_QUEUE_STUCK_CAUSE.get(entry.state)
        if (
            cause == queue_diagnose.CAUSE_GATE_BLOCKED
            and CONFLICT_FIX_HEAD_UNMOVED_SIGNAL in (entry.error or "")
        ):
            cause = CAUSE_CONFLICT_FIX_HEAD_UNMOVED
        if cause is None:
            if (
                entry.state == PENDING
                and entry.last_attempt is not None
                and moment - entry.last_attempt >= MERGE_PENDING_STALE_AFTER_SECONDS
            ):
                cause = CAUSE_MERGE_PENDING_STUCK
            else:
                continue
        evidence = (f"merge_queue.state={entry.state}",)
        if entry.error:
            evidence += (f"error={entry.error}",)
        rows.append(
            StuckRow(
                queue="merge-queue",
                key=f"{entry.repo_name}#{entry.issue_number}",
                state=entry.state,
                cause=cause,
                confidence=queue_diagnose.CONFIDENCE_HIGH,
                evidence=evidence,
                fresh=True,
            )
        )
    return rows


def _confirm_merge_pending_stuck(
    rows: list[StuckRow], merge_entries: Sequence[Any], *, board: Board, config
) -> list[StuckRow]:
    """Keep :data:`CAUSE_MERGE_PENDING_STUCK` only for a row this sweep can
    confirm, LIVE, actually reads green/approved/mergeable (#3887).

    :func:`merge_queue_rows` mints this cause off ``entry.last_attempt``
    staleness alone, which is not proof every gate is clear — two
    reachable counterexamples: (a) an entry a conflict-fix rebase just
    returned to `PENDING` (`coord.merge_queue.reconcile_conflict_entries`)
    whose CI is now re-running on the new head, routinely past this
    cause's own staleness window; (b) an entry deferred behind another
    entry's #3886 per-repo merge-gate reservation, whose `last_attempt`
    predates the reservation and never refreshes while it holds. Both are
    healthy rows waiting on something ordinary, not a stuck merge tick.

    `coord.merge_queue.entry_gate_status` already evaluates review, smoke,
    CI, the epic-closing-keyword gate AND the #3886 reservation, in the
    SAME order `process()` would — one re-derivation covers both
    counterexamples above, the same "ask the live gate, don't guess"
    posture `coord.board_doctor_policy._sealed_confined_lines` already
    takes for a different cause. Fail-closed throughout: no matching
    merge-queue entry, no CI store configured, a `gh` read that raises, or
    a gate that reads anything other than `coord.merge_queue.PLAN_READY`
    all drop the row from this sweep rather than mint a cause this sweep
    cannot actually back up — a missed, truly stuck row simply waits for
    the next sweep, while a false "stuck" label would otherwise burn a
    convergence-ladder slot (and eventually a human page) for a row that
    was never wrong.
    """
    from coord import github_ops  # noqa: PLC0415
    from coord.ci_store import build_ci_store  # noqa: PLC0415
    from coord.merge_queue import PLAN_READY, entry_gate_status  # noqa: PLC0415

    entries_by_key = {
        f"{entry.repo_name}#{entry.issue_number}": entry for entry in merge_entries
    }
    confirmed: list[StuckRow] = []
    for row in rows:
        if row.queue != "merge-queue" or row.cause != CAUSE_MERGE_PENDING_STUCK:
            confirmed.append(row)
            continue
        entry = entries_by_key.get(row.key)
        live_ready = False
        if entry is not None:
            try:
                ci_cfg = config.ci_store
                ci_store = build_ci_store(
                    ci_cfg.type or "github", host=ci_cfg.host, token_env=ci_cfg.token_env,
                )
                status, _reason = entry_gate_status(
                    entry, board, config, ci_store, github_ops,
                )
                live_ready = status == PLAN_READY
            except Exception:  # noqa: BLE001 — best-effort live confirm; see docstring
                _log.debug(
                    "board-doctor: merge-pending-stuck live confirm failed for %s",
                    row.key, exc_info=True,
                )
                live_ready = False
        if live_ready:
            confirmed.append(row)
        # else: not confirmed stuck THIS sweep — drop it rather than act
        # (or escalate) on an unconfirmed reading.
    return confirmed


#: Synthetic issue number for the one-row-per-repo :func:`baseline_red_rows`
#: key — never a real GitHub issue number (those start at 1), so it can
#: never collide with a genuine per-issue row for the same repo in
#: `coord.state`'s escalation/convergence tables.
_BASELINE_RED_ROW_ISSUE = 0


def baseline_red_rows(blocked_streaks: Mapping[str, int]) -> list[StuckRow]:
    """One :class:`StuckRow` per repo whose base is confirmed red (#3386).

    *blocked_streaks* is ``{repo: streak}`` for every repo already past
    `coord.state.BASELINE_RED_STREAK_LIMIT` consecutive confirmed-red
    classifications (`coord.state.baseline_red_merge_blocked`) — callers
    build it by filtering `coord.state.baseline_red_streaks()` through
    that predicate, never by re-deriving the threshold here (#2096: one
    question, one answer). Collapses to exactly one row per repo — key
    ``f"{repo}#{_BASELINE_RED_ROW_ISSUE}"`` — regardless of how many real
    rows that repo has in either queue, so the escalation ladder files ONE
    alert for the repo, not one per stuck PR: the rows underneath are not
    individually wrong, the base is.
    """
    return [
        StuckRow(
            queue="baseline-red",
            key=f"{repo_name}#{_BASELINE_RED_ROW_ISSUE}",
            state="blocked",
            cause=CAUSE_BASELINE_RED,
            confidence=queue_diagnose.CONFIDENCE_HIGH,
            evidence=(f"baseline_red_streak={streak}",),
            fresh=True,
        )
        for repo_name, streak in blocked_streaks.items()
    ]


#: Assignment types `coord.state.reset_work_test_state`/`test_reset_candidates`
#: can clear with no `assignment_id` (`coord.state._test_reset_where`). A
#: `test-author`/`mock-author` row parked on the same mute-leg tally needs
#: its own `assignment_id` threaded through to be clearable at all — this
#: detector has no row to thread (`StuckRow` carries only `queue`/`key`, not
#: `assignment_id`), so it is restricted to the types the handler can
#: actually reach rather than minting a `StuckRow` `sweep_smoke_requeue`
#: would see as "no live row" every sweep forever (`auto-dispatch-noop`
#: with no path to the alert tier, since the row never stops matching this
#: detector's predicate to let `clear_absent_rows` give up on it).
_SMOKE_REQUEUE_REACHABLE_TYPES = frozenset({"work", "plan", "epic-decompose"})


def smoke_no_verdict_rows(board: Board) -> list[StuckRow]:
    """One :class:`StuckRow` per *issue* with an assignment parked on a
    no-verdict smoke leg.

    #3760 Action 5 / #2244: `coord.notify._record_smoke_verdict` parks an
    assignment at ``test_state=TEST_STATE_BLOCKED`` with a
    `coord.smoke.mute_smoke_tally`-prefixed `test_reason` once
    `coord.smoke.mute_smoke_legs` reaches `coord.smoke.MUTE_SMOKE_LEG_BUDGET`
    consecutive Test-stage legs that ended without printing a verdict marker
    (not a genuine suite result). That predicate is read directly off the
    live `test_state`/`test_reason` the board already carries — never
    re-derived — and is exactly what naturally excludes the two sibling
    retry-budget parks (#3315's environmental-retry, #3737's
    abandoned-background): each writes its own distinct tally marker text
    that `coord.smoke.mute_smoke_legs` does not match, so neither is ever
    picked up here.

    Restricted to :data:`_SMOKE_REQUEUE_REACHABLE_TYPES` — a parked
    ``test-author``/``mock-author`` row is invisible to
    `coord.state.reset_work_test_state` without its own `assignment_id`,
    which this detector has no row to carry, so including it here would
    only mint a `StuckRow` the handler can never actually clear.

    Deduped by issue (``f"{repo_name}#{issue_number}"``): two parked
    assignments on the same issue produce exactly one row, since
    `coord.board_doctor_permissions.sweep_smoke_requeue` clears every
    matching row for the issue in a single call and a second, duplicate
    `StuckRow` for the same ``(queue, key)`` would only double-count the
    sweep's own convergence/decision bookkeeping for one real episode.

    *board* is a single already-built `coord.models.Board` (callers pass
    the one `collect_rows` already builds for `queue_diagnose.GhLiveProbe`,
    rather than this function calling `coord.state.build_board` a second
    time). Walks ``board.active + board.completed`` since a parked
    assignment may have already been marked done in some callers' boards.
    """
    from coord.smoke import (  # noqa: PLC0415
        MUTE_SMOKE_LEG_BUDGET,
        TEST_STATE_BLOCKED,
        mute_smoke_legs,
    )

    seen: dict[str, StuckRow] = {}
    for assignment in (*board.active, *board.completed):
        if assignment.type not in _SMOKE_REQUEUE_REACHABLE_TYPES:
            continue
        if assignment.test_state != TEST_STATE_BLOCKED:
            continue
        legs = mute_smoke_legs(assignment.test_reason)
        if legs < MUTE_SMOKE_LEG_BUDGET:
            continue
        key = f"{assignment.repo_name}#{assignment.issue_number}"
        if key in seen:
            continue
        seen[key] = StuckRow(
            queue="smoke-no-verdict",
            key=key,
            state="blocked",
            cause=CAUSE_SMOKE_NO_VERDICT,
            confidence=queue_diagnose.CONFIDENCE_HIGH,
            evidence=(f"mute_smoke_legs={legs}",),
            fresh=True,
        )
    return list(seen.values())


def drive_queue_rows(diagnoses: Sequence[queue_diagnose.Diagnosis]) -> list[StuckRow]:
    """Adapt :func:`coord.queue_diagnose.run_pass` output to :class:`StuckRow`.

    ``fresh=True``: every *diagnoses* entry came from `run_pass` probing the
    entry live THIS sweep (#3777) — never a stored verdict from a prior one.
    """
    return [
        StuckRow(
            queue="drive-queue",
            key=d.key,
            state=d.state,
            cause=d.cause,
            confidence=d.confidence,
            evidence=d.evidence,
            fresh=True,
        )
        for d in diagnoses
    ]


def _episode_stuck_row(episode: dict) -> StuckRow | None:
    """Reconstruct a :class:`StuckRow` from an already-diagnosed episode (#3758).

    :func:`coord.queue_diagnose.run_pass` only re-probes episodes
    :func:`coord.queue_diagnose.needs_diagnosis` says still need it —
    deliberately NOT every open episode, since re-probing a cause Phase 1
    already concluded would waste a `gh` round-trip to re-learn the same
    answer. That filtering is correct for Phase 1's own job (don't
    re-diagnose), but it must not make an already-concluded episode
    invisible to THIS sweep, whose job is exactly the opposite: act on
    concluded causes. So for episodes :func:`collect_rows` does not hand to
    `run_pass`, this rebuilds the same :class:`StuckRow` shape directly
    from the episode's own stored diagnosis fields — no re-probe needed.

    Returns ``None`` for an episode with no concluded, actionable cause
    yet (empty or still :data:`coord.queue_diagnose.CAUSE_UNKNOWN`) — same
    as today's effective behavior for those, just reached without a retry.

    Leaves :attr:`StuckRow.fresh` at its default ``False`` (#3777): the
    cause/confidence here are whatever `block_log` persisted from the
    diagnosis event that concluded the episode, which can be arbitrarily
    old — this never re-confirms them, so nothing downstream may treat
    this row as this-sweep live evidence.
    """
    cause = episode.get("diagnosed_cause") or ""
    if not cause or cause == queue_diagnose.CAUSE_UNKNOWN:
        return None
    key = episode.get("key")
    if not key:
        return None
    return StuckRow(
        queue="drive-queue",
        key=str(key),
        state=str(episode.get("state") or ""),
        cause=cause,
        confidence=str(episode.get("diagnosis_confidence") or queue_diagnose.CONFIDENCE_LOW),
        evidence=tuple(episode.get("diagnosis_evidence") or ()),
    )


def _refresh_ceiling_blocked_settled_rows(
    settled_episodes: Sequence[dict],
    *,
    entries_by_key: Mapping[str, Any],
    probe: queue_diagnose.LiveProbe,
    trigger: str,
) -> tuple[list[StuckRow], list[dict]]:
    """Give a #2972-ceiling-blocked settled episode one more live look (#3777).

    :func:`_episode_stuck_row` is right that a settled episode's stored
    cause must stay visible to this sweep — but for the subset of settled
    episodes whose drive-queue row has already exhausted the fix-round
    ceiling, that stored cause can also go stale forever: once
    :func:`coord.queue_diagnose.needs_diagnosis` decides an episode is
    concluded, nothing ever re-probes it, even after the underlying PR
    later merges. Widening what :func:`coord.queue_diagnose.run_pass` is
    handed cannot fix that — it re-applies `needs_diagnosis` internally to
    every candidate regardless of which list the caller passes, so a
    settled episode never reaches its probe through that path no matter
    what. This calls the same two primitives `run_pass` calls internally
    (`probe.probe` then :func:`coord.queue_diagnose.diagnose`) directly,
    bypassing `needs_diagnosis` for just this narrow, already-permanently-
    blocked subset. That bypass does spend one of this sweep's live `gh`
    probes outside `needs_diagnosis`'s own call-rate budget for this
    episode — but it cannot corrupt `block_log` itself: neither `run_pass`
    nor this module ever writes a diagnosis back to it (both only read it
    and build ephemeral rows for this sweep).

    Returns the freshly diagnosed rows (:attr:`StuckRow.fresh` set, since
    each reflects a probe this sweep just took), plus whichever episodes
    this left untouched (not ceiling-blocked, unparseable key, no matching
    drive-queue entry, a failed probe, or a refresh that still can't name
    an actionable cause) for :func:`_episode_stuck_row` to keep handling
    exactly as before.
    """
    from coord.drive_queue import (  # noqa: PLC0415
        fix_round_ceiling_blocked_for_issue,
        parse_key as _parse_row_key,
    )

    refreshed: list[StuckRow] = []
    remaining: list[dict] = []
    for episode in settled_episodes:
        key = str(episode.get("key") or "")
        if not key:
            remaining.append(episode)
            continue
        parsed = _parse_row_key(key)
        entry = entries_by_key.get(key)
        if (
            entry is None
            or parsed is None
            or not fix_round_ceiling_blocked_for_issue(*parsed)
        ):
            remaining.append(episode)
            continue
        try:
            live = probe.probe(entry)
        except Exception:  # noqa: BLE001 — a refresh must never crash the sweep
            _log.debug("board-doctor: ceiling-refresh probe failed for %s", key, exc_info=True)
            remaining.append(episode)
            continue
        diagnosis = queue_diagnose.diagnose(entry, live, trigger=trigger)
        if diagnosis.cause == queue_diagnose.CAUSE_UNKNOWN:
            # A partial/inconclusive probe must not downgrade a previously
            # settled, actionable stored cause (e.g. CAUSE_PR_MERGED) to
            # unknown — keep the episode as `_episode_stuck_row` would have
            # reconstructed it from `block_log`.
            remaining.append(episode)
            continue
        refreshed.append(
            StuckRow(
                queue="drive-queue",
                key=diagnosis.key,
                state=diagnosis.state,
                cause=diagnosis.cause,
                confidence=diagnosis.confidence,
                evidence=diagnosis.evidence,
                fresh=True,
            )
        )
    return refreshed, remaining


def _reclassify_ceiling_blocked(rows: list[StuckRow]) -> list[StuckRow]:
    """Relabel #2972-ceiling-blocked-but-live-clear rows (#3760 Action 1).

    Runs strictly AFTER :func:`queue_diagnose.diagnose` has already picked
    a cause from live evidence alone — this never feeds the entry's stated
    reason into that classifier (#2235's invariant); it only reads the
    drive-queue row's OWN stored state/reason afterward, through the one
    existing predicate every other caller already uses
    (:func:`coord.drive_queue.fix_round_ceiling_blocked_for_issue`), to ask
    whether a cause this sweep would otherwise leave at the alert tier is
    actually a ceiling a fresh budget would clear.

    Also requires :data:`coord.queue_diagnose.CONFIDENCE_LOW` be absent —
    a low-confidence verdict means the live probe that produced it was
    itself incomplete (``live.probe_errors`` downgraded it), so "the live
    reads are clear" has not actually been established for that row; this
    reclassification, and the auto-dispatch it feeds, must not fire on an
    unconfirmed reading.

    Also requires :attr:`StuckRow.fresh` (#3806, follow-up to #3760): a row
    :func:`_episode_stuck_row` rebuilt from `block_log`'s *stored*
    ``diagnosed_cause``/``diagnosis_confidence`` can be arbitrarily old —
    ``queue_diagnose.needs_diagnosis`` never re-probes a settled episode, so
    a ``nothing-blocking`` verdict earned once would otherwise replay into
    this reclassifier, and the auto-dispatch it feeds, indefinitely, even
    after the PR has since gone red or conflicted. Only a row THIS sweep
    just computed from a live probe (:func:`drive_queue_rows`,
    :func:`_refresh_ceiling_blocked_settled_rows`) is new-enough evidence to
    grant a fresh #2972 fix-round budget — the same age bound
    `_fixit_rebudget_environmental` (`coord/board_doctor_policy.py`)
    already applies to its own environmental-vs-flailing decision (#3847),
    so the two stay consistent (#2096: one question — "is this evidence
    fresh enough to act on?" — one answer, `StuckRow.fresh`).
    """
    from coord.drive_queue import (  # noqa: PLC0415
        fix_round_ceiling_blocked_for_issue,
        parse_key as _parse_row_key,
    )

    reclassified: list[StuckRow] = []
    for row in rows:
        if (
            row.queue == "drive-queue"
            and row.fresh
            and row.cause in _LIVE_CLEAR_CAUSES
            and row.confidence != queue_diagnose.CONFIDENCE_LOW
        ):
            parsed = _parse_row_key(row.key)
            if parsed is not None and fix_round_ceiling_blocked_for_issue(*parsed):
                row = replace(
                    row,
                    cause=CAUSE_CEILING_BLOCKED_MERGEABLE,
                    evidence=row.evidence
                    + (
                        "drive_queue: blocked on the #2972 fix-round ceiling "
                        "while live reads clear",
                    ),
                )
        reclassified.append(row)
    return reclassified


def _reclassify_checks_stale_race(rows: list[StuckRow], *, config) -> list[StuckRow]:
    """Relabel checks-stale-race-but-live-clear rows (#3859).

    Runs strictly AFTER :func:`queue_diagnose.diagnose` has already picked a
    cause from live evidence alone, exactly like :func:`_reclassify_ceiling_
    blocked` — this never feeds the entry's own stated reason into that
    classifier (#2235's invariant); it only reads the drive-queue row's
    OWN stored state/reason afterward, through
    :func:`coord.drive_queue.checks_stale_race_giveup_for_issue`, to ask
    whether a row this sweep would otherwise leave classified
    `CAUSE_NOTHING_BLOCKING` (:data:`_LIVE_CLEAR_CAUSES` — the same set
    :func:`_reclassify_ceiling_blocked` reads, a row whose live gate reads
    clear) actually lost its last life to the quadraui#1961 sibling-merge
    race rather than a real problem.

    Same freshness/confidence bar as :func:`_reclassify_ceiling_blocked`
    (:attr:`StuckRow.fresh`, confidence above
    :data:`coord.queue_diagnose.CONFIDENCE_LOW`) and the same reason: a row
    :func:`_episode_stuck_row` rebuilt from a stale stored verdict, or a
    probe `queue_diagnose` itself flagged as incomplete, is not fresh enough
    evidence to reclassify — the review-approved/Test-passed/mergeable
    reading this reclassifier promotes on must be confirmed THIS sweep, not
    inherited from whenever the episode last concluded.

    Gated per-repo by :func:`coord.board_doctor_policy._stale_race_tier_
    enabled` — the same fleet + per-repo opt-in
    `coord.commands.drive_queue._run_auto_revalidate_checks_stale` checks
    before reserving a merge-gate slot. A repo that has not opted in keeps
    its pre-#3859 label (whatever `queue_diagnose` assigned) unchanged; this
    label is purely informational today (`policy_tier_of` has no entry for
    `CAUSE_CHECKS_STALE_RACE`, so it resolves to the same alert tier either
    way), but it must not claim a race-driven disposition is in play on a
    repo where that disposition cannot actually fire.
    """
    from coord.board_doctor_policy import _stale_race_tier_enabled  # noqa: PLC0415
    from coord.drive_queue import (  # noqa: PLC0415
        checks_stale_race_giveup_for_issue,
        parse_key as _parse_row_key,
    )

    reclassified: list[StuckRow] = []
    for row in rows:
        if (
            row.queue == "drive-queue"
            and row.fresh
            and row.cause in _LIVE_CLEAR_CAUSES
            and row.confidence != queue_diagnose.CONFIDENCE_LOW
        ):
            parsed = _parse_row_key(row.key)
            if (
                parsed is not None
                and _stale_race_tier_enabled(parsed[0], config)
                and checks_stale_race_giveup_for_issue(*parsed)
            ):
                row = replace(
                    row,
                    cause=CAUSE_CHECKS_STALE_RACE,
                    evidence=row.evidence
                    + (
                        "drive_queue: the owning drive session exhausted its "
                        "merge-attempt budget losing a race against sibling "
                        "merges while live reads clear (#3859)",
                    ),
                )
        reclassified.append(row)
    return reclassified


def _reclassify_stale_latch(
    rows: list[StuckRow], merge_entries: Sequence[Any], *, board: Board, config
) -> list[StuckRow]:
    """Relabel a stale `HUMAN_REQUIRED` merge-queue latch whose approval is
    already confirmed bound to the branch's current content (#3887).

    Runs strictly AFTER :func:`merge_queue_rows` has already picked
    ``CAUSE_GATE_BLOCKED`` from the entry's own live ``state``/``error``
    alone, the same "classify first, reclassify a narrow confirmed subset
    second" shape :func:`_reclassify_ceiling_blocked` uses. The extra,
    independent confirmation here is `coord.merge_queue.has_approved_review`
    — quadraui#2034's shape: a latch is only WORTH an auto-``clear_latch``
    attempt once the approval on file is already known to cover the live
    branch content; a latch whose approval is stale for an unrelated
    reason gets no promotion and stays at the alert tier, same as it
    always has.

    Deliberately excludes ``CAUSE_MERGE_CONFLICT`` (#3887): that
    cause means ``QueuedMerge.state == "conflict"`` — GitHub's own
    ``mergeable is False`` reading, live evidence already in hand — and
    `has_approved_review` alone (approval bound to content) says nothing
    about whether that specific conflict has since cleared. Promoting it
    anyway would relabel a genuinely still-conflicting row to this
    mechanical cause, which routes to `clear_latch`'s own live-mergeability
    re-test (a confirmed no-op for a real conflict) instead of the #3769
    investigate lane `queue_diagnose.CAUSE_MERGE_CONFLICT` would otherwise
    reach — losing the conflict-fix dispatch that cause exists to trigger,
    in favor of three silent no-ops and a bare escalation. A genuine
    ``HUMAN_REQUIRED`` latch has no such live counter-evidence to override:
    it is a cached decision from whenever the retry cap or a permission
    gate fired, which `has_approved_review` plus `clear_latch`'s own
    `coord.merge_queue.reconcile_conflict_entries` re-test are exactly
    positioned to re-open.

    This promotion is deliberately NOT "the latch will clear" — the third,
    live-mergeability gate inside `coord.board_doctor_permissions.
    sweep_clear_latch` (via `coord.merge_queue.reconcile_conflict_entries`)
    is what actually decides that, and no-ops harmlessly when it still
    refuses. Promoting on the approval check alone, rather than
    pre-computing full eligibility here too, keeps the live-mergeability
    re-test as the ONE place that question is answered (#2096).
    """
    from coord.merge_queue import has_approved_review  # noqa: PLC0415

    entries_by_key = {
        f"{entry.repo_name}#{entry.issue_number}": entry for entry in merge_entries
    }
    reclassified: list[StuckRow] = []
    for row in rows:
        if (
            row.queue == "merge-queue"
            and row.fresh
            and row.cause == queue_diagnose.CAUSE_GATE_BLOCKED
        ):
            entry = entries_by_key.get(row.key)
            if entry is not None and has_approved_review(entry, board, config=config):
                row = replace(
                    row,
                    cause=CAUSE_LATCH_STALE_MERGEABLE,
                    evidence=row.evidence
                    + (
                        "merge_queue: approval is bound to the current branch "
                        "content — worth re-testing live mergeability (#3887)",
                    ),
                )
        reclassified.append(row)
    return reclassified


def after_prereqs_landed_for_entry(
    entry: Any,
    entries_by_key: Mapping[str, Any],
    board: Board,
    board_view: Any = None,
) -> bool:
    """Does *entry* match #3901's "frozen block cause no longer holds"
    shape — the ONE predicate both :func:`_reclassify_after_prereqs_landed`
    (classification) and `coord.board_doctor_policy`'s pre-dispatch live
    re-check call, rather than two independently-maintained copies of the
    same question (#2096).

    *board* is the `coord.models.Board` `collect_rows` already built this
    sweep (`coord.state.build_board`) — the one thing `coord.gates.
    assignments_for_issue` reads. *board_view* is `coord.drive_queue.
    BoardView` — the SAME typed wrapper `_resolve_prereqs`/`diagnose_
    blocked_after` call `.facts(key)` on — kept a SEPARATE, optional
    parameter rather than derived from *board* here: building one is its
    own `GET /board` round trip this predicate must not silently repeat
    per row, so a caller that could not build one (an advisory failure,
    same posture :func:`collect_rows`'s own no-progress pass already takes)
    passes ``None`` and this predicate degrades to trusting only
    *entries_by_key*'s `coord.drive_queue.STATE_DONE_LIKE` reading for a
    dep still queued — never a dep that left the queue after landing,
    exactly the gap the next bullet's second check exists to close when a
    `board_view` IS on hand.

    True only when ALL of:

    * *entry*'s state is `blocked`/`parked` (`coord.drive_queue.
      STATE_BLOCKED`/`STATE_PARKED`) — a row an operator already resumed
      (back to `waiting`) no longer matches, which is exactly what makes
      this callable safely as the live re-check too;
    * *entry* declares a non-empty `after=` and is NOT a permanent block
      (`coord.drive_queue.is_permanent_block_reason`);
    * `entry.last_reason` is still exactly `coord.drive_queue._resolve_
      prereqs`'s own unsatisfiable-`after=` verdict text
      (`coord.drive_queue.is_unsatisfiable_prereq_reason`) — the recorded
      block cause names ONLY its `after=` graph, never an unrelated reason
      this predicate has no business overriding;
    * every named pre-req now reads terminal — its OWN drive-queue row (if
      still queued, per *entries_by_key*) is in `coord.drive_queue.
      STATE_DONE_LIKE`, or (when *board_view* is given) `board_view.
      facts(dep).landed` is `True` — the SAME two positive-evidence checks
      `_resolve_prereqs` itself treats as "satisfied; continue" (#2096
      again: not a second derivation of what "landed" means);
    * the issue was actually DISPATCHED at least once
      (`coord.gates.assignments_for_issue` — the same existing primitive
      :func:`coord.board_doctor_policy._never_dispatched_still_true`
      already answers this question with) — the #3874
      `CAUSE_NEVER_DISPATCHED` -> `drive_queue_requeue` wiring already
      covers the NEVER-dispatched shape (`coord.drive_queue.
      is_unreachable_wait_shape`, which unconditionally excludes this
      entry's own `after=` reason); this predicate is deliberately the
      complement of that one, not a second path to the same place.
    """
    from coord.drive_queue import (  # noqa: PLC0415
        STATE_BLOCKED,
        STATE_DONE_LIKE,
        STATE_PARKED,
        is_permanent_block_reason,
        is_unsatisfiable_prereq_reason,
        parse_key as _parse_row_key,
    )
    from coord.gates import assignments_for_issue  # noqa: PLC0415

    if entry.state not in (STATE_BLOCKED, STATE_PARKED):
        return False
    if not entry.after or is_permanent_block_reason(entry.last_reason):
        return False
    if not is_unsatisfiable_prereq_reason(entry.last_reason):
        return False

    def _dep_landed(dep: str) -> bool:
        dep_entry = entries_by_key.get(dep)
        if dep_entry is not None and dep_entry.state in STATE_DONE_LIKE:
            return True
        if board_view is None:
            return False
        return board_view.facts(dep).landed

    if not all(_dep_landed(dep) for dep in entry.after):
        return False

    parsed = _parse_row_key(entry.key)
    return parsed is not None and bool(assignments_for_issue(board, *parsed))


def _reclassify_after_prereqs_landed(
    rows: list[StuckRow],
    entries_by_key: Mapping[str, Any],
    *,
    board: Board,
    board_view: Any = None,
) -> list[StuckRow]:
    """Relabel a frozen `blocked`/`parked` drive-queue row whose `after=`
    block cause no longer holds (#3901, #3887 case 4).

    Runs strictly AFTER :func:`queue_diagnose.diagnose`/:func:`no_progress_
    rows` have already picked a cause from live evidence alone, same
    "classify first, reclassify a narrow confirmed subset second" shape
    every other reclassifier in this module uses. Promotes a row to
    :data:`CAUSE_AFTER_PREREQS_LANDED` only for a row this sweep classified
    fresh (:attr:`StuckRow.fresh`) — never a stale episode reconstruction
    (:func:`_episode_stuck_row`), whose `entry.last_reason` could be
    arbitrarily old — whose incoming cause is one of
    :data:`_AFTER_PREREQS_LANDED_SAFE_CAUSES` (#3901 review: the three
    sibling reclassifiers in this module all require a specific incoming
    cause — `_reclassify_ceiling_blocked`/`_reclassify_checks_stale_race`
    require `_LIVE_CLEAR_CAUSES` plus confidence above
    :data:`queue_diagnose.CONFIDENCE_LOW`, `_reclassify_stale_latch`
    requires exactly `queue_diagnose.CAUSE_GATE_BLOCKED` — this is the same
    shape, never "promote regardless of what the live diagnosis already
    found"), and whose live drive-queue entry (*entries_by_key*, loaded
    fresh by :func:`collect_rows` this same sweep) satisfies
    :func:`after_prereqs_landed_for_entry`.

    `coord.drive_queue._reconcile_blocked_after` resumes a LIVE `blocked`
    row matching this exact shape to `waiting` in place on every ordinary
    tick, no board-doctor involvement at all — this reclassifier (and the
    `drive_queue_requeue` dispatch it feeds, same mechanical action
    :data:`CAUSE_NEVER_DISPATCHED`/:data:`CAUSE_CEILING_BLOCKED_MERGEABLE`
    already use) only ever matters for a row the ordinary tick's own walk
    is not still reaching — e.g. one #3888's `no_progress_rows` had to
    rescue from a budget-exhausted, silently-dropped episode. Never a
    competing second implementation of `_reconcile_blocked_after`'s own
    resume — this reclassifies, the SAME mechanical `drive_queue_requeue`
    action every other row on this allowlist dispatches, re-confirmed live
    immediately before it fires (see `coord.board_doctor_policy.
    _requeue_after_prereqs_landed`).
    """
    reclassified: list[StuckRow] = []
    for row in rows:
        if (
            row.queue == "drive-queue"
            and row.fresh
            and row.cause in _AFTER_PREREQS_LANDED_SAFE_CAUSES
        ):
            entry = entries_by_key.get(row.key)
            if entry is not None and after_prereqs_landed_for_entry(
                entry, entries_by_key, board, board_view
            ):
                row = replace(
                    row,
                    cause=CAUSE_AFTER_PREREQS_LANDED,
                    evidence=row.evidence
                    + (
                        f"drive_queue: every --after prereq "
                        f"({', '.join(entry.after)}) now reads terminal while "
                        "the stored block reason still names only them "
                        "(#3901)",
                    ),
                )
        reclassified.append(row)
    return reclassified


#: #3929 (#3918 gap 1, second half): a merge-queue entry that has NEVER been
#: attempted at all (``entry.last_attempt is None``) is invisible to both
#: :func:`merge_queue_rows` (its own #3918-review carve-out: a `pending`
#: entry is only ever labelled stuck there via LAST-ATTEMPT staleness,
#: deliberately never off an unset `last_attempt` — see that function's
#: docstring for why) and :data:`_MERGE_QUEUE_STUCK_CAUSE` (same reason).
#: quadraui#2160: a row whose test and review both read fresh and whose
#: merge-queue row sits `PENDING` with no attempt recorded escalated or
#: idled toward the 240-minute deadline instead of this sweep just running
#: the one merge attempt that unblocks it. Minted ONLY by
#: :func:`_reclassify_merge_never_attempted`, from a row that reclassifier
#: independently re-confirms LIVE via `coord.merge_queue.entry_gate_status`
#: — the SAME re-confirmation :func:`_confirm_merge_pending_stuck` already
#: trusts for the STALE-pending shape — never from the bare PENDING/
#: unset-`last_attempt` shape alone.
CAUSE_MERGE_NEVER_ATTEMPTED = "merge-never-attempted"

#: The incoming `queue_diagnose` causes :func:`_reclassify_merge_never_
#: attempted` may promote away from. Starts from
#: :data:`_AFTER_PREREQS_LANDED_SAFE_CAUSES` (`unknown`/`no-pr`: the live
#: PR/branch-lifecycle diagnosis found nothing actionable of its own) and
#: widens it with `queue_diagnose.CAUSE_NOTHING_BLOCKING` — quadraui#2160's
#: own row has an open PR with every check green, review approved and
#: `mergeable` true, which `queue_diagnose._rule_nothing_blocking` reads as
#: `nothing-blocking`, not `unknown`/`no-pr`. A SEPARATE constant from
#: `_AFTER_PREREQS_LANDED_SAFE_CAUSES` rather than widening that one in
#: place: `_reclassify_after_prereqs_landed` promotes to a `drive_queue_
#: requeue`, a completely different action with its own, already-settled
#: #3901 scope — this cause's own live `entry_gate_status` re-check below
#: is the independent confirmation that makes `CAUSE_NOTHING_BLOCKING`
#: safe to add HERE without touching that one.
_MERGE_NEVER_ATTEMPTED_SAFE_CAUSES = _AFTER_PREREQS_LANDED_SAFE_CAUSES | frozenset(
    {queue_diagnose.CAUSE_NOTHING_BLOCKING}
)


def _reclassify_merge_never_attempted(
    rows: list[StuckRow],
    merge_entries: Sequence[Any],
    *,
    board: Board,
    config,
    now: float | None = None,
) -> list[StuckRow]:
    """#3929: promote a no-progress drive-queue row to
    :data:`CAUSE_MERGE_NEVER_ATTEMPTED` when its merge-queue row is
    `PENDING` with ``last_attempt is None`` (never evaluated at all), has
    sat at least :data:`MERGE_PENDING_STALE_AFTER_SECONDS` since
    ``enqueued_at``, AND a live re-check confirms every gate ahead of the
    merge attempt itself already reads clear.

    The ``enqueued_at`` age bound mirrors :func:`merge_queue_rows`'s own
    staleness filter for the STALE-pending shape (same threshold, same
    reasoning): a `PENDING` entry with no attempt recorded yet that was
    JUST enqueued is, per that function's own docstring, "very likely
    still alive and polling" under the ordinary auto-drain tick — minting
    this cause (and the `coord merge --only` it dispatches) off a
    freshly-enqueued entry would race that tick rather than rescue a row
    it has genuinely stopped reaching. An entry with no ``enqueued_at`` at
    all (pre-#274 rows, hand-edited queue state) is treated as already
    past the bound — the same fail-open convention every other optional
    ``QueuedMerge`` anchor in this module uses — rather than permanently
    exempted from this cause.

    Same "promote only an independently-confirmed subset of a broader
    cause" shape as :func:`_reclassify_after_prereqs_landed` — the incoming
    cause must be one of :data:`_MERGE_NEVER_ATTEMPTED_SAFE_CAUSES`
    (`unknown`/`no-pr`: the live PR/branch-lifecycle diagnosis found
    nothing actionable of its own, which is exactly what a row stuck ONLY
    because `coord merge` never got to it looks like; `nothing-blocking`:
    the live diagnosis found an open PR with every check green, review
    approved and `mergeable` true — quadraui#2160's own shape — #2096, the
    same incoming-cause precondition, not a second independently-maintained
    judgment call under a new name), and :func:`coord.merge_queue.entry_gate_status`
    — the SAME function :func:`_confirm_merge_pending_stuck` already trusts
    for the STALE-pending shape — must read `PLAN_READY` right now. A row
    whose review/smoke genuinely is not ready yet reads `PLAN_BLOCKED` here
    and is left exactly as the caller already classified it, so a row that
    is not actually ready for a merge attempt is never mis-labelled as one
    that is.

    Also requires :attr:`StuckRow.fresh` — the SAME guard `_reclassify_
    after_prereqs_landed`/`_reclassify_ceiling_blocked`/`_reclassify_checks_
    stale_race` all apply, and for the same reason: a row rebuilt by
    `_episode_stuck_row` from an arbitrarily old stored `cause=unknown`
    carries no live evidence of its own, so promoting IT off a merge-queue
    row that merely happens to still read `PENDING`/`last_attempt is None`
    right now would be reasoning from a snapshot this sweep never actually
    re-diagnosed.

    *now* defaults to :func:`time.time` — overridable so a replay test can
    assert on a fixed, reproducible staleness window, same convention as
    :func:`merge_queue_rows`.
    """
    import time as _time  # noqa: PLC0415

    from coord import github_ops  # noqa: PLC0415
    from coord.ci_store import build_ci_store  # noqa: PLC0415
    from coord.merge_queue import PENDING, PLAN_READY, entry_gate_status  # noqa: PLC0415

    moment = _time.time() if now is None else now
    entries_by_key = {
        f"{entry.repo_name}#{entry.issue_number}": entry for entry in merge_entries
    }
    out: list[StuckRow] = []
    for row in rows:
        if (
            row.queue != "drive-queue"
            or not row.fresh
            or row.cause not in _MERGE_NEVER_ATTEMPTED_SAFE_CAUSES
        ):
            out.append(row)
            continue
        entry = entries_by_key.get(row.key)
        if (
            entry is None
            or entry.state != PENDING
            or entry.last_attempt is not None
            or (
                entry.enqueued_at is not None
                and moment - entry.enqueued_at < MERGE_PENDING_STALE_AFTER_SECONDS
            )
        ):
            out.append(row)
            continue
        live_ready = False
        try:
            ci_cfg = config.ci_store
            ci_store = build_ci_store(
                ci_cfg.type or "github", host=ci_cfg.host, token_env=ci_cfg.token_env,
            )
            status, _reason = entry_gate_status(entry, board, config, ci_store, github_ops)
            live_ready = status == PLAN_READY
        except Exception:  # noqa: BLE001 — best-effort live confirm; see docstring
            _log.debug(
                "board-doctor: merge-never-attempted live confirm failed for %s",
                row.key, exc_info=True,
            )
            live_ready = False
        if live_ready:
            row = replace(
                row,
                cause=CAUSE_MERGE_NEVER_ATTEMPTED,
                confidence=queue_diagnose.CONFIDENCE_HIGH,
                evidence=row.evidence
                + (
                    f"merge_queue.state={entry.state} last_attempt=None — the "
                    "merge gate has never actually been evaluated at all; a "
                    "live re-check confirms every gate ahead of the merge "
                    "attempt itself reads clear (#3929)",
                ),
            )
        out.append(row)
    return out


#: #3888: the three drive-queue shapes that can sit making no progress while
#: carrying neither a `block_log` episode (only `blocked`/`parked`
#: transitions ever open one — :data:`coord.block_log.STALL_STATES`) nor a
#: merge-queue row, so :func:`collect_rows`'s episode/merge-queue walk above
#: never looks at them at all: #3875's "(D) coverage classification for rows
#: with no gate to read". ``_NO_PROGRESS_BLOCKED`` additionally covers the
#: narrower bug in the same ask — a `blocked`/`parked` row DOES open an
#: episode, but :func:`_episode_stuck_row` silently drops one whose stored
#: cause is still :data:`queue_diagnose.CAUSE_UNKNOWN` (budget-exhausted or
#: not yet diagnosed), so it never reaches :func:`sweep` either. These
#: string constants are evidence labels only — never branched on by a
#: caller outside this module.
_NO_PROGRESS_RUNNING_DEAD = "running-no-session-no-activity"
#: #3918: a FOURTH shape — a `running` row whose tmux session is alive (so
#: the dead-leg shape above never matches) but whose own drive loop has
#: published :func:`coord.drive._publish_stall_nudge`'s record of "no state
#: change, nothing active" for longer than this sweep's own
#: ``no_progress_minutes`` threshold (quadraui#2160: the pane printed `!!
#: no state change in 20m … 40m … 161m (nothing active)` for over two
#: hours while the doctor wrote nothing, because a live session alone was
#: treated as proof of progress). Reuses the SAME persisted record
#: `coord.notifier.collect.worker_probes` already reads to answer the
#: identical question for its own `CONDITION_STALL_NUDGED` alert
#: (#2096) — never a second, independently-derived "is this stalled"
#: signal — rather than re-deriving staleness from `coord drive`'s raw
#: pane output here.
_NO_PROGRESS_RUNNING_IDLE = "running-live-no-state-change"
_NO_PROGRESS_BLOCKED = "blocked"
_NO_PROGRESS_WAITING_READY = "waiting-ready-not-launched"


def _resolve_no_progress_concurrency(
    entries: Sequence[Any],
    board_view: Any,
    *,
    config,
    systemd_flags: Mapping[str, int] | None,
    now: float | None,
    local_host: str | None,
):
    """The one capacity verdict the no-progress ``waiting`` shape reads —
    repo-level AND fleet-level ceilings, each with its own current
    occupancy — resolved through :func:`coord.drive_queue.
    resolve_concurrency_report`, the SAME entry point `coord config
    --effective` and the real tick's own ``ConcurrencyReport`` both already
    go through (#3428, #2096 "one question, one answer"), rather than this
    module hand-deriving the resolution order a second time. Replaces the
    former `_repo_running_counts`/`_repo_parallel_ceiling` pair, which only
    ever answered the repo-level half of this question.

    *systemd_flags* is ``{}`` (no override known) when the caller gives
    ``None`` — never this function reading the filesystem itself, so a test
    can call this without touching a real ``~/.config/systemd/user/``.
    :func:`collect_rows` reads :func:`coord.drive_queue.
    read_systemd_max_parallel_flags` (which now also sees any drop-in under
    that unit's own ``.d/`` directory) once per sweep and threads the
    result down, the same posture it already has for *local_host*.
    """
    from coord.drive_queue import resolve_concurrency_report  # noqa: PLC0415

    return resolve_concurrency_report(
        repo_overrides={repo.name: repo.max_parallel for repo in config.repos},
        pipeline_max_parallel=config.pipeline.max_parallel,
        pipeline_max_parallel_per_repo=config.pipeline.max_parallel_per_repo,
        max_workers_cap=config.concurrency.max_workers,
        systemd_flags=systemd_flags or {},
        entries=entries,
        board=board_view,
        now=now,
        local_host=local_host,
    )


def _repo_parallel_ceiling(
    repo_name: str, *, config, systemd_flags: Mapping[str, int]
) -> int:
    """*repo_name*'s effective ``max_parallel_per_repo`` ceiling, with no
    occupancy question attached — a thin single-repo convenience over
    :func:`coord.drive_queue.resolve_concurrency_report` (the same shared
    entry point :func:`_resolve_no_progress_concurrency` uses) for a caller
    that only wants to check one repo's ceiling, e.g. a unit test asserting
    a systemd override wins over `coordinator.yml`. ``0`` means uncapped,
    mirroring that resolution's own meaning. *systemd_flags* is required —
    a caller must say explicitly whether it is asking "with the daemon
    host's real override" or "with none known" rather than the answer
    silently depending on an unstated default.
    """
    from coord.drive_queue import resolve_concurrency_report  # noqa: PLC0415

    report = resolve_concurrency_report(
        repo_overrides={repo.name: repo.max_parallel for repo in config.repos},
        pipeline_max_parallel=config.pipeline.max_parallel,
        pipeline_max_parallel_per_repo=config.pipeline.max_parallel_per_repo,
        max_workers_cap=config.concurrency.max_workers,
        systemd_flags=systemd_flags,
    )
    resolution = report.repo_resolutions.get(repo_name, report.max_parallel_per_repo)
    return resolution.value


def _render_ceiling(resolution: Any) -> str:
    """Render one :class:`coord.drive_queue.CeilingResolution` for a
    no-progress evidence line — the bare number, or ``uncapped`` for the
    ``0`` value both `max_parallel_per_repo` and `max_parallel` document as
    "no limit" (:mod:`coord.config`'s own validation permits ``0`` there).
    Without this, an uncapped repo's evidence read as e.g. "1/0 occupied",
    which looks like a repo already over its limit rather than one with no
    limit at all.
    """
    return "uncapped" if resolution.value == 0 else str(resolution.value)


def _fresh_stall_nudge(
    nudge: Mapping[str, Any] | None,
    *,
    now: float,
    launched_at: float | None,
    ttl_seconds: float,
) -> Mapping[str, Any] | None:
    """*nudge* only if it still plausibly describes the entry's current session.

    A stall-nudge record carries no link back to any particular drive
    session and `load_state` applies no expiry of its own (pruning only
    happens inside `save_state`, which the record's own retraction path
    does not pass through) — so, read in isolation, a record can describe
    a session that already ended. A record is accepted here only if it is
    both recent (within *ttl_seconds*) and dated on or after *launched_at*,
    the start of the session it is being read against; either check
    failing means the record predates (or has outlived) the thing it would
    be used to explain, so it is treated the same as no record at all.
    """
    if not nudge:
        return None
    at = nudge.get("at")
    if at is None:
        return None
    try:
        at = float(at)
    except (TypeError, ValueError):
        return None
    if now - at > ttl_seconds:
        return None
    if launched_at is not None and at < launched_at:
        return None
    return nudge


def _no_progress_shape(
    entry: Any,
    *,
    live_sessions: Collection[str],
    facts_of: Callable[[str], Any],
    repo_ceiling: Callable[[str], int],
    running_counts: Mapping[str, int],
    fleet_ceiling: int,
    fleet_occupied: int,
    dep_states: Mapping[str, str],
    local_host: str | None,
    now: float = 0.0,
    stall_nudge_of: Callable[[str, int], Mapping[str, Any] | None] = lambda repo, issue: None,
) -> str | None:
    """Which of the #3888/#3918 no-progress shapes *entry* matches, or
    ``None``.

    ``running``, dead-leg: launched on *this* host — a foreign-launched row
    (:func:`coord.queue_diagnose.is_foreign_launch`) is never judged dead
    from here, same guard :meth:`coord.queue_diagnose.GhLiveProbe._foreign`
    and :func:`coord.drive_queue._reconcile_running`'s own #1870 branch
    already apply, because an absent LOCAL tmux session proves nothing
    about a session another machine launched — AND no live tmux session
    (*live_sessions*, the same local read :func:`collect_rows` already
    takes) AND no non-terminal work-like assignment on the board
    (``facts_of(entry.key).active_work`` — :attr:`coord.drive_queue.
    IssueFacts.active_work`, the one existing "is there live work in
    flight" answer, never re-derived here) — the quadraui#2038 shape: a row
    the queue still calls ``running`` whose real work already finished (by
    hand or otherwise) with nobody telling the queue.

    ``running``, live-but-idle (#3918): gated on the SAME ``no non-terminal
    work-like assignment on the board`` check the dead-leg shape above
    uses — the issue's own condition names a `running` row with "nothing
    active", and an idle nudge alone must never override a live,
    non-terminal assignment the board still has recorded for this row
    (the ordinary "making real progress" case). Past that guard: the
    session IS alive (so the dead-leg check above never matches), but
    *stall_nudge_of* — a thin wrapper over
    :func:`coord.notifier.store.nudge_for`, the SAME persisted record the
    notifier's own stall-survived-its-nudge condition reads, filtered
    through :func:`_fresh_stall_nudge` so a record from an earlier
    session (predating this row's own ``launched_at``) or one that has
    simply outlived :data:`coord.notifier.store.NUDGE_TTL_SECS` is never
    read as describing the CURRENT session — reports a ``stalled_for``
    duration for this row's ``(repo, issue)``. `coord.drive.Driver._loop`
    publishes that record every time its own ``--stall``-driven nudge
    fires (#1593), and clears it the instant the board fingerprint it
    describes moves — so a record present here means the OWNING drive
    session itself has observed no pipeline state change for that long,
    not a guess this function makes from session liveness alone. A
    session being alive proves the TMUX PROCESS is up; it says nothing
    about whether the pipeline inside it is moving (quadraui#2160: test
    passed, then the pane printed `!! no state change in 20m … 40m … 161m
    (nothing active)` for over two hours with the doctor never writing a
    row for it, because the dead-leg check above — the only `running`
    check that existed — stops at "the session is alive").
    *_no_progress_age_seconds* reads the SAME record (passed down by the
    caller) rather than this function's own ``launched_at``-based age for
    this one shape, since "idle since when" is this record's own
    ``stalled_for``, not how long ago the session happened to launch.

    ``blocked``/``parked``: always a candidate — unlike the other shapes
    this one needs no extra evidence to be WORTH a fresh look, since
    sitting blocked already means nothing is progressing; the N-minute
    grace window (applied by the caller) is what keeps a merely-just-blocked
    row out, not this predicate.

    ``waiting``: every ``after=`` pre-req reads ``landed`` off the SAME
    :attr:`coord.drive_queue.IssueFacts.landed` property `_resolve_prereqs`
    itself consults (#2096 again) — OR, like `_resolve_prereqs` itself
    (#2715), the dep's OWN drive-queue row already reading
    :data:`coord.drive_queue.STATE_DONE_LIKE` in *dep_states*, since the
    ``issues`` cache backing ``landed`` can run minutes behind a merge the
    queue performed itself — AND the repo has room under its own ceiling
    right now, per *running_counts* (the scheduler's own occupancy
    verdict) — AND the FLEET has room too, per *fleet_occupied* against
    *fleet_ceiling*. Both capacity answers come from the same
    ``ConcurrencyReport`` :func:`_resolve_no_progress_concurrency` resolves,
    the one the real tick's own ``plan_tick`` enforces at both levels: a
    `waiting` row at `repo` capacity and one at the FLEET's own
    ``--max-parallel`` ceiling are the identical "queue working as
    intended" shape, just at different ceilings — ``plan_tick``'s own
    global-capacity early return only refreshes a `waiting` row's prereq
    text (never writes a capacity reason into it), so a row deferred there
    is just as easy to misread as `cause=unknown` without this check as the
    repo-level one was. A ``waiting`` row failing any of the three checks
    is correctly explained already (still waiting on a dep, or waiting its
    turn for capacity at either level) — this only flags the quadraui#2047
    shape, a row the scheduler should have launched by now and, for
    whatever reason, did not.
    """
    from coord.drive_queue import (  # noqa: PLC0415
        STATE_BLOCKED,
        STATE_DONE_LIKE,
        STATE_PARKED,
        STATE_RUNNING,
        STATE_WAITING,
    )

    if entry.state == STATE_RUNNING:
        if queue_diagnose.is_foreign_launch(entry, local_host):
            return None
        if facts_of(entry.key).active_work:
            return None
        if entry.session_name and entry.session_name in live_sessions:
            from coord.notifier.store import NUDGE_TTL_SECS  # noqa: PLC0415

            nudge = _fresh_stall_nudge(
                stall_nudge_of(entry.repo, entry.issue),
                now=now,
                launched_at=entry.launched_at,
                ttl_seconds=NUDGE_TTL_SECS,
            )
            if nudge is not None and nudge.get("stalled_for") is not None:
                return _NO_PROGRESS_RUNNING_IDLE
            return None
        return _NO_PROGRESS_RUNNING_DEAD
    if entry.state in (STATE_BLOCKED, STATE_PARKED):
        return _NO_PROGRESS_BLOCKED
    if entry.state == STATE_WAITING:
        if not all(
            facts_of(dep).landed or dep_states.get(dep) in STATE_DONE_LIKE
            for dep in entry.after
        ):
            return None
        ceiling = repo_ceiling(entry.repo)
        if ceiling > 0 and running_counts.get(entry.repo, 0) >= ceiling:
            return None
        if fleet_ceiling > 0 and fleet_occupied >= fleet_ceiling:
            return None
        return _NO_PROGRESS_WAITING_READY
    return None


def _no_progress_age_seconds(
    entry: Any, *, now: float, stall_nudge: Mapping[str, Any] | None = None,
) -> float | None:
    """How long *entry* has sat in its current shape, or ``None`` if unknown.

    ``running``/live-but-idle (#3918): when *stall_nudge* is given (the
    SAME record :func:`_no_progress_shape` consulted to mint
    :data:`_NO_PROGRESS_RUNNING_IDLE` for this entry), its own
    ``stalled_for`` IS the age — "idle since when" is a duration the
    drive's own stall nudge already computed, not a timestamp this
    function would have to subtract from *now* itself. ``launched_at``
    (when THIS session started) would silently understate a long-running,
    long-healthy session that only went idle recently, or overstate one
    that went idle almost immediately — neither is "how long has nothing
    changed", which is the only question this shape asks.

    ``running``, otherwise, ages off ``launched_at``; every other state
    ages off ``reason_at`` (#2133's "when was this text last true" stamp).
    ``None`` — a row predating either column, or one whose field was never
    written — is deliberately never treated as "just now" (the same rule
    :class:`coord.drive_queue.QueueEntry` documents for ``reason_at``
    itself): the caller treats an unknown age as past the grace window
    rather than silently exempting it.

    #3888 review: ``reason_at`` is re-stamped on every ``last_reason``
    write, not only on a real state change (:class:`coord.drive_queue.
    QueueEntry`'s own #2273 comment) — a ``blocked`` row whose per-tick
    re-derived reason text wiggles (a gate listing different check names,
    a shifting backoff suffix) keeps resetting this clock and can stay
    under ``no_progress_minutes`` indefinitely, the exact "moving target"
    defect #2273 already named and fixed for the RETRY backoff window by
    switching that ONE caller onto its own dedicated, write-once stamp
    (``QueueEntry.retry_backoff_at`` — never touched again until the next
    death) instead of ``reason_at``. This pass is accepted to carry the
    same risk for now rather than grow a fourth stamp: giving every
    no-progress shape its own write-once anchor the way #2273 did for
    retry backoff is a follow-up, not required for this issue's three
    shapes to stop being silently invisible.
    """
    from coord.drive_queue import STATE_RUNNING  # noqa: PLC0415

    if (
        entry.state == STATE_RUNNING
        and stall_nudge is not None
        and stall_nudge.get("stalled_for") is not None
    ):
        try:
            return max(0.0, float(stall_nudge["stalled_for"]))
        except (TypeError, ValueError):
            pass
    stamp = entry.launched_at if entry.state == STATE_RUNNING else entry.reason_at
    if stamp is None:
        return None
    return max(0.0, now - stamp)


#: #3888 review nit: unlike :func:`coord.queue_diagnose.needs_diagnosis`'s
#: per-episode budget, this pass has no cap of its own — the first sweep
#: after deploy could otherwise fan out one `gh` probe per legacy
#: `blocked`/`parked` row all at once, the exact synchronized-poll shape
#: #3536 already had to guard against elsewhere in this fleet. A row that
#: matches a shape but loses the race for this budget is still returned
#: (never silently dropped, #3888's whole point) — just as `unknown`, with
#: evidence saying so, so the NEXT sweep (which re-derives this budget from
#: scratch) gets first claim on probing it.
_NO_PROGRESS_PROBE_CAP = 25


def no_progress_rows(
    entries: Sequence[Any],
    *,
    covered_keys: Collection[str],
    live_sessions: Collection[str],
    board_view: Any,
    probe: queue_diagnose.LiveProbe,
    config,
    trigger: str = "board-doctor-sweep",
    now: float | None = None,
    local_host: str | None = None,
    systemd_flags: Mapping[str, int] | None = None,
) -> list[StuckRow]:
    """Classify every drive-queue row #3888 says the episode walk misses.

    :func:`collect_rows`'s episode-based walk only ever looks at keys
    `block_log` opened an episode for — exclusively `blocked`/`parked`
    transitions (:data:`coord.block_log.STALL_STATES`) — plus whatever
    :func:`merge_queue_rows` covers. A `running` row whose work finished out
    of band, one whose drive session is alive but reports no state change
    (#3918), or a `waiting` row the scheduler should have launched, carries
    none of that, so it never reached this sweep's output at all (#3875 ask
    D). This walks the FULL current *entries* instead and, for every one NOT
    already in *covered_keys* (so a row the episode walk already explained
    is never double-classified or double-alerted), asks :func:`_no_progress_
    shape` whether it matches one of the four no-progress shapes and
    :func:`_no_progress_age_seconds` whether it has sat there past
    ``config.board_doctor.no_progress_minutes`` (#2096: one threshold, read
    from one place).

    *board_view* is the same :class:`coord.drive_queue.BoardView`
    :func:`collect_rows` already builds for its own probe — its
    ``.facts(key)`` is this pass's ONLY "is this issue landed / is there
    live work" reading, and it is also handed to
    :func:`_resolve_no_progress_concurrency` to answer "is there a free
    repo slot, and a free fleet slot" with the scheduler's own verdict
    rather than a second count. *local_host* gates the `running` shape the
    same way :func:`coord.queue_diagnose.is_foreign_launch` gates every
    other liveness call in this fleet: a row launched on a different host
    is never judged dead from an absent LOCAL tmux session.

    A matching, aged-out row is diagnosed through the SAME `probe` +
    :func:`queue_diagnose.diagnose` pair every other fresh row in this
    module uses — never a second classifier — so a row that DOES still have
    live PR/CI evidence gets a real cause, and only a genuinely thin picture
    falls through to :data:`queue_diagnose.CAUSE_UNKNOWN`. Bounded by
    :data:`_NO_PROGRESS_PROBE_CAP` probes per call — see its own docstring.

    `unknown` is returned as an ordinary :class:`StuckRow`, never dropped —
    the #3888 fix for :func:`_episode_stuck_row`'s silent abstention-skip:
    `sweep`'s default `tier_of` already routes any cause with no
    :mod:`coord.board_doctor_policy` entry (`unknown` included) to the
    alert tier, so returning the row here is sufficient to make it alert;
    nothing downstream needs to special-case the cause name.

    *systemd_flags* is ``{}`` (no override known) unless the caller
    supplies the real read; :func:`collect_rows` reads the daemon host's
    installed unit (and any drop-in under its own ``.d/`` directory — see
    :func:`coord.drive_queue.read_systemd_max_parallel_flags`) once per
    sweep and passes it down here, so both the repo and the FLEET ceiling
    read the SAME override the real tick would.
    """
    stamp = _utcnow().timestamp() if now is None else now
    threshold_seconds = config.board_doctor.no_progress_minutes * 60
    facts_of = board_view.facts
    dep_states = {entry.key: entry.state for entry in entries}
    # #3918: one notifier-state read for the whole sweep (not one per
    # entry) — `coord.notifier.store.nudge_for` is a pure dict lookup once
    # the state is loaded, same posture `collect_rows`' own single
    # `live_sessions`/`systemd_flags` reads already take. Advisory: a
    # notifier whose state file is unreadable degrades to "no nudges
    # known" (every entry reads as not-idle from this signal) rather than
    # failing the whole no-progress pass — the dead-leg/blocked/waiting
    # shapes above are unaffected either way.
    from coord.notifier.store import NUDGE_TTL_SECS as _NUDGE_TTL_SECS  # noqa: PLC0415

    try:
        from coord.notifier.store import load_state as _load_notifier_state
        from coord.notifier.store import nudge_for as _nudge_for

        _notifier_state = _load_notifier_state()
    except Exception:  # noqa: BLE001 — advisory channel, see comment above
        _log.debug(
            "board-doctor: could not read notifier state for the "
            "running-idle no-progress shape this sweep", exc_info=True,
        )
        _notifier_state = None

    def stall_nudge_of(repo_name: str, issue_number: int) -> Mapping[str, Any] | None:
        if _notifier_state is None:
            return None
        return _nudge_for(_notifier_state, repo_name, issue_number)
    concurrency = _resolve_no_progress_concurrency(
        entries,
        board_view,
        config=config,
        systemd_flags=systemd_flags,
        now=stamp,
        local_host=local_host,
    )
    running_counts = concurrency.repo_occupied
    fleet_ceiling = concurrency.max_parallel.value
    fleet_occupied = concurrency.occupied

    def repo_ceiling(repo_name: str) -> int:
        resolution = concurrency.repo_resolutions.get(
            repo_name, concurrency.max_parallel_per_repo
        )
        return resolution.value

    rows: list[StuckRow] = []
    probes_spent = 0
    for entry in entries:
        if entry.key in covered_keys:
            continue
        shape = _no_progress_shape(
            entry,
            live_sessions=live_sessions,
            facts_of=facts_of,
            repo_ceiling=repo_ceiling,
            running_counts=running_counts,
            fleet_ceiling=fleet_ceiling,
            fleet_occupied=fleet_occupied,
            dep_states=dep_states,
            local_host=local_host,
            now=stamp,
            stall_nudge_of=stall_nudge_of,
        )
        if shape is None:
            continue
        nudge = (
            _fresh_stall_nudge(
                stall_nudge_of(entry.repo, entry.issue),
                now=stamp,
                launched_at=entry.launched_at,
                ttl_seconds=_NUDGE_TTL_SECS,
            )
            if shape == _NO_PROGRESS_RUNNING_IDLE
            else None
        )
        age = _no_progress_age_seconds(entry, now=stamp, stall_nudge=nudge)
        if age is not None and age < threshold_seconds:
            continue

        # #3888 review nit: an episode can exist for the `blocked`/`parked`
        # shape and still have been dropped by `_episode_stuck_row` (its
        # stored cause is still `CAUSE_UNKNOWN` — budget-exhausted or never
        # diagnosed); the other shapes genuinely carry no episode at all.
        # Phrasing this one sentence per shape keeps the alert from
        # pointing an operator at "no episode exists" when one does, just
        # an unresolved one.
        if shape == _NO_PROGRESS_BLOCKED:
            coverage_note = (
                "board_doctor: any block_log episode for this entry was "
                "dropped (its stored cause is still unresolved) or none "
                "was ever opened, and no merge-queue row covers it either"
            )
        elif shape == _NO_PROGRESS_RUNNING_IDLE:
            coverage_note = (
                "board_doctor: the drive session is alive but its own "
                f"stall nudge reports no pipeline state change for "
                f"{(nudge or {}).get('stalled_for', None) / 60.0:g}m "
                "(coord.notifier.store nudge record) — a live session "
                "alone is not evidence of progress"
            )
        else:
            coverage_note = (
                "board_doctor: no block_log episode or merge-queue row "
                "covers this entry"
            )

        if probes_spent >= _NO_PROGRESS_PROBE_CAP:
            cause, confidence, extra_evidence = (
                queue_diagnose.CAUSE_UNKNOWN,
                queue_diagnose.CONFIDENCE_NONE,
                (
                    f"board_doctor: this sweep's no-progress probe budget "
                    f"({_NO_PROGRESS_PROBE_CAP}) is spent — unprobed this "
                    "round, re-checked next sweep",
                ),
            )
        else:
            probes_spent += 1
            try:
                live = probe.probe(entry)
            except Exception:  # noqa: BLE001 — a probe failure must never crash the sweep
                _log.debug(
                    "board-doctor: no-progress probe failed for %s", entry.key, exc_info=True
                )
                live = None

            if live is not None:
                diagnosis = queue_diagnose.diagnose(entry, live, trigger=trigger)
                cause, confidence, extra_evidence = (
                    diagnosis.cause,
                    diagnosis.confidence,
                    diagnosis.evidence,
                )
            else:
                cause, confidence, extra_evidence = (
                    queue_diagnose.CAUSE_UNKNOWN,
                    queue_diagnose.CONFIDENCE_NONE,
                    (),
                )

        # For the `waiting-ready` shape specifically, name the exact
        # capacity numbers THIS sweep read — at BOTH levels the scheduler
        # gates on (repo ceiling and fleet-wide ceiling), each paired with
        # its resolved provenance (coordinator.yml vs. a systemd override)
        # — so a human checking an alert against the tick's own
        # `repo ... at its limit (N/M)` deferral text, or its global
        # early-return, can tell at a glance whether the two numbers
        # actually agree or this sweep read stale/incomplete evidence.
        # Every other shape has no capacity question to answer, so this is
        # never added for them.
        capacity_note = ""
        if shape == _NO_PROGRESS_WAITING_READY:
            repo_resolution = concurrency.repo_resolutions.get(
                entry.repo, concurrency.max_parallel_per_repo
            )
            fleet_resolution = concurrency.max_parallel
            capacity_note = (
                f"; repo capacity this sweep read: "
                f"{running_counts.get(entry.repo, 0)}/"
                f"{_render_ceiling(repo_resolution)} occupied "
                f"({repo_resolution.source}); fleet capacity this sweep "
                f"read: {fleet_occupied}/{_render_ceiling(fleet_resolution)} "
                f"occupied ({fleet_resolution.source})"
            )
        evidence = extra_evidence + (
            f"{coverage_note}, and it has made no progress for over "
            f"{config.board_doctor.no_progress_minutes}m ({shape}, "
            f"queue-state={entry.state}); last queue reason: "
            f"{entry.last_reason or '(none)'}{capacity_note}",
        )
        rows.append(
            StuckRow(
                queue="drive-queue",
                key=entry.key,
                state=entry.state,
                cause=cause,
                confidence=confidence,
                evidence=evidence,
                fresh=True,
            )
        )
    return rows


def collect_rows(*, config, trigger: str = "board-doctor-sweep") -> list[StuckRow]:
    """The impure production walk over both queues (#3758).

    Drive-queue: mirrors ``coord drive-queue diagnose``'s own wiring
    (:mod:`coord.commands.drive_queue`'s ``drive_queue_diagnose``) exactly —
    same ``block_log`` open-episode read, same ``GhLiveProbe`` construction
    — rather than a second, independently-built read of the same queue
    (#2096). Merge-queue: :func:`merge_queue_rows`, no `gh` needed.

    Every OPEN episode becomes a row, never only the subset
    :func:`coord.queue_diagnose.needs_diagnosis` would still re-probe:
    episodes that need fresh diagnosis go through `run_pass` as before;
    episodes whose cause Phase 1 already concluded are reconstructed
    directly via :func:`_episode_stuck_row`, so a cause this sweep already
    knows never becomes permanently invisible just because it is settled
    (#3758) — that would invert the point of a sweep that is supposed to
    ACT on settled causes. Within that settled set, one whose drive-queue
    row is also blocked on the #2972 fix-round ceiling gets one more live
    probe via :func:`_refresh_ceiling_blocked_settled_rows` first (#3777)
    — the one case a stale settled cause (e.g. a merge-conflict diagnosis
    recorded before the PR actually merged) would otherwise park forever,
    since nothing else ever re-probes a concluded episode.

    #3888 adds a THIRD pass, BEFORE the two reclassifiers below rather than
    after them (review fix): :func:`no_progress_rows` walks every current
    *entries* row, including ones neither the episode walk nor
    :func:`merge_queue_rows` ever looked at (a `running` row with no
    episode at all, a `waiting` row with nothing blocking it), and flags
    any that have sat with no progress past ``no_progress_minutes`` —
    classified through the same probe, or `unknown` when nothing fits, but
    never silently dropped. Running it first means a no-progress row that
    happens to diagnose to `nothing-blocking` or a checks-stale-race shape
    gets exactly the same second look an episode-sourced row does — one row
    shape, one answer, instead of two depending on which pass found it
    first. ``covered_keys`` is taken from the FULL row list built so far
    (both queues, not just drive-queue — :func:`merge_queue_rows` already
    covers a drive-queue dependent's merge the moment it enters that
    queue), so a row already explained by either walk is never
    double-classified or double-alerted.

    Deferred imports, same reason ``drive_queue_diagnose`` defers them: this
    module is imported by the CLI's thin registration layer, and these
    pull in `gh`-touching modules this harness's own tests must not need.
    """
    from coord import block_log  # noqa: PLC0415
    from coord.commands.drive_queue import (  # noqa: PLC0415
        _fetch_board_view_with_retry,
        _local_host_id,
    )
    from coord.drive import list_drive_sessions  # noqa: PLC0415
    from coord.drive_queue import (  # noqa: PLC0415
        entries_from_rows,
        read_systemd_max_parallel_flags,
    )
    from coord.health.aggregate import local_fleet_health_block  # noqa: PLC0415
    from coord.merge_queue import load_queue as load_merge_queue  # noqa: PLC0415
    from coord.state import (  # noqa: PLC0415
        baseline_red_merge_blocked,
        baseline_red_streaks,
        build_board,
        list_drive_queue,
    )

    episodes = [
        ep for ep in block_log.episodes(block_log.read_events()) if not ep.get("resolved")
    ]
    fresh_episodes = [ep for ep in episodes if queue_diagnose.needs_diagnosis(ep)]
    settled_episodes = [ep for ep in episodes if not queue_diagnose.needs_diagnosis(ep)]

    entries = entries_from_rows(list_drive_queue())
    entries_by_key = {e.key: e for e in entries}
    fleet_health = local_fleet_health_block([m.name for m in config.machines])
    live_sessions = frozenset(
        str(row["session_name"])
        for row in list_drive_sessions()
        if row.get("session_name")
    )
    board = build_board()
    local_host = _local_host_id()
    # Read once per sweep, same posture as `local_host` above — the
    # no-progress pass's repo and fleet ceilings must read the SAME
    # systemd-unit override (main unit or drop-in; if any) the real tick
    # enforces, not a second, independent read that could land on a
    # different mtime mid-sweep.
    systemd_flags = read_systemd_max_parallel_flags()
    probe = queue_diagnose.GhLiveProbe(
        config=config,
        board=board,
        fleet_health=fleet_health,
        live_sessions=live_sessions,
        local_host=local_host,
    )
    triggers = {str(ep["key"]): trigger for ep in fresh_episodes if ep.get("key")}
    diagnoses = queue_diagnose.run_pass(
        entries, fresh_episodes, probe=probe, keys=None, triggers=triggers, limit=None
    )
    refreshed_rows, settled_episodes = _refresh_ceiling_blocked_settled_rows(
        settled_episodes, entries_by_key=entries_by_key, probe=probe, trigger=trigger
    )
    rows = drive_queue_rows(diagnoses)
    rows.extend(refreshed_rows)
    rows.extend(row for ep in settled_episodes if (row := _episode_stuck_row(ep)) is not None)
    merge_entries = load_merge_queue()
    rows.extend(merge_queue_rows(merge_entries))

    # #3888: the episode/merge-queue walk above only ever covers a
    # `blocked`/`parked` transition or a merge-queue row — this is the
    # three-shape sweep over the whole queue that catches everything else.
    # A read failure here is advisory-only (the same posture
    # `_fetch_board_view_with_retry`'s own docstring documents for its
    # *leg_counts* read): this pass is additive on top of an already-
    # complete sweep, so losing it for one tick must never take the rest
    # of the sweep down with it. Run BEFORE the reclassifiers below (review
    # fix) so a no-progress row gets the same second look any other fresh
    # row does.
    #
    # #3901: also the ONE `BoardView` :func:`_reclassify_after_prereqs_
    # landed` reuses for its own `.facts(dep).landed` reads below — never a
    # second `GET /board` round trip for the same sweep. ``None`` on the
    # same advisory failure this comment already documents; that
    # reclassifier degrades to trusting only still-queued deps in that case
    # (see its own docstring), never raises.
    board_view: Any = None
    try:
        board_view = _fetch_board_view_with_retry()
    except Exception:  # noqa: BLE001 — see comment above
        _log.warning(
            "board-doctor: could not build a board view for the no-progress "
            "sweep this round — skipping it, episode/merge-queue coverage "
            "is unaffected",
            exc_info=True,
        )
    else:
        covered_keys = frozenset(row.key for row in rows)
        rows.extend(
            no_progress_rows(
                entries,
                covered_keys=covered_keys,
                live_sessions=live_sessions,
                board_view=board_view,
                probe=probe,
                config=config,
                trigger=trigger,
                local_host=local_host,
                systemd_flags=systemd_flags,
            )
        )

    rows = _reclassify_ceiling_blocked(rows)
    rows = _reclassify_checks_stale_race(rows, config=config)
    rows = _reclassify_stale_latch(rows, merge_entries, board=board, config=config)
    rows = _reclassify_after_prereqs_landed(
        rows, entries_by_key, board=board, board_view=board_view,
    )
    rows = _confirm_merge_pending_stuck(rows, merge_entries, board=board, config=config)
    rows = _reclassify_merge_never_attempted(rows, merge_entries, board=board, config=config)
    rows.extend(smoke_no_verdict_rows(board))

    blocked_streaks = {
        repo_name: streak
        for repo_name, streak in baseline_red_streaks().items()
        if baseline_red_merge_blocked(repo_name)
    }
    if blocked_streaks:
        # A red base poisons every row for that repo in both queues — they
        # are not individually stuck, so this withdraws them from this
        # sweep's OWN tier/dispatch decision (no C3 auto-action, no second
        # C4 alert) in favor of the single synthetic row
        # :func:`baseline_red_rows` builds. #3761 review: marking them
        # ``suppressed=True`` rather than dropping them from *rows*
        # entirely is load-bearing — :func:`sweep`'s ``clear_absent_rows``
        # walk treats anything missing from *rows* as resolved (wrong
        # here: the underlying cause is untouched, just superseded for
        # this sweep) and would wipe their convergence counters and
        # dismiss any live escalation filed against them, only for them
        # to reappear un-escalated — and re-page — the moment the base
        # flaps green. Keeping the row present (just suppressed) keeps it
        # in `sweep`'s ``touched`` set instead.
        #
        # #3761 non-blocking: this also withdraws C3 safe auto-actions
        # (e.g. a stale `CAUSE_PR_MERGED` row's cleanup) while the streak
        # holds — deliberate, since any per-row action here would be
        # against evidence this sweep no longer trusts (the base, not the
        # row, is why the queue diagnosis is unreliable right now).
        from coord.drive_queue import parse_key as _parse_row_key  # noqa: PLC0415

        def _suppress_if_blocked(row: StuckRow) -> StuckRow:
            parsed = _parse_row_key(row.key)
            if parsed is not None and parsed[0] in blocked_streaks:
                return replace(row, suppressed=True)
            return row

        rows = [_suppress_if_blocked(row) for row in rows]
        rows.extend(baseline_red_rows(blocked_streaks))
    return rows


def _board_doctor_other_resolutions() -> dict[str, list[float]]:
    """``key`` -> every ``resolved_at`` timestamp this host's
    :mod:`coord.block_log` records with evidence SOMETHING OTHER THAN this
    doctor's own confirmed action resolved it (#3889, widened by review).

    Two kinds of evidence qualify, either one on its own:

    * ``human_acted=True`` — an operator ``remove``, an ``add`` that
      requeued a stalled row, or a ``coord merge --override-human-required``
      that cleared a latched entry (all three via :func:`coord.commands.
      drive_queue._record_operator_release_entry`), a logged intervention,
      or a Gate-A sign-off. All of these run through :mod:`coord.block_log`'s
      own command surface and are already flagged there.
    * ``resolution == "landed"`` — :mod:`coord.block_log`'s
      ``_LANDED_CAUSE``, which its own docstring already distinguishes from
      ``_AUTO_MERGED_CAUSE``: "the entry sat here and something ELSE
      finished it while nobody was looking" — a human's hand merge, an
      unrelated branch landing, or ``coord reconcile-merges`` — as opposed
      to the queue's OWN tick landing the merge directly. Deliberately
      excludes ``resolution == "auto_merged"``: that is the queue's own
      tick completing the merge, which is exactly the shape of "the normal
      drive loop was carrying the doctor's own dispatched leg to
      completion" that stays credited to the doctor.

    Read ONCE per sweep (:func:`sweep`'s outcome-backfill section), not
    once per pending decision — the block log can hold weeks of episodes
    and every pending decision would otherwise re-parse it. Deliberately
    reads the WHOLE log rather than filtering by a ``since`` cutoff at the
    :func:`coord.block_log.read_events` layer: an episode's own ``enter``
    record (needed to pair with its ``resolve`` and populate
    ``resolved_at``) is almost always well before any cutoff worth passing
    here, since the row was already stuck when the doctor's decision was
    recorded — filtering it away would silently drop the very resolution
    this exists to find.

    Best-effort: an unreadable block log (missing file, corrupt line) reads
    as "no evidence of human action anywhere" — this must never block a
    sweep's outcome backfill, only decide which string an outcome gets.
    """
    try:
        from coord.block_log import episodes, read_events  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return {}
    try:
        items = episodes(read_events())
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, list[float]] = {}
    for item in items:
        if not item.get("human_acted") and item.get("resolution") != "landed":
            continue
        resolved_at = item.get("resolved_at")
        if resolved_at is None:
            continue
        try:
            out.setdefault(str(item.get("key") or ""), []).append(float(resolved_at))
        except (TypeError, ValueError):
            continue
    return out


def _board_doctor_attributed_to_other(
    other_resolutions: Mapping[str, Sequence[float]],
    key: str,
    since_ts: float | None,
) -> bool:
    """Did an "other"-attributed :mod:`coord.block_log` resolution for
    *key* (see :func:`_board_doctor_other_resolutions`) land at/after
    *since_ts* (#3889)?

    *since_ts* is the PAST decision's own ``recorded_at`` — ``None`` (an
    unparseable timestamp) reads as "no evidence either way" rather than
    matching everything. This answers "did an OPERATOR action resolve the
    row" (``remove``/``add``, ``log-intervention``, a Gate-A sign-off) OR
    "did something else land it while nobody was looking" (a hand merge,
    an unrelated branch, ``coord reconcile-merges``); it cannot distinguish
    either of those from "the ordinary drive loop resolved it with no
    operator action and no landed-elsewhere evidence", so a row that
    simply retried its way out on its own, with no logged human action and
    no ``landed`` resolution, still reads as the doctor's own
    `"progressed"` credit — the one case this signal cannot see is exactly
    the one #3875's wording ("the normal drive loop moved it") also cannot
    be told apart from "the doctor's own dispatched leg is what the normal
    drive loop was carrying to completion," so crediting the doctor there
    is the defensible default rather than a gap.
    """
    if since_ts is None:
        return False
    return any(ts >= since_ts for ts in other_resolutions.get(key, ()))


def _elapsed_hours_since_decision(decision: Mapping[str, Any], moment: datetime) -> float | None:
    """Hours between *decision*'s own ``recorded_at`` and *moment* (#3907).

    ``None`` when ``recorded_at`` is missing or unparseable — the caller
    treats that as "cannot confirm the grace window has elapsed" rather
    than as zero elapsed time, so a decision with a corrupt timestamp is
    held back from grading rather than graded against a fabricated
    instant.
    """
    from coord import state  # noqa: PLC0415

    ts = state.board_doctor_decision_ts(decision)
    if ts is None:
        return None
    return (moment.timestamp() - ts) / 3600.0


def _grade_open_outcomes(
    *,
    open_outcomes: Sequence[dict],
    current_by_row: Mapping[tuple[str, str], dict],
    touched: Collection[tuple[str, str]],
    other_resolutions: Mapping[str, Sequence[float]],
    moment: datetime,
    sweep_id: str,
    grace_hours: float,
    clear_absent_rows: bool = True,
) -> None:
    """Fill in / correct the outcome for every entry in *open_outcomes* —
    #3889's still-``None`` pending decisions and #3907's already-graded-
    but-revisable ``unchanged``/``regressed`` ones — against THIS sweep's
    view of their row.

    *current_by_row* maps a row :func:`sweep` actually classified this
    pass to its live ``{"cause", "escalated"}`` snapshot; a row that was
    itself skipped this pass (``suppressed``/``investigate-deferred``) is
    in *touched* but not *current_by_row*, and gets no verdict here
    either — still present, just not freshly classified, so there is
    nothing new to grade it against yet. A row key absent from *touched*
    entirely has resolved outright: unambiguous progress regardless of
    how little time has passed, which is exactly why this branch is
    never grace-gated below.

    #3907: a decision whose own ``outcome`` is still ``None`` — being
    graded for the FIRST time — is held back until *grace_hours* have
    passed since it was recorded, UNLESS its row has already resolved
    (the ``row_key not in touched`` branch, checked first). A decision
    that already carries a verdict is never grace-gated: it is being
    RE-graded, not graded for the first time, so there is nothing left
    to protect it from — only a chance to correct it.

    *clear_absent_rows* mirrors :func:`sweep`'s own parameter of the same
    name: "absent from *touched*" only means "resolved" when *rows* was
    the FULL stuck set. A caller that passed ``clear_absent_rows=False``
    has already declared *rows* a deliberately partial view — absence
    there is "not walked this pass", not "progressed" — so with it
    ``False`` the resolved branch below is skipped entirely and a row
    absent from *touched* is left untouched, exactly as its counter is
    left untouched by the matching ``clear_absent_rows`` guard around the
    tracked-row clear loop.

    The re-grade branch (``pending.get("outcome")`` already set) only
    writes when the freshly-computed outcome actually differs from the
    stored one — mirroring :func:`backfill_board_doctor_outcomes`'s own
    guard — so a sweep that keeps finding the same verdict for an
    already-graded decision does not re-``UPDATE`` (and re-``commit``)
    every row, every pass.
    """
    from coord import state  # noqa: PLC0415

    for pending in open_outcomes:
        pending_sweep_id = pending.get("sweep_id")
        if pending_sweep_id is None or pending_sweep_id == sweep_id:
            continue
        row_key = (pending.get("queue"), pending.get("key"))

        if row_key not in touched:
            if not clear_absent_rows:
                # Partial walk: absence here says nothing about whether
                # the row actually resolved, so leave the decision open
                # rather than mass-crediting (or mass-blaming) rows this
                # pass never observed.
                continue
            # Resolved: absent from the whole stuck set this pass — "the
            # leg finished" is itself the signal here, so this is never
            # held back by the grace window below.
            attributed_to_other = _board_doctor_attributed_to_other(
                other_resolutions, row_key[1], state.board_doctor_decision_ts(pending),
            )
            state.record_board_doctor_outcome(
                pending_sweep_id, row_key[0], row_key[1],
                "progressed_by_other" if attributed_to_other else "progressed",
            )
            continue

        if pending.get("outcome") is None:
            elapsed = _elapsed_hours_since_decision(pending, moment)
            if elapsed is None or elapsed < grace_hours:
                continue

        current = current_by_row.get(row_key)
        if current is None:
            continue
        attributed_to_other = _board_doctor_attributed_to_other(
            other_resolutions, row_key[1], state.board_doctor_decision_ts(pending),
        )
        outcome = state.evaluate_board_doctor_outcome(
            pending, current, attributed_to_other=attributed_to_other,
        )
        if outcome != pending.get("outcome"):
            state.record_board_doctor_outcome(pending_sweep_id, row_key[0], row_key[1], outcome)


def backfill_board_doctor_outcomes() -> int:
    """One-shot re-grade (#3907) of every PAST ``board_doctor_log``
    decision still open for grading — #3889's ``None``-outcome pending
    ones and #3907's ``unchanged``/``regressed`` ones — against the
    doctor's own CURRENT per-row state, without waiting for a fresh
    sweep to walk past those rows again.

    Reads :func:`coord.state.board_doctor_row_state`/
    ``board_doctor_tracked_rows`` rather than re-running
    :func:`collect_rows` — those are exactly the ``{"cause", "escalated"}``
    (or absence, meaning resolved) snapshot the most recent REAL sweep
    already maintains for every row, the identical shape
    :func:`_grade_open_outcomes`'s own ``current_by_row`` builds live.
    Never grace-gated (unlike a normal sweep's first grading): this is an
    explicit, operator-invoked correction of entries that are typically
    already well past any sensible grace window — see `coord
    drive-queue doctor-log --backfill-outcomes`.

    Returns the number of entries whose outcome this call actually
    changed.
    """
    from coord import state  # noqa: PLC0415

    open_outcomes = state.pending_board_doctor_outcomes() + state.regradable_board_doctor_outcomes()
    if not open_outcomes:
        return 0
    other_resolutions = _board_doctor_other_resolutions()
    tracked = set(state.board_doctor_tracked_rows())
    changed = 0
    for pending in open_outcomes:
        pending_sweep_id = pending.get("sweep_id")
        if pending_sweep_id is None:
            continue
        row_key = (pending.get("queue"), pending.get("key"))
        current: dict | None = None
        if row_key in tracked:
            row_state = state.board_doctor_row_state(row_key[0], row_key[1]) or None
            if row_state is not None:
                current = {
                    "cause": row_state.get("cause"),
                    "escalated": bool(row_state.get("escalated")),
                }
        attributed_to_other = _board_doctor_attributed_to_other(
            other_resolutions, row_key[1], state.board_doctor_decision_ts(pending),
        )
        outcome = state.evaluate_board_doctor_outcome(
            pending, current, attributed_to_other=attributed_to_other,
        )
        if outcome != pending.get("outcome"):
            state.record_board_doctor_outcome(pending_sweep_id, row_key[0], row_key[1], outcome)
            changed += 1
    return changed


def sweep(
    rows: Sequence[StuckRow],
    *,
    tier_of: Callable[[str], str] = default_tier_of,
    auto_dispatchers: Mapping[str, Callable[[StuckRow], bool]] | None = None,
    alert_dispatcher: Callable[[StuckRow], bool | str] | None = None,
    dry_run: bool = False,
    convergence_limit: int = CONVERGENCE_LIMIT,
    sweep_id: str | None = None,
    now: Callable[[], datetime] | None = None,
    clear_absent_rows: bool = True,
    escalation_resolver: Callable[[str, str], None] | None = None,
    capacity_check: Callable[[StuckRow], bool] | None = None,
    log_retention_days: float | None = None,
    backoff_initial_hours: float | None = None,
    backoff_cap_hours: float | None = None,
    outcome_grace_hours: float | None = None,
) -> SweepReport:
    """The pure harness core: classify-already-done rows in, decisions out.

    Takes no board connection and no `gh` handle — only *rows* (already
    classified by :func:`collect_rows` or hand-built by a test) and
    dispatcher callbacks. Never mutates the board itself; the only state
    this function's non-dry-run calls write is the sweep's OWN convergence
    counters and decision log (:mod:`coord.state`'s
    ``record_board_doctor_observation`` / ``record_board_doctor_decision``),
    never a queue row or a GitHub comment — those are C3/C4's job, reached
    only through *auto_dispatchers*/*alert_dispatcher*.

    For each row:
      1. Record this sweep's observation of (queue, key, cause) — same
         cause as last sweep increments the counter; a new/changed cause
         resets it to 1 and clears any prior escalation and backoff
         clock.
      2. ``tier_of(cause)`` decides auto vs. alert.
      3. Alert tier: dispatch via *alert_dispatcher* when either not yet
         escalated for this cause, OR already escalated but
         ``state.board_doctor_alert_due`` says the exponential backoff
         window has elapsed — never forever-silent once the first alert
         fires. Marks escalated (and bumps the backoff clock) only once
         the dispatch is CONFIRMED (``alert_dispatcher`` returns truthy).
         A dispatcher that raises or returns falsy leaves ``escalated``
         unchanged so the next sweep retries, rather than silently
         treating an unconfirmed write as done; returning
         :data:`ALERT_SKIPPED` does the same while saying the dispatcher
         chose not to file, not that filing failed.
      4. Auto tier: dispatch via ``auto_dispatchers[cause]`` while under
         *convergence_limit*; at/past the limit, falls back to the SAME
         backoff-gated alert path as step 3 instead of re-dispatching the
         same auto action forever. An ``auto_dispatchers`` callable
         returns ``True`` only once its action is CONFIRMED to have done
         something (mirroring *alert_dispatcher*'s own confirmed-truthy
         contract); a falsy return — the underlying C3 handler's
         primitive reported a no-op (row already gone, nothing to
         requeue, …) — is recorded as ``action="auto-dispatch-noop"``
         rather than ``"auto-dispatch"``, so the decision log never
         claims an action happened when the queue was, in fact, left
         untouched.

    *dry_run* classifies and logs but dispatches nothing and writes no
    convergence counter — a second sweep immediately after a dry run sees
    the SAME state a first sweep would, by construction.

    A row with ``suppressed=True`` (:func:`collect_rows`'s baseline-red
    fold, #3761) is counted as present — it is added to ``touched`` and
    logged with ``action="suppressed-baseline-red"`` — but skips tier
    decision, convergence counting, and both dispatch paths entirely: it
    is withdrawn from THIS sweep's own decision in favor of the single
    collapsed baseline-red row elsewhere in *rows*, not resolved, so it
    must not be treated as absent by *clear_absent_rows* below.

    "CONSECUTIVE sweeps" (the module docstring's convergence contract) is
    only true if a row that stops appearing also stops counting. *rows* is
    taken to be the FULL current stuck set *collect_rows* walked (both
    queues, every open episode) — not a partial view — so after dispatching
    every row above, any (queue, key) this call's :mod:`coord.state` still
    has a counter for but that did NOT appear in *rows* has, by definition,
    resolved or otherwise stopped being stuck: :func:`clear_absent_rows`
    (default on; *clear_absent_rows=False* opts a caller intentionally
    driving a partial row set out of it) drops its counter entirely via
    ``coord.state.clear_board_doctor_row``, so a later recurrence starts
    fresh at ``count=1, escalated=False`` instead of resuming a stale count
    or staying permanently escalation-blind.

    *escalation_resolver*, if given, is called as ``escalation_resolver
    (queue, key)`` for every row :func:`clear_absent_rows` drops — the
    counterpart to the alert path: once a row's cause has resolved (it
    stopped appearing), whatever alert was filed for it is stale and the
    resolver is expected to dismiss it. Called only on real (non-dry-run)
    sweeps, same as the clearing itself; never let a resolver's own
    failure interrupt the clear loop — that is the resolver's own
    responsibility to swallow, exactly like *alert_dispatcher* is expected
    to never raise.

    *capacity_check*, if given, is consulted BEFORE ``tier_of``/dispatch
    runs (#3770): a row for which ``capacity_check(row)`` returns ``True``
    is deferred — never dispatched this sweep, logged with
    ``action="investigate-deferred"`` (or ``"alert"`` once its own count
    reaches *convergence_limit*, see below), added to ``touched`` (so
    *clear_absent_rows* leaves it alone; it is still present, just not
    acted on this sweep). Peeking ``capacity_check(row)`` never itself
    claims a slot — only a row whose dispatch is actually CONFIRMED this
    sweep (``action == "auto-dispatch"``) does, via
    ``capacity_check.confirm_dispatch(row)`` if that method exists (see
    :class:`coord.board_doctor_policy.InvestigateCapacityGate`). That is
    what makes "bounding how many legs one sweep can fan out" true
    without ever starving a row outright: a row that is let through the
    check but turns out to be a converged-noop, an internal alert-tier
    fallback, or an in-flight no-op never holds a slot it never used, so
    the next row in *rows* — not the same row again next sweep — is the
    one that gets the freed capacity.

    #3918: a deferred row's convergence counter is NOT frozen — it bumps
    the exact same per-row counter (:func:`coord.state.
    record_board_doctor_observation`/:func:`coord.state.
    next_board_doctor_counter`) a non-deferred row's cause-observation
    bumps, "this cause observed again" meaning the same thing whether or
    not this sweep's own budget let it act on that observation. Once that
    count reaches *convergence_limit*, a deferred row escalates through
    the SAME :func:`_alert_action` path (subject to the same backoff via
    ``board_doctor_alert_due``) a non-deferred row past the limit already
    takes, instead of deferring indefinitely. Without this, a row the
    investigate tier's own budget keeps deferring sat at a permanently
    frozen ``count=0`` and could never escalate no matter how many sweeps
    it was deferred for (quadraui#2160: six consecutive hourly sweeps, all
    `investigate-deferred, count=0`).

    *log_retention_days*/*backoff_initial_hours*/*backoff_cap_hours*
    (#3889, #3875 asks B/C) default to ``None``, resolved below to
    :mod:`coord.state`'s module constants — the fleet's live
    ``coordinator.yml`` ``board_doctor:`` knobs when a caller threads them
    through (:func:`run_sweep` does, every real sweep), otherwise today's
    hardcoded defaults. *log_retention_days* governs
    :func:`coord.state.prune_board_doctor_log`, run once at the end of this
    sweep (never per-decision); the backoff hours govern
    :func:`coord.state.board_doctor_alert_due`/
    :func:`coord.state.mark_board_doctor_escalated`'s re-alert schedule.

    *outcome_grace_hours* (#3907) defaults to ``None``, resolved below to
    :data:`coord.state.BOARD_DOCTOR_OUTCOME_GRACE_HOURS`. Outcome
    backfill (see :func:`_grade_open_outcomes`) grades BOTH #3889's
    still-``None`` pending decisions and #3907's already-graded-but-
    revisable ``unchanged``/``regressed`` ones every sweep — but a
    decision's very FIRST grading is held back until this many hours
    have passed since it was recorded, unless its row has already
    resolved outright. Without this, a row that is still stuck on the
    exact same cause the very next sweep purely because the leg its own
    ``auto-dispatch`` launched has not had time to land yet — a Rust
    work/test/review cycle routinely runs well past an hour — gets
    graded against a doctor-raised escalation that is itself an artifact
    of being graded too soon, not evidence the action failed. A decision
    already carrying a verdict is re-graded on every sweep regardless of
    this window: there is nothing left to protect it from, only a chance
    to correct it once the row finally reaches a done-like state.
    """
    moment = (now or _utcnow)()
    sweep_id = sweep_id or f"sweep-{moment.strftime('%Y%m%dT%H%M%SZ')}"
    auto_dispatchers = auto_dispatchers or {}
    rows = list(rows)
    report = SweepReport(sweep_id=sweep_id, dry_run=dry_run, rows_observed=len(rows))

    from coord import state  # noqa: PLC0415 — deferred so dry-run callers need no DB.

    backoff_initial_hours = (
        backoff_initial_hours
        if backoff_initial_hours is not None
        else state.BOARD_DOCTOR_BACKOFF_INITIAL_HOURS
    )
    backoff_cap_hours = (
        backoff_cap_hours
        if backoff_cap_hours is not None
        else state.BOARD_DOCTOR_BACKOFF_CAP_HOURS
    )
    outcome_grace_hours = (
        outcome_grace_hours
        if outcome_grace_hours is not None
        else state.BOARD_DOCTOR_OUTCOME_GRACE_HOURS
    )

    touched: set[tuple[str, str]] = set()
    # Outcome backfill: decisions from EARLIER sweeps still open for a
    # verdict, read once up front so this sweep's own decisions below
    # never get evaluated against themselves. #3889's still-``None``
    # pending ones, plus #3907's already-graded-but-revisable
    # ``unchanged``/``regressed`` ones — a row that reaches a done-like
    # state (or moves on to a different cause) after being graded
    # negatively still deserves credit for that. Populated below as rows
    # are classified (`current_by_row`) and consumed by
    # `_grade_open_outcomes` after the main loop.
    open_outcomes = (
        state.pending_board_doctor_outcomes() + state.regradable_board_doctor_outcomes()
        if not dry_run
        else []
    )
    # #3889: evidence an operator action, a hand merge/unrelated branch
    # landing (or the ordinary drive loop, which this signal does NOT
    # distinguish from — see `_board_doctor_attributed_to_other`'s
    # docstring) moved a row BEFORE this sweep's own confirmed action could
    # have — read ONCE here rather than once per open decision, same
    # rationale as `open_outcomes` above. Empty (and never read) when
    # there is nothing open.
    other_resolutions = (
        _board_doctor_other_resolutions() if open_outcomes else {}
    )
    current_by_row: dict[tuple[str, str], dict] = {}

    def _alert_action(row: StuckRow) -> tuple[bool, str]:
        """Attempt the alert path for *row*; returns (escalated, action).

        Only a CONFIRMED dispatch (truthy return) marks the row escalated
        and persists that via `state.mark_board_doctor_escalated`. An
        exception, a falsy return or :data:`ALERT_SKIPPED` must not look
        like a successful alert: each leaves the row un-escalated so the
        next sweep gets another chance to actually deliver it.
        """
        if dry_run:
            return False, "would-alert" if alert_dispatcher is not None else "no-dispatcher"
        if alert_dispatcher is None:
            return False, "no-dispatcher"
        try:
            outcome = alert_dispatcher(row)
        except Exception:  # noqa: BLE001 — unconfirmed delivery, not a sweep crash
            return False, "alert-failed"
        if outcome == ALERT_SKIPPED:
            return False, "alert-skipped"
        if not outcome:
            return False, "alert-failed"
        state.mark_board_doctor_escalated(
            row.queue, row.key, now=moment,
            backoff_initial_hours=backoff_initial_hours,
            backoff_cap_hours=backoff_cap_hours,
        )
        return True, "alert"

    for row in rows:
        touched.add((row.queue, row.key))
        if row.suppressed:
            # #3761: present (so `clear_absent_rows` below leaves its
            # counter and any live escalation alone) but deliberately
            # neither counted nor dispatched — the collapsed baseline-red
            # row elsewhere in *rows* is what alerts for this repo. Reads
            # (never writes) the persisted counter purely so the decision
            # log / CLI can still show what this row was last known at,
            # instead of a misleading ``count=0``.
            prior = state.board_doctor_row_state(row.queue, row.key) or {}
            decision = Decision(
                queue=row.queue,
                key=row.key,
                cause=row.cause,
                tier=tier_of(row.cause),
                action="suppressed-baseline-red",
                count=int(prior.get("count", 0)),
                escalated=bool(prior.get("escalated", False)),
                dry_run=dry_run,
            )
            report.decisions.append(decision)
            if not dry_run:
                state.record_board_doctor_decision(
                    {
                        "sweep_id": sweep_id,
                        "queue": decision.queue,
                        "key": decision.key,
                        "cause": decision.cause,
                        "tier": decision.tier,
                        "action": decision.action,
                        "count": decision.count,
                        "escalated": decision.escalated,
                        "confidence": row.confidence,
                        "evidence": list(row.evidence),
                        "recorded_at": moment.isoformat(),
                    }
                )
            continue
        if capacity_check is not None and capacity_check(row):
            # #3770/#3918: the row's repo has no free investigate-tier slot
            # this sweep — never dispatched, same as before. But deferred no
            # longer means "frozen forever": this bumps the SAME per-row
            # convergence counter the dispatch path below bumps for a row
            # that IS let through ("this cause observed again", regardless
            # of whether capacity allowed acting on it), so a row the
            # investigate tier's own per-repo budget keeps deferring still
            # advances toward `convergence_limit` sweep over sweep — and
            # once it reaches it, escalates through the SAME `_alert_action`
            # path a non-deferred row takes past the limit, rather than
            # deferring with a permanently frozen `count=0` (quadraui#2160:
            # six consecutive hourly sweeps all logged
            # `investigate-deferred, count=0`, because nothing here ever
            # moved the counter that decides when to stop deferring and
            # page a human instead).
            if dry_run:
                prior = state.board_doctor_row_state(row.queue, row.key) or None
                counter_state = state.next_board_doctor_counter(
                    prior, row.cause, backoff_initial_hours=backoff_initial_hours,
                )
            else:
                counter_state = state.record_board_doctor_observation(
                    row.queue, row.key, row.cause,
                    backoff_initial_hours=backoff_initial_hours,
                )
            count, escalated = counter_state["count"], counter_state["escalated"]
            action = "investigate-deferred"
            if count >= convergence_limit:
                # Escalation is the only outcome a row can reach from here:
                # unlike the non-deferred branch below (which keeps a row
                # under `convergence_limit` eligible for `auto_dispatchers`
                # on every sweep), a row that has spent its whole counter
                # being deferred for capacity never gets routed back
                # through the dispatch path even once capacity frees up —
                # it alerts instead. A capacity-starved row that would
                # otherwise self-resolve on the next free slot is traded
                # for a human seeing it sooner.
                alert_due = not escalated or state.board_doctor_alert_due(
                    counter_state, now=moment, backoff_initial_hours=backoff_initial_hours,
                )
                if alert_due:
                    escalated, action = _alert_action(row)
            current_by_row[(row.queue, row.key)] = {
                "cause": row.cause, "escalated": escalated,
            }
            decision = Decision(
                queue=row.queue,
                key=row.key,
                cause=row.cause,
                tier=tier_of(row.cause),
                action=action,
                count=count,
                escalated=escalated,
                dry_run=dry_run,
            )
            report.decisions.append(decision)
            if not dry_run:
                state.record_board_doctor_decision(
                    {
                        "sweep_id": sweep_id,
                        "queue": decision.queue,
                        "key": decision.key,
                        "cause": decision.cause,
                        "tier": decision.tier,
                        "action": decision.action,
                        "count": decision.count,
                        "escalated": decision.escalated,
                        "confidence": row.confidence,
                        "evidence": list(row.evidence),
                        "recorded_at": moment.isoformat(),
                    }
                )
            continue
        if dry_run:
            prior = state.board_doctor_row_state(row.queue, row.key) or None
            counter_state = state.next_board_doctor_counter(
                prior, row.cause, backoff_initial_hours=backoff_initial_hours,
            )
        else:
            counter_state = state.record_board_doctor_observation(
                row.queue, row.key, row.cause,
                backoff_initial_hours=backoff_initial_hours,
            )
        count, escalated = counter_state["count"], counter_state["escalated"]
        # A row already escalated for this SAME cause is not
        # re-acted on forever — `board_doctor_alert_due` re-opens the
        # alert path on an exponential backoff instead, so a still-wrong
        # row keeps getting flagged (just less often over time) rather
        # than converging to permanent silence once its first alert
        # fires.
        alert_due = not escalated or state.board_doctor_alert_due(
            counter_state, now=moment, backoff_initial_hours=backoff_initial_hours,
        )

        tier = tier_of(row.cause)
        action = "converged-noop"

        if tier == TIER_ALERT:
            if alert_due:
                escalated, action = _alert_action(row)
        elif count < convergence_limit:
            dispatcher = auto_dispatchers.get(row.cause)
            if dispatcher is not None:
                if dry_run:
                    action = "auto-dispatch"
                else:
                    performed = dispatcher(row)
                    action = "auto-dispatch" if performed else "auto-dispatch-noop"
            else:
                action = "no-dispatcher"
        else:
            if alert_due:
                escalated, action = _alert_action(row)

        current_by_row[(row.queue, row.key)] = {"cause": row.cause, "escalated": escalated}

        if action == "auto-dispatch" and capacity_check is not None:
            # #3770: claim this row's capacity slot only now that its
            # dispatch is CONFIRMED (dry-run's own "would dispatch" counts
            # as confirmed too, same as the real-run branch above sets
            # `action="auto-dispatch"` only once `dispatcher(row)` returns
            # truthy) — never merely because `capacity_check` let the row
            # through. `confirm_dispatch` is optional on *capacity_check*
            # (a bare `Callable[[StuckRow], bool]` test double has none) so
            # this is a no-op rather than an error for those callers.
            confirm_dispatch = getattr(capacity_check, "confirm_dispatch", None)
            if confirm_dispatch is not None:
                confirm_dispatch(row)

        decision = Decision(
            queue=row.queue,
            key=row.key,
            cause=row.cause,
            tier=tier,
            action=action,
            count=count,
            escalated=escalated,
            dry_run=dry_run,
        )
        report.decisions.append(decision)
        if not dry_run:
            state.record_board_doctor_decision(
                {
                    "sweep_id": sweep_id,
                    "queue": row.queue,
                    "key": row.key,
                    "cause": row.cause,
                    "tier": tier,
                    "action": action,
                    "count": count,
                    "escalated": escalated,
                    "confidence": row.confidence,
                    "evidence": list(row.evidence),
                    "recorded_at": moment.isoformat(),
                }
            )

    if not dry_run:
        # Fill in / correct the verdict for every decision still open for
        # grading (#3889's pending + #3907's regradable, see
        # `_grade_open_outcomes`) — whether its row was classified again
        # THIS sweep (`current_by_row`, still stuck) or dropped out of
        # *rows* entirely (`touched`, resolved).
        _grade_open_outcomes(
            open_outcomes=open_outcomes,
            current_by_row=current_by_row,
            touched=touched,
            other_resolutions=other_resolutions,
            moment=moment,
            sweep_id=sweep_id,
            grace_hours=outcome_grace_hours,
            clear_absent_rows=clear_absent_rows,
        )

    if not dry_run and clear_absent_rows:
        for queue, key in state.board_doctor_tracked_rows():
            if (queue, key) not in touched:
                state.clear_board_doctor_row(queue, key)
                if escalation_resolver is not None:
                    try:
                        escalation_resolver(queue, key)
                    except Exception:  # noqa: BLE001, S110 — one row's cleanup
                        # failing must not abort the walk and leave the
                        # remaining tracked rows' counters uncleared.
                        pass

    if not dry_run:
        state.prune_board_doctor_log(retention_days=log_retention_days)

    return report


def run_sweep(
    *,
    config,
    tier_of: Callable[[str], str] = default_tier_of,
    auto_dispatchers: Mapping[str, Callable[[StuckRow], bool]] | None = None,
    alert_dispatcher: Callable[[StuckRow], bool | str] | None = None,
    dry_run: bool = False,
    convergence_limit: int = CONVERGENCE_LIMIT,
    escalation_resolver: Callable[[str, str], None] | None = None,
    capacity_check: Callable[[StuckRow], bool] | _CapacityCheckUnset | None = (
        _CAPACITY_CHECK_UNSET
    ),
) -> SweepReport:
    """Production entry point: :func:`collect_rows` then :func:`sweep` (#3758).

    The only function the CLI subcommand calls — everything it needs
    (`gh`, the board, the merge queue) is read here, so :func:`sweep` itself
    stays importable and runnable with none of that.

    *capacity_check* defaults to the sentinel :data:`_CAPACITY_CHECK_UNSET`
    rather than plain ``None`` (#3770): "caller did not pass it at all"
    resolves to the real per-repo investigate-tier cap via
    :func:`default_investigate_capacity`, while "caller explicitly passed
    ``capacity_check=None``" disables capacity-gating entirely — a plain
    ``None`` default could not tell those two cases apart.

    #3889: *config*'s ``board_doctor.log_retention_days`` /
    ``backoff_initial_hours`` / ``backoff_cap_hours`` (and #3907's
    ``outcome_grace_hours``, read the same defensive ``getattr`` way so a
    config version predating that knob still resolves to `sweep`'s own
    default) are read fresh HERE, every call — this is the per-sweep
    production entry point, so a ``coordinator.yml`` edit takes effect on
    the very next sweep with no daemon restart, same posture as every
    other `coord.board_doctor_policy` knob this module already reads live
    from *config*.
    """
    rows = collect_rows(config=config)
    resolved_capacity_check = (
        default_investigate_capacity(config)
        if capacity_check is _CAPACITY_CHECK_UNSET
        else capacity_check
    )
    board_doctor_cfg = getattr(config, "board_doctor", None)
    return sweep(
        rows,
        tier_of=tier_of,
        auto_dispatchers=auto_dispatchers,
        alert_dispatcher=alert_dispatcher,
        dry_run=dry_run,
        convergence_limit=convergence_limit,
        escalation_resolver=escalation_resolver,
        capacity_check=resolved_capacity_check,
        log_retention_days=getattr(board_doctor_cfg, "log_retention_days", None),
        backoff_initial_hours=getattr(board_doctor_cfg, "backoff_initial_hours", None),
        backoff_cap_hours=getattr(board_doctor_cfg, "backoff_cap_hours", None),
        outcome_grace_hours=getattr(board_doctor_cfg, "outcome_grace_hours", None),
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
