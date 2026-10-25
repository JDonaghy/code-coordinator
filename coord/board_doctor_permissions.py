"""#3745's C2: the permission model the board-doctor sweep's auto-act tier
runs under, and its enforcement boundary.

The problem this module solves
-------------------------------
`coord merge` / `coord drive-queue add|remove` and friends are CLI commands
written for an attended operator session — they assume a human is reading
their output, can be asked to re-run with `--force-merge` or
`--override-human-required` when a gate is wrong, and is the one accountable
for that override. The board-doctor sweep (`coord.board_doctor`, #3758) runs
unattended on a timer with nobody reading its output, so it must never reach
those operator-only escape hatches — not because a runtime check happens to
turn them away today, but because nothing in this module's closed allow-list
names them, so there is no code path from the sweep into them at all.

This module is the enforcement boundary between the two: every daemon-
internal action the sweep's C3 dispatchers are allowed to take is named in
:data:`SAFE_TIER_ACTIONS`, and :func:`authorize_sweep_action` is the single
gate every one of them must pass through before touching the board. A cause
dispatcher that wants to do something not on this list is a bug to fix by
deliberately editing :data:`SAFE_TIER_ACTIONS` (and adding a real handler to
:data:`_HANDLERS`) in a reviewable diff — never by relaxing the gate at the
call site.

Every safe-tier action is a mechanical board mutation with a crisp,
re-checkable precondition, so each handler here calls `coord`'s in-process
primitives directly rather than spawning an agent leg. A future cause that
needs LLM judgment may add a leg scoped with `--allowedTools` to its own
safe-tier subcommands and never `--dangerously-skip-permissions` — it still
calls :func:`authorize_sweep_action`, because this module is the boundary,
not the CLI layer.

Operator-declared policy is preserved, never declared
-------------------------------------------------------
`coord.state.enqueue_drive_queue` is an upsert that REPLACES every
operator-declared column on every call: `machine`, `after`, `hold_after`,
`hold_reason`, `resume_when`, `hold_scope`, `max_fix_rounds`,
`no_acceptance`, `plan_destructive`. A write that merely *omits* them
therefore disarms a #1757 deploy gate and clears a #3236 destructive-plan
marker — a blanket board permission by accident, which is exactly what
:data:`SAFE_TIER_ACTIONS` exists to rule out. Three invariants keep that out
of reach:

* No policy parameter exists on any primitive here, so a cause dispatcher
  cannot set `hold_scope="fleet"` or `plan_destructive` by construction.
* Every write carries an already-queued row's own declaration back through
  unchanged, read off the row *inside* this module and never passed in — a
  preserved value is not a caller-supplied one. A caller that passes a
  `machine`/`after`/`position` CONTRADICTING the queued row is refused by
  name; :func:`sweep_drive_queue_requeue` is the only way to express a
  remove + re-add, because it is the only one that can take the full
  declaration down and put it back.
* `hold_state`/`hold_probes` cannot be carried through at all: they are
  derived from `hold_after` and reset to ``armed``/``0`` on every write,
  with no parameter to restore them. A gate that has FIRED (or been
  released by a human `coord drive-queue resume`) is therefore refused
  outright rather than re-armed behind the operator's back — see
  :func:`_refuse_if_gate_mid_flight`.

Each write is confirmed by re-reading the row and comparing it field by
field against what was declared, not by trusting the write call's own
return, so an unconfirmed write raises
:class:`SweepWriteNotConfirmedError` rather than returning something a
caller reads as success. The one pre-write check that applies to the
mechanical parameters this module accepts is
:func:`coord.drive_queue.validate_enqueue` — `after` can still describe a
self-edge or a dependency cycle. The CLI `add` command's other checks
(`validate_hold_flags`, `validate_apply_gate`, the #2377 open-PR refusal)
all operate on operator-only parameters that have no parameter here, and
#2247 overlap advice orders rather than refuses.

Why `merge` has no working handler yet
-----------------------------------------
:data:`SAFE_TIER_ACTIONS` names `"merge_approved_green"` because the issue's
hard constraints fix its shape now, before any cause is wired to it — but
actually executing a merge (forge calls, branch-protection polling, the
`MergeEvent` bookkeeping `coord/merge_queue.py` does) is #3745's C3, a
separate future issue once a real cause earns auto-action from Phase 0
evidence (`coord.board_doctor_policy`'s empty `ALLOWLIST`, #2268). Until a
handler is registered, :func:`dispatch_sweep_action` for this action raises
:class:`SweepHandlerNotRegisteredError` — a `SweepPermissionError` subclass,
loud and specific, never a silent no-op, but distinguishable from "not
allowed" for a caller that wants to tell the two apart.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, TypedDict, cast

from coord.drive_queue import HOLD_RELEASED

if TYPE_CHECKING:  # pragma: no cover — typing only
    from coord.config import Config
    from coord.drive_queue import QueueEntry
    from coord.models import Board

log = logging.getLogger(__name__)


class SweepPermissionError(PermissionError):
    """Raised when the board-doctor sweep attempts an action outside its
    closed safe-tier allow-list, or one permanently excluded from it."""


class SweepHandlerNotRegisteredError(SweepPermissionError):
    """Raised when *action* is on :data:`SAFE_TIER_ACTIONS` but
    :data:`_HANDLERS` has no entry for it yet.

    A subclass of :class:`SweepPermissionError`, not a sibling: every
    existing caller that catches the permission error still catches this,
    but a caller that cares about the distinction (allowed-in-principle,
    no dispatcher built yet vs. not allowed at all) can match on the more
    specific type.
    """


class SweepWriteNotConfirmedError(RuntimeError):
    """Raised when a board write this module made could not be confirmed by
    re-reading the row afterwards, or came back differing from what was
    declared.

    Deliberately NOT a :class:`SweepPermissionError`: the action was
    permitted, the board just does not agree it happened as asked. A caller
    distinguishing "not allowed" from "allowed but did not land" needs those
    to be separate types, and an unconfirmed write must never be reported
    through the same return value as a confirmed one.
    """


#: The closed set of daemon-internal actions the sweep's auto-act tier may
#: ever perform. Nothing outside this set is reachable from the sweep
#: context — adding to it is a deliberate, reviewable edit to this file,
#: never a runtime decision. Mirrors #3745's hard-constraints list exactly:
#: merge only of an already-approved + CI-green row, drive-queue add/remove,
#: and a CI rerun. ``"drive_queue_requeue"`` is the remove-then-re-add of a
#: single row — a composition of two names already on this list, both
#: re-authorized when it runs, granting nothing neither grants alone; it is
#: a separate name so a cause may be allow-listed for a faithful requeue
#: without also being allow-listed for a bare add or a bare remove.
#: ``"dispatch_fixit_leg"`` (#3768) is the one action here that spawns a
#: worker rather than mutating a drive-queue row directly — it is still
#: safe-tier because the leg it spawns runs at ordinary worker tool-scope
#: (edit/push/git), never `--dangerously-skip-permissions`, and has no path
#: to either name in :data:`PERMANENTLY_FORBIDDEN`.
SAFE_TIER_ACTIONS: frozenset[str] = frozenset(
    {
        "merge_approved_green",
        "drive_queue_add",
        "drive_queue_remove",
        "drive_queue_requeue",
        "ci_rerun",
        "dispatch_fixit_leg",
    }
)

#: Permanently excluded from :data:`SAFE_TIER_ACTIONS` — and checked for
#: explicitly, ahead of plain allow-list membership, so an attempt at one of
#: these gets a sharper, more specific refusal than "not on the list".
#: #3745 established these are not merely risky but *ineffective*: branch
#: protection (`enforce_admins`) blocks a red required check regardless of
#: `--admin`, so this lever belongs only to a human operator's own informed
#: override, never to an unattended sweep. Do not add either name to
#: :data:`SAFE_TIER_ACTIONS` to "unblock" a stuck row — that is precisely
#: the override this model exists to keep out of reach.
PERMANENTLY_FORBIDDEN: frozenset[str] = frozenset(
    {
        "force_merge",
        "override_human_required",
    }
)


def authorize_sweep_action(action: str) -> None:
    """The single gate every daemon-internal sweep action must pass first.

    Raises :class:`SweepPermissionError` naming *action* when it is not on
    :data:`SAFE_TIER_ACTIONS` — a permanently-forbidden action gets its own,
    more specific message — so a blocked action is immediately actionable
    ("add X to SAFE_TIER_ACTIONS") rather than a debugging session. Returns
    ``None`` (does nothing) when *action* is permitted; callers mutate the
    board only after this returns.
    """
    if action in PERMANENTLY_FORBIDDEN:
        raise SweepPermissionError(
            f"board-doctor sweep may never perform {action!r} — permanently "
            "excluded from the safe-tier allow-list (#3745): branch "
            "protection blocks a red required check regardless, so this "
            "lever belongs only to a human operator's own override, never "
            "an unattended sweep."
        )
    if action not in SAFE_TIER_ACTIONS:
        raise SweepPermissionError(
            f"board-doctor sweep is not permitted to perform {action!r} — "
            f"the safe-tier allow-list is {sorted(SAFE_TIER_ACTIONS)!r}. "
            "Extending it is a deliberate edit to "
            "coord.board_doctor_permissions.SAFE_TIER_ACTIONS, not a "
            "runtime decision."
        )


#: The operator-declared policy columns `coord.state.enqueue_drive_queue`
#: replaces on every call, and which this module therefore has to carry
#: through a write rather than let revert to the fleet default. Named here
#: once so :func:`_declaration_of` and :func:`_confirm_write` can never drift
#: apart on which fields count as policy.
_POLICY_FIELDS: tuple[str, ...] = (
    "hold_after",
    "hold_reason",
    "resume_when",
    "hold_scope",
    "max_fix_rounds",
    "no_acceptance",
    "plan_destructive",
)


def _existing_entry(repo_name: str, issue_number: int) -> QueueEntry | None:
    """The queued row for *repo_name*``#``*issue_number*, typed, or ``None``."""
    from coord.drive_queue import QueueEntry  # noqa: PLC0415
    from coord.state import get_drive_queue_entry  # noqa: PLC0415

    row = get_drive_queue_entry(repo_name, issue_number)
    if row is None:
        return None
    return QueueEntry.from_row(row)


