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

Why daemon-internal (Route 1), not an agent leg (Route 2)
-----------------------------------------------------------
Every safe-tier action here is a mechanical board mutation with a crisp,
re-checkable precondition (row is approved + CI green; row key parses) —
no judgment call a `claude -p` leg would earn its cost on. Route 1 (direct,
in-process `coord` primitives, same shape as #1478's daemon-internal board
writes) is therefore the only route this module builds. A future cause that
genuinely needs LLM judgment is free to add an agent leg scoped with
`--allowedTools` to exactly its own safe-tier subcommands and never
`--dangerously-skip-permissions` — but that leg would still call
:func:`authorize_sweep_action` for the same reason a direct dispatcher does:
this module is the boundary, not the CLI layer.

Why no context object or contextvar
------------------------------------
There is no "we're inside the sweep" marker threaded through anywhere in
this module, and there does not need to be: the primitives here
(:func:`sweep_drive_queue_add`, :func:`sweep_drive_queue_remove`) call
`coord.state`'s own `enqueue_drive_queue` / `dequeue_drive_queue` directly —
the same functions `coord drive-queue add`/`remove` call — never the Click
command callbacks that carry operator-session affordances. There is nothing
for a contextvar to disable, because the human-operator-only surface
(confirmation prompts, `--force-merge`, `--override-human-required`) lives
entirely in `coord/commands/drive_queue.py`'s and `coord/commands/merge.py`'s
command bodies, which this module's call paths never reach. "Present but not
consulted" is a property of WHICH function gets called, not a flag checked
at runtime.

Why `sweep_drive_queue_add` takes only `machine`/`after`/`position`
----------------------------------------------------------------------
`coord.state.enqueue_drive_queue` also accepts `hold_after`, `hold_reason`,
`resume_when`, `hold_scope`, `max_fix_rounds`, `no_acceptance` and
`plan_destructive` — every one of them an operator-declared policy lever
(deploy gates, fleet-wide holds, destructive-plan bookkeeping), not a
mechanical "queue this issue" fact. Accepting them here would let a sweep
action set `hold_scope="fleet"` or `plan_destructive` by construction, which
is exactly the kind of blanket permission :data:`SAFE_TIER_ACTIONS` exists to
rule out. Narrowing the signature closes that off structurally rather than
relying on a runtime check: there is no parameter for a cause dispatcher to
pass those values through even if it wanted to. The one remaining
pre-write check that still applies — `after` can still describe a self-edge
or a dependency cycle — is :func:`coord.drive_queue.validate_enqueue`, called
here before the write, mirroring the one pre-write check
`coord.upstream_gaps._block_queue_row` (the other daemon-internal, non-CLI
caller of `enqueue_drive_queue`) makes. The CLI `add` command additionally
runs `validate_hold_flags`, `validate_apply_gate`, a #2377 open-PR refusal
and #2247 overlap-ordering advice — all either operate on the operator-only
parameters this module never accepts, or (#2247) are advisory ordering, not
a safety check (it "orders, never refuses" per this repo's own design
rules) — so neither applies to a safe-tier sweep add.

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
from typing import Any, Callable, TypedDict, cast

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


#: The closed set of daemon-internal actions the sweep's auto-act tier may
#: ever perform. Nothing outside this set is reachable from the sweep
#: context — adding to it is a deliberate, reviewable edit to this file,
#: never a runtime decision. Mirrors #3745's hard-constraints list exactly:
#: merge only of an already-approved + CI-green row, drive-queue add/remove,
#: and a CI rerun.
SAFE_TIER_ACTIONS: frozenset[str] = frozenset(
    {
        "merge_approved_green",
        "drive_queue_add",
        "drive_queue_remove",
        "ci_rerun",
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


def sweep_drive_queue_add(
    repo_name: str,
    issue_number: int,
    *,
    machine: str | None = None,
    after: list[str] | None = None,
    position: int | None = None,
) -> int | None:
    """Daemon-internal `drive-queue add`, authorized for the sweep context.

    Only the mechanical "queue this issue" parameters are accepted — see
    the module docstring's "Why `sweep_drive_queue_add` takes only
    `machine`/`after`/`position`" section for why the operator-only policy
    levers (`hold_after`, `resume_when`, `hold_scope`, `plan_destructive`,
    ...) have no parameter here at all, by construction.

    Validates `after` for a self-edge or dependency cycle via
    `coord.drive_queue.validate_enqueue` before writing — the one pre-write
    check that still applies to the parameters this accepts, mirroring
    `coord.upstream_gaps._block_queue_row`. Calls
    `coord.state.enqueue_drive_queue` directly — the same primitive
    `coord drive-queue add` calls — never the Click command, so there is no
    confirmation prompt or TTY check to bypass: that affordance lives in the
    command body, which this call path never reaches.

    Returns the queued entry's row id, or ``None`` if the row cannot be
    confirmed queued by a re-read taken after the write — never a bare,
    unverified pass-through of what the write call happened to return.
    """
    authorize_sweep_action("drive_queue_add")
    from coord.drive_queue import QueueEntry, QueueError, validate_enqueue  # noqa: PLC0415
    from coord.state import (  # noqa: PLC0415
        enqueue_drive_queue,
        get_drive_queue_entry,
        list_drive_queue,
    )

    after_list = [str(a) for a in (after or [])]
    entries = [QueueEntry.from_row(row) for row in list_drive_queue()]
    try:
        validate_enqueue(entries, repo_name, issue_number, after_list)
    except QueueError as exc:
        raise SweepPermissionError(
            f"sweep refused to enqueue {repo_name}#{issue_number}: {exc}"
        ) from exc

    entry_id = enqueue_drive_queue(
        repo_name,
        issue_number,
        machine=machine,
        after=after_list,
        position=position,
    )
    if get_drive_queue_entry(repo_name, issue_number) is None:
        return None
    return entry_id


class DriveQueueRemoval(TypedDict):
    """:func:`sweep_drive_queue_remove`'s return shape — identical to
    `coord.state.dequeue_drive_queue`'s, typed."""

    removed: bool
    driver_ok: bool
    driver_session: str | None
    driver_detail: str | None


def sweep_drive_queue_remove(repo_name: str, issue_number: int) -> DriveQueueRemoval:
    """Daemon-internal `drive-queue remove`, authorized for the sweep context.

    Calls `coord.state.dequeue_drive_queue` directly — the same primitive
    `coord drive-queue remove` calls, including owning the live driver
    session it orphans (#3282) — never the Click command. A live session
    that could not be confirmed stopped (`driver_ok=False`) is logged loudly
    here rather than left for the caller to notice or not: an unattended
    sweep has nobody else reading its output.
    """
    authorize_sweep_action("drive_queue_remove")
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
