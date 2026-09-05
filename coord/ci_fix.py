"""Auto-dispatch a fix worker for a CONFIRMED CI check failure at the Merge
gate (#2510).

``coord/merge_queue.py``'s CI gate already classifies a failing check into
three shapes: a verdictless (infra) failure auto-reruns up to
``MAX_CI_INFRA_RERUNS``; a verdicted failure gets one flake re-check up to
``MAX_CI_FLAKY_RERUNS``; and once both budgets are exhausted/inapplicable the
failure is CONFIRMED — real, not infra, not (as far as one re-run can tell)
flaky. Both exhaustion points emit a ``"checks_failed"`` :class:`~coord.
merge_queue.MergeEvent`. Before this module existed that event did nothing
but set ``entry.error`` — the entry stayed ``PENDING`` in the merge queue
forever, invisible to any status view that only surfaces ``HUMAN_REQUIRED``
rows, with no automated path back to a fix.

Why a separate module, following ``coord/conflict_fix.py``'s lead: triggered
by a merge_queue event, not a planner proposal, so it shares little with
``coord.dispatch``.

Unlike a merge CONFLICT (mechanical, content-preserving, handled by a
dedicated ``type="conflict-fix"`` worker whose success needs no fresh
Test/Review since the diff is unchanged), a confirmed CI failure needs a
worker that can genuinely change code — the same shape as a review
``request-changes`` bounce. So this dispatches via
:func:`coord.auto_loop._dispatch_fix` itself (not a bespoke HTTP POST): the
new commit lands on the SAME branch/PR, and the existing generic Work→Test→
Review pipeline (already built to re-drive Test then Review for any bounced
``WORK_LIKE_TYPES`` fix) picks the new commit up on its own — no separate
re-review wiring needed here. The merge queue's own ``branch_head_sha``/
``branch_patch_id`` staleness check (see ``has_approved_review``) already
invalidates the stale approval once the new commit lands, so the PR
naturally cannot merge again until it clears Test + Review + CI fresh.
"""

from __future__ import annotations

import logging

import httpx

from coord.ci_store import CIFailureDetail
from coord.config import Config
from coord.merge_queue import QueuedMerge, _chain_work_ids
from coord.models import Assignment, Board

_log = logging.getLogger(__name__)


# #2510: bounded the same shape as MAX_CI_INFRA_RERUNS/MAX_CI_FLAKY_RERUNS —
# a couple of genuine attempts is enough to tell "this is fixable by a
# worker" from "this needs a human"; a higher cap just burns more of the
# same exhausted budget the way an unbounded conflict-fix retry would.
MAX_CI_FIX_DISPATCHES = 2

# #3011: a ci-fix leg that finishes with the branch HEAD unchanged (see
# `dispatch_was_noop`) is refunded and does NOT count toward
# MAX_CI_FIX_DISPATCHES above — but that refund must itself be bounded, or
# two workers that keep (correctly) declining could cycle forever without
# ever reaching a verdict. Same magnitude as MAX_CI_FIX_DISPATCHES for the
# same "a couple of genuine signals is enough" reasoning: once this many
# CONSECUTIVE legs come back having pushed nothing, that itself is the
# verdict — the failure is not attributable to this branch — and
# `coord.commands.merge._dispatch_ci_fixes` escalates to HUMAN_REQUIRED
# with a reason that says so, distinct from the generic retry-cap message.
MAX_CI_FIX_NOOP_STREAK = 2

# Prefix on the dispatched fix worker's issue_title — mirrors
# `conflict_fix.SEMANTIC_FIX_TITLE_PREFIX`'s visibility purpose: the TUI
# Pipeline row and the fix briefing itself both make it obvious WHY this
# round was dispatched (a CI failure, not a review request-changes bounce).
CI_FIX_TITLE_PREFIX = "[ci-fix]"


def _detail_has_content(detail: CIFailureDetail) -> bool:
    """True when *detail* carries anything :func:`_format_ci_failure_detail`
    would actually render (#3114 review nit).

    ``build_ci_failure_detail`` can return a non-``None`` ``CIFailureDetail``
    with only ``check_name`` populated — e.g. a failed check whose ``run_id``
    was empty, or whose job name never matched any job on the run (see
    ``test_no_matching_job_leaves_job_and_step_empty``). ``check_name`` alone
    isn't rendered by ``_format_ci_failure_detail`` (the plain
    ``checks_summary`` line right above it already names the check), so
    without this check the briefing would grow a "## CI failure detail"
    section containing nothing but its own header — harmless, but pointless
    noise. Treated identically to ``detail is None``.
    """
    return bool(
        detail.job_name or detail.step_name or detail.run_url or detail.log_excerpt
    )


