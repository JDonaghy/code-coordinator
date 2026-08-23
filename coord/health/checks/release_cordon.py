"""Machine-scope: is THIS host cordoned, idle, and past its drain deadline? (#2595)

`coord release propagate` cordons a host to make a rollable window (#2101),
and escalates loudly when a cordon outlives ``--drain-deadline`` (#2136) —
but "loudly" means a stderr line from a ``Type=oneshot`` timer, which goes
to the journal and nowhere an operator routinely looks. #2595's incident:
``precision`` sat cordoned and 22 releases behind, idle and reachable the
whole time, found only because someone happened to read the journal by
hand.

This check answers the sharpest form of that question directly, on the
machine it runs on: is the cordon here genuinely stuck? A cordoned host
with active work is "draining" — normal, temporary, no different from any
other in-progress roll. A cordoned host with **zero** active assignments
has nothing left to drain; if it is still cordoned past the deadline, it is
not in progress, it is a whole machine quietly pulled from the fleet.

Deliberately self-contained (the registry's own acceptance bar: adding a
check should touch exactly one file). It does its own I/O — the cordon
store and the board, both already daemon-aware and fail-soft
(:func:`coord.machine_pause.cordons`, :func:`coord.board_service.read_board`)
— rather than waiting on a fact the daemon's fleet-health tick would have to
be taught to gather first. ``cost="network"`` so ``--no-network``/timer
runs can skip it exactly like the other network-costed checks.

This check runs inside ``coord agent``'s ``/health`` route, i.e. on every
worker machine's own agent process — most of which are thin clients per
``docs/AGENT_OPERATIONS.md``'s ``board_service = "http://<daemon-host>:7435"``
per-machine config. ``coord.state.build_board()`` reconstructs the LOCAL,
non-canonical SQLite board on such a host and only warns (does not raise) on
the thin-client guard, so a genuinely busy thin client could silently read
back ``is_busy=False`` and fire a false CRIT. ``coord.board_service.read_board()``
is the fix: it GETs the daemon's canonical board when ``board_service`` is
configured and only falls back to the local DB otherwise — the same routing
``coord doctor`` uses for this identical idleness question (#2595 review).

The actual "cordoned + idle + overdue" decision is
:func:`coord.release_cordon.idle_overdue_cordons` — the SAME pure function
`coord status` and `coord doctor` call, so the wording and the threshold
can never drift apart between this check and those two surfaces.
"""

from __future__ import annotations

import socket

from coord.health.models import CheckResult, HealthContext, Severity
from coord.health.registry import COST_NETWORK, check


def _this_machine_name(config: object) -> str | None:
    """The ``coordinator.yml`` machine entry for the host this runs on.

    Same hostname-matching rule :func:`coord.health.context.local_checkouts`
    uses: a machine whose ``name`` or ``host`` matches this box's short
    hostname. ``None`` when there is no config, or no match — a
    config-free agent (#1801) or a hostname that doesn't appear in
    ``coordinator.yml`` simply has nothing this check can say.
    """
    if config is None:
        return None
    try:
        local = socket.gethostname().split(".")[0]
    except OSError:  # pragma: no cover — gethostname basically cannot fail
        return None
    for machine in getattr(config, "machines", ()) or ():
        name = getattr(machine, "name", "")
        host = getattr(machine, "host", "")
        if name == local or (host and host.split(".")[0] == local):
            return name
    return None


@check(
    id="release_cordon_idle",
    scope="machine",
    title="cordoned but idle",
    cost=COST_NETWORK,
    order=30,
    description=(
        "A release cordon that has outlived its drain deadline on a host "
        "with zero active assignments — nothing left to drain, taking no "
        "new work, and invisible on every surface that only shows plain "
        "`online • idle` (#2595)."
    ),
)
def probe_release_cordon_idle(ctx: HealthContext) -> CheckResult | None:
    machine_name = _this_machine_name(ctx.config)
    if machine_name is None:
        return None

    from coord.machine_pause import cordons as fetch_cordons  # noqa: PLC0415
    from coord.release_cordon import (  # noqa: PLC0415
        DEFAULT_DRAIN_DEADLINE_SECONDS,
        idle_overdue_cordons,
    )

    deadline = (
        getattr(ctx.thresholds, "release_cordon_drain_deadline_secs", None)
        or DEFAULT_DRAIN_DEADLINE_SECONDS
    )

    try:
        live = fetch_cordons(now=ctx.now)
    except Exception as exc:  # noqa: BLE001 — a probe must never raise
        return CheckResult(
            check_id="release_cordon_idle",
            scope="machine",
            severity=Severity.UNKNOWN,
            headroom=f"could not read the cordon store: {exc}",
        )

    cordon = live.get(machine_name)
    if cordon is None:
        return CheckResult(
            check_id="release_cordon_idle",
            scope="machine",
            severity=Severity.OK,
            headroom="not cordoned",
        )

    # #2595 review: NOT `Board.idle_machines()` — that filters
    # `Board.machines`, which reflects machine registration/dispatch
    # history rather than `coordinator.yml`, so a host `read_board()` has
    # no row for would read as neither idle nor busy instead of idle. The
    # only fact that actually matters here is "does THIS host have a
    # `running` board row right now" — `Board.active` is authoritative for
    # that regardless of what `Board.machines` contains.
    #
    # NOT `coord.state.build_board()` — this check runs inside `coord
    # agent`'s /health route, i.e. on every worker machine's own process,
    # most of which are thin clients (see module docstring). A direct local
    # read would reconstruct the non-canonical local SQLite board and could
    # read a genuinely busy thin client as idle, firing a false CRIT.
    # `board_service.read_board()` GETs the daemon's canonical board when
    # `board_service` is configured and only falls back to the local DB
    # otherwise — the same routing `coord doctor` uses for this identical
    # question.
    try:
        from coord.board_service import read_board  # noqa: PLC0415

        is_busy = any(
            a.machine_name == machine_name and a.status == "running"
            for a in read_board().active
        )
    except Exception as exc:  # noqa: BLE001 — a probe must never raise
        return CheckResult(
            check_id="release_cordon_idle",
            scope="machine",
            severity=Severity.UNKNOWN,
            headroom=f"cordoned ({cordon.describe()}) — could not read the "
            f"board to tell whether it is idle: {exc}",
        )
    idle_names = set() if is_busy else {machine_name}

    found = idle_overdue_cordons(
        {machine_name: cordon}, now=ctx.now, idle_hosts=idle_names, deadline=deadline,
    )
    if found:
        overdue = found[0]
        return CheckResult(
            check_id="release_cordon_idle",
            scope="machine",
            severity=Severity.CRIT,
            headroom=f"cordoned and IDLE for {overdue.waited_seconds / 60.0:.0f}m "
            f"(deadline {deadline / 60.0:.0f}m) — taking no work",
            detail=overdue.message,
            threshold=f"crit when idle and cordoned past {deadline / 60.0:.0f}m",
            values=overdue.to_dict(),
        )

    if machine_name not in idle_names:
        return CheckResult(
            check_id="release_cordon_idle",
            scope="machine",
            severity=Severity.OK,
            headroom=f"cordoned, draining ({cordon.age(ctx.now) / 60.0:.0f}m) — has "
            "active work",
        )

    return CheckResult(
        check_id="release_cordon_idle",
        scope="machine",
        severity=Severity.OK,
        headroom=f"cordoned, idle, within deadline "
        f"({cordon.age(ctx.now) / 60.0:.0f}m/{deadline / 60.0:.0f}m)",
    )
