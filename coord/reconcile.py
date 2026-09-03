"""Reconcile the coordinator's board with live agent server state."""

from __future__ import annotations

import re
import time
import uuid

import httpx

from typing import TYPE_CHECKING

from coord.config import INTERACTIVE_SESSION_TYPES, Config
from coord.dispatch import AGENT_PORT
from coord.models import (
    WORK_LIKE_TYPES,
    Assignment,
    Board,
    Machine,
    trust_issue_closed_for,
)

if TYPE_CHECKING:
    from coord.merge_queue import QueuedMerge

# #2639: bounds the per-row file-content fetch count in sweep (h)'s
# falsely-merged audit (reconcile_board_merges) — a branch with a huge diff
# (e.g. a generated-file sweep) must not turn one row's audit into hundreds
# of `gh api contents` round-trips. The first differing file is enough to
# flag the row; this only limits how many files are checked before giving up
# and treating a huge, all-matching-so-far diff as inconclusive (fail open).
_FALSE_MERGE_AUDIT_MAX_FILES = 20

# #2989: bounds the OUTER candidate set of that same sweep — the loop #2639
# left unbounded. Without it the set is proportional to project history
# (1,302 rows on the drive host, +1 per merge, never shrinking), re-probed
# every 30s. This is a per-pass ceiling applied newest-first as a backstop
# behind the persistent terminal marker (`state.mark_false_merge_audit_
# clean`); the older tail drains over subsequent passes as newer rows are
# confirmed and drop out permanently. Not applied to a targeted
# `--issue N` audit.
_FALSE_MERGE_AUDIT_MAX_ROWS = 50

# #2989: how often the sweep may run when the caller opts into throttling
# (the daemon's reconcile tick does; a manual `coord reconcile-merges` does
# not). It detects a rare, non-urgent condition — a merged-marked row whose
# branch still differs from base — which is an hourly sweep, not a
# twice-a-minute one.
_FALSE_MERGE_AUDIT_MIN_INTERVAL_SECONDS = 3600.0


def _query_agent(host: str, port: int = AGENT_PORT, timeout: float = 5.0) -> dict | None:
    try:
        resp = httpx.get(f"http://{host}:{port}/status", timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, httpx.TimeoutException):
        return None


# Terminal statuses an agent reports in its /status `completed` history,
# mapped to the board terminal status we persist. (#625)
#
# #2234: "refused_policy" (coord.agent.REFUSED_POLICY) joins "advisory" as
# its own pass-through entry — WITHOUT this, `reconcile_completed_
# assignments` below (the daemon's passive tick, the primary production
# path a completion is first observed on) reads `terminal = _AGENT_TERMINAL_
# STATUS.get(...)` as `None` for an unrecognised status and just
# `continue`s, leaving the board row on `status="running"` FOREVER — the
# opposite of #2234's goal (the row would look perpetually in-flight,
# `active_count` would never clear, and `coord drive` would wait forever
# instead of ever reaching the terminal-status branch that avoids spending
# an attempt).
_AGENT_TERMINAL_STATUS = {
    "done": "done",
    "advisory": "advisory",
    "refused_policy": "refused_policy",
    "failed": "failed",
    "cancelled": "failed",
}


# ── #2275: the agent has no record of this assignment ──────────────────────
#
# Pinned reason string, written verbatim to `assignments.failure_reason` when
# the no-record arm below reconciles a row.  Asserted on by
# `tests/test_reconcile_no_agent_record.py` and (deliberately) the only thing
# a human or a downstream reader has to grep for to understand why a row went
# terminal without a worker verdict.
NO_AGENT_RECORD_REASON = (
    "agent has no record of this assignment (present in neither its /status "
    "`active` nor `completed` list) — the worker process is gone: the agent "
    "restarted with its state lost, the machine rebooted, or the completion "
    "rolled off the capped history before anything observed it (#2275)"
)

# Minimum age (seconds since `dispatched_at`) before the no-record arm will
# reconcile a row.
#
# The positive disproof — "in neither list" — is what makes this arm safe;
# this window is the belt to its braces, and exists for exactly one shape:
# `AgentServer.assign()` builds the worktree BEFORE taking `self._lock` to
# insert into `self._assignments` (see the identical race window called out in
# `clean_worktrees`).  Any dispatch path that learns an assignment id and
# writes a `running` board row without waiting for that insert would otherwise
# hand this arm a row the agent genuinely will know about a moment later.  Two
# minutes is orders of magnitude longer than that window and orders of
# magnitude shorter than the 11 hours #2208 burned, so it costs nothing real.
#
# It is NOT a restart grace window — see `reconcile_completed_assignments`'
# docstring for why an agent restart does not produce a mass reap.
_NO_RECORD_GRACE_SECONDS = 120.0

# #2547: substring common to both `NO_AGENT_RECORD_REASON` and
# `_no_agent_record_branch_reason`'s output — the only thing distinguishing a
# `_reconcile_no_agent_record` GUESS from any other way a row goes terminal
# (a real agent-reported completion, a `coord diagnose --reset`, ...). Used by
# `reconcile_late_agent_reports` below to find rows this arm guessed at,
# without a schema migration. If either reason string above changes, this
# must change with it.
_NO_AGENT_RECORD_GUESS_MARKER = (
    "agent has no record of this assignment (present in neither its"
)

# #2547: how long after a `_reconcile_no_agent_record` GUESS a late,
# authoritative agent completion report is still allowed to correct it.
# `_reap` itself can run for minutes after the worker subprocess exits (git
# push, cleanup, its own status classification) — long enough for the
# no-record arm's positive disproof ("in neither `active` nor `completed`")
# to catch the assignment mid-reap and guess wrong, exactly like
# coord-portal#129 / claude-coordinator#2547's assignment `c2120f7206ec`.
# Six hours is generous margin over any real reap duration while still being
# bounded — this correction pass must never become an unbounded full-history
# rescan.
_LATE_REPORT_CORRECTION_WINDOW_SECONDS = 6 * 3600.0

# The only statuses `reconcile_late_agent_reports` will ever correct a guess
# INTO. `done` is deliberately excluded — exactly like
# `_reconcile_no_agent_record` itself never writes `done` (see its
# docstring): promoting an already-terminal row straight to `done` here would
# skip every side effect a real completion normally goes through (the #1616
# notify drain, review-verdict capture, Test-stage propagation), which this
# passive correction pass has no business doing. A row whose real status
# turns out to be `done` is left on its (safe, conservative) `failed`/
# `advisory` guess — a human or `coord notify` can still promote it.
_LATE_REPORT_SAFE_TARGETS = frozenset({"failed", "advisory", "refused_policy"})


def is_attended_session(a: object) -> bool:
    """True when *a* is a human-attended session, not an agent subprocess (#2275).

    The exclusion that keeps the no-record arm in
    :func:`reconcile_completed_assignments` from reaping live work.  An
    attended session is launched into tmux/a PTY by ``coord assign
    --interactive`` — it is not an agent subprocess, so it appears in NEITHER
    the agent's ``active`` list NOR its ``completed`` list, for its whole
    life.  Without this check the no-record arm would reap every attended
    session the moment it started, which is the #1658 failure mode (reaping
    live headless workers) pointed at a human's own terminal.

    Two independent discriminators, either of which is sufficient — this is a
    reaping path, so it is deliberately over-inclusive:

    * ``provider_name == "claude-pty"`` — stamped by every ``--interactive``
      dispatch (the same discriminator :func:`is_interactive_merge_session`
      and :meth:`coord.config.PipelineConfig.attention_threshold_for` use).
      This is the load-bearing one: an interactive ``--fix-of``/
      ``--review-of``/``--smoke-of`` session shares its ``type`` with a
      headless counterpart, so only ``provider_name`` tells them apart.
    * ``type in`` :data:`coord.config.INTERACTIVE_SESSION_TYPES` — the
      chat/troubleshoot/audit/refinement family, which has no headless
      counterpart at all and never goes near an agent.

    Dead attended sessions are reaped by their own path
    (:func:`coord.interactive.reap_stale_interactive_sessions`, called from
    :func:`reconcile`), which probes the actual tmux server rather than
    inferring death from an agent's silence.  That is the correct instrument
    for them; this one must stay out of the way.
    """
    if getattr(a, "provider_name", None) == "claude-pty":
        return True
    return getattr(a, "type", None) in INTERACTIVE_SESSION_TYPES


def agent_has_no_record(status: dict, assignment_id: str) -> bool:
    """True when *status* positively shows the agent knows nothing of *assignment_id*.

    #2275's whole point: ``/status`` carries ``active`` (everything the agent
    has a live subprocess for) and ``completed`` (its capped terminal
    history).  An id in NEITHER is a **positive statement** about the agent's
    state — "no record" — not an inference from silence, which is what the
    old ``entry is None → continue`` conflated with "still running".

    Fail-open on a payload that doesn't carry both keys.  A response missing
    ``active`` cannot support the disproof (we would be inferring from
    absence again, exactly the bug), so it is treated as "cannot tell" and
    the caller leaves the row alone.  This also means an older agent build,
    or any future ``/status`` variant, degrades to today's behaviour rather
    than to a reap.
    """
    if not isinstance(status, dict):
        return False
    active = status.get("active")
    completed = status.get("completed")
    if not isinstance(active, list) or not isinstance(completed, list):
        return False
    for entries in (active, completed):
        for e in entries:
            if isinstance(e, dict) and e.get("id") == assignment_id:
                return False
    return True


def _no_record_grace_elapsed(a: Assignment, *, now: float | None = None) -> bool:
    """True when *a* is old enough for the no-record arm (:data:`_NO_RECORD_GRACE_SECONDS`).

    A row with no ``dispatched_at`` at all is eligible: the grace window
    guards a dispatch-time race, and a row that never recorded a dispatch
    time is not in one.
    """
    dispatched_at = getattr(a, "dispatched_at", None)
    if not dispatched_at:
        return True
    try:
        dispatched = float(dispatched_at)
    except (TypeError, ValueError):
        return True
    return (time.time() if now is None else now) - dispatched >= _NO_RECORD_GRACE_SECONDS


def effective_agent_status(entry: dict) -> str:
    """The agent-reported status for *entry*, with #1534's ``done`` refusal.

    An agent ``completed`` entry that reports ``status="done"`` while ALSO
    carrying a ``usage_limit_reason`` is self-contradictory: the worker was
    killed mid-task by the account's Claude session/weekly usage limit (its
    transcript ends on "You've hit your session limit · resets <time>"), so
    whatever it did or didn't push, it did not *finish*.  Recording that as
    ``done`` is the silent corruption #1534 was filed for — it burns money and
    reports success, and every downstream gate (review dispatch, the
    acceptance gate, the Pipeline view) then behaves as if the slice exists.

    The agent-side reap (``AgentServer._reap``) already refuses this at the
    source as of #1534, but that fix only reaches the fleet after a PyPI
    release and ``coord agent update``.  This is the coordinator-side backstop
    for the interim — and permanently, for agents pinned to an older build.

    Downgraded to ``failed`` rather than ``advisory`` for the same reason
    ``_record_usage_limit_reason`` normalises to ``failed``: a usage-limit kill
    is the one terminal state known safe to re-dispatch unchanged once the
    window resets, whereas ``advisory`` means "a human needs to look".

    Any truthy ``usage_limit_reason`` counts — not just one matching
    :func:`coord.worker_events.is_usage_limit_reason`'s prefix — because
    refusing a ``done`` is the fail-safe direction and the field is only ever
    written by the kill detector.
    """
    raw = (entry.get("status") or "").lower()
    if raw == "done" and entry.get("usage_limit_reason"):
        return "failed"
    return raw


