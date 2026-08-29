"""Fleet-scope: are the four deploy lanes running the same release? (#1630)

The four lanes named in the issue: each agent's ``~/.coord-venv`` (N of
them), the ``coord-serve`` process on the daemon host, the operator's
``~/.coord-cli-venv``, and the locally-built ``tui/`` binary.  The CLI venv
lane exists *specifically* because it was found three releases stale on
2026-07-29 — silently driving `coord` commands without fixes everyone
believed were live.

This module never probes the filesystem itself.  Two different sources feed
it, and #1806 is precisely about not conflating them:

* ``coord-serve``'s own version is a genuinely daemon-host-local fact (it can
  only be introspected from the process actually running it) — the daemon's
  health-poll tick gathers that into ``HealthContext.fleet.daemon_host``
  (``coord.serve_app``'s ``FleetHealthRefresher``).
* The CLI venv's version is *not* a daemon-host fact — it's whichever
  machine's filesystem the operator actually put ``~/.coord-cli-venv`` on,
  which is very often a different box from the one running ``coord-serve``.
  That fact rides the same transport every agent's ``~/.coord-venv`` already
  does: each machine's own ``/health`` poll, via the ``cli_venv`` machine-
  scope check in :mod:`coord.health.checks.deploy_lane_facts`.  See #1806.

#1834 added a fifth kind of lane, and it is the one that makes this check
worth running: ``<unit> spawns (<machine>)`` — the version each *running*
coord service would actually hand its subprocesses, resolved from the live
process's own PATH. Every lane above measures an install; on 2026-08-04 all
of them agreed on 0.4.105 while the daemon spawned 0.4.103, because the
defect was not in any install but in what ``shutil.which("coord")`` found
first. Skew is only ever visible as a *relationship between* lanes, which is
why it is checked here and not by any amount of per-lane staleness logic.

Both checks below are fail-soft toward UNKNOWN, never toward OK: a lane no
machine has data for (an agent that never reported, a CLI venv that was
never configured anywhere) must not read as "in sync" just because there was
nothing to disagree with.

A third fleet check, ``fleet_webapp_bundle``, aggregates ``webapp_bundle``
(#1834 lane 5, the phone-webapp bundle `coord web --dist` serves) the same
way ``fleet_tui_binary`` aggregates ``tui_binary`` — same source
(:mod:`coord.health.checks.deploy_lane_facts`), same shape, same fail-soft
convention. It is deliberately **not** folded into the version-skew map
below: ``deploy/coord-web-dist-build.timer`` publishes continuously off
``origin/main``'s SHA, decoupled on purpose from the pip release cadence
every other lane in this module compares against, so a bundle's "version" is
never comparable to ``coord-serve``'s or an agent venv's. What it CAN
report — staleness against its own source tree, and disagreement between
machines about which bundle is live — it does, on its own terms, exactly
like ``tui_binary`` already does for the same reason.
"""

from __future__ import annotations

from coord.health.models import CheckResult, HealthContext, Severity
from coord.health.registry import check


def _machine_check_values(ctx: HealthContext, check_id: str) -> dict[str, dict]:
    """machine_name -> that machine's reported ``values`` for *check_id*.

    Only includes machines whose result for this check exists and didn't
    error — a machine that never runs/reports this check is simply absent
    from the returned dict (the right shape for "optional lane" checks like
    ``cli_venv``/``tui_binary``, where most machines legitimately have
    nothing to say). ``_agent_lane_versions`` below wants the opposite shape
    — every machine present, ``None`` standing in for "no data" — because
    ``agent_venv`` is mandatory on every machine, so it doesn't use this
    helper.
    """
    out: dict[str, dict] = {}
    if ctx.fleet is None:
        return out
    for name, entry in ctx.fleet.machines.items():
        checks = (entry or {}).get("checks") or {}
        for r in checks.get("results", []) or []:
            if r.get("check_id") == check_id and not r.get("error"):
                out[name] = r.get("values") or {}
                break
    return out


def _agent_lane_versions(ctx: HealthContext) -> dict[str, str | None]:
    """machine_name -> its reported ``agent_venv`` version, or ``None``.

    ``None`` covers both "machine offline" and "machine online but its
    ``agent_venv`` check didn't run/errored" — both are "no data", not
    "matches everyone else". Unlike ``_machine_check_values``, every machine
    in ``ctx.fleet.machines`` gets an entry — ``agent_venv`` is mandatory,
    so a machine with nothing to say about it is a missing lane, not an
    inapplicable one.
    """
    out: dict[str, str | None] = {}
    if ctx.fleet is None:
        return out
    for name, entry in ctx.fleet.machines.items():
        checks = (entry or {}).get("checks") or {}
        version = None
        for r in checks.get("results", []) or []:
            if r.get("check_id") == "agent_venv" and not r.get("error"):
                version = (r.get("values") or {}).get("version") or None
                break
        out[name] = version
    return out


