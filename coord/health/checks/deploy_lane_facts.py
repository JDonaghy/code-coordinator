"""Per-machine facts for two of the fleet deploy-lane checks (#1806).

Root cause of #1806: `fleet_deploy_lanes`'s ``~/.coord-cli-venv`` lane and
the whole of `fleet_tui_binary` used to be gathered by ``os.stat``-ing the
**daemon host's** filesystem
(``coord.health.fleet_snapshot.FleetHealthRefresher._daemon_host_facts``).
That is correct for a fact that can only exist on the daemon process itself
(``coord-serve``'s own venv) — it is wrong for these two, whose subject is a
path on the **operator's** machine, which is very often a different box
from the one running ``coord-serve``. When the two differ, the daemon was
reporting on its own, usually-absent, copy of paths that live somewhere
else entirely — and reporting the *opposite* of the truth (#1806's live
example: WARN "stale" while the operator's real binary was current;
UNKNOWN "no data" while the operator's real CLI venv was on the release
everyone believed was live).

These two **machine-scope** checks report the same two raw facts (CLI-venv
version; tui-binary mtime vs. newest ``coord-tui`` source mtime) from wherever
they actually run — every agent's own ``/health`` poll, the transport #1630
already built for exactly this "measure locally, judge centrally" split.
`coord.health.checks.fleet_deploy_lanes` aggregates these across every
``ctx.fleet.machines`` entry instead of trusting a single host's local
``os.stat`` to answer a fleet-wide question.

Absence is the *common* case here — most machines in a fleet have neither
an operator's CLI venv nor a local ``coord-tui`` checkout+build — so both probes
report that plainly as ``OK`` ("not present on this machine") rather than
``UNKNOWN``/``WARN``: a box that was never meant to have either lane must
not read as faulty just because it doesn't.

A third check, ``webapp_bundle``, joins these two for the same reason and
the same shape (#1834 lane 5, the phone-webapp bundle `coord web --dist`
serves): the machine actually running `coord web` is very often not the
daemon host either. It is deliberately **not** graded against the released
wheel's version the way ``agent_venv``/``spawned_coord`` are —
``deploy/coord-web-dist-build.timer`` publishes continuously off
``origin/main``'s SHA (#1543), decoupled on purpose from the
``~/.coord-venv`` release cadence, so "is it on the released version?" is
not a well-formed question for it. What *is* well-formed, and what this
checks — mirroring ``tui_binary`` exactly — is whether the live bundle is
older than the source tree it claims to have been built from: the
`coord-web` repo's own checkout (#2470; retargeted from this repo's now-gone
``coord/dashboard/webapp/`` once the webapp moved out under epic #2002 —
see :func:`resolve_webapp_source_dir`, which shares its checkout-discovery
convention with ``coord.health.checks.coord_web_ci_pin`` so the two checks
can never disagree about which local checkout *is* `coord-web`, with a
fallback to the pre-#2009 in-repo layout for a checkout still parked on a
pre-split commit).
"""

from __future__ import annotations

import subprocess

from coord.dist_name import CANDIDATE_NAMES
from coord.health.checks.agent_install import pip_show
from coord.health.checks.coord_web_ci_pin import resolve_coord_web_checkout
from coord.health.models import CheckResult, HealthContext, Severity
from coord.health.registry import check
from coord.health.units import expand, shorten_path

# Same documented default locations `coord.health.fleet_snapshot` used to
# stat on the daemon host — now stat'd wherever this check actually runs.
# `health.cli_venv_python` / `health.tui_binary_path` / `health.tui_source_dir`
# override these, same convention as `agent_venv_python`: None means "use the
# documented default location", NOT "disable the lane".
_DEFAULT_CLI_VENV_PYTHON = "~/.coord-cli-venv/bin/python3"
_DEFAULT_TUI_BINARY = "~/.local/bin/coord-tui"
# Mirrors `deploy/coord-web-dist-build.sh`'s `$LIVE_LINK` — the symlink
# `coord-web-dist-build.timer` atomically repoints at each new release.
_DEFAULT_WEBAPP_DIST = "~/coord-web-dist"
# Mirrors `deploy/coord-web-dist-build.sh`'s `$HEARTBEAT_FILE` (#2122) — its
# default `$RELEASES_DIR` sibling to `$BLOCKED_SHA_FILE`.
_DEFAULT_WEBAPP_BUILD_HEARTBEAT = "~/.coord-web-releases/.last-run-at"

