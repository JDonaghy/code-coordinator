"""``coord terminal`` — persistent, fleet-wide plain shell sessions (#952).

Keystone for the "TUI: Fleet Terminal Manager" milestone (#26). Unlike the
standalone Terminal view's ephemeral local ``$SHELL`` pty
(``tui/src/app/terminal.rs``), and unlike interactive ``claude`` sessions
(``coord-<assignment_id>`` tmux sessions, ``coord/interactive.py``), a
*terminal* session is a free-floating shell with **no** issue/repo/assignment
attached to it. It lives entirely in tmux — there is no on-disk sessions
file, no board row, no pipeline/merge-queue involvement — so it is created,
listed, killed, and attached purely by talking to tmux (locally or over
``ssh``, reusing the :class:`~coord.interactive.TmuxHost` seam).

Naming convention: ``coord-term-<slug>`` (:data:`~coord.interactive.
TERM_SESSION_PREFIX`) — a prefix distinct from ``coord-<assignment_id>``
(:data:`~coord.interactive.TMUX_SESSION_PREFIX`) so the two kinds of tmux
session never collide and each side filters only its own:
:func:`~coord.interactive.list_coord_tmux_sessions` (assignment discovery,
consumed by ``coord sessions``/reattach) explicitly excludes
``coord-term-*``; :func:`list_tmux_terminal_sessions` here matches *only*
that prefix.
"""

from __future__ import annotations

import json as _json
import secrets
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import click

from coord.commands._common import _CONFIG_OPTION, _load_config
from coord.interactive import TERM_SESSION_PREFIX, TmuxHost, tmux_available, tmux_session_alive

if TYPE_CHECKING:
    from coord.config import Config
    from coord.models import Machine

__all__ = [
    "terminal_group",
    "terminal_session_name",
    "parse_terminal_slug",
    "generate_slug",
    "parse_machine_qualified_name",
    "list_tmux_terminal_sessions",
]


# ── pure helpers (unit-testable without Click / a real tmux) ────────────────


def terminal_session_name(slug: str) -> str:
    """Return the canonical tmux session name for a terminal *slug*."""
    return f"{TERM_SESSION_PREFIX}{slug}"


def parse_terminal_slug(session_name: str) -> str | None:
    """Return the slug for a ``coord-term-<slug>`` session name.

    Returns ``None`` when *session_name* does not carry the terminal
    prefix (e.g. a ``coord-<assignment_id>`` session) or has an empty slug.
    """
    if not session_name.startswith(TERM_SESSION_PREFIX):
        return None
    slug = session_name[len(TERM_SESSION_PREFIX):]
    return slug or None


def generate_slug() -> str:
    """Return a short, human-typeable random slug (6 hex chars)."""
    return secrets.token_hex(3)


def parse_machine_qualified_name(target: str) -> tuple[str | None, str]:
    """Split ``"machine:name"`` into ``(machine, name)``; bare ``"name"`` → ``(None, name)``.

    Only the FIRST ``:`` is significant — slugs never contain one.
    """
    if ":" in target:
        machine, _, name = target.partition(":")
        return (machine or None), name
    return None, target


