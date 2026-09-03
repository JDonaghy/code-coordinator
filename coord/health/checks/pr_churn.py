"""A branch whose PRs open and close in a loop (#3064).

The #3063 incident: between 02:54 and 12:40 the daemon opened and closed 102
PRs on one repo (#201-#300) for a single branch, then spent another two
hours retrying ``gh pr create`` roughly once a minute against a hard GitHub
ceiling. Throughout, ``coord health`` reported clean — ``coord status``
looked normal too. The merge-queue row backing the loop was deleted and
recreated every tick, so it was also absent from most board snapshots the
TUI polls.

Every check that existed before this one is keyed to a row *sitting still*:
``wedged_drive_queue``, ``stalled_pipeline``, ``fleet_phantom_running``. A
row moving in a circle — opened, closed, opened, closed — is invisible to a
stall detector precisely because it never stalls. This check is the first
instance of a different class: "the fleet is doing work that produces
nothing."

**Signal**: every PR the daemon opens via auto-drain or auto-revalidate
already lands a durable ``merge_opened`` row in the audit trail
(``coord.merge_queue.process``'s ``MergeEvent(entry, "opened", ...)``,
recorded by ``coord.serve_app._auto_drain_tick``/``_auto_revalidate_tick``)
— no new GitHub call needed, this check only re-reads what already exists.
A normal branch produces exactly one such row over its whole lifetime; a
legitimately rebased/reopened one a handful at most. More than
``pr_churn_crit_count`` for one ``(repo, issue)`` inside a rolling
``pr_churn_window_hours`` window is never legitimate — it is a sweep
fighting itself.

**Grouping key is ``(repo, issue)``, not the literal branch string**: the
audit row's structured columns carry ``repo``/``issue`` but not a dedicated
``branch`` field (only embedded in the free-text ``summary``, best-effort
parsed here for *display* only). Branch names are deterministically derived
from the issue (``f"issue-{issue_number}-{slug}"``, e.g.
``coord.state``'s proposal-branch construction and ``coord.agent``'s
dispatch-time equivalent), so ``(repo, issue)`` and "head branch" agree for
the overwhelming majority of rows; grouping/severity never depend on the
parsed branch string, only the structured columns.

CRIT, not WARN: unlike a wedged row (costs an operator ~10 seconds once
they know), a churning branch is actively burning API quota and merge-queue
cycles *right now*, and #3063 showed it can run for ten-plus hours
completely unnoticed — this is the outage this check exists to end.

``cost=COST_NETWORK``: reads via ``coord.state.list_audit_log``, which
routes to the daemon over HTTP whenever ``board_service`` is set (the
common case on a thin-client fleet machine, same precedent as
``wedged_drive_queue``/``release_cordon``) — so ``--no-network``/timer runs
and the automatic per-agent health poll skip it exactly like the other
network-costed checks.
"""

from __future__ import annotations

from coord.health.models import CheckResult, HealthContext, Severity
from coord.health.registry import COST_NETWORK, check

# Safety cap on how many audit rows one run pages through — bounds worst-case
# cost the same way `coord.forge_availability.availability_report`'s
# `_MAX_REPORT_ROWS` bounds its own audit-log scan. A single-branch loop this
# large would already be far past CRIT long before hitting the cap.
_MAX_AUDIT_ROWS = 5_000

# One page per `coord.state.list_audit_log` round trip — `coord.audit.
# query_audit_log`'s own MAX_LIMIT, so every page is as large as the server
# will ever hand back.
_PAGE_LIMIT = 500


