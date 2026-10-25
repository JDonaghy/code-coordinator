"""Board-doctor policy: the closed auto-action allowlist (C2) and the
escalation-alert dispatcher (C4) — #2268's Phases 2 and 3.

#3758's `coord.board_doctor.sweep` harness is pure plumbing: given a
`tier_of` callable and dispatcher callbacks, it classifies, dispatches,
tracks convergence and logs, but it deliberately does not decide WHICH
cause gets auto-acted on (that is C2's job) or WHAT an alert actually does
(C4's job) — see that module's docstring. This module is where those two
decisions live, now that the Phase 0 block-log recorder (#2235) has run
long enough to inform them.

Why the allowlist below is EMPTY
---------------------------------
#2268 is explicit that a cause belongs on the auto-action allowlist only
once the Phase 0 log shows it recurring with a known-safe fix — never as a
pre-approved guess, however plausible the worked example (claude-
coordinator#2208's 8-hour dead-leg stall, cleared in 14 minutes by a human
running `coord status`). That log is not yet mined for a recurring,
safe-to-automate cause, so :data:`ALLOWLIST` ships empty: every cause is
alert-tier today, identical in effect to :func:`coord.board_doctor.
default_tier_of` but now the real, documented, auditable policy seam
C2 asked for — the next PR populates it from log evidence without
touching this module's shape or `sweep`'s control flow. A wrong auto-action
default is worse than an extra, harmless alert (#3758's own words).

Why the alert path reuses `drive_escalations`
------------------------------------------------
"Issue filing" in #2268's title is not a literal new-GitHub-issue flow —
`gh` is denied to the sweep's own host context, and the fleet already has
a first-class "something needs a human" surface: the `drive_escalations`
table (`coord.state.record_drive_escalation`), already wired into the
daemon HTTP API, the board schema and the dashboard/reports, and already
used this same way by `coord.commands.drive_queue`'s roll-ledger escalation
(#2096: one question, one answer — a second filing mechanism would answer
the same question twice). :func:`escalation_alert_dispatcher` writes to
exactly that surface, with `proposed_command="coord status"` — the same
command that resolved #2208's worked example — so an operator landing on
the alert has the next action right there.
"""

from __future__ import annotations

from coord.board_doctor import StuckRow

#: cause -> auto-action name. Empty by design; see the module docstring.
#: A future PR populates this from Phase 0 block-log evidence of a
#: recurring, safe-to-automate cause — never speculatively.
ALLOWLIST: dict[str, str] = {}


def policy_tier_of(cause: str) -> str:
    """#3745's C2: real (if currently conservative) cause -> tier policy.

    Returns :data:`coord.board_doctor.TIER_AUTO` only for a cause present
    in :data:`ALLOWLIST`; every other cause — which today is every cause,
    since the allowlist is empty — gets :data:`coord.board_doctor.
    TIER_ALERT`. Behaviorally identical to `coord.board_doctor.
    default_tier_of` until the allowlist gains its first entry; this is the
    function `coord drive-queue sweep` wires in instead, so that future
    entry is the only diff a later PR needs.
    """
    from coord.board_doctor import TIER_ALERT, TIER_AUTO  # noqa: PLC0415

    return TIER_AUTO if cause in ALLOWLIST else TIER_ALERT


def escalation_alert_dispatcher(row: StuckRow) -> None:
    """#3745's C4: surface an alert-tier row via `drive_escalations` (#2268).

    Parses ``row.key`` (always ``"repo#issue"`` for both drive-queue and
    merge-queue rows — `coord.drive_queue.entry_key`, `queue_diagnose.
    Diagnosis.key`, and `coord.board_doctor.merge_queue_rows` all build it
    the same way) and records/renews the escalation for that issue. Never
    raises: an escalation write failing must not take the sweep down with
    it, the same contract `coord.commands.drive_queue._escalate_roll_
    ledger` already relies on — a `warning:` line on stderr is the floor.
    """
    import click  # noqa: PLC0415

    from coord.state import record_drive_escalation  # noqa: PLC0415

    repo_name, _, issue_text = row.key.rpartition("#")
    try:
        issue_number = int(issue_text)
    except ValueError:
        click.echo(
            f"warning: board-doctor alert dropped — unparseable row key "
            f"{row.key!r} (expected 'repo#issue')",
            err=True,
        )
        return

    reason = (
        f"board-doctor: {row.queue} row {row.key} stuck on cause="
        f"{row.cause} (confidence={row.confidence}) — see `coord "
        "drive-queue sweep --json` for the full decision log"
    )
    gate_readings = f"queue={row.queue} | cause={row.cause} | evidence={' , '.join(row.evidence)}"
    try:
        record_drive_escalation(
            repo_name,
            issue_number,
            stage=f"board-doctor:{row.queue}",
            reason=reason,
            gate_readings=gate_readings,
            proposed_command="coord status",
        )
    except Exception as exc:  # noqa: BLE001 — see docstring: never crash the sweep
        click.echo(f"warning: could not record board-doctor escalation: {exc}", err=True)
