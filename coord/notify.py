"""Poll agent servers and post completion/failure comments to GitHub."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from coord.diagnose import PhantomRowHeal, StuckTestStateHeal
    from coord.merge_queue import QueuedMerge
    from coord.models import Assignment, Board
    from coord.progress import SmokeVerdict

log = logging.getLogger(__name__)

# Cache: machine_name → host. Populated by `run(config)` so post_transition →
# _try_parse_and_post_review can fetch a remote agent's log via /logs/<id>
# without threading the Config through every helper.
_AGENT_HOSTS: dict[str, str] = {}


def _agent_host(machine_name: str) -> str | None:
    return _AGENT_HOSTS.get(machine_name)

from coord import github_ops
from coord.comments import (
    EVENT_ADVISORY,
    EVENT_COMPLETION,
    EVENT_FAILURE,
    EVENT_LIVENESS_STALL,
    EVENT_NEEDS_ATTENTION,
    EVENT_PLAN,
    EVENT_REFUSED_POLICY,
    EVENT_STALLED,
    EVENT_STUCK,
    format_liveness_stall,
    format_needs_attention,
    format_plan,
    format_stalled_pipeline,
    format_stalled_pipeline_dispatch,
    format_stuck,
)
from coord.config import Config
from coord.dispatch import (
    AGENT_PORT,
    post_advisory,
    post_completion,
    post_failure,
    post_refused_policy,
)
from coord.progress import parse_progress

# #2272: the mute-Test-stage retry budget. Canonically in `coord.smoke`
# because `dispatch_smoke` is the writer that has to carry the tally across
# its own `running` stamp — a second definition here is exactly how the
# counter and the thing that clobbers it drift apart again. Safe as a
# module-level import: `coord.smoke` reaches config/dispatch/models/revalidate
# and none of them import this module.
from coord.smoke import MUTE_SMOKE_LEG_BUDGET, TEST_STATE_BLOCKED
from coord.smoke import NO_SMOKE_VERDICT_MARKER as _NO_SMOKE_VERDICT_MARKER
from coord.smoke import mute_smoke_legs, mute_smoke_tally
from coord.state import (
    load_dispatched,
    load_done_reviews_needing_post,
    load_liveness_audit_state,
    load_notified,
    mark_notified,
    mark_review_posted,
    save_liveness_audit_state,
    save_plan,
)

# #1710 inventory: kept as a direct import — `is_usage_limit_reason` is a
# trivial string-prefix predicate over `Assignment.failure_reason` (a
# coordinator-authored value stamped by `format_usage_limit_reason`, itself
# only ever produced by the reap path's claude-specific kill detection), not
# a per-provider log-format parse. Any provider's `failure_reason` would be
# checked the same way.
from coord.worker_events import is_usage_limit_reason


@dataclass
class Transition:
    assignment_id: str
    machine_name: str
    repo_name: str
    issue_number: int
    event: str  # completion | failure
    exit_code: int | None


@dataclass
class StuckDetection:
    assignment_id: str
    machine_name: str
    repo_name: str
    issue_number: int
    stuck_message: str
    log_path: str | None


def _stuck_notified_key(assignment_id: str) -> str:
    """Notified ledger key for stuck events.

    Uses a composite key so that a stuck notification does not block later
    completion/failure notifications (which key on bare assignment_id).
    """
    return f"{assignment_id}:stuck"


@dataclass
class NeedsAttentionDetection:
    assignment_id: str
    machine_name: str
    repo_name: str
    issue_number: int
    reason: str  # "wall_clock" | "non_convergence"
    detail: str


def _needs_attention_notified_key(assignment_id: str) -> str:
    """Notified ledger key for needs-attention events (#846).

    Composite key (mirrors :func:`_stuck_notified_key`) so a one-shot
    needs-attention comment does not block later completion/failure/stuck
    notifications, and vice versa.
    """
    return f"{assignment_id}:needs-attention"


@dataclass
class StalledDetection:
    """#1441: a pipeline row whose auto-loop transition already fired once
    but which is stuck on a precondition that landed too late for that
    one-shot reaction to see. See :func:`detect_stalled_pipeline`."""

    assignment_id: str
    machine_name: str
    repo_name: str
    issue_number: int
    reason: str  # "review_request_changes_no_fix" | "review_done_no_verdict" |
    # "done_no_review" | "approved_not_queued" | "merge_conflict_unresolved"
    # (#1478, #1582) | "review_failed_no_verdict" (#1584)
    detail: str


def _stalled_notified_key(assignment_id: str) -> str:
    """Notified ledger key for stalled-pipeline events (#1441).

    Composite key (mirrors :func:`_needs_attention_notified_key`) so a
    one-shot stalled-pipeline comment does not block later completion/
    failure/stuck/needs-attention notifications for the same assignment_id,
    and vice versa.
    """
    return f"{assignment_id}:stalled"


@dataclass
class LivenessStallDetection:
    """#2048: N consecutive ``blocked`` verdicts from the cheap per-turn
    liveness auditor. See :func:`detect_liveness_stall`."""

    assignment_id: str
    machine_name: str
    repo_name: str
    issue_number: int
    consecutive_blocked: int
    last_verdict: str | None


def _liveness_notified_key(assignment_id: str) -> str:
    """Notified ledger key for liveness-stall events (#2048).

    Composite key (mirrors :func:`_needs_attention_notified_key` /
    :func:`_stalled_notified_key`) so a one-shot liveness comment does not
    block later completion/failure/stuck/needs-attention/stalled
    notifications for the same assignment_id, and vice versa. This exact
    shape is also what keeps ``mark_notified``'s bare-``else`` branch from
    ever writing ``status='failed'`` onto a real assignment row for this
    event — see the comment in ``coord.state._mark_notified_local``.
    """
    return f"{assignment_id}:liveness"


def _fmt_minutes(seconds: float) -> str:
    minutes = seconds / 60.0
    if minutes < 1:
        return f"{seconds:.0f}s"
    if minutes == int(minutes):
        return f"{int(minutes)}m"
    return f"{minutes:.1f}m"


def attention_signal(
    *,
    assignment_type: str,
    status: str | None,
    dispatched_at: float | None,
    review_iteration: int,
    config: Config,
    now: float | None = None,
    provider_name: str | None = None,
    review_of_assignment_id: str | None = None,
) -> tuple[str, str] | tuple[None, None]:
    """Pure #846 detection core: the two "needs attention" signals, decoupled
    from where the assignment's fields come from.

    1. **Non-convergence**: ``review_iteration >= config.pipeline.
       convergence_rounds`` fix/review rounds without reaching a terminal
       green test verdict + approved review. Checked first — a thrashing
       assignment is worth flagging even if it hasn't yet cleared the
       wall-clock threshold.
    2. **Wall-clock**: running longer than
       ``config.pipeline.attention_threshold_for(assignment_type,
       provider_name=..., review_of_assignment_id=...)``, computed from
       *dispatched_at*. ``provider_name``/``review_of_assignment_id``
       (#1137) let an interactive ``--fix-of``/``--rework-of`` session be
       recognized despite sharing ``type="work"`` with headless coding
       workers — see :meth:`Config.pipeline.attention_threshold_for`'s
       docstring. Both default to ``None`` (no effect) for callers that
       don't have the full assignment record.

    Deliberately time/round-based rather than self-report-based (#448: the
    failure mode that motivated this was a worker that never emitted a
    ``STUCK:`` line — it just silently burned budget while looking
    "productive").

    Shared by :func:`detect_needs_attention` (the coordinator backstop,
    dispatch-ledger-dict based), ``coord.pipeline.compute_pipeline`` (the
    ``/api/pipeline`` field the web dashboard renders), and the dashboard's
    background poller (``Assignment``-object based) — one signal, several
    call sites, instead of three copies of the same threshold logic.

    Returns ``(reason, detail)`` — ``reason`` is ``"wall_clock"`` or
    ``"non_convergence"`` — or ``(None, None)`` when nothing is flagged.
    """
    if (status or "").lower() != "running":
        return None, None
    if now is None:
        now = time.time()

    if review_iteration >= config.pipeline.convergence_rounds:
        return "non_convergence", (
            f"{review_iteration} fix/review round(s) on this assignment "
            f"without reaching a green test verdict + approved review "
            f"(threshold: {config.pipeline.convergence_rounds})."
        )

    threshold = config.pipeline.attention_threshold_for(
        assignment_type,
        provider_name=provider_name,
        review_of_assignment_id=review_of_assignment_id,
    )
    if dispatched_at is not None:
        running_for = now - dispatched_at
        if running_for > threshold:
            return "wall_clock", (
                f"Running {_fmt_minutes(running_for)}, past the "
                f"{_fmt_minutes(threshold)} threshold for "
                f"type={assignment_type!r}."
            )

    return None, None


def detect_needs_attention(
    config: Config, *, now: float | None = None
) -> list[tuple[NeedsAttentionDetection, dict]]:
    """Scan dispatched assignments for the two #846 "needs attention" signals
    (see :func:`attention_signal`). Detection only — no dispatch/kill/handoff
    behaviour.

    Returns ``(NeedsAttentionDetection, dispatch_record)`` pairs for
    assignments that haven't already been notified as needing attention (or
    reached a terminal notification), mirroring :func:`detect_stuck`'s shape
    so callers can post + mark idempotently the same way.
    """
    dispatched = load_dispatched()
    if not dispatched:
        return []
    notified = load_notified()

    active_records = [
        r for r in dispatched
        if r["assignment_id"] not in notified
        and _needs_attention_notified_key(r["assignment_id"]) not in notified
    ]
    if not active_records:
        return []

    results: list[tuple[NeedsAttentionDetection, dict]] = []
    for record in active_records:
        reason, detail = attention_signal(
            assignment_type=record.get("type") or "work",
            status=record.get("status"),
            dispatched_at=record.get("dispatched_at"),
            review_iteration=record.get("review_iteration") or 0,
            config=config,
            now=now,
            provider_name=record.get("provider_name"),
            review_of_assignment_id=record.get("review_of_assignment_id"),
        )
        if reason is None:
            continue
        results.append((
            NeedsAttentionDetection(
                assignment_id=record["assignment_id"],
                machine_name=record["machine_name"],
                repo_name=record["repo_name"],
                issue_number=record["issue_number"],
                reason=reason,
                detail=detail,
            ),
            record,
        ))

    return results


def post_needs_attention(detection: NeedsAttentionDetection, record: dict) -> None:
    """Post a needs-attention comment to GitHub and mark notified (#846)."""
    body = format_needs_attention(
        assignment_id=detection.assignment_id,
        machine_name=detection.machine_name,
        repo_name=detection.repo_name,
        issue_number=detection.issue_number,
        reason=detection.reason,
        detail=detection.detail,
    )
    github_ops.post_issue_comment(
        record["repo_github"], detection.issue_number, body
    )
    mark_notified(_needs_attention_notified_key(detection.assignment_id), EVENT_NEEDS_ATTENTION)


# ── Liveness auditor (#2048) ─────────────────────────────────────────────────
#
# Tier 2.5 in the stall-detection ladder (see coord/liveness_auditor.py's
# module docstring): a cheap, independent, per-turn judgment call, sitting
# between EVENT_NEEDS_ATTENTION (a clock, no judgment) and a metered
# adversarial review (judgment, but only at a stage boundary). Detection +
# a one-shot GitHub comment only, mirroring detect_needs_attention's/
# detect_stalled_pipeline's contract exactly: this function NEVER sets
# Assignment.status/review_state/test_state, never kills/reassigns a
# worker, and never influences a merge decision. It only ever (a) runs a
# `claude -p` subprocess against a fixed-size (objective, latest-turn)
# context and (b) records/reads the resulting strike streak.


def _latest_turn_text_for_liveness(
    machine_name: str, log_path: str | None, assignment_id: str,
) -> str | None:
    """Best-effort latest-assistant-turn text for a RUNNING assignment, for
    the liveness auditor.

    Mirrors :func:`_fetch_raw_log_text`'s local-file-then-agent-fetch
    fallback (that helper takes a completed :class:`Transition`; this one
    is called against a still-running assignment, so it takes the bare
    fields instead) — with one deliberate difference: the local-file
    branch uses :func:`coord.worker_events.latest_assistant_turn_text`'s
    seek-based tail read (``tail_bytes=65536``) instead of reading the
    whole file into memory. This runs once per debounce interval for the
    *entire lifetime* of a running assignment, so a full read here would
    quietly turn "audit cost is flat" into "disk I/O scales with log
    size" — worst in exactly the stuck-worker-with-a-growing-log scenario
    the auditor exists to catch (#2048 review). The remote (agent-fetch)
    branch has no tail-range support server-side, so it still fetches the
    full response and slices in Python.

    Returns ``None`` on any I/O failure or if the tail has no assistant
    turn yet — best-effort, the auditor must never be the reason
    ``coord notify`` raises.

    The ``log_path`` recorded on an :class:`~coord.agent.AgentAssignment` is
    a path on the *worker's own* machine, and ``coord notify`` normally runs
    on the daemon host — so for the multi-machine fleet topology this repo
    is built around, that path usually does NOT exist locally and the HTTP
    fallback is the only branch that can ever return text. The local branch
    is therefore taken only when the file actually exists: because
    :func:`~coord.worker_events.latest_assistant_turn_text` swallows a
    missing/unreadable file and returns ``None`` internally, committing to
    it unconditionally would make the agent-fetch below dead code and the
    auditor would silently never fire for any remote worker (#2048 review).
    """
    if log_path:
        from coord.worker_events import latest_assistant_turn_text  # noqa: PLC0415

        try:
            local_readable = Path(log_path).is_file()
        except OSError:
            local_readable = False
        if local_readable:
            return latest_assistant_turn_text(log_path, tail_bytes=65536)
    host = _agent_host(machine_name)
    if host:
        from coord.worker_events import latest_assistant_turn_text_from_text  # noqa: PLC0415

        try:
            resp = httpx.get(
                f"http://{host}:{AGENT_PORT}/logs/{assignment_id}", timeout=15.0
            )
            resp.raise_for_status()
        except (httpx.HTTPError, httpx.TimeoutException):
            return None
        return latest_assistant_turn_text_from_text(resp.text[-65536:])
    return None


def detect_liveness_stall(
    config: Config, *, now: float | None = None
) -> list[tuple[LivenessStallDetection, dict]]:
    """#2048: run the cheap per-turn liveness auditor against running
    assignments and flag the ones whose latest turns earned
    ``config.pipeline.liveness_auditor.strikes`` consecutive ``blocked``
    verdicts in a row.

    Returns ``(LivenessStallDetection, dispatch_record)`` pairs, mirroring
    :func:`detect_needs_attention`'s shape. No-ops entirely (returns ``[]``
    without touching the DB or spawning a subprocess) when
    ``config.pipeline.liveness_auditor.enabled`` is ``False`` — the default.
    """
    cfg = config.pipeline.liveness_auditor
    if not cfg.enabled:
        return []

    from coord.liveness_auditor import (  # noqa: PLC0415
        apply_verdict,
        run_audit,
        should_audit,
        strip_self_report_lines,
    )

    if now is None:
        now = time.time()

    dispatched = load_dispatched()
    if not dispatched:
        return []
    notified = load_notified()

    active_records = [
        r for r in dispatched
        if (r.get("status") or "").lower() == "running"
        and r["assignment_id"] not in notified
        and _liveness_notified_key(r["assignment_id"]) not in notified
    ]
    if not active_records:
        return []

    machines_by_name = {m.name: m for m in config.machines}
    by_machine: dict[str, list[dict]] = {}
    for r in active_records:
        by_machine.setdefault(r["machine_name"], []).append(r)

    # Deliberately serial: each `run_audit` call below is a subprocess
    # spawn with up to a `timeout_seconds` (default 30s) ceiling, and
    # audits for every not-yet-raised running assignment past its
    # debounce window run one at a time in this loop. At today's typical
    # concurrency this is a non-issue; if the audited fleet grows enough
    # for a single `coord notify` pass to stack up meaningful wall-clock
    # time here, parallelize (e.g. a bounded thread pool around
    # `run_audit`) rather than accept unbounded serial latency (#2048
    # review).
    results: list[tuple[LivenessStallDetection, dict]] = []
    for machine_name, records in by_machine.items():
        machine = machines_by_name.get(machine_name)
        if machine is None:
            continue
        status = _agent_status(machine.host)
        if status is None:
            continue
        active_by_id: dict[str, dict] = {}
        for entry in status.get("active", []):
            eid = entry.get("id")
            if eid:
                active_by_id[eid] = entry

        for record in records:
            aid = record["assignment_id"]
            entry = active_by_id.get(aid)
            if entry is None:
                continue

            state = load_liveness_audit_state(aid)
            if state.raised:
                continue
            if not should_audit(
                last_audit_at=state.last_audit_at,
                now=now,
                debounce_seconds=cfg.debounce_seconds,
            ):
                continue

            # Seek-based tail read for a local log (see
            # _latest_turn_text_for_liveness's docstring) — the auditor
            # only ever needs the single most recent turn, never the
            # whole (potentially multi-MB) transcript, and this repeats
            # every debounce interval for the assignment's whole runtime.
            turn_text = _latest_turn_text_for_liveness(
                record["machine_name"], entry.get("log_path"), aid
            )
            if turn_text is None:
                continue  # no assistant turn yet, or fetch failed

            # #2048 context isolation: strip the worker's own STATUS:/
            # STUCK: lines before the auditor ever sees this turn — see
            # coord.liveness_auditor's module docstring.
            turn_text = strip_self_report_lines(turn_text)

            outcome = run_audit(
                record.get("briefing") or "",
                turn_text,
                model=cfg.model,
                claude_bin=cfg.claude_bin,
                timeout=cfg.timeout_seconds,
            )
            new_state, just_raised = apply_verdict(
                state, outcome.verdict, now=now, strikes=cfg.strikes
            )
            save_liveness_audit_state(aid, new_state)

            if just_raised:
                results.append((
                    LivenessStallDetection(
                        assignment_id=aid,
                        machine_name=record["machine_name"],
                        repo_name=record["repo_name"],
                        issue_number=record["issue_number"],
                        consecutive_blocked=new_state.consecutive_blocked,
                        last_verdict=new_state.last_verdict,
                    ),
                    record,
                ))

    return results


def post_liveness_stall(detection: LivenessStallDetection, record: dict) -> None:
    """Post a liveness-stall comment to GitHub and mark notified (#2048)."""
    body = format_liveness_stall(
        assignment_id=detection.assignment_id,
        machine_name=detection.machine_name,
        repo_name=detection.repo_name,
        issue_number=detection.issue_number,
        consecutive_blocked=detection.consecutive_blocked,
    )
    github_ops.post_issue_comment(
        record["repo_github"], detection.issue_number, body
    )
    mark_notified(_liveness_notified_key(detection.assignment_id), EVENT_LIVENESS_STALL)


# ── Stalled-pipeline sweeper (#1441) ────────────────────────────────────────
#
# The auto-loop (coord.auto_loop) only reacts to review/fix TRANSITIONS — the
# instant `coord notify` sees a review or fix flip to `done` during THAT
# pass. Once the transition is consumed nothing ever re-examines the row, so
# a precondition that lands late (a Test verdict backfilled two days after
# the review completed — vimcode #602) leaves it stranded: looks complete on
# the board, isn't. This sweeper re-scans every *done* work chain on the
# board each notify pass and flags the ones stuck on an unmet precondition
# a fresh transition would have already resolved. Detection only — no
# dispatch, mirroring detect_needs_attention's contract.


def _pipeline_heads(board: "Board") -> list["Assignment"]:
    """Return the most-recent WORK_LIKE_TYPES assignment per (repo, issue).

    A row can be bounced through 1+ auto-loop fix iterations, each a
    separate ``Assignment`` sharing the same ``(repo_name, issue_number)``.
    Only the most recent one reflects the pipeline's actual current
    position — earlier rows in the chain are superseded, and evaluating them
    too would re-flag a condition a later fix already addressed.
    """
    from coord.models import WORK_LIKE_TYPES  # noqa: PLC0415

    all_assignments = list(board.active) + list(board.completed)
    heads: dict[tuple[str, int], "Assignment"] = {}
    for a in all_assignments:
        if a.type not in WORK_LIKE_TYPES:
            continue
        key = (a.repo_name, a.issue_number)
        ts = a.dispatched_at or a.finished_at or 0.0
        cur = heads.get(key)
        cur_ts = (cur.dispatched_at or cur.finished_at or 0.0) if cur is not None else -1.0
        if cur is None or ts >= cur_ts:
            heads[key] = a
    return list(heads.values())


def detect_stalled_pipeline(
    config: Config,
    *,
    board: "Board | None" = None,
    merge_queue_items: list | None = None,
    terminal_cache: dict | None = None,
    ignore_notified: bool = False,
) -> list[tuple[StalledDetection, "Assignment"]]:
    """Scan the board for *done* work chains stuck on an unmet precondition
    that a fresh review/fix transition would already have resolved (#1441).

    Five candidate stall states, checked per pipeline "head" (the most
    recent work-like assignment for a given (repo, issue) — see
    :func:`_pipeline_heads`):

    1. ``review_request_changes_no_fix`` — the head's linked review
       completed with verdict ``request-changes`` and no fix assignment was
       ever dispatched in response (the vimcode #602 reference case: the
       review's transition fired and was consumed while some other
       precondition was outstanding, and nothing has re-examined it since).
    2. ``review_done_no_verdict`` (#1582) — the head's linked review is
       ``status="done"`` but ``review_verdict IS NULL``: the reviewing
       session finalised without ever capturing a verdict (elitebook's
       documented ~14% review-verdict drop rate, #873).

       #2019: the detail text used to blame this on "the session likely
       failed to start or exited before recording one (#812)" for EVERY
       review. That was wrong twice over on the #1956 reference case — the
       session ran 392s, produced a complete 6.5KB review and exited 0, and
       #812 is CLOSED and was about *interactive* reviews, while that one was
       ``interactive=False``. An operator following it looked at a closed
       issue and a false cause. The wording is now provider-aware: a headless
       review that reached ``done`` is reported as the
       END_REVIEW-without-verdict class (#1956), and only a ``claude-pty``
       review still cites #812's never-started shape.

       This matches NONE of the other three arms — it
       isn't ``request-changes`` (no verdict at all), a review WAS
       dispatched (so not ``done_no_review``), and there is no approval (so
       not ``approved_not_queued``) — so before this arm existed it fell
       through every check and parked the drive forever (#1582's own
       observed case, #1563).
    3. ``done_no_review`` — the head carries a terminal Test verdict
       (``passed``/``skipped``), the "review" gate is required, the
       completion is not an interactive (``provider_name="claude-pty"``)
       session (interactive completions are deliberately excluded from
       automatic review dispatch — #555), and yet no review assignment was
       ever dispatched for it.
    4. ``approved_not_queued`` — the head satisfies every merge gate
       (:func:`coord.merge_queue.passes_merge_gates` — reused rather than
       re-derived, per #1441's own request) but has no merge-queue entry.
    5. ``merge_conflict_unresolved`` (#1478) — the head already HAS a
       merge-queue entry, but that entry is parked ``CONFLICT`` with an
       error :func:`coord.merge_queue.classify_conflict` calls
       ``"rebaseable"`` and no conflict-fix attempt is active or already
       failed (:func:`coord.conflict_fix.has_prior_conflict_fix`). This is
       exactly the gap :mod:`coord.commands.merge`'s
       ``_dispatch_conflict_fixes`` docstring calls out for the ``--only``
       path pre-#1474 — a bare ``CONFLICT`` row that never got a second
       classify-and-dispatch pass, except here for *any* path (not only
       ``--only``): a ``coord merge`` invocation that dispatched a
       conflict-fix which then failed to actually attempt (no idle
       machine) leaves the entry parked with nothing watching it.
    5. ``review_failed_no_verdict`` (#1584) — the head's linked review
       WORKER died (transient API error, network drop, ...) before ever
       producing a verdict — ``status="failed"`` with no
       ``review_verdict``. Before #1584 this could not happen (a dying
       review was mislabelled ``done``, silently masquerading as a real
       completion); now that it is correctly ``failed``, it needs its own
       arm here so it is not silently skipped (``reason`` staying ``None``)
       the way an unrecognized status would be.

    Every candidate is checked against the shared #522 terminal-state guard
    (:func:`coord.github_ops.work_is_terminal`, via *terminal_cache* — the
    same cache :func:`coord.notify.run` threads through the review/fix
    auto-loop calls) so a closed issue or merged PR never surfaces, and
    (unless *ignore_notified* is set) against the ``notified`` ledger
    (composite key, :func:`_stalled_notified_key`) so a flagged row is not
    re-flagged every pass.

    *ignore_notified* (#2679): the ``notified`` ledger is right for the
    one-shot GitHub comment this function's other callers post — chatty by
    design, so it must stop repeating itself. It is wrong for a *terminal*
    stall, which cannot resolve itself: miss the single comment and the row
    becomes permanently invisible even though the underlying condition is
    still true. ``coord health``'s ``stalled_pipeline`` check passes
    ``ignore_notified=True`` so it re-derives the same rows from live state
    on every run, independent of whatever the GitHub-comment ledger already
    recorded — see ``coord/health/checks/stalled_pipeline.py``.

    Detection only, mirroring :func:`detect_needs_attention`'s contract — no
    dispatch, no kill, no handoff (that lives in
    :func:`dispatch_stalled_pipeline_action`, #1478, gated behind
    ``config.pipeline.auto_dispatch_stalled``). *board* / *merge_queue_items*
    / *terminal_cache* are all optional so callers (tests, or a future
    ``reconcile()`` caller) can supply their own instead of hitting the
    board service / DB / GitHub.
    """
    # `github_ops` is already imported at module level (used by every other
    # post_* helper in this file) — no local re-import here, so a caller
    # that mocks `coord.notify.github_ops.post_issue_comment` for the
    # posting side doesn't also have to reason about a separately-imported
    # local name for the terminal-state check below.
    from coord.auto_loop import FIX_DISPATCH_TYPES  # noqa: PLC0415
    from coord.conflict_fix import has_prior_conflict_fix  # noqa: PLC0415
    from coord.merge_queue import (  # noqa: PLC0415
        CONFLICT,
        classify_conflict,
        live_gate_entry,
        load_queue,
        passes_merge_gates,
    )
    from coord.models import trust_issue_closed_for  # noqa: PLC0415

    if board is None:
        from coord.board_service import read_board  # noqa: PLC0415
        board = read_board()
    if merge_queue_items is None:
        merge_queue_items = load_queue()
    if terminal_cache is None:
        terminal_cache = {}

    notified = load_notified()
    all_assignments = list(board.active) + list(board.completed)

    results: list[tuple[StalledDetection, "Assignment"]] = []
    for work in _pipeline_heads(board):
        if work.status != "done" or not work.assignment_id:
            continue
        if not ignore_notified and _stalled_notified_key(work.assignment_id) in notified:
            continue

        repo = config.repo(work.repo_name)
        repo_github = repo.github if repo is not None else None
        # #2639: trust_issue_closed_for(work.type) — _pipeline_heads(board)
        # can surface test-author/mock-author heads, whose issue_number is
        # the milestone's tracking issue (closed for most of its life while
        # slices are still authored against it), not this row's own
        # deliverable. Trusting a closed tracking epic here would suppress
        # a legitimate stall notification for a genuinely stuck slice.
        if repo_github and github_ops.work_is_terminal(
            repo_github,
            work.issue_number,
            work.branch,
            cache=terminal_cache,
            trust_issue_closed=trust_issue_closed_for(getattr(work, "type", None)),
        ):
            continue

        required_gates = work.required_gates or list(config.pipeline.default_gates)

        review = next(
            (
                a for a in all_assignments
                if a.review_of_assignment_id == work.assignment_id and a.type == "review"
            ),
            None,
        )

        # #1566: a review that just finished lands on status="finalizing"
        # (not "done") until `coord notify`'s own _try_parse_and_post_review
        # promotes it — i.e. THIS function is what closes that window. None
        # of the `review.status == "done"` checks below match "finalizing",
        # so a still-finalizing review falls through this whole if/elif
        # chain with `reason` left unset (no stall reported), which is
        # correct as long as the finalizing window stays short. That relies
        # on `coord notify` actually running again soon — nothing here
        # guards against `coord notify` itself never running (e.g. daemon
        # down), which would leave the row on "finalizing" forever without
        # ever tripping this stall detector.
        reason: str | None = None
        detail = ""

        if (
            review is not None
            and review.status == "done"
            and review.review_verdict == "request-changes"
        ):
            fix = next(
                (
                    a for a in all_assignments
                    if a.review_of_assignment_id == work.assignment_id
                    and a.type in FIX_DISPATCH_TYPES
                ),
                None,
            )
            if fix is None:
                reason = "review_request_changes_no_fix"
                detail = (
                    f"Review {review.assignment_id} completed with "
                    "request-changes and no fix worker was ever dispatched "
                    "for it."
                )
        elif (
            review is not None
            and review.status == "done"
            and review.review_verdict is None
        ):
            # #1582: a review that finalised `done` with NO verdict ever
            # captured. Checked BEFORE the `review is None or review.status
            # == "done"` catch-all below — that branch's merge-gate check
            # (`passes_merge_gates`) never fires for a `None` verdict (no
            # approval), so this row would otherwise fall all the way
            # through with `reason` left unset.
            reason = "review_done_no_verdict"
            # #2019: provider-aware, because the pre-#2019 single sentence
            # ("the session likely failed to start or exited before recording
            # one (#812)") was demonstrably false for the headless case and
            # pointed at a CLOSED issue about interactive reviews. See this
            # function's docstring, arm 2.
            if review.provider_name == "claude-pty":
                detail = (
                    f"Review {review.assignment_id} finalised as done but no "
                    "verdict was ever captured — an interactive review that "
                    "failed to start, or exited before `coord report-result` "
                    "ran (#812)."
                )
            else:
                detail = (
                    f"Review {review.assignment_id} finalised as done but no "
                    "verdict was ever captured. This is a HEADLESS review "
                    "that ran to completion (a session that died lands "
                    "status='failed', not 'done'), so the reviewer's "
                    "REVIEW_VERDICT header was omitted or unparsed — the "
                    "END_REVIEW-without-verdict class (#1956), not a "
                    "never-started session. The verdict is very likely "
                    "already in the transcript; relay it rather than "
                    "re-dispatching: coord report-result --assignment "
                    f"{review.assignment_id} --status done --verdict "
                    "<approve|request-changes> --verdict-source recovered "
                    "--verdict-reason '...' --body-file <extracted-review.md>"
                )
        elif (
            review is None
            and "review" in required_gates
            and work.provider_name != "claude-pty"
            and work.test_state in ("passed", "skipped")
        ):
            reason = "done_no_review"
            detail = (
                f"Work is done with test_state={work.test_state!r} but no "
                "review assignment was ever dispatched for it."
            )
        elif review is not None and review.status == "failed":
            # #1584: the review worker died (transient API error, network
            # drop, ...) before producing a verdict. Checked before the
            # `review is None or review.status == "done"` catch-all below so
            # a failed review is never mistaken for "no review dispatched"
            # or "review approved" — neither of which is true here.
            #
            # ...UNLESS it was killed by the account's usage limit. That is
            # an account-wide exhausted budget, not a per-review defect:
            # `AgentServer._reap` lands a usage-limit kill on FAILED exactly
            # like an api_error kill, so without this guard the sweep would
            # spend this work row's ONE auto-recovery action (the
            # `_stalled_notified_key` ledger is one-shot per work row) on a
            # `dispatch_review` that is guaranteed to die the same way until
            # the reset — the precise anti-pattern `reconcile.py`'s
            # `auto_reassign` block was hardened against in #1461, and the
            # one `coord/drive.py`'s `_decide_review` already guards with
            # this same predicate. Skipped at CLASSIFICATION rather than
            # declined at dispatch so the row is never marked notified: a
            # later review attempt that fails for a *different* (genuinely
            # recoverable) reason can still be picked up by a future tick.
            if is_usage_limit_reason(review.failure_reason):
                continue
            reason = "review_failed_no_verdict"
            detail = (
                f"Review {review.assignment_id} failed "
                f"({review.failure_reason or 'no reason recorded'}) before "
                "producing a verdict, and no retry was dispatched."
            )
        elif review is None or review.status == "done":
            # Either the review gate doesn't apply, or a review already
            # completed without leaving a request-changes verdict blocking
            # it (approved, or advanced past advisory-only nits) — the only
            # remaining question is whether it made it into the merge queue,
            # and if it did, whether that entry is stuck.
            matching_entry = next(
                (m for m in merge_queue_items if m.assignment_id == work.assignment_id),
                None,
            )
            if matching_entry is None:
                # #2085: `work` is a raw board Assignment — no
                # `branch_head_sha`/`repo_github`/`target_branch` attribute,
                # so handing it straight to `passes_merge_gates` made the
                # #821 SHA-freshness check inside `has_approved_review`
                # permanently unconfirmable (fails closed on every review
                # carrying a real `review_head_sha`, i.e. virtually every
                # modern approval). Build the same live-anchored synthetic
                # entry `coord.gates.build_gate_report` uses so a genuinely
                # fresh approval can still be confirmed via `github_ops`
                # (already imported at module level). Falls back to the raw
                # `work` row (still gh_ops-backed, just missing target_branch/
                # repo_github) when the repo isn't configured — the gate
                # then fails closed exactly as before, never open.
                gate_entry = work
                if repo is not None and repo_github:
                    from coord.branch_model import (  # noqa: PLC0415
                        resolve_base_branch_for_issue_number,
                    )
                    target_branch = resolve_base_branch_for_issue_number(
                        repo, repo_github, work.issue_number,
                    )
                    gate_entry = live_gate_entry(
                        work, repo_github, target_branch, github_ops
                    )
                if passes_merge_gates(gate_entry, config, board, gh_ops=github_ops):
                    reason = "approved_not_queued"
                    detail = (
                        "Work passes every merge gate (review + test) but has "
                        "no merge-queue entry."
                    )
            elif (
                matching_entry.state == CONFLICT
                and classify_conflict(matching_entry.error) == "rebaseable"
                and not has_prior_conflict_fix(
                    board,
                    matching_entry.assignment_id,
                    current_error=matching_entry.error,
                )
            ):
                # #1478: a rebaseable CONFLICT with no active/failed
                # conflict-fix attempt — the #1474 classify-and-dispatch step
                # never got (or never got a second) chance at this entry.
                reason = "merge_conflict_unresolved"
                detail = (
                    f"Merge queue entry for branch {matching_entry.branch!r} is "
                    f"stuck in CONFLICT ({matching_entry.error or 'no error recorded'}) "
                    "with no active or previously-failed conflict-fix attempt."
                )

        if reason is None:
            continue

        results.append((
            StalledDetection(
                assignment_id=work.assignment_id,
                machine_name=work.machine_name,
                repo_name=work.repo_name,
                issue_number=work.issue_number,
                reason=reason,
                detail=detail,
            ),
            work,
        ))

    return results


def post_stalled_pipeline(detection: StalledDetection, config: Config) -> None:
    """Post a stalled-pipeline comment to GitHub and mark notified (#1441)."""
    repo = config.repo(detection.repo_name)
    repo_github = repo.github if repo is not None else None
    if not repo_github:
        return
    body = format_stalled_pipeline(
        assignment_id=detection.assignment_id,
        machine_name=detection.machine_name,
        repo_name=detection.repo_name,
        issue_number=detection.issue_number,
        reason=detection.reason,
        detail=detection.detail,
    )
    github_ops.post_issue_comment(repo_github, detection.issue_number, body)
    mark_notified(_stalled_notified_key(detection.assignment_id), EVENT_STALLED)


# ── #1478: dispatch arm ──────────────────────────────────────────────────────


@dataclass
class StalledDispatchAction:
    """The outcome of :func:`dispatch_stalled_pipeline_action` for one
    :class:`StalledDetection`."""

    kind: str
    """One of:
    - ``"fix_dispatch_attempted"`` — re-ran the review-completion transition
      (:func:`coord.auto_loop.process_review_completion`) for
      ``review_request_changes_no_fix`` (or, #1582, for a
      ``review_done_no_verdict`` whose verdict was just recovered from the
      transcript) and it dispatched a fix worker; see *detail* for what it
      did.
    - ``"review_transition_applied"`` — re-ran
      :func:`coord.auto_loop.process_review_completion` for
      ``review_request_changes_no_fix`` (or a transcript-recovered
      ``review_done_no_verdict``, #1582) and it resolved as ``approved``,
      ``approved_with_nits`` (the #476 advisory-only gate), or
      ``terminal_skip`` — no fix worker was dispatched, but the call still
      mutated *board* in place (``review.review_verdict``,
      ``work.review_state = "done"``, a merge-queue ``refresh_entry_assignment``)
      per that function's own "the caller is responsible for persisting the
      board after this returns" contract. Must be persisted exactly like a
      real dispatch even though no agent was launched.
    - ``"review_verdict_recovered"`` — ``review_done_no_verdict``: a verdict
      was recovered from the reviewing session's own transcript (#617's
      ``_review_findings_from_transcript``, the same recovery
      ``coord diagnose --stage review`` runs) and durably persisted, but
      ``process_review_completion`` made no further board mutation from it
      (e.g. ``pipeline.auto_loop`` is off). See *detail* for the recovered
      verdict.
    - ``"review_reset_redispatched"`` — ``review_done_no_verdict``: nothing
      was recoverable from the transcript, so the review stage was reset
      (the review rows deleted, ``work.review_state`` cleared — #1180's
      ``_reset_review_stage``, branch/commits always kept) and a fresh
      review dispatched for the same work.
    - ``"review_dispatched"``       — a review was dispatched for
      ``done_no_review``.
    - ``"enqueued"``                — the work was enqueued for merge for
      ``approved_not_queued`` (including when a *different* row's
      ``enqueue_approved_work`` call already enqueued this one earlier in
      the same sweep tick — see the queue-membership check below).
    - ``"conflict_fix_dispatched"`` — a conflict-fix worker was dispatched
      for ``merge_conflict_unresolved``.
    - ``"no_action"``               — the reused dispatcher declined (no
      capable machine, already in flight, gate not actually satisfied,
      entry vanished from the board/queue between detection and dispatch).
    - ``"skipped_live_session"``    — a running/pending assignment already
      exists for this (repo, issue); never act underneath a live session
      (#602).
    - ``"skipped_human_required"``  — the conflict-fix retry cap was already
      hit; surfacing to a human, not auto-retrying.
    - ``"skipped_sealed_conflict"`` (#2537, narrowed by #2555) —
      ``merge_conflict_unresolved`` on a
      :data:`coord.models.SEALED_PATH_AUTHOR_TYPES` row whose conflict was
      confirmed (:func:`_conflict_confined_to_sealed_paths`) to be confined
      to the repo's sealed acceptance-oracle paths AND (#2555:
      :func:`coord.conflict_fix.sealed_conflict_could_touch_manifest`) NO
      file in that (whole-branch-diff) list is even a milestone
      ``manifest.yml`` — i.e. a manifest.yml conflict is provably
      impossible, so the sealed-aware conflict-fix branch is guaranteed to
      have nothing to resolve. Whenever a manifest.yml DOES appear in that
      list — whether alone or alongside other sealed files the branch also
      authored, the realistic common shape — this no longer skips here: it
      falls through to :func:`coord.conflict_fix.dispatch_conflict_fix`,
      whose sealed-author branch (#2555) attempts it and lets the worker's
      own additive-only restriction do the precise, per-file filtering at
      rebase time.
    - ``"disabled"``                — ``pipeline.auto_dispatch_stalled`` is
      off; detection/narration still happened, dispatch did not.
    """
    detail: str = ""


# Action kinds that represent a REAL dispatch OR a board mutation that must
# be persisted (mutate the board / merge queue / fire an agent request) —
# used to decide (a) whether the board needs writing back, (b) which GitHub
# comment to post, and (c) whether the audit row is business-tier (a real
# transition) or operational-tier (a no-op/skip, informational only).
#
# ``review_transition_applied`` belongs here even though it does not launch
# an agent: an approved/approved-with-nits/terminal-skip resolution from
# ``process_review_completion`` still flips ``work.review_state``/
# ``review.review_verdict`` in place, and losing that mutation while the
# one-shot ledger marks the row notified anyway is exactly the #1478 review
# bug this set exists to prevent.
_STALLED_DISPATCH_KINDS = frozenset({
    "fix_dispatch_attempted", "review_transition_applied", "review_dispatched",
    "enqueued", "conflict_fix_dispatched",
    # #1582
    "review_verdict_recovered", "review_reset_redispatched",
})

# process_review_completion (and the _dispatch_fix_for_review it may call)
# kinds that mutate `board` in place per its own documented contract, even
# when they don't dispatch a fix worker. `disabled`/`no_findings` return
# before any mutation; `no_work_found`/`max_iterations` return without
# touching `board` (only a GitHub notice for the latter).
_MUTATING_REVIEW_COMPLETION_KINDS = frozenset({
    "fix_dispatched", "approved", "approved_with_nits", "terminal_skip",
})


def _stalled_row_has_live_session(board: "Board", work: "Assignment") -> bool:
    """#602 guardrail: true when a running/pending assignment already exists
    for *work*'s (repo, issue) — e.g. an interactive ``--fix-of``/
    ``--review-of``/``--merge-of`` session a human is actively driving.
    :func:`dispatch_stalled_pipeline_action` must never act underneath one:
    racing an auto-dispatch against a live session can duplicate or clobber
    it. Broader than :func:`coord.claim.has_active_work_followup` (which
    only checks ``work``/``conflict-fix``) — any live assignment type
    (review, smoke, chat, ...) for the same issue counts here.
    """
    for a in board.active:
        if a.status not in ("running", "pending"):
            continue
        if a.repo_name == work.repo_name and a.issue_number == work.issue_number:
            return True
    return False


def _conflict_confined_to_sealed_paths(
    entry: "QueuedMerge", config: Config,
) -> list[str] | None:
    """#2537: best-effort check for whether *entry*'s merge conflict can
    ONLY be within the repo's sealed acceptance-oracle paths
    (:meth:`coord.config.AcceptanceConfig.sealed_paths`).

    Returns the entry branch's changed-file list when every file it touches
    (the three-dot compare of *entry.target_branch*...*entry.branch*, via
    :func:`coord.github_ops.get_compare_files` — the SAME GitHub-API-only,
    no-local-checkout seam #1720's dispatch-time overlap fence already uses)
    falls under a sealed path. GitHub's conflicting-file set is necessarily
    a SUBSET of this branch's own changed files — a file can only conflict
    if *this* branch touches it too — so confining the WHOLE branch diff to
    sealed paths is sufficient to guarantee the conflict is, without needing
    a local checkout or a `git merge-tree` call.

    Returns ``None`` — "can't confirm confinement, don't skip the ordinary
    dispatch" — when: the repo has no acceptance driver configured (nothing
    is sealed); the compare API call fails or reports no files; or anything
    is touched outside every sealed path. This is a pure availability
    signal, never a hard "not confined" claim — a caller that gets ``None``
    falls back to :func:`coord.conflict_fix.dispatch_conflict_fix` exactly
    as before, which is always safe: a genuinely sealed-confined conflict
    just makes that dispatch a no-op (the conflict-fix worker self-restricts
    per CLAUDE.md's own sealed-path rule), so the only cost of a missed
    detection here is one wasted worker session, never an incorrect action.
    """
    sealed = config.acceptance.sealed_paths(entry.repo_name)
    if not sealed:
        return None
    from coord import github_ops  # noqa: PLC0415
    from coord.review import _path_is_sealed  # noqa: PLC0415

    files = github_ops.get_compare_files(
        entry.repo_github, entry.target_branch, entry.branch,
    )
    if not files:
        return None
    if all(any(_path_is_sealed(f, s) for s in sealed) for f in files):
        return files
    return None


def dispatch_stalled_pipeline_action(
    detection: StalledDetection,
    work: "Assignment",
    board: "Board",
    config: Config,
    *,
    terminal_cache: dict | None = None,
) -> StalledDispatchAction:
    """#1478: act on a #1441 stalled-pipeline detection instead of only
    narrating it.

    Gated by ``config.pipeline.auto_dispatch_stalled`` (default ``False`` —
    detection/narration via :func:`post_stalled_pipeline` is unconditional;
    this is the opt-in action half) — EXCEPT for a ``review_request_changes_
    no_fix`` OR (#2537) ``merge_conflict_unresolved`` stall on a
    :data:`coord.models.SEALED_PATH_AUTHOR_TYPES` row (``test-author``/
    ``mock-author``), which dispatches regardless of the flag: no loop owns
    that row (#2302), so the flag would only leave it stalled forever with
    no alternative dispatcher to race. ``work`` rows under the same reasons
    keep the opt-in gate — ``coord drive`` owns those.
    Mutates *board* in place exactly like
    the auto-loop / review-dispatch helpers it delegates to — the caller is
    responsible for persisting it.

    Reuses the SAME dispatch machinery the original, on-time transition
    would have used for each reason, rather than re-deriving new logic:

    - ``review_request_changes_no_fix`` → re-locates the ``request-changes``
      review and re-runs :func:`coord.auto_loop.process_review_completion`
      on it — the exact function the auto-loop calls the instant a review
      transitions to done, complete with its iteration cap and terminal
      guard.
    - ``review_done_no_verdict`` (#1582) → :func:`coord.diagnose._recover_review`
      (the exact recovery ``coord diagnose --stage review`` runs: try the
      session transcript first). A recovered verdict is then run through
      :func:`coord.auto_loop.process_review_completion` like a normal
      transition; nothing recoverable falls through to
      :func:`coord.diagnose._reset_review_stage` (the exact reset
      ``coord diagnose --stage review --reset`` runs — keeps the branch,
      wipes the review rows + review_state) followed by a fresh
      :func:`coord.review.dispatch_review` call.
    - ``done_no_review`` → :func:`coord.review.dispatch_review`, the same
      call ``detect_transitions``/``dispatch_pending_reviews`` make on a
      fresh work completion.
    - ``approved_not_queued`` → :func:`coord.merge_queue.enqueue_approved_work`,
      the same bulk gate-checked enqueue the daemon passive tick already
      runs on every interval.
    - ``merge_conflict_unresolved`` → :func:`coord.conflict_fix.dispatch_conflict_fix`,
      the #1474 ``_dispatch_conflict_fixes`` path — UNLESS (#2537) *work* is a
      :data:`coord.models.SEALED_PATH_AUTHOR_TYPES` row AND the conflict can
      be confirmed (via :func:`_conflict_confined_to_sealed_paths`, a
      compare-API-only check) to be confined to the repo's sealed
      acceptance-oracle paths AND (#2555:
      :func:`coord.conflict_fix.sealed_conflict_could_touch_manifest`) that
      confined, whole-branch-diff file list contains no manifest.yml at
      all — a manifest.yml conflict is then provably impossible, and a
      conflict-fix worker is REQUIRED by CLAUDE.md's own sealed-path rule to
      self-restrict from editing any OTHER sealed file, so it is guaranteed
      to push nothing; that case returns ``"skipped_sealed_conflict"``
      instead of spending a worker session on a known no-op. When a
      manifest.yml IS present in that list (even alongside other sealed
      files the branch also authored — the realistic common shape), this
      falls through to the ordinary dispatch, whose sealed-author branch
      (#2555) resolves a genuine manifest-only conflict and self-refuses,
      exactly like any other conflict-fix failure, the instant the actual
      conflict reaches beyond that file.
    - ``review_failed_no_verdict`` (#1584) → :func:`coord.review.dispatch_review`
      again, the SAME call as ``done_no_review`` — the failed review left no
      verdict behind, so recovery is identical to "no review was ever
      dispatched": open a fresh one against the still-``done`` work row.

    Never re-entrant across ticks: the caller only reaches this after
    :func:`detect_stalled_pipeline` has already filtered out any row whose
    ``_stalled_notified_key`` is in the ``notified`` ledger, and the caller
    marks that key notified right after this returns (via
    :func:`post_stalled_pipeline` or :func:`post_stalled_pipeline_dispatch`)
    — so a given assignment_id gets exactly one dispatch attempt per stall,
    mirroring the one-shot comment (#1441's own guardrail, reused rather
    than re-derived per #1478's own request).
    """
    # #2302: `pipeline.auto_dispatch_stalled` exists to bound blast radius on
    # rows another loop already owns (`coord drive`'s request-changes → fix
    # arm for `work` rows, #1692) — a second dispatcher racing that loop
    # would duplicate or clobber its fix. A `request-changes` stall on a
    # SEALED_PATH_AUTHOR_TYPES row (`test-author`/`mock-author`, Gate A/JIT
    # acceptance slices) has no such owner: `coord drive` explicitly `_die()`s
    # on those, and `coord acceptance mock` dispatches Gate A standalone with
    # no drive run over it at all (see #2289). So this one reason+type
    # combination bypasses the flag — every other reason, and `work` rows
    # under this same reason, keep the opt-in gate unchanged.
    #
    # #2537: a `merge_conflict_unresolved` stall on the SAME row types is the
    # identical shape — `coord drive` has no conflict-fix arm for
    # test-author/mock-author rows either (it `_die()`s on them before ever
    # reaching a merge), and the passive tick only re-enqueues, never
    # dispatches a conflict-fix. Without this, the row bounces
    # PENDING → CONFLICT → parked forever with nobody attempting a fix
    # (observed live: coord-portal#132). So this reason joins the bypass for
    # these row types too — every other reason, and `work` rows under either
    # reason, keep the opt-in gate unchanged.
    from coord.models import SEALED_PATH_AUTHOR_TYPES  # noqa: PLC0415

    sealed_author_stall = (
        detection.reason in ("review_request_changes_no_fix", "merge_conflict_unresolved")
        and work.type in SEALED_PATH_AUTHOR_TYPES
    )
    if not config.pipeline.auto_dispatch_stalled and not sealed_author_stall:
        return StalledDispatchAction(
            kind="disabled", detail="pipeline.auto_dispatch_stalled is False",
        )

    if _stalled_row_has_live_session(board, work):
        return StalledDispatchAction(
            kind="skipped_live_session",
            detail=(
                f"a running/pending assignment already exists for "
                f"{work.repo_name}#{work.issue_number} — not acting "
                "underneath a live session (#602)"
            ),
        )

    if detection.reason == "review_request_changes_no_fix":
        from coord.auto_loop import process_review_completion  # noqa: PLC0415

        all_assignments = list(board.active) + list(board.completed)
        review = next(
            (
                a for a in all_assignments
                if a.review_of_assignment_id == work.assignment_id and a.type == "review"
            ),
            None,
        )
        if review is None:
            return StalledDispatchAction(
                kind="no_action", detail="review no longer found on board",
            )
        machine_host = next(
            (m.host for m in config.machines if m.name == review.machine_name), None,
        )
        actions = process_review_completion(
            review, board, config,
            machine_host=machine_host, terminal_cache=terminal_cache,
        )
        kind_set = {a.kind for a in actions}
        kinds = ", ".join(a.kind for a in actions) or "no_action"
        details = "; ".join(a.detail for a in actions if a.detail)
        detail_msg = f"process_review_completion → {kinds}" + (f" ({details})" if details else "")
        # #1478 review fix: `process_review_completion` mutates `board` in
        # place for several outcomes besides `fix_dispatched` — an
        # `approved`/`approved_with_nits`/`terminal_skip` resolution still
        # flips `review.review_verdict`/`work.review_state` and refreshes the
        # merge-queue entry (see that function's own "caller is responsible
        # for persisting the board" contract). Classifying those as
        # `no_action` silently dropped the mutation (the sweep's `board_dirty`
        # never got set) while the one-shot ledger still marked the row
        # notified — permanently losing the transition. Any kind in
        # `_MUTATING_REVIEW_COMPLETION_KINDS` must therefore map to a
        # `_STALLED_DISPATCH_KINDS` member so `_sweep_stalled_pipeline`
        # persists it.
        if "fix_dispatched" in kind_set:
            return StalledDispatchAction(kind="fix_dispatch_attempted", detail=detail_msg)
        if kind_set & _MUTATING_REVIEW_COMPLETION_KINDS:
            return StalledDispatchAction(kind="review_transition_applied", detail=detail_msg)
        return StalledDispatchAction(kind="no_action", detail=detail_msg)

    if detection.reason == "review_done_no_verdict":
        # #1582: a review finalised `done` with no verdict ever captured
        # (#812). Reuse the SAME two steps `coord diagnose --stage review
        # [--reset]` runs for this exact shape, rather than re-deriving new
        # recovery/reset logic — `_recover_review`/`_reset_review_stage` are
        # the private functions behind that command for this branch. Called
        # directly (not through the full `diagnose_stage` orchestration),
        # which skips that command's tmux session-state probe and
        # issue-wide phantom-row cleanup — the review here is already
        # terminal, so neither applies, and both would add real
        # subprocess/ssh cost to every notify sweep tick.
        from coord.diagnose import (  # noqa: PLC0415
            DiagnoseResult,
            _recover_review,
            _reset_review_stage,
        )

        all_assignments = list(board.active) + list(board.completed)
        review = next(
            (
                a for a in all_assignments
                if a.review_of_assignment_id == work.assignment_id and a.type == "review"
            ),
            None,
        )
        if review is None:
            return StalledDispatchAction(
                kind="no_action", detail="review no longer found on board",
            )

        diag = DiagnoseResult(
            repo_name=work.repo_name, issue_number=work.issue_number, stage="review",
        )
        # `state="unknown"` is safe: `_recover_review`'s live/dead-session
        # branches are only reached when `latest.status != "done"`, which
        # can't happen here (`detect_stalled_pipeline` only flags this
        # reason for a `status="done"` review).
        _recover_review(board, config, review, "unknown", diag, dry_run=False)

        if diag.recovered:
            # A verdict was recovered from the session transcript and
            # durably persisted (#617's `_review_findings_from_transcript` →
            # `issue_store.post_result`). Run it through the SAME auto-loop
            # chokepoint a live review completion would have used — mirrors
            # `review_request_changes_no_fix` just above — so a recovered
            # `request-changes` still gets its fix worker and a recovered
            # `approve` still advances the pipeline.
            from coord.auto_loop import process_review_completion  # noqa: PLC0415

            machine_host = next(
                (m.host for m in config.machines if m.name == review.machine_name), None,
            )
            actions = process_review_completion(
                review, board, config,
                machine_host=machine_host, terminal_cache=terminal_cache,
            )
            kind_set = {a.kind for a in actions}
            kinds = ", ".join(a.kind for a in actions) or "no_action"
            details = "; ".join(a.detail for a in actions if a.detail)
            detail_msg = (
                "recovered verdict from the session transcript → "
                f"process_review_completion → {kinds}" + (f" ({details})" if details else "")
            )
            if "fix_dispatched" in kind_set:
                return StalledDispatchAction(kind="fix_dispatch_attempted", detail=detail_msg)
            if kind_set & _MUTATING_REVIEW_COMPLETION_KINDS:
                return StalledDispatchAction(kind="review_transition_applied", detail=detail_msg)
            return StalledDispatchAction(kind="review_verdict_recovered", detail=detail_msg)

        if not diag.needs_reset:
            return StalledDispatchAction(
                kind="no_action", detail="; ".join(diag.findings) or "nothing to do",
            )

        # Nothing recoverable — reset the review stage (delete the review
        # rows, clear review_state — #1180's `_reset_review_stage`, KEEPS
        # the branch/commits) and re-dispatch a fresh review.
        reset_res = DiagnoseResult(
            repo_name=work.repo_name, issue_number=work.issue_number, stage="review",
        )
        _reset_review_stage(
            config, work.repo_name, work.issue_number, reset_res,
            dry_run=False, assignment_id=work.assignment_id,
        )
        if not reset_res.reset_performed:
            return StalledDispatchAction(
                kind="no_action",
                detail="reset did not complete: " + "; ".join(reset_res.findings),
            )

        # `_reset_review_stage` writes the canonical DB directly (the same
        # seam `coord diagnose --reset` uses — see commands/status.py's
        # "NOTE: deliberately NO save_board" comment for why) WITHOUT
        # touching `board`. Mirror the same two writes on `board` in place
        # so a later `write_board` upsert of the now-stale `review`/`work`
        # objects doesn't resurrect the just-deleted review row or clobber
        # the just-cleared review_state back to its wedged value.
        board.active[:] = [
            a for a in board.active
            if not (a.type == "review" and a.review_of_assignment_id == work.assignment_id)
        ]
        board.completed[:] = [
            a for a in board.completed
            if not (a.type == "review" and a.review_of_assignment_id == work.assignment_id)
        ]
        work.review_state = "pending"
        work.review_verdict = None
        work.review_posted_at = None

        from coord.review import dispatch_review  # noqa: PLC0415

        new_review = dispatch_review(work, board, config, terminal_cache=terminal_cache)
        if new_review is None:
            return StalledDispatchAction(
                kind="no_action",
                detail=(
                    "review stage reset (no verdict recoverable) but "
                    "re-dispatch declined (no machine / already in flight / gate)"
                ),
            )
        return StalledDispatchAction(
            kind="review_reset_redispatched",
            detail=(
                "no verdict recoverable from transcript — reset the review "
                f"stage and re-dispatched as {new_review.assignment_id} to "
                f"{new_review.machine_name}"
            ),
        )

    if detection.reason == "done_no_review":
        from coord.review import dispatch_review  # noqa: PLC0415

        review = dispatch_review(work, board, config, terminal_cache=terminal_cache)
        if review is None:
            return StalledDispatchAction(
                kind="no_action",
                detail="dispatch_review declined (no machine / already in flight / gate)",
            )
        return StalledDispatchAction(
            kind="review_dispatched",
            detail=f"review {review.assignment_id} dispatched to {review.machine_name}",
        )

    if detection.reason == "review_failed_no_verdict":
        # #1584: the previous review died with no verdict — recovery is
        # identical to `done_no_review` above: `work` itself is still
        # `status="done"` (only the review it spawned failed), so a fresh
        # `dispatch_review` call is a normal, ungated re-dispatch. Reusing
        # the same call (rather than e.g. `coord retry` against the dead
        # review row) also picks up any board state that changed since —
        # same reasoning `done_no_review` already relies on.
        from coord.review import dispatch_review  # noqa: PLC0415

        # Belt-and-braces against the usage-limit kill (#1461/#1584):
        # `detect_stalled_pipeline` already skips those rows at
        # classification, but this function is public and is also reachable
        # with a caller-built detection, or after a race in which the
        # usage-limit `failure_reason` was stamped onto the review row
        # between detection and dispatch. Re-dispatching into an
        # account-wide exhausted budget only produces another corpse, so
        # decline — mirroring `_decide_review`'s WAIT in `coord/drive.py`.
        all_assignments = list(board.active) + list(board.completed)
        dead_review = next(
            (
                a for a in all_assignments
                if a.review_of_assignment_id == work.assignment_id
                and a.type == "review"
                and a.status == "failed"
            ),
            None,
        )
        if dead_review is not None and is_usage_limit_reason(dead_review.failure_reason):
            return StalledDispatchAction(
                kind="no_action",
                detail=(
                    f"review {dead_review.assignment_id} was killed by the "
                    f"usage limit ({dead_review.failure_reason}) — waiting "
                    "for the reset instead of re-dispatching"
                ),
            )

        review = dispatch_review(work, board, config, terminal_cache=terminal_cache)
        if review is None:
            return StalledDispatchAction(
                kind="no_action",
                detail="dispatch_review declined (no machine / already in flight / gate)",
            )
        return StalledDispatchAction(
            kind="review_dispatched",
            detail=f"review {review.assignment_id} dispatched to {review.machine_name}",
        )

    if detection.reason == "approved_not_queued":
        from coord.merge_queue import enqueue_approved_work, load_queue  # noqa: PLC0415

        changed = enqueue_approved_work(config, board)
        if work.assignment_id in changed:
            return StalledDispatchAction(
                kind="enqueued", detail=f"{work.assignment_id} enqueued for merge",
            )
        # #1478 review non-blocking finding: `enqueue_approved_work` bulk-
        # enqueues EVERY eligible row on `board.completed`, not just this one.
        # If an earlier row in the same sweep tick already triggered the
        # enqueue for this assignment, this call's `changed` list comes back
        # without it (nothing new to do) even though it genuinely is queued —
        # checking `changed` alone would misreport a real outcome as
        # `no_action`. Check queue membership directly instead of relying
        # solely on `changed`.
        if any(m.assignment_id == work.assignment_id for m in load_queue()):
            return StalledDispatchAction(
                kind="enqueued",
                detail=(
                    f"{work.assignment_id} already enqueued for merge (queued "
                    "earlier in this sweep tick)"
                ),
            )
        return StalledDispatchAction(
            kind="no_action",
            detail="enqueue_approved_work made no change for this assignment",
        )

    if detection.reason == "merge_conflict_unresolved":
        from coord.conflict_fix import (  # noqa: PLC0415
            dispatch_conflict_fix,
            has_prior_conflict_fix,
            sealed_conflict_could_touch_manifest,
        )
        from coord.merge_queue import load_queue  # noqa: PLC0415

        entry = next(
            (m for m in load_queue() if m.assignment_id == work.assignment_id), None,
        )
        if entry is None:
            return StalledDispatchAction(
                kind="no_action", detail="merge queue entry no longer found",
            )
        if has_prior_conflict_fix(
            board, entry.assignment_id, current_error=entry.error,
        ):
            return StalledDispatchAction(
                kind="skipped_human_required",
                detail="conflict-fix already active or its retry cap was already hit",
            )
        # #2537/#2555: dispatch_conflict_fix used to be guaranteed to no-op on
        # a conflict confined to the repo's sealed acceptance-oracle paths —
        # an ordinary conflict-fix worker is required by CLAUDE.md's own
        # sealed-path rule to self-restrict from editing them (the additive
        # carve-out applied only to test-author/mock-author dispatches, never
        # conflict-fix) — so it pushed nothing and the entry just re-stalled
        # next tick (confirmed live on coord-portal#132/#135). #2555 gave
        # `dispatch_conflict_fix` a sealed-aware branch (keyed off
        # `entry.assignment_type`, the same field checked below) that CAN
        # resolve the one shape that guarantee doesn't hold for: a conflict
        # confined to a milestone's `manifest.yml`. Pre-#2543 that was the
        # file every slice under that milestone additively appended its own
        # block to; #2543 moved that per-issue traffic into
        # `manifest.d/<issue>.yml` fragments instead (a legacy manifest.yml
        # can still carry it too, and always can conflict with itself on a
        # same-issue retry), so `sealed_conflict_could_touch_manifest`
        # (below) checks both shapes via `conflict_fix._is_sealed_manifest_path`.
        #
        # `sealed_files` here is `entry`'s WHOLE branch diff (the three-dot
        # compare), not the actual conflicting subset — GitHub's compare API
        # cannot report "which files conflict", only "which files differ".
        # A real test-author/mock-author slice's own diff almost always ALSO
        # contains the spec/test file(s) it authored alongside its
        # `manifest.yml` edit, so gating on "every file in this superset is
        # a manifest.yml" (`sealed_conflict_is_manifest_only`) rejected the
        # common, textbook-compliant case outright (#2555 review finding).
        # Gate on "a manifest.yml appears somewhere in the superset" instead
        # (`sealed_conflict_could_touch_manifest`) — still a sound negative
        # filter (a file can only conflict if this branch touches it too, so
        # NO manifest.yml anywhere in the diff means a manifest.yml conflict
        # is impossible and dispatch would be a guaranteed no-op) — and let
        # the worker's own additive-only restriction do the PRECISE
        # filtering at rebase time, refusing via `SEALED_SCOPE_STUCK_MARKER`
        # the instant the actual conflict reaches beyond that one file.
        if work.type in SEALED_PATH_AUTHOR_TYPES:
            sealed_files = _conflict_confined_to_sealed_paths(entry, config)
            if sealed_files is not None:
                if not sealed_conflict_could_touch_manifest(sealed_files):
                    return StalledDispatchAction(
                        kind="skipped_sealed_conflict",
                        detail=(
                            "conflict is confined to sealed acceptance paths ("
                            + ", ".join(sealed_files)
                            + ") and none of them is a manifest.yml — the "
                            "sealed conflict-fix branch (#2555) is only "
                            "authorized to resolve a manifest.yml conflict, "
                            "so a worker would push nothing here; needs a "
                            "human"
                        ),
                    )
        fix = dispatch_conflict_fix(entry, board, config, prefer_machine=work.machine_name)
        if fix is None:
            return StalledDispatchAction(
                kind="no_action",
                detail="dispatch_conflict_fix declined (no machine / no repo_path)",
            )
        return StalledDispatchAction(
            kind="conflict_fix_dispatched",
            detail=f"conflict-fix {fix.assignment_id} dispatched to {fix.machine_name}",
        )

    return StalledDispatchAction(
        kind="no_action", detail=f"no dispatch arm for reason={detection.reason!r}",
    )


def post_stalled_pipeline_dispatch(
    detection: StalledDetection, action: StalledDispatchAction, config: Config,
) -> None:
    """Post the #1478 auto-dispatch outcome comment and mark notified.

    Posted INSTEAD OF :func:`post_stalled_pipeline` when
    :func:`dispatch_stalled_pipeline_action` actually dispatched something
    for this row (see that function's *kind* values) — the two write to the
    same GitHub thread, so posting both would leave a directly
    contradictory "nothing was dispatched automatically" comment sitting
    right above this one.
    """
    repo = config.repo(detection.repo_name)
    repo_github = repo.github if repo is not None else None
    if not repo_github:
        return
    body = format_stalled_pipeline_dispatch(
        assignment_id=detection.assignment_id,
        repo_name=detection.repo_name,
        issue_number=detection.issue_number,
        reason=detection.reason,
        action_kind=action.kind,
        action_detail=action.detail,
    )
    github_ops.post_issue_comment(repo_github, detection.issue_number, body)
    mark_notified(_stalled_notified_key(detection.assignment_id), EVENT_STALLED)


def _agent_status(host: str, port: int = AGENT_PORT, timeout: float = 5.0) -> dict | None:
    try:
        resp = httpx.get(f"http://{host}:{port}/status", timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, httpx.TimeoutException):
        return None


def detect_transitions(config: Config) -> list[tuple[Transition, dict, dict]]:
    """Return (transition, dispatch_record, agent_assignment) for each
    assignment whose terminal state has not yet been notified.

    Splitting detection from posting makes the loop testable without
    mocking GitHub.
    """
    dispatched = load_dispatched()
    if not dispatched:
        return []
    notified = load_notified()
    by_id = {r["assignment_id"]: r for r in dispatched}

    # Collect machine hostnames we care about
    machines_by_name = {m.name: m for m in config.machines}
    needed = {r["machine_name"] for r in dispatched if r["assignment_id"] not in notified}

    transitions: list[tuple[Transition, dict, dict]] = []
    for machine_name in needed:
        machine = machines_by_name.get(machine_name)
        if machine is None:
            continue
        status = _agent_status(machine.host)
        if status is None:
            continue
        for entry in status.get("completed", []):
            aid = entry.get("id")
            record = by_id.get(aid)
            if record is None or aid in notified:
                continue
            entry_status = entry.get("status")
            # Cancelled-on-agent for an assignment the DB already marks done
            # is cleanup noise (e.g. operator ran POST /cancel to unstick a
            # hung reap). Don't post a false failure for it.
            db_status = (record.get("status") or "").lower()
            if entry_status == "cancelled" and db_status == "done":
                continue
            if entry_status == "done":
                event = EVENT_COMPLETION
            elif entry_status in ("failed", "cancelled"):
                event = EVENT_FAILURE
            elif entry_status == "advisory":
                # #448: advisory (0-commit clean exit) — post a distinctive
                # GitHub comment so operators who rely on GitHub (not just
                # coord status) know the worker finished with no code change
                # and that human review is needed.
                event = EVENT_ADVISORY
            elif entry_status == "refused_policy":
                # #2234: the worker exited cleanly, pushed 0 commits, and its
                # own final message cited a standing repo-rule prohibition
                # (`coord.agent.REFUSED_POLICY`). Modeled on the ADVISORY arm
                # above — same "post so GitHub-only readers see it" reasoning
                # — but a distinct event so the comment doesn't ask for human
                # review of an undecided outcome; the worker already found
                # the answer.
                event = EVENT_REFUSED_POLICY
            else:
                continue
            transitions.append(
                (
                    Transition(
                        assignment_id=aid,
                        machine_name=record["machine_name"],
                        repo_name=record["repo_name"],
                        issue_number=record["issue_number"],
                        event=event,
                        exit_code=entry.get("exit_code"),
                    ),
                    record,
                    entry,
                )
            )
    return transitions


def detect_stuck(config: Config) -> list[tuple[StuckDetection, dict]]:
    """Scan active worker logs for STUCK signals.

    Returns (StuckDetection, dispatch_record) for each stuck worker that
    hasn't already been notified as stuck.
    """
    dispatched = load_dispatched()
    if not dispatched:
        return []
    notified = load_notified()
    by_id = {r["assignment_id"]: r for r in dispatched}

    machines_by_name = {m.name: m for m in config.machines}

    # Only look at assignments that haven't been notified at all (still active)
    # and haven't already been notified as stuck.
    active_records = [
        r for r in dispatched
        if r["assignment_id"] not in notified
        and _stuck_notified_key(r["assignment_id"]) not in notified
    ]
    if not active_records:
        return []

    # Group by machine
    by_machine: dict[str, list[dict]] = {}
    for r in active_records:
        by_machine.setdefault(r["machine_name"], []).append(r)

    results: list[tuple[StuckDetection, dict]] = []
    for machine_name, records in by_machine.items():
        machine = machines_by_name.get(machine_name)
        if machine is None:
            continue
        status = _agent_status(machine.host)
        if status is None:
            continue

        # Build lookup of active entries by id
        active_by_id: dict[str, dict] = {}
        for entry in status.get("active", []):
            eid = entry.get("id")
            if eid:
                active_by_id[eid] = entry

        for record in records:
            aid = record["assignment_id"]
            entry = active_by_id.get(aid)
            if entry is None:
                continue

            stuck_message: str | None = None
            log_path: str | None = None

            # Check progress data from agent status
            progress = entry.get("progress")
            if progress and progress.get("stuck"):
                stuck_message = progress["stuck"]
                log_path = entry.get("log_path")

            # Also try parsing the log file directly
            entry_log = entry.get("log_path")
            if entry_log and not stuck_message:
                try:
                    # #1710: thread the dispatch record's resolved provider
                    # name through so a non-claude worker's log parses via
                    # its own provider rather than always assuming claude.
                    parsed = parse_progress(
                        entry_log, provider_name=record.get("provider_name"),
                    )
                    if parsed.stuck:
                        stuck_message = parsed.stuck
                        log_path = entry_log
                except Exception:  # noqa: BLE001
                    pass

            if stuck_message:
                results.append(
                    (
                        StuckDetection(
                            assignment_id=aid,
                            machine_name=record["machine_name"],
                            repo_name=record["repo_name"],
                            issue_number=record["issue_number"],
                            stuck_message=stuck_message,
                            log_path=log_path,
                        ),
                        record,
                    )
                )

    return results


def post_stuck(detection: StuckDetection, record: dict) -> None:
    """Post a stuck comment to GitHub and mark notified."""
    body = format_stuck(
        assignment_id=detection.assignment_id,
        machine_name=detection.machine_name,
        repo_name=detection.repo_name,
        issue_number=detection.issue_number,
        stuck_message=detection.stuck_message,
    )
    github_ops.post_issue_comment(
        record["repo_github"], detection.issue_number, body
    )
    mark_notified(_stuck_notified_key(detection.assignment_id), EVENT_STUCK)


def _capture_completion_summary(transition: Transition, entry: dict) -> None:
    """#874: parse the worker's ### Summary block and persist it on the row.

    Tries the local log first, then falls back to the agent's /logs/<id>
    endpoint for remote-agent assignments.  Silent on failure — a worker
    that emits no summary leaves the field NULL without error.
    """
    from coord.progress import (  # noqa: PLC0415
        parse_completion_summary_from_agent,
        parse_completion_summary_from_log,
    )
    from coord.state import update_assignment_completion_summary  # noqa: PLC0415

    prose: str | None = None
    log_path = entry.get("log_path")
    if log_path:
        try:
            prose = parse_completion_summary_from_log(Path(log_path))
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "_capture_completion_summary: failed to parse local log for %s: %s",
                transition.assignment_id, exc,
            )

    if prose is None:
        # Local log unavailable (remote-agent assignment) — fetch via the
        # agent's /logs/<id> endpoint.  Same fallback used by smoke tests.
        host = _agent_host(transition.machine_name)
        if host:
            try:
                prose = parse_completion_summary_from_agent(host, transition.assignment_id)
            except Exception as exc:  # noqa: BLE001
                log.debug(
                    "_capture_completion_summary: failed to fetch from agent %s for %s: %s",
                    host, transition.assignment_id, exc,
                )

    if prose is None:
        # No ### Summary block anywhere — leave completion_summary NULL.
        return
    try:
        update_assignment_completion_summary(transition.assignment_id, prose)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "_capture_completion_summary: failed to persist summary for %s: %s",
            transition.assignment_id, exc,
        )


def _capture_smoke_tests(transition: Transition, entry: dict) -> None:
    """#252: parse the worker's SMOKE_TESTS block and persist it on the row.

    Tries the local log first, then falls back to the agent's /logs/<id>
    endpoint for remote-agent assignments (mirrors the plan and review
    capture paths).  Silent on failure.
    """
    from coord.progress import (  # noqa: PLC0415
        parse_smoke_tests_from_agent,
        parse_smoke_tests_from_log,
    )
    from coord.state import update_assignment_smoke_tests  # noqa: PLC0415

    parsed: list[str] | None = None
    log_path = entry.get("log_path")
    if log_path:
        try:
            parsed = parse_smoke_tests_from_log(Path(log_path))
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "_capture_smoke_tests: failed to parse local log for %s: %s",
                transition.assignment_id, exc,
            )

    if parsed is None:
        # Local log unavailable (remote-agent assignment) — fetch via the
        # agent's /logs/<id> endpoint.  Same fallback the plan and review
        # paths use.
        host = _agent_host(transition.machine_name)
        if host:
            try:
                parsed = parse_smoke_tests_from_agent(host, transition.assignment_id)
            except Exception as exc:  # noqa: BLE001
                log.debug(
                    "_capture_smoke_tests: failed to fetch from agent %s for %s: %s",
                    host, transition.assignment_id, exc,
                )

    if parsed is None:
        # No SMOKE_TESTS block anywhere — leave smoke_tests NULL so the
        # TUI shows the graceful-degradation placeholder.
        return
    try:
        update_assignment_smoke_tests(transition.assignment_id, parsed)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "_capture_smoke_tests: failed to persist list for %s: %s",
            transition.assignment_id, exc,
        )


