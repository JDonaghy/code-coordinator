"""Phase 0 stall log (#2235) — every ``blocked``/``parked`` entry, recorded.

**What this is.** #2235's evidence table showed that *five of seven* overnight
stalls named a symptom rather than a cause: the queue said "stale test
verdict" where the truth was a four-branch conflict, said "CI red, 2/2
attempts" for a build that went green 23 minutes later.  That finding is the
load-bearing constraint on everything Phase 1+ might do, and it was
reconstructed by hand, once, from a single morning's triage.  Phase 1's scope
is explicitly gated on **two weeks of this log**.

So this module records, for every entry that reaches ``blocked`` or
``parked``:

* the reason the queue **stated** at the moment it stopped (verbatim), and
* how the entry eventually **left** that state — which is the only honest
  Phase-0 read on its *true* cause — and
* whether a **human had to act** to get it out.

**What the RECORDER is emphatically NOT.**  The writer above does not
diagnose.  It runs no ``gh`` call, re-derives no gate, and consults no live
state that the tick has not already fetched for its own purposes.

**Phase 1 (#2276) fills the column Phase 0 cannot.**  ``true_cause`` is empty
on every ``enter`` record — at that instant nobody knows it — and on ``exit``
it is derived from *the release the queue itself performed*, which is still
the queue talking about itself.  #2235's finding is that the queue is wrong
about this five times out of seven.  So :mod:`coord.queue_diagnose` re-derives
the cause from live state and appends a third record shape here,
:data:`EVENT_DIAGNOSIS`, which :func:`episodes` folds onto the open episode as
its ``true_cause``.  That module owns the *deriving*; this one owns the
*record*, the pairing, and the honesty accounting — how often the derived
cause later agreed with how the episode actually resolved
(:func:`agreement_for`), reported as a rate rather than assumed
(:func:`summarize`).  A diagnostician that is confidently wrong is worse than
one that abstains, which is why ``unknown`` is counted separately from
``disagreed`` and never folded into it.

**#2540 fills the column that neither Phase counts: operator effort that never
touched the queue's own command surface.**  ``human_acted`` was, until #2540,
true only for the narrow set of releases the queue itself can attribute to a
person — an operator's ``remove``, a Gate-A sign-off.  Real recovery routinely
happens elsewhere entirely: a manual git rebase and force-with-lease push, a
direct ``coord test``/``coord merge --only``/``coord pr``/``coord fix``
against the assignment underneath an entry, a ``systemctl``/``coord agent
update``/``coord diagnose --reset`` on the machine running it.  None of that
is visible to a tick, so none of it was ever counted — a night of real manual
recovery could read as ``0 needed a human``, which is the exact failure this
module exists to prevent, just relocated one layer down.  This module cannot
detect that kind of intervention after the fact (recording must not probe live
state — see above), so :func:`intervention_event` gives an operator a place to
say so, in real time or shortly after: ``coord drive-queue log-intervention``
appends a record that :func:`episodes` folds onto whichever episode was open
for that key, or — if it had already resolved by the time the operator got to
logging it — the most recently closed one.  It flips ``human_acted`` to
``True`` the same as an ``operator_resolution_event`` does, and is kept
separately visible (``intervention_categories``) so the report can still say
*how* it knows, rather than asserting a single flat "a human acted" with no
paper trail behind it.

**Recording must never change a decision.**  Every public entry point here is
best-effort and swallows its own exceptions (:func:`record`), for the same
reason :mod:`coord.audit` does: the tick's merge/dispatch/attempt accounting
is the write that matters, and an observability append that can fail a tick is
strictly worse than no observability.  Nothing in this module is ever consulted
by :func:`coord.drive_queue.plan_tick` — it reads a plan that has already been
decided, and writes to a file no decision path reads.

**Storage.** One JSON object per line, appended, at
:func:`block_log_path`.  Append-only on purpose: an *episode* (entry blocked →
entry released) is two records that the reader pairs up (:func:`episodes`),
never one record mutated in place.  That keeps the writer a single ``O(1)``
append with no read-modify-write race between a tick and an operator command
running concurrently on the same host, and it keeps the raw file honest — a
resolution that contradicts an earlier one is visible rather than overwritten.

The file is per-host, because ticks are per-host and this is instrumentation,
not board state.  Reading two weeks of it off two machines is a ``cat``; a new
board table plus a daemon route is a schema migration and a network dependency
in the tick's write path, which is a poor trade for a temporary measurement.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from coord.drive_queue import (
    STATE_BLOCKED,
    STATE_DONE,
    STATE_PARKED,
    STATE_WAITING,
    QueueEntry,
    TickPlan,
)
from coord.gate_a import is_gate_a_refusal_reason
from coord.models import is_policy_refusal_reason

_log = logging.getLogger(__name__)

__all__ = [
    "AGREEMENT_ABSTAINED",
    "AGREEMENT_AGREED",
    "AGREEMENT_DISAGREED",
    "AGREEMENT_UNDECIDED",
    "AUTO_BUCKETS",
    "BUCKET_AUTO_MECHANISM",
    "BUCKET_AUTO_RESCUE",
    "BUCKET_HUMAN",
    "BUCKET_OPEN",
    "BUCKET_SUCCEEDED",
    "BY_DESIGN_CAUSES",
    "EVENT_DIAGNOSIS",
    "EVENT_ENTER",
    "EVENT_INTERVENTION",
    "EVENT_RESOLVE",
    "INTERVENTION_CATEGORIES",
    "OUTCOME_BUCKETS",
    "RESCUE_SOURCES",
    "STALL_STATES",
    "UNCLASSIFIED_CATEGORY",
    "agreement_for",
    "block_log_path",
    "diagnosis_event",
    "enter_event",
    "episode_bucket",
    "episode_category",
    "episodes",
    "intervention_event",
    "is_by_design",
    "log_location",
    "merge_only_event",
    "merge_only_fallback_event",
    "operator_resolution_event",
    "plan_events",
    "read_events",
    "record",
    "summarize",
]

# The two states this log exists to measure.  `failed` is deliberately absent:
# nothing in `coord.drive_queue` writes it any more (see `_reconcile_blocked`'s
# note), so a `failed` row is a pre-existing artefact, not a stall this queue
# produced.
STALL_STATES: frozenset[str] = frozenset({STATE_BLOCKED, STATE_PARKED})

EVENT_ENTER = "enter"
EVENT_RESOLVE = "resolve"
#: #2276 Phase 1.  A read-only re-derivation of an OPEN episode's cause, from
#: live state rather than from the queue's own stated reason.  Appended to the
#: same file rather than a new store for the same reason `enter`/`resolve` are
#: two records and not one mutated row: the writer stays an O(1) append with
#: no read-modify-write race, and a later diagnosis that contradicts an earlier
#: one stays visible instead of overwriting it.
EVENT_DIAGNOSIS = "diagnosis"
#: #2540.  An operator saying, out of band, "I acted on this key" — the
#: durable record for intervention that never touches the drive-queue command
#: surface (`remove`/`resume`) and so was invisible to `human_acted` before
#: this. See `intervention_event` and the module docstring's #2540 section.
EVENT_INTERVENTION = "intervention"

# Rotate at 4 MiB — comfortably more than two weeks at this fleet's rate
# (single-digit stalls a night, ~400 bytes a record), and small enough that
# `read_events` stays a whole-file parse rather than needing an index.
MAX_LOG_BYTES = 4 * 1024 * 1024


def block_log_path() -> Path:
    """Where the Phase-0 log lives.

    A function, not a module constant, for the same reason
    :func:`coord.filelock.drive_queue_lock_path` is: a constant captured at
    import freezes whatever ``Path.home()`` was at process start, which breaks
    both tests with a relocated ``$HOME`` and the daemon.
    """
    override = os.environ.get("COORD_BLOCK_LOG")
    if override:
        return Path(override)
    return Path.home() / ".coord" / "queue-block-log.jsonl"


def log_location(path: Path | None = None) -> dict[str, Any]:
    """Where this process would read the log, and whether it is actually there.

    The log is **per-host** and only the host that runs the tick writes one, so
    a reader that cannot say *where it read* is indistinguishable from a reader
    that found nothing — and "found nothing" over a stall log reads as a
    perfect score.  #1806 is the same trap in the fleet checks: a measurement
    taken on the wrong machine's filesystem, reported as if it were the right
    one's.  Every consumer of this dict is expected to surface ``exists=False``
    loudly rather than fold it into a zero (see
    :func:`coord.reports.run_queue_outcomes`).
    """
    target = block_log_path() if path is None else path
    try:
        exists = target.is_file()
        size = target.stat().st_size if exists else 0
    except OSError:  # pragma: no cover — defensive
        exists, size = False, 0
    try:
        host = socket.gethostname()
    except OSError:  # pragma: no cover — defensive
        host = ""
    return {"path": str(target), "host": host, "exists": exists, "size": size}


# ── classification (derived, never probed) ───────────────────────────────────
#
# The markers below are the ones the queue's own reconcile branches already
# stamp into `last_reason`.  Reusing them — rather than inventing a parallel
# taxonomy, or worse, re-deriving the gate — is the same convention
# `is_permanent_block_reason` and `is_gate_a_refusal_reason` follow, and it is
# what keeps this module a *recorder*: every string it classifies was written
# by the decision it is recording, not by a second opinion about it.

# (marker, true_cause, human_acted) — first match wins, so order is meaningful:
# the Gate-A release (#2063) must be tested before the generic park resume,
# because it is the one auto-release that a HUMAN caused.
_RESUME_CAUSES: tuple[tuple[str, str, bool], ...] = (
    (
        "(#2063)",
        "gate-a-signed — released only because a human recorded the sign-off",
        True,
    ),
    (
        "(#2230)",
        "gate-cleared-after-giveup — the merge gate read clear again after the "
        "queue had already given up; the stated reason was a stale symptom",
        False,
    ),
    (
        "(#2158)",
        "unrefreshable-reading — the park rested on a CI string with no writer "
        "left, and aged out rather than being released by real news",
        False,
    ),
    (
        "(#2182)",
        "ci-reported — a live re-check found the gate READY",
        False,
    ),
    (
        "(#1891)",
        "ci-reported — CI checks reported and the park released itself",
        False,
    ),
)

_LANDED_CAUSE = (
    "already-landed — the work merged or closed while the entry sat in this "
    "state, so the stated reason had outlived the thing it described"
)
#: #2350: distinct from `_LANDED_CAUSE` on purpose — that one means "the entry
#: sat here and something ELSE finished it while nobody was looking" (a human,
#: an unrelated branch merge, `coord reconcile-merges`); this one means the
#: QUEUE ITSELF, this same tick, ran `coord merge --only` and landed it —
#: see `coord.commands.drive_queue._run_merge_only_candidates`'s success
#: branch, the ONLY writer of the `outcome == "merged"` `_resolve_record`
#: reads to pick this cause. Conflating the two into one `auto-released`-style
#: bucket is exactly what #2350 was written to stop: #2235's corpus needs to
#: tell "the mechanism did this" apart from "the state flipped and something
#: unrelated did it".
_AUTO_MERGED_CAUSE = (
    "auto-merged — the queue completed the merge directly from the tick "
    "(#2350), Test/Review already satisfied; Merge was the only gate left"
)
_OPERATOR_CAUSE = (
    "operator-intervened — no automatic release fired; a human cleared it by "
    "hand, and what they actually fixed is NOT recorded here"
)
_UNKNOWN_CAUSE = "unclassified — left {state} by a transition this log does not recognise"
#: `_reconcile_blocked_unreachable`'s own outcome string, not a `last_reason`
#: marker — unlike `_RESUME_CAUSES`, this is matched on `outcome` directly in
#: `_resolve_record` because the function has exactly one reset shape (no
#: marker family to disambiguate), so giving it a `_resume_cause` entry would
#: add indirection a single constant does not need.
_UNREACHABLE_RESET_CAUSE = (
    "unreachable-reset — the entry was wedged on a merge-gate probe with "
    "nothing dispatched or landed for it to ever read, so the queue reset "
    "it to `waiting` instead of waiting out a gate that could never clear"
)


def _resume_cause(reason: str) -> tuple[str, bool]:
    """``(true_cause, human_acted)`` for an auto-resume, from its own reason."""
    for marker, cause, human in _RESUME_CAUSES:
        if marker in reason:
            return cause, human
    return (
        "auto-released — the tick resumed the entry without naming a known "
        "release condition",
        False,
    )


# ── event construction (pure) ────────────────────────────────────────────────


def _final_states(plan: TickPlan) -> dict[str, str]:
    """The state each key ENDS this tick in, per ``plan.writes()`` order.

    ``writes()`` is the plan's own apply order (reconciles, holds, blocked,
    deferrals), so last-write-wins here matches exactly what
    ``_apply_writes`` persists.  A key absent from the result is a key whose
    state this tick did not touch — not a key that stayed put by accident.
    """
    out: dict[str, str] = {}
    for key, updates in plan.writes():
        state = updates.get("state")
        if state:
            out[key] = str(state)
    return out


def _final_reasons(plan: TickPlan) -> dict[str, str]:
    """The ``last_reason`` each key ends this tick with, same order/rule."""
    out: dict[str, str] = {}
    for key, updates in plan.writes():
        reason = updates.get("last_reason")
        if reason:
            out[key] = str(reason)
    return out


def _outcomes(plan: TickPlan) -> dict[str, str]:
    """The reconcile outcome per key, for keys a reconcile touched."""
    return {item.key: item.outcome for item in plan.reconciles}


def _entry_fields(entry: QueueEntry | None) -> dict[str, Any]:
    if entry is None:
        return {}
    return {
        "position": entry.position,
        "attempts": entry.attempts,
        "resumes": entry.resumes,
        "machine": entry.machine or "",
    }


def plan_events(
    entries: Sequence[QueueEntry],
    plan: TickPlan,
    *,
    host: str = "",
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Every Phase-0 record one already-decided tick implies.

    Pure: *entries* is the PRE-tick snapshot the shell handed to
    :func:`coord.drive_queue.plan_tick`, *plan* is what that call returned.
    Nothing here re-reads the board, and nothing it returns is fed back into
    any decision — the caller appends the result and moves on.

    Two record shapes come out:

    * ``enter``   — a key whose state this tick moves INTO ``blocked``/
      ``parked`` from something else.  Carries the reason the queue stated,
      verbatim, and ``true_cause: ""`` — at this instant nobody knows it, and
      recording a guess is precisely the failure #2235 documents.
    * ``resolve`` — a key whose state this tick moves OUT of ``blocked``/
      ``parked``.  Carries the classification derived from the release itself
      (:func:`_resume_cause`), which is the strongest honest claim available
      without diagnosing.

    A key that is blocked before AND after (the overwhelmingly common case —
    a blocked entry that stayed blocked, or #2230's ``oscillating`` outcome,
    which rewrites ``last_reason`` but deliberately does not move the row)
    produces nothing.  This log counts transitions, not tick-seconds.
    """
    stamp = time.time() if now is None else now
    by_key = {e.key: e for e in entries}
    states = _final_states(plan)
    reasons = _final_reasons(plan)
    outcomes = _outcomes(plan)
    # A `Blocked` item's own reason is the escalation prose, which is at least
    # as specific as whatever `last_reason` its paired reconcile wrote — and
    # for the launch-failure path it is the ONLY reason there is.
    block_reasons = {item.key: item.reason for item in plan.blocked}

    out: list[dict[str, Any]] = []
    for key, new_state in sorted(states.items()):
        entry = by_key.get(key)
        old_state = entry.state if entry is not None else ""
        was_stalled = old_state in STALL_STATES
        now_stalled = new_state in STALL_STATES
        if was_stalled == now_stalled:
            continue
        reason = block_reasons.get(key) or reasons.get(key, "")
        if now_stalled:
            out.append(
                _enter_record(
                    key,
                    new_state,
                    reason,
                    entry,
                    host=host,
                    now=stamp,
                    outcome=outcomes.get(key, ""),
                )
            )
        else:
            out.append(
                _resolve_record(
                    key,
                    old_state,
                    new_state,
                    reason,
                    entry,
                    host=host,
                    now=stamp,
                    outcome=outcomes.get(key, ""),
                )
            )
    return out