def reconcile_completed_assignments(
    config: Config,
    *,
    board: Board | None = None,
    agent_status_fn=_query_agent,
    update_state_fn=None,
    capture_plan: bool = True,
    commits_ahead_fn=None,
) -> list[dict]:
    """Dispatch-free passive completion reconcile (#625).

    Poll the agent of every RUNNING board assignment; for any the agent
    reports terminal in its ``/status`` ``completed`` history, write the
    terminal status + ``finished_at`` to the board via the issue_store seam
    and (best-effort) capture a plan's structured output.  This reflects a
    headless worker's already-finished state so the board — and the TUI box
    colour — stops lying when the auto-loop (the only other thing that polled
    agents) is turned off.

    Deliberately minimal — it is the WHOLE point of #625 that reflecting a
    termination is *passive* state, decoupled from auto-dispatch so it can
    never re-introduce the dispatch flood:

    * NEVER dispatches work/review.
    * NEVER posts a GitHub comment.
    * Only acts on ``status == "running"`` rows, so it is idempotent — once a
      row is flipped terminal a later tick skips it.

    **#1616 — what runs the side effects this function refuses to (CONTRACT
    CHANGE, read this before adding anything here).**  This function's scope is
    unchanged and must stay unchanged; what changed is *who runs the rest*.
    Until #1616 the parenthetical here read "the single completion/plan comment
    is left to an explicit ``coord notify``" — and on a fleet where
    ``coord-notify.timer`` is deliberately disabled, "an explicit ``coord
    notify``" meant *a live* ``coord drive``'s **stall nudge**, and nothing
    else.  So this function advancing a row to ``done`` was the LAST thing that
    happened to it for as long as 47 minutes (#1122), or forever when no drive
    was running at all (vimcode#611/#613).  ``status`` said done; ``finished_at``
    was NULL, no comment was posted, no review was dispatched, and every surface
    rendered the stage as complete.

    The fix was **not** to widen this function.  The daemon ``_tick_loop`` now
    calls :func:`coord.serve_app._notify_drain_tick` →
    :func:`coord.notify.run_drain` as a SIBLING step immediately after this one,
    which posts the comment, stamps ``finished_at``, backfills the #1076/#1152
    test gate, and dispatches Test + Review under ``~/.coord/notify.lock``.
    That is where pipeline-advancing behaviour belongs; this stays passive.

    This is the failure shape ``docs/OPERATING_GOTCHAS.md`` §7 already names —
    "``reconcile()`` accretes behaviour the automatic drivers never invoke".
    If you are about to add a side effect here because "nothing else runs it",
    that is the bug, not the fix: add it to ``run_drain`` instead, and decide
    deliberately whether it belongs on the daemon's clock at all (work dispatch
    and fix-round dispatch explicitly do NOT — see that docstring).

    One consequence worth knowing when reading a board: a ``type="review"``
    ``done`` is downgraded to ``"finalizing"`` below and only leaves that state
    when the drain captures the verdict.  Pre-#1616 that window was unbounded
    (#1610); it is now bounded by ``COORD_NOTIFY_DRAIN_INTERVAL`` (default 60s).

    Interactive sessions are tmux launches, not agent subprocesses, so they
    never appear in the agent's ``completed`` list — a live attended session
    can't be reaped by this path.  #2275 made that a CHECKED property rather
    than an emergent one: see :func:`is_attended_session`.

    **#2275 — the second reconcile arm: "the agent has no record of it".**
    Until #2275 this function only ever acted on a row it found in the agent's
    ``completed`` history.  Anything else hit a bare ``continue`` whose comment
    read *"still active on the agent (or rolled off history) → leave it"* —
    two cases with opposite correct handling, conflated, both left.  That is
    not a race: ``_COMPLETED_HISTORY_CAP`` is 25, so a leg that dies needs only
    25 further completions on its machine before its id is gone from
    ``/status``, after which nothing on the daemon's 30s clock would ever look
    at that row again.  It cost claude-coordinator#2208 8 hours and both drive
    attempts on an already-green branch; a human running ``coord status``
    cleared it, and the branch was merge-READY 14 minutes later.

    The fix is a positive disproof: ``/status`` carries both ``active`` and
    ``completed``, and an id in NEITHER is a statement about the agent's state,
    not an inference from its silence.  Those — and only those — are
    reconciled, never to ``done``, with :data:`NO_AGENT_RECORD_REASON`.  See
    :func:`agent_has_no_record` and :func:`_reconcile_no_agent_record`.

    **#2553 — "the agent forgot" is not "the work is gone".** The arm above
    used to record ``failed`` unconditionally, but "the agent has no record"
    is evidence about the agent's memory, not about the work: a
    :data:`~coord.models.WORK_LIKE_TYPES` row's own ``branch`` is the one
    artifact in this path that survives an agent restart, a machine reboot,
    and the 25-entry completion-history cap, and the board row already has
    its name. :func:`_reconcile_no_agent_record` now consults it
    (``commits_ahead_fn``, defaulting to :func:`coord.github_ops.
    branch_commits_ahead_for_assignment`) before choosing ``failed`` — a
    branch with real commits ahead of its base lands on ``advisory`` instead
    (work exists, provenance unverified, needs a human or a verify pass),
    naming the branch in the reason so an operator doesn't have to go
    spelunking for it.

    Three guards, because **this arm reaps** and #1658 is what getting that
    wrong looks like in production:

    1. :func:`is_attended_session` — attended sessions appear in neither list
       for their whole life.  Reaping them is the regression that costs real
       work; :func:`coord.interactive.reap_stale_interactive_sessions` owns
       them, and it probes tmux rather than guessing.
    2. An unreachable agent still short-circuits at ``if not status`` above,
       unweakened.  **No record and no answer are different things.**
    3. :data:`_NO_RECORD_GRACE_SECONDS` since ``dispatched_at``.

    On **agent restart** (the ``coord agent update`` roll) this is deliberately
    NOT a mass reap, and not because of the grace window: ``AgentServer.
    _load_state`` restores the persisted assignments and rewrites every
    ``pending``/``running`` one to ``failed`` with *"agent restarted;
    subprocess lost"* before the first ``/status`` is served.  Those rows are
    therefore IN ``completed``, and the ordinary path above handles them with a
    real reason.  Genuinely-empty ``active`` + ``completed`` means the agent
    lost its state file too (fresh install, corrupt state moved aside by
    #1421's handler) — and there the workers really are gone, so reconciling
    every row on that machine is the correct answer rather than an accident.

    ``commits_ahead_fn`` (#2553) is the injectable seam for the branch check
    above — ``fn(assignment, config) -> int | None`` — defaulting to
    :func:`coord.github_ops.branch_commits_ahead_for_assignment`.  Tests stub
    it to avoid a real ``gh`` call, exactly like ``agent_status_fn`` stubs the
    real agent poll.

    Returns one dict per reconciled assignment (empty when nothing changed).
    """
    if update_state_fn is None:
        from coord.issue_store import _update_local_state  # noqa: PLC0415

        update_state_fn = _update_local_state

    if commits_ahead_fn is None:
        from coord import github_ops  # noqa: PLC0415

        commits_ahead_fn = github_ops.branch_commits_ahead_for_assignment

    if board is None:
        from coord.state import build_board  # noqa: PLC0415

        board = build_board()

    running = [a for a in board.active if a.status == "running"]
    if not running:
        return []

    hosts = {m.name: m.host for m in config.machines}
    status_by_host: dict[str, dict | None] = {}  # poll each agent at most once
    reconciled: list[dict] = []

    for a in running:
        aid = a.assignment_id
        if not aid:
            continue
        host = hosts.get(a.machine_name)
        if not host:
            continue
        if host not in status_by_host:
            status_by_host[host] = agent_status_fn(host)
        status = status_by_host[host]
        if not status:
            continue  # agent unreachable → leave the row, retry next tick
        entry = next(
            (e for e in status.get("completed", []) if e.get("id") == aid),
            None,
        )
        if entry is None:
            # #2275: NOT in `completed` has two causes with OPPOSITE correct
            # handling — "still running on the agent" (leave it) and "the
            # agent has no record of it at all" (reconcile it).  This used to
            # be a bare `continue` that named both cases in a comment and
            # then left both, so a row in the second case was re-skipped every
            # 30s forever while `status` read `running` (#2208: 8 hours and
            # both drive attempts burned on an already-green branch).
            # `agent_has_no_record` tells them apart from the `active` list —
            # a positive disproof, not an inference from silence.
            if (
                agent_has_no_record(status, aid)
                and not is_attended_session(a)
                and _no_record_grace_elapsed(a)
            ):
                _reconcile_no_agent_record(
                    a, aid, update_state_fn, reconciled,
                    config=config, commits_ahead_fn=commits_ahead_fn,
                )
            continue
        # #1534: `effective_agent_status` refuses an agent-reported `done`
        # that also carries a usage-limit-kill reason — the daemon's passive
        # tick is the FIRST place most completions are observed, so without
        # this the corrupt `done` is persisted here before any other path
        # gets a chance to look at it.
        terminal = _AGENT_TERMINAL_STATUS.get(effective_agent_status(entry))
        if terminal is None:
            continue
        # #1566: a review agent reporting `done` has only finished the LLM
        # session — the verdict itself is parsed + persisted by `coord
        # notify` (`_try_parse_and_post_review`), a separate, slower step
        # that can run minutes after this tick observes the completion (this
        # passive tick runs on a short ~30s cadence; `coord notify` runs on
        # whatever cadence its caller configures). Writing `status="done"`
        # straight away leaves a window where the board shows a finished
        # review with `review_verdict IS NULL` — indistinguishable from the
        # verdict having been dropped (the #1346/#1348/#1563 failure mode).
        # `finalizing` closes that window: it reads as "still wrapping up"
        # (not in `drive_state.TERMINAL_STATUSES`, so `coord drive` correctly
        # waits rather than declaring a dead end) until `coord notify`'s own
        # `mark_notified` advances it to the real `done` alongside the
        # verdict. Other terminal outcomes (`failed`, `advisory`) never go
        # through that verdict-capture step, so they are unaffected.
        if terminal == "done" and a.type == "review":
            terminal = "finalizing"

        # #1083: prefer the board's already-known branch, but fall back to
        # the agent's live ``completed`` entry (populated by AgentServer._reap
        # from the worktree's checked-out HEAD — see agent.py) when the board
        # doesn't have one yet. This is almost always the FIRST place a
        # freshly-completed assignment is observed (the daemon runs this tick
        # on a short interval, well ahead of any human-triggered `coord
        # notify`), so passing the board's stale (usually still-None) branch
        # here — as this used to do unconditionally — let status flip to
        # "done" with branch left NULL. For `type="work"` that NULL branch is
        # later patched by the #611 remote-branch-listing backfill sweep in
        # `reconcile()`, but that sweep is scoped to `type="work"` only, so
        # every other write-capable type (mock-author, test-author, ...) had
        # no path back to a correct branch once this tick got there first.
        # #1461: stamp a usage-limit-kill diagnostic onto the board row when
        # the agent's own reap flagged one (AgentServer._reap, agent.py) —
        # regardless of whether the agent landed on FAILED or ADVISORY, both
        # observed for a real kill. This is the primary production path
        # (the daemon's passive tick) for getting the reason out of the
        # ephemeral agent-side JSON and into the persisted, drive.py-visible
        # `failure_reason` column.
        # #1584: `api_error_reason` (a terminal `is_error: true` result event
        # — e.g. "529 Overloaded") is the SAME `failure_reason` column,
        # stamped by `AgentServer._reap` exactly like `usage_limit_reason`
        # (see `coord.agent.AgentAssignment`, both surfaced on the same
        # `/status` `completed` entry via `to_status_dict`'s `asdict`). The
        # two are mutually exclusive by construction — a usage-limit kill is
        # detected from a TRUNCATED log with no terminal `result` event,
        # while an API-error is read OFF that terminal `result` event — so
        # this `or` never picks the wrong one.
        # #1797: `push_failure_reason` is the SAME column too — stamped by
        # `AgentServer._reap` when the reap-time safety-net push hits an
        # auth-shaped rejection (see `_is_auth_push_failure`). It never
        # coexists with the other two either: it is only ever set on a
        # clean `exit_code == 0` reap, which both `usage_limit_reason` and
        # `api_error_reason` preempt before the push-failure branch even
        # runs (see the `elif` chain in `AgentServer._reap`). Without this,
        # a `type="work"` auth-push failure lands FAILED with no
        # `failure_reason` at all — invisible to `coord status`, the TUI,
        # and drive.py, which is the exact visibility gap #1797 exists to
        # close.
        # #2131: `spend_ceiling_reason` is the SAME column again — stamped by
        # `AgentServer._reap` when the per-leg spend ceiling killed the leg.
        # It cannot coexist with the other three: the ceiling kill returns a
        # non-zero `SPEND_CEILING_EXIT`, which rules out the `exit_code == 0`
        # push-failure branch, and it fires only while the transcript has NO
        # terminal `result` event (so no `api_error_reason`) on a leg that
        # was killed for spending, not for hitting a usage limit. Stamping it
        # here is what makes the kill distinguishable from a crash for
        # `coord retry`, the auto-reassign skip below, and the escalation.
        # #2316: `truncation_reason` is the SAME column again — stamped by
        # `AgentServer._reap` when a 0-commit clean exit's `stop_reason`
        # shows the worker was cut off by its output-token ceiling
        # (`coord.agent._TRUNCATION_STOP_REASONS`) rather than genuinely
        # finding nothing to do. Never coexists with the other three: it is
        # only ever set on the SAME `_ahead == 0` branch as the #448 ADVISORY
        # default (see `AgentServer._reap`'s `elif` chain), which the other
        # three all preempt before that branch even runs. Without this, a
        # truncated run lands FAILED with no `failure_reason` recorded —
        # exactly the "advisory looks the same as this" gap #2316 exists to
        # close.
        # #2638: `runtime_ceiling_reason`/`host_sleep_reason` are the SAME
        # column again — stamped by `AgentServer._reap` when the wall-clock
        # runtime ceiling or host-sleep detector killed the leg (see
        # `coord.agent.RUNTIME_CEILING_EXIT`/`HOST_SLEEP_EXIT`). Never
        # coexist with each other (mutually exclusive exit codes) or with the
        # other five: both are non-zero exits that preempt the push-failure
        # and truncation branches, and neither is a terminal `result` event
        # so `api_error_reason`/`usage_limit_reason` never fire alongside
        # them either. Without this, a suspended-host kill lands FAILED with
        # no `failure_reason` — invisible to `coord status`/`coord health`,
        # `coord retry`, and the GitHub failure comment, which is the exact
        # "nothing said the word asleep" gap #2638 exists to close.
        _failure_reason = (
            entry.get("usage_limit_reason")
            or entry.get("api_error_reason")
            or entry.get("push_failure_reason")
            or entry.get("spend_ceiling_reason")
            or entry.get("truncation_reason")
            or entry.get("runtime_ceiling_reason")
            or entry.get("host_sleep_reason")
        )
        _escalate_spend_ceiling_best_effort(a, entry)
        update_state_fn(
            assignment_id=aid,
            terminal_status=terminal,
            branch=a.branch or entry.get("branch"),
            review_state=None,
            failure_reason=_failure_reason,
            # #1605: the reap already computed the exit code (AgentServer._reap,
            # agent.py) and it rides on this same `/status` `completed` entry —
            # nothing downstream of THIS write path ever persisted it, so a
            # failed Test-stage row was undiagnosable from the board (both
            # `failure_reason` AND `exit_code` null) even when the reap knew
            # exactly why it died.
            exit_code=entry.get("exit_code"),
        )

        # #1605: a `type="smoke"` (Test-stage) assignment reaching a terminal
        # FAILED status must resolve the PARENT work row's `test_state` —
        # never leave it `running` forever. `running` is a documented
        # transient non-verdict marker (#1395) that every gate treats as "no
        # verdict yet", so a stranded child leaves the issue permanently
        # unresolvable and invisible to every instrument except a worker
        # transcript. See `propagate_smoke_terminal_failure` for the
        # environmental-vs-work classification (#1590) that decides whether
        # this clears the verdict for a fresh auto-dispatch or records a real
        # test failure.
        if a.type == "smoke" and terminal == "failed":
            propagate_smoke_terminal_failure(
                parent_assignment_id=a.review_of_assignment_id,
                failure_reason=_failure_reason,
            )

        # #666 Gap A: best-effort cost/token capture from the agent completed
        # entry.  Must never raise — a tick crash breaks the daemon.
        _capture_cost_from_entry_best_effort(aid, entry)

        plan_captured = (
            _capture_plan_best_effort(host, aid)
            if capture_plan and a.type == "plan"
            else False
        )

        # #667: capture token counts from the /status entry (the agent now
        # includes them there after parsing its own log).  Best-effort — any
        # failure is swallowed so it can't break the reconcile.
        _capture_tokens_best_effort(aid, entry)

        # #2316: capture the raw stop_reason from the /status entry for
        # EVERY terminal assignment (not just failed ones) — the enabling
        # persistence step; `_failure_reason` above already carries the
        # human-readable classification for a truncated run specifically.
        # Best-effort — any failure is swallowed so it can't break the
        # reconcile.
        _capture_stop_reason_best_effort(aid, entry)

        reconciled.append(
            {
                "assignment_id": aid,
                "issue_number": a.issue_number,
                "repo": a.repo_name,
                "type": a.type,
                "to_status": terminal,
                "plan_captured": plan_captured,
            }
        )

    return reconciled


def _no_agent_record_branch_reason(branch: str, ahead: int) -> str:
    """#2553's operator-facing reason for the "work is on the branch" half.

    Distinct from :data:`NO_AGENT_RECORD_REASON` (the "nothing was pushed"
    half) precisely so the two are told apart at a glance — the acceptance
    bar for #2553 is that the reason NAMES the branch rather than making an
    operator go looking for it, since that branch is the whole point: it is
    the one artifact of this leg that survived the agent's amnesia.
    """
    return (
        "agent has no record of this assignment (present in neither its "
        "/status `active` nor `completed` list), but its branch "
        f"`{branch}` has {ahead} commit(s) ahead of its base — the worker "
        "pushed real work before the agent's record of it was lost. "
        "Provenance is unverified (no worker ever reported a verdict), so "
        "this is recorded as advisory rather than failed: it needs a human "
        "or a verify pass, not to be discarded (#2553)"
    )


def _reconcile_no_agent_record(
    a: Assignment,
    aid: str,
    update_state_fn,
    reconciled: list[dict],
    *,
    config: Config,
    commits_ahead_fn,
) -> None:
    """#2275: flip a ``running`` row the agent has no record of to a terminal
    status — ``failed`` by default, or ``advisory`` when #2553's branch check
    below finds the row's own branch outlived the agent's amnesia.

    Called from :func:`reconcile_completed_assignments` only, and only once
    all three guards there have passed (positive disproof, not attended, past
    the grace window).

    **Never ``done``.** This leg never reported a verdict, so the one thing
    that must not happen — regardless of which terminal status below fires —
    is a silent flip to ``done``: that manufactures a pass and every
    downstream gate (review dispatch, the acceptance gate, the merge queue)
    then behaves as if a slice exists that nobody ever produced.  A stall is
    bad; a manufactured pass is worse.

    **#2553 — "the agent forgot" is evidence about the agent, not the work.**
    Before this, every row reaching here was unconditionally recorded
    ``failed`` with :data:`NO_AGENT_RECORD_REASON`, even when the row's own
    ``branch`` carried real, pushed commits — stranding that work with no
    second chance (most visible on ``test-author``, whose sealed-path rule
    means nothing else will ever pick the branch back up; see
    claude-coordinator#2553, ``coord-portal``#129 assignment
    ``c2120f7206ec``). The branch is the one artifact in this whole path
    that survives an agent restart, a machine reboot, and the 25-entry
    completion-history cap, and the board row already names it — so this now
    asks it directly via ``commits_ahead_fn(a, config)`` before choosing:

    * Only for :data:`~coord.models.WORK_LIKE_TYPES` (``work``,
      ``mock-author``, ``test-author``) — the types whose ``a.branch`` holds
      commits THIS row itself produced.  A ``smoke``/``review`` row's
      ``branch`` is inherited verbatim from the work it rides on (e.g.
      ``coord.smoke.dispatch_pending_smoke`` stamps
      ``branch=completed.branch``), so "commits ahead of base" there would
      just be re-discovering the PARENT's diff, not anything this leg
      produced — checking it would misfire the #2208 smoke path (a dead Test
      leg on an already-green branch) into ``advisory`` instead of the
      environmental clear it needs.
    * A positive, known commit count (``ahead`` a plain ``int > 0``) is the
      only thing that moves the needle, to ``advisory`` with
      :func:`_no_agent_record_branch_reason` (which names the branch).
      Anything else — no branch, a non-``WORK_LIKE_TYPES`` row, ``ahead ==
      0``, or ``ahead is None`` (unknown: the repo isn't in ``config``, the
      remote call failed, ...) — keeps the pre-#2553 behaviour: ``failed``
      with :data:`NO_AGENT_RECORD_REASON`.  Unknown fails CLOSED toward
      ``failed`` rather than guessing ``advisory``, matching this arm's
      existing polarity: a stall is recoverable, a manufactured verdict of
      either kind is not the thing to guess at.
    * ``commits_ahead_fn`` is called defensively — a network hiccup querying
      the remote must not crash the passive daemon tick any more than an
      unreachable agent does elsewhere in this module.

    ``exit_code`` is deliberately left ``None`` in both cases: there is no
    reap, so there is no exit code, and writing a fake one would be the same
    class of lie as writing ``done``.

    Kept passive, per this module's #1616 contract — it writes state through
    the same ``update_state_fn`` seam and dispatches nothing.  The commits-
    ahead check is a read, not a mutation, and it decides between two
    non-dispatching terminal states — it does not advance the pipeline any
    more than the write itself does.  The #1605 Test-stage propagation below
    is not a new side effect either: it is the same call the ordinary
    ``terminal == "failed"`` path already makes, and skipping it would leave
    the parent work row's ``test_state="running"`` forever — i.e. it would
    fix half of #2208 and leave the other half stranded.
    """
    terminal_status = "failed"
    reason = NO_AGENT_RECORD_REASON
    branch = (a.branch or "").strip()

    if branch and a.type in WORK_LIKE_TYPES:
        try:
            ahead = commits_ahead_fn(a, config)
        except Exception:  # noqa: BLE001 — best-effort; unknown → failed below
            ahead = None
        if isinstance(ahead, int) and not isinstance(ahead, bool) and ahead > 0:
            terminal_status = "advisory"
            reason = _no_agent_record_branch_reason(branch, ahead)

    update_state_fn(
        assignment_id=aid,
        terminal_status=terminal_status,
        branch=a.branch,
        review_state=None,
        failure_reason=reason,
        exit_code=None,
    )

    if a.type == "smoke" and terminal_status == "failed":
        # #2275: environmental by construction, so state it rather than
        # letting `classify_failure` guess.  `NO_AGENT_RECORD_REASON` is
        # coordinator-authored prose with no wire token in it, so the
        # classifier would (correctly, by its own "default to work" rule)
        # call it a WORK failure and record `test_state="failed"` — which
        # spends a bounded `coord fix` round chasing a code defect that never
        # existed, on a branch that in #2208's case was already green.  A
        # vanished worker is the machine's fault, not the work's; clearing
        # the verdict lets `dispatch_pending_smoke` re-run the Test stage on
        # its next tick, which is exactly what a human did in 14 minutes.
        # (`terminal_status` is always "failed" for `smoke` in practice — the
        # branch check above only ever fires for WORK_LIKE_TYPES — but the
        # guard keeps this correct even if that scoping ever changes.)
        propagate_smoke_terminal_failure(
            parent_assignment_id=a.review_of_assignment_id,
            failure_reason=reason,
            environmental=True,
        )

    reconciled.append(
        {
            "assignment_id": aid,
            "issue_number": a.issue_number,
            "repo": a.repo_name,
            "type": a.type,
            "to_status": terminal_status,
            "plan_captured": False,
            "reason": reason,
        }
    )


def reconcile_late_agent_reports(
    config: Config,
    *,
    board: Board | None = None,
    agent_status_fn=_query_agent,
    update_state_fn=None,
    now: float | None = None,
) -> list[dict]:
    """#2547: let a late-arriving, authoritative agent completion correct a
    stale :func:`_reconcile_no_agent_record` GUESS.

    ``_reconcile_no_agent_record`` fires on a positive disproof — the
    assignment id is in NEITHER the agent's ``active`` nor its ``completed``
    list — but that disproof has a real gap: ``AgentServer._reap`` runs as a
    background thread AFTER the worker subprocess exits (push, cleanup,
    classifying the terminal status) and only appends to ``completed`` once
    it finishes. An assignment mid-reap is, for that window, in neither
    list — exactly the shape the no-record arm's grace period is meant to
    absorb, except the grace period is sized for a *dispatch-time* race
    (:data:`_NO_RECORD_GRACE_SECONDS`, 2 minutes), not a reap that can run for
    several minutes doing real network I/O.

    When that race is lost, the no-record arm guesses a terminal status from
    weaker evidence (a branch's commit count) than the reap's own, complete
    verdict — and because :func:`reconcile_completed_assignments` only ever
    looks at rows still ``status == "running"``, once the guess lands the row
    is terminal and NOTHING ever revisits it. The correct verdict can sit in
    the agent's own ``/status`` ``completed`` history — reachable, complete,
    machine-readable — for as long as it stays in the 25-entry cap, and
    coord's board would never look again. This is the "two subsystems reached
    two different terminal states and the wrong one won" gap
    claude-coordinator#2547 was filed for (coord-portal#129, assignment
    ``c2120f7206ec``: the agent's own reap correctly logged ``status set to
    advisory``, but the board was left on a guessed ``failed``).

    This is a SEPARATE, later pass rather than folded into the no-record arm
    above, because its input set is the opposite: rows already terminal
    (``failed``/``advisory``) whose ``failure_reason`` carries
    :data:`_NO_AGENT_RECORD_GUESS_MARKER` — the only rows this pass has any
    business touching, found without a schema migration.

    Bounded three ways, deliberately narrower than the sibling arm:

    1. **Age** — only rows still inside
       :data:`_LATE_REPORT_CORRECTION_WINDOW_SECONDS` of their own
       ``finished_at``. This is a correction window for a specific race, not
       a standing full-history rescan.
    2. **Target status** — only ever corrects INTO
       :data:`_LATE_REPORT_SAFE_TARGETS` (never ``done``), for the same
       reason the sibling arm never writes ``done``: promoting straight to
       ``done`` here would skip the #1616 notify drain and every side effect
       a real completion goes through. A row whose real status is ``done``
       stays on its safer guess; a human or ``coord notify`` can still
       promote it.
    3. **No-op guard** — skipped when the recomputed status agrees with the
       existing guess (the common case once #2553 made the guess itself
       branch-aware) or the row's current status isn't one this arm could
       have produced (:data:`_LATE_REPORT_SAFE_TARGETS` again — never touches
       a row a human already reset).

    Kept passive per this module's #1616 contract, same as its sibling: it
    writes state through ``update_state_fn`` and dispatches nothing.

    Returns one dict per corrected assignment (empty when nothing changed).
    """
    if update_state_fn is None:
        from coord.issue_store import _update_local_state  # noqa: PLC0415

        update_state_fn = _update_local_state

    if board is None:
        from coord.state import build_board  # noqa: PLC0415

        board = build_board()

    if now is None:
        now = time.time()

    hosts = {m.name: m.host for m in config.machines}
    status_by_host: dict[str, dict | None] = {}  # poll each agent at most once
    corrected: list[dict] = []

    for a in board.completed:
        aid = a.assignment_id
        if not aid:
            continue
        if a.status not in _LATE_REPORT_SAFE_TARGETS:
            continue  # not a status this arm could have guessed — never touch it
        reason = a.failure_reason or ""
        if _NO_AGENT_RECORD_GUESS_MARKER not in reason:
            continue  # terminal for a real reason — nothing to correct
        finished_at = a.finished_at
        if not finished_at or (now - finished_at) > _LATE_REPORT_CORRECTION_WINDOW_SECONDS:
            continue  # past the correction window — never revisit forever

        host = hosts.get(a.machine_name)
        if not host:
            continue
        if host not in status_by_host:
            status_by_host[host] = agent_status_fn(host)
        status = status_by_host[host]
        if not status:
            continue  # agent unreachable → leave the guess, retry next tick

        entry = next(
            (e for e in status.get("completed", []) if e.get("id") == aid),
            None,
        )
        if entry is None:
            continue  # still no record — nothing to correct with yet

        terminal = _AGENT_TERMINAL_STATUS.get(effective_agent_status(entry))
        if terminal not in _LATE_REPORT_SAFE_TARGETS or terminal == a.status:
            continue  # agrees with the guess, or would need the `done` path

        corrected_reason = (
            f"corrected a stale no-agent-record guess ({a.status!r}) — the "
            f"agent's own completed record for this assignment has since "
            f"arrived and reports {entry.get('status')!r} (#2547)"
        )
        update_state_fn(
            assignment_id=aid,
            terminal_status=terminal,
            branch=a.branch or entry.get("branch"),
            review_state=None,
            failure_reason=corrected_reason,
            exit_code=entry.get("exit_code"),
        )

        corrected.append(
            {
                "assignment_id": aid,
                "issue_number": a.issue_number,
                "repo": a.repo_name,
                "type": a.type,
                "from_status": a.status,
                "to_status": terminal,
                "reason": corrected_reason,
            }
        )

    return corrected


