"""``coord drive-queue`` — the queue CLI and the tick processor (#1754, DQ-2).

The thin I/O shell around :mod:`coord.drive_queue`.  Everything that *decides*
lives there (``plan_tick``); everything that *touches the world* lives here:
the flock, the board fetch, the DQ-1 state accessors, the ``coord drive
--tmux`` subprocess, the escalation write.  Same split, for the same reason, as
``coord/drive.py`` (pure ``decide``) and ``coord/commands/drive.py`` (thin
Click wrapper).

WHY A SEPARATE COMMAND GROUP.  ``drive`` itself spends its argument positions
on ``REPO ISSUE``, which is why its ``--tmux`` companions are flat
(``drive-sessions``/``drive-attach``/``drive-stop``).  The queue has a real
verb set of its own (add/list/remove/move/status/tick), so it gets a group —
``coord drive-queue <verb>`` — rather than six more hyphenated top-level
commands.

TWO POSTURES WORTH KEEPING WHEN EDITING THIS FILE:

* **Fail closed.**  An unreadable board aborts the tick without launching
  anything.  A transient GitHub/daemon error must never read as "nothing is
  running" — that reads as free capacity and stacks drives on live work.
* **Launch out of process.**  ``coord drive --tmux`` is a subprocess, never an
  inline ``Driver.run()``.  A drive runs 60–90 minutes; an inline one under a
  ``Type=oneshot`` timer would hold the unit for hours, and the tick would
  stop being a tick.  ``--tmux`` already waits for a live session writing its
  run log before exiting 0 (#1606), so a non-zero exit here is a genuinely
  failed attempt, not an unknown.
"""

from __future__ import annotations

import json as _json
import logging
import socket
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

import click

from coord.block_log import INTERVENTION_CATEGORIES, STALL_STATES
from coord.commands._common import _CONFIG_OPTION, apply_pipeline_track_labels_best_effort
from coord.drive_state import WORK_LIKE
from coord.drive_queue import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_PARALLEL_PER_REPO,
    HOLD_RELEASED,
    HOLD_SCOPE_ENTRY,
    HOLD_SCOPE_FLEET,
    MAX_BLOCKED_RESUMES,
    QUEUE_ALERT_ISSUE,
    QUEUE_ALERT_REPO,
    QUEUE_ALERT_STAGE,
    RESUME_PROBE_TIMEOUT_SECONDS,
    ROLL_LEDGER_CUMULATIVE_BOUND_SECONDS,
    ROLL_PENDING_DEFAULT_MAX_DEFERRALS,
    ROLL_PENDING_DEFAULT_TTL_SECONDS,
    STATE_BLOCKED,
    STATE_DONE,
    STATE_FAILED,
    STATE_PARKED,
    STATE_RUNNING,
    STATE_WAITING,
    TERMINAL_QUEUE_STATES,
    BoardView,
    ProbeResult,
    QueueEntry,
    QueueError,
    RollLedger,
    RollPending,
    TickPlan,
    add_preflight_notice,
    build_board_view,
    detect_unreachable_waits,
    diagnose_blocked_after,
    effective_max_fix_rounds,
    entries_from_rows,
    entry_key,
    find_cycle,
    fired_holds,
    is_dispatch_failure_reason,
    is_merge_gate_block_reason,
    is_permanent_block_reason,
    is_pre_dispatch_block_reason,
    is_unsatisfiable_prereq_reason,
    merge_gate_remedy_command,
    merge_plan_inspect_command,
    parse_after_spec,
    parse_key,
    pending_probe_targets,
    plan_tick,
    render_plan,
    unreachable_wait_alert,
    validate_enqueue,
)
from coord.overlap_predict import (
    AUDIT_CATEGORY,
    EVENT_PREDICTED,
    EVENT_SCORED,
    OUTCOME_UNKNOWN,
    SOURCE_DECLARED,
    Prediction,
    classify_outcome,
    collect_candidate_files,
    declared_footprints,
    fanout_warnings,
    inflight_footprints,
    parse_declared_files,
    predict_overlap,
    predictions_from_audit,
    tally,
)

if TYPE_CHECKING:  # pragma: no cover — typing only
    from coord.config import Config

log = logging.getLogger(__name__)

# Wall-clock ceiling for the `coord drive --tmux` launch subprocess.  The
# launch itself only blocks for #1606's liveness verification (16 × 0.5s) plus
# interpreter startup; this is a backstop against a wedged tmux server, not a
# budget.
_LAUNCH_TIMEOUT_SECONDS = 120.0

# Wall-clock ceiling for the tick's own `coord merge --only` attempt
# (#2350's fast path, `_run_merge_only_candidates`).  Deliberately its OWN
# constant, not a reuse of `_LAUNCH_TIMEOUT_SECONDS` above: that one is
# scoped to a `coord drive --tmux` launch, which only ever blocks on #1606's
# bounded liveness check, so 120s is already generous there.  `coord merge
# --only` is a different animal — elsewhere in the codebase
# (`Driver._spawn` in `coord/drive.py`) the SAME subcommand runs under NO
# subprocess timeout at all, nested inside up to a 1800s merge-lock
# acquisition, i.e. the existing precedent treats its runtime as
# unbounded/long, not launch-fast.  This call site does still want a
# backstop — unlike `_spawn`'s fire-and-forget, this one runs synchronously
# inside `drive_queue_tick`, in a loop over every `merge_only` candidate
# this tick found (typically 0-2, see `_fetch_merge_only_ready`, but not
# hard-capped), and a wedge here stalls the whole tick — so it gets a wider
# ceiling than the launch check instead of no ceiling at all, without going
# fully unbounded: `coord-drive-queue.service` (the deployed timer unit)
# gives the ENTIRE `coord drive-queue tick` invocation only 300s
# (`TimeoutStartSec`) before systemd kills it outright, which is a harder
# and less graceful stop than this subprocess's own SIGKILL. 240s leaves
# room for one slow `gh pr merge`/git round trip on a bad network day
# without letting a single candidate alone consume the whole tick's outer
# budget (this PR's invocation never passes `--revalidate`, so
# `wait_for_ci_settle`'s up to 360s doesn't even apply today, but the argv
# could grow that flag later — revisit this ceiling together with
# `coord-drive-queue.service`'s `TimeoutStartSec` if it does).
_MERGE_ONLY_TIMEOUT_SECONDS = 240.0

# Counts are rendered in pipeline order, not alphabetically, so
# `coord drive-queue status` reads as "1 running · 1 waiting". `parked`
# (#1891) sits between `waiting` and `blocked` — closer to "nothing wrong"
# than to "needs a human", but distinct from both, which is the entire point
# of the state: a held queue must not look like an idle one, see
# `coord.drive_queue.STATE_PARKED`'s docstring.
_STATE_ORDER = (
    STATE_RUNNING, STATE_WAITING, STATE_PARKED, STATE_BLOCKED, "done", "failed",
)


_GROUP_HELP = """The operator-declared `coord drive` work queue (#1750).

`coord drive` drives ONE issue; nothing decided what to drive next, so an
overnight batch was a bash loop that (twice) launched on top of live work.
This is the durable, board-backed replacement: declare the order once, then
let `tick` launch at most one drive per run, first-eligible-wins, never past
the concurrency ceiling.

Run `tick` from a systemd timer (DQ-4) or by hand, on any machine that can
reach the board daemon — the board itself is fleet-global. Liveness of a
RUNNING entry is not: it is always a local `tmux` read, so a tick only ever
confirms a session it launched itself. A tick run on a different machine than
the one that launched an entry reads that entry as UNKNOWN, not dead, and
leaves it alone rather than reaping a healthy drive out from under another
host (#1870).
"""


@click.group("drive-queue", help=_GROUP_HELP)
def drive_queue_group() -> None:
    pass


# ── add ──────────────────────────────────────────────────────────────────────


@drive_queue_group.command("add")
@click.argument("repo")
@click.argument("issue", type=int)
@click.option("--machine", default="", help="Pin the drive to one machine (default: let `coord drive` route it).")
@click.option(
    "--after",
    "after_specs",
    multiple=True,
    default=(),
    help=(
        "Pre-req issues that must land first. `N` or `REPO#N`, comma-separated, "
        "repeatable. Bare numbers resolve against REPO."
    ),
)
@click.option(
    "--position",
    type=int,
    default=None,
    help="Insert at this 0-based slot instead of appending at the tail.",
)
@click.option(
    "--hold-after",
    is_flag=True,
    default=False,
    help=(
        "Deploy gate: when this entry completes, hold the queue — launch "
        "NOTHING until a human deploys and runs `drive-queue resume` (or "
        "`--resume-when` starts passing). `merged` is not `live`."
    ),
)
@click.option(
    "--hold-reason",
    default="",
    help="What the operator must do while the gate is held. Shown in the alert.",
)
@click.option(
    "--resume-when",
    default="",
    help=(
        "Optional shell probe re-run each tick while the gate is held; exit 0 "
        f"auto-releases it. Killed at {RESUME_PROBE_TIMEOUT_SECONDS:.0f}s and "
        "treated as a failure. Requires --hold-after."
    ),
)
@click.option(
    "--no-predict-overlap",
    is_flag=True,
    default=False,
    help=(
        "Skip #2247's predicted file-overlap ordering for this add — queue it "
        "exactly where the flags say, even if its declared files collide with "
        "work already in flight."
    ),
)
@click.option(
    "--reject-after",
    "reject_after_specs",
    multiple=True,
    default=(),
    help=(
        "#2603: predicted #2247 edges to NOT auto-apply, even though this "
        "add's own output says they were found. `N` or `REPO#N`, "
        "comma-separated, repeatable. Narrower than --no-predict-overlap, "
        "which drops EVERY prediction for this add — this drops only the "
        "edge(s) named, e.g. one an operator has independently confirmed is "
        "stale (a squash-merged branch, a since-edited declaration), while "
        "still applying anything else #2247 found."
    ),
)
@click.option(
    "--scope",
    "hold_scope",
    type=click.Choice([HOLD_SCOPE_ENTRY, HOLD_SCOPE_FLEET]),
    default=HOLD_SCOPE_ENTRY,
    show_default=True,
    help=(
        "How far a fired gate reaches (#2186). `entry` holds only entries "
        "whose own --after names THIS one. `fleet` is the whole-queue stop — "
        "launch NOTHING anywhere, for the rare case (a rename, a schema "
        "migration) where that is really what's needed. Requires --hold-after."
    ),
)
@click.option(
    "--max-fix-rounds",
    "max_fix_rounds",
    type=int,
    default=None,
    help=(
        "#2604: override the `coord drive --tmux --max-fix-rounds` THIS "
        "entry's tick-launched drive gets. Omit to use "
        "pipeline.max_fix_rounds (or, absent that, "
        "coord.drive_queue.DEFAULT_TICK_MAX_FIX_ROUNDS — deliberately lower "
        "than interactive `coord drive`'s own default of 3, since an "
        "unattended fix round that goes nowhere costs a queue slot for "
        "hours, not a human a few minutes of noticing). Re-adding an "
        "already-queued entry WITHOUT this flag reverts it to the fleet "
        "default — it does not leave a previous override in place."
    ),
)
@click.option(
    "--no-acceptance",
    "no_acceptance",
    is_flag=True,
    default=False,
    help=(
        "#2589: per-entry passthrough of `coord drive --no-acceptance` — "
        "skip #1453's oracle-loop JIT slice authoring for THIS entry's "
        "tick-launched drive (use when the issue's own deliverable has no "
        "user-visible behaviour for a slice to exercise, e.g. a config "
        "schema change — see the #2531 incident). Following that advice by "
        "running `coord drive` directly instead bypasses the queue's own "
        "--max-parallel-per-repo ceiling; this flag is how to follow it "
        "THROUGH the queue instead. Re-adding an already-queued entry "
        "WITHOUT this flag reverts it to the ordinary oracle-loop path — it "
        "does not leave a previous passthrough in place."
    ),
)
@_CONFIG_OPTION
def drive_queue_add(
    repo: str,
    issue: int,
    machine: str,
    after_specs: tuple[str, ...],
    position: int | None,
    hold_after: bool,
    hold_reason: str,
    resume_when: str,
    no_predict_overlap: bool,
    reject_after_specs: tuple[str, ...],
    hold_scope: str,
    max_fix_rounds: int | None,
    no_acceptance: bool,
    config_path: Path,
) -> None:
    """Queue REPO ISSUE for `coord drive`, or update it if already queued.

    Validation happens BEFORE the write, the same posture `coord milestone
    write-order` takes for `## Work order`: a self-edge or a dependency cycle
    exits non-zero and leaves the queue exactly as it was.

    #2247: when this issue's DECLARED files (a `## Files` block in its body)
    collide with work already in flight in the same repo, the entry is chained
    `--after` that work automatically and the reason is recorded. That is an
    ORDER change, never a refusal — see `coord.overlap_predict`.

    #2603: the printed reason names each edge's PROVENANCE, not just its
    conclusion — the compared branch and head sha, whether #2602's liveness
    check actually confirmed the branch was still open, and how old a
    `[declared]` edge's cached body was — because a stale prediction and a
    correct one render identically without it. `--reject-after` drops one
    named edge without disabling the whole feature (`--no-predict-overlap`
    still does that, for everything at once).
    """
    from coord.state import enqueue_drive_queue, list_drive_queue  # noqa: PLC0415

    existing_entries = entries_from_rows(list_drive_queue())
    if max_fix_rounds is not None and max_fix_rounds < 1:
        raise click.ClickException("--max-fix-rounds must be a positive integer")
    # #2839: reused below for the Pipeline label write's slug resolution, so
    # that best-effort projection costs no SECOND config load (a `GET /config`
    # round trip to the board daemon on a thin client). `None` on the
    # documented fail-open path — an unloadable config never blocks an
    # enqueue, and never blocks the label write either.
    cfg: Config | None = None
    try:
        after = parse_after_spec(after_specs, repo)
        # #2603: reuses `--after`'s own `N` / `REPO#N` parsing — a rejected
        # edge is named the same way an operator would name a real one.
        reject_after = set(parse_after_spec(reject_after_specs, repo))
        cfg = validate_config_repo(config_path, repo)
        validate_hold_flags(hold_after, hold_reason, resume_when, hold_scope)
        validate_enqueue(existing_entries, repo, issue, after)
    except QueueError as exc:
        raise click.ClickException(str(exc)) from None

    # #2186: the UPDATE path in `_enqueue_drive_queue_local` always writes
    # whatever `hold_scope` THIS call passed (default `entry`) — so re-adding
    # an already fleet-scoped gate without repeating `--scope fleet` silently
    # narrows it. That fails toward the safer/narrower scope, not a
    # correctness bug, but it is a real behaviour change an operator who only
    # meant to touch `--machine`/`--after` would not expect, so it is echoed
    # rather than left silent.
    previous = next((e for e in existing_entries if e.key == entry_key(repo, issue)), None)
    scope_downgrade_warning = ""
    if (
        previous is not None
        and previous.hold_scope == HOLD_SCOPE_FLEET
        and hold_after
        and hold_scope != HOLD_SCOPE_FLEET
    ):
        scope_downgrade_warning = (
            f"\nwarning: {entry_key(repo, issue)} was scope=fleet — this add did not "
            f"repeat --scope {HOLD_SCOPE_FLEET}, so its gate is now scope={hold_scope} "
            "(entries-only). Pass --scope fleet again if the fleet-wide stop was still needed."
        )

    # #2247: predicted file overlap ORDERS, never refuses. Anything that goes
    # wrong in here (unreadable body, unreachable board, a failed compare)
    # yields an empty prediction and this add behaves exactly as it did before
    # the feature existed.
    prediction = Prediction()
    staleness_note = ""
    auto_after: list[str] = []
    rejected_after: list[str] = []
    if not no_predict_overlap:
        prediction, staleness_note = _predict_overlap(
            config_path, repo, issue, existing_entries
        )
        candidate_after = _applicable_auto_after(
            existing_entries, repo, issue, after, prediction
        )
        # #2603: --reject-after is the narrower escape hatch — it drops only
        # the named edge(s) from what actually gets applied, never the whole
        # prediction (that's what --no-predict-overlap is for).
        auto_after = [k for k in candidate_after if k not in reject_after]
        rejected_after = [k for k in candidate_after if k in reject_after]
        after = [*after, *auto_after]

    enqueue_drive_queue(
        repo,
        issue,
        machine=machine or None,
        after=after,
        position=position,
        hold_after=hold_after,
        hold_reason=hold_reason,
        resume_when=resume_when,
        hold_scope=hold_scope,
        max_fix_rounds=max_fix_rounds,
        no_acceptance=no_acceptance,
    )
    # #2839: queueing a drive is a strictly STRONGER statement than "send to
    # Pipeline" (`coord track`), so it must never leave the issue in a
    # weaker label state — apply the same `coord` + `status:ready` labels
    # `track` does, resolved through the SAME `cfg` already loaded above
    # (coordinator.yml's `name:` is routinely not the GitHub slug, so the
    # forge needs the resolved `github:` value, not `repo`). Best-effort/
    # non-blocking (a GitHub outage warns and carries on; the board row above
    # is already written and is the
    # source of truth for queue membership) and skipped for an entry that is
    # already `running` — #2821 was mid-drive with `coord` but no
    # `status:*`, and re-adding `status:ready` to a running card would
    # misrepresent it as not-yet-started. This is enqueue-time ONLY: never
    # mirrored onto `coord drive-queue remove`, which must not untrack.
    if previous is None or previous.state != STATE_RUNNING:
        apply_pipeline_track_labels_best_effort(repo, issue, config=cfg)
    if auto_after:
        _record_overlap_prediction(repo, issue, prediction, auto_after)
    suffix = f" after {', '.join(after)}" if after else ""
    pinned = f" on {machine}" if machine else ""
    fix_rounds_note = f" · max-fix-rounds={max_fix_rounds}" if max_fix_rounds else ""
    no_acceptance_note = " · --no-acceptance" if no_acceptance else ""
    gate = ""
    if hold_after:
        gate = " · holds the queue when done"
        if hold_scope == HOLD_SCOPE_FLEET:
            gate += " (fleet-wide — nothing anywhere launches)"
        if resume_when:
            gate += f" (auto-resume when `{resume_when}` passes)"
    # #2601: the reason (if an edge was actually applied), any high-fanout
    # directory-token warning (independent of whether the edge stuck — a
    # cycle-dropped edge's token is just as worth narrowing), and a staleness
    # note when the candidate's own body could only be read from cache.
    # #2603: plus, when known, how old each APPLIED `[declared]` edge's OTHER
    # side is, and confirmation of anything --reject-after dropped — shown
    # even if that leaves nothing else to report, since a rejection an
    # operator asked for and got no acknowledgement of looks identical to one
    # that silently didn't take.
    overlap_notes: list[str] = []
    if auto_after:
        overlap_notes.append(prediction.reason)
        overlap_notes.extend(_declared_overlap_age_notes(prediction, auto_after))
    if rejected_after:
        overlap_notes.append(
            "rejected via --reject-after (not applied): " + ", ".join(rejected_after)
        )
    overlap_notes.extend(fanout_warnings(prediction))
    if staleness_note:
        overlap_notes.append(staleness_note)
    overlap_note = ("\n" + "\n".join(overlap_notes)) if overlap_notes else ""
    click.echo(
        f"queued {entry_key(repo, issue)}{pinned}{suffix}{gate}{fix_rounds_note}"
        f"{no_acceptance_note}{scope_downgrade_warning}{overlap_note}"
    )

    # #2339: say out loud when this add cannot possibly accomplish anything —
    # a terminal ADVISORY work row that only `coord retry` clears, and/or an
    # upsert onto a `blocked`/`failed` entry whose run state `enqueue`
    # deliberately leaves alone. Emitted AFTER the `queued ...` line and never
    # fatal: the write already happened, and #2247's posture (order/advise,
    # never refuse) applies here for the same reason.
    aid, status, work_machine = _latest_work_assignment(repo, issue)
    notice = add_preflight_notice(
        repo,
        issue,
        previous,
        work_aid=aid,
        work_status=status,
        work_machine=work_machine,
    )
    if notice:
        click.echo(notice)


def _latest_work_assignment(repo: str, issue: int) -> tuple[str, str, str]:
    """``(assignment_id, status, machine)`` of *repo*#*issue*'s newest
    work-like row, or ``("", "", "")`` (#2339).

    Fail-open at every layer, exactly like :func:`_issue_body` above: `add`
    is an interactive command whose whole job is the write it already did, so
    an unreachable daemon, a missing board, or a shape this version does not
    recognise must degrade to "no preflight notice", never an error.

    Reads the same ``BoardFetcher`` projection `tick` uses (daemon HTTP when
    a `board_service` is configured, the local DB otherwise) and applies
    ``coord.drive_state``'s own "latest by ``dispatched_at``, restricted to
    ``WORK_LIKE``" rule — the same row `coord drive`'s `_decide_advisory`
    branches on, so the two cannot disagree about which assignment is
    current. Deliberately NO GitHub leg: confirming a *genuine* zero-commit
    advisory is `coord retry`'s job, and it does that itself.
    """
    try:
        from coord.drive_state import BoardFetcher  # noqa: PLC0415

        payload = BoardFetcher().fetch()
        rows = [
            a
            for a in (payload.get("assignments") or [])
            if a.get("repo_name") == repo
            and a.get("issue_number") == issue
            and a.get("type") in WORK_LIKE
        ]
        if not rows:
            return ("", "", "")
        latest = max(rows, key=lambda r: r.get("dispatched_at") or 0.0)
        return (
            str(latest.get("assignment_id") or ""),
            str(latest.get("status") or ""),
            str(latest.get("machine_name") or ""),
        )
    except Exception:  # noqa: BLE001 — see docstring
        return ("", "", "")


# ── #2247: predicted file-overlap ordering ───────────────────────────────────


def _issue_body(repo_name: str, issue_number: int) -> str:
    """This issue's body from the coordinator's OWN issue store, or ``""``.

    Daemon (a thin client's canonical copy) first, then the local ``issues``
    cache. Deliberately NO GitHub leg: `add` is an interactive command, the
    overwhelmingly common case is an issue with no `## Files` block at all,
    and putting a live `gh` round-trip on every one of those to learn nothing
    is a bad trade against a feature whose whole design premise is that no
    prediction is a fine answer. An issue the board has never synced simply
    gets today's behaviour — and #2246's post-merge sweep is what catches the
    collisions prediction misses.

    Fail-open at every layer — a body we cannot read means no prediction.
    """
    try:
        from coord.client import fetch_issue, resolve_board_service  # noqa: PLC0415

        svc = resolve_board_service()
        if svc is not None:
            row = fetch_issue(svc, repo_name, int(issue_number))
            if row is not None:
                return str(row.get("body") or "")
    except Exception:  # noqa: BLE001 — see docstring
        pass
    try:
        from coord import sql  # noqa: PLC0415
        from coord.db import get_connection  # noqa: PLC0415

        row = sql.execute(
            get_connection(),
            "SELECT body FROM issues WHERE repo_name = ? AND number = ?",
            (repo_name, int(issue_number)),
        ).fetchone()
        return "" if row is None else str(row["body"] or "")
    except Exception:  # noqa: BLE001 — see docstring
        return ""


def _live_issue_body(repo_github: str, issue_number: int) -> str | None:
    """A live ``gh issue view`` body for the ONE entry actually being enqueued.

    #2601: `_issue_body` above is a cache read by design — cheap, and correct
    for the overwhelmingly common case (no ``## Files`` block at all) and for
    every OTHER issue's declaration this module consults for a footprint. But
    that same design let a `gh issue edit` made directly against the
    tracker — bypassing `coord.state.edit_issue_content`'s cache mirror — go
    unnoticed: the predictor kept citing the removed line forever, because
    nothing had ever told the cache the edit happened.

    Called ONLY for the candidate (see `_candidate_body`), and only when its
    cached body already parses to a declaration — never on the common
    empty-declaration path, so this does not reintroduce the per-``add``
    GitHub round-trip the module was built to avoid.

    Fails open like every other fetch here: ``None`` on any error, and the
    caller falls back to the cached body.
    """
    if not repo_github:
        return None
    try:
        from coord import github_ops  # noqa: PLC0415

        data = github_ops.get_issue(repo_github, int(issue_number))
    except Exception:  # noqa: BLE001 — see docstring
        return None
    if not data:
        return None
    return str(data.get("body") or "")


def _mirror_issue_body(repo_name: str, issue_number: int, body: str) -> None:
    """Write a freshly live-fetched body back into the local cache (#2601).

    Best-effort, mirroring `_edit_issue_content_local`'s own cache write: the
    live fetch above is authoritative, this only makes the freshness outlive
    the one predict call it was fetched for — a later `add` for a DIFFERENT
    issue that compares declaration-to-declaration against this one, or
    `overlap-report`, should see the same answer without a second round-trip.
    """
    try:
        from coord import sql  # noqa: PLC0415
        from coord.db import get_connection  # noqa: PLC0415

        conn = get_connection()
        sql.execute(
            conn,
            "UPDATE issues SET body = ?, synced_at = ? "
            "WHERE repo_name = ? AND number = ?",
            (body, time.time(), repo_name, int(issue_number)),
        )
        conn.commit()
    except Exception:  # noqa: BLE001 — the cache write is advisory
        pass


def _issue_body_synced_at(repo_name: str, issue_number: int) -> float | None:
    """Raw ``issues.synced_at`` for *repo_name*#*issue_number*, or ``None``.

    #2603: shared by `_cached_body_age_note` below (the CANDIDATE's own
    staleness note, on a live-refresh failure) and by `_predict_overlap`'s
    ``synced_at_fetcher`` passthrough to
    :func:`coord.overlap_predict.declared_footprints` (every OTHER `declared`
    footprint's cache age — those are never live-refreshed, so this is the
    only freshness signal available for them; see the module docstring for
    why re-reading them all live is out of scope).
    """
    try:
        from coord import sql  # noqa: PLC0415
        from coord.db import get_connection  # noqa: PLC0415

        row = sql.execute(
            get_connection(),
            "SELECT synced_at FROM issues WHERE repo_name = ? AND number = ?",
            (repo_name, int(issue_number)),
        ).fetchone()
    except Exception:  # noqa: BLE001 — an unreadable age is just unknown
        return None
    if row is None or not row["synced_at"]:
        return None
    return float(row["synced_at"])


def _cached_body_age_note(repo_name: str, issue_number: int) -> str:
    """#2601 point 1's fallback: when a live re-read wasn't possible, say how
    old the cached body actually is, so a stale prediction is at least
    visible instead of silently trusted.
    """
    synced_at = _issue_body_synced_at(repo_name, issue_number)
    if not synced_at:
        return (
            "note: predicted from a cached issue body of unknown age "
            "(live refresh failed) — the declaration may be stale"
        )
    age = _age_str(max(0.0, time.time() - synced_at))
    return (
        f"note: predicted from a cached issue body synced {age} ago "
        "(live refresh failed) — the declaration may be stale"
    )


def _declared_overlap_age_notes(prediction: Prediction, applied: list[str]) -> list[str]:
    """#2603: how old each APPLIED ``[declared]`` overlap's OTHER side is.

    `Overlap.describe()` cannot render this itself — it is a pure module
    with no wall clock (see its docstring) — so this is the CLI-layer half:
    for every ``declared`` overlap whose cache age we know (``synced_at`` was
    set by `_predict_overlap`'s ``synced_at_fetcher``), say how old that read
    was. Silent when unknown, same posture as the rest of this feature: a
    freshness signal we don't have is not manufactured, only shown when real.

    *applied* restricts this to the overlaps that actually landed in the
    entry's ``after=`` (i.e. ``auto_after``) — review of #2603's first
    iteration flagged that walking every predicted overlap regardless would
    surface a cache-age note describing an edge a cycle guard or
    ``--reject-after`` had just dropped, which the operator never sees
    applied and has no other cue was excluded.
    """
    notes: list[str] = []
    now = time.time()
    for overlap in prediction.overlaps:
        if overlap.key not in applied:
            continue
        if overlap.source != SOURCE_DECLARED or not overlap.synced_at:
            continue
        age = _age_str(max(0.0, now - overlap.synced_at))
        notes.append(
            f"note: {overlap.key}'s declared list was read from a cache "
            f"synced {age} ago"
        )
    return notes