def _format_ci_failure_detail(detail: CIFailureDetail) -> list[str]:
    """Render *detail* into briefing lines (#3114).

    ``checks_summary`` alone is a one-line rollup (e.g. "checks failed:
    Test (Linux, headless) (failure)") — no job name, no failing test, no
    log. This is the section that fills that gap with what
    ``list_jobs_for_run``/the failing step's log already told the
    coordinator, so a ci-fix worker doesn't have to spend a whole session
    rediscovering it from scratch (see the issue's evidence: 82 turns/$2.55
    to re-find a one-line fix this data already pointed at).
    """
    lines: list[str] = ["## CI failure detail", ""]
    if detail.job_name:
        lines.append(f"Failing job: {detail.job_name}")
    if detail.step_name:
        lines.append(f"Failing step: {detail.step_name}")
    if detail.run_url:
        lines.append(f"Run: {detail.run_url}")
    if detail.log_excerpt:
        lines.append("")
        # #3114 acceptance: truncation must be visible in the text itself,
        # never a silent cut.
        lines.append(
            "Log excerpt (truncated — showing the tail only):"
            if detail.truncated else "Log excerpt:"
        )
        lines.append("```")
        lines.append(detail.log_excerpt)
        lines.append("```")
    lines.append("")
    return lines


def build_ci_fix_briefing(
    *,
    entry: QueuedMerge,
    checks_summary: str,
    attempt: int,
    detail: CIFailureDetail | None = None,
) -> str:
    """Assemble the CI-fix worker's briefing. Pure function — testable.

    *detail* (#3114) is the structured failing-job/step/log-excerpt data
    :func:`coord.ci_github.build_ci_failure_detail` fetches at dispatch
    time — optional and additive: when it's ``None``, or non-``None`` but
    empty of anything beyond the check name (see :func:`_detail_has_content`),
    the briefing is byte-identical to before #3114, still carrying
    ``checks_summary``.
    """
    lines: list[str] = [
        f"# CI failure fix: {entry.repo_github} branch `{entry.branch}`",
        "",
        f"Issue #{entry.issue_number} — {entry.issue_title}",
        "",
        "Review already approved this PR and it reached the Merge gate, "
        "where GitHub Actions CI reported a REAL failure (not infra, and "
        "not resolved by one automatic re-run) on the checks below:",
        "",
        f"    {checks_summary}",
        "",
    ]
    if detail is not None and _detail_has_content(detail):
        lines.extend(_format_ci_failure_detail(detail))
    lines += [
        f"This is fix attempt {attempt}/{MAX_CI_FIX_DISPATCHES} for this "
        "failure streak — the coordinator will escalate to a human if this "
        "many attempts don't produce a green run.",
        "",
        "## What to do",
        "",
        "1. You are already on this issue's existing branch — continue on "
        "it, do NOT start a fresh branch.",
        "2. Reproduce the failure locally if the project's test command "
        "covers it (`coord`'s Test stage already runs the same diff-scoped "
        "suite CI reruns in full — a failure CI alone caught may live in a "
        "suite that only runs in CI, e.g. a sealed acceptance suite).",
        "3. Fix the actual regression. Do not edit the failing check's "
        "workflow config to make it pass without fixing the underlying "
        "issue, and do not skip/xfail the failing test unless the test "
        "itself is proven wrong.",
        "4. Commit and push to the SAME branch. The coordinator re-runs "
        "Test, Review, and CI from scratch on the new commit — this is not "
        "a force-merge shortcut.",
        "",
        "You will NOT use `gh` — the coordinator owns PR/issue interaction "
        "and CI status reads.",
        "",
        f"Last merge-gate error: {entry.error or checks_summary}",
    ]
    return "\n".join(lines)


def _has_active_fix(board: Board, entry: QueuedMerge) -> bool:
    """True when a WORK_LIKE fix for *entry*'s chain is already running or
    pending — regardless of whether it was dispatched for this CI failure or
    an unrelated review bounce; either way a second dispatch here would race
    it onto the same branch.
    """
    pool = list(board.completed) + list(board.active)
    chain_ids = _chain_work_ids(entry, pool)
    active_ids = {
        getattr(a, "assignment_id", None) for a in board.active
    } - {None}
    for a in pool:
        aid = getattr(a, "assignment_id", None)
        if aid is None or aid not in chain_ids or aid == entry.assignment_id:
            continue
        if aid in active_ids and getattr(a, "status", None) in ("running", "pending"):
            return True
    return False


