"""Are this host's installed systemd *timers* actually enabled and running?
(#2082)

:mod:`coord.health.checks.unit_drift` (#1831) proves an installed unit's
**content** matches the release. It says nothing about whether systemd was
ever told to run it — and `coord-release-propagate.timer` demonstrated that
gap directly. On 2026-08-10 its content matched `deploy/` byte for byte
(`unit_drift` green, always) while the fleet sat eleven releases behind:

    $ systemctl --user list-unit-files 'coord-release-propagate*'
    coord-release-propagate.timer   disabled   enabled
                                     ^^^^^^^^   ^^^^^^^
                                     STATE      VENDOR PRESET

A `.timer` unit's *file* being current says nothing about whether
``systemctl --user enable --now`` was ever run on it. Writing (or
refreshing) a unit's content — by hand, or via
:func:`coord.deploy_units.install_units` — never implies enabling it;
those are two independent systemd operations, and the second one has no
visible symptom until someone thinks to ask "when did this last fire?".
That is precisely how the timer's off state and its working state produced
identical files on disk and stayed indistinguishable from the outside for a
day.

Deliberately generic over every packaged ``*.timer`` (not hardcoded to
``coord-release-propagate.timer``): every timer this fleet ships
(``coord-drive-queue``, ``coord-notify``, ``coord-web-dist-build``, this
one) declares ``[Install] WantedBy=timers.target`` — see ``deploy/*.timer``
— so "installed but not enabled" is the same defect on any of them, not a
property of this one release's timer.

Reuses :mod:`coord.health.checks.unit_drift`'s reference resolution
(:func:`~coord.health.checks.unit_drift.resolve_reference`,
:func:`~coord.health.checks.unit_drift.resolve_systemd_user_dir`) rather
than a second implementation of "which ``deploy/`` is authoritative" —
two answers to that question is exactly the class of defect #2096 names:
two surfaces that happen to agree today and can silently drift apart.

Judgement (:func:`grade_timer_state`) is pure and takes plain
``systemctl --user show`` fields, so it is unit-testable against a
hand-built dict with no systemd, no fleet, and no root — same split
``spawned_coord.py`` documents for "measure locally, judge centrally".

#3219 — a masked timer is not a neglected one
----------------------------------------------
A timer `systemctl --user mask`ed on purpose (the same deliberate-manual-
release-rolls case :mod:`coord.health.checks.unit_drift` documents for
#3049) reports `UnitFileState=masked`, which lands in `_INACTIVE_STATES`
below and grades CRIT — indistinguishable, to this probe, from a timer
nobody ever enabled. This probe still reports the honest severity: masking
a timer that is supposed to fire on its own schedule is a real fault from
*this* probe's narrow view, and deciding whether that's wanted is policy
this probe has no context for. But — mirroring `unit_drift` exactly — it
also checks the SAME sentinel the watchdog already honours
(`~/.coord/watchdog-suppress.json`, #2580) and publishes the verdict in
`values["suppressed"]` (plus `"suppress_reason"`/`"suppress_set"`), so a
policy-aware consumer (`coord release verify`, #3049) can render "masked by
policy" instead of surfacing an unclearable CRIT.
"""

from __future__ import annotations

import subprocess

from coord.health.checks.unit_drift import (
    _unit_files,
    resolve_reference,
    resolve_systemd_user_dir,
)
from coord.health.models import CheckResult, HealthContext, Severity
from coord.health.registry import check, is_suppressed, load_suppressions

# systemctl is fast, but a wedged call must not eat the whole health-tick
# budget — same rationale/value as spawned_coord._SYSTEMCTL_TIMEOUT.
_SYSTEMCTL_TIMEOUT = 5.0

#: ``UnitFileState`` values that mean "systemd will not fire this unit on
#: its own schedule", whatever content is on disk.
_INACTIVE_STATES = frozenset(
    {"disabled", "linked", "linked-runtime", "masked", "masked-runtime"}
)


def _timer_states(
    units: tuple[str, ...], *, runner=None, timeout: float = _SYSTEMCTL_TIMEOUT,
) -> dict[str, dict[str, str]]:
    """One ``systemctl --user show`` call for every timer in *units*.

    Batched into a single subprocess the same way
    ``spawned_coord.running_unit_pids`` batches its own systemctl call: this
    runs on every health tick on every machine, and N timers must cost one
    subprocess, not N. Returns ``{}`` when there is no systemd on this host
    (macOS, a container, a thin client) — that is "no data", handled by the
    caller, not a crash.

    *runner*/*timeout* are injectable (default: ``subprocess.run`` and this
    module's own budget) so :mod:`coord.deploy_units` can reuse this exact
    query — rather than re-implementing "is this timer already enabled" a
    second time — for its own ``enable --now`` vs. leave-it-alone decision
    (#2124). Two independent readings of the same systemd state is how a
    detector and its fixer end up disagreeing about what "enabled" means.
    """
    if not units:
        return {}
    argv = [
        "systemctl",
        "--user",
        "show",
        "--property=Id",
        "--property=UnitFileState",
        "--property=UnitFilePreset",
        "--property=ActiveState",
        "--property=SubState",
        *units,
    ]
    run = runner or subprocess.run
    try:
        proc = run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}

    out: dict[str, dict[str, str]] = {}
    # `systemctl show` emits one KEY=VALUE block per unit, blank-line
    # separated, same shape spawned_coord already parses defensively.
    for block in proc.stdout.split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            key, sep, value = line.partition("=")
            if sep:
                fields[key.strip()] = value.strip()
        unit_id = fields.get("Id", "")
        if unit_id:
            out[unit_id] = fields
    return out


