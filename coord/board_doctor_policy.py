"""Board-doctor policy: the closed auto-action allowlist (C2) and the
escalation-alert dispatcher (C4).

`coord.board_doctor.sweep` is pure plumbing: given a `tier_of` callable
and dispatcher callbacks, it classifies, dispatches, tracks convergence and
logs, but it deliberately does not decide WHICH cause gets auto-acted on
(C2's job) or WHAT an alert actually does (C4's job) — see that module's
docstring. This module is where those two decisions live.

Why the allowlist below is NOT empty
-------------------------------------
A cause belongs on the auto-action allowlist only once there is a
known-safe, reviewed-in-advance fix for it — never as a pre-approved
guess. #3760 (C3) started this allowlist with two such causes; a third
(``CAUSE_SMOKE_NO_VERDICT``, #3810), a fourth
(``queue_diagnose.CAUSE_NEVER_DISPATCHED``, #3874) and three more
(``CAUSE_LATCH_STALE_MERGEABLE``/``CAUSE_MERGE_PENDING_STUCK``/
``CAUSE_CONFLICT_FIX_HEAD_UNMOVED``, #3887) have since joined it the same
way, each wired below via :func:`_register_auto_action`:

* ``CAUSE_CEILING_BLOCKED_MERGEABLE`` (minted by
  `coord.board_doctor._reclassify_ceiling_blocked`, never by
  `queue_diagnose` itself) — a row that already gave up on #2972's
  fix-round ceiling but reads completely clear live (review-approved,
  every check green, PR mergeable). ``drive_queue_requeue`` hands it a
  fresh fix-round budget without touching the PR itself, and without
  re-running Work/Test/Review.
* ``queue_diagnose.CAUSE_PR_MERGED`` — the row's PR already merged while
  it sat ``running``/``blocked``/``waiting`` (quadraui#2038); ``mark_done``
  (#3881's investigator wrapper, `actor="sweep"` — #3887) reconciles the
  stale assignment to ``status="merged"``, freeing its machine/concurrency
  slot without deleting the drive-queue row's own declaration.
* ``CAUSE_SMOKE_NO_VERDICT`` — a smoke leg that exhausted #2272's
  mute-leg retry budget without ever producing a parseable verdict.
  ``sweep_smoke_requeue`` clears it, bounded by its own
  `coord.smoke.BOARD_DOCTOR_REQUEUE_BUDGET` so a leg that keeps muting
  after each requeue escalates to a human instead of looping forever.
* ``queue_diagnose.CAUSE_NEVER_DISPATCHED`` — a `blocked`/`parked` row
  with no branch, no PR and no merge-queue row ever created for it
  (`coord.drive_queue.is_unreachable_wait_shape`), most often one whose
  only cause was an `after=` pre-req that has since landed. The SAME
  ``drive_queue_requeue`` action `CAUSE_CEILING_BLOCKED_MERGEABLE` uses is
  safe here too, and for the identical reason: nothing was ever
  dispatched, so taking the row down and putting it straight back as
  `waiting` re-runs nothing that already ran — it is the mechanical
  equivalent of the operator `remove` + `add` this shape has otherwise
  needed every time it has been hit. Because that safety argument is only
  as good as the "nothing was dispatched" reading behind it, and a stored
  diagnosis can be arbitrarily old (`coord.board_doctor.StuckRow.fresh`),
  :func:`_never_dispatched_still_true` re-confirms it from the board
  immediately before the requeue fires.
* ``CAUSE_LATCH_STALE_MERGEABLE`` (minted by `coord.board_doctor.
  _reclassify_stale_latch`, #3887) — a `HUMAN_REQUIRED` merge-queue latch
  whose approval is already confirmed bound to the current branch content
  (quadraui#2034). Never promoted from `CAUSE_MERGE_CONFLICT`: that cause
  is GitHub's own live `mergeable is False` reading, and approval alone
  cannot override live conflict evidence — a genuine conflict keeps that
  cause name and its own #3769 investigate-lane remediation. ``clear_latch``
  (#3881's wrapper) re-tests live mergeability before clearing anything; a
  latch that still refuses is a confirmed no-op, not a false success.
* ``CAUSE_MERGE_PENDING_STUCK`` (`coord.board_doctor.merge_queue_rows`) —
  a `PENDING` merge-queue entry that has sat past
  `coord.board_doctor.MERGE_PENDING_STALE_AFTER_SECONDS` since its last
  attempt — the ordinary merge tick keeps missing a row that already
  reads green/approved/mergeable. ``merge`` (#3881's wrapper, plain
  ``coord merge --only``) re-confirms every gate live via `coord.
  merge_queue.process()` itself.
* ``CAUSE_CONFLICT_FIX_HEAD_UNMOVED`` (`coord.board_doctor.
  merge_queue_rows`) — a `HUMAN_REQUIRED` row whose last conflict-fix
  completed clean but never moved the branch (#3871's own
  `CONFLICT_FIX_HEAD_UNMOVED_SIGNAL`). ``conflict_fix_redispatch``
  re-dispatches the ordinary conflict-fix within the SAME retry cap
  #3871's own synchronous completion-time redispatch already enforces —
  this is the periodic net for when that one-shot attempt had no capable
  machine right then, never a second or larger budget.
* ``CAUSE_AFTER_PREREQS_LANDED`` (`coord.board_doctor.
  _reclassify_after_prereqs_landed`, #3901 — #3887 case 4's remainder: the
  PR that shipped #3887 claimed this shape was already covered by
  #3874's `queue_diagnose.CAUSE_NEVER_DISPATCHED`, which is wrong — that
  cause requires `coord.drive_queue.is_unreachable_wait_shape` (no
  branch, no PR, no merge-queue row ever), and unconditionally EXCLUDES a
  row blocked on an `after=` verdict, which is exactly this shape's whole
  premise) — a `blocked`/`parked` row that WAS dispatched at least once
  whose stored block reason is still `coord.drive_queue._resolve_
  prereqs`'s own unsatisfiable-`after=` verdict even though every named
  pre-req now reads terminal. Dispatches through the SAME audited,
  in-flight-guarded ``"requeue"`` investigator verb the four #3887
  mechanical dispatchers already use (`coord.board_doctor_permissions.
  investigator_requeue`, ``actor="sweep"``) rather than the bare
  ``drive_queue_requeue`` primitive the OTHER two mechanical-requeue
  causes above call directly — unlike them, this cause's own precondition
  REQUIRES at least one assignment to exist (`coord.board_doctor.
  after_prereqs_landed_for_entry`), so the row can genuinely have live
  work in flight against it, which only `investigator_requeue`'s
  `_in_flight_work_detail` guard refuses. `coord.drive_queue.
  _reconcile_blocked_after` already resumes a LIVE `blocked` row matching
  this shape on every ordinary tick with no board-doctor involvement at
  all; this cause only ever matters for a row that tick's own walk is no
  longer reaching (e.g. one #3888's `no_progress_rows` had to rescue from
  a budget-exhausted, silently-dropped episode).
* ``queue_diagnose.CAUSE_MERGE_CONFLICT`` (#3918) — moved OFF the
  investigate tier onto this allow-list: a merge conflict already has a
  known-safe mechanical fix (the ordinary conflict-fix leg), so leaving it
  on the investigate tier only meant it competed with every other
  alert-eligible cause for the same scarce per-repo fix-it-leg budget
  (quadraui#2160: that competition deferred it for six consecutive hourly
  sweeps with nothing dispatched at all). ``conflict_fix_dispatch`` calls
  the SAME `coord.conflict_fix.dispatch_conflict_fix` primitive the
  reactive `coord merge` auto-drain tick already dispatches through the
  instant IT raises a conflict — this is the periodic net for a STANDING
  conflict that tick never revisits.

Every other mechanical cause stays off this allow-list until it has the
same kind of reviewed, narrowly-scoped evidence behind it — a wrong
auto-action default is worse than an extra, harmless alert — so
:data:`ALLOWLIST` growing beyond these entries is a deliberate,
one-at-a-time decision, not a bulk promotion of a whole existing cause
bucket.

:func:`_register_auto_action` also checks every new entry's action name
against `coord.board_doctor_permissions.SAFE_TIER_ACTIONS` (#3745's C2
enforcement boundary) before adding it, and :func:`policy_tier_of` checks
it again at read time — so this allow-list can never promote a cause to
auto-tier for an action that module's own closed list does not permit.

Why alert-eligible causes go through the investigate tier (#3769)
-------------------------------------------------------------------
A cause with no known-safe mechanical fix still does not have to jump
straight to paging a human. :data:`INVESTIGATE_CAUSES` names the causes
that instead get a fix-it leg dispatched against them
(`coord.board_doctor_permissions.sweep_dispatch_fixit_leg`, #3768's C1) —
registered into :data:`AUTO_DISPATCHERS` the same way as the mechanical
two, but kept out of :data:`ALLOWLIST`, which is reserved for fixes that
touch the queue row itself rather than spawning a worker leg.
:func:`_investigate` is the one handler behind every entry: it builds the
board/config the primitive needs, derives ``(repo, issue)`` from the row
key, and dispatches. The primitive's own in-flight check (a running or
pending work-like assignment, or a live `coord drive` session) is the
"only investigate a row the drive loop has given up on" trigger guard —
reused as-is rather than re-derived here, so there is exactly one place
that answers "is this issue already being worked." The same call's
machine-routing decision (`coord.auto_loop._dispatch_fix` ->
`coord.dispatch.select_fix_machine`) is the "pin to a capable host or
refuse" guard; when it declines, :func:`_investigate` falls back to
:func:`escalation_alert_dispatcher` for that row instead of leaving it
silently unhandled.

`coord.board_doctor.sweep`'s own convergence/escalate-once machinery
(#3758) is untouched by any of this: a row that keeps re-diagnosing to
the same investigate-eligible cause for `CONVERGENCE_LIMIT` sweeps stops
being handed to :func:`_investigate` and falls to the alert tier exactly
once, the same as it already does for every :data:`TIER_AUTO` cause —
this module only decides WHICH causes reach that path as auto-tier, not
how the path itself behaves.

Why the investigate tier is capped per repo (#3770)
----------------------------------------------------
A sweep's `tier_of`/dispatch decisions run row-by-row with no shared view
of how many fix-it legs the sweep has already launched this pass — so an
unbounded number of :data:`INVESTIGATE_CAUSES` rows for the same repo
would otherwise all dispatch in one sweep. :func:`make_investigate_capacity_check`
builds `coord.board_doctor.sweep`'s optional ``capacity_check`` gate as a
:class:`InvestigateCapacityGate`: it bounds concurrent investigate-tier
legs per repo at a small, FIXED budget of its own
(:data:`FIXIT_LEG_BUDGET_DEFAULT`) — deliberately not a repo's
`max_parallel` ceiling, which is ordinary drive-queue throughput, not a
diagnostic-leg allowance, and sharing it means a repo whose queue is
already full of real work starves its own doctor at exactly the moment a
stuck row needs it. The gate counts legs already in flight
(:func:`_fixit_leg_in_flight_count` — a running/pending assignment whose
`assignment_id` this doctor itself recorded via the `fixit_leg_dispatched`
audit event, never ordinary queue work) plus whatever the gate has
CONFIRMED claimed earlier in the same sweep via
:meth:`InvestigateCapacityGate.confirm_dispatch`. A row the gate defers is
not dispatched, not escalated and does not touch the row's convergence
counter — it is simply deferred to the next sweep, which is why `sweep`
checks it before, not after, the counter bump. The gate only claims a
slot once a row's dispatch is confirmed (``action == "auto-dispatch"``),
never merely because the row was let through the check — so a row that
turns out to be a converged-noop, an internal alert-tier fallback, or an
in-flight no-op never burns a slot, and the repo keeps making progress
through its rows sweep over sweep instead of one never-dispatching row
starving the rest.

Why the alert path reuses `drive_escalations`
------------------------------------------------
"Issue filing" is not a literal new-GitHub-issue flow — `gh` is denied to
the sweep's own host context, and the fleet already has a first-class
"something needs a human" surface: the `drive_escalations` table
(`coord.state.record_drive_escalation`), already wired into the daemon
HTTP API, the board schema and the dashboard/reports, and already used
this same way by `coord.commands.drive_queue`'s roll-ledger escalation
(one question, one answer — a second filing mechanism would answer the
same question twice). :func:`escalation_alert_dispatcher` writes to
exactly that surface, with `proposed_command="coord status"`, so an
operator landing on the alert has the next action right there.

The finisher tier (Trigger A) ships gated the same two-switch shape as the
investigate tier above, via :func:`_finisher_tier_enabled` and
:func:`_at_repo_capacity` — see their own docstrings, and
:func:`dispatch_nit_only_finisher`'s, for the details and for how its
single-call-site shape differs from :class:`InvestigateCapacityGate`'s
per-sweep one.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

_log = logging.getLogger(__name__)

from coord import queue_diagnose
from coord.board_doctor import (
    ALERT_SKIPPED,
    CAUSE_AFTER_PREREQS_LANDED,
    CAUSE_BASELINE_RED,
    CAUSE_CEILING_BLOCKED_MERGEABLE,
    CAUSE_CONFLICT_FIX_HEAD_UNMOVED,
    CAUSE_LATCH_STALE_MERGEABLE,
    CAUSE_MERGE_NEVER_ATTEMPTED,
    CAUSE_MERGE_PENDING_STUCK,
    CAUSE_NEXT_ACTION_UNPERFORMED,
    CAUSE_SMOKE_NO_VERDICT,
    CAUSE_STRANDED_PREREQ,
    TIER_ALERT,
    TIER_AUTO,
    StuckRow,
    after_prereqs_landed_for_entry,
    stranded_prereq_dep,
    stranded_prereq_guard,
)
from coord.board_doctor_permissions import (
    INVESTIGATOR_ACTIONS,
    SAFE_TIER_ACTIONS,
    SweepPermissionError,
    SweepWriteNotConfirmedError,
    authorize_sweep_action,
    dispatch_investigator_action,
    dispatch_sweep_action,
)
from coord.drive_queue import parse_key as _parse_row_key
from coord.queue_diagnose import DiagnosisOption, EnvProbeResult

if TYPE_CHECKING:  # pragma: no cover — typing only
    from coord.board_doctor_permissions import InvestigatorActionResult
    from coord.config import Config
    from coord.models import Assignment, Board, Machine, Repo

#: A fix-it-leg dispatch is already in flight for this row — both
#: `coord.board_doctor_permissions.sweep_dispatch_fixit_leg` detail strings
#: for "don't dispatch a second one" (a running/pending work-like
#: assignment, or a live `coord drive` session) share this substring, so
#: :func:`_investigate` can tell "leave it alone, the drive loop is
#: already on it" apart from the two detail strings that mean "nothing is
#: in flight and the leg still didn't start" without re-deriving either
#: check itself (#3769).
_FIXIT_LEG_IN_FLIGHT_MARKER = "already in flight"

#: The one `coord.board_doctor_permissions.SAFE_TIER_ACTIONS` action name
#: the investigate tier dispatches — named once here rather than as a
#: string literal repeated in :func:`policy_tier_of`,
#: :func:`_register_investigate_action` and :func:`_investigate`'s own
#: `dispatch_sweep_action` call.
_INVESTIGATE_ACTION = "dispatch_fixit_leg"

#: The `coord.board_doctor_permissions.SAFE_TIER_ACTIONS` action name a
#: future finisher-tier cause handler dispatches (#3832's C3/C4), and the one
#: :func:`dispatch_nit_only_finisher` already dispatches today (#3835,
#: `CAUSE_NIT_ONLY_REQUEST_CHANGES`) — named here once so both reference one
#: constant rather than a repeated string literal. #3835's call is
#: synchronous, firing straight from `coord.auto_loop.process_review_
#: completion` at the moment a review completes, so it still registers
#: nothing into `ALLOWLIST`/`AUTO_DISPATCHERS`/`INVESTIGATE_CAUSES` or
#: `policy_tier_of` — those three are exclusively for causes the *periodic*
#: sweep discovers and auto-dispatches on its own, which a future C3/C4
#: handler would still need to wire up.
_FINISHER_ACTION = "dispatch_finisher_leg"

#: The `coord.board_doctor_permissions.SAFE_TIER_ACTIONS` action name
#: :func:`dispatch_mechanic_leg_for_triage` dispatches (#4094) — mirrors
#: :data:`_FINISHER_ACTION` exactly: a synchronous call from a future
#: Test-stage caller (the place `coord.drive` decides a Test leg failed and
#: hands the failure to `coord.failure_triage.triage_failure`), never the
#: periodic sweep, so it registers nothing into `ALLOWLIST`/
#: `AUTO_DISPATCHERS`/`INVESTIGATE_CAUSES`/`policy_tier_of` either.
_MECHANIC_ACTION = "dispatch_mechanic_leg"

#: cause -> auto-action name. Populated only via :func:`_register_auto_action`
#: — never by assigning into this dict directly, so every entry is also
#: checked against `coord.board_doctor_permissions.SAFE_TIER_ACTIONS` — the
#: one closed allow-list this module answers to, not a second copy of it.
#: See the module docstring for why this names exactly two causes today.
ALLOWLIST: dict[str, str] = {}

#: cause -> the C3 callable that actually performs the auto-action named in
#: :data:`ALLOWLIST`. Kept as a separate mapping (rather than trusting
#: :data:`ALLOWLIST` alone) so :func:`policy_tier_of` can never promote a
#: cause to :data:`coord.board_doctor.TIER_AUTO` on the strength of an
#: allowlist entry with no real dispatcher behind it — that combination
#: would silently do nothing while still suppressing the alert tier's own
#: escalation for the cause. Each handler returns ``True`` only once its
#: action is CONFIRMED to have done something; a falsy return means the
#: underlying primitive reported a no-op, and :func:`coord.board_doctor.sweep`
#: must not log that as a successful auto-dispatch.
AUTO_DISPATCHERS: dict[str, Callable[[StuckRow], bool]] = {}

#: Causes routed to the investigate tier (#3769) rather than either
#: :data:`ALLOWLIST`'s mechanical fixes or straight to the alert tier.
#: Populated only via :func:`_register_investigate_action`, kept as its
#: own set (never folded into :data:`ALLOWLIST`) because these causes have
#: no known-safe fix for the queue row itself — the module docstring's
#: "not a bulk promotion" rule for :data:`ALLOWLIST` still applies to
#: THAT dict; this is a separate decision about a separate action
#: (``dispatch_fixit_leg``) with its own, already-conservative built-in
#: no-ops (#3768's in-flight lock and machine-routing refusal).
INVESTIGATE_CAUSES: set[str] = set()

#: Causes routed to the decomposition act tier — a THIRD, separate bucket
#: from :data:`ALLOWLIST` and :data:`INVESTIGATE_CAUSES`, because dispatching
#: a read-only decomposition-followup leg and filing the split it proposes
#: is not on `coord.board_doctor_permissions.SAFE_TIER_ACTIONS` at all —
#: that closed allow-list governs actions which mutate a drive-queue/
#: merge-queue ROW; filing a split is a GitHub-issue-filing primitive with
#: its OWN, already-reviewed mechanical gate
#: (`coord.preflight_size.verify_split_proposal`), the same one
#: `coord.dispatch.enforce_preflight_guard` and
#: `coord.decomposition_chat.auto_split_needs_decomposition` already call
#: directly with no `SAFE_TIER_ACTIONS` entry of their own. The ONE
#: drive-queue-row mutation this tier does perform — removing the
#: superseded original once its split is confirmed filed — still goes
#: through `dispatch_sweep_action("drive_queue_remove", ...)`, the same
#: authorized primitive every other tier's row removal uses; only the
#: leg-dispatch/split-filing half answers to the separate mechanical gate
#: above. Populated only via :func:`_register_decomposition_action`.
DECOMPOSITION_CAUSES: set[str] = set()


def _register_auto_action(
    cause: str, action: str, handler: Callable[[StuckRow], bool]
) -> None:
    """The only way this module may populate :data:`ALLOWLIST` /
    :data:`AUTO_DISPATCHERS` — a future PR wiring a Phase-0-evidenced cause
    to a real C3 handler calls this instead of assigning into either dict
    directly.

    Calls `coord.board_doctor_permissions.authorize_sweep_action` twice
    over: once here, refusing to register *cause* at all if *action* is not
    on :data:`coord.board_doctor_permissions.SAFE_TIER_ACTIONS`; and again
    inside the wrapped dispatcher every time it actually runs, refusing the
    call even if some future edit mutated :data:`AUTO_DISPATCHERS` directly
    rather than going through this function. This keeps
    `SAFE_TIER_ACTIONS` the one allow-list :func:`policy_tier_of` and the
    sweep's dispatch path both ultimately answer to, rather than a second,
    independently-maintained judgment living in this module alone.
    """
    authorize_sweep_action(action)

    def _gated(row: StuckRow) -> bool:
        authorize_sweep_action(action)
        return handler(row)

    ALLOWLIST[cause] = action
    AUTO_DISPATCHERS[cause] = _gated


def _is_mechanical_route(cause: str) -> bool:
    """The #3760 mechanical route's own predicate (#4027 review, epic
    #2096/#2085 "one question, one answer") — *cause* present in BOTH
    :data:`ALLOWLIST` and :data:`AUTO_DISPATCHERS`, and the action
    :data:`ALLOWLIST` names for it is on ``SAFE_TIER_ACTIONS``.

    The ONE place that answers "is this cause on the mechanical route?" —
    :func:`policy_tier_of`'s first branch and :func:`mechanical_causes`
    both call this rather than each re-deriving the same conjunction from
    the same three structures. Without that, the two could silently
    diverge the moment the mechanical route gained a fourth term: a
    cause :func:`policy_tier_of` had moved to :data:`coord.board_doctor.
    TIER_ALERT` would stay in :func:`mechanical_causes`' result, and the
    tight-cadence sweep would then escalate it every minute instead of
    hourly.
    """
    action = ALLOWLIST.get(cause)
    return action is not None and cause in AUTO_DISPATCHERS and action in SAFE_TIER_ACTIONS


def policy_tier_of(cause: str) -> str:
    """#3745's C2 (extended by #3769): cause -> tier policy.

    Returns :data:`coord.board_doctor.TIER_AUTO` for a *cause* reaching
    auto-tier by any of three routes — the first two gated on the same
    safe-tier check so neither can promote a cause for an action
    `coord.board_doctor_permissions.SAFE_TIER_ACTIONS` does not permit;
    the third answers to its own, separate mechanical gate instead (see
    :data:`DECOMPOSITION_CAUSES`'s own docstring for why):

    * The #3760 mechanical route — :func:`_is_mechanical_route`.
    * The #3769 investigate route — *cause* present in BOTH
      :data:`INVESTIGATE_CAUSES` and :data:`AUTO_DISPATCHERS`, and
      ``"dispatch_fixit_leg"`` is on ``SAFE_TIER_ACTIONS``.
    * The #3986 decomposition route — *cause* present in BOTH
      :data:`DECOMPOSITION_CAUSES` and :data:`AUTO_DISPATCHERS`.

    Everything else — including an :data:`ALLOWLIST` entry naming an
    action outside the safe tier, which :func:`_register_auto_action` will
    already have refused to create but a direct-assignment bug could
    still produce — gets :data:`coord.board_doctor.TIER_ALERT`, the same
    as a cause with no entry at all. This is the function `coord
    drive-queue sweep` wires in as `tier_of`.
    """
    if _is_mechanical_route(cause):
        return TIER_AUTO
    if (
        cause in INVESTIGATE_CAUSES
        and cause in AUTO_DISPATCHERS
        and _INVESTIGATE_ACTION in SAFE_TIER_ACTIONS
    ):
        return TIER_AUTO
    if cause in DECOMPOSITION_CAUSES and cause in AUTO_DISPATCHERS:
        return TIER_AUTO
    return TIER_ALERT


def mechanical_causes() -> frozenset[str]:
    """The causes a tight-cadence sweep is safe to run unattended on.

    Exactly :func:`policy_tier_of`'s mechanical route
    (:func:`_is_mechanical_route`) — never the investigate or
    decomposition routes, which dispatch a worker leg or a split rather
    than a direct, pre-reviewed queue repair and stay on the hourly
    cadence's own budget and per-repo opt-in. Passed to
    :func:`coord.board_doctor.run_sweep` as *mechanical_causes* to narrow
    a sweep invocation to only this set.
    """
    return frozenset(cause for cause in ALLOWLIST if _is_mechanical_route(cause))


def _board_doctor_stage(queue: str) -> str:
    return f"board-doctor:{queue}"


#: ``"repo#issue"`` -> ``(repo, issue)``, or ``None`` if unparseable — this
#: module's own name for :func:`coord.drive_queue.parse_key`, which already
#: lives next to :func:`coord.drive_queue.entry_key` (the inverse, building
#: the same keys). Every C3/C4 reader of a ``row.key``-shaped string in this
#: module and in `coord.board_doctor` (:func:`escalation_alert_dispatcher`,
#: :func:`dismiss_resolved_escalation`,
#: :func:`coord.board_doctor._refresh_ceiling_blocked_settled_rows`,
#: :func:`coord.board_doctor._reclassify_ceiling_blocked`,
#: :func:`coord.board_doctor._withdraw_rows_for_blocked_streaks`) calls this
#: one alias (#3807) rather than re-deriving the split — before #3807 three
#: of those re-derived it inline with ``int()`` in a ``try``, which (unlike
#: ``str.isdigit()``) accepts a leading ``-``, so e.g. ``"repo#-5"`` parsed
#: on some paths and not others. Keeping this as a plain import (not a
#: wrapper function) means there is exactly one implementation in the
#: codebase, not two that happen to agree today.


def _requeue_ceiling_blocked_mergeable(row: StuckRow) -> bool:
    """#3760 Action 1: a ceiling-blocked-but-mergeable row gets a fresh budget.

    ``remove``+``add`` (:func:`coord.board_doctor_permissions.
    sweep_drive_queue_requeue`) re-stamps ``legs_at_enqueue`` from scratch
    while preserving the row's declared policy fields, so this resumes only
    the missing gate — it never re-runs Work/Test/Review, and it never
    touches the PR itself (no merge, no force-merge: that stays outside
    `SAFE_TIER_ACTIONS` entirely).

    This action IS the fix-it tier's lifetime one-shot auto-rebudget
    (#3847) for this cause — ``CAUSE_CEILING_BLOCKED_MERGEABLE`` only
    exists because the row already gave up on the #2972/#3454 fix-round
    ceiling, so every call here is already a rebudget attempt, gated by
    :func:`_fixit_rebudget_eligible` the same as :func:`_investigate`'s
    ceiling-bypass branch (#2096: one question, one answer — neither call
    site keeps its own judgment). A row already spent, or that no longer
    reads environmental, is left untouched here so it falls through to
    :func:`coord.board_doctor.sweep`'s own alert tier instead of looping.

    Returns ``True`` only when `sweep_drive_queue_requeue` confirms
    ``requeued=True`` — an unparseable row key, a not-yet-eligible row, or
    a reported no-op (row no longer queued, gate mid-flight refusal aside,
    since that raises instead) all return ``False`` so
    :func:`coord.board_doctor.sweep` records ``"auto-dispatch-noop"``
    rather than falsely claiming the action fired.
    """
    parsed = _parse_row_key(row.key)
    if parsed is None:
        return False
    repo_name, issue_number = parsed

    import click  # noqa: PLC0415

    if not _fixit_rebudget_eligible(row, repo_name, issue_number):
        click.echo(
            f"warning: board-doctor NOT rebudgeting {repo_name}#{issue_number} "
            "— it already spent its one lifetime fix-it auto-rebudget leg, "
            "or no longer reads as environmental rather than flailing "
            "(#3847); leaving the row for the alert tier instead of "
            "requeuing it again",
            err=True,
        )
        return False

    result = dispatch_sweep_action("drive_queue_requeue", repo_name, issue_number)
    if not result.get("requeued"):
        click.echo(
            f"warning: board-doctor requeue no-op for {repo_name}#"
            f"{issue_number}: {result.get('detail')}",
            err=True,
        )
        return False

    from coord.state import record_board_doctor_fixit_rebudget_spent  # noqa: PLC0415

    record_board_doctor_fixit_rebudget_spent(repo_name, issue_number)
    return True


def _resolve_board_and_config_for_mechanical_action(
    repo_name: str, issue_number: int, *, action: str,
):
    """Build the ``(board, config)`` pair the #3887 mechanical dispatchers
    below need to call :func:`coord.board_doctor_permissions.
    dispatch_investigator_action` — the same deferred-import, fail-soft
    shape :func:`_investigate` already uses for the SAME reason (this
    harness never threads a board/config through to an auto-dispatcher,
    only a bare :class:`~coord.board_doctor.StuckRow`).

    Returns ``None`` (after printing a warning) on a config load OR board
    read failure — never raises — so a caller treats that exactly like any
    other "precondition did not hold" no-op: the row stays for a later
    sweep (or, past the convergence limit, the alert tier) rather than
    this taking the whole sweep down.
    """
    import click  # noqa: PLC0415

    from coord.commands._common import ConfigError, resolve_and_load_config  # noqa: PLC0415
    from coord.state import build_board  # noqa: PLC0415

    try:
        config = resolve_and_load_config(None)
    except ConfigError as exc:
        click.echo(
            f"warning: board-doctor could not load config to {action} "
            f"{repo_name}#{issue_number}: {exc}",
            err=True,
        )
        return None
    try:
        board = build_board()
    except Exception as exc:  # noqa: BLE001 — a board-read failure is the
        # identical "can't act" case as a config-load failure above, and
        # must reach the same no-op return rather than propagate into the
        # unshielded dispatcher call site above (#3887).
        click.echo(
            f"warning: board-doctor could not read the board to {action} "
            f"{repo_name}#{issue_number}: {type(exc).__name__}: {exc}",
            err=True,
        )
        return None
    return board, config


#: #3887's own attribution for every `reason=` this module passes to
#: :func:`coord.board_doctor_permissions.dispatch_investigator_action` —
#: named once so the four mechanical dispatchers below never drift on the
#: wording, and so `coord audit`/`coord log` readers see one consistent
#: phrase for "the deterministic sweep decided this, not a human session".
_SWEEP_REASON = "board-doctor sweep: unambiguous queue-repair precondition held (#3887)"


def _warn_noop(action: str, repo_name: str, issue_number: int, detail: str) -> bool:
    """Warn-then-``False`` for a mechanical dispatcher's confirmed no-op.

    One shared copy of the shape each #3887 dispatcher below otherwise
    repeated with its own local ``import click`` + f-string — a confirmed
    no-op (the precondition no longer holds by the time this call
    actually ran) is never a false success.
    """
    import click  # noqa: PLC0415

    click.echo(
        f"warning: board-doctor {action} no-op for {repo_name}#{issue_number}: {detail}",
        err=True,
    )
    return False


def _run_mechanical_action(action: str, row: StuckRow, body: Callable[[], bool]) -> bool:
    """Run one #3887 mechanical dispatcher's *body*, never letting an
    uncaught exception escape to `coord.board_doctor.sweep`'s bare
    ``performed = dispatcher(row)`` call site (#3887, reintroducing
    #3805's whole-sweep abort otherwise).

    Every primitive the four dispatchers below reach —
    `coord.merge_queue.process` (via `investigator_merge`),
    `coord.merge_queue.reconcile_conflict_entries` (via
    `investigator_clear_latch`/`sweep_clear_latch`),
    `coord.reconcile.reconcile_board_merges` (via `investigator_mark_done`),
    `coord.conflict_fix.dispatch_conflict_fix` (via
    `sweep_redispatch_head_unmoved_conflict_fix`), and the strict-mode
    ``RuntimeError`` `coord.state._thin_client_local_board_guard` raises —
    is live-GitHub/network-bound, not local-DB-only, and `sweep` has no
    shield of its own at this call site: an uncaught exception here would
    abort the WHOLE sweep, leaving every row ordered after this one
    unclassified and `clear_absent_rows` never run. Reports the exception
    and returns ``False`` instead, the same "confirmed no-op, not a false
    success" posture :func:`_warn_noop` gives an ordinary refusal — the row
    simply stays for a later sweep (or, past the convergence limit, the
    alert tier).
    """
    import click  # noqa: PLC0415

    try:
        return body()
    except Exception as exc:  # noqa: BLE001 — see docstring: must never abort the sweep
        click.echo(
            f"warning: board-doctor {action} raised for {row.key}: "
            f"{type(exc).__name__}: {exc}",
            err=True,
        )
        return False


def _reconcile_merged_row(row: StuckRow) -> bool:
    """#3760 Action 4 / #3887 case 1: reconcile a row whose PR already
    merged while it sat ``running``/``blocked``/``waiting`` (quadraui#2038).

    `queue_diagnose`'s highest-priority rule already fires
    :data:`coord.queue_diagnose.CAUSE_PR_MERGED` straight off live PR state
    — no reclassification needed, unlike Action 1 — so this only has to
    call the SAME `mark_done` wrapper #3881's investigator session would
    (:data:`INVESTIGATOR_RECOMMENDED_ACTION`), with ``actor="sweep"``: it
    reconciles the STALE assignment on the board to ``status="merged"``
    (`coord.reconcile.reconcile_board_merges`), freeing whatever
    machine/concurrency slot still thought this work was running, without
    deleting the drive-queue row's own declaration or history the way a
    bare removal would.

    Returns ``True`` only when the wrapper confirms ``acted=True``. An
    unparseable key, a config/board load failure, or the wrapper's own
    refusal (no candidate row, or its PR/issue does not read as terminal
    yet) all report ``False`` so :func:`coord.board_doctor.sweep` records
    ``"auto-dispatch-noop"`` rather than a false success. Any OTHER
    exception — `reconcile_board_merges` is live-GitHub/network-bound — is
    caught by :func:`_run_mechanical_action` rather than aborting the sweep.
    """
    def _body() -> bool:
        parsed = _parse_row_key(row.key)
        if parsed is None:
            return False
        repo_name, issue_number = parsed

        resolved = _resolve_board_and_config_for_mechanical_action(
            repo_name, issue_number, action="mark_done",
        )
        if resolved is None:
            return False
        board, config = resolved

        result = dispatch_investigator_action(
            "mark_done", repo_name, issue_number, board, config,
            reason=_SWEEP_REASON, actor="sweep",
        )
        if not result["acted"]:
            return _warn_noop("mark_done", repo_name, issue_number, result["detail"])
        return True

    return _run_mechanical_action("mark_done", row, _body)


def _clear_stale_latch(row: StuckRow) -> bool:
    """#3887 case 2: clear a stale `HUMAN_REQUIRED` merge-queue latch whose
    approval is already confirmed bound to the current branch content
    (quadraui#2034).

    :data:`CAUSE_LATCH_STALE_MERGEABLE` is only minted by
    :func:`coord.board_doctor._reclassify_stale_latch` once that
    independent confirmation already holds, so this dispatcher's own job
    is just to call the SAME `clear_latch` wrapper #3881's investigator
    session would, with ``actor="sweep"`` — its own three gates (latched,
    approval bound to current content, live mergeability via
    `coord.merge_queue.reconcile_conflict_entries`) are re-checked live at
    this call, never trusted from the reclassifier's earlier read.

    Returns ``True`` only when the wrapper confirms ``acted=True`` — a
    latch that does not actually clear (live mergeability still refuses
    it) is a confirmed no-op, not a success. Any OTHER exception —
    `reconcile_conflict_entries` is live-GitHub/network-bound — is caught
    by :func:`_run_mechanical_action` rather than aborting the sweep.
    """
    def _body() -> bool:
        parsed = _parse_row_key(row.key)
        if parsed is None:
            return False
        repo_name, issue_number = parsed

        resolved = _resolve_board_and_config_for_mechanical_action(
            repo_name, issue_number, action="clear_latch",
        )
        if resolved is None:
            return False
        board, config = resolved

        from coord import github_ops as gh_ops  # noqa: PLC0415

        result = dispatch_investigator_action(
            "clear_latch", repo_name, issue_number, board, config, gh_ops,
            reason=_SWEEP_REASON, actor="sweep",
        )
        if not result["acted"]:
            return _warn_noop("clear_latch", repo_name, issue_number, result["detail"])
        return True

    return _run_mechanical_action("clear_latch", row, _body)


def _merge_pending_stuck_row(row: StuckRow) -> bool:
    """Merge a `PENDING` merge-queue row that already looks fully
    green/approved/mergeable but the ordinary merge tick keeps missing.
    Registered against two distinct causes with two distinct staleness
    preconditions:

    - :data:`CAUSE_MERGE_PENDING_STUCK` (#3887 case 5), which only fires
      (:func:`coord.board_doctor.merge_queue_rows`) once the entry has sat
      past :data:`coord.board_doctor.MERGE_PENDING_STALE_AFTER_SECONDS`
      since its last attempt.
    - :data:`coord.board_doctor.CAUSE_MERGE_NEVER_ATTEMPTED` (#3929), which
      fires with NO age bound at all — see that cause's own docstring for
      why a never-attempted entry has no "last attempt" to measure a delay
      against in the first place.

    Either way this calls the SAME `merge` wrapper #3881's investigator
    session would — plain ``coord merge --only`` of an already-approved row
    (`coord.board_doctor_permissions.investigator_merge`) — with
    ``actor="sweep"``. Every live gate (CI green, approval still bound to
    current content, mergeable) is re-confirmed by `coord.merge_queue.
    process()` itself the moment it runs; a fresh conflict or CI failure
    there is reported ``acted=False``, never treated as this call's own
    failure. Any OTHER exception — `process()` is live-GitHub/network-bound
    — is caught by :func:`_run_mechanical_action` rather than aborting the
    sweep.
    """
    def _body() -> bool:
        parsed = _parse_row_key(row.key)
        if parsed is None:
            return False
        repo_name, issue_number = parsed

        resolved = _resolve_board_and_config_for_mechanical_action(
            repo_name, issue_number, action="merge",
        )
        if resolved is None:
            return False
        board, config = resolved

        from coord import github_ops as gh_ops  # noqa: PLC0415

        # #3902: `allow_fix_dispatch=False` — this mechanical-tier cause is
        # never routed through `InvestigateCapacityGate` or the per-repo
        # `fixit_leg_budget` (it is on the C2 allowlist, not an investigate
        # cause), so it must not let `investigator_merge`'s own post-merge
        # fan-out (conflict-fix/ci-fix/sibling-conflict dispatch) start a
        # worker leg of its own on this call's behalf — see
        # `investigator_merge`'s docstring.
        result = dispatch_investigator_action(
            "merge", repo_name, issue_number, board, config, gh_ops,
            reason=_SWEEP_REASON, actor="sweep", allow_fix_dispatch=False,
        )
        if not result["acted"]:
            return _warn_noop("merge", repo_name, issue_number, result["detail"])
        return True

    return _run_mechanical_action("merge", row, _body)


def _restage_stale_gate_row(row: StuckRow, *, stage: str) -> bool:
    """#3929: re-dispatch a fresh review or test for a row the merge gate's
    own freshness check confirms is stale (:data:`coord.queue_diagnose.
    CAUSE_REVIEW_STALE` / :data:`coord.queue_diagnose.CAUSE_TEST_STALE`) —
    never the generic investigate-tier fix-it leg, which cannot fix a
    staleness problem by editing code.

    Calls the SAME ``restage`` wrapper #3881's investigator session would
    (`coord.board_doctor_permissions.sweep_restage`), with ``actor="sweep"``.
    Returns ``True`` only when the wrapper confirms ``restaged=True`` — no
    completed work assignment to restage against is a confirmed no-op, not
    a false success. Any OTHER exception — `coord.review.dispatch_review`/
    the smoke-requeue primitive are live-GitHub/network-bound — is caught
    by :func:`_run_mechanical_action` rather than aborting the sweep.
    """
    def _body() -> bool:
        parsed = _parse_row_key(row.key)
        if parsed is None:
            return False
        repo_name, issue_number = parsed

        resolved = _resolve_board_and_config_for_mechanical_action(
            repo_name, issue_number, action="restage",
        )
        if resolved is None:
            return False
        board, config = resolved

        result = dispatch_investigator_action(
            "restage", repo_name, issue_number, board, config,
            stage=stage, reason=_SWEEP_REASON, actor="sweep",
        )
        if not result["acted"]:
            return _warn_noop("restage", repo_name, issue_number, result["detail"])
        return True

    return _run_mechanical_action("restage", row, _body)


def _restage_review_stale_row(row: StuckRow) -> bool:
    """#3929: :func:`_restage_stale_gate_row` bound to ``stage="review"`` —
    the handler :func:`_register_auto_action` registers against
    :data:`coord.queue_diagnose.CAUSE_REVIEW_STALE`."""
    return _restage_stale_gate_row(row, stage="review")


def _restage_test_stale_row(row: StuckRow) -> bool:
    """#3929: :func:`_restage_stale_gate_row` bound to ``stage="test"`` —
    the handler :func:`_register_auto_action` registers against
    :data:`coord.queue_diagnose.CAUSE_TEST_STALE`. Reuses ``sweep_smoke_
    requeue`` under the hood (`sweep_restage`'s own dispatch for this
    stage), clearing the stale verdict's fields so it re-enters the
    ordinary Test-stage dispatch scan. Shares `coord.smoke.
    BOARD_DOCTOR_REQUEUE_BUDGET` with :data:`CAUSE_SMOKE_NO_VERDICT`'s own
    restage — a row that alternates between the two causes spends one
    shared budget for two different reasons, which is the existing #3810
    posture for that counter, not a new one this cause introduces."""
    return _restage_stale_gate_row(row, stage="test")


def review_fix_remedy_target(
    board: "Board", repo_name: str, issue_number: int,
) -> str | None:
    """#4117: the review assignment id whose own recorded ``request-
    changes`` verdict IS this issue's remedy — dispatch a fix FROM that
    review (:func:`coord.board_doctor_permissions.sweep_bounce`, the same
    primitive ``coord bounce`` runs), never another review. Returns
    ``None`` when the shape does not hold, which covers "nothing to fix"
    (no review at all, or the newest one approved/is still pending),
    "already handled" (a work-like leg has already been dispatched since
    that review — ITS own eventual review is the one still open, not this
    stale one), and "a review is already in flight for this row" (the
    newest review OVERALL — not just the newest *verdicted* one — has no
    verdict yet). That last case matters even though a pending review's
    own eventual next action is a mechanical no-op here: an older
    request-changes verdict must not look like the newest word on this
    row just because the review that superseded it hasn't posted yet, or
    never will (quadraui#2873's "verdict not propagated after an agent
    restart") — bouncing off it would dispatch a fix worker onto a branch
    a review leg is still actively reading, the same double-dispatch
    shape #476/#477 guard everywhere else.

    Reads *board* directly rather than consulting
    :func:`coord.entry_truth.reconcile_entry`'s ``next_action``: that
    derivation folds every "review required but not ok" cause into the
    SAME ``dispatch_review`` verdict — an ordinary review that simply
    hasn't run yet and one that ran and explicitly said no read
    identically — which is exactly the #4117 defect for a board-doctor
    remedy: the row's own review already ran and rejected the diff, so
    dispatching ANOTHER review just re-derives a conclusion the log
    already has, the same "not the fix" reasoning #1956 established for
    an unparseable verdict.
    """
    from coord.decomposition_chat import is_size_triage_review_target  # noqa: PLC0415
    from coord.gates import assignments_for_issue  # noqa: PLC0415
    from coord.models import WORK_LIKE_TYPES  # noqa: PLC0415

    rows = assignments_for_issue(board, repo_name, issue_number)
    reviews = [
        a
        for a in rows
        if a.type == "review"
        and not is_size_triage_review_target(a.review_target)
    ]
    if not reviews:
        return None
    # `assignments_for_issue` sorts oldest dispatch first.
    newest_review = reviews[-1]
    if newest_review.review_verdict is None:
        # The newest review for this row hasn't posted a verdict yet
        # (still running/pending, or a crashed leg whose verdict never
        # propagated) — an older request-changes verdict underneath it is
        # not this row's newest word, and bouncing off it would race the
        # in-flight review.
        return None
    if newest_review.review_verdict != "request-changes":
        return None
    newest_review_at = newest_review.dispatched_at or 0.0
    newer_work_exists = any(
        a.type in WORK_LIKE_TYPES and (a.dispatched_at or 0.0) > newest_review_at
        for a in rows
    )
    if newer_work_exists:
        return None
    return newest_review.assignment_id


def _perform_next_action_unperformed(row: StuckRow) -> bool:
    """#4045: re-derive *row*'s live :class:`~coord.entry_truth.EntryTruth`
    right before acting — never trusting :func:`coord.board_doctor.
    next_action_unperformed_rows`'s own classification, which can be a
    sweep or more stale — and perform whichever gated primitive its FRESH
    ``next_action`` names: ``restage`` (review), ``merge``, or
    ``mark_done``. Registered against :data:`coord.board_doctor.
    CAUSE_NEXT_ACTION_UNPERFORMED` under the nominal action name
    ``"restage"`` (#4045's own motivating quadraui#2744 case, a stuck
    ``dispatch_review``) — every branch below still re-authorizes its REAL
    action through :func:`coord.board_doctor_permissions.
    dispatch_investigator_action` (``authorize_investigator_action`` ->
    ``authorize_sweep_action``) before touching the board, so the nominal
    registration name never grants anything the real call would not
    already permit on its own (#2096: one question, one answer, asked
    again at the real call site, not a second judgment this module invents).

    ``NEXT_ACTION_DISPATCH_TEST`` is deliberately absent from the branches
    below, mirroring its exclusion from :func:`coord.board_doctor.
    next_action_unperformed_rows`'s own ``dispatchable`` set (see that
    function's docstring for why) — a fresh truth read reporting it falls
    straight to the "no longer dispatchable" branch, a confirmed no-op,
    the same as ``WAIT``/``ESCALATE``/``UNKNOWN``.

    Returns ``True`` only once the matching wrapper confirms it actually
    acted. A live truth that no longer names a dispatchable ``next_action``
    at all — reality moved on between classification and this call: an
    operator already acted (exactly the quadraui#2744 incident's own
    resolution, `coord review <leg>` run by hand), or a leg is now in
    flight — is a confirmed no-op, never a false success. Any OTHER
    exception — `reconcile_entry`, and every primitive the three verbs below
    reach, are live-GitHub/network-bound — is caught by
    :func:`_run_mechanical_action` rather than aborting the sweep.
    """
    def _body() -> bool:
        parsed = _parse_row_key(row.key)
        if parsed is None:
            return False
        repo_name, issue_number = parsed

        resolved = _resolve_board_and_config_for_mechanical_action(
            repo_name, issue_number, action="next_action_unperformed",
        )
        if resolved is None:
            return False
        board, config = resolved

        from coord import github_ops as gh_ops  # noqa: PLC0415
        from coord.entry_truth import (  # noqa: PLC0415
            NEXT_ACTION_DISPATCH_REVIEW,
            NEXT_ACTION_MARK_DONE,
            NEXT_ACTION_MERGE,
            reconcile_entry,
        )

        truth = reconcile_entry(board, config, repo_name, issue_number, gh_ops=gh_ops)

        if truth.next_action == NEXT_ACTION_DISPATCH_REVIEW:
            # #4117: `reconcile_entry` folds "no review has run yet" and "a
            # review ran and said request-changes" into the SAME
            # `dispatch_review` verdict — but when it's the latter, with
            # nothing dispatched since, the right remedy is a fix off that
            # review (`coord bounce`), never another review. Checked BEFORE
            # the generic `restage(review)` fallback below, which is now
            # reached only when `review_fix_remedy_target` finds no such
            # review to bounce.
            fix_target = review_fix_remedy_target(board, repo_name, issue_number)
            if fix_target is not None:
                action_name = "fix-from-review"
                from coord.board_doctor_permissions import (  # noqa: PLC0415
                    _in_flight_work_detail,
                )

                # #4117: `review_fix_remedy_target` already refuses when
                # the newest review itself is still unverdicted, but that
                # leaves the live-`coord drive` half uncovered — the one
                # question every OTHER sweep primitive that takes a row
                # down and dispatches a worker against it
                # (`sweep_dispatch_fixit_leg`, `investigator_requeue`) asks
                # before acting.
                in_flight = _in_flight_work_detail(
                    repo_name, issue_number, board, verb="a fix-from-review bounce",
                )
                if in_flight is not None:
                    return _warn_noop(action_name, repo_name, issue_number, in_flight)

                bounce_result = dispatch_sweep_action(
                    "bounce", repo_name, issue_number, fix_target, board, config,
                )
                acted = bool(bounce_result.get("bounced"))
                if not acted:
                    return _warn_noop(
                        action_name, repo_name, issue_number,
                        str(bounce_result.get("detail", "")),
                    )
                return True
            action_name = "restage(review)"
            result = dispatch_investigator_action(
                "restage", repo_name, issue_number, board, config,
                stage="review", reason=_SWEEP_REASON, actor="sweep",
            )
            acted = result["acted"]
        elif truth.next_action == NEXT_ACTION_MERGE:
            action_name = "merge"
            # #3902: `allow_fix_dispatch=False` — see `_merge_pending_
            # stuck_row`'s own comment on the identical call: this cause is
            # not routed through `InvestigateCapacityGate`/the per-repo
            # `fixit_leg_budget`, so `investigator_merge`'s own post-merge
            # fan-out must not spend a worker leg on this call's behalf.
            result = dispatch_investigator_action(
                "merge", repo_name, issue_number, board, config, gh_ops,
                reason=_SWEEP_REASON, actor="sweep", allow_fix_dispatch=False,
            )
            acted = result["acted"]
        elif truth.next_action == NEXT_ACTION_MARK_DONE:
            action_name = "mark_done"
            result = dispatch_investigator_action(
                "mark_done", repo_name, issue_number, board, config,
                reason=_SWEEP_REASON, actor="sweep",
            )
            acted = result["acted"]
        else:
            return _warn_noop(
                "next_action_unperformed", repo_name, issue_number,
                f"live gates now read next_action={truth.next_action!r} — "
                "no longer a dispatchable action; reality moved on since "
                "this row was classified",
            )

        if not acted:
            return _warn_noop(action_name, repo_name, issue_number, result["detail"])
        return True

    return _run_mechanical_action("next_action_unperformed", row, _body)


def _redispatch_head_unmoved_conflict_fix(row: StuckRow) -> bool:
    """#3887 case 3, pairs with #3871: re-dispatch the ordinary
    conflict-fix for a `HUMAN_REQUIRED` row whose last attempt completed
    clean but never moved the branch.

    Unlike the three dispatchers above, this calls `coord.
    board_doctor_permissions.dispatch_sweep_action` directly rather than
    the #3881 investigator wrapper: there is no human-attended verb for
    this shape (#3871's own completion-time redispatch is the ONLY other
    caller of the underlying primitive, and it is synchronous, not an
    operator-invoked action), so `sweep_redispatch_head_unmoved_
    conflict_fix` audits ``actor="sweep"`` itself rather than through
    `_investigator_result`.

    Returns ``True`` only when the primitive confirms ``redispatched=True``
    — "no capable machine right now" (the same reason #3871's own
    synchronous attempt may have declined) is a confirmed no-op, so the
    row stays eligible for another attempt next sweep, still within the
    SAME retry cap #3871 already enforces. Any OTHER exception —
    `coord.conflict_fix.dispatch_conflict_fix` is live-GitHub/network-bound
    — is caught by :func:`_run_mechanical_action` rather than aborting the
    sweep.
    """
    def _body() -> bool:
        parsed = _parse_row_key(row.key)
        if parsed is None:
            return False
        repo_name, issue_number = parsed

        resolved = _resolve_board_and_config_for_mechanical_action(
            repo_name, issue_number, action="conflict_fix_redispatch",
        )
        if resolved is None:
            return False
        board, config = resolved

        result = dispatch_sweep_action(
            "conflict_fix_redispatch", repo_name, issue_number, board, config,
        )
        if not result["redispatched"]:
            return _warn_noop(
                "conflict_fix_redispatch", repo_name, issue_number, result["detail"],
            )
        return True

    return _run_mechanical_action("conflict_fix_redispatch", row, _body)


def _dispatch_conflict_fix(row: StuckRow) -> bool:
    """#3918: mechanically dispatch the ordinary conflict-fix for a
    merge-queue entry GitHub already reports ``CONFLICT`` on, instead of
    routing `queue_diagnose.CAUSE_MERGE_CONFLICT` through the investigate
    tier's generic fix-it-leg budget.

    A merge conflict has a known-safe mechanical fix — the SAME
    `coord.conflict_fix.dispatch_conflict_fix` primitive the reactive
    `coord merge` auto-drain tick already dispatches through the instant
    IT raises a conflict — so it belongs on the C2 allow-list next to
    ``CAUSE_MERGE_PENDING_STUCK``'s ``"merge"`` action, not in
    :data:`INVESTIGATE_CAUSES` competing with every other cause for the
    per-repo fix-it-leg budget (quadraui#2160: that competition deferred
    this row for six consecutive hourly sweeps with no dispatch of any
    kind).

    Same shape as :func:`_redispatch_head_unmoved_conflict_fix` above —
    calls `coord.board_doctor_permissions.dispatch_sweep_action` directly,
    since this has no human-attended CLI verb either, so
    `sweep_dispatch_conflict_fix` audits ``actor="sweep"`` itself. Returns
    ``True`` only when the primitive confirms ``dispatched=True``; any
    decline (no merge-queue entry, the entry no longer reads ``CONFLICT``,
    no capable machine, one already in flight, or the retry cap already
    spent) is a confirmed no-op, leaving the row for the normal convergence
    counter to escalate if it recurs.
    """
    def _body() -> bool:
        parsed = _parse_row_key(row.key)
        if parsed is None:
            return False
        repo_name, issue_number = parsed

        resolved = _resolve_board_and_config_for_mechanical_action(
            repo_name, issue_number, action="conflict_fix_dispatch",
        )
        if resolved is None:
            return False
        board, config = resolved

        result = dispatch_sweep_action(
            "conflict_fix_dispatch", repo_name, issue_number, board, config,
        )
        if not result["dispatched"]:
            return _warn_noop(
                "conflict_fix_dispatch", repo_name, issue_number, result["detail"],
            )
        return True

    return _run_mechanical_action("conflict_fix_dispatch", row, _body)


def _requeue_smoke_no_verdict(row: StuckRow) -> bool:
    """#3760 Action 5: re-dispatch a smoke leg that produced no verdict (#2244).

    Mirrors :func:`_clear_merged_row`'s shape rather than
    :func:`_requeue_ceiling_blocked_mergeable`'s: `sweep_smoke_requeue`
    (`coord.board_doctor_permissions`) bounds its own retries via
    `coord.smoke.BOARD_DOCTOR_REQUEUE_BUDGET` and can raise
    `SweepWriteNotConfirmedError` if a clear it attempted does not read back
    confirmed — both land here as an honest ``False`` (``"auto-dispatch-noop"``)
    rather than a crash, same defensive handling as `SweepPermissionError`
    gets every other dispatch call in this module.

    Returns ``True`` only when `sweep_smoke_requeue` confirms
    ``requeued=True``. A row already at the requeue budget, a row whose
    `test_state` this already cleared (the next sweep no longer selects it
    at all, since :func:`coord.board_doctor.smoke_no_verdict_rows` re-reads
    live `test_state`), an unconfirmed write, or an unparseable key all
    report `"auto-dispatch-noop"` instead of a false success — the row stays
    selected so its convergence counter keeps climbing toward escalation.
    """
    parsed = _parse_row_key(row.key)
    if parsed is None:
        return False
    repo_name, issue_number = parsed
    import click  # noqa: PLC0415

    try:
        result = dispatch_sweep_action("smoke_requeue", repo_name, issue_number)
    except (SweepPermissionError, SweepWriteNotConfirmedError) as exc:
        click.echo(
            f"warning: board-doctor smoke-requeue refused for {repo_name}#"
            f"{issue_number}: {exc}",
            err=True,
        )
        return False
    if not result.get("requeued"):
        click.echo(
            f"warning: board-doctor smoke-requeue no-op for {repo_name}#"
            f"{issue_number}: {result.get('detail')}",
            err=True,
        )
        return False
    return True


def _never_dispatched_still_true(repo_name: str, issue_number: int) -> bool:
    """Does *repo_name*#*issue_number* still look never-dispatched right
    now — zero board assignments AND no live CI-pending park?

    Echoes its own `warning:` line (via `click`) for every ``False`` it
    returns, so :func:`_requeue_never_dispatched` never has to guess, or
    reprint, which of the several refusal shapes below actually fired.

    `queue_diagnose.CAUSE_NEVER_DISPATCHED` is minted from exactly the
    board-assignments half of this observation
    (`coord.queue_diagnose.LiveState.no_assignment_ever`, read off
    `coord.gates.build_gate_report`'s own `rows`), but a
    :class:`~coord.board_doctor.StuckRow` reaching an auto action does not
    have to carry a verdict THIS sweep computed: `coord.board_doctor.
    _episode_stuck_row` reconstructs a row from an episode's *stored*
    diagnosis, which can be arbitrarily old (``fresh=False``). Between that
    diagnosis and this call the row may well have been dispatched for real,
    and ``drive_queue_requeue`` — a remove plus a re-add — would then orphan
    a live branch/PR and re-dispatch Work on top of it.

    So this re-asks the one question the action depends on, against the same
    predicate `coord gates` uses (`coord.gates.assignments_for_issue`, via
    `coord.state.build_board`) rather than a second scan that could drift
    (#2096: one question, one answer). It answers only from a board read
    that SUCCEEDED: a read that raises is reported and treated as "cannot
    confirm", which refuses the requeue — never as "nothing is there".

    `assignments_for_issue` only ever scans `board.active` +
    `board.completed` (see that function's own docstring) — a dispatch
    whose assignment has since aged into the drive-queue's own archive
    table reads identically to "never happened" there, even though the
    row's LIVE drive-queue entry still carries a "CI running:"/"CI stuck:"
    `last_reason` naming the real PR CI is still (or again) working
    through. That text is written only by `coord.drive_queue.
    _reconcile_running`'s CI-pending park, which by construction never
    fires without a branch/PR to park on — stronger, more current evidence
    than the board scan above, so it is checked FIRST and wins outright:
    `drive_queue_requeue` is a remove-plus-re-add, and tearing down a row
    that is merely waiting on CI (not missing a dispatch) would orphan the
    live PR it already opened.
    """
    import click  # noqa: PLC0415

    from coord.gates import assignments_for_issue  # noqa: PLC0415
    from coord.merge_queue import is_ci_pending_reason  # noqa: PLC0415
    from coord.state import build_board, get_drive_queue_entry  # noqa: PLC0415

    try:
        live_entry = get_drive_queue_entry(repo_name, issue_number)
    except Exception as exc:  # noqa: BLE001 — a re-check, never a decision
        click.echo(
            f"warning: board-doctor could not read the drive-queue entry to "
            f"confirm {repo_name}#{issue_number} was never dispatched: "
            f"{type(exc).__name__}: {exc}",
            err=True,
        )
        return False
    if live_entry is not None and is_ci_pending_reason(
        str(live_entry.get("last_reason") or "")
    ):
        click.echo(
            f"warning: board-doctor NOT requeuing {repo_name}#{issue_number} "
            "— its live drive-queue entry already shows a \"CI running:\"/"
            "\"CI stuck:\" park, so a dispatch happened even though the "
            "board scan below would read as empty; leaving it for the "
            "alert tier rather than removing a row with a live PR behind it",
            err=True,
        )
        return False

    try:
        board = build_board()
    except Exception as exc:  # noqa: BLE001 — a re-check, never a decision
        click.echo(
            f"warning: board-doctor could not read the board to confirm "
            f"{repo_name}#{issue_number} was never dispatched: "
            f"{type(exc).__name__}: {exc}",
            err=True,
        )
        return False
    if assignments_for_issue(board, repo_name, issue_number):
        click.echo(
            f"warning: board-doctor NOT requeuing {repo_name}#{issue_number} "
            "— the board now shows at least one assignment for it, so it is "
            "no longer the never-dispatched row this cause was diagnosed "
            "from; leaving it for the alert tier rather than removing a row "
            "with live work behind it",
            err=True,
        )
        return False
    return True


def _requeue_never_dispatched(row: StuckRow) -> bool:
    """#3874: reset-to-waiting for a row that was never dispatched at all.

    `queue_diagnose.CAUSE_NEVER_DISPATCHED` already fires straight off
    `coord.drive_queue.is_unreachable_wait_shape(entry)` plus a confirmed
    `LiveState.no_assignment_ever` — no board-doctor reclassification
    needed, same as :func:`_clear_merged_row` — so this only has to put the
    row back. Reuses the SAME ``drive_queue_requeue`` primitive
    :func:`_requeue_ceiling_blocked_mergeable` dispatches (remove then
    re-add, declaration preserved): safe here for the same reason that
    action's own docstring gives — a row with nothing dispatched behind it
    has no Work/Test/Review run to lose by being taken down and put
    straight back as `waiting`.

    That safety argument holds only while "nothing was dispatched" is still
    true, and a stored diagnosis can be arbitrarily old, so
    :func:`_never_dispatched_still_true` re-reads it from the board
    immediately before dispatching. A row that now HAS an assignment (or
    whose board read failed) is left for the alert tier rather than
    requeued — `sweep_drive_queue_requeue` itself only refuses a mid-flight
    #1757 deploy gate, not live work, so this is the only check standing
    between a stale verdict and an orphaned PR.

    Returns ``True`` only when `sweep_drive_queue_requeue` confirms
    ``requeued=True``. A row that no longer reads as never-dispatched, one
    whose #1757 deploy gate is mid-flight (`SweepPermissionError`), or one
    no longer present/parseable, reports `"auto-dispatch-noop"` instead of
    a false success — same defensive handling every other dispatch call in
    this module gets.
    """
    parsed = _parse_row_key(row.key)
    if parsed is None:
        return False
    repo_name, issue_number = parsed
    import click  # noqa: PLC0415

    if not _never_dispatched_still_true(repo_name, issue_number):
        # `_never_dispatched_still_true` already echoed the specific reason
        # (board now shows an assignment, a live CI-pending park, or an
        # unreadable re-check) — nothing more to say here.
        return False

    try:
        result = dispatch_sweep_action("drive_queue_requeue", repo_name, issue_number)
    except SweepPermissionError as exc:
        click.echo(
            f"warning: board-doctor requeue refused for {repo_name}#"
            f"{issue_number}: {exc}",
            err=True,
        )
        return False
    if not result.get("requeued"):
        click.echo(
            f"warning: board-doctor requeue no-op for {repo_name}#"
            f"{issue_number}: {result.get('detail')}",
            err=True,
        )
        return False
    return True


def _requeue_after_prereqs_landed(row: StuckRow) -> bool:
    """#3901 (#3887 case 4): requeue a frozen `blocked`/`parked` row whose
    `--after` block cause no longer holds.

    Unlike :func:`_requeue_ceiling_blocked_mergeable`/:func:`_requeue_never_
    dispatched` (the two existing `drive_queue_requeue` callers, neither of
    which is wrapped), this one IS run through :func:`_run_mechanical_action`
    — its own live re-check reads the board (`coord.state.build_board`),
    the drive queue (`coord.state.list_drive_queue`) AND a fresh
    `coord.drive_queue.BoardView` (`coord.commands.drive_queue.
    _fetch_board_view_with_retry` — the SAME typed board
    :func:`coord.board_doctor.collect_rows` builds for its own no-progress
    pass, just a fresh fetch rather than a reused one: this call runs long
    after that sweep concluded, often on a different process entirely), any
    of which can raise the same live-GitHub/network-bound way the four
    #3887 dispatchers this module already shields do, so an uncaught
    exception here must not abort the whole sweep either.

    Re-derives the FULL eligibility — not just "did a dispatch happen since
    diagnosis" the way :func:`_never_dispatched_still_true` narrowly
    re-checks for its own cause — via :func:`coord.board_doctor.
    after_prereqs_landed_for_entry`, the SAME predicate
    :func:`coord.board_doctor._reclassify_after_prereqs_landed` used to mint
    this cause in the first place (#2096: one question, one answer). A
    stored diagnosis can be arbitrarily old, and unlike a merge-gate
    reading, an operator may have already resumed this exact row by hand
    (`coord drive-queue remove`+`add`, or simply waiting for the ordinary
    tick's own `_reconcile_blocked_after` to get there first) — either
    leaves the row no longer `blocked`/`parked`, which the predicate reads
    as "no longer this shape" and refuses rather than requeuing a row that
    does not need it.

    Dispatches through :func:`coord.board_doctor_permissions.
    dispatch_investigator_action`'s ``"requeue"`` verb — the SAME audited,
    in-flight-guarded wrapper the other three mechanical dispatchers in this
    module use — rather than calling `dispatch_sweep_action` on the bare
    `drive_queue_requeue` primitive directly. Two reasons that is not
    optional here, unlike the other two ``drive_queue_requeue`` callers in
    this module: (1) `investigator_requeue` is the only path that writes a
    `coord.audit.record_audit` row with `actor="sweep"`, which the issue's
    Expected #1 names and `sweep_drive_queue_requeue` itself never does; and
    (2) it refuses via `_in_flight_work_detail` when a running/pending
    assignment or a live `coord drive` session already exists for the row —
    the guard this cause's own precondition needs and the other two
    `drive_queue_requeue` callers do not, because THEIR safety argument
    (`_requeue_never_dispatched` requires zero assignments;
    `_requeue_ceiling_blocked_mergeable` acts on a row that already gave up
    on fix rounds) does not transfer to this cause, whose predicate
    (`after_prereqs_landed_for_entry`) requires at least one assignment to
    exist. Without that guard a row reading `blocked`/`parked` in the queue
    while its OWN work assignment still reads `running`/`pending` on the
    board — exactly the board/queue divergence this sweep exists to catch —
    would get yanked out from under the live leg using it.

    Returns ``True`` only when `investigator_requeue` confirms
    ``acted=True`` — preserving ``--machine``, ``--after``, position and
    fix-round state exactly like the other two callers of the underlying
    `drive_queue_requeue` primitive. An unparseable key, a row that no
    longer matches the live predicate, an in-flight refusal, or a reported
    no-op all report ``False`` so :func:`coord.board_doctor.sweep` records
    ``"auto-dispatch-noop"`` rather than a false success.
    """
    def _body() -> bool:
        parsed = _parse_row_key(row.key)
        if parsed is None:
            return False
        repo_name, issue_number = parsed

        import click  # noqa: PLC0415
        from coord.commands.drive_queue import _fetch_board_view_with_retry  # noqa: PLC0415
        from coord.drive_queue import entries_from_rows  # noqa: PLC0415
        from coord.state import build_board, list_drive_queue  # noqa: PLC0415

        board = build_board()
        entries_by_key = {e.key: e for e in entries_from_rows(list_drive_queue())}
        entry = entries_by_key.get(row.key)
        try:
            board_view = _fetch_board_view_with_retry()
        except Exception:  # noqa: BLE001 — advisory, see this function's own docstring
            board_view = None
        if entry is None or not after_prereqs_landed_for_entry(
            entry, entries_by_key, board, board_view
        ):
            click.echo(
                f"warning: board-doctor NOT requeuing {repo_name}#"
                f"{issue_number} — it no longer matches the frozen "
                "--after-prereqs-landed shape this cause was diagnosed "
                "from (an operator may have already resumed it, or a "
                "prereq's reading changed) — leaving it for the alert "
                "tier rather than requeuing a row that no longer needs it",
                err=True,
            )
            return False

        result = dispatch_investigator_action(
            "requeue", repo_name, issue_number, board,
            reason=_SWEEP_REASON, actor="sweep",
        )
        if not result["acted"]:
            return _warn_noop(
                "drive_queue_requeue", repo_name, issue_number, result["detail"],
            )
        return True

    return _run_mechanical_action("drive_queue_requeue", row, _body)


_register_auto_action(
    CAUSE_CEILING_BLOCKED_MERGEABLE,
    "drive_queue_requeue",
    _requeue_ceiling_blocked_mergeable,
)
_register_auto_action(
    CAUSE_AFTER_PREREQS_LANDED,
    "drive_queue_requeue",
    _requeue_after_prereqs_landed,
)
_register_auto_action(
    queue_diagnose.CAUSE_PR_MERGED,
    "mark_done",
    _reconcile_merged_row,
)
_register_auto_action(
    CAUSE_SMOKE_NO_VERDICT,
    "smoke_requeue",
    _requeue_smoke_no_verdict,
)
_register_auto_action(
    queue_diagnose.CAUSE_NEVER_DISPATCHED,
    "drive_queue_requeue",
    _requeue_never_dispatched,
)
# #3887 (#3875 ask A): the sweep applies these four unambiguous queue
# repairs itself, via the SAME #3881 investigator wrapper a human-attended
# session would use (`actor="sweep"` distinguishes the two in the audit
# trail) — no agent session, no `max_parallel` slot spent.
_register_auto_action(
    CAUSE_LATCH_STALE_MERGEABLE,
    "clear_latch",
    _clear_stale_latch,
)
_register_auto_action(
    CAUSE_MERGE_PENDING_STUCK,
    # Named "merge" — the SAME verb name `_merge_pending_stuck_row` →
    # `dispatch_investigator_action` → `investigator_merge` already
    # audits this action under (`investigator_merge`/
    # `investigator_merge_refused`), so `ALLOWLIST` and the audit trail
    # never disagree about this cause's action name.
    "merge",
    _merge_pending_stuck_row,
)
_register_auto_action(
    CAUSE_CONFLICT_FIX_HEAD_UNMOVED,
    "conflict_fix_redispatch",
    _redispatch_head_unmoved_conflict_fix,
)
_register_auto_action(
    queue_diagnose.CAUSE_MERGE_CONFLICT,
    "conflict_fix_dispatch",
    _dispatch_conflict_fix,
)
# #3929: a row the merge gate's own freshness re-check confirms is stale
# gets a fresh review/test, never the generic investigate-tier fix-it leg
# (which cannot repair staleness by editing code) — see
# `_restage_stale_gate_row`'s docstring.
_register_auto_action(
    queue_diagnose.CAUSE_REVIEW_STALE,
    "restage",
    _restage_review_stale_row,
)
_register_auto_action(
    queue_diagnose.CAUSE_TEST_STALE,
    "restage",
    _restage_test_stale_row,
)
# #3929: a never-attempted-but-otherwise-clear merge row reuses the EXACT
# same "merge" action/handler `CAUSE_MERGE_PENDING_STUCK` already does —
# the dispatcher itself never branches on which cause named the row, only
# on the row's own key, so one handler legitimately serves both causes.
_register_auto_action(
    CAUSE_MERGE_NEVER_ATTEMPTED,
    "merge",
    _merge_pending_stuck_row,
)
# #4045: a row whose live gates already name a dispatchable `next_action`
# that nothing has performed — `_perform_next_action_unperformed` re-derives
# that `next_action` fresh and routes to the matching `restage`/`merge`/
# `mark_done` primitive itself; see its own docstring for why "restage" is
# the nominal registration name here.
_register_auto_action(
    CAUSE_NEXT_ACTION_UNPERFORMED,
    "restage",
    _perform_next_action_unperformed,
)


def _queue_stranded_prereq(row: StuckRow) -> bool:
    """#3944: enqueue the pre-req a `waiting` row is stranded on.

    `CAUSE_STRANDED_PREREQ` only ever names a row `coord.board_doctor.
    stranded_prereq_rows` already confirmed, for THIS sweep, both (a)
    stuck on exactly the "open, not queued" `after=` shape and (b) cleared
    by `coord.board_doctor.stranded_prereq_guard` to auto-queue — so this
    dispatcher's own job is narrower than re-deriving either: settle,
    re-confirm the shape is still live, enqueue, audit.

    Settle (the issue's own requirement): only acts once `coord.
    board_doctor.sweep`'s OWN convergence counter (`coord.state.
    board_doctor_row_state`) shows this row observed on at least two
    consecutive sweeps with this cause — i.e. the stranded shape has
    survived one full sweep interval — so an operator mid-way through
    queuing the pre-req by hand is never raced by this sweep doing it
    first. The very first sighting (``count == 1``) always returns
    ``False`` here, which `sweep` records as ``"auto-dispatch-noop"``, not
    a failure — the row is simply not ready yet.

    Re-confirms live, immediately before acting, the same posture every
    other #3887/#3901 mechanical dispatcher in this module takes: reads
    the dependent's CURRENT drive-queue row (not the frozen *row* snapshot
    `collect_rows` built this sweep) and re-derives the pre-req key and the
    guard verdict from it. A row that resolved, moved on to a different
    `after=` dep, or whose guard verdict flipped since classification (a
    label added, a hand `remove`) is left alone — the alert tier (via
    `CAUSE_STRANDED_PREREQ_BLOCKED`, or simply this cause persisting) picks
    it up on the very next sweep.

    Enqueues through `coord.upstream_gaps.queue_prereq` — #3944's own
    generalisation of `_queue_upstream`'s idempotency guards (already-
    queued/landed/stuck/closed), never a second, independently-maintained
    copy of them (#2096). *position* is the dependent's OWN current queue
    position for a same-repo pre-req (the dependent's place in line is the
    operator's stated priority for the whole chain), or the front of the
    pre-req's own repo queue for a cross-repo one — the same "front, not
    the tail" rule `_queue_upstream` already applies to a blocker.

    Returns ``True`` only once `queue_prereq` confirms the enqueue — the
    SAME "a dispatcher returns truthy only once its action is CONFIRMED"
    contract every other :data:`AUTO_DISPATCHERS` entry keeps. Every
    decline (not yet settled, row moved on, guard now says no, a
    config/queue read failure, `queue_prereq` itself reporting a no-op)
    returns ``False`` — `sweep` logs ``"auto-dispatch-noop"`` and, once
    this row's OWN convergence counter reaches `CONVERGENCE_LIMIT`, falls
    back to the alert tier on its own; this dispatcher never alerts
    directly.
    """

    def _body() -> bool:
        from coord import state  # noqa: PLC0415
        from coord.commands._common import ConfigError, resolve_and_load_config  # noqa: PLC0415
        from coord.drive_queue import (  # noqa: PLC0415
            STATE_WAITING,
            QueueEntry,
            entry_key,
            parse_key,
        )
        from coord.state import get_drive_queue_entry  # noqa: PLC0415
        from coord.upstream_gaps import queue_prereq  # noqa: PLC0415

        parsed = _parse_row_key(row.key)
        if parsed is None:
            return False
        repo_name, issue_number = parsed

        prior = state.board_doctor_row_state(row.queue, row.key)
        if int(prior.get("count", 0)) < 2:
            return False

        live_row = get_drive_queue_entry(repo_name, issue_number)
        if live_row is None:
            return _warn_noop(
                "drive_queue_add", repo_name, issue_number,
                "row no longer in the drive queue",
            )
        entry = QueueEntry.from_row(live_row)
        if entry.state != STATE_WAITING:
            return _warn_noop(
                "drive_queue_add", repo_name, issue_number,
                f"row moved on to {entry.state} — no longer stranded",
            )
        dep = stranded_prereq_dep(entry.last_reason)
        if dep is None:
            return _warn_noop(
                "drive_queue_add", repo_name, issue_number,
                "row's reason no longer names a stranded pre-req",
            )

        try:
            config = resolve_and_load_config(None)
        except ConfigError as exc:
            return _warn_noop(
                "drive_queue_add", repo_name, issue_number,
                f"could not load config to re-check {dep}: {exc}",
            )

        guard = stranded_prereq_guard(dep, config)
        if not guard.may_queue:
            return _warn_noop(
                "drive_queue_add", repo_name, issue_number,
                f"declined to auto-queue {dep}: {guard.reason}",
            )

        dep_parsed = parse_key(dep)
        if dep_parsed is None:  # pragma: no cover — the guard above already validated this
            return _warn_noop(
                "drive_queue_add", repo_name, issue_number,
                f"unparseable pre-req key {dep!r}",
            )
        dep_repo, dep_issue = dep_parsed
        dep_repo_cfg = next((r for r in (config.repos or []) if r.name == dep_repo), None)
        position = entry.position if dep_repo == repo_name else 0

        enqueued, note = queue_prereq(
            dep_repo, dep_issue, position=position,
            github=dep_repo_cfg.github if dep_repo_cfg is not None else None,
        )
        if not enqueued:
            return _warn_noop("drive_queue_add", repo_name, issue_number, f"{dep}: {note}")

        # #2096 ("unconfirmed success is a defect"): `queue_prereq` reports
        # ``enqueued=True`` off `enqueue_drive_queue`'s own return, not a
        # re-read — every OTHER write this module makes through the sweep
        # is confirmed by reading the row back
        # (`coord.board_doctor_permissions._confirm_write`'s own contract),
        # so this dispatcher holds itself to the same bar rather than
        # trusting the primitive's say-so.
        if get_drive_queue_entry(dep_repo, dep_issue) is None:
            return _warn_noop(
                "drive_queue_add", repo_name, issue_number,
                f"{dep}: queue_prereq reported enqueued, but a re-read found "
                "no row — not confirmed, retrying next sweep",
            )

        since_text = _stuck_since_text(row.queue, row.key)
        try:
            state.comment_on_issue(
                repo_name, issue_number,
                f"Queued {dep} because {entry_key(repo_name, issue_number)} "
                f"was waiting on it (open, not queued) {since_text}.",
                repo_github=next(
                    (r.github for r in (config.repos or []) if r.name == repo_name), None,
                ),
            )
        except Exception as exc:  # noqa: BLE001 — the enqueue itself already succeeded
            import click  # noqa: PLC0415

            click.echo(
                f"warning: board-doctor could not comment on {repo_name}#"
                f"{issue_number} about auto-queueing {dep}: {exc}",
                err=True,
            )
        return True

    return _run_mechanical_action("drive_queue_add", row, _body)


_register_auto_action(
    CAUSE_STRANDED_PREREQ,
    "drive_queue_add",
    _queue_stranded_prereq,
)


NO_INVESTIGATE_ENV_VAR = "COORD_BOARD_DOCTOR_NO_INVESTIGATE"
"""Process-wide kill switch read by :func:`_investigate_tier_enabled`.

Set (to any truthy string) by ``coord drive-queue sweep --no-investigate``
so the flag can reach this module without widening the
``Callable[[StuckRow], bool]`` dispatcher/handler signature (#3771).
"""


def _investigate_tier_enabled(repo_name: str, config: "Config") -> bool:
    """Decide whether the C1 investigate tier may fire for ``repo_name``.

    Three independent gates, any of which can disarm the tier (#3771):

    1. The :data:`NO_INVESTIGATE_ENV_VAR` process-wide kill switch, set by
       ``coord drive-queue sweep --no-investigate`` — disarms the tier
       fleet-wide for the lifetime of that sweep invocation regardless of
       config.
    2. ``board_doctor.investigate`` at the top of ``coordinator.yml`` — a
       fleet-wide config kill switch, re-read from disk on every call
       (:func:`_investigate` reloads config fresh each time), so flipping
       it takes effect on the very next sweep with no daemon restart.
    3. ``repos[].board_doctor.investigate`` — the per-repo opt-in. Even
       with the fleet-wide switch on, a repo that has not explicitly opted
       in stays on the mechanical/alert tiers only.

    The investigate tier is strictly more powerful than the mechanical
    (C2/C3 allowlist) tier — it dispatches a fix-it leg rather than just
    running a pre-approved action — so it defaults to off at both the
    fleet level (``board_doctor.investigate`` defaults ``True`` fleet-wide
    but each repo defaults ``False``) and must be explicitly opted into
    per repo.
    """
    if os.environ.get(NO_INVESTIGATE_ENV_VAR):
        return False
    if not config.board_doctor.investigate:
        return False
    repo = config.repo(repo_name)
    return bool(repo is not None and repo.board_doctor.investigate)


# ── #4126: a model-chosen investigator decision, in place of a fix-it leg ──
#
# ci-pending/gate-blocked (and most of the rest of `INVESTIGATE_CAUSES`) are
# coordinator-state problems — nothing a worker leg editing code can fix —
# yet :func:`_investigate` today dispatches exactly that for every one of
# them. This section gives `_investigate` a cheaper, narrower first move for
# those rows: one bounded `claude -p` call that picks a verb from the SAME
# closed #3881 investigator vocabulary (`coord.board_doctor_permissions.
# INVESTIGATOR_ACTIONS`) a human running `coord drive-queue doctor-act`
# would, through the identical precondition-checked wrapper
# (`dispatch_investigator_action`) — never a new, independently-reasoned
# write path. Opt-in only (:data:`MODEL_INVESTIGATOR_ENV_VAR`, default off):
# a model fed the same stale evidence a fixed classifier already loops on
# would loop the same way, so this stays off until the dispatch-guard and
# measurement work the rest of this epic adds has actually landed.

MODEL_INVESTIGATOR_ENV_VAR = "COORD_BOARD_DOCTOR_MODEL_INVESTIGATOR"
"""Process-wide opt-in read by :func:`_model_investigator_enabled`.

Unlike :data:`NO_INVESTIGATE_ENV_VAR`/:data:`NO_FINISHER_ENV_VAR` (both
default-on kill switches), this one defaults OFF: set it (to any truthy
string) to turn the model-chosen decision on. There is deliberately no
``coordinator.yml`` field for this yet — it is a single fleet-wide switch,
not a per-repo opt-in, until this epic's own dispatch-guard/measurement
issues land and a per-repo config knob is worth adding.
"""

#: The #3881 investigator verbs this model call is allowed to choose from —
#: a deliberate, narrower subset of :data:`coord.board_doctor_permissions.
#: INVESTIGATOR_ACTIONS` than a human `doctor-act` invocation gets. Every
#: name here resolves to a live board/config-only call
#: (:func:`_dispatch_model_eligible_action` below) — ``raise_budget``,
#: ``bounce``, ``integration_fix`` and ``restage`` all need an extra
#: identifier (a review assignment id, a sibling repo/issue, a restage
#: stage) this function makes no attempt to infer from evidence alone, so
#: they stay off this list until a future change threads that identifier in
#: too. ``"none"`` is not a member — it is the sentinel the model itself
#: returns for "do nothing," handled as a no-op before this set is ever
#: consulted.
MODEL_ELIGIBLE_ACTIONS: frozenset[str] = frozenset(
    {"requeue", "mark_done", "clear_latch", "merge"}
)

#: ``mark_done``/``merge`` mutate the board/merge queue in a way that is
#: awkward to undo (reconciling a row to done, or actually merging a PR) —
#: the #4126 ask requires a higher confidence bar for these than for
#: ``requeue``/``clear_latch``, which only reset a row back to its own
#: declared starting state.
_MODEL_HIGH_STAKES_ACTIONS: frozenset[str] = frozenset({"mark_done", "merge"})
_MODEL_HIGH_STAKES_CONFIDENCE_FLOOR = 0.8

#: Below this confidence, and only once the row has sat stuck longer than
#: :data:`_MODEL_ESCALATE_STUCK_HOURS`, the haiku-first call escalates to a
#: second, sonnet call rather than acting (or not acting) on a shaky first
#: read.
_MODEL_LOW_CONFIDENCE_ESCALATE = 0.5
_MODEL_ESCALATE_STUCK_HOURS = 6.0

#: Caps (#4126's own ask): at most this many model decisions per row in a
#: rolling 24h window, plus a fleet-default per-repo daily cap — both
#: counted off the audit trail this module itself writes
#: (:data:`MODEL_DECISION_EVENT_TYPE`), the same "count audited events, not
#: an in-memory counter that resets on daemon restart" shape
#: :func:`_fixit_leg_in_flight_count` already uses.
MODEL_DECISION_CAP_PER_ROW_24H = 3
MODEL_DECISION_CAP_PER_REPO_DAILY = 10
_MODEL_DECISION_WINDOW_HOURS = 24.0

#: Every #4126 decision that actually CONSULTED a model — acted, declined,
#: or an unparseable/failed call — is audited under this event type, with
#: ``actor="board_doctor_investigator_model"`` (distinct from both
#: ``"board_doctor_investigator"``, the human `doctor-act` CLI's own
#: default, and ``"sweep"``, the deterministic mechanical tier's). This is
#: NOT a second answer to "did the action land" — that question is asked
#: and answered exactly once, by :func:`coord.board_doctor_permissions.
#: _investigator_result` inside :func:`dispatch_investigator_action`; this
#: event only carries that already-computed ``acted`` value forward as
#: metadata (confidence, model used, escalation) the underlying primitive's
#: own audit row has no field for, so :func:`score_model_investigator_decisions`
#: and the per-row/per-repo cap checks have one place to read from.
#:
#: Deliberately excludes the capped branch (see
#: :data:`MODEL_DECISION_CAPPED_EVENT_TYPE`) — a decision this type's own
#: cap check (:func:`_model_investigator_cap_reason`) refused BEFORE any
#: model call happened must never itself count toward that same cap, or
#: toward :func:`score_model_investigator_decisions`'s ``"declined"``
#: bucket. Counting cap refusals here would make the cap a one-way
#: ratchet: once a row's per-24h quota of real decisions is spent, the
#: hourly sweep (`coord-board-doctor.timer`) would audit one more "capped"
#: row every pass, forever topping the window back up and never letting it
#: drain — and the same phantom rows would inflate the per-repo daily cap
#: toward disabling the whole repo's model path permanently.
MODEL_DECISION_EVENT_TYPE = "investigator_model_decision"

#: A #4126 decision refused by :func:`_model_investigator_cap_reason`
#: BEFORE any model call was made — audited for visibility, but under a
#: type the cap counters and :func:`score_model_investigator_decisions`
#: never query, so a capped round can never feed its own cap (see
#: :data:`MODEL_DECISION_EVENT_TYPE`'s docstring).
MODEL_DECISION_CAPPED_EVENT_TYPE = "investigator_model_decision_capped"


def _check_model_eligible_actions_are_in_the_closed_set() -> None:
    unknown = MODEL_ELIGIBLE_ACTIONS - INVESTIGATOR_ACTIONS
    if unknown:
        raise RuntimeError(
            f"coord.board_doctor_policy.MODEL_ELIGIBLE_ACTIONS names "
            f"verb(s) outside INVESTIGATOR_ACTIONS: {sorted(unknown)!r}"
        )


_check_model_eligible_actions_are_in_the_closed_set()


def _model_investigator_enabled() -> bool:
    """Is the #4126 model-chosen decision path armed for this process?"""
    return bool(os.environ.get(MODEL_INVESTIGATOR_ENV_VAR))


@dataclass(frozen=True)
class ModelInvestigatorDecision:
    """One #4126 model-chosen decision against a stuck row — the unit both
    :func:`decide_with_model_investigator` returns and the test suite
    asserts on."""

    repo_name: str
    issue_number: int
    action: str | None
    confidence: float
    reasoning: str
    model_used: str
    escalated: bool
    acted: bool
    detail: str


def _model_decision_window_count(
    repo_name: str, issue_number: int | None, *, hours: float,
) -> int:
    """How many :data:`MODEL_DECISION_EVENT_TYPE` events exist for
    *repo_name* (and, when given, *issue_number*) in the last *hours* — the
    cap counter for both the per-row and the per-repo-daily check. Counts
    only decisions that actually consulted a model: a row refused by the
    cap itself is audited under :data:`MODEL_DECISION_CAPPED_EVENT_TYPE`,
    a different event type this query never matches, so a capped round
    can never count toward its own (or the repo's) cap. Fails open to 0 on
    an unreadable audit log (same posture as
    :func:`_fixit_leg_in_flight_count`): a cap that can never be read is
    worse than a cap occasionally under-counted by a transient DB error.
    """
    import time as _time  # noqa: PLC0415

    from coord.audit import query_audit_log  # noqa: PLC0415

    try:
        result = query_audit_log(
            event_type=MODEL_DECISION_EVENT_TYPE,
            repo=repo_name,
            issue=issue_number,
            since=_time.time() - hours * 3600.0,
            limit=500,
        )
    except Exception:  # noqa: BLE001 — fails open, see docstring
        _log.warning(
            "board-doctor: could not read %s audit log for %s — model "
            "decision cap check fails open to 0",
            MODEL_DECISION_EVENT_TYPE,
            repo_name,
            exc_info=True,
        )
        return 0
    return len(result.get("entries") or [])


def _model_investigator_cap_reason(repo_name: str, issue_number: int) -> str | None:
    """``None`` when #4126's model path may still decide against this row;
    otherwise the human-readable reason it is capped this round."""
    row_count = _model_decision_window_count(
        repo_name, issue_number, hours=_MODEL_DECISION_WINDOW_HOURS,
    )
    if row_count >= MODEL_DECISION_CAP_PER_ROW_24H:
        return (
            f"{repo_name}#{issue_number} already had {row_count} model "
            f"investigator decision(s) in the last 24h (cap "
            f"{MODEL_DECISION_CAP_PER_ROW_24H})"
        )
    repo_count = _model_decision_window_count(
        repo_name, None, hours=_MODEL_DECISION_WINDOW_HOURS,
    )
    if repo_count >= MODEL_DECISION_CAP_PER_REPO_DAILY:
        return (
            f"{repo_name} already had {repo_count} model investigator "
            f"decision(s) fleet-repo-wide in the last 24h (cap "
            f"{MODEL_DECISION_CAP_PER_REPO_DAILY})"
        )
    return None


def _row_stuck_hours(repo_name: str, issue_number: int) -> float | None:
    """How long *repo_name*#*issue_number*'s drive-queue row has sat in its
    current shape, in hours — the escalation clock's input. ``None`` when
    there is no drive-queue row (a merge-queue-only row, or one this read
    can't resolve) rather than guessed as either "just now" or "ages ago".

    The caller (:func:`decide_with_model_investigator`) only escalates to
    the sonnet second opinion when *stuck_hours* is a known value above
    :data:`_MODEL_ESCALATE_STUCK_HOURS` — an unknown age never escalates,
    so a low-confidence haiku read on an unresolvable row is acted on (or
    declined) as-is, with no second call. This is a narrower case than
    :func:`coord.board_doctor._no_progress_age_seconds`'s own unknown-age
    handling, which escalates in the OPPOSITE direction (unknown reads as
    past the grace window, not exempt from it) — the two functions answer
    different questions and should not be read as mirroring each other.
    """
    import time as _time  # noqa: PLC0415

    from coord.board_doctor import _no_progress_age_seconds  # noqa: PLC0415
    from coord.drive_queue import QueueEntry  # noqa: PLC0415
    from coord.state import get_drive_queue_entry  # noqa: PLC0415

    try:
        row = get_drive_queue_entry(repo_name, issue_number)
    except Exception:  # noqa: BLE001 — best-effort; unknown beats a crash
        return None
    if row is None:
        return None
    entry = QueueEntry.from_row(row)
    age_seconds = _no_progress_age_seconds(entry, now=_time.time())
    return age_seconds / 3600.0 if age_seconds is not None else None


def _live_pr_snapshot(
    repo_name: str, issue_number: int, config: "Config",
) -> dict[str, Any] | None:
    """Best-effort live PR state for *repo_name*#*issue_number* (#4126) —
    the evidence bundle's own freshest signal, never the board's possibly-
    stale snapshot. ``None`` when this row has no merge-queue entry yet (no
    PR, or not queued there) or any read fails — folded into "no live PR
    evidence" rather than raised, the same posture as every other
    enrichment in this module (e.g. :func:`_merge_ref_instruction`).

    ``pr_state`` is read with :func:`coord.github_ops.get_pr_state_for_branch`
    (``"OPEN"``/``"MERGED"``/``"CLOSED"``, resolved directly off the PR's
    head branch) rather than :func:`coord.github_ops.get_issue_state` — that
    accessor shells `gh issue view` against the *issue* vocabulary
    (``"open"``/``"closed"`` only) and, by its own docstring, is scoped to
    issues; handed a PR number it can never report ``"merged"``, which is
    exactly the state the ``mark_done`` verb this evidence feeds is defined
    around ("the PR already merged ... out of band"). A failed/absent read
    is reported as the explicit string ``"unknown"`` here, never folded into
    ``None``/``"open"``, so the model is never told "nothing's wrong" about a
    read that simply didn't happen.

    ``checks`` is likewise reported as the explicit string ``"unavailable"``
    (not an empty list) when the CI store itself has no live read, so a
    blind read never renders as "nothing failing, nothing pending" to the
    model — distinguishing "I checked, all clear" from "I could not check".
    Reuses the SAME `coord.ci_store` read :func:`_live_ci_red_only`
    (`coord.board_doctor_permissions`) already makes for the identical
    question "what does this PR's CI say right now" — one live-CI
    abstraction answering it, never a second hand-rolled parse of
    ``gh pr checks``' raw JSON here.
    """
    try:
        from coord.merge_queue import load_queue  # noqa: PLC0415

        entry = next(
            (
                e
                for e in load_queue()
                if e.repo_name == repo_name and e.issue_number == issue_number
            ),
            None,
        )
        if entry is None or not entry.pr_number:
            return None

        from coord import github_ops  # noqa: PLC0415
        from coord.ci_store import build_ci_store, failed_checks, in_flight_checks  # noqa: PLC0415

        pr_state = github_ops.get_pr_state_for_branch(entry.repo_github, entry.branch)

        ci_store = build_ci_store(
            config.ci_store.type, host=config.ci_store.host, token_env=config.ci_store.token_env,
        )
        checks_available = ci_store.is_available
        checks = (
            ci_store.list_checks_for_pr(entry.repo_github, entry.pr_number)
            if checks_available
            else []
        )
        return {
            "pr_number": entry.pr_number,
            "pr_state": pr_state if pr_state is not None else "unknown",
            "failing_check_names": (
                [c.name for c in failed_checks(checks)] if checks_available else "unavailable"
            ),
            "pending_check_names": (
                [c.name for c in in_flight_checks(checks)] if checks_available else "unavailable"
            ),
        }
    except Exception:  # noqa: BLE001 — best-effort live enrichment only
        return None


def _render_model_evidence(
    row: StuckRow,
    repo_name: str,
    issue_number: int,
    *,
    stuck_hours: float | None,
    pr_snapshot: dict[str, Any] | None,
) -> str:
    """The evidence bundle text handed to the #4126 model call — every
    field a human running `coord drive-queue diagnose` would already see,
    plus the live PR snapshot a stored diagnosis can't carry.

    Diagnosis ``evidence`` lines and live check names are untrusted text
    (whatever a gate/CI check/commit happened to say) interpolated
    straight into this prompt, so a prompt-injected reply is reachable in
    principle. The blast radius stays bounded by construction, not by
    this function: the call is argv/stdin (no shell), the model may only
    name a verb in the closed :data:`MODEL_ELIGIBLE_ACTIONS` vocabulary
    (:func:`_parse_model_response` rejects anything else), and every verb
    still re-runs through `dispatch_investigator_action`'s own
    precondition checks before it mutates anything.
    """
    lines = [
        f"repo: {repo_name}",
        f"issue: {issue_number}",
        f"queue: {row.queue}",
        f"state: {row.state}",
        f"cause: {row.cause}",
        f"diagnosis_confidence: {row.confidence}",
        f"stuck_hours: {stuck_hours:.1f}" if stuck_hours is not None else "stuck_hours: unknown",
    ]
    if row.evidence:
        lines.append("evidence:")
        lines.extend(f"  - {line}" for line in row.evidence)
    if pr_snapshot is not None:
        lines.append(f"live_pr_number: {pr_snapshot['pr_number']}")
        lines.append(f"live_pr_state: {pr_snapshot['pr_state']}")
        lines.append(f"live_pr_failing_checks: {pr_snapshot['failing_check_names']}")
        lines.append(f"live_pr_pending_checks: {pr_snapshot['pending_check_names']}")
    else:
        lines.append(
            "live_pr_number: unknown (no merge-queue entry/PR found, or the live read failed)"
        )
    return "\n".join(lines)


_MODEL_INVESTIGATOR_SYSTEM_PROMPT = """\
You are the board-doctor investigator for one stuck row in a Claude Code \
fleet coordinator. You will be given evidence about a drive-queue or \
merge-queue row that has stopped making progress. Choose exactly ONE \
action from this closed list: requeue, mark_done, clear_latch, merge, none.

- requeue: the row's own blocker has already cleared live; take it down \
and put it back exactly as declared.
- mark_done: the PR already merged (or the issue already closed) out of \
band; reconcile the row.
- clear_latch: a stale HUMAN_REQUIRED merge-queue latch whose approval is \
already bound to the current branch content.
- merge: the row already reads green/approved/mergeable but the ordinary \
merge tick keeps missing it.
- none: the evidence does not clearly support any of the above — defer to \
a human rather than guess. Prefer none over a wrong guess.

Respond with ONLY a single JSON object, no prose, no code fences:
{"action": "<one of the five names above>", "confidence": <float 0.0-1.0>, \
"reasoning": "<one sentence>"}
"""


def _invoke_investigator_model(
    system_prompt: str, user_prompt: str, *, model: str, timeout: float = 60.0,
) -> str:
    """One bounded ``claude -p`` call with an explicit ``--model`` (#4126).

    Deliberately does not go through `coord.brain.call_claude` /
    `coord.providers.claude.ClaudeProvider.oneshot_command`: that method's
    own docstring says it never threads ``self._model`` through an oneshot
    call (#1706) — brain planning and the dashboard assistant never needed
    per-call model selection, this one genuinely does (haiku-first,
    sonnet-on-low-confidence), so it builds its own argv rather than
    quietly relying on a path that would silently ignore *model*.
    """
    import json as _json  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    from coord.agent import DEFAULT_WORKER_BINARY  # noqa: PLC0415

    cmd = [
        DEFAULT_WORKER_BINARY, "-p", "--system-prompt", system_prompt,
        "--model", model, "--output-format", "json",
    ]
    result = subprocess.run(
        cmd, input=user_prompt, capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"board-doctor model investigator call failed (exit "
            f"{result.returncode}): {result.stderr.strip()}"
        )
    try:
        outer = _json.loads(result.stdout)
        if isinstance(outer, dict) and "result" in outer:
            return outer["result"]
    except (_json.JSONDecodeError, ValueError):
        pass
    return result.stdout


def _parse_model_response(text: str) -> tuple[str | None, float, str]:
    """Parse the #4126 model's JSON reply into ``(action, confidence,
    reasoning)``.

    Never raises on malformed input — a response this cannot parse, whose
    ``action`` is not one of :data:`MODEL_ELIGIBLE_ACTIONS` plus the
    ``"none"`` sentinel, or whose ``confidence`` is not a finite number in
    ``[0, 1]``, reads as ``(None, 0.0, "<why>")`` — a refusal to act, never
    a guessed action (#2096: the gate that follows must be able to fail,
    which means a parse failure can never accidentally read as a
    confident, actionable decision).
    """
    import json as _json  # noqa: PLC0415
    import re as _re  # noqa: PLC0415

    cleaned = text.strip()
    fence = _re.match(r"^```(?:json)?\s*\n(.*?)```\s*$", cleaned, _re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        data = _json.loads(cleaned)
    except (_json.JSONDecodeError, ValueError) as exc:
        return None, 0.0, f"unparseable model response: {exc}"
    if not isinstance(data, dict):
        return None, 0.0, "model response was not a JSON object"

    action = data.get("action")
    if action == "none":
        action = None
    elif action not in MODEL_ELIGIBLE_ACTIONS:
        return None, 0.0, f"model returned unrecognized action {action!r}"

    confidence = data.get("confidence")
    if not isinstance(confidence, (int, float)) or not (0.0 <= float(confidence) <= 1.0):
        return None, 0.0, f"model returned invalid confidence {confidence!r}"

    reasoning = data.get("reasoning")
    reasoning = str(reasoning) if reasoning is not None else ""
    return action, float(confidence), reasoning


def _dispatch_model_eligible_action(
    action: str,
    repo_name: str,
    issue_number: int,
    board: "Board",
    config: "Config",
    *,
    reason: str,
) -> "InvestigatorActionResult":
    """Call `dispatch_investigator_action` for one of
    :data:`MODEL_ELIGIBLE_ACTIONS` with that verb's own required args —
    *action* is assumed already validated against that set.

    ``allow_fix_dispatch=False`` for ``merge`` (#4126, mirroring
    `_merge_pending_stuck_row`'s own #3902 note): this call is unattended,
    so it must never let `investigator_merge`'s own post-merge fan-out
    (dispatching a conflict-fix/ci-fix/sibling-conflict leg) spend a
    machine slot on this decision's behalf.
    """
    actor = "board_doctor_investigator_model"
    if action == "requeue":
        return dispatch_investigator_action(
            "requeue", repo_name, issue_number, board, reason=reason, actor=actor,
        )
    if action == "mark_done":
        return dispatch_investigator_action(
            "mark_done", repo_name, issue_number, board, config, reason=reason, actor=actor,
        )
    from coord import github_ops as gh_ops  # noqa: PLC0415

    if action == "clear_latch":
        return dispatch_investigator_action(
            "clear_latch", repo_name, issue_number, board, config, gh_ops,
            reason=reason, actor=actor,
        )
    if action == "merge":
        return dispatch_investigator_action(
            "merge", repo_name, issue_number, board, config, gh_ops,
            reason=reason, actor=actor, allow_fix_dispatch=False,
        )
    raise ValueError(f"unhandled model-eligible action {action!r}")  # pragma: no cover


def decide_with_model_investigator(
    row: StuckRow,
    repo_name: str,
    issue_number: int,
    board: "Board",
    config: "Config",
    *,
    invoke_model: Callable[..., str] | None = None,
) -> ModelInvestigatorDecision:
    """The #4126 entry point: one bounded model call deciding whether (and
    how) to act on *row*, in place of dispatching a fix-it leg.

    *invoke_model* defaults to :func:`_invoke_investigator_model`; tests
    inject a stub so this never shells out to a real ``claude -p``.

    Audits a :data:`MODEL_DECISION_EVENT_TYPE` row for every outcome that
    actually consulted a model — declined, failed-to-parse and acted
    decisions alike — so :func:`_model_investigator_cap_reason`'s own
    counters and :func:`score_model_investigator_decisions` have one
    consistent trail to read. The capped branch is audited separately
    under :data:`MODEL_DECISION_CAPPED_EVENT_TYPE` instead (see that
    constant's docstring for why counting it under the same type would
    make the cap a one-way ratchet). Never raises: every failure mode (a
    capped row, an unparseable model reply, a subprocess timeout) resolves
    to ``action=None, acted=False``, the same "gate fails, nothing
    mutates" direction `dispatch_investigator_action`'s own preconditions
    already fail toward.
    """
    call = invoke_model if invoke_model is not None else _invoke_investigator_model

    def _audit(
        decision: ModelInvestigatorDecision, *, event_type: str = MODEL_DECISION_EVENT_TYPE,
    ) -> ModelInvestigatorDecision:
        from coord.audit import record_audit  # noqa: PLC0415

        record_audit(
            tier="business",
            category="board_doctor_investigator",
            event_type=event_type,
            actor="board_doctor_investigator_model",
            summary=(
                f"model investigator decided {decision.action or 'none'} for "
                f"{repo_name}#{issue_number} (confidence={decision.confidence:.2f}, "
                f"model={decision.model_used}, acted={decision.acted})"
            ),
            repo=repo_name,
            issue=issue_number,
            details={
                "cause": row.cause,
                "action": decision.action,
                "confidence": decision.confidence,
                "reasoning": decision.reasoning,
                "model_used": decision.model_used,
                "escalated": decision.escalated,
                "acted": decision.acted,
                "detail": decision.detail,
            },
        )
        return decision

    cap_reason = _model_investigator_cap_reason(repo_name, issue_number)
    if cap_reason is not None:
        return _audit(
            ModelInvestigatorDecision(
                repo_name=repo_name,
                issue_number=issue_number,
                action=None,
                confidence=0.0,
                reasoning=cap_reason,
                model_used="",
                escalated=False,
                acted=False,
                detail=cap_reason,
            ),
            event_type=MODEL_DECISION_CAPPED_EVENT_TYPE,
        )

    stuck_hours = _row_stuck_hours(repo_name, issue_number)
    pr_snapshot = _live_pr_snapshot(repo_name, issue_number, config)
    evidence = _render_model_evidence(
        row, repo_name, issue_number, stuck_hours=stuck_hours, pr_snapshot=pr_snapshot,
    )

    # resolve() only returns None for a None alias (preserves the "omit
    # --model" code path) — passing a literal alias always gets a str back
    # (the pinned override, or the alias unchanged), so there is no `or`
    # fallback to write here.
    haiku_model = config.models.resolve("haiku")
    sonnet_model = config.models.resolve("sonnet")
    assert haiku_model is not None  # resolve("haiku") never returns None
    assert sonnet_model is not None  # resolve("sonnet") never returns None

    def _call(model: str) -> tuple[str | None, float, str]:
        try:
            raw = call(_MODEL_INVESTIGATOR_SYSTEM_PROMPT, evidence, model=model, timeout=60.0)
        except Exception as exc:  # noqa: BLE001 — a model-call failure is a refusal, not a crash
            return None, 0.0, f"model call failed: {exc}"
        return _parse_model_response(raw)

    action, confidence, reasoning = _call(haiku_model)
    model_used = haiku_model
    escalated = False
    if (
        confidence < _MODEL_LOW_CONFIDENCE_ESCALATE
        and stuck_hours is not None
        and stuck_hours > _MODEL_ESCALATE_STUCK_HOURS
    ):
        action, confidence, reasoning = _call(sonnet_model)
        model_used = sonnet_model
        escalated = True

    if action is None:
        return _audit(
            ModelInvestigatorDecision(
                repo_name=repo_name,
                issue_number=issue_number,
                action=None,
                confidence=confidence,
                reasoning=reasoning,
                model_used=model_used,
                escalated=escalated,
                acted=False,
                detail=reasoning,
            )
        )

    if action in _MODEL_HIGH_STAKES_ACTIONS and confidence < _MODEL_HIGH_STAKES_CONFIDENCE_FLOOR:
        detail = (
            f"model chose {action!r} at confidence {confidence:.2f}, below the "
            f"{_MODEL_HIGH_STAKES_CONFIDENCE_FLOOR} floor required for a "
            "high-stakes action — not acted on"
        )
        return _audit(
            ModelInvestigatorDecision(
                repo_name=repo_name,
                issue_number=issue_number,
                action=action,
                confidence=confidence,
                reasoning=reasoning,
                model_used=model_used,
                escalated=escalated,
                acted=False,
                detail=detail,
            )
        )

    reason = (
        f"board-doctor model investigator ({model_used}, confidence="
        f"{confidence:.2f}): {reasoning}"
    )
    try:
        result = _dispatch_model_eligible_action(
            action, repo_name, issue_number, board, config, reason=reason,
        )
        acted, detail = result["acted"], result["detail"]
    except Exception as exc:  # noqa: BLE001 — a dispatch failure is a refusal, not a crash
        acted, detail = False, f"dispatch failed: {exc}"

    return _audit(
        ModelInvestigatorDecision(
            repo_name=repo_name,
            issue_number=issue_number,
            action=action,
            confidence=confidence,
            reasoning=reasoning,
            model_used=model_used,
            escalated=escalated,
            acted=acted,
            detail=detail,
        )
    )


@dataclass(frozen=True)
class ModelDecisionOutcome:
    """One #4126 model decision's scored outcome — the unit
    :func:`score_model_investigator_decisions` returns and
    `coord.commands.stats` renders."""

    repo_name: str
    issue_number: int
    ts: float
    action: str | None
    confidence: float
    model_used: str
    escalated: bool
    acted: bool
    #: ``"too_recent"`` | ``"declined"`` | ``"unstuck"`` | ``"still_stuck"``
    outcome: str


def score_model_investigator_decisions(
    audit_entries: Sequence[Mapping[str, Any]],
    assignment_rows: Sequence[Mapping[str, Any]],
    *,
    now: float | None = None,
    score_after_hours: float = _MODEL_DECISION_WINDOW_HOURS,
) -> list[ModelDecisionOutcome]:
    """Score every #4126 :data:`MODEL_DECISION_EVENT_TYPE` row in
    *audit_entries* against *assignment_rows* — the SAME two already-
    fetched reads `coord stats waste` hands every other aggregator (see
    that command's own module docstring), never a fresh live board/GitHub
    read of this function's own. Pure dict-in/dict-out, like every other
    aggregator in that command's pipeline.

    ``outcome``:

    * ``"too_recent"`` — fewer than *score_after_hours* have elapsed since
      the decision; not enough time has passed to judge it yet.
    * ``"declined"`` — the model chose not to act (``acted=False``: a
      capped row, a parse failure, ``"none"``, or a confidence below the
      high-stakes floor) — nothing to score.
    * ``"unstuck"`` — an *acted* decision with a LATER assignment row for
      the same issue reading a terminal status (``merged``/``done``).
    * ``"still_stuck"`` — an *acted* decision with no such later row. This
      reads identically whether the action never moved the row at all, or
      moved it and it re-stuck afterward — telling those two apart needs a
      denser history of this row's state over time than the board/audit
      reads this function is handed carry; a future change can sharpen
      this once that's available, but "did this decision's action stick"
      is the question #4126 asks, and a false "unstuck" is the wrong
      direction to be permissive in.
    """
    import time as _time  # noqa: PLC0415

    from coord.usage_rollup import parse_timestamp, row_issue_number  # noqa: PLC0415

    resolved_now = now if now is not None else _time.time()
    terminal_statuses = {"merged", "done"}

    outcomes: list[ModelDecisionOutcome] = []
    for entry in audit_entries:
        if entry.get("event_type") != MODEL_DECISION_EVENT_TYPE:
            continue
        details = entry.get("details") or {}
        repo_name = str(entry.get("repo") or "")
        issue_number = int(entry.get("issue") or 0)
        ts = parse_timestamp(entry.get("ts")) or 0.0
        acted = bool(details.get("acted"))
        action = details.get("action")
        confidence = float(details.get("confidence") or 0.0)
        model_used = str(details.get("model_used") or "")
        escalated = bool(details.get("escalated"))

        if resolved_now - ts < score_after_hours * 3600.0:
            outcome = "too_recent"
        elif not acted:
            outcome = "declined"
        else:
            later_terminal = any(
                str(row.get("repo_name") or "") == repo_name
                and row_issue_number(row) == issue_number
                and str(row.get("status") or "") in terminal_statuses
                and (parse_timestamp(row.get("finished_at")) or 0.0) >= ts
                for row in assignment_rows
            )
            outcome = "unstuck" if later_terminal else "still_stuck"

        outcomes.append(
            ModelDecisionOutcome(
                repo_name=repo_name,
                issue_number=issue_number,
                ts=ts,
                action=action,
                confidence=confidence,
                model_used=model_used,
                escalated=escalated,
                acted=acted,
                outcome=outcome,
            )
        )
    return outcomes


NO_FINISHER_ENV_VAR = "COORD_BOARD_DOCTOR_NO_FINISHER"
"""Process-wide kill switch read by :func:`_finisher_tier_enabled`.

Mirrors :data:`NO_INVESTIGATE_ENV_VAR` for Trigger A (#3835) — set (to any
truthy string) to disarm nit-only finisher routing fleet-wide without
touching ``coordinator.yml``.
"""

#: Trigger A's (#3835) own cause string, passed through to
#: `coord.board_doctor_permissions.sweep_dispatch_finisher_leg` as
#: ``cause=``. Not a `coord.board_doctor` `StuckRow.cause` — this fires
#: synchronously from `coord.auto_loop.process_review_completion` at the
#: moment a review completes, never from the periodic sweep, so it is never
#: registered into `ALLOWLIST`/`AUTO_DISPATCHERS`/`INVESTIGATE_CAUSES`.
CAUSE_NIT_ONLY_REQUEST_CHANGES = "nit_only_request_changes"


def _finisher_tier_enabled(repo_name: str, config: "Config") -> bool:
    """Decide whether Trigger A (#3835) may route a nit-only review here.

    Mirrors :func:`_investigate_tier_enabled`'s three independent gates
    exactly, reading the ``finish``-named fields instead of
    ``investigate``:

    1. :data:`NO_FINISHER_ENV_VAR` — a process-wide kill switch.
    2. ``board_doctor.finish`` — the fleet-wide config switch.
    3. ``repos[].board_doctor.finish`` — the per-repo opt-in. A repo that
       has not opted in keeps today's #476 behavior (silent
       approve-with-nits, nothing applied) regardless of the fleet-wide
       switch.
    """
    if os.environ.get(NO_FINISHER_ENV_VAR):
        return False
    if not config.board_doctor.finish:
        return False
    repo = config.repo(repo_name)
    return bool(repo is not None and repo.board_doctor.finish)


NO_STALE_RACE_ENV_VAR = "COORD_BOARD_DOCTOR_NO_STALE_RACE"
"""Process-wide kill switch read by :func:`_stale_race_tier_enabled`.

Mirrors :data:`NO_INVESTIGATE_ENV_VAR`/:data:`NO_FINISHER_ENV_VAR` for #3859
— set (to any truthy string) to disarm the checks-stale-race disposition
fleet-wide without touching ``coordinator.yml``.
"""

#: #3859: roughly 2x quadraui's own measured PR CI wall-clock (~25-40
#: minutes, per the issue) — the bound on how long a merge-gate reservation
#: may hold a repo's siblings before releasing on its own. A single
#: fleet-wide constant rather than a new per-repo config knob: there is no
#: per-repo CI-wall-clock measurement anywhere in this fleet yet to size a
#: knob against, and a reservation that outlives its holder's actual CI run
#: costs the rest of the repo's rows at most one extra wait, never a stuck
#: row (:func:`coord.state.get_merge_reservation` auto-expires it).
STALE_RACE_RESERVATION_TTL_SECONDS = 2 * 60 * 60


def _stale_race_tier_enabled(repo_name: str, config: "Config") -> bool:
    """Decide whether #3859's checks-stale-race disposition may act for
    *repo_name* — reserve the merge gate and dispatch a stale-rebase,
    instead of today's straight-to-``merge_checks_stale_human_required``
    escalation.

    Three independent gates, structurally like
    :func:`_investigate_tier_enabled`/:func:`_finisher_tier_enabled` but with
    the per-repo polarity flipped — this disposition only orders retries a
    repo is already exposed to, so it participates by default rather than
    needing an explicit per-repo opt-in:

    1. :data:`NO_STALE_RACE_ENV_VAR` — a process-wide kill switch.
    2. ``board_doctor.stale_race`` — the fleet-wide config switch.
    3. ``repos[].board_doctor.stale_race`` — the per-repo switch, defaulting
       ``True`` (:class:`~coord.models.BoardDoctorRepoConfig`). A repo that
       has explicitly opted OUT keeps today's straight-to-escalation
       behavior regardless of the fleet-wide switch.

    Called directly by `coord.commands.drive_queue._run_auto_revalidate_
    checks_stale` (not only from the periodic board-doctor sweep) — the
    checks-stale-race shape this disposition targets is produced by that
    same tick's own escalation path, so the ordering/reservation logic lives
    there rather than behind a second, independently-derived gate check.
    """
    if os.environ.get(NO_STALE_RACE_ENV_VAR):
        return False
    if not config.board_doctor.stale_race:
        return False
    repo = config.repo(repo_name)
    return bool(repo is not None and repo.board_doctor.stale_race)


NO_CONFIG_DRIFT_AUTOFIX_ENV_VAR = "COORD_BOARD_DOCTOR_NO_CONFIG_DRIFT_AUTOFIX"
"""Process-wide kill switch read by :func:`_config_drift_autofix_enabled`
(#4035) — mirrors :data:`NO_STALE_RACE_ENV_VAR` and friends."""


def _config_drift_autofix_enabled(repo_name: str, config: "Config") -> bool:
    """Whether *repo_name* has opted in to the #4035 config-drift autofix
    disposition — same three-gate shape as
    :func:`_stale_race_tier_enabled`, but the per-repo polarity of
    ``auto_refile``/``auto_split``.

    1. :data:`NO_CONFIG_DRIFT_AUTOFIX_ENV_VAR` — a process-wide kill switch.
    2. ``board_doctor.config_drift_autofix`` — the fleet-wide config switch.
    3. ``repos[].board_doctor.config_drift_autofix`` — the per-repo opt-in,
       default ``False`` (:class:`~coord.models.BoardDoctorRepoConfig`).

    Feeds :func:`plan_config_drift_fix`'s ``ConfigDriftFix.enabled``/
    ``patched_command`` fields and the wording of its ``alert_message``.
    ``True`` here is what :func:`coord.board_doctor.
    run_config_drift_fix_sweep` reads to decide whether to actually write
    ``coordinator.yml`` and commit in ``coord-settings`` (#4048) —
    ``False`` instead routes the row to the ordinary alert tier with the
    one-line patch in ``alert_message`` for a human to apply by hand.
    """
    if os.environ.get(NO_CONFIG_DRIFT_AUTOFIX_ENV_VAR):
        return False
    if not config.board_doctor.config_drift_autofix:
        return False
    repo = config.repo(repo_name)
    return bool(repo is not None and repo.board_doctor.config_drift_autofix)


@dataclass(frozen=True)
class ConfigDriftFix:
    """One #4035 fix-path decision for a single dangling cargo target.

    ``patched_command`` is the exact *token*-removed command text, set
    ONLY when the autofix disposition is enabled for the repo AND the
    token was found exactly once in the command (see
    :func:`coord.smoke.remove_dangling_target_token` — ambiguous or
    absent matches refuse rather than guess). :func:`coord.board_doctor.
    run_config_drift_fix_sweep` reads ``patched_command`` and writes it
    back to the live, symlinked ``coordinator.yml``, committing in the
    ``coord-settings`` repo, whenever it is set (#4048). ``alert_message``
    is always populated too, independent of ``enabled`` — the opted-out
    path's only outcome — so the row is never left silently unexplained.
    """

    enabled: bool
    patched_command: str | None
    alert_message: str


def plan_config_drift_fix(
    repo_name: str, *, command: str, source: str, token: str, config: "Config",
) -> ConfigDriftFix:
    """Decide the #4035 fix-path outcome for one dangling *token* found in
    *command* (*source* names where that command comes from, e.g.
    ``"repos[quadraui].ci_command"`` — :class:`coord.smoke.SmokeCommand.
    source`).

    Never mutates anything, and never claims to have: this only COMPUTES
    the patch — writing it back to the live, symlinked
    ``coordinator.yml`` and committing in the ``coord-settings`` repo is
    :func:`coord.board_doctor.run_config_drift_fix_sweep`'s job, gated on
    ``enabled``. ``alert_message`` always describes the patch
    PROSPECTIVELY — "apply this" rather than "applied this" — regardless
    of ``enabled``, since it is also what the opted-out path shows a
    human instead of applying it automatically. Edits exactly the matched
    *token* (:func:`coord.smoke.remove_dangling_target_token`), never
    anything else in *command* — a *token* that is absent, or appears
    more than once, produces no ``patched_command`` at all, only the
    alert naming the exact one-line
    patch for a human to apply.
    """
    from coord.smoke import remove_dangling_target_token  # noqa: PLC0415

    enabled = _config_drift_autofix_enabled(repo_name, config)
    patched = remove_dangling_target_token(command, token) if enabled else None

    if patched is not None:
        alert_message = (
            f"{repo_name}: {source} still runs the dangling `{token}` "
            f"(#4035) — apply this one-line patch to clear it: `{patched}`"
        )
    else:
        alert_message = (
            f"{repo_name}: {source} still runs `{token}`, naming a target "
            f"this branch's own diff deletes (#4035). Remove `{token}` "
            f"from {source} in coordinator.yml to clear this."
        )
    return ConfigDriftFix(
        enabled=enabled, patched_command=patched, alert_message=alert_message,
    )


def _at_repo_capacity(
    repo_name: str,
    config: "Config",
    board: "Board",
    *,
    claimed: int = 0,
) -> bool:
    """Shared "is *repo_name* at or over its per-repo dispatch cap" predicate
    (#3837), used by both the investigate tier's per-sweep
    :class:`InvestigateCapacityGate` and the finisher tier's single-call
    :func:`dispatch_nit_only_finisher`. Both tiers share one capacity knob —
    :func:`_investigate_repo_capacity` / :func:`_investigate_in_flight_count`,
    neither of which has an investigate-specific meaning despite the name
    (see their own docstrings) — so this is the one place that resolves it
    and compares against in-flight occupancy.

    *claimed* is the caller's own additional in-flight count not yet
    reflected in *board* — :class:`InvestigateCapacityGate` passes its own
    per-sweep ``self._claimed.get(repo_name, 0)`` so a row CONFIRMED
    dispatched earlier in the same sweep still counts even though *board*
    was built once at sweep start and is never refreshed mid-sweep.
    :func:`dispatch_nit_only_finisher` has no such per-sweep state — it is
    the one and only call site for a review completing — and omits it,
    defaulting to ``0``.

    Returns ``False`` (never at capacity) if the resolved cap is
    non-positive — ``0`` means "no ceiling" per
    :func:`_investigate_repo_capacity`'s own documented semantics, not
    "zero capacity".
    """
    cap = _investigate_repo_capacity(repo_name, config)
    if cap <= 0:
        return False
    in_flight = _investigate_in_flight_count(repo_name, board) + claimed
    return in_flight >= cap


def dispatch_nit_only_finisher(
    repo_name: str,
    issue_number: int,
    board: "Board",
    config: "Config",
    *,
    evidence: tuple[str, ...] = (),
    nits: tuple[str, ...] = (),
    http_client: object | None = None,
) -> bool:
    """Trigger A (#3835): route a nit-only `request-changes` to a finisher
    leg instead of #476's silent approve-with-nits.

    Called directly from `coord.auto_loop.process_review_completion` at
    the moment a review's blocking section is confirmed empty
    (`coord.review.classify_blocking_evidence().confirmed_absent` — exactly
    `coord.review.classify_finding_nit_class`'s own fallback answer when no
    fix has been drafted yet, since no fix exists at review-completion
    time). This is a plain, synchronously-called function, NOT a
    `coord.board_doctor.sweep` auto-dispatcher: it is never registered into
    `ALLOWLIST` / `AUTO_DISPATCHERS` / `INVESTIGATE_CAUSES` /
    `policy_tier_of`, since that machinery exists for the periodic
    `StuckRow` sweep, a different call site with different available data.

    Returns ``True`` only when `coord.board_doctor_permissions.
    sweep_dispatch_finisher_leg` (via :data:`_FINISHER_ACTION`) actually
    confirms ``dispatched=True`` — mirroring :func:`_investigate`'s
    confirmed-dispatch discipline (never signalling "handled" off the mere
    absence of an exception, per #2096). The caller must fall through to
    its own existing behavior on ``False``, for any reason: the tier is
    off, the repo hasn't opted in, the repo is already at its per-repo
    capacity cap (:func:`_at_repo_capacity`, #3837), a
    fix-it/finisher/drive-session is already in flight for this issue, or
    `_dispatch_fix` declined (no capable machine, unreachable agent, …).
    There is no alert-fallback here, unlike :func:`_investigate` —
    declining just means #476's own behavior already applies, which is
    itself a safe, legible outcome (approve-with-nits, nothing silently
    lost), not a stuck row needing a human page.

    The capacity decline is terminal for this review, unlike the
    investigate tier's per-sweep deferral (:class:`InvestigateCapacityGate`
    re-peeks a deferred row on every later sweep pass, so it eventually
    gets through once something else frees a slot): this function has no
    sweep loop retrying it, so "at capacity" here just means #476's
    approve-with-nits applies for THIS review, permanently — a later
    review for the same issue gets its own fresh capacity check.

    *nits* carries the reviewer's actual nit/non-blocking findings text
    (see `coord.review.extract_nits_section`) so the finisher leg's
    briefing can tell the worker what to apply, rather than just a count.
    """
    if not _finisher_tier_enabled(repo_name, config):
        return False
    if _at_repo_capacity(repo_name, config, board):
        import click  # noqa: PLC0415

        click.echo(
            f"warning: board-doctor NOT dispatching a finisher leg for "
            f"{repo_name}#{issue_number} — the repo is already at its "
            "per-repo capacity cap (_at_repo_capacity, #3837); falling back "
            "to #476's approve-with-nits for this review instead",
            err=True,
        )
        return False
    result = dispatch_sweep_action(
        _FINISHER_ACTION,
        repo_name,
        issue_number,
        board,
        config,
        cause=CAUSE_NIT_ONLY_REQUEST_CHANGES,
        evidence=evidence,
        nits=nits,
        http_client=http_client,
    )
    return bool(result.get("dispatched"))


NO_MECHANIC_ENV_VAR = "COORD_BOARD_DOCTOR_NO_MECHANIC"
"""Process-wide kill switch read by :func:`_mechanic_tier_enabled` (#4094)
— mirrors :data:`NO_FINISHER_ENV_VAR` and friends: set (to any truthy
string) to disarm the mechanic leg fleet-wide without touching
``coordinator.yml``."""

#: #4094's own cause string, passed through to `coord.board_doctor_
#: permissions.sweep_dispatch_mechanic_leg` as ``cause=``. Not a
#: `coord.board_doctor` `StuckRow.cause` — like `CAUSE_NIT_ONLY_REQUEST_
#: CHANGES` this fires synchronously from the Test-stage triage-first
#: route (`coord.drive.MergeVerifier.triage_test_failure` /
#: `coord.commands.drive`'s ``coord mechanic-dispatch``), never from the
#: periodic sweep.
CAUSE_TEST_TRIAGE_MECHANICAL = "test_triage_mechanical"

#: #4097's own cause string for the CI-red triage-first route
#: (`coord.ci_fix`) — same role as `CAUSE_TEST_TRIAGE_MECHANICAL`, passed
#: explicitly to `dispatch_mechanic_leg_for_triage` so a mechanic leg
#: dispatched off a CI-red verdict is never audited/briefed as a Test-stage
#: one.
CAUSE_CI_TRIAGE_MECHANICAL = "ci_triage_mechanical"

#: #4097's own cause string for the merge-failure triage-first route
#: (`coord.ci_fix.dispatch_mechanic_leg_for_conflict`, called from
#: `coord.commands.merge._dispatch_conflict_fixes`) — same role as
#: `CAUSE_TEST_TRIAGE_MECHANICAL`/`CAUSE_CI_TRIAGE_MECHANICAL`.
CAUSE_CONFLICT_TRIAGE_MECHANICAL = "conflict_triage_mechanical"


NO_TRIAGE_ENV_VAR = "COORD_BOARD_DOCTOR_NO_TRIAGE"
"""Process-wide kill switch read by :func:`_triage_tier_enabled` (#4100)
— mirrors :data:`NO_MECHANIC_ENV_VAR` and friends: set (to any truthy
string) to disarm #4093's triage-first routing fleet-wide, across every
edge it reaches, without touching ``coordinator.yml``."""


def _triage_tier_enabled(repo_name: str, config: "Config | None") -> bool:
    """Decide whether a failure on *repo_name* may be routed through
    #4093's triage-first classifier AT ALL (#4100) — one level upstream of
    :func:`_mechanic_tier_enabled`, which only governs whether an ALREADY
    PRODUCED ``BUCKET_MECHANICAL`` verdict may dispatch a mechanic leg.
    This is the single question "should this failure even be classified",
    answered once here and read by every triage-first edge before it ever
    calls `coord.failure_triage.triage_failure` — never a second,
    independently-gated copy of the same decision (#2096):
    `coord.drive.MergeVerifier.triage_test_failure` (the Test-stage
    retry/route table), `coord.ci_fix.triage_ci_failure` (the CI-red
    merge-gate route), and `coord.ci_fix.triage_conflict_failure` (the
    merge-conflict route, via `coord.commands.merge._dispatch_conflict_
    fixes`, whose own *config* is legitimately `None` for the one call
    site — a real local ``git merge`` attempt — that verifies conflicts a
    stronger way already).

    Three gates, mirroring :func:`_finisher_tier_enabled`'s shape:

    0. *config* of `None` — no `coordinator.yml` in scope at all — fails
       closed to `False`, the same direction every switch below already
       defaults to.
    1. :data:`NO_TRIAGE_ENV_VAR` — the process-wide kill switch.
    2. ``board_doctor.triage`` (`coord.config.BoardDoctorConfig.triage`,
       default `True`) — the fleet-wide operator switch. Read fresh on
       every call, never cached, so flipping it in ``coordinator.yml``
       takes effect on the very next tick/poll with no daemon or
       ``coord drive`` restart.
    3. ``repos[].board_doctor.triage`` (`coord.models.
       BoardDoctorRepoConfig.triage`, default `False`) — the per-repo
       opt-in. A repo that has not opted in (or either switch above is
       off) gets EXACTLY the pre-#4093 behaviour on every edge: the
       Test-stage edge's #3714 one-shot retry fires unconditionally
       (never consulting a bucket), the CI-red edge dispatches its worker
       unconditionally, and the merge-failure edge's conflict-fix worker
       dispatches unconditionally.
    """
    if config is None:
        return False
    if os.environ.get(NO_TRIAGE_ENV_VAR):
        return False
    if not config.board_doctor.triage:
        return False
    repo = config.repo(repo_name)
    return bool(repo is not None and repo.board_doctor.triage)


def _mechanic_tier_enabled(repo_name: str, config: "Config") -> bool:
    """Decide whether an ALREADY PRODUCED ``BUCKET_MECHANICAL`` #4093
    triage verdict may route to the #4094 mechanic leg (#4094).

    This gate is reached only once :func:`_triage_tier_enabled` has
    already allowed the classifier to run and it returned a mechanical
    verdict — it does not re-decide "should this repo be triaged at all",
    only "may a mechanical verdict dispatch a mechanic leg". There is
    deliberately no paired fleet-wide `Config.board_doctor.mechanic`
    switch: :data:`NO_TRIAGE_ENV_VAR` / ``board_doctor.triage`` above
    already cover the fleet-wide emergency-brake role for the whole
    triage-first system, upstream of this one.

    1. :data:`NO_MECHANIC_ENV_VAR` — the process-wide kill switch.
    2. ``board_doctor.mechanic`` — the per-repo opt-in
       (`coord.models.BoardDoctorRepoConfig.mechanic`, default `False`). A
       repo that has not opted in gets today's behaviour unchanged: a
       Test-stage failure (mechanical or not) goes through the ordinary
       fix-round path regardless of what the triage classifier says.
    """
    if os.environ.get(NO_MECHANIC_ENV_VAR):
        return False
    repo = config.repo(repo_name)
    return bool(repo is not None and repo.board_doctor.mechanic)


#: #4100: fallback mechanic-leg concurrency budget used when *repo_name*
#: has no `coordinator.yml` entry at all (:func:`_at_mechanic_leg_capacity`
#: reads `BoardDoctorRepoConfig.mechanic_leg_cap`, default ``1``, for every
#: configured repo — this constant only covers the "no such repo" edge,
#: mirroring :data:`FIXIT_LEG_BUDGET_DEFAULT`'s own posture).
MECHANIC_LEG_BUDGET_DEFAULT = 1


def _mechanic_leg_in_flight_count(repo_name: str, board: "Board") -> int:
    """How many mechanic legs this doctor dispatched for *repo_name* are
    still ``running``/``pending`` — never ordinary drive-queue, review or
    smoke work, which must not consume this dedicated budget. Mirrors
    :func:`_fixit_leg_in_flight_count` exactly, reading `coord.board_
    doctor_permissions.MECHANIC_LEG_DISPATCHED_EVENT` instead of
    ``fixit_leg_dispatched`` — the one event every confirmed mechanic-leg
    dispatch records (`coord.board_doctor_permissions.
    sweep_dispatch_mechanic_leg`), and the only writer of that event type.
    """
    from coord.audit import query_audit_log  # noqa: PLC0415
    from coord.board_doctor_permissions import (  # noqa: PLC0415
        MECHANIC_LEG_DISPATCHED_EVENT,
    )

    try:
        entries = (
            query_audit_log(
                event_type=MECHANIC_LEG_DISPATCHED_EVENT, repo=repo_name, limit=200,
            ).get("entries")
            or []
        )
    except Exception:  # noqa: BLE001 — an unreadable audit log fails open
        # ("nothing confirmed in flight") rather than permanently refusing
        # every future mechanic-leg dispatch for this repo. Failing open is
        # the expensive direction for a budget this tight (default 1), so
        # at least make it visible rather than silently granting a fresh
        # dispatch every call.
        _log.warning(
            "board-doctor: could not read %s audit log for %s — mechanic-leg "
            "concurrency check fails open to 0 in-flight",
            MECHANIC_LEG_DISPATCHED_EVENT,
            repo_name,
            exc_info=True,
        )
        return 0
    dispatched_ids = {e.get("assignment_id") for e in entries if e.get("assignment_id")}
    if not dispatched_ids:
        return 0
    return sum(
        1
        for assignment in board.active
        if assignment.repo_name == repo_name
        and assignment.assignment_id in dispatched_ids
        and assignment.status in ("running", "pending")
    )


def _at_mechanic_leg_capacity(
    repo_name: str, config: "Config", board: "Board", *, claimed: int = 0,
) -> bool:
    """Is *repo_name* at or over its OWN per-repo mechanic-leg concurrency
    cap (#4100)?

    Distinct from :func:`_at_repo_capacity` (shared `max_parallel`-keyed
    cap, also drawn on by the investigate/finisher tiers) and from
    :func:`_mechanic_leg_already_attempted_at_sha` in `coord.board_doctor_
    permissions` (the #4094 one-per-SHA budget, which blocks a SECOND
    attempt at the SAME failure rather than bounding how many DIFFERENT
    mechanic legs run at once). *claimed* mirrors :func:`_at_repo_capacity`'s
    own parameter — the caller's own additional in-flight count not yet
    reflected in *board*.
    """
    repo = config.repo(repo_name)
    cap = (
        repo.board_doctor.mechanic_leg_cap
        if repo is not None
        else MECHANIC_LEG_BUDGET_DEFAULT
    )
    if cap <= 0:
        return False
    in_flight = _mechanic_leg_in_flight_count(repo_name, board) + claimed
    return in_flight >= cap


def dispatch_mechanic_leg_for_triage(
    repo_name: str,
    issue_number: int,
    board: "Board",
    config: "Config",
    *,
    verdict: Any,
    branch_sha: str,
    cause: str = CAUSE_TEST_TRIAGE_MECHANICAL,
    failed_stage_commands: tuple[str, ...] = (),
    evidence: tuple[str, ...] = (),
    http_client: object | None = None,
) -> bool:
    """#4094: route a triage-first failure `coord.failure_triage.
    triage_failure` classified ``BUCKET_MECHANICAL`` to a mechanic leg
    instead of spending a full fix round + review + test fan-out on it.

    Called from each triage-first edge's own routing: `coord drive`'s
    Test-stage route (via the ``coord mechanic-dispatch`` CLI), and
    `coord.ci_fix`'s CI-red and merge-failure routes directly. This is a
    plain, synchronously-called function, NOT a `coord.board_doctor.sweep`
    auto-dispatcher — mirrors :func:`dispatch_nit_only_finisher`'s own
    shape and rationale exactly.

    *verdict* is a `coord.failure_triage.TriageVerdict` (typed as `Any`
    here to avoid importing that module at this function's call surface
    before a real caller needs it — every attribute this function reads
    off it, ``bucket``/``repro_capability``/``proposed_scope``/
    ``diagnosis``, is part of that dataclass's stable public shape).

    *cause* names which edge triggered this dispatch — defaults to
    :data:`CAUSE_TEST_TRIAGE_MECHANICAL` for the original Test-stage
    caller; a CI-red or merge-failure caller passes
    :data:`CAUSE_CI_TRIAGE_MECHANICAL`/:data:`CAUSE_CONFLICT_TRIAGE_MECHANICAL`
    explicitly so the dispatched leg's briefing and audit trail name the
    edge that actually fired, not a hardcoded Test-stage one.

    Returns ``True`` only when `coord.board_doctor_permissions.
    sweep_dispatch_mechanic_leg` (via :data:`_MECHANIC_ACTION`) actually
    confirms ``dispatched=True`` — never from the mere absence of an
    exception (#2096). The caller must fall through to its own existing
    behaviour on ``False``, for any reason: #4100's triage-first tier is
    off (:func:`_triage_tier_enabled`), the mechanic tier itself is off,
    the repo hasn't opted in, *verdict* isn't ``BUCKET_MECHANICAL``, the
    repo is already at its per-repo capacity cap (:func:`_at_repo_capacity`,
    shared with the finisher tier), the repo is already at its OWN
    mechanic-leg concurrency cap (:func:`_at_mechanic_leg_capacity`,
    #4100), a mechanic/fix-it/finisher leg or drive-session is already in
    flight, the #4094 one-per-SHA budget is already spent at *branch_sha*,
    no machine declares the verdict's ``repro_capability``, or the
    dispatch itself declined.

    The :func:`_triage_tier_enabled` check here is a second, independent
    confirmation of the SAME question every caller already asked before
    ever producing *verdict* in the first place (#4100) — never a looser
    one: a future caller that forgot its own upstream gate still cannot
    reach a mechanic-leg dispatch through this, the one and only entry
    point every edge shares.
    """
    from coord.failure_triage import BUCKET_MECHANICAL  # noqa: PLC0415

    if getattr(verdict, "bucket", None) != BUCKET_MECHANICAL:
        return False
    if not _triage_tier_enabled(repo_name, config):
        return False
    if not _mechanic_tier_enabled(repo_name, config):
        return False
    if _at_repo_capacity(repo_name, config, board):
        import click  # noqa: PLC0415

        click.echo(
            f"warning: board-doctor NOT dispatching a mechanic leg for "
            f"{repo_name}#{issue_number} — the repo is already at its "
            "per-repo capacity cap (_at_repo_capacity, #3837); falling back "
            "to an ordinary fix round instead",
            err=True,
        )
        return False
    if _at_mechanic_leg_capacity(repo_name, config, board):
        import click  # noqa: PLC0415

        click.echo(
            f"warning: board-doctor NOT dispatching a mechanic leg for "
            f"{repo_name}#{issue_number} — the repo is already at its own "
            "mechanic-leg concurrency cap (_at_mechanic_leg_capacity, "
            "#4100); falling back to an ordinary fix round instead",
            err=True,
        )
        return False
    result = dispatch_sweep_action(
        _MECHANIC_ACTION,
        repo_name,
        issue_number,
        board,
        config,
        cause=cause,
        verdict=verdict,
        branch_sha=branch_sha,
        failed_stage_commands=failed_stage_commands,
        evidence=evidence,
        http_client=http_client,
    )
    return bool(result.get("dispatched"))


def _alert_fallback(row: StuckRow) -> bool:
    """Replicate `coord.board_doctor.sweep`'s own private ``_alert_action``
    confirmed-dispatch contract for a row :func:`_investigate` is falling
    back on (#3769).

    Only `escalation_alert_dispatcher` returning truthy (never
    :data:`ALERT_SKIPPED`, never an exception) marks the row escalated via
    `coord.state.mark_board_doctor_escalated` — the same "unconfirmed
    success is a defect" rule `sweep` itself applies, so a row this helper
    could not actually get an alert filed for stays un-escalated and the
    next sweep gets another chance, rather than this module silently
    assuming its own fallback worked.
    """
    outcome = escalation_alert_dispatcher(row)
    if outcome is ALERT_SKIPPED or not outcome:
        return False
    from coord.state import mark_board_doctor_escalated  # noqa: PLC0415

    mark_board_doctor_escalated(row.queue, row.key)
    return True


#: #3847/#3999: causes admitted as "environmental" evidence for the fix-it
#: tier's lifetime one-shot auto-rebudget (:func:`_fixit_rebudget_eligible`)
#: — evidence the #2972/#3454 blind-relaunch ceiling was never meant to
#: stop, as opposed to a row flailing on the same unresolved problem every
#: round (vimcode#2018's ~14 legs/$31 overnight is exactly the blind
#: relaunch the ceiling itself guards against; this set is strictly
#: narrower than "the cause is still alert-eligible"): each means "this
#: row's own history changed for reasons outside the fix itself" —
#: ``CAUSE_CEILING_BLOCKED_MERGEABLE`` (the row already gave up on the
#: ceiling but now reads completely clear live — review-approved, every
#: check green, PR mergeable), `queue_diagnose.CAUSE_CI_GREEN` (checks are
#: green now, whatever broke them before has cleared), and
#: `queue_diagnose.CAUSE_INTEGRATION_RED` (#3999: by definition the
#: branch's own latest Test-gate verdict already read green — CAUSE_CI_RED
#: is NOT admitted here for exactly this reason, since it covers the
#: shape where the branch's own diff is also broken — so a fix-it leg for
#: this cause is a narrow, evidence-bearing diagnosis of the merge-ref
#: combination, not a blind relaunch of an unresolved problem). A
#: diagnosed merge-conflict is NOT in this set (#3918 review): that cause
#: moved off the investigate tier entirely onto the mechanical
#: `conflict_fix_dispatch` allow-list action, which this ceiling never
#: gates, so there is no `_investigate` call for it left to rebudget.
#: Every other cause, and every confidence `_fixit_rebudget_environmental`
#: does not explicitly admit, stays governed by the ceiling exactly as
#: before (#3767: uncertain defaults to NO, the conservative existing
#: behavior).
_FIXIT_REBUDGET_ENVIRONMENTAL_CAUSES = frozenset(
    {
        CAUSE_CEILING_BLOCKED_MERGEABLE,
        queue_diagnose.CAUSE_CI_GREEN,
        queue_diagnose.CAUSE_INTEGRATION_RED,
    }
)


def _fixit_rebudget_environmental(row: StuckRow) -> bool:
    """True when *row*'s own cause/confidence read as environmental rather
    than flailing (#3847).

    This is the classifier half of :func:`_fixit_rebudget_eligible` — a
    pure, zero-cost function over fields :func:`coord.board_doctor.sweep`
    already computed this sweep; the other half is the global
    already-spent cap, checked separately in
    :func:`_fixit_rebudget_eligible` so this function never reads state.

    Requires :attr:`StuckRow.fresh` (#3777): `row.cause`/`row.confidence`
    only carry "new evidence" meaning when THIS sweep just computed them
    from a live probe. A row rebuilt by
    :func:`coord.board_doctor._episode_stuck_row` from `block_log` carries
    whatever cause/confidence were stored whenever the episode concluded,
    which can be arbitrarily stale (e.g. the PR has since merged) — that
    is not new evidence, so it must not count as environmental.

    Every admitted cause requires only ``confidence != CONFIDENCE_LOW``,
    rather than ``== CONFIDENCE_HIGH``: `CAUSE_CEILING_BLOCKED_MERGEABLE`
    and `queue_diagnose.CAUSE_CI_GREEN` never resolve to `CONFIDENCE_HIGH`
    in the first place (both always `CONFIDENCE_MEDIUM`), so a
    ``== CONFIDENCE_HIGH`` bar would silently admit neither of them ever —
    `queue_diagnose.CAUSE_INTEGRATION_RED` (#3999) always resolves to
    `CONFIDENCE_HIGH` and so is admitted either way, but the bar stays
    ``!= CONFIDENCE_LOW`` for all three so this function has one rule, not
    a per-cause exception.
    """
    if not row.fresh or row.cause not in _FIXIT_REBUDGET_ENVIRONMENTAL_CAUSES:
        return False
    return row.confidence != queue_diagnose.CONFIDENCE_LOW


def _fixit_rebudget_eligible(row: StuckRow, repo_name: str, issue_number: int) -> bool:
    """#3847: may the fix-it tier spend its *one lifetime* auto-rebudget leg
    on *repo_name*#*issue_number* right now?

    Two independent gates, both required, checked cheapest-first:

    1. The global, remove+add-resistant cap
       (`coord.state.board_doctor_fixit_rebudget_spent`) — keyed by
       ``(repo_name, issue_number)`` alone, never by assignment id or row
       key, so it survives a `drive_queue_remove`+`drive_queue_requeue`
       cycle on the live row the way the #2972/#3454 ceiling check itself
       does not (#3756 only made that ceiling check CONSULTED fleet-wide
       for the SAME live row — it does not survive a fresh row).
    2. :func:`_fixit_rebudget_environmental` — the row's own cause/
       confidence must read as environmental, not flailing.

    Both of the fix-it tier's rebudget call sites
    (:func:`_requeue_ceiling_blocked_mergeable`'s mechanical ``remove``+
    ``add``, and :func:`_investigate`'s ceiling-bypass branch) call this
    one function rather than each keeping its own judgment (#2096: one
    question, one answer) — and each calls
    `coord.state.record_board_doctor_fixit_rebudget_spent` itself, only
    once its own dispatch is CONFIRMED to have actually fired, so a row
    that turns out to be an unparseable key or a reported no-op never
    burns the lifetime leg it never used.
    """
    from coord.state import board_doctor_fixit_rebudget_spent  # noqa: PLC0415

    if board_doctor_fixit_rebudget_spent(repo_name, issue_number):
        return False
    return _fixit_rebudget_environmental(row)


#: Causes whose fix is reproducing a CI-shaped failure — GitHub Actions
#: builds the PR's merge ref (branch + current base), never the branch in
#: isolation, so a fix-it leg dispatched for either of these must merge the
#: base into its worktree before trusting a local `cargo`/`pytest` run.
#: `CAUSE_CI_RED` covers the generic case; `CAUSE_INTEGRATION_RED` is the
#: shape where the branch's own Test verdict already read green and only
#: the combination with the base is red.
_CI_SHAPED_INVESTIGATE_CAUSES = (
    queue_diagnose.CAUSE_CI_RED,
    queue_diagnose.CAUSE_INTEGRATION_RED,
)


def _merge_ref_instruction(
    repo_name: str, issue_number: int, config: "Config",
) -> str | None:
    """The merge-the-base instruction for a CI-shaped fix-it leg, or
    ``None`` when the base branch can't be resolved (unconfigured repo, a
    ``gh`` read failure) — best-effort, same posture as every other
    enrichment in this module: a resolution failure must never block the
    fix-it dispatch itself, it just leaves the briefing without this one
    extra line.

    Resolved the same way `coord.gates.build_gate_report` resolves a PR's
    actual target branch (`coord.branch_model.
    resolve_base_branch_for_issue_number`) rather than assuming
    `repo.default_branch`, so a repo on the milestone/`develop_branch`
    model gets the issue's real base, not a guess.
    """
    try:
        repo_cfg = config.repo(repo_name)
        if repo_cfg is None or not getattr(repo_cfg, "github", None):
            return None

        from coord.branch_model import (  # noqa: PLC0415
            resolve_base_branch_for_issue_number,
        )

        target_branch = resolve_base_branch_for_issue_number(
            repo_cfg, repo_cfg.github, issue_number,
        )
    except Exception:  # noqa: BLE001 — best-effort; see docstring
        return None
    return (
        "Reproduce on the MERGED tree, not the branch alone: run `git fetch "
        f"origin && git rebase origin/{target_branch}` before running the "
        "test/build command — GitHub Actions CI built this PR's merge ref "
        f"(this branch plus the current `{target_branch}`), and a local "
        "run that skips this step can stay green while CI stays red for "
        "the exact same commit. A REBASE, not a merge, so this branch "
        "stays free of merge commits and eligible for GitHub's "
        "rebase-merge."
    )


def _issue_branch(board: "Board", repo_name: str, issue_number: int) -> str | None:
    """The most recent branch an assignment recorded for *repo_name*
    ``#``*issue_number* — read-only, best-effort: returns ``None`` when no
    assignment names one, never raises on a board whose
    ``active``/``completed`` lists are absent or malformed.
    """
    candidates = list(getattr(board, "active", None) or ()) + list(
        getattr(board, "completed", None) or ()
    )
    for assignment in candidates:
        if str(getattr(assignment, "repo_name", "")) != repo_name:
            continue
        a_issue = getattr(assignment, "for_issue_number", None) or getattr(
            assignment, "issue_number", None
        )
        try:
            if a_issue is None or int(a_issue) != issue_number:
                continue
        except (TypeError, ValueError):
            continue
        branch = str(getattr(assignment, "branch", "") or "")
        if branch:
            return branch
    return None


#: `quadraui#2028`'s shape — an integration-red row whose live failing CI
#: job is confined to the repo's own sealed acceptance paths.
#: `coord.ci_fix`'s briefing already tells a dispatched fix-it leg to stop
#: with a `STUCK:` line rather than attempt a fix confined to a sealed
#: path; this is the doctor-side half — recognizing the same shape BEFORE
#: a leg is even dispatched (from `_investigate`, never from a leg's own
#: report, which this module has no access to) and paging "needs a design
#: decision" instead of burning a fix-it leg on something that briefing
#: already refuses to touch.
def _sealed_confined_lines(
    row: StuckRow, repo_name: str, issue_number: int, config: "Config",
) -> tuple[str, ...] | None:
    """The failing file path(s), when *row* is a `CAUSE_INTEGRATION_RED`
    row whose failure is entirely inside *repo_name*'s sealed acceptance
    paths — ``None`` otherwise (no sealed driver configured, the cause
    isn't `integration-red`, no PR/log evidence resolvable, or at least
    one failing path falls outside the sealed tree, which keeps this from
    misfiring on an ordinary mixed failure that happens to touch a sealed
    file alongside others).

    The evidence is the failing CI job's own log excerpt
    (`coord.ci_github.build_ci_failure_detail`), never the check's own
    NAME: a GitHub Actions check name is an operator-chosen label
    ("Tests", "coord acceptance run --all --ci (cli-pytest route, ms-37)"
    per this repo's own `.github/workflows/test.yml`), never the path of
    whatever file actually failed inside it — matching against the name
    alone could never fire on this repo's own CI. The log excerpt is read
    with the SAME `FAILED <path>` pytest short-summary parser
    (:func:`~coord.queue_diagnose.extract_failing_file_paths`) a
    baseline-red probe already uses, one parser rather than two
    independent guesses at "what file failed."

    Best-effort end to end, the same posture as every other enrichment in
    this module: no branch recorded for the issue, no open PR for that
    branch, no CI store configured, no completed failing job, or any
    `gh`/network read failing along the way all degrade to ``None`` —
    "can't tell", never "confirmed not sealed-confined".
    """
    if row.cause != queue_diagnose.CAUSE_INTEGRATION_RED:
        return None
    sealed_paths = config.acceptance.sealed_paths(repo_name)
    if not sealed_paths:
        return None
    try:
        from coord import github_ops  # noqa: PLC0415
        from coord.ci_github import build_ci_failure_detail  # noqa: PLC0415
        from coord.ci_store import build_ci_store  # noqa: PLC0415
        from coord.state import build_board  # noqa: PLC0415

        repo_cfg = config.repo(repo_name)
        if repo_cfg is None or not getattr(repo_cfg, "github", None):
            return None
        branch = _issue_branch(build_board(), repo_name, issue_number)
        if not branch:
            return None
        pr = github_ops.find_pr_for_branch(repo_cfg.github, branch)
        pr_number = pr.get("number") if pr else None
        if not pr_number:
            return None
        ci_cfg = config.ci_store
        ci_store = build_ci_store(
            ci_cfg.type or "github", host=ci_cfg.host, token_env=ci_cfg.token_env,
        )
        detail = build_ci_failure_detail(ci_store, repo_cfg.github, int(pr_number))
        if detail is None or not detail.log_excerpt:
            return None
        failing_files = queue_diagnose.extract_failing_file_paths(detail.log_excerpt)
    except Exception:  # noqa: BLE001 — best-effort; see docstring
        return None
    if not queue_diagnose.is_sealed_confined_failure(failing_files, sealed_paths):
        return None
    return failing_files


def _design_decision_options(sealed_lines: tuple[str, ...]) -> tuple[DiagnosisOption, ...]:
    """#3883 point 4's two choices — never a mechanical fix, since a sealed
    oracle only EVER changes for a human-approved reason (#944). Neither is
    marked recommended over the other: unlike every other option set this
    module builds, there genuinely is no safe default here — see the
    module-level "Why the allowlist... is NOT empty" rationale for the same
    reasoning applied to C2's allowlist. :func:`_investigate` must never
    dispatch a fix-it leg against this shape (see its own docstring),
    so this exists purely to describe the decision, never to let the sweep
    make it.
    """
    lines = ", ".join(sealed_lines) if sealed_lines else "the sealed check(s)"
    return (
        DiagnosisOption(
            label="edit the sealed suite itself and bump its minor version",
            command=(
                f"edit the sealed descriptor/test lines that now fail ({lines}) "
                "to accept the new field, then bump the acceptance suite's "
                "own minor version so the change is reviewed as a deliberate "
                "oracle update, not a quiet weakening"
            ),
        ),
        DiagnosisOption(
            label="redesign the change to stay non-breaking",
            command=(
                "make the new field/behavior additive (optional with a "
                "backward-compatible default) so the existing sealed "
                f"descriptor/test ({lines}) keeps validating unchanged"
            ),
        ),
    )


def _release_leaked_review_claim_for_row(
    repo_name: str, issue_number: int, board: "Board",
) -> bool:
    """#4003: release a leaked review-dispatch claim behind *repo_name*#
    *issue_number*'s own work leg, if one is sitting there.

    "Which work leg" is resolved the SAME way `coord.board_doctor_
    permissions.sweep_dispatch_fixit_leg` resolves it for the very row
    this pre-empts (`coord.gates._select_winning_work_assignment` over
    every `WORK_LIKE_TYPES` assignment for the issue, #2096 — never a
    second, independently-derived "which leg is this row about").
    "Leaked" is answered by :func:`coord.state.review_claim_is_leaked`,
    the SAME predicate :func:`coord.state._claim_review_dispatch_local`'s
    own self-heal uses.

    Returns ``True`` only once the release is confirmed — the claim is
    gone on re-read (:func:`coord.state.has_review_claim`) — so a release
    that silently failed (the exact #3998 incident shape: a `database is
    locked` collision on the release write itself) is reported as a
    no-op, not a false success, letting the sweep fall through to the
    alert tier rather than claiming a fix that did not land. ``False``
    with nothing released (no work leg, no claim, the claim is too fresh
    to tell apart from an in-flight dispatch, or a review genuinely is
    still running) leaves the row for the ordinary fix-it-leg dispatch
    exactly as before #4003.
    """
    from coord.gates import _select_winning_work_assignment, assignments_for_issue  # noqa: PLC0415
    from coord.models import WORK_LIKE_TYPES  # noqa: PLC0415
    from coord.state import (  # noqa: PLC0415
        has_review_claim,
        release_review_dispatch_claim,
        review_claim_is_leaked,
    )

    work_like = [
        a for a in assignments_for_issue(board, repo_name, issue_number)
        if a.type in WORK_LIKE_TYPES
    ]
    if not work_like:
        return False
    work_id = _select_winning_work_assignment(work_like).assignment_id
    if not work_id or not review_claim_is_leaked(work_id):
        return False

    release_review_dispatch_claim(work_id)
    released = not has_review_claim(work_id)
    if released:
        import click  # noqa: PLC0415

        from coord.audit import record_audit  # noqa: PLC0415

        record_audit(
            tier="operational",
            category="board_doctor",
            event_type="leaked_review_claim_released",
            actor="daemon",
            summary=(
                f"board-doctor released a leaked review-dispatch claim for "
                f"{repo_name}#{issue_number} (work assignment {work_id})"
            ),
            repo=repo_name,
            issue=issue_number,
            assignment_id=work_id,
            details={"work_assignment_id": work_id},
        )
        click.echo(
            f"board-doctor released a leaked review-dispatch claim for "
            f"{repo_name}#{issue_number} (work assignment {work_id}) — "
            "a review is dispatchable again",
            err=True,
        )
    return released


def _try_auto_split(repo_name: str, issue_number: int) -> bool:
    """#4034: attempt the board-doctor auto-split disposition for a
    ceiling-blocked row that is NOT eligible for the fix-it rebudget
    exemption, instead of falling straight to :func:`_alert_fallback`.

    Loads its own config (the caller's own `config` is not yet resolved at
    this point in :func:`_investigate` — the ceiling check runs before that)
    and builds its own board, so a caller that declines this (for ANY
    reason — the repo hasn't opted in, config failed to load, the row's
    queue entry is already gone, or the latest review's remaining blocking
    findings don't classify as missing-scope-only) falls through to the
    existing alert-fallback behaviour completely unaffected.

    Returns ``True`` only when `coord.board_doctor.run_auto_split_for_row`
    returns a disposition AND that disposition's own ``action`` confirms
    ``ACTION_AUTO_SPLIT_FILED`` — never merely because a disposition object
    came back (#2096: a write-failed disposition still alerted on its own,
    so returning `True` for it here would both double-alert AND tell the
    caller a human page never fired for a row that, in fact, still needs
    one).

    Only `ConfigError` is caught here; `build_auto_split_candidate`'s own
    live-GitHub reads (``get_issue``/``get_issue_parent``) and
    `run_auto_split_for_row`'s review-stage reset can both raise beyond
    that. The caller wraps this call in `_run_mechanical_action` so any of
    those exceptions is reported and reads as "declined" rather than
    aborting the whole sweep.
    """
    from coord.board_doctor import run_auto_split_for_row  # noqa: PLC0415
    from coord.board_doctor_followthrough import ACTION_AUTO_SPLIT_FILED  # noqa: PLC0415
    from coord.commands._common import ConfigError, resolve_and_load_config  # noqa: PLC0415

    try:
        config = resolve_and_load_config(None)
    except ConfigError:
        return False

    from coord.state import build_board  # noqa: PLC0415

    board = build_board()
    disposition = run_auto_split_for_row(
        repo_name, issue_number, config=config, board=board,
    )
    return disposition is not None and disposition.action == ACTION_AUTO_SPLIT_FILED


def _investigate(row: StuckRow) -> bool:
    """#3769's C1 wiring: dispatch a fix-it leg for an alert-eligible
    cause instead of paging a human outright.

    Builds the board/config `sweep_dispatch_fixit_leg` needs itself
    (deferred imports — `coord.board_doctor.sweep` never threads a board
    through to auto-dispatchers, only *row*) and derives ``(repo, issue)``
    from ``row.key`` the same way every other handler in this module does.
    A config that fails to resolve/load is treated as a routing failure,
    not a crash: it falls back to the alert tier the same as a declined
    machine pin would, since neither case has anywhere safe left to
    dispatch a leg.

    `sweep_dispatch_fixit_leg` itself is both the trigger guard ("only
    investigate a row the drive loop has given up on" — its own in-flight
    check against a running/pending work-like assignment or a live
    `coord drive` session) and the machine-routing guard ("pin to a
    capable host or refuse" — via `coord.auto_loop._dispatch_fix` ->
    `coord.dispatch.select_fix_machine`), so this function never
    re-derives either check; it only reads the primitive's own
    :class:`~coord.board_doctor_permissions.FixitLegDispatch` result,
    which documents exactly this disambiguation as its intended use:

    * ``dispatched=True`` — a leg started; returns ``True``.
    * ``dispatched=False`` with :data:`_FIXIT_LEG_IN_FLIGHT_MARKER` in
      ``detail`` — the drive loop (or a prior fix-it leg) is already on
      this row; left alone, returns ``False`` with no alert filed.
    * ``dispatched=False`` otherwise — no originating work assignment, or
      `_dispatch_fix` declined (no capable machine, unreachable agent,
      …) — falls back to :func:`_alert_fallback` so the row still gets a
      human-visible alert rather than silently going nowhere.

    Before any of that, `coord.drive_queue.fix_round_ceiling_blocked_for_issue`
    is consulted — the single required check (#3756/#3454) every
    independent dispatcher in the fleet must honor before relaunching a fix
    leg for an issue, so an issue whose drive-queue row already gave up on
    the #2972 fix-round ceiling cannot get an unbounded stream of
    investigate-dispatched legs on top of that (vimcode#2018's ~14 legs/$31
    overnight is exactly the failure mode this guards against). A
    ceiling-blocked row falls straight to :func:`_alert_fallback`, the same
    as a declined machine pin — UNLESS :func:`_fixit_rebudget_eligible`
    says otherwise (#3777, generalized by #3847): a row reading as
    environmental rather than flailing, that has not already spent its one
    lifetime fix-it auto-rebudget leg on this issue, still gets one
    targeted fix-it leg even past the ceiling — the ceiling's own point is
    to stop a blind relaunch, not a leg fresh evidence specifically calls
    for, and the global cap is what keeps that exception a one-time leg
    rather than a second unbounded retry stream.

    Finally, :func:`_investigate_tier_enabled` gates the whole tier on the
    global kill switch (config-level and the ``--no-investigate`` sweep
    flag) and the per-repo ``repos[].board_doctor.investigate`` opt-in
    (#3771) — a repo that has not opted in, or a sweep run with the tier
    disarmed fleet-wide, falls back to the alert tier exactly like a
    ceiling-blocked or config-load failure does. The leaked-review-claim
    release below is gated behind this same check, even though it mutates
    ``review_claims`` rather than dispatching a leg — a repo that opted out
    of the investigate tier must see NO mutation from this function, not
    just no fix-it leg.

    This function's return value answers exactly one question — "was a
    fix-it leg dispatched?" — never "was the alert fallback delivered?".
    Every fallback branch (unparseable key, ceiling-blocked issue,
    unresolvable config, tier disarmed/not opted in, no safe machine to
    pin) calls :func:`_alert_fallback` only for its side effect and always
    returns ``False`` regardless of whether the fallback alert itself was
    confirmed delivered.
    """
    parsed = _parse_row_key(row.key)
    if parsed is None:
        _alert_fallback(row)
        return False
    repo_name, issue_number = parsed

    import click  # noqa: PLC0415

    from coord.commands._common import ConfigError, resolve_and_load_config  # noqa: PLC0415
    from coord.drive_queue import fix_round_ceiling_blocked_for_issue  # noqa: PLC0415

    ceiling_bypassed = False
    if fix_round_ceiling_blocked_for_issue(repo_name, issue_number):
        if not _fixit_rebudget_eligible(row, repo_name, issue_number):
            if _run_mechanical_action(
                "auto_split", row, lambda: _try_auto_split(repo_name, issue_number),
            ):
                return True
            click.echo(
                f"warning: board-doctor NOT investigating {repo_name}#{issue_number} "
                "— its drive-queue row already gave up on the fix-round ceiling "
                "(#2972/#3454), and it either already spent its one lifetime "
                "fix-it auto-rebudget leg or no longer reads as environmental "
                "rather than flailing (#3847); falling back to the alert tier "
                "instead of dispatching another fix-it leg (#3756)",
                err=True,
            )
            _alert_fallback(row)
            return False
        ceiling_bypassed = True
        click.echo(
            f"warning: board-doctor bypassing the fix-round ceiling for "
            f"{repo_name}#{issue_number} — a fresh {row.cause} diagnosis is "
            "environmental, not the blind relaunch #3454 guards against "
            "(#3777/#3847); spending this issue's one lifetime fix-it "
            "auto-rebudget leg to continue to the investigate tier",
            err=True,
        )

    try:
        config = resolve_and_load_config(None)
    except ConfigError as exc:
        click.echo(
            f"warning: board-doctor could not load config to investigate "
            f"{repo_name}#{issue_number}: {exc}",
            err=True,
        )
        _alert_fallback(row)
        return False

    if not _investigate_tier_enabled(repo_name, config):
        click.echo(
            f"warning: board-doctor NOT investigating {repo_name}#{issue_number} "
            "— the investigate tier is disarmed for this repo (either the "
            "fleet-wide board_doctor.investigate kill switch, the "
            "--no-investigate sweep flag, or this repo has not opted in via "
            "repos[].board_doctor.investigate); falling back to the alert "
            "tier instead of dispatching a fix-it leg (#3771)",
            err=True,
        )
        _alert_fallback(row)
        return False

    sealed_lines = _sealed_confined_lines(row, repo_name, issue_number, config)
    if sealed_lines is not None:
        click.echo(
            f"warning: board-doctor NOT investigating {repo_name}#{issue_number} "
            "— its live failing check(s) are confined to this repo's sealed "
            "acceptance paths (#3883 point 4); a fix-it leg would only "
            "re-confirm the same `STUCK:` a worker's own ci-fix briefing "
            "(#3882) already refuses to touch, so this pages a design "
            "decision instead of dispatching one",
            err=True,
        )
        _alert_fallback(row)
        return False

    from coord.state import build_board  # noqa: PLC0415

    board = build_board()

    # #4003: a `gate-blocked` drive-queue row whose live gate reads
    # "not ready" can mean the review this row's work leg is waiting on
    # never actually got dispatched, because the `review_claims` row that
    # claimed its dispatch leaked (a `database is locked` collision right
    # after the claim landed, or on the deny-path release — see
    # `coord.state.release_review_dispatch_claim`'s own #4003 note).
    # `sweep_dispatch_fixit_leg` below has no idea any of that happened —
    # it would just dispatch a brand new WORK leg for already-completed
    # work, which fixes nothing. Checked and released here, as its own
    # confirmed auto-tier action, only once every fallback gate above
    # (ceiling, config, tier opt-in, sealed-confined) has already passed —
    # this mutates `review_claims`, so a repo that opted out of the
    # investigate tier must see no mutation at all, not merely no fix-it
    # leg.
    if row.queue == "drive-queue" and row.cause == queue_diagnose.CAUSE_GATE_BLOCKED:
        if _release_leaked_review_claim_for_row(repo_name, issue_number, board):
            return True

    # #4126: try the model-chosen investigator decision first, in place of
    # a fix-it leg — a bounded `claude -p` call picking a verb from
    # `MODEL_ELIGIBLE_ACTIONS` through the SAME precondition-checked
    # `dispatch_investigator_action` wrapper a human `doctor-act`
    # invocation uses. Opt-in only (`_model_investigator_enabled`,
    # default off): when it declines (capped, low-confidence, "none", a
    # parse/dispatch failure) this falls straight through to the existing
    # fix-it-leg dispatch below, unchanged.
    if _model_investigator_enabled():
        model_decision = decide_with_model_investigator(
            row, repo_name, issue_number, board, config,
        )
        if model_decision.acted:
            return True

    evidence = row.evidence
    if row.cause in _CI_SHAPED_INVESTIGATE_CAUSES:
        # The fixit-leg briefing (`_fixit_leg_briefing`) renders *evidence*
        # verbatim under "Evidence:" — this is the only channel that
        # reaches the dispatched worker, since that briefing carries no
        # separate merge-ref/CI-excerpt section the way
        # `coord.ci_fix.build_ci_fix_briefing` does. Appended, never
        # prepended, so the diagnosis's own decisive line — which already
        # names the failing check(s), the closest thing to a CI excerpt
        # this tier has — stays first.
        instruction = _merge_ref_instruction(repo_name, issue_number, config)
        if instruction:
            evidence = (*evidence, instruction)

    result = dispatch_sweep_action(
        _INVESTIGATE_ACTION,
        repo_name,
        issue_number,
        board,
        config,
        cause=row.cause,
        evidence=evidence,
    )
    if result.get("dispatched"):
        if ceiling_bypassed:
            from coord.state import record_board_doctor_fixit_rebudget_spent  # noqa: PLC0415

            record_board_doctor_fixit_rebudget_spent(repo_name, issue_number)
        return True
    detail = result.get("detail", "")
    if _FIXIT_LEG_IN_FLIGHT_MARKER in detail:
        return False
    click.echo(
        f"warning: board-doctor fix-it leg not dispatched for "
        f"{repo_name}#{issue_number}: {detail}",
        err=True,
    )
    _alert_fallback(row)
    return False


def _register_investigate_action(cause: str) -> None:
    """The only way this module may populate :data:`INVESTIGATE_CAUSES` /
    :data:`AUTO_DISPATCHERS` for the investigate tier (#3769) — mirrors
    :func:`_register_auto_action`'s authorization discipline without
    touching :data:`ALLOWLIST`, which stays reserved for mechanical,
    per-cause-reviewed fixes (see the module docstring).
    """
    authorize_sweep_action(_INVESTIGATE_ACTION)

    def _gated(row: StuckRow) -> bool:
        authorize_sweep_action(_INVESTIGATE_ACTION)
        return _investigate(row)

    INVESTIGATE_CAUSES.add(cause)
    AUTO_DISPATCHERS[cause] = _gated


for _investigate_cause in (
    queue_diagnose.CAUSE_PR_CLOSED,
    # #3918: `CAUSE_MERGE_CONFLICT` moved OFF the investigate tier onto the
    # mechanical C2 allow-list above (`conflict_fix_dispatch`) — a merge
    # conflict has a known-safe deterministic fix (the ordinary
    # conflict-fix leg) and must never compete with every other
    # alert-eligible cause for the shared per-repo fix-it-leg budget.
    queue_diagnose.CAUSE_DEAD_LEG,
    queue_diagnose.CAUSE_NO_PR,
    queue_diagnose.CAUSE_CI_RED,
    # #3882: a `ci-red` row whose branch Test verdict already read green —
    # the merged-tree-only failure `queue_diagnose._rule_integration_red`
    # exists to name. Routed through the SAME `sweep_dispatch_fixit_leg`
    # primitive as plain `ci-red` rather than a bespoke dispatcher: the
    # fix still needs a worker that can change code, bounded by the same
    # per-repo investigate-tier budget (`FIXIT_LEG_BUDGET_DEFAULT`) and
    # convergence limit as every other investigate-eligible cause.
    queue_diagnose.CAUSE_INTEGRATION_RED,
    queue_diagnose.CAUSE_CI_PENDING,
    queue_diagnose.CAUSE_GATE_BLOCKED,
    queue_diagnose.CAUSE_AGENT_UNREACHABLE,
    queue_diagnose.CAUSE_UNKNOWN,
):
    _register_investigate_action(_investigate_cause)
del _investigate_cause


def _decomposition_tier_enabled(repo_name: str, config: "Config") -> bool:
    """#3986: the ONE gate for the decomposition act tier — NOT a new
    per-repo ``board_doctor.*`` opt-in (the issue's own instruction), but
    the exact switch #3985 already gates the identical mechanical filing
    path with: `coord.config.Config.auto_split_enabled`. A repo that
    trusts that gate to file a split a WORK leg proposed has already
    trusted it to file one a read-only decomposition-followup leg
    proposes — the mechanical verification
    (`coord.preflight_size.verify_split_proposal`) in between is identical
    either way.
    """
    return config.auto_split_enabled(repo_name)


def _decomposition_leg_machine(repo_name: str, config: "Config") -> "Machine | None":
    """Pick a machine for the #3986 decomposition-followup leg.

    Delegates to `coord.new_issue_chat.pick_new_issue_chat_machine` — ANY
    unpaused machine with a resolved ``repo_path`` for *repo_name*, no
    freshness/capacity weighting — rather than inventing a second "which
    machine" answer here (#2096): that function's own docstring already
    states the reason this leg shares, verbatim: read-only and
    short-lived, so any qualified machine works.
    """
    from coord.new_issue_chat import pick_new_issue_chat_machine  # noqa: PLC0415

    return pick_new_issue_chat_machine(config, repo_name)


def _dispatch_decomposition_leg(
    row: StuckRow,
    *,
    repo: "Repo",
    issue_number: int,
    board: "Board",
    config: "Config",
) -> bool:
    """#3986: the "nothing dispatched yet" half of the decomposition act
    tier — fire the one read-only decomposition-followup leg this issue is
    ever allowed (the one-shot cap is the board scan in
    :func:`_decomposition_followthrough`, which never calls this a second
    time once a matching `review_target` exists on the board).
    """
    import click  # noqa: PLC0415

    from coord import github_ops  # noqa: PLC0415
    from coord.decomposition_chat import (  # noqa: PLC0415
        SizeTriageError,
        board_doctor_decomposition_review_target_for,
        collect_needs_decomposition_comments,
        dispatch_size_triage_review,
    )

    try:
        issue_data = github_ops.get_issue(repo.github, issue_number)
    except RuntimeError as exc:
        click.echo(
            f"warning: board-doctor could not fetch {repo.name}#{issue_number} "
            f"to dispatch its decomposition-followup leg: {exc}",
            err=True,
        )
        _alert_fallback(row)
        return False

    # Already closed by some other path (an operator's own split, a
    # replayed transition) — nothing left to decompose; leave quietly,
    # same fail-open posture `auto_split_needs_decomposition` takes for a
    # closed original.
    if str(issue_data.get("state") or "").upper() == "CLOSED":
        return False

    machine = _decomposition_leg_machine(repo.name, config)
    if machine is None:
        click.echo(
            f"warning: board-doctor NOT decomposing {repo.name}#{issue_number} "
            "— no unpaused machine with a resolved repo_path claims this repo",
            err=True,
        )
        _alert_fallback(row)
        return False

    prior_proposals = collect_needs_decomposition_comments(repo.github, issue_number)

    try:
        dispatch_size_triage_review(
            repo_cfg=repo,
            machine=machine,
            issue_number=issue_number,
            issue_title=issue_data.get("title") or f"issue #{issue_number}",
            issue_body=issue_data.get("body") or "",
            board=board,
            prior_proposals=prior_proposals,
            review_target=board_doctor_decomposition_review_target_for(issue_number),
        )
    except SizeTriageError as exc:
        click.echo(
            f"warning: board-doctor decomposition-followup leg not "
            f"dispatched for {repo.name}#{issue_number}: {exc}",
            err=True,
        )
        _alert_fallback(row)
        return False
    return True


def _consume_decomposition_leg(
    row: StuckRow,
    *,
    repo: "Repo",
    issue_number: int,
    terminal: "Assignment",
    config: "Config",
) -> bool:
    """The "a decomposition-followup leg already ran" half — fetch its raw
    log and feed it through the SAME `coord.decomposition_chat.
    consume_split_verdict` chain `coord.decomposition_chat.
    auto_split_needs_decomposition` uses, so a verified SPLIT is filed
    exactly once and anything else alerts instead of retrying.

    `_decomposition_followthrough`'s board scan matches the SAME terminal
    `Assignment` on every later sweep for as long as it stays on the
    board — nothing in this module ever removes it, and `block_log`'s own
    episode for this row only resolves via a tick that still sees the key
    in the queue, which a filed split's own dequeue ensures never happens
    again. So this function is its own idempotency boundary: it re-reads
    the original issue's live GitHub state, and separately checks for an
    already-filed epic, BEFORE ever calling `consume_split_verdict` again
    for the same `terminal` leg — two checks, because a single "is the
    original closed" test only catches the shape where filing fully
    succeeded; it misses a filing that raised partway through (the epic,
    maybe its children, already exist, but closing the original failed
    and never ran), which the epic-body marker scan catches instead.

    Every failure branch here is terminal for THIS leg — it calls
    :func:`_alert_fallback` and returns ``False``, never raises, except
    the two idempotency short-circuits below, which quietly return
    ``False`` with no new alert since there is nothing new to report: an
    earlier sweep already alerted (or already succeeded) for this exact
    leg.
    """
    import click  # noqa: PLC0415

    from coord import github_ops  # noqa: PLC0415

    try:
        issue_data = github_ops.get_issue(repo.github, issue_number)
    except RuntimeError as exc:
        click.echo(
            f"warning: board-doctor could not fetch {repo.name}#"
            f"{issue_number} to consume its decomposition-followup leg: "
            f"{exc}",
            err=True,
        )
        _alert_fallback(row)
        return False

    if str(issue_data.get("state") or "").upper() == "CLOSED":
        # A split for this exact terminal leg was already filed (and the
        # original closed) by an earlier sweep — or an operator closed it
        # some other way. Either way there is nothing left for this leg
        # to do, and no new alert: whichever sweep actually finished this
        # already reported its own outcome.
        return False

    machine = next(
        (m for m in config.machines if m.name == terminal.machine_name), None
    )
    if machine is None or not terminal.assignment_id:
        _alert_fallback(row)
        return False

    from coord.notify import _fetch_raw_log_text_by_id  # noqa: PLC0415

    raw_log = _fetch_raw_log_text_by_id(terminal.assignment_id, None, machine.host)
    if raw_log is None:
        _alert_fallback(row)
        return False

    from coord.upstream_gaps import final_message_from_log_text  # noqa: PLC0415

    # `raw_log` is the stream-json log exactly as the agent wrote it — the
    # worker's final message lives inside it as an escaped string (``\n``
    # for line breaks, ``\'`` for quotes), so `consume_split_verdict`'s own
    # `## Child N:` heading regex (MULTILINE) can never match it directly.
    # Decode the SAME way `coord.notify._capture_needs_decomposition_split`
    # already does for the worker-message path, so both paths parse an
    # identical shape (#2096/#2085 — "one question, one answer").
    text = final_message_from_log_text(raw_log)
    if not text:
        _alert_fallback(row)
        return False

    from coord.decomposition_chat import (  # noqa: PLC0415
        BOARD_DOCTOR_DECOMPOSITION_ORIGIN,
        SizeTriageError,
        consume_split_verdict,
        find_filed_split_epic,
    )

    existing_epic = find_filed_split_epic(repo, issue_number)
    if existing_epic is not None:
        # The original is still open, but an epic for it already exists —
        # a previous consume got far enough to file the epic (and maybe
        # its children) before something downstream (re-pointing a
        # dependent, closing the original) raised. Filing a second epic
        # from the same raw log would duplicate it; finishing the
        # supersede by hand is now an operator's job.
        click.echo(
            f"warning: board-doctor found an already-filed split epic "
            f"#{existing_epic} for {repo.name}#{issue_number} that never "
            "confirmed superseding the original — not filing a second "
            "one",
            err=True,
        )
        _alert_fallback(row)
        return False

    issue_title = (
        issue_data.get("title") or terminal.issue_title or f"issue #{issue_number}"
    )

    try:
        epic_number = consume_split_verdict(
            text,
            repo=repo,
            issue_number=issue_number,
            issue_title=issue_title,
            origin=BOARD_DOCTOR_DECOMPOSITION_ORIGIN,
        )
    except SizeTriageError as exc:
        click.echo(
            f"warning: board-doctor decomposition-followup leg for "
            f"{repo.name}#{issue_number} produced no usable split: {exc}",
            err=True,
        )
        _alert_fallback(row)
        return False
    except Exception as exc:  # noqa: BLE001 — never crash the sweep
        click.echo(
            f"warning: board-doctor decomposition-followup leg for "
            f"{repo.name}#{issue_number} failed while filing its split: {exc}",
            err=True,
        )
        _alert_fallback(row)
        return False

    if epic_number is None:
        # A clean PROCEED verdict: the followup leg read the issue and
        # decided no split is warranted after all. Nothing was filed, so
        # the row stays parked exactly as it was — an operator judgment
        # call, same as every other terminal-park cause, so this alerts
        # rather than silently leaving it with no next step named.
        _alert_fallback(row)
        return False

    try:
        removal = dispatch_sweep_action("drive_queue_remove", repo.name, issue_number)
    except SweepPermissionError as exc:
        click.echo(
            f"warning: board-doctor filed epic #{epic_number} for "
            f"{repo.name}#{issue_number} but the sweep refused to remove "
            f"its superseded drive-queue row: {exc}",
            err=True,
        )
        return True
    if not removal.get("removed"):
        click.echo(
            f"warning: board-doctor filed epic #{epic_number} for "
            f"{repo.name}#{issue_number} but its drive-queue row did not "
            "confirm removed",
            err=True,
        )
    return True


def _decomposition_followthrough(row: StuckRow) -> bool:
    """#3986's C2/C1-shaped wiring for
    :data:`queue_diagnose.CAUSE_NEEDS_DECOMPOSITION`: act, don't just
    alert, on a terminal needs-decomposition park — dispatch ONE read-only
    decomposition-followup leg per issue, then consume its verdict the
    next time this fires and the leg has gone terminal.

    The one-shot cap ("at most one decomposition leg per issue") is this
    function's own scan of `board.active`/`board.completed` for an
    assignment whose `review_target` is `coord.decomposition_chat.
    board_doctor_decomposition_review_target_for(issue_number)` — the
    IDENTICAL "has a matching leg already run" ledger `coord.dispatch.
    enforce_preflight_guard` (#3818) already relies on for its own
    size-triage dispatch, scoped to THIS leg's own distinct review_target
    so the two can never mistake the other's terminal leg for their own.
    Three outcomes:

    * No matching assignment at all — dispatch one
      (:func:`_dispatch_decomposition_leg`); a confirmed dispatch returns
      ``True``.
    * A matching assignment still non-terminal — the leg is already
      running; leave it alone, no alert, same as :func:`_investigate`'s
      own in-flight branch. Returns ``False``.
    * A matching assignment that has gone terminal — consume its verdict
      (:func:`_consume_decomposition_leg`): a verified split files +
      queues the epic, dequeues the superseded original, and returns
      ``True``; anything else (unparseable, PROCEED, gate-rejected, a
      filing failure) alerts via :func:`_alert_fallback` and returns
      ``False`` — "never relaunch the original Work leg" per the issue's
      own instruction, so there is no third attempt.

    Before any of that, :func:`_decomposition_tier_enabled` gates the
    whole tier on `decomposition.auto_split` (#3985's own switch, not a
    new opt-in) — a repo that has not opted in falls back to the alert
    tier exactly like every other gated tier in this module does when
    disarmed.
    """
    parsed = _parse_row_key(row.key)
    if parsed is None:
        _alert_fallback(row)
        return False
    repo_name, issue_number = parsed

    import click  # noqa: PLC0415

    from coord.commands._common import ConfigError, resolve_and_load_config  # noqa: PLC0415

    try:
        config = resolve_and_load_config(None)
    except ConfigError as exc:
        click.echo(
            f"warning: board-doctor could not load config to decompose "
            f"{repo_name}#{issue_number}: {exc}",
            err=True,
        )
        _alert_fallback(row)
        return False

    if not _decomposition_tier_enabled(repo_name, config):
        click.echo(
            f"warning: board-doctor NOT decomposing {repo_name}#{issue_number} "
            "— decomposition.auto_split is disarmed for this repo (#3985); "
            "falling back to the alert tier instead of dispatching a "
            "decomposition-followup leg (#3986)",
            err=True,
        )
        _alert_fallback(row)
        return False

    repo = config.repo(repo_name)
    if repo is None:
        _alert_fallback(row)
        return False

    from coord.dao import TERMINAL_STATUSES  # noqa: PLC0415
    from coord.decomposition_chat import (  # noqa: PLC0415
        board_doctor_decomposition_review_target_for,
    )
    from coord.state import build_board  # noqa: PLC0415

    board = build_board()
    review_target = board_doctor_decomposition_review_target_for(issue_number)
    matches = [
        a
        for a in (*board.active, *board.completed)
        if a.review_target == review_target and a.repo_name == repo_name
    ]
    in_flight = next((a for a in matches if a.status not in TERMINAL_STATUSES), None)
    if in_flight is not None:
        return False

    if matches:
        terminal = max(matches, key=lambda a: a.dispatched_at or 0.0)
        return _consume_decomposition_leg(
            row, repo=repo, issue_number=issue_number, terminal=terminal, config=config,
        )

    return _dispatch_decomposition_leg(
        row, repo=repo, issue_number=issue_number, board=board, config=config,
    )


def _register_decomposition_action(
    cause: str, handler: Callable[[StuckRow], bool]
) -> None:
    """The only way this module may populate :data:`DECOMPOSITION_CAUSES` /
    :data:`AUTO_DISPATCHERS` for the decomposition act tier.

    Deliberately does NOT call `authorize_sweep_action` for the tier as a
    whole the way :func:`_register_auto_action`/
    :func:`_register_investigate_action` do — see
    :data:`DECOMPOSITION_CAUSES`'s own docstring for why leg-dispatch and
    split-filing answer to a different, already-reviewed mechanical gate
    (`coord.preflight_size.verify_split_proposal`) instead of
    `SAFE_TIER_ACTIONS`. The registered handler's own drive-queue-row
    removal still goes through `dispatch_sweep_action` at the call site.
    """
    DECOMPOSITION_CAUSES.add(cause)
    AUTO_DISPATCHERS[cause] = handler


_register_decomposition_action(
    queue_diagnose.CAUSE_NEEDS_DECOMPOSITION, _decomposition_followthrough
)


def _investigate_repo_capacity(repo_name: str, config: "Config") -> int:
    """The investigate tier's per-repo dispatch cap for *repo_name* (#3770).

    Reuses *repo_name*'s existing drive-queue ceiling verbatim — there is
    no second capacity knob for board-doctor to configure. Mirrors
    `coord.commands.drive_queue.drive_queue_tick`'s own resolution order
    exactly: `coord.drive_queue.resolve_max_parallel_per_repo` with no CLI
    override (board-doctor has no such flag) for the fleet-wide default,
    then `coord.drive_queue.resolve_repo_max_parallel` for *repo_name*'s own
    ``repos[].max_parallel`` override, if any.

    ``0`` from either source means "no per-repo ceiling at all"
    (unlimited) per that resolution's own documented semantics, not "zero
    capacity" — callers must treat a non-positive return as "never defer".
    """
    from coord.drive_queue import (  # noqa: PLC0415 — see module note on deferred imports
        resolve_max_parallel_per_repo,
        resolve_repo_max_parallel,
    )

    fleet = resolve_max_parallel_per_repo(
        override_value=None,
        override_source="board-doctor",
        config_value=config.pipeline.max_parallel_per_repo,
    )
    repo = config.repo(repo_name)
    resolution = resolve_repo_max_parallel(
        repo_name,
        repo_override=repo.max_parallel if repo is not None else None,
        fleet=fleet,
    )
    return resolution.value


def _investigate_in_flight_count(repo_name: str, board: "Board") -> int:
    """How many issues in *repo_name* are already occupied by in-flight work,
    counted the SAME two ways :func:`coord.board_doctor_permissions.
    sweep_dispatch_fixit_leg`'s own in-flight lock does — a ``running``/
    ``pending`` work-like assignment, OR a live `coord drive` tmux session
    (`coord.drive.list_drive_sessions`) — just aggregated across the whole
    repo (every issue on *board.active* / every session for *repo_name*)
    rather than scoped to one issue. An issue counted by both signals is
    only counted once, since the union is taken over issue numbers rather
    than over raw signal counts.

    This cap is shared with ordinary drive-queue work, not a dedicated
    investigate-tier budget: any running/pending work-like assignment or
    live drive session for the repo — fix-it leg or not — eats into the
    same ``max_parallel`` ceiling checked here.
    """
    from coord.drive import list_drive_sessions  # noqa: PLC0415
    from coord.models import WORK_LIKE_TYPES  # noqa: PLC0415

    busy_issues: set[int] = {
        assignment.issue_number
        for assignment in board.active
        if assignment.repo_name == repo_name
        and assignment.type in WORK_LIKE_TYPES
        and assignment.status in ("running", "pending")
    }
    busy_issues.update(
        session["issue"]
        for session in list_drive_sessions()
        if session.get("repo") == repo_name and session.get("issue") is not None
    )
    return len(busy_issues)


#: Fix-it worker legs get their own per-repo dispatch budget, independent
#: of a repo's `max_parallel` ceiling: a fixed small constant rather than
#: a second `coordinator.yml` knob, since a diagnostic/repair leg is not
#: ordinary throughput and never needs to scale with a repo's normal
#: concurrency. :func:`_at_fixit_leg_capacity` is what
#: :class:`InvestigateCapacityGate` actually compares in-flight occupancy
#: against — a full repo (every `max_parallel` slot busy with real work)
#: no longer starves its own investigate tier.
FIXIT_LEG_BUDGET_DEFAULT = 1


def _fixit_leg_in_flight_count(repo_name: str, board: "Board") -> int:
    """How many fix-it legs this doctor dispatched for *repo_name* are
    still ``running``/``pending`` — never ordinary drive-queue, review or
    smoke work, which must not consume this dedicated budget.

    An assignment counts only when both are true: it is `running`/
    `pending` on *board* right now, AND its `assignment_id` appears in
    `coord.audit`'s ``fixit_leg_dispatched`` event log — the one event
    `coord.board_doctor_permissions.sweep_dispatch_fixit_leg` records on
    every confirmed dispatch, and the only writer of that event type. A
    leg this doctor never started can therefore never be mistaken for one
    occupying its own budget, however busy the repo's ordinary queue is.
    """
    from coord.audit import query_audit_log  # noqa: PLC0415

    try:
        entries = (
            query_audit_log(
                event_type="fixit_leg_dispatched", repo=repo_name, limit=200,
            ).get("entries")
            or []
        )
    except Exception:  # noqa: BLE001 — an unreadable audit log fails open
        # ("nothing confirmed in flight") rather than permanently refusing
        # every future dispatch for this repo. Failing open is the
        # expensive direction for a budget this tight (default 1), so at
        # least make it visible rather than silently granting a fresh
        # dispatch every sweep.
        _log.warning(
            "board-doctor: could not read fixit_leg_dispatched audit log for "
            "%s — fix-it budget check fails open to 0 in-flight",
            repo_name,
            exc_info=True,
        )
        return 0
    dispatched_ids = {e.get("assignment_id") for e in entries if e.get("assignment_id")}
    if not dispatched_ids:
        return 0
    return sum(
        1
        for assignment in board.active
        if assignment.repo_name == repo_name
        and assignment.assignment_id in dispatched_ids
        and assignment.status in ("running", "pending")
    )


def _at_fixit_leg_capacity(
    repo_name: str, board: "Board", *, claimed: int = 0, budget: int | None = None
) -> bool:
    """Is *repo_name* at or over its OWN fix-it-leg budget?

    Unlike :func:`_at_repo_capacity` (still shared with the finisher tier,
    keyed off `max_parallel`), this is the investigate tier's dedicated
    cap — *budget* (:data:`FIXIT_LEG_BUDGET_DEFAULT` when ``None`` — the
    fallback also used when ``coordinator.yml``'s
    ``board_doctor.fixit_leg_budget`` is unset or unreadable), never a
    repo's ordinary concurrency ceiling. *claimed* is the caller's own
    additional in-flight count not yet reflected in *board*, same meaning
    as :func:`_at_repo_capacity`'s own *claimed* parameter.
    """
    resolved_budget = budget if budget is not None else FIXIT_LEG_BUDGET_DEFAULT
    in_flight = _fixit_leg_in_flight_count(repo_name, board) + claimed
    return in_flight >= resolved_budget


class InvestigateCapacityGate:
    """Per-sweep "is the investigate tier over its OWN budget" gate for
    `coord.board_doctor.sweep`'s ``capacity_check`` parameter.

    Built ONCE per sweep (`board = coord.state.build_board()` plus a fresh
    ``claimed`` counter at construction time), then used in two phases:

    * ``__call__`` is a pure peek — it answers "would this row's repo be
      at capacity right now" from :data:`FIXIT_LEG_BUDGET_DEFAULT`, the
      persisted in-flight signal (:func:`_fixit_leg_in_flight_count`) and
      whatever has already been CONFIRMED claimed this sweep. It never
      itself reserves a slot, so calling it twice for the same row (or not
      calling :meth:`confirm_dispatch` afterwards) never changes the
      answer. This budget is independent of *repo_name*'s `max_parallel` —
      a repo whose ordinary queue is full of real work still has its own
      small fix-it-leg allowance.
    * :meth:`confirm_dispatch` is the only thing that claims a slot.
      `coord.board_doctor.sweep` calls it only after a row's own
      dispatcher has actually returned a confirmed dispatch (``action ==
      "auto-dispatch"``) — so a row that turns out to be a
      converged-noop, an internal alert-tier fallback, or an in-flight
      no-op (`_investigate`'s three non-dispatch returns, or `sweep`'s own
      already-escalated default) never holds a slot it never used, and a
      later row in the same sweep can still get through. This is what
      keeps "exactly N dispatch, the rest defer" true across repeated
      sweeps rather than only within the first one — the old single-phase
      design reserved a slot the instant a row was LET THROUGH, before
      knowing whether it would actually dispatch, which let one
      never-dispatching row at the front of `collect_rows`' stable order
      permanently starve every later row in a ``max_parallel=1`` repo.
    """

    def __init__(self, config: "Config") -> None:
        from coord.state import build_board  # noqa: PLC0415

        # #3889 (#3875 ask B): *config*'s `board_doctor.fixit_leg_budget`
        # is read HERE, once per sweep (`make_investigate_capacity_check`
        # is called fresh every sweep) — the live knob when set,
        # `FIXIT_LEG_BUDGET_DEFAULT` on any read failure (missing section,
        # unloadable config) so a bad config never widens this budget
        # silently.
        self._config = config
        try:
            self._fixit_leg_budget = int(config.board_doctor.fixit_leg_budget)
        except Exception:  # noqa: BLE001 — fail closed to the known-safe default
            self._fixit_leg_budget = FIXIT_LEG_BUDGET_DEFAULT
        self._board = build_board()
        self._claimed: dict[str, int] = {}

    def __call__(self, row: StuckRow) -> bool:
        """``True`` means defer: *row*'s repo is at or over its OWN
        fix-it-leg budget.

        * Returns ``False`` (never defer) immediately for any cause outside
          :data:`INVESTIGATE_CAUSES` — the budget bounds the investigate
          tier only; :data:`ALLOWLIST`'s mechanical fixes are untouched.
        * Returns ``False`` if ``row.key`` does not parse — a row this gate
          cannot even attribute to a repo cannot be capacity-checked, so it
          is left to run the normal tier/dispatch path rather than silently
          stuck.
        * Otherwise defers to :func:`_at_fixit_leg_capacity` for the actual
          ``in_flight + claimed`` vs. :data:`FIXIT_LEG_BUDGET_DEFAULT`
          comparison.
        """
        if row.cause not in INVESTIGATE_CAUSES:
            return False
        parsed = _parse_row_key(row.key)
        if parsed is None:
            return False
        repo_name, _issue_number = parsed
        return _at_fixit_leg_capacity(
            repo_name,
            self._board,
            claimed=self._claimed.get(repo_name, 0),
            budget=self._fixit_leg_budget,
        )

    def confirm_dispatch(self, row: StuckRow) -> None:
        """Claim one slot for *row*'s repo after a CONFIRMED dispatch.

        No-op for rows outside :data:`INVESTIGATE_CAUSES` or with an
        unparseable key — the same rows :meth:`__call__` never defers, so
        there is nothing to claim.
        """
        if row.cause not in INVESTIGATE_CAUSES:
            return
        parsed = _parse_row_key(row.key)
        if parsed is None:
            return
        repo_name, _issue_number = parsed
        self._claimed[repo_name] = self._claimed.get(repo_name, 0) + 1


def make_investigate_capacity_check(config: "Config") -> InvestigateCapacityGate:
    """Build a fresh :class:`InvestigateCapacityGate` for one sweep (#3770)."""
    return InvestigateCapacityGate(config)


def _attempted_actions_suffix(queue: str, key: str) -> str:
    """" — already tried: ..." naming what the doctor tried on *queue*/*key*
    before this alert, and how it turned out — the one thing #3761's C4
    reason text could not say until the 90-day outcome-tracked log
    (`coord.state.board_doctor_decisions`) existed to answer it.

    Reads only the most recent handful of OUTCOME_ACTIONS entries for this
    exact row — an alert that cannot even read its own history must not
    block on it, so any read failure here degrades to "" (no suffix),
    the same fail-open posture as the rest of this module's alert path.
    """
    from coord import state  # noqa: PLC0415

    try:
        entries = state.board_doctor_decisions()
    except Exception:  # noqa: BLE001 — see docstring
        return ""
    attempts = [
        e for e in entries
        if e.get("queue") == queue
        and e.get("key") == key
        and e.get("action") in state.BOARD_DOCTOR_OUTCOME_ACTIONS
    ]
    if not attempts:
        return ""
    recent = attempts[-3:]
    parts = [
        f"{e.get('action')} (outcome={e.get('outcome') or 'pending'})" for e in recent
    ]
    return " — already tried: " + ", ".join(parts)


def _stuck_since_text(queue: str, key: str) -> str:
    """"since <timestamp>" / "since this sweep" for *queue*/*key* (#3883
    point 1's "what is stuck and since when").

    Reuses the SAME durable decision log :func:`_attempted_actions_suffix`
    already reads (`coord.state.board_doctor_decisions`) rather than a
    second timestamp source: the earliest recorded decision for this exact
    row IS the earliest moment the doctor itself can prove this row was
    already stuck. A row with no prior decision — the first sweep to ever
    flag it — reads "since this sweep" rather than inventing a timestamp
    nothing observed; a read failure degrades the same way, fail-open,
    never blocking the alert itself on this being answerable.
    """
    import datetime as _dt  # noqa: PLC0415

    from coord import state  # noqa: PLC0415

    try:
        entries = state.board_doctor_decisions()
    except Exception:  # noqa: BLE001 — see docstring
        return "since this sweep"
    matching = [e for e in entries if e.get("queue") == queue and e.get("key") == key]
    if not matching:
        return "since this sweep"
    timestamps = [state.board_doctor_decision_ts(e) for e in matching]
    timestamps = [t for t in timestamps if t is not None]
    if not timestamps:
        return "since this sweep"
    earliest = min(timestamps)
    stamp = _dt.datetime.fromtimestamp(earliest, tz=_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%MZ"
    )
    return f"since {stamp}"


def _candidate_machines_for_repo(repo_name: str, config: "Config") -> tuple["Machine", ...]:
    """Every machine declared to carry *repo_name* (``Machine.repos``) —
    the baseline-red env probe runs against each in turn (#3883 point 2),
    since coord#3870's own incident spanned TWO hosts (dellserver +
    precision) with independently expired credentials; a probe confined
    to a single host would only ever have named half the fix.

    Returns the :class:`~coord.models.Machine` objects themselves, not
    just their ``host`` strings, so a caller can also read
    ``repo_path(repo_name)`` (where the repo actually lives on that
    machine) and compare machine identity against
    :func:`coord.config.resolve_local_machine` — both of which need the
    whole record, not a bare hostname.
    """
    return tuple(
        m
        for m in (getattr(config, "machines", None) or ())
        if repo_name in (getattr(m, "repos", None) or ())
    )


def _default_env_probe_runner(
    host: str, command: str, *, cwd: str | None, base_branch: str | None,
) -> EnvProbeResult:
    """#3883 point 2's production re-run of *command* under THIS process's
    own environment (PATH, HOME) — real, never simulated.

    *cwd* must be the repo's own checkout on *host*
    (``Machine.repo_path(repo_name)``) — the same requirement
    `coord.agent.run_plain_runner_command` already enforces for this exact
    kind of re-run, since a probe with no `cwd` executes in whatever
    directory the sweep process happens to be in, never a checkout of the
    repo being diagnosed. A caller that cannot resolve a configured
    checkout path passes ``cwd=None``, which this function reports as an
    :attr:`EnvProbeResult.error` rather than guessing at a directory —
    `diagnose_baseline_red_env` then reports it as
    :data:`queue_diagnose.ENV_DIAGNOSIS_INCONCLUSIVE` exactly like any
    other probe that could not run.

    *base_branch*, when given, is checked out (fetch + hard reset to
    ``origin/<base_branch>``) in *cwd* before *command* runs — "re-run the
    failing tests on the base" (#3883 point 2) means the base branch's own
    tree, not whatever the checkout happened to have sitting in it. A
    checkout failure is reported as an error rather than falling through
    to running *command* against an unknown tree.

    Whether *host* is reachable from this process at all (same host, vs.
    the fleet's missing agent-HTTP exec primitive for a different one) is
    the CALLER's decision (:func:`coord.config.resolve_local_machine` —
    #2085's single answer to "is this host me") — this function trusts
    that it was only invoked for a host *cwd* actually resolves locally.
    """
    import subprocess  # noqa: PLC0415

    if cwd is None:
        return EnvProbeResult(
            error=f"no configured checkout for this repo on {host} (#3883)",
        )
    if base_branch:
        for prep in (
            ["git", "fetch", "origin", base_branch],
            ["git", "checkout", base_branch],
            ["git", "reset", "--hard", f"origin/{base_branch}"],
        ):
            try:
                prep_result = subprocess.run(  # noqa: S603, S607 — fixed argv, operator-configured cwd
                    prep, cwd=cwd, capture_output=True, text=True, timeout=120,
                )
            except Exception as exc:  # noqa: BLE001 — a probe failing is not a verdict
                return EnvProbeResult(
                    error=(
                        f"could not check out {base_branch} in {cwd}: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                )
            if prep_result.returncode != 0:
                return EnvProbeResult(
                    error=(
                        f"could not check out {base_branch} in {cwd}: "
                        f"{(prep_result.stderr or prep_result.stdout).strip()}"
                    ),
                )
    try:
        completed = subprocess.run(  # noqa: S602 — operator-configured test command
            command, shell=True, capture_output=True, text=True, timeout=600, cwd=cwd,
        )
    except Exception as exc:  # noqa: BLE001 — a probe failing is not a verdict
        return EnvProbeResult(error=f"{type(exc).__name__}: {exc}")
    return EnvProbeResult(
        exit_code=completed.returncode,
        output=(completed.stdout or "") + (completed.stderr or ""),
    )


def _baseline_red_env_and_lost_work_options(
    row: StuckRow, repo_name: str, config: "Config",
) -> tuple[DiagnosisOption, ...]:
    """#3883 points 2 and 3, folded into zero or more options for a
    :data:`CAUSE_BASELINE_RED` row.

    Re-runs the repo's own Test-stage command (`coord.smoke.
    resolve_smoke_command` — the SAME resolution `coord drive-queue
    sweep`'s own Test-stage dispatch uses, never a second guess at what
    "the failing tests" means) on every machine :func:`_candidate_
    machines_for_repo` names, classifies each re-run
    (`queue_diagnose.diagnose_baseline_red_env`), and — only for a host
    that genuinely reproduced a red base rather than a credential/missing-
    tool error — cross-checks the files that re-run named against the
    board's own :class:`~coord.queue_diagnose.LostWorkCandidate` branches
    (:func:`_lost_work_candidates`) for a likely ready-made fix.

    Which machine is reachable from this process at all is answered once,
    by `coord.config.resolve_local_machine` (#2085's single "is this host
    me" answer) — a machine that isn't the local one degrades straight to
    :data:`queue_diagnose.ENV_DIAGNOSIS_INCONCLUSIVE` without ever calling
    the probe runner, the fleet having no agent-HTTP exec primitive yet to
    reach a different host's environment from here. The probe itself runs
    in that machine's own configured checkout (``Machine.repo_path``) on
    the repo's base branch (`coord.branch_model.resolve_base_branch` with
    no milestone — this cause is about the repo's base, not one issue), so
    the re-run is of the actual tree the issue asks about, never whatever
    directory the sweep process happens to be in.

    Returns ``()`` whenever nothing came of any of this (no acceptance
    driver/test command configured, no machine declares the repo, every
    probe was inconclusive) — callers must then fall back to the #3761
    generic ``coord status`` wording exactly as before #3883, never
    invent a finding out of an inconclusive probe.
    """
    from coord.branch_model import resolve_base_branch  # noqa: PLC0415
    from coord.config import resolve_local_machine  # noqa: PLC0415
    from coord.smoke import resolve_smoke_command  # noqa: PLC0415

    repo_cfg = config.repo(repo_name)
    if repo_cfg is None:
        return ()
    smoke_cfg = getattr(config, "smoke_tests", None)
    command = resolve_smoke_command(repo_cfg, smoke_cfg).command if smoke_cfg else None
    if not command:
        return ()
    machines = _candidate_machines_for_repo(repo_name, config)
    if not machines:
        return ()
    base_branch = resolve_base_branch(repo_cfg, None)
    local_machine = resolve_local_machine(config)

    credential_hosts: list[str] = []
    credential_name: str | None = None
    tool_hosts: list[str] = []
    tool_name: str | None = None
    failing_files: tuple[str, ...] = ()
    for machine in machines:
        host = machine.host
        if local_machine is None or local_machine.name != machine.name:
            diagnosis = queue_diagnose.EnvDiagnosis(
                queue_diagnose.ENV_DIAGNOSIS_INCONCLUSIVE,
                f"no remote-exec primitive to reach {host} from this "
                "process yet (#3883)",
                host,
            )
        else:
            cwd = machine.repo_path(repo_name)
            diagnosis = queue_diagnose.diagnose_baseline_red_env(
                repo_name, command, host=host,
                run=lambda h, c, _cwd=cwd, _base=base_branch: (
                    _default_env_probe_runner(h, c, cwd=_cwd, base_branch=_base)
                ),
            )
        if diagnosis.category == queue_diagnose.ENV_DIAGNOSIS_CREDENTIAL:
            credential_hosts.append(host)
            credential_name = credential_name or diagnosis.credential_name
        elif diagnosis.category == queue_diagnose.ENV_DIAGNOSIS_MISSING_TOOL:
            tool_hosts.append(host)
            tool_name = tool_name or diagnosis.tool_name
        elif diagnosis.category == queue_diagnose.ENV_DIAGNOSIS_BASE_RED and not failing_files:
            failing_files = queue_diagnose.extract_failing_file_paths(
                diagnosis.raw_output
            )

    options: list[DiagnosisOption] = []
    if credential_hosts:
        named = f" ({credential_name})" if credential_name else ""
        options.append(
            DiagnosisOption(
                label=f"renew credential{named}",
                command=f"renew credential{named} on {' + '.join(credential_hosts)}",
                recommended=True,
            )
        )
    elif tool_hosts:
        options.append(
            DiagnosisOption(
                label=f"install {tool_name or 'the missing tool'}",
                command=(
                    f"install {tool_name or 'the missing tool'} on "
                    f"{' + '.join(tool_hosts)}"
                ),
                recommended=True,
            )
        )

    if failing_files:
        for candidate in _lost_work_candidates(repo_name, config, failing_files):
            options.append(
                DiagnosisOption(
                    label=(
                        f"a likely ready-made fix already exists on "
                        f"{candidate.branch} ({candidate.assignment_id})"
                    ),
                    command=(
                        f"git fetch origin {candidate.branch} && git log "
                        f"--oneline origin/{candidate.branch} -- "
                        f"{' '.join(candidate.changed_files)}"
                    ),
                    recommended=not options,
                )
            )
            break  # one named branch is enough for a page, not a list

    return tuple(options)


#: Bounds how many of a candidate branch's changed files this function
#: diffs against the base before giving up on confirming "actually
#: differs" — mirrors `coord.reconcile`'s own `_FALSE_MERGE_AUDIT_MAX_FILES`
#: (per-candidate file cap), kept as a separate constant rather than an
#: import so this module never depends on reconcile's private name.
_LOST_WORK_AUDIT_MAX_FILES = 20

#: Bounds how many distinct branches one pass of this cross-check probes
#: — a per-PASS cap on candidates, not the per-candidate file cap above.
_LOST_WORK_AUDIT_MAX_BRANCHES = 20


def _lost_work_candidates(
    repo_name: str, config: "Config", failing_files: Sequence[str],
) -> list[queue_diagnose.LostWorkCandidate]:
    """#3883 point 3: which of the board's merged-status rows for
    *repo_name* touched any of *failing_files* — a best-effort, bounded
    scan that reuses `coord.reconcile`'s own false-merge AUDIT chain (the
    SAME ``branch_exists_on_remote`` -> ``pr_is_merged`` ->
    ``branch_commits_ahead`` -> ``get_compare_files`` -> per-file
    ``get_repo_file`` content-diff steps, #2096: one question, one
    answer) so a branch whose content already landed via squash/rebase —
    gone from the remote, or ahead-but-verbatim-on-base — is ruled OUT
    here exactly as it would be by `coord.reconcile`'s own audit, rather
    than named on the page as a likely fix reconcile would have confirmed
    clean. This still never REPLACES that audit (which alone owns
    clearing a row), only answers a narrower question: "could one of
    these already be the fix a baseline-red streak is waiting on" — and
    every failure fails open to "no candidate found" rather than raising
    into the escalation path.
    """
    import click  # noqa: PLC0415

    from coord import github_ops  # noqa: PLC0415
    from coord.branch_model import resolve_base_branch_for_issue_number  # noqa: PLC0415
    from coord.state import build_board  # noqa: PLC0415

    repo_cfg = config.repo(repo_name)
    if repo_cfg is None or not getattr(repo_cfg, "github", None):
        return []

    try:
        board = build_board()
    except Exception as exc:  # noqa: BLE001 — best-effort; see docstring
        click.echo(
            f"warning: board-doctor could not read the board for the "
            f"{repo_name} lost-work cross-check: {exc}",
            err=True,
        )
        return []

    out: list[queue_diagnose.LostWorkCandidate] = []
    seen_branches: set[str] = set()
    candidates = list(getattr(board, "completed", None) or ()) + list(
        getattr(board, "active", None) or ()
    )
    for assignment in candidates:
        if str(getattr(assignment, "repo_name", "")) != repo_name:
            continue
        if str(getattr(assignment, "status", "")) != "merged":
            continue
        branch = str(getattr(assignment, "branch", "") or "")
        if not branch or branch in seen_branches:
            continue
        if len(seen_branches) >= _LOST_WORK_AUDIT_MAX_BRANCHES:
            break  # bounded scan — caps branches probed in this one pass
        seen_branches.add(branch)
        try:
            if not github_ops.branch_exists_on_remote(repo_cfg.github, branch):
                # Gone from the remote — presumed genuinely merged and
                # cleaned up (reconcile's own confirmed-clean rule), never
                # a lost-work candidate.
                continue
            issue_number = int(getattr(assignment, "for_issue_number", None)
                                or getattr(assignment, "issue_number", 0) or 0)
            base_branch = resolve_base_branch_for_issue_number(
                repo_cfg, repo_cfg.github, issue_number,
            )
            if github_ops.pr_is_merged(repo_cfg.github, branch):
                continue
            ahead = github_ops.branch_commits_ahead(repo_cfg.github, base_branch, branch)
            if not ahead:
                continue
            changed = github_ops.get_compare_files(repo_cfg.github, base_branch, branch)
            if not changed:
                continue
            content_differs = False
            for _path in changed[:_LOST_WORK_AUDIT_MAX_FILES]:
                try:
                    branch_content = github_ops.get_repo_file(repo_cfg.github, _path, branch)
                except RuntimeError:
                    continue  # file gone from the branch tip too — no signal
                try:
                    base_content = github_ops.get_repo_file(
                        repo_cfg.github, _path, base_branch,
                    )
                except RuntimeError:
                    base_content = None  # never landed on base at all
                if branch_content != base_content:
                    content_differs = True
                    break
            if not content_differs:
                # Verbatim on base already — rebase/squash, not lost
                # (reconcile's own confirmed-clean rule).
                continue
        except Exception as exc:  # noqa: BLE001 — one bad row must not sink the scan
            click.echo(
                f"warning: board-doctor lost-work probe failed for "
                f"{repo_name} branch {branch!r}: {exc}",
                err=True,
            )
            continue
        matches = queue_diagnose.find_lost_work_touching(
            failing_files,
            (
                queue_diagnose.LostWorkCandidate(
                    assignment_id=str(getattr(assignment, "assignment_id", "")),
                    repo_name=repo_name,
                    issue_number=issue_number,
                    branch=branch,
                    changed_files=tuple(changed),
                ),
            ),
        )
        out.extend(matches)
    return out


def _diagnosis_options(
    row: StuckRow,
    repo_name: str,
    issue_number: int,
    config: "Config | None",
    *,
    sealed_lines: tuple[str, ...] | None,
) -> tuple[DiagnosisOption, ...]:
    """2-3 concrete, ready-to-run operator options for *row* — exactly one
    marked recommended whenever this can tell which (#3883 point 1).

    *sealed_lines* is the caller's own, already-computed
    :func:`_sealed_confined_lines` result (#2096: one question, one
    answer — `escalation_alert_dispatcher` needs that same verdict for its
    own ``resolution_suffix``, so it is passed in here rather than asked a
    second time). Non-``None`` routes to :func:`_design_decision_options`
    instead of a "recommended" pick — see that function's own docstring
    for why neither choice is marked there. A :data:`CAUSE_BASELINE_RED`
    row with an environment/lost-work finding
    (:func:`_baseline_red_env_and_lost_work_options`) gets that finding as
    its recommended option, with the #3761 ``coord status`` default kept
    on as a second, non-recommended option. Every other row falls back to
    the #3761 :func:`_alert_resolution` command as its one recommended
    option, plus a second, generic option so the operator always has
    somewhere else to look.

    *config* may be ``None`` when the caller could not load one (the same
    best-effort posture `escalation_alert_dispatcher` already has for
    everything else) — causes that need it (baseline-red) are simply
    skipped, never crashed on.
    """
    if sealed_lines is not None:
        return _design_decision_options(sealed_lines)
    if config is not None and row.cause == CAUSE_BASELINE_RED:
        found = _baseline_red_env_and_lost_work_options(row, repo_name, config)
        if found:
            return (*found[:2], DiagnosisOption(
                label="wait — the streak clears on its own once the base is green",
                command="coord status",
            ))

    _suffix, command = _alert_resolution(row, repo_name, issue_number)
    options = [
        DiagnosisOption(
            label="act on the doctor's proposed next step", command=command, recommended=True,
        )
    ]
    if command == "coord status":
        options.append(
            DiagnosisOption(
                label="re-derive the live blocker first",
                command="coord drive-queue diagnose --json",
            )
        )
    else:
        options.append(
            DiagnosisOption(label="check overall fleet/pipeline status first", command="coord status")
        )
    return tuple(options)


def _most_recent_leg_aid(repo_name: str, issue_number: int) -> str | None:
    """The ``assignment_id`` of the most recently dispatched leg against
    *repo_name*#*issue_number*, or ``None`` on no match or a read failure.

    Reads `coord.state.load_dispatched`, the SAME dispatched-ledger read
    `coord log`/`coord sessions`/`coord status` already use (#2096) rather
    than a second history source — its own docstring guarantees the result
    is sorted oldest-first, so the last match is the most recent leg. Only
    used to make a board-doctor alert's suggested ``coord log <aid>``
    concrete; a missing or unreadable ledger must never block the alert
    itself, so this fails open to ``None`` rather than raising.
    """
    from coord.state import load_dispatched  # noqa: PLC0415

    try:
        matches = [
            d
            for d in load_dispatched()
            if d.get("repo_name") == repo_name and d.get("issue_number") == issue_number
            and d.get("assignment_id")
        ]
    except Exception:  # noqa: BLE001 — best-effort enrichment only
        return None
    return str(matches[-1]["assignment_id"]) if matches else None


def _alert_resolution(row: StuckRow, repo_name: str, issue_number: int) -> tuple[str, str]:
    """``(reason_suffix, proposed_command)`` for *row*'s cause (#3761's C4).

    Every alert-only cause gets a one-line, ready-to-run operator command
    rather than a self-executing fix — the whole point of this tier is that
    recovery needs a human decision. ``"coord status"`` is the fallback for
    causes with no single-command remedy; the causes below each have one:

    * ``queue_diagnose.CAUSE_GATE_BLOCKED`` on a **merge-queue** row — this
      cause is only ever minted there (`_MERGE_QUEUE_STUCK_CAUSE`) from the
      queue's own ``human_required`` state (a required check is red,
      possibly flaky-and-unfixable-by-rerun per #3744), so `coord merge`'s
      own ``--override-human-required`` flag is always a valid next step —
      surfaced here verbatim, never run by the sweep itself. On a
      **drive-queue** row the same cause instead means
      `queue_diagnose`'s generic ``live.gate_ready is False`` (unapproved
      review, a different red required check, or a non-mergeable PR) — NOT
      necessarily a merge-queue ``human_required`` state, so
      ``coord merge --override-human-required`` would hard-error there;
      this points at the read-only ``coord drive-queue diagnose`` instead,
      to re-derive the actual blocker before deciding what to run.
    * :data:`coord.board_doctor.CAUSE_BASELINE_RED` — the repo's base is
      red (#3386), not this row; there is no per-row command that fixes a
      broken base. `baseline_red_merge_blocked` only reclassifies a
      merge-queue entry's OWN already-skipped smoke verdict once the
      streak passes the limit (`coord.merge_queue`'s
      `evaluate_smoke_verdict`) — it does not pause drive-queue dispatch of
      new work/ci-fix legs against the repo, so the suffix says only what
      is actually true rather than claiming a pause that doesn't exist;
      the command stays ``coord status`` — the streak clears on its own
      the next time the base goes green.
    * #3979: :data:`queue_diagnose.CAUSE_NEEDS_DECOMPOSITION`/
      :data:`queue_diagnose.CAUSE_PREMISE_REFUSAL`/
      :data:`queue_diagnose.CAUSE_UPSTREAM_GAP_PARK` — a row the tick's
      own parked pre-pass already treats as terminal (the worker/upstream
      park that parked it wrote one of `coord.models`'
      ``NEEDS_DECOMPOSITION_MARKER``/``PREMISE_REFUSAL_MARKER``/
      ``UPSTREAM_GAP_PARK_MARKER`` into `last_reason`). There is no
      single-command *fix* for any of the three — each needs an
      operator judgment call (decompose, re-scope, or wait on a release)
      — so unlike every other alert-only cause above the "command" here
      is the read-only inspection step, never ``coord status``: the whole
      point is naming the SPECIFIC action, not pointing at the generic
      fleet view.
    """
    if row.cause in (
        queue_diagnose.CAUSE_NEEDS_DECOMPOSITION,
        queue_diagnose.CAUSE_PREMISE_REFUSAL,
        queue_diagnose.CAUSE_UPSTREAM_GAP_PARK,
    ):
        command = f"coord log {_most_recent_leg_aid(repo_name, issue_number) or '<aid>'}"
        action = {
            queue_diagnose.CAUSE_NEEDS_DECOMPOSITION: (
                "decompose the issue into the sub-issues already proposed "
                "on it (posted as a comment by the coordinator), then "
                "`coord drive-queue remove` + `add` for the replacement(s)"
            ),
            queue_diagnose.CAUSE_PREMISE_REFUSAL: (
                "re-scope or close the issue once the missing prerequisite "
                "it named is confirmed, then `coord drive-queue clear-refusal` "
                "and `remove` + `add` for a fresh row"
            ),
            queue_diagnose.CAUSE_UPSTREAM_GAP_PARK: (
                "wait for the named release to land or the gap to be "
                "confirmed, then `coord drive-queue remove` + `add` for a "
                "fresh row"
            ),
        }[row.cause]
        return (
            f" — this row is parked terminal, not stuck; no fix-it leg or "
            f"relaunch can resolve it — {action}; re-read its own evidence "
            f"first with: `{command}`",
            command,
        )
    if row.cause == queue_diagnose.CAUSE_GATE_BLOCKED:
        if row.queue == "merge-queue":
            command = (
                f'coord merge --only {repo_name}#{issue_number} '
                '--override-human-required "<reason the override is safe>"'
            )
            return (
                " — a required gate is blocking merge; if it is flaky, deflake "
                "or drop the check (#3744), otherwise force past it with: "
                f"`{command}`",
                command,
            )
        command = "coord drive-queue diagnose --json"
        return (
            " — the live gate is not ready (unapproved review, a red "
            "required check, or a non-mergeable PR — not necessarily a "
            "merge-queue human_required state, so `coord merge "
            "--override-human-required` may not apply here); re-derive the "
            f"actual blocker with: `{command}`",
            command,
        )
    if row.cause == CAUSE_BASELINE_RED:
        return (
            f" — {repo_name}'s merge base is red; this does not pause "
            "dispatch of new work/ci-fix legs against it, but any "
            "merge-queue entry whose own smoke run was skipped as "
            "baseline-red will be blocked from merging once the streak "
            "clears the limit — this is not fixable per-row, it clears on "
            "its own the next time the base goes green",
            "coord status",
        )
    return ("", "coord status")


#: #3999: a covering escalation's own ``proposed_command`` naming a full
#: ``remove`` + ``add`` relaunch — the exact two-command string
#: `coord.commands.drive_queue._requeue_command` builds
#: (``coord drive-queue remove <repo> <issue> && coord drive-queue add
#: <repo> <issue>``) — is a dead end the instant
#: `coord.drive_queue.existing_pr_relaunch_remedy` would itself refuse the
#: ``add`` half — "its checks are not all green — fix it forward … before
#: relaunching". A command naming only ``remove`` (e.g.
#: `coord.review.needs_redesign_escalation`'s park, which tells the
#: operator to drop the row and make a human-judgment call, never to
#: re-add it unattended) is a different, human-decision remedy that
#: `coord drive-queue remove` always succeeds at regardless of check
#: state — it must NOT be treated as refused here.
#: `_is_relaunch_shaped_remedy` is the cheap, local half of that check: a
#: textual match on the covering escalation's own recorded command, never
#: a second live `gh` probe — :func:`_covering_escalation_remedy_refused_now`
#: below pairs it with live evidence this sweep already has for the
#: opposite half.
def _is_relaunch_shaped_remedy(proposed_command: str) -> bool:
    command = proposed_command.lower()
    return "drive-queue remove" in command and "drive-queue add" in command


def _covering_escalation_remedy_refused_now(
    existing: Mapping[str, Any], row: StuckRow,
) -> bool:
    """#3999/#4033: true when *existing* — a covering escalation for a
    DIFFERENT stage than *row*'s own board-doctor alert — names a relaunch
    remedy that is already known to be a dead end for *row*.

    *existing*'s own ``proposed_command`` must be relaunch-shaped
    (:func:`_is_relaunch_shaped_remedy`) in every case — otherwise its
    remedy (a retry, an override, a human decision) is orthogonal to
    whether checks are red and must not be second-guessed from here. Given
    that, either of two independent reasons proves the relaunch dead:

    1. *row* is :attr:`StuckRow.fresh` evidence of exactly the shape that
       makes a relaunch unsafe to even attempt right now — a live-red
       check combined with the branch's OWN diff already reading green
       (:data:`queue_diagnose.CAUSE_INTEGRATION_RED`) or red
       (:data:`queue_diagnose.CAUSE_CI_RED`). A stale/non-fresh row, or
       one whose cause says nothing about the PR's checks at all, proves
       nothing about whether *existing*'s remedy still works here (#3777's
       same "new evidence only" bar `_fixit_rebudget_environmental`
       applies).
    2. *row*'s own issue already gave up on the #2972/#3454 fix-round
       ceiling (`coord.drive_queue.fix_round_ceiling_blocked_for_issue`).
       A relaunch-shaped command is exactly `_requeue_command`'s ``remove``
       + ``add`` pair, and that helper's own docstring states plainly that
       the pair does NOT reset the ceiling's own budget — so proposing it
       as "the" remedy for a row the ceiling has already given up on is a
       dead end by construction, independent of *row*'s cause/freshness:
       a diagnosed gate-blocked or merge-conflict cause still means the
       ceiling's own remedy never runs again (#4033 — the hours-long
       silent gap `_covering_escalation_names_concrete_action` alone could
       not close, because `_requeue_command`'s output is never blank).
    """
    command = str(existing.get("proposed_command") or "")
    if not _is_relaunch_shaped_remedy(command):
        return False
    if row.fresh and row.cause in _CI_SHAPED_INVESTIGATE_CAUSES:
        return True
    from coord.drive_queue import fix_round_ceiling_blocked_for_issue  # noqa: PLC0415

    parsed = _parse_row_key(row.key)
    if parsed is None:
        return False
    repo_name, issue_number = parsed
    return fix_round_ceiling_blocked_for_issue(repo_name, issue_number)


def _covering_escalation_names_concrete_action(existing: Mapping[str, Any]) -> bool:
    """Does *existing* — a different-stage escalation `write_drive_escalation`
    is weighing deferring to — actually tell an operator what to do next?

    `coord.state.get_drive_escalation` only ever returns a row that is
    still open (a dismissed row is deleted, not marked closed), so a
    non-``None`` *existing* already answers "is the covering record still
    live". It says nothing about whether that record ever carried a real
    remedy: a row whose ``proposed_command`` is blank or whitespace-only
    names nothing to run, so deferring to it leaves the stuck row with no
    alert and no actionable next step at all — worse than no cover,
    because it also blocks this writer's own alert from ever landing.
    """
    return bool(str(existing.get("proposed_command") or "").strip())


def write_drive_escalation(
    repo_name: str,
    issue_number: int,
    *,
    stage: str,
    reason: str,
    gate_readings: str,
    proposed_command: str,
    row: "StuckRow | None" = None,
) -> bool | str:
    """The ONE `drive_escalations` write path every board-doctor alert/page
    sink shares — `escalation_alert_dispatcher` below and
    `coord.board_doctor._followthrough_notify` both route through this
    rather than keeping independent copies that can disagree (#2085, #3945).

    Skips the write when a more specific, non-*stage* escalation already
    covers this issue — `drive_escalations` holds one row per issue, so an
    unconditional write here would clobber a live e.g. `plan.blocked`
    escalation's `reason`/`gate_readings`/`created_at` with a generic one.
    A covering row only actually counts as cover when
    :func:`_covering_escalation_names_concrete_action` confirms it still
    names something to run; one that does not falls straight through to
    recording this writer's own alert instead, the same as if no covering
    row existed at all. Given a row that does name an action, the skip case
    returns :data:`ALERT_SKIPPED` — UNLESS *row* is given and
    :func:`_covering_escalation_remedy_refused_now` confirms the covering
    escalation's own remedy is a relaunch already known to be a dead end
    for *row* — either because *row*'s fresh evidence already proves the
    relaunch unsafe (#3999), or because *row*'s own issue already gave up
    on the #2972/#3454 fix-round ceiling, which a relaunch does not reset
    (#4033): a dead end never "covers" anything, and leaving it in place
    would leave the row un-alerted forever rather than just until the
    stuck cause resolves, so this upgrades (overwrites) it with the real
    cause/evidence instead of skipping. *row* is ``None`` for
    `_followthrough_notify`'s own calls
    (no `StuckRow` to check), which keeps today's skip-only behavior.
    Otherwise records (or renews) the row and returns only once the write
    is confirmed by reading it back — a clean `record_drive_escalation`
    return proves the request was issued, not that it landed. Never
    raises: a failed/unconfirmed write returns `False` rather than taking
    its caller down with it.
    """
    import click  # noqa: PLC0415

    from coord.state import get_drive_escalation, record_drive_escalation  # noqa: PLC0415

    try:
        existing = get_drive_escalation(repo_name, issue_number)
        if existing is not None and existing.get("stage") != stage:
            if not _covering_escalation_names_concrete_action(existing):
                click.echo(
                    f"warning: board-doctor NOT deferring to a covering "
                    f"{existing.get('stage')!r} escalation for "
                    f"{repo_name}#{issue_number} — it names no "
                    "proposed_command at all, so it covers nothing; recording "
                    "this row's own cause/evidence instead",
                    err=True,
                )
            elif row is None or not _covering_escalation_remedy_refused_now(existing, row):
                return ALERT_SKIPPED
            else:
                click.echo(
                    f"warning: board-doctor overriding a covering "
                    f"{existing.get('stage')!r} escalation for "
                    f"{repo_name}#{issue_number} — its own remedy "
                    f"({existing.get('proposed_command')!r}) names a relaunch "
                    f"already known to be a dead end for this row (its fresh "
                    f"{row.cause} evidence, or its issue having already given "
                    "up on the fix-round ceiling — #3999/#4033); recording the "
                    "real cause instead of leaving a dead end as cover",
                    err=True,
                )
        record_drive_escalation(
            repo_name,
            issue_number,
            stage=stage,
            reason=reason,
            gate_readings=gate_readings,
            proposed_command=proposed_command,
        )
        confirmed = get_drive_escalation(repo_name, issue_number)
        return bool(
            confirmed is not None
            and confirmed.get("stage") == stage
            and confirmed.get("reason") == reason
        )
    except Exception as exc:  # noqa: BLE001 — never crash the caller
        click.echo(f"warning: could not record escalation for {repo_name}#{issue_number}: {exc}", err=True)
        return False


def escalation_alert_dispatcher(row: StuckRow) -> bool | str:
    """#3745's C4: surface an alert-tier row via `drive_escalations`.

    Parses ``row.key`` (always ``"repo#issue"`` for both drive-queue and
    merge-queue rows — `coord.drive_queue.entry_key`, `queue_diagnose.
    Diagnosis.key`, and `coord.board_doctor.merge_queue_rows` all build it
    the same way) and records/renews the escalation for that issue.

    Returns ``True`` only once the write is confirmed by reading the row
    back — `sweep`'s "escalate exactly once" contract depends on this
    return value to decide whether it may mark the row escalated, so a
    silently-swallowed failure here must never look like success. Skips
    the write when a more specific, non-board-doctor escalation already
    covers this issue, so a generic `coord status` pointer never clobbers
    it; that case returns :data:`coord.board_doctor.ALERT_SKIPPED` rather
    than ``True``, because no board-doctor alert exists to suppress future
    ones — `drive_escalations` holds one row per issue, so the covering
    escalation may be dismissed by its own writer at any time, and the
    next sweep must then be free to file this row's alert itself. #3999/
    #4033: passing *row* through to :func:`write_drive_escalation` lets
    THAT skip be bypassed too, but only for the narrow case
    :func:`_covering_escalation_remedy_refused_now` confirms — a covering
    escalation whose own remedy is a relaunch already known to be a dead
    end for *row* (its fresh evidence proves the relaunch unsafe, or its
    issue already gave up on the fix-round ceiling a relaunch cannot
    reset) never actually "covers" anything, so leaving it in place would
    dead-end the row rather than defer to a cover that could still resolve
    on its own.

    Never raises: an escalation write failing must not take the sweep
    down with it, the same contract
    `coord.commands.drive_queue._escalate_roll_ledger` already relies on —
    a `warning:` line on stderr is the floor.
    """
    import click  # noqa: PLC0415

    parsed = _parse_row_key(row.key)
    if parsed is None:
        click.echo(
            f"warning: board-doctor alert dropped — unparseable row key "
            f"{row.key!r} (expected 'repo#issue')",
            err=True,
        )
        return False
    repo_name, issue_number = parsed

    stage = _board_doctor_stage(row.queue)

    # #3883: a best-effort config load for the enrichment below
    # (sealed-path design-decision detection, baseline-red env/lost-work
    # diagnosis) — never required for the alert itself, which is exactly
    # as deliverable without it as it was before #3883 existed.
    config = None
    try:
        from coord.commands._common import resolve_and_load_config  # noqa: PLC0415

        config = resolve_and_load_config(None)
    except Exception:  # noqa: BLE001 — enrichment only, never blocks the alert
        config = None

    try:
        sealed_lines = (
            _sealed_confined_lines(row, repo_name, issue_number, config)
            if config is not None else None
        )
        if sealed_lines is not None:
            lines_text = ", ".join(sealed_lines) if sealed_lines else "the sealed check(s)"
            resolution_suffix = (
                " — needs a design decision: the live failure is confined "
                f"to this repo's own sealed acceptance paths ({lines_text}); "
                "never fixable by retrying — choose: edit the sealed suite "
                "and bump its minor version, or redesign the change to "
                "stay non-breaking"
            )
            proposed_command = "coord status"
        else:
            resolution_suffix, proposed_command = _alert_resolution(row, repo_name, issue_number)
        attempted_suffix = _attempted_actions_suffix(row.queue, row.key)
        since_text = _stuck_since_text(row.queue, row.key)
        options = _diagnosis_options(
            row, repo_name, issue_number, config, sealed_lines=sealed_lines,
        )
        options_suffix = (
            " — options: " + "; ".join(
                f"{'[recommended] ' if o.recommended else ''}{o.label}: `{o.command}`"
                for o in options
            )
            if options
            else ""
        )
        reason = (
            f"board-doctor: {row.queue} row {row.key} stuck ({since_text}) on "
            f"cause={row.cause} (confidence={row.confidence}) — see `coord "
            "drive-queue doctor-log` for the full decision log"
            f"{attempted_suffix}{resolution_suffix}{options_suffix}"
        )
        gate_readings = f"queue={row.queue} | cause={row.cause} | evidence={' | '.join(row.evidence)}"
    except Exception as exc:  # noqa: BLE001 — see docstring: never crash the sweep
        click.echo(f"warning: could not record board-doctor escalation: {exc}", err=True)
        return False

    return write_drive_escalation(
        repo_name,
        issue_number,
        stage=stage,
        reason=reason,
        gate_readings=gate_readings,
        proposed_command=proposed_command,
        row=row,
    )


def dismiss_resolved_escalation(queue: str, key: str) -> None:
    """Clear a board-doctor escalation once its row stops appearing.

    `sweep`'s ``clear_absent_rows`` walk calls this for every ``(queue,
    key)`` that dropped out of the current sweep — the cause resolved, so
    any alert filed for it is now stale. Only dismisses an escalation this
    module itself filed (``stage == "board-doctor:<queue>"``); a
    differently-staged escalation on the same issue (e.g. from
    `plan.blocked`) is left untouched, since its cause has nothing to do
    with the board-doctor row going away. Never raises — a best-effort
    cleanup failing must not take the sweep down with it.
    """
    import click  # noqa: PLC0415

    from coord.state import dismiss_drive_escalation, get_drive_escalation  # noqa: PLC0415

    parsed = _parse_row_key(key)
    if parsed is None:
        return
    repo_name, issue_number = parsed

    try:
        existing = get_drive_escalation(repo_name, issue_number)
        if existing is not None and existing.get("stage") == _board_doctor_stage(queue):
            dismiss_drive_escalation(repo_name, issue_number)
    except Exception as exc:  # noqa: BLE001 — best-effort, never crash the sweep
        click.echo(f"warning: could not dismiss board-doctor escalation: {exc}", err=True)


# ── #3881: a starting recommendation for the investigator session ──────────
#
# `coord.board_doctor_permissions`'s investigator wrapper enforces every
# verb's precondition at ACT time regardless of why it was called — this
# function exists only to save the investigator session a diagnosis step
# for the causes #3875's audit already showed a mechanical fix for, mapped
# from the issue's own evidence table. It is advice, never a decision the
# wrapper trusts blindly: :func:`recommend_investigator_action` returning a
# verb does not mean that verb's precondition currently holds, and a cause
# absent from :data:`INVESTIGATOR_RECOMMENDED_ACTION` is not an error —
# it means "this cause needs the session's own judgment," most often
# because more than one verb maps to it depending on live evidence this
# function does not have (e.g. a `gate-blocked` HUMAN_REQUIRED latch is
# only a `clear_latch` candidate when it is the conflict-fix-retry-cap
# shape AND the branch is rebaseable — see `coord.merge_queue.
# reconcile_conflict_entries`'s own docstring for why a permission/
# protection latch must never be offered that verb).
#
# #3874's row (a fix-round-ceiling block with a substantive request-
# changes verdict) deliberately has NO entry here either: its fix was a
# two-step sequence (`raise_budget` then `bounce`), which a function
# returning one verb for one cause cannot express — the investigator
# calls this again after the first verb lands, same as it re-diagnoses
# after any other action.
INVESTIGATOR_RECOMMENDED_ACTION: dict[str, str] = {
    # quadraui#2035/#2047/#2045/#2051/#2052/#2058/#2059, coord#3875: a row
    # frozen in the drive queue with nothing live against it — fixed by a
    # remove+add preserving its declaration.
    queue_diagnose.CAUSE_NEVER_DISPATCHED: "requeue",
    CAUSE_CEILING_BLOCKED_MERGEABLE: "requeue",
    # quadraui#2038: merged by hand, the queue slot still held — fixed by
    # reconciling the row to done.
    queue_diagnose.CAUSE_PR_MERGED: "mark_done",
    # coord#3870: a repo-wide baseline-red streak — fixed by dispatching
    # the base-repo's own integration fix, then (once it lands) a plain
    # merge of this row.
    CAUSE_BASELINE_RED: "integration_fix",
}


def recommend_investigator_action(row: StuckRow) -> str | None:
    """A starting recommendation for which #3881 investigator verb to try
    against *row*, derived from :data:`INVESTIGATOR_RECOMMENDED_ACTION`.

    Every name this can return is also in `coord.board_doctor_permissions.
    INVESTIGATOR_ACTIONS` — asserted once at import time just below, not
    re-checked per call, since the dict above is a fixed module constant.
    Returns ``None`` when *row*'s cause has no mechanical recommendation —
    "use judgment, or escalate," never a guessed verb.
    """
    return INVESTIGATOR_RECOMMENDED_ACTION.get(row.cause)


def _check_investigator_recommendations_are_in_the_closed_set() -> None:
    from coord.board_doctor_permissions import INVESTIGATOR_ACTIONS  # noqa: PLC0415

    unknown = set(INVESTIGATOR_RECOMMENDED_ACTION.values()) - INVESTIGATOR_ACTIONS
    if unknown:
        raise RuntimeError(
            f"coord.board_doctor_policy.INVESTIGATOR_RECOMMENDED_ACTION names "
            f"verb(s) outside INVESTIGATOR_ACTIONS: {sorted(unknown)!r}"
        )


_check_investigator_recommendations_are_in_the_closed_set()