def _candidate_body(
    repo_name: str, issue_number: int, repo_github: str,
) -> tuple[str, str]:
    """This entry's OWN body — refreshed live when staleness could matter (#2601).

    The natural correction to a bad prediction is editing the issue's ``##
    Files`` block and re-adding. That did nothing (#2601) because the
    predictor always read `_issue_body`'s cache, which a direct
    ``gh issue edit`` never invalidates.

    So the candidate gets one extra check the rest of the module does not:
    if its cached body already parses to a declaration, re-read it live and
    mirror the answer back into the cache. A body with NO cached declaration
    skips the live round-trip entirely — there is nothing to have gone
    stale, and a declaration ADDED since the last sync just falls back to
    today's "no prediction" (rule 3), exactly as an unreadable body would.

    Returns ``(body, staleness_note)``: *body* is what prediction runs
    against; *staleness_note* is non-empty only when a live re-read was
    warranted but failed.
    """
    cached = _issue_body(repo_name, issue_number)
    if not parse_declared_files(cached):
        return cached, ""
    fresh = _live_issue_body(repo_github, issue_number)
    if fresh is None:
        return cached, _cached_body_age_note(repo_name, issue_number)
    if fresh != cached:
        _mirror_issue_body(repo_name, issue_number, fresh)
    return fresh, ""


def _repo_coordinates(config_path: Path, repo: str) -> tuple[str, str] | None:
    """``(github slug, default branch)`` for *repo*, or ``None``."""
    try:
        from coord.commands._common import _load_config  # noqa: PLC0415

        repo_cfg = _load_config(config_path).repo(repo)
    except (Exception, SystemExit):  # noqa: BLE001 — a config that won't load
        # means no prediction. `SystemExit` is listed for the same reason as in
        # `validate_config_repo` (#2839 review): `_load_config` reports a
        # `ConfigError` as `click.echo` + `sys.exit(2)`, a `BaseException`, so
        # `except Exception` alone would abort `drive-queue add` here instead
        # of degrading to "no overlap prediction" as documented.
        return None
    if repo_cfg is None:
        return None
    return str(repo_cfg.github or ""), str(getattr(repo_cfg, "default_branch", "") or "main")


def _predict_overlap(
    config_path: Path, repo: str, issue: int, existing_entries: list[QueueEntry],
) -> tuple[Prediction, str]:
    """Compare this issue's declared files against work already in flight.

    Same-repo only: two repos' paths cannot collide, and comparing them would
    manufacture overlaps out of a shared filename. In-flight branches are
    checked first (ground truth); a queued entry with no branch yet is
    compared declaration-to-declaration, and only when it has one.

    Returns ``(prediction, staleness_note)`` — see `_candidate_body` for when
    the note is non-empty. Every OTHER body this consults (an in-flight
    branch's own declaration, an unrelated queued entry's) still comes from
    the plain cache: re-reading fifteen bodies live on every `add` is exactly
    the cost the module's docstring rejects, and #2601's own report is about
    correcting THIS entry's declaration, not anyone else's.
    """
    coordinates = _repo_coordinates(config_path, repo)
    if coordinates is None:
        return Prediction(), ""
    repo_github, base_branch = coordinates

    candidate_body, staleness_note = _candidate_body(repo, issue, repo_github)

    def body_fetcher(repo_name: str, number: int) -> str:
        if repo_name == repo and number == issue:
            return candidate_body
        return _issue_body(repo_name, number)

    candidate = collect_candidate_files(repo, issue, body_fetcher)
    if not candidate:
        # Rule 3: no prediction is a valid answer. Nothing is fetched, nothing
        # is compared, and the add is byte-identical to the pre-#2247 one.
        return Prediction(), ""

    key = entry_key(repo, issue)
    footprints = inflight_footprints(
        repo, repo_github, base_branch, exclude_issue_number=issue,
    )
    covered = {key, *(f.key for f in footprints)}
    queued = [
        (e.repo, e.issue)
        for e in existing_entries
        if e.repo == repo
        and e.key not in covered
        and e.state not in TERMINAL_QUEUE_STATES
    ]
    footprints.extend(
        declared_footprints(
            queued,
            body_fetcher,
            exclude_keys=covered,
            synced_at_fetcher=_issue_body_synced_at,
        )
    )
    return predict_overlap(candidate, footprints, exclude_keys={key}), staleness_note


def _applicable_auto_after(
    existing_entries: list[QueueEntry],
    repo: str,
    issue: int,
    after: list[str],
    prediction: Prediction,
) -> list[str]:
    """The predicted pre-reqs that are actually safe to add.

    An INFERRED edge must never be able to fail an add the operator's own
    flags would have allowed, so each one is validated on its own and simply
    dropped if it would self-edge or close a cycle — the opposite posture to
    `validate_enqueue`'s treatment of an operator-declared `--after`, which is
    a typo worth reporting.
    """
    applied: list[str] = []
    for candidate_key in prediction.after_keys:
        if candidate_key in after or candidate_key in applied:
            continue
        try:
            validate_enqueue(
                existing_entries, repo, issue, [*after, *applied, candidate_key]
            )
        except QueueError:
            continue
        applied.append(candidate_key)
    return applied


def _record_overlap_prediction(
    repo: str, issue: int, prediction: Prediction, applied: list[str],
) -> None:
    """Persist WHY this entry was ordered — on the row and in the audit log.

    Two sinks, deliberately. `last_reason` is what an operator reading `coord
    drive-queue list` sees immediately, but the tick owns that column and will
    overwrite it on the entry's first attempt. The audit row is the durable
    one, and it carries both sides' file lists so the claim can be scored
    later (`coord drive-queue overlap-report`) — without that, nobody can tell
    a working predictor from a lucky one.
    """
    details = prediction.audit_details()
    details["applied_after"] = list(applied)
    try:
        from coord.audit import record_audit  # noqa: PLC0415

        record_audit(
            tier="business",
            category=AUDIT_CATEGORY,
            event_type=EVENT_PREDICTED,
            actor="drive-queue",
            summary=prediction.reason,
            repo=repo,
            issue=issue,
            details=details,
        )
    except Exception:  # noqa: BLE001 — recording must never fail the enqueue
        pass
    try:
        from coord.state import update_drive_queue_entry  # noqa: PLC0415

        update_drive_queue_entry(repo, issue, last_reason=prediction.reason)
    except Exception:  # noqa: BLE001 — same
        pass


def validate_hold_flags(
    hold_after: bool,
    hold_reason: str,
    resume_when: str,
    hold_scope: str = HOLD_SCOPE_ENTRY,
) -> None:
    """Refuse gate detail without a gate (#1757, extended by #2186).

    `--resume-when` / `--hold-reason` / a non-default `--scope` on an entry
    with no `--hold-after` would be stored and then never read — a silent
    no-op on the ONE flag whose whole job is to stop the queue (or narrow
    what it stops).  An operator who mistyped that has no signal at all that
    overnight sequencing will now blow straight through the deploy step, so
    this is a usage error, not a warning.
    """
    if hold_after:
        return
    offenders = [
        flag
        for flag, value in (
            ("--resume-when", resume_when),
            ("--hold-reason", hold_reason),
        )
        if value
    ]
    if hold_scope == HOLD_SCOPE_FLEET:
        offenders.append("--scope=fleet")
    if offenders:
        raise QueueError(
            f"{' and '.join(offenders)} require --hold-after "
            "(without it there is no gate to resume, explain, or scope)"
        )


def validate_config_repo(config_path: Path, repo: str) -> Config | None:
    """Refuse a repo coordinator.yml has never heard of.

    `coord drive <repo> <issue>` would fail at preflight anyway
    (``DriveStateError: repo ... is not in coordinator.yml``) — catching it at
    ``add`` time turns a mysterious tick-time block into an immediate typo
    report.  Fail-OPEN on a config that won't load at all: a thin client whose
    config cache is momentarily unreadable must still be able to queue work.

    Returns the loaded ``Config`` (``None`` when it could not be loaded, i.e.
    the fail-open path) so the caller does not have to load it a SECOND time
    — on a thin client each load is a `GET /config` round trip to the board
    daemon, and #2839's best-effort label write needs the same config only to
    resolve `repo`'s GitHub slug.

    ``SystemExit`` is caught alongside ``Exception`` deliberately (#2839
    review): ``_load_config`` reports a ``ConfigError`` as ``click.echo`` +
    ``sys.exit(2)``, which is a ``BaseException`` — so a bare ``except
    Exception`` here would NOT fail open at all, it would abort `add` with
    exit code 2, the exact opposite of this function's documented posture.
    """
    try:
        from coord.commands._common import _load_config  # noqa: PLC0415

        config = _load_config(config_path)
    except (Exception, SystemExit):  # noqa: BLE001 — see the fail-open note above
        return None
    if config.repo(repo) is None:
        known = ", ".join(sorted(r.name for r in config.repos)) or "(none)"
        raise QueueError(f"repo {repo!r} is not in coordinator.yml (known: {known})")
    return config


# ── list ─────────────────────────────────────────────────────────────────────

# `blocked`/`failed` are the two states #2183's re-diagnosis applies to: both
# are in `TERMINAL_QUEUE_STATES` (the tick will never look at this row's
# `after=` graph again), so both are exactly where a stale `after=` reading
# can survive indefinitely. `done` is also terminal but carries no live
# "why is this stuck" question — nothing to re-diagnose.
_DIAGNOSABLE_STATES = (STATE_BLOCKED, STATE_FAILED)

# How much of a (possibly multi-line, possibly paragraph-length) `last_reason`
# fits on the summary row itself, next to `attempts=`/`deferrals=`, without
# wrapping a normal terminal. The FULL text always still prints on the `last:`
# continuation line right below — this is only the on-row teaser.
_ROW_CAUSE_MAX_CHARS = 88

_BLOCKED_REMEDY = (
    "remedy: {state}'s `after=` graph above is never re-checked on its own "
    "— `coord drive-queue remove` + `add` only helps once the cause is "
    "actually fixed, not merely because a pre-req merged"
)

# #2362: a `blocked` row (never `failed` — the tick's `after=` resume sweep,
# `coord.drive_queue._reconcile_blocked_after`, is scoped to `blocked` only)
# whose OWN `last_reason` IS `_resolve_prereqs`'s "queued but blocked/failed
# — it will never satisfy" verdict is auto re-checked every tick, and
# resumes to `waiting` on its own the moment every named pre-req lands.
# `_BLOCKED_REMEDY` above is flatly wrong for exactly this row shape (it is
# in fact re-checked, on its own, without an operator) — this note replaces
# it there instead of merely appending, mirroring #2230's own concern: an
# operator told "never re-checked" for a row about to self-resume would
# either do a needless remove+add or race the automatic one.
_BLOCKED_AFTER_NOTE = (
    "remedy: {state}'s `after=` graph above IS re-checked automatically "
    "every tick (#2362) — once every named pre-req lands (`facts.landed`), "
    "this row resumes to `waiting` on its own, attempt budget reset; no "
    "operator remove+add needed. See `resumes=` above if it has already "
    "self-resumed and re-blocked for an unrelated reason."
)

# #2230: `blocked`'s MERGE GATE (as opposed to its `after=` graph, handled by
# `_BLOCKED_AFTER_NOTE`/`_BLOCKED_REMEDY` above) is no longer unconditionally
# terminal — see `coord.drive_queue.is_permanent_block_reason`/
# `_reconcile_blocked`. Shown only for `blocked` (never `failed`, which
# #2230 does not touch at all) so an operator reading `list` right after this
# ships doesn't have to go read the issue to learn their remove+add might be
# about to race an automatic resume.
_BLOCKED_GATE_NOTE = (
    "note: a re-evaluable blocked cause (i.e. not a #1844/#2019 permanent "
    "refusal) IS re-checked against the merge gate automatically (#2230) — "
    "see `resumes=` above if this row has already self-resumed and "
    "re-blocked"
)

# #2589: `_BLOCKED_GATE_NOTE` above is flatly wrong for a row whose cause is
# `coord.drive_queue.is_pre_dispatch_block_reason` — an empty-branch
# DONE/ADVISORY death (#2363) or a "no assignment was ever created" dispatch
# failure (#2273). Neither ever produced a branch or a PR, so there is no
# merge-queue row for #2230's `_blocked_gate_reading` to have ANY opinion
# about — it returns `None` ("no evidence either way") EVERY tick, forever,
# which is exactly the "will never satisfy" shape `_BLOCKED_AFTER_NOTE`
# already names for the `after=`-graph case. The claude-coordinator#2531
# incident this closes: the row's OWN reason recommended `coord retry` or
# `coord drive --no-acceptance` while this note, right below it, told the
# operator #2230 would clear it on its own — the opposite of true. Two
# distinct sentences (never "a live gate, confirmed clear" — this predicate
# fires when there never WAS a gate) so `attempts=N/N` reads as the headline
# it is, not a suffix the operator has to go hunting for.
#
# #2635: `pre_dispatch_terminal` (the row's caller) only reaches these two
# strings once `_fetch_live_dispatch_evidence` has already had its chance to
# override the dispatch-failure shape with positive proof otherwise (a board
# assignment or a remote branch a prior attempt left behind) — so by the
# time either note prints, "no branch/PR was ever created" is either an
# actual live-checked finding or, for the empty-branch shape (deliberately
# not live-re-checked here — see that function's docstring for why it
# doesn't need to be), the drive's own `branch_has_commits` verdict at exit
# time. Worded as "found" rather than "ever created": an honest report of
# what was looked for and not seen, not a metaphysical claim about all of
# history — the same posture #2273's own dispatch-failure note (in
# `coord.drive_queue._reconcile_running`) already takes for the per-run case.
_BLOCKED_TERMINAL_NOTE = (
    "NEEDS OPERATOR — attempts {attempts}/{attempts} exhausted, and this "
    "cause has no merge gate to re-check (#2589): no branch/PR was found "
    "for this issue, so #2230's automatic re-check has no evidence to "
    "act on and never will. `coord drive-queue remove` + `add` only helps "
    "once the underlying cause is actually fixed."
)
_BLOCKED_TERMINAL_NOTE_NO_ATTEMPTS = (
    "NEEDS OPERATOR — this cause has no merge gate to re-check (#2589): no "
    "branch/PR was found for this issue, so #2230's automatic "
    "re-check has no evidence to act on and never will. `coord drive-queue "
    "remove` + `add` only helps once the underlying cause is actually "
    "fixed."
)


def _row_cause(reason: str) -> str:
    """The first line of *reason*, clipped to fit the summary row (#2183)."""
    first_line = reason.strip().splitlines()[0] if reason.strip() else ""
    if len(first_line) > _ROW_CAUSE_MAX_CHARS:
        return first_line[: _ROW_CAUSE_MAX_CHARS - 1].rstrip() + "…"
    return first_line


def _board_for_list_diagnosis() -> BoardView | None:
    """The board, for `list`'s #2183 re-diagnosis — ``None`` when unreachable.

    Unlike `tick`, `list` is a read-only inspection command an operator runs
    often and expects to be fast and always available, so an unreachable
    board must never make it fail or hang the way `tick`'s fail-closed abort
    does. ``None`` here means "skip the re-diagnosis" — every row falls back
    to exactly today's rendering (the `after=` list unfiltered, no cause on
    the row itself), never a crash or a misleading empty board's worth of
    "everything is unsatisfied".
    """
    try:
        return _fetch_board_view()
    except Exception:  # noqa: BLE001 — see docstring: this must never fail `list`
        return None


@drive_queue_group.command("list")
@click.option("--repo", "repo", default=None, help="Restrict to one repo (default: every repo).")
@click.option("--json", "output_json", is_flag=True, default=False, help="Emit the raw rows as JSON.")
@_CONFIG_OPTION
def drive_queue_list(repo: str | None, output_json: bool, config_path: Path) -> None:
    """Show the queue in run order."""
    from coord.state import list_drive_queue  # noqa: PLC0415

    rows = list_drive_queue(repo)
    if output_json:
        click.echo(_json.dumps(rows))
        return
    if not rows:
        click.echo("(drive queue is empty)")
        return
    now = time.time()
    entries = entries_from_rows(rows)

    # #2183: a `blocked`/`failed` row's `after=` graph needs to be re-checked
    # against the FULL queue (cross-repo pre-reqs), not just whatever `--repo`
    # filtered down to — otherwise a `--repo` view would misdiagnose an
    # out-of-repo pre-req as "unknown to the queue" purely from the filter,
    # not from anything actually true.
    all_entries = entries if repo is None else entries_from_rows(list_drive_queue())
    # A row with no `after=` at all has nothing for #2183's re-diagnosis to
    # do — criterion "entries with no after= are unaffected" — so both the
    # board fetch and the per-row diagnosis below are gated on `entry.after`
    # being non-empty, not just on the state being terminal.
    needs_diagnosis = any(
        e.state in _DIAGNOSABLE_STATES and e.after for e in entries
    )
    # #2635: a `blocked` row whose `last_reason` carries #2273's dispatch-
    # failure marker needs the SAME live board read `needs_diagnosis` above
    # already triggers for an `after=` diagnosis — see
    # `_fetch_live_dispatch_evidence`'s docstring for why a per-run reason
    # string alone cannot tell "this entry never dispatched anything, ever"
    # apart from "this LAUNCH dispatched nothing because an earlier attempt's
    # work was still in flight". Scoped tightly (dispatch-failure rows only,
    # never every `blocked` row) so a queue with none of this shape costs
    # nothing extra here, same posture as `needs_diagnosis` itself.
    needs_dispatch_evidence = any(
        e.state == STATE_BLOCKED and is_dispatch_failure_reason(e.last_reason)
        for e in entries
    )
    board = (
        _board_for_list_diagnosis()
        if (needs_diagnosis or needs_dispatch_evidence)
        else None
    )
    dispatch_evidence = (
        _fetch_live_dispatch_evidence(entries, board, config_path)
        if needs_dispatch_evidence
        else {}
    )
    states = {e.key: e.state for e in all_entries}
    cycle_keys: dict[str, str] = {}
    cycle = find_cycle({e.key: list(e.after) for e in all_entries})
    if cycle is not None:
        message = "dependency cycle: " + " -> ".join(cycle)
        for key in cycle:
            cycle_keys[key] = message

    for entry in entries:
        diagnosed = (
            board is not None and entry.state in _DIAGNOSABLE_STATES and bool(entry.after)
        )
        unsatisfied = entry.after
        dependency_reason = ""
        if diagnosed:
            diagnosis = diagnose_blocked_after(entry, board, states, cycle_keys)
            unsatisfied = diagnosis.unsatisfied
            dependency_reason = diagnosis.dependency_reason

        # #2589: a `blocked` row whose cause never produced a branch/PR has
        # NOTHING for #2230's merge-gate sweep to re-check — see
        # `_BLOCKED_TERMINAL_NOTE`'s comment. Computed once per row and
        # consulted below both for the row's own headline (this must read as
        # terminal, not as "just blocked", the moment an operator sees it —
        # `attempts=N/N` is the fact that matters, not a suffix buried after
        # `deferrals=`) and for which remedy note prints.
        #
        # #2635: `dispatch_evidence` overrides the text-only verdict when
        # THIS entry (not just this run) has positive proof otherwise — a
        # board assignment or a remote branch a prior attempt already left
        # behind. See `_fetch_live_dispatch_evidence`'s docstring for why
        # this is scoped to the dispatch-failure shape only, never the
        # empty-branch one.
        pre_dispatch_terminal = entry.state == STATE_BLOCKED and (
            not is_permanent_block_reason(entry.last_reason)
            and is_pre_dispatch_block_reason(entry.last_reason)
            and not dispatch_evidence.get(entry.key, False)
        )

        # A diagnosed row whose block is NOT (or no longer) caused by its
        # `after=` graph gets its own cause on the state token itself — #2183
        # point 2: the terminal reason leads, not a dependency list that may
        # have nothing to do with it. A row that IS dependency-caused (or
        # wasn't diagnosed at all) keeps the plain state token unchanged.
        state_label = entry.state
        if pre_dispatch_terminal:
            state_label = f"{entry.state} [NEEDS OPERATOR]"
        elif diagnosed and not dependency_reason and entry.last_reason:
            state_label = f"{entry.state}: {_row_cause(entry.last_reason)}"

        bits = [f"{entry.position:>2}  {entry.key:<28} {state_label}"]
        if entry.machine:
            bits.append(f"machine={entry.machine}")
        # Suppress `after=` entirely on a diagnosed row that is NOT
        # dependency-caused: every named pre-req is either satisfied or was
        # never the reason this row is stuck, so showing it invites exactly
        # the misreading #2183 exists to stop. Otherwise (not diagnosed, or
        # genuinely dependency-caused) show whatever is still unsatisfied.
        show_after = () if (diagnosed and not dependency_reason) else unsatisfied
        if show_after:
            bits.append(f"after={','.join(show_after)}")
        if entry.attempts:
            bits.append(f"attempts={entry.attempts}")
        if entry.deferrals:
            bits.append(f"deferrals={entry.deferrals}")
        if entry.resumes:
            # #2230: how many times the merge-gate sweep has auto-resumed
            # THIS row from `blocked` — the churn signal the issue asks to be
            # visible, not just logged in a tick's journal.
            bits.append(f"resumes={entry.resumes}/{MAX_BLOCKED_RESUMES}")
        if entry.hold_after:
            bits.append(f"hold={entry.hold_state or 'armed'}")
            if entry.hold_scope == HOLD_SCOPE_FLEET:
                bits.append("scope=fleet")
        click.echo("  ".join(bits))
        if entry.last_reason:
            click.echo(f"      last{_reason_age_suffix(entry, now)}: {entry.last_reason}")
        if diagnosed:
            # #2183 point 4: `blocked`/`failed` is terminal — say so where the
            # operator is already reading the row, not just in an unrelated
            # operator's hand-written `--hold-after` note.
            #
            # #2404: EXCEPT when this row's own `last_reason` is exactly the
            # unsatisfiable-`after=`-pre-req verdict on a `blocked` (not
            # `failed`) entry — that shape auto-resumes on its own every tick
            # (#2362), so the generic "never re-checked on its own" remedy
            # would be actively wrong for it; swap in the note that says so.
            if entry.state == STATE_BLOCKED and is_unsatisfiable_prereq_reason(
                entry.last_reason
            ):
                click.echo(f"      {_BLOCKED_AFTER_NOTE.format(state=entry.state)}")
            else:
                click.echo(f"      {_BLOCKED_REMEDY.format(state=entry.state)}")
        # #2230: independent of the `after=` diagnosis above (most `blocked`
        # rows have no `after=` at all — they blocked on attempts, not a
        # pre-req), so gated on the row's OWN state and cause, not `diagnosed`.
        #
        # #2404 review: EXCLUDE the unsatisfiable-`after=`-pre-req shape
        # `_BLOCKED_AFTER_NOTE` above already covers. #2230's sweep
        # (`_blocked_gate_reading`) returns `None` — "no evidence" — for an
        # entry that never reached the merge queue at all, which is exactly
        # what a purely `after=`-caused block is; showing this note there
        # would stack two auto-recheck notes citing two different issue
        # numbers under one row, one of which has nothing to say about that
        # row's actual cause. `is_permanent_block_reason` rows already skip
        # this note on their own; this adds the sibling exclusion so the two
        # notes stay mutually exclusive, matching what each mechanism can
        # actually promise for a given row.
        if (
            entry.state == STATE_BLOCKED
            and not is_permanent_block_reason(entry.last_reason)
            and not is_unsatisfiable_prereq_reason(entry.last_reason)
        ):
            if pre_dispatch_terminal:
                note = (
                    _BLOCKED_TERMINAL_NOTE.format(attempts=entry.attempts)
                    if entry.attempts
                    else _BLOCKED_TERMINAL_NOTE_NO_ATTEMPTS
                )
                click.echo(f"      {note}")
            else:
                click.echo(f"      {_BLOCKED_GATE_NOTE}")
        for line in _hold_lines(entry):
            click.echo(line)


def _reason_age_suffix(entry: QueueEntry, now: float) -> str:
    """`` (3h ago)`` / `` (42s ago)``, or ``''`` when the age is unknown.

    #2133: a `last_reason` is a snapshot taken the instant the tick (or a
    guard) wrote it — never re-validated — so displaying it bare lets an
    hours-old, no-longer-true observation read as current state (that
    misdirected a live diagnosis on #2104: `checks_failed` was still shown
    ~3 hours after the named checks had gone green, while the actual
    blocker — a later `request-changes` review — was nowhere in the
    output). Stamping the age doesn't make the reason current, but it stops
    it from being silently *mistaken* for current, which is the whole
    defect. Empty for `entry.reason_at is None` — a row predating #2133's
    migration, or an entry whose `last_reason` was never written through
    `update_drive_queue_entry` (e.g. hand-built in a test) — rather than
    guessing an age it doesn't have.
    """
    if entry.reason_at is None:
        return ""
    return f" ({_age_str(max(0.0, now - entry.reason_at))} ago)"


def _age_str(delta_seconds: float) -> str:
    """``42s`` / ``3h`` / ``5d`` — coarse, single-unit, matches #2133's ask."""
    if delta_seconds < 60:
        return f"{int(delta_seconds)}s"
    if delta_seconds < 3600:
        return f"{int(delta_seconds // 60)}m"
    if delta_seconds < 86400:
        return f"{int(delta_seconds // 3600)}h"
    return f"{int(delta_seconds // 86400)}d"


def _hold_lines(entry: QueueEntry) -> list[str]:
    """The gate's rendering for `list` / `status`, or `[]` when there is none.

    Both verbs render through this one function so `list` and `status` can
    never disagree about whether the queue is held — the failure mode that
    makes an operator stop trusting either.
    """
    if not entry.hold_after:
        return []
    scope_suffix = (
        " [fleet-wide — holds everything, not just this entry's dependents]"
        if entry.hold_scope == HOLD_SCOPE_FLEET
        else ""
    )
    lines = [f"      hold-after: {entry.gate_reason}{scope_suffix}"]
    if entry.resume_when:
        probe = f"      resume-when: {entry.resume_when}"
        if entry.hold_probes:
            probe += f"  (failed {entry.hold_probes}×)"
        lines.append(probe)
    return lines


# ── #2235 Phase 0 stall log ──────────────────────────────────────────────────


def _record_block_log(events: list[dict[str, Any]]) -> int:
    """Append #2235's Phase-0 stall records.  Observability only.

    Best-effort by construction (:func:`coord.block_log.record` swallows its
    own errors) and, in the tick, called only AFTER ``_apply_writes`` — so the
    recording can neither precede nor influence the writes it describes.
    #2235's Phase 0 is explicitly *instrumentation*: it must change no merge,
    dispatch or attempt-accounting decision, and the cheapest way to guarantee
    that is for the log to sit downstream of every decision and be read by
    none of them.

    Returns how many events actually landed — :func:`coord.block_log.record`'s
    own count, ``0`` on a full disk / read-only ``$HOME`` / unserialisable
    event. Every tick call site treats this as fire-and-forget and ignores
    the count; ``drive_queue_log_intervention`` is the one caller that must
    not (#2540 review) — printing "logged" on the strength of "did not raise"
    alone is the "unconfirmed success" shape epic #2096's checklist forbids.
    """
    if not events:
        return 0
    from coord import block_log  # noqa: PLC0415

    return block_log.record(events)