def _machine_check_rows(ctx: HealthContext, check_id: str) -> dict[str, list[dict]]:
    """machine_name -> every non-errored result row this machine reported for
    *check_id*.

    :func:`_machine_check_values` stops at the first match, which is right for
    the singleton checks it was written for. ``spawned_coord`` (#1834) reports
    one row **per running unit**, so a first-match read would see only
    ``coord-agent`` and structurally miss ``coord-serve`` — the one unit whose
    spawned version was the whole 2026-08-04 incident.
    """
    out: dict[str, list[dict]] = {}
    if ctx.fleet is None:
        return out
    for name, entry in ctx.fleet.machines.items():
        checks = (entry or {}).get("checks") or {}
        rows = [
            r
            for r in (checks.get("results") or [])
            if r.get("check_id") == check_id and not r.get("error")
        ]
        if rows:
            out[name] = rows
    return out


def _spawned_lanes(ctx: HealthContext) -> dict[str, str]:
    """``<unit> spawns (<machine>)`` -> the version that unit would actually
    spawn, for every running coord service across the fleet (#1834).

    This is the lane that did not exist on 2026-08-04, and its absence is why
    every other lane in this check read green while the fleet was running two
    versions: each of them measures an *install*, and the defect lived in what
    ``shutil.which("coord")`` resolved to inside a live service's PATH.
    See :mod:`coord.health.checks.spawned_coord`.

    Only units with a resolvable spawned version become lanes. A unit whose
    PATH has no ``coord`` on it at all is deliberately **not** a lane: its
    subprocesses fall back to ``python -m coord.cli`` on the parent's own
    interpreter, which cannot disagree with the parent, so admitting it as a
    null lane would manufacture a permanent "missing lane" UNKNOWN on every
    correctly-deployed fleet.
    """
    out: dict[str, str] = {}
    for machine, rows in _machine_check_rows(ctx, "spawned_coord").items():
        for row in rows:
            values = row.get("values") or {}
            version = values.get("version")
            unit = row.get("subject") or values.get("unit")
            if version and unit:
                out[f"{unit} spawns ({machine})"] = version
    return out


def _cli_venv_lanes(ctx: HealthContext) -> dict[str, str | None]:
    """``~/.coord-cli-venv (<machine>)`` -> version, for every machine whose
    own ``cli_venv`` check (#1806) reports one present.

    Named per-machine, unlike the daemon-host fact it replaces, because more
    than one machine can plausibly have a CLI venv (more than one operator).
    When *no* machine reports one present, a single ``"~/.coord-cli-venv":
    None`` entry stands in — "no data anywhere", not "matches everyone else"
    just because nothing disagreed.
    """
    out: dict[str, str | None] = {}
    for name, values in _machine_check_values(ctx, "cli_venv").items():
        if values.get("present"):
            out[f"~/.coord-cli-venv ({name})"] = values.get("version") or None
    if not out:
        out["~/.coord-cli-venv"] = None
    return out


@check(
    id="fleet_deploy_lanes",
    scope="fleet",
    title="deploy lanes",
    order=10,
    description=(
        "Every ~/.coord-venv (per agent), the daemon's own coord-serve "
        "install, ~/.coord-cli-venv, and the coord each running service "
        "would actually spawn all report the same coordinator version."
    ),
)
def probe_deploy_lanes(ctx: HealthContext) -> CheckResult:
    if ctx.fleet is None:
        return CheckResult(
            check_id="fleet_deploy_lanes",
            scope="fleet",
            severity=Severity.UNKNOWN,
            headroom="no fleet snapshot (fleet checks only run on the daemon)",
        )

    lanes: dict[str, str | None] = dict(_agent_lane_versions(ctx))
    dh = ctx.fleet.daemon_host or {}
    lanes["coord-serve (daemon host)"] = dh.get("coord_serve_version")
    lanes.update(_cli_venv_lanes(ctx))
    # #1834: what each running service would actually SPAWN. Added last and
    # only where known, so it can introduce skew but never a missing lane.
    lanes.update(_spawned_lanes(ctx))

    known = {v for v in lanes.values() if v}
    missing = sorted(name for name, v in lanes.items() if not v)

    if not known:
        return CheckResult(
            check_id="fleet_deploy_lanes",
            scope="fleet",
            severity=Severity.UNKNOWN,
            headroom="no lane has a resolvable version yet",
            detail=f"no data for: {', '.join(missing)}" if missing else "",
            values={"lanes": lanes},
        )

    if len(known) > 1:
        by_version: dict[str, list[str]] = {}
        for name, v in lanes.items():
            if v:
                by_version.setdefault(v, []).append(name)
        skew_desc = "; ".join(
            f"{v}: {', '.join(sorted(names))}" for v, names in sorted(by_version.items())
        )
        return CheckResult(
            check_id="fleet_deploy_lanes",
            scope="fleet",
            severity=Severity.CRIT,
            headroom=f"{len(known)} versions live across the fleet",
            detail=skew_desc,
            threshold="crit when any lane disagrees",
            values={"lanes": lanes},
        )

    (version,) = known
    headroom = f"all lanes on {version}"
    if missing:
        headroom += f" ({len(missing)} lane(s) with no data)"
    return CheckResult(
        check_id="fleet_deploy_lanes",
        scope="fleet",
        severity=Severity.OK if not missing else Severity.UNKNOWN,
        headroom=headroom,
        detail=f"no data for: {', '.join(missing)}" if missing else "",
        values={"lanes": lanes},
    )