def _declaration_of(entry: QueueEntry) -> dict[str, Any]:
    """Every field an `enqueue_drive_queue` call replaces, read back off a
    real row — the full declaration needed to write that row again without
    losing anything the operator declared.

    Only ever built from a row this module itself read; no caller supplies
    one, which is what keeps the policy values here preserved rather than
    caller-controlled.
    """
    declaration: dict[str, Any] = {
        "machine": entry.machine or None,
        "after": [str(a) for a in entry.after],
        "position": entry.position,
    }
    declaration.update({name: getattr(entry, name) for name in _POLICY_FIELDS})
    return declaration


#: The two `hold_state` values a re-declaration of the row reproduces
#: exactly: no gate at all, and a gate that is armed but has not fired yet
#: (`enqueue_drive_queue` writes `armed` whenever `hold_after` is set, and
#: `""` whenever it is not). Any other value is a gate mid-flight — see
#: :func:`_refuse_if_gate_mid_flight`.
_REPRODUCIBLE_HOLD_STATES: frozenset[str] = frozenset({"", "armed"})


def _refuse_if_gate_mid_flight(
    repo_name: str, issue_number: int, entry: QueueEntry
) -> None:
    """Refuse to re-write a row whose #1757 deploy gate has fired or been
    released.

    `enqueue_drive_queue` derives `hold_state` from `hold_after` and zeroes
    `hold_probes`, and takes neither as a parameter — so a row sitting at
    ``fired`` (holding its dependents, waiting for a human to deploy) or
    ``released`` (a human already resumed it) comes back ``armed`` from any
    write, however faithfully the rest of the declaration is preserved. That
    is a gate-state change the sweep has no permission to make in either
    direction, and unlike the other columns it cannot be carried through, so
    the write itself is refused.
    """
    if (
        entry.hold_state in _REPRODUCIBLE_HOLD_STATES
        and entry.hold_probes == 0
    ):
        return
    raise SweepPermissionError(
        f"board-doctor sweep is not permitted to reset a deploy gate's run "
        f"state — {repo_name}#{issue_number} is queued with "
        f"hold_state={entry.hold_state!r} (hold_probes={entry.hold_probes}), "
        "and re-writing the row would re-arm the gate, releasing or "
        "re-imposing a hold a human owns. Leave the row alone until the gate "
        "is resolved (`coord drive-queue resume`)."
    )