def dispatch_was_noop(entry: QueuedMerge) -> bool:
    """True when the LAST ci-fix dispatch for *entry* completed with the
    branch HEAD unchanged — the worker correctly concluded the CI failure
    wasn't its to fix and pushed no commit, rather than genuinely attempting
    a fix. Compares the live, per-tick-refreshed ``entry.branch_head_sha``
    (see ``coord.merge_queue.process``'s freshness-anchor refresh, which
    runs before a ``checks_failed`` event is ever emitted) against
    ``entry.ci_fix_head_sha``, the sha :func:`dispatch_ci_fix` snapshotted
    at the moment of that dispatch.

    Both empty (no ci-fix ever dispatched for this streak, or a probe
    failure left the current SHA unknown) reads as ``False`` — fails
    closed, same as every other SHA-staleness check in this codebase
    (#821): "we can't tell" must never be treated as "we can tell it
    didn't move".
    """
    return (
        bool(entry.ci_fix_head_sha)
        and bool(entry.branch_head_sha)
        and entry.ci_fix_head_sha == entry.branch_head_sha
    )


def refund_noop_ci_fix(entry: QueuedMerge) -> None:
    """Undo the attempt :func:`dispatch_ci_fix` speculatively spent for a
    leg that turned out to be a no-op (:func:`dispatch_was_noop` is
    ``True``), and track the no-op streak separately so it cannot cycle
    forever without ever reaching a verdict — see
    :data:`MAX_CI_FIX_NOOP_STREAK`.

    Called by ``coord.commands.merge._dispatch_ci_fixes`` BEFORE it decides
    whether to dispatch again for this entry's event; the caller is
    responsible for checking ``entry.ci_fix_noop_streak`` against the cap
    afterwards and escalating/persisting as needed, same division of
    responsibility as :func:`dispatch_ci_fix` itself.
    """
    entry.ci_fix_dispatches = max(0, entry.ci_fix_dispatches - 1)
    entry.ci_fix_noop_streak += 1
    entry.ci_fix_head_sha = ""


def dispatch_precheck(
    entry: QueuedMerge, board: Board, *, log: bool = True,
) -> Assignment | None:
    """Return the originating work :class:`Assignment` when *entry* is
    otherwise eligible for a fresh ci-fix dispatch — ``None`` when the retry
    cap is already spent, a fix for this chain is already in flight, or the
    original work assignment can't be found on *board*.

    Extracted out of :func:`dispatch_ci_fix` (#3114 review fix) so a caller
    that wants to fetch expensive CI-failure detail (:func:`coord.ci_github.
    build_ci_failure_detail` — a network call, `gh api .../actions/jobs/{id}
    /logs`) can check these conditions FIRST and skip the fetch entirely for
    an entry that is going to be declined for one of these reasons anyway —
    without duplicating this logic. Does not cover every way
    :func:`dispatch_ci_fix` can still return ``None`` afterward (the
    underlying ``_dispatch_fix`` HTTP dispatch itself can still decline: no
    capable machine, agent unreachable, the #2538 DB-lock-contention case)
    — those aren't knowable without actually attempting the dispatch, so a
    ``True``-ish return here is necessary, not sufficient, for a dispatch to
    succeed.

    *log* controls whether a "work assignment not found" outcome is logged
    — the caller doing the up-front eligibility check should pass ``False``
    to avoid double-logging the same warning that :func:`dispatch_ci_fix`
    itself will also emit when it re-derives the same ``None`` a moment
    later.
    """
    if entry.ci_fix_dispatches >= MAX_CI_FIX_DISPATCHES:
        return None
    if _has_active_fix(board, entry):
        return None
    if entry.assignment_id is None:
        return None
    work = board.find_by_id(entry.assignment_id)
    if work is None:
        if log:
            _log.warning(
                "ci_fix: cannot find original work assignment %s for %s#%d — "
                "no fix dispatched",
                entry.assignment_id, entry.repo_name, entry.issue_number,
            )
        return None
    return work


