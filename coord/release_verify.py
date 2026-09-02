"""``coord release verify`` — does every deploy lane on every host actually
reflect the released version? (#1834)

The question this answers did not previously have an answer anywhere. On
2026-08-04, hours after v0.4.105 shipped, four independent readouts all said
the fleet was on 0.4.105 and the board daemon was spawning 0.4.103. Nothing
was lying: each readout was reading a different lane, correctly, and no
command compared them.

Two design rules, both learned the expensive way, both load-bearing here:

**Report skew BETWEEN lanes, not staleness WITHIN one.** Every individual
lane was green on 2026-08-04. The defect existed only as a relationship —
daemon 0.4.105 spawning 0.4.103 — so a report that grades each lane against
an absolute and never compares them would have passed. :func:`verify`
therefore computes the lane set first and grades the *disagreement*;
``--expected`` narrows that to "disagreement with a named version" but is
never required for the check to bite.

**Verify the running process, not the venv.** ``pip install --upgrade``
silently no-ops often enough to be a documented fleet gotcha, so a venv
reporting the right version proves nothing about what executes. The
``spawns`` lanes (:mod:`coord.health.checks.spawned_coord`) are the ones that
read a live process; the rest read installs and are kept because a fleet can
be wrong in both ways at once.

**Read-only, always.** ``coord diagnose`` is a documented trap for having
write side effects; this command must be safe mid-flight. It issues GETs to
each agent's ``/health`` and (optionally) the daemon's ``/board`` and does
nothing else. Fixing drift is explicitly *not* its job — ``coord agent
update`` owns that lane, and an automatic ``systemctl`` write across every
host is a far bigger blast radius than detection.

**Thin-client capable.** Lane facts ride the transport that already exists:
every machine computes its own machine-scope health checks and serves them
at ``/health``, and the daemon publishes its own process-local facts in
``/board``'s ``fleet_health`` block. Nothing here shells out over ssh, so it
works from a laptop that holds no credentials and no checkout.

**Masked-by-policy is not stale (#3049).** A unit masked on purpose — this
fleet's ``coord-release-propagate``/``coord-release-window`` lanes, masked
because release rolls here are manually initiated by choice — reads
identically to genuine neglect to the ``unit_drift`` machine-scope check: a
masked unit's installed copy is a symlink to ``/dev/null``, which always
content-diffs against ``deploy/<name>``. That probe still reports the raw
WARN (severity is its call, not this module's), but it also checks the same
intent sentinel the fleet watchdog already honours
(``~/.coord/watchdog-suppress.json``, #2580) and hands this module the
verdict. ``findings_for_host`` is the policy-aware layer: a suppressed
``unit_drift`` WARN renders here as "masked by policy" with the sentinel's
reason and ``set`` date, never as a WARN carrying a ``cp``/``restart``
remedy — following that remedy verbatim un-masks the unit and re-arms
exactly what the masking exists to prevent. A masked unit with no sentinel
entry keeps WARNing exactly as before; the sentinel is the signal, not the
masking.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

# Lane severities, deliberately the same vocabulary as coord.health so a
# reader moving between `coord health` and `coord release verify` never has
# to translate. UNKNOWN outranks OK (an unverified lane is not a verified
# one) and is outranked by WARN (it must never page).
SEVERITY_RANK = {"ok": 0, "unknown": 1, "warn": 2, "crit": 3}

EXIT_OK = 0
EXIT_WARN = 1
EXIT_CRIT = 2

# How each machine-scope check id projects into a lane row. The value is the
# lane's display name template; `{machine}` is substituted. Ordering here is
# the ordering in the report.
_VERSION_LANES: tuple[tuple[str, str], ...] = (
    ("agent_venv", "~/.coord-venv"),
    ("cli_venv", "~/.coord-cli-venv"),
)


@dataclass(frozen=True)
class Lane:
    """One (host, lane) row: what version that lane is actually on.

    ``version=None`` means "no data", which is emphatically not "agrees with
    everyone else" — the whole point of #1834 is that a lane nobody can see
    is the one that bites.
    """

    host: str
    lane: str
    version: str | None = None
    editable: bool | None = None
    # Free-text context for the report (a resolved path, a unit name).
    detail: str = ""
    # True for lanes that read a LIVE process rather than an install.
    process: bool = False

    @property
    def label(self) -> str:
        return f"{self.lane} ({self.host})"


@dataclass(frozen=True)
class Finding:
    """Something wrong, named down to the host and the lane."""

    severity: str
    host: str
    lane: str
    summary: str
    detail: str = ""

    @property
    def rank(self) -> int:
        return SEVERITY_RANK.get(self.severity, 1)


@dataclass
class VerifyReport:
    expected: str | None = None
    lanes: list[Lane] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    # Hosts that could not be reached at all, with the reason.
    unreachable: dict[str, str] = field(default_factory=dict)

    @property
    def severity(self) -> str:
        worst = "ok"
        for f in self.findings:
            if f.rank > SEVERITY_RANK[worst]:
                worst = f.severity
        return worst

    @property
    def ok(self) -> bool:
        return self.severity == "ok"

    @property
    def exit_code(self) -> int:
        sev = self.severity
        if sev == "crit":
            return EXIT_CRIT
        if sev in ("warn", "unknown"):
            return EXIT_WARN
        return EXIT_OK

    @property
    def versions(self) -> dict[str, list[str]]:
        """version -> the lane labels on it, for every lane with data."""
        out: dict[str, list[str]] = {}
        for lane in self.lanes:
            if lane.version:
                out.setdefault(lane.version, []).append(lane.label)
        for labels in out.values():
            labels.sort()
        return out

    def to_dict(self) -> dict:
        return {
            "schema": 1,
            "expected": self.expected,
            "severity": self.severity,
            "exit_code": self.exit_code,
            "unreachable": dict(self.unreachable),
            "versions": self.versions,
            "lanes": [
                {
                    "host": lane.host,
                    "lane": lane.lane,
                    "version": lane.version,
                    "editable": lane.editable,
                    "detail": lane.detail,
                    "process": lane.process,
                }
                for lane in self.lanes
            ],
            "findings": [
                {
                    "severity": f.severity,
                    "host": f.host,
                    "lane": f.lane,
                    "summary": f.summary,
                    "detail": f.detail,
                }
                for f in self.findings
            ],
        }


# ──────────────────────────────────────────────────────────────────────────
# Projection: /health payload -> lanes + findings
# ──────────────────────────────────────────────────────────────────────────


def _results(health: dict | None) -> list[dict]:
    """The machine-scope check rows inside an agent's ``/health`` body.

    The agent nests its health report under a ``health`` key alongside
    ``version``/``tool_versions``/... (``coord/agent.py``); a payload without
    one is an older agent, which is "no data", not "no findings".
    """
    if not health:
        return []
    block = health.get("health") or {}
    return [r for r in (block.get("results") or []) if isinstance(r, dict)]


def _rows(health: dict | None, check_id: str) -> list[dict]:
    return [r for r in _results(health) if r.get("check_id") == check_id]


def lanes_for_host(host: str, health: dict | None) -> list[Lane]:
    """Every deploy lane *host* can speak for, projected from its ``/health``.

    Pure and side-effect free so the whole projection is unit-testable from a
    dict — the transport is the part that needs a live fleet, the judgement
    is not.
    """
    lanes: list[Lane] = []

    for check_id, name in _VERSION_LANES:
        for row in _rows(health, check_id):
            values = row.get("values") or {}
            # cli_venv reports present=False on the many machines that never
            # had one; that is a genuine absence, not a missing lane.
            if check_id == "cli_venv" and not values.get("present"):
                continue
            if row.get("error"):
                lanes.append(Lane(host=host, lane=name, version=None,
                                  detail=str(row.get("error"))[:200]))
                continue
            lanes.append(
                Lane(
                    host=host,
                    lane=name,
                    version=values.get("version") or None,
                    editable=values.get("editable"),
                )
            )

    for row in _rows(health, "spawned_coord"):
        values = row.get("values") or {}
        unit = row.get("subject") or values.get("unit")
        if not unit:
            # The "no coord service running here" singleton row.
            continue
        if values.get("fallback"):
            # No `coord` on the service PATH: coord_argv() falls back to the
            # parent's own interpreter, which cannot be skewed against the
            # parent. Not a lane, and not a gap.
            continue
        lanes.append(
            Lane(
                host=host,
                lane=f"{unit} spawns",
                version=values.get("version") or None,
                editable=values.get("editable"),
                detail=str(values.get("resolved") or ""),
                process=True,
            )
        )

    # ── coord-agent process — the agent's OWN running version (#2841) ────
    # `version` is `coord.__version__`, imported once at process start and
    # frozen for that process's whole life (`coord/agent_app.py`'s health
    # handler: `data["version"] = __version__`). That is exactly what #1834
    # means by "verify the running process, not the venv" — unlike
    # `coord-agent spawns` above, which resolves a *fresh* `coord --version`
    # subprocess through the service PATH and therefore reports the new
    # version the instant a blue/green swap flips `~/.coord-venv`, whether or
    # not the agent that would spawn it has itself restarted.
    #
    # That distinction is cosmetic almost everywhere, because the agent
    # self-restarts on an idle swap (`_idle_restart_target`) and the two
    # lanes reconverge within one poll cycle. It stops being cosmetic on the
    # daemon host: `_idle_restart_target` deliberately returns `None` there
    # (a self-restarted agent would out-rank the `coord-serve` sitting next
    # to it — see that function's docstring), so the daemon host's agent is
    # the one case where nothing but this lane ever reads a staged-but-
    # unrestarted swap as behind. #2069 gave `coord-serve` a `process` lane
    # for the identical reason; this is that fix one unit over, for
    # `coord-agent`.
    if health is not None and "version" in health:
        lanes.append(
            Lane(
                host=host,
                lane="coord-agent process",
                version=health.get("version") or None,
                process=True,
            )
        )

    return lanes


def findings_for_host(host: str, health: dict | None) -> list[Finding]:
    """Per-lane findings that are true regardless of what any other lane says.

    Version skew is NOT computed here — it is a relationship between lanes
    and belongs to :func:`verify`. What lives here is the set of defects that
    are wrong on their own terms: an editable install on a service PATH, a
    unit file that drifted from ``deploy/``, a stale ``coord-tui`` binary, an
    unreadable lane.
    """
    out: list[Finding] = []

    # ── the process whose code changed underneath it (#2121) ─────────────
    # An agent publishes two different reads of its own version: `version`
    # is the module it loaded at import time (fixed for the life of the
    # process) and `installed_version` is a fresh `importlib.metadata` read
    # of the site-packages that same process resolves through. A correct
    # blue/green update cannot make those disagree — the swap moves the
    # `~/.coord-venv` symlink, while a running process stays pinned to the
    # slot it started from, so *both* reads stay on the old version until
    # it restarts. They disagree only when the files under the running
    # process's own sys.path were **replaced in place**.
    #
    # That is a different and worse condition than "this host is behind",
    # and until now it graded identically: a routine `CRIT ... on 0.5.36,
    # expected 0.5.37`, the same line a merely-lagging host gets. A host
    # that is behind is running code that exists; this one is running code
    # that no longer exists on disk, in a process that will load the *new*
    # version for anything it imports from here on. It reads differently.
    running = (health or {}).get("version")
    installed = (health or {}).get("installed_version")
    if running and installed and running != installed:
        out.append(
            Finding(
                severity="crit",
                host=host,
                lane="coord-agent process",
                summary=(
                    f"MIXED-VERSION PROCESS: running v{running} from an "
                    f"install that is now v{installed}"
                ),
                detail=(
                    "this process's site-packages was REPLACED UNDERNEATH IT "
                    "— it holds already-imported modules at the old version "
                    "and loads anything imported later at the new one. This "
                    "is not a host that is merely behind: a blue/green swap "
                    "never produces this, so something wrote into the venv "
                    "colour a live process was executing from (#2121). "
                    "Restart it (`systemctl --user restart coord-agent`) and "
                    "find the install in `coord audit --type venv_install`."
                ),
            )
        )

    # ── editable installs ────────────────────────────────────────────────
    # #1834: "any editable install on a service PATH is a finding on its own,
    # independent of its current version — it is a drift amplifier that
    # silently tracks a checkout nothing keeps current."
    for row in _rows(health, "agent_venv"):
        if (row.get("values") or {}).get("editable"):
            out.append(
                Finding(
                    severity="crit",
                    host=host,
                    lane="~/.coord-venv",
                    summary="agent venv is an EDITABLE install",
                    detail=(
                        "this machine runs whatever branch its checkout is "
                        "parked on; no release can account for its behaviour"
                    ),
                )
            )

    for row in _rows(health, "spawned_coord"):
        values = row.get("values") or {}
        unit = row.get("subject") or values.get("unit") or "unit"
        if values.get("editable"):
            out.append(
                Finding(
                    severity="crit",
                    host=host,
                    lane=f"{unit} spawns",
                    summary=f"{unit} would spawn an EDITABLE checkout",
                    detail=(
                        f"{values.get('resolved') or 'coord'} resolves to "
                        f"{values.get('module_file') or 'a checkout'} on this "
                        "unit's live PATH — a drift amplifier regardless of "
                        "the version it happens to report today"
                    ),
                )
            )
        elif row.get("severity") == "unknown" and not values.get("fallback"):
            out.append(
                Finding(
                    severity="unknown",
                    host=host,
                    lane=f"{unit} spawns",
                    summary=f"could not read what {unit} would spawn",
                    detail=str(row.get("headroom") or row.get("error") or ""),
                )
            )

    # ── unit files vs the packaged release units (#1831, #1927, folded in
    # per #1834's lane 3) ────────────────────────────────────────────────
    # UNKNOWN rows are findings too (#1927): the machine-scope check grades a
    # match against an *unverified* reference — a working copy nothing keeps
    # current — as UNKNOWN rather than OK, because a stale checkout and a
    # stale installed unit go stale together and agree. Dropping those rows
    # here would restore precisely the false green this fold-in exists to
    # surface. UNKNOWN outranks OK and is outranked by WARN, so it annotates
    # the report without paging.
    # #3049: a unit masked ON PURPOSE (a fleet that chose manual release
    # rolls masks the propagate/window lanes deliberately) reads identically
    # to genuine neglect to `unit_drift` — a masked unit's installed copy is
    # a symlink to /dev/null, which content-diffs against `deploy/<name>`
    # exactly like a three-week-stale one would. `unit_drift` still reports
    # the honest WARN (deciding whether drift is *wanted* is not its job —
    # see that probe's own #3049 note) but also checks the SAME intent
    # sentinel the watchdog already honours
    # (`~/.coord/watchdog-suppress.json`, #2580) and publishes the verdict in
    # `values["suppressed"]`. THIS layer is the policy-aware one: a
    # suppressed WARN renders as "masked by policy" — no WARN, and
    # critically no `cp .../restart` remedy, because following that remedy
    # verbatim un-masks the unit and re-arms exactly what the masking exists
    # to prevent. A unit with no sentinel entry (`suppressed` false/absent)
    # keeps WARNing exactly as before — the sentinel is the signal, not the
    # masking (#3049's own acceptance line).
    for row in _rows(health, "unit_drift"):
        sev = row.get("severity")
        lane = f"unit {row.get('subject') or '?'}"
        values = row.get("values") or {}
        if sev == "warn" and values.get("suppressed"):
            reason = values.get("suppress_reason") or "no reason recorded"
            set_on = values.get("suppress_set")
            when = f", set {set_on}" if set_on else ""
            out.append(
                Finding(
                    severity="ok",
                    host=host,
                    lane=lane,
                    summary=f"masked by policy: {reason}{when}",
                    detail=(
                        "suppressed via ~/.coord/watchdog-suppress.json — the "
                        "drift is intentional; do NOT run the cp/restart "
                        "remedy, it re-arms the auto-release this masking "
                        "exists to prevent (#3049)"
                    ),
                )
            )
            continue
        if sev in ("crit", "warn", "unknown"):
            out.append(
                Finding(
                    severity=str(sev),
                    host=host,
                    lane=lane,
                    summary=str(row.get("headroom") or "unit drift"),
                    detail=str(row.get("detail") or ""),
                )
            )

    # ── coord-tui: staleness only, NEVER a version vs. `expected` ────────
    # #2102's grading rule, and #2898 makes it permanent rather than
    # provisional: this lane reports staleness (the binary's mtime vs. its
    # `tui/` source tree — see coord.health.checks.deploy_lane_facts), never
    # a version compared against `expected`/`--pypi`.
    #
    # #2102's reason was that a `tui/`-only range published no PyPI wheel, so
    # PyPI's "latest" sat behind a fresh `coord tui update` on purpose and
    # grading against it read every such release as "ahead of expected"
    # forever. #2898 (phase 3 of #2894) replaces that contingent reason with
    # a structural one: coord-tui releases from its OWN repo on its OWN `v*`
    # tag line, so `expected` — the coordinator's channel's version — is not
    # a number coord-tui's version is even comparable to. A fleet on coord
    # v0.5.31 with coord-tui v0.2.7 is CORRECT. Grading one against the other
    # would report every such fleet as ~29 releases behind, permanently, and
    # `coord release propagate --rollback-on-red` gates on this report.
    #
    # That is also why `lanes_for_host` never emits a `coord-tui` Lane: a
    # WARN here is a real, actionable finding, but it must never enter
    # `report.versions`' skew map, which exists precisely to compare like
    # with like. The right home for "is this coord-tui build wire-compatible
    # with this daemon?" is coord-tui's own CI (the phase-3 ADR) — it was
    # never answerable by a version-number diff, it only looked answerable
    # while one tag stamped both repos.
    for row in _rows(health, "tui_binary"):
        if row.get("severity") == "warn":
            out.append(
                Finding(
                    severity="warn",
                    host=host,
                    lane="coord-tui",
                    summary=str(row.get("headroom") or "tui binary is stale"),
                    detail=str(row.get("detail") or ""),
                )
            )

    # ── webapp bundle vs coord-web's own source tree (#1834 lane 5, #2470) ─
    # Deliberately not a Lane/version comparison — see release_verify.py's
    # module docstring and coord.health.checks.fleet_deploy_lanes: the
    # bundle is versioned by origin/main's SHA on a continuous publish
    # timer (#1543), never by the pip release every other lane compares
    # against, so folding it into `versions` would manufacture permanent,
    # meaningless skew rather than report a real one.
    for row in _rows(health, "webapp_bundle"):
        if row.get("severity") == "warn":
            out.append(
                Finding(
                    severity="warn",
                    host=host,
                    lane="webapp bundle",
                    summary=str(row.get("headroom") or "webapp bundle is stale"),
                    detail=str(row.get("detail") or ""),
                )
            )

    return out


#: The systemd user unit that IS the daemon. A machine whose ``spawned_coord``
#: check reports a row for this unit is, by definition, the machine running
#: ``coord-serve`` — that check only emits a row per unit it found *running*
#: (see :func:`coord.health.checks.spawned_coord.running_unit_pids`).
DAEMON_UNIT = "coord-serve"


def daemon_host_from_health(machine_health: dict[str, dict | None]) -> str | None:
    """Which machine actually runs ``coord-serve``, derived — never guessed.

    #2052 fault 2: ``coord release propagate`` could not identify the daemon
    host and fell back to ``coordinator.yml`` order, which during a partial
    revert put the daemon *behind* its callers — precisely the 405 hazard the
    lane order exists to prevent. The daemon host is not a mystery: it is the
    machine with a running ``coord-serve``, and every agent already publishes
    exactly that fact in its own ``/health`` (``spawned_coord`` emits one row
    per *running* coord unit).

    Returns ``None`` when nobody reports one **or when more than one machine
    does** — two live daemons is a fault in its own right, and a caller that
    must order a roll around "the" daemon should refuse rather than pick one.
    """
    found = [
        host
        for host in sorted(machine_health)
        if any(
            (row.get("subject") or (row.get("values") or {}).get("unit")) == DAEMON_UNIT
            for row in _rows(machine_health[host], "spawned_coord")
        )
    ]
    return found[0] if len(found) == 1 else None


def daemon_lanes(daemon_host: dict | None, *, host: str = "daemon") -> list[Lane]:
    """The lanes only the ``coord-serve`` process itself can report.

    ``coord_serve_version`` is genuinely process-local: it is the daemon
    introspecting its own interpreter, which no other machine can do for it
    (that conflation is what #1806 was about). It arrives via ``/board``'s
    ``fleet_health.daemon_host`` block.
    """
    if not daemon_host:
        return []
    if not {"coord_serve_version", "coord_serve_editable"} & set(daemon_host):
        return []
    version = daemon_host.get("coord_serve_version")
    editable = daemon_host.get("coord_serve_editable")
    # A published-but-null version is a lane with no data, NOT an absent lane:
    # it still emits an UNKNOWN row below rather than vanishing from the table.
    return [
        Lane(
            host=host,
            lane="coord-serve process",
            version=version or None,
            editable=editable,
            process=True,
        )
    ]


# ──────────────────────────────────────────────────────────────────────────
# The verdict
# ──────────────────────────────────────────────────────────────────────────


def verify(
    *,
    machine_health: dict[str, dict | None],
    daemon_host: dict | None = None,
    unreachable: dict[str, str] | None = None,
    expected: str | None = None,
    daemon_host_name: str = "daemon",
) -> VerifyReport:
    """Grade a whole fleet's deploy lanes. Pure: no I/O, no config, no clock.

    *machine_health* maps machine name -> that machine's ``/health`` body (or
    ``None`` when it answered with nothing usable). *unreachable* maps
    machine name -> why, for hosts that did not answer at all.

    An unreachable host is UNKNOWN, never OK: #1834's whole thesis is that a
    lane nobody looked at is the one that drifts, so "we could not ask" must
    not render as "verified".
    """
    report = VerifyReport(expected=expected, unreachable=dict(unreachable or {}))

    for host in sorted(machine_health):
        health = machine_health[host]
        report.lanes.extend(lanes_for_host(host, health))
        report.findings.extend(findings_for_host(host, health))

    report.lanes.extend(daemon_lanes(daemon_host, host=daemon_host_name))

    for host, reason in sorted(report.unreachable.items()):
        report.findings.append(
            Finding(
                severity="unknown",
                host=host,
                lane="(all lanes)",
                summary="host unreachable — its lanes are unverified",
                detail=reason,
            )
        )

    # ── lanes with no data ───────────────────────────────────────────────
    for lane in report.lanes:
        if lane.version is None:
            report.findings.append(
                Finding(
                    severity="unknown",
                    host=lane.host,
                    lane=lane.lane,
                    summary="no version reported for this lane",
                    detail=lane.detail,
                )
            )

    if not report.lanes and not report.unreachable:
        report.findings.append(
            Finding(
                severity="unknown",
                host="(fleet)",
                lane="(all lanes)",
                summary="no host reported a single deploy lane",
                detail=(
                    "every machine answered, none had health results — check "
                    "that the agents are running a release with the health "
                    "engine (#1628) enabled"
                ),
            )
        )

    # ── the relationship, which is the actual check ──────────────────────
    versions = report.versions
    if expected:
        for version, labels in sorted(versions.items()):
            if version == expected:
                continue
            report.findings.append(
                Finding(
                    severity="crit",
                    host=_hosts_of(report, labels),
                    lane=", ".join(labels),
                    summary=f"on {version}, expected {expected}",
                    detail=(
                        "the released version is not what this lane is "
                        "actually running"
                    ),
                )
            )
    else:
        # No --expected: skew alone is the finding. This is the 2026-08-04
        # shape — nobody knew what to expect, but two lanes disagreeing was
        # already conclusive.
        if len(versions) > 1:
            spread = "; ".join(
                f"{v}: {', '.join(labels)}" for v, labels in sorted(versions.items())
            )
            report.findings.append(
                Finding(
                    severity="crit",
                    host="(fleet)",
                    lane="(version skew)",
                    summary=f"{len(versions)} versions live across the fleet",
                    detail=spread,
                )
            )
        elif versions:
            # #2035 item 4, demonstrated by #2052: after a botched
            # propagation reverted the whole fleet to 0.4.104 while `main`
            # was four releases ahead, this command reported `crit=0` — it
            # compares the fleet against *itself*, so **uniform staleness
            # reads as health**. A skew-only run can never see that, and
            # must therefore never render as a clean bill of health.
            report.findings.append(
                Finding(
                    severity="unknown",
                    host="(fleet)",
                    lane="(expected version)",
                    summary=(
                        "no expected version to grade against — a fleet that "
                        "is uniformly BEHIND reads as clean here"
                    ),
                    detail=(
                        f"every lane agrees on {next(iter(sorted(versions)))}, "
                        "but nothing here knows whether that is the released "
                        "version. Pass --expected vX.Y.Z, or --pypi to resolve "
                        "it from the simple index."
                    ),
                )
            )

    report.findings.sort(key=lambda f: (-f.rank, f.host, f.lane))
    return report


def _hosts_of(report: VerifyReport, labels: Iterable[str]) -> str:
    wanted = set(labels)
    hosts = sorted({lane.host for lane in report.lanes if lane.label in wanted})
    return ", ".join(hosts) if hosts else "(fleet)"


# ──────────────────────────────────────────────────────────────────────────
# Transport (thin-client capable; every call is a GET)
# ──────────────────────────────────────────────────────────────────────────


def gather(
    config: Any,
    *,
    timeout: float = 5.0,
    machine_filter: str | None = None,
    check_machine: Callable[..., Any] | None = None,
    board_payload: Callable[[], dict] | None = None,
) -> tuple[dict[str, dict | None], dict[str, str], dict | None, str]:
    """Poll the fleet. Returns ``(machine_health, unreachable, daemon_host,
    daemon_host_name)``.

    Injectable seams (*check_machine*, *board_payload*) exist so the whole
    command can be driven in tests without a live fleet — and so this module
    never has to care whether the board came from a local DB or from a
    daemon over Tailscale.
    """
    from coord import network  # noqa: PLC0415 — import cycle at module scope

    probe = check_machine or network.check_machine

    machines = list(getattr(config, "machines", ()) or ())
    if machine_filter:
        machines = [m for m in machines if m.name == machine_filter]

    health: dict[str, dict | None] = {}
    unreachable: dict[str, str] = {}
    for machine in machines:
        try:
            status = probe(machine, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 — a probe must never abort the sweep
            unreachable[machine.name] = f"{type(exc).__name__}: {exc}"
            continue
        if not getattr(status, "is_online", False):
            unreachable[machine.name] = (
                getattr(status, "reason", None) or getattr(status, "state", "offline")
            )
            continue
        health[machine.name] = getattr(status, "health", None)

    daemon_host, daemon_name = _daemon_facts(board_payload)
    # #2052 fault 2: label the daemon's lane with the machine that actually
    # runs it whenever the fleet's own health says which one that is. The
    # literal "daemon" placeholder is a last resort, not a name — a lane
    # labelled after nothing cannot be matched to a host by anything
    # downstream (which is how propagation ended up guessing at config order).
    derived = daemon_host_from_health(health)
    if derived:
        daemon_name = derived
    return health, unreachable, daemon_host, daemon_name


#: The lane name `coord.health.checks.fleet_deploy_lanes` publishes for the
#: daemon's own install. Matched by string because that is the only contract
#: `/board` offers; `tests/test_release_verify.py` pins the two together so a
#: rename over there fails here loudly instead of silently dropping the lane.
DAEMON_SERVE_LANE = "coord-serve (daemon host)"

#: Deliberately NOT the per-host `--timeout`. `/board` is a multi-megabyte
#: read on a real fleet and routinely takes several seconds; a recorded
#: operational gotcha is that a 5s budget makes a healthy daemon look
#: unreachable. Timing out here would silently drop the `coord-serve process`
#: lane — precisely the lane #1834 exists to stop losing — so it gets its own,
#: generous budget.
_BOARD_TIMEOUT = 30.0


def _default_board_fetch() -> dict:
    """``GET /board``, from a thin client *or* from the daemon host itself.

    A thin client has ``board_service`` configured and reads it over
    Tailscale. On the daemon host ``resolve_board_service()`` returns None
    (host mode reads the DB directly) — and without the loopback fallback
    below, running ``coord release verify`` *on the daemon host* would
    silently drop the ``coord-serve process`` lane, which is exactly the lane
    #1834 exists to stop losing. The daemon's own version cannot be read any
    other way from a sibling process: it is introspection of a running
    interpreter (#1806), so ``/board`` is the only publisher.
    """
    from coord.client import (  # noqa: PLC0415
        ServiceConfig,
        fetch_board_payload,
        resolve_board_service,
    )

    svc = resolve_board_service()
    if svc is None:
        from coord.commands._common import SERVE_PORT  # noqa: PLC0415
        from coord.serve_app import resolve_serve_token  # noqa: PLC0415

        svc = ServiceConfig(
            url=f"http://127.0.0.1:{SERVE_PORT}", token=resolve_serve_token()
        )
    return fetch_board_payload(svc, timeout=_BOARD_TIMEOUT)


def _daemon_facts(board_payload: Callable[[], dict] | None) -> tuple[dict | None, str]:
    """The ``coord-serve`` process's own version, read out of ``/board``.

    ``coord-serve``'s version can only be introspected from the process
    actually running it (#1806) — no other machine's ``/health`` can speak
    for it. The daemon gathers it into its fleet snapshot and publishes it
    inside ``fleet_health.fleet_checks``' ``fleet_deploy_lanes`` row, which
    is the only place a thin client can read it from. (The richer internal
    ``daemon_host`` fact block, which also carries ``coord_serve_editable``,
    is deliberately not on the wire; editability of the daemon's own install
    therefore reads as unknown from a thin client rather than as ``False``.)

    Absent on a fleet whose daemon predates the health engine, and absent in
    a host-mode run with no daemon configured. Both are "no data" for the
    ``coord-serve process`` lane, which :func:`verify` reports as UNKNOWN
    rather than skipping — a daemon nobody could ask is exactly the lane
    2026-08-04 hid in.
    """
    fetch = board_payload or _default_board_fetch

    try:
        payload = fetch() or {}
    except Exception:  # noqa: BLE001 — read-only, best effort, never fatal
        return None, "daemon"

    fleet_health = payload.get("fleet_health") or {}
    for row in fleet_health.get("fleet_checks") or []:
        if not isinstance(row, dict) or row.get("check_id") != "fleet_deploy_lanes":
            continue
        lanes = (row.get("values") or {}).get("lanes") or {}
        if DAEMON_SERVE_LANE in lanes:
            return {"coord_serve_version": lanes[DAEMON_SERVE_LANE]}, "daemon"
    return None, "daemon"


# ──────────────────────────────────────────────────────────────────────────
# Rendering
# ──────────────────────────────────────────────────────────────────────────

_SEVERITY_MARK = {"ok": "OK  ", "unknown": "?   ", "warn": "WARN", "crit": "CRIT"}


def render(report: VerifyReport, *, verbose: bool = False) -> str:
    """A per-lane report. Lanes first, then findings, worst first.

    The lane table is printed even on success, deliberately: the failure mode
    this command exists for is a readout that says "fine" while hiding the
    lane it never looked at, so the set of lanes actually inspected is part
    of the answer, not debug output.
    """
    lines: list[str] = []

    if report.expected:
        lines.append(f"expected version: {report.expected}")

    lines.append("lanes:")
    if not report.lanes:
        lines.append("  (none)")
    for lane in sorted(report.lanes, key=lambda l: (l.host, l.lane)):
        version = lane.version or "?"
        marks = []
        if lane.process:
            marks.append("live process")
        if lane.editable:
            marks.append("EDITABLE")
        if lane.detail and verbose:
            marks.append(lane.detail)
        suffix = f"  [{', '.join(marks)}]" if marks else ""
        lines.append(f"  {lane.host:<14} {lane.lane:<26} {version}{suffix}")

    versions = report.versions
    if len(versions) > 1:
        lines.append("")
        lines.append("SKEW:")
        for version, labels in sorted(versions.items()):
            lines.append(f"  {version}: {', '.join(labels)}")

    if report.findings:
        lines.append("")
        lines.append("findings:")
        for f in report.findings:
            lines.append(
                f"  {_SEVERITY_MARK.get(f.severity, '?   ')} "
                f"{f.host}/{f.lane}: {f.summary}"
            )
            if f.detail:
                lines.append(f"         {f.detail}")

    lines.append("")
    counts = {sev: 0 for sev in SEVERITY_RANK}
    for f in report.findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    lines.append(
        f"RELEASE VERIFY: {report.severity.upper()} "
        f"crit={counts.get('crit', 0)} warn={counts.get('warn', 0)} "
        f"unknown={counts.get('unknown', 0)} "
        f"lanes={len(report.lanes)} hosts={len({l.host for l in report.lanes})}"
    )
    return "\n".join(lines)
