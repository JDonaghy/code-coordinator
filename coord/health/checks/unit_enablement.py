"""Systemd unit *enablement* vs. the per-role manifest (#2098).

`unit_drift` (:mod:`coord.health.checks.unit_drift`) answers "does the
installed unit's *content* match the release" and deliberately treats
absence as OK — most hosts don't run every deploy-lane unit, and which
units a host runs is a topology decision neither that check nor
`coord.deploy_units` will infer (#1831). Neither of those probes answers
the question that actually cost a day: **an installed unit whose content
is byte-perfect can still be sitting there disabled.**

`coord-release-propagate.timer` was `cp`'d onto dellserver correctly and
then never `systemctl --user enable --now`'d, because the runbook that
would have said so never mentioned it (fixed in
`docs/AGENT_OPERATIONS.md`, but the doc fix alone leaves the *next* missed
enable step just as invisible). A disabled timer and a deferring timer
both produce zero log lines — the fleet ran 11 releases behind for a day
with every readout looking normal, because nothing distinguished "not
enabled" from "enabled and just hasn't fired yet".

`coord.deploy_manifest` is this check's reference: a role -> unit-name
table that used to exist only as prose (or, before that, only as
`~/.config/systemd/user/timers.target.wants/` symlinks on dellserver, which
is machine state that dies with the machine). This probe reads
:func:`~coord.deploy_manifest.all_manifest_units` but only asks about units
this host has *already chosen to install* — same "don't guess topology"
boundary as `unit_drift`: an uninstalled manifest unit is not a fault,
because most hosts are workers and are not supposed to run the daemon
lanes. An *installed* one that the manifest expects and that
`systemctl --user is-enabled` reports as anything other than enabled is
exactly the state that hid the propagate timer.

#3128 — a host that never installed the unit at all
------------------------------------------------------
The boundary above has a blind spot: it can only judge units a host has
already chosen to install, so a daemon host that never ran the *install*
step for `coord-backup.timer`/`coord-dr-verify.timer` — #2098's own failure
shape, one layer up — was invisible to every check, `coord doctor` included.
`coord.deploy_manifest.resolve_role` closes it: when a host has *declared*
its role (`~/.coord/role` or `COORD_ROLE`, resolved exactly once, there —
this probe never reads either source itself), a manifest unit that role
requires and that isn't installed is now a `WARN` naming the unit and the
fix, mirroring `unit_drift`'s fix-line convention. A host that has not
declared a role stays exactly as silent as before: `resolve_role` defaults
to `ROLE_WORKER` with `declared=False`, and this probe only applies the
"must be installed" requirement when `declared` is true — an inferred
default role must never manufacture a new fault that didn't exist
yesterday.
"""

from __future__ import annotations

import subprocess

from coord.deploy_manifest import (
    ROLE_UNITS,
    ROLE_WORKER,
    all_manifest_units,
    resolve_role,
    units_for_role,
)
from coord.health.checks.unit_drift import (
    _KNOWN_PLACEHOLDER_VALUES,
    _PLACEHOLDER_RE,
    _is_templated,
    resolve_reference,
    resolve_systemd_user_dir,
)
from coord.health.models import CheckResult, HealthContext, Severity
from coord.health.registry import check

# `systemctl --user is-enabled` states that mean "this will run". `static`
# is deliberately excluded: every manifest unit ships an `[Install]`
# section (it is either a timer or a persistent service), so an installed
# manifest unit reporting `static` has lost that section relative to
# `deploy/` — a real fault, not a unit that can't be enabled by design.
#
# `alias` means systemctl resolved the queried name to a *different* unit
# via an `Alias=` in that other unit's `[Install]` section and reports the
# alias target's own state — i.e. "enabled, under another name" — not a
# distinct half-enabled state. None of the units this check reads out of
# `deploy/` declare an `Alias=`, so this should not currently be reachable
# in practice; it is kept in the accepted set (rather than omitted or
# treated as WARN) because, per `systemd.unit`(5), it never means "this
# will NOT run" — the one thing this check exists to catch — the opposite
# would risk a false WARN on a legitimately-running unit.
#
# systemctl/`coord --version` are both fast, but a wedged one must not eat
# the ~2s registry budget for the whole tick (mirrors
# `coord.health.checks.spawned_coord`'s `_SYSTEMCTL_TIMEOUT`).
_SYSTEMCTL_TIMEOUT = 5.0

_ENABLED_STATES = {"enabled", "enabled-runtime", "alias"}


