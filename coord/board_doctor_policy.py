"""Board-doctor policy: the closed auto-action allowlist (C2) and the
escalation-alert dispatcher (C4).

`coord.board_doctor.sweep` is pure plumbing: given a `tier_of` callable
and dispatcher callbacks, it classifies, dispatches, tracks convergence and
logs, but it deliberately does not decide WHICH cause gets auto-acted on
(C2's job) or WHAT an alert actually does (C4's job) — see that module's
docstring. This module is where those two decisions live.

Why the allowlist below is EMPTY
---------------------------------
A cause belongs on the auto-action allowlist only once the Phase 0 block
log shows it recurring with a known-safe fix — never as a pre-approved
guess. That log is not yet mined for a recurring, safe-to-automate cause,
so :data:`ALLOWLIST` ships empty: every cause is alert-tier today. A later
change populates it from log evidence without touching this module's shape
or `sweep`'s control flow. A wrong auto-action default is worse than an
extra, harmless alert.

Why the alert path reuses `drive_escalations`
------------------------------------------------
"Issue filing" is not a literal new-GitHub-issue flow — `gh` is denied to
the sweep's own host context, and the fleet already has a first-class
"something needs a human" surface: the `drive_escalations` table
(`coord.state.record_drive_escalation`), already wired into the daemon
HTTP API, the board schema and the dashboard/reports, and already used
this same way by `coord.commands.drive_queue`'s roll-ledger escalation
(one question, one answer — a second filing mechanism would answer the
same question twice). :func:`escalation_alert_dispatcher` writes to
exactly that surface, with `proposed_command="coord status"`, so an
operator landing on the alert has the next action right there.
"""

from __future__ import annotations

from typing import Callable

from coord.board_doctor import ALERT_SKIPPED, TIER_ALERT, TIER_AUTO, StuckRow

#: cause -> auto-action name. Empty by design; see the module docstring.
#: A future PR populates this from Phase 0 block-log evidence of a
#: recurring, safe-to-automate cause — never speculatively.
ALLOWLIST: dict[str, str] = {}

#: cause -> the C3 callable that actually performs the auto-action named in
#: :data:`ALLOWLIST`. Kept as a separate mapping (rather than trusting
#: :data:`ALLOWLIST` alone) so :func:`policy_tier_of` can never promote a
#: cause to :data:`coord.board_doctor.TIER_AUTO` on the strength of an
#: allowlist entry with no real dispatcher behind it — that combination
#: would silently do nothing while still suppressing the alert tier's own
#: escalation for the cause. Empty today for the same reason
#: :data:`ALLOWLIST` is.
AUTO_DISPATCHERS: dict[str, Callable[[StuckRow], None]] = {}


def policy_tier_of(cause: str) -> str:
    """#3745's C2: real (if currently conservative) cause -> tier policy.

    Returns :data:`coord.board_doctor.TIER_AUTO` only for a cause present
    in BOTH :data:`ALLOWLIST` and :data:`AUTO_DISPATCHERS` — every other
    cause, which today is every cause since both mappings are empty, gets
    :data:`coord.board_doctor.TIER_ALERT`. This is the function `coord
    drive-queue sweep` wires in as `tier_of`.
    """
    if cause in ALLOWLIST and cause in AUTO_DISPATCHERS:
        return TIER_AUTO
    return TIER_ALERT


def _board_doctor_stage(queue: str) -> str:
    return f"board-doctor:{queue}"