def _record_operator_release(row: Mapping[str, Any] | None, *, resolution: str) -> None:
    """Log a stall a HUMAN just cleared, if the row was actually stalled.

    Called with the PRE-mutation row, since the whole record is about the
    state that is being destroyed.  A row that was not ``blocked``/``parked``
    logs nothing: removing a ``done`` entry is housekeeping, and counting it
    as an intervention would inflate the one number #2235 wants to watch fall.
    """
    if row is None:
        return
    try:
        entry = entries_from_rows([row])[0]
    except (IndexError, KeyError, TypeError, ValueError):  # pragma: no cover
        return
    if entry.state not in STALL_STATES:
        return
    from coord.block_log import operator_resolution_event  # noqa: PLC0415

    _record_block_log(
        [
            operator_resolution_event(
                entry, resolution=resolution, host=_local_host_id()
            )
        ]
    )


# ── log-intervention (#2540) ─────────────────────────────────────────────────


@drive_queue_group.command("log-intervention")
@click.argument("repo")
@click.argument("issue", type=int)
@click.option(
    "--category",
    default="other",
    show_default=True,
    help=(
        "What kind of intervention this was. Free text, not a closed set — "
        "the documented starting buckets are: "
        + ", ".join(INTERVENTION_CATEGORIES)
        + "."
    ),
)
@click.option(
    "--note",
    default="",
    help="Optional free-text detail — what you actually did.",
)
@_CONFIG_OPTION
def drive_queue_log_intervention(
    repo: str, issue: int, category: str, note: str, config_path: Path
) -> None:
    """Record that a human acted on REPO ISSUE, outside the queue's own commands (#2540).

    `human_acted` in `coord drive-queue block-log` only ever recognised
    `remove`/`resume` and a Gate-A sign-off — the drive-queue command surface.
    Real recovery routinely happens elsewhere entirely: a manual git rebase /
    conflict resolution / `git push --force-with-lease`, a direct `coord
    test`/`coord merge --only`/`coord pr`/`coord fix` against the assignment
    underneath this entry, a `systemctl`/`coord agent update`/`coord diagnose
    --reset` on the machine running it. None of that touches this process's
    own write paths, so none of it was ever counted — a night of real manual
    recovery could read as `0 needed a human`.

    This command does not try to detect that after the fact (block-log
    recording never probes live state — see `coord/block_log.py`). It gives
    you a place to say so. Run it once per intervention, whenever you do one
    — during the recovery or shortly after, either is fine — and `block-log`
    folds it onto whichever episode was open for this key at the time, or, if
    it had already resolved by the time you got to logging it, the most
    recently closed one. It never guesses at *why* the entry was stalled or
    what specifically you fixed — `--note` is where that goes, verbatim.

    A REPO ISSUE this host has never recorded a stall for has nothing to
    attach this to — the record is still written (append-only, never lost),
    but this warns rather than pretending it landed somewhere.

    Runs against **this host's** block log only — it is per-host by design
    (see `coord/block_log.py`). Run it on the same host that recorded the
    original stall (usually the daemon host that ran the tick), not e.g. your
    laptop, or the write lands in a log `coord drive-queue block-log` on the
    host that actually stalled will never read.
    """
    from coord.block_log import episodes, intervention_event, read_events  # noqa: PLC0415

    key = entry_key(repo, issue)
    event = intervention_event(
        key=key, category=category, note=note, host=_local_host_id()
    )
    written = _record_block_log([event])
    if written < 1:
        raise click.ClickException(
            f"failed to log intervention against {key} — the block log did "
            "not accept the write (full disk, read-only $HOME, or an "
            "unserialisable record); nothing was recorded, try again"
        )

    # #2540 review: "did not raise" is not "landed" — read the log back and
    # confirm the record we just wrote is actually in it before telling the
    # operator it's logged. This reuses the same `read_events()` call
    # `episodes()` below already needs for the attach-to-episode check (one
    # membership check against it), rather than a second pass over the log.
    all_events = read_events()
    if event not in all_events:
        raise click.ClickException(
            f"failed to log intervention against {key} — the write reported "
            "success but the record could not be read back afterward; "
            "nothing is confirmed logged, try again"
        )

    matches = [ep for ep in episodes(all_events) if ep.get("key") == key]
    if not matches:
        click.echo(
            f"logged, but {key} has no recorded stall on this host's block "
            "log yet — it will not attach to an episode until one exists "
            "(see `coord drive-queue block-log`)",
            err=True,
        )
        return
    click.echo(f"logged a {category!r} intervention against {key}")


# ── remove / move ────────────────────────────────────────────────────────────


@drive_queue_group.command("remove")
@click.argument("repo")
@click.argument("issue", type=int)
@_CONFIG_OPTION
def drive_queue_remove(repo: str, issue: int, config_path: Path) -> None:
    """Drop REPO ISSUE from the queue (positions are renumbered dense).

    #2839: deliberately does NOT touch the `coord`/`status:*` labels `add`
    applies — dropping out of the drive queue is not eviction from the
    Pipeline. `coord untrack` stays the only way to evict a card; do not
    "fix" this asymmetry by mirroring `add`'s label write here.
    """
    from coord.state import dequeue_drive_queue, get_drive_queue_entry  # noqa: PLC0415

    # #2235 Phase 0: read the row BEFORE deleting it, because `remove` on a
    # `blocked`/`parked` entry IS the intervention the plan's success metric
    # counts — it is the documented one-key fix (`_requeue_command`), and the
    # only signal this fleet has that a human had to act. Read first or the
    # state and the stated reason are gone with the row. Failure to read is
    # silently tolerated: instrumentation must never be able to stop an
    # operator removing an entry.
    try:
        before = get_drive_queue_entry(repo, issue)
    except Exception:  # noqa: BLE001 — observability only, never blocks the remove
        before = None

    removed = dequeue_drive_queue(repo, issue)
    if not removed:
        raise click.ClickException(f"{entry_key(repo, issue)} is not in the drive queue")
    _record_operator_release(before, resolution="operator_removed")
    click.echo(f"removed {entry_key(repo, issue)} from the drive queue")


@drive_queue_group.command("move")
@click.argument("repo")
@click.argument("issue", type=int)
@click.option("--to", "to_position", type=int, required=True, help="New 0-based position (clamped into range).")
@_CONFIG_OPTION
def drive_queue_move(repo: str, issue: int, to_position: int, config_path: Path) -> None:
    """Move REPO ISSUE to a new position in the queue."""
    from coord.state import move_drive_queue_entry  # noqa: PLC0415

    moved = move_drive_queue_entry(repo, issue, to_position)
    if not moved:
        raise click.ClickException(f"{entry_key(repo, issue)} is not in the drive queue")
    click.echo(f"moved {entry_key(repo, issue)} to position {to_position}")


# ── status ───────────────────────────────────────────────────────────────────


def _counts(rows: list[Mapping[str, Any]]) -> dict[str, int]:
    """State histogram for a queue read."""
    counts: dict[str, int] = {}
    for entry in entries_from_rows(rows):
        counts[entry.state] = counts.get(entry.state, 0) + 1
    return counts


def _queue_alert() -> dict | None:
    """The current queue-level alert record, if a tick raised one.

    Read back through the same synthetic escalation key the tick writes — see
    ``QUEUE_ALERT_REPO`` in coord/drive_queue.py for why that seam and not a
    synthetic ``drive_queue`` row.
    """
    from coord.state import get_drive_escalation  # noqa: PLC0415

    return get_drive_escalation(QUEUE_ALERT_REPO, QUEUE_ALERT_ISSUE)


def _unreachable_wait_alert_dict(rows: list[Mapping[str, Any]]) -> dict | None:
    """#2944's synthetic queue-level alert, or ``None`` when nothing qualifies.

    Computed fresh from the CURRENT queue rows on every call — deliberately
    independent of :func:`_queue_alert`'s per-tick escalation record, which a
    busy queue overwrites (and clears — see the tick's own ``else:
    _clear_queue_alert()`` branch) every single tick it finds *something*
    else to launch. A wedged entry with `attempts=0` sits in `blocked`/
    `parked` for as long as it likes regardless of what else the queue is
    doing, so its visibility must not depend on a tick ever having nothing
    better to report — see :func:`coord.drive_queue.detect_unreachable_waits`
    for the predicate and the #2900/#2907 incident this closes.
    """
    waits = detect_unreachable_waits(entries_from_rows(rows))
    if not waits:
        return None
    synthetic = unreachable_wait_alert(waits)
    return {
        "reason": synthetic.reason,
        "gate_readings": " | ".join(synthetic.details),
        "proposed_command": synthetic.command,
    }


@drive_queue_group.command("status")
@click.option("--json", "output_json", is_flag=True, default=False, help="Emit counts + alert as JSON.")
@_CONFIG_OPTION
def drive_queue_status(output_json: bool, config_path: Path) -> None:
    """Queue counts by state, plus the current queue-level alert."""
    from coord.state import list_drive_queue  # noqa: PLC0415

    rows = list_drive_queue()
    counts = _counts(rows)
    # #2944: a tick-raised alert (a HELD gate, "nothing eligible to launch")
    # takes priority when one exists — it is fresher and, on a busy queue,
    # `_unreachable_wait_alert_dict` would silently win-and-hide it every
    # time a real, actionable alert IS present. Only when the tick has
    # nothing to say (the common, dangerous case a queue with free slots
    # sits in for hours) does the guaranteed-false-wait check get to speak.
    alert = _queue_alert() or _unreachable_wait_alert_dict(rows)
    held = fired_holds(entries_from_rows(rows))
    # #2587: read directly from the marker file, not from the last tick's
    # `TickPlan` — `status` may run between ticks (or on a machine that never
    # ticks at all), and the marker's own `deferrals`/TTL are exactly what an
    # operator needs to see this is a DELIBERATE hold, not a stalled queue.
    roll_pending = read_roll_pending()

    if output_json:
        click.echo(
            _json.dumps(
                {
                    "total": len(rows),
                    "counts": counts,
                    "alert": alert,
                    # #1757: typed, so a client (or a test) reads the gate
                    # without parsing the rendered sentence back out.
                    "held": [
                        {
                            "key": e.key,
                            "reason": e.gate_reason,
                            "resume_when": e.resume_when,
                            "probes": e.hold_probes,
                            # #2186: typed, so a client reads what this gate
                            # holds without parsing the `[fleet-wide ...]`
                            # suffix back out of the rendered text.
                            "scope": e.hold_scope,
                        }
                        for e in held
                    ],
                    "roll_pending": (
                        roll_pending.to_dict() if roll_pending is not None else None
                    ),
                }
            )
        )
        return

    if not rows:
        click.echo("drive queue: empty")
    else:
        ordered = [s for s in _STATE_ORDER if counts.get(s)]
        ordered += sorted(s for s in counts if s not in _STATE_ORDER)
        click.echo(
            "drive queue: " + " · ".join(f"{counts[s]} {s}" for s in ordered)
        )
    # The gate goes ABOVE the alert: "HELD" is the state, the alert is the
    # note about it, and an operator scanning the first line must not have to
    # read three more to learn the queue has stopped.
    for entry in held:
        click.echo(f"HELD — {entry.gate_reason}")
        for line in _hold_lines(entry):
            click.echo(line)
        click.echo("      release with: coord drive-queue resume")
    if roll_pending is not None:
        # #2587: deliberately its OWN line, not folded into `alert:` below —
        # this is a benign, self-clearing, EXPECTED state ("the fleet is
        # about to upgrade itself"), not an anomaly needing operator action,
        # and #2587 is explicit that a held-for-a-roll queue must never read
        # as broken.
        age = max(0.0, time.time() - roll_pending.set_at)
        click.echo(
            f"{roll_pending.describe()} — set {age:.0f}s ago, "
            f"{roll_pending.deferrals}/{roll_pending.max_deferrals} deferrals, "
            f"launches nothing until the queue empties out or "
            f"{max(0.0, roll_pending.ttl_seconds - age):.0f}s pass"
        )
    if alert is not None:
        click.echo(f"alert: {alert.get('reason') or ''}")
        for detail in (alert.get("gate_readings") or "").split(" | "):
            if detail:
                click.echo(f"  {detail}")
    else:
        click.echo("alert: (none)")


@drive_queue_group.command("cancel-roll")
@click.option("--json", "output_json", is_flag=True, default=False,
              help="Emit the cancelled marker (and any cordons cleared with it) as JSON.")
def drive_queue_cancel_roll(output_json: bool) -> None:
    """Cancel a #2587 roll-pending marker so the queue resumes launching now.

    #2607: every OTHER queue-blocking state has an operator override — a
    held deploy gate has `coord drive-queue resume`, a release cordon has
    `coord release cordon --clear` — but the roll-pending marker had none.
    It is supposed to be self-bounding (a TTL / deferral ceiling —
    `RollPending.expired`), but a marker that keeps getting RE-ARMED before
    it ever expires never reaches that bound on its own (the defect #2607
    fixed at the write sites); this command is the escape hatch for while
    that marker is still live, and for any future case a re-arm loop is not
    yet ruled out for. Also clears any release cordon still open for the
    SAME target — the cordon a stalled `coord release propagate`/
    `nightly-window` run can leave behind — so cancelling actually frees the
    queue rather than trading one hold for another.

    Does NOT change the target: the next `coord release propagate`/
    `nightly-window` run re-arms a marker from scratch if the fleet is still
    behind. This only stops the CURRENT wait.

    #2889: also resets the roll LEDGER (`RollLedger`) — the cumulative
    frozen-time bookkeeping a fresh marker's own arm now checks — so an
    operator clearing a stuck marker also clears whatever run-up of
    cumulative frozen time and fresh-arm history led to it, exactly the
    "operator intervenes" escape hatch item 1's cumulative bound requires.
    Reset unconditionally, even when there is no LIVE marker to cancel: a
    ledger can be `escalated` (refusing every fresh arm) with nothing
    currently pending — its last marker already expired and cleared on its
    own — and that state needs the SAME override.
    """
    from coord.machine_pause import clear_cordon, cordons as read_cordons  # noqa: PLC0415

    pending = read_roll_pending()
    ledger = read_roll_ledger()
    ledger_had_history = ledger.marker_count > 0 or ledger.cumulative_frozen_seconds > 0
    cleared_cordons: list[str] = []
    if pending is not None:
        try:
            active_cordons = read_cordons()
        except Exception:  # noqa: BLE001 — best-effort; the marker clear below is load-bearing
            active_cordons = {}
        for name, cordon in active_cordons.items():
            if getattr(cordon, "target_version", None) != pending.target_version:
                continue
            try:
                if clear_cordon(name):
                    cleared_cordons.append(name)
            except Exception:  # noqa: BLE001 — best-effort; never block the marker clear on this
                pass
        clear_roll_pending()
    if ledger_had_history:
        reset_roll_ledger()

    if output_json:
        click.echo(
            _json.dumps(
                {
                    "cancelled": pending.to_dict() if pending is not None else None,
                    "cleared_cordons": sorted(cleared_cordons),
                    "ledger_reset": ledger.to_dict() if ledger_had_history else None,
                },
                sort_keys=True,
            )
        )
        return

    if pending is None and not ledger_had_history:
        click.echo("no roll-pending marker to cancel — the queue is not held for a roll")
        return
    if pending is not None:
        click.echo(
            f"cancelled {pending.describe()} — the drive-queue tick resumes "
            "launching immediately"
        )
    else:
        click.echo(
            "no roll-pending marker to cancel, but the roll ledger had "
            f"{ledger.cumulative_frozen_seconds:.0f}s cumulative frozen time "
            f"across {ledger.marker_count} marker generation(s)"
        )
    if cleared_cordons:
        click.echo(
            "also cleared cordon(s) still open for this target: "
            + ", ".join(sorted(cleared_cordons))
        )
    if ledger_had_history:
        click.echo(
            "also reset the #2889 roll ledger "
            f"({ledger.cumulative_frozen_seconds:.0f}s cumulative frozen time / "
            f"{ledger.marker_count} marker generation(s)) — a fresh arm is no "
            "longer refused for having run up that history"
        )
    if pending is not None:
        click.echo(
            "note: this only stops the current wait — the next `coord release "
            "propagate`/`nightly-window` run re-arms a fresh marker if the "
            "fleet is still behind"
        )


# ── block-log (#2235 Phase 0) ────────────────────────────────────────────────


def _episode_line(item: Mapping[str, Any]) -> str:
    """One episode, one line, ordered so the interesting column is first.

    ``stated`` and ``cause`` sit adjacent on purpose: #2235's whole finding is
    that they disagree five times out of seven, and a render that separates
    them makes the reader hold both in their head to notice.
    """
    key = str(item.get("key") or "?")
    state = str(item.get("state") or "?")
    stated = " ".join(str(item.get("stated_reason") or "(none)").split())
    if len(stated) > 90:
        stated = stated[:87] + "..."
    # #2540: whatever `coord drive-queue log-intervention` has recorded
    # against this episode, open or resolved — rendered the same way in
    # both branches below so an operator sees it whether the entry is still
    # stalled (they logged it live) or already closed (they logged it after).
    intervened = ", ".join(str(c) for c in (item.get("intervention_categories") or []))
    if not item.get("resolved"):
        line = f"{key:<28} {state:<8} STILL STALLED  stated: {stated}"
        if intervened:
            line += f"\n{'':<28} {'':<8}               intervened: {intervened}"
        # #2276 Phase 1.  An open episode used to have nothing in the `cause`
        # column at all — `summarize` bucketed it as `(unresolved)`. When the
        # diagnostician has been round, it does, and the CONTRADICTS marker is
        # what makes #2235's five-of-seven finding legible at a glance instead
        # of requiring the reader to hold both strings in their head.
        cause = " ".join(str(item.get("true_cause") or "").split())
        if cause:
            flag = (
                "  ← CONTRADICTS the stated reason"
                if item.get("diagnosis_contradicts_stated")
                else ""
            )
            confidence = str(item.get("diagnosis_confidence") or "?")
            line += (
                f"\n{'':<28} {'':<8}               cause:  {cause}"
                f"\n{'':<28} {'':<8}               "
                f"(diagnosed, confidence {confidence}){flag}"
            )
        return line
    mark = "HUMAN" if item.get("human_acted") else "auto "
    held = item.get("stalled_seconds")
    age = _age_str(float(held)) if held is not None else "?"
    cause = " ".join(str(item.get("true_cause") or "").split())
    line = (
        f"{key:<28} {state:<8} {mark} {age:>6}  stated: {stated}\n"
        f"{'':<28} {'':<8}              cause:  {cause}"
    )
    if intervened:
        # `cause` is still the auto mechanism's own account of what flipped
        # the state (#2540 never rewrites it) — this line is the separate,
        # explicit claim that a human was ALSO in the loop.
        line += f"\n{'':<28} {'':<8}              logged: {intervened}"
    return line


@drive_queue_group.command("block-log")
@click.option("--json", "output_json", is_flag=True, default=False, help="Emit episodes + summary as JSON.")
@click.option(
    "--days",
    type=float,
    default=14.0,
    show_default=True,
    help=(
        "Only episodes that STARTED within this many days. The default is "
        "#2235's own window — Phase 1's scope is gated on two weeks of this "
        "log, so two weeks is what the default report shows."
    ),
)
@_CONFIG_OPTION
def drive_queue_block_log(output_json: bool, days: float, config_path: Path) -> None:
    """Every stall this host recorded: stated reason vs. true cause (#2235).

    Phase 0 of the queue-rescue plan, and *only* Phase 0. Each `blocked`/
    `parked` transition is recorded as it happens, together with how the entry
    eventually got out, so the question "what actually stalls this queue, and
    how often did a human have to act?" is answered from two weeks of evidence
    rather than from one morning's triage.

    This command itself does not diagnose — it renders what other passes
    recorded. For a *resolved* episode, `cause` is still derived from the
    release the queue itself performed (a #2230 gate-clear, a #1891 CI
    resume, an operator's `remove`), never from a live re-check. For a
    still-open episode, `cause` may instead be a Phase 1 (#2276) live
    re-check's verdict — `coord drive-queue diagnose` or the notifier tick's
    `diagnose_pass` recorded it as a `diagnosis` event, and `_episode_line`
    renders it with a `(diagnosed, confidence ...)` tag so it reads distinct
    from a resolution-derived cause.

    Read the two summary numbers together: `human_acted` is the count #2235
    wants to see fall, but a queue that stops needing interventions by leaving
    everything stalled forever shows up as `open` climbing, not as success.
    `repeats` is the tripwire — the same repo stalling on the same stated
    reason twice is a bug report, not a rescue.

    `human_acted` folds in both how it always could tell (an operator's
    `remove`, a Gate-A sign-off) and #2540's `coord drive-queue
    log-intervention` records — the ones logged are called out separately in
    the summary line, because that count is the only one with an actual paper
    trail behind it (see `human_acted_logged` / `_episode_line`'s `logged:`
    row). It is still not a ceiling: an intervention nobody ran
    `log-intervention` for is invisible to this command by construction.
    """
    from coord.block_log import block_log_path, episodes, read_events, summarize  # noqa: PLC0415

    since = time.time() - days * 86400.0 if days > 0 else None
    events = read_events(since=since)
    items = episodes(events)
    stats = summarize(items)

    if output_json:
        click.echo(
            _json.dumps(
                {
                    "path": str(block_log_path()),
                    "days": days,
                    "summary": stats,
                    "episodes": items,
                }
            )
        )
        return

    if not items:
        click.echo(
            f"no stalls recorded in the last {days:g}d "
            f"({block_log_path()})"
        )
        return
    for item in items:
        click.echo(_episode_line(item))
    click.echo("")
    # #2540: call out how many of `human_acted` are backed by an actual
    # `log-intervention` record, but only when there is one to show — the
    # base sentence stays exactly what it always was otherwise, so a fleet
    # that has never run the new command reads no different than before.
    logged = stats.get("human_acted_logged") or 0
    human_note = f" ({logged} logged)" if logged else ""
    click.echo(
        f"{stats['episodes']} stall(s) in {days:g}d — "
        f"{stats['human_acted']} needed a human{human_note} · "
        f"{stats['auto_released']} released themselves · "
        f"{stats['open']} still stalled"
    )
    if stats["repeat_causes"]:
        click.echo("repeats (a repeat is a bug report, not a success):")
        for label, count in sorted(
            stats["repeat_causes"].items(), key=lambda kv: (-kv[1], kv[0])
        ):
            click.echo(f"  {count}× {label}")
    for line in _diagnosis_summary_lines(stats.get("diagnosis") or {}):
        click.echo(line)


def _diagnosis_summary_lines(diag: Mapping[str, Any]) -> list[str]:
    """#2276's success criterion, rendered as a measurement.

    The disagreement rate is printed as ``(not yet measurable)`` rather than
    ``0%`` until something scorable has resolved, because #2276 is explicit
    that the number must be *measured, not assumed* — and "0% wrong out of
    nothing" is the most flattering way there is to assume it.
    """
    if not diag.get("diagnosed"):
        return []
    lines = [
        f"diagnosed {diag['diagnosed']} stall(s) — "
        f"{diag.get('contradicted_stated_reason', 0)} contradicted the stated reason"
    ]
    rate = diag.get("disagreement_rate")
    scored = f"{diag.get('agreed', 0)} agreed · {diag.get('disagreed', 0)} disagreed"
    tail = (
        f"{rate * 100:.0f}% disagreement"
        if rate is not None
        else "disagreement rate: (not yet measurable — nothing scorable has resolved)"
    )
    lines.append(f"  vs. how they actually resolved: {scored} — {tail}")
    lines.append(
        f"  {diag.get('abstained', 0)} abstained (said unknown, which is not a "
        f"failure) · {diag.get('undecided', 0)} unscorable"
    )
    return lines


# ── diagnose (#2276 Phase 1) ─────────────────────────────────────────────────


def _diagnosis_lines(diagnosis: Any) -> list[str]:
    """One diagnosis, rendered.  ``stated`` and ``cause`` adjacent, as ever."""
    flag = " ← CONTRADICTS the stated reason" if diagnosis.contradicts_stated else ""
    trigger = f"  (triggered by: {diagnosis.trigger})" if diagnosis.trigger else ""
    lines = [
        f"{diagnosis.key} [{diagnosis.state}]{trigger}",
        f"  stated: {' '.join((diagnosis.stated_reason or '(none)').split())}",
        f"  cause:  {diagnosis.cause} (confidence {diagnosis.confidence}){flag}",
    ]
    if diagnosis.abstained:
        lines.append(
            "          unknown is a verdict, not a failure — the evidence "
            "below was too thin to name a cause"
        )
    lines.append("  evidence:")
    lines.extend(f"    - {line}" for line in diagnosis.evidence)
    return lines