@check(
    id="fleet_tui_binary",
    scope="fleet",
    title="tui binary",
    order=11,
    description=(
        "The locally-built coord-tui binary is not older than the coord-tui source "
        "tree it was supposedly built from, on every machine that has one."
    ),
)
def probe_tui_binary(ctx: HealthContext) -> CheckResult:
    """Aggregates every machine's own ``tui_binary`` machine-scope check
    (:mod:`coord.health.checks.deploy_lane_facts`, #1806) instead of
    ``os.stat``-ing a single path on the daemon host — the daemon host is
    frequently not the operator's machine, so a single-path check there was
    structurally blind to the binary that actually matters.
    """
    if ctx.fleet is None:
        return CheckResult(
            check_id="fleet_tui_binary",
            scope="fleet",
            severity=Severity.UNKNOWN,
            headroom="no fleet snapshot (fleet checks only run on the daemon)",
        )

    facts = _machine_check_values(ctx, "tui_binary")
    present = {name: v for name, v in facts.items() if v.get("present")}

    if not present:
        return CheckResult(
            check_id="fleet_tui_binary",
            scope="fleet",
            severity=Severity.UNKNOWN,
            headroom="no machine reports a coord-tui binary",
            detail=(
                "build and install it (`cd tui && cargo build && cp "
                "target/debug/coord-tui ~/.local/bin/coord-tui`) on the "
                "machine you actually run the tui from, or set "
                "health.tui_binary_path if it lives elsewhere"
            ),
            values={"machines": facts},
        )

    # Compare each machine against ITS OWN source tree — a machine with no
    # coord-tui checkout has nothing to compare against and is left out of the
    # verdict (present, uncomparable), same as the single-machine case
    # always was; it just no longer speaks for the whole fleet.
    stale: list[tuple[str, float, float]] = []
    comparable: list[str] = []
    for name, values in present.items():
        binary_mtime = values.get("binary_mtime")
        source_mtime = values.get("source_mtime")
        if binary_mtime is None or source_mtime is None:
            continue
        comparable.append(name)
        if source_mtime > binary_mtime:
            stale.append((name, binary_mtime, source_mtime))

    if stale:
        # Worst (most hours stale) first, but name every stale machine —
        # this check exists specifically so a stale lane is never silent.
        stale.sort(key=lambda t: (t[2] - t[1]), reverse=True)
        names = ", ".join(n for n, _, _ in stale)
        _worst_name, worst_bm, worst_sm = stale[0]
        stale_hours = (worst_sm - worst_bm) / 3600.0
        return CheckResult(
            check_id="fleet_tui_binary",
            scope="fleet",
            severity=Severity.WARN,
            headroom=f"{names}: binary is {stale_hours:.1f}h older than coord-tui source",
            detail=f"rebuild the coord-tui binary on: {names}",
            threshold="warn when any machine's source/ is newer than its built binary",
            values={"machines": facts, "stale": [n for n, _, _ in stale]},
        )

    if not comparable:
        return CheckResult(
            check_id="fleet_tui_binary",
            scope="fleet",
            severity=Severity.OK,
            headroom=(
                f"binary present on {', '.join(sorted(present))} "
                "(no source tree found to compare)"
            ),
            values={"machines": facts},
        )

    return CheckResult(
        check_id="fleet_tui_binary",
        scope="fleet",
        severity=Severity.OK,
        headroom=f"up to date with coord-tui source on {', '.join(sorted(comparable))}",
        values={"machines": facts},
    )