def list_tmux_terminal_sessions(*, host: TmuxHost = TmuxHost(None)) -> list[dict]:
    """Return live ``coord-term-*`` sessions on *host* as parsed dicts.

    Each entry: ``{"name": <slug>, "attached": bool, "created": <iso8601>?}``.
    Uses a single ``tmux list-sessions -F …`` call; returns ``[]`` when tmux
    is unavailable, has no server running, or has no matching sessions —
    callers never need to distinguish those cases.
    """
    try:
        result = subprocess.run(
            host.cmd([
                "list-sessions", "-F",
                "#{session_name}\t#{session_attached}\t#{session_created}",
            ]),
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    if result.returncode != 0:
        return []

    sessions: list[dict] = []
    for raw_line in result.stdout.splitlines():
        parts = raw_line.split("\t")
        if len(parts) < 3:
            continue
        name, attached_raw, created_raw = (p.strip() for p in parts[:3])
        slug = parse_terminal_slug(name)
        if slug is None:
            continue
        entry: dict = {
            "name": slug,
            "attached": attached_raw not in ("", "0"),
        }
        if created_raw.isdigit():
            entry["created"] = (
                datetime.fromtimestamp(int(created_raw), tz=timezone.utc).isoformat()
            )
        sessions.append(entry)
    return sessions


def _local_short_hostname() -> str:
    return socket.gethostname().split(".")[0].lower()


def _is_local_machine(cfg: "Config", machine: "Machine") -> bool:
    """Is *machine* the host this process is running on?

    Routes through :func:`coord.config.resolve_local_machine` (#3440) — the
    shared "is this the local machine or must I SSH?" answer — rather than a
    private hostname comparison.
    """
    from coord.config import resolve_local_machine  # noqa: PLC0415

    resolved = resolve_local_machine(cfg)
    return resolved is not None and resolved.name == machine.name


def _resolve_machine_host(
    machine_name: str | None, config_path: Path
) -> tuple[TmuxHost, str | None]:
    """Resolve *machine_name* (from config) to a :class:`TmuxHost`.

    Returns ``(TmuxHost(ssh_target=None), None)`` when *machine_name* is
    ``None`` (the local/default path — no config lookup needed).  Exits the
    process with an error message when *machine_name* is given but not
    found in ``coordinator.yml``'s ``machines`` table.
    """
    if not machine_name:
        return TmuxHost(ssh_target=None), None

    cfg = _load_config(config_path)
    machine = next((m for m in cfg.machines if m.name == machine_name), None)
    if machine is None:
        click.echo(f"error: machine {machine_name!r} not in config", err=True)
        sys.exit(1)

    if _is_local_machine(cfg, machine):
        return TmuxHost(ssh_target=None), machine.name
    return TmuxHost(ssh_target=machine.host), machine.name


# ── Click group ──────────────────────────────────────────────────────────────


@click.group(
    "terminal",
    help=(
        "Persistent, fleet-wide plain shell sessions hosted in tmux "
        "(coord-term-* named sessions, #952). Distinct from `coord sessions` "
        "(interactive claude/assignment sessions) — these carry no "
        "issue/repo/assignment and never enter the board or pipeline."
    ),
)
def terminal_group() -> None:
    pass


@terminal_group.command(
    "new",
    help=(
        "Create a persistent shell on MACHINE (default: local). "
        "Runs `tmux new-session -d` locally, or over ssh on a fleet machine."
    ),
)
@click.argument("machine", required=False, default=None)
@click.option(
    "--name", "name_opt", default=None,
    help="Slug for the session (auto-generated when omitted).",
)
@_CONFIG_OPTION
def terminal_new(machine: str | None, name_opt: str | None, config_path: Path) -> None:
    if not tmux_available():
        click.echo("error: tmux is not available on this machine.", err=True)
        sys.exit(1)

    host, resolved_machine = _resolve_machine_host(machine, config_path)

    slug = name_opt
    if slug is None:
        # Auto-generate, retrying a handful of times on an (unlikely)
        # collision with an existing session on the same host.
        for _ in range(5):
            candidate = generate_slug()
            if not tmux_session_alive(terminal_session_name(candidate), host=host):
                slug = candidate
                break
        else:
            slug = generate_slug()
    elif tmux_session_alive(terminal_session_name(slug), host=host):
        where = f" on {resolved_machine}" if resolved_machine else ""
        click.echo(f"error: a terminal named {slug!r} already exists{where}.", err=True)
        sys.exit(1)

    sname = terminal_session_name(slug)
    try:
        result = subprocess.run(
            host.cmd(["new-session", "-d", "-s", sname]),
            capture_output=True,
            text=True,
            timeout=15.0,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        click.echo(f"error: failed to create tmux session: {exc}", err=True)
        sys.exit(1)
    if result.returncode != 0:
        click.echo(
            f"error: tmux new-session failed: {(result.stderr or '').strip()}",
            err=True,
        )
        sys.exit(1)

    where = resolved_machine or "local"
    target = f"{resolved_machine}:{slug}" if resolved_machine else slug
    click.echo(f"Created terminal '{slug}' on {where}.")
    click.echo(f"  attach with: coord terminal attach {target}")


@terminal_group.command(
    "list",
    help=(
        "List persistent coord-term-* terminal sessions. "
        "Use --json for machine-readable output (consumed by coord-tui)."
    ),
)
@click.option(
    "--json", "output_json", is_flag=True, default=False,
    help="Output as JSON.",
)
@click.option(
    "--remote", is_flag=True, default=False,
    help="Also sweep every fleet machine over ssh (parallelised, bounded per-host probe).",
)
@_CONFIG_OPTION
def terminal_list(output_json: bool, remote: bool, config_path: Path) -> None:
    try:
        cfg = _load_config(config_path)
    except Exception:  # noqa: BLE001
        cfg = None

    local_hn = _local_short_hostname()
    local_machine_name = local_hn
    local_host_str = local_hn
    if cfg is not None:
        m = next(
            (mm for mm in cfg.machines if _is_local_machine(cfg, mm)), None
        )
        if m is not None:
            local_machine_name = m.name
            local_host_str = m.host

    entries: list[dict] = [
        {**s, "machine": local_machine_name, "host": local_host_str}
        for s in list_tmux_terminal_sessions(host=TmuxHost(None))
    ]

    if remote and cfg is not None:
        remotes = [
            m for m in cfg.machines if not _is_local_machine(cfg, m)
        ]
        if remotes:
            import concurrent.futures as _cf  # noqa: PLC0415

            def _probe(machine: object) -> tuple[object, list[dict]]:
                try:
                    found = list_tmux_terminal_sessions(
                        host=TmuxHost(ssh_target=machine.host, batch=True)  # type: ignore[attr-defined]
                    )
                    return machine, found
                except Exception:  # noqa: BLE001
                    return machine, []

            with _cf.ThreadPoolExecutor(max_workers=min(8, len(remotes))) as ex:
                for machine, found in ex.map(_probe, remotes):
                    for s in found:
                        entries.append(
                            {**s, "machine": machine.name, "host": machine.host}  # type: ignore[attr-defined]
                        )

    if output_json:
        click.echo(_json.dumps(entries))
        return

    if not entries:
        click.echo("No persistent terminal sessions.")
        return

    for e in entries:
        attached_tag = " [attached]" if e.get("attached") else ""
        target = f"{e['machine']}:{e['name']}"
        click.echo(f"  {target}{attached_tag}")
        click.echo(f"    attach with: coord terminal attach {target}")
        click.echo(f"    kill with:   coord terminal kill {target}")


@terminal_group.command(
    "kill",
    help="Kill a persistent terminal session. TARGET is name or machine:name.",
)
@click.argument("target")
@_CONFIG_OPTION
def terminal_kill(target: str, config_path: Path) -> None:
    machine, name = parse_machine_qualified_name(target)
    host, resolved_machine = _resolve_machine_host(machine, config_path)
    sname = terminal_session_name(name)

    if not tmux_session_alive(sname, host=host):
        where = f" on {resolved_machine}" if resolved_machine else ""
        click.echo(f"error: no terminal named {name!r} found{where}.", err=True)
        sys.exit(1)

    try:
        result = subprocess.run(
            host.cmd(["kill-session", "-t", sname]),
            capture_output=True,
            text=True,
            timeout=10.0,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        click.echo(f"error: failed to kill tmux session: {exc}", err=True)
        sys.exit(1)
    if result.returncode != 0:
        click.echo(
            f"error: tmux kill-session failed: {(result.stderr or '').strip()}",
            err=True,
        )
        sys.exit(1)

    where = f" on {resolved_machine}" if resolved_machine else ""
    click.echo(f"Killed terminal '{name}'{where}.")


@terminal_group.command(
    "attach",
    help=(
        "Attach to a persistent terminal session. TARGET is name or "
        "machine:name. Type this into a local PTY exactly as `coord "
        "reattach` is used for claude sessions."
    ),
)
@click.argument("target")
@_CONFIG_OPTION
def terminal_attach(target: str, config_path: Path) -> None:
    import os  # noqa: PLC0415

    machine, name = parse_machine_qualified_name(target)
    host, resolved_machine = _resolve_machine_host(machine, config_path)
    sname = terminal_session_name(name)

    if not tmux_session_alive(sname, host=host):
        where = f" on {resolved_machine}" if resolved_machine else ""
        click.echo(f"error: no terminal named {name!r} found{where}.", err=True)
        sys.exit(1)

    if host.ssh_target is None and os.environ.get("TMUX"):
        # Already inside a tmux client — attach-session refuses to nest;
        # switch-client moves the current client into the target session.
        cmd = ["tmux", "switch-client", "-t", sname]
    else:
        cmd = list(host.cmd(["attach-session", "-t", sname], tty=True))

    try:
        result = subprocess.run(cmd)
    except (subprocess.SubprocessError, OSError) as exc:
        click.echo(f"error: failed to attach: {exc}", err=True)
        sys.exit(1)
    sys.exit(result.returncode)