def _refuse_if_remove_would_release_gate(
    repo_name: str, issue_number: int, entry: QueueEntry
) -> None:
    """Refuse to remove a row whose #1757 deploy gate is armed or fired but
    not yet released.

    `coord.drive_queue._after_satisfied` treats a pre-req key ABSENT from
    the queue entirely as satisfied — so deleting a row with an unresolved
    ``hold_after`` gate silently releases every ``after=``-dependent row,
    exactly as if a human had already deployed, regardless of whether
    `hold_state` is ``""``, ``"armed"`` or ``"fired"``. Only
    ``hold_state == HOLD_RELEASED`` means a human already resolved the gate
    by hand (`coord drive-queue resume`), which is the one state removal is
    safe in. This is a stricter check than :func:`_refuse_if_gate_mid_flight`
    (which permits re-writing an armed-with-no-probes row because the write
    reproduces that exact state): a remove makes the row vanish rather than
    reproduce anything, so there is no "faithful re-write" exception here.
    """
    if not entry.hold_after or entry.hold_state == HOLD_RELEASED:
        return
    raise SweepPermissionError(
        f"board-doctor sweep is not permitted to remove {repo_name}#"
        f"{issue_number} — its #1757 deploy gate is hold_state="
        f"{entry.hold_state!r}, not yet released, and removing the row "
        "would silently satisfy every `after=`-dependent row's "
        "prerequisite as if a human had already deployed. Leave the row "
        "queued until the gate is resolved (`coord drive-queue resume`)."
    )


