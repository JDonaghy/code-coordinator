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

Why `dispatch_sweep_action("merge", ...)` still has no working handler
-------------------------------------------------------------------------
:data:`SAFE_TIER_ACTIONS` names `"merge"` — matching the `"merge"` verb
`investigator_merge` is authorized under — before a real C3 `_HANDLERS`
dispatcher for it exists.
`CAUSE_MERGE_PENDING_STUCK` is in fact already wired to a working merge
(`coord.board_doctor_policy._merge_pending_stuck_row`), but through the
#3881 investigator path (`dispatch_investigator_action("merge", ...)` →
:func:`investigator_merge`), never through :func:`dispatch_sweep_action`'s
own `_HANDLERS` registry — the two are independent routing tables for the
SAME permission name. A caller that reaches `dispatch_sweep_action("merge",
...)` directly still raises :class:`SweepHandlerNotRegisteredError` — a
`SweepPermissionError` subclass, loud and specific, never a silent no-op,
but distinguishable from "not allowed" for a caller that wants to tell the
two apart — until a real `_HANDLERS["merge"]` entry is built.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, TypedDict, cast

from coord.drive_queue import HOLD_RELEASED

if TYPE_CHECKING:  # pragma: no cover — typing only
    from coord.config import Config
    from coord.drive_queue import QueueEntry
    from coord.merge_queue import QueuedMerge
    from coord.models import Assignment, Board
    from coord.review import ProposedFix, ReviewFindings

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
#: to either name in :data:`PERMANENTLY_FORBIDDEN`. ``"smoke_requeue"``
#: (#2244) clears a smoke leg's `test_state`/`test_reason` so it re-enters
#: the ordinary dispatch scan — it never mutates the drive queue or spawns
#: anything, so it is strictly narrower than any other name on this list.
#: ``"dispatch_finisher_leg"`` spawns a leg the same way
#: ``"dispatch_fixit_leg"`` does, with the same tool-scope bound — but a
#: finisher leg may ALSO, on its own authority, overwrite a review
#: assignment's verdict fields (``review_verdict``, ``verdict_source``, ...)
#: and advance it to the merge queue. That bounded self-bless is still
#: safe-tier because it is gated, every time, on the leg's own diff (taken
#: over the already-reviewed SHA) passing
#: :func:`coord.review.classify_finding_nit_class` — never on the leg's own
#: say-so — and it has no path to ``force_merge`` or
#: ``override_human_required``: it only ever calls
#: :func:`coord.auto_loop.propagate_review_verdict`, the same bookkeeping
#: hand-off to the ordinary merge queue a human-approved review uses, never
#: a merge-execution primitive of its own.
#: #3881 adds five names for the daemon-host investigator wrapper
#: (:func:`authorize_investigator_action`) — a coordinator-role session
#: that decides, never a routed worker, acting only through named handlers
#: here: ``"raise_budget"`` (a row's ONE lifetime fix-round-ceiling bump),
#: ``"bounce"`` (re-run the auto-loop's review-bounce fix dispatch),
#: ``"restage"`` (re-dispatch a missing/unreadable Test or Review stage),
#: ``"clear_latch"`` (re-test a stale CONFLICT/HUMAN_REQUIRED merge-queue
#: latch against live mergeability), and ``"mark_done"`` (reconcile a row
#: whose PR merged out-of-band). The investigator's ``"requeue"`` and
#: ``"integration_fix"`` verbs deliberately reuse ``"drive_queue_requeue"``
#: and ``"dispatch_fixit_leg"`` above rather than minting new names —
#: :func:`authorize_investigator_action`'s alias table, not a second grant,
#: so the sweep and the investigator can never disagree about whether one
#: of those two is allowed (#2096, one question one answer). ``"merge"``
#: needs no such alias (#3902, dropping the earlier ``"merge_approved_
#: green"`` name): the investigator verb and the permission name are now
#: the identical string, which is also the audit event's own verb
#: (``investigator_merge``/``investigator_merge_refused``) and
#: `coord.board_doctor_policy.ALLOWLIST`'s action name for
#: `CAUSE_MERGE_PENDING_STUCK` — one name, not three independently
#: spelled ones for the same underlying action.
#: #3887 adds ``"conflict_fix_redispatch"``: re-dispatch the ordinary
#: mechanical conflict-fix for a `HUMAN_REQUIRED` row whose last attempt
#: completed clean but never actually moved the branch (`coord.merge_queue.
#: CONFLICT_FIX_HEAD_UNMOVED_SIGNAL`, #3871's own detection). It calls the
#: SAME `coord.conflict_fix.dispatch_conflict_fix` primitive #3871's own
#: completion-time redispatch does, with the SAME `after_head_unmoved_
#: fix_assignment_id` retry-cap carve-out, so it never spends a budget
#: #3871 did not already intend this row to have — this is the periodic
#: retry for when that one-shot, synchronous redispatch had no capable
#: machine right then, never a second or larger budget.
#: #3918 adds ``"conflict_fix_dispatch"``: the FIRST mechanical conflict-fix
#: attempt for a merge-queue entry GitHub already reports ``CONFLICT`` on,
#: standing (no in-pass `merge_queue.process()` tick raised it just now) —
#: the shape the reactive auto-drain tick's own conflict dispatch never
#: revisits. Same underlying `dispatch_conflict_fix` primitive as
#: ``"conflict_fix_redispatch"``, just without that one's head-unmoved
#: retry-cap carve-out, since this is the ordinary first attempt.
SAFE_TIER_ACTIONS: frozenset[str] = frozenset(
    {
        "merge",
        "drive_queue_add",
        "drive_queue_remove",
        "drive_queue_requeue",
        "ci_rerun",
        "dispatch_fixit_leg",
        "dispatch_finisher_leg",
        "conflict_fix_redispatch",
        "conflict_fix_dispatch",
        "smoke_requeue",
        "raise_budget",
        "bounce",
        "restage",
        "clear_latch",
        "mark_done",
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


#: #3881: the closed, operator-facing action vocabulary a daemon-host
#: investigator session — a coordinator-role `claude -p` reading with
#: `coord gates`/`coord log`/`coord audit`/`drive-queue list`/`block-log`/
#: `git`/read-only `gh`, never a routed worker — may invoke via the CLI
#: wrapper (`coord drive-queue doctor-act`). Every name here is also
#: authorized by :func:`authorize_sweep_action` (:func:`authorize_
#: investigator_action` re-derives the underlying name via
#: :data:`_INVESTIGATOR_ACTION_ALIAS` and calls it) — the investigator
#: never gets a permission the periodic sweep itself could not also reach,
#: it is only ever handed a NARROWER, named subset: :data:`SAFE_TIER_
#: ACTIONS` also contains ``"drive_queue_add"``/``"dispatch_finisher_leg"``/
#: ``"smoke_requeue"``/``"ci_rerun"``, none of which have an investigator
#: verb, because those are reserved for the sweep's own per-cause
#: dispatchers.
INVESTIGATOR_ACTIONS: frozenset[str] = frozenset(
    {
        "requeue",
        "raise_budget",
        "bounce",
        "integration_fix",
        "restage",
        "clear_latch",
        "merge",
        "mark_done",
    }
)

#: Investigator verbs that are a thin rename of an existing safe-tier name
#: rather than a new grant — see :data:`SAFE_TIER_ACTIONS`'s own #3881
#: comment. Looked up by :func:`authorize_investigator_action`; a verb
#: absent here authorizes under its own, identical name.
_INVESTIGATOR_ACTION_ALIAS: dict[str, str] = {
    "requeue": "drive_queue_requeue",
    "integration_fix": "dispatch_fixit_leg",
}

#: #3881 design point 4: permanently forbidden to the investigator, on top
#: of :data:`PERMANENTLY_FORBIDDEN` (which already covers ``force_merge``/
#: ``override_human_required``). None of these is a mechanical board
#: mutation this module could even express a handler for — they are listed
#: explicitly anyway so a caller that requests one gets a sharp, named
#: refusal instead of a generic "not on INVESTIGATOR_ACTIONS" one, and so a
#: future maintainer can never "add a handler" for them by mistake: editing
#: an issue's scope/title, closing/reopening it, touching credentials or
#: `coordinator.yml`, and any `gh` write that does not go through this
#: wrapper are an operator's own informed call, never an unattended
#: investigator's.
INVESTIGATOR_FORBIDDEN_EXTRA: frozenset[str] = frozenset(
    {
        "edit_sealed_suite",
        "close_issue",
        "reopen_issue",
        "retitle_issue",
        "rescope_issue",
        "edit_credentials",
        "edit_coordinator_yml",
        "gh_write",
    }
)


def authorize_investigator_action(action: str) -> None:
    """The single gate every #3881 investigator action must pass first.

    Raises :class:`SweepPermissionError` for *action* not on
    :data:`INVESTIGATOR_ACTIONS`, or explicitly on
    :data:`INVESTIGATOR_FORBIDDEN_EXTRA`/:data:`PERMANENTLY_FORBIDDEN` —
    the sharper, named refusal in that order. An *action* that passes both
    checks is then re-authorized under its real :data:`SAFE_TIER_ACTIONS`
    name via :func:`authorize_sweep_action` (:data:`_INVESTIGATOR_ACTION_
    ALIAS`), so this function can never grant something that gate alone
    would refuse — one question, one answer, asked twice under two names
    for the same underlying mutation only when the alias table says so.
    """
    if action in PERMANENTLY_FORBIDDEN or action in INVESTIGATOR_FORBIDDEN_EXTRA:
        raise SweepPermissionError(
            f"board-doctor investigator may never perform {action!r} — "
            "#3881 permanently excludes it from the investigator's closed "
            "action set: that lever belongs only to a human operator's own "
            "informed call, never an unattended investigator session."
        )
    if action not in INVESTIGATOR_ACTIONS:
        raise SweepPermissionError(
            f"board-doctor investigator is not permitted to perform "
            f"{action!r} — its closed action set is "
            f"{sorted(INVESTIGATOR_ACTIONS)!r}. Extending it is a "
            "deliberate edit to coord.board_doctor_permissions."
            "INVESTIGATOR_ACTIONS, never a runtime decision."
        )
    authorize_sweep_action(_INVESTIGATOR_ACTION_ALIAS.get(action, action))


class InvestigatorActionResult(TypedDict):
    """Every #3881 investigator handler's return shape.

    ``acted`` is the one answer to "did this call actually change
    anything" — set from the underlying primitive's own confirmed result,
    never from the mere absence of an exception (#2096), so a refused
    precondition and a confirmed action are never reported through the
    same truthy value. ``detail`` explains an ``acted=False`` result (a
    precondition that did not hold, or an underlying primitive's own
    no-op) so a caller — or #3875's outcome tracking, reading the audit
    row this always writes — never has to infer why nothing happened.
    """

    acted: bool
    action: str
    repo: str
    issue: int
    detail: str


def _investigator_result(
    action: str,
    repo_name: str,
    issue_number: int,
    *,
    acted: bool,
    detail: str,
    reason: str,
    actor: str = "board_doctor_investigator",
) -> InvestigatorActionResult:
    """Build an :class:`InvestigatorActionResult` and audit it — every
    #3881 investigator handler's only way to return, so neither a refusal
    nor a confirmed action can leave this module without a corresponding
    audit row (actor, row, action, reason) for #3875's outcome tracking to
    read. Never raises — audit writes are best-effort
    (:func:`coord.audit.record_audit`'s own contract).

    *actor* (#3887) defaults to the human-attended ``coord drive-queue
    doctor-act`` CLI's own attribution — every pre-#3887 call site. The
    deterministic sweep (`coord.board_doctor_policy`) calling the SAME
    handler through :func:`dispatch_investigator_action` passes
    ``actor="sweep"`` instead, so #3875's outcome tracking can tell "a
    human-attended investigator session decided this" apart from "the
    unattended periodic sweep applied an unambiguous mechanical repair" —
    one audit trail, one real answer to "who did this", never a second
    record for the same mutation under a different name (#2096).
    """
    from coord.audit import record_audit  # noqa: PLC0415

    record_audit(
        tier="business",
        category="board_doctor_investigator",
        event_type=f"investigator_{action}" if acted else f"investigator_{action}_refused",
        actor=actor,
        summary=detail,
        repo=repo_name,
        issue=issue_number,
        details={"reason": reason, "acted": acted},
    )
    return InvestigatorActionResult(
        acted=acted,
        action=action,
        repo=repo_name,
        issue=issue_number,
        detail=detail,
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


def _in_flight_work_detail(
    repo_name: str, issue_number: int, board: "Board", *, verb: str = "a leg",
) -> str | None:
    """``None`` when *repo_name*#*issue_number* has no running/pending
    work-like assignment and no live `coord drive` tmux session; otherwise
    the human-readable reason it is already in flight.

    The shared "something is already working this row" guard
    :func:`sweep_dispatch_fixit_leg` (#3768) and the #3881 investigator's
    :func:`investigator_requeue` both need before taking a row down and
    putting it back — one question ("is anything live against this row
    right now"), one answer, so the two callers can never disagree about
    it. Deliberately narrower than :func:`sweep_dispatch_fixit_leg`'s own
    "no originating work assignment at all" check just above its call
    site: a never-dispatched row (no work-like assignment ever created)
    has nothing live against it either, so it reads as NOT in flight here
    — exactly the shape :data:`queue_diagnose.CAUSE_NEVER_DISPATCHED`
    needs :func:`investigator_requeue` to act on.

    *verb* names what the caller would otherwise dispatch a second of —
    e.g. ``"a fix-it leg"`` — and is interpolated directly into the
    returned message, so a caller that wants its own wording never has to
    `str.replace()` this function's fixed phrasing (a silent no-op the
    moment either side's wording drifts).
    """
    from coord.gates import assignments_for_issue  # noqa: PLC0415
    from coord.models import WORK_LIKE_TYPES  # noqa: PLC0415

    work_like = [
        a
        for a in assignments_for_issue(board, repo_name, issue_number)
        if a.type in WORK_LIKE_TYPES
    ]
    if any(a.status in ("running", "pending") for a in work_like):
        return (
            f"{repo_name}#{issue_number} already has a running or "
            f"pending assignment — {verb} is already in flight, not "
            "acting a second time"
        )

    from coord.drive import list_drive_sessions  # noqa: PLC0415

    live_sessions = [
        s
        for s in list_drive_sessions()
        if s.get("repo") == repo_name and s.get("issue") == issue_number
    ]
    if live_sessions:
        return (
            f"{repo_name}#{issue_number} has a live `coord drive` session "
            f"({live_sessions[0].get('session_name')}) — {verb} is already "
            "in flight, not acting a second time"
        )
    return None


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

    in_flight = _in_flight_work_detail(
        repo_name, issue_number, board, verb="a fix-it leg",
    )
    if in_flight is not None:
        return FixitLegDispatch(
            dispatched=False,
            assignment_id=None,
            machine=None,
            detail=in_flight,
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


def _finisher_leg_briefing(
    *, cause: str, evidence: tuple[str, ...], nits: tuple[str, ...] = ()
) -> str:
    """The briefing text handed to a finisher leg — the same sweep *cause*/
    *evidence* shape :func:`_fixit_leg_briefing` hands a fix-it leg, plus the
    bounded self-bless contract spelled out explicitly so the leg itself
    knows the limits of its own authority before it starts. *nits*, when the
    caller has the reviewer's actual nit findings on hand (as Trigger A's
    `coord.auto_loop.process_review_completion` call site does, via
    `coord.review.extract_nits_section`), is listed explicitly; any other
    caller that does not have that text on hand leaves it empty.

    A finisher leg runs like any other fix round right up until it pushes —
    same worker tool-scope, same `git`/edit/push primitives, no
    `--dangerously-skip-permissions`. The one thing that differs is what
    happens to *its own* diff afterward: it is graded against
    `coord.review.classify_finding_nit_class` over the already-reviewed SHA,
    and only a diff that passes gets to re-bless the prior review and advance
    to merge on its own say-so. Anything else — behavioral, touches a public
    API, touches a sealed path, or too large — falls back to an ordinary
    full-review fix round exactly like a fix-it leg's output would.
    """
    lines = [
        "The board-doctor sweep dispatched this finisher leg automatically "
        "to apply reviewer nits directly, rather than bounce the review "
        "back for a full fix round.",
        "",
        f"Cause: {cause}",
    ]
    if evidence:
        lines.append("Evidence:")
        lines.extend(f"- {item}" for item in evidence)
    if nits:
        lines.append("Reviewer's nits to apply:")
        lines.extend(f"- {item}" for item in nits)
    lines.extend([
        "",
        "You will NOT use `--dangerously-skip-permissions`, `gh`, "
        "`--force-merge`, or `--override-human-required` — this leg runs at "
        "ordinary worker tool-scope (edit/push/git) only, same as any other "
        "fix round.",
        "",
        "Your own authority is bounded: once you push, your diff (taken over "
        "the SHA the prior review actually reviewed) is graded against the "
        "same nit-class test a reviewer would apply — non-behavioral, no "
        "public-API surface, no sealed path, small. Only a diff that passes "
        "re-blesses the prior review and advances it to merge. Anything "
        "larger or more substantive falls back to an ordinary fix round and "
        "waits for a fresh human-or-reviewer approval — you have no merge "
        "authority of your own beyond that bound, and nothing you do here "
        "can reach `force_merge` or `override_human_required`.",
    ])
    return "\n".join(lines)


class FinisherLegDispatch(TypedDict):
    """:func:`sweep_dispatch_finisher_leg`'s return shape.

    ``dispatched`` is the one answer to "did a new finisher leg actually
    start", set from `_dispatch_fix`'s own confirmed return value — never
    from the mere absence of an exception (#2096). ``detail`` explains a
    ``False`` result so a caller can tell a lock-blocked no-op from a
    no-work-found no-op from a declined dispatch without inspecting
    exceptions.
    """

    dispatched: bool
    assignment_id: str | None
    machine: str | None
    detail: str


def sweep_dispatch_finisher_leg(
    repo_name: str,
    issue_number: int,
    board: "Board",
    config: "Config",
    *,
    cause: str,
    evidence: tuple[str, ...] = (),
    nits: tuple[str, ...] = (),
    http_client: Any | None = None,
) -> FinisherLegDispatch:
    """Daemon-internal "dispatch one autonomous finisher leg", authorized for
    the sweep context.

    Mirrors :func:`sweep_dispatch_fixit_leg` exactly for the dispatch half —
    same in-flight lock (no second leg while a work-like assignment is
    ``running``/``pending`` or a live `coord drive` session already exists
    for this issue), same `_dispatch_fix` primitive, same fresh fix-round
    number via `coord.auto_loop.next_fix_iteration`. The only difference is
    the briefing (:func:`_finisher_leg_briefing`, carrying the bounded
    self-bless contract) and the audit `event_type`. What the leg's *own*
    diff is later allowed to do on its own authority — self-bless versus fall
    back to a full review — is decided by :func:`finisher_self_bless`, not by
    this dispatch step; this function only ever starts the leg.

    Returns a :class:`FinisherLegDispatch` with ``dispatched=False`` — never
    raises — for: no originating work-like assignment found for this issue,
    a leg already in flight, or an underlying `_dispatch_fix` call that
    declined. Only an observed, confirmed `Assignment` back from
    `_dispatch_fix` is reported as ``dispatched=True``.

    *nits* is forwarded straight to :func:`_finisher_leg_briefing` — the
    reviewer's actual nit/non-blocking findings text, when the caller has
    it, so the leg's briefing names what to apply instead of just a count.
    """
    authorize_sweep_action("dispatch_finisher_leg")

    from coord.gates import _select_winning_work_assignment, assignments_for_issue  # noqa: PLC0415
    from coord.models import WORK_LIKE_TYPES  # noqa: PLC0415

    work_like = [
        a
        for a in assignments_for_issue(board, repo_name, issue_number)
        if a.type in WORK_LIKE_TYPES
    ]
    if not work_like:
        return FinisherLegDispatch(
            dispatched=False,
            assignment_id=None,
            machine=None,
            detail=(
                f"{repo_name}#{issue_number} has no originating work "
                "assignment on the board — nothing to dispatch a finisher "
                "leg against"
            ),
        )

    if any(a.status in ("running", "pending") for a in work_like):
        return FinisherLegDispatch(
            dispatched=False,
            assignment_id=None,
            machine=None,
            detail=(
                f"{repo_name}#{issue_number} already has a running or "
                "pending assignment — a finisher leg is already in flight, "
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
        return FinisherLegDispatch(
            dispatched=False,
            assignment_id=None,
            machine=None,
            detail=(
                f"{repo_name}#{issue_number} has a live `coord drive` "
                f"session ({live_sessions[0].get('session_name')}) — a "
                "finisher leg is already in flight, not dispatching a second "
                "one"
            ),
        )

    work = _select_winning_work_assignment(work_like)

    from coord.auto_loop import _dispatch_fix, next_fix_iteration  # noqa: PLC0415

    iteration = next_fix_iteration(board, work)
    briefing = _finisher_leg_briefing(cause=cause, evidence=evidence, nits=nits)

    fix = _dispatch_fix(
        work, briefing, board, config, iteration, http_client=http_client,
    )
    if fix is None:
        return FinisherLegDispatch(
            dispatched=False,
            assignment_id=None,
            machine=None,
            detail=(
                f"{repo_name}#{issue_number} finisher leg dispatch could not "
                "be confirmed started — no capable machine, an unreachable "
                "agent, or a declined dispatch"
            ),
        )

    from coord.audit import record_audit  # noqa: PLC0415

    record_audit(
        tier="operational",
        category="board_doctor",
        event_type="finisher_leg_dispatched",
        actor="daemon",
        summary=(
            f"board-doctor dispatched a finisher leg for {repo_name}#"
            f"{issue_number} (cause={cause}) -> {fix.machine_name}"
        ),
        repo=repo_name,
        issue=issue_number,
        assignment_id=fix.assignment_id,
        machine=fix.machine_name,
        details={"cause": cause, "evidence": list(evidence)},
    )

    return FinisherLegDispatch(
        dispatched=True,
        assignment_id=fix.assignment_id,
        machine=fix.machine_name,
        detail=(
            f"{repo_name}#{issue_number} finisher leg dispatched to "
            f"{fix.machine_name} (iteration {iteration})"
        ),
    )


#: The one `coord.auto_loop._dispatch_fix_for_review` action kind that means
#: a fix round actually went out. Every other kind it can yield
#: (``no_work_found``, ``terminal_skip``, ``max_iterations``,
#: ``fix_round_ceiling_blocked``, ``needs_redesign``) is a refusal, so a
#: decline detail must not call it a fix round.
_FIX_DISPATCHED_KIND = "fix_dispatched"


class FinisherSelfBlessResult(TypedDict):
    """:func:`finisher_self_bless`'s return shape.

    ``blessed`` is the one answer to "did this diff re-bless the prior
    review on its own authority" — ``True`` means a PENDING merge-queue
    entry keyed to the parent work assignment was **read back** afterwards
    (:func:`_observed_merge_queue_entry`), not merely that nothing raised.
    ``False`` means either the normal full-review fix path
    (:func:`coord.auto_loop._dispatch_fix_for_review`) ran instead, or the
    override was recorded but nothing reached the merge queue. ``actions``
    carries that fallback path's own `LoopAction` list when the fallback
    ran, empty otherwise.
    """

    blessed: bool
    actions: list[Any]
    detail: str


def finisher_self_bless(
    review: "Assignment",
    findings: "ReviewFindings",
    board: "Board",
    config: "Config",
    *,
    fix: "ProposedFix",
    new_head_sha: str,
    new_patch_id: str | None = None,
    applied_nits: tuple[str, ...] = (),
    waived_nits: tuple[str, ...] = (),
    http_client: Any | None = None,
) -> FinisherSelfBlessResult:
    """Decide whether a finisher leg's own diff may re-bless *review* — the
    bounded-authority core of a finisher leg.

    A nit fix changes the patch-id, which voids the prior review's approval.
    Something has to re-bless the new SHA. This is the one thing in the
    codebase authorized to do that *for nit-class deltas specifically* —
    never for anything larger — and it is authorized the same way every
    other daemon-internal mutation here is: gated on
    :func:`authorize_sweep_action`, and gated, every single call, on *fix*
    actually passing `coord.review.classify_finding_nit_class` over
    *findings* **and** on `coord.review.classify_blocking_evidence` over
    the same body confirming no blocking finding survives — never on the
    leg's own say-so.

    *fix* is a caller attestation, not a mechanically-derived property:
    whoever calls this (the finisher leg itself, reporting on its own diff)
    asserts `fix.behavioral` and `fix.touches_public_api` at exactly the
    trust level a human reviewer's own attestation carries today — this
    function does not and cannot independently verify either beyond the
    mechanical size/sealed-path checks `classify_finding_nit_class` already
    does from `fix.diff_text`.

    On a passing (nit-class, confirmed-blocking-free) *fix*: overwrites
    *review*'s verdict fields — preserving the original verdict and reason,
    same shape #476's approve-with-nits override uses
    (`review_verdict_original`, `review_verdict_override_reason`,
    `verdict_source="overridden"`) — re-points the review's staleness
    anchors onto *new_head_sha*/*new_patch_id* both in-memory and durably
    via `coord.state.record_review_reaffirm` (so a merge-queue scan in this
    process or a later one both see the new SHA as approved, not stale),
    then hands off to `coord.auto_loop.propagate_review_verdict` with
    ``refresh_merge_queue=True`` to advance to the merge queue. That handoff
    is the *only* merge-queue primitive this function ever calls; it has no
    path to `force_merge` or `override_human_required`, and neither name is
    ever referenced here.

    ``blessed`` is then decided by **reading the merge queue back**
    (:func:`_observed_merge_queue_entry`), never by that handoff returning
    without raising. ``propagate_review_verdict`` returns the parent work
    row whenever the row is on the board, which says nothing about its
    merge-queue refresh: the refresh is skipped outright for a row with no
    branch, and its failures are logged rather than propagated. So a
    missing parent row, a branchless parent row and a refresh that threw
    all report ``blessed=False`` with the reason in ``detail`` — the review
    row's override stands either way, but the claim "advanced it to the
    merge queue" is only ever made about an entry that was observed to
    exist.

    On a failing fix — either not nit-class, or nit-class but the review
    body still carries a confirmed blocking finding — records why, then
    falls back WHOLESALE to `coord.auto_loop._dispatch_fix_for_review` — the
    same full-review fix path any other request-changes verdict takes — and
    reports ``blessed=False``. This function never merges or approves on
    that path.

    Records an audit row on both branches, and a PR comment (recording which
    nits were applied versus waived) on the self-bless branch, so the
    decision is visible both as a structured audit trail and on the PR
    itself.
    """
    authorize_sweep_action("dispatch_finisher_leg")

    if review.type != "review":
        raise ValueError(
            f"finisher_self_bless requires a review assignment, got "
            f"assignment_id={review.assignment_id!r} with "
            f"type={review.type!r}"
        )

    from coord.review import classify_blocking_evidence, classify_finding_nit_class  # noqa: PLC0415

    sealed_paths = config.acceptance.sealed_paths(review.repo_name)
    blocking_evidence = classify_blocking_evidence(
        findings.body, assignment_id=review.assignment_id,
    )
    is_nit_class = (
        classify_finding_nit_class(
            findings.body,
            assignment_id=review.assignment_id,
            fix=fix,
            sealed_paths=sealed_paths,
        )
        and blocking_evidence.confirmed_absent
    )

    from coord.audit import record_audit  # noqa: PLC0415

    if not is_nit_class:
        record_audit(
            tier="business",
            category="review",
            event_type="finisher_self_bless_declined",
            actor="daemon",
            summary=(
                f"finisher leg diff for {review.repo_name}#"
                f"{review.issue_number} is not nit-class — falling back to "
                "a full-review fix round, not self-blessing"
            ),
            repo=review.repo_name,
            issue=review.issue_number,
            assignment_id=review.assignment_id,
            machine=review.machine_name,
            details={
                "behavioral": fix.behavioral,
                "touches_public_api": fix.touches_public_api,
                "blocking_confirmed_absent": blocking_evidence.confirmed_absent,
            },
        )

        from coord.auto_loop import _dispatch_fix_for_review  # noqa: PLC0415

        actions = list(_dispatch_fix_for_review(
            review, findings, board, config, http_client=http_client,
        ))
        if actions and actions[0].kind == _FIX_DISPATCHED_KIND:
            fallback_detail = f"fell back to a full-review fix round ({actions[0].detail})"
        elif actions:
            fallback_detail = (
                "fell back to the full-review path, which dispatched no fix "
                f"round ({actions[0].kind}: {actions[0].detail})"
            )
        else:
            fallback_detail = "fell back to the full-review path, but no fix round was dispatched"
        return FinisherSelfBlessResult(
            blessed=False,
            actions=actions,
            detail=(
                f"{review.repo_name}#{review.issue_number}: finisher leg "
                f"diff is not nit-class — {fallback_detail}"
            ),
        )

    original_verdict = findings.verdict
    override_reason = (
        "finisher self-bless: own diff passed classify_finding_nit_class "
        "with confirmed-absent blocking findings "
        f"(applied={list(applied_nits)}, waived={list(waived_nits)})"
    )
    review.review_verdict_original = original_verdict
    review.review_verdict_override_reason = override_reason
    review.review_verdict = "approve"
    review.verdict_source = "overridden"
    review.verdict_source_reason = override_reason
    review.review_head_sha = new_head_sha
    review.review_patch_id = new_patch_id

    from coord.state import record_review_reaffirm  # noqa: PLC0415

    record_review_reaffirm(
        review_assignment_id=review.assignment_id,
        new_head_sha=new_head_sha,
        new_patch_id=new_patch_id,
        reason=(
            "finisher leg self-bless re-pointed staleness anchors after "
            "pushing a nit-class fix over the reviewed SHA"
        ),
        actor="daemon",
        conflict_fix_only=None,
    )

    record_audit(
        tier="business",
        category="review",
        event_type="finisher_self_bless_approved",
        actor="daemon",
        summary=(
            f"Finisher leg self-blessed its own nit-class diff for "
            f"{review.repo_name}#{review.issue_number}, overriding "
            f"{original_verdict!r} -> approve"
        ),
        repo=review.repo_name,
        issue=review.issue_number,
        assignment_id=review.assignment_id,
        machine=review.machine_name,
        details={
            "original_verdict": original_verdict,
            "applied_nits": list(applied_nits),
            "waived_nits": list(waived_nits),
            "new_head_sha": new_head_sha,
            "new_patch_id": new_patch_id,
        },
    )

    _post_finisher_self_bless_notice(
        review, board, config,
        original_verdict=original_verdict,
        applied_nits=applied_nits,
        waived_nits=waived_nits,
    )

    from coord.auto_loop import propagate_review_verdict  # noqa: PLC0415

    work = propagate_review_verdict(review, board, config, refresh_merge_queue=True)
    if work is None:
        return _self_bless_not_advanced(
            review,
            reason=(
                "the parent work assignment could not be found on the board"
            ),
            work_assignment_id=None,
        )

    entry = _observed_merge_queue_entry(work)
    if entry is None:
        return _self_bless_not_advanced(
            review,
            reason=(
                "no pending merge-queue entry is keyed to parent work "
                f"assignment {work.assignment_id!r} "
                f"(branch={work.branch!r}) afterwards"
            ),
            work_assignment_id=work.assignment_id,
        )

    return FinisherSelfBlessResult(
        blessed=True,
        actions=[],
        detail=(
            f"{review.repo_name}#{review.issue_number}: finisher leg "
            "self-blessed a nit-class diff and advanced it to the merge "
            f"queue (pending entry on {entry.branch!r} keyed to "
            f"{entry.assignment_id!r})"
        ),
    )


def _observed_merge_queue_entry(work: "Assignment") -> "QueuedMerge | None":
    """Read the merge queue back and return the PENDING entry keyed to
    *work*, or ``None`` when there isn't one.

    This is the observation that decides :func:`finisher_self_bless`'s
    ``blessed`` (#2096: never derive success from the absence of an
    exception). :func:`coord.auto_loop.propagate_review_verdict` cannot
    answer the question itself — it returns the parent work row whenever
    that row is on the board, while its merge-queue refresh is skipped for
    a branchless row and swallows its own failures into a log line. Only a
    read-back distinguishes "an entry the merge gate can pick up now
    exists" from "the review row was rewritten and nothing else happened".

    A terminal entry (MERGED, CONFLICT, ...) does not count:
    `coord.merge_queue.refresh_entry_assignment` deliberately leaves those
    untouched rather than re-keying them, so one is evidence that nothing
    was advanced, not that something was.
    """
    if not work.assignment_id:
        return None
    try:
        from coord.merge_queue import PENDING, load_queue  # noqa: PLC0415

        items = load_queue()
    except Exception:  # noqa: BLE001 — an unreadable queue is an unobserved advance
        log.warning(
            "finisher self-bless: could not read the merge queue back for %s",
            work.assignment_id, exc_info=True,
        )
        return None
    return next(
        (
            x for x in items
            if x.assignment_id == work.assignment_id and x.state == PENDING
        ),
        None,
    )


def _self_bless_not_advanced(
    review: "Assignment",
    *,
    reason: str,
    work_assignment_id: str | None,
) -> FinisherSelfBlessResult:
    """Report a self-bless whose override landed but whose merge-queue
    advance was not observed, and record it as an audit row.

    The review row keeps its override — it describes the new SHA truthfully
    — but ``blessed`` is ``False`` and *reason* names what was missing, so
    the stuck row stays visible to the next board-doctor sweep instead of
    being reported as a merge that is on its way.
    """
    from coord.audit import record_audit  # noqa: PLC0415

    record_audit(
        tier="business",
        category="review",
        event_type="finisher_self_bless_advance_unconfirmed",
        actor="daemon",
        summary=(
            f"finisher leg recorded a self-bless override for "
            f"{review.repo_name}#{review.issue_number} but could not "
            f"confirm the merge-queue advance — {reason}"
        ),
        repo=review.repo_name,
        issue=review.issue_number,
        assignment_id=review.assignment_id,
        machine=review.machine_name,
        details={"reason": reason, "work_assignment_id": work_assignment_id},
    )
    return FinisherSelfBlessResult(
        blessed=False,
        actions=[],
        detail=(
            f"{review.repo_name}#{review.issue_number}: finisher leg "
            f"recorded the self-bless override, but {reason} — nothing was "
            "advanced to the merge queue"
        ),
    )


def _post_finisher_self_bless_notice(
    review: "Assignment",
    board: "Board",
    config: "Config",
    *,
    original_verdict: str,
    applied_nits: tuple[str, ...],
    waived_nits: tuple[str, ...],
) -> None:
    """Post the audit-trail PR comment for a finisher self-bless.

    Mirrors `coord.auto_loop._post_advisory_nits_notice`'s shape exactly —
    looks up the parent work assignment via *board* and no-ops if it can't
    be found, uses the parent work's assignment id in the marker (not the
    review's own), and is best-effort: a `github_ops` failure must never
    block the pipeline. Names which nits were actually applied versus
    waived, since that (not a blocking/nonblocking count) is what this
    override is actually attesting to.
    """
    work = (
        board.find_by_id(review.review_of_assignment_id)
        if review.review_of_assignment_id
        else None
    )
    if work is None:
        return
    repo = config.repo(work.repo_name)
    if repo is None:
        return
    from coord import github_ops  # noqa: PLC0415

    applied_lines = (
        "\n".join(f"- {item}" for item in applied_nits)
        if applied_nits
        else "- (none recorded)"
    )
    waived_lines = (
        "\n".join(f"- {item}" for item in waived_nits)
        if waived_nits
        else "- (none)"
    )
    body = (
        f"<!-- coord:event=finisher_self_bless assignment={work.assignment_id} "
        f"original_verdict={original_verdict} override_verdict=approve -->\n"
        f"## 🤖 Finisher leg self-blessed this nit-class fix\n\n"
        f"- **Reviewer's verdict:** `{original_verdict}`\n"
        f"- **Finisher's override:** `approve`\n\n"
        f"A board-doctor finisher leg applied reviewer nits directly and "
        f"its own diff passed the nit-class bound "
        f"(`coord.review.classify_finding_nit_class`) — non-behavioral, no "
        f"public-API surface, no sealed path, small, and with no confirmed "
        f"blocking finding left in the review. That is enough authority for "
        f"the finisher to re-bless this review's verdict and advance it to "
        f"merge on its own; the reviewer's own verdict is preserved on the "
        f"assignment (`review_verdict_original`), not erased.\n\n"
        f"**Nits applied:**\n{applied_lines}\n\n"
        f"**Nits waived:**\n{waived_lines}\n"
    )
    try:
        github_ops.post_issue_comment(repo.github, work.issue_number, body)
    except Exception as exc:  # noqa: BLE001 — best-effort notice
        log.warning(
            "board_doctor_permissions: failed to post finisher self-bless "
            "notice for %s: %s",
            review.assignment_id, exc,
        )


class SmokeRequeue(TypedDict):
    """:func:`sweep_smoke_requeue`'s return shape.

    ``requeued`` is the one answer to "was the smoke leg actually cleared
    for re-dispatch" — ``detail`` says why not when it is ``False``, so a
    no-op reads distinctly from a success to an unattended caller.
    """

    requeued: bool
    detail: str


def sweep_smoke_requeue(repo_name: str, issue_number: int) -> SmokeRequeue:
    """Daemon-internal "re-dispatch a no-verdict smoke leg", authorized for
    the sweep context (#2244).

    A smoke leg that exhausted #2272's mute-leg retry budget without ever
    producing a parseable verdict (`coord.smoke.NO_SMOKE_VERDICT_MARKER`)
    parks its row at ``test_state=TEST_STATE_BLOCKED`` exactly like any
    other Test-gate block — there is nothing to merge, approve or remove,
    only a stale verdict to clear so the row re-enters the ordinary
    dispatch scan as if the smoke leg had never run. Calls
    `coord.state.reset_work_test_state` directly — the same primitive
    `coord diagnose --stage test --reset` calls — which best-effort
    releases any outstanding #3182 fan-out smoke claim too.

    #3810: bounded by `coord.smoke.BOARD_DOCTOR_REQUEUE_BUDGET`, separately
    from the #2272 mute-leg budget it is requeuing past. Clearing
    `test_state`/`test_reason` to ``NULL`` on every requeue with no bound of
    its own erased the only tally `coord.smoke.mute_smoke_legs` reads, so a
    leg that keeps muting after each requeue restarted the #2272 budget from
    zero and looped forever rather than ever reaching a human. The budget is
    NOT tracked in `test_reason`: `coord.notify._record_smoke_verdict`
    overwrites that column wholesale on every subsequent mute leg, so a
    marker written there would not survive even one more mute/requeue
    cycle. Instead it reads/increments
    `coord.state.board_doctor_smoke_requeue_count` /
    `coord.state.record_board_doctor_smoke_requeue`, a counter kept in its
    own `board_meta` row keyed by assignment id — untouched by both
    `_record_smoke_verdict`'s overwrites and `coord.board_doctor.sweep`'s
    `clear_absent_rows` pass. At or past budget, this refuses WITHOUT
    touching the row at all, so `test_state` stays set and
    `coord.board_doctor.smoke_no_verdict_rows` keeps selecting it every
    subsequent sweep, letting the row's convergence counter climb
    uninterrupted to the alert tier instead of being wiped by
    `clear_absent_rows` the moment a reset makes the row stop matching.

    Reads `coord.state.test_reset_candidates` FIRST to confirm at least one
    matching row still carries a `test_state` before writing, so a repeat
    call against a row this already cleared reports ``requeued=False``
    rather than a bare pass-through of `reset_work_test_state`'s row-matched
    count — that count reflects rows the WHERE clause matched, not rows
    whose value actually changed, so by itself it cannot tell a genuine
    clear apart from a no-op re-clear of an already-``NULL`` column. After
    writing, re-reads `test_reset_candidates` again and raises
    :class:`SweepWriteNotConfirmedError` if any matched row still comes
    back with a `test_state` set — mirroring `coord.diagnose._reset_test_stage`'s
    before/after confirm pattern, per this module's own documented
    write-confirmation invariant, rather than trusting the write call's own
    row-matched count as proof the clear landed.
    """
    authorize_sweep_action("smoke_requeue")

    from coord.smoke import BOARD_DOCTOR_REQUEUE_BUDGET  # noqa: PLC0415
    from coord.state import (  # noqa: PLC0415
        board_doctor_smoke_requeue_count,
        record_board_doctor_smoke_requeue,
        reset_work_test_state,
        test_reset_candidates,
    )

    before = test_reset_candidates(repo_name, issue_number)
    live = [(aid, typ) for aid, typ, test_state in before if test_state is not None]
    if not live:
        return SmokeRequeue(
            requeued=False,
            detail=(
                f"{repo_name}#{issue_number} has no assignment row with a "
                "test_state/test_reason to clear — already re-dispatched or "
                "moved on"
            ),
        )

    legs = max(board_doctor_smoke_requeue_count(aid) for aid, _typ in live)
    if legs >= BOARD_DOCTOR_REQUEUE_BUDGET:
        return SmokeRequeue(
            requeued=False,
            detail=(
                f"{repo_name}#{issue_number}: already requeued {legs} time(s), "
                f"at or past BOARD_DOCTOR_REQUEUE_BUDGET={BOARD_DOCTOR_REQUEUE_BUDGET} "
                "— leaving the row set so it escalates instead of looping"
            ),
        )

    cleared = reset_work_test_state(repo_name, issue_number)
    if cleared <= 0:
        return SmokeRequeue(
            requeued=False,
            detail=(
                f"{repo_name}#{issue_number}: reset matched no assignment "
                "rows — nothing to clear"
            ),
        )

    after = test_reset_candidates(repo_name, issue_number)
    still_set = [(aid, typ) for aid, typ, test_state in after if test_state is not None]
    if still_set:
        listed = ", ".join(f"{aid} ({typ})" for aid, typ in still_set)
        log.error(
            "board-doctor sweep: smoke-requeue write for %s#%s could not be "
            "confirmed — test_state is still set on re-read for: %s",
            repo_name,
            issue_number,
            listed,
        )
        raise SweepWriteNotConfirmedError(
            f"sweep cleared {repo_name}#{issue_number}'s smoke verdict but "
            f"test_state read back still set for: {listed} — the clear did "
            "not persist"
        )

    new_count = record_board_doctor_smoke_requeue(live[0][0])

    from coord.audit import record_audit  # noqa: PLC0415

    record_audit(
        tier="operational",
        category="board_doctor",
        event_type="smoke_requeued",
        actor="daemon",
        summary=(
            f"board-doctor requeued {repo_name}#{issue_number}'s no-verdict "
            f"smoke leg ({new_count}/{BOARD_DOCTOR_REQUEUE_BUDGET})"
        ),
        repo=repo_name,
        issue=issue_number,
        assignment_id=live[0][0],
        details={"cause": "smoke-no-verdict", "requeue_count": new_count},
    )
    return SmokeRequeue(
        requeued=True,
        detail=(
            f"{repo_name}#{issue_number}: cleared test_state on {cleared} "
            f"assignment row(s) so the smoke leg re-dispatches "
            f"(requeue {new_count}/{BOARD_DOCTOR_REQUEUE_BUDGET})"
        ),
    )


class RaiseBudget(TypedDict):
    """:func:`sweep_raise_fixit_budget`'s return shape — same ``detail``
    contract as :class:`DriveQueueRequeue`."""

    raised: bool
    entry_id: int | None
    new_max_fix_rounds: int | None
    detail: str


def sweep_raise_fixit_budget(
    repo_name: str, issue_number: int, config: "Config", *, increment: int = 2,
) -> RaiseBudget:
    """Bump a queued row's ``max_fix_rounds`` ceiling by *increment* (#3881).

    The mechanical half of the investigator's ``raise_budget`` verb —
    eligibility (the once-ever cap, and the request-changes/CI-red
    evidence that makes a bump safe) is the caller's job
    (:func:`investigator_raise_budget`); this function only ever resolves
    the row's CURRENT effective ceiling (`coord.drive_queue.
    effective_max_fix_rounds`, which already knows how to fall back from
    an unset `entry.max_fix_rounds` to the fleet default) and writes back
    *increment* above it — never a bare, caller-supplied absolute number,
    so a repeated call without an intervening fix round cannot silently
    compound past what the caller asked for in isolation.

    Shares :func:`sweep_drive_queue_requeue`'s take-down-and-put-back
    shape (and its own `_refuse_if_gate_mid_flight`/`_validate_after`
    guards) because raising the ceiling is, mechanically, the exact same
    write with one field changed — reusing it rather than inventing a
    second remove+add keeps the #1757/#3236 declaration-preservation
    invariants in exactly one place.
    """
    authorize_sweep_action("raise_budget")

    entry = _existing_entry(repo_name, issue_number)
    if entry is None:
        return RaiseBudget(
            raised=False,
            entry_id=None,
            new_max_fix_rounds=None,
            detail=(
                f"{repo_name}#{issue_number} is not in the drive queue — "
                "nothing to raise"
            ),
        )

    _refuse_if_gate_mid_flight(repo_name, issue_number, entry)
    declaration = _declaration_of(entry)
    _validate_after(repo_name, issue_number, declaration["after"])

    from coord.drive_queue import effective_max_fix_rounds  # noqa: PLC0415

    current_ceiling = effective_max_fix_rounds(entry, config.pipeline.max_fix_rounds)
    new_ceiling = current_ceiling + increment
    declaration["max_fix_rounds"] = new_ceiling

    authorize_sweep_action("drive_queue_remove")
    removal = _dequeue_and_log(repo_name, issue_number)
    if not removal.get("removed"):
        return RaiseBudget(
            raised=False,
            entry_id=None,
            new_max_fix_rounds=None,
            detail=(
                f"{repo_name}#{issue_number} was not removed, so its "
                "budget was not raised — the queue is unchanged"
            ),
        )

    try:
        entry_id = _enqueue_declared(repo_name, issue_number, declaration)
    except Exception:
        log.error(
            "board-doctor sweep: removed drive-queue row %s#%s to raise its "
            "fix-round ceiling but could NOT put it back (declaration: %r) "
            "— the queue is short a row and needs `coord drive-queue add` "
            "by hand",
            repo_name,
            issue_number,
            declaration,
        )
        raise

    return RaiseBudget(
        raised=True,
        entry_id=entry_id,
        new_max_fix_rounds=new_ceiling,
        detail=(
            f"{repo_name}#{issue_number} max_fix_rounds raised "
            f"{current_ceiling} -> {new_ceiling}"
        ),
    )


class RestageResult(TypedDict):
    """:func:`sweep_restage`'s return shape."""

    restaged: bool
    detail: str


def sweep_restage(
    repo_name: str, issue_number: int, board: "Board", config: "Config", *, stage: str,
) -> RestageResult:
    """Re-dispatch a missing/unreadable Test or Review stage (#3881).

    *stage* is ``"test"`` or ``"review"``; any other value raises
    ``ValueError`` immediately — a typo here must never silently fall
    through to one of the two real branches.

    ``"test"`` reuses :func:`sweep_smoke_requeue` verbatim (already the
    sweep's own "clear a muted smoke leg's verdict fields so it
    re-dispatches" primitive, #2272/#3810) — restaging a Test stage IS
    that action under a name an investigator session reads more plainly.

    ``"review"`` finds the row's own most recent completed ``work``
    assignment and calls :func:`coord.review.dispatch_review` against it
    directly — the same primitive the auto-loop's own review-dispatch tick
    calls, never a bespoke HTTP POST. No completed work assignment to
    restage a review for is reported ``restaged=False``, not an error.
    """
    authorize_sweep_action("restage")

    if stage == "test":
        result = sweep_smoke_requeue(repo_name, issue_number)
        return RestageResult(restaged=result["requeued"], detail=result["detail"])

    if stage != "review":
        raise ValueError(f"sweep_restage: stage must be 'test' or 'review', got {stage!r}")

    from coord.gates import assignments_for_issue  # noqa: PLC0415

    work = next(
        (
            a
            for a in reversed(assignments_for_issue(board, repo_name, issue_number))
            if a.type == "work" and a.status == "done"
        ),
        None,
    )
    if work is None:
        return RestageResult(
            restaged=False,
            detail=(
                f"{repo_name}#{issue_number} has no completed work "
                "assignment to restage a review for"
            ),
        )

    from coord.board_service import write_board  # noqa: PLC0415
    from coord.review import dispatch_review  # noqa: PLC0415

    try:
        dispatched = dispatch_review(work, board, config)
    except Exception as exc:  # noqa: BLE001 — report, never crash the wrapper
        return RestageResult(
            restaged=False,
            detail=f"{repo_name}#{issue_number} review restage raised: {exc}",
        )
    if dispatched is None:
        return RestageResult(
            restaged=False,
            detail=f"{repo_name}#{issue_number} review restage made no dispatch",
        )
    # `dispatch_review`'s own contract (coord/review.py): "the caller is
    # responsible for persisting the board" — it mutates `work.review_state`
    # in place but never writes it back itself.
    write_board(board)
    return RestageResult(
        restaged=True,
        detail=(
            f"{repo_name}#{issue_number} re-dispatched a review "
            f"({getattr(dispatched, 'assignment_id', dispatched)!r})"
        ),
    )


class ClearLatchResult(TypedDict):
    """:func:`sweep_clear_latch`'s return shape."""

    cleared: bool
    detail: str


def sweep_clear_latch(
    repo_name: str, issue_number: int, board: "Board", config: "Config", gh_ops: Any,
) -> ClearLatchResult:
    """Re-test a stale CONFLICT/HUMAN_REQUIRED merge-queue latch and clear
    it when it no longer reflects live reality (#3881).

    Two independent, both-required gates before touching anything:

    1. The row must actually BE latched (``CONFLICT`` or
       ``HUMAN_REQUIRED``) — a row in any other merge-queue state is left
       untouched and reported ``cleared=False``.
    2. :func:`coord.merge_queue.has_approved_review` must confirm the
       current approval is bound to the branch's live content (SHA or,
       for a content-identical rebase, patch-id) — #3881's "approval
       bound to the current patch-id" precondition. A latch whose approval
       is stale is left alone; clearing it would return a row with no
       valid approval straight to ``PENDING``.

    Only once both hold does this call
    :func:`coord.merge_queue.reconcile_conflict_entries` — itself a third,
    independent live-mergeability re-check (GitHub's own
    ``check_pr_mergeable``, plus the rebase-refusal linearity probe) that
    this function does not duplicate. #3902: scoped to exactly THIS row via
    ``only=(repo_name, issue_number)`` — a periodic sweep with N latched
    rows must not pay for N full-queue GitHub probe passes (one per
    latched row per sweep tick) when it only ever needs this one row's
    answer; every OTHER row's own latch still clears on its own turn, or
    on the next ordinary `coord merge` tick, which already probes the
    whole queue unscoped.

    ``cleared=True`` only when *this* row's key appears in the returned
    events — a call that unparks a different sibling row but leaves this
    one latched (gate 3 above still refused it) reports ``cleared=False``.
    """
    authorize_sweep_action("clear_latch")

    from coord.merge_queue import (  # noqa: PLC0415
        CONFLICT,
        HUMAN_REQUIRED,
        has_approved_review,
        load_queue,
        reconcile_conflict_entries,
    )

    queue = load_queue()
    entry = next(
        (e for e in queue if e.repo_name == repo_name and e.issue_number == issue_number),
        None,
    )
    if entry is None:
        return ClearLatchResult(
            cleared=False,
            detail=f"{repo_name}#{issue_number} has no merge-queue entry",
        )
    if entry.state not in (CONFLICT, HUMAN_REQUIRED):
        return ClearLatchResult(
            cleared=False,
            detail=(
                f"{repo_name}#{issue_number} merge-queue entry is "
                f"{entry.state!r}, not a latched conflict/human_required row"
            ),
        )
    if not has_approved_review(entry, board, gh_ops, config=config):
        return ClearLatchResult(
            cleared=False,
            detail=(
                f"{repo_name}#{issue_number} has no approval bound to the "
                "current branch content — refusing to clear its latch"
            ),
        )

    events = reconcile_conflict_entries(gh_ops, only=(repo_name, issue_number))
    mine = [
        ev
        for ev in events
        if ev.entry.repo_name == repo_name and ev.entry.issue_number == issue_number
    ]
    if not mine:
        return ClearLatchResult(
            cleared=False,
            detail=(
                f"{repo_name}#{issue_number} latch did not clear — live "
                "mergeability still refuses it"
            ),
        )
    return ClearLatchResult(cleared=True, detail=mine[0].message)


class ConflictFixRedispatchResult(TypedDict):
    """:func:`sweep_redispatch_head_unmoved_conflict_fix`'s return shape."""

    redispatched: bool
    detail: str


def _redispatch_result(
    repo_name: str, issue_number: int, *, redispatched: bool, detail: str,
) -> ConflictFixRedispatchResult:
    """Build a :class:`ConflictFixRedispatchResult` and audit it (#3887) —
    this action has no human-attended CLI call site (unlike the #3881
    investigator verbs' :func:`_investigator_result`), so ``actor="sweep"``
    is the only answer this audit trail will ever carry: there is no
    session to attribute it to instead. Never raises —
    :func:`coord.audit.record_audit`'s own best-effort contract.
    """
    from coord.audit import record_audit  # noqa: PLC0415

    record_audit(
        tier="operational",
        category="board_doctor",
        event_type="conflict_fix_redispatched" if redispatched else "conflict_fix_redispatch_refused",
        actor="sweep",
        summary=detail,
        repo=repo_name,
        issue=issue_number,
        details={"redispatched": redispatched},
    )
    return ConflictFixRedispatchResult(redispatched=redispatched, detail=detail)


def _latest_done_conflict_fix_assignment_id(
    board: "Board", entry: "QueuedMerge",
) -> str | None:
    """The ``type="conflict-fix"`` assignment whose clean-but-unmoved exit
    produced *entry*'s current head-unmoved HUMAN_REQUIRED park, or
    ``None``.

    Primary source: :func:`coord.merge_queue.head_unmoved_fix_assignment_id`
    parsed straight off ``entry.error`` — the EXACT id
    `coord.reconcile.on_conflict_fix_done` recorded there the moment it
    detected the park, never re-derived, and always the LATEST marker when
    the row has parked more than once. This is the only source that can
    never name the wrong row: a periodic sweep that runs after some OTHER
    conflict-fix attempt for this same entry finished (a manual retry, or
    an unrelated earlier sweep tick) cannot shift which leg this answers
    for.

    Fallback, only when the marker is absent (a latch parked before this
    marker existed): the most recently FINISHED ``type="conflict-fix"``
    assignment linked to this entry
    (``a.review_of_assignment_id == entry.assignment_id``), picking the
    one with the latest ``finished_at``, and skipping stale-rebase-titled
    rows (:data:`coord.conflict_fix.STALE_REBASE_FIX_TITLE_PREFIX`) the
    same way :func:`coord.conflict_fix._classify_prior_conflict_fix` does
    — a stale-rebase attempt never tried the ordinary mechanical
    resolution this carve-out is about, so it must never be mistaken for
    the just-completed ordinary leg.

    Either way, the result is passed through to
    :func:`coord.conflict_fix.dispatch_conflict_fix`'s own
    ``after_head_unmoved_fix_assignment_id`` carve-out, which excludes
    exactly this one row from the retry-cap's identical-error walk — the
    same exclusion `coord.reconcile._try_ordinary_escalation_after_
    head_unmoved`'s own synchronous completion-time redispatch applies,
    never a looser one.
    """
    from coord.conflict_fix import STALE_REBASE_FIX_TITLE_PREFIX  # noqa: PLC0415
    from coord.merge_queue import head_unmoved_fix_assignment_id  # noqa: PLC0415

    recorded = head_unmoved_fix_assignment_id(entry.error)
    if recorded is not None:
        return recorded

    merge_entry_id = entry.assignment_id
    if merge_entry_id is None:
        return None
    candidates = [
        a
        for a in list(board.active) + list(board.completed)
        if a.type == "conflict-fix"
        and a.review_of_assignment_id == merge_entry_id
        and a.status == "done"
        and not (a.issue_title or "").startswith(STALE_REBASE_FIX_TITLE_PREFIX)
    ]
    if not candidates:
        return None
    latest = max(candidates, key=lambda a: a.finished_at or 0.0)
    return latest.assignment_id


def sweep_redispatch_head_unmoved_conflict_fix(
    repo_name: str, issue_number: int, board: "Board", config: "Config",
) -> ConflictFixRedispatchResult:
    """Re-dispatch the ordinary conflict-fix for a `HUMAN_REQUIRED` row
    whose last attempt completed clean but never moved the branch (#3887,
    pairs with #3871).

    Two gates before anything is dispatched:

    1. The row must actually BE the head-unmoved shape — ``HUMAN_REQUIRED``
       AND `coord.merge_queue.CONFLICT_FIX_HEAD_UNMOVED_SIGNAL` present in
       the entry's own ``error`` — never a generic latch (that is
       :func:`sweep_clear_latch`'s job, not this one's).
    2. :func:`coord.conflict_fix.dispatch_conflict_fix` itself, called with
       :func:`_latest_done_conflict_fix_assignment_id` as ``after_head_
       unmoved_fix_assignment_id`` — the SAME retry-cap carve-out #3871's
       own synchronous completion-time redispatch uses, so this never
       spends a budget that carve-out did not already intend this row to
       have. A ``None`` return (no capable machine, already active, or the
       cap genuinely spent by a DIFFERENT prior attempt) is reported
       ``redispatched=False``, never raised.

    #3902: also passes ``status_fetcher=coord.network.fetch_status`` and
    ``prefer_machine`` (the just-completed leg's own ``machine_name``, read
    off the board by *ignore_assignment_id*) — the SAME two arguments
    #3871's own caller (`coord.reconcile._try_ordinary_escalation_after_
    head_unmoved`) passes. Without ``status_fetcher``, machine selection
    degrades to busy-only ranking with no liveness probe at all, which can
    pick a machine that is offline rather than merely busy; without
    ``prefer_machine`` the redispatch loses the SAME "reuse the original
    worker's local checkout" preference #3871's own redispatch gets.

    On a confirmed dispatch, moves the entry back to ``CONFLICT`` (the
    redispatched worker is now in flight) with an appended, honest error
    trailer — the same shape #3871's own redispatch leaves behind — and
    persists only this entry back over the current on-disk queue (never
    the whole pre-dispatch snapshot), then writes the board so the new
    assignment is visible.
    """
    authorize_sweep_action("conflict_fix_redispatch")

    from coord.conflict_fix import dispatch_conflict_fix  # noqa: PLC0415
    from coord.merge_queue import (  # noqa: PLC0415
        CONFLICT,
        CONFLICT_FIX_HEAD_UNMOVED_SIGNAL,
        HUMAN_REQUIRED,
        load_queue,
        save_queue,
    )
    from coord.network import fetch_status  # noqa: PLC0415

    queue = load_queue()
    entry = next(
        (e for e in queue if e.repo_name == repo_name and e.issue_number == issue_number),
        None,
    )
    if entry is None:
        return _redispatch_result(
            repo_name, issue_number, redispatched=False,
            detail=f"{repo_name}#{issue_number} has no merge-queue entry",
        )
    if entry.state != HUMAN_REQUIRED or CONFLICT_FIX_HEAD_UNMOVED_SIGNAL not in (entry.error or ""):
        return _redispatch_result(
            repo_name, issue_number, redispatched=False,
            detail=(
                f"{repo_name}#{issue_number} merge-queue entry is not a "
                "head-unmoved HUMAN_REQUIRED latch"
            ),
        )

    ignore_assignment_id = _latest_done_conflict_fix_assignment_id(board, entry)
    preferred_machine = next(
        (
            a.machine_name
            for a in list(board.active) + list(board.completed)
            if a.assignment_id == ignore_assignment_id
        ),
        None,
    ) if ignore_assignment_id is not None else None
    fix = dispatch_conflict_fix(
        entry,
        board,
        config,
        after_head_unmoved_fix_assignment_id=ignore_assignment_id,
        prefer_machine=preferred_machine,
        status_fetcher=fetch_status,
    )
    if fix is None:
        return _redispatch_result(
            repo_name, issue_number, redispatched=False,
            detail=(
                f"{repo_name}#{issue_number}: a fresh conflict-fix could not "
                "be dispatched (no capable machine, one already active, or "
                "the retry cap was already spent by a different prior "
                "attempt)"
            ),
        )

    entry.state = CONFLICT
    entry.error = (
        f"{entry.error}; board-doctor redispatched an ordinary conflict-fix "
        f"(assignment {fix.assignment_id}) on {fix.machine_name}."
    )
    fresh_queue = load_queue()
    by_id = {entry.assignment_id: entry}
    save_queue([by_id.get(x.assignment_id, x) for x in fresh_queue])

    from coord.board_service import write_board  # noqa: PLC0415

    write_board(board)

    return _redispatch_result(
        repo_name, issue_number, redispatched=True,
        detail=(
            f"{repo_name}#{issue_number}: redispatched conflict-fix "
            f"{fix.assignment_id} on {fix.machine_name}"
        ),
    )


class ConflictFixDispatchResult(TypedDict):
    """:func:`sweep_dispatch_conflict_fix`'s return shape."""

    dispatched: bool
    detail: str


def _conflict_fix_dispatch_result(
    repo_name: str, issue_number: int, *, dispatched: bool, detail: str,
) -> ConflictFixDispatchResult:
    """Build a :class:`ConflictFixDispatchResult` and audit it (#3918) —
    mirrors :func:`_redispatch_result`'s own audit shape for the identical
    reason: this action has no human-attended CLI call site, so
    ``actor="sweep"`` is the only attribution it will ever carry. Never
    raises — :func:`coord.audit.record_audit`'s own best-effort contract.
    """
    from coord.audit import record_audit  # noqa: PLC0415

    record_audit(
        tier="operational",
        category="board_doctor",
        event_type="conflict_fix_dispatched" if dispatched else "conflict_fix_dispatch_refused",
        actor="sweep",
        summary=detail,
        repo=repo_name,
        issue=issue_number,
        details={"dispatched": dispatched},
    )
    return ConflictFixDispatchResult(dispatched=dispatched, detail=detail)


def sweep_dispatch_conflict_fix(
    repo_name: str, issue_number: int, board: "Board", config: "Config",
) -> ConflictFixDispatchResult:
    """Dispatch the ordinary mechanical conflict-fix for a merge-queue entry
    GitHub itself already reports ``CONFLICT`` on, standing (#3918).

    `queue_diagnose.CAUSE_MERGE_CONFLICT` used to route through the
    investigate tier's generic ``dispatch_fixit_leg`` — a plain
    ``type="work"`` worker, not the dedicated ``type="conflict-fix"`` leg
    the auto-drain tick (`coord.commands.merge._dispatch_conflict_fixes`)
    sends the instant it raises the SAME conflict itself. That tick only
    ever reacts to a conflict it JUST produced this pass — a standing
    ``CONFLICT`` row nothing re-attempted is never retried by it
    (quadraui#2160: the investigate tier's fix-it-leg budget kept deferring
    this row for six hours with no dispatch at all, mechanical or
    otherwise). This is the periodic net for exactly that gap: the SAME
    :func:`coord.conflict_fix.dispatch_conflict_fix` primitive the reactive
    path calls, with its own retry-cap bookkeeping (one mechanical attempt,
    then ``HUMAN_REQUIRED``) completely untouched — this can never spend a
    budget that primitive would otherwise refuse.

    Deliberately NOT routed through `InvestigateCapacityGate`/the per-repo
    ``fixit_leg_budget`` — like ``CAUSE_MERGE_PENDING_STUCK``'s ``"merge"``
    action, this is a mechanical allow-list fix (#3745's C2), not a worker
    leg, so it never competes with the investigate tier's scarce budget for
    a different row's attention.

    Returns ``dispatched=False`` (never raises) when the row has no
    merge-queue entry, the entry no longer reads ``CONFLICT`` live, or
    `dispatch_conflict_fix` itself declines (no capable machine, one
    already in flight, or the retry cap already spent) — any of those
    leaves the row exactly where :func:`coord.board_doctor.sweep`'s own
    convergence counter already tracks it, so a row this never actually
    helps still escalates once ``CONVERGENCE_LIMIT`` sweeps pass, the same
    as every other mechanical action's noop.
    """
    authorize_sweep_action("conflict_fix_dispatch")

    from coord.conflict_fix import dispatch_conflict_fix  # noqa: PLC0415
    from coord.merge_queue import CONFLICT, load_queue  # noqa: PLC0415
    from coord.network import fetch_status  # noqa: PLC0415

    queue = load_queue()
    entry = next(
        (e for e in queue if e.repo_name == repo_name and e.issue_number == issue_number),
        None,
    )
    if entry is None:
        return _conflict_fix_dispatch_result(
            repo_name, issue_number, dispatched=False,
            detail=f"{repo_name}#{issue_number} has no merge-queue entry",
        )
    if entry.state != CONFLICT:
        return _conflict_fix_dispatch_result(
            repo_name, issue_number, dispatched=False,
            detail=(
                f"{repo_name}#{issue_number} merge-queue entry no longer "
                f"reads CONFLICT (state={entry.state})"
            ),
        )

    fix = dispatch_conflict_fix(entry, board, config, status_fetcher=fetch_status)
    if fix is None:
        return _conflict_fix_dispatch_result(
            repo_name, issue_number, dispatched=False,
            detail=(
                f"{repo_name}#{issue_number}: a conflict-fix could not be "
                "dispatched (no capable machine, one already active, or the "
                "retry cap was already spent)"
            ),
        )

    from coord.board_service import write_board  # noqa: PLC0415

    write_board(board)

    return _conflict_fix_dispatch_result(
        repo_name, issue_number, dispatched=True,
        detail=(
            f"{repo_name}#{issue_number}: dispatched conflict-fix "
            f"{fix.assignment_id} on {fix.machine_name}"
        ),
    )


class MarkDoneResult(TypedDict):
    """:func:`sweep_mark_done`'s return shape."""

    marked: bool
    detail: str


def sweep_mark_done(
    repo_name: str, issue_number: int, board: "Board", config: "Config",
) -> MarkDoneResult:
    """Reconcile a row whose PR merged (or whose issue closed) out-of-band
    against GitHub reality (#3881).

    A thin, repo/issue-scoped call into
    :func:`coord.reconcile.reconcile_board_merges` — the same
    conservative, confirmed-live-first sweep the daemon tick and ``coord
    reconcile-merges`` both already run, never a bespoke flip of
    ``status='merged'`` built here. ``marked=True`` only when that sweep's
    own action log reports a ``"mark merged"`` line for THIS row — a
    ``"skip"``/backfill-only/no-op pass reports ``marked=False``, with the
    sweep's own line as *detail* either way.
    """
    authorize_sweep_action("mark_done")

    from coord.reconcile import reconcile_board_merges  # noqa: PLC0415

    actions = reconcile_board_merges(
        board, config, repo=repo_name, issue=issue_number, dry_run=False,
    )
    # `coord.reconcile.reconcile_board_merges`'s action lines consistently
    # quote their row as ``(<repo_name> #<issue_number>...)`` — matching on
    # that exact ``"(repo #issue"`` prefix (rather than a bare
    # ``f"#{issue_number})"``/``f"#{issue_number},"`` substring) means issue
    # 1 can never match another row's ``#21,``/``#21)`` line.
    row_marker = f"({repo_name} #{issue_number}"
    mine = [a for a in actions if row_marker in a]
    marked_line = next((a for a in mine if a.startswith("mark merged")), None)
    if marked_line is not None:
        return MarkDoneResult(marked=True, detail=marked_line)
    detail = (
        mine[0]
        if mine
        else (
            f"{repo_name}#{issue_number}: reconcile found nothing to mark "
            "done — no candidate row, or its PR/issue does not read as "
            "terminal yet"
        )
    )
    return MarkDoneResult(marked=False, detail=detail)


class BounceResult(TypedDict):
    """:func:`sweep_bounce`'s return shape."""

    bounced: bool
    detail: str


def sweep_bounce(
    repo_name: str,
    issue_number: int,
    review_assignment_id: str,
    board: "Board",
    config: "Config",
) -> BounceResult:
    """Re-run the auto-loop's review-bounce fix dispatch for
    *review_assignment_id* (#3881).

    The same primitive `coord bounce` (``coord/commands/merge.py``) runs
    under the hood — :func:`coord.auto_loop.process_review_completion` —
    called directly rather than through the Click command, which this
    daemon-internal context never reaches (no TTY, no ``sys.exit``).
    Refuses (``bounced=False``) rather than dispatching anything when
    *review_assignment_id* does not resolve to a ``type="review"``
    assignment on *board*, or when its verdict is not
    ``"request-changes"``. Stricter than `coord bounce` on the second
    check: that command also accepts ``review_verdict is None`` (a review
    that has not posted a verdict at all), which this sweep-tier
    primitive deliberately refuses — a daemon-internal action with no
    human reading its output should not guess at a still-in-flight
    review's eventual verdict.

    Persists *board* via `coord.board_service.write_board` whenever
    `process_review_completion` produced an action kind in
    `coord.auto_loop.PERSIST_ACTION_KINDS` — the same persist obligation
    `coord bounce` itself discharges with its own narrower
    ``dispatched or terminal`` check (``coord/commands/merge.py``), and the
    reason that module-level tuple exists at all rather than living as a
    local in one caller (#1601/#1624): `process_review_completion` mutates
    `review.review_verdict` in memory on an approve/approved-with-nits path
    and `work.review_state` on the #522 terminal path, and either is lost
    unless the caller writes the board back.
    """
    authorize_sweep_action("bounce")

    review = board.find_by_id(review_assignment_id)
    if review is None or getattr(review, "type", None) != "review":
        return BounceResult(
            bounced=False,
            detail=f"{review_assignment_id!r} is not a review assignment on the board",
        )
    if getattr(review, "review_verdict", None) != "request-changes":
        return BounceResult(
            bounced=False,
            detail=(
                f"{review_assignment_id} verdict is "
                f"{review.review_verdict!r}, not 'request-changes' — "
                "nothing to bounce"
            ),
        )

    from coord.auto_loop import PERSIST_ACTION_KINDS, process_review_completion  # noqa: PLC0415
    from coord.board_service import write_board  # noqa: PLC0415
    from coord.state import COORD_DIR  # noqa: PLC0415

    machine = next(
        (m for m in config.machines if m.name == review.machine_name), None,
    )
    machine_host = machine.host if machine and getattr(machine, "host", None) else None
    local_log = COORD_DIR / "logs" / f"{review_assignment_id}.log"
    log_path = str(local_log) if local_log.exists() else None

    actions = process_review_completion(
        review, board, config, log_path=log_path, machine_host=machine_host,
    )
    if any(getattr(a, "kind", None) in PERSIST_ACTION_KINDS for a in actions):
        write_board(board)
    dispatched = any(getattr(a, "kind", None) == "fix_dispatched" for a in actions)
    detail = (
        f"{repo_name}#{issue_number} bounce re-dispatched a fix worker for "
        f"{review_assignment_id}"
        if dispatched
        else (
            f"{repo_name}#{issue_number} bounce made no fix dispatch for "
            f"{review_assignment_id}"
        )
    )
    return BounceResult(bounced=dispatched, detail=detail)


#: action name -> the daemon-internal handler that actually performs it.
#: Looked up by :func:`dispatch_sweep_action` only after
#: :func:`authorize_sweep_action` has already cleared the action — an
#: entry missing here for a name that IS on :data:`SAFE_TIER_ACTIONS`
#: (today: ``"merge"``, ``"ci_rerun"``) means THIS registry has no C3
#: dispatcher for it, not that the action is unsafe — ``"merge"`` already
#: has a working dispatcher reached a different way, through the #3881
#: investigator path (:func:`dispatch_investigator_action`/
#: :func:`investigator_merge`), which does not consult this dict at all.
_HANDLERS: dict[str, Callable[..., Any]] = {
    "drive_queue_add": sweep_drive_queue_add,
    "drive_queue_remove": sweep_drive_queue_remove,
    "drive_queue_requeue": sweep_drive_queue_requeue,
    "dispatch_fixit_leg": sweep_dispatch_fixit_leg,
    "dispatch_finisher_leg": sweep_dispatch_finisher_leg,
    "smoke_requeue": sweep_smoke_requeue,
    "raise_budget": sweep_raise_fixit_budget,
    "bounce": sweep_bounce,
    "restage": sweep_restage,
    "clear_latch": sweep_clear_latch,
    "mark_done": sweep_mark_done,
    "conflict_fix_redispatch": sweep_redispatch_head_unmoved_conflict_fix,
    "conflict_fix_dispatch": sweep_dispatch_conflict_fix,
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


# ── #3881: the investigator's eight closed verbs ─────────────────────────
#
# Every function below starts with `authorize_investigator_action`, ends by
# returning through `_investigator_result` (which audits unconditionally,
# success or refusal), and never raises for a refused precondition — only
# for a programmer error (an unknown *stage*) or `authorize_investigator_
# action` itself. Each re-checks its OWN precondition from live state
# immediately before calling the underlying `sweep_*` primitive, which in
# turn re-checks whatever ITS OWN docstring promises (#3881 design point 2:
# "re-reads live GitHub + board state immediately before acting") — belt
# and suspenders, not a duplicate question, because the investigator's
# precondition (e.g. "has this row ever had a raise-budget") and the
# primitive's (e.g. "is the row still removable") are different questions.


def investigator_requeue(
    repo_name: str,
    issue_number: int,
    board: "Board",
    *,
    reason: str,
    actor: str = "board_doctor_investigator",
) -> InvestigatorActionResult:
    """Take a stuck-but-approved row down and put it back exactly as
    declared (#3881) — the investigator's ``requeue`` verb.

    Refuses when :func:`_in_flight_work_detail` finds a running/pending
    assignment or a live `coord drive` session already against this row:
    a requeue mid-flight would yank the row out from under a leg that is
    actively using it.

    *actor* (#3887): see :func:`_investigator_result`.
    """
    authorize_investigator_action("requeue")

    in_flight = _in_flight_work_detail(repo_name, issue_number, board)
    if in_flight is not None:
        return _investigator_result(
            "requeue",
            repo_name,
            issue_number,
            acted=False,
            reason=reason,
            detail=in_flight,
            actor=actor,
        )

    result = sweep_drive_queue_requeue(repo_name, issue_number)
    return _investigator_result(
        "requeue",
        repo_name,
        issue_number,
        acted=result["requeued"],
        reason=reason,
        detail=result["detail"],
        actor=actor,
    )


def _live_review_red_with_findings(
    review_assignment_id: str | None, board: "Board", config: "Config",
) -> bool:
    """True when *review_assignment_id* resolves, live, to a real
    ``type="review"`` row on *board* with ``review_verdict ==
    "request-changes"`` AND a body that `coord.review.classify_
    blocking_evidence` does not confirm as free of blocking findings.

    An observation taken here, from the board and the review's own log —
    never the caller's say-so — because :func:`investigator_raise_budget`
    is gated on this being a live re-read (#3881 design point 2), not an
    assertion by the very agent the gate exists to check.

    Fails closed: no assignment id, an id that does not resolve to a
    review, a verdict other than ``"request-changes"``, or a body this
    machine cannot read (`classify_blocking_evidence`'s own `body=""`
    fail-closed case, same as every other blocking-findings caller in
    this codebase) all read as "findings not confirmed" in the direction
    that matters for THIS gate: unreadable means not positively eligible,
    not positively ineligible.
    """
    if not review_assignment_id:
        return False
    review = board.find_by_id(review_assignment_id)
    if review is None or getattr(review, "type", None) != "review":
        return False
    if getattr(review, "review_verdict", None) != "request-changes":
        return False

    from coord.auto_loop import _load_review_findings  # noqa: PLC0415
    from coord.review import classify_blocking_evidence  # noqa: PLC0415
    from coord.state import COORD_DIR  # noqa: PLC0415

    machine = next(
        (m for m in config.machines if m.name == review.machine_name), None,
    )
    machine_host = machine.host if machine and getattr(machine, "host", None) else None
    local_log = COORD_DIR / "logs" / f"{review_assignment_id}.log"
    log_path = str(local_log) if local_log.exists() else None

    findings = _load_review_findings(review, log_path, machine_host)
    if findings is None:
        return False
    return not classify_blocking_evidence(
        findings.body, assignment_id=review_assignment_id,
    ).confirmed_absent


def _live_ci_red_only(
    repo_name: str, issue_number: int, board: "Board", config: "Config", gh_ops: Any,
) -> bool:
    """True when *repo_name*#*issue_number*'s merge-queue entry has an
    approval bound to its current content (`coord.merge_queue.
    has_approved_review`) AND a live CI read reports at least one failing
    check — i.e. CI, never the review, is the reason this row would not
    merge right now.

    A live observation, not a caller-supplied flag: builds its own
    `coord.ci_store.CiStore` from *config* (the same `build_ci_store` call
    every merge call site makes) and asks it directly, rather than trust
    an ``--ci-red-only`` assertion from the very session being gated.
    """
    from coord.ci_store import build_ci_store, failed_checks  # noqa: PLC0415
    from coord.merge_queue import has_approved_review, load_queue  # noqa: PLC0415

    entry = next(
        (
            e
            for e in load_queue()
            if e.repo_name == repo_name and e.issue_number == issue_number
        ),
        None,
    )
    if entry is None or not entry.pr_number:
        return False
    if not has_approved_review(entry, board, gh_ops, config=config):
        return False

    ci_store = build_ci_store(
        config.ci_store.type, host=config.ci_store.host, token_env=config.ci_store.token_env,
    )
    if not ci_store.is_available:
        return False
    checks = ci_store.list_checks_for_pr(entry.repo_github, entry.pr_number)
    return bool(failed_checks(checks))


def investigator_raise_budget(
    repo_name: str,
    issue_number: int,
    board: "Board",
    config: "Config",
    gh_ops: Any,
    *,
    review_assignment_id: str | None = None,
    reason: str,
    increment: int = 2,
    actor: str = "board_doctor_investigator",
) -> InvestigatorActionResult:
    """Spend this row's ONE lifetime fix-round-ceiling bump (#3881) — the
    investigator's ``raise_budget`` verb.

    Two independent, both-required gates: the lifetime cap
    (`coord.state.board_doctor_fixit_rebudget_spent`, keyed by
    ``(repo_name, issue_number)`` alone so it survives a remove+add
    cycle), and the evidence that makes a bump safe rather than a blind
    relaunch — a live-read ``request-changes`` review with confirmed
    findings (:func:`_live_review_red_with_findings`), OR a live-read
    "CI is the only red gate" (:func:`_live_ci_red_only`). *review_
    assignment_id* names which review to check; both evidence functions
    re-derive their own answer from the board/CI backend THIS call reads,
    never from a boolean the caller merely asserts (#3881 design point 2:
    "re-reads live GitHub + board state immediately before acting") — the
    investigator session chooses which review to point at, the wrapper
    is the one that decides whether it actually qualifies.
    """
    authorize_investigator_action("raise_budget")

    from coord.state import (  # noqa: PLC0415
        board_doctor_fixit_rebudget_spent,
        record_board_doctor_fixit_rebudget_spent,
    )

    if board_doctor_fixit_rebudget_spent(repo_name, issue_number):
        return _investigator_result(
            "raise_budget",
            repo_name,
            issue_number,
            acted=False,
            reason=reason,
            detail=(
                f"{repo_name}#{issue_number} already spent its one "
                "lifetime raise-budget"
            ),
            actor=actor,
        )

    eligible = _live_review_red_with_findings(
        review_assignment_id, board, config,
    ) or _live_ci_red_only(repo_name, issue_number, board, config, gh_ops)
    if not eligible:
        return _investigator_result(
            "raise_budget",
            repo_name,
            issue_number,
            acted=False,
            reason=reason,
            detail=(
                f"{repo_name}#{issue_number} is neither a "
                "request-changes verdict with concrete findings nor "
                "CI-red-on-the-merge-ref-only — not eligible for a "
                "raise-budget"
            ),
            actor=actor,
        )

    result = sweep_raise_fixit_budget(repo_name, issue_number, config, increment=increment)
    if result["raised"]:
        record_board_doctor_fixit_rebudget_spent(repo_name, issue_number)
    return _investigator_result(
        "raise_budget",
        repo_name,
        issue_number,
        acted=result["raised"],
        reason=reason,
        detail=result["detail"],
        actor=actor,
    )


def investigator_bounce(
    repo_name: str,
    issue_number: int,
    review_assignment_id: str,
    board: "Board",
    config: "Config",
    *,
    reason: str,
    actor: str = "board_doctor_investigator",
) -> InvestigatorActionResult:
    """Re-run the auto-loop's review-bounce fix dispatch (#3881) — the
    investigator's ``bounce`` verb.

    #3881's design table scopes this to "after a raise-budget": refuses
    unless this row has already spent its lifetime raise-budget (see
    :func:`investigator_raise_budget`) — bouncing a row that never needed
    a fresh ceiling is ordinary auto-loop business, not an investigator
    action. :func:`sweep_bounce` then makes its own two checks (a real
    review assignment, verdict ``"request-changes"`` — stricter than
    `coord bounce` itself, which also accepts an in-flight review with no
    verdict posted yet) before dispatching.
    """
    authorize_investigator_action("bounce")

    from coord.state import board_doctor_fixit_rebudget_spent  # noqa: PLC0415

    if not board_doctor_fixit_rebudget_spent(repo_name, issue_number):
        return _investigator_result(
            "bounce",
            repo_name,
            issue_number,
            acted=False,
            reason=reason,
            detail=(
                f"{repo_name}#{issue_number} has not had a raise-budget "
                "yet — bounce is only for a row already given a fresh "
                "fix-round ceiling"
            ),
            actor=actor,
        )

    result = sweep_bounce(repo_name, issue_number, review_assignment_id, board, config)
    return _investigator_result(
        "bounce",
        repo_name,
        issue_number,
        acted=result["bounced"],
        reason=reason,
        detail=result["detail"],
        actor=actor,
    )


def investigator_integration_fix(
    repo_name: str,
    issue_number: int,
    board: "Board",
    config: "Config",
    *,
    sibling_repo: str,
    sibling_issue: int,
    cause: str,
    reason: str,
    evidence: tuple[str, ...] = (),
    actor: str = "board_doctor_investigator",
) -> InvestigatorActionResult:
    """Dispatch the base-merged fix leg against *sibling_repo*#
    *sibling_issue* — the issue tracking the broken base/integration this
    row's own PR cannot land against — never against *repo_name*#
    *issue_number* itself (#3881, the investigator's ``integration_fix``
    verb).

    A thin re-target of :func:`sweep_dispatch_fixit_leg` — reuses its
    in-flight guard, its briefing shape and its audit write verbatim
    (via `authorize_sweep_action("dispatch_fixit_leg")`, reached through
    :data:`_INVESTIGATOR_ACTION_ALIAS`), so a sibling-issue fix leg reads
    on the board exactly like any other fix-it leg dispatch.

    Consults `coord.drive_queue.fix_round_ceiling_blocked_for_issue` for
    the SIBLING row before dispatching — "#3756: the ONE lookup every
    dispatcher must use" (vimcode#2018: an independent dispatcher pacing
    itself off a different counter produced ~14 legs/$31 overnight). A
    sibling that already gave up on the #2972 fix-round ceiling refuses
    here with ``acted=False``, the same fallback-to-alert posture
    `coord.board_doctor_policy._investigate` applies around
    :func:`sweep_dispatch_fixit_leg` directly.
    """
    authorize_investigator_action("integration_fix")

    from coord.drive_queue import fix_round_ceiling_blocked_for_issue  # noqa: PLC0415

    if fix_round_ceiling_blocked_for_issue(sibling_repo, sibling_issue):
        return _investigator_result(
            "integration_fix",
            repo_name,
            issue_number,
            acted=False,
            reason=reason,
            detail=(
                f"{sibling_repo}#{sibling_issue} already gave up on the "
                "#2972 fix-round ceiling — refusing to dispatch another "
                f"fix-it leg against it (integration fix for {repo_name}#"
                f"{issue_number})"
            ),
            actor=actor,
        )

    result = dispatch_sweep_action(
        "dispatch_fixit_leg",
        sibling_repo,
        sibling_issue,
        board,
        config,
        cause=cause,
        evidence=evidence,
    )
    acted = bool(result["dispatched"])
    detail = (
        f"{result['detail']} (integration fix for {repo_name}#{issue_number}, "
        f"via {sibling_repo}#{sibling_issue})"
    )
    return _investigator_result(
        "integration_fix",
        repo_name,
        issue_number,
        acted=acted,
        reason=reason,
        detail=detail,
        actor=actor,
    )


def investigator_restage(
    repo_name: str,
    issue_number: int,
    board: "Board",
    config: "Config",
    *,
    stage: str,
    reason: str,
    actor: str = "board_doctor_investigator",
) -> InvestigatorActionResult:
    """Re-dispatch a missing/unreadable Test or Review stage (#3881) —
    the investigator's ``restage`` verb. *stage* is ``"test"`` or
    ``"review"``; see :func:`sweep_restage`."""
    authorize_investigator_action("restage")

    result = sweep_restage(repo_name, issue_number, board, config, stage=stage)
    return _investigator_result(
        "restage",
        repo_name,
        issue_number,
        acted=result["restaged"],
        reason=reason,
        detail=result["detail"],
        actor=actor,
    )


def investigator_clear_latch(
    repo_name: str,
    issue_number: int,
    board: "Board",
    config: "Config",
    gh_ops: Any,
    *,
    reason: str,
    actor: str = "board_doctor_investigator",
) -> InvestigatorActionResult:
    """Re-test a stale CONFLICT/HUMAN_REQUIRED merge-queue latch (#3881)
    — the investigator's ``clear_latch`` verb. See :func:`sweep_clear_latch`
    for the three gates (latched, approval bound to current content, live
    mergeability) this requires before clearing anything."""
    authorize_investigator_action("clear_latch")

    result = sweep_clear_latch(repo_name, issue_number, board, config, gh_ops)
    return _investigator_result(
        "clear_latch",
        repo_name,
        issue_number,
        acted=result["cleared"],
        reason=reason,
        detail=result["detail"],
        actor=actor,
    )


def investigator_merge(
    repo_name: str,
    issue_number: int,
    board: "Board",
    config: "Config",
    gh_ops: Any,
    *,
    reason: str,
    actor: str = "board_doctor_investigator",
    allow_fix_dispatch: bool = True,
) -> InvestigatorActionResult:
    """Plain ``coord merge --only`` of an already-approved, CI-green row
    (#3881) — the investigator's ``merge`` verb, authorized under the
    identical ``"merge"`` :data:`SAFE_TIER_ACTIONS` name, with no
    :data:`_INVESTIGATOR_ACTION_ALIAS` indirection needed.

    *allow_fix_dispatch* gates the post-``process()`` fan-out below
    (a fresh conflict or CI failure dispatching a conflict-fix/ci-fix
    worker leg, and the sibling-conflict sweep's own leg dispatch) —
    defaults ``True`` for the human-attended `coord drive-queue doctor-act`
    CLI's call site: an operator who invoked this verb directly is
    accountable for whatever it dispatches, exactly like
    `coord merge --only` itself. `coord.board_doctor_policy.
    _merge_pending_stuck_row` — the UNATTENDED mechanical-tier caller —
    passes ``False``: that cause (:data:`coord.board_doctor_policy.
    CAUSE_MERGE_PENDING_STUCK`) is on the C2 allowlist, never routed
    through :class:`coord.board_doctor_policy.InvestigateCapacityGate` or
    its `fixit_leg_budget`, so letting it start worker legs of its own
    would be an UNGATED fan-out from a tier that is supposed to be
    mechanical-only. With it ``False``, this still lands the merge itself
    and persists the entry; it just never dispatches a new leg on the
    strength of what that merge's own live gates turned up — any fresh
    conflict/CI failure is left for the ordinary C1 investigate tier (which
    IS capacity-gated) or a later sweep tick to pick up.

    Precondition — approval bound to the current branch content
    (:func:`coord.merge_queue.has_approved_review`) — is checked here;
    CI-green and "merge gate reads clear" are re-confirmed, live, by
    :func:`coord.merge_queue.process` itself the moment it runs (a
    ``checks_failed``/``checks_pending``/``conflict`` event is exactly
    "the precondition no longer holds", reported as ``acted=False``,
    never treated as this call's own failure) — but only once `process()`
    is actually handed a real CI backend: a config-built
    :func:`coord.ci_store.build_ci_store`, the same one every other merge
    call site (`coord merge`, `coord merge --only`) constructs, never the
    `NoOpCi` `process()` falls back to when no store is passed, which
    reports ``is_available=False`` and so never runs the CI gate at all.

    Mirrors the ``--only`` surgical path's post-`process()` bookkeeping,
    not just its `process()` call, WHEN ``allow_fix_dispatch`` is ``True``:
    a fresh ``conflict`` or confirmed ``checks_failed`` event here still
    needs :func:`coord.commands.merge._dispatch_conflict_fixes` /
    :func:`coord.commands.merge._dispatch_ci_fixes` to escalate or dispatch
    a bounded fix, and a merge that lands still needs
    :func:`coord.commands.merge._sweep_sibling_conflicts` to park any
    sibling PR GitHub just broke — the same three steps `--only` runs
    before its own save (``coord/commands/merge.py``'s ``merge()`` body).
    With ``allow_fix_dispatch=False`` all three are skipped wholesale,
    including `_sweep_sibling_conflicts`' own non-dispatch half (parking a
    broken sibling at ``CONFLICT`` with an error naming the merge that
    broke it) — a mechanically-merged row's siblings keep reading as
    whatever they read before this merge until the next ordinary
    `coord merge` tick, or a later sweep, re-derives their true state.

    Persists by re-loading the queue and merging *entry*'s own mutated
    state back over it (the ``--only`` path's own save shape), never by
    writing back the whole pre-``process()`` snapshot this call started
    with — a concurrent writer's row added or changed during the
    (possibly minutes-long) ``process()`` call is preserved, not reverted.

    ``acted`` is `coord.merge_queue.MERGE_LANDED_KINDS` applied to the
    events `process()` returned — the one answer `process()` itself gives
    to "did this land" — never a second, locally-invented tuple of kinds
    that can drift out of the lockstep `MERGE_LANDED_KINDS` already keeps
    with `process()`'s own source (#2096).
    """
    authorize_investigator_action("merge")

    from coord.ci_store import build_ci_store  # noqa: PLC0415
    from coord.commands.merge import (  # noqa: PLC0415
        _dispatch_ci_fixes,
        _dispatch_conflict_fixes,
        _sweep_sibling_conflicts,
    )
    from coord.merge_queue import (  # noqa: PLC0415
        MERGE_LANDED_KINDS,
        PENDING,
        has_approved_review,
        load_queue,
        process,
        save_queue,
    )

    queue = load_queue()
    entry = next(
        (e for e in queue if e.repo_name == repo_name and e.issue_number == issue_number),
        None,
    )
    if entry is None:
        return _investigator_result(
            "merge",
            repo_name,
            issue_number,
            acted=False,
            reason=reason,
            detail=f"{repo_name}#{issue_number} has no merge-queue entry",
            actor=actor,
        )
    if entry.state != PENDING:
        return _investigator_result(
            "merge",
            repo_name,
            issue_number,
            acted=False,
            reason=reason,
            detail=(
                f"{repo_name}#{issue_number} merge-queue entry is "
                f"{entry.state!r}, not pending"
            ),
            actor=actor,
        )
    if not has_approved_review(entry, board, gh_ops, config=config):
        return _investigator_result(
            "merge",
            repo_name,
            issue_number,
            acted=False,
            reason=reason,
            detail=(
                f"{repo_name}#{issue_number} has no approval bound to the "
                "current branch content — refusing to merge"
            ),
            actor=actor,
        )

    ci_store = build_ci_store(
        config.ci_store.type,
        host=config.ci_store.host,
        token_env=config.ci_store.token_env,
    )
    events = process(
        [entry], gh_ops, config=config, board=board, ci_store=ci_store,
        skip_review=False, skip_smoke=False, force_merge=False,
    )
    # Same ordering as the `--only` path: classify a fresh conflict/
    # confirmed-CI-failure event, and sweep any sibling the merge just
    # broke, BEFORE the save below — each mutates `entry`/its siblings'
    # `.state` in place, and the save has to pick those mutations up.
    # #3902: every one of these three can start a NEW worker leg
    # (conflict-fix, ci-fix, or a sibling's own conflict-fix) — skip all
    # three when *allow_fix_dispatch* is False, so the mechanical sweep's
    # ungated call site lands the merge alone and never fans out.
    if allow_fix_dispatch:
        _dispatch_conflict_fixes(events, config, dry_run=False, gh_ops=gh_ops)
        _dispatch_ci_fixes(events, config, ci_store, dry_run=False)
        _sweep_sibling_conflicts(events, [entry], config, gh_ops, dry_run=False)
    # Persist only `entry` back over the current on-disk queue — never the
    # whole pre-`process()` snapshot this call started with, which could by
    # now be stale against a concurrent writer.
    fresh_queue = load_queue()
    by_id = {entry.assignment_id: entry}
    save_queue([by_id.get(x.assignment_id, x) for x in fresh_queue])
    merged_event = next((ev for ev in events if ev.kind in MERGE_LANDED_KINDS), None)
    acted = merged_event is not None
    detail = (
        merged_event.message
        if merged_event is not None
        else (
            events[0].message
            if events
            else f"{repo_name}#{issue_number}: merge attempt produced no event"
        )
    )
    return _investigator_result(
        "merge",
        repo_name,
        issue_number,
        acted=acted,
        reason=reason,
        detail=detail,
        actor=actor,
    )


def investigator_mark_done(
    repo_name: str,
    issue_number: int,
    board: "Board",
    config: "Config",
    *,
    reason: str,
    actor: str = "board_doctor_investigator",
) -> InvestigatorActionResult:
    """Reconcile a row whose PR merged out-of-band and free its slot
    (#3881) — the investigator's ``mark_done`` verb. See
    :func:`sweep_mark_done`."""
    authorize_investigator_action("mark_done")

    result = sweep_mark_done(repo_name, issue_number, board, config)
    return _investigator_result(
        "mark_done",
        repo_name,
        issue_number,
        acted=result["marked"],
        reason=reason,
        detail=result["detail"],
        actor=actor,
    )


_INVESTIGATOR_DISPATCH: dict[str, Callable[..., InvestigatorActionResult]] = {
    "requeue": investigator_requeue,
    "raise_budget": investigator_raise_budget,
    "bounce": investigator_bounce,
    "integration_fix": investigator_integration_fix,
    "restage": investigator_restage,
    "clear_latch": investigator_clear_latch,
    "merge": investigator_merge,
    "mark_done": investigator_mark_done,
}


def dispatch_investigator_action(
    action: str, repo_name: str, issue_number: int, *args: Any, **kwargs: Any,
) -> InvestigatorActionResult:
    """The single entry point `coord drive-queue doctor-act` calls (#3881).

    Daemon-host only: this function calls
    :func:`coord.state._thin_client_local_board_guard` first, exactly like
    every other board-doctor state primitive this module's handlers reach
    into — an investigator session dispatched against a thin client would
    otherwise act on an empty/stale local board copy rather than the
    canonical one. That guard's own enforcement is weaker than "fires
    first" implies, though: it only raises under
    ``COORD_STRICT_LOCAL_BOARD=1``; otherwise it emits a ``UserWarning``
    and lets the call through. The actual hard refusal an operator sees
    is the `doctor-act` CLI command's own ``is_remote()`` ->
    ``click.ClickException`` check, ahead of this function — this guard
    is a second, library-level backstop for a caller that reaches this
    function directly (bypassing the CLI), not the primary enforcement.
    Then authorizes *action* against the closed :data:`INVESTIGATOR_
    ACTIONS` set (which also covers every permanently-forbidden name)
    before routing to its handler, so a disallowed verb never reaches this
    dict lookup at all.
    """
    from coord.state import _thin_client_local_board_guard  # noqa: PLC0415

    _thin_client_local_board_guard("board_doctor_investigator_action")
    authorize_investigator_action(action)
    handler = _INVESTIGATOR_DISPATCH[action]
    return handler(repo_name, issue_number, *args, **kwargs)
