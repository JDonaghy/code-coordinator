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
(``CAUSE_SMOKE_NO_VERDICT``, #3810) has since joined it the same way, each
wired below via :func:`_register_auto_action`:

* ``CAUSE_CEILING_BLOCKED_MERGEABLE`` (minted by
  `coord.board_doctor._reclassify_ceiling_blocked`, never by
  `queue_diagnose` itself) — a row that already gave up on #2972's
  fix-round ceiling but reads completely clear live (review-approved,
  every check green, PR mergeable). ``drive_queue_requeue`` hands it a
  fresh fix-round budget without touching the PR itself, and without
  re-running Work/Test/Review.
* ``queue_diagnose.CAUSE_PR_MERGED`` — the row's PR already merged, so
  there is nothing left for the queue to track; ``drive_queue_remove``
  clears it.
* ``CAUSE_SMOKE_NO_VERDICT`` — a smoke leg that exhausted #2272's
  mute-leg retry budget without ever producing a parseable verdict.
  ``sweep_smoke_requeue`` clears it, bounded by its own
  `coord.smoke.BOARD_DOCTOR_REQUEUE_BUDGET` so a leg that keeps muting
  after each requeue escalates to a human instead of looping forever.

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
legs per repo at that repo's existing drive-queue ceiling
(:func:`_investigate_repo_capacity` — `coordinator.yml`
`repos[].max_parallel` / `pipeline.max_parallel_per_repo`, never a second
knob), counting legs already in flight (:func:`_investigate_in_flight_count`
— the same running/pending work-like assignment plus live `coord drive`
session signals `sweep_dispatch_fixit_leg`'s own in-flight lock already
answers) plus whatever the gate has CONFIRMED claimed earlier in the same
sweep via :meth:`InvestigateCapacityGate.confirm_dispatch`. A row the gate
defers is not dispatched, not escalated and does not touch the row's
convergence counter — it is simply deferred to the next sweep, which is
why `sweep` checks it before, not after, the counter bump. The gate only
claims a slot once a row's dispatch is confirmed (``action ==
"auto-dispatch"``), never merely because the row was let through the
check — so a row that turns out to be a converged-noop, an internal
alert-tier fallback, or an in-flight no-op never burns a slot, and a
repo with ``max_parallel=1`` keeps making progress through its rows
sweep over sweep instead of one never-dispatching row starving the rest.

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
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Callable

from coord import queue_diagnose
from coord.board_doctor import (
    ALERT_SKIPPED,
    CAUSE_BASELINE_RED,
    CAUSE_CEILING_BLOCKED_MERGEABLE,
    CAUSE_SMOKE_NO_VERDICT,
    TIER_ALERT,
    TIER_AUTO,
    StuckRow,
)
from coord.board_doctor_permissions import (
    SAFE_TIER_ACTIONS,
    SweepPermissionError,
    SweepWriteNotConfirmedError,
    authorize_sweep_action,
    dispatch_sweep_action,
)
from coord.drive_queue import parse_key as _parse_row_key

