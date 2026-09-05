"""`coord backup` — off-site, verified store backup (#3118, closes #1822).

Thin CLI over :mod:`coord.backup`; all of the policy (backend resolution,
verify-before-it-counts, retention's never-leave-zero floor) lives there so
the systemd timer and a human at a terminal cannot take different paths.
"""

from __future__ import annotations

import click

from coord.backup import (
    BackupConfig,
    BackupError,
    format_bytes,
    init_repository,
    list_snapshots,
    push as _push,
    restore as _restore,
    summarize_snapshots,
)


def _fail(exc: BackupError) -> None:
    click.echo(f"backup: {exc}", err=True)
    raise SystemExit(1)


@click.group(
    "backup",
    help=(
        "Off-site, verified backup of the coordinator store to a restic "
        "repository (Azure Blob). Credentials come from the environment or a "
        "managed identity only -- never from coordinator.yml, never from argv."
    ),
)
def backup_group() -> None:
    pass


@backup_group.command("push", help="Snapshot, verify, upload, then prune by policy.")
@click.option(
    "--no-prune",
    is_flag=True,
    help="Upload but skip retention. Useful when catching up after an outage.",
)
def backup_push(no_prune: bool) -> None:
    try:
        result = _push(do_prune=not no_prune)
    except BackupError as exc:
        _fail(exc)
        return
    click.echo(
        f"backup: ok {result.snapshot_id} backend={result.backend} "
        f"snapshot={format_bytes(result.snapshot_bytes)} "
        f"transferred={format_bytes(result.transferred_bytes)}"
        + (f" assignments={result.rows}" if result.rows is not None else "")
    )
    if result.prune_skipped_reason:
        click.echo(f"backup: retention skipped: {result.prune_skipped_reason}")


@backup_group.command("list", help="List the snapshots this lane owns, newest first.")
def backup_list() -> None:
    try:
        snapshots = list_snapshots(BackupConfig.from_env())
    except BackupError as exc:
        _fail(exc)
        return
    if not snapshots:
        click.echo("backup: no snapshots in the repository")
        return
    for line in summarize_snapshots(snapshots):
        click.echo(line)


@backup_group.command(
    "restore",
    help=(
        "Restore a snapshot to --into. Refuses to overwrite the live database, "
        "or any existing file, without --force."
    ),
)
@click.argument("snapshot_id")
@click.option("--into", required=True, type=click.Path(), help="Destination path.")
@click.option(
    "--force",
    is_flag=True,
    help="Allow overwriting an existing file, including the LIVE database.",
)
def backup_restore(snapshot_id: str, into: str, force: bool) -> None:
    from pathlib import Path  # noqa: PLC0415

    try:
        dest = _restore(snapshot_id, Path(into), force=force)
    except BackupError as exc:
        _fail(exc)
        return
    click.echo(f"backup: restored {snapshot_id} -> {dest}")


@backup_group.command(
    "init", help="Initialise the restic repository (one-time, operator action)."
)
def backup_init() -> None:
    try:
        config = BackupConfig.from_env()
        init_repository(config)
    except BackupError as exc:
        _fail(exc)
        return
    click.echo(f"backup: initialised {config.repository}")
