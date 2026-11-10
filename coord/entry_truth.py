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
  ``next_action`` orders a both-unmet case per
  ``config.pipeline.test_precedes_review()`` — the same canonical answer
  ``coord.review.dispatch_pending_reviews`` consults — so this and the
  headless auto-loop's real review-dispatch gate never disagree about
  which stage an entry is waiting on.
- merge — PR lifecycle state (open/merged/closed/none-yet), live
  mergeability, and a CI summary (including #1904 ``checks_absent``), read
  fresh on every call; never the persisted
  ``merge_queue`` row's ``state``/``error`` (those stay exactly what #1
  below says: display only) — except for a latched HUMAN_REQUIRED/SKIPPED
  row, the one fact nothing else here can see, carried through via
  `build_gate_report`'s own "merge" `GateDecision`. Every inconclusive read
  (an unreadable mergeability check, an unreadable CI probe, an
  unconfirmed PR state) fails closed to ``wait``/``escalate``, never
  silently to ``merge``.
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

from coord.gates import GateDecision, GateReport, build_gate_report, gate_columns_for_issue

if TYPE_CHECKING:  # avoid import cycles / heavy imports at module load
    from coord.config import Config
    from coord.ci_store import CiStore
    from coord.merge_queue import GhOps
    from coord.models import Board

# #2234/#3164/#3931 (see coord.dao.TERMINAL_STATUSES's own comment): the
# three terminal `Assignment.status` values a worker leg ends in when it
# refused to do the requested work rather than failing at it. Inlined as
# literals here (mirroring dao.py's own convention) rather than importing
# coord.agent — a large module this one has no other reason to pull in —
# for the three string constants it defines: `coord.agent.REFUSED_POLICY`/
# `REFUSED_PREMISE`/`NEEDS_DECOMPOSITION`.
_REFUSED_POLICY = "refused_policy"
_REFUSED_PREMISE = "refused_premise"
_NEEDS_DECOMPOSITION = "needs_decomposition"

# Mirrors `coord.dao.TERMINAL_STATUSES` (inlined for the same reason as the
# three constants above — this module has no other reason to import
# `coord.dao` or `coord.agent`). Anything NOT in this set is still running:
# dispatching a review/test/merge action for it would race the in-flight
# leg. Keep in sync with `coord.dao.TERMINAL_STATUSES` and
# `coord.drive_state.TERMINAL_STATUSES` (#2096) — a status added to one and
# not the others reads a reaped leg as still in flight, forever.
_TERMINAL_STATUSES = frozenset(
    {
        "done", "merged", "failed", "cancelled", "advisory",
        _REFUSED_POLICY, _REFUSED_PREMISE, _NEEDS_DECOMPOSITION,
    }
)

