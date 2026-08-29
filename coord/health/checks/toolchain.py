"""Toolchain versions: per-machine facts, and fleet-scope skew (#1629, H-2).

**The finding.** Measured 2026-07-30 diagnosing vimcode#615: CI
(``dtolnay/rust-toolchain@stable``) built with rustc 1.97.1 while the three
fleet machines that can run vimcode's tests were on 1.95.0, 1.95.0, and
1.93.1 — five months stale. Six ``insta`` render snapshots passed on every
fleet machine and failed in CI. The recorded Test verdict was ``passed``;
the truth was "the code does not build on a current toolchain," and the gate
had been silently green for months because nothing measured *which*
toolchain produced a `passed`.

**Annotate, don't gate — locked 2026-07-31 (operator).** This module does
two independent things, and neither one blocks a dispatch, a routing
decision, or a merge:

1. :func:`probe_toolchain_versions` (machine scope) — what's actually
   installed on THIS box for the toolchains its local checkouts need.  Pure
   fact-gathering, like ``agent_venv``: always OK when detected, UNKNOWN
   when it can't be.
2. :func:`probe_toolchain_skew` (fleet scope) — reads every machine's #1
   results (fed through the daemon's health-poll tick, same "gather in the
   context, judge in the probe" split as ``fleet_deploy_lanes``) plus the CI
   pin the daemon-host facts read out of the repo's own workflow YAML, and
   turns disagreement into a severity: all equal = OK, any machine differs
   from another = WARN, a machine differs from CI = CRIT.

Gating on this is explicitly out of scope (a follow-up issue, once the data
says something) — see ``tests/test_fleet_health_probes.py``'s
``test_toolchain_skew_crit_never_touches_dispatch_routing_or_merge`` and the
structural guarantee in ``tests/test_fleet_health_snapshot.py``.

**Table-driven, not ``rustc --version`` hardcoded (scope item 3).** A new
interpreter/SDK a repo's tests depend on is a new :data:`TOOLCHAIN_SPECS`
entry — the detection, the CI-pin parser, and the skew comparison all key
off the table, not off a toolchain-specific code path.

Everything here is fail-soft toward UNKNOWN: a version this module could not
determine is "we don't know," never fabricated as "matches" or "differs."
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from coord.health.models import CheckResult, HealthContext, Severity
from coord.health.registry import check

VERSIONS_CHECK_ID = "toolchain_versions"
SKEW_CHECK_ID = "fleet_toolchain_skew"


@dataclass(frozen=True)
class ToolchainSpec:
    """One row of the table #1629 scope item 3 asks for."""

    kind: str
    # argv to run locally to get a version string, e.g. ("rustc", "--version").
    version_argv: tuple[str, ...]
    # Regex whose first group is the dotted version number.
    version_regex: str
    # A checkout needs this kind if any of these subdirectories exist...
    dir_markers: tuple[str, ...] = ()
    # ...or the checkout ROOT has any of these marker files (a repo that IS
    # that language, e.g. vimcode/quadraui/coord-tui's root Cargo.toml, vs. a
    # repo with a nested subdirectory in another language).
    root_markers: tuple[str, ...] = ()
    # GitHub Actions step(s) (the `uses:` name, before the `@`) that install
    # this toolchain in CI.
    ci_actions: tuple[str, ...] = ()
    # The `with:` key on that action carrying a literal version pin, e.g.
    # "python-version" / "node-version" / "toolchain".
    ci_version_key: str = ""
    # `with:`/`@ref` values that mean "CI floats, no literal pin to read" —
    # dtolnay/rust-toolchain@stable being the textbook case from #1629 itself.
    ci_floating_values: tuple[str, ...] = ("stable", "beta", "nightly")
    # True only for actions where the `@ref` itself IS the toolchain version
    # (``dtolnay/rust-toolchain@1.97.1``) — the ref-as-version fallback in
    # ``_parse_ci_pin`` is gated on this. Actions like ``actions/setup-python``
    # / ``actions/setup-node`` version their OWN release tag in `@ref`
    # (``actions/setup-node@v4``), which has nothing to do with the
    # interpreter version installed — falling back to it fabricates a pin
    # that was never configured. Default False; only the rust spec sets it.
    ci_ref_is_version: bool = False