if TYPE_CHECKING:  # pragma: no cover — typing only
    from coord.config import Config
    from coord.models import Board

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
    auto-tier by either of two routes, each gated on the same safe-tier
    check so neither can promote a cause for an action
    `coord.board_doctor_permissions.SAFE_TIER_ACTIONS` does not permit:

    * The #3760 mechanical route — *cause* present in BOTH
      :data:`ALLOWLIST` and :data:`AUTO_DISPATCHERS`, and the action
      :data:`ALLOWLIST` names for it is on ``SAFE_TIER_ACTIONS``.
    * The #3769 investigate route — *cause* present in BOTH
      :data:`INVESTIGATE_CAUSES` and :data:`AUTO_DISPATCHERS`, and
      ``"dispatch_fixit_leg"`` is on ``SAFE_TIER_ACTIONS``.

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

    Returns ``True`` only when `sweep_drive_queue_requeue` confirms
    ``requeued=True`` — an unparseable row key or a reported no-op (row no
    longer queued, gate mid-flight refusal aside, since that raises instead)
    both return ``False`` so :func:`coord.board_doctor.sweep` records
    ``"auto-dispatch-noop"`` rather than falsely claiming the action fired.
    """
    parsed = _parse_row_key(row.key)
    if parsed is None:
        return False
    repo_name, issue_number = parsed
    result = dispatch_sweep_action("drive_queue_requeue", repo_name, issue_number)
    if not result.get("requeued"):
        import click  # noqa: PLC0415

        click.echo(
            f"warning: board-doctor requeue no-op for {repo_name}#"
            f"{issue_number}: {result.get('detail')}",
            err=True,
        )
        return False
    return True


def _clear_merged_row(row: StuckRow) -> bool:
    """#3760 Action 4: drop a drive-queue row whose PR already merged.

    `queue_diagnose`'s highest-priority rule already fires
    :data:`coord.queue_diagnose.CAUSE_PR_MERGED` straight off live PR state
    — no reclassification needed, unlike Action 1 — so this only has to
    remove the now-stale row.

    Returns ``True`` only when `sweep_drive_queue_remove` confirms
    ``removed=True``. A row no longer present (or an unparseable key) is a
    no-op, not a success, so :func:`coord.board_doctor.sweep` records
    ``"auto-dispatch-noop"`` rather than claiming the action fired; a
    confirmed remove whose driver session could not be stopped
    (``driver_ok=False``) still counts as a successful removal here —
    `sweep_drive_queue_remove` already logged that loudly on its own.

    A row whose #1757 deploy gate is armed or fired but not yet released
    makes `sweep_drive_queue_remove` raise :class:`SweepPermissionError`
    (`_refuse_if_remove_would_release_gate`) rather than remove anything.
    That refusal is caught HERE, not left to propagate: `sweep`'s dispatch
    call site (`coord.board_doctor.sweep`) has no `try` around it — unlike
    its own `_alert_action` — so an uncaught exception would abort the
    entire sweep, leaving every row ordered after this one unclassified and
    `clear_absent_rows` never run. Swallowing it and returning `False`
    instead reports `"auto-dispatch-noop"` for this row (recorded as a
    decision, same as any other no-op) and lets the rest of the sweep
    proceed; the row's own count keeps climbing toward the convergence
    limit, where C4 escalates it to a human instead of this action
    silently starving the sweep on every run until someone notices.
    """
    parsed = _parse_row_key(row.key)
    if parsed is None:
        return False
    repo_name, issue_number = parsed
    import click  # noqa: PLC0415

    try:
        result = dispatch_sweep_action("drive_queue_remove", repo_name, issue_number)
    except SweepPermissionError as exc:
        click.echo(
            f"warning: board-doctor remove refused for {repo_name}#"
            f"{issue_number}: {exc}",
            err=True,
        )
        return False
    if not result.get("removed"):
        click.echo(
            f"warning: board-doctor remove no-op for {repo_name}#"
            f"{issue_number} — row was not found in the drive queue",
            err=True,
        )
        return False
    return True


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


_register_auto_action(
    CAUSE_CEILING_BLOCKED_MERGEABLE,
    "drive_queue_requeue",
    _requeue_ceiling_blocked_mergeable,
)
_register_auto_action(
    queue_diagnose.CAUSE_PR_MERGED,
    "drive_queue_remove",
    _clear_merged_row,
)
_register_auto_action(
    CAUSE_SMOKE_NO_VERDICT,
    "smoke_requeue",
    _requeue_smoke_no_verdict,
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


#: #3777: the #2972/#3454 ceiling check below exists to stop a BLIND
#: relaunch loop — dispatching another fix-it leg with no better reason
#: than "the cause is still alert-eligible" (vimcode#2018's ~14 legs/$31
#: overnight). A HIGH-confidence diagnosed merge-conflict is not that: the
#: diagnosis itself is new evidence the ceiling's own ``remaining_fix_rounds``
#: exhaustion never saw, and the targeted remedy (a conflict-fix rebase) is
#: a single bounded leg, not an open-ended retry stream. Every other cause,
#: and every confidence below `CONFIDENCE_HIGH`, stays governed by the
#: ceiling exactly as before.
_CEILING_BYPASS_CAUSES = frozenset({queue_diagnose.CAUSE_MERGE_CONFLICT})


def _ceiling_bypass_eligible(row: StuckRow) -> bool:
    """True for a row the #2972 ceiling must not stop `_investigate` dispatching.

    Requires :attr:`StuckRow.fresh` (#3777): the bypass exists because a
    high-confidence diagnosed merge-conflict is new evidence the ceiling's
    blind-relaunch guard was never meant to stop — but `row.cause`/
    `row.confidence` only carry that meaning when this sweep itself just
    computed them from a live probe. A row rebuilt by
    :func:`coord.board_doctor._episode_stuck_row` from `block_log` carries
    whatever cause/confidence were stored whenever the episode concluded,
    which can be arbitrarily stale (e.g. the PR has since merged) — that is
    not new evidence, so it must not bypass anything.
    """
    return (
        row.fresh
        and row.cause in _CEILING_BYPASS_CAUSES
        and row.confidence == queue_diagnose.CONFIDENCE_HIGH
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
    as a declined machine pin — UNLESS :func:`_ceiling_bypass_eligible` says
    otherwise (#3777): a HIGH-confidence diagnosed merge-conflict still gets
    one targeted fix-it leg even past the ceiling, since that diagnosis is
    the fleet's strongest signal the row is not actually out of options, and
    the ceiling's own point is to stop a blind relaunch, not a leg a fresh
    diagnosis specifically calls for.

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

    if fix_round_ceiling_blocked_for_issue(repo_name, issue_number):
        if not _ceiling_bypass_eligible(row):
            click.echo(
                f"warning: board-doctor NOT investigating {repo_name}#{issue_number} "
                "— its drive-queue row already gave up on the fix-round ceiling "
                "(#2972/#3454); falling back to the alert tier instead of "
                "dispatching another fix-it leg (#3756)",
                err=True,
            )
            _alert_fallback(row)
            return False
        click.echo(
            f"warning: board-doctor bypassing the fix-round ceiling for "
            f"{repo_name}#{issue_number} — a fresh high-confidence diagnosed "
            f"{row.cause} is new evidence, not the blind relaunch #3454 "
            "guards against (#3777); continuing to the investigate tier",
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

    board = build_board()
    result = dispatch_sweep_action(
        _INVESTIGATE_ACTION,
        repo_name,
        issue_number,
        board,
        config,
        cause=row.cause,
        evidence=row.evidence,
    )
    if result.get("dispatched"):
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
    queue_diagnose.CAUSE_MERGE_CONFLICT,
    queue_diagnose.CAUSE_DEAD_LEG,
    queue_diagnose.CAUSE_NO_PR,
    queue_diagnose.CAUSE_CI_RED,
    queue_diagnose.CAUSE_CI_PENDING,
    queue_diagnose.CAUSE_GATE_BLOCKED,
    queue_diagnose.CAUSE_AGENT_UNREACHABLE,
    queue_diagnose.CAUSE_UNKNOWN,
):
    _register_investigate_action(_investigate_cause)
del _investigate_cause


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


class InvestigateCapacityGate:
    """Per-sweep "is the investigate tier over its per-repo cap" gate for
    `coord.board_doctor.sweep`'s ``capacity_check`` parameter.

    Built ONCE per sweep (`board = coord.state.build_board()` plus a fresh
    ``claimed`` counter at construction time), then used in two phases:

    * ``__call__`` is a pure peek — it answers "would this row's repo be
      at capacity right now" from the repo's resolved cap, the persisted
      in-flight signal (:func:`_investigate_in_flight_count`) and whatever
      has already been CONFIRMED claimed this sweep. It never itself
      reserves a slot, so calling it twice for the same row (or not
      calling :meth:`confirm_dispatch` afterwards) never changes the
      answer.
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

        self._config = config
        self._board = build_board()
        self._claimed: dict[str, int] = {}

    def __call__(self, row: StuckRow) -> bool:
        """``True`` means defer: *row*'s repo is at or over its cap.

        * Returns ``False`` (never defer) immediately for any cause outside
          :data:`INVESTIGATE_CAUSES` — the per-repo cap bounds the
          investigate tier only; :data:`ALLOWLIST`'s mechanical fixes
          (#3760) are untouched.
        * Returns ``False`` if ``row.key`` does not parse — a row this gate
          cannot even attribute to a repo cannot be capacity-checked, so it
          is left to run the normal tier/dispatch path rather than silently
          stuck.
        * Returns ``False`` (unlimited) if the repo's resolved cap
          (:func:`_investigate_repo_capacity`) is non-positive.
        * Otherwise compares ``in_flight + claimed`` against the cap.
        """
        if row.cause not in INVESTIGATE_CAUSES:
            return False
        parsed = _parse_row_key(row.key)
        if parsed is None:
            return False
        repo_name, _issue_number = parsed
        cap = _investigate_repo_capacity(repo_name, self._config)
        if cap <= 0:
            return False
        in_flight = _investigate_in_flight_count(
            repo_name, self._board
        ) + self._claimed.get(repo_name, 0)
        return in_flight >= cap

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


def _alert_resolution(row: StuckRow, repo_name: str, issue_number: int) -> tuple[str, str]:
    """``(reason_suffix, proposed_command)`` for *row*'s cause (#3761's C4).

    Every alert-only cause gets a one-line, ready-to-run operator command
    rather than a self-executing fix — the whole point of this tier is that
    recovery needs a human decision. ``"coord status"`` is the fallback for
    causes with no single-command remedy; the two causes below each have
    one:

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
    """
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

    from coord.state import get_drive_escalation, record_drive_escalation  # noqa: PLC0415

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

    try:
        existing = get_drive_escalation(repo_name, issue_number)
        if existing is not None and existing.get("stage") != stage:
            # A more specific escalation (e.g. plan.blocked) already covers
            # this issue — don't overwrite its reason/created_at with our
            # generic "coord status" pointer.
            return ALERT_SKIPPED

        resolution_suffix, proposed_command = _alert_resolution(row, repo_name, issue_number)
        reason = (
            f"board-doctor: {row.queue} row {row.key} stuck on cause="
            f"{row.cause} (confidence={row.confidence}) — see `coord "
            "drive-queue sweep --json` for the full decision log"
            f"{resolution_suffix}"
        )
        gate_readings = f"queue={row.queue} | cause={row.cause} | evidence={' | '.join(row.evidence)}"
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
    except Exception as exc:  # noqa: BLE001 — see docstring: never crash the sweep
        click.echo(f"warning: could not record board-doctor escalation: {exc}", err=True)
        return False


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