def grade_timer_state(fields: dict[str, str]) -> tuple[Severity, str]:
    """Pure judgement over one timer's ``systemctl --user show`` fields.

    The rule is deliberately simple and does not consult
    ``UnitFilePreset``: a ``.timer`` unit copied into
    ``~/.config/systemd/user/`` is, by construction, meant to run — nobody
    installs a schedule to leave it off, and every packaged timer in this
    fleet declares ``WantedBy=timers.target``. So *installed* implies
    *should be enabled and active*, full stop; the moment that stops being
    true the fleet has exactly the invisible-off-switch problem #2082
    describes. (Contrast ``coord.deploy_units.enable_timers``, the write
    side, which applies the identical rule when it (re)installs a timer's
    content — one invariant, asserted by both the detector and the fixer.)
    """
    state = fields.get("UnitFileState", "")
    active = fields.get("ActiveState", "")
    sub = fields.get("SubState", "")

    if not state and not active:
        return Severity.UNKNOWN, "systemd reported no state for this unit"

    if state in _INACTIVE_STATES:
        return (
            Severity.CRIT,
            f"installed but {state.upper()} — a copied .timer unit that is "
            "never enabled will not fire on its own; run 'systemctl --user "
            f"enable --now <unit>' (ActiveState={active or '?'})",
        )

    if active != "active":
        return (
            Severity.CRIT,
            f"enabled but not active (ActiveState={active or '?'}, "
            f"SubState={sub or '?'}) — check 'systemctl --user status <unit>'",
        )

    return Severity.OK, f"enabled and active (state={state or '?'})"


@check(
    id="timer_active",
    scope="machine",
    title="timer active",
    order=45,
    description=(
        "Installed systemd *user* timers (deploy/*.timer) are enabled and "
        "active, not merely present with matching content (#2082) — a "
        "disabled timer's file is indistinguishable from an active one's "
        "until something asks systemd whether it actually runs."
    ),
)
def probe_timer_active(ctx: HealthContext) -> list[CheckResult]:
    reference = resolve_reference(ctx)
    if reference is None:
        return [
            CheckResult(
                check_id="timer_active",
                scope="machine",
                severity=Severity.OK,
                headroom="no deploy/ checkout found on this machine",
                values={"reference_source": None},
            )
        ]

    timers = [p.name for p in _unit_files(reference.path) if p.name.endswith(".timer")]
    if not timers:
        return [
            CheckResult(
                check_id="timer_active",
                scope="machine",
                severity=Severity.OK,
                headroom="no timer units in the packaged reference",
                values={"reference_source": reference.source},
            )
        ]

    installed_dir = resolve_systemd_user_dir(ctx)
    present = [name for name in timers if (installed_dir / name).exists()]
    if not present:
        # The common case — most machines run none of the fleet's timers.
        # Same "absence is OK, not a fault" convention as spawned_coord /
        # cli_venv / tui_binary.
        return [
            CheckResult(
                check_id="timer_active",
                scope="machine",
                severity=Severity.OK,
                headroom="no packaged timer is installed on this machine",
                values={"reference_source": reference.source},
            )
        ]

    states = _timer_states(tuple(present))
    # #3219: same sentinel unit_drift already reads (#3049,
    # ~/.coord/watchdog-suppress.json) — a timer masked on purpose reads
    # identically to a neglected one to systemctl, so `values["suppressed"]`
    # is published for every result here regardless of severity, mirroring
    # unit_drift's convention, so a policy-aware consumer can tell the two
    # apart without this probe guessing at policy itself.
    suppressions = load_suppressions(ctx.coord_dir)
    results: list[CheckResult] = []
    for name in present:
        fields = states.get(name)
        values = {"unit": name, "reference_source": reference.source}
        # Bare unit name first, matching the key fleet_watchdog's own
        # timer-disabled check already suppresses under
        # (`suppress_keys=(unit,)`) — an operator who suppressed this unit
        # for the watchdog does not maintain a second key for this probe.
        # `timer_active:<name>` is also accepted for symmetry with
        # unit_drift's `unit_drift:<name>`.
        suppressed, entry = is_suppressed(
            suppressions, (name, f"timer_active:{name}"), now=ctx.now
        )
        values["suppressed"] = suppressed
        values["suppress_reason"] = (entry or {}).get("reason") if suppressed else None
        values["suppress_set"] = (entry or {}).get("set") if suppressed else None
        if fields is None:
            results.append(
                CheckResult(
                    check_id="timer_active",
                    scope="machine",
                    subject=name,
                    severity=Severity.UNKNOWN,
                    headroom="could not read systemd state for this unit",
                    detail=(
                        "no systemd on this host, or 'systemctl --user show' "
                        "failed — this lane is unverified, not confirmed active"
                    ),
                    error="systemctl unavailable",
                    values=values,
                )
            )
            continue

        severity, headroom = grade_timer_state(fields)
        values.update(
            {
                "unit_file_state": fields.get("UnitFileState"),
                "unit_file_preset": fields.get("UnitFilePreset"),
                "active_state": fields.get("ActiveState"),
                "sub_state": fields.get("SubState"),
            }
        )
        results.append(
            CheckResult(
                check_id="timer_active",
                scope="machine",
                subject=name,
                severity=severity,
                headroom=headroom,
                detail=f"systemctl --user enable --now {name}" if severity is Severity.CRIT else "",
                threshold="crit when an installed timer is not enabled and active",
                values=values,
            )
        )

    return results