def _smoke_worker_verdict(
    transition: Transition, entry: dict,
) -> SmokeVerdict | None:
    """#2244: the smoke worker's own `SMOKE: pass|fail|baseline-red` verdict
    line, or ``None`` if it never printed one.

    Same local-log-first, agent-endpoint-fallback discipline as
    :func:`_capture_smoke_tests` — read-only (nothing persisted here; the
    caller decides what the verdict means), and best-effort: any lookup
    failure is logged and treated as "no verdict line found", which routes the
    caller to its fail-CLOSED default rather than a fabricated pass.

    This is the PRIMARY verdict channel for a headless Test stage. The session
    exit code cannot be one: `claude -p` exits 0 whenever the session ends
    normally, no matter what the suite did (an `exit 1` inside a Bash tool
    call ends that tool call, not the session) — see #2244.
    """
    from coord.progress import (  # noqa: PLC0415
        parse_smoke_verdict_from_agent,
        parse_smoke_verdict_from_log,
    )

    verdict: SmokeVerdict | None = None
    log_path = entry.get("log_path")
    if log_path:
        try:
            verdict = parse_smoke_verdict_from_log(Path(log_path))
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "_smoke_worker_verdict: failed to parse local log for %s: %s",
                transition.assignment_id, exc,
            )

    if verdict is None:
        host = _agent_host(transition.machine_name)
        if host:
            try:
                verdict = parse_smoke_verdict_from_agent(
                    host, transition.assignment_id
                )
            except Exception as exc:  # noqa: BLE001
                log.debug(
                    "_smoke_worker_verdict: failed to fetch from agent "
                    "%s for %s: %s",
                    host, transition.assignment_id, exc,
                )

    return verdict


