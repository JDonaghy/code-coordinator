"""How the agent's ``code-coordinator`` is installed, and how current (#1628).

Two checks, one shared ``pip show`` call:

``agent_venv``
    The agent venv must be a **PyPI install**, not an editable one.  An
    editable agent runs whatever is checked out in someone's source tree —
    including a half-finished feature branch — which makes the machine's
    behaviour untraceable to any release.  #1182 is the recorded version of
    this going wrong (a stale non-editable install silently evaluating
    retired logic and producing a false merge-gate block); an *editable*
    agent is the same failure with no version number to blame it on.

``agent_version``
    How many released versions the install is behind.  The comparison is
    against **PyPI's simple index**, not the JSON API — see
    ``coord.health.pypi`` for why that distinction is load-bearing rather
    than pedantic.

Editable detection uses the ``Editable project location:`` line ``pip show``
prints only for editable installs; a PyPI install has just ``Location:``
pointing into site-packages.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from coord.dist_name import CANDIDATE_NAMES
from coord.health.models import CheckResult, HealthContext, Severity
from coord.health.registry import COST_NETWORK, check
from coord.health.units import expand, shorten_path

#: #2103/#2106: kept only for messages that need "a name" rather than "the
#: installed name" (e.g. the ``description=`` strings below, which are
#: static and evaluated before any probe runs). Everything that actually
#: probes the venv resolves through :data:`CANDIDATE_NAMES` instead of
#: hardcoding this.
PROJECT = CANDIDATE_NAMES[0]

# Where install-agent.sh puts the agent's venv.  Overridable via
# `health.agent_venv_python`.
_DEFAULT_AGENT_VENV = "~/.coord-venv/bin/python3"


def resolve_agent_python(ctx: HealthContext) -> Path:
    """The interpreter whose environment we're reporting on.

    Configured value wins; otherwise the standard agent venv when it exists;
    otherwise the running interpreter (which is the honest answer on a
    coordinator-only box that never installed an agent venv).
    """
    configured = getattr(ctx.thresholds, "agent_venv_python", None)
    if configured:
        return expand(configured, ctx.home)
    candidate = expand(_DEFAULT_AGENT_VENV, ctx.home)
    if candidate.exists():
        return candidate
    return Path(sys.executable)


def pip_show(
    python: Path, names: tuple[str, ...] = CANDIDATE_NAMES, *, timeout: float = 8.0
) -> dict[str, str]:
    """Parse ``<python> -m pip show <name>`` into a field dict, trying each
    of *names* in order and returning the first that resolves (#2103/#2106).

    Resolves through :data:`CANDIDATE_NAMES` rather than a hardcoded
    literal, so a future rename only has to change that one tuple. The
    dict's ``Name`` field (``pip show`` always prints one) tells the caller
    which name actually matched.

    Returns ``{}`` when pip isn't there or the name isn't installed. Raises
    only for genuinely unexpected conditions — the probe wrapping this
    fails soft either way.
    """
    for name in names:
        result = subprocess.run(
            [str(python), "-m", "pip", "show", name],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            continue
        fields = _parse_pip_show(result.stdout)
        if fields:
            return fields
    return {}


def _parse_pip_show(stdout: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in stdout.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    return fields


@check(
    id="agent_venv",
    scope="machine",
    title="agent venv",
    order=40,
    description="The agent's coordinator install is a PyPI install, not editable.",
)
def probe_agent_venv(ctx: HealthContext) -> CheckResult:
    python = resolve_agent_python(ctx)
    try:
        fields = pip_show(python)
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult(
            check_id="agent_venv",
            scope="machine",
            severity=Severity.UNKNOWN,
            headroom=f"could not run pip show ({type(exc).__name__})",
            error=str(exc),
            values={"python": str(python)},
        )

    if not fields:
        tried = " or ".join(CANDIDATE_NAMES)
        return CheckResult(
            check_id="agent_venv",
            scope="machine",
            severity=Severity.UNKNOWN,
            headroom=f"neither {tried} installed for {shorten_path(str(python), str(ctx.home))}",
            error="pip show returned nothing",
            values={"python": str(python)},
        )

    editable_location = fields.get("Editable project location") or ""
    version = fields.get("Version", "")
    location = fields.get("Location", "")

    if editable_location:
        return CheckResult(
            check_id="agent_venv",
            scope="machine",
            severity=Severity.CRIT,
            headroom=f"editable {version or '?'} from {shorten_path(editable_location, str(ctx.home))}",
            detail=(
                "an editable agent runs whatever is checked out in that tree — "
                "its behaviour is not traceable to any release"
            ),
            threshold="crit when editable",
            values={
                "python": str(python),
                "version": version,
                "editable": True,
                "editable_location": editable_location,
                "location": location,
            },
        )

    return CheckResult(
        check_id="agent_venv",
        scope="machine",
        severity=Severity.OK,
        headroom=f"pypi {version or '?'}",
        detail=shorten_path(location, str(ctx.home)) if location else "",
        values={
            "python": str(python),
            "version": version,
            "editable": False,
            "editable_location": None,
            "location": location,
        },
    )


@check(
    id="agent_version",
    scope="machine",
    title="agent version",
    order=41,
    cost=COST_NETWORK,
    description="Installed coordinator version vs the latest release on PyPI's simple index.",
)
def probe_agent_version(ctx: HealthContext) -> CheckResult:
    from coord.health.pypi import latest_release, parse_version  # noqa: PLC0415
    from coord.health.pypi import releases_behind as _releases_behind  # noqa: PLC0415

    th = ctx.thresholds
    python = resolve_agent_python(ctx)
    try:
        fields = pip_show(python)
    except (OSError, subprocess.SubprocessError) as exc:
        fields = {}
        installed_raw = ""
        pip_error: str | None = f"{type(exc).__name__}: {exc}"
    else:
        installed_raw = fields.get("Version", "")
        pip_error = None

    if not installed_raw:
        return CheckResult(
            check_id="agent_version",
            scope="machine",
            severity=Severity.UNKNOWN,
            headroom="installed version unknown",
            error=pip_error or "pip show reported no Version",
            values={"python": str(python)},
        )

    # #2103/#2106: query PyPI for whichever name `pip show` actually
    # matched, not a hardcoded one — a rename mid-flight can leave a
    # `.dist-info` under a different project than the one currently
    # published, and comparing its version against the wrong project's
    # index would compute nonsense skew.
    installed_project = fields.get("Name") or PROJECT

    try:
        latest, finals = latest_release(
            installed_project,
            index_url=th.pypi_index_url,
            timeout=th.network_timeout_secs,
        )
    except Exception as exc:  # noqa: BLE001 — any network/parse failure is "unknown"
        return CheckResult(
            check_id="agent_version",
            scope="machine",
            severity=Severity.UNKNOWN,
            headroom=f"installed {installed_raw}, PyPI index unreachable",
            error=f"{type(exc).__name__}: {exc}",
            values={
                "python": str(python),
                "installed": installed_raw,
                "project": installed_project,
            },
        )

    installed = parse_version(installed_raw)
    if installed is None or latest is None:
        return CheckResult(
            check_id="agent_version",
            scope="machine",
            severity=Severity.UNKNOWN,
            headroom=f"installed {installed_raw}, could not compare against the index",
            error="unparseable version",
            values={"python": str(python), "installed": installed_raw},
        )

    # #2583: shared with `coord release propagate`/`nightly-window`'s own
    # min-releases-behind gate — one computation of "how far behind", not
    # a second one that could quietly disagree with this check's own number.
    behind = _releases_behind(installed, finals)
    if behind >= th.agent_version_crit_behind:
        severity = Severity.CRIT
    elif behind >= th.agent_version_warn_behind:
        severity = Severity.WARN
    else:
        severity = Severity.OK

    if behind == 0:
        headroom = f"{installed_raw} (latest {latest.raw})"
    else:
        headroom = (
            f"{installed_raw}, {behind} release{'s' if behind != 1 else ''} behind "
            f"(latest {latest.raw})"
        )

    return CheckResult(
        check_id="agent_version",
        scope="machine",
        severity=severity,
        headroom=headroom,
        threshold=f"crit at {th.agent_version_crit_behind} behind",
        values={
            "python": str(python),
            "installed": installed_raw,
            "project": installed_project,
            "latest": latest.raw,
            "releases_behind": behind,
            "index_url": th.pypi_index_url,
            "warn_behind": th.agent_version_warn_behind,
            "crit_behind": th.agent_version_crit_behind,
        },
    )