def escalation_alert_dispatcher(row: StuckRow) -> bool | str:
    """#3745's C4: surface an alert-tier row via `drive_escalations`.

    Parses ``row.key`` (always ``"repo#issue"`` for both drive-queue and
    merge-queue rows — `coord.drive_queue.entry_key`, `queue_diagnose.
    Diagnosis.key`, and `coord.board_doctor.merge_queue_rows` all build it
    the same way) and records/renews the escalation for that issue.

    Returns ``True`` only once the write is confirmed by reading the row
    back — `sweep`'s "escalate exactly once" contract depends on this
    return value to decide whether it may mark the row escalated, so a
    silently-swallowed failure here must never look like success. Skips
    the write when a more specific, non-board-doctor escalation already
    covers this issue, so a generic `coord status` pointer never clobbers
    it; that case returns :data:`coord.board_doctor.ALERT_SKIPPED` rather
    than ``True``, because no board-doctor alert exists to suppress future
    ones — `drive_escalations` holds one row per issue, so the covering
    escalation may be dismissed by its own writer at any time, and the
    next sweep must then be free to file this row's alert itself.

    Never raises: an escalation write failing must not take the sweep
    down with it, the same contract
    `coord.commands.drive_queue._escalate_roll_ledger` already relies on —
    a `warning:` line on stderr is the floor.
    """
    import click  # noqa: PLC0415

    from coord.state import get_drive_escalation, record_drive_escalation  # noqa: PLC0415

    repo_name, _, issue_text = row.key.rpartition("#")
    if not repo_name:
        click.echo(
            f"warning: board-doctor alert dropped — unparseable row key "
            f"{row.key!r} (expected 'repo#issue')",
            err=True,
        )
        return False
    try:
        issue_number = int(issue_text)
    except ValueError:
        click.echo(
            f"warning: board-doctor alert dropped — unparseable row key "
            f"{row.key!r} (expected 'repo#issue')",
            err=True,
        )
        return False

    stage = _board_doctor_stage(row.queue)

    try:
        existing = get_drive_escalation(repo_name, issue_number)
        if existing is not None and existing.get("stage") != stage:
            # A more specific escalation (e.g. plan.blocked) already covers
            # this issue — don't overwrite its reason/created_at with our
            # generic "coord status" pointer.
            return ALERT_SKIPPED

        reason = (
            f"board-doctor: {row.queue} row {row.key} stuck on cause="
            f"{row.cause} (confidence={row.confidence}) — see `coord "
            "drive-queue sweep --json` for the full decision log"
        )
        gate_readings = f"queue={row.queue} | cause={row.cause} | evidence={' | '.join(row.evidence)}"
        record_drive_escalation(
            repo_name,
            issue_number,
            stage=stage,
            reason=reason,
            gate_readings=gate_readings,
            proposed_command="coord status",
        )

        confirmed = get_drive_escalation(repo_name, issue_number)
        return bool(
            confirmed is not None
            and confirmed.get("stage") == stage
            and confirmed.get("reason") == reason
        )
    except Exception as exc:  # noqa: BLE001 — see docstring: never crash the sweep
        click.echo(f"warning: could not record board-doctor escalation: {exc}", err=True)
        return False


def dismiss_resolved_escalation(queue: str, key: str) -> None:
    """Clear a board-doctor escalation once its row stops appearing.

    `sweep`'s ``clear_absent_rows`` walk calls this for every ``(queue,
    key)`` that dropped out of the current sweep — the cause resolved, so
    any alert filed for it is now stale. Only dismisses an escalation this
    module itself filed (``stage == "board-doctor:<queue>"``); a
    differently-staged escalation on the same issue (e.g. from
    `plan.blocked`) is left untouched, since its cause has nothing to do
    with the board-doctor row going away. Never raises — a best-effort
    cleanup failing must not take the sweep down with it.
    """
    import click  # noqa: PLC0415

    from coord.state import dismiss_drive_escalation, get_drive_escalation  # noqa: PLC0415

    repo_name, _, issue_text = key.rpartition("#")
    if not repo_name:
        return
    try:
        issue_number = int(issue_text)
    except ValueError:
        return

    try:
        existing = get_drive_escalation(repo_name, issue_number)
        if existing is not None and existing.get("stage") == _board_doctor_stage(queue):
            dismiss_drive_escalation(repo_name, issue_number)
    except Exception as exc:  # noqa: BLE001 — best-effort, never crash the sweep
        click.echo(f"warning: could not dismiss board-doctor escalation: {exc}", err=True)