def dispatch_ci_fix(
    entry: QueuedMerge,
    board: Board,
    config: Config,
    *,
    checks_summary: str | None = None,
    detail: CIFailureDetail | None = None,
    http_client: httpx.Client | None = None,
) -> Assignment | None:
    """Dispatch a fix worker for *entry*'s confirmed CI failure.

    *detail* (#3114): structured failing-job/step/log-excerpt data the
    caller (``coord.commands.merge._dispatch_ci_fixes``) fetched via
    ``coord.ci_github.build_ci_failure_detail`` — threaded straight into
    :func:`build_ci_fix_briefing`. Optional; ``None`` produces the same
    briefing this function always has.

    Returns the new ``Assignment``, or ``None`` when dispatch couldn't
    proceed: the retry cap (:data:`MAX_CI_FIX_DISPATCHES`) is already spent,
    a fix for this chain is already in flight, the original work assignment
    can't be found on *board*, or the underlying ``_dispatch_fix`` call
    itself declined (no capable machine, agent unreachable, …). The caller
    (``coord.commands.merge._dispatch_ci_fixes``) is responsible for
    promoting *entry* to ``HUMAN_REQUIRED`` when this returns ``None`` with
    the retry cap exhausted, and for persisting the board/queue.

    Does NOT increment ``entry.ci_fix_dispatches`` on a ``None`` return —
    only a successful dispatch spends the budget, mirroring how
    ``ci_infra_reruns``/``ci_flaky_reruns`` are only bumped when their
    respective remedy actually fired.

    #3011: a successful dispatch also snapshots ``entry.branch_head_sha``
    into ``entry.ci_fix_head_sha`` so a later tick can tell whether this
    leg actually moved the branch — see ``dispatch_was_noop``/
    ``refund_noop_ci_fix``. Callers are expected to check
    ``dispatch_was_noop(entry)`` and route through ``refund_noop_ci_fix``
    BEFORE calling this function again for the same entry, so a worker
    that pushed nothing doesn't silently spend a second real attempt.
    """
    work = dispatch_precheck(entry, board)
    if work is None:
        return None

    summary = checks_summary or entry.error or "CI checks failed"
    briefing = build_ci_fix_briefing(
        entry=entry, checks_summary=summary, attempt=entry.ci_fix_dispatches + 1,
        detail=detail,
    )

    from coord.auto_loop import _dispatch_fix  # noqa: PLC0415

    fix = _dispatch_fix(
        work, briefing, board, config, entry.ci_fix_dispatches + 1,
        http_client=http_client,
    )
    if fix is None:
        return None

    # #2510 visibility: mark the row so the TUI/board can tell a CI-triggered
    # round apart from an ordinary review-bounce fix at a glance, same
    # purpose as conflict_fix's SEMANTIC_FIX_TITLE_PREFIX.
    fix.issue_title = f"{CI_FIX_TITLE_PREFIX} {work.issue_title}"

    # #3011: if a PRIOR dispatch's snapshot is still on the entry and the
    # branch has since moved past it, that prior leg was a genuine attempt
    # (not a no-op) — clear any no-op streak it may have left behind so it
    # doesn't linger into this fresh attempt. (When the branch has NOT
    # moved, the caller — `coord.commands.merge._dispatch_ci_fixes` — is
    # expected to have already routed through `dispatch_was_noop`/
    # `refund_noop_ci_fix` instead of reaching here; this is a harmless
    # no-op in that case too, since `entry.ci_fix_noop_streak` would already
    # be 0 for a caller that follows the intended sequencing.)
    if (
        entry.ci_fix_head_sha
        and entry.branch_head_sha
        and entry.ci_fix_head_sha != entry.branch_head_sha
    ):
        entry.ci_fix_noop_streak = 0

    # Snapshot the current branch HEAD so a later tick can tell whether
    # THIS dispatch's leg actually moved the branch (see
    # `dispatch_was_noop`). `branch_head_sha` may itself be None (a probe
    # failure) — falls back to "" so an unknown SHA never spuriously reads
    # as "no ci-fix dispatch pending" on the next comparison.
    entry.ci_fix_head_sha = entry.branch_head_sha or ""

    entry.ci_fix_dispatches += 1

    from coord.audit import record_audit  # noqa: PLC0415

    record_audit(
        tier="operational",
        category="merge",
        event_type="ci_fix_dispatched",
        actor="daemon",
        summary=(
            f"ci-fix dispatched ({entry.ci_fix_dispatches}/"
            f"{MAX_CI_FIX_DISPATCHES}): {entry.repo_name}#{entry.issue_number} "
            f"-> {fix.machine_name}"
        ),
        repo=entry.repo_name,
        issue=entry.issue_number,
        assignment_id=fix.assignment_id,
        machine=fix.machine_name,
        details={
            "merge_entry_id": entry.assignment_id,
            "ci_fix_dispatches": entry.ci_fix_dispatches,
            "checks_summary": summary,
            "ci_fix_job": detail.job_name if detail else None,
            "ci_fix_step": detail.step_name if detail else None,
        },
    )

    return fix
