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

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from coord import queue_diagnose

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


@dataclass(frozen=True)
class StuckRow:
    """One drive-queue or merge-queue row this sweep looked at."""

    queue: str  # "drive-queue" or "merge-queue"
    key: str
    state: str
    cause: str
    confidence: str
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class Decision:
    """One row's outcome from a single sweep — the unit both the test
    suite and the decision log assert on."""

    queue: str
    key: str
    cause: str
    tier: str
    #: "auto-dispatch" | "alert" | "would-alert" | "alert-failed" |
    #: "alert-skipped" | "no-dispatcher" | "converged-noop"
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
            )
        )
    return rows


def drive_queue_rows(diagnoses: Sequence[queue_diagnose.Diagnosis]) -> list[StuckRow]:
    """Adapt :func:`coord.queue_diagnose.run_pass` output to :class:`StuckRow`."""
    return [
        StuckRow(
            queue="drive-queue",
            key=d.key,
            state=d.state,
            cause=d.cause,
            confidence=d.confidence,
            evidence=d.evidence,
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
    ACT on settled causes.

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
    from coord.state import build_board, list_drive_queue  # noqa: PLC0415

    episodes = [
        ep for ep in block_log.episodes(block_log.read_events()) if not ep.get("resolved")
    ]
    fresh_episodes = [ep for ep in episodes if queue_diagnose.needs_diagnosis(ep)]
    settled_episodes = [ep for ep in episodes if not queue_diagnose.needs_diagnosis(ep)]

    entries = entries_from_rows(list_drive_queue())
    fleet_health = local_fleet_health_block([m.name for m in config.machines])
    live_sessions = frozenset(
        str(row["session_name"])
        for row in list_drive_sessions()
        if row.get("session_name")
    )
    probe = queue_diagnose.GhLiveProbe(
        config=config,
        board=build_board(),
        fleet_health=fleet_health,
        live_sessions=live_sessions,
        local_host=_local_host_id(),
    )
    triggers = {str(ep["key"]): trigger for ep in fresh_episodes if ep.get("key")}
    diagnoses = queue_diagnose.run_pass(
        entries, fresh_episodes, probe=probe, keys=None, triggers=triggers, limit=None
    )
    rows = drive_queue_rows(diagnoses)
    rows.extend(row for ep in settled_episodes if (row := _episode_stuck_row(ep)) is not None)
    rows.extend(merge_queue_rows(load_merge_queue()))
    return rows


def sweep(
    rows: Sequence[StuckRow],
    *,
    tier_of: Callable[[str], str] = default_tier_of,
    auto_dispatchers: Mapping[str, Callable[[StuckRow], None]] | None = None,
    alert_dispatcher: Callable[[StuckRow], bool | str] | None = None,
    dry_run: bool = False,
    convergence_limit: int = CONVERGENCE_LIMIT,
    sweep_id: str | None = None,
    now: Callable[[], datetime] | None = None,
    clear_absent_rows: bool = True,
    escalation_resolver: Callable[[str, str], None] | None = None,
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
         forever — the "stop being re-acted" half of #3758.

    *dry_run* classifies and logs but dispatches nothing and writes no
    convergence counter — a second sweep immediately after a dry run sees
    the SAME state a first sweep would, by construction.

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
                if not dry_run:
                    dispatcher(row)
                action = "auto-dispatch"
            else:
                action = "no-dispatcher"
        else:
            if not escalated:
                escalated, action = _alert_action(row)

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
    auto_dispatchers: Mapping[str, Callable[[StuckRow], None]] | None = None,
    alert_dispatcher: Callable[[StuckRow], bool | str] | None = None,
    dry_run: bool = False,
    convergence_limit: int = CONVERGENCE_LIMIT,
    escalation_resolver: Callable[[str, str], None] | None = None,
) -> SweepReport:
    """Production entry point: :func:`collect_rows` then :func:`sweep` (#3758).

    The only function the CLI subcommand calls — everything it needs
    (`gh`, the board, the merge queue) is read here, so :func:`sweep` itself
    stays importable and runnable with none of that.
    """
    rows = collect_rows(config=config)
    return sweep(
        rows,
        tier_of=tier_of,
        auto_dispatchers=auto_dispatchers,
        alert_dispatcher=alert_dispatcher,
        dry_run=dry_run,
        convergence_limit=convergence_limit,
        escalation_resolver=escalation_resolver,
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
