"""#3922: ``EntryTruth`` — the single, reconciled view of one ``(repo,
issue)`` pipeline entry, derived from LIVE facts rather than a persisted
conclusion some other actor already wrote and nobody re-checked.

The incidents this answers (quadraui#2173, quadraui#2160, quadraui#2256,
vimcode#2140, claude-coordinator#3917) share one shape: a stored conclusion
(``merge_queue.error``, a terminal ``test_state``, a drive-queue dependency
``state``) stayed put after a DIFFERENT actor changed the reality it once
described — a conflict-fix push, a merge done outside the drive, a second
verdict, a worker's own report — so the pipeline kept acting on the stale
conclusion instead of the new reality.

:func:`reconcile_entry` is the one place that derives every gate from live
facts:

- branch head + patch-id — a LIVE probe via *gh_ops* (fail-closed
  ``None``/"unknown" on any probe failure, same posture as #2704's
  ``UNKNOWN_BRANCH_HEAD_REASON``, never silently reused from a stale value).
- review/test — reuses :func:`coord.gates.build_gate_report`'s own
  :class:`~coord.gates.GateDecision` objects rather than re-implementing the
  #1479 freshness math a second time (that module's own stated reason for
  doing the same against a second, driftable reimplementation, #2096).
- merge — PR lifecycle state (open/merged/closed), live mergeability, and a
  CI summary, read fresh on every call; never the persisted ``merge_queue``
  row's ``state``/``error`` (those stay exactly what #1 below says: display
  only).
- issue state (open/closed) and the latest worker outcome (including the
  #3923 ADVISORY-"already implemented" classification, which already lives
  on ``Assignment.status`` at write time — nothing new to derive there).
- the next action the facts above call for.

Rules this module itself must keep (see the issue for the full list):

1. Persisted strings such as ``merge_queue.error``/``last_reason`` are
   DISPLAY ONLY here — ``next_action`` never branches on them, only on the
   live facts gathered above.
2. This is additive: it does not yet replace what ``coord.drive``'s
   ``_decide_merge``, ``coord.merge_queue.process()``, drive-queue dependency
   resolution, or the board doctor compute internally — wiring each of those
   onto :class:`EntryTruth` is follow-up work (see the issue's own framing:
   "the single place they all read from" for the sibling instance-fixes),
   scoped per-consumer so a regression in one does not risk the other three.
   Today, only ``coord gates`` (:mod:`coord.commands.gates`) consumes it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from coord.gates import GateDecision, build_gate_report, gate_columns_for_issue

if TYPE_CHECKING:  # avoid import cycles / heavy imports at module load
    from coord.config import Config
    from coord.ci_store import CiStore
    from coord.merge_queue import GhOps
    from coord.models import Board

# #2234/#3164 (see coord.dao.TERMINAL_STATUSES's own comment): the two
# terminal `Assignment.status` values a worker leg ends in when it refused
# to do the requested work rather than failing at it. Inlined as literals
# here (mirroring dao.py's own convention) rather than importing coord.agent
# — a large module this one has no other reason to pull in — for the two
# string constants it defines: `coord.agent.REFUSED_POLICY`/`REFUSED_PREMISE`.
_REFUSED_POLICY = "refused_policy"
_REFUSED_PREMISE = "refused_premise"

# next_action vocabulary. "unknown" is deliberately distinct from any of
# these — it means the live facts needed to decide at all could not be
# gathered (no *gh_ops*, or every probe failed), never a default guess.
NEXT_ACTION_DISPATCH_REVIEW = "dispatch_review"
NEXT_ACTION_DISPATCH_TEST = "dispatch_test"
NEXT_ACTION_DISPATCH_CI_FIX = "dispatch_ci_fix"
NEXT_ACTION_MERGE = "merge"
NEXT_ACTION_CONFLICT_FIX = "conflict_fix"
NEXT_ACTION_MARK_DONE = "mark_done"
NEXT_ACTION_ESCALATE = "escalate"
NEXT_ACTION_WAIT = "wait"
NEXT_ACTION_UNKNOWN = "unknown"


@dataclass
class EntryTruth:
    """The reconciled, live-derived truth for one ``(repo_name,
    issue_number)`` pipeline entry. See the module docstring for the
    derivation rules.
    """

    repo_name: str
    issue_number: int

    branch: str | None = None
    target_branch: str | None = None

    # Live probes (#2704 fail-closed posture): `None` means "could not be
    # read live" — either no `gh_ops` was supplied, or the probe itself
    # failed. Never populated from a cached/persisted value.
    head_sha: str | None = None
    patch_id: str | None = None
    # True only when a `gh_ops` was actually supplied AND a branch existed
    # to probe — i.e. `head_sha is None` is a genuine LIVE PROBE FAILURE
    # (#2704), not merely "nobody asked". Distinguishes `next_action`
    # ESCALATE (a probe failed; cannot safely decide) from UNKNOWN (no live
    # access was requested at all, e.g. a `gh_ops=None` diagnostic read).
    head_probe_attempted: bool = False

    # The live review/test/other-registry-gate decisions, straight from
    # `coord.gates.build_gate_report` — never re-derived here.
    review: GateDecision | None = None
    test: GateDecision | None = None
    other_gates: list[GateDecision] = field(default_factory=list)

    # Merge-side live facts. `pr_state` is one of "open"/"merged"/"closed"/
    # "unknown" (no PR found, or no live access to ask).
    pr_state: str = "unknown"
    pr_number: int | None = None
    pr_mergeable: bool | None = None
    # "green" (required checks exist and all pass) / "red" (one failed) /
    # "pending" (one still running) / "none" (no checks reported, repo has
    # no CI gate wired) / "unknown" (no *ci_store*, or the read failed).
    ci_state: str = "unknown"

    # "open" / "closed" / "unknown" (read failed, or no `gh_ops`).
    issue_state: str = "unknown"

    # The most recent work-like assignment's own outcome — `Assignment.
    # status` is itself a live write (set once by the worker/reaper that
    # terminated it, #3923's ADVISORY-"already implemented" classification
    # included), so this is a direct read, not a derivation.
    latest_worker_status: str | None = None
    latest_worker_assignment_id: str | None = None

    # Passthrough, for display parity with `coord gates` (#252/#3922's own
    # "what an operator sees is what the merge gate enforces" requirement).
    notes: list[str] = field(default_factory=list)

    next_action: str = NEXT_ACTION_UNKNOWN
    next_action_reason: str = "reconcile_entry has not run yet"


def _live_ci_state(
    ci_store: "CiStore | None", repo_github: str | None, pr_number: int | None,
) -> str:
    if ci_store is None or not repo_github or not pr_number:
        return "unknown"
    try:
        checks = ci_store.list_checks_for_pr(repo_github, pr_number)
    except Exception:  # noqa: BLE001 — fail closed: an erroring read is unknown, not green
        return "unknown"
    if not checks:
        return "none"
    from coord.ci_store import failed_checks, in_flight_checks  # noqa: PLC0415

    if failed_checks(checks):
        return "red"
    if in_flight_checks(checks):
        return "pending"
    return "green"


def _live_pr_facts(
    gh_ops: "GhOps | None", repo_github: str | None, branch: str | None,
) -> tuple[str, int | None, bool | None]:
    """Return ``(pr_state, pr_number, pr_mergeable)`` from live `gh_ops`
    reads only — never from a persisted merge-queue/board column.

    ``getattr(..., None)`` on each optional method mirrors
    `coord.gates._ci_expects_checks`'s own fail-closed posture toward a
    duck-typed `gh_ops` stand-in that predates one of these capabilities:
    a stand-in that hasn't been taught to answer reads "unknown" for that
    one fact rather than raising.
    """
    if gh_ops is None or not repo_github or not branch:
        return "unknown", None, None

    find_pr_for_branch = getattr(gh_ops, "find_pr_for_branch", None)
    pr: dict | None = None
    if find_pr_for_branch is not None:
        try:
            pr = find_pr_for_branch(repo_github, branch)
        except Exception:  # noqa: BLE001 — fail closed to "couldn't tell"
            pr = None
    if pr is not None:
        pr_number = pr.get("number")
        mergeable = pr.get("mergeable")
        pr_mergeable: bool | None
        if isinstance(mergeable, bool):
            pr_mergeable = mergeable
        else:
            check_pr_mergeable = getattr(gh_ops, "check_pr_mergeable", None)
            pr_mergeable = None
            if check_pr_mergeable is not None and pr_number is not None:
                try:
                    pr_mergeable = check_pr_mergeable(repo_github, pr_number)
                except Exception:  # noqa: BLE001
                    pr_mergeable = None
        return "open", pr_number, pr_mergeable

    pr_is_merged = getattr(gh_ops, "pr_is_merged", None)
    if pr_is_merged is not None:
        try:
            if pr_is_merged(repo_github, branch):
                return "merged", None, None
        except Exception:  # noqa: BLE001
            pass

    # No open PR, not merged: either a PR was closed without merging, or
    # none was ever opened. `GhOps` has no third call that tells those
    # apart, so this collapses to "closed" — see the dataclass field's own
    # docstring for the same caveat.
    return "closed", None, None


def _derive_next_action(truth: EntryTruth) -> tuple[str, str]:
    """Rule 1: branches ONLY on the live facts already gathered onto
    *truth* — never on a persisted string like `merge_queue.error`."""
    if truth.issue_state == "closed":
        return NEXT_ACTION_MARK_DONE, "issue is already closed"
    if truth.pr_state == "merged":
        return NEXT_ACTION_MARK_DONE, "PR merged; issue should close"
    if truth.latest_worker_status in (_REFUSED_PREMISE, _REFUSED_POLICY):
        return (
            NEXT_ACTION_ESCALATE,
            f"latest worker leg exited {truth.latest_worker_status!r} — needs "
            "operator recovery, not another dispatch",
        )
    if truth.head_sha is None:
        if truth.head_probe_attempted:
            return (
                NEXT_ACTION_ESCALATE,
                "branch head could not be read live (#2704) — fail closed, "
                "cannot safely decide the next action",
            )
        return NEXT_ACTION_UNKNOWN, "no live gh access requested — cannot determine next action"
    if truth.review is not None and truth.review.required and not truth.review.ok:
        return NEXT_ACTION_DISPATCH_REVIEW, truth.review.reason or "review required"
    if truth.test is not None and truth.test.required and not truth.test.ok:
        return NEXT_ACTION_DISPATCH_TEST, truth.test.reason or "test required"
    for gate in truth.other_gates:
        if gate.required and not gate.ok:
            return (
                NEXT_ACTION_ESCALATE,
                f"{gate.gate} gate required but not satisfied: "
                f"{gate.reason or 'no reason recorded'}",
            )
    if truth.pr_state == "open" and truth.pr_mergeable is False:
        return NEXT_ACTION_CONFLICT_FIX, "PR reports CONFLICTING against its base"
    if truth.ci_state == "red":
        return NEXT_ACTION_DISPATCH_CI_FIX, "a required CI check failed"
    if truth.ci_state == "pending":
        return NEXT_ACTION_WAIT, "a required CI check is still running"
    if truth.pr_state == "closed":
        return NEXT_ACTION_ESCALATE, "PR closed without merging"
    # review/test/registry gates all clear, and merge-side facts (PR open or
    # not yet created, mergeable or unknown, CI green/none) raise nothing.
    return NEXT_ACTION_MERGE, "all gates clear; merge-side facts raise no refusal"


def reconcile_entry(
    board: "Board",
    config: "Config",
    repo_name: str,
    issue_number: int,
    *,
    gh_ops: "GhOps | None" = None,
    ci_store: "CiStore | None" = None,
) -> EntryTruth:
    """Derive the single reconciled :class:`EntryTruth` for ``(repo_name,
    issue_number)`` — every gate from a live fact, never a persisted
    conclusion.

    *gh_ops* mirrors :func:`coord.gates.build_gate_report`'s own parameter
    exactly (pass ``None`` to skip every live lookup — every field then
    reads "unknown"/``None`` rather than guessing); *ci_store* is the
    additional, separately-optional CI backend (e.g.
    :class:`coord.ci_github.GitHubCi`) this adds for the merge-side CI
    summary `build_gate_report` does not itself compute.
    """
    truth = EntryTruth(repo_name=repo_name, issue_number=issue_number)

    report = build_gate_report(board, config, repo_name, issue_number, gh_ops=gh_ops)
    truth.branch = report.branch
    truth.target_branch = report.target_branch
    truth.notes = list(report.notes)
    for decision in report.decisions:
        if decision.gate == "review":
            truth.review = decision
        elif decision.gate == "test":
            truth.test = decision
        elif decision.gate != "merge":
            # "merge" is `build_gate_report`'s own narrower reading (review/
            # test/registry gates only, see its own note on that field) —
            # this module computes its own `next_action` from the fuller
            # live fact set instead of reusing that one.
            truth.other_gates.append(decision)

    winner_row = gate_columns_for_issue(board, repo_name, issue_number)
    if winner_row is not None:
        truth.latest_worker_status = winner_row.status
        truth.latest_worker_assignment_id = winner_row.assignment_id

    repo_cfg = config.repo(repo_name) if config is not None else None
    repo_github = repo_cfg.github if repo_cfg is not None else None

    if gh_ops is not None and repo_github and truth.branch:
        truth.head_probe_attempted = True
        try:
            truth.head_sha = gh_ops.get_branch_sha(repo_github, truth.branch)
        except Exception:  # noqa: BLE001 — #2704 fail-closed: unknown, not stale-reused
            truth.head_sha = None
        if truth.head_sha is not None and truth.target_branch:
            try:
                truth.patch_id = gh_ops.get_branch_patch_id(
                    repo_github, truth.target_branch, truth.branch,
                )
            except Exception:  # noqa: BLE001
                truth.patch_id = None

    if gh_ops is not None and repo_github:
        try:
            truth.issue_state = gh_ops.get_issue_state(repo_github, issue_number) or "unknown"
        except Exception:  # noqa: BLE001
            truth.issue_state = "unknown"

    truth.pr_state, truth.pr_number, truth.pr_mergeable = _live_pr_facts(
        gh_ops, repo_github, truth.branch,
    )
    truth.ci_state = _live_ci_state(ci_store, repo_github, truth.pr_number)

    truth.next_action, truth.next_action_reason = _derive_next_action(truth)
    return truth
