"""Drive-queue entries stuck in a guaranteed-false wait (#2944).

``coord drive-queue status``'s ``alert:`` line is a per-tick record — a busy
queue with free capacity elsewhere clears it every tick that finds
*something* to launch (see ``coord.commands.drive_queue._run_tick``'s own
``else: _clear_queue_alert()`` branch), even while an unrelated entry sits
wedged for hours. That is exactly what happened to claude-coordinator#2900
and #2907 on 2026-08-29: both sat ``blocked``, ``attempts=0``, 207 and 186
deferrals, ~10h and 22.7h respectively — and ``alert: (none)`` read clean the
entire time, because the rest of the queue kept launching fine.

An entry with ``attempts == 0`` has never been dispatched — no branch, no
PR, no merge-queue row ever existed for it, and none ever can, because
nothing was ever built. A ``blocked``/``parked`` entry in that state is not
waiting on a gate that MIGHT clear; it is waiting on a gate that structurally
cannot exist. ``coord.drive_queue.detect_unreachable_waits`` is the one
predicate for that shape (also what backs the ``status`` alert line itself,
so this check and that line can never say different things) — this probe
just re-derives it, live, from ``drive_queue`` state every time ``coord
doctor`` runs, independent of whatever the last tick happened to report.

#2978: the predicate also flags an ``attempts > 0`` entry that exhausted its
retry budget without ever getting a #2273 dispatch-layer death past `coord
assign` (same "no branch/PR ever existed" invariant, just reached a
different way) — and excludes an entry blocked solely on an unsatisfiable
``after=`` pre-req, since that one is ``_reconcile_blocked_after``'s to
self-heal (#2362/#2756), not an operator's. See
``coord.drive_queue.detect_unreachable_waits`` for the exact predicate; this
probe does not duplicate it, only renders its result.

This is deliberately independent of #2935 (which fixes the specific sweep
that produced the #2900/#2907 incident) and #2230 (the sweep's origin): both
are about *why* a row gets wedged this way; this check is about the row
being wedged *at all*, whatever sweep — present or future — produced it.

WARN, not CRIT: the fix costs an operator ~10 seconds (``coord drive-queue
remove`` + ``add``) once they know, and this check exists to make sure they
know — it is not itself an outage.

``cost=COST_NETWORK``, not ``COST_CHEAP``: ``coord.state.list_drive_queue``
routes to the daemon over HTTP whenever ``board_service`` is set, which is
the common case on a thin-client fleet machine (same situation
``release_cordon.py`` documents for its own board-state read) — so this is a
real network call on most of the fleet, not a local DB read, and must be
skippable by ``--no-network``/timer runs and the automatic per-agent health
poll exactly like the other network-costed checks.
"""

from __future__ import annotations

from coord.health.models import CheckResult, HealthContext, Severity
from coord.health.registry import COST_NETWORK, check


@check(
    id="wedged_drive_queue",
    scope="machine",
    title="wedged drive-queue entries",
    order=23,
    cost=COST_NETWORK,
    description=(
        "Drive-queue rows in `coord.drive_queue`'s #2944 guaranteed-false "
        "wait: `blocked`/`parked` with no branch/PR/merge-queue row any "
        "sweep could ever act on — either `attempts == 0` (never dispatched) "
        "past a few ticks of grace, or `attempts > 0` with an exhausted "
        "#2273 dispatch-layer death (#2978). Excludes an entry blocked "
        "solely on an unsatisfiable `after=` pre-req — that one is "
        "`_reconcile_blocked_after`'s to self-heal (#2756), not an "
        "operator's. Reads via `coord.state.list_drive_queue`, which — per "
        "its own docstring — routes to the daemon over HTTP when "
        "`board_service` is set (the common case on a thin-client fleet "
        "machine, see `release_cordon.py`) and only reads the local DB "
        "directly otherwise; `cost=network` so `--no-network`/timer runs "
        "and the automatic per-agent health poll skip it exactly like the "
        "other network-costed checks, same precedent as `release_cordon`."
    ),
)
def probe_wedged_drive_queue(ctx: HealthContext) -> CheckResult:
    del ctx  # unused: this check reads local queue state directly, not config
    from coord.drive_queue import (  # noqa: PLC0415
        detect_unreachable_waits,
        entries_from_rows,
    )
    from coord.state import list_drive_queue  # noqa: PLC0415

    try:
        rows = list_drive_queue()
        waits = detect_unreachable_waits(entries_from_rows(rows))
    except Exception as exc:  # noqa: BLE001 — a probe must never raise
        return CheckResult(
            check_id="wedged_drive_queue",
            scope="machine",
            severity=Severity.UNKNOWN,
            headroom=f"could not read the drive queue: {exc}",
            error=str(exc),
        )

    if not waits:
        return CheckResult(
            check_id="wedged_drive_queue",
            scope="machine",
            severity=Severity.OK,
            headroom="0 wedged drive-queue entries",
        )

    sample = ", ".join(
        f"{w.key} ({w.state}, {w.deferrals} deferrals"
        + (f", {w.dependents} dependents self-heal (#2756)" if w.dependents else "")
        + ")"
        for w in waits[:5]
    )
    return CheckResult(
        check_id="wedged_drive_queue",
        scope="machine",
        severity=Severity.WARN,
        headroom=(
            f"{len(waits)} wedged drive-queue entr"
            f"{'y' if len(waits) == 1 else 'ies'}"
        ),
        detail=(
            f"e.g. {sample}" + (", ..." if len(waits) > 5 else "")
            + " — no branch/PR/merge-queue row any sweep could ever act on "
            "(never dispatched, or every dispatch attempt died before "
            "creating an assignment); cannot clear on its own. fix (root "
            "only — any dependents self-heal, #2756): coord drive-queue "
            "remove <repo> <issue> && coord drive-queue add <repo> <issue>"
        ),
        threshold=(
            "warn when any entry is blocked/parked with no branch/PR a sweep "
            "could ever act on: attempts=0 past a few ticks of grace, or "
            "attempts>0 with an exhausted #2273 dispatch-layer death"
        ),
        values={
            "count": len(waits),
            "entries": [
                {
                    "key": w.key,
                    "state": w.state,
                    "deferrals": w.deferrals,
                    "dependents": w.dependents,
                    "last_reason": w.last_reason,
                }
                for w in waits
            ],
        },
    )