def _missing_templated_unit_remedy(
    deploy_text: str, deploy_path, installed_path, name: str
) -> str:
    """The fix line for a required-but-never-installed *templated* manifest
    unit — today, only `coord-agent.service` (#1928's `<MACHINE_NAME>`/
    `<PORT>`) — as part of #3128.

    A sibling of `unit_drift._templated_remedy`, not a call to it: that
    function's known-placeholder branch ends in `systemctl --user restart
    {service}` (with the `.timer`/`.service` suffix stripped to the bare
    service stem), which is correct for *its* caller — a unit that is
    already installed and enabled, where only the *content* drifted, so a
    restart is all that is needed to pick up the render. Here the unit was
    never installed at all: it is not loaded, let alone enabled, so a
    `restart` is not guaranteed to start it and — even where it does — never
    enables it, leaving the unit running-but-not-enabled and silently
    reproducing the exact "disabled unit produces zero evidence" failure
    shape (#2098) this whole check exists to catch. The command chain here
    ends in `enable --now {name}` instead — targeting *name* in full,
    suffix included, the same reasoning as the non-templated branch below
    (stripping `.timer` would silently enable `coord-backup.service` instead
    of `coord-backup.timer`).

    The whole point of this fix line is that an operator can copy-paste it
    verbatim into a shell, so — unlike the previous shape of this remedy —
    nothing here is appended after a trailing `#` comment: every step that
    must actually run is part of the `&&` chain.
    """
    names = sorted(set(_PLACEHOLDER_RE.findall(deploy_text)))
    if names and all(n in _KNOWN_PLACEHOLDER_VALUES for n in names):
        sed_args = " ".join(f'-e "s/<{n}>/{_KNOWN_PLACEHOLDER_VALUES[n]}/"' for n in names)
        return (
            f"{deploy_path} is a TEMPLATE — do not cp it verbatim (#1928). Render "
            f"it for this host first: sed {sed_args} {deploy_path} > {installed_path} "
            f"&& systemctl --user daemon-reload && systemctl --user enable --now {name}"
        )
    return (
        f"{deploy_path} is a TEMPLATE ({', '.join(names)} placeholder(s)) — copying "
        "it verbatim installs those as literal text and the unit will not start. "
        f"See the install instructions at the top of {deploy_path} (sed substitution "
        f"or install-agent.sh) to render it for this host, then systemctl --user "
        f"daemon-reload && systemctl --user enable --now {name}."
    )


def _missing_unit_remedy(name: str, installed_path, reference) -> str:
    """The fix line for a required-but-never-installed manifest unit
    (#3128), mirroring `unit_drift`'s own remedy convention (source from the
    verified packaged reference, never a checkout nothing confirmed is
    current) rather than inventing a second one.

    `enable --now` always targets *name* in full, suffix included — unlike
    the `restart` target `unit_drift`/`_templated_remedy` strip to the bare
    service stem, `systemctl enable` has no notion of "the service this
    timer fires": stripping `.timer` off here would silently enable
    `coord-backup.service` (systemctl's implicit default suffix) instead of
    `coord-backup.timer`, re-creating the exact half-enabled state (#2098)
    this whole check exists to catch.
    """
    if reference is None:
        return (
            f"no packaged deploy/ reference found on this host — install "
            f"{name} from a coord release's deploy/ directory, then "
            f"systemctl --user daemon-reload && systemctl --user enable --now {name}"
        )
    deploy_path = reference.path / name
    try:
        deploy_text = deploy_path.read_text()
    except OSError as exc:
        return (
            f"{deploy_path} (from {reference.label}) could not be read ({exc}) "
            f"— install {name} manually, then systemctl --user daemon-reload "
            f"&& systemctl --user enable --now {name}"
        )
    # #1928: coord-agent.service is a template (`<MACHINE_NAME>`/`<PORT>`) —
    # a bare `cp` installs the placeholders as literal text and the unit
    # refuses to start. `_missing_templated_unit_remedy` is a sibling of
    # unit_drift's own templated-unit remedy (see its docstring for why it
    # is not a call to that function) — only reachable here for
    # `coord-agent.service`, which has no timer to conflate the `enable
    # --now` target with.
    if _is_templated(deploy_text):
        return _missing_templated_unit_remedy(deploy_text, deploy_path, installed_path, name)
    return (
        f"cp {deploy_path} {installed_path} && systemctl --user daemon-reload "
        f"&& systemctl --user enable --now {name}   # reference: {reference.label}"
    )


def _is_enabled(unit: str, *, runner=None) -> tuple[str | None, str | None]:
    """`(state, error)` from `systemctl --user is-enabled <unit>`.

    systemctl exits non-zero for every state that isn't `enabled` —
    `disabled`, `static`, `masked`, `not-found`... — so returncode is
    never the signal here, only stdout is. Split out with an injectable
    *runner* (mirrors `coord.deploy_units.daemon_reload`) so the probe is
    testable without a real systemd.
    """
    run = runner or subprocess.run
    try:
        proc = run(
            ["systemctl", "--user", "is-enabled", unit],
            capture_output=True,
            text=True,
            timeout=_SYSTEMCTL_TIMEOUT,
        )
    except FileNotFoundError:
        return None, "systemctl not found (no systemd on this host)"
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    state = (proc.stdout or "").strip() or (proc.stderr or "").strip() or "unknown"
    return state, None


