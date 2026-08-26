"""Per-stage "doctor": diagnose a pipeline stage, best-effort recover, and —
when recovery isn't possible — offer a non-destructive reset.

Pipeline stages routinely get into bad DB states with no clean UI recovery:
phantom ``running`` rows (board says running, no live session — #366), reviews
whose findings were silently dropped (#607), stale-but-live detached sessions
days old (#494/#370/#546), merged-but-grey boxes, orphaned worktrees.  This
module is the orchestration the TUI's "Diagnose & fix stage" action and the
``coord diagnose`` command call; it *composes* existing primitives rather than
reinventing them:

* :func:`coord.interactive.finalize_interactive_exit` — record a terminal state
  for a dead/phantom session (pushes commits, releases claim, prunes worktree).
* :func:`coord.interactive._review_findings_from_transcript` — the #617
  remote-aware transcript-floor that recovers a review's verdict + findings from
  the session's own host.
* :func:`coord.reconcile.reconcile_board_merges` — flip merged-but-grey work and
  backfill missing branches.

Design decisions (locked with the operator):

* **Reset is non-destructive**: it clears the stage's board rows, releases the
  claim, removes the orphaned worktree, and stops a live session — but NEVER
  deletes the feature branch.  ``origin/issue-<N>-*`` and its commits are
  preserved, so the stage re-dispatches fresh with the work intact.  (There is
  deliberately no branch-deletion code path in this module.)
* **Cleanup is scoped to the one issue**, not a fleet-wide sweep.
* **The issue-wide phantom-row scan never writes without ``--reset``** (#1658):
  ``diagnose_stage``'s targeted best-effort recovery of the STAGE the operator
  asked about may still write without ``--reset`` (that's the whole point of
  "best-effort recover"), but :func:`_cleanup_issue`'s sweep over the issue's
  OTHER rows only ever reports a finding + ``needs_reset=True`` unless
  ``--reset`` was passed. That sweep touches rows the operator did not ask
  about and did not get a tailored diagnosis for, so a wrong liveness read on
  it is pure collateral damage — see :func:`_session_state`'s note on why
  tmux-only liveness was itself wrong for headless workers.

The side-effecting steps are factored into small module-level helpers so the
orchestration in :func:`diagnose_stage` is unit-testable by monkeypatching them.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid import cycles / heavy imports at module load
    from coord.config import Config
    from coord.models import Assignment, Board, Machine

log = logging.getLogger(__name__)

# Stages the doctor understands.  Each maps to the assignment ``type`` that
# carries its state; ``test`` and ``merge`` are tracked on the *work* row
# (``test_state`` / ``status='merged'`` + the merge queue) rather than a
# dedicated assignment type.
STAGE_ASSIGNMENT_TYPES: dict[str, tuple[str, ...]] = {
    "plan": ("plan",),
    "work": ("work", "plan"),
    # #1180: a `type="test-author"`/`"mock-author"` completion carries its own
    # `review_state`/`review_verdict` (they're in WORK_LIKE_TYPES and go
    # through the same review chokepoint as `work`) but never spawns a
    # dedicated `type="review"` row when it's wedged — before this fix,
    # `coord diagnose --stage review` looked at `type="review"` rows only, so
    # a test-author row stuck at `review_state="done"` with no verdict and no
    # review assignment was invisible: the tool would report on whatever
    # unrelated `type="review"` row happened to share the tracking issue
    # number (false "stage looks healthy"/wrong-row confidence) instead of
    # flagging the real wedge.
    "review": ("review", "test-author", "mock-author"),
    "test": ("work", "plan"),
    "merge": ("work", "plan"),
    # #2087: previously absent entirely — `--stage smoke` (or an implicit
    # `current_stage()` pick landing on a `type="smoke"` row, e.g. a Test
    # stage dispatched more recently than its parent `work` row) fell
    # straight into the "no diagnosis available" dead end below with no
    # recovery and no `--reset` path. Routed through the same work-like
    # recovery as `work`/`plan` (`diagnose_stage`'s per-stage dispatch and
    # `_do_reset` both fall back to that branch for any type not `review` or
    # `test`) — a smoke row is a one-shot session exactly like a work row.
    "smoke": ("smoke",),
}


@dataclass
class DiagnoseResult:
    """Outcome of a diagnose/recover/reset run for one stage of one issue."""

    repo_name: str
    issue_number: int
    stage: str
    findings: list[str] = field(default_factory=list)
    actions_taken: list[str] = field(default_factory=list)
    # True when the stage is healthy after this run (nothing was wrong, or the
    # problem was recovered).  False + needs_reset=True means "still wedged".
    recovered: bool = False
    # True when best-effort recovery could not clear the problem and the only
    # remaining option is a reset.
    needs_reset: bool = False
    # Always True for this module — reset keeps the branch.  Surfaced so the TUI
    # can promise "keeps branch + commits" in the confirm dialog.
    branch_preserved: bool = True
    # Whether a reset was actually performed this run.
    reset_performed: bool = False

    def to_json_dict(self) -> dict:
        """Return a JSON-serialisable dict of all DiagnoseResult fields.
        Used by ``coord diagnose --json`` and the daemon ``post_diagnose``
        handler (#935 Part C) so the TUI can parse findings/actions without
        scraping the human-readable output lines."""
        import dataclasses  # noqa: PLC0415 — lazy to avoid circular import risk
        return dataclasses.asdict(self)

    def summary_line(self) -> str:
        """The machine-readable trailer the TUI greps for (mirrors the
        ``coord:`` marker convention)."""
        return (
            f"DIAGNOSE_RESULT: stage={self.stage} "
            f"recovered={str(self.recovered).lower()} "
            f"needs_reset={str(self.needs_reset).lower()} "
            f"reset_performed={str(self.reset_performed).lower()} "
            f"actions={len(self.actions_taken)}"
        )


# ── stage / assignment resolution ───────────────────────────────────────────


def stage_assignments(
    board: "Board", repo_name: str, issue_number: int, stage: str
) -> list["Assignment"]:
    """All assignments for *issue_number* in *repo_name* matching *stage*,
    newest-dispatched first.  Mirrors the TUI's ``assignments_for_stage``."""
    types = STAGE_ASSIGNMENT_TYPES.get(stage, (stage,))
    rows = [
        a
        for a in (board.active + board.completed)
        if a.issue_number == issue_number
        and a.repo_name == repo_name
        and (a.type or "work") in types
    ]
    rows.sort(key=lambda a: (a.dispatched_at or 0.0), reverse=True)
    return rows


def _latest(assignments: list["Assignment"]) -> "Assignment | None":
    return assignments[0] if assignments else None


def _flag_contradictory_failed(latest: "Assignment", res: DiagnoseResult) -> None:
    """#1451: flag a ``status='failed'`` work row whose own fields already
    prove it isn't — a passing test verdict and/or an approved review on a
    row that pushed a real branch is self-evidently not a failure.

    This is deliberately NOT based on ``exit_code``/``failure_reason`` being
    empty — those are empty on the overwhelming majority of legitimate
    ``failed`` rows too (a launch-failure ``failure_reason`` is the rare
    exception, and no current write path persists ``exit_code`` to the DB at
    all), so that pair is not a usable signal on its own. The reliable
    signal is a genuine contradiction: evidence of *success* recorded on a
    row the board calls failed.

    Best-effort and read-only — appends a finding only, no write. Detection,
    not correction: the fix is either ``coord report-result --assignment
    <id> --status done`` (interactive) or re-running the completing worker.
    """
    if latest.status != "failed":
        return
    contradictions: list[str] = []
    if latest.test_state == "passed":
        contradictions.append("test_state=passed")
    if latest.review_verdict == "approve":
        contradictions.append("review_verdict=approve")
    if not contradictions:
        return
    if not latest.branch:
        # No pushed branch to review/test at all — the "passed"/"approve"
        # values must be stale carry-over from a prior assignment row, not
        # evidence about *this* failed row. Don't flag without a branch.
        return
    res.findings.append(
        f"⚠ status='failed' contradicts its own fields ({', '.join(contradictions)}, "
        f"branch={latest.branch}) — looks like a phantom failure (#1451), not a "
        "real one. If the work is actually done, recover it with "
        f"`coord report-result --assignment {latest.assignment_id} --status done "
        '--summary "..."` (or re-run the completing worker if unsure).'
    )


def current_stage(board: "Board", repo_name: str, issue_number: int) -> str:
    """The stage of the most-recently-dispatched assignment for the issue
    (what ``coord diagnose <repo> <issue>`` targets when ``--stage`` is
    omitted).  Falls back to ``work`` when the issue has no assignments.

    #1083: previously coerced any assignment ``type`` this module doesn't
    recognize (e.g. ``test-author``, ``mock-author``, ``smoke``) to
    ``"work"`` — which then had ``diagnose_stage`` recover/report on
    whatever unrelated ``work``/``plan`` row happened to exist for the issue,
    *silently* presenting it as if it were a diagnosis of the real (ignored)
    assignment. Now the actual type is returned verbatim; ``diagnose_stage``
    explicitly reports "no diagnosis available" for types outside
    :data:`STAGE_ASSIGNMENT_TYPES` instead of guessing.
    """
    rows = [
        a
        for a in (board.active + board.completed)
        if a.issue_number == issue_number and a.repo_name == repo_name
    ]
    if not rows:
        return "work"
    newest = max(rows, key=lambda a: (a.dispatched_at or 0.0))
    return newest.type or "work"


# ── monkeypatchable side-effecting wrappers ─────────────────────────────────
#
# Each wraps an existing primitive and is replaced in unit tests so the
# orchestration can be exercised without touching git/tmux/the network.


def _resolve_machine(config: "Config", machine_name: str | None):
    if not machine_name:
        return None
    return next((m for m in config.machines if m.name == machine_name), None)


def _session_state(assignment: "Assignment", config: "Config") -> str:
    """``"live"`` | ``"dead"`` | ``"unknown"`` for *assignment*.

    Probes the assignment's machine (local tmux, or the remote host's tmux over
    ssh — same mechanism as ``coord reattach`` / the stale-session reaper).
    ``"unknown"`` when the machine can't be resolved or the probe errors, so the
    caller never finalizes on a false negative.

    #1658: tmux liveness alone is blind to HEADLESS workers. A headless
    assignment (the normal shape for a daemon-dispatched review/work — see
    ``AgentServer.assign``) runs as a plain subprocess tracked by the agent's
    own ``_assignments`` dict; it never has a tmux session at all, so
    ``tmux_session_alive`` reads "dead" for it unconditionally, regardless of
    whether the worker is still running. Before this fix, that false "dead"
    made every live headless assignment look like a phantom the instant
    ``coord diagnose`` looked at it — the incident this closes: a live review
    worker's row was finalized to ``failed`` mid-review. Now, when tmux says
    dead, the assignment's own agent ``/status`` is consulted before trusting
    that — the same seam :func:`coord.reconcile.reconcile_completed_assignments`
    uses to tell "still running" from "actually finished" — and a match in its
    ``active`` list is authoritative: the agent is the ground truth for its
    own subprocesses. An unreachable agent still returns "unknown" rather than
    "dead", preserving the never-finalize-on-a-probe-failure guarantee.
    """
    import socket  # noqa: PLC0415

    from coord.interactive import (  # noqa: PLC0415
        TmuxHost,
        tmux_session_name,
        tmux_session_running,
    )

    if not assignment.assignment_id:
        return "unknown"
    machine = _resolve_machine(config, assignment.machine_name)
    ssh_target = None
    if machine is not None:
        local_hn = socket.gethostname().split(".")[0].lower()
        is_local = (
            machine.name.lower() == local_hn
            or machine.host.split(".")[0].lower() == local_hn
        )
        if not is_local:
            ssh_target = machine.host
    elif assignment.machine_name:
        # machine_name set but unknown in config — can't probe safely.
        return "unknown"
    host = TmuxHost(ssh_target=ssh_target)
    sname = tmux_session_name(assignment.assignment_id)
    try:
        # #2541: tmux_session_running (alive AND pane not dead), not the
        # bare has-session check this used to make — remain-on-exit keeps
        # has-session True after ANY pane exit (clean success or crash)
        # until a reaper notices, so a bare check here would report a
        # crashed/finished --merge-of session as "live" and never fall
        # through to the agent cross-check below, undermining exactly the
        # diagnosability this probe exists for (#1658).
        if tmux_session_running(sname, host=host):
            return "live"
    except Exception:  # noqa: BLE001 — never let a probe error finalize a session
        return "unknown"

    # tmux says dead (or the assignment never had a tmux session at all — the
    # headless case). Consult the agent before trusting that.
    return _agent_liveness(assignment, machine)


def _agent_liveness(assignment: "Assignment", machine: "Machine | None") -> str:
    """``"dead"`` | ``"live"`` | ``"unknown"`` per the assignment's own agent
    ``/status`` — see :func:`_session_state`'s #1658 note for why this exists.
    ``machine`` is ``None`` when the assignment has no ``machine_name`` at all
    (rare) — treated as genuinely dead since there's nothing to probe."""
    if machine is None:
        return "dead"
    from coord.network import fetch_status  # noqa: PLC0415

    result = fetch_status(machine)
    if not result.ok or result.data is None:
        # Agent unreachable — don't trust tmux-dead alone, but don't claim
        # "live" either. Matches the "never finalize on a probe failure"
        # contract the rest of this module relies on.
        return "unknown"
    active = result.data.get("active") or []
    if any(isinstance(e, dict) and e.get("id") == assignment.assignment_id for e in active):
        return "live"
    return "dead"


def _ssh_target_for(assignment: "Assignment", config: "Config") -> str | None:
    """The ssh host for *assignment*'s machine, or ``None`` when it's local."""
    import socket  # noqa: PLC0415

    machine = _resolve_machine(config, assignment.machine_name)
    if machine is None:
        return None
    local_hn = socket.gethostname().split(".")[0].lower()
    if machine.name.lower() == local_hn or machine.host.split(".")[0].lower() == local_hn:
        return None
    return machine.host


def _recover_review_findings(assignment: "Assignment", config: "Config") -> str | None:
    """Recover a review's verdict + findings from its session transcript and
    persist them through the durable seam (#617).  Returns the verdict on
    success, ``None`` when nothing was recoverable.  Read-only w.r.t. the
    session (safe to run even while it's live)."""
    from coord import issue_store  # noqa: PLC0415
    from coord.interactive import _review_findings_from_transcript  # noqa: PLC0415

    if not assignment.assignment_id:
        return None
    assignment_id: str = assignment.assignment_id
    ssh_target = _ssh_target_for(assignment, config)
    started_at = assignment.dispatched_at
    findings = _review_findings_from_transcript(
        assignment.issue_number,
        started_at,
        assignment_id=assignment_id,
        ssh_target=ssh_target,
    )
    if findings is None:
        return None
    repo_cfg = next((r for r in config.repos if r.name == assignment.repo_name), None)
    try:
        issue_store.post_result(
            issue_store.ResultRecord(
                assignment_id=assignment_id,
                machine_name=assignment.machine_name or "unknown",
                repo_name=assignment.repo_name,
                repo_github=(repo_cfg.github if repo_cfg else assignment.repo_name),
                issue_number=assignment.issue_number,
                status="done",
                verdict=findings.verdict,  # type: ignore[arg-type]
                summary="Findings recovered from the session transcript by coord diagnose.",
                findings_body=findings.body,
                branch=None,
            )
        )
    except RuntimeError as exc:
        # #990: the verdict was recovered from the transcript but couldn't be
        # durably persisted (retries exhausted / readback mismatch). Surface
        # this instead of letting it crash `coord diagnose` — the caller
        # treats a ``None`` return as "not recoverable" and reports
        # "re-review needed", which is the safe outcome here too since the
        # write did not actually land.
        import click  # noqa: PLC0415

        click.echo(
            f"  ⚠ recovered verdict {findings.verdict!r} from transcript for "
            f"{assignment.assignment_id} but failed to persist it: {exc}",
            err=True,
        )
        return None
    return findings.verdict


def _finalize_dead(assignment: "Assignment", config: "Config") -> str:
    """Finalize a dead/phantom session: record a terminal state, push any
    commits, release the claim, prune the worktree.  Returns a short status."""
    from coord.interactive import finalize_interactive_exit  # noqa: PLC0415
    from coord.state import COORD_DIR  # noqa: PLC0415

    machine = _resolve_machine(config, assignment.machine_name)
    repo_cfg = next((r for r in config.repos if r.name == assignment.repo_name), None)
    base = (repo_cfg.default_branch if repo_cfg else None) or "main"
    repo_github = repo_cfg.github if repo_cfg else assignment.repo_name
    repo_path = None
    if machine is not None and assignment.repo_name:
        from pathlib import Path  # noqa: PLC0415

        rp = machine.repo_path(assignment.repo_name)
        if rp:
            repo_path = str(Path(rp).expanduser())
    worktree = str(COORD_DIR / "worktrees" / (assignment.assignment_id or ""))
    fr = finalize_interactive_exit(
        assignment_id=assignment.assignment_id or "",
        repo_name=assignment.repo_name,
        repo_github=repo_github,
        issue_number=assignment.issue_number,
        machine_name=assignment.machine_name or "unknown",
        worktree_path=worktree if assignment.type in ("work", "plan") else None,
        base_branch=base,
        exit_code=0,
        started_at=assignment.dispatched_at,
        repo_path=repo_path,
        ssh_target=_ssh_target_for(assignment, config),
        # #1256: unconditional — restore_live_checkout_from_smoke_snapshot()
        # is a documented no-op unless a snapshot marker for this
        # assignment_id exists in repo_path's .git/ dir, so this is safe for
        # every assignment type, not just "smoke". `coord diagnose`'s
        # unfiltered dead-session sweep (_cleanup_issue) is one of the two
        # automated/operator-recovery paths (the other is
        # reap_stale_interactive_sessions) that exist specifically to handle
        # a session that died without a clean exit — exactly when a
        # --smoke-of session's live-checkout mutation needs reverting.
        smoke_repo_path=repo_path,
    )
    return fr.terminal_status or "finalized"


def _kill_session(assignment: "Assignment", config: "Config") -> bool:
    """``tmux kill-session`` for *assignment* (local or remote).  Used by reset
    to stop a live session before finalizing.  Returns True when the kill ran."""
    import subprocess  # noqa: PLC0415

    from coord.interactive import (  # noqa: PLC0415
        TmuxHost,
        tmux_session_name,
    )

    if not assignment.assignment_id:
        return False
    host = TmuxHost(ssh_target=_ssh_target_for(assignment, config))
    sname = tmux_session_name(assignment.assignment_id)
    try:
        subprocess.run(
            host.cmd(["kill-session", "-t", sname]),
            capture_output=True,
            timeout=20,
        )
        return True
    except Exception:  # noqa: BLE001 — best-effort
        return False


def _reconcile_issue_merges(
    board: "Board", config: "Config", repo_name: str, issue_number: int, *, dry_run: bool
) -> list[str]:
    """Run the merge reconcile sweep scoped to one issue (branch backfill +
    out-of-band-merge detection)."""
    from coord.reconcile import reconcile_board_merges  # noqa: PLC0415

    return reconcile_board_merges(
        board, config, repo=repo_name, issue=issue_number, dry_run=dry_run
    )


def _mark_terminal(assignment: "Assignment", config: "Config") -> None:
    """Best-effort terminal write via the issue_store seam — the fallback used
    only when :func:`_finalize_dead` itself raised.  Records a failed completion
    so the phantom row leaves ``running`` and persists to the canonical DB
    WITHOUT relying on ``save_board`` (which the diagnose path deliberately does
    not call — it would clobber the seam writes with a stale snapshot)."""
    from coord import issue_store  # noqa: PLC0415

    if not assignment.assignment_id:
        return
    repo_cfg = next((r for r in config.repos if r.name == assignment.repo_name), None)
    try:
        issue_store.post_completion(
            issue_store.CompletionRecord(
                assignment_id=assignment.assignment_id,
                machine_name=assignment.machine_name or "unknown",
                repo_name=assignment.repo_name,
                repo_github=(repo_cfg.github if repo_cfg else assignment.repo_name),
                issue_number=assignment.issue_number,
                exit_code=1,  # → failed terminal state (out of 'running')
                commits_ahead=0,
                branch=assignment.branch,
            )
        )
    except Exception:  # noqa: BLE001 — fallback of a fallback; leave the phantom
        pass


def _downgrade_empty_branch_done(assignment: "Assignment", config: "Config") -> str:
    """#1155: flip a wedged ``done``-with-empty-branch work row to ``advisory``
    via the issue_store seam (same seam :func:`_mark_terminal` uses for its
    own fallback write).  A ``done`` row with no branch has nothing to review
    — it must not sit in the Pipeline masquerading as reviewable work.
    Best-effort: on failure the row is left as-is and the caller's finding
    still surfaces the problem to the operator."""
    from coord import issue_store  # noqa: PLC0415

    if not assignment.assignment_id:
        return "skipped (no assignment_id)"
    repo_cfg = next((r for r in config.repos if r.name == assignment.repo_name), None)
    try:
        outcome = issue_store.post_completion(
            issue_store.CompletionRecord(
                assignment_id=assignment.assignment_id,
                machine_name=assignment.machine_name or "unknown",
                repo_name=assignment.repo_name,
                repo_github=(repo_cfg.github if repo_cfg else assignment.repo_name),
                issue_number=assignment.issue_number,
                exit_code=0,
                commits_ahead=0,  # → advisory terminal state (the #448 shape)
                branch=assignment.branch,
            )
        )
        return outcome.status
    except Exception as exc:  # noqa: BLE001 — best-effort recovery
        return f"failed ({exc})"


# ── orchestration ───────────────────────────────────────────────────────────


def diagnose_stage(
    board: "Board",
    config: "Config",
    repo_name: str,
    issue_number: int,
    stage: str,
    *,
    reset: bool = False,
    dry_run: bool = False,
) -> DiagnoseResult:
    """Diagnose *stage* of *repo_name* #*issue_number*; best-effort recover;
    always reconcile this issue's DB; optionally reset (non-destructive).

    Returns a :class:`DiagnoseResult`.  Board mutations happen on the board
    passed in; the caller is responsible for persisting it (the CLI/daemon do
    so after this returns) — consistent with ``reconcile_board_merges``.
    """
    res = DiagnoseResult(repo_name=repo_name, issue_number=issue_number, stage=stage)

    # #1083: `stage` came either from an explicit `--stage` or from
    # `current_stage()`'s newest-assignment lookup. `current_stage()` now
    # surfaces a non-standard assignment type (e.g. "test-author",
    # "mock-author", "smoke") verbatim instead of silently mapping it to
    # "work" — so a type this module has no recovery logic for lands here as
    # `stage` rather than being guessed at. Report that plainly instead of
    # running `_recover_work_like` against it (which was never validated for
    # these types) or, worse, silently returning an unrelated `work`/`plan`
    # row's status as if it were this stage's diagnosis (the bug reported in
    # #1083: `coord diagnose` picked an unrelated, already-merged assignment
    # instead of flagging the real problem).
    if stage not in STAGE_ASSIGNMENT_TYPES:
        assignments = stage_assignments(board, repo_name, issue_number, stage)
        latest = _latest(assignments)
        known = ", ".join(sorted(STAGE_ASSIGNMENT_TYPES))
        if latest is None:
            res.findings.append(
                f"no diagnosis available for assignment type {stage!r} — "
                f"coord diagnose only understands: {known} (and no {stage!r} "
                f"assignment exists for #{issue_number} either)"
            )
        else:
            res.findings.append(
                f"no diagnosis available for assignment type {stage!r} — "
                f"coord diagnose only understands: {known}. Latest {stage!r} "
                f"assignment: {latest.assignment_id} status={latest.status} "
                f"branch={latest.branch or '(none)'} machine={latest.machine_name}"
            )
        res.recovered = False
        res.needs_reset = False
        return res

    assignments = stage_assignments(board, repo_name, issue_number, stage)
    latest = _latest(assignments)

    if latest is None:
        res.findings.append(f"no {stage} assignment on the board for #{issue_number}")
        res.recovered = True  # nothing wedged
        # Still run the issue-wide cleanup below.
        _cleanup_issue(board, config, repo_name, issue_number, res, dry_run=dry_run, reset=reset)
        return res

    # The stage step owns *latest*; record it so the issue-wide cleanup pass
    # doesn't re-finalize the same row (finalize writes the DB, not this
    # in-memory board row, so its status would still read "running" here).
    handled = {latest.assignment_id} if latest.assignment_id else set()

    state = _session_state(latest, config)
    res.findings.append(
        f"{stage}: latest={latest.assignment_id} status={latest.status} "
        f"session={state} machine={latest.machine_name}"
    )
    if stage in ("work", "test", "merge"):
        _flag_contradictory_failed(latest, res)

    # #2087: a `running`/`pending` row naming a machine that isn't a
    # configured machine can never be probed — `_session_state` above
    # already returns "unknown" for exactly that reason ("machine_name set
    # but unknown in config — can't probe safely"). Before this,
    # every stage's best-effort recovery below had no branch for
    # state=="unknown", so it fell through to that recovery function's own
    # "stage looks healthy" catch-all — the textbook phantom `coord
    # diagnose` exists to find, reported as fine (the exact `work-repro`
    # shape: `machine=laptop`, `status=running` for 9h, "stage looks
    # healthy"). Flag it here, uniformly across every stage, BEFORE any
    # stage-specific recovery gets a chance to fall through to its healthy
    # catch-all — and reuse the existing non-destructive `--reset` path
    # (branch/commits always preserved) as the supported way to clear it.
    machine_unconfigured = (
        bool(latest.machine_name) and _resolve_machine(config, latest.machine_name) is None
    )
    if latest.status in ("running", "pending") and machine_unconfigured:
        res.findings.append(
            f"{stage}: machine {latest.machine_name!r} is not a configured "
            "machine (not in coordinator.yml) — a 'running' row with no "
            "host to poll is a phantom, not healthy; re-run with --reset to "
            "clear it (the branch and any commits are preserved)"
        )
        res.recovered = False
        res.needs_reset = True

    if reset:
        _do_reset(
            board, config, assignments, res, stage=stage,
            repo_name=repo_name, issue_number=issue_number, dry_run=dry_run,
        )
        _cleanup_issue(
            board, config, repo_name, issue_number, res,
            dry_run=dry_run, reset=reset, skip_ids=handled,
        )
        return res

    if latest.status in ("running", "pending") and machine_unconfigured:
        # Reported above; nothing safe to attempt without --reset (no host
        # to probe/finalize against) and no stage-specific recovery below
        # would do better than mis-file it as healthy.
        _cleanup_issue(
            board, config, repo_name, issue_number, res,
            dry_run=dry_run, reset=reset, skip_ids=handled,
        )
        return res

    # ── Best-effort recovery, per stage ─────────────────────────────────────
    if stage in ("review",):
        _recover_review(board, config, latest, state, res, dry_run=dry_run)
    elif stage in ("merge",):
        _recover_merge(board, config, repo_name, issue_number, latest, res, dry_run=dry_run)
    elif stage == "test":
        _recover_test(board, latest, state, res, config=config, dry_run=dry_run)
    else:  # work / plan
        _recover_work_like(board, config, latest, state, res, dry_run=dry_run)

    _cleanup_issue(
        board, config, repo_name, issue_number, res,
        dry_run=dry_run, reset=reset, skip_ids=handled,
    )
    return res


def _recover_review(
    board, config, latest, state, res: DiagnoseResult, *, dry_run: bool
) -> None:
    from coord.state import load_assignment_review_findings  # noqa: PLC0415

    has_findings = False
    if latest.assignment_id:
        cached = load_assignment_review_findings(latest.assignment_id)
        has_findings = bool(cached and (cached[1] or "").strip())

    verdict = latest.review_verdict
    if verdict == "request-changes" and not has_findings:
        res.findings.append("review verdict is request-changes but findings are EMPTY (#607 class)")
        if dry_run:
            res.findings.append("(dry-run) would recover findings from the session transcript")
            res.needs_reset = True
            return
        recovered_verdict = _recover_review_findings(latest, config)
        if recovered_verdict:
            res.actions_taken.append("recovered review findings from the session transcript → #603 store")
            res.recovered = True
        else:
            res.findings.append("findings NOT recoverable from transcript — re-review needed")
            res.needs_reset = True
    elif latest.status == "done" and verdict is None:
        # #812: review finalised as done but no verdict was ever captured.
        # The session likely failed to start (no session_id, no exit_code) or
        # exited before the reviewer ran coord report-result / the transcript-floor.
        # This is a permanent stuck state: nothing is running, TUI rendered it
        # blue/Active (now Fixed → red/Failed), Diagnose & Reset must handle it.
        res.findings.append(
            "review finalised as done but has no verdict — "
            "session likely failed to start or exited before verdict capture (#812)"
        )
        if dry_run:
            res.findings.append(
                "(dry-run) would try transcript recovery; if verdict not found, reset"
            )
            res.needs_reset = True
            return
        recovered_verdict = _recover_review_findings(latest, config)
        if recovered_verdict:
            res.actions_taken.append(
                "recovered review verdict/findings from session transcript"
            )
            res.recovered = True
        else:
            res.findings.append(
                "no verdict recoverable from transcript — "
                "reset to re-dispatch a fresh review"
            )
            res.needs_reset = True
    elif state == "dead" and latest.status == "running":
        res.findings.append("review session is dead but board still says running (phantom)")
        if not dry_run:
            # Try a transcript recovery first (captures the verdict if present),
            # then finalize to clear the phantom.
            if _recover_review_findings(latest, config):
                res.actions_taken.append("recovered review verdict/findings from transcript")
            res.actions_taken.append(f"finalized phantom review session ({_finalize_dead(latest, config)})")
            res.recovered = True
    elif state == "live" and _is_stale(latest):
        res.findings.append("review session is LIVE but stale (idle days) — capturing read-only, reset to clear")
        if not dry_run and _recover_review_findings(latest, config):
            res.actions_taken.append("captured current review findings from transcript (session left running)")
        res.needs_reset = True
    else:
        res.findings.append("review stage looks healthy")
        res.recovered = True


def _recover_merge(
    board, config, repo_name, issue_number, latest, res: DiagnoseResult, *, dry_run: bool
) -> None:
    actions = _reconcile_issue_merges(board, config, repo_name, issue_number, dry_run=dry_run)
    if actions:
        res.actions_taken.extend(actions)
        res.recovered = True
        return

    # #1601: reconcile_board_merges (above) only ever does two things —
    # backfill a missing `branch` and detect an out-of-band GitHub merge. It
    # has never asked the one question this issue is about: "is this done,
    # approved work even IN the merge queue at all?" A fix round's terminal
    # review verdict routinely lands on a *different* board row than
    # `latest`/the parent work row's own `review_state` (which can be stuck
    # at "dispatched" forever once a later round supersedes it — see the
    # #1566 incident this fixed), and when the periodic enqueue sweep
    # (`merge_queue.enqueue_approved_work`, run from the daemon passive tick)
    # misses its window, the branch is left approved-and-done with an EMPTY
    # merge_queue. Before this, that state was indistinguishable here from
    # actually healthy ("nothing to reconcile"). Ask the same question the
    # merge gate itself asks (`passes_merge_gates`) so this can never
    # disagree with what `coord merge --plan`/`--only` decide.
    _diagnose_unqueued_merge(
        board, config, repo_name, issue_number, latest, res, dry_run=dry_run
    )


def _diagnose_unqueued_merge(
    board, config, repo_name, issue_number, latest, res: DiagnoseResult, *, dry_run: bool
) -> None:
    """#1601: detect (and, when possible, fix) a done+approved branch with
    no merge_queue entry — the "nothing ever enqueued the merge" failure
    mode. Reuses `merge_queue`'s own winner-resolution
    (`group_branch_candidates`) and gate predicate (`passes_merge_gates`) so
    this reports the exact same verdict `coord merge --plan`/`--only` would,
    never a re-derived one."""
    from coord import merge_queue as mq  # noqa: PLC0415

    branch = getattr(latest, "branch", None)
    if not branch:
        res.findings.append("merge stage: nothing to reconcile")
        res.recovered = True
        return

    existing_queue = mq.load_queue()
    if any(getattr(e, "branch", None) == branch for e in existing_queue):
        res.findings.append("merge stage: nothing to reconcile")
        res.recovered = True
        return

    scoped_completed = [
        a for a in board.completed
        if a.repo_name == repo_name and getattr(a, "branch", None) == branch
    ]
    winners = mq.group_branch_candidates(scoped_completed)
    if not winners:
        res.findings.append("merge stage: nothing to reconcile")
        res.recovered = True
        return
    winner, _superseded = winners[0]

    # #2085: `winner` is a raw board Assignment — no `branch_head_sha`/
    # `repo_github`/`target_branch` attribute, so handing it straight to
    # `passes_merge_gates` made the #821 SHA-freshness check inside
    # `has_approved_review` permanently unconfirmable (fails closed on every
    # review carrying a real `review_head_sha`, i.e. virtually every modern
    # approval — this diagnostic would report "waiting on the pipeline" for
    # branches that actually pass every real gate). Build the same
    # live-anchored synthetic entry `coord.gates.build_gate_report` uses so
    # a genuinely fresh approval can still be confirmed. Falls back to the
    # raw `winner` row (gate then fails closed, never open) when the repo
    # isn't configured.
    from coord import github_ops  # noqa: PLC0415

    gate_entry = winner
    repo_cfg = config.repo(repo_name)
    if repo_cfg is not None:
        from coord.branch_model import resolve_base_branch_for_issue_number  # noqa: PLC0415

        target_branch = resolve_base_branch_for_issue_number(
            repo_cfg, repo_cfg.github, issue_number,
        )
        gate_entry = mq.live_gate_entry(winner, repo_cfg.github, target_branch, github_ops)

    if not mq.passes_merge_gates(gate_entry, config, board, gh_ops=github_ops):
        res.findings.append(
            f"merge stage: {branch} is done but not queued for merge, and does "
            f"not (yet) pass the review/smoke gates — winning row "
            f"{winner.assignment_id}: review_state={getattr(winner, 'review_state', None)!r} "
            f"review_verdict={getattr(winner, 'review_verdict', None)!r} "
            f"test_state={getattr(winner, 'test_state', None)!r}. Waiting on the "
            "pipeline, not wedged."
        )
        res.recovered = True
        return

    if dry_run:
        res.findings.append(
            f"merge stage: {branch} passes every merge gate but has NO "
            f"merge_queue entry — (dry-run) would enqueue {winner.assignment_id} now"
        )
        res.recovered = True
        return

    changed = mq.enqueue_approved_work(config, board)
    if changed:
        res.actions_taken.append(
            f"merge stage: {branch} passed every merge gate but had no "
            f"merge_queue entry (#1601) — enqueued {', '.join(changed)}"
        )
        res.recovered = True
    else:
        res.findings.append(
            f"merge stage: {branch} appears to pass every merge gate but "
            "enqueue_approved_work made no change for it — inspect by hand "
            "(e.g. already merged/closed on GitHub, or repo not in config)"
        )
        res.recovered = False


def _recover_test(
    board, latest, state, res: DiagnoseResult, *, config, dry_run: bool
) -> None:
    """#1605: the Test-gate check.

    ``latest`` here is the WORK row (``STAGE_ASSIGNMENT_TYPES["test"] ==
    ("work", "plan")`` — ``test_state`` lives on it, not on the
    ``type="smoke"`` child that actually ran the suite). Before this,
    nothing here ever looked past the work row itself: a work row wedged at
    ``test_state="running"`` with its smoke child already dead/failed fell
    straight through to :func:`_recover_work_like`'s catch-all
    ("stage looks healthy", since ``latest.status`` is already ``"done"``)
    — exactly the #1598 incident's "nothing to reconcile" symptom, and
    exactly why the daemon restart mentioned in that report didn't clear it
    either: nothing was ever looking at the CHILD row.
    """
    if latest.test_state == "running":
        smoke = next(
            (
                a
                for a in (board.active + board.completed)
                if a.type == "smoke"
                and a.review_of_assignment_id == latest.assignment_id
            ),
            None,
        )
        if smoke is None:
            res.findings.append(
                "⚠ test_state='running' but no Test-stage (smoke) assignment "
                "exists for this work row at all — the 'running' marker is "
                "set at dispatch (#1426) so the child row should exist "
                "(#1605 class)."
            )
            res.needs_reset = True
            return
        if (smoke.status or "") in ("failed", "cancelled"):
            res.findings.append(
                f"⚠ test_state='running' but the Test-stage worker "
                f"{smoke.assignment_id} already finished "
                f"(status={smoke.status!r}, failure_reason="
                f"{smoke.failure_reason or 'none recorded'!r}) — the parent "
                "verdict was never resolved (#1605)."
            )
            if dry_run:
                res.findings.append(
                    "(dry-run) would resolve test_state from the smoke "
                    "child's terminal status — passed, failed, or cleared "
                    "for re-dispatch depending on the #1590 environmental "
                    "classification"
                )
                res.needs_reset = True
                return
            from coord.reconcile import (  # noqa: PLC0415
                propagate_smoke_terminal_failure,
            )

            propagate_smoke_terminal_failure(
                parent_assignment_id=latest.assignment_id,
                failure_reason=smoke.failure_reason,
            )
            res.actions_taken.append(
                f"resolved stuck test_state='running' from smoke child "
                f"{smoke.assignment_id}'s terminal status={smoke.status!r} "
                "(#1605)"
            )
            res.recovered = True
            return
    _recover_work_like(board, config, latest, state, res, dry_run=dry_run)


def _recover_work_like(
    board, config, latest, state, res: DiagnoseResult, *, dry_run: bool
) -> None:
    if state == "dead" and latest.status in ("running", "pending"):
        res.findings.append("session is dead but board still says running (phantom)")
        if not dry_run:
            res.actions_taken.append(f"finalized phantom session ({_finalize_dead(latest, config)})")
            res.recovered = True
    elif latest.status == "failed" and latest.failure_reason:
        # #618: assignment failed at launch (worktree-add or similar).  The
        # failure_reason tells us what happened; if it's a "branch already checked
        # out" error we can detect and prune the blocking orphaned worktree.
        res.findings.append(
            f"launch-failed: {latest.failure_reason}"
        )
        if latest.branch:
            _prune_orphan_for_failed(board, config, latest, res, dry_run=dry_run)
        # Only mark recovered when _prune_orphan_for_failed did NOT set needs_reset
        # (dirty worktrees that couldn't be pruned mean the block is still present).
        if not res.needs_reset:
            res.recovered = True  # stage row is already terminal — nothing more needed
    elif latest.status == "failed":
        # #814: remote interactive sessions finalize as "failed" without setting
        # failure_reason (the local-launch code path sets it; the remote backstop
        # in finalize_remote_interactive_exit does not).  The stage row is already
        # terminal, but there may be a blocking branch lock on the remote machine
        # that will cause the next retry to fail identically — detect and fix it.
        res.findings.append("work stage failed (no captured failure reason)")
        if latest.branch:
            _prune_orphan_for_failed(board, config, latest, res, dry_run=dry_run)
        if not res.needs_reset:
            res.recovered = True
    elif state == "live" and _is_stale(latest):
        res.findings.append("session is LIVE but stale (idle days) — reset to clear it")
        res.needs_reset = True
    elif state == "live":
        res.findings.append("session is live and recent — left running")
        res.recovered = True
    elif (
        latest.type == "work"
        and latest.status == "done"
        and not (latest.branch or "").strip()
    ):
        # #1155: a `done` work row with no branch is never legitimately
        # reviewable — there is no branch to open a PR against. This is
        # exactly the shape the #448 zero-commit / unresolved-worktree guard
        # is supposed to catch before it ever reaches `done`; if one slipped
        # through anyway, downgrade it here rather than leaving it sitting in
        # the Pipeline indistinguishable from real reviewable work.
        res.findings.append(
            "work stage is 'done' but has no branch — not reviewable (#1155)"
        )
        if not dry_run:
            res.actions_taken.append(
                f"downgraded empty-branch done row to advisory "
                f"({_downgrade_empty_branch_done(latest, config)})"
            )
        res.recovered = True
    elif latest.type in ("work", "plan") and latest.status == "advisory":
        # #1606: an ADVISORY row is TERMINAL — no session is running and
        # nothing else on the board will ever move it forward, so this must
        # NOT fall through to "stage looks healthy" (that false-healthy read
        # is exactly what made the state invisible: `coord retry` refused it,
        # `--accept-advisory` adopted it, and this diagnose call said
        # everything was fine). Ask GitHub the same zero-commit question
        # #1534's review gate asks, so a genuine zero-commit exit (nothing
        # pushed — `coord retry` now handles this) is reported distinctly
        # from the #1357 false-positive shape (real commits present — that
        # one needs `coord drive --accept-advisory`, not a diagnose fix).
        #
        # Deliberately `latest.type in ("work", "plan")`, not just "work":
        # `_recover_work_like` also runs for `stage in ("plan", "test",
        # "merge")` (STAGE_ASSIGNMENT_TYPES["work"] itself is `("work",
        # "plan")` — a plan row can be `latest` for `--stage work` too), and
        # `reconcile.py`'s advisory transition sets `done.status =
        # "advisory"` unconditionally before its type-specific branches, so
        # a zero-commit `type="plan"` row CAN land here. Without this it
        # would silently fall through to the "stage looks healthy" catch-all
        # below — the exact false-healthy read this fix exists to close.
        stage_label = "work" if latest.type == "work" else "plan"
        ahead = _work_advisory_commits_ahead(latest, config)
        if ahead == 0:
            res.findings.append(
                f"{stage_label} stage is 'advisory' with 0 commits on its "
                "branch — nothing was pushed, so there is nothing to test, "
                f"review, or merge; re-dispatch with `coord retry "
                f"{latest.assignment_id}`"
            )
            res.recovered = False
        elif ahead is None:
            res.findings.append(
                f"{stage_label} stage is 'advisory' but its commit count "
                "against the base branch could not be confirmed (gh lookup "
                f"failed) — not reporting healthy; inspect by hand: coord "
                f"log {latest.assignment_id}"
            )
            res.recovered = False
        else:
            res.findings.append(
                f"{stage_label} stage is 'advisory' with {ahead} commit(s) "
                "on its branch — the #1357 false-positive signature, not a "
                "genuine zero-commit exit; `coord retry` refuses to touch "
                "it on purpose, use `coord drive --accept-advisory` to "
                "proceed"
            )
            res.recovered = True
    elif latest.type in ("work", "plan") and latest.status == "refused_policy":
        # #2234: a REFUSED_POLICY row is TERMINAL exactly like ADVISORY
        # above — no session is running and nothing else on the board will
        # move it forward — so this must NOT fall through to "stage looks
        # healthy" either; that's the same false-healthy read the ADVISORY
        # branch exists to close, and `coord diagnose` is called out in
        # CLAUDE.md as a first-line triage tool.
        #
        # Unlike ADVISORY, `refused_policy` is only ever set when the
        # worker's own zero-commit exit cited a standing repo-rule
        # prohibition (`coord.agent.REFUSED_POLICY`, set from the same
        # `_ZERO_COMMIT_TYPES` gate as advisory in `_reap`) — there is no
        # #1357 false-positive shape to disambiguate here (a refused_policy
        # row is ALWAYS 0 commits by construction, see `coord.agent.
        # AgentServer._reap`), so this doesn't need the ahead-count probe
        # the ADVISORY branch above does.
        stage_label = "work" if latest.type == "work" else "plan"
        res.findings.append(
            f"{stage_label} stage is 'refused_policy' — the worker correctly "
            "refused the dispatched work on a standing repo-rule "
            "prohibition; `coord retry` refuses to touch it on purpose "
            "(retrying reproduces the identical refusal, since the rule "
            "isn't going anywhere) — needs the coordinator: do the work "
            "directly, or re-scope the issue so its deliverable isn't "
            "coordinator-only"
        )
        res.recovered = False
    else:
        res.findings.append("stage looks healthy")
        res.recovered = True


def _work_advisory_commits_ahead(assignment: "Assignment", config: "Config") -> int | None:
    """#1606: commits *assignment*'s branch carries over the repo's base
    branch, or ``None`` when it cannot be confirmed.

    Thin wrapper (kept as its own name so tests can monkeypatch it without
    reaching into ``github_ops``) around
    :func:`coord.github_ops.branch_commits_ahead_for_assignment` — the one
    shared implementation `coord retry`'s advisory gate
    (``coord/commands/dispatch.py``) also calls, rather than each keeping
    its own copy of "branch empty -> 0, repo missing -> None, else ask
    GitHub". Reuses the ``gh api compare`` call (the same one #1534's review
    zero-commit gate uses) rather than a local git checkout — `coord
    diagnose` runs on the daemon host, which has no guarantee of a local
    clone of every worker's branch.

    #2324: ``None`` here means the lookup genuinely couldn't be confirmed
    (network/auth/rate-limit/repo-missing) — a head branch GitHub positively
    404s, with the base branch still resolving, comes back as ``0`` instead
    (see :func:`coord.github_ops.branch_commits_ahead`), since a deleted
    branch is proof nothing was pushed, not an unknown.
    """
    from coord import github_ops  # noqa: PLC0415

    return github_ops.branch_commits_ahead_for_assignment(assignment, config)


def _prune_orphan_for_failed(
    board, config, latest: "Assignment", res: DiagnoseResult, *, dry_run: bool
) -> None:
    """#618/#814: if *latest* is a failed launch, detect and prune the orphaned
    worktree that caused the "branch already checked out" collision.

    Also checks (#814) whether the blocking holder is the repo BASE checkout
    (~/src/<repo>) on the assignment's machine.  Coord-managed worktrees are
    under ``~/.coord/worktrees/`` and can be force-removed; the base checkout
    must NEVER be removed — instead, ``git checkout <default_branch>`` frees the
    branch.  This second check is performed remotely via SSH when the assignment
    ran on a different machine.
    """
    branch = latest.branch
    if not branch:
        return
    repo_name = latest.repo_name
    repo_cfg = next((r for r in config.repos if r.name == repo_name), None)
    if repo_cfg is None:
        return

    # Find the repo path on the local machine.
    repo_path: Path | None = None
    for machine in config.machines:
        rp = machine.repo_path(repo_name)
        if rp:
            candidate = Path(rp).expanduser()
            if candidate.exists():
                repo_path = candidate
                break
    if repo_path is None:
        # #814: even without a local path, attempt the remote base-checkout check.
        _maybe_fix_base_checkout_lock(latest, config, branch, res, dry_run=dry_run)
        return

    active_ids = _active_assignment_ids_for_repo(board, repo_name)
    orphans = _find_orphaned_worktrees(repo_path, branch, active_assignment_ids=active_ids)
    if not orphans:
        # #814: no local coord worktree holding the branch — check whether the
        # BASE checkout on the assignment's machine is the blocker.
        _maybe_fix_base_checkout_lock(latest, config, branch, res, dry_run=dry_run)
        return

    res.findings.append(
        f"found {len(orphans)} orphaned worktree(s) holding branch {branch!r}: "
        + ", ".join(str(p) for p in orphans)
    )
    if dry_run:
        res.findings.append(
            f"(dry-run) would prune {len(orphans)} orphaned worktree(s) "
            "(re-run without --dry-run to remove)"
        )
        return

    removed, skipped = _prune_orphaned_worktrees(repo_path, orphans)
    if removed:
        res.actions_taken.append(
            f"pruned {len(removed)} orphaned worktree(s): "
            + ", ".join(str(p) for p in removed)
        )
    if skipped:
        res.findings.append(
            f"{len(skipped)} worktree(s) skipped (uncommitted work — inspect manually): "
            + ", ".join(str(p) for p in skipped)
        )
        res.needs_reset = True


def _maybe_fix_base_checkout_lock(
    latest: "Assignment",
    config: "Config",
    branch: str,
    res: DiagnoseResult,
    *,
    dry_run: bool,
) -> None:
    """#814: detect and optionally fix a base-checkout branch lock on the
    assignment's machine (local or remote).

    When ``~/src/<repo>`` on the target machine is checked out on *branch*,
    ``git worktree add`` refuses to create a worktree for that branch, causing
    launch failures that loop uselessly.  The fix is ``git checkout
    <default_branch>`` in the base checkout — NEVER pruning or deleting it
    (invariant #561).

    Works for both local assignments (SSH to ``localhost``) and remote ones.
    SSH failures are silently ignored — conservative: if we can't check we
    don't report a false "healthy".
    """
    machine = next(
        (m for m in config.machines if m.name == latest.machine_name), None
    )
    if machine is None:
        return
    repo_name = latest.repo_name
    repo_cfg = next((r for r in config.repos if r.name == repo_name), None)
    if repo_cfg is None:
        return

    rp_str = machine.repo_path(repo_name)
    if not rp_str:
        return
    # Build the $HOME-form path for the remote shell.
    if rp_str.startswith("~/"):
        remote_repo_sh = "$HOME/" + rp_str[2:]
    elif rp_str == "~":
        remote_repo_sh = "$HOME"
    else:
        remote_repo_sh = rp_str

    default_branch = repo_cfg.default_branch or "main"

    try:
        from coord.interactive import (  # noqa: PLC0415
            _holder_is_base_checkout,
            _remote_base_checkout_free_branch,
            find_remote_branch_holder,
        )
    except ImportError:
        return  # interactive module unavailable — skip gracefully

    holder = find_remote_branch_holder(machine.host, remote_repo_sh, branch)
    if holder is None or not _holder_is_base_checkout(holder):
        return  # not the base-checkout case

    res.findings.append(
        f"base checkout {holder!r} on {machine.host} is on branch {branch!r}"
        f" — this blocks worktree creation for {branch!r}"
    )
    if dry_run:
        res.findings.append(
            f"(dry-run) would checkout {default_branch!r} in {holder!r}"
            f" on {machine.host} to free the branch"
        )
        return

    freed = _remote_base_checkout_free_branch(
        machine.host, remote_repo_sh, default_branch,
    )
    if freed:
        res.actions_taken.append(
            f"freed base checkout {holder!r} on {machine.host}:"
            f" checked out {default_branch!r} (was on {branch!r})"
        )
    else:
        res.findings.append(
            f"could not auto-free base checkout on {machine.host} —"
            f" run manually: ssh {machine.host}"
            f" 'git -C {remote_repo_sh} checkout {default_branch}'"
        )
        res.needs_reset = True


def _do_reset(
    board, config, assignments, res: DiagnoseResult, *, stage: str,
    repo_name: str, issue_number: int, dry_run: bool,
) -> None:
    """Stage-aware, non-destructive reset (KEEP the branch + commits always).

    The shape of "reset" depends on the stage's state, not just on a live
    session: a completed REVIEW has no session to kill — its data lives in the
    board rows + #603 store — so resetting it means wiping that data so the
    stage goes back to grey/unrun and re-reviewable.
    """
    latest = _latest(assignments)
    if latest is None:
        res.findings.append(f"no {stage} stage to reset")
        res.recovered = True
        return

    if stage == "review":
        # #1180: `_reset_review_stage`'s `assignment_id` means "the id of the
        # assignment BEING reviewed" — that's the FK the review rows carry and
        # the id the test-author/mock-author review_state reset keys on. But
        # STAGE_ASSIGNMENT_TYPES["review"] matches ('review','test-author',
        # 'mock-author'), so `latest` is EITHER:
        #   - the reviewed row itself (test-author/mock-author wedged before a
        #     review was ever dispatched — the JIT-slice case), or
        #   - a type='review' row, whose OWN id is meaningless here; the
        #     reviewed assignment is its `review_of_assignment_id` FK
        #     (set at review.py: review_of_assignment_id=completed.assignment_id).
        # Passing the review row's own id would match no FK, silently resetting
        # nothing — resolve the reviewed id explicitly.
        target_id = (
            latest.review_of_assignment_id
            if latest.type == "review" and latest.review_of_assignment_id
            else latest.assignment_id
        )
        _reset_review_stage(
            config, repo_name, issue_number, res,
            dry_run=dry_run, assignment_id=target_id,
        )
        return
    if stage == "test":
        _reset_test_stage(repo_name, issue_number, res, dry_run=dry_run)
        return

    # work / plan / merge — clear a live/phantom session, KEEP the branch.
    # (Merge reset deliberately does NOT un-merge; it only clears a stuck
    # session/row, so a clean re-attempt is possible without rewriting history.)
    if dry_run:
        res.findings.append("(dry-run) would reset: stop session, finalize, clear row — branch kept")
        res.needs_reset = True
        return
    if _session_state(latest, config) == "live" and _kill_session(latest, config):
        res.actions_taken.append("stopped the live session (tmux kill-session)")
    try:
        res.actions_taken.append(f"finalized session ({_finalize_dead(latest, config)})")
    except Exception as exc:  # noqa: BLE001 — fall back to a direct terminal mark
        res.findings.append(f"finalize failed ({exc}); marking row terminal directly")
        _mark_terminal(latest, config)
        res.actions_taken.append("marked stage row terminal")
    res.reset_performed = True
    res.recovered = True
    res.branch_preserved = True
    res.actions_taken.append("branch preserved — stage is re-dispatchable")


def _reset_review_stage(
    config, repo_name: str, issue_number: int, res: DiagnoseResult, *,
    dry_run: bool, assignment_id: str,
) -> None:
    """Wipe a completed review so the stage returns to grey + re-reviewable:
    delete the ``type='review'`` rows, reset the work's ``review_state``, and
    purge the #603 ``source='review'`` context entries (the operator's
    'completely cleared out' choice).  No branch/commits touched.

    #1180: ``assignment_id`` is **the id of the assignment being reviewed** —
    NOT the id of a ``type='review'`` row. It is threaded through to both the
    delete and the reset so a milestone tracking issue with multiple
    ``test-author``/``mock-author`` slices only has the *targeted* slice's
    review data touched — see ``state.delete_assignments_for_issue`` and
    ``state.reset_work_review_state`` docstrings for the aliasing hazard this
    guards against. ``work``/``plan`` behavior is unchanged (still issue-wide,
    which is safe for those types).

    Callers must resolve this themselves: the review stage's ``latest`` row can
    be either the reviewed assignment (test-author/mock-author, no review
    dispatched yet) or a ``type='review'`` row pointing at it via
    ``review_of_assignment_id`` — the two cases need different resolution. See
    ``_do_reset``.
    """
    from coord import state  # noqa: PLC0415

    if dry_run:
        res.findings.append(
            "(dry-run) would DELETE the review rows, reset work review_state → "
            "pending, and purge #603 review notes (box → grey, re-reviewable)"
        )
        res.needs_reset = True
        return
    deleted = state.delete_assignments_for_issue(
        repo_name, issue_number, types=("review",),
        review_of_assignment_id=assignment_id,
    )
    res.actions_taken.append(f"deleted {deleted} review row(s) → stage grey")
    updated = state.reset_work_review_state(
        repo_name, issue_number, assignment_id=assignment_id
    )
    res.actions_taken.append(f"reset review_state→pending on {updated} work row(s) (re-reviewable)")
    purged = state.clear_issue_context_by_source(repo_name, issue_number, "review")
    res.actions_taken.append(f"purged {purged} #603 review note(s)")
    res.reset_performed = True
    res.recovered = True
    res.branch_preserved = True


def _reset_test_stage(
    repo_name: str, issue_number: int, res: DiagnoseResult, *, dry_run: bool
) -> None:
    """Clear the Test-gate verdict so the issue is re-testable.  No code touched."""
    from coord import state  # noqa: PLC0415

    if dry_run:
        res.findings.append("(dry-run) would clear test_state → re-testable")
        res.needs_reset = True
        return
    updated = state.reset_work_test_state(repo_name, issue_number)
    res.actions_taken.append(f"cleared Test verdict on {updated} work row(s) (re-testable)")
    res.reset_performed = True
    res.recovered = True
    res.branch_preserved = True


def _cleanup_issue(
    board,
    config,
    repo_name,
    issue_number,
    res: DiagnoseResult,
    *,
    dry_run: bool,
    reset: bool,
    skip_ids: set | None = None,
) -> None:
    """Always-on, issue-scoped DB *scan*: any OTHER phantom ``running`` rows for
    this issue whose session is dead are reported. They are only FINALIZED
    (a write) when *reset* is set.

    #1658: this sweep looks past the one row the operator explicitly asked to
    diagnose — at every other row for the issue — so a false "dead" verdict
    here (or a genuinely-live row this scan wasn't asked about) has no
    operator-reviewed finding backing it the way the targeted stage's own
    best-effort recovery does. That over-reach is exactly what turned a
    plain, no-flags ``coord diagnose --stage test`` into a write against a
    live headless review worker's row (finalized to ``failed`` mid-review):
    ``reset`` was ``False`` and it wrote anyway. Now a phantom found here is
    only a *recommendation* — ``needs_reset=True`` plus a finding telling the
    operator to re-run with ``--reset`` — unless ``--reset`` was already
    passed, in which case the existing finalize behaviour is unchanged.

    #2087 (fix-review nit): a sibling row on an unconfigured machine is a
    phantom too, by the same reasoning ``diagnose_stage`` already applies to
    the row it was explicitly asked about — but ``_session_state`` reports
    "unknown" (not "dead") for it, since a machine that isn't in
    ``coordinator.yml`` can't be probed at all. Before this, that "unknown"
    made this sweep silently skip it: a milestone tracking issue with a
    ``work`` row *and* a sibling ``smoke`` row, both on the same
    unconfigured machine, diagnosed with ``--stage work``, reported the
    ``work`` row correctly but said nothing about the ``smoke`` sibling.
    Flag it the same way regardless of session probe result — reuses
    ``_finalize_dead``, already safe for an unconfigured machine (no host to
    probe/ssh into; see ``_do_reset``'s identical use for the targeted row).
    """
    skip = skip_ids or set()
    for a in (board.active + board.completed):
        if a.issue_number != issue_number or a.repo_name != repo_name:
            continue
        if a.assignment_id in skip:
            continue
        if a.status not in ("running", "pending"):
            continue
        machine_unconfigured = (
            bool(a.machine_name) and _resolve_machine(config, a.machine_name) is None
        )
        if not machine_unconfigured and _session_state(a, config) != "dead":
            continue
        if machine_unconfigured:
            res.findings.append(
                f"cleanup: phantom {a.type} row {a.assignment_id} — machine "
                f"{a.machine_name!r} is not a configured machine (not in "
                "coordinator.yml)"
            )
        else:
            res.findings.append(f"cleanup: phantom {a.type} row {a.assignment_id} (session dead)")
        if not reset:
            res.findings.append(
                f"cleanup: would finalize phantom {a.type} row {a.assignment_id} "
                "— re-run with --reset to clear it"
            )
            res.needs_reset = True
            continue
        if dry_run:
            res.findings.append(f"(dry-run) would finalize phantom {a.type} row {a.assignment_id}")
            res.needs_reset = True
            continue
        try:
            _finalize_dead(a, config)
            res.actions_taken.append(f"cleanup: finalized phantom {a.type} row {a.assignment_id}")
        except Exception as exc:  # noqa: BLE001
            _mark_terminal(a, config)
            res.actions_taken.append(f"cleanup: marked phantom row {a.assignment_id} terminal ({exc})")


def _is_stale(assignment: "Assignment", *, max_age_hours: float = 12.0) -> bool:
    """A still-running session whose dispatch is older than *max_age_hours* is
    treated as stale (abandoned/idle) — recovery can't safely finalize a live
    session, so these escalate to a reset offer."""
    if not assignment.dispatched_at:
        return False
    return (time.time() - assignment.dispatched_at) > max_age_hours * 3600.0


# ── #2536: fleet-wide phantom-row self-heal sweep ───────────────────────────
#
# `_cleanup_issue` above already recovers phantom `running` rows — but only
# ONE issue at a time, and only when a human happens to run `coord diagnose
# <repo> <issue> --reset`. Nothing runs that recovery on its own, so a
# phantom row can sit indefinitely holding its repo's entire drive-queue
# concurrency slot (`coord/drive_queue.py`'s own capacity comment: "a drive
# whose observer died still holds its repo's slot until something reconciles
# it"). :func:`sweep_dead_running_rows` is that "something" — a fleet-wide,
# board-scanning counterpart callable periodically (see
# `coord.notify._sweep_phantom_rows`, which piggybacks it on
# `coord-notify.timer`'s existing 5-minute cadence) rather than only on
# operator demand.

# How far past a row's own needs-attention wall-clock threshold it must sit
# before this sweep will treat a CONFIRMED-dead session as safe to
# auto-heal. Two full `coord-notify.timer` cycles (5min cadence) of margin:
# large enough that `coord.notify.detect_needs_attention` has already had a
# full extra tick to flag (and a human a full extra tick to notice) the same
# row via the existing `needs_attention` comment before this sweep ever
# touches it, small enough that a genuinely dead row doesn't sit much longer
# than it already would have. Never a substitute for the liveness check
# below — a row can be arbitrarily old and still left alone if its session
# reads "live" or "unknown".
PHANTOM_HEAL_BUFFER_SECONDS = 600.0


@dataclass
class PhantomRowHeal:
    """One board row this sweep confirmed dead, aged out past its own
    needs-attention threshold, and healed automatically (or, in a dry run,
    WOULD heal) — see :func:`sweep_dead_running_rows`."""

    assignment_id: str
    machine_name: str
    repo_name: str
    issue_number: int
    stage: str
    detail: str
    action: str = ""


def sweep_dead_running_rows(
    board: "Board",
    config: "Config",
    *,
    now: float | None = None,
    dry_run: bool = False,
) -> list[PhantomRowHeal]:
    """The automatic counterpart to a human running ``coord diagnose <repo>
    <issue> --reset`` on a phantom ``running`` row (#2536).

    Scans every ``running``/``pending`` row on *board* (fleet-wide, not
    scoped to one issue — unlike :func:`_cleanup_issue`) and, for each,
    applies the same two guards a careful human would before touching a
    row that merely *looks* wedged:

    1. **Confirmed dead, never ambiguous** — liveness is read via
       :func:`_session_state` (tmux first, local or remote, falling back to
       the assignment's own recorded machine's ``/status``), and only a
       ``"dead"`` verdict is actionable. ``"unknown"`` (an unresolvable or
       unconfigured machine, an unreachable agent, a probe error) is always
       left alone, exactly like ``"live"`` — the same #1870 caution
       ``coord/drive_queue.py`` documents ("liveness cannot be verified from
       here, so this is UNKNOWN, not dead") applies here too: only the
       assignment's own recorded machine can make this call.
    2. **Aged out, not merely between turns** — the row must be running
       longer than its own ``config.pipeline.attention_threshold_for(...)``
       (the same wall-clock threshold
       :func:`coord.notify.detect_needs_attention` uses) PLUS
       :data:`PHANTOM_HEAL_BUFFER_SECONDS` of margin, so a session that is
       merely idle between turns or briefly disconnected is never raced.

    Recovery, once both guards pass, is byte-for-byte
    :func:`_finalize_dead` — the exact non-destructive action ``coord
    diagnose --reset`` runs for a phantom row: branch and commits are always
    preserved, and the stage becomes re-dispatchable. Writes go through the
    same seam (``issue_store``, not ``save_board``) :func:`_cleanup_issue`
    already uses, so this function never mutates *board* and the caller
    does not need to persist it. Freeing the row's repo's drive-queue slot
    is then a side effect of the write, not something this function or its
    caller has to do explicitly — the next board read simply no longer
    counts the row as ``running``.

    Returns one :class:`PhantomRowHeal` per row healed (or, when *dry_run*,
    per row that WOULD be healed), so the caller can post a GitHub comment
    recording it — mirroring the existing ``needs_attention`` comment's
    posture, but reporting an action already taken rather than only a
    finding.
    """
    if now is None:
        now = time.time()

    healed: list[PhantomRowHeal] = []
    seen_ids: set[str] = set()
    for a in (board.active + board.completed):
        if a.status not in ("running", "pending"):
            continue
        if not a.assignment_id or a.assignment_id in seen_ids:
            continue
        seen_ids.add(a.assignment_id)
        if not a.dispatched_at:
            continue  # nothing to compute an age from — never guess

        threshold = config.pipeline.attention_threshold_for(
            a.type or "work",
            provider_name=a.provider_name,
            review_of_assignment_id=a.review_of_assignment_id,
        )
        if threshold == float("inf"):
            continue  # interactive session type — no wall-clock concept
        running_for = now - a.dispatched_at
        if running_for <= threshold + PHANTOM_HEAL_BUFFER_SECONDS:
            continue  # not aged out yet — don't race a session between turns

        if _session_state(a, config) != "dead":
            # "unknown" or "live" — only ever act on a CONFIRMED-dead read,
            # never an ambiguous one (#1870/#2536).
            continue

        detail = (
            f"{a.type or 'work'} row running {running_for / 60.0:.0f}m "
            f"(past its {threshold / 60.0:.0f}m needs-attention threshold + "
            f"{PHANTOM_HEAL_BUFFER_SECONDS / 60.0:.0f}m buffer) — session "
            f"confirmed dead on machine {a.machine_name!r}"
        )
        if dry_run:
            healed.append(PhantomRowHeal(
                assignment_id=a.assignment_id,
                machine_name=a.machine_name or "unknown",
                repo_name=a.repo_name,
                issue_number=a.issue_number,
                stage=a.type or "work",
                detail=detail,
                action="(dry-run) would finalize the phantom session",
            ))
            continue

        try:
            action = f"finalized phantom session ({_finalize_dead(a, config)})"
        except Exception as exc:  # noqa: BLE001 — fall back to a direct terminal mark
            _mark_terminal(a, config)
            action = f"finalize failed ({exc}); marked row terminal directly"

        healed.append(PhantomRowHeal(
            assignment_id=a.assignment_id,
            machine_name=a.machine_name or "unknown",
            repo_name=a.repo_name,
            issue_number=a.issue_number,
            stage=a.type or "work",
            detail=detail,
            action=action,
        ))

    return healed


# ── #2803: fleet-wide stuck test_state='running' watchdog ───────────────────
#
# `test_state='running'` is the marker `dispatch_smoke` stamps on the PARENT
# work row the instant it dispatches a Test-stage (`type="smoke"`) child
# (`coord/smoke.py`, #1426) — transient by design, meant to be cleared only
# by an inbound verdict. `_recover_test` above already resolves the
# contradiction when the child died `failed`/`cancelled` without ever
# reporting pass/fail, but only when a HUMAN happens to run `coord diagnose
# <repo> <issue> --stage test`, and only for that one narrow terminal shape.
# Nothing ran it on its own, and nothing at all covered a child that reached
# `status="done"` (the child believes it succeeded, but the write that
# should have carried its verdict onto the parent never landed — the DB-lock
# class of loss #2802 hardened the write path against, going forward) or a
# parent with no Test-stage child at all.
#
# Before this, the only thing that ever noticed a row stuck this way was
# #2273's 240-minute drive-session deadline — by which point the owning
# drive had burned its whole attempt budget, gone `blocked`, and blocked
# every `after=`-chained row behind it too (vimcode#555, 2026-08-26: five
# rows, ~4h). Worse, the deadline's own message ("no assignment was ever
# created for this run") and `coord drive-queue diagnose`'s independent
# re-derivation ("gate-blocked") both misdirect — a Test-stage assignment
# plainly WAS created, and neither name the stuck row at all.
# :func:`sweep_stuck_test_state_rows` is the automatic, fleet-wide
# counterpart — a bounded grace window measured from the actual cause, not a
# fixed 240-minute wait, and a message that names the stuck row and its
# child's real status.

# How long to wait, after the Test-stage child itself went terminal (or —
# for the "no child was ever created" case — after the parent work row
# itself went terminal), before treating a still-`running` parent
# `test_state` as a lost verdict rather than the ordinary, bounded
# propagation lag every terminal Test-stage completion has (see
# `coord.drive._decide_test`'s own comment: "a fresh `done` smoke completion
# has an expected, bounded propagation lag ... that is NOT this bug").
# Deliberately smaller than `PHANTOM_HEAL_BUFFER_SECONDS` above: that buffer
# guards against racing a session that might still be ALIVE; this one only
# ever looks at a child that has ALREADY reached a terminal status, so the
# only thing being waited out here is a slow write, never a slow worker.
STUCK_TEST_STATE_GRACE_SECONDS = 600.0  # 10 minutes


@dataclass
class StuckTestStateHeal:
    """One work row this sweep found wedged at ``test_state='running'`` well
    past its Test-stage child's own resolution, and cleared (or, in a dry
    run, WOULD clear) for a fresh Test-stage dispatch (#2803).

    Mirrors :class:`PhantomRowHeal`'s shape so ``coord notify`` can post one
    comment per row the same way.
    """

    assignment_id: str
    machine_name: str
    repo_name: str
    issue_number: int
    detail: str
    action: str = ""


def _latest_smoke_child(board: "Board", parent_id: str) -> "Assignment | None":
    """The most recently dispatched ``type="smoke"`` row for *parent_id*, or
    ``None``.

    Picks the newest by ``dispatched_at`` rather than the first match — a
    work row can carry more than one Test-stage leg over its lifetime
    (#2272's mute-leg retries), and resolving against a stale earlier leg
    would misreport (or, worse, mis-clear) the CURRENT leg's outcome.

    Ties on ``dispatched_at`` (including the ``or 0.0`` fallback for legs
    with no timestamp at all) break first-max-wins, per Python's ``max()``
    semantics — harmless in practice (two legs would need the same
    wall-clock second, or both be missing a timestamp), not a claim that a
    tie is meaningfully ordered.
    """
    candidates = [
        a for a in (board.active + board.completed)
        if a.type == "smoke" and a.review_of_assignment_id == parent_id
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda a: a.dispatched_at or 0.0)


def sweep_stuck_test_state_rows(
    board: "Board",
    config: "Config",
    *,
    now: float | None = None,
    dry_run: bool = False,
) -> list[StuckTestStateHeal]:
    """The automatic counterpart to a human running ``coord diagnose <repo>
    <issue> --stage test`` on a work row wedged at ``test_state='running'``
    (#2803) — see the module-level comment above for the full incident this
    closes.

    Scans every ``status="done"`` work-like row on *board* whose
    ``test_state == "running"`` (fleet-wide, not scoped to one issue — like
    :func:`sweep_dead_running_rows`). For each, classifies via its latest
    Test-stage (``type="smoke"``) child:

    * **No child found at all** — the ``dispatch_smoke``-stamped marker
      exists with nothing behind it (a lost row). Always resolved
      environmentally (never a work failure — there is no worker output to
      even blame).
    * **Child reached ``status in ("failed", "cancelled")``** — mirrors
      ``_recover_test``'s existing classification exactly (environmental vs.
      work, decided by :func:`coord.failure_class.classify_failure` from the
      child's own ``failure_reason``), just fleet-wide and automatic instead
      of issue-scoped and manual.
    * **Child reached ``status == "done"``** — the child itself believes it
      succeeded, but the parent's verdict write never landed. Always
      resolved environmentally: a lost write is a coordinator/infra fault,
      never a code defect.
    * **Child still ``running``/``pending`` itself, or nothing to anchor an
      age from** — left alone. A child that is still genuinely executing
      (or phantom-dead) is :func:`sweep_dead_running_rows`'s /
      ``coord.notify.detect_needs_attention``'s job — both key off the
      CHILD's own liveness/wall-clock, not the parent/child mismatch this
      sweep targets.

    Every actionable case above is gated on :data:`STUCK_TEST_STATE_GRACE_SECONDS`
    having elapsed since the anchor (the child's own ``finished_at``,
    falling back to ``dispatched_at``; the parent's own ``finished_at``/
    ``dispatched_at`` when there is no child at all) — an ordinary terminal
    Test-stage completion has an expected, bounded propagation lag before
    its verdict lands on the parent, and that lag is not this bug.

    Recovery is always :func:`coord.reconcile.propagate_smoke_terminal_failure`
    — the same, already-reviewed seam ``_recover_test`` and
    ``_reconcile_no_agent_record`` both use. It never fabricates a pass/fail
    verdict: an environmental clear resets ``test_state`` back to ``NULL``
    so ``dispatch_pending_smoke``'s ordinary bookkeeping (already run every
    daemon tick) re-dispatches a fresh Test stage on its own next pass; a
    work classification records ``test_state="failed"`` exactly like a
    normal non-zero-exit smoke completion would have, spending a bounded
    ``coord fix`` round exactly as it should have if the write had landed on
    time. Writes go through ``coord.state.record_test_verdict`` directly
    (not ``save_board``), so this function never mutates *board* and the
    caller does not need to persist it.

    Returns one :class:`StuckTestStateHeal` per row actually healed (or, when
    *dry_run*, per row that WOULD be healed). A row whose recovery write
    itself raises (e.g. sustained DB-lock contention, #2802) is deliberately
    left OUT of the returned list — it was not healed, `test_state` is still
    ``"running"``, and the caller (`coord.notify._sweep_stuck_test_state`)
    must not report it as a heal. It is logged and left for the next sweep
    tick (or a human via ``coord diagnose --stage test``) to retry.
    """
    from coord.models import WORK_LIKE_TYPES  # noqa: PLC0415

    if now is None:
        now = time.time()

    healed: list[StuckTestStateHeal] = []
    seen_ids: set[str] = set()
    for w in board.completed:
        if w.type not in WORK_LIKE_TYPES:
            continue
        if w.status != "done" or w.test_state != "running":
            continue
        if not w.assignment_id or w.assignment_id in seen_ids:
            continue
        seen_ids.add(w.assignment_id)

        smoke = _latest_smoke_child(board, w.assignment_id)

        if smoke is None:
            anchor = w.finished_at or w.dispatched_at
            cause = "no Test-stage (smoke) assignment exists for this work row at all"
            environmental: bool | None = True
            failure_reason = (
                "watchdog (#2803): test_state='running' with no Test-stage "
                "assignment found at all"
            )
        elif (smoke.status or "") in ("failed", "cancelled"):
            anchor = smoke.finished_at or smoke.dispatched_at
            cause = (
                f"Test-stage worker {smoke.assignment_id} already finished "
                f"(status={smoke.status!r}, failure_reason="
                f"{smoke.failure_reason or 'none recorded'!r}) but its "
                "verdict was never propagated to the parent"
            )
            environmental = None  # classify from failure_reason, like `_recover_test`
            failure_reason = smoke.failure_reason
        elif smoke.status == "done":
            anchor = smoke.finished_at or smoke.dispatched_at
            cause = (
                f"Test-stage worker {smoke.assignment_id} finished "
                "(status='done') but no verdict was ever recorded on the "
                "parent row"
            )
            environmental = True
            failure_reason = (
                f"watchdog (#2803): Test-stage worker {smoke.assignment_id} "
                "finished but the parent verdict write never landed (the "
                "DB-lock class of loss, #2802) — cleared for a fresh "
                "Test-stage dispatch"
            )
        else:
            continue  # still running/pending — not this sweep's job

        if anchor is None:
            continue  # nothing to compute an age from — never guess
        running_for = now - anchor
        if running_for <= STUCK_TEST_STATE_GRACE_SECONDS:
            continue  # ordinary propagation lag — not lost yet

        detail = (
            f"test_state='running' for {running_for / 60.0:.0f}m — {cause} "
            f"(past the {STUCK_TEST_STATE_GRACE_SECONDS / 60.0:.0f}m grace "
            "window)"
        )

        if dry_run:
            healed.append(StuckTestStateHeal(
                assignment_id=w.assignment_id,
                machine_name=w.machine_name or "unknown",
                repo_name=w.repo_name,
                issue_number=w.issue_number,
                detail=detail,
                action="(dry-run) would clear test_state for a fresh Test-stage dispatch",
            ))
            continue

        from coord.reconcile import propagate_smoke_terminal_failure  # noqa: PLC0415

        try:
            propagate_smoke_terminal_failure(
                parent_assignment_id=w.assignment_id,
                failure_reason=failure_reason,
                environmental=environmental,
            )
        except Exception as exc:  # noqa: BLE001 — never sink the sweep
            # The recovery WRITE itself raised (e.g. sustained DB-lock
            # contention — the exact #2802 failure class this watchdog
            # exists to route around). `test_state` is untouched, so this
            # row is NOT healed — do not report it as one. Appending a
            # `StuckTestStateHeal` here would make
            # `coord.notify._sweep_stuck_test_state` post a misleading
            # "auto-healed" GitHub comment for a row nothing happened to,
            # and since the row is still `test_state='running'` it would be
            # re-found (and re-commented) on every subsequent ~60s drain
            # tick for as long as the contention lasts — an unbounded loop
            # of duplicate, mislabeled comments on the same issue. Log and
            # leave it at `running`: the next tick retries silently, and a
            # human can still reach it via `coord diagnose --stage test`.
            log.warning(
                "sweep_stuck_test_state_rows: recovery write failed for %s "
                "(%s) — leaving test_state='running' for the next tick",
                w.assignment_id, exc,
            )
            continue

        healed.append(StuckTestStateHeal(
            assignment_id=w.assignment_id,
            machine_name=w.machine_name or "unknown",
            repo_name=w.repo_name,
            issue_number=w.issue_number,
            detail=detail,
            action="cleared test_state for a fresh Test-stage dispatch (#2803)",
        ))

    return healed


# ── #618: orphaned worktree detection + pruning ──────────────────────────────


def _find_orphaned_worktrees(
    repo_path: Path,
    branch: str | None,
    *,
    active_assignment_ids: set[str],
    worktrees_dir: Path | None = None,
) -> list[Path]:
    """Return worktree paths under *worktrees_dir* that hold *branch* but belong
    to no active (live-tmux OR running-DB) assignment.

    A worktree is "orphaned" when ALL of:
    * Its directory is under ``~/.coord/worktrees/`` (coordinator-managed).
    * Its git checkout has *branch* checked out (or *branch* is ``None``,
      meaning any branch — used for fleet sweeps).
    * Its assignment_id (derived from the directory name) is NOT in
      *active_assignment_ids* — i.e. no live tmux session and no running DB row.

    Dirty worktrees (uncommitted changes) are listed but callers must skip
    force-remove — they'd lose uncommitted work.  Use ``_prune_orphaned_worktrees``
    to prune them with an uncommitted-work guard.
    """
    if worktrees_dir is None:
        from coord.state import COORD_DIR  # noqa: PLC0415
        worktrees_dir = COORD_DIR / "worktrees"

    orphans: list[Path] = []
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=10.0,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    if result.returncode != 0:
        return []

    # Parse the porcelain output into blocks.
    current: dict[str, str] = {}
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            if current:
                _maybe_orphan(current, branch, worktrees_dir, active_assignment_ids, orphans)
                current = {}
        elif line.startswith("worktree "):
            current["worktree"] = line[len("worktree "):]
        elif line.startswith("branch "):
            raw_branch = line[len("branch "):]
            current["branch"] = (
                raw_branch[len("refs/heads/"):] if raw_branch.startswith("refs/heads/") else raw_branch
            )
    if current:
        _maybe_orphan(current, branch, worktrees_dir, active_assignment_ids, orphans)

    return orphans


def _maybe_orphan(
    entry: dict[str, str],
    branch: str | None,
    worktrees_dir: Path,
    active_assignment_ids: set[str],
    out: list[Path],
) -> None:
    """Append to *out* if *entry* is an orphaned worktree for *branch*.

    When *branch* is ``None`` any branch matches (fleet sweep).
    """
    wt_str = entry.get("worktree", "")
    if not wt_str:
        return
    if branch is not None and entry.get("branch", "") != branch:
        return
    wt_path = Path(wt_str)
    # Only consider coordinator-managed worktrees (under ~/.coord/worktrees/).
    try:
        wt_path.relative_to(worktrees_dir)
    except ValueError:
        return
    # The assignment_id is the directory name component immediately under worktrees_dir.
    aid = wt_path.relative_to(worktrees_dir).parts[0]
    if aid in active_assignment_ids:
        return
    out.append(wt_path)


def _prune_orphaned_worktrees(
    repo_path: Path,
    orphans: list[Path],
    *,
    force: bool = False,
) -> tuple[list[Path], list[Path]]:
    """Remove *orphans* from *repo_path* via ``git worktree remove``.

    Returns ``(removed, skipped)``.  Worktrees with uncommitted changes are
    skipped when *force* is ``False`` (default) so no uncommitted work is lost.
    After removal, runs ``git worktree prune`` to clean admin entries.
    """
    removed: list[Path] = []
    skipped: list[Path] = []
    for wt in orphans:
        if not wt.exists():
            removed.append(wt)
            continue
        if not force:
            # Check for uncommitted changes — skip dirty worktrees.
            try:
                dirty = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=str(wt),
                    capture_output=True,
                    text=True,
                    timeout=10.0,
                )
                if dirty.returncode == 0 and dirty.stdout.strip():
                    skipped.append(wt)
                    continue
            except (subprocess.SubprocessError, OSError):
                skipped.append(wt)
                continue
        try:
            r = subprocess.run(
                ["git", "worktree", "remove", str(wt), "--force"],
                cwd=str(repo_path),
                capture_output=True,
                timeout=15.0,
            )
            if r.returncode == 0:
                removed.append(wt)
            else:
                skipped.append(wt)
        except (subprocess.SubprocessError, OSError):
            skipped.append(wt)
    # Prune stale git admin entries regardless of what was removed.
    try:
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=str(repo_path),
            capture_output=True,
            timeout=10.0,
        )
    except (subprocess.SubprocessError, OSError):
        pass
    return removed, skipped