def _validate_after(repo_name: str, issue_number: int, after: list[str]) -> None:
    """Refuse a self-edge or dependency cycle before anything is written.

    The one pre-write check that applies to the mechanical parameters this
    module accepts, mirroring `coord.upstream_gaps._block_queue_row`. Raises
    :class:`SweepPermissionError` so a refusal reaches the caller as the
    sweep lacking permission for *this specific* write, with the queue
    error's own explanation attached.
    """
    from coord.drive_queue import QueueEntry, QueueError, validate_enqueue  # noqa: PLC0415
    from coord.state import list_drive_queue  # noqa: PLC0415

    entries = [QueueEntry.from_row(row) for row in list_drive_queue()]
    try:
        validate_enqueue(entries, repo_name, issue_number, after)
    except QueueError as exc:
        raise SweepPermissionError(
            f"sweep refused to enqueue {repo_name}#{issue_number}: {exc}"
        ) from exc


def _confirm_write(
    repo_name: str, issue_number: int, declaration: dict[str, Any]
) -> None:
    """Re-read the row and check it matches *declaration* field by field.

    Raises :class:`SweepWriteNotConfirmedError`, after logging the specific
    mismatches, when the row is absent or any declared field came back
    different — a write whose effect cannot be confirmed must not be
    reported through the same return value as one that landed.

    ``position`` is reported as an error log but is NOT a refusal:
    `coord.state.enqueue_drive_queue` deliberately treats a position move it
    could not complete as self-healing on the next explicit move rather than
    a failed enqueue, and raising here would contradict that while leaving
    the row queued anyway.
    """
    written = _existing_entry(repo_name, issue_number)
    if written is None:
        log.error(
            "board-doctor sweep: write for %s#%s could not be confirmed — no "
            "drive-queue row came back on the re-read",
            repo_name,
            issue_number,
        )
        raise SweepWriteNotConfirmedError(
            f"sweep wrote {repo_name}#{issue_number} to the drive queue but "
            "the row is not there on a re-read — the board did not accept "
            "the write"
        )

    mismatches: list[str] = []
    expected_after = [str(a) for a in declaration["after"]]
    if list(written.after) != expected_after:
        mismatches.append(f"after={list(written.after)!r} (declared {expected_after!r})")
    expected_machine = declaration["machine"] or ""
    if written.machine != expected_machine:
        mismatches.append(
            f"machine={written.machine!r} (declared {expected_machine!r})"
        )
    for name in _POLICY_FIELDS:
        actual = getattr(written, name)
        if actual != declaration[name]:
            mismatches.append(f"{name}={actual!r} (declared {declaration[name]!r})")

    position = declaration["position"]
    if position is not None and written.position != position:
        log.error(
            "board-doctor sweep: drive-queue row %s#%s landed at position %s, "
            "not the declared %s — the slot will catch up on the next "
            "explicit move or renumber",
            repo_name,
            issue_number,
            written.position,
            position,
        )

    if mismatches:
        log.error(
            "board-doctor sweep: drive-queue row %s#%s does not match what "
            "was declared after the write: %s",
            repo_name,
            issue_number,
            "; ".join(mismatches),
        )
        raise SweepWriteNotConfirmedError(
            f"sweep wrote {repo_name}#{issue_number} to the drive queue but "
            f"the row read back differently: {'; '.join(mismatches)}"
        )


def _enqueue_declared(
    repo_name: str, issue_number: int, declaration: dict[str, Any]
) -> int | None:
    """Write *declaration* via `coord.state.enqueue_drive_queue`, then confirm it.

    Calls the same primitive `coord drive-queue add` calls — never the Click
    command, so there is no confirmation prompt or TTY check to bypass: that
    affordance lives in the command body, which this call path never
    reaches.

    Returns the row id the write reported, which is ``None`` only on the
    daemon path when the response carried no ``entry_id`` — the row itself
    is confirmed present and matching by then, or this raises.
    """
    from coord.state import enqueue_drive_queue  # noqa: PLC0415

    entry_id = enqueue_drive_queue(repo_name, issue_number, **declaration)
    _confirm_write(repo_name, issue_number, declaration)
    return entry_id


