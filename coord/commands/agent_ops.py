"""The `coord agent` group: per-machine agent server lifecycle
(start/update/restart/clean-worktrees) plus `pause`/`unpause`.
Extracted from coord/cli.py (#747)."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click
import httpx

from coord import __version__
# Aliased: `agent_update` is also the name of this module's click command.
from coord.agent_update import cli_initiator as _cli_initiator
from coord.config import Config
from coord.dist_name import CANDIDATE_NAMES

if TYPE_CHECKING:  # pragma: no cover — typing only
    from collections.abc import Callable

from coord.commands._common import (
    AGENT_PORT,
    _CONFIG_OPTION,
    _load_config,
    server_extra_guard,
)


@click.group(
    invoke_without_command=True,
    help=(
        "Agent server management.  Without a subcommand, starts the agent "
        "server on this machine (port 7433)."
    ),
)


@_CONFIG_OPTION
@click.option(
    "--machine",
    "machine_name",
    default=None,
    help="Machine name from coordinator.yml (defaults to hostname match).",
)


@click.option("--host", "bind_host", default="0.0.0.0", show_default=True)
@click.option("--port", "bind_port", default=AGENT_PORT, show_default=True, type=int)
@click.pass_context
def agent(
    ctx: click.Context,
    config_path: Path,
    machine_name: str | None,
    bind_host: str,
    bind_port: int,
) -> None:
    ctx.ensure_object(dict)
    ctx.obj.update(
        config_path=config_path,
        machine_name=machine_name,
        bind_host=bind_host,
        bind_port=bind_port,
    )
    if ctx.invoked_subcommand is None:
        _start_agent_server(config_path, machine_name, bind_host, bind_port)


def _startup_diagnostic_lines(
    capabilities: list[str], *, path_env: str | None = None
) -> list[str]:
    """Lines to log once at agent startup so the #1671 failure class is
    visible in `journalctl --user -u coord-agent` right after a restart —
    no hand-run `coord doctor` (or SSH + `/proc/<pid>/environ`) required.

    #1671: every machine's `rust` capability read unmet even though `cargo`
    was installed, because the *capability probe* resolves through the
    *agent process's* PATH — and a systemd user unit's PATH is minimal
    (omits `~/.cargo/bin`) unless the unit says otherwise (see
    `deploy/coord-agent.service`). The probe result alone doesn't say
    *why* a tool is missing; logging the resolved PATH plus any declared
    capability that its own probe contradicts turns "mysteriously unmet"
    into "read the last agent restart's log line."

    Pure function (no I/O of its own) so it's cheaply testable without
    mocking `click.echo`/subprocess at the call site — callers pass in
    already-probed data.
    """
    from coord.prereqs import probe_all, tool_versions_summary, unmet_capabilities

    lines = [f"coord agent: PATH={path_env if path_env is not None else os.environ.get('PATH', '')}"]

    probes = probe_all(capabilities)
    unmet = unmet_capabilities(capabilities, probes)
    if not unmet:
        lines.append(
            f"coord agent: capabilities {capabilities} all probe OK "
            f"({tool_versions_summary(probes)})"
        )
    else:
        for cap, reasons in unmet.items():
            for reason in reasons:
                lines.append(
                    f"coord agent: WARNING capability '{cap}' declared in "
                    f"coordinator.yml but its own probe disagrees: {reason} "
                    f"— dispatch_smoke will refuse to route to this machine "
                    f"for '{cap}'-gated work (#1570 D) until this is fixed"
                )
    return lines


def _log_install_location() -> str:
    """One line describing how *this* agent process's coordinator distro
    (whichever of :data:`coord.dist_name.CANDIDATE_NAMES` resolves — #2103)
    is installed — editable (a dev checkout, #1628's flagged risk) vs a
    normal PyPI/site-packages install — logged once at startup so it's
    visible without a separate `coord health` run.

    Best-effort: any failure to determine this (pip missing, `pip show`
    timing out, ...) degrades to a note saying so rather than blowing up
    agent startup over a diagnostic.
    """
    from coord.health.checks.agent_install import pip_show

    try:
        fields = pip_show(Path(sys.executable))
    except (OSError, subprocess.SubprocessError) as exc:
        return f"coord agent: install location unknown ({type(exc).__name__}: {exc})"

    if not fields:
        tried = " or ".join(CANDIDATE_NAMES)
        return f"coord agent: install location unknown (pip show returned nothing for {tried})"

    name = fields.get("Name", "/".join(CANDIDATE_NAMES))
    version = fields.get("Version", "?")
    editable_location = fields.get("Editable project location") or ""
    if editable_location:
        return (
            f"coord agent: {name} {version} — EDITABLE at "
            f"{editable_location} (not a PyPI install — see #1628)"
        )
    location = fields.get("Location", "")
    return f"coord agent: {name} {version} — pypi install at {location}"


@dataclass(frozen=True)
class _AgentStartup:
    """Everything :func:`_start_agent_server` needs out of the config layer,
    resolved in one place so the *order* of resolution is directly testable
    without booting uvicorn (#1712).

    ``config_free_reason`` is ``None`` on the normal path and a human-readable
    explanation when the agent genuinely could not obtain a config from any
    source — it rides into ``/health`` so a capability-less agent is
    distinguishable from a misconfigured one.
    """

    machine: Any
    health_config: Any | None = None
    concurrency: Any = None
    artifact_paths: dict[str, list[str]] = field(default_factory=dict)
    build_commands: dict[str, str] = field(default_factory=dict)
    providers: dict[str, object] = field(default_factory=dict)
    config_free_reason: str | None = None
    notices: tuple[str, ...] = ()


def _load_agent_config(
    config_path: Path,
    svc: Any | None,
    *,
    attempts: int = 3,
    retry_delay: float = 2.0,
    sleep: "Callable[[float], None]" = time.sleep,
) -> Config:
    """Load the coordinator config for `coord agent`, retrying a thin-client
    fetch before giving up — and NEVER degrading to config-free mode (#1712).

    ``_load_config`` already knows how to obtain a config without a local file
    (#1080's thin-client ``GET /config``); it exits(2) when it can't. The only
    thing this wrapper adds is (a) a bounded retry when the config source is a
    *network* one — the agent and the daemon often start at the same time after
    a reboot, and a 2-second-early agent must not lose the whole fleet's
    capability routing over it — and (b) a loud explanation of what the agent
    refused to do, so the journal says "could not reach the daemon" rather than
    leaving an operator to infer it from an empty ``capabilities`` list days
    later (the #1673 / #1712 failure).
    """
    last_exit: SystemExit | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return _load_config(config_path)
        except SystemExit as exc:
            last_exit = exc
            # Only a remote source is worth retrying; a malformed/absent local
            # file will fail identically every time.
            if svc is None or attempt >= attempts:
                break
            click.echo(
                f"coord agent: config load failed (attempt {attempt}/{attempts}) "
                f"— retrying in {retry_delay:g}s",
                err=True,
            )
            sleep(retry_delay)

    local_state = "present" if config_path.exists() else "ABSENT"
    svc_state = f"configured ({svc.url})" if svc is not None else "not configured"
    click.echo(
        "coord agent: FATAL — could not load a coordinator config.\n"
        f"  local coordinator.yml: {config_path} ({local_state})\n"
        f"  board service: {svc_state}\n"
        "  Refusing to fall back to config-free mode: this machine would come "
        "up publishing capabilities=[] and repos=[], silently ineligible for "
        "capability-matched routing with nothing anywhere reporting an error "
        "(#1712). Fix the config source and restart.",
        err=True,
    )
    raise last_exit if last_exit is not None else SystemExit(2)


def _resolve_agent_startup(
    config_path: Path,
    machine_name: str | None,
    *,
    sleep: "Callable[[float], None]" = time.sleep,
    attempts: int = 3,
    retry_delay: float = 2.0,
) -> _AgentStartup:
    """Resolve this agent's machine identity + config-derived settings.

    #1712 — THE ORDER HERE IS THE FIX. The old code branched on
    ``not config_path.exists() and machine_name`` *first*, so passing
    ``--machine`` on a host with no local ``coordinator.yml`` entered
    config-free mode and never even attempted the thin-client daemon fetch
    that lives inside ``_load_config`` (#1080). Two machines with byte-identical
    config availability (no local file, daemon reachable) published different
    capabilities purely because one systemd unit passed ``--machine`` and the
    other didn't — the more explicit invocation being the broken one. So:
    ``--machine`` MUST NOT change which config source is used.

    The old guard conflated two different conditions:

    * *"there is no local config file"* — often true and completely fine,
      because the daemon has one;
    * *"there is no config obtainable at all"* — the only condition that may
      trigger config-free mode.

    Config-free mode is therefore the LAST resort: it needs no local file
    **and** no board service configured. That genuine case (ephemeral Azure
    workers, docs/EPHEMERAL_WORKERS.md) still starts, still publishes
    ``capabilities=[]``, and still does not crash — it just says so out loud
    now, and carries a reason into ``/health`` so a legitimately config-free
    worker is distinguishable from a machine whose declared capabilities
    vanished.
    """
    from coord.client import resolve_board_service  # noqa: PLC0415
    from coord.config import ConcurrencyConfig as _ConcurrencyConfig  # noqa: PLC0415

    svc = resolve_board_service()
    has_local = config_path.exists()

    if not has_local and svc is None:
        # Genuine config-free mode: nothing to load from, anywhere.
        if not machine_name:
            click.echo(
                f"error: no coordinator.yml at {config_path}, no board service "
                "configured (~/.coord/client.toml / $COORD_SERVICE_URL), and no "
                "--machine given — this agent cannot determine its own identity. "
                "Pass --machine NAME to run config-free "
                "(docs/EPHEMERAL_WORKERS.md), or configure a board service.",
                err=True,
            )
            sys.exit(2)
        from coord.models import Machine as _Machine  # noqa: PLC0415

        reason = (
            f"no local coordinator.yml at {config_path} and no board service "
            "configured — running config-free (capabilities and repos come "
            "from the coordinator at dispatch time)"
        )
        return _AgentStartup(
            machine=_Machine(
                name=machine_name,
                host="localhost",
                capabilities=[],
                repos=[],
                repo_paths={},
            ),
            concurrency=_ConcurrencyConfig(),
            config_free_reason=reason,
            # #1712 item 2: never let this be silent. #1671's startup
            # diagnostics iterate over `machine.capabilities`, so with an
            # empty list they print nothing in exactly the case that most
            # needs a signal.
            notices=(f"coord agent: NOTICE {reason}",),
        )

    cfg = _load_agent_config(
        config_path, svc, attempts=attempts, retry_delay=retry_delay, sleep=sleep
    )
    machine = _resolve_machine(cfg, machine_name)

    from coord.providers import build_provider as _build_provider  # noqa: PLC0415

    providers_registry: dict[str, object] = {}
    # #425: instantiate each named provider so the agent can dispatch to it
    # when an assignment names it (spec.provider).  An unknown provider type
    # raises ValueError from build_provider — surface it as a startup failure
    # rather than silently dropping the definition, so operators notice
    # misconfiguration early.
    for prov_name, defn in cfg.providers.definitions.items():
        providers_registry[prov_name] = _build_provider(prov_name, defn, cfg.models)

    notices: list[str] = []
    source = "daemon (thin client)" if svc is not None else str(config_path)
    notices.append(
        f"coord agent: config source={source} machine={machine.name} "
        f"capabilities={list(machine.capabilities)}"
    )
    # #2299: say up front whether this process will notice a coordinator.yml
    # edit, because the answer differs by deployment and the failure mode of
    # guessing wrong is silent. An agent with a local file re-reads it on the
    # next /health poll; a thin client has no local file to watch and its repo
    # list really is fixed for the life of the process. Naming the
    # restart-only fields here means the journal answers "do I need to
    # restart?" without anyone having to remember the issue thread.
    if cfg.path is not None:
        notices.append(
            f"coord agent: watching {cfg.path} for edits — repos, repo_paths, "
            "capabilities, artifact_paths, build_command and (#2326) "
            "providers.definitions are re-read on the next /health poll or "
            "dispatch, no restart needed (#2299). providers.definitions "
            "changes apply to the NEXT dispatch of that provider name — an "
            "in-flight assignment keeps the definition it started with. "
            "RESTART-ONLY: concurrency (bash_wrap_spawn/first_output_timeout/"
            "runtime_ceiling_s) and the bind host/port."
        )
    else:
        notices.append(
            "coord agent: NOTICE config came from the daemon, not a local "
            "file — there is nothing on this machine to re-read, so this "
            "agent's repo list IS fixed until it restarts (#2299)."
        )

    if not machine.capabilities:
        # Declaring no capabilities is legal, but it means dispatch_smoke can
        # never pick this machine for capability-gated work — say so once at
        # startup rather than letting it read as "healthy, just quiet" (#1712).
        notices.append(
            f"coord agent: NOTICE machine {machine.name!r} declares NO "
            "capabilities in coordinator.yml — it is ineligible for every "
            "capability-matched dispatch (#1570 D)"
        )

    return _AgentStartup(
        machine=machine,
        health_config=cfg,
        concurrency=cfg.concurrency,
        # #305: collect artifact_paths per repo for the stash helper.
        artifact_paths={r.name: r.artifact_paths for r in cfg.repos if r.artifact_paths},
        # #1323 (fix #3): collect build_command per repo so _stash_artifacts
        # can run it in the worktree before globbing, ensuring the binary
        # exists regardless of the worker's dev-loop feature flags.
        build_commands={r.name: r.build_command for r in cfg.repos if r.build_command},
        providers=providers_registry,
        notices=tuple(notices),
    )


def _start_agent_server(
    config_path: Path,
    machine_name: str | None,
    bind_host: str,
    bind_port: int,
) -> None:
    """Internal helper: start the uvicorn-backed agent server."""
    # #1237: these imports are function-local *and* guarded so (a) `import
    # coord.cli` stays client-clean on a base install and (b) hitting `coord
    # agent` there says "install the [server] extra" instead of raising a raw
    # ModuleNotFoundError.
    with server_extra_guard("agent"):
        import uvicorn

        from coord.agent import AgentServer
        from coord.agent_app import build_app

    startup = _resolve_agent_startup(config_path, machine_name)
    machine = startup.machine
    concurrency = startup.concurrency

    server = AgentServer(
        machine_name=machine.name,
        capabilities=machine.capabilities,
        repos=machine.repos,
        repo_paths=machine.repo_paths,
        bash_wrap_spawn=concurrency.bash_wrap_spawn,
        first_output_timeout=concurrency.first_output_timeout,
        # #2638: fleet-configurable wall-clock runtime ceiling — was
        # previously hardcoded to `AgentServer`'s own default regardless of
        # what `coordinator.yml` set, unlike its `first_output_timeout`
        # sibling right above.
        runtime_ceiling_s=concurrency.runtime_ceiling_s,
        artifact_paths=startup.artifact_paths,
        build_commands=startup.build_commands,
        providers=startup.providers,
        # #1630: the loaded Config, threaded into AgentServer purely so
        # /health's periodic local check run can resolve this machine's
        # checkouts (coord.health.context.build_context) the same way
        # `coord health` does.  None in config-free mode — the health engine
        # still reports every machine-scope check, just with no checkouts to
        # sweep (same fallback `coord health` itself uses with no
        # coordinator.yml).
        health_config=startup.health_config,
        # #1712: rides into /health so `coord doctor` can tell a legitimately
        # config-free ephemeral worker from a machine whose declared
        # capabilities went missing.
        config_free_reason=startup.config_free_reason,
    )
    # #2139: idle_restart=True turns on the background watcher that
    # re-execs this process onto an already-staged blue/green slot the
    # moment its own active-assignment count reaches (and holds at) zero —
    # the real `coord agent` entrypoint is the one place that should own a
    # background thread for the life of the process; tests build their own
    # app via `build_app(server)` and get the pre-#2139 default (off).
    app = build_app(server, idle_restart=True)
    # #1671: loud-by-default startup diagnostics — resolved PATH, install
    # location, and any declared capability this machine's own probe
    # contradicts — so the class of failure in #1671 shows up in
    # `journalctl --user -u coord-agent` on the very next restart instead
    # of needing an operator to notice a `coord doctor` red and go SSH in.
    click.echo(_log_install_location())
    # #1712: config-source / capability notices go FIRST — with
    # capabilities=[] the #1671 loop below has nothing to say, which is
    # exactly the case that most needs a signal.
    for line in startup.notices:
        click.echo(line)
    for line in _startup_diagnostic_lines(machine.capabilities):
        click.echo(line)
    click.echo(
        f"coord agent: machine={machine.name} repos={machine.repos} "
        f"listening on http://{bind_host}:{bind_port}"
    )
    try:
        uvicorn.run(app, host=bind_host, port=bind_port, log_level="info")
    finally:
        server.shutdown()


def _resolve_target_version(
    explicit_version: str | None,
    *,
    own_version: str = __version__,
    index_url: str = "https://pypi.org/simple",
    timeout: float = 5.0,
) -> tuple[str, list[str]]:
    """Resolve the version `coord agent update` should target.

    #1886 Path A: the target used to come straight from this CLI's own
    ``__version__`` — reasonable-sounding ("bring the fleet in line with
    whatever's running here"), but wrong the instant the operator's own
    install is behind PyPI: a stale CLI silently under-updates the entire
    fleet and still reports success on every machine, because success was
    judged against the wrong target from the start. The target must come
    from PyPI's simple index instead — the same source ``pip install -U``
    itself resolves against (see ``coord.health.pypi`` for why the simple
    index and not the JSON API) — so a stale operator CLI either targets
    the real, newer release or says so loudly, never silently targets its
    own age.

    ``explicit_version`` (``--version``) always wins and skips the network
    call entirely — the documented escape hatch for pinning to a rollback
    or a pre-release on purpose.

    Returns ``(target_version, warning_lines)``. Warnings are informational
    except for the "operator CLI is behind PyPI" case, where the returned
    target is the newer PyPI release (never ``own_version``) — that's the
    whole point of resolving from PyPI instead of trusting the caller.
    A PyPI lookup failure (network down, unparseable index, ...) degrades
    to targeting ``own_version`` with a warning rather than failing the
    whole command — this fix must not turn "no network" into "can no
    longer update the fleet at all."
    """
    if explicit_version:
        return explicit_version, []

    from coord.health.pypi import latest_release_any, parse_version  # noqa: PLC0415

    try:
        project, latest, _finals = latest_release_any(index_url=index_url, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — degrade, don't fail the update
        return own_version, [
            "⚠ could not resolve the latest release from PyPI's simple "
            f"index ({type(exc).__name__}: {exc}) — targeting this CLI's "
            f"own version v{own_version} instead. Pass --version to pin "
            "explicitly, or fix network access so this can be verified.",
        ]

    if latest is None:
        return own_version, [
            "⚠ PyPI's simple index returned no parseable releases under "
            f"{' or '.join(CANDIDATE_NAMES)} — targeting this CLI's own "
            f"version v{own_version} instead. Pass --version to pin "
            "explicitly.",
        ]

    own = parse_version(own_version)
    if own is None or latest > own:
        return latest.raw, [
            f"⚠ this operator CLI is v{own_version} but PyPI's latest "
            f"release ({project}) is v{latest.raw} — targeting v{latest.raw} "
            "(the PyPI release), not this CLI's own version. This CLI's own "
            f"`{project}` install is stale; consider `pip install --upgrade "
            f"{project}` here too.",
        ]

    return own_version, []


@agent.command(
    "update",
    help=(
        "POST /update to one or all agent servers, pinning the upgrade to "
        "the latest release on PyPI's simple index (or --version, when "
        "given) — NOT this CLI's own version (#1886: a stale operator "
        "install must never silently under-update the fleet).  #1241: each "
        "agent installs the target version into a FRESH venv slot, "
        "smoke-checks it, then atomically swaps it into place — an "
        "in-flight update can never leave a torn/partial install for a "
        "concurrent `coord` invocation to observe.  An editable install "
        "(`pip install -e .`) is refused outright, never silently `git "
        "pull`ed.  #2139: an agent with live (RUNNING/PENDING) assignments "
        "still gets the swap — it never disturbs a running worker — but "
        "the RESTART is deferred to that agent's own idle self-restart "
        "watcher, which applies it the moment its assignment count reaches "
        "zero, with no further operator action; pass --force to restart "
        "immediately instead and kill in-flight workers.  Polls each "
        "agent's self-reported *running* version for up to --timeout "
        "seconds and reports success once it matches the requested "
        "version (or, for a deferred host, reports it as staged rather "
        "than waiting out the timeout), escalating to a `systemctl --user "
        "restart coord-agent` if a forced restart's version is stuck."
    ),
)


@_CONFIG_OPTION
@click.option(
    "--machine",
    "machine_filter",
    default=None,
    help="Name of a single machine to update (from coordinator.yml).",
)


@click.option(
    "--all",
    "all_machines",
    is_flag=True,
    help="Update all machines (mutually exclusive with --machine).",
)


@click.option(
    "--version",
    "version_override",
    default=None,
    help=(
        "Pin the upgrade to this exact version instead of resolving the "
        "latest release from PyPI's simple index (#1886)."
    ),
)


@click.option(
    "--timeout",
    default=120,
    show_default=True,
    type=int,
    help="Seconds to wait for the agent to come back online after restart.",
)


@click.option(
    "--force",
    is_flag=True,
    help=(
        "Update even if the agent has live (RUNNING/PENDING) assignments — "
        "the restart-after-swap kills them mid-flight (#1241). Without "
        "this, an agent with live sessions refuses the update outright."
    ),
)


def agent_update(
    config_path: Path,
    machine_filter: str | None,
    all_machines: bool,
    version_override: str | None,
    timeout: int,
    force: bool,
) -> None:
    # #1886 Path A: the target used to be `__version__` — this CLI's own
    # version.  A stale operator install (PyPI already has v0.4.108, this
    # CLI is still v0.4.107) silently under-updated the whole fleet and
    # still printed three clean checkmarks, because "success" was judged
    # against the wrong target from the start.  Resolve the target from
    # PyPI's simple index instead (the same source `pip install -U`
    # itself resolves against — see coord.health.pypi) so a stale operator
    # CLI is either overridden by the real target or refuses loudly,
    # rather than silently pinning the fleet to its own age.  --version
    # remains an explicit escape hatch for pinning to something else on
    # purpose (a rollback, a pre-release, ...).
    target_version, resolve_warnings = _resolve_target_version(version_override)
    for line in resolve_warnings:
        click.echo(line, err=True)

    # #1568: sending an explicit target_version lets the agent pin its pip
    # install to that exact release (turning a stale-index no-op into a
    # loud pip failure) and lets THIS command verify success by polling
    # for that exact version, instead of inferring success from "the POST
    # was accepted" (false positive on a cache-stale no-op) or failure
    # from "the process stopped answering pings" (false negative on the
    # execv-under-systemd restart, #404).

    cfg = _load_config(config_path)
    targets = _resolve_agent_targets(cfg, machine_filter, all_machines)
    if not targets:
        click.echo("No machines to update.", err=True)
        sys.exit(2)

    click.echo(f"Requesting upgrade to v{target_version}...")

    # Capture each agent's start time BEFORE we trigger /update so the
    # wait loop can distinguish "old agent still answering during pip"
    # from "new agent came back up".
    pre_started_at = _fetch_pre_started_at(targets)

    # #1241: a machine that refuses (editable install, or live sessions
    # without --force) never gets restarted, so polling it for a version
    # change would just burn the whole --timeout window for nothing.
    # `posted` is the subset of `targets` that actually accepted the POST;
    # `refused_reasons` carries why the rest didn't.
    posted: list = []
    refused_reasons: dict[str, str] = {}

    for machine in targets:
        url = f"http://{machine.host}:{AGENT_PORT}/update"
        click.echo(f"  {machine.name}: POST {url} ...", nl=False)
        try:
            resp = httpx.post(
                url,
                json={
                    "target_version": target_version,
                    "force": force,
                    # #2121: name the invocation, so the audit row on the
                    # target host points back at a person and a box.
                    "initiator": _cli_initiator("coord agent update"),
                },
                timeout=10,
            )
            if resp.status_code == 202:
                data = resp.json()
                click.echo(f" accepted (mode: {data.get('mode', '?')})")
                posted.append(machine)
            elif resp.status_code == 409:
                try:
                    reason = resp.json().get("error") or "refused"
                except Exception:
                    reason = "refused"
                click.echo(f" refused — {reason}")
                refused_reasons[machine.name] = reason
            else:
                click.echo(f" HTTP {resp.status_code}")
                refused_reasons[machine.name] = f"HTTP {resp.status_code}"
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            click.echo(f" error: {e}")
            refused_reasons[machine.name] = str(e)

    all_matched = not refused_reasons
    if posted:
        click.echo(
            f"\nWaiting up to {timeout}s for agent(s) to report v{target_version}..."
        )
        outcomes = _wait_agents_updated(
            posted,
            target_version=target_version,
            timeout=timeout,
            pre_started_at=pre_started_at,
        )

        click.echo("")
        for machine in posted:
            outcome = outcomes[machine.name]
            version_now = outcome["version_now"]
            if outcome["matched"]:
                vbefore = outcome.get("version_before") or "?"
                click.echo(f"  {machine.name}: ✓ {vbefore} → {target_version}")
                continue

            result = outcome.get("result")
            if result == "staged":
                # #2139: the swap landed but the agent had live assignments
                # when it did — restart is intentionally deferred to that
                # agent's own idle self-restart watcher, not something this
                # command needs to drive or wait out. Not a failure.
                click.echo(
                    f"  {machine.name}: ⧗ staged v{target_version} — "
                    f"{outcome.get('error') or 'restart deferred until idle (#2139)'}"
                )
                continue

            all_matched = False
            if result == "no_change":
                click.echo(
                    f"  {machine.name}: ✗ no change (still {version_now}) — "
                    f"{outcome.get('error') or 'pip resolved to the same version'}",
                    err=True,
                )
            elif result == "failed":
                err = outcome.get("error") or "pip failed; see ~/.coord/last_update.log"
                click.echo(f"  {machine.name}: ✗ failed — {err}", err=True)
            elif outcome.get("escalated"):
                click.echo(
                    f"  {machine.name}: ✗ pip upgraded to {target_version} but the "
                    f"process is stuck reporting {version_now} even after a "
                    "`systemctl --user restart` — needs manual investigation",
                    err=True,
                )
            elif not outcome.get("came_online"):
                click.echo(f"  {machine.name}: ✗ did not come back online", err=True)
            else:
                installed_now = outcome.get("installed_version_now")
                if installed_now and installed_now != version_now:
                    # #1886 Path B: pip (or the disk) has already moved to
                    # a newer install, but the running process — the only
                    # thing "matched" ever keys off of — hasn't caught up.
                    # Distinguishing this from a bare "still reporting X"
                    # is the whole point: it says the process needs a
                    # restart, not another pip attempt.
                    click.echo(
                        f"  {machine.name}: ✗ installed {installed_now} but the "
                        f"running process still reports {version_now} (expected "
                        f"{target_version}) — it hasn't restarted since the "
                        "update; try `systemctl --user restart coord-agent` "
                        "on that machine",
                        err=True,
                    )
                else:
                    click.echo(
                        f"  {machine.name}: ✗ still reporting {version_now}, "
                        f"expected {target_version}",
                        err=True,
                    )

    if refused_reasons:
        if posted:
            click.echo("")
        for name, reason in refused_reasons.items():
            click.echo(f"  {name}: ✗ refused — {reason}", err=True)

    if not all_matched:
        sys.exit(1)


@agent.command(
    "restart",
    help=(
        "POST /restart to one or all agent servers.  The agent waits for "
        "active workers to finish (or cancels them after --cancel-timeout "
        "seconds) then restarts itself.  Waits up to --timeout seconds for "
        "the agent(s) to come back online."
    ),
)


@_CONFIG_OPTION
@click.option(
    "--machine",
    "machine_filter",
    default=None,
    help="Name of a single machine to restart (from coordinator.yml).",
)


@click.option(
    "--all",
    "all_machines",
    is_flag=True,
    help="Restart all machines (mutually exclusive with --machine).",
)


@click.option(
    "--timeout",
    default=120,
    show_default=True,
    type=int,
    help="Seconds to wait for the agent to come back online after restart.",
)


@click.option(
    "--cancel-timeout",
    default=30,
    show_default=True,
    type=int,
    help="Seconds the agent waits for active workers to finish before cancelling them.",
)


def agent_restart(
    config_path: Path,
    machine_filter: str | None,
    all_machines: bool,
    timeout: int,
    cancel_timeout: int,
) -> None:
    cfg = _load_config(config_path)
    targets = _resolve_agent_targets(cfg, machine_filter, all_machines)
    if not targets:
        click.echo("No machines to restart.", err=True)
        sys.exit(2)

    for machine in targets:
        url = f"http://{machine.host}:{AGENT_PORT}/restart"
        click.echo(f"  {machine.name}: POST {url} ...", nl=False)
        try:
            resp = httpx.post(
                url,
                json={"cancel_timeout": cancel_timeout},
                timeout=10,
            )
            if resp.status_code == 202:
                data = resp.json()
                active = data.get("active_workers", 0)
                click.echo(f" accepted ({active} active worker(s))")
            else:
                click.echo(f" HTTP {resp.status_code}")
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            click.echo(f" error: {e}")

    if targets:
        click.echo(f"\nWaiting up to {timeout}s for agent(s) to come back online...")
        results = _wait_agents_online(targets, timeout=timeout)
        for name, came_back in results.items():
            tag = "✓ online" if came_back else "✗ did not come back"
            click.echo(f"  {name}: {tag}")
        if not all(results.values()):
            sys.exit(1)


@agent.command(
    "clean-worktrees",
    help=(
        "POST /worktree-clean to one or all agent servers.  Each agent "
        "removes git worktrees whose assignment is in a terminal state "
        "(done/failed/cancelled) and finished more than --recent-secs ago.  "
        "Running/pending worktrees are never touched."
    ),
)


@_CONFIG_OPTION
@click.option(
    "--machine",
    "machine_filter",
    default=None,
    help="Name of a single machine to clean (from coordinator.yml).",
)


@click.option(
    "--all",
    "all_machines",
    is_flag=True,
    help="Clean all machines (mutually exclusive with --machine).",
)


@click.option(
    "--recent-secs",
    default=300,
    show_default=True,
    type=int,
    help=(
        "Minimum age in seconds for a terminal assignment's worktree to be "
        "eligible for removal (guards against racing with a just-finished worker)."
    ),
)


def agent_clean_worktrees(
    config_path: Path,
    machine_filter: str | None,
    all_machines: bool,
    recent_secs: int,
) -> None:
    cfg = _load_config(config_path)
    targets = _resolve_agent_targets(cfg, machine_filter, all_machines)
    if not targets:
        click.echo("No machines to clean.", err=True)
        sys.exit(2)

    any_error = False
    for machine in targets:
        url = f"http://{machine.host}:{AGENT_PORT}/worktree-clean"
        click.echo(f"  {machine.name}: POST {url} ...", nl=False)
        try:
            resp = httpx.post(url, json={"recent_secs": recent_secs}, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                cleaned = data.get("cleaned", 0)
                kept = data.get("kept", 0)
                freed = data.get("bytes_freed", 0)
                freed_mb = freed / (1024 * 1024)
                # #1402: the same endpoint GCs the shared cargo target cache.
                cargo_mb = data.get("cargo_cache_bytes", 0) / (1024 * 1024)
                evicted = data.get("cargo_caches_evicted", 0)
                # #2137: pruned bytes and, above all, the GC's give-up signal.
                pruned_mb = data.get("cargo_pruned_bytes", 0) / (1024 * 1024)
                line = (
                    f" cleaned={cleaned} kept={kept} freed={freed_mb:.1f} MB "
                    f"cargo-cache={cargo_mb:.1f} MB (evicted {evicted}, "
                    f"pruned {pruned_mb:.1f} MB)"
                )
                click.echo(line)
                if data.get("cargo_over_cap"):
                    reason = data.get("cargo_over_cap_reason") or "cache over cap"
                    click.echo(
                        f"    WARN: cargo cache GC could not get under cap — {reason}"
                    )
            else:
                click.echo(f" HTTP {resp.status_code}")
                any_error = True
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            click.echo(f" error: {e}")
            any_error = True

    if any_error:
        sys.exit(1)


@agent.command(
    "versions",
    help=(
        "GET /health from one or all agent servers and print each one's "
        "self-reported version alongside the coordinator's own.  #1568: "
        "a version split-brain across the fleet is only detectable by "
        "comparing versions directly — this is the fleet-wide check to "
        "run before trusting a rule change, and after `coord agent "
        "update` to confirm it actually landed everywhere."
    ),
)
@_CONFIG_OPTION
@click.option(
    "--machine",
    "machine_filter",
    default=None,
    help="Name of a single machine to check (from coordinator.yml).",
)
@click.option(
    "--all",
    "all_machines",
    is_flag=True,
    help="Check all machines (mutually exclusive with --machine).",
)
def agent_versions(
    config_path: Path,
    machine_filter: str | None,
    all_machines: bool,
) -> None:
    cfg = _load_config(config_path)
    targets = _resolve_agent_targets(cfg, machine_filter, all_machines)
    if not targets:
        click.echo("No machines to check.", err=True)
        sys.exit(2)

    click.echo(f"coordinator: v{__version__}\n")

    versions_seen: set[str] = set()
    any_offline = False
    any_mismatch = False
    for machine in targets:
        version: str | None
        try:
            resp = httpx.get(f"http://{machine.host}:{AGENT_PORT}/health", timeout=5)
            version = resp.json().get("version") if resp.status_code == 200 else None
        except (httpx.HTTPError, httpx.TimeoutException):
            version = None

        if version is None:
            click.echo(f"  {machine.name}: ✗ unreachable", err=True)
            any_offline = True
            continue

        versions_seen.add(version)
        mismatch = version != __version__
        any_mismatch = any_mismatch or mismatch
        marker = "  ⚠ mismatch" if mismatch else ""
        click.echo(f"  {machine.name}: v{version}{marker}")

    if len(versions_seen) > 1:
        click.echo(
            f"\n⚠ split-brain: {len(versions_seen)} distinct versions across the "
            f"fleet ({', '.join(sorted(versions_seen))}). Do not trust a rule "
            "change until `coord agent update --all` brings everyone in line.",
            err=True,
        )
        sys.exit(1)
    if any_mismatch:
        click.echo(
            f"\n⚠ mismatch: fleet is uniformly on a version that differs from "
            f"the coordinator's own v{__version__}. Run `coord agent update "
            "--all` to bring the fleet in line.",
            err=True,
        )
        sys.exit(1)
    if any_offline:
        sys.exit(1)


def _resolve_agent_targets(cfg, machine_filter: str | None, all_machines: bool):
    """Return the list of Machine objects to target for update/restart.

    Validates --machine / --all flags and prints errors on bad input.
    """
    if machine_filter and all_machines:
        click.echo("error: --machine and --all are mutually exclusive.", err=True)
        sys.exit(2)
    if not machine_filter and not all_machines:
        click.echo(
            "error: specify either --machine NAME or --all.", err=True
        )
        sys.exit(2)

    if machine_filter:
        machine = next((m for m in cfg.machines if m.name == machine_filter), None)
        if machine is None:
            click.echo(
                f"error: machine {machine_filter!r} not in coordinator.yml "
                f"(have: {[m.name for m in cfg.machines]})",
                err=True,
            )
            sys.exit(2)
        return [machine]

    return list(cfg.machines)


def _wait_agents_online(
    machines: list,
    *,
    timeout: float = 120.0,
    poll_interval: float = 2.0,
    pre_started_at: dict[str, float | None] | None = None,
) -> dict[str, bool]:
    """Poll /health on each machine until all are online or timeout expires.

    When ``pre_started_at`` is provided, a machine is only considered
    "back" once its reported ``agent_started_at`` differs from the
    pre-update value (or appears for the first time on an agent that
    didn't expose it before).  This stops the CLI from racing the old
    agent while a pip upgrade is still running inside it.

    For agents that don't expose ``agent_started_at`` at all (pre-v0.4.3),
    we fall back to "responding to /health is enough."

    Returns ``{machine_name: came_back_online}`` for every machine.
    """
    # Scale the sleep down for short timeouts (e.g. tests passing
    # --timeout 1) so a tiny deadline isn't dominated by a single fixed
    # 2s sleep — callers that want the full 2s just pass a bigger timeout.
    poll_interval = min(poll_interval, max(timeout / 5, 0.05))
    deadline = time.time() + timeout
    online: set[str] = set()
    pre = pre_started_at or {}

    while time.time() < deadline:
        for machine in machines:
            if machine.name in online:
                continue
            try:
                resp = httpx.get(
                    f"http://{machine.host}:{AGENT_PORT}/health",
                    timeout=3.0,
                )
                if resp.status_code != 200:
                    continue
                if machine.name in pre:
                    pre_val = pre[machine.name]
                    try:
                        cur = resp.json().get("agent_started_at")
                    except Exception:
                        cur = None
                    if cur is None:
                        # Old agent (no started_at) — fall back to "alive
                        # is good enough" so /update on a pre-v0.4.3
                        # agent isn't blocked forever.
                        online.add(machine.name)
                    elif pre_val is None or cur != pre_val:
                        # Either the agent didn't expose started_at
                        # before (just upgraded TO v0.4.3) or the value
                        # changed (restart happened).
                        online.add(machine.name)
                else:
                    online.add(machine.name)
            except Exception:
                pass

        if len(online) == len(machines):
            break
        time.sleep(poll_interval)

    return {m.name: m.name in online for m in machines}


def _wait_agents_updated(
    machines: list,
    *,
    target_version: str,
    timeout: float = 120.0,
    poll_interval: float = 2.0,
    pre_started_at: dict[str, float | None] | None = None,
) -> dict[str, dict]:
    """Poll /health on each machine until its self-reported version equals
    ``target_version``, escalating to a driven restart if needed.

    #1568: success is judged by the version the agent actually reports —
    never by "the POST was accepted" and never by "the process answers
    pings again."  Those liveness signals fail in opposite directions:

    - Cause A: pip resolves to a cached/stale version and exits 0.  The
      POST is accepted, the process never restarts, but nothing changed —
      the old ``_wait_agents_online``-based check reported success anyway.
    - Cause B (#404): the update's ``os.execv`` self-restart doesn't take
      under systemd, so the OLD process keeps answering /health after a
      real upgrade.  ``_wait_agents_online`` reported "did not come back"
      even though the new version was installed and the service was
      active.

    When a machine's pip step genuinely succeeded (``last_update.result
    == "upgraded"``) but the version still hasn't advanced once the
    normal poll window elapses, escalate once with an SSH-driven
    ``systemctl --user restart coord-agent`` — the documented fix for the
    execv-under-systemd stall (see docs/AGENT_OPERATIONS.md) — and give
    it one more short window before giving up.

    Returns ``{machine_name: {matched, came_online, version_now,
    installed_version_now, version_before, result, error, escalated}}``.
    """
    # Scale the sleep down for short timeouts (e.g. tests passing
    # --timeout 1) so a tiny deadline isn't dominated by a single fixed
    # 2s sleep — callers that want the full 2s just pass a bigger timeout.
    poll_interval = min(poll_interval, max(timeout / 5, 0.05))
    pre = pre_started_at or {}
    out: dict[str, dict] = {
        m.name: {
            "matched": False,
            "came_online": False,
            "version_now": "?",
            "installed_version_now": None,
            "version_before": None,
            "result": None,
            "error": None,
            "escalated": False,
        }
        for m in machines
    }
    pending = {m.name: m for m in machines}

    def _poll_once(machine) -> bool:
        """Fetch /health once, update out[machine.name], return True on match."""
        info = out[machine.name]
        try:
            resp = httpx.get(f"http://{machine.host}:{AGENT_PORT}/health", timeout=3.0)
            if resp.status_code != 200:
                return False
            health = resp.json()
        except Exception:
            return False

        # #1886 Path B: `version` (checked below) is the RUNNING process's
        # loaded-module version — the only thing "matched" may key off of.
        # `installed_version` is a disk read that can advance the instant
        # pip writes to site-packages, well before (or without) a restart
        # (#404's execv-under-systemd stall). Recording both lets a
        # still-pending outcome say *why* — "installed advanced, running
        # didn't" — instead of a bare "still reporting X".
        version_now = health.get("version")
        info["version_now"] = version_now or "?"
        info["installed_version_now"] = health.get("installed_version")
        last = health.get("last_update") or {}
        info["result"] = last.get("result")
        info["error"] = last.get("error")
        info["version_before"] = last.get("version_before")

        if machine.name in pre:
            pre_val = pre[machine.name]
            cur = health.get("agent_started_at")
            if cur is None or pre_val is None or cur != pre_val:
                info["came_online"] = True
        else:
            info["came_online"] = True

        if version_now == target_version:
            info["matched"] = True
            return True
        return False

    deadline = time.time() + timeout
    while time.time() < deadline and pending:
        for name in list(pending):
            if _poll_once(pending[name]):
                del pending[name]
            elif out[name]["result"] == "staged":
                # #2139: the swap landed but the agent had live assignments,
                # so it deliberately deferred the restart to its own idle
                # self-restart watcher — a healthy outcome on a schedule
                # this poll loop has no way to predict (it fires whenever
                # THAT host next goes idle, not on this loop's timeout).
                # Stop burning the wait window on a host that isn't stuck;
                # `_do_update`'s explanatory message is already in
                # `info["error"]` for the caller to print.
                del pending[name]
        if not pending:
            break
        time.sleep(poll_interval)

    # Escalate the machines still stuck on the old version whose pip step
    # actually succeeded — the classic execv-under-systemd stall.  Give
    # each a short follow-up window after the driven restart.
    escalate_timeout = min(30.0, max(timeout / 2, 15.0))
    for name in list(pending):
        machine = pending[name]
        info = out[name]
        if info["result"] != "upgraded":
            continue
        info["escalated"] = _escalate_restart(machine)
        if not info["escalated"]:
            continue
        sub_deadline = time.time() + escalate_timeout
        while time.time() < sub_deadline:
            if _poll_once(machine):
                del pending[name]
                break
            time.sleep(poll_interval)

    return out


def _escalate_restart(machine) -> bool:
    """Best-effort ``systemctl --user restart coord-agent`` over SSH.

    #404 / #1568: ``/update``'s ``os.execv`` self-restart does not take
    under systemd — same PID, stale version.  ``XDG_RUNTIME_DIR=/run/user/
    $(id -u)`` is load-bearing: a bare ``systemctl --user restart``
    silently no-ops in a non-interactive SSH session.  See
    docs/AGENT_OPERATIONS.md for the manual runbook this automates.

    Returns True if the ssh command itself exited 0 — NOT whether the
    agent actually came back on the new version; the caller re-polls
    /health afterwards to confirm that.
    """
    cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new",
        machine.host,
        "XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user restart coord-agent",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except Exception:
        return False
    return result.returncode == 0


def _fetch_pre_started_at(machines: list) -> dict[str, float | None]:
    """Capture each agent's `agent_started_at` BEFORE we trigger /update.

    Returns ``{name: started_at_or_None}`` — None when the agent is
    unreachable or doesn't expose the field yet.
    """
    out: dict[str, float | None] = {}
    for m in machines:
        try:
            resp = httpx.get(f"http://{m.host}:{AGENT_PORT}/health", timeout=3.0)
            if resp.status_code == 200:
                out[m.name] = resp.json().get("agent_started_at")
            else:
                out[m.name] = None
        except Exception:
            out[m.name] = None
    return out


def _resolve_machine(cfg: Config, explicit_name: str | None):
    if explicit_name:
        m = next((m for m in cfg.machines if m.name == explicit_name), None)
        if m is None:
            click.echo(
                f"error: machine {explicit_name!r} not in coordinator.yml "
                f"(have: {[m.name for m in cfg.machines]})",
                err=True,
            )
            sys.exit(2)
        return m

    hostname = socket.gethostname().lower()
    short = hostname.split(".")[0]
    candidates = [m for m in cfg.machines if m.name.lower() == short or m.host.lower() == hostname or m.host.lower().split(".")[0] == short]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        click.echo(
            f"error: could not match hostname {hostname!r} to any machine in coordinator.yml. "
            f"Pass --machine explicitly. Known: {[m.name for m in cfg.machines]}",
            err=True,
        )
        sys.exit(2)
    click.echo(
        f"error: hostname {hostname!r} matches multiple machines: "
        f"{[m.name for m in candidates]}. Pass --machine explicitly.",
        err=True,
    )
    sys.exit(2)


@click.command(
    help=(
        "Pause a machine — no new agents will be routed to it until "
        "`coord unpause` is called.  In-flight assignments are NOT "
        "cancelled (use `coord stop` for that).\n\n"
        "MACHINE is the local name from coordinator.yml."
    ),
)


@_CONFIG_OPTION
@click.argument("machine")
def pause(config_path: Path, machine: str) -> None:
    from coord.machine_pause import pause as _pause

    # #1563: on a thin client this routes to the daemon's `/pause` endpoint
    # and can raise (network/HTTP failure) — surface that loudly rather than
    # letting a bare traceback stand in for "coord pause silently no-oped",
    # which is exactly the failure mode this fix closes.
    try:
        changed = _pause(machine)
    except Exception as e:  # noqa: BLE001
        click.echo(
            f"error: could not confirm pause of {machine!r} with the daemon: {e}",
            err=True,
        )
        sys.exit(1)
    if changed:
        click.echo(f"paused: {machine}")
    else:
        click.echo(f"already paused: {machine}")


@click.command(
    help=(
        "Resume a paused machine — new assignments can be routed to it "
        "again.  No-op if the machine wasn't paused.\n\n"
        "#1862: if MACHINE isn't hand-paused but IS inside its "
        "coordinator.yml `quiet_hours` window, this overrides that window "
        "until it ends rather than silently doing nothing (a bare re-read "
        "would otherwise show it paused again on the very next poll)."
    ),
)


@_CONFIG_OPTION
@click.argument("machine")
def unpause(config_path: Path, machine: str) -> None:
    from coord.machine_pause import unpause as _unpause

    # #1862: best-effort quiet-hours context — an unloadable/placeholder
    # config must not block the (still fully functional) explicit-unpause
    # path below, it only means quiet-hours overrides can't be resolved.
    # Deliberately NOT `_load_config()`: that helper `sys.exit(2)`s on a
    # ConfigError (a SystemExit, which a bare `except Exception:` doesn't
    # catch) and, on a thin client, fetches the daemon's remote config over
    # HTTP — wasted work here, since a thin client's `_unpause()` call below
    # routes over HTTP too and never even looks at `machines`.
    machines = None
    try:
        from coord.config import load as _load_yaml_config  # noqa: PLC0415

        machines = _load_yaml_config(config_path).machines
    except Exception:  # noqa: BLE001
        pass

    # #1563: fail loudly, see the matching comment on pause() above.
    try:
        outcome = _unpause(machine, machines)
    except Exception as e:  # noqa: BLE001
        click.echo(
            f"error: could not confirm unpause of {machine!r} with the daemon: {e}",
            err=True,
        )
        sys.exit(1)
    if outcome.kind == "resumed":
        click.echo(f"resumed: {machine}")
    elif outcome.kind == "quiet_override":
        click.echo(
            f"{machine}: quiet hours overridden until {outcome.quiet_until} "
            f"({outcome.tz}) — resumes its normal quiet schedule next window"
        )
    else:
        click.echo(f"not paused: {machine}")


# ── #2146: `coord quiet-hours` ──────────────────────────────────────────────


def _local_tz_name() -> str | None:
    """The CLIENT's IANA zone name, or None if it can't be determined.

    #2146: `--tz` must default to the operator's own zone — they mean "22:00
    MY time", and the daemon runs UTC, so defaulting to the daemon's clock
    would fire hours off. `datetime.now().astimezone().tzinfo` gives an
    ABBREVIATION ("CDT"), not an IANA name `ZoneInfo` can resolve, so this
    walks the usual POSIX sources first and only falls back to that.

    Returning None (rather than guessing "UTC") is deliberate: the caller
    turns it into "pass --tz explicitly", because a silently-UTC window is
    exactly the wrong-hour failure #1862 made `tz` mandatory to prevent.
    """
    from zoneinfo import ZoneInfo  # noqa: PLC0415

    def _ok(name: str | None) -> str | None:
        if not name or name in ("localtime", "UTC0"):
            return None
        try:
            ZoneInfo(name)
        except Exception:  # noqa: BLE001 — not a resolvable zone, try the next source
            return None
        return name

    candidates: list[str | None] = [os.environ.get("TZ")]
    try:
        candidates.append(Path("/etc/timezone").read_text(encoding="utf-8").strip())
    except OSError:
        pass
    try:
        parts = Path("/etc/localtime").resolve().parts
        if "zoneinfo" in parts:
            candidates.append("/".join(parts[parts.index("zoneinfo") + 1:]))
    except OSError:
        pass
    from datetime import datetime as _dt  # noqa: PLC0415

    candidates.append(_dt.now().astimezone().tzname())
    for candidate in candidates:
        resolved = _ok(candidate)
        if resolved is not None:
            return resolved
    return None


def _parse_window_arg(window: str) -> tuple[str, str]:
    """``"22:00-08:00"`` → ``("22:00", "08:00")``. Shape only — the real
    validation (HH:MM, IANA tz, start != end) is `coord.config`'s, so the
    CLI can never accept a window `coordinator.yml` would reject."""
    parts = window.split("-")
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        raise ValueError(
            f"window must look like '22:00-08:00', got {window!r}"
        )
    return parts[0].strip(), parts[1].strip()


def _quiet_hours_rows(machines: Any) -> dict[str, dict]:
    from coord.machine_pause import effective_quiet_hours  # noqa: PLC0415

    return effective_quiet_hours(machines)


def _describe_source(source: str) -> str:
    return "set here" if source == "store" else "coordinator.yml"


@click.command(
    "quiet-hours",
    help=(
        "Show or set a machine's recurring no-new-dispatch window (#2146).\n\n"
        "\b\n"
        "  coord quiet-hours MACHINE 22:00-08:00 [--tz America/Chicago]\n"
        "  coord quiet-hours MACHINE --clear\n"
        "  coord quiet-hours [MACHINE] --list\n"
        "  coord quiet-hours MACHINE --print-yaml\n\n"
        "A window set here is stored on the DAEMON (alongside pauses and "
        "release cordons) and overrides that machine's coordinator.yml "
        "`quiet_hours:` block entirely — it does not rewrite the YAML. "
        "`--print-yaml` emits a paste-ready block for when a window should "
        "become permanent and version-controlled.\n\n"
        "--tz defaults to THIS machine's zone, never the daemon's UTC, and "
        "the resolved zone is echoed back so a wrong default is visible "
        "immediately."
    ),
)


@_CONFIG_OPTION
@click.argument("machine", required=False)
@click.argument("window", required=False)
@click.option("--tz", "tz", default=None, help="IANA zone (default: this machine's).")
@click.option("--clear", "clear_", is_flag=True, help="Remove an operator-set window.")
@click.option("--list", "list_", is_flag=True, help="Show windows and where they came from.")
@click.option(
    "--print-yaml",
    "print_yaml",
    is_flag=True,
    help="Emit a coordinator.yml `quiet_hours:` block for MACHINE.",
)
def quiet_hours(
    config_path: Path,
    machine: str | None,
    window: str | None,
    tz: str | None,
    clear_: bool,
    list_: bool,
    print_yaml: bool,
) -> None:
    from coord.machine_pause import clear_quiet_hours, set_quiet_hours  # noqa: PLC0415

    # Best-effort config, exactly like `unpause` above: an unloadable config
    # must not block the (fully functional) store path — it only means
    # coordinator.yml-sourced windows can't be listed alongside store ones.
    machines = None
    try:
        from coord.config import load as _load_yaml_config  # noqa: PLC0415

        machines = _load_yaml_config(config_path).machines
    except Exception:  # noqa: BLE001
        pass

    if list_ or (machine is None and not (clear_ or print_yaml or window)):
        rows = _quiet_hours_rows(machines)
        if machine is not None:
            rows = {k: v for k, v in rows.items() if k == machine}
        if not rows:
            click.echo(
                "no quiet hours set"
                + (f" for {machine}" if machine else "")
                + " (set one: coord quiet-hours MACHINE 22:00-08:00)"
            )
            return
        width = max(len(name) for name in rows)
        for name, row in sorted(rows.items()):
            click.echo(
                f"{name.ljust(width)}  {row.get('start')}-{row.get('end')}  "
                f"{row.get('tz')}  [{_describe_source(str(row.get('source') or ''))}]"
            )
        return

    if machine is None:
        click.echo("error: MACHINE is required", err=True)
        sys.exit(2)

    if print_yaml:
        # The promotion path: a window that has proved itself belongs in
        # version control, where a daemon rebuild can't lose it.
        if window is not None:
            try:
                start, end = _parse_window_arg(window)
            except ValueError as e:
                click.echo(f"error: {e}", err=True)
                sys.exit(2)
            zone = tz or _local_tz_name()
        else:
            row = _quiet_hours_rows(machines).get(machine)
            if row is None:
                click.echo(
                    f"error: no quiet hours known for {machine!r} — pass a window "
                    "(e.g. `coord quiet-hours MACHINE 22:00-08:00 --print-yaml`)",
                    err=True,
                )
                sys.exit(1)
            start, end, zone = row.get("start"), row.get("end"), tz or row.get("tz")
        if not zone:
            click.echo(
                "error: could not determine a time zone — pass --tz "
                "(e.g. --tz America/Chicago)",
                err=True,
            )
            sys.exit(2)
        click.echo(f"  # machines[] entry for {machine!r} in coordinator.yml")
        click.echo("  quiet_hours:")
        click.echo(f'    start: "{start}"')
        click.echo(f'    end: "{end}"')
        click.echo(f'    tz: "{zone}"')
        return

    if clear_:
        # #2146 acceptance: "nothing set" is NOT success. A machine can still
        # have a coordinator.yml block this cannot touch, and reporting
        # "cleared" for a no-op is the failure class this feature exists to
        # avoid.
        try:
            changed = clear_quiet_hours(machine)
        except Exception as e:  # noqa: BLE001
            click.echo(
                f"error: could not confirm clearing quiet hours for {machine!r} "
                f"with the daemon: {e}",
                err=True,
            )
            sys.exit(1)
        if not changed:
            click.echo(f"nothing set: {machine} has no operator-set quiet hours")
            return
        click.echo(f"cleared: {machine} quiet hours")
        row = _quiet_hours_rows(machines).get(machine)
        if row is not None:
            click.echo(
                f"note: {machine} still has a coordinator.yml window "
                f"{row.get('start')}-{row.get('end')} ({row.get('tz')})"
            )
        return

    if window is None:
        click.echo(
            "error: pass a window (e.g. `coord quiet-hours MACHINE 22:00-08:00`), "
            "--clear, --list or --print-yaml",
            err=True,
        )
        sys.exit(2)
    try:
        start, end = _parse_window_arg(window)
    except ValueError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(2)
    zone = tz or _local_tz_name()
    if not zone:
        click.echo(
            "error: could not determine this machine's time zone — pass --tz "
            "(e.g. --tz America/Chicago). Quiet hours never default to the "
            "daemon's UTC clock: it would fire at the wrong local hour.",
            err=True,
        )
        sys.exit(2)

    # #1563/#2146: fail loudly. A thin client's write that never reaches the
    # daemon must not print a confirmation.
    try:
        stored = set_quiet_hours(machine, start=start, end=end, tz=zone)
    except Exception as e:  # noqa: BLE001
        click.echo(
            f"error: could not set quiet hours for {machine!r}: {e}",
            err=True,
        )
        sys.exit(1)
    # Echo the RESOLVED zone: a wrong `--tz` default is otherwise invisible
    # until the machine goes quiet at the wrong hour.
    click.echo(
        f"quiet hours set: {machine} {stored.get('start')}-{stored.get('end')} "
        f"({stored.get('tz')}) — overrides any coordinator.yml block for this "
        f"machine; `coord quiet-hours {machine} --print-yaml` to make it permanent"
    )