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

#3128 — a host cannot tell you a unit was never installed
-----------------------------------------------------------
The boundary above is deliberate but it has a blind spot: an uninstalled
manifest unit is *usually* fine (a worker box) but occasionally is exactly
the fault (the daemon host, missing `coord-backup.timer` because nobody
ran the install step — #2098's own failure shape, one layer up). Neither
this module nor `unit_enablement` can tell those two cases apart without
being told which one applies, and `coordinator.yml` is the wrong place to
ask: the daemon host must be able to answer "what am I?" even with the
board down, which is precisely the DR case #3117 exists for.

:func:`resolve_role` is that answer, host-local and independent of the
board: `COORD_ROLE` (settable in a systemd unit's `Environment=`) if set,
else `<coord_dir>/role` (`~/.coord/role`), else `ROLE_WORKER` — the safe
majority, and it reproduces today's behaviour byte-for-byte for every host
that never opts in. It is the *only* function that reads either source —
`unit_enablement` consumes the :class:`RoleDeclaration` it returns rather
than re-reading `~/.coord/role` itself, so "what role is this host" has
exactly one implementation.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

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
        "coord-dr-verify.timer",
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


#: Env var a systemd unit's `Environment=` can set to declare this host's
#: role — checked before `<coord_dir>/role` so a unit-scoped override never
#: needs a file write. Named like every other host-local override in this
#: codebase (`COORD_CONFIG`, `COORD_DIR`, ...).
ROLE_ENV_VAR = "COORD_ROLE"

#: `<coord_dir>/<_ROLE_FILENAME>` — `~/.coord/role` by default. A plain file
#: rather than a `coordinator.yml` key on purpose: this must resolve with the
#: board down (#3117's DR case), and `coordinator.yml` is board-adjacent
#: fleet config, not a fact one host can assert about only itself.
_ROLE_FILENAME = "role"


@dataclass(frozen=True)
class RoleDeclaration:
    """What :func:`resolve_role` found, and whether it was usable.

    `role` is always a valid key into :data:`ROLE_UNITS` — callers never
    need to re-validate it, they can go straight to `units_for_role(role)`.
    `declared` distinguishes "nothing was set" (today's behaviour, and it
    must stay silent — #3128's first acceptance criterion) from "something
    was set", which the other two fields describe:

    * `valid=True`  — the declared value is a real role name, or nothing was
      declared at all (the safe default is not itself a fault).
    * `valid=False` — something *was* declared (env var or file, non-blank)
      but it isn't a role `ROLE_UNITS` knows, e.g. a typo. `role` still
      falls back to `ROLE_WORKER` rather than raising or silently reading as
      `ROLE_DAEMON`, but a consumer that wants to flag the bad input can do
      so via `raw` without re-reading the source itself.
    """

    role: str
    raw: str | None
    source: str  # "env" | "file" | "default"
    valid: bool

    @property
    def declared(self) -> bool:
        return self.source != "default"


def resolve_role(coord_dir: Path, *, env: Mapping[str, str] | None = None) -> RoleDeclaration:
    """What role this host plays — the single source of truth (#3128).

    Order: :data:`ROLE_ENV_VAR` (a systemd unit's own `Environment=` can set
    it without a file write) first, then `<coord_dir>/role`, then the
    `ROLE_WORKER` default. Never raises: a missing/unreadable file, an unset
    env var, or a blank value in either all read the same as "nothing
    declared" (`source="default"`). A non-blank value that isn't a known
    role name still resolves `role` to `ROLE_WORKER` (fail safe, never fail
    open into `ROLE_DAEMON`) but comes back with `valid=False` so the caller
    can raise it as a fault instead of it being silently swallowed.

    This is the *only* place either source is read — see the module
    docstring's #3128 section for why that matters.
    """
    active_env = os.environ if env is None else env
    raw = active_env.get(ROLE_ENV_VAR)
    source = "env"
    if not raw:
        source = "file"
        try:
            raw = Path(coord_dir).joinpath(_ROLE_FILENAME).read_text(encoding="utf-8")
        except OSError:
            raw = None

    if raw is None or not raw.strip():
        return RoleDeclaration(role=ROLE_WORKER, raw=None, source="default", valid=True)

    stripped = raw.strip()
    normalized = stripped.lower()
    if normalized in ROLE_UNITS:
        return RoleDeclaration(role=normalized, raw=stripped, source=source, valid=True)
    return RoleDeclaration(role=ROLE_WORKER, raw=stripped, source=source, valid=False)
