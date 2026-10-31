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

Convergence (#3758, load-bearing)
----------------------------------
A row stuck on the SAME cause across :data:`CONVERGENCE_LIMIT` (default 3)
consecutive sweeps stops being re-acted on and escalates to the alert tier
exactly once — never a second alert for the same still-unresolved cause,
never an auto-dispatch past the limit. Per-(queue, key) sweep counts persist
in `board_meta` via :mod:`coord.state`'s ``record_board_doctor_observation``
/ ``mark_board_doctor_escalated`` — the same one-row-of-truth pattern
``baseline_red_streaks`` uses for its own streak counter (#2096: one
question, one answer; convergence is decided by reading that ONE counter,
never re-derived from the decision log).

"CONSECUTIVE" is only honest because :func:`sweep` also clears a
(queue, key)'s counter the moment that row stops appearing in the current
*rows* — ``coord.state.clear_board_doctor_row``, driven by the
*clear_absent_rows* parameter (default on). Without it a resolved-then-recurring
row would resume its stale count instead of starting over, and a row that
escalated once could never be re-alerted after it later resolves and recurs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

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
#: re-acting on it and escalates once to the alert tier (#3758).
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
    #: `_ceiling_bypass_eligible` requires this before it will let a
    #: high-confidence cause skip the #2972 ceiling: bypassing the ceiling
    #: on a stale stored verdict (e.g. a `merge-conflict`/`high` row whose
    #: PR has since merged) would spend a fix-it leg against evidence that
    #: is no longer true, which is exactly what the ceiling exists to stop.
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


def merge_queue_rows(entries: Sequence[Any]) -> list[StuckRow]:
    """Local cause mapping for merge-queue rows (#3758).

    :func:`coord.queue_diagnose.run_pass` only understands drive-queue
    ``QueueEntry`` rows (#2276 Phase 1); the merge-queue's own state
    vocabulary is already thin (``pending``/``merging``/``merged``/
    ``conflict``/``skipped``/``human_required``), so this reuses
    ``queue_diagnose``'s ``CAUSE_*`` vocabulary rather than inventing a
    second one, and needs no `gh` call — the already-refreshed
    ``QueuedMerge.state``/``.error`` is evidence enough for a cause this
    coarse.
    """
    rows: list[StuckRow] = []
    for entry in entries:
        cause = _MERGE_QUEUE_STUCK_CAUSE.get(entry.state)
        if cause is None:
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
    `_ceiling_bypass_eligible` (`coord/board_doctor_policy.py`) already
    applies to its own ceiling-bypass decision, so the two stay consistent
    (#2096: one question — "is this evidence fresh enough to act on?" — one
    answer, `StuckRow.fresh`).
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

    Deferred imports, same reason ``drive_queue_diagnose`` defers them: this
    module is imported by the CLI's thin registration layer, and these
    pull in `gh`-touching modules this harness's own tests must not need.
    """
    from coord import block_log  # noqa: PLC0415
    from coord.commands.drive_queue import _local_host_id  # noqa: PLC0415
    from coord.drive import list_drive_sessions  # noqa: PLC0415
    from coord.drive_queue import entries_from_rows  # noqa: PLC0415
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
    probe = queue_diagnose.GhLiveProbe(
        config=config,
        board=board,
        fleet_health=fleet_health,
        live_sessions=live_sessions,
        local_host=_local_host_id(),
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
    rows = _reclassify_ceiling_blocked(rows)
    rows.extend(merge_queue_rows(load_merge_queue()))
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
         resets it to 1 and clears any prior escalation.
      2. ``tier_of(cause)`` decides auto vs. alert.
      3. Alert tier: dispatch via *alert_dispatcher* iff not already
         escalated for this cause, then mark escalated only once that
         dispatch is CONFIRMED (``alert_dispatcher`` returns truthy) —
         exactly once per (row, cause), the "escalate exactly once" half
         of #3758. A dispatcher that raises or returns falsy leaves
         ``escalated`` False so the next sweep retries, rather than
         silently treating an unconfirmed write as done; returning
         :data:`ALERT_SKIPPED` does the same while saying the dispatcher
         chose not to file, not that filing failed.
      4. Auto tier: dispatch via ``auto_dispatchers[cause]`` while under
         *convergence_limit*; at/past the limit, fall back to the alert
         path exactly once instead of re-dispatching the same auto action
         forever — the "stop being re-acted" half of #3758. An
         ``auto_dispatchers`` callable returns ``True`` only once its action
         is CONFIRMED to have done something (mirroring *alert_dispatcher*'s
         own confirmed-truthy contract); a falsy return — the underlying C3
         handler's primitive reported a no-op (row already gone, nothing to
         requeue, …) — is recorded as ``action="auto-dispatch-noop"`` rather
         than ``"auto-dispatch"``, so the decision log never claims an
         action happened when the queue was, in fact, left untouched.

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

    *capacity_check*, if given, is consulted BEFORE the convergence
    counter is bumped or ``tier_of``/dispatch runs (#3770): a row for
    which ``capacity_check(row)`` returns ``True`` is deferred — logged
    with ``action="investigate-deferred"`` at whatever count/escalated
    state the prior sweep left it at, added to ``touched`` (so
    *clear_absent_rows* leaves it alone; it is still present, just not
    reached this sweep), and otherwise skipped entirely: no counter
    movement, no dispatch, no escalation. Peeking ``capacity_check(row)``
    never itself claims a slot — only a row whose dispatch is actually
    CONFIRMED this sweep (``action == "auto-dispatch"``) does, via
    ``capacity_check.confirm_dispatch(row)`` if that method exists (see
    :class:`coord.board_doctor_policy.InvestigateCapacityGate`). That is
    what makes "bounding how many legs one sweep can fan out" true
    without ever starving a row outright: a row that is let through the
    check but turns out to be a converged-noop, an internal alert-tier
    fallback, or an in-flight no-op never holds a slot it never used, so
    the next row in *rows* — not the same row again next sweep — is the
    one that gets the freed capacity.
    """
    sweep_id = sweep_id or f"sweep-{(now or _utcnow)().strftime('%Y%m%dT%H%M%SZ')}"
    auto_dispatchers = auto_dispatchers or {}
    rows = list(rows)
    report = SweepReport(sweep_id=sweep_id, dry_run=dry_run, rows_observed=len(rows))

    from coord import state  # noqa: PLC0415 — deferred so dry-run callers need no DB.

    touched: set[tuple[str, str]] = set()

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
        state.mark_board_doctor_escalated(row.queue, row.key)
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
                        "recorded_at": (now or _utcnow)().isoformat(),
                    }
                )
            continue
        if capacity_check is not None and capacity_check(row):
            # #3770: the row's repo has no free investigate-tier slot this
            # sweep — present (so `clear_absent_rows` leaves its counter
            # and any live escalation alone) but deliberately neither
            # counted nor dispatched, same shape as the suppressed branch
            # above. Reads (never writes) the persisted counter so the
            # decision log / CLI still shows the row's last known count
            # instead of a misleading ``count=0``, and a deferred row
            # carries no escalation side effect of its own.
            prior = state.board_doctor_row_state(row.queue, row.key) or {}
            decision = Decision(
                queue=row.queue,
                key=row.key,
                cause=row.cause,
                tier=tier_of(row.cause),
                action="investigate-deferred",
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
                        "recorded_at": (now or _utcnow)().isoformat(),
                    }
                )
            continue
        if dry_run:
            prior = state.board_doctor_row_state(row.queue, row.key) or None
            updated = state.next_board_doctor_counter(prior, row.cause)
            count, escalated = updated["count"], updated["escalated"]
        else:
            counter = state.record_board_doctor_observation(row.queue, row.key, row.cause)
            count, escalated = counter["count"], counter["escalated"]

        tier = tier_of(row.cause)
        action = "converged-noop"

        if tier == TIER_ALERT:
            if not escalated:
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
            if not escalated:
                escalated, action = _alert_action(row)

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
                    "recorded_at": (now or _utcnow)().isoformat(),
                }
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
    """
    rows = collect_rows(config=config)
    resolved_capacity_check = (
        default_investigate_capacity(config)
        if capacity_check is _CAPACITY_CHECK_UNSET
        else capacity_check
    )
    return sweep(
        rows,
        tier_of=tier_of,
        auto_dispatchers=auto_dispatchers,
        alert_dispatcher=alert_dispatcher,
        dry_run=dry_run,
        convergence_limit=convergence_limit,
        escalation_resolver=escalation_resolver,
        capacity_check=resolved_capacity_check,
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
