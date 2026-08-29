"""Machine onboarding: the six layers a machine must clear to actually be part
of the fleet, and a verifier that answers *is it true right now* (#2915).

This is the machine-side analogue of :mod:`coord.repo_onboard` (#2220), and it
exists for the same reason: onboarding is a hand-assembled sequence gathered
from four documents that do not cross-reference each other
(``docs/AGENT_OPERATIONS.md``, ``docs/GRAPHIFY_SETUP.md``, ``docs/MAC_MINI.md``,
``docs/WSL_WINDOWS_WORKER.md``), **every step of which fails silently**. #2220's
own rationale is the argument: a runbook is the weakest available answer
*"because nothing checks it."*

Onboarding ``dell64`` on 2026-08-28 cost six separate hand-found failures, and
this module's findings are named after them one-for-one:

1. ``install-agent.sh`` left a partial venv that poisoned every retry
   → ``runtime.agent_venv``
2. ``host: dell64`` resolved to a LAN device, not the tailnet node, so the
   board read ``[timeout]`` while ``tailscale ping`` and the agent's own
   ``/health`` were both perfectly healthy
   → ``network.host_resolves_offtailnet`` (reusing #2912's
   :func:`coord.network.check_host_resolution`)
3. the agent came up **config-free**, so its capability probes were empty and
   the #1570 D cross-check silently failed open
   → ``agent.config_free`` / ``agent.capabilities_unpublished``
4. ``repo_paths`` used the checkout's *directory* name instead of the fleet's
   *repo* name, which made the ENTIRE ``coordinator.yml`` fail to load — for
   every machine, not just the new one
   → ``config.repo_path_missing`` (the survivable half; the fatal half can
   only be caught at WRITE time, which is what ``coord machine add`` does)
5. ``~/.coord/coordinator.yml`` on the daemon host had been replaced by a
   regular file, so a correctly committed-and-pushed edit had no effect
   → out of scope here; :mod:`coord.fleet_config_health` already owns it, and
   ``coord machine add``'s residue points at it
6. ``graphify`` was absent, so every graph operation degraded to grep silently
   → ``graph.graphify_missing``

**Read live state, not config.** Same design constraint as
:mod:`coord.repo_onboard`: a finding here is only worth having if it could not
have been produced by reading ``coordinator.yml`` alone. The one deliberate
exception is layer 1 (``config``), which reports the two *structural* defects
that refuse dispatch without any agent being involved (#1801) — those are
cheap, they need no fleet, and they are the reason a brand-new machine that
looks perfect is never routed anything.

Shape mirrors :mod:`coord.repo_onboard`: a **facts** layer that does the I/O
(:func:`gather_facts`), a **pure** evaluator over those facts, and a renderer.
That split is what makes the black-box test in ``tests/test_machine_onboard.py``
possible — a seeded fleet with a deliberately half-onboarded machine produces
one distinct, *named* finding per defect, with no network and no live agents.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — typing only
    from coord.config import Config

# ── Severities ───────────────────────────────────────────────────────────────
# Deliberately the same four strings (and the same marks) as
# `coord.repo_onboard` rather than an import: the two doctors render side by
# side in `coord doctor`, and a divergence there would be invisible until an
# operator saw two different spellings of CRIT in one report.
CRIT = "crit"
WARN = "warn"
OK = "ok"
UNKNOWN = "unknown"

_SEVERITY_MARK = {CRIT: "✗ CRIT", WARN: "⚠ WARN", OK: "✓", UNKNOWN: "?"}
_SEVERITY_RANK = {CRIT: 0, WARN: 1, UNKNOWN: 2, OK: 3}

#: The onboarding layers, in onboarding order — the report reads like the
#: runbook it replaces.
LAYERS: tuple[str, ...] = ("config", "network", "agent", "clones", "graph", "runtime")

#: ``/health`` check ids this module projects into findings. Named here so a
#: rename on the health-registry side is a one-line change rather than a
#: silently-empty layer.
AGENT_VENV_CHECK = "agent_venv"
GRAPHIFY_CLI_CHECK = "graphify_cli"
GRAPH_CHECK = "graph"


@dataclass(frozen=True)
class Finding:
    """One checkable statement about one layer of one machine's onboarding.

    ``check`` is a **stable dotted id** (``network.host_resolves_offtailnet``),
    not a rendered string: it is what the black-box test asserts on, what a
    future ``--json`` consumer keys off, and what keeps "one distinct, named
    finding per defect" from decaying into a generic failure line.
    """

    layer: str
    check: str
    severity: str
    summary: str
    subject: str | None = None
    fix: str | None = None

    @property
    def is_problem(self) -> bool:
        return self.severity in (CRIT, WARN)


# ── Facts (the I/O boundary) ─────────────────────────────────────────────────


@dataclass
class HealthCheckFact:
    """One ``/health`` ``health.results`` row, flattened.

    Only the four fields any finding here needs. Keeping this a plain
    dataclass (rather than passing raw dicts into the evaluator) is what lets
    the tests construct a half-onboarded machine without a live agent.
    """

    check_id: str
    severity: str
    headroom: str = ""
    subject: str | None = None
    detail: str = ""


@dataclass
class MachineFacts:
    """Everything the evaluator is allowed to see. Populated by
    :func:`gather_facts`; constructed directly by tests.

    Every ``*_error``/``None`` is a *distinct* value from "absent": a probe
    that could not run must never render as a clean pass, and must never
    render as the defect either (#1525's rule, as applied by
    :mod:`coord.repo_onboard`).
    """

    name: str
    configured: bool = False

    # ── Layer 1: config ──────────────────────────────────────────────────
    host: str | None = None
    declared_capabilities: list[str] = field(default_factory=list)
    declared_repos: list[str] = field(default_factory=list)
    repo_paths: dict[str, str] = field(default_factory=dict)
    #: Every repo name in the config's own ``repos:`` block — the vocabulary
    #: ``repos:``/``repo_paths:`` entries must be drawn from.
    known_repos: list[str] = field(default_factory=list)

    # ── Layer 2: network ─────────────────────────────────────────────────
    #: ``coord.network.check_host_resolution``'s verdict: ``True`` (host
    #: resolves to this node's tailnet address), ``False`` (it resolves
    #: somewhere else — the #2912 LAN-DNS collision), or ``None`` (no local
    #: tailscale / node not in the peer list / host does not resolve at all,
    #: i.e. absence of evidence).
    host_matches_tailnet: bool | None = None
    host_resolution_reason: str | None = None
    magicdns_fqdn: str | None = None
    reachable: bool | None = None
    unreachable_reason: str | None = None

    # ── Layer 3: agent (all straight out of `/health`) ───────────────────
    config_free: str | None = None
    #: ``None`` when unreachable or when the agent predates the field —
    #: never ``[]``, which is itself a finding.
    published_capabilities: list[str] | None = None
    published_repos: list[str] | None = None
    #: repo name -> why it was dropped from ``published_repos`` (#1527).
    degraded: dict[str, str] = field(default_factory=dict)
    version: str | None = None
    #: Every reachable machine's ``/health`` version, including this one —
    #: the evaluator takes the mode so "vs fleet" needs no second probe.
    fleet_versions: dict[str, str] = field(default_factory=dict)
    #: ``{capability: [reason, ...]}`` from
    #: :func:`coord.prereqs.unmet_capabilities` — a declared capability whose
    #: backing tool this machine's own probe cannot back up.
    unmet_capabilities: dict[str, list[str]] = field(default_factory=dict)

    # ── Layers 5 & 6: the machine's own health registry results ──────────
    health_checks: list[HealthCheckFact] = field(default_factory=list)

    # ── Layer 6: systemd linger (needs SSH — `/health` cannot see it) ────
    linger: bool | None = None
    linger_error: str | None = None

    # ── Layer 6: coord resolvable from a WORKER-shaped PATH (needs SSH —
    # `/health` cannot see it either: it answers from the AGENT's own
    # process, which still has ~/.coord-venv/bin on PATH) ────────────────
    coord_on_worker_path: bool | None = None
    coord_on_worker_path_error: str | None = None
    coord_on_worker_path_version: str | None = None

    def check(self, check_id: str, subject: str | None = None) -> HealthCheckFact | None:
        for row in self.health_checks:
            if row.check_id == check_id and (subject is None or row.subject == subject):
                return row
        return None


@dataclass
class MachineDoctorReport:
    machine_name: str
    findings: list[Finding] = field(default_factory=list)

    def for_layer(self, layer: str) -> list[Finding]:
        return [f for f in self.findings if f.layer == layer]

    @property
    def crits(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == CRIT]

    @property
    def warns(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == WARN]

    @property
    def ok(self) -> bool:
        """True when nothing CRITs.

        Warnings aggregate into ``ok`` on purpose, matching
        :class:`coord.repo_onboard.RepoDoctorReport`: a fleet can run
        indefinitely and deliberately with a stale graph or an unpinned
        capability, and a gate that goes red for those is a gate nobody
        leaves switched on.
        """
        return not self.crits


# ── Fact gathering (the only I/O in this module) ─────────────────────────────


def health_checks_from_health(health: dict | None) -> list[HealthCheckFact]:
    """Flatten a ``/health`` body's ``health.results`` block.

    Best-effort and total: an agent predating the #1630 check registry simply
    has no ``health`` key, which yields ``[]`` — and every finding derived
    from it then reports UNKNOWN rather than a false clean.
    """
    rows = ((health or {}).get("health") or {}).get("results") or []
    out: list[HealthCheckFact] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("check_id"):
            continue
        out.append(
            HealthCheckFact(
                check_id=str(row.get("check_id")),
                severity=str(row.get("severity") or UNKNOWN),
                headroom=str(row.get("headroom") or ""),
                subject=row.get("subject") or None,
                detail=str(row.get("detail") or ""),
            )
        )
    return out


def probe_linger(host: str, *, timeout: float = 20.0) -> tuple[bool | None, str | None]:
    """Is ``loginctl enable-linger`` set for the agent user on *host*?

    The one check here that ``/health`` structurally cannot answer: without
    linger the user manager is torn down at logout and every ``--user`` unit
    (``coord-agent``, the release-propagate timer) dies with it — but only
    *later*, at the next logout or reboot, so a freshly-onboarded machine
    looks perfect right up until it silently disappears from the fleet.

    Returns ``(linger, error)``. Fail-soft in both directions: a machine
    without ``loginctl`` at all (macOS — ``docs/MAC_MINI.md`` uses launchd)
    reports ``(None, reason)``, which renders UNKNOWN, never a defect.
    """
    import subprocess  # noqa: PLC0415

    try:
        result = subprocess.run(
            [
                "ssh", "-o", "BatchMode=yes",
                "-o", f"ConnectTimeout={max(1, int(timeout))}",
                host,
                "loginctl show-user \"$USER\" --property=Linger 2>/dev/null "
                "|| echo Linger=unknown",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"ssh probe failed: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()[-1:]
        return None, f"ssh probe failed: {detail[0] if detail else 'no output'}"

    text = (result.stdout or "").strip()
    if "Linger=yes" in text:
        return True, None
    if "Linger=no" in text:
        return False, None
    return None, "`loginctl` did not report a Linger property (not systemd?)"


# #2937 review: this probe must answer "can a worker resolve `coord`?" using
# the exact same algorithm the agent itself uses to spawn a worker — not a
# second, hand-rolled PATH-stripping implementation that can silently
# disagree with it (e.g. on a trailing slash, or a PATH entry that's already
# an unresolved `.blue`/`.green` path rather than the `~/.coord-venv`
# symlink name). `coord.agent.worker_coord_reachable()` (#2936) is the
# canonical, already-shipped answer to that exact question — built on
# `_worker_subprocess_env`'s realpath-based strip
# (`coord.agent._pinned_venv_bin_dirs`), the same function every real worker
# spawn goes through. So instead of re-deriving the strip over SSH, this
# probe SSHes in and asks the remote host's own pinned interpreter to import
# and call that function directly — the machine-doctor check and the agent's
# own startup check are then provably the same code, not two
# implementations that happen to agree today. The script is sent over
# stdin (`python3 -`) rather than embedded as a `-c` argument so no
# shell-quoting layer stands between it and the interpreter.
_COORD_ON_WORKER_PATH_PROBE_SCRIPT = """\
import subprocess
from coord.agent import worker_coord_reachable, _worker_subprocess_env

