"""Auto-loop: drive the review → fix → re-review cycle until clean.

When a **review** assignment completes with a verdict of ``request-changes``,
this module dispatches a fix worker on the same branch with the reviewer's
findings as the briefing.  When the fix worker finishes, the normal review
dispatch machinery (reconcile / notify) fires another review automatically —
creating a closed loop.

The loop terminates when:
  - A review approves the changes (verdict = ``approve``)
  - The iteration count hits ``pipeline.max_review_iterations``
  - A fix worker fails to dispatch (agent unreachable, no capable machine, etc.)

Config (coordinator.yml)::

    pipeline:
      auto_loop: true            # default true
      max_review_iterations: 3   # default 3

Integration:
  Called from ``coord.notify.run()`` after review completion transitions are
  posted.  ``run_for_review_transition`` loads the board, processes the review,
  saves if a fix was dispatched, and returns a list of :class:`LoopAction`
  for logging.

Data model:
  ``Assignment.review_iteration`` tracks the fix-round number.  The original
  work assignment has ``review_iteration=0``.  Each fix worker gets the
  previous worker's iteration + 1.  When the auto-loop sees a review that
  requests changes and the reviewed work's ``review_iteration >=
  max_review_iterations``, it posts a notice to GitHub and stops.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass

import httpx

from coord import sql
from coord.config import Config
from coord.db import is_lock_contention_error
from coord.dispatch import AGENT_PORT
from coord import github_ops
from coord.models import SEALED_PATH_AUTHOR_TYPES, Assignment, Board
from coord.review import (
    ReviewFindings,
    blocking_findings_confirmed_absent,
    dispatch_review,
    estimate_review_counts,
    parse_review_from_agent,
    parse_review_from_log,
)
from coord.board_service import read_board, write_board
from coord.state import record_dispatched_assignment
from coord.test_author import TEST_AUTHOR_SYSTEM_PROMPT, test_author_deny_commands

log = logging.getLogger(__name__)


# #1176 review: every assignment ``type`` value ``_dispatch_fix`` can emit,
# kept in sync with the ``fix_type`` mapping in that function. Exposed so
# ``coord.notify``'s fix-completion detector doesn't hardcode ``"work"`` and
# silently miss a newer fix-dispatch type — the same class of bug as #1141
# ("test-author was never added to WORK_LIKE_TYPES").
#
# #2302: "mock-author" (Gate A) is the same class of bug repeating — a
# request-changes fix for a mock-author row dispatched as plain "work",
# which trips coord/review.py's sealed-path tamper rule on every round
# (the fix worker isn't authorized to write tests/acceptance/ms-NN/, but
# that's exactly where the diff has to land) until max_review_iterations
# burns out. `_dispatch_fix` derives `fix_type` from
# ``coord.models.SEALED_PATH_AUTHOR_TYPES`` rather than a hardcoded string
# so this set — and this one — never drift from it again.
FIX_DISPATCH_TYPES: frozenset[str] = frozenset({"work"}) | SEALED_PATH_AUTHOR_TYPES


# Every :class:`LoopAction` kind whose production means the in-memory board was
# mutated and must be written back.  A fix was dispatched (new assignment row),
# an approve was parsed (so ``review_verdict`` is persisted for the merge gate,
# #253), an advisory-only review advanced the pipeline (#476 — ``review_verdict``
# flips to approve + ``review_state="done"`` so the merge gate unblocks; without
# this the gate suppresses the fix but the advance is never persisted and the PR
# silently can't merge), the work was found terminal (#522), or (#1663) a
# request-changes verdict was propagated onto the parent row without a fix
# dispatch.
#
# #1622: module-level rather than a local in ``_run_for_review_transition``
# because ``coord fix`` is now a second caller of
# :func:`process_review_completion` and has the same persist obligation.  Two
# copies of this tuple is exactly the drift shape #1601/#1624 keep hitting.
PERSIST_ACTION_KINDS: tuple[str, ...] = (
    "fix_dispatched", "approved", "approved_with_nits", "terminal_skip",
    "verdict_propagated",
)


# ── Action reporting ──────────────────────────────────────────────────────────

@dataclass
class LoopAction:
    """One step taken by the auto-loop, for logging and test assertions."""

    kind: str
    """One of:
    - ``"fix_dispatched"``     — a fix worker was dispatched
    - ``"approved"``           — review approved; no further action needed
    - ``"approved_with_nits"`` — review said request-changes but flagged no
                                 blocking findings (#476); pipeline advanced and
                                 no fix dispatched
    - ``"max_iterations"``     — loop stopped; user intervention required
    - ``"no_findings"``        — log had no structured REVIEW_VERDICT block
    - ``"no_work_found"``      — could not locate the work assignment on the board
    - ``"disabled"``           — auto_loop is disabled in config
    - ``"review_dispatched"``  — a re-review was dispatched after a fix worker completed
    - ``"iteration_cap_hit"``  — fix.review_iteration >= max_review_iterations;
                                 not dispatching another review
    - ``"terminal_skip"``      — the work's issue is already closed or its PR is
                                 already merged; no fix/review dispatched (#522)
    - ``"interactive_skip"``   — the fix was an interactive (claude-pty) session;
                                 its re-review is human-attended, so no headless
                                 review was dispatched (#555)
    - ``"test_gate_held"``     — pipeline.default_gates orders test before review
                                 and the fix's test verdict isn't in yet
                                 (``running``/``None``/``failed``); review_state
                                 was set to ``"pending"`` so
                                 ``dispatch_pending_reviews`` picks it up once a
                                 ``passed``/``skipped`` verdict lands (#1612)
    - ``"verdict_propagated"`` — #1663: the ``request-changes`` verdict was
                                 written onto the parent work row but NO fix
                                 worker was dispatched, because the caller asked
                                 for bookkeeping only (``dispatch_fixes=False``
                                 — the daemon drain, which must never spawn a
                                 fix worker; #476/#477)
    """
    assignment_id: str | None
    detail: str = ""


# ── Terminal-state guard (#522) ───────────────────────────────────────────────

def _work_is_terminal(
    work: Assignment,
    config: Config,
    *,
    cache: dict | None = None,
) -> bool:
    """True when *work* is already done on GitHub and must not be re-dispatched.

    Thin Assignment/Config-shaped wrapper over
    :func:`coord.github_ops.work_is_terminal` (the shared chokepoint guard,
    #522) — resolves the repo's GitHub slug and delegates.  Fail-open.

    #2639: derives ``trust_issue_closed`` from *work*'s own ``type`` via
    :func:`coord.models.trust_issue_closed_for` rather than trusting
    ``work_is_terminal``'s ``True`` default — this wrapper is reached by the
    #522 fix-dispatch and re-review-dispatch guards for reviews of ANY
    :data:`coord.models.WORK_LIKE_TYPES` row (#1574), including
    test-author/mock-author, whose ``issue_number`` is a milestone tracking
    issue rather than their own deliverable.
    """
    repo = config.repo(work.repo_name)
    if repo is None or not repo.github:
        return False

    from coord import github_ops  # noqa: PLC0415
    from coord.models import trust_issue_closed_for  # noqa: PLC0415

    return github_ops.work_is_terminal(
        repo.github,
        work.issue_number,
        work.branch,
        cache=cache,
        trust_issue_closed=trust_issue_closed_for(work.type),
    )


# ── Core logic ────────────────────────────────────────────────────────────────

def _load_review_findings(
    review: Assignment,
    log_path: str | None,
    machine_host: str | None,
    repo_github: str | None = None,
) -> ReviewFindings | None:
    """Resolve a reviewer's structured findings.

    Resolution order, cheapest first:
    1. **DB cache** — `notify` (or `report-result --body-file`) populates
       `review_findings` on the row.  Hit means zero I/O.
    2. **Local log file** — works when the review ran on this machine.
    3. **Agent HTTP `/logs/<id>`** — fetches the worker's full log
       from the remote agent (claude -p reviews on another machine).
    4. **GitHub message bus** — when `repo_github` is supplied, recover the
       findings posted to the issue under a `coord:review-findings` marker.
       This is the cross-machine path for INTERACTIVE (claude-pty) reviews,
       which have no parseable log and may not be in this machine's DB.

    Returns `None` only when ALL sources fail.
    """
    # 1. DB cache — fastest.
    if review.assignment_id:
        try:
            from coord.state import load_assignment_review_findings  # noqa: PLC0415
            cached = load_assignment_review_findings(review.assignment_id)
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "auto_loop: DB cache lookup failed for %s: %s",
                review.assignment_id, exc,
            )
            cached = None
        if cached is not None:
            verdict, body = cached
            return ReviewFindings(verdict=verdict, body=body)

    # 2. Local log file.
    findings = parse_review_from_log(log_path) if log_path else None
    if findings is not None:
        return findings

    # 3. Agent HTTP fallback.
    if machine_host:
        try:
            findings = parse_review_from_agent(machine_host, review.assignment_id or "")
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "auto_loop: failed to fetch review log from agent %s for %s: %s",
                machine_host, review.assignment_id, exc,
            )
            findings = None
        if findings is not None:
            return findings

    # 4. GitHub message bus — works on ANY machine (no shared DB / local log
    #    needed).  Interactive (claude-pty) reviews post their full body to the
    #    issue under a `coord:review-findings` marker via `--body-file`; recover
    #    it here when the review ran elsewhere.  This is the cross-machine path.
    issue_number = getattr(review, "issue_number", None)
    if repo_github and issue_number and review.assignment_id:
        try:
            from coord.review import fetch_review_findings_from_github  # noqa: PLC0415
            gh_findings = fetch_review_findings_from_github(
                repo_github, int(issue_number), review.assignment_id,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "auto_loop: GitHub findings fetch failed for %s: %s",
                review.assignment_id, exc,
            )
            gh_findings = None
        if gh_findings is not None:
            return gh_findings

    return findings


def process_review_completion(
    review: Assignment,
    board: Board,
    config: Config,
    *,
    log_path: str | None = None,
    machine_host: str | None = None,
    http_client: httpx.Client | None = None,
    terminal_cache: dict | None = None,
    dispatch_fixes: bool = True,
) -> list[LoopAction]:
    """Process a completed review assignment through the auto-loop.

    Parses the reviewer's verdict (local log or agent HTTP fallback when
    *machine_host* is supplied), then either:

    - Returns an ``approved`` action (no side effects) if verdict is
      ``approve``.
    - Dispatches a fix worker (mutates *board*) if verdict is
      ``request-changes`` and the iteration limit has not been reached.
    - Posts a GitHub notice and returns a ``max_iterations`` action when the
      loop has run too many times.

    The caller is responsible for persisting the board after this returns.

    **#1663 —** *dispatch_fixes* is the bookkeeping/dispatch seam.  With
    ``dispatch_fixes=False`` every verdict-determination step still runs (the
    #476 approve-with-nits gate, the #1456 fail-closed rule, the
    ``review_verdict`` capture) and the verdict is still propagated onto the
    parent work row via :func:`propagate_review_verdict` — but a genuine
    ``request-changes`` returns ``verdict_propagated`` instead of entering
    :func:`_dispatch_fix_for_review`.  No metered worker is ever spawned.  This
    is what the daemon drain calls: the drain's responsibility table puts the
    parent-row write squarely in the "bookkeeping, no race, no cost if
    repeated" class and fix dispatch squarely outside it, and before #1663 the
    only way to get the former was to accept the latter.
    """
    if not config.pipeline.auto_loop:
        return [LoopAction(kind="disabled", assignment_id=review.assignment_id)]

    _rg = None
    try:
        _rc = config.repo(review.repo_name)
        _rg = _rc.github if _rc is not None else None
    except Exception:  # noqa: BLE001
        _rg = None
    findings = _load_review_findings(review, log_path, machine_host, repo_github=_rg)
    if findings is None:
        log.debug(
            "auto_loop: no structured REVIEW_VERDICT for %s (log=%r, host=%r) — skipping",
            review.assignment_id, log_path, machine_host,
        )
        return [LoopAction(
            kind="no_findings",
            assignment_id=review.assignment_id,
            detail=(
                f"No structured review output (log={log_path!r}, host={machine_host!r})"
            ),
        )]

    # #253: persist the parsed verdict on the review assignment so the merge
    # gate can refuse to merge work whose review hasn't approved.
    review.review_verdict = findings.verdict

    if findings.verdict == "approve":
        return _advance_pipeline(
            review, board, config,
            kind="approved",
            detail="Review verdict: approve — pipeline advancing",
        )

    # verdict == "request-changes". #476 decision gate: a request-changes
    # verdict that flags NO blocking findings — only non-blocking observations
    # or nits — must NOT trigger another fix+review cycle. Doing so churns an
    # already-correct PR over cosmetic suggestions and burns the session budget
    # (the 2026-06-11 #532 incident: 3 real fix rounds, then a 4th round
    # dispatched over a single cosmetic one-liner the reviewer itself counted
    # as non-blocking). Treat advisory-only request-changes as approve-with-
    # nits: advance the pipeline, surface the nits, and do not dispatch a fix.
    #
    # #1456 (CRITICAL, fails open): this gate used to fire on
    # `parsed_any and not bool(blocking)`, which treated `blocking=None`
    # ("could not determine") identically to `blocking=0` ("reviewer raised
    # nothing blocking") — opposite meanings needing opposite defaults. On
    # #1445 a reviewer's well-formed prose `request-changes` was rewritten to
    # `approve` and marked merge-ready because the *nits* bucket happened to
    # parse as 0 (satisfying `parsed_any`) while *blocking* parsed as None.
    # The evidence standard now lives in `blocking_findings_confirmed_absent`
    # and is fail-closed: no positive evidence ⇒ the reviewer's verdict stands.
    blocking, nonblocking, nits = estimate_review_counts(findings.body)
    if blocking_findings_confirmed_absent(findings.body):
        # log.warning, not info: this is the coordinator overriding a human-
        # readable rejection. It must be conspicuous in the log, and it is
        # additionally recorded on the assignment (below) and posted to GitHub.
        log.warning(
            "auto_loop: OVERRIDING reviewer verdict for review %s — "
            "request-changes with an explicitly empty blocking section "
            "(blocking=%r nonblocking=%r nits=%r); advancing as approve-with-"
            "nits per the #476 gate, not dispatching a fix",
            review.assignment_id, blocking, nonblocking, nits,
        )
        # The merge gate keys off review_verdict; record approve so the nits
        # don't block the merge. The nits remain visible in the review comment
        # already posted to the PR, plus the advisory notice below.
        #
        # #1456: never *silently* rewrite a verdict — preserve the reviewer's
        # own verdict alongside the override plus the counts that justified it,
        # so an audit can always tell a reviewer approval from a coordinator
        # one.
        override_reason = (
            f"#476 approve-with-nits gate: blocking={blocking} "
            f"nonblocking={nonblocking} nits={nits}"
        )
        review.review_verdict_original = findings.verdict
        review.review_verdict_override_reason = override_reason
        review.review_verdict = "approve"
        # #1956: this IS a coordinator override of the reviewer's own
        # verdict — stamp the same provenance a human relaying an override
        # via `coord report-result --verdict-source overridden` would carry,
        # so this automatic path and a manual one read identically at every
        # surface that shows verdict_source, instead of only the manual path
        # being auditable.
        review.verdict_source = "overridden"
        review.verdict_source_reason = override_reason
        _record_verdict_override(
            review, board,
            original_verdict=findings.verdict,
            blocking=blocking, nonblocking=nonblocking, nits=nits,
        )
        _post_advisory_nits_notice(
            review, board, config, nonblocking, nits,
            original_verdict=findings.verdict,
        )
        return _advance_pipeline(
            review, board, config,
            kind="approved_with_nits",
            detail=(
                "Review requested changes but its blocking section was "
                f"explicitly empty (nonblocking={nonblocking}, nits={nits}) — "
                "advancing as approve-with-nits (reviewer verdict preserved in "
                "review_verdict_original); no fix dispatched"
            ),
        )

    if blocking is None:
        # The common case now: the reviewer wrote prose with no recognisable
        # blocking section. Fail closed and say so, so the operator can see
        # *why* a fix round was dispatched rather than the gate firing.
        log.info(
            "auto_loop: could not determine a blocking-finding count for review "
            "%s (blocking=None nonblocking=%r nits=%r) — honouring the "
            "reviewer's request-changes verdict (#1456 fail-closed)",
            review.assignment_id, nonblocking, nits,
        )

    # Genuine blocking findings (or counts unparseable) → dispatch a fix worker.
    if not dispatch_fixes:
        # #1663: the caller (the daemon drain) is explicitly not allowed to
        # spawn a fix worker — that is the #476/#477 blast radius. Do the
        # bookkeeping half anyway so the work row says `request-changes`
        # instead of lying about being `dispatched` with no verdict: a drive,
        # a human, or the #1441 stalled sweep can then act on a legible row.
        # No merge-queue refresh — a request-changes row can never satisfy
        # `has_approved_review`.
        work = propagate_review_verdict(
            review, board, config, refresh_merge_queue=False,
        )
        return [LoopAction(
            kind="verdict_propagated",
            assignment_id=review.assignment_id,
            detail=(
                "Review verdict: request-changes — propagated to work "
                f"{work.assignment_id if work is not None else '<not on board>'}; "
                "fix dispatch withheld (caller is bookkeeping-only)"
            ),
        )]

    return _dispatch_fix_for_review(
        review, findings, board, config,
        http_client=http_client, terminal_cache=terminal_cache,
    )


def propagate_review_verdict(
    review: Assignment,
    board: Board,
    config: Config,
    *,
    refresh_merge_queue: bool = True,
) -> "Assignment | None":
    """Write *review*'s verdict onto the parent **work** row. Bookkeeping only.

    This is the half of :func:`process_review_completion` that is pure
    bookkeeping — no metered worker, no race, no cost if repeated:

    - ``work.review_state = "done"``
    - ``work.review_verdict = review.review_verdict``
    - :func:`coord.state.record_work_review_verdict` (durable single-row write)
    - the merge-queue entry refresh (*refresh_merge_queue*, approve only)

    **#1663** is why this is a separate function.  The exclusion that keeps
    fix-worker dispatch out of the daemon drain (#476/#477) used to sit at
    *function* granularity — the drain refused to enter
    ``process_review_completion`` at all, which silently took the parent-row
    write with it.  Every verdict consumed by the drain instead of by a human's
    ``coord notify`` left its work row at ``review_state='dispatched'`` /
    ``review_verdict=NULL`` forever (the 2026-08-01 overnight batch: five
    issues, four clean approves, 4h02m of wall clock, zero merges).  Splitting
    the bookkeeping out lets the drain call *this* without ever reaching
    ``_dispatch_fix_for_review``.

    Returns the parent work assignment when one was found and written, else
    ``None`` (no ``review_of_assignment_id``, or it isn't on *board*).

    *refresh_merge_queue* is ``True`` on the approve paths — #292 (Defect 2)
    wants the entry created/re-keyed so the TUI shows Merge as ready without a
    manual ``coord merge`` — and ``False`` on the request-changes paths, where
    creating a PENDING queue entry would only add a row that
    ``has_approved_review`` is guaranteed to reject.
    """
    if not review.review_of_assignment_id:
        return None
    work = board.find_by_id(review.review_of_assignment_id)
    if work is None:
        return None

    work.review_state = "done"
    # #1565: propagate the verdict onto the parent work row itself —
    # it used to live only on the review assignment, which left the
    # work row's own review_verdict NULL forever. Persist it as an
    # immediate, scoped single-row write (not deferred to whichever
    # caller happens to save the whole board afterward) so a crash,
    # a skipped persist step, or a stale concurrent save_board()
    # elsewhere can't leave the parent stuck at review_state=
    # 'pending' with a real approval sitting unreferenced on the
    # review row — the #1565 incident (4 redundant metered reviews
    # re-deriving the same approval).
    work.review_verdict = review.review_verdict
    if work.assignment_id and review.review_verdict:
        from coord.state import record_work_review_verdict  # noqa: PLC0415

        record_work_review_verdict(work.assignment_id, review.review_verdict)

    if not refresh_merge_queue:
        return work

    # #292 (Defect 2): proactively enqueue/refresh the merge queue
    # entry so the TUI shows the Merge stage as ready without requiring
    # a manual `coord merge` run first. If the entry was keyed to an
    # earlier work assignment (the original pre-bounce assignment),
    # refresh_entry_assignment updates its assignment_id so
    # has_approved_review can find this approval.
    try:
        from coord import merge_queue as mq  # noqa: PLC0415
        repo_cfg = config.repo(work.repo_name)
        if repo_cfg is not None and work.branch:
            # #934: target `feature/ms-NN` when this issue belongs
            # to a milestone and the repo opted into the git model —
            # the milestone lookup itself is skipped (no `gh` call)
            # when it hasn't, falling back to `default_branch`.
            target_branch = repo_cfg.default_branch
            if getattr(repo_cfg, "develop_branch", None):
                from coord.branch_model import (  # noqa: PLC0415
                    fetch_issue_milestone_number,
                    resolve_base_branch,
                )

                milestone_number = fetch_issue_milestone_number(
                    repo_cfg.github, work.issue_number,
                )
                target_branch = resolve_base_branch(repo_cfg, milestone_number)
            mq.refresh_entry_assignment(
                work,
                repo_github=repo_cfg.github,
                target_branch=target_branch,
            )
    except Exception as exc:  # noqa: BLE001 — best-effort; merge gate still works
        log.warning(
            "auto_loop: refresh_entry_assignment failed for %s: %s",
            work.assignment_id, exc,
        )
    return work


def _advance_pipeline(
    review: Assignment,
    board: Board,
    config: Config,
    *,
    kind: str,
    detail: str,
) -> list[LoopAction]:
    """Mark the reviewed work approved and refresh its merge-queue entry.

    Shared by the plain ``approve`` path and the #476 approve-with-nits path so
    both advance the pipeline identically (the only difference is the action
    ``kind``/``detail`` reported back to the caller).
    """
    propagate_review_verdict(review, board, config)
    return [LoopAction(kind=kind, assignment_id=review.assignment_id, detail=detail)]


def _record_verdict_override(
    review: Assignment,
    board: Board,
    *,
    original_verdict: str,
    blocking: int | None,
    nonblocking: int | None,
    nits: int | None,
) -> None:
    """#1456: write a durable audit row when the coordinator downgrades a
    reviewer's ``request-changes`` to ``approve``.

    The third leg of the audit trail, after the board row (which carries both
    verdicts) and the GitHub notice: a timestamped business-tier event, so an
    operator can answer "which merges rode an overridden verdict?" after the
    fact — including for work whose issue has since been closed.  Best-effort;
    an audit failure must never block the pipeline.
    """
    work = (
        board.find_by_id(review.review_of_assignment_id)
        if review.review_of_assignment_id
        else None
    )
    try:
        from coord.audit import record_audit  # noqa: PLC0415

        record_audit(
            tier="business",
            category="review",
            event_type="review_verdict_overridden",
            actor="coordinator",
            summary=(
                f"Coordinator overrode review verdict {original_verdict} → "
                f"approve for {review.repo_name}#{review.issue_number} "
                f"(#476 advisory-only gate: blocking={blocking}, "
                f"nonblocking={nonblocking}, nits={nits})"
            ),
            repo=review.repo_name,
            issue=review.issue_number,
            assignment_id=review.assignment_id,
            machine=review.machine_name,
            details={
                "original_verdict": original_verdict,
                "effective_verdict": "approve",
                "blocking": blocking,
                "nonblocking": nonblocking,
                "nits": nits,
                "work_assignment_id": work.assignment_id if work else None,
            },
        )
    except Exception as exc:  # noqa: BLE001 — audit is best-effort
        log.warning(
            "auto_loop: failed to record verdict-override audit for %s: %s",
            review.assignment_id, exc,
        )


def _post_advisory_nits_notice(
    review: Assignment,
    board: Board,
    config: Config,
    nonblocking: int | None,
    nits: int | None,
    original_verdict: str = "request-changes",
) -> None:
    """Post a short audit-trail comment when the loop auto-advances past an
    advisory-only request-changes verdict (#476).

    Keeps the auto-advance *visible* — the user was previously burned by silent
    auto-loop behaviour. Best-effort: a gh failure must never block the
    pipeline. The full findings are already on the PR via the review comment;
    this just records the decision not to dispatch another fix round.

    #1456: the comment now names both verdicts — the reviewer's
    (*original_verdict*) and the coordinator's override — so the GitHub thread
    reads as an override, not as an approval the reviewer never gave.
    """
    work = (
        board.find_by_id(review.review_of_assignment_id)
        if review.review_of_assignment_id
        else None
    )
    if work is None:
        return
    repo = config.repo(work.repo_name)
    if repo is None:
        return
    from coord import github_ops  # noqa: PLC0415

    body = (
        f"<!-- coord:event=auto_loop_advisory_advance assignment={work.assignment_id} "
        f"original_verdict={original_verdict} override_verdict=approve -->\n"
        f"## ⚠️ Coordinator overrode the review verdict (no blocking findings)\n\n"
        f"- **Reviewer's verdict:** `{original_verdict}`\n"
        f"- **Coordinator's override:** `approve` (approve-with-nits)\n"
        f"- **Evidence:** the review's blocking section was **explicitly empty** "
        f"(non-blocking={nonblocking}, nits={nits})\n\n"
        f"The latest review of issue **#{work.issue_number}** returned "
        f"`{original_verdict}` while flagging no blocking findings. Per the #476 "
        f"decision gate, the coordinator is **not** dispatching another fix "
        f"round over non-blocking suggestions — the PR advances to the merge "
        f"gate. The reviewer's own verdict is preserved on the assignment "
        f"(`review_verdict_original`); it has not been erased.\n\n"
        f"The reviewer's notes remain in the review comment above. If any nit "
        f"is in fact a must-fix, dispatch a fix manually with `coord assign` "
        f"or bounce it before merging.\n"
    )
    try:
        github_ops.post_issue_comment(repo.github, work.issue_number, body)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "auto_loop: failed to post advisory-advance notice for %s: %s",
            work.assignment_id, exc,
        )


def _fix_model_for_iteration(config: Config, iteration: int) -> str | None:
    """Choose the model alias for a fix worker on a given bounce *iteration*.

    Pure function so the iteration → model mapping is unit-testable.

    Returns ``None`` when ``pipeline.escalate_fix_model`` is disabled — the
    fix dispatch then sets no model and the agent falls back to ``claude -p``'s
    default (today's behaviour).

    When escalation is enabled:
      - iteration 1 → ``config.models.default`` (first fix stays cheap/fast).
      - iteration 2+ → climb one rung up ``config.models.escalation`` per
        iteration, capped at the top of the ladder.

    Example with escalation ``[haiku, sonnet, opus]`` and default ``sonnet``:
    iter 1 → sonnet, iter 2 → opus, iter 3 → opus (capped).
    """
    if not config.pipeline.escalate_fix_model:
        return None

    model = config.models.default
    # iteration 1 stays on the base model; each later iteration escalates one
    # rung (next_model caps at the top of the ladder).
    for _ in range(max(iteration, 1) - 1):
        model = config.models.next_model(model)
    return model


def _merge_blocking_review_findings(
    work: Assignment,
    primary_review: Assignment,
    primary_findings: ReviewFindings,
    board: Board,
    config: Config,
) -> ReviewFindings:
    """Fold every OTHER completed review's blocking findings for *work* into
    *primary_findings* (#3113).

    Two reviews can complete for the same work assignment — a dispatch race
    (the vimcode#804 incident: two reviews 3 seconds apart, both to the same
    machine) or a legacy duplicate that predates the #3113 atomic claim.
    `_dispatch_fix_for_review` used to build the fix briefing from whichever
    review's completion happened to trigger this call, silently discarding
    the other review's findings forever — the loser's real perf-bug finding
    in vimcode#804 survived only in the issue-context digest, which then
    truncated it mid-word (the ``ISSUE_CONTEXT_MAX_CHARS`` gap this same
    issue also closes in ``render_issue_context_entries``).

    Returns *primary_findings* unchanged when there is nothing else to add
    (the overwhelmingly common single-review case — zero extra DB reads
    beyond the board scan below). Otherwise returns a NEW ``ReviewFindings``
    whose ``.body`` concatenates every DISTINCT blocking section, so
    ``_build_fix_briefing`` (unchanged: it just embeds ``findings.body``
    under one ``## Reviewer findings to address`` heading) tells the fix
    worker to address every reviewer's findings, not just one.

    Only ``request-changes`` reviews contribute (an ``approve`` review has
    nothing to fix). Exact-duplicate bodies — a re-capture of the SAME
    review, or two reviews that happen to agree word-for-word — are folded
    into a single entry rather than listed twice.
    """
    repo_github = None
    try:
        repo_cfg = config.repo(work.repo_name)
        repo_github = repo_cfg.github if repo_cfg is not None else None
    except Exception:  # noqa: BLE001
        repo_github = None

    seen: set[str] = set()
    sections: list[str] = []

    def _add(body: str | None) -> None:
        norm = (body or "").strip()
        if not norm or norm in seen:
            return
        seen.add(norm)
        sections.append(norm)

    _add(primary_findings.body)

    # Deliberately not filtered to status=="done" here: a sibling review
    # that hasn't completed yet has no cached findings, so `_load_review_findings`
    # below returns None for it and it's skipped by the `continue` — harmless
    # today. But `_load_review_findings` has a fallback chain (cache miss ->
    # log parse -> GitHub fetch) that could someday read a still-running
    # review's partial output; if it ever does, this loop would need an
    # explicit terminal-status filter to keep from folding in a partial verdict.
    others = [
        a
        for a in list(board.completed) + list(board.active)
        if a.type == "review"
        and a.review_of_assignment_id == work.assignment_id
        and a.assignment_id != primary_review.assignment_id
    ]
    for other in others:
        try:
            other_findings = _load_review_findings(
                other, None, None, repo_github=repo_github
            )
        except Exception as exc:  # noqa: BLE001 — best-effort; never block the fix dispatch
            log.debug(
                "auto_loop: failed to load findings for sibling review %s: %s",
                other.assignment_id, exc,
            )
            other_findings = None
        if other_findings is None or other_findings.verdict != "request-changes":
            continue
        _add(other_findings.body)

    if len(sections) <= 1:
        return primary_findings

    merged_body = sections[0]
    for extra in sections[1:]:
        merged_body += (
            "\n\n---\n\n**Additional blocking findings from another review of "
            "the same patch (#3113 — do not skip these):**\n\n" + extra
        )
    return ReviewFindings(verdict="request-changes", body=merged_body)


def _dispatch_fix_for_review(
    review: Assignment,
    findings,
    board: Board,
    config: Config,
    *,
    http_client: httpx.Client | None = None,
    terminal_cache: dict | None = None,
) -> list[LoopAction]:
    """Find the reviewed work assignment and dispatch a fix worker for it."""
    # Locate the work assignment that was reviewed.
    work: Assignment | None = None
    if review.review_of_assignment_id:
        work = board.find_by_id(review.review_of_assignment_id)

    if work is None:
        log.warning(
            "auto_loop: cannot find work assignment for review %s "
            "(review_of_assignment_id=%r)",
            review.assignment_id,
            review.review_of_assignment_id,
        )
        return [LoopAction(
            kind="no_work_found",
            assignment_id=review.assignment_id,
            detail=(
                f"work assignment {review.review_of_assignment_id!r} not on board"
            ),
        )]

    # #1663 (second gap): write the verdict onto the parent work row on the
    # request-changes path too. `_advance_pipeline` has always done this for
    # approve (#1565) but there was no request-changes twin — the only place
    # this function ever touched the parent was the `_work_is_terminal` early
    # return below, and even that set `review_state` without `review_verdict`.
    # The fix worker is dispatched off the *transition* and doesn't need the
    # row, so nothing broke loudly; what it cost was legibility — a row that
    # still reads `dispatched`/NULL after a real rejection is invisible to
    # `coord drive`, to the TUI's Review stage, and to any state-derived
    # recovery sweep. Done BEFORE the terminal/iteration-cap branches so every
    # outcome of this function leaves an honest parent row, not just the happy
    # one. No merge-queue refresh: request-changes can never satisfy
    # `has_approved_review`.
    propagate_review_verdict(review, board, config, refresh_merge_queue=False)

    # #522: never dispatch a fix for work that is already done on GitHub.
    # A merged PR / closed issue must not re-enter the review→fix loop — this
    # is the root cause of the 2026-06-09 launch flood (#349 ×4, #194).
    if _work_is_terminal(work, config, cache=terminal_cache):
        log.info(
            "auto_loop: NOT dispatching fix for %s — issue #%s is terminal "
            "(merged/closed)",
            work.assignment_id, work.issue_number,
        )
        # The review of merged work is moot; mark it resolved so the board /
        # merge gate stop treating it as needing another round.
        work.review_state = "done"
        return [LoopAction(
            kind="terminal_skip",
            assignment_id=review.assignment_id,
            detail=(
                f"issue #{work.issue_number} already merged/closed — "
                "no fix dispatched"
            ),
        )]

    # Compute the next iteration number and check the limit.
    next_iteration = (work.review_iteration or 0) + 1
    max_iter = config.pipeline.max_review_iterations

    if next_iteration > max_iter:
        log.warning(
            "auto_loop: max_review_iterations (%d) reached for assignment %s "
            "— stopping loop and notifying user",
            max_iter, work.assignment_id,
        )
        _post_max_iterations_notice(work, config)
        return [LoopAction(
            kind="max_iterations",
            assignment_id=review.assignment_id,
            detail=(
                f"max_review_iterations={max_iter} reached for "
                f"work assignment {work.assignment_id}"
            ),
        )]

    # #3113: gather every OTHER completed review of THIS work assignment with
    # its own blocking findings before building the briefing — a dispatch
    # race (or a legacy duplicate) can leave two reviews on the same work
    # row, and the fix worker must see every reviewer's findings, not just
    # whichever review's completion happened to trigger this call.
    merged_findings = _merge_blocking_review_findings(work, review, findings, board, config)

    # Build briefing and dispatch.  The fix worker escalates the model per
    # iteration (when pipeline.escalate_fix_model is enabled); compute it here
    # where the iteration is known and thread it into the dispatch.
    # #603: prepend the per-issue context digest (prior-attempt findings,
    # cross-repo deps) to the TOP of the -p fix briefing.  The interactive fix
    # path prefixes it at its own call site, so the shared _build_fix_briefing
    # stays pure (no double injection).
    from coord.state import issue_context_block  # noqa: PLC0415

    briefing = issue_context_block(work.repo_name, work.issue_number) + _build_fix_briefing(
        work, merged_findings, next_iteration, max_iter
    )
    model = _fix_model_for_iteration(config, next_iteration)
    fix = _dispatch_fix(
        work, briefing, board, config, next_iteration,
        model=model, http_client=http_client,
    )

    if fix is None:
        return [LoopAction(
            kind="no_work_found",
            assignment_id=review.assignment_id,
            detail="fix worker dispatch failed (agent unreachable or no capable machine)",
        )]

    log.info(
        "auto_loop: dispatched fix worker %s for review %s (iteration %d/%d)",
        fix.assignment_id, review.assignment_id, next_iteration, max_iter,
    )
    return [LoopAction(
        kind="fix_dispatched",
        assignment_id=review.assignment_id,
        detail=(
            f"fix worker {fix.assignment_id} dispatched to {fix.machine_name} "
            f"(iteration {next_iteration}/{max_iter})"
        ),
    )]


def _build_fix_briefing(
    work: Assignment,
    findings,
    iteration: int,
    max_iter: int,
) -> str:
    """Assemble the briefing for the fix worker.  Pure function — easy to test.

    #1176: a ``type="test-author"`` source row gets test-authoring-flavored
    instructions instead — "make the acceptance suite pass" is actively
    wrong guidance for an oracle that must stay RED until the real
    implementation lands.

    #2302: a ``type="mock-author"`` source row (Gate A) gets its own
    variant for the same reason one level up the pipeline — the diff is a
    specification (``contract.md`` + rendered mocks), not an
    implementation, so "run tests, make them pass" is equally wrong there.
    """
    if work.type == "test-author":
        return _build_test_author_fix_briefing(work, findings, iteration, max_iter)
    if work.type == "mock-author":
        return _build_mock_author_fix_briefing(work, findings, iteration, max_iter)

    lines: list[str] = [
        f"# Fix assignment (iteration {iteration}/{max_iter}): {work.issue_title}",
        "",
        f"You are fixing review findings for issue #{work.issue_number}.",
        (
            f"Work on branch `{work.branch or '(check your git branches)'}` — "
            "**do not change the branch name**."
        ),
        "",
        "## Reviewer findings to address",
        "",
        findings.body.strip(),
        "",
        "## Instructions",
        "",
        "1. Read the review findings above carefully.",
        "2. Fix **every** issue identified by the reviewer.",
        "3. Stay on the **same branch** — push your fixes to the existing branch.",
        "4. Run the project test suite and ensure all tests pass before pushing.",
        (
            f"5. This is fix iteration {iteration} of {max_iter} allowed. "
            "Address all findings completely so the next review can approve."
        ),
        "",
        "STATUS: reading review findings → implementing fixes → confidence: high",
        "",
    ]
    if work.briefing and work.briefing.strip():
        lines += [
            "## Original work briefing",
            "",
            work.briefing.strip(),
            "",
        ]
    return "\n".join(lines)


def _build_test_author_fix_briefing(
    work: Assignment,
    findings,
    iteration: int,
    max_iter: int,
) -> str:
    """Fix briefing for a ``type="test-author"`` slice (#1176).

    Findings here are almost always test-quality issues (a dead
    ``monkeypatch.setattr``, a fragile assertion that would false-fail a
    correct implementation) — not a request to implement anything. The
    acceptance suite must stay RED until the real implementation lands, so
    "run tests, make them pass" (the generic fix instruction) is actively
    wrong guidance and is deliberately NOT used here. The dispatcher pairs
    this briefing with ``TEST_AUTHOR_SYSTEM_PROMPT`` (independence rules,
    RED verification) — see ``_dispatch_fix``.
    """
    lines: list[str] = [
        f"# Test-author fix (iteration {iteration}/{max_iter}): {work.issue_title}",
        "",
        f"You are the independent acceptance-test author fixing review findings "
        f"against the acceptance suite for issue #{work.issue_number}.",
        (
            f"Work on branch `{work.branch or '(check your git branches)'}` — "
            "**do not change the branch name**."
        ),
        "",
        "## Reviewer findings to address",
        "",
        findings.body.strip(),
        "",
        "## Instructions",
        "",
        (
            "1. Read the reviewer findings above carefully — they are almost "
            "always test-quality issues (fragile assertions, stale/dead mocks, "
            "tests that would false-fail a correct implementation), not a "
            "request to implement anything."
        ),
        (
            "2. Fix **every** issue identified by the reviewer, in the "
            "acceptance suite under `tests/acceptance/` only."
        ),
        "3. Stay on the **same branch** — push your fixes to the existing branch.",
        (
            "4. Your tests MUST remain RED. Do NOT touch any implementation to "
            "make them pass. Run the driver's run command yourself and confirm "
            "the fixed tests still fail cleanly (not error out from a broken "
            "framework hookup)."
        ),
        (
            "5. Update the manifest if you added, removed, or renamed any test "
            "ids — merge with the existing manifest, don't clobber it."
        ),
        (
            f"6. This is fix iteration {iteration} of {max_iter} allowed. "
            "Address all findings completely so the next review can approve."
        ),
        "",
        "STATUS: reading review findings → fixing acceptance suite → confidence: high",
        "",
    ]
    if work.briefing and work.briefing.strip():
        lines += [
            "## Original test-author briefing",
            "",
            work.briefing.strip(),
            "",
        ]
    return "\n".join(lines)


def _build_mock_author_fix_briefing(
    work: Assignment,
    findings,
    iteration: int,
    max_iter: int,
) -> str:
    """Fix briefing for a ``type="mock-author"`` slice (#2302, Gate A).

    The diff here is a SPECIFICATION — ``contract.md`` plus rendered
    ``tests/acceptance/ms-NN/mocks/*`` — not an implementation. There is no
    suite to run, so "run the tests and make them pass" (the generic fix
    instruction) is actively wrong guidance, the same shape #1176 already
    fixed for ``test-author``. Findings on a Gate A review are almost
    always an internal-consistency defect: a mock depicting an end state
    that the contract's own stated rules cannot produce from the narrated
    input sequence — so the contract and the mocks must be reconciled to
    agree with each other in BOTH directions, not just patched locally.
    The dispatcher pairs this briefing with a bare ``type="mock-author"``
    dispatch (see ``_dispatch_fix``) — ``agent.py``'s own
    ``elif spec.type == "mock-author"`` branch supplies
    ``MOCK_AUTHOR_SYSTEM_PROMPT`` and its deny-list on its own, so no
    explicit ``system_prompt``/``deny_commands`` injection is needed here
    the way ``test-author`` (which has no such branch) requires.
    """
    lines: list[str] = [
        f"# Gate A fix (iteration {iteration}/{max_iter}): {work.issue_title}",
        "",
        (
            f"You are the independent mock-author fixing review findings "
            f"against the Gate A contract and mocks for issue "
            f"#{work.issue_number}."
        ),
        (
            f"Work on branch `{work.branch or '(check your git branches)'}` — "
            "**do not change the branch name**."
        ),
        "",
        "## Reviewer findings to address",
        "",
        findings.body.strip(),
        "",
        "## Instructions",
        "",
        (
            "1. Read the reviewer findings above carefully. Your diff is a "
            "SPECIFICATION (`contract.md` + rendered `mocks/*`), not an "
            "implementation — there is no suite to run and nothing to make "
            "pass. Findings on a Gate A review are almost always an "
            "internal-consistency defect: a mock depicting an end state "
            "that the contract's own stated rules cannot produce from the "
            "narrated input sequence."
        ),
        (
            "2. Fix **every** issue identified by the reviewer so the "
            "contract and the mocks agree with each other in BOTH "
            "directions — every rule the contract states must be reachable "
            "from the mocks' narrated sequence, and every state the mocks "
            "depict must be producible by the contract's stated rules."
        ),
        "3. Stay on the **same branch** — push your fixes to the existing branch.",
        (
            "4. Do NOT touch any file outside `tests/acceptance/ms-NN/`. "
            "Touching anything outside that directory is a mandatory "
            "`request-changes` for this assignment type — you are pinning "
            "the milestone's contract, not implementing it."
        ),
        (
            f"5. This is fix iteration {iteration} of {max_iter} allowed. "
            "Address all findings completely so the next review can approve."
        ),
        "",
        (
            "STATUS: reading review findings → reconciling contract and mocks "
            "→ confidence: high"
        ),
        "",
    ]
    if work.briefing and work.briefing.strip():
        lines += [
            "## Original mock-author briefing",
            "",
            work.briefing.strip(),
            "",
        ]
    return "\n".join(lines)


def _dispatch_fix(
    work: Assignment,
    briefing: str,
    board: Board,
    config: Config,
    iteration: int,
    *,
    model: str | None = None,
    http_client: httpx.Client | None = None,
    remote_branch_checker=None,
) -> Assignment | None:
    """POST a fix assignment to the agent server.

    Prefers the same machine as the original worker (the branch is already
    checked out there).  Falls back to any capable machine.

    #1176/#2302: the dispatched type mirrors ``work.type`` for any
    :data:`coord.models.SEALED_PATH_AUTHOR_TYPES` member (``"test-author"``,
    ``"mock-author"``) — those types are the ones ``coord/review.py``
    inverts its sealed-path tamper rule for, so a fix that needs to write
    back into ``tests/acceptance/ms-NN/`` must carry the same type as the
    row it's fixing or every round trips "TAMPER DETECTED" until the
    iteration cap burns out. ``test-author`` additionally needs
    ``TEST_AUTHOR_SYSTEM_PROMPT`` + its deny-list injected explicitly since
    ``agent.py`` has no dispatch-table branch for it; ``mock-author``
    doesn't — ``agent.py``'s own ``elif spec.type == "mock-author"`` branch
    supplies its system prompt and deny-list unconditionally. Every other
    source type still gets the long-standing plain ``"work"`` fix.

    Returns the new Assignment (already added to ``board.active``), or None
    on failure.
    """
    # Pick machine: prefer the original worker's machine first.
    machine = next(
        (m for m in config.machines if m.name == work.machine_name), None
    )
    # #2240: the pause set here is `follow_on_paused_set()`, NOT `paused_set()`
    # — a fix leg is the tail of work that is already running (dispatched
    # after a `request-changes` review verdict on a row that has not gone
    # anywhere new), so a release cordon ("route no NEW work here") must not
    # filter its host out. This is the same fix as `coord/review.py`'s
    # reviewer-selection change; leaving this call on `paused_set()` would
    # reproduce the fleet-wide deadlock for the fix leg instead of the
    # review leg. Explicit pauses and quiet hours still apply.
    from coord.machine_pause import follow_on_paused_set
    paused = follow_on_paused_set(config.machines)
    if (
        machine is None
        or not machine.can_work_on(work.repo_name)
        or machine.repo_path(work.repo_name) is None
        or machine.name in paused
    ):
        # Fallback: any machine capable of working on this repo, minus
        # any the user has paused via `coord pause` (routing-pause).
        candidates = [
            m for m in config.machines
            if m.can_work_on(work.repo_name)
            and m.repo_path(work.repo_name) is not None
            and m.name not in paused
        ]
        if not candidates:
            log.warning(
                "auto_loop: no machine can handle repo %r (paused=%r)",
                work.repo_name, sorted(paused)
            )
            return None
        machine = candidates[0]

    # #586: if we ended up routing to a different machine than the original
    # worker, the branch must exist on the remote so the fix worker can fetch
    # it.  If the worker never pushed, this assignment would crash in 2–3
    # seconds with no commits and no exit code — the classic branch-absent
    # silent failure.  Block early and surface a clear log message instead.
    if machine.name != work.machine_name and work.branch:
        repo_obj = config.repo(work.repo_name)
        if repo_obj is not None:
            _check_remote = remote_branch_checker or github_ops.branch_exists_on_remote
            if not _check_remote(repo_obj.github, work.branch):
                log.error(
                    "auto_loop: branch %r not on remote — cannot dispatch fix "
                    "to different machine %s; original worker %s must push "
                    "the branch to origin first",
                    work.branch, machine.name, work.machine_name,
                )
                return None

    repo_path = machine.repo_path(work.repo_name)
    if repo_path is None:
        return None

    repo = config.repo(work.repo_name)
    if repo is None:
        return None

    # #1176/#2302: preserve the source row's type for a sealed-path-author
    # slice fix (test-author, mock-author) — dispatching `type="work"`
    # sends a plain worker that is forbidden from (and never given the
    # system prompt authorizing) exactly the `tests/acceptance/**` path it
    # needs to fix. Derived from SEALED_PATH_AUTHOR_TYPES rather than a
    # hardcoded string so this stays in sync with that set — see the
    # FIX_DISPATCH_TYPES comment above for the class of bug that guards
    # against. Every other source type (including today's "work") keeps
    # the long-standing "work" fix.
    fix_type = work.type if work.type in SEALED_PATH_AUTHOR_TYPES else "work"

    payload = {
        "repo_name": work.repo_name,
        "repo_path": repo_path,
        "issue_number": work.issue_number,
        "issue_title": f"[fix-{iteration}] {work.issue_title}",
        "briefing": briefing,
        "files_allowed": work.files_allowed,
        "files_forbidden": work.files_forbidden,
        "pull_repos": [],
        "type": fix_type,
        # #255: fix-loop dispatches inherit the repo's configured default
        # branch so the agent branches from origin/<default> rather than
        # any local-only ref.
        "branch": repo.default_branch or "main",
        # #target_branch: tell the agent to check out the ORIGINAL work's
        # branch rather than deriving a new one from the `[fix-N] …`
        # issue title.  Without this the fix worker pushed to a
        # new orphan branch and the existing PR never received the fix
        # commits (quadraui#166 hit this hard).
        "target_branch": work.branch,
    }
    if fix_type == "test-author":
        # `agent.py`'s dispatch table has no `elif spec.type == "test-author"`
        # branch — it relies on the caller supplying `system_prompt`
        # explicitly (mirrors `test_author.dispatch_test_author`). Without
        # this the fix worker gets the generic WORKER_SYSTEM_PROMPT, which
        # is what #1176 is about: it never authorizes editing
        # `tests/acceptance/**` and doesn't carry the independence /
        # stay-RED rules a test-author session needs.
        payload["system_prompt"] = TEST_AUTHOR_SYSTEM_PROMPT
        payload["deny_commands"] = test_author_deny_commands(config, work.repo_name)
    # #2302: no analogous branch for `fix_type == "mock-author"` — unlike
    # test-author, `agent.py`'s `elif spec.type == "mock-author"` branch
    # already supplies `MOCK_AUTHOR_SYSTEM_PROMPT` and unconditionally
    # appends `MOCK_AUTHOR_DENY_COMMANDS` regardless of `spec.deny_commands`,
    # so injecting either here would be redundant with what the agent-side
    # dispatch table already does purely from `type="mock-author"`.
    # Escalated model per bounce iteration (None when pipeline
    # .escalate_fix_model is disabled — preserves today's no-model behaviour).
    # The board record keeps the alias for legibility; the wire payload is
    # resolved through models.versions so claude -p gets an exact id when
    # one is pinned.
    if model is not None:
        payload["model"] = config.models.resolve(model)

    url = f"http://{machine.host}:{AGENT_PORT}/assign"
    client = http_client or httpx
    try:
        resp = client.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        agent_response = resp.json()
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        log.warning("auto_loop: agent request failed for fix dispatch: %s", exc)
        return None

    fix_assignment = Assignment(
        machine_name=machine.name,
        repo_name=work.repo_name,
        issue_number=work.issue_number,
        issue_title=f"[fix-{iteration}] {work.issue_title}",
        files_allowed=list(work.files_allowed),
        files_forbidden=list(work.files_forbidden),
        briefing=briefing,
        assignment_id=agent_response.get("id") or uuid.uuid4().hex[:12],
        status="running",
        branch=work.branch,
        pr_url=work.pr_url,
        dispatched_at=time.time(),
        type=fix_type,
        # Link back so the next review can find the work chain.
        review_of_assignment_id=work.assignment_id,
        # Iteration counter so the loop knows when to stop.
        review_iteration=iteration,
        # Escalated model for this bounce iteration (None preserves the
        # legacy behaviour where the agent picks claude -p's default).
        model=model,
        # #1176: carry over the JIT-slice correlation so the TUI's
        # per-issue Acceptance-Authoring mini-pipeline still recognizes
        # this fix as the same member issue's slice (None for every other
        # type, matching `test_author.dispatch_test_author`).
        for_issue_number=work.for_issue_number,
    )
    board.active.append(fix_assignment)

    try:
        record_dispatched_assignment(
            assignment=fix_assignment,
            repo_github=repo.github,
        )
    except sql.driver_errors() as exc:  # #2784: was sqlite3.OperationalError only
        if not is_lock_contention_error(exc):
            # Not the transient contention this guard exists for (#2538) —
            # a schema mismatch or malformed statement is a real bug and
            # must not be swallowed as an ordinary declined dispatch.
            raise
        # `record_dispatched_assignment` already retries transient
        # "database is locked" contention itself (coord.db.retry_on_locked,
        # via coord._record_dispatched_assignment_local) — this is only
        # reached once that bounded retry budget is exhausted. The fix
        # worker has already been POSTed to the agent above and is really
        # running; only the durable board-DB row failed to write. Treat it
        # the same as any other declined dispatch (no machine, agent
        # unreachable, …) rather than letting a transient lock crash the
        # whole caller (`coord merge`'s CI-fix queue, the review auto-loop,
        # …) — undo the in-memory append so a saved board never carries a
        # row this call never durably recorded, and let the caller's
        # existing "dispatch declined" handling take it from here.
        board.active.remove(fix_assignment)
        log.warning(
            "auto_loop: fix dispatch for %s hit persistent DB contention "
            "recording assignment %s (%s) — treating as not dispatched; "
            "will retry next run",
            work.assignment_id, fix_assignment.assignment_id, exc,
        )
        return None
    return fix_assignment


def _post_max_iterations_notice(work: Assignment, config: Config) -> None:
    """Post a GitHub issue comment when the loop hits the iteration limit."""
    from coord import github_ops  # noqa: PLC0415

    repo = config.repo(work.repo_name)
    if repo is None:
        return

    completed_rounds = work.review_iteration  # rounds completed so far
    max_iter = config.pipeline.max_review_iterations
    body = (
        f"<!-- coord:event=auto_loop_stopped assignment={work.assignment_id} -->\n"
        f"## ⚠️ Auto-loop stopped — max review iterations reached\n\n"
        f"The review → fix cycle for issue **#{work.issue_number}** has "
        f"completed **{completed_rounds}** fix round(s) without receiving an "
        f"approval, which equals the configured maximum of "
        f"**{max_iter}** `pipeline.max_review_iterations`.\n\n"
        f"**Manual intervention required.** Options:\n"
        f"- Review the diff and the reviewer's latest findings, then dispatch "
        f"a fix manually with `coord assign`.\n"
        f"- Run `coord merge --force-merge` to merge the branch as-is "
        f"(if the review findings are acceptable).\n"
        f"- Bump `pipeline.max_review_iterations` in `coordinator.yml` "
        f"(currently `{max_iter}`) to allow more automated fix rounds.\n"
        f"- Adjust the issue scope and open a fresh issue.\n\n"
        f"Details:\n"
        f"- Assignment: `{work.assignment_id}`\n"
        f"- Branch: `{work.branch or '(unknown)'}`\n"
        f"- Completed fix rounds: {completed_rounds}/{max_iter}\n"
    )
    try:
        github_ops.post_issue_comment(repo.github, work.issue_number, body)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "auto_loop: failed to post max-iterations notice for %s: %s",
            work.assignment_id, exc,
        )


# ── notify.py integration ─────────────────────────────────────────────────────

def run_for_fix_transition(
    assignment_id: str,
    config: Config,
    *,
    terminal_cache: dict | None = None,
) -> list[LoopAction]:
    """Entry point called from ``notify.run()`` for each completed fix worker.

    When a bounce-fix worker (``type="work"``, ``review_of_assignment_id``
    IS NOT NULL, title starting with ``"[fix-"``) completes, dispatch a fresh
    review against it so the review → fix → re-review cycle closes
    automatically without manual ``coord pr`` invocations.

    Caps re-review iterations at ``config.pipeline.max_review_iterations``
    using the fix worker's ``review_iteration`` field.  When
    ``fix.review_iteration >= max_review_iterations`` the loop has already
    used all its fix rounds, so no further review is dispatched and an
    ``iteration_cap_hit`` action is returned instead.

    Parameters
    ----------
    assignment_id:
        The completed fix worker's assignment ID.
    config:
        Parsed coordinator config.

    Returns
    -------
    list[LoopAction]
        ``[LoopAction(kind="review_dispatched", ...)]`` on success,
        ``[LoopAction(kind="iteration_cap_hit", ...)]`` when the cap is hit,
        ``[LoopAction(kind="disabled", ...)]`` when auto_loop is off, or
        ``[]`` when the assignment is not found on the board or
        ``dispatch_review`` cannot find a capable machine.
    """
    if not config.pipeline.auto_loop:
        return [LoopAction(kind="disabled", assignment_id=assignment_id)]

    # #749: read_board()/write_board() route through the daemon when
    # board_service is configured — previously this always hit the local DB
    # directly regardless of thin-client status.
    board = read_board()

    fix = board.find_by_id(assignment_id)
    if fix is None:
        log.debug(
            "auto_loop: fix assignment %s not found on board — skipping",
            assignment_id,
        )
        return []

    # #555: an *interactive* fix (provider_name="claude-pty") gets its re-review
    # from the human-attended TUI flow (leg 3 #517), never a headless metered
    # `claude -p` review. Skip the automatic re-review dispatch — mirrors the
    # dispatch_pending_reviews guard for the same interactive-blindness gap.
    if fix.provider_name == "claude-pty":
        log.info(
            "auto_loop: NOT dispatching headless re-review for %s — interactive "
            "fix (provider_name=claude-pty); re-review is human-attended",
            assignment_id,
        )
        return [LoopAction(
            kind="interactive_skip",
            assignment_id=assignment_id,
            detail=(
                "interactive fix — re-review is human-attended; "
                "no headless review dispatched"
            ),
        )]

    # #522: a fix worker that finished against already-merged/closed work must
    # not trigger another review. Guards the second flood vector (re-review
    # dispatch) the same way the fix-dispatch path is guarded above.
    if _work_is_terminal(fix, config, cache=terminal_cache):
        log.info(
            "auto_loop: NOT dispatching re-review for %s — issue #%s is "
            "terminal (merged/closed)",
            assignment_id, fix.issue_number,
        )
        fix.review_state = "done"
        write_board(board)
        return [LoopAction(
            kind="terminal_skip",
            assignment_id=assignment_id,
            detail=(
                f"issue #{fix.issue_number} already merged/closed — "
                "no re-review dispatched"
            ),
        )]

    max_iter = config.pipeline.max_review_iterations
    if fix.review_iteration >= max_iter:
        log.warning(
            "auto_loop: fix %s has review_iteration=%d >= max_review_iterations=%d "
            "— not dispatching another review",
            assignment_id, fix.review_iteration, max_iter,
        )
        # Surface the cap-hit as a persisted blocker: post a GitHub comment so
        # the operator sees it outside the TUI, mark the board entry with a
        # distinct review_state so `coord status` shows an explicit blocker line,
        # and save the board so the state survives a coordinator restart.
        _post_max_iterations_notice(fix, config)
        fix.review_state = "cap_hit"
        write_board(board)
        return [LoopAction(
            kind="iteration_cap_hit",
            assignment_id=assignment_id,
            detail=(
                f"fix iteration {fix.review_iteration} >= "
                f"max_review_iterations {max_iter}; "
                "not dispatching another review"
            ),
        )]

    # #1612: this is the *other* auto-dispatch path — `dispatch_pending_reviews`
    # (reached from reconcile()/`coord notify`) holds review dispatch until the
    # work carries a passed/skipped test verdict whenever
    # `pipeline.default_gates` orders Test before Review, but this fix-transition
    # path called `dispatch_review` directly, bypassing that gate entirely. A
    # fix round's review was therefore dispatched while its smoke test was
    # still `running` (or before it had even started), burning a metered
    # review on code of unknown quality. Mirror the gate here — but holding
    # cannot just `return []`: this function fires once, on the fix worker's
    # completion transition (`notify.py`'s `fix_completions`), and is never
    # re-entered, so a bare return would strand the row with no review ever
    # dispatched. Instead, set `review_state="pending"` so the row becomes
    # eligible for `dispatch_pending_reviews`, which runs every pass and
    # already carries the correct gate — this is a deferral to that path, not
    # a drop.
    if config.pipeline.test_precedes_review() and fix.test_state not in (
        "passed", "skipped",
    ):
        log.info(
            "auto_loop: NOT dispatching re-review for %s yet — test gate is "
            "active and fix.test_state=%r is not a passed/skipped verdict; "
            "deferring to dispatch_pending_reviews",
            assignment_id, fix.test_state,
        )
        fix.review_state = "pending"
        write_board(board)
        return [LoopAction(
            kind="test_gate_held",
            assignment_id=assignment_id,
            detail=(
                f"test gate active — fix.test_state={fix.test_state!r} is not "
                "passed/skipped; review_state set to 'pending' for "
                "dispatch_pending_reviews to pick up once the verdict lands"
            ),
        )]

    review = dispatch_review(fix, board, config)

    if review is None:
        log.warning(
            "auto_loop: dispatch_review returned None for fix %s "
            "(no capable machine or dedup check rejected the dispatch)",
            assignment_id,
        )
        return []

    fix.review_state = "dispatched"
    write_board(board)

    log.info(
        "auto_loop: dispatched re-review %s for fix worker %s (iteration %d/%d)",
        review.assignment_id, assignment_id, fix.review_iteration, max_iter,
    )
    return [LoopAction(
        kind="review_dispatched",
        assignment_id=assignment_id,
        detail=(
            f"re-review {review.assignment_id} dispatched to "
            f"{review.machine_name} (fix iteration {fix.review_iteration}/"
            f"{max_iter})"
        ),
    )]


def run_for_review_transition(
    assignment_id: str,
    record: dict,
    entry: dict,
    config: Config,
    *,
    terminal_cache: dict | None = None,
) -> list[LoopAction]:
    """Entry point called from ``notify.run()`` for each completed review.

    Loads the board from the database, processes the review completion, saves
    the board if a fix worker was dispatched, and returns the list of actions
    taken.

    **May dispatch a metered fix worker** — this is the ``coord notify`` /
    drive-nudge entry point.  Callers that must not spawn one (the daemon
    drain) want :func:`propagate_review_verdict_for_transition` instead;
    ``tests/test_notify_drain.py::test_does_not_run_the_review_auto_loop``
    patches *this* name and asserts it is never called from a drain pass.

    Parameters
    ----------
    assignment_id:
        The completed review's assignment ID.
    record:
        The dispatched-assignment record dict (from ``load_dispatched()``).
    entry:
        The agent /status entry for this assignment (contains ``log_path``).
    config:
        Parsed coordinator config.
    """
    return _run_for_review_transition(
        assignment_id, record, entry, config,
        terminal_cache=terminal_cache, dispatch_fixes=True,
    )


def propagate_review_verdict_for_transition(
    assignment_id: str,
    record: dict,
    entry: dict,
    config: Config,
) -> list[LoopAction]:
    """#1663: the drain-safe half of :func:`run_for_review_transition`.

    Same board load / verdict resolution / parent-row write / persist, with
    :func:`_dispatch_fix_for_review` unreachable — a genuine ``request-changes``
    comes back as ``verdict_propagated`` rather than ``fix_dispatched``.

    A deliberately *separate public name*, not a keyword argument on
    ``run_for_review_transition``: the #476/#477 guard tests patch that symbol
    and assert it is never called from ``notify.run_drain``, and a flag would
    have made "the drain called the auto-loop entry point" indistinguishable
    from "the drain dispatched a fix worker" at the seam the guard watches.

    No ``terminal_cache``: nothing on this path calls ``_work_is_terminal``, so
    a drain pass makes no ``gh`` round-trips on behalf of this function.
    """
    return _run_for_review_transition(
        assignment_id, record, entry, config,
        terminal_cache=None, dispatch_fixes=False,
    )


def _run_for_review_transition(
    assignment_id: str,
    record: dict,
    entry: dict,
    config: Config,
    *,
    terminal_cache: dict | None = None,
    dispatch_fixes: bool = True,
) -> list[LoopAction]:
    """Shared body of the two review-transition entry points above."""
    if not config.pipeline.auto_loop:
        return [LoopAction(kind="disabled", assignment_id=assignment_id)]

    if record.get("type") != "review":
        return []

    # #749: read_board() routes through the daemon when board_service is
    # configured, instead of always hitting the local DB directly.
    board = read_board()

    review = board.find_by_id(assignment_id)
    if review is None:
        # Review not on board yet — it was recorded by notify but the board
        # might not be persisted.  Try looking it up by review_of_assignment_id
        # from the record dict and create a minimal proxy.
        log.debug(
            "auto_loop: review %s not found on board — cannot process", assignment_id
        )
        return []

    log_path: str | None = entry.get("log_path")
    # #fix-cli: include the agent's host so the auto-loop can fall back
    # to HTTP /logs/<id> when the local log isn't on this filesystem
    # (the gap that left quadraui#166 without a fix dispatch).
    machine_host: str | None = None
    machine_name = record.get("machine_name")
    if machine_name:
        machine = next((m for m in config.machines if m.name == machine_name), None)
        if machine is not None and machine.host:
            machine_host = machine.host
    actions = process_review_completion(
        review,
        board,
        config,
        log_path=log_path,
        machine_host=machine_host,
        terminal_cache=terminal_cache,
        dispatch_fixes=dispatch_fixes,
    )

    # See PERSIST_ACTION_KINDS for why each kind implies a board write.
    if any(a.kind in PERSIST_ACTION_KINDS for a in actions):
        write_board(board)

    return actions