def _enter_record(
    key: str,
    state: str,
    reason: str,
    entry: QueueEntry | None,
    *,
    host: str,
    now: float,
    outcome: str,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "event": EVENT_ENTER,
        "ts": now,
        "key": key,
        "state": state,
        "from_state": entry.state if entry is not None else "",
        # VERBATIM.  The whole point of the exercise is comparing this against
        # what the resolution later reveals, so it must not be normalised,
        # truncated or "helpfully" rewritten on the way in.
        "stated_reason": reason,
        # Filled by nobody.  A Phase-0 `enter` record cannot know the true
        # cause; the paired `resolve` is where that appears.
        "true_cause": "",
        "human_acted": None,
        "outcome": outcome,
        "host": host,
        "source": "tick",
    }
    record.update(_entry_fields(entry))
    return record


def _resolve_record(
    key: str,
    old_state: str,
    new_state: str,
    reason: str,
    entry: QueueEntry | None,
    *,
    host: str,
    now: float,
    outcome: str,
) -> dict[str, Any]:
    if outcome == "merged":
        # #2350: checked BEFORE the "done" branch below — a successful
        # Merge-only fast-path attempt also lands `new_state == "done"` (it
        # skips the `waiting`/relaunch cycle entirely), so without this
        # ordering the generic landed check would win the match and this
        # episode would be indistinguishable from "something else merged it
        # while it sat here" — exactly the conflation #2350 exists to split
        # out of #2230's `auto-released`/#2055's `already-landed`.
        true_cause, human = _AUTO_MERGED_CAUSE, False
        resolution = "auto_merged"
    elif outcome == "done" or new_state == "done":
        true_cause, human = _LANDED_CAUSE, False
        resolution = "landed"
    elif outcome == "resumed":
        true_cause, human = _resume_cause(reason)
        resolution = "auto_resumed"
    elif outcome == "unreachable_reset":
        true_cause, human = _UNREACHABLE_RESET_CAUSE, False
        resolution = "auto_resumed"
    else:
        true_cause, human = _UNKNOWN_CAUSE.format(state=old_state), False
        resolution = outcome or "moved"
    record: dict[str, Any] = {
        "event": EVENT_RESOLVE,
        "ts": now,
        "key": key,
        "state": new_state,
        "from_state": old_state,
        "stated_reason": entry.last_reason if entry is not None else "",
        "resolution": resolution,
        "true_cause": true_cause,
        "human_acted": human,
        "release_reason": reason,
        "outcome": outcome,
        "host": host,
        "source": "tick",
    }
    record.update(_entry_fields(entry))
    return record