@drive_queue_group.command("diagnose")
@click.option("--json", "output_json", is_flag=True, default=False, help="Emit diagnoses as JSON.")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Derive and print, but do not append the diagnosis to the block log.",
)
@_CONFIG_OPTION
def drive_queue_diagnose(output_json: bool, dry_run: bool, config_path: Path) -> None:
    """Re-derive the REAL blocker of every still-stalled entry (#2276 Phase 1).

    Read-only. It runs no `coord merge`, dispatches nothing, and writes nothing
    to the board or the queue — its single output is a `diagnosis` record in
    the Phase-0 block log, which no decision path reads.

    The queue's stated reason is an INPUT TO BE CONTRADICTED, never a starting
    hypothesis: #2235 found that five of seven overnight stalls named a symptom
    rather than a cause, so this asks GitHub, the live gate report and the
    agent's /health what is actually true right now, and only then checks
    whether the evidence still supports what the queue said.

    `unknown` is a first-class verdict. Thin evidence gets an abstention, not a
    guess — a confidently wrong cause is worse than no cause, because #2268's
    Phase 2 would inherit the confidence.

    It runs here, and in `coord serve`'s notifier tick, rather than as a
    dispatched worker: a diagnosis needs `gh`, and `gh` is denied to workers
    (#1483). The trigger in the daemon is #1632's own stall detector — there is
    deliberately no second definition of "stalled" anywhere in this path.

    That notifier-tick trigger only fires when `notifications.enabled` is
    true (`coord.notifier.service._tick` returns before `diagnose_pass` runs
    otherwise) — notifications are off by default in this repo, so on a
    fleet that has not turned them on, THIS command is the only live entry
    point into Phase 1.
    """
    from coord import block_log, queue_diagnose  # noqa: PLC0415
    from coord.board_service import is_remote  # noqa: PLC0415
    from coord.commands._common import _load_config  # noqa: PLC0415

    # #615/#906 guard: everything this command reads — the Phase-0 block log,
    # the queue rows, the board handed to GhLiveProbe — lives on the daemon
    # host, and the probes need `gh` (denied to workers, #1483). On a thin
    # client all three reads would be silently empty, so the "diagnosis"
    # would be a confident report about a queue that isn't there. Fail loud
    # instead (the exact #615 failure mode this audit exists to prevent).
    if is_remote():
        raise click.ClickException(
            "drive-queue diagnose reads the daemon host's block log, queue "
            "and board directly — run it there (a board_service-configured "
            "thin client would diagnose an empty queue)."
        )

    from coord.drive import list_drive_sessions  # noqa: PLC0415
    from coord.health.aggregate import local_fleet_health_block  # noqa: PLC0415
    from coord.state import build_board, list_drive_queue  # noqa: PLC0415

    config = _load_config(config_path)
    episodes = [
        ep for ep in block_log.episodes(block_log.read_events()) if not ep.get("resolved")
    ]
    entries = entries_from_rows(list_drive_queue())
    # #2276 review: without these two, `GhLiveProbe._health()` short-circuits
    # to `(None, None, [])` for every entry — `agent_reachable` and
    # `agent_has_session` are permanently unknown, which makes `dead-leg` and
    # `agent-unreachable` structurally unreachable verdicts from this command.
    # Same local-DB read `coord status` uses in host mode (status.py's own
    # `local_fleet_health_block` call) — no fresh /health round trip, just
    # the last tick's reported state; and the same `list_drive_sessions()`
    # this file already calls elsewhere (`_fetch_board_view`) for live tmux
    # session names.
    fleet_health = local_fleet_health_block([m.name for m in config.machines])
    live_sessions = frozenset(
        str(row["session_name"])
        for row in list_drive_sessions()
        if row.get("session_name")
    )
    probe = queue_diagnose.GhLiveProbe(
        config=config,
        board=build_board(),
        fleet_health=fleet_health,
        live_sessions=live_sessions,
        # #1870: that `list_drive_sessions()` above is a LOCAL tmux read, so
        # the probe has to know whose tmux it is. Without this an entry some
        # OTHER machine launched reads as "session absent" here and lands a
        # high-confidence `dead-leg` on a session that is very much alive —
        # the same trap `_reconcile_running` already sidesteps.
        local_host=_local_host_id(),
    )
    # `keys=None` — an operator asking directly wants every open episode
    # looked at, not just the ones the notifier happens to have raised this
    # minute. `limit=None` for the same reason, and because the per-pass cap
    # exists to protect `coord serve`'s 30 s tick, which this is not on: a cap
    # here would silently diagnose four of ten and print "4 diagnosed", which
    # reads as "that was all of them". The per-episode budget still applies,
    # so this cannot become a way to spend `gh` calls in a loop.
    diagnoses = queue_diagnose.run_pass(
        entries, episodes, probe=probe, keys=None, limit=None
    )

    if diagnoses and not dry_run:
        _record_block_log(
            [
                block_log.diagnosis_event(
                    key=d.key,
                    state=d.state,
                    stated_reason=d.stated_reason,
                    true_cause=d.true_cause,
                    cause=d.cause,
                    confidence=d.confidence,
                    evidence=d.evidence,
                    contradicts_stated=d.contradicts_stated,
                    trigger=d.trigger,
                    host=_local_host_id(),
                )
                for d in diagnoses
            ]
        )

    if output_json:
        click.echo(
            _json.dumps(
                {
                    "recorded": bool(diagnoses) and not dry_run,
                    "diagnoses": [
                        {
                            "key": d.key,
                            "state": d.state,
                            "stated_reason": d.stated_reason,
                            "cause": d.cause,
                            "true_cause": d.true_cause,
                            "confidence": d.confidence,
                            "evidence": list(d.evidence),
                            "contradicts_stated": d.contradicts_stated,
                            "trigger": d.trigger,
                        }
                        for d in diagnoses
                    ],
                }
            )
        )
        return

    if not diagnoses:
        click.echo(
            "nothing to diagnose: no open stall episode is both unexplained and "
            "still within its diagnosis budget"
        )
        return
    for diagnosis in diagnoses:
        for line in _diagnosis_lines(diagnosis):
            click.echo(line)
        click.echo("")
    contradicted = sum(1 for d in diagnoses if d.contradicts_stated)
    abstained = sum(1 for d in diagnoses if d.abstained)
    click.echo(
        f"{len(diagnoses)} diagnosed — {contradicted} contradicted the stated "
        f"reason · {abstained} abstained (unknown)"
    )
    click.echo(
        "nothing was mutated; read the outcome later with "
        "`coord drive-queue block-log`"
    )


# ── overlap-report (#2247) ───────────────────────────────────────────────────


def _branch_index(board: Any, repo: str) -> dict[int, str]:
    """``issue number -> branch`` for every work-like assignment in *repo*.

    Reads ``completed`` as well as ``active``: scoring happens AFTER the work
    landed, which is precisely when its assignment is no longer active.
    """
    index: dict[int, str] = {}
    for bucket in ("active", "completed"):
        for a in list(getattr(board, bucket, ()) or []):
            if getattr(a, "type", "") not in WORK_LIKE:
                continue
            if getattr(a, "repo_name", "") != repo or not getattr(a, "branch", ""):
                continue
            index.setdefault(int(a.issue_number), str(a.branch))
    return index


@drive_queue_group.command("overlap-report")
@click.option("--repo", "repo", default=None, help="Restrict to one repo (default: every repo).")
@click.option("--limit", type=int, default=200, show_default=True, help="How many prediction rows to read back.")
@click.option("--json", "output_json", is_flag=True, default=False, help="Emit the scored rows as JSON.")
@_CONFIG_OPTION
def drive_queue_overlap_report(
    repo: str | None, limit: int, output_json: bool, config_path: Path,
) -> None:
    """Score #2247's file-overlap predictions against what the branches DID touch.

    Every auto-`--after` this feature applied is a checkable claim ("these two
    file sets will intersect"). This reads those claims back out of the audit
    log and compares them to the real diffs, recording each verdict so a
    FALSE POSITIVE — an entry serialized for nothing — is a number rather than
    an anecdote. A prediction whose branches cannot be diffed yet is left
    unscored, not counted against the predictor.
    """
    from coord.audit import query_audit_log, record_audit  # noqa: PLC0415

    try:
        predicted = query_audit_log(
            event_type=EVENT_PREDICTED, category=AUDIT_CATEGORY, repo=repo, limit=limit,
        )
        already = query_audit_log(
            event_type=EVENT_SCORED, category=AUDIT_CATEGORY, repo=repo, limit=limit,
        )
    except Exception as exc:  # noqa: BLE001 — a read-only report never crashes
        raise click.ClickException(f"could not read the audit log: {exc}") from None

    scored_before = {
        (str((e.get("details") or {}).get("key") or ""),
         str((e.get("details") or {}).get("other_key") or ""))
        for e in already.get("entries") or []
    }
    records = predictions_from_audit(predicted.get("entries") or [])
    if not records:
        click.echo("overlap predictions: (none recorded)")
        return

    boards: dict[str, Any] = {}
    diffs: dict[tuple[str, str], list[str] | None] = {}

    def actual_files(repo_name: str, issue_number: int) -> list[str] | None:
        coordinates = _repo_coordinates(config_path, repo_name)
        if coordinates is None:
            return None
        repo_github, base_branch = coordinates
        if repo_name not in boards:
            try:
                from coord.board_service import read_board  # noqa: PLC0415

                boards[repo_name] = read_board()
            except Exception:  # noqa: BLE001 — unknown, never "no overlap"
                boards[repo_name] = None
        board = boards[repo_name]
        if board is None:
            return None
        branch = _branch_index(board, repo_name).get(int(issue_number))
        if not branch:
            return None
        cache_key = (repo_github, branch)
        if cache_key not in diffs:
            try:
                from coord import github_ops  # noqa: PLC0415

                diffs[cache_key] = github_ops.get_compare_files(
                    repo_github, base_branch, branch
                )
            except Exception:  # noqa: BLE001 — unknown, never "no overlap"
                diffs[cache_key] = None
        return diffs[cache_key]

    rows: list[dict[str, Any]] = []
    for record in records:
        other = parse_key(record["other_key"])
        outcome = OUTCOME_UNKNOWN
        if other is not None:
            outcome = classify_outcome(
                record["files"],
                actual_files(record["repo"], record["issue"]),
                actual_files(other[0], other[1]),
            )
        rows.append({**record, "outcome": outcome})
        if outcome == OUTCOME_UNKNOWN:
            continue
        if (record["key"], record["other_key"]) in scored_before:
            continue
        record_audit(
            tier="business",
            category=AUDIT_CATEGORY,
            event_type=EVENT_SCORED,
            actor="drive-queue",
            summary=f"{record['key']} ordered after {record['other_key']}: {outcome}",
            repo=record["repo"],
            issue=record["issue"],
            details={
                "key": record["key"],
                "other_key": record["other_key"],
                "outcome": outcome,
                "predicted_files": record["files"],
                "source": record["source"],
            },
        )

    accuracy = tally(r["outcome"] for r in rows)
    if output_json:
        click.echo(
            _json.dumps(
                {
                    "rows": rows,
                    "confirmed": accuracy.confirmed,
                    "false_positive": accuracy.false_positive,
                    "unknown": accuracy.unknown,
                    "precision": accuracy.precision,
                }
            )
        )
        return
    click.echo(f"overlap predictions: {accuracy.render()}")
    for row in rows:
        files = ", ".join(row["files"][:3]) or "(none)"
        click.echo(
            f"  {row['key']} after {row['other_key']} [{row['source']}] "
            f"{row['outcome']} — {files}"
        )


# ── resume (#1757) ───────────────────────────────────────────────────────────


@drive_queue_group.command("resume")
@click.argument("repo", required=False)
@click.argument("issue", type=int, required=False)
@_CONFIG_OPTION
def drive_queue_resume(repo: str | None, issue: int | None, config_path: Path) -> None:
    """Release a fired deploy gate so the next tick can launch again.

    With no arguments this releases every held gate — in practice there is at
    most one, because a held queue launches nothing and therefore cannot reach
    a second one. Pass REPO ISSUE to name a specific entry.

    The entry itself is NOT removed or re-run: the release is what unblocks
    the queue, not the held entry leaving it, so `list` keeps its run history.
    """
    from coord.state import list_drive_queue, update_drive_queue_entry  # noqa: PLC0415

    held = fired_holds(entries_from_rows(list_drive_queue()))
    if repo is not None:
        if issue is None:
            raise click.ClickException("give both REPO and ISSUE, or neither")
        wanted = entry_key(repo, issue)
        held = [e for e in held if e.key == wanted]
        if not held:
            raise click.ClickException(
                f"{wanted} has no fired deploy gate to release "
                "(see `coord drive-queue status`)"
            )
    if not held:
        # Exit non-zero: "resume" on a queue that was never held is an
        # operator misreading the board, and a silent success would confirm
        # the misreading.
        raise click.ClickException("no deploy gate is currently held")

    for entry in held:
        update_drive_queue_entry(
            entry.repo, entry.issue, hold_state=HOLD_RELEASED, hold_probes=0
        )
        click.echo(f"released the deploy gate on {entry.key}")
    _clear_queue_alert()
    click.echo("the next tick will launch the next eligible entry")


def _clear_queue_alert() -> None:
    """Drop the queue-level HELD alert once its gate is released.

    Best-effort: the next tick overwrites (or re-raises) this record anyway,
    but leaving a stale "QUEUE HELD" sitting in `status` between the release
    and the next timer fire is exactly the kind of contradiction that trains
    an operator to stop reading alerts.
    """
    try:
        from coord.state import dismiss_drive_escalation  # noqa: PLC0415

        dismiss_drive_escalation(QUEUE_ALERT_REPO, QUEUE_ALERT_ISSUE)
    except Exception:  # noqa: BLE001 — cosmetic; never fail a release on it
        pass


# ── board-read retry (#2159) ─────────────────────────────────────────────────
#
# A transient SQLite "database is locked" / SQLITE_BUSY on the board read is
# not evidence the board is broken — it is evidence something else (the
# daemon's own tick loop, `coord notify`, the web dashboard, another machine's
# drive-queue tick) was mid-write on the same DB at the exact instant this
# tick asked to read it. The read is idempotent — nothing has been decided
# yet — so retrying it a few times over a couple of seconds is strictly safer
# than the alternative: aborting the whole tick and leaving
# `coord-drive-queue.service` sitting `failed` over a one-off, millisecond-
# wide race (2026-08-12, dellserver, one tick in a 14-hour window).
#
# `coord.db._open` already sets `PRAGMA busy_timeout=5000` on the connection
# the local (daemon-host) board read goes through, so SQLite itself already
# retries internally for up to 5s before ever raising — this loop is the
# belt-and-braces layer above that: it also covers a thin client's board read
# (an HTTP round trip to the daemon, which cannot be taught a SQLite pragma)
# and the case where 5s of internal retry still wasn't enough. Any OTHER read
# failure — the daemon is genuinely unreachable, a malformed payload, a real
# non-transient error — does not match `_is_db_locked_error` and is re-raised
# on the FIRST attempt: retrying those would only delay the fail-closed abort
# this module's docstring requires.
_BOARD_READ_RETRY_ATTEMPTS = 3
_BOARD_READ_RETRY_BACKOFF_SECONDS = (0.5, 1.0)  # 2 sleeps across 3 attempts, ~2s total


def _is_db_locked_error(exc: BaseException) -> bool:
    """``True`` for the transient "database is locked" / SQLITE_BUSY class.

    Matched on the exception's message rather than restricted to
    ``sqlite3.OperationalError``: a thin client's board read goes through
    ``httpx`` against the daemon's ``GET /board``, so a locked DB on the
    daemon's end can surface here wrapped in whatever exception carries the
    daemon's error detail, not necessarily a local ``sqlite3`` exception.
    Every other read failure lacks this text and is unaffected.
    """
    return "database is locked" in str(exc).lower() or "sqlite_busy" in str(exc).lower()


def _fetch_board_view_with_retry() -> BoardView:
    """:func:`_fetch_board_view`, with a bounded retry for lock contention.

    Up to :data:`_BOARD_READ_RETRY_ATTEMPTS` reads, backing off
    :data:`_BOARD_READ_RETRY_BACKOFF_SECONDS` between them — but ONLY while
    the failure matches :func:`_is_db_locked_error`; anything else is
    re-raised immediately so the caller's existing fail-closed abort is
    unchanged. Logs once, at the tick level, when a retry actually recovered
    the read, so the contention stays visible rather than silently smoothed
    over — never once when the very first attempt already succeeded.
    """
    last_exc: Exception | None = None
    for attempt in range(_BOARD_READ_RETRY_ATTEMPTS):
        try:
            board = _fetch_board_view()
        except Exception as exc:  # noqa: BLE001 — re-raised below when not retryable
            if not _is_db_locked_error(exc) or attempt == _BOARD_READ_RETRY_ATTEMPTS - 1:
                raise
            last_exc = exc
            time.sleep(
                _BOARD_READ_RETRY_BACKOFF_SECONDS[
                    min(attempt, len(_BOARD_READ_RETRY_BACKOFF_SECONDS) - 1)
                ]
            )
            continue
        if last_exc is not None:
            click.echo(
                f"board read hit lock contention and recovered after {attempt} "
                f"retry(ies) (last error: {last_exc})"
            )
        return board
    raise AssertionError("unreachable — the loop above always returns or raises")


# ── tick ─────────────────────────────────────────────────────────────────────


def _local_issue_rows() -> list[dict]:
    """``issues`` rows straight from the local DB (daemon-host path only).

    ``BoardFetcher`` builds the standalone payload with
    ``coord.client.serialize_board``, which ships assignment rows and
    ``round_number`` and nothing else — no ``issues`` key at all.  The daemon's
    own ``GET /board`` (``coord.dao.board_projection``) DOES carry one, so a
    thin client already sees issue open/closed state and the daemon host would
    not.  Without this top-up a pre-req that is simply open-and-undispatched
    would look "unknown to the board" on the exact machine a systemd timer runs
    the tick on, and get blocked instead of deferred.

    Fail-soft: an unreadable/absent table degrades to ``[]``, which puts the
    daemon host back on the assignment-only signals rather than aborting.
    """
    from coord import sql  # noqa: PLC0415
    from coord.db import get_connection  # noqa: PLC0415

    try:
        rows = sql.execute(
            get_connection(), "SELECT repo_name, number, state FROM issues"
        ).fetchall()
    except Exception:  # noqa: BLE001 — see the fail-soft note above
        return []
    return [dict(r) for r in rows]


def _local_merge_queue_rows() -> list[dict]:
    """``merge_queue`` rows straight from the local DB (daemon-host path only).

    Same gap as :func:`_local_issue_rows`, one table over: the standalone
    ``coord.client.serialize_board`` payload ships assignment rows and
    ``round_number`` only — no ``merge_queue`` (and no ``merge_plan``, which
    is computed, not stored) — so on the daemon host itself (no
    ``board_service`` configured, the tick reads the local DB directly)
    :func:`build_board_view`'s ``merge_ci_pending`` fact (#1891) would never
    see a checks-still-pending entry at all. ``merge_plan`` is deliberately
    NOT backfilled here — it needs a live ``config``/``ci_store`` to compute,
    and ``build_board_view`` already falls back to this raw table's ``error``
    column when the plan section is absent, exactly the fallback
    ``drive_state._merge_entry`` uses for the same gap.

    Fail-soft: an unreadable table degrades to ``[]``, same posture as
    :func:`_local_issue_rows`.
    """
    from coord import sql  # noqa: PLC0415
    from coord.db import get_connection  # noqa: PLC0415

    try:
        rows = sql.execute(
            get_connection(), "SELECT repo_name, issue_number, error FROM merge_queue"
        ).fetchall()
    except Exception:  # noqa: BLE001 — see the fail-soft note above
        return []
    return [dict(r) for r in rows]


def _local_host_id() -> str:
    """This machine's identity for #1870's launch-host / reconcile matching.

    Same normalisation every other host-locality check in this codebase uses
    (``coord/commands/sessions.py``, ``coord/commands/_common.py``,
    ``coord.interactive._get_local_short_hostname``): the short hostname,
    lowercased, domain suffix dropped — so a machine addressed as
    ``dellserver`` in one config and ``dellserver.local`` by DNS still
    compares equal to itself.
    """
    return socket.gethostname().split(".")[0].lower()


def _fetch_cordons() -> dict[str, str]:
    """``{machine: "cordoned: draining for v0.5.31"}`` (#2101).

    Daemon-aware (`coord.machine_pause.cordons()` routes to `GET /pause` on a
    thin client) and fail-SOFT: an unreadable cordon store degrades to "no
    cordons", the same posture `paused_set()` takes on every dispatch decision
    in this codebase. That is the right trade here even though the board fetch
    above fails CLOSED: a missed cordon costs one drive launched into a host
    about to roll (which `coord agent update` then refuses, leaving the entry
    to retry), whereas failing the whole tick closed on a cordon read would
    stop the queue on a network blip — the outage this issue exists to end,
    reintroduced by its own fix.
    """
    from coord.machine_pause import cordons as fetch_cordons  # noqa: PLC0415

    try:
        return {name: record.describe() for name, record in fetch_cordons().items()}
    except Exception as exc:  # noqa: BLE001 — see docstring
        click.echo(f"warning: could not read release cordons ({exc}) — "
                   "treating the fleet as uncordoned", err=True)
        return {}


def _fetch_editable_drift() -> tuple[str, str] | None:
    """``(repo_root, shown)`` when THIS host's own `coord` is an editable
    checkout that has drifted off its default branch, else ``None`` (#2314).

    Thin wrapper around ``coord.cli._editable_checkout_drift`` — the exact
    same read `coord.cli._warn_if_editable_checkout_moved` uses for its
    CLI-startup warning — reshaped to a plain ``(str, str)`` tuple so
    :func:`coord.drive_queue.plan_tick` (a pure function; see its module
    docstring) never has to import ``coord.cli`` or touch a ``Path``.
    Already fail-soft/best-effort at the source (see that function's
    docstring); this wrapper adds nothing on top beyond the reshape and the
    ``str(repo_root)`` conversion.

    Same ``"pytest" in sys.modules`` guard as
    ``_warn_if_editable_checkout_moved`` — for the identical reason: this
    repo's OWN test suite runs from an editable checkout on a feature
    branch as a matter of course (that is what a worker's worktree is), so
    without the guard every CLI-level `drive-queue tick` test would trip
    this exact gate on itself. `coord.drive_queue.plan_tick`'s own tests
    (tests/test_drive_queue.py) exercise the real gate logic directly via
    its ``editable_drift`` parameter, unaffected by this guard.
    """
    import sys as _sys  # noqa: PLC0415

    if "pytest" in _sys.modules:
        return None

    from coord.cli import _editable_checkout_drift  # noqa: PLC0415

    drift = _editable_checkout_drift()
    if drift is None:
        return None
    repo_root, shown = drift
    return (str(repo_root), shown)


# ── #2587: roll-pending marker store ──────────────────────────────────────────
#
# A plain JSON file, not a `board_meta` DB row: every reader/writer —
# `coord release propagate` / `coord release nightly-window`
# (`coord/commands/release.py`), this module's own `tick`, and `coord notify`
# (`coord/notify.py`) — runs ONLY on the daemon host in production
# (`deploy/coord-release-window.service`'s and `deploy/coord-notify.service`'s
# own headers both say so: `coord-drive-queue.timer`/`coord.db` exist there and
# nowhere else). There is therefore exactly one process family ever touching
# this file, on exactly one machine, and no daemon-routing/thin-client seam
# (`coord.board_service`) to plug into — the same reasoning
# `coord.machine_pause`'s local pause/cordon store predates its own daemon-aware
# half. Kept a standalone file (mirroring `coord.machine_pause`'s
# `paused_machines.json`) rather than a new `board_meta` key: unlike a cordon or
# a milestone drain, at most one roll is ever pending at a time, so there is no
# list to keep, and it lets `coord release propagate`/`nightly-window` set this
# marker without importing `coord.state`'s DB machinery at all.
_ROLL_PENDING_FILENAME = "roll_pending.json"


def roll_pending_path() -> Path:
    """Absolute path to the roll-pending marker file.

    ``$COORD_ROLL_PENDING_STATE`` overrides it — the same seam
    ``coord.notifier.store.state_path`` uses, so a test redirects this store
    with one env var rather than monkeypatching a private function
    (``_no_real_pause_store``'s (#2101) heavier-weight approach, from before
    this seam existed). Never let a test write the OPERATOR'S real
    ``~/.coord/roll_pending.json`` — a state file a test *can* write is a
    state file a test *will* write, and this one gates whether the daemon
    host's drive queue launches anything at all.
    """
    import os  # noqa: PLC0415

    from coord.platform_paths import default_coord_dir  # noqa: PLC0415

    override = os.environ.get("COORD_ROLL_PENDING_STATE")
    if override:
        return Path(override).expanduser()
    return default_coord_dir() / _ROLL_PENDING_FILENAME


def read_roll_pending() -> RollPending | None:
    """The current marker, or ``None`` when no roll is pending.

    Fail-soft on a missing/corrupt/hand-edited file — same posture
    :func:`_fetch_cordons` takes on an unreadable cordon store, and for the
    same reason: a marker this read cannot make sense of must read as "no
    roll pending" (resume launching), never as a reason to stop the tick.
    """
    path = roll_pending_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        click.echo(
            f"warning: could not read {path} ({exc}) — treating no roll as pending",
            err=True,
        )
        return None
    if not raw.strip():
        return None
    try:
        data = _json.loads(raw)
    except ValueError:
        click.echo(f"warning: {path} is not valid JSON — ignoring it", err=True)
        return None
    if not isinstance(data, dict) or not data:
        return None
    try:
        return RollPending.from_dict(data)
    except ValueError as exc:
        click.echo(f"warning: {path} is not a usable roll-pending record ({exc})", err=True)
        return None


