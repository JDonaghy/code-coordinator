"""``coord machine add`` / ``coord machine doctor`` — onboarding a machine, and
checking that it actually happened (#2915).

The machine-side analogue of ``coord repo add`` / ``coord repo doctor``
(#2220), and it exists for the same reason, stated in #2220's own rationale: a
runbook is the weakest available answer *"because nothing checks it."*

Adding a machine is still a hand-assembled sequence reconstructed from four
documents that do not cross-reference each other (``docs/AGENT_OPERATIONS.md``,
``docs/GRAPHIFY_SETUP.md``, ``docs/MAC_MINI.md``, ``docs/WSL_WINDOWS_WORKER.md``),
and onboarding ``dell64`` on 2026-08-28 cost six separately-hand-found silent
failures. :mod:`coord.machine_onboard` names each one; this module is the CLI
over it, plus the write half.

**``doctor`` is the one that matters most** — same judgement #2220 made. But
``add`` is not merely a convenience here, because two of the six failures are
*write-time* defects that no verifier can catch after the fact:

* **incident 2** — ``host: dell64`` resolved to a LAN device that shared the
  name, not the tailnet node. So ``add`` refuses a ``--host`` that does not
  resolve to the tailnet address which answers ``/health``, rather than
  trusting the string.
* **incident 4** — a ``repo_paths`` KEY that named the checkout's *directory*
  (``claude-coordinator``) instead of the fleet's *repo name*
  (``code-coordinator``) made the **entire** ``coordinator.yml`` fail to load,
  for every machine. So ``add`` validates every ``--repos`` entry against the
  config's own ``repos:`` names, derives the ``repo_paths`` keys from those
  names, and — like every other write in ``coord.commands.repo`` — re-parses
  the edit into a temp file and **refuses to write** if the result would not
  load.

Everything ``add`` cannot safely automate (installing the agent, cloning each
repo, graphify, linger) it prints as explicit residue, and ``doctor`` then
verifies each one from live state.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from coord.commands._common import _CONFIG_OPTION, _load_config

# The same write-target rules `coord repo add` uses: the TRACKED config in the
# coord-settings checkout, never the `~/.coord/` symlink (#1779/#1832), and
# never onto a checkout that is behind its upstream (#2861).
from coord.commands.repo import _guard_settings_fresh, _resolve_write_target
from coord.fleet_config_health import TRACKED_CONFIG_REL, default_settings_dir


@click.group(
    "machine",
    help="Add a machine to the fleet, and verify it is actually onboarded.",
)
def machine_group() -> None:
    """Machine onboarding (#2915)."""


def _parse_csv(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _parse_repo_paths(
    repos: list[str], overrides: tuple[str, ...], default_tmpl: str
) -> dict[str, str]:
    """``{repo_name: path}`` for every ``--repos`` entry.

    ``--repo-path repo=/path`` overrides one repo; everything else gets
    ``default_tmpl`` with ``{repo}`` substituted. The KEYS are always the repo
    names passed in — never a path basename — which is the whole point (see
    the module docstring's incident 4).
    """
    paths = {repo: default_tmpl.format(repo=repo) for repo in repos}
    for override in overrides:
        if "=" not in override:
            raise click.ClickException(
                f"--repo-path must be REPO=PATH, got {override!r}"
            )
        repo, _, path = override.partition("=")
        repo, path = repo.strip(), path.strip()
        if repo not in paths:
            raise click.ClickException(
                f"--repo-path names {repo!r}, which is not in --repos {repos}"
            )
        paths[repo] = path
    return paths


def _validate_host(
    name: str, host: str, *, verify: bool, timeout: float
) -> list[str]:
    """Refuse a ``--host`` that does not resolve to this node's tailnet address.

    #2915 incident 2: ``host: dell64`` resolved to a LAN DHCP/DNS entry that
    shared the name with the tailnet node, so the board read ``[timeout]``
    while ``tailscale ping`` and the agent's own ``/health`` were both
    perfectly healthy — a symptom indistinguishable from a dead agent, a
    firewall, or a crashed unit. Writing that string into ``coordinator.yml``
    is what made it a config-level fact nobody rechecked.

    Reuses #2912's :func:`coord.network.check_host_resolution` rather than a
    second implementation. Returns advisory lines to print; **raises** only on
    a definite mismatch. An *unanswerable* comparison (no local ``tailscale``,
    the node is not in this box's peer list, the host does not resolve here at
    all) is deliberately not a refusal: "unknown" is not "wrong", and failing
    closed there would make it impossible to onboard a machine from a box that
    is not itself on the tailnet.
    """
    from coord import network  # noqa: PLC0415
    from coord.models import Machine  # noqa: PLC0415

    if not verify:
        return [
            "⚠ --no-verify-host: `host:` was NOT checked against the tailnet. "
            "A LAN DNS entry sharing this name silently shadows MagicDNS and "
            "the board then reads [timeout] on a perfectly healthy agent "
            "(#2912/#2915)."
        ]

    probe = Machine(name=name, host=host)
    result = network.check_host_resolution(probe, network.tailscale_ip_map(timeout=timeout))
    if result.matches is False:
        raise click.ClickException(
            f"refusing to write: {result.reason}.\n"
            f"  Fix: --host {result.magicdns_fqdn}  (the MagicDNS FQDN, which a "
            "LAN DNS entry cannot shadow)\n"
            "  Override (rarely right): --no-verify-host. Nothing was written."
        )
    if result.matches is True:
        lines = [f"✓ host {host} resolves to {result.resolved_ip} (this node's tailnet address)"]
        if result.magicdns_fqdn and host != result.magicdns_fqdn:
            lines.append(
                f"  note: the MagicDNS FQDN is {result.magicdns_fqdn} — preferring "
                "it over a bare hostname is what makes a future LAN DNS "
                "collision impossible rather than merely absent today."
            )
        return lines
    return [
        f"? host {host} could not be checked against the tailnet — "
        f"{result.reason}. Writing anyway (unknown is not wrong)."
    ]


@machine_group.command(
    "add",
    help=(
        "Write a new machine's coordinator.yml entry into the coord-settings "
        "checkout — validating that `host:` resolves to the tailnet address "
        "and that every --repos entry names a real repo — then print the "
        "residue it deliberately did NOT do."
    ),
)
@click.argument("name")
@click.option("--host", "host", required=True, help="Tailnet hostname (prefer the MagicDNS FQDN).")
@click.option(
    "--capabilities", "capabilities_csv", default=None,
    help="Comma-separated capabilities this machine's toolchain actually backs.",
)
@click.option(
    "--repos", "repos_csv", default=None,
    help=(
        "Comma-separated repo NAMES from the config's own `repos:` block — "
        "not directory names. A key that names no configured repo makes the "
        "WHOLE coordinator.yml fail to load (#2915)."
    ),
)
@click.option(
    "--repo-path", "repo_path_overrides", multiple=True, metavar="REPO=PATH",
    help="Override one repo's clone path. Repeatable. Default: ~/src/<repo>.",
)
@click.option(
    "--repo-path-template", "repo_path_tmpl", default="~/src/{repo}", show_default=True,
    help="Path template for repos with no --repo-path override. `{repo}` is substituted.",
)
@click.option(
    "--max-workers", type=int, default=None,
    help="Per-machine override of concurrency.max_workers (#1417). Omit for the fleet default.",
)
@click.option(
    "--verify-host/--no-verify-host", "verify_host", default=True, show_default=True,
    help="Check `host:` resolves to this node's tailnet address before writing (#2912).",
)
@click.option(
    "--skip-freshness-check", "skip_freshness", is_flag=True, default=False,
    help=(
        "Do NOT compare the config checkout to its upstream before writing. "
        "Rarely right: an entry written onto a stale base looks clean in the "
        "diff (#2861)."
    ),
)
@click.option(
    "--timeout", default=5.0, show_default=True, type=float,
    help="Timeout for the tailnet host check (seconds).",
)
@click.option(
    "--dry-run", is_flag=True, default=False,
    help="Print the edited config and the residue without writing anything.",
)
@click.option(
    "--config", "config_path", type=click.Path(path_type=Path), default=None,
    help="coordinator.yml to edit. Default: the coord-settings tracked file.",
)
def machine_add(  # noqa: PLR0913 — one option per thing the command can set
    name: str,
    host: str,
    capabilities_csv: str | None,
    repos_csv: str | None,
    repo_path_overrides: tuple[str, ...],
    repo_path_tmpl: str,
    max_workers: int | None,
    verify_host: bool,  # noqa: FBT001
    skip_freshness: bool,  # noqa: FBT001
    timeout: float,
    dry_run: bool,  # noqa: FBT001
    config_path: Path | None,
) -> None:
    from coord.config import load as load_config  # noqa: PLC0415
    from coord.repo_edit import (  # noqa: PLC0415
        RepoEditError,
        insert_machine_entry,
        render_machine_entry,
    )

    target = _resolve_write_target(config_path)
    # #2861: before ANY read of the file the edit is derived from, not just
    # before the write — the failure mode is deriving new content from a
    # stale base, and the resulting diff looks perfectly clean.
    _guard_settings_fresh(target, enabled=not skip_freshness)
    original = target.read_text(encoding="utf-8")

    try:
        cfg = load_config(target)
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(f"{target} does not currently load: {exc}") from exc

    if any(m.name == name for m in cfg.machines):
        raise click.ClickException(
            f"machine {name!r} already has a machines[] entry in {target} — use "
            f"`coord machine doctor {name}` to find what is actually missing."
        )

    # ── #2915 incident 4: validate repo names BEFORE writing ─────────────
    known_repos = [r.name for r in cfg.repos]
    repos = _parse_csv(repos_csv)
    unknown = [r for r in repos if r not in known_repos]
    if unknown:
        raise click.ClickException(
            f"unknown repo(s) {unknown} — coordinator.yml's `repos:` block has "
            f"{sorted(known_repos)}. These are repo NAMES, not checkout "
            "directory names; the two routinely differ. A `repo_paths` key "
            "that names no configured repo makes the ENTIRE coordinator.yml "
            "fail to load — for every machine, not just this one (#2915). "
            "Nothing was written."
        )
    repo_paths = _parse_repo_paths(repos, repo_path_overrides, repo_path_tmpl)

    for line in _validate_host(name, host, verify=verify_host, timeout=timeout):
        click.echo(line, err=line.startswith(("⚠", "?")))

    entry = render_machine_entry(
        name, host,
        capabilities=_parse_csv(capabilities_csv),
        repo_paths=repo_paths,
        max_workers=max_workers,
    )
    try:
        updated = insert_machine_entry(original, entry)
    except RepoEditError as exc:
        raise click.ClickException(str(exc)) from exc

    # ── Seatbelt: the edit must produce a config that LOADS and contains
    # what we think it contains. #2220's whole thesis, and the specific
    # protection against incident 4 — a bad machines[] entry takes the whole
    # fleet's config down, so this must never be written optimistically.
    import tempfile  # noqa: PLC0415

    with tempfile.NamedTemporaryFile(
        "w", suffix=".yml", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(updated)
        probe_path = Path(fh.name)
    try:
        new_cfg = load_config(probe_path)
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(
            f"refusing to write: the edited config does not parse ({exc}). "
            f"{target} is unchanged — which matters more here than anywhere "
            "else, since a bad machines[] entry stops the config loading for "
            "the WHOLE fleet (#2915)."
        ) from exc
    finally:
        probe_path.unlink(missing_ok=True)

    landed = next((m for m in new_cfg.machines if m.name == name), None)
    if landed is None:
        raise click.ClickException(
            f"refusing to write: the edit parsed but machine {name!r} is not in "
            f"the result. {target} is unchanged."
        )
    missing = [r for r in repos if r not in (landed.repos or [])]
    if missing:
        raise click.ClickException(
            f"refusing to write: the edit parsed but {name!r} does not list "
            f"repo(s) {missing}. {target} is unchanged."
        )
    unmapped = [r for r in repos if not landed.repo_path(r)]
    if unmapped:
        raise click.ClickException(
            f"refusing to write: the edit parsed but {name!r} has no repo_paths "
            f"entry for {unmapped} — dispatch would be refused for exactly "
            f"those repos (#1801). {target} is unchanged."
        )

    if dry_run:
        click.echo(f"--dry-run: would write {target}")
        click.echo(updated)
    else:
        target.write_text(updated, encoding="utf-8")
        click.echo(f"✓ wrote machines[{name}] to {target}")
        click.echo(f"  host: {host}")
        click.echo(f"  capabilities: {landed.capabilities or '[]'}")
        click.echo(f"  repos: {landed.repos or '[]'}")
        for repo in landed.repos or []:
            click.echo(f"    {repo}: {landed.repo_path(repo)}")

    _print_add_residue(target=target, name=name, host=host, repo_paths=repo_paths)


def _print_add_residue(
    *, target: Path, name: str, host: str, repo_paths: dict[str, str]
) -> None:
    """What ``coord machine add`` deliberately did NOT do.

    Same posture as ``coord repo add``'s residue block: the steps skipped here
    are the ones a wrong guess makes *worse* (installing a venv, cloning
    repos, enabling linger on someone else's box), and they are exactly what
    ``coord machine doctor`` then verifies from live state. Printing an
    honest residue beats pretending completeness — that pretence is how
    ``dell64`` looked onboarded while six things were silently broken.
    """
    click.echo("")
    click.echo("NOT DONE — these need a human, and `coord machine doctor` checks each:")
    tracked = default_settings_dir() / TRACKED_CONFIG_REL
    if target == tracked:
        click.echo(
            f"  1. commit + push in {default_settings_dir()}, then `git pull` on "
            "the daemon host — the fleet runs the COMMITTED config. Check the "
            "daemon's ~/.coord/coordinator.yml is still a SYMLINK into that "
            "checkout: if it has been replaced by a regular file, a correctly "
            "committed-and-pushed edit has no effect and nothing says so "
            "(#2915). `coord diagnose --config` reports this."
        )
    else:
        click.echo(
            f"  1. commit + push {target} wherever it is tracked, then `git "
            "pull` on the daemon host — the fleet runs the COMMITTED config."
        )
    click.echo(
        f"  2. install the agent on {host}: `install-agent.sh`. If a previous "
        "run failed partway, DELETE ~/.coord-venv first — a partial venv "
        "poisons every retry (#2915), and the venv must stay a plain, "
        "NON-editable PyPI install (#402/#2569)."
    )
    click.echo(
        f"  3. give {name} its own ~/.coord/coordinator.yml (a symlink into the "
        "coord-settings checkout) BEFORE starting the agent. An agent that "
        "comes up config-free publishes no capabilities at all, and every "
        "config-vs-/health cross-check then reads as absence rather than truth."
    )
    click.echo(
        f"  4. clone each repo onto {name} — these are the worker WORKTREE "
        "BASES, not convenience checkouts; without one, every dispatch of "
        "that repo here is refused while `coord status` stays green:"
    )
    for repo, path in (repo_paths or {"<each repo>": "~/src/<repo>"}).items():
        click.echo(f"       {repo}: {path}")
    click.echo(
        f"  5. `loginctl enable-linger \"$USER\"` on {host} — without it "
        "coord-agent dies at the next logout/reboot and never comes back, and "
        "nothing distinguishes that from a machine being switched off."
    )
    click.echo(
        "  6. install graphify and set `core.hooksPath .githooks` in each "
        "clone. Without the CLI every graph query on that machine degrades to "
        "grep SILENTLY (docs/GRAPHIFY_SETUP.md); `coord repo doctor <repo> "
        "--fix` does the machine-local half once the clones exist."
    )
    click.echo("")
    click.echo(f"Then: coord machine doctor {name} --ssh")


@machine_group.command(
    "doctor",
    help=(
        "Probe all onboarding layers for a machine and report per-layer "
        "status: config, network (does `host:` reach THIS machine?), agent "
        "(live /health), repo clones, graph, and runtime (the agent's venv "
        "and whether it survives logout). Reads LIVE state, not config. "
        "Exits non-zero on any CRIT so it can gate."
    ),
)
@click.argument("name")
@_CONFIG_OPTION
@click.option(
    "--timeout", default=3.0, show_default=True, type=float,
    help="Per-machine /health timeout (seconds).",
)
@click.option(
    "--ssh/--no-ssh", "probe_ssh", default=False, show_default=True,
    help=(
        "Also SSH in to check systemd linger — the one thing /health cannot "
        "see, because an agent answering a probe only proves the user manager "
        "is up right now."
    ),
)
@click.option(
    "--ssh-timeout", default=20.0, show_default=True, type=float,
    help="Timeout for the --ssh linger probe (seconds).",
)
@click.option(
    "--verbose", "-v", is_flag=True, default=False,
    help="Show passing checks too, not just the residue.",
)
def machine_doctor(  # noqa: PLR0913 — one option per thing the command can do
    name: str,
    config_path: Path,
    timeout: float,
    probe_ssh: bool,  # noqa: FBT001
    ssh_timeout: float,
    verbose: bool,  # noqa: FBT001
) -> None:
    from coord import machine_onboard, network  # noqa: PLC0415
    from coord.network import check_all  # noqa: PLC0415

    cfg = _load_config(config_path)

    if not any(m.name == name for m in cfg.machines):
        known = [m.name for m in cfg.machines]
        click.echo(
            f"error: machine {name!r} is not in coordinator.yml (have: {known})",
            err=True,
        )
        # Still render the report — `config.machine_missing` IS the finding,
        # and a caller gating on this deserves the same structured output.
        facts = machine_onboard.MachineFacts(name=name, configured=False)
        for line in machine_onboard.format_report(
            machine_onboard.evaluate(facts), verbose=verbose
        ):
            click.echo(line)
        sys.exit(1)

    # Probe the WHOLE fleet, not just this machine: `agent.version_skew` is
    # "vs the fleet", which needs the others' /health to have an answer at
    # all. It is the same sweep `coord status` does and costs the same.
    statuses = check_all(cfg.machines, timeout=timeout)
    facts = machine_onboard.gather_facts(
        cfg, name,
        statuses=statuses,
        ts_map=network.tailscale_ip_map(timeout=timeout) or {},
        probe_ssh=probe_ssh,
        ssh_timeout=ssh_timeout,
    )
    report = machine_onboard.evaluate(facts)
    for line in machine_onboard.format_report(report, verbose=verbose):
        click.echo(line)
    if not probe_ssh:
        click.echo("")
        click.echo(
            "  → systemd linger was NOT checked (/health cannot see it). "
            f"Re-run with --ssh, or `ssh {facts.host} 'loginctl show-user "
            "\"$USER\" --property=Linger'`."
        )
    if not report.ok:
        sys.exit(1)
