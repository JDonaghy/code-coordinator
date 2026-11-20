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

Convergence (load-bearing)
----------------------------------
A row stuck on the SAME cause across :data:`CONVERGENCE_LIMIT` (default 3)
consecutive sweeps stops being re-acted on and falls back to the alert
tier. Per-(queue, key) sweep counts persist in `board_meta` via
:mod:`coord.state`'s ``record_board_doctor_observation`` /
``mark_board_doctor_escalated`` — the same one-row-of-truth pattern
``baseline_red_streaks`` uses for its own streak counter (one question,
one answer; convergence is decided by reading that ONE counter, never
re-derived from the decision log).

Converging never means going silent: a still-unresolved cause keeps
getting re-alerted on an exponential backoff
(``coord.state.BOARD_DOCTOR_BACKOFF_INITIAL_HOURS`` doubling each firing
up to ``BOARD_DOCTOR_BACKOFF_CAP_HOURS``,
``coord.state.board_doctor_alert_due``) rather than escalating exactly
once and then being ignored forever — a row that never gets better is
exactly the one that must not go quiet.

"CONSECUTIVE" is only honest because :func:`sweep` also clears a
(queue, key)'s counter the moment that row stops appearing in the current
*rows* — ``coord.state.clear_board_doctor_row``, driven by the
*clear_absent_rows* parameter (default on). Without it a resolved-then-recurring
row would resume its stale count instead of starting over, and a row that
escalated once could never restart its backoff clock after it later
resolves and recurs.
"""

from __future__ import annotations

import json
import logging
import re
import time as _time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Collection, Mapping, Sequence

from coord import board_doctor_followthrough, queue_diagnose
from coord.dao import TERMINAL_STATUSES as _ASSIGNMENT_TERMINAL_STATUSES
from coord.models import Board

if TYPE_CHECKING:
    from coord.entry_truth import EntryTruth

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
#: re-acting on it and falls back to the (backoff-gated) alert tier.
CONVERGENCE_LIMIT = 3

#: :func:`sweep`'s *counter_namespace* for a ``mechanical_causes``-filtered
#: :func:`run_sweep` call — keeps that tight-cadence pass's convergence
#: counter separate from the plain hourly call's, so running this pass 60x
#: more often does not burn :data:`CONVERGENCE_LIMIT` 60x faster on the
#: SAME counter the hourly sweep still relies on for these causes.
MECHANICAL_SWEEP_COUNTER_NAMESPACE = "mechanical-only"

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

#: A narrow cause minted by :func:`_reclassify_checks_stale_race`, never by
#: `queue_diagnose` itself (#3859) — mirrors :data:`CAUSE_CEILING_BLOCKED_
#: MERGEABLE`'s own "promote only an independently-confirmed subset of
#: :data:`_LIVE_CLEAR_CAUSES`" shape. A drive-queue row whose live evidence
#: reads completely clear (review approved, Test passed, PR mergeable) but
#: whose owning `coord drive` session already gave up because it kept
#: losing a race against sibling PRs merging into the same repo faster than
#: its own re-run CI could finish (quadraui#1961) — never a problem with
#: the branch itself. See `coord.drive_queue.is_checks_stale_race_giveup_
#: reason` for the exact marker this reclassifier keys on.
CAUSE_CHECKS_STALE_RACE = "checks-stale-race"

#: #3887 (#3875 ask A): a narrow cause minted by :func:`_reclassify_stale_latch`,
#: never by `queue_diagnose` itself — the same "promote only an
#: independently-confirmed subset of a broader cause" shape as
#: :data:`CAUSE_CEILING_BLOCKED_MERGEABLE`. A merge-queue row latched
#: ``HUMAN_REQUIRED`` (:data:`_MERGE_QUEUE_STUCK_CAUSE`'s
#: ``CAUSE_GATE_BLOCKED`` — never ``CAUSE_MERGE_CONFLICT``: that cause is
#: GitHub's own live ``mergeable is False`` reading, which approval alone
#: cannot override, see :func:`_reclassify_stale_latch`'s docstring)
#: whose approval is ALSO confirmed, live, bound to the branch's current
#: content (`coord.merge_queue.has_approved_review`) — quadraui#2034's
#: shape: the latch is stale archaeology, not a live blocker, but nothing
#: re-tests it until a human runs `coord merge --only
#: --override-human-required` or `--drop` + re-enqueue. Registered in
#: `coord.board_doctor_policy.ALLOWLIST` against the `clear_latch` action
#: (`coord.board_doctor_permissions.sweep_clear_latch`, via #3881's
#: investigator wrapper), whose own three gates (latched, approval bound
#: to current content, live mergeability) are the real precondition —
#: this cause only decides which rows are worth the attempt, same
#: division of labor as every other allowlisted cause.
CAUSE_LATCH_STALE_MERGEABLE = "latch-stale-mergeable"

#: #3887: a `PENDING` merge-queue row `_MERGE_QUEUE_STUCK_CAUSE` otherwise
#: never names (a row merely waiting its turn is not this doctor's
#: problem) — ``entry.last_attempt`` being set AND
#: :data:`MERGE_PENDING_STALE_AFTER_SECONDS` old is only the CHEAP,
#: no-`gh` pre-filter :func:`merge_queue_rows` applies; staleness alone is
#: NOT proof every gate reads clear (a conflict-fix rebase just returning
#: the entry to `PENDING` leaves `last_attempt` at its old, pre-conflict
#: stamp while CI re-runs on the new head; a #3886 per-repo merge-gate
#: reservation leaves `last_attempt` stale for as long as it holds — #3887
#: review, two reachable false positives). :func:`_confirm_merge_pending_
#: stuck` re-confirms, LIVE, via `coord.merge_queue.entry_gate_status`
#: (review/smoke/CI/epic-keyword/reservation, the SAME order `process()`
#: evaluates), and drops the row entirely unless that read is
#: `coord.merge_queue.PLAN_READY` — only THEN does this cause survive to
#: the `merge` action (`coord.merge_queue.process()` via #3881's
#: investigator wrapper, `coord.board_doctor_permissions.
#: investigator_merge`), which re-confirms every gate live a second time
#: right before merging anything.
CAUSE_MERGE_PENDING_STUCK = "merge-pending-not-progressing"

#: #3887, pairs with #3871: a `HUMAN_REQUIRED` merge-queue row whose
#: `entry.error` carries `coord.merge_queue.CONFLICT_FIX_HEAD_UNMOVED_
#: SIGNAL` — a conflict-fix worker completed cleanly but never actually
#: pushed, and #3871's own completion-time redispatch
#: (`coord.reconcile._try_ordinary_escalation_after_head_unmoved`) could
#: not find a capable machine right then. That redispatch is a one-shot
#: synchronous attempt; this cause is the periodic net that retries it on
#: a later sweep, still within the SAME retry cap #3871 already enforces
#: (`coord.conflict_fix.dispatch_conflict_fix`'s own `after_head_unmoved_
#: fix_assignment_id` carve-out) — never a fresh budget, never the
#: ordinary give-up escalation a real conflict would trigger. Registered
#: against the `conflict_fix_redispatch` action (`coord.board_doctor_
#: permissions.sweep_redispatch_head_unmoved_conflict_fix`).
CAUSE_CONFLICT_FIX_HEAD_UNMOVED = "conflict-fix-head-unmoved"

#: #3887: how long a `PENDING` merge-queue entry must sit past its last
#: merge attempt before :data:`CAUSE_MERGE_PENDING_STUCK` fires — long
#: enough that a row merely waiting a few ordinary ticks for its turn
#: never matches, short enough that a genuinely stuck green/approved PR
#: does not wait indefinitely for a human to notice.
MERGE_PENDING_STALE_AFTER_SECONDS = 1800.0

#: #4027: the staleness floor :func:`run_sweep` uses in place of
#: :data:`MERGE_PENDING_STALE_AFTER_SECONDS` for a ``mechanical_causes``-
#: filtered (tight-cadence) call ONLY — the plain hourly call keeps the
#: 30-minute floor unchanged. 30 minutes exists to keep a row merely
#: waiting its ordinary turn off this sweep's radar; it was never a
#: correctness requirement, since :func:`_confirm_merge_pending_stuck` /
#: :func:`_reclassify_merge_never_attempted`'s own LIVE ``entry_gate_
#: status`` re-check is what actually proves a row safe to act on, not
#: this pre-filter. Left at the hourly value, the motivating "green,
#: approved, mergeable, not merging" shape would still sit for up to 30
#: minutes no matter how often the fast timer fires; this value (2
#: minutes — long enough that a row the ordinary `merge.auto_drain` tick
#: (30s cadence) is still actively retrying does not flap in and out of
#: this sweep) is what actually closes that gap down to the fast timer's
#: own cadence.
MECHANICAL_MERGE_PENDING_STALE_AFTER_SECONDS = 120.0

#: #3901 (#3887 case 4, the remainder #3887 itself claimed but never
#: wired): a `blocked`/`parked` drive-queue row that was DISPATCHED at
#: least once (it has at least one `coord.gates.assignments_for_issue`
#: board assignment behind it, which is NOT necessarily a branch or a PR —
#: not the #3874 never-dispatched shape) whose stored `last_reason` is
#: still `coord.drive_queue._resolve_prereqs`'s own unsatisfiable-`after=`
#: verdict, even though every named pre-req now reads terminal.
#:
#: `coord.drive_queue._reconcile_blocked_after` already resumes a LIVE
#: `blocked` row matching this exact shape on every ordinary drive-queue
#: tick (#2362/#2756) — but only while the periodic tick is still walking
#: that row at all. A row #3888's `no_progress_rows` surfaces here reached
#: this sweep precisely because the episode/no-progress walk, not the
#: ordinary tick, is the thing still looking at it (a budget-exhausted
#: `CAUSE_UNKNOWN` episode `_episode_stuck_row` would otherwise silently
#: drop — see that function's own docstring), so this is the belt the
#: ordinary tick's suspenders do not reach, never a second, competing
#: implementation of the same resume: see
#: :func:`_reclassify_after_prereqs_landed`.
CAUSE_AFTER_PREREQS_LANDED = "after-prereqs-landed"

#: The ONLY incoming `queue_diagnose` causes :func:`_reclassify_after_
#: prereqs_landed` may promote away from (#3901 review) — both mean the
#: live diagnosis found nothing actionable about THIS entry's own
#: PR/branch lifecycle (`CAUSE_UNKNOWN`: no rule matched, or the probe
#: read nothing at all; `CAUSE_NO_PR`: the PR probe ran and confirmed no
#: PR exists). Every other named cause (`CAUSE_PR_MERGED`,
#: `CAUSE_MERGE_CONFLICT`, `CAUSE_CI_RED`, `CAUSE_CI_PENDING`, ...) is live
#: evidence about a DIFFERENT, already-actionable blocker on the entry's
#: own PR — `ALLOWLIST`/`INVESTIGATE_CAUSES` already route some of those
#: to their own real fix (e.g. `CAUSE_PR_MERGED` -> `mark_done`, freeing
#: the stale assignment slot) and this reclassifier must never relabel one
#: of those into a blunt `drive_queue_requeue` remove+re-add instead,
#: losing the dispatch that cause exists to trigger — the same reasoning
#: :func:`_reclassify_stale_latch` spells out for why it excludes
#: `CAUSE_MERGE_CONFLICT`. Same shape as the other three reclassifiers in
#: this module (:data:`_LIVE_CLEAR_CAUSES`, `_reclassify_stale_latch`'s own
#: `CAUSE_GATE_BLOCKED` check): require a specific incoming cause, never
#: "any cause at all".
_AFTER_PREREQS_LANDED_SAFE_CAUSES = frozenset(
    {queue_diagnose.CAUSE_UNKNOWN, queue_diagnose.CAUSE_NO_PR}
)

#: #3944: a `waiting` row's `after=` edge names an issue that is open on
#: the board but has no drive-queue row of its own —
#: `coord.drive_queue._resolve_prereqs`'s own `facts.open` verdict (see
#: :data:`_STRANDED_PREREQ_RE`). Nothing ever enqueues the pre-req, so the
#: row just repeats the same deferral forever — the board-doctor sweep
#: (#3758) never saw this shape before #3944 because it carries neither a
#: `block_log` episode (only `blocked`/`parked` opens one) nor merge-queue
#: evidence, the two things :func:`collect_rows` walked before this.
#: :func:`stranded_prereq_rows` is this sweep's own classifier for it.
CAUSE_STRANDED_PREREQ = "stranded-prereq"

#: Same shape as :data:`CAUSE_STRANDED_PREREQ`, but the pre-req is one
#: :func:`stranded_prereq_guard` says this sweep must NOT enqueue on its
#: own authority — an epic, an operator opt-out label, a repo this sweep
#: cannot resolve, or one an operator deliberately removed from the queue
#: (see the issue's "Must NOT auto-queue" list, #3944). Left off
#: :data:`coord.board_doctor_policy.ALLOWLIST` on purpose, so
#: :func:`coord.board_doctor_policy.policy_tier_of` routes it straight to
#: the alert tier — the same backoff-gated escalation path every other
#: un-registered cause already gets, never a bespoke comment/event of its
#: own.
CAUSE_STRANDED_PREREQ_BLOCKED = "stranded-prereq-blocked"

#: #3944: the one marker `_resolve_prereqs`'s `facts.open` branch writes —
#: see that f-string in ``coord/drive_queue.py`` (``f"waiting on {dep}
#: (open, not queued)"``). No public alias exists in that module for this
#: one shape (unlike `is_unsatisfiable_prereq_reason`'s two), so this
#: regex is the one place outside it that knows the text; keep it in sync
#: with that f-string if it ever changes.
_STRANDED_PREREQ_RE = re.compile(r"^waiting on (?P<dep>\S+) \(open, not queued\)$")

#: The operator opt-out marker for #3944's "satisfied by a human act, not
#: a drive" case (e.g. a release-gate tracking issue like quadraui#2283) —
#: see the module-level "Must NOT auto-queue" list in the issue this
#: closes. Checked by :func:`stranded_prereq_guard` the same way it checks
#: :data:`coord.milestone_order.TRACKING_ISSUE_LABEL`.
STRANDED_PREREQ_OPT_OUT_LABEL = "coord:no-autoqueue"


def stranded_prereq_dep(last_reason: str | None) -> str | None:
    """The pre-req key named by *last_reason*, if it is exactly
    `_resolve_prereqs`'s "open, not queued" verdict text (#3944) — else
    ``None``. The one place both :func:`stranded_prereq_rows`
    (classification) and `coord.board_doctor_policy._queue_stranded_prereq`
    (dispatch, re-reading the row's LIVE reason right before acting) parse
    this shape, so the two can never disagree about what counts (#2096).
    """
    if not last_reason:
        return None
    m = _STRANDED_PREREQ_RE.match(last_reason)
    return m.group("dep") if m else None


@dataclass(frozen=True)
class StrandedPrereqGuard:
    """Whether #3944's sweep may enqueue a stranded pre-req, and why not."""

    may_queue: bool
    reason: str = ""


def _dep_last_operator_removed(dep_repo: str, dep_issue: int) -> bool:
    """True when *dep_repo*/*dep_issue*'s most recent drive-queue removal
    was a human's, not the sweep's own (#3944's "removed on purpose" guard).

    Reads the shared `audit_log` `drive_queue_removed` event
    `coord.state._dequeue_drive_queue_local` writes on every successful
    removal, tagged with the `operator` bit the caller passed in. That
    choke point is reached by EVERY removal path regardless of topology —
    a thin client's `coord drive-queue remove` routes through the daemon's
    own `/drive-queue` handler, which calls the exact same local function —
    so, unlike `block_log` (per-host, one-generation-rotated), a removal
    typed on any machine at any time in the past is visible from here.

    A key currently sitting unqueued (the only state this guard is ever
    asked about — see :func:`stranded_prereq_guard`) can have at most one
    "most recent" removal: re-adding it would put a queue row back and take
    this shape out of :func:`stranded_prereq_rows`'s classification
    entirely, so there is never a later addition to reconcile against.

    A key with no removal record at all (never queued, or the removal
    predates this event existing) reads ``False`` — absence of a record is
    not evidence of a human decision.
    """
    from coord.state import list_audit_log  # noqa: PLC0415

    result = list_audit_log(
        category="drive_queue",
        event_type="drive_queue_removed",
        repo=dep_repo,
        issue=dep_issue,
        limit=1,
    )
    entries = result.get("entries") or []
    if not entries:
        return False
    details = entries[0].get("details") or {}
    return bool(details.get("operator"))


def stranded_prereq_guard(dep: str, config: Any) -> StrandedPrereqGuard:
    """Checks #3944's "Must NOT auto-queue" list for *dep*.

    Fails CLOSED: any check this sweep could not actually run — an
    unresolvable repo, a `gh` read that errors — reads as "do not queue",
    never as "assume it's fine". Dispatching a worker onto an epic, or onto
    a release-gate issue an operator deliberately opted out with
    :data:`STRANDED_PREREQ_OPT_OUT_LABEL`, is the one mistake this guard
    exists to prevent, and a probe failure must never relax it.

    Order matters only in that the cheapest/most structural checks (an
    unparseable key, an unconfigured repo) run before anything that needs a
    `gh` round-trip. Shared verbatim by :func:`stranded_prereq_rows`
    (classification) and `coord.board_doctor_policy._queue_stranded_prereq`
    (the dispatcher's own immediate-before-acting re-check) — #2096 again:
    one question, one answer.
    """
    from coord.drive_queue import parse_key  # noqa: PLC0415

    parsed = parse_key(dep)
    if parsed is None:
        return StrandedPrereqGuard(False, f"{dep!r} is not a parseable repo#issue key")
    dep_repo, dep_issue = parsed

    repo_cfg = next(
        (r for r in (getattr(config, "repos", None) or []) if r.name == dep_repo), None,
    )
    if repo_cfg is None:
        return StrandedPrereqGuard(
            False, f"{dep_repo!r} is not a repo in coordinator.yml"
        )

    if _dep_last_operator_removed(dep_repo, dep_issue):
        return StrandedPrereqGuard(
            False,
            f"{dep}'s last drive-queue history is an operator `drive-queue "
            "remove` — removals can be deliberate, escalating instead of "
            "re-adding it",
        )

    from coord import github_ops  # noqa: PLC0415
    from coord.milestone_order import TRACKING_ISSUE_LABEL  # noqa: PLC0415

    try:
        issue_data = github_ops.get_issue(repo_cfg.github or dep_repo, dep_issue)
    except Exception as exc:  # noqa: BLE001 — fail CLOSED, see docstring
        return StrandedPrereqGuard(
            False, f"could not read {dep}'s labels to check epic/opt-out: {exc}"
        )
    # `github_ops.get_issue` fails OPEN to `{}` on an exit-0 `gh` that
    # emitted empty/malformed stdout (see its own docstring) — so a missing
    # `"labels"` key means "could not actually read the labels", never "read
    # them, there are none". Treat the two differently: refuse instead of
    # falling through to the permissive "no labels" branch, or an unreadable
    # dep would default to auto-queueable.
    if not issue_data or "labels" not in issue_data:
        return StrandedPrereqGuard(
            False, f"could not read {dep}'s labels to check epic/opt-out"
        )
    labels = {lbl.get("name", "") for lbl in (issue_data.get("labels") or [])}
    if TRACKING_ISSUE_LABEL in labels:
        return StrandedPrereqGuard(
            False,
            f"{dep} carries the {TRACKING_ISSUE_LABEL!r} label — an `after=` "
            "on an epic is an operator modelling error, not something to "
            "auto-queue",
        )
    if STRANDED_PREREQ_OPT_OUT_LABEL in labels:
        return StrandedPrereqGuard(
            False,
            f"{dep} carries {STRANDED_PREREQ_OPT_OUT_LABEL!r} — explicitly "
            "opted out of auto-queueing (satisfied by a human act, not a "
            "drive)",
        )
    return StrandedPrereqGuard(True)


def stranded_prereq_rows(entries: Sequence[Any], *, config: Any) -> list[StuckRow]:
    """One :class:`StuckRow` per `waiting` row stranded on an open,
    unqueued pre-req (#3944).

    Walks the FULL current *entries* — same posture as :func:`no_progress_
    rows` — because this shape carries no `block_log` episode (only
    `blocked`/`parked` opens one) and no merge-queue row, the two things
    the rest of :func:`collect_rows` already walks: a plain `waiting` row
    whose `after=` names an open-but-unqueued issue would otherwise never
    reach this sweep at all (the gap quadraui#2174/#2178 hit, 960+
    deferrals, #3944). :func:`no_progress_rows`'s own `waiting` shape is
    the deliberate complement of this one — it requires every `after=` dep
    to already read LANDED; this requires one to read exactly the
    "open, not queued" shape instead — so the two never double-classify the
    same row.

    Every match gets :data:`CAUSE_STRANDED_PREREQ` (auto-queueable) or
    :data:`CAUSE_STRANDED_PREREQ_BLOCKED` (alert-only) per
    :func:`stranded_prereq_guard` — decided HERE, once per sweep, so
    `coord.board_doctor_policy._queue_stranded_prereq` never has to
    re-derive which guard tripped, only re-confirm the shape still holds
    immediately before it acts.
    """
    from coord.drive_queue import STATE_WAITING  # noqa: PLC0415

    rows: list[StuckRow] = []
    for entry in entries:
        if entry.state != STATE_WAITING:
            continue
        dep = stranded_prereq_dep(entry.last_reason)
        if dep is None:
            continue
        guard = stranded_prereq_guard(dep, config)
        if guard.may_queue:
            cause = CAUSE_STRANDED_PREREQ
            evidence = (
                f"drive_queue: after={dep} is open on the board but has no "
                "drive-queue row of its own — nothing ever dispatches it, "
                f"so this row's own deferral count ({entry.deferrals}) just "
                f"keeps climbing; last reason: {entry.last_reason!r}",
            )
        else:
            cause = CAUSE_STRANDED_PREREQ_BLOCKED
            evidence = (
                f"drive_queue: after={dep} is open on the board but has no "
                f"drive-queue row of its own, and {guard.reason} — not "
                "auto-queued",
            )
        rows.append(
            StuckRow(
                queue="drive-queue",
                key=entry.key,
                state=entry.state,
                cause=cause,
                confidence=queue_diagnose.CONFIDENCE_HIGH,
                evidence=evidence,
                fresh=True,
            )
        )
    return rows


# ── Next-action-unperformed (#4045) ──────────────────────────────────────────
#
# A row whose LIVE gates already name a dispatchable `next_action`
# (`coord.entry_truth.reconcile_entry`, via `coord.queue_diagnose.
# live_entry_truth`) and nothing has performed it is a stall this doctor can
# name AND fix mechanically, through the normal gated commands every other
# consumer of `next_action` already uses (`coord.review.dispatch_review`,
# `coord.merge_queue.process`, ...). The motivating incident (quadraui#2744,
# 2026-11-19): a fix leg's smoke passed; its review ran and requested
# changes, but the verdict bound to the chain root rather than the fix leg
# itself (claude-coordinator#4029/#4044) — so the fix leg's OWN gates kept
# reading `next_action=dispatch_review` for ~50 minutes with no leg in
# flight and nothing performing it, while this sweep's existing
# `stranded_prereq_rows` walk reported the unrelated, alert-only
# `CAUSE_STRANDED_PREREQ_BLOCKED` instead — the row's `after=` text was
# stale, not its actual blocker.

#: #4045: the one cause :func:`next_action_unperformed_rows` ever mints.
CAUSE_NEXT_ACTION_UNPERFORMED = "next-action-unperformed"

#: How long a row may sit with a live, dispatchable `next_action` and no leg
#: in flight before this sweep acts on it (#4045) — a much tighter floor
#: than :attr:`coord.config.BoardDoctorConfig.no_progress_minutes`'s
#: hour-scale default: `coord.entry_truth.reconcile_entry` is a sharp,
#: direct read of "what should happen next", not the coarser no-progress
#: shapes that threshold also gates.
NEXT_ACTION_UNPERFORMED_STALE_AFTER_SECONDS = 20 * 60.0

#: Live `gh`/gate probes :func:`next_action_unperformed_rows` may spend per
#: sweep — the same cost-containment :data:`_NO_PROGRESS_PROBE_CAP` applies
#: to its own walk, for the identical reason: fanning out one
#: `reconcile_entry` call per aged-out row all at once is the synchronized-
#: poll shape #3536/#3888 already had to guard against elsewhere in this
#: module.
_NEXT_ACTION_UNPERFORMED_PROBE_CAP = 25


def next_action_unperformed_rows(
    entries: Sequence[Any],
    *,
    covered_keys: Collection[str],
    truth_of: Callable[[Any], "EntryTruth | None"],
    board_view: Any = None,
    stale_after_seconds: float = NEXT_ACTION_UNPERFORMED_STALE_AFTER_SECONDS,
    probe_cap: int = _NEXT_ACTION_UNPERFORMED_PROBE_CAP,
    now: float | None = None,
) -> list[StuckRow]:
    """One :class:`StuckRow` per row whose live gates already name a
    dispatchable ``next_action`` that nothing has performed (#4045).

    *truth_of* is the ONE live read this classifier trusts — production
    wiring binds it to `coord.queue_diagnose.live_entry_truth` against the
    real board/config/``gh_ops``; a test can inject a canned
    :class:`~coord.entry_truth.EntryTruth` directly, never paying for a real
    `gh` round trip. Its exceptions are caught here (a probe failure must
    never crash the sweep, the same posture :func:`no_progress_rows` takes
    for its own probe) and a ``None`` return (no probe asked, or one that
    failed) reads as "nothing to say", never "nothing to do".

    Eligible rows: every *entries* row not already in *covered_keys*
    (another pass already explained it) and not in a terminal
    (:data:`coord.drive_queue.STATE_DONE_LIKE`) state. A ``waiting`` row is
    additionally required to have EVERY ``after=`` pre-req already landed —
    the same board-level check :func:`_no_progress_shape`'s own ``waiting``
    branch makes, via *board_view*. This is the one case this cause can
    collide with :data:`CAUSE_STRANDED_PREREQ_BLOCKED`'s own classification
    of the SAME row (a stale, textual "waiting on X (open, not queued)"
    reading that live reality has already moved past) and this cause must
    win it — see :func:`_collect_rows_with_board`'s own ordering, which runs
    this pass strictly before :func:`stranded_prereq_rows`. *board_view*
    being unavailable (the caller's own live-board-view read failed, or
    nothing was supplied) fails CLOSED for a ``waiting`` row specifically —
    never promoted without a confirmed "every pre-req landed" reading —
    while every other state is unaffected by it.

    Age uses the SAME :func:`_no_progress_age_seconds` anchor every other
    no-progress shape in this module reads (``launched_at`` for ``running``,
    ``reason_at`` otherwise) — one "how long has this sat here" clock, never
    a second one this cause invents for itself.

    Only :data:`~coord.entry_truth.NEXT_ACTION_DISPATCH_REVIEW`/
    :data:`~coord.entry_truth.NEXT_ACTION_MERGE`/:data:`~coord.entry_truth.
    NEXT_ACTION_MARK_DONE` are "dispatchable" here.
    :data:`~coord.entry_truth.NEXT_ACTION_DISPATCH_CI_FIX` and
    :data:`~coord.entry_truth.NEXT_ACTION_CONFLICT_FIX` are deliberately
    excluded: each already has its own dedicated detection/dispatch path in
    this module (`queue_diagnose.CAUSE_MERGE_CONFLICT`,
    :data:`CAUSE_CONFLICT_FIX_HEAD_UNMOVED`), and no safe-tier mechanical
    primitive exists yet for an unattended CI-fix dispatch FROM this cause —
    adding one is follow-up work, not a silent extension of this one.
    :data:`~coord.entry_truth.NEXT_ACTION_DISPATCH_TEST` is ALSO excluded,
    for a sharper reason than the other two: `reconcile_entry`'s
    ``latest_worker_status`` check (what makes a leg genuinely in flight
    read as :data:`~coord.entry_truth.NEXT_ACTION_WAIT` instead of
    dispatchable) comes from `coord.gates.gate_columns_for_issue`, which
    is scoped to `coord.models.WORK_LIKE_TYPES` — a `smoke` leg in flight
    is invisible to it. The restage this action would otherwise trigger
    (`coord.board_doctor_permissions.sweep_restage` with ``stage="test"``,
    i.e. `sweep_smoke_requeue`) clears a `test_state="running"` marker
    unconditionally, with none of the liveness check
    `coord.merge_queue._abandoned_running_marker`/
    `RUNNING_MARKER_STALE_AFTER` requires before treating that SAME marker
    as abandoned elsewhere in this codebase — so without this exclusion, a
    row whose smoke leg is still genuinely running would read as stale
    here well before an hour, get its in-flight marker wiped, and get a
    SECOND smoke leg dispatched on top of the first. No safe-tier
    mechanical primitive exists yet that reuses that liveness check FROM
    this cause — adding one is follow-up work, not a silent extension of
    this one.
    :data:`~coord.entry_truth.NEXT_ACTION_ESCALATE`/:data:`~coord.
    entry_truth.NEXT_ACTION_WAIT`/:data:`~coord.entry_truth.
    NEXT_ACTION_UNKNOWN` all mean either "a human already owns this" or "a
    WORK_LIKE_TYPES leg is in flight" (`reconcile_entry`'s own worker-status
    check resolves to ``WAIT`` whenever one of THOSE is, so it is never read
    as dispatchable here) — never this cause's concern. A `review` leg in
    flight is likewise invisible to that check, but the surviving
    ``dispatch_review`` action is still safe against it: the primitive it
    reaches (`coord.review.dispatch_review`/
    `dispatch_scoped_reviews_for_queue`, via `sweep_restage`'s own
    ``stage="review"`` branch) refuses outright when a review is already in
    flight for the same work — the #4002 per-item gate every OTHER review
    dispatch path already goes through, not a check this cause invents.
    """
    from coord.drive_queue import STATE_DONE_LIKE, STATE_WAITING  # noqa: PLC0415
    from coord.entry_truth import (  # noqa: PLC0415
        NEXT_ACTION_DISPATCH_REVIEW,
        NEXT_ACTION_MARK_DONE,
        NEXT_ACTION_MERGE,
    )

    dispatchable = frozenset(
        {
            NEXT_ACTION_DISPATCH_REVIEW,
            NEXT_ACTION_MERGE,
            NEXT_ACTION_MARK_DONE,
        }
    )
    stamp = _utcnow().timestamp() if now is None else now
    dep_states = {e.key: e.state for e in entries}

    def _after_prereqs_landed(entry: Any) -> bool:
        if board_view is None:
            return False
        facts_of = board_view.facts
        for dep in entry.after:
            facts = facts_of(dep)
            if facts.landed or (facts.already_done and not facts.reopened):
                continue
            if dep_states.get(dep) in STATE_DONE_LIKE:
                continue
            return False
        return True

    rows: list[StuckRow] = []
    probes_spent = 0
    for entry in entries:
        if entry.key in covered_keys:
            continue
        if entry.state in STATE_DONE_LIKE:
            continue
        if entry.state == STATE_WAITING and not _after_prereqs_landed(entry):
            continue
        age = _no_progress_age_seconds(entry, now=stamp)
        if age is None or age < stale_after_seconds:
            continue
        if probes_spent >= probe_cap:
            continue
        probes_spent += 1
        try:
            truth = truth_of(entry)
        except Exception:  # noqa: BLE001 — a probe failure must never crash the sweep
            _log.debug(
                "board-doctor: next-action-unperformed probe failed for %s",
                entry.key, exc_info=True,
            )
            continue
        if truth is None or truth.next_action not in dispatchable:
            continue
        rows.append(
            StuckRow(
                queue="drive-queue",
                key=entry.key,
                state=entry.state,
                cause=CAUSE_NEXT_ACTION_UNPERFORMED,
                confidence=queue_diagnose.CONFIDENCE_HIGH,
                evidence=(
                    f"coord.entry_truth: live gates read "
                    f"next_action={truth.next_action!r} "
                    f"({truth.next_action_reason}) for over {age / 60.0:g}m "
                    "with no leg in flight — nothing has performed it",
                ),
                fresh=True,
            )
        )
    return rows


# ── Merged-partial follow-through (#3945) ───────────────────────────────────
#
# The disposition itself — plan-leg prompt/parse/validate, write/record
# orchestration — is :mod:`coord.board_doctor_followthrough` (#3879). What
# was missing: nobody called it. This section is the production wiring —
# a THIRD `StuckRow` source alongside :func:`drive_queue_rows`/
# :func:`merge_queue_rows`, and the impure seams (a real plan leg dispatched
# through the agent, a real GitHub-backed writer) that feed
# :func:`coord.board_doctor_followthrough.run_followthrough`.
#
# Deliberately NOT routed through :func:`sweep`'s own tier/auto_dispatchers/
# convergence loop: that machinery exists to retry a cause across sweeps
# with backoff, which is the wrong shape for "file the follow-ups and close
# the issue, once" — :func:`run_followthrough_sweep` is a separate pass
# :func:`run_sweep` runs alongside it, not a cause registered in
# :mod:`coord.board_doctor_policy`'s allowlist.

#: The one cause :func:`merged_partial_rows` ever mints — mirrors
#: :data:`coord.drive_queue.STATE_MERGED_PARTIAL` (the drive-queue STATE,
#: not a `queue_diagnose` cause: no block_log episode or merge-queue entry
#: is involved, so there is nothing for that classifier to diagnose).
CAUSE_MERGED_PARTIAL = "merged-partial"

#: The one cause :func:`branch_merged_guard_rows` ever mints (#4038) — an
#: OPEN drive-queue row the #3376 structural dispatch-liveness gate refused
#: permanently because one of the issue's own ``issue-{N}-*`` branches
#: already merged (`coord.dispatch_liveness.
#: is_branch_merged_guard_refusal_reason`). Distinct from
#: :data:`CAUSE_MERGED_PARTIAL`: that row's worker actually ran and
#: declared ``ISSUE_RESOLUTION: partial``; THIS row's worker never ran at
#: all — the guard refused the dispatch before any assignment existed —
#: so there is no PR, review, or diff to build a plan leg prompt from,
#: only the issue's own body.
CAUSE_BRANCH_MERGED_GUARD = "branch-merged-guard"

#: :func:`run_refile_sweep`'s own action (#4061) for a ``branch-merged-
#: guard`` row whose LIVE recheck (`coord.claim.any_matching_branch_merged`
#: — the same predicate the #3376 guard itself consults, re-asked rather
#: than re-derived, #2096) no longer reports the branch merged. The stored
#: ``last_reason`` that minted :data:`CAUSE_BRANCH_MERGED_GUARD` can be
#: stale: it only proves the guard refused dispatch THEN, off whatever
#: "merged" meant at the time — and before #4061, bare `ahead_by == 0`
#: ancestry (a branch cut from the base's tip and never advanced — a
#: premise refusal, a `needs-decomposition` exit, an advisory audit) read
#: identically to a real landed branch. Nothing had actually landed for
#: this row, so refiling a replacement issue would be wrong (there is
#: nothing left on the original to split off) — this requeues the
#: ORIGINAL issue instead (quadraui#2539).
ACTION_BRANCH_MERGED_GUARD_FALSE_POSITIVE_REQUEUED = (
    "branch-merged-guard-false-positive-requeued"
)

#: Sibling of the above for when the live recheck says "not merged" but the
#: mechanical requeue itself did not land (a transient `gh`/DB failure, a
#: gate mid-flight) — the row is left exactly where it was, for the next
#: sweep or an operator to retry, rather than silently reporting success.
ACTION_BRANCH_MERGED_GUARD_FALSE_POSITIVE_REQUEUE_FAILED = (
    "branch-merged-guard-false-positive-requeue-failed"
)

#: How long :func:`dispatch_plan_leg` waits for the follow-through's
#: read-only plan leg to finish before giving up. A `claude -p` session
#: grepping one repo, not a build — generous but still bounded, so a hung
#: leg frees this sweep instead of blocking it indefinitely.
PLAN_LEG_TIMEOUT_SECONDS = 20 * 60.0
PLAN_LEG_POLL_INTERVAL_SECONDS = 15.0

#: `/status` values :func:`dispatch_plan_leg` treats as "the leg is done
#: trying" — the SAME terminal set as every other assignment
#: (:data:`coord.dao.TERMINAL_STATUSES`), never a second, divergent copy
#: (that contract applies to any status a plan leg can reach too, so
#: minting a plan-leg-only subset risks missing a real one, e.g. an
#: operator `coord stop` landing `"cancelled"` or a `"needs_decomposition"`
#: verdict — both of which this bound polling loop must still recognise
#: as "stop waiting").
_PLAN_LEG_TERMINAL_STATUSES = _ASSIGNMENT_TERMINAL_STATUSES


def merged_partial_rows(entries: Sequence[Any]) -> list[StuckRow]:
    """One :class:`StuckRow` per drive-queue row in
    :data:`coord.drive_queue.STATE_MERGED_PARTIAL` (#3945) — the feed
    :func:`run_followthrough_sweep` resolves.

    Pure, like every other row source in this module: whether the issue is
    STILL open (a human, or an earlier follow-through pass, may already
    have closed it) is checked live, right before a plan leg would be
    dispatched (:func:`run_followthrough_sweep`), not here — mirrors
    :func:`merge_queue_rows`'s own split between "which rows" (cheap,
    already-fetched evidence) and "what to do about them" (impure).
    """
    from coord.drive_queue import STATE_MERGED_PARTIAL  # noqa: PLC0415

    rows: list[StuckRow] = []
    for entry in entries:
        if entry.state != STATE_MERGED_PARTIAL:
            continue
        evidence = (f"drive_queue.state={entry.state}",)
        if entry.last_reason:
            evidence += (f"last_reason={entry.last_reason}",)
        rows.append(
            StuckRow(
                queue="drive-queue",
                key=entry.key,
                state=entry.state,
                cause=CAUSE_MERGED_PARTIAL,
                confidence=queue_diagnose.CONFIDENCE_HIGH,
                evidence=evidence,
                fresh=True,
            )
        )
    return rows


def branch_merged_guard_rows(entries: Sequence[Any]) -> list[StuckRow]:
    """One :class:`StuckRow` per `blocked` drive-queue row the #3376 guard
    refused permanently because its own branch already merged (#4038) —
    the feed :func:`run_refile_sweep` resolves.

    Pure, like :func:`merged_partial_rows`: whether the issue is STILL open
    is checked live, right before anything is dispatched
    (:func:`run_refile_sweep`), not here. Marker-based
    (`coord.dispatch_liveness.is_branch_merged_guard_refusal_reason`), the
    SAME recognition :func:`coord.drive_queue._reconcile_running` already
    uses to route this one `(#1844)`-shaped refusal to `blocked` rather
    than treat it as a transient death — one question ("is this row stuck
    on the #3376 branch-merged guard specifically"), one answer (#2096).
    """
    from coord.dispatch_liveness import (  # noqa: PLC0415
        is_branch_merged_guard_refusal_reason,
    )
    from coord.drive_queue import STATE_BLOCKED  # noqa: PLC0415

    rows: list[StuckRow] = []
    for entry in entries:
        if entry.state != STATE_BLOCKED:
            continue
        if not is_branch_merged_guard_refusal_reason(entry.last_reason):
            continue
        evidence = (f"drive_queue.state={entry.state}",)
        if entry.last_reason:
            evidence += (f"last_reason={entry.last_reason}",)
        rows.append(
            StuckRow(
                queue="drive-queue",
                key=entry.key,
                state=entry.state,
                cause=CAUSE_BRANCH_MERGED_GUARD,
                confidence=queue_diagnose.CONFIDENCE_HIGH,
                evidence=evidence,
                fresh=True,
            )
        )
    return rows


def _followthrough_notify(
    repo_name: str, issue_number: int, stage: str, subject: str, body: str,
) -> None:
    """Real alert/page sink for :func:`coord.board_doctor_followthrough.
    run_followthrough` (#3945): routes through
    :func:`coord.board_doctor_policy.write_drive_escalation`, the SAME
    confirmed, skip-aware write every other board-doctor alert/page sink
    uses — one question ("does this issue need a human"), one answer,
    rather than a second notification channel (and a second, divergent
    write path) just for follow-through.
    """
    import click  # noqa: PLC0415

    from coord.board_doctor_policy import write_drive_escalation  # noqa: PLC0415

    outcome = write_drive_escalation(
        repo_name, issue_number, stage=stage, reason=subject,
        gate_readings=body, proposed_command="coord drive-queue sweep",
    )
    if outcome is ALERT_SKIPPED or not outcome:
        click.echo(
            f"warning: board-doctor follow-through alert for "
            f"{repo_name}#{issue_number} was not recorded "
            f"({'a more specific escalation covers this issue' if outcome is ALERT_SKIPPED else 'write unconfirmed'})",
            err=True,
        )


def _github_slug(repo_name: str, config: Any) -> str:
    """The GitHub ``owner/repo`` slug for *repo_name* — never the local
    `coordinator.yml` repo name itself. `coord.github_ops` shells
    `gh ... --repo <slug>` / builds `repos/{slug}/...` REST paths; the two
    names differ for the normal case in this fleet (``claude-coordinator``
    -> ``JDonaghy/code-coordinator``). Mirrors
    :func:`stranded_prereq_guard`'s own ``repo_cfg.github or dep_repo``
    resolution, falling back to *repo_name* only when the repo is not in
    config at all (should not happen in production — ``github:`` is
    required at parse time — but keeps this usable against a minimal test
    double)."""
    repo_cfg = config.repo(repo_name) if hasattr(config, "repo") else None
    return (getattr(repo_cfg, "github", None) or repo_name) if repo_cfg is not None else repo_name


def build_partial_merge(
    repo_name: str, issue_number: int, *, config: Any, board: Board,
) -> board_doctor_followthrough.PartialMerge:
    """Real :class:`~coord.board_doctor_followthrough.PartialMerge` builder
    for a live ``merged-partial`` row (#3945).

    Best-effort on everything the plan leg's prompt treats as optional
    context (epic body, CLAUDE.md) — the plan leg itself has Read/Bash on
    the actual checkout and re-derives the truth from the live code
    regardless; this only has to get it ENOUGH context to know what to
    look for. ``resolution_text`` comes off the board — the originating
    ``work`` assignment's own ``completion_summary`` (the #874 durable
    copy of the worker's final message, the same column
    :mod:`coord.drive_queue`'s own ``merge_resolution`` reads) — never a
    second `gh` round trip for text already on disk. The most recently
    completed matching assignment wins, in case the issue went through
    more than one work leg.

    ``review_findings``/``merged_diff``/``pr_number`` are deliberately
    left unset: resolving the merging PR (and, if approved, its review
    findings) needs the merge-queue's own heavier machinery
    (:func:`coord.merge_queue._issue_resolution_for_entry`'s scan), which
    this production entry point does not have cheaply in hand. The plan
    leg's own Read/Bash access to the live checkout makes this
    non-load-bearing — the prompt already tells it to verify everything
    against the code, not trust supplied context — so the degradation is
    "a slightly thinner prompt", not "an unreliable plan".
    """
    from coord import github_ops  # noqa: PLC0415

    github_repo = _github_slug(repo_name, config)

    issue = github_ops.get_issue(github_repo, issue_number)
    milestone = issue.get("milestone") or {}
    parent = github_ops.get_issue_parent(github_repo, issue_number)
    epic_number = parent.get("number") if parent else None
    epic_body = ""
    if epic_number is not None:
        try:
            epic_body = github_ops.get_issue(github_repo, epic_number).get("body") or ""
        except Exception:  # noqa: BLE001 — epic body is advisory context only
            epic_body = ""

    resolution_text = ""
    for a in reversed(list(board.completed)):
        if (
            a.repo_name == repo_name and a.issue_number == issue_number
            and a.type == "work" and a.completion_summary
        ):
            resolution_text = a.completion_summary
            break

    claude_md = ""
    try:
        claude_md = github_ops.get_repo_file(github_repo, "CLAUDE.md")
    except Exception:  # noqa: BLE001 — advisory context only
        claude_md = ""

    return board_doctor_followthrough.PartialMerge(
        repo_name=repo_name,
        github_repo=github_repo,
        issue_number=issue_number,
        title=issue.get("title") or "",
        body=issue.get("body") or "",
        resolution_text=resolution_text,
        epic_number=epic_number,
        epic_body=epic_body,
        milestone=milestone.get("title"),
        claude_md=claude_md,
    )


def build_refile_candidate(
    repo_name: str, issue_number: int, *, config: Any, entry: Any,
) -> board_doctor_followthrough.RefileCandidate:
    """Real (best-effort) :class:`~coord.board_doctor_followthrough.
    RefileCandidate` builder for a live `branch-merged-guard` row (#4038).

    *entry* is the row's own :class:`~coord.drive_queue.QueueEntry` — the
    snapshot :func:`~coord.board_doctor_followthrough._requeue_refile`
    needs to move the drive-queue row onto the new issue number unchanged
    (same position, same ``after=`` edges, same machine pin/hold gate).
    Unlike :func:`build_partial_merge`, there is no completed `work`
    assignment to pull an ``ISSUE_RESOLUTION`` from — the #3376 guard
    refused the dispatch before one ever ran — so only the issue's own
    body feeds the plan leg.
    """
    from coord import github_ops  # noqa: PLC0415

    github_repo = _github_slug(repo_name, config)

    issue = github_ops.get_issue(github_repo, issue_number)
    milestone = issue.get("milestone") or {}
    labels = tuple(
        name for label in (issue.get("labels") or [])
        if (name := label.get("name"))
    )
    parent = github_ops.get_issue_parent(github_repo, issue_number)
    epic_number = parent.get("number") if parent else None

    return board_doctor_followthrough.RefileCandidate(
        repo_name=repo_name,
        github_repo=github_repo,
        issue_number=issue_number,
        title=issue.get("title") or "",
        body=issue.get("body") or "",
        labels=labels,
        milestone=milestone.get("title"),
        epic_number=epic_number,
        queue_position=int(getattr(entry, "position", 0) or 0),
        queue_after=tuple(getattr(entry, "after", ()) or ()),
        queue_machine=str(getattr(entry, "machine", "") or ""),
        queue_hold_after=bool(getattr(entry, "hold_after", False)),
        queue_hold_reason=str(getattr(entry, "hold_reason", "") or ""),
        queue_resume_when=str(getattr(entry, "resume_when", "") or ""),
        queue_hold_scope=str(getattr(entry, "hold_scope", "entry") or "entry"),
        queue_max_fix_rounds=getattr(entry, "max_fix_rounds", None),
        queue_no_acceptance=bool(getattr(entry, "no_acceptance", False)),
        queue_plan_destructive=bool(getattr(entry, "plan_destructive", False)),
    )


def build_auto_split_candidate(
    repo_name: str, issue_number: int, *, config: Any, entry: Any,
    findings: Sequence[str],
) -> board_doctor_followthrough.AutoSplitCandidate:
    """Real :class:`~coord.board_doctor_followthrough.AutoSplitCandidate`
    builder for a live ceiling-blocked row (#4034) — mirrors
    :func:`build_refile_candidate` exactly, except *findings* (the latest
    review's already-classified missing-scope blocking findings — see
    :func:`latest_review_blocking_findings`) is supplied by the caller
    rather than read here, since fetching and classifying is a separate,
    independently testable step.
    """
    from coord import github_ops  # noqa: PLC0415

    github_repo = _github_slug(repo_name, config)

    issue = github_ops.get_issue(github_repo, issue_number)
    milestone = issue.get("milestone") or {}
    labels = tuple(
        name for label in (issue.get("labels") or [])
        if (name := label.get("name"))
    )
    parent = github_ops.get_issue_parent(github_repo, issue_number)
    epic_number = parent.get("number") if parent else None

    return board_doctor_followthrough.AutoSplitCandidate(
        repo_name=repo_name,
        github_repo=github_repo,
        issue_number=issue_number,
        title=issue.get("title") or "",
        body=issue.get("body") or "",
        findings=tuple(findings),
        labels=labels,
        milestone=milestone.get("title"),
        epic_number=epic_number,
        queue_position=int(getattr(entry, "position", 0) or 0),
        queue_after=tuple(getattr(entry, "after", ()) or ()),
        queue_machine=str(getattr(entry, "machine", "") or ""),
        queue_hold_after=bool(getattr(entry, "hold_after", False)),
        queue_hold_reason=str(getattr(entry, "hold_reason", "") or ""),
        queue_resume_when=str(getattr(entry, "resume_when", "") or ""),
        queue_hold_scope=str(getattr(entry, "hold_scope", "entry") or "entry"),
        queue_max_fix_rounds=getattr(entry, "max_fix_rounds", None),
        queue_no_acceptance=bool(getattr(entry, "no_acceptance", False)),
        queue_plan_destructive=bool(getattr(entry, "plan_destructive", False)),
    )


def latest_review_blocking_findings(
    repo_name: str, issue_number: int, *, board: Board, config: Any,
) -> list[str]:
    """The live, remaining blocking findings off the ISSUE's most recently
    dispatched review assignment, split one-per-bullet (#4034 point 1's own
    data source).

    Walks :func:`coord.diagnose.stage_assignments` for ``stage="review"``
    (the same newest-first list the TUI's own `assignments_for_stage`
    mirrors) to find the latest review's ``assignment_id``, recovers its
    findings off the GitHub message bus
    (:func:`coord.review.fetch_review_findings_from_github` — interactive
    reviews never land a parseable local log, so GitHub is the one store
    every machine already reaches), extracts the "## Blocking findings"
    section (:func:`coord.review.extract_blocking_section`) and splits it
    (:func:`coord.board_doctor_followthrough.split_blocking_findings`).

    Returns ``[]`` on any failure to resolve a review, an assignment id, or
    a findings body — the fail-safe "nothing to classify" answer
    :func:`coord.board_doctor_followthrough.classify_split_findings` already
    reads as ineligible, never a crash.
    """
    from coord.diagnose import stage_assignments  # noqa: PLC0415
    from coord.review import extract_blocking_section, fetch_review_findings_from_github  # noqa: PLC0415

    reviews = stage_assignments(board, repo_name, issue_number, "review")
    if not reviews:
        return []
    latest = reviews[0]
    if not latest.assignment_id:
        return []
    github_repo = _github_slug(repo_name, config)
    try:
        findings = fetch_review_findings_from_github(
            github_repo, issue_number, latest.assignment_id,
        )
    except Exception:  # noqa: BLE001 — a flaky/unreachable fetch reads as "nothing to classify"
        return []
    if findings is None:
        return []
    blocking = extract_blocking_section(findings.body)
    if not blocking:
        return []
    return board_doctor_followthrough.split_blocking_findings(blocking)


def run_auto_split_for_row(
    repo_name: str, issue_number: int, *, config: Any, board: Board,
    dry_run: bool = False,
    findings_fetcher: Callable[..., list[str]] = latest_review_blocking_findings,
    writer: board_doctor_followthrough.FollowThroughWriter | None = None,
    reset_review: Callable[[str, int], None] | None = None,
) -> board_doctor_followthrough.Disposition | None:
    """Production entry point :func:`coord.board_doctor_policy._investigate`
    calls, instead of its own :func:`_alert_fallback`, for a row that has
    given up on the #2972 fix-round ceiling (#4034).

    Returns ``None`` — never a disposition — for every reason the caller
    must fall back to its own alert path: the repo hasn't opted in
    (:func:`~coord.board_doctor_followthrough.auto_split_enabled`), there is
    no live drive-queue row for ``(repo_name, issue_number)`` any more, or
    the latest review's remaining blocking findings don't classify as
    missing-scope-only (:func:`~coord.board_doctor_followthrough.
    classify_split_findings`). A real :class:`~coord.board_doctor_
    followthrough.Disposition` is returned only once a split has actually
    been attempted — the caller still must check ``disposition.action``
    before treating this as handled (#2096: returning a disposition at all
    is not the same as confirming it filed).
    """
    if not board_doctor_followthrough.auto_split_enabled(repo_name, config):
        return None

    from coord.drive_queue import QueueEntry  # noqa: PLC0415
    from coord.state import get_drive_queue_entry  # noqa: PLC0415

    row = get_drive_queue_entry(repo_name, issue_number)
    if row is None:
        return None
    entry = QueueEntry.from_row(row)

    findings = findings_fetcher(repo_name, issue_number, board=board, config=config)
    classification = board_doctor_followthrough.classify_split_findings(findings)
    if not classification.eligible:
        return None

    candidate = build_auto_split_candidate(
        repo_name, issue_number, config=config, entry=entry,
        findings=classification.findings,
    )
    row_writer = writer or board_doctor_followthrough.GitHubFollowThroughWriter()

    def _default_reset_review(_repo: str, _issue: int) -> None:
        from coord.diagnose import diagnose_stage  # noqa: PLC0415

        res = diagnose_stage(board, config, _repo, _issue, "review", reset=True, dry_run=dry_run)
        # #2096: `diagnose_stage(reset=True)` has non-raising failure paths
        # (`_reset_review_stage` can neither stop the live session nor
        # confirm the row delete persisted) that leave the review stage
        # untouched while returning normally — the mere absence of an
        # exception is not proof the reset happened. Derive the verdict
        # from the result instead of the call succeeding, and raise so
        # `run_auto_split`'s own try/except records
        # `ACTION_AUTO_SPLIT_WRITE_FAILED` rather than requeuing the
        # original with its `request-changes` review still in place.
        if not dry_run and (res.needs_reset or not res.recovered):
            raise RuntimeError(
                f"review-stage reset for {_repo}#{_issue} did not complete: "
                + ("; ".join(res.findings) or "reset reported not recovered")
            )

    return board_doctor_followthrough.run_auto_split(
        candidate,
        writer=row_writer,
        alert=lambda subject, body, _r=repo_name, _n=issue_number: _followthrough_notify(
            _r, _n, "board-doctor:auto-split-alert", subject, body,
        ),
        dry_run=dry_run,
        reset_review=reset_review or _default_reset_review,
    )


def dispatch_plan_leg(
    prompt: str,
    *,
    partial: board_doctor_followthrough.PartialMerge
    | board_doctor_followthrough.RefileCandidate,
    config: Any,
    status_fetcher: Callable[..., Any] | None = None,
    text_fetcher: Callable[[str, str], str | None] | None = None,
    sleep: Callable[[float], None] = _time.sleep,
    now: Callable[[], float] = _time.time,
    timeout_seconds: float = PLAN_LEG_TIMEOUT_SECONDS,
    poll_interval_seconds: float = PLAN_LEG_POLL_INTERVAL_SECONDS,
) -> board_doctor_followthrough.PlanLegResult:
    """Real ``plan_leg`` seam for :func:`coord.board_doctor_followthrough.
    run_followthrough` (#3945) AND :func:`coord.board_doctor_followthrough.
    run_refile` (#4038) — both pass only the four attributes this function
    actually reads (``repo_name``/``issue_number``/``title``/
    ``github_slug``), which :class:`~coord.board_doctor_followthrough.
    PartialMerge` and :class:`~coord.board_doctor_followthrough.
    RefileCandidate` both carry, so one dispatcher serves both rather than
    two near-duplicates that could silently drift (#2096).

    Dispatches an ordinary ``type="plan"`` headless leg through the agent
    (:func:`coord.dispatch.dispatch`, never an in-process call) with a
    custom system prompt carrying the follow-through's own JSON-plan
    contract (``Proposal.system_prompt`` — the worker never sees
    ``type="plan"``'s default ``WORKER_PLAN_PROMPT``/FILES_READ/.../
    ESTIMATE schema once this is set), then blocks polling ``/status``
    until the assignment reaches a terminal state or *timeout_seconds*
    elapses, and returns its raw transcript
    (:func:`coord.plan_parser.fetch_raw_text_from_agent` — the extraction
    half shared with the generic plan-worker parser, regardless of which
    schema the transcript actually follows).

    Routes machine selection through :func:`coord.dispatch.select_fix_machine`
    (the SAME "which machine" answer a same-branch fix dispatch gets,
    #2096) rather than a second, independently-drifting picker — this is a
    fresh dispatch, not a same-branch fix, so there is no "original
    machine" to prefer; the first configured machine is named as that
    (inert) preference and every capable, reachable one is an equally good
    candidate. Dispatches through :func:`coord.dispatch.dispatch_with_retry`
    (``config.concurrency.max_retries``/``backoff_base``) rather than a bare
    ``dispatch()``, same as every other non-CLI dispatcher in this codebase
    — a transient POST failure must not abandon an otherwise-eligible row.
    Records the dispatched assignment (:func:`coord.state.
    record_dispatched_assignment`) immediately after, so this leg is
    visible to `coord status`/cost attribution/"is this host busy" for the
    up-to-20-minutes it occupies a slot, exactly like
    :func:`coord.milestone_chat`'s own ``dispatch* -> record_dispatched_
    assignment`` pattern.

    Raises :class:`RuntimeError` when no machine is reachable/capable, the
    dispatch itself fails, or the leg never reaches a terminal state before
    the timeout — :func:`run_followthrough` has no recovery path for a plan
    leg that produced no output, so surfacing the failure loudly (rather
    than returning an empty result :func:`parse_plan` would merely read as
    "unreadable") is deliberate.
    """
    from coord.dispatch import dispatch_with_retry, select_fix_machine  # noqa: PLC0415
    from coord.models import Assignment, Proposal  # noqa: PLC0415
    from coord.network import fetch_status  # noqa: PLC0415
    from coord.plan_parser import fetch_raw_text_from_agent  # noqa: PLC0415
    from coord.state import record_dispatched_assignment  # noqa: PLC0415

    fetch = status_fetcher or fetch_status
    fetch_text = text_fetcher or fetch_raw_text_from_agent

    preferred = config.machines[0].name if config.machines else ""
    selection = select_fix_machine(
        original_machine_name=preferred,
        repo_name=partial.repo_name,
        machines=list(config.machines),
        status_fetcher=fetch,
    )
    if selection.machine is None:
        raise RuntimeError(
            f"no machine reachable to run the follow-through plan leg for "
            f"{partial.repo_name}#{partial.issue_number}: {selection.tried}"
        )

    proposal = Proposal(
        id=0,
        machine_name=selection.machine.name,
        repo_name=partial.repo_name,
        issue_number=partial.issue_number,
        issue_title=partial.title,
        rationale="board-doctor follow-through/refile plan leg",
        briefing="Begin.",
        type="plan",
        system_prompt=prompt,
    )
    result = dispatch_with_retry(
        proposal, config,
        max_retries=config.concurrency.max_retries,
        backoff_base=config.concurrency.backoff_base,
        status_fetcher=fetch,
    )
    assignment_id = str(result.get("id") or result.get("assignment_id") or "")
    if not assignment_id:
        raise RuntimeError(
            f"follow-through plan leg dispatch for "
            f"{partial.repo_name}#{partial.issue_number} returned no "
            f"assignment id: {result!r}"
        )
    record_dispatched_assignment(
        assignment=Assignment(
            machine_name=selection.machine.name,
            repo_name=partial.repo_name,
            issue_number=partial.issue_number,
            issue_title=partial.title,
            files_allowed=[],
            files_forbidden=[],
            briefing="Begin.",
            assignment_id=assignment_id,
            status="running",
            dispatched_at=now(),
            type="plan",
        ),
        repo_github=partial.github_slug,
    )

    deadline = now() + timeout_seconds
    status: str | None = None
    while now() < deadline:
        probe = fetch(selection.machine)
        if probe.ok:
            data = probe.data or {}
            candidates = list(data.get("active") or []) + list(data.get("completed") or [])
            match = next(
                (d for d in candidates if str(d.get("id")) == assignment_id), None,
            )
            if match is not None:
                status = match.get("status")
                if status in _PLAN_LEG_TERMINAL_STATUSES:
                    break
        sleep(poll_interval_seconds)

    if status not in _PLAN_LEG_TERMINAL_STATUSES:
        raise RuntimeError(
            f"follow-through plan leg {assignment_id} for "
            f"{partial.repo_name}#{partial.issue_number} did not reach a "
            f"terminal state within {timeout_seconds:.0f}s "
            f"(last observed status: {status!r})"
        )

    output = fetch_text(selection.machine.host, assignment_id)
    return board_doctor_followthrough.PlanLegResult(
        assignment_id=assignment_id, output=output or "",
    )


def _issue_exists(repo_name: str, issue_number: int) -> bool:
    from coord import github_ops  # noqa: PLC0415

    try:
        issue = github_ops.get_issue(repo_name, issue_number)
    except Exception:  # noqa: BLE001 — unreachable/unknown reads as "doesn't exist"
        return False
    return bool(issue.get("number"))


def _issue_open(repo_name: str, issue_number: int) -> bool:
    """Whether *issue_number* is readably OPEN right now — fail CLOSED.

    :func:`coord.github_ops.get_issue_state` returns ``None`` for "could
    not tell" rather than folding that into either real state (unlike its
    own ``issue_is_closed`` wrapper, which is deliberately fail-OPEN for
    its dispatch-gating callers). A follow-through row that cannot be
    confirmed open must not proceed to file follow-ups and close an issue
    whose state was never actually observed, so this treats "couldn't
    tell" the same as "not open" — the permissive branch here is exactly
    the one the "a gate must be able to fail" rule forbids.
    """
    from coord import github_ops  # noqa: PLC0415

    return github_ops.get_issue_state(repo_name, issue_number) == "open"


def _milestone_exists(repo_name: str, title: str) -> bool:
    from coord import github_ops  # noqa: PLC0415

    try:
        milestones = github_ops.get_repo_milestones(repo_name, state="all")
    except Exception:  # noqa: BLE001 — unreachable reads as "doesn't exist"
        return False
    return any(m.get("title") == title for m in milestones)


def _covering_prs_by_issue(github_repo: str) -> dict[int, int]:
    """Every issue number ANY open PR in *github_repo* already carries a
    GitHub closing keyword for, mapped to that PR's number — one scan of
    every open PR's body, not one scan per issue:
    `github_ops.list_open_prs` returns only `number`/`title`/`headRefName`,
    so answering even a single issue needs one `get_pr_body` subprocess
    per open PR; without batching this per repo, a sweep with several
    eligible rows in the same repo repeats that whole scan from scratch
    for each one.

    Reuses :func:`coord.pr_body_lint.find_closing_references` — the same
    closing-keyword scan `coord.merge_queue.process` runs pre-merge — so
    "does this PR close that issue" has one answer, not a second regex.
    Fail-open on any `gh` read failure: an unreachable PR body must not
    itself block this sweep from otherwise acting on the row.
    """
    from coord import github_ops  # noqa: PLC0415
    from coord.pr_body_lint import find_closing_references  # noqa: PLC0415

    index: dict[int, int] = {}
    try:
        prs = github_ops.list_open_prs(github_repo)
    except Exception:  # noqa: BLE001 — unreachable reads as "no covering PR"
        return index
    for pr in prs:
        number = pr.get("number")
        if number is None:
            continue
        try:
            body = github_ops.get_pr_body(github_repo, int(number))
        except Exception:  # noqa: BLE001 — one bad PR body must not sink the sweep
            continue
        for closed_issue in find_closing_references(body):
            index.setdefault(closed_issue, int(number))
    return index


def _covering_open_pr(github_repo: str, issue_number: int) -> int | None:
    """An open PR in *github_repo* whose body already carries a GitHub
    closing keyword for *issue_number* — remediation for the parent is
    already in flight, so :func:`run_followthrough_sweep` must file
    nothing for it. A single-issue convenience over
    :func:`_covering_prs_by_issue`; :func:`_default_covering_pr_check`
    is what :func:`run_followthrough_sweep` actually uses by default, so
    that one scan is shared across every row in the same sweep that
    shares a `github_repo`, rather than repeated per row the way a direct
    call to this function would.
    """
    return _covering_prs_by_issue(github_repo).get(issue_number)


def _default_covering_pr_check() -> Callable[[str, int], int | None]:
    """Default *covering_pr_check* for :func:`run_followthrough_sweep`: a
    fresh per-sweep-call cache of :func:`_covering_prs_by_issue`, keyed by
    `github_repo`, so a repo with several eligible rows pays for that scan
    once rather than once per row.
    """
    cache: dict[str, dict[int, int]] = {}

    def _check(github_repo: str, issue_number: int) -> int | None:
        if github_repo not in cache:
            cache[github_repo] = _covering_prs_by_issue(github_repo)
        return cache[github_repo].get(issue_number)

    return _check


def _load_pending_followthrough(
    repo_name: str, issue_number: int,
) -> board_doctor_followthrough.PendingFollowThrough | None:
    """The most recent unfinished follow-through attempt recorded for
    *repo_name*#*issue_number*, if any.

    Reads the durable decision log (:func:`coord.state.
    board_doctor_decisions`) for every disposition recorded against this
    key and looks only at the LATEST one overall — not the latest filtered
    to :data:`~coord.board_doctor_followthrough.ACTION_WRITE_FAILED`. A
    resumed attempt that is re-validated and rejected (or any other
    disposition) records a newer, different-action entry for the same
    key; filtering by action first would skip straight past that and keep
    resurfacing the OLD write-failed entry's frozen plan forever, with no
    way for a later sweep to ever notice the resume path gave up on it.
    Only when the latest entry IS itself a write failure is its resumable
    plan/follow-up-number state — :func:`~coord.board_doctor_followthrough.
    Disposition.as_record` folded it into ``evidence`` (see
    :data:`~coord.board_doctor_followthrough.PENDING_FOLLOWTHROUGH_EVIDENCE_PREFIX`'s
    docstring for why there, rather than a new column) — decoded and
    returned. ``None`` otherwise: no recorded decision at all, the latest
    one is not a write failure, or its evidence carries no resumable
    payload (an older write-failed row recorded before this existed).
    """
    from coord.state import board_doctor_decisions  # noqa: PLC0415

    decisions = board_doctor_decisions(repo=repo_name, issue=issue_number)
    if not decisions:
        return None
    latest = decisions[-1]
    if latest.get("action") != board_doctor_followthrough.ACTION_WRITE_FAILED:
        return None
    prefix = board_doctor_followthrough.PENDING_FOLLOWTHROUGH_EVIDENCE_PREFIX
    for item in reversed(latest.get("evidence") or []):
        if not (isinstance(item, str) and item.startswith(prefix)):
            continue
        try:
            payload = json.loads(item[len(prefix):])
            plan = board_doctor_followthrough.plan_from_jsonable(payload["plan"])
            follow_up_keys = {
                str(k): int(v) for k, v in (payload.get("follow_up_keys") or {}).items()
            }
        except Exception:  # noqa: BLE001 — a corrupt payload reads as "nothing to resume"
            return None
        return board_doctor_followthrough.PendingFollowThrough(
            plan=plan, follow_up_keys=follow_up_keys,
        )
    return None


#: Default ceiling on how many eligible ``branch-merged-guard`` rows
#: :func:`run_refile_sweep` will actually dispatch a plan leg for in ONE
#: call — same rationale as :data:`DEFAULT_FOLLOWTHROUGH_ROWS_PER_SWEEP`.
DEFAULT_REFILE_ROWS_PER_SWEEP = 3


def run_refile_sweep(
    rows: Sequence[StuckRow],
    *,
    config: Any,
    board: Board | None = None,
    dry_run: bool = False,
    plan_leg_dispatcher: Callable[..., board_doctor_followthrough.PlanLegResult] = (
        dispatch_plan_leg
    ),
    writer: board_doctor_followthrough.FollowThroughWriter | None = None,
    issue_open: Callable[[str, int], bool] | None = None,
    max_rows: int = DEFAULT_REFILE_ROWS_PER_SWEEP,
) -> list[board_doctor_followthrough.Disposition]:
    """Resolve every eligible ``branch-merged-guard`` row in *rows*, up to
    *max_rows* of them (#4038). Mirrors :func:`run_followthrough_sweep`'s
    own shape — see its docstring for the parts duplicated verbatim here
    (*board*'s lazy build, *writer*'s real default, the open-check
    fail-closed-by-skip posture).

    #4061: before filing anything, re-checks LIVE whether the issue's
    branch is still actually merged (`coord.claim.
    any_matching_branch_merged`, not a second derivation of the same
    question). A row's `branch-merged-guard` cause only proves the #3376
    guard refused dispatch for this reason at SOME point — not that the
    branch is still read as merged now, and not that it ever genuinely was
    (a zero-commit leg's own residue branch used to read identically to a
    landed one, #4061). When the live recheck says "not merged", there is
    no remainder to refile — the row is requeued instead
    (:data:`ACTION_BRANCH_MERGED_GUARD_FALSE_POSITIVE_REQUEUED`/
    :data:`ACTION_BRANCH_MERGED_GUARD_FALSE_POSITIVE_REQUEUE_FAILED`),
    never counted toward *already_refiled*, and `run_refile` is never
    called for it.

    A row whose key no longer resolves to a real
    :class:`~coord.drive_queue.QueueEntry` is skipped rather than acted
    on: :func:`~coord.board_doctor_followthrough.run_refile`'s own write
    path dequeues the original row the moment it successfully files a
    replacement, so a row that is STILL in *rows* (collected earlier this
    same sweep) but already gone from a FRESH queue read means a previous
    iteration of this very loop — or a concurrent writer — already
    resolved it; retrying would refile it a second time.

    A row can also reach *rows* more than once in the SAME call (e.g. a
    `block_log` episode's own `premise-refusal` history reclassified to
    `branch-merged-guard` alongside the belt-and-braces orphan scan both
    surfacing the same key) — the `entry is None` guard above does not
    catch that, since *entries_by_key* is one fresh snapshot read ONCE
    before this loop, not re-read per row, so a SECOND row for the same
    key still finds the stale, not-yet-dequeued entry. *already_refiled*
    (seeded from :func:`~coord.board_doctor_followthrough.pending_refiles`,
    the SAME durable "has this key already been refiled" answer
    :func:`run_pending_refile_close_sweep` reads, rather than a second,
    independent one) closes that gap: it is checked before EVERY dispatch
    and updated the moment one lands, so a second row for a key this very
    loop just filed sees it pending too, and a row revisited in a LATER,
    independent sweep call sees the durable record from the one before.
    """
    from coord.claim import any_matching_branch_merged  # noqa: PLC0415
    from coord.drive_queue import entries_from_rows  # noqa: PLC0415
    from coord.drive_queue import parse_key as _parse_row_key  # noqa: PLC0415
    from coord.state import board_doctor_decisions, list_drive_queue  # noqa: PLC0415

    if board is None:
        from coord.state import build_board  # noqa: PLC0415

        board = build_board()

    is_open = issue_open or _issue_open
    row_writer = writer or board_doctor_followthrough.GitHubFollowThroughWriter()
    entries_by_key = {e.key: e for e in entries_from_rows(list_drive_queue())}
    already_refiled: dict[str, int] = dict(
        board_doctor_followthrough.pending_refiles(board_doctor_decisions())
    )

    dispositions: list[board_doctor_followthrough.Disposition] = []
    #: Keys this call has already dispatched `run_refile` for, regardless
    #: of outcome. A key lands here the moment it is dispatched, not only
    #: when it successfully mints a replacement — that is what keeps a
    #: SECOND occurrence of the same key in this same sweep from running
    #: a second plan leg / alert even when the first attempt didn't file
    #: anything (`ACTION_REFILE_PLAN_FAILED`, `ACTION_REFILE_DEPTH_PAGED`,
    #: or a write failure after the replacement issue was already minted).
    seen_keys: set[str] = set()
    dispatched = 0
    for row in rows:
        if dispatched >= max_rows:
            break
        if row.cause != CAUSE_BRANCH_MERGED_GUARD or row.suppressed:
            continue
        parsed = _parse_row_key(row.key)
        if parsed is None:
            continue
        repo_name, issue_number = parsed
        if not board_doctor_followthrough.auto_refile_enabled(repo_name, config):
            continue
        github_repo = _github_slug(repo_name, config)
        try:
            if not is_open(github_repo, issue_number):
                continue
        except Exception:  # noqa: BLE001 — a flaky open-check must not sink the sweep
            continue

        entry = entries_by_key.get(row.key)
        if entry is None:
            continue

        existing_refile = already_refiled.get(row.key)
        if existing_refile is not None:
            # Reuse, don't file again — and never `record()` this: doing so
            # would overwrite the `ACTION_REFILE_FILED` entry `pending_refiles`
            # itself reads back, making this key invisible to
            # `run_pending_refile_close_sweep` on the next pass.
            dispositions.append(
                board_doctor_followthrough.Disposition(
                    repo_name=repo_name, issue_number=issue_number,
                    action=board_doctor_followthrough.ACTION_REFILE_ALREADY_FILED,
                    dry_run=dry_run, cause=CAUSE_BRANCH_MERGED_GUARD,
                    follow_ups=[existing_refile],
                    reasons=[
                        f"already refiled as #{existing_refile}; not filing again"
                    ],
                )
            )
            continue

        if row.key in seen_keys:
            # Already dispatched for this key earlier in THIS call and it
            # didn't land a replacement issue (the `already_refiled` check
            # above would have caught it if it had) — a second plan leg /
            # alert for the identical key buys nothing and burns a second
            # slot of `max_rows`. Skip quietly, same as the `entry is None`
            # guard above.
            continue

        seen_keys.add(row.key)

        # #4061: re-ask the SAME guard predicate live, right before acting
        # on a stored refusal that can be stale — `is_branch_merged_guard_
        # refusal_reason` only proves the #3376 guard refused dispatch at
        # SOME point in the past, off whatever "merged" meant then. Before
        # #4061, bare `ahead_by == 0` ancestry read a zero-commit leg's own
        # residue branch (never advanced — a premise refusal, a
        # `needs-decomposition` exit, an advisory audit) identically to a
        # branch whose real work had genuinely landed. One question
        # (`coord.claim.any_matching_branch_merged`, #2096), re-asked
        # rather than re-derived: if it no longer reports this issue's
        # branch merged, nothing ever landed here, so there is no
        # remainder to refile — requeue the ORIGINAL issue instead
        # (quadraui#2539). A flaky recheck fails toward the existing
        # refile path (`still_merged = True`) rather than guessing a new
        # action is safe off evidence this call couldn't actually get.
        try:
            still_merged = any_matching_branch_merged(github_repo, issue_number)
        except Exception:  # noqa: BLE001 — see comment above
            still_merged = True
        if not still_merged:
            requeued = dry_run
            detail = ""
            if not dry_run:
                try:
                    from coord.board_doctor_permissions import (  # noqa: PLC0415
                        sweep_drive_queue_requeue,
                    )

                    result = sweep_drive_queue_requeue(repo_name, issue_number)
                    requeued = bool(result["requeued"])
                    detail = str(result["detail"])
                except Exception as exc:  # noqa: BLE001 — a flaky requeue
                    # must not sink the sweep; the row is left exactly
                    # where it was for the next sweep or an operator.
                    detail = str(exc)
            reason = (
                "live recheck: coord.claim.any_matching_branch_merged no "
                "longer reports this issue's branch merged — nothing ever "
                "landed, so this is requeued instead of refiled"
            )
            if not requeued:
                reason += f" — requeue did not land: {detail}" if detail else (
                    " — requeue did not land"
                )
            false_positive = board_doctor_followthrough.Disposition(
                repo_name=repo_name, issue_number=issue_number,
                action=(
                    ACTION_BRANCH_MERGED_GUARD_FALSE_POSITIVE_REQUEUED
                    if requeued else
                    ACTION_BRANCH_MERGED_GUARD_FALSE_POSITIVE_REQUEUE_FAILED
                ),
                dry_run=dry_run, cause=CAUSE_BRANCH_MERGED_GUARD,
                reasons=[reason],
            )
            if not dry_run:
                from coord.state import record_board_doctor_decision  # noqa: PLC0415

                record_board_doctor_decision(false_positive.as_record())
            dispositions.append(false_positive)
            dispatched += 1
            continue

        candidate = build_refile_candidate(
            repo_name, issue_number, config=config, entry=entry,
        )
        disposition = board_doctor_followthrough.run_refile(
            candidate,
            plan_leg=lambda prompt, _c=candidate: plan_leg_dispatcher(
                prompt, partial=_c, config=config,
            ),
            writer=row_writer,
            alert=lambda subject, body, _r=repo_name, _n=issue_number: _followthrough_notify(
                _r, _n, "board-doctor:refile-alert", subject, body,
            ),
            dry_run=dry_run,
        )
        dispositions.append(disposition)
        # Keyed on whether a replacement issue was actually minted — NOT
        # on `disposition.action == ACTION_REFILE_FILED`: `run_refile` sets
        # `follow_up_keys["refile"]` the moment `_file_refile` returns,
        # before `requeue(...)` or `writer.comment_issue(...)` run, so a
        # write failure AFTER a successful file still has a real,
        # already-created issue number here even though the action is
        # `ACTION_REFILE_WRITE_FAILED`. Missing that would let a second
        # occurrence of the same key file a second, identical replacement
        # issue.
        new_number = disposition.follow_up_keys.get("refile")
        if new_number is not None:
            already_refiled[row.key] = new_number
        dispatched += 1
    return dispositions


def _issue_closed_default(repo_name: str, issue_number: int) -> bool:
    from coord import github_ops  # noqa: PLC0415

    return github_ops.issue_is_closed(repo_name, issue_number)


def run_pending_refile_close_sweep(
    *,
    config: Any,
    dry_run: bool = False,
    issue_closed: Callable[[str, int], bool] | None = None,
    writer: board_doctor_followthrough.FollowThroughWriter | None = None,
    decisions: Sequence[Mapping[str, Any]] | None = None,
    record: Callable[[dict], None] | None = None,
) -> list[dict]:
    """Close every original issue a previous #4038 refile pointed away
    from, the first round this sweep observes its own replacement issue
    closed — step 4 of #4038's refile: "comment the pointer on #N. Once
    the refile merges, close #N as superseded. Don't leave that close for
    an operator."

    Independent of :func:`run_refile_sweep` and of *rows*/`collect_rows`
    entirely: the drive-queue row for the ORIGINAL issue is gone the
    moment a refile files successfully (:func:`~coord.board_doctor_
    followthrough._requeue_refile` dequeues it), so nothing about this
    issue is visible to the ordinary row walk any more — the only durable
    trace is the decision log entry :func:`~coord.board_doctor_
    followthrough.run_refile` wrote, which
    :func:`~coord.board_doctor_followthrough.pending_refiles` reads back.

    *decisions*, when given, is the FULL decision log to scan (tests pass
    a fixed list); the default reads it fresh via :func:`coord.state.
    board_doctor_decisions` with no filter, since a pending refile can be
    for ANY repo/issue, not one this call already knows about.
    """
    from coord.drive_queue import parse_key as _parse_row_key  # noqa: PLC0415
    from coord.state import (  # noqa: PLC0415
        board_doctor_decisions,
        record_board_doctor_decision,
    )

    is_closed = issue_closed or _issue_closed_default
    row_writer = writer or board_doctor_followthrough.GitHubFollowThroughWriter()
    do_record = record or record_board_doctor_decision
    all_decisions = decisions if decisions is not None else board_doctor_decisions()

    results: list[dict] = []
    for key, refile_issue in board_doctor_followthrough.pending_refiles(
        all_decisions
    ).items():
        parsed = _parse_row_key(key)
        if parsed is None:
            continue
        repo_name, issue_number = parsed
        github_repo = _github_slug(repo_name, config)
        try:
            closed = is_closed(github_repo, refile_issue)
        except Exception:  # noqa: BLE001 — a flaky read must not sink this pass
            continue
        if not closed:
            continue
        outcome: dict[str, Any] = {"key": key, "refile_issue": refile_issue}
        if dry_run:
            outcome["action"] = "would-close"
            results.append(outcome)
            continue
        try:
            row_writer.close_issue(
                github_repo, issue_number,
                comment=(
                    f"Superseded by #{refile_issue} — refiled for execution "
                    "and the replacement has merged."
                ),
            )
        except Exception as exc:  # noqa: BLE001 — recorded, not raised; a flaky close retries next sweep
            outcome["action"] = "close-failed"
            outcome["error"] = str(exc)
            results.append(outcome)
            continue
        outcome["action"] = board_doctor_followthrough.ACTION_REFILE_SUPERSEDED
        do_record({
            "queue": "drive-queue",
            "key": key,
            "cause": CAUSE_BRANCH_MERGED_GUARD,
            "action": board_doctor_followthrough.ACTION_REFILE_SUPERSEDED,
            "dry_run": False,
            "plan_leg_assignment_id": None,
            "follow_ups": [refile_issue],
            "follow_up_keys": {"refile": refile_issue},
            "deduped": {},
            "queued": {},
            "reasons": [],
            "evidence": [],
        })
        results.append(outcome)
    return results


#: Default ceiling on how many eligible ``merged-partial`` rows
#: :func:`run_followthrough_sweep` will actually dispatch a plan leg for
#: in ONE call, when the caller passes no *max_rows* of its own. Each
#: dispatched row blocks this sweep for up to
#: :data:`PLAN_LEG_TIMEOUT_SECONDS` (20 minutes) — unlike the C1
#: investigate tier (bounded by `capacity_check`/
#: `default_investigate_capacity`), this pass has no per-repo budget to
#: defer to, so without SOME ceiling N eligible rows in one sweep means N
#: serialized 20-minute legs before the sweep (and the scheduler slot
#: running it) is free again. A row past the ceiling is simply left for
#: the next sweep — no disposition, no alert, same as an opted-out repo.
DEFAULT_FOLLOWTHROUGH_ROWS_PER_SWEEP = 3


def run_followthrough_sweep(
    rows: Sequence[StuckRow],
    *,
    config: Any,
    board: Board | None = None,
    dry_run: bool = False,
    plan_leg_dispatcher: Callable[..., board_doctor_followthrough.PlanLegResult] = (
        dispatch_plan_leg
    ),
    writer: board_doctor_followthrough.FollowThroughWriter | None = None,
    issue_open: Callable[[str, int], bool] | None = None,
    max_rows: int = DEFAULT_FOLLOWTHROUGH_ROWS_PER_SWEEP,
    covering_pr_check: Callable[[str, int], int | None] | None = None,
    pending_loader: Callable[
        [str, int], board_doctor_followthrough.PendingFollowThrough | None
    ] | None = None,
) -> list[board_doctor_followthrough.Disposition]:
    """Resolve every eligible ``merged-partial`` row in *rows*, up to
    *max_rows* of them (#3945).

    Eligible: ``row.cause == CAUSE_MERGED_PARTIAL``, not ``row.suppressed``
    (a #3761-suppressed row defers to its repo's baseline-red row instead),
    the repo has opted in
    (:func:`coord.board_doctor_followthrough.followthrough_enabled`), and
    the issue is STILL open (*issue_open*, defaulting to :func:`_issue_open`
    — the one place this is checked, see :func:`merged_partial_rows`'s
    docstring for why it isn't checked there).

    *board*, when ``None`` (the default), is read once here via
    :func:`coord.state.build_board` — a caller that already has one from
    the same sweep (:func:`collect_rows`) should pass it through rather
    than paying for a second read. *writer* defaults to a real
    :class:`~coord.board_doctor_followthrough.GitHubFollowThroughWriter`.

    Every GitHub-side read this function itself performs (*issue_open*'s
    default, ``epic_exists``, ``milestone_exists``, and the
    ``covering_issue_open`` check on a plan's own ``already_covered_by``
    claim) is addressed by the repo's GitHub ``owner/repo`` slug
    (:func:`_github_slug`), never the local `coordinator.yml` name
    *repo_name* — :func:`build_partial_merge` resolves the same slug for
    everything it reads/writes, and the two must agree on which repo they
    are each talking to GitHub about.

    Before dispatching anything, each row's existing coverage is checked:
    *covering_pr_check* (default :func:`_default_covering_pr_check`, a
    per-repo-cached :func:`_covering_prs_by_issue`) for an open PR that
    already carries a GitHub closing keyword for the parent, and — only
    when that finds none — *pending_loader* (default
    :func:`_load_pending_followthrough`) for a previously-recorded,
    unfinished attempt to resume. Both feed
    :func:`~coord.board_doctor_followthrough.run_followthrough` as its
    ``coverage`` argument, which is what makes a write failure after
    `create_issue` idempotent across sweeps rather than re-planned and
    re-filed from scratch. Injectable the same way *issue_open*/*writer*
    already are, so a caller can fake either GitHub-side read without a
    live `gh` call. Both calls are guarded the same way as *issue_open*'s:
    a read that raises (an injected fake, or `_load_pending_followthrough`
    under `COORD_STRICT_LOCAL_BOARD=1`) reads as "no coverage" for this row
    rather than aborting the whole sweep.

    A row resolved as :data:`~coord.board_doctor_followthrough.
    ACTION_ALREADY_COVERED` does not consume one of *max_rows*' slots —
    see the loop's own comment for why.
    """
    from coord.drive_queue import parse_key as _parse_row_key  # noqa: PLC0415

    if board is None:
        from coord.state import build_board  # noqa: PLC0415

        board = build_board()

    is_open = issue_open or _issue_open
    check_covering_pr = covering_pr_check or _default_covering_pr_check()
    load_pending = pending_loader or _load_pending_followthrough
    row_writer = writer or board_doctor_followthrough.GitHubFollowThroughWriter()
    cap = getattr(
        getattr(config, "board_doctor", None), "followthrough_cap",
        board_doctor_followthrough.DEFAULT_FOLLOWUP_CAP,
    )

    dispositions: list[board_doctor_followthrough.Disposition] = []
    # Counts only rows that actually dispatch a plan leg or resume a
    # pending write — NOT one resolved as `ACTION_ALREADY_COVERED` before
    # any of that: the cap's rationale (see its own docstring) is bounding
    # how many 20-minute plan legs one sweep call blocks on, and a covered
    # row dispatches none. Counting it anyway would let up to `max_rows`
    # already-covered rows — which never resolve until their covering PR
    # merges, so they recur every sweep — starve the follow-through pass
    # of its real budget indefinitely.
    dispatched = 0
    for row in rows:
        if dispatched >= max_rows:
            break
        if row.cause != CAUSE_MERGED_PARTIAL or row.suppressed:
            continue
        parsed = _parse_row_key(row.key)
        if parsed is None:
            continue
        repo_name, issue_number = parsed
        if not board_doctor_followthrough.followthrough_enabled(repo_name, config):
            continue
        github_repo = _github_slug(repo_name, config)
        try:
            if not is_open(github_repo, issue_number):
                continue
        except Exception:  # noqa: BLE001 — a flaky open-check must not sink the sweep
            continue

        try:
            covering_pr = check_covering_pr(github_repo, issue_number)
        except Exception:  # noqa: BLE001 — a flaky coverage read must not sink the sweep
            covering_pr = None
        try:
            pending = (
                None if covering_pr is not None
                else load_pending(repo_name, issue_number)
            )
        except Exception:  # noqa: BLE001 — a flaky resume read must not sink the sweep
            pending = None
        coverage = board_doctor_followthrough.FollowThroughCoverage(
            covering_pr=covering_pr, pending=pending,
        )

        partial = build_partial_merge(
            repo_name, issue_number, config=config, board=board,
        )
        backends = (
            config.preflight_guard.for_repo(repo_name).backends
            if getattr(config, "preflight_guard", None) is not None
            else ()
        )
        auto_queue = board_doctor_followthrough.build_auto_queue_context(
            repo_name, config,
        )
        disposition = board_doctor_followthrough.run_followthrough(
            partial,
            plan_leg=lambda prompt, _partial=partial: plan_leg_dispatcher(
                prompt, partial=_partial, config=config,
            ),
            writer=row_writer,
            alert=lambda subject, body, _r=repo_name, _n=issue_number: _followthrough_notify(
                _r, _n, "board-doctor:followthrough-alert", subject, body,
            ),
            page=lambda subject, body, _r=repo_name, _n=issue_number: _followthrough_notify(
                _r, _n, "board-doctor:followthrough-page", subject, body,
            ),
            cap=cap,
            backends=backends,
            epic_exists=lambda epic_number, _r=github_repo: _issue_exists(_r, epic_number),
            milestone_exists=lambda title, _r=github_repo: _milestone_exists(_r, title),
            covering_issue_open=lambda number, _r=github_repo: _issue_open(_r, number),
            dry_run=dry_run,
            auto_queue=auto_queue,
            coverage=coverage,
        )
        dispositions.append(disposition)
        if disposition.action != board_doctor_followthrough.ACTION_ALREADY_COVERED:
            dispatched += 1
    return dispositions


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

    queue: str  # "drive-queue", "merge-queue", "baseline-red", "smoke-no-verdict", or "idle-fleet-root"
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
    #: `_fixit_rebudget_environmental` (`coord/board_doctor_policy.py`)
    #: requires this before it will let a high-confidence cause read as
    #: environmental and skip the #2972 ceiling: bypassing the ceiling on a
    #: stale stored verdict (e.g. a `merge-conflict`/`high` row whose PR has
    #: since merged) would spend this issue's one lifetime fix-it
    #: auto-rebudget leg (#3847) against evidence that is no longer true,
    #: which is exactly what the ceiling exists to stop.
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
    #: #3945: one :class:`~coord.board_doctor_followthrough.Disposition`
    #: per ``merged-partial`` row :func:`run_followthrough_sweep` acted on
    #: this round — separate from *decisions* because the follow-through
    #: disposition isn't a tier/auto_dispatchers/convergence decision (see
    #: the "Merged-partial follow-through" section's module comment).
    followthrough: list[board_doctor_followthrough.Disposition] = field(
        default_factory=list,
    )
    #: #4037: one note per PREDICTED ``after=`` edge
    #: :func:`drop_stale_predicted_after_edges` dropped this round, via
    #: :func:`sweep_predicted_after_edges`. Separate from *decisions* for
    #: the same reason *followthrough* is: dropping a stale edge is not a
    #: tier/auto_dispatchers/convergence decision over a :class:`StuckRow`
    #: at all — it runs over the raw queue, independent of `collect_rows`'
    #: own classification walk.
    dropped_predicted_after_edges: list[str] = field(default_factory=list)
    #: #4038: one :class:`~coord.board_doctor_followthrough.Disposition`
    #: per ``branch-merged-guard`` row :func:`run_refile_sweep` acted on
    #: this round — same "not a tier/auto_dispatchers/convergence decision"
    #: shape as *followthrough* above, kept in its own list rather than
    #: folded into it since the two causes resolve differently (step 4's
    #: deferred close has no `merged-partial` analogue) and a CLI reader
    #: must be able to label each correctly rather than guess from
    #: `action` strings.
    refile: list[board_doctor_followthrough.Disposition] = field(default_factory=list)
    #: #4038: one dict per previously-filed refile
    #: :func:`run_pending_refile_close_sweep` closed as superseded this
    #: round, because its own replacement issue was observed closed.
    refile_closed: list[dict] = field(default_factory=list)
    #: #4036: one dict per ceiling-blocked, ``CAUSE_CI_PENDING``
    #: drive-queue row :func:`run_conflict_fix_ceiling_pending_sweep`
    #: evaluated this round — ``{"repo", "issue", "requeued", "detail"}``.
    #: Same "not a tier/auto_dispatchers/convergence decision" shape as
    #: *followthrough*/*refile* above: waiting for (or acting on) live CI
    #: is not a cause this sweep converges or alerts on.
    conflict_fix_ceiling_pending: list[dict] = field(default_factory=list)
    #: #4048: one dict per :class:`ConfigDriftCandidate`
    #: :func:`run_config_drift_fix_sweep` attempted this round —
    #: ``{"repo", "issue", "applied", "requeued", "refunded", "detail"}``.
    #: Same "not a tier/auto_dispatchers/convergence decision" shape as
    #: *conflict_fix_ceiling_pending* above.
    config_drift_fix: list[dict] = field(default_factory=list)
    #: #4050: one dict per ceiling-blocked, ``CAUSE_INTEGRATION_RED``
    #: drive-queue row :func:`run_base_sync_fix_ceiling_sweep` evaluated
    #: this round — ``{"repo", "issue", "dispatched", "detail"}``. Same
    #: "not a tier/auto_dispatchers/convergence decision" shape as
    #: *conflict_fix_ceiling_pending*/*config_drift_fix* above: this cause
    #: has a positively-confirmed, budget-free remedy the moment the row
    #: is ceiling-blocked, so it is never handed to the generic
    #: investigate-tier fix-it-leg dispatcher (which WOULD refuse — see
    #: #3454 — since that dispatcher's budget is exactly what this row
    #: already exhausted).
    base_sync_fix_ceiling_dispatch: list[dict] = field(default_factory=list)


def merge_queue_rows(
    entries: Sequence[Any],
    *,
    now: float | None = None,
    stale_after_seconds: float | None = None,
) -> list[StuckRow]:
    """Local cause mapping for merge-queue rows (#3758).

    :func:`coord.queue_diagnose.run_pass` only understands drive-queue
    ``QueueEntry`` rows (#2276 Phase 1); the merge-queue's own state
    vocabulary is already thin (``pending``/``merging``/``merged``/
    ``conflict``/``skipped``/``human_required``), so this reuses
    ``queue_diagnose``'s ``CAUSE_*`` vocabulary rather than inventing a
    second one, and needs no `gh` call — the already-refreshed
    ``QueuedMerge.state``/``.error`` is evidence enough for a cause this
    coarse.

    Two #3887 refinements on top of the plain state->cause table
    (:data:`_MERGE_QUEUE_STUCK_CAUSE`):

    * A ``human_required`` entry whose ``error`` carries
      `coord.merge_queue.CONFLICT_FIX_HEAD_UNMOVED_SIGNAL` is relabelled
      :data:`CAUSE_CONFLICT_FIX_HEAD_UNMOVED` instead of the generic
      :data:`coord.queue_diagnose.CAUSE_GATE_BLOCKED` — this is a sharper
      reading of the SAME live evidence `_MERGE_QUEUE_STUCK_CAUSE` already
      has in hand, not a reclassification of a settled verdict, so it
      belongs here rather than in a post-hoc ``_reclassify_*`` pass.
    * A ``pending`` entry (otherwise invisible to this function entirely —
      waiting its turn is not stuck) whose ``last_attempt`` is set and at
      least :data:`MERGE_PENDING_STALE_AFTER_SECONDS` old is labelled
      :data:`CAUSE_MERGE_PENDING_STUCK` — but only as a cheap candidate
      filter. Staleness alone is not proof the row is actually stuck
      (see :func:`_confirm_merge_pending_stuck`'s docstring), so this
      label never reaches a caller un-confirmed: :func:`collect_rows`
      always runs it through that live re-check before the cause is
      acted on or escalated. #3918 review: an entry whose ``last_attempt``
      is ``None`` (the merge gate has never actually been evaluated at
      all) is deliberately NOT folded into this filter even though it
      looks like the same shape aged off ``enqueued_at`` instead — this
      function's own ``cause`` becomes the caller's ``covered_keys``
      BEFORE :func:`_confirm_merge_pending_stuck` gets a chance to drop an
      unconfirmed candidate, so minting a row here that confirmation then
      silently drops (its own documented, correct behavior for a row that
      is merely waiting on something ordinary — CI still running, a
      #3886 reservation held) would make the row invisible to
      :func:`no_progress_rows` too, for the one state that caller's own
      nudge-based `running` shape (#3918) is positioned to catch instead:
      the owning drive is very likely still alive and polling, so leaving
      this entry OFF this filter is what lets that shape see it.

    *now* defaults to :func:`time.time` — overridable so a replay test can
    assert on a fixed, reproducible staleness window.

    *stale_after_seconds* (#4027) overrides :data:`MERGE_PENDING_STALE_AFTER_SECONDS`
    for this call only — :func:`run_sweep`'s ``mechanical_causes``-filtered
    (tight-cadence) call threads a much shorter window through here so the
    "green/approved/mergeable but the ordinary merge tick keeps missing it"
    shape does not wait up to 30 minutes just because the general-purpose
    pre-filter does; the live re-check in :func:`_confirm_merge_pending_stuck`
    still has to confirm the row before anything acts on it either way, so
    shortening this cheap candidate window never trades away correctness.
    """
    import time as _time  # noqa: PLC0415

    from coord.merge_queue import CONFLICT_FIX_HEAD_UNMOVED_SIGNAL, PENDING  # noqa: PLC0415

    moment = _time.time() if now is None else now
    stale_after = (
        MERGE_PENDING_STALE_AFTER_SECONDS if stale_after_seconds is None else stale_after_seconds
    )
    rows: list[StuckRow] = []
    for entry in entries:
        cause = _MERGE_QUEUE_STUCK_CAUSE.get(entry.state)
        if (
            cause == queue_diagnose.CAUSE_GATE_BLOCKED
            and CONFLICT_FIX_HEAD_UNMOVED_SIGNAL in (entry.error or "")
        ):
            cause = CAUSE_CONFLICT_FIX_HEAD_UNMOVED
        if cause is None:
            if (
                entry.state == PENDING
                and entry.last_attempt is not None
                and moment - entry.last_attempt >= stale_after
            ):
                cause = CAUSE_MERGE_PENDING_STUCK
            else:
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


def _confirm_merge_pending_stuck(
    rows: list[StuckRow], merge_entries: Sequence[Any], *, board: Board, config
) -> list[StuckRow]:
    """Keep :data:`CAUSE_MERGE_PENDING_STUCK` only for a row this sweep can
    confirm, LIVE, actually reads green/approved/mergeable (#3887).

    :func:`merge_queue_rows` mints this cause off ``entry.last_attempt``
    staleness alone, which is not proof every gate is clear — two
    reachable counterexamples: (a) an entry a conflict-fix rebase just
    returned to `PENDING` (`coord.merge_queue.reconcile_conflict_entries`)
    whose CI is now re-running on the new head, routinely past this
    cause's own staleness window; (b) an entry deferred behind another
    entry's #3886 per-repo merge-gate reservation, whose `last_attempt`
    predates the reservation and never refreshes while it holds. Both are
    healthy rows waiting on something ordinary, not a stuck merge tick.

    `coord.merge_queue.entry_gate_status` already evaluates review, smoke,
    CI, the epic-closing-keyword gate AND the #3886 reservation, in the
    SAME order `process()` would — one re-derivation covers both
    counterexamples above, the same "ask the live gate, don't guess"
    posture `coord.board_doctor_policy._sealed_confined_lines` already
    takes for a different cause. Fail-closed throughout: no matching
    merge-queue entry, no CI store configured, a `gh` read that raises, or
    a gate that reads anything other than `coord.merge_queue.PLAN_READY`
    all drop the row from this sweep rather than mint a cause this sweep
    cannot actually back up — a missed, truly stuck row simply waits for
    the next sweep, while a false "stuck" label would otherwise burn a
    convergence-ladder slot (and eventually a human page) for a row that
    was never wrong.
    """
    from coord import github_ops  # noqa: PLC0415
    from coord.ci_store import build_ci_store  # noqa: PLC0415
    from coord.merge_queue import PLAN_READY, entry_gate_status  # noqa: PLC0415

    entries_by_key = {
        f"{entry.repo_name}#{entry.issue_number}": entry for entry in merge_entries
    }
    confirmed: list[StuckRow] = []
    for row in rows:
        if row.queue != "merge-queue" or row.cause != CAUSE_MERGE_PENDING_STUCK:
            confirmed.append(row)
            continue
        entry = entries_by_key.get(row.key)
        live_ready = False
        if entry is not None:
            try:
                ci_cfg = config.ci_store
                ci_store = build_ci_store(
                    ci_cfg.type or "github", host=ci_cfg.host, token_env=ci_cfg.token_env,
                )
                status, _reason = entry_gate_status(
                    entry, board, config, ci_store, github_ops,
                )
                live_ready = status == PLAN_READY
            except Exception:  # noqa: BLE001 — best-effort live confirm; see docstring
                _log.debug(
                    "board-doctor: merge-pending-stuck live confirm failed for %s",
                    row.key, exc_info=True,
                )
                live_ready = False
        if live_ready:
            confirmed.append(row)
        # else: not confirmed stuck THIS sweep — drop it rather than act
        # (or escalate) on an unconfirmed reading.
    return confirmed


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


#: Synthetic queue for the one-row-per-root alert a repo idling entirely
#: behind one or more blocked/parked roots gets — distinct from the root's
#: own ordinary ``queue="drive-queue"`` row so the two keep separate
#: convergence/backoff counters (`sweep`'s ``touched`` set is keyed on
#: ``(queue, key)``, never ``key`` alone) and so `run_sweep` can route it to
#: its own delivery path (:func:`coord.notify.post_idle_fleet_root_alert`)
#: instead of the ordinary per-row escalation.
IDLE_FLEET_ROOT_QUEUE = "idle-fleet-root"


def _transitive_dependent_count(key: str, dependents_of: Mapping[str, Sequence[str]]) -> int:
    """How many entries, at any chain depth, have *key* somewhere in their
    ``after=`` ancestry — walks *dependents_of* (``dep -> [keys naming it]``)
    rather than counting only direct edges, so a root's evidence line names
    the full chain behind it, not just its immediate dependents.
    """
    seen: set[str] = set()
    stack = list(dependents_of.get(key, ()))
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(dependents_of.get(current, ()))
    return len(seen)


def idle_fleet_root_rows(
    entries: Sequence[Any],
    rows: Sequence[StuckRow] = (),
    *,
    blocked_streaks: Mapping[str, int] | None = None,
) -> list[StuckRow]:
    """One :class:`StuckRow` per root a whole repo sits idle behind.

    A repo is idle when none of its drive-queue entries is ``running`` —
    a ``waiting`` entry counts for nothing here: once its own ``after=``
    chain bottoms out on a terminal park, `_resolve_prereqs` leaves it
    `waiting` forever rather than ever reaching `blocked` (see
    :func:`coord.notifier.collect.halted_drives`'s own docstring for the
    identical shape), so treating `waiting` as "still making progress"
    is exactly how a repo can sit idle behind a parked root with nothing
    ever firing.

    A root is a `blocked`/`parked` entry whose own ``last_reason`` is NOT
    one of `_resolve_prereqs`'s after-chain cascade verdicts
    (:func:`coord.drive_queue.is_unsatisfiable_prereq_reason`) — i.e. it
    is stuck for its OWN reason, not because a pre-req of its own is.
    Every other entry chained behind it inherits that cascade verdict as
    its own ``last_reason`` and is excluded, leaving exactly the root(s) —
    regardless of ``attempts`` or which specific marker its reason
    carries, so a confirmed-shut-gate/exhausted-budget block and a
    #1844/#2019/#2972/#3522 permanent block both qualify as roots just
    like a terminal park does.

    *rows* is this same sweep's already-classified rows
    (:func:`_collect_rows_with_board`'s own accumulator at the point this
    is called) — reused for a root's cause/confidence/evidence rather than
    re-deriving a second, narrower classification here: a root's cause is
    whatever `coord.queue_diagnose.run_pass` (or the stored episode it
    settled from) already concluded for that exact key, which covers the
    full cause vocabulary, not just a handful of labels invented for this
    check. A root with no matching row (no episode ever opened for it, or
    one suppressed by a baseline-red streak) falls back to
    :func:`coord.drive_queue.classify_unreachable_wait_cause`.

    *blocked_streaks* (repos already past the baseline-red merge-block
    threshold) are skipped outright — :func:`baseline_red_rows` already
    files the one repo-wide alert those need; a root's own drive-queue row
    is `suppressed` for exactly this reason and must not also page here
    under a fallback classification.

    Only emits a root whose cause :func:`default_tier_of` says is
    alert-tier — a cause this epic already auto-fixes needs no second,
    redundant page; its own handler already acts on the root directly.
    EXCEPT a root whose own ``last_reason`` carries a
    #1844/#2019/#2972/#3522 permanent-block marker
    (:func:`coord.drive_queue.is_permanent_block_reason`): that marker is
    written only once a handler has already tried and given up (e.g. the
    #2972 fix-round ceiling text an exhausted CI-red/gate-blocked
    investigate-tier retry stream stamps into ``last_reason`` once its own
    budget is spent — `coord.drive_queue.is_fix_round_ceiling_reason`),
    so the cause's normally-auto tier no longer describes this instance;
    this always alerts for it, unconditionally.
    """
    from coord.drive_queue import (  # noqa: PLC0415
        STATE_BLOCKED,
        STATE_PARKED,
        STATE_RUNNING,
        classify_unreachable_wait_cause,
        is_permanent_block_reason,
        is_unsatisfiable_prereq_reason,
    )

    rows_by_key: dict[str, StuckRow] = {}
    for row in rows:
        if row.queue == "drive-queue" and not row.suppressed:
            rows_by_key.setdefault(row.key, row)

    by_repo: dict[str, list[Any]] = {}
    for entry in entries:
        by_repo.setdefault(entry.repo, []).append(entry)

    out: list[StuckRow] = []
    for repo_name, repo_entries in by_repo.items():
        if blocked_streaks and repo_name in blocked_streaks:
            continue
        if any(e.state == STATE_RUNNING for e in repo_entries):
            continue
        dependents_of: dict[str, list[str]] = {}
        for e in repo_entries:
            for dep in e.after:
                dependents_of.setdefault(dep, []).append(e.key)
        for entry in repo_entries:
            if entry.state not in (STATE_BLOCKED, STATE_PARKED):
                continue
            if is_unsatisfiable_prereq_reason(entry.last_reason):
                continue
            root_row = rows_by_key.get(entry.key)
            cause = root_row.cause if root_row is not None else classify_unreachable_wait_cause(entry)
            if (
                default_tier_of(cause) != TIER_ALERT
                and not is_permanent_block_reason(entry.last_reason)
            ):
                continue
            confidence = (
                root_row.confidence if root_row is not None else queue_diagnose.CONFIDENCE_HIGH
            )
            own_evidence = (
                root_row.evidence if root_row is not None else (f"last_reason: {entry.last_reason}",)
            )
            out.append(
                StuckRow(
                    queue=IDLE_FLEET_ROOT_QUEUE,
                    key=entry.key,
                    state=entry.state,
                    cause=cause,
                    confidence=confidence,
                    evidence=(
                        f"{repo_name}: 0 running drive-queue rows",
                        f"{_transitive_dependent_count(entry.key, dependents_of)} "
                        "row(s) queued behind this root",
                        *own_evidence,
                    ),
                    fresh=root_row.fresh if root_row is not None else True,
                )
            )
    return out


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


@dataclass(frozen=True)
class ConfigDriftCandidate:
    """One #4035/#4048 config-drift classification — everything both the
    Detect-step row (:func:`config_drift_rows`) and the Apply-step sweep
    (:func:`run_config_drift_fix_sweep`) need, built exactly once per
    (repo, issue) so the two never re-derive (and risk disagreeing on) the
    same (kind, name)/token/command/plan from two independent parses
    (#2096, "one question, one answer")."""

    repo_name: str
    issue_number: int
    kind: str
    name: str
    token: str
    command: str
    source: str
    plan: Any  # coord.board_doctor_policy.ConfigDriftFix
    #: #4048: how many WORK-LIKE legs for this issue failed/blocked on this
    #: EXACT (kind, name) signature — "the work legs whose only failure
    #: was the drift signature" the issue's own Refund step gives back via
    #: :func:`coord.board_doctor_permissions.sweep_raise_fixit_budget`'s
    #: *increment*. Counts every matching leg, including the issue's
    #: unconditional initial one — #3998's own documented direction for
    #: this kind of imprecision ("harmless... it can only hand out a few
    #: EXTRA rounds... never fewer") applies here too: refunding one round
    #: more than `coord.drive_queue.remaining_fix_rounds`'s own
    #: ``fix_rounds_spent`` formula would count is the safe side to err on.
    rounds_consumed: int


def config_drift_candidates(
    board: Board,
    config: Any,
    *,
    diff_lookup: Callable[[str, str], list[str]] | None = None,
) -> list[ConfigDriftCandidate]:
    """One :class:`ConfigDriftCandidate` per issue whose Test-stage verdict
    names a cargo target this branch's OWN diff deletes entirely (#4035's
    Detect step) — quadraui#2695's shape: the fleet config still runs a
    command naming a target the branch legitimately removed, so the
    identical failure repeats every round no matter what a worker does to
    the branch's own code.

    *diff_lookup* defaults to :func:`coord.smoke._fetch_deleted_files` —
    injectable so a test never needs a live ``gh`` call. Mirrors
    :func:`smoke_no_verdict_rows`'s own shape: walks ``board.active +
    board.completed`` directly (never a second ``build_board`` call),
    reads ``assignment.test_reason``/``.branch`` straight off the row the
    normal Test-stage write already populated, and dedupes to ONE
    candidate per issue.

    Classification is :func:`coord.queue_diagnose.classify_config_drift` —
    the SAME function :mod:`coord.smoke`'s own pre-smoke gate
    (``_gate_config_drift_target_deleted``) ultimately answers to via the
    shared ``coord.smoke.cargo_target_touches_path`` primitive (#2096,
    "one question, one answer") — never a second, independently-tuned
    copy of "does this look like config drift" living in this module.
    A deleted-paths lookup that comes back empty (no PR found, a transient
    `gh` failure) never manufactures a hit — it just finds nothing, the
    same fail-open posture every other probe in this module takes.

    The row's cause must stay FALSIFIABLE by the thing it accuses
    (#4035 review): ``assignment.test_reason`` is a stored snapshot from
    whatever round wrote it, so confirming only against that snapshot and
    the diff would keep minting the same row every sweep even after an
    operator (or the autofix path) removes the token from
    ``coordinator.yml`` — the exact "a snapshot too stale to contradict
    anything" shape epic #2096 names. So a candidate is minted ONLY when
    the repo's TODAY-resolved command (:func:`coord.smoke.
    resolve_smoke_command`) still carries the matching token; once it
    does, ``plan`` (:func:`coord.board_doctor_policy.
    plan_config_drift_fix`) carries both the alert message a human (or,
    opted in, the doctor itself) would apply AND the opt-in/patch
    decision :func:`run_config_drift_fix_sweep` acts on.
    """
    from coord.board_doctor_policy import plan_config_drift_fix  # noqa: PLC0415
    from coord.models import WORK_LIKE_TYPES  # noqa: PLC0415
    from coord.queue_diagnose import (  # noqa: PLC0415
        classify_config_drift,
        extract_dangling_cargo_target,
    )
    from coord.smoke import (  # noqa: PLC0415
        SmokeTestsConfig,
        TEST_STATE_BLOCKED,
        _fetch_deleted_files,
        cargo_target_flags_in_command,
        resolve_smoke_command,
    )

    lookup = diff_lookup if diff_lookup is not None else _fetch_deleted_files

    seen: dict[str, ConfigDriftCandidate] = {}
    for assignment in (*board.active, *board.completed):
        if assignment.type not in WORK_LIKE_TYPES:
            continue
        if assignment.test_state not in ("failed", TEST_STATE_BLOCKED):
            continue
        if not assignment.test_reason or not assignment.branch:
            continue
        key = f"{assignment.repo_name}#{assignment.issue_number}"
        if key in seen:
            continue
        # Cheap, local filter FIRST (#4035 review): most failures don't
        # even name a dangling cargo target, so rule those out before
        # spending a `gh` round-trip on `lookup` below.
        if extract_dangling_cargo_target(assignment.test_reason) is None:
            continue
        repo = config.repo(assignment.repo_name)
        if repo is None:
            continue
        deleted = lookup(repo.github, assignment.branch)
        if not deleted:
            continue
        found = classify_config_drift(assignment.test_reason, deleted)
        if found is None:
            continue
        kind, name = found

        smoke_cfg = getattr(config, "smoke_tests", SmokeTestsConfig())
        command_obj = resolve_smoke_command(repo, smoke_cfg, deleted)
        if not command_obj.command:
            continue
        token = next(
            (
                tok for k, n, tok in cargo_target_flags_in_command(
                    command_obj.command,
                )
                if k == kind and n == name
            ),
            None,
        )
        if token is None:
            # The command resolved TODAY no longer carries the token the
            # stored `test_reason` complained about — the config may
            # already have moved on since that snapshot was written, so
            # this cause can no longer be confirmed against it.
            continue

        plan = plan_config_drift_fix(
            assignment.repo_name, command=command_obj.command,
            source=command_obj.source, token=token, config=config,
        )
        rounds_consumed = sum(
            1
            for other in (*board.active, *board.completed)
            if other.repo_name == assignment.repo_name
            and other.issue_number == assignment.issue_number
            and other.type in WORK_LIKE_TYPES
            and other.test_state in ("failed", TEST_STATE_BLOCKED)
            and extract_dangling_cargo_target(other.test_reason or "")
            == (kind, name)
        )
        seen[key] = ConfigDriftCandidate(
            repo_name=assignment.repo_name,
            issue_number=assignment.issue_number,
            kind=kind,
            name=name,
            token=token,
            command=command_obj.command,
            source=command_obj.source,
            plan=plan,
            rounds_consumed=rounds_consumed,
        )
    return list(seen.values())


def config_drift_rows(
    board: Board,
    config: Any,
    *,
    diff_lookup: Callable[[str, str], list[str]] | None = None,
) -> list[StuckRow]:
    """One :class:`StuckRow` per :class:`ConfigDriftCandidate`
    (:func:`config_drift_candidates`) — the Detect-step sweep source
    feeding :data:`queue_diagnose.CAUSE_CONFIG_DRIFT` into the board
    doctor. ``evidence`` is always the candidate's own ``plan.
    alert_message`` — the exact one-line coordinator.yml patch — whether
    or not this repo has opted into the #4048 Apply step
    (:func:`run_config_drift_fix_sweep`): an opted-out repo's row rides
    the ordinary alert tier straight to a human with that same message;
    an opted-in repo's row is pulled out of the alert tier entirely once
    (and only once) :func:`run_sweep` confirms the Apply step actually
    landed this round.
    """
    return [
        StuckRow(
            queue="config-drift",
            key=f"{candidate.repo_name}#{candidate.issue_number}",
            state="blocked",
            cause=queue_diagnose.CAUSE_CONFIG_DRIFT,
            confidence=queue_diagnose.CONFIDENCE_HIGH,
            evidence=(candidate.plan.alert_message,),
            fresh=True,
        )
        for candidate in config_drift_candidates(
            board, config, diff_lookup=diff_lookup,
        )
    ]


@dataclass(frozen=True)
class ConfigDriftApplyResult:
    """:func:`apply_config_drift_fix_to_coord_settings`'s return shape."""

    applied: bool
    detail: str


def _config_drift_git(repo_dir: Any, *args: str) -> tuple[bool, str]:
    """One read/write ``git`` invocation against *repo_dir*, never raising.

    Local to this module rather than imported from
    :mod:`coord.health.checks.config_drift` (that module's own ``_git`` is
    private, and deliberately read-only — see its docstring, "Report
    only — never ``git push``" — the opposite contract this function
    exists for), so this module's entire mutating git surface stays
    visible in one place.
    """
    import subprocess  # noqa: PLC0415

    try:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), *args],
            capture_output=True,
            text=True,
            timeout=10.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "").strip()
    return True, result.stdout