#: #2244: the marker `_record_smoke_verdict` leaves in the parent's
#: ``test_reason`` when a headless smoke produced no parseable verdict. Read
#: back on the NEXT such reap to decide between "park for one automatic
#: re-dispatch" and "block, an operator has to look" — see below.
#:
#: #2272: canonically defined in :mod:`coord.smoke` (imported at the top of
#: this module), next to `dispatch_smoke` — the function that has to carry the
#: tally across its own ``running`` stamp for this counter to survive at all.
#: Re-bound here because this is the name the reap path has always used.
NO_SMOKE_VERDICT_MARKER = _NO_SMOKE_VERDICT_MARKER


#: #2579: the ``test_state`` written when a #2464 confirmation REFUTES a pass
#: claim on a work row whose review has *already* rendered a terminal
#: ``"approve"`` verdict (the #2528 race — dispatch fast on the self-reported
#: pass, reconcile once the independent re-run lands). Deliberately distinct
#: from ``"failed"``: every automatic fix-dispatch door this repo has
#: (`coord/drive.py`'s ``_decide_test``, `coord/commands/plan_followup.py`'s
#: `fix` gate) keys off the literal string ``"failed"``, so writing anything
#: else there means a genuine post-approval refutation is never mistaken for
#: an ordinary Test-stage failure and silently bounced to a fix worker as if
#: nothing unusual had happened. It still fails the merge gate exactly like
#: ``"failed"`` does — `coord/merge_queue.py` only treats
#: ``test_state in ("passed", "skipped")`` as satisfied — so this can never
#: read as a clean pass either. `test_reason` carries the full story for
#: `coord log`/`coord status`/the GitHub comment; this constant is only the
#: board-column value.
#:
#: Visibility (#2579 review): a `test_state` value that no display site
#: recognizes is worse than "failed", not better — it falls through to
#: whatever default that site uses for "nothing has happened yet". So this
#: value is explicitly threaded through every "is this test verdict bad"
#: rendering site alongside "failed": `coord.stage_projection.
#: test_stage_status_for` (and its Rust mirror, `tui/src/app/pipeline.rs::
#: test_stage_status_for`, shared by the TUI board and the web dashboard)
#: map it to the same red FAILED badge as a plain failure, and `coord status`
#: (`coord/commands/status.py`) gives it its own tag distinct from both
#: "[✗ test FAILED — needs fix]" and the review-state tags it would
#: otherwise fall through to (`"[review done]"` being the actively
#: misleading one for this issue's own scenario).
TEST_STATE_CONTESTED = "contested"


