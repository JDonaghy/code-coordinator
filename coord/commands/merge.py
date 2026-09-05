"""`coord merge` and the rest of the merge-queue surface: `verify-merge`,
`reconcile-merges`, `bounce`, `post-pending-reviews`. Extracted from
coord/cli.py (#747)."""

from __future__ import annotations

import sys
from pathlib import Path

import click


from coord import sql
from coord.commands._common import _CONFIG_OPTION, _load_config
from coord.db import LockContentionExhaustedError, is_lock_contention_error, retry_on_locked
from coord.models import (
    WORK_LIKE_TYPES,
    effective_issue_number,
    trust_issue_closed_for,
)


def _machine_for_assignment(board, assignment_id: str | None) -> str | None:
    """Return the machine name that ran *assignment_id*, or None.

    Used by ``coord merge`` (#241) to prefer dispatching a conflict-fix to
    the original worker's machine — that machine already has the repo
    checked out, the branch present, and the test deps installed.
    """
    if assignment_id is None or board is None:
        return None
    target = board.find_by_id(assignment_id)
    return target.machine_name if target is not None else None


def _apply_revalidation(items, board, config, gh_ops, *, dry_run: bool, skip_review: bool = False):
    """#1769: the ``--revalidate`` arm for the merge lane.

    Finds the entries in *items* blocked **solely** on a stale-but-``passed``
    smoke verdict (:func:`coord.merge_queue.revalidation_candidates` is the
    whole eligibility policy — review/CI/conflict/missing-verdict blocks are
    never touched), re-tests them against the current base, and lets
    :func:`coord.merge_queue.process` re-evaluate afterwards.

    #3107: *skip_review* is forwarded to
    :func:`coord.merge_queue.revalidation_candidates` unchanged, so an entry
    this same run already waived the review gate for (``--skip-review``) is
    evaluated as blocked *solely* on staleness rather than on the review
    finding the waiver already disposed of. Without this, ``--skip-review
    --revalidate`` together on one entry printed the waiver and then refused
    to revalidate anyway — the predicate below never saw the waiver.

    Batch (#1715): candidates are grouped by ``(repo, target_branch)`` and each
    group is composed onto its current base and validated by ONE suite run —
    which is exactly what the operator did by hand three times in the
    2026-08-03 session that motivated this. N approved branches on one base
    cost one suite run, not N.

    A group whose composite fails merges **nothing** on that result and marks
    nothing failed; :func:`coord.revalidate.revalidate_group` then re-tests each
    branch alone so the culprit is named and the innocent branches still merge.
    A failing re-test can never launder a merge in either pass.

    Under ``--dry-run`` this only *names* the batches and their members — no
    worktree, no suite, no verdict write.

    Returns the board to hand to ``process()``: a freshly-loaded one when any
    verdict was actually recorded (the in-memory board predates that write and
    would still show the entries as stale), otherwise *board* unchanged.
    """
    from coord import merge_queue as _mq  # noqa: PLC0415
    from coord import revalidate as _rv  # noqa: PLC0415

    candidates = _mq.revalidation_candidates(
        items, board, config, gh_ops, skip_review=skip_review,
    )
    if not candidates:
        click.echo(
            "  --revalidate: no entry is blocked solely on a stale test "
            "verdict — nothing to revalidate (review/CI/conflict/missing-"
            "verdict blocks are never revalidated)"
        )
        return board

    if dry_run:
        for line in _rv.describe_batches(candidates):
            click.echo(line)
        return board

    for line in _rv.describe_candidates(candidates):
        click.echo(line)

    recorded_any = False
    total_runs = 0
    conflicted: list = []
    groups = _rv.group_candidates(candidates)
    for (repo_name, target_branch), group in groups:
        click.echo(
            f"  --revalidate: {repo_name} → {target_branch}: "
            f"{len(group)} entry(ies)"
        )
        batch = _rv.revalidate_group(group, config, echo=click.echo)
        total_runs += batch.suite_runs
        recorded_any = recorded_any or bool(batch.recorded)
        conflicted.extend(batch.conflicted)
        if not batch.composite.ok:
            # The composite's own failure report — stderr, as before.
            for line in _rv.format_failure(batch.composite):
                click.echo(line, err=True)
        # What actually came of it (what merged anyway, who the culprit was)
        # is ordinary output, even when the composite above was red.
        for line in _rv.format_batch(batch):
            click.echo(line)

    if len(candidates) > 1:
        click.echo(
            f"  --revalidate: {total_runs} suite run(s) for "
            f"{len(candidates)} entry(ies)"
        )

    # #2231: the composed run just PROVED these branches don't merge. That is
    # not a staleness problem and re-testing can never resolve it — hand it to
    # the mechanism built for it instead of formatting a diagnosis nobody acts
    # on.
    if conflicted:
        _dispatch_revalidation_conflicts(conflicted, config, dry_run=dry_run)

    if not recorded_any:
        return board

    from coord.models import Board as _Board  # noqa: PLC0415
    from coord.state import load_board as _load_board  # noqa: PLC0415

    refreshed = _load_board()
    return refreshed if refreshed is not None else _Board(active=[], completed=[])


def _apply_ci_revalidation(
    items, board, config, ci_store, gh_ops, *, dry_run: bool,
    poll_sleep=None, poll_clock=None,
) -> set[str]:
    """#1851: the ``--revalidate`` arm for CI staleness — the CI analogue of
    :func:`_apply_revalidation`'s stale-local-verdict arm, resolving a
    different staleness signal (see ``coord/ci_store.py``'s module docstring
    for why the two are distinct: GitHub re-runs ``pull_request`` checks on
    head ``synchronize``, never on base movement, so a green check can
    outlive the base it actually validated even when the local Test verdict
    is perfectly fresh).

    Unlike :func:`_apply_revalidation` there is nothing to compose and no
    local suite to run — the remedy is :meth:`coord.ci_store.CiStore.
    rerun_for_pr`, a ``gh run rerun`` that costs CI minutes on GitHub's own
    runners, not a routed Test-stage agent. Strictly cheaper than what
    :func:`_apply_revalidation` does for the same reason CI should always be
    preferred when both would establish the same fact (#1851's "Cost
    framing").

    Opt-in behind ``--revalidate`` exactly like :func:`_apply_revalidation`
    — never called from auto-drain (see ``docs/DRIVE_QUEUE.md``). Under
    ``--dry-run`` this only *names* what it would trigger; no ``gh`` mutation
    runs.

    #1925: triggering a rerun and then handing the entry straight to
    ``merge_queue.process()`` used to fail-close on the rerun's OWN
    registration gap — ``gh pr checks`` errors for the few seconds before
    GitHub has created any check-run record, which reads as the #1525
    synthetic ``unknown`` conclusion and blocks exactly like a genuinely
    broken CI would. :func:`coord.ci_store.wait_for_ci_settle` closes that
    gap with a bounded poll right here, so the caller's subsequent
    ``process()`` call sees a real, resolved result — pass or fail — for the
    common case.

    Returns the ``assignment_id``s whose wait ran out the budget while STILL
    only seeing the registration-gap symptom (never a real check) —
    :func:`wait_for_ci_settle`'s ``registering=True`` case. The caller must
    exclude these from the ``process()`` call that follows: evaluating the
    gate against that exact symptom is the bug this fixes, so it must not
    reappear at the timeout edge just because the wait gave up. The entry
    stays ``PENDING`` with an explanatory ``entry.error`` instead — legible
    as "come back shortly", never as "checks failed" (#1925's acceptance:
    an ``unknown`` this command caused must not be presented identically to
    an ``unknown`` from genuinely broken CI).
    """
    from coord import merge_queue as _mq  # noqa: PLC0415
    from coord.ci_store import wait_for_ci_settle  # noqa: PLC0415

    deferred: set[str] = set()
    if ci_store is None or not ci_store.is_available:
        return deferred
    candidates = _mq.ci_revalidation_candidates(items, board, config, ci_store, gh_ops)
    if not candidates:
        click.echo(
            "  --revalidate: no entry is blocked solely on stale CI checks "
            "— nothing to re-run"
        )
        return deferred
    for entry in candidates:
        label = f"{entry.repo_name} #{entry.issue_number} ({entry.branch})"
        if dry_run:
            click.echo(
                f"  --revalidate: would re-run CI for {label} "
                f"(PR #{entry.pr_number}) — checks predate the current base"
            )
            continue
        ok = ci_store.rerun_for_pr(entry.repo_github, entry.pr_number)
        if not ok:
            click.echo(
                f"  --revalidate: could not trigger a CI re-run for {label} "
                f"(PR #{entry.pr_number}) — see gh output above",
                err=True,
            )
            continue
        click.echo(
            f"  --revalidate: triggered a CI re-run for {label} "
            f"(PR #{entry.pr_number})"
        )
        result = wait_for_ci_settle(
            ci_store, entry.repo_github, entry.pr_number,
            echo=click.echo, sleep=poll_sleep, clock=poll_clock,
        )
        if result.settled:
            click.echo(
                f"  --revalidate: CI re-run for {label} settled after "
                f"{result.waited_seconds:.0f}s — the merge gate will "
                "evaluate the fresh result"
            )
        elif result.registering:
            entry.error = (
                f"{label}: the CI re-run --revalidate just triggered "
                f"(PR #{entry.pr_number}) has not registered on GitHub yet "
                f"after {result.waited_seconds:.0f}s — this is the re-run "
                "THIS command started, not a CI failure; re-run `coord "
                "merge --revalidate` (or plain `coord merge`) shortly (#1925)"
            )
            deferred.add(entry.assignment_id)
            click.echo(f"  --revalidate: {entry.error}")
        else:
            click.echo(
                f"  --revalidate: CI re-run for {label} is still running "
                f"after {result.waited_seconds:.0f}s — leaving it to the "
                "merge gate this pass (will report as CI still running)"
            )
    return deferred


def _reload_board_after_wait(board, *, dry_run: bool):
    """#2143: force a fresh board read after ``_apply_ci_revalidation``'s
    ``wait_for_ci_settle`` poll — which, like ``_apply_revalidation``'s
    composite/solo suite runs, can hold the caller for minutes.

    ``_apply_revalidation`` already refreshes the board it hands back, but
    only when *it* recorded a new verdict; it has no way to know a CI-settle
    wait ran afterwards. Real state can change on GitHub during that wait —
    most dangerously a review approval landing, or a concurrent merge driver
    (the drive-queue timer, another operator) merging the exact branch this
    run is about to act on — and `merge_queue.process()` must not evaluate
    review/smoke gates against a *board* snapshot that predates any of it
    (the 2026-08-12 incident: a review approved 89s into the wait was still
    reported as unapproved because the pre-wait board was never re-read).

    Unconditional (not "only when something looks stale"): the risk here is
    a stale *read*, and there's no cheap way to tell "nothing changed" from
    "something changed but we didn't notice" without just re-reading. One
    extra ``load_board()`` is a rounding error next to the suite run(s) or
    the CI-settle poll that just happened. Under ``--dry-run`` nothing was
    triggered (no wait actually ran), so the board is left untouched.
    """
    if dry_run:
        return board
    from coord.models import Board as _Board  # noqa: PLC0415
    from coord.state import load_board as _load_board  # noqa: PLC0415

    refreshed = _load_board()
    return refreshed if refreshed is not None else _Board(active=[], completed=[])


def _dispatch_revalidation_conflicts(conflicted, config, *, dry_run: bool) -> None:
    """#2231: turn ``--revalidate``'s conflict verdict into a #241 dispatch.

    *conflicted* is :attr:`coord.revalidate.BatchRevalidationResult.conflicted`
    — the candidates whose revalidation run died composing the branch onto its
    current base. ``--revalidate`` already did the expensive, authoritative
    part: it fetched, built a worktree at the live base, ran ``git merge`` and
    watched it fail. That is strictly better evidence than the failed ``gh pr
    merge`` that normally arms this path, and it was being spent on an
    operator-facing paragraph and then discarded (quadraui #306/#309 sat 11h
    each behind a "test verdict stale" gate whose real blocker was a content
    conflict; a human eventually typed ``coord fix --force`` by hand, which is
    #241's own job description).

    Two things happen per entry, in this order:

    1. The entry is moved to ``CONFLICT`` with an ``entry.error`` that names
       the conflict (:func:`coord.revalidate.compose_conflict_error`). This is
       the reporting half: the gate reason stops reading (only) "stale
       verdict", so ``coord drive``'s #1738 arm no longer sees a shape it
       would answer with a re-test, and ``merge_queue.process()`` — which acts
       only on ``PENDING`` — leaves the entry alone this pass instead of
       re-deriving the same smoke block. The caller persists the mutation with
       the rest of the queue; nothing is written here.
    2. The entry is handed to :func:`_dispatch_conflict_fixes` as a synthetic
       ``conflict`` event, which is the SAME call the whole-queue and
       ``--only`` paths make after a live merge attempt. Every guard it owns
       still applies unchanged: :func:`coord.merge_queue.classify_conflict`
       must read the error as ``rebaseable``, the #241/#784 retry cap still
       flips a second failure to ``HUMAN_REQUIRED``, and
       ``dispatch_conflict_fix``'s own in-flight check still refuses a
       duplicate. Deliberately routed through that function rather than
       calling ``dispatch_conflict_fix`` directly — a second dispatch site
       with its own copy of those guards is how they drift apart.

    Under ``--dry-run`` nothing is mutated and nothing is dispatched (the
    revalidation itself never ran either — see :func:`_apply_revalidation`).
    """
    if dry_run or not conflicted:
        return

    from coord.merge_queue import CONFLICT, MergeEvent  # noqa: PLC0415
    from coord.revalidate import compose_conflict_error  # noqa: PLC0415

    events = []
    for candidate, _result in conflicted:
        entry = candidate.entry
        entry.error = compose_conflict_error(entry)
        entry.state = CONFLICT
        click.echo(
            f"  --revalidate: {entry.repo_name} #{entry.issue_number}: "
            "the blocker is a CONFLICT, not a stale verdict — the branch does "
            "not compose onto its base. Routing to the conflict-fix path "
            "(#241) instead of re-testing (#2231)."
        )
        events.append(MergeEvent(entry, "conflict", entry.error))
    _dispatch_conflict_fixes(events, config, dry_run=False)