# Scope item 1's starting table: "rustc/cargo for tui/** and the Rust repos,
# python for the coord suite, node for coord/dashboard/webapp/**".
TOOLCHAIN_SPECS: tuple[ToolchainSpec, ...] = (
    ToolchainSpec(
        kind="rustc",
        version_argv=("rustc", "--version"),
        version_regex=r"rustc (\d+(?:\.\d+){1,2})",
        # #2899 moved coord-tui's crate out of code-coordinator, so its
        # checkout now matches on the root `Cargo.toml` below like every
        # other Rust repo. `dir_markers=("tui",)` is KEPT as a pre-split
        # marker: a machine can legitimately still have a `claude-coordinator`
        # checkout parked on a pre-#2899 commit, and that tree genuinely does
        # need rustc. Same reasoning as `resolve_webapp_source_dir`'s
        # pre-#2009 fallback — a marker that stops matching turns the lane
        # off, and an off lane is indistinguishable from a healthy one.
        dir_markers=("tui",),
        root_markers=("Cargo.toml",),
        ci_actions=("dtolnay/rust-toolchain", "actions-rs/toolchain"),
        ci_version_key="toolchain",
        ci_ref_is_version=True,
    ),
    ToolchainSpec(
        kind="python",
        version_argv=("python3", "--version"),
        version_regex=r"Python (\d+(?:\.\d+){1,2})",
        dir_markers=("coord",),
        root_markers=("pyproject.toml", "setup.py"),
        ci_actions=("actions/setup-python",),
        ci_version_key="python-version",
        ci_floating_values=(),
    ),
    ToolchainSpec(
        kind="node",
        version_argv=("node", "--version"),
        version_regex=r"v?(\d+(?:\.\d+){0,2})",
        dir_markers=("coord/dashboard/webapp",),
        root_markers=("package.json",),
        ci_actions=("actions/setup-node",),
        ci_version_key="node-version",
        ci_floating_values=(),
    ),
)


# ── applicability: does a checkout's test suite depend on this kind? ────────


def _spec_applies(checkout_path: Path, spec: ToolchainSpec) -> bool:
    for marker in spec.dir_markers:
        if (checkout_path / marker).is_dir():
            return True
    for marker in spec.root_markers:
        if (checkout_path / marker).is_file():
            return True
    return False


def repo_toolchain_kinds(checkout_path: Path) -> list[str]:
    """Which :data:`TOOLCHAIN_SPECS` kinds *checkout_path*'s tests plausibly
    depend on — table-driven, per #1629 scope item 3."""
    return [spec.kind for spec in TOOLCHAIN_SPECS if _spec_applies(checkout_path, spec)]


# ── local version detection (machine-scope fact gathering) ──────────────────


def _parse_version(raw: str, regex: str) -> str | None:
    m = re.search(regex, raw)
    return m.group(1) if m else None


def detect_local_version(spec: ToolchainSpec, *, timeout: float = 5.0) -> str | None:
    """Run ``<spec.version_argv>`` on THIS machine and extract a version.

    None — never raised, never fabricated — for a missing binary, a
    non-zero exit, unparseable output, or a timeout.
    """
    try:
        result = subprocess.run(
            list(spec.version_argv),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return _parse_version(result.stdout + result.stderr, spec.version_regex)


def local_toolchain_label(checkout_path: Path) -> str | None:
    """``"rustc 1.95.0"`` / ``"python 3.12.4, node 20.11.0"`` for whatever
    *checkout_path*'s tests depend on — used to annotate a Test verdict
    recorded on THIS machine (#1629 scope item 2). ``None`` when no known
    toolchain applies, or none could be detected; a caller records the
    verdict either way, just without this optional annotation.
    """
    parts: list[str] = []
    for spec in TOOLCHAIN_SPECS:
        if not _spec_applies(checkout_path, spec):
            continue
        version = detect_local_version(spec)
        if version:
            parts.append(f"{spec.kind} {version}")
    return ", ".join(parts) or None


def toolchain_label_for_machine(
    health_block: dict | None, kinds: "list[str] | None" = None
) -> str | None:
    """Compact label built from one machine's last-known H-1 report block
    (``{"results": [...]}``, the same shape ``fleet_snapshot`` stores per
    machine) — restricted to *kinds* when given. Used by a caller recording
    a verdict for a REMOTE machine's test run, reusing the H-1 tick's
    already-gathered facts instead of shelling out again.
    """
    if not health_block:
        return None
    results = health_block.get("results") or []
    parts: list[str] = []
    for r in results:
        if not isinstance(r, dict) or r.get("check_id") != VERSIONS_CHECK_ID:
            continue
        subject = r.get("subject")
        if kinds is not None and subject not in kinds:
            continue
        version = (r.get("values") or {}).get("version")
        if subject and version:
            parts.append(f"{subject} {version}")
    return ", ".join(parts) or None


@check(
    id=VERSIONS_CHECK_ID,
    scope="machine",
    title="toolchain",
    order=45,
    description=(
        "Installed versions of the toolchains this machine's local "
        "checkouts need (rustc/python/node...). Pure fact-gathering — feeds "
        "the fleet_toolchain_skew judgement, says nothing about skew itself."
    ),
)
def probe_toolchain_versions(ctx: HealthContext) -> list[CheckResult] | None:
    needed: dict[str, ToolchainSpec] = {}
    for checkout in ctx.checkouts:
        for spec in TOOLCHAIN_SPECS:
            if spec.kind not in needed and _spec_applies(checkout.path, spec):
                needed[spec.kind] = spec
    if not needed:
        return None  # nothing this machine's checkouts need — silence beats a green line

    results: list[CheckResult] = []
    for kind, spec in sorted(needed.items()):
        version = detect_local_version(spec)
        if version is None:
            results.append(
                CheckResult(
                    check_id=VERSIONS_CHECK_ID,
                    scope="machine",
                    severity=Severity.UNKNOWN,
                    subject=kind,
                    headroom=f"{' '.join(spec.version_argv)} could not be run",
                    detail=(
                        "a local checkout needs this toolchain but it could "
                        "not be detected on this machine"
                    ),
                    values={"argv": list(spec.version_argv)},
                )
            )
        else:
            results.append(
                CheckResult(
                    check_id=VERSIONS_CHECK_ID,
                    scope="machine",
                    severity=Severity.OK,
                    subject=kind,
                    headroom=version,
                    values={"version": version},
                )
            )
    return results


# ── CI facts: parse pinned versions out of workflow YAML (daemon-host-local,
#    no network — see coord.health.fleet_snapshot._daemon_host_facts) ───────


def _iter_workflow_steps(checkout_path: Path):
    workflows_dir = checkout_path / ".github" / "workflows"
    if not workflows_dir.is_dir():
        return
    try:
        files = sorted(workflows_dir.glob("*.yml")) + sorted(workflows_dir.glob("*.yaml"))
    except OSError:
        return
    for wf_file in files:
        try:
            doc = yaml.safe_load(wf_file.read_text(encoding="utf-8", errors="replace"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(doc, dict):
            continue
        jobs = doc.get("jobs")
        if not isinstance(jobs, dict):
            continue
        for job in jobs.values():
            if not isinstance(job, dict):
                continue
            for step in job.get("steps") or []:
                if isinstance(step, dict):
                    yield step


def _version_tuple(s: str) -> tuple[int, ...] | None:
    parts = re.findall(r"\d+", s or "")
    return tuple(int(p) for p in parts) if parts else None


def ci_matches_machine(ci_version: str, machine_version: str) -> bool:
    """True when *machine_version* satisfies a (possibly less precise)
    *ci_version* pin.

    CI configs commonly name a MINOR version (``python-version: '3.12'``)
    while a local ``--version`` reports the full patch triplet
    (``"3.12.4"``) — that's still "running what CI runs", so the comparison
    is a component-wise PREFIX match at CI's own precision, not string
    equality. A toolchain CI pins to full precision (rustc's 3-part
    ``1.97.1``) still requires every one of those components to match.
    """
    ci_t = _version_tuple(ci_version)
    m_t = _version_tuple(machine_version)
    if not ci_t or not m_t or len(m_t) < len(ci_t):
        return False
    return m_t[: len(ci_t)] == ci_t


def _parse_ci_pin(spec: ToolchainSpec, step: dict) -> str | None:
    uses = str(step.get("uses") or "")
    if "@" not in uses:
        return None
    action_name, action_ref = uses.split("@", 1)
    if action_name not in spec.ci_actions:
        return None
    with_block = step.get("with")
    pinned = with_block.get(spec.ci_version_key) if isinstance(with_block, dict) else None
    if pinned is not None:
        pinned_s = str(pinned).strip().strip("'\"")
        if pinned_s and pinned_s not in spec.ci_floating_values and _version_tuple(pinned_s):
            return pinned_s
        return None  # explicit but floating ("stable") or unparseable — honestly unknown
    if not spec.ci_ref_is_version:
        # No `with:` pin, and this action's `@ref` is the ACTION's own release
        # tag (e.g. actions/setup-node@v4), not the toolchain version — that's
        # the common "default runner version" / ".nvmrc-driven" CI pattern,
        # honestly unknown, never fabricated from the action's tag (#1629 fix).
        return None
    action_ref = action_ref.strip()
    if action_ref and action_ref not in spec.ci_floating_values and _version_tuple(action_ref):
        return action_ref
    return None  # no `with:` pin and the @ref is a floating channel (or a SHA) — unknown


def ci_toolchain_versions(checkout_path: Path) -> dict[str, str | None]:
    """kind -> the version CI pins, or None when CI floats / doesn't
    configure that toolchain at all.

    Local-filesystem-only (workflow YAML already on disk) — no ``gh`` call,
    cheap enough for the daemon's health-poll tick. A floating pin
    (``dtolnay/rust-toolchain@stable``, #1629's own motivating example) is
    honestly ``None``: the version it actually resolves to on a given day
    can only be read from a live CI run, which this function does not fetch.
    """
    out: dict[str, str | None] = {spec.kind: None for spec in TOOLCHAIN_SPECS}
    for step in _iter_workflow_steps(checkout_path):
        for spec in TOOLCHAIN_SPECS:
            if out[spec.kind] is not None:
                continue
            pin = _parse_ci_pin(spec, step)
            if pin:
                out[spec.kind] = pin
    return out


# ── fleet-scope skew judgement ───────────────────────────────────────────────


def _machine_toolchain_versions(ctx: HealthContext) -> dict[str, dict[str, str]]:
    """machine_name -> {kind: version}, read from each machine's last-known
    H-1 report (the same ``checks.results`` block ``fleet_deploy_lanes``
    reads for ``agent_venv``)."""
    out: dict[str, dict[str, str]] = {}
    if ctx.fleet is None:
        return out
    for name, entry in ctx.fleet.machines.items():
        checks = (entry or {}).get("checks") or {}
        versions: dict[str, str] = {}
        for r in checks.get("results", []) or []:
            if not isinstance(r, dict) or r.get("check_id") != VERSIONS_CHECK_ID:
                continue
            if r.get("error"):
                continue
            subject = r.get("subject")
            version = (r.get("values") or {}).get("version")
            if subject and version:
                versions[subject] = version
        out[name] = versions
    return out


def _by_version(versions: dict[str, str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for machine, v in versions.items():
        out.setdefault(v, []).append(machine)
    return out


def _format_by_version(by_version: dict[str, list[str]]) -> str:
    return "; ".join(f"{v}: {', '.join(sorted(ms))}" for v, ms in sorted(by_version.items()))


@check(
    id=SKEW_CHECK_ID,
    scope="fleet",
    title="toolchain skew",
    order=15,
    description=(
        "Do all machines that can run a repo's tests agree on its "
        "toolchain, and does that agree with CI? #1629: annotate-only — "
        "this NEVER gates dispatch, routing, or merge."
    ),
)
def probe_toolchain_skew(ctx: HealthContext) -> list[CheckResult] | CheckResult | None:
    if ctx.fleet is None:
        return CheckResult(
            check_id=SKEW_CHECK_ID,
            scope="fleet",
            severity=Severity.UNKNOWN,
            headroom="no fleet snapshot (fleet checks only run on the daemon)",
        )

    dh = ctx.fleet.daemon_host or {}
    repo_kinds: dict[str, list[str]] = dh.get("repo_toolchain_kinds") or {}
    ci_versions: dict[str, dict[str, str | None]] = dh.get("ci_toolchains") or {}
    if not repo_kinds:
        return CheckResult(
            check_id=SKEW_CHECK_ID,
            scope="fleet",
            severity=Severity.UNKNOWN,
            headroom="no repo has a resolvable toolchain from the daemon host's local checkouts",
        )

    machine_versions = _machine_toolchain_versions(ctx)
    config = ctx.config
    machines_by_repo: dict[str, list[str]] = {}
    for m in getattr(config, "machines", ()) or ():
        for repo_name in getattr(m, "repo_paths", None) or {}:
            machines_by_repo.setdefault(repo_name, []).append(m.name)

    results: list[CheckResult] = []
    for repo_name, kinds in sorted(repo_kinds.items()):
        candidate_machines = sorted(set(machines_by_repo.get(repo_name, ())))
        for kind in kinds:
            subject = f"{repo_name}:{kind}"

            if not candidate_machines:
                results.append(
                    CheckResult(
                        check_id=SKEW_CHECK_ID,
                        scope="fleet",
                        severity=Severity.UNKNOWN,
                        subject=subject,
                        headroom="no machine advertises a checkout of this repo",
                    )
                )
                continue

            versions_by_machine = {
                m: machine_versions.get(m, {}).get(kind) for m in candidate_machines
            }
            known = {m: v for m, v in versions_by_machine.items() if v}
            missing = sorted(m for m, v in versions_by_machine.items() if not v)
            ci_version = (ci_versions.get(repo_name) or {}).get(kind)

            if not known:
                results.append(
                    CheckResult(
                        check_id=SKEW_CHECK_ID,
                        scope="fleet",
                        severity=Severity.UNKNOWN,
                        subject=subject,
                        headroom=f"no {kind} version data yet",
                        detail=f"no data for: {', '.join(missing)}" if missing else "",
                    )
                )
                continue

            distinct = sorted(set(known.values()))
            ci_mismatches = (
                sorted(m for m, v in known.items() if not ci_matches_machine(ci_version, v))
                if ci_version
                else []
            )

            if ci_version and ci_mismatches:
                results.append(
                    CheckResult(
                        check_id=SKEW_CHECK_ID,
                        scope="fleet",
                        severity=Severity.CRIT,
                        subject=subject,
                        headroom=(
                            f"CI pins {kind} {ci_version}; "
                            f"{', '.join(ci_mismatches)} disagree"
                        ),
                        detail=_format_by_version(_by_version(known)),
                        threshold=f"crit when a machine's {kind} != CI's {ci_version}",
                        values={
                            "versions": known,
                            "ci_version": ci_version,
                            "missing": missing,
                        },
                    )
                )
            elif len(distinct) > 1:
                results.append(
                    CheckResult(
                        check_id=SKEW_CHECK_ID,
                        scope="fleet",
                        severity=Severity.WARN,
                        subject=subject,
                        headroom=f"{len(distinct)} {kind} versions across the fleet",
                        detail=_format_by_version(_by_version(known)),
                        threshold="warn when machines disagree",
                        values={
                            "versions": known,
                            "ci_version": ci_version,
                            "missing": missing,
                        },
                    )
                )
            else:
                (version,) = distinct
                headroom = f"all machines on {kind} {version}"
                if ci_version:
                    headroom += f" (matches CI's {ci_version})"
                if missing:
                    headroom += f" ({len(missing)} machine(s) with no data)"
                results.append(
                    CheckResult(
                        check_id=SKEW_CHECK_ID,
                        scope="fleet",
                        severity=Severity.OK if not missing else Severity.UNKNOWN,
                        subject=subject,
                        headroom=headroom,
                        detail=f"no data for: {', '.join(missing)}" if missing else "",
                        values={
                            "versions": known,
                            "ci_version": ci_version,
                            "missing": missing,
                        },
                    )
                )
    return results or None
