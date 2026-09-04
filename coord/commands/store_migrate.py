"""``coord migrate-to-postgres`` -- one-shot import of an existing SQLite
``coord.db`` into the Postgres store #827's ``store:`` config seam names
(#828 second half), plus #3086's rehearsal and content-verification modes.
See ``coord.store_migrate`` for the actual audit/copy/verify logic this
command is a thin CLI wrapper around.

Three modes, and the one sentence that keeps them apart:

- ``--dry-run`` audits the **source** and never opens the target -- cheap,
  and cannot tell you anything about the target.
- ``--verify`` runs the real import and then diffs the imported content
  against the source.
- ``--rehearse`` does all of that into a **throwaway** target it then drops,
  and reports the timing you size the outage with.
"""

from __future__ import annotations

from pathlib import Path

import click

from coord import db as coord_db
from coord import sql
from coord.store_migrate import ImportAborted, ImportReport, run_import, run_rehearsal

#: Printed on every rehearsal. Three facts about the *existing* system that a
#: rehearsal makes newly relevant, and that #3086 deliberately documents rather
#: than changes (all three were adjudicated by #828 / #827 / #1960).
REHEARSAL_RUNBOOK = (
    "Rehearse against deploy/coord-db-backup.sh's latest VACUUM INTO snapshot",
    "(/media/crucial/coord-backups/coord.db.latest), not the live coord.db:",
    "it exercises the restore path and cannot touch live data.",
    "import_table() commits PER TABLE, so a mid-import failure leaves a",
    "partially populated target that needs --force to retry. Harmless on a",
    "scratch target; know it before the real cutover run.",
    "To point a pytest run at the rehearsal database, use",
    "COORD_TEST_POSTGRES_DSN / tests/backends.py -- NEVER coordinator.yml's",
    "store.dsn: coord.db.refuse_postgres_under_pytest() raises whenever",
    "PYTEST_CURRENT_TEST is set, and that guard is correct (#1960/#827).",
)