def write_roll_pending(pending: RollPending) -> None:
    """Persist *pending*, overwriting whatever was there before.

    Used both for the INITIAL set (`coord release propagate`/
    `nightly-window`) and for this module's own re-write after bumping
    ``deferrals`` each tick it does not fire the roll.

    Atomic tempfile-then-rename — the same pattern
    `coord.machine_pause._save_state` uses for its own shared JSON state
    file. This one has THREE independent process families writing it (this
    module's own tick, and `coord release propagate`/`nightly-window`) with
    no cross-process lock between them (#2587 review) — a plain
    ``write_text`` is a real torn-read window a concurrent
    :func:`read_roll_pending` could land in, even though that reader's
    fail-soft parse degrades a torn read to "no marker" rather than raising.
    """
    import os  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    path = roll_pending_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _json.dumps(pending.to_dict(), sort_keys=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".roll_pending.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def clear_roll_pending() -> None:
    """Drop the marker. A no-op (not an error) when nothing was pending."""
    path = roll_pending_path()
    try:
        path.unlink()
    except FileNotFoundError:
        pass


# ── #2889: the roll ledger — memory that survives a marker's own clear ──────

_ROLL_LEDGER_FILENAME = "roll_pending_ledger.json"


def roll_pending_ledger_path() -> Path:
    """Absolute path to the #2889 roll ledger.

    ``$COORD_ROLL_PENDING_LEDGER_STATE`` overrides it — same test-isolation
    seam as :func:`roll_pending_path`; never let a test write the operator's
    real ``~/.coord/roll_pending_ledger.json``.
    """
    import os  # noqa: PLC0415

    from coord.platform_paths import default_coord_dir  # noqa: PLC0415

    override = os.environ.get("COORD_ROLL_PENDING_LEDGER_STATE")
    if override:
        return Path(override).expanduser()
    return default_coord_dir() / _ROLL_LEDGER_FILENAME


def read_roll_ledger() -> RollLedger:
    """The current ledger — never ``None``: an absent/corrupt/hand-edited
    file reads as a fresh, empty ``RollLedger()``, the same fail-soft
    posture :func:`read_roll_pending` takes on its own file, and for the
    same reason — a ledger this cannot make sense of must never wedge future
    arms forever.
    """
    path = roll_pending_ledger_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return RollLedger()
    if not raw.strip():
        return RollLedger()
    try:
        data = _json.loads(raw)
    except ValueError:
        return RollLedger()
    if not isinstance(data, dict):
        return RollLedger()
    return RollLedger.from_dict(data)


def write_roll_ledger(ledger: RollLedger) -> None:
    """Persist *ledger*, overwriting whatever was there before. Same atomic
    tempfile-then-rename pattern as :func:`write_roll_pending`."""
    import os  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    path = roll_pending_ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _json.dumps(ledger.to_dict(), sort_keys=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".roll-ledger.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def reset_roll_ledger() -> None:
    """Drop the ledger — a CONFIRMED roll or explicit operator intervention
    (`coord drive-queue cancel-roll`) both mean "clean slate": whatever ran
    up the cumulative bound is over. A no-op when there was nothing to
    reset."""
    path = roll_pending_ledger_path()
    try:
        path.unlink()
    except FileNotFoundError:
        pass


#: #2889's own escalation key — deliberately distinct from
#: `ROLL_PENDING_ALERT_STAGE` (a single expired marker) so the two can
#: coexist as separate rows: a marker expiring is routine (bounded by design,
#: #2587's whole point); the LEDGER crossing its cumulative bound is the
#: escalation that means "this has now failed to roll unattended several
#: times in a row — an operator needs to look, not just wait for the next
#: gap."
ROLL_LEDGER_ALERT_REPO = "(roll-pending)"
ROLL_LEDGER_ALERT_ISSUE = 0
ROLL_LEDGER_ALERT_STAGE = "roll-ledger-escalated"


def _escalate_roll_ledger(ledger: RollLedger, *, now: float) -> None:
    """Surface a ledger that just crossed its cumulative bound (#2889 item
    1's "escalate loudly") — mirrors `_escalate_roll_pending_expired` one
    level up: that one is routine (a single marker hit ITS OWN bound, exactly
    as designed); this one means the SAME target has now failed to roll
    unattended across `ledger.marker_count` separate marker generations, and
    arming a fresh one is refused until `coord drive-queue cancel-roll`
    clears this ledger.
    """
    from coord.state import record_drive_escalation  # noqa: PLC0415

    reason = (
        f"roll-pending ledger escalated: {ledger.cumulative_frozen_seconds:.0f}s "
        f"cumulative frozen time across {ledger.marker_count} marker "
        "generation(s), none of them confirming a roll — refusing to arm a "
        "fresh marker until `coord drive-queue cancel-roll` clears this "
        "ledger (#2889: a per-marker TTL bounds one marker; this bounds the "
        "RATE of fresh ones that follow)"
    )
    click.echo(f"warning: {reason}", err=True)
    try:
        record_drive_escalation(
            ROLL_LEDGER_ALERT_REPO,
            ROLL_LEDGER_ALERT_ISSUE,
            stage=ROLL_LEDGER_ALERT_STAGE,
            reason=reason,
            gate_readings=(
                f"cumulative_frozen_seconds={ledger.cumulative_frozen_seconds:.0f} | "
                f"bound={ROLL_LEDGER_CUMULATIVE_BOUND_SECONDS:.0f} | "
                f"marker_count={ledger.marker_count}"
            ),
            proposed_command="coord drive-queue cancel-roll",
        )
    except Exception as exc:  # noqa: BLE001 — the stderr line above is the
        # floor; an escalation table that cannot be written must not take
        # the message down with it.
        click.echo(f"  (could not record the roll-ledger escalation: {exc})", err=True)


def _roll_pending_may_fire(*, occupied: int, now: float, queue_rows) -> tuple[bool, str]:
    """Is it worth attempting to fire a pending roll THIS tick? (#2870 part 2)

    #2587's original rule was ``occupied == 0`` — this tick's own
    reconciliation (`_reconcile_running`'s ``occupies`` verdict) found
    literally nothing left running. That rule never learned #2854: `coord
    release propagate`'s own quiescence check
    (:func:`coord.release_propagate.assess_quiescence`) treats a `running`
    drive-queue row with no LIVE assignment right now, past its settle
    window, as genuinely idle — the row just hasn't reached a terminal state
    yet (nothing has reconciled it to ``done``). This tick's own ``occupied``
    count has no such reading: a between-legs row launched on a different
    host reads ``unknown`` here (#1870 — this tick's local tmux check proves
    nothing about a row launched elsewhere) and stays charged, forever,
    against an ``occupied == 0`` bar it can never clear. The 2026-08-28
    incident this closes: `coord release propagate`, run directly, reported
    ``quiescent — nothing in flight`` (between-legs, past the settle window)
    for the SAME row this tick's own ``occupied`` reading was still counting
    — so the tick never even ATTEMPTED the roll `coord release propagate`
    would have accepted.

    ``occupied == 0`` stays the cheap fast path — no extra board read. Only
    when it is nonzero does this re-derive the SAME between-legs/settle-
    window reading `coord release propagate` itself makes, from a fresh
    board fetch. This is an ADVISORY re-check, not the authoritative one:
    firing merely hands off to `coord-release-window.service`, which invokes
    `coord release propagate` for real (never `--force`) and simply defers
    again — costing nothing — if this read turns out to have been too
    optimistic. So there is no harm in checking, and every reason to: the
    alternative is a marker that can sit past its own between-legs row
    forever with `coord drive-queue status` reporting `alert: (none)` the
    whole time, because from the queue's own point of view nothing is wrong
    — it is waiting on a roll, exactly as designed.

    *queue_rows* are the SAME raw `drive_queue` rows this tick already read
    (`coord.state.list_drive_queue()`) — reused rather than re-fetched, and
    passed straight through as `assess_quiescence`'s ``queue_entries``: that
    function reads the raw ``state``/``repo_name``/``issue_number`` columns,
    not `QueueEntry` or `BoardView`'s reduced shapes.

    A board this cannot read degrades to "no, don't fire" (never raises) —
    the tick's own fail-closed abort already covers a board it cannot read
    for reconciliation; a re-check that itself cannot read the board must
    not provoke a fire it cannot justify.
    """
    if occupied == 0:
        return True, ""
    from coord import release_propagate as rp  # noqa: PLC0415

    try:
        payload = _fetch_board_payload()
    except Exception:  # noqa: BLE001 — see docstring: fail closed, never raise
        return False, ""
    quiescence = rp.assess_quiescence(
        queue_entries=queue_rows,
        assignments=payload.get("assignments") or [],
        issues=payload.get("issues") or [],
        now=now,
    )
    return quiescence.quiescent, quiescence.reason


def _fire_pending_roll(*, dry_run: bool = False) -> tuple[bool, str]:
    """Best-effort ``systemctl --user start --no-block
    coord-release-window.service`` (#2587 design point 3).

    ``--no-block`` (a.k.a. ``--job-mode=fail`` without ``--wait``, the
    systemctl default) is the whole point: this must return almost instantly
    and hand the actual roll off to a unit that outlives THIS tick's process
    tree, never a synchronous, in-process ``coord release propagate`` call —
    the roll swaps the venv the tick is executing from, so it must not be a
    child of the tick's own cgroup or share its lifetime. See
    ``deploy/coord-release-window.service`` for what that unit actually does.

    Returns ``(fired, detail)``, never raises — a `systemctl` this host
    cannot run (no systemd, PATH issue, unit not installed) must be a
    deferral the marker survives to retry next tick, not a crashed tick.

    #2889 item 4: ``--setenv=COORD_ROLL_INVOKER=drive-queue-tick`` tags THIS
    specific start with a caller identity `coord release nightly-window`
    (the spawned unit's `ExecStart=`) reads back and journals as
    `WindowRecord.invoked_by` — overriding the unit file's own static
    `Environment=` default for just this one invocation (systemd layers a
    transient `--setenv=` on top of a unit's static `Environment=`), so
    "what invoked this run" is answerable from `coord release
    window-history` instead of requiring a live reproduction.
    """
    if dry_run:
        return True, "--dry-run: would run `systemctl --user start --no-block coord-release-window.service`"
    try:
        proc = subprocess.run(  # noqa: S603, S607 — fixed argv, no shell
            [
                "systemctl", "--user", "start", "--no-block",
                "--setenv=COORD_ROLL_INVOKER=drive-queue-tick",
                "coord-release-window.service",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if proc.returncode == 0:
        return True, "started coord-release-window.service"
    detail = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
    return False, detail


def _bump_roll_pending_deferral(pending: RollPending) -> RollPending:
    """*pending* with ``deferrals`` incremented by one — every OTHER field
    (including ``set_at``, deliberately never refreshed) unchanged.

    Called once per tick this marker survives without firing — the tick-count
    half of #2587's bound (see `ROLL_PENDING_DEFAULT_MAX_DEFERRALS`'s
    comment). `set_at` stays frozen at the ORIGINAL set time so the TTL half
    of the bound measures real wall-clock age, not "time since last bumped" —
    a marker re-armed every tick would otherwise never age out on the clock
    at all, defeating the whole point of having two independent bounds.
    """
    import dataclasses as _dataclasses  # noqa: PLC0415

    return _dataclasses.replace(pending, deferrals=pending.deferrals + 1)


#: #2587's own escalation key — deliberately NOT `QUEUE_ALERT_REPO`/
#: `QUEUE_ALERT_ISSUE`: that single slot is overwritten (or cleared via
#: `_clear_queue_alert()`) by THIS SAME tick's `plan.alert` handling further
#: down, which would erase an expiry record the instant it was written (the
#: marker is already cleared by the time that code runs, so `plan.alert` is
#: back to whatever the rest of the tick decided). Mirrors
#: `coord.commands.release`'s `WINDOW_ALERT_REPO`/`WINDOW_ALERT_ISSUE`/
#: `WINDOW_ALERT_STAGE` — same shape, same reasoning, one escalation slot per
#: independent "this must be loud" condition.
ROLL_PENDING_ALERT_REPO = "(roll-pending)"
ROLL_PENDING_ALERT_ISSUE = 0
ROLL_PENDING_ALERT_STAGE = "roll-pending"


def _escalate_roll_pending_expired(pending: RollPending, *, now: float) -> None:
    """Surface an expired roll-pending marker loudly (#2587's own "never
    silently held" requirement — mirrors `coord.commands.release.
    _escalate_window`'s "a skipped/failed night must be loud" reasoning, one
    level up: here it is a roll that never got its window, not a night that
    never rolled).
    """
    from coord.state import record_drive_escalation  # noqa: PLC0415

    age_seconds = max(0.0, now - pending.set_at)
    reason = (
        f"roll-pending for v{pending.target_version} expired without reaching "
        f"a quiescent window ({age_seconds:.0f}s elapsed / {pending.deferrals} "
        "tick(s) deferred) — resuming normal launching (#2587: a pending "
        "roll must never hold the queue down indefinitely)"
    )
    click.echo(f"warning: {reason}", err=True)
    try:
        record_drive_escalation(
            ROLL_PENDING_ALERT_REPO,
            ROLL_PENDING_ALERT_ISSUE,
            stage=ROLL_PENDING_ALERT_STAGE,
            reason=reason,
            gate_readings=(
                f"target_version={pending.target_version} | reason={pending.reason} | "
                f"ttl_seconds={pending.ttl_seconds} | "
                f"deferrals={pending.deferrals}/{pending.max_deferrals}"
            ),
            proposed_command=(
                f"coord release propagate --target {pending.target_version}"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — the stderr line above is the
        # floor; an escalation table that cannot be written must not take
        # the message down with it.
        click.echo(f"  (could not record the roll-pending escalation: {exc})", err=True)


# ── #2572: escalate a PERSISTENT self-cordon ────────────────────────────────
#
# #2569/#2570: from 03:43 to 04:23 this tick correctly refused to launch
# every single time (`_editable_drift_alert`'s well-written "no launch —
# this host's coord is an editable checkout ... not its default branch"
# message) and correctly re-recorded that SAME alert into `drive_escalations`
# every tick — but `record_drive_escalation`'s own
# `ON CONFLICT ... DO UPDATE SET ... created_at=excluded.created_at` means
# every one of those re-records resets the row's own timestamp, so nothing
# about the alert itself distinguishes "just noticed" from "been true for 40
# minutes". The only thing that was ever going to turn that alert into a
# phone push was a SEPARATE process — `coord notifier`'s own tick, reading
# this same table — and that process was dead from the identical root cause
# (#2570). This tick, unlike that one, kept running (successfully, on
# drifted code, but running) the entire window — so it is the one process
# that could have said something and didn't.
#
# This closes that gap: track how long the CURRENT drift reason has held,
# independent of the routine per-tick re-record above, and once it crosses
# `SELF_CORDON_ESCALATE_AFTER_SECONDS` push directly from THIS process —
# never through the notifier — so a dead notifier tick is no longer a single
# point of failure for this one condition. A plain JSON marker file, same
# pattern as `roll_pending.json` (`RollPending`) one level up: at most one
# self-cordon reason is ever active at a time, keyed by a wall-clock
# `first_seen_at` this module controls directly rather than trusting
# `drive_escalations.created_at`.
_SELF_CORDON_STATE_FILENAME = "self_cordon_escalation.json"

#: How long the SAME drift reason must persist, unbroken, before this tick
#: pushes its own direct escalation. ~30 min per #2572 — long enough that a
#: transient/one-tick drift (a build, or an interactive `coord test` run
#: briefly checking out a branch on the shared checkout — see
#: `_fetch_editable_drift`'s docstring) never pages, short enough that it
#: still catches the 2026-08-22 incident's 40-minute window well before the
#: 11-hour outage it was part of.
SELF_CORDON_ESCALATE_AFTER_SECONDS = 1800

#: #2572's own escalation key — deliberately NOT `QUEUE_ALERT_REPO`/
#: `QUEUE_ALERT_ISSUE` (the routine per-tick `plan.alert` record, rewritten
#: every tick — see above) and NOT `ROLL_PENDING_ALERT_REPO`: a distinct slot
#: so this escalation persists independently of whatever the rest of the
#: tick's ordinary alert handling does to its own key.
SELF_CORDON_ALERT_REPO = "(drive-queue-self-cordon)"
SELF_CORDON_ALERT_ISSUE = 0
SELF_CORDON_ALERT_STAGE = "self-cordon"


def _self_cordon_state_path() -> Path:
    """Absolute path to the self-cordon persistence marker.

    ``$COORD_SELF_CORDON_STATE`` overrides it — same test-isolation seam as
    :func:`roll_pending_path`; never let a test touch the operator's real
    ``~/.coord/self_cordon_escalation.json``.
    """
    import os  # noqa: PLC0415

    from coord.platform_paths import default_coord_dir  # noqa: PLC0415

    override = os.environ.get("COORD_SELF_CORDON_STATE")
    if override:
        return Path(override).expanduser()
    return default_coord_dir() / _SELF_CORDON_STATE_FILENAME


def _read_self_cordon_state() -> dict | None:
    """The current marker, or ``None``.

    Fail-soft on anything unreadable — same posture as
    :func:`read_roll_pending`: a marker this can't parse must read as "no
    persistence tracked yet", never as a reason to stop the tick or raise.
    """
    path = _self_cordon_state_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return None
    if not raw.strip():
        return None
    try:
        data = _json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _write_self_cordon_state(data: dict) -> None:
    path = _self_cordon_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json.dumps(data, sort_keys=True), encoding="utf-8")
    except OSError:
        pass


def _clear_self_cordon_state() -> None:
    try:
        _self_cordon_state_path().unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _push_self_cordon_escalation(
    reason: str, *, age_seconds: float, config_path: Path | None
) -> bool:
    """The actual out-of-band push (#2572) — direct from THIS process, never
    routed through `coord notifier`'s own tick.

    Records into ``drive_escalations`` under :data:`SELF_CORDON_ALERT_REPO`
    (so ``coord drive-queue status`` / ``list_drive_escalations`` / the
    TUI's escalations panel show it like any other row), AND attempts a live
    ntfy push using the SAME transport/config `coord notifier` uses
    (``notifications:`` in coordinator.yml) — best-effort, never raises: a
    broken import/config or a failed record never takes the tick down with
    it, same isolation rule docs/NOTIFIER.md states for the notifier's own
    tick.

    Returns whether the caller may treat this occurrence as "escalated"
    (mark ``escalated_at`` so it does not fire again): ``True`` when the
    ntfy push actually landed, or when there was no push to *attempt*
    because notifications are disabled/unconfigured — a legitimate reason,
    not a failure. ``False`` when a push was attempted and failed (a
    transient ntfy outage, a raised exception building/sending it), so the
    caller leaves the marker unescalated and the next tick retries — same
    policy `coord.notifier.service.deliver`'s docstring states: "A failed
    send is not ledgered... an ntfy server that was down for an hour costs
    a delayed notification, not a lost one." Recording this push as
    escalated regardless of outcome would forfeit it permanently the moment
    a transient failure lined up with the 30-minute threshold — precisely
    the "the one channel that was supposed to reach a human silently
    didn't" failure class #2572 exists to close, one layer down.

    Deliberately bypasses quiet hours (unlike the notifier's own digest
    path, `coord.notifier.digest`) — this fires at most once per persisted
    incident, only after 30 minutes of an unbroken self-cordon, which is
    exactly the class of "time-critical, the system knows and the operator
    doesn't" event #1632's own quiet-hours doc calls out as the deliberate
    exception, not a routine notification an operator would want batched
    until morning.
    """
    from coord.state import record_drive_escalation  # noqa: PLC0415

    detail = (
        f"self-cordon has held for {age_seconds / 60:.0f}+ minutes with the "
        f"same reason and no operator action recorded: {reason}"
    )
    try:
        record_drive_escalation(
            SELF_CORDON_ALERT_REPO,
            SELF_CORDON_ALERT_ISSUE,
            stage=SELF_CORDON_ALERT_STAGE,
            reason=detail,
            gate_readings=f"age_seconds={age_seconds:.0f} | reason={reason}",
            proposed_command="git -C <repo_root> checkout main",
        )
    except Exception as exc:  # noqa: BLE001 — an escalation table that
        # cannot be written must not take the message down with it (mirrors
        # `_escalate_roll_pending_expired`'s identical guard just above this
        # function, and this function's own docstring promise to "never
        # raise").
        click.echo(f"  (could not record the self-cordon escalation: {exc})", err=True)
    click.echo(f"warning: {detail}", err=True)

    try:
        from coord.commands._common import _load_config  # noqa: PLC0415
        from coord.notifier.models import Message  # noqa: PLC0415
        from coord.notifier.transport import build_transport, safe_send  # noqa: PLC0415

        cfg = _load_config(config_path)
        notif = getattr(cfg, "notifications", None)
        if notif is None or not getattr(notif, "enabled", False):
            # Nothing configured to push through — a legitimate reason not
            # to escalate-and-retry, same as `build_transport`'s own
            # "half-configured notifier is a notifier that says nothing"
            # fallback.
            return True
        transport = build_transport(notif)
        result = safe_send(
            transport,
            Message(
                title="coord drive-queue: stuck self-cordon",
                body=detail,
                tags=("rotating_light",),
                priority=4,
            ),
        )
        if not result.ok:
            click.echo(
                "  (self-cordon escalation push failed, will retry next "
                f"tick: {result.error or 'unknown transport failure'})",
                err=True,
            )
            return False
        return True
    except Exception as exc:  # noqa: BLE001 — this is an ADDITION on top of
        # the drive_escalations record above, which has already been
        # attempted by this point; a broken import/config here must never
        # take the tick down with it (same isolation rule docs/NOTIFIER.md
        # states for the notifier's own tick) — but it also means the push
        # itself never happened, so treat it like any other failed send:
        # retry next tick rather than forfeiting it silently.
        click.echo(f"  (could not push the self-cordon escalation: {exc})", err=True)
        return False


def _escalate_persistent_self_cordon(
    drift_reason: str, *, now: float, config_path: Path | None
) -> None:
    """Track how long *drift_reason* (``plan.drift_reason`` — empty when no
    self-cordon is active) has held, unbroken, and push once it crosses
    :data:`SELF_CORDON_ESCALATE_AFTER_SECONDS` (#2572).

    Deliberately separate from the routine `plan.alert` recording a few
    lines below this function's call site (which re-records the SAME
    reason, with a refreshed timestamp, every single tick — see this
    module's header comment above `_SELF_CORDON_STATE_FILENAME` for why
    that can't answer "how long has this been going on").
    """
    if not drift_reason:
        _clear_self_cordon_state()
        return

    state = _read_self_cordon_state()
    if not isinstance(state, dict) or state.get("reason") != drift_reason:
        # First tick with this exact reason (or the very first self-cordon
        # ever recorded on this host) — start the clock, nothing to push yet.
        _write_self_cordon_state(
            {"reason": drift_reason, "first_seen_at": now, "escalated_at": None}
        )
        return

    first_seen_at = state.get("first_seen_at")
    if not isinstance(first_seen_at, (int, float)):
        # Corrupt/hand-edited marker — restart the clock rather than guess.
        _write_self_cordon_state(
            {"reason": drift_reason, "first_seen_at": now, "escalated_at": None}
        )
        return

    age_seconds = max(0.0, now - first_seen_at)
    if age_seconds < SELF_CORDON_ESCALATE_AFTER_SECONDS:
        return
    if state.get("escalated_at") is not None:
        # Fire once per persisted occurrence — mirrors `coord notifier`'s own
        # "once per subject per condition, for ever" rule (docs/NOTIFIER.md)
        # so a stuck self-cordon does not re-page every tick for the rest of
        # its lifetime.
        return

    pushed = _push_self_cordon_escalation(
        drift_reason, age_seconds=age_seconds, config_path=config_path
    )
    if not pushed:
        # Push attempted and failed (transient ntfy outage, a raised
        # exception) — leave the marker as-is (`escalated_at` still `None`)
        # so the very next tick retries rather than forfeiting this
        # occurrence for the lifetime of the persisted reason (#2572 review).
        return
    state = dict(state)
    state["escalated_at"] = now
    _write_self_cordon_state(state)


def _fetch_board_payload() -> dict:
    """The raw ``/board`` payload (+ standalone top-ups), untyped.

    Split out of :func:`_fetch_board_view` (#2870) so a caller that needs the
    RAW ``assignments``/``issues`` rows — not `BoardView`'s already-reduced
    per-issue facts — has one place to get them. :func:`coord.
    release_propagate.assess_quiescence` is exactly such a caller (#2870's
    between-legs settle-window re-check for the roll-pending fire condition,
    see :func:`_roll_pending_may_fire`): it reads raw assignment rows
    (``machine_name``/``dispatched_at``/``finished_at``) `BoardView` never
    carries at all.

    Raises whatever the fetch raised — same fail-closed contract as before
    this split.
    """
    from coord.board_service import resolve as resolve_board_service  # noqa: PLC0415
    from coord.drive_state import BoardFetcher  # noqa: PLC0415

    payload = BoardFetcher().fetch()
    if not isinstance(payload, dict):
        raise ValueError(f"board payload is not an object: {type(payload).__name__}")
    # Standalone shape top-up, gated per-key rather than on "issues" alone
    # (#2040: BoardFetcher's own standalone path now supplies "issues" —
    # see coord.drive_state.BoardFetcher._fetch_local — so a single combined
    # gate on that key would silently stop topping up "merge_queue" too,
    # regressing #1891's merge_ci_pending signal on the daemon host). Each
    # top-up is independently gated on board_service being unset so a thin
    # client never reads its own local DB — both keys are always present in
    # the daemon's HTTP projection, even when empty.
    if resolve_board_service() is None:
        top_up: dict = {}
        if "issues" not in payload:
            top_up["issues"] = _local_issue_rows()
        if "merge_queue" not in payload:
            # #1891: same top-up, one table over — see _local_merge_queue_rows.
            top_up["merge_queue"] = _local_merge_queue_rows()
        if top_up:
            payload = {**payload, **top_up}
    return payload


def _fetch_board_view() -> BoardView:
    """Board + live drive sessions, typed.

    Raises whatever the fetch raised — the caller turns that into a fail-closed
    abort.  ``list_drive_sessions()`` is deliberately NOT allowed to fail the
    tick: it returns ``[]`` when tmux is unavailable, and the board's
    ``active_work`` signal still holds the capacity line in that case.
    """
    from coord.drive import list_drive_sessions  # noqa: PLC0415

    payload = _fetch_board_payload()
    return build_board_view(payload, list_drive_sessions())


def _fetch_exit_reasons(
    entries: list,
) -> tuple[dict[str, str], dict[str, bool], dict[str, bool]]:
    """The drive's own ``drive_exited`` summary — and whether it was
    PERMANENT (a pre-dispatch refusal, or a dead end) — for every ``running``
    entry THIS launch, keyed by entry key (#1845/#1844/#2019).

    ``coord.drive.Driver.run`` already writes the true reason a run stopped —
    a deliberate refusal narrated in full, not just an exit code — to the
    audit trail before it returns. Nothing downstream used to read it, so
    `_reconcile_running`'s "no session, no active work, nothing landed" death
    classifier (which also matches a clean, deliberate exit) always
    overwrote it with a synthesised "drive session died" reason. This is the
    one DB read the shell does to close that gap; `plan_tick`/
    `_reconcile_running` stay pure and just consume the result as data (like
    *probes*).

    Returns ``(reasons, refused, dead_end)``. *reasons* is the summary text,
    same as before #1844. *refused* is ``True`` for a key whose recorded
    ``details.exit_code`` equals ``coord.drive.EXIT_DISPATCH_REFUSED`` — a
    DETERMINISTIC pre-dispatch guard refusal (#1138's oracle-readiness gate,
    #1314's epic-target gate, or any other check `coord assign`/`coord
    approve-plan`/`coord fix` raises a plain ``ValueError`` for) rather than
    a transient crash. `_reconcile_running` uses this second mapping to skip
    straight to `blocked` without spending an attempt — see its ``refused``
    branch.

    *dead_end* (#2019) is the same signal for ``coord.drive.EXIT_DEAD_END``:
    the drive's dead-end predicate found the row terminal and unactionable
    and exited on the first poll rather than counting ``no state change``
    against an event that cannot happen. Same disposition as *refused*
    (`blocked`, no attempt spent), separate mapping so the blocked entry's
    ``last_reason`` names the real cause instead of claiming a pre-dispatch
    guard refused something.

    Scoped with ``since=entry.launched_at`` so a stale reason (or refusal)
    from a PRIOR attempt on the same (repo, issue) — the entry's key doesn't
    change across a retry — is never replayed as if it explained the run
    that just ended. An entry with no `launched_at` (a row from before this
    launch was stamped) is skipped; the caller's fallback wording covers it.

    Fail-soft per entry: an unreadable audit table degrades to "no reason
    known for this entry", never aborts the tick — same posture as
    :func:`_local_issue_rows`.
    """
    from coord.audit import query_audit_log  # noqa: PLC0415
    from coord.drive import EXIT_DEAD_END, EXIT_DISPATCH_REFUSED  # noqa: PLC0415

    reasons: dict[str, str] = {}
    refused: dict[str, bool] = {}
    dead_end: dict[str, bool] = {}
    for e in entries:
        if e.state != STATE_RUNNING or e.launched_at is None:
            continue
        try:
            result = query_audit_log(
                event_type="drive_exited",
                repo=e.repo,
                issue=e.issue,
                since=e.launched_at,
                limit=1,
            )
        except Exception:  # noqa: BLE001 — see the fail-soft note above
            continue
        rows = result.get("entries") or []
        if not rows:
            continue
        row = rows[0]
        summary = row.get("summary")
        if summary:
            reasons[e.key] = str(summary)
        details = row.get("details") or {}
        if details.get("exit_code") == EXIT_DISPATCH_REFUSED:
            refused[e.key] = True
        elif details.get("exit_code") == EXIT_DEAD_END:
            dead_end[e.key] = True
    return reasons, refused, dead_end


def _fetch_gate_a_pending(entries: list) -> dict[str, bool]:
    """``{entry_key: still_waiting_on_a_human}`` for every entry parked on a
    missing Gate-A sign-off (#2063).

    The ``(repo_name, milestone_number)`` pair comes out of the park
    reason's own marker (:func:`coord.gate_a.parse_park_marker`), so this is
    a pure board read — no ``gh`` call, and no work at all on a tick where
    nothing is parked that way.

    Fails **closed**: an entry whose verdict can't be read stays parked. The
    cost of being wrong in that direction is a parked row an operator can
    see and release; the cost of being wrong the other way is dispatching
    work against a contract nobody approved, which is the whole issue.
    """
    from coord.gate_a import approval_fingerprint, parse_park_marker  # noqa: PLC0415

    pending: dict[str, bool] = {}
    #: ``None`` marks "the board could not be read for this milestone".
    cache: dict[tuple[str, int], str | None] = {}
    for e in entries:
        if e.state != STATE_PARKED:
            continue
        parsed = parse_park_marker(getattr(e, "last_reason", "") or "")
        if parsed is None:
            continue
        repo_name, milestone_number, parked_fingerprint = parsed
        key = (repo_name, milestone_number)
        if key not in cache:
            try:
                from coord.state import get_gate_a_approval  # noqa: PLC0415

                raw = get_gate_a_approval(
                    repo_name=repo_name, milestone_number=milestone_number
                )
                cache[key] = approval_fingerprint(raw)
            except Exception:  # noqa: BLE001 — fail closed, stay parked
                cache[key] = None
        if cache[key] is None:
            pending[e.key] = True
            continue
        # Resume when the stored verdict has CHANGED since the park — not
        # merely when one exists. A `--changes` verdict refuses too, so
        # "exists" would resume, relaunch, refuse and re-park every tick
        # forever; "changed" bounds it at one relaunch per operator action.
        # The guard itself re-derives the real answer (including the
        # contract-SHA freshness check) on that relaunch, so this predicate
        # deliberately does not duplicate it.
        pending[e.key] = cache[key] == parked_fingerprint
    return pending


def _fetch_live_ci_gate(
    entries: list, config_path: Path | None
) -> tuple[dict[str, bool], dict[str, str]]:
    """``({entry_key: still_blocked}, {entry_key: reason})`` for every
    ``parked`` entry whose OWN park reason is CI-shaped
    (#1891/#1892/#2347) — a FRESH, single-entry re-derivation of the SAME
    gate ``coord merge --plan`` computes, taken live, right now, this tick
    (#2182).

    The second dict (#2347, extended by #2556) carries the reason text
    :func:`coord.merge_queue.entry_gate_status` returned alongside each
    still-blocked verdict, and is handed to :func:`coord.drive_queue.
    plan_tick` as its ``live_ci_gate_reason`` parameter, which now gives it
    a first, decisive job (#2556): when this text carries none of the
    self-refreshing prefixes (``is_ci_pending_reason``/``is_ci_infra_reason``/
    ``is_ci_flaky_reason``/``is_ci_unreadable_reason``), ``plan_tick``
    overrides the first dict's still-blocked verdict into a resume — a
    completed, *failing* CI run must never be indistinguishable from a slow
    one. When it DOES carry one of those prefixes, this reason is used the
    pre-#2556 way: only to rewrite a still-``parked`` entry's
    ``last_reason`` when the fresh reading is specifically "GitHub
    unreachable" (``is_ci_unreadable_reason``) and differs from what is
    currently stored. See ``plan_tick``'s own ``live_ci_gate_reason``
    docstring for the full account.

    THE GAP THIS CLOSES. On the daemon host — the only machine that ever
    runs this tick (``docs/AGENT_OPERATIONS.md``) — ``_fetch_board_view``
    reads the local DB directly and never populates ``merge_plan`` at all
    (see ``_local_merge_queue_rows``'s docstring): it is *computed*, not
    stored, and computing it needs a live ``ci_store``/``gh_ops`` this
    read-only board fetch deliberately does not build. So
    ``IssueFacts.merge_ci_pending_live`` is unconditionally ``False`` on
    every parked entry this tick ever sees, and the ONLY thing that used to
    release such a park was :data:`coord.drive_queue.PARK_STALE_SECONDS` —
    up to 45 minutes — even at the instant CI actually reports, because
    nothing on the read path ever re-checked. claude-coordinator#2159 is
    that gap caught live: at 03:25:46 UTC, ``coord merge --dry-run`` for the
    entry read READY (no gate objection) while ``coord drive-queue list``,
    reading the SAME board a moment later, still read ``parked`` — the two
    surfaces disagreeing about the exact same fact.

    THE FIX. Scoped to the bounded few entries actually sitting in
    ``parked`` on a CI reason right now (typically 0-1, never the whole
    merge queue — see :func:`coord.merge_queue.entry_gate_status`'s
    docstring for why this deliberately does NOT call the whole-queue
    :func:`coord.merge_queue.plan` instead), so paying for a live
    ``gh``-backed check per entry, every ~3-minute tick, is the same
    "small and predictable" cost an operator's own ``coord merge --plan``
    already pays on demand — not the unbounded per-tick GitHub polling
    #1344 removed from the read path.

    Fails OPEN per entry and CLOSED overall in the sense that matters: a key
    simply ABSENT from the returned dict (an unreadable config, a
    ``ci_store`` that failed to build, an entry ``coord.merge_queue.
    load_queue()`` no longer carries, or any other exception) leaves that
    entry to :func:`coord.drive_queue.plan_tick`'s pre-#2182 fallback — the
    #2158 :data:`~coord.drive_queue.PARK_STALE_SECONDS` ceiling — exactly as
    if this function had never run. A transient failure here degrades to
    the OLD (already-shipped, already-bounded) behaviour, never to a wedge
    and never to a wrongly-forced resume.

    Gated on ``resolve_board_service() is None`` (the daemon-host tick,
    same guard :func:`_fetch_board_view` uses for its own local-DB top-up):
    on a thin client the daemon's real ``GET /board`` already carries a live
    ``merge_plan`` section, so ``build_board_view`` already resolves
    ``merge_ci_pending_live=True`` there on its own and this function would
    have nothing to add — worse, ``coord.state.load_board()`` guards against
    being called from a thin client at all (#615).
    """
    from coord.merge_queue import (  # noqa: PLC0415
        is_ci_infra_reason,
        is_ci_pending_reason,
        is_ci_unreadable_reason,
    )

    targets = [
        e for e in entries
        if e.state == STATE_PARKED
        and (
            is_ci_pending_reason(getattr(e, "last_reason", "") or "")
            or is_ci_infra_reason(getattr(e, "last_reason", "") or "")
            or is_ci_unreadable_reason(getattr(e, "last_reason", "") or "")
        )
    ]
    if not targets:
        return {}, {}

    from coord.board_service import resolve as resolve_board_service  # noqa: PLC0415

    if resolve_board_service() is not None:
        return {}, {}

    try:
        from coord import github_ops as _gh_ops  # noqa: PLC0415
        from coord import merge_queue as _mq  # noqa: PLC0415
        from coord.ci_store import build_ci_store  # noqa: PLC0415
        from coord.commands._common import _load_config  # noqa: PLC0415
        from coord.state import load_board as _load_board  # noqa: PLC0415

        cfg = _load_config(config_path)
        board = _load_board()
        ci_store = build_ci_store(
            cfg.ci_store.type, host=cfg.ci_store.host, token_env=cfg.ci_store.token_env
        )
        queue_by_key = {
            entry_key(q.repo_name, q.issue_number): q for q in _mq.load_queue()
        }
    except Exception:  # noqa: BLE001 — see the fail-soft note above
        return {}, {}

    overrides: dict[str, bool] = {}
    reasons: dict[str, str] = {}
    for e in targets:
        q = queue_by_key.get(e.key)
        if q is None or not q.pr_number:
            continue
        try:
            status, reason = _mq.entry_gate_status(q, board, cfg, ci_store, _gh_ops)
        except Exception:  # noqa: BLE001 — leave this one entry to the ceiling
            continue
        overrides[e.key] = status != _mq.PLAN_READY
        if reason:
            reasons[e.key] = reason
    return overrides, reasons


def _index_merge_queue_by_key(rows: list, board: Any) -> dict[str, Any]:
    """``{entry_key: row}`` for every *rows* (``coord.merge_queue.QueuedMerge``),
    plus a SECOND key for each row's #1553 *effective* issue when it differs
    from the row's own ``issue_number`` (#3012).

    ``QueuedMerge.issue_number`` is always the assignment's BOOKED-TO issue —
    ``merge_queue.enqueue``/``refresh_entry_assignment`` set it straight from
    ``assignment.issue_number`` — which for an oracle-loop acceptance slice is
    the milestone's tracking/epic issue, never the slice's own issue. A
    drive-queue entry for the slice is keyed on the SLICE issue (`entry_key`
    over the queue row's own `.issue`, `coord/drive_queue.py`), so looking the
    merge row up by that key alone always misses it — reported as "no
    merge-queue row for this entry" (unreadable, "retry might help") even
    though the row exists, is perfectly readable, and may sit in a terminal
    state like `human_required` that no retry will ever change
    (coord-portal#164, 2026-08-31).

    Mirrors ``coord.gates.build_gate_report``'s own resolution (``a.issue_
    number == issue_number or effective_issue_number(a) == issue_number``) by
    cross-referencing each row's ``assignment_id`` against the board to find
    its ``for_issue_number``. Best-effort: a board that can't be walked (a
    bare stub in a unit test, or an ``assignment_id`` with no matching board
    row) just skips the second key — the row stays reachable by its own
    ``issue_number``, exactly as before this fix.
    """
    from coord.models import effective_issue_number  # noqa: PLC0415

    assignments_by_id: dict[str, Any] = {}
    try:
        assignments_by_id = {
            a.assignment_id: a
            for a in (list(board.active) + list(board.completed))
            if getattr(a, "assignment_id", None)
        }
    except Exception:  # noqa: BLE001 — best-effort enrichment only, see docstring
        assignments_by_id = {}

    indexed: dict[str, Any] = {}
    for q in rows:
        indexed.setdefault(entry_key(q.repo_name, q.issue_number), q)
        a = assignments_by_id.get(getattr(q, "assignment_id", None))
        if a is not None:
            effective = effective_issue_number(a)
            if effective and effective != q.issue_number:
                indexed.setdefault(entry_key(q.repo_name, effective), q)
    return indexed


def _fetch_live_blocked_gate(
    entries: list, config_path: Path | None
) -> tuple[dict[str, bool], dict[str, str]]:
    """``({entry_key: still_blocked}, {entry_key: unreadable_reason})`` for
    every RE-EVALUABLE ``blocked`` entry (#2230) — the counterpart of
    :func:`_fetch_live_ci_gate` above: same mechanism, same bound, a
    different queue state.

    THE GAP THIS CLOSES. `blocked` used to be terminal: once
    `_reconcile_running` exhausted an entry's attempts, nothing ever asked
    again whether the gate it kept dying against had since cleared — even
    minutes later, even when `coord merge --only` would land it on the first
    try with no objection. quadraui#309 sat `blocked attempts=2` for ~11h on
    exactly that: `coord gates quadraui 309` read `merge: READY` for most of
    the window, and nothing in the queue ever looked again.

    NOT EVERY BLOCKED ENTRY QUALIFIES. `coord.drive_queue.
    is_permanent_block_reason` excludes #1844 (a pre-dispatch guard's
    refusal — deterministic, cannot change on retry) and #2019 (a dead-end
    row — also cannot change): paying for a live `gh`-backed call on either
    would be spending real cost to re-confirm an answer this function
    already knows without asking. Everything else that reached `blocked` —
    chiefly `exhausted`, a drive that died `max_attempts` times for whatever
    reason — is a candidate.

    THE COST. One `coord.merge_queue.entry_gate_status` call — the same live
    backend `coord merge --plan`/`--only` build — per QUALIFYING blocked
    entry, mirroring `_fetch_live_ci_gate`'s exact justification: bounded to
    the (typically 0-2) entries actually sitting in `blocked` right now, not
    the whole queue and not forever — an entry that resumes leaves `blocked`
    and stops costing anything; one confirmed still-shut is untouched and
    re-pays this same bounded cost next tick, identical to how a
    still-CI-pending `parked` entry already does. This no longer holds
    exactly once the #2806 self-heal below fires: `enqueue_approved_work`
    scans the WHOLE `board.completed` list, not just this tick's qualifying
    blocked entries, so an entry that will never earn a queue row (its Work
    never satisfies the review/smoke gates) repeats that full-board scan
    every tick indefinitely rather than paying a bounded per-entry cost —
    accepted for now since `coord serve`'s passive tick already runs the
    same call roughly every `COORD_RECONCILE_INTERVAL` (~30s), far more
    often than this tick's own cadence, so this self-heal is a rare-race
    backstop rather than the common path; revisit with a cooldown/backoff
    per entry if the full-board scan cost ever shows up in practice.

    #2806 SELF-HEAL. A qualifying entry with a real branch/PR behind it can
    still have no ``coord.merge_queue`` row yet — the merge queue is
    populated by ``enqueue_approved_work``, run independently (the board
    daemon's passive tick, or an operator's own ``coord merge``), not by
    this tick — so a board row that turned gate-clear only moments ago can
    race this exact read. `coord merge --only`'s #1845 fix hit the identical
    race and answered it by running `enqueue_approved_work` once, inline,
    before giving up; this mirrors that precedent: when at least one target
    has no queue row, one bounded `enqueue_approved_work(cfg, board)` call
    runs (never more than once per tick, whatever the target count), then
    every such target is looked up again against the refreshed queue before
    this function concedes it has nothing.

    #2806 VISIBILITY. `_fetch_live_ci_gate`'s sibling docstring, and this
    one before #2806, both describe "a key ABSENT" as if it were one thing —
    it is actually at least four (no queue row even after the self-heal
    above, no PR number yet, `entry_gate_status` raising, or the whole
    fetch failing closed before any entry is even tried) and NONE of them
    used to be logged anywhere, so a run of them looked, from the tick's own
    output, identical to the gate genuinely still being shut. Every one is
    now (a) logged here, so `journalctl` names the actual cause instead of
    silence, and (b) surfaced in the second returned dict, keyed by entry,
    with a short human-readable reason — :func:`coord.drive_queue.
    _reconcile_blocked_unreadable` turns a present key there into a
    distinct, operator-visible "could not read this gate" outcome instead
    of folding it into "still shut" the way an absent key from the FIRST
    dict alone always has and still does (see that function's docstring for
    why the two must never render identically).

    Same fail-open-per-entry, fail-closed-overall contract as
    `_fetch_live_ci_gate` otherwise: a key ABSENT from the FIRST dict (no
    queue row, no PR yet, a `ci_store` that failed to build, any exception)
    leaves that entry to `plan_tick`'s cheap board-only fallback
    (`IssueFacts.merge_gate_status`, populated for free on a thin client's
    live `/board`) — which on the daemon-host tick (the only host this
    actually runs on, `docs/DRIVE_QUEUE.md` §2) has no `merge_plan` section
    to read at all, so an absent key there means "no evidence, stays
    blocked, but distinctly reported as unreadable when the second dict
    says why" (`_reconcile_blocked` never guesses at a gate reading).
    """
    targets = [
        e for e in entries
        if e.state == STATE_BLOCKED
        and not is_permanent_block_reason(getattr(e, "last_reason", "") or "")
    ]
    if not targets:
        return {}, {}

    from coord.board_service import resolve as resolve_board_service  # noqa: PLC0415

    if resolve_board_service() is not None:
        return {}, {}

    try:
        from coord import github_ops as _gh_ops  # noqa: PLC0415
        from coord import merge_queue as _mq  # noqa: PLC0415
        from coord.ci_store import build_ci_store  # noqa: PLC0415
        from coord.commands._common import _load_config  # noqa: PLC0415
        from coord.state import load_board as _load_board  # noqa: PLC0415

        cfg = _load_config(config_path)
        board = _load_board()
        ci_store = build_ci_store(
            cfg.ci_store.type, host=cfg.ci_store.host, token_env=cfg.ci_store.token_env
        )
        queue_by_key = _index_merge_queue_by_key(_mq.load_queue(), board)
    except Exception as exc:  # noqa: BLE001 — see the fail-soft note above
        log.warning(
            "blocked-gate sweep (#2806): could not build board/config/queue "
            "for this tick's %d qualifying blocked entr%s — every one falls "
            "back to the board-only reading, unreadable: %r",
            len(targets), "y" if len(targets) == 1 else "ies", exc,
        )
        return {}, {}

    # #2806 self-heal: mirror `coord merge --only`'s #1845 fix — a target
    # with no queue row yet may simply not have been enqueued by
    # `enqueue_approved_work` (the board daemon's passive tick, or an
    # operator's own `coord merge`) since its board row turned gate-clear.
    # One bounded call, never more than once per tick.
    if any(queue_by_key.get(e.key) is None for e in targets):
        try:
            _mq.enqueue_approved_work(cfg, board)
            queue_by_key = _index_merge_queue_by_key(_mq.load_queue(), board)
        except Exception as exc:  # noqa: BLE001 — self-heal is best-effort
            log.warning(
                "blocked-gate sweep (#2806): enqueue_approved_work self-heal "
                "failed, continuing with the queue as already loaded: %r", exc,
            )

    overrides: dict[str, bool] = {}
    unreadable: dict[str, str] = {}
    for e in targets:
        q = queue_by_key.get(e.key)
        if q is None:
            reason = "no merge-queue row for this entry, even after the self-heal enqueue attempt"
            # #2589/#2635: a genuinely pre-dispatch cause (no branch/PR was
            # EVER created) will never have a merge-queue row, forever — that
            # is already reported distinctly by `coord drive-queue list`'s
            # own #2589 terminal note, and reporting "unreadable" (which
            # reads as "retry might help") on top of it would contradict
            # that note. But #2635 also showed this TEXT classification can
            # be wrong per-run (a retry's own launch dispatched nothing only
            # because an earlier attempt's work was still in flight) — so it
            # only suppresses here, where there is in fact no queue row to
            # contradict it. The instant a queue row (with or without a PR
            # yet) or a probe result exists below, that positive evidence
            # wins outright and this text is never consulted again.
            if not is_pre_dispatch_block_reason(getattr(e, "last_reason", "") or ""):
                unreadable[e.key] = reason
                log.warning("blocked-gate sweep (#2806): %s — %s", e.key, reason)
            continue
        if not q.pr_number:
            reason = "merge-queue row has no PR number yet"
            unreadable[e.key] = reason
            log.warning("blocked-gate sweep (#2806): %s — %s", e.key, reason)
            continue
        try:
            status, _reason = _mq.entry_gate_status(q, board, cfg, ci_store, _gh_ops)
        except Exception as exc:  # noqa: BLE001 — leave this one entry to the fallback
            reason = f"entry_gate_status raised: {exc!r}"
            unreadable[e.key] = reason
            log.warning("blocked-gate sweep (#2806): %s — %s", e.key, reason)
            continue
        overrides[e.key] = status != _mq.PLAN_READY
    return overrides, unreadable


def _fetch_live_prereq_terminal(
    entries: list, board: Any, config_path: Path | None
) -> dict[str, bool]:
    """``{dep_key: True}`` for every ``after=`` pre-req the cached board
    cannot yet confirm landed, re-checked LIVE against GitHub (#2602) — the
    recovery-half counterpart of :func:`_fetch_live_ci_gate`/
    :func:`_fetch_live_blocked_gate`: same bounded per-tick mechanism, a
    different question.

    THE GAP THIS CLOSES. ``coord.drive_queue._resolve_prereqs`` trusts
    ``board.facts(dep).landed`` — a periodic ``/board`` build — before
    anything else. When a pre-req has JUST left the queue (its PR merged, its
    issue closed) and that cache hasn't caught up, ``_resolve_prereqs`` falls
    through to its last branch and marks every dependent entry chained
    ``--after`` it PERMANENTLY blocked — recoverable, before this, only by an
    operator ``remove`` + ``add`` (claude-coordinator#2602,
    coord-portal#145/#149/#150, 2026-08-22). This gives that branch one more
    chance: a live, per-dep ``github_ops.work_is_terminal`` read (issue
    closed OR PR merged) — the SAME positive-liveness test
    ``coord.overlap_predict``'s ``terminal_checker`` already uses for the
    sibling ``[branch]`` half of #2602 — taken THIS tick, before
    ``_resolve_prereqs`` ever reaches that branch, and again later in the
    same tick if the entry is instead already sitting ``blocked`` on that
    exact verdict (``coord.drive_queue._reconcile_blocked_after``'s #2602
    widening).

    BOUNDED THE SAME WAY ITS SIBLINGS ARE. Only deps named by an entry
    currently ``waiting`` or ``blocked`` with a non-empty ``after=``, and
    only those the board does NOT already resolve on its own
    (``facts.landed``, ``facts.open``, ``facts.active_work``, or a
    ``STATE_DONE`` hit in this tick's own pre-reconcile ``states`` — i.e.
    already known landed) make it into a live call. A queue with no
    unresolved ``after=`` edge costs nothing here, same as
    ``_fetch_live_blocked_gate`` costs nothing with no ``blocked`` rows.
    ``states`` is approximated from *entries*' pre-tick ``state`` field
    (cheap, no I/O) rather than threaded from ``plan_tick``'s own
    in-progress reconcile — a false negative here (a dep this approximation
    still calls a live-check target, when the tick's own authoritative
    ``states`` would already have resolved it another way) costs one
    harmless extra ``gh`` read, never a wrong verdict: ``_resolve_prereqs``
    only ever consults this mapping in its OWN final branch, after its own
    ``states``/``facts`` checks have already returned. A false positive (a
    dep this skips that the tick's real walk would have reached the final
    branch for) simply leaves that one dep to the pre-#2602 fallback for
    this tick — caught on the next one.

    #2850 WIDENS THE BOUND, in two ways, both closing the same class of gap
    the incident exposed (a pre-req stuck under a stale/bogus queue row, not
    merely absent from the queue):

    * a dep whose queue row IS present now still gets a live call as long as
      that row isn't already ``STATE_DONE`` — before #2850 any dep present
      in ``states`` at all (``running``, ``waiting``, ``blocked``, ...) was
      excluded outright, on the (pre-#2850) assumption that "still queued"
      meant the local row was trustworthy. A `running` row that outlived the
      drive it named (the #2850 incident: a drive that exited 0 having
      merged, requeued into a `running` row nothing else re-checks) broke
      that assumption; ``coord.drive_queue._resolve_prereqs``'s matching
      #2850 change is what actually consults this for those deps.
    * every ``running`` entry's OWN key is also added as a target when it
      has no live tmux session (``board.live_sessions``) and the board does
      not yet show it landed — i.e. exactly the entries
      ``coord.drive_queue._reconcile_running`` is about to run its
      death/retry diagnosis on THIS tick. That function consults this same
      mapping, keyed by its OWN ``entry.key``, right before it would
      otherwise spend a requeue attempt — the "re-check before requeuing
      anything" half of #2850, independent of whatever (if anything) the
      drive's own exit reason said.
    """
    states = {e.key: e.state for e in entries}
    targets: set[str] = set()
    for e in entries:
        if e.state not in (STATE_WAITING, STATE_BLOCKED) or not e.after:
            continue
        for dep in e.after:
            if dep in targets or states.get(dep) == STATE_DONE:
                continue
            facts = board.facts(dep)
            if facts.landed or facts.open or facts.active_work:
                continue
            targets.add(dep)
    # #2850: a RUNNING entry about to be reconciled by `_reconcile_running`'s
    # death/retry logic THIS tick (no live session, and the board's own
    # cached facts don't show it landed) gets the identical live re-check —
    # bounded to exactly the entries that would otherwise risk a requeue.
    for e in entries:
        if e.state != STATE_RUNNING or e.key in targets:
            continue
        if e.key in board.live_sessions:
            continue
        if board.facts(e.key).landed:
            continue
        targets.add(e.key)
    if not targets:
        return {}

    try:
        from coord import github_ops as _gh_ops  # noqa: PLC0415
        from coord.commands._common import _load_config  # noqa: PLC0415

        cfg = _load_config(config_path)
        github_by_repo = {r.name: r.github for r in cfg.repos}
    except Exception:  # noqa: BLE001 — see the fail-soft note above
        return {}

    out: dict[str, bool] = {}
    for dep in targets:
        parsed = parse_key(dep)
        if parsed is None:
            continue
        repo_name, issue_number = parsed
        repo_github = github_by_repo.get(repo_name)
        if not repo_github:
            continue
        try:
            # #2639 audit: intentionally left at the default
            # trust_issue_closed=True. `dep` is a drive-queue `repo#issue`
            # key, not a board assignment row, and the branch argument here
            # is always "" (no branch to check against a `--after` pre-req
            # key) — `work_is_terminal`'s `elif branch and pr_is_merged(...)`
            # never fires, so issue-closed IS the only signal this call can
            # ever use. Setting trust_issue_closed=False would make this
            # function permanently return nothing rather than fix a real
            # test-author aliasing bug: a `--after` pre-req is queued by its
            # own issue number (`coord drive-queue add REPO ISSUE`), not a
            # milestone tracking issue, so `issue_number` here is already
            # the dependency's own deliverable.
            if _gh_ops.work_is_terminal(repo_github, issue_number, ""):
                out[dep] = True
        except Exception:  # noqa: BLE001 — leave this one dep to the fallback
            continue
    return out


def _fetch_live_dispatch_evidence(
    entries: list, board: BoardView | None, config_path: Path | None
) -> dict[str, bool]:
    """``{entry_key: True}`` for every `blocked` entry whose `last_reason`
    carries #2273's "no assignment was ever created for this run" marker
    but which actually has positive evidence to the contrary (#2635) — the
    per-run/per-entry confusion :func:`coord.drive_queue.is_dispatch_failure_reason`
    cannot tell apart on its own: a retry's `last_reason` only ever
    describes what THAT launch dispatched, never what an EARLIER attempt on
    the SAME entry already left behind (a board assignment, a pushed
    branch). The claude-coordinator#2569 incident this closes: attempt 2
    dispatched nothing new only because attempt 1's work/test/smoke legs
    were still in flight — claim detection doing its job, not an
    infrastructure failure — yet the entry rendered NEEDS OPERATOR forever.

    Two sources, cheapest first, the same bounded/fail-soft posture as
    :func:`_fetch_live_prereq_terminal` just above:

    * the board itself (`board.facts(key)`, already fetched this call for
      `list`'s `after=` diagnosis, or fetched here on purpose when only
      this check needs it) — `active_work` (a live work-like row right
      now), `merged`, or `last_dispatched_at is not None` (SOME work-like
      assignment was ever dispatched for this issue, whatever launch
      created it — see `IssueFacts.last_dispatched_at`'s own docstring for
      why it is a high-water mark rather than scoped to the current run).
      Free: no extra I/O beyond a board this command may already hold.
    * a live remote-branch lookup (`coord.github_ops.list_remote_branch_names`,
      filtered to the `issue-{N}-*` prefix — the same positive-liveness
      shape `coord.claim`'s claim detection and `coord.issue_store`'s
      branch-fallback already trust), consulted only when the board itself
      shows nothing — so a `/board` read that has not yet caught up with a
      `coord assign` that just ran does not still read as "nothing was
      ever dispatched".

    Deliberately NOT computed for :func:`coord.drive_queue.is_empty_branch_death_reason`
    rows. Unlike the dispatch-failure marker, that one is already anchored
    to a LIVE check of the actual branch (`Driver.branch_has_commits`, a
    fresh `git fetch` + `rev-list` against the default branch at the moment
    the drive exited) rather than a per-run timestamp comparison, so it
    cannot go stale the same way: a retry can only ever produce that reason
    when the branch it just checked out genuinely carried zero commits,
    real prior-attempt commits and all (retries reuse the same deterministic
    branch name and check it out at the remote tip — see `agent.py`'s
    `setup_interactive_worktree`). Treating `last_dispatched_at` (a mere "an
    assignment existed at some point") as a rebuttal for THAT reason would
    be wrong in the other direction — a stale board row for a now-superseded
    attempt is not evidence of anything left for #2230's sweep to re-check.

    Fail-soft, same direction as #2602: any setup failure (no config, no
    ``gh``, an API error) returns whatever was already resolved from board
    facts alone — never invents evidence, never trusts a lookup it could
    not complete. Bounded the same way too: only entries actually reaching
    #2635's rendering with nothing already found do a live lookup at all.
    """
    evidence: dict[str, bool] = {}
    if board is None:
        return evidence

    targets: list = []
    for e in entries:
        if e.state != STATE_BLOCKED or is_permanent_block_reason(e.last_reason):
            continue
        if not is_dispatch_failure_reason(e.last_reason):
            continue
        facts = board.facts(e.key)
        if facts.known and (
            facts.active_work or facts.merged or facts.last_dispatched_at is not None
        ):
            evidence[e.key] = True
            continue
        targets.append(e)

    if not targets:
        return evidence

    try:
        from coord import github_ops as _gh_ops  # noqa: PLC0415
        from coord.commands._common import _load_config  # noqa: PLC0415

        cfg = _load_config(config_path)
        github_by_repo = {r.name: r.github for r in cfg.repos}
    except Exception:  # noqa: BLE001 — see docstring: fail soft to board-only evidence
        return evidence

    for e in targets:
        parsed = parse_key(e.key)
        if parsed is None:
            continue
        repo_name, issue_number = parsed
        repo_github = github_by_repo.get(repo_name)
        if not repo_github:
            continue
        try:
            names = _gh_ops.list_remote_branch_names(repo_github)
        except Exception:  # noqa: BLE001 — leave this one entry to the fallback
            continue
        prefix = f"issue-{issue_number}-"
        if any(name.startswith(prefix) for name in names):
            evidence[e.key] = True
    return evidence


def _fetch_merge_only_ready(
    entries: list,
    live_ci_gate: Mapping[str, bool],
    live_blocked_gate: Mapping[str, bool],
) -> dict[str, bool]:
    """``{entry_key: True}`` for #2350's Merge-only fast path: every entry
    THIS tick's live gate re-check just found clear — a `parked` entry with
    ``live_ci_gate[key] is False``, a `blocked` one with
    ``live_blocked_gate[key] is False`` — whose board-recorded pipeline
    state ALSO shows Test already `passed` and Review already `approve`,
    meaning Merge was the only gate that was ever still shut.

    Scoped to exactly the entries about to reconcile `resumed` this tick —
    typically 0-2, the same bound :func:`_fetch_live_ci_gate`/
    :func:`_fetch_live_blocked_gate` already keep — so this never re-derives
    a gate reading of its own (that authority stays with *live_ci_gate*/
    *live_blocked_gate*, computed moments earlier in the same tick): it only
    adds two board-only reads (:func:`coord.merge_queue.has_approved_review`,
    :func:`coord.merge_queue.has_passed_test`) against the SAME local board
    those callers already loaded. Almost always cheap and I/O-free — but not
    unconditionally: ``has_approved_review`` can fall through to
    ``scan_approved_reviews``'s ``_backfill_branch_patch_id``, which does a
    live ``gh api compare`` round trip via *_gh_ops* when a stale-SHA review
    needs its patch-id computed on demand. That path is exception-guarded
    below and fails closed to the pre-#2350 `resumed` path like everything
    else here, so it is not unsafe — just not the zero-I/O read the rest of
    this docstring describes.

    Takes no *config_path*, unlike both siblings: neither board-only read
    needs a :class:`~coord.config.Config` (no ``ci_store``, no live gate
    re-derivation), so there is nothing to load here.

    Same fail-open-per-entry, fail-closed-overall contract as its siblings:
    a key ABSENT from the result (no queue row, an unreadable board or
    merge queue, any exception) simply takes the pre-#2350 `resumed` path —
    never a wrongly skipped relaunch.
    """
    targets = [
        e for e in entries
        if (e.state == STATE_PARKED and live_ci_gate.get(e.key) is False)
        or (e.state == STATE_BLOCKED and live_blocked_gate.get(e.key) is False)
    ]
    if not targets:
        return {}

    from coord.board_service import resolve as resolve_board_service  # noqa: PLC0415

    if resolve_board_service() is not None:
        return {}

    try:
        from coord import github_ops as _gh_ops  # noqa: PLC0415
        from coord import merge_queue as _mq  # noqa: PLC0415
        from coord.state import load_board as _load_board  # noqa: PLC0415

        board = _load_board()
        queue_by_key = {
            entry_key(q.repo_name, q.issue_number): q for q in _mq.load_queue()
        }
    except Exception:  # noqa: BLE001 — see the fail-soft note above
        return {}

    ready: dict[str, bool] = {}
    for e in targets:
        q = queue_by_key.get(e.key)
        if q is None:
            continue
        try:
            ready[e.key] = _mq.has_approved_review(
                q, board, _gh_ops
            ) and _mq.has_passed_test(q, board)
        except Exception:  # noqa: BLE001 — leave this one entry to the ordinary path
            continue
    return ready


def _launch_argv(entry: QueueEntry, config_path: Path | None) -> list[str]:
    """The ``coord drive --tmux`` argv for *entry*.

    #1809: this is the argv the tick actually spawns as a subprocess (below,
    in the caller). When ``coord_argv()``'s PATH-less fallback was silently
    broken (no ``__main__`` guard on ``coord/cli.py``), that subprocess
    exited 0 having imported the module and run nothing — BEFORE ever
    reaching ``launch_drive_in_tmux``'s #1606 alive/log-growth verification.
    From the tick's side that is indistinguishable from a real launch that
    passed verification: both are "subprocess exited 0". That fully explains
    a launch reported as a success banner while its tmux session had already
    died — no separate bug in this module's returncode handling or in
    ``launch_drive_in_tmux``'s growth check (both were re-verified against
    the #1809 investigation and are correct: a non-zero exit is never
    counted as running — see ``test_a_failed_launch_is_a_consumed_attempt_
    not_a_running_entry`` — and the growth check does register an
    absent-before-launch log file that then gets written to, per
    ``test_session_dies_immediately_raises_instead_of_reporting_success``).
    Fixing the ``__main__`` guard closes this path too, since it is the same
    fallback the driver's own ``coord assign`` calls go through.

    #2604: always emits ``--max-fix-rounds`` (never left to `coord drive`'s
    own interactive default of 3) — see
    ``coord.drive_queue.effective_max_fix_rounds`` for the resolution order
    (entry override → ``pipeline.max_fix_rounds`` → the tick's own lower
    default). Reads the config here rather than threading it through the
    caller because every OTHER caller of this function already only has
    ``config_path``, the same shape ``_merge_only_argv`` takes — a config
    LOAD failure (unreadable file, bad YAML) falls back to the tick's default
    rather than aborting the launch, the same fail-soft posture the rest of
    this module takes for advisory reads.

    #2589: also emits ``--no-acceptance`` when ``entry.no_acceptance`` is
    set — the per-entry passthrough `coord drive-queue add --no-acceptance`
    stores. Unlike ``--max-fix-rounds`` this has no fleet-config fallback:
    it is opt-in-only, so an entry that never set it launches exactly as
    before this column existed.
    """
    from coord.drive import coord_argv  # noqa: PLC0415

    config_default: int | None = None
    try:
        from coord.commands._common import _load_config  # noqa: PLC0415

        config_default = _load_config(config_path).pipeline.max_fix_rounds
    except Exception:  # noqa: BLE001 — advisory read, see docstring
        config_default = None

    argv = coord_argv() + ["drive", entry.repo, str(entry.issue), "--tmux"]
    if entry.machine:
        argv += ["--machine", entry.machine]
    argv += [
        "--max-fix-rounds",
        str(effective_max_fix_rounds(entry, config_default)),
    ]
    if entry.no_acceptance:
        argv += ["--no-acceptance"]
    if config_path:
        argv += ["--config", str(config_path)]
    return argv


def _merge_only_argv(entry: QueueEntry, config_path: Path | None) -> list[str]:
    """The ``coord merge --only <repo>#<issue>`` argv for *entry* (#2350).

    Mirrors :func:`_launch_argv`'s exact shape — same ``coord_argv()`` base,
    same ``--config`` passthrough — for the direct-merge fast path instead
    of a fresh drive session. ``--only`` accepts the durable ``repo#issue``
    form (#1477), which is exactly ``entry.key``, so no assignment_id lookup
    is needed here.
    """
    from coord.drive import coord_argv  # noqa: PLC0415

    argv = coord_argv() + ["merge", "--only", entry.key]
    if config_path:
        argv += ["--config", str(config_path)]
    return argv


def _merge_only_landed(entry: QueueEntry) -> bool:
    """Did *entry* actually MERGE, per the merge-queue's own row — the one
    ground truth ``coord merge --only``'s exit code alone cannot supply.

    #2350: a ``--only`` run against an already-PENDING entry exits 0
    whether the attempt landed it, left it blocked on a gate, or routed it
    into a conflict-fix — see ``coord.commands.merge``'s ``--only`` handler,
    which prints a summary and returns unconditionally once ``process()``
    has run. Exit code alone is therefore not evidence of a landed merge;
    the queue row's own resulting state is.
    """
    from coord import merge_queue as _mq  # noqa: PLC0415

    try:
        rows = _mq.load_queue()
    except Exception:  # noqa: BLE001 — unreadable queue is not proof of a landed merge
        return False
    for q in rows:
        if entry_key(q.repo_name, q.issue_number) == entry.key:
            return q.state == _mq.MERGED
    return False


def _run_merge_only_candidates(plan: TickPlan, config_path: Path | None) -> None:
    """#2350: attempt ``coord merge --only`` directly, THIS tick, for every
    entry :func:`coord.drive_queue.plan_tick` marked ``merge_only`` — no
    relaunch, no capacity slot spent. Called AFTER ``_apply_writes(plan)``,
    same posture as the launch subprocess below it: the entry's real next
    state is decided from the live outcome of THIS attempt, not from
    anything ``plan_tick`` could have known when it returned — see
    :class:`~coord.drive_queue.Reconcile`'s ``merge_only`` outcome for why
    that Reconcile itself writes nothing.

    On success (the queue's own row now reads ``MERGED`` — see
    :func:`_merge_only_landed`): writes ``STATE_DONE`` directly, skipping
    the ``waiting``/relaunch cycle a NEXT tick's ordinary ``landed``
    re-check would otherwise need, and records a distinct #2350 block-log
    ``resolve`` (:func:`coord.block_log.merge_only_event`) so #2235's corpus
    can tell "the queue itself finished this" apart from "the state flipped
    and something else finished it" (``already-landed``/``auto-released``).

    On failure (the gate flipped shut again between the read and the
    attempt — a genuine race, not the common case; or the attempt itself hit
    something a single bounded try cannot resolve): falls back to EXACTLY
    the pre-#2350 ``resumed`` shape — ``STATE_WAITING``, ``attempts`` reset
    to 0, ``resumes`` bumped for a ``blocked``-origin entry (mirroring
    ``_reconcile_blocked``'s own formula, so the #2230 oscillation ceiling
    still sees every chance this entry got, fast-path or not) — never worse
    than the status quo, and never a `coord drive --tmux` launch attempt
    spent retrying a race this bounded attempt already ruled out.
    """
    if not plan.merge_only:
        return

    from coord.block_log import merge_only_event, merge_only_fallback_event  # noqa: PLC0415
    from coord.state import update_drive_queue_entry  # noqa: PLC0415

    for target in plan.merge_only:
        argv = _merge_only_argv(target, config_path)
        try:
            result = subprocess.run(  # noqa: S603 — argv built from coord_argv + typed row
                argv,
                capture_output=True,
                text=True,
                timeout=_MERGE_ONLY_TIMEOUT_SECONDS,
            )
            returncode = result.returncode
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            message = detail[-1] if detail else ""
        except (subprocess.SubprocessError, OSError) as exc:
            returncode, message = 1, str(exc)

        if returncode == 0 and _merge_only_landed(target):
            update_drive_queue_entry(
                target.repo,
                target.issue,
                state=STATE_DONE,
                last_reason=(
                    "merged directly from the tick — Test/Review were "
                    "already satisfied, Merge was the only gate left (#2350)"
                ),
                session_name=None,
            )
            _record_block_log([merge_only_event(target, host=_local_host_id())])
            click.echo(f"merge-only: {target.key} merged directly from the tick")
            continue

        # #2350: the race case — fall back to exactly today's `resumed`
        # shape, never a consumed drive-queue attempt (this was never a
        # `coord drive --tmux` launch).
        reason = f"merge-only attempt for {target.key} did not land it this tick"
        if message:
            reason += f" ({message})"
        reason += " — falling back to an ordinary relaunch (#2350)"
        updates: dict[str, Any] = {
            "state": STATE_WAITING,
            "attempts": 0,
            "last_reason": reason,
        }
        if target.state == STATE_BLOCKED:
            updates["resumes"] = target.resumes + 1
        update_drive_queue_entry(target.repo, target.issue, **updates)
        # #2350: this write bypassed `plan.writes()` entirely, so the
        # `plan_events` call below (keyed off the ORIGINAL plan) can never
        # see it — without recording it here, the episode `plan_events`
        # already opened for `target` when it first went `parked`/`blocked`
        # would never see a matching close.
        _record_block_log(
            [merge_only_fallback_event(target, reason=reason, host=_local_host_id())]
        )
        click.echo(f"merge-only: {reason}")


def _run_auto_revalidate_checks_stale(config_path: Path | None) -> None:
    """#2535: best-effort auto-fire of the CI-staleness rerun for merge-queue
    entries blocked SOLELY on stale CI checks against an already-approved
    review — closing the gap where nothing periodic ever calls a live
    ``coord merge`` or ``coord merge --revalidate`` on such an entry's
    behalf, so it just sits until an operator notices ``--dry-run``'s
    ``checks_stale`` line and reruns by hand (the trigger: #2530's Gate-A
    PR #2534 sat blocked 2026-08-21 on nothing but 210 unrelated merges
    having landed since its CI last ran).

    Deliberately narrow — this is NOT ``merge.auto_drain`` reopened (that
    flag stays ``False`` by design; see ``docs/DRIVE_QUEUE.md`` and the
    2026-06-07 incident it guards against). This step never merges anything
    and never touches an entry blocked on review, a real CI failure, or a
    conflict — it only triggers a ``gh run rerun`` for the exact shape
    :func:`coord.merge_queue.ci_revalidation_candidates` already scopes
    ``--revalidate``'s CI arm to (#1851/#1925): ``PENDING``, review
    approved, smoke fresh, CI checks green but predating the current base.

    BOUNDED, shared with the live path. Draws from the SAME
    ``ci_stale_reruns``/``MAX_CI_STALE_RERUNS`` budget
    :func:`coord.merge_queue.process`'s own #2197 auto-rerun already spends
    from — a live ``coord merge`` attempt and this tick share one ceiling,
    never two independent ones that could double the effective retry count.
    An entry that has already exhausted the budget (on a prior live attempt
    or a prior tick) is left for a human exactly as #2197 already leaves it;
    this function never fires a further rerun past the cap, though it does
    keep recording the escalation (below) for as long as the entry stays
    stuck, so a human watching the audit trail sees it, not just the first
    tick that hit the ceiling.

    Cost-visible (#1632's posture): every triggered rerun is a real CI run
    on GitHub's own runners, recorded via :func:`coord.audit.record_audit`
    (operational tier) so an operator can see the auto-fired count — same
    spirit as the notifier being advisory rather than silent, never a
    silent background spend.

    Deliberately NOT behind a new config flag — this reuses exactly the
    bound and the counter #2197 already established for "an automatic CI
    rerun triggered by staleness"; the only thing new here is a periodic
    caller for the case nothing was already about to call ``process()``
    live. If that judgment turns out to be wrong in practice, the fix is a
    ``merge.auto_revalidate_stale_ci`` off-switch, not removing the
    dedicated budget this already shares.

    Best-effort like every other optional step in this tick (conflict
    reconciliation in ``_auto_drain_tick``, the merge-only fast path above):
    a failure here must never abort the rest of the tick. Skipped entirely
    on a thin client — this daemon-host tick is the only place it ever
    runs, same guard :func:`_fetch_live_ci_gate`/
    :func:`_run_merge_only_candidates` use.
    """
    from coord.board_service import resolve as resolve_board_service  # noqa: PLC0415

    if resolve_board_service() is not None:
        return

    try:
        from coord import github_ops as _gh_ops  # noqa: PLC0415
        from coord import merge_queue as _mq  # noqa: PLC0415
        from coord.audit import record_audit  # noqa: PLC0415
        from coord.ci_store import build_ci_store  # noqa: PLC0415
        from coord.commands._common import _load_config  # noqa: PLC0415
        from coord.state import load_board as _load_board  # noqa: PLC0415

        cfg = _load_config(config_path)
        board = _load_board()
        ci_store = build_ci_store(
            cfg.ci_store.type, host=cfg.ci_store.host, token_env=cfg.ci_store.token_env,
        )
        if ci_store is None or not ci_store.is_available:
            return
        items = _mq.load_queue()
        candidates = _mq.ci_revalidation_candidates(items, board, cfg, ci_store, _gh_ops)
    except Exception:  # noqa: BLE001 — best-effort, see docstring
        return

    if not candidates:
        return

    touched: list = []
    for entry in candidates:
        label = f"{entry.repo_name} #{entry.issue_number} ({entry.branch})"
        if entry.ci_stale_reruns >= _mq.MAX_CI_STALE_RERUNS:
            # Budget already spent (on a previous tick, or a previous live
            # `coord merge` attempt) — #2197's own terminal wording is
            # already on the entry from whichever call last evaluated it.
            # Nothing new to trigger; still worth one audit row per tick so
            # an operator watching the trail sees this has been sitting
            # exhausted, not silently forgotten.
            record_audit(
                tier="operational", category="merge",
                event_type="merge_checks_stale_auto_revalidate_exhausted",
                actor="drive-queue-tick",
                summary=(
                    f"auto-revalidate: {label} still checks_stale after "
                    f"{entry.ci_stale_reruns}/{_mq.MAX_CI_STALE_RERUNS} "
                    "auto-reruns — needs a human (`coord merge --revalidate` "
                    "or `coord merge --only`)"
                ),
                repo=entry.repo_name, issue=entry.issue_number,
                assignment_id=entry.assignment_id,
                details={
                    "pr_number": entry.pr_number,
                    "ci_stale_reruns": entry.ci_stale_reruns,
                },
            )
            continue
        try:
            reran = ci_store.rerun_for_pr(entry.repo_github, entry.pr_number)
        except Exception as exc:  # noqa: BLE001 — one entry's failure must not sink the rest
            click.echo(
                f"auto-revalidate: could not re-run CI for {label}: {exc}", err=True
            )
            continue
        entry.ci_stale_reruns += 1
        entry.error = (
            f"{_mq.CI_PENDING_PREFIX} re-run triggered for CI checks that "
            "predate the current base (#2535 auto-revalidate "
            f"{entry.ci_stale_reruns}/{_mq.MAX_CI_STALE_RERUNS} "
            f"{'triggered' if reran else 'failed to trigger'})"
        )
        touched.append(entry)
        click.echo(
            f"auto-revalidate: {'triggered' if reran else 'FAILED to trigger'} "
            f"a CI re-run for {label} (checks_stale, review already approved) "
            f"— {entry.ci_stale_reruns}/{_mq.MAX_CI_STALE_RERUNS}"
        )
        record_audit(
            tier="operational", category="merge",
            event_type="merge_checks_stale_auto_revalidate",
            actor="drive-queue-tick",
            summary=(
                f"auto-revalidate: {'triggered' if reran else 'FAILED to trigger'} "
                f"a CI re-run for {label} — {entry.ci_stale_reruns}/"
                f"{_mq.MAX_CI_STALE_RERUNS}"
            ),
            repo=entry.repo_name, issue=entry.issue_number,
            assignment_id=entry.assignment_id,
            details={"pr_number": entry.pr_number, "reran": reran},
        )

    if not touched:
        return

    try:
        from coord import merge_queue as _mq  # noqa: PLC0415

        fresh = _mq.load_queue()
        by_id = {e.assignment_id: e for e in touched}
        merged = [by_id.get(item.assignment_id, item) for item in fresh]
        _mq.save_queue(merged)
    except Exception:  # noqa: BLE001 — best-effort persistence; next tick recomputes
        pass


def _run_resume_probe(entry: QueueEntry) -> ProbeResult:
    """Run one entry's ``--resume-when`` probe with a hard timeout.

    TRUST BOUNDARY — READ THIS BEFORE TOUCHING THE FUNCTION.
    ``resume_when`` is a SHELL command, executed by the tick, as the tick's
    user, on the daemon host.  That is deliberate, and it is acceptable for
    exactly one reason: the string is **operator-authored and
    operator-scoped**, the same trust level as the ``ExecStart=`` line of the
    systemd timer unit that invokes this tick in the first place.  It is:

    * NOT sent to a worker and never executed on a worker machine;
    * NOT derived from an issue body, a PR, a review comment, a plan, or any
      other model output;
    * NOT reachable by anything an agent writes — ``coord drive-queue add`` is
      the only writer of this column, and DQ-1's update whitelist
      (``coord.state._DRIVE_QUEUE_UPDATABLE``) deliberately excludes it, so
      not even the tick can rewrite its own probe.

    If any of those three ever stops being true, this is remote code execution
    on the daemon host and the feature must be redesigned — not patched.

    Fails CLOSED: a non-zero exit, a timeout, or a command that could not be
    spawned at all all keep the gate held.  A gate that releases because its
    probe crashed is a gate that never existed.
    """
    import os  # noqa: PLC0415
    import signal  # noqa: PLC0415

    try:
        proc = subprocess.Popen(  # noqa: S602 — operator-authored; see the trust note
            entry.resume_when,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            # Its own process GROUP, so a timeout can kill the whole tree.
            # `sh -c 'a | b'` leaves children that outlive the shell; killing
            # only the shell would leave a wedged probe holding the pipe and
            # the tick blocked in communicate() — a tick that stops ticking is
            # indistinguishable from a queue with nothing to do (#1616).
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        return ProbeResult(entry.key, False, f"could not run the probe: {exc}")

    try:
        out, _ = proc.communicate(timeout=RESUME_PROBE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            proc.kill()
        try:
            proc.communicate(timeout=2.0)
        except (subprocess.SubprocessError, OSError):
            pass
        return ProbeResult(
            entry.key,
            False,
            f"timed out after {RESUME_PROBE_TIMEOUT_SECONDS:.0f}s (killed)",
        )

    tail = (out or "").strip().splitlines()
    detail = f"exit {proc.returncode}"
    if proc.returncode != 0 and tail:
        detail += f": {tail[-1][:160]}"
    return ProbeResult(entry.key, proc.returncode == 0, detail)


def _apply_writes(plan: TickPlan) -> None:
    from coord.state import update_drive_queue_entry  # noqa: PLC0415

    for key, updates in plan.writes():
        parsed = parse_key(key)
        if parsed is None:
            continue
        update_drive_queue_entry(parsed[0], parsed[1], **updates)


def _escalate(repo: str, issue: int, *, reason: str, gates: str, command: str) -> None:
    from coord.state import record_drive_escalation  # noqa: PLC0415

    record_drive_escalation(
        repo,
        issue,
        stage=QUEUE_ALERT_STAGE,
        reason=reason,
        gate_readings=gates,
        proposed_command=command,
    )


def _requeue_command(entry: QueueEntry | None, key: str) -> str:
    """The one-key fix for a blocked entry: drop it and re-add it clean.

    There is deliberately no ``coord drive-queue reset`` — DQ-1's update
    whitelist keeps run state out of the operator's write surface, so
    remove+add IS the reset (a fresh row is ``waiting`` with ``attempts=0``
    and no ``after``).  Re-adding without the bad ``--after`` is also the fix
    for an unsatisfiable pre-req.
    """
    parsed = parse_key(key)
    if parsed is None:
        return "coord drive-queue list"
    repo, issue = parsed
    tail = f" --machine {entry.machine}" if entry is not None and entry.machine else ""
    return (
        f"coord drive-queue remove {repo} {issue} && "
        f"coord drive-queue add {repo} {issue}{tail}"
    )


def _blocked_escalation_command(entry: QueueEntry | None, key: str, reason: str) -> str:
    """The command a `blocked`/`oscillating` escalation should propose
    (#3016) — `_requeue_command` ONLY for a genuinely never-dispatched entry;
    a gate-specific remedy (or the safe read-only inspect fallback) whenever
    *reason* already names a merge-gate block.

    `_requeue_command` is right exactly once: nothing was ever dispatched
    for this row (or every dispatch attempt died before an assignment
    existed), so there is nothing left to lose by dropping and re-adding it.
    But `is_merge_gate_block_reason` names the other shape — Work/Test/
    Review already completed and it is the MERGE stage that is stuck — where
    the same requeue would discard that completed cycle instead of fixing
    the one gate actually blocking it. `merge_gate_remedy_command` is the
    one place that maps a merge-gate reason to its safe one-line fix (or, if
    none is known blind, the inspect command); this only decides WHICH of
    the two families applies.
    """
    if is_merge_gate_block_reason(reason):
        parsed = parse_key(key)
        if parsed is not None:
            return merge_gate_remedy_command(reason, parsed[0], parsed[1])
    return _requeue_command(entry, key)


@drive_queue_group.command("tick")
@click.option(
    "--max-parallel",
    type=int,
    default=1,
    show_default=True,
    help=(
        "Concurrency ceiling. Capacity is counted from BOARD state, not from a "
        "session count, so a drive whose observer hit its deadline (#1660) "
        "still occupies a slot."
    ),
)
@click.option(
    "--max-parallel-per-repo",
    type=int,
    default=None,
    help=(
        "Per-repo concurrency ceiling, applied after --max-parallel (#1972). "
        "An entry whose repo is already at it DEFERS — position unchanged, no "
        "attempt spent — so the walk lands on the first entry from a repo with "
        "headroom: per-repo serialisation, cross-repo parallelism. 0 disables "
        "it, restoring one global counter. Omit this flag to use "
        "pipeline.max_parallel_per_repo from coordinator.yml (or, absent "
        f"that, {DEFAULT_MAX_PARALLEL_PER_REPO}, #2573) — passing it "
        "explicitly always wins over both. Prefer the config setting over a "
        "systemd drop-in for anything that needs to persist: a drop-in has "
        "to restate the packaged unit's whole ExecStart= to change just this "
        "flag, and that copy silently drifts from the packaged unit's other "
        "flags over time (#2573)."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print the resolved plan and mutate nothing.",
)
@click.option(
    "--reconcile-only",
    is_flag=True,
    default=False,
    help=(
        "Update the queue's view of reality and launch nothing (#2110). "
        "Every `running` entry is still checked against the board "
        "(done/blocked/parked/retry, exactly as a normal tick would), but no "
        "new `coord drive` is ever started this run, and no `coord merge "
        "--only` fast-path attempt (#2350) runs either — equivalent to "
        "`--max-parallel 0`. This is the missing primitive for the "
        "stop-the-timer-to-roll-the-fleet sequence: with the timer stopped, "
        "nothing reconciles a finished drive's `running` row, and that stale "
        "row alone can pin `coord release propagate` indefinitely. Run this "
        "once (by hand, timer still stopped) to drain it before propagating, "
        "then restart the timer."
    ),
)
@_CONFIG_OPTION
def drive_queue_tick(
    max_parallel: int,
    max_parallel_per_repo: int | None,
    dry_run: bool,
    reconcile_only: bool,
    config_path: Path,
) -> None:
    """Drain one step of the queue: reconcile, then launch at most one drive.

    Safe to run on any interval and from any machine that can reach the board
    daemon. A tick already in progress makes this a quiet no-op (exit 0) — a
    slow tick must never stack, and two ticks seconds apart are safe: a drive
    launched inside the startup grace window reconciles as `starting`
    (occupying a slot, attempts untouched) rather than as a death (#1794).
    Two ticks on DIFFERENT machines are also safe: liveness is always a local
    `tmux` read, so a tick reconciles only the entries it itself launched —
    one launched elsewhere reads as `unknown`, occupying its slot but never
    retried or relaunched, rather than being declared dead out from under the
    host actually running it (#1870).

    Capacity has two ceilings (#1972): the global `--max-parallel`, then
    `--max-parallel-per-repo`. Entries whose repo is already at the per-repo
    ceiling defer, so a tick with a claude-coordinator drive running skips the
    38 queued claude-coordinator entries behind it and launches the quadraui
    one — per-repo serialisation, cross-repo parallelism. `--dry-run` prints
    the per-repo breakdown so "why didn't item 2 go?" is answerable from the
    output alone.

    #2573: `--max-parallel-per-repo` resolves in this order — the flag on
    THIS invocation, when given; else `pipeline.max_parallel_per_repo` from
    `coordinator.yml`; else `coord.drive_queue.DEFAULT_MAX_PARALLEL_PER_REPO`
    (1). Set the fleet-wide value in `coordinator.yml`, not a systemd
    drop-in — a drop-in has to restate the packaged unit's entire
    `ExecStart=` to override one flag, and that copy silently drifts from
    the packaged unit (a dellserver drop-in built to carry this flag
    reverted #2314's pinned-venv `ExecStart=` right back to a
    worker-overwritable path as an unnoticed side effect).

    `--max-parallel 0` (or `--reconcile-only`, the readable spelling of the
    same thing — #2110) reconciles every `running` entry against the board and
    then stops: no capacity walk, no deferrals, no queue-level alert, no
    launch. Reconciliation (`plan_tick` step 1/1b: a finished entry moves to
    `done`, a permanently-refused one to `blocked`, a CI-pending one parks) is
    unconditional and runs regardless of capacity, which is what makes this
    safe to run with the periodic timer stopped — see
    `docs/AGENT_OPERATIONS.md`'s propagation section for why that combination
    used to deadlock.

    #2587: when a fleet roll is pending (`coord release propagate`/
    `nightly-window` set the marker instead of draining — see
    `coord.drive_queue.RollPending`), this tick refuses to LAUNCH, whatever
    `--max-parallel` says — the same capacity-0 posture `--reconcile-only`
    gives #2110 — and, once THIS tick's own reconciliation empties the queue
    out (zero entries still occupying a slot), fires the roll
    (`systemctl --user start --no-block coord-release-window.service`).
    Unlike an EXPLICIT `--reconcile-only`/`--max-parallel 0` run, a pending
    roll does not also skip the #2350 direct-merge fast path: merging an
    already-fully-approved entry is not "launching new work", and letting it
    proceed frees that entry rather than leaving it queued for the whole
    span the marker survives. Never stops `coord-drive-queue.timer` to reach
    the gap — see the marker's own docstring for why that would reproduce
    #2569. Firing the roll does NOT clear the marker itself — see
    `RollPending`'s own docstring for why that clear belongs solely to the
    spawned `coord-release-window.service`, once it has actually confirmed
    the roll.
    """
    from coord.filelock import FileLock, LockBusy, drive_queue_lock_path  # noqa: PLC0415
    from coord.state import list_drive_queue, update_drive_queue_entry  # noqa: PLC0415

    if max_parallel < 0:
        raise click.ClickException(
            "--max-parallel must be at least 0 (0 = reconcile-only, launch "
            "nothing this run — see --reconcile-only)"
        )

    # #2573: an explicit `--max-parallel-per-repo` always wins; otherwise
    # fall back to the fleet-wide `pipeline.max_parallel_per_repo` in
    # coordinator.yml, and only then to the hardcoded default. Resolved
    # BEFORE validation below so a bad value from either source is caught
    # the same way regardless of which one supplied it.
    if max_parallel_per_repo is None:
        try:
            from coord.commands._common import _load_config  # noqa: PLC0415

            config_default = _load_config(config_path).pipeline.max_parallel_per_repo
        except Exception:  # noqa: BLE001 — an unreadable config must not abort the tick
            config_default = None
        max_parallel_per_repo = (
            DEFAULT_MAX_PARALLEL_PER_REPO if config_default is None else config_default
        )

    if max_parallel_per_repo < 0:
        raise click.ClickException(
            "--max-parallel-per-repo must be 0 (no per-repo ceiling) or more"
        )

    # #2110: `--reconcile-only` and `--max-parallel 0` are the same request —
    # one flag is a mnemonic for the other rather than a second code path, so
    # there is exactly one way this behaves, not two that could drift apart.
    reconcile_only = reconcile_only or max_parallel == 0
    # #2587 review: captured BEFORE a pending roll (below) forces
    # `reconcile_only` too — this is the operator's own EXPLICIT request,
    # used only to gate the #2350 merge-only fast path and the #2535
    # CI-revalidate sweep (both real external actions that flag's contract
    # promises not to take). A roll-pending marker must NOT also suppress
    # those: merging an already-approved entry is not "launching new work",
    # and skipping it would leave a mergeable entry queued for the marker's
    # entire span for no reason connected to the roll.
    explicit_reconcile_only = reconcile_only

    lock = FileLock(drive_queue_lock_path())
    try:
        lock.acquire(timeout=0.0)
    except LockBusy:
        # Quiet by design: this is the normal outcome when a timer fires while
        # the previous tick is still verifying a launch.  Noise here would
        # train the operator to ignore the log.
        click.echo("another drive-queue tick is running — skipping")
        return
    except OSError as exc:
        raise click.ClickException(f"could not take the drive-queue lock: {exc}") from None

    try:
        # FAIL CLOSED. An unreadable board is not "nothing is running"; it is
        # "we do not know what is running", and launching on that assumption is
        # how a sequential batch becomes concurrent on the fleet.
        try:
            board = _fetch_board_view_with_retry()
        except Exception as exc:  # noqa: BLE001 — every fetch failure is fatal here
            raise click.ClickException(
                f"could not read the board — aborting without launching anything: {exc}"
            ) from None

        # #2870: the raw rows are kept alongside the typed `entries` — see
        # `_roll_pending_may_fire`'s `queue_rows` parameter, which needs the
        # untyped `state`/`repo_name`/`issue_number` shape
        # `coord.release_propagate.assess_quiescence` reads, not `QueueEntry`.
        raw_queue_rows = list_drive_queue()
        entries = entries_from_rows(raw_queue_rows)

        # #2587: is a fleet roll queued for the next inter-drive gap? Read
        # BEFORE `effective_capacity` is resolved — a live, unexpired marker
        # forces this tick into the same capacity-0 posture `--reconcile-only`
        # already gives #2110 (see `RollPending`'s docstring for why this
        # reuses that path rather than inventing a new one): reconcile,
        # launch nothing. It does NOT also force `explicit_reconcile_only`
        # (captured above, BEFORE this), so the #2350 merge-only fast path
        # below still runs — see this function's own docstring for why.
        # An EXPIRED marker (TTL or deferral ceiling — `RollPending.expired`)
        # is dropped right here, loudly (`_escalate_roll_pending_expired`),
        # and this tick proceeds exactly as if no marker had ever been set —
        # the #2587 "never hold the queue down indefinitely" requirement.
        # #2889: before it is dropped, its lived duration is folded into the
        # roll LEDGER (`RollLedger`, `roll_pending_ledger.json`) — memory
        # that survives THIS clear, unlike the marker itself, so a fresh arm
        # right after this one cannot dodge the cumulative bound the way a
        # brand new `RollPending` dodges a single marker's own TTL. Crossing
        # that bound is its own, separate, louder escalation.
        now = time.time()
        roll_pending = read_roll_pending()
        if roll_pending is not None and roll_pending.expired(now):
            _escalate_roll_pending_expired(roll_pending, now=now)
            ledger = read_roll_ledger()
            was_escalated = ledger.escalated
            ledger = ledger.record_expiry(roll_pending, now=now)
            write_roll_ledger(ledger)
            if ledger.escalated and not was_escalated:
                _escalate_roll_ledger(ledger, now=now)
            clear_roll_pending()
            roll_pending = None
        if roll_pending is not None:
            reconcile_only = True
        effective_capacity = 0 if reconcile_only else max_parallel

        # #1757: run each held gate's `--resume-when` BEFORE deciding
        # anything, and hand the results to `plan_tick` as data so the
        # decision half stays pure.  Deliberately skipped under `--dry-run`:
        # the probe is an arbitrary operator-authored shell command and
        # `--dry-run` promises to touch nothing.  The consequence — a dry run
        # reports the gate as still held even if the deploy just landed — is
        # stated in the output rather than left for the operator to discover.
        probes: dict[str, ProbeResult] = {}
        pending = pending_probe_targets(entries)
        if pending and dry_run:
            click.echo(
                f"(--dry-run: not running {len(pending)} --resume-when probe(s); "
                "a held gate below may already be releasable)"
            )
        elif not dry_run:
            for target in pending:
                probes[target.key] = _run_resume_probe(target)

        # #1845/#1844: the drive's own `drive_exited` summary — and, when it
        # was a PERMANENT pre-dispatch guard refusal, that fact too — for
        # each `running` entry, when one was recorded for THIS launch. Read
        # here (the shell) and handed to `plan_tick` as data, same as
        # `probes`, so a "no session, no active work, nothing landed"
        # reconcile can report the drive's real reason instead of a
        # synthesised "drive session died" for an exit that was actually
        # deliberate, and — when it was a deterministic refusal — block
        # immediately instead of spending an attempt on a guaranteed-to-fail
        # retry. #2019 adds a second permanent cause with the same handling:
        # a drive that exited on its own dead-end predicate.
        exit_reasons, exit_refused, exit_dead_end = _fetch_exit_reasons(entries)

        # #2063: for every entry parked on a missing Gate-A human sign-off,
        # re-read the recorded verdict so `plan_tick` can un-park it the tick
        # after the operator approves. Board-only (the (repo, milestone) pair
        # is embedded in the park reason's marker), so this costs nothing per
        # tick when no entry is parked that way — which is the common case.
        gate_a_pending = _fetch_gate_a_pending(entries)

        # #2182: for every entry parked on a CI reason, a fresh single-entry
        # re-derivation of its gate, taken live THIS tick — see
        # `_fetch_live_ci_gate`'s docstring for the gap this closes (a park
        # on the daemon-host tick could previously be released only by the
        # #2158 45-minute ceiling, never by CI actually reporting, because
        # this tick's board read never computes a live `merge_plan`).
        # #2347: the reason half — lets a still-parked entry's `last_reason`
        # be rewritten when the real cause is "GitHub unreachable" rather
        # than whatever it originally parked on; see `plan_tick`'s
        # `live_ci_gate_reason` parameter.
        live_ci_gate, live_ci_gate_reason = _fetch_live_ci_gate(entries, config_path)

        # #2230: the same live re-derivation, for RE-EVALUABLE `blocked`
        # entries — see `_fetch_live_blocked_gate`'s docstring for the gap
        # this closes (quadraui#309 sat `blocked` ~11h on a merge that was
        # landable for most of that window, and nothing ever looked again).
        live_blocked_gate, live_blocked_unreadable = _fetch_live_blocked_gate(
            entries, config_path
        )

        # #2350: for every entry the two live re-checks above just found
        # clear, also confirm — from the board's own recorded Test/Review
        # verdicts — that Merge was the only gate ever still shut, so
        # `plan_tick` can attempt it directly this tick instead of spending
        # a relaunch. See `_fetch_merge_only_ready`'s docstring.
        merge_only_ready = _fetch_merge_only_ready(
            entries, live_ci_gate, live_blocked_gate
        )

        # #2602: a live re-check for every `after=` pre-req the cached board
        # cannot yet confirm landed — see `_fetch_live_prereq_terminal`'s
        # docstring for the gap this closes (a pre-req that merges/closes
        # between `/board` builds used to permanently block every dependent
        # chained `--after` it, recoverable only by a manual remove+add).
        live_prereq_terminal = _fetch_live_prereq_terminal(entries, board, config_path)

        # #2101: release cordons. THIS is the hole the issue names — the
        # queue's launcher had zero pause awareness (`coord/drive.py` checks
        # pause only when routing a *worker*), so a cordoned host kept getting
        # drive sessions launched on it and the fleet could never drain into
        # a rollable state.
        cordons = _fetch_cordons()

        # #2314: is THIS host's own `coord` a drifted editable checkout?
        # Escalates `coord.cli._warn_if_editable_checkout_moved` from an
        # advisory-only startup warning (nothing unattended ever reads) to
        # an actual refusal — see `_editable_drift_alert` and `plan_tick`'s
        # `editable_drift` parameter for the gate itself.
        editable_drift = _fetch_editable_drift()

        # #1794: the clock is the shell's to read, never `coord.drive_queue`'s.
        # It powers the startup grace window on both sides of the tick — a
        # drive launched seconds ago is `starting`, not dead, and cannot be
        # relaunched — so a tick that fires immediately after another (which
        # `docs/DRIVE_QUEUE.md` §2's install sequence reliably produces) sees
        # a running entry rather than a phantom death.
        #
        # #1870: same posture for the machine's own identity. Liveness is a
        # LOCAL tmux read; without `local_host` a tick on host B would read a
        # healthy drive launched on host A as dead the instant it fell out of
        # #1794's grace window, and reap it.
        plan = plan_tick(
            entries,
            board,
            effective_capacity,
            max_parallel_per_repo=max_parallel_per_repo,
            probes=probes,
            now=now,
            local_host=_local_host_id(),
            exit_reasons=exit_reasons,
            exit_refused=exit_refused,
            exit_dead_end=exit_dead_end,
            gate_a_pending=gate_a_pending,
            cordons=cordons,
            live_ci_gate=live_ci_gate,
            live_ci_gate_reason=live_ci_gate_reason,
            live_blocked_gate=live_blocked_gate,
            live_blocked_unreadable=live_blocked_unreadable,
            editable_drift=editable_drift,
            merge_only_ready=merge_only_ready,
            roll_pending_reason=roll_pending.describe() if roll_pending is not None else "",
            live_prereq_terminal=live_prereq_terminal,
        )

        if roll_pending is not None:
            # #2587: name the marker, not just the generic reconcile-only
            # notice below — an operator reading the journal should see WHY
            # without cross-referencing `coord drive-queue status`.
            click.echo(
                f"({roll_pending.describe()}: updating queue state, launching "
                f"nothing until the queue empties out — {plan.occupied} "
                "entries still occupying a slot)"
            )
        elif reconcile_only:
            # #2110: capacity 0 already makes `plan_tick` return before its
            # capacity walk (no deferrals, no queue-level alert, no launch —
            # see its docstring's step 3), so `plan.launch` is guaranteed
            # `None` below without any extra branching here. This line exists
            # purely so the log reads as an intentional reconcile-only run
            # rather than a queue that mysteriously stopped launching.
            click.echo("(--reconcile-only: updating queue state, launching nothing)")

        for line in render_plan(plan, dry_run=dry_run):
            click.echo(line)
        if dry_run:
            return

        _apply_writes(plan)

        # #2587: this tick's own reconciliation (just applied above) may have
        # been the very thing that emptied the queue out — `plan.occupied` is
        # the POST-reconcile reading (see `RollPending`'s docstring), so this
        # is the earliest point at which "the inter-drive gap arrived" can be
        # known. Fires the roll the INSTANT that is true, rather than polling
        # a separate bounded drain — the whole point of #2587. Skipped
        # entirely once `dry_run` has already returned above, matching
        # `--dry-run`'s "mutate nothing" contract for every other write in
        # this function.
        if roll_pending is not None:
            # #2870: `plan.occupied == 0` alone is the pre-#2854 reading —
            # a between-legs row with no live assignment right now, past
            # its settle window, can sit `occupied` here (e.g. #1870's
            # `unknown` verdict for a row launched on another host)
            # forever, even though `coord release propagate` itself would
            # already call the fleet quiescent for that exact row. See
            # `_roll_pending_may_fire`'s own docstring for the full
            # mechanism and the incident this closes.
            fire_ready, fire_note = _roll_pending_may_fire(
                occupied=plan.occupied, now=now, queue_rows=raw_queue_rows
            )
            if fire_ready:
                if plan.occupied and fire_note:
                    click.echo(
                        f"(queue still shows {plan.occupied} occupying a slot, "
                        f"but #2854's settle-window reading calls it quiescent — "
                        f"{fire_note} — attempting the roll anyway)"
                    )
                fired, detail = _fire_pending_roll()
                if fired:
                    # #2587 review: this must NOT clear the marker. `fired`
                    # here only means `systemctl --user start --no-block`
                    # returned 0 — i.e. the start request was ACCEPTED — not
                    # that a roll happened; `--no-block` returns the instant
                    # the job is queued, before the spawned unit has even
                    # begun executing, let alone reached its own read of this
                    # marker. Clearing it here, in this same statement, raced
                    # the freshly spawned `coord-release-window.service` out
                    # from under it on every real invocation: that process
                    # (`coord.commands.release.release_nightly_window`, run
                    # with no `--target`) re-resolves the target from scratch
                    # — a PyPI lookup plus a fleet-wide health gather, both
                    # real network I/O — before it ever reaches its own
                    # `existing = read_roll_pending()`. By then this tick's
                    # `clear_roll_pending()` had already run, so `existing`
                    # was always `None` and the spawned process took the "no
                    # marker pending -> (re)set one and return" branch
                    # instead of ever calling `coord release propagate` — the
                    # roll never actually happened, silently, forever.
                    #
                    # The marker now stays exactly as it was (nothing here
                    # rewrites `set_at`/`deferrals` either — this tick spends
                    # no deferral on a successful fire attempt) and is
                    # cleared ONLY by the spawned process itself, and only
                    # once it has confirmed the roll — see
                    # `release_nightly_window`'s "existing marker pending for
                    # THIS target" branch, which attempts `coord release
                    # propagate` and clears the marker on a verified/rolled/
                    # up-to-date outcome. A `systemctl --user start` against
                    # an already-active `Type=oneshot` unit is systemd's own
                    # no-op (see `deploy/coord-release-window.service`), so
                    # re-firing on every subsequent tick while that process
                    # is still mid-run (or has already finished and cleared
                    # the marker, in which case `roll_pending` reads `None`
                    # next tick and this branch is never reached again) costs
                    # nothing. Bounded, as ever, by the marker's own TTL — a
                    # roll that never confirms still self-clears, per
                    # `RollPending.expired`, rather than holding the queue
                    # down waiting for a confirmation that never comes.
                    click.echo(
                        f"requested roll for {roll_pending.describe()}: {detail} "
                        "— marker left in place; coord-release-window.service "
                        "clears it once `coord release propagate` confirms the "
                        "roll"
                    )
                else:
                    # Could not even hand off to systemd — stays pending, one
                    # deferral spent, retried next tick. Loud (stderr): a
                    # roll that is quiescent-ready but cannot be started is
                    # not the benign "still busy" case below.
                    bumped = _bump_roll_pending_deferral(roll_pending)
                    write_roll_pending(bumped)
                    click.echo(
                        f"roll pending for v{roll_pending.target_version}: queue is "
                        f"quiescent but could not start coord-release-window.service "
                        f"({detail}) — will retry next tick "
                        f"({bumped.deferrals}/{bumped.max_deferrals} deferrals)",
                        err=True,
                    )
            else:
                # Still busy — the normal, expected steady state while
                # waiting for a gap. Quiet on purpose: this is every tick's
                # cadence (every ~3 minutes in production) until the gap
                # arrives, same "benign, not a stall" reasoning
                # `Deferral.benign` already applies to a cordon/repo-limit/
                # backoff deferral elsewhere in this module.
                bumped = _bump_roll_pending_deferral(roll_pending)
                write_roll_pending(bumped)

        # #2350: attempt the direct merges `plan_tick` marked `merge_only`,
        # right after the rest of the plan's writes have landed. Skipped
        # under an EXPLICIT `--reconcile-only`/`--max-parallel 0`
        # (`explicit_reconcile_only`, captured above before a roll-pending
        # marker can force plain `reconcile_only`) — that mode's whole
        # contract is "update queue state, launch nothing", and a live
        # `coord merge --only` attempt is exactly the external action it
        # promises not to take, same posture as the launch subprocess
        # further down. Deliberately NOT skipped merely because a roll is
        # pending (#2587 review): merging an already-fully-approved entry
        # is not "launching new work" — it is completing work the queue
        # already decided to do — and letting it proceed frees that entry
        # instead of leaving it queued, unmerged, for the marker's entire
        # span for no reason connected to the roll.
        if not explicit_reconcile_only:
            _run_merge_only_candidates(plan, config_path)

        # #2535: independent of the drive-queue plan above (this scans the
        # MERGE queue directly, not drive-queue rows) — a bounded, best-effort
        # CI re-run for any entry blocked solely on stale-but-green checks
        # with an already-approved review. Still gated on the FULL
        # `reconcile_only` (roll-pending included, unlike the merge-only fast
        # path just above) — a `gh run rerun` is a real external action, and
        # unlike a direct merge it does not free a queued entry on its own,
        # so there is no equivalent reason to run it while a roll is pending.
        if not reconcile_only:
            _run_auto_revalidate_checks_stale(config_path)

        # #2235 Phase 0: record every entry this tick moved INTO or OUT OF
        # `blocked`/`parked`, with the reason the queue stated and — for a
        # release — what the release itself reveals about the true cause.
        # Derived entirely from `entries` (the pre-tick snapshot) and `plan`
        # (already decided, already applied); nothing is re-read and nothing
        # here feeds back into the tick.
        #
        # `_record_block_log` -> `block_log.record` already swallows its own
        # I/O errors, but `plan_events` itself is NOT wrapped there — it is a
        # pure function over `entries`/`plan` with no I/O to fail, but the
        # module's own invariant ("recording must never change a decision"
        # / "an observability append that can fail a tick is strictly worse
        # than no observability" — see `coord/block_log.py`'s docstring) means
        # even an unanticipated edge case here must not turn an
        # already-applied, already-successful tick into a reported CLI
        # failure. Broad and silent to match `_record_operator_release`'s own
        # defensive read above.
        try:
            from coord.block_log import plan_events  # noqa: PLC0415

            _record_block_log(
                plan_events(entries, plan, host=_local_host_id(), now=time.time())
            )
        except Exception:  # noqa: BLE001 — observability only, never blocks the tick
            pass

        by_key = {e.key: e for e in entries}
        for item in plan.blocked:
            parsed = parse_key(item.key)
            if parsed is None:
                continue
            entry = by_key.get(item.key)
            _escalate(
                parsed[0],
                parsed[1],
                reason=item.reason,
                gates=(
                    f"queue_state=blocked | position="
                    f"{entry.position if entry else '?'} | after="
                    f"{','.join(entry.after) if entry and entry.after else '(none)'}"
                ),
                command=_blocked_escalation_command(entry, item.key, item.reason),
            )

        # #2230: an entry #2230's sweep would have resumed, but has already
        # oscillated blocked/waiting :data:`MAX_BLOCKED_RESUMES` times — the
        # "say so out loud" half of the issue. This is a SEPARATE record from
        # the `plan.blocked` loop above (that one only fires the tick an
        # entry FIRST reaches `blocked`; this one fires on every tick the
        # ceiling stays hit, same posture the queue-level alert already takes
        # for an ongoing stall) so the escalation names the oscillation
        # itself, not just "still blocked".
        for item in plan.reconciles:
            if item.outcome != "oscillating":
                continue
            parsed = parse_key(item.key)
            if parsed is None:
                continue
            entry = by_key.get(item.key)
            _escalate(
                parsed[0],
                parsed[1],
                reason=item.reason,
                gates=(
                    f"queue_state=blocked | resumes={entry.resumes if entry else '?'}"
                    f"/{MAX_BLOCKED_RESUMES} | position="
                    f"{entry.position if entry else '?'}"
                ),
                command=_blocked_escalation_command(entry, item.key, item.reason),
            )

        # #2806: a `blocked` entry #2230's sweep targeted this tick, but whose
        # live gate probe came back with no evidence at all — a merge-queue
        # row not enqueued yet, a missing PR number, `entry_gate_status`
        # raising. Distinct from BOTH the silent "no evidence, never asked"
        # case (pre-#2806 behaviour, still silent) and the oscillating loop
        # above: this says "I tried to read this entry's gate and could not",
        # which must never render the same as "I read it and it is still
        # shut" — see `coord.drive_queue._reconcile_blocked_unreadable`'s
        # docstring for the incident (vimcode#555) this closes.
        for item in plan.reconciles:
            if item.outcome != "gate_unreadable":
                continue
            parsed = parse_key(item.key)
            if parsed is None:
                continue
            entry = by_key.get(item.key)
            # #3016: NEVER the `remove && add` requeue here — `item.reason`
            # already says "the next tick re-probes rather than guessing"
            # (#2806); a destructive requeue directly contradicts that, and
            # would additionally discard a completed Work/Test/Review cycle
            # for no reason connected to what actually failed (a probe, not
            # a gate). The read-only inspect command is the only thing safe
            # to propose when the tick itself does not yet know what shape
            # this block is.
            _escalate(
                parsed[0],
                parsed[1],
                reason=item.reason,
                gates=(
                    f"queue_state=blocked | gate_reading=unreadable | "
                    f"position={entry.position if entry else '?'}"
                ),
                command=merge_plan_inspect_command(parsed[0]),
            )

        if plan.alert is not None:
            _escalate(
                QUEUE_ALERT_REPO,
                QUEUE_ALERT_ISSUE,
                reason=plan.alert.reason,
                gates=" | ".join(plan.alert.details),
                command=plan.alert.command,
            )
        else:
            # #2381: ANY resolved alert-raising condition — a released hold,
            # a lapsed/cleared cordon, a fixed editable-drift checkout — must
            # drop the stale escalation record in the SAME tick it resolves,
            # or `status` (and the TUI/`decisions` report reading the same
            # record) keeps shouting the old reason while the queue is
            # demonstrably running again. This used to be gated on `any(h.
            # outcome == "released" for h in plan.holds)` — the deploy-gate
            # case only — so a cleared CORDON (no `plan.holds` entry at all)
            # never cleared its own stale alert. `_clear_queue_alert()` is a
            # no-op when there is nothing to dismiss, so calling it on every
            # alert-free tick is safe and cheap.
            _clear_queue_alert()

        # #2572: independent of the routine alert record just above (which
        # `record_drive_escalation` overwrites — including its own
        # timestamp — every tick this stays true), track how long THIS
        # SPECIFIC self-cordon reason has held and push a direct escalation
        # once it crosses `SELF_CORDON_ESCALATE_AFTER_SECONDS`. See that
        # function's own docstring for the incident this closes.
        _escalate_persistent_self_cordon(
            plan.drift_reason, now=now, config_path=config_path
        )

        target = plan.launch
        if target is None:
            return

        argv = _launch_argv(target, config_path)
        try:
            result = subprocess.run(  # noqa: S603 — argv built from coord_argv + typed row
                argv,
                capture_output=True,
                text=True,
                timeout=_LAUNCH_TIMEOUT_SECONDS,
            )
            returncode = result.returncode
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            message = detail[-1] if detail else ""
        except (subprocess.SubprocessError, OSError) as exc:
            returncode, message = 1, str(exc)

        if returncode == 0:
            from coord.drive import drive_session_name  # noqa: PLC0415

            session = drive_session_name(target.repo, target.issue)
            update_drive_queue_entry(
                target.repo,
                target.issue,
                state=STATE_RUNNING,
                session_name=session,
                launched_at=time.time(),
                last_reason="",
                # #1870: stamp THIS host as the launcher so a later tick —
                # possibly on a different machine — knows whose tmux to trust.
                launch_host=_local_host_id(),
            )
            click.echo(f"launched {target.key} in tmux session {session!r}")
            return

        # #1606: `--tmux` only exits 0 once the session is live and writing its
        # run log, so a non-zero exit means nothing is running — record a
        # consumed attempt, never a running entry.
        #
        # #2230: `target` is `plan_tick`'s PRE-tick snapshot — its internal
        # `by_key` is never refreshed after step 1b's writes (see
        # `coord.drive_queue.plan_tick`'s own note on why `by_key` stays
        # frozen). An entry #2230's sweep just resumed from `blocked` had its
        # `attempts` reset to 0 by `_apply_writes` above; reading
        # `target.attempts` here would silently undo that reset the moment
        # the launch subprocess itself fails (before ever reaching tmux) —
        # rare, but exactly the "resumed only to give up again immediately"
        # failure the issue calls out by name. `plan.writes()` carries the
        # freshest resolved value for this key, the same one `_apply_writes`
        # already persisted; fall back to `target.attempts` only when this
        # tick wrote nothing for it (the common case — no reconcile touched
        # this entry's `attempts`).
        base_attempts = dict(plan.writes()).get(target.key, {}).get(
            "attempts", target.attempts
        )
        attempts = base_attempts + 1
        reason = (
            f"launch failed (exit {returncode}): {message}"
            if message
            else f"launch failed (exit {returncode})"
        )
        if attempts < DEFAULT_MAX_ATTEMPTS:
            update_drive_queue_entry(
                target.repo,
                target.issue,
                state=STATE_WAITING,
                attempts=attempts,
                last_reason=reason,
            )
        else:
            update_drive_queue_entry(
                target.repo,
                target.issue,
                state=STATE_BLOCKED,
                attempts=attempts,
                last_reason=reason,
            )
            _escalate(
                target.repo,
                target.issue,
                reason=reason,
                gates=f"queue_state=blocked | attempts={attempts}",
                command=_requeue_command(target, target.key),
            )
            # #2235 Phase 0: a launch that never reached tmux is #2235's own
            # `stick-demo#1` row. It blocks OUTSIDE the plan (the subprocess
            # exits after `_apply_writes`), so `plan_events` cannot see it.
            from coord.block_log import enter_event  # noqa: PLC0415

            _record_block_log(
                [
                    enter_event(
                        target,
                        state=STATE_BLOCKED,
                        reason=reason,
                        attempts=attempts,
                        host=_local_host_id(),
                    )
                ]
            )
        raise click.ClickException(reason)
    finally:
        lock.release()