# Cap on the coord-tui source walk, mirroring the old daemon-side walk: the tree
# is a few hundred .rs files, so anything past this is a misconfigured
# `tui_source_dir` pointed at something huge, and a machine's own /health
# tick must not spend its budget discovering that. A partial walk still
# yields a *lower bound* on the newest mtime — it can only under-report
# staleness, never fabricate it.
_MAX_TUI_SOURCE_FILES = 5000
# Same cap, same reasoning, for the webapp/ source walk below.
_MAX_WEBAPP_SOURCE_FILES = 5000
# Directories a webapp source walk must never descend into: dependency and
# build output trees that dwarf the actual source and would either blow the
# file cap on noise or (worse) let a stale `dist/` sitting inside a checkout
# masquerade as "source newer than itself".
_WEBAPP_SOURCE_SKIP_DIRS = {"node_modules", "dist", "build", "coverage"}


def resolve_cli_venv_python(ctx: HealthContext):
    """The interpreter for this machine's ``~/.coord-cli-venv``, if any."""
    configured = getattr(ctx.thresholds, "cli_venv_python", None)
    if configured:
        return expand(configured, ctx.home)
    return expand(_DEFAULT_CLI_VENV_PYTHON, ctx.home)


def resolve_tui_binary_path(ctx: HealthContext):
    """This machine's locally-built ``coord-tui`` binary, if any."""
    configured = getattr(ctx.thresholds, "tui_binary_path", None)
    if configured:
        return expand(configured, ctx.home)
    return expand(_DEFAULT_TUI_BINARY, ctx.home)


#: The repo name a `coord-tui` checkout is expected to carry, plus a
#: structural marker that identifies one even if it is checked out under some
#: other directory name. The marker is a file unique to THIS crate, not a bare
#: ``Cargo.toml``: every Rust checkout on the machine (``quadraui``,
#: ``vimcode``) has one of those, so matching on it would point this lane at
#: the wrong tree — the "manufacture staleness rather than report it" failure
#: :func:`resolve_tui_source_dir` warns about. It is also not shared with the
#: pre-#2899 in-repo layout, whose copy lives at ``tui/src/app/data.rs``, so
#: the marker can never make a `claude-coordinator` checkout answer here.
COORD_TUI_REPO_NAME = "coord-tui"
COORD_TUI_MARKER = "src/app/data.rs"


def _norm_repo_name(raw: str) -> str:
    """Mirror of ``coord_web_ci_pin._norm_dist`` — a checkout directory named
    ``coord_tui`` or ``Coord-TUI`` is still a coord-tui checkout."""
    return raw.strip().lower().replace("_", "-")


def resolve_coord_tui_checkout(ctx: HealthContext):
    """This machine's `coord-tui` checkout, if it has one.

    Same convention as :func:`coord.health.checks.coord_web_ci_pin.
    resolve_coord_web_checkout`: a configured ``health.coord_tui_checkout``
    wins outright, ``None`` means "discover it", never "disable the lane".
    """
    configured = getattr(ctx.thresholds, "coord_tui_checkout", None)
    if configured:
        return expand(configured, ctx.home)
    for checkout in ctx.checkouts:
        if _norm_repo_name(checkout.name) == COORD_TUI_REPO_NAME:
            return checkout.path
    # Fall back to the structural marker so a rename of the repo doesn't
    # silently turn this lane off — an off lane is indistinguishable from a
    # healthy one, which is the whole failure mode being guarded against.
    for checkout in ctx.checkouts:
        if (checkout.path / COORD_TUI_MARKER).is_file():
            return checkout.path
    return None


def resolve_tui_source_dir(ctx: HealthContext):
    """The coord-tui source tree in the first local checkout that has one.

    Prefers a `coord-tui` checkout (#2899), discovered via
    :func:`resolve_coord_tui_checkout`. That checkout's ``src/`` is the crate
    root, not the checkout root — same ``src/``-not-root reasoning as
    :func:`resolve_webapp_source_dir`: rooting the walk at the checkout would
    sweep ``target/`` (multi-GB) and ``.git/``, which
    :func:`_newest_rust_source_mtime`'s budget is not sized for.

    Falls back to ``<checkout>/tui/src`` — the pre-#2899 in-repo layout. Kept
    for the same reason the webapp lane kept its pre-#2009 fallback: a machine
    can legitimately still have an older ``claude-coordinator`` checkout parked
    on a pre-split commit, and this lane reporting UNKNOWN there would be a
    regression in signal for no gain.

    Derived from ``ctx.checkouts`` (the same locally-existing checkouts
    every other checkout-scope probe sees) rather than guessed relative to
    the binary path — a `target/release/...` install path says nothing
    reliable about where the sources live, and comparing against the
    *wrong* tree would manufacture staleness rather than report it.
    Configured `health.tui_source_dir` wins outright when set.
    """
    configured = getattr(ctx.thresholds, "tui_source_dir", None)
    if configured:
        return expand(configured, ctx.home)
    checkout = resolve_coord_tui_checkout(ctx)
    if checkout is not None:
        candidate = checkout / "src"
        if candidate.is_dir():
            return candidate
    for checkout_entry in ctx.checkouts:
        candidate = checkout_entry.path / "tui" / "src"
        if candidate.is_dir():
            return candidate
    return None


