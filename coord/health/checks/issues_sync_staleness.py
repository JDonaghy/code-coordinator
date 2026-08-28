"""Fleet-scope: per-repo issues-cache staleness (#2858).

``coord.serve_app._sync_issues_tick`` refreshes the local ``issues`` cache
every repo needs — and before this check existed, a repo that stopped
refreshing (the 2026-08-27 incident: a shared ``gh`` backoff latch,
:mod:`coord.github_throttle`, re-armed by faster pollers every ~80s outbid
this tick's 300s cadence for 39 minutes straight) looked IDENTICAL to a
fresh cache everywhere an operator would look — ``coord status``, ``coord
health``, the board itself. That silence is what let the staleness cascade:
``coord.drive_queue.IssueFacts.landed`` reads this same cache with no notion
of its own age, so a merged-and-closed issue kept reporting ``open`` and the
drive queue blocked every dependent on it.

This check makes that age visible. It reads
``ctx.fleet.daemon_host["issues_sync_status"]`` — a per-repo
``{last_success_at, last_attempt_at, last_error}`` snapshot
:mod:`coord.health.fleet_snapshot`'s ``FleetHealthRefresher`` stamps from
:mod:`coord.issues_sync_status` on its own tick, the same "daemon-host-local
fact" shape :mod:`coord.health.checks.fleet_board` already established for
``/board``'s own latency+size. Like that check, this only reports something
real on the daemon host — everywhere else ``ctx.fleet`` is ``None`` and the
result is ``unknown``.
"""

from __future__ import annotations

from coord.health.models import CheckResult, HealthContext, Severity
from coord.health.registry import check
from coord.issues_sync_status import STALENESS_CRIT_SECONDS, STALENESS_WARN_SECONDS


def _configured_repo_names(ctx: HealthContext) -> list[str]:
    repos = getattr(ctx.config, "repos", None) or ()
    return [r.name for r in repos if getattr(r, "name", None)]


@check(
    id="issues_sync_staleness",
    scope="fleet",
    title="issues sync",
    order=21,
    description=(
        "How long since each repo's issues cache last synced successfully "
        "(#2858) — a stale cache silently freezes IssueFacts.landed."
    ),
)
def probe_issues_sync_staleness(ctx: HealthContext) -> list[CheckResult] | CheckResult:
    if ctx.fleet is None:
        return CheckResult(
            check_id="issues_sync_staleness",
            scope="fleet",
            severity=Severity.UNKNOWN,
            headroom="no fleet snapshot (fleet checks only run on the daemon)",
        )

    status = (ctx.fleet.daemon_host or {}).get("issues_sync_status")
    if status is None:
        return CheckResult(
            check_id="issues_sync_staleness",
            scope="fleet",
            severity=Severity.UNKNOWN,
            headroom="no issues-sync data yet",
        )

    repo_names = _configured_repo_names(ctx)
    if not repo_names:
        # No config to enumerate against (e.g. a bare context) — fall back
        # to whatever repos the status snapshot itself knows about, so this
        # check still says something rather than reporting nothing at all.
        repo_names = sorted(status.keys())

    results: list[CheckResult] = []
    for repo_name in repo_names:
        row = status.get(repo_name)
        if row is None:
            results.append(
                CheckResult(
                    check_id="issues_sync_staleness",
                    scope="fleet",
                    subject=repo_name,
                    severity=Severity.UNKNOWN,
                    headroom="never synced",
                )
            )
            continue

        last_success_at = row.get("last_success_at")
        last_error = row.get("last_error")

        if last_success_at is None:
            results.append(
                CheckResult(
                    check_id="issues_sync_staleness",
                    scope="fleet",
                    subject=repo_name,
                    severity=Severity.UNKNOWN,
                    headroom="never synced successfully",
                    detail=last_error or "",
                    values=dict(row),
                )
            )
            continue

        age_s = max(0.0, ctx.now - last_success_at)
        if age_s >= STALENESS_CRIT_SECONDS:
            severity = Severity.CRIT
        elif age_s >= STALENESS_WARN_SECONDS:
            severity = Severity.WARN
        else:
            severity = Severity.OK

        age_min = age_s / 60.0
        headroom = f"synced {age_min:.0f}m ago"
        if severity is not Severity.OK and last_error:
            headroom += f" (last error: {last_error[:120]})"

        results.append(
            CheckResult(
                check_id="issues_sync_staleness",
                scope="fleet",
                subject=repo_name,
                severity=severity,
                headroom=headroom,
                threshold=(
                    f"warn at {STALENESS_WARN_SECONDS / 60:.0f}m, "
                    f"crit at {STALENESS_CRIT_SECONDS / 60:.0f}m"
                ),
                values=dict(row, age_s=age_s),
            )
        )

    return results