def propagate_smoke_terminal_failure(
    *,
    parent_assignment_id: str | None,
    failure_reason: str | None,
    environmental: bool | None = None,
) -> None:
    """#1605: resolve a work row's ``test_state`` when its Test-stage
    (``type="smoke"``) child dies without ever reporting pass/fail.

    Before this, a smoke assignment landing on ``status="failed"`` (a dead
    agent, a killed process group, a terminal API error — anything short of
    the worker itself printing ``SMOKE: pass``/``SMOKE: fail``) left the
    parent's ``test_state`` at whatever it was — almost always ``"running"``,
    the marker `dispatch_smoke` stamps the instant it dispatches (#1426).
    Every downstream gate treats ``"running"`` as "no verdict yet" (#1395),
    so the work sits in a state nothing will ever resolve: `coord drive`
    polls it forever, the merge gate never sees a verdict, and `coord
    diagnose --stage test` had nothing to say because it never looked past
    the (terminal, `status="done"`) work row itself.

    Classified through :func:`coord.failure_class.classify_failure` — the
    same #1590 environmental-vs-work split already used for the work/review
    stages, applied here for the first time to the Test stage:

    * **environmental** (usage limit, an API 5xx, a network drop) — the
      provider's fault, not the work's. Clears ``test_state`` back to
      ``NULL`` (not ``"failed"``) so the daemon's normal
      :func:`coord.smoke.dispatch_pending_smoke` auto-queue picks the work
      row back up on its next tick and re-dispatches a fresh Test stage —
      never spending the bounded ``coord fix`` retry budget on a code defect
      that never existed.
    * **work** (an unclassifiable crash, a real defect) — records
      ``test_state="failed"`` exactly like a normal non-zero-exit smoke
      completion already does (`coord/notify.py`'s completion handler), so
      the existing bounded `coord fix` loop picks it up from there.

    *environmental* (#2275) overrides that classification when the CALLER
    already knows the answer.  Default ``None`` means "classify from
    *failure_reason*", i.e. every pre-#2275 caller is unchanged.  Pass
    ``True`` only when the reason is coordinator-authored prose describing a
    lost worker rather than a worker's own terminal output — the classifier's
    vocabulary is deliberately restricted to named API/network wire tokens
    (see :mod:`coord.failure_class`) and correctly defaults everything else to
    WORK, so a call site that *knows* the machine ate the worker has to say
    so rather than smuggle a token into its prose.  The only such caller today
    is :func:`_reconcile_no_agent_record`.

    A no-op when *parent_assignment_id* is falsy (a smoke row somehow
    missing its ``review_of_assignment_id`` — should not happen in practice,
    but this must never raise on it).
    """
    if not parent_assignment_id:
        return
    from coord.failure_class import classify_failure  # noqa: PLC0415
    from coord.smoke import mute_smoke_legs, mute_smoke_tally  # noqa: PLC0415
    from coord.state import (  # noqa: PLC0415
        load_assignment_test_reason,
        record_test_verdict,
    )

    classification = classify_failure(failure_reason=failure_reason)
    if environmental is None:
        is_environmental = classification.is_environmental
        cause = classification.reason
    else:
        is_environmental = bool(environmental)
        cause = failure_reason or classification.reason
    if is_environmental:
        # #2272: this clear must CARRY the mute-leg tally, for exactly the
        # reason `dispatch_smoke`'s `running` stamp must. `test_reason` is the
        # only field that survives between Test-stage legs, so any writer that
        # replaces it wholesale silently hands the row a fresh retry budget —
        # and a row that alternates "mute leg" / "worker died environmentally"
        # would then never reach the bound from either side. The tally is not
        # incremented here (an environmental death is a different, genuinely
        # self-healing cause and #1605's unbounded re-dispatch of it is
        # deliberate) — it is only preserved, so mute legs keep counting
        # across it.
        carried = mute_smoke_legs(
            load_assignment_test_reason(parent_assignment_id)
        )
        prefix = f"{mute_smoke_tally(carried)} — " if carried else ""
        record_test_verdict(
            assignment_id=parent_assignment_id,
            test_state=None,
            test_reason=(
                f"{prefix}Test stage worker died environmentally "
                f"({cause}) — cleared for automatic "
                "re-dispatch, not recorded as a work failure (#1605)"
            ),
        )
    else:
        record_test_verdict(
            assignment_id=parent_assignment_id,
            test_state="failed",
            test_reason=(
                failure_reason
                or "Test stage worker failed with no reason recorded (#1605)"
            ),
        )


def _capture_plan_best_effort(host: str, assignment_id: str) -> bool:
    """Fetch + persist a plan's structured output from the agent log so the
    TUI's plan detail panel isn't empty after a passive reconcile.  Best
    effort: any failure is swallowed — the terminal-status write already
    landed and is what fixes the stuck box."""
    try:
        from coord.plan_parser import parse_plan_from_agent  # noqa: PLC0415
        from coord.state import save_plan  # noqa: PLC0415

        plan = parse_plan_from_agent(host, assignment_id)
        if plan is None or plan.is_empty():
            return False
        save_plan(assignment_id, plan.to_dict())
        return True
    except Exception:  # noqa: BLE001 — never let plan capture break the reconcile
        return False


def _capture_cost_from_entry_best_effort(assignment_id: str, entry: dict) -> None:
    """#666 Gap A: capture cost from an agent ``completed`` entry when flipping
    a row terminal.

    Best-effort and silent — any exception is swallowed so a cost-capture
    failure never crashes the daemon's reconcile tick.

    Cost source: ``total_cost_usd`` (full-log parse, available when the agent
    serves terminal entries) with ``cost_so_far`` as a fallback.  Either is
    used only when present and > 0 so an un-measured session isn't written as 0.

    Token counts are captured separately by ``_capture_tokens_best_effort``
    (#667 Gap B), which is called at the same call site.
    """
    try:
        from coord.state import update_assignment_cost  # noqa: PLC0415

        raw_cost = entry.get("total_cost_usd") or entry.get("cost_so_far")
        if raw_cost is not None:
            try:
                cost = float(raw_cost)
            except (TypeError, ValueError):
                cost = None
            else:
                if cost > 0:
                    update_assignment_cost(assignment_id, cost)
    except Exception:  # noqa: BLE001 — never let cost capture break the reconcile
        pass


def _capture_tokens_best_effort(assignment_id: str, entry: dict) -> None:
    """#667/#2786: persist token counts (+ turns) from a /status completed entry.

    The agent now parses its own log and includes
    ``input_tokens`` / ``output_tokens`` / ``cache_creation_tokens`` /
    ``cache_read_tokens`` / ``num_turns`` in the completed entry.  We write
    them to the DB here so a passive reconcile also captures tokens (not
    just cost).  Best-effort — any failure is swallowed.
    """
    try:
        input_tokens = int(entry.get("input_tokens") or 0)
        output_tokens = int(entry.get("output_tokens") or 0)
        cache_creation_tokens = int(entry.get("cache_creation_tokens") or 0)
        cache_read_tokens = int(entry.get("cache_read_tokens") or 0)
        num_turns = int(entry.get("num_turns") or 0)
        if input_tokens + output_tokens + cache_creation_tokens + cache_read_tokens == 0:
            return
        from coord.state import update_assignment_tokens  # noqa: PLC0415

        update_assignment_tokens(
            assignment_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_tokens=cache_creation_tokens,
            cache_read_tokens=cache_read_tokens,
            num_turns=num_turns,
        )
    except Exception:  # noqa: BLE001 — never let token capture break the reconcile
        pass


def _capture_stop_reason_best_effort(assignment_id: str, entry: dict) -> None:
    """#2316: persist the raw ``stop_reason`` from a /status completed entry.

    The agent parses its own log and includes ``stop_reason`` on every
    terminal ``completed`` entry (``coord.agent.AgentServer.list_assignments``
    — this predates #2316, only the write path is new). Captured for EVERY
    terminal assignment, not gated on status, so the column reflects the
    worker's own last word (``"end_turn"``, ``"length"``, ``"max_tokens"``,
    ...) regardless of how ``_failure_reason``/the board's ``status``
    classified the run. Best-effort — any failure is swallowed so it can't
    break the reconcile.
    """
    try:
        stop_reason = entry.get("stop_reason")
        if not stop_reason:
            return
        from coord.state import update_assignment_stop_reason  # noqa: PLC0415

        update_assignment_stop_reason(assignment_id, stop_reason)
    except Exception:  # noqa: BLE001 — never let this capture break the reconcile
        pass


def _build_fix_round_retry_briefing(
    failed: Assignment, board: Board, repo_cfg, max_review_iterations: int,
) -> str | None:
    """#1411: rebuild the FIX briefing (reviewer findings included) for a
    retried fix-round assignment.

    ``failed`` is itself a fix worker (``review_iteration > 0``) dispatched
    by the auto-loop — its branch already carries the code the reviewer
    rejected. The generic retry briefing built by :func:`_build_retry_briefing`
    has no notion of *why*, so a plain continuation retry reliably repeats
    the same request-changes verdict (a wasted work+review round) and, worse,
    used to reset ``review_iteration`` to 0 — silently disabling the
    ``max_review_iterations`` flood guard.

    Reuses ``auto_loop._build_fix_briefing`` — the exact function the
    original fix dispatch used — fed with the reviewer's findings recovered
    via ``auto_loop._load_review_findings`` (DB cache → local log → agent
    HTTP → GitHub message bus), the same resolution chain the auto-loop
    itself relies on. This is deliberately a reuse, not a new capability.

    Returns ``None`` when the review chain can't be reconstructed (the
    reviewed work assignment or its review is missing from the board, or the
    findings can't be recovered from any source) — the caller falls back to
    the generic continuation briefing rather than blocking the retry.
    """
    work = board.find_by_id(failed.review_of_assignment_id)
    if work is None:
        return None

    review = next(
        (
            a for a in (*board.active, *board.completed)
            if a.type == "review"
            and a.review_of_assignment_id == failed.review_of_assignment_id
        ),
        None,
    )
    if review is None:
        return None

    from coord.auto_loop import _build_fix_briefing, _load_review_findings  # noqa: PLC0415

    findings = _load_review_findings(
        review, None, None,
        repo_github=repo_cfg.github if repo_cfg is not None else None,
    )
    if findings is None:
        return None

    return _build_fix_briefing(
        work, findings, failed.review_iteration, max_review_iterations,
    )


def _build_retry_briefing(
    failed: Assignment, repo_cfg, *, default_branch: str | None = None,
    board: Board | None = None, max_review_iterations: int = 3,
) -> str:
    """#1101: reconstruct a real briefing for a retried assignment.

    ``failed.briefing`` is frequently empty or unhelpful by the time a
    failed assignment is retried — a `work` assignment's fully-assembled
    briefing (issue body + board context) is built at initial-dispatch
    time and not always persisted back onto the stored ``Assignment``.
    Replaying it verbatim can hand the retried worker nothing at all,
    which reproduced as the worker exiting in one turn with 0 commits
    (silently reclassified as "advisory" instead of a broken dispatch).

    This rebuilds something the worker can act on:
    - the original briefing text when present, else a fresh fetch of the
      issue body from GitHub (mirrors ``coord assign``'s own
      auto-generate-from-issue-body fallback) so the briefing is never
      blank;
    - continuation instructions when the failed assignment already has a
      branch (mirrors the equivalent ``coord fix`` briefing in
      ``plan_followup.py``): don't start over, inspect what's already
      committed;
    - the recorded failure reason, so the worker knows why the previous
      attempt stopped instead of re-discovering it from scratch.

    #1411: when *failed* is itself a fix round (``review_iteration > 0``),
    the ``## Task`` section is instead the rebuilt FIX briefing — reviewer
    findings included — via :func:`_build_fix_round_retry_briefing`, so the
    retry knows what the reviewer objected to instead of blindly redoing
    work the branch already contains.
    """
    fix_task: str | None = None
    if failed.review_iteration and failed.review_iteration > 0 and board is not None:
        fix_task = _build_fix_round_retry_briefing(
            failed, board, repo_cfg, max_review_iterations,
        )

    base = (failed.briefing or "").strip()
    if not base and fix_task is None and repo_cfg is not None:
        try:
            from coord import github_ops  # noqa: PLC0415

            issue_data = github_ops.get_issue(repo_cfg.github, failed.issue_number)
            issue_body = issue_data.get("body", "")
            if issue_body:
                base = f"Issue #{failed.issue_number}: {failed.issue_title}\n\n{issue_body}"
        except RuntimeError:
            pass  # best-effort — fall through with whatever we have

    sections: list[str] = []
    if failed.branch:
        # #934: point the retry's diff/log instructions at `feature/ms-NN`
        # when this issue belongs to a milestone and the repo opted into the
        # git model — falls back to `default_branch` (today's behavior)
        # otherwise. Callers that already resolved this (``_reassign``) pass
        # it in via *default_branch*; otherwise resolve it here, but only
        # perform the milestone lookup (a `gh` call) when the repo opted in.
        if default_branch is None:
            default_branch = (repo_cfg.default_branch if repo_cfg is not None else None) or "main"
            if repo_cfg is not None and getattr(repo_cfg, "develop_branch", None):
                from coord.branch_model import (  # noqa: PLC0415
                    fetch_issue_milestone_number,
                    resolve_base_branch,
                )

                milestone_number = fetch_issue_milestone_number(
                    repo_cfg.github, failed.issue_number,
                )
                default_branch = resolve_base_branch(repo_cfg, milestone_number)
        sections.append(
            "## Retry — continuing existing work\n"
            f"This is a retry of a previously failed assignment "
            f"({failed.assignment_id}). The previous worker's branch "
            f"`{failed.branch}` already exists and may carry real, "
            f"committed work — you are continuing it, NOT starting over.\n"
            f"Run `git fetch origin && git log --oneline "
            f"origin/{default_branch}..HEAD` to see what's already done, "
            f"and `git diff origin/{default_branch}...HEAD` for the full "
            f"diff, before writing any new code."
        )
    if failed.failure_reason:
        sections.append(f"## Why the previous attempt failed\n{failed.failure_reason}")
    if fix_task is not None:
        sections.append(
            f"## Task — fix round {failed.review_iteration} "
            f"(reviewer findings included)\n{fix_task}"
        )
    elif base:
        sections.append(f"## Task\n{base}")
    if not sections:
        # Nothing stored, nothing fetched, no branch context either — this
        # is exactly the silent-empty-briefing failure mode from #1101.
        sections.append(
            f"Issue #{failed.issue_number}: {failed.issue_title}\n\n"
            f"(No stored briefing or issue body was available to "
            f"reconstruct this retry — investigate issue "
            f"#{failed.issue_number} directly.)"
        )
    return "\n\n".join(sections)


def _running_by_machine(board: Board) -> dict[str, list[Assignment]]:
    """Group ``board.active`` running assignments by machine name (#1417).

    Shared by :func:`_reassign` and :func:`describe_no_candidate_machines` so
    the two paths can never drift on what counts as "running".
    """
    running: dict[str, list[Assignment]] = {}
    for a in board.active:
        if a.status == "running":
            running.setdefault(a.machine_name, []).append(a)
    return running


def _machine_capacity(machine: Machine, config: Config) -> int:
    """Effective concurrent-assignment cap for *machine* (#1417).

    ``machines[].max_workers`` in coordinator.yml overrides the fleet-wide
    ``concurrency.max_workers`` default — set it lower on hardware that
    can't keep up with the fleet norm (e.g. a 4-core box among 20-core
    desktops). Unset (``None``) means "use the fleet-wide default", so a
    single running assignment no longer reads as "full" the way a bare
    ``machine in busy`` membership check used to (#1417).
    """
    return machine.max_workers if machine.max_workers is not None else config.concurrency.max_workers


class UnsupportedRetryType(ValueError):
    """Raised by :func:`_reassign` when *failed.type* cannot be safely
    re-dispatched through the work-retry path (#1636).

    ``_reassign`` used to hardcode ``type="work"`` on every retry regardless
    of the failed assignment's actual type — a retried ``smoke``/``review``
    row silently came back as a fresh WORK worker (model escalated) pointed
    at the already-complete branch, instead of re-running the Test/Review
    stage. Raising here — instead of silently downgrading to work — lets
    every caller (``coord retry``, ``auto_reassign``) surface the command
    that actually re-runs the right stage rather than quietly doing the
    wrong thing.
    """

    def __init__(self, assignment_type: str, work_assignment_id: str | None):
        self.assignment_type = assignment_type
        self.work_assignment_id = work_assignment_id
        super().__init__(
            f"assignment type {assignment_type!r} cannot be retried "
            "through the work-retry path"
        )


# #1636: types whose failed row can be re-dispatched with the exact command
# that re-runs their stage — `review_of_assignment_id` on a smoke/review
# assignment is the work assignment it targets, so the hint is always
# actionable when set. Extend this map, not the work-retry path, when a new
# non-WORK_LIKE type grows its own retry story.
_RETRY_REDIRECT_FLAGS: dict[str, str] = {
    "smoke": "--smoke-of",
    "review": "--review-of",
}


def describe_unsupported_retry_type(exc: UnsupportedRetryType) -> str:
    """Human-readable refusal message for :class:`UnsupportedRetryType` (#1636).

    Mirrors :func:`describe_no_candidate_machines` — a caller shouldn't have
    to hand-craft the "this can't be retried" message.
    """
    flag = _RETRY_REDIRECT_FLAGS.get(exc.assignment_type)
    if flag is not None and exc.work_assignment_id:
        return (
            f"assignment type {exc.assignment_type!r} cannot be retried "
            "with `coord retry` — that would silently re-dispatch it as a "
            "fresh work worker on the already-complete branch. Re-run its "
            f"stage instead: `coord assign --interactive {flag} "
            f"{exc.work_assignment_id}`."
        )
    return (
        f"assignment type {exc.assignment_type!r} cannot be retried with "
        "`coord retry` — that would silently re-dispatch it as a fresh "
        "work worker. Re-dispatch it through its own path instead."
    )


class RetryProviderMismatch(ValueError):
    """Raised by :func:`_reassign` when a retry would resolve to a
    different provider than the failed run it is replacing (#2323).

    A retry that consults ``providers.labels`` can legitimately land on a
    different provider than *this exact assignment row* if the issue's
    labels or ``coordinator.yml`` changed between the original dispatch and
    the retry — but silently substituting the provider is exactly the #1796
    failure mode one level up: an operator (or an unattended `coord drive`)
    asking to retry a `harness:opencode` leg is not asking to move it to
    `claude`, even if that is where today's precedence chain would land.
    Refuse instead, the same way the #437 TOS gate refuses rather than
    substitutes a human-attended provider.
    """

    def __init__(self, failed_provider: str, resolved_provider: str):
        self.failed_provider = failed_provider
        self.resolved_provider = resolved_provider
        super().__init__(
            f"retry would move this assignment from provider "
            f"{failed_provider!r} to {resolved_provider!r}"
        )