def sweep_drive_queue_add(
    repo_name: str,
    issue_number: int,
    *,
    machine: str | None = None,
    after: list[str] | None = None,
    position: int | None = None,
) -> int | None:
    """Daemon-internal `drive-queue add`, authorized for the sweep context.

    Only the mechanical "queue this issue" parameters are accepted; the
    operator-declared policy levers (`hold_after`, `resume_when`,
    `hold_scope`, `plan_destructive`, ...) have no parameter here at all.
    For an issue that is ALREADY queued, every one of those is carried
    through from the row itself, because `enqueue_drive_queue` replaces them
    on every call — so this never disarms a deploy gate or clears a
    destructive-plan marker as a side effect of re-adding a row.

    For that same reason an explicit *machine*, *after* or *position* that
    CONTRADICTS the queued row is refused with
    :class:`SweepPermissionError`: re-declaring an operator's queue policy
    is not a safe-tier permission. Omit them to re-add the row exactly as
    declared, or use :func:`sweep_drive_queue_requeue` to take it down and
    put it back. A queued row whose deploy gate has fired or been released
    is refused outright — :func:`_refuse_if_gate_mid_flight`.

    Returns the queued entry's row id. Raises
    :class:`SweepWriteNotConfirmedError` if a re-read cannot confirm the row
    landed as declared — never a bare, unverified pass-through of what the
    write call happened to return.
    """
    authorize_sweep_action("drive_queue_add")

    existing = _existing_entry(repo_name, issue_number)
    if existing is None:
        declaration: dict[str, Any] = {
            "machine": machine,
            "after": [str(a) for a in (after or [])],
            "position": position,
        }
        # The fleet defaults `enqueue_drive_queue` itself applies, spelled
        # out so `_confirm_write` compares the row against real values
        # rather than placeholders.
        declaration.update(
            hold_after=False,
            hold_reason="",
            resume_when="",
            hold_scope="entry",
            max_fix_rounds=None,
            no_acceptance=False,
            plan_destructive=False,
        )
    else:
        _refuse_if_gate_mid_flight(repo_name, issue_number, existing)
        declaration = _declaration_of(existing)
        conflicts: list[str] = []
        if machine is not None and machine != (existing.machine or None):
            conflicts.append(
                f"machine={machine!r} (row declares {existing.machine or None!r})"
            )
        if after is not None and [str(a) for a in after] != [
            str(a) for a in existing.after
        ]:
            conflicts.append(
                f"after={[str(a) for a in after]!r} "
                f"(row declares {[str(a) for a in existing.after]!r})"
            )
        if position is not None and position != existing.position:
            conflicts.append(
                f"position={position!r} (row declares {existing.position!r})"
            )
        if conflicts:
            raise SweepPermissionError(
                f"board-doctor sweep is not permitted to re-declare an "
                f"existing drive-queue row's operator policy — "
                f"{repo_name}#{issue_number} is already queued and this add "
                f"would change: {'; '.join(conflicts)}. The sweep may re-add "
                "a row exactly as the operator declared it (omit the "
                "conflicting argument), never on different terms."
            )
        # An explicit position equal to the row's own slot is a no-op move;
        # leaving it out keeps the write a pure in-place refresh.
        declaration["position"] = None

    _validate_after(repo_name, issue_number, declaration["after"])
    return _enqueue_declared(repo_name, issue_number, declaration)


class DriveQueueRemoval(TypedDict):
    """:func:`sweep_drive_queue_remove`'s return shape — identical to
    `coord.state.dequeue_drive_queue`'s, typed."""

    removed: bool
    driver_ok: bool
    driver_session: str | None
    driver_detail: str | None


