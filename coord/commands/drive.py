"""``coord drive`` — the unattended single-issue driver (#1392).

Thin Click wrapper: parse flags into :class:`~coord.drive.DriveOptions`, build
the :class:`~coord.drive.Driver`, translate :class:`~coord.drive.DriveError`
into a clean exit code.  Every decision lives in ``coord/drive.py``.

Replaces ``scripts/drive-issue.sh`` + ``scripts/coord_issue_state.py``, deleted
in the same change (no two implementations).  ``scripts/coord-test-runner.sh``
deliberately stays as shell — it is genuinely subprocess/venv/cargo-env work,
and its four sharp-edged parsers already live in ``coord/test_report.py``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import click

from coord.commands._common import _CONFIG_OPTION, _load_config


_DRIVE_HELP = """Drive ONE issue Work → Test → Review → Merge, unattended.

A resumable state machine over the daemon's board: it dispatches the WORK
assignment, then OBSERVES Test/Review/Merge (coord dispatches all three
itself), looping a failing test through `coord fix` on the same branch.
Re-running it on the same issue is safe and resumes from wherever the board
actually is.

#1453: when this issue resolves to a milestone with a merged Gate-A contract
(`tests/acceptance/ms-NN/contract.md`) and the repo has an acceptance driver
configured, the sealed JIT acceptance slice is authored first (`coord
acceptance author <repo> <tracking_issue> --issue <N>`) and observed through
to a landed merge — ONLY THEN is `coord assign`/`coord approve-plan` run.
Pass --no-acceptance to skip this and drive the issue exactly as before.
The preflight banner's "acceptance" line always states which mode a run is
in and why.