def describe_retry_provider_mismatch(exc: RetryProviderMismatch) -> str:
    """Human-readable refusal message for :class:`RetryProviderMismatch`
    (#2323).

    Mirrors :func:`describe_unsupported_retry_type`'s shape — names both
    providers and points at the deliberate override an operator who
    actually wants the move can use.
    """
    return (
        f"refusing retry: the failed run dispatched through provider "
        f"{exc.failed_provider!r}, but this retry resolves to "
        f"{exc.resolved_provider!r} — retrying would silently move the "
        "work onto a different provider (and, for a claude provider, walk "
        "a model-escalation ladder that has no meaning for the other one). "
        "If the issue's labels or `providers.labels` changed since the "
        "original dispatch and the move is intentional, dispatch by hand "
        "instead: `coord assign --interactive`."
    )


def _resolve_retry_provider(
    failed: Assignment, config: Config, issue_labels: list[str] | None,
) -> str:
    """Resolve (and #437 TOS-guard) the provider a retry of *failed* would
    dispatch through, refusing if it differs from the provider the failed
    run itself used (#2323).

    Mirrors a first work dispatch's resolution precedence exactly
    (``coord.dispatch.dispatch``, ``coord/dispatch.py:548``): spec (always
    ``None`` here — a retry never carries a fresh per-spec override) ->
    ``providers.labels`` (gated to ``failed.type == "work"``, the same
    restriction the first dispatch applies) -> repo default ->
    ``providers.default``.

    Args:
        failed: The failed/advisory assignment being retried.
        config: The coordinator config.
        issue_labels: The target issue's current GitHub label names, or
            ``None``/empty when unavailable — falls back to no label match,
            reproducing pre-#2323 (label-blind) resolution for that one
            issue rather than raising.

    Returns:
        The resolved (and TOS-cleared) provider name.

    Raises:
        ValueError: the #437 TOS gate refuses (the resolved provider is
            ``human_attended_only``) — same contract
            :func:`coord.providers.guard_unattended_dispatch` uses.
        RetryProviderMismatch: the resolved provider differs from
            ``failed.provider_name`` (falling back to ``"claude"`` for a
            row dispatched before #324 started recording it).
    """
    from coord.providers import guard_unattended_dispatch  # noqa: PLC0415

    repo_for_provider = config.repo(failed.repo_name)
    # #1889/#2323: providers.labels routes work dispatches by the issue's
    # harness-eval label — gated to type="work" exactly as the first
    # dispatch gates it (coord/dispatch.py:548) so a label meant for the
    # eventual work dispatch never leaks into mock-author/test-author
    # retries that never saw it on their original dispatch either.
    provider_issue_labels = issue_labels if failed.type == "work" else None
    resolved = guard_unattended_dispatch(
        spec_provider=None,
        repo_provider=(
            repo_for_provider.provider if repo_for_provider is not None else None
        ),
        providers_cfg=config.providers,
        models_cfg=config.models,
        where="auto-reassign (reconcile)",
        issue_labels=provider_issue_labels,
    )
    # #324: None means "dispatched before #324 landed or via a path that
    # doesn't set this field" — the documented implicit default is "claude".
    failed_provider = failed.provider_name or "claude"
    if resolved != failed_provider:
        raise RetryProviderMismatch(failed_provider, resolved)
    return resolved


def _reassign(
    failed: Assignment, board: Board, config: Config,
    *,
    model: str | None = None,
    issue_labels: list[str] | None = None,
) -> Assignment | None:
    """Re-dispatch a failed assignment to a machine with spare capacity.

    *model* overrides the model tier on the retry. When None, the
    original assignment's model is reused (escalation happens at the call
    site — and must only be requested when the retry resolves to the same
    claude-family provider the failed run used, #2323: the claude
    escalation ladder means nothing to another provider).

    *issue_labels* (#2323) is the target issue's current GitHub label
    names, threaded into provider resolution via
    :func:`_resolve_retry_provider` so ``providers.labels`` is honoured on
    a retry the same way it is on a first dispatch — without this, every
    retry fell through to the repo/global default regardless of which
    label originally routed the issue.

    Raises :class:`UnsupportedRetryType` when ``failed.type`` is not in
    :data:`coord.models.WORK_LIKE_TYPES` — a ``smoke``/``review``/other
    non-work row must not be silently re-dispatched as a fresh
    ``type="work"`` worker (#1636).

    Raises :class:`RetryProviderMismatch` (#2323) when the resolved
    provider differs from the one the failed run actually used — refuses
    rather than silently moving the work onto a different provider, the
    #1796 rule applied at dispatch.
    """
    if failed.type not in WORK_LIKE_TYPES:
        raise UnsupportedRetryType(failed.type, failed.review_of_assignment_id)

    from coord.machine_pause import paused_set
    paused = paused_set(config.machines)
    running = _running_by_machine(board)

    # #1417: fleet-wide cap first — respected regardless of per-machine
    # headroom, mirroring `concurrency.max_workers`'s documented meaning as
    # the total concurrent-worker budget across the whole fleet.
    fleet_running = sum(len(v) for v in running.values())
    if fleet_running >= config.concurrency.max_workers:
        return None

    def has_room(m: Machine) -> bool:
        return len(running.get(m.name, [])) < _machine_capacity(m, config)

    # #437: STRUCTURAL TOS-COMPLIANCE GATE — auto-reassign is an
    # unattended dispatch path; refuse to retry through a provider that
    # opts out of unattended use.  #2323: also resolves through
    # `providers.labels` (gated to type="work", exactly as a first
    # dispatch gates it) instead of skipping straight to the repo/global
    # default, and refuses outright — RetryProviderMismatch, NOT a silent
    # `return None` — when the resolution disagrees with the provider the
    # failed run actually used.  A plain TOS ValueError still resolves to
    # `return None`: skip the reassignment, leaving the failed assignment
    # for human attention rather than getting silently re-tried on the
    # wrong provider.  Resolved BEFORE machine selection so the #1711
    # capability filter below can key off it.
    try:
        resolved_provider_name = _resolve_retry_provider(failed, config, issue_labels)
    except RetryProviderMismatch:
        raise
    except ValueError:
        return None

    # #1711: STRUCTURAL PROVIDER-AVAILABILITY GATE, applied as a candidate
    # filter — a retry must never route to a machine that hasn't declared
    # it can run the resolved provider (e.g. an `opencode` retry landing on
    # a machine with no `provider:opencode` capability), exactly as a first
    # dispatch refuses that combination (`coord.dispatch.dispatch` →
    # `guard_provider_machine_capability`). Filtering (rather than raising
    # on `candidates[0]`) means a fleet where SOME machine declares the
    # capability still retries there; a fleet where none does falls through
    # to the "no candidates" `return None`, leaving the row for human
    # attention — `describe_no_candidate_machines` names the reason.
    from coord.providers import machine_supports_provider  # noqa: PLC0415

    def can_run_provider(m: Machine) -> bool:
        return machine_supports_provider(m, resolved_provider_name, config.providers)

    candidates = [
        m for m in config.machines
        if m.can_work_on(failed.repo_name)
        and m.repo_path(failed.repo_name) is not None
        and has_room(m)
        and can_run_provider(m)
        and m.name != failed.machine_name
        and m.name not in paused
    ]
    if not candidates:
        # Fall back to including the same machine that failed last time —
        # paused machines (and #1711 capability-lacking machines) stay
        # excluded even from the fallback.
        candidates = [
            m for m in config.machines
            if m.can_work_on(failed.repo_name)
            and m.repo_path(failed.repo_name) is not None
            and has_room(m)
            and can_run_provider(m)
            and m.name not in paused
        ]
    if not candidates:
        return None

    machine = candidates[0]
    repo_path = machine.repo_path(failed.repo_name)

    retry_model = model if model is not None else failed.model
    # #2383: `failed.model` can name a model an operator has since REMOVED
    # from `models.escalation` (e.g. a run auto-escalated onto it before the
    # ladder was edited) — inheriting it here forever, on every subsequent
    # auto-reassign of the same lineage, would silently keep dispatching a
    # model the current config no longer sanctions. Only the inherited
    # (`model is None`) path needs this: an explicit caller-supplied `model`
    # is a deliberate choice and passes through unclamped. And only a
    # CLAUDE-family retry needs it — `models.escalation` is a claude-only
    # concept (#2323's "the claude escalation ladder means nothing to
    # another provider"), so an opencode/other-provider model like
    # `opencode/glm-5.2` must never be walked onto the claude ladder just
    # because it doesn't happen to appear on it.
    if (
        model is None
        and resolved_provider_name == "claude"
        and retry_model not in (*config.models.escalation, config.models.default)
    ):
        retry_model = (
            config.models.escalation[-1]
            if config.models.escalation
            else config.models.default
        )
    # The Assignment keeps the alias for legibility; the wire payload is
    # resolved through models.versions when an exact id is pinned.
    retry_model_wire = config.models.resolve(retry_model)

    repo_cfg = config.repo(failed.repo_name)
    # #934: retry inherits `feature/ms-NN` as its integration base when this
    # issue belongs to a milestone and the repo opted into the git model —
    # falls back to `default_branch` (today's behavior) otherwise. Resolved
    # once and reused for both the briefing's diff instructions and the
    # `branch` payload field below, so they never disagree. The milestone
    # lookup itself is skipped (no `gh` call) when the repo hasn't opted in.
    retry_default_branch = (repo_cfg.default_branch if repo_cfg is not None else None) or "main"
    if repo_cfg is not None and getattr(repo_cfg, "develop_branch", None):
        from coord.branch_model import (  # noqa: PLC0415
            fetch_issue_milestone_number,
            resolve_base_branch,
        )

        milestone_number = fetch_issue_milestone_number(
            repo_cfg.github, failed.issue_number,
        )
        retry_default_branch = resolve_base_branch(repo_cfg, milestone_number)
    retry_briefing = _build_retry_briefing(
        failed, repo_cfg, default_branch=retry_default_branch,
        board=board, max_review_iterations=config.pipeline.max_review_iterations,
    )
    payload = {
        "repo_name": failed.repo_name,
        "repo_path": repo_path,
        "issue_number": failed.issue_number,
        "issue_title": f"[retry] {failed.issue_title}",
        "briefing": retry_briefing,
        "files_allowed": failed.files_allowed,
        "files_forbidden": failed.files_forbidden,
        "pull_repos": [],
        # #1636: carries the failed assignment's own type (guaranteed to be
        # in WORK_LIKE_TYPES by the guard above) instead of hardcoding
        # "work" — a "mock-author"/"test-author" retry must not silently
        # relabel itself as plain work.
        "type": failed.type,
        "model": retry_model_wire,
        # #255: retry inherits the repo's configured default branch as the
        # worker's integration base (the start point / rebase target).
        "branch": retry_default_branch,
    }
    # #2323: name the resolved provider on the wire the same way a first
    # dispatch does (`coord.dispatch._wire_payload_needs_provider_field`) —
    # omitted only for the vanilla, uncustomized "claude" definition so
    # byte-identical payloads survive for every deployment that never
    # touches `providers:`. Without this, `resolved_provider_name` above
    # was resolved (and TOS-guarded, and matched against the failed run)
    # purely for bookkeeping, and the wire payload silently fell back to
    # the agent's hardcoded legacy claude spawn path regardless — the exact
    # bug #2323 reported.
    from coord.dispatch import _wire_payload_needs_provider_field  # noqa: PLC0415
    if _wire_payload_needs_provider_field(resolved_provider_name, config):
        payload["provider"] = resolved_provider_name
    # #1101: continue the failed assignment's actual branch instead of
    # silently forking a fresh one off the repo default — any real work it
    # already committed and pushed must not be orphaned by a retry. Mirrors
    # the `target_branch` wire field `--fix-of`/`--rework-of`/
    # `_dispatch_followup` already use; the agent checks out this exact
    # branch (hard-reset to the remote tip) when it exists on origin, and
    # falls back to a fresh branch off `branch` above when it doesn't.
    if failed.branch:
        payload["target_branch"] = failed.branch

    url = f"http://{machine.host}:{AGENT_PORT}/assign"
    try:
        resp = httpx.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        agent_response = resp.json()
    except (httpx.HTTPError, httpx.TimeoutException):
        return None

    retry_assignment = Assignment(
        machine_name=machine.name,
        repo_name=failed.repo_name,
        issue_number=failed.issue_number,
        issue_title=f"[retry] {failed.issue_title}",
        files_allowed=failed.files_allowed,
        files_forbidden=failed.files_forbidden,
        briefing=retry_briefing,
        assignment_id=agent_response.get("id") or uuid.uuid4().hex[:12],
        status="running",
        dispatched_at=time.time(),
        type=failed.type,
        model=retry_model,
        # #2323: record the resolved provider the same way a first dispatch
        # does (`response.get("_provider_name")` in coord/commands/
        # dispatch.py) — so the board/TUI and the next retry's own
        # `_resolve_retry_provider` mismatch check see the provider this
        # retry actually ran under, not the one the failed row ran under.
        provider_name=resolved_provider_name,
        # #1101: record the continued branch on the board immediately
        # instead of waiting for a later reconcile backfill from agent
        # /status — the retry payload above already told the agent to
        # check out `failed.branch` via target_branch.
        branch=failed.branch,
        # #1411: carry the fix-loop bookkeeping across the retry. Without
        # this the retry's review_iteration silently resets to 0, so
        # `pipeline.max_review_iterations` loses its accounting — a story
        # that already burned fix rounds looks fresh again. Preserving
        # review_of_assignment_id too keeps the work/review chain intact
        # for `_build_fix_round_retry_briefing` if THIS retry also fails
        # and gets retried again.
        review_iteration=failed.review_iteration,
        review_of_assignment_id=failed.review_of_assignment_id,
        # #1553: carry the oracle-loop slice attribution across the retry
        # too — otherwise a retried acceptance slice silently falls back to
        # being booked against the milestone's tracking issue and the
        # child's row goes quiet again mid-run. None for ordinary work.
        for_issue_number=failed.for_issue_number,
    )
    board.active.append(retry_assignment)

    from coord.state import record_dispatched_assignment
    repo = config.repo(failed.repo_name)
    if repo is not None:
        record_dispatched_assignment(
            assignment=retry_assignment,
            repo_github=repo.github,
        )

    return retry_assignment


def describe_no_candidate_machines(
    failed: Assignment, board: Board, config: Config,
    issue_labels: list[str] | None = None,
) -> str:
    """Explain why :func:`_reassign` found no candidate machine (#1396).

    ``_reassign`` silently returns ``None`` on any of: no machine with spare
    capacity can work on the repo, a TOS-gate refusal, or a dispatch POST
    failure — so a caller (``coord retry``) can only ever say "no available
    machine to retry on", which is true from the code's point of view and
    useless to an operator when ``coord status`` shows every machine well
    under capacity. The real cause is almost always a phantom ``running``
    board row: a dead interactive (``claude-pty``) session that nothing
    reaped, still counted against the machine's capacity.

    Mirrors ``_reassign``'s exact candidate filter (repo capability, repo
    path, pause set, capacity check, #1711 provider-capability check,
    same-machine exclusion — #1417 replaced the old binary "any running
    assignment = busy" rule with a per-machine capacity count against
    ``machines[].max_workers``/``concurrency.max_workers``) but keeps a
    reason per excluded machine instead of discarding it, so the message
    names the blocking machines and what they're apparently running —
    including the age, which makes a 400-hour-old phantom obvious at a
    glance.

    *issue_labels* (#2323) threads the same label list the caller handed
    ``_reassign``, so the provider this diagnostic resolves (for the #1711
    capability lines) matches the one the retry actually resolved.
    """
    from coord.machine_pause import paused_set  # noqa: PLC0415
    from coord.providers import (  # noqa: PLC0415
        machine_supports_provider,
        resolve_provider_name,
    )

    # #1711: resolve the provider the retry would use, so machines that
    # can't run it are named as such instead of reading as "free". A
    # resolution failure (unknown provider name in config) degrades to
    # skipping the capability lines — this is a diagnostic, not a gate.
    repo_for_capability = config.repo(failed.repo_name)
    try:
        provider_for_capability: str | None = resolve_provider_name(
            None,
            repo_for_capability.provider if repo_for_capability is not None else None,
            config.providers,
            issue_labels=issue_labels if failed.type == "work" else None,
        )
    except ValueError:
        provider_for_capability = None

    paused = paused_set(config.machines)
    now = time.time()

    running_by_machine = _running_by_machine(board)

    relevant_machines = [
        m for m in config.machines
        if m.can_work_on(failed.repo_name) and m.repo_path(failed.repo_name) is not None
    ]
    if not relevant_machines:
        return f"no machine in coordinator.yml can work on repo {failed.repo_name!r}"

    # #1417: the fleet-wide cap blocks every machine regardless of
    # individual headroom — computed once so a machine that's personally
    # under its own cap can still be correctly labeled as blocked by the
    # fleet-wide budget instead of silently reading as "free".
    fleet_running = sum(len(v) for v in running_by_machine.values())
    fleet_cap = config.concurrency.max_workers
    fleet_full = fleet_running >= fleet_cap

    lines: list[str] = []
    has_free_candidate = False
    for m in relevant_machines:
        if m.name in paused:
            lines.append(f"  {m.name}: paused")
            continue
        if provider_for_capability is not None and not machine_supports_provider(
            m, provider_for_capability, config.providers,
        ):
            # #1711: mirrors `_reassign`'s capability filter — this machine
            # never declared it can run the resolved provider, so it was
            # excluded regardless of load.
            lines.append(
                f"  {m.name}: cannot run provider {provider_for_capability!r} "
                "(no matching capability in coordinator.yml "
                "machines[].capabilities)"
            )
            continue
        running = running_by_machine.get(m.name, [])
        cap = _machine_capacity(m, config)
        own_full = len(running) >= cap
        if own_full or fleet_full:
            if running:
                parts = []
                for a in running:
                    age_h = (now - a.dispatched_at) / 3600 if a.dispatched_at else None
                    age_str = f"{age_h:.1f}h" if age_h is not None else "?h"
                    parts.append(
                        f"{a.repo_name}#{a.issue_number} type={a.type} age={age_str}"
                    )
                load_desc = f"{len(running)}/{cap} running: {'; '.join(parts)}"
            else:
                load_desc = f"0/{cap} running"
            if own_full:
                reason = f"busy — at capacity ({load_desc})"
            else:
                # This machine has its own headroom, but the fleet-wide
                # budget (concurrency.max_workers) is exhausted — name the
                # actual binding constraint rather than implying the
                # machine itself is the problem.
                reason = (
                    f"fleet at capacity ({fleet_running}/{fleet_cap} running "
                    f"fleet-wide; this machine {load_desc})"
                )
            lines.append(f"  {m.name}: {reason}")
            continue
        if m.name == failed.machine_name:
            # `_reassign`'s fallback pass drops only the "different machine"
            # constraint — it still honors capacity/paused — so a
            # under-capacity machine that just failed IS a real fallback
            # candidate (#1396 review finding 1). Categorize it as such; the
            # "(fallback-only)" label stays in `lines` for readers of the
            # capacity/paused branch below, but this path never reaches that
            # branch once a candidate is found.
            lines.append(f"  {m.name}: same machine that just failed (fallback-only)")
            has_free_candidate = True
            continue
        has_free_candidate = True

    if has_free_candidate:
        # A machine WAS free per this filter — _reassign must have failed for
        # a different reason: a TOS-gate refusal or a dispatch POST error.
        # Re-run the same config-only gate check (no network call) so the
        # message states the *actual* reason instead of a guess that may be
        # a dead end (#1396 review finding 2 — "check daemon logs" pointed
        # nowhere, since neither failure path logs anything today).
        from coord.providers import guard_unattended_dispatch  # noqa: PLC0415

        repo_for_provider = config.repo(failed.repo_name)
        try:
            guard_unattended_dispatch(
                spec_provider=None,
                repo_provider=(
                    repo_for_provider.provider
                    if repo_for_provider is not None
                    else None
                ),
                providers_cfg=config.providers,
                models_cfg=config.models,
                where="describe_no_candidate_machines (diagnostic re-check)",
            )
        except ValueError as exc:
            return (
                "a candidate machine was available, but the retry was "
                f"refused by the provider TOS gate: {exc}"
            )
        return (
            "a candidate machine was available but the retry dispatch "
            "request failed (network error or the agent was unreachable) "
            "— re-run `coord retry` to try again"
        )

    return "no available machine to retry on:\n" + "\n".join(lines)