def _dequeue_and_log(repo_name: str, issue_number: int) -> DriveQueueRemoval:
    """The actual removal, shared by :func:`sweep_drive_queue_remove` and
    :func:`sweep_drive_queue_requeue`'s internal remove-then-re-add.

    Does NOT call :func:`_refuse_if_remove_would_release_gate` — callers
    decide whether that guard applies. `sweep_drive_queue_requeue` calls
    this directly rather than through :func:`sweep_drive_queue_remove`
    because it has already confirmed (via :func:`_refuse_if_gate_mid_flight`)
    that the row's gate is reproducible and is about to put the exact same
    declaration straight back — a remove that is immediately followed by a
    faithful re-add never leaves any ``after=``-dependent row looking at a
    gate that is actually gone.
    """
    from coord.state import dequeue_drive_queue  # noqa: PLC0415

    result = dequeue_drive_queue(repo_name, issue_number)
    if result.get("removed") and not result.get("driver_ok", True):
        log.warning(
            "board-doctor sweep: drive-queue remove for %s#%s orphaned a "
            "live driver session (%s) that could NOT be confirmed stopped "
            "(%s) — it may need to be killed by hand",
            repo_name,
            issue_number,
            result.get("driver_session"),
            result.get("driver_detail") or "unknown",
        )
    return cast(DriveQueueRemoval, result)


def sweep_drive_queue_remove(repo_name: str, issue_number: int) -> DriveQueueRemoval:
    """Daemon-internal `drive-queue remove`, authorized for the sweep context.

    Calls `coord.state.dequeue_drive_queue` directly — the same primitive
    `coord drive-queue remove` calls, including owning the live driver
    session it orphans (#3282) — never the Click command. A live session
    that could not be confirmed stopped (`driver_ok=False`) is logged loudly
    here rather than left for the caller to notice or not: an unattended
    sweep has nobody else reading its output.

    Refuses outright, via :func:`_refuse_if_remove_would_release_gate`, when
    the row's #1757 deploy gate is armed or fired but not yet released —
    deleting such a row would silently satisfy every ``after=``-dependent
    row's prerequisite, as `coord.drive_queue._after_satisfied` treats an
    absent pre-req as already met. A row with no `hold_after` gate at all,
    or one a human already released, removes normally.
    """
    authorize_sweep_action("drive_queue_remove")

    entry = _existing_entry(repo_name, issue_number)
    if entry is not None:
        _refuse_if_remove_would_release_gate(repo_name, issue_number, entry)

    return _dequeue_and_log(repo_name, issue_number)


class DriveQueueRequeue(TypedDict):
    """:func:`sweep_drive_queue_requeue`'s return shape.

    ``requeued`` is the one answer to "did the row come back"; ``detail``
    says why not when it is ``False``, so a no-op is never indistinguishable
    from a success to an unattended caller.
    """

    requeued: bool
    entry_id: int | None
    removal: DriveQueueRemoval | None
    detail: str


def sweep_drive_queue_requeue(repo_name: str, issue_number: int) -> DriveQueueRequeue:
    """Take a queued row down and put it back exactly as declared.

    The sweep's safe-tier "requeue a stuck-but-approved row" action, and the
    only faithful way to express it: `coord.state.dequeue_drive_queue`
    reports only whether a row went away, so by the time a bare
    :func:`sweep_drive_queue_add` ran the row's `machine`, `after`, position
    and every #1757/#2186/#2604/#2589/#3236 policy column would already be
    gone. This snapshots the full declaration FIRST, validates the re-add
    before removing anything, and restores the snapshot at the same slot.

    Both halves re-authorize through :func:`authorize_sweep_action` as
    themselves, so this grants nothing the two names do not already grant.

    A row whose deploy gate has fired or been released is refused with
    :class:`SweepPermissionError` before anything is removed, for the reason
    :func:`_refuse_if_gate_mid_flight` gives: that run state cannot be put
    back.

    Returns a :class:`DriveQueueRequeue`; an issue that is not queued is not
    an error (nothing to requeue), it reports ``requeued=False``. Raises
    :class:`SweepWriteNotConfirmedError` if the row cannot be confirmed back
    in place, after logging loudly that the queue is now short a row.
    """
    authorize_sweep_action("drive_queue_requeue")

    entry = _existing_entry(repo_name, issue_number)
    if entry is None:
        return DriveQueueRequeue(
            requeued=False,
            entry_id=None,
            removal=None,
            detail=(
                f"{repo_name}#{issue_number} is not in the drive queue — "
                "nothing to requeue"
            ),
        )

    _refuse_if_gate_mid_flight(repo_name, issue_number, entry)
    declaration = _declaration_of(entry)
    # Validated against the queue as it stands, BEFORE the remove: a re-add
    # refused for a cycle must not be able to leave the operator's row off
    # the queue entirely.
    _validate_after(repo_name, issue_number, declaration["after"])

    authorize_sweep_action("drive_queue_remove")
    removal = _dequeue_and_log(repo_name, issue_number)
    if not removal.get("removed"):
        return DriveQueueRequeue(
            requeued=False,
            entry_id=None,
            removal=removal,
            detail=(
                f"{repo_name}#{issue_number} was not removed, so it was not "
                "re-added — the queue is unchanged"
            ),
        )

    try:
        entry_id = _enqueue_declared(repo_name, issue_number, declaration)
    except Exception:
        log.error(
            "board-doctor sweep: removed drive-queue row %s#%s but could NOT "
            "put it back (declaration: %r) — the queue is short a row and "
            "needs `coord drive-queue add` by hand",
            repo_name,
            issue_number,
            declaration,
        )
        raise

    return DriveQueueRequeue(
        requeued=True,
        entry_id=entry_id,
        removal=removal,
        detail=(
            f"{repo_name}#{issue_number} requeued at position "
            f"{declaration['position']} with its declaration preserved"
        ),
    )


