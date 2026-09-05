"""`coord dr` — disaster-recovery verification (#3119, rung D2 of #3117).

Thin CLI over :mod:`coord.dr_verify`; all of the policy (which checks run, in
what order, what counts as a failure, what gets persisted) lives there so the
systemd timer and a human at a terminal cannot take different paths — the same
split ``coord backup`` uses over :mod:`coord.backup`.

Three commands, deliberately separate:

* ``coord dr verify`` — restore the latest off-site snapshot into a scratch
  location and prove it is usable. Runs on the daemon host, on a timer.
* ``coord dr status`` — read ``last_verify.json`` and report whether the
  *last* verify is recent enough to be believed. Needs no restore, no
  credentials and no daemon, so it is the command **another machine** runs
  against a mirrored record when the daemon host is the thing that died. A
  verify that stopped running is a failure, not an absence of news.
* ``coord dr promote`` — the recovery itself (#3129, rung D3): restore onto a
  tailnet standby and bring it up as the board. Same split as above: all of
  the policy lives in :mod:`coord.dr_promote`, so the refusal an operator sees
  at a terminal and the one a future automation would hit are the same code.
"""

from __future__ import annotations

from pathlib import Path

import click

from coord import dr_promote, dr_verify


@click.group(
    "dr",
    help=(
        "Disaster recovery: prove the off-site backup restores (#3119), and "
        "restore onto a standby when the daemon host is gone (#3129). "
        "`verify` does a scratch restore; `status` reports whether the last "
        "one is recent enough to be believed; `promote` brings a standby up "
        "as the board."
    ),
)
def dr_group() -> None:
    pass


@dr_group.command(
    "verify",
    help=(
        "Restore the latest off-site snapshot into a scratch location and "
        "prove it is usable: structure, row counts vs live, schema version, "
        "and a real `coord serve` answering GET /board. Quiet on success; "
        "alerts through the notifier on failure."
    ),
)
@click.option(
    "--snapshot",
    "snapshot_id",
    default=None,
    help="Verify this snapshot instead of the newest one. Useful for testing "
    "the failure path against a deliberately corrupted snapshot.",
)
@click.option(
    "--scratch",
    "scratch_root",
    default=None,
    type=click.Path(),
    help="Parent directory for the scratch restore (default: the system temp "
    "dir). The scratch copy is removed on every exit path either way.",
)
@click.option(
    "--no-parity",
    is_flag=True,
    help="Skip booting a throwaway `coord serve` against the scratch DB. "
    "Faster, and strictly weaker: parity is the step that proves "
    "recoverability rather than restorability.",
)
@click.option(
    "--no-alert",
    is_flag=True,
    help="Do not push a failure to the notifier (still exits non-zero).",
)
@click.option(
    "--record",
    "record_path",
    default=None,
    type=click.Path(),
    help="Where to write last_verify.json (default: ~/.coord/last_verify.json).",
)
def dr_verify_cmd(
    snapshot_id: str | None,
    scratch_root: str | None,
    no_parity: bool,
    no_alert: bool,
    record_path: str | None,
) -> None:
    report = dr_verify.verify(
        snapshot_id=snapshot_id,
        scratch_root=Path(scratch_root) if scratch_root else None,
        parity=not no_parity,
    )
    path = dr_verify.write_record(
        report, path=Path(record_path) if record_path else None
    )
    if report.ok:
        # Success is quiet — one line, no alert. The number worth reading is
        # the restore duration: it is #3117's Domain-A RTO input.
        click.echo(report.summary())
        return
    click.echo(report.summary(), err=True)
    click.echo(f"dr verify: record written to {path}", err=True)
    if not no_alert:
        dr_verify.alert(
            "DR verify FAILED",
            f"{report.failure}\n\nsnapshot={report.snapshot_id}\n"
            f"Nothing is proving the off-site backup restores. "
            f"See {path}.",
        )
    raise SystemExit(1)