def _escalate_spend_ceiling_best_effort(assignment: Assignment, entry: dict) -> None:
    """#2131: surface a spend-ceiling kill where a human will actually see it.

    A ceiling kill is the one terminal state that must NOT be quietly retried
    — the whole point is that the money is gone and somebody should decide
    whether to spend more. So it goes onto the **drive-escalation** board
    (``coord escalate list`` / the TUI), the same channel a merge entry's
    ``HUMAN_REQUIRED`` surfaces through, alongside the GitHub failure comment
    ``coord notify`` already posts from the stamped ``failure_reason``.

    ``proposed_command`` deliberately names the acknowledged retry rather than
    a bare ``coord retry``: the operator's next step is a decision ("is this
    worth another $8?"), and the command should say so.

    Best-effort and silent — an escalation write must never break a real
    status transition. The record is keyed by (repo, issue) and *replaces*
    any prior one, so a repeat can't flood the channel.
    """
    from coord.spend_ceiling import is_spend_ceiling_reason  # noqa: PLC0415

    reason = entry.get("spend_ceiling_reason")
    if not is_spend_ceiling_reason(reason):
        return
    repo_name = getattr(assignment, "repo_name", None)
    issue_number = getattr(assignment, "issue_number", None)
    if not repo_name or not issue_number:
        return
    assignment_id = getattr(assignment, "assignment_id", None)
    try:
        from coord.state import record_drive_escalation  # noqa: PLC0415

        record_drive_escalation(
            repo_name,
            int(issue_number),
            stage=str(getattr(assignment, "type", "work") or "work"),
            reason=(
                f"per-leg spend ceiling breached and the worker was killed: "
                f"{reason}. Nothing will re-dispatch this automatically — "
                f"decide whether the work is worth more spend before retrying."
            ),
            gate_readings=f"spend_ceiling=breached | exit_code={entry.get('exit_code')}",
            proposed_command=(
                f"coord retry {assignment_id} --acknowledge-cost"
                if assignment_id
                else "coord retry <assignment-id> --acknowledge-cost"
            ),
            assignment_id=assignment_id,
        )
    except Exception:  # noqa: BLE001 — never let escalation break a transition
        pass


def _record_usage_limit_reason(assignment_id: str | None, entry: dict) -> None:
    """#1461/#1584/#1797: stamp a usage-limit-kill, terminal-API-error, or
    auth-shaped-push-failure diagnostic (whichever the agent flagged on
    *entry*) onto *assignment_id*'s persisted ``failure_reason``.

    Used by :func:`reconcile`'s (``coord resume``) FAILED/ADVISORY branches.
    ``reconcile_completed_assignments`` — the daemon's own passive tick and
    the primary production path — does the equivalent inline via
    ``update_state_fn`` (a raw local write is safe there: that function is
    daemon-tick-only, never thin-client-reachable). ``reconcile()`` is
    different — it is called from ``coord resume``, which IS reachable from a
    thin client (same #906 audit gap `get_issue_test_mode` was fixed for) —
    so this goes through :func:`coord.state.set_assignment_failure_reason`,
    which is already daemon-aware (routes to ``POST
    /assignment-failure-reason`` when a board service is configured), rather
    than a raw ``get_connection()`` write that would silently land on a thin
    client's empty local DB instead.

    ``usage_limit_reason`` is tried first, then ``api_error_reason`` (#1584 —
    a terminal `is_error: true` result event, e.g. "529 Overloaded"; see
    `coord.agent.AgentAssignment.api_error_reason`), then
    ``push_failure_reason`` (#1797 — an auth-shaped rejection from the
    reap-time safety-net push; see `coord.agent._is_auth_push_failure`). The
    three never coexist on the same entry — a usage-limit kill is detected
    from a truncated log with no terminal `result` event, an API error is
    read OFF that terminal `result` event, and a push failure only ever
    surfaces on an otherwise-clean `exit_code == 0` reap that neither of the
    other two preempted (see the `elif` chain in `AgentServer._reap`) — so
    trying them in order never picks the wrong reason.

    This also normalises the row's status to ``'failed'`` (that helper's own
    behaviour) even when the agent's reap landed on ADVISORY — a usage-limit
    kill is, per #1461, the ONE terminal state known safe to re-dispatch
    unchanged, which is what `coord/drive.py`'s FAILED bucket already means;
    ADVISORY otherwise implies "needs a human look", which a kill does not.
    (An `api_error_reason` or `push_failure_reason` entry is never ADVISORY —
    `AgentServer._reap` always lands both directly on FAILED — so this
    normalisation is a no-op for those cases, not a behaviour change.)

    Best-effort: never raises — a diagnostic write must not break a real
    status transition.
    """
    reason = (
        entry.get("usage_limit_reason")
        or entry.get("api_error_reason")
        or entry.get("push_failure_reason")
        # #2131: the per-leg spend ceiling killed this leg. Same column, same
        # mutual exclusivity as the three above (see the identical chain in
        # `reconcile_completed_assignments`).
        or entry.get("spend_ceiling_reason")
        # #2316: a truncated (output-token-ceiling) 0-commit run. Same
        # column, same mutual exclusivity — see the identical chain in
        # `reconcile_completed_assignments`.
        or entry.get("truncation_reason")
        # #2638: a wall-clock runtime-ceiling or host-sleep kill. Same
        # column, same mutual exclusivity — see the identical chain in
        # `reconcile_completed_assignments`.
        or entry.get("runtime_ceiling_reason")
        or entry.get("host_sleep_reason")
    )
    if not reason or not assignment_id:
        return
    try:
        from coord.state import set_assignment_failure_reason  # noqa: PLC0415

        set_assignment_failure_reason(assignment_id, reason)
    except Exception:  # noqa: BLE001
        pass


def reconcile(board: Board, config: Config) -> list[str]:
    """Poll agent servers and update board assignments that have finished.

    Returns assignment_ids whose status changed or were backfilled.
    """
    machines_by_name = {m.name: m for m in config.machines}

    # Collect all machines we need to query: those with active assignments
    # OR completed assignments missing branch info.
    machines_to_query: set[str] = set()
    for a in board.active:
        machines_to_query.add(a.machine_name)
    for a in board.completed:
        if a.branch is None and a.assignment_id is not None:
            machines_to_query.add(a.machine_name)

    # Query each machine once and cache the result.
    agent_completed: dict[str, dict] = {}
    reachable_machines: set[str] = set()
    for machine_name in machines_to_query:
        machine = machines_by_name.get(machine_name)
        if machine is None:
            continue
        status = _query_agent(machine.host)
        if status is None:
            continue
        reachable_machines.add(machine_name)
        for e in status.get("completed", []):
            agent_completed[e["id"]] = e

    changed: list[str] = []
    newly_failed: list = []  # assignments that just transitioned to failed

    # Sweep for dead interactive (--interactive / claude-pty) sessions before
    # processing agent-based assignments.  A killed tmux session leaves a
    # stale "running" board row + orphaned worktree that blocks relaunch.
    # Reaping here ensures ``coord resume`` / ``coord notify`` clean up
    # without requiring the user to first run ``coord reattach``.
    from coord.interactive import (  # noqa: PLC0415
        reap_stale_interactive_sessions,
        reap_stale_remote_interactive_sessions,
    )

    reaped = reap_stale_interactive_sessions(board, config)
    changed.extend(reaped)

    # #588: probe remote claude-pty sessions older than the configured timeout
    # threshold.  The local reaper above skips these; this sweep SSHes to the
    # remote host and finalizes sessions whose tmux has exited.
    remote_reaped = reap_stale_remote_interactive_sessions(board, config)
    changed.extend(remote_reaped)

    # Pass 1: transition active assignments that have finished.
    for a in board.active[:]:
        if a.assignment_id is None:
            continue

        # Track unreachable agents for stale detection
        if a.machine_name in machines_to_query and a.machine_name not in reachable_machines:
            a.unreachable_count = getattr(a, "unreachable_count", 0) + 1
            stale_threshold = getattr(config.concurrency, "stale_threshold", 3)
            if a.unreachable_count >= stale_threshold:
                board.mark_failed_by_id(a.assignment_id)
                newly_failed.append(a)
                changed.append(a.assignment_id)
            continue
        elif a.machine_name in reachable_machines:
            a.unreachable_count = 0

        entry = agent_completed.get(a.assignment_id)
        if entry is None:
            continue
        branch = entry.get("branch")
        # #1534: read the status through the `done`-refusal helper so a
        # usage-limit kill the agent mislabelled `done` lands in the `failed`
        # branch below (which stamps `failure_reason` via
        # `_record_usage_limit_reason`) instead of being recorded as a clean,
        # unmarked completion that auto-dispatches a review.
        agent_status = effective_agent_status(entry)
        if agent_status == "done":
            done = board.mark_done_by_id(
                a.assignment_id,
                finished_at=entry.get("finished_at"),
                branch=branch,
            )
            if done is not None:
                if done.type in WORK_LIKE_TYPES:
                    # Always mark work(-like) completions as pending review so
                    # the dispatch loop below (and future reconcile passes)
                    # can pick them up reliably. #930: "mock-author" is
                    # work-like too — see WORK_LIKE_TYPES. (#1426: Test-stage
                    # dispatch no longer needs its own "just transitioned"
                    # list — dispatch_pending_smoke scans the full completed
                    # backlog, the same shape as dispatch_pending_reviews.)
                    done.review_state = "pending"
                elif done.type == "review":
                    # #1566: `mark_done_by_id` just stamped `done.status =
                    # "done"` — correct it to "finalizing". The review
                    # AGENT finished, but the verdict is parsed + persisted
                    # by `coord notify` (a separate, slower step — see the
                    # matching comment in `reconcile_completed_assignments`
                    # above), so calling this row "done" before that lands
                    # would show a finished review with no verdict, which is
                    # indistinguishable from a dropped one. `orig.review_state`
                    # below is intentionally left "done" immediately (that
                    # field means "the review PROCESS is over", not "verdict
                    # known" — see #1584 — and `drive_state.TERMINAL_STATUSES`
                    # not listing "finalizing" is what keeps `coord drive`
                    # from misreading this row as a dead end in the meantime).
                    done.status = "finalizing"
                    # A review finished — update the original work assignment.
                    orig_id = done.review_of_assignment_id
                    if orig_id:
                        orig = board.find_by_id(orig_id)
                        if orig is not None:
                            orig.review_state = "done"
                elif done.type == "conflict-fix":
                    # #241: re-enqueue the parent merge entry for retry —
                    # UNLESS #2565's semantic-marker check (run inside
                    # `_on_conflict_fix_done` regardless of `succeeded`)
                    # finds the worker actually gave up. Pass `agent_entry`/
                    # `board`/`config` (mirroring the advisory/failed call
                    # sites below) so that check can run; a clean exit alone
                    # is not proof of a real resolution.
                    _on_conflict_fix_done(
                        done, succeeded=True,
                        agent_entry=entry, board=board, config=config,
                    )
        elif agent_status == "advisory":
            # #448: worker exited cleanly but pushed 0 commits. Move to
            # completed with status "advisory" — NOT "failed" — so that
            # auto_reassign does not loop on it. Review is also skipped
            # because there is no code to review on the branch.
            done = board.mark_done_by_id(
                a.assignment_id,
                finished_at=entry.get("finished_at"),
                branch=branch,
            )
            if done is not None:
                # mark_done_by_id sets status="done"; correct it to "advisory".
                done.status = "advisory"
                if done.type in WORK_LIKE_TYPES:
                    # No code pushed → nothing to review. Set review_state to
                    # "advisory" so the review-dispatch loop skips this entry.
                    done.review_state = "advisory"
                elif done.type == "review":
                    # Defensive (should not occur after Bug 2 fix): review
                    # workers that somehow hit advisory still advance the
                    # original work assignment's review_state.
                    orig_id = done.review_of_assignment_id
                    if orig_id:
                        orig = board.find_by_id(orig_id)
                        if orig is not None:
                            orig.review_state = "done"
                elif done.type == "conflict-fix":
                    # A conflict-fix with 0 commits didn't resolve anything.
                    _on_conflict_fix_done(
                        done, succeeded=False,
                        agent_entry=entry, board=board, config=config,
                    )
                _record_usage_limit_reason(a.assignment_id, entry)
            # NOTE: do NOT add to newly_failed — prevents auto_reassign loop.
        elif agent_status == "refused_policy":
            # #2234: worker exited cleanly, pushed 0 commits, and its own
            # final message cited a standing repo-rule prohibition — the
            # #2195 shape. Mirrors the "advisory" branch immediately above
            # (move to completed, skip review — there is no code to review),
            # EXCEPT the status is preserved distinctly rather than folded
            # into "advisory": the whole point is that this reads as a
            # routing decision ("needs the coordinator"), not as "the worker
            # got stuck or found nothing to do".
            done = board.mark_done_by_id(
                a.assignment_id,
                finished_at=entry.get("finished_at"),
                branch=branch,
            )
            if done is not None:
                # mark_done_by_id sets status="done"; correct it.
                done.status = "refused_policy"
                if done.type in WORK_LIKE_TYPES:
                    # No code pushed → nothing to review.
                    done.review_state = "advisory"
                _record_usage_limit_reason(a.assignment_id, entry)
            # NOTE: do NOT add to newly_failed — a policy refusal is never a
            # candidate for auto_reassign; retrying it reproduces the
            # identical, correct refusal every time.
        else:
            # Defensive: don't downgrade a DB-done assignment to failed when
            # the agent reports cancelled (e.g. after POST /cancel cleanup
            # of a hung reap). The work succeeded; cancellation here is
            # bookkeeping noise.
            if (agent_status == "cancelled"
                    and (a.status or "").lower() == "done"):
                continue
            failed = board.mark_failed_by_id(
                a.assignment_id,
                finished_at=entry.get("finished_at"),
            )
            if failed is not None:
                newly_failed.append(failed)
                if failed.type == "conflict-fix":
                    # #241: the auto-fix didn't work — escalate.  #1291: a
                    # SEMANTIC give-up buys one stronger attempt first.
                    _on_conflict_fix_done(
                        failed, succeeded=False,
                        agent_entry=entry, board=board, config=config,
                    )
                elif failed.type == "review":
                    # #1584: a review worker that died (transient API error,
                    # network drop, ...) before producing a verdict is now
                    # correctly recorded FAILED (not the pre-#1584 silent
                    # `done`) — but without this, the ORIGINAL work row's
                    # `review_state` is left at "dispatched" forever, exactly
                    # like the `done` (above) and `advisory` branches this
                    # mirrors would leave it if THEY skipped this update.
                    # "done" (not "failed") to match the existing
                    # `coord._board_mapping.infer_review_state` convention,
                    # which already treats a review row's `status in ("done",
                    # "failed")` identically when inferring this same field —
                    # the review PROCESS is over either way; `review_verdict`
                    # (left empty here) is what actually distinguishes "no
                    # verdict" from an approval, and `coord/drive.py`'s
                    # `_decide_review` reads `review_status`/`review_verdict`
                    # directly rather than this field for that distinction.
                    orig_id = failed.review_of_assignment_id
                    if orig_id:
                        orig = board.find_by_id(orig_id)
                        if orig is not None:
                            orig.review_state = "done"
                _record_usage_limit_reason(a.assignment_id, entry)
                # #2131: same escalation as the daemon's passive tick — this
                # is `coord resume`'s path into the identical transition.
                _escalate_spend_ceiling_best_effort(a, entry)
        changed.append(a.assignment_id)

    # Open PRs for completed work-like assignments still missing one (#2844).
    # Runs BEFORE smoke/review dispatch below so the pull_request CI run
    # starts overlapping the smoke leg instead of waiting for review dispatch
    # to open the PR itself once smoke finishes. Idempotent — see
    # dispatch_pending_pr_opens's docstring.
    from coord.review import dispatch_pending_pr_opens

    for opened in dispatch_pending_pr_opens(board, config):
        if opened.assignment_id is not None:
            changed.append(opened.assignment_id)

    # Dispatch pending reviews for all completed work assignments.
    # We iterate board.completed (not just newly-done) so that a failed
    # dispatch on a previous reconcile pass is retried here automatically.
    #
    # #465: review fires immediately on work completion — no manual smoke
    # prerequisite (the interactive smoke gate now lives on merge).
    # dispatch_pending_reviews() bounds this with a per-pass cap + surge gate
    # (flood guard, incident 2026-06-08) and applies the #459 active-fix
    # dedupe, so a backlog unmasking can't flood metered reviews.
    from coord.review import dispatch_pending_reviews, dispatch_scoped_reviews_for_queue

    for review in dispatch_pending_reviews(board, config):
        if review.assignment_id is not None:
            changed.append(review.assignment_id)

    # #1476: a conflict-fix rebase can void an already-approved review by
    # changing content (patch-id mismatch) without any other new commit —
    # dispatch a re-review SCOPED to just the resolution delta instead of
    # leaving the merge entry blocked until a human notices and forces a
    # full re-review. Independent of dispatch_pending_reviews above (that
    # one looks at completed WORK rows; this one looks at PENDING merge
    # queue entries whose approval a rebase just voided).
    for review in dispatch_scoped_reviews_for_queue(board, config):
        if review.assignment_id is not None:
            changed.append(review.assignment_id)

    # Auto-queue smoke tests for any completed work-like assignment still
    # missing a test verdict. Independent of review dispatch — both can fire
    # for the same completion.
    #
    # #1426: routed through `dispatch_pending_smoke`, the single choke point
    # `reconcile()` and `coord notify` both call (mirroring
    # `dispatch_pending_reviews` for the Review stage) — it scans the FULL
    # completed backlog, not just this pass's `newly_done_work`, so a row
    # that was missed on an earlier pass (e.g. no capable machine existed
    # yet) is retried here automatically instead of staying stuck forever.
    # `dispatch_pending_smoke` itself enforces `smoke_tests.auto_queue`, the
    # #685 per-issue test-mode gate (test-mode:smoke skips auto-dispatch —
    # the TUI offers the interactive smoke agent instead), and the
    # has_active_followup dedupe.
    from coord.smoke import dispatch_pending_smoke

    for smoke in dispatch_pending_smoke(board, config):
        if smoke.assignment_id is not None:
            changed.append(smoke.assignment_id)

    # Auto-reassign failed work assignments to a different machine.
    if newly_failed and getattr(config.concurrency, "auto_reassign", False):
        # #1590: one decision point for "was this the weather or the work".
        from coord.failure_class import classify_failure  # noqa: PLC0415

        for failed_a in newly_failed:
            if getattr(failed_a, "type", "work") != "work":
                continue
            # #1461 review finding 1: a usage-limit kill is an account-wide
            # exhausted budget, not a per-machine defect — re-dispatching it
            # onto a *different* machine still burns the same subscription
            # limit and is guaranteed to die the same way until the reset.
            # Check the just-seen agent entry (this pass; `_record_usage_
            # limit_reason` below only writes through to the DB / board
            # service, it does not mutate this in-memory `failed_a`) AND the
            # already-persisted `failure_reason` (a prior pass already
            # stamped it, e.g. after a race with `reconcile_completed_
            # assignments`'s own tick).
            #
            # #1590 deliberately does NOT widen this skip to every
            # `environmental` class: an API 5xx/network failure genuinely can
            # be machine-local (one agent host's DNS, one flaky link), so
            # moving it to another machine is a reasonable first move and the
            # bounded `auto_reassign` retry still terminates. Only the
            # usage limit is provably account-wide, and only it is skipped.
            entry = agent_completed.get(failed_a.assignment_id)
            classification = classify_failure(
                usage_limit_reason=(entry or {}).get("usage_limit_reason"),
                failure_reason=getattr(failed_a, "failure_reason", None),
            )
            if classification.is_usage_limit:
                continue
            # #2131: a spend-ceiling kill must NEVER auto-reassign. The leg
            # was killed precisely because it was burning money, and nothing
            # about moving it to another machine makes it cheaper — an
            # auto-retry here re-spends the whole ceiling, unattended, which
            # is the exact failure this issue exists to prevent. It requires
            # a human decision (`coord retry --acknowledge-cost`), and the
            # escalation record written above is how they hear about it.
            # Checked against both the just-seen agent entry (this pass) and
            # the already-persisted `failure_reason` (a prior pass stamped
            # it), mirroring the usage-limit skip above.
            from coord.spend_ceiling import is_spend_ceiling_reason  # noqa: PLC0415

            if is_spend_ceiling_reason(
                (entry or {}).get("spend_ceiling_reason")
            ) or is_spend_ceiling_reason(
                getattr(failed_a, "failure_reason", None)
            ):
                continue
            # #2323: thread the issue's cached labels through so
            # auto-reassign resolves `providers.labels` the same way a
            # first dispatch (and `coord retry`) does — best-effort local
            # cache read (never a GitHub call from this passive tick); an
            # uncached issue falls back to label-blind resolution, same as
            # passing `None`.
            from coord.state import get_cached_issue_labels  # noqa: PLC0415

            cached_labels = get_cached_issue_labels(
                failed_a.repo_name, failed_a.issue_number,
            )
            try:
                reassigned = _reassign(
                    failed_a, board, config, issue_labels=cached_labels,
                )
            except RetryProviderMismatch:
                # Refuse rather than substitute (#2323) — leave the failed
                # assignment for human attention (`coord retry`) instead of
                # silently auto-moving it to a different provider.
                continue
            if reassigned is not None and reassigned.assignment_id is not None:
                changed.append(reassigned.assignment_id)

    # Pass 2: backfill branch on completed assignments that are missing it.
    for a in board.completed:
        if a.branch is not None or a.assignment_id is None:
            continue
        entry = agent_completed.get(a.assignment_id)
        if entry is None:
            continue
        branch = entry.get("branch")
        if branch:
            a.branch = branch
            changed.append(a.assignment_id)

    return changed


