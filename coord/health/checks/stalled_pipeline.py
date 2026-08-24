"""Machine-scope: pipeline rows terminally stalled with no fix in flight (#2679).

``coord.notify.detect_stalled_pipeline`` (#1441) already re-scans the board
each ``coord notify`` pass and correctly classifies a row stuck on an unmet
precondition. The gap is what happens after it fires: the row is announced
into a GitHub issue comment exactly once, and ``_stalled_notified_key`` then
suppresses it from every later sweep, permanently — the ``notified`` ledger
that exists to stop comment spam for chatty transient states is the wrong
contract for a state that cannot resolve itself. Three rows (claude-
coordinator#1823, vimcode#617, quadraui#595) sat announced-then-invisible for
eight days; #1823 alone deadlocked seven chained drive-queue entries.

This check is the durable fix for *visibility*: it re-derives the same rows
``detect_stalled_pipeline`` would flag right now by calling it with
``ignore_notified=True`` (#2679), so a row already sitting in the ``notified``
ledger still reports here on every run — the check has no ledger of its own
to go stale. It never posts, dispatches, or mutates anything; it is exactly
as read-only as ``detect_stalled_pipeline`` itself. The one-shot GitHub
comment (``coord.notify.post_stalled_pipeline``) is intentionally untouched —
that ledger still does its job of not spamming the issue thread.

``review_request_changes_no_fix``, ``merge_conflict_unresolved``, and
``review_done_no_verdict`` are CRIT here: none of them has an owner once the
drive session that would have reacted to the transition has already exited
(#1692's fix loop only runs inside a live drive session) — nothing but a
fresh dispatch or the issue going terminal clears them. The other reasons
(``done_no_review``, ``approved_not_queued``, ``review_failed_no_verdict``)
are WARN — routinely cleared by the very next `coord notify`/`coord drive`
pass reaching them, so persistence there is a milder signal.
"""

from __future__ import annotations

from coord.health.models import CheckResult, HealthContext, Severity
from coord.health.registry import COST_NETWORK, check

# Reason kinds `detect_stalled_pipeline` flags that cannot self-resolve: no
# drive session owns them once it has exited, so nothing but a fresh
# dispatch (or the underlying issue going terminal) ever clears them. All
# three #2679 reference rows were this shape.
TERMINAL_STALL_REASONS = frozenset(
    {
        "review_request_changes_no_fix",
        "merge_conflict_unresolved",
        "review_done_no_verdict",
    }
)


@check(
    id="stalled_pipeline",
    scope="machine",
    title="stalled pipeline rows",
    order=22,
    cost=COST_NETWORK,
    description=(
        "Pipeline rows `detect_stalled_pipeline` would flag right now, "
        "re-derived from live board state on every run — independent of "
        "the one-shot GitHub-comment `notified` ledger, so a row already "
        "announced once does not go invisible (#2679). Marked "
        "`cost=network`: for every already-`done` head, `detect_stalled_"
        "pipeline` calls `github_ops.work_is_terminal`, a real `gh` CLI "
        "round-trip per row, so this is excluded from the cheap set and "
        "`coord health --no-network` (and the automatic per-agent "
        "`/health` poll, which runs with `allow_network=False`) skip it."
    ),
)
def probe_stalled_pipeline(ctx: HealthContext) -> CheckResult:
    if ctx.config is None:
        return CheckResult(
            check_id="stalled_pipeline",
            scope="machine",
            severity=Severity.UNKNOWN,
            headroom="no coordinator.yml loaded",
        )

    try:
        from coord.notify import detect_stalled_pipeline  # noqa: PLC0415

        detections = detect_stalled_pipeline(ctx.config, ignore_notified=True)
    except Exception as exc:  # noqa: BLE001 — a probe must never raise
        return CheckResult(
            check_id="stalled_pipeline",
            scope="machine",
            severity=Severity.UNKNOWN,
            headroom=f"could not sweep the board: {exc}",
            error=str(exc),
        )

    if not detections:
        return CheckResult(
            check_id="stalled_pipeline",
            scope="machine",
            severity=Severity.OK,
            headroom="0 stalled pipeline rows",
        )

    terminal = [(d, w) for d, w in detections if d.reason in TERMINAL_STALL_REASONS]
    transient = [(d, w) for d, w in detections if d.reason not in TERMINAL_STALL_REASONS]
    total = len(detections)

    severity = Severity.CRIT if terminal else Severity.WARN
    worst = terminal or transient
    sample = ", ".join(
        f"{d.repo_name}#{d.issue_number} ({d.reason})" for d, _work in worst[:5]
    )

    return CheckResult(
        check_id="stalled_pipeline",
        scope="machine",
        severity=severity,
        headroom=(
            f"{len(terminal)} terminal, {len(transient)} transient stalled "
            f"row{'s' if total != 1 else ''}"
        ),
        detail=f"e.g. {sample}" + (", ..." if len(worst) > 5 else ""),
        threshold=(
            "crit when a terminal stall is found (review request-changes "
            "with no fix dispatched, an unresolved rebaseable merge "
            "conflict, or a review that finalized with no verdict); warn "
            "for any other stalled-pipeline reason"
        ),
        values={
            "total": total,
            "terminal": len(terminal),
            "transient": len(transient),
            "rows": [
                {
                    "assignment_id": d.assignment_id,
                    "repo_name": d.repo_name,
                    "issue_number": d.issue_number,
                    "reason": d.reason,
                    "machine_name": d.machine_name,
                    "terminal": d.reason in TERMINAL_STALL_REASONS,
                }
                for d, _work in detections
            ],
        },
    )