def _sweep_sibling_conflicts(events, items, config, gh_ops, *, dry_run: bool) -> list:
    """#2246: after a merge lands, ask GitHub which sibling PRs it just broke.

    A merge into ``target_branch`` can invalidate every other open PR against
    that same branch. GitHub computes exactly which ones, asynchronously and
    for free, and until #2246 nothing asked at the moment it matters —
    ``mergeable`` was consulted only at *merge* time, i.e. one drive attempt
    too late. On 2026-08-14 that cost four terminal ``blocked`` entries across
    two repos: quadraui #306/#309 presented as "smoke gate — test verdict
    stale", claude-coordinator #2234 as "checks_failed … (unknown)" with
    ``coord fix`` asserting "CI is RED" for a PR whose CI had never run at all
    (GitHub builds ``pull_request`` workflows from ``refs/pull/N/merge``, which
    cannot exist while the PR conflicts, so *zero* check-suites were queued and
    the absence was read as failure — #2244). No surface said *conflict*.

    Two things happen for each sibling GitHub now reports ``CONFLICTING``,
    both inside :func:`coord.merge_queue.sweep_sibling_conflicts` and the
    dispatch call below:

    1. The entry is parked at ``CONFLICT`` with an error naming the merge that
       broke it — so the next surface to read it says the true thing instead of
       re-deriving whichever gate happened to be failing for an unrelated
       reason, and ``coord drive``'s #1738 re-test arm stops answering a
       conflict with a suite run.
    2. It is handed to :func:`_dispatch_conflict_fixes` as an ordinary
       ``conflict`` event — the SAME call the post-``process()`` path makes —
       so #241's conflict-fix worker is dispatched with every guard intact:
       :func:`coord.merge_queue.classify_conflict` must still read the error as
       ``rebaseable``, the #241/#784 retry cap still flips a second failure to
       ``HUMAN_REQUIRED``, and ``dispatch_conflict_fix``'s in-flight check
       still refuses a duplicate. Routed through that function rather than
       calling ``dispatch_conflict_fix`` directly for the reason #2231 gives:
       a second dispatch site with its own copy of those guards is how they
       drift apart. That dispatch call can advance ``ev.entry.state`` a
       second time (e.g. to ``HUMAN_REQUIRED`` on a retry-cap hit) — this
       function persists that too, scoped to just the swept entries, so a
       sibling this caller doesn't hold in ``items`` (the ``--only`` path)
       doesn't have its escalation silently dropped on the floor while the
       audit log claims it happened (#2246 review).

    Skipped entirely under ``--dry-run`` — ``process()`` emits ``merged``
    events there too, but nothing actually landed, so no sibling's mergeability
    can have changed and marking one would be a lie written to the queue.

    Returns the conflict events so the caller can fold them into its summary;
    never raises (the sweep itself fails open — see its docstring). The merge
    that triggered this has already succeeded and must not be undone or
    obscured by a failed read afterwards.
    """
    if dry_run:
        return []
    from coord import merge_queue as _mq  # noqa: PLC0415

    try:
        sweep_events = _mq.sweep_sibling_conflicts(events, items, gh_ops)
    except Exception as e:  # noqa: BLE001 — never let the sweep undo a merge
        click.echo(
            f"  sibling-conflict sweep failed (merge itself is unaffected): {e!r}",
            err=True,
        )
        return []
    for ev in sweep_events:
        e = ev.entry
        click.echo(
            f"  {e.repo_name} #{e.issue_number} ({e.branch}): {ev.kind} — "
            f"{ev.message}"
        )
    _dispatch_conflict_fixes(sweep_events, config, dry_run=False)
    # #2246 review: `_dispatch_conflict_fixes` can advance a swept sibling's
    # state a second time in place — e.g. `entry.state = HUMAN_REQUIRED` when
    # `has_prior_conflict_fix` finds this sibling already burned a
    # conflict-fix attempt against an earlier conflict. `mq.sweep_sibling_
    # conflicts` above already persisted its own CONFLICT write, but for a
    # sibling this caller doesn't hold in `items` (the `--only` path, the
    # exact one #2246 targets), nothing after that dispatch call ever writes
    # the newer HUMAN_REQUIRED back — `merge()`'s own final save keys only on
    # `only_entry`. Without this, the audit log records "manual resolution
    # required" while the board/queue still say CONFLICT: the split-brain
    # class this repo's CLAUDE.md calls out (#1832/#2085). Persist here,
    # scoped to just the entries this sweep touched — never the full queue —
    # so a fresh conflict elsewhere isn't clobbered.
    if sweep_events:
        try:
            fresh = _mq.load_queue()
            by_id = {ev.entry.assignment_id: ev.entry for ev in sweep_events}
            _mq.save_queue([by_id.get(x.assignment_id, x) for x in fresh])
        except Exception as e:  # noqa: BLE001 — fail open, per the sweep's own contract
            click.echo(
                "  sibling-conflict fix-dispatch state failed to persist "
                f"(merge itself is unaffected): {e!r}",
                err=True,
            )
    return sweep_events


def _dispatch_conflict_fixes(events, config, *, dry_run: bool) -> None:
    """#241: classify any conflict events and dispatch a conflict-fix worker
    for the eligible ones.  Mutates each conflict event's ``ev.entry.state``
    in place (to ``HUMAN_REQUIRED`` on a retry-cap hit or a non-rebaseable
    classification) — ``ev.entry`` is the same object the caller's own
    items list holds, so its subsequent save-queue step picks the mutation
    up naturally, without a separate write here.

    Shared by the whole-queue path and the ``--only`` surgical path
    (#1474 review finding): the whole-queue path always ran this block, but
    ``--only`` returned before ever reaching it — so a ``--only``-only
    caller (``coord drive``, the TUI's ``--merge-of``) could park an entry
    at ``CONFLICT`` with no conflict-fix ever dispatched and nothing
    watching it, permanently: ``merge_queue.process()`` only ever acts on
    ``PENDING`` entries, so a bare ``CONFLICT`` row is never reprocessed and
    never gets a second chance at this classify-and-dispatch step.
    """
    conflict_events = [ev for ev in events if ev.kind == "conflict"]
    if not conflict_events or dry_run:
        return

    from coord.audit import record_audit  # noqa: PLC0415
    from coord.conflict_fix import (  # noqa: PLC0415
        dispatch_conflict_fix,
        has_prior_conflict_fix,
    )
    from coord.merge_queue import (  # noqa: PLC0415
        HUMAN_REQUIRED,
        classify_conflict,
        is_rebase_refusal,
    )
    from coord.state import load_board, save_board  # noqa: PLC0415

    fix_board = load_board()
    if fix_board is None:
        return
    dispatched_any = False
    for ev in conflict_events:
        kind = classify_conflict(ev.entry.error)
        if kind == "rebaseable":
            # Retry cap (#241/#784): if a conflict-fix already ran and
            # failed for this entry in this session, don't loop — mark
            # HUMAN_REQUIRED so the user takes over.  A successful
            # prior fix does not trigger this guard (#784).
            if has_prior_conflict_fix(
                fix_board, ev.entry.assignment_id, current_error=ev.entry.error,
            ):
                ev.entry.state = HUMAN_REQUIRED
                click.echo(
                    f"  {ev.entry.repo_name} #{ev.entry.issue_number}: "
                    "conflict-fix retry cap hit — manual resolution required"
                )
                # #1467: a rebase-refusal ("This branch can't be rebased")
                # is fixed by linearising the branch — spell out the exact
                # recovery so it isn't left to archaeology, and give the
                # durable `repo#issue` key (#1477) `--only` accepts.
                if is_rebase_refusal(ev.entry.error):
                    _key = f"{ev.entry.repo_name}#{ev.entry.issue_number}"
                    click.echo(
                        f"    recovery: git checkout {ev.entry.branch} && "
                        "git rebase origin/"
                        f"{ev.entry.target_branch} && "
                        "git push --force-with-lease, then "
                        f"`coord merge --only {_key} "
                        '--override-human-required "rebase refusal '
                        'resolved manually"`'
                    )
                # #1038: the coordinator's own retry-cap logic made this
                # call, not the human running `coord merge` — operational
                # tier, same as the other automatic conflict-classification
                # outcomes below.
                record_audit(
                    tier="operational",
                    category="merge",
                    event_type="conflict_human_required",
                    actor="daemon",
                    summary=f"conflict-fix retry cap hit: "
                    f"{ev.entry.repo_name}#{ev.entry.issue_number} — "
                    "manual resolution required",
                    repo=ev.entry.repo_name,
                    issue=ev.entry.issue_number,
                    assignment_id=ev.entry.assignment_id,
                    details={"reason": "retry_cap"},
                )
                continue
            fix = dispatch_conflict_fix(
                ev.entry,
                fix_board,
                config,
                prefer_machine=_machine_for_assignment(
                    fix_board, ev.entry.assignment_id,
                ),
            )
            if fix is not None:
                click.echo(
                    f"  {ev.entry.repo_name} #{ev.entry.issue_number}: "
                    f"conflict-fix dispatched to {fix.machine_name}"
                )
                dispatched_any = True
            else:
                click.echo(
                    f"  {ev.entry.repo_name} #{ev.entry.issue_number}: "
                    "conflict-fix not dispatched (no machine / already in flight)"
                )
        elif kind == "human":
            ev.entry.state = HUMAN_REQUIRED
            click.echo(
                f"  {ev.entry.repo_name} #{ev.entry.issue_number}: "
                "permission/protection error — manual resolution required"
            )
            record_audit(
                tier="operational",
                category="merge",
                event_type="conflict_human_required",
                actor="daemon",
                summary=f"conflict classified non-rebaseable: "
                f"{ev.entry.repo_name}#{ev.entry.issue_number} — "
                "manual resolution required",
                repo=ev.entry.repo_name,
                issue=ev.entry.issue_number,
                assignment_id=ev.entry.assignment_id,
                details={"reason": "permission_or_protection"},
            )
    if dispatched_any:
        save_board(fix_board)