# next_action vocabulary. "unknown" is deliberately distinct from any of
# these — it means the live facts needed to decide at all could not be
# gathered (no *gh_ops*, or every probe failed), never a default guess.
# Two values beyond the issue's own seven-item enumeration:
# `dispatch_ci_fix` (a live CI read, not one of the issue's named gates, that
# still needs its own distinct remedy from `conflict_fix`/`escalate`) and
# `unknown` itself (no live access was even requested — distinct from a
# probe that was attempted and failed, which escalates instead).
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

    # Merge-side live facts. `pr_state` is one of:
    # "open" — a live, open PR was found.
    # "merged" — `pr_is_merged` confirmed the branch's tip already merged.
    # "closed" — a live read positively confirmed the PR was closed WITHOUT
    #   merging (e.g. `get_pr_state_for_branch` returning "CLOSED").
    # "none" — no open PR, not merged, and no stronger signal available.
    #   This is the BENIGN default: "approved + tested, PR not yet created"
    #   is a normal pipeline state (`merge_queue.process()` creates the PR
    #   itself) and must never be reported the same as a confirmed "closed".
    # "unknown" — no live access to ask (no `gh_ops`, no branch) or the
    #   probe itself failed.
    pr_state: str = "unknown"
    pr_number: int | None = None
    pr_mergeable: bool | None = None
    # "green" (required checks exist and all pass) / "red" (one failed) /
    # "pending" (one still running) / "checks_absent" (no checks reported
    # though CI is expected for this repo/PR — #1904) / "none" (no checks
    # reported, and the repo genuinely has no CI gate wired) / "unknown"
    # (no *ci_store* was supplied, no PR to check yet, or the read itself
    # failed). Does NOT yet detect a #1851/#3263 stale-green check — see
    # `_live_ci_state`'s own note on why reusing `_ci_checks_are_stale`
    # unchanged would misfire against every test double in this codebase.
    ci_state: str = "unknown"
    # True only when a live `list_checks_for_pr` call was actually attempted
    # (a *ci_store* was supplied AND there was a PR number to check) — same
    # "could this have been asked at all" distinction `head_probe_attempted`
    # draws for the branch-head probe.
    ci_probed: bool = False
    # The live "merge" `GateDecision` from `coord.gates.build_gate_report` —
    # carried through ONLY for its operator-latch detail (a real,
    # persisted `merge_queue` row parked HUMAN_REQUIRED/SKIPPED, which
    # nothing else this module derives can see), never reused for the
    # review/test/registry verdicts it also folds in (this module re-derives
    # those from `review`/`test`/`other_gates` above instead).
    merge_gate: GateDecision | None = None

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
) -> tuple[str, bool]:
    """Return ``(ci_state, probed)``. ``probed`` is True only once a live
    ``list_checks_for_pr`` call was actually made — distinguishing "nothing
    to check yet" (no *ci_store*, or no PR) from "a check was attempted and
    came back inconclusive", the same split :func:`reconcile_entry` draws
    for the branch-head probe via `head_probe_attempted`.
    """
    if ci_store is None or not repo_github or not pr_number:
        return "unknown", False
    try:
        checks = ci_store.list_checks_for_pr(repo_github, pr_number)
    except Exception:  # noqa: BLE001 — fail closed: an erroring read is unknown, not green
        return "unknown", True
    from coord.ci_store import failed_checks, in_flight_checks  # noqa: PLC0415

    if not checks:
        # #1904: an empty check list is ALSO what an erroring/absent probe
        # returns — ambiguous between "CI never ran" and "this repo has no
        # CI wired". `coord.merge_queue._ci_expects_checks` is the existing,
        # canonical answer to exactly that question (the same merge gate's
        # own `checks_absent` refusal consults it); reused here rather than
        # re-deriving a second, driftable notion of "should there be checks".
        from coord.merge_queue import _ci_expects_checks  # noqa: PLC0415

        try:
            expects = _ci_expects_checks(ci_store, repo_github, pr_number)
        except Exception:  # noqa: BLE001 — fail closed: assume expected
            expects = True
        return ("checks_absent" if expects else "none"), True
    if failed_checks(checks):
        return "red", True
    if in_flight_checks(checks):
        return "pending", True
    # #1851/#3263: a PASSING check can itself be stale relative to the
    # base's CURRENT tip (GitHub never re-runs a `pull_request` workflow on
    # a base-only move) — `coord.merge_queue._ci_checks_are_stale` is the
    # canonical check, but it wants a `SmokeVerdictStatus` for the
    # #1738/#1778/#1847 base-move exemptions this module has no equivalent
    # of, and fails closed to "stale" on ANY unreadable anchor (including
    # every `CheckRun.started_at is None`, which most test doubles never
    # populate) — reusing it here would read "stale" for a green PR every
    # test double in this codebase constructs today. Left as "green"; CI
    # staleness for `EntryTruth` is follow-up work, tracked alongside the
    # review-staleness-labelling gap in the issue's own acceptance list.
    return "green", True