def enter_event(
    entry: QueueEntry,
    *,
    state: str,
    reason: str,
    attempts: int | None = None,
    host: str = "",
    now: float | None = None,
    source: str = "tick",
) -> dict[str, Any]:
    """An ``enter`` record for a stall the TICK PLAN does not describe.

    One caller today: the launch-failure branch of ``coord drive-queue tick``,
    where the entry is blocked by the ``coord drive --tmux`` subprocess exiting
    non-zero *after* the plan was applied.  That transition is invisible to
    :func:`plan_events` — it is not in ``plan.writes()`` and never could be —
    and it is #2235's own ``stick-demo#1`` row ("dispatch failed"), so leaving
    it out would omit exactly the category the log was built to count.

    *attempts* overrides the snapshot's own count for the same reason the
    branch itself recomputes it (see the ``base_attempts`` note there): the
    row's persisted value has already moved on.
    """
    record = _enter_record(
        entry.key,
        state,
        reason,
        entry,
        host=host,
        now=time.time() if now is None else now,
        outcome="launch_failed",
    )
    record["source"] = source
    if attempts is not None:
        record["attempts"] = attempts
    return record


def merge_only_event(
    entry: QueueEntry,
    *,
    host: str = "",
    now: float | None = None,
) -> dict[str, Any]:
    """The ``resolve`` record for #2350's Merge-only fast path.

    The other caller-constructed record besides :func:`enter_event`, and for
    the identical structural reason: the transition happens OUTSIDE
    :func:`plan_events`'s view. A `merge_only` :class:`~coord.drive_queue.
    Reconcile` deliberately writes no `state` (see its docstring), so
    `plan.writes()` never carries the `parked`/`blocked` → `done` move this
    function records — it only happens once the shell's own live
    ``coord merge --only`` attempt reports success, strictly after
    ``_apply_writes`` has already run for the rest of the tick. *entry* is
    the PRE-tick snapshot (its ``state`` is still `parked`/`blocked`); the
    entry has already landed by the time this is called, so `new_state` is
    unconditionally :data:`~coord.drive_queue.STATE_DONE`.
    """
    stamp = time.time() if now is None else now
    record = _resolve_record(
        entry.key,
        entry.state,
        STATE_DONE,
        "",
        entry,
        host=host,
        now=stamp,
        outcome="merged",
    )
    record["source"] = "tick"
    return record