def _run_pass_confirmation(transition: Transition, entry: dict):
    """#2464: independently re-run the repo's real build+test for this branch.

    Returns a :class:`coord.confirm_test.ConfirmationResult`, or ``None`` when
    the confirmation is switched off or could not even be attempted. ``None``
    and an *inconclusive* result mean the same thing to every caller — fall
    back to the worker's own claim — but they are distinguished here so the log
    line can say which happened.

    Every failure path returns ``None`` rather than raising. This runs inside
    the reap loop, and an exception here would abandon the whole transition
    (leaving the row unnotified and the parent's ``test_state`` at
    ``"running"`` forever, which is the #1598 stranding shape). A confirmation
    that cannot run must degrade to pre-#2464 behaviour, never break the reap.
    """
    from coord.confirm_test import (  # noqa: PLC0415
        RECORDABLE_DURATION_KINDS,
        confirm_branch,
        confirmation_enabled,
        confirmation_timeout,
        expected_confirmation_seconds,
        record_confirmation_duration,
        spend_confirmation_budget,
    )

    try:
        from coord.config import load as _load_config  # noqa: PLC0415

        config = _load_config()
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "smoke %s: could not load config to confirm the pass claim (%s) — "
            "falling back to the worker's own claim (#2464).",
            transition.assignment_id, exc,
        )
        return None

    if not confirmation_enabled(config):
        log.info(
            "smoke %s: Test-verdict confirmation is disabled — recording the "
            "worker's own claim unchecked (#2464).",
            transition.assignment_id,
        )
        return None

    # #2464-review: a confirmation runs synchronously in this pass, and this
    # pass holds `~/.coord/notify.lock` for the whole fleet. Draw the run's
    # ceiling from the pass-wide budget rather than the per-run one, so a board
    # with several completed smoke rows cannot make the lock hold — and the
    # `/notify` request the thin client is blocked on — scale with board
    # activity. Exhausted budget means UNCONFIRMED, which is pre-#2464
    # behaviour, and it is said out loud rather than truncating silently.
    #
    # #2975: also hand in whatever this repo's OWN last confirmation measured
    # (`expected_confirmation_seconds`) — a repo whose suite structurally
    # cannot finish inside the budget (quadraui's `cargo test --features
    # tui`) would otherwise spend the full ceiling relearning that identical
    # fact on every single PASS claim, serialising every other repo's Test/
    # Review dispatch behind it each time. Knowing the answer already lets
    # `confirmation_timeout` skip straight to UNCONFIRMED instead.
    expected = expected_confirmation_seconds(transition.repo_name)
    timeout = confirmation_timeout(expected)
    if timeout is None:
        if expected is not None:
            log.warning(
                "smoke %s: %s's last measured confirmation took about %.0fs, "
                "at or beyond what this pass has left to spend — skipping "
                "the run and recording the worker's own claim UNCONFIRMED "
                "rather than repeating a run already known not to finish "
                "(#2975).",
                transition.assignment_id, transition.repo_name, expected,
            )
        else:
            log.warning(
                "smoke %s: this notify pass has spent its confirmation budget "
                "(coord.confirm_test.CONFIRM_PASS_BUDGET_SECONDS) — recording the "
                "worker's own claim for %s UNCONFIRMED rather than holding "
                "notify.lock any longer (#2464).",
                transition.assignment_id, transition.repo_name,
            )
        return None

    branch = entry.get("branch")
    log.info(
        "smoke %s: independently re-running %s's build+test at origin/%s to "
        "confirm the pass claim, within %ss (#2464).",
        transition.assignment_id, transition.repo_name, branch, timeout,
    )
    started = time.monotonic()
    result: ConfirmationResult | None = None
    try:
        result = confirm_branch(
            transition.repo_name, branch, config, timeout=timeout,
        )
        return result
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "smoke %s: confirmation run raised (%s) — falling back to the "
            "worker's own claim (#2464).",
            transition.assignment_id, exc,
        )
        return None
    finally:
        # Charged in `finally` so a raising confirmation still costs its time.
        # It really did hold the drain (and `notify.lock`) for those seconds,
        # and a run that reliably blows up after ten minutes must not be able
        # to repeat that for every row on the board free of charge.
        elapsed = time.monotonic() - started
        spend_confirmation_budget(elapsed)
        # #2975: only remember the duration when the result is actually a
        # trustworthy measurement (see RECORDABLE_DURATION_KINDS) — an
        # exception, a disabled config load, or a KIND_SETUP/INFRA/SIGNAL
        # short-circuit tells us nothing about how long the real suite takes
        # and must never overwrite a previously-learned expectation.
        if result is not None and result.kind in RECORDABLE_DURATION_KINDS:
            record_confirmation_duration(transition.repo_name, elapsed)


def _confirmed_pass_verdict(
    transition: Transition, entry: dict, parent_id: str, *, claim_reason: str,
) -> tuple[str, str]:
    """#2464: the ``(test_state, test_reason)`` to record for a *claimed* pass.

    The Test stage's pass claim — whether it arrived as a ``SMOKE: pass``
    marker or as the worker calling ``coord test --passed`` on itself (#2217) —
    is a self-report by the thing being graded. This turns it into an
    observation by re-running the repo's own build/test command out-of-band and
    reading the real exit code (:mod:`coord.confirm_test`).

    Four outcomes, and note that only ONE of them is a downgrade:

    * **refuted** — a command ran to completion and returned nonzero. The claim
      was wrong; record ``failed``. This is the arm #2464 exists for.

      #2579: UNLESS the parent work row's own review already rendered a
      terminal ``"approve"`` verdict — `dispatch_pending_reviews` gates
      automatic review dispatch on `test_state`, not on this confirmation
      (#2528: serializing every review behind the out-of-band re-run would
      add its full latency, up to over an hour, to every review, not just
      the refuted ones), so a review can complete and approve before its own
      confirmation lands. Recording a plain ``failed`` in that case is
      indistinguishable from an ordinary Test-stage failure and silently
      chains a `coord fix` bounce onto a branch a human reviewer already
      signed off on. Record :data:`TEST_STATE_CONTESTED` instead — still
      fails the merge gate, but never matches the literal ``"failed"`` every
      automatic fix-dispatch door keys off. A row with no review verdict
      yet, or a non-terminal / ``request-changes`` verdict, is unaffected —
      this must not widen into a general suppression of refutations.
    * **baseline-red** — it failed identically on the merge-base, so the branch
      is not at fault. ``skipped``, matching #2170's existing convention: the
      merge gate treats it as satisfied and no fix round is burned.
    * **confirmed** — record ``passed``, now backed by a real run.
    * **inconclusive / unavailable** — record ``passed`` exactly as before
      #2464, with ``UNCONFIRMED`` in the reason so the row says plainly that
      nobody checked. See :mod:`coord.confirm_test` on why a missing toolchain,
      a missing checkout or a timeout must never read as a failing branch.

    #2563: whatever output the run captured — failing node ids, the
    assertion, tracebacks — is also persisted to *parent_id*'s
    ``test_output/<id>.txt`` (:func:`coord.confirm_test.write_confirmation_output`)
    before this returns, so the escalated fix worker's briefing
    (`coord/commands/plan_followup.py`, which already reads that file ahead of
    every other evidence source) quotes the real failure instead of the
    one-line *reason* alone. ``test_reason`` on the row stays exactly that
    one-line summary — #1337 deliberately keeps unbounded free text out of the
    board upsert, and this does not undo that; the file is the long form.
    """
    from coord.confirm_test import write_confirmation_output  # noqa: PLC0415

    result = _run_pass_confirmation(transition, entry)

    if result is None:
        return (
            "passed",
            f"{claim_reason} — UNCONFIRMED: no independent re-run was possible "
            "on this machine, so this verdict rests on the worker's own report "
            "(#2464).",
        )

    # Best-effort and unconditional on *kind*: REFUTED/BASELINE-RED/TIMEOUT/
    # INFRA/SIGNAL all carry a captured tail (`ConfirmationResult.output`) worth
    # keeping around; CONFIRMED and setup-stage inconclusives carry none, so
    # this is simply a no-op for them (`write_confirmation_output` checks
    # `.output` itself, and swallows `OSError`). Wrapped again here — this
    # runs inside the reap loop, and this whole module's standing rule is that
    # nothing in it may abandon a transition; a write failure must degrade to
    # "no file", never to a dropped verdict.
    try:
        write_confirmation_output(parent_id, result)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "smoke %s: could not persist confirmation output for parent %s "
            "(%s) — the fix briefing will fall back to the one-line reason "
            "(#2563).",
            transition.assignment_id, parent_id, exc,
        )

    if result.refuted:
        log.warning(
            "smoke %s: REFUTED the pass claim for %s — %s\n%s",
            transition.assignment_id, transition.repo_name, result.reason,
            result.output,
        )

        # #2579: check the PARENT's own review verdict before recording a
        # plain "failed" — a terminal "approve" here means the #2528 race
        # fired: review dispatch (gated on `test_state`, not on this
        # confirmation) already ran, completed, and approved while this
        # out-of-band re-run was still in flight. `record_work_review_verdict`
        # (coord/state.py) is the single place that stamps `review_verdict`
        # onto a work row, and only ever with the pipeline's own winning
        # terminal verdict — so a non-None value here is real, not a race on
        # this same write. A `None` or `"request-changes"` verdict is exactly
        # today's behaviour: keep it, don't widen this into a general
        # suppression of refutations.
        from coord.state import load_assignment_review_verdict  # noqa: PLC0415

        _review_state, _review_verdict = load_assignment_review_verdict(parent_id)
        if _review_verdict == "approve":
            log.warning(
                "smoke %s: parent %s's review already carries a terminal "
                "'approve' verdict — recording %r instead of 'failed' so this "
                "post-approval refutation is never mistaken for an ordinary "
                "Test-stage failure and silently bounced to a fix worker "
                "(#2579).",
                transition.assignment_id, parent_id, TEST_STATE_CONTESTED,
            )
            return (
                TEST_STATE_CONTESTED,
                f"CONTESTED (#2579): an independent re-run (#2464) REFUTED "
                f"this claimed pass ({claim_reason}) AFTER its review had "
                f"already approved it (review_state={_review_state!r}) — "
                f"{result.reason}. This is the #2528 dispatch-fast/reconcile-"
                "after-the-fact race, not an ordinary test failure: the "
                "approved review and the refuting re-run disagree, and that "
                "conflict needs a human, not an automatic fix dispatch. The "
                "merge gate stays shut (this is not 'passed'/'skipped'), but "
                "no fix round is auto-dispatched — after checking `coord log "
                f"{parent_id}` and the confirmation output, recover with "
                f"`coord fix --force --guidance <what's broken> {parent_id}` "
                "or re-dispatch the Test stage by hand.",
            )

        return (
            "failed",
            f"REFUTED by an independent re-run (#2464): {result.reason}. The "
            f"Test-stage worker claimed a pass ({claim_reason}), but re-running "
            "the repo's own command out-of-band disagreed — trust the run, not "
            "the report.",
        )

    if result.baseline_red:
        log.warning(
            "smoke %s: confirmation says the BASELINE is red for %s — %s",
            transition.assignment_id, transition.repo_name, result.reason,
        )
        return (
            "skipped",
            f"baseline-red (#2170), found by an independent re-run (#2464): "
            f"{result.reason}",
        )

    if result.confirmed:
        return (
            "passed",
            f"{claim_reason} — independently confirmed (#2464): {result.reason}",
        )

    log.warning(
        "smoke %s: confirmation was INCONCLUSIVE for %s (%s) — recording the "
        "worker's own claim (#2464).",
        transition.assignment_id, transition.repo_name, result.reason,
    )
    return (
        "passed",
        f"{claim_reason} — UNCONFIRMED: {result.reason}",
    )


def _record_smoke_verdict(
    transition: Transition, entry: dict, parent_id: str,
) -> None:
    """#2244: record the parent work row's Test verdict from a completed
    headless smoke run — from the worker's `SMOKE:` marker, failing CLOSED.

    Before this, the verdict was ``"passed"`` whenever the session exit code
    was 0. For a `claude -p` smoke worker that exit code is not a signal at
    all: `exit 1` inside a Bash tool call ends the tool call, the session
    still ends ``end_turn``, and `claude -p` exits 0. Assignment
    ``8de33c80fcd0`` ran the full suite, hit 5 real failures, printed
    ``SMOKE: fail`` — and was recorded ``test_state=passed``. CI then found
    the identical five failures and blocked the merge (#2230, and the same
    shape in #2091/#2182/#2143).

    Precedence, most authoritative first:

    1. **A terminal verdict already on the row.** #2217 made
       ``COORD_ASSIGNMENT_ID`` reachable from headless workers, so the smoke
       worker is now told to call ``coord test --passed|--fail <parent>``
       itself (`coord.smoke.build_smoke_briefing`). That write is
       authoritative and this must never clobber it — `dispatch_smoke` stamps
       ``"running"`` at dispatch, so anything terminal here came from the
       worker.
    2. **The worker's `SMOKE:` marker** — pass / fail / baseline-red (#2170).
    3. **A non-zero session exit with no marker** — still ``failed``. Only
       reachable for a non-`claude -p` smoke lane where the exit code IS the
       signal; it stays fail-closed there.
    4. **Nothing parseable at all** — record NO verdict. Never ``passed``:
       that fallback is the whole defect. The row is cleared to ``NULL`` so
       `dispatch_pending_smoke` re-dispatches a fresh Test stage (the #1605
       environmental-death path does exactly this), and if a SECOND smoke
       also comes back mute the row is parked ``"blocked"`` instead — the
       same value `dispatch_smoke` uses for an unroutable stage, which stops
       the auto-queue and waits for `coord diagnose --stage test --reset`
       rather than re-dispatching forever.
    """
    from coord.state import (  # noqa: PLC0415
        load_assignment_test_reason,
        load_assignment_test_state,
        record_test_verdict,
    )

    current_state = load_assignment_test_state(parent_id)
    if current_state in ("passed", "failed", "skipped"):
        if current_state == "passed":
            # #2464: a self-recorded `passed` is the STRONGEST form of the
            # defect, not an exception to it — the worker graded its own work
            # and wrote the grade straight to the row. #2217 called that write
            # authoritative, and it stays authoritative against everything
            # except an actual contradicting run: the only thing permitted to
            # overturn it here is the repo's own build/test command, executed
            # out-of-band, exiting nonzero. Skipping this branch would leave
            # the common case unguarded, since `build_smoke_briefing` tells
            # every smoke worker to self-record exactly this way.
            # #2464-review: this must record on EVERY outcome, not just a
            # downgrade. `_confirmed_pass_verdict` also distinguishes
            # confirmed-by-a-real-run from merely inconclusive, and that
            # distinction is the audit trail the module docstring promises
            # ("the row must say WHY"). Falling through to the generic
            # "already authoritative — leaving it untouched" log below when
            # `state == "passed"` would silently discard it: `test_reason`
            # would keep whatever text was there before this confirmation
            # ran, so a (potentially 20-minute) run that agreed with the
            # claim would leave no trace it ever happened — including on the
            # next reap of the same already-passed parent.
            state, reason = _confirmed_pass_verdict(
                transition, entry, parent_id,
                claim_reason="worker self-recorded via `coord test` (#2217)",
            )
            record_test_verdict(
                assignment_id=parent_id,
                test_state=state,
                test_reason=reason,
            )
            if state != "passed":
                log.warning(
                    "smoke %s: overturned parent %s's self-recorded "
                    "test_state='passed' to %r on independent evidence "
                    "(#2464).",
                    transition.assignment_id, parent_id, state,
                )
            else:
                log.info(
                    "smoke %s: parent %s's self-recorded test_state='passed' "
                    "— %s",
                    transition.assignment_id, parent_id, reason,
                )
            return
        log.info(
            "smoke %s: parent %s already carries an authoritative test_state="
            "%r (the worker recorded it via `coord test`, #2217) — leaving it "
            "untouched.",
            transition.assignment_id, parent_id, current_state,
        )
        return

    verdict = _smoke_worker_verdict(transition, entry)
    exit_code = transition.exit_code or 0

    if verdict is not None and verdict.kind == "baseline-red":
        # #2170: `skipped`, not `failed` — the merge gate treats a skipped
        # Test stage as satisfied, and neither `coord fix` nor `coord drive`
        # burns an attempt on breakage this branch did not cause.
        record_test_verdict(
            assignment_id=parent_id,
            test_state="skipped",
            test_reason=(
                f"baseline-red (#2170): {verdict.reason}"
                if verdict.reason
                else "baseline-red (#2170): every failure reproduces "
                "identically on the merge-base"
            ),
        )
        return

    if verdict is not None and verdict.kind == "fail":
        # #1384: no `smoke_test=` argument needed — the writer
        # (`state._record_test_verdict_local`) derives the legacy mirror from
        # `test_state`, so a headless smoke FAILURE lands as
        # test_state='failed' AND smoke_test='fail' and stays reachable from
        # `coord fix`.
        record_test_verdict(
            assignment_id=parent_id,
            test_state="failed",
            test_reason=(
                f"headless smoke: {verdict.reason}"
                if verdict.reason
                else "headless smoke reported SMOKE: fail"
            ),
        )
        return

    if verdict is not None and verdict.kind == "pass":
        # #2464: `SMOKE: pass` is a line the worker chose to print, not a
        # suite result. #2244 made the ACCIDENTAL version of a wrong pass less
        # likely by reading the marker instead of the meaningless `claude -p`
        # exit code, but the channel underneath is still self-report — nothing
        # stopped a worker printing it after a partial or backgrounded run it
        # never finished polling (#2272/#2301). Confirm it against a real run
        # before it becomes a merge-gate-satisfying verdict.
        state, reason = _confirmed_pass_verdict(
            transition, entry, parent_id,
            claim_reason="headless smoke reported SMOKE: pass",
        )
        record_test_verdict(
            assignment_id=parent_id,
            test_state=state,
            test_reason=reason,
        )
        return

    if exit_code != 0:
        record_test_verdict(
            assignment_id=parent_id,
            test_state="failed",
            test_reason="headless smoke",
        )
        return

    # No verdict line, clean session exit. NOT a pass.
    #
    # #2272: count the legs, don't just test for the marker's presence. The
    # tally lives at the FRONT of `test_reason` so a bounded `/board` preview
    # still carries it, and `dispatch_smoke` re-states it across the `running`
    # stamp that used to erase it. Together those two make this a real budget
    # rather than the never-firing one #2244 intended.
    previous_reason = load_assignment_test_reason(parent_id) or ""
    legs = mute_smoke_legs(previous_reason) + 1
    reason = (
        f"{mute_smoke_tally(legs)}: the Test-stage worker "
        f"{transition.assignment_id} ended without printing a verdict marker "
        "line, and a `claude -p` exit code says only that the session ended — "
        "it is not a suite result."
    )
    if legs >= MUTE_SMOKE_LEG_BUDGET:
        # The same park value `dispatch_smoke` uses for an unroutable stage
        # (#1672): every gate that wants "passed"/"skipped" keeps the merge
        # shut, `dispatch_pending_smoke` stops re-dispatching, and no fix
        # round is burned on a branch nothing has found fault with.
        #
        # #2272: the terminal reason NAMES the cause and the count. "N smoke
        # legs produced no verdict" is a statement an operator can act on; the
        # five-lap incident presented as "the branch is slow" precisely
        # because nothing on the row ever said how many legs had gone mute.
        record_test_verdict(
            assignment_id=parent_id,
            test_state=TEST_STATE_BLOCKED,
            test_reason=(
                f"{reason} {legs} smoke legs produced no verdict — the "
                f"Test-stage retry budget ({MUTE_SMOKE_LEG_BUDGET}) is "
                "exhausted, so the row is parked instead of re-dispatched "
                "(#2272). A mute leg is not a failing branch: check whether "
                "the smoke command is exceeding the worker's 600s Bash "
                "ceiling before you blame the diff. Recover with `coord "
                "diagnose <repo> <issue> --stage test --reset`, or record the "
                f"verdict by hand with `coord test --passed|--fail "
                f"{parent_id}`."
            ),
        )
        log.warning(
            "smoke %s: no SMOKE: verdict in the transcript — that is %d mute "
            "Test-stage leg(s) on parent %s and the retry budget (%d) is "
            "exhausted, so parking test_state=%r instead of re-dispatching "
            "(#2272). Check the worker's 600s Bash ceiling first.",
            transition.assignment_id, legs, parent_id, MUTE_SMOKE_LEG_BUDGET,
            TEST_STATE_BLOCKED,
        )
        return

    remaining = MUTE_SMOKE_LEG_BUDGET - legs
    record_test_verdict(
        assignment_id=parent_id,
        test_state=None,
        test_reason=(
            f"{reason} Cleared for a Test-stage re-dispatch; {remaining} of "
            f"{MUTE_SMOKE_LEG_BUDGET} legs left in the budget before the row "
            "parks instead (#2272)."
        ),
    )
    log.warning(
        "smoke %s: no SMOKE: verdict in the transcript — recording NO verdict "
        "for parent %s (mute leg %d of %d, cleared for re-dispatch), never "
        "'passed' (#2244).",
        transition.assignment_id, parent_id, legs, MUTE_SMOKE_LEG_BUDGET,
    )