def apply_config_drift_fix_to_coord_settings(
    *,
    config_path: Any,
    command: str,
    token: str,
    patched_command: str,
    repo_name: str,
    issue_number: int,
) -> ConfigDriftApplyResult:
    """#4048: remove *token* from *command* in the live, symlinked
    ``coordinator.yml`` *config_path* resolves to, and commit that one-line
    removal in its ``coord-settings`` checkout.

    *config_path* is resolved the same way :func:`coord.health.checks.
    config_drift.probe_config_drift` resolves it — ``Path.resolve()``,
    never a bare ``Path.exists()`` on the symlink itself, since the
    canonical config path has flipped between a plain file and a symlink
    before (that check's own docstring) and this must survive either
    shape.

    Edits ONLY the matched *token*, and only when both it and the exact
    *command* text that names it appear in the file EXACTLY once —
    mirroring :func:`coord.smoke.remove_dangling_target_token`'s own
    refusal discipline one level up, at the file-text level rather than
    the in-memory command string: a *token* already cleared by an earlier
    round (or a human), or one ambiguous because some OTHER repo's command
    in the same file happens to name it too, is left for a human rather
    than guessed at. Writes nothing at all when either check refuses.

    Two more guards run BEFORE any of that, both refusing rather than
    writing — a write that lands on either is exactly the live-incident
    shape #2096/#2861 already name for this checkout:

    * **Dirty.** ``git status --porcelain -- <real_path>`` must be empty
      first. An operator mid-edit of the live
      ``~/.coord/coordinator.yml`` symlink already has an uncommitted
      change sitting in this exact file; overwriting it with
      ``read_text()``/``write_text()`` would silently fold that
      in-progress (possibly broken) edit into this function's own
      one-token commit. Refuses rather than guessing which lines are
      "theirs".
    * **Stale.** The checkout must not be behind its ``@{upstream}``,
      checked the same way :func:`coord.commands.repo._guard_settings_fresh`
      guards a config WRITE (best-effort ``git fetch`` first, then
      ``git rev-list --left-right --count``) — never trusting the
      existing remote-tracking ref alone, since the whole point is "the
      remote moved and nobody here noticed". Unlike that CLI guard this
      never raises: no upstream configured, or the fetch itself failing,
      is reported as unknown and left to proceed (an offline daemon host
      must not wedge), but a confirmed nonzero `behind` count refuses —
      committing onto a stale base would land a push failure that
      silently refunds a fix round while the fleet's canonical config
      has already diverged.

    The commit itself is pathspec-limited to *real_path* (``git commit
    -- <real_path>``, not a bare ``git commit``), so even if something
    else were staged in the checkout, only this one file's change is
    swept into the commit this function makes.

    Editing and committing directly in the checkout `~/.coord/
    coordinator.yml` resolves into already IS "fast-forwarding the daemon
    host's checkout" (#4048's own wording for this step): there is no
    second, separate checkout to advance — the resolved path already
    belongs to the one checkout this very daemon's ``coord serve`` reads
    on every reload, so the commit this function makes takes effect
    immediately with no further step. ``git push`` is best-effort only:
    :func:`coord.health.checks.config_drift.probe_config_drift` (#3120)
    already monitors "committed but unpushed" as its own standing health
    signal, so a push failure here (no network, no configured upstream)
    never undoes the commit that already cleared the live config this
    host reads, and is reported in ``detail`` rather than flipping
    ``applied`` to ``False``.

    Never raises: every refusal — an unresolvable path, an ambiguous or
    already-cleared token/command, a failed ``git add``/``commit`` — comes
    back as ``applied=False`` with an explanatory ``detail``, the same
    "report, never crash the caller" posture every other sweep write in
    this module takes.
    """
    from pathlib import Path  # noqa: PLC0415

    try:
        real_path = Path(config_path).expanduser().resolve()
    except OSError as exc:
        return ConfigDriftApplyResult(
            applied=False,
            detail=f"could not resolve {config_path!r}: {type(exc).__name__}: {exc}",
        )
    if not real_path.exists():
        return ConfigDriftApplyResult(
            applied=False,
            detail=f"{config_path} does not resolve to an existing file — nothing to patch",
        )

    repo_dir = real_path.parent

    # ── Guard: refuse onto an already-dirty tracked file (#4048 review) ──
    # A human mid-edit of the live symlink has an uncommitted change
    # sitting in exactly this file; `read_text()`/`write_text()` below
    # would silently fold that in-progress edit into this function's own
    # commit. Check BEFORE reading/writing anything.
    dirty_ok, dirty_out = _config_drift_git(
        repo_dir, "status", "--porcelain", "--", str(real_path),
    )
    if not dirty_ok:
        return ConfigDriftApplyResult(
            applied=False,
            detail=(
                f"{repo_name}#{issue_number}: could not check whether "
                f"{real_path} is already dirty: {dirty_out}"
            ),
        )
    if dirty_out.strip():
        return ConfigDriftApplyResult(
            applied=False,
            detail=(
                f"{repo_name}#{issue_number}: refusing — {real_path} "
                f"already has an uncommitted change ({dirty_out.strip()!r}); "
                "a human may be mid-edit and this would overwrite it rather "
                "than commit a clean one-token removal"
            ),
        )

    # ── Guard: refuse onto a checkout behind its upstream (#4048 review) ─
    # Mirrors `coord.commands.repo._guard_settings_fresh`: fetch first
    # (the failure mode is "the remote moved and nobody here noticed"),
    # then compare HEAD to `@{upstream}`. Unknown (no upstream, no
    # network) is never a refusal — only a CONFIRMED nonzero `behind`
    # count is, since committing onto that base makes the later `git
    # push` fail non-fast-forward while this function still reports
    # `applied=True`.
    _config_drift_git(repo_dir, "fetch", "--quiet")
    upstream_ok, upstream_out = _config_drift_git(
        repo_dir, "rev-parse", "--abbrev-ref", "@{upstream}",
    )
    if upstream_ok and upstream_out.strip():
        upstream = upstream_out.strip()
        counts_ok, counts_out = _config_drift_git(
            repo_dir, "rev-list", "--left-right", "--count", f"{upstream}...HEAD",
        )
        if counts_ok:
            parts = counts_out.split()
            if len(parts) == 2:
                try:
                    behind = int(parts[0])
                except ValueError:
                    behind = 0
                if behind:
                    return ConfigDriftApplyResult(
                        applied=False,
                        detail=(
                            f"{repo_name}#{issue_number}: refusing — "
                            f"{repo_dir} is {behind} commit(s) behind "
                            f"{upstream}; committing onto a stale checkout "
                            "would diverge from the fleet's canonical "
                            f"config. Run `git -C {repo_dir} pull --ff-only` "
                            "first."
                        ),
                    )

    text = real_path.read_text()
    token_count = text.count(token)
    if token_count != 1:
        return ConfigDriftApplyResult(
            applied=False,
            detail=(
                f"{repo_name}#{issue_number}: `{token}` appears "
                f"{token_count} time(s) in {real_path} — refusing an "
                "ambiguous or already-cleared edit"
            ),
        )
    command_count = text.count(command)
    if command_count != 1:
        return ConfigDriftApplyResult(
            applied=False,
            detail=(
                f"{repo_name}#{issue_number}: the command naming `{token}` "
                f"does not appear exactly once in {real_path} "
                f"({command_count} match(es)) — refusing to guess which "
                "occurrence to edit"
            ),
        )

    new_text = text.replace(command, patched_command, 1)
    real_path.write_text(new_text)

    ok, detail = _config_drift_git(repo_dir, "add", "--", str(real_path))
    if not ok:
        return ConfigDriftApplyResult(
            applied=False,
            detail=(
                f"{repo_name}#{issue_number}: edited {real_path} but "
                f"`git add` failed: {detail} — the working tree is left "
                "modified for a human to commit or revert by hand"
            ),
        )
    commit_message = (
        f"board-doctor: clear config drift for {repo_name}#{issue_number} "
        f"(remove dangling `{token}`)"
    )
    ok, detail = _config_drift_git(
        repo_dir, "commit", "-m", commit_message, "--", str(real_path),
    )
    if not ok:
        return ConfigDriftApplyResult(
            applied=False,
            detail=(
                f"{repo_name}#{issue_number}: edited {real_path} but "
                f"`git commit` failed: {detail} — the working tree is "
                "left modified for a human to commit or revert by hand"
            ),
        )

    push_ok, push_detail = _config_drift_git(repo_dir, "push")
    result_detail = (
        f"{repo_name}#{issue_number}: committed the removal of `{token}` "
        f"in {real_path} ({commit_message!r})"
    )
    if not push_ok:
        result_detail += (
            f"; push failed, left for a human or the #3120 config_drift "
            f"health check to flag: {push_detail}"
        )
    return ConfigDriftApplyResult(applied=True, detail=result_detail)