def _fixit_leg_briefing(*, cause: str, evidence: tuple[str, ...]) -> str:
    """The briefing text handed to the fix-it leg — the sweep's *cause* and
    *evidence* for why this row is blocked, translated into the same
    worker-facing shape every other fix dispatch (`coord.ci_fix`,
    `coord.auto_loop._dispatch_fix_for_review`) uses, so the leg reads this
    like any other fix round rather than a special case.
    """
    lines = [
        "The board-doctor sweep (#3758) dispatched this fix-it leg "
        "automatically because this issue's row is stuck.",
        "",
        f"Cause: {cause}",
    ]
    if evidence:
        lines.append("Evidence:")
        lines.extend(f"- {item}" for item in evidence)
    lines.extend([
        "",
        "You will NOT use `--dangerously-skip-permissions`, `gh`, "
        "`--force-merge`, or `--override-human-required` — this leg runs "
        "at ordinary worker tool-scope (edit/push/git) only, same as any "
        "other fix round.",
    ])
    return "\n".join(lines)


class FixitLegDispatch(TypedDict):
    """:func:`sweep_dispatch_fixit_leg`'s return shape.

    ``dispatched`` is the one answer to "did a new fix-it leg actually
    start" — set from `_dispatch_fix`'s own confirmed return value, never
    from the mere absence of an exception (#2096): a dispatch that cannot
    be confirmed started reads ``False`` here, same as a lock-blocked or
    no-work-found no-op. ``detail`` explains a ``False`` result so a caller
    can tell the three apart without inspecting exceptions.
    """

    dispatched: bool
    assignment_id: str | None
    machine: str | None
    detail: str