def _capture_cost(transition: Transition, entry: dict, record: dict | None = None) -> None:
    """#208/#546: parse the worker's final cost+tokens and persist them.

    Preferred source is the local stream-json log (cheap, no network).
    Falls back to the agent's status entry, which carries ``cost_so_far``
    / ``total_cost_usd`` reported live by the worker.  Tokens are only
    available from the log (not from the agent status dict), so they are
    captured when the local log exists.  Either path is best-effort —
    failure is silent so it can't block the comment post.

    #1710: *record* (the dispatch record from ``load_dispatched()``) carries
    ``provider_name`` — threaded into :func:`coord.usage.parse_usage_from_log`
    so cost/token parsing uses the assignment's actual provider instead of
    always assuming claude. ``None`` (no record, or predates #324) falls back
    to the claude default, unchanged from before #1710.
    """
    from coord.state import update_assignment_cost, update_assignment_tokens  # noqa: PLC0415
    from coord.usage import parse_usage_from_log  # noqa: PLC0415

    cost: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    num_turns: int = 0
    provider_name = (record or {}).get("provider_name")

    log_path = entry.get("log_path")
    if log_path:
        try:
            parsed = parse_usage_from_log(Path(log_path), provider_name=provider_name)
            if parsed is not None:
                if parsed.total_cost_usd > 0:
                    cost = parsed.total_cost_usd
                # #546: also capture token counts from the same parse.
                input_tokens = parsed.input_tokens
                output_tokens = parsed.output_tokens
                cache_creation_tokens = parsed.cache_creation_tokens
                cache_read_tokens = parsed.cache_read_tokens
                # #2786: and the turn count, off the same `result` event.
                num_turns = parsed.num_turns
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "_capture_cost: failed to parse log for %s: %s",
                transition.assignment_id, exc,
            )

    if cost is None:
        # Fall back to the live value the agent had at reap time.
        remote_cost = entry.get("total_cost_usd") or entry.get("cost_so_far")
        if remote_cost is not None:
            try:
                cost = float(remote_cost)
            except (TypeError, ValueError):
                cost = None

    # #667: token fallback — when the local log was absent/unreadable the
    # token counts are still 0.  The agent now includes them in the /status
    # completed entry, so read them from there.
    if input_tokens + output_tokens + cache_creation_tokens + cache_read_tokens == 0:
        try:
            input_tokens = int(entry.get("input_tokens") or 0)
            output_tokens = int(entry.get("output_tokens") or 0)
            cache_creation_tokens = int(entry.get("cache_creation_tokens") or 0)
            cache_read_tokens = int(entry.get("cache_read_tokens") or 0)
            num_turns = int(entry.get("num_turns") or 0)
        except (TypeError, ValueError):
            pass

    if cost is not None and cost > 0:
        try:
            update_assignment_cost(transition.assignment_id, cost)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "_capture_cost: failed to persist cost for %s: %s",
                transition.assignment_id, exc,
            )

    # #546/#2786: persist token counts + turns (best-effort; silent on
    # missing columns).
    if input_tokens + output_tokens + cache_creation_tokens + cache_read_tokens > 0:
        try:
            update_assignment_tokens(
                transition.assignment_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_creation_tokens=cache_creation_tokens,
                cache_read_tokens=cache_read_tokens,
                num_turns=num_turns,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "_capture_cost: failed to persist tokens for %s: %s",
                transition.assignment_id, exc,
            )


def _persist_review_verdict(assignment_id: str, verdict: str) -> None:
    """Store the parsed reviewer verdict on the review assignment row.

    #253: consumed by ``coord.merge_queue.has_approved_review`` so the merge
    gate can refuse to merge work whose review hasn't approved.  Best-effort;
    a DB error is logged and swallowed (the merge gate falls back to "no
    approval found" which is the safe answer).
    """
    if verdict not in ("approve", "request-changes"):
        return
    try:
        from coord import sql  # noqa: PLC0415
        from coord.db import get_connection  # noqa: PLC0415

        conn = get_connection()
        with conn:
            sql.execute(
                conn,
                "UPDATE assignments SET review_verdict = ? WHERE assignment_id = ?",
                (verdict, assignment_id),
            )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "Failed to persist review_verdict for %s: %s", assignment_id, exc
        )


def _persist_review_findings(assignment_id: str, verdict: str, body: str) -> None:
    """#bounce: persist both verdict + findings body in one shot.

    Mirrors `_persist_review_verdict` (which we keep for callers that
    only have the verdict) but also caches the body so `coord bounce`
    can skip the slow HTTP log fetch.  Best-effort; a DB error is
    logged and swallowed.
    """
    if verdict not in ("approve", "request-changes"):
        return
    try:
        from coord.state import update_assignment_review_findings  # noqa: PLC0415

        update_assignment_review_findings(
            assignment_id, verdict=verdict, body=body,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "Failed to persist review_findings for %s: %s", assignment_id, exc
        )


def _fetch_raw_log_text_by_id(
    assignment_id: str, log_path: str | None, host: str | None,
) -> str | None:
    """Shared local-file-then-agent-fetch raw-text primitive.

    Tries *log_path* on the local filesystem first (cheap, no network); when
    that's absent or unreadable (the worker ran on a remote agent whose log
    isn't on this filesystem), falls back to fetching it via the agent's
    ``/logs/<id>`` endpoint at *host*. Returns ``None`` when neither source
    yields text — every caller here is best-effort by design.
    """
    if log_path:
        try:
            return Path(log_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    if host:
        try:
            resp = httpx.get(
                f"http://{host}:{AGENT_PORT}/logs/{assignment_id}",
                timeout=15.0,
            )
            resp.raise_for_status()
            return resp.text
        except (httpx.HTTPError, httpx.TimeoutException):
            return None
    return None


def _fetch_raw_log_text(transition: Transition, entry: dict) -> str | None:
    """Best-effort raw log text for #1956/#1348 diagnostics.

    Mirrors the local-file-then-agent-fetch fallback :func:`_try_parse_and_post_review`
    itself uses to PARSE the log, but returns the raw text instead — the
    diagnostic detectors (:func:`coord.review.detect_end_review_without_verdict`,
    :func:`coord.review.detect_unparsed_review_marker`) need the text the
    strict parser already rejected, not another parse attempt. Returns
    ``None`` on any I/O failure — diagnostics are best-effort by design and
    must never be the reason ``coord notify`` raises.
    """
    log_path = entry.get("log_path")
    host = _agent_host(transition.machine_name)
    return _fetch_raw_log_text_by_id(transition.assignment_id, log_path, host)


def _capture_cost_and_tokens_for_review(
    assignment_id: str,
    *,
    log_path: str | None,
    host: str | None,
    provider_name: str | None = None,
) -> bool:
    """#2476: best-effort cost/token capture for a review row, local-log-
    first with a remote-agent-fetch fallback.

    ``post_transition``'s ``_capture_cost`` is the ONLY place cost/tokens get
    captured for a review that completes through the direct
    ``detect_transitions`` path — but investigation of #2476 found the
    majority of review completions actually get their GitHub comment posted
    later, by :func:`post_orphaned_review_findings` (run_drain's step 5,
    which runs on every ~60s drain tick, not just as manual cleanup): that
    function does its own independent ``/status`` poll and local-then-remote
    log fallback to recover the VERDICT, but until now never captured
    cost/tokens at all. Once that row is marked ``notified``/
    ``review_posted_at``, nothing ever revisits it — so a review rescued by
    the orphaned-findings path was permanently stuck at ``cost_usd IS NULL``
    even though the exact same log :func:`post_orphaned_review_findings`
    just successfully parsed the verdict from also has a perfectly good
    ``total_cost_usd`` in it.

    Mirrors :func:`_capture_cost`'s log-parse-then-persist logic (same
    :func:`coord.usage.parse_usage_from_log`, same
    ``update_assignment_cost``/``update_assignment_tokens`` writers) so this
    is not a new capture mechanism — just the existing one, reachable from a
    second call site. Never raises; returns True iff something was
    persisted.
    """
    from coord.state import update_assignment_cost, update_assignment_tokens  # noqa: PLC0415
    from coord.usage import parse_usage_from_log  # noqa: PLC0415

    parsed = None
    if log_path:
        try:
            parsed = parse_usage_from_log(Path(log_path), provider_name=provider_name)
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "_capture_cost_and_tokens_for_review: local parse failed for %s: %s",
                assignment_id, exc,
            )
            parsed = None

    if parsed is None and host:
        text = _fetch_raw_log_text_by_id(assignment_id, None, host)
        if text:
            import tempfile  # noqa: PLC0415

            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".log", delete=True
                ) as tf:
                    tf.write(text)
                    tf.flush()
                    parsed = parse_usage_from_log(
                        Path(tf.name), provider_name=provider_name
                    )
            except Exception as exc:  # noqa: BLE001
                log.debug(
                    "_capture_cost_and_tokens_for_review: remote parse failed "
                    "for %s: %s", assignment_id, exc,
                )
                parsed = None

    if parsed is None:
        return False

    wrote = False
    if parsed.total_cost_usd and parsed.total_cost_usd > 0:
        try:
            update_assignment_cost(assignment_id, parsed.total_cost_usd)
            wrote = True
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "_capture_cost_and_tokens_for_review: failed to persist cost "
                "for %s: %s", assignment_id, exc,
            )

    token_total = (
        parsed.input_tokens + parsed.output_tokens
        + parsed.cache_creation_tokens + parsed.cache_read_tokens
    )
    if token_total > 0:
        try:
            update_assignment_tokens(
                assignment_id,
                input_tokens=parsed.input_tokens,
                output_tokens=parsed.output_tokens,
                cache_creation_tokens=parsed.cache_creation_tokens,
                cache_read_tokens=parsed.cache_read_tokens,
                num_turns=parsed.num_turns,
            )
            wrote = True
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "_capture_cost_and_tokens_for_review: failed to persist "
                "tokens for %s: %s", assignment_id, exc,
            )

    return wrote


def _warn_missing_review_verdict(
    transition: Transition, entry: dict, diagnostic: list,
) -> None:
    """#1956: when a review's structured verdict could not be parsed, make it
    LOUD instead of silent — run the #1348/#1956 diagnostics against the raw
    log text and ``log.warning`` a recovery command.

    Before this, a review that reached ``END_REVIEW`` with a full body but
    no ``REVIEW_VERDICT:`` header (quadraui#533's live incident — grepping
    the raw log found the string exactly once, inside the briefing's own
    instructions, never in an assistant message) landed ``status="done"``
    with ``review_verdict IS NULL`` and nothing anywhere said so; the merge
    gate just read ``review_required`` forever. Appends whichever
    diagnostic fired (if any) to *diagnostic* so :func:`post_transition` can
    tailor the GitHub-visible completion comment too — the operator should
    not have to go spelunking in ``coord notify``'s own log to learn this.
    Best-effort throughout: a failure to even fetch the raw text is
    swallowed, matching this module's "never crash notify" contract.
    """
    from coord.review import (  # noqa: PLC0415
        detect_end_review_without_verdict,
        detect_unparsed_review_marker,
    )

    text = _fetch_raw_log_text(transition, entry)
    if not text:
        return
    aid = transition.assignment_id
    log_path = entry.get("log_path")
    recover_hint = (
        f"coord report-result --assignment {aid} "
        "--verdict <approve|request-changes> --verdict-source recovered "
        '--verdict-reason "<why>" --body-file <extracted-review.md>'
    )

    end_marker = detect_end_review_without_verdict(text, transcript_path=log_path)
    if end_marker is not None:
        log.warning(
            "review %s: reviewer wrote END_REVIEW but never emitted "
            "REVIEW_VERDICT: anywhere (#1956) — this is NOT a crashed/"
            "truncated session, the verdict is very likely recoverable "
            "from the transcript. Recover with:\n  %s\nExcerpt before "
            "END_REVIEW:\n%s",
            aid, recover_hint, end_marker.excerpt,
        )
        diagnostic.append(end_marker)
        return

    marker = detect_unparsed_review_marker(text, transcript_path=log_path)
    if marker is not None:
        log.warning(
            "review %s: a REVIEW_VERDICT: marker is present but malformed "
            "(#1348, detected word=%r) — the strict parser rejected it. "
            "Recover with:\n  %s",
            aid, marker.verdict_word, recover_hint,
        )
        diagnostic.append(marker)
        return

    log.debug(
        "review %s: no REVIEW_VERDICT:/END_REVIEW markers found at all — "
        "likely a crashed or truncated session, not a #1956/#1348 "
        "recoverable case",
        aid,
    )


def _try_parse_and_post_review(
    transition: Transition,
    record: dict,
    entry: dict,
    duration: float | None,
    *,
    _diagnostic: list | None = None,
) -> bool:
    """Parse reviewer findings from the log and post as a PR review or issue comment.

    Returns True if a review was successfully posted (either as a ``gh pr review``
    or as an issue comment when no PR number is available), False on any failure.
    Silently swallows all errors so callers can fall back gracefully.

    *_diagnostic* (#1956): optional out-parameter, mirroring
    ``coord.interactive``'s identically-shaped convention for #1348. When a
    list is supplied and the structured verdict cannot be parsed, whichever
    of :func:`coord.review.detect_end_review_without_verdict` /
    :func:`coord.review.detect_unparsed_review_marker` fires is appended to
    it, so the caller can tailor the fallback GitHub comment instead of a
    generic "could not be extracted" message every single time.
    """
    from coord.review import parse_review_from_log, parse_review_from_agent  # noqa: PLC0415

    log_path = entry.get("log_path")
    findings = None
    if log_path:
        try:
            findings = parse_review_from_log(log_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to parse review log for %s: %s", transition.assignment_id, exc)

    # Local file unavailable (worker ran on a remote agent whose log isn't on
    # this filesystem) — fetch via the agent's /logs endpoint and parse the
    # same way. Agents never use gh; the coordinator pulls + posts.
    if findings is None:
        host = _agent_host(transition.machine_name)
        if host:
            try:
                findings = parse_review_from_agent(host, transition.assignment_id)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "Failed to fetch review log from agent %s for %s: %s",
                    host, transition.assignment_id, exc,
                )

    if findings is None:
        if _diagnostic is not None:
            try:
                _warn_missing_review_verdict(transition, entry, _diagnostic)
            except Exception as exc:  # noqa: BLE001 — diagnostics must never crash notify
                log.debug(
                    "review %s: #1956 diagnostic itself failed: %s",
                    transition.assignment_id, exc,
                )
        return False

    # #253: persist the parsed verdict on the review assignment so the merge
    # gate can refuse to merge work whose review hasn't approved.  Independent
    # of auto_loop (which may be disabled in config).
    # #bounce: also persist the findings.body so `coord bounce` (and the
    # future per-stage display) can read it from the DB without re-fetching
    # the worker's full log.
    _persist_review_findings(
        transition.assignment_id, findings.verdict, findings.body
    )

    review_target = record.get("review_target")
    repo_github = record["repo_github"]

    # Determine whether review_target is a PR number (integer string) or a branch.
    pr_number: int | None = None
    if review_target:
        try:
            pr_number = int(review_target)
        except (ValueError, TypeError):
            pr_number = None

    # #248: prepend a machine-readable header so the TUI / coordinator can
    # surface the verdict + counts without re-ingesting the prose body.
    body_with_header = _attach_review_header(
        findings.body,
        verdict=findings.verdict,
        reviewer_machine=transition.machine_name,
        assignment_id=transition.assignment_id,
    )

    if pr_number is not None:
        try:
            github_ops.post_pr_review(repo_github, pr_number, findings.verdict, body_with_header)
            mark_review_posted(transition.assignment_id)
            return True
        except Exception as exc:  # noqa: BLE001
            # GitHub rejects self-reviews (same user who opened the PR can't
            # review it via the API). Log the actual error and fall through to
            # post the findings as an issue comment instead of silently failing.
            log.warning(
                "Failed to post PR review for %s PR#%s via gh: %s — "
                "falling back to issue comment",
                transition.assignment_id, pr_number, exc,
            )
            # Fall through to the issue-comment path below.

    # No PR number available, or gh pr review was rejected — post findings as
    # an issue comment so they are never silently lost.
    verdict_label = "✅ Approved" if findings.verdict == "approve" else "⚠️ Changes Requested"
    if pr_number is not None:
        preamble = (
            f"*Reviewer findings could not be posted directly to PR #{pr_number} "
            f"(gh pr review was rejected — likely a self-review restriction). "
            f"Findings are reproduced here.*"
        )
    else:
        preamble = (
            "*Reviewer could not post directly to a PR (no PR number available). "
            "Findings are reproduced here.*"
        )
    body = (
        f"## Review Complete — {verdict_label}\n\n"
        f"{preamble}\n\n"
        f"{body_with_header}"
    )
    try:
        github_ops.post_issue_comment(repo_github, transition.issue_number, body)
        mark_review_posted(transition.assignment_id)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "Failed to post review comment for %s: %s", transition.assignment_id, exc
        )
        return False


def _attach_review_header(
    body: str,
    *,
    verdict: str,
    reviewer_machine: str | None = None,
    assignment_id: str | None = None,
) -> str:
    """#248: prepend the machine-readable header line to a review *body*.

    Counts are derived heuristically from the body's markdown sections.
    The header always carries the verdict; counts/identity fields are
    omitted when unavailable.
    """
    from coord.review import (  # noqa: PLC0415 — local import keeps import graph clean
        estimate_review_counts, format_review_header,
    )
    blocking, nonblocking, nits = estimate_review_counts(body)
    header = format_review_header(
        verdict=verdict,
        reviewer_machine=reviewer_machine,
        assignment_id=assignment_id,
        blocking=blocking,
        nonblocking=nonblocking,
        nits=nits,
    )
    return f"{header}\n\n{body}"


def _try_parse_and_post_plan(
    transition: Transition,
    record: dict,
    entry: dict,
    duration: float | None,
) -> bool:
    """Try to parse a WorkerPlan from the worker log and post it to GitHub.

    Returns True if a plan comment was successfully posted, False otherwise.
    Silently swallows all errors so callers can fall back gracefully.
    """
    from coord.plan_parser import parse_plan_from_log, parse_plan_from_agent  # noqa: PLC0415

    log_path = entry.get("log_path")
    worker_plan = None
    if log_path:
        try:
            worker_plan = parse_plan_from_log(log_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to parse plan log for %s: %s", transition.assignment_id, exc)

    # Local log unavailable (worker ran on a remote agent — entry.log_path
    # is the agent's filesystem path, not the coordinator's).  Mirror the
    # review path: fall back to the agent's /logs/<id> endpoint.  Without
    # this, every remote-agent plan got posted as a generic "completion"
    # comment and the structured plan was lost (we hit this on quadraui#264).
    if worker_plan is None or worker_plan.is_empty():
        host = _agent_host(transition.machine_name)
        if host:
            try:
                worker_plan = parse_plan_from_agent(host, transition.assignment_id)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "Failed to fetch plan log from agent %s for %s: %s",
                    host, transition.assignment_id, exc,
                )

    if worker_plan is None or worker_plan.is_empty():
        return False

    try:
        body = format_plan(
            assignment_id=transition.assignment_id,
            machine_name=transition.machine_name,
            repo_name=transition.repo_name,
            issue_number=transition.issue_number,
            plan=worker_plan,
            duration_seconds=duration,
        )
        github_ops.post_issue_comment(
            record["repo_github"], transition.issue_number, body
        )
        # Cache the parsed plan in the state directory.
        save_plan(transition.assignment_id, worker_plan.to_dict())
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to post plan comment for %s: %s", transition.assignment_id, exc)
        return False

    return True


