"""Starlette HTTP layer over `AgentServer`."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

from coord import __version__, agent_update
from coord.agent import RUNNING, PENDING, AgentAssignment, AgentServer, AssignmentSpec
from coord.dist_name import DistributionNotFoundError, resolve_installed, resolve_installed_name
from coord.dist_name import pkg_spec as _dist_pkg_spec
from coord.events import stream_assignment_log
from coord.openapi import build_spec, dataclass_schema, openapi_and_docs_routes
from coord.platform_paths import venv_python as platform_venv_python

_log = logging.getLogger(__name__)


def _agent_pkg_spec() -> str:
    """What `POST /update` asks pip to install (#1237). An agent *is* the
    server half of the package, so it must reinstall itself WITH the
    `[server]` extra — a bare upgrade would, on a fresh venv, leave the
    agent without starlette/uvicorn and dead on the next restart.

    #2103/#2106: resolved via `coord.dist_name` rather than a hardcoded
    `claude-coordinator[server]` — installing the wrong name either 404s
    against PyPI or, worse, silently reinstalls a stale package.
    Deliberately NOT caught here: if the name doesn't resolve, the caller
    (`_do_update`'s existing try/except) turns that into an explicit
    `last_update.json` failure naming what was expected, instead of
    guessing.
    """
    return _dist_pkg_spec(extra="server")


#: The sibling systemd *user* units `POST /restart-services` (#2069) is
#: allowed to restart. `coord-agent` is deliberately excluded — that unit
#: restarts itself, via `/update`/`/rollback`/`/restart`, and doing it again
#: here would race those endpoints' own restart threads. Matches
#: `coord.health.checks.spawned_coord.DEFAULT_UNITS` minus `coord-agent` and
#: `coord-notify` (the latter has no deploy lane of its own to be behind on).
RESTARTABLE_SIBLING_UNITS: frozenset[str] = frozenset(
    {"coord-serve", "coord-web", "coord-drive-queue"}
)


def _venv_dir() -> Path:
    """Root of the venv `coord agent update` swaps blue/green (#1241).

    Overridable via `COORD_VENV_DIR` (tests, non-default installs);
    defaults to `~/.coord-venv` — the path `install-agent.sh` creates and
    every `deploy/coord-*.service` unit hardcodes as `ExecStart`'s venv.
    """
    override = os.environ.get("COORD_VENV_DIR")
    return Path(override) if override else Path.home() / ".coord-venv"


def _installed_version() -> str | None:
    """Return the currently-installed coordinator distribution's version.

    #1238: ``coord.__version__`` (imported once, at module-import time — see
    the module-level ``from coord import __version__`` above) and this are
    deliberately different reads. This one re-queries ``importlib.metadata``
    fresh on every call, so it reflects a ``pip install``/``pip install
    --upgrade`` that happened to site-packages *after* this process started,
    without needing a restart — exactly what ``/health`` needs to tell "the
    process hasn't restarted since the last update" apart from "the update
    never happened".

    #2103/#2106: resolves via ``coord.dist_name`` (see that module) rather
    than hardcoding the distribution name — installing under a name this
    process doesn't query used to make a fully-updated agent report
    ``None`` here, the exact false negative behind the fleet's
    most-recurring `✗ did not come back`.

    This is a *report* site, not an *act* site (contrast `_agent_pkg_spec`,
    which deliberately lets `DistributionNotFoundError` propagate): callers
    already fall back to the literal string `"unknown"` when this returns
    `None`, so a miss isn't silently swallowed end to end — but it's still
    logged here, naming what was expected, rather than disappearing into a
    bare `None` with no trace of why.
    """
    try:
        return resolve_installed().version
    except DistributionNotFoundError as exc:
        _log.warning("could not determine installed version: %s", exc)
        return None
    except Exception:
        _log.exception("unexpected error resolving installed version")
        return None


def _write_last_update(state_dir: Path, payload: dict) -> None:
    """Persist the most recent update attempt summary so /health can
    surface it after the agent restarts."""
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "last_update.json").write_text(json.dumps(payload, indent=2))
    except Exception:
        pass


def _read_last_update(state_dir: Path) -> dict | None:
    try:
        return json.loads((state_dir / "last_update.json").read_text())
    except Exception:
        return None


def _running_under_systemd() -> bool:
    """True when this process was started by systemd (in practice, a user
    unit — see ``deploy/coord-agent.service``).

    ``INVOCATION_ID`` is set by systemd for every unit invocation (since
    v232) and is the standard "am I running under systemd" signal — unlike
    checking the parent PID, it survives the process being reparented.
    """
    return bool(os.environ.get("INVOCATION_ID"))


def _systemctl_env() -> dict[str, str]:
    """``os.environ`` overlaid with a best-effort ``XDG_RUNTIME_DIR`` default
    for the ``systemctl --user`` calls below.

    ``XDG_RUNTIME_DIR`` is an XDG/systemd concept with no Windows meaning --
    and ``os.getuid()`` doesn't exist there either, so the unguarded default
    raised ``AttributeError`` at call time on win32 (#2681). None of these
    call sites are reachable on Windows anyway (there is no ``systemctl``),
    so the default is simply skipped there rather than invented.
    """
    env = dict(os.environ)
    if sys.platform != "win32":
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return env


def _restart_via_systemctl(unit: str = "coord-agent") -> bool:
    """Best-effort ``systemctl --user restart <unit>``, run from *inside*
    the unit's own process.

    #404 / #1886: ``os.execv`` self-restart does not take under systemd —
    same PID survives with stale code loaded, and nothing detected it
    (that silent survival is the concrete failure #1886 reports). Asking
    systemd itself to restart the unit is the mechanism that's known to
    work — it's the documented manual workaround, and what
    ``coord.commands.agent_ops._escalate_restart`` already does over SSH
    as a fallback from the CLI side. Doing it from inside the process
    removes the dependency on a human noticing the stall and running it
    by hand.

    Returns True once the ``systemctl`` command has been launched — NOT
    whether the restart actually completed; the caller's process is about
    to exit either way, so there is nothing left here to poll for.
    """
    # Should already be set for a process systemd itself started, but
    # setting it explicitly costs nothing and matches the SSH-driven
    # fallback in agent_ops.py, where it IS load-bearing.
    env = _systemctl_env()
    try:
        subprocess.Popen(
            ["systemctl", "--user", "restart", unit],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return False
    return True


# Unit -> the HTTP path this process should GET, on localhost, once
# `systemctl` reports the unit active, before believing the restart actually
# worked (#2095). `is-active` proves the process exists; it does not prove a
# freshly-started server has finished binding its socket, nor that it is not
# about to crash-loop moments later. coord-web's entire job is answering
# HTTP GETs — `/api/pipeline` is the exact endpoint the 2026-08-10 incident
# report used by hand to confirm the dashboard was down — so a GET is the
# honest check for it. Every other sibling unit gets no probe here, exactly
# the pre-#2095 behaviour: `is-active` alone is still what decides them.
_LIVENESS_PROBE_PATHS: dict[str, str] = {"coord-web": "/api/pipeline"}

# Unit -> (explicit-override env var, last-resort port) for the probe above.
# The override exists for setups systemd cannot answer for (a hand-started
# `coord web`, a test harness pointing the probe at an ephemeral server); the
# last-resort value is only reached when BOTH the override and systemd itself
# have nothing to say, and it is the only hardcoded port left in this path.
_LIVENESS_PORT_SOURCES: dict[str, tuple[str, str]] = {"coord-web": ("COORD_WEB_PORT", "7434")}

# `--port 7434` / `--port=7434` on a unit's ExecStart command line.
_EXEC_START_PORT_RE = re.compile(r"--port[= ](\d+)")


def _unit_listen_port(unit: str) -> str | None:
    """The ``--port`` value on *unit*'s **installed** ``ExecStart``, or None.

    #2095 review: the liveness probe below has to GET the port coord-web is
    actually listening on, and the only authority on that is the unit file
    systemd is running right now — ``deploy/coord-web.service``'s
    ``ExecStart=... --port 7434``. Asking systemd for it (rather than
    declaring the number a second time somewhere this process can read)
    keeps the probe and the listener reading ONE source: change ``--port``
    on the unit, restart it, and the probe follows on its own.

    The first cut of this instead read a ``COORD_WEB_PORT`` env var declared
    on ``coord-web.service`` — which this process, running under
    ``coord-agent.service``, can never see (systemd does not share
    ``Environment=`` across units), so the probe silently fell back to a
    hardcoded ``7434`` no matter what the unit said. Declaring the same
    number on ``coord-agent.service`` instead would make it *readable*, but
    would still be a second surface that has to agree with the first by
    hand — the exact shape epic #2096 rules out. Reading the live unit is
    the version with only one surface.

    Returns None (caller falls back) whenever systemd cannot answer: no
    systemctl on PATH, no user bus, unit not installed, or an ExecStart with
    no ``--port`` on it.
    """
    env = _systemctl_env()
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", unit, "--property=ExecStart"],
            env=env, capture_output=True, text=True, timeout=5,
        )
    except Exception:  # noqa: BLE001 - no systemctl, no bus, timeout: all "unknown"
        return None
    if result.returncode != 0:
        return None
    match = _EXEC_START_PORT_RE.search(result.stdout or "")
    return match.group(1) if match else None


def _probe_port(unit: str) -> str:
    """Which local port :func:`_probe_liveness` should GET for *unit*.

    Precedence: an explicit env override, then the live unit's own
    ``ExecStart`` (:func:`_unit_listen_port`), then the last-resort default.
    The override comes first so a deliberately-pointed probe (tests, a
    hand-started server on another port) always wins over what systemd
    happens to have installed; nothing in `deploy/` sets it, so on a real
    host the unit's own ``--port`` is what decides.
    """
    env_var, default = _LIVENESS_PORT_SOURCES.get(unit, ("", "7434"))
    override = os.environ.get(env_var) if env_var else None
    if override:
        return override
    return _unit_listen_port(unit) or default


def _probe_liveness(unit: str, *, timeout: float = 5.0) -> tuple[bool, str] | None:
    """GET ``_LIVENESS_PROBE_PATHS[unit]`` on localhost, or ``None`` if
    *unit* has no configured probe — the caller then trusts ``is-active``
    alone, same as every unit did before #2095.

    Always localhost, never the unit's configured bind host: this only ever
    runs from inside :func:`_restart_sibling_unit`, which only ever runs for
    a unit this same host's ``/restart-services`` found actually running
    HERE (see ``restart_services``'s docstring) — there is no other host to
    reach, and a phone/tailnet address would just add a second way to fail
    that has nothing to do with whether the process itself is answering.
    """
    path = _LIVENESS_PROBE_PATHS.get(unit)
    if path is None:
        return None
    port = _probe_port(unit)
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            status = resp.getcode()
    except Exception as exc:  # noqa: BLE001
        # #2095 nit: urlopen() raises HTTPError (a subclass of Exception,
        # caught above) for any response status >= 400 -- it never returns
        # normally with such a status -- so a >=500 check after this except
        # clause would be unreachable dead code. Any failure to answer,
        # connection-level or an HTTP error status, is reported the same
        # way: coord-web is active but not usably serving.
        return False, f"active, but not answering GET {path}: {type(exc).__name__}: {exc}"
    return True, f"active and answering GET {path} (HTTP {status})"


def _restart_sibling_unit(unit: str, *, timeout: float = 30.0) -> tuple[bool, str]:
    """``systemctl --user restart <unit>`` for a UNIT OTHER THAN THIS ONE,
    and wait for it to report active — and, for units with a liveness probe
    configured above, actually answering (#2069, #2095).

    Unlike :func:`_restart_via_systemctl` — used for the agent's OWN restart,
    where the caller is about to exit and there is nothing left here to poll
    for — this process stays alive throughout a sibling's restart, so it can
    and should wait rather than fire-and-forget. "The systemctl command was
    launched" is a statement about the request, not the outcome; #2052 fault
    1 is exactly what trusting that distinction cost on a self-restart, and
    there's no reason to reintroduce it here just because it's a neighbour's
    process instead of this one's.

    #2095: the restart itself used to be issued with a hard 15s
    ``subprocess.run`` timeout on a BLOCKING ``systemctl restart`` — which
    waits for the unit to fully stop before returning. A unit serving
    ``text/event-stream`` (coord-web, #700) does not stop on its own while a
    browser or the phone PWA holds an SSE connection open, so that 15s cap
    fired routinely, raised ``TimeoutExpired``, and left the unit abandoned
    mid-stop: not restarted, STOPPED — worse than never having tried, and
    reported as a failure that nonetheless printed a leading ``✓`` one layer
    up (see ``coord/commands/release.py``'s ``_roll_python``). ``--no-block``
    returns the instant the restart job is QUEUED, regardless of how long the
    unit actually takes to stop and start — the ``is-active`` poll below,
    which already existed and already had nothing to do with the abandoned
    call, is what actually decides the outcome now, exactly as its own
    docstring paragraph above argues it should.
    """
    env = _systemctl_env()
    try:
        result = subprocess.run(
            ["systemctl", "--user", "restart", "--no-block", unit],
            env=env, capture_output=True, text=True, timeout=15,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "systemctl restart failed").strip()[:300]

    deadline = time.time() + max(timeout, 0.0)
    # #2095 review: a unit whose stop was forced via `TimeoutStopSec` (added
    # by this same PR, alongside `KillMode=process`, specifically to escalate
    # to SIGKILL against a stuck SSE-holding stop) is commonly observed to
    # transiently report `ActiveState=failed` (Result: timeout) for one
    # `is-active` poll before the start half of the same `restart --no-block`
    # job takes over and settles at `active` moments later. Trusting the
    # FIRST sight of `failed` outright would misread that blip as a genuine
    # failure on precisely the unit and mechanism this issue is about — so a
    # single "failed" only counts once it is seen on two consecutive polls.
    # A unit that is actually dead reports `failed` again immediately, so
    # this still exits well before burning the rest of the deadline; it just
    # no longer trusts a single sample.
    saw_failed = False
    while True:
        try:
            probe = subprocess.run(
                ["systemctl", "--user", "is-active", unit],
                env=env, capture_output=True, text=True, timeout=5,
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        state = probe.stdout.strip()
        if state != "failed":
            # Any non-`failed` sighting — `active` included — ends the run of
            # consecutive failures the check below counts. Without this, a
            # `failed` -> `active` (up, not yet answering) -> `failed`
            # sequence would trip "two consecutive failed polls" on a single
            # fresh sighting, after the blip it was written to tolerate had
            # already resolved (#2095 review).
            saw_failed = False
        if state == "active":
            live = _probe_liveness(unit)
            if live is None:
                return True, "active"
            live_ok, live_detail = live
            if live_ok:
                return True, live_detail
            if time.time() >= deadline:
                return False, live_detail
            time.sleep(0.5)
            continue
        if state == "failed":
            if saw_failed:
                return False, "unit failed to (re)start"
            saw_failed = True
            if time.time() >= deadline:
                return False, "unit failed to (re)start"
            time.sleep(0.5)
            continue
        # (the reset for this branch happens at the top of the loop, above)
        if time.time() >= deadline:
            return False, f"still {state or 'unknown'} {timeout:.0f}s after restart"
        time.sleep(0.5)


def _default_exec_restart(argv: list[str]) -> None:
    """Restart the agent process — via systemd when running under it,
    otherwise by re-exec'ing in place.

    #404 / #1886: a bare ``os.execv`` doesn't take under systemd (same
    PID, stale code), and nothing used to detect it. Under systemd, ask
    systemd to restart the unit instead — the mechanism actually known to
    work — and let this process exit; that also re-runs `ExecStart`
    through `~/.coord-venv` fresh, which is what makes a #1241 blue/green
    swap actually take effect (see below).

    #1241: falls back to ``os.execv`` using the *current* `~/.coord-venv`
    symlink's python, re-resolved right now — NOT ``sys.executable``.
    ``sys.executable`` is the literal interpreter path baked into this
    process's own venv *slot* at the time it started (e.g.
    ``~/.coord-venv.blue/bin/python3``, from that slot's own shebang line)
    and stays pinned to that slot for the process's whole life, even after
    a blue/green swap flips the symlink onto the other slot. Re-exec'ing
    with it would silently keep running the OLD slot forever — the process
    "restarts" but never advances. Resolving through the symlink instead
    picks up whichever slot is live *right now*. Falls back to
    ``sys.executable`` when there's no such venv at all (dev/editable
    installs not using the blue/green layout), preserving the pre-#1241
    behaviour there.
    """
    if _running_under_systemd() and _restart_via_systemctl():
        os._exit(0)
    venv_python_path = platform_venv_python(_venv_dir())
    executable = str(venv_python_path) if venv_python_path.exists() else sys.executable
    os.execv(executable, [executable] + argv)


def _detect_install_mode() -> tuple[bool, str | None]:
    """Return ``(is_editable, project_path)``.

    *is_editable* is True when the package is installed in editable mode (i.e.
    ``pip install -e .``).  *project_path* is the on-disk source directory for
    editable installs, or *None* for regular (site-packages) installs.

    #2103/#2106: ``pip show`` needs an exact distribution name, so this
    resolves which name is actually installed (see ``coord.dist_name``)
    first rather than hardcoding one — asking `pip show` for a name
    nothing is installed under always reports "not editable", which would
    misreport a real editable install.
    """
    dist_name = resolve_installed_name()
    if dist_name is None:
        return False, None
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "show", dist_name],
            capture_output=True,
            text=True,
            timeout=15,
        )
        for line in result.stdout.splitlines():
            if line.startswith("Editable project location:"):
                path = line.split(":", 1)[1].strip()
                return True, path
        return False, None
    except Exception:
        return False, None


# ── Idle self-restart (#2139) ────────────────────────────────────────────
#
# #1241 made `coord agent update` atomic and reversible but left "when does
# the running process actually pick up a staged swap" to an external actor
# that has to find a fleet-wide quiescent window — and a working overnight
# queue keeps a host busy essentially always, so that window stopped
# opening in practice (18 consecutive deferred `coord release propagate`
# attempts on 2026-08-11). This section is the missing trigger: each agent
# watches its OWN active-assignment count and, the moment it — and only
# it — has none, re-execs onto whatever slot `~/.coord-venv` already
# resolves to. No board read, no fleet-wide window, no coordination with
# any other host: the whole point is that this is a decision one process
# can make about itself.

#: How often the watcher below re-checks "am I idle, and is a newer slot
#: staged". Overridable for tests — production leaves this at its default.
IDLE_RESTART_POLL_SECONDS = float(os.environ.get("COORD_AGENT_IDLE_RESTART_POLL") or 5.0)

#: How long zero active (RUNNING/PENDING) assignments must hold
#: *continuously* before the watcher actually restarts — the debounce the
#: design calls for: a host that clears one leg and picks up the next a
#: moment later must not restart out from under it, and this must never
#: preempt a dispatch that has already been accepted (a PENDING assignment
#: counts as active, same as RUNNING). Overridable for tests.
IDLE_RESTART_DEBOUNCE_SECONDS = float(os.environ.get("COORD_AGENT_IDLE_RESTART_DEBOUNCE") or 20.0)


def _daemon_runs_here() -> bool:
    """True if `coord-serve` — the daemon every `coord` caller talks to —
    is also a systemd unit on THIS host, or if that can't be determined.

    The daemon-first invariant (`commands/release.py:640`'s documented
    405: a caller must never reach an endpoint its daemon predates) is what
    keeps a self-restarting agent from overtaking the daemon it calls. On a
    host that does NOT run coord-serve, that invariant already holds
    structurally, with no extra check needed here: `release_propagate.
    plan_lanes` puts the daemon host's python lane first, and
    `commands/release.py`'s `daemon_python_failed` gate refuses to roll ANY
    other host until the daemon is confirmed on the target version — so by
    the time a non-daemon host's OWN slot ever advances, the daemon has
    already gone. The one case that invariant does NOT cover on its own is
    a host that runs coord-agent and coord-serve side by side, sharing one
    `~/.coord-venv`: `/update` there swaps the shared venv and restarts
    coord-agent, but coord-serve keeps running the old slot until a
    separate `/restart-services` call catches it up (#2069) — a coord-agent
    that self-restarted in between would be a newer caller than the
    coord-serve sitting right next to it. So: restrict this watcher to the
    agent lane by simply never firing on a host where coord-serve is
    present, and leave that host on the existing ordered
    `/update`+`/restart-services` path (`commands/release.py`'s
    `_roll_python`) instead. Purely local — `running_unit_pids` is the same
    systemd query `/restart-services` already makes against THIS host, no
    network call leaves it.

    Errs toward "yes, treat as the daemon host" (i.e. stay quiet) whenever
    the answer can't be determined at all (no systemd here — a dev box, a
    container, a thin client) — a host this can't confirm is safe on is one
    the existing ordered path should keep handling.
    """
    if not _running_under_systemd():
        return True
    try:
        from coord.health.checks.spawned_coord import running_unit_pids  # noqa: PLC0415

        return bool(running_unit_pids(("coord-serve",)))
    except Exception:  # noqa: BLE001
        return True


def _host_has_live_interactive_session() -> bool:
    """True when a ``coord-<assignment_id>`` tmux session (an interactive
    Test/Review/Merge/Work pane) is alive anywhere on this host.

    Load-bearing guard for :func:`_idle_restart_target`, mirroring
    ``AgentServer.clean_worktrees``'s tmux guard (#1295) — see that
    method's docstring, and the hourly-worktree-sweep incident it cites,
    for why this exists: an interactive session can outlive its
    assignment record. The dispatch subprocess backing a Test/Review/
    Merge/Work pane can finish (moving the assignment to a terminal
    status in ``self._assignments``) while the operator is still sitting
    in the pane. ``self._assignments[...].status`` alone therefore is
    NOT a reliable "is this host busy" signal — exactly the gap that
    incident exposed. Consulting tmux directly, the same way
    ``clean_worktrees`` does before ever touching a worktree, is ground
    truth: it is only "up" for as long as the session — and therefore the
    operator's claim on this host — actually exists.

    Deferred import for the same reason ``AgentServer._tmux_session_alive``
    defers it: keep ``coord.interactive`` (which pulls in curses/tty
    helpers) out of the agent process's top-level import graph.
    :func:`~coord.interactive.list_coord_tmux_sessions` already swallows
    "tmux not installed"/"no server running"/subprocess errors internally
    and returns ``[]`` for all of them, so this only needs to guard the
    import itself. Any exception here errs toward "busy" — i.e. skip this
    restart cycle and re-evaluate next tick — the same fail-safe direction
    :func:`_daemon_runs_here` takes when it can't determine an answer.
    """
    try:
        from coord.interactive import list_coord_tmux_sessions  # noqa: PLC0415

        return bool(list_coord_tmux_sessions())
    except Exception:  # noqa: BLE001
        return True


def _idle_restart_target(server: "AgentServer", venv_dir: Path) -> Path | None:
    """Return the slot the idle watcher should restart onto right now, or
    ``None`` if it should not act.

    A small, pure(ish) decision function — every branch below is a single
    fact read fresh (no state of its own), so the debounce/threading
    concerns in :class:`_IdleRestartWatcher` stay entirely in that class and
    this stays trivially unit-testable without spinning a real thread.

    Ordered cheapest-first on purpose: the first three checks are a path
    comparison or an in-memory lock/iteration; :func:`_host_has_live_interactive_session`
    and :func:`_daemon_runs_here` both shell out (``tmux list-panes``,
    ``systemctl --user show``) and only run once the cheap checks have
    already established there's an actual swap waiting. Note this still
    means both subprocess calls run on *every* poll tick (default every 5s)
    for as long as the agent is idle with a different slot staged — not
    merely on the one tick that ends up restarting — since neither check
    has a way to know in advance which tick that will be.
    """
    live = agent_update.current_slot(venv_dir)
    if live is None:
        # Not a migrated blue/green venv (or it doesn't exist) — nothing to
        # restart onto.
        return None
    running = agent_update.running_slot(venv_dir)
    if running is None or running == live:
        # Either this isn't a blue/green interpreter at all (dev/editable),
        # or it's already running the slot the symlink points at — no swap
        # is waiting.
        return None
    with server._lock:
        active_count = sum(
            1 for a in server._assignments.values() if a.status in (PENDING, RUNNING)
        )
    if active_count:
        return None
    if _host_has_live_interactive_session():
        # #2139 blocking review fix: an interactive pane is not an
        # assignment (see docstring above) — a live one must veto a
        # restart exactly like a RUNNING/PENDING assignment does, however
        # long its backing assignment record has already gone terminal.
        return None
    if _daemon_runs_here():
        return None
    return live


class _IdleRestartWatcher:
    """Background poll loop implementing #2139's trigger.

    Runs for the life of the agent process, polling
    :func:`_idle_restart_target` every ``poll_seconds``. Once it returns a
    non-``None`` slot for ``debounce_seconds`` *continuously* — not merely
    on one poll — this restarts the process onto it via ``exec_restart``,
    the same callable `/update`/`/restart`/`/rollback` already use (so
    tests inject the same no-op/mock they always have, and production gets
    the same systemd-aware `_default_exec_restart`).

    Any poll that finds the agent busy, or the slot unchanged, resets the
    debounce clock to "not idle yet" — there is no partial credit for a
    host that flickers between busy and idle.
    """

    def __init__(
        self,
        server: "AgentServer",
        *,
        venv_dir: Path,
        exec_restart: "Callable[[list[str]], None]",
        poll_seconds: float | None = None,
        debounce_seconds: float | None = None,
    ) -> None:
        self._server = server
        self._venv_dir = venv_dir
        self._exec_restart = exec_restart
        self._poll_seconds = (
            IDLE_RESTART_POLL_SECONDS if poll_seconds is None else poll_seconds
        )
        self._debounce_seconds = (
            IDLE_RESTART_DEBOUNCE_SECONDS if debounce_seconds is None else debounce_seconds
        )
        self._stop = threading.Event()
        self._idle_since: float | None = None
        self._thread = threading.Thread(
            target=self._run, name="agent-idle-restart", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """Stop polling. Does not interrupt a restart already in flight —
        by the time that happens ``exec_restart`` has already replaced (or
        is about to exit) this process, so there is nothing left to stop."""
        self._stop.set()

    def _run(self) -> None:
        # `Event.wait` doubles as the sleep AND the stop signal, so `stop()`
        # takes effect within one tick instead of up to `poll_seconds` late.
        while not self._stop.wait(self._poll_seconds):
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                _log.exception("idle self-restart watcher tick failed")

    def _tick(self) -> None:
        target = _idle_restart_target(self._server, self._venv_dir)
        if target is None:
            self._idle_since = None
            return
        now = time.time()
        if self._idle_since is None:
            self._idle_since = now
        if now - self._idle_since < self._debounce_seconds:
            return
        self._restart_onto(target)

    def _restart_onto(self, target: Path) -> None:
        version_before = _installed_version() or "unknown"
        # Re-confirm the target slot is actually a good one right before
        # acting — it was smoke-checked once already, back when `/update`
        # swapped it in, but re-checking here costs one subprocess call and
        # means a slot that somehow went bad after the swap (disk issue,
        # someone hand-editing `~/.coord-venv.*`) is refused instead of
        # exec'd into blindly.
        ok, new_version, log = agent_update._smoke_check(target, target_version=None)
        if not ok:
            _log.error(
                "idle self-restart: smoke check failed for staged slot %s — "
                "not restarting; will retry next debounce window.\n%s",
                target, log,
            )
            # #2139 review fix: a slot that fails the re-smoke-check must be
            # visible somewhere an operator actually looks — `/health` —
            # not just the raw agent log. Without this, a staged slot that
            # keeps failing here (bad disk, someone hand-editing
            # `~/.coord-venv.*`) restart-loops through debounce windows
            # forever with zero signal outside `journalctl`/the agent log.
            now_ts = time.time()
            _write_last_update(
                self._server.state_dir,
                {
                    "mode": "idle self-restart (#2139)",
                    "started_at": now_ts,
                    "finished_at": now_ts,
                    "version_before": version_before,
                    "version_after": version_before,
                    "target_version": None,
                    "result": "failed",
                    "error": f"re-smoke-check failed for staged slot {target}: {log}",
                    "log_excerpt": "\n".join((log or "").splitlines()[-20:]),
                },
            )
            # Don't retry on every poll tick against a slot that's already
            # known bad this round — wait out a fresh debounce window first.
            self._idle_since = None
            return
        now_ts = time.time()
        payload = {
            "mode": "idle self-restart (#2139)",
            "started_at": now_ts,
            "finished_at": now_ts,
            "version_before": version_before,
            "version_after": new_version or "unknown",
            "target_version": None,
            "result": "upgraded",
            "error": None,
            "log_excerpt": (
                f"agent idle for >= {self._debounce_seconds:.0f}s with a "
                f"staged slot ({target}) already resolved by ~/.coord-venv "
                "— self-restarting onto it (#2139)"
            ),
        }
        _write_last_update(self._server.state_dir, payload)
        self._idle_since = None
        # #2139 non-blocking review note: `update()`'s own background
        # thread (`_do_update` below) can also call `exec_restart` directly
        # when a swap completes with zero active assignments at that
        # instant — this watcher polls the same "idle + newer slot staged"
        # condition concurrently and could fire around the same moment.
        # Harmless in practice: both paths only ever exec/restart onto the
        # slot `~/.coord-venv` already resolves to, and `os.execv`/
        # `systemctl restart` make a second call against an already-
        # replaced process a no-op (there's nothing left to race with by
        # the time it would run).
        self._exec_restart(list(sys.argv))


def _path_param(name: str, description: str = "") -> dict:
    return {
        "name": name,
        "in": "path",
        "required": True,
        "schema": {"type": "string"},
        "description": description,
    }


def _openapi_spec() -> dict:
    """#757: the agent's OpenAPI 3 document.

    ``POST /assign`` is fully specified (request = ``AssignmentSpec``,
    response = ``AgentAssignment``, both introspected via
    :func:`coord.openapi.dataclass_schema`); the remaining routes carry a
    summary/description and path-param shapes but a loosely-typed body, since
    they return small ad-hoc dicts rather than a dataclass.
    """
    components: dict = {}
    assign_request = dataclass_schema(AssignmentSpec, components)
    assign_response = dataclass_schema(AgentAssignment, components)
    paths = {
        "/health": {
            "get": {
                "summary": "Agent health + version",
                "responses": {"200": {"description": "OK"}},
            }
        },
        "/status": {
            "get": {
                "summary": "List this agent's assignments (active + completed)",
                "responses": {"200": {"description": "OK"}},
            }
        },
        "/repos": {
            "get": {
                "summary": "Repos this agent can dispatch work into",
                "responses": {"200": {"description": "OK"}},
            }
        },
        "/assign": {
            "post": {
                "summary": "Dispatch a new assignment (spawns `claude -p`)",
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": assign_request}},
                },
                "responses": {
                    "202": {
                        "description": "Accepted",
                        "content": {"application/json": {"schema": assign_response}},
                    },
                    "400": {"description": "Bad assignment payload"},
                },
            }
        },
        "/cancel/{id}": {
            "post": {
                "summary": "Cancel a running/pending assignment",
                "description": (
                    "#1567: by default, any uncommitted worker changes are "
                    "committed locally but NOT pushed anywhere — the "
                    "worker's remote branch is left unchanged. Pass "
                    "?rescue=1 to push the WIP commit to a disposable "
                    "rescue/<id> ref instead (the worker's own branch is "
                    "still never touched)."
                ),
                "parameters": [
                    _path_param("id", "assignment id"),
                    {
                        "name": "rescue",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "boolean", "default": False},
                        "description": (
                            "Push the WIP commit to rescue/<id> instead of "
                            "leaving it local-only."
                        ),
                    },
                ],
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": assign_response}},
                    },
                    "404": {"description": "Unknown assignment"},
                },
            }
        },
        "/inject/{id}": {
            "post": {
                "summary": "Inject a new user message into a running worker's session",
                "parameters": [_path_param("id", "assignment id")],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {"text": {"type": "string"}},
                                "required": ["text"],
                            }
                        }
                    },
                },
                "responses": {
                    "202": {"description": "Delivered"},
                    "404": {"description": "Unknown assignment"},
                    "409": {"description": "Worker not running"},
                    "410": {"description": "Worker stdin already closed"},
                },
            }
        },
        "/logs/{id}": {
            "get": {
                "summary": "Read (a tail of) the worker's log file",
                "parameters": [
                    _path_param("id", "assignment id"),
                    {
                        "name": "since",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "integer"},
                        "description": "byte offset to read from",
                    },
                ],
                "responses": {
                    "200": {"description": "OK"},
                    "404": {"description": "Unknown assignment or no log file"},
                },
            }
        },
        "/stream/{id}": {
            "get": {
                "summary": "Server-sent-event stream of the worker's log",
                "parameters": [_path_param("id", "assignment id")],
                "responses": {"200": {"description": "text/event-stream"}},
            }
        },
        "/update": {
            "post": {
                "summary": "Upgrade the installed package and restart the agent process",
                "responses": {"202": {"description": "Updating"}},
            }
        },
        "/deploy-units": {
            "post": {
                "summary": (
                    "Install this host's systemd user units from the units "
                    "packaged in the running release (#1831/#1835). Restarts "
                    "nothing — daemon-reload only."
                ),
                "responses": {
                    "200": {"description": "Units deployed (or nothing to do)"},
                    "500": {"description": "A unit could not be written, or daemon-reload failed"},
                },
            }
        },
        "/restart-services": {
            "post": {
                "summary": (
                    "Restart whichever of coord-serve/coord-web/coord-drive-queue "
                    "are actually running on this host (#2069) — the rest of a "
                    "python-lane roll that /update itself only does for "
                    "coord-agent. Call AFTER /update on the same host."
                ),
                "requestBody": {
                    "required": False,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "units": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    }
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "Restart attempted for every running sibling unit"},
                    "400": {"description": "\"units\" named something not restartable here"},
                },
            }
        },
        "/rollback": {
            "post": {
                "summary": "Roll back to the previous blue/green venv generation and restart",
                "responses": {
                    "202": {"description": "Rolling back"},
                    "404": {"description": "No previous generation to roll back to"},
                    "409": {"description": "Live sessions running; pass force=true"},
                },
            }
        },
        "/restart": {
            "post": {
                "summary": "Gracefully restart the agent process",
                "requestBody": {
                    "required": False,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {"cancel_timeout": {"type": "number"}},
                            }
                        }
                    },
                },
                "responses": {"202": {"description": "Restarting"}},
            }
        },
        "/worktree-clean": {
            "post": {
                "summary": "Remove stale git worktrees managed by this agent",
                "requestBody": {
                    "required": False,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "recent_secs": {"type": "number"},
                                    "protect": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": (
                                            "#1295: assignment IDs the caller "
                                            "considers non-terminal; the agent "
                                            "keeps their worktrees regardless "
                                            "of its own state.  Optional — an "
                                            "older agent without this field "
                                            "behaves exactly as before."
                                        ),
                                    },
                                },
                            }
                        }
                    },
                },
                "responses": {"200": {"description": "OK"}},
            }
        },
        "/graph-fix": {
            "post": {
                "summary": (
                    "Build a missing graphify graph and set core.hooksPath for "
                    "one repo on this machine (#2237)"
                ),
                "description": (
                    "The machine-local half of graphify onboarding only: "
                    "`graphify update .` when no graph exists here, plus "
                    "`git config core.hooksPath .githooks`. Idempotent. "
                    "Refuses (200, `refused` set) when the repo does not ship "
                    "`.githooks/post-checkout` — porting those is a versioned "
                    "change and is never automated."
                ),
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["repo"],
                                "properties": {
                                    "repo": {"type": "string"},
                                    "timeout": {
                                        "type": "number",
                                        "description": (
                                            "seconds for `graphify update .`; a "
                                            "first build on a large repo is "
                                            "minutes, so this defaults to 600."
                                        ),
                                    },
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "OK (including a refusal)"},
                    "400": {"description": "repo missing from the body"},
                },
            }
        },
        "/drive-queue-reconcile": {
            "post": {
                "summary": (
                    "Run `coord drive-queue tick --reconcile-only` on this "
                    "machine (#2373)"
                ),
                "description": (
                    "The per-machine self-heal `coord release propagate`'s "
                    "drain-deadline escalation calls before it escalates "
                    "loudly: only the host that actually launched a "
                    "`running` drive-queue entry can resolve the #1870 "
                    "cross-host liveness guard for it. Fleet-queue-wide, "
                    "not scoped to a repo."
                ),
                "requestBody": {
                    "required": False,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "timeout": {
                                        "type": "number",
                                        "description": (
                                            "seconds for the tick subprocess; "
                                            "defaults to 120."
                                        ),
                                    },
                                },
                            }
                        }
                    },
                },
                "responses": {"200": {"description": "OK (including a failed tick)"}},
            }
        },
        "/artifact/{repo}/{branch}": {
            "get": {
                "summary": "Manifest of stashed build artifacts for a (repo, branch) pair",
                "parameters": [
                    _path_param("repo", "repo name"),
                    _path_param("branch", "sanitized branch name"),
                ],
                "responses": {
                    "200": {"description": "OK"},
                    "404": {"description": "No artifacts for this repo/branch"},
                },
            }
        },
        "/metrics": {
            "get": {
                "summary": "CPU + memory snapshot for the agent machine",
                "responses": {
                    "200": {"description": "OK"},
                    "503": {"description": "psutil not installed"},
                },
            }
        },
    }
    return build_spec(
        title="coord agent",
        version=__version__,
        description="Per-machine agent server: spawns and tracks `claude -p` workers.",
        paths=paths,
        components=components,
    )


def build_app(
    server: AgentServer,
    *,
    exec_restart: Callable[[list[str]], None] | None = None,
    idle_restart: bool = False,
) -> Starlette:
    """Build the Starlette app bound to a specific AgentServer instance.

    Parameters
    ----------
    server:
        The ``AgentServer`` instance to bind routes to.
    exec_restart:
        Callable invoked to replace the current process when ``/update`` or
        ``/restart`` completes.  Receives ``sys.argv`` as its argument.
        Defaults to :func:`_default_exec_restart` (calls ``os.execv``).
        Tests may inject a no-op or a mock to prevent the test process from
        being replaced.
    idle_restart:
        Start the #2139 idle self-restart watcher (:class:`_IdleRestartWatcher`)
        as a background daemon thread bound to this app's ``server`` and
        ``exec_restart``.  Defaults to ``False`` so building an app for a
        short-lived test (or any embedding that manages its own process
        lifecycle) never spins up a stray background thread; the real
        `coord agent` entrypoint (``coord.commands.agent_ops.
        _start_agent_server``) passes ``True``.  The watcher instance, when
        started, is reachable at ``app.state.idle_restart_watcher`` — mainly
        so tests can ``.stop()`` it deterministically instead of relying on
        it being a daemon thread.
    """
    if exec_restart is None:
        exec_restart = _default_exec_restart

    async def health(request: Request) -> JSONResponse:
        # server.health() can shell out to probe tool versions (#1570 B,
        # via AgentServer._cached_tool_versions -> probe_all) — real
        # subprocess.run calls with a per-tool timeout. Running that inline
        # would block this event loop (and every in-flight /assign) for up
        # to the probe timeout on a slow/hung tool. Cache TTL means this
        # only bites the first /health after a restart or every few
        # minutes, but push it off-loop regardless.
        data = await asyncio.to_thread(server.health)
        # #1886 Path B: `version` is bound at process import time (see the
        # module-level `from coord import __version__` above) and never
        # changes for the life of this process — it is the *running*,
        # loaded-module version. `installed_version` is a fresh disk read
        # (see `_installed_version` above) and changes the instant `pip`
        # writes to site-packages, regardless of whether this process has
        # restarted to pick it up. Exposing both — instead of just one,
        # ambiguous "version" — lets a caller (`coord agent update`'s poll
        # loop, `coord status`) detect a process that never restarted
        # after an update purely from /health, without inferring it from
        # liveness or PID.
        data["version"] = __version__
        data["installed_version"] = _installed_version()
        # Surface the most recent /update attempt so the CLI can show
        # "0.3.0 → 0.4.0" or "no_change (0.3.0)" or "failed: <error>".
        last = _read_last_update(server.state_dir)
        if last is not None:
            data["last_update"] = last
        return JSONResponse(data)

    async def status(request: Request) -> JSONResponse:
        data = server.list_assignments()
        data["version"] = __version__
        return JSONResponse(data)

    async def repos(request: Request) -> JSONResponse:
        return JSONResponse(server.list_repos())

    async def assign(request: Request) -> JSONResponse:
        # #3145 audit note: `server.assign()` below runs inline on the loop
        # and does do some blocking subprocess work (`_setup_worktree`'s
        # `git worktree add`, etc.) — the same class of bug just fixed for
        # `worktree_clean`. Left alone here deliberately: it's bounded to
        # ordinary git/process latency (not the 600s-class `build_command`
        # path this issue is about), and changing dispatch to run off-loop
        # is a separate, larger question. Worth a tracked follow-up rather
        # than another silent pass if this handler is revisited.
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        try:
            spec = AssignmentSpec(**body)
        except TypeError as e:
            return JSONResponse({"error": f"bad assignment payload: {e}"}, status_code=400)
        try:
            assignment = server.assign(spec)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse(assignment.to_dict(), status_code=202)

    async def cancel(request: Request) -> JSONResponse:
        # #3145 audit note: same as `assign` above — `server.cancel()`
        # below does blocking process wait/kill work inline on the loop.
        # Left alone for the same reason (bounded to ordinary
        # process-signal latency; out of scope for this issue).
        assignment_id = request.path_params["id"]
        # #1567: ?rescue=1 opts into pushing the WIP commit to a disposable
        # rescue/<id> ref. Default (no query param, or any falsy value) is
        # to commit locally only and leave the remote branch untouched.
        rescue_param = request.query_params.get("rescue", "")
        rescue = rescue_param.strip().lower() in ("1", "true", "yes")
        # ``push_mode``, when given, overrides the rescue-derived default —
        # an internal-only escape hatch for callers that are not an operator
        # `coord stop` (e.g. `coord resume-stuck`, which cancels a stuck
        # worker but immediately dispatches a continuation onto the SAME
        # branch and needs the WIP pushed there, not withheld or diverted to
        # a rescue ref — see coord/commands/plan_followup.py::resume_stuck).
        push_mode = request.query_params.get("push_mode") or None
        try:
            assignment = server.cancel(
                assignment_id, rescue=rescue, push_mode=push_mode
            )
        except KeyError:
            return JSONResponse({"error": f"unknown assignment {assignment_id}"}, status_code=404)
        return JSONResponse(assignment.to_dict())

    async def inject(request: Request) -> JSONResponse:
        """Inject a new user message into a running worker's session.

        Body (JSON): ``{"text": "..."}``.  Worker picks up the message at
        its next turn boundary.  Returns 404 if the assignment isn't on
        this agent, 409 if it isn't running, 410 if the worker's stdin
        is already closed.
        """
        assignment_id = request.path_params["id"]
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str) or not text.strip():
            return JSONResponse(
                {"error": "body must be {\"text\": \"<non-empty string>\"}"},
                status_code=400,
            )
        try:
            server.inject_message(assignment_id, text)
        except KeyError:
            return JSONResponse(
                {"error": f"unknown assignment {assignment_id}"}, status_code=404
            )
        except RuntimeError as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        except BrokenPipeError as e:
            return JSONResponse({"error": str(e)}, status_code=410)
        return JSONResponse({"status": "delivered"}, status_code=202)

    async def logs(request: Request) -> Response:
        assignment_id = request.path_params["id"]
        assignment = server.get(assignment_id)
        status = assignment.status if assignment is not None else "unknown"
        if assignment is not None and assignment.log_path is not None:
            log_path = Path(assignment.log_path)
        else:
            # #2541: the human-attended ssh+tmux interactive launcher
            # (`coord assign --interactive ...`) never goes through this
            # agent's `/assign` — it SSHes in directly from the
            # coordinator and drives `claude` inside a tmux session, so
            # `server.get()` has no record of it. Those sessions still
            # persist a best-effort pane-capture snapshot at the SAME
            # conventional per-assignment path
            # (`coord.interactive._persist_interactive_pane_log`) so a
            # launch failure is diagnosable via `coord log <id>` without a
            # manual SSH repro. Fall back to that path directly rather
            # than 404ing just because this agent never tracked the
            # assignment in memory.
            # assignment_id is attacker-controllable (raw path param) and,
            # unlike `assignment.log_path` above, is interpolated directly
            # into a filesystem path here — reject anything that isn't the
            # plain `[a-zA-Z0-9_-]+` shape every assignment_id generator in
            # this codebase actually produces, so a `../` can't escape
            # `logs/`.
            if not re.fullmatch(r"[A-Za-z0-9_-]+", assignment_id):
                return JSONResponse(
                    {"error": f"invalid assignment id {assignment_id!r}"},
                    status_code=400,
                )

            # `server.log_dir` (not the module-level `DEFAULT_STATE_DIR`
            # default) so this honours a non-default `state_dir` exactly
            # like every other lookup in this handler would if the
            # assignment WERE tracked — production agents never override
            # it, but this keeps the fallback truthful to what this
            # specific server instance is actually configured with.
            log_path = server.log_dir / f"{assignment_id}.log"
        if not log_path.exists():
            return JSONResponse(
                {"error": f"no log file for assignment {assignment_id}"}, status_code=404
            )

        since_raw = request.query_params.get("since", "0")
        try:
            since = max(0, int(since_raw))
        except ValueError:
            return JSONResponse(
                {"error": f"invalid since value: {since_raw!r}"}, status_code=400
            )

        with open(log_path, "rb") as f:
            f.seek(since)
            body = f.read()
        total_size = log_path.stat().st_size
        headers = {
            "X-Coord-Log-Total": str(total_size),
            "X-Coord-Log-Status": status,
        }
        return PlainTextResponse(body.decode("utf-8", errors="replace"), headers=headers)

    async def stream(request: Request) -> Response:
        assignment_id = request.path_params["id"]
        assignment = server.get(assignment_id)
        if assignment is None or assignment.log_path is None:
            return JSONResponse(
                {"error": f"unknown assignment {assignment_id}"}, status_code=404
            )
        log_path = Path(assignment.log_path)

        last_event_id = request.headers.get("last-event-id")
        if last_event_id is not None:
            try:
                start_offset = max(0, int(last_event_id))
            except ValueError:
                start_offset = 0
        else:
            try:
                start_offset = max(0, int(request.query_params.get("since", "0")))
            except ValueError:
                start_offset = 0

        def is_active() -> bool:
            current = server.get(assignment_id)
            return current is not None and current.status in (PENDING, RUNNING)

        gen = stream_assignment_log(
            log_path,
            is_active=is_active,
            request=request,
            start_offset=start_offset,
        )
        return StreamingResponse(
            gen,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    async def update(request: Request) -> JSONResponse:
        """Atomically install the target version, restarting the agent onto
        it now if idle, or staging it for the idle self-restart watcher to
        apply once it is (#1241, #2139).

        Installs into a *fresh* venv slot next to the live one, smoke-checks
        it, then atomically flips ``~/.coord-venv`` onto it — see
        :mod:`coord.agent_update` for why an in-place ``pip install
        --upgrade`` isn't safe (it can leave a concurrent ``coord``
        invocation observing a half-written ``site-packages``). Runs in a
        daemon-less background thread so the HTTP response reaches the
        caller before this process is potentially replaced.

        Refuses outright — HTTP 409, nothing touched, no swap attempted —
        only for an **editable install** (``pip install -e .``):
        ``~/.coord-venv`` must stay a PyPI install (mirrors
        ``coord.health.checks.agent_install``'s ``agent_venv`` check). An
        editable checkout is reported as drift, never silently ``git
        pull``ed — the operator switches it back by hand (see
        ``docs/AGENT_OPERATIONS.md``'s editable → PyPI section).

        #2139: **live sessions no longer refuse the swap.** The swap itself
        never touches a running process — a live worker's own interpreter
        stays pinned to whichever slot it started from regardless of where
        the symlink points (see :mod:`coord.agent_update`'s module
        docstring) — so there is nothing unsafe about it landing while this
        agent has active (RUNNING/PENDING) assignments. What DOES still need
        gating is the *restart*, because that's what actually kills
        in-flight workers. So: with no active assignments, or with
        ``{"force": true}``, this restarts immediately after a successful
        swap, exactly as before. With active assignments and no ``force``,
        the swap still lands (``result: "staged"`` in ``last_update``,
        surfaced via ``/health``) and the agent's own idle self-restart
        watcher applies it the moment this agent's assignment count reaches
        — and holds at — zero, with no operator action and no need for a
        fleet-wide quiescent window.

        Request body (JSON, optional)::

            {"target_version": "0.4.85", "force": false}

        #1568: when the caller (``coord agent update``) knows exactly which
        release it's asking for, it passes ``target_version``, pinning the
        pip install to that exact version rather than a bare ``--upgrade``
        so a stale PyPI index/cache produces a loud pip failure ("no
        matching distribution") instead of a silent no-op. ``target_version``
        is echoed back in ``last_update`` so ``/health`` lets the caller
        verify the upgrade actually landed.
        """
        is_editable, project_path = _detect_install_mode()
        if is_editable:
            payload = {
                "mode": "editable (refused)",
                "started_at": time.time(),
                "finished_at": time.time(),
                "result": "refused",
                "error": (
                    f"editable install detected at {project_path!r} — "
                    "refusing to touch it automatically (#1241: "
                    "~/.coord-venv must stay a PyPI install). Switch it "
                    "back by hand — see docs/AGENT_OPERATIONS.md's "
                    "editable → PyPI section — then retry."
                ),
            }
            _write_last_update(server.state_dir, payload)
            return JSONResponse(payload, status_code=409)

        body: dict = {}
        try:
            body = await request.json()
        except Exception:
            pass
        if not isinstance(body, dict):
            body = {}
        target_version = body.get("target_version") or None
        force = bool(body.get("force"))

        # #2121 item 2: every install must name who asked for it. A caller
        # that knows (`coord release propagate`, `coord agent update`) says
        # so explicitly; anything else is attributed to the socket it
        # arrived on, which is still a fact and still more than the
        # 2026-08-11 upgrade left behind. Never silently "unattributed"
        # here — that value is reserved for an in-process call that named
        # nobody at all.
        initiator = body.get("initiator") or None
        if not isinstance(initiator, str) or not initiator.strip():
            peer = request.client.host if request.client else "unknown peer"
            agent_hdr = request.headers.get("user-agent") or "no user-agent"
            initiator = f"POST /update from {peer} ({agent_hdr})"

        mode = "pip install (blue/green)"

        # Capture argv now — exec_restart replaces the process later.
        saved_argv = list(sys.argv)
        state_dir = server.state_dir

        def _do_update() -> None:
            version_before = _installed_version() or "unknown"
            started_at = time.time()
            payload: dict = {
                "mode": mode,
                "started_at": started_at,
                "version_before": version_before,
                "version_after": version_before,
                "target_version": target_version,
                "result": "failed",
                "error": None,
                "log_excerpt": "",
            }
            try:
                venv_dir = _venv_dir()
                result = agent_update.perform_update(
                    venv_dir,
                    _agent_pkg_spec(),
                    target_version=target_version,
                    initiator=initiator,
                )
                payload["finished_at"] = time.time()
                # Persist the full venv/pip/smoke-check transcript to a log
                # file so the user can read it after the agent restarts.
                log_path = state_dir / "last_update.log"
                try:
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    log_path.write_text(
                        f"# mode: {mode}\n"
                        f"# venv_dir: {venv_dir}\n"
                        f"# ok: {result.ok}  swapped: {result.swapped}\n"
                        f"# slot: {result.slot}  previous_slot: {result.previous_slot}\n\n"
                        f"{result.log}\n"
                    )
                except Exception:  # noqa: BLE001
                    pass
                # Keep a short excerpt inline so it appears in /health.
                tail = (result.log or "").splitlines()
                payload["log_excerpt"] = "\n".join(tail[-20:])

                if not result.ok:
                    payload["error"] = result.error or (
                        "blue/green update failed; see "
                        "~/.coord/last_update.log on this machine"
                    )
                    _write_last_update(state_dir, payload)
                    return

                # #1241: prefer the version the smoke check already read
                # straight from the new slot (deterministic, no reliance on
                # this process's own site-packages resolution having
                # noticed the symlink flip yet) — fall back to a fresh
                # in-process read only if that's somehow missing.
                version_after = result.new_version or _installed_version() or "unknown"
                payload["version_after"] = version_after
                if version_after == version_before:
                    # Swap "succeeded" but landed on the same version —
                    # shouldn't happen (the smoke check already verified
                    # target_version when one was given) but don't restart
                    # into a no-op.
                    payload["result"] = "no_change"
                    payload["error"] = (
                        f"swap completed but resolved to {version_after} "
                        "(same as before) — unexpected for a successful "
                        "blue/green update"
                    )
                    _write_last_update(state_dir, payload)
                    return

                # #2139: the busy check moves HERE — right before the
                # restart decision, not up front before the swap even
                # started. Re-read fresh: `perform_update` above can take
                # tens of seconds (venv creation, pip, smoke check), so the
                # count at the top of this request is stale by now.
                with server._lock:
                    still_active = sum(
                        1
                        for a in server._assignments.values()
                        if a.status in (PENDING, RUNNING)
                    )
                if still_active and not force:
                    payload["result"] = "staged"
                    payload["error"] = (
                        f"swapped to v{version_after}; {still_active} active "
                        "assignment(s) still running — restart deferred to "
                        "this agent's idle self-restart watcher (#2139); "
                        'pass {"force": true} to restart immediately instead'
                    )
                    _write_last_update(state_dir, payload)
                    return

                payload["result"] = "upgraded"
                _write_last_update(state_dir, payload)

                # Brief pause so the HTTP response reaches the client first.
                #
                # #2139 non-blocking review note: this is one of two paths
                # that can call `exec_restart` — the other is
                # `_IdleRestartWatcher._restart_onto`, polling the same
                # "idle + newer slot staged" condition concurrently. See
                # that method's comment for why a near-simultaneous double
                # fire here is harmless.
                time.sleep(0.5)
                exec_restart(saved_argv)
            except Exception as e:
                payload["error"] = f"{type(e).__name__}: {e}"
                _write_last_update(state_dir, payload)

        threading.Thread(target=_do_update, daemon=False, name="agent-update").start()
        return JSONResponse({"status": "updating", "mode": mode}, status_code=202)

    async def deploy_units(request: Request) -> JSONResponse:
        """Install this host's systemd user units from the wheel (#1831/#1835).

        The `deploy/**` lane's missing *deploy step*. ``unit_drift`` already
        detects that ``~/.config/systemd/user/coord-*.service`` has fallen
        behind the units packaged in the installed release; until now the
        remedy was a human with ``cp`` and ``systemctl``, which is exactly
        the gap #1835 cannot ship around — #1543's whole mechanism was three
        unit files and a shell script.

        Ordering matters and is the caller's job, not this endpoint's: the
        reference is ``coord/deploy/`` *inside the installed distribution*,
        so this must run **after** that host's ``/update`` swapped the venv,
        or it re-installs the version the host already had. ``coord release
        propagate`` (:func:`coord.release_propagate.plan_lanes`) encodes that
        order.

        Synchronous, unlike ``/update``: writing a handful of unit files and
        running ``daemon-reload`` takes milliseconds and — critically —
        **restarts nothing**. A ``daemon-reload`` re-reads unit files; it
        does not restart running services, so no in-flight worker dies here.
        Restarting the affected services stays an explicit, separate
        operator action.

        Body (JSON, optional)::

            {"dry_run": false}

        A units-lane deploy touches only units this host *already* has
        installed, and keeps a ``.bak`` of each — see
        :mod:`coord.deploy_units` for why both are deliberate.

        Also asserts every installed *timer* is enabled
        (:func:`coord.deploy_units.enable_timers`, #2082): refreshing a
        timer's content has never implied enabling it, and that gap is
        exactly how ``coord-release-propagate.timer`` reached this host with
        matching content and sat disabled for a day with nothing noticing.
        This runs on every non-dry-run call, not just when content changed —
        an already-enabled timer's *enablement* is untouched (idempotent).

        It does **not**, as of #2124, force a start (``--now``) on a timer
        that is already enabled: that used to restart a timer an operator
        had deliberately stopped moments earlier — the exact window the
        documented manual-roll sequence exists to create — because ``enable
        --now`` cannot tell "never enabled" (#2082's actual bug) apart from
        "enabled, but stopped on purpose" (never a bug at all). See
        :func:`coord.deploy_units.enable_timers` for how it now tells the
        two apart, and ``timers_enabled[<unit>]["changed"]`` below for
        which case each timer landed in.
        """
        from coord import deploy_units as du  # noqa: PLC0415
        from coord.brain import AGENT_PORT  # noqa: PLC0415

        body: dict = {}
        try:
            body = await request.json()
        except Exception:
            pass
        if not isinstance(body, dict):
            body = {}
        dry_run = bool(body.get("dry_run"))

        report = du.install_units(
            machine_name=getattr(server, "machine_name", None),
            port=AGENT_PORT,
            version=_installed_version(),
            dry_run=dry_run,
        )
        payload = report.to_dict()
        payload["dry_run"] = dry_run
        payload["reloaded"] = False
        payload["reload_detail"] = ""
        if report.changed and not dry_run:
            ok, detail = du.daemon_reload()
            payload["reloaded"] = ok
            payload["reload_detail"] = detail
            if not ok:
                payload["ok"] = False

        payload["timers_enabled"] = {}
        if not dry_run:
            timer_results = du.enable_timers(report)
            payload["timers_enabled"] = {
                name: {"ok": ok, "changed": changed, "detail": detail}
                for name, (ok, changed, detail) in timer_results.items()
            }
            if any(not ok for ok, _changed, _detail in timer_results.values()):
                payload["ok"] = False
        return JSONResponse(payload, status_code=200 if payload.get("ok") else 500)

    async def restart_services(request: Request) -> JSONResponse:
        """Restart this host's sibling coord-* units (#2069).

        ``POST /update`` swaps the venv and re-execs **the agent** — and
        nothing else. ``coord-serve``, ``coord-web`` and ``coord-drive-queue``
        keep running the generation they started with until something
        restarts THEM, which used to mean a human. This is that something,
        meant to be called right after ``/update`` lands on the same host
        (see ``coord/commands/release.py``'s ``_roll_python``).

        Which of the three units to touch is decided HERE, from what
        ``coord.health.checks.spawned_coord`` finds actually running on this
        host right now — the same "which services a host runs is a topology
        decision, not a release decision" rule ``/deploy-units`` already
        follows. A host that never ran ``coord-web`` never gets one started
        by this call.

        Synchronous, unlike ``/update``: restarting a sibling process does
        not kill the request handling this restart (that's the whole reason
        it can wait for confirmation rather than fire-and-forget — see
        :func:`_restart_sibling_unit`).

        Request body (JSON, optional)::

            {"units": ["coord-serve", "coord-web"]}

        Omit ``units`` (or POST ``{}``/no body) to consider all of
        :data:`RESTARTABLE_SIBLING_UNITS`. Naming a unit outside that set is
        a 400 — ``coord-agent`` restarts itself through ``/update``,
        ``/rollback`` and ``/restart``, and doing it again from here would
        race those endpoints' own restart threads.
        """
        from coord.health.checks.spawned_coord import running_unit_pids  # noqa: PLC0415

        body: dict = {}
        try:
            body = await request.json()
        except Exception:
            pass
        if not isinstance(body, dict):
            body = {}
        raw_units = body.get("units")
        if raw_units is None:
            wanted = set(RESTARTABLE_SIBLING_UNITS)
        elif isinstance(raw_units, list):
            wanted = {str(u) for u in raw_units}
        else:
            return JSONResponse({"error": "\"units\" must be a list"}, status_code=400)
        unknown = wanted - RESTARTABLE_SIBLING_UNITS
        if unknown:
            return JSONResponse(
                {
                    "error": (
                        f"not a restartable sibling unit here: {', '.join(sorted(unknown))} "
                        f"— must be one of {', '.join(sorted(RESTARTABLE_SIBLING_UNITS))}"
                    )
                },
                status_code=400,
            )

        if not _running_under_systemd():
            return JSONResponse(
                {
                    "units": {},
                    "detail": (
                        "this agent is not running under systemd — nothing here "
                        "can restart a sibling unit"
                    ),
                },
                status_code=200,
            )

        running = running_unit_pids(tuple(sorted(wanted)))
        results: dict[str, dict] = {}
        all_ok = True
        for unit in sorted(wanted):
            if unit not in running:
                results[unit] = {"restarted": None, "detail": "not running on this host"}
                continue
            # #2069 follow-up: _restart_sibling_unit is synchronous — subprocess.run
            # calls plus a time.sleep(0.5) poll loop for up to `timeout` seconds per
            # unit. This handler is `async def`, so Starlette does not thread it
            # automatically; running that blocking work inline here would freeze the
            # single uvicorn event loop (no other request — status polls, cancels,
            # health checks — could be served) for up to ~90s across 3 units. Same
            # fix as `server.health` above: hand it to a worker thread and await it.
            restarted, detail = await asyncio.to_thread(_restart_sibling_unit, unit)
            results[unit] = {"restarted": restarted, "detail": detail}
            all_ok = all_ok and restarted
        return JSONResponse({"units": results}, status_code=200 if all_ok else 500)

    async def rollback(request: Request) -> JSONResponse:
        """Flip ``~/.coord-venv`` back onto the previous blue/green
        generation and restart (#1241).

        Every successful ``/update`` keeps exactly one prior generation on
        disk (see :mod:`coord.agent_update`) precisely so this exists.
        Refuses — 404, nothing touched — when there's no previous
        generation (e.g. this machine has never run a blue/green
        ``/update``), and 409 (same as ``/update``, same ``{"force":
        true}`` override) when live sessions are running.

        Request body (JSON, optional)::

            {"force": false}
        """
        body: dict = {}
        try:
            body = await request.json()
        except Exception:
            pass
        if not isinstance(body, dict):
            body = {}
        force = bool(body.get("force"))

        with server._lock:
            active_count = sum(
                1
                for a in server._assignments.values()
                if a.status in (PENDING, RUNNING)
            )
        if active_count and not force:
            payload = {
                "mode": "rollback",
                "started_at": time.time(),
                "finished_at": time.time(),
                "result": "refused",
                "error": (
                    f"{active_count} active assignment(s) running — rolling "
                    "back restarts the process and kills them mid-flight. "
                    'Pass {"force": true} to roll back anyway, or wait for '
                    "them to finish."
                ),
            }
            _write_last_update(server.state_dir, payload)
            return JSONResponse(payload, status_code=409)

        venv_dir = _venv_dir()
        version_before = _installed_version() or "unknown"
        rb_initiator = body.get("initiator")
        if not isinstance(rb_initiator, str) or not rb_initiator.strip():
            peer = request.client.host if request.client else "unknown peer"
            agent_hdr = request.headers.get("user-agent") or "no user-agent"
            rb_initiator = f"POST /rollback from {peer} ({agent_hdr})"
        result = agent_update.rollback(venv_dir, initiator=rb_initiator)
        if not result.ok:
            payload = {
                "mode": "rollback",
                "started_at": time.time(),
                "finished_at": time.time(),
                "version_before": version_before,
                "result": "failed",
                "error": result.error,
                "log_excerpt": "\n".join((result.log or "").splitlines()[-20:]),
            }
            _write_last_update(server.state_dir, payload)
            return JSONResponse(payload, status_code=404 if "no previous generation" in (result.error or "") else 500)

        saved_argv = list(sys.argv)
        state_dir = server.state_dir

        def _do_rollback() -> None:
            payload = {
                "mode": "rollback",
                "started_at": time.time(),
                "finished_at": time.time(),
                "version_before": version_before,
                "version_after": result.new_version or "unknown",
                "result": "upgraded",
                "error": None,
                "log_excerpt": "\n".join((result.log or "").splitlines()[-20:]),
            }
            _write_last_update(state_dir, payload)
            time.sleep(0.5)
            exec_restart(saved_argv)

        threading.Thread(target=_do_rollback, daemon=False, name="agent-rollback").start()
        return JSONResponse(
            {"status": "rolling back", "slot": str(result.slot)}, status_code=202
        )

    async def artifact_manifest(request: Request) -> JSONResponse:
        """Return a JSON manifest of stashed artifacts for a (repo, branch) pair.

        Path parameters:
            repo   — repo name (e.g. ``quadraui``)
            branch — sanitized branch name (slashes already replaced with
                     dashes, e.g. ``issue-305-artifact-pull``)

        Response (200)::

            {
                "files": [{"name": "...", "size": N, "mtime": N}, ...],
                "total_bytes": N,
                "built_by_assignment_id": "abc123" | null
            }

        Returns 404 when no stash exists for the given (repo, branch) pair.
        The 404 body's ``error`` field carries the agent's ground-truth
        reason (#914) — e.g. a live worktree exists but was never stashed,
        vs. genuinely nothing was ever built here — rather than a generic
        message, since only this host can tell the difference.
        """
        repo = request.path_params["repo"]
        branch = request.path_params["branch"]
        manifest = server.artifact_manifest(repo, branch)
        if manifest is None:
            reason = server.artifact_absence_reason(repo, branch)
            return JSONResponse(
                {"error": f"no artifacts for repo={repo!r} branch={branch!r}: {reason}"},
                status_code=404,
            )
        return JSONResponse(manifest)

    async def worktree_clean(request: Request) -> JSONResponse:
        """Remove stale git worktrees managed by this agent.

        Idempotent POST — skips worktrees for running/pending assignments
        and those finished within the last 5 minutes.  Returns a JSON
        summary: ``{"cleaned": N, "kept": M, "bytes_freed": B}``.

        Optional JSON body::

            {
                "recent_secs": 300,           # override recency window (s)
                "protect": ["aid1", "aid2"]   # #1295: never sweep these AIDs
            }

        ``protect`` is optional and free-form — unknown/extra keys in the
        body are ignored, so a coordinator sending the new field to an
        older agent that ignores it, and a coordinator omitting the field
        entirely against a new agent, both work.  A protected entry is
        counted as ``kept`` in the response; the return shape is
        unchanged.

        #3145: ``clean_worktrees`` can run a per-worktree pre-stash
        ``build_command`` (``_stash_artifacts`` -> ``_run_pre_stash_build``,
        ``coord/agent.py``) with up to a 600s ``subprocess.run`` timeout
        *per worktree*. This handler is `async def`, so Starlette does not
        thread it automatically — calling the synchronous method inline
        here blocked the whole uvicorn event loop (every ``/health``,
        ``/assign``, ``/status`` on this agent) for up to 600s, which is
        exactly what happened on dellserver on 2026-09-05 (616s with zero
        served requests). Same fix as ``health``/``restart_services``/
        ``graph_fix``/``drive_queue_reconcile`` above: hand it to a worker
        thread and await it instead of calling it inline.
        """
        body: dict = {}
        try:
            body = await request.json()
        except Exception:
            pass
        recent_secs = float(body.get("recent_secs", 300.0))
        # #1295: accept "protect" as either a list or omitted.  Anything
        # non-list-like (a string, a dict, garbage) is dropped rather
        # than 400ing — we prefer to sweep what we can over rejecting an
        # otherwise-valid request because of a malformed side field.
        raw_protect = body.get("protect")
        protect: list[str] | None
        if isinstance(raw_protect, list):
            protect = [str(x) for x in raw_protect if isinstance(x, str)]
        else:
            protect = None
        # #3145 review note: this shares the default `asyncio.to_thread`
        # executor with `health`/`restart_services`/`graph_fix`/
        # `drive_queue_reconcile`. A burst of concurrent `/worktree-clean`
        # calls (e.g. several repos finalizing near-simultaneously, each
        # running its own pre-stash build) could saturate that pool and
        # delay other to_thread-based endpoints queuing behind it — a
        # milder version of the same freeze, bounded by pool size instead
        # of one global loop. Not the incident scenario (single in-flight
        # call), so left as the same pattern rather than a dedicated
        # executor; worth revisiting if concurrent finalizes become common.
        result = await asyncio.to_thread(
            server.clean_worktrees, recent_secs=recent_secs, protect=protect
        )
        return JSONResponse(result)

    async def restart(request: Request) -> JSONResponse:
        """Gracefully restart the agent process.

        Waits up to ``cancel_timeout`` seconds (default 30) for active workers
        to finish on their own.  Any workers still running after the timeout
        are cancelled before the process is replaced.  Returns HTTP 202
        immediately; the actual restart happens in a background thread.

        Request body (JSON, optional)::

            {"cancel_timeout": 30}
        """
        body: dict = {}
        try:
            body = await request.json()
        except Exception:
            pass

        cancel_timeout = float(body.get("cancel_timeout", 30))
        saved_argv = list(sys.argv)

        with server._lock:
            active_count = sum(
                1
                for a in server._assignments.values()
                if a.status in (PENDING, RUNNING)
            )

        def _do_restart() -> None:
            # Wait for workers to drain.
            deadline = time.time() + cancel_timeout
            while time.time() < deadline:
                with server._lock:
                    still_active = sum(
                        1
                        for a in server._assignments.values()
                        if a.status in (PENDING, RUNNING)
                    )
                if still_active == 0:
                    break
                time.sleep(1)

            # Cancel any workers that are still running.
            with server._lock:
                pending_ids = [
                    aid
                    for aid, a in server._assignments.items()
                    if a.status in (PENDING, RUNNING)
                ]
            for aid in pending_ids:
                try:
                    # #1567: this is an infra-triggered restart, not an
                    # operator `coord stop` — nobody decided this work was
                    # unwanted, so keep the pre-#1567 behaviour of pushing
                    # any WIP straight onto the worker's own branch.
                    server.cancel(aid, push_mode="branch")
                except Exception:
                    pass

            time.sleep(0.5)
            exec_restart(saved_argv)

        threading.Thread(target=_do_restart, daemon=False, name="agent-restart").start()
        return JSONResponse(
            {
                "status": "restarting",
                "active_workers": active_count,
                "cancel_timeout": cancel_timeout,
            },
            status_code=202,
        )

    async def metrics(_request: Request) -> JSONResponse:
        """#207: Return CPU and memory metrics for the agent machine.

        Uses ``psutil`` for sub-millisecond, non-blocking snapshots.
        ``cpu_percent(interval=None)`` returns the CPU utilisation since
        the previous call (or since process start on the very first call),
        which is essentially free — no sleep, no blocking.
        """
        try:
            import psutil  # lazy import — keeps startup fast on old agents
        except ImportError:
            return JSONResponse(
                {"error": "psutil not installed on this agent"},
                status_code=503,
            )
        cpu = psutil.cpu_percent(interval=None)
        vm = psutil.virtual_memory()
        return JSONResponse(
            {
                "cpu_percent": cpu,
                "mem_percent": vm.percent,
                "mem_used_mb": round(vm.used / (1024 * 1024), 1),
                "mem_total_mb": round(vm.total / (1024 * 1024), 1),
                "timestamp": time.time(),
            }
        )

    async def graph_fix(request: Request) -> JSONResponse:
        """Repair graphify's machine-local half for one repo on THIS machine
        (#2237) — ``coord repo doctor --fix`` fans this out to every machine
        that clones the repo.

        Body: ``{"repo": "<name>", "timeout": 600}``. ``repo`` is required;
        anything else is ignored, so an older coordinator posting extra
        fields and a newer one omitting optional ones both work.

        Idempotent by construction (``git config core.hooksPath`` +
        ``graphify update .``), and it **refuses** rather than acting when
        the repo does not ship ``.githooks/post-checkout`` — see
        :func:`coord.graph_health.apply_local_graph_fix`. Never writes a
        tracked file, so this can never turn into "the tool silently
        committed hooks into my repo".

        Runs off the event loop: a first build is minutes of AST work, and
        blocking the loop here would stall /health for the whole fleet.
        """
        body: dict = {}
        try:
            body = await request.json()
        except Exception:
            pass
        repo = body.get("repo")
        if not isinstance(repo, str) or not repo:
            return JSONResponse({"error": "repo is required"}, status_code=400)
        try:
            timeout = float(body.get("timeout", 600.0))
        except (TypeError, ValueError):
            timeout = 600.0
        result = await asyncio.to_thread(server.fix_graph, repo, timeout=timeout)
        return JSONResponse(result)

    async def drive_queue_reconcile(request: Request) -> JSONResponse:
        """Run ``coord drive-queue tick --reconcile-only`` on THIS machine
        (#2373). ``coord release propagate``'s drain-deadline escalation
        POSTs here — before it escalates loudly — on whichever host it has
        cordoned but that will not drain, so the one machine that can
        actually resolve the #1870 cross-host liveness ambiguity for an
        entry it launched gets asked to, automatically, instead of a human
        needing to SSH in and run this by hand (the live 2026-08-18 incident
        this closes).

        Body: optional ``{"timeout": 120}``. No required fields, unlike
        ``/graph-fix`` — a reconcile-only tick is fleet-queue-wide, not
        scoped to one repo.

        Runs off the event loop: the subprocess holds the drive-queue file
        lock for the length of one tick, which must never stall this
        agent's ``/health`` for the rest of the fleet.
        """
        body: dict = {}
        try:
            body = await request.json()
        except Exception:
            pass
        try:
            timeout = float(body.get("timeout", 120.0))
        except (TypeError, ValueError):
            timeout = 120.0
        result = await asyncio.to_thread(server.reconcile_drive_queue, timeout=timeout)
        return JSONResponse(result)

    routes = [
        Route("/health", health, methods=["GET"]),
        Route("/status", status, methods=["GET"]),
        Route("/repos", repos, methods=["GET"]),
        Route("/assign", assign, methods=["POST"]),
        Route("/cancel/{id}", cancel, methods=["POST"]),
        Route("/inject/{id}", inject, methods=["POST"]),
        Route("/logs/{id}", logs, methods=["GET"]),
        Route("/stream/{id}", stream, methods=["GET"]),
        Route("/update", update, methods=["POST"]),
        # #1831/#1835: the `deploy/**` lane's deploy step. Must be POSTed
        # AFTER /update on the same host — see the handler's docstring.
        Route("/deploy-units", deploy_units, methods=["POST"]),
        # #2069: restarts coord-serve/coord-web/coord-drive-queue — the rest
        # of a python-lane roll /update itself only does for coord-agent.
        # Must be POSTed AFTER /update on the same host, same reason as
        # /deploy-units above.
        Route("/restart-services", restart_services, methods=["POST"]),
        Route("/rollback", rollback, methods=["POST"]),
        Route("/restart", restart, methods=["POST"]),
        Route("/worktree-clean", worktree_clean, methods=["POST"]),
        # #2237: `coord repo doctor --fix`'s per-machine leg — build a
        # missing graph and set core.hooksPath, on the machines that
        # actually run workers rather than only on the operator's laptop.
        Route("/graph-fix", graph_fix, methods=["POST"]),
        # #2373: `coord release propagate`'s drain-deadline escalation's
        # per-machine self-heal — resolve a `running` drive-queue entry this
        # host launched before the escalation fires loudly.
        Route("/drive-queue-reconcile", drive_queue_reconcile, methods=["POST"]),
        # #305: artifact stash manifest (GET /artifact/<repo>/<branch>)
        Route("/artifact/{repo}/{branch}", artifact_manifest, methods=["GET"]),
        # #207: CPU + memory snapshot for TUI sparklines
        Route("/metrics", metrics, methods=["GET"]),
    ]
    # #757: served OpenAPI 3 spec + Swagger UI docs page.
    routes.extend(openapi_and_docs_routes(_openapi_spec()))
    app = Starlette(routes=routes)
    app.state.idle_restart_watcher = None
    if idle_restart:
        watcher = _IdleRestartWatcher(
            server, venv_dir=_venv_dir(), exec_restart=exec_restart
        )
        watcher.start()
        app.state.idle_restart_watcher = watcher
    return app