@check(
    id="unit_enablement",
    scope="machine",
    title="unit enablement",
    order=48,
    description=(
        "Installed deploy-lane units the per-role manifest "
        "(coord/deploy_manifest.py) says should run are actually "
        "`systemctl --user enable`d, not just present (#2098)."
    ),
)
def probe_unit_enablement(ctx: HealthContext) -> list[CheckResult]:
    installed_dir = resolve_systemd_user_dir(ctx)
    results: list[CheckResult] = []

    # #3128: resolve_role is the ONLY place either role source is read —
    # this probe consumes the RoleDeclaration, it never re-opens
    # `~/.coord/role` or re-reads `COORD_ROLE` itself.
    declaration = resolve_role(ctx.coord_dir)
    if not declaration.valid:
        results.append(
            CheckResult(
                check_id="unit_enablement",
                scope="machine",
                subject="role",
                severity=Severity.WARN,
                headroom=(
                    f"declared role {declaration.raw!r} is not recognized "
                    f"(expected one of {sorted(ROLE_UNITS)}) — falling back "
                    f"to {ROLE_WORKER!r} (#3128)"
                ),
                detail=(
                    f"fix the role declaration (COORD_ROLE env var, or "
                    f"{ctx.coord_dir / 'role'}) to one of {sorted(ROLE_UNITS)}"
                ),
                threshold="warn when a declared role value isn't a known role",
                values={"raw_role": declaration.raw, "source": declaration.source},
            )
        )

    # An inferred/default role (nothing declared) must never manufacture a
    # new fault that didn't exist yesterday — only a role this host actually
    # *declared* (env var or ~/.coord/role, valid or not) turns "manifest
    # unit not installed" from silent into a WARN. See the module docstring.
    required: set[str] = set(units_for_role(declaration.role)) if declaration.declared else set()
    reference = resolve_reference(ctx) if required else None

    for name in all_manifest_units():
        installed_path = installed_dir / name
        if not installed_path.exists():
            if name in required:
                results.append(
                    CheckResult(
                        check_id="unit_enablement",
                        scope="machine",
                        subject=name,
                        severity=Severity.WARN,
                        headroom=(
                            f"required by declared role {declaration.role!r} but "
                            "never installed on this host — an uninstalled unit "
                            "and a working one both produce zero evidence until "
                            "something needed it (#3128)"
                        ),
                        detail=_missing_unit_remedy(name, installed_path, reference),
                        threshold=(
                            f"warn when role {declaration.role!r} requires {name} "
                            "and it is not installed"
                        ),
                        values={
                            "installed_path": str(installed_path),
                            "state": None,
                            "role": declaration.role,
                            "required": True,
                        },
                    )
                )
                continue
            # Not this host's topology. `unit_drift` reports the same
            # absence as OK for the same reason (#1831/#1927) — this probe
            # only judges units a host has already chosen to install, or
            # (#3128) a role it has explicitly declared requires.
            continue

        state, error = _is_enabled(name)
        values: dict = {"installed_path": str(installed_path), "state": state}

        if error:
            results.append(
                CheckResult(
                    check_id="unit_enablement",
                    scope="machine",
                    subject=name,
                    severity=Severity.UNKNOWN,
                    headroom=f"could not check enablement: {error}",
                    error=error,
                    values=values,
                )
            )
            continue

        if state in _ENABLED_STATES:
            results.append(
                CheckResult(
                    check_id="unit_enablement",
                    scope="machine",
                    subject=name,
                    severity=Severity.OK,
                    headroom=state,
                    values=values,
                )
            )
            continue

        results.append(
            CheckResult(
                check_id="unit_enablement",
                scope="machine",
                subject=name,
                severity=Severity.WARN,
                headroom=(
                    f"installed but {state} — a disabled unit and a working "
                    "one produce identical evidence until something needed "
                    "it (#2098)"
                ),
                detail=f"systemctl --user daemon-reload && systemctl --user enable --now {name}",
                threshold="warn when a manifest-listed, installed unit is not enabled",
                values=values,
            )
        )

    if not results:
        return [
            CheckResult(
                check_id="unit_enablement",
                scope="machine",
                severity=Severity.OK,
                headroom="no manifest-listed unit installed on this host",
                values={"installed_dir": str(installed_dir)},
            )
        ]
    return results