def _capture_claude_session_id(transition: Transition, entry: dict) -> None:
    """#315: persist the worker's claude session ID to the coordinator DB.

    The agent captures this from the ``system.init`` event in the worker log
    and includes it in the ``/status`` response.  Once stored in the DB,
    ``coord chat-continue`` can read it and pass ``--resume <id>`` to the
    next worker so it loads the prior conversation.  Best-effort; a missing
    ID just means chat-continue will refuse with a clear error.
    """
    session_id = entry.get("claude_session_id")
    if not isinstance(session_id, str) or not session_id:
        return
    try:
        from coord.state import update_assignment_claude_session_id  # noqa: PLC0415
        update_assignment_claude_session_id(transition.assignment_id, session_id)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "_capture_claude_session_id: failed for %s: %s",
            transition.assignment_id, exc,
        )


def post_transition(transition: Transition, record: dict, entry: dict) -> None:
    """Post the GitHub comment for one transition and mark it notified."""
    started = entry.get("started_at")
    finished = entry.get("finished_at")
    duration = (finished - started) if (started and finished) else None
    # #208: capture worker cost as soon as the assignment completes — the
    # value is in the worker's final stream-json result event and would
    # otherwise be lost when the agent prunes the log.  Best-effort:
    # local log → remote agent entry → skip.
    _capture_cost(transition, entry, record)
    # #252: capture the worker-emitted SMOKE_TESTS block at the same
    # moment so the TUI can render it under the Test stage.  Same
    # best-effort discipline — failure is silent.
    _capture_smoke_tests(transition, entry)
    # #874: capture the worker's ### Summary prose block at the same moment
    # so the board has a durable, queryable summary field.  Best-effort.
    _capture_completion_summary(transition, entry)
    # #315: persist the worker's claude session ID so chat-continue can
    # pass --resume to the next worker.  Best-effort; silent on failure.
    _capture_claude_session_id(transition, entry)
    common = dict(
        assignment_id=transition.assignment_id,
        machine_name=transition.machine_name,
        repo_github=record["repo_github"],
        repo_name=transition.repo_name,
        issue_number=transition.issue_number,
        duration_seconds=duration,
        log_path=entry.get("log_path"),
    )
    assignment_type = record.get("type", "work")
    if transition.issue_number == 0:
        # #3039: issue_number=0 is the established "no GitHub issue" sentinel
        # (see coord/milestone_chat.py:524, coord/refine_chat.py:439,
        # coord/new_issue_chat.py) — a board-level chat (decomposition-chat
        # against a portal submission, a brand-new milestone/issue draft,
        # board-level refinement) has no real issue to comment on, and the
        # TUI routes these rows to a Board Chat tab rather than an issue
        # thread. `gh issue comment 0` always fails (GraphQL "Could not
        # resolve to an issue or pull request with the number of 0"), so
        # posting must be skipped entirely rather than retried — record the
        # notification locally only, same as the milestone-chat/refinement
        # no-post branch below (which happens to also apply here, but not
        # every type=="refinement" row is issue_number==0, so this check
        # must stand on its own rather than folding into that allowlist).
        #
        # Non-blocking #3039 follow-up: thread the same failure-reason/
        # exit-code pair every other EVENT_FAILURE branch in this function
        # carries (see the identical `or` chain a few branches down) so an
        # EVENT_FAILURE sentinel row doesn't land as `status='failed'` with
        # both columns null — `mark_notified` only applies them on an
        # EVENT_FAILURE-flavoured write, so passing them unconditionally is
        # a no-op for every other event.
        _failure_reason = (
            entry.get("usage_limit_reason")
            or entry.get("api_error_reason")
            or entry.get("push_failure_reason")
            or entry.get("spend_ceiling_reason")
            or entry.get("truncation_reason")
            or entry.get("runtime_ceiling_reason")
            or entry.get("host_sleep_reason")
        )
        mark_notified(
            transition.assignment_id,
            transition.event,
            branch=entry.get("branch"),
            failure_reason=_failure_reason,
            exit_code=transition.exit_code,
        )
        return
    if transition.event == EVENT_COMPLETION and assignment_type in (
        "refinement",
        "milestone-chat",
    ):
        # #315: refinement chat turns are developer-side conversation — do NOT
        # post completion comments to GitHub.  Each turn would spam the issue
        # with identical "assignment completed" noise.  We still capture cost,
        # smoke tests, and session ID above; just skip the GitHub post.
        # #770: milestone-chat is dispatched AGAINST the tracking issue
        # itself (unlike refinement's target issue, this one is the live
        # planning document a human reads) — a generic completion comment on
        # every conversational turn would be even noisier here. The
        # meaningful GitHub-visible effect is the tracking issue's body
        # update via `coord milestone write-order`, not a completion comment.
        mark_notified(
            transition.assignment_id,
            transition.event,
            branch=entry.get("branch"),
        )
    elif transition.event == EVENT_COMPLETION and assignment_type == "plan":
        # For plan assignments, post the structured plan comment.  Fall back
        # to a standard completion comment if the log can't be parsed.
        posted = _try_parse_and_post_plan(transition, record, entry, duration)
        if not posted:
            post_completion(exit_code=transition.exit_code or 0, **common)
        mark_notified(
            transition.assignment_id,
            EVENT_PLAN if posted else EVENT_COMPLETION,
            branch=entry.get("branch"),
        )
    elif transition.event == EVENT_COMPLETION and assignment_type == "review":
        # For review assignments, parse the structured findings and post as a
        # PR review (or issue comment when no PR number is available).  Fall
        # back to a plain completion comment noting the parse failure — #1956:
        # tailored per-diagnostic instead of one generic message, so an
        # operator reading GitHub (not `coord notify`'s own log) can ALSO see
        # that a verdict is recoverable, not just that parsing failed.
        _diag: list = []
        posted = _try_parse_and_post_review(
            transition, record, entry, duration, _diagnostic=_diag,
        )
        if not posted:
            from coord.review import EndReviewWithoutVerdict  # noqa: PLC0415

            if _diag and isinstance(_diag[0], EndReviewWithoutVerdict):
                fallback_summary = (
                    "Review assignment completed and the reviewer wrote END_REVIEW, "
                    "but never emitted the machine-readable REVIEW_VERDICT: header "
                    "(#1956) — this is NOT a crashed/truncated session, the verdict "
                    "is very likely recoverable from the transcript. Recover with: "
                    f"`coord report-result --assignment {transition.assignment_id} "
                    "--verdict <approve|request-changes> --verdict-source recovered "
                    '--verdict-reason "<why>" --body-file <extracted-review.md>`.'
                )
            elif _diag:
                fallback_summary = (
                    "Review assignment completed but a REVIEW_VERDICT: marker in "
                    "the worker log was malformed and could not be parsed (#1348) "
                    "— the verdict is likely still recoverable from the transcript. "
                    "Recover with: "
                    f"`coord report-result --assignment {transition.assignment_id} "
                    "--verdict <approve|request-changes> --verdict-source recovered "
                    '--verdict-reason "<why>" --body-file <extracted-review.md>`.'
                )
            else:
                fallback_summary = (
                    "Review assignment completed but findings could not be extracted "
                    "from the worker log. The reviewer may not have produced the "
                    "expected structured output (REVIEW_VERDICT / REVIEW_BODY / END_REVIEW)."
                )
            post_completion(
                exit_code=transition.exit_code or 0,
                summary=fallback_summary,
                **common,
            )
        mark_notified(
            transition.assignment_id,
            transition.event,
            branch=entry.get("branch"),
        )
    elif transition.event == EVENT_COMPLETION and assignment_type == "conflict-fix":
        post_completion(exit_code=transition.exit_code or 0, **common)
        mark_notified(
            transition.assignment_id,
            transition.event,
            branch=entry.get("branch"),
        )
        # Re-enqueue the parent merge entry so the next `coord merge` retries.
        # This mirrors the reconcile() path — whichever runs first wins.
        #
        # #2565: a `claude -p` conflict-fix worker ends its turn the same
        # way (exit 0 / EVENT_COMPLETION here) whether it actually resolved
        # the conflict or judged it SEMANTIC and gave up with a STUCK line —
        # it has no way to set its own exit code, so this branch previously
        # treated every clean completion as a resolved conflict and reset
        # the entry to PENDING regardless. `coord notify` is the routinely-
        # scheduled path (unlike the full `reconcile()`, which only
        # `coord resume` calls), so this was the operationally dominant
        # place the bug bit. Read the worker's own `coord:conflict=semantic`
        # marker from its transcript first — mirrors
        # `coord.reconcile._on_conflict_fix_done` — and downgrade to
        # `succeeded=False` when it's present, so the entry lands on the
        # same HUMAN_REQUIRED/escalation outcome a reported failure would,
        # instead of silently retrying the identical, already-diagnosed
        # conflict.
        parent_id = record.get("review_of_assignment_id")
        if parent_id:
            from coord.conflict_fix import detect_semantic_conflict  # noqa: PLC0415
            from coord.reconcile import on_conflict_fix_done  # noqa: PLC0415

            log_path = entry.get("log_path")
            host = _agent_host(transition.machine_name)
            try:
                semantic = detect_semantic_conflict(
                    log_path=log_path,
                    host=host,
                    assignment_id=transition.assignment_id,
                )
            except Exception:  # noqa: BLE001 — best-effort, never break notify
                semantic = False

            stuck_summary: str | None = None
            board = None
            config = None
            if semantic:
                progress = entry.get("progress") or {}
                stuck_summary = progress.get("stuck")
                if not stuck_summary and log_path:
                    try:
                        from coord.progress import parse_progress  # noqa: PLC0415
                        stuck_summary = parse_progress(log_path).stuck
                    except Exception:  # noqa: BLE001
                        stuck_summary = None
                # Board/config are only needed to attempt the #1291
                # escalated retry — load them lazily, and only for this rare
                # give-up path, so the overwhelming common case (no marker,
                # a real fix) pays no extra cost.
                try:
                    from coord.board_service import read_board  # noqa: PLC0415
                    from coord.config import load as _load_config  # noqa: PLC0415
                    board = read_board()
                    config = _load_config()
                except Exception:  # noqa: BLE001
                    board, config = None, None

            on_conflict_fix_done(
                parent_assignment_id=parent_id,
                fix_assignment_id=transition.assignment_id,
                machine_name=transition.machine_name,
                succeeded=not semantic,
                semantic=semantic,
                board=board,
                config=config,
                stuck_summary=stuck_summary,
            )
            if semantic and board is not None:
                try:
                    from coord.board_service import write_board  # noqa: PLC0415
                    write_board(board)
                except Exception:  # noqa: BLE001
                    log.exception(
                        "notify: failed to persist board after semantic "
                        "conflict-fix escalation for %s",
                        transition.assignment_id,
                    )
    elif transition.event == EVENT_COMPLETION and assignment_type == "smoke":
        # #1021: propagate the headless smoke result to the parent work row's
        # Test verdict so the merge gate is satisfied automatically. #2244:
        # the RESULT is the worker's `SMOKE:` marker, not its exit code.
        post_completion(exit_code=transition.exit_code or 0, **common)
        mark_notified(
            transition.assignment_id,
            transition.event,
            branch=entry.get("branch"),
        )
        parent_id = record.get("review_of_assignment_id")
        if parent_id:
            # Guard: only auto-certify when the issue's test-mode is "auto"
            # or unset (no label).  A "smoke" label means the TUI offers an
            # interactive smoke agent — do NOT auto-certify here.
            from coord.state import get_issue_test_mode  # noqa: PLC0415
            test_mode = get_issue_test_mode(
                transition.repo_name, transition.issue_number
            )
            if test_mode != "smoke":
                _record_smoke_verdict(transition, entry, parent_id)
    elif transition.event == EVENT_FAILURE and assignment_type == "smoke":
        # #1605: the Test-stage WORKER itself died (a dead agent, a killed
        # process group, a terminal API error — anything short of the
        # worker actually printing `SMOKE: pass`/`SMOKE: fail`) without ever
        # producing a verdict. Mirrors the EVENT_COMPLETION branch above
        # (#1021) but for the terminal-FAILED case that branch never
        # covered: before this, a failed smoke row left the parent's
        # `test_state` at whatever `dispatch_smoke` set it to (almost always
        # `"running"`, #1426) — forever, since no gate ever resolves
        # `"running"` on its own. That is the #1598 incident: a smoke worker
        # died on a terminal API error and the issue was permanently
        # stranded with the board reporting a plausible in-progress state.
        # #1797: `push_failure_reason` is the same column too — see the
        # identical `or` chain in `coord.reconcile.reconcile_completed_assignments`.
        # #2131: `spend_ceiling_reason` is the same column again — the
        # per-leg spend ceiling killed the worker. It must reach `error=`
        # below as well as `mark_notified`, so the GitHub failure comment
        # says "spend ceiling" instead of leaving an operator to guess why a
        # leg died mid-task. Mutually exclusive with the other three (see
        # `coord.reconcile.reconcile_completed_assignments`).
        # #2316: `truncation_reason` is the same column again — the worker's
        # output-token ceiling cut it off before it committed anything. It
        # must reach `error=` below too, so the GitHub failure comment reads
        # "the model was cut off at its output limit before writing
        # anything" instead of the misleading "exited cleanly but pushed 0
        # commits" the #448 ADVISORY default would otherwise have produced.
        # Mutually exclusive with the other four (see
        # `coord.reconcile.reconcile_completed_assignments`).
        # #2638: `runtime_ceiling_reason`/`host_sleep_reason` are the same
        # column again — a suspended/asleep host killed the leg. Must reach
        # `error=` below too, so the GitHub failure comment says "runtime
        # ceiling" / "host sleep detected" instead of leaving the operator to
        # `journalctl | grep suspend`. Mutually exclusive with the other five
        # (see `coord.reconcile.reconcile_completed_assignments`).
        _failure_reason = (
            entry.get("usage_limit_reason")
            or entry.get("api_error_reason")
            or entry.get("push_failure_reason")
            or entry.get("spend_ceiling_reason")
            or entry.get("truncation_reason")
            or entry.get("runtime_ceiling_reason")
            or entry.get("host_sleep_reason")
        )
        post_failure(
            exit_code=transition.exit_code,
            error=entry.get("error") or _failure_reason or "",
            **common,
        )
        mark_notified(
            transition.assignment_id,
            transition.event,
            branch=entry.get("branch"),
            failure_reason=_failure_reason,
            exit_code=transition.exit_code,
        )
        parent_id = record.get("review_of_assignment_id")
        if parent_id:
            from coord.reconcile import (  # noqa: PLC0415
                propagate_smoke_terminal_failure,
            )
            propagate_smoke_terminal_failure(
                parent_assignment_id=parent_id,
                failure_reason=_failure_reason,
            )
    elif transition.event == EVENT_COMPLETION:
        # #2188: a `deliverable:analysis` issue that legitimately ended with
        # 0 commits has no diff to point at — the deliverable IS the
        # worker's own final message (`AgentAssignment.result_text`, see
        # `coord.agent.AgentServer._reap`). Post it as the completion
        # comment's summary so it reaches the issue automatically; the
        # worker itself has no `gh` access to post it. Every other
        # completion (the overwhelming majority) is unaffected — `entry.get
        # ("analysis_deliverable")` is only ever truthy for this one shape.
        _analysis_summary = (
            entry.get("result_text") or ""
            if entry.get("analysis_deliverable")
            else ""
        )
        post_completion(
            exit_code=transition.exit_code or 0,
            summary=_analysis_summary,
            **common,
        )
        mark_notified(
            transition.assignment_id,
            transition.event,
            branch=entry.get("branch"),
        )
    elif transition.event == EVENT_ADVISORY:
        # #448: 0-commit clean exit — post a distinctive advisory comment.
        # No ❌ emoji, no re-dispatch suggestion; just surfaces the advisory
        # state on GitHub so operators not watching coord status are informed.
        post_advisory(
            reason=entry.get("zero_commit_reason") or "",
            **common,
        )
        mark_notified(
            transition.assignment_id,
            transition.event,
            branch=entry.get("branch"),
        )
    elif transition.event == EVENT_REFUSED_POLICY:
        # #2234: 0-commit clean exit whose worker cited a standing repo-rule
        # prohibition — post a distinctive comment naming the routing
        # decision (needs the coordinator), not an advisory "human review
        # needed" framing. Without this arm the event fell into the
        # catch-all `else` below, which requires a failure_reason-shaped
        # signal to post anything and silently drops everything else —
        # exactly the "no comment at all" regression #2234's review caught.
        post_refused_policy(
            reason=entry.get("policy_refusal_reason") or "",
            **common,
        )
        mark_notified(
            transition.assignment_id,
            transition.event,
            branch=entry.get("branch"),
        )
    else:
        # #1605/#1797: carry the agent's own diagnostic (a usage-limit kill,
        # a terminal API-error classification, or an auth-shaped push
        # failure — all stamped by `AgentServer._reap` onto this same
        # `/status` completed entry — see
        # `coord.reconcile.reconcile_completed_assignments`'s identical
        # `or`) through to `mark_notified` so a `status='failed'` row is
        # never left with both `failure_reason` and `exit_code` null. This
        # is the branch a `type="work"` push-auth failure actually hits
        # (none of the type-specific `elif`s above match "work"), so
        # `_failure_reason` also feeds `error=` below — otherwise the
        # posted GitHub failure comment's `error` field is blank for
        # exactly the failure #1797 exists to surface.
        # #2131: `spend_ceiling_reason` is the same column again — the
        # per-leg spend ceiling killed the worker. It must reach `error=`
        # below as well as `mark_notified`, so the GitHub failure comment
        # says "spend ceiling" instead of leaving an operator to guess why a
        # leg died mid-task. Mutually exclusive with the other three (see
        # `coord.reconcile.reconcile_completed_assignments`).
        # #2316: `truncation_reason` is the same column again — the worker's
        # output-token ceiling cut it off before it committed anything. It
        # must reach `error=` below too, so the GitHub failure comment reads
        # "the model was cut off at its output limit before writing
        # anything" instead of the misleading "exited cleanly but pushed 0
        # commits" the #448 ADVISORY default would otherwise have produced.
        # Mutually exclusive with the other four (see
        # `coord.reconcile.reconcile_completed_assignments`).
        # #2638: `runtime_ceiling_reason`/`host_sleep_reason` are the same
        # column again — a suspended/asleep host killed the leg. Must reach
        # `error=` below too, so the GitHub failure comment says "runtime
        # ceiling" / "host sleep detected" instead of leaving the operator to
        # `journalctl | grep suspend`. Mutually exclusive with the other five
        # (see `coord.reconcile.reconcile_completed_assignments`).
        _failure_reason = (
            entry.get("usage_limit_reason")
            or entry.get("api_error_reason")
            or entry.get("push_failure_reason")
            or entry.get("spend_ceiling_reason")
            or entry.get("truncation_reason")
            or entry.get("runtime_ceiling_reason")
            or entry.get("host_sleep_reason")
        )
        post_failure(
            exit_code=transition.exit_code,
            error=entry.get("error") or _failure_reason or "",
            **common,
        )
        mark_notified(
            transition.assignment_id,
            transition.event,
            branch=entry.get("branch"),
            failure_reason=_failure_reason,
            exit_code=transition.exit_code,
        )


def post_orphaned_review_findings(
    config: Config,
    repo_name: str | None = None,
) -> list[str]:
    """Walk done-review assignments with unposted findings and attempt to post.

    Handles two scenarios that cause findings to be lost:

    1. The agent reported the assignment as 'done' but notify never ran (or
       ran at the wrong time) — no notification record in the DB at all.
    2. Notify ran and posted a fallback completion comment (because the log
       couldn't be parsed at that time), but findings were never extracted.

    In both cases ``review_posted_at`` is NULL on the assignment row.

    The function queries each relevant agent server to discover the log path,
    then re-parses and re-posts.  If the agent is offline or its completed
    list no longer contains the assignment, the entry is silently skipped
    so ``coord notify`` stays non-fatal.

    Returns a list of assignment_ids for which findings were successfully posted.
    Optionally filter to a single *repo_name*.
    """
    from coord.review import parse_review_from_log  # noqa: PLC0415

    candidates = load_done_reviews_needing_post(repo_name=repo_name)
    if not candidates:
        return []

    notified = load_notified()
    machines_by_name = {m.name: m for m in config.machines}

    # Group by machine so we query each agent server once.
    by_machine: dict[str, list[dict]] = {}
    for row in candidates:
        by_machine.setdefault(row["machine_name"], []).append(row)

    posted_ids: list[str] = []
    for machine_name, rows in by_machine.items():
        machine = machines_by_name.get(machine_name)
        if machine is None:
            log.debug("post_orphaned: unknown machine %r — skipping %d assignment(s)", machine_name, len(rows))
            continue

        status = _agent_status(machine.host)
        log_by_id: dict[str, str] = {}
        if status:
            for entry in status.get("completed", []):
                eid = entry.get("id")
                lp = entry.get("log_path")
                if eid and lp:
                    log_by_id[eid] = lp

        for row in rows:
            aid = row["assignment_id"]
            log_path = log_by_id.get(aid)

            # #2476: capture cost/tokens for this row too. This is the ONLY
            # place a review whose verdict is being recovered here (rather
            # than through the direct detect_transitions → post_transition
            # path, which already calls `_capture_cost`) ever gets a chance
            # at cost/token capture — once `posted`/`mark_notified` below
            # lands, nothing ever revisits this row. Independent of whether
            # findings parsing succeeds: a row whose body can't be recovered
            # can still have its cost recovered from the same log.
            _capture_cost_and_tokens_for_review(
                aid, log_path=log_path, host=machine.host,
                provider_name=row.get("provider_name"),
            )

            findings = None
            # Try local file first (cheap) — works when notify runs on the
            # same host as the agent. Falls back to fetching via HTTP so the
            # coordinator can post reviews from any machine.
            if log_path:
                try:
                    findings = parse_review_from_log(log_path)
                except Exception as exc:  # noqa: BLE001
                    log.warning("post_orphaned: failed to parse local log for %s: %s", aid, exc)
            if findings is None and machine.host:
                from coord.review import parse_review_from_agent  # noqa: PLC0415
                try:
                    findings = parse_review_from_agent(machine.host, aid)
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "post_orphaned: failed to fetch log from agent %s for %s: %s",
                        machine.host, aid, exc,
                    )
            if findings is None:
                log.debug("post_orphaned: no findings (local + agent both missed) for %s", aid)
                continue

            # #bounce: cache the parsed findings so coord bounce + the
            # per-stage display can skip the HTTP fetch on later runs.
            _persist_review_findings(aid, findings.verdict, findings.body)

            review_target = row.get("review_target")
            repo_github = row.get("repo_github") or ""
            issue_number = row.get("issue_number", 0)

            pr_number: int | None = None
            if review_target:
                try:
                    pr_number = int(review_target)
                except (ValueError, TypeError):
                    pr_number = None

            # Build a preamble that distinguishes retroactive posts from fresh ones.
            already_notified = aid in notified
            if already_notified:
                retro_note = (
                    "\n\n*Note: a completion comment was posted earlier but findings "
                    "could not be extracted at that time. These are the retroactive findings.*"
                )
            else:
                retro_note = ""

            # #248: same header injection as the live path.
            body_with_header = _attach_review_header(
                findings.body,
                verdict=findings.verdict,
                reviewer_machine=machine.name,
                assignment_id=aid,
            )

            posted = False
            if pr_number is not None:
                try:
                    github_ops.post_pr_review(repo_github, pr_number, findings.verdict, body_with_header + retro_note)
                    posted = True
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "post_orphaned: failed gh pr review for %s PR#%s: %s — "
                        "falling back to issue comment",
                        aid, pr_number, exc,
                    )

            if not posted:
                verdict_label = "✅ Approved" if findings.verdict == "approve" else "⚠️ Changes Requested"
                if pr_number is not None:
                    preamble = (
                        f"*Reviewer findings could not be posted directly to PR #{pr_number} "
                        f"(gh pr review was rejected — likely a self-review restriction). "
                        f"Findings are reproduced here.*"
                    )
                else:
                    preamble = (
                        "*Reviewer could not post directly to a PR (no PR number available). "
                        "Findings are reproduced here.*"
                    )
                body = (
                    f"## Review Complete — {verdict_label}\n\n"
                    f"{preamble}{retro_note}\n\n"
                    f"{body_with_header}"
                )
                try:
                    github_ops.post_issue_comment(repo_github, issue_number, body)
                    posted = True
                except Exception as exc:  # noqa: BLE001
                    log.warning("post_orphaned: failed to post comment for %s: %s", aid, exc)

            if posted:
                mark_review_posted(aid)
                if not already_notified:
                    mark_notified(aid, EVENT_COMPLETION)
                posted_ids.append(aid)
                log.info("post_orphaned: posted findings for review %s", aid)

    return posted_ids


