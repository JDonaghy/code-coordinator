"""`coord release-preflight` — local sanity checks before cutting a release.

#1471: `main` is now a protected branch, so a plain ``git push origin main``
can be silently *rejected* while a subsequent ``git push origin vX.Y.Z``
still *succeeds* — the two pushes are independent refs and nothing couples
them. That let a v0.4.82 release publish (immutably) to PyPI from a commit
that, at that moment, existed nowhere but the releaser's local checkout and
the tag.

This command is a fast, local, no-side-effects check meant to run right
before tagging a release, per the flow in docs/AGENT_OPERATIONS.md (merge PR
-> pull merged main -> tag -> push tag — #1238 dropped the version-bump step
that used to precede it: the git tag *is* the version now, single-sourced
via setuptools-scm). It does not push, tag, or modify anything itself.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import click

from coord.commands._common import _CONFIG_OPTION


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def release_preflight_checks(repo_root: Path) -> list[str]:
    """Return a list of problems with *repo_root* as a release candidate.

    Empty list == clear to tag. Kept as a pure(ish) function over a repo
    checkout — the only side effect is a ``git fetch origin main`` — so it's
    straightforward to unit test against local-only git fixtures (no real
    network) and to reuse outside the CLI command if needed.

    Checks, mirroring the issue's #1471 proposal:
    - working tree is clean (no staged/unstaged changes)
    - currently on ``main``, and local ``main`` == ``origin/main`` (the
      protected-branch push must have already landed via a merged PR)

    #1238: this used to also assert ``pyproject.toml``'s ``version`` and
    ``coord/__init__.py``'s ``__version__`` agreed, and that the version
    they named wasn't already tagged. Both checks are gone along with the
    hand-maintained version literals they compared — the version is now
    single-sourced from the git tag itself (setuptools-scm), so there is no
    bump left to forget or mismatch. Cutting a release is just choosing and
    pushing a ``vX.Y.Z`` tag that doesn't exist yet; ``git tag vX.Y.Z``
    itself already refuses a name collision, so a redundant check here would
    add nothing.
    """
    problems: list[str] = []

    if not (repo_root / ".git").exists():
        return [f"{repo_root} is not a git checkout"]

    status = _git(repo_root, "status", "--porcelain")
    if status.returncode != 0:
        problems.append(f"git status failed: {status.stderr.strip()}")
    elif status.stdout.strip():
        problems.append(
            "working tree is not clean — commit or stash changes before "
            "releasing:\n" + status.stdout.rstrip()
        )

    branch = _git(repo_root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if branch != "main":
        # #1525: this check fires by design on a `release-v*` bump branch —
        # this command is a post-merge, pre-tag check (see the module
        # docstring's flow), not something to run while the bump PR is still
        # open. Spell that out here since the bare "not on main" message
        # read as a bug the first time it fired on a release branch.
        problems.append(
            f"not on main (currently on '{branch}') — this is a post-merge, "
            "pre-tag check: merge the release PR first, then `git checkout "
            "main && git pull origin main` and re-run this from there"
        )

    fetch = _git(repo_root, "fetch", "origin", "main")
    if fetch.returncode != 0:
        problems.append(f"git fetch origin main failed: {fetch.stderr.strip()}")
    else:
        local_head = _git(repo_root, "rev-parse", "HEAD").stdout.strip()
        remote_head = _git(repo_root, "rev-parse", "origin/main").stdout.strip()
        if local_head and remote_head and local_head != remote_head:
            problems.append(
                f"local main ({local_head[:8]}) != origin/main ({remote_head[:8]}) — "
                "pull/rebase onto origin/main first. main is protected: your "
                "change must land there via a merged PR *before* you tag "
                "it (#1471) — a tag built from a commit main rejected still "
                "publishes to PyPI, and PyPI releases are immutable."
            )

    return problems


@click.command(
    "release-preflight",
    help="Sanity-check the checkout before cutting a release (#1471).",
)
@click.option(
    "--path",
    "path_opt",
    default=None,
    help="Repo checkout to check (defaults to the current directory).",
)
def release_preflight(path_opt: str | None) -> None:
    """Fail loudly, before any tag is pushed, if release ordering would be wrong.

    Run this right before ``git tag vX.Y.Z && git push origin vX.Y.Z``. It
    fetches ``origin/main`` and confirms local ``main`` matches it and the
    working tree is clean — so the #1471 failure mode (tagging a commit that
    never actually landed on the protected ``main`` branch) is caught
    locally instead of shipping an immutable bad PyPI release.
    """
    repo_root = Path(path_opt).expanduser() if path_opt else Path.cwd()
    problems = release_preflight_checks(repo_root)
    if problems:
        click.echo("release preflight FAILED:", err=True)
        for problem in problems:
            click.echo(f"  - {problem}", err=True)
        sys.exit(1)
    click.echo(
        "release preflight OK — local main matches origin/main, working "
        "tree clean. Ready to tag: git tag vX.Y.Z && git push origin vX.Y.Z."
    )


# ──────────────────────────────────────────────────────────────────────────
# `coord release verify` — the POST-release half (#1834)
# ──────────────────────────────────────────────────────────────────────────
#
# `release-preflight` above guards the moment *before* a tag is pushed. It
# says nothing about whether the release that came out the other end ever
# reached the fleet — and on 2026-08-04 it demonstrably had not, while four
# independent readouts said it had. See `coord/release_verify.py` for the
# incident and the design rules; this file only owns the click surface.
#
# `release-preflight` stays registered as a flat top-level command for
# backward compatibility (it is in every operator's muscle memory and in
# docs/AGENT_OPERATIONS.md); the new `release` group carries `verify`, and
# aliases `preflight` under it so the pair is discoverable together.


@click.group("release", help="Release lifecycle checks (#1471, #1834).")
def release_group() -> None:
    """Pre-tag sanity checks and post-release fleet verification."""


def _resolve_expected(expected: str | None, *, use_pypi: bool, index_url: str,
                      timeout: float) -> tuple[str | None, str | None]:
    """(expected version, warning) — the version every lane *should* be on.

    ``--expected`` wins outright. ``--pypi`` asks the simple index (never the
    JSON API — see ``coord.health.pypi`` for why that distinction is
    load-bearing rather than pedantic). With neither, there is no absolute to
    grade against and the command falls back to pure skew detection, which is
    what actually caught 2026-08-04: nobody knew what to expect, but two
    lanes disagreeing was already conclusive.
    """
    if expected:
        return expected.lstrip("v"), None
    if not use_pypi:
        return None, None
    from coord.health.pypi import latest_release_any  # noqa: PLC0415

    try:
        _project, latest, _all = latest_release_any(index_url=index_url, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — read-only, degrade to skew-only
        return None, f"could not read the PyPI simple index ({exc}); checking skew only"
    if latest is None:
        return None, "PyPI simple index returned no release; checking skew only"
    return latest.raw, None


def _resolve_min_behind(min_behind_override: int | None, config) -> int:
    """The effective ``min_releases_behind`` for this run (#2583).

    ``--min-behind`` wins outright; otherwise ``propagation.
    min_releases_behind`` from ``coordinator.yml``; otherwise ``1`` — any
    delta at all, today's behaviour. An absent ``propagation:`` block (the
    common case until this is deliberately turned on, see
    ``docs/AGENT_OPERATIONS.md``) resolves to that same default via
    :class:`coord.config.PropagationConfig`'s own field default, so this is
    really just "flag beats config, config beats 1" spelled out for the two
    call sites (`release_propagate`/`release_nightly_window`) that need it.
    """
    if min_behind_override is not None:
        return min_behind_override
    return getattr(getattr(config, "propagation", None), "min_releases_behind", 1)


def _releases_behind_count(
    current_version: str | None, *, index_url: str, timeout: float,
) -> tuple[int | None, str | None]:
    """``(releases_behind, warning)`` for *current_version* against PyPI's
    simple index — the #2583 min-releases-behind gate's own delta.

    Reuses :func:`coord.health.pypi.releases_behind` — the SAME comparison
    ``coord/health/checks/agent_install.py``'s ``agent_version`` check runs
    on every machine's own ``/health`` — rather than a second
    version-comparison path. ``coord.release_cordon.version_drift`` is
    deliberately NOT reused here: it is a cheap, network-free
    PATCH-ARITHMETIC *estimate* built for decisions that run on every tick
    and must survive a network hiccup (cordoning, ``needs_roll``) — #2583's
    gate is the opposite case, an operator-set threshold checked once per
    run, so it is worth the extra PyPI read to answer with the actual count
    of published releases being held back instead of a guess.

    ``(None, warning)`` when *current_version* is unknown/unparseable or the
    index is unreachable — the caller must treat that as "cannot confirm
    holding" and gate OPEN (proceed exactly as if this gate did not exist),
    never hold on missing data.
    """
    if not current_version:
        return None, "current version unknown; not holding on it"
    from coord.health.pypi import latest_release_any, parse_version  # noqa: PLC0415
    from coord.health.pypi import releases_behind as _count_behind  # noqa: PLC0415

    try:
        _project, _latest, finals = latest_release_any(index_url=index_url, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — read-only; gate open on failure
        return None, f"could not read the PyPI simple index ({exc}); not holding on it"
    installed = parse_version(current_version)
    if installed is None:
        return None, f"could not parse version {current_version!r}; not holding on it"
    return _count_behind(installed, finals), None


@release_group.command(
    "verify",
    help=(
        "Assert every deploy lane on every host actually reflects the "
        "released version (#1834). Read-only; safe to run mid-flight."
    ),
)
@_CONFIG_OPTION
@click.option(
    "--expected",
    default=None,
    help=(
        "The version every lane must be on (leading 'v' optional). Without "
        "it, the command reports skew BETWEEN lanes, which is what the "
        "2026-08-04 incident actually looked like."
    ),
)
@click.option(
    "--pypi/--no-pypi",
    "use_pypi",
    default=True,
    show_default=True,
    help=(
        "Resolve --expected from the PyPI simple index (the released "
        "version). On by default since #2052: without an expected version "
        "this command compares the fleet against ITSELF, so a fleet that is "
        "uniformly four releases behind reports crit=0."
    ),
)
@click.option("--machine", "machine_filter", default=None,
              help="Only poll this machine (still reports it as one lane set).")
@click.option("--timeout", default=5.0, show_default=True,
              help="Per-host HTTP timeout, seconds.")
@click.option("--json", "as_json", is_flag=True, help="Emit the report as JSON.")
@click.option("-v", "--verbose", is_flag=True, help="Show each lane's resolved path.")
@click.option(
    "--exit-code/--no-exit-code",
    default=True,
    show_default=True,
    help="Exit 2 on crit, 1 on warn/unknown (mirrors `coord health`).",
)
def release_verify(
    config_path: Path,
    expected: str | None,
    use_pypi: bool,
    machine_filter: str | None,
    timeout: float,
    as_json: bool,
    verbose: bool,
    exit_code: bool,
) -> None:
    """Post-release: prove the fleet is on the version you think it is.

    Runs entirely over HTTP — each machine's own ``/health`` plus the
    daemon's ``/board`` — so it works from a thin client with no checkout and
    no credentials, and it never writes anything anywhere.
    """
    import json as _json  # noqa: PLC0415

    from coord import release_verify as rv  # noqa: PLC0415
    from coord.commands._common import _load_config  # noqa: PLC0415

    config = _load_config(config_path)
    index_url = getattr(getattr(config, "health", None), "pypi_index_url",
                        "https://pypi.org/simple")
    resolved, warning = _resolve_expected(
        expected, use_pypi=use_pypi, index_url=index_url, timeout=timeout
    )
    if warning and not as_json:
        click.echo(f"warning: {warning}", err=True)

    machine_health, unreachable, daemon_host, daemon_name = rv.gather(
        config, timeout=timeout, machine_filter=machine_filter
    )
    report = rv.verify(
        machine_health=machine_health,
        unreachable=unreachable,
        daemon_host=daemon_host,
        daemon_host_name=daemon_name,
        expected=resolved,
    )

    if as_json:
        click.echo(_json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        click.echo(rv.render(report, verbose=verbose))

    if exit_code and report.exit_code:
        sys.exit(report.exit_code)


# ──────────────────────────────────────────────────────────────────────────
# `coord release propagate` — the PROPAGATE half (#1835, PKG-7)
# ──────────────────────────────────────────────────────────────────────────
#
# The I/O shell over `coord.release_propagate`. Everything that decides
# anything — is the fleet quiescent, in what order may lanes roll, which
# deploy gates a finished roll releases — lives in that module and is unit
# tested without a fleet. What lives here is the part that needs one:
# fetching the board, POSTing to agents, running the verifier, appending the
# journal.
#
# Publish and propagate are separate on purpose and the separation is the
# whole design: see `.github/workflows/auto-release.yml` and
# `coord/release_propagate.py`'s module docstring.


def _state_dir() -> Path:
    from coord.platform_paths import default_coord_dir  # noqa: PLC0415

    return default_coord_dir()


def _fetch_board() -> tuple[dict, str | None]:
    """``(board_payload, error)`` — never raises.

    A board this command cannot read is a *deferral*, not a crash: the
    propagation timer runs unattended, and an unreadable board means we
    cannot prove the fleet is idle, which is exactly the state in which the
    safe move is to do nothing and say so.
    """
    from coord import release_verify as rv  # noqa: PLC0415

    try:
        return rv._default_board_fetch() or {}, None
    except Exception as exc:  # noqa: BLE001 — see docstring
        return {}, f"{type(exc).__name__}: {exc}"


def _interactive_session_busy(config) -> list:
    """Live interactive tmux sessions as host-pinned `Busy` signals (#2228).

    `release_propagate.assess_quiescence`'s ``extra_busy`` seam was built
    for exactly this — its own docstring names "an interactive tmux
    session" as the seam's purpose — but until now nothing ever fed it one.
    An interactive session is invisible to the board *by construction*
    (``coord assign --interactive`` launches into tmux; the tmux session
    never POSTs ``/assign``), so without this a host running one reads as
    idle and rolls out from under a human, restarting `coord-serve` on the
    daemon host mid-session.

    Reuses the same discovery `coord sessions --remote` renders
    (:func:`coord.interactive.gather_fleet_tmux_sessions`) rather than
    inventing a second way to look.

    Fails OPEN, not closed: a probe error is logged and DROPPED, never
    turned into a host-less `Busy` — an unattributable signal blocks EVERY
    host by design (`Quiescence.fleet_wide_busy`), and an unreadable
    session list must not silently escalate into that.
    """
    from coord import release_propagate as rp  # noqa: PLC0415
    from coord.interactive import gather_fleet_tmux_sessions  # noqa: PLC0415

    try:
        sessions, errors = gather_fleet_tmux_sessions(config)
    except Exception as exc:  # noqa: BLE001 — fail open, see docstring
        click.echo(
            f"warning: could not probe interactive sessions ({exc}) — "
            "quiescence is blind to them this run",
            err=True,
        )
        return []

    for machine in errors:
        click.echo(
            f"warning: could not probe {machine} for live interactive "
            "sessions — quiescence is blind to that host this run",
            err=True,
        )

    busy = []
    for s in sessions:
        if s.get("pane_dead") == "1":
            continue  # claude exited; tmux is up but nobody is driving it
        machine = s.get("machine")
        if not machine:
            continue  # no coordinator.yml host to pin it to (#2228: never invent one)
        busy.append(
            rp.Busy(
                kind="interactive session",
                subject=f"{machine}:{s['session_name']}",
                detail="`coord assign --interactive` never POSTs /assign, "
                "so the board cannot see this",
                host=machine,
            )
        )
    return busy


def _paused_machine_busy(config) -> list:
    """Operator pause / quiet-hours state as host-pinned `Busy` signals (#2174).

    `release_propagate.assess_quiescence`'s ``extra_busy`` docstring has
    named "a machine paused by an operator" as a seam producer since
    #1835, but until now nothing fed it one: `coord pause <machine>` said
    "leave this box alone" and `coord release propagate` read the machine
    as quiescent anyway, restarting `coord-agent` (and, on the daemon
    host, `coord-serve`) on it regardless.

    `coord.machine_pause.follow_on_paused_set()` is explicit `coord pause`
    UNION any quiet-hours window currently covering the machine — both
    genuine "an operator or an operator-set policy said stay off this
    box" facts — MINUS #2101 release cordons. Cordons are deliberately
    excluded: a cordon is THIS command's own drain mechanism, set to make
    a behind host quiescent, not a sign that it already is one; feeding a
    cordon back in here as "busy" would make it defer the very roll it
    exists to unblock.

    Thin-client aware for free: `follow_on_paused_set` routes through the
    daemon's `/pause` endpoint when a board service is configured (#1563),
    so this reads the one copy of pause state that actually governs
    dispatch — same as every other pause-aware call site in the fleet.
    """
    from coord.machine_pause import (  # noqa: PLC0415
        describe_pause_state,
        effective_quiet_hours,
        follow_on_paused_set,
    )
    from coord import release_propagate as rp  # noqa: PLC0415

    machines = list(getattr(config, "machines", ()) or ())
    if not machines:
        return []
    paused = follow_on_paused_set(machines)
    if not paused:
        return []
    quiet_hours = effective_quiet_hours(machines)
    busy = []
    for machine in machines:
        if machine.name not in paused:
            continue
        state = describe_pause_state(machine, paused, quiet_hours=quiet_hours)
        if state is None:
            continue  # defensive: membership in `paused` implies a reason
        detail = (
            f"quiet hours {state.detail}" if state.kind == "quiet"
            else "explicit `coord pause`"
        )
        busy.append(
            rp.Busy(
                kind="machine paused",
                subject=machine.name,
                detail=detail,
                host=machine.name,
            )
        )
    return busy


def _confirmation_running_busy() -> list:
    """A #2464 out-of-band test confirmation in flight, as a `Busy` signal
    (#2596).

    Thin wiring around :func:`coord.release_propagate.confirmation_lock_busy`
    — see that function's docstring for the incident and why a LOCAL,
    non-networked lock probe is the right (and sufficient) check here. Feeds
    the same `extra_busy` seam `_interactive_session_busy`/
    `_paused_machine_busy` do, at both call sites that build one (the top of
    `propagate` and `_drain`'s default `extra_busy_fetch`), so a confirmation
    in flight defers a restart exactly like a live headless assignment does.
    """
    from coord import release_propagate as rp  # noqa: PLC0415
    from coord.filelock import notify_lock_path  # noqa: PLC0415

    return rp.confirmation_lock_busy(notify_lock_path())


def _daemon_machine_name(
    config, override: str | None, machine_health: dict | None = None
) -> str | None:
    """Which machine in ``coordinator.yml`` runs ``coord-serve``.

    The daemon must lead every roll (see :func:`coord.release_propagate.
    plan_lanes`), so getting this wrong is not cosmetic — it reintroduces
    the documented 405, and #2052 watched exactly that happen: a partial
    revert briefly left the daemon host on 0.5.4 while both callers sat on
    0.5.8, because nothing could name the daemon and the roll fell back to
    ``coordinator.yml`` order.

    Resolution order, derivation first and guesswork nowhere:

    1. the explicit ``--daemon-host`` flag;
    2. **derived** — the machine whose own ``/health`` reports a running
       ``coord-serve`` unit (:func:`coord.release_verify.
       daemon_host_from_health`). This is the fact itself, not a proxy for it;
    3. the host in the configured ``board_service`` URL matched against each
       machine's host — still derived, just from config rather than from the
       fleet;
    4. ``None``, which the caller treats as *refuse the run*. Ordering is the
       one thing protecting against the 405; a run that cannot order itself
       must stop, not roll in whatever order the file happens to list.
    """
    machines = list(getattr(config, "machines", ()) or ())
    if override:
        return override

    if machine_health:
        from coord.release_verify import daemon_host_from_health  # noqa: PLC0415

        derived = daemon_host_from_health(machine_health)
        if derived:
            return derived

    try:
        from urllib.parse import urlparse  # noqa: PLC0415

        from coord.client import resolve_board_service  # noqa: PLC0415

        svc = resolve_board_service()
        if svc is None:
            return None
        host = (urlparse(svc.url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return None
    if not host:
        return None
    for machine in machines:
        if str(getattr(machine, "host", "")).lower() == host:
            return machine.name
        if machine.name.lower() == host:
            return machine.name
    return None


def _post(url: str, payload: dict, *, timeout: float) -> tuple[int | None, dict, str]:
    """POST JSON, tolerantly. ``(status, body, error)``."""
    import httpx  # noqa: PLC0415

    try:
        resp = httpx.post(url, json=payload, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return None, {}, f"{type(exc).__name__}: {exc}"
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = {}
    return resp.status_code, (body if isinstance(body, dict) else {}), ""


#: #2373: default timeout for the remote-reconcile POST below. Generous, but
#: still small next to `DEFAULT_DRAIN_DEADLINE_SECONDS` (90 minutes), so a
#: wedged/unreachable agent never turns "ask before escalating" into a
#: second, longer wait before the loud message an operator is actually
#: watching for. The POST body forwards a *shorter* remote-subprocess
#: timeout (see `_RECONCILE_TIMEOUT_MARGIN_SECONDS` below) so the remote
#: agent's own tick (`AgentServer.reconcile_drive_queue`, otherwise 120s by
#: default) can never outlive this wait — without that, a tick legitimately
#: taking longer than this timeout would surface here as a misleading
#: "unreachable" even though it was still running and would have resolved
#: correctly on its own.
DEFAULT_RECONCILE_TIMEOUT_SECONDS = 30.0

#: Margin subtracted from *timeout* before it is forwarded as the remote
#: subprocess's own timeout (see `_reconcile_launch_host`) — leaves the
#: agent's HTTP handler room to marshal and return its response before
#: this side's own `httpx` deadline fires.
_RECONCILE_TIMEOUT_MARGIN_SECONDS = 5.0


def _reconcile_launch_host(
    machine_host: str,
    *,
    agent_port: int,
    timeout: float = DEFAULT_RECONCILE_TIMEOUT_SECONDS,
    post=None,
) -> tuple[bool | None, str]:
    """Ask *machine_host*'s own agent to run a local reconcile-only tick
    (#2373), over the same ``POST /<verb>`` HTTP pattern ``/graph-fix``
    already uses for a per-machine self-heal fanned out from elsewhere.

    Why this has to be a REMOTE call rather than something run locally by
    whichever host happens to be executing `coord release propagate`: the
    #1870 cross-host guard (`coord.drive_queue._reconcile_running`) is keyed
    on a LOCAL tmux read, so only the machine that actually launched a
    `running` drive-queue entry can ever resolve whether it is still alive.
    A non-daemon launch host has no periodic tick of its own — confirmed
    live 2026-08-18 (claude-coordinator#2360): elitebook ran only
    `coord-agent.service`, dellserver's `coord-drive-queue.timer` correctly
    refused to declare a foreign-host entry dead every time it ticked, and
    the ambiguity sat wedging the fleet's release drain for ~17h until a
    human happened to SSH in and run this exact command by hand. Routing it
    through the agent HTTP API that already runs on every machine capable of
    launching a drive closes that gap without a new timer unit anywhere.

    Returns ``(ok, detail)``. ``ok`` is ``None`` when the call could not even
    be attempted (never treated as a *failure* by the caller — see
    :func:`_escalate_drain`), ``False`` on an unreachable agent or a non-200
    response, ``True`` when the agent ran the tick (its own exit code, not
    "and it resolved the entry" — a genuinely still-busy host is expected to
    report ``ok=True`` with nothing to reconcile).

    The POST body forwards a remote subprocess timeout derived from
    *timeout* itself (minus `_RECONCILE_TIMEOUT_MARGIN_SECONDS`) rather than
    leaving the remote agent to fall back to its own 120s default
    (`AgentServer.reconcile_drive_queue`) — otherwise a tick that legitimately
    takes longer than this call's own HTTP wait reports here as
    `ok=False, detail="unreachable: ..."` even though it is still running and
    will resolve correctly on its own, just not attributed to this call.
    """
    poster = post or _post
    url = f"http://{machine_host}:{agent_port}/drive-queue-reconcile"
    remote_timeout = max(1.0, timeout - _RECONCILE_TIMEOUT_MARGIN_SECONDS)
    status, body, err = poster(url, {"timeout": remote_timeout}, timeout=timeout)
    if err:
        return False, f"unreachable: {err}"
    if status != 200:
        return False, f"HTTP {status}: {body.get('detail') or body}"
    return bool(body.get("ok")), str(body.get("detail") or "")


def _lane_versions_by_host(report) -> dict[str, list[str | None]]:
    out: dict[str, list[str | None]] = {}
    for lane in report.lanes:
        out.setdefault(lane.host, []).append(lane.version)
    return out


def _queue_provably_busy(quiescence, daemon_name: str | None) -> bool:
    """#2889 item 2: does *quiescence* name a genuine `drive_queue` row (not
    some OTHER busy reason — an interactive session, a paused machine, an
    in-flight confirmation) actively occupying *daemon_name* right now?

    Deliberately narrower than "the daemon is busy" (`daemon_name in
    busy_hosts`, this module's existing arm TRIGGER — see
    `_ensure_roll_pending_marker`'s call site): a genuine drive-queue entry
    is the one busy reason a freshly-armed marker cannot do anything about
    faster than `coord drive-queue tick`'s own reconciliation already will
    on its normal ~3-minute cadence — arming one anyway just spends up to a
    full TTL (#2587's own hour-long bound) learning that for free, exactly
    the "10 fresh arms, 49 ticks refused to launch" pathology #2889 reports.
    A busy signal of any OTHER kind (`Quiescence.busy`'s `kind` field —
    `coord.release_propagate.assess_quiescence`'s own vocabulary:
    "confirmation/notify drain running", "live RUNNING assignment", a paused
    machine) is exactly the case a marker's capacity-0 freeze DOES help:
    nothing NEW should launch onto the daemon host while it clears, and
    nothing here can predict when it will on its own.

    Unattributable signals (``b.host is None`` — `Quiescence.fleet_wide_busy`)
    are included: an unreadable board or an unattributed drive-queue row
    means "busy somewhere unknown", which must be read as busy everywhere,
    the same rule `Quiescence.rollable_hosts` already applies.
    """
    return any(
        b.kind == "drive-queue entry running" and (b.host is None or b.host == daemon_name)
        for b in quiescence.busy
    )


def _fresh_arm_refusal_reason(ledger, *, now: float, queue_provably_busy: bool) -> str:
    """Empty string when a FRESH `RollPending` arm (no existing marker at
    all) may proceed; otherwise the reason it must not, in priority order —
    escalated ledger, then the rate limit, then a provably busy queue.

    Shared by `_ensure_roll_pending_marker` and `release_nightly_window`'s
    own arm site (#2587 section 3) so the three #2889 checks can never drift
    between the two places a fresh marker gets armed — #2096's "two
    surfaces, one function" rule, one level up from `_drain`'s own reuse of
    `release_propagate.assess_quiescence` for the identical reason.

    Pure — takes a `coord.drive_queue.RollLedger` and the caller's own
    already-computed *queue_provably_busy* rather than reading a clock or a
    board itself, so it stays trivially testable and every call site decides
    for itself how loud to be about the result (only the ESCALATED reason is
    meant to also trigger a recorded escalation — see both callers).
    """
    if ledger.escalated:
        return (
            f"the #2889 roll ledger has escalated "
            f"({ledger.cumulative_frozen_seconds:.0f}s cumulative frozen time across "
            f"{ledger.marker_count} marker generation(s)) — run `coord drive-queue "
            "cancel-roll` to clear it before arming again"
        )
    wait = ledger.seconds_until_next_arm(now)
    if wait > 0:
        return (
            f"under the #2889 rate limit ({wait:.0f}s remaining since the last "
            "fresh arm)"
        )
    if queue_provably_busy:
        return (
            "a genuine drive-queue entry is actively occupying the daemon host "
            "right now (#2889 item 2) — a fresh marker cannot roll any faster "
            "than the tick's own reconciliation already will"
        )
    return ""


def _ensure_roll_pending_marker(
    target_version: str, *, reason: str, min_releases_behind: int | None = None,
    dry_run: bool = False, queue_provably_busy: bool = False,
) -> bool:
    """#2587: make sure a roll-pending marker exists for *target_version*,
    without resetting an already-live one's clock.

    #2889: returns whether a marker is live as a result of this call
    (existing/updated/freshly armed) — ``False`` only for a REFUSED fresh
    arm (see the three checks below). Every existing call site predates this
    return value and simply ignores it — a refused arm is already, on its
    own, an ordinary deferral from the caller's point of view, exactly like
    a still-busy fleet. *queue_provably_busy* (see
    :func:`_queue_provably_busy`) only ever affects a FRESH arm (no existing
    marker at all): a re-arm of an already-live marker (same OR different
    target) is a continuation of a campaign already in progress and proceeds
    regardless — refusing THAT would just reintroduce #2607's own bug by a
    different door, since a marker that cannot be kept current would then
    fire against a target it no longer names.

    #2870: *min_releases_behind* is the effective `propagation.
    min_releases_behind`/`--min-behind` threshold THIS run resolved and
    already gated on before reaching this call (see the #2583 gate just
    above this function's one call site) — stamped onto the marker
    (`RollPending.min_releases_behind`) so whatever eventually discharges it
    (`coord.commands.release._run_propagate`, threaded a matching
    `--min-behind`) is gated at the SAME threshold this arm used, not
    whatever threshold ITS OWN invocation happens to resolve. Like
    `target_version`/`reason`, this is updated on every re-arm (a re-arm is
    a fresh decision about what SHOULD roll and at what threshold) — only
    `set_at`/`deferrals` stay frozen (see below).

    #2869: ``dry_run=True`` is the ONE seam every call site must go through —
    it never touches disk, only echoing what it *would* have written, in the
    same "would ..." register as ``_fire_pending_roll``'s and
    ``_apply_cordons``'s own dry-run wording. A caller-side ``if dry_run:``
    guard around the call site was tried first and is exactly what let this
    bug happen: the marker write is the one state mutation on the #2587
    defer branch that never got that guard. Folding the check in here means
    a future second call site inherits the guard for free instead of having
    to remember to add its own.

    Called from the one place `coord release propagate` itself hits the
    #2112 daemon-busy deadlock it cannot roll through on its own (the
    daemon-first ``busy_hosts`` check in :func:`release_propagate`) — so a
    plain, periodic ``coord-release-propagate.timer`` firing while the
    daemon host is behind and itself busy ALSO queues the marker
    :func:`coord.drive_queue.plan_tick` and the drive-queue tick watch for,
    exactly like ``coord release nightly-window`` does explicitly. See
    :class:`coord.drive_queue.RollPending`'s docstring for the mechanism
    this feeds.

    Never overwrites an existing marker's ``set_at``/``deferrals`` — for the
    SAME target this is a plain no-op (unchanged from before #2607); for a
    DIFFERENT target the target/reason are updated in place but the clock
    and deferral count carry over unchanged. #2607: PyPI's "latest" climbs on
    every merge (`Auto-release on merge to main`), so a busy fleet's target
    had almost always moved by the time this timer re-fired — treating that
    as a "new" roll and resetting ``set_at``/``deferrals`` made the TTL/
    deferral bound (:meth:`~coord.drive_queue.RollPending.expired`)
    unreachable in practice, exactly the "never actually bounded" failure
    #2587's own bound exists to prevent, and exactly how #2607's queue froze
    for good. A re-arm — same target or not — is a continuation of the SAME
    stuck roll, not a new operator request; only a marker set from scratch
    (no existing one at all, e.g. right after `coord drive-queue
    cancel-roll`) starts a new clock.
    """
    import dataclasses as _dataclasses  # noqa: PLC0415
    import time as _time  # noqa: PLC0415

    from coord.commands.drive_queue import (  # noqa: PLC0415
        _escalate_roll_ledger,
        read_roll_ledger,
        read_roll_pending,
        write_roll_pending,
    )
    from coord.drive_queue import RollPending  # noqa: PLC0415

    existing = read_roll_pending()
    if existing is not None and existing.target_version == target_version:
        return True
    if existing is not None:
        if dry_run:
            click.echo(
                f"--dry-run: would replace the pending roll-pending marker "
                f"({existing.describe()}) with v{target_version} (reason="
                f"{reason!r}), preserving its original set_at/deferrals (#2607)"
            )
            return True
        write_roll_pending(
            _dataclasses.replace(
                existing, target_version=target_version, reason=reason,
                min_releases_behind=min_releases_behind,
            )
        )
        return True

    # #2889: a genuinely FRESH arm — no existing marker at all — is the one
    # case the RATE of markers can run away (#2889's own report: ten fresh
    # arms in ~15 hours, each one individually well-behaved).
    now = _time.time()
    ledger = read_roll_ledger()
    refusal = _fresh_arm_refusal_reason(
        ledger, now=now, queue_provably_busy=queue_provably_busy,
    )
    if refusal:
        message = f"declining to arm a roll-pending marker for v{target_version} — {refusal}"
        if dry_run:
            click.echo(f"--dry-run: would refuse to arm — {refusal}")
            return False
        click.echo(message, err=True)
        if ledger.escalated:
            # Re-recorded on every refused attempt, same "quiet re-record
            # keeps the escalation fresh" posture the #2572 self-cordon
            # escalation already documents — cheap (an upsert) and means an
            # operator checking `coord drive-queue status` mid-day sees this
            # is still live, not a one-time blip from hours ago.
            _escalate_roll_ledger(ledger, now=now)
        return False

    if dry_run:
        click.echo(
            f"--dry-run: would set a roll-pending marker for v{target_version} "
            f"(reason={reason!r}) — the drive-queue tick would then hold "
            "capacity at 0 until the queue drains"
        )
        return True
    write_roll_pending(
        RollPending(
            target_version=target_version, set_at=now, reason=reason,
            min_releases_behind=min_releases_behind,
        )
    )
    # Nothing to write to the ledger here — only an EXPIRY moves its
    # rate-limit clock (`RollLedger.record_expiry`), never an arm. See that
    # method's own docstring for why measuring from the arm, not the clear,
    # would let the exact re-arm-right-after-TTL-expiry case through.
    return True


#: #3047 part 2: default ceiling for `--drain`'s resident polling loop before
#: it gives up and exits non-zero with whichever hosts are still behind. Same
#: order of magnitude as `release_cordon.DEFAULT_TTL_SECONDS` (an hour) on
#: purpose — a drain that has not converged in the time its own cordons would
#: lapse anyway has nothing left protecting it, so there is no point holding
#: this loop open any longer than that either.
#:
#: Deliberately NOT named `DEFAULT_DRAIN_DEADLINE_SECONDS` — this module
#: already imports `release_cordon` as `rc` and references
#: `rc.DEFAULT_DRAIN_DEADLINE_SECONDS` (the cordon-escalation deadline, a
#: different concept with a different default: 5400s vs. this loop's 3600s).
#: A same-named constant with a different value, in the same file that
#: already carries the qualified reference, is exactly the drifted-constant
#: trap #2136 paid to close for `release_window` — see that issue's rename of
#: `DEFAULT_DRAIN_DEADLINE_SECONDS` to `DEFAULT_DRAIN_WAIT_SECONDS`. Do not
#: rename this back to the shared name even for consistency.
DEFAULT_DRAIN_LOOP_DEADLINE_SECONDS = 3600.0

#: How often `--drain` re-evaluates fleet quiescence. #2854's between-legs
#: gap is "normally seconds long" (`coord/release_propagate.py`'s module
#: docstring) — a single manual `coord release propagate` call is
#: overwhelmingly likely to land outside it, which is the whole reason
#: `--drain` exists. Short enough that a normally-seconds gap is actually
#: caught inside it; long enough that polling is not pure noise against a
#: fleet that is still genuinely busy.
DEFAULT_DRAIN_INTERVAL_SECONDS = 15.0


@release_group.command(
    "propagate",
    help=(
        "Roll the released version onto each host at ITS next quiescent "
        "window (#2067 — per host, not fleet-wide), verify it, and roll "
        "back on red (#1835). Safe to run from a timer: a busy host is a "
        "recorded deferral for that host, not a failure of the run. "
        "--drain (#3047) turns this into a resident loop that keeps polling "
        "for a window itself, instead of leaving that to the operator."
    ),
)
@_CONFIG_OPTION
@click.option("--target", "target", default=None,
              help="Version to propagate (leading 'v' optional). Default: PyPI's latest.")
@click.option("--daemon-host", "daemon_host_override", default=None,
              help="Machine name running coord-serve. It rolls FIRST — a caller "
                   "must never reach an endpoint its daemon predates. Normally "
                   "DERIVED from the fleet's own /health; pass this when it "
                   "cannot be, since an unorderable multi-host run refuses.")
@click.option("--lane", "lane_filter", multiple=True,
              type=click.Choice(["python", "units", "tui"]),
              help="Only roll these lanes (repeatable). Default: all of them.")
@click.option("--dry-run", is_flag=True,
              help="Print the window verdict and the roll plan; change nothing.")
@click.option("--force", is_flag=True,
              help="Roll even if the fleet is busy. This KILLS in-flight headless "
                   "workers — the whole reason propagation is quiescence-scheduled.")
@click.option("--verify/--no-verify", "do_verify", default=True, show_default=True,
              help="Run `coord release verify` as the final gate.")
@click.option("--rollback-on-red/--no-rollback-on-red", default=True, show_default=True,
              help="Roll every updated host back to its previous venv generation "
                   "when verification comes back CRIT *on a lane this run could "
                   "actually roll* (#2052). Findings on lanes propagation has no "
                   "channel for are advisory and never trigger this.")
@click.option("--release-holds/--no-release-holds", "release_holds", default=True,
              show_default=True,
              help="After a VERIFIED roll, release the drive-queue deploy gates "
                   "(#1757) that were waiting for exactly this deploy.")
@click.option("--timeout", default=180.0, show_default=True,
              help="Seconds to wait for each agent to report the new version.")
@click.option("--cordon/--no-cordon", "do_cordon", default=True, show_default=True,
              help="#2101: stop each behind host from starting NEW work until it "
                   "is up to date, so it drains into a rollable state instead of "
                   "waiting for a window that never comes. In-flight work is "
                   "never killed. --no-cordon also CLEARS any cordon this "
                   "mechanism already set — turning it off must release the "
                   "fleet, not freeze it.")
@click.option("--cordon-after", default=None, type=int,
              help="Releases behind before a host is cordoned (default: 1, i.e. "
                   "any drift). Raise it if release cadence ever makes one "
                   "fleet drain per release too expensive — see #2101 trap F.")
@click.option("--cordon-ttl", default=None, type=float,
              help="Seconds a cordon stays effective without being renewed "
                   "(default 3600). This is what stops a run killed mid-drain "
                   "from cordoning the fleet forever.")
@click.option("--drain-deadline", default=None, type=float,
              help="Seconds a host may fail to drain before the cordon "
                   "escalates loudly (default 5400).")
@click.option("--cordon-max-deferrals", default=None, type=int,
              help="#2240/#2741: consecutive DEFERRED runs with an UNCHANGED "
                   "busy signal that may hold a cordon for the same target "
                   "before it is released outright (default 2) — a cordon "
                   "does not itself block follow-on dispatch (a review still "
                   "routes onto a cordoned host), so this bound is a genuine "
                   "stall detector, not a workaround for blocked dispatch. "
                   "0 disables the bound and re-arms the deadlock.")
@click.option("--cordon-cooldown", default=None, type=float,
              help="Seconds after a #2240 release before cordoning may resume "
                   "(default 1800). Without it the next run re-cordons — the "
                   "hosts are still behind — and the deadlock re-arms.")
@click.option("--min-behind", "min_behind_override", default=None, type=int,
              help="#2583: hold this run — no cordon, no host touched — below "
                   "this many releases behind PyPI's latest. Default: "
                   "propagation.min_releases_behind in coordinator.yml, or 1 "
                   "if that is unset — any delta at all rolls, today's "
                   "behaviour.")
@click.option("--json", "as_json", is_flag=True, help="Emit the propagation record as JSON.")
@click.option("--drain", "drain", is_flag=True,
              help="#3047: resident mode. Instead of one attempt, keep re-evaluating "
                   "fleet quiescence on an interval — renewing this run's own cordons "
                   "every pass, same as a normal attempt already does — until every "
                   "behind host reaches the target or --give-up-after passes. Exits "
                   "the moment the roll is done, unlike `coord-release-propagate.timer`. "
                   "Cannot be combined with --dry-run: a dry run changes nothing, so "
                   "draining would never converge.")
@click.option("--give-up-after", "deadline_seconds",
              default=DEFAULT_DRAIN_LOOP_DEADLINE_SECONDS,
              type=float, show_default=True,
              help="#3047: seconds --drain may keep polling before giving up and "
                   "exiting non-zero with whichever hosts are still behind — checked "
                   "once per poll, so the real worst case is up to one "
                   "--drain-interval past this many seconds, not exactly this many. "
                   "Ignored without --drain. Deliberately not named --deadline: this "
                   "command already has an unrelated --drain-deadline (the "
                   "cordon-escalation window) and the two are too easy to mistype "
                   "for each other under an active roll.")
@click.option("--drain-interval", "drain_interval_seconds",
              default=DEFAULT_DRAIN_INTERVAL_SECONDS, type=float, show_default=True,
              help="#3047: seconds between --drain's re-evaluation attempts. Ignored "
                   "without --drain.")
def release_propagate(  # noqa: PLR0912, PLR0915 — a pipeline; the decisions are elsewhere
    config_path: Path,
    target: str | None,
    daemon_host_override: str | None,
    lane_filter: tuple[str, ...],
    dry_run: bool,
    force: bool,
    do_verify: bool,
    rollback_on_red: bool,
    release_holds: bool,
    timeout: float,
    do_cordon: bool,
    cordon_after: int | None,
    cordon_ttl: float | None,
    drain_deadline: float | None,
    cordon_max_deferrals: int | None,
    cordon_cooldown: float | None,
    min_behind_override: int | None,
    as_json: bool,
    drain: bool,
    deadline_seconds: float,
    drain_interval_seconds: float,
    _drain_quiet: bool = False,
) -> None:
    """One propagation attempt. Exit 0 on deferral, 1 on red, 2 on rollback.

    #2067: the window is assessed PER HOST, not fleet-wide. A host with a
    live assignment or a running drive-queue entry defers on its own; the
    others roll and get verified this run regardless. The one case that
    still defers the whole run is the daemon host itself being occupied —
    every other host's python lane has to wait behind it (see
    ``coord/release_propagate.py``'s LANE ORDER section) — and the case a
    signal can't be pinned to any one host at all (an unreadable board), in
    which nothing can be proven safe to roll.

    #2052: the final gate is scoped to the lanes this run attempted and could
    have moved. Verify grades lanes propagation cannot roll — the operator's
    ``~/.coord-cli-venv`` and a remote ``coord-tui`` binary, currently — and
    holding a roll to those made every successful run red, which
    ``--rollback-on-red`` then reverted. Those findings are still reported and
    journalled in full; they are simply not evidence about *this* roll.

    #2069: the python lane's reach used to stop at ``coord-agent`` — a venv
    could swap cleanly while ``coord-serve`` kept serving the generation it
    started with, and this command still exited green. ``_roll_python`` now
    also restarts ``coord-serve``/``coord-web``/``coord-drive-queue`` on
    whichever host actually runs them, right after that host's own ``/update``
    lands, so the ``coord-serve process`` and ``<unit> spawns`` findings are
    graded like any other python-lane lane instead of being permanently
    advisory.

    #2101: this command no longer only WAITS for a window, it CREATES one.
    Every host that is behind the target is *cordoned* — no new agents route
    there, in-flight work is untouched — so it drains itself into a rollable
    state; the moment it is rolled it is uncordoned again. That is why the
    version sweep below happens BEFORE the "fleet is busy, defer" branch: a
    run that defers without cordoning is a run that will defer again in 20
    minutes for exactly the same reason, which is how the fleet sat eleven
    releases behind for a day with elitebook idle and rollable throughout.

    #2240: and a deferral is not passive either. The cordon it leaves behind
    is what blocks the review dispatch that would let the between-legs entry
    finish, and that unfinished entry is what defers the next run — four
    cycles, 70 minutes, three idle machines. So the cordon now has a bound
    that nothing else has to be running for: ``--cordon-max-deferrals``
    consecutive deferrals for one target and every cordon is dropped, with
    ``--cordon-cooldown`` seconds before any may be set again. The counter
    lives in the propagation journal, because the process holding it is
    restarted by the roll it gates.

    #3047: ``--drain`` turns the single attempt this docstring describes into
    a resident loop (:func:`_run_drain`) that keeps calling this SAME
    function — one attempt at a time, `deadline_seconds`/`drain_interval_
    seconds` apart, everything else unchanged — until every host this run
    would otherwise cordon has rolled, or the deadline passes. It exists
    because #2854 already stops charging a between-legs row as busy once its
    gap outlasts a short settle window, but nothing SAMPLES that window while
    `coord-release-propagate.timer` is masked for a manual roll: one call is
    overwhelmingly likely to land outside the (normally seconds-long) gap and
    report ``deferred``, leaving an operator to re-run this command by hand
    until a poll finally lands inside it.

    *_drain_quiet* is internal-only (never a Click option, so an ordinary CLI
    invocation always uses its ``False`` default) — :func:`_run_drain` sets it
    on every attempt it makes when ``--json`` is also requested, so this
    function's own per-attempt record echo is suppressed and ``_run_drain``
    can print exactly ONE aggregated JSON document for the whole drain
    instead of interleaving N per-attempt documents with its own `[drain]`
    status lines (#3047 review).
    """
    if drain:
        # #3047: hand off to the resident loop and never reach the rest of
        # this body directly — `_run_drain` calls right back into THIS
        # function, one attempt at a time, with `drain=False`. It always
        # exits (success, deadline, or a hard failure/rollback from one of
        # its attempts), so nothing after this call ever runs.
        if dry_run:
            raise click.UsageError(
                "--drain cannot be combined with --dry-run: a dry run changes "
                "nothing on any host, so draining would never converge (#3047)."
            )
        _run_drain(
            deadline_seconds=deadline_seconds,
            poll_seconds=drain_interval_seconds,
            config_path=config_path,
            target=target,
            daemon_host_override=daemon_host_override,
            lane_filter=lane_filter,
            dry_run=dry_run,
            force=force,
            do_verify=do_verify,
            rollback_on_red=rollback_on_red,
            release_holds=release_holds,
            timeout=timeout,
            do_cordon=do_cordon,
            cordon_after=cordon_after,
            cordon_ttl=cordon_ttl,
            drain_deadline=drain_deadline,
            cordon_max_deferrals=cordon_max_deferrals,
            cordon_cooldown=cordon_cooldown,
            min_behind_override=min_behind_override,
            as_json=as_json,
        )
        return  # pragma: no cover — _run_drain always calls sys.exit()

    import json as _json  # noqa: PLC0415
    import time  # noqa: PLC0415

    from coord import release_propagate as rp  # noqa: PLC0415
    from coord import release_verify as rv  # noqa: PLC0415
    from coord.commands._common import AGENT_PORT, _load_config  # noqa: PLC0415

    config = _load_config(config_path)
    state_dir = _state_dir()
    record = rp.PropagationRecord(started_at=time.time(), dry_run=dry_run)

    def _finish(status: str, exit_code: int = 0) -> None:
        record.status = status
        record.finished_at = time.time()
        if not dry_run:
            try:
                rp.append_record(state_dir, record)
                rp.trim_journal(state_dir)
            except OSError as exc:
                click.echo(f"warning: could not append the propagation journal: {exc}",
                           err=True)
        if not _drain_quiet:
            # #3047 review: `_drain_quiet` is set only by `_run_drain`, and
            # only when `--json` is also requested — it suppresses THIS
            # attempt's own echo so a `--drain --json` run's stdout carries
            # exactly one JSON document (`_run_drain`'s own aggregated
            # summary) instead of one per attempt. Every other caller
            # (a plain single-shot run, or `--drain` without `--json`) is
            # byte-identical to before this existed.
            if as_json:
                click.echo(_json.dumps(record.to_dict(), indent=2, sort_keys=True))
            else:
                click.echo("\n".join(rp.render_record(record)))
        # #3047: equivalent to `sys.exit(exit_code)` for every existing
        # caller — `SystemExit.code` is identical either way, so a real CLI
        # invocation or a `CliRunner` both see the exact same exit behaviour
        # as before. The only change is `.record`: an attribute nothing but
        # `--drain`'s resident loop (`_run_drain`) ever reads. That loop
        # calls this whole function again for its next attempt rather than
        # letting the process actually exit, and needs the finished record
        # back to know what happened — without it, `_run_drain` would have
        # no way to tell "rolled, still hosts behind" from "up to date"
        # except by re-parsing this call's own rendered text.
        exc = SystemExit(exit_code)
        exc.record = record
        raise exc

    def _out(message: str) -> None:
        """Stdout work-log line for THIS attempt (a roll/rollback/gate-release
        step, or `_apply_cordons`'s human-readable plan) — as opposed to
        `_finish`'s own final record echo, and as opposed to every other
        `click.echo(..., err=True)` in this function, which is diagnostic and
        already never touches stdout.

        Suppressed under `_drain_quiet` (#3047 review) for the same reason
        `_finish`'s record echo is: `_run_drain` needs stdout to carry
        exactly one JSON document per drain run when `--json` is set, not
        this attempt's work log interleaved with it. Every other caller — a
        plain single-shot run, or `--drain` without `--json` — is
        byte-identical to before this existed.
        """
        if not _drain_quiet:
            click.echo(message)

    def _scope_gate(report) -> "rp.GateVerdict":
        """Score *report* against this run's own attempts, print advisories
        with their remedy, and stamp both onto *record* (#3048).

        Shared by the post-roll gate (step 5, the only caller before #3048)
        AND every early deferral above it. #3048: dell64 sat cordoned and
        idle for a whole roll while `coord release propagate` rolled the
        other three hosts fine and said nothing about it — because the run
        that found nothing to attempt (`if not rolls:`) exited via
        `_finish()` before ever reaching step 5, so the CRIT this same
        run's own `before` sweep already had in hand (dell64 on 0.5.335,
        expected 0.5.341) never got scored or printed. It surfaced only
        because an operator happened to run `coord release verify`
        separately.

        Calling this wherever `record.lanes` reflects "nothing attempted so
        far" is always safe: :func:`~coord.release_propagate.
        attempted_scope` only counts rows this run actually rolled
        (``ok`` is not ``None`` and not flagged ``unrollable``), so at any
        point before the roll loop it is empty by construction and every
        CRIT the report carries scores as advisory — the exact shape the
        post-roll gate already knew how to report, just reached sooner.
        """
        gate = rp.scope_verification(report.to_dict(), lanes=record.lanes)
        record.verification = report.to_dict()
        record.gate = gate.to_dict()
        for finding in gate.advisory:
            host = finding.get("host")
            # #2403: elitebook sat on 0.5.146 (expected 0.5.148) for the
            # length of a cordon's full lifetime with this line as the ONLY
            # signal an operator got — "fix by hand" with no hand to
            # follow. When the finding names a host, name the two commands
            # that actually clear it, so nobody has to derive them from
            # scratch under time pressure.
            #
            # #2963: that fixed remedy is `coord agent update` — which
            # swaps the venv and nothing else — for EVERY advisory finding,
            # including ones on the `units` lane (`unit
            # coord-agent.service`, #1831/#1927). `coord agent update`
            # never installs a unit file, so printing it for a stale-unit
            # finding sent an operator to run the one command guaranteed
            # not to fix it (#2938's `Restart=always` fix shipped in four
            # releases and reached zero hosts' live systemd). The units
            # lane's own health check (`coord.health.checks.unit_drift`)
            # already computed the exact per-host, per-unit remedy into
            # `detail` — reuse it here instead of fabricating a wrong one.
            lane = str(finding.get("lane") or "")
            if rp.verify_lane_kind(lane) == rp.LANE_UNITS:
                own_detail = str(finding.get("detail") or "").strip()
                remedy = (
                    f" — {own_detail}"
                    if own_detail
                    else " — run `coord release propagate --lane units` to "
                    "install it (mask-safe since #2812)"
                )
            else:
                remedy = (
                    f" — run `coord agent update --machine {host}` then "
                    f"`coord release cordon --clear {host}`"
                    if host
                    else ""
                )
            click.echo(
                f"  ~ advisory [{finding.get('severity')}] {host} "
                f"{finding.get('lane')}: {finding.get('summary')} "
                f"— outside propagation's reach, fix by hand{remedy}",
                err=True,
            )
        if gate.unrollable:
            click.echo(
                "  ~ lanes with no channel from this host: "
                + ", ".join(gate.unrollable),
                err=True,
            )
        return gate

    # ── 1. what version are we propagating? ──────────────────────────────
    index_url = getattr(getattr(config, "health", None), "pypi_index_url",
                        "https://pypi.org/simple")
    resolved, warning = _resolve_expected(
        target, use_pypi=not target, index_url=index_url, timeout=10.0
    )
    if warning:
        click.echo(f"warning: {warning}", err=True)
    record.target_version = rp.normalize_version(resolved)
    if not record.target_version:
        record.error = (
            "could not resolve a target version — pass --target, or fix "
            "access to the PyPI simple index"
        )
        _finish(rp.STATUS_FAILED, 1)

    # ── 2. is there a window? (fleet + per-host, #2067) ──────────────────
    board, board_error = _fetch_board()
    extra_busy = []
    if board_error:
        extra_busy.append(
            rp.Busy(kind="board unreadable", subject="/board", detail=board_error)
        )
    # #2228: the board can't see an interactive session (no /assign POST) —
    # feed assess_quiescence's extra_busy seam from the same fleet-wide tmux
    # probe `coord sessions --remote` uses, so a live session defers a roll
    # exactly like a live headless assignment does.
    extra_busy.extend(_interactive_session_busy(config))
    # #2174: nor can it see `coord pause`/quiet-hours state — feed the same
    # seam from the daemon-aware pause store, so an operator-paused machine
    # defers exactly like a live assignment does instead of reading idle.
    extra_busy.extend(_paused_machine_busy(config))
    # #2596: nor can it see a #2464 confirmation running in the notify
    # drain — a restart landing mid-confirmation is exactly what let a
    # SIGTERM get read as a test failure on 2026-08-22 (the confirmation
    # itself now refuses that read, #2527, but the restart still shouldn't
    # have happened at all). Feed the same seam so it defers like a live
    # assignment instead of getting reaped.
    extra_busy.extend(_confirmation_running_busy())
    # #2854: `now` opts this run into the between-legs settle window — a
    # drive-queue row that is `running` but has genuinely had no live
    # assignment on its last-known host for at least the settle window rolls
    # that host without waiting for the row to reach a terminal state.
    quiescence = rp.assess_quiescence(
        queue_entries=board.get("drive_queue") or [],
        assignments=board.get("assignments") or [],
        issues=board.get("issues") or [],
        extra_busy=extra_busy,
        now=time.time(),
    )
    record.quiescence = quiescence.to_dict()
    if quiescence.stale:
        # #2110: a `running` row this assessment could disprove (its issue is
        # merged/closed) — not a busy signal, but not silent either. Printed
        # unconditionally, not just under `--json`, so a plain journal read
        # shows the fleet self-corrected a stale row instead of that fact
        # only ever existing inside `record.quiescence["stale"]`.
        click.echo(
            "note: ignoring stale drive-queue row(s) whose issue already "
            f"landed: {', '.join(quiescence.stale)} (run `coord drive-queue "
            "tick --reconcile-only` to clear them for good)",
            err=True,
        )
    if quiescence.settled:
        # #2854: a `running` row whose host rolled BETWEEN LEGS — the row
        # itself is still in flight, only the settled host is being treated
        # as idle. Printed unconditionally, same reasoning as `stale` above:
        # a host rolled mid-drive must leave a readable trace of why that was
        # judged safe.
        click.echo(
            "note: rolling host(s) for drive-queue row(s) that are between "
            f"legs and past the settle window: {', '.join(quiescence.settled)} "
            "(#2854 — the row itself is still running; only the currently "
            "idle host is being treated as quiescent)",
            err=True,
        )

    hosts = [m.name for m in (getattr(config, "machines", ()) or ())]
    busy_hosts = quiescence.busy_hosts()
    # #2067: a signal that cannot be pinned to a host (the board itself
    # unreadable, a drive-queue entry with no recorded launch host) has to
    # block every host — and so does every configured host individually
    # being occupied, which is the same outcome by a different route.
    fully_busy = bool(quiescence.fleet_wide_busy) or (
        bool(hosts) and busy_hosts.issuperset(hosts)
    )
    if quiescence.busy and force:
        click.echo(
            "warning: --force — rolling over a BUSY fleet; in-flight "
            f"headless workers will be killed ({quiescence.reason})",
            err=True,
        )
        busy_hosts = set()  # --force overrides per-host busyness too

    # ── 3. who still needs it, and in what order? ────────────────────────
    #
    # #2101: this sweep used to sit BELOW the "fleet fully busy → defer"
    # return, because a run that could roll nothing had nothing to learn from
    # it. That is no longer true: a busy fleet is exactly the fleet that needs
    # cordoning, and cordoning needs to know who is behind. The cost is one
    # `/health` sweep (10s, parallel) on a tick that would otherwise have
    # returned immediately — paid so a deferral can make the NEXT run
    # different from this one instead of repeating it forever.
    machine_health, unreachable, daemon_facts, daemon_label = rv.gather(
        config, timeout=10.0
    )
    before = rv.verify(
        machine_health=machine_health, unreachable=unreachable,
        daemon_host=daemon_facts, daemon_host_name=daemon_label,
        expected=record.target_version,
    )
    current = rp.hosts_already_current(_lane_versions_by_host(before), record.target_version)

    # #2176: resolved here, ahead of cordoning, so the cordon plan can honour
    # the same daemon-leads invariant the roll itself is bound by (see below).
    # Purely a derivation from data already in hand (`machine_health`) — this
    # does not move the "unorderable fleet" REFUSAL, which stays below the
    # deferral branch on purpose (see its own comment).
    daemon_name = _daemon_machine_name(config, daemon_host_override, machine_health)

    # ── 3a. #2583 min-releases-behind gate ────────────────────────────────
    #
    # Below this threshold, this run is a REPORTED no-op. Checked here —
    # after the daemon's own version is known, but BEFORE cordoning (3b) and
    # BEFORE the busy-fleet defer branch below — so a held run genuinely
    # cordons nothing and touches no host (the #2583 acceptance bar).
    # `effective_min_behind <= 1` (the default: no `propagation:` block, no
    # `--min-behind`) skips the PyPI read entirely — any delta at all rolls,
    # exactly like before this gate existed.
    effective_min_behind = _resolve_min_behind(min_behind_override, config)
    record.min_releases_behind = effective_min_behind
    if effective_min_behind > 1:
        daemon_current = (
            _python_lane_versions(before, [daemon_name], record.target_version)
            .get(daemon_name)
            if daemon_name else None
        )
        behind, behind_warning = _releases_behind_count(
            daemon_current, index_url=index_url, timeout=10.0
        )
        record.releases_behind = behind
        if behind_warning:
            click.echo(f"warning: {behind_warning}", err=True)
        if behind is not None and behind < effective_min_behind:
            # `render_record` (called by `_finish` below) prints the
            # "holding: N behind, threshold M" line itself — same pattern
            # `STATUS_DEFERRED`'s "window: ..." reason uses, one place that
            # renders a status's own detail rather than a second echo here
            # that could drift from it.
            _finish(rp.STATUS_HOLDING, 0)

    # ── 3b. cordon the hosts that are behind, so they DRAIN (#2101) ──────
    #
    # Before the deferral return below, on purpose: cordoning is the thing
    # that turns "no window" into "a window in a few minutes". A run that
    # defers without cordoning has changed nothing about why it deferred.
    #
    # #2176: a non-daemon host with no busy signal of its own is exempted
    # while the daemon host is itself busy and behind — see `plan_cordons`'s
    # `daemon_host` handling. It cannot roll ahead of a busy daemon host
    # regardless of its own drain state (the daemon-leads invariant below),
    # so cordoning it protects nothing and just spends fleet capacity for an
    # unbounded wait.
    record.cordons = _apply_cordons(
        hosts=hosts,
        report=before,
        target_version=record.target_version,
        busy_reasons={h: quiescence.busy_reason_for_host(h) for h in hosts},
        enabled=do_cordon,
        threshold=cordon_after,
        ttl_seconds=cordon_ttl,
        drain_deadline=drain_deadline,
        dry_run=dry_run,
        state_dir=state_dir,
        max_deferrals=cordon_max_deferrals,
        release_cooldown=cordon_cooldown,
        daemon_host=daemon_name,
        # #2373: lets a blown drain deadline ask the escalated host's own
        # agent to resolve the #1870 cross-host liveness ambiguity locally
        # before the loud message goes out — see `_reconcile_launch_host`.
        machines=getattr(config, "machines", ()) or (),
        agent_port=AGENT_PORT,
        # #3047 review: suppresses the plan's human-readable render() lines
        # on stdout when `_drain_quiet` — see `_out` above.
        quiet=_drain_quiet,
    )

    if fully_busy and not force:
        # The single most important line in this command: a deferral is a
        # normal, recorded, exit-0 outcome. A timer that defers all night
        # must be visibly *working*, not visibly failing. #2101: and now it
        # has cordoned whatever is behind on the way past, so the next tick
        # meets a fleet that is actually draining.
        #
        # #3048: score `before` — the sweep already paid for above, ahead
        # of cordoning — before finishing, so a host this run could never
        # have reached anyway (busy for a reason that will never resolve
        # on its own) prints its CRIT and remedy NOW rather than staying
        # invisible until an operator happens to run `coord release
        # verify` or `coord status` separately.
        _scope_gate(before)
        _finish(rp.STATUS_DEFERRED, 0)

    # #2101: this refusal is checked AFTER the deferral above, deliberately.
    # This refusal is about the ORDER a roll happens in; a run that is going
    # to roll nothing has no order to get wrong, and turning "the fleet is
    # busy" into a `failed` record (exit 1, systemd marks the unit failed)
    # would teach an operator to ignore the one signal that matters.
    # (`daemon_name` itself was resolved earlier, before cordoning — #2176.)
    if daemon_name is None and len(hosts) > 1:
        # #2052 fault 2: this used to warn and roll in coordinator.yml order.
        # It then briefly put the daemon host BEHIND both its callers during a
        # partial revert — the documented 405 hazard the warning itself named.
        # Ordering is the one thing protecting against that, so an unorderable
        # run refuses. It is not a failure of the fleet, but it is a failure of
        # this run, and a recorded one.
        record.error = (
            "could not identify which machine runs coord-serve, and this "
            "fleet has more than one host — REFUSING to roll. The lane order "
            "(daemon first) is the only thing preventing the documented 405, "
            "and rolling in coordinator.yml order is a guess, not an order. "
            "Fix the daemon host's /health so its coord-serve unit is "
            "visible, or pass --daemon-host <machine>."
        )
        click.echo(f"error: {record.error}", err=True)
        _finish(rp.STATUS_FAILED, 1)

    # #2067: the daemon must lead every python-lane roll (see the module
    # docstring's LANE ORDER section) — if it is itself occupied and not
    # already on the target, nothing may roll ahead of it, because that
    # would put a caller on a newer `coord` than the daemon it talks to
    # (the documented 405). This is the one case a per-host window still
    # has to defer the WHOLE run rather than just skip the busy host.
    if daemon_name in busy_hosts and daemon_name not in current:
        # #2587: this is exactly the deadlock `coord release nightly-window`
        # exists to route around — queue the SAME roll-pending marker here
        # too, so a plain periodic `coord-release-propagate.timer` run also
        # arms the drive-queue tick's own inter-drive-gap trigger instead of
        # requiring an operator to separately reach for `nightly-window`.
        # #2870: stamp THIS run's own already-passed `effective_min_behind`
        # (resolved above, 3a) onto the marker, so whatever discharges it is
        # gated at the threshold that armed it, not a threshold re-resolved
        # from scratch later.
        # #2889 item 2: `quiescence` was already computed above (3) — reuse
        # it rather than re-fetching the board, to tell a genuine
        # drive-queue occupancy apart from the OTHER busy reasons that put
        # the daemon in `busy_hosts` (see `_queue_provably_busy`'s
        # docstring). Only affects a genuinely FRESH arm.
        _ensure_roll_pending_marker(
            record.target_version, reason="propagate",
            min_releases_behind=effective_min_behind, dry_run=dry_run,
            queue_provably_busy=_queue_provably_busy(quiescence, daemon_name),
        )
        # #3048: this defer holds back EVERY host's lanes this tick, not
        # just the daemon's — score `before` now so any of them already
        # outside propagation's reach says so immediately instead of
        # waiting for a tick that finally reaches step 5.
        _scope_gate(before)
        _finish(rp.STATUS_DEFERRED, 0)

    still_busy = busy_hosts - set(current)
    rolls = rp.plan_lanes(
        daemon_host=daemon_name,
        hosts=hosts,
        lanes=lane_filter or rp.ALL_LANES,
        skip_hosts=set(current) | busy_hosts,
    )
    for host in current:
        record.lanes.append(
            {"lane": "-", "host": host, "ok": None,
             "detail": f"already on v{record.target_version}"}
        )
    for host in sorted(still_busy):
        # #2067: the whole point — a busy host defers on its own, it does
        # not hold every OTHER host hostage. A re-run resumes it, same as
        # an unreachable host or a failed daemon roll does today.
        record.lanes.append(
            {"lane": "-", "host": host, "ok": None,
             "detail": f"deferred — {quiescence.busy_reason_for_host(host)}"}
        )
    for host, reason in sorted(unreachable.items()):
        record.lanes.append(
            {"lane": "-", "host": host, "ok": False, "detail": f"unreachable: {reason}"}
        )

    if dry_run:
        for roll in rolls:
            # #2898: the channel travels with the plan entry so `--dry-run`
            # names both channels distinctly (rendered by
            # `rp.render_record`), rather than showing a `tui` lane under a
            # coordinator version it does not actually roll to.
            record.lanes.append(
                {"lane": roll.lane, "host": roll.host, "ok": None,
                 "channel": roll.channel,
                 "detail": f"would roll ({roll.rationale})"}
            )
        if rolls:
            _finish(rp.STATUS_ROLLED, 0)
        _finish(rp.STATUS_DEFERRED if still_busy else rp.STATUS_UP_TO_DATE, 0)

    if not rolls:
        # #3048: dell64 sat cordoned and idle, behind the target, while
        # this exact branch fired tick after tick — every OTHER behind
        # host had already rolled and gone `current`, so `rolls` came back
        # empty and the run finished right here without ever reaching step
        # 5's gate. Score `before` now, the same sweep already paid for in
        # step 3 (before cordoning), so a host stuck outside propagation's
        # reach says so on THIS tick instead of staying silent until an
        # operator happens to run `coord release verify` by hand.
        _scope_gate(before)
        _finish(rp.STATUS_DEFERRED if still_busy else rp.STATUS_UP_TO_DATE, 0)

    # ── 4. roll, in the planned order ────────────────────────────────────
    by_name = {m.name: m for m in (getattr(config, "machines", ()) or ())}
    updated_hosts: list[str] = []
    local_name = _local_machine_name(config)

    # #1835 review: plan_lanes() puts the daemon host's python lane first
    # specifically so "a caller must never reach an endpoint its daemon
    # predates" holds — but that is only true if a failure there actually
    # stops every other host's python lane from rolling forward. Without
    # this, a failed daemon roll left the loop free to advance every other
    # host to target_version anyway, reproducing the documented 405 skew
    # for the rest of this run (up to --timeout seconds per remaining
    # host) until the final `coord release verify` gate caught it — or,
    # with --no-verify, not at all. So this is an enforced precondition,
    # not just an ordering suggestion: once the daemon's own python roll
    # fails, every other host's python lane is skipped outright.
    #
    # #2095 review: this used to be set from `_roll_python`'s own overall
    # `ok`, which #2095 correctly made `False` whenever ANY restarted
    # sibling failed — including coord-web, which has nothing to do with the
    # 405 hazard this flag exists to prevent (that hazard is specifically
    # "a caller running ahead of a daemon whose coord-serve hasn't reached
    # target_version yet"). Reusing that aggregate here meant a coord-web-
    # only failure on the daemon host — coord-serve itself restarts and
    # reports target_version fine — would ALSO halt every other host's
    # python lane for the rest of the run: a materially larger blast radius
    # than before #2095, and exactly the shape of the 2026-08-10 incident
    # this issue is about (dellserver's coord-serve was fine; coord-web was
    # what failed). `_roll_python` now reports `serve_unit_ok` separately —
    # whether coord-serve ITSELF is confirmed on target_version — and that,
    # not the lane's own `ok`, is what decides this.
    daemon_python_failed = False

    for roll in rolls:
        machine = by_name.get(roll.host)
        if machine is None:
            record.lanes.append({"lane": roll.lane, "host": roll.host, "ok": False,
                                 "detail": "not in coordinator.yml"})
            continue
        if roll.host in unreachable:
            record.lanes.append({"lane": roll.lane, "host": roll.host, "ok": False,
                                 "detail": "skipped — host unreachable"})
            continue

        if (
            roll.lane == rp.LANE_PYTHON
            and daemon_python_failed
            and roll.host != daemon_name
        ):
            # Not a failure of *this* host — it was simply never attempted,
            # because attempting it would put it ahead of a daemon that
            # cannot yet serve it. A re-run after the daemon is fixed
            # should resume here, not treat this host as needing rollback.
            detail = (
                "not attempted — daemon host's python lane failed; rolling "
                "this host first would reproduce the 405 skew the lane "
                "order exists to prevent"
            )
            record.lanes.append({"lane": roll.lane, "host": roll.host, "ok": None,
                                 "detail": detail})
            _out(f"  · {roll.label}: {detail}")
            continue

        if roll.lane == rp.LANE_PYTHON:
            ok, detail, serve_unit_ok = _roll_python(
                machine, target_version=record.target_version,
                agent_port=AGENT_PORT, timeout=timeout, force=force,
            )
            if ok:
                updated_hosts.append(roll.host)
            elif roll.host == daemon_name and not serve_unit_ok:
                daemon_python_failed = True
        elif roll.lane == rp.LANE_UNITS:
            ok, detail = _roll_units(machine, agent_port=AGENT_PORT)
        else:
            # #2898: no target_version — the tui lane resolves its own
            # channel's latest (see _roll_tui). record.target_version names a
            # tag in the coordinator's channel, which coord-tui's Releases
            # have never heard of.
            ok, detail = _roll_tui(machine, local_name=local_name)

        # #2052: `ok is None` from a lane executor means "there is no channel
        # for this lane on this host" — not a failure, and emphatically not
        # something the post-roll gate may hold this run to. The remote
        # coord-tui binary is the canonical case: propagation itself reports
        # there is no remote install path, so counting its staleness as
        # grounds for rolling back a good python roll is a category error.
        entry = {"lane": roll.lane, "host": roll.host, "ok": ok,
                 "channel": roll.channel, "detail": detail}
        if ok is None:
            entry["unrollable"] = True
        record.lanes.append(entry)
        _out(f"  {'·' if ok is None else ('✓' if ok else '✗')} "
             f"{roll.label}: {detail}")

    # ── 4b. uncordon what just rolled, immediately (#2101) ───────────────
    #
    # Immediately, and not after the verify gate below: the host is on the
    # target version and its agent has re-execed, so there is nothing left to
    # drain for and every extra second of cordon is work the fleet is not
    # doing. If verification then comes back red and rolls the host back, the
    # NEXT run re-cordons it — one loop, converging, rather than a cordon
    # whose lifetime is coupled to an unrelated gate.
    _uncordon_hosts(updated_hosts, record.cordons, quiet=_drain_quiet)

    # ── 5. the final gate ────────────────────────────────────────────────
    if not do_verify:
        _finish(rp.STATUS_ROLLED, 0)

    machine_health, unreachable, daemon_facts, daemon_label = rv.gather(
        config, timeout=10.0
    )
    after = rv.verify(
        machine_health=machine_health, unreachable=unreachable,
        daemon_host=daemon_facts, daemon_host_name=daemon_label,
        expected=record.target_version,
    )

    # #2052: the gate is scoped to the lanes this run attempted and could
    # have moved. The full report above is still journalled verbatim — this
    # narrows what may TRIGGER a rollback, not what gets reported.
    gate = _scope_gate(after)

    if gate.red and rollback_on_red:
        # #1835: "a red post-deploy verification must roll back, not just
        # report." Only the hosts THIS run updated — rolling back a host we
        # never touched would undo somebody else's deliberate state.
        from coord.agent_update import cli_initiator  # noqa: PLC0415

        down: list[str] = []
        for host in updated_hosts:
            machine = by_name.get(host)
            if machine is None:
                continue
            ok, detail = _rollback_host(
                machine, agent_port=AGENT_PORT, timeout=min(timeout, 120.0),
                initiator=cli_initiator(
                    f"coord release propagate -> {machine.name} rollback (red gate)"
                ),
            )
            record.rolled_back.append(f"{host}: {detail}")
            if not ok:
                down.append(host)
            _out(f"  {'↩' if ok else '✗'} rollback {host}: {detail}")
        # #2052 fault 1: a rollback that stops a service and does not restore
        # it leaves the fleet WORSE off than the failed roll did — precision's
        # coord-agent sat `inactive (dead)` until a human noticed. If any host
        # did not come back, that is the headline, not a footnote.
        if down:
            record.error = (
                "ROLLBACK LEFT AGENTS DOWN: "
                + ", ".join(down)
                + " — these hosts answered the rollback but never came back "
                "on /health, and an SSH `systemctl --user restart "
                "coord-agent` did not revive them either. Recover by hand "
                "before anything else."
            )
            click.echo(f"error: {record.error}", err=True)
        _finish(rp.STATUS_ROLLED_BACK, 2)

    if gate.red:
        _finish(rp.STATUS_FAILED, 1)

    # ── 6. release the deploy gates that were waiting for this ───────────
    # Reaching this line means `after.severity != "crit"` — both crit
    # branches above already exit — so this roll is, definitionally, verified.
    for key in rp.holds_to_release(quiescence, verified=True):
        if not release_holds:
            _out(f"  · deploy gate {key} left held (--no-release-holds)")
            continue
        ok, detail = _release_hold(key)
        if ok:
            record.released_holds.append(key)
        _out(f"  {'✓' if ok else '✗'} release deploy gate {key}: {detail}")

    _finish(rp.STATUS_VERIFIED, 0)


def _sleep(seconds: float) -> None:
    """`time.sleep`, indirected so `--drain`'s tests never really wait (#3047).

    Matches the module's existing style of monkeypatchable module-level
    functions (`_fetch_board`, `_post`, `_interactive_session_busy`) rather
    than reaching for `time.sleep` directly inside `_run_drain`.
    """
    import time  # noqa: PLC0415

    time.sleep(max(0.0, seconds))


def _drain_remaining_hosts(record: "rp.PropagationRecord") -> set[str]:
    """Hosts *record* says are still behind the target after one attempt
    (#3047).

    The union of everything :func:`_apply_cordons` counts as "not yet
    drained": actively cordoned, spared only because the daemon host itself
    blocks them from rolling ahead of it (#2176's ``collateral_spared``), or
    held off only by the #2240 deadlock cooldown (``stuck_in_cooldown``) —
    minus whatever THIS SAME attempt uncordoned after rolling it.

    That subtraction matters: ``_uncordon_hosts`` mutates the very same
    ``record.cordons`` dict :func:`_apply_cordons` built earlier in the same
    attempt, so a host that was cordoned at the start of an attempt and
    rolled+uncordoned before that attempt finished shows up in BOTH
    ``cordoned`` (the early plan) and ``uncordoned`` (the late roll) — never
    only the latter. Reading ``cordoned`` alone would therefore report a host
    as still outstanding on the very attempt that finished it.

    #3047 review: also folds in ``unknown`` — hosts whose version this
    attempt could not read, so ``plan_cordons`` left their existing cordon
    exactly as-is rather than proving it either current or behind. Failing
    CLOSED here (counting them as still remaining) is deliberate: without it,
    a host that stays unreadable for an entire ``--drain`` session is
    invisible to this function, so the loop can report "every host reached
    the target" and exit 0 while that host's real state was never confirmed
    — and its stale cordon, no longer renewed once ``--drain`` has walked
    away, would eventually lapse and reopen for new work still on the old
    version.
    """
    cordons = record.cordons or {}
    remaining = (
        set(cordons.get("cordoned") or [])
        | set(cordons.get("collateral_spared") or [])
        | set(cordons.get("stuck_in_cooldown") or [])
        | set(cordons.get("unknown") or [])
    )
    return remaining - set(cordons.get("uncordoned") or [])


def _run_drain(
    *,
    deadline_seconds: float,
    poll_seconds: float,
    **attempt_kwargs: Any,
) -> None:
    """`coord release propagate --drain` (#3047 part 2): keep polling for a
    quiescent window instead of leaving that to the operator.

    #2854 already stops charging a between-legs row as busy once its gap has
    outlasted the settle window — that mechanism works unmodified; nothing
    here duplicates it. The gap this closes is that NOTHING samples it while
    `coord-release-propagate.timer` is masked for a manual roll: a single
    `coord release propagate` call is overwhelmingly likely to land outside
    the (normally seconds-long) window and report `deferred`, leaving an
    operator re-running the same command by hand until a poll finally lands
    inside it.

    This is exactly that loop, automated: call ``release_propagate`` itself
    again — one attempt at a time, *poll_seconds* apart, `drain=False` so it
    never recurses — renewing this run's own cordons on every pass (each
    attempt already does that on its own; see :func:`_apply_cordons`), until
    :func:`_drain_remaining_hosts` reports nothing left behind, *
    deadline_seconds* elapses, or an attempt comes back with a real failure.

    Never lets a single attempt actually terminate the process: each call is
    ``release_propagate.callback(...)`` — the plain function Click wraps,
    invoked directly rather than through Click's own dispatch — with its
    terminal ``sys.exit`` caught right here. ``_finish`` raises that
    ``SystemExit`` with the finished record attached (``exc.record``) for
    exactly this reason: a resident caller needs to know what one attempt
    did without a second, parallel account of the same run.

    #3047 review: with ``--json`` also set, every per-attempt record is a
    full JSON document — echoing each one (as the pre-review version did)
    interleaves N of them with this loop's own plain-text ``[drain] ...``
    lines, which is not a single parseable JSON stream for a script reading
    stdout. So when ``as_json`` is in *attempt_kwargs*, this function instead
    passes ``_drain_quiet=True`` to every attempt (suppressing its echo),
    routes its own ``[drain] ...`` progress lines to stderr, and prints
    exactly ONE aggregated JSON summary to stdout right before exiting —
    see the local ``_emit_summary`` helper below.
    """
    import json as _json  # noqa: PLC0415
    import time  # noqa: PLC0415

    from coord import release_propagate as rp  # noqa: PLC0415

    json_mode = bool(attempt_kwargs.get("as_json"))
    start = time.time()

    def _echo(message: str, *, force_stderr: bool = False) -> None:
        # #3047 review: in `--json` mode every `[drain] ...` progress line
        # moves to stderr so stdout carries only the final aggregated JSON
        # summary below. Outside `--json` mode this is unchanged from
        # before — plain-text progress lines on stdout as they happen.
        click.echo(message, err=(json_mode or force_stderr))

    def _emit_summary(
        *, drain_status: str, attempt: int, remaining: set[str], record: "rp.PropagationRecord | None"
    ) -> None:
        if not json_mode:
            return
        click.echo(_json.dumps(
            {
                "drain_status": drain_status,
                "attempts": attempt,
                "elapsed_seconds": round(time.time() - start, 3),
                "remaining": sorted(remaining),
                "last_attempt": record.to_dict() if record is not None else None,
            },
            indent=2, sort_keys=True,
        ))

    attempt = 0
    last_remaining: set[str] | None = None
    while True:
        attempt += 1
        if attempt > 1 and time.time() - start > deadline_seconds:
            stragglers = ", ".join(sorted(last_remaining or ())) or "(unknown)"
            _echo(
                f"[drain] --give-up-after deadline of {deadline_seconds:.0f}s reached "
                f"after {attempt - 1} attempt(s) — still behind: {stragglers}",
                force_stderr=True,
            )
            _emit_summary(
                drain_status="give_up_after_exceeded", attempt=attempt - 1,
                remaining=last_remaining or set(), record=None,
            )
            sys.exit(1)

        try:
            release_propagate.callback(  # type: ignore[misc]
                drain=False, deadline_seconds=0.0, drain_interval_seconds=0.0,
                _drain_quiet=json_mode,
                **attempt_kwargs,
            )
            record = None
            exit_code = 0
        except SystemExit as exc:
            record = getattr(exc, "record", None)
            exit_code = exc.code if isinstance(exc.code, int) else 0

        if record is None:
            # `release_propagate` always reaches `_finish` on every path, so
            # this should be unreachable — but a caller this loop cannot
            # interpret must never spin forever on it either.
            _emit_summary(
                drain_status="no_record", attempt=attempt,
                remaining=last_remaining or set(), record=None,
            )
            sys.exit(exit_code)

        remaining = _drain_remaining_hosts(record)
        status = record.status
        if last_remaining is None:
            suffix = f" — still behind: {', '.join(sorted(remaining))}" if remaining else ""
            _echo(f"[drain] attempt {attempt}: {status}{suffix}")
        else:
            for host in sorted(last_remaining - remaining):
                _echo(f"[drain] {host}: reached the target")
            for host in sorted(remaining - last_remaining):
                _echo(f"[drain] {host}: now behind (cordoned)")
            if status != rp.STATUS_DEFERRED or not remaining:
                _echo(f"[drain] attempt {attempt}: {status}")
        last_remaining = remaining

        if status in (rp.STATUS_FAILED, rp.STATUS_ROLLED_BACK):
            _echo(
                f"[drain] stopping — attempt {attempt} came back {status}",
                force_stderr=True,
            )
            _emit_summary(
                drain_status=status, attempt=attempt, remaining=remaining, record=record
            )
            sys.exit(exit_code)
        if status == rp.STATUS_HOLDING:
            _echo("[drain] holding — not enough drift to roll yet (--min-behind)")
            _emit_summary(
                drain_status="holding", attempt=attempt, remaining=remaining, record=record
            )
            sys.exit(0)
        if not attempt_kwargs.get("do_cordon"):
            # #3047 review: this branch MUST be checked before the `not
            # remaining` branch below, and on its own — not folded into it —
            # because `_drain_remaining_hosts` reads `record.cordons`, which
            # `_apply_cordons` never populates at all with `--no-cordon`
            # (`plan_cordons(enabled=False, ...)` short-circuits before ever
            # touching `cordoned`/`collateral_spared`/`stuck_in_cooldown`/
            # `unknown`). `remaining` is therefore the empty set on EVERY
            # `--no-cordon` attempt, deferred or not. Checking `not remaining`
            # first — as an earlier version of this loop did — made THIS
            # branch dead code and reported "every host reached the target"
            # on attempt 1 even when a host was still deferred and had
            # rolled nothing (caught by the #3047 review's own request for a
            # test of this exact branch). Best this loop can do without
            # cordon bookkeeping is stop at the first attempt that managed to
            # roll something, rather than spin on a signal it structurally
            # cannot read — and keep polling (below) while every host is
            # still deferred, same as with cordoning on.
            if status != rp.STATUS_DEFERRED:
                _echo(
                    "[drain] --no-cordon: stopping after the first non-deferred "
                    "attempt — no cordon bookkeeping to confirm every host converged"
                )
                _emit_summary(
                    drain_status="no_cordon_stopped", attempt=attempt, remaining=remaining,
                    record=record,
                )
                sys.exit(0)
        elif not remaining:
            _echo(f"[drain] every host reached the target after {attempt} attempt(s)")
            _emit_summary(
                drain_status="converged", attempt=attempt, remaining=remaining, record=record
            )
            sys.exit(0)
        _sleep(poll_seconds)


# ── #2101: the cordon loop's I/O half ───────────────────────────────────────
#
# The decisions live in `coord/release_cordon.py` (pure, clock passed in);
# what lives here is the part that needs a fleet: reading each host's python
# lane out of the `/health` sweep this command already did, writing the
# daemon-backed cordon store, and surfacing a blown drain deadline where an
# operator will actually see it.


def _python_lane_versions(
    report, hosts: list[str], target_version: str | None
) -> dict[str, str | None]:
    """``{host: the OLDEST version its python lane reports}`` (#2101).

    "Python lane" is whatever :func:`coord.release_propagate.verify_lane_kind`
    grades as one — the venv itself plus the live `coord-serve` and
    `coord-agent` processes — rather than a second list that could drift
    from the one the roll and the gate already use.

    Two deliberate readings:

    * the OLDEST readable version wins, because a host is as behind as its
      most stale python lane. A venv that swapped while `coord-serve` (or,
      on the daemon host, `coord-agent` — #2841) still runs the old
      generation is #2069's exact defect, and it must read as "behind", not
      as "done";
    * a host with an unreadable lane and no readable lane BEHIND the target is
      ``None`` — "no data", never "current" (#1834). `None` is what stops the
      host being cordoned on a guess *and* what stops an existing cordon being
      cleared on a failed HTTP call.
    """
    from coord import release_cordon as rc  # noqa: PLC0415
    from coord import release_propagate as rp  # noqa: PLC0415

    seen: dict[str, list[str | None]] = {}
    for lane in report.lanes:
        if rp.verify_lane_kind(lane.lane) != rp.LANE_PYTHON:
            continue
        seen.setdefault(lane.host, []).append(lane.version)

    out: dict[str, str | None] = {}
    for host in hosts:
        versions = seen.get(host) or []
        oldest = _oldest_version(versions)
        if (
            oldest is not None
            and any(v is None for v in versions)
            and rc.version_drift(oldest, target_version) == 0
        ):
            # Some lane could not be read at all, and every lane that COULD be
            # read is already on the target. That is not proof of "current" —
            # the unreadable lane is exactly the one #1834 says will be the
            # one that bites — so report "no data" and leave any existing
            # cordon exactly as it is. (A readable lane that IS behind is
            # proof enough to cordon, and `_oldest_version` already
            # surfaced it.)
            oldest = None
        out[host] = oldest
    return out


def _oldest_version(versions: list[str | None]) -> str | None:
    """The lowest readable version in *versions*, or ``None``.

    String comparison is wrong here (``0.5.9`` sorts above ``0.5.31``), so
    this compares numerically component by component.
    """
    def _key(raw: str) -> tuple[int, ...]:
        parts: list[int] = []
        for chunk in raw.lstrip("vV").split("."):
            digits = ""
            for ch in chunk:
                if not ch.isdigit():
                    break
                digits += ch
            if not digits:
                break
            parts.append(int(digits))
        return tuple(parts)

    readable = [v for v in versions if v]
    if not readable:
        return None
    return min(readable, key=_key)


def _apply_cordons(  # noqa: PLR0912 — one linear apply-the-plan pass
    *,
    hosts: list[str],
    report,
    target_version: str | None,
    busy_reasons: dict[str, str],
    enabled: bool,
    threshold: int | None,
    ttl_seconds: float | None,
    drain_deadline: float | None,
    dry_run: bool,
    state_dir: Path | None = None,
    max_deferrals: int | None = None,
    release_cooldown: float | None = None,
    daemon_host: str | None = None,
    machines=None,
    agent_port: int | None = None,
    reconcile_timeout: float | None = None,
    quiet: bool = False,
) -> dict:
    """Plan and apply this run's cordons. Returns the journal fragment.

    Never raises: a cordon store this run cannot write is recorded as an
    error and the roll continues on its existing (quiescence-based) rules.
    Failing the whole propagation because the cordon could not be renewed
    would make #2101's fix strictly worse than not having it.

    *daemon_host* (#2176) lets :func:`~coord.release_cordon.plan_cordons`
    exempt a non-daemon host with no busy signal of its own from cordoning
    while the daemon host is itself busy and behind — see that function's
    docstring for the reasoning. ``None`` (an unorderable fleet) disables
    the exemption and falls back to cordoning every behind host, same as
    before #2176.

    #2240: *state_dir* is where the propagation journal lives, and the journal
    is where "how many runs in a row has this cordon now deferred?" is stored
    — the process holding the answer in memory is restarted by the roll it is
    gating, so an in-memory counter would reset exactly when the deadlock is
    at its worst. An unreadable journal degrades to "no pressure" (the
    pre-#2240 behaviour) rather than to a spurious release: dropping the
    fleet's cordon because a file could not be read is the wrong direction to
    fail in.

    #2373: *machines*/*agent_port* let each :class:`~coord.release_cordon.
    DrainEscalation` ask the escalated host's OWN agent to run a local
    reconcile-only tick before the loud message goes out — see
    :func:`_reconcile_launch_host`. ``machines=None`` (a caller with no fleet
    topology, e.g. a unit test exercising `plan_cordons` output directly)
    skips the remote call entirely and escalates exactly as before #2373.
    """
    import time  # noqa: PLC0415

    from coord import release_cordon as rc  # noqa: PLC0415
    from coord import release_propagate as rp  # noqa: PLC0415
    from coord.machine_pause import (  # noqa: PLC0415
        clear_cordon,
        cordons as read_cordons,
        set_cordon,
    )

    now = time.time()
    outcome = rc.CordonOutcome()
    try:
        existing = read_cordons()
    except Exception as exc:  # noqa: BLE001 — see docstring
        outcome.errors.append(f"could not read the cordon store: {exc}")
        return outcome.to_dict()

    pressure = rc.DeferralPressure()
    if state_dir is not None:
        try:
            pressure = rc.deferral_pressure(
                rp.read_records(state_dir), target_version=target_version
            )
        except Exception as exc:  # noqa: BLE001 — see docstring
            outcome.errors.append(f"could not read the propagation journal: {exc}")
    outcome.pressure = pressure.to_dict()

    # #2240 review: resolved once and journaled on every run (not just a
    # releasing one) via `outcome.max_deferrals`, so `deferral_pressure()`
    # can read the operator's actual `--cordon-max-deferrals` back out of
    # the newest record instead of `describe_deferral_pressure()` callers
    # silently assuming `DEFAULT_MAX_DEFERRALS`.
    resolved_max_deferrals = (
        rc.DEFAULT_MAX_DEFERRALS if max_deferrals is None else max_deferrals
    )
    outcome.max_deferrals = resolved_max_deferrals

    plan = rc.plan_cordons(
        target_version=target_version,
        host_versions=_python_lane_versions(report, hosts, target_version),
        existing=existing,
        now=now,
        ttl_seconds=(
            rc.DEFAULT_TTL_SECONDS if ttl_seconds is None else ttl_seconds
        ),
        drain_deadline=(
            rc.DEFAULT_DRAIN_DEADLINE_SECONDS
            if drain_deadline is None
            else drain_deadline
        ),
        threshold=(
            rc.DEFAULT_DRIFT_THRESHOLD if threshold is None else threshold
        ),
        busy_reasons=busy_reasons,
        enabled=enabled,
        pressure=pressure,
        max_deferrals=resolved_max_deferrals,
        release_cooldown=(
            rc.DEFAULT_RELEASE_COOLDOWN_SECONDS
            if release_cooldown is None
            else release_cooldown
        ),
        daemon_host=daemon_host,
    )
    if not quiet:
        # #3047 review: suppressed under `--drain --json` (`quiet=True`,
        # threaded from `release_propagate`'s `_drain_quiet`) so stdout
        # carries only `_run_drain`'s own aggregated JSON summary, not this
        # attempt's human-readable cordon plan interleaved with it.
        for line in plan.render():
            click.echo(line)
    outcome.cooling_seconds = plan.cooling_seconds
    outcome.collateral_spared = list(plan.collateral_spared)
    outcome.blocked_behind = plan.blocked_behind
    outcome.stuck_in_cooldown = list(plan.stuck_in_cooldown)
    if plan.released is not None:
        outcome.released = plan.released.to_dict()
        # stderr as well as stdout: on the timer host this line IS the
        # operator's only notice that the fleet just spent 40 minutes
        # cordoned for nothing, and the timer's journal is stderr.
        click.echo(f"warning: {plan.released.message}", err=True)
    if dry_run:
        # `--dry-run` promises to change nothing, cordon store included. The
        # plan above is still printed, so a dry run answers "and what would
        # this do to the fleet's routing?" rather than going silent on it.
        outcome.errors.append("dry-run: cordon store not written")
        return {**plan.to_dict(), **outcome.to_dict()}

    outcome.expired = list(plan.expired)
    for record in plan.cordon:
        try:
            set_cordon(
                record.machine,
                reason=record.reason,
                target_version=record.target_version,
                ttl_seconds=max(0.0, record.expires_at - record.renewed_at),
            )
            outcome.cordoned.append(record.machine)
        except Exception as exc:  # noqa: BLE001 — see docstring
            outcome.errors.append(f"cordon {record.machine}: {exc}")
            click.echo(f"  ✗ cordon {record.machine}: {exc}", err=True)
    for name in plan.uncordon:
        try:
            if clear_cordon(name):
                outcome.uncordoned.append(name)
        except Exception as exc:  # noqa: BLE001 — see docstring
            outcome.errors.append(f"uncordon {name}: {exc}")
            click.echo(f"  ✗ uncordon {name}: {exc}", err=True)

    if plan.released is not None:
        # #2240: stamped even when every clear_cordon() below fails or finds
        # nothing — `released_at` is what resets the counter and starts the
        # cooldown, and a release the journal does not record is a release
        # that happens again on the very next tick, forever.
        outcome.released_at = now
        for name in plan.released.hosts:
            try:
                if clear_cordon(name):
                    outcome.uncordoned.append(name)
            except Exception as exc:  # noqa: BLE001 — see docstring
                outcome.errors.append(f"release cordon {name}: {exc}")
                click.echo(f"  ✗ release cordon {name}: {exc}", err=True)

    machines_by_name = {m.name: m for m in (machines or ())}
    # #2373: each escalation below makes one synchronous `_reconcile_launch_host`
    # HTTP call, serially, each blocking up to ~`DEFAULT_RECONCILE_TIMEOUT_SECONDS`.
    # With multiple simultaneously-overdue hosts this adds up to N * timeout of
    # latency inside one `release propagate` invocation. Fine today — escalations
    # are rare and few at once — but if that ever stops being true, parallelize
    # this loop (e.g. a thread pool over `plan.escalations`) rather than letting
    # a single propagate call silently grow with fleet size.
    for escalation in plan.escalations:
        # #2373: before the loud message, give the escalated host's OWN
        # agent one chance to resolve the ambiguity that is actually holding
        # it — see `_reconcile_launch_host`'s docstring. `machine` missing
        # from the fleet map, or no `agent_port` supplied (a caller with no
        # HTTP topology), skips the call rather than guessing an address.
        reconcile_ok: bool | None = None
        reconcile_detail = ""
        target = machines_by_name.get(escalation.machine)
        if target is not None and agent_port:
            reconcile_ok, reconcile_detail = _reconcile_launch_host(
                target.host,
                agent_port=agent_port,
                timeout=(
                    DEFAULT_RECONCILE_TIMEOUT_SECONDS
                    if reconcile_timeout is None
                    else reconcile_timeout
                ),
            )
        _escalate_drain(escalation, reconcile_ok=reconcile_ok, reconcile_detail=reconcile_detail)
        outcome.escalated.append({
            **escalation.to_dict(),
            "reconcile_ok": reconcile_ok,
            "reconcile_detail": reconcile_detail,
        })

    # #3047 review: `CordonOutcome` (unlike `CordonPlan`) has never tracked
    # `unknown` — hosts whose version this attempt could not read, so their
    # existing cordon was left exactly as-is rather than renewed or cleared
    # (see `plan_cordons`'s docstring). That gap predates this PR and
    # `render_record` doesn't surface it either, but `--drain`'s own
    # convergence check (`_drain_remaining_hosts`) needs it to fail closed
    # instead of declaring victory while a host's real state was never
    # confirmed — so it rides along here rather than through a new
    # `CordonOutcome` field, keeping this change additive to the dict this
    # function already returns.
    return {**outcome.to_dict(), "unknown": list(plan.unknown)}


#: Where a blown drain deadline (#2101 trap C) is recorded, in the same
#: escalation channel `coord drive-queue`'s own alerts use — so it shows up in
#: the TUI's escalations panel and `coord drive escalations` with no new
#: surface to remember to look at. Mirrors
#: `coord.drive_queue.QUEUE_ALERT_REPO`'s pseudo-repo convention.
DRAIN_ALERT_REPO = "(release-cordon)"
DRAIN_ALERT_ISSUE = 0
DRAIN_ALERT_STAGE = "release-cordon"


def _escalate_drain(
    escalation, *, reconcile_ok: bool | None = None, reconcile_detail: str = ""
) -> None:
    """Surface a host that will not drain — loudly, in three places.

    stderr (the timer's journal), the escalation table (the TUI and
    `coord drive escalations`) and the propagation journal. #2101's acceptance
    criterion 4 is explicitly about the surfaced MESSAGE rather than an
    internal state change, because a silent forever-wait is the failure this
    whole mechanism replaces.

    #2373: *reconcile_ok*/*reconcile_detail* are the outcome of the remote
    reconcile-only tick `_apply_cordons` asked the escalated host's own
    agent to run just before calling this — ``None`` when that call was
    never attempted (no fleet topology / agent port available), so the
    message stays byte-identical to before #2373 in that case. When it WAS
    attempted, the outcome is folded into the same message rather than a
    second alert, so an operator reading one escalation sees both "this is
    overdue" and "here is what the self-heal attempt already found" without
    correlating two records.
    """
    reconcile_note = ""
    if reconcile_ok is not None:
        verb = "ok" if reconcile_ok else "failed"
        detail = f" ({reconcile_detail})" if reconcile_detail else ""
        reconcile_note = (
            f" — asked {escalation.machine}'s own agent to run a local "
            f"reconcile-only tick first (#2373): {verb}{detail}"
        )
    message = f"{escalation.message}{reconcile_note}"
    click.echo(f"  ! {message}", err=True)
    try:
        from coord.state import record_drive_escalation  # noqa: PLC0415

        gate_readings = (
            f"machine={escalation.machine} | "
            f"waited={escalation.waited_seconds:.0f}s | "
            f"deadline={escalation.deadline_seconds:.0f}s"
        )
        if reconcile_ok is not None:
            gate_readings += f" | remote_reconcile={'ok' if reconcile_ok else 'failed'}"
        record_drive_escalation(
            DRAIN_ALERT_REPO,
            DRAIN_ALERT_ISSUE,
            stage=DRAIN_ALERT_STAGE,
            reason=message,
            gate_readings=gate_readings,
            proposed_command=escalation.command,
        )
    except Exception as exc:  # noqa: BLE001 — the stderr line above is the
        # floor; an escalation table that cannot be written must not take the
        # message down with it.
        click.echo(f"  (could not record the drain escalation: {exc})", err=True)


def _uncordon_hosts(hosts: list[str], journal: dict, *, quiet: bool = False) -> None:
    """Clear the release cordon on every host in *hosts*, best effort.

    #3047 review: *quiet* (threaded from `release_propagate`'s
    `_drain_quiet`) suppresses only the success line — the error line stays
    on stderr regardless, same as everywhere else `_drain_quiet` reaches.
    """
    if not hosts:
        return
    from coord.machine_pause import clear_cordon  # noqa: PLC0415

    for host in hosts:
        try:
            if clear_cordon(host):
                journal.setdefault("uncordoned", []).append(host)
                if not quiet:
                    click.echo(f"  ✓ uncordon {host}: rolled, work may resume")
        except Exception as exc:  # noqa: BLE001
            journal.setdefault("errors", []).append(f"uncordon {host}: {exc}")
            click.echo(f"  ✗ uncordon {host}: {exc}", err=True)


def _local_machine_name(config) -> str | None:
    """This host's name in ``coordinator.yml``, if it is in there at all."""
    import socket  # noqa: PLC0415

    here = socket.gethostname().split(".")[0].lower()
    for machine in getattr(config, "machines", ()) or ():
        if machine.name.lower() == here:
            return machine.name
        if str(getattr(machine, "host", "")).split(".")[0].lower() == here:
            return machine.name
    return None


def _roll_python(machine, *, target_version: str, agent_port: int, timeout: float,
                 force: bool) -> tuple[bool, str, bool]:
    """POST /update and wait for the agent to actually report the version.

    Success is judged by the version the agent reports, never by "the POST
    was accepted" (#1568: a stale pip index makes a no-op look like a
    success) — the wait loop is ``coord agent update``'s own, reused rather
    than reimplemented so the two can't drift.

    Three-element return, ``(ok, detail, serve_unit_ok)`` (#2095 review):

    * ``ok`` is the whole lane's own verdict, exactly as before — #2095
      correctly made this ``False`` whenever ANY restarted sibling failed,
      coord-web included, so the lane never prints a `✓` over a real
      outage.
    * ``serve_unit_ok`` is narrower and answers a different question: is
      *coord-serve itself* — the unit whose version every other host's
      caller depends on, and the entire reason the main roll loop's
      ``daemon_python_failed`` cascade exists — confirmed to be on
      ``target_version`` and running? It is ``False`` only when the venv
      swap itself never completed (nothing downstream can be trusted
      either) or coord-serve was itself the sibling that failed to
      restart. A coord-web-only (or coord-drive-queue-only) failure
      leaves it ``True``. Reusing ``ok`` for that cascade decision used to
      mean a coord-web outage on the daemon host — coord-serve unaffected —
      also halted every other host's python lane for the rest of the run:
      a materially larger blast radius than before #2095, and exactly the
      2026-08-10 incident's shape (dellserver's coord-serve was fine;
      coord-web was what failed). Callers deciding whether it's safe to
      let OTHER hosts proceed must key off ``serve_unit_ok``, not ``ok``.
    """
    from coord.agent_update import cli_initiator  # noqa: PLC0415
    from coord.commands.agent_ops import (  # noqa: PLC0415
        _fetch_pre_started_at,
        _wait_agents_updated,
    )
    from coord.release_verify import DAEMON_UNIT  # noqa: PLC0415

    pre = _fetch_pre_started_at([machine])
    status, body, error = _post(
        f"http://{machine.host}:{agent_port}/update",
        {
            "target_version": target_version,
            "force": force,
            # #2121: the roll names itself on the target host's audit trail.
            "initiator": cli_initiator(
                f"coord release propagate -> {machine.name} python lane"
            ),
        },
        timeout=15.0,
    )
    if error:
        return False, error, False
    if status == 409:
        # The agent refused: live sessions, or an editable install. Both are
        # correct refusals and neither is this command's to override.
        return False, str(body.get("error") or "refused (409)"), False
    if status != 202:
        return False, f"HTTP {status}", False

    outcomes = _wait_agents_updated(
        [machine], target_version=target_version, timeout=timeout,
        pre_started_at=pre,
    )
    outcome = outcomes.get(machine.name) or {}
    if not outcome.get("matched"):
        return False, str(
            outcome.get("error")
            or f"still reporting v{outcome.get('version_now', '?')} after "
               f"{timeout:.0f}s (last update result: {outcome.get('result')})"
        ), False

    # #2069: /update only ever restarted the agent — coord-serve, coord-web
    # and coord-drive-queue kept running the generation they started with
    # until a human restarted them by hand. This is the rest of the lane,
    # not a separate one: it runs against whichever of those three units the
    # freshly-restarted agent finds actually running on ITS host, so a run
    # that never touches coord-web anywhere still reports "now vX.Y.Z" clean.
    sib_ok, sib_detail, sib_failed = _restart_sibling_services(machine, agent_port=agent_port)
    if sib_ok is not False:
        # True (nothing failed) and None (this agent predates
        # /restart-services entirely — no channel to have restarted anything
        # through, see `_restart_sibling_services`) both still count as a
        # lane success: neither one is a service THIS run took down.
        return True, f"now v{target_version}; {sib_detail}", True
    # #2095: this used to return `True` here too — "the venv swap succeeded"
    # bleeding into "the lane succeeded", printed as a leading `✓` over a
    # line that itself said `FAILED to restart: coord-web`. That is exactly
    # what happened during the 2026-08-10 0.5.15 -> 0.5.26 roll: the phone
    # dashboard went offline and the run reported success. The old comment
    # here claimed `coord release verify` would catch the resulting skew as
    # the justification for staying green — it does not: verify grades
    # *versions*, and there is no coord-web lane in it at all, so a dead
    # service is invisible to the thing named as its backstop. A sibling
    # that failed to (re)start — or restarted but never answered its own
    # liveness probe, see `agent_app._probe_liveness` — is not a lane
    # success, full stop, whatever the venv itself did.
    #
    # `sib_failed` is only populated when the endpoint told us per-unit
    # detail (a real 200/500-with-`units`-body); the opaque-failure branches
    # in `_restart_sibling_services` (a network error reaching the endpoint
    # at all, or a 500 with no body) return it empty because coord-serve's
    # own fate is genuinely unknown there — treated conservatively as NOT
    # confirmed, same as before #2095's per-unit distinction existed.
    serve_unit_ok = (DAEMON_UNIT not in sib_failed) if sib_failed else False
    return False, f"now v{target_version}, but {sib_detail}", serve_unit_ok


def _restart_sibling_services(
    machine, *, agent_port: int, timeout: float = 120.0
) -> tuple[bool | None, str, dict[str, str]]:
    """``POST /restart-services`` — the rest of a python-lane roll (#2069).

    ``/update`` swaps the venv and re-execs *the agent* — and nothing else.
    ``coord-serve``, ``coord-web`` and ``coord-drive-queue`` keep running the
    generation they started with until something restarts them, so this is
    called right after ``/update`` reports success above. Which of the three
    units actually need restarting is decided on the agent side, from what
    it finds running on its own host (see the endpoint's docstring) — this
    function only reports what came back.

    Three-element return: ``(ok, detail, failed)``. ``ok``/``detail`` follow
    the same tri-state convention as the other lane executors below
    (``_roll_units``/``_roll_tui``'s ``ok=None`` "no channel"):

    * ``True`` — the endpoint answered and no unit it touched failed.
    * ``False`` (#2095) — a sibling this run took down and never brought
      back: a real outage, not a cosmetic detail to carry forward under a
      `✓`. DOES fail the python lane — see ``_roll_python``. This used to
      defer to `coord release verify` catching the resulting skew; it
      cannot — verify grades versions, not liveness, and carries no lane
      for these units at all, so relying on it left exactly the outage this
      issue is about invisible to its own named backstop.
    * ``None`` — this host's agent predates the endpoint entirely (HTTP
      404): there is no channel here to have restarted anything through,
      the same "unrollable" shape as a lane with no executor at all, not a
      failure of this roll.

    ``failed`` (#2095 review) is the ``{unit: detail}`` mapping of units
    explicitly confirmed to have failed to restart — empty when ``ok`` is
    not ``False``, and ALSO empty for the opaque-failure branches below (a
    network error reaching the endpoint, or a 500 with no ``units`` body),
    where no individual unit's fate is actually known. ``_roll_python`` uses
    this — not the aggregate ``ok`` — to tell whether coord-serve itself is
    the sibling that failed, which is the only thing its ``serve_unit_ok``
    (and, through it, the main roll loop's daemon-python-failed cascade)
    cares about: a coord-web-only failure must not be indistinguishable from
    a coord-serve one to that caller.
    """
    status, body, error = _post(
        f"http://{machine.host}:{agent_port}/restart-services", {}, timeout=timeout,
    )
    if error:
        return False, f"sibling service restart: {error}", {}
    if status == 404:
        # Pre-#2069 agent build: /restart-services doesn't exist yet. Not
        # this run's failure to have restarted anything through a channel
        # that was never there — `coord agent update --all` is what closes
        # this gap, not a red python lane.
        return None, "agent predates /restart-services (HTTP 404) — update the agent build", {}
    # The endpoint (`agent_app.py`'s `restart_services`) returns HTTP 500 — with the
    # *same* `{"units": {...}}` body shape as 200 — whenever any single unit fails to
    # restart. That is the exact partial-failure path this function exists to report
    # in detail, so a 500 *with a `units` body* must still be parsed below rather than
    # treated as an opaque failure. A 500 WITHOUT a `units` body is a different,
    # genuinely-unexpected failure (an unhandled exception, a proxy error, ...) —
    # Starlette's own default error page carries no such body — and must still
    # short-circuit, or a real crash would be misread as "no sibling units to
    # restart" / a false-positive success.
    if status != 200 and not (status == 500 and "units" in body):
        return False, f"sibling service restart: {body.get('error') or f'HTTP {status}'}", {}

    units = body.get("units") or {}
    restarted = sorted(u for u, r in units.items() if isinstance(r, dict) and r.get("restarted"))
    skipped = sorted(
        u for u, r in units.items() if isinstance(r, dict) and r.get("restarted") is None
    )
    failed = {
        u: (r.get("detail") or "?")
        for u, r in units.items() if isinstance(r, dict) and r.get("restarted") is False
    }
    parts = []
    if restarted:
        parts.append(f"restarted {', '.join(restarted)}")
    if skipped:
        parts.append(f"not running here: {', '.join(skipped)}")
    if failed:
        parts.append(
            "FAILED to restart: "
            + ", ".join(f"{u} ({detail})" for u, detail in sorted(failed.items()))
        )
    if not parts:
        parts.append(body.get("detail") or "no sibling units to restart")
    return not failed, "; ".join(parts), failed


def _roll_units(machine, *, agent_port: int) -> tuple[bool | None, str]:
    """POST /deploy-units — the `deploy/**` lane's deploy step (#1831).

    Returns ``ok=None`` when this host offers no channel for the lane at all
    (see :func:`_roll_tui` and #2052): the run is not accountable for a lane
    it was structurally unable to roll.

    Also names every timer whose *enablement* state this call changed
    (#2124 item 3) — and, separately, every timer it deliberately left
    alone because it was already enabled (see
    :func:`coord.deploy_units.enable_timers`) — so "the queue came back
    mid-roll" (or, after this fix, "it didn't") is legible in this output
    rather than reconstructed afterwards from journal timestamps.

    ``ok`` (#2124 review) reflects every way ``agent_app.py``'s
    ``deploy_units`` endpoint can fail, not just a failed timer: a unit
    whose install itself failed (``action == "failed"`` — e.g. an unreadable
    installed unit file, :mod:`coord.deploy_units` lines ~249/264/284), a
    top-level ``error``, or a ``daemon-reload`` that was attempted and
    failed. Before this check existed, ``ok`` was derived from
    ``failed_timers`` alone, so any of those three could return HTTP 500
    with ``payload["ok"]=False`` and still be recorded here as a green
    lane — exactly the "unconfirmed success" defect #2096 exists to catch.
    """
    from coord.deploy_units import ACTION_FAILED, ACTION_MASKED  # noqa: PLC0415

    status, body, error = _post(
        f"http://{machine.host}:{agent_port}/deploy-units", {}, timeout=30.0
    )
    if error:
        return False, error
    if status in (404, 405):
        # Bootstrap: this agent predates the endpoint. It will have it after
        # the python lane above lands, so this is a *next run* fact, not a
        # failure — recorded rather than swallowed, and NOT grounds for the
        # gate to revert everything else this run got right (#2052).
        return None, ("agent has no /deploy-units yet (predates #1835) — "
                      "the next propagation will roll this lane")
    # The endpoint (`agent_app.py`'s `deploy_units`) returns HTTP 500 — with
    # the SAME body shape as 200 — whenever a daemon-reload or a single
    # timer's enablement fails. That is exactly the partial-failure path
    # this function exists to narrate in detail, so a 500 *with a `units`
    # body* must still be parsed below rather than treated as opaque. A 500
    # WITHOUT a `units` body is a different, genuinely-unexpected failure
    # (an unhandled exception, a proxy error, ...) and must still
    # short-circuit, or a real crash would be misread as "nothing to
    # report" / a false-positive success.
    if status != 200 and not (status == 500 and "units" in body):
        return False, str(body.get("error") or body.get("summary") or f"HTTP {status}")
    units = body.get("units") or []
    changed = [u.get("name") for u in units if u.get("action") == "updated"]
    new = [u.get("name") for u in units if u.get("action") == "new"]
    masked = sorted(u.get("name") for u in units if u.get("action") == ACTION_MASKED)
    failed_units = {
        (u.get("name") or "?"): (u.get("detail") or "?")
        for u in units if u.get("action") == ACTION_FAILED
    }
    parts = []
    parts.append(f"{len(changed)} unit(s) refreshed" if changed else "units already current")
    if body.get("reloaded"):
        parts.append("daemon-reload ok")
    if masked:
        # #2812: an operator-masked unit's content is deliberately left
        # untouched — see `coord.deploy_units.install_units`'s `_is_masked`
        # guard. Reported here, not folded into a `✓` silently, so "which
        # units did this run leave masked" is legible without grepping
        # journal timestamps the way the #2124 carve-outs already are.
        parts.append(
            f"{len(masked)} unit(s) left masked, not refreshed (operator "
            f"mask, #2812): {', '.join(map(str, masked))}"
        )
    if new:
        parts.append(
            f"{len(new)} packaged unit(s) NOT installed here ({', '.join(sorted(map(str, new)))}) "
            "— a release does not decide which services a host runs"
        )
    if failed_units:
        parts.append(
            "FAILED to install unit(s): "
            + ", ".join(f"{u} ({detail})" for u, detail in sorted(failed_units.items()))
        )

    # A daemon-reload is only attempted when a unit's bytes actually
    # changed (`agent_app.py`'s `deploy_units`); "not reloaded" is a
    # failure only when the report itself says a reload was attempted
    # (`body["changed"]`, `InstallReport.changed`) — absent that, "not
    # reloaded" just means there was nothing to reload, never a failure.
    reload_failed = bool(body.get("changed")) and not body.get("reloaded")
    if reload_failed:
        parts.append(
            "daemon-reload FAILED: " + str(body.get("reload_detail") or "?")
        )

    top_level_error = body.get("error")
    if top_level_error:
        parts.append(f"deploy-units error: {top_level_error}")

    timers = body.get("timers_enabled") or {}
    started = sorted(
        name for name, r in timers.items()
        if isinstance(r, dict) and r.get("ok") and r.get("changed")
    )
    # Held timers (confirmed already-enabled, left alone) split on the
    # confirmed `ActiveState` carried in `enable_timers`'s own detail text
    # (coord/deploy_units.py's `already enabled (ActiveState=...)` branch)
    # — NOT on `changed` alone, which is equally true for the #2124 case
    # (operator stopped it) and for the routine, overwhelmingly common case
    # of a timer that was never touched because it is already enabled AND
    # already running. Reporting "left stopped as-is" for the latter names
    # a state (stopped) this call never confirmed — the #2124 review's
    # reporting-accuracy fix.
    held_masked = []
    held_stopped = []
    held_other = []
    for name, r in timers.items():
        if not (isinstance(r, dict) and r.get("ok") and not r.get("changed")):
            continue
        detail = r.get("detail") or ""
        if detail.startswith("masked ("):
            # #2812: `enable_timers`'s own masked branch — checked before
            # the `ActiveState=` regex below, or a masked (always-inactive)
            # timer would fall into `held_stopped` and get mislabeled
            # "already enabled" for a timer that was never enabled at all.
            held_masked.append(name)
            continue
        match = re.search(r"ActiveState=(\w+)", detail)
        if match and match.group(1) == "inactive":
            held_stopped.append(name)
        else:
            held_other.append(name)
    held_masked.sort()
    held_stopped.sort()
    held_other.sort()
    failed_timers = {
        name: (r.get("detail") or "?")
        for name, r in timers.items() if isinstance(r, dict) and not r.get("ok")
    }
    if started:
        parts.append(f"enabled timer(s): {', '.join(started)}")
    if held_masked:
        # An operator's mask, respected rather than fought (#2812) — the
        # units-lane equivalent of `held_stopped` below, one signal
        # stronger.
        parts.append(
            f"left masked as-is (operator mask, #2812): {', '.join(held_masked)}"
        )
    if held_stopped:
        # Confirmed inactive (ActiveState=inactive) and deliberately left
        # alone — the #2124 fix in one word: a timer an operator stopped is
        # still here.
        parts.append(
            f"left stopped as-is (already enabled, #2124): {', '.join(held_stopped)}"
        )
    if held_other:
        # Already enabled and left alone, but NOT confirmed stopped — most
        # commonly a timer that is already enabled and already running
        # normally. Reported distinctly so it never gets read as "the
        # timer I stopped is still stopped" for a timer nobody stopped.
        parts.append(f"already enabled (unchanged): {', '.join(held_other)}")
    if failed_timers:
        parts.append(
            "FAILED to enable timer(s): "
            + ", ".join(f"{u} ({detail})" for u, detail in sorted(failed_timers.items()))
        )
    ok = not (failed_units or reload_failed or top_level_error or failed_timers)
    return ok, "; ".join(parts)


def _roll_tui(machine, *, local_name: str | None) -> tuple[bool | None, str]:
    """`coord tui update` — local host only, and honest about the rest.

    ``coord-tui`` is a binary in each host's ``~/.local/bin``; there is no
    agent endpoint that installs it, so this lane can only roll where this
    command is running. Remote hosts are recorded as an explicit gap rather
    than silently omitted — a lane nobody can see is the lane that bites
    (#1834).

    #2052: that gap returns ``ok=None``, not ``ok=False``. It used to return
    False, which the post-roll gate then read as a failed lane and
    ``--rollback-on-red`` used as grounds to revert three good python rolls.
    A lane that reports "there is no remote install path" in its own failure
    message cannot also be evidence that this run went wrong.

    #2898 — NO ``--version`` ANY MORE, AND THAT IS THE POINT.
    This used to run ``coord tui update --version <this run's target>``, which
    was correct while one tag stamped both repos (#1242). After the #2894
    split that argument names a tag in the *coordinator's* channel, which
    coord-tui's Releases have never heard of: passing it would 404 on every
    run, and the propagation would report a failed tui lane forever for a
    fleet that is in fact perfectly current. Bare ``coord tui update``
    resolves coord-tui's OWN latest release
    (:func:`coord.tui_release.fetch_latest_release_tag`) — that is the
    :data:`~coord.release_propagate.CHANNEL_TUI` channel doing its own version
    selection, not this run guessing across a channel boundary.

    #2981 — AN EMPTY CHANNEL IS NOT A FAILED ROLL.
    A channel with zero releases/tags 404s on every ``coord tui update``
    forever, until someone cuts a first release — that is a fact about
    ``coord-tui``'s Releases, not about whatever this host just attempted.
    ``coord tui update`` exits :data:`~coord.commands.tui.EXIT_EMPTY_CHANNEL`
    specifically for that case (``EmptyReleaseChannelError``, raised at the
    source in :func:`coord.tui_release.fetch_latest_release_tag`), so this
    reads that exact exit code and returns ``ok=None`` — the same "no channel
    to roll" treatment already given to a remote host above, which
    :func:`coord.release_propagate.scope_verification` excludes from the
    gate that ``--rollback-on-red`` acts on. A *genuine* failure (a real
    release exists but the asset/checksum/install step fails) still exits
    ``1`` here and stays ``ok=False`` — still blocking, still eligible to
    trigger a rollback.
    """
    import subprocess  # noqa: PLC0415

    from coord import release_propagate as rp  # noqa: PLC0415
    from coord.commands.tui import EXIT_EMPTY_CHANNEL  # noqa: PLC0415

    if local_name is None or machine.name != local_name:
        return None, (
            f"coord-tui is a per-host binary with no remote install path — run "
            f"`coord tui update` on {machine.name} ({rp.CHANNEL_TUI} channel)"
        )
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "coord.cli", "tui", "update"],
            capture_output=True, text=True, timeout=300,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    if proc.returncode == 0:
        installed = _installed_tui_version(proc.stdout)
        return True, (
            f"coord-tui now {installed} ({rp.CHANNEL_TUI} channel)"
            if installed
            else f"coord-tui updated from the {rp.CHANNEL_TUI} channel"
        )
    detail = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()[:300]
    if proc.returncode == EXIT_EMPTY_CHANNEL:
        return None, (
            f"{rp.CHANNEL_TUI} channel has no published release yet — "
            f"nothing to install, not a roll failure (#2981): {detail}"
        )
    return False, detail


def _installed_tui_version(stdout: str) -> str | None:
    """The version `coord tui update` reported, parsed back out of its output.

    #2898: the caller no longer *chooses* the version — coord-tui's own
    channel does — so the only way to journal what actually landed is to read
    it back. Both of that command's terminal lines carry it: ``coord-tui is
    already vX -- nothing to do`` (the idempotent path) and ``Installed
    <path> -- coord-tui reports X.`` (the install path). Returns ``None``
    rather than guessing if neither matches; the roll still counts as
    successful, it is just journalled without a number.
    """
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if "coord-tui reports " in line:
            return line.split("coord-tui reports ", 1)[1].strip().rstrip(".") or None
        if line.startswith("coord-tui is already "):
            return line.split("coord-tui is already ", 1)[1].split()[0].strip() or None
    return None


def _get(url: str, *, timeout: float) -> tuple[int | None, dict]:
    """GET JSON, tolerantly. ``(status, body)`` — never raises."""
    import httpx  # noqa: PLC0415

    try:
        resp = httpx.get(url, timeout=timeout)
    except Exception:  # noqa: BLE001
        return None, {}
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = {}
    return resp.status_code, (body if isinstance(body, dict) else {})


def _wait_agent_back(machine, *, agent_port: int, timeout: float) -> tuple[bool, str]:
    """Poll ``/health`` until the agent answers again. ``(back, version)``.

    A rollback re-execs the agent process, and #2052 fault 1 is what happens
    when that re-exec does not take: precision's ``coord-agent`` went
    ``inactive (dead)`` at the moment of the rollback and stayed there until
    a human noticed. "The POST was accepted" is therefore not an outcome —
    the outcome is whether the service is serving again.
    """
    import time  # noqa: PLC0415

    deadline = time.time() + max(timeout, 1.0)
    poll = min(2.0, max(timeout / 10, 0.05))
    while True:
        status, body = _get(f"http://{machine.host}:{agent_port}/health", timeout=3.0)
        if status == 200:
            return True, str(body.get("version") or "?")
        if time.time() >= deadline:
            return False, "?"
        time.sleep(poll)


def _rollback_host(
    machine, *, agent_port: int, timeout: float = 90.0, initiator: str | None = None
) -> tuple[bool, str]:
    """POST /rollback — back to the previous blue/green generation (#1241) —
    and then put the service back on its feet.

    #2052 fault 1: "a rollback that stops a service and does not restore it
    leaves the fleet worse off than the failed roll did." This used to return
    True the instant the agent answered 202, which is a statement about the
    *request*, not about the host. It now waits for ``/health`` to answer
    again, escalates once to the documented SSH ``systemctl --user restart
    coord-agent`` (#404/#1568 — ``os.execv`` self-restart does not always
    take under systemd), and only then gives up — loudly, naming the host as
    DOWN rather than reporting a tidy "rolling back".

    #2121: *initiator* names this call on the target host's audit trail, the
    same as ``_roll_python``'s own ``/update`` POST a few lines up — both are
    automation that fires during a fleet roll with nobody at a keyboard, so
    both callers below build one with :func:`coord.agent_update.cli_initiator`
    rather than leaving it to the generic peer/user-agent fallback.
    """
    from coord.commands.agent_ops import _escalate_restart  # noqa: PLC0415

    status, body, error = _post(
        f"http://{machine.host}:{agent_port}/rollback",
        {"force": True, "initiator": initiator} if initiator else {"force": True},
        timeout=30.0,
    )
    if error:
        return False, error
    if status == 404:
        return False, "no previous generation on this host"
    if status != 202:
        return False, str(body.get("error") or f"HTTP {status}")

    back, version = _wait_agent_back(machine, agent_port=agent_port, timeout=timeout)
    if back:
        return True, f"rolled back; agent is serving again on v{version}"

    # The re-exec did not take. This is the documented systemd stall, and it
    # has a documented fix — apply it rather than handing the operator a
    # dead host and a tidy success message.
    escalated = _escalate_restart(machine)
    if escalated:
        back, version = _wait_agent_back(
            machine, agent_port=agent_port, timeout=min(timeout, 60.0)
        )
        if back:
            return True, (
                f"rolled back; agent needed an SSH `systemctl --user restart "
                f"coord-agent` but is serving again on v{version}"
            )
    return False, (
        "rolled back the venv but the agent is DOWN — it never came back on "
        f"/health within {timeout:.0f}s and "
        + (
            "the SSH restart did not revive it"
            if escalated
            else "the SSH `systemctl --user restart coord-agent` escalation "
            "could not run"
        )
        + f". Recover by hand on {machine.name}."
    )


def _release_hold(key: str) -> tuple[bool, str]:
    """``coord drive-queue resume REPO ISSUE`` — the gate the deploy was for.

    The queue's own command takes the pair, not the ``repo#issue`` key, so
    the key is split here rather than a second spelling of "resume this
    gate" being invented alongside it.
    """
    import subprocess  # noqa: PLC0415

    from coord.drive_queue import parse_key  # noqa: PLC0415

    parsed = parse_key(key)
    if parsed is None:
        return False, f"unparseable queue key {key!r}"
    repo, issue = parsed
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "coord.cli", "drive-queue", "resume",
             repo, str(issue)],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    if proc.returncode == 0:
        return True, "queue released"
    return False, (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()[:200]


@release_group.command(
    "cordon",
    help=(
        "Inspect, set or clear release cordons (#2101). A cordoned machine "
        "starts no NEW work — in-flight work is never touched — so it drains "
        "into a state where `coord release propagate` can roll it. "
        "`coord release propagate` manages these automatically; this command "
        "is the operator's window into them and the documented override for a "
        "host that will not drain."
    ),
)
@click.argument("machines", nargs=-1)
@click.option("--clear", "clear", is_flag=True,
              help="Clear the named machines' cordons (or --all of them). "
                   "This lets work resume and LEAVES THE HOST BEHIND — the "
                   "next propagate run will cordon it again unless whatever "
                   "was wedged has been fixed.")
@click.option("--all", "all_machines", is_flag=True,
              help="With --clear: clear every release cordon.")
@click.option("--reason", default="", help="Free text stored with the cordon.")
@click.option("--target", "target_version", default=None,
              help="Version this cordon is draining for; shown in every "
                   "surface that renders the cordon.")
@click.option("--ttl", default=None, type=float,
              help="Seconds before the cordon lapses on its own if nothing "
                   "renews it (default 3600) — see #2101. That bounds a "
                   "propagate run that crashed mid-drain; it does NOT bound "
                   "a healthy one. `coord release propagate` renews this on "
                   "every tick a host is still behind, so a driven issue "
                   "stuck in a long fix loop can hold a cordon for as long "
                   "as that loop runs — see the drain-deadline escalation "
                   "(`coord release propagate --drain-deadline`, #2136) for "
                   "the case this TTL does not cover.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
def release_cordon(
    machines: tuple[str, ...],
    clear: bool,
    all_machines: bool,
    reason: str,
    target_version: str | None,
    ttl: float | None,
    as_json: bool,
) -> None:
    """List (no args), set, or clear release cordons.

    Every cordon carries an owner, a reason, a creation time and an expiry,
    and is stored separately from `coord pause` — so this command can never
    clear a pause an operator set by hand, and `coord unpause` can never lift
    a cordon out from under a drain (#2101 trap A).
    """
    import json as _json  # noqa: PLC0415
    import time  # noqa: PLC0415

    from coord import release_cordon as rc  # noqa: PLC0415
    from coord.machine_pause import (  # noqa: PLC0415
        clear_cordon,
        cordons as read_cordons,
        set_cordon,
    )

    if clear:
        targets = list(machines)
        if all_machines:
            targets = sorted(read_cordons())
        if not targets:
            raise click.ClickException(
                "name at least one machine, or pass --all"
            )
        cleared = [name for name in targets if clear_cordon(name)]
        if as_json:
            click.echo(_json.dumps({"cleared": cleared}, indent=2, sort_keys=True))
        elif cleared:
            click.echo("uncordoned: " + ", ".join(cleared))
            click.echo(
                "note: these hosts are still BEHIND the released version — "
                "the next `coord release propagate` will cordon them again "
                "unless the thing that stopped them draining is fixed."
            )
        else:
            click.echo("nothing to do — none of those machines was cordoned")
        return

    if machines:
        written = [
            set_cordon(
                name,
                reason=reason or "cordoned by hand",
                target_version=target_version,
                ttl_seconds=ttl,
            )
            for name in machines
        ]
        if as_json:
            click.echo(
                _json.dumps([c.to_dict() for c in written], indent=2, sort_keys=True)
            )
        else:
            for record in written:
                click.echo(f"⊘ {record.machine}: {record.describe()}")
        return

    now = time.time()
    active = read_cordons(now=now)
    if as_json:
        click.echo(
            _json.dumps(
                [c.to_dict() for _, c in sorted(active.items())],
                indent=2,
                sort_keys=True,
            )
        )
        return
    # #2490: hosts the newest propagate run flagged as behind, idle, and
    # left uncordoned only because the #2240 cooldown is suppressing new
    # cordons. These are exactly the machines an operator would otherwise
    # have to notice by hand (`coord status` showing `online • idle` on a
    # host that's actually stuck on an old version) — surface them even when
    # `active` is empty, since a stuck host is by definition NOT cordoned.
    stuck_hosts, stuck_target = _stuck_hosts_from_journal()
    if not active:
        if stuck_hosts:
            _echo_stuck_hosts(stuck_hosts, stuck_target)
        else:
            click.echo("no machines are cordoned — the fleet is free to take work")
        return
    for name, record in sorted(active.items()):
        remaining = max(0.0, record.expires_at - now) / 60.0
        overdue = " OVERDUE" if record.overdue(now) else ""
        click.echo(
            f"⊘ {name}: {record.describe()} "
            f"[{record.age(now) / 60.0:.0f}m draining, lapses in "
            f"{remaining:.0f}m, owner={record.owner}]{overdue}"
        )
    # #2240: the same stall count `coord status` appends, on the surface an
    # operator reaches for once they have noticed the fleet is quiet. The
    # journal is a local file (the propagate timer's host); no journal → no
    # line, never a fabricated one.
    target = next(
        (c.target_version for c in active.values() if c.target_version), None
    )
    try:
        pressure = rc.deferral_pressure(
            _read_propagation_records(), target_version=target
        )
    except Exception:  # noqa: BLE001 — a listing must not fail on the journal
        pressure = rc.DeferralPressure()
    if pressure.consecutive:
        if pressure.progressed:
            stall_note = (
                "the fleet's busy signal has changed between at least two of "
                "those runs — legs completing, new ones dispatching — so this "
                "reads as a converging drain, not a stall (#2741); a cordon "
                "does not itself block follow-on dispatch (a review still "
                "routes onto a cordoned host)"
            )
        else:
            stall_note = (
                "these cordons have not produced a rollable window, and the "
                "fleet's busy signal has held identical across every one of "
                "those runs — a cordon does not itself block follow-on "
                "dispatch (a review still routes onto a cordoned host), so "
                "this is read as a genuine stall. `coord release propagate` "
                f"releases it outright after {rc.DEFAULT_MAX_DEFERRALS} "
                "(#2240/#2741)"
            )
        click.echo(f"\n! {rc.describe_deferral_pressure(pressure)}: {stall_note}.")
    if stuck_hosts:
        _echo_stuck_hosts(stuck_hosts, stuck_target)
    click.echo(
        f"\nclear one with `coord release cordon --clear <machine>` "
        f"(default lifetime {rc.DEFAULT_TTL_SECONDS / 60:.0f}m — a cordon "
        "nobody renews lapses on its own)"
    )


def _read_propagation_records() -> list[dict]:
    """The propagation journal, or ``[]`` when this host does not have one."""
    from coord import release_propagate as rp  # noqa: PLC0415

    try:
        return rp.read_records(_state_dir())
    except Exception:  # noqa: BLE001 — see every caller: never load-bearing
        return []


def _stuck_hosts_from_journal() -> tuple[list[str], str | None]:
    """Hosts the NEWEST propagate run flagged as stuck-in-cooldown (#2490).

    ``CordonPlan.stuck_in_cooldown`` (mirrored onto ``CordonOutcome`` and
    journalled under a run's ``cordons`` key) names behind, idle hosts a run
    left uncordoned only because the #2240 deadlock-release cooldown is
    still active — the gap the issue is about: nothing gave such a host a
    signal or a path back to rolling for up to the cooldown's full length.

    Only the single newest record counts, deliberately: this is a
    point-in-time reading ("as of the last tick"), and a host named in an
    older record may since have gone busy, rolled, or had its cooldown
    lift — reporting it from a stale record would be a false alarm, which is
    exactly the kind of noise this fleet already has too much of. No
    journal (a thin client, or a host that has never run propagate)
    degrades to nothing rather than raising — mirrors every other reader of
    this journal in this module.
    """
    try:
        records = _read_propagation_records()
    except Exception:  # noqa: BLE001 — never load-bearing
        return [], None
    if not records:
        return [], None
    newest = records[-1]
    if not isinstance(newest, dict):
        return [], None
    cordons = newest.get("cordons")
    if not isinstance(cordons, dict):
        return [], None
    stuck = cordons.get("stuck_in_cooldown") or []
    if not isinstance(stuck, list):
        return [], None
    target = newest.get("target_version")
    return [str(h) for h in stuck], (str(target) if target else None)


def _echo_stuck_hosts(stuck_hosts: list[str], stuck_target: str | None) -> None:
    """The #2490 notice both branches of ``release_cordon`` share.

    One line per host, each naming its own override command — the format
    the issue itself asks for: "precision: 18m behind v0.5.192, idle, cordon
    suppressed by cooldown (#2240) — consider `coord agent update --machine
    precision`."
    """
    version = f"v{stuck_target}" if stuck_target else "the release"
    click.echo(f"\n! STUCK (#2490): behind {version}, idle, cordon suppressed "
               "by the #2240 deadlock-release cooldown — no automatic path "
               "back to rolling until it lifts on its own:")
    for host in sorted(stuck_hosts):
        click.echo(f"  ! {host}: consider `coord agent update --machine {host}`")


@release_group.command(
    "rollback",
    help=(
        "ONE command that puts every agent back on its previous venv "
        "generation (#1241/#1560). The escape hatch for a bad release."
    ),
)
@_CONFIG_OPTION
@click.option("--machine", "machine_filter", default=None, help="Only this machine.")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@click.option("--wait", default=90.0, show_default=True,
              help="Seconds to wait for each agent to start serving again "
                   "before escalating to an SSH restart (#2052).")
def release_rollback(config_path: Path, machine_filter: str | None, yes: bool,
                     wait: float) -> None:
    """#1560 requires rollback to be one command, not a runbook.

    Every successful ``/update`` leaves the previous generation on disk
    (``coord.agent_update``'s two fixed blue/green slots) precisely so this
    can exist. It force-rolls: a rollback is what you reach for when the
    fleet is broken, and refusing because a worker is running on a broken
    release would be the wrong tradeoff at exactly the wrong moment.
    """
    from coord.agent_update import cli_initiator  # noqa: PLC0415
    from coord.commands._common import AGENT_PORT, _load_config  # noqa: PLC0415

    config = _load_config(config_path)
    machines = [
        m for m in (getattr(config, "machines", ()) or ())
        if not machine_filter or m.name == machine_filter
    ]
    if not machines:
        click.echo("no machines to roll back", err=True)
        sys.exit(2)
    if not yes:
        click.confirm(
            f"Roll back {len(machines)} agent(s) to the previous venv generation "
            "and restart them (this kills any in-flight worker)?",
            abort=True,
        )
    failures = 0
    for machine in machines:
        ok, detail = _rollback_host(
            machine, agent_port=AGENT_PORT, timeout=wait,
            initiator=cli_initiator(f"coord release rollback -> {machine.name}"),
        )
        click.echo(f"  {'↩' if ok else '✗'} {machine.name}: {detail}")
        failures += 0 if ok else 1
    if failures:
        sys.exit(1)


@release_group.command(
    "history",
    help="What propagation actually did, and when (#1835's observability gate).",
)
@click.option("--limit", default=40, show_default=True,
              help="Show at most this many recorded attempts (most recent last).")
@click.option("-v", "--verbose", is_flag=True,
              help="Show every no-op attempt individually instead of collapsing runs.")
@click.option("--json", "as_json", is_flag=True, help="Emit the raw records as JSON.")
def release_history(limit: int, verbose: bool, as_json: bool) -> None:
    """Read the propagation journal.

    #1835: "a silent success is indistinguishable from a silent no-op, which
    is precisely how 2026-08-04 stayed invisible." Every attempt is
    journalled, including the deferrals — so an empty history means the
    timer never ran, which is itself the finding.
    """
    import json as _json  # noqa: PLC0415

    from coord import release_propagate as rp  # noqa: PLC0415

    records = rp.read_records(_state_dir(), limit=limit)
    if as_json:
        click.echo(_json.dumps(records, indent=2, sort_keys=True))
        return
    click.echo(rp.render_history(records, verbose=verbose))


# ── #2112: the nightly daemon-host release window ───────────────────────────
#
# `coord release propagate` waits for a quiescent window that may never
# arrive on the daemon host specifically, because it both leads every roll
# (the documented 405) and is the box that launches drive-queue work, so
# almost any drive anywhere keeps it "busy" and defers the entire fleet
# (see `coord/release_window.py`'s module docstring for the full mechanism
# and the 2026-08-10 measurement). This section is the I/O shell over that
# module's decisions: stop the queue timer, drain in-flight drives bounded
# by a deadline, roll via `coord release propagate`, and ALWAYS restart the
# timer — the pure judgement (`needs_roll`, the journal, the record shape)
# lives in `coord/release_window.py`, same split as `release_propagate`'s
# own I/O shell above.
#
# GATED ON #2110: the drain loop below calls `coord drive-queue tick
# --reconcile-only` on every poll specifically because stopping the timer
# also stops the ONLY thing that otherwise reconciles a finished drive's row
# from `running` to `done` — without #2110 making that reconciliation safe to
# call standalone, this whole mechanism would deadlock every night.


def _systemctl(unit: str, action: str, *, runner=None, timeout: float = 30.0) -> tuple[bool, str]:
    """``systemctl --user <action> <unit>``, tolerantly.

    Mirrors `coord.deploy_units.enable_timers`'s own systemctl wrapper: same
    injectable ``runner`` seam for tests (a fleet-free unit test never
    spawns a real ``systemctl``), same "never raise" contract — a window run
    must be able to report "could not stop the queue" as a normal, recorded
    outcome rather than crash.
    """
    import subprocess  # noqa: PLC0415

    run = runner or subprocess.run
    try:
        proc = run(
            ["systemctl", "--user", action, unit],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        return False, "systemctl not found (no systemd on this host)"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    ok = getattr(proc, "returncode", 1) == 0
    detail = (getattr(proc, "stderr", "") or getattr(proc, "stdout", "") or "").strip()
    return ok, detail or (f"{action} ok" if ok else f"{action} failed")


def _run_reconcile_tick(config_path: Path, *, runner=None) -> tuple[bool, str]:
    """``coord drive-queue tick --reconcile-only`` — best effort, #2110.

    Stopping `coord-drive-queue.timer` for the drain also stops the ONLY
    thing that otherwise reconciles a finished drive's row from ``running``
    to ``done``. Without calling this on every poll, a drive that finished
    *after* the timer stopped would stay stuck ``running`` for the rest of
    the drain no matter how generous the deadline is — the exact deadlock
    #2110 fixed reconciliation to no longer require the timer for.

    Thin wrapper around :func:`coord.reconcile_tick.run_reconcile_tick`, the
    #2373-extracted shared implementation also used by
    `AgentServer.reconcile_drive_queue` (`coord/agent.py`) — see that
    module's docstring for why there must be exactly one implementation of
    "what a reconcile-only tick does". A failure here is not fatal on its
    own: the quiescence check right after this call is the real signal a
    stuck reconcile eventually clears on a later poll, not this one call's
    job to guarantee.
    """
    from coord.reconcile_tick import run_reconcile_tick  # noqa: PLC0415

    return run_reconcile_tick(config_path, timeout=120, detail_limit=200, runner=runner)


def _drain(
    *,
    daemon_host: str,
    config_path: Path,
    deadline: float,
    poll_interval: float,
    config=None,
    reconcile=None,
    board_fetch=None,
    extra_busy_fetch=None,
    now=None,
    sleep=None,
):
    """Bounded wait for *daemon_host* to stop being busy (trap 2).

    Reuses `release_propagate.assess_quiescence` — the SAME computation
    `coord release propagate` itself uses to decide whether the daemon host
    may lead a roll — rather than a second definition of "busy" (#2096's
    "two surfaces, one function" rule). ``now``/``sleep`` are injectable so
    this loop is unit-testable without a real clock; ``reconcile``/
    ``board_fetch``/``extra_busy_fetch`` default to the real subprocess/
    board/tmux-probe calls.

    #2228: ``extra_busy_fetch`` re-probes live interactive sessions on
    every poll (not just once) — a session that ends mid-drain should let
    the daemon host clear within THIS wait, not only on the next `coord
    release propagate` tick. #2174: the same default also re-reads
    `coord pause`/quiet-hours state on every poll, for the same reason —
    an operator pausing the daemon host mid-drain must stop the drain from
    ever reporting "clear", and an operator lifting a pause mid-drain
    should let it clear within THIS wait rather than the next tick.
    Deliberately keyed off the explicit *config* param, never an implicit
    ``_load_config(config_path)`` fallback: this function is called with
    ``config_path=None`` from unit tests that inject their own clock/board
    — silently resolving a real ``coordinator.yml`` (and then SSH-probing
    whatever machines it names, or reading this host's own pause store)
    the moment ``config`` is omitted would make this loop non-hermetic by
    default. No *config* simply means "no interactive-session or pause
    signal this call" — the caller opts in by passing one, as the real
    ``coord release nightly-window`` call site does.
    """
    import time as _time  # noqa: PLC0415

    from coord import release_propagate as rp  # noqa: PLC0415
    from coord import release_window as rw  # noqa: PLC0415

    now_fn = now or _time.time
    sleep_fn = sleep or _time.sleep
    reconcile_fn = reconcile or (lambda: _run_reconcile_tick(config_path))
    fetch_fn = board_fetch or _fetch_board
    if extra_busy_fetch is None:
        if config is not None:
            # #2174: paused/quiet-hours state, same as the top-level
            # `propagate` call site — a paused daemon host must keep the
            # drain from ever reading "clear" just because tmux is quiet.
            # #2596: same for a confirmation running under `notify.lock` —
            # gated on `config is not None` too, even though the check
            # itself needs no config, purely to keep this function's
            # documented "no *config* means no real filesystem I/O"
            # contract intact for the `config=None` unit-test path above.
            extra_busy_fetch = lambda: (  # noqa: E731
                _interactive_session_busy(config)
                + _paused_machine_busy(config)
                + _confirmation_running_busy()
            )
        else:
            extra_busy_fetch = lambda: []  # noqa: E731

    start = now_fn()
    while True:
        reconcile_fn()
        board, board_error = fetch_fn()
        extra_busy = []
        if board_error:
            extra_busy.append(
                rp.Busy(kind="board unreadable", subject="/board", detail=board_error)
            )
        extra_busy.extend(extra_busy_fetch())
        # #2854: same settle-window opt-in as the top-level `propagate` call
        # site — one clock read per poll, reused for both the settle window
        # and `elapsed` below, so a between-legs gap that crosses the settle
        # threshold mid-drain clears within THIS wait rather than only on
        # the next `coord release propagate` tick.
        poll_now = now_fn()
        quiescence = rp.assess_quiescence(
            queue_entries=board.get("drive_queue") or [],
            assignments=board.get("assignments") or [],
            issues=board.get("issues") or [],
            extra_busy=extra_busy,
            now=poll_now,
        )
        busy = bool(quiescence.fleet_wide_busy) or daemon_host in quiescence.busy_hosts()
        elapsed = poll_now - start
        if not busy:
            return rw.DrainOutcome(drained=True, elapsed_seconds=elapsed, detail="drained")
        detail = quiescence.busy_reason_for_host(daemon_host) or quiescence.reason
        if elapsed >= deadline:
            return rw.DrainOutcome(drained=False, elapsed_seconds=elapsed, detail=detail)
        sleep_fn(max(0.0, min(poll_interval, deadline - elapsed)))


def _parse_trailing_json(stdout: str) -> dict | None:
    """The last top-level JSON object printed to *stdout*, or ``None``.

    #2187: this used to look only at the LAST LINE of stdout and try to
    parse THAT LINE alone as JSON. `coord release propagate --json` (like
    every ``--json`` command in this codebase) prints its record via
    ``json.dumps(..., indent=2, ...)`` — pretty-printed, for a human running
    it by hand — which means the payload spans many lines and its own last
    line is just ``}``, never a complete object on its own. The old check
    therefore never found the record for ANY propagate run, successful or
    not, and every call fell back to the ``f"exit {code}"`` placeholder in
    `_run_propagate` below — which is exactly the false "propagate-failed
    (status=exit 0, exit=0)" #2187 reports for a verified, exit-0 roll.

    This instead finds the LAST line that is the object's own unindented
    ``{`` (json.dumps never indents the outermost brace) and parses
    everything from there to the end of stdout as one document. A single
    compact line (no ``indent=``) still works too, tried second.
    """
    import json as _json  # noqa: PLC0415

    text = stdout.strip()
    if not text:
        return None
    lines = text.splitlines()
    for idx in range(len(lines) - 1, -1, -1):
        if lines[idx].strip() != "{":
            continue
        try:
            payload = _json.loads("\n".join(lines[idx:]))
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    for line in reversed(lines):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = _json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _latest_propagate_record_since(state_dir: Path, since: float) -> dict | None:
    """The propagation journal's own record for the run started at/after
    *since*, if any — the ground truth `coord release history` itself reads.

    #2187 proposal 1: "read `coord release history` for the run it just
    launched rather than inferring from a side-channel [stdout]." The
    journal has no request id to join on, so this uses ``started_at``: the
    subprocess `_run_propagate` shells out to is synchronous, so by the
    time it has returned, a successful run has already appended its record
    with a ``started_at`` at or after the moment this function's caller
    launched it. Picks the OLDEST such record — the first thing written
    after launch — in case some unrelated `coord release propagate` run
    elsewhere raced it onto the same shared journal.
    """
    from coord import release_propagate as rp  # noqa: PLC0415

    candidates = [
        rec for rec in rp.read_records(state_dir)
        if isinstance(rec.get("started_at"), (int, float)) and rec["started_at"] >= since
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda rec: rec["started_at"])


def _run_propagate(
    *, daemon_host: str, target_version: str, config_path: Path, state_dir: Path,
    min_behind: int | None = None, runner=None, now_fn=None,
) -> tuple[str, int, str, float | None]:
    """``coord release propagate --daemon-host ... --target ... --json``.

    A real subprocess of THIS interpreter (matches `_roll_tui`/
    `_release_hold` above), not an in-process call: `release_propagate` is a
    click command that calls ``sys.exit()`` itself and is not meant to be
    invoked as a plain function. ``--target`` is passed explicitly — not
    left to re-resolve PyPI's "latest" a second time — so the version this
    run decided was needed is the version that actually rolls, even if a new
    release lands on PyPI mid-drain.

    #2870: *min_behind*, when given, is passed through as `--min-behind` —
    the threshold the roll-pending marker being discharged here was ARMED
    at (`RollPending.min_releases_behind`), NOT this process's own
    `propagation.min_releases_behind`. Before this, this call never passed
    `--min-behind` at all, so `coord release propagate` always re-resolved
    ITS OWN threshold from `coordinator.yml` — a marker armed below the
    fleet default (e.g. `coord release nightly-window --min-behind 1`
    against a fleet configured `min_releases_behind: 5`) could never reach
    the threshold ITS discharge required and held forever, `alert: (none)`
    the whole time. ``None`` (an old marker with no recorded threshold, or
    one whose arming run never evaluated the gate) omits the flag, matching
    the pre-#2870 behaviour — this process resolves its own default exactly
    as it always did.

    Returns ``(status, exit_code, combined_output, propagate_started_at)``.
    ``status`` is read from the propagation journal entry that run itself
    wrote (:func:`_latest_propagate_record_since`) — the same ground truth
    `coord release history` reads — not reparsed from stdout (#2187:
    reparsing stdout was the false-negative bug). ``propagate_started_at``
    is that record's own ``started_at``, stamped onto the window's own
    record so `window-history` can be joined to `history` (#2187 proposal
    2) instead of the two stores re-deciding the same outcome separately.
    Only when no journal entry can be found — the subprocess died before
    writing one, or the journal write itself failed — does this fall back
    to `_parse_trailing_json` on stdout, and then to ``f"exit {code}"``;
    either fallback returns ``None`` for ``propagate_started_at`` since
    there is then nothing to join to.
    """
    import subprocess  # noqa: PLC0415
    import time  # noqa: PLC0415

    run = runner or subprocess.run
    clock = now_fn or time.time
    launched_at = clock()
    argv = [
        sys.executable, "-m", "coord.cli", "release", "propagate",
        "--daemon-host", daemon_host, "--target", target_version,
        "--json", "--config", str(config_path),
    ]
    if min_behind is not None:
        argv.extend(["--min-behind", str(min_behind)])
    try:
        proc = run(argv, capture_output=True, text=True, timeout=1800)
    except Exception as exc:  # noqa: BLE001
        detail = f"{type(exc).__name__}: {exc}"
        return f"error: {detail}", 1, detail, None
    code = getattr(proc, "returncode", 1)
    stdout = getattr(proc, "stdout", "") or ""
    stderr = getattr(proc, "stderr", "") or ""
    output = (stdout + ("\n" + stderr if stderr else "")).strip()

    record = _latest_propagate_record_since(state_dir, launched_at)
    if record is not None:
        status = str(record.get("status") or f"exit {code}")
        return status, code, output, record.get("started_at")

    payload = _parse_trailing_json(stdout)
    status = (
        str(payload["status"]) if payload and payload.get("status") else f"exit {code}"
    )
    return status, code, output, None


#: Where a skipped/failed nightly window (trap 3) is recorded — same
#: escalation channel #2101's drain-deadline escalation and `coord
#: drive-queue`'s own alerts use, so it shows up in the TUI's escalations
#: panel and `coord drive escalations` with no new surface to remember to
#: look at. Mirrors `_escalate_drain`'s `DRAIN_ALERT_*` convention above.
WINDOW_ALERT_REPO = "(release-window)"
WINDOW_ALERT_ISSUE = 0
WINDOW_ALERT_STAGE = "release-window"


def _escalate_window(record, *, reason: str) -> None:
    """Surface a skipped/failed nightly window loudly (trap 3).

    #2112's whole point: a night propagation was supposed to happen and did
    not is exactly the state #2082 exists to make loud elsewhere in this
    fleet. Silence must not be the report here either.
    """
    click.echo(f"  ! {reason}", err=True)
    try:
        from coord.state import record_drive_escalation  # noqa: PLC0415

        proposed = (
            f"coord release propagate --daemon-host {record.daemon_host} "
            f"--target {record.target_version}"
            if record.daemon_host and record.target_version
            else "coord release nightly-window --dry-run   # investigate first"
        )
        record_drive_escalation(
            WINDOW_ALERT_REPO, WINDOW_ALERT_ISSUE, stage=WINDOW_ALERT_STAGE,
            reason=reason,
            gate_readings=(
                f"daemon_host={record.daemon_host} | target={record.target_version} | "
                f"daemon_version={record.daemon_version} | status={record.status}"
            ),
            proposed_command=proposed,
        )
    except Exception as exc:  # noqa: BLE001 — the stderr line above is the
        # floor; an escalation table that cannot be written must not take
        # the message down with it.
        click.echo(f"  (could not record the window escalation: {exc})", err=True)


@release_group.command(
    "nightly-window",
    help=(
        "Guarantee the daemon host rolls, without waiting for a fleet-wide "
        "quiescent moment that may never come (#2112, #2587). Sets a "
        "roll-pending marker naming the target version and returns "
        "immediately — the drive-queue tick fires the actual roll at the "
        "next inter-drive gap. Never stops `coord-drive-queue.timer`."
    ),
)
@_CONFIG_OPTION
@click.option("--target", default=None,
              help="Version to propagate (leading 'v' optional). Default: PyPI's latest.")
@click.option("--daemon-host", "daemon_host_override", default=None,
              help="Machine name running coord-serve. Normally DERIVED from the "
                   "fleet's own /health, same as `coord release propagate`.")
@click.option("--queue-timer", default="coord-drive-queue.timer", show_default=True,
              help="DEPRECATED, ignored (#2587): this command no longer stops or "
                   "starts any timer — kept only so an old invocation (or the "
                   "deployed unit, before it is redeployed) does not fail on an "
                   "unrecognised flag.")
@click.option("--drain-deadline", default=3600.0, show_default=True, type=float,
              help="DEPRECATED, ignored (#2587): there is no drain to bound any "
                   "more — see `coord.drive_queue.ROLL_PENDING_DEFAULT_TTL_SECONDS` "
                   "for the marker's own bound instead.")
@click.option("--poll-interval", default=30.0, show_default=True, type=float,
              help="DEPRECATED, ignored (#2587): nothing here polls any more — "
                   "the drive-queue tick's own cadence is what re-checks.")
@click.option("--dry-run", is_flag=True,
              help="Print what this run would do; set nothing.")
@click.option("--ensure-queue-running", is_flag=True,
              help="Legacy escape hatch, kept for a hand invocation only: does "
                   "ONLY `systemctl --user start <queue-timer>` and exits. #2587 "
                   "never stops the timer in the first place, so this is no "
                   "longer wired into `deploy/coord-release-window.service` — "
                   "it exists purely so `systemctl --user start "
                   "coord-drive-queue.timer` after a hand-stopped timer is one "
                   "recognisable command rather than a new one to remember. "
                   "Every other option is ignored with this flag.")
@click.option("--min-behind", "min_behind_override", default=None, type=int,
              help="#2583: hold this run — no marker set, no host touched — "
                   "below this many releases behind PyPI's latest. Default: "
                   "propagation.min_releases_behind in coordinator.yml, or 1 "
                   "if that is unset — any delta at all rolls, today's "
                   "behaviour.")
@click.option("--json", "as_json", is_flag=True, help="Emit the window record as JSON.")
def release_nightly_window(
    config_path: Path,
    target: str | None,
    daemon_host_override: str | None,
    queue_timer: str,
    drain_deadline: float,
    poll_interval: float,
    dry_run: bool,
    ensure_queue_running: bool,
    min_behind_override: int | None,
    as_json: bool,
) -> None:
    """One nightly-window attempt. Exit 0 on up-to-date/roll-pending/rolled/
    dry-run, 1+ otherwise.

    #2112: `coord release propagate` cannot roll the daemon host past a busy
    fleet on its own — the daemon leads every roll (the documented 405), and
    dellserver's own drive-queue tick charges itself as busy for essentially
    any queued drive (see `coord/release_window.py`'s module docstring).

    #2587 REWRITE: this used to manufacture the window itself — stop
    `coord-drive-queue.timer`, poll a bounded drain (up to an hour), roll,
    always restart the timer. Measured 2026-08-22: that drain ran the full
    60 minutes, drained nothing, rolled nothing, with the timer — and
    therefore ALL reconciliation and ALL new dispatch — stopped the entire
    time. The fleet was never going to reach fleet-wide quiescence; it did
    not need to. Now this command does the one thing only IT can do (resolve
    the target, decide a roll is needed) and hands the actual waiting off to
    :func:`coord.drive_queue.plan_tick` via the roll-pending marker
    (`coord.commands.drive_queue.write_roll_pending`) — the tick already
    knows the fleet's true busy/idle state every ~3 minutes, for free,
    without stopping anything. See `RollPending`'s own docstring for the
    full mechanism and `docs/AGENT_OPERATIONS.md`'s propagation section for
    the incident this replaces.

    If a roll is ALREADY pending for THIS target (e.g. a previous night's
    marker never got its window and #2587's own TTL has not yet lapsed),
    this makes one best-effort attempt to fire it directly via `coord
    release propagate` — never `--force` (trap 1) — rather than leaving it
    to the tick alone; a real fleet host running its own nightly timer while
    genuinely idle is a fine moment to roll immediately. A still-busy fleet
    just leaves the marker exactly as it was for the tick to keep watching.
    """
    import dataclasses as _dataclasses  # noqa: PLC0415
    import json as _json  # noqa: PLC0415
    import time  # noqa: PLC0415

    from coord import release_propagate as rp  # noqa: PLC0415
    from coord import release_verify as rv  # noqa: PLC0415
    from coord import release_window as rw  # noqa: PLC0415
    from coord.commands._common import _load_config  # noqa: PLC0415
    from coord.commands.drive_queue import (  # noqa: PLC0415
        clear_roll_pending,
        read_roll_ledger,
        read_roll_pending,
        reset_roll_ledger,
        write_roll_pending,
    )
    from coord.drive_queue import RollPending  # noqa: PLC0415

    if ensure_queue_running:
        ok, detail = _systemctl(queue_timer, "start")
        click.echo(f"{'✓' if ok else '✗'} ensure {queue_timer} running: {detail}")
        sys.exit(0 if ok else 1)

    config = _load_config(config_path)
    state_dir = _state_dir()
    # #2889 item 4: "what invoked this?" straight from the journal —
    # `$COORD_ROLL_INVOKER` is set on the process environment by whichever
    # trigger started `coord-release-window.service`: the unit's own static
    # `Environment=` default (its timer, or a human running `systemctl
    # --user start` by hand — systemd cannot tell those two apart, see the
    # unit's own "INVOKER" section) or `_fire_pending_roll`'s explicit
    # `--setenv=` override when the drive-queue tick is the one firing it.
    # Empty for a bare CLI invocation outside the unit (a test, an
    # operator's own shell) — nothing set the env var at all.
    import os as _os  # noqa: PLC0415

    record = rw.WindowRecord(
        started_at=time.time(), dry_run=dry_run, queue_timer=queue_timer,
        invoked_by=_os.environ.get("COORD_ROLL_INVOKER", ""),
    )

    def _finish(status: str, exit_code: int = 0) -> None:
        record.status = status
        record.finished_at = time.time()
        if not dry_run:
            try:
                rw.append_record(state_dir, record)
                rw.trim_journal(state_dir)
            except OSError as exc:
                click.echo(f"warning: could not append the window journal: {exc}",
                           err=True)
        if as_json:
            click.echo(_json.dumps(record.to_dict(), indent=2, sort_keys=True))
        else:
            click.echo("\n".join(rw.render_record(record)))
        sys.exit(exit_code)

    # ── 1. what version, and who leads? ───────────────────────────────────
    index_url = getattr(getattr(config, "health", None), "pypi_index_url",
                        "https://pypi.org/simple")
    resolved, warning = _resolve_expected(
        target, use_pypi=not target, index_url=index_url, timeout=10.0
    )
    if warning:
        click.echo(f"warning: {warning}", err=True)
    record.target_version = rp.normalize_version(resolved)
    if not record.target_version:
        record.error = (
            "could not resolve a target version — pass --target, or fix "
            "access to the PyPI simple index"
        )
        click.echo(f"error: {record.error}", err=True)
        _escalate_window(record, reason=record.error)
        _finish(rw.STATUS_ERROR, 1)

    machine_health, unreachable, daemon_facts, daemon_label = rv.gather(config, timeout=10.0)
    daemon_name = _daemon_machine_name(config, daemon_host_override, machine_health)
    if daemon_name is None:
        record.error = (
            "could not identify which machine runs coord-serve — pass "
            "--daemon-host, or fix its /health so the unit is visible "
            "(same requirement as `coord release propagate`)"
        )
        click.echo(f"error: {record.error}", err=True)
        _escalate_window(record, reason=record.error)
        _finish(rw.STATUS_ERROR, 1)
    record.daemon_host = daemon_name

    report = rv.verify(
        machine_health=machine_health, unreachable=unreachable,
        daemon_host=daemon_facts, daemon_host_name=daemon_label,
        expected=record.target_version,
    )
    record.daemon_version = _python_lane_versions(
        report, [daemon_name], record.target_version
    ).get(daemon_name)

    # #2583: resolved here, ahead of the up-to-date check below, so the
    # journal always records what threshold this run actually used —
    # whether or not a roll turned out to be needed at all. The network
    # read this gate needs (`_releases_behind_count`) is still deferred
    # until it is actually relevant — see the gate below.
    effective_min_behind = _resolve_min_behind(min_behind_override, config)
    record.min_releases_behind = effective_min_behind

    # ── 2. acceptance 3 — already current, so nothing EXTERNAL is touched:
    #      no systemctl call, no `coord release propagate` subprocess. A
    #      pending marker IS cleared here, though (never left standing) — if
    #      the daemon has already reached the freshest resolvable target by
    #      some other means (a human ran `coord release propagate` by hand,
    #      or a previous run already rolled it), the marker's whole job is
    #      done and leaving it would force `--reconcile-only` posture on the
    #      queue for up to its own TTL/deferral ceiling for nothing. Safe
    #      under the deployed unit's normal call shape (no explicit
    #      `--target`, always PyPI's current latest — see
    #      `deploy/coord-release-window.service`): any marker still present
    #      was set by an EARLIER resolution of the same "latest" lookup, so
    #      it can only name an equal-or-older target, never a newer one this
    #      check has not yet accounted for.
    if not rw.needs_roll(record.daemon_version, record.target_version):
        if read_roll_pending() is not None:
            clear_roll_pending()
            # #2889: the goal is reached without needing the marker at all —
            # a clean slate, same as a CONFIRMED roll below. Whatever
            # cumulative frozen time/fresh-arm history the ledger carried is
            # over; a FUTURE delta starts counting fresh.
            reset_roll_ledger()
        _finish(rw.STATUS_UP_TO_DATE, 0)

    # ── 2b. #2583 min-releases-behind gate ────────────────────────────────
    #
    # A roll IS needed, but not yet enough of one: below this threshold this
    # run is a REPORTED no-op that touches NEITHER the roll-pending marker
    # NOR `coord release propagate` — an existing marker (set before this
    # gate existed, or by a lower threshold) is left exactly as it is for a
    # later, above-threshold run to pick back up; nothing here clears it or
    # fires it. `effective_min_behind <= 1` (the default) skips the PyPI
    # read entirely, same as `release_propagate`'s identical gate.
    if effective_min_behind > 1:
        behind, behind_warning = _releases_behind_count(
            record.daemon_version, index_url=index_url, timeout=10.0
        )
        record.releases_behind = behind
        if behind_warning:
            click.echo(f"warning: {behind_warning}", err=True)
        if behind is not None and behind < effective_min_behind:
            # `render_record` (called by `_finish` below) prints the
            # "holding: N behind, threshold M" line itself — same pattern
            # `STATUS_UP_TO_DATE`'s daemon-host line uses, one place that
            # renders a status's own detail rather than a second echo here
            # that could drift from it.
            _finish(rw.STATUS_HOLDING, 0)

    existing = read_roll_pending()

    if dry_run:
        # #2866: a marker naming a STALE target (not equal to the freshly
        # resolved `record.target_version` — PyPI's "latest" moved, or the
        # daemon reached/passed it some other way since the marker was set)
        # must never be echoed back as "would attempt ... --target <stale>".
        # This is the exact 2026-08-28 incident: a `propagate`-set marker
        # for v0.5.254 survived on disk after the daemon had already
        # reached v0.5.258 by other means, and `nightly-window --dry-run`
        # read it back and printed a proposal to roll BACKWARDS to
        # v0.5.254 — cleared by hand with `coord drive-queue cancel-roll`
        # at the time. In reality a NON-dry-run run never fires a stale
        # target either (section 3 below replaces it with
        # `record.target_version` instead, whenever it differs) — this
        # branch only makes the --dry-run description match that real
        # behaviour instead of quoting the stale value as if it would run.
        if existing is not None and existing.target_version != record.target_version:
            click.echo(
                f"a roll is pending for a stale target ({existing.describe()}) — "
                f"would replace it with v{record.target_version} ({daemon_name} "
                f"currently reports v{record.daemon_version or '?'}) rather than "
                "ever firing the stale value (#2866); its original set_at/"
                "deferrals would be preserved (#2607)"
            )
        elif existing is not None:
            click.echo(
                f"a roll is already pending ({existing.describe()}) — would "
                f"attempt `coord release propagate --daemon-host {daemon_name} "
                f"--target {existing.target_version}` now, leaving the marker "
                "for the drive-queue tick if the fleet is still busy"
            )
        else:
            click.echo(
                f"would set a roll-pending marker for v{record.target_version} "
                f"({daemon_name} currently reports "
                f"v{record.daemon_version or '?'}) — the drive-queue tick "
                "fires `coord release propagate` at the next inter-drive gap "
                # #2587 review nit: this must name the REAL timer #2587 never
                # stops (`rw.DEFAULT_QUEUE_TIMER`), not the now-ignored
                # `--queue-timer` value — a caller who still passes a custom
                # value here would otherwise read this line as a claim about
                # a timer this command no longer even looks at.
                f"(#2587); {rw.DEFAULT_QUEUE_TIMER} is never stopped"
            )
        _finish(rw.STATUS_DRY_RUN, 0)

    if existing is not None and existing.target_version == record.target_version:
        # #2587: a roll is already pending for this exact target — most
        # likely set by a PRIOR run of this same nightly timer that never
        # got its window yet. The drive-queue tick is the PRIMARY trigger
        # (see `RollPending`'s docstring); this is a belt-and-braces extra
        # attempt, safe to make redundantly since `coord release propagate`
        # never carries `--force` here (trap 1) and simply defers again if
        # the fleet is still busy.
        # #2870: gate the discharge at the threshold the marker was ARMED
        # at (`existing.min_releases_behind`), not whatever `coord release
        # propagate` would re-resolve on its own — that mismatch is exactly
        # how a marker armed via `--min-behind 1` froze the queue against a
        # fleet configured `min_releases_behind: 5`, forever holding at "3
        # behind, threshold 5" no matter how many times this fired. A
        # marker with no recorded threshold (written before this field
        # existed, or whose arming run never evaluated the gate at all)
        # falls back to THIS run's own `effective_min_behind` — the same
        # value it would have used before #2870.
        prop_status, prop_exit, prop_output, prop_started_at = _run_propagate(
            daemon_host=daemon_name, target_version=existing.target_version,
            config_path=config_path, state_dir=state_dir,
            min_behind=(
                existing.min_releases_behind
                if existing.min_releases_behind is not None
                else effective_min_behind
            ),
        )
        record.propagate_status = prop_status
        record.propagate_exit_code = prop_exit
        record.propagate_output = prop_output
        record.propagate_started_at = prop_started_at
        if prop_output:
            click.echo(prop_output)

        if prop_exit == 0 and prop_status in (
            rp.STATUS_VERIFIED, rp.STATUS_UP_TO_DATE, rp.STATUS_ROLLED,
        ):
            clear_roll_pending()
            # #2889: a CONFIRMED roll (or a race that found it already
            # current) is the clean-slate outcome — reset the ledger so a
            # FUTURE delta starts counting fresh rather than inheriting
            # whatever run-up this campaign accumulated on its way here.
            reset_roll_ledger()
            status = (
                rw.STATUS_UP_TO_DATE if prop_status == rp.STATUS_UP_TO_DATE
                else rw.STATUS_ROLLED
            )
            _finish(status, 0)
        elif prop_exit == 0 and prop_status == rp.STATUS_DEFERRED:
            # Still busy — completely normal. Leave the marker exactly as it
            # is (this attempt spends none of its TTL/deferral bound; only
            # the tick's own per-tick deferrals do) for the tick to keep
            # watching.
            click.echo(
                f"roll for v{existing.target_version} still pending — "
                f"{daemon_name} not yet quiescent "
                f"({existing.deferrals}/{existing.max_deferrals} tick "
                "deferral(s) recorded so far)"
            )
            _finish(rw.STATUS_ROLL_PENDING, 0)
        elif prop_status.startswith("exit ") or prop_status.startswith("error:"):
            # #2187 proposal 3, preserved verbatim through the #2587 rewrite:
            # this arm is reached only when NEITHER ground truth was
            # available — no matching entry in `coord release history` (see
            # `_latest_propagate_record_since`) AND no parseable `--json`
            # payload on stdout (`_parse_trailing_json`). `prop_status` here
            # is one of those two functions' placeholder fallbacks, never a
            # real outcome — so the message must say exactly what evidence
            # is missing instead of quoting the placeholder as if it were
            # one (the old text, "status=exit 0, exit=0", read as a
            # verification failure when exit 0 is what a VERIFIED roll also
            # produces).
            record.error = (
                f"coord release propagate exited {prop_exit}, but its "
                "outcome could not be confirmed: no matching entry "
                "was found in `coord release history` for this run, "
                "and its --json stdout had no parseable status "
                "either — see propagate_output"
            )
            click.echo(f"error: {record.error}", err=True)
            _escalate_window(record, reason=record.error)
            _finish(rw.STATUS_PROPAGATE_FAILED, prop_exit or 1)
        else:
            # A real, ground-truth status from `coord release history` — just
            # not a verified roll (e.g. `failed` or `rolled-back`). A real
            # failure, not a mere deferral — loud (trap 3), and the marker
            # survives so the tick keeps trying too, bounded by its own
            # TTL/deferral ceiling.
            record.error = (
                f"coord release propagate did not verify the pending roll — "
                f"`coord release history` recorded status="
                f"{prop_status!r} (exit {prop_exit}) for this run"
            )
            click.echo(f"error: {record.error}", err=True)
            _escalate_window(record, reason=record.error)
            _finish(rw.STATUS_PROPAGATE_FAILED, prop_exit or 1)

    # ── 3. set (or refresh) the marker and return immediately (#2587) ─────
    # #2607: a marker already pending for a DIFFERENT target is updated in
    # place — target_version/reason move, but set_at/deferrals carry over
    # unchanged. This is the exact site the incident traced back to: PyPI's
    # "latest" climbs on every merge, so by the time this nightly timer
    # re-fired the target had almost always moved, and writing a brand new
    # `RollPending` here reset the TTL/deferral escape hatch on every single
    # re-arm — the queue froze because the bound that was supposed to save it
    # could never be reached. Only a genuinely fresh marker (no existing one
    # at all) gets a new clock.
    # #2870: every write below stamps `effective_min_behind` — THIS run's
    # own already-resolved threshold (section 2b above) — onto the marker,
    # so `_run_propagate`'s belt-and-braces call above (and any future
    # discharge) is gated at the SAME threshold that armed it, not whatever
    # `coord release propagate` would re-resolve on its own later. Updated
    # on every re-arm, same as `target_version`/`reason` just below — only
    # `set_at`/`deferrals` are the frozen "clock" #2607 protects.
    if existing is not None:
        click.echo(
            f"replacing stale roll-pending marker ({existing.describe()}) "
            f"with v{record.target_version} — preserving its original "
            f"set_at/deferrals (#2607: a re-arm is a continuation of the "
            f"same stuck roll, not a new request)"
        )
        write_roll_pending(
            _dataclasses.replace(
                existing, target_version=record.target_version, reason="nightly-window",
                min_releases_behind=effective_min_behind,
            )
        )
    else:
        # #2889: a genuinely FRESH arm — no existing marker for this
        # campaign at all — is the one case the RATE of markers can run
        # away. Same three checks `_ensure_roll_pending_marker` applies to
        # `coord release propagate`'s own arm site (#2096: one function, not
        # two definitions that could drift) — `_queue_provably_busy` needs
        # its own board read here since this command does not otherwise
        # compute a `Quiescence` (unlike `release_propagate`, which already
        # has one to reuse).
        arm_now = time.time()
        ledger = read_roll_ledger()
        board, board_error = _fetch_board()
        if board_error:
            # Cannot prove the queue non-empty either way — the #2889 item 2
            # check contributes nothing this run; the ledger's own
            # escalation/rate-limit checks still apply regardless.
            queue_provably_busy = False
        else:
            quiescence = rp.assess_quiescence(
                queue_entries=board.get("drive_queue") or [],
                assignments=board.get("assignments") or [],
                issues=board.get("issues") or [],
                now=arm_now,
            )
            queue_provably_busy = _queue_provably_busy(quiescence, daemon_name)
        refusal = _fresh_arm_refusal_reason(
            ledger, now=arm_now, queue_provably_busy=queue_provably_busy,
        )
        if refusal:
            record.error = (
                f"declined to arm a roll-pending marker for v{record.target_version} "
                f"— {refusal}"
            )
            click.echo(f"note: {record.error}")
            if ledger.escalated:
                from coord.commands.drive_queue import _escalate_roll_ledger  # noqa: PLC0415

                _escalate_roll_ledger(ledger, now=arm_now)
                _escalate_window(record, reason=record.error)
                _finish(rw.STATUS_LEDGER_ESCALATED, 1)
            _finish(rw.STATUS_ARM_DEFERRED, 0)
        write_roll_pending(
            RollPending(
                target_version=record.target_version,
                set_at=arm_now,
                reason="nightly-window",
                min_releases_behind=effective_min_behind,
            )
        )
        # Nothing to write to the ledger here — only an EXPIRY moves its
        # rate-limit clock (`RollLedger.record_expiry`), never an arm.
    click.echo(
        f"roll pending: v{record.target_version} — the drive-queue tick will "
        f"fire `coord release propagate` at the next inter-drive gap "
        # See the --dry-run branch above for why this names
        # `rw.DEFAULT_QUEUE_TIMER`, not the ignored `--queue-timer` value.
        f"(#2587); {rw.DEFAULT_QUEUE_TIMER} is never stopped"
    )
    _finish(rw.STATUS_ROLL_PENDING, 0)


@release_group.command(
    "window-history",
    help="What the nightly release window actually did, and when (#2112).",
)
@click.option("--limit", default=40, show_default=True,
              help="Show at most this many recorded attempts (most recent last).")
@click.option("--json", "as_json", is_flag=True, help="Emit the raw records as JSON.")
def release_window_history(limit: int, as_json: bool) -> None:
    """Read the `coord release nightly-window` journal.

    Separate from `coord release history` (`release_propagate`'s journal):
    this record carries fields — the queue stop/drain/restart outcome — a
    plain propagate attempt does not have.
    """
    import json as _json  # noqa: PLC0415

    from coord import release_window as rw  # noqa: PLC0415

    records = rw.read_records(_state_dir(), limit=limit)
    if as_json:
        click.echo(_json.dumps(records, indent=2, sort_keys=True))
        return
    if not records:
        click.echo("no nightly-window attempts recorded yet")
        return
    for rec in records:
        click.echo("\n".join(rw.render_record(rec)))
        click.echo("")


# Same callback under the group, so `coord release preflight` and `coord
# release verify` are one discoverable pair. The flat `coord
# release-preflight` above keeps working unchanged.
release_group.add_command(release_preflight, name="preflight")