def run_config_drift_fix_sweep(
    candidates: Sequence[ConfigDriftCandidate],
    *,
    config: Any,
    config_path: Any = None,
    apply_fn: Callable[..., ConfigDriftApplyResult] | None = None,
) -> list[dict]:
    """#4048: for every *candidates* entry whose repo has opted into
    ``board_doctor.config_drift_autofix`` AND whose command resolved a
    *patched_command* (``candidate.plan.enabled and candidate.plan.
    patched_command is not None`` — :func:`coord.board_doctor_policy.
    plan_config_drift_fix`'s own decision, never re-derived here):

    1. **Apply** (:func:`apply_config_drift_fix_to_coord_settings`, or
       *apply_fn* in a test) — edit the ONE matched *token* out of the
       live, symlinked ``coordinator.yml`` and commit (push best-effort).
    2. **Refund + requeue**, only once the Apply step above actually
       landed: reset the row's Test-stage verdict
       (:func:`coord.state.reset_work_test_state`, confirmed against a
       fresh :func:`coord.state.test_reset_candidates` re-read — #2096,
       never trusting the write call's own row-matched count — the same
       pattern :func:`coord.drive_queue.requeue_after_conflict_fix` uses
       for the identical "the prior verdict no longer covers what's on the
       branch now" reason), then give back *candidate.rounds_consumed*
       fix rounds (:func:`coord.board_doctor_permissions.
       sweep_raise_fixit_budget`), whose own remove-then-re-add puts the
       row back at its ORIGINAL position with its ORIGINAL ``after=``/
       machine/hold edges intact — #4048's "requeue the row at its
       position with its edges".

    A refused/failed Apply (an ambiguous or already-cleared token, a
    failed ``git`` call) or an unconfirmed Test-stage reset stops here
    with NO refund/requeue — :func:`run_sweep` only withdraws a row from
    the ordinary alert path once THIS function reports it fully
    ``requeued``, so a failed attempt leaves the row's convergence
    counter and any live escalation untouched rather than silently
    dismissing them (#4036 review: "pulling these rows out... makes their
    escalation vanish").

    Every candidate *candidates* names whose opt-in/patch precondition
    does not hold (``plan.enabled`` is ``False``, or no *patched_command*
    was computed) is skipped entirely — no dict reported for it, no call
    made — so this function only ever reports on the subset it actually
    attempted.

    Returns one dict per candidate attempted — ``{"repo", "issue",
    "applied", "requeued", "refunded", "detail"}`` — reported onto
    :attr:`SweepReport.config_drift_fix` rather than :attr:`SweepReport.
    decisions`, the same "not a tier/auto_dispatchers/convergence
    decision" shape *followthrough*/*refile*/*conflict_fix_ceiling_pending*
    already use.
    """
    from coord.board_doctor_permissions import (  # noqa: PLC0415
        sweep_raise_fixit_budget,
    )
    from coord.config import resolve_config_path  # noqa: PLC0415
    from coord.state import (  # noqa: PLC0415
        reset_work_test_state,
        test_reset_candidates,
    )

    apply_impl = apply_fn or apply_config_drift_fix_to_coord_settings
    resolved_config_path = (
        config_path if config_path is not None else resolve_config_path()
    )

    results: list[dict] = []
    for candidate in candidates:
        if not (candidate.plan.enabled and candidate.plan.patched_command is not None):
            continue
        repo_name = candidate.repo_name
        issue_number = candidate.issue_number

        outcome = apply_impl(
            config_path=resolved_config_path,
            command=candidate.command,
            token=candidate.token,
            patched_command=candidate.plan.patched_command,
            repo_name=repo_name,
            issue_number=issue_number,
        )
        requeued = False
        refunded = False
        detail = outcome.detail
        if outcome.applied:
            reset_work_test_state(repo_name, issue_number)
            still_set = [
                f"{aid} ({typ})"
                for aid, typ, test_state in test_reset_candidates(
                    repo_name, issue_number,
                )
                if test_state is not None
            ]
            if still_set:
                detail = (
                    f"{outcome.detail}; the pre-fix Test-stage verdict "
                    "could not be confirmed cleared on re-read for "
                    f"{', '.join(still_set)} — leaving the fix-round "
                    "budget and the queue row untouched for a later sweep"
                )
            else:
                budget_outcome = sweep_raise_fixit_budget(
                    repo_name, issue_number, config,
                    increment=max(candidate.rounds_consumed, 1),
                )
                requeued = bool(budget_outcome.get("raised"))
                refunded = requeued
                detail = (
                    f"{outcome.detail}; Test-stage verdict cleared; "
                    f"{budget_outcome.get('detail', '')}"
                )
        results.append({
            "repo": repo_name,
            "issue": issue_number,
            "applied": bool(outcome.applied),
            "requeued": requeued,
            "refunded": refunded,
            "detail": detail,
        })
    return results


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
    `_fixit_rebudget_environmental` (`coord/board_doctor_policy.py`)
    already applies to its own environmental-vs-flailing decision (#3847),
    so the two stay consistent (#2096: one question — "is this evidence
    fresh enough to act on?" — one answer, `StuckRow.fresh`).
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


def _reclassify_checks_stale_race(rows: list[StuckRow], *, config) -> list[StuckRow]:
    """Relabel checks-stale-race-but-live-clear rows (#3859).

    Runs strictly AFTER :func:`queue_diagnose.diagnose` has already picked a
    cause from live evidence alone, exactly like :func:`_reclassify_ceiling_
    blocked` — this never feeds the entry's own stated reason into that
    classifier (#2235's invariant); it only reads the drive-queue row's
    OWN stored state/reason afterward, through
    :func:`coord.drive_queue.checks_stale_race_giveup_for_issue`, to ask
    whether a row this sweep would otherwise leave classified
    `CAUSE_NOTHING_BLOCKING` (:data:`_LIVE_CLEAR_CAUSES` — the same set
    :func:`_reclassify_ceiling_blocked` reads, a row whose live gate reads
    clear) actually lost its last life to the quadraui#1961 sibling-merge
    race rather than a real problem.

    Same freshness/confidence bar as :func:`_reclassify_ceiling_blocked`
    (:attr:`StuckRow.fresh`, confidence above
    :data:`coord.queue_diagnose.CONFIDENCE_LOW`) and the same reason: a row
    :func:`_episode_stuck_row` rebuilt from a stale stored verdict, or a
    probe `queue_diagnose` itself flagged as incomplete, is not fresh enough
    evidence to reclassify — the review-approved/Test-passed/mergeable
    reading this reclassifier promotes on must be confirmed THIS sweep, not
    inherited from whenever the episode last concluded.

    Gated per-repo by :func:`coord.board_doctor_policy._stale_race_tier_
    enabled` — the same fleet switch + per-repo switch (default `True`)
    `coord.commands.drive_queue._run_auto_revalidate_checks_stale` checks
    before reserving a merge-gate slot. A repo that has explicitly opted
    out keeps its pre-#3859 label (whatever `queue_diagnose` assigned)
    unchanged; this label is purely informational today (`policy_tier_of`
    has no entry for `CAUSE_CHECKS_STALE_RACE`, so it resolves to the same
    alert tier either way), but it must not claim a race-driven disposition
    is in play on a repo where that disposition cannot actually fire.
    """
    from coord.board_doctor_policy import _stale_race_tier_enabled  # noqa: PLC0415
    from coord.drive_queue import (  # noqa: PLC0415
        checks_stale_race_giveup_for_issue,
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
            if (
                parsed is not None
                and _stale_race_tier_enabled(parsed[0], config)
                and checks_stale_race_giveup_for_issue(*parsed)
            ):
                row = replace(
                    row,
                    cause=CAUSE_CHECKS_STALE_RACE,
                    evidence=row.evidence
                    + (
                        "drive_queue: the owning drive session exhausted its "
                        "merge-attempt budget losing a race against sibling "
                        "merges while live reads clear (#3859)",
                    ),
                )
        reclassified.append(row)
    return reclassified


def _reclassify_stale_latch(
    rows: list[StuckRow], merge_entries: Sequence[Any], *, board: Board, config
) -> list[StuckRow]:
    """Relabel a stale `HUMAN_REQUIRED` merge-queue latch whose approval is
    already confirmed bound to the branch's current content (#3887).

    Runs strictly AFTER :func:`merge_queue_rows` has already picked
    ``CAUSE_GATE_BLOCKED`` from the entry's own live ``state``/``error``
    alone, the same "classify first, reclassify a narrow confirmed subset
    second" shape :func:`_reclassify_ceiling_blocked` uses. The extra,
    independent confirmation here is `coord.merge_queue.has_approved_review`
    — quadraui#2034's shape: a latch is only WORTH an auto-``clear_latch``
    attempt once the approval on file is already known to cover the live
    branch content; a latch whose approval is stale for an unrelated
    reason gets no promotion and stays at the alert tier, same as it
    always has.

    Deliberately excludes ``CAUSE_MERGE_CONFLICT`` (#3887): that
    cause means ``QueuedMerge.state == "conflict"`` — GitHub's own
    ``mergeable is False`` reading, live evidence already in hand — and
    `has_approved_review` alone (approval bound to content) says nothing
    about whether that specific conflict has since cleared. Promoting it
    anyway would relabel a genuinely still-conflicting row to this
    mechanical cause, which routes to `clear_latch`'s own live-mergeability
    re-test (a confirmed no-op for a real conflict) instead of the #3769
    investigate lane `queue_diagnose.CAUSE_MERGE_CONFLICT` would otherwise
    reach — losing the conflict-fix dispatch that cause exists to trigger,
    in favor of three silent no-ops and a bare escalation. A genuine
    ``HUMAN_REQUIRED`` latch has no such live counter-evidence to override:
    it is a cached decision from whenever the retry cap or a permission
    gate fired, which `has_approved_review` plus `clear_latch`'s own
    `coord.merge_queue.reconcile_conflict_entries` re-test are exactly
    positioned to re-open.

    This promotion is deliberately NOT "the latch will clear" — the third,
    live-mergeability gate inside `coord.board_doctor_permissions.
    sweep_clear_latch` (via `coord.merge_queue.reconcile_conflict_entries`)
    is what actually decides that, and no-ops harmlessly when it still
    refuses. Promoting on the approval check alone, rather than
    pre-computing full eligibility here too, keeps the live-mergeability
    re-test as the ONE place that question is answered (#2096).
    """
    from coord.merge_queue import has_approved_review  # noqa: PLC0415

    entries_by_key = {
        f"{entry.repo_name}#{entry.issue_number}": entry for entry in merge_entries
    }
    reclassified: list[StuckRow] = []
    for row in rows:
        if (
            row.queue == "merge-queue"
            and row.fresh
            and row.cause == queue_diagnose.CAUSE_GATE_BLOCKED
        ):
            entry = entries_by_key.get(row.key)
            if entry is not None and has_approved_review(entry, board, config=config):
                row = replace(
                    row,
                    cause=CAUSE_LATCH_STALE_MERGEABLE,
                    evidence=row.evidence
                    + (
                        "merge_queue: approval is bound to the current branch "
                        "content — worth re-testing live mergeability (#3887)",
                    ),
                )
        reclassified.append(row)
    return reclassified


def after_prereqs_landed_for_entry(
    entry: Any,
    entries_by_key: Mapping[str, Any],
    board: Board,
    board_view: Any = None,
) -> bool:
    """Does *entry* match #3901's "frozen block cause no longer holds"
    shape — the ONE predicate both :func:`_reclassify_after_prereqs_landed`
    (classification) and `coord.board_doctor_policy`'s pre-dispatch live
    re-check call, rather than two independently-maintained copies of the
    same question (#2096).

    *board* is the `coord.models.Board` `collect_rows` already built this
    sweep (`coord.state.build_board`) — the one thing `coord.gates.
    assignments_for_issue` reads. *board_view* is `coord.drive_queue.
    BoardView` — the SAME typed wrapper `_resolve_prereqs`/`diagnose_
    blocked_after` call `.facts(key)` on — kept a SEPARATE, optional
    parameter rather than derived from *board* here: building one is its
    own `GET /board` round trip this predicate must not silently repeat
    per row, so a caller that could not build one (an advisory failure,
    same posture :func:`collect_rows`'s own no-progress pass already takes)
    passes ``None`` and this predicate degrades to trusting only
    *entries_by_key*'s `coord.drive_queue.STATE_DONE_LIKE` reading for a
    dep still queued — never a dep that left the queue after landing,
    exactly the gap the next bullet's second check exists to close when a
    `board_view` IS on hand.

    True only when ALL of:

    * *entry*'s state is `blocked`/`parked` (`coord.drive_queue.
      STATE_BLOCKED`/`STATE_PARKED`) — a row an operator already resumed
      (back to `waiting`) no longer matches, which is exactly what makes
      this callable safely as the live re-check too;
    * *entry* declares a non-empty `after=` and is NOT a permanent block
      (`coord.drive_queue.is_permanent_block_reason`);
    * `entry.last_reason` is still exactly `coord.drive_queue._resolve_
      prereqs`'s own unsatisfiable-`after=` verdict text
      (`coord.drive_queue.is_unsatisfiable_prereq_reason`) — the recorded
      block cause names ONLY its `after=` graph, never an unrelated reason
      this predicate has no business overriding;
    * every named pre-req now reads terminal — its OWN drive-queue row (if
      still queued, per *entries_by_key*) is in `coord.drive_queue.
      STATE_DONE_LIKE`, or (when *board_view* is given) `board_view.
      facts(dep).landed` is `True` — the SAME two positive-evidence checks
      `_resolve_prereqs` itself treats as "satisfied; continue" (#2096
      again: not a second derivation of what "landed" means);
    * the issue was actually DISPATCHED at least once
      (`coord.gates.assignments_for_issue` — the same existing primitive
      :func:`coord.board_doctor_policy._never_dispatched_still_true`
      already answers this question with) — the #3874
      `CAUSE_NEVER_DISPATCHED` -> `drive_queue_requeue` wiring already
      covers the NEVER-dispatched shape (`coord.drive_queue.
      is_unreachable_wait_shape`, which unconditionally excludes this
      entry's own `after=` reason); this predicate is deliberately the
      complement of that one, not a second path to the same place.
    """
    from coord.drive_queue import (  # noqa: PLC0415
        STATE_BLOCKED,
        STATE_DONE_LIKE,
        STATE_PARKED,
        is_permanent_block_reason,
        is_unsatisfiable_prereq_reason,
        parse_key as _parse_row_key,
    )
    from coord.gates import assignments_for_issue  # noqa: PLC0415

    if entry.state not in (STATE_BLOCKED, STATE_PARKED):
        return False
    if not entry.after or is_permanent_block_reason(entry.last_reason):
        return False
    if not is_unsatisfiable_prereq_reason(entry.last_reason):
        return False

    def _dep_landed(dep: str) -> bool:
        dep_entry = entries_by_key.get(dep)
        if dep_entry is not None and dep_entry.state in STATE_DONE_LIKE:
            return True
        if board_view is None:
            return False
        return board_view.facts(dep).landed

    if not all(_dep_landed(dep) for dep in entry.after):
        return False

    parsed = _parse_row_key(entry.key)
    return parsed is not None and bool(assignments_for_issue(board, *parsed))


def _reclassify_after_prereqs_landed(
    rows: list[StuckRow],
    entries_by_key: Mapping[str, Any],
    *,
    board: Board,
    board_view: Any = None,
) -> list[StuckRow]:
    """Relabel a frozen `blocked`/`parked` drive-queue row whose `after=`
    block cause no longer holds (#3901, #3887 case 4).

    Runs strictly AFTER :func:`queue_diagnose.diagnose`/:func:`no_progress_
    rows` have already picked a cause from live evidence alone, same
    "classify first, reclassify a narrow confirmed subset second" shape
    every other reclassifier in this module uses. Promotes a row to
    :data:`CAUSE_AFTER_PREREQS_LANDED` only for a row this sweep classified
    fresh (:attr:`StuckRow.fresh`) — never a stale episode reconstruction
    (:func:`_episode_stuck_row`), whose `entry.last_reason` could be
    arbitrarily old — whose incoming cause is one of
    :data:`_AFTER_PREREQS_LANDED_SAFE_CAUSES` (#3901 review: the three
    sibling reclassifiers in this module all require a specific incoming
    cause — `_reclassify_ceiling_blocked`/`_reclassify_checks_stale_race`
    require `_LIVE_CLEAR_CAUSES` plus confidence above
    :data:`queue_diagnose.CONFIDENCE_LOW`, `_reclassify_stale_latch`
    requires exactly `queue_diagnose.CAUSE_GATE_BLOCKED` — this is the same
    shape, never "promote regardless of what the live diagnosis already
    found"), and whose live drive-queue entry (*entries_by_key*, loaded
    fresh by :func:`collect_rows` this same sweep) satisfies
    :func:`after_prereqs_landed_for_entry`.

    `coord.drive_queue._reconcile_blocked_after` resumes a LIVE `blocked`
    row matching this exact shape to `waiting` in place on every ordinary
    tick, no board-doctor involvement at all — this reclassifier (and the
    `drive_queue_requeue` dispatch it feeds, same mechanical action
    :data:`CAUSE_NEVER_DISPATCHED`/:data:`CAUSE_CEILING_BLOCKED_MERGEABLE`
    already use) only ever matters for a row the ordinary tick's own walk
    is not still reaching — e.g. one #3888's `no_progress_rows` had to
    rescue from a budget-exhausted, silently-dropped episode. Never a
    competing second implementation of `_reconcile_blocked_after`'s own
    resume — this reclassifies, the SAME mechanical `drive_queue_requeue`
    action every other row on this allowlist dispatches, re-confirmed live
    immediately before it fires (see `coord.board_doctor_policy.
    _requeue_after_prereqs_landed`).
    """
    reclassified: list[StuckRow] = []
    for row in rows:
        if (
            row.queue == "drive-queue"
            and row.fresh
            and row.cause in _AFTER_PREREQS_LANDED_SAFE_CAUSES
        ):
            entry = entries_by_key.get(row.key)
            if entry is not None and after_prereqs_landed_for_entry(
                entry, entries_by_key, board, board_view
            ):
                row = replace(
                    row,
                    cause=CAUSE_AFTER_PREREQS_LANDED,
                    evidence=row.evidence
                    + (
                        f"drive_queue: every --after prereq "
                        f"({', '.join(entry.after)}) now reads terminal while "
                        "the stored block reason still names only them "
                        "(#3901)",
                    ),
                )
        reclassified.append(row)
    return reclassified


#: #4003: the complement of :data:`CAUSE_AFTER_PREREQS_LANDED` — a `blocked`/
#: `parked` row whose `after=` pre-req is NOT every-dep-landed but is
#: instead itself stuck (`blocked`/`failed` in the queue right now). Before
#: this cause existed, a row this shape fell straight through to
#: ``queue_diagnose.CAUSE_UNKNOWN`` (nothing else names it) and the
#: investigate tier then tried a fix-it leg against THIS row's own,
#: already-completed work — the claude-coordinator#4002 shape in the #4003
#: incident: "has no originating work assignment on the board" was the
#: wrong diagnosis for a row that was never missing a leg, only waiting on
#: a different one. Never auto-dispatched — see this cause's absence from
#: every :mod:`coord.board_doctor_policy` registration list (ALLOWLIST,
#: AUTO_DISPATCHERS, INVESTIGATE_CAUSES, DECOMPOSITION_CAUSES): fixing THIS
#: row means fixing the NAMED pre-req, not this one, so ``policy_tier_of``
#: falls through to the alert tier by construction, the same as
#: :data:`CAUSE_STRANDED_PREREQ_BLOCKED`.
CAUSE_BLOCKED_ON_PREREQ = "blocked-on-prereq"


def blocked_on_prereq_dep_for_entry(
    entry: Any, entries_by_key: Mapping[str, Any],
) -> str | None:
    """The ``after=`` pre-req responsible for *entry* staying permanently
    blocked, when at least one such pre-req is ITSELF stuck (#4003) — the
    complement of :func:`after_prereqs_landed_for_entry`'s "every pre-req
    now reads terminal" check: this looks for one that is neither terminal
    nor landed, but is sitting `blocked`/`failed` in the queue right now.

    Returns ``None`` when *entry* does not match the shape at all (not
    `blocked`/`parked`, no ``after=``, a permanent block unrelated to its
    `after=` graph, or every named pre-req reads terminal/landed instead —
    see :func:`after_prereqs_landed_for_entry`, the shape this is the
    deliberate complement of), or when it matches but none of the named
    pre-reqs is itself in a locally-queued `blocked`/`failed` state (e.g.
    it is simply still `waiting`/`running`, or it left the queue entirely —
    neither is this shape).
    """
    from coord.drive_queue import (  # noqa: PLC0415
        STATE_BLOCKED,
        STATE_FAILED,
        STATE_PARKED,
        is_permanent_block_reason,
        is_unsatisfiable_prereq_reason,
    )

    if entry.state not in (STATE_BLOCKED, STATE_PARKED):
        return None
    if not entry.after or is_permanent_block_reason(entry.last_reason):
        return None
    if not is_unsatisfiable_prereq_reason(entry.last_reason):
        return None
    for dep in entry.after:
        dep_entry = entries_by_key.get(dep)
        if dep_entry is not None and dep_entry.state in (STATE_BLOCKED, STATE_FAILED):
            return dep
    return None


def _reclassify_blocked_on_prereq(
    rows: list[StuckRow], entries_by_key: Mapping[str, Any],
) -> list[StuckRow]:
    """Relabel a `blocked`/`parked` drive-queue row whose own ``after=``
    pre-req is itself stuck, rather than merely not-yet-landed (#4003).

    Runs strictly AFTER :func:`_reclassify_after_prereqs_landed` (same
    "classify first, reclassify a narrow confirmed subset second" shape
    every reclassifier in this module uses) and over the SAME incoming
    cause set (:data:`_AFTER_PREREQS_LANDED_SAFE_CAUSES`) — the two
    reclassifiers answer mutually exclusive verdicts for the same question
    ("does this row's `after=` graph, which is all `queue_diagnose` could
    ever say about it, still explain the block"), so whichever runs second
    only ever sees rows the first one declined to promote.

    Before this reclassifier existed, a row matching this shape stayed at
    ``queue_diagnose.CAUSE_UNKNOWN`` — registered on the investigate tier
    — and the sweep tried a fix-it leg against THIS row's own (already-
    completed) work, which fixes nothing: the real blocker is the NAMED
    pre-req, not this issue. :data:`CAUSE_BLOCKED_ON_PREREQ` is
    deliberately unregistered in every :mod:`coord.board_doctor_policy`
    tier list, so it falls to the alert tier by construction and names
    the pre-req instead of attempting a fix-it leg against the wrong row
    (claude-coordinator#4002 in the #4003 incident).
    """
    reclassified: list[StuckRow] = []
    for row in rows:
        if (
            row.queue == "drive-queue"
            and row.fresh
            and row.cause in _AFTER_PREREQS_LANDED_SAFE_CAUSES
        ):
            entry = entries_by_key.get(row.key)
            dep = (
                blocked_on_prereq_dep_for_entry(entry, entries_by_key)
                if entry is not None
                else None
            )
            if dep is not None:
                dep_entry = entries_by_key.get(dep)
                dep_state = dep_entry.state if dep_entry is not None else "unknown"
                row = replace(
                    row,
                    cause=CAUSE_BLOCKED_ON_PREREQ,
                    evidence=row.evidence
                    + (
                        f"drive_queue: this row's own --after prereq ({dep}) "
                        f"is itself {dep_state} — fixing this row means "
                        f"fixing {dep}, not dispatching another leg here "
                        "(#4003)",
                    ),
                )
        reclassified.append(row)
    return reclassified


def _reclassify_branch_merged_guard(
    rows: list[StuckRow], entries_by_key: Mapping[str, Any],
) -> list[StuckRow]:
    """Relabel ANY `blocked` drive-queue row whose entry's own
    `last_reason` names the #3376 branch-merged guard specifically (#4038),
    regardless of what cause the episode walk above already settled on.

    Unlike every other `_reclassify_*` pass in this module, this does not
    restrict itself to a narrow "safe" incoming cause set and does not
    require `row.fresh`: `coord.dispatch_liveness.
    is_branch_merged_guard_refusal_reason`'s marker is a deterministic,
    permanent fact about the row (the guard refuses the exact same
    dispatch again on every retry — see the marker's own docstring), not a
    live probe reading that can go stale between when an episode settled
    and now — `entries_by_key` is this sweep's own fresh
    `list_drive_queue()` read, not the row's possibly-stale stored
    evidence — so there is no narrower cause set to protect and no
    freshness bar to clear: whatever the episode walk concluded, THIS
    marker always wins.
    """
    from coord.dispatch_liveness import (  # noqa: PLC0415
        is_branch_merged_guard_refusal_reason,
    )

    reclassified: list[StuckRow] = []
    for row in rows:
        if row.queue == "drive-queue" and row.cause != CAUSE_BRANCH_MERGED_GUARD:
            entry = entries_by_key.get(row.key)
            if entry is not None and is_branch_merged_guard_refusal_reason(
                entry.last_reason
            ):
                row = replace(
                    row,
                    cause=CAUSE_BRANCH_MERGED_GUARD,
                    evidence=row.evidence + (
                        "drive_queue: last_reason names the #3376 "
                        f"branch-merged guard specifically (#4038) — "
                        f"{entry.last_reason}",
                    ),
                )
        reclassified.append(row)
    return reclassified


# ── #4037: drop stale predicted `after=` edges ──────────────────────────────
#
# #2247's predictor chains a newcomer `--after` any in-flight work it
# collides with on files — correct the moment it is written, but the edge is
# never revisited once the target stops being reachable soon: deferred and
# dequeued (#2537's shape — the target's own row is simply gone), or stuck
# itself behind something else entirely unrelated to the newcomer (#2534's
# shape — a whole CellGrid/MCP chain sitting behind one blocked root). Both
# left the newcomer waiting on `coord drive-queue list`'s "(open, not
# queued)" or `cause=blocked-on-prereq tier=alert` forever, with nothing
# auto-acting on either. This sweep is the fix: drop a PREDICTED edge (never
# an operator's `--after` or an epic's `{after:}` — see
# :func:`coord.overlap_predict.predicted_after_targets`, the provenance
# seam) whose target cannot land soon. Self-correction needs no new code
# here: the moment the dropped target re-enters the queue (or anything else
# does, on top of it), the SAME predictor chains against its then-current
# footprint — a real branch diff once it has launched — exactly as every
# other `add` call already does.


def _after_chain_hits_blocked_or_parked(
    entry: Any, entries_by_key: Mapping[str, Any], *, _seen: set[str] | None = None,
) -> bool:
    """Does *entry*'s OWN ``after=`` graph transitively reach a row sitting
    `blocked`/`parked` right now (#2534's shape) — never *entry* itself,
    only what it is chained behind. ``_seen`` guards a cycle (malformed data;
    `coord.drive_queue.find_cycle` already refuses to write one, but this
    walk must still terminate against a row some other path let through).
    """
    from coord.drive_queue import STATE_BLOCKED, STATE_PARKED  # noqa: PLC0415

    seen = _seen if _seen is not None else set()
    for dep_key in entry.after:
        if dep_key in seen:
            continue
        seen.add(dep_key)
        dep = entries_by_key.get(dep_key)
        if dep is None:
            continue
        if dep.state in (STATE_BLOCKED, STATE_PARKED):
            return True
        if _after_chain_hits_blocked_or_parked(dep, entries_by_key, _seen=seen):
            return True
    return False


def predicted_after_target_drop_reason(
    target_key: str,
    entries_by_key: Mapping[str, Any],
    *,
    config: Any = None,
    issue_open: Callable[[str, int], bool] | None = None,
) -> str:
    """Why a PREDICTED ``after=`` edge pointed at *target_key* should be
    dropped, or ``""`` when the target still looks reachable soon enough
    that the edge is worth keeping (#4037).

    Only ever called for an edge :func:`drop_stale_predicted_after_edges`
    has already confirmed came from the predictor, never an operator's own
    ``--after`` or an epic's ``{after:}`` — this predicate has no opinion on
    provenance, only on the target's own reachability. Four shapes qualify,
    the exact set the issue's own evidence (#2537, #2534) landed in:

    * the target left the queue entirely — ``entries_by_key`` has no row
      for it — while its issue is STILL OPEN on GitHub: dequeued, or never
      queued at all (#2537's "waiting on #N (open, not queued)" shape). A
      target that is instead closed/merged is `_resolve_prereqs`'s own
      satisfied case, not a stale edge — left alone here on purpose, so
      this sweep never races the ordinary tick's own resume of it;
    * the target itself reads ``blocked``;
    * the target itself reads ``parked``;
    * the target is ``waiting``/``running`` right now, but its OWN
      ``after=`` graph transitively reaches a `blocked`/`parked` row
      (#2534's shape — the dependent is fine, everything IT depends on is
      not).

    Deliberately NOT a fifth, standalone shape for "the target has simply
    never launched yet": the predictor's own declared-vs-declared half
    (``coord.overlap_predict.declared_footprints``) exists precisely for
    two rows that both have no branch yet and share declared files —
    dropping on launch status alone would delete that exact edge before
    either row starts, which is the one case the predictor exists to
    serialize. An unlaunched target is only droppable through the
    blocked-chain shape above: an unstarted target chained behind a
    blocked/parked root is genuinely stuck; an unstarted target with
    nothing blocking it may simply be next in line.

    *config*, when given, resolves *target_key*'s repo to the GitHub
    ``owner/repo`` slug (:func:`_github_slug`) before calling *issue_open*
    for the dequeued-and-open check — the local `coordinator.yml` repo
    name is not a slug `gh` accepts. ``None`` (a bare test double, or a
    repo whose local name already IS its slug) leaves the name unresolved.

    *issue_open* defaults to :func:`_issue_open` (a live `gh` read); tests
    inject a stub so this stays a pure-logic call over hand-built entries.
    """
    from coord.drive_queue import STATE_BLOCKED, STATE_PARKED  # noqa: PLC0415
    from coord.drive_queue import parse_key as _parse_row_key  # noqa: PLC0415

    target = entries_by_key.get(target_key)
    if target is None:
        parsed = _parse_row_key(target_key)
        opener = issue_open or _issue_open
        if parsed is not None:
            repo_name, issue_number = parsed
            if opener(_github_slug(repo_name, config), issue_number):
                return "dequeued"
        return ""
    if target.state == STATE_BLOCKED:
        return "blocked"
    if target.state == STATE_PARKED:
        return "parked"
    if _after_chain_hits_blocked_or_parked(target, entries_by_key):
        return "blocked chain"
    return ""


def drop_stale_predicted_after_edges(
    entries: Sequence[Any],
    *,
    predicted_keys_for: Callable[[Any], frozenset[str]],
    config: Any = None,
    issue_open: Callable[[str, int], bool] | None = None,
    write_after: Callable[[Any, Sequence[str]], None] | None = None,
    record_drop: Callable[[Any, Sequence[tuple[str, str]]], None] | None = None,
) -> list[str]:
    """Drop every PREDICTED ``after=`` edge pointed at a row
    :func:`predicted_after_target_drop_reason` says cannot land soon (#4037).

    *predicted_keys_for* is the provenance seam: ``entry ->`` the subset of
    *entry*'s OWN ``after=`` the predictor actually wrote (see
    :func:`coord.overlap_predict.predicted_after_targets`). An edge not in
    that set is operator- or epic-declared and is NEVER a candidate here,
    whatever its target looks like — this sweep only ever touches edges
    #2247 itself produced.

    *config* is forwarded to :func:`predicted_after_target_drop_reason` to
    resolve the GitHub slug for the dequeued-and-open check.

    *write_after*, when given, is called once per CHANGED entry with its
    surviving ``after=`` tuple (``coord.drive_queue.write_queue_after`` in
    production). *record_drop*, when given, is called once per CHANGED
    entry with its ``(dep_key, reason)`` drops, so the drop lands somewhere
    durable on the row itself rather than only in this call's own return
    value (``coord.drive_queue.record_predicted_after_drop`` in
    production). Both default to ``None``, which computes the drop set
    without writing or recording anything — the shape a dry run, or a test
    asserting only on the returned notes, wants.

    Returns one note per dropped edge — ``"{entry.key}: edge dropped:
    {dep} (predicted, target {reason})"``, the issue's own acceptance text
    — in entry/edge order, so a caller can log or alert on them directly.
    """
    entries_by_key = {e.key: e for e in entries}
    notes: list[str] = []
    for entry in entries:
        if not entry.after:
            continue
        predicted = predicted_keys_for(entry)
        if not predicted:
            continue
        keep: list[str] = []
        drops: list[tuple[str, str]] = []
        for dep_key in entry.after:
            reason = (
                predicted_after_target_drop_reason(
                    dep_key, entries_by_key, config=config, issue_open=issue_open,
                )
                if dep_key in predicted
                else ""
            )
            if reason:
                drops.append((dep_key, reason))
            else:
                keep.append(dep_key)
        if not drops:
            continue
        if write_after is not None:
            write_after(entry, keep)
        if record_drop is not None:
            record_drop(entry, drops)
        for dep_key, reason in drops:
            notes.append(
                f"{entry.key}: edge dropped: {dep_key} (predicted, target {reason})"
            )
    return notes


def sweep_predicted_after_edges(
    *, dry_run: bool = False, config: Any = None,
) -> list[str]:
    """Production entry point for :func:`drop_stale_predicted_after_edges`
    (#4037): read the live drive queue, drop every stale predicted
    ``after=`` edge, and report what changed.

    *config* resolves the GitHub slug for each target's dequeued-and-open
    check (see :func:`predicted_after_target_drop_reason`) — forwarded
    from :func:`run_sweep`'s own *config*, never defaulted to a live read
    here, since there is no board-doctor-wide config to fall back to
    outside a caller that already has one.

    *dry_run* computes the drop set without writing it back — the same
    "classify, never mutate" contract :func:`sweep`'s own *dry_run* keeps.
    Advisory the same way every other optional pass in :func:`run_sweep` is:
    a caller that cannot reach the queue or the audit log this round skips
    this sweep, not the rest of the report.
    """
    from coord.drive_queue import (  # noqa: PLC0415
        entries_from_rows,
        record_predicted_after_drop,
        write_queue_after,
    )
    from coord.overlap_predict import predicted_after_targets  # noqa: PLC0415
    from coord.state import list_drive_queue  # noqa: PLC0415

    entries = entries_from_rows(list_drive_queue())
    return drop_stale_predicted_after_edges(
        entries,
        predicted_keys_for=lambda e: predicted_after_targets(e.repo, e.issue),
        config=config,
        write_after=None if dry_run else write_queue_after,
        record_drop=None if dry_run else record_predicted_after_drop,
    )


#: #3929 (#3918 gap 1, second half): a merge-queue entry that has NEVER been
#: attempted at all (``entry.last_attempt is None``) is invisible to both
#: :func:`merge_queue_rows` (its own #3918-review carve-out: a `pending`
#: entry is only ever labelled stuck there via LAST-ATTEMPT staleness,
#: deliberately never off an unset `last_attempt` — see that function's
#: docstring for why) and :data:`_MERGE_QUEUE_STUCK_CAUSE` (same reason).
#: quadraui#2160: a row whose test and review both read fresh and whose
#: merge-queue row sits `PENDING` with no attempt recorded escalated or
#: idled toward the 240-minute deadline instead of this sweep just running
#: the one merge attempt that unblocks it. Minted ONLY by
#: :func:`_reclassify_merge_never_attempted`, from a row that reclassifier
#: independently re-confirms LIVE via `coord.merge_queue.entry_gate_status`
#: — the SAME re-confirmation :func:`_confirm_merge_pending_stuck` already
#: trusts for the STALE-pending shape — never from the bare PENDING/
#: unset-`last_attempt` shape alone.
CAUSE_MERGE_NEVER_ATTEMPTED = "merge-never-attempted"

#: The incoming `queue_diagnose` causes :func:`_reclassify_merge_never_
#: attempted` may promote away from. Starts from
#: :data:`_AFTER_PREREQS_LANDED_SAFE_CAUSES` (`unknown`/`no-pr`: the live
#: PR/branch-lifecycle diagnosis found nothing actionable of its own) and
#: widens it with `queue_diagnose.CAUSE_NOTHING_BLOCKING` — quadraui#2160's
#: own row has an open PR with every check green, review approved and
#: `mergeable` true, which `queue_diagnose._rule_nothing_blocking` reads as
#: `nothing-blocking`, not `unknown`/`no-pr`. A SEPARATE constant from
#: `_AFTER_PREREQS_LANDED_SAFE_CAUSES` rather than widening that one in
#: place: `_reclassify_after_prereqs_landed` promotes to a `drive_queue_
#: requeue`, a completely different action with its own, already-settled
#: #3901 scope — this cause's own live `entry_gate_status` re-check below
#: is the independent confirmation that makes `CAUSE_NOTHING_BLOCKING`
#: safe to add HERE without touching that one.
_MERGE_NEVER_ATTEMPTED_SAFE_CAUSES = _AFTER_PREREQS_LANDED_SAFE_CAUSES | frozenset(
    {queue_diagnose.CAUSE_NOTHING_BLOCKING}
)


def _reclassify_merge_never_attempted(
    rows: list[StuckRow],
    merge_entries: Sequence[Any],
    *,
    board: Board,
    config,
    now: float | None = None,
    stale_after_seconds: float | None = None,
) -> list[StuckRow]:
    """#3929: promote a no-progress drive-queue row to
    :data:`CAUSE_MERGE_NEVER_ATTEMPTED` when its merge-queue row is
    `PENDING` with ``last_attempt is None`` (never evaluated at all), has
    sat at least :data:`MERGE_PENDING_STALE_AFTER_SECONDS` since
    ``enqueued_at``, AND a live re-check confirms every gate ahead of the
    merge attempt itself already reads clear.

    The ``enqueued_at`` age bound mirrors :func:`merge_queue_rows`'s own
    staleness filter for the STALE-pending shape (same threshold, same
    reasoning): a `PENDING` entry with no attempt recorded yet that was
    JUST enqueued is, per that function's own docstring, "very likely
    still alive and polling" under the ordinary auto-drain tick — minting
    this cause (and the `coord merge --only` it dispatches) off a
    freshly-enqueued entry would race that tick rather than rescue a row
    it has genuinely stopped reaching. An entry with no ``enqueued_at`` at
    all (pre-#274 rows, hand-edited queue state) is treated as already
    past the bound — the same fail-open convention every other optional
    ``QueuedMerge`` anchor in this module uses — rather than permanently
    exempted from this cause.

    Same "promote only an independently-confirmed subset of a broader
    cause" shape as :func:`_reclassify_after_prereqs_landed` — the incoming
    cause must be one of :data:`_MERGE_NEVER_ATTEMPTED_SAFE_CAUSES`
    (`unknown`/`no-pr`: the live PR/branch-lifecycle diagnosis found
    nothing actionable of its own, which is exactly what a row stuck ONLY
    because `coord merge` never got to it looks like; `nothing-blocking`:
    the live diagnosis found an open PR with every check green, review
    approved and `mergeable` true — quadraui#2160's own shape — #2096, the
    same incoming-cause precondition, not a second independently-maintained
    judgment call under a new name), and :func:`coord.merge_queue.entry_gate_status`
    — the SAME function :func:`_confirm_merge_pending_stuck` already trusts
    for the STALE-pending shape — must read `PLAN_READY` right now. A row
    whose review/smoke genuinely is not ready yet reads `PLAN_BLOCKED` here
    and is left exactly as the caller already classified it, so a row that
    is not actually ready for a merge attempt is never mis-labelled as one
    that is.

    Also requires :attr:`StuckRow.fresh` — the SAME guard `_reclassify_
    after_prereqs_landed`/`_reclassify_ceiling_blocked`/`_reclassify_checks_
    stale_race` all apply, and for the same reason: a row rebuilt by
    `_episode_stuck_row` from an arbitrarily old stored `cause=unknown`
    carries no live evidence of its own, so promoting IT off a merge-queue
    row that merely happens to still read `PENDING`/`last_attempt is None`
    right now would be reasoning from a snapshot this sweep never actually
    re-diagnosed.

    *now* defaults to :func:`time.time` — overridable so a replay test can
    assert on a fixed, reproducible staleness window, same convention as
    :func:`merge_queue_rows`.

    *stale_after_seconds* (#4027) overrides :data:`MERGE_PENDING_STALE_AFTER_SECONDS`
    for this call only — same rationale and same caller
    (:func:`run_sweep`'s ``mechanical_causes``-filtered call) as
    :func:`merge_queue_rows`'s own override.
    """
    import time as _time  # noqa: PLC0415

    from coord import github_ops  # noqa: PLC0415
    from coord.ci_store import build_ci_store  # noqa: PLC0415
    from coord.merge_queue import PENDING, PLAN_READY, entry_gate_status  # noqa: PLC0415

    moment = _time.time() if now is None else now
    stale_after = (
        MERGE_PENDING_STALE_AFTER_SECONDS if stale_after_seconds is None else stale_after_seconds
    )
    entries_by_key = {
        f"{entry.repo_name}#{entry.issue_number}": entry for entry in merge_entries
    }
    out: list[StuckRow] = []
    for row in rows:
        if (
            row.queue != "drive-queue"
            or not row.fresh
            or row.cause not in _MERGE_NEVER_ATTEMPTED_SAFE_CAUSES
        ):
            out.append(row)
            continue
        entry = entries_by_key.get(row.key)
        if (
            entry is None
            or entry.state != PENDING
            or entry.last_attempt is not None
            or (
                entry.enqueued_at is not None
                and moment - entry.enqueued_at < stale_after
            )
        ):
            out.append(row)
            continue
        live_ready = False
        try:
            ci_cfg = config.ci_store
            ci_store = build_ci_store(
                ci_cfg.type or "github", host=ci_cfg.host, token_env=ci_cfg.token_env,
            )
            status, _reason = entry_gate_status(entry, board, config, ci_store, github_ops)
            live_ready = status == PLAN_READY
        except Exception:  # noqa: BLE001 — best-effort live confirm; see docstring
            _log.debug(
                "board-doctor: merge-never-attempted live confirm failed for %s",
                row.key, exc_info=True,
            )
            live_ready = False
        if live_ready:
            row = replace(
                row,
                cause=CAUSE_MERGE_NEVER_ATTEMPTED,
                confidence=queue_diagnose.CONFIDENCE_HIGH,
                evidence=row.evidence
                + (
                    f"merge_queue.state={entry.state} last_attempt=None — the "
                    "merge gate has never actually been evaluated at all; a "
                    "live re-check confirms every gate ahead of the merge "
                    "attempt itself reads clear (#3929)",
                ),
            )
        out.append(row)
    return out


#: #3888: the three drive-queue shapes that can sit making no progress while
#: carrying neither a `block_log` episode (only `blocked`/`parked`
#: transitions ever open one — :data:`coord.block_log.STALL_STATES`) nor a
#: merge-queue row, so :func:`collect_rows`'s episode/merge-queue walk above
#: never looks at them at all: #3875's "(D) coverage classification for rows
#: with no gate to read". ``_NO_PROGRESS_BLOCKED`` additionally covers the
#: narrower bug in the same ask — a `blocked`/`parked` row DOES open an
#: episode, but :func:`_episode_stuck_row` silently drops one whose stored
#: cause is still :data:`queue_diagnose.CAUSE_UNKNOWN` (budget-exhausted or
#: not yet diagnosed), so it never reaches :func:`sweep` either. These
#: string constants are evidence labels only — never branched on by a
#: caller outside this module.
_NO_PROGRESS_RUNNING_DEAD = "running-no-session-no-activity"
#: #3918: a FOURTH shape — a `running` row whose tmux session is alive (so
#: the dead-leg shape above never matches) but whose own drive loop has
#: published :func:`coord.drive._publish_stall_nudge`'s record of "no state
#: change, nothing active" for longer than this sweep's own
#: ``no_progress_minutes`` threshold (quadraui#2160: the pane printed `!!
#: no state change in 20m … 40m … 161m (nothing active)` for over two
#: hours while the doctor wrote nothing, because a live session alone was
#: treated as proof of progress). Reuses the SAME persisted record
#: `coord.notifier.collect.worker_probes` already reads to answer the
#: identical question for its own `CONDITION_STALL_NUDGED` alert
#: (#2096) — never a second, independently-derived "is this stalled"
#: signal — rather than re-deriving staleness from `coord drive`'s raw
#: pane output here.
_NO_PROGRESS_RUNNING_IDLE = "running-live-no-state-change"
_NO_PROGRESS_BLOCKED = "blocked"
_NO_PROGRESS_WAITING_READY = "waiting-ready-not-launched"


def _resolve_no_progress_concurrency(
    entries: Sequence[Any],
    board_view: Any,
    *,
    config,
    systemd_flags: Mapping[str, int] | None,
    now: float | None,
    local_host: str | None,
):
    """The one capacity verdict the no-progress ``waiting`` shape reads —
    repo-level AND fleet-level ceilings, each with its own current
    occupancy — resolved through :func:`coord.drive_queue.
    resolve_concurrency_report`, the SAME entry point `coord config
    --effective` and the real tick's own ``ConcurrencyReport`` both already
    go through (#3428, #2096 "one question, one answer"), rather than this
    module hand-deriving the resolution order a second time. Replaces the
    former `_repo_running_counts`/`_repo_parallel_ceiling` pair, which only
    ever answered the repo-level half of this question.

    *systemd_flags* is ``{}`` (no override known) when the caller gives
    ``None`` — never this function reading the filesystem itself, so a test
    can call this without touching a real ``~/.config/systemd/user/``.
    :func:`collect_rows` reads :func:`coord.drive_queue.
    read_systemd_max_parallel_flags` (which now also sees any drop-in under
    that unit's own ``.d/`` directory) once per sweep and threads the
    result down, the same posture it already has for *local_host*.
    """
    from coord.drive_queue import resolve_concurrency_report  # noqa: PLC0415

    return resolve_concurrency_report(
        repo_overrides={repo.name: repo.max_parallel for repo in config.repos},
        pipeline_max_parallel=config.pipeline.max_parallel,
        pipeline_max_parallel_per_repo=config.pipeline.max_parallel_per_repo,
        max_workers_cap=config.concurrency.max_workers,
        systemd_flags=systemd_flags or {},
        entries=entries,
        board=board_view,
        now=now,
        local_host=local_host,
    )


def _repo_parallel_ceiling(
    repo_name: str, *, config, systemd_flags: Mapping[str, int]
) -> int:
    """*repo_name*'s effective ``max_parallel_per_repo`` ceiling, with no
    occupancy question attached — a thin single-repo convenience over
    :func:`coord.drive_queue.resolve_concurrency_report` (the same shared
    entry point :func:`_resolve_no_progress_concurrency` uses) for a caller
    that only wants to check one repo's ceiling, e.g. a unit test asserting
    a systemd override wins over `coordinator.yml`. ``0`` means uncapped,
    mirroring that resolution's own meaning. *systemd_flags* is required —
    a caller must say explicitly whether it is asking "with the daemon
    host's real override" or "with none known" rather than the answer
    silently depending on an unstated default.
    """
    from coord.drive_queue import resolve_concurrency_report  # noqa: PLC0415

    report = resolve_concurrency_report(
        repo_overrides={repo.name: repo.max_parallel for repo in config.repos},
        pipeline_max_parallel=config.pipeline.max_parallel,
        pipeline_max_parallel_per_repo=config.pipeline.max_parallel_per_repo,
        max_workers_cap=config.concurrency.max_workers,
        systemd_flags=systemd_flags,
    )
    resolution = report.repo_resolutions.get(repo_name, report.max_parallel_per_repo)
    return resolution.value


def _render_ceiling(resolution: Any) -> str:
    """Render one :class:`coord.drive_queue.CeilingResolution` for a
    no-progress evidence line — the bare number, or ``uncapped`` for the
    ``0`` value both `max_parallel_per_repo` and `max_parallel` document as
    "no limit" (:mod:`coord.config`'s own validation permits ``0`` there).
    Without this, an uncapped repo's evidence read as e.g. "1/0 occupied",
    which looks like a repo already over its limit rather than one with no
    limit at all.
    """
    return "uncapped" if resolution.value == 0 else str(resolution.value)


def _fresh_stall_nudge(
    nudge: Mapping[str, Any] | None,
    *,
    now: float,
    launched_at: float | None,
    ttl_seconds: float,
) -> Mapping[str, Any] | None:
    """*nudge* only if it still plausibly describes the entry's current session.

    A stall-nudge record carries no link back to any particular drive
    session and `load_state` applies no expiry of its own (pruning only
    happens inside `save_state`, which the record's own retraction path
    does not pass through) — so, read in isolation, a record can describe
    a session that already ended. A record is accepted here only if it is
    both recent (within *ttl_seconds*) and dated on or after *launched_at*,
    the start of the session it is being read against; either check
    failing means the record predates (or has outlived) the thing it would
    be used to explain, so it is treated the same as no record at all.
    """
    if not nudge:
        return None
    at = nudge.get("at")
    if at is None:
        return None
    try:
        at = float(at)
    except (TypeError, ValueError):
        return None
    if now - at > ttl_seconds:
        return None
    if launched_at is not None and at < launched_at:
        return None
    return nudge


def _no_progress_shape(
    entry: Any,
    *,
    live_sessions: Collection[str],
    facts_of: Callable[[str], Any],
    repo_ceiling: Callable[[str], int],
    running_counts: Mapping[str, int],
    fleet_ceiling: int,
    fleet_occupied: int,
    dep_states: Mapping[str, str],
    local_host: str | None,
    now: float = 0.0,
    stall_nudge_of: Callable[[str, int], Mapping[str, Any] | None] = lambda repo, issue: None,
    prereq_truth_of: Callable[[str], "EntryTruth | None"] = lambda dep: None,
) -> str | None:
    """Which of the #3888/#3918 no-progress shapes *entry* matches, or
    ``None``.

    ``running``, dead-leg: launched on *this* host — a foreign-launched row
    (:func:`coord.queue_diagnose.is_foreign_launch`) is never judged dead
    from here, same guard :meth:`coord.queue_diagnose.GhLiveProbe._foreign`
    and :func:`coord.drive_queue._reconcile_running`'s own #1870 branch
    already apply, because an absent LOCAL tmux session proves nothing
    about a session another machine launched — AND no live tmux session
    (*live_sessions*, the same local read :func:`collect_rows` already
    takes) AND no non-terminal work-like assignment on the board
    (``facts_of(entry.key).active_work`` — :attr:`coord.drive_queue.
    IssueFacts.active_work`, the one existing "is there live work in
    flight" answer, never re-derived here) — the quadraui#2038 shape: a row
    the queue still calls ``running`` whose real work already finished (by
    hand or otherwise) with nobody telling the queue.

    ``running``, live-but-idle (#3918): gated on the SAME ``no non-terminal
    work-like assignment on the board`` check the dead-leg shape above
    uses — the issue's own condition names a `running` row with "nothing
    active", and an idle nudge alone must never override a live,
    non-terminal assignment the board still has recorded for this row
    (the ordinary "making real progress" case). Past that guard: the
    session IS alive (so the dead-leg check above never matches), but
    *stall_nudge_of* — a thin wrapper over
    :func:`coord.notifier.store.nudge_for`, the SAME persisted record the
    notifier's own stall-survived-its-nudge condition reads, filtered
    through :func:`_fresh_stall_nudge` so a record from an earlier
    session (predating this row's own ``launched_at``) or one that has
    simply outlived :data:`coord.notifier.store.NUDGE_TTL_SECS` is never
    read as describing the CURRENT session — reports a ``stalled_for``
    duration for this row's ``(repo, issue)``. `coord.drive.Driver._loop`
    publishes that record every time its own ``--stall``-driven nudge
    fires (#1593), and clears it the instant the board fingerprint it
    describes moves — so a record present here means the OWNING drive
    session itself has observed no pipeline state change for that long,
    not a guess this function makes from session liveness alone. A
    session being alive proves the TMUX PROCESS is up; it says nothing
    about whether the pipeline inside it is moving (quadraui#2160: test
    passed, then the pane printed `!! no state change in 20m … 40m … 161m
    (nothing active)` for over two hours with the doctor never writing a
    row for it, because the dead-leg check above — the only `running`
    check that existed — stops at "the session is alive").
    *_no_progress_age_seconds* reads the SAME record (passed down by the
    caller) rather than this function's own ``launched_at``-based age for
    this one shape, since "idle since when" is this record's own
    ``stalled_for``, not how long ago the session happened to launch.

    ``blocked``/``parked``: always a candidate — unlike the other shapes
    this one needs no extra evidence to be WORTH a fresh look, since
    sitting blocked already means nothing is progressing; the N-minute
    grace window (applied by the caller) is what keeps a merely-just-blocked
    row out, not this predicate.

    ``waiting``: every ``after=`` pre-req is landed, decided per-dep by
    :func:`_dep_landed` (#3936) with the SAME ordering and the SAME two
    functions :func:`coord.drive_queue._resolve_prereqs` uses for the real
    tick's own ``after=`` resolution (#2096: one question, one answer) —
    every board-level positive signal checked FIRST (:attr:`coord.
    drive_queue.IssueFacts.landed`, OR ``already_done and not reopened``
    (#3931), OR the dep's OWN drive-queue row already reading
    :data:`coord.drive_queue.STATE_DONE_LIKE` in *dep_states* — like
    `_resolve_prereqs` itself (#2715), since the ``issues`` cache backing
    ``landed`` can run minutes behind a merge the queue performed itself),
    and only once none of those fire does a live :class:`~coord.
    entry_truth.EntryTruth` from *prereq_truth_of* get a say, via
    :func:`coord.drive_queue._truth_is_done` — so a pre-req the queue
    itself already confirmed `done` this tick, or whose cached `landed`
    fact already reads `True`, is never shadowed by a live truth read
    whose OWN probe happens to fail against that same pre-req. A dep with
    no live truth supplied, or whose truth has nothing decisive to say
    (:data:`~coord.entry_truth.NEXT_ACTION_UNKNOWN`, or an inconclusive
    probe-failure :data:`~coord.entry_truth.NEXT_ACTION_ESCALATE` — see
    :attr:`~coord.entry_truth.EntryTruth.escalate_probe_failure` and
    :func:`coord.drive_queue._truth_is_done`'s own docstring), simply
    reads as not landed by this check — AND the repo has room under its
    own ceiling right now, per *running_counts* (the scheduler's own
    occupancy verdict) — AND the FLEET has room too, per *fleet_occupied*
    against *fleet_ceiling*. Both capacity answers come from the same
    ``ConcurrencyReport`` :func:`_resolve_no_progress_concurrency` resolves,
    the one the real tick's own ``plan_tick`` enforces at both levels: a
    `waiting` row at `repo` capacity and one at the FLEET's own
    ``--max-parallel`` ceiling are the identical "queue working as
    intended" shape, just at different ceilings — ``plan_tick``'s own
    global-capacity early return only refreshes a `waiting` row's prereq
    text (never writes a capacity reason into it), so a row deferred there
    is just as easy to misread as `cause=unknown` without this check as the
    repo-level one was. A ``waiting`` row failing any of the three checks
    is correctly explained already (still waiting on a dep, or waiting its
    turn for capacity at either level) — this only flags the quadraui#2047
    shape, a row the scheduler should have launched by now and, for
    whatever reason, did not.
    """
    from coord.drive_queue import (  # noqa: PLC0415
        STATE_BLOCKED,
        STATE_DONE_LIKE,
        STATE_PARKED,
        STATE_RUNNING,
        STATE_WAITING,
    )

    if entry.state == STATE_RUNNING:
        if queue_diagnose.is_foreign_launch(entry, local_host):
            return None
        if facts_of(entry.key).active_work:
            return None
        if entry.session_name and entry.session_name in live_sessions:
            from coord.notifier.store import NUDGE_TTL_SECS  # noqa: PLC0415

            nudge = _fresh_stall_nudge(
                stall_nudge_of(entry.repo, entry.issue),
                now=now,
                launched_at=entry.launched_at,
                ttl_seconds=NUDGE_TTL_SECS,
            )
            if nudge is not None and nudge.get("stalled_for") is not None:
                return _NO_PROGRESS_RUNNING_IDLE
            return None
        return _NO_PROGRESS_RUNNING_DEAD
    if entry.state in (STATE_BLOCKED, STATE_PARKED):
        return _NO_PROGRESS_BLOCKED
    if entry.state == STATE_WAITING:
        from coord.drive_queue import _truth_is_done  # noqa: PLC0415

        def _dep_landed(dep: str) -> bool:
            facts = facts_of(dep)
            if facts.landed or (facts.already_done and not facts.reopened):
                return True
            if dep_states.get(dep) in STATE_DONE_LIKE:
                return True
            return _truth_is_done(prereq_truth_of(dep))

        if not all(_dep_landed(dep) for dep in entry.after):
            return None
        ceiling = repo_ceiling(entry.repo)
        if ceiling > 0 and running_counts.get(entry.repo, 0) >= ceiling:
            return None
        if fleet_ceiling > 0 and fleet_occupied >= fleet_ceiling:
            return None
        return _NO_PROGRESS_WAITING_READY
    return None


def _no_progress_age_seconds(
    entry: Any, *, now: float, stall_nudge: Mapping[str, Any] | None = None,
) -> float | None:
    """How long *entry* has sat in its current shape, or ``None`` if unknown.

    ``running``/live-but-idle (#3918): when *stall_nudge* is given (the
    SAME record :func:`_no_progress_shape` consulted to mint
    :data:`_NO_PROGRESS_RUNNING_IDLE` for this entry), its own
    ``stalled_for`` IS the age — "idle since when" is a duration the
    drive's own stall nudge already computed, not a timestamp this
    function would have to subtract from *now* itself. ``launched_at``
    (when THIS session started) would silently understate a long-running,
    long-healthy session that only went idle recently, or overstate one
    that went idle almost immediately — neither is "how long has nothing
    changed", which is the only question this shape asks.

    ``running``, otherwise, ages off ``launched_at``; every other state
    ages off ``reason_at`` (#2133's "when was this text last true" stamp).
    ``None`` — a row predating either column, or one whose field was never
    written — is deliberately never treated as "just now" (the same rule
    :class:`coord.drive_queue.QueueEntry` documents for ``reason_at``
    itself): the caller treats an unknown age as past the grace window
    rather than silently exempting it.

    #3888 review: ``reason_at`` is re-stamped on every ``last_reason``
    write, not only on a real state change (:class:`coord.drive_queue.
    QueueEntry`'s own #2273 comment) — a ``blocked`` row whose per-tick
    re-derived reason text wiggles (a gate listing different check names,
    a shifting backoff suffix) keeps resetting this clock and can stay
    under ``no_progress_minutes`` indefinitely, the exact "moving target"
    defect #2273 already named and fixed for the RETRY backoff window by
    switching that ONE caller onto its own dedicated, write-once stamp
    (``QueueEntry.retry_backoff_at`` — never touched again until the next
    death) instead of ``reason_at``. This pass is accepted to carry the
    same risk for now rather than grow a fourth stamp: giving every
    no-progress shape its own write-once anchor the way #2273 did for
    retry backoff is a follow-up, not required for this issue's three
    shapes to stop being silently invisible.
    """
    from coord.drive_queue import STATE_RUNNING  # noqa: PLC0415

    if (
        entry.state == STATE_RUNNING
        and stall_nudge is not None
        and stall_nudge.get("stalled_for") is not None
    ):
        try:
            return max(0.0, float(stall_nudge["stalled_for"]))
        except (TypeError, ValueError):
            pass
    stamp = entry.launched_at if entry.state == STATE_RUNNING else entry.reason_at
    if stamp is None:
        return None
    return max(0.0, now - stamp)


#: #3888 review nit: unlike :func:`coord.queue_diagnose.needs_diagnosis`'s
#: per-episode budget, this pass has no cap of its own — the first sweep
#: after deploy could otherwise fan out one `gh` probe per legacy
#: `blocked`/`parked` row all at once, the exact synchronized-poll shape
#: #3536 already had to guard against elsewhere in this fleet. A row that
#: matches a shape but loses the race for this budget is still returned
#: (never silently dropped, #3888's whole point) — just as `unknown`, with
#: evidence saying so, so the NEXT sweep (which re-derives this budget from
#: scratch) gets first claim on probing it.
_NO_PROGRESS_PROBE_CAP = 25


def no_progress_rows(
    entries: Sequence[Any],
    *,
    covered_keys: Collection[str],
    live_sessions: Collection[str],
    board_view: Any,
    probe: queue_diagnose.LiveProbe,
    config,
    trigger: str = "board-doctor-sweep",
    now: float | None = None,
    local_host: str | None = None,
    systemd_flags: Mapping[str, int] | None = None,
    prereq_truth: Mapping[str, "EntryTruth"] | None = None,
) -> list[StuckRow]:
    """Classify every drive-queue row #3888 says the episode walk misses.

    *prereq_truth* (#3936) is ``{dep_key: EntryTruth}`` for whichever
    ``after=`` pre-reqs the caller already reconciled LIVE this sweep —
    threaded straight to :func:`_no_progress_shape`'s own ``waiting`` dep
    check, exactly as :func:`coord.drive_queue._resolve_prereqs` consults
    the same mapping for the real tick (#2096). ``None`` (the default)
    reproduces today's behaviour exactly: every dep falls through to the
    board/queue-state reasoning `_no_progress_shape` already had.

    :func:`collect_rows`'s episode-based walk only ever looks at keys
    `block_log` opened an episode for — exclusively `blocked`/`parked`
    transitions (:data:`coord.block_log.STALL_STATES`) — plus whatever
    :func:`merge_queue_rows` covers. A `running` row whose work finished out
    of band, one whose drive session is alive but reports no state change
    (#3918), or a `waiting` row the scheduler should have launched, carries
    none of that, so it never reached this sweep's output at all (#3875 ask
    D). This walks the FULL current *entries* instead and, for every one NOT
    already in *covered_keys* (so a row the episode walk already explained
    is never double-classified or double-alerted), asks :func:`_no_progress_
    shape` whether it matches one of the four no-progress shapes and
    :func:`_no_progress_age_seconds` whether it has sat there past
    ``config.board_doctor.no_progress_minutes`` (#2096: one threshold, read
    from one place).

    *board_view* is the same :class:`coord.drive_queue.BoardView`
    :func:`collect_rows` already builds for its own probe — its
    ``.facts(key)`` is this pass's ONLY "is this issue landed / is there
    live work" reading, and it is also handed to
    :func:`_resolve_no_progress_concurrency` to answer "is there a free
    repo slot, and a free fleet slot" with the scheduler's own verdict
    rather than a second count. *local_host* gates the `running` shape the
    same way :func:`coord.queue_diagnose.is_foreign_launch` gates every
    other liveness call in this fleet: a row launched on a different host
    is never judged dead from an absent LOCAL tmux session.

    A matching, aged-out row is diagnosed through the SAME `probe` +
    :func:`queue_diagnose.diagnose` pair every other fresh row in this
    module uses — never a second classifier — so a row that DOES still have
    live PR/CI evidence gets a real cause, and only a genuinely thin picture
    falls through to :data:`queue_diagnose.CAUSE_UNKNOWN`. Bounded by
    :data:`_NO_PROGRESS_PROBE_CAP` probes per call — see its own docstring.

    `unknown` is returned as an ordinary :class:`StuckRow`, never dropped —
    the #3888 fix for :func:`_episode_stuck_row`'s silent abstention-skip:
    `sweep`'s default `tier_of` already routes any cause with no
    :mod:`coord.board_doctor_policy` entry (`unknown` included) to the
    alert tier, so returning the row here is sufficient to make it alert;
    nothing downstream needs to special-case the cause name.

    *systemd_flags* is ``{}`` (no override known) unless the caller
    supplies the real read; :func:`collect_rows` reads the daemon host's
    installed unit (and any drop-in under its own ``.d/`` directory — see
    :func:`coord.drive_queue.read_systemd_max_parallel_flags`) once per
    sweep and passes it down here, so both the repo and the FLEET ceiling
    read the SAME override the real tick would.
    """
    stamp = _utcnow().timestamp() if now is None else now
    threshold_seconds = config.board_doctor.no_progress_minutes * 60
    facts_of = board_view.facts
    dep_states = {entry.key: entry.state for entry in entries}
    # #3918: one notifier-state read for the whole sweep (not one per
    # entry) — `coord.notifier.store.nudge_for` is a pure dict lookup once
    # the state is loaded, same posture `collect_rows`' own single
    # `live_sessions`/`systemd_flags` reads already take. Advisory: a
    # notifier whose state file is unreadable degrades to "no nudges
    # known" (every entry reads as not-idle from this signal) rather than
    # failing the whole no-progress pass — the dead-leg/blocked/waiting
    # shapes above are unaffected either way.
    from coord.notifier.store import NUDGE_TTL_SECS as _NUDGE_TTL_SECS  # noqa: PLC0415

    try:
        from coord.notifier.store import load_state as _load_notifier_state
        from coord.notifier.store import nudge_for as _nudge_for

        _notifier_state = _load_notifier_state()
    except Exception:  # noqa: BLE001 — advisory channel, see comment above
        _log.debug(
            "board-doctor: could not read notifier state for the "
            "running-idle no-progress shape this sweep", exc_info=True,
        )
        _notifier_state = None

    def stall_nudge_of(repo_name: str, issue_number: int) -> Mapping[str, Any] | None:
        if _notifier_state is None:
            return None
        return _nudge_for(_notifier_state, repo_name, issue_number)
    concurrency = _resolve_no_progress_concurrency(
        entries,
        board_view,
        config=config,
        systemd_flags=systemd_flags,
        now=stamp,
        local_host=local_host,
    )
    running_counts = concurrency.repo_occupied
    fleet_ceiling = concurrency.max_parallel.value
    fleet_occupied = concurrency.occupied

    def repo_ceiling(repo_name: str) -> int:
        resolution = concurrency.repo_resolutions.get(
            repo_name, concurrency.max_parallel_per_repo
        )
        return resolution.value

    rows: list[StuckRow] = []
    probes_spent = 0
    for entry in entries:
        if entry.key in covered_keys:
            continue
        shape = _no_progress_shape(
            entry,
            live_sessions=live_sessions,
            facts_of=facts_of,
            repo_ceiling=repo_ceiling,
            running_counts=running_counts,
            fleet_ceiling=fleet_ceiling,
            fleet_occupied=fleet_occupied,
            dep_states=dep_states,
            local_host=local_host,
            now=stamp,
            stall_nudge_of=stall_nudge_of,
            prereq_truth_of=(lambda dep: (prereq_truth or {}).get(dep)),
        )
        if shape is None:
            continue
        nudge = (
            _fresh_stall_nudge(
                stall_nudge_of(entry.repo, entry.issue),
                now=stamp,
                launched_at=entry.launched_at,
                ttl_seconds=_NUDGE_TTL_SECS,
            )
            if shape == _NO_PROGRESS_RUNNING_IDLE
            else None
        )
        age = _no_progress_age_seconds(entry, now=stamp, stall_nudge=nudge)
        if age is not None and age < threshold_seconds:
            continue

        # #3888 review nit: an episode can exist for the `blocked`/`parked`
        # shape and still have been dropped by `_episode_stuck_row` (its
        # stored cause is still `CAUSE_UNKNOWN` — budget-exhausted or never
        # diagnosed); the other shapes genuinely carry no episode at all.
        # Phrasing this one sentence per shape keeps the alert from
        # pointing an operator at "no episode exists" when one does, just
        # an unresolved one.
        if shape == _NO_PROGRESS_BLOCKED:
            coverage_note = (
                "board_doctor: any block_log episode for this entry was "
                "dropped (its stored cause is still unresolved) or none "
                "was ever opened, and no merge-queue row covers it either"
            )
        elif shape == _NO_PROGRESS_RUNNING_IDLE:
            coverage_note = (
                "board_doctor: the drive session is alive but its own "
                f"stall nudge reports no pipeline state change for "
                f"{(nudge or {}).get('stalled_for', None) / 60.0:g}m "
                "(coord.notifier.store nudge record) — a live session "
                "alone is not evidence of progress"
            )
        else:
            coverage_note = (
                "board_doctor: no block_log episode or merge-queue row "
                "covers this entry"
            )

        if probes_spent >= _NO_PROGRESS_PROBE_CAP:
            cause, confidence, extra_evidence = (
                queue_diagnose.CAUSE_UNKNOWN,
                queue_diagnose.CONFIDENCE_NONE,
                (
                    f"board_doctor: this sweep's no-progress probe budget "
                    f"({_NO_PROGRESS_PROBE_CAP}) is spent — unprobed this "
                    "round, re-checked next sweep",
                ),
            )
        else:
            probes_spent += 1
            try:
                live = probe.probe(entry)
            except Exception:  # noqa: BLE001 — a probe failure must never crash the sweep
                _log.debug(
                    "board-doctor: no-progress probe failed for %s", entry.key, exc_info=True
                )
                live = None

            if live is not None:
                diagnosis = queue_diagnose.diagnose(entry, live, trigger=trigger)
                cause, confidence, extra_evidence = (
                    diagnosis.cause,
                    diagnosis.confidence,
                    diagnosis.evidence,
                )
            else:
                cause, confidence, extra_evidence = (
                    queue_diagnose.CAUSE_UNKNOWN,
                    queue_diagnose.CONFIDENCE_NONE,
                    (),
                )

        # For the `waiting-ready` shape specifically, name the exact
        # capacity numbers THIS sweep read — at BOTH levels the scheduler
        # gates on (repo ceiling and fleet-wide ceiling), each paired with
        # its resolved provenance (coordinator.yml vs. a systemd override)
        # — so a human checking an alert against the tick's own
        # `repo ... at its limit (N/M)` deferral text, or its global
        # early-return, can tell at a glance whether the two numbers
        # actually agree or this sweep read stale/incomplete evidence.
        # Every other shape has no capacity question to answer, so this is
        # never added for them.
        capacity_note = ""
        if shape == _NO_PROGRESS_WAITING_READY:
            repo_resolution = concurrency.repo_resolutions.get(
                entry.repo, concurrency.max_parallel_per_repo
            )
            fleet_resolution = concurrency.max_parallel
            capacity_note = (
                f"; repo capacity this sweep read: "
                f"{running_counts.get(entry.repo, 0)}/"
                f"{_render_ceiling(repo_resolution)} occupied "
                f"({repo_resolution.source}); fleet capacity this sweep "
                f"read: {fleet_occupied}/{_render_ceiling(fleet_resolution)} "
                f"occupied ({fleet_resolution.source})"
            )
        evidence = extra_evidence + (
            f"{coverage_note}, and it has made no progress for over "
            f"{config.board_doctor.no_progress_minutes}m ({shape}, "
            f"queue-state={entry.state}); last queue reason: "
            f"{entry.last_reason or '(none)'}{capacity_note}",
        )
        rows.append(
            StuckRow(
                queue="drive-queue",
                key=entry.key,
                state=entry.state,
                cause=cause,
                confidence=confidence,
                evidence=evidence,
                fresh=True,
            )
        )
    return rows


def collect_rows(
    *,
    config,
    trigger: str = "board-doctor-sweep",
    merge_pending_stale_after_seconds: float | None = None,
) -> list[StuckRow]:
    """The impure production walk over both queues (#3758). Thin wrapper
    over :func:`_collect_rows_with_board` for every caller that only wants
    the rows — :func:`run_sweep` wants the :class:`Board` this walk
    already built too, so it unpacks both instead of paying for a second
    :func:`coord.state.build_board` read of its own."""
    rows, _board = _collect_rows_with_board(
        config=config,
        trigger=trigger,
        merge_pending_stale_after_seconds=merge_pending_stale_after_seconds,
    )
    return rows


def _collect_rows_with_board(
    *,
    config,
    trigger: str = "board-doctor-sweep",
    merge_pending_stale_after_seconds: float | None = None,
) -> tuple[list[StuckRow], Board]:
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

    #3888 adds a THIRD pass, BEFORE the two reclassifiers below rather than
    after them (review fix): :func:`no_progress_rows` walks every current
    *entries* row, including ones neither the episode walk nor
    :func:`merge_queue_rows` ever looked at (a `running` row with no
    episode at all, a `waiting` row with nothing blocking it), and flags
    any that have sat with no progress past ``no_progress_minutes`` —
    classified through the same probe, or `unknown` when nothing fits, but
    never silently dropped. Running it first means a no-progress row that
    happens to diagnose to `nothing-blocking` or a checks-stale-race shape
    gets exactly the same second look an episode-sourced row does — one row
    shape, one answer, instead of two depending on which pass found it
    first. ``covered_keys`` is taken from the FULL row list built so far
    (both queues, not just drive-queue — :func:`merge_queue_rows` already
    covers a drive-queue dependent's merge the moment it enters that
    queue), so a row already explained by either walk is never
    double-classified or double-alerted.

    Deferred imports, same reason ``drive_queue_diagnose`` defers them: this
    module is imported by the CLI's thin registration layer, and these
    pull in `gh`-touching modules this harness's own tests must not need.

    *merge_pending_stale_after_seconds* (#4027) is forwarded, unchanged, as
    both :func:`merge_queue_rows`'s and :func:`_reclassify_merge_never_
    attempted`'s own *stale_after_seconds* override — ``None`` (every
    caller except :func:`run_sweep`'s ``mechanical_causes``-filtered one)
    keeps today's :data:`MERGE_PENDING_STALE_AFTER_SECONDS` floor.
    """
    from coord import block_log  # noqa: PLC0415
    from coord.commands.drive_queue import (  # noqa: PLC0415
        _fetch_board_view_with_retry,
        _local_host_id,
    )
    from coord.drive import list_drive_sessions  # noqa: PLC0415
    from coord.drive_queue import (  # noqa: PLC0415
        entries_from_rows,
        read_systemd_max_parallel_flags,
    )
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
    local_host = _local_host_id()
    # Read once per sweep, same posture as `local_host` above — the
    # no-progress pass's repo and fleet ceilings must read the SAME
    # systemd-unit override (main unit or drop-in; if any) the real tick
    # enforces, not a second, independent read that could land on a
    # different mtime mid-sweep.
    systemd_flags = read_systemd_max_parallel_flags()
    probe = queue_diagnose.GhLiveProbe(
        config=config,
        board=board,
        fleet_health=fleet_health,
        live_sessions=live_sessions,
        local_host=local_host,
    )
    triggers = {str(ep["key"]): trigger for ep in fresh_episodes if ep.get("key")}
    diagnoses = queue_diagnose.run_pass(
        entries, fresh_episodes, probe=probe, keys=None, triggers=triggers, limit=None
    )
    refreshed_rows, settled_episodes = _refresh_ceiling_blocked_settled_rows(
        settled_episodes, entries_by_key=entries_by_key, probe=probe, trigger=trigger
    )
    # #4039: `_episode_stuck_row` rebuilds a row from `block_log`'s stored
    # diagnosis alone — it has no way to tell a row still open on the live
    # queue from one an operator (or an auto-dispatcher) has since removed,
    # because nothing here ever writes an `EVENT_RESOLVE` record back to
    # `block_log` when a row leaves the queue by any path other than the
    # queue's own resolution bookkeeping. Gate it on the SAME live
    # `entries_by_key` read `run_pass` already uses to drop a diagnosed-
    # episode-with-no-live-row (`queue_diagnose.run_pass`'s own "left the
    # queue" branch) and `_refresh_ceiling_blocked_settled_rows` above
    # already uses for its own ceiling check — one live-presence check,
    # not three independent ones that could disagree. Without this, a
    # removed row's episode never sees `resolved` and keeps producing the
    # exact same `StuckRow` every sweep, so `sweep`'s `clear_absent_rows`
    # never observes it absent and its convergence counter/alert back-off
    # runs forever against a row that no longer exists.
    live_settled_episodes = [
        ep for ep in settled_episodes if str(ep.get("key") or "") in entries_by_key
    ]
    rows = drive_queue_rows(diagnoses)
    rows.extend(refreshed_rows)
    rows.extend(
        row for ep in live_settled_episodes if (row := _episode_stuck_row(ep)) is not None
    )
    merge_entries = load_merge_queue()
    rows.extend(
        merge_queue_rows(
            merge_entries, stale_after_seconds=merge_pending_stale_after_seconds,
        )
    )

    # #3945: a `merged-partial` row carries no open `block_log` episode (it
    # is terminal/done-like, never `blocked`/`parked`) and no merge-queue
    # entry, so neither walk above ever sees it — same "otherwise invisible
    # to this sweep" gap `stranded_prereq_rows` closes for its own shape.
    # `covered_keys` guards the same way every other additive pass here
    # does, though in practice a merged-partial row never collides with an
    # episode/merge-queue cause.
    covered_keys = frozenset(row.key for row in rows)
    rows.extend(
        row for row in merged_partial_rows(entries) if row.key not in covered_keys
    )

    # #4038: belt-and-braces alongside `_reclassify_branch_merged_guard`
    # above — a row whose `blocked` transition never opened a `block_log`
    # episode at all (or whose episode was marked resolved while the row
    # itself stayed blocked) never reaches the reclassifier, since that
    # pass only relabels a row the episode walk already produced. This
    # catches exactly that orphan, the same way `merged_partial_rows`
    # above catches its own orphan shape.
    covered_keys = frozenset(row.key for row in rows)
    rows.extend(
        row for row in branch_merged_guard_rows(entries) if row.key not in covered_keys
    )

    # #3888: the episode/merge-queue walk above only ever covers a
    # `blocked`/`parked` transition or a merge-queue row — this is the
    # three-shape sweep over the whole queue that catches everything else.
    # A read failure here is advisory-only (the same posture
    # `_fetch_board_view_with_retry`'s own docstring documents for its
    # *leg_counts* read): this pass is additive on top of an already-
    # complete sweep, so losing it for one tick must never take the rest
    # of the sweep down with it. Run BEFORE the reclassifiers below (review
    # fix) so a no-progress row gets the same second look any other fresh
    # row does.
    #
    # #3901: also the ONE `BoardView` :func:`_reclassify_after_prereqs_
    # landed` reuses for its own `.facts(dep).landed` reads below — never a
    # second `GET /board` round trip for the same sweep. ``None`` on the
    # same advisory failure this comment already documents; that
    # reclassifier degrades to trusting only still-queued deps in that case
    # (see its own docstring), never raises.
    board_view: Any = None
    try:
        board_view = _fetch_board_view_with_retry()
    except Exception:  # noqa: BLE001 — see comment above
        _log.warning(
            "board-doctor: could not build a board view for the no-progress "
            "sweep this round — skipping it, episode/merge-queue coverage "
            "is unaffected",
            exc_info=True,
        )
    else:
        covered_keys = frozenset(row.key for row in rows)
        rows.extend(
            no_progress_rows(
                entries,
                covered_keys=covered_keys,
                live_sessions=live_sessions,
                board_view=board_view,
                probe=probe,
                config=config,
                trigger=trigger,
                local_host=local_host,
                systemd_flags=systemd_flags,
            )
        )

    rows = _reclassify_ceiling_blocked(rows)
    rows = _reclassify_checks_stale_race(rows, config=config)
    rows = _reclassify_stale_latch(rows, merge_entries, board=board, config=config)
    rows = _reclassify_after_prereqs_landed(
        rows, entries_by_key, board=board, board_view=board_view,
    )
    rows = _reclassify_blocked_on_prereq(rows, entries_by_key)
    rows = _reclassify_branch_merged_guard(rows, entries_by_key)
    rows = _confirm_merge_pending_stuck(rows, merge_entries, board=board, config=config)
    rows = _reclassify_merge_never_attempted(
        rows, merge_entries, board=board, config=config,
        stale_after_seconds=merge_pending_stale_after_seconds,
    )
    rows.extend(smoke_no_verdict_rows(board))

    # #4035: a row whose Test-stage verdict names a cargo target this
    # branch's own diff deletes outright — covered_keys guards against
    # double-classifying a key `smoke_no_verdict_rows` just above (or any
    # earlier pass) already explained, same discipline as every other
    # additive pass in this function.
    covered_keys = frozenset(row.key for row in rows)
    rows.extend(
        row for row in config_drift_rows(board, config) if row.key not in covered_keys
    )

    # #4045: a row whose LIVE gates already name a dispatchable
    # `next_action` that nothing has performed — run strictly BEFORE
    # `stranded_prereq_rows` below, so `covered_keys` gives this cause
    # precedence over `CAUSE_STRANDED_PREREQ_BLOCKED` for the one row shape
    # the two can collide on (see `next_action_unperformed_rows`'s own
    # docstring): a `waiting` row whose stored "waiting on X (open, not
    # queued)" text is stale because every named pre-req has since landed,
    # while this row's OWN gates read a live, dispatchable `next_action`.
    covered_keys = frozenset(row.key for row in rows)

    def _next_action_truth_of(entry: Any) -> "EntryTruth | None":
        from coord import github_ops  # noqa: PLC0415

        return queue_diagnose.live_entry_truth(
            entry, board, config, gh_ops=github_ops,
        )

    rows.extend(
        next_action_unperformed_rows(
            entries,
            covered_keys=covered_keys,
            truth_of=_next_action_truth_of,
            board_view=board_view,
        )
    )

    # #3944: a `waiting` row stranded on an open, unqueued `after=` pre-req
    # carries neither a `block_log` episode nor merge-queue evidence, so
    # nothing above this line ever classifies it — see
    # `stranded_prereq_rows`'s own docstring. `covered_keys` guards against
    # double-classifying a key some other pass already explained, same
    # discipline as the no-progress pass above (though in practice no
    # other pass here ever produces this exact shape).
    covered_keys = frozenset(row.key for row in rows)
    rows.extend(
        row for row in stranded_prereq_rows(entries, config=config)
        if row.key not in covered_keys
    )

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

    # A live read over *entries*, cross-referenced against every row this
    # walk has classified so far — independent of where in the walk it
    # lands, since it only reads *rows*, never mutates an existing one.
    rows.extend(idle_fleet_root_rows(entries, rows, blocked_streaks=blocked_streaks))
    return rows, board


def _board_doctor_other_resolutions() -> dict[str, list[float]]:
    """``key`` -> every ``resolved_at`` timestamp this host's
    :mod:`coord.block_log` records with evidence SOMETHING OTHER THAN this
    doctor's own confirmed action resolved it (#3889, widened by review).

    Two kinds of evidence qualify, either one on its own:

    * ``human_acted=True`` — an operator ``remove``, an ``add`` that
      requeued a stalled row, or a ``coord merge --override-human-required``
      that cleared a latched entry (all three via :func:`coord.commands.
      drive_queue._record_operator_release_entry`), a logged intervention,
      or a Gate-A sign-off. All of these run through :mod:`coord.block_log`'s
      own command surface and are already flagged there.
    * ``resolution == "landed"`` — :mod:`coord.block_log`'s
      ``_LANDED_CAUSE``, which its own docstring already distinguishes from
      ``_AUTO_MERGED_CAUSE``: "the entry sat here and something ELSE
      finished it while nobody was looking" — a human's hand merge, an
      unrelated branch landing, or ``coord reconcile-merges`` — as opposed
      to the queue's OWN tick landing the merge directly. Deliberately
      excludes ``resolution == "auto_merged"``: that is the queue's own
      tick completing the merge, which is exactly the shape of "the normal
      drive loop was carrying the doctor's own dispatched leg to
      completion" that stays credited to the doctor.

    Read ONCE per sweep (:func:`sweep`'s outcome-backfill section), not
    once per pending decision — the block log can hold weeks of episodes
    and every pending decision would otherwise re-parse it. Deliberately
    reads the WHOLE log rather than filtering by a ``since`` cutoff at the
    :func:`coord.block_log.read_events` layer: an episode's own ``enter``
    record (needed to pair with its ``resolve`` and populate
    ``resolved_at``) is almost always well before any cutoff worth passing
    here, since the row was already stuck when the doctor's decision was
    recorded — filtering it away would silently drop the very resolution
    this exists to find.

    Best-effort: an unreadable block log (missing file, corrupt line) reads
    as "no evidence of human action anywhere" — this must never block a
    sweep's outcome backfill, only decide which string an outcome gets.
    """
    try:
        from coord.block_log import episodes, read_events  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return {}
    try:
        items = episodes(read_events())
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, list[float]] = {}
    for item in items:
        if not item.get("human_acted") and item.get("resolution") != "landed":
            continue
        resolved_at = item.get("resolved_at")
        if resolved_at is None:
            continue
        try:
            out.setdefault(str(item.get("key") or ""), []).append(float(resolved_at))
        except (TypeError, ValueError):
            continue
    return out


def _board_doctor_attributed_to_other(
    other_resolutions: Mapping[str, Sequence[float]],
    key: str,
    since_ts: float | None,
) -> bool:
    """Did an "other"-attributed :mod:`coord.block_log` resolution for
    *key* (see :func:`_board_doctor_other_resolutions`) land at/after
    *since_ts* (#3889)?

    *since_ts* is the PAST decision's own ``recorded_at`` — ``None`` (an
    unparseable timestamp) reads as "no evidence either way" rather than
    matching everything. This answers "did an OPERATOR action resolve the
    row" (``remove``/``add``, ``log-intervention``, a Gate-A sign-off) OR
    "did something else land it while nobody was looking" (a hand merge,
    an unrelated branch, ``coord reconcile-merges``); it cannot distinguish
    either of those from "the ordinary drive loop resolved it with no
    operator action and no landed-elsewhere evidence", so a row that
    simply retried its way out on its own, with no logged human action and
    no ``landed`` resolution, still reads as the doctor's own
    `"progressed"` credit — the one case this signal cannot see is exactly
    the one #3875's wording ("the normal drive loop moved it") also cannot
    be told apart from "the doctor's own dispatched leg is what the normal
    drive loop was carrying to completion," so crediting the doctor there
    is the defensible default rather than a gap.
    """
    if since_ts is None:
        return False
    return any(ts >= since_ts for ts in other_resolutions.get(key, ()))


def _elapsed_hours_since_decision(decision: Mapping[str, Any], moment: datetime) -> float | None:
    """Hours between *decision*'s own ``recorded_at`` and *moment* (#3907).

    ``None`` when ``recorded_at`` is missing or unparseable — the caller
    treats that as "cannot confirm the grace window has elapsed" rather
    than as zero elapsed time, so a decision with a corrupt timestamp is
    held back from grading rather than graded against a fabricated
    instant.
    """
    from coord import state  # noqa: PLC0415

    ts = state.board_doctor_decision_ts(decision)
    if ts is None:
        return None
    return (moment.timestamp() - ts) / 3600.0


def _grade_open_outcomes(
    *,
    open_outcomes: Sequence[dict],
    current_by_row: Mapping[tuple[str, str], dict],
    touched: Collection[tuple[str, str]],
    other_resolutions: Mapping[str, Sequence[float]],
    moment: datetime,
    sweep_id: str,
    grace_hours: float,
    clear_absent_rows: bool = True,
) -> None:
    """Fill in / correct the outcome for every entry in *open_outcomes* —
    #3889's still-``None`` pending decisions and #3907's already-graded-
    but-revisable ``unchanged``/``regressed`` ones — against THIS sweep's
    view of their row.

    *current_by_row* maps a row :func:`sweep` actually classified this
    pass to its live ``{"cause", "escalated"}`` snapshot; a row that was
    itself skipped this pass (``suppressed``/``investigate-deferred``) is
    in *touched* but not *current_by_row*, and gets no verdict here
    either — still present, just not freshly classified, so there is
    nothing new to grade it against yet. A row key absent from *touched*
    entirely has resolved outright: unambiguous progress regardless of
    how little time has passed, which is exactly why this branch is
    never grace-gated below.

    #3907: a decision whose own ``outcome`` is still ``None`` — being
    graded for the FIRST time — is held back until *grace_hours* have
    passed since it was recorded, UNLESS its row has already resolved
    (the ``row_key not in touched`` branch, checked first). A decision
    that already carries a verdict is never grace-gated: it is being
    RE-graded, not graded for the first time, so there is nothing left
    to protect it from — only a chance to correct it.

    *clear_absent_rows* mirrors :func:`sweep`'s own parameter of the same
    name: "absent from *touched*" only means "resolved" when *rows* was
    the FULL stuck set. A caller that passed ``clear_absent_rows=False``
    has already declared *rows* a deliberately partial view — absence
    there is "not walked this pass", not "progressed" — so with it
    ``False`` the resolved branch below is skipped entirely and a row
    absent from *touched* is left untouched, exactly as its counter is
    left untouched by the matching ``clear_absent_rows`` guard around the
    tracked-row clear loop.

    The re-grade branch (``pending.get("outcome")`` already set) only
    writes when the freshly-computed outcome actually differs from the
    stored one — mirroring :func:`backfill_board_doctor_outcomes`'s own
    guard — so a sweep that keeps finding the same verdict for an
    already-graded decision does not re-``UPDATE`` (and re-``commit``)
    every row, every pass.
    """
    from coord import state  # noqa: PLC0415

    for pending in open_outcomes:
        pending_sweep_id = pending.get("sweep_id")
        if pending_sweep_id is None or pending_sweep_id == sweep_id:
            continue
        row_key = (pending.get("queue"), pending.get("key"))

        if row_key not in touched:
            if not clear_absent_rows:
                # Partial walk: absence here says nothing about whether
                # the row actually resolved, so leave the decision open
                # rather than mass-crediting (or mass-blaming) rows this
                # pass never observed.
                continue
            # Resolved: absent from the whole stuck set this pass — "the
            # leg finished" is itself the signal here, so this is never
            # held back by the grace window below.
            attributed_to_other = _board_doctor_attributed_to_other(
                other_resolutions, row_key[1], state.board_doctor_decision_ts(pending),
            )
            state.record_board_doctor_outcome(
                pending_sweep_id, row_key[0], row_key[1],
                "progressed_by_other" if attributed_to_other else "progressed",
            )
            continue

        if pending.get("outcome") is None:
            elapsed = _elapsed_hours_since_decision(pending, moment)
            if elapsed is None or elapsed < grace_hours:
                continue

        current = current_by_row.get(row_key)
        if current is None:
            continue
        attributed_to_other = _board_doctor_attributed_to_other(
            other_resolutions, row_key[1], state.board_doctor_decision_ts(pending),
        )
        outcome = state.evaluate_board_doctor_outcome(
            pending, current, attributed_to_other=attributed_to_other,
        )
        if outcome != pending.get("outcome"):
            state.record_board_doctor_outcome(pending_sweep_id, row_key[0], row_key[1], outcome)


def backfill_board_doctor_outcomes() -> int:
    """One-shot re-grade (#3907) of every PAST ``board_doctor_log``
    decision still open for grading — #3889's ``None``-outcome pending
    ones and #3907's ``unchanged``/``regressed`` ones — against the
    doctor's own CURRENT per-row state, without waiting for a fresh
    sweep to walk past those rows again.

    Reads :func:`coord.state.board_doctor_row_state`/
    ``board_doctor_tracked_rows`` rather than re-running
    :func:`collect_rows` — those are exactly the ``{"cause", "escalated"}``
    (or absence, meaning resolved) snapshot the most recent REAL sweep
    already maintains for every row, the identical shape
    :func:`_grade_open_outcomes`'s own ``current_by_row`` builds live.
    Never grace-gated (unlike a normal sweep's first grading): this is an
    explicit, operator-invoked correction of entries that are typically
    already well past any sensible grace window — see `coord
    drive-queue doctor-log --backfill-outcomes`.

    Returns the number of entries whose outcome this call actually
    changed.
    """
    from coord import state  # noqa: PLC0415

    open_outcomes = state.pending_board_doctor_outcomes() + state.regradable_board_doctor_outcomes()
    if not open_outcomes:
        return 0
    other_resolutions = _board_doctor_other_resolutions()
    tracked = set(state.board_doctor_tracked_rows())
    changed = 0
    for pending in open_outcomes:
        pending_sweep_id = pending.get("sweep_id")
        if pending_sweep_id is None:
            continue
        row_key = (pending.get("queue"), pending.get("key"))
        current: dict | None = None
        if row_key in tracked:
            row_state = state.board_doctor_row_state(row_key[0], row_key[1]) or None
            if row_state is not None:
                current = {
                    "cause": row_state.get("cause"),
                    "escalated": bool(row_state.get("escalated")),
                }
        attributed_to_other = _board_doctor_attributed_to_other(
            other_resolutions, row_key[1], state.board_doctor_decision_ts(pending),
        )
        outcome = state.evaluate_board_doctor_outcome(
            pending, current, attributed_to_other=attributed_to_other,
        )
        if outcome != pending.get("outcome"):
            state.record_board_doctor_outcome(pending_sweep_id, row_key[0], row_key[1], outcome)
            changed += 1
    return changed


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
    log_retention_days: float | None = None,
    backoff_initial_hours: float | None = None,
    backoff_cap_hours: float | None = None,
    outcome_grace_hours: float | None = None,
    counter_namespace: str | None = None,
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
         resets it to 1 and clears any prior escalation and backoff
         clock.
      2. ``tier_of(cause)`` decides auto vs. alert.
      3. Alert tier: dispatch via *alert_dispatcher* when either not yet
         escalated for this cause, OR already escalated but
         ``state.board_doctor_alert_due`` says the exponential backoff
         window has elapsed — never forever-silent once the first alert
         fires. Marks escalated (and bumps the backoff clock) only once
         the dispatch is CONFIRMED (``alert_dispatcher`` returns truthy).
         A dispatcher that raises or returns falsy leaves ``escalated``
         unchanged so the next sweep retries, rather than silently
         treating an unconfirmed write as done; returning
         :data:`ALERT_SKIPPED` does the same while saying the dispatcher
         chose not to file, not that filing failed.
      4. Auto tier: dispatch via ``auto_dispatchers[cause]`` while under
         *convergence_limit*; at/past the limit, falls back to the SAME
         backoff-gated alert path as step 3 instead of re-dispatching the
         same auto action forever. An ``auto_dispatchers`` callable
         returns ``True`` only once its action is CONFIRMED to have done
         something (mirroring *alert_dispatcher*'s own confirmed-truthy
         contract); a falsy return — the underlying C3 handler's
         primitive reported a no-op (row already gone, nothing to
         requeue, …) — is recorded as ``action="auto-dispatch-noop"``
         rather than ``"auto-dispatch"``, so the decision log never
         claims an action happened when the queue was, in fact, left
         untouched.

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
    or staying permanently escalation-blind. "Did not appear in *rows*" is
    decided on the real row behind each stored counter, so counters another
    caller wrote under its own *counter_namespace* are dropped on the same
    evidence as the plain one — the row genuinely left the walk — and kept
    whenever it is still stuck here.

    *escalation_resolver*, if given, is called as ``escalation_resolver
    (queue, key)`` (always the real, un-namespaced row key, once per row)
    for every row :func:`clear_absent_rows` drops — the
    counterpart to the alert path: once a row's cause has resolved (it
    stopped appearing), whatever alert was filed for it is stale and the
    resolver is expected to dismiss it. Called only on real (non-dry-run)
    sweeps, same as the clearing itself; never let a resolver's own
    failure interrupt the clear loop — that is the resolver's own
    responsibility to swallow, exactly like *alert_dispatcher* is expected
    to never raise.

    *capacity_check*, if given, is consulted BEFORE ``tier_of``/dispatch
    runs (#3770): a row for which ``capacity_check(row)`` returns ``True``
    is deferred — never dispatched this sweep, logged with
    ``action="investigate-deferred"`` (or ``"alert"`` once its own count
    reaches *convergence_limit*, see below), added to ``touched`` (so
    *clear_absent_rows* leaves it alone; it is still present, just not
    acted on this sweep). Peeking ``capacity_check(row)`` never itself
    claims a slot — only a row whose dispatch is actually CONFIRMED this
    sweep (``action == "auto-dispatch"``) does, via
    ``capacity_check.confirm_dispatch(row)`` if that method exists (see
    :class:`coord.board_doctor_policy.InvestigateCapacityGate`). That is
    what makes "bounding how many legs one sweep can fan out" true
    without ever starving a row outright: a row that is let through the
    check but turns out to be a converged-noop, an internal alert-tier
    fallback, or an in-flight no-op never holds a slot it never used, so
    the next row in *rows* — not the same row again next sweep — is the
    one that gets the freed capacity.

    #3918: a deferred row's convergence counter is NOT frozen — it bumps
    the exact same per-row counter (:func:`coord.state.
    record_board_doctor_observation`/:func:`coord.state.
    next_board_doctor_counter`) a non-deferred row's cause-observation
    bumps, "this cause observed again" meaning the same thing whether or
    not this sweep's own budget let it act on that observation. Once that
    count reaches *convergence_limit*, a deferred row escalates through
    the SAME :func:`_alert_action` path (subject to the same backoff via
    ``board_doctor_alert_due``) a non-deferred row past the limit already
    takes, instead of deferring indefinitely. Without this, a row the
    investigate tier's own budget keeps deferring sat at a permanently
    frozen ``count=0`` and could never escalate no matter how many sweeps
    it was deferred for (quadraui#2160: six consecutive hourly sweeps, all
    `investigate-deferred, count=0`).

    *log_retention_days*/*backoff_initial_hours*/*backoff_cap_hours*
    (#3889, #3875 asks B/C) default to ``None``, resolved below to
    :mod:`coord.state`'s module constants — the fleet's live
    ``coordinator.yml`` ``board_doctor:`` knobs when a caller threads them
    through (:func:`run_sweep` does, every real sweep), otherwise today's
    hardcoded defaults. *log_retention_days* governs
    :func:`coord.state.prune_board_doctor_log`, run once at the end of this
    sweep (never per-decision); the backoff hours govern
    :func:`coord.state.board_doctor_alert_due`/
    :func:`coord.state.mark_board_doctor_escalated`'s re-alert schedule.

    *counter_namespace*, when given, is folded into the convergence
    counter's storage key (never into *rows*' own ``(queue, key)`` identity
    used for ``touched``/decision-log/outcome-grading) so this call's
    observations land in a counter distinct from a plain (unnamespaced)
    call's. Without this, two callers sweeping the SAME rows at different
    cadences (:func:`run_sweep`'s hourly default call and its
    ``mechanical_causes``-filtered call, run every minute or two
    alongside it) would silently SHARE one counter — the tight-cadence
    caller would then burn *convergence_limit* in minutes instead of
    sweeps, and the slower caller would inherit that exhausted count the
    next time it ran, permanently pre-empting its own dispatch decision
    for a cause the fast pass merely saw a transient no-op on. Each
    distinct namespace gets its own independent budget against the SAME
    *convergence_limit*, so a caller meant to run 60x more often does not
    have to compensate with a 60x larger limit just to behave as if it
    were the only writer.

    ``"::"`` is reserved as that namespace separator and never appears in
    a real ``repo#issue`` row key, so a stored counter key always decodes
    back to the row it belongs to. ``clear_absent_rows`` below relies on
    that: it judges presence on the REAL row identity behind each stored
    key, so a namespaced counter is cleared when (and only when) its row
    has genuinely left this sweep's walk — never merely because a
    different call's namespace wrote it.

    *outcome_grace_hours* (#3907) defaults to ``None``, resolved below to
    :data:`coord.state.BOARD_DOCTOR_OUTCOME_GRACE_HOURS`. Outcome
    backfill (see :func:`_grade_open_outcomes`) grades BOTH #3889's
    still-``None`` pending decisions and #3907's already-graded-but-
    revisable ``unchanged``/``regressed`` ones every sweep — but a
    decision's very FIRST grading is held back until this many hours
    have passed since it was recorded, unless its row has already
    resolved outright. Without this, a row that is still stuck on the
    exact same cause the very next sweep purely because the leg its own
    ``auto-dispatch`` launched has not had time to land yet — a Rust
    work/test/review cycle routinely runs well past an hour — gets
    graded against a doctor-raised escalation that is itself an artifact
    of being graded too soon, not evidence the action failed. A decision
    already carrying a verdict is re-graded on every sweep regardless of
    this window: there is nothing left to protect it from, only a chance
    to correct it once the row finally reaches a done-like state.
    """
    moment = (now or _utcnow)()
    sweep_id = sweep_id or f"sweep-{moment.strftime('%Y%m%dT%H%M%SZ')}"
    auto_dispatchers = auto_dispatchers or {}
    rows = list(rows)
    report = SweepReport(sweep_id=sweep_id, dry_run=dry_run, rows_observed=len(rows))

    from coord import state  # noqa: PLC0415 — deferred so dry-run callers need no DB.

    backoff_initial_hours = (
        backoff_initial_hours
        if backoff_initial_hours is not None
        else state.BOARD_DOCTOR_BACKOFF_INITIAL_HOURS
    )
    backoff_cap_hours = (
        backoff_cap_hours
        if backoff_cap_hours is not None
        else state.BOARD_DOCTOR_BACKOFF_CAP_HOURS
    )
    outcome_grace_hours = (
        outcome_grace_hours
        if outcome_grace_hours is not None
        else state.BOARD_DOCTOR_OUTCOME_GRACE_HOURS
    )

    def _counter_key(key: str) -> str:
        """*key*, namespaced for the convergence counter only (never for
        ``touched``/decision-log/outcome-grading identity) — see
        *counter_namespace* above."""
        return key if counter_namespace is None else f"{key}::{counter_namespace}"

    touched: set[tuple[str, str]] = set()
    # Outcome backfill: decisions from EARLIER sweeps still open for a
    # verdict, read once up front so this sweep's own decisions below
    # never get evaluated against themselves. #3889's still-``None``
    # pending ones, plus #3907's already-graded-but-revisable
    # ``unchanged``/``regressed`` ones — a row that reaches a done-like
    # state (or moves on to a different cause) after being graded
    # negatively still deserves credit for that. Populated below as rows
    # are classified (`current_by_row`) and consumed by
    # `_grade_open_outcomes` after the main loop.
    open_outcomes = (
        state.pending_board_doctor_outcomes() + state.regradable_board_doctor_outcomes()
        if not dry_run
        else []
    )
    # #3889: evidence an operator action, a hand merge/unrelated branch
    # landing (or the ordinary drive loop, which this signal does NOT
    # distinguish from — see `_board_doctor_attributed_to_other`'s
    # docstring) moved a row BEFORE this sweep's own confirmed action could
    # have — read ONCE here rather than once per open decision, same
    # rationale as `open_outcomes` above. Empty (and never read) when
    # there is nothing open.
    other_resolutions = (
        _board_doctor_other_resolutions() if open_outcomes else {}
    )
    current_by_row: dict[tuple[str, str], dict] = {}

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
        state.mark_board_doctor_escalated(
            row.queue, _counter_key(row.key), now=moment,
            backoff_initial_hours=backoff_initial_hours,
            backoff_cap_hours=backoff_cap_hours,
        )
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
            prior = state.board_doctor_row_state(row.queue, _counter_key(row.key)) or {}
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
                        "recorded_at": moment.isoformat(),
                    }
                )
            continue
        if capacity_check is not None and capacity_check(row):
            # #3770/#3918: the row's repo has no free investigate-tier slot
            # this sweep — never dispatched, same as before. But deferred no
            # longer means "frozen forever": this bumps the SAME per-row
            # convergence counter the dispatch path below bumps for a row
            # that IS let through ("this cause observed again", regardless
            # of whether capacity allowed acting on it), so a row the
            # investigate tier's own per-repo budget keeps deferring still
            # advances toward `convergence_limit` sweep over sweep — and
            # once it reaches it, escalates through the SAME `_alert_action`
            # path a non-deferred row takes past the limit, rather than
            # deferring with a permanently frozen `count=0` (quadraui#2160:
            # six consecutive hourly sweeps all logged
            # `investigate-deferred, count=0`, because nothing here ever
            # moved the counter that decides when to stop deferring and
            # page a human instead).
            if dry_run:
                prior = state.board_doctor_row_state(row.queue, _counter_key(row.key)) or None
                counter_state = state.next_board_doctor_counter(
                    prior, row.cause, backoff_initial_hours=backoff_initial_hours,
                )
            else:
                counter_state = state.record_board_doctor_observation(
                    row.queue, _counter_key(row.key), row.cause,
                    backoff_initial_hours=backoff_initial_hours,
                )
            count, escalated = counter_state["count"], counter_state["escalated"]
            action = "investigate-deferred"
            if count >= convergence_limit:
                # Escalation is the only outcome a row can reach from here:
                # unlike the non-deferred branch below (which keeps a row
                # under `convergence_limit` eligible for `auto_dispatchers`
                # on every sweep), a row that has spent its whole counter
                # being deferred for capacity never gets routed back
                # through the dispatch path even once capacity frees up —
                # it alerts instead. A capacity-starved row that would
                # otherwise self-resolve on the next free slot is traded
                # for a human seeing it sooner.
                alert_due = not escalated or state.board_doctor_alert_due(
                    counter_state, now=moment, backoff_initial_hours=backoff_initial_hours,
                )
                if alert_due:
                    escalated, action = _alert_action(row)
            current_by_row[(row.queue, row.key)] = {
                "cause": row.cause, "escalated": escalated,
            }
            decision = Decision(
                queue=row.queue,
                key=row.key,
                cause=row.cause,
                tier=tier_of(row.cause),
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
                        "queue": decision.queue,
                        "key": decision.key,
                        "cause": decision.cause,
                        "tier": decision.tier,
                        "action": decision.action,
                        "count": decision.count,
                        "escalated": decision.escalated,
                        "confidence": row.confidence,
                        "evidence": list(row.evidence),
                        "recorded_at": moment.isoformat(),
                    }
                )
            continue
        if dry_run:
            prior = state.board_doctor_row_state(row.queue, _counter_key(row.key)) or None
            counter_state = state.next_board_doctor_counter(
                prior, row.cause, backoff_initial_hours=backoff_initial_hours,
            )
        else:
            counter_state = state.record_board_doctor_observation(
                row.queue, _counter_key(row.key), row.cause,
                backoff_initial_hours=backoff_initial_hours,
            )
        count, escalated = counter_state["count"], counter_state["escalated"]
        # A row already escalated for this SAME cause is not
        # re-acted on forever — `board_doctor_alert_due` re-opens the
        # alert path on an exponential backoff instead, so a still-wrong
        # row keeps getting flagged (just less often over time) rather
        # than converging to permanent silence once its first alert
        # fires.
        alert_due = not escalated or state.board_doctor_alert_due(
            counter_state, now=moment, backoff_initial_hours=backoff_initial_hours,
        )

        tier = tier_of(row.cause)
        action = "converged-noop"

        if tier == TIER_ALERT:
            if alert_due:
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
            if alert_due:
                escalated, action = _alert_action(row)

        current_by_row[(row.queue, row.key)] = {"cause": row.cause, "escalated": escalated}

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
                    "recorded_at": moment.isoformat(),
                }
            )

    if not dry_run:
        # Fill in / correct the verdict for every decision still open for
        # grading (#3889's pending + #3907's regradable, see
        # `_grade_open_outcomes`) — whether its row was classified again
        # THIS sweep (`current_by_row`, still stuck) or dropped out of
        # *rows* entirely (`touched`, resolved).
        _grade_open_outcomes(
            open_outcomes=open_outcomes,
            current_by_row=current_by_row,
            touched=touched,
            other_resolutions=other_resolutions,
            moment=moment,
            sweep_id=sweep_id,
            grace_hours=outcome_grace_hours,
            clear_absent_rows=clear_absent_rows,
        )

    if not dry_run and clear_absent_rows:
        # A single resolved row can have several stored counters (a plain
        # one plus one per namespace that observed it); its escalation is
        # one object, so dismiss it once rather than per stored key.
        dismissed: set[tuple[str, str]] = set()
        for queue, key in state.board_doctor_tracked_rows():
            # *key* here is the RAW stored key, which carries a
            # ``"::<namespace>"`` suffix whenever the call that wrote it
            # passed a *counter_namespace* — this one's, or any other
            # caller's. Presence is judged on the REAL row identity
            # behind that key, never on the stored key itself: this
            # sweep's own `touched` only ever holds plain row keys, so
            # comparing raw would read EVERY foreign-namespaced counter
            # as absent and wipe it (plus dismiss the row's live
            # escalation) even while the row sits stuck in this very
            # sweep's own walk.
            real_key = key.split("::", 1)[0]
            if (queue, real_key) not in touched:
                state.clear_board_doctor_row(queue, key)
                if escalation_resolver is not None and (queue, real_key) not in dismissed:
                    dismissed.add((queue, real_key))
                    try:
                        escalation_resolver(queue, real_key)
                    except Exception:  # noqa: BLE001, S110 — one row's cleanup
                        # failing must not abort the walk and leave the
                        # remaining tracked rows' counters uncleared.
                        pass

    if not dry_run:
        state.prune_board_doctor_log(retention_days=log_retention_days)

    return report


def _branch_merged_guard_row_opted_in(row: StuckRow, config: Any) -> bool:
    """Whether *row* (cause :data:`CAUSE_BRANCH_MERGED_GUARD`) may be
    pulled out of the ordinary alert/mechanical path for refiling.

    Parses the row's drive-queue key for its repo and defers to
    :func:`coord.board_doctor_followthrough.auto_refile_enabled`. An
    unparseable key reads as "not opted in" — the row then stays in
    :func:`run_sweep`'s *other_rows*, the same fail-safe posture as a repo
    that never set ``board_doctor.auto_refile: true``.
    """
    from coord.drive_queue import parse_key  # noqa: PLC0415

    parsed = parse_key(row.key)
    if parsed is None:
        return False
    repo_name, _issue_number = parsed
    return board_doctor_followthrough.auto_refile_enabled(repo_name, config)


def _ceiling_blocked_ci_pending_candidates(
    rows: Sequence[StuckRow],
) -> list[tuple[str, int]]:
    """``(repo_name, issue_number)`` for every FRESH drive-queue row in
    *rows* that is both ceiling-blocked (#2972) and currently diagnosed
    :data:`queue_diagnose.CAUSE_CI_PENDING` — the one shape neither
    :func:`_reclassify_ceiling_blocked` (which only promotes
    :data:`CAUSE_NOTHING_BLOCKING`) nor the ordinary mechanical
    ``CAUSE_MERGE_CONFLICT`` dispatch covers (#4036): a conflict-fix leg
    already pushed a resolution, GitHub is still running checks on the
    new head, and nothing else in this sweep is watching for them to
    settle.

    ``fresh`` is required for the same reason :func:`_reclassify_ceiling_
    blocked` requires it: a :class:`StuckRow` :func:`_episode_stuck_row`
    rebuilt from a settled ``block_log`` episode can be arbitrarily old,
    and this must act only on a live probe THIS sweep just took.

    Also requires :data:`coord.queue_diagnose.CONFIDENCE_LOW` be absent —
    the same guard :func:`_reclassify_ceiling_blocked` applies to its own
    live-clear reclassification: a low-confidence verdict means the live
    probe that produced it was itself incomplete (``live.probe_errors``
    downgraded it), so "this row is CI-pending" has not actually been
    established. :func:`run_conflict_fix_ceiling_pending_sweep`'s own
    *evidence_fetcher* re-probe softens, but does not replace, this.

    Deduped — a (repo, issue) can legitimately appear more than once if
    an earlier pass in :func:`_collect_rows_with_board` also produced a
    row for it, but this candidate list feeds exactly one requeue attempt
    per issue per sweep.
    """
    from coord.drive_queue import (  # noqa: PLC0415
        fix_round_ceiling_blocked_for_issue,
        parse_key as _parse_row_key,
    )

    seen: set[tuple[str, int]] = set()
    candidates: list[tuple[str, int]] = []
    for row in rows:
        if (
            row.queue != "drive-queue"
            or not row.fresh
            or row.cause != queue_diagnose.CAUSE_CI_PENDING
            or row.confidence == queue_diagnose.CONFIDENCE_LOW
        ):
            continue
        parsed = _parse_row_key(row.key)
        if parsed is None or parsed in seen:
            continue
        if not fix_round_ceiling_blocked_for_issue(*parsed):
            continue
        seen.add(parsed)
        candidates.append(parsed)
    return candidates


def _live_conflict_fix_evidence(
    repo_name: str, issue_number: int, *, config: Any,
) -> tuple[tuple[str, ...] | None, bool]:
    """A fresh, single-entry live read for *repo_name*#*issue_number*, via
    the SAME :class:`coord.queue_diagnose.GhLiveProbe` the main sweep walk
    uses (#2096: one question, one answer — never a second,
    independently-shaped `gh` call).

    Returns ``(checks, branch_moved)``:

    * *checks* — each check's bare ``conclusion`` string, for
      :func:`coord.drive_queue.conflict_fix_push_ci_verdict`'s own
      three-way classification. ``None`` on any read failure, an unqueued
      entry, or a probe that could not confirm the checks at all — the
      fail-closed "we could not tell" reading that function already treats
      as "still pending", never as green.
    * *branch_moved* — #4036 review (blocking finding #5): ``True`` only
      when the SAME probe's :attr:`~coord.queue_diagnose.LiveState.
      review_stale`/:attr:`~coord.queue_diagnose.LiveState.test_stale`
      confirms a recorded review/test verdict no longer covers the
      branch's CURRENT head — live evidence that something has actually
      been pushed since this row's ceiling-blocking verdict was recorded,
      never merely that its checks happen to be in flight for an unrelated
      reason (nobody has pushed anything new at all). ``False`` on a read
      failure or an unqueued entry too — "could not tell" must never read
      as "yes, it moved". The caller (:func:`run_conflict_fix_ceiling_
      pending_sweep`) must not reset the stage or re-stamp a fresh
      #2972 fix-round budget without this confirmed ``True`` — pending
      checks with no confirmed push behind them are exactly the
      CAUSE_NOTHING_BLOCKING/#3760-Action-1 shape, not this one.
    """
    from coord.commands.drive_queue import _local_host_id  # noqa: PLC0415
    from coord.drive import list_drive_sessions  # noqa: PLC0415
    from coord.drive_queue import QueueEntry  # noqa: PLC0415
    from coord.health.aggregate import local_fleet_health_block  # noqa: PLC0415
    from coord.state import build_board, get_drive_queue_entry  # noqa: PLC0415

    row = get_drive_queue_entry(repo_name, issue_number)
    if row is None:
        return None, False
    entry = QueueEntry.from_row(row)

    try:
        board = build_board()
        local_host = _local_host_id()
        live_sessions = frozenset(
            str(r["session_name"]) for r in list_drive_sessions() if r.get("session_name")
        )
        fleet_health = local_fleet_health_block([m.name for m in config.machines])
        probe = queue_diagnose.GhLiveProbe(
            config=config, board=board, fleet_health=fleet_health,
            live_sessions=live_sessions, local_host=local_host,
        )
        live = probe.probe(entry)
    except Exception:  # noqa: BLE001 — a failed re-probe reads as "still pending"
        _log.debug(
            "board-doctor: live-check re-probe failed for %s#%s",
            repo_name, issue_number, exc_info=True,
        )
        return None, False
    checks = None if live.checks is None else tuple(c.conclusion for c in live.checks)
    branch_moved = live.review_stale is True or live.test_stale is True
    return checks, branch_moved


def run_conflict_fix_ceiling_pending_sweep(
    rows: Sequence[StuckRow],
    *,
    config: Any,
    evidence_fetcher: Callable[[str, int], tuple[tuple[str, ...] | None, bool]] | None = None,
    requeuer: Callable[..., Any] | None = None,
) -> list[dict]:
    """#4036: for every ceiling-blocked drive-queue row this sweep just
    diagnosed :data:`queue_diagnose.CAUSE_CI_PENDING` (see
    :func:`_ceiling_blocked_ci_pending_candidates`), re-probe its live
    evidence and hand the verdict to :func:`coord.drive_queue.
    requeue_after_conflict_fix` — the one place that decides whether to
    wait, alert, or actually requeue the row.

    #4036 review (blocking finding #5): a candidate's premise — that a
    conflict-fix leg actually pushed something — is never ASSUMED from
    "ceiling-blocked and CI pending" alone; that shape also matches an
    ordinary row whose checks are simply still running for an unrelated
    reason. Before *requeuer* is ever called, *evidence_fetcher*'s own
    ``branch_moved`` reading (:func:`_live_conflict_fix_evidence`'s
    ``review_stale``/``test_stale``-backed confirmation that the live head
    has actually moved past the recorded verdict) must read ``True`` — a
    candidate whose evidence does not confirm this is reported
    ``requeued=False`` with NO call to *requeuer* at all, so neither the
    stage reset nor a fresh #2972 fix-round budget is ever granted off a
    checks-only reading.

    *evidence_fetcher* defaults to :func:`_live_conflict_fix_evidence`;
    *requeuer* defaults to :func:`coord.drive_queue.requeue_after_conflict_fix`
    — both injectable so a test never needs a live `gh` call or a real
    drive-queue/board DB.

    Returns one dict per candidate — ``{"repo", "issue", "requeued",
    "detail"}`` — mirroring :func:`coord.drive_queue.
    ConflictFixRequeueResult`'s own shape, reported onto :attr:`SweepReport.
    conflict_fix_ceiling_pending` rather than :attr:`SweepReport.decisions`:
    like *followthrough*/*refile*, this is not a tier/auto_dispatchers/
    convergence decision over the row at all.
    """
    from coord.drive_queue import conflict_fix_push_ci_verdict  # noqa: PLC0415
    from coord.drive_queue import requeue_after_conflict_fix as _default_requeuer  # noqa: PLC0415

    fetch_evidence = evidence_fetcher or (
        lambda repo_name, issue_number: _live_conflict_fix_evidence(
            repo_name, issue_number, config=config,
        )
    )
    requeue = requeuer or _default_requeuer

    results: list[dict] = []
    for repo_name, issue_number in _ceiling_blocked_ci_pending_candidates(rows):
        checks, branch_moved = fetch_evidence(repo_name, issue_number)
        if not branch_moved:
            outcome: dict[str, Any] = {
                "requeued": False,
                "detail": (
                    f"{repo_name}#{issue_number}: no live evidence yet that "
                    "a conflict-fix push moved this branch past its "
                    "recorded review/test verdict — waiting for that "
                    "confirmation before resetting the stage or spending a "
                    "fresh fix-round budget"
                ),
            }
        else:
            ci_verdict = conflict_fix_push_ci_verdict(checks or ())
            outcome = requeue(repo_name, issue_number, ci_verdict=ci_verdict)
        results.append({
            "repo": repo_name,
            "issue": issue_number,
            "requeued": bool(outcome.get("requeued")),
            "detail": outcome.get("detail", ""),
        })
    return results


def _ceiling_blocked_integration_red_candidates(
    rows: Sequence[StuckRow],
) -> list[tuple[str, int]]:
    """``(repo_name, issue_number)`` for every FRESH drive-queue row in
    *rows* that is both ceiling-blocked (#2972) and currently diagnosed
    :data:`queue_diagnose.CAUSE_INTEGRATION_RED` (#4050) — the quadraui#2726
    shape: no textual conflict, a branch Test verdict that already read
    green, and a required CI check failing on the PR's merge ref anyway
    because the target branch moved under it. The ordinary investigate-
    tier dispatcher for this cause (``coord.ci_fix.dispatch_ci_fix`` via
    ``coord.board_doctor_policy``'s ``sweep_dispatch_fixit_leg``) is a
    ``type="work"`` leg and so is exactly what #3454 refuses once this
    budget is spent — this is the narrow carve-out :func:`run_base_sync_
    fix_ceiling_sweep` acts on instead, mirroring :func:`_ceiling_blocked_
    ci_pending_candidates`'s own shape (#4036) exactly.

    ``fresh`` is required for the same reason as that sibling filter: a
    :class:`StuckRow` :func:`_episode_stuck_row` rebuilt from a settled
    ``block_log`` episode can be arbitrarily old, and this must act only
    on a live probe THIS sweep just took.

    Also requires :data:`coord.queue_diagnose.CONFIDENCE_LOW` be absent —
    a low-confidence verdict means the live probe that produced it was
    itself incomplete (``live.probe_errors`` downgraded it), so
    "this row is integration-red" has not actually been established.
    :func:`run_base_sync_fix_ceiling_sweep`'s own *evidence_fetcher*
    re-probe softens, but does not replace, this.

    Deduped — a (repo, issue) can legitimately appear more than once if
    an earlier pass in :func:`_collect_rows_with_board` also produced a
    row for it, but this candidate list feeds exactly one dispatch attempt
    per issue per sweep.
    """
    from coord.drive_queue import (  # noqa: PLC0415
        fix_round_ceiling_blocked_for_issue,
        parse_key as _parse_row_key,
    )

    seen: set[tuple[str, int]] = set()
    candidates: list[tuple[str, int]] = []
    for row in rows:
        if (
            row.queue != "drive-queue"
            or not row.fresh
            or row.cause != queue_diagnose.CAUSE_INTEGRATION_RED
            or row.confidence == queue_diagnose.CONFIDENCE_LOW
        ):
            continue
        parsed = _parse_row_key(row.key)
        if parsed is None or parsed in seen:
            continue
        if not fix_round_ceiling_blocked_for_issue(*parsed):
            continue
        seen.add(parsed)
        candidates.append(parsed)
    return candidates


def _live_base_sync_fix_evidence(
    repo_name: str, issue_number: int, *, config: Any,
) -> tuple[bool, str]:
    """A fresh, single-entry live re-probe for *repo_name*#*issue_number*
    (#4050), via the SAME :class:`coord.queue_diagnose.GhLiveProbe` the
    main sweep walk uses (#2096: one question, one answer — never a
    second, independently-shaped `gh` call).

    This spends a fresh probe and runs it back through :func:`coord.
    queue_diagnose.diagnose` (the same pure classifier, on the SAME
    ceiling-blocked rule table :func:`_refresh_ceiling_blocked_settled_
    rows` already re-probes through) rather than reading raw fields the
    way :func:`_live_conflict_fix_evidence` does — unlike that sibling,
    which only needs checks/staleness booleans, #4050's premise is the
    CAUSE itself, not just one field of it, so confirming the cause is
    the question that actually needs asking immediately before a dispatch
    that bypasses #3454's ceiling.

    Returns ``(confirmed, detail)``:

    * ``confirmed=True`` only when the fresh probe's diagnosis STILL
      reads :data:`queue_diagnose.CAUSE_INTEGRATION_RED` at a confidence
      better than :data:`queue_diagnose.CONFIDENCE_LOW` AND the
      SAME probe's :attr:`~coord.queue_diagnose.LiveState.base_moved_note`
      is populated — ``coord gates`` itself positively confirming the
      quadraui#2726 divergence, not merely "a check happens to be red
      while the branch test happens to read green" (the #4036 review's
      "never assume the premise" rule, applied to this cause).
    * ``confirmed=False`` for every other outcome — no drive-queue row,
      a failed probe, a diagnosis that no longer names this cause, or one
      that does but with no confirmed ``base_moved_note`` — with a
      *detail* string explaining which. The caller must never dispatch
      the budget-free base-sync-fix leg on anything less than a fresh
      ``True`` here.
    """
    from coord.commands.drive_queue import _local_host_id  # noqa: PLC0415
    from coord.drive import list_drive_sessions  # noqa: PLC0415
    from coord.drive_queue import QueueEntry  # noqa: PLC0415
    from coord.health.aggregate import local_fleet_health_block  # noqa: PLC0415
    from coord.state import build_board, get_drive_queue_entry  # noqa: PLC0415

    row = get_drive_queue_entry(repo_name, issue_number)
    if row is None:
        return False, f"{repo_name}#{issue_number}: no drive-queue row to confirm against"
    entry = QueueEntry.from_row(row)

    try:
        board = build_board()
        local_host = _local_host_id()
        live_sessions = frozenset(
            str(r["session_name"]) for r in list_drive_sessions() if r.get("session_name")
        )
        fleet_health = local_fleet_health_block([m.name for m in config.machines])
        probe = queue_diagnose.GhLiveProbe(
            config=config, board=board, fleet_health=fleet_health,
            live_sessions=live_sessions, local_host=local_host,
        )
        live = probe.probe(entry)
    except Exception:  # noqa: BLE001 — a failed re-probe reads as "not confirmed"
        _log.debug(
            "board-doctor: live-check re-probe failed for %s#%s",
            repo_name, issue_number, exc_info=True,
        )
        return False, f"{repo_name}#{issue_number}: live re-probe failed"

    diagnosis = queue_diagnose.diagnose(entry, live, trigger="board-doctor-sweep")
    if diagnosis.cause != queue_diagnose.CAUSE_INTEGRATION_RED:
        return False, (
            f"{repo_name}#{issue_number}: live re-probe no longer confirms "
            f"integration-red (now {diagnosis.cause!r}) — not dispatching a "
            "base-sync fix on a stale reading"
        )
    if diagnosis.confidence == queue_diagnose.CONFIDENCE_LOW:
        return False, (
            f"{repo_name}#{issue_number}: live re-probe confidence is too "
            "low to confirm the cause"
        )
    if not live.base_moved_note:
        return False, (
            f"{repo_name}#{issue_number}: coord gates does not confirm a "
            "BASE MOVED divergence on this live read — refusing to spend a "
            "budget-free fix on an unconfirmed premise"
        )
    return True, f"{repo_name}#{issue_number}: confirmed — {live.base_moved_note}"


def _dispatch_base_sync_fix(
    repo_name: str, issue_number: int, *, config: Any,
) -> dict:
    """Dispatch the #4050 base-sync-fix conflict-fix leg for *repo_name*
    #*issue_number*'s live merge-queue entry.

    Mirrors ``coord.board_doctor_permissions.sweep_dispatch_conflict_fix``'s
    own shape (the mechanical, budget-free dispatcher #3918 already
    registered for a standing ``CAUSE_MERGE_CONFLICT`` row) but stays
    inside this module rather than extending that one: this reuses the
    SAME already-allowlisted ``"conflict_fix_dispatch"``
    :func:`coord.board_doctor_permissions.authorize_sweep_action` entry —
    the permission being granted ("this sweep may dispatch a
    ``type="conflict-fix"`` leg") does not change just because the cause
    that qualifies the row for it is different, and :data:`coord.
    board_doctor_permissions.SAFE_TIER_ACTIONS` is a fixed allow-list
    extended by deliberate edit, not by a second name meaning the same
    thing. :func:`coord.conflict_fix.dispatch_conflict_fix` itself already
    enforces the one-mechanical-attempt-then-human retry cap via
    ``has_prior_conflict_fix`` — this function never re-derives that.

    Returns ``{"dispatched": bool, "detail": str}`` for every outcome this
    function itself decides: no merge-queue entry, no capable machine, an
    already-active conflict-fix, or a spent retry cap are all confirmed
    no-ops reported as ``dispatched=False`` rather than raised. This does
    NOT mean the function can never raise at all — ``authorize_sweep_
    action`` raises :class:`coord.board_doctor_permissions.
    SweepPermissionError` by design, and ``load_queue``/``build_board``/
    ``write_board`` can all raise their own I/O errors; callers already
    expect that (:func:`run_base_sync_fix_ceiling_sweep` has no `try` of
    its own, and :func:`run_sweep` wraps its whole call in one). The
    caller withdraws a row from the tier path only on ``dispatched=True``
    (:func:`run_sweep`'s own ``base_sync_fix_keys``) — every ``False``
    outcome here is left for the row's own convergence counter to
    escalate if it recurs.
    """
    from coord.board_doctor_permissions import authorize_sweep_action  # noqa: PLC0415
    from coord.board_service import write_board  # noqa: PLC0415
    from coord.conflict_fix import dispatch_conflict_fix  # noqa: PLC0415
    from coord.merge_queue import load_queue  # noqa: PLC0415
    from coord.network import fetch_status  # noqa: PLC0415
    from coord.state import build_board  # noqa: PLC0415

    authorize_sweep_action("conflict_fix_dispatch")

    entry = next(
        (
            e for e in load_queue()
            if e.repo_name == repo_name and e.issue_number == issue_number
        ),
        None,
    )
    if entry is None:
        return {
            "dispatched": False,
            "detail": f"{repo_name}#{issue_number} has no merge-queue entry",
        }

    board = build_board()
    fix = dispatch_conflict_fix(
        entry, board, config, base_sync_fix=True, status_fetcher=fetch_status,
    )
    if fix is None:
        return {
            "dispatched": False,
            "detail": (
                f"{repo_name}#{issue_number}: a base-sync fix could not be "
                "dispatched (no capable machine, one already active, or "
                "the retry cap is already spent)"
            ),
        }
    write_board(board)
    return {
        "dispatched": True,
        "detail": (
            f"{repo_name}#{issue_number}: dispatched base-sync-fix "
            f"{fix.assignment_id} on {fix.machine_name}"
        ),
    }


def run_base_sync_fix_ceiling_sweep(
    rows: Sequence[StuckRow],
    *,
    config: Any,
    evidence_fetcher: Callable[[str, int], tuple[bool, str]] | None = None,
    dispatcher: Callable[[str, int], dict] | None = None,
) -> list[dict]:
    """#4050: for every ceiling-blocked drive-queue row this sweep just
    diagnosed :data:`queue_diagnose.CAUSE_INTEGRATION_RED` (see
    :func:`_ceiling_blocked_integration_red_candidates`), re-confirm the
    cause live and, only on a confirmed premise, dispatch the budget-free
    base-sync-fix conflict-fix leg (:func:`_dispatch_base_sync_fix`) —
    never the generic investigate-tier fix-it leg, which #3454 already
    refuses for a row this ceiling-blocked.

    A candidate's premise is never assumed from the cause label alone:
    before *dispatcher* is ever called, *evidence_fetcher*'s fresh
    re-confirmation (:func:`_live_base_sync_fix_evidence` by default) must
    read ``True`` — a candidate whose evidence does not confirm this is
    reported ``dispatched=False`` with NO call to *dispatcher* at all, the
    same "never act on an unconfirmed premise" posture :func:`run_
    conflict_fix_ceiling_pending_sweep` already applies to its own
    ``branch_moved`` reading.

    *evidence_fetcher*/*dispatcher* default to :func:`_live_base_sync_fix_
    evidence`/:func:`_dispatch_base_sync_fix`; both injectable so a test
    never needs a live `gh` call, a real merge queue, or an HTTP POST.

    Returns one dict per candidate — ``{"repo", "issue", "dispatched",
    "detail"}`` — reported onto :attr:`SweepReport.
    base_sync_fix_ceiling_dispatch` rather than :attr:`SweepReport.
    decisions`: like *conflict_fix_ceiling_pending*, this is not a
    tier/auto_dispatchers/convergence decision over the row at all.
    """
    fetch_evidence = evidence_fetcher or (
        lambda repo_name, issue_number: _live_base_sync_fix_evidence(
            repo_name, issue_number, config=config,
        )
    )
    dispatch = dispatcher or (
        lambda repo_name, issue_number: _dispatch_base_sync_fix(
            repo_name, issue_number, config=config,
        )
    )

    results: list[dict] = []
    for repo_name, issue_number in _ceiling_blocked_integration_red_candidates(rows):
        confirmed, detail = fetch_evidence(repo_name, issue_number)
        if not confirmed:
            results.append({
                "repo": repo_name, "issue": issue_number,
                "dispatched": False, "detail": detail,
            })
            continue
        outcome = dispatch(repo_name, issue_number)
        results.append({
            "repo": repo_name,
            "issue": issue_number,
            "dispatched": bool(outcome.get("dispatched")),
            "detail": outcome.get("detail", ""),
        })
    return results


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
    followthrough: bool = True,
    followthrough_sweep: Callable[..., list[board_doctor_followthrough.Disposition]] = (
        run_followthrough_sweep
    ),
    mechanical_causes: frozenset[str] | None = None,
    drop_predicted_after_edges: bool = True,
    predicted_after_edges_sweep: Callable[..., list[str]] = sweep_predicted_after_edges,
    auto_refile: bool = True,
    refile_sweep: Callable[..., list[board_doctor_followthrough.Disposition]] = (
        run_refile_sweep
    ),
    refile_close_sweep: Callable[..., list[dict]] = run_pending_refile_close_sweep,
    conflict_fix_ceiling_requeue: bool = True,
    conflict_fix_ceiling_sweep: Callable[..., list[dict]] = (
        run_conflict_fix_ceiling_pending_sweep
    ),
    config_drift_autofix_apply: bool = True,
    config_drift_fix_sweep: Callable[..., list[dict]] = run_config_drift_fix_sweep,
    base_sync_fix_ceiling_dispatch: bool = True,
    base_sync_fix_ceiling_sweep: Callable[..., list[dict]] = (
        run_base_sync_fix_ceiling_sweep
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

    #3889: *config*'s ``board_doctor.log_retention_days`` /
    ``backoff_initial_hours`` / ``backoff_cap_hours`` (and #3907's
    ``outcome_grace_hours``, read the same defensive ``getattr`` way so a
    config version predating that knob still resolves to `sweep`'s own
    default) are read fresh HERE, every call — this is the per-sweep
    production entry point, so a ``coordinator.yml`` edit takes effect on
    the very next sweep with no daemon restart, same posture as every
    other `coord.board_doctor_policy` knob this module already reads live
    from *config*.

    #3945: ``merged-partial`` rows are pulled OUT of *rows* before they
    ever reach :func:`sweep` — that function's tier/auto_dispatchers/
    convergence loop exists to retry a cause across sweeps with backoff,
    the wrong shape for "file the follow-ups and close the issue, once".
    They're resolved by *followthrough_sweep* (default
    :func:`run_followthrough_sweep`) instead and reported on
    :attr:`SweepReport.followthrough`, while still counting toward
    :attr:`SweepReport.rows_observed` so "nothing stuck" stays honest.
    *followthrough* is the same kind of fleet-wide kill switch
    ``--no-investigate`` is for the C1 tier — ``False`` disarms this pass
    for this invocation regardless of any repo's own opt-in
    (:func:`coord.board_doctor_followthrough.followthrough_enabled` is
    still consulted per-row on top of this).

    *mechanical_causes*, when given, restricts this call to ONLY the
    named causes — every other row :func:`collect_rows` found (including
    every ``merged-partial`` follow-through row) is dropped before
    :func:`sweep` ever sees it, not merely alert-suppressed. This is the
    tight-cadence entry point a scheduler can run every minute or two
    alongside an hourly full sweep: a narrower, more frequent pass that
    only ever touches the deterministic, pre-approved queue repairs
    (``coord.board_doctor_policy.mechanical_causes()``), never the
    investigate or decomposition tiers' convergence counters,
    escalations or `gh`/leg-dispatch load. ``clear_absent_rows=False`` is
    forced alongside the filter for exactly this reason: this call's own
    row set is a deliberate subset, so a row outside it going "absent"
    from THIS sweep's point of view must never be read as "resolved" and
    have its counter wiped — only a full (unfiltered) sweep's own absence
    check may draw that conclusion. This call also threads
    :data:`MECHANICAL_SWEEP_COUNTER_NAMESPACE` through as *sweep*'s
    *counter_namespace* — its own convergence counter, independent of the
    plain (unfiltered) call's, so a tight-cadence scheduler running this
    pass every minute or two does not burn the SAME ``convergence_limit``
    budget the hourly call still relies on for these causes. The budgets
    are independent, not immortal: the plain call's absence check judges
    presence on real row identity, so it retires this call's namespaced
    counter too — once the row it belongs to has genuinely left the full
    walk, and never while it is still stuck.

    It also collects with :data:`MECHANICAL_MERGE_PENDING_STALE_AFTER_SECONDS`
    in place of :data:`MERGE_PENDING_STALE_AFTER_SECONDS` for
    :data:`CAUSE_MERGE_PENDING_STUCK`/:data:`CAUSE_MERGE_NEVER_ATTEMPTED`'s
    own staleness pre-filter — the motivating "green, approved, mergeable,
    but the ordinary merge tick keeps missing it" row would otherwise stay
    invisible to THIS call for up to 30 minutes regardless of how often the
    tight cadence fires, since both causes are minted off that same floor.
    The plain (unfiltered) call keeps the 30-minute floor unchanged.

    #4037: *predicted_after_edges_sweep* (default
    :func:`sweep_predicted_after_edges`) runs once per call and reports onto
    :attr:`SweepReport.dropped_predicted_after_edges` — gated on
    *drop_predicted_after_edges* (the same kind of fleet-wide kill switch
    *followthrough* already is) AND ``mechanical_causes is None``, so the
    tight-cadence call does not pay for an extra queue read plus one audit
    query per `after=`-bearing entry every minute or two; the hourly plain
    call still catches it well inside #2534's "every hour and did nothing"
    window. A failure here is advisory-only, same posture as the
    no-progress pass in :func:`_collect_rows_with_board`: it never takes
    the rest of this report down.

    #4038: ``branch-merged-guard`` rows are pulled out of *rows* the same
    way ``merged-partial`` rows are, and resolved by *refile_sweep*
    (default :func:`run_refile_sweep`) onto :attr:`SweepReport.refile`,
    gated by *auto_refile* — the same kind of fleet-wide kill switch
    *followthrough* is (:func:`coord.board_doctor_followthrough.
    auto_refile_enabled` is still consulted per-row on top of this).
    *refile_close_sweep* (default :func:`run_pending_refile_close_sweep`)
    additionally runs every plain (non-mechanical) call regardless of
    whether THIS round observed any ``branch-merged-guard`` row at all —
    step 4 of a refile already filed in some EARLIER round has no row of
    its own left to be driven by (the original's drive-queue row is gone
    the moment the refile files successfully), so it reports onto
    :attr:`SweepReport.refile_closed` independently of *rows*/*refile_rows*.

    #4048: *config_drift_fix_sweep* (default :func:`run_config_drift_fix_sweep`)
    attempts the #4048 Apply+Refund+requeue steps for every
    :data:`queue_diagnose.CAUSE_CONFIG_DRIFT` candidate whose repo has
    opted into ``board_doctor.config_drift_autofix`` — gated by
    *config_drift_autofix_apply*, the same kind of fleet-wide kill switch
    *auto_refile*/*followthrough* are, AND a hard ``dry_run`` skip (never
    ``config_drift_fix_sweep(..., dry_run=...)``, mirroring
    *conflict_fix_ceiling_sweep*'s own posture): unlike every other pass
    here, this one commits to a SEPARATE git checkout
    (``coord-settings``), which has no dry-run mode of its own to thread
    through, so a dry-run caller must see zero mutation there, not a
    best-effort one. Runs BEFORE :func:`sweep` (not after, unlike
    *conflict_fix_ceiling_sweep*): a :class:`StuckRow` this round's own
    attempt just resolved (``requeued=True``) is withdrawn from *rows*
    before it ever reaches the tier/alert path, exactly the way
    ``merged-partial``/``branch-merged-guard`` rows are withdrawn above —
    but, per :func:`run_config_drift_fix_sweep`'s own docstring, ONLY the
    keys this round confirms resolved, never every opted-in candidate
    regardless of outcome: a failed attempt leaves its row's convergence
    counter and any live escalation untouched instead of silently
    dismissing them.

    #4050: *base_sync_fix_ceiling_sweep* (default :func:`run_base_sync_fix_
    ceiling_sweep`) is the twin of *conflict_fix_ceiling_sweep* for a
    DIFFERENT budget-free shape — a ceiling-blocked row diagnosed
    :data:`queue_diagnose.CAUSE_INTEGRATION_RED` (no textual conflict, a
    branch Test verdict that already read green, CI red only because the
    target branch moved under it) gets a fresh, confirmed-live dispatch of
    the base-sync-fix conflict-fix leg instead of the generic investigate-
    tier fix-it leg #3454 already refuses it.

    Unlike *conflict_fix_ceiling_sweep*'s ``CAUSE_CI_PENDING`` rows (which
    self-resolve the moment checks settle, so suppressing every opted-in
    candidate regardless of outcome just costs a sweep or two), a row
    diagnosed ``CAUSE_INTEGRATION_RED`` does not self-clear — it stays red
    until something pushes a new head. So this pass runs BEFORE the row
    loop below (not after :func:`sweep`, hard ``dry_run`` skip, failure is
    advisory-only — otherwise the same posture), and only the keys it
    actually confirmed ``dispatched=True`` (``base_sync_fix_keys``, mirroring
    *config_drift_resolved_keys* a few lines down) are withdrawn from the
    tier/alert path as ``suppressed=True``. A candidate whose premise never
    reconfirms, or whose dispatch finds the retry cap already spent, is
    left in the ordinary row walk so its convergence counter keeps
    advancing and still escalates to a human at *convergence_limit* —
    gated by *base_sync_fix_ceiling_dispatch*, the same kind of fleet-wide
    kill switch *conflict_fix_ceiling_requeue* is.
    """
    board_doctor_cfg = getattr(config, "board_doctor", None)
    rows, board = _collect_rows_with_board(
        config=config,
        merge_pending_stale_after_seconds=(
            MECHANICAL_MERGE_PENDING_STALE_AFTER_SECONDS
            if mechanical_causes is not None
            else None
        ),
    )
    followthrough_rows = [r for r in rows if r.cause == CAUSE_MERGED_PARTIAL]
    # A `branch-merged-guard` row is pulled out of *other_rows* ONLY when
    # it is actually going to be refiled this call (the fleet-wide
    # *auto_refile* switch AND the row's own repo opted in via
    # `board_doctor.auto_refile: true`) — a repo that has not opted in
    # (the fleet default) keeps seeing this row on the ordinary
    # alert/mechanical path instead, so it neither vanishes from the
    # report nor has `clear_absent_rows` read the vanishing as "resolved"
    # and dismiss its live escalation.
    refile_rows: list[StuckRow] = []
    other_rows: list[StuckRow] = []
    # #4036: a ceiling-blocked drive-queue row diagnosed `CAUSE_CI_PENDING`
    # is resolved by *conflict_fix_ceiling_sweep* onto its own report field
    # below, never by the generic tier/auto_dispatchers path. That path DOES
    # have a dispatcher registered for `CAUSE_CI_PENDING`
    # (`coord.board_doctor_policy`'s investigate tier, `sweep_dispatch_fixit_leg`)
    # — the exclusion is not "there is nothing to call", it is that this row
    # is already KNOWN not to need a worker: a conflict-fix leg already
    # pushed a resolution, so dispatching a fresh investigate-tier fix-it
    # leg here would spend that per-repo budget "fixing" a row that is not
    # broken, just waiting on CI to settle. Unlike `merged-partial`/`branch-merged-guard`,
    # this row is NOT dropped from *other_rows* — it is marked
    # `suppressed=True` instead (the same #3761 baseline-red carve-out
    # `sweep()` already honours): it stays `touched`, its convergence
    # counter is left exactly where it was, and `clear_absent_rows` is NOT
    # told it disappeared, so a live escalation from an EARLIER cause (the
    # row may have escalated before this sweep ever reclassified it
    # CI_PENDING) is never silently dismissed as resolved just because a
    # conflict-fix push happens to be in flight. Requeuing can take more
    # than one sweep (pending checks, a re-probe that fails) and this
    # exclusion must not read as "resolved" on any of those intermediate
    # rounds.
    ceiling_pending_keys = (
        frozenset(_ceiling_blocked_ci_pending_candidates(rows))
        if conflict_fix_ceiling_requeue and mechanical_causes is None
        else frozenset()
    )
    # #4050 review: unlike *ceiling_pending_keys* above, `CAUSE_INTEGRATION_
    # RED` does not self-clear — a required check red only because the base
    # moved stays red until something pushes a new head, so a row withdrawn
    # from the tier/alert path on the cheap candidate filter alone (the
    # pre-review shape of this block) would be suppressed forever on an
    # unconfirmed premise or a spent retry cap, with its convergence counter
    # frozen and no escalation ever reaching a human. `base_sync_fix_keys`
    # is therefore computed from this pass's own `dispatched=True` outcomes
    # ONLY, run here BEFORE the row loop below — the same "withdraw only
    # what this round confirmed" rule `config_drift_resolved_keys` already
    # applies a few lines down. A candidate whose premise never confirms,
    # or whose dispatch finds the retry cap already spent, is left in
    # `other_rows` so its convergence counter keeps advancing and still
    # escalates at `convergence_limit`.
    base_sync_fix_results: list[dict] = []
    if base_sync_fix_ceiling_dispatch and mechanical_causes is None and not dry_run:
        # Hard `dry_run` skip, same posture as *config_drift_fix_sweep* just
        # below: this pass always probes live and, on a confirmed premise,
        # always dispatches a leg, so a dry-run caller must see zero
        # dispatch and zero suppression from this lane, not a best-effort
        # one.
        try:
            base_sync_fix_results = base_sync_fix_ceiling_sweep(rows, config=config)
        except Exception:  # noqa: BLE001 — advisory, same posture as the
            # other best-effort passes in this function: a failed attempt
            # this round never takes the rest of this report down, and
            # every candidate stays exactly where `rows` found it for the
            # next sweep to retry.
            _log.warning(
                "board-doctor: base-sync-fix ceiling-blocked dispatch sweep "
                "failed this round",
                exc_info=True,
            )
    base_sync_fix_keys = frozenset(
        (outcome["repo"], outcome["issue"])
        for outcome in base_sync_fix_results
        if outcome.get("dispatched")
    )
    # #4048: run the Apply+Refund+requeue attempt BEFORE `sweep()` sees
    # *rows* at all — never after, unlike *conflict_fix_ceiling_sweep*
    # (which only ever waits on live CI, never mutates a second git
    # checkout) — a hard `dry_run` skip, same reasoning as that pass's own
    # posture but one notch stricter: this one commits outside the board
    # entirely, so a dry-run caller must see zero mutation there.
    config_drift_fix_results: list[dict] = []
    if config_drift_autofix_apply and mechanical_causes is None and not dry_run:
        try:
            config_drift_fix_results = config_drift_fix_sweep(
                config_drift_candidates(board, config), config=config,
            )
        except Exception:  # noqa: BLE001 — advisory, same posture as the
            # other best-effort passes in this function: a failed attempt
            # this round never takes the rest of this report down, and the
            # row stays exactly as `config_drift_rows` found it for the
            # next sweep to retry.
            _log.warning(
                "board-doctor: config-drift autofix sweep failed this round",
                exc_info=True,
            )
    # Only a key THIS round's attempt actually confirmed `requeued=True`
    # is withdrawn from the ordinary alert path below — never every
    # opted-in candidate regardless of outcome (#4036 review: pulling a
    # row out on a merely-attempted, not confirmed-resolved, action makes
    # its escalation vanish and dismisses any live one). A failed/refused
    # attempt leaves its row exactly where `config_drift_rows` put it, so
    # the alert tier's one-line-patch message still reaches a human.
    config_drift_resolved_keys = frozenset(
        f"{outcome['repo']}#{outcome['issue']}"
        for outcome in config_drift_fix_results
        if outcome.get("requeued")
    )
    for r in rows:
        if r.cause == CAUSE_MERGED_PARTIAL:
            continue
        if (
            r.cause == CAUSE_BRANCH_MERGED_GUARD
            and auto_refile
            and _branch_merged_guard_row_opted_in(r, config)
        ):
            refile_rows.append(r)
            continue
        if (
            r.cause == queue_diagnose.CAUSE_CONFIG_DRIFT
            and r.key in config_drift_resolved_keys
        ):
            continue
        if r.cause == queue_diagnose.CAUSE_CI_PENDING and r.queue == "drive-queue":
            from coord.drive_queue import parse_key as _parse_row_key  # noqa: PLC0415

            parsed = _parse_row_key(r.key)
            if parsed is not None and parsed in ceiling_pending_keys:
                other_rows.append(replace(r, suppressed=True))
                continue
        if r.cause == queue_diagnose.CAUSE_INTEGRATION_RED and r.queue == "drive-queue":
            from coord.drive_queue import parse_key as _parse_row_key  # noqa: PLC0415

            parsed = _parse_row_key(r.key)
            if parsed is not None and parsed in base_sync_fix_keys:
                other_rows.append(replace(r, suppressed=True))
                continue
        other_rows.append(r)
    if mechanical_causes is not None:
        other_rows = [r for r in other_rows if r.cause in mechanical_causes]
        followthrough_rows = []
        refile_rows = []
    resolved_capacity_check = (
        default_investigate_capacity(config)
        if capacity_check is _CAPACITY_CHECK_UNSET
        else capacity_check
    )

    routed_alert_dispatcher = alert_dispatcher
    if alert_dispatcher is not None:

        def _route_alert(row: StuckRow) -> bool | str:
            """#4039: an :data:`IDLE_FLEET_ROOT_QUEUE` row never goes
            through *alert_dispatcher* (``board_doctor_policy.
            escalation_alert_dispatcher`` in production) — that writer
            keys `drive_escalations` on ``(repo, issue)`` alone, and the
            root's OWN ordinary ``queue="drive-queue"`` row already owns
            that exact slot for the SAME cause, so a second writer there
            would almost always find itself "covered" and skip —
            precisely the quiet window this cause exists to page
            through. :func:`coord.notify.post_idle_fleet_root_alert`
            posts to the root issue directly instead, an independent
            channel with no such collision. Every other queue keeps
            going through *alert_dispatcher* exactly as before — and
            *alert_dispatcher* being ``None`` (the no-op default) is left
            completely untouched above, so `sweep`'s own ``"no-dispatcher"``
            bookkeeping for that case is unaffected by this routing.
            """
            if row.queue == IDLE_FLEET_ROOT_QUEUE:
                from coord import notify  # noqa: PLC0415

                return notify.post_idle_fleet_root_alert(row)
            return alert_dispatcher(row)

        routed_alert_dispatcher = _route_alert

    report = sweep(
        other_rows,
        tier_of=tier_of,
        auto_dispatchers=auto_dispatchers,
        alert_dispatcher=routed_alert_dispatcher,
        dry_run=dry_run,
        convergence_limit=convergence_limit,
        escalation_resolver=escalation_resolver,
        capacity_check=resolved_capacity_check,
        clear_absent_rows=mechanical_causes is None,
        log_retention_days=getattr(board_doctor_cfg, "log_retention_days", None),
        backoff_initial_hours=getattr(board_doctor_cfg, "backoff_initial_hours", None),
        backoff_cap_hours=getattr(board_doctor_cfg, "backoff_cap_hours", None),
        outcome_grace_hours=getattr(board_doctor_cfg, "outcome_grace_hours", None),
        counter_namespace=(
            MECHANICAL_SWEEP_COUNTER_NAMESPACE if mechanical_causes is not None else None
        ),
    )
    # `sweep()` already set `report.rows_observed = len(other_rows)` (the
    # *rows* it was actually handed) — only the unfiltered call needs
    # correcting here, to the FULL walk (`rows`, before the mechanical
    # filter/`merged-partial` split above), so "nothing stuck" keeps
    # meaning the whole board, not just this call's own narrowed subset.
    if mechanical_causes is None:
        report.rows_observed = len(rows)
    report.config_drift_fix = config_drift_fix_results
    report.base_sync_fix_ceiling_dispatch = base_sync_fix_results
    if followthrough and followthrough_rows:
        report.followthrough = followthrough_sweep(
            followthrough_rows, config=config, board=board, dry_run=dry_run,
        )
    if auto_refile and refile_rows:
        report.refile = refile_sweep(
            refile_rows, config=config, board=board, dry_run=dry_run,
        )
    if conflict_fix_ceiling_requeue and ceiling_pending_keys and not dry_run:
        # #4036: `dry_run` is a hard skip (never `conflict_fix_ceiling_
        # sweep(..., dry_run=...)`) because, unlike *followthrough_sweep*/
        # *refile_sweep*, this pass has no dry-run mode of its own — it
        # always probes live and, on a confirmed-green verdict, always
        # mutates the drive queue. A dry-run caller (`coord board-doctor
        # sweep --dry-run`) must see zero mutation, not a best-effort one.
        try:
            report.conflict_fix_ceiling_pending = conflict_fix_ceiling_sweep(
                rows, config=config,
            )
        except Exception:  # noqa: BLE001 — advisory, same posture as the
            # refile-close/predicted-after-edges passes just below: a
            # failed requeue attempt this round never takes the rest of
            # this report down, and the row stays ceiling-blocked
            # (unchanged) for the next sweep to retry.
            _log.warning(
                "board-doctor: conflict-fix ceiling-pending requeue sweep "
                "failed this round",
                exc_info=True,
            )
    if auto_refile and mechanical_causes is None:
        try:
            report.refile_closed = refile_close_sweep(config=config, dry_run=dry_run)
        except Exception:  # noqa: BLE001 — advisory only
            # Same posture as *predicted_after_edges_sweep* just below: a
            # failed pending-close check never takes the rest of this
            # report down, and the pending state it reads is durable (the
            # decision log), so a skipped round just means the close
            # happens next sweep.
            _log.warning(
                "board-doctor: pending refile-close sweep failed this round",
                exc_info=True,
            )
    if drop_predicted_after_edges and mechanical_causes is None:
        try:
            report.dropped_predicted_after_edges = predicted_after_edges_sweep(
                dry_run=dry_run, config=config,
            )
        except Exception:  # noqa: BLE001 — advisory, see docstring
            _log.warning(
                "board-doctor: predicted after= edge sweep failed this round",
                exc_info=True,
            )
    return report


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
