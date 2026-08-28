"""Decision half of the nightly daemon-host release window (#2112).

#2587 UPDATE — read this before the rest of this docstring, which otherwise
describes a mechanism this module no longer drives directly. Measured
2026-08-22: the "stop the timer, poll a bounded drain, roll, restart the
timer" sequence this module was built around ran for its full 60-minute
deadline, drained nothing, and rolled nothing — the fleet-wide quiescent
window it waited for never arrived, because the drive queue refills from its
own backlog continuously. `coord.commands.release.release_nightly_window`
no longer calls the drain loop described below; it sets a roll-pending
marker (`coord.drive_queue.RollPending`, persisted via
`coord.commands.drive_queue.write_roll_pending`) and returns immediately.
The marker is what `coord drive-queue tick` checks every ~3 minutes — the
fleet's OWN natural inter-drive gap, which happens far more often than a
fleet-wide idle window — and the tick fires the roll the instant it observes
one, never stopping any timer to get there. `needs_roll`, the
`WindowRecord`/journal machinery, and `render_record` below are all still
live (the journal now also records `STATUS_ROLL_PENDING` runs); `DrainOutcome`
and the bounded-drain constants below describe the RETIRED mechanism, kept
only because `coord.commands.release._drain`/`_run_reconcile_tick` (and
their own direct unit tests) still exist as a manual escape hatch, wired to
nothing by default.

`coord release propagate` (#1835/#2067) rolls each host at ITS OWN quiescent
window — except the daemon host, which the daemon-first lane order (#1835's
LANE ORDER, the documented 405) forces to gate the *whole run*: no lane may
roll ahead of an unrolled daemon. dellserver is both the daemon host and a
work machine. Until #2138, every unpinned drive-queue entry charged it via
``launch_host`` (see :func:`coord.release_propagate.busy_host_for_entry`)
regardless of where the worker actually landed — so almost any drive anywhere
kept the daemon "busy" and deferred the entire fleet. Measured 2026-08-10: the
fleet sat eleven releases behind for a day with elitebook idle and rollable
throughout; measured again 2026-08-12 with the fleet stuck six hours the same
way, because #2101 had only fixed the ``--machine``-pinned half of that
attribution and production entries are unpinned. #2138 resolves an unpinned
entry's real worker host from its live assignment row instead, so the daemon
is charged only when it is genuinely running the work itself — but a row
that is `running` with no live assignment right now (between legs) still
reads as unattributable and blocks every host, the daemon included. This
module remains the belt-and-braces answer for that residual case, and for
the daemon host doing real work directly.

#2618: the reason this module treats fleet-wide quiescence as a hard
prerequisite for restarting `coord-serve` at all — rather than a survivable
blip for whatever else is running elsewhere — was the WRITE side of an
in-flight `coord drive`: every daemon-mutating `coord` subcommand it spawns
made exactly one unretried request, so a restart landing mid-write killed
that drive outright. `coord.drive.Driver._spawn` now retries a clean
connection-refusal (never a reset/timeout — see its comment) across a
bounded window sized for an ordinary restart, so a drive elsewhere in the
fleet is no longer inherently unable to survive `coord-serve` restarting.
That does not, on its own, make stopping `coord-drive-queue.timer` here
unnecessary — the timer is what LAUNCHES new drives onto the daemon host,
a different concern (see `coord.release_propagate`'s own #2618 note for the
full split) — but it does mean a future revisit of "must the daemon host's
OWN restart wait for drives running on OTHER hosts" starts from "the drives
can take it" rather than "unconfirmed, so assume they cannot."

#2101 (release cordons) answers this for every OTHER host: cordon it,
drain it, roll it the moment it's free. It cannot answer it for the daemon
host itself — cordoning stops NEW work from routing there, but the daemon
host is what runs the drive-queue tick that launches drives fleet-wide in
the first place, so cordoning it does not stop new drives from being queued
against it. The only way to guarantee the daemon host reaches quiescence is
to stop the thing that launches work onto it. Hence this module: a nightly
window that stops `coord-drive-queue.timer`, waits (bounded) for whatever is
already running to finish, rolls, and restarts the timer — always, whether
or not the roll happened.

GATED ON #2110, HARD PREREQUISITE
----------------------------------
Steps "stop the timer, wait, roll" are exactly the sequence that deadlocked
on 2026-08-10: the reconciler that moves a finished drive from `running` to
`done` lives *inside* `coord drive-queue tick`, so stopping the timer stops
reconciliation too, and the last drive's row stays `running` forever — the
daemon host reads as busy permanently and this window would defer forever,
every night, unattended, and exit 0. #2110 made `coord drive-queue tick
--reconcile-only` (equivalently `--max-parallel 0`) safe to call on its own,
which is exactly what the drain loop below needs: reconcile without
launching anything new.

THE THREE TRAPS THIS MODULE IS SHAPED AROUND
----------------------------------------------
1. **Never `--force`.** That flag kills in-flight headless workers — the
   whole reason propagation is quiescence-scheduled — and an unattended
   nightly job must never carry it. If the drain does not finish, this
   module says so and declines; it does not reach for the escape hatch.
2. **The drain is bounded.** :data:`DEFAULT_DRAIN_WAIT_SECONDS` is the
   deadline after which the caller must restart the timer and report
   failure rather than leave the queue stopped into the working day — the
   mirror image of "an expired deadline stops the observer, not the work."
   Renamed from ``DEFAULT_DRAIN_DEADLINE_SECONDS`` (#2136): this module's
   3600s bounded *wait* for its own drain loop and
   :mod:`coord.release_cordon`'s 5400s cordon-escalation deadline are
   different concepts that used to share one name across two release
   modules — a readability trap that reads as one constant drifting in
   value, when they were never the same constant.
3. **A skipped night must be loud.** Every non-happy status here
   (:data:`STATUS_DRAIN_TIMEOUT`, :data:`STATUS_PROPAGATE_DEFERRED`,
   :data:`STATUS_PROPAGATE_FAILED`, :data:`STATUS_ERROR`) is something the
   I/O shell (`coord/commands/release.py`'s `nightly-window` command) is
   expected to escalate through `coord.state.record_drive_escalation` — the
   same channel #2101's drain-deadline escalation and #2082 exist to make
   this class of silence impossible.

Architecture mirrors `coord/release_propagate.py` and `coord/release_cordon.py`
on purpose: this module is pure decision-making over already-fetched facts
(a version string, a deadline, elapsed time) plus the journal format, so it
is unit-testable with no fleet, no systemd and no board. Everything that
needs a live host — stopping/starting the timer, running the reconcile-only
tick, invoking `coord release propagate`, writing the escalation — lives in
the command's I/O shell, next to `release_propagate`'s own for the same
reason (see that module's docstring's "what lives here" split).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

#: The systemd --user timer this window stops for the duration of the
#: drain. Overridable (`--queue-timer`) purely for tests and for an
#: unusual install that renamed the unit; production has exactly one name.
DEFAULT_QUEUE_TIMER = "coord-drive-queue.timer"

#: Bounded wait (trap 2) for in-flight drives to finish before the window
#: gives up, restarts the queue and reports failure. An hour leaves ample
#: room inside the 22:00-08:00 quiet-hours window even started as late as
#: 03:00 — the queue is never stopped anywhere near the working day.
#:
#: Formerly named ``DEFAULT_DRAIN_DEADLINE_SECONDS`` — the same name
#: :mod:`coord.release_cordon` uses for a different value (5400.0, its
#: cordon-escalation deadline). Renamed (#2136) so the two stop reading as
#: one constant that drifted between modules.
DEFAULT_DRAIN_WAIT_SECONDS = 3600.0

#: How often the drain loop reconciles the queue and re-checks quiescence.
#: `coord-drive-queue.timer` itself fires every 3 minutes in production —
#: polling much faster than that buys nothing (nothing on the board can
#: change faster) and just spends `coord drive-queue tick --reconcile-only`
#: subprocesses for no reason.
DEFAULT_POLL_INTERVAL_SECONDS = 30.0


# ── status vocabulary ────────────────────────────────────────────────────
#
# A small closed set, mirroring release_propagate.py's STATUS_* — so an
# operator who already knows how to read `coord release history` does not
# have to learn a second vocabulary for this journal.

STATUS_UP_TO_DATE = "up-to-date"
STATUS_DRY_RUN = "dry-run"
STATUS_ROLLED = "rolled"
STATUS_DRAIN_TIMEOUT = "drain-timeout"
STATUS_PROPAGATE_DEFERRED = "propagate-deferred"
STATUS_PROPAGATE_FAILED = "propagate-failed"
STATUS_ERROR = "error"
#: #2587: this run set (or found, and left standing) a roll-pending marker —
#: the drive-queue tick, not this command, will fire the actual roll at the
#: next inter-drive gap. A GOOD outcome (see OK_STATUSES below), not a
#: failure to roll: the whole point of #2587 is that "not rolled THIS
#: instant" and "not working" are different things, and #2112's old
#: STATUS_DRAIN_TIMEOUT — the loud, failing outcome it replaces for the
#: still-busy case — must never be confused with it.
STATUS_ROLL_PENDING = "roll-pending"
#: #2583: the daemon host needs a roll (there is a delta) but it has not yet
#: reached ``propagation.min_releases_behind``/``--min-behind`` — a
#: REPORTED no-op. Deliberately distinct from the drain-era
#: :data:`STATUS_DRAIN_TIMEOUT`/:data:`STATUS_PROPAGATE_DEFERRED` (both mean
#: "tried and could not"): holding never sets or touches the roll-pending
#: marker at all, so it is a GOOD outcome, same tier as
#: :data:`STATUS_ROLL_PENDING`.
STATUS_HOLDING = "holding"
#: #2889 items 1-3: a FRESH `RollPending` arm (no existing marker for this
#: campaign at all) was deliberately declined this run — rate-limited, or a
#: genuine drive-queue entry is provably occupying the daemon host right now
#: (see `coord.commands.release._fresh_arm_refusal_reason`). A GOOD outcome,
#: same tier as :data:`STATUS_ROLL_PENDING`/:data:`STATUS_HOLDING`: the
#: queue keeps launching normally, and a LATER run (the next nightly/
#: periodic timer firing, or the drive-queue tick's own inter-drive-gap
#: watch once something else eventually arms a marker) tries again — never
#: silent, `record.error` names the specific reason declined.
STATUS_ARM_DEFERRED = "arm-deferred"
#: #2889 item 1: the roll LEDGER (`coord.drive_queue.RollLedger`) has
#: crossed its cumulative frozen-time bound — this target has now failed to
#: roll unattended across several separate marker generations, not just one
#: unlucky busy night. Deliberately LOUD (unlike
#: :data:`STATUS_ARM_DEFERRED` just above): every further fresh arm is
#: refused until an operator runs `coord drive-queue cancel-roll`, so a
#: skipped night here really is "supposed to happen and did not" — trap 3.
STATUS_LEDGER_ESCALATED = "ledger-escalated"

#: Statuses meaning "this window did what it was for, or correctly had
#: nothing to do" — everything else is a night propagation was supposed to
#: happen and did not (trap 3: loud, not silent).
OK_STATUSES = frozenset(
    {
        STATUS_UP_TO_DATE, STATUS_ROLLED, STATUS_DRY_RUN, STATUS_ROLL_PENDING,
        STATUS_HOLDING, STATUS_ARM_DEFERRED,
    }
)

#: The inverse of OK_STATUSES, spelled out for readability at call sites.
LOUD_STATUSES = frozenset(
    {
        STATUS_DRAIN_TIMEOUT, STATUS_PROPAGATE_DEFERRED, STATUS_PROPAGATE_FAILED,
        STATUS_ERROR, STATUS_LEDGER_ESCALATED,
    }
)


def needs_roll(daemon_version: str | None, target_version: str | None) -> bool:
    """Is there anything for this window to do?

    #2112 acceptance 3: "with the fleet already current, the job does not
    stop the queue at all." This is the check that has to answer that
    *before* anything touches the queue.

    `daemon_version=None` (no data — an unreachable daemon host, an
    unreadable python lane) reads as NEEDS a roll, never as "current": #1834's
    rule is that no-data is not evidence of agreement, and skipping the
    window on a guess is exactly the silent-no-op shape this issue exists to
    close. Delegates the actual comparison to
    :func:`coord.release_cordon.version_drift` rather than a third
    reimplementation of version arithmetic in this codebase.
    """
    from coord.release_cordon import version_drift  # noqa: PLC0415

    if not target_version:
        return False
    drift = version_drift(daemon_version, target_version)
    return drift is None or drift > 0


@dataclass
class DrainOutcome:
    """What the bounded wait for in-flight drives found (trap 2)."""

    drained: bool
    elapsed_seconds: float
    detail: str = ""


@dataclass
class WindowRecord:
    """One nightly-window attempt, start to finish, as journalled.

    Deliberately shaped like `release_propagate.PropagationRecord`: same
    append-only-JSONL-one-object-per-attempt journal, same reason — this
    record must survive a half-installed venv and be readable with `tail`
    while the very upgrade it describes is in flight.
    """

    started_at: float
    target_version: str | None = None
    daemon_host: str | None = None
    daemon_version: str | None = None
    status: str = STATUS_ERROR
    queue_timer: str = DEFAULT_QUEUE_TIMER
    queue_stopped: bool | None = None
    queue_stop_detail: str = ""
    drained: bool | None = None
    drain_seconds: float | None = None
    drain_detail: str = ""
    queue_restarted: bool | None = None
    queue_restart_detail: str = ""
    propagate_status: str | None = None
    propagate_exit_code: int | None = None
    propagate_output: str = ""
    #: The propagation journal record's OWN ``started_at`` — the join key
    #: (#2187 proposal 2) that lets `window-history` be correlated to
    #: `coord release history` for the SAME run, instead of each store
    #: independently re-deciding what happened. ``None`` when no matching
    #: journal record could be found (see
    #: `coord.commands.release._latest_propagate_record_since`) — there is
    #: then nothing to join to.
    propagate_started_at: float | None = None
    #: #2583: this run's own readings of the min-releases-behind gate — see
    #: `coord.release_propagate.PropagationRecord`'s twin fields for the
    #: same shape and reasoning. ``None`` when the gate was never evaluated
    #: (``min_releases_behind <= 1``, the default).
    releases_behind: int | None = None
    min_releases_behind: int | None = None
    finished_at: float | None = None
    error: str | None = None
    dry_run: bool = False
    #: #2889 item 4: what started THIS run's `coord-release-window.service`
    #: invocation, when it can be known — ``"timer-or-manual"`` (the unit's
    #: own static default `Environment=`, covering both its
    #: `coord-release-window.timer` trigger and a human running `systemctl
    #: --user start` by hand — systemd does not record a `systemctl start`'s
    #: calling process, so these two cannot be told apart from inside the
    #: unit) or ``"drive-queue-tick"`` (the OTHER known trigger —
    #: `coord.commands.drive_queue._fire_pending_roll`'s own
    #: `--setenv=COORD_ROLL_INVOKER=drive-queue-tick`, which overrides the
    #: unit's static default for that one invocation). Empty when this run
    #: was not started via the packaged unit at all (a bare CLI invocation,
    #: e.g. in a test or an operator's own shell) and so the env var was
    #: never set. See `coord/deploy/coord-release-window.service`'s
    #: "INVOKER" section for the full mechanism and its honest limits.
    invoked_by: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def ok(self) -> bool:
        return self.status in OK_STATUSES


# ── the journal ──────────────────────────────────────────────────────────

#: Filename under the coord state root (`~/.coord` on Linux — see
#: `coord.platform_paths.default_coord_dir`). Separate from
#: `release_propagate.JOURNAL_NAME`: this record carries fields (queue
#: stop/drain/restart) a plain propagate attempt does not have, and
#: conflating the two would make either journal's shape a lie about the
#: other.
JOURNAL_NAME = "release_window.jsonl"

#: Records kept when the journal is trimmed. One per night, so this is
#: years of history — small enough to `cat`, generous enough that "when did
#: this last actually roll something" never scrolls off.
JOURNAL_MAX_RECORDS = 2000


def journal_path(state_dir: Path) -> Path:
    return Path(state_dir) / JOURNAL_NAME


def append_record(state_dir: Path, record: WindowRecord) -> Path:
    """Append *record* as one JSON line. Best effort by contract.

    A window run must never fail *because* it could not write its own
    diary — but a silently-unwritten diary is the exact 2026-08-04/#2082
    shape, so the caller is told (the shell reports a write failure as a
    warning and still exits on the real outcome, same as
    `release_propagate.append_record`).
    """
    path = journal_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record.to_dict(), sort_keys=True) + "\n")
    return path


def read_records(state_dir: Path, *, limit: int | None = None) -> list[dict]:
    """Most-recent-last records from the journal; unparseable lines skipped.

    A torn final line (the process killed mid-append — see #2112 acceptance
    4) must not make the whole history unreadable.
    """
    path = journal_path(state_dir)
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    if limit is not None and limit >= 0:
        out = out[-limit:]
    return out


def trim_journal(state_dir: Path, *, keep: int = JOURNAL_MAX_RECORDS) -> int:
    """Truncate the journal to its last *keep* records. Returns records kept."""
    records = read_records(state_dir)
    if len(records) <= keep:
        return len(records)
    kept = records[-keep:]
    path = journal_path(state_dir)
    path.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in kept), encoding="utf-8"
    )
    return len(kept)


# ── rendering ────────────────────────────────────────────────────────────


def _stamp(ts: float | None) -> str:
    if not ts:
        return "?"
    import datetime as _dt  # noqa: PLC0415 — leaf import, keeps the module light

    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


_STATUS_MARK = {
    STATUS_UP_TO_DATE: "=",
    STATUS_DRY_RUN: "·",
    STATUS_ROLLED: "✓",
    STATUS_DRAIN_TIMEOUT: "⏱",
    STATUS_PROPAGATE_DEFERRED: "~",
    STATUS_PROPAGATE_FAILED: "✗",
    STATUS_ERROR: "✗",
    STATUS_ROLL_PENDING: "…",
    STATUS_HOLDING: "⊖",
    STATUS_ARM_DEFERRED: "⏳",
    STATUS_LEDGER_ESCALATED: "‼",
}


def render_record(record: WindowRecord | Mapping[str, Any]) -> list[str]:
    """Human-readable lines for one attempt — what `coord release
    window-history` and the command's own stdout print."""
    data = record.to_dict() if isinstance(record, WindowRecord) else dict(record)
    status = str(data.get("status") or "?")
    mark = _STATUS_MARK.get(status, "?")
    version = data.get("target_version") or "?"
    prefix = "[dry-run] " if data.get("dry_run") else ""
    lines = [
        f"{mark} {prefix}{_stamp(data.get('started_at'))}  v{version}  {status}"
    ]
    if data.get("daemon_host"):
        lines.append(
            f"    daemon host: {data['daemon_host']} "
            f"(was v{data.get('daemon_version') or '?'})"
        )
    # #2889 item 4: "what invoked this?" answerable straight from the
    # journal, no live reproduction required.
    if data.get("invoked_by"):
        lines.append(f"    invoked by: {data['invoked_by']}")
    # #2583: a held run must read as "deliberately holding at N behind",
    # never as a silent no-op indistinguishable from a dead timer.
    if status == STATUS_HOLDING:
        lines.append(
            f"    holding: {data.get('releases_behind')} behind, "
            f"threshold {data.get('min_releases_behind')}"
        )
    if data.get("queue_stopped") is not None:
        lines.append(
            f"    {data.get('queue_timer')}: stopped="
            f"{data['queue_stopped']} ({data.get('queue_stop_detail') or '-'})"
        )
    if data.get("drained") is not None:
        secs = data.get("drain_seconds")
        verdict = "clean" if data["drained"] else "TIMED OUT"
        suffix = f" after {secs:.0f}s" if secs is not None else ""
        lines.append(f"    drain: {verdict}{suffix}")
        if data.get("drain_detail"):
            lines.append(f"      {data['drain_detail']}")
    if data.get("propagate_status"):
        joined = (
            f" [history @ {_stamp(data['propagate_started_at'])}]"
            if data.get("propagate_started_at")
            else ""
        )
        lines.append(
            f"    propagate: {data['propagate_status']} "
            f"(exit {data.get('propagate_exit_code')}){joined}"
        )
    if data.get("queue_restarted") is not None:
        lines.append(
            f"    {data.get('queue_timer')}: restarted="
            f"{data['queue_restarted']} ({data.get('queue_restart_detail') or '-'})"
        )
    if data.get("error"):
        lines.append(f"    error: {data['error']}")
    return lines