def _dispatch_ci_fixes(events, config, ci_store=None, *, dry_run: bool) -> None:
    """#2510: classify any CONFIRMED ``checks_failed`` events and dispatch a
    bounded CI-fix worker for the eligible ones, escalating to
    ``HUMAN_REQUIRED`` once the retry cap (``coord.ci_fix.
    MAX_CI_FIX_DISPATCHES``) is spent.

    Mutates each qualifying event's ``ev.entry.state``/``ev.entry.error`` in
    place — same shape as :func:`_dispatch_conflict_fixes` — so the caller's
    own subsequent save-queue step picks the mutation up naturally.

    ``process()`` emits a bare ``"checks_failed"`` event kind at exactly two
    points, both reached only once the OTHER auto-remedies (the #1892 infra
    rerun budget, the #2252 one-shot flake re-check) are exhausted or
    inapplicable — i.e. both ARE the "confirmed real failure" case this leg
    exists for (see ``coord.ci_fix``'s module docstring). Every other CI
    outcome (``checks_pending``, ``ci_infra_rerun``, ``ci_flaky_rerun``,
    ``checks_unreadable``, ``checks_stale``) is still self-healing or
    mid-retry and must not be touched here.

    Shared by the whole-queue path and the ``--only`` surgical path, same
    reasoning as ``_dispatch_conflict_fixes``: a bare ``checks_failed`` entry
    is never reprocessed by a later ``process()`` call (it only acts on
    ``PENDING`` entries) so whichever caller sees the event first must also
    be the one to act on it.

    #3011: before dispatching again, checks whether the entry's LAST ci-fix
    leg was a no-op — the branch HEAD is unchanged from
    ``entry.ci_fix_head_sha``, the snapshot ``dispatch_ci_fix`` took when it
    dispatched that leg. A no-op means a fresh worker looked at this
    failure and pushed no commit — evidence the failure isn't attributable
    to this branch, not evidence a fix attempt failed — so it is refunded
    (``coord.ci_fix.refund_noop_ci_fix``) rather than counted toward
    ``MAX_CI_FIX_DISPATCHES``. Consecutive no-ops are bounded separately by
    ``MAX_CI_FIX_NOOP_STREAK``, escalating to ``HUMAN_REQUIRED`` with a
    distinct reason once THAT cap is hit — the goal being that a human is
    called in because a worker genuinely tried and failed, or because the
    failure is provably not this branch's, never because two correct
    declines were miscounted as two failed attempts.

    Before trusting ``dispatch_was_noop`` at all, first checks ``coord.
    ci_fix._has_active_fix`` — the SHA comparison alone cannot distinguish
    "the fix worker finished and declined to push" from "the fix worker is
    still running and just hasn't pushed yet" (a dispatched leg stays
    PENDING for its whole lifetime, so the very next tick after dispatch
    would otherwise read as a noop). While a fix is still active, the entry
    is left untouched — no refund, no streak bump, no new dispatch — for
    the next tick to re-check.

    *ci_store* (#3114): the same ``CiStore`` the caller already built to run
    ``mq.process()`` this pass — threaded through so a fresh dispatch can
    fetch the failing job/step/log-excerpt detail via
    ``coord.ci_github.build_ci_failure_detail`` instead of handing the
    worker a bare one-line ``checks_summary``. Optional (defaults to
    ``None``) purely so every pre-#3114 caller/test keeps working
    unmodified; a ``None`` store just means no detail is fetched, same as
    today's behavior.

    The fetch itself only runs when ``coord.ci_fix.dispatch_precheck``
    confirms *entry* is otherwise dispatch-eligible, and at most once per
    distinct ``branch_head_sha`` — a repeat tick against the SAME
    still-failing SHA (dispatch declined for a reason unrelated to CI: no
    capable machine, agent unreachable, the #2538 DB-lock-contention case)
    reuses the cached result on ``entry.ci_fix_detail_sha``/
    ``ci_fix_detail_json`` instead of re-hitting ``gh`` for the log every
    time (review fix for #3114: this used to fetch unconditionally for
    every eligible event, before it was known whether dispatch would
    actually succeed).
    """
    ci_events = [ev for ev in events if ev.kind == "checks_failed"]
    if not ci_events or dry_run:
        return

    from coord.audit import record_audit  # noqa: PLC0415
    from coord.ci_fix import (  # noqa: PLC0415
        MAX_CI_FIX_DISPATCHES,
        MAX_CI_FIX_NOOP_STREAK,
        _has_active_fix,
        dispatch_ci_fix,
        dispatch_precheck,
        dispatch_was_noop,
        refund_noop_ci_fix,
    )
    from coord.merge_queue import HUMAN_REQUIRED  # noqa: PLC0415
    from coord.state import load_board, save_board  # noqa: PLC0415

    fix_board = load_board()
    if fix_board is None:
        return
    dispatched_any = False
    for ev in ci_events:
        entry = ev.entry
        # #3011 follow-up: `dispatch_was_noop` only compares the SHA
        # snapshotted at dispatch time against the current branch head — it
        # cannot tell "the fix worker finished and declined to push" apart
        # from "the fix worker is still running and just hasn't pushed yet".
        # A dispatched leg stays PENDING for its whole lifetime (`process()`
        # never mutates entry.state for this path), so on the very next tick
        # after dispatch the SHA still matches and `dispatch_was_noop` would
        # read `True` for a leg that hasn't even had a chance to push.
        # `_has_active_fix` is the same in-flight guard `dispatch_ci_fix`
        # itself checks before dispatching — treat "still running" as "still
        # pending" here too: skip the noop/refund accounting entirely and
        # leave the entry untouched for the next tick, exactly like the
        # generic "already in flight" decline below.
        if _has_active_fix(fix_board, entry):
            click.echo(
                f"  {entry.repo_name} #{entry.issue_number}: "
                "ci-fix already in flight — will re-check next run"
            )
            continue
        if dispatch_was_noop(entry):
            refund_noop_ci_fix(entry)
            dispatched_any = True
            if entry.ci_fix_noop_streak >= MAX_CI_FIX_NOOP_STREAK:
                entry.state = HUMAN_REQUIRED
                click.echo(
                    f"  {entry.repo_name} #{entry.issue_number}: "
                    f"{entry.ci_fix_noop_streak} consecutive ci-fix workers "
                    "pushed no commit — not attributable to this branch, "
                    "manual resolution required"
                )
                record_audit(
                    tier="operational",
                    category="merge",
                    event_type="ci_fix_not_attributable",
                    actor="daemon",
                    summary=(
                        f"ci-fix not attributable to branch: "
                        f"{entry.repo_name}#{entry.issue_number} — "
                        f"{entry.ci_fix_noop_streak} consecutive fix "
                        "workers pushed no commit; manual resolution "
                        "required"
                    ),
                    repo=entry.repo_name,
                    issue=entry.issue_number,
                    assignment_id=entry.assignment_id,
                    details={
                        "reason": "ci_fix_noop_streak",
                        "ci_fix_noop_streak": entry.ci_fix_noop_streak,
                        "error": entry.error,
                    },
                )
            else:
                click.echo(
                    f"  {entry.repo_name} #{entry.issue_number}: "
                    "ci-fix leg pushed no commit — refunding attempt "
                    f"({entry.ci_fix_dispatches}/{MAX_CI_FIX_DISPATCHES} "
                    "real attempts spent), will retry next run"
                )
                record_audit(
                    tier="operational",
                    category="merge",
                    event_type="ci_fix_noop_refunded",
                    actor="daemon",
                    summary=(
                        f"ci-fix leg was a no-op (branch head unchanged): "
                        f"{entry.repo_name}#{entry.issue_number} — attempt "
                        "refunded, not counted toward the retry cap"
                    ),
                    repo=entry.repo_name,
                    issue=entry.issue_number,
                    assignment_id=entry.assignment_id,
                    details={
                        "ci_fix_dispatches": entry.ci_fix_dispatches,
                        "ci_fix_noop_streak": entry.ci_fix_noop_streak,
                    },
                )
            continue
        detail = None
        if ci_store is not None and entry.pr_number is not None:
            # #3114 review fix: only fetch detail once we already know the
            # entry is otherwise dispatch-eligible (retry cap not spent, no
            # fix already in flight, the originating work assignment still
            # resolvable) — `dispatch_ci_fix` below will re-derive the same
            # `None` cheaply (no network call) for an entry that fails this
            # check, so skipping the fetch here costs nothing when it turns
            # out NOT to matter, and saves exactly the `gh api .../logs`
            # call the review flagged when it does. `log=False`: avoid
            # double-logging the "work assignment not found" warning
            # `dispatch_ci_fix` itself will also emit a moment later.
            if dispatch_precheck(entry, fix_board, log=False) is not None:
                sha_key = entry.branch_head_sha or ""
                if (
                    sha_key
                    and entry.ci_fix_detail_sha == sha_key
                    and entry.ci_fix_detail_json is not None
                ):
                    # Reuse the detail fetched for this SHA on a previous
                    # tick instead of re-hitting `gh` for the log again —
                    # this is precisely the repeat-tick case the review
                    # flagged: a dispatch declined for a reason unrelated
                    # to the CI detail (no capable machine, agent
                    # unreachable, the #2538 DB-lock-contention case)
                    # leaves the entry PENDING, and this same still-failing
                    # SHA would otherwise be re-fetched every subsequent
                    # `coord merge` tick until dispatch finally succeeds or
                    # the retry cap is hit.
                    from coord.ci_store import (  # noqa: PLC0415
                        ci_failure_detail_from_json,
                    )
                    detail = ci_failure_detail_from_json(entry.ci_fix_detail_json)
                else:
                    try:
                        from coord.ci_github import (  # noqa: PLC0415
                            build_ci_failure_detail,
                        )
                        detail = build_ci_failure_detail(
                            ci_store, entry.repo_github, entry.pr_number,
                        )
                    except Exception:  # noqa: BLE001 — best-effort enrichment
                        # (#3114): a throttled/rate-limited/malformed detail
                        # fetch must never block a dispatch that would
                        # otherwise succeed. `build_ci_failure_detail`
                        # already fails soft internally; this is
                        # defense-in-depth for anything that escapes it
                        # (e.g. a duck-typed `ci_store` missing a method
                        # entirely).
                        detail = None
                    if sha_key:
                        from coord.ci_store import (  # noqa: PLC0415
                            ci_failure_detail_to_json,
                        )
                        entry.ci_fix_detail_sha = sha_key
                        entry.ci_fix_detail_json = ci_failure_detail_to_json(detail)
        fix = dispatch_ci_fix(
            entry, fix_board, config, checks_summary=ev.message, detail=detail,
        )
        if fix is not None:
            click.echo(
                f"  {entry.repo_name} #{entry.issue_number}: "
                f"ci-fix dispatched to {fix.machine_name} "
                f"({entry.ci_fix_dispatches}/{MAX_CI_FIX_DISPATCHES})"
            )
            dispatched_any = True
            continue
        if entry.ci_fix_dispatches >= MAX_CI_FIX_DISPATCHES:
            entry.state = HUMAN_REQUIRED
            click.echo(
                f"  {entry.repo_name} #{entry.issue_number}: "
                f"ci-fix retry cap hit ({entry.ci_fix_dispatches}/"
                f"{MAX_CI_FIX_DISPATCHES}) — manual resolution required"
            )
            record_audit(
                tier="operational",
                category="merge",
                event_type="ci_fix_human_required",
                actor="daemon",
                summary=(
                    f"ci-fix retry cap hit: {entry.repo_name}#"
                    f"{entry.issue_number} — manual resolution required"
                ),
                repo=entry.repo_name,
                issue=entry.issue_number,
                assignment_id=entry.assignment_id,
                details={"reason": "ci_fix_retry_cap", "error": entry.error},
            )
            dispatched_any = True  # the HUMAN_REQUIRED mutation needs saving
        else:
            # Budget remains but dispatch declined for another reason (no
            # machine, an unrelated fix already in flight, agent
            # unreachable, or — #2538 — `_dispatch_fix` hit persistent
            # "database is locked" contention recording the assignment
            # after its own bounded retry budget was exhausted) — leave the
            # entry PENDING for the next tick to retry, exactly like a
            # declined conflict-fix dispatch does. Echoed (matching
            # `_dispatch_conflict_fixes`'s equivalent branch) so an
            # operator watching `coord merge` isn't left wondering why
            # nothing happened for this entry — and so a transient DB lock
            # collision on ONE entry is visibly a retry, not a silent drop,
            # while every other entry keeps processing normally.
            click.echo(
                f"  {entry.repo_name} #{entry.issue_number}: "
                "ci-fix not dispatched (no machine / already in flight / "
                "transient DB contention — will retry next run)"
            )
    if dispatched_any:
        save_board(fix_board)


@click.command(
    "verify-merge",
    help=(
        "Self-check a --merge-of rebase before reporting done (#604). Run from "
        "inside the merge worktree: `coord verify-merge <work_aid>`. Reports how "
        "many commits the branch is still MISSING from the default branch "
        "(`default-ahead`, must be 0), the commits it adds, and any FOREIGN "
        "commits (referencing a different issue) — the signature of a botched "
        "rebase that dragged in unrelated history. Exits non-zero when the "
        "branch is not merge-ready."
    ),
)


@click.argument("work_aid")
@click.option(
    "--path",
    "path_opt",
    type=click.Path(file_okay=False),
    default=None,
    help="Worktree to check (default: current directory).",
)


@click.option(
    "--repo",
    "repo_opt",
    default=None,
    help=(
        "Repo name — fallback when the assignment is not found on the board "
        "(thin-client machines where the board lives on the daemon, #681)."
    ),
)


@click.option(
    "--issue-number",
    "issue_number_opt",
    type=int,
    default=None,
    help=(
        "Issue number — fallback when the assignment is not found on the board "
        "(thin-client machines where the board lives on the daemon, #681)."
    ),
)


@_CONFIG_OPTION
def verify_merge(
    work_aid: str,
    path_opt: str | None,
    repo_opt: str | None,
    issue_number_opt: int | None,
    config_path: Path,
) -> None:
    """``coord verify-merge <work_aid>`` — git-truth check of a merge-prep branch.

    Resolves the issue + default branch from the *work* assignment id (the same
    id passed to ``coord assign --merge-of``) and runs the shared
    :func:`coord.agent.verify_merge_branch` primitive against the worktree the
    merge agent is sitting in.  This is the defense-in-depth twin of the
    coordinator-side gate in :func:`coord.interactive.finalize_interactive_exit`:
    same check, available to the agent before it self-reports.

    On thin-client machines (where the canonical board lives on a daemon) the
    board is fetched from the daemon automatically (#681).  As a last-resort
    fallback, supply ``--repo`` and ``--issue-number`` explicitly so the check
    can run even when the board lookup returns nothing.
    """
    from coord.agent import (  # noqa: PLC0415
        resolve_closed_issue_numbers,
        verify_merge_branch,
    )
    from coord.board_service import read_board  # noqa: PLC0415

    cfg = _load_config(config_path)
    board = read_board()
    work = board.find_by_id(work_aid)
    if work is None:
        if repo_opt and issue_number_opt is not None:
            # Thin-client fallback: the board lookup found nothing (empty local
            # DB or daemon didn't carry this aid), but the caller supplied the
            # known values explicitly via --repo / --issue-number (#681).
            repo_name = repo_opt
            issue_num = issue_number_opt
            branch_display = "(unknown)"
        else:
            click.echo(
                f"error: no assignment {work_aid!r} on the board "
                "(use the work id from `coord status`, or supply "
                "--repo and --issue-number as a fallback).",
                err=True,
            )
            sys.exit(2)
        extra_allowed: frozenset[int] = frozenset()
    else:
        repo_name = work.repo_name
        issue_num = int(work.issue_number)
        branch_display = work.branch or "(unknown)"
        # #2545: a `type="test-author"`/`"mock-author"` merge entry's
        # `issue_number` is always the milestone's TRACKING issue (every JIT
        # slice for one milestone shares a branch/PR), but its commit
        # correctly cites the slice's OWN child issue — resolved via the
        # canonical `effective_issue_number` helper (built for exactly this
        # tracking-vs-slice distinction, #1553) rather than reading
        # `for_issue_number` directly, so this stays in sync with the other
        # callers of that helper (e.g. `merge_queue._test_author_effective_
        # issue_number`) instead of drifting as an independent copy. Ordinary
        # `work` assignments have no `for_issue_number`, so this resolves to
        # `issue_num` itself — already in the "home" set, a harmless no-op.
        _for_issue = effective_issue_number(work)
        extra_allowed = (
            frozenset({_for_issue}) if _for_issue and _for_issue != issue_num else frozenset()
        )

    repo_cfg = cfg.repo(repo_name)
    base = (repo_cfg.default_branch if repo_cfg else None) or "main"
    if repo_cfg is not None and getattr(repo_cfg, "develop_branch", None):
        # #934: verify against `feature/ms-NN` when this issue belongs to a
        # milestone and the repo opted into the git model — falls back to
        # `default_branch` (above) for everything else.
        from coord.branch_model import (  # noqa: PLC0415
            fetch_issue_milestone_number,
            resolve_base_branch,
        )

        milestone_number = fetch_issue_milestone_number(repo_cfg.github, issue_num)
        base = resolve_base_branch(repo_cfg, milestone_number)
    repo_github = repo_cfg.github if repo_cfg else None
    wt_path = Path(path_opt).expanduser() if path_opt else Path.cwd()

    mv = verify_merge_branch(
        wt_path,
        base=base,
        issue_number=issue_num,
        extra_allowed_issue_numbers=extra_allowed,
    )
    # #1279: only worth a `gh` round-trip when the cheap git-only pass above
    # actually found blocking foreign commits — corroborate against GitHub's
    # closed-issue state and re-verify with the downgrade signal populated.
    closed = resolve_closed_issue_numbers(repo_github, mv.foreign, issue_num)
    if closed:
        mv = verify_merge_branch(
            wt_path,
            base=base,
            issue_number=issue_num,
            extra_allowed_issue_numbers=extra_allowed,
            closed_issue_numbers=closed,
        )

    click.echo(f"branch:        {branch_display}")
    click.echo(f"target base:   {base}")
    click.echo(f"{base}-ahead:   {mv.default_ahead}  (must be 0)")
    click.echo(f"adds {len(mv.added)} commit(s) over {base}:")
    advisory_set = set(mv.advisory_foreign)
    for sha, subj in mv.added:
        if (sha, subj) in mv.foreign:
            flag = " [FOREIGN — BLOCKING]"
        elif (sha, subj) in advisory_set:
            flag = " [advisory: references closed issue]"
        else:
            flag = ""
        click.echo(f"  {sha[:9]} {subj}{flag}")

    if mv.ok:
        click.echo("✓ merge-ready: base fully contained, no foreign commits.")
        note = mv.advisory_note()
        if note:
            click.echo(f"  {note}")
        return
    click.echo(f"✗ NOT merge-ready: {mv.block_summary(base)}", err=True)
    sys.exit(1)


@click.command(
    help=(
        "Bounce the pipeline back to Work after a review requested changes. "
        "Dispatches a fix worker that reads the reviewer's findings as its "
        "briefing and pushes corrections to the same branch."
    ),
)