def _post_human_required_comment_raw(
    entry: QueuedMerge,
    fix_assignment_id: str,
    machine_name: str,
    *,
    semantic_escalation_note: str | None = None,
) -> None:
    """Notify the user on GitHub that a conflict-fix worker gave up.

    *semantic_escalation_note* (#2566): when the conflict-fix worker judged
    the conflict SEMANTIC but ``pipeline.escalate_semantic_conflicts`` is
    off, callers pass a short explanation so the comment says *why* there
    was no tier-2 attempt instead of reading as though escalation ran and
    failed.
    """
    from coord import github_ops  # noqa: PLC0415

    body = (
        "## Conflict-fix worker could not auto-resolve\n\n"
        f"Worker `{fix_assignment_id}` on "
        f"`{machine_name}` attempted to rebase "
        f"`{entry.branch}` onto `{entry.target_branch}` and exited "
        "non-zero. The merge queue entry is now `HUMAN_REQUIRED`.\n\n"
        f"**Last error:** `{entry.error or 'unknown'}`\n\n"
        + (f"{semantic_escalation_note}\n\n" if semantic_escalation_note else "")
        + "Manual resolution required: rebase the branch locally and "
        "`git push --force-with-lease`, then re-run `coord merge`. The "
        "coordinator will not re-dispatch a conflict-fix for this entry "
        "in the current session."
    )
    try:
        github_ops.post_issue_comment(entry.repo_github, entry.issue_number, body)
    except Exception as exc:  # noqa: BLE001 — best-effort notification
        import logging  # noqa: PLC0415
        logging.warning(
            "could not post HUMAN_REQUIRED comment on %s#%d: %s",
            entry.repo_github, entry.issue_number, exc,
        )


def _post_semantic_escalation_comment(
    entry: QueuedMerge,
    *,
    model: str,
    escalated_assignment_id: str,
    machine_name: str,
) -> None:
    """#1291: tell the operator a SEMANTIC merge is being attempted.

    A semantic auto-resolution is higher-trust than a mechanical rebase, so
    it is announced up front — the point is that the human reviews the diff
    rather than discovering it after the merge.
    """
    from coord import github_ops  # noqa: PLC0415

    body = (
        "## Semantic conflict — escalated for one stronger attempt\n\n"
        f"The conflict-fix worker judged the conflict on `{entry.branch}` → "
        f"`{entry.target_branch}` **semantic** and stopped rather than "
        f"guess.  The coordinator has dispatched ONE escalated attempt with "
        f"model `{model}` (assignment `{escalated_assignment_id}` on "
        f"`{machine_name}`).\n\n"
        f"**Last error:** `{entry.error or 'unknown'}`\n\n"
        "⚠️ **Review this diff before it merges.** A semantic resolution "
        "reconciles two different intents — it is a judgement call, not a "
        "mechanical rebase.  Every gate still applies (tests, CI, "
        "`verify-merge`, review); nothing is force-merged.  If this attempt "
        "fails, the merge entry goes to `HUMAN_REQUIRED` — there is no "
        "second escalation."
    )
    try:
        github_ops.post_issue_comment(entry.repo_github, entry.issue_number, body)
    except Exception as exc:  # noqa: BLE001 — best-effort notification
        import logging  # noqa: PLC0415
        logging.warning(
            "could not post semantic-escalation comment on %s#%d: %s",
            entry.repo_github, entry.issue_number, exc,
        )


def _try_semantic_escalation(
    entry: QueuedMerge,
    *,
    board: Board | None,
    config: Config | None,
    machine_name: str,
    stuck_summary: str | None,
) -> "Assignment | None":
    """Dispatch the one escalated (semantic) conflict-fix attempt, if allowed.

    Returns the escalated assignment, or ``None`` when the feature is off,
    the plumbing isn't available, this entry already had its one escalation,
    or dispatch failed — in every ``None`` case the caller falls through to
    today's HUMAN_REQUIRED behaviour.

    The "is the feature off" question is delegated to
    :func:`coord.conflict_fix.semantic_escalation_disabled` rather than
    re-derived here, so this function and the HUMAN_REQUIRED message built
    in :func:`on_conflict_fix_done` can never disagree about *why* no
    escalation was attempted (#2566 review).
    """
    if board is None or config is None:
        return None

    from coord.conflict_fix import (  # noqa: PLC0415
        dispatch_conflict_fix,
        semantic_escalation_disabled,
    )

    if semantic_escalation_disabled(config):
        return None

    pipeline = getattr(config, "pipeline", None)
    model = getattr(pipeline, "semantic_conflict_model", None) or "fable"
    try:
        return dispatch_conflict_fix(
            entry,
            board,
            config,
            prefer_machine=machine_name or None,
            semantic=True,
            model=model,
            stuck_summary=stuck_summary,
        )
    except Exception as exc:  # noqa: BLE001 — never break reconcile on this
        import logging  # noqa: PLC0415
        logging.warning("semantic escalation dispatch failed: %s", exc)
        return None


def on_conflict_fix_done(
    *,
    parent_assignment_id: str,
    fix_assignment_id: str,
    machine_name: str,
    succeeded: bool,
    semantic: bool = False,
    board: Board | None = None,
    config: Config | None = None,
    stuck_summary: str | None = None,
    usage_limit_reason: str | None = None,
) -> None:
    """Update the parent merge entry after a conflict-fix worker finishes.

    On *succeeded*: the merge entry is reset to PENDING so the next
    ``coord merge`` retries.  On failure: marked HUMAN_REQUIRED so the TUI
    can surface "manual resolution required", and a comment is posted on
    the underlying issue so the user is notified outside the TUI too.

    #2566: when *semantic* is ``True`` and the tier-2 escalation didn't
    fire specifically because ``pipeline.escalate_semantic_conflicts`` is
    off (as opposed to it already having had its one escalation this
    entry, or dispatch itself failing), both the parked entry's ``error``
    and the GitHub comment say so explicitly — see
    :func:`coord.conflict_fix.semantic_escalation_disabled`. Otherwise the
    dark default is indistinguishable from "escalation ran and failed",
    and an operator reading ``conflict_fix.py`` has no way to tell the
    tier is switched off short of grepping ``coordinator.yml``.

    *usage_limit_reason* (#1461 review finding 2): when the conflict-fix
    worker was killed by the account's usage limit mid-fix, it did not
    actually fail to resolve anything — frame the parked entry as "wait for
    the reset", not "manual rebase required", so the operator isn't sent
    chasing a defect that doesn't exist. Still lands in HUMAN_REQUIRED
    (rather than auto-retrying, which would just burn more of the same
    exhausted budget — the #1461 "do not auto-retry immediately" rule
    applies here too), just with an accurate message.

    Called from both ``reconcile()`` (via mark_done/failed) and
    ``coord notify`` (via post_transition) — both paths must trigger this
    so the re-enqueue fires regardless of which polling command runs first.
    """
    from coord import merge_queue as mq  # noqa: PLC0415

    items = mq.load_queue()
    changed = False
    failed_entry: mq.QueuedMerge | None = None
    failed_entry_note: str | None = None
    escalated: tuple[mq.QueuedMerge, str, str, str] | None = None
    for entry in items:
        if entry.assignment_id != parent_assignment_id:
            continue
        if succeeded:
            entry.state = mq.PENDING
            entry.error = None
            entry.last_attempt = None
        else:
            existing_error = entry.error or "conflict-fix failed"
            if usage_limit_reason:
                entry.state = mq.HUMAN_REQUIRED
                entry.error = (
                    f"{existing_error}; conflict-fix worker was killed by "
                    f"the account's {usage_limit_reason} — not a real "
                    "conflict. Wait for the reset, then re-run `coord "
                    "merge` to retry unchanged."
                )
                failed_entry = entry
            else:
                # #1291: a SEMANTIC give-up gets ONE escalated attempt from a
                # stronger model before the entry is parked.  Everything
                # else — and a second semantic failure (the escalated
                # attempt is itself a conflict-fix row, so
                # `has_prior_semantic_escalation` blocks it) — behaves
                # exactly as before.
                fix = (
                    _try_semantic_escalation(
                        entry,
                        board=board,
                        config=config,
                        machine_name=machine_name,
                        stuck_summary=stuck_summary,
                    )
                    if semantic
                    else None
                )
                if fix is not None:
                    # Stay in CONFLICT, not HUMAN_REQUIRED — the escalated
                    # worker is in flight.  If it fails, this hook runs
                    # again and the escalation guard sends the entry to
                    # HUMAN_REQUIRED.
                    entry.state = mq.CONFLICT
                    model = fix.model or "escalated model"
                    entry.error = (
                        f"{existing_error}; semantic conflict escalated to "
                        f"{model} (assignment {fix.assignment_id}) — review "
                        "the resolution diff before merge."
                    )
                    escalated = (
                        entry, model, fix.assignment_id or "",
                        fix.machine_name or "",
                    )
                else:
                    from coord.conflict_fix import (  # noqa: PLC0415
                        semantic_escalation_disabled,
                    )

                    entry.state = mq.HUMAN_REQUIRED
                    if semantic and semantic_escalation_disabled(config):
                        # #2566: the worker judged this SEMANTIC and there
                        # is a whole second tier built for exactly this
                        # (#1291) — but `pipeline.escalate_semantic_
                        # conflicts` ships dark, so it never ran. Say so,
                        # instead of reading as "escalation tried and
                        # failed" indistinguishably from every other
                        # conflict-fix give-up.
                        failed_entry_note = (
                            "No tier-2 attempt was made: this conflict-fix "
                            "worker judged the conflict **semantic**, but "
                            "`pipeline.escalate_semantic_conflicts` is "
                            "disabled (the default) — set it to `true` in "
                            "`coordinator.yml` to allow one escalated "
                            "attempt from a stronger model on future "
                            "semantic conflicts."
                        )
                        entry.error = (
                            f"{existing_error}; semantic conflict, but "
                            "pipeline.escalate_semantic_conflicts is "
                            "disabled — no tier-2 attempt made. Manual "
                            "rebase required."
                        )
                    else:
                        entry.error = (
                            f"{existing_error}; conflict-fix worker did not "
                            "resolve. Manual rebase required."
                        )
                    failed_entry = entry
        changed = True
    if changed:
        mq.save_queue(items)

    if escalated is not None:
        esc_entry, esc_model, esc_id, esc_machine = escalated
        _post_semantic_escalation_comment(
            esc_entry,
            model=esc_model,
            escalated_assignment_id=esc_id,
            machine_name=esc_machine,
        )

    if failed_entry is not None:
        _post_human_required_comment_raw(
            entry=failed_entry,
            fix_assignment_id=fix_assignment_id,
            machine_name=machine_name,
            semantic_escalation_note=failed_entry_note,
        )


def _on_conflict_fix_done(
    fix_assignment: Assignment,
    *,
    succeeded: bool,
    agent_entry: dict | None = None,
    board: Board | None = None,
    config: Config | None = None,
) -> None:
    """Thin wrapper used by the reconcile() loop.

    It also asks the worker's log whether it gave up on a SEMANTIC conflict
    (the ``coord:conflict=semantic`` marker), which — with
    ``pipeline.escalate_semantic_conflicts`` on — buys one escalated attempt
    instead of an immediate HUMAN_REQUIRED (#1291).

    #2565: this check runs regardless of *succeeded*, not just on failure. A
    ``claude -p`` worker ends its turn (and the harness reports ``exit_code
    0``/``status done``) the same way whether it resolved the conflict or
    gave up with a STUCK line — the briefing's "exit non-zero" instruction
    asks for something outside the worker's control, so a clean exit can
    NOT be trusted as "succeeded" on its own. The marker in the transcript
    is the only reliable signal; when it's present the give-up is real even
    though the agent-reported status is ``done``, so *succeeded* is
    downgraded to ``False`` before calling :func:`on_conflict_fix_done` —
    otherwise the entry would be reset to PENDING and silently retried
    against the identical, already-diagnosed conflict.

    #1461 review finding 2: when the conflict-fix worker was itself killed
    by the account's usage limit (flagged on *agent_entry* by
    ``AgentServer._reap`` — the same signal ``_record_usage_limit_reason``
    stamps onto ordinary work assignments), it didn't actually fail to
    resolve anything. Skip the SEMANTIC-conflict check (there is nothing to
    diagnose in the transcript — it was cut off, not concluded) and pass the
    reason through so the parked entry gets an accurate message instead of
    "manual rebase required".
    """
    parent_id = fix_assignment.review_of_assignment_id
    if not parent_id:
        return

    usage_limit_reason = (agent_entry or {}).get("usage_limit_reason")

    semantic = False
    stuck_summary: str | None = None
    if (
        not usage_limit_reason
        and board is not None
        and config is not None
    ):
        semantic, stuck_summary = _semantic_verdict(
            fix_assignment, agent_entry, config,
        )
        if semantic:
            succeeded = False

    on_conflict_fix_done(
        parent_assignment_id=parent_id,
        fix_assignment_id=fix_assignment.assignment_id or "",
        machine_name=fix_assignment.machine_name or "",
        succeeded=succeeded,
        semantic=semantic,
        board=board,
        config=config,
        stuck_summary=stuck_summary,
        usage_limit_reason=usage_limit_reason,
    )


def _semantic_verdict(
    fix_assignment: Assignment,
    agent_entry: dict | None,
    config: Config,
) -> tuple[bool, str | None]:
    """(is_semantic, stuck line) for a finished conflict-fix worker.

    Best-effort — any failure to read the log means "not semantic", which
    preserves the pre-#1291 HUMAN_REQUIRED path.
    """
    from coord.conflict_fix import detect_semantic_conflict  # noqa: PLC0415

    log_path = (agent_entry or {}).get("log_path")
    machine = next(
        (m for m in config.machines if m.name == fix_assignment.machine_name), None,
    )
    try:
        semantic = detect_semantic_conflict(
            log_path=log_path,
            host=machine.host if machine is not None else None,
            assignment_id=fix_assignment.assignment_id,
        )
    except Exception:  # noqa: BLE001 — never break reconcile on a log read
        return False, None

    stuck_summary: str | None = None
    if semantic:
        progress = (agent_entry or {}).get("progress") or {}
        stuck_summary = progress.get("stuck")
        if not stuck_summary and log_path:
            try:
                from coord.progress import parse_progress  # noqa: PLC0415
                stuck_summary = parse_progress(log_path).stuck
            except Exception:  # noqa: BLE001
                stuck_summary = None
    return semantic, stuck_summary


def _extract_issue_number(branch: str) -> int | None:
    """Extract N from ``issue-{N}-*`` branch names; returns None if no match."""
    m = re.match(r"issue-(\d+)-", branch)
    return int(m.group(1)) if m else None


def close_stale_prs(
    config: Config,
    *,
    board: Board | None = None,
    repo: str | None = None,
    issue: int | None = None,
    dry_run: bool = False,
    skip_dormant_repos: bool = False,
) -> list[str]:
    """Close open PRs whose work is already on main or whose issue is closed.

    Sweeps every coord-tracked repo (filtered by *repo* / *issue* when given)
    for OPEN PRs with ``issue-{N}-*`` head branches.  Each PR is classified as
    stale when either condition holds:

      1. The linked issue N is CLOSED on GitHub.
      2. The branch has 0 commits ahead of the repo's default branch (catches
         fast-forward merges; squash/rebase cases are caught by condition 1
         because coord closes the issue when squash-merging).

    Stale PRs are closed with an explanatory comment.  Non-stale PRs are left
    untouched.  *dry_run* lists what would change without writing.  Idempotent.

    #2994: when *skip_dormant_repos* is set (the daemon tick's opt-in — see
    ``coord.serve_app._reconcile_merges_tick``; a manual ``coord
    reconcile-merges`` leaves this False), a repo with no open assignment, no
    drive-queue entry, and no coord-authored open PR (``coord.repo_dormancy.
    should_skip_sweep``) is skipped this call rather than costing a
    ``list_open_prs`` call — bounded by ``coord.repo_dormancy.
    DORMANT_SWEEP_FLOOR_S`` so it's still swept eventually. Requires *board*
    to evaluate; with *board* left ``None`` this is a no-op regardless of
    *skip_dormant_repos* (no activity signal to check against, so never
    skip).

    Note: the floor is recorded as soon as a repo is deemed due, before
    ``list_open_prs`` is even attempted, and it stays recorded if that call
    then raises (deliberate — see ``coord.repo_dormancy.record_swept``'s
    docstring: it tracks "a real gh call was spent", not "and it worked").
    One operator-facing consequence: a repo that is persistently failing
    (renamed, deleted, secondary-rate-limited) goes quiet for up to
    ``DORMANT_SWEEP_FLOOR_S`` between retries rather than being retried
    sooner, same as a genuinely idle repo would be.
    """
    from coord import github_ops  # noqa: PLC0415

    actions: list[str] = []
    dormant_skipped = 0

    # #3063: a (repo_name, issue_number) -> work-like `type` lookup, same
    # shape and same WORK_LIKE_TYPES scope as reconcile_board_merges sweep
    # (e)'s `work_type_for` below. A test-author/mock-author row's PR head
    # branch is named `issue-{N}-*` where N is the milestone's *tracking*
    # issue, not this row's own deliverable (SEALED_PATH_AUTHOR_TYPES,
    # coord/models.py) — a tracking epic is closed for most of a milestone's
    # life while slices are still being authored against it, so trusting
    # `issue_is_closed` below for one of these PRs closes it the instant the
    # epic closes regardless of whether THIS PR's own branch ever landed.
    # `merge_queue.enqueue_approved_work` already applies
    # `trust_issue_closed_for` on the way in (#2639); without this lookup,
    # this sweep undid that on the way back out — closing the PR the very
    # next tick, `prune_stale_queue_entries` deleting the queue row, and
    # auto-drain re-opening a new PR next tick: an infinite open/close loop
    # (#3063). Built once per call from *board* when given; ``None`` when
    # *board* is omitted (e.g. a direct/test call) degrades to today's
    # behaviour (unconditionally trusting `issue_is_closed`) via
    # `trust_issue_closed_for(None) == True`.
    assignment_type_by_issue: dict[tuple[str, int], str] = {}
    if board is not None:
        for _a in board.active + board.completed:
            if _a.type in WORK_LIKE_TYPES and _a.issue_number is not None:
                key = (_a.repo_name, _a.issue_number)
                if key not in assignment_type_by_issue:
                    assignment_type_by_issue[key] = _a.type

    for repo_cfg in config.repos:
        if repo is not None and repo_cfg.name != repo:
            continue

        if skip_dormant_repos and board is not None:
            from coord import repo_dormancy  # noqa: PLC0415

            if repo_dormancy.should_skip_sweep(
                repo_cfg.name, board, repo_dormancy.KIND_PRS
            ):
                dormant_skipped += 1
                continue
            repo_dormancy.record_swept(repo_cfg.name, repo_dormancy.KIND_PRS)

        try:
            open_prs = github_ops.list_open_prs(repo_cfg.github)
        except Exception as exc:  # noqa: BLE001
            actions.append(
                f"skip stale-PR sweep for {repo_cfg.name}: could not list PRs ({exc})"
            )
            continue

        default_branch = repo_cfg.default_branch or "main"
        # #934: per-run cache for the issue -> milestone-number lookup, since
        # this loop re-derives the base branch per-PR below (a milestone
        # issue's stale-PR base is `feature/ms-NN`, not the repo's flat
        # `default_branch`). Only populated when the repo opted in.
        milestone_cache: dict = {}

        for pr in open_prs:
            branch = pr.get("headRefName") or ""
            pr_number = pr.get("number")
            if not branch or pr_number is None:
                continue

            issue_number = _extract_issue_number(branch)
            if issue_number is None:
                continue  # not a coord-managed branch — skip
            if issue is not None and issue_number != issue:
                continue

            # Fail-safe classification: when uncertain, leave the PR open.
            stale_reason: str | None = None

            # #934: this issue's actual base — `feature/ms-NN` when it
            # belongs to a milestone and the repo opted into the git model,
            # `repo_cfg.default_branch` (today's behavior) otherwise. The
            # milestone lookup itself is skipped (no `gh` call) when the
            # repo hasn't opted in.
            pr_base = default_branch
            if getattr(repo_cfg, "develop_branch", None):
                from coord.branch_model import (  # noqa: PLC0415
                    fetch_issue_milestone_number,
                    resolve_base_branch,
                )

                milestone_number = fetch_issue_milestone_number(
                    repo_cfg.github, issue_number, cache=milestone_cache,
                )
                pr_base = resolve_base_branch(repo_cfg, milestone_number)

            trust_issue_closed = trust_issue_closed_for(
                assignment_type_by_issue.get((repo_cfg.name, issue_number))
            )
            if trust_issue_closed and github_ops.issue_is_closed(
                repo_cfg.github, issue_number
            ):
                stale_reason = f"issue #{issue_number} is closed"
            elif github_ops.branch_is_fully_merged(
                repo_cfg.github, branch, pr_base
            ):
                stale_reason = f"all commits already on {pr_base}"

            if stale_reason is None:
                continue  # live PR — leave it alone

            actions.append(
                f"close PR #{pr_number} "
                f"({repo_cfg.name} #{issue_number}, {branch}): {stale_reason}"
                + (" [dry-run]" if dry_run else "")
            )

            if not dry_run:
                comment = (
                    f"Closing stale PR — {stale_reason}. "
                    f"The work for issue #{issue_number} has already landed.\n\n"
                    f"<!-- coord:stale-close issue={issue_number} -->"
                )
                try:
                    github_ops.close_pr(repo_cfg.github, pr_number, comment=comment)
                except Exception as exc:  # noqa: BLE001
                    actions.append(f"  ↳ error closing PR #{pr_number}: {exc}")

    if dormant_skipped:
        # #2994: one aggregate line rather than one per repo — visible
        # without being noisy on a fleet with many idle repos.
        actions.append(
            f"stale-PR sweep: skipped {dormant_skipped} dormant repo(s) "
            "(no open assignment, drive-queue entry, or open PR)"
        )

    return actions