@check(
    id="pr_churn",
    scope="machine",
    title="PR churn",
    order=24,
    cost=COST_NETWORK,
    description=(
        "A (repo, branch) that has opened more than `pr_churn_crit_count` "
        "PRs in a rolling `pr_churn_window_hours` window — a self-undoing "
        "open/close loop, invisible to every stall-keyed check because it "
        "never stalls (#3064/#3063). Sourced from the `merge_opened` audit "
        "trail rows the daemon already records on every auto-drain/auto-"
        "revalidate PR-open, so this needs no new GitHub call. `cost="
        "network`: `coord.state.list_audit_log` routes to the daemon over "
        "HTTP when `board_service` is set, same precedent as `wedged_"
        "drive_queue`; `--no-network`/timer runs and the automatic per-"
        "agent health poll skip it accordingly."
    ),
)
def probe_pr_churn(ctx: HealthContext) -> CheckResult:
    window_hours = float(getattr(ctx.thresholds, "pr_churn_window_hours", 24.0))
    crit_count = int(getattr(ctx.thresholds, "pr_churn_crit_count", 3))
    since = ctx.now - window_hours * 3600.0

    from coord.state import list_audit_log  # noqa: PLC0415

    entries: list[dict] = []
    cursor: str | None = None
    try:
        while True:
            page = list_audit_log(
                since=since,
                until=ctx.now,
                category="merge",
                event_type="merge_opened",
                limit=_PAGE_LIMIT,
                cursor=cursor,
            )
            entries.extend(page["entries"])
            if len(entries) >= _MAX_AUDIT_ROWS or not page["has_more"]:
                break
            cursor = page["next_cursor"]
    except Exception as exc:  # noqa: BLE001 — a probe must never raise
        return CheckResult(
            check_id="pr_churn",
            scope="machine",
            severity=Severity.UNKNOWN,
            headroom=f"could not read the audit log: {exc}",
            error=str(exc),
        )

    groups: dict[tuple[str, int], list[dict]] = {}
    for entry in entries:
        repo = entry.get("repo")
        issue = entry.get("issue")
        if repo is None or issue is None:
            continue
        groups.setdefault((repo, issue), []).append(entry)

    churning = {key: rows for key, rows in groups.items() if len(rows) > crit_count}

    if not churning:
        return CheckResult(
            check_id="pr_churn",
            scope="machine",
            severity=Severity.OK,
            headroom=f"0 churning branches in the last {window_hours:g}h",
            values={"window_hours": window_hours, "threshold": crit_count},
        )

    def _branch_for(rows: list[dict]) -> str:
        # Best-effort cosmetic extraction from the free-text summary ("...
        # for <branch>") — never a grouping/severity input, display only.
        summary = (rows[0].get("summary") or "") if rows else ""
        if " for " in summary:
            return summary.rsplit(" for ", 1)[-1].strip()
        return "?"

    worst = sorted(churning.items(), key=lambda kv: len(kv[1]), reverse=True)
    sample = ", ".join(
        f"{repo}#{issue} ({_branch_for(rows)}, {len(rows)} opens)"
        for (repo, issue), rows in worst[:5]
    )

    return CheckResult(
        check_id="pr_churn",
        scope="machine",
        severity=Severity.CRIT,
        headroom=(
            f"{len(churning)} branch{'es' if len(churning) != 1 else ''} "
            f"churning PRs in the last {window_hours:g}h"
        ),
        detail=(
            f"e.g. {sample}" + (", ..." if len(worst) > 5 else "")
            + " — a normal branch opens one PR, a rebased/reopened one a "
            "handful at most; this many `merge_opened` audit rows for one "
            "branch is a self-undoing open/close loop, not legitimate "
            "churn (#3064/#3063). Stop the loop first (`coord drive-queue "
            "remove <repo> <issue>`, or pause auto-drain/auto-revalidate), "
            "THEN diagnose why it kept re-opening — check the daemon log "
            "for `auto-drain: opened`/`auto-revalidate: opened` and "
            "`close_stale_prs`/`prune_stale_queue_entries` sweeps racing "
            "against it before re-adding."
        ),
        threshold=(
            f"crit when any (repo, branch) has more than {crit_count} "
            f"`merge_opened` audit rows in a rolling {window_hours:g}h "
            "window — a normal branch never exceeds this"
        ),
        values={
            "window_hours": window_hours,
            "threshold": crit_count,
            "branches": [
                {
                    "repo": repo,
                    "issue": issue,
                    "branch": _branch_for(rows),
                    "count": len(rows),
                }
                for (repo, issue), rows in worst
            ],
        },
    )