@click.argument("review_assignment_id")
@_CONFIG_OPTION
def bounce(review_assignment_id: str, config_path: Path) -> None:
    """Manual trigger for the auto-loop's fix-dispatch path.

    `coord notify` already runs this automatically the first time a
    review completion is observed, but the auto-loop bails when the
    review log isn't reachable at that moment (remote agent offline /
    log pruned).  This command re-runs the same dispatch on demand —
    useful as a recovery path for the user and as the TUI's "Fix"
    button.
    """
    from coord.auto_loop import process_review_completion
    from coord.board_service import read_board, write_board
    from coord.state import COORD_DIR

    cfg = _load_config(config_path)
    board = read_board()

    review = board.find_by_id(review_assignment_id)
    if review is None:
        click.echo(
            f"error: assignment {review_assignment_id!r} not found in board",
            err=True,
        )
        sys.exit(1)
    if review.type != "review":
        click.echo(
            f"error: {review_assignment_id} is type={review.type!r}, not 'review'. "
            f"Pass the review assignment id, not the work assignment id.",
            err=True,
        )
        sys.exit(1)
    if review.review_verdict not in ("request-changes", None):
        click.echo(
            f"info: review verdict is {review.review_verdict!r} — only "
            f"'request-changes' triggers a fix dispatch. Nothing to do.",
            err=True,
        )
        sys.exit(1)

    # Try local log first; fall back to agent HTTP /logs when the
    # review ran on a remote machine and the file isn't on this
    # coordinator's filesystem.
    machine = next(
        (m for m in cfg.machines if m.name == review.machine_name), None,
    )
    machine_host = machine.host if machine and machine.host else None
    local_log = COORD_DIR / "logs" / f"{review_assignment_id}.log"
    log_path = str(local_log) if local_log.exists() else None

    actions = process_review_completion(
        review,
        board,
        cfg,
        log_path=log_path,
        machine_host=machine_host,
    )

    dispatched = any(a.kind == "fix_dispatched" for a in actions)
    # #522: terminal_skip mutates work.review_state="done" in
    # process_review_completion — persist it (same as the notify path) so the
    # row doesn't get re-evaluated, and treat it as a clean (not failed) exit.
    terminal = any(a.kind == "terminal_skip" for a in actions)
    if dispatched or terminal:
        write_board(board)

    for a in actions:
        click.echo(f"{a.kind}: {a.detail}")

    if not dispatched:
        # Distinguish clean outcomes (approve / already-merged-or-closed) from
        # genuine failure modes.
        if any(a.kind in ("approved", "terminal_skip") for a in actions):
            sys.exit(0)
        sys.exit(1)


@click.command(
    "reconcile-merges",
    help=(
        "Reconcile done work assignments against git/GitHub reality.\n\n"
        "Three conservative sweeps:\n"
        "  #611 — backfill a missing branch from a matching `issue-N-*` remote "
        "branch (a remote interactive session can finish done with branch=None, "
        "greying the TUI Start review/test/merge buttons);\n"
        "  #609 — flip work merged out-of-band (direct GitHub merge or a drained "
        "merge_queue row) to status='merged' so the TUI stops showing a grey "
        "merge box forever;\n"
        "  #721 — close open PRs whose work has already landed (issue closed or "
        "branch fully on the default branch) — review-PRs accumulate forever "
        "after squash merges otherwise.\n\n"
        "Acts only when certain; skips and explains otherwise."
    ),
)


@click.option("--repo", "repo_name", default=None, help="Only reconcile this repo.")
@click.option(
    "--dry-run", is_flag=True, help="Show what would change without writing."
)


@_CONFIG_OPTION
def reconcile_merges(repo_name: str | None, dry_run: bool, config_path: Path) -> None:
    """#609/#611: record out-of-band merges and backfill missing branches."""
    # #584: the canonical board + gh live on the daemon host, so on a thin
    # client this would sweep an empty local board and silently do nothing.
    # Route the whole operation to the daemon (mirrors `coord merge`).
    # COORD_RECONCILE_ON_DAEMON guards the daemon against re-routing to itself.
    from coord.board_service import daemon_reroute_target  # noqa: PLC0415

    _svc = daemon_reroute_target("COORD_RECONCILE_ON_DAEMON")
    if _svc is not None:
        _reconcile_via_daemon(_svc, {"repo": repo_name, "dry_run": dry_run})
        return

    from coord.reconcile import reconcile_board_merges
    from coord.state import build_board, save_board

    cfg = _load_config(config_path)
    board = build_board()
    actions = reconcile_board_merges(
        board, cfg, repo=repo_name, dry_run=dry_run
    )
    if not dry_run:
        save_board(board)
    if not actions:
        click.echo("Nothing to reconcile.")
        return
    for action in actions:
        click.echo(action)


@click.command(
    "post-pending-reviews",
    help=(
        "Post unposted review findings for done review assignments.\n\n"
        "Useful when a reviewer finished but notify didn't see the transition "
        "(e.g. agent reported 'cancelled', reap hung, or notify ran at the wrong time). "
        "Idempotent — already-posted findings are never re-posted."
    ),
)


@_CONFIG_OPTION
@click.option("--repo", "repo_name", default=None, help="Only process assignments for this repo.")
def post_pending_reviews(config_path: Path, repo_name: str | None) -> None:
    from coord.notify import post_orphaned_review_findings
    from coord.state import load_done_reviews_needing_post

    cfg = _load_config(config_path)

    candidates = load_done_reviews_needing_post(repo_name=repo_name)
    if not candidates:
        click.echo("No pending review assignments found.")
        return

    click.echo(f"Found {len(candidates)} review assignment(s) with unposted findings:")
    for row in candidates:
        aid = row["assignment_id"]
        click.echo(
            f"  {aid} — {row['repo_name']} #{row['issue_number']} "
            f"(machine: {row['machine_name']}, target: {row['review_target'] or 'n/a'})"
        )

    posted_ids = post_orphaned_review_findings(cfg, repo_name=repo_name)

    if not posted_ids:
        click.echo("\nNo findings posted (agents may be offline or logs unavailable).")
        return

    click.echo(f"\nPosted findings for {len(posted_ids)} assignment(s):")
    for aid in posted_ids:
        click.echo(f"  {aid}")

    still_pending = load_done_reviews_needing_post(repo_name=repo_name)
    if still_pending:
        click.echo(f"\n{len(still_pending)} assignment(s) still pending (logs not available):")
        for row in still_pending:
            click.echo(
                f"  {row['assignment_id']} — {row['repo_name']} #{row['issue_number']} "
                f"(machine: {row['machine_name']})"
            )


@click.command(
    "backfill-review-cost",
    help=(
        "One-shot repair for #2476: capture cost/tokens for review "
        "assignments left at cost_usd IS NULL/0 by the review-completion "
        "capture gap (post_orphaned_review_findings recovered the verdict "
        "but never captured cost/tokens — fixed going forward, this repairs "
        "the backlog it already created).\n\n"
        "Walks every terminal type='review' row with cost_usd IS NULL or 0, "
        "tries the local log first, then the assignment's agent /logs/<id> "
        "endpoint, parses, and persists via the same "
        "update_assignment_cost/update_assignment_tokens writers the live "
        "capture path uses — no new write mechanism. Run once per machine "
        "that hosts review logs; logs that have already aged out (or "
        "whose agent is offline) are reported as still-missing rather than "
        "silently dropped, so the residual gap stays known."
    ),
)
@_CONFIG_OPTION
@click.option("--repo", "repo_name", default=None, help="Only process assignments for this repo.")
def backfill_review_cost(config_path: Path, repo_name: str | None) -> None:
    from coord.notify import _capture_cost_and_tokens_for_review
    from coord.state import load_review_assignments_missing_cost
    from coord.usage import LOGS_DIR

    cfg = _load_config(config_path)
    machines_by_name = {m.name: m for m in cfg.machines}

    candidates = load_review_assignments_missing_cost(repo_name=repo_name)
    if not candidates:
        click.echo("No review assignments with missing cost/tokens found.")
        return

    click.echo(f"Found {len(candidates)} review assignment(s) with missing cost/tokens:")
    for row in candidates:
        click.echo(
            f"  {row['assignment_id']} — {row['repo_name']} #{row['issue_number']} "
            f"(machine: {row['machine_name']})"
        )

    recovered: list[dict] = []
    still_missing: list[dict] = []
    for row in candidates:
        aid = row["assignment_id"]
        machine = machines_by_name.get(row["machine_name"])
        host = machine.host if machine is not None else None
        # #2476: always offer the conventional local path — it resolves
        # correctly whenever this command runs on the same box that hosted
        # the review (the common case for a per-machine repair run), and is
        # a harmless miss otherwise (falls straight through to the *host*
        # fetch below, same as `_capture_cost_and_tokens_for_review` already
        # does for every other caller).
        log_path = str(LOGS_DIR / f"{aid}.log")
        wrote = _capture_cost_and_tokens_for_review(
            aid, log_path=log_path, host=host,
            provider_name=row.get("provider_name"),
        )
        (recovered if wrote else still_missing).append(row)

    click.echo(f"\nRecovered cost/tokens for {len(recovered)} assignment(s):")
    for row in recovered:
        click.echo(f"  {row['assignment_id']} — {row['repo_name']} #{row['issue_number']}")

    if still_missing:
        click.echo(
            f"\n{len(still_missing)} assignment(s) still missing "
            "(log gone on disk and agent unreachable/offline):"
        )
        for row in still_missing:
            click.echo(
                f"  {row['assignment_id']} — {row['repo_name']} #{row['issue_number']} "
                f"(machine: {row['machine_name']})"
            )
    click.echo(
        f"\nSummary: {len(recovered)} recovered, {len(still_missing)} still missing "
        f"of {len(candidates)} total."
    )


def _load_issue_states() -> tuple[dict[str, set[int]], dict[str, set[int]]]:
    """Return ``(open_by_repo, known_by_repo)``.

    - ``open_by_repo[repo]`` = set of issue numbers with state='open'.
    - ``known_by_repo[repo]`` = set of issue numbers with ANY state row in
      the cache.

    Used by the `coord merge` auto-enqueue path (#242).  Filter logic
    (in the caller) is permissive on cache misses:

    - issue in ``known_by_repo[repo]`` AND not in ``open_by_repo[repo]``
      → deny (we have explicit "closed" evidence)
    - otherwise → allow

    The earlier implementation denied any issue whose repo had ANY rows in
    the issues table but no row for the specific number — which silently
    skipped issues created after the cache's most-recent sync (we hit this
    when #278/#280 landed but the local cache stopped at #271).
    """
    try:
        from coord import sql
        from coord.db import get_connection

        conn = get_connection()
        rows = sql.execute(conn, "SELECT repo_name, number, state FROM issues").fetchall()
    except Exception:  # noqa: BLE001 — caller treats empty as "unknown"
        return {}, {}

    open_by_repo: dict[str, set[int]] = {}
    known_by_repo: dict[str, set[int]] = {}
    for row in rows:
        repo_name = row[0]
        number = int(row[1])
        known_by_repo.setdefault(repo_name, set()).add(number)
        if row[2] == "open":
            open_by_repo.setdefault(repo_name, set()).add(number)
    return open_by_repo, known_by_repo


def _reconcile_via_daemon(svc, params: dict) -> None:
    """#584: run ``coord reconcile-merges`` on the daemon host (where the
    canonical DB lives + gh is authenticated) and relay its output, so the
    command does real work from a thin client instead of no-opping against an
    empty local board.  Reconcile is gh-bound but quick, hence the shorter
    timeout."""
    from coord.client import post_record  # noqa: PLC0415

    try:
        resp = post_record(svc, "/reconcile-merges", params, timeout=120.0)
    except Exception as exc:  # noqa: BLE001
        click.echo(f"error: reconcile-merges via daemon failed: {exc}", err=True)
        sys.exit(1)
    output = resp.get("output") or ""
    if output:
        click.echo(output, nl=False)
    if resp.get("error"):
        click.echo(f"error: {resp['error']}", err=True)
    code = resp.get("exit_code") or 0
    if code:
        sys.exit(int(code))


def _ci_field(summary, name: str, default=0):
    """Read *name* off a CI rollup, tolerating both shapes ``p.ci_summary``/
    ``p.ci_summary_all`` can arrive in (#2446).

    ``coord.merge_queue.plan()`` (the local, in-process path) hands back
    real :class:`~coord.ci_store.CiCheckSummary` instances. ``coord merge
    --plan`` against a daemon instead reconstructs ``PlannedMerge`` from the
    ``/board`` JSON payload (``_show_plan_from_daemon`` below) — the server
    side flattens each rollup with ``dataclasses.asdict`` and nothing
    re-hydrates it, so on that path both fields arrive as plain ``dict``s
    instead. ``summary`` is ``None`` on either path when there's nothing to
    report (no PR yet, no ``ci_store``, …).
    """
    if summary is None:
        return default
    if isinstance(summary, dict):
        return summary.get(name, default)
    return getattr(summary, name, default)


def _advisory_ci_note(p) -> str:
    """#2446: describe any check failing/still-running in the unfiltered
    (required + advisory) CI rollup that the required-only rollup
    (``p.ci_summary`` — the same view the merge gate itself evaluates)
    doesn't already account for.

    A regression here is on a check GitHub's own branch protection doesn't
    require — `coord merge`'s gate correctly no longer blocks on it (see
    ``coord.ci_github.GitHubCi.list_checks_for_pr``'s docstring), but an
    operator staring at a READY row while a real job is red/hung nearby
    still deserves to see it. Returns ``""`` when there's nothing extra to
    report (no ``ci_summary_all``, or it agrees with ``ci_summary``).
    """
    if p.ci_summary_all is None:
        return ""
    req_failed = _ci_field(p.ci_summary, "failed")
    req_running = _ci_field(p.ci_summary, "running")
    req_failed_names = set(_ci_field(p.ci_summary, "failed_names", []) or [])
    extra_failed = _ci_field(p.ci_summary_all, "failed") - req_failed
    extra_running = _ci_field(p.ci_summary_all, "running") - req_running
    if extra_failed <= 0 and extra_running <= 0:
        return ""
    all_failed_names = _ci_field(p.ci_summary_all, "failed_names", []) or []
    advisory_failed_names = [n for n in all_failed_names if n not in req_failed_names]
    bits = []
    if advisory_failed_names:
        bits.append("failing: " + ", ".join(advisory_failed_names))
    elif extra_failed > 0:
        bits.append(f"{extra_failed} failing")
    if extra_running > 0:
        bits.append(f"{extra_running} running")
    return "   [advisory CI, not blocking — " + "; ".join(bits) + "]"


def _print_merge_plan_entries(planned: list) -> None:
    """Print a list of PlannedMerge entries grouped by repo → target_branch."""
    if not planned:
        click.echo("Merge queue is empty (nothing to plan).")
        return
    _last_group: tuple[str, str] | None = None
    for _p in planned:
        _gkey = (_p.repo_name, _p.target_branch)
        if _gkey != _last_group:
            if _last_group is not None:
                click.echo("")
            click.echo(f"{_p.repo_name} → {_p.target_branch}")
            _last_group = _gkey
        _size_str = f"+{_p.size}" if _p.size is not None else "?"
        _status_str = _p.status
        if _p.reason:
            _status_str = f"{_p.status}   {_p.reason}"
        click.echo(
            f"  {_p.rank}. #{_p.issue_number}  {_size_str}   "
            f"{_status_str}     {_p.issue_title}{_advisory_ci_note(_p)}"
        )


