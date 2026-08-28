"""``coord migrate-to-postgres`` -- one-shot import of an existing SQLite
``coord.db`` into the Postgres store #827's ``store:`` config seam names
(#828 second half). See ``coord.store_migrate`` for the actual audit/copy
logic this command is a thin CLI wrapper around.
"""

from __future__ import annotations

from pathlib import Path

import click

from coord import db as coord_db
from coord import sql
from coord.store_migrate import ImportAborted, run_import


@click.command(
    "migrate-to-postgres",
    help=(
        "Import an existing SQLite coord.db into the Postgres store configured "
        "by coordinator.yml's store: block (#828). Audits referential integrity "
        "and type-affinity drift before writing anything."
    ),
)
@click.option(
    "--source",
    "source_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to the source coord.db (default: this install's DB_PATH).",
)
@click.option(
    "--dsn",
    default=None,
    help="Target Postgres DSN (default: coordinator.yml's store.dsn).",
)
@click.option(
    "--force",
    is_flag=True,
    help="Wipe and re-import any target tables that already have rows (default: refuse).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Audit the source and report row counts without opening or writing the target.",
)
def migrate_to_postgres(
    source_path: Path | None, dsn: str | None, force: bool, dry_run: bool
) -> None:
    source_path = source_path or coord_db.DB_PATH
    if dsn is None and not dry_run:
        target = coord_db._resolve_store_target()
        if not target.dsn:
            raise click.ClickException(
                "no --dsn given and coordinator.yml has no store.dsn configured -- "
                "pass --dsn explicitly, or set store.backend: postgres / store.dsn "
                "in coordinator.yml first (#827)."
            )
        dsn = target.dsn

    click.echo(f"Source: {source_path}")
    # Never echo the raw DSN -- it routinely embeds a password
    # (`store.dsn`'s plain-string shape, coord/config.py), and this command's
    # stdout is exactly the kind of text that can end up in a log or a
    # GitHub comment (coord.db._migrate_if_needed's docstring states the
    # policy this follows). sql.redact_dsn() prints host/dbname only.
    target_line = sql.redact_dsn(dsn) if dsn else "(dry-run -- target is never opened)"
    click.echo(f"Target: {target_line}")

    try:
        report = run_import(sqlite_path=source_path, dsn=dsn or "", force=force, dry_run=dry_run)
    except ImportAborted as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo()
    if report.dry_run:
        # The target is never opened for a dry run, so TableReport.target_rows
        # is a placeholder 0 -- printing a source/target/status table here
        # would read every table as a false "MISMATCH". Report source counts
        # only; the audits (referential integrity, type affinity) already ran
        # and would have raised ImportAborted above if either found anything.
        click.echo(f"{'table':<30} {'source rows':>12}")
        for t in report.tables:
            click.echo(f"{t.table:<30} {t.source_rows:>12}")
        click.echo(
            "\nAudits passed (referential integrity, type affinity). "
            "Dry run -- no target opened, no rows written."
        )
        return

    click.echo(f"{'table':<30} {'source':>10} {'target':>10}  status")
    all_ok = True
    for t in report.tables:
        status = "OK" if t.ok else "MISMATCH"
        if not t.ok:
            all_ok = False
        click.echo(f"{t.table:<30} {t.source_rows:>10} {t.target_rows:>10}  {status}")

    if not all_ok:
        raise click.ClickException(
            "row-count parity mismatch after import -- see table above; the target "
            "was written but does not match the source row-for-row"
        )
    click.echo("\nImport complete: row-count parity confirmed for every table.")