def merge_only_fallback_event(
    entry: QueueEntry,
    *,
    reason: str,
    host: str = "",
    now: float | None = None,
) -> dict[str, Any]:
    """The ``resolve`` record for #2350's Merge-only fast path RACE case:
    the live gate read clear enough to attempt a direct merge, but the
    attempt itself did not confirm a landed merge, so
    ``coord.commands.drive_queue._run_merge_only_candidates`` falls back to
    exactly the pre-#2350 ``resumed`` shape (``STATE_WAITING``) — OUTSIDE
    ``plan.writes()``, the same reason :func:`merge_only_event` exists for
    the success case: without this, the episode :func:`plan_events` already
    opened for *entry* would never see a matching close, and
    ``coord drive-queue block-log`` would show it stuck ``(unresolved)``
    forever even though the entry plainly left `parked`/`blocked` this tick.

    Classified as an ordinary ``resumed`` outcome — not a new #2350 cause —
    on purpose: unlike the success case, this genuinely IS "state flipped,
    cause a plain gate re-clear" (:func:`_resume_cause`'s fallback,
    ``auto-released``), the same honest non-answer a bare board-signal
    resume already gets. #2350's distinct cause is reserved for the tick
    actually landing the merge itself.
    """
    stamp = time.time() if now is None else now
    record = _resolve_record(
        entry.key,
        entry.state,
        STATE_WAITING,
        reason,
        entry,
        host=host,
        now=stamp,
        outcome="resumed",
    )
    record["source"] = "tick"
    return record


def operator_resolution_event(
    entry: QueueEntry,
    *,
    resolution: str,
    host: str = "",
    now: float | None = None,
) -> dict[str, Any]:
    """The ``resolve`` record for a stall a HUMAN cleared by hand.

    This is the other half of the metric #2235 asks for, and the half no tick
    can see: an operator running ``coord drive-queue remove`` on a blocked
    entry is the intervention the whole plan is trying to drive to zero.
    ``human_acted`` is unconditionally ``True`` here — the command ran because
    a person typed it.

    ``true_cause`` is deliberately the honest non-answer: what the operator
    actually fixed happened outside this process and is not recoverable from
    here.  A field that says "operator intervened" is useful; one that
    guesses at *why* would poison the very dataset Phase 1 is meant to size
    itself from.
    """
    stamp = time.time() if now is None else now
    record: dict[str, Any] = {
        "event": EVENT_RESOLVE,
        "ts": stamp,
        "key": entry.key,
        "state": "",
        "from_state": entry.state,
        "stated_reason": entry.last_reason,
        "resolution": resolution,
        "true_cause": _OPERATOR_CAUSE,
        "human_acted": True,
        "release_reason": "",
        "outcome": "",
        "host": host,
        "source": "operator",
    }
    record.update(_entry_fields(entry))
    return record


#: Documented common buckets for `coord drive-queue log-intervention
#: --category`.  Deliberately NOT a closed enum enforced anywhere — same
#: reasoning as `episode_category`'s open vocabulary: a kind of intervention
#: this build has never named must still be recordable as itself, not
#: coerced into "other" and lost.  This tuple exists purely so the CLI help
#: text and this module agree on the suggested starting set from #2540's own
#: evidence (a manual git rebase/conflict-resolution/force-push, a direct
#: `coord test`/`coord merge --only`/`coord pr`/`coord fix` against the
#: assignment, and infra-level recovery like `systemctl`/`coord agent
#: update`/`coord diagnose --reset`).
INTERVENTION_CATEGORIES: tuple[str, ...] = (
    "git-recovery",
    "cli-recheck",
    "infra",
    "other",
)


def intervention_event(
    *,
    key: str,
    category: str,
    note: str = "",
    host: str = "",
    now: float | None = None,
) -> dict[str, Any]:
    """A #2540 record: a human acted on *key* outside the queue's own commands.

    The other half of #2235's metric that :func:`operator_resolution_event`
    cannot reach.  That function fires only from inside a command
    (``remove``) that IS the resolution — a board mutation this process just
    performed.  A manual git rebase, a direct ``coord test``/``coord merge
    --only``/``coord pr``/``coord fix``, or a ``systemctl``/``coord agent
    update`` on the host underneath an entry are real operator effort that
    touches none of this process's own write paths, so nothing here can see
    them happen.  What it CAN do is give the operator a place to say so —
    this is that place, called from ``coord drive-queue log-intervention``.

    Deliberately not a ``resolve``: unlike an operator's ``remove``, logging
    an intervention does not by itself mean the stall is over — the entry may
    still be blocked, may have already resolved by the time the operator gets
    around to typing the command, or may resolve later still by an ordinary
    auto mechanism.  :func:`episodes` folds this onto whichever episode was
    open for *key* at the time, or, failing that, the most recently closed
    one — never inventing an episode, and never guessing at *why* the entry
    was stalled, the same restraint :func:`operator_resolution_event` already
    keeps.

    *category* is free text, not validated against
    :data:`INTERVENTION_CATEGORIES` — the same "open vocabulary, never
    coerced" contract :func:`episode_category` documents. An empty string
    normalises to ``"other"`` so the report always has something to show
    rather than a blank category column.
    """
    return {
        "event": EVENT_INTERVENTION,
        "ts": time.time() if now is None else now,
        "key": key,
        "category": str(category or "other"),
        "note": note,
        "host": host,
        "source": "operator",
    }