def _live_pr_facts(
    gh_ops: "GhOps | None", repo_github: str | None, branch: str | None,
) -> tuple[str, int | None, bool | None]:
    """Return ``(pr_state, pr_number, pr_mergeable)`` from live `gh_ops`
    reads only — never from a persisted merge-queue/board column. See the
    `EntryTruth.pr_state` field docstring for what each returned state means.

    ``getattr(..., None)`` on each optional method mirrors
    `coord.merge_queue._ci_expects_checks`'s own fail-closed posture toward a
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
        # `coord.github_ops.find_pr_for_branch` reports GitHub's own
        # `mergeable` field VERBATIM — in production that is a STRING
        # (``"MERGEABLE"``/``"CONFLICTING"``/``"UNKNOWN"``), never a `bool`.
        # Test doubles are free to use a plain `bool` instead; both are
        # handled directly here so the production (string) path is not dead
        # code behind a branch only test stubs ever take.
        pr_mergeable: bool | None
        if isinstance(mergeable, bool):
            pr_mergeable = mergeable
        elif isinstance(mergeable, str) and mergeable.upper() == "MERGEABLE":
            pr_mergeable = True
        elif isinstance(mergeable, str) and mergeable.upper() == "CONFLICTING":
            pr_mergeable = False
        else:
            # Missing, or GitHub's own "UNKNOWN" (still computing) — ask
            # again via `check_pr_mergeable`, which polls a few times before
            # giving up. `check_pr_mergeable`'s own contract: `None` means
            # "could not confirm", and callers (here: `_derive_next_action`)
            # must treat that the same as `False` for deciding whether to
            # merge — never a green light.
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

    # No OPEN PR, not merged: either a PR was closed without merging, or
    # none was ever opened — the common, perfectly healthy "approved and
    # tested, PR not yet created" pipeline state (`merge_queue.process()` is
    # what opens it). `get_pr_state_for_branch` (optional — not every `GhOps`
    # stand-in implements it) can confirm a genuine CLOSED; absent that
    # confirmation this defaults to "none", the BENIGN reading, never the
    # alarming "closed" one a false positive here would wrongly escalate.
    get_pr_state_for_branch = getattr(gh_ops, "get_pr_state_for_branch", None)
    if get_pr_state_for_branch is not None:
        try:
            state = get_pr_state_for_branch(repo_github, branch)
        except Exception:  # noqa: BLE001
            state = None
        if state == "MERGED":
            return "merged", None, None
        if state == "CLOSED":
            return "closed", None, None
        # "OPEN" (raced with `find_pr_for_branch` above returning nothing —
        # treat as "none", the next read will catch up) or `None` (no PR at
        # all, or the read itself failed) falls through to "none" below.

    return "none", None, None


def _review_unmet_action(review: GateDecision) -> tuple[str, str]:
    """The action for an unmet "review" `GateDecision` — split out because
    "review required but not ok" is not always "go dispatch a review".

    Two defect shapes `coord.gates.build_gate_report` folds into the SAME
    ``ok=False`` `GateDecision` need a DIFFERENT remedy than an ordinary
    pending review, and both must be caught here so every caller of
    :func:`_derive_next_action` sees them — not just `coord.drive`'s own
    `_decide_merge` arm that first needed this split:

    - `review.reason` carrying :data:`coord.merge_queue.
      UNKNOWN_BRANCH_HEAD_REASON` (#2704, possibly suffixed by #2809's
      request-id/retry-after detail — matched with `startswith`) means the
      branch head could not be confirmed live at all; this is NEVER "not
      approved", which asserts a refusal this scan never actually
      confirmed. `coord.drive._merge_gate_kind` already treats the same
      string as its own gate kind the auto-loop WAITS on rather than
      escalating or re-dispatching — mirrored here as `wait`.
    - `review.verdict_unparseable` (#1956) means a linked review row
      finished with NO parseable verdict — a defect, not a pending/failed
      review. The remedy is OPERATOR RECOVERY (`review.reason` carries the
      exact `coord report-result --verdict-source recovered` command), not
      another dispatched review, which would just re-derive a conclusion
      that already exists in the log.
    """
    from coord.merge_queue import UNKNOWN_BRANCH_HEAD_REASON  # noqa: PLC0415

    reason = review.reason or "review required but not approved"
    if reason.startswith(UNKNOWN_BRANCH_HEAD_REASON):
        return NEXT_ACTION_WAIT, reason
    if review.verdict_unparseable:
        return NEXT_ACTION_ESCALATE, reason
    return NEXT_ACTION_DISPATCH_REVIEW, reason


def _derive_next_action(truth: EntryTruth, *, test_precedes_review: bool) -> tuple[str, str]:
    """Rule 1: branches ONLY on the live facts already gathered onto
    *truth* — never on a persisted string like `merge_queue.error`.

    *test_precedes_review* is `config.pipeline.test_precedes_review()`
    (#2096: the one, already-canonical answer `coord.review.
    dispatch_pending_reviews` itself consults) — when both the test and
    review gates are unmet, it decides which to name FIRST, so `coord gates`
    and the headless auto-loop's real review-dispatch gate can never
    disagree about which stage an entry is actually waiting on.

    Fails closed throughout: every live fact that could not be confirmed
    (`None`/"unknown"/"checks_absent") routes to `wait`/`escalate`, never
    silently falls through to `merge`.
    """
    if truth.issue_state == "closed":
        return NEXT_ACTION_MARK_DONE, "issue is already closed"
    if truth.pr_state == "merged":
        return NEXT_ACTION_MARK_DONE, "PR merged; issue should close"
    if truth.latest_worker_status in (
        _REFUSED_PREMISE, _REFUSED_POLICY, _NEEDS_DECOMPOSITION,
    ):
        return (
            NEXT_ACTION_ESCALATE,
            f"latest worker leg exited {truth.latest_worker_status!r} — needs "
            "operator recovery, not another dispatch",
        )
    if (
        truth.latest_worker_status is not None
        and truth.latest_worker_status not in _TERMINAL_STATUSES
    ):
        return (
            NEXT_ACTION_WAIT,
            f"latest worker leg {truth.latest_worker_assignment_id!r} is still "
            f"in flight (status={truth.latest_worker_status!r}) — dispatching "
            "another action now would race it",
        )
    if truth.head_sha is None:
        if truth.head_probe_attempted:
            return (
                NEXT_ACTION_ESCALATE,
                "branch head could not be read live (#2704) — fail closed, "
                "cannot safely decide the next action",
            )
        return NEXT_ACTION_UNKNOWN, "no live gh access requested — cannot determine next action"

    test_unmet = truth.test is not None and truth.test.required and not truth.test.ok
    review_unmet = truth.review is not None and truth.review.required and not truth.review.ok
    if test_precedes_review:
        # Work → Test → Review (CLAUDE.md; `coord.review.
        # dispatch_pending_reviews` holds review dispatch on exactly this
        # condition) — a pending test outranks a pending review.
        if test_unmet:
            return NEXT_ACTION_DISPATCH_TEST, truth.test.reason or "test required"  # type: ignore[union-attr]
        if review_unmet:
            return _review_unmet_action(truth.review)  # type: ignore[arg-type]
    else:
        if review_unmet:
            return _review_unmet_action(truth.review)  # type: ignore[arg-type]
        if test_unmet:
            return NEXT_ACTION_DISPATCH_TEST, truth.test.reason or "test required"  # type: ignore[union-attr]

    for gate in truth.other_gates:
        if gate.required and not gate.ok:
            return (
                NEXT_ACTION_ESCALATE,
                f"{gate.gate} gate required but not satisfied: "
                f"{gate.reason or 'no reason recorded'}",
            )

    if truth.pr_state == "open":
        if truth.pr_mergeable is not True:
            # `check_pr_mergeable`'s own contract: `None` (still computing,
            # or the read failed) must be treated the same as `False` — an
            # inconclusive read is never a green light to merge. Only a
            # CONFIRMED `False` names the conflict; an inconclusive read
            # just waits for the next pass to try again.
            if truth.pr_mergeable is False:
                return NEXT_ACTION_CONFLICT_FIX, "PR reports CONFLICTING against its base"
            return (
                NEXT_ACTION_WAIT,
                "PR mergeability could not be confirmed live — fail closed, "
                "never a green light to merge",
            )
        if truth.ci_state == "red":
            return NEXT_ACTION_DISPATCH_CI_FIX, "a required CI check failed"
        if truth.ci_state == "pending":
            return NEXT_ACTION_WAIT, "a required CI check is still running"
        if truth.ci_state == "checks_absent":
            return (
                NEXT_ACTION_DISPATCH_CI_FIX,
                "no checks reported though this repo declares CI (#1904) — "
                "merging would run untested code",
            )
        if truth.ci_state == "unknown":
            if truth.ci_probed:
                return (
                    NEXT_ACTION_WAIT,
                    "CI check read failed live — fail closed, cannot confirm green",
                )
            return (
                NEXT_ACTION_UNKNOWN,
                "no live CI access requested — cannot evaluate the CI gate",
            )
        # "none" — this repo genuinely has no CI gate wired. Falls through.
    elif truth.pr_state == "closed":
        return NEXT_ACTION_ESCALATE, "PR closed without merging"
    elif truth.pr_state == "unknown":
        return (
            NEXT_ACTION_ESCALATE,
            "PR state could not be read live — fail closed, cannot safely decide",
        )
    # else "none" (no PR yet — the normal pre-merge-queue pipeline state):
    # falls through to merge, which creates the PR (`merge_queue.process()`).

    if truth.merge_gate is not None and truth.merge_gate.required and not truth.merge_gate.ok:
        # Every gate this module derives directly (review/test/registry/PR/
        # CI) already passed above — the only way `build_gate_report`'s own
        # "merge" decision can still be unmet here is its one piece of
        # information nothing else above carries: a latched, operator-only
        # HUMAN_REQUIRED/SKIPPED `merge_queue` row (#3445).
        return NEXT_ACTION_ESCALATE, truth.merge_gate.reason or "merge gate blocked"

    # review/test/registry/PR/CI/merge-queue-latch gates all clear.
    return NEXT_ACTION_MERGE, "all gates clear; merge-side facts raise no refusal"


def reconcile_entry(
    board: "Board",
    config: "Config",
    repo_name: str,
    issue_number: int,
    *,
    gh_ops: "GhOps | None" = None,
    ci_store: "CiStore | None" = None,
    report: "GateReport | None" = None,
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

    *report* lets a caller that already built its own
    :class:`~coord.gates.GateReport` (e.g. to print it) pass that SAME
    snapshot through rather than paying for — and risking disagreeing
    with — a second live `build_gate_report` call a moment later. Built
    fresh via *gh_ops* when omitted.
    """
    truth = EntryTruth(repo_name=repo_name, issue_number=issue_number)

    if report is None:
        report = build_gate_report(board, config, repo_name, issue_number, gh_ops=gh_ops)
    truth.branch = report.branch
    truth.target_branch = report.target_branch
    truth.notes = list(report.notes)
    for decision in report.decisions:
        if decision.gate == "review":
            truth.review = decision
        elif decision.gate == "test":
            truth.test = decision
        elif decision.gate == "merge":
            # `build_gate_report`'s own narrower reading (review/test/
            # registry gates only, see its own note on that field) — kept
            # ONLY for its HUMAN_REQUIRED/SKIPPED merge-queue-latch detail
            # (see `_derive_next_action`), never reused for the
            # review/test/registry verdicts it also folds in.
            truth.merge_gate = decision
        else:
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
    truth.ci_state, truth.ci_probed = _live_ci_state(ci_store, repo_github, truth.pr_number)

    test_precedes_review = (
        config is not None
        and getattr(config, "pipeline", None) is not None
        and config.pipeline.test_precedes_review()
    )
    truth.next_action, truth.next_action_reason = _derive_next_action(
        truth, test_precedes_review=bool(test_precedes_review),
    )
    return truth