@check(
    id="fleet_webapp_bundle",
    scope="fleet",
    title="webapp bundle",
    order=13,
    description=(
        "The dist/ bundle `coord web --dist` serves is not older than the "
        "`coord-web` source tree it was supposedly built from, "
        "on every machine that has one (#1834 lane 5, #2470)."
    ),
)
def probe_webapp_bundle(ctx: HealthContext) -> CheckResult:
    """Aggregates every machine's own ``webapp_bundle`` machine-scope check
    — the same "measure locally, judge centrally" split :func:`probe_tui_binary`
    uses above, for the same reason: the machine actually running `coord web`
    is very often not the daemon host.

    Deliberately NOT compared against the released wheel's version, unlike
    every other lane in this module: `coord-web-dist-build.timer` publishes
    continuously off `origin/main`'s SHA (#1543), decoupled on purpose from
    the ~/.coord-venv release cadence, so "is it at the released version?" is
    not a well-formed question for it — see docs/AGENT_OPERATIONS.md's
    `coord release verify` section. What IS well-formed, and what this
    checks: whether the live bundle is stale relative to the source it
    claims to have been built from, and whether machines that both run
    `coord web` agree on which bundle that is.
    """
    if ctx.fleet is None:
        return CheckResult(
            check_id="fleet_webapp_bundle",
            scope="fleet",
            severity=Severity.UNKNOWN,
            headroom="no fleet snapshot (fleet checks only run on the daemon)",
        )

    facts = _machine_check_values(ctx, "webapp_bundle")
    present = {name: v for name, v in facts.items() if v.get("present")}

    if not present:
        return CheckResult(
            check_id="fleet_webapp_bundle",
            scope="fleet",
            severity=Severity.UNKNOWN,
            headroom="no machine reports a coord-web-dist bundle",
            detail=(
                "install deploy/coord-web-dist-build.service + .timer on the "
                "machine that runs `coord web` (see docs/AGENT_OPERATIONS.md), "
                "or set health.webapp_dist_path if it lives elsewhere"
            ),
            values={"machines": facts},
        )

    # Compare each machine against ITS OWN source tree, same reasoning as
    # fleet_tui_binary: a machine with no webapp/ checkout has nothing to
    # compare against and is left out of the verdict (present, uncomparable).
    stale: list[tuple[str, float, float]] = []
    comparable: list[str] = []
    for name, values in present.items():
        dist_mtime = values.get("dist_mtime")
        source_mtime = values.get("source_mtime")
        if dist_mtime is None or source_mtime is None:
            continue
        comparable.append(name)
        if source_mtime > dist_mtime:
            stale.append((name, dist_mtime, source_mtime))

    if stale:
        stale.sort(key=lambda t: (t[2] - t[1]), reverse=True)
        names = ", ".join(n for n, _, _ in stale)
        _worst_name, worst_dm, worst_sm = stale[0]
        stale_hours = (worst_sm - worst_dm) / 3600.0
        return CheckResult(
            check_id="fleet_webapp_bundle",
            scope="fleet",
            severity=Severity.WARN,
            headroom=f"{names}: bundle is {stale_hours:.1f}h older than webapp/ source",
            detail=f"check coord-web-dist-build.timer on: {names}",
            threshold="warn when any machine's webapp/ source is newer than its live bundle",
            values={"machines": facts, "stale": [n for n, _, _ in stale]},
        )

    # Two machines both running `coord web` but serving different builds is
    # its own drift, independent of staleness against source — most fleets
    # only ever have one, but when there are two they must agree.
    shas = {v.get("sha") for v in present.values() if v.get("sha")}
    if len(shas) > 1:
        by_sha: dict[str, list[str]] = {}
        for name, v in present.items():
            sha = v.get("sha")
            if sha:
                by_sha.setdefault(sha, []).append(name)
        skew_desc = "; ".join(
            f"{sha}: {', '.join(sorted(names))}" for sha, names in sorted(by_sha.items())
        )
        return CheckResult(
            check_id="fleet_webapp_bundle",
            scope="fleet",
            severity=Severity.WARN,
            headroom=f"{len(shas)} different bundles live across the fleet",
            detail=skew_desc,
            threshold="warn when machines serving coord web disagree on the live bundle",
            values={"machines": facts},
        )

    if not comparable:
        return CheckResult(
            check_id="fleet_webapp_bundle",
            scope="fleet",
            severity=Severity.OK,
            headroom=(
                f"bundle present on {', '.join(sorted(present))} "
                "(no source tree found to compare)"
            ),
            values={"machines": facts},
        )

    return CheckResult(
        check_id="fleet_webapp_bundle",
        scope="fleet",
        severity=Severity.OK,
        headroom=f"up to date with webapp/ source on {', '.join(sorted(comparable))}",
        values={"machines": facts},
    )