def _dispatch_board_pending_pr_opens(config: Config) -> None:
    """Load the board, open PRs for any work-leg completions still missing
    one, and save (#2844).

    Runs BEFORE smoke/review dispatch so the ``pull_request`` CI run starts
    the instant a work leg pushes its branch, overlapping the ~20-minute
    smoke leg and the review leg instead of being serialised after both.
    `dispatch_pending_pr_opens` (:mod:`coord.review`) is itself idempotent —
    it always finds-or-creates via GitHub — so calling it every pass, even
    after `dispatch_review` already opened the PR, only ever finds the
    existing one. Mirrors :func:`_dispatch_board_pending_smoke` exactly, and
    is safe to call even when the board file doesn't exist.
    """
    from coord.board_service import read_board, write_board
    from coord.review import dispatch_pending_pr_opens

    board = read_board()
    opened = dispatch_pending_pr_opens(board, config)
    if opened:
        write_board(board)


def _dispatch_board_pending_smoke(config: Config) -> None:
    """Load the board, dispatch any pending Test-stage smoke, and save.

    #1426: `dispatch_pending_smoke` (:mod:`coord.smoke`) was previously only
    ever called from `reconcile()`'s per-item loop, and the ONLY sanctioned
    caller of the full `reconcile()` is `coord resume`, a human-invoked
    command. A thin-client setup driven purely by `coord-notify.timer` (which
    calls `notify.run()`, not `reconcile()`) never dispatched the Test stage
    at all — the exact gap `scripts/drive-issue.sh` had to paper over with a
    local `scripts/coord-test-runner.sh` subprocess (#1395). Mirrors
    :func:`_dispatch_board_pending_reviews` exactly, and is safe to call even
    when the board file doesn't exist.
    """
    from coord.board_service import read_board, write_board
    from coord.smoke import dispatch_pending_smoke

    board = read_board()
    dispatched = dispatch_pending_smoke(board, config)
    if dispatched:
        write_board(board)


def _dispatch_board_pending_reviews(config: Config) -> None:
    """Load the board, dispatch any pending reviews, and save.

    Mirrors the review-dispatch loop in reconcile() so that ``coord notify``
    also triggers review dispatch — not just ``coord status --reconcile``.
    Safe to call even when the board file doesn't exist.
    """
    from coord.board_service import read_board, write_board
    from coord.review import dispatch_pending_reviews, dispatch_scoped_reviews_for_queue

    # #749: read_board()/write_board() route through the daemon when
    # board_service is configured, so this no longer silently no-ops on a
    # thin client's empty local DB — read_board() falls back to an
    # effectively-empty board when nothing has been saved yet, which is
    # exactly as harmless as the old "return early" guard.
    board = read_board()

    # #465: review fires immediately on work completion — no manual smoke
    # prerequisite.  Mirrors reconcile().  dispatch_pending_reviews() enforces
    # the bulk-dispatch flood guard (per-pass cap + surge gate, incident
    # 2026-06-08) and the #459 active-fix dedupe, so notify can't flood either.
    dispatched = dispatch_pending_reviews(board, config)

    # #1476: same scoped-re-review dispatch reconcile() runs, so a conflict-fix
    # that voids an approval by changing content gets a delta-scoped re-review
    # from `coord notify` too, not just `coord status --reconcile`.
    dispatched = dispatched + dispatch_scoped_reviews_for_queue(board, config)

    if dispatched:
        write_board(board)


def _sweep_stalled_pipeline(
    config: Config, *, terminal_cache: dict | None = None,
) -> list[StalledDetection]:
    """Detect #1441 stalled-pipeline rows, post one comment per row, and —
    when ``config.pipeline.auto_dispatch_stalled`` is on — dispatch the
    action the original transition would have taken (#1478).

    Loads its own board (rather than accepting one) so mutations from a
    dispatched action (a freshly-enqueued merge entry, a newly dispatched
    review/fix/conflict-fix, ``board.review_state`` flips) can be persisted
    back via ``write_board`` — mirrors ``_dispatch_board_pending_reviews``/
    ``_dispatch_board_pending_smoke`` above. A comment-posting failure for
    one row must not stop the sweep from reaching the rest (matches every
    other best-effort loop in this module) — the ``continue`` on failure
    means that row's ``notified`` key is never set, so it is picked back up
    on the next tick rather than silently dropped.

    An unexpected exception *from* ``dispatch_stalled_pipeline_action``
    itself (e.g. a momentarily-unreachable agent during ``dispatch_review``/
    ``dispatch_conflict_fix``) gets the same treatment, not the "declined"
    treatment: no comment is posted and the row is NOT marked notified, so
    it is retried on the next tick rather than permanently foreclosed. A
    considered decline (``no_action`` returned normally — no capable
    machine, gate not satisfied, entry vanished) still posts the diagnostic
    comment and marks notified per the one-shot "act once" guardrail; only a
    genuine raised exception gets the retry treatment.
    """
    from coord.board_service import read_board, write_board

    board = read_board()
    detections = detect_stalled_pipeline(config, board=board, terminal_cache=terminal_cache)

    posted: list[StalledDetection] = []
    board_dirty = False
    for detection, work in detections:
        try:
            action = dispatch_stalled_pipeline_action(
                detection, work, board, config, terminal_cache=terminal_cache,
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "dispatch_stalled_pipeline_action: unexpected error for %s — "
                "not marking notified so this row is retried next tick",
                detection.assignment_id,
            )
            continue

        dispatched = action.kind in _STALLED_DISPATCH_KINDS
        try:
            if dispatched:
                post_stalled_pipeline_dispatch(detection, action, config)
            else:
                post_stalled_pipeline(detection, config)
        except Exception:  # noqa: BLE001
            continue
        posted.append(detection)
        if dispatched:
            board_dirty = True

        # #1478 guardrail: "log every auto-dispatch to the audit trail with
        # the detection that triggered it" — business-tier (never dropped
        # by the operational/business audit-level gate) for an actual
        # dispatch; operational-tier for a no-op/skip, so the "nothing
        # happened" rows don't inflate the business audit stream but are
        # still reconstructable when `audit.level` includes operational.
        try:
            from coord.audit import record_audit  # noqa: PLC0415

            record_audit(
                tier="business" if dispatched else "operational",
                category="pipeline",
                event_type="stalled_pipeline_auto_dispatch",
                actor="coordinator",
                summary=(
                    f"stalled-pipeline sweep ({detection.reason}) -> {action.kind} "
                    f"for {detection.repo_name}#{detection.issue_number}"
                ),
                repo=detection.repo_name,
                issue=detection.issue_number,
                assignment_id=detection.assignment_id,
                machine=detection.machine_name,
                details={
                    "stalled_reason": detection.reason,
                    "stalled_detail": detection.detail,
                    "action_kind": action.kind,
                    "action_detail": action.detail,
                },
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "record_audit failed for stalled dispatch %s", detection.assignment_id,
            )

    if board_dirty:
        try:
            write_board(board)
        except Exception:  # noqa: BLE001
            log.exception("write_board failed after stalled-pipeline dispatch")

    return posted


# ── #2536: fleet-wide phantom-row self-heal sweep ───────────────────────────
#
# See `coord.diagnose.sweep_dead_running_rows`'s docstring for the full
# rationale. This is the thin `coord notify` glue around it: read the board,
# run the sweep (which does the actual recovery write per row), post one
# comment per row it healed, and audit-log it. No board write-back is needed
# here — the sweep writes through `issue_store` directly (the same seam
# `_cleanup_issue` uses), never mutating the in-memory `board` object.


def post_phantom_row_healed(heal: "PhantomRowHeal", config: Config) -> None:
    """Post the #2536 auto-heal comment for one board row
    :func:`_sweep_phantom_rows` just recovered."""
    from coord.comments import (  # noqa: PLC0415
        format_phantom_row_healed,
    )

    repo = config.repo(heal.repo_name)
    repo_github = repo.github if repo is not None else None
    if not repo_github:
        return
    body = format_phantom_row_healed(
        assignment_id=heal.assignment_id,
        machine_name=heal.machine_name,
        repo_name=heal.repo_name,
        issue_number=heal.issue_number,
        stage=heal.stage,
        detail=heal.detail,
        action=heal.action,
    )
    github_ops.post_issue_comment(repo_github, heal.issue_number, body)


def _sweep_phantom_rows(config: Config) -> list["PhantomRowHeal"]:
    """Scan the board for ``running``/``pending`` rows whose session is
    CONFIRMED dead and aged well past their own needs-attention threshold,
    auto-heal them with the same non-destructive recovery ``coord diagnose
    --reset`` performs by hand, and post one comment per row recovered
    (#2536).

    Gated by ``config.pipeline.auto_heal_phantom_rows`` (**default
    ``True``** — see that field's docstring for why this ships lit unlike
    ``auto_dispatch_stalled``/``escalate_semantic_conflicts``: every action
    here is gated behind a confirmed-dead liveness read plus an aged-out
    wall-clock buffer, and the recovery itself never dispatches work or
    touches a branch).

    Best-effort per row, mirroring :func:`_sweep_stalled_pipeline`'s
    contract: a comment-posting failure for one row must not stop the sweep
    from reaching the rest, and does not block the row's own recovery
    (already durably written by the time this posts) — it just means the
    GitHub comment is missing, which a future manual `coord diagnose` can
    still explain.

    **#2570: not this function's only caller anymore.** ``coord notify``
    (via :func:`run`) is one caller; ``coord.serve_app._phantom_heal_tick``
    is the other, invoked from that daemon's own long-lived tick loop
    (``_phantom_heal_loop``) so the sweep survives a ``~/.coord-venv``
    corruption that also takes out the ``coord notify``/``coord
    drive-queue tick`` subprocesses that share that venv — see
    ``coord/serve_app.py``'s ``_phantom_heal_tick`` docstring and
    ``docs/AGENT_OPERATIONS.md``'s "Periodic coord notify" section for the
    full incident. This function itself needed no change for that: it was
    already a pure, idempotent, board-scanning sweep with no dependency on
    which process calls it.
    """
    if not config.pipeline.auto_heal_phantom_rows:
        return []

    from coord.board_service import read_board  # noqa: PLC0415
    from coord.diagnose import sweep_dead_running_rows  # noqa: PLC0415

    board = read_board()
    healed = sweep_dead_running_rows(board, config)

    posted: list["PhantomRowHeal"] = []
    for heal in healed:
        try:
            post_phantom_row_healed(heal, config)
        except Exception:  # noqa: BLE001
            log.exception(
                "post_phantom_row_healed failed for %s", heal.assignment_id,
            )
            continue
        posted.append(heal)

        try:
            from coord.audit import record_audit  # noqa: PLC0415

            record_audit(
                tier="business",
                category="pipeline",
                event_type="phantom_row_auto_healed",
                actor="coordinator",
                summary=(
                    f"phantom-row sweep healed {heal.stage} row "
                    f"{heal.assignment_id} for {heal.repo_name}#{heal.issue_number}"
                ),
                repo=heal.repo_name,
                issue=heal.issue_number,
                assignment_id=heal.assignment_id,
                machine=heal.machine_name,
                details={"detail": heal.detail, "action": heal.action},
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "record_audit failed for phantom-row heal %s", heal.assignment_id,
            )

    return posted


# ── #2803: fleet-wide stuck test_state='running' watchdog ───────────────────
#
# See `coord.diagnose.sweep_stuck_test_state_rows`'s docstring for the full
# rationale. This is the thin `coord notify` glue around it — read the
# board, run the sweep (which does the actual recovery write per row via
# `coord.reconcile.propagate_smoke_terminal_failure`), post one comment per
# row it healed, and audit-log it. No board write-back is needed here — the
# sweep writes through `coord.state.record_test_verdict` directly, never
# mutating the in-memory `board` object, mirroring `_sweep_phantom_rows`
# above exactly.


def post_stuck_test_state_healed(heal: "StuckTestStateHeal", config: Config) -> None:
    """Post the #2803 auto-heal comment for one board row
    :func:`_sweep_stuck_test_state` just recovered."""
    from coord.comments import (  # noqa: PLC0415
        format_stuck_test_state_healed,
    )

    repo = config.repo(heal.repo_name)
    repo_github = repo.github if repo is not None else None
    if not repo_github:
        return
    body = format_stuck_test_state_healed(
        assignment_id=heal.assignment_id,
        machine_name=heal.machine_name,
        repo_name=heal.repo_name,
        issue_number=heal.issue_number,
        detail=heal.detail,
        action=heal.action,
    )
    github_ops.post_issue_comment(repo_github, heal.issue_number, body)


def _sweep_stuck_test_state(config: Config) -> list["StuckTestStateHeal"]:
    """Scan the board for work rows wedged at ``test_state='running'`` well
    past their Test-stage child's own terminal resolution (or absence),
    auto-clear them for a fresh Test-stage dispatch via
    :func:`coord.reconcile.propagate_smoke_terminal_failure`, and post one
    comment per row recovered (#2803).

    Gated by ``config.pipeline.auto_heal_stuck_test_state`` (**default
    ``True``** — see that field's docstring: every action here is gated on
    the Test-stage child already being TERMINAL or missing entirely, plus a
    fixed grace window, and recovery never fabricates a pass/fail verdict).

    Best-effort per row, mirroring :func:`_sweep_phantom_rows`'s contract: a
    comment-posting failure for one row must not stop the sweep from
    reaching the rest, and does not undo the row's own recovery (already
    durably written by the time this posts).
    """
    if not config.pipeline.auto_heal_stuck_test_state:
        return []

    from coord.board_service import read_board  # noqa: PLC0415
    from coord.diagnose import sweep_stuck_test_state_rows  # noqa: PLC0415

    board = read_board()
    healed = sweep_stuck_test_state_rows(board, config)

    posted: list["StuckTestStateHeal"] = []
    for heal in healed:
        try:
            post_stuck_test_state_healed(heal, config)
        except Exception:  # noqa: BLE001
            log.exception(
                "post_stuck_test_state_healed failed for %s", heal.assignment_id,
            )
            continue
        posted.append(heal)

        try:
            from coord.audit import record_audit  # noqa: PLC0415

            record_audit(
                tier="business",
                category="pipeline",
                event_type="stuck_test_state_auto_healed",
                actor="coordinator",
                summary=(
                    f"stuck test_state sweep healed work row "
                    f"{heal.assignment_id} for {heal.repo_name}#{heal.issue_number}"
                ),
                repo=heal.repo_name,
                issue=heal.issue_number,
                assignment_id=heal.assignment_id,
                machine=heal.machine_name,
                details={"detail": heal.detail, "action": heal.action},
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "record_audit failed for stuck test_state heal %s", heal.assignment_id,
            )

    return posted


@dataclass(frozen=True)
class DrainResult:
    """What one :func:`run_drain` pass actually did.

    ``skipped_locked`` is the "someone else is draining" outcome, which is a
    success, not an error — the next tick picks the work up.

    ``propagated_verdicts`` (#1663) lists the review assignment IDs whose
    verdict this pass wrote through onto the parent **work** row.  Never
    implies a fix worker was dispatched — the drain cannot dispatch one.
    """

    transitions: list[Transition] = field(default_factory=list)
    orphaned_findings: list[str] = field(default_factory=list)
    propagated_verdicts: list[str] = field(default_factory=list)
    skipped_locked: bool = False

    def __bool__(self) -> bool:
        """Truthy when this pass advanced something (for terse log guards)."""
        return bool(
            self.transitions or self.orphaned_findings or self.propagated_verdicts
        )


def run_drain(
    config: Config,
    *,
    lock_path: "Path | None" = None,
    lock_timeout: float = 0.0,
) -> DrainResult:
    """The pipeline's **clock** (#1616) — advance terminal rows' side effects.

    ``reconcile_completed_assignments`` (the daemon's passive tick) writes
    ``status='done'`` and stops there, by contract.  Everything downstream —
    ``finished_at``, the completion comment, the #1076/#1152 test-gate
    backfill, the Test-stage smoke dispatch, the review dispatch, the #1610
    ``finalizing`` → verdict capture — is a side effect of ``coord notify``.
    On this fleet ``coord-notify.timer`` is deliberately disabled and the only
    caller of ``coord notify`` is a live ``coord drive``'s **stall nudge**, so
    a completed stage sat until the stall detector gave up (9 min on #1123,
    47 min on #1122) — and rows with no drive at all (vimcode#611/#613) sat
    until a human poked the daemon.  This function is what the daemon tick
    calls so the pipeline advances on a clock instead of on an accident.

    **Scope is the whole point — this is deliberately NOT ``run()``.**
    ``coord notify`` triggers five side effects; four are bookkeeping with no
    race and no cost if repeated, and one spawns a metered worker.  The line
    sits at exactly one place:

    ==========================================  =======  ===================================
    side effect                                 here?    why
    ==========================================  =======  ===================================
    ``finished_at`` stamped                     yes      no race, no cost
    completion comment posted                   yes      ``coord:`` markers make it idempotent
    test-gate backfill (#1076/#1152)            yes      no race, no cost
    Test-stage smoke dispatch (#1426)           yes      the gate review waits on; see below
    orphaned review findings posted             yes      comment + verdict capture only
    review dispatch                             yes      guarded; see below
    verdict → parent work row (#1663)           yes      no race, no cost; see below
    stuck test_state='running' watchdog (#2803) yes      bookkeeping: clears a lost-verdict
                                                         marker so smoke dispatch (above) can
                                                         redispatch; never fabricates a verdict
    merge enqueue                               n/a      the daemon tick already runs
                                                         ``enqueue_approved_work`` right after
    **work dispatch**                           **no**   stays with a drive or a human
    **fix-round dispatch** (``auto_loop``)      **no**   this is where #476/#477 lives
    **stalled-pipeline sweep/dispatch**         **no**   can dispatch work (#1478)
    ==========================================  =======  ===================================

    #1663 is what the "verdict → parent work row" row costs to learn.  That
    write — ``work.review_state='done'``, ``work.review_verdict=<verdict>``,
    ``record_work_review_verdict``, the merge-queue refresh — is bookkeeping by
    every criterion in this table, but it lived *inside*
    ``auto_loop.process_review_completion`` alongside the fix dispatch, and
    excluding the function excluded both.  So every verdict the daemon consumed
    instead of a human's ``coord notify`` was captured on the review row and
    dropped on the way to the work row, for **both** verdicts — the approve
    case stayed invisible only because ``merge_queue.has_approved_review``
    reads the *review* row.  ``coord drive``, the TUI's Review stage and the
    auto-loop all read the *work* row, so an approved issue simply stopped:
    2026-08-01's overnight batch reviewed five issues clean and merged none of
    them in 4h02m.  The propagation half is now separately callable
    (``auto_loop.propagate_review_verdict_for_transition``) and step 6 calls
    only that; fix dispatch is as unreachable from here as it ever was.

    Why review dispatch is in and fix dispatch is out — the asymmetry is the
    whole argument.  #476/#477, the incident that got ``coord-notify.timer``
    disabled, was duplicate **fix-workers**: they create conflicting branches
    on the same issue and cost real recovery work.  A duplicate *review* costs
    a few dollars and a redundant comment.  Withholding reviews inherits a
    mitigation for a risk that does not apply to them.  And bookkeeping-only
    is not sufficient: work→review is the most frequent boundary in the
    pipeline and the one that stalled #1122, so a drain that stamps state but
    will not dispatch reviews fixes the *watched* half and leaves the
    unwatched half exactly as broken as before.

    Smoke dispatch rides along because ``dispatch_pending_reviews`` holds
    review dispatch until ``test_state`` is passed/skipped when
    ``pipeline.test_precedes_review()`` (#1612).  Draining reviews without
    ever dispatching the Test stage would just move the stall one box left —
    that is #1605.  It is a Test-stage worker on the work's own branch, not a
    second author on a fresh branch, so it carries none of the #476/#477
    shape.

    Stuck / needs-attention detection is deliberately absent: those are
    *notifications*, not pipeline advancement, and giving the daemon a
    periodic detector is #1632's job (which is blocked on this).

    **Dispatch order (#2975).** Smoke/review/PR-open dispatch also runs as a
    HEAD START right at the top of the pass, before this pass's own
    transition detection (which is where a #2464 out-of-band confirmation
    runs — see :mod:`coord.confirm_test`). A confirmation re-runs a repo's
    real build+test synchronously and can hold this very lock for the whole
    confirmation-pass budget, several timer fires' worth for a repo whose
    suite is structurally too slow. Every row already eligible for dispatch
    as of the top of the pass must not queue behind that — a slow suite on
    one repo must delay only its own confirmation, never another repo's
    Test/Review dispatch. The three dispatch functions run a second time
    afterward too, in their historical place, to pick up anything the
    transition-detection step below made newly eligible; all three are
    idempotent, so the repeat costs only a second, mostly-empty scan.

    **Concurrency.**  The whole pass runs under ``~/.coord/notify.lock`` —
    literally :class:`coord.filelock.FileLock`, the same class on the same
    path ``coord drive``'s ``run_notify()`` takes — so a drive's nudge and the
    daemon's clock can never both be inside ``dispatch_pending_reviews``,
    which reads ``review_state == 'pending'`` and writes ``'dispatched'``
    non-atomically (two concurrent passes would both see ``pending`` and
    dispatch two reviews).  ``lock_timeout`` defaults to **0.0**
    (non-blocking): if another drain holds it, return ``skipped_locked`` and
    let the next tick retry rather than pinning a threadpool worker.

    Every step is independently try/except'd — one failing side effect must
    never sink the rest of the pass, and a drain must never crash the daemon.
    """
    from coord.filelock import FileLock, LockBusy, notify_lock_path  # noqa: PLC0415

    lock = FileLock(lock_path if lock_path is not None else notify_lock_path())
    try:
        lock.acquire(timeout=lock_timeout)
    except LockBusy:
        log.debug("notify drain: %s held elsewhere — skipping this pass", lock.path)
        return DrainResult(skipped_locked=True)
    try:
        return _run_drain_locked(config)
    finally:
        lock.release()