# ── #2276 Phase 1: the diagnosis record, and scoring it ──────────────────────


def diagnosis_event(
    *,
    key: str,
    state: str,
    stated_reason: str,
    true_cause: str,
    cause: str,
    confidence: str,
    evidence: Sequence[str] = (),
    contradicts_stated: bool = False,
    trigger: str = "",
    host: str = "",
    now: float | None = None,
) -> dict[str, Any]:
    """A ``diagnosis`` record for one still-open stall.

    Deliberately NOT a ``resolve``: the entry is still stalled, and a record
    that closed the episode would make an observation look like an outcome —
    which is the exact conflation (*a stated reason read as a cause*) this
    whole plan exists to undo.  :func:`episodes` folds it onto the open
    episode instead, so ``coord drive-queue block-log`` shows a populated
    ``true_cause`` where it previously showed ``(unresolved)`` while the
    episode itself stays open.

    ``human_acted`` is absent on purpose rather than ``False``: Phase 1 acts on
    nothing and knows nothing about what a human did, and a ``False`` here
    would be counted by :func:`summarize` as an auto-release that never
    happened.

    Every argument is a plain scalar so this module needs no import from
    :mod:`coord.queue_diagnose` — the recorder must not depend on the thing it
    records.
    """
    return {
        "event": EVENT_DIAGNOSIS,
        "ts": time.time() if now is None else now,
        "key": key,
        "state": state,
        # VERBATIM, same contract as `_enter_record`: the comparison between
        # this and `true_cause` below IS the deliverable.
        "stated_reason": stated_reason,
        "true_cause": true_cause,
        "cause": cause,
        "confidence": confidence,
        "evidence": [str(line) for line in evidence],
        "contradicts_stated": bool(contradicts_stated),
        "trigger": trigger,
        "host": host,
        "source": "diagnostician",
    }


AGREEMENT_AGREED = "agreed"
AGREEMENT_DISAGREED = "disagreed"
#: The diagnosis said ``unknown``.  A first-class verdict with NO penalty:
#: #2276 is explicit that a diagnostician which abstains is better than one
#: that guesses, so this is counted apart from `disagreed` and kept out of the
#: disagreement rate's denominator entirely.
AGREEMENT_ABSTAINED = "abstained"
#: The RESOLUTION is the unknown one.  `operator-intervened` says in as many
#: words that what the human fixed is not recorded, so scoring a diagnosis
#: against it would be scoring it against noise.
AGREEMENT_UNDECIDED = "undecided"

#: ``resolve``-side cause slug -> the diagnosis causes that agree with it.
#: Keyed on the slug before the first " — ", the same split
#: :func:`summarize` buckets on.  A resolution absent from this table is
#: undecidable, not a disagreement — see :data:`AGREEMENT_UNDECIDED`.
_AGREEING_CAUSES: dict[str, frozenset[str]] = {
    "already-landed": frozenset({"pr-merged", "pr-closed"}),
    "gate-cleared-after-giveup": frozenset(
        {"nothing-blocking", "ci-green", "ci-pending"}
    ),
    "ci-reported": frozenset({"nothing-blocking", "ci-green", "ci-pending"}),
    "unrefreshable-reading": frozenset({"nothing-blocking", "ci-green", "no-pr"}),
}


def agreement_for(diagnosed_cause: str, resolution_cause: str) -> str:
    """Did Phase 1's derived cause match how the episode actually ended?

    #2235's stated route to Phase 2 is *"run it read-only and check whether
    its diagnosis matches what the human actually did"*, so this comparison
    is a deliverable, not a follow-up.  It is deliberately conservative in
    both directions: an unrecognised resolution scores ``undecided`` rather
    than being counted as a win, and an ``unknown`` diagnosis scores
    ``abstained`` rather than being counted as a loss.
    """
    diagnosed = str(diagnosed_cause or "").split(" — ")[0]
    resolved = str(resolution_cause or "").split(" — ")[0]
    if not diagnosed:
        return ""
    if diagnosed == "unknown":
        return AGREEMENT_ABSTAINED
    agreeing = _AGREEING_CAUSES.get(resolved)
    if agreeing is None:
        return AGREEMENT_UNDECIDED
    return AGREEMENT_AGREED if diagnosed in agreeing else AGREEMENT_DISAGREED


# ── persistence (best-effort, never raises) ──────────────────────────────────


def record(events: Iterable[Mapping[str, Any]], *, path: Path | None = None) -> int:
    """Append *events* to the log.  Returns how many landed.

    **Never raises.**  Same contract as :func:`coord.audit.record_audit`, for
    the same reason and with more force: the caller is a drive-queue tick
    mid-way through applying real writes, and #2235's own prohibitions make
    the point that a healer which damages the thing it observes is worse than
    the stall.  A full disk, a read-only ``$HOME``, or a schema this build
    does not understand costs the measurement, never the tick.
    """
    target = block_log_path() if path is None else path
    lines = []
    for event in events:
        try:
            lines.append(json.dumps(dict(event), sort_keys=True, separators=(",", ":")))
        except (TypeError, ValueError):  # pragma: no cover — defensive
            _log.debug("block-log: unserialisable event dropped", exc_info=True)
    if not lines:
        return 0
    blob = "\n".join(lines) + "\n"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(target)
        # One `write` of one buffer, in append mode: on POSIX that is the
        # atomic unit, so a tick and an operator command appending at the same
        # instant interleave whole records rather than shredding a line. This
        # is why the format is append-only in the first place.
        with target.open("a", encoding="utf-8") as handle:
            handle.write(blob)
    except OSError:
        _log.debug("block-log: append failed", exc_info=True)
        return 0
    return len(lines)