def _active_assignment_ids_for_repo(
    board: "Board", repo_name: str
) -> set[str]:
    """Return assignment IDs for *repo_name* that are still running/pending."""
    return {
        a.assignment_id
        for a in board.active
        if a.repo_name == repo_name and a.assignment_id
    }


def find_and_prune_orphaned_worktrees(
    board: "Board",
    config: "Config",
    repo_name: str,
    branch: str,
) -> tuple[list[Path], list[Path]]:
    """Detect and prune orphaned coordinator worktrees holding *branch*.

    Public entry point used by :func:`diagnose_stage` (Gap 2 of #618) and
    by the ``coord diagnose --orphan-worktrees`` fleet sweep.

    Returns ``(removed, skipped)`` path lists.  The *skipped* list contains
    worktrees that have uncommitted changes — the operator must inspect and
    clean them manually.
    """
    repo_cfg = next((r for r in config.repos if r.name == repo_name), None)
    if repo_cfg is None:
        return [], []

    # Find the local checkout path for this repo.  We need it to run git commands.
    # On a thin client the local checkout may not exist; fall back gracefully.
    repo_path: Path | None = None
    for machine in config.machines:
        rp = machine.repo_path(repo_name)
        if rp:
            candidate = Path(rp).expanduser()
            if candidate.exists():
                repo_path = candidate
                break
    if repo_path is None:
        return [], []

    active_ids = _active_assignment_ids_for_repo(board, repo_name)
    orphans = _find_orphaned_worktrees(repo_path, branch, active_assignment_ids=active_ids)
    if not orphans:
        return [], []
    return _prune_orphaned_worktrees(repo_path, orphans)