def _print_sibling_overlap_warnings(warnings: list) -> None:
    """#920: print "these approved branches will conflict" warnings.

    ``warnings`` is a list of :class:`coord.merge_queue.SiblingOverlapWarning`
    (or daemon-payload reconstructions of the same shape). No-op when empty —
    callers don't need to guard the call.
    """
    if not warnings:
        return
    click.echo("")
    click.echo("⚠ Sibling overlap (approved branches aging against a moving main):")
    for w in warnings:
        order = " → ".join(f"#{n}" for n in w.issue_numbers)
        files = list(w.overlapping_files)
        files_str = ", ".join(files[:5])
        if len(files) > 5:
            files_str += f", +{len(files) - 5} more"
        click.echo(f"  {w.repo_name} → {w.target_branch}: {order}")
        click.echo(f"    overlapping files: {files_str}")
        click.echo(
            f"    oldest waiting {w.oldest_age_hours:.1f}h — these will conflict if merged"
            " out of order or later; merge promptly, oldest first"
            " ('coord merge --order <assignment_ids>' to force this order)."
        )


def _board_row_gate_report(a, config, board, gh_ops: "GhOps | None"):
    """The live, #1479-freshness-aware gate report for board row *a* — the
    SAME evaluation ``coord gates <repo> <issue>`` runs
    (:func:`coord.gates.build_gate_report`), or ``None`` when it couldn't be
    computed (repo not configured, no branch, board/config error, …).

    Shared by both ``--only`` call sites that used to re-derive gate status
    by calling :func:`coord.merge_queue.merge_gate_failures` directly on a
    raw board ``Assignment`` — the #1845 retry-enqueue probe below and
    :func:`_explain_missing_only_entry`'s diagnostic. A bare ``Assignment``
    has no ``repo_github``/``branch_head_sha``/``target_branch_head_sha``/
    ``branch_patch_id`` (those only exist on a
    :class:`~coord.merge_queue.QueuedMerge` that has been through
    :func:`~coord.merge_queue.process`), so every #1479 staleness check
    inside :func:`~coord.merge_queue.evaluate_smoke_verdict` silently no-ops
    against it — the row reads as fresh no matter how stale it really is.
    ``build_gate_report`` builds the same synthetic, live-SHA-backed
    ``QueuedMerge`` ``coord gates`` does, so the two can never disagree
    (#1926).

    Never raises: any failure degrades to ``None``, which both callers
    treat as "not gated" / "not evaluated" rather than a guessed pass.
    """
    from coord.gates import build_gate_report as _build_gate_report  # noqa: PLC0415
    from coord.models import effective_issue_number as _effective_issue_number  # noqa: PLC0415

    try:
        return _build_gate_report(
            board,
            config,
            a.repo_name,
            _effective_issue_number(a) or a.issue_number,
            gh_ops=gh_ops,
        )
    except Exception:  # noqa: BLE001 — diagnostics/probes must never mask the error
        return None


def _board_row_merge_gate_ok(a, config, board, gh_ops: "GhOps | None") -> bool:
    """True only when *a*'s live gate report says the merge gate is
    satisfied — see :func:`_board_row_gate_report`. Fails closed to
    ``False`` (not gated) whenever the report itself couldn't be computed,
    matching #1926's "silence is safe, optimism is not": a ``False`` here
    just means the caller falls through to a diagnostic, never a wrongly
    attempted enqueue/merge.
    """
    report = _board_row_gate_report(a, config, board, gh_ops)
    if report is None:
        return False
    merge_decision = next((d for d in report.decisions if d.gate == "merge"), None)
    return merge_decision is not None and merge_decision.ok


def _explain_missing_only_entry(
    key: str, config, gh_ops: "GhOps | None" = None
) -> list[str]:
    """#1695: explain why ``coord merge --only <key>`` found no queue entry.

    Returns the lines to print on stderr. The pre-#1695 message was a single
    line — *"no entry found for 'X' (tried assignment_id, repo#issue, issue
    number, and branch name)"* — which describes a **key-lookup** failure and
    is what sent the #1695 operator hunting for a different identifier for 40
    minutes. In the case that actually happens, the identifier resolved fine
    and the row simply never became an entry because a gate blocked enqueue.

    So the three cases are now stated separately:

    1. **No board row either.** The identifier genuinely did not resolve —
       the old wording, now only printed when it is true.
    2. **A board row exists and a gate is failing.** Name the row, name the
       gate, and point at the flag that waives it. Under #1695 the row is
       enqueued (visibly BLOCKED) by the auto-enqueue scan, so the fix is to
       let a plain ``coord merge`` pass run the scan and then retry ``--only``
       with the waiver — hence the explicit next-step line.
    3. **A board row exists and every gate passes.** Then it was one of the
       non-gate skips (issue closed, branch gone from origin, PR already
       merged) or the scan simply has not run yet.

    #1926: case 2/3 used to call :func:`coord.merge_queue.merge_gate_failures`
    directly on the raw board ``Assignment`` with no *gh_ops* and no live
    SHAs, which makes every #1479 staleness check inside
    :func:`~coord.merge_queue.evaluate_smoke_verdict` a silent no-op (it needs
    ``entry.repo_github``/``entry.target_branch_head_sha``/etc., none of which
    a bare work ``Assignment`` carries — those only exist on a
    :class:`~coord.merge_queue.QueuedMerge` that has been through
    :func:`~coord.merge_queue.process`). That let this fallback print "all
    merge gates pass" for a row ``coord gates`` reported ``BLOCKED`` with a
    STALE verdict — the exact false-green this issue is about. Routing
    through :func:`coord.gates.build_gate_report` instead reuses the SAME
    live-SHA-backed evaluation ``coord gates`` runs, so the two can never
    disagree.

    Never raises: a board/config problem, or a gate report that can't reach a
    merge decision (no branch, repo not configured, …), degrades to an
    honest "not evaluated" line rather than a guessed pass.
    """
    from coord import merge_queue as _mq  # noqa: PLC0415
    from coord.state import load_board as _load_board  # noqa: PLC0415

    not_found = (
        f"merge-queue: no entry found for {key!r} "
        "(tried assignment_id, repo#issue, issue number, and branch name)"
    )
    try:
        board = _load_board()
        rows = _mq.resolve_board_work_key(board, key) if board is not None else []
    except Exception:  # noqa: BLE001 — diagnostics must never mask the error
        rows = []
    if not rows:
        return [
            not_found,
            "  no done work row on the board matches that identifier either — "
            "the identifier did not resolve.",
        ]

    lines = [
        f"merge-queue: no entry found for {key!r}, but "
        f"{len(rows)} done work row(s) on the board match it:",
    ]
    any_blocked = False
    for a in rows:
        where = f"{a.repo_name} #{a.issue_number} (assignment {a.assignment_id}, branch {a.branch})"
        report = _board_row_gate_report(a, config, board, gh_ops)
        merge_decision = (
            next((d for d in report.decisions if d.gate == "merge"), None)
            if report is not None
            else None
        )
        if merge_decision is None:
            # Same evaluation `coord gates` runs couldn't reach a merge
            # decision for this row (repo not configured, no branch on the
            # winning assignment, board/config error, …). Say so instead of
            # guessing — silence is safe, optimism is not (#1926).
            lines.append(
                f"  {where} — gate status not evaluated on this path; run "
                f"`coord gates {a.repo_name} {a.issue_number}` for the live "
                "decision."
            )
            continue
        if not merge_decision.ok:
            any_blocked = True
            clauses = []
            for gate_name, waiver_flag in (
                ("review", "--skip-review"),
                ("test", "--skip-smoke"),
            ):
                d = next((x for x in report.decisions if x.gate == gate_name), None)
                if d is not None and not d.ok:
                    clauses.append(f"{gate_name} gate — {d.reason} (waive with {waiver_flag})")
            lines.append(f"  {where} — enqueue blocked by {'; '.join(clauses)}")
        else:
            lines.append(
                f"  {where} — all merge gates pass (#1479 freshness checked); "
                "it was skipped for a non-gate reason (issue closed, branch "
                "missing from origin, or PR already merged), or the "
                "auto-enqueue scan has not run yet."
            )
    if any_blocked:
        lines.append(
            "  next: run `coord merge --dry-run` (the auto-enqueue scan "
            "enqueues gate-blocked rows in a visibly BLOCKED state, #1695), "
            "then retry --only with the waiver flag named above."
        )
    return lines


def _show_plan_from_daemon(
    svc,
    *,
    repo_filter: str | None,
    order: str | None,
) -> None:
    """#779-fix: display merge plan via /board — never touches /merge.

    Older daemons (≤v0.4.53 pre-#779) receive ``plan=True`` via ``/merge``
    but have no show_plan handler, so they fall through to a full live merge
    cycle with side effects.  The ``merge_plan`` field has been injected into
    ``/board`` since #776/v0.4.53, so we fetch that instead — guaranteed
    read-only on every supported daemon version.

    Exits with an error message if the daemon payload lacks ``merge_plan``
    (daemon predates v0.4.53); the caller should not fall through to a local
    path that would show an empty thin-client queue.
    """
    from coord.client import fetch_board_payload  # noqa: PLC0415
    from coord.merge_queue import PlannedMerge  # noqa: PLC0415

    try:
        payload = fetch_board_payload(svc)
    except Exception as exc:  # noqa: BLE001
        click.echo(f"error: fetch board for --plan failed: {exc}", err=True)
        sys.exit(1)

    if "merge_plan" not in payload:
        click.echo(
            "error: daemon does not expose merge_plan in /board "
            "(upgrade the daemon to v0.4.53+ to use coord merge --plan).",
            err=True,
        )
        sys.exit(1)

    raw: list[dict] = payload.get("merge_plan") or []
    known = set(PlannedMerge.__dataclass_fields__)
    planned = [PlannedMerge(**{k: v for k, v in d.items() if k in known}) for d in raw]

    if repo_filter:
        planned = [p for p in planned if p.repo_name == repo_filter]

    if order:
        _override_ids = [s.strip() for s in order.split(",") if s.strip()]
        _by_id = {p.assignment_id: p for p in planned}
        _head = [_by_id[aid] for aid in _override_ids if aid in _by_id]
        _tail = [p for p in planned if p.assignment_id not in set(_override_ids)]
        planned = _head + _tail
        for _i, _p in enumerate(planned, 1):
            _p.rank = _i

    _print_merge_plan_entries(planned)

    # #920: sibling-overlap warnings — precomputed server-side into /board
    # (see coord.serve_app's `sibling_overlap_warnings` projection) since a
    # thin client has no local queue/board to compute them from itself.
    from coord.merge_queue import SiblingOverlapWarning  # noqa: PLC0415

    raw_overlaps: list[dict] = payload.get("sibling_overlap_warnings") or []
    known_ow = set(SiblingOverlapWarning.__dataclass_fields__)
    overlaps = [
        SiblingOverlapWarning(**{
            k: (tuple(v) if isinstance(v, list) else v)
            for k, v in d.items() if k in known_ow
        })
        for d in raw_overlaps
    ]
    if repo_filter:
        overlaps = [w for w in overlaps if w.repo_name == repo_filter]
    _print_sibling_overlap_warnings(overlaps)