def resolve_webapp_dist_path(ctx: HealthContext):
    """This machine's live ``coord web --dist`` bundle, if any (#1834)."""
    configured = getattr(ctx.thresholds, "webapp_dist_path", None)
    if configured:
        return expand(configured, ctx.home)
    return expand(_DEFAULT_WEBAPP_DIST, ctx.home)


def resolve_webapp_build_heartbeat_path(ctx: HealthContext):
    """This machine's ``coord-web-dist-build.sh`` heartbeat file, if any
    (#2122). Written on EVERY invocation — including the up-to-date no-op
    the script deliberately no longer logs — so its content answers "did
    the timer actually fire recently?" independent of the journal."""
    configured = getattr(ctx.thresholds, "webapp_build_heartbeat_path", None)
    if configured:
        return expand(configured, ctx.home)
    return expand(_DEFAULT_WEBAPP_BUILD_HEARTBEAT, ctx.home)


def resolve_webapp_source_dir(ctx: HealthContext):
    """The webapp source tree in the first local checkout that has one.

    Prefers a `coord-web` checkout (#2470), discovered via
    :func:`coord.health.checks.coord_web_ci_pin.resolve_coord_web_checkout`
    — the same "checkout named ``coord-web``, else one carrying
    ``playwright.acceptance.config.ts`` at its root" discovery that check
    already uses — so `webapp_bundle` and `coord_web_ci_pin` can never
    disagree about which local checkout is `coord-web`. That checkout's
    ``src/`` is the webapp root (#2009, epic #2002).

    Falls back to ``<checkout>/coord/dashboard/webapp/src`` — the pre-#2009
    in-repo layout. Kept because a machine can legitimately still have an
    older ``claude-coordinator`` checkout parked on a pre-split commit, and
    this lane reporting UNKNOWN there would be a regression in signal for no
    gain. Configured ``health.webapp_source_dir`` still wins outright, same
    convention as every other path in this module.
    """
    configured = getattr(ctx.thresholds, "webapp_source_dir", None)
    if configured:
        return expand(configured, ctx.home)
    checkout = resolve_coord_web_checkout(ctx)
    if checkout is not None:
        candidate = checkout / "src"
        if candidate.is_dir():
            return candidate
    for checkout in ctx.checkouts:
        candidate = checkout.path / "coord" / "dashboard" / "webapp" / "src"
        if candidate.is_dir():
            return candidate
    return None


def _newest_webapp_source_mtime(source_dir):
    """Newest mtime under *source_dir*, skipping dependency/build dirs.

    Unlike :func:`_newest_rust_source_mtime` this is deliberately NOT
    extension-filtered: a webapp source tree mixes ``.ts``/``.tsx``/``.css``/
    ``.html`` meaningfully and filtering would only risk under-reporting
    staleness. ``node_modules``/``dist``/``build`` are the trees that
    actually need skipping (size, and — for ``dist``/``build`` sitting
    inside a checkout — self-reference), so those are excluded outright.
    """
    newest = 0.0
    seen = 0
    try:
        if not source_dir.is_dir():
            return None
        stack = [source_dir]
        while stack:
            current = stack.pop()
            try:
                entries = list(current.iterdir())
            except OSError:
                continue
            for entry in entries:
                name = entry.name
                if name.startswith(".") or name in _WEBAPP_SOURCE_SKIP_DIRS:
                    continue
                try:
                    if entry.is_dir():
                        stack.append(entry)
                        continue
                    newest = max(newest, entry.stat().st_mtime)
                except OSError:
                    continue
                seen += 1
                if seen >= _MAX_WEBAPP_SOURCE_FILES:
                    stack.clear()
                    break
    except OSError:
        return newest or None
    return newest or None


