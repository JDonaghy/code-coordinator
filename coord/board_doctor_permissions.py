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
`--allowedTools` to exactly its own safe-tier subcommands (mirroring commits
`8f314f82`/`562d3522`) and never `--dangerously-skip-permissions` — but that
leg would still call :func:`authorize_sweep_action` for the same reason a
direct dispatcher does: this module is the boundary, not the CLI layer.

Why no context object or contextvar
------------------------------------
An earlier sketch threaded a "we're inside the sweep" marker through so the
daemon-internal primitives could tell sweep-context calls apart from
operator-session ones. That is backwards: the primitives in this module
(:func:`sweep_drive_queue_add`, :func:`sweep_drive_queue_remove`) call
`coord.state`'s own `enqueue_drive_queue` / `dequeue_drive_queue` directly —
the same functions `coord drive-queue add`/`remove` call — never the Click
command callbacks that carry operator-session affordances. There is nothing
for a contextvar to disable, because the human-operator-only surface
(confirmation prompts, `--force-merge`, `--override-human-required`) lives
entirely in `coord/commands/merge.py`'s command bodies, which this module's
call paths never reach. "Present but not consulted" is a property of WHICH
function gets called, not a flag checked at runtime.

Why `merge` has no working handler yet
-----------------------------------------
:data:`SAFE_TIER_ACTIONS` names `"merge_approved_green"` because the issue's
hard constraints fix its shape now, before any cause is wired to it — but
actually executing a merge (forge calls, branch-protection polling, the
`MergeEvent` bookkeeping `coord/merge_queue.py` does) is #3745's C3, a
separate future issue once a real cause earns auto-action from Phase 0
evidence (`coord.board_doctor_policy`'s empty `ALLOWLIST`, #2268). Until a
handler is registered, :func:`dispatch_sweep_action` for this action raises
the same `SweepPermissionError` family a missing allow-list entry would —
loud and specific, never a silent no-op.
"""

from __future__ import annotations

from typing import Any, Callable


class SweepPermissionError(PermissionError):
    """Raised when the board-doctor sweep attempts an action outside its
    closed safe-tier allow-list, or one permanently excluded from it."""


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
    repo_name: str, issue_number: int, **enqueue_kwargs: Any
) -> int | None:
    """Daemon-internal `drive-queue add`, authorized for the sweep context.

    Calls `coord.state.enqueue_drive_queue` directly — the same primitive
    `coord drive-queue add` calls — never the Click command, so there is no
    confirmation prompt or TTY check to bypass: that affordance lives in the
    command body, which this call path never reaches.
    """
    authorize_sweep_action("drive_queue_add")
    from coord.state import enqueue_drive_queue  # noqa: PLC0415

    return enqueue_drive_queue(repo_name, issue_number, **enqueue_kwargs)


def sweep_drive_queue_remove(repo_name: str, issue_number: int) -> dict:
    """Daemon-internal `drive-queue remove`, authorized for the sweep context.

    Calls `coord.state.dequeue_drive_queue` directly — the same primitive
    `coord drive-queue remove` calls, including owning the live driver
    session it orphans (#3282) — never the Click command.
    """
    authorize_sweep_action("drive_queue_remove")
    from coord.state import dequeue_drive_queue  # noqa: PLC0415

    return dequeue_drive_queue(repo_name, issue_number)


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
    allow-list — including every permanently-forbidden name.
    """
    authorize_sweep_action(action)
    handler = _HANDLERS.get(action)
    if handler is None:
        raise SweepPermissionError(
            f"{action!r} is on the safe-tier allow-list but has no "
            "registered daemon handler yet — add one to "
            "coord.board_doctor_permissions._HANDLERS before dispatching it "
            "(#3745's C3)."
        )
    return handler(*args, **kwargs)