Exit codes: 0 merged (verified against the remote default branch), or review
approved with --no-merge; 1 a stage reached a terminal failure a script cannot
resolve; 2 bad usage / configuration; 3 deadline exceeded.
"""


def _rebuild_drive_argv(
    repo: str,
    issue: int,
    *,
    machine: str,
    model: str,
    briefing_file: str,
    do_plan: bool,
    max_fix_rounds: int,
    skip_test: bool,
    repo_path: str,
    poll: float,
    max_work_retries: int,
    deadline_mins: float,
    stall_mins: float,
    notify: bool,
    urgent: bool,
    accept_advisory: bool,
    force_review: bool,
    no_merge: bool,
    merge_method: str,
    max_merge_attempts: int,
    dry_run: bool,
    config_path: Path,
) -> list[str]:
    """Rebuild a ``drive <repo> <issue> ...`` argv from parsed flags (`--tmux`).

    The `--tmux` launch re-execs `coord drive` (minus `--tmux`) detached
    inside a fresh tmux session, so every OTHER flag the operator passed
    must survive the trip. Deliberately always emits every option (rather
    than only the ones that differ from Click's own default) so this stays
    correct even if a default ever changes — no drift between two places
    that both need to know it.
    """
    argv = ["drive", repo, str(issue)]
    if machine:
        argv += ["--machine", machine]
    if model:
        argv += ["--model", model]
    if briefing_file:
        argv += ["--briefing-file", briefing_file]
    if do_plan:
        argv.append("--plan")
    argv += ["--max-fix-rounds", str(max_fix_rounds)]
    if skip_test:
        argv.append("--skip-test")
    if repo_path:
        argv += ["--repo-path", repo_path]
    argv += ["--poll", str(poll)]
    argv += ["--max-work-retries", str(max_work_retries)]
    argv += ["--deadline", str(deadline_mins)]
    argv += ["--stall", str(stall_mins)]
    if notify:
        argv.append("--notify")
    if urgent:
        argv.append("--urgent")
    if accept_advisory:
        argv.append("--accept-advisory")
    if force_review:
        argv.append("--force-review")
    if no_merge:
        argv.append("--no-merge")
    argv += ["--merge-method", merge_method]
    argv += ["--max-merge-attempts", str(max_merge_attempts)]
    if dry_run:
        argv.append("--dry-run")
    if config_path:
        argv += ["--config", str(config_path)]
    return argv


@click.command("drive", help=_DRIVE_HELP)
@click.argument("repo")
@click.argument("issue", type=int)
@click.option(
    "--machine",
    default="",
    help=(
        "Machine for the work dispatch (default: least-loaded unpaused machine "
        "that hosts the repo)."
    ),
)
@click.option(
    "--model",
    default="",
    help="Model tier (haiku|sonnet|opus). Default: models.default.",
)
@click.option(
    "--briefing-file",
    default="",
    type=click.Path(),
    help=(
        "REPLACES the entire auto-generated briefing for the work dispatch — it "
        "is NOT an addendum. The issue body, project rules and file scope are "
        "all dropped, so the worker only ever sees this file. To ADD guidance "
        "while keeping the real briefing, use `coord context add --pin <repo> "
        "<issue> '<note>'`, which prepends to every briefing (#603)."
    ),
)
@click.option(
    "--plan",
    "do_plan",
    is_flag=True,
    help=(
        "Run a read-only plan stage first and auto-approve it "
        "(coord assign --plan-only → coord approve-plan)."
    ),
)
@click.option(
    "--max-fix-rounds",
    default=3,
    show_default=True,
    help=(
        "Headless `coord fix` rounds this run is willing to spend, shared by "
        "BOTH fix arms: a failing test suite and a request-changes review "
        "(#1692). Each round continues the SAME branch with the model "
        "escalated (sonnet → opus). coord dispatches the Test stage itself "
        "onto a capability-matched machine (#1426); this only observes the "
        "verdict. Distinct from pipeline.max_review_iterations, which bounds "
        "the review loop per ISSUE across every drive."
    ),
)
@click.option(
    "--skip-test",
    is_flag=True,
    help=(
        "Record the Test gate as `skipped` directly (via `coord test --skipped`) "
        "— no dispatch. Use only for genuinely untestable diffs."
    ),
)
@click.option(
    "--repo-path",
    default="",
    type=click.Path(),
    help=(
        "Local checkout used for branch/merge verification (git fetch + "
        "cherry against origin). Default: ~/src/<repo>."
    ),
)
@click.option("--poll", default=60.0, show_default=True, help="Board poll interval (s).")
@click.option(
    "--max-work-retries",
    default=1,
    show_default=True,
    help="`coord retry` attempts on a failed work stage.",
)
@click.option(
    "--deadline",
    "deadline_mins",
    default=240.0,
    show_default=True,
    help="Give up after this long (minutes).",
)
@click.option(
    "--stall",
    "stall_mins",
    default=20.0,
    show_default=True,
    help=(
        "Warn (and nudge, with --notify) after this long with no state "
        "change, repeating on the same cadence for as long as the stall "
        "continues."
    ),
)
@click.option(
    "--notify",
    is_flag=True,
    help=(
        "Also run `coord notify` under ~/.coord/notify.lock when stalled, to cut "
        "the 5-minute timer latency. OFF by default: two drivers racing to "
        "dispatch the same thing is the #476/#477 duplicate-fix-worker incident."
    ),
)
@click.option(
    "--urgent",
    is_flag=True,
    help=(
        "Opt THIS drive out of the notifier's quiet hours (#1632), so a "
        "'nobody is coming' push for it arrives at 02:00 instead of waiting "
        "for the 08:00 digest. The exception to quiet hours is a deadline, "
        "not a severity: you know when something is time-critical and the "
        "system does not. Scoped to this issue and expires on its own "
        "(notifications.urgent_ttl_hours)."
    ),
)
@click.option(
    "--accept-advisory",
    is_flag=True,
    help=(
        "Proceed when the work row is ADVISORY but its branch demonstrably "
        "carries commits. Needed while #1357 is open."
    ),
)
@click.option(
    "--force-review",
    is_flag=True,
    help=(
        "Explicitly request the review for work completed in an INTERACTIVE "
        "session. coord's #555 guard never auto-dispatches one for "
        "provider=claude-pty, so without this the run stops at preflight "
        "rather than stalling forever."
    ),
)
@click.option("--no-merge", is_flag=True, help="Stop after the review approves.")
@click.option(
    "--merge-method",
    type=click.Choice(["rebase", "squash", "merge"]),
    default="rebase",
    show_default=True,
)
@click.option(
    "--max-merge-attempts",
    default=3,
    show_default=True,
    help="`coord merge` attempts before giving up (bounds a merge the board never reflects).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the resolved plan and current state, then exit.",
)
@click.option(
    "--tmux",
    "use_tmux",
    is_flag=True,
    help=(
        "Launch the drive loop DETACHED in a `coord-drive-<repo>-<issue>` tmux "
        "session instead of running inline (#1398). Waits a few seconds to "
        "confirm the session is still alive and has started writing its run "
        "log (#1606) before exiting 0 — a launch that dies immediately (e.g. "
        "nothing left to do) exits non-zero with the reason instead of "
        "printing a false success banner. Once confirmed live, this "
        "invocation exits immediately — the run survives this terminal "
        "closing, a TUI restart, or an ssh drop. Reattach with `coord "
        "drive-attach <repo> <issue>`; list live runs with `coord "
        "drive-sessions`; stop with `coord drive-stop <repo> <issue>` — killing "
        "the session releases the per-issue flock, which IS the correct Stop."
    ),
)
@click.option(
    "--no-acceptance",
    is_flag=True,
    help=(
        "Skip the #1453 oracle-loop JIT slice authoring step even when this "
        "issue's milestone has a merged Gate-A contract — use when the "
        "contract is stale/wrong for this issue, or you just want a plain "
        "run. Dispatches straight to `coord assign` as before #1453."
    ),
)
@_CONFIG_OPTION
def drive(
    repo: str,
    issue: int,
    machine: str,
    model: str,
    briefing_file: str,
    do_plan: bool,
    max_fix_rounds: int,
    skip_test: bool,
    repo_path: str,
    poll: float,
    max_work_retries: int,
    deadline_mins: float,
    stall_mins: float,
    notify: bool,
    urgent: bool,
    accept_advisory: bool,
    force_review: bool,
    no_merge: bool,
    merge_method: str,
    max_merge_attempts: int,
    dry_run: bool,
    use_tmux: bool,
    no_acceptance: bool,
    config_path: Path,
) -> None:
    # Imported lazily so `coord --help` doesn't pay for httpx/subprocess setup.
    from coord.drive import (  # noqa: PLC0415
        Driver,
        DriveError,
        DriveOptions,
        coord_argv,
        launch_drive_in_tmux,
    )

    if use_tmux:
        argv = coord_argv() + _rebuild_drive_argv(
            repo,
            issue,
            machine=machine,
            model=model,
            briefing_file=briefing_file,
            do_plan=do_plan,
            max_fix_rounds=max_fix_rounds,
            skip_test=skip_test,
            repo_path=repo_path,
            poll=poll,
            max_work_retries=max_work_retries,
            deadline_mins=deadline_mins,
            stall_mins=stall_mins,
            notify=notify,
            urgent=urgent,
            accept_advisory=accept_advisory,
            force_review=force_review,
            no_merge=no_merge,
            merge_method=merge_method,
            max_merge_attempts=max_merge_attempts,
            dry_run=dry_run,
            config_path=config_path,
        )
        try:
            session = launch_drive_in_tmux(argv, repo=repo, issue=issue)
        except DriveError as exc:
            click.echo(f"✗ {exc}", err=True)
            raise SystemExit(exc.exit_code) from None
        click.echo(f"driving {repo} #{issue} in tmux session {session!r}")
        click.echo(f"  attach with: coord drive-attach {repo} {issue}")
        click.echo(f"  stop with:   coord drive-stop {repo} {issue}")
        raise SystemExit(0)

    config = _load_config(config_path)
    opts = DriveOptions(
        machine=machine,
        model=model,
        briefing_file=briefing_file,
        do_plan=do_plan,
        max_fix_rounds=max_fix_rounds,
        skip_test=skip_test,
        repo_path=repo_path,
        poll=poll,
        max_work_retries=max_work_retries,
        deadline_mins=deadline_mins,
        stall_mins=stall_mins,
        notify=notify,
        do_merge=not no_merge,
        merge_method=merge_method,
        accept_advisory=accept_advisory,
        force_review=force_review,
        dry_run=dry_run,
        max_merge_attempts=max_merge_attempts,
        no_acceptance=no_acceptance,
        config_path=str(config_path) if config_path else "",
    )
    # #1632: register the quiet-hours opt-out BEFORE the first tick, so a
    # drive that dies in its own preflight still gets its failure pushed
    # rather than held until 08:00. Scoped to this issue, carries its own
    # expiry, and is cleared below when the drive ends — a forgotten flag
    # must not be able to make every future night loud. Advisory: a
    # notifier that cannot record this never affects the drive.
    if urgent:
        _set_drive_urgency(repo, issue, config, on=True)

    driver = Driver(repo=repo, issue=issue, opts=opts, config=config)
    try:
        code = driver.run()
    except DriveError as exc:
        click.echo(f"✗ {exc}", err=True)
        raise SystemExit(exc.exit_code) from None
    except KeyboardInterrupt:
        click.echo("interrupted", err=True)
        raise SystemExit(130) from None
    finally:
        if urgent:
            _set_drive_urgency(repo, issue, config, on=False)
    raise SystemExit(code)


def _set_drive_urgency(repo: str, issue: int, config, *, on: bool) -> None:
    """Add/remove this drive's #1632 quiet-hours opt-out, never raising."""
    try:
        import time as _time  # noqa: PLC0415

        from coord.notifier import store as _notifier_store  # noqa: PLC0415

        if on:
            ttl_hours = float(
                getattr(getattr(config, "notifications", None), "urgent_ttl_hours", 12.0)
                or 12.0
            )
            _notifier_store.mark_urgent(
                repo, issue, expires_at=_time.time() + ttl_hours * 3600.0
            )
        else:
            _notifier_store.clear_urgent(repo, issue)
    except Exception:  # noqa: BLE001 — advisory channel, never breaks a drive
        pass


# ── `--tmux` companions: list / attach / stop (#1398) ─────────────────────────
#
# Three flat, hyphenated commands rather than a `drive` sub-group — `drive`
# itself already spends its position on `REPO ISSUE`, so a group would need
# a distinct verb anyway. Mirrors the naming of other hyphenated one-off
# commands in this CLI (`verify-merge`, `fix-briefing`, `approve-plan`).


@click.command(
    "drive-sessions",
    help="List live `coord drive --tmux` sessions (coord-drive-<repo>-<issue>).",
)
@click.option(
    "--json", "output_json", is_flag=True, default=False,
    help="Output as JSON — an array of {repo, issue, session_name, attached}. "
         "Consumed by coord-tui to badge Pipeline rows and gate the menu.",
)
def drive_sessions(output_json: bool) -> None:
    import json as _json  # noqa: PLC0415

    from coord.drive import list_drive_sessions  # noqa: PLC0415

    entries = list_drive_sessions()
    if output_json:
        click.echo(_json.dumps(entries))
        return
    if not entries:
        click.echo("No live drive sessions.")
        return
    for e in entries:
        attached_tag = " [attached]" if e.get("attached") else ""
        click.echo(f"  {e['repo']} #{e['issue']}{attached_tag}")
        click.echo(f"    attach with: coord drive-attach {e['repo']} {e['issue']}")
        click.echo(f"    stop with:   coord drive-stop {e['repo']} {e['issue']}")


@click.command(
    "drive-attach",
    help=(
        "Attach to a live `coord drive --tmux` session for REPO ISSUE. "
        "Type this into a local PTY exactly as `coord reattach`/`coord "
        "terminal attach` are used for other session kinds."
    ),
)
@click.argument("repo")
@click.argument("issue", type=int)
def drive_attach(repo: str, issue: int) -> None:
    from coord.drive import drive_session_name  # noqa: PLC0415
    from coord.interactive import TmuxHost, tmux_session_alive  # noqa: PLC0415

    host = TmuxHost(None)
    session = drive_session_name(repo, issue)
    if not tmux_session_alive(session, host=host):
        click.echo(f"error: no live drive session for {repo} #{issue}.", err=True)
        sys.exit(1)

    if os.environ.get("TMUX"):
        # Already inside a tmux client — attach-session refuses to nest;
        # switch-client moves the current client into the target session.
        cmd = ["tmux", "switch-client", "-t", session]
    else:
        cmd = list(host.cmd(["attach-session", "-t", session], tty=True))

    try:
        result = subprocess.run(cmd)
    except (subprocess.SubprocessError, OSError) as exc:
        click.echo(f"error: failed to attach: {exc}", err=True)
        sys.exit(1)
    sys.exit(result.returncode)


@click.command(
    "drive-stop",
    help=(
        "Stop a live `coord drive --tmux` run for REPO ISSUE by killing its "
        "tmux session. This IS the correct cancellation: the per-issue flock "
        "releases the instant the process exits, so the driver's own "
        "already-driving guard never sees a stale lock."
    ),
)
@click.argument("repo")
@click.argument("issue", type=int)
def drive_stop(repo: str, issue: int) -> None:
    from coord.drive import drive_session_name  # noqa: PLC0415
    from coord.interactive import TmuxHost, tmux_session_alive  # noqa: PLC0415

    host = TmuxHost(None)
    session = drive_session_name(repo, issue)
    if not tmux_session_alive(session, host=host):
        click.echo(f"error: no live drive session for {repo} #{issue}.", err=True)
        sys.exit(1)

    try:
        result = subprocess.run(
            host.cmd(["kill-session", "-t", session]),
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
    click.echo(f"Stopped driving {repo} #{issue}.")


# ── driver escalation records (#1505) ────────────────────────────────────────
#
# `_decide_merge` writes one of these the moment the merge stage hits a
# status no amount of retrying can fix (NEEDS_ATTENTION / an unrecognised
# status) — see that function's docstring. `record` is the driver's own
# write (`Driver.run` executes it as the last thing before exiting, via
# `run_coord`, never as a direct DB call — same CLI-is-the-contract rule
# every other board mutation in this module follows). `run`/`dismiss`/`list`
# are the human's one-key responses, surfaced in coord-tui's Pipeline
# right-click menu (`run-escalation`/`dismiss-escalation`) and reachable
# from any shell.


@click.group("escalate")
def escalate_group() -> None:
    """Board-visible "driver stuck" records the merge stage of `coord drive`
    writes instead of burning the retry budget on a status retrying can't
    fix (#1505). `record` is written by the driver itself; the human answers
    with `run` (execute the proposed fix), `dismiss` (clear the record), or
    `list` (see what's outstanding).
    """


@escalate_group.command("record")
@click.argument("repo")
@click.argument("issue", type=int)
@click.option("--stage", default="merge", show_default=True, help="Pipeline stage that escalated.")
@click.option("--reason", required=True, help="Why the driver stopped.")
@click.option(
    "--gate", "gates", multiple=True,
    help="One observed gate reading as key=value. Repeatable.",
)
@click.option("--command", "proposed_command", required=True, help="The proposed fix, as a shell command.")
@click.option("--assignment", "assignment_id", default=None, help="The merge-queue/work assignment id, if known.")
@click.option(
    "--annotate-test-reason", "annotate_test_reason", default=None,
    help=(
        "#4095: also stamp the originating work row's own test_reason with "
        "this text (typically the Test-stage triage verdict this escalation "
        "quotes), so `coord gates` shows WHY without a second lookup. "
        "Meaningful only for --stage test; a best-effort no-op if no "
        "work-like row is found."
    ),
)
@_CONFIG_OPTION
def escalate_record(
    repo: str,
    issue: int,
    stage: str,
    reason: str,
    gates: tuple[str, ...],
    proposed_command: str,
    assignment_id: str | None,
    annotate_test_reason: str | None,
    config_path: Path,
) -> None:
    """Write (or replace) the escalation record for REPO ISSUE."""
    from coord.state import record_drive_escalation  # noqa: PLC0415

    record_drive_escalation(
        repo,
        issue,
        stage=stage,
        reason=reason,
        gate_readings=" | ".join(gates),
        proposed_command=proposed_command,
        assignment_id=assignment_id,
    )
    if annotate_test_reason:
        try:
            _annotate_test_reason(_board_for_annotation(), repo, issue, annotate_test_reason)
        except Exception as exc:  # noqa: BLE001 — best-effort: the escalation
            # record above already landed; a failure annotating test_reason
            # must not make this command report failure over it.
            click.echo(
                f"warning: failed to annotate test_reason for {repo} #{issue}: {exc}",
                err=True,
            )
    click.echo(f"escalation recorded for {repo} #{issue}: {reason}")


@escalate_group.command("dismiss")
@click.argument("repo")
@click.argument("issue", type=int)
@_CONFIG_OPTION
def escalate_dismiss(repo: str, issue: int, config_path: Path) -> None:
    """Clear the escalation record for REPO ISSUE without acting on it."""
    from coord.state import dismiss_drive_escalation  # noqa: PLC0415

    ok = dismiss_drive_escalation(repo, issue)
    click.echo(f"dismissed escalation for {repo} #{issue}" if ok else "no escalation on file")


@escalate_group.command("list")
@click.option("--repo", "repo", default=None, help="Restrict to one repo (default: every repo).")
@_CONFIG_OPTION
def escalate_list(repo: str | None, config_path: Path) -> None:
    """List every open escalation record."""
    from coord.state import list_drive_escalations  # noqa: PLC0415

    entries = list_drive_escalations(repo)
    if not entries:
        click.echo("(no open escalations)")
        return
    for e in entries:
        click.echo(f"{e['repo_name']} #{e['issue_number']} [{e['stage']}]: {e['reason']}")
        if e.get("gate_readings"):
            click.echo(f"  gates:    {e['gate_readings']}")
        click.echo(f"  proposed: {e['proposed_command']}")


@escalate_group.command("run")
@click.argument("repo")
@click.argument("issue", type=int)
@click.option(
    "--dismiss/--no-dismiss", default=True,
    help="Clear the record after the proposed command exits 0 (default: yes).",
)
@_CONFIG_OPTION
def escalate_run(repo: str, issue: int, dismiss: bool, config_path: Path) -> None:
    """Run the proposed fix command for REPO ISSUE's escalation.

    This IS the "one-key human decision" the escalation record exists for —
    nothing runs it automatically; a human (or the TUI's "Run it" menu item,
    which shells out to exactly this) has to explicitly ask for it.
    """
    from coord.state import dismiss_drive_escalation, get_drive_escalation  # noqa: PLC0415

    entry = get_drive_escalation(repo, issue)
    if entry is None:
        raise click.ClickException(f"no escalation on file for {repo} #{issue}")
    command = (entry.get("proposed_command") or "").strip()
    if not command:
        raise click.ClickException("escalation record has no proposed command")
    click.echo(f"running: {command}")
    result = subprocess.run(command, shell=True)  # noqa: S602 — operator-approved, one-key by design
    if result.returncode != 0:
        raise click.ClickException(f"proposed command exited {result.returncode}")
    if dismiss:
        dismiss_drive_escalation(repo, issue)
    click.echo("done")


# ── coord decide (#2370) ──────────────────────────────────────────────────
#
# `escalate run` above only ever executes ONE thing — the single
# `proposed_command` a `drive_escalations` row carries — because that's all
# that source has. The `decisions` report (#2369) folds a SECOND source in
# too (drive-queue `blocked`/`failed` rows with no escalation record at
# all) and renders 2-4 *options* per card, not one, with no "run it" path
# for either source. `decide` generalizes `escalate run`'s one-key pattern
# to both sources and to any option on the card, not just the recommended
# one — it does NOT replace `escalate run`/`dismiss`/`list`, which keep
# working exactly as before.


@click.command("decide")
@click.argument("repo")
@click.argument("issue", type=int)
@click.argument("option_index", type=int, required=False, default=None)
@click.option(
    "--dismiss/--no-dismiss", default=True,
    help=(
        "Clear the drive_escalations record after a zero exit, IF the chosen "
        "option is that record's own proposed fix (default: yes). Has no "
        "effect otherwise — there is no record to dismiss for a non-default "
        "option or a drive-queue-only card; see the command's docstring."
    ),
)
@click.option(
    "--list", "list_only", is_flag=True, default=False,
    help=(
        "Print REPO ISSUE's numbered option list (same options "
        "`coord report run decisions`/`coord escalate list` would show for "
        "this row) and exit — runs nothing. #2375: the discovery path the "
        "bare/indexed invocation deliberately doesn't provide."
    ),
)
@_CONFIG_OPTION
def decide(
    repo: str,
    issue: int,
    option_index: int | None,
    dismiss: bool,
    list_only: bool,
    config_path: Path,
) -> None:
    """Run one option from REPO ISSUE's `decisions` report card.

    OPTION_INDEX is the 0-based index into that card's `options` list
    (default: the card's `recommended` option). The row is looked up fresh
    from `coord.reports.find_decision` every call — `decide` never re-derives
    or caches its own copy of "what the options are"; it runs exactly the
    `command_or_action` string the `decisions` report would show for that
    slot, via the same `subprocess.run(command, shell=True)` primitive
    `escalate run` uses.

    Post-run bookkeeping differs by what the card is backed by:

    \b
    - A card folded from a `drive_escalations` row (#1505), when the chosen
      option is that row's `proposed_command` (i.e. no OPTION_INDEX given,
      or OPTION_INDEX 0): behaves exactly like `coord escalate run` —
      dismiss the record on a zero exit (--dismiss/--no-dismiss).
    - Anything else — a non-default option on an escalation card, or ANY
      option on a drive-queue-only card (#2283/coord-portal#107, no
      escalation record exists for these): there is nothing to dismiss.
      `decide` echoes success/failure and stops; the card's own
      reappearance-or-not on the next `coord report run decisions` is the
      confirmation signal, not a second "resolved" state that could go
      stale or lie.

    #2375: `--list` prints this same card's numbered options and stops —
    the discovery path for picking OPTION_INDEX without running anything.
    It is a NEW flag, not a change to the bare/no-index contract above:
    that contract (#2370's own shipped acceptance criterion — no
    OPTION_INDEX behaves identically to `coord escalate run`) stays exactly
    as it is, since the TUI's "Recommended" click and #2370's acceptance
    test both depend on it firing immediately.
    """
    from coord.reports import find_decision, format_option_cell  # noqa: PLC0415
    from coord.state import dismiss_drive_escalation  # noqa: PLC0415

    card = find_decision(repo, issue)
    if card is None:
        raise click.ClickException(f"no decision on file for {repo} #{issue}")

    options = card.get("options") or []
    if not options:
        raise click.ClickException(f"decision card for {repo} #{issue} has no options")

    if list_only:
        click.echo(f"{repo} #{issue}: {len(options)} option(s)")
        for i, opt in enumerate(options):
            click.echo(f"  {i}: {format_option_cell(opt)}")
        return

    if option_index is None:
        index = next(
            (i for i, opt in enumerate(options) if opt.get("recommended")), 0
        )
    else:
        index = option_index
        if not 0 <= index < len(options):
            raise click.ClickException(
                f"option index {index} out of range for {repo} #{issue} "
                f"— valid indices are 0..{len(options) - 1}"
            )

    selected = options[index]
    command = str(selected.get("command_or_action") or "").strip()
    if not command:
        raise click.ClickException(f"option {index} for {repo} #{issue} has no command")

    click.echo(f"running: {command}")
    result = subprocess.run(command, shell=True)  # noqa: S602 — operator-approved, one-key by design
    if result.returncode != 0:
        raise click.ClickException(f"chosen command exited {result.returncode}")

    is_escalation_default = card.get("source") == "escalation" and bool(
        selected.get("recommended")
    )
    if is_escalation_default:
        if dismiss:
            dismiss_drive_escalation(repo, issue)
        click.echo("done")
    else:
        click.echo(
            "done (no escalation record to dismiss — rerun "
            "`coord report run decisions` to confirm this card is gone)"
        )


# ── coord mechanic-dispatch (#4095) ──────────────────────────────────────
#
# `coord drive`'s Test-failure triage router (`coord.drive._decide_test`)
# dispatches the #4094 mechanic leg through THIS command, never a direct
# in-process call into `coord.board_doctor_policy` — "every board mutation
# this driver performs goes through the CLI" (see `coord/drive.py`'s own
# module docstring, and #1384 which that rule exists to avoid repeating).
# The verdict is rebuilt from the flags `_decide_test` already derived from
# its OWN `GitMergeVerifier.triage_test_failure` call, never re-classified
# here — a second, independent classifier run (`coord.failure_triage.
# triage_failure` has a model tier) could disagree with the first and
# dispatch on a different bucket than the one the drive pane just logged
# (#2096, "one question, one answer").


def _board_for_annotation() -> Any:
    """The read :func:`_annotate_test_reason` needs, thin-client-safe —
    ``coord.state.build_board`` refuses off the daemon host
    (``_thin_client_local_board_guard``), so a caller (``escalate record``)
    that may run from a thin client goes through the SAME ``GET /board`` +
    :func:`coord.client.board_from_payload` reassembly ``coord status``
    already uses there, instead of a second, divergent projection.
    """
    from coord.client import board_from_payload, fetch_board_payload, resolve_board_service  # noqa: PLC0415
    from coord.state import build_board  # noqa: PLC0415

    svc = resolve_board_service()
    if svc is not None:
        return board_from_payload(fetch_board_payload(svc))
    return build_board()


def _annotate_test_reason(board: Any, repo: str, issue: int, text: str) -> bool:
    """#4095: stamp *text* onto the originating work row's own
    ``test_reason`` column — the one column ``coord gates`` already
    renders — so a Test-stage triage verdict (or the human-escalation
    reason quoting it) is visible from that read-only surface without a
    second, out-of-band lookup. No new column, no schema migration: this
    reuses the existing ``test_reason``/`coord.state.record_test_verdict`
    plumbing every other Test-gate writer already goes through.

    A no-op (returns ``False``) when no work-like row exists for
    *repo*/*issue* — never raises; both call sites (``mechanic-dispatch``,
    ``escalate record``) treat the annotation as best-effort and must not
    fail their own primary job over it.
    """
    from coord.gates import _select_winning_work_assignment, assignments_for_issue  # noqa: PLC0415
    from coord.models import WORK_LIKE_TYPES  # noqa: PLC0415
    from coord.state import record_test_verdict  # noqa: PLC0415

    work_like = [
        a for a in assignments_for_issue(board, repo, issue) if a.type in WORK_LIKE_TYPES
    ]
    if not work_like:
        return False
    winner = _select_winning_work_assignment(work_like)
    if not winner.assignment_id:
        return False
    original = (winner.test_reason or "").strip()
    annotated = text if (not original or original == text) else f"{text} || {original}"
    # test_state="failed" keeps the row's own verdict exactly as it was —
    # this never clears it back to the `record_test_verdict(test_state=
    # None)` "un-stick for re-dispatch" meaning, it only refreshes the
    # text describing the SAME failed verdict.
    record_test_verdict(
        assignment_id=winner.assignment_id, test_state="failed", test_reason=annotated,
    )
    return True


@click.command("mechanic-dispatch")
@click.argument("repo")
@click.argument("issue", type=int)
@click.argument("branch_sha")
@click.option("--diagnosis", default="", help="The triage verdict's own diagnosis text.")
@click.option(
    "--confidence", type=float, default=0.0,
    help="The triage verdict's own confidence (0.0-1.0).",
)
@click.option("--via", default="", help="Which triage tier produced the verdict.")
@click.option(
    "--repro-capability", default=None,
    help="Machine capability the triage verdict says can reproduce the failure.",
)
@click.option(
    "--proposed-file", "proposed_files", multiple=True,
    help="A file the triage verdict guesses a fix would touch. Repeatable.",
)
@click.option(
    "--proposed-line-delta", type=int, default=0,
    help="The triage verdict's own estimated changed-line count.",
)
@click.option(
    "--evidence", "evidence", multiple=True,
    help=(
        "One line of supporting evidence for the leg's briefing — the "
        "failure text the triage call classified. Repeatable."
    ),
)
@click.option(
    "--failed-stage-command", "failed_stage_commands", multiple=True,
    help=(
        "A command the leg should run to reproduce the failure, instead "
        "of the full suite. Repeatable."
    ),
)
@_CONFIG_OPTION
def mechanic_dispatch(
    repo: str,
    issue: int,
    branch_sha: str,
    diagnosis: str,
    confidence: float,
    via: str,
    repro_capability: str | None,
    proposed_files: tuple[str, ...],
    proposed_line_delta: int,
    evidence: tuple[str, ...],
    failed_stage_commands: tuple[str, ...],
    config_path: Path,
) -> None:
    """Route a BUCKET_MECHANICAL Test-stage triage verdict to the #4094
    mechanic leg — ``coord drive``'s one call site for it (#4095).

    Exits 0 only when `coord.board_doctor_policy.dispatch_mechanic_leg_
    for_triage` confirms a dispatch actually started; exits 1 for every
    graceful decline (the per-repo tier not opted in, the env-var kill
    switch, the repo already at its capacity cap, no machine declaring the
    needed capability, the one-mechanic-leg-per-SHA budget already spent,
    ...). `coord drive` treats either non-zero outcome identically: fall
    back to an ordinary fix round on the next poll — never a worse outcome
    than before this command existed.

    Daemon-host only, same guard as `coord drive-queue doctor-act` — the
    board this reads/mutates lives there; a thin client would act on an
    empty local copy and always decline.
    """
    from coord.board_service import is_remote  # noqa: PLC0415

    if is_remote():
        raise click.ClickException(
            "mechanic-dispatch mutates the daemon host's board directly — "
            "run it there (a board_service-configured thin client would act "
            "on an empty local copy and always decline)."
        )

    from coord.board_doctor_policy import dispatch_mechanic_leg_for_triage  # noqa: PLC0415
    from coord.failure_triage import BUCKET_MECHANICAL, ProposedScope, TriageVerdict  # noqa: PLC0415
    from coord.state import build_board  # noqa: PLC0415

    config = _load_config(config_path)
    board = build_board()
    verdict = TriageVerdict(
        bucket=BUCKET_MECHANICAL,
        diagnosis=diagnosis,
        repro_capability=repro_capability,
        proposed_scope=ProposedScope(
            files=tuple(proposed_files), estimated_line_delta=proposed_line_delta,
        ),
        confidence=confidence,
        via=via,
    )
    dispatched = dispatch_mechanic_leg_for_triage(
        repo, issue, board, config, verdict=verdict, branch_sha=branch_sha,
        evidence=evidence, failed_stage_commands=failed_stage_commands,
    )
    # #4095: persist the verdict onto the work row regardless of whether
    # the dispatch itself was confirmed or declined — `coord gates` should
    # show WHY Test failed either way, not only on the confirmed path.
    try:
        _annotate_test_reason(
            board, repo, issue,
            f"[triage=mechanical via={via or 'unknown'} confidence={confidence:.2f} "
            f"sha={branch_sha}] {diagnosis.strip() or 'no diagnosis text'}",
        )
    except Exception as exc:  # noqa: BLE001 — best-effort: the dispatch
        # decision above already happened; a failure annotating
        # test_reason must not change this command's own exit code.
        click.echo(
            f"warning: failed to annotate test_reason for {repo} #{issue}: {exc}",
            err=True,
        )
    if dispatched:
        click.echo(f"mechanic leg dispatched for {repo} #{issue} at sha={branch_sha}")
        return
    click.echo(
        f"mechanic leg NOT dispatched for {repo} #{issue} — declined; an "
        "ordinary fix round should follow",
        err=True,
    )
    sys.exit(1)