def _newest_rust_source_mtime(source_dir):
    """Newest ``*.rs`` mtime under *source_dir*, or None if there are none.

    Skips ``target``/hidden directories so a ``tui_source_dir`` pointed at a
    crate root (rather than its ``src/``) degrades to slow-but-correct
    instead of walking a multi-GB build tree.
    """
    newest = 0.0
    seen = 0
    try:
        if not source_dir.is_dir():
            return None
        stack = [source_dir]
        while stack:
            current = stack.pop()
            try:
                entries = list(current.iterdir())
            except OSError:
                continue
            for entry in entries:
                name = entry.name
                if name.startswith(".") or name == "target":
                    continue
                try:
                    if entry.is_dir():
                        stack.append(entry)
                        continue
                    if not name.endswith(".rs"):
                        continue
                    newest = max(newest, entry.stat().st_mtime)
                except OSError:
                    continue
                seen += 1
                if seen >= _MAX_TUI_SOURCE_FILES:
                    stack.clear()
                    break
    except OSError:
        return newest or None
    return newest or None


@check(
    id="cli_venv",
    scope="machine",
    title="cli venv",
    order=42,
    description=(
        "This machine's ~/.coord-cli-venv coordinator version, when "
        "it has one — the operator's CLI venv lane of fleet_deploy_lanes (#1806)."
    ),
)
def probe_cli_venv(ctx: HealthContext) -> CheckResult:
    python = resolve_cli_venv_python(ctx)
    if not python.exists():
        # The overwhelming common case: most machines are workers, not the
        # operator's own box, and never had this venv created. That is not
        # a fault — fleet_deploy_lanes reads "no machine reports this lane"
        # as UNKNOWN, not this machine's absence as one.
        return CheckResult(
            check_id="cli_venv",
            scope="machine",
            severity=Severity.OK,
            headroom="not present on this machine",
            values={"python": str(python), "present": False, "version": None},
        )

    try:
        fields = pip_show(python)
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult(
            check_id="cli_venv",
            scope="machine",
            severity=Severity.UNKNOWN,
            headroom=f"could not run pip show ({type(exc).__name__})",
            error=str(exc),
            values={"python": str(python), "present": True, "version": None},
        )

    version = fields.get("Version") or None
    if not version:
        tried = " or ".join(CANDIDATE_NAMES)
        return CheckResult(
            check_id="cli_venv",
            scope="machine",
            severity=Severity.UNKNOWN,
            headroom=f"neither {tried} installed in {shorten_path(str(python), str(ctx.home))}",
            error="pip show returned nothing",
            values={"python": str(python), "present": True, "version": None},
        )

    return CheckResult(
        check_id="cli_venv",
        scope="machine",
        severity=Severity.OK,
        headroom=f"pypi {version}",
        values={
            "python": str(python),
            "present": True,
            "version": version,
            "editable": bool(fields.get("Editable project location")),
        },
    )


@check(
    id="tui_binary",
    scope="machine",
    title="tui binary",
    order=43,
    description=(
        "This machine's locally-built coord-tui binary vs. the coord-tui source tree "
        "it was built from — feeds the fleet_tui_binary check (#1806)."
    ),
)
def probe_tui_binary(ctx: HealthContext) -> CheckResult:
    """Mtime-based, and #2898 is why it stays that way.

    The obvious "improvement" here is to report the binary's ``--version`` and
    let ``coord release verify`` grade it against the fleet's expected
    version. Don't. Since phase 3 of #2894 coord-tui releases from its own
    repo on its own ``v*`` tag line, so the expected version this fleet is
    propagating (the *coordinator's* channel) is not a number coord-tui's
    version is comparable to — a fleet on coord v0.5.x with coord-tui v0.2.y
    is correct, and grading them against each other would report every host
    as permanently behind. See ``coord/release_verify.py``'s ``coord-tui``
    block. "Was this binary rebuilt since its source changed?" is a question
    that stays answerable locally, which is exactly why it is the one asked.
    """
    binary_path = resolve_tui_binary_path(ctx)
    values: dict = {"path": str(binary_path)}

    try:
        binary_mtime = binary_path.stat().st_mtime if binary_path.exists() else None
    except OSError:
        binary_mtime = None

    if binary_mtime is None:
        # Most machines never built a local coord-tui binary — absent, not stale.
        return CheckResult(
            check_id="tui_binary",
            scope="machine",
            severity=Severity.OK,
            headroom="not present on this machine",
            values={**values, "present": False},
        )

    values["present"] = True
    values["binary_mtime"] = binary_mtime

    source_dir = resolve_tui_source_dir(ctx)
    if source_dir is None:
        return CheckResult(
            check_id="tui_binary",
            scope="machine",
            severity=Severity.OK,
            headroom="binary present (coord-tui source tree not found to compare)",
            values=values,
        )

    values["source_dir"] = str(source_dir)
    source_mtime = _newest_rust_source_mtime(source_dir)
    if source_mtime is None:
        return CheckResult(
            check_id="tui_binary",
            scope="machine",
            severity=Severity.OK,
            headroom="binary present (coord-tui source tree not found to compare)",
            values=values,
        )

    values["source_mtime"] = source_mtime
    if source_mtime > binary_mtime:
        stale_hours = (source_mtime - binary_mtime) / 3600.0
        return CheckResult(
            check_id="tui_binary",
            scope="machine",
            severity=Severity.WARN,
            headroom=f"binary is {stale_hours:.1f}h older than coord-tui source",
            detail="rebuild the coord-tui binary — source changed since the last local build",
            threshold="warn when source/ is newer than the built binary",
            values=values,
        )

    return CheckResult(
        check_id="tui_binary",
        scope="machine",
        severity=Severity.OK,
        headroom="up to date with coord-tui source",
        values=values,
    )