@dr_group.command(
    "status",
    help=(
        "Report whether the last DR verify is recent enough to be believed. "
        "Exits non-zero when the record is missing, stale, or records a "
        "failure -- a verify that stopped running is itself a failure. Reads "
        "a mirrored record with --record, so another machine can run this "
        "when the daemon host is the thing that died."
    ),
)
@click.option(
    "--record",
    "record_path",
    default=None,
    type=click.Path(),
    help="last_verify.json to read (default: ~/.coord/last_verify.json).",
)
@click.option(
    "--max-age",
    "max_age_hours",
    default=None,
    type=float,
    help=f"Staleness window in hours (default: ${dr_verify.STALENESS_ENV} or "
    f"{dr_verify.DEFAULT_STALENESS_HOURS:g}).",
)
@click.option(
    "--no-alert",
    is_flag=True,
    help="Do not push a stale/failed verdict to the notifier.",
)
def dr_status_cmd(
    record_path: str | None, max_age_hours: float | None, no_alert: bool
) -> None:
    try:
        verdict = dr_verify.evaluate_staleness(
            path=Path(record_path) if record_path else None,
            max_age_hours=max_age_hours,
        )
    except dr_verify.DRVerifyError as exc:
        click.echo(f"dr status: {exc}", err=True)
        raise SystemExit(1) from None
    if verdict.ok:
        click.echo(f"dr status: ok — {verdict.reason}")
        return
    click.echo(f"dr status: STALE — {verdict.reason}", err=True)
    if not no_alert:
        dr_verify.alert("DR verify is not running", verdict.reason)
    raise SystemExit(1)


@dr_group.command(
    "promote",
    help=(
        "Restore the latest off-site backup onto THIS host and bring it up as "
        "the board (#3129). Operator-initiated only: it refuses while the "
        "incumbent board still answers /healthz, while the coord-settings "
        "checkout is absent/dirty/behind, and while any credential the daemon "
        "needs to be useful (not merely to boot) is missing. Run --dry-run "
        "first: it does everything except mutate, and is the mode to rehearse "
        "with."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Probe, resolve, check credentials, enumerate units and print the "
    "ordered plan — mutating nothing. Exits zero even when it reports the "
    "incumbent is alive: a rehearsal that says 'dellserver is still serving' "
    "has succeeded at rehearsing.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Waive the two refusals that name it: a live incumbent, and a "
    "non-empty local store. Never waives a missing credential or a stale "
    "coord-settings checkout — forcing past those is how you get a board that "
    "looks recovered and cannot merge.",
)
@click.option(
    "--board-url",
    default=None,
    help="The incumbent board to probe (default: $COORD_SERVICE_URL, then "
    "~/.coord/client.toml's board_service).",
)
@click.option(
    "--snapshot",
    "snapshot_id",
    default=None,
    help="Restore this snapshot instead of the newest one.",
)
@click.option(
    "--no-network",
    is_flag=True,
    help="Skip the credential probes that need the network. Strictly weaker: "
    "an unprobed credential reports `unknown`, which still blocks a real run.",
)
@click.option(
    "--local-board-url",
    default=None,
    help="Where the PROMOTED daemon will be reachable for the final "
    "verification (default: http://127.0.0.1:7435). Distinct from "
    "--board-url, which names the incumbent this command refuses to race.",
)
@click.option(
    "--verify-timeout",
    default=None,
    type=float,
    help="Seconds to wait for the promoted daemon to serve GET /board "
    "(default: 90).",
)
@click.option(
    "--record",
    "record_path",
    default=None,
    type=click.Path(),
    help="Write a JSON record of the run (including the measured elapsed "
    "time) to this path.",
)
def dr_promote_cmd(
    dry_run: bool,
    force: bool,
    board_url: str | None,
    snapshot_id: str | None,
    no_network: bool,
    local_board_url: str | None,
    verify_timeout: float | None,
    record_path: str | None,
) -> None:
    report = dr_promote.promote(
        dry_run=dry_run,
        force=force,
        board_url=board_url,
        network=not no_network,
        snapshot_id=snapshot_id,
        local_board_url=local_board_url,
        **({"verify_timeout": verify_timeout} if verify_timeout else {}),
    )
    for line in dr_promote.render_plan(report.plan, dry_run=dry_run):
        click.echo(line)
    if record_path:
        click.echo(f"record written to {dr_promote.write_record(report, Path(record_path))}")
    if dry_run:
        return
    click.echo("")
    for line in dr_promote.render_report(report):
        click.echo(line, err=not report.ok)
    if not report.ok:
        raise SystemExit(1)
