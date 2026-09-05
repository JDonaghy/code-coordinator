"""Per-role systemd unit manifest — which units a host runs (#2098).

`deploy/` ships every packaged unit and `coord release verify` /
`deploy_units.install_units` both deliberately decline to guess which of
them a given host should run — *"a release does not decide which services
a host runs"* (`coord/deploy_units.py`'s module docstring). That refusal is
correct, but it left the actual decision nowhere: not in a file, not in
code, only as `~/.config/systemd/user/timers.target.wants/` symlinks on
dellserver. Losing dellserver would have meant rebuilding that list from
memory.

This module is that list, in code instead of folklore. It mirrors
`docs/AGENT_OPERATIONS.md`'s "Daemon-host unit inventory" table — keep both
in sync; `tests/test_deploy_manifest.py` cross-checks the doc table against
:data:`ROLE_UNITS` so the two cannot quietly drift apart the way the
propagate-timer enable step did.

Each name is the unit that should actually be `systemctl --user enable
--now`'d. For a timer-backed lane that is always the `.timer`, never the
oneshot `.service` it fires — enabling the oneshot does nothing; only the
timer's own `[Install]` section wires anything into
`timers.target.wants/`. `coord-agent`, `coord-serve` and `coord-web` have
no timer and are listed as `.service` because they ARE the long-running
unit.

Consumed by :mod:`coord.health.checks.unit_enablement` (#2098), which
reads :func:`all_manifest_units` to decide which *installed* units it is
entitled to expect `enabled`. It does not use this module to guess which
role a given host plays — same "don't infer topology" boundary as
`unit_drift` and `deploy_units`: an installed-but-unlisted unit is not this
module's business, and an uninstalled manifest unit is not a fault (most
hosts are workers, and are not supposed to run the daemon lanes).
"""

from __future__ import annotations

ROLE_WORKER = "worker"
ROLE_DAEMON = "daemon"

#: role -> unit names that role's host should have running, in the order
#: they appear in docs/AGENT_OPERATIONS.md's "Daemon-host unit inventory"
#: table. `coord-agent.service` is listed under both roles because every
#: machine — daemon host included — runs it.
ROLE_UNITS: dict[str, tuple[str, ...]] = {
    ROLE_WORKER: (
        "coord-agent.service",
    ),
    ROLE_DAEMON: (
        "coord-agent.service",
        "coord-serve.service",
        "coord-web.service",
        "coord-web-dist-build.timer",
        "coord-notify.timer",
        "coord-drive-queue.timer",
        "coord-release-propagate.timer",
        "coord-db-backup.timer",
        "coord-backup.timer",
    ),
}


def units_for_role(role: str) -> tuple[str, ...]:
    """The units *role* should run, or `()` for an unknown role."""
    return ROLE_UNITS.get(role, ())


def all_manifest_units() -> tuple[str, ...]:
    """Every unit named by any role, deduped and sorted.

    What :mod:`coord.health.checks.unit_enablement` iterates: it does not
    need to know *which* role this host plays, only whether an installed
    unit is one this manifest ever expects to be enabled somewhere.
    """
    seen: set[str] = set()
    out: list[str] = []
    for units in ROLE_UNITS.values():
        for name in units:
            if name not in seen:
                seen.add(name)
                out.append(name)
    return tuple(sorted(out))