def is_interactive_merge_session(a: object) -> bool:
    """True when *a* is an interactive ``--merge-of`` session (#1110).

    Interactive merge-prep sessions are dispatched with ``type="conflict-fix"``
    — the same type the automated #241 conflict-fix worker uses — so a bare
    ``type`` check can't tell them apart.  What distinguishes them:

    * ``provider_name == "claude-pty"`` — the automated #241 worker runs
      headless ``claude -p`` and never sets this.
    * ``review_of_assignment_id`` is set — both share this, but combined with
      the provider check above it's unambiguous.

    Used by :func:`reconcile_board_merges` (sweep b) and
    :func:`coord.serve_app._reap_merged_sessions_tick` to scope terminal-state
    detection / reaping to interactive merge sessions only, without touching
    automated conflict-fix workers or ordinary work/review/smoke rows.
    """
    return (
        getattr(a, "type", None) == "conflict-fix"
        and getattr(a, "provider_name", None) == "claude-pty"
        and getattr(a, "review_of_assignment_id", None) is not None
    )


def reconcile_board_merges(
    board: Board,
    config: Config,
    *,
    repo: str | None = None,
    issue: int | None = None,
    dry_run: bool = False,
    throttle_false_merge_audit: bool = False,
    skip_dormant_repos: bool = False,
) -> list[str]:
    """Reconcile done work assignments against git/GitHub reality.

    Two conservative sweeps, returning a list of human-readable action (and
    skip) strings:

    (a) #611/#1083 branch backfill — runs over ``status='done'`` rows whose
        ``type`` is in :data:`coord.models.WORK_LIKE_TYPES` (``work``,
        ``mock-author``, ``test-author``).  A remote interactive work session
        (or a headless ``test-author``/``mock-author`` session finalized by
        the #625 passive reconcile tick before its branch was known — #1083)
        can finish ``status=done`` with ``branch=None`` even though it pushed
        ``issue-{N}-*`` to origin, which greys the TUI Start review/test/merge
        buttons (they require a done work assignment WITH a branch) and makes
        ``coord pr <aid>`` refuse outright.  When exactly one remote branch
        matches ``issue-{N}-*`` for the issue, the branch is backfilled via
        :func:`state.update_assignment_branch`.  More than one candidate (or
        none) is left untouched and logged.  #1574: sweep (b) below now shares
        this same ``WORK_LIKE_TYPES`` scope (plus interactive merge sessions,
        #1110) — a landed branch is a landed branch regardless of which
        work-like type authored it; only ``type='review'`` (and other
        non-work-like types) stay out of scope for the terminal-merge check.

    (b) #609/#951 record out-of-band merges — work merged directly on GitHub,
        or a ``merge_queue`` row that drained without flipping the board, is
        never recorded as ``status='merged'`` so the TUI shows a grey merge
        box forever.  When :func:`github_ops.work_is_terminal` reports the
        issue closed OR the PR merged (fail-open), the row is flipped via
        :func:`state.mark_assignment_merged`.  ``work_is_terminal``'s
        issue-closed check needs **no branch**, so this still fires even when
        sweep (a)'s backfill couldn't resolve one (#951) — an unresolved
        branch must not block the issue-closed fast path.  Because every
        finished work assignment defaults to ``review_state='pending'``
        (reconcile's own Pass 1 sets it unconditionally so the review-dispatch
        loop can pick it up), flipping ``status`` alone leaves that ghost
        behind — the row keeps surfacing as "[awaiting review]" forever even
        though it's merged.  So this sweep also clears a lingering
        ``review_state='pending'`` via :func:`state.mark_work_review_settled`
        (#951), mirroring how sweep (e) below settles the sibling
        review/smoke/conflict-fix rows. This only reaches rows still carrying
        ``status='done'`` — a row whose ``status`` already flipped to
        ``'merged'`` in a *prior* reconcile run permanently drops out of this
        sweep's candidate list, so sweep (e) below also matches
        ``type='work' status='merged' review_state='pending'`` to catch those
        (#951 round 2).

    Both sweeps are **conservative**: they never act when uncertain and append a
    skip reason instead.  *repo* filters to a single local repo name.  When
    *dry_run* is True no writes happen (no ``state.update_*`` calls) — the
    actions list still describes what *would* change.  The board objects are
    mutated in place on a real run so a subsequent ``save_board`` agrees with
    the targeted DB writes.

    (This docstring predates sweeps (c)-(g), which grew this function well
    past "two" — see their own inline comments below for what each does.
    Sweep (h), #2639, is DETECTION-ONLY and never mutates the board: it flags
    a ``status='merged'`` row whose branch is still ahead of its base with no
    merged PR at its tip and content that genuinely differs from the base —
    the "already-corrupted rows are undetectable" gap the #2639 review named,
    catching a historical false ``status='merged'`` flip that predates this
    fix (or any future bug shaped like it) rather than leaving it invisible
    to every other diagnostic forever. See its own inline comment for the
    full methodology and false-positive guards.

    #2989 bounds sweep (h)'s candidate set — it used to select every
    ``merged`` work-like row in project history and re-probe all of them on
    the daemon's 30s tick (97% of a pass's ``gh`` calls). Set
    *throttle_false_merge_audit* to run that sweep at most hourly; the
    daemon tick does, a manual ``coord reconcile-merges`` deliberately does
    not. See the inline block above ``_false_merge_candidates``.

    #2994: *skip_dormant_repos* (also daemon-tick-only, also off for a
    manual ``coord reconcile-merges``) is threaded through to sweep (c),
    :func:`close_stale_prs` — see its own docstring — so a repo with no open
    assignment, no drive-queue entry, and no coord-authored open PR doesn't
    cost a ``list_open_prs`` call on every tick either.)
    """
    from coord import github_ops, state  # noqa: PLC0415

    actions: list[str] = []
    terminal_cache: dict = {}
    # One remote-branch listing per repo, fetched lazily and reused.
    branches_by_repo: dict[str, set[str]] = {}

    candidates = [
        a
        for a in board.active + board.completed
        if (a.type in WORK_LIKE_TYPES or is_interactive_merge_session(a))
        and a.status == "done"
        and (repo is None or a.repo_name == repo)
        and (issue is None or a.issue_number == issue)
    ]

    for a in candidates:
        repo_cfg = config.repo(a.repo_name)
        if repo_cfg is None:
            actions.append(
                f"skip {a.assignment_id} ({a.repo_name} #{a.issue_number}): "
                "repo not in config"
            )
            continue

        # (a) #611/#1083 — backfill a missing branch from origin.
        if not a.branch:
            if repo_cfg.github not in branches_by_repo:
                branches_by_repo[repo_cfg.github] = (
                    github_ops.list_remote_branch_names(repo_cfg.github)
                )
            prefix = f"issue-{a.issue_number}-"
            matches = sorted(
                name
                for name in branches_by_repo[repo_cfg.github]
                if name.startswith(prefix)
            )
            if len(matches) == 1:
                branch = matches[0]
                actions.append(
                    f"backfill branch {a.assignment_id} "
                    f"({a.repo_name} #{a.issue_number}) -> {branch}"
                    + (" [dry-run]" if dry_run else "")
                )
                if not dry_run:
                    a.branch = branch
                    state.update_assignment_branch(a.assignment_id or "", branch)
            elif len(matches) > 1:
                actions.append(
                    f"skip backfill {a.assignment_id} "
                    f"({a.repo_name} #{a.issue_number}): "
                    f"{len(matches)} ambiguous branch candidates {matches}"
                )
                # #951: do NOT bail out here — a.branch is still None, but the
                # issue-closed fast path below needs no branch, so give it a
                # chance instead of stranding the row forever.
            else:
                actions.append(
                    f"skip backfill {a.assignment_id} "
                    f"({a.repo_name} #{a.issue_number}): "
                    f"no remote branch matching {prefix}*"
                )
                # #951: same — fall through rather than `continue`.

        # (b) #609/#951 — flip done work whose branch is merged on GitHub, OR
        # whose issue is closed even when no branch could be resolved above
        # (work_is_terminal's issue-closed check needs no branch).  #1083
        # originally scoped this to type='work' only — test-author rows were
        # added to `candidates` above for sweep (a)'s branch backfill alone,
        # with the merged/review-settled semantics here deliberately left
        # out of scope.  #1574: that scope limit meant a `type='test-author'`
        # row (every oracle-loop acceptance slice, and by the same token
        # `type='mock-author'`, #930 Gate A) could never reach `status=
        # 'merged'` no matter how completely its branch landed, since
        # `work_is_terminal` — branch/commit-scoped since #1150 — already
        # answers correctly for these rows too.  There's nothing pipeline-
        # specific about "this branch merged"; widened to the same
        # :data:`coord.models.WORK_LIKE_TYPES` set sweep (a) uses.  #1110:
        # interactive merge sessions (type='conflict-fix',
        # provider_name='claude-pty', review_of_assignment_id set — see
        # :func:`is_interactive_merge_session`) reach 'done' the same way work
        # sessions do, so they get the same terminal-detection sweep so the
        # auto-reaper can pick them up.  Automated #241 conflict-fix workers
        # are deliberately excluded (they never set provider_name='claude-pty').
        # `type='review'` rows never reach this point at all — they aren't in
        # `candidates` (sweep (a) above is also WORK_LIKE_TYPES-scoped).
        # #2639: a `test-author`/`mock-author` row's `issue_number` is always
        # the milestone's *tracking* issue (per-slice issue lives in
        # `for_issue_number`), never something this row's own branch
        # resolves — SEALED_PATH_AUTHOR_TYPES is the exact set for which
        # that's true (see CLOSES_ISSUE_TYPES/SEALED_PATH_AUTHOR_TYPES in
        # coord/models.py). Trusting `issue_is_closed` for those rows means
        # a tracking epic that's closed for most of its life (while slices
        # are still being authored against it) reports EVERY such row
        # terminal the instant the epic closes — regardless of whether this
        # row's own branch ever landed — silently evaporating the pushed
        # slice into `status='merged'` with nothing on GitHub to show for
        # it. Only `pr_is_merged` (branch/commit-scoped, #1150) may decide
        # for these rows; every other work-like type (chiefly `type='work'`,
        # where `issue_number` genuinely is the row's own deliverable) keeps
        # the #522 issue-closed fast path so a manually-closed issue still
        # retires it here. `trust_issue_closed_for` (coord/models.py) is the
        # single shared derivation of this — every other `work_is_terminal`
        # call site that can see a WORK_LIKE_TYPES row should use it too,
        # rather than re-deriving `type not in SEALED_PATH_AUTHOR_TYPES`.
        if (
            a.type in WORK_LIKE_TYPES or is_interactive_merge_session(a)
        ) and github_ops.work_is_terminal(
            repo_cfg.github,
            a.issue_number,
            a.branch,
            cache=terminal_cache,
            trust_issue_closed=trust_issue_closed_for(a.type),
        ):
            actions.append(
                f"mark merged {a.assignment_id} "
                f"({a.repo_name} #{a.issue_number}, {a.branch or 'no branch'})"
                + (" [dry-run]" if dry_run else "")
            )
            if not dry_run:
                a.status = "merged"
                state.mark_assignment_merged(a.assignment_id or "")
                # #951: mark_assignment_merged only flips status — clear a
                # lingering review_state='pending' ghost too, or the row keeps
                # showing "[awaiting review]" forever despite being merged.
                # #1574: kept ``type == "work"``-only (not widened to
                # WORK_LIKE_TYPES like the status flip above) — a
                # test-author/mock-author row's review_state is exactly what
                # sweep (f)'s #1180 wedged-review repair polices (a stray
                # review_state='done' with no real review behind it), and
                # settling it to 'done' here would immediately be flagged as
                # wedged and reset back to 'pending' by that sweep, an
                # unhelpful churn this fix doesn't need to introduce. Only
                # `is_interactive_merge_session` rows share sweep (b)'s
                # type='work' review-settle path, same as before #1574.
                if a.review_state == "pending" and (
                    a.type == "work" or is_interactive_merge_session(a)
                ):
                    a.review_state = "done"
                    state.mark_work_review_settled(a.assignment_id or "")

    # (c) #721 — close open PRs whose work has already landed.
    actions.extend(
        close_stale_prs(
            config,
            board=board,
            repo=repo,
            issue=issue,
            dry_run=dry_run,
            skip_dormant_repos=skip_dormant_repos,
        )
    )

    # (d) #732 — prune stale merge_queue entries for closed issues / merged PRs.
    # Runs after the board sweeps so a just-marked-merged assignment doesn't
    # also appear as a pruned queue entry in the same reconcile run.
    # repo/issue filters don't apply here — we always scan the full queue, since
    # a stale entry affects every `coord merge` run regardless of --repo.
    from coord import merge_queue as mq  # noqa: PLC0415

    pruned = mq.prune_stale_queue_entries(dry_run=dry_run)
    for entry in pruned:
        actions.append(
            f"prune queue entry {entry.assignment_id} "
            f"({entry.repo_name} #{entry.issue_number}, state={entry.state})"
            + (" [dry-run]" if dry_run else "")
        )

    # (e) #894/#951 — settle sibling ghost rows for terminal issues.
    #
    # The #609 sweep (b) only processes type='work' status='done' rows, so it
    # misses three classes of lingering ghost rows for already-merged/closed issues:
    #
    #   * type=review/smoke/conflict-fix rows whose status='done' but
    #     review_state='pending' — the interactive-completion path
    #     (issue_store._update_local_state) sets review_state='pending' on ALL
    #     completed assignments so reconcile picks them up like claude -p workers.
    #     When the parent issue closes before that handoff fires, these rows
    #     surface as "awaiting review" in coord status / the TUI forever.
    #
    #   * status='advisory' rows (any type) — the #609 candidates filter requires
    #     status='done', so advisory rows are never reached.  They linger in the
    #     TUI's advisory view after the issue is terminal.
    #
    #   * type='work' rows whose status is ALREADY 'merged' but review_state is
    #     still 'pending' (#951) — once `mark_assignment_merged` (#609) flips a
    #     row's status to 'merged' (in this run's sweep (b) above, or in a prior
    #     reconcile run), it permanently drops out of sweep (b)'s
    #     status=='done' candidates list on every future pass, so a
    #     review_state='pending' ghost left on it (from #609 predating the
    #     review_state clear added above, or any other stale write) is never
    #     revisited. `state.mark_work_review_settled` already handles
    #     status='merged' rows fine (no status gate) — the gap was purely that
    #     reconcile() never called it for a row outside sweep (b)'s candidate
    #     set. This class fixes that: it directly matches the bug report's
    #     scenario of already-merged+closed issues stuck "awaiting review".
    #
    # This sweep is conservative and fail-open:
    #   - Only acts when work_is_terminal(...) is confirmed true.
    #   - Uses the terminal_cache populated by sweep (b) to avoid extra GH calls;
    #     falls back to a fresh check (still fail-open) for ghost rows whose issue
    #     wasn't processed in sweep (b) (e.g. work already merged in a prior run).
    #   - Respects the repo/issue filter so --repo/--issue scopes apply.
    #   - Terminality is keyed on issue_is_closed OR pr_is_merged — NOT branch
    #     ancestry, so rebase/squash merges with new SHAs are correctly handled.

    # Build a (repo_name, issue_number) → branch lookup from all work rows so
    # that sibling rows lacking a branch can still pass a branch to work_is_terminal
    # (enabling the pr_is_merged fast-path in addition to issue_is_closed).
    work_branch_for: dict[tuple[str, int], str | None] = {}
    for _a in board.active + board.completed:
        if _a.type == "work" and _a.issue_number is not None:
            key = (_a.repo_name, _a.issue_number)
            # Prefer a non-None branch; first seen wins (done rows come before
            # merged rows in board.completed, but any non-None branch is fine).
            if key not in work_branch_for or work_branch_for[key] is None:
                work_branch_for[key] = _a.branch

    # #2639: a parallel (repo_name, issue_number) → work-like `type` lookup,
    # scoped to WORK_LIKE_TYPES (unlike work_branch_for above, which is
    # deliberately "work"-only for its branch-fallback purpose). A
    # review/smoke/conflict-fix ghost sibling below inherits its
    # `issue_number` from the work row it was dispatched for — if THAT row
    # is test-author/mock-author, `issue_number` is the milestone's tracking
    # issue, not the sibling's own deliverable, even though the sibling's
    # own `a.type` is "review"/"smoke"/"conflict-fix" (not itself in
    # SEALED_PATH_AUTHOR_TYPES). Trusting a closed tracking epic for such a
    # sibling would settle it before the underlying slice ever really
    # landed.
    work_type_for: dict[tuple[str, int], str] = {}
    for _a in board.active + board.completed:
        if _a.type in WORK_LIKE_TYPES and _a.issue_number is not None:
            key = (_a.repo_name, _a.issue_number)
            if key not in work_type_for:
                work_type_for[key] = _a.type

    # Identify ghost sibling rows subject to this sweep.
    ghost_candidates = [
        a
        for a in board.active + board.completed
        if (
            (
                a.type in ("review", "smoke", "conflict-fix")
                and a.status == "done"
                and a.review_state == "pending"
            )
            or a.status == "advisory"
            # #2234: a refused_policy row is the same "ghost sibling" shape
            # as advisory above — a terminal, zero-commit no-op that should
            # auto-settle to `merged` once GitHub confirms the issue went
            # terminal, rather than sitting on `refused_policy` forever.
            or a.status == "refused_policy"
            or (
                a.type == "work"
                and a.status == "merged"
                and a.review_state == "pending"
            )
        )
        and (repo is None or a.repo_name == repo)
        and (issue is None or a.issue_number == issue)
    ]

    for a in ghost_candidates:
        repo_cfg = config.repo(a.repo_name)
        if repo_cfg is None:
            actions.append(
                f"skip settle {a.assignment_id} "
                f"({a.repo_name} #{a.issue_number}): repo not in config"
            )
            continue

        # Resolve the best available branch for the terminality probe.  The
        # sibling row itself may carry a branch; fall back to the work row's
        # branch so the pr_is_merged check fires even when the sibling has none.
        branch = a.branch or work_branch_for.get((a.repo_name, a.issue_number))

        # #2639: trust_issue_closed_for the underlying work-like row's type
        # (falling back to this sibling's own type when no work-like row is
        # on the board for the same issue) — see work_type_for above.
        _terminal_type = work_type_for.get((a.repo_name, a.issue_number), a.type)
        if not github_ops.work_is_terminal(
            repo_cfg.github,
            a.issue_number,
            branch,
            cache=terminal_cache,
            trust_issue_closed=trust_issue_closed_for(_terminal_type),
        ):
            continue  # Issue still live — leave this row alone.

        if a.status == "advisory":
            actions.append(
                f"settle advisory {a.assignment_id} "
                f"({a.repo_name} #{a.issue_number})"
                + (" [dry-run]" if dry_run else "")
            )
            if not dry_run:
                a.status = "merged"
                state.mark_advisory_settled(a.assignment_id or "")
        elif a.status == "refused_policy":
            # #2234: same settling as advisory above — once GitHub confirms
            # the issue went terminal, a refused_policy row auto-settles to
            # 'merged' rather than sitting on the board forever.
            actions.append(
                f"settle refused_policy {a.assignment_id} "
                f"({a.repo_name} #{a.issue_number})"
                + (" [dry-run]" if dry_run else "")
            )
            if not dry_run:
                a.status = "merged"
                state.mark_refused_policy_settled(a.assignment_id or "")
        elif a.type == "work":
            # #951: type=work, status=merged, review_state=pending — a row
            # that already fell out of sweep (b)'s status=='done' candidates
            # in a prior run (or earlier in this run) but still carries a
            # review_state ghost. status is already 'merged', so only the
            # review_state needs settling.
            actions.append(
                f"settle work review_state {a.assignment_id} "
                f"({a.repo_name} #{a.issue_number})"
                + (" [dry-run]" if dry_run else "")
            )
            if not dry_run:
                a.review_state = "done"
                state.mark_work_review_settled(a.assignment_id or "")
        else:
            # type=review/smoke/conflict-fix, status=done, review_state=pending
            actions.append(
                f"settle sibling {a.assignment_id} "
                f"({a.repo_name} #{a.issue_number}, type={a.type})"
                + (" [dry-run]" if dry_run else "")
            )
            if not dry_run:
                a.review_state = "done"
                state.mark_sibling_review_done(a.assignment_id or "")

    # (f) #1180 — un-wedge a test-author/mock-author row whose review_state
    # was stamped 'done' by a `work_is_terminal` false positive (pre-#1150:
    # test-author assignments carry issue_number = the milestone's *tracking*
    # issue — the JIT-slice aliasing convention, #1142/#1150 — so a tracking
    # issue with ANY historical merged PR could satisfy the then-issue-only
    # terminal check for an unrelated, still-open slice sharing that number).
    # #1150 fixed the check going forward (branch/commit-scoped) but did not
    # repair rows it had already corrupted: a row stuck at
    # review_state='done' with no verdict and no type='review' assignment
    # ever dispatched against its branch is invisible to
    # dispatch_pending_reviews (only review_state in (None, 'pending') is
    # eligible) AND to the merge gate (requires a real approved type='review'
    # row) — a permanent deadlock between the two subsystems. Reset
    # review_state -> 'pending' so the (now-fixed) auto-loop retries a real
    # review. This is safe even if the branch genuinely IS terminal by now:
    # the very next dispatch_pending_reviews pass re-checks
    # work_is_terminal (correctly branch-scoped post-#1150) and re-settles
    # the row.
    wedged_review_candidates = [
        a
        for a in board.active + board.completed
        if a.type in ("test-author", "mock-author")
        and a.review_state == "done"
        and a.review_verdict is None
        and a.branch
        and (repo is None or a.repo_name == repo)
        and (issue is None or a.issue_number == issue)
    ]
    for a in wedged_review_candidates:
        has_review = any(
            r.type == "review"
            and r.repo_name == a.repo_name
            and r.branch == a.branch
            # #1566: "finalizing" is a review row whose agent already
            # finished but whose verdict hasn't been parsed/posted by
            # `coord notify` yet — it must count as "has a review" here too,
            # or a review that lands on 'finalizing' the instant its
            # candidate check above resolves triggers a spurious "repair
            # wedged review_state ... done -> pending" and a duplicate
            # dispatch_pending_reviews pass while the first review is still
            # wrapping up.
            and r.status in ("done", "finalizing")
            for r in board.active + board.completed
        )
        if has_review:
            continue
        actions.append(
            f"repair wedged review_state {a.assignment_id} "
            f"({a.repo_name} #{a.issue_number}, branch={a.branch}): "
            "done -> pending (#1180)"
            + (" [dry-run]" if dry_run else "")
        )
        if not dry_run:
            a.review_state = "pending"
            state.reset_wedged_test_author_review(a.assignment_id or "")

    # (g) #1767 — drop drive escalations whose issue resolved out of band.
    #
    # `coord merge`'s success path (merge_queue.process()) dismisses the
    # escalation for the issue it just merged, but that only covers work
    # that landed *through* `coord merge`. Work merged directly on GitHub,
    # or closed without merging, never goes through that path — its
    # escalation (if any) would otherwise linger forever, since nothing
    # else ever clears one short of `coord escalate dismiss`. Measured on
    # the live board (#1767): four open escalations, three already stale —
    # PRs merged and issues closed days earlier, with the escalation the
    # only record that hadn't caught up.
    #
    # Conservative like every sweep above: only acts when `work_is_terminal`
    # confirms the issue closed or its PR merged, reusing `terminal_cache`
    # and the `work_branch_for` lookup built for sweep (e) so this costs no
    # extra `gh` calls for issues already resolved elsewhere in this run.
    # An escalation on a still-open, still-blocked issue is never touched,
    # no matter how old — age is not the signal, resolved state is.
    for _entry in state.list_drive_escalations(repo):
        _esc_repo = _entry.get("repo_name")
        _esc_issue = _entry.get("issue_number")
        if _esc_repo is None or _esc_issue is None:
            continue
        if issue is not None and _esc_issue != issue:
            continue
        _repo_cfg = config.repo(_esc_repo)
        if _repo_cfg is None:
            # Also filters out the drive-queue's own synthetic alert entry
            # (repo_name="(drive-queue)"), which isn't a real GitHub issue.
            continue
        _branch = work_branch_for.get((_esc_repo, _esc_issue))
        # #2639: an escalation can be raised against a test-author/
        # mock-author dispatch too (drive-queue milestone automation) —
        # reuse work_type_for so a closed tracking epic doesn't dismiss a
        # still-unresolved escalation.
        _esc_type = work_type_for.get((_esc_repo, _esc_issue), "work")
        if not github_ops.work_is_terminal(
            _repo_cfg.github,
            _esc_issue,
            _branch,
            cache=terminal_cache,
            trust_issue_closed=trust_issue_closed_for(_esc_type),
        ):
            continue
        actions.append(
            f"dismiss escalation {_esc_repo} #{_esc_issue}: "
            "issue resolved out of band (#1767)"
            + (" [dry-run]" if dry_run else "")
        )
        if not dry_run:
            state.dismiss_drive_escalation(_esc_repo, _esc_issue)

    # (h) #2639 second half — flag a `status='merged'` row that may never
    # actually have landed.
    #
    # THE GAP: before this fix, `work_is_terminal`'s issue-closed check could
    # flip a `status='merged'` row whose branch was NEVER actually merged
    # anywhere (a test-author/mock-author row booked against a closed
    # tracking epic — see sweep (b) above and #2639). Once flipped, the row
    # is invisible to every other diagnostic: `coord diagnose --stage review`
    # reads "review stage looks healthy", `coord diagnose --stage work` reads
    # "no work assignment", and `coord merge --dry-run` proposes nothing —
    # because all three trust `status='merged'` at face value. This sweep is
    # the one place that DOESN'T: it re-derives "did this actually land" from
    # git/GitHub reality for every already-`merged` row, the same way sweep
    # (b) does for `done` rows, so a historical mis-flip (from before this
    # fix existed, or from any other future bug shaped like it) doesn't stay
    # permanently invisible.
    #
    # DETECTION ONLY — this sweep NEVER mutates board state. Recovering a
    # falsely-merged row needs a human call (re-dispatch? was it actually
    # superseded? is the branch salvageable?) that this sweep cannot safely
    # make on its own; it only surfaces the row so an operator can decide.
    #
    # METHODOLOGY (from the live #2639 blast-radius sweep, 2026-08-23 — 67
    # branches checked, 3 flagged, 1 genuine casualty):
    #   1. Skip if the branch was deleted from origin — the dominant, benign
    #      case (merged + cleaned up the normal way).
    #   2. Skip if `pr_is_merged` confirms a merged PR at the branch's
    #      CURRENT tip (#1150, SHA-exact) — correctly tracked, nothing to see.
    #   3. Skip if the branch is 0 commits ahead of its resolved base branch
    #      — its content is already an ancestor of the base, just never had
    #      (or needed) its own PR. This is the sweep's own first false
    #      positive: `issue-2531-config-portal-project-repo-mapping`.
    #   4. Otherwise the branch carries commits neither `pr_is_merged` nor
    #      "already an ancestor" accounts for — but SHA identity alone is
    #      NOT proof of loss: a rebase, squash, different PR, or direct push
    #      can all land the exact same CONTENT under a different SHA (the
    #      sweep's second false positive: coord-portal's
    #      `issue-16-gate-a-...` — rebased to a new SHA, content identical).
    #      So compare CONTENT: fetch every file the branch's diff touches
    #      (bounded — `_FALSE_MERGE_AUDIT_MAX_FILES`) at both the branch's
    #      tip and the base branch's tip; if every one is byte-identical, the
    #      content already landed — not lost, skip. Only when at least one
    #      changed file's content actually differs is this flagged.
    #
    # Still imperfect (a legitimate LATER edit to the same file on the base
    # branch, unrelated to this branch's change, would also show up as
    # "differs" and get flagged) — deliberately conservative on outcome:
    # this is a FLAG for a human to check, exactly like the manual sweep this
    # automates, not a verdict. Fail-open at every layer: any fetch failure
    # skips that row/file rather than either flagging or clearing it.
    #
    # BOUNDING (#2989) — three mechanisms, applied in this order.  Before
    # them this sweep selected EVERY `merged` work-like row in the board:
    # 1,302 rows on the drive host, proportional to project history rather
    # than to work in flight, re-probed on the daemon's 30s tick.  Measured,
    # that was 1,304 of one pass's 1,346 `gh` invocations (97%), and the
    # generator behind the fleet-wide secondary rate limiting #2809/#2858/
    # #2934/#2977 all treated symptomatically.  (#2639 had already capped
    # the inner per-file loop — the wrong loop; the outer candidate set was
    # left unbounded.)
    #
    #   (1) CADENCE.  This detects a rare, non-urgent condition — a
    #       merged-marked row whose branch still differs from base, which is
    #       recovered by a human days later regardless.  Twice a minute is
    #       absurd for that.  The daemon tick passes
    #       `throttle_false_merge_audit=True` and the sweep then runs at most
    #       once an hour (~120x fewer passes on its own).  A manual `coord
    #       reconcile-merges` / `coord diagnose` never throttles: an operator
    #       who asked for a sweep gets one, deterministically.
    #   (2) TERMINAL MARKER.  A clean verdict is clean forever (branch
    #       deleted, merged PR at the tip, already an ancestor of base, or
    #       content byte-identical on base — none of those un-happen for a
    #       terminal row).  Persisting it (`state.mark_false_merge_audit_
    #       clean`) drops the row from the candidate list permanently, which
    #       makes the set proportional to *unaudited* merges — bounded by
    #       recent throughput, not project age.  This is the real fix, and
    #       the same discipline `coord.gate_snapshot`'s refresh already
    #       states ("merged history is never refreshed").
    #   (3) RECENCY CAP.  A backstop for the first pass after this ships (and
    #       for any board whose marker was lost): audit at most
    #       `_FALSE_MERGE_AUDIT_MAX_ROWS` rows per pass, newest first.  A
    #       false merge that has gone unnoticed for two months is not being
    #       caught by a 30-second sweep anyway; the older tail is simply
    #       drained over the following passes as newer rows get marked.
    #
    # An explicit `--issue N` bypasses (2) and (3) entirely: a targeted
    # re-audit must always re-probe, so a previously-cleared row stays
    # re-checkable by hand.  Nothing here changes what the sweep CONCLUDES,
    # only how many probes it takes to conclude it.
    _false_merge_targeted = issue is not None
    _run_false_merge_audit = True
    if throttle_false_merge_audit and not _false_merge_targeted:
        _last_run = state.get_false_merge_audit_last_run()
        if (time.time() - _last_run) < _FALSE_MERGE_AUDIT_MIN_INTERVAL_SECONDS:
            _run_false_merge_audit = False

    _false_merge_candidates: list[Assignment] = []
    if _run_false_merge_audit:
        _false_merge_candidates = [
            a
            for a in board.active + board.completed
            if a.status == "merged"
            and a.branch
            and (a.type in WORK_LIKE_TYPES or is_interactive_merge_session(a))
            and (repo is None or a.repo_name == repo)
            and (issue is None or a.issue_number == issue)
            # #2989: a row whose repo has no GitHub mapping is unprobeable —
            # drop it HERE rather than after the recency cap, so a pile of
            # orphaned rows (a repo since removed from coordinator.yml) can't
            # eat the cap's slots and starve the rows we can actually audit.
            and getattr(config.repo(a.repo_name), "github", None)
        ]
        if not _false_merge_targeted:
            _already_clean = state.load_false_merge_audit_clean()
            if _already_clean:
                _false_merge_candidates = [
                    a
                    for a in _false_merge_candidates
                    if a.assignment_id not in _already_clean
                ]
            if len(_false_merge_candidates) > _FALSE_MERGE_AUDIT_MAX_ROWS:
                _false_merge_candidates.sort(
                    key=lambda a: (a.finished_at or a.dispatched_at or 0.0),
                    reverse=True,
                )
                _false_merge_candidates = _false_merge_candidates[
                    :_FALSE_MERGE_AUDIT_MAX_ROWS
                ]

    if (
        _run_false_merge_audit
        and throttle_false_merge_audit
        and not _false_merge_targeted
        and not dry_run  # a dry-run must not consume the real run's turn
    ):
        # Stamp even when there was nothing to do — the point of the stamp is
        # "this sweep had its turn this hour", not "this sweep found work".
        state.set_false_merge_audit_last_run(time.time())

    if _false_merge_candidates:
        from coord.branch_model import resolve_base_branch_for_issue_number  # noqa: PLC0415

        _false_merge_milestone_cache: dict = {}
        # #2989 mechanism (3'): one ref lookup per DISTINCT (repo, branch)
        # per pass, not one per row.  Sibling rows legitimately share a
        # branch (a work row and its conflict-fix, a re-dispatch), which
        # measured 1.53x redundancy — ~453 wasted calls in a single pass.
        _false_merge_branch_cache: dict = {}
        _false_merge_confirmed_clean: list[str] = []
        for a in _false_merge_candidates:
            repo_cfg = config.repo(a.repo_name)
            if repo_cfg is None or not repo_cfg.github:
                continue
            try:
                if not github_ops.branch_exists_on_remote(
                    repo_cfg.github, a.branch, cache=_false_merge_branch_cache
                ):
                    # branch gone — presumed genuinely merged + cleaned up,
                    # and permanently so (#2989).
                    _false_merge_confirmed_clean.append(a.assignment_id)
                    continue
                if github_ops.pr_is_merged(repo_cfg.github, a.branch):
                    # correctly tracked: a merged PR sits at this exact tip
                    _false_merge_confirmed_clean.append(a.assignment_id)
                    continue
                base_branch = resolve_base_branch_for_issue_number(
                    repo_cfg,
                    repo_cfg.github,
                    a.issue_number,
                    cache=_false_merge_milestone_cache,
                )
                ahead = github_ops.branch_commits_ahead(
                    repo_cfg.github, base_branch, a.branch
                )
                if ahead is None:
                    # fail-open: the probe itself failed, so this row is NOT
                    # confirmed clean — re-probe it next pass (#2989).
                    continue
                if ahead == 0:
                    # already an ancestor of base: its content landed, and
                    # that is permanent (#2989).
                    _false_merge_confirmed_clean.append(a.assignment_id)
                    continue
                changed_files = github_ops.get_compare_files(
                    repo_cfg.github, base_branch, a.branch
                )
                if not changed_files:
                    continue  # can't confirm anything — fail open, re-probe
                content_differs = False
                differing_path = None
                for _path in changed_files[:_FALSE_MERGE_AUDIT_MAX_FILES]:
                    try:
                        branch_content = github_ops.get_repo_file(
                            repo_cfg.github, _path, a.branch
                        )
                    except RuntimeError:
                        continue  # file gone from the branch tip too — no signal
                    try:
                        base_content = github_ops.get_repo_file(
                            repo_cfg.github, _path, base_branch
                        )
                    except RuntimeError:
                        base_content = None  # never landed on base at all
                    if branch_content != base_content:
                        content_differs = True
                        differing_path = _path
                        break
                if not content_differs:
                    # verbatim on base already — rebase/squash, not lost, and
                    # a terminal row's content cannot un-land (#2989).
                    _false_merge_confirmed_clean.append(a.assignment_id)
                    continue
                actions.append(
                    f"POSSIBLY LOST (#2639): {a.assignment_id} "
                    f"({a.repo_name} #{a.issue_number}, branch={a.branch}) is "
                    f"status='merged' but is {ahead} commit(s) ahead of "
                    f"{base_branch} with no merged PR at its current tip, and "
                    f"{differing_path!r} differs from {base_branch}'s copy — "
                    "this sweep never auto-fixes; an operator should confirm "
                    "the branch's content genuinely never landed (check "
                    f"{differing_path!r}'s history on {base_branch}) before "
                    "deciding how to recover it (re-dispatch, manual PR, or "
                    "confirm it's a false positive and leave the row alone)"
                )
            except Exception as exc:  # noqa: BLE001 — detection-only, never
                # let one bad row's `gh` failure sink the whole sweep or
                # (worse) get treated as evidence either way.
                actions.append(
                    f"skip false-merge audit for {a.assignment_id} "
                    f"({a.repo_name} #{a.issue_number}): {exc}"
                )

        # #2989: persist the terminal marker once, at the end of the sweep —
        # one board_meta write per pass, not one per row.  Never on a
        # dry-run: `--dry-run` must not change what the next real pass does.
        if _false_merge_confirmed_clean and not dry_run:
            state.mark_false_merge_audit_clean(_false_merge_confirmed_clean)

    return actions