def _rotate_if_needed(target: Path) -> None:
    try:
        if target.stat().st_size < MAX_LOG_BYTES:
            return
    except OSError:
        return
    # One generation only.  This is a two-week measurement, not an archive;
    # keeping N generations would imply a retention policy nobody has decided.
    try:
        target.replace(target.with_suffix(target.suffix + ".1"))
    except OSError:  # pragma: no cover — defensive
        _log.debug("block-log: rotation failed", exc_info=True)


def read_events(
    *,
    path: Path | None = None,
    since: float | None = None,
) -> list[dict[str, Any]]:
    """Every record in the log, oldest first.  Unparseable lines are skipped.

    Skipping rather than raising is deliberate: a half-written final line
    (power loss mid-append) must not make two weeks of evidence unreadable.
    """
    target = block_log_path() if path is None else path
    out: list[dict[str, Any]] = []
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if not isinstance(parsed, dict):
            continue
        if since is not None and float(parsed.get("ts") or 0.0) < since:
            continue
        out.append(parsed)
    out.sort(key=lambda item: float(item.get("ts") or 0.0))
    return out


# ── reading (pairing + summary) ──────────────────────────────────────────────


def _fold_intervention(
    open_by_key: dict[str, dict[str, Any]],
    out: list[dict[str, Any]],
    key: str,
    event: Mapping[str, Any],
) -> None:
    """Attach one #2540 ``intervention`` record to the episode it happened
    during — mutating the episode dict in place, exactly like the
    ``EVENT_DIAGNOSIS`` branch of :func:`episodes` does for ``true_cause``.

    Preferred target is the OPEN episode for *key* — the common case, an
    operator logging what they are doing while the entry is still stalled.
    Failing that, the most recently CLOSED episode for *key* in *out*:
    ``coord drive-queue log-intervention`` very often runs minutes AFTER the
    fix already landed (the operator ran ``coord merge --only``, it worked,
    then they typed the log line), and by then :func:`episodes` has already
    popped that episode out of ``open_by_key``.  Searched from the end of
    ``out`` because that list is built in the same time order *events* is
    iterated in, so the last match for *key* is the most recent past episode
    — never an older one a later re-block would make ambiguous.

    A *key* with no episode at all yet (logged before this host ever recorded
    a stall for it) is dropped, the same convention an orphan ``resolve`` or
    ``diagnosis`` already follows: there is nothing here to attach evidence
    to, and it is better to say so (`coord drive-queue log-intervention`
    warns) than to silently fabricate one.
    """
    episode = open_by_key.get(key)
    if episode is None:
        for candidate in reversed(out):
            if candidate.get("key") == key:
                episode = candidate
                break
    if episode is None:
        return
    category = str(event.get("category") or "other")
    episode.setdefault("interventions", []).append(
        {
            "ts": float(event.get("ts") or 0.0),
            "category": category,
            "note": str(event.get("note") or ""),
            "host": str(event.get("host") or ""),
        }
    )
    categories = episode.setdefault("intervention_categories", [])
    if category not in categories:
        categories.append(category)
    # The whole point: this is the signal `human_acted` was blind to before
    # #2540. Set unconditionally, whether the episode is still open (the
    # eventual `resolve` branch above ORs it back in rather than clobbering
    # it) or already closed (there is no later write to clobber it).
    episode["human_acted"] = True