@check(
    id="webapp_bundle",
    scope="machine",
    title="webapp bundle",
    order=47,
    description=(
        "This machine's live `coord web --dist` bundle vs. the "
        "coord/dashboard/webapp/ source tree it was supposedly built from — "
        "feeds fleet_webapp_bundle (#1834 lane 5)."
    ),
)
def probe_webapp_bundle(ctx: HealthContext) -> CheckResult:
    """The webapp analogue of :func:`probe_tui_binary`: same shape, same
    fail-soft-to-absent convention, different subject.

    Not graded against ``OWN_VERSION`` or the released wheel — see this
    module's docstring — so this only ever reports staleness relative to the
    ``webapp/`` source tree, never a version.
    """
    dist_path = resolve_webapp_dist_path(ctx)
    values: dict = {"path": str(dist_path)}

    try:
        present = dist_path.exists()
    except OSError:
        present = False

    if not present:
        # Most machines never run `coord web --dist` — absent, not stale.
        return CheckResult(
            check_id="webapp_bundle",
            scope="machine",
            severity=Severity.OK,
            headroom="not present on this machine",
            values={**values, "present": False},
        )

    values["present"] = True
    try:
        resolved = dist_path.resolve()
        # `coord-web-dist-build.sh` names each release directory after the
        # SHA it built — the symlink target's basename doubles as "which
        # commit this machine is actually serving" whenever that convention
        # holds, and is simply absent (not fabricated) when it doesn't.
        values["sha"] = resolved.name
    except OSError:
        resolved = dist_path

    try:
        dist_mtime = resolved.stat().st_mtime
    except OSError:
        dist_mtime = None
    values["dist_mtime"] = dist_mtime

    source_dir = resolve_webapp_source_dir(ctx)
    if source_dir is None or dist_mtime is None:
        return CheckResult(
            check_id="webapp_bundle",
            scope="machine",
            severity=Severity.OK,
            headroom="bundle present (webapp/ source tree not found to compare)",
            values=values,
        )

    values["source_dir"] = str(source_dir)
    source_mtime = _newest_webapp_source_mtime(source_dir)
    if source_mtime is None:
        return CheckResult(
            check_id="webapp_bundle",
            scope="machine",
            severity=Severity.OK,
            headroom="bundle present (webapp/ source tree not found to compare)",
            values=values,
        )

    values["source_mtime"] = source_mtime
    if source_mtime > dist_mtime:
        stale_hours = (source_mtime - dist_mtime) / 3600.0
        return CheckResult(
            check_id="webapp_bundle",
            scope="machine",
            severity=Severity.WARN,
            headroom=f"bundle is {stale_hours:.1f}h older than webapp/ source",
            detail=(
                "coord-web-dist-build.timer has not published since this "
                "source changed — check `systemctl --user status "
                "coord-web-dist-build.timer` on this machine"
            ),
            threshold="warn when webapp/ source is newer than the live bundle",
            values=values,
        )

    return CheckResult(
        check_id="webapp_bundle",
        scope="machine",
        severity=Severity.OK,
        headroom="up to date with webapp/ source",
        values=values,
    )


