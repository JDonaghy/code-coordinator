"""`coord init`/`config`/`version`/`install-skills` — one-time and
diagnostic setup commands. Extracted from coord/cli.py (#747)."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import click
import httpx

from coord import __version__

from coord.commands._common import _CONFIG_OPTION, _load_config
import json

if TYPE_CHECKING:  # pragma: no cover — typing only
    from coord.config import Config
    from coord.drive_queue import CeilingResolution


@click.command(help="Print the coord version.")
def version() -> None:
    click.echo(f"coord {__version__}")


@click.command(
    "store-backend",
    help=(
        "Print the resolved store backend (sqlite/postgres) for THIS machine, "
        "reading local config only -- no daemon, no network. Output on "
        "success is one line: '<backend>' or '<backend> <redacted-target>'."
    ),
)
def store_backend_cmd() -> None:
    """Deliberately the narrowest possible surface over
    :func:`coord.db.resolve_store_backend` (#3084/#3085).

    Every other config-reading command in this file (``config_cmd`` below,
    ``coord doctor``) goes through ``_load_config``, which -- correctly, for
    those commands -- redirects to the daemon over the network when this
    machine is a thin client (``resolve_board_service()`` returns non-None,
    e.g. `precision`'s ``~/.coord/client.toml``, no local
    ``coordinator.yml`` at all -- see ``tests/test_ambient_home_isolation.py``
    for the incident that shape caused). ``resolve_store_backend()`` reads
    THIS machine's local ``coordinator.yml``/``$COORD_CONFIG`` directly and
    never redirects -- exactly the "which store am I on, right here" question
    a shell script consuming this command's stdout needs answered without a
    network round trip, an unreachable-daemon failure mode, or a
    fleet-wide config it didn't ask for. (``coord doctor``'s own
    store-backend section makes the identical local-only call for the
    identical reason -- see its docstring.)

    A malformed *explicit* ``store:`` block still fails loud here -- same
    contract as ``resolve_store_backend()`` itself: that one config problem
    must produce a nonzero exit rather than silently answering "sqlite".
    Callers such as ``deploy/coord-db-backup.sh`` treat any nonzero exit from
    this command as "could not determine the backend" and refuse to guess.
    ``ConfigError`` is caught here and turned into a single curated line on
    stderr rather than left to propagate as a raw Python traceback -- still a
    loud nonzero exit naming the problem, just a cleaner one for an on-call
    engineer reading a systemd journal (a shell script embeds this command's
    stderr verbatim into its own failure message; a multi-line traceback
    there is noise, not signal).
    """
    from coord.config import ConfigError  # noqa: PLC0415
    from coord.db import resolve_store_backend  # noqa: PLC0415

    try:
        backend, redacted_target = resolve_store_backend()
    except ConfigError as exc:
        click.echo(f"store-backend: invalid store config: {exc}", err=True)
        raise SystemExit(1) from None
    if redacted_target:
        click.echo(f"{backend} {redacted_target}")
    else:
        click.echo(backend)


@click.command("config", help="Load coordinator.yml and pretty-print the parsed config.")
@click.option(
    "--effective",
    "effective",
    is_flag=True,
    default=False,
    help=(
        "Print every concurrency ceiling's EFFECTIVE value and provenance "
        "instead of the parsed config (#3408): which source won (a "
        "machine-local systemd unit's hardcoded flag, an explicit CLI "
        "flag, coordinator.yml, or a derived default), what any losing "
        "source said, and current usage against each ceiling. Answers "
        "'why isn't this issue being worked' without an SSH in, a "
        "`systemctl --user cat`, and a coordinator.yml grep."
    ),
)
@click.option(
    "--max-parallel",
    "sim_max_parallel",
    type=int,
    default=None,
    help=(
        "Only with --effective: report as though `coord drive-queue tick` "
        "were invoked with this explicit --max-parallel (wins outright, "
        "even over a detected systemd override) -- 'what would tick "
        "resolve to if I passed this flag'. Resolves nothing by itself."
    ),
)
@click.option(
    "--max-parallel-per-repo",
    "sim_max_parallel_per_repo",
    type=int,
    default=None,
    help="Only with --effective: the same simulation for --max-parallel-per-repo.",
)
@_CONFIG_OPTION
def config_cmd(
    config_path: Path,
    effective: bool,  # noqa: FBT001
    sim_max_parallel: int | None,
    sim_max_parallel_per_repo: int | None,
) -> None:
    cfg = _load_config(config_path)
    if effective:
        _print_effective_concurrency(
            cfg,
            sim_max_parallel=sim_max_parallel,
            sim_max_parallel_per_repo=sim_max_parallel_per_repo,
        )
        return
    click.echo(f"# {cfg.path}")
    click.echo("")
    click.echo("Repos:")
    for r in cfg.repos:
        deps = f"  depends_on: {', '.join(r.depends_on)}" if r.depends_on else "  depends_on: (none)"
        click.echo(f"  - {r.name} ({r.github}) [branch: {r.default_branch}]")
        click.echo(f"  {deps}")
    click.echo("")
    click.echo("Machines:")
    for m in cfg.machines:
        caps = ", ".join(m.capabilities) if m.capabilities else "(none)"
        repos = ", ".join(m.repos) if m.repos else "(none)"
        click.echo(f"  - {m.name} @ {m.host}")
        click.echo(f"    capabilities: {caps}")
        click.echo(f"    repos: {repos}")
    # #2783 — non-fatal parse-time warnings (e.g. an unrecognised repos[]
    # key). Advisory only: the config above loaded and is fully usable.
    if cfg.warnings:
        click.echo("")
        click.echo("Warnings:")
        for w in cfg.warnings:
            click.echo(f"  ! {w}")


def _print_effective_concurrency(
    cfg: "Config",
    *,
    sim_max_parallel: int | None,
    sim_max_parallel_per_repo: int | None,
) -> None:
    """`coord config --effective` (#3408): print every concurrency ceiling
    coord actually enforces, its resolved value, which source won, and what
    any losing source said. This is the whole point — a bare effective
    number would not have told #3408's operator that pushing
    `pipeline.max_parallel` to `coord-settings` was futile while a
    machine-local systemd unit's hardcoded ``--max-parallel`` outranks it.

    Calls the SAME `coord.drive_queue.resolve_max_parallel`/
    `resolve_max_parallel_per_repo` functions
    `coord.commands.drive_queue.drive_queue_tick` uses to decide what it
    actually launches, so this can never show a different answer than the
    tick that enforces it (#2085's "one question, one answer").

    *sim_max_parallel*/*sim_max_parallel_per_repo* let an operator ask "what
    would tick resolve to if I passed this flag" without actually running
    one — they win outright, even over a detected systemd override, exactly
    as an explicit CLI flag would on the real `coord drive-queue tick`
    invocation. Left `None` (the default), this instead reports the real
    machine's own installed systemd unit, if any.
    """
    from coord.drive_queue import (  # noqa: PLC0415
        flag_shadows_config_warning,
        read_systemd_max_parallel_flags,
        resolve_max_parallel,
        resolve_max_parallel_per_repo,
    )

    systemd_flags = read_systemd_max_parallel_flags()

    per_repo_override = sim_max_parallel_per_repo
    per_repo_source = "--max-parallel-per-repo flag"
    if per_repo_override is None and "max_parallel_per_repo" in systemd_flags:
        per_repo_override = systemd_flags["max_parallel_per_repo"]
        per_repo_source = "systemd ExecStart --max-parallel-per-repo"

    per_repo_resolution = resolve_max_parallel_per_repo(
        override_value=per_repo_override,
        override_source=per_repo_source,
        config_value=cfg.pipeline.max_parallel_per_repo,
    )
    per_repo_warning = flag_shadows_config_warning(
        flag_name="max-parallel-per-repo",
        override_value=per_repo_override,
        config_key="pipeline.max_parallel_per_repo",
        config_value=cfg.pipeline.max_parallel_per_repo,
    )

    max_parallel_override = sim_max_parallel
    max_parallel_source = "--max-parallel flag"
    if max_parallel_override is None and "max_parallel" in systemd_flags:
        max_parallel_override = systemd_flags["max_parallel"]
        max_parallel_source = "systemd ExecStart --max-parallel"

    global_resolution = resolve_max_parallel(
        override_value=max_parallel_override,
        override_source=max_parallel_source,
        config_value=cfg.pipeline.max_parallel,
        repo_count=len(cfg.repos),
        max_parallel_per_repo=per_repo_resolution.value,
        max_workers_cap=cfg.concurrency.max_workers,
    )
    global_warning = flag_shadows_config_warning(
        flag_name="max-parallel",
        override_value=max_parallel_override,
        config_key="pipeline.max_parallel",
        config_value=cfg.pipeline.max_parallel,
    )

    click.echo(f"# {cfg.path}")
    click.echo("")
    click.echo("Effective concurrency ceilings (#3408):")
    _echo_ceiling(global_resolution)
    if global_warning:
        click.echo(f"  {global_warning}")
    _echo_ceiling(per_repo_resolution)
    if per_repo_warning:
        click.echo(f"  {per_repo_warning}")
    click.echo(
        f"  {'concurrency.max_workers':<24} {cfg.concurrency.max_workers}"
        "  <- coordinator.yml concurrency.max_workers"
    )
    click.echo("")
    click.echo(_effective_usage_line(cfg, global_resolution, per_repo_resolution))


def _echo_ceiling(resolution: "CeilingResolution") -> None:
    line = f"  {resolution.name:<24} {resolution.value}  <- {resolution.source}"
    if resolution.losing:
        losers = ", ".join(f"{name}: {value}" for name, value in resolution.losing)
        line += f"  (losing: {losers})"
    click.echo(line)


def _effective_usage_line(
    cfg: "Config",
    global_resolution: "CeilingResolution",
    per_repo_resolution: "CeilingResolution",
) -> str:
    """Current in-flight usage against the two resolved ceilings above
    (#3408 acceptance: "a queue at its global ceiling reports N/N rather
    than rendering as idle"). Best-effort — a board/queue read needs a
    reachable daemon or a readable local DB, neither of which this command
    otherwise depends on, so any failure here is reported inline rather
    than failing the whole command; the ceilings above are the answer that
    matters most and must still print.

    Uses `coord.drive_queue.compute_running_occupancy`, the SAME
    "is this entry still occupying a slot" verdict `plan_tick` itself
    computes (via `_reconcile_running`) — not a second, independently
    counted number that could disagree with it (#2085).
    """
    try:
        from coord.commands.drive_queue import _fetch_board_view_with_retry  # noqa: PLC0415
        from coord.drive_queue import (  # noqa: PLC0415
            DEFAULT_MAX_ATTEMPTS,
            compute_running_occupancy,
            entries_from_rows,
        )
        from coord.state import list_drive_queue  # noqa: PLC0415

        board = _fetch_board_view_with_retry()
        entries = entries_from_rows(list_drive_queue())
        occupied, repo_occupied = compute_running_occupancy(
            entries, board, DEFAULT_MAX_ATTEMPTS
        )
    except Exception as exc:  # noqa: BLE001 — best-effort; ceilings above still print
        return f"in flight  unavailable ({exc})"

    line = f"in flight  {occupied}/{global_resolution.value} global"
    per_repo_bits = " · ".join(
        f"{repo} {count}/{per_repo_resolution.value}"
        for repo, count in sorted(repo_occupied.items())
    )
    if per_repo_bits:
        line += f" · {per_repo_bits}"
    return line


def _ensure_coord_permissions(cwd: Path) -> None:
    """Check .claude/settings.local.json for Bash(coord *) / Bash(coord) entries.

    If either is absent, prompt the user and add both.  Skips silently when
    both entries are already present.
    """
    settings_dir = cwd / ".claude"
    settings_path = settings_dir / "settings.local.json"

    COORD_PERMS = ["Bash(coord *)", "Bash(coord)"]

    # Read existing settings or start fresh.
    data: dict = {}
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}

    existing_allow: list = data.get("permissions", {}).get("allow", [])
    missing = [p for p in COORD_PERMS if p not in existing_allow]

    if not missing:
        return  # Already configured — nothing to do.

    click.echo("\n── Claude Code permissions ──")
    click.echo(
        "  .claude/settings.local.json is missing: "
        + ", ".join(missing)
    )
    if click.confirm(
        "  Add Bash(coord *) and Bash(coord) to allow list?",
        default=True,
    ):
        if "permissions" not in data:
            data["permissions"] = {}
        if "allow" not in data["permissions"]:
            data["permissions"]["allow"] = []
        for perm in missing:
            if perm not in data["permissions"]["allow"]:
                data["permissions"]["allow"].append(perm)

        settings_dir.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(data, indent=2) + "\n")
        click.echo(f"  Updated: {settings_path}")
    else:
        click.echo(
            "  Skipped. Add them manually to .claude/settings.local.json."
        )


@click.command(help="Interactive setup; generates coordinator.yml.")
def init() -> None:
    cwd = Path(os.getcwd())
    config_file = cwd / "coordinator.yml"

    # ── Step 1: Check for existing config ───────────────────────────────
    if config_file.exists():
        if not click.confirm(
            "coordinator.yml already exists. Overwrite?", default=False
        ):
            click.echo("Aborted.")
            return

    # ── Step 2: Detect current machine ──────────────────────────────────
    click.echo("\n── Machine setup ──")
    hostname = socket.gethostname()
    short_hostname = hostname.split(".")[0]
    machine_name = click.prompt("Machine name", default=short_hostname)

    detected_caps: list[str] = []
    # gtk: check via pkg-config
    try:
        subprocess.run(
            ["pkg-config", "--exists", "gtk4"],
            capture_output=True,
            check=True,
        )
        detected_caps.append("gtk")
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    # rust
    if shutil.which("cargo"):
        detected_caps.append("rust")
    # python
    if shutil.which("python3"):
        detected_caps.append("python")
    # docker
    if shutil.which("docker"):
        detected_caps.append("docker")
    # node
    if shutil.which("node"):
        detected_caps.append("node")

    if detected_caps:
        click.echo(f"Detected capabilities: {', '.join(detected_caps)}")
    else:
        click.echo("No capabilities auto-detected.")
    caps_input = click.prompt(
        "Capabilities (comma-separated)", default=",".join(detected_caps)
    )
    capabilities = [c.strip() for c in caps_input.split(",") if c.strip()]

    # ── Step 3: Discover repos ──────────────────────────────────────────
    click.echo("\n── Repo discovery ──")
    candidate_dirs: list[Path] = []
    # Scan cwd
    if (cwd / ".git").is_dir():
        candidate_dirs.append(cwd)
    # Scan ~/src/
    src_dir = Path.home() / "src"
    if src_dir.is_dir():
        for child in sorted(src_dir.iterdir()):
            if child.is_dir() and (child / ".git").is_dir():
                if child.resolve() != cwd.resolve():
                    candidate_dirs.append(child)

    # For each candidate, try to get the GitHub remote
    discovered: list[dict] = []
    for d in candidate_dirs:
        try:
            result = subprocess.run(
                ["git", "-C", str(d), "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                check=True,
            )
            remote_url = result.stdout.strip()
            gh = _parse_github_remote(remote_url)
            if gh:
                repo_name = gh.split("/")[-1]
                discovered.append(
                    {"name": repo_name, "github": gh, "path": str(d)}
                )
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

    if not discovered:
        click.echo("No git repos with GitHub remotes found in cwd or ~/src/.")
        click.echo("You can edit coordinator.yml manually to add repos.")
        # Write a minimal config with just the machine
        yaml_str = _build_init_yaml(
            repos=[],
            machines=[
                {
                    "name": machine_name,
                    "host": hostname,
                    "capabilities": capabilities,
                    "repos": [],
                    "repo_paths": {},
                }
            ],
            max_workers=2,
            stagger_seconds=30,
        )
        config_file.write_text(yaml_str)
        click.echo(f"\nCreated coordinator.yml with 0 repos and 1 machine.")
        click.echo("Next: edit coordinator.yml to add repos, then run 'coord agent'.")
        _ensure_coord_permissions(cwd)
        return

    click.echo("Found repos:")
    for i, r in enumerate(discovered, 1):
        click.echo(f"  [{i}] {r['github']} ({r['path']})")

    selection = click.prompt(
        'Which repos to include? (comma-separated numbers or "all")',
        default="all",
    )
    if selection.strip().lower() == "all":
        selected_repos = list(discovered)
    else:
        try:
            indices = [int(x.strip()) for x in selection.split(",")]
            selected_repos = [discovered[i - 1] for i in indices if 1 <= i <= len(discovered)]
        except (ValueError, IndexError):
            click.echo("Invalid selection — including all repos.")
            selected_repos = list(discovered)

    if not selected_repos:
        click.echo("No repos selected.")
        selected_repos = []

    # Gather per-repo details
    repos_config: list[dict] = []
    repo_names = [r["name"] for r in selected_repos]
    for r in selected_repos:
        click.echo(f"\n  Configuring {r['name']} ({r['github']}):")

        # Detect default branch
        default_branch = "main"
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    r["path"],
                    "symbolic-ref",
                    "refs/remotes/origin/HEAD",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            ref = result.stdout.strip()  # e.g. refs/remotes/origin/main
            default_branch = ref.rsplit("/", 1)[-1]
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

        default_branch = click.prompt("    Default branch", default=default_branch)

        # Dependencies
        other_repos = [n for n in repo_names if n != r["name"]]
        if other_repos:
            deps_input = click.prompt(
                f"    Dependencies ({', '.join(other_repos)} or none)",
                default="none",
            )
            if deps_input.strip().lower() == "none":
                deps: list[str] = []
            else:
                deps = [d.strip() for d in deps_input.split(",") if d.strip()]
        else:
            deps = []

        build_cmd = click.prompt("    Build command (enter to skip)", default="", show_default=False)
        test_cmd = click.prompt("    Test command (enter to skip)", default="", show_default=False)

        repos_config.append(
            {
                "name": r["name"],
                "github": r["github"],
                "depends_on": deps,
                "default_branch": default_branch,
                "build_command": build_cmd or None,
                "test_command": test_cmd or None,
                "path": r["path"],
            }
        )

    # Build this machine's repo list and paths
    local_repo_names = [r["name"] for r in repos_config]
    local_repo_paths = {r["name"]: r["path"] for r in repos_config}

    machines_config: list[dict] = [
        {
            "name": machine_name,
            "host": hostname,
            "capabilities": capabilities,
            "repos": local_repo_names,
            "repo_paths": local_repo_paths,
        }
    ]

    # ── Step 4: Ask about other machines ────────────────────────────────
    click.echo("\n── Additional machines ──")
    while click.confirm("Add another machine?", default=False):
        m_name = click.prompt("  Machine name")
        m_host = click.prompt("  Tailscale hostname")
        m_caps_input = click.prompt("  Capabilities (comma-separated)", default="")
        m_caps = [c.strip() for c in m_caps_input.split(",") if c.strip()]

        click.echo(f"  Available repos: {', '.join(local_repo_names)}")
        m_repos_input = click.prompt(
            '  Which repos? (comma-separated names or "all")', default="all"
        )
        if m_repos_input.strip().lower() == "all":
            m_repos = list(local_repo_names)
        else:
            m_repos = [r.strip() for r in m_repos_input.split(",") if r.strip() in local_repo_names]

        m_repo_paths: dict[str, str] = {}
        for rn in m_repos:
            m_repo_paths[rn] = click.prompt(f"  Path to {rn} on {m_name}", default=f"~/src/{rn}")

        # Try to reach the machine
        try:
            resp = httpx.get(f"http://{m_host}:7433/health", timeout=3)
            click.echo(f"  ✓ {m_host} is reachable (HTTP {resp.status_code})")
        except Exception:
            click.echo(f"  ✗ {m_host} is not reachable (agent may not be running yet)")

        machines_config.append(
            {
                "name": m_name,
                "host": m_host,
                "capabilities": m_caps,
                "repos": m_repos,
                "repo_paths": m_repo_paths,
            }
        )

    # ── Step 5: Concurrency settings ────────────────────────────────────
    click.echo("\n── Concurrency settings ──")
    max_workers = click.prompt("Max concurrent workers", default=2, type=int)
    stagger_seconds = click.prompt(
        "Stagger seconds between dispatches", default=30, type=int
    )

    # ── Step 6: Generate coordinator.yml ────────────────────────────────
    yaml_str = _build_init_yaml(
        repos=repos_config,
        machines=machines_config,
        max_workers=max_workers,
        stagger_seconds=stagger_seconds,
    )
    config_file.write_text(yaml_str)

    # ── Step 7: Validate ────────────────────────────────────────────────
    try:
        from coord.config import load as load_config

        load_config(config_file)
    except Exception as e:
        click.echo(f"\nWarning: generated config has a validation error: {e}", err=True)
        click.echo("You may need to edit coordinator.yml manually.", err=True)
        return

    # ── Step 8: Print next steps ────────────────────────────────────────
    click.echo(
        f"\nCreated coordinator.yml with {len(repos_config)} repo(s) "
        f"and {len(machines_config)} machine(s)."
    )
    click.echo("Next: start the agent with 'coord agent', then run 'coord plan'.")

    # ── Step 9: Ensure Claude Code permissions are configured ────────────
    _ensure_coord_permissions(cwd)


def _parse_github_remote(url: str) -> str | None:
    """Extract owner/repo from a GitHub remote URL.

    Handles both:
      git@github.com:owner/repo.git
      https://github.com/owner/repo.git
    """
    import re

    # SSH format
    m = re.match(r"git@github\.com:(.+?)(?:\.git)?$", url)
    if m:
        return m.group(1)
    # HTTPS format
    m = re.match(r"https?://github\.com/(.+?)(?:\.git)?$", url)
    if m:
        return m.group(1)
    return None


def _yaml_scalar(value: str) -> str:
    """Quote a YAML scalar if it contains special chars, otherwise return bare."""
    if not value:
        return '""'
    needs_quoting = any(c in value for c in ":#{}[]|>&*!%@`,?") or value != value.strip()
    if needs_quoting:
        escaped = value.replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _build_init_yaml(
    repos: list[dict],
    machines: list[dict],
    max_workers: int,
    stagger_seconds: int,
) -> str:
    """Build a coordinator.yml string with inline comments."""
    lines: list[str] = []

    # Repos
    lines.append("repos:")
    if not repos:
        lines.append("  # Add repos here. Example:")
        lines.append("  # - name: my-project")
        lines.append("  #   github: owner/my-project")
        lines.append("  #   default_branch: main")
        lines.append("  #   build_command: make build")
        lines.append("  #   test_command: make test")
        lines.append("  []")
    else:
        for r in repos:
            lines.append(f"  - name: {_yaml_scalar(r['name'])}")
            lines.append(f"    github: {_yaml_scalar(r['github'])}")
            deps = r.get("depends_on", [])
            if deps:
                deps_str = ", ".join(deps)
                lines.append(f"    depends_on: [{deps_str}]")
            else:
                lines.append("    depends_on: []")
            lines.append(f"    default_branch: {_yaml_scalar(r['default_branch'])}")
            if r.get("build_command"):
                lines.append(f"    build_command: {_yaml_scalar(r['build_command'])}")
            if r.get("test_command"):
                lines.append(f"    test_command: {_yaml_scalar(r['test_command'])}")
            lines.append("")

    lines.append("")
    lines.append("machines:")
    for m in machines:
        lines.append(f"  - name: {_yaml_scalar(m['name'])}")
        lines.append(f"    host: {_yaml_scalar(m['host'])}")
        caps = m.get("capabilities", [])
        if caps:
            caps_str = ", ".join(caps)
            lines.append(f"    capabilities: [{caps_str}]")
        else:
            lines.append("    capabilities: []")
        mrepos = m.get("repos", [])
        if mrepos:
            repos_str = ", ".join(mrepos)
            lines.append(f"    repos: [{repos_str}]")
        else:
            lines.append("    repos: []")
        rpaths = m.get("repo_paths", {})
        if rpaths:
            lines.append("    repo_paths:")
            for rn, rp in rpaths.items():
                lines.append(f"      {rn}: {_yaml_scalar(rp)}")
        lines.append("")

    lines.append("")
    lines.append("# Concurrency settings")
    lines.append("concurrency:")
    lines.append(f"  max_workers: {max_workers}          # max simultaneous claude -p sessions")
    lines.append(f"  stagger_seconds: {stagger_seconds}     # delay between starting workers")
    lines.append("")
    lines.append("# Lifecycle hooks (optional)")
    lines.append("# hooks:")
    lines.append("#   on_round_complete:")
    lines.append("#     - summary_report")
    lines.append("#   on_session_end:")
    lines.append("#     - summary_report")
    lines.append("")

    return "\n".join(lines) + "\n"


def list_bundled_skill_dirs() -> list[tuple[str, object]]:
    """Enumerate bundled `coord/skills/*/SKILL.md` directories (#319).

    Returns ``(skill_name, entry)`` pairs, sorted by name, for every
    sub-directory of the installed `coord.skills` package that contains a
    readable ``SKILL.md``. Raises ``ModuleNotFoundError``/``TypeError`` if
    the package itself can't be located, or ``FileNotFoundError``/
    ``NotADirectoryError`` if its directory listing can't be read — callers
    decide how to surface that (``install_skills`` below exits 1; the
    agent-side self-heal in `agent.py` logs and moves on, since a skills
    hiccup must never block `/health`).
    """
    import importlib.resources as _ilr  # noqa: PLC0415

    skills_ref = _ilr.files("coord").joinpath("skills")
    skill_dirs: list[tuple[str, object]] = []
    for entry in skills_ref.iterdir():
        skill_name = entry.name  # type: ignore[attr-defined]
        skill_file = entry.joinpath("SKILL.md")
        try:
            skill_file.read_text(encoding="utf-8")
            skill_dirs.append((skill_name, entry))
        except (FileNotFoundError, IsADirectoryError, TypeError):
            pass
    return sorted(skill_dirs)


def sync_bundled_skills(
    target_root: Path, skill_dirs: list[tuple[str, object]]
) -> list[tuple[str, str]]:
    """Write each ``(skill_name, entry)`` from *skill_dirs* into
    *target_root* (normally ``~/.claude/skills/``) if missing or changed
    (#319 / agent self-heal).

    Returns ``(skill_name, action)`` pairs, ``action`` one of ``"installed"``,
    ``"updated"``, or ``"unchanged"`` — content-identical skills are left
    untouched (no rewrite, no mtime bump) so a periodic caller can run this
    every tick without churn. Does not create *target_root* itself; callers
    create it first once they know there is something to write.
    """
    results: list[tuple[str, str]] = []
    for skill_name, skill_dir_ref in skill_dirs:
        skill_file_dest = target_root / skill_name / "SKILL.md"
        src_text = skill_dir_ref.joinpath("SKILL.md").read_text(encoding="utf-8")  # type: ignore[attr-defined]
        if skill_file_dest.exists() and skill_file_dest.read_text(encoding="utf-8") == src_text:
            results.append((skill_name, "unchanged"))
            continue
        action = "updated" if skill_file_dest.exists() else "installed"
        skill_file_dest.parent.mkdir(parents=True, exist_ok=True)
        skill_file_dest.write_text(src_text, encoding="utf-8")
        results.append((skill_name, action))
    return results


@click.command(
    "install-skills",
    help=(
        "Copy bundled coordinator skills to ~/.claude/skills/ so they are "
        "available as slash commands inside Claude Code sessions. "
        "No repo clone required — reads from the installed PyPI package."
    ),
)
@click.option(
    "--list",
    "do_list",
    is_flag=True,
    default=False,
    help="Show bundled skills and their installed status without copying.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print what would be installed without writing any files.",
)
def install_skills(do_list: bool, dry_run: bool) -> None:  # noqa: FBT001
    """Install bundled coordinator skills to ~/.claude/skills/ (#319)."""
    if do_list and dry_run:
        click.echo("warning: --dry-run has no effect when --list is used", err=True)

    target_root = Path.home() / ".claude" / "skills"

    try:
        skill_dirs = list_bundled_skill_dirs()
    except (TypeError, ModuleNotFoundError) as e:
        click.echo(f"error: cannot locate bundled skills: {e}", err=True)
        sys.exit(1)
    except (FileNotFoundError, NotADirectoryError) as e:
        click.echo(f"error: bundled skills directory not readable: {e}", err=True)
        sys.exit(1)

    if not skill_dirs:
        click.echo("No bundled skills found in the installed package.")
        return

    if do_list:
        click.echo("Bundled skills:")
        for skill_name, _ in skill_dirs:
            installed_path = target_root / skill_name / "SKILL.md"
            status = "installed" if installed_path.exists() else "not installed"
            click.echo(f"  {skill_name:30s}  {status}")
        return

    if dry_run:
        for skill_name, _ in skill_dirs:
            skill_file_dest = target_root / skill_name / "SKILL.md"
            action = "update" if skill_file_dest.exists() else "install"
            click.echo(f"  would {action}: {skill_file_dest}")
        return

    target_root.mkdir(parents=True, exist_ok=True)
    for skill_name, action in sync_bundled_skills(target_root, skill_dirs):
        if action == "unchanged":
            continue
        click.echo(f"  {action}: {target_root / skill_name / 'SKILL.md'}")

    installed_names = sorted(name for name, _ in skill_dirs)
    cmd_list = "  ".join(f"/{n}" for n in installed_names)
    click.echo(f"\nDone. Available skills: {cmd_list}")
    click.echo("Type a skill name inside a Claude Code session to use it.")