ok, _msg = worker_coord_reachable()
version = ""
if ok:
    env = _worker_subprocess_env()
    try:
        result = subprocess.run(
            ["coord", "--version"], env=env, capture_output=True, text=True, timeout=10
        )
        version = (result.stdout or result.stderr or "").strip()
    except Exception:
        version = ""
print("COORD_ON_WORKER_PATH_OK=" + ("1" if ok else "0"))
print("VERSION=" + version.replace("\\n", " "))
"""


def probe_coord_on_worker_path(
    host: str, *, timeout: float = 20.0
) -> tuple[bool | None, str | None, str | None]:
    """Can a WORKER-shaped shell on *host* resolve ``coord`` at all?

    This is #2937's whole gap: workers are spawned with ``~/.coord-venv/bin``
    stripped from ``PATH`` (#402, hardened by #2569) so a `pip install` in a
    worktree can never land in the agent's own runtime venv — but nothing
    upstream of that check ever asks whether ``coord`` is *still resolvable*
    once that directory is gone. dell64 passed every existing layer of
    ``coord machine doctor`` — including ``runtime.agent_venv`` (which only
    proves the AGENT can find its own venv) — while being structurally
    incapable of recording a single test verdict, because the agent's PATH
    was the only PATH anything had checked.

    Delegates the actual found/absent decision to
    :func:`coord.agent.worker_coord_reachable` (#2936), invoked on *host* via
    its own pinned interpreter (``~/.coord-venv/bin/python3``) — see
    :data:`_COORD_ON_WORKER_PATH_PROBE_SCRIPT`. This is deliberately NOT a
    reimplementation of the PATH strip: it is the same function every real
    worker spawn's environment is built from, run where it matters (the
    worker host), so this check and the agent's own startup warning
    (``worker_coord_reachable`` logged at ``AgentServer`` init) can never
    silently disagree.

    Returns ``(found, error, version)``. Fail-soft, same discipline as
    :func:`probe_linger`: this needs SSH, which ``/health`` structurally
    cannot substitute for (the agent process answering a probe still has its
    own venv on PATH — proving nothing about a worker's shell) — so an SSH
    failure, or the pinned interpreter itself being unreachable/absent,
    reports ``(None, reason, None)``, UNKNOWN, never a fabricated CRIT.
    ``found=False`` — the actual defect this check exists to catch — is the
    one outcome that must never collapse into UNKNOWN.
    """
    import subprocess  # noqa: PLC0415

    try:
        result = subprocess.run(
            [
                "ssh", "-o", "BatchMode=yes",
                "-o", f"ConnectTimeout={max(1, int(timeout))}",
                host, "$HOME/.coord-venv/bin/python3 -",
            ],
            input=_COORD_ON_WORKER_PATH_PROBE_SCRIPT,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"ssh probe failed: {exc}", None
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()[-1:]
        return None, f"ssh probe failed: {detail[0] if detail else 'no output'}", None

    text = (result.stdout or "").strip()
    if "COORD_ON_WORKER_PATH_OK=1" in text:
        version = None
        for line in text.splitlines():
            if line.startswith("VERSION="):
                version = line[len("VERSION="):].strip() or None
        return True, None, version
    if "COORD_ON_WORKER_PATH_OK=0" in text:
        return False, None, None
    return None, "worker-PATH probe produced no parseable output", None


def gather_facts(
    cfg: "Config",
    machine_name: str,
    *,
    statuses: list | None = None,
    ts_map: dict | None = None,
    probe_ssh: bool = False,
    ssh_timeout: float = 20.0,
) -> MachineFacts:
    """Collect everything :func:`evaluate` needs for *machine_name*.

    *statuses* are :class:`coord.network.MachineStatus` objects a caller has
    already fetched (``coord doctor`` has them; folding this in must not cost
    a second round trip — the #2096 "two surfaces, one function" rule). When
    omitted, nothing is probed and every live finding reports UNKNOWN.

    *ts_map* is :func:`coord.network.tailscale_ip_map`'s result, resolved
    once by the caller so a fleet-wide sweep shells out to ``tailscale``
    exactly once. ``None`` means "not resolved yet" — this function resolves
    it itself; pass ``{}`` to mean "no tailnet data", which renders nothing
    rather than fabricating a mismatch.
    """
    from coord import network  # noqa: PLC0415
    from coord.prereqs import ToolProbe, unmet_capabilities  # noqa: PLC0415

    machine = next((m for m in cfg.machines if m.name == machine_name), None)
    known_repos = [r.name for r in (getattr(cfg, "repos", None) or [])]
    if machine is None:
        return MachineFacts(name=machine_name, configured=False, known_repos=known_repos)

    facts = MachineFacts(
        name=machine_name,
        configured=True,
        host=machine.host,
        declared_capabilities=list(machine.capabilities or []),
        declared_repos=list(machine.repos or []),
        repo_paths=dict(machine.repo_paths or {}),
        known_repos=known_repos,
    )

    if ts_map is None:
        ts_map = network.tailscale_ip_map()
    resolution = network.check_host_resolution(machine, ts_map or None)
    facts.host_matches_tailnet = resolution.matches
    facts.host_resolution_reason = resolution.reason
    facts.magicdns_fqdn = resolution.magicdns_fqdn

    status = next(
        (s for s in (statuses or []) if s.machine.name == machine_name), None
    )
    if status is not None:
        facts.reachable = status.is_online
        facts.unreachable_reason = None if status.is_online else status.reason
        health = status.health or {}
        if status.is_online:
            facts.config_free = health.get("config_free")
            caps = health.get("capabilities")
            facts.published_capabilities = list(caps) if isinstance(caps, list) else None
            repos = health.get("repos")
            facts.published_repos = list(repos) if isinstance(repos, list) else None
            degraded = health.get("degraded")
            facts.degraded = dict(degraded) if isinstance(degraded, dict) else {}
            facts.version = health.get("version")
            facts.health_checks = health_checks_from_health(health)

            # Same construction `coord doctor` uses (coord/commands/status.py)
            # — `what_breaks` is a static description that never crosses the
            # wire, so it is reconstructed as empty rather than guessed.
            probes = {
                tool: ToolProbe(
                    tool=tool,
                    capability=spec.get("capability"),
                    found=bool(spec.get("found", False)),
                    version=spec.get("version"),
                    min_version=spec.get("min_version"),
                    meets_floor=spec.get("meets_floor"),
                    what_breaks="",
                )
                for tool, spec in (health.get("tool_versions") or {}).items()
                if isinstance(spec, dict)
            }
            if probes:
                facts.unmet_capabilities = unmet_capabilities(
                    machine.capabilities or [], probes
                )

    for s in statuses or []:
        if s.is_online and (s.health or {}).get("version"):
            facts.fleet_versions[s.machine.name] = (s.health or {})["version"]

    if probe_ssh:
        facts.linger, facts.linger_error = probe_linger(
            machine.host, timeout=ssh_timeout
        )
        (
            facts.coord_on_worker_path,
            facts.coord_on_worker_path_error,
            facts.coord_on_worker_path_version,
        ) = probe_coord_on_worker_path(machine.host, timeout=ssh_timeout)
    return facts


# ── Layer 1: config ──────────────────────────────────────────────────────────


def evaluate_config(facts: MachineFacts) -> list[Finding]:
    """The structural defects that refuse dispatch with no agent involved.

    Deliberately narrow. ``coordinator.yml`` is validated at load
    (:func:`coord.config._parse_machines` rejects an unknown ``repos:`` entry
    and an unknown ``repo_paths:`` KEY outright), so by the time this runs
    those two can only ever be clean — the fleet-wide "config does not load
    at all" failure of #2915 item 4 is unreachable from here, and is instead
    prevented at WRITE time by ``coord machine add``. What survives the
    loader and still refuses every dispatch is #1801's pair: a declared repo
    with no ``repo_paths`` entry, and a machine that declares no capabilities.
    """
    out: list[Finding] = []
    if not facts.configured:
        return [
            Finding(
                layer="config",
                check="config.machine_missing",
                severity=CRIT,
                summary=(
                    f"no machines[] entry named {facts.name!r} in coordinator.yml"
                ),
                subject=facts.name,
                fix=(
                    f"coord machine add {facts.name} --host <tailnet-fqdn> "
                    "--capabilities ... --repos ..."
                ),
            )
        ]

    if not (facts.host or "").strip():
        out.append(
            Finding(
                layer="config", check="config.host_missing", severity=CRIT,
                summary="machines[] entry has an empty `host:`",
                subject=facts.name,
                fix="set `host:` to this node's MagicDNS FQDN.",
            )
        )
    else:
        out.append(
            Finding(
                layer="config", check="config.host_present", severity=OK,
                summary=f"host: {facts.host}", subject=facts.name,
            )
        )

    missing_paths = sorted(r for r in facts.declared_repos if r not in facts.repo_paths)
    if missing_paths:
        out.append(
            Finding(
                layer="config", check="config.repo_path_missing", severity=CRIT,
                summary=(
                    f"declared under `repos:` but absent from `repo_paths:`: "
                    f"{missing_paths} — coord.dispatch raises on exactly this, "
                    "so every dispatch of these repos to this machine is "
                    "refused (#1801)"
                ),
                subject=facts.name,
                fix=(
                    "add a `repo_paths:` entry per repo. The KEY is the "
                    "fleet's repo NAME (the `repos[].name` in coordinator.yml), "
                    "the VALUE is the on-disk path — they routinely differ, and "
                    "using the checkout's directory name as the key makes the "
                    "WHOLE config fail to load (#2915)."
                ),
            )
        )
    elif facts.declared_repos:
        out.append(
            Finding(
                layer="config", check="config.repo_paths_complete", severity=OK,
                summary=f"every declared repo has a repo_paths entry ({len(facts.repo_paths)})",
                subject=facts.name,
            )
        )

    # A repo_paths entry for a repo this machine does NOT declare is dead
    # config, not a defect — the loader accepts it, dispatch never reads it,
    # and it is usually a half-finished edit. Worth naming once; never a CRIT.
    orphan_paths = sorted(set(facts.repo_paths) - set(facts.declared_repos))
    if orphan_paths:
        out.append(
            Finding(
                layer="config", check="config.repo_path_orphan", severity=WARN,
                summary=(
                    f"repo_paths maps {orphan_paths} but `repos:` does not list "
                    "them — dead config; this machine is never routed those repos"
                ),
                subject=facts.name,
                fix=f"add {orphan_paths} to this machine's `repos:`, or drop the paths.",
            )
        )

    if not facts.declared_capabilities:
        out.append(
            Finding(
                layer="config", check="config.no_capabilities", severity=WARN,
                summary=(
                    "declares no `capabilities:` — ineligible for every "
                    "capability-gated dispatch (smoke_tests.capability_rules, "
                    "acceptance drivers with a `capability:`)"
                ),
                subject=facts.name,
                fix="add the capabilities this machine's toolchain actually backs.",
            )
        )
    if not facts.declared_repos:
        out.append(
            Finding(
                layer="config", check="config.no_repos", severity=CRIT,
                summary=(
                    "declares no `repos:` — nothing can ever be dispatched "
                    f"here. The fleet's repos are {sorted(facts.known_repos)}"
                ),
                subject=facts.name,
                fix=(
                    "add the repo NAMES from that list (not checkout directory "
                    "names) to this machine's `repos:` and `repo_paths:`."
                ),
            )
        )
    return out


# ── Layer 2: network ─────────────────────────────────────────────────────────


def evaluate_network(facts: MachineFacts) -> list[Finding]:
    """Does ``host:`` reach *this* machine, and does the agent answer there?

    The two halves are separate findings on purpose. #2915's incident 2 is a
    machine whose agent was perfectly healthy and whose board entry still read
    ``[timeout]``, because ``host:`` resolved to an unrelated LAN device that
    happened to share the name. Collapsing that into a single "unreachable"
    line is precisely the report that cost an afternoon.
    """
    out: list[Finding] = []

    if facts.host_matches_tailnet is False:
        out.append(
            Finding(
                layer="network", check="network.host_resolves_offtailnet", severity=CRIT,
                summary=(
                    f"{facts.host_resolution_reason or 'host: resolves off-tailnet'} "
                    "(#2912) — the agent can be perfectly healthy while the board "
                    "reads [timeout]"
                ),
                subject=facts.name,
                fix=(
                    f"set `host: {facts.magicdns_fqdn}` in coordinator.yml — the "
                    "MagicDNS FQDN, which a LAN DNS entry cannot shadow — instead "
                    "of the bare hostname."
                ),
            )
        )
    elif facts.host_matches_tailnet is True:
        out.append(
            Finding(
                layer="network", check="network.host_resolves_tailnet", severity=OK,
                summary=f"{facts.host} resolves to this node's tailnet address",
                subject=facts.name,
            )
        )
    else:
        out.append(
            Finding(
                layer="network", check="network.host_resolution_unknown", severity=UNKNOWN,
                summary=(
                    f"could not verify `host:` against the tailnet — "
                    f"{facts.host_resolution_reason or 'no local tailscale data'}"
                ),
                subject=facts.name,
            )
        )

    if facts.reachable is None:
        out.append(
            Finding(
                layer="network", check="network.agent_unprobed", severity=UNKNOWN,
                summary="agent not probed (no /health round trip was made)",
                subject=facts.name,
            )
        )
    elif not facts.reachable:
        out.append(
            Finding(
                layer="network", check="network.agent_unreachable", severity=CRIT,
                summary=(
                    f"no agent answered /health at {facts.host} — "
                    f"{facts.unreachable_reason or 'unreachable'}"
                ),
                subject=facts.name,
                fix=(
                    "on that machine: `systemctl --user status coord-agent`. A "
                    "partial ~/.coord-venv from a failed install-agent.sh run "
                    "poisons every retry — see runtime.agent_venv below and "
                    "docs/AGENT_OPERATIONS.md."
                ),
            )
        )
    else:
        out.append(
            Finding(
                layer="network", check="network.agent_reachable", severity=OK,
                summary=f"agent answered /health at {facts.host}",
                subject=facts.name,
            )
        )
    return out


# ── Layer 3: agent ───────────────────────────────────────────────────────────


def fleet_version_mode(versions: dict[str, str]) -> str | None:
    """The version the *rest* of the fleet is on, or ``None`` when there is no
    majority to compare against (fewer than two reporting machines, or a
    perfect tie). A tie is deliberately "no answer": grading a machine against
    an arbitrary tie-break would report a coin flip as a defect."""
    if len(versions) < 2:
        return None
    counts = Counter(versions.values()).most_common()
    if len(counts) > 1 and counts[0][1] == counts[1][1]:
        return None
    return counts[0][0]


def evaluate_agent(facts: MachineFacts) -> list[Finding]:
    """What the live agent says about itself, cross-checked against config.

    The subtle one is ``config_free``. A config-free agent (#1712) is a
    legitimate shape — the coordinator supplies capabilities and repos at
    dispatch time — so "publishes nothing" is not automatically a defect. But
    a machine with a *standing* entry in ``coordinator.yml`` that nonetheless
    came up config-free is #2915's incident 3: nothing said so, its capability
    probes were empty, and the #1570 D cross-check silently failed open
    (closed since #2913, which probes ``ALL_CAPABILITY_NAMES`` when
    config-free). Reported at WARN with the remedy, never silently.
    """
    if not facts.reachable:
        return []

    out: list[Finding] = []

    if facts.config_free:
        out.append(
            Finding(
                layer="agent", check="agent.config_free", severity=WARN,
                summary=(
                    f"agent is running CONFIG-FREE ({facts.config_free}) while "
                    "coordinator.yml declares a standing entry for it — its own "
                    "capability/repo publication is empty by design, so every "
                    "config-vs-/health cross-check reads as absence rather than "
                    "as truth"
                ),
                subject=facts.name,
                fix=(
                    "give this machine a ~/.coord/coordinator.yml (a symlink "
                    "into the coord-settings checkout) and restart coord-agent, "
                    "unless it is genuinely an ephemeral worker "
                    "(docs/EPHEMERAL_WORKERS.md)."
                ),
            )
        )
    else:
        if facts.declared_capabilities and facts.published_capabilities == []:
            out.append(
                Finding(
                    layer="agent", check="agent.capabilities_unpublished", severity=CRIT,
                    summary=(
                        f"config declares {sorted(facts.declared_capabilities)} but "
                        "the agent publishes NO capabilities — every "
                        "capability-gated dispatch skips this machine (#1712)"
                    ),
                    subject=facts.name,
                    fix=(
                        "the agent has a config but not THIS machine's entry — "
                        "check `coord agent --machine` matches `name:`, then "
                        "`git pull` the settings checkout on that host."
                    ),
                )
            )
        if facts.declared_repos and facts.published_repos == []:
            out.append(
                Finding(
                    layer="agent", check="agent.repos_unpublished", severity=CRIT,
                    summary=(
                        f"config declares repos {sorted(facts.declared_repos)} but "
                        "the agent publishes none — the review router and every "
                        "dispatch treat this machine as serving nothing (#1485)"
                    ),
                    subject=facts.name,
                    fix="same as above: the agent is not reading this machine's entry.",
                )
            )

    for cap, reasons in sorted(facts.unmet_capabilities.items()):
        out.append(
            Finding(
                layer="agent", check="agent.capability_unmet", severity=CRIT,
                summary=(
                    f"declares capability {cap!r} but this machine's own probe "
                    f"cannot back it up: {'; '.join(reasons)}"
                ),
                subject=cap,
                fix=(
                    f"install the tooling {cap!r} implies on that machine, or "
                    "drop the capability from coordinator.yml — a claimed-but-"
                    "unmet capability routes work that then fails."
                ),
            )
        )
    if facts.declared_capabilities and not facts.unmet_capabilities:
        out.append(
            Finding(
                layer="agent", check="agent.capabilities_probed", severity=OK,
                summary="declared capabilities back-checked against live probes",
                subject=facts.name,
            )
        )

    expected = fleet_version_mode(facts.fleet_versions)
    if not facts.version:
        out.append(
            Finding(
                layer="agent", check="agent.version_unknown", severity=UNKNOWN,
                summary="agent did not report a version (predates /health `version`)",
                subject=facts.name,
            )
        )
    elif expected is None:
        out.append(
            Finding(
                layer="agent", check="agent.version", severity=OK,
                summary=f"agent version {facts.version} (no fleet majority to compare against)",
                subject=facts.name,
            )
        )
    elif facts.version != expected:
        out.append(
            Finding(
                layer="agent", check="agent.version_skew", severity=WARN,
                summary=(
                    f"agent version {facts.version} but the rest of the fleet is "
                    f"on {expected}"
                ),
                subject=facts.name,
                fix=f"coord agent update --machine {facts.name}",
            )
        )
    else:
        out.append(
            Finding(
                layer="agent", check="agent.version", severity=OK,
                summary=f"agent version {facts.version} matches the fleet",
                subject=facts.name,
            )
        )
    return out


# ── Layer 4: clones ──────────────────────────────────────────────────────────


def evaluate_clones(facts: MachineFacts) -> list[Finding]:
    """Is each ``repo_paths`` clone actually on that machine's disk?

    Read from ``/health``'s ``degraded`` map (#1527) rather than guessed:
    ``_servable_repos()`` already filters out any repo whose ``repo_path``
    does not exist, and publishes *why*. A missing clone is a CRIT because
    the checkout is the worker WORKTREE BASE, not a convenience copy — every
    dispatch of that repo here 400s while ``coord status`` stays green.
    """
    if not facts.reachable:
        return []
    if facts.config_free:
        # A config-free agent publishes no repos by design; `degraded` is
        # empty for the same reason. Absence of evidence, not a clean bill.
        return [
            Finding(
                layer="clones", check="clones.unverifiable_config_free", severity=UNKNOWN,
                summary=(
                    "agent is config-free, so it publishes neither servable nor "
                    "degraded repos — clone presence cannot be read from /health"
                ),
                subject=facts.name,
            )
        ]

    out: list[Finding] = []
    for repo, reason in sorted(facts.degraded.items()):
        if repo not in facts.declared_repos:
            continue
        out.append(
            Finding(
                layer="clones", check="clones.missing", severity=CRIT,
                summary=f"{repo}: {reason}",
                subject=repo,
                fix=(
                    f"clone it to {facts.repo_paths.get(repo, '~/src/' + repo)} on "
                    f"{facts.name} — this is the worker WORKTREE BASE, so without "
                    "it every dispatch of this repo here is refused."
                ),
            )
        )

    published = facts.published_repos
    if published is None:
        out.append(
            Finding(
                layer="clones", check="clones.unpublished", severity=UNKNOWN,
                summary="agent did not publish a `repos` list (predates /health `repos`)",
                subject=facts.name,
            )
        )
        return out

    for repo in sorted(set(facts.declared_repos) - set(published) - set(facts.degraded)):
        out.append(
            Finding(
                layer="clones", check="clones.not_served", severity=CRIT,
                summary=(
                    f"{repo}: declared in coordinator.yml but the agent does not "
                    "serve it and gave no `degraded` reason — its config predates "
                    "the entry (#2219 agent/config skew)"
                ),
                subject=repo,
                fix=(
                    f"`git pull` the coord-settings checkout on {facts.name}. Since "
                    "#2299 the agent re-reads its own coordinator.yml on the next "
                    "/health poll — no restart needed (and none wanted: it kills "
                    "live workers)."
                ),
            )
        )
    served = sorted(set(facts.declared_repos) & set(published))
    if served:
        out.append(
            Finding(
                layer="clones", check="clones.served", severity=OK,
                summary=f"agent serves {served}",
                subject=facts.name,
            )
        )
    return out


# ── Layer 5: graph ───────────────────────────────────────────────────────────


def evaluate_graph(facts: MachineFacts) -> list[Finding]:
    """graphify's four silently-failing layers, as this machine reports them.

    ``graphify`` being absent is the loudest of the six #2915 incidents that
    nothing anywhere reports: every worker prompt in this fleet tells the
    agent to query the graph first, and on a machine without the CLI that
    instruction degrades to grep with no error, no warning, and no difference
    in any status readout.
    """
    if not facts.reachable:
        return []
    if not facts.health_checks:
        return [
            Finding(
                layer="graph", check="graph.unreported", severity=UNKNOWN,
                summary=(
                    "agent published no health-check results — its build predates "
                    "the #1630 check registry, so graph readiness is unknown here"
                ),
                subject=facts.name,
            )
        ]

    out: list[Finding] = []
    cli = facts.check(GRAPHIFY_CLI_CHECK)
    if cli is None:
        out.append(
            Finding(
                layer="graph", check="graph.graphify_cli_unreported", severity=UNKNOWN,
                summary="agent reported no `graphify_cli` result",
                subject=facts.name,
            )
        )
    elif cli.severity == OK:
        out.append(
            Finding(
                layer="graph", check="graph.graphify_cli", severity=OK,
                summary=f"graphify CLI present — {cli.headroom}",
                subject=facts.name,
            )
        )
    else:
        out.append(
            Finding(
                layer="graph", check="graph.graphify_missing", severity=CRIT,
                summary=(
                    f"graphify CLI is not installed here — {cli.headroom}. Every "
                    "graph query on this machine degrades to grep SILENTLY, and "
                    "no status readout says so"
                ),
                subject=facts.name,
                fix="install graphify on that machine (docs/GRAPHIFY_SETUP.md).",
            )
        )

    graph_rows = [r for r in facts.health_checks if r.check_id == GRAPH_CHECK]
    if not graph_rows:
        out.append(
            Finding(
                layer="graph", check="graph.no_checkouts_graphed", severity=WARN,
                summary=(
                    "agent reported graph readiness for zero checkouts — no "
                    "clone here has a graph built yet"
                ),
                subject=facts.name,
                fix="coord repo doctor <repo> --fix, or `graphify update .` in each clone.",
            )
        )
    for row in sorted(graph_rows, key=lambda r: r.subject or ""):
        if row.severity == OK:
            continue
        out.append(
            Finding(
                layer="graph",
                check="graph.checkout_degraded" if row.severity != UNKNOWN else "graph.checkout_unknown",
                severity=WARN if row.severity != UNKNOWN else UNKNOWN,
                summary=f"{row.subject}: {row.headroom}",
                subject=row.subject,
                fix=(
                    f"coord repo doctor {row.subject} --fix — builds the graph and "
                    "sets core.hooksPath on every machine that clones it."
                ),
            )
        )
    healthy = [r for r in graph_rows if r.severity == OK]
    if healthy:
        out.append(
            Finding(
                layer="graph", check="graph.checkouts_ok", severity=OK,
                summary=f"{len(healthy)} checkout(s) have a fresh graph",
                subject=facts.name,
            )
        )
    return out


# ── Layer 6: runtime ─────────────────────────────────────────────────────────


_VERSION_NUMBER_RE = re.compile(r"\d+(?:\.\d+)+")


def _extract_version_number(text: str | None) -> str | None:
    """Pull the dotted version number out of free-form text, e.g.
    ``"coord, version 0.5.290"`` -> ``"0.5.290"``.

    Used so version comparisons here are an EXACT match on the number, never
    a substring test (``"0.5.29" in "coord, version 0.5.290"`` is ``True``
    even though those are different releases — #2937 review). Returns
    ``None`` when no dotted number is found.
    """
    if not text:
        return None
    match = _VERSION_NUMBER_RE.search(text)
    return match.group(0) if match else None


def evaluate_runtime(facts: MachineFacts) -> list[Finding]:
    """The agent's own installation: its venv, and whether it survives logout.

    ``agent_venv`` is #2915's incident 1 — ``install-agent.sh`` left a partial
    venv behind and every retry inherited it. It is also the #402/#2569
    invariant: ``~/.coord-venv`` must be a plain, NON-editable PyPI install,
    because an editable one pins the live fleet's runtime to whatever branch a
    worktree happens to have checked out.

    ``linger`` and ``coord_on_worker_path`` are the two things ``/health``
    structurally cannot see: the agent answering a probe is proof only that
    the AGENT's own user manager / PATH is fine *right now*, not that a
    WORKER spawned on this host (with ``~/.coord-venv/bin`` stripped from
    PATH, #402/#2569) can survive logout or resolve ``coord`` at all. Both
    are UNKNOWN unless the caller opted into the SSH probe.
    """
    out: list[Finding] = []

    if facts.reachable:
        venv = facts.check(AGENT_VENV_CHECK)
        if venv is None:
            out.append(
                Finding(
                    layer="runtime", check="runtime.agent_venv_unreported", severity=UNKNOWN,
                    summary="agent reported no `agent_venv` result",
                    subject=facts.name,
                )
            )
        elif venv.severity == OK:
            out.append(
                Finding(
                    layer="runtime", check="runtime.agent_venv", severity=OK,
                    summary=f"~/.coord-venv: {venv.headroom}",
                    subject=facts.name,
                )
            )
        else:
            out.append(
                Finding(
                    layer="runtime",
                    check="runtime.agent_venv"
                    if venv.severity == UNKNOWN
                    else "runtime.agent_venv_broken",
                    severity=UNKNOWN if venv.severity == UNKNOWN else CRIT,
                    summary=f"~/.coord-venv: {venv.headroom}{f' — {venv.detail}' if venv.detail else ''}",
                    subject=facts.name,
                    fix=(
                        "re-run install-agent.sh on that machine. A PARTIAL venv "
                        "from a failed run poisons every retry (#2915), and an "
                        "EDITABLE install pins the live fleet to a worktree's "
                        "branch (#402/#2569) — delete ~/.coord-venv first."
                    ),
                )
            )

    if facts.coord_on_worker_path is False:
        out.append(
            Finding(
                layer="runtime", check="runtime.coord_on_worker_path_missing", severity=CRIT,
                summary=(
                    "`coord` does not resolve on a WORKER-shaped PATH (with "
                    "~/.coord-venv/bin stripped, as #402/#2569 spawn workers) — "
                    "this machine can dispatch and run a worker, but that worker "
                    "cannot run `coord test` to record a verdict. This is exactly "
                    "the #2937 gap: every other layer here, and `/health` itself, "
                    "only ever sees the AGENT's own PATH"
                ),
                subject=facts.name,
                fix=(
                    "install `coord` somewhere on the agent user's PATH outside "
                    "~/.coord-venv (e.g. `pipx install code-coordinator`, or a "
                    "system-wide non-editable install) — the fix must survive "
                    "~/.coord-venv/bin being removed from PATH, because that is "
                    "exactly what a worker's shell does."
                ),
            )
        )
    elif facts.coord_on_worker_path is True:
        expected_raw = (facts.version or "").strip()
        reported_raw = facts.coord_on_worker_path_version or ""
        expected = _extract_version_number(expected_raw)
        reported = _extract_version_number(reported_raw)
        # #2937 review: an EXACT comparison of the extracted version numbers,
        # not `expected in reported` — a substring test reads a genuine skew
        # as a match whenever the shorter string happens to be a substring of
        # the longer one (this project's own scheme: "0.5.29" in "coord,
        # version 0.5.290" is True even though those are different releases).
        if expected and expected != (reported or ""):
            out.append(
                Finding(
                    layer="runtime", check="runtime.coord_on_worker_path_version_mismatch",
                    severity=WARN,
                    summary=(
                        f"`coord` on the worker PATH reports {reported_raw!r}, but the "
                        f"agent's own /health reports version {expected_raw!r} — a "
                        "worker may record verdicts against a stale or unrelated "
                        "install"
                    ),
                    subject=facts.name,
                    fix="reinstall/upgrade the worker-PATH `coord` to match the agent's version.",
                )
            )
        else:
            out.append(
                Finding(
                    layer="runtime", check="runtime.coord_on_worker_path", severity=OK,
                    summary=f"`coord` resolves on a worker-shaped PATH ({reported_raw or 'version unreported'})",
                    subject=facts.name,
                )
            )
    else:
        out.append(
            Finding(
                layer="runtime", check="runtime.coord_on_worker_path_unknown", severity=UNKNOWN,
                summary=(
                    "worker-PATH `coord` resolution not checked — /health cannot "
                    f"see it (it only proves the AGENT can find coord); "
                    f"{facts.coord_on_worker_path_error or 'pass --ssh to probe it'}"
                ),
                subject=facts.name,
            )
        )

    if facts.linger is True:
        out.append(
            Finding(
                layer="runtime", check="runtime.linger", severity=OK,
                summary="systemd linger enabled — --user units survive logout/reboot",
                subject=facts.name,
            )
        )
    elif facts.linger is False:
        out.append(
            Finding(
                layer="runtime", check="runtime.linger_disabled", severity=CRIT,
                summary=(
                    "systemd linger is DISABLED — coord-agent dies at the next "
                    "logout or reboot and never comes back, with nothing to "
                    "distinguish it from a machine that was simply turned off"
                ),
                subject=facts.name,
                fix=f"ssh {facts.host} 'loginctl enable-linger \"$USER\"'",
            )
        )
    else:
        out.append(
            Finding(
                layer="runtime", check="runtime.linger_unknown", severity=UNKNOWN,
                summary=(
                    "systemd linger not checked — /health cannot see it; "
                    f"{facts.linger_error or 'pass --ssh to probe it'}"
                ),
                subject=facts.name,
            )
        )
    return out


def evaluate(facts: MachineFacts) -> MachineDoctorReport:
    """Run every layer over *facts*. Pure — no I/O, no network."""
    findings: list[Finding] = list(evaluate_config(facts))
    if facts.configured:
        findings.extend(evaluate_network(facts))
        findings.extend(evaluate_agent(facts))
        findings.extend(evaluate_clones(facts))
        findings.extend(evaluate_graph(facts))
        findings.extend(evaluate_runtime(facts))
    return MachineDoctorReport(machine_name=facts.name, findings=findings)


# ── Rendering ────────────────────────────────────────────────────────────────


_LAYER_TITLES = {
    "config": "1 — config (coordinator.yml in the coord-settings checkout)",
    "network": "2 — network (does `host:` reach THIS machine?)",
    "agent": "3 — agent (live /health, not config)",
    "clones": "4 — repo clones (the worker worktree bases)",
    "graph": "5 — graph",
    "runtime": "6 — runtime (agent install + survives logout)",
}


def format_report(report: MachineDoctorReport, *, verbose: bool = False) -> list[str]:
    """Human-readable lines for ``coord machine doctor``.

    Without ``verbose`` the OK findings collapse to a per-layer count — the
    point of the command is the *residue*, and a wall of green hides it.
    """
    lines: list[str] = [f"machine: {report.machine_name}"]
    for layer in LAYERS:
        entries = report.for_layer(layer)
        if not entries:
            continue
        lines.append("")
        lines.append(f"{_LAYER_TITLES.get(layer, layer)}:")
        shown = [f for f in entries if verbose or f.severity != OK]
        oks = len([f for f in entries if f.severity == OK])
        for f in sorted(shown, key=lambda f: _SEVERITY_RANK.get(f.severity, 9)):
            lines.append(f"  {_SEVERITY_MARK.get(f.severity, '·')} [{f.check}] {f.summary}")
            if f.fix and f.severity in (CRIT, WARN):
                lines.append(f"        fix: {f.fix}")
        if oks and not verbose:
            lines.append(f"  ✓ {oks} check(s) passed")
    lines.append("")
    lines.append(summary_line(report))
    return lines


def summary_line(report: MachineDoctorReport) -> str:
    """The machine-readable trailer, mirroring ``REPO_DOCTOR:`` /
    ``CONFIG_PROVENANCE:`` / ``GRAPH_HEALTH:``."""
    unknown = len([f for f in report.findings if f.severity == UNKNOWN])
    return (
        f"MACHINE_DOCTOR: machine={report.machine_name} "
        f"crit={len(report.crits)} warn={len(report.warns)} unknown={unknown} "
        f"ok={'true' if report.ok else 'false'}"
    )


#: The layers ``coord doctor`` folds in.
#:
#: Two filters, both deliberate, and both the same argument
#: :data:`coord.repo_onboard.DOCTOR_LIVE_LAYERS` makes.
#:
#: **Live layers only.** ``config`` is derivable from a config read that costs
#: nothing and needs no fleet, so duplicating it into the fleet report buys no
#: new information while burying the findings that do.
#:
#: **Only the live layers ``coord doctor`` does not already render itself.**
#: It prints the #2912 host-resolution line, its own ``unreachable`` line, the
#: #1712 capabilities/repos cross-check and the #1570 D per-capability probe —
#: i.e. all of ``network`` and all of ``agent``. ``clones`` is EXCLUDED here
#: for the same reason: the #1712 cross-check
#: (``_health_vs_config_lines`` in ``coord/commands/status.py``) already
#: computes "declared repo, not published" from the exact same ``degraded``/
#: ``published_repos`` shape ``evaluate_clones`` reads, for both the
#: total-loss case (a repo entirely unserved) and the #2219 partial-drift
#: case — it just prints the verdict under a different name
#: (``CRIT repos: ...`` instead of ``clones.missing``/``clones.not_served``).
#: Folding ``clones`` back in here printed the SAME defect twice under two
#: names, which is how a report stops being read (found in review of
#: #2915). What ``coord doctor`` renders *nowhere* is layers 5-6 — graphify
#: and the agent's venv — and those are two of the six silent failures
#: #2915 was opened for; clone presence is the third, and it is covered by
#: the pre-existing #1712 check instead. ``coord machine doctor`` on its own
#: still renders the full ``clones`` layer (:func:`format_report` walks
#: :data:`LAYERS`, not this tuple) — this only trims what gets folded into
#: the fleet-wide ``coord doctor`` report.
DOCTOR_LIVE_LAYERS: tuple[str, ...] = ("graph", "runtime")


def doctor_summary_lines(
    report: MachineDoctorReport, *, layers: tuple[str, ...] = DOCTOR_LIVE_LAYERS
) -> list[tuple[bool, str]]:
    """Compact ``(is_problem, line)`` pairs for folding into ``coord doctor``.

    CRIT only, and only the layers in *layers* — same reasoning as
    :func:`coord.repo_onboard.doctor_summary_lines`. Warnings (a stale graph, a
    version skew, an orphan ``repo_paths`` entry) are real residue but are
    states a fleet runs in indefinitely and deliberately, and a report that is
    always red is a report nobody reads.
    """
    out: list[tuple[bool, str]] = []
    for f in report.findings:
        if f.severity != CRIT or f.layer not in layers:
            continue
        mark = _SEVERITY_MARK[f.severity]
        out.append((True, f"  {mark} onboarding: [{f.check}] {f.summary}"))
    return out