@click.command(
    "migrate-to-postgres",
    help=(
        "Import an existing SQLite coord.db into the Postgres store configured "
        "by coordinator.yml's store: block (#828). Audits referential integrity "
        "and type-affinity drift before writing anything.\n"
        "\n"
        "Use --rehearse first (#3086): it imports into a throwaway scratch "
        "target through the ordinary import path, verifies the content, prints "
        "per-table and total elapsed time so you can size the cutover outage, "
        "and drops the scratch target again. Rehearse against the latest "
        "VACUUM INTO snapshot (deploy/coord-db-backup.sh writes "
        "/media/crucial/coord-backups/coord.db.latest) rather than the live "
        "database: that exercises the restore path too and cannot touch live "
        "data. Run it while the fleet is quiet -- `coord status` shows no "
        "active assignments -- so the timing number is representative."
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
    help=(
        "Cheap SOURCE-ONLY audit: report row counts and run the referential-"
        "integrity / type-affinity checks without opening or writing the "
        "target at all. It therefore proves nothing about the target -- for "
        "'will the target accept these rows', 'is the imported data the same "
        "data' and 'how long is the outage', use --rehearse."
    ),
)
@click.option(
    "--verify",
    is_flag=True,
    help=(
        "After importing, diff the imported CONTENT against the source (not "
        "just row counts) and print the parity report; exit non-zero if it is "
        "not empty. Implied by --rehearse. Ignored with --dry-run, which never "
        "opens the target."
    ),
)
@click.option(
    "--rehearse",
    is_flag=True,
    help=(
        "Rehearse the cutover: import into a throwaway scratch schema on --dsn "
        "through the ordinary import path, verify content, print per-table and "
        "total elapsed time, then drop the scratch schema. Kept (and named) "
        "instead of dropped when the rehearsal fails."
    ),
)
def migrate_to_postgres(
    source_path: Path | None,
    dsn: str | None,
    force: bool,
    dry_run: bool,
    verify: bool,
    rehearse: bool,
) -> None:
    if rehearse and dry_run:
        raise click.ClickException(
            "--rehearse and --dry-run are mutually exclusive: --dry-run never "
            "opens a target, which is exactly what a rehearsal exists to do."
        )
    if rehearse and force:
        raise click.ClickException(
            "--force is meaningless with --rehearse: the scratch target is "
            "created empty for this run, so there is never anything to wipe."
        )

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
    # policy this follows). sql.redact_dsn() prints host/dbname only, and it
    # is the ONLY way a DSN reaches stdout from this command -- including on
    # the rehearsal paths, where the scratch target's own name is built from
    # sql.redact_dsn() in coord.store_migrate.postgres_scratch_schema.
    target_line = sql.redact_dsn(dsn) if dsn else "(dry-run -- target is never opened)"
    click.echo(f"Target: {target_line}")
    if rehearse:
        click.echo("Mode:   rehearsal -- a throwaway scratch schema, dropped when it passes")
        for line in REHEARSAL_RUNBOOK:
            click.echo(f"        {line}")

    try:
        if rehearse:
            report = run_rehearsal(sqlite_path=source_path, dsn=dsn or "")
        else:
            report = run_import(
                sqlite_path=source_path,
                dsn=dsn or "",
                force=force,
                dry_run=dry_run,
                verify=verify and not dry_run,
            )
    except ImportAborted as exc:
        message = str(exc)
        if exc.retained_target:
            message += (
                f"\n\nScratch target RETAINED for inspection: {exc.retained_target}\n"
                "It was not dropped on purpose -- a failed rehearsal's partial "
                "target is the evidence. Drop it by hand when you are done "
                "(DROP SCHEMA ... CASCADE)."
            )
        raise click.ClickException(message) from exc

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

    _echo_import_table(report)
    _echo_timing(report)
    _echo_parity(report)

    if not report.row_counts_ok:
        raise click.ClickException(
            "row-count parity mismatch after import -- see table above; the target "
            "was written but does not match the source row-for-row"
            + _retained_suffix(report)
        )
    if report.content_ok is False:
        raise click.ClickException(
            "content parity FAILED -- see the report above. Row counts matched, so "
            "this is a difference row counting cannot see (type affinity, upsert "
            "semantics, identity resync). Classify every entry before cutting over."
            + _retained_suffix(report)
        )

    if report.scratch_target:
        click.echo(f"\nScratch target dropped: {report.scratch_target}")
        click.echo(
            "Rehearsal complete: the import path, the content check and the timing "
            "above are what the real cutover will do."
        )
    else:
        click.echo("\nImport complete: row-count parity confirmed for every table.")
    if report.content_ok:
        click.echo("Content parity confirmed: no differences between source and target.")


def _retained_suffix(report: ImportReport) -> str:
    if not report.retained_target:
        return ""
    return (
        f"\n\nScratch target RETAINED for inspection: {report.retained_target}\n"
        "It was not dropped on purpose -- a failed rehearsal's target is the "
        "evidence. Drop it by hand when you are done (DROP SCHEMA ... CASCADE)."
    )


def _echo_import_table(report: ImportReport) -> None:
    click.echo(f"{'table':<30} {'source':>10} {'target':>10} {'seconds':>10}  status")
    for t in report.tables:
        status = "OK" if t.ok else "MISMATCH"
        click.echo(
            f"{t.table:<30} {t.source_rows:>10} {t.target_rows:>10} "
            f"{t.elapsed_seconds:>10.2f}  {status}"
        )


def _echo_timing(report: ImportReport) -> None:
    """The rehearsal's primary deliverable: a number you can plan an outage
    around. Printed for every real import, not just rehearsals -- the cutover
    run's own timing is worth having in the log too."""
    total_rows = sum(t.source_rows for t in report.tables)
    click.echo(
        f"\nTotal: {len(report.tables)} tables, {total_rows} rows, "
        f"{report.elapsed_seconds:.2f}s elapsed."
    )
    click.echo(
        "That elapsed time is the quiet window to plan for. It is only "
        "representative if the fleet was quiet while it ran (`coord status` "
        "shows no active assignments)."
    )


def _echo_parity(report: ImportReport) -> None:
    if report.parity is None:
        click.echo(
            "\nContent NOT verified (row counts only). Pass --verify to diff the "
            "imported content against the source -- row counts cannot see type-"
            "affinity drift, upsert semantics or identity resync."
        )
        return
    click.echo()
    # ParityReport.render() prints table/row-key/column/both-values and never
    # touches a DSN, so this is safe to paste into an issue comment.
    click.echo(report.parity.render())