def _merge_via_daemon(svc, params: dict) -> None:
    """#584: run ``coord merge`` on the daemon host (where the canonical DB +
    merge queue + gh live) and relay its output, so the TUI 'Go' button and
    ``coord merge`` work from any thin client.  Merges can take minutes (PR
    creation, CI waits), hence the long timeout.

    #1769: a ``--revalidate`` run additionally executes the repo's whole test
    suite on the daemon host, which is minutes-to-tens-of-minutes on its own —
    the default 900 s ceiling would abandon the client mid-suite (the daemon
    keeps going and finishes the merge, so the operator sees a timeout error
    for a run that actually succeeded).

    #1715-review: batch revalidation's own worst case is 1 (composite) + N
    (per-entry fallback) serial suite runs, not just one — see
    :func:`coord.revalidate.revalidate_group`. :func:`coord.revalidate.
    client_timeout_seconds` sizes the window off that worst case (a
    documented ceiling on N, since the client posts before any candidate is
    known) rather than a single :data:`coord.revalidate.
    DEFAULT_TIMEOUT_SECONDS`, which is the ceiling ONE suite run is killed
    at."""
    from coord.client import post_record  # noqa: PLC0415
    from coord.revalidate import client_timeout_seconds  # noqa: PLC0415

    timeout = client_timeout_seconds(bool(params.get("revalidate")))

    try:
        resp = post_record(svc, "/merge", params, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        click.echo(f"error: merge via daemon failed: {exc}", err=True)
        sys.exit(1)
    output = resp.get("output") or ""
    if output:
        click.echo(output, nl=False)
    if resp.get("error"):
        click.echo(f"error: {resp['error']}", err=True)
    code = resp.get("exit_code") or 0
    if code:
        sys.exit(int(code))


@click.command(help="Process the merge queue: open PRs and merge in sequence.")
@_CONFIG_OPTION
@click.option("--dry-run", is_flag=True, help="Show the plan without opening or merging PRs.")
@click.option(
    "--plan",
    "show_plan",
    is_flag=True,
    help=(
        "#779: Print the ranked merge order and per-entry gate status. "
        "No PRs opened, no merges — purely read-only."
    ),
)


@click.option(
    "--order",
    default=None,
    help="Comma-separated assignment IDs to merge first (overrides size-based sequencing).",
)


@click.option("--repo", "repo_filter", default=None, help="Only process this repo's queue.")
@click.option(
    "--method",
    type=click.Choice(["rebase", "squash", "merge"]),
    default="rebase",
    show_default=True,
)


@click.option(
    "--force-merge",
    is_flag=True,
    help=(
        "Skip the CI check gate — merge even if checks failed or are still running. "
        "Also overrides the #1318 epic-closing-keyword guard: merge anyway even "
        "when a commit message on the branch contains a closing keyword targeting "
        "an epic (the epic WILL auto-close on GitHub)."
    ),
)


@click.option(
    "--skip-review",
    is_flag=True,
    help=(
        "Skip the review-approval gate — merge even when no approved review is on "
        "the board (#253). Local-only: when this run is routed to the daemon "
        "(thin client / no local canonical DB), the daemon rejects a truthy "
        "--skip-review outright (non-zero exit, explicit error) rather than "
        "honouring or silently dropping it — the review gate can never be "
        "bypassed remotely (#821, #1489)."
    ),
)


@click.option(
    "--skip-smoke",
    is_flag=True,
    help="Skip the interactive smoke-test gate — merge even when no smoke verdict is recorded (#465).",
)


@click.option(
    "--revalidate",
    is_flag=True,
    help=(
        "#1769/#1715: re-test entries blocked SOLELY on a stale-but-passed "
        "test verdict against the current base, then merge them. Off by "
        "default — `coord merge` with no flag is unchanged, and the unattended "
        "auto-drain never sets it. Applies only to the stale case: an entry "
        "blocked on review, CI, conflict, or a genuinely missing verdict is "
        "left untouched. "
        "BATCH (#1715): when several entries share a base they are composed "
        "onto it together and validated by ONE suite run, not one run each — "
        "N approved branches cost 1 run. The honest trade: that validates the "
        "COMPOSITE, not each branch alone. It is a re-confirmation rather than "
        "a first proof — every member already holds its own passed verdict "
        "from an earlier base — and the batch merges against the same base "
        "snapshot the composite was built on. "
        "If the composite FAILS, nothing merges and nothing is marked failed; "
        "each branch is then re-tested alone, so the culprit is named and the "
        "innocent branches still merge (worst case 1+N runs, typical case 1). "
        "Runs the repo's own build/test commands locally, so it must run where "
        "the repo is checked out (on a thin client it routes to the daemon "
        "host, like the rest of `coord merge`). Distinct from --skip-smoke, "
        "which waives the gate instead of satisfying it."
    ),
)


@click.option(
    "--drop",
    "drop_assignment",
    default=None,
    metavar="ASSIGNMENT_ID",
    help=(
        "#732: Drop exactly one merge_queue entry — accepts the assignment_id, the "
        "durable 'repo#issue' form (#1477), a bare issue number, or the branch name "
        "(#1490) — whichever the board printed, since assignment_id can re-key across "
        "a drop + re-enqueue (or an auto-enqueue tick) between the read and this call. "
        "Routes through the daemon so thin clients don't need local DB access."
    ),
)


@click.option(
    "--only",
    "only_assignment",
    default=None,
    metavar="ASSIGNMENT_ID",
    help=(
        "#780: Merge exactly one entry — accepts the assignment_id, the durable "
        "'repo#issue' form (#1477), e.g. 'acme/api#1461', a bare issue number, or the "
        "branch name (#1490) — whichever the board printed.  Resolution falls back "
        "through these forms in order, so an id that was re-keyed by a concurrent "
        "auto-enqueue tick since the board was last read still resolves via issue "
        "number or branch.  Leaves the rest of the queue untouched.  Mutually "
        "exclusive with --order.  BLOCKED entries are reported and skipped (use "
        "--force-merge to override gates)."
    ),
)


@click.option(
    "--override-human-required",
    "override_human_required",
    default=None,
    metavar="REASON",
    help=(
        "#1251: explicit, audited override for a HUMAN_REQUIRED entry — clears the "
        "flag and requeues it as PENDING so this run's other gates (--skip-review, "
        "--skip-smoke, --force-merge) can still apply normally. Requires --only "
        "<assignment_id> and a reason string, which is written to the audit trail "
        "alongside the original conflict_human_required event. Distinct from "
        "--force-merge on purpose: human_required means an automated process already "
        "gave up on this entry, not just that a gate wasn't run."
    ),
)


def merge(
    config_path: Path,
    dry_run: bool,
    show_plan: bool,
    order: str | None,
    repo_filter: str | None,
    method: str,
    force_merge: bool,
    skip_review: bool,
    skip_smoke: bool,
    revalidate: bool,
    drop_assignment: str | None,
    only_assignment: str | None,
    override_human_required: str | None,
) -> None:
    # #1251: --override-human-required is a surgical single-entry override — it
    # only makes sense paired with --only, which pins down the one entry it
    # applies to.  Validate up front (before any daemon round-trip) so a thin
    # client fails fast instead of silently no-op'ing the flag on the daemon
    # side (only_assignment gates the block that actually consumes it below).
    #
    # #1251-review: both this check and the later `if override_human_required:`
    # gate treat an empty/whitespace-only reason as falsy, so
    # `--override-human-required ""` would otherwise skip *every* validation
    # and *every* effect — no error, no override, no audit row — leaving the
    # entry stuck HUMAN_REQUIRED with no feedback that the reason was
    # rejected.  Catch it explicitly first, before the --only check, since an
    # empty reason is invalid regardless of what else was passed.
    if override_human_required is not None and not override_human_required.strip():
        click.echo(
            "error: --override-human-required requires a non-empty reason string",
            err=True,
        )
        sys.exit(1)
    if override_human_required and not only_assignment:
        click.echo(
            "error: --override-human-required requires --only <assignment_id> — "
            "it targets exactly one entry, never a repo-wide scan",
            err=True,
        )
        sys.exit(1)

    # #584: the merge queue + board live in the canonical (host-local) DB, so on
    # a thin client `coord merge` (and the TUI 'Go' button, which shells out to
    # it) would silently no-op against an empty local board.  Route the whole
    # operation to the daemon — it runs the merge where the DB + gh live and
    # returns its output.  COORD_MERGE_ON_DAEMON guards the daemon against
    # re-routing to itself (it calls this same command with the env var set).
    from coord.board_service import daemon_reroute_target  # noqa: PLC0415

    _merge_svc = daemon_reroute_target("COORD_MERGE_ON_DAEMON")
    if _merge_svc is not None:
        # #779-fix: --plan must never reach /merge on an older daemon — it has
        # no show_plan handler and falls through to a live merge cycle (side
        # effects).  Route through /board instead; merge_plan has been in the
        # /board payload since #776/v0.4.53.
        if show_plan:
            _show_plan_from_daemon(_merge_svc, repo_filter=repo_filter, order=order)
            return
        _merge_via_daemon(_merge_svc, {
            "dry_run": dry_run, "order": order,
            "repo_filter": repo_filter, "method": method,
            "force_merge": force_merge, "skip_review": skip_review,
            "skip_smoke": skip_smoke, "revalidate": revalidate,
            "drop": drop_assignment,
            "only": only_assignment,
            "override_human_required": override_human_required,
        })
        return

    # #732: --drop is a surgical single-entry removal; handle before the full
    # merge pipeline so it works even when the queue is otherwise busy/blocked.
    if drop_assignment:
        from coord import merge_queue as _mq  # noqa: PLC0415

        removed = _mq.drop_entry(drop_assignment)
        if removed:
            click.echo(f"merge-queue: dropped entry {drop_assignment}")
        else:
            click.echo(
                f"merge-queue: no entry found for {drop_assignment!r} "
                "(tried assignment_id, repo#issue, issue number, and branch name)",
                err=True,
            )
            sys.exit(1)
        return

    # #779: --plan is a pure read-only path; handle it before the auto-enqueue
    # scan so it never causes side effects.  When a daemon is present this path
    # is short-circuited above by _show_plan_from_daemon (/board, not /merge).
    # This local branch runs on the daemon itself (COORD_MERGE_ON_DAEMON set)
    # or when no daemon is configured (standalone dev environment).
    #
    # #1477-review: the reconcile call just below is a deliberate, narrow
    # carve-out from "never causes side effects" — it persists CONFLICT ->
    # PENDING (clearing entry.error) via save_queue() when the reconciled
    # branch turns out to be clean. That's accepted here because (a) it's a
    # state *correction*, not a merge action — no PR is touched — and (b)
    # daemon-fronted setups (the common case) never reach this branch at all,
    # since --plan is short-circuited above to the read-only /board path.
    # Only a standalone/no-daemon dev environment running --plan directly
    # observes this side effect.
    if show_plan:
        from coord import github_ops as _plan_gh_ops  # noqa: PLC0415
        from coord import merge_queue as _plan_mq  # noqa: PLC0415
        from coord.ci_store import build_ci_store as _build_ci_store  # noqa: PLC0415
        from coord.state import load_board as _load_board  # noqa: PLC0415

        _cfg = _load_config(config_path)
        _board = _load_board()
        _ci = _build_ci_store(
            _cfg.ci_store.type, host=_cfg.ci_store.host, token_env=_cfg.ci_store.token_env
        )

        # #1477: re-test any parked CONFLICT entry against GitHub's own
        # mergeability computation before building the plan — otherwise a
        # branch repaired by a conflict-fix worker (or by hand) keeps
        # showing its stale conflict verdict here indefinitely.  See the
        # #1477-review note above the `if show_plan:` line for why this is a
        # deliberate exception to the "no side effects" contract.
        for _ev in _plan_mq.reconcile_conflict_entries(_plan_gh_ops):
            click.echo(
                f"  {_ev.entry.repo_name} #{_ev.entry.issue_number} "
                f"({_ev.entry.branch}): {_ev.kind} — {_ev.message}"
            )

        planned = _plan_mq.plan(_board, _cfg, _ci, gh_ops=_plan_gh_ops)

        # --repo scoping
        if repo_filter:
            planned = [p for p in planned if p.repo_name == repo_filter]

        # --order: put the named IDs first, then renumber ranks so the display
        # matches what a subsequent `coord merge --order <ids>` would actually do.
        if order:
            _override_ids = [s.strip() for s in order.split(",") if s.strip()]
            _by_id = {p.assignment_id: p for p in planned}
            _head = [_by_id[aid] for aid in _override_ids if aid in _by_id]
            _tail = [p for p in planned if p.assignment_id not in set(_override_ids)]
            planned = _head + _tail
            for _i, _p in enumerate(planned, 1):
                _p.rank = _i

        _print_merge_plan_entries(planned)

        # #920: sibling-overlap warnings, computed live off the same board.
        try:
            _overlaps = _plan_mq.find_sibling_overlaps(_board, _cfg)
        except Exception:  # noqa: BLE001 — never let the warning break --plan
            _overlaps = []
        if repo_filter:
            _overlaps = [w for w in _overlaps if w.repo_name == repo_filter]
        _print_sibling_overlap_warnings(_overlaps)
        return

    from coord import github_ops as gh_ops
    from coord import merge_queue as mq
    from coord.ci_store import build_ci_store
    from coord.merge_queue import CONFLICT, HUMAN_REQUIRED, MERGED, PENDING
    from coord.state import load_board

    # #780: --only is a surgical single-entry merge that leaves all other queue
    # entries in PENDING state.  Handled early — before the full auto-enqueue
    # scan — so a --only run doesn't touch unrelated entries.
    if only_assignment:
        if order:
            click.echo(
                "error: --only and --order are mutually exclusive", err=True
            )
            sys.exit(1)
        cfg_only = _load_config(config_path)
        # #1477: re-test any parked CONFLICT entry before resolving
        # --only — a branch repaired since the last tick should be eligible
        # for --only the moment it's clean, not only after a manual --drop.
        for _ev in mq.reconcile_conflict_entries(gh_ops):
            click.echo(
                f"  {_ev.entry.repo_name} #{_ev.entry.issue_number} "
                f"({_ev.entry.branch}): {_ev.kind} — {_ev.message}"
            )
        only_queue = mq.load_queue()
        only_entry = mq.resolve_entry_key(only_queue, only_assignment)
        if only_entry is None:
            # #1845: `--only` used to fail outright right here even when the
            # addressed row is done, fully gated, and simply hasn't been
            # through the auto-enqueue scan yet — `coord merge` (no --only)
            # runs that scan on every invocation, but the surgical --only
            # path went straight to the queue lookup and never triggered it.
            # A drive's merge stage hits this race routinely: the work
            # finishes moments before the daemon tick's own scan runs, and
            # each `--only` attempt in that window used to burn one of the
            # drive's few merge attempts on a false negative — see the
            # overnight incident in #1845. `enqueue_approved_work` is the
            # SAME scan `coord merge` (no --only) runs; when a matching
            # board row exists and every gate already passes, run it inline
            # and retry the resolution once before reporting failure.
            #
            # #1926: "every gate already passes" is decided by
            # `_board_row_merge_gate_ok`, NOT a direct
            # `mq.merge_gate_failures(a, ...)` call on the raw board
            # `Assignment` — that call silently no-ops every #1479 staleness
            # check (the Assignment has no repo_github/live-SHA fields), so
            # a row with a genuinely STALE verdict used to read as "gated"
            # here and get enqueued (and then merge-attempted) on the same
            # false-green basis the fallback message below was fixed for.
            _retry_board = load_board()
            if _retry_board is not None:
                _retry_rows = mq.resolve_board_work_key(_retry_board, only_assignment)
                _retry_all_gated = bool(_retry_rows) and all(
                    _board_row_merge_gate_ok(a, cfg_only, _retry_board, gh_ops)
                    for a in _retry_rows
                )
                if _retry_all_gated:
                    mq.enqueue_approved_work(cfg_only, _retry_board)
                    only_queue = mq.load_queue()
                    only_entry = mq.resolve_entry_key(only_queue, only_assignment)
        if only_entry is None:
            # #1695: the old message said only "tried assignment_id,
            # repo#issue, issue number, and branch name", which reads as a
            # key-lookup problem and sends the operator hunting for a
            # different identifier. Usually the identifier was fine and a
            # gate was the reason no entry exists — so say which.
            for line in _explain_missing_only_entry(only_assignment, cfg_only, gh_ops):
                click.echo(line, err=True)
            sys.exit(1)
        # #1251: --override-human-required is the explicit, audited escape
        # hatch for an entry an automated conflict-fix (or a permission /
        # branch-protection classification) already gave up on.  It's a
        # different class of override from --skip-smoke/--skip-review/
        # --force-merge — those waive a gate that simply wasn't run; this
        # clears a flag that says "automation gave up, a human must decide" —
        # so it gets its own flag, its own validation, and its own audit
        # row, never bundled into --force-merge.
        if override_human_required:
            if only_entry.state != HUMAN_REQUIRED:
                click.echo(
                    "error: --override-human-required only applies to a "
                    f"HUMAN_REQUIRED entry; {only_assignment!r} is in state "
                    f"{only_entry.state!r}",
                    err=True,
                )
                sys.exit(1)
            if dry_run:
                click.echo(
                    "  --override-human-required: (dry run) would clear "
                    f"HUMAN_REQUIRED on {only_assignment!r} — "
                    f"{override_human_required!r}"
                )
            else:
                from coord.audit import record_audit  # noqa: PLC0415

                record_audit(
                    tier="business",
                    category="merge",
                    event_type="human_required_override",
                    actor="user",
                    summary=(
                        f"human_required override: {only_entry.repo_name}"
                        f"#{only_entry.issue_number} ({only_assignment}) — "
                        f"{override_human_required}"
                    ),
                    repo=only_entry.repo_name,
                    issue=only_entry.issue_number,
                    assignment_id=only_entry.assignment_id,
                    details={"reason": override_human_required},
                )
                click.echo(
                    "  --override-human-required: cleared HUMAN_REQUIRED on "
                    f"{only_assignment!r} — {override_human_required!r} — "
                    "requeued as PENDING"
                )
            # Reset in-memory state either way so the dry-run event stream
            # below reflects what a real run would do (matching the review/
            # smoke gate dry-run convention); actual persistence is still
            # gated on `not dry_run` in the save block further down.
            only_entry.state = PENDING
            only_entry.error = None
        # #2157: MERGED is the one non-PENDING state that is not a failure.
        # An exit code reports whether the CALLER's postcondition holds, and
        # for `--only <aid>` that postcondition is "aid's branch is on the
        # target branch" — which an already-merged entry satisfies outright.
        # Lumping it in with CONFLICT/HUMAN_REQUIRED/DROPPED below cost
        # coord-portal#51 5h47m `blocked`: `coord drive` counted the exit 1
        # against `--max-merge-attempts`, exhausted, and blocked the
        # drive-queue entry for an issue whose acceptance slice had merged 12
        # seconds into the run. Every other non-PENDING state keeps exiting 1
        # with the message unchanged — for those the goal genuinely is unmet.
        if only_entry.state == MERGED:
            pr = f" (PR #{only_entry.pr_number})" if only_entry.pr_number else ""
            click.echo(
                f"merge-queue: entry {only_assignment!r} already merged{pr} "
                "— nothing to do"
            )
            sys.exit(0)
        # #2558: a CONFLICT entry used to dead-end right here — the generic
        # "not PENDING) — cannot merge" refusal below applies equally to
        # HUMAN_REQUIRED/DROPPED, but CONFLICT is different from those: it is
        # the state #241's conflict-fix exists to clear, and nothing else
        # ever gives it a second look (`merge_queue.process()` only acts on
        # PENDING; the reconcile pass above only unparks an entry GitHub
        # already reports mergeable again). Give it the SAME
        # classify-and-dispatch chance the whole-queue path gets for a fresh
        # conflict, then still fall through to the generic refusal below —
        # dispatching a fix does not put the branch on the target branch
        # THIS pass, so the postcondition genuinely isn't met yet.
        if only_entry.state == CONFLICT:
            _verb = "(dry run) would retry" if dry_run else "retrying"
            click.echo(
                f"  {only_entry.repo_name} #{only_entry.issue_number} "
                f"({only_entry.branch}): still CONFLICT — {_verb} #241 "
                "conflict-fix dispatch instead of refusing outright (#2558)"
            )
            _dispatch_conflict_fixes(
                [mq.MergeEvent(only_entry, "conflict", only_entry.error)],
                cfg_only, dry_run=dry_run,
            )
            if not dry_run:
                all_items_only = mq.load_queue()
                by_id_only = {only_entry.assignment_id: only_entry}
                merged_only = [
                    by_id_only.get(x.assignment_id, x) for x in all_items_only
                ]
                mq.save_queue(merged_only)
        if only_entry.state != PENDING:
            click.echo(
                f"merge-queue: entry {only_assignment!r} is in state "
                f"{only_entry.state!r} (not PENDING) — cannot merge",
                err=True,
            )
            sys.exit(1)
        # #821: never pass None to process() — use an empty board so
        # has_smoke_verdict can apply its "no work found → fail open" rule.
        # process() blocks on board=None when a gate IS required; an empty
        # board lets the gate function decide.
        from coord.models import Board as _Board  # noqa: PLC0415
        _raw_board_only = load_board()
        board_only = _raw_board_only if _raw_board_only is not None else _Board(active=[], completed=[])
        ci_store_only = build_ci_store(
            cfg_only.ci_store.type,
            host=cfg_only.ci_store.host,
            token_env=cfg_only.ci_store.token_env,
        )
        # #2809 review: `only_entry` came straight off the queue DB
        # (`resolve_entry_key`) and may have been live-anchored on a much
        # earlier tick (or never, e.g. a manually-inserted entry) — its
        # `branch_head_probe_error` can be stale or unset. Re-anchor against
        # the CURRENT GitHub state so the review-gate report line below can
        # distinguish "GitHub confirmed unknown" (enriched status/request-id/
        # retry-after) from "never probed" (the generic fallback string) —
        # this is the exact `coord merge --only` invocation the issue's own
        # reproduction names for the review gate specifically.
        mq.live_anchor_entry(only_entry, gh_ops)
        # #1695: name the blocking gate(s) up front. Under #1695 a gate-blocked
        # row IS enqueued (visibly BLOCKED) instead of being dropped, so
        # `--only` now resolves it — and the operator needs to be told, before
        # process() runs, exactly which gate stands between them and the merge
        # and which flag waives it. process() still enforces every gate below;
        # this is a report, not a decision.
        _only_gate_failures = mq.merge_gate_failures(
            only_entry, cfg_only, board_only, gh_ops,
        )
        for _gf in _only_gate_failures:
            # NB: --force-merge is deliberately absent here — it waives the CI
            # gate only (merge_queue.process), never review or smoke.
            _waived = (
                (_gf.gate == "review" and skip_review)
                or (_gf.gate == "smoke" and skip_smoke)
            )
            _status = "waived by this run" if _waived else "will block this merge"
            click.echo(f"  gate {_gf.gate}: {_gf.reason} — {_status}")
        if skip_review:
            click.echo("  --skip-review: review-approval gate bypassed (#253)")
        if skip_smoke:
            click.echo("  --skip-smoke: interactive smoke-test gate bypassed (#465)")
        only_items = [only_entry]
        # #1769: re-test a stale-but-passed verdict against the current base
        # before the gate runs. Opt-in only; `--skip-smoke` is unaffected (it
        # waives the gate, this satisfies it), and a failing re-test leaves the
        # entry blocked exactly as it was.
        if revalidate and not skip_smoke:
            board_only = _apply_revalidation(
                only_items, board_only, cfg_only, gh_ops, dry_run=dry_run,
                skip_review=skip_review,
            )
        # #1851: CI staleness is a separate gate from the local smoke
        # verdict — always run under --revalidate, independent of
        # --skip-smoke (which waives the smoke gate specifically, not CI).
        deferred_ci: set[str] = set()
        if revalidate:
            deferred_ci = _apply_ci_revalidation(
                only_items, board_only, cfg_only, ci_store_only, gh_ops,
                dry_run=dry_run,
            )
            # #2143: the CI-settle wait just above can run for minutes —
            # re-read the board so the gates `process()` runs below see
            # whatever landed on GitHub during it, not the pre-wait snapshot.
            board_only = _reload_board_after_wait(board_only, dry_run=dry_run)
        # #1925: an entry deferred by the CI-settle wait above must not go
        # through process() this pass — that would immediately re-derive the
        # exact self-triggered "unknown" reading the wait was just trying to
        # avoid handing to the gate. It stays PENDING with the explanatory
        # `entry.error` _apply_ci_revalidation already set.
        if only_entry.assignment_id in deferred_ci:
            events_only = []
        else:
            events_only = mq.process(
                only_items, gh_ops,
                method=method, dry_run=dry_run, presorted=True,
                ci_store=ci_store_only, force_merge=force_merge,
                config=cfg_only, board=board_only,
                skip_review=skip_review, skip_smoke=skip_smoke,
            )
        for ev in events_only:
            e = ev.entry
            prefix = f"  {e.repo_name} #{e.issue_number} ({e.branch})"
            click.echo(f"{prefix}: {ev.kind} — {ev.message}")
        # #1474 review: --only used to return here without ever classifying
        # a fresh conflict — see _dispatch_conflict_fixes's docstring. Run it
        # before the save below so a retry-cap/non-rebaseable HUMAN_REQUIRED
        # mutation on only_entry.state is persisted, not lost.
        _dispatch_conflict_fixes(events_only, cfg_only, dry_run=dry_run)
        # #2510: same reasoning, for a CONFIRMED checks_failed event — dispatch
        # a bounded ci-fix worker or escalate to HUMAN_REQUIRED before the
        # save below, so the mutation on only_entry.state is persisted too.
        _dispatch_ci_fixes(events_only, cfg_only, ci_store_only, dry_run=dry_run)
        # #2246: `--only` merges one entry, but the branch it just moved is
        # shared — every OTHER queued PR based on it may have become
        # CONFLICTING a second ago. This is the drive-queue's path (`coord
        # drive` and the TUI both merge via `--only`), so it is exactly where
        # the 2026-08-14 collisions were minted. The sweep persists any
        # sibling it parks itself: the save below deliberately writes back
        # only `only_entry`.
        _sweep_sibling_conflicts(
            events_only, only_items, cfg_only, gh_ops, dry_run=dry_run,
        )
        if not dry_run:
            # Save only the modified entry back; all other entries are untouched.
            all_items_only = mq.load_queue()
            by_id_only = {only_entry.assignment_id: only_entry}
            merged_only = [by_id_only.get(x.assignment_id, x) for x in all_items_only]
            mq.save_queue(merged_only)
        click.echo("")
        click.echo(
            "Summary (--only): "
            + ", ".join(f"{k}={v}" for k, v in sorted(
                {x.state: 1 for x in only_items}.items()
            ))
        )
        return

    cfg = _load_config(config_path)

    # #242: Before processing, scan board.completed for done work assignments
    # that should be queued but aren't.  Without this, `coord merge` silently
    # no-ops when a work assignment reached "done" via a path that didn't
    # also trigger the `coord status` enqueue hook (restart, notify-driven
    # mark_done, etc.).  enqueue() is idempotent — by assignment_id — so this
    # is safe to call on every invocation.
    #
    # Filter on issue.state == 'open': a closed issue was almost certainly
    # already merged externally (or won't-fix'd) and re-attempting a merge
    # for it would open spurious PRs against branches that may not even
    # exist anymore.  When the issues table has no row for an issue (cache
    # miss), default to OPEN — that matches the prior coord status enqueue
    # path which had no such check.
    # #821: never pass None to process() — use an empty board so
    # has_smoke_verdict can apply its "no work found → fail open" rule.
    # process() blocks on board=None when a gate IS required; an empty
    # board lets the gate function decide.
    from coord.models import Board as _Board  # noqa: PLC0415
    _raw_board = load_board()
    board = _raw_board if _raw_board is not None else _Board(active=[], completed=[])
    open_by_repo, known_by_repo = _load_issue_states()

    auto_enqueued: list[str] = []
    # #2597-review: assignments whose enqueue write is still locked after
    # `retry_on_locked` exhausts its budget. Collected here instead of
    # raised in place — one contended assignment must not abort the scan
    # for every other assignment still waiting in this batch (that's the
    # exact #1353 regression this file already fixed once, resurrected for
    # lock contention specifically). The whole run still fails loudly: once
    # every assignment has had its turn, a non-zero exit is raised below.
    # #2784: BaseException, not sqlite3.OperationalError — the driver error
    # this collects is whichever one sql.driver_errors() catches below,
    # which is Postgres-shaped on a psycopg connection.
    lock_contention_failures: list[tuple[str, BaseException]] = []
    # Per-repo cache of branches that still exist on origin.  Lets us skip
    # re-enqueuing done-work whose branch was already merged-and-deleted — the
    # dominant merge-queue clog source.  A done assignment for a closed issue
    # often isn't in the open-only issues cache, so the issue-state filter
    # above misses it; branch-existence catches every merge path (coord merge,
    # gh pr merge, manual) uniformly.  Fail OPEN on lookup failure.
    from coord import github_ops as _gho
    branch_cache: dict[str, set[str]] = {}
    # #525: per-run cache for work_is_terminal; shared across the whole
    # auto-enqueue loop so one gh round-trip covers every repeated
    # (repo, issue, branch) triple.
    terminal_cache: dict = {}
    # #934: per-run cache for the issue -> milestone-number lookup, mirroring
    # terminal_cache above.
    milestone_cache: dict = {}
    if board is not None:
        # #1490: resolve every branch to a single winning work-like row
        # before any of the filters below run. A fix/bounce cycle piles up
        # more than one WORK_LIKE_TYPES row on the same branch (the
        # original dispatch plus every retry), and processing each
        # independently used to call refresh_entry_assignment once per row
        # — re-keying the branch's one queue entry, and re-printing
        # "auto-enqueued", every single time, forever (the exact symptom
        # #1490 reports: one branch, three identical announcements, on
        # every `coord merge` pass). Superseded rows are reported once here
        # and never reach refresh_entry_assignment at all.
        scoped_completed = [
            a for a in board.completed
            if not repo_filter or a.repo_name == repo_filter
        ]
        for a, superseded in mq.group_branch_candidates(scoped_completed):
            for row in superseded:
                auto_enqueued.append(
                    f"  superseded: {row.repo_name} #{row.issue_number} "
                    f"(assignment {row.assignment_id}, branch {row.branch}) — "
                    "not the winning row for this branch, skipped (#1490)"
                )
            repo_cfg = cfg.repo(a.repo_name)
            if repo_cfg is None:
                continue
            # #1353: isolate the rest of this assignment's scan — a bad
            # `gh` round-trip (e.g. empty stdout on exit 0, see
            # github_ops._gh) or a transient decode failure anywhere below
            # used to raise straight out of this loop and abort auto-enqueue
            # for every *other* assignment in the batch too, with the CLI
            # printing nothing before dying. One assignment misbehaving is
            # not a reason to skip the whole drain — catch it, report which
            # assignment and why, and keep scanning the rest.
            try:
                # Issue-state filter: skip closed issues (probably merged
                # elsewhere).  We deny only when the cache has explicit
                # evidence the issue is closed — i.e. there's a row for this
                # (repo, number) and its state isn't 'open'.  If the cache
                # simply has no row for this issue (e.g. it was created
                # after the last sync), treat as unknown and allow — denying
                # on cache miss silently skipped post-sync issues
                # (#278/#280 hit this).
                #
                # #2639: gated on trust_issue_closed_for(a.type) — a
                # test-author/mock-author row's `issue_number` is the
                # milestone's tracking issue, not its own deliverable, so a
                # closed tracking epic (closed for most of a milestone's
                # life) must not silently drop it out of auto-enqueue here.
                # The branch-existence and work_is_terminal (#525) checks
                # right below already answer "is THIS row's own work landed"
                # correctly for these types.
                known_issues = known_by_repo.get(a.repo_name, set())
                open_issues = open_by_repo.get(a.repo_name, set())
                if (
                    trust_issue_closed_for(getattr(a, "type", None))
                    and a.issue_number in known_issues
                    and a.issue_number not in open_issues
                ):
                    continue
                # Skip work whose branch no longer exists on origin (already
                # merged + deleted).  Fail OPEN: only skip when we got a real
                # (non-empty) branch list back and the branch isn't in it.
                origin_branches = branch_cache.get(a.repo_name)
                if origin_branches is None:
                    origin_branches = _gho.list_remote_branch_names(repo_cfg.github)
                    branch_cache[a.repo_name] = origin_branches
                if origin_branches and a.branch not in origin_branches:
                    continue
                # #525: never enqueue work that is already done on GitHub —
                # issue closed OR PR merged.  Mirrors the #522 guard in
                # review.dispatch_review.  Fail OPEN: a transient gh error
                # must never block a real enqueue.
                #
                # #2639: trust_issue_closed_for(a.type) — see the
                # issue-state filter above for the same rationale.
                if _gho.work_is_terminal(
                    repo_cfg.github, a.issue_number, a.branch,
                    cache=terminal_cache,
                    trust_issue_closed=trust_issue_closed_for(getattr(a, "type", None)),
                ):
                    continue
                # #946: review + smoke gates, via the shared predicate — this
                # loop was the primary ungated enqueue path (#782/#795 reached
                # the merge queue with a failed test / no review at all).
                #
                # #1695: the gate no longer *drops* the row here. Blocking at
                # enqueue time made `coord merge --skip-review` structurally
                # unreachable — the flag waives the gate for an entry that
                # already exists, but an un-approved row could never become
                # an entry, so `--only` had nothing to address and the silent
                # `continue` printed nothing about why. The row is now
                # enqueued in a visibly BLOCKED state (it will render as
                # BLOCKED in `--plan`/`--dry-run` via `_entry_gate_status`,
                # and `--only` can name it), and the gate is enforced where
                # its override lives: `process()` still refuses to merge it
                # unless `--skip-review`/`--skip-smoke` is given. Enqueueing
                # changes visibility, never eligibility — auto-drain only
                # ever touches PLAN_READY entries, and a blocked entry is
                # PLAN_BLOCKED, so this is safe with `merge.auto_drain: true`.
                #
                # #934: target `feature/ms-NN` when this issue belongs to a
                # milestone and the repo opted into the git model — the
                # milestone lookup itself is skipped (no `gh` call) when it
                # hasn't, falling back to `default_branch` unchanged.
                # #2085: resolved BEFORE the gate check now (it used to run
                # after) — `mq.live_gate_entry` below needs a target_branch
                # to populate the #821/#1479 freshness anchors live.
                target_branch = repo_cfg.default_branch
                if getattr(repo_cfg, "develop_branch", None):
                    from coord.branch_model import (  # noqa: PLC0415
                        fetch_issue_milestone_number,
                        resolve_base_branch,
                    )

                    milestone_number = fetch_issue_milestone_number(
                        repo_cfg.github, a.issue_number, cache=milestone_cache,
                    )
                    target_branch = resolve_base_branch(repo_cfg, milestone_number)

                # #2085: `a` is a raw board Assignment — no `branch_head_sha`/
                # `repo_github`/`target_branch` attribute, so handing it
                # straight to `merge_gate_failures` made the #821
                # SHA-freshness check inside `has_approved_review`
                # permanently unconfirmable (fails closed on every review
                # carrying a real `review_head_sha`, i.e. virtually every
                # modern approval) — this scan printed "BLOCKED: review
                # required but not approved" for ordinary fresh approvals on
                # every single invocation. `mq.live_gate_entry` builds the
                # same live-anchored synthetic entry
                # `coord.gates.build_gate_report` uses, backed by `_gho`
                # (already available in this loop for the terminal-state
                # check above), so a genuinely fresh approval reads BLOCKED
                # only when it actually is.
                gate_entry = mq.live_gate_entry(a, repo_cfg.github, target_branch, _gho)
                gate_failures = mq.merge_gate_failures(gate_entry, cfg, board, _gho)
                gate_note = ""
                if gate_failures:
                    gate_note = (
                        " — BLOCKED: "
                        + mq.describe_merge_gate_failures(gate_failures)
                    )
                # #736 / #292: use refresh_entry_assignment (not bare
                # enqueue) so an existing PENDING entry is re-keyed to the
                # latest fix assignment when the original assignment_id no
                # longer matches.  Dedup by (repo_github, branch) is
                # preserved — refresh_entry_assignment is a no-op when the
                # entry is already correctly keyed.
                # #2597: wrapped in retry_on_locked — this is a real DB write
                # (merge_queue.save_queue), and unlike the gh-round-trip
                # failures this try/except otherwise isolates (#1353), a
                # `database is locked` collision here is pure transient
                # contention with a concurrent writer, not a reason to give
                # up on this assignment.
                if retry_on_locked(lambda: mq.refresh_entry_assignment(
                    a,
                    repo_github=repo_cfg.github,
                    target_branch=target_branch,
                )):
                    auto_enqueued.append(
                        f"  auto-enqueued: {a.repo_name} #{a.issue_number} "
                        f"({a.branch} → {target_branch}){gate_note}"
                    )
                elif gate_failures:
                    # Already queued and still blocked. Re-state it every pass
                    # rather than once at creation: a blocked entry is exactly
                    # the row the operator is looking for, and #1695's whole
                    # complaint is that this state was invisible. Emitted at
                    # most once per branch — `group_branch_candidates` has
                    # already collapsed the fix/bounce rows (#1490).
                    auto_enqueued.append(
                        f"  blocked: {a.repo_name} #{a.issue_number} "
                        f"(assignment {a.assignment_id}, {a.branch} → "
                        f"{target_branch}) — "
                        f"{mq.describe_merge_gate_failures(gate_failures)}"
                    )
            except sql.driver_errors() as exc:  # #2784: was sqlite3.OperationalError only
                if is_lock_contention_error(exc):
                    # #2597: `retry_on_locked` above already gave the write
                    # several backed-off attempts — reaching here means the
                    # DB is still locked after that budget, i.e. sustained
                    # contention, not a momentary collision. Dropping a
                    # mergeable assignment out of the scan on that basis is
                    # the exact correctness bug #2597 reported (a skipped
                    # entry here is silent — nothing re-scans it until the
                    # next `coord merge` invocation). This is a hard
                    # failure, not the soft "skipped" outcome below — but
                    # (per review) it must not abort the rest of the batch
                    # either. Record it and keep scanning; a clearly-flagged
                    # line goes into the summary and the run still fails
                    # loudly (non-zero exit, real exception) once every
                    # other assignment has had its turn.
                    lock_contention_failures.append((a.assignment_id, exc))
                    auto_enqueued.append(
                        f"  LOCK CONTENTION: {a.repo_name} #{a.issue_number} "
                        f"(assignment {a.assignment_id}) — auto-enqueue write "
                        f"still locked after exhausting retries: {exc!r}"
                    )
                    continue
                auto_enqueued.append(
                    f"  skipped: {a.repo_name} #{a.issue_number} "
                    f"(assignment {a.assignment_id}) — auto-enqueue scan "
                    f"failed, skipping this assignment: {exc!r}"
                )
            except Exception as e:  # noqa: BLE001
                auto_enqueued.append(
                    f"  skipped: {a.repo_name} #{a.issue_number} "
                    f"(assignment {a.assignment_id}) — auto-enqueue scan "
                    f"failed, skipping this assignment: {e!r}"
                )
    for line in auto_enqueued:
        click.echo(line)

    if lock_contention_failures:
        # #2597-review: every assignment in the batch has now had its turn
        # — including the ones after the first contended write, and their
        # results (if any) are already printed above. Surface the
        # contention loudly now — propagating out of this command (not a
        # swallowed summary line) so `coord merge` exits non-zero and the
        # failure is impossible to miss, same as any other genuinely
        # unrecoverable write failure.
        #
        # #2784: this used to fabricate a bare `sqlite3.OperationalError(...)`
        # regardless of which driver actually raised the per-assignment
        # errors being summarized — a lie about origin on a Postgres
        # deployment, and a driver-named exception outside coord/sql.py that
        # the #2768 ratchet now forbids. `LockContentionExhaustedError` is
        # coord-owned and dialect-agnostic; the actual driver error survives
        # as `__cause__` below for anyone inspecting the traceback.
        assignment_ids = ", ".join(aid for aid, _exc in lock_contention_failures)
        raise LockContentionExhaustedError(
            "database is locked — auto-enqueue write(s) still locked after "
            f"exhausting retries for assignment(s): {assignment_ids}"
        ) from lock_contention_failures[-1][1]

    # #1477: re-test any parked CONFLICT entry against GitHub's own
    # mergeability computation before the pending scan below — a branch
    # repaired by a conflict-fix worker (or by hand) since the last tick
    # must clear itself here, not require a manual --drop + re-enqueue +
    # --only. Runs unconditionally (even under --dry-run): it's a state
    # correction, not a merge action, same posture as the auto-enqueue scan
    # above. #1477-review: unlike every other --dry-run line in this
    # command, this one really does persist (it corrects previously-cached
    # state rather than proposing a merge action), so it's called out
    # explicitly here rather than left to blend in with the "(dry run)
    # would ..." lines below.
    for ev in mq.reconcile_conflict_entries(gh_ops):
        e = ev.entry
        suffix = " (reconciled — persisted even under --dry-run)" if dry_run else ""
        click.echo(
            f"  {e.repo_name} #{e.issue_number} ({e.branch}): {ev.kind} — {ev.message}{suffix}"
        )

    items = mq.load_queue()
    if repo_filter:
        items = [x for x in items if x.repo_name == repo_filter]
    if not items:
        # Distinguish "nothing in the queue" from "nothing to do because
        # there's no completed work to merge" — the latter is the common
        # case before #242 was fixed and was the silent-fail symptom.
        if board is not None and any(
            a.type in WORK_LIKE_TYPES and a.status == "done" and a.branch
            for a in board.completed
            if (not repo_filter or a.repo_name == repo_filter)
        ):
            click.echo("Merge queue is empty (all done-work is already merged or has no branch).")
        else:
            click.echo("Merge queue is empty (no completed work to merge).")
        return

    presorted = False
    if order:
        ids = [s.strip() for s in order.split(",") if s.strip()]
        items = mq.reorder(items, ids)
        presorted = True

    pending = [x for x in items if x.state == PENDING]

    # #2558: a CONFLICT row is a dead end everywhere else in this command.
    # #241's conflict-fix is normally dispatched off a FRESH transition into
    # CONFLICT — process() below, the sibling sweep, --revalidate — because
    # all three build their dispatch event from the transition itself. An
    # entry that transitioned on some EARLIER tick and is still sitting
    # there (no machine was free at the time, the fix is still running, or
    # dispatch was simply declined) never reappears in a fresh `events`
    # list, so nothing ever asks again — see the #2558 issue's coord-portal
    # #131 repro, wedged this way with zero conflict-fix assignments ever
    # dispatched. Give every standing CONFLICT row a fresh chance on EVERY
    # invocation, before the terminal-state early-return below — a queue
    # that is 100% terminal (#131's exact shape) is exactly the case that
    # return skips.
    standing_conflicts = [x for x in items if x.state == CONFLICT]
    if standing_conflicts:
        _verb = "(dry run) would retry" if dry_run else "retrying"
        for x in standing_conflicts:
            click.echo(
                f"  {x.repo_name} #{x.issue_number} ({x.branch}): still "
                f"CONFLICT — {_verb} #241 conflict-fix dispatch (#2558)"
            )
        _dispatch_conflict_fixes(
            [mq.MergeEvent(x, "conflict", x.error) for x in standing_conflicts],
            cfg, dry_run=dry_run,
        )

    if not pending:
        if not dry_run and standing_conflicts:
            # process() never runs this pass (nothing is PENDING), so its
            # own save-queue step below is unreached — persist whatever the
            # redispatch above mutated (e.g. a retry-cap escalation to
            # HUMAN_REQUIRED) before returning, same convention as the
            # --only path above.
            all_items = mq.load_queue()
            by_id = {x.assignment_id: x for x in items}
            merged = [by_id.get(x.assignment_id, x) for x in all_items]
            mq.save_queue(merged)
        # Still surface terminal states so the user knows what happened.
        for x in items:
            click.echo(f"  [{x.state}] {x.repo_name} #{x.issue_number} ({x.branch})")
        return

    ci_store = build_ci_store(
        cfg.ci_store.type, host=cfg.ci_store.host, token_env=cfg.ci_store.token_env
    )
    if skip_review:
        click.echo("  --skip-review: review-approval gate bypassed (#253)")
    if skip_smoke:
        click.echo("  --skip-smoke: interactive smoke-test gate bypassed (#465)")
    # #1769: the merge lane's stale-verdict resolution. Off unless the operator
    # asked for it — with no `--revalidate` this block does not run at all and
    # `coord merge` behaves byte-identically to before.
    if revalidate and not skip_smoke:
        board = _apply_revalidation(
            pending, board, cfg, gh_ops, dry_run=dry_run, skip_review=skip_review,
        )
    # #1851: CI staleness is a separate gate from the local smoke verdict —
    # always run under --revalidate, independent of --skip-smoke (which
    # waives the smoke gate specifically, not CI).
    deferred_ci: set[str] = set()
    if revalidate:
        deferred_ci = _apply_ci_revalidation(
            pending, board, cfg, ci_store, gh_ops, dry_run=dry_run,
        )
        # #2143: the CI-settle wait just above can run for minutes —
        # re-read the board so the gates `process()` runs below see
        # whatever landed on GitHub during it, not the pre-wait snapshot.
        board = _reload_board_after_wait(board, dry_run=dry_run)
    # #1925: entries the CI-settle wait above gave up on while still only
    # seeing the registration-gap symptom must not go through process() this
    # pass — see `_apply_ci_revalidation`'s docstring. They keep their
    # PENDING state and the explanatory `entry.error` it already set; `items`
    # (unfiltered) still carries them through to the save step below.
    process_items = (
        [x for x in items if x.assignment_id not in deferred_ci]
        if deferred_ci else items
    )
    events = mq.process(
        process_items, gh_ops,
        method=method, dry_run=dry_run, presorted=presorted,
        ci_store=ci_store, force_merge=force_merge,
        config=cfg, board=board, skip_review=skip_review, skip_smoke=skip_smoke,
    )

    for ev in events:
        e = ev.entry
        prefix = f"  {e.repo_name} #{e.issue_number} ({e.branch})"
        click.echo(f"{prefix}: {ev.kind} — {ev.message}")

    # #241: classify any conflict events and dispatch a conflict-fix worker
    # for the eligible ones (extracted to _dispatch_conflict_fixes, #1474
    # review, so the --only path below can share it).
    _dispatch_conflict_fixes(events, cfg, dry_run=dry_run)

    # #2510: classify any CONFIRMED checks_failed events and dispatch a
    # bounded ci-fix worker for the eligible ones, escalating to
    # HUMAN_REQUIRED once the retry cap is spent.
    _dispatch_ci_fixes(events, cfg, ci_store, dry_run=dry_run)

    # #2246: whatever just landed may have invalidated its siblings — ask
    # GitHub now, while the merge that caused it is still the obvious
    # explanation, rather than letting the next drive attempt discover it as
    # some unrelated gate failure. Entries it parks are in `items`, so the
    # save below carries the mutation through naturally.
    _sweep_sibling_conflicts(events, items, cfg, gh_ops, dry_run=dry_run)

    # Save state only when we actually moved
    if not dry_run:
        # Persist the updated entries by merging back over the on-disk queue.
        all_items = mq.load_queue()
        by_id = {x.assignment_id: x for x in items}
        merged = [by_id.get(x.assignment_id, x) for x in all_items]
        mq.save_queue(merged)

    # Summary
    states: dict[str, int] = {}
    for x in items:
        states[x.state] = states.get(x.state, 0) + 1
    click.echo("")
    click.echo(
        "Summary: "
        + ", ".join(f"{k}={v}" for k, v in sorted(states.items()))
    )
    if states.get(CONFLICT):
        click.echo("note: at least one PR has a conflict — resolve manually, then re-run.")