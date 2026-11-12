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
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

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

#: Causes routed to the #3986 decomposition act tier — a THIRD, separate
#: bucket from :data:`ALLOWLIST` and :data:`INVESTIGATE_CAUSES`, because the
#: action it dispatches (a read-only decomposition-followup leg, then a
#: mechanically-verified split filing) is not on `coord.board_doctor_
#: permissions.SAFE_TIER_ACTIONS` at all — that closed allow-list governs
#: actions which mutate a drive-queue/merge-queue ROW; filing a split is a
#: GitHub-issue-filing primitive with its OWN, already-reviewed mechanical
#: gate (`coord.preflight_size.verify_split_proposal`), the same one
#: `coord.dispatch.enforce_preflight_guard` (#3818) and #3980's
#: `coord.decomposition_chat.auto_split_needs_decomposition` already call
#: directly with no `SAFE_TIER_ACTIONS` entry of their own. Populated only
#: via :func:`_register_decomposition_action`.
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


def policy_tier_of(cause: str) -> str:
    """#3745's C2 (extended by #3769): cause -> tier policy.

    Returns :data:`coord.board_doctor.TIER_AUTO` for a *cause* reaching
    auto-tier by any of three routes — the first two gated on the same
    safe-tier check so neither can promote a cause for an action
    `coord.board_doctor_permissions.SAFE_TIER_ACTIONS` does not permit;
    the third answers to its own, separate mechanical gate instead (see
    :data:`DECOMPOSITION_CAUSES`'s own docstring for why):

    * The #3760 mechanical route — *cause* present in BOTH
      :data:`ALLOWLIST` and :data:`AUTO_DISPATCHERS`, and the action
      :data:`ALLOWLIST` names for it is on ``SAFE_TIER_ACTIONS``.
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
    action = ALLOWLIST.get(cause)
    if action is not None and cause in AUTO_DISPATCHERS and action in SAFE_TIER_ACTIONS:
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
    """Does the board, read right now, still show ZERO assignments for
    *repo_name*#*issue_number*?

    `queue_diagnose.CAUSE_NEVER_DISPATCHED` is minted from exactly this
    observation (`coord.queue_diagnose.LiveState.no_assignment_ever`, read
    off `coord.gates.build_gate_report`'s own `rows`), but a
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
    """
    import click  # noqa: PLC0415

    from coord.gates import assignments_for_issue  # noqa: PLC0415
    from coord.state import build_board  # noqa: PLC0415

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
    return not assignments_for_issue(board, repo_name, issue_number)


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
        click.echo(
            f"warning: board-doctor NOT requeuing {repo_name}#{issue_number} "
            "— the board now shows at least one assignment for it, so it is "
            "no longer the never-dispatched row this cause was diagnosed "
            "from; leaving it for the alert tier rather than removing a row "
            "with live work behind it",
            err=True,
        )
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

    Mirrors :func:`_investigate_tier_enabled`/:func:`_finisher_tier_enabled`'s
    three independent gates exactly, reading the ``stale_race``-named fields:

    1. :data:`NO_STALE_RACE_ENV_VAR` — a process-wide kill switch.
    2. ``board_doctor.stale_race`` — the fleet-wide config switch.
    3. ``repos[].board_doctor.stale_race`` — the per-repo opt-in. A repo
       that has not opted in keeps today's behavior unchanged regardless of
       the fleet-wide switch.

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


#: #3847: causes admitted as "environmental" evidence for the fix-it
#: tier's lifetime one-shot auto-rebudget (:func:`_fixit_rebudget_eligible`)
#: — evidence the #2972/#3454 blind-relaunch ceiling was never meant to
#: stop, as opposed to a row flailing on the same unresolved problem every
#: round (vimcode#2018's ~14 legs/$31 overnight is exactly the blind
#: relaunch the ceiling itself guards against; this set is strictly
#: narrower than "the cause is still alert-eligible"): both mean "this
#: row's own history changed for reasons outside the fix itself" —
#: ``CAUSE_CEILING_BLOCKED_MERGEABLE`` (the row already gave up on the
#: ceiling but now reads completely clear live — review-approved, every
#: check green, PR mergeable) and `queue_diagnose.CAUSE_CI_GREEN` (checks
#: are green now, whatever broke them before has cleared). A diagnosed
#: merge-conflict is NOT in this set (#3918 review): that cause moved off
#: the investigate tier entirely onto the mechanical `conflict_fix_dispatch`
#: allow-list action, which this ceiling never gates, so there is no
#: `_investigate` call for it left to rebudget. Every other cause, and
#: every confidence `_fixit_rebudget_environmental` does not explicitly
#: admit, stays governed by the ceiling exactly as before (#3767: uncertain
#: defaults to NO, the conservative existing behavior).
_FIXIT_REBUDGET_ENVIRONMENTAL_CAUSES = frozenset(
    {
        CAUSE_CEILING_BLOCKED_MERGEABLE,
        queue_diagnose.CAUSE_CI_GREEN,
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

    Both admitted causes require only ``confidence != CONFIDENCE_LOW``,
    because `queue_diagnose` never assigns either of them `CONFIDENCE_HIGH`
    in the first place — both always resolve to `CONFIDENCE_MEDIUM` — so a
    ``== CONFIDENCE_HIGH`` bar would silently admit neither cause ever.
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
    ceiling-blocked or config-load failure does.

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
    from coord.state import build_board  # noqa: PLC0415

    ceiling_bypassed = False
    if fix_round_ceiling_blocked_for_issue(repo_name, issue_number):
        if not _fixit_rebudget_eligible(row, repo_name, issue_number):
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

    board = build_board()
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
    """#3986: the "a decomposition-followup leg already ran" half — fetch
    its raw log and feed it through the SAME `coord.decomposition_chat.
    consume_split_verdict` chain #3980's own auto-split uses, so a verified
    SPLIT is filed exactly once and anything else alerts instead of
    retrying.

    Every failure branch here is terminal for THIS leg — it calls
    :func:`_alert_fallback` and returns ``False``, never raises. The one
    decomposition leg this issue is ever allowed is this exact `terminal`
    assignment (enforced by :func:`_decomposition_followthrough`'s board
    scan, not by anything in this function), so there is nothing left to
    retry even on failure — `coord.board_doctor.sweep`'s own convergence
    counter (`CONVERGENCE_LIMIT`) stops calling this dispatcher at all
    once it has been observed enough times, falling to the plain alert
    path for good.
    """
    import click  # noqa: PLC0415

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

    from coord.decomposition_chat import (  # noqa: PLC0415
        BOARD_DOCTOR_DECOMPOSITION_ORIGIN,
        SizeTriageError,
        consume_split_verdict,
    )

    try:
        epic_number = consume_split_verdict(
            raw_log,
            repo=repo,
            issue_number=issue_number,
            issue_title=terminal.issue_title or f"issue #{issue_number}",
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
        # call, same as every other #3979 terminal-park cause, so this
        # alerts rather than silently leaving it with no next step named.
        _alert_fallback(row)
        return False

    from coord.state import dequeue_drive_queue  # noqa: PLC0415

    try:
        dequeue_drive_queue(repo.name, issue_number, operator=False)
    except Exception as exc:  # noqa: BLE001
        click.echo(
            f"warning: board-doctor filed epic #{epic_number} for "
            f"{repo.name}#{issue_number} but could not dequeue the "
            f"superseded original: {exc}",
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
    :data:`AUTO_DISPATCHERS` for the #3986 decomposition act tier.

    Deliberately does NOT call `authorize_sweep_action` the way
    :func:`_register_auto_action`/:func:`_register_investigate_action` do
    — see :data:`DECOMPOSITION_CAUSES`'s own docstring for why this tier
    answers to a different, already-reviewed mechanical gate
    (`coord.preflight_size.verify_split_proposal`) instead of
    `SAFE_TIER_ACTIONS`.
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


def write_drive_escalation(
    repo_name: str,
    issue_number: int,
    *,
    stage: str,
    reason: str,
    gate_readings: str,
    proposed_command: str,
) -> bool | str:
    """The ONE `drive_escalations` write path every board-doctor alert/page
    sink shares — `escalation_alert_dispatcher` below and
    `coord.board_doctor._followthrough_notify` both route through this
    rather than keeping independent copies that can disagree (#2085, #3945).

    Skips the write when a more specific, non-*stage* escalation already
    covers this issue — `drive_escalations` holds one row per issue, so an
    unconditional write here would clobber a live e.g. `plan.blocked`
    escalation's `reason`/`gate_readings`/`created_at` with a generic one.
    That case returns :data:`ALERT_SKIPPED`. Otherwise records (or renews)
    the row and returns only once the write is confirmed by reading it
    back — a clean `record_drive_escalation` return proves the request was
    issued, not that it landed. Never raises: a failed/unconfirmed write
    returns `False` rather than taking its caller down with it.
    """
    import click  # noqa: PLC0415

    from coord.state import get_drive_escalation, record_drive_escalation  # noqa: PLC0415

    try:
        existing = get_drive_escalation(repo_name, issue_number)
        if existing is not None and existing.get("stage") != stage:
            return ALERT_SKIPPED
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
    next sweep must then be free to file this row's alert itself.

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