def _run_drain_locked(config: Config) -> DrainResult:
    """:func:`run_drain`'s body, with the lock already held.

    Split out so tests can exercise the side effects without the lock and the
    lock without the side effects.
    """
    # Refresh the agent-host cache so _try_parse_and_post_review (and any other
    # helper using _agent_host) can resolve hostnames without threading config
    # through every call.  Mirrors run().
    global _AGENT_HOSTS
    _AGENT_HOSTS = {m.name: m.host for m in config.machines}

    # #2464-review: open this pass's Test-verdict confirmation budget.  We are
    # inside `notify.lock` for the whole pass, so the drain's wall clock is the
    # fleet's; the budget is what keeps it bounded no matter how many smoke
    # rows completed, and is what `notify_client_timeout_seconds()` sizes the
    # thin client's `/notify` timeout off.
    from coord.confirm_test import begin_confirmation_pass  # noqa: PLC0415

    begin_confirmation_pass()

    # Step 0a (#2803, moved up by #2975): clear any work row wedged at
    # test_state='running' well past its Test-stage child's own terminal
    # resolution (or absence) — a lost verdict write, never a fabricated one
    # (see `sweep_stuck_test_state_rows`'s docstring). It still runs BEFORE
    # every smoke dispatch in this pass, which is #2803's invariant: a row
    # this clears is picked up and redispatched in THIS SAME pass, not the
    # next one. It now runs before step 0b's head start as well, so a row it
    # clears gets that head start too rather than waiting behind step 1's
    # confirmations. Nothing is lost by sweeping before transition detection:
    # every case the sweep acts on is gated on
    # `STUCK_TEST_STATE_GRACE_SECONDS` (10 minutes) having elapsed since the
    # child's own resolution, so a child that only just went terminal in
    # THIS pass is out of scope either way.
    try:
        _sweep_stuck_test_state(config)
    except Exception:  # noqa: BLE001
        log.exception("notify drain: stuck test_state sweep failed")

    # Step 0b (#2975): dispatch pending Test-stage smoke / PR-opens / reviews
    # from the board exactly as it reads RIGHT NOW — before step 1 below gets
    # anywhere near a `confirm_branch` call. A confirmation re-runs one
    # repo's real build+test synchronously inside THIS pass and can
    # legitimately hold `notify.lock` for the whole `CONFIRM_PASS_BUDGET_
    # SECONDS` ceiling — several `coord-notify.timer` fires' worth (#2975).
    # Every row already eligible for dispatch as of the top of this pass
    # must not queue behind that: a slow suite on one repo should delay only
    # its own confirmation, never another repo's Test/Review dispatch.
    #
    # Steps 2-3 below repeat these same three calls after the transition
    # detection has had a chance to add anything newly eligible (a work leg
    # that just finished, a PR that just opened) — this head start is
    # additive, not a replacement.
    # All three are idempotent (`_dispatch_board_pending_pr_opens`'s and
    # `_dispatch_board_pending_smoke`'s own docstrings: "safe to call even
    # when the board file doesn't exist", find-or-create PRs, dedupe via
    # `has_active_followup`), so calling each twice in one pass costs
    # nothing beyond a second, mostly-empty scan.
    try:
        _dispatch_board_pending_pr_opens(config)
    except Exception:  # noqa: BLE001
        log.exception("notify drain: head-start PR-open dispatch failed")
    try:
        _dispatch_board_pending_smoke(config)
    except Exception:  # noqa: BLE001
        log.exception("notify drain: head-start smoke dispatch failed")
    try:
        _dispatch_board_pending_reviews(config)
    except Exception:  # noqa: BLE001
        log.exception("notify drain: head-start review dispatch failed")

    # Step 1: post completion/failure/advisory/plan/review comments for rows
    # the agent reports terminal.  This is what stamps `finished_at` (via
    # mark_notified) and captures cost / SMOKE_TESTS / summary / session id /
    # the review verdict + findings.  Idempotent: detect_transitions skips any
    # assignment already in the `notifications` table, so a second drain over
    # the same board posts nothing.
    posted: list[Transition] = []
    # #1663: (transition, record, entry) for every review that completed in
    # THIS pass, so step 6 can propagate its verdict onto the parent work row.
    review_completions: list[tuple[Transition, dict, dict]] = []
    try:
        from coord.comments import EVENT_COMPLETION  # noqa: PLC0415

        for transition, record, entry in detect_transitions(config):
            try:
                post_transition(transition, record, entry)
            except Exception:  # noqa: BLE001 — one bad row must not sink the pass
                log.exception(
                    "notify drain: post_transition failed for %s",
                    transition.assignment_id,
                )
                continue
            posted.append(transition)
            if (
                record.get("type") == "review"
                and transition.event == EVENT_COMPLETION
            ):
                review_completions.append((transition, record, entry))
    except Exception:  # noqa: BLE001
        log.exception("notify drain: detect_transitions failed")

    # Step 2 (#2844): open PRs for work-leg completions still missing one —
    # BEFORE the Test-stage dispatch below, so the pull_request CI run starts
    # overlapping smoke instead of waiting for review dispatch to open the
    # PR ~20 minutes later.
    try:
        _dispatch_board_pending_pr_opens(config)
    except Exception:  # noqa: BLE001
        log.exception("notify drain: PR-open dispatch failed")

    # Step 3: dispatch pending Test-stage smoke (#1426).  Runs BEFORE review
    # dispatch to mirror the pipeline's Work -> Test -> Review order.
    try:
        _dispatch_board_pending_smoke(config)
    except Exception:  # noqa: BLE001
        log.exception("notify drain: smoke dispatch failed")

    # Step 4: dispatch pending reviews.  Carries the #1612 test-precedes-review
    # gate, the #1076/#1152 test-gate backfill, the #946 enqueue gate, the
    # 2026-06-08 flood guard (per-pass cap + surge gate) and the #459 active-fix
    # dedupe — this is calling existing machinery from a clock, not new
    # machinery.
    try:
        _dispatch_board_pending_reviews(config)
    except Exception:  # noqa: BLE001
        log.exception("notify drain: review dispatch failed")

    # Step 5: post findings for done-review assignments that were never
    # processed (agent reported 'cancelled', a human marked the row done, or
    # notify ran at the wrong time).  Comment + verdict capture only.
    orphaned: list[str] = []
    try:
        orphaned = post_orphaned_review_findings(config) or []
    except Exception:  # noqa: BLE001
        log.exception("notify drain: post_orphaned_review_findings failed")

    # Step 6 (#1663): propagate each captured verdict onto its parent WORK row.
    #
    # Steps 1 and 5 both stamp the verdict on the *review* row and stop there.
    # Everything that reads the *work* row — `coord drive`, the TUI's Review
    # stage, `_stalled_pipeline`, any state-derived recovery — therefore saw
    # `review_state='dispatched'` / `review_verdict=NULL` for every verdict the
    # daemon consumed instead of a human's `coord notify`.  The 2026-08-01
    # overnight batch is the receipt: five issues reviewed, four clean approves,
    # not one reached its work row, 4h02m of wall clock and zero merges.
    #
    # This is the bookkeeping half ONLY — `propagate_review_verdict_for_
    # transition` cannot reach `_dispatch_fix_for_review`, so the #476/#477
    # line (no metered fix worker from a clock) is exactly where it was.  The
    # exclusion used to sit at function granularity and took the parent-row
    # write down with the dispatch; it now sits at side-effect granularity,
    # which is where the table above always said it belonged.
    _propagated: list[str] = []
    if review_completions or orphaned:
        try:
            from coord.auto_loop import (  # noqa: PLC0415
                propagate_review_verdict_for_transition,
            )

            seen: set[str] = set()
            # Orphaned rows have no transition tuple (their comment was posted
            # on an earlier pass, or never).  `_load_review_findings` reads the
            # DB findings cache first — which step 5 just populated — so an
            # empty record/entry still resolves the verdict without any I/O.
            pending: list[tuple[str, dict, dict]] = [
                (t.assignment_id, record, entry)
                for t, record, entry in review_completions
            ] + [(aid, {"type": "review"}, {}) for aid in orphaned]

            for aid, record, entry in pending:
                if not aid or aid in seen:
                    continue
                seen.add(aid)
                try:
                    actions = propagate_review_verdict_for_transition(
                        aid, record, entry, config,
                    )
                except Exception:  # noqa: BLE001 — never sink the pass
                    log.exception(
                        "notify drain: verdict propagation failed for %s", aid,
                    )
                    continue
                for action in actions:
                    log.info(
                        "notify drain: verdict propagation %s: %s (assignment=%s)",
                        action.kind, action.detail, action.assignment_id,
                    )
                    if action.kind in (
                        "approved", "approved_with_nits", "verdict_propagated",
                        "terminal_skip",
                    ):
                        _propagated.append(aid)
        except Exception:  # noqa: BLE001
            log.exception("notify drain: verdict propagation loop failed")

    return DrainResult(
        transitions=posted,
        orphaned_findings=orphaned,
        propagated_verdicts=_propagated,
    )


def _roll_pending_blocks_new_dispatch(*, now: float | None = None) -> bool:
    """#2587: is a fleet roll pending right now, in a way that should stop
    `run()` from dispatching any NEW leg this pass?

    Reads the SAME marker `coord.commands.drive_queue.drive_queue_tick`
    watches (`coord.commands.drive_queue.read_roll_pending`) — one marker,
    one meaning, read by both callers, per this codebase's "quiescence is
    the drive queue's, not a second opinion" rule
    (`coord/release_propagate.py`'s module docstring states the same
    principle for `assess_quiescence`).

    Deliberately READ-ONLY: an EXPIRED marker (`RollPending.expired`) still
    reads as blocking here rather than being cleared on the spot. Only the
    drive-queue tick clears a marker (loudly, via its own escalation) —
    letting `coord notify` also mutate it would be two independent processes
    racing to own the same piece of state, exactly the #1440 "two
    overseers" hazard this codebase's design notes warn against elsewhere.
    The tick's own cadence (`coord-drive-queue.timer`, ~3 minutes in
    production) bounds how long a truly-expired marker can keep blocking
    dispatch here before the tick catches up and clears it — nowhere near
    the unbounded 2026-08-22 incident this issue exists to end.

    Fail-soft on a read that cannot make sense of the marker file — see
    `coord.commands.drive_queue.read_roll_pending`'s own docstring; the same
    "an unreadable/corrupt marker reads as no marker" posture applies here,
    so `coord notify` degrades to ITS pre-#2587 behaviour rather than a
    corrupt file silently blocking every future notify pass.
    """
    import time as _time  # noqa: PLC0415

    from coord.commands.drive_queue import read_roll_pending  # noqa: PLC0415

    pending = read_roll_pending()
    if pending is None:
        return False
    return not pending.expired(_time.time() if now is None else now)


def run(
    config: Config,
) -> tuple[
    list[Transition],
    list[StuckDetection],
    list[NeedsAttentionDetection],
    list[StalledDetection],
    list[LivenessStallDetection],
    list["PhantomRowHeal"],
    list["StuckTestStateHeal"],
]:
    """Detect and post all pending transitions, stuck signals, #846
    needs-attention detections, #1441 stalled-pipeline detections, #2048
    liveness-auditor stalls, #2536 phantom-row auto-heals, and #2803 stuck
    test_state='running' auto-heals.

    Also dispatches any pending reviews found on the saved board so that
    ``coord notify`` acts as a reliable review-dispatch trigger in addition
    to ``coord status --reconcile``.

    Returns (posted_transitions, posted_stuck, posted_needs_attention,
    posted_stalled, posted_liveness, posted_phantom_healed,
    posted_stuck_test_state_healed). Each of the liveness, phantom-healed
    and stuck-test-state-healed entries was appended rather than inserted,
    following #1441's own precedent: any existing caller unpacking a
    shorter tuple positionally breaks loudly, which is a good thing (it
    means the CLI/board/TUI surfacing was actually wired up, not silently
    skipped). ``posted_liveness`` is always ``[]`` when
    ``config.pipeline.liveness_auditor.enabled`` is ``False`` (the
    default); ``posted_phantom_healed`` is always ``[]`` when
    ``config.pipeline.auto_heal_phantom_rows`` is ``False`` (not the
    default — see that field's docstring); ``posted_stuck_test_state_healed``
    is always ``[]`` when ``config.pipeline.auto_heal_stuck_test_state`` is
    ``False`` (not the default — see that field's docstring).
    """
    # Refresh the agent-host cache so _try_parse_and_post_review (and any
    # other helper using _agent_host) can resolve hostnames without
    # threading config through every call.
    global _AGENT_HOSTS
    _AGENT_HOSTS = {m.name: m.host for m in config.machines}

    # #2464-review: open this pass's Test-verdict confirmation budget.  Same
    # reason as `_run_drain_locked` — this is the other entrypoint a pass can
    # come in through (the CLI, and the daemon's `/notify` handler, which
    # invokes the `coord notify` callback rather than `run_drain`).
    from coord.confirm_test import begin_confirmation_pass  # noqa: PLC0415

    begin_confirmation_pass()

    # #2587: while a fleet roll is pending (`coord release propagate`/
    # `nightly-window` armed the marker, waiting for the drive-queue tick's
    # next inter-drive gap), this pass must dispatch no NEW legs — smoke,
    # review, an auto-loop fix/re-review, or a stalled-pipeline action. This
    # is the exact gap the 2026-08-22 incident hit: a review for #2540 and a
    # work dispatch for #2541 both landed within the drain's first minute,
    # because nothing had ever told `coord notify` a drain was in progress.
    # Detecting/posting completion, stuck, needs-attention, and liveness
    # signals below is UNAFFECTED — those advance legs already in flight and
    # are exactly what lets the queue keep draining toward the gap the tick
    # is waiting for; see `_roll_pending_blocks_new_dispatch`'s docstring for
    # why this reads the marker but never writes it.
    _roll_pending = _roll_pending_blocks_new_dispatch()

    # Step 0 (#2975): same head start `_run_drain_locked` takes, and for the
    # identical reason — dispatch pending Test-stage smoke / PR-opens /
    # reviews from the board exactly as it reads RIGHT NOW, before the
    # transition-detection loop below gets anywhere near a `confirm_branch`
    # call that can hold `notify.lock` for up to `CONFIRM_PASS_BUDGET_
    # SECONDS`. Gated on `_roll_pending` like every other NEW-leg dispatch in
    # this function (#2587) — the later calls to the same three functions,
    # a few dozen lines down, repeat this after the loop below has had a
    # chance to add anything newly eligible; this is a head start, not a
    # replacement, and all three are idempotent so calling each twice costs
    # only a second, mostly-empty scan.
    if not _roll_pending:
        try:
            _dispatch_board_pending_pr_opens(config)
        except Exception:  # noqa: BLE001
            pass
        try:
            _dispatch_board_pending_smoke(config)
        except Exception:  # noqa: BLE001
            pass
        try:
            _dispatch_board_pending_reviews(config)
        except Exception:  # noqa: BLE001
            pass

    # #522: one terminal-state cache shared across every gh-hitting check in
    # this notify run (the auto-loop review/fix dispatches below, and the
    # #1441 stalled-pipeline sweep at the end), so a burst of activity for
    # the same merged/closed issue (the #349 ×4 case) costs a single `gh`
    # round-trip, not one per caller.
    terminal_cache: dict = {}

    # Collect (transition, record, entry) tuples for review completions so we
    # can feed them to the auto-loop after all notifications are posted.
    review_completions: list[tuple[Transition, dict, dict]] = []
    # Collect (transition, record) tuples for completed fix workers so we can
    # dispatch a fresh review against each one after notifications are posted.
    fix_completions: list[tuple[Transition, dict]] = []

    posted: list[Transition] = []
    for transition, record, entry in detect_transitions(config):
        try:
            post_transition(transition, record, entry)
        except Exception:  # noqa: BLE001 — surface to caller; continue with rest
            continue
        posted.append(transition)
        # Track completed reviews for auto-loop processing below.
        from coord.comments import EVENT_COMPLETION  # noqa: PLC0415
        from coord.auto_loop import FIX_DISPATCH_TYPES  # noqa: PLC0415
        if (
            record.get("type") == "review"
            and transition.event == EVENT_COMPLETION
        ):
            review_completions.append((transition, record, entry))
        # Track completed fix workers (type in FIX_DISPATCH_TYPES,
        # review_of_assignment_id set, title starts with "[fix-") for
        # auto-loop re-review dispatch. #1176 review: this used to hardcode
        # type == "work", which meant a completed type="test-author" fix
        # (added by #1176 itself) never reached run_for_fix_transition —
        # the same class of bug as #1141 ("test-author was never added to
        # WORK_LIKE_TYPES"). FIX_DISPATCH_TYPES is the single source of
        # truth for what _dispatch_fix can emit, so a future fix-dispatch
        # type can't reintroduce this gap silently.
        elif (
            record.get("type") in FIX_DISPATCH_TYPES
            and transition.event == EVENT_COMPLETION
            and record.get("review_of_assignment_id")
            and (record.get("issue_title") or "").startswith("[fix-")
        ):
            fix_completions.append((transition, record))

    # Also detect and post stuck signals
    stuck_posted: list[StuckDetection] = []
    for detection, record in detect_stuck(config):
        try:
            post_stuck(detection, record)
        except Exception:  # noqa: BLE001
            continue
        stuck_posted.append(detection)

    # #846: coordinator backstop for long-running / non-converging
    # assignments. Best-effort, non-fatal — one bad record must not sink the
    # rest of the notify run.
    needs_attention_posted: list[NeedsAttentionDetection] = []
    try:
        for detection, record in detect_needs_attention(config):
            try:
                post_needs_attention(detection, record)
            except Exception:  # noqa: BLE001
                continue
            needs_attention_posted.append(detection)
    except Exception:  # noqa: BLE001
        log.exception("detect_needs_attention: unexpected error")

    # #2536: fleet-wide phantom-row self-heal — a `running` row whose
    # session its own recorded machine confirms is dead, aged well past its
    # own needs-attention threshold, gets the same non-destructive recovery
    # `coord diagnose --reset` performs by hand, automatically. Runs right
    # after the needs-attention scan above (same "long-running row" concern,
    # acted on rather than only narrated) and before the dispatch steps
    # below, so a slot this sweep frees is visible to them in the same pass.
    # Best-effort, non-fatal — mirrors every other sweep in this function.
    phantom_healed_posted: list["PhantomRowHeal"] = []
    try:
        phantom_healed_posted = _sweep_phantom_rows(config)
    except Exception:  # noqa: BLE001
        log.exception("_sweep_phantom_rows: unexpected error")

    # #2803: fleet-wide stuck test_state='running' watchdog — a work row
    # whose Test-stage child already reached a terminal status (or was never
    # created at all) but whose verdict never propagated to the parent, well
    # past the ordinary write-propagation lag. Runs right after the phantom-
    # row sweep above (same "long-running row this daemon can act on itself"
    # concern) and before the smoke dispatch below, so a row this sweep
    # clears is redispatched in this same pass. Best-effort, non-fatal —
    # mirrors every other sweep in this function.
    stuck_test_state_healed_posted: list["StuckTestStateHeal"] = []
    try:
        stuck_test_state_healed_posted = _sweep_stuck_test_state(config)
    except Exception:  # noqa: BLE001
        log.exception("_sweep_stuck_test_state: unexpected error")

    # Dispatch pending Test-stage smoke from the saved board (#1426;
    # best-effort, non-fatal). Runs BEFORE review dispatch to mirror the
    # pipeline's Work -> Test -> Review order, though ordering isn't load-
    # bearing here: dispatch_pending_reviews already holds review dispatch
    # until test_state is passed/skipped regardless of which runs first in
    # a given pass.
    #
    # #2587: both this and the review dispatch just below are NEW-leg
    # dispatch — skipped while `_roll_pending` is true, same posture the
    # drive-queue tick takes under a pending roll.
    if not _roll_pending:
        # #2844: open PRs for work-leg completions still missing one, before
        # smoke/review dispatch — see _dispatch_board_pending_pr_opens.
        try:
            _dispatch_board_pending_pr_opens(config)
        except Exception:  # noqa: BLE001
            pass

        try:
            _dispatch_board_pending_smoke(config)
        except Exception:  # noqa: BLE001
            pass

        # Dispatch pending reviews from the saved board (best-effort, non-fatal).
        try:
            _dispatch_board_pending_reviews(config)
        except Exception:  # noqa: BLE001
            pass

    # Post findings for done-review assignments that were never processed
    # (e.g. agent reported 'cancelled', user manually marked done, or notify
    # ran at the wrong time).  Best-effort, non-fatal.
    try:
        post_orphaned_review_findings(config)
    except Exception:  # noqa: BLE001
        log.exception("post_orphaned_review_findings: unexpected error")

    # Auto-loop: for each completed review, optionally dispatch a fix worker.
    # Runs after notify posts the completion comment so GitHub has the full
    # review body before any fix briefing references "previous findings".
    # #2587: a fix worker is a NEW leg — skipped while `_roll_pending` is
    # true. The review completion itself was still posted above (unaffected
    # — that's advancing an existing leg, not starting one).
    if review_completions and not _roll_pending:
        try:
            from coord.auto_loop import run_for_review_transition  # noqa: PLC0415
            for transition, record, entry in review_completions:
                try:
                    actions = run_for_review_transition(
                        transition.assignment_id, record, entry, config,
                        terminal_cache=terminal_cache,
                    )
                    for action in actions:
                        log.info(
                            "auto_loop %s: %s (assignment=%s)",
                            action.kind, action.detail, action.assignment_id,
                        )
                except Exception:  # noqa: BLE001
                    log.exception(
                        "auto_loop: error processing review %s",
                        transition.assignment_id,
                    )
        except Exception:  # noqa: BLE001
            log.exception("auto_loop: unexpected error in review completion loop")

    # Auto-loop: for each completed fix worker, dispatch a fresh review so
    # the review → fix → re-review cycle closes without manual coord pr invocations.
    # Runs after review_completions so a simultaneous review + fix completion
    # in the same notify run is handled review-first.
    # #2587: a fresh review is a NEW leg — skipped while `_roll_pending` is
    # true, same reasoning as the review-completion block above.
    if fix_completions and not _roll_pending:
        try:
            from coord.auto_loop import run_for_fix_transition  # noqa: PLC0415
            for transition, _record in fix_completions:
                try:
                    actions = run_for_fix_transition(
                        transition.assignment_id, config,
                        terminal_cache=terminal_cache,
                    )
                    for action in actions:
                        log.info(
                            "auto_loop fix_transition %s: %s (assignment=%s)",
                            action.kind, action.detail, action.assignment_id,
                        )
                except Exception:  # noqa: BLE001
                    log.exception(
                        "auto_loop: error processing fix completion %s",
                        transition.assignment_id,
                    )
        except Exception:  # noqa: BLE001
            log.exception("auto_loop: unexpected error in fix completion loop")

    # #1441/#1478: sweep for pipeline rows whose auto-loop transition
    # already fired once but which are now stuck on a precondition that
    # landed too late (the vimcode #602 reference case), post a diagnostic
    # (or, when `pipeline.auto_dispatch_stalled` is on, act). Runs last,
    # after the review/fix auto-loop above has had a chance to act on THIS
    # pass's transitions, so a row that just got a fresh fix/review
    # dispatched above is not also flagged as stalled in the same pass.
    # Best-effort, non-fatal — mirrors the #846 needs-attention block above;
    # the crucial difference from `reconcile()`-only sweepers (see
    # docs/OPERATING_GOTCHAS.md §7) is that this runs from `coord notify`.
    # #2587: `_sweep_stalled_pipeline` can itself dispatch (a fresh
    # review/fix/conflict-fix — `config.pipeline.auto_dispatch_stalled`),
    # not just report, so the whole sweep is skipped while `_roll_pending`
    # is true rather than threading the flag through its own dispatch
    # decision. The cost is one pass of stalled-pipeline diagnostics not
    # posted; bounded by the marker's own TTL (`RollPending.expired`), same
    # as every other #2587 deferral.
    stalled_posted: list[StalledDetection] = []
    if not _roll_pending:
        try:
            stalled_posted = _sweep_stalled_pipeline(config, terminal_cache=terminal_cache)
        except Exception:  # noqa: BLE001
            log.exception("detect_stalled_pipeline: unexpected error")

    # #2048: cheap per-turn liveness auditor. Best-effort, non-fatal —
    # mirrors the #846 needs-attention block above. Entirely a no-op
    # (returns [] immediately, no subprocess, no DB write) unless
    # config.pipeline.liveness_auditor.enabled is set.
    liveness_posted: list[LivenessStallDetection] = []
    try:
        for detection, record in detect_liveness_stall(config):
            try:
                post_liveness_stall(detection, record)
            except Exception:  # noqa: BLE001
                continue
            liveness_posted.append(detection)
    except Exception:  # noqa: BLE001
        log.exception("detect_liveness_stall: unexpected error")

    return (
        posted,
        stuck_posted,
        needs_attention_posted,
        stalled_posted,
        liveness_posted,
        phantom_healed_posted,
        stuck_test_state_healed_posted,
    )