def episodes(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Pair ``enter`` records with the ``resolve`` that closed them.

    One episode is one stall: an entry stopped, then (maybe) started again.
    An ``enter`` with no matching ``resolve`` is still an episode — with
    ``resolved=False`` — because "blocked and still blocked" is the outcome
    the plan cares most about, and dropping it would flatter the numbers.

    A ``resolve`` with no preceding ``enter`` (the log started mid-stall) is
    dropped rather than synthesised: an episode whose stated reason is
    unknown cannot answer the one question this log exists to answer.

    A ``diagnosis`` (#2276 Phase 1) folds onto the open episode: it fills
    ``true_cause`` — the column Phase 0 leaves empty and ``summarize`` buckets
    as ``(unresolved)`` — without closing the episode, because the entry is
    still stalled.  A diagnosis for a key with no open episode is dropped for
    the same reason an orphan ``resolve`` is: there is nothing to attach it
    to, and inventing the episode would fabricate a stated reason to compare
    against.  Once the episode resolves, ``true_cause`` reverts to the
    resolution's own account of what happened and the derived one is kept
    beside it as ``diagnosed_cause``, so ``agreement`` scores two independent
    claims rather than one claim against itself.

    An ``intervention`` (#2540) folds onto an episode the same way — see
    :func:`_fold_intervention` — except it targets a CLOSED episode too, not
    only an open one: unlike a diagnosis, an operator very often logs an
    intervention after the fact, once the fix they made by hand has already
    let the entry auto-resolve. It sets ``human_acted`` true and appends to
    ``intervention_categories``/``interventions`` without touching
    ``true_cause`` — what mechanically flipped the state is still whatever
    the resolution said; #2540 only means the record also captures that a
    human was in the loop, in a way the pre-#2540 log could not see at all.
    """
    open_by_key: dict[str, dict[str, Any]] = {}
    out: list[dict[str, Any]] = []
    for event in events:
        key = str(event.get("key") or "")
        if not key:
            continue
        kind = event.get("event")
        if kind == EVENT_ENTER:
            # A second `enter` without a `resolve` means the first episode's
            # release went unrecorded (a tick on another host, say). Close it
            # as unresolved rather than losing it.
            existing = open_by_key.pop(key, None)
            if existing is not None:
                out.append(existing)
            open_by_key[key] = {
                "key": key,
                "state": event.get("state", ""),
                "stated_reason": event.get("stated_reason", ""),
                "entered_at": float(event.get("ts") or 0.0),
                "attempts": event.get("attempts", 0),
                "host": event.get("host", ""),
                "resolved": False,
                "resolution": "",
                "true_cause": "",
                "human_acted": None,
                "resolved_at": None,
                "stalled_seconds": None,
                # #2276 Phase 1.  `diagnoses` is what `needs_diagnosis` spends
                # against the attempt budget, so it has to survive a restart —
                # which it does, being recounted from the log every read
                # rather than held in memory.
                "diagnoses": 0,
                "diagnosed_cause": "",
                "diagnosis_confidence": "",
                "diagnosis_evidence": [],
                "diagnosis_contradicts_stated": False,
                "agreement": "",
                # #2540.  Populated only by `intervention_event` records
                # folded on below — a fresh episode has neither.
                "interventions": [],
                "intervention_categories": [],
            }
        elif kind == EVENT_INTERVENTION:
            _fold_intervention(open_by_key, out, key, event)
        elif kind == EVENT_DIAGNOSIS:
            episode = open_by_key.get(key)
            if episode is None:
                continue
            cause = str(event.get("cause") or "")
            episode.update(
                {
                    "diagnoses": int(episode.get("diagnoses") or 0) + 1,
                    "diagnosed_cause": cause,
                    "diagnosis_confidence": str(event.get("confidence") or ""),
                    "diagnosis_evidence": list(event.get("evidence") or []),
                    "diagnosis_contradicts_stated": bool(
                        event.get("contradicts_stated")
                    ),
                    # The whole point of the column: an OPEN episode now has a
                    # cause where it used to have "".
                    "true_cause": str(event.get("true_cause") or ""),
                }
            )
        elif kind == EVENT_RESOLVE:
            episode = open_by_key.pop(key, None)
            if episode is None:
                continue
            resolved_at = float(event.get("ts") or 0.0)
            resolution_cause = str(event.get("true_cause") or "")
            episode.update(
                {
                    "resolved": True,
                    "resolution": event.get("resolution", ""),
                    "true_cause": resolution_cause,
                    # #2540: OR in whatever an earlier `intervention_event`
                    # already set. Without the `or`, an ordinary auto
                    # resolution's own `human_acted=False` would blindly
                    # overwrite the `True` a logged intervention just
                    # earned — exactly the undercount this issue is about,
                    # relocated one line down instead of fixed.
                    "human_acted": bool(event.get("human_acted"))
                    or bool(episode.get("human_acted")),
                    "resolved_at": resolved_at,
                    "stalled_seconds": max(0.0, resolved_at - episode["entered_at"]),
                    "source": event.get("source", ""),
                    "agreement": agreement_for(
                        str(episode.get("diagnosed_cause") or ""), resolution_cause
                    ),
                }
            )
            out.append(episode)
    out.extend(open_by_key.values())
    out.sort(key=lambda item: float(item.get("entered_at") or 0.0))
    return out


def summarize(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The counts #2235's success metric is defined in terms of.

    ``human_acted`` is the numerator that must trend DOWN.  It is reported
    alongside ``open`` (still stalled) rather than in isolation, because a
    queue that stops needing interventions by leaving everything blocked
    forever is the failure mode, not the goal — the two numbers are only
    meaningful together.

    ``repeat_causes`` is #2235's tripwire, in the weakest form Phase 0 can
    honestly support: the same ``(repo, stated_reason-prefix)`` stalling more
    than once.  The issue's framing — *a repeat is a bug report, not a
    success* — needs that count visible from day one, or the trend line has
    nothing to be checked against.

    ``diagnosis`` is #2276's own success criterion, and it is reported as a
    measurement rather than an assertion.  ``disagreement_rate`` is
    ``disagreed / (agreed + disagreed)`` and is ``None`` — not ``0.0`` — when
    nothing scorable has resolved yet, because a rate of zero out of zero
    reads as a perfect record.  ``abstained`` sits outside that fraction
    entirely: #2276 makes ``unknown`` a first-class verdict with no penalty,
    and putting it in the denominator would penalise it.  ``contradicted``
    counts the episodes where the live evidence positively ruled the queue's
    stated reason out — the five-of-seven number, finally measured instead of
    reconstructed by hand.

    ``human_acted_logged`` (#2540) is the subset of ``human_acted`` this build
    can actually SHOW ITS WORK for: episodes carrying at least one
    ``coord drive-queue log-intervention`` record.  The rest of ``human_acted``
    still comes from the pre-#2540 queue-command surface (an operator's
    ``remove``, a Gate-A sign-off) — real, but with no evidence trail beyond
    "the resolve record said so".  Neither of those two is a claim that
    *nothing else* happened: a night with ``human_acted_logged == 0`` means
    no intervention was LOGGED, not that none occurred — this module still
    cannot see a git rebase or a direct ``coord test`` call that nobody typed
    the one extra command for.  That is the honest limit of what an
    append-only log fed only by this process's own choke points can claim,
    and it is why the CLI help for ``log-intervention`` asks operators to run
    it as a matter of habit rather than treating the count as automatically
    complete.
    """
    by_state: dict[str, int] = {}
    by_cause: dict[str, int] = {}
    seen_pairs: dict[tuple[str, str], int] = {}
    human = 0
    human_logged = 0
    auto = 0
    still_open = 0
    diagnosed = 0
    contradicted = 0
    agreement_counts: dict[str, int] = {}
    for item in items:
        if item.get("diagnosed_cause"):
            diagnosed += 1
            if item.get("diagnosis_contradicts_stated"):
                contradicted += 1
            verdict = str(item.get("agreement") or "")
            if verdict:
                agreement_counts[verdict] = agreement_counts.get(verdict, 0) + 1
        state = str(item.get("state") or "?")
        by_state[state] = by_state.get(state, 0) + 1
        if not item.get("resolved"):
            still_open += 1
        elif item.get("human_acted"):
            human += 1
            if item.get("intervention_categories"):
                human_logged += 1
        else:
            auto += 1
        cause = episode_category(item)
        by_cause[cause] = by_cause.get(cause, 0) + 1
        repo = str(item.get("key") or "").split("#")[0]
        stated = " ".join(str(item.get("stated_reason") or "").split())[:60]
        if stated:
            # An episode with NO stated reason cannot repeat one. Counting the
            # empty string would make every reasonless row in a repo collide
            # into a single fat "repeat", which is the opposite of a tripwire.
            pair = (repo, stated)
            seen_pairs[pair] = seen_pairs.get(pair, 0) + 1
    repeats = {
        f"{repo}: {stated}": count
        for (repo, stated), count in sorted(seen_pairs.items())
        if count > 1
    }
    agreed = agreement_counts.get(AGREEMENT_AGREED, 0)
    disagreed = agreement_counts.get(AGREEMENT_DISAGREED, 0)
    scored = agreed + disagreed
    return {
        "episodes": len(items),
        "by_state": by_state,
        "by_cause": by_cause,
        "human_acted": human,
        "human_acted_logged": human_logged,
        "auto_released": auto,
        "open": still_open,
        "repeat_causes": repeats,
        "diagnosis": {
            "diagnosed": diagnosed,
            "contradicted_stated_reason": contradicted,
            "agreed": agreed,
            "disagreed": disagreed,
            "abstained": agreement_counts.get(AGREEMENT_ABSTAINED, 0),
            "undecided": agreement_counts.get(AGREEMENT_UNDECIDED, 0),
            "disagreement_rate": (disagreed / scored) if scored else None,
        },
    }


# ── #2270: the outcome vocabulary the queue-outcomes report buckets on ───────
#
# One question — *what fraction of the queue got over the line without me?* —
# needs exactly two derivations over an episode, and both belong here rather
# than in `coord.reports`: this module owns what an episode MEANS, and a
# second opinion living next to the renderer is how `by_cause` and a report's
# categories drift apart.

#: Merged with no stall recorded at all.  Structurally **never** produced from
#: an episode — an episode IS a stall — so :func:`episode_bucket` cannot
#: return it.  The queue-outcomes report counts this bucket from the merge
#: events on the audit trail and says so in its notes; naming the constant
#: here keeps the five-bucket vocabulary in one place.
BUCKET_SUCCEEDED = "succeeded"
#: Stalled, then released by a deterministic arm that already exists (#1891
#: park auto-resume, #2230 blocked re-check, #2197 CI re-run, #2252 flaky
#: re-run) — i.e. a `resolve` whose ``human_acted`` is false.
BUCKET_AUTO_MECHANISM = "auto_resolved_mechanism"
#: Stalled, then released by the rescue AGENT (#2268).  **Structurally zero
#: today** — nothing writes a `resolve` with one of :data:`RESCUE_SOURCES` —
#: and modelled anyway, from day one, for two reasons #2270 is explicit
#: about: the report does not change shape when #2268 lands, and "a
#: deterministic arm fixed it" stays visibly distinct from "an agent judged
#: it" instead of being merged into one flattering number.
BUCKET_AUTO_RESCUE = "auto_resolved_rescue"
#: Stalled and a human acted.  Broken down by category, and split again by
#: :func:`is_by_design` — see that function for why the split is load-bearing.
BUCKET_HUMAN = "human"
#: Still stalled at the end of the window.  Reported beside the others rather
#: than dropped: a queue that stops needing interventions by leaving
#: everything blocked forever is the failure mode, not the goal.
BUCKET_OPEN = "open"

#: Display / iteration order — worst-case last.
OUTCOME_BUCKETS: tuple[str, ...] = (
    BUCKET_SUCCEEDED,
    BUCKET_AUTO_MECHANISM,
    BUCKET_AUTO_RESCUE,
    BUCKET_HUMAN,
    BUCKET_OPEN,
)

#: The headline metric's numerator: ``(succeeded + auto_resolved_mechanism +
#: auto_resolved_rescue) / total``, the fraction the operator wants trending
#: to ~100%.
AUTO_BUCKETS: frozenset[str] = frozenset(
    {BUCKET_SUCCEEDED, BUCKET_AUTO_MECHANISM, BUCKET_AUTO_RESCUE}
)

#: ``source`` values on a `resolve` record that mean *the rescue agent did
#: this*.  Two spellings because #2268 has not landed and cannot be asked;
#: whichever it stamps, this report already counts it in its own series
#: rather than silently flattering :data:`BUCKET_AUTO_MECHANISM`.
RESCUE_SOURCES: frozenset[str] = frozenset({"rescue", "rescue-agent"})

#: What :func:`episode_category` returns for an episode with no cause at all —
#: an open stall nobody has diagnosed yet.  Deliberately the same label
#: :func:`summarize` has always used for that case, so the two agree.
UNCLASSIFIED_CATEGORY = "(unresolved)"

#: Cause slugs that mean *a human was SUPPOSED to be the one who acted*.  This
#: is not an enum over the category vocabulary — that vocabulary is open and
#: read from the data (see :func:`episode_category`) — it is a small,
#: deliberately incomplete allow-list, and an unknown category is `False`.
BY_DESIGN_CAUSES: frozenset[str] = frozenset({"gate-a-signed"})


def episode_category(episode: Mapping[str, Any]) -> str:
    """One episode's category — the cause slug, as an OPEN vocabulary.

    The slug before the first ``" — "`` of ``true_cause``, which is whatever
    the resolution (or, for a still-open episode, #2276's diagnosis) named.
    Deliberately not an enum and deliberately not validated: #2270's category
    set is the ``true_cause`` vocabulary that two weeks of Phase 0 exists to
    *discover*, so a cause this build has never seen must appear in the report
    as itself rather than as "other".  Same contract
    :class:`coord.reports.ColumnMeta`'s ``kind`` already states — a client that
    meets a value it predates falls back, never fails.
    """
    return str(episode.get("true_cause") or "").split(" — ")[0] or UNCLASSIFIED_CATEGORY


def episode_bucket(episode: Mapping[str, Any]) -> str:
    """Which outcome bucket one episode lands in.

    Never :data:`BUCKET_SUCCEEDED`: an episode is a stall by construction, so
    "merged without ever stalling" is not a shape this log can produce — the
    report counts that bucket from a different source and says so.

    The ``human_acted`` test precedes the rescue test on purpose.  A Gate-A
    release (#2063) is recorded as an auto-resume by the tick that performed
    it, yet its ``human_acted`` is true because a person signed the gate; a
    bucketing that read the mechanism first would file the one release a human
    definitely caused under "resolved itself".
    """
    if not episode.get("resolved"):
        return BUCKET_OPEN
    if episode.get("human_acted"):
        return BUCKET_HUMAN
    if str(episode.get("source") or "") in RESCUE_SOURCES:
        return BUCKET_AUTO_RESCUE
    return BUCKET_AUTO_MECHANISM


def is_by_design(episode: Mapping[str, Any]) -> bool:
    """Was a human *supposed* to be the thing that unblocked this?

    Two stalls in this fleet stop for a person on purpose: an unsigned Gate A
    (#2063 — a sign-off is a human judgement and automating it would defeat
    the gate) and a policy refusal (#2234 — the rule it names is standing, and
    nothing will arrive to clear it).  Without this flag both land in the
    ``human`` bucket, the target metric can never reach 100%, and a working
    queue reads as permanent failure.

    Derived from the queue's own predicates
    (:func:`coord.gate_a.is_gate_a_refusal_reason`,
    :func:`coord.models.is_policy_refusal_reason`) over the reason the queue
    itself stamped, plus :data:`BY_DESIGN_CAUSES` for the resolution-side slug
    — never from a second classification invented here.  That also means an
    UNDIAGNOSED open Gate-A park still flags correctly: its category is
    ``(unresolved)``, but its stated reason still carries the marker.
    """
    if episode_category(episode) in BY_DESIGN_CAUSES:
        return True
    reason = str(episode.get("stated_reason") or "")
    if not reason:
        return False
    return is_gate_a_refusal_reason(reason) or is_policy_refusal_reason(reason)