def _parse_webapp_build_heartbeat(text: str):
    """Parses ``"<epoch> <status> [<sha>]"``, the line
    ``coord-web-dist-build.sh``'s ``heartbeat()`` helper writes. Returns
    ``(epoch, status, sha)``, or ``None`` for anything that doesn't parse —
    a partial/corrupt write (mid-``mv``, disk full) must degrade to
    UNKNOWN, never crash the health tick or fabricate a timestamp.
    """
    parts = text.strip().split(None, 2)
    if not parts:
        return None
    try:
        epoch = float(parts[0])
    except ValueError:
        return None
    status = parts[1] if len(parts) > 1 else ""
    sha = parts[2] if len(parts) > 2 else ""
    return epoch, status, sha


@check(
    id="webapp_build_heartbeat",
    scope="machine",
    title="webapp build heartbeat",
    order=49,
    description=(
        "coord-web-dist-build.sh's heartbeat file, written on EVERY tick "
        "whether or not there was anything to build (#2122) — the only "
        "surface that distinguishes 'up to date' from 'has not run since "
        "<time>', now that the up-to-date tick is deliberately silent in "
        "the journal (see that script's header)."
    ),
)
def probe_webapp_build_heartbeat(ctx: HealthContext) -> CheckResult:
    """The freshness analogue of :func:`probe_webapp_bundle`, but answering
    a different question. `webapp_bundle` only notices a dead trigger AFTER
    `webapp/` source has moved past the published bundle — a host with no
    pending webapp/ merges looks identically "OK" whether the timer fires
    every 10 minutes or has been disabled for a week. This reads the
    heartbeat directly, so a dead trigger is visible immediately, with no
    dependency on there being unbuilt source to expose it.
    """
    path = resolve_webapp_build_heartbeat_path(ctx)
    values: dict = {"path": str(path)}

    try:
        present = path.exists()
    except OSError:
        present = False

    if not present:
        # Same convention as every other lane in this module: most machines
        # never run coord-web-dist-build.timer at all — absent, not stale.
        return CheckResult(
            check_id="webapp_build_heartbeat",
            scope="machine",
            severity=Severity.OK,
            headroom="not present on this machine",
            values={**values, "present": False},
        )

    values["present"] = True
    try:
        text = path.read_text()
    except OSError as exc:
        return CheckResult(
            check_id="webapp_build_heartbeat",
            scope="machine",
            severity=Severity.UNKNOWN,
            headroom=f"could not read heartbeat file ({type(exc).__name__})",
            error=str(exc),
            values=values,
        )

    parsed = _parse_webapp_build_heartbeat(text)
    if parsed is None:
        return CheckResult(
            check_id="webapp_build_heartbeat",
            scope="machine",
            severity=Severity.UNKNOWN,
            headroom="heartbeat file present but unparseable",
            error=f"unparseable content: {text[:120]!r}",
            values=values,
        )

    epoch, status, sha = parsed
    age_minutes = max(0.0, (ctx.now - epoch) / 60.0)
    values.update(
        {"last_run_at": epoch, "status": status, "sha": sha, "age_minutes": age_minutes}
    )

    th = ctx.thresholds
    warn_minutes = getattr(th, "webapp_build_heartbeat_warn_minutes", 30.0)
    crit_minutes = getattr(th, "webapp_build_heartbeat_crit_minutes", 180.0)

    if age_minutes >= crit_minutes:
        return CheckResult(
            check_id="webapp_build_heartbeat",
            scope="machine",
            severity=Severity.CRIT,
            headroom=f"has not run in {age_minutes / 60.0:.1f}h (last: {status or 'unknown'})",
            detail=(
                "coord-web-dist-build.timer has not fired in a very long "
                "time — check `systemctl --user status "
                "coord-web-dist-build.timer` on this machine; a merged "
                "webapp/ change could be sitting unpublished with no other "
                "symptom until someone loads the dashboard"
            ),
            threshold=f"crit when the last heartbeat is older than {crit_minutes:.0f}min",
            values=values,
        )

    if age_minutes >= warn_minutes:
        return CheckResult(
            check_id="webapp_build_heartbeat",
            scope="machine",
            severity=Severity.WARN,
            headroom=f"has not run in {age_minutes:.0f}m (last: {status or 'unknown'})",
            detail="check `systemctl --user status coord-web-dist-build.timer` on this machine",
            threshold=f"warn when the last heartbeat is older than {warn_minutes:.0f}min",
            values=values,
        )

    return CheckResult(
        check_id="webapp_build_heartbeat",
        scope="machine",
        severity=Severity.OK,
        headroom=f"last ran {age_minutes:.0f}m ago ({status or 'unknown'})",
        values=values,
    )