def sweep_dispatch_fixit_leg(
    repo_name: str,
    issue_number: int,
    board: "Board",
    config: "Config",
    *,
    cause: str,
    evidence: tuple[str, ...] = (),
    http_client: Any | None = None,
) -> FixitLegDispatch:
    """Daemon-internal "dispatch one autonomous fix-it leg", authorized for
    the sweep context (#3768).

    Re-dispatches a routed worker/ci-fix leg via `coord.auto_loop.
    _dispatch_fix` — the same primitive `coord.ci_fix.dispatch_ci_fix` and
    the headless review-bounce path call, never a bespoke HTTP POST built
    here — with a fresh fix-round number (`coord.auto_loop.
    next_fix_iteration`) and a briefing carrying the sweep's *cause* and
    *evidence* (:func:`_fixit_leg_briefing`). The leg it spawns is scoped to
    ordinary worker tool-scope (edit/push/git); nothing here can reach
    `--dangerously-skip-permissions` or either name in
    :data:`PERMANENTLY_FORBIDDEN`.

    In-flight lock: no-ops, rather than dispatching a second leg, when
    *repo_name*/*issue_number* already has a ``running`` or ``pending``
    work-like assignment on *board*, OR a live `coord drive` tmux session
    (`coord.drive.list_drive_sessions`) — one leg per blocked item per
    sweep, idempotent if the sweep runs again while a leg is live.

    Returns a :class:`FixitLegDispatch` with ``dispatched=False`` — never
    raises — for: no originating work-like assignment found for this
    issue, a leg already in flight, or an underlying `_dispatch_fix` call
    that declined (no capable machine, unreachable agent, …). Only an
    observed, confirmed `Assignment` back from `_dispatch_fix` is reported
    as ``dispatched=True``.
    """
    authorize_sweep_action("dispatch_fixit_leg")

    from coord.gates import _select_winning_work_assignment, assignments_for_issue  # noqa: PLC0415
    from coord.models import WORK_LIKE_TYPES  # noqa: PLC0415

    work_like = [
        a
        for a in assignments_for_issue(board, repo_name, issue_number)
        if a.type in WORK_LIKE_TYPES
    ]
    if not work_like:
        return FixitLegDispatch(
            dispatched=False,
            assignment_id=None,
            machine=None,
            detail=(
                f"{repo_name}#{issue_number} has no originating work "
                "assignment on the board — nothing to dispatch a fix-it "
                "leg against"
            ),
        )

    if any(a.status in ("running", "pending") for a in work_like):
        return FixitLegDispatch(
            dispatched=False,
            assignment_id=None,
            machine=None,
            detail=(
                f"{repo_name}#{issue_number} already has a running or "
                "pending assignment — a fix-it leg is already in flight, "
                "not dispatching a second one"
            ),
        )

    from coord.drive import list_drive_sessions  # noqa: PLC0415

    live_sessions = [
        s
        for s in list_drive_sessions()
        if s.get("repo") == repo_name and s.get("issue") == issue_number
    ]
    if live_sessions:
        return FixitLegDispatch(
            dispatched=False,
            assignment_id=None,
            machine=None,
            detail=(
                f"{repo_name}#{issue_number} has a live `coord drive` "
                f"session ({live_sessions[0].get('session_name')}) — a "
                "fix-it leg is already in flight, not dispatching a second "
                "one"
            ),
        )

    work = _select_winning_work_assignment(work_like)

    from coord.auto_loop import _dispatch_fix, next_fix_iteration  # noqa: PLC0415

    iteration = next_fix_iteration(board, work)
    briefing = _fixit_leg_briefing(cause=cause, evidence=evidence)

    fix = _dispatch_fix(
        work, briefing, board, config, iteration, http_client=http_client,
    )
    if fix is None:
        return FixitLegDispatch(
            dispatched=False,
            assignment_id=None,
            machine=None,
            detail=(
                f"{repo_name}#{issue_number} fix-it leg dispatch could not "
                "be confirmed started — no capable machine, an unreachable "
                "agent, or a declined dispatch"
            ),
        )

    from coord.audit import record_audit  # noqa: PLC0415

    record_audit(
        tier="operational",
        category="board_doctor",
        event_type="fixit_leg_dispatched",
        actor="daemon",
        summary=(
            f"board-doctor dispatched a fix-it leg for {repo_name}#"
            f"{issue_number} (cause={cause}) -> {fix.machine_name}"
        ),
        repo=repo_name,
        issue=issue_number,
        assignment_id=fix.assignment_id,
        machine=fix.machine_name,
        details={"cause": cause, "evidence": list(evidence)},
    )

    return FixitLegDispatch(
        dispatched=True,
        assignment_id=fix.assignment_id,
        machine=fix.machine_name,
        detail=(
            f"{repo_name}#{issue_number} fix-it leg dispatched to "
            f"{fix.machine_name} (iteration {iteration})"
        ),
    )


#: action name -> the daemon-internal handler that actually performs it.
#: Looked up by :func:`dispatch_sweep_action` only after
#: :func:`authorize_sweep_action` has already cleared the action — an
#: entry missing here for a name that IS on :data:`SAFE_TIER_ACTIONS`
#: (today: ``"merge_approved_green"``, ``"ci_rerun"``) means the permission
#: exists but no C3 dispatcher has been built yet, not that the action is
#: unsafe.
_HANDLERS: dict[str, Callable[..., Any]] = {
    "drive_queue_add": sweep_drive_queue_add,
    "drive_queue_remove": sweep_drive_queue_remove,
    "drive_queue_requeue": sweep_drive_queue_requeue,
    "dispatch_fixit_leg": sweep_dispatch_fixit_leg,
}


def dispatch_sweep_action(action: str, *args: Any, **kwargs: Any) -> Any:
    """Authorize *action*, then run its registered daemon-internal handler.

    The one call C3 dispatchers make instead of reaching for `coord merge`
    / `coord drive-queue` directly. Raises :class:`SweepPermissionError`
    (from :func:`authorize_sweep_action`) before any handler runs, and
    before any board mutation, when *action* is not on the safe-tier
    allow-list — including every permanently-forbidden name. Raises the
    more specific :class:`SweepHandlerNotRegisteredError` when *action* IS
    allow-listed but :data:`_HANDLERS` has no entry for it yet.
    """
    authorize_sweep_action(action)
    handler = _HANDLERS.get(action)
    if handler is None:
        raise SweepHandlerNotRegisteredError(
            f"{action!r} is on the safe-tier allow-list but has no "
            "registered daemon handler yet — add one to "
            "coord.board_doctor_permissions._HANDLERS before dispatching it "
            "(#3745's C3)."
        )
    return handler(*args, **kwargs)
