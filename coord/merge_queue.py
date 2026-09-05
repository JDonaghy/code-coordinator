"""Merge queue: sequence completed assignments into their target branches.

Two-layer design so the logic is testable without hitting `gh`:

- Data + sequencing live here (pure functions over QueuedMerge).
- Wire calls (gh pr create / merge / size) are passed in as `gh_ops` so
  tests can substitute a stub. `coord.cli` wires the real `coord.github_ops`.
"""

from __future__ import annotations

import inspect
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, NamedTuple, Protocol

from coord import sql
from coord.audit import record_audit
# #2704: recognized (not required) — only `coord.github_ops.get_branch_sha`'s
# opt-in `raise_on_transient=True` path raises this. A `gh_ops` stand-in that
# doesn't support the kwarg (e.g. `coord.gate_snapshot.GateSnapshot`, whose
# cache-miss `None` is a deliberate, unrelated fail-open convention — see
# `evaluate_smoke_verdict`) raises a plain `TypeError` instead, caught by the
# generic `except Exception` alongside it and never mistaken for this.
from coord.github_ops import GhTransientError
from coord.ci_store import (
    CheckRun,
    CiCheckSummary,
    CiStore,
    JobRun,
    NoOpCi,
    checks_are_stale,
    failed_checks,
    in_flight_checks,
    is_unreadable_check,
    is_verdictless_job,
    summarize,
    summarize_counts,
)
from coord.db import get_connection, retry_on_locked, rollback_after_driver_error
from coord.forge_availability import MERGE_GATE_REFUSAL_KINDS, record_merge_gate_refusal
from coord.models import (
    CLOSES_ISSUE_TYPES,
    WORK_LIKE_TYPES,
    Assignment,
    trust_issue_closed_for,
)
from coord.pr_body_lint import downgrade_closing_keywords, find_closing_references
from coord.state import COORD_DIR, dismiss_drive_escalation

_log = logging.getLogger(__name__)

# Legacy path constant — kept for backward compat with monkeypatch calls in tests.
QUEUE_FILE = COORD_DIR / "merge_queue.json"

# States
PENDING = "pending"
MERGING = "merging"
MERGED = "merged"
CONFLICT = "conflict"
SKIPPED = "skipped"
# Set on a merge entry whose conflict-fix attempt also failed — the user must
# resolve the conflict by hand.  See #241.
HUMAN_REQUIRED = "human_required"


# ── Conflict classification ─────────────────────────────────────────────────

# #1467: the specific subset of GitHub wording that means a --rebase merge
# was refused purely because the branch contains a merge commit — a
# *linearity* failure, not a content conflict. This distinction matters for
# reconcile_conflict_entries: GitHub's `mergeable` field (what
# check_pr_mergeable reads) only reflects content conflicts and happily
# reports MERGEABLE for a branch that is clean but not rebase-able, so a
# plain mergeable check is not evidence that a retried --rebase will
# succeed. See is_rebase_refusal(). Defined once here and folded into
# _REBASEABLE_SIGNALS below so the two lists can't drift apart.
_REBASE_REFUSAL_SIGNALS = (
    "can't be rebased",
    "cannot be rebased",
)

_REBASEABLE_SIGNALS = (
    "could not be rebased",
    # #1467: GitHub's actual wording when a branch contains a merge commit
    # — distinct from "could not be rebased" above (which never matched it)
    # and previously fell through to "unknown", so #241's conflict-fix
    # worker was never dispatched and the entry parked forever. A local
    # `git rebase origin/main` linearises the branch, which is exactly what
    # the dispatched conflict-fix worker attempts.
    *_REBASE_REFUSAL_SIGNALS,
    "merge conflict",
    "not up to date",
    "non-fast-forward",
    "behind the base branch",
    # `gh pr merge` returns this when the PR is behind base and a rebase
    # would be needed.  Common on PRs that sat open while main moved.
    "merge commit cannot be cleanly created",
    "not mergeable",
)

_HUMAN_SIGNALS = (
    "required status check",
    "review required",
    "permission",
    "protected branch",
    "branch protection",
    # #2475: GitHub's wording when a required status check can never report
    # — e.g. its source CI job was deleted from the workflow while the
    # check was still required in branch protection. This is a *permanent*
    # block; no rebase or content change fixes it. The full message is
    # something like "Pull request X is not mergeable: the base branch
    # policy prohibits the merge.", which also contains "not mergeable" —
    # a _REBASEABLE_SIGNALS entry — so without this specific phrase here,
    # classify_conflict fell through past _HUMAN_SIGNALS (no match) to
    # _REBASEABLE_SIGNALS's generic "not mergeable" match and misclassified
    # the failure as "rebaseable", dispatching a conflict-fix worker that
    # could never succeed (#2009's 38-turn thrash).
    "policy prohibits the merge",
)


def classify_conflict(error: str | None) -> str:
    """Decide what kind of merge failure ``error`` represents.

    Returns ``"rebaseable"`` (a mechanical rebase conflict an agent can
    attempt), ``"human"`` (permission / branch protection — surface to the
    user), or ``"unknown"`` (don't auto-dispatch; let the user inspect).

    Used by ``coord merge`` (#241) to decide whether to spawn a
    ``type="conflict-fix"`` assignment or surface the failure as-is.
    """
    if not error:
        return "unknown"
    text = error.lower()
    if any(sig in text for sig in _HUMAN_SIGNALS):
        return "human"
    if any(sig in text for sig in _REBASEABLE_SIGNALS):
        return "rebaseable"
    return "unknown"


def is_rebase_refusal(error: str | None) -> bool:
    """True when ``error`` is specifically GitHub's "branch can't be
    rebased" refusal — a merge commit on the branch, not a content
    conflict (#1467).

    Narrower than ``classify_conflict(error) == "rebaseable"``, which also
    matches ordinary content conflicts ("merge conflict", "not mergeable",
    …) that GitHub's own ``mergeable`` field already reports accurately.
    This predicate isolates the one failure mode where ``mergeable:
    MERGEABLE`` is *not* proof a retried ``--rebase`` will succeed, so
    :func:`reconcile_conflict_entries` and the ``coord merge`` CLI can treat
    it differently from a plain conflict.
    """
    if not error:
        return False
    text = error.lower()
    return any(sig in text for sig in _REBASE_REFUSAL_SIGNALS)


# ── Work-chain resolution (#567) ────────────────────────────────────────────

def _chain_work_ids(entry: "QueuedMerge", pool: list) -> set[str]:
    """Collect every work-assignment id connected to *entry*: by branch
    equality (pre-#567 behaviour) **or** by the ``review_of_assignment_id``
    linkage a bounce-fix worker records back to the assignment it fixes.

    #567: a fix worker dispatched under the #557 remote-interactive-rework
    gap has ``branch=NULL``, so it never matches ``branch == entry.branch``
    and a verdict recorded on it is invisible to ``has_approved_review`` /
    ``has_smoke_verdict``. Every ``WORK_LIKE_TYPES`` assignment dispatched as
    a fix records ``review_of_assignment_id`` pointing at the assignment it
    fixes (``auto_loop.py`` fix dispatch), so the chain is reconstructable
    without a branch match. Expansion runs to a fixed point so multi-hop
    bounce chains (a fix of a fix) are fully covered, not just one hop.

    #1601: the walk used to be forward-only — a known PARENT pulled in its
    CHILD (the fix round), but not the reverse. An entry keyed to the child
    (e.g. the fix round's own approved re-review, per #292 Defect 2's
    re-keying) could not walk *backward* to reach the parent's still-useful
    fields (its ``test_state``/``smoke_test`` verdict, when the fix round
    never re-ran one) whenever branch equality alone didn't already bridge
    the two — the same ``branch=NULL`` gap #567 fixed for the forward
    direction. The expansion is now symmetric: a known row pulls in both its
    recorded children AND its own ``review_of_assignment_id`` parent.
    """
    work_ids: set[str] = set()
    if entry.assignment_id:
        work_ids.add(entry.assignment_id)

    work_assignments = [a for a in pool if getattr(a, "type", None) in WORK_LIKE_TYPES]

    # Branch equality — the original (#292) expansion.
    for a in work_assignments:
        aid = getattr(a, "assignment_id", None)
        branch = getattr(a, "branch", None)
        if aid and branch and branch == entry.branch:
            work_ids.add(aid)

    # review_of_assignment_id chain — covers fix workers with branch=NULL,
    # and multi-iteration bounce chains via a fixed-point expansion. Runs in
    # BOTH directions (#1601) so the chain is the same set regardless of
    # which round in it the entry happens to be keyed to.
    changed = True
    while changed:
        changed = False
        for a in work_assignments:
            aid = getattr(a, "assignment_id", None)
            parent = getattr(a, "review_of_assignment_id", None)
            # Forward: a known parent pulls in its child.
            if aid and parent in work_ids and aid not in work_ids:
                work_ids.add(aid)
                changed = True
            # Backward (#1601): a known child pulls in its own parent.
            if aid and aid in work_ids and parent and parent not in work_ids:
                work_ids.add(parent)
                changed = True

    return work_ids


# ── Branch winner resolution (#1490) ────────────────────────────────────────
#
# A fix/bounce cycle dispatches a fresh WORK_LIKE_TYPES assignment for every
# retry, and every one of them keeps its row in `board.completed` forever —
# all targeting the same branch. `enqueue_approved_work` (the daemon tick)
# and `coord merge`'s own auto-enqueue scan both used to process every such
# row independently and hand each one to `refresh_entry_assignment`, which
# re-keys the ONE queue row that exists for the branch to whichever
# assignment_id it was just called with. Processing three rows on one
# branch in a single pass therefore re-keyed the same entry three times in
# a row and printed three "auto-enqueued" lines for what is — and always
# was — a single queue entry; because the gates
# (`passes_merge_gates`/`has_approved_review`/`has_smoke_verdict`) are
# resolved over the whole branch chain rather than the specific row passed
# in, even the row with a *failed* test_state would pass the gate and win a
# later iteration's re-key, so the "current" key flip-flopped across every
# row on every single tick, forever (#1490's observed bug).
#
# The fix: resolve every branch to a single winner *before* touching the
# queue at all, and never enqueue (or re-announce) the other rows.


def _select_winning_work_assignment(work_assignments: list) -> "Assignment":
    """Pick the one row in *work_assignments* — all sharing one branch —
    that should key the branch's merge-queue entry.

    Prefers the most-recently-dispatched row that already carries a fresh
    terminal smoke verdict (``test_state in ('passed', 'skipped')``) — the
    "approved + test-passed" row the issue asks the queue entry to track.
    Falls back to the most-recently-dispatched row overall when none has
    passed yet (the branch is still mid-cycle; it should still enqueue —
    blocked on the smoke gate — rather than vanish). Ties on
    ``dispatched_at`` (including everything being ``None``, e.g. rows from
    tests or pre-#821 data) resolve to the last one in *work_assignments*
    (typically ``board.completed`` insertion order, i.e. the most recently
    seen row), same tie-break convention as :func:`resolve_entry_key`.
    """
    def _dispatched_at(a) -> float:
        return getattr(a, "dispatched_at", None) or 0

    passed = [
        a for a in work_assignments
        if getattr(a, "test_state", None) in ("passed", "skipped")
    ]
    pool = passed if passed else work_assignments
    winner = pool[0]
    for a in pool[1:]:
        if _dispatched_at(a) >= _dispatched_at(winner):
            winner = a
    return winner


def group_branch_candidates(completed: Iterable) -> list[tuple["Assignment", list]]:
    """Group every done :data:`~coord.models.WORK_LIKE_TYPES` assignment in
    *completed* by ``(repo_name, branch)`` and resolve each group to a
    single winner (#1490).

    Returns one ``(winner, superseded)`` pair per distinct ``(repo_name,
    branch)`` group, in first-seen order (stable — output doesn't jitter
    run to run). ``superseded`` holds the group's other rows (``[]`` when
    there was only one); callers must log them and never enqueue them —
    see :func:`_select_winning_work_assignment` for how the winner is
    chosen.

    Rows missing ``branch``/``assignment_id``, not in ``WORK_LIKE_TYPES``,
    or not ``status == "done"`` are dropped from consideration entirely —
    the same ad-hoc filter both call sites (`enqueue_approved_work`, the
    ``coord merge`` auto-enqueue scan) applied before this was extracted.
    """
    order: list[tuple[str, str]] = []
    groups: dict[tuple[str, str], list] = {}
    for a in completed:
        if getattr(a, "type", None) not in WORK_LIKE_TYPES:
            continue
        if getattr(a, "status", None) != "done":
            continue
        branch = getattr(a, "branch", None)
        aid = getattr(a, "assignment_id", None)
        if not branch or not aid:
            continue
        key = (getattr(a, "repo_name", None), branch)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(a)

    result: list[tuple["Assignment", list]] = []
    for key in order:
        rows = groups[key]
        winner = rows[0] if len(rows) == 1 else _select_winning_work_assignment(rows)
        superseded = [r for r in rows if r is not winner]
        result.append((winner, superseded))
    return result


def _log_superseded(row) -> None:
    """One clear line per row a branch-winner scan skipped (#1490) — so
    "three rows, one queue entry" reads as expected coalescing rather than
    "two got lost"."""
    _log.info(
        "merge-queue: %s#%s assignment %s (branch %s) superseded on this "
        "branch — not enqueued",
        getattr(row, "repo_name", None),
        getattr(row, "issue_number", None),
        getattr(row, "assignment_id", None),
        getattr(row, "branch", None),
    )


# ── Review gate (#253) ──────────────────────────────────────────────────────

def requires_review(entry: "QueuedMerge", config) -> bool:
    """True when *entry* must have an approved review before merging.

    Honours ``config.reviews.enabled`` (the master switch for the
    adversarial review feature) and the *effective* gate list: ``entry``'s
    own ``required_gates`` when set, falling back to
    ``config.pipeline.default_gates`` otherwise (#1213).  ``entry`` is
    duck-typed — both ``QueuedMerge`` (``required_gates`` snapshotted at
    :func:`enqueue` time, commit-bound) and ``Assignment`` (``required_gates``
    resolved from ``config.pipeline.labels`` at dispatch time, see
    :func:`coord.brain.resolve_required_gates`) carry the attribute, so
    ``coord.merge_queue.plan`` can pass either.  Untagged work — the entry
    has no override — behaves exactly as before this change: the default
    policy applies. Explicit-only overrides (``--skip-review``) remain
    available as a manual escape hatch on top of this.
    """
    if not getattr(config, "reviews", None) or not config.reviews.enabled:
        return False
    pipeline = getattr(config, "pipeline", None)
    if pipeline is None:
        return True
    gates = getattr(entry, "required_gates", None) or (pipeline.default_gates or [])
    return "review" in gates


def _backfill_branch_patch_id(entry: "QueuedMerge", gh_ops: "GhOps | None") -> str | None:
    """Return ``entry.branch_patch_id``, computing and persisting it via
    *gh_ops* when null, or ``None`` when it can't be determined.

    #1506: ``entry.branch_patch_id`` is normally populated by :func:`process`
    before the review/smoke gates run, but any entry that reaches
    :func:`has_approved_review` / :func:`find_scoped_review_candidate`
    without having gone through that backfill first — most notably every
    queue row whose approved review predates #1475, which never got a
    chance to backfill it — has ``branch_patch_id: None`` forever, and a
    null there previously meant "cannot prove identical", voiding an
    approval for a diff that had not changed by one byte.

    The base passed is *entry.target_branch* — a branch **name**, resolved
    by GitHub's three-dot compare API (:func:`coord.github_ops.
    get_branch_patch_id`) to the true merge-base of the two refs — never the
    PR's recorded ``baseRefOid``. Using ``baseRefOid`` produces a false
    mismatch once the base branch has advanced past the PR's original fork
    point (#1506's investigation hit exactly this).

    ``gh_ops=None`` (no client available) or a missing repo/base/branch on
    *entry* returns ``None`` without any I/O — callers fail closed exactly as
    before. A successful computation is written back onto *entry* so the
    ``gh api compare`` round trip happens at most once per entry; the caller
    is responsible for persisting the entry (e.g. ``save_queue``) same as
    the existing ``branch_head_sha``/``branch_patch_id`` backfills in
    :func:`process`.
    """
    if gh_ops is None:
        return None
    repo = getattr(entry, "repo_github", None)
    base = getattr(entry, "target_branch", None)
    branch = getattr(entry, "branch", None)
    if not repo or not base or not branch:
        return None
    try:
        computed = gh_ops.get_branch_patch_id(repo, base, branch)
    except Exception:  # noqa: BLE001 — fail-safe: unknown patch-id is not blocking
        return None
    if computed is not None:
        try:
            entry.branch_patch_id = computed
        except Exception:  # noqa: BLE001 — best effort; a read-only entry just recomputes next time
            pass
    return computed


# #2704: the honest reason for a review/smoke gate refusal when the
# underlying cause is that this caller could not read the branch's live head
# SHA at all — GitHub unreachable, `gh` unauthenticated, or (the incident
# that filed this) a secondary rate limit. Before #2704 the two gates
# inverted in OPPOSITE directions on exactly this condition: the review gate
# folded it into the generic "review required but not approved" (a
# fabricated refusal — see `ApprovalScan.unknown_head`, which already
# distinguished this case but never reached the reason string), while
# `evaluate_smoke_verdict` silently skipped every staleness compare it
# couldn't make and returned SMOKE_OK (a fabricated pass — see
# `SMOKE_UNKNOWN`). Both gates now report THIS string instead, so:
#   - the operator sees the real cause, not a fictitious "not approved"/
#     "passing" verdict;
#   - `coord.drive._merge_gate_kind` recognizes it as its own kind (neither
#     "review" nor "smoke"), so the auto-loop WAITS for the probe to recover
#     instead of escalating a re-review or a Test re-run for a gate that was
#     never actually evaluated.
UNKNOWN_BRANCH_HEAD_REASON = (
    "branch head unknown — cannot evaluate freshness gates (GitHub "
    "unreachable, unauthenticated, or rate-limited; retry once it recovers)"
)


def unknown_branch_head_reason(probe_error: "GhTransientError | None" = None) -> str:
    """:data:`UNKNOWN_BRANCH_HEAD_REASON`, enriched with *probe_error*'s
    structured detail when there is any to add (#2809).

    #2809: the generic string collapses three very different causes — GitHub
    unreachable, `gh` unauthenticated, or rate-limited — into one sentence an
    operator cannot act on differently. When *probe_error* is the
    :class:`~coord.github_ops.GhRateLimitError` subclass (raised by a live
    :func:`~coord.github_ops.get_branch_sha` call, threaded through by
    :func:`_gh_get_branch_sha`/:func:`live_gate_entry`), this appends the
    real HTTP status, GitHub request ID, and (when GitHub sent one) the
    ``Retry-After`` wait — exactly the detail the issue's evidence shows was
    being silently discarded.

    Returns the UNCHANGED constant — byte for byte — whenever *probe_error*
    is ``None`` or carries no structured detail (e.g. a plain
    :class:`~coord.github_ops.GhTransientError` for an auth/network failure,
    or a caller whose ``gh_ops`` stand-in doesn't support
    ``raise_on_transient`` at all): every existing caller of the bare
    constant, and every :func:`_merge_gate_kind` classifier that substring-
    matches against it, keeps working unchanged — this only ever ADDS a
    suffix, never rewords the prefix `_merge_gate_kind` depends on.
    """
    if probe_error is None:
        return UNKNOWN_BRANCH_HEAD_REASON
    status = getattr(probe_error, "status_code", None)
    request_id = getattr(probe_error, "request_id", None)
    retry_after = getattr(probe_error, "retry_after_s", None)
    if status is None and request_id is None and retry_after is None:
        return UNKNOWN_BRANCH_HEAD_REASON
    secondary = getattr(probe_error, "secondary", False)
    bits = []
    if status is not None:
        kind = "secondary rate limit" if secondary else "rate limit"
        bits.append(f"GitHub {kind}, HTTP {status}")
    if request_id:
        bits.append(f"request {request_id}")
    if retry_after is not None and retry_after > 0:
        bits.append(f"retry after ~{retry_after:.0f}s")
    return f"{UNKNOWN_BRANCH_HEAD_REASON} [{', '.join(bits)}]"


class ApprovalScan(NamedTuple):
    """The result of one walk over *board*'s approving reviews for an entry.

    #2085/#2096: two surfaces ask about the same walk and must not answer it
    with two implementations that merely agree today —

    - ``approved`` — the merge gate's verdict. :func:`has_approved_review`
      is exactly this field, so every gate caller is unchanged.
    - ``unknown_head`` — True when an approving review was refused *only*
      because the entry's branch head SHA was unknown to this caller
      (``branch_head_sha is None``), rather than because it was confirmed
      to differ from ``review_head_sha``. The two are indistinguishable in
      a bool, but they mean opposite things to a read-only display:
      "confirmed superseded" vs "not yet checked". :func:`display_error`
      needs that distinction (see its docstring); the gate deliberately
      does not — both fail closed there.

      #2704 follow-up: "not yet checked" is only honest while nothing else
      settled the question. When the chain's LATEST verdict-bearing review
      is a ``request-changes`` (the #2085/#1966 chain: approve, then a fix
      round, then an explicit refusal), the reviewer refused on the record
      — that verdict needs no head SHA to be true, so ``unknown_head`` is
      False and every surface reads the genuine refusal. See
      :func:`_latest_review_verdict`.
    """

    approved: bool
    unknown_head: bool


def _latest_review_verdict(pool, branch_work_ids: set | frozenset) -> str | None:
    """The verdict of the most-recently-dispatched *verdict-bearing* review
    in *pool* covering any of *branch_work_ids*, or ``None`` when the chain
    has no completed verdict at all.

    Reviews still in flight (``review_verdict is None``) are skipped — an
    unfinished review settles nothing. Ties on ``dispatched_at`` (including
    everything being ``None``, e.g. rows from tests or pre-#821 data)
    resolve to the last matching row in *pool* order, the same tie-break
    convention as :func:`_select_winning_work_assignment`.

    This is the same "latest review wins" reading the board's per-issue
    stage projection uses for its ``review`` badge, which is why the two
    surfaces can no longer disagree about a superseded approval (#2085).
    """
    winner: str | None = None
    winner_at = float("-inf")
    for a in pool:
        if getattr(a, "type", None) != "review":
            continue
        if getattr(a, "review_of_assignment_id", None) not in branch_work_ids:
            continue
        verdict = getattr(a, "review_verdict", None)
        if verdict is None:
            continue
        at = getattr(a, "dispatched_at", None) or 0.0
        if at >= winner_at:
            winner, winner_at = verdict, at
    return winner


def has_approved_review(
    entry: "QueuedMerge", board, gh_ops: "GhOps | None" = None
) -> bool:
    """True when a completed review with ``review_verdict='approve'`` exists
    on *board* for the work assignment behind *entry*.

    Thin wrapper over :func:`scan_approved_reviews` — this is the merge
    gate's verdict and nothing else. See that function for the walk itself.

    Scans both active and completed assignments — a review whose findings
    were just posted may still be on ``board.active`` for a tick before
    reconcile moves it to ``completed``.  We accept either, since the
    verdict is what matters.

    #292 (Defect 1): after a review bounce the queue entry may be keyed to
    the *original* work assignment while the approved re-review is linked to
    the *fix* work assignment.  To handle this we collect **all** work
    assignment IDs connected to the entry — by shared branch, or (#567) by
    the ``review_of_assignment_id`` chain, which also catches fix workers
    dispatched with ``branch=NULL`` — and accept any approved review that
    points to any of them.

    #1475: a SHA mismatch alone no longer voids the approval outright. When
    the branch's current content-addressed patch-id (``branch_patch_id``)
    matches the patch-id captured at review time (``review_patch_id``), the
    SHA moved but the diff didn't — e.g. a conflict-fix rebase that resolved
    cleanly — so the approval still covers this content. Missing either
    patch-id fails closed to the pre-#1475 behaviour (stale, re-review) —
    UNLESS *gh_ops* is supplied, in which case a null ``branch_patch_id`` is
    computed on demand (#1506) rather than treated as an unrecoverable
    mismatch; see :func:`_backfill_branch_patch_id`.
    """
    return scan_approved_reviews(entry, board, gh_ops).approved


def scan_approved_reviews(
    entry: "QueuedMerge", board, gh_ops: "GhOps | None" = None
) -> ApprovalScan:
    """Walk *board* for an approving review covering *entry*'s work chain.

    The single implementation behind both :func:`has_approved_review` (the
    merge gate) and :func:`display_error`'s read-only recompute — see
    :class:`ApprovalScan` for why the second one needs more than the bool.
    """
    pool = list(getattr(board, "completed", []) or []) + list(getattr(board, "active", []) or [])

    branch_work_ids = _chain_work_ids(entry, pool)

    if not branch_work_ids:
        return ApprovalScan(approved=False, unknown_head=False)

    # #821: commit-bound check.  If the entry has a branch_head_sha (set at
    # process() time from the live branch tip) and the review has a
    # review_head_sha (set when the review assignment ran), an approval only
    # counts when the two SHAs match — i.e. no new commits were pushed after
    # the review completed.  When the review has no review_head_sha at all
    # (pre-#821 rows, SHA tracking never available for that verdict) the
    # check is skipped — nothing to compare against, unchanged from before
    # #821 existed.
    #
    # #2085: a review WITH a review_head_sha but a caller that cannot supply
    # entry.branch_head_sha (a raw work Assignment — no such attribute; a
    # QueuedMerge whose branch was deleted — attribute present but None) is
    # the opposite case and must fail CLOSED, not skip the check. Before
    # #2085 `current_sha is None` fell through to the same "return True" as
    # "review predates SHA tracking", so any caller that didn't populate
    # branch_head_sha (the board's stage projection, `enqueue_approved_work`,
    # a queue row whose branch had just been deleted) silently accepted a
    # superseded approval — see #2085 for the observed #1966 chain and the
    # deleted-branch READY flip this produced.
    current_sha = getattr(entry, "branch_head_sha", None)
    current_patch_id = getattr(entry, "branch_patch_id", None)
    patch_id_attempted = current_patch_id is not None
    # #2085: set when an approval is refused *purely* because `current_sha`
    # is unknown here — see `ApprovalScan.unknown_head`.
    unknown_head = False

    for a in pool:
        if getattr(a, "type", None) != "review":
            continue
        if getattr(a, "review_of_assignment_id", None) not in branch_work_ids:
            continue
        if getattr(a, "review_verdict", None) != "approve":
            continue
        review_sha = getattr(a, "review_head_sha", None)
        # #2085: `current_sha is None` (branch head unknown to this caller)
        # is folded into the same "not confirmed fresh" branch as an actual
        # mismatch — previously it skipped straight to `return True` below,
        # which is exactly the fail-open gap #2085 documents. A review with
        # no `review_head_sha` at all (the `review_sha is not None` guard)
        # still takes the legacy no-SHA-to-compare path unchanged.
        if review_sha is not None and (current_sha is None or review_sha != current_sha):
            # #1475: the SHA moved (or is unknown) — before declaring the
            # approval stale/unconfirmed, check whether the underlying
            # content is identical via patch-id. A pure rebase (no conflict)
            # replays the identical diff against a new base and produces the
            # same patch-id even though the commit SHA changed; a conflict
            # resolution or a genuine content change produces a different
            # one. Fail closed when either patch-id is unavailable — which
            # it always is when *entry* has no way to supply one (e.g. a raw
            # work Assignment with no `branch_patch_id` attribute and
            # *gh_ops* is ``None``), so a caller with no live SHA/patch-id
            # access correctly can never confirm freshness and falls to
            # `continue` → ``False``.
            review_patch_id = getattr(a, "review_patch_id", None)
            if review_patch_id is not None:
                if current_patch_id is None and not patch_id_attempted:
                    # #1506: compute-once, not fail-closed-forever.
                    current_patch_id = _backfill_branch_patch_id(entry, gh_ops)
                    patch_id_attempted = True
                if current_patch_id is not None and review_patch_id == current_patch_id:
                    # content-identical rebase — approval still covers it
                    return ApprovalScan(approved=True, unknown_head=False)
            if current_sha is None:
                # Refused because we don't KNOW the head, not because we
                # checked and it moved. Same (closed) gate verdict, but a
                # display surface must not call this "not approved".
                unknown_head = True
            continue  # stale/unconfirmed: cannot prove this approval covers the current head
        return ApprovalScan(approved=True, unknown_head=False)
    if unknown_head and _latest_review_verdict(pool, branch_work_ids) != "approve":
        # #2704 follow-up: an unreadable branch head only explains the
        # refusal while the approval is the chain's last word. The #1966
        # chain (approve → fix round → request-changes) ends in an explicit
        # refusal, which is true regardless of what the head SHA is — so
        # reporting "branch head unknown" here would hide a real
        # request-changes behind a transient-looking probe failure, and
        # would tell `coord.drive` to WAIT for a probe that can never
        # unblock it. Fall back to the confirmed refusal.
        unknown_head = False
    return ApprovalScan(approved=False, unknown_head=unknown_head)


def find_scoped_review_candidate(
    entry: "QueuedMerge", board, gh_ops: "GhOps | None" = None
) -> Assignment | None:
    """Return the previously-approved review whose approval was voided
    ONLY by a content-changing rebase (#1476), or ``None``.

    Mirrors :func:`has_approved_review`'s SHA/patch-id staleness walk but
    returns the review :class:`~coord.models.Assignment` itself (not a
    bool) — a scoped re-review needs the prior review's ``review_head_sha``
    (the base to diff the resolution from) and ``briefing``/findings as
    established context, not just a yes/no.

    Returns ``None`` — meaning "not this path, fall back to a full review"
    — when:

    - No approved review exists for *entry*'s work chain at all.
    - The branch's current SHA isn't known (can't confirm anything changed),
      or the current patch-id isn't known and can't be computed (#1506: when
      *gh_ops* is supplied, a null ``branch_patch_id`` is backfilled on
      demand via :func:`_backfill_branch_patch_id` instead of failing
      immediately).
    - The most-recently-matched approved review's SHA still matches the
      current one (nothing changed — not stale at all).
    - Its patch-id still matches the current one (content-identical
      rebase — :func:`has_approved_review` already carries this forward,
      there is no delta to scope a review around).
    - Either patch-id is missing (fail closed, same posture as
      ``has_approved_review``: an unconfirmable diff gets a full review,
      never a guessed-at scoped one).
    """
    pool = list(getattr(board, "completed", []) or []) + list(getattr(board, "active", []) or [])
    branch_work_ids = _chain_work_ids(entry, pool)
    if not branch_work_ids:
        return None

    current_sha = getattr(entry, "branch_head_sha", None)
    current_patch_id = getattr(entry, "branch_patch_id", None)
    if current_patch_id is None:
        current_patch_id = _backfill_branch_patch_id(entry, gh_ops)
    if current_sha is None or current_patch_id is None:
        return None

    # Walk most-recently-dispatched first so a branch that's been through
    # more than one review-then-rebase cycle picks its latest approval as
    # the diff base, not an older one — a stale pick still produces a safe
    # (over-inclusive, never under-inclusive) delta, but a needlessly large
    # one. ``pool`` is otherwise unordered (completed + active concatenated).
    ordered = sorted(pool, key=lambda a: getattr(a, "dispatched_at", None) or 0, reverse=True)

    for a in ordered:
        if getattr(a, "type", None) != "review":
            continue
        if getattr(a, "review_of_assignment_id", None) not in branch_work_ids:
            continue
        if getattr(a, "review_verdict", None) != "approve":
            continue
        review_sha = getattr(a, "review_head_sha", None)
        if review_sha is None or review_sha == current_sha:
            continue  # not stale, or SHA tracking unavailable — not this path
        review_patch_id = getattr(a, "review_patch_id", None)
        if review_patch_id is None:
            continue  # fail closed — cannot confirm scope, full review
        if review_patch_id == current_patch_id:
            continue  # content-identical — has_approved_review already covers it
        return a  # approval voided ONLY by a content-changing rebase
    return None


def intervening_work_since_review(
    entry: "QueuedMerge", board, review: Assignment
) -> list[Assignment]:
    """Return the :data:`~coord.models.WORK_LIKE_TYPES` assignments in
    *entry*'s branch chain that were **dispatched after** *review* was — i.e.
    genuine new commits (a bounce/fix round, a fresh work dispatch), not a
    mechanical rebase.

    Extracted from :func:`only_conflict_fix_since_review` so callers that need
    to distinguish its two distinct "False" reasons can do so: a non-empty
    list means "another commit landed after the approval" (never reaffirmable
    without a re-review), whereas an empty list plus a ``False`` from
    ``only_conflict_fix_since_review`` merely means "no coord-tracked
    conflict-fix explains the delta" (e.g. the operator rebased by hand) —
    unattributable, but not evidence of new logic. ``#1488``'s
    ``coord review-reaffirm`` hard-refuses the former and warns loudly on the
    latter; the automated dispatcher (``#1476``) declines both.

    Dispatch order, not completion order, is compared — see
    :func:`only_conflict_fix_since_review` for why.
    """
    pool = list(getattr(board, "completed", []) or []) + list(getattr(board, "active", []) or [])
    branch_work_ids = _chain_work_ids(entry, pool)
    review_dispatched_at = getattr(review, "dispatched_at", None)
    if review_dispatched_at is None:
        return []

    out: list[Assignment] = []
    for a in pool:
        if getattr(a, "type", None) not in WORK_LIKE_TYPES:
            continue
        if getattr(a, "assignment_id", None) not in branch_work_ids:
            continue
        a_dispatched_at = getattr(a, "dispatched_at", None)
        if a_dispatched_at is not None and a_dispatched_at > review_dispatched_at:
            out.append(a)
    return out


def only_conflict_fix_since_review(entry: "QueuedMerge", board, review: Assignment) -> bool:
    """True when the sole thing that changed *entry*'s branch since *review*
    approved it was one or more successful conflict-fix rebases (#1476's
    scoping guardrail) — i.e. a scoped review is safe to dispatch.

    False (⇒ the caller must fall back to a full review) when:

    - No successful (``status="done"``) conflict-fix for this merge entry is
      found at all — there is nothing to attribute the content change to,
      and guessing would be unsound.
    - Any other :data:`~coord.models.WORK_LIKE_TYPES` assignment in the
      branch's work chain (a fix/bounce round, a fresh work dispatch — i.e.
      a genuine new commit, not a rebase) was dispatched after *review* ran.

    Dispatch order, not completion order, is what's compared against
    *review*'s own dispatch time — a fix round that was *in flight* when the
    review was dispatched (and so is exactly what the review covered) must
    not itself disqualify the scoped path; only a fix/work round that
    started **after** the approval counts as "another commit".
    """
    if intervening_work_since_review(entry, board, review):
        return False  # a new work/fix round happened — not conflict-fix-only

    pool = list(getattr(board, "completed", []) or []) + list(getattr(board, "active", []) or [])
    review_dispatched_at = getattr(review, "dispatched_at", None)

    for a in pool:
        if getattr(a, "type", None) != "conflict-fix":
            continue
        if getattr(a, "review_of_assignment_id", None) != entry.assignment_id:
            continue
        if getattr(a, "status", None) != "done":
            continue
        a_dispatched_at = getattr(a, "dispatched_at", None)
        if (
            review_dispatched_at is not None
            and a_dispatched_at is not None
            and a_dispatched_at < review_dispatched_at
        ):
            continue  # a conflict-fix from BEFORE this review isn't relevant
        return True
    return False


# ── Smoke gate (#465) ──────────────────────────────────────────────────────

def requires_smoke(entry: "QueuedMerge", config) -> bool:
    """True when *entry* must have an interactive smoke verdict before merging.

    Honours the *effective* gate list — ``entry``'s own ``required_gates``
    when set, falling back to ``config.pipeline.default_gates`` otherwise
    (#1213; see :func:`requires_review` for the duck-typing/fallback
    contract shared by both gates).  When ``"test"`` is in the resolved
    gate list the user must record ``coord test --passed`` (or ``--skip``)
    before ``coord merge`` proceeds.  ``"test"`` absent → gate disabled.
    """
    pipeline = getattr(config, "pipeline", None)
    if pipeline is None:
        return False
    gates = getattr(entry, "required_gates", None) or (pipeline.default_gates or [])
    return "test" in gates


# ── UAT gate (#2687) ─────────────────────────────────────────────────────────
#
# A human-attended gate for customer-facing repos: the operator must click
# through the PR's deployed preview and record a verdict before merge — the
# thing that would have caught natal-chart#42 (a shipped visual defect no
# automated gate could see). Two-part opt-in, deliberately AND-ed together
# rather than either one alone being sufficient:
#
#   1. "uat" appears in the entry's effective gate list (required_gates, or
#      config.pipeline.default_gates) — the fleet-wide half, same mechanism
#      requires_review/requires_smoke use.
#   2. The entry's OWN repo has Repo.uat_preview OR Repo.uat_live_preview
#      configured — the per-repo half (#2948: either one alone is a full
#      opt-in; a repo needs neither to leave the gate off). This is what
#      keeps "ship the mechanism with the default off everywhere" true even
#      if an operator adds "uat" to default_gates (fleet-wide) without
#      meaning to turn it on for every repo: a repo with neither set never
#      blocks, no matter what default_gates says.
#
# Unlike requires_review/requires_smoke, there is no SHA/patch-id staleness
# tracking here (see evaluate_uat_verdict's docstring) — a UAT verdict is a
# human's judgment on a rendered preview, not a re-runnable measurement.

def _uat_repo_for(entry, config):
    """Best-effort ``config.repo(entry.repo_name)`` lookup for the UAT gate.

    Mirrors the existing ``try/except`` duck-typing convention this module
    already uses for the identical lookup in ``_staging_smoke_entry`` — a
    *config* that is a real ``coord.config.Config`` (every production
    caller) always has ``.repo()``, but a minimal test double or a future
    duck-typed stand-in might not, and an unknown/malformed repo name is
    "can't confirm this repo opted in", not a crash.
    """
    if config is None:
        return None
    try:
        return config.repo(getattr(entry, "repo_name", None))
    except Exception:  # noqa: BLE001 — unknown repo: no live lookup possible
        return None


def requires_uat(entry: "QueuedMerge", config) -> bool:
    """True when *entry* must have a recorded UAT verdict before merging.

    See the module-comment above this function for the two-part opt-in.
    Duck-typed on ``entry.repo_name``/``entry.required_gates``, matching
    :func:`requires_review`/:func:`requires_smoke`.

    #2948: the per-repo half is now ``uat_preview`` (the override template)
    OR ``uat_live_preview`` (opt-in to the live GitHub-Deployment lookup) —
    either one alone is a full opt-in; a repo needs neither to leave the gate
    off, matching the pre-#2948 behaviour of an unset ``uat_preview``.
    """
    pipeline = getattr(config, "pipeline", None)
    if pipeline is None or config is None:
        return False
    gates = getattr(entry, "required_gates", None) or (pipeline.default_gates or [])
    if "uat" not in gates:
        return False
    repo = _uat_repo_for(entry, config)
    if repo is None:
        return False
    return bool(repo.uat_preview) or bool(getattr(repo, "uat_live_preview", False))


def _uat_branch_work(entry: "QueuedMerge", board) -> list:
    """Work-like assignments in *entry*'s branch chain, most-recently-
    dispatched first — the population :func:`evaluate_uat_verdict` reads
    ``uat_state``/``uat_reason`` from. Mirrors the board walk
    :func:`evaluate_smoke_verdict` opens with."""
    pool = list(getattr(board, "completed", []) or []) + list(getattr(board, "active", []) or [])
    branch_work_ids = _chain_work_ids(entry, pool)
    branch_work = [
        a for a in pool
        if getattr(a, "assignment_id", None) in branch_work_ids
        and getattr(a, "type", None) in WORK_LIKE_TYPES
    ]
    branch_work.sort(key=lambda a: getattr(a, "dispatched_at", None) or 0, reverse=True)
    return branch_work


def _resolve_uat_preview_url(
    entry: "QueuedMerge", config, gh_ops: "GhOps | None"
) -> str | None:
    """Resolve the preview URL to surface for *entry* (#2948).

    Resolution order:

    1. ``Repo.uat_preview`` (:meth:`coord.models.Repo.resolve_uat_preview_url`)
       — an explicit operator override, for a repo whose preview host has a
       genuinely templatable URL. Always wins when set.
    2. ``Repo.uat_live_preview`` — the live GitHub-Deployment lookup
       (:func:`coord.github_ops.get_pr_deployment_url`, via *gh_ops*),
       matched on environment name rather than recency. Requires both a
       *gh_ops* and a known branch; returns ``None`` on any read failure
       rather than raising.

    Returns ``None`` when neither resolves — never a guessed/constructed
    URL (the #2948 bug: a template placeholder that renders a plausible but
    dead link).
    """
    repo = _uat_repo_for(entry, config)
    if repo is None:
        return None
    if repo.uat_preview:
        return repo.resolve_uat_preview_url(
            branch=getattr(entry, "branch", None),
            issue_number=getattr(entry, "issue_number", None),
            pr_number=getattr(entry, "pr_number", None),
        )
    branch = getattr(entry, "branch", None)
    if getattr(repo, "uat_live_preview", False) and gh_ops is not None and branch:
        try:
            return gh_ops.get_pr_deployment_url(entry.repo_github, branch)
        except Exception:  # noqa: BLE001 — no URL to report, not a crash
            return None
    return None


def evaluate_uat_verdict(
    entry: "QueuedMerge", board, config, gh_ops: "GhOps | None" = None
) -> tuple[bool, str]:
    """Return ``(ok, message)`` for *entry*'s UAT-gate state.

    ``ok`` is True only when the most recent verdict on the branch's work
    chain is ``"passed"``. ``message`` (populated only when not ``ok``) is
    the whole point of #2687's "surface the URL where the operator already
    looks": it names the missing/failed verdict, the repo's real preview URL
    (see :func:`_resolve_uat_preview_url` for the #2948 resolution order),
    and the exact ``coord uat`` command to clear it — so a caller can print
    it verbatim instead of sending the operator hunting for the link.

    #2948: when *neither* resolution path produces a URL, the message says
    so explicitly rather than silently omitting the "— preview:" clause (the
    pre-#2948 behaviour when a template placeholder rendered but pointed
    nowhere real) — the whole point being that an unresolvable preview must
    read as unresolved, never as a plausible dead link. *gh_ops* is optional
    (``None`` by default) so callers that are deliberately I/O-free (e.g.
    :func:`display_error`'s read-only recompute) can still call this and get
    the override-template path, just not the live lookup.

    Fails CLOSED when no work assignment can be identified for the branch
    (unlike :func:`evaluate_smoke_verdict`, which fails open there) — same
    posture as the review gate's :func:`has_approved_review`: a verdict
    this gate exists specifically to force a human to record is never
    assumed to exist merely because the board can't prove otherwise.
    """
    branch_work = _uat_branch_work(entry, board)
    aid = getattr(entry, "assignment_id", None)
    uat_state: str | None = None
    uat_reason: str | None = None
    if branch_work:
        # Most-recently-dispatched row carrying ANY verdict wins — mirrors
        # find_scoped_review_candidate's "latest wins" rationale: a bounce/
        # fix round's fresh work assignment is a new thing for the operator
        # to look at, so an older sibling's stale verdict must not paper
        # over it.
        for a in branch_work:
            state = getattr(a, "uat_state", None)
            if state:
                uat_state = state
                uat_reason = getattr(a, "uat_reason", None)
                aid = getattr(a, "assignment_id", None) or aid
                break
    if uat_state == "passed":
        return True, ""

    preview_url = _resolve_uat_preview_url(entry, config, gh_ops)
    if uat_state == "failed":
        reason_part = f": {uat_reason}" if (uat_reason or "").strip() else ""
        message = f"uat verdict FAILED{reason_part}"
    else:
        message = "uat verdict missing"
    if preview_url:
        message += f" — preview: {preview_url}"
    else:
        message += (
            " — preview URL could not be resolved (no uat_preview override "
            "configured and no matching GitHub Deployment found for this "
            "branch)"
        )
    message += f" — run: coord uat {aid or '<assignment-id>'} --passed|--failed"
    return False, message


# ── Gate-bypass auditing (#1213) ────────────────────────────────────────────

def _bypassed_gates(entry: "QueuedMerge", config) -> list[str]:
    """Which of the default pipeline's gates *entry*'s resolved gate list
    drops.

    Returns ``[]`` when ``entry`` carries no override (``required_gates``
    empty/absent — falls back to ``config.pipeline.default_gates``, nothing
    to bypass) or when its resolved gates already match the default list.
    Only ``"review"``, ``"test"``, and ``"uat"`` are reported — ``"merge"``
    is the terminal action being gated, not a checkpoint that can be
    "bypassed".

    ``"review"`` is reported only when ``config.reviews.enabled`` is truthy
    — mirroring the guard :func:`requires_review` applies first. When review
    is globally disabled, dropping ``"review"`` from a label's resolved gate
    list changes nothing (the gate was already off), so it isn't a real
    bypass and reporting it would produce a misleading audit row / CLI note
    (#1213 review finding 1). ``"uat"`` (#2687) gets the same treatment,
    mirroring :func:`requires_uat`'s guard instead: reported only when the
    entry's own repo has ``uat_preview`` or ``uat_live_preview`` configured
    (#2948: either is a full per-repo opt-in).
    """
    gates = getattr(entry, "required_gates", None)
    if not gates:
        return []
    pipeline = getattr(config, "pipeline", None) if config is not None else None
    default_gates = list(getattr(pipeline, "default_gates", None) or []) if pipeline else []
    reviews_enabled = bool(getattr(config, "reviews", None)) and bool(
        getattr(config.reviews, "enabled", True)
    )
    # #2687: only look up the repo when "uat" is actually a candidate — same
    # short-circuit requires_uat applies, and it means a config stand-in
    # with no `.repo()` (an older test double, a future duck-typed caller)
    # never has to grow one just because this function now knows about a
    # gate it will filter out on the very next line anyway.
    uat_configured = "uat" in default_gates and "uat" not in gates and bool(
        (repo := _uat_repo_for(entry, config)) is not None
        and (repo.uat_preview or getattr(repo, "uat_live_preview", False))
    )
    candidates = [
        g for g in ("review", "test", "uat") if g in default_gates and g not in gates
    ]
    if not reviews_enabled:
        candidates = [g for g in candidates if g != "review"]
    if not uat_configured:
        candidates = [g for g in candidates if g != "uat"]
    return candidates


def _bypass_label(entry: "QueuedMerge", config) -> str | None:
    """Best-effort reverse lookup of the ``pipeline.labels`` key that
    produced *entry*'s resolved ``required_gates``, for a readable audit
    row / CLI message.

    Returns ``None`` when no exact match is found (the label was renamed or
    removed from config after enqueue time, or ``pipeline.labels`` is
    empty) — the audit event and CLI note still fire without a name in that
    case, since the gate list itself is the durable evidence.  Ambiguous
    when two labels resolve to the same gate list — the first match (dict
    iteration order) wins; this is display-only and never affects gate
    enforcement.
    """
    pipeline = getattr(config, "pipeline", None) if config is not None else None
    labels = getattr(pipeline, "labels", None) if pipeline else None
    gates = getattr(entry, "required_gates", None)
    if not labels or not gates:
        return None
    for label, label_gates in labels.items():
        if list(label_gates) == list(gates):
            return label
    return None


def _bypass_note(entry: "QueuedMerge", config) -> str:
    """Human-readable suffix naming any bypassed gate, or ``""`` when none.

    Appended to the ``coord merge`` "merged" event message (real and
    dry-run) so a bypass is never silent (#1213).  Side-effect free — the
    audit row itself is written separately, only on a real (non-dry-run)
    merge, by the caller in :func:`process`.
    """
    bypassed = _bypassed_gates(entry, config)
    if not bypassed:
        return ""
    label = _bypass_label(entry, config)
    label_desc = f"label {label!r}" if label else "an issue-label override"
    return f" [gate bypass via {label_desc}: {', '.join(bypassed)} skipped]"


def _record_gate_bypass_audit(entry: "QueuedMerge", config) -> list[str]:
    """Emit one ``gate_bypassed`` business-tier audit row per bypassed gate
    set, and return the bypassed gate names (``[]`` if none).

    Called once per real merge success in :func:`process` — never in
    dry-run, so previews never write phantom audit rows.  ``record_audit``
    is itself best-effort (never raises), matching every other write
    choke point in :mod:`coord.state`.
    """
    bypassed = _bypassed_gates(entry, config)
    if not bypassed:
        return []
    label = _bypass_label(entry, config)
    label_desc = f"label {label!r}" if label else "an issue-label override"
    record_audit(
        tier="business",
        category="gate",
        event_type="gate_bypassed",
        actor="user",
        summary=(
            f"Gate bypass via {label_desc}: {', '.join(bypassed)} skipped "
            f"for {entry.repo_name}#{entry.issue_number}"
        ),
        repo=entry.repo_name,
        issue=entry.issue_number,
        assignment_id=entry.assignment_id,
        details={
            "label": label,
            "resolved_gates": list(getattr(entry, "required_gates", None) or []),
            "bypassed_gates": bypassed,
        },
    )
    return bypassed


def _record_ci_flake_audit(entry: "QueuedMerge", pending_json: str) -> None:
    """Emit one ``ci_flake_detected`` operational audit row (#2252): a check
    that failed once, was re-run exactly once, and came back clean.

    *pending_json* is the JSON blob :func:`process` stashed on
    ``entry.ci_flaky_pending`` at the moment it triggered the re-run — the
    failing check names/conclusions and the branch SHA they failed against,
    captured then because ``branch_head_sha`` is transient (recomputed every
    tick, never persisted) and the checks themselves may already read
    differently on ``gh`` by the time the re-run resolves.

    This audit row is the thing #2252 asks for explicitly: without a durable
    record, a check that is flaky 30% of the time gets silently waved
    through every time instead of surfacing as a repeat offender someone
    should fix — ``record_audit`` (queryable via ``coord audit`` /
    ``query_audit_log``) is that durable record. Best-effort like every
    other audit call site in this module: ``record_audit`` never raises,
    and a malformed/missing blob (should not happen — this module is the
    only writer of ``ci_flaky_pending``) degrades to an empty ``checks``
    list rather than raising into the merge loop.
    """
    try:
        payload = json.loads(pending_json) if pending_json else {}
    except (TypeError, ValueError):
        payload = {}
    record_audit(
        tier="operational",
        category="ci",
        event_type="ci_flake_detected",
        actor="system",
        summary=(
            f"CI flake on {entry.repo_name}#{entry.issue_number} "
            f"(PR #{entry.pr_number}): failed once, passed on re-run — "
            "zero drive attempts spent (#2252)"
        ),
        repo=entry.repo_name,
        issue=entry.issue_number,
        assignment_id=entry.assignment_id,
        details={
            "pr_number": entry.pr_number,
            "sha": payload.get("sha"),
            "checks": payload.get("checks", []),
        },
    )


@dataclass(frozen=True)
class MergeGateFailure:
    """One un-satisfied merge gate for a work row / queue entry (#1695).

    :func:`passes_merge_gates` collapses this to a bool, which is all the
    gate *decision* ever needed. What it never carried was *why* — so the
    ``coord merge`` auto-enqueue scan's ``if not passes_merge_gates(...):
    continue`` printed nothing at all, and an operator staring at a branch
    in ``--dry-run`` that ``--only`` could not address had no statement of
    the cause anywhere (#1695's 40-minute diagnosis).

    ``waiver_flag`` is the ``coord merge`` flag that waives this gate **at
    merge time** — the whole point of #1695 being that the gate and its
    override must live at the same stage. It is a display string; nothing
    here waives anything.

    #2687: ``"uat"`` has no waiver flag at all — recording an actual verdict
    (``coord uat <id> --passed``) is the only way through, by design (an
    unattended bypass would defeat the entire point of the gate). Its
    ``waiver_flag`` carries that command instead of a ``coord merge`` flag —
    the field is repurposed as "the thing that clears this", not narrowed to
    literally "a waiver", so :func:`describe_merge_gate_failures` still
    prints one actionable line per failure without a third field.
    """

    gate: str          # "review" | "smoke" | "uat"
    reason: str        # short human-readable cause
    waiver_flag: str   # "--skip-review" | "--skip-smoke" | "coord uat <id> --passed"

    def __str__(self) -> str:
        return f"{self.gate} gate — {self.reason} (waive with {self.waiver_flag})"


def merge_gate_failures(
    a,
    config,
    board,
    gh_ops: "GhOps | None" = None,
    stop_early: bool = False,
) -> list[MergeGateFailure]:
    """Every merge gate *a* has NOT satisfied, in :func:`process` order.

    The reason-carrying form of :func:`passes_merge_gates` — that function is
    now literally ``not merge_gate_failures(..., stop_early=True)``, so the
    two can never disagree about whether a row is gated (the #946 drift this
    predicate exists to prevent).

    Returns ``[]`` when every configured gate is satisfied (or none is
    configured — each gate no-ops when ``requires_*`` is False).

    *stop_early* returns as soon as the first failure is found, preserving
    :func:`passes_merge_gates`'s original short-circuit so the boolean path
    never pays for a second gate evaluation (and, with *gh_ops* supplied,
    never makes a second round trip) just to answer yes/no.
    """
    failures: list[MergeGateFailure] = []
    if requires_review(a, config):
        review_scan = scan_approved_reviews(a, board, gh_ops)
        if not review_scan.approved:
            # #2704: `unknown_head` means the branch head could not be
            # confirmed at all — never collapse that into "not approved",
            # which asserts a refusal this scan never actually confirmed.
            failures.append(MergeGateFailure(
                gate="review",
                reason=(
                    # #2809: `a` is the live-anchored entry `live_gate_entry`
                    # built — carries the confirmed probe error, when there
                    # was one, so this names the real cause instead of the
                    # generic sentence.
                    unknown_branch_head_reason(getattr(a, "branch_head_probe_error", None))
                    if review_scan.unknown_head
                    else "review required but not approved"
                ),
                waiver_flag="--skip-review",
            ))
            if stop_early:
                return failures
    if requires_smoke(a, config):
        smoke = evaluate_smoke_verdict(a, board, gh_ops)
        if not smoke.ok:
            failures.append(MergeGateFailure(
                gate="smoke",
                reason=smoke.short_reason or "test verdict missing",
                waiver_flag="--skip-smoke",
            ))
            if stop_early:
                return failures
    if requires_uat(a, config):
        uat_ok, uat_message = evaluate_uat_verdict(a, board, config, gh_ops)
        if not uat_ok:
            aid = getattr(a, "assignment_id", None) or "<id>"
            failures.append(MergeGateFailure(
                gate="uat",
                reason=uat_message,
                waiver_flag=f"coord uat {aid} --passed",
            ))
            if stop_early:
                return failures
    return failures


def describe_merge_gate_failures(failures: "list[MergeGateFailure]") -> str:
    """Render *failures* as one operator-readable clause.

    ``""`` when *failures* is empty, so call sites can interpolate it
    unconditionally.
    """
    return "; ".join(str(f) for f in failures)


def passes_merge_gates(a, config, board, gh_ops: "GhOps | None" = None) -> bool:
    """True when *a* (a work ``Assignment`` or ``QueuedMerge`` entry) has
    satisfied every gate required before it may merge.

    Shared predicate (#946) so untested/unreviewed work can never *merge*
    through any path — previously each of the enqueue/merge call sites
    (the daemon's :func:`enqueue_approved_work`, the ``coord merge``
    auto-enqueue loop, the raw :func:`enqueue` helper, :func:`process`)
    re-derived this logic and drifted: only the daemon path actually gated,
    so untested/unreviewed work could sneak into the queue via
    ``coord merge``.

    #1695 narrows *where* a False answer is allowed to act. It still refuses
    the merge (:func:`process` and :func:`_entry_gate_status` are unchanged —
    a row that fails here can never be merged by any automatic path), but the
    ``coord merge`` auto-enqueue scan no longer treats it as "drop this row
    on the floor": the row is enqueued in a visibly BLOCKED state so it is
    addressable by ``--only``, where ``--skip-review``/``--skip-smoke`` can
    waive the gate. Enqueueing changes an entry's *visibility*, never its
    *eligibility*.

    Duck-typed on ``entry.assignment_id`` / ``entry.branch`` (both
    ``Assignment`` and ``QueuedMerge`` have them), matching
    :func:`requires_review` / :func:`has_approved_review` / :func:`requires_smoke`
    / :func:`has_smoke_verdict`, which this composes.

    *gh_ops* (optional, #1601) is forwarded to both gates so a live SHA/
    patch-id lookup can back a fresh ``QueuedMerge`` entry that hasn't been
    through :func:`process` yet — see :func:`has_smoke_verdict`'s docstring.
    """
    return not merge_gate_failures(a, config, board, gh_ops, stop_early=True)


# ── Smoke-verdict outcome kinds (#1640) ─────────────────────────────────────
# The gate has always been a bool; #1640 splits the *failure* into the two
# cases an operator has to act on differently:
#
#   SMOKE_MISSING — nothing terminal was ever recorded. Run the Test stage.
#   SMOKE_STALE   — a passing verdict EXISTS but was recorded against a
#                   branch/base combination that no longer exists (#1479).
#                   Re-verify against the current base, then re-record.
#                   (#1732: a ``skipped`` verdict is never SMOKE_STALE — it's
#                   a structural claim about the diff, not a measurement at
#                   a SHA, so it can't go stale when the base moves.)
#
# Before #1640 both collapsed to "smoke test required but no verdict
# recorded", which is a false statement in the stale case and is exactly what
# made #1640 get filed as a lost DB write.
#
# #2704 adds a third: SMOKE_UNKNOWN — a recorded verdict exists, but the LIVE
# branch/base SHA needed to check it for staleness could not be read at all
# (GitHub unreachable, `gh` unauthenticated, or a rate limit), as opposed to
# being read and confirmed unchanged. Before #2704 this case fell through
# every staleness compare below (each one requires the current SHA to be
# non-``None`` to run) straight to SMOKE_OK — a verdict this code never
# actually confirmed still covers the current head. See
# `UNKNOWN_BRANCH_HEAD_REASON`.
SMOKE_OK = "ok"
SMOKE_MISSING = "missing"
SMOKE_STALE = "stale"
SMOKE_UNKNOWN = "unknown"


def _short_sha(sha: str | None) -> str:
    """7-char display form of *sha*, or ``"unknown"`` when it isn't known."""
    if not sha:
        return "unknown"
    return sha[:7]


@dataclass(frozen=True)
class SmokeVerdictStatus:
    """Structured outcome of the smoke gate for one entry (#1640).

    ``ok`` is what :func:`has_smoke_verdict` returns; the remaining fields
    exist so every surface that renders the refusal (``coord merge``, ``coord
    merge --plan``, the ``/board`` staging section, the TUI) can say *which*
    failure it is and against what, instead of all of them printing the
    "no verdict recorded" wording that only fits :data:`SMOKE_MISSING`.

    ``anchor`` is ``"base"`` when the merge base moved out from under the
    verdict (the #1479-specific condition — the common one on a sequential
    drain, since every merge moves the base for the next entry) and
    ``"branch"`` when the branch's own content changed since the test ran.

    #1819 adds a third anchor, ``"run"``: no terminal verdict exists, but the
    row is pinned at the transient ``test_state="running"`` marker (#1395)
    with no Test worker left alive to resolve it. That is an *abandoned*
    verdict, not an absent one — see :data:`RUNNING_MARKER_STALE_AFTER`.

    #2704: ``anchor`` is also set (to ``"base"`` or ``"branch"``) on a
    :data:`SMOKE_UNKNOWN` result — it names which side's live SHA could not
    be read, mirroring the STALE case, even though (unlike STALE) there is
    no ``current_sha`` to report since that is exactly what is unknown.

    ``spared_reason`` is the mirror image, set only on a passing (``ok``)
    verdict when the merge base *did* move but one of the #1479 escape
    hatches proved the move couldn't have invalidated the verdict — #1738
    (base move inert), #1778 (branch inert), or #1847 (base move and branch
    touch disjoint files). ``None`` whenever the base didn't move at all, so
    the common unremarkable-fresh case stays silent. See
    :func:`_base_move_spared` for the three wordings.
    """

    ok: bool
    kind: str  # SMOKE_OK | SMOKE_MISSING | SMOKE_STALE | SMOKE_UNKNOWN
    assignment_id: str | None = None
    anchor: str | None = None  # "base" | "branch" | "run" (SMOKE_STALE/SMOKE_UNKNOWN only)
    recorded_sha: str | None = None
    current_sha: str | None = None
    spared_reason: str | None = None  # set only on `ok=True` after a base move (#1847)
    # #2809: the confirmed transient error behind a SMOKE_UNKNOWN verdict —
    # often `GhRateLimitError` — so `short_reason`/`message` below can report
    # the real status/request-id/retry-after instead of only the generic
    # sentence. `None` for every other kind, and for a SMOKE_UNKNOWN whose
    # `gh_ops` stand-in didn't support `raise_on_transient` at all.
    probe_error: "GhTransientError | None" = None

    @property
    def short_reason(self) -> str | None:
        """Compact wording for plan / staging rows (``PlannedMerge.reason``).

        ``None`` when the gate passes.
        """
        if self.ok:
            return None
        if self.kind == SMOKE_UNKNOWN:
            return unknown_branch_head_reason(self.probe_error)
        if self.kind == SMOKE_STALE:
            if self.anchor == "run":
                return (
                    "test verdict stale (Test stage stuck at 'running' with no "
                    "live worker)"
                )
            noun = "base" if self.anchor == "base" else "branch"
            return (
                f"test verdict stale (recorded against {noun} "
                f"{_short_sha(self.recorded_sha)}, {noun} now "
                f"{_short_sha(self.current_sha)})"
            )
        return "test verdict missing"

    @property
    def message(self) -> str | None:
        """Full wording for a merge attempt (``QueuedMerge.error`` / the
        ``smoke_required`` event message). ``None`` when the gate passes."""
        if self.ok:
            return None
        if self.kind == SMOKE_UNKNOWN:
            # #2704: never fabricate SMOKE_OK on evidence this call never
            # actually obtained — see `UNKNOWN_BRANCH_HEAD_REASON`. #2809:
            # `unknown_branch_head_reason` appends real status/request-id/
            # retry-after when `probe_error` carries any.
            return unknown_branch_head_reason(self.probe_error)
        if self.kind == SMOKE_STALE:
            aid = self.assignment_id or "<assignment>"
            if self.anchor == "run":
                return (
                    "smoke test verdict is stale: the Test stage has been "
                    "marked 'running' since before the last Test worker for "
                    "this branch stopped, so no verdict is coming — re-verify "
                    f"against the current base, then `coord test {aid} "
                    "--passed`"
                )
            noun = "base" if self.anchor == "base" else "branch"
            return (
                f"smoke test verdict is stale: recorded against {noun} "
                f"{_short_sha(self.recorded_sha)}, {noun} is now "
                f"{_short_sha(self.current_sha)} — re-verify against the "
                f"current base, then `coord test {aid} --passed`"
            )
        return "smoke test required but no verdict recorded"


# ── Stale-vs-missing: the ONE implementation (#1769) ────────────────────────
#
# Both wordings a stale (as opposed to never-recorded) smoke verdict can be
# reported under are produced *by this module*: `SmokeVerdictStatus.message`
# ("smoke test verdict is stale: …", what `process()` stores on `entry.error`
# and what lands on the board as `merge_reason`) and
# `SmokeVerdictStatus.short_reason` ("test verdict stale (…)", what `plan()` /
# the staging rows render). So the predicate that recognises them belongs
# here, next to the code that emits them, and NOT copied into every consumer.
#
# #1738 put a private copy in `coord/drive.py` to give `coord drive` its
# re-test arm. #1769 adds the second consumer — `coord merge --revalidate` —
# and a *third* string-matching copy in a third module is exactly how #1141
# went stale, so the copy was lifted here instead: `coord.drive` and
# `coord.revalidate` both import THIS function, and `tests/test_merge_queue.py`
# asserts they are the same object.
#
# Deliberately a strict subset of `coord.drive._SMOKE_GATE_MARKERS`, which
# also matches "no verdict at all" ("smoke test required" / "test verdict
# missing"). Only the stale case has a safe, bounded, automatable fix:
# re-verify against the CURRENT base and let a fresh verdict land. A
# missing-verdict block is the #1640 lost-write shape instead — the driver and
# the gate disagree about whether a verdict exists at all — which a re-test
# cannot safely paper over, so it still escalates to a human.
STALE_SMOKE_MARKERS = ("smoke test verdict is stale", "test verdict stale")


def is_stale_smoke_reason(reason: str | None) -> bool:
    """True when *reason* names a STALE (not missing) smoke verdict.

    The single implementation of the stale-vs-missing distinction over merge
    *prose* (#1738/#1769). The structured form of the same question is
    ``evaluate_smoke_verdict(...).kind == SMOKE_STALE``; this string-matching
    variant exists only for the consumers whose input is a persisted
    ``merge_reason``/``entry.error`` rather than a live gate evaluation.
    """
    r = (reason or "").lower()
    return any(marker in r for marker in STALE_SMOKE_MARKERS)


# #1738: paths whose content cannot affect a pytest/cargo test result — the
# allowlist a base-SHA move is checked against before staling an otherwise-
# fresh verdict. Deliberately small and additive (start conservative, widen
# later if a real false-stale shows up outside it). Two shapes:
#   - a directory prefix ("docs/", "scripts/", ...) — everything under it,
#     recursively, is inert;
#   - a bare top-level filename pattern ("*.md") — matches ONLY files with no
#     directory component. This is why `tests/acceptance/foo.md` is NOT
#     inert despite the `.md` extension: extension alone never qualifies, only
#     a top-level `*.md` (README.md, CONTRIBUTING.md, ...) does. Any other
#     path — including any `coord/**`, `tests/**`, `tui/**`, `pyproject.toml`,
#     or `.github/workflows/**` — stales the verdict exactly as before.
_INERT_BASE_DIR_PREFIXES = ("docs/", "scripts/", ".github/ISSUE_TEMPLATE/")

# #1778: explicit deny-list that takes precedence over
# `_INERT_BASE_DIR_PREFIXES` — the executable test surface the Test stage
# actually runs. `scripts/` is otherwise allowlisted wholesale, which made
# `scripts/coord-test-runner.sh` inert by omission: a base move touching
# only the runner didn't stale (wrong — the runner IS what the suite runs),
# and worse, a *branch* that edits the runner could point at this same
# allowlist to declare itself untestable and skip its own gate
# (self-certification). An explicit deny beats trimming the allowlist
# because the next executable script added under `scripts/` inherits the
# safe (non-inert) default instead of silently inheriting the hole.
_INERT_DENY_PATHS = frozenset({
    "scripts/coord-test-runner.sh",
})


def _path_is_inert(path: str) -> bool:
    """True when *path* matches the #1738 inert-base allowlist.

    Checked on both the base-move side (:func:`_base_move_is_inert`) and the
    branch side (:func:`_branch_is_inert`) — the deny-list in
    `_INERT_DENY_PATHS` is consulted first so it wins over the directory
    allowlist on either side (#1778).
    """
    if path in _INERT_DENY_PATHS:
        return False
    if any(path.startswith(prefix) for prefix in _INERT_BASE_DIR_PREFIXES):
        return True
    return "/" not in path and path.endswith(".md")


def _fetch_compare_files(
    gh_ops: "GhOps | None",
    repo_github: str | None,
    base_sha: str | None,
    head_sha: str | None,
) -> list[str] | None:
    """Fetch the file list for one ``get_compare_files(base_sha, head_sha)``
    compare, failing closed to ``None`` (never raising) whenever the answer
    can't be established: no *gh_ops*/*repo_github* to ask, a missing SHA on
    either side, the call raising, or it returning ``None`` (unreadable)
    itself.

    The single I/O seam behind :func:`_base_move_is_inert`,
    :func:`_branch_is_inert`, and (#1847) :func:`_base_move_disjoint_from_branch`
    — all three ultimately ask "what files does this compare touch" of one of
    the same two compares (``test_base_sha..current_base_sha`` or
    ``test_base_sha..test_head_sha``), so :func:`_base_move_spared` fetches
    each side through here at most once and shares the result across every
    predicate that consults it, rather than each predicate fetching its own
    copy.

    Note: :class:`coord.gate_snapshot.GateSnapshot` (the ``/board`` display
    path's ``gh_ops`` stand-in) does not yet cache ``get_compare_files``, so a
    plain ``AttributeError`` lands here and this fails closed exactly as it
    does for a genuine lookup failure — the display can show STALE for a
    base move that a live ``coord merge``/``coord drive`` (real
    ``coord.github_ops``) correctly treats as fresh. That's the safe
    direction of disagreement (pessimistic display, correct live gate) —
    the opposite of the #1640 incident — but wiring this cache through
    :class:`~coord.gate_snapshot.GateSnapshotRefresher` would close it too.
    """
    if gh_ops is None or not repo_github or not base_sha or not head_sha:
        return None
    try:
        return gh_ops.get_compare_files(repo_github, base_sha, head_sha)
    except Exception:  # noqa: BLE001 — fail-safe: unknown diff is not "inert"/disjoint
        return None


def _files_are_inert(files: list[str] | None) -> bool:
    """True when *files* — an already-fetched compare file list — is
    non-``None`` and every entry passes :func:`_path_is_inert`.

    ``None`` (an unreadable compare) fails closed to ``False``. Factored out
    of :func:`_base_move_is_inert`/:func:`_branch_is_inert` (#1847) so the
    same predicate can run against a list :func:`_base_move_spared` fetched
    once, instead of each of the three #1479 escape hatches fetching (and
    re-checking) its own copy.
    """
    if files is None:
        return False
    return all(_path_is_inert(f) for f in files)


def _base_move_is_inert(
    gh_ops: "GhOps | None", repo_github: str | None, old_sha: str, new_sha: str
) -> bool:
    """True when every file the base moved through (*old_sha*..*new_sha*) is
    provably inert (#1738) — content that cannot alter a test result, so a
    fresh verdict recorded against *old_sha* still covers *new_sha*.

    Fails closed (returns ``False``, i.e. "not proven inert, stale as
    before") whenever inertness can't be established: no *gh_ops*/*repo_github*
    to ask, the compare call raises, or it comes back ``None`` (unreadable) or
    empty-but-unconfirmed. The bar set by #1738 is "bias hard toward staling":
    a false "fresh" merges untested code; a false "stale" only costs a re-run.

    Thin composition of :func:`_fetch_compare_files` + :func:`_files_are_inert`
    — kept as its own function (rather than inlined at the one call site) so
    it stays independently testable and so :func:`_base_move_spared`'s
    single-fetch orchestration reads as "the same predicates, sharing one
    fetch" rather than a parallel implementation.
    """
    return _files_are_inert(_fetch_compare_files(gh_ops, repo_github, old_sha, new_sha))


def _branch_is_inert(
    gh_ops: "GhOps | None",
    repo_github: str | None,
    base_sha: str | None,
    branch_sha: str | None,
) -> bool:
    """True when a branch's entire diff against its merge-base is provably
    inert (#1778) — content the suite cannot see, so
    ``suite(base + branch) ≡ suite(base)`` and re-running the suite over a
    moved base tells you nothing about *this branch* that wasn't already
    known.

    This is the mirror of :func:`_base_move_is_inert`: that function asks
    "did the base move through anything that matters"; this one asks "does
    the branch touch anything that matters", independent of whether the base
    moved at all. The three are consulted together at the base-move staling
    check (#1479/#1847) by :func:`_base_move_spared` — any one being true is
    enough to skip staling on a base move alone. None of the three replace
    the separate branch-*content*-changed check (patch-id comparison) that
    follows: a branch that is inert today and later gains a `coord/**`
    commit is caught by that check, not this one.

    Fails closed exactly like :func:`_base_move_is_inert` — returns
    ``False`` ("not proven inert, stale as before") whenever inertness can't
    be established: no *gh_ops*/*repo_github*, a raising compare call, or a
    ``None`` (unreadable) result. A false "fresh" would let an untested
    branch merge; a false "stale" only costs a redundant re-run.

    Reuses :func:`_path_is_inert` for the allowlist, which is why the
    `_INERT_DENY_PATHS` exclusion of `scripts/coord-test-runner.sh` matters
    here specifically: without it, a branch that edits the composed test
    runner could point at the `scripts/` allowlist and declare its own diff
    untestable, skipping the gate it is trying to evade.
    """
    return _files_are_inert(
        _fetch_compare_files(gh_ops, repo_github, base_sha, branch_sha)
    )


def _base_move_disjoint_from_branch(
    base_files: list[str] | None, branch_files: list[str] | None
) -> bool:
    """True when the files the base moved through and the files the branch
    touches share no path (#1847) — the third #1479 base-move escape hatch,
    alongside :func:`_base_move_is_inert` and :func:`_branch_is_inert`.

    Those two are allowlist-based: each asks "is this one diff inert on its
    own". This asks a different, cheaper-to-satisfy question — "do these two
    diffs have anything to do with each other" — which is the shape that
    actually costs a human intervention on a queue drain: a substantive base
    move and a substantive branch that simply never touch the same file.

    Fails closed to ``False`` when either list is ``None`` (an unreadable
    compare on either side), matching the fail-closed posture of the other
    two checks: a false "disjoint" would let an untested base/branch
    combination merge; a false "overlapping" only costs a redundant re-run.

    File-level disjointness is *not* semantic independence — a base change
    to one module can still break a branch that never names it (there is no
    compiler to catch a moved signature at this granularity, and a shared
    `conftest.py` fixture makes it worse). Two things bound that risk enough
    to accept it here rather than requiring semantic analysis:

    * CI already tests the *composite*. `.github/workflows/test.yml` runs
      pytest and `.github/workflows/cargo-test.yml` runs cargo test on every
      `pull_request` push, built against branch-merged-into-base, and
      `coord merge` gates on those checks independently via
      `coord.ci_store.CiStore` — the local Test verdict this function spares
      is substantially re-deriving what CI already proves.
    * GitHub does not re-run PR workflows when only the *base* moves (only
      on head `synchronize`), which is a real gap — but it is the SAME gap
      `_base_move_is_inert`/`_branch_is_inert` already accept for their own
      allowlisted content, not a new one this check introduces. Closing it
      (re-running CI on a base move) is out of scope here.
    """
    if base_files is None or branch_files is None:
        return False
    return set(base_files).isdisjoint(branch_files)


def _base_move_spared(
    gh_ops: "GhOps | None",
    repo_github: str | None,
    test_base_sha: str,
    current_base_sha: str,
    test_head_sha: str | None,
) -> tuple[bool, str | None]:
    """Whether a moved base still spares a `passed` verdict recorded against
    *test_base_sha*, and — when it does — why.

    Tries the three #1479 escape hatches in order, stopping at the first
    that fires: #1738 (:func:`_base_move_is_inert`), #1778
    (:func:`_branch_is_inert`), #1847 (:func:`_base_move_disjoint_from_branch`).
    Ordered cheapest-first and fetch-sharing on purpose: the base-move file
    list is fetched once and checked for #1738 before the branch file list is
    fetched at all; the branch file list, once fetched for #1778, is reused
    for #1847 rather than re-fetched. At most two `get_compare_files` calls
    total per invocation, regardless of which disjunct fires or whether none
    do — same worst case as the pre-#1847 `_base_move_is_inert(...) or
    _branch_is_inert(...)` this replaces at the call site.

    #2705 considered a fourth escape hatch here — "the base move is this
    entry's own already-tested content landing" — but nothing this module
    can ask (only file-*name* compares via `get_compare_files`, no commit
    ancestry, no local git checkout) can tell "the base absorbed exactly
    this branch's diff" apart from "the base absorbed a DIFFERENT change to
    the same file(s)"; the file-set-subset heuristic that was tried here
    collapsed onto exactly the case
    `_base_move_disjoint_from_branch`'s own existing test suite deliberately
    keeps STALE (overlapping-but-not-identical file sets), so it would have
    been an unsound, test-regressing entry in this chain, not a real one.
    See :func:`evaluate_smoke_verdict`'s ``state == MERGED`` short-circuit
    for how #2705's actual reported case (quadraui#595) is handled instead —
    at the "is this entry already merged" layer, not by inferring it from a
    compare diff.
    """
    base_files = _fetch_compare_files(
        gh_ops, repo_github, test_base_sha, current_base_sha
    )
    if _files_are_inert(base_files):
        return True, "base move touches only inert paths (#1738)"
    branch_files = _fetch_compare_files(
        gh_ops, repo_github, test_base_sha, test_head_sha
    )
    if _files_are_inert(branch_files):
        return True, "branch touches only inert paths (#1778)"
    if _base_move_disjoint_from_branch(base_files, branch_files):
        return True, "base move and branch touch disjoint files (#1847)"
    return False, None


# #1851: the reason string prefix `_entry_gate_status` returns for a CI-stale
# entry. Both the wording and the eligibility check in
# :func:`ci_revalidation_candidates` key off this constant so the two can
# never drift apart the way #1141 warns about (see `STALE_SMOKE_MARKERS`'s
# comment above for the same lesson learned the hard way for the smoke gate).
CI_STALE_PREFIX = "CI stale:"


# #1891: the reason string prefix `_entry_gate_status` (board-render time)
# and `process()`'s live `checks_pending` event (real merge-attempt time)
# BOTH use for "checks exist on GitHub but have not reported a conclusion
# yet" — as opposed to `checks_failed` (a check that DID report, and
# reported red). This is the one piece of vocabulary #1891's incident was
# missing: a CI verdict that has not arrived was indistinguishable from one
# that arrived and said no, so a drive spent its bounded merge-attempt
# budget (and then a drive-queue launch attempt) retrying a merge that only
# more real time — never another retry — could resolve.
#
# `IssueState.merge_reason` (`drive_state._merge_entry`) already falls back
# from the live plan's freshly-recomputed `reason` to the raw queue row's
# *persisted* `error` whenever the plan's own re-evaluation comes back
# empty — which is exactly what happens when `_gate_refresher`'s
# periodically-refreshed snapshot (`coord/gate_snapshot.py`) lags or gaps a
# live `coord merge` attempt's own fresher read. That makes `merge_reason`
# — not `merge_status`, which has no such fallback — the robust signal to
# key off. `coord.drive._decide_merge` and `coord.drive_queue`'s `parked`
# outcome both import :func:`is_ci_pending_reason` below so the two can
# never drift apart the way #1141 warns about.
CI_PENDING_PREFIX = "CI running:"


def is_ci_pending_reason(reason: str | None) -> bool:
    """True when *reason* names checks that exist but have not reported a
    conclusion yet (#1891) — as opposed to a check that ran and failed.

    The single implementation of the pending-vs-failed distinction over merge
    prose, the same posture :func:`is_stale_smoke_reason` takes for the smoke
    gate: callers whose input is a persisted ``merge_reason``/``entry.error``
    string (not a live ``CheckRun`` list) use this instead of re-deriving it.
    """
    return (reason or "").startswith(CI_PENDING_PREFIX)


# #2347: the reason string prefix for a `checks_failed` block whose failing
# check(s) are ALL the #1525 synthetic "could not read CI status"/"gh too
# old" stand-ins (`coord.ci_store.is_unreadable_check`) — the check-list
# FETCH itself failed (a transient `gh pr checks` HTTP 5xx, an auth blip,
# a rate limit), never a real CI verdict of any shape. Before this existed,
# such a failure fell all the way through to the plain "checks failed: ..."
# wording — indistinguishable from a genuine red suite — and, because
# `coord.drive_queue`'s #1891/#2182 park machinery keys off exactly
# `CI_PENDING_PREFIX`/`CI_INFRA_PREFIX` and nothing else, never got their
# "still shut, self-refreshing, no attempt spent" treatment either. The
# observed incident (see the issue): a fully green, already-mergeable PR
# sat `parked` for most of `PARK_STALE_SECONDS` behind a run of transient
# GitHub API 503s, showing a stale "CI running: ..." reason the whole time
# because nothing ever recomputed it.
#
# Distinct from BOTH siblings it could otherwise be confused with:
# `CI_PENDING_PREFIX` ("checks exist on GitHub and are genuinely still
# running") and `CI_INFRA_PREFIX` ("a check DID complete on GitHub, but said
# nothing about the code") — this one means GitHub could not even be asked
# the question. See :func:`_ci_unreadable_reason` for the classification.
CI_UNREADABLE_PREFIX = "CI unreadable:"


def is_ci_unreadable_reason(reason: str | None) -> bool:
    """True when *reason* names a `checks_failed` block whose failing
    check(s) are ALL the #1525 synthetic "could not read CI status"
    stand-in — GitHub could not be reached, not a real CI verdict (#2347).
    See :data:`CI_UNREADABLE_PREFIX`."""
    return (reason or "").startswith(CI_UNREADABLE_PREFIX)


# #1904: every CI gate predicate — `failed_checks`, `in_flight_checks`,
# `_ci_checks_are_stale` — is a filter *over* `checks`, so `checks == []`
# satisfies all three vacuously and the pre-#1904 gate fell all the way
# through to "merge". `checks == []` is genuinely ambiguous (see
# `CiStore.expects_checks`'s docstring): "no CI configured for this repo"
# (correct to merge) and "CI exists but never triggered for this PR" (a
# throttled webhook, a wedged run, a path-filtered-out workflow — wrong to
# merge) both produce it. `CI_ABSENT_PREFIX` names the second reading,
# distinctly from `CI_PENDING_PREFIX` ("checks exist, still running") and
# `CI_STALE_PREFIX` ("checks exist, green, but predate the base") — an
# operator (and `coord drive`) needs to know nothing was ever triggered, not
# that something is still in flight.
#
# #1877: `checks == []` has a THIRD reading — the PR conflicts with its
# base, so GitHub never built a merge ref to run a `pull_request`-triggered
# workflow against at all. Unlike the "never triggered" reading above, this
# one is self-healing: routing it to the #241 conflict-fix path (rather
# than blocking here) is the cure. `process()` and `_entry_gate_status`
# both consult `GhOps.check_pr_mergeable` before committing to the
# `CI_ABSENT_PREFIX` block, specifically to give this reading a chance to
# fall through first — see the `#1877` comments at each call site.
CI_ABSENT_PREFIX = "CI never ran:"


def is_ci_absent_reason(reason: str | None) -> bool:
    """True when *reason* names a PR whose CI was expected to run but never
    reported a single check (#1904) — as opposed to one that ran and failed
    (``checks_failed``), is still running (:func:`is_ci_pending_reason`), or
    ran stale (``CI_STALE_PREFIX``)."""
    return (reason or "").startswith(CI_ABSENT_PREFIX)


# #1892: the reason string prefix for a `checks_failed` entry whose failing
# checks carry NO VERDICT ABOUT THE CODE — every one of them either never got
# a runner (cancelled at the queue timeout, zero steps) or died at "Set up
# job" (before checkout, so no repo code ran). Distinctly named from the
# plain "checks failed: ..." wording a genuine red suite produces, so
# `coord.drive`'s retry accounting and `coord.drive_queue`'s `parked` state
# (both already keying off :data:`CI_PENDING_PREFIX`/`is_ci_pending_reason`
# for the #1891 "still running" case) can extend the identical treatment to
# this one: don't spend a merge attempt / launch attempt on a failure that
# says nothing about whether the branch is any good — see
# :func:`_ci_infra_reason` for the classification and `MAX_CI_INFRA_RERUNS`
# for the auto-rerun this reason also triggers.
#
# Deliberately NOT surfaced by `_entry_gate_status` (board-render time,
# consumed by `plan()`/`/board`): classifying this needs one extra
# `gh api .../actions/runs/{id}/jobs` call per distinct failing run
# (`CiStore.list_jobs_for_run`), and `coord.gate_snapshot`'s module
# docstring states Invariant 1 — the board *read* path performs no
# third-party I/O. Only the LIVE merge path (`process()`, which already
# pays for fresh truth — see that module's docstring) computes this and
# persists it onto `QueuedMerge.error`; `coord.drive_state._merge_entry`
# prefers that raw, more-specific reading over the plan's own generic
# re-derivation when the two diverge (mirrors the NEEDS_ATTENTION recovery
# a few lines into that function).
CI_INFRA_PREFIX = "CI infra:"


def is_ci_infra_reason(reason: str | None) -> bool:
    """True when *reason* names a `checks_failed` block whose failures were
    all classified verdictless (#1892) — see :data:`CI_INFRA_PREFIX`."""
    return (reason or "").startswith(CI_INFRA_PREFIX)


# #2252: the reason string prefix for a `checks_failed` entry whose failing
# check(s) carry a REAL verdict about the code — unlike `CI_INFRA_PREFIX`
# above — but have only been observed failing ONCE so far. `process()` has
# triggered exactly one scoped re-run of the failed job(s) (#2252's whole
# ask: "before a failed check consumes a drive attempt, re-run the failed
# job(s) once and re-read" — a 1-in-N flaky test reports the identical
# completed/failure verdict a genuine regression does, so the verdict alone
# can't tell them apart; a second, independent observation can) and is
# waiting on its answer.
#
# Distinctly named from the plain "checks failed: ..." wording a CONFIRMED
# failure produces (this entry's one-shot re-run budget already spent,
# still red on the second read) so `coord.drive`'s retry accounting and
# `coord.drive_queue`'s `parked` state can extend the SAME "self-refreshing,
# no attempt yet" treatment #1891/#1892 already give
# `CI_PENDING_PREFIX`/`CI_INFRA_PREFIX` to this one — see
# :func:`is_ci_flaky_reason` and `MAX_CI_FLAKY_RERUNS`.
CI_FLAKY_PREFIX = "CI re-checking:"


def is_ci_flaky_reason(reason: str | None) -> bool:
    """True when *reason* names a `checks_failed` block currently mid its
    ONE #2252 re-run to rule out a flake — see :data:`CI_FLAKY_PREFIX`."""
    return (reason or "").startswith(CI_FLAKY_PREFIX)


def is_ci_terminal_reason(reason: str | None) -> bool:
    """True when *reason* is a CONCLUDED CI verdict — a plain "checks
    failed: ..." (or similarly final) reading carrying none of the four
    self-refreshing, no-verdict-yet prefixes above (#2556).

    ``not is_ci_terminal_reason(r)`` is exactly "this reading is still
    genuinely in flight, or mid one of its own bounded self-refreshing
    windows, and answers nothing yet about whether the code is any good" —
    :func:`is_ci_pending_reason` (checks exist and are still running),
    :func:`is_ci_infra_reason` (a check completed but said nothing about the
    code, mid its own auto-rerun), :func:`is_ci_flaky_reason` (a real
    verdict, but only observed once, mid its one-shot re-run to rule out a
    flake), or :func:`is_ci_unreadable_reason` (GitHub could not even be
    asked the question). Anything else — most commonly a plain "checks
    failed: ..." with none of those prefixes — is terminal: a concluded
    result the queue can act on now, never indistinguishable from a slow or
    still-resolving one.

    The single implementation of this classification. Two call sites used to
    each spell out the identical four-way disjunction inline — a `parked`
    entry's "is this still self-refreshing?" gate in this module's own
    `process()`, and `coord.drive_queue.plan_tick`'s #2556 resume-on-
    terminal-failure branch — which is exactly the "two independent
    implementations that agree today are a split-brain waiting to happen"
    shape this repo's own review checklist warns against. Call this instead
    of re-deriving the disjunction at a new call site.
    """
    return not (
        is_ci_pending_reason(reason)
        or is_ci_infra_reason(reason)
        or is_ci_flaky_reason(reason)
        or is_ci_unreadable_reason(reason)
    )


def ci_rollup_all_clear(summary: Any) -> bool:
    """``True`` when a ``merge_plan`` row's ``ci_summary`` positively shows
    every check on that PR has finished and none of them failed (#2158,
    generalised by #2808).

    *summary* is the wire form of :class:`coord.ci_store.CiCheckSummary` —
    ``asdict``'d into the ``/board`` payload by ``serve_app`` — or ``None``
    (no PR, no ``ci_store``, or a gate snapshot that has not fetched this PR
    yet). Anything that is not a readable rollup, or a rollup with nothing in
    it at all, returns ``False``: callers use this as evidence AGAINST a
    persisted "CI running: ..."/"CI infra: ..."/"CI re-checking: ..."
    reading, and absence of a rollup is not evidence. ``passed > 0`` (rather
    than ``passed + failed > 0``) with ``failed == 0`` is the same "all
    green" reading :func:`_entry_gate_status` arrives at when it returns
    ``PLAN_READY``.

    #2808: originally private to :mod:`coord.drive_queue` (as
    ``_ci_rollup_all_clear``), where it recovers ``coord drive-queue``'s
    `parked` reconciliation from the exact same bug this generalises for
    :func:`coord.drive_state._merge_entry` — a fresh, empty ``reason`` from
    ``_entry_gate_status`` (``PLAN_READY``, nothing blocking) getting
    discarded in favour of a raw queue row's ``error`` string that only a
    LIVE ``coord merge`` attempt ever rewrites, and #1891's "wait, don't
    retry" contract means nothing ever runs one again while the frozen
    reading itself is what keeps the driver waiting. Moved here — the
    ``CiCheckSummary`` wire-form reader belongs beside the other CI-reason
    predicates, not duplicated per caller (the exact "two independent
    implementations that agree today are a split-brain waiting to happen"
    shape :func:`is_ci_terminal_reason` above was already written to avoid).
    """
    if not isinstance(summary, Mapping):
        return False
    try:
        passed = int(summary.get("passed") or 0)
        failed = int(summary.get("failed") or 0)
        running = int(summary.get("running") or 0)
    except (TypeError, ValueError):  # a malformed rollup is not evidence
        return False
    return running == 0 and failed == 0 and passed > 0


# #1892: auto-reruns `process()` will trigger for a single entry's verdictless
# CI failure (via `CiStore.rerun_for_pr`) before giving up and parking it for
# a human instead of the queue's own #1891 machinery. A workflow genuinely
# broken at the "Set up job" level — a bad `uses:` ref, a deleted action —
# would otherwise loop this forever; two tries is enough to ride out a queue-
# timeout blip or a transient "Service Unavailable" without masking a
# standing breakage.
MAX_CI_INFRA_RERUNS = 2

# #2197: same shape as MAX_CI_INFRA_RERUNS above, but for the OTHER CI
# auto-rerun trigger `process()` supports — a PASSING check recorded against
# a base that has since moved (:data:`CI_STALE_PREFIX`, #1851's staleness
# signal), not a failure. Deliberately a SEPARATE constant/counter from
# `ci_infra_reruns`: the two triggers answer opposite readings of CI ("this
# failed and needs to prove itself again" vs. "this passed but predates the
# base and needs a fresh answer") and must be independently capped and
# independently legible in the audit trail. A base that keeps moving out
# from under one PR (a busy queue, or a genuinely wedged branch) would
# otherwise auto-rerun forever; two tries rides out an ordinary busy tick
# without masking a PR that just isn't going to catch up unattended.
MAX_CI_STALE_RERUNS = 2

# #2252: at most one auto-rerun per failure streak before a genuinely-
# verdicted (non-infra) `checks_failed` spends the drive attempt exactly as
# it does today. "One re-run, not a retry loop" is the issue's own bound:
# two independent observations (the original run and this one re-run) is
# enough to tell "broken" from "flaky" without starting to mask code that is
# genuinely, repeatedly broken — a higher cap here would blur back into the
# thing #1892's own cap already guards against for the verdictless case.
MAX_CI_FLAKY_RERUNS = 1

# #2347: bounded count of consecutive LIVE `process()` attempts that have
# observed a bare check-list FETCH failure for a single entry — mirrors
# `MAX_CI_INFRA_RERUNS`'s shape (same cap: a queue-timeout-shaped GitHub
# blip clears in a couple of ticks, not ten). Unlike `MAX_CI_INFRA_RERUNS`
# there is no remedy action to spend this budget on — `CiStore.rerun_for_pr`
# reruns a CI *workflow*, and nothing about a transport failure reading
# GitHub is fixed by re-running one — so reaching the cap does not fall back
# to the generic "checks failed" wording the way #1892's own cap does (that
# collapse back into a plain CI verdict is exactly what #2347 exists to
# stop). It only escalates the WORDING from "retrying automatically" to
# "this has been failing for a while, worth a human glance" — the entry
# stays a bare, no-attempt-spent wait either side of the cap, because there
# is genuinely nothing else `coord merge`/`coord drive` can do about it but
# wait for GitHub to answer again.
MAX_CI_UNREADABLE_RERUNS = 2


def _ci_unreadable_reason(failed: "list[CheckRun]") -> str | None:
    """The :data:`CI_UNREADABLE_PREFIX` reason when EVERY check in *failed*
    is the #1525 synthetic "could not read CI status" stand-in (#2347),
    else ``None``.

    Unlike :func:`_ci_infra_reason`, this needs no extra `CiStore` call —
    :func:`coord.ci_store.is_unreadable_check` reads straight off the
    `CheckRun` objects `list_checks_for_pr` already returned. That means
    (unlike the #1892 infra classifier, deliberately confined to the live
    merge path — see that function's docstring) BOTH `_entry_gate_status`
    (the board/plan *read* path, `coord.gate_snapshot`'s Invariant 1: no
    third-party I/O) and `process()` (the live merge path) can call this
    directly with identical results, so the board's own fresh reading and a
    live `coord merge` attempt's reading never disagree about whether this
    is "GitHub unreachable" — no raw-row reason recovery needed the way
    #1892/#2252 require for `coord.drive_state`/`coord.drive_queue`.
    """
    if not failed:
        return None
    if not all(is_unreadable_check(c) for c in failed):
        return None
    summary = ", ".join(f"{c.name} ({c.conclusion})" for c in failed)
    return (
        f"{CI_UNREADABLE_PREFIX} {summary} — GitHub could not be reached to "
        "read CI status; this is not a CI result"
    )


# #2380: a bare check-list FETCH failure (`_ci_unreadable_reason`, above) has
# TWO readings, exactly like `checks == []` already does (#1877): "GitHub is
# genuinely unreachable right now" (real, self-healing — keep retrying), and
# "this PR is DIRTY/CONFLICTING against its base, so GitHub can never build a
# merge ref for it and `gh pr checks` has nothing to read" (definitive,
# self-*never*-healing — no amount of retrying a CI read produces CI that
# will never run). The two are indistinguishable from the check list alone;
# GitHub's own `mergeable` field is the one thing that tells them apart, and
# — unlike CI — it is always computable, conflict or not.
def _pr_reports_conflicting(gh_ops, repo: str | None, number: int | None) -> bool:
    """True only when GitHub's live ``mergeable`` field reads ``CONFLICTING``
    for PR *number* — definitive, readable evidence a CI-unreadable park can
    never resolve on its own (#2380).

    Duck-typed the same way the sibling #1877 checks-absent conflict probe
    (a few lines above each of this function's call sites) already is:
    ``gh_ops`` on the board/plan read path may be a :class:`~coord.gate_
    snapshot.GateSnapshot` with no ``check_pr_mergeable`` at all (#1336
    Invariant 1 — no third-party I/O there), and any of "no probe available",
    "the probe raised", or a ``True``/``None`` verdict (cleanly mergeable, or
    GitHub still computing it) must read as "not confirmed conflicting" —
    never as false evidence of one. This is intentionally narrower than the
    design's "``mergeable == CONFLICTING`` or ``mergeStateStatus == DIRTY``"
    framing: :func:`coord.github_ops.check_pr_mergeable` already reads GitHub's
    ``mergeable`` field, which is exactly ``CONFLICTING`` whenever
    ``mergeStateStatus`` would read ``DIRTY`` — one probe, one `gh` call,
    same readable-and-definitive signal either way.
    """
    probe = getattr(gh_ops, "check_pr_mergeable", None)
    if probe is None or not repo or number is None:
        return False
    try:
        return probe(repo, number) is False
    except Exception:  # noqa: BLE001 — inconclusive, not a confirmed conflict
        return False


def _ci_infra_reason(
    ci: "CiStore", repo: str, number: int, failed: "list[CheckRun]"
) -> str | None:
    """The :data:`CI_INFRA_PREFIX` reason when EVERY check in *failed* is
    verdictless (#1892), else ``None``.

    Issues at most one :meth:`CiStore.list_jobs_for_run` call per distinct
    ``run_id`` among *failed* — never when *failed* is empty (the all-green
    or still-pending path), matching the "only on the failure path" scoping
    this feature was built to. ``getattr(ci, "list_jobs_for_run", None)``
    mirrors the same fail-closed-toward-"no answer" pattern
    ``_ci_checks_are_stale``/``_ci_expects_checks`` already use for a
    ``CiStore`` stand-in that predates a capability (a duck-typed test stub,
    or :class:`coord.gate_snapshot.GateSnapshot` — which deliberately does
    NOT implement this, per Invariant 1 above) — such a store just never
    produces this classification, falling back to the plain "checks failed"
    wording exactly like #1892 didn't exist for it.
    """
    if not failed:
        return None
    list_jobs = getattr(ci, "list_jobs_for_run", None)
    if list_jobs is None:
        return None
    run_ids = sorted({c.run_id for c in failed if c.run_id})
    jobs_by_run: dict[str, dict[str, JobRun]] = {}
    for run_id in run_ids:
        try:
            jobs = list_jobs(repo, run_id)
        except Exception:  # noqa: BLE001 — classification-only, never raises upward
            jobs = []
        jobs_by_run[run_id] = {j.name: j for j in (jobs or [])}
    all_verdictless = all(
        is_verdictless_job(c, jobs_by_run.get(c.run_id, {}).get(c.name))
        for c in failed
    )
    if not all_verdictless:
        return None
    summary = ", ".join(f"{c.name} ({c.conclusion})" for c in failed)
    return (
        f"{CI_INFRA_PREFIX} {summary} — no verdict about the code (never "
        "assigned a runner, or died before checkout)"
    )


def _ci_expects_checks(
    ci_store: "CiStore", repo_github: str | None, pr_number: int | None
) -> bool:
    """True when *ci_store* believes *repo_github*#*pr_number* should have
    reported at least one check (#1904) — i.e. an empty
    ``list_checks_for_pr`` result is suspicious, not a legitimate "no CI
    here" reading. Callers only consult this once ``list_checks_for_pr`` has
    already come back empty.

    ``getattr(..., None)`` mirrors `_ci_checks_are_stale`'s own fail-closed
    posture toward a ``CiStore``/``GhOps`` stand-in that predates a
    capability (see that function's ``get_branch_commit_timestamp`` probe):
    a store that hasn't been taught to answer this question yet reads as
    "checks were expected" rather than silently reopening the #1904 hole for
    any backend or test double this code doesn't already know about.
    :class:`coord.ci_store.NoOpCi` and :class:`coord.ci_github.GitHubCi`
    both implement this explicitly; so does
    :class:`coord.gate_snapshot.GateSnapshot`.
    """
    probe = getattr(ci_store, "expects_checks", None)
    if probe is None:
        return True
    try:
        return bool(probe(repo_github, pr_number))
    except Exception:  # noqa: BLE001 — fail closed: an erroring probe still means "assume expected"
        return True


def _base_commit_time(
    gh_ops: "GhOps | None",
    repo_github: str | None,
    target_branch: str | None,
) -> float | None:
    """*target_branch*'s current tip commit timestamp, or ``None`` when it
    cannot be read (#1826).

    The single place the base anchor is fetched, so :func:`_ci_checks_are_stale`
    (the predicate) and :func:`ci_staleness_note` (the #1479-parity wording)
    can never disagree about what "the current base" means. ``None`` covers
    every unreadable case uniformly — no *gh_ops*, no *target_branch*, a
    *gh_ops* stand-in with no ``get_branch_commit_timestamp`` (e.g.
    :class:`coord.gate_snapshot.GateSnapshot`, which deliberately doesn't
    cache it), or a live lookup that raises or returns ``None`` — and each
    caller decides for itself which way to lean on it.
    """
    if gh_ops is None or not repo_github or not target_branch:
        return None
    getter = getattr(gh_ops, "get_branch_commit_timestamp", None)
    if getter is None:
        return None
    try:
        return getter(repo_github, target_branch)
    except Exception:  # noqa: BLE001 — unreadable, never a raise out of a gate
        return None


def _ci_checks_are_stale(
    checks: "list[CheckRun]",
    gh_ops: "GhOps | None",
    repo_github: str | None,
    target_branch: str | None,
    smoke: "SmokeVerdictStatus | None",
    *,
    fail_closed: bool = True,
) -> bool:
    """True when *checks* — already confirmed by the caller to have no
    failed/in-flight entries — are stale relative to *target_branch*'s
    current base (#1851).

    GitHub does not re-run ``pull_request`` workflows on a base-only move
    (only on head ``synchronize``), so a green check can silently outlive the
    base commit it actually validated. See ``coord/ci_store.py``'s module
    docstring and :func:`coord.ci_store.checks_are_stale` for the full
    rationale; this function supplies the two pieces that predicate needs and
    can't fetch for itself — the base's current commit timestamp, and (#1847
    reuse) whether the base move can be dismissed without even reading one.

    *smoke*, when given, is the :class:`SmokeVerdictStatus` this same gate
    pass already computed for the entry's local Test verdict (evaluated only
    when the smoke gate applies). Its ``spared_reason`` is set exactly when a
    moved base was proven inert (#1738), the branch's own diff was proven
    inert (#1778), or the two diffs are file-disjoint (#1847) — any one of
    which spares the local verdict for the *same* base move, and the same
    reasoning spares the CI result: "the two checks answer the same question
    about different evidence" (#1851). When *smoke* is ``None`` or carries no
    ``spared_reason`` (smoke gate not required/evaluated for this entry, or
    the base move wasn't spared), this falls back to a pure timestamp
    comparison — the only evidence available without an anchor.

    Fails closed (returns ``True``) whenever the base anchor itself can't be
    read — see :func:`_base_commit_time` for the cases. That is the right
    lean for a *gate*: a false "fresh" merges untested code, a false "stale"
    only costs a re-run (:func:`coord.ci_store.checks_are_stale` documents the
    same bias). ``fail_closed=False`` inverts it for the one caller that is
    not a gate — the ``--force-merge`` waiver notice (#1826,
    :func:`ci_stale_waiver_message`), which is pure advisory prose printed
    beside a merge that is happening either way. There, an unreadable anchor
    must not manufacture a "stale CI is being waived" warning about a PR
    whose checks may well be current; only positive evidence of staleness is
    worth saying out loud. The flag governs *only* that unreadable-anchor
    arm — the comparison itself is still
    :func:`coord.ci_store.checks_are_stale`, never a second notion of stale.
    """
    if not checks:
        return False
    if smoke is not None and smoke.spared_reason is not None:
        return False
    base_commit_time = _base_commit_time(gh_ops, repo_github, target_branch)
    if base_commit_time is None:
        return fail_closed
    return checks_are_stale(checks, base_commit_time)


def _stale_stamp(ts: float | None) -> str:
    """UTC ``YYYY-MM-DDTHH:MM:SSZ`` for *ts*, or ``"unknown"`` (#1826).

    The CI analogue of :func:`_short_sha` — the anchor a CI result carries is
    a run timestamp, not a SHA (GitHub attaches `pull_request` checks to the
    PR's *merge ref*, whose base parent costs another round trip to resolve),
    so the staleness prose names times where the Test verdict's names commits.
    """
    if ts is None:
        return "unknown"
    try:
        return (
            datetime.fromtimestamp(float(ts), tz=timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    except (TypeError, ValueError, OSError, OverflowError):
        return "unknown"


def ci_staleness_note(
    checks: "list[CheckRun]",
    gh_ops: "GhOps | None",
    repo_github: str | None,
    target_branch: str | None,
) -> str:
    """The ``(ran against ... , ... now ...)`` clause for a CI-stale refusal
    (#1826), or ``""`` when the anchors can't be named.

    #1826's acceptance asks that stale CI be reported "in wording that matches
    #1479's Test-verdict staleness". #1479 prints *recorded against base X,
    base is now Y* (see :func:`coord.gates.format_report`); this is the same
    sentence with the anchors CI actually has — the oldest passing check's
    start time, and the base branch's current tip commit time.

    Costs one extra ``get_branch_commit_timestamp`` round trip, and is called
    **only** on the already-blocked stale path — never in the common,
    checks-are-fresh case, which is #1826's explicit "must not add latency to
    the common case" criterion.
    """
    base_commit_time = _base_commit_time(gh_ops, repo_github, target_branch)
    if base_commit_time is None:
        return ""
    started = [c.started_at for c in checks if c.started_at is not None]
    if not started or len(started) != len(checks):
        # A check with no `started_at` is exactly what makes
        # `checks_are_stale` fail closed; there is no honest anchor to print
        # for it, so print none rather than a partial one.
        return ""
    branch = target_branch or "base"
    return (
        f" (ran against {branch} as of {_stale_stamp(min(started))}, "
        f"{branch} now {_stale_stamp(base_commit_time)})"
    )


def ci_stale_reason(
    checks: "list[CheckRun]",
    gh_ops: "GhOps | None",
    repo_github: str | None,
    target_branch: str | None,
    *,
    suffix: str = "",
) -> str:
    """The single rendering of a CI-stale refusal (#1826).

    ``_entry_gate_status`` (board/plan render) and ``process()`` (the live
    merge attempt) both call this, so the two can never print different prose
    for the same condition — the #1141 lesson :data:`CI_STALE_PREFIX` already
    encodes for the machine-readable half, applied to the human half too.

    *suffix* is the extra clause the live path adds once its #2197 auto-rerun
    budget is spent; it lands before the remedy so the remedy stays the last
    thing an operator reads.
    """
    note = ci_staleness_note(checks, gh_ops, repo_github, target_branch)
    return (
        f"{CI_STALE_PREFIX} checks predate the current base{note}{suffix} — "
        "re-run CI (`coord merge --revalidate`) before merging"
    )


# #1826: `--force-merge` still overrides the CI gate, but the override must
# never be SILENT — the same posture `epic_closing_keyword_in_commit_forced`
# already takes for the #1318 gate. A waived stale-CI merge is precisely the
# 2026-08-04 incident's shape done deliberately, so the audit trail has to
# record that it was waived rather than leaving "merged, no CI event" to be
# misread as "merged, CI was green".
CI_STALE_WAIVED_PREFIX = "CI stale WAIVED:"


def ci_stale_waiver_message(
    entry: "QueuedMerge",
    ci_store: "CiStore | None",
    gh_ops: "GhOps | None",
    smoke: "SmokeVerdictStatus | None" = None,
) -> str | None:
    """Advisory message for a ``--force-merge`` that is waiving STALE CI
    (#1826), or ``None`` when there is nothing to warn about.

    Best-effort by construction: this runs on a merge that is going to happen
    regardless, so every unreadable input (no PR yet, GitHub unreachable, a
    ``CiStore``/``GhOps`` stand-in that can't answer) returns ``None`` rather
    than raising or guessing. Reuses :func:`_ci_checks_are_stale` — with
    ``fail_closed=False``, since a warning invented from an unreadable anchor
    is noise, not safety — so there is exactly one notion of "stale CI" in the
    merge lane, per #1826's own "two independent notions is how these diverge".

    Deliberately silent for failed/in-flight checks: ``--force-merge``
    overriding a RED or still-running suite is the pre-existing #240 override
    and already has its own semantics; this names the one condition that used
    to be invisible.

    Costs one check-list read plus a base-timestamp read — paid *only* under
    ``--force-merge``, a rare, deliberate, human-initiated act, and never on
    the ordinary gated path #1826 requires to stay latency-free.
    """
    if ci_store is None or not getattr(ci_store, "is_available", False):
        return None
    if not entry.pr_number:
        return None
    try:
        checks = ci_store.list_checks_for_pr(entry.repo_github, entry.pr_number)
    except Exception:  # noqa: BLE001 — advisory only, never blocks the merge
        return None
    if not checks or failed_checks(checks) or in_flight_checks(checks):
        return None
    try:
        stale = _ci_checks_are_stale(
            checks, gh_ops, entry.repo_github, entry.target_branch, smoke,
            fail_closed=False,
        )
    except Exception:  # noqa: BLE001 — advisory only, never blocks the merge
        return None
    if not stale:
        return None
    note = ci_staleness_note(checks, gh_ops, entry.repo_github, entry.target_branch)
    return (
        f"{CI_STALE_WAIVED_PREFIX} --force-merge is merging on checks that "
        f"predate the current base{note} — the CI gate was waived, NOT "
        "satisfied; nothing has tested this branch against "
        f"{entry.target_branch or 'the base'} as it stands now"
    )


def has_smoke_verdict(
    entry: "QueuedMerge", board, gh_ops: "GhOps | None" = None
) -> bool:
    """True when the smoke requirement for *entry* is satisfied.

    Thin ``.ok`` projection of :func:`evaluate_smoke_verdict` — kept as the
    boolean seam every existing gate call site already uses. Callers that
    need to *render* a refusal should call :func:`evaluate_smoke_verdict`
    directly so they can distinguish stale from missing (#1640).
    """
    return evaluate_smoke_verdict(entry, board, gh_ops).ok


def has_passed_test(entry: "QueuedMerge", board) -> bool:
    """True when a WORK_LIKE assignment in *entry*'s chain carries a
    recorded ``test_state == "passed"`` verdict.

    Deliberately NOT :func:`has_smoke_verdict` — that is the live SMOKE
    GATE (staleness re-derivation against the current branch/base SHAs, an
    unconditional ``skipped`` short-circuit, an optional *gh_ops* backfill).
    #2350's Merge-only fast path asks a narrower, cheaper question: does the
    board ALREADY show this issue's Test stage passed, as a second,
    independent confirmation layered on top of a live merge-gate-clear
    reading, not "would the smoke gate pass a fresh live check right now".
    Reusing :func:`has_smoke_verdict` here would also accept a fresh
    ``skipped`` verdict — a true smoke-gate pass, but not literally "Test
    passed" — which would auto-merge a case the issue never asked this fast
    path to cover.

    No *gh_ops* parameter: this never needs one, since it does no staleness
    check at all — a plain, cheap, board-only read, same posture as
    ``IssueFacts.merge_gate_status`` (:mod:`coord.drive_queue`).
    """
    pool = list(getattr(board, "completed", []) or []) + list(
        getattr(board, "active", []) or []
    )
    branch_work_ids = _chain_work_ids(entry, pool)
    if not branch_work_ids:
        return False
    for a in pool:
        if getattr(a, "assignment_id", None) not in branch_work_ids:
            continue
        if getattr(a, "type", None) not in WORK_LIKE_TYPES:
            continue
        if getattr(a, "test_state", None) == "passed":
            return True
    return False


#: #1819: how long a transient ``test_state="running"`` marker (#1395) may
#: outlive the last Test worker on its branch before the gate calls it STALE
#: rather than MISSING. The marker is written at dispatch and cleared when a
#: terminal verdict lands; if the worker died, was reaped, or its verdict write
#: was lost, nothing ever clears it and every gate reads "no verdict" forever
#: (#1797). ``--revalidate`` — the one tool built for exactly this cascade —
#: only ever touches STALE entries, so without this classification the
#: operator's escape hatch is unreachable from the state that most needs it
#: (the #1640 shape, in its load-bearing form).
#:
#: Generous on purpose: a real suite run is minutes, so an hour of slack still
#: never races a live worker, and the check ALSO requires that no live Test
#: worker for the branch remains on the board.
RUNNING_MARKER_STALE_AFTER = 60 * 60.0


def _abandoned_running_marker(
    branch_work: list,
    board,
    now: float | None = None,
) -> "Assignment | None":
    """The work row wedged at ``test_state="running"`` with no live Test worker.

    #1819. Returns the row whose Test stage can never resolve itself, or
    ``None`` when the marker is absent or a run is plausibly still going.

    Deliberately conservative in two ways:

    * it only fires when coord itself dispatched a Test-stage assignment for
      the branch. A ``running`` marker with **no** smoke assignment anywhere is
      the #1395 local-driver shape (``scripts/drive-issue.sh`` sets the marker
      and runs the suite in-process), and there is no worker row whose age
      could tell a live run from a dead one — so that case keeps the old
      MISSING classification rather than risk resetting a driver mid-run;
    * the window is measured from the newest Test worker's ``dispatched_at``
      and applies uniformly, whether that worker is still in ``board.active``
      or already reaped. A live-and-young smoke is obviously a run in
      progress; a *just*-reaped one still buys the notify path time to land
      the verdict it produced, so a lost write is never confused with a slow
      one.
    """
    running = [
        a for a in branch_work if getattr(a, "test_state", None) == "running"
    ]
    if not running:
        return None

    now = time.time() if now is None else now
    pool = list(getattr(board, "completed", []) or []) + list(
        getattr(board, "active", []) or []
    )
    active_ids = {
        getattr(a, "assignment_id", None)
        for a in (getattr(board, "active", []) or [])
    } - {None}
    work_ids = {
        getattr(a, "assignment_id", None) for a in branch_work
    } - {None}

    smokes = [
        a for a in pool
        if getattr(a, "type", None) == "smoke"
        and getattr(a, "review_of_assignment_id", None) in work_ids
    ]
    if not smokes:
        return None
    for s in smokes:
        if getattr(s, "assignment_id", None) not in active_ids:
            continue  # already reaped — cannot still be running
        if getattr(s, "status", None) in ("done", "failed", "cancelled"):
            continue
        if now - (getattr(s, "dispatched_at", None) or 0.0) < RUNNING_MARKER_STALE_AFTER:
            return None  # a Test worker is plausibly still going
    newest = max(smokes, key=lambda s: getattr(s, "dispatched_at", None) or 0.0)
    if now - (getattr(newest, "dispatched_at", None) or 0.0) < RUNNING_MARKER_STALE_AFTER:
        return None
    return running[0]


# #2704: True only for a `gh_ops.get_branch_sha` that actually declares the
# `raise_on_transient` kwarg (today: only `coord.github_ops.get_branch_sha`
# itself). Checked once per call via `inspect.signature` rather than just
# always passing the kwarg, because `GhOps` is a duck-typed `Protocol` with
# many concrete stand-ins — `coord.gate_snapshot.GateSnapshot` (whose
# cache-miss `None` is an unrelated, deliberate fail-open convention, #1640)
# and every test's ad hoc stub among them — and blindly passing an unknown
# keyword to a two-positional-argument stub raises `TypeError` before the
# stub's own body ever runs, silently discarding whatever SHA it would have
# returned. This keeps every such stand-in's exact existing behaviour byte
# for byte; only a `get_branch_sha` that opted in ever sees the kwarg.
_RAISE_ON_TRANSIENT_KW = "raise_on_transient"


def _gh_get_branch_sha(
    gh_ops: "GhOps", repo: str, branch: str
) -> tuple[str | None, bool, "GhTransientError | None"]:
    """``(sha, probe_failed_transiently, error)`` for one
    ``gh_ops.get_branch_sha`` call (#2704, extended by #2809).

    ``probe_failed_transiently`` is ``True`` only when *gh_ops* both
    declares support for ``raise_on_transient`` and raised
    :class:`~coord.github_ops.GhTransientError` for this call — a CONFIRMED
    transient failure (GitHub unreachable, ``gh`` unauthenticated, a rate
    limit), never a merely-empty result. See the module-level comment above
    for why support is detected rather than assumed.

    #2809: ``error`` is the caught exception itself (``None`` unless
    ``probe_failed_transiently``) — often the
    :class:`~coord.github_ops.GhRateLimitError` subclass, which carries the
    HTTP status/request-id/retry-after this issue asks to preserve rather
    than fold into a bare bool. Callers that only need the #2704 bool keep
    ignoring the third element; :func:`unknown_branch_head_reason` is what
    turns it into operator-facing detail.
    """
    fn = gh_ops.get_branch_sha
    try:
        supports = _RAISE_ON_TRANSIENT_KW in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        supports = False
    if not supports:
        try:
            return fn(repo, branch), False, None
        except Exception:  # noqa: BLE001 — fail-safe: unknown SHA is not blocking
            return None, False, None
    try:
        return fn(repo, branch, raise_on_transient=True), False, None
    except GhTransientError as exc:
        return None, True, exc
    except Exception:  # noqa: BLE001 — fail-safe: unknown SHA is not blocking
        return None, False, None


def evaluate_smoke_verdict(
    entry: "QueuedMerge", board, gh_ops: "GhOps | None" = None
) -> SmokeVerdictStatus:
    """Evaluate the smoke requirement for *entry*, with the reason it failed.

    The gate **fails open**: if no work assignment can be found on the board
    for the entry's branch (e.g. board was cleared, manual queue entry, or
    the assignment pre-dates board persistence), this returns ``ok=True`` so
    that the merge is not silently blocked without evidence.

    The gate **fails closed** (``ok=False``) only when we can positively
    identify the work assignment(s) on the branch and none of them carries a
    *fresh* ``test_state in ('passed', 'skipped')`` verdict.

    Collects all work assignment IDs connected to the entry — by shared
    branch, or (#567) by the ``review_of_assignment_id`` chain, which also
    catches fix workers dispatched with ``branch=NULL`` (the #557 remote-
    interactive-rework gap) — to handle bounce/fix-work chains.

    #1479: unlike the pre-existing behaviour, a terminal ``passed`` verdict is
    not trusted unconditionally — it is checked against the branch/base state
    it was recorded against (``test_head_sha``/``test_patch_id``/
    ``test_base_sha``, stamped by ``coord.state._record_test_verdict_local``)
    the same way ``has_approved_review`` checks ``review_head_sha``/
    ``review_patch_id``, **plus** one condition the review gate deliberately
    doesn't have: the *merge base itself* moving. A rebase onto a moved base
    can break tests without the branch's own diff changing at all (a
    semantic conflict — upstream renamed something the branch calls), so
    that combination no longer having been tested must re-block the gate
    even when ``branch_patch_id`` is unchanged. Content changing (new
    commits on the branch) also re-blocks, mirroring the review gate. Either
    anchor missing on either side skips that half of the check (fail open —
    #821/#1475's existing convention), so rows/entries predating this
    feature behave exactly as before.

    #1732: ``skipped`` is deliberately excluded from all of the above. It is
    not a measurement of code at a SHA the way ``passed`` is — it is a
    structural claim about the diff itself ("contract/fixture-only, nothing
    to smoke-test", #1076/#1152) that cannot be falsified by the base or
    branch moving. Treating it like ``passed`` meant a Gate-A slice approved
    and merge-ready would get refused as "STALE" with an unperformable
    remedy: there is no suite to re-run, and recording ``passed`` would be a
    lie about a suite that does not apply. A ``skipped`` verdict short-
    circuits straight to :data:`SMOKE_OK` before any SHA comparison runs.

    #1601: *gh_ops* (optional, mirroring :func:`has_approved_review`) fetches
    the branch's/base's *live* SHA (and, via :func:`_backfill_branch_patch_id`,
    the live patch-id) on demand when *entry* doesn't already carry them.
    Without it, an entry that has never been through a live :func:`process`
    pass has ``branch_head_sha``/``target_branch_head_sha``/``branch_patch_id``
    all ``None``, which makes every staleness check above a no-op — so
    ``coord merge --plan`` (which calls this via :func:`_entry_gate_status`
    on a freshly-enqueued entry) could show READY for a verdict that
    ``coord merge --only`` (whose :func:`process` DOES backfill these before
    checking) then correctly refuses as stale. Passing *gh_ops* through
    closes that "plan says ready, only refuses" disagreement — the #1566
    incident's reader 3 vs. reader 4 split.

    #1640: a *gh_ops* that cannot answer ``get_branch_sha`` /
    ``get_branch_patch_id`` (the daemon's :class:`coord.gate_snapshot.
    GateSnapshot` used to be exactly that) reopens the same disagreement
    through a different door — the ``except Exception`` fallbacks below
    swallow the ``AttributeError`` and every staleness check silently
    degrades to a no-op, so ``/board``'s plan shows READY while a live
    ``coord merge --only`` refuses. ``GateSnapshot`` now serves both lookups
    from its tick-refreshed data; keep that in lockstep if a new gh_ops
    stand-in is ever introduced.

    #2704: a *gh_ops* that answers a live SHA lookup with ``None`` — GitHub
    unreachable, ``gh`` unauthenticated, or a rate limit — is not the same
    as never having asked. Before #2704 that ``None`` looked identical to
    "no live lookup was needed/possible", so every staleness check below
    (each requires the live SHA to be non-``None`` to run) silently no-opped
    and this function reached :data:`SMOKE_OK` on a verdict it never
    actually confirmed. Now a genuinely *attempted-and-failed* lookup
    produces :data:`SMOKE_UNKNOWN` instead — fail closed, distinctly from
    both other failure kinds, so the caller (and, since #2704, ``coord
    drive``) can wait for GitHub to answer rather than treat an unreadable
    probe as either a pass or a plain missing/stale verdict.

    Returns a :class:`SmokeVerdictStatus`: ``ok`` plus, when it fails,
    whether the verdict is :data:`SMOKE_MISSING` (never recorded),
    :data:`SMOKE_STALE` (recorded, but against a branch/base combination
    that no longer exists — and the SHAs that disagree), or
    :data:`SMOKE_UNKNOWN` (recorded, but the live SHA needed to check it
    could not be read at all).

    #2705: an *entry* already carrying ``state == MERGED`` short-circuits to
    :data:`SMOKE_OK` before any SHA is looked at. A merge is exactly the
    event that moves ``target_branch``'s head — so re-running this same
    check on the entry that JUST performed that move reads its own merge
    back as "the base moved out from under the verdict" and reports a
    staleness the merge itself created (quadraui#595, 2026-08-24: a
    ``--revalidate`` run recorded a fresh ``passed`` verdict, merged clean,
    then this function — handed the same, now-``MERGED`` row by a later
    reader — refused it as stale against the base its own merge had just
    produced, naming a ``coord test ... --passed`` remedy for work that had
    already landed). There is nothing left to gate: a ``MERGED`` entry's
    code is already in the base, so no SHA comparison below can produce a
    meaningful refusal — the same posture ``coord.drive``'s
    ``_extract_already_merged`` (#2157) takes once a diagnostic confirms the
    merge already happened. ``process()`` itself never reaches this
    function for a non-``PENDING`` entry (it filters before grouping), so
    this guard is for every OTHER reader that hands this function a
    persisted queue row without first checking state — a live re-derivation
    of ``merge_reason``, a direct ``evaluate_smoke_verdict``/
    ``merge_gate_failures`` call, or a future caller that doesn't know to
    filter terminal rows out first.
    """
    if getattr(entry, "state", None) == MERGED:
        return SmokeVerdictStatus(ok=True, kind=SMOKE_OK)

    pool = list(getattr(board, "completed", []) or []) + list(
        getattr(board, "active", []) or []
    )

    branch_work_ids = _chain_work_ids(entry, pool)

    # Collect work assignments that are explicitly present on the board.
    branch_work = [
        a for a in pool
        if getattr(a, "assignment_id", None) in branch_work_ids
        and getattr(a, "type", None) in WORK_LIKE_TYPES
    ]
    # #2705: most-recently-dispatched first, mirroring `_uat_branch_work`.
    # `pool` order is `board.completed + board.active`, which is NOT
    # chronological — a bounce/fix chain leaves several terminal-verdict rows
    # in this list, and the `if stale is None:` latch below keeps only the
    # first one the loop sees. Without this sort that's whichever stale row
    # happens to sit earliest in pool order — frequently the oldest,
    # long-superseded round — so both the reported anchor and the
    # `coord test <aid> --passed` remedy name a row nobody is merging. Sorting
    # here makes "first stale row seen" and "winning (newest) row" the same
    # row, for every reader below: this loop, the SMOKE_MISSING fallback, and
    # `_abandoned_running_marker`.
    branch_work.sort(key=lambda a: getattr(a, "dispatched_at", None) or 0, reverse=True)
    # Fail open: no work assignment found → can't block without evidence.
    if not branch_work:
        return SmokeVerdictStatus(ok=True, kind=SMOKE_OK)

    current_base_sha = getattr(entry, "target_branch_head_sha", None)
    current_branch_sha = getattr(entry, "branch_head_sha", None)
    current_patch_id = getattr(entry, "branch_patch_id", None)
    repo_github = getattr(entry, "repo_github", None)
    entry_branch = getattr(entry, "branch", None)
    target_branch = getattr(entry, "target_branch", None)
    base_sha_attempted = current_base_sha is not None
    branch_sha_attempted = current_branch_sha is not None
    patch_id_attempted = current_patch_id is not None
    # #2704: True only when a live lookup *positively confirmed* a transient
    # failure (`GhTransientError`, opt-in via `raise_on_transient=True`) —
    # never merely because the lookup returned/left `None`. That keeps
    # `coord.gate_snapshot.GateSnapshot`'s deliberate cache-miss-is-`None`
    # fail-open convention (#1640) intact: it doesn't accept the kwarg, so
    # the call raises a plain `TypeError` there, caught by the generic
    # `except Exception` below and never mistaken for this.
    base_sha_probe_failed = False
    branch_sha_probe_failed = False
    # #2809: the actual exception behind each `*_probe_failed` bool above —
    # `None` until a probe confirms one — so the SMOKE_UNKNOWN verdicts built
    # below can carry the real status/request-id/retry-after through
    # `unknown_branch_head_reason` instead of just the generic sentence.
    base_sha_probe_error: "GhTransientError | None" = None
    branch_sha_probe_error: "GhTransientError | None" = None

    # #1640: the first row rejected purely for staleness, so a refusal can
    # name the case ("recorded at X, base now Y") instead of claiming no
    # verdict exists. Only set when a terminal verdict was actually found —
    # a board with no terminal verdict at all stays SMOKE_MISSING.
    stale: SmokeVerdictStatus | None = None
    # #2704: the first row rejected because a live SHA lookup this call
    # actually attempted came back empty — distinct from `stale` (which
    # means the lookup SUCCEEDED and the SHAs disagree) and reported ahead
    # of it below: "cannot confirm" must never lose to a `stale` verdict
    # found on some other row in the same chain, since neither tells this
    # caller the verdict is actually fresh.
    unknown: SmokeVerdictStatus | None = None

    # Work found — check whether any carries a fresh terminal smoke verdict.
    for a in branch_work:
        test_state = getattr(a, "test_state", None)
        if test_state not in ("passed", "skipped"):
            continue

        # #1732: `skipped` is a structural claim about the *shape of the
        # diff* ("contract/fixture-only, nothing to smoke-test" — #1076/
        # #1152), not a claim about code behaving correctly at a particular
        # SHA. Unlike `passed`, it does not decay when the base or branch
        # moves — a rename upstream, or new commits on the branch, can't
        # turn "there is nothing here a smoke test could exercise" into
        # false. Only `passed` verdicts go through the #1479 base/branch
        # staleness check below; `skipped` is accepted unconditionally.
        if test_state == "skipped":
            return SmokeVerdictStatus(
                ok=True, kind=SMOKE_OK, assignment_id=getattr(a, "assignment_id", None)
            )

        # Merge base moved: the tested combination (this branch + that base)
        # no longer exists, even if the branch's own diff is unchanged.
        test_base_sha = getattr(a, "test_base_sha", None)
        if (
            test_base_sha is not None
            and current_base_sha is None
            and not base_sha_attempted
            and gh_ops is not None
            and repo_github
            and target_branch
        ):
            current_base_sha, base_sha_probe_failed, base_sha_probe_error = _gh_get_branch_sha(
                gh_ops, repo_github, target_branch
            )
            base_sha_attempted = True

        # #2704: the live probe just above positively confirmed it could not
        # read GitHub, while a recorded verdict names a specific base SHA to
        # compare against. Before #2704 a failed (or merely un-attempted)
        # lookup was indistinguishable from a clean `None`, so this fell
        # straight through to the "base moved" check below, which is a
        # silent no-op on `current_base_sha is None` and lets execution
        # reach SMOKE_OK further down: a verdict this call never actually
        # confirmed still covers the current base. Fail closed instead — we
        # do not KNOW whether the base moved, so we cannot vouch for it.
        if test_base_sha is not None and base_sha_probe_failed:
            if unknown is None:
                unknown = SmokeVerdictStatus(
                    ok=False,
                    kind=SMOKE_UNKNOWN,
                    assignment_id=getattr(a, "assignment_id", None),
                    anchor="base",
                    recorded_sha=test_base_sha,
                    probe_error=base_sha_probe_error,
                )
            continue

        # #1738/#1778/#1847: the base moved, but a moved SHA doesn't
        # necessarily mean a content change that could affect a test result.
        # `_base_move_spared` tries, in order: is the base move itself
        # provably inert content (docs/scripts/issue-template only, #1738);
        # failing that, is *this branch*'s entire diff (as actually tested,
        # test_base_sha..test_head_sha) provably inert (#1778); failing that,
        # do the two diffs simply touch disjoint files (#1847) — a
        # substantive base move and a substantive branch that have nothing to
        # do with each other. Any one being true means the tested combination
        # is still covered — fall through to the branch-content check below
        # instead of staling here. If the branch has since gained real
        # content, that check still catches it independently via the
        # patch-id compare (#1847 doesn't short-circuit it).
        base_move_spare_reason: str | None = None
        if (
            test_base_sha is not None
            and current_base_sha is not None
            and test_base_sha != current_base_sha
        ):
            spared, base_move_spare_reason = _base_move_spared(
                gh_ops,
                repo_github,
                test_base_sha,
                current_base_sha,
                getattr(a, "test_head_sha", None),
            )
            if not spared:
                # stale: re-verify against the new base
                if stale is None:
                    stale = SmokeVerdictStatus(
                        ok=False,
                        kind=SMOKE_STALE,
                        assignment_id=getattr(a, "assignment_id", None),
                        anchor="base",
                        recorded_sha=test_base_sha,
                        current_sha=current_base_sha,
                    )
                continue

        # Branch content changed since the test ran. Same SHA-then-patch-id
        # fallback as has_approved_review: a content-identical rebase (SHA
        # moved, patch-id didn't) does not invalidate the verdict.
        test_head_sha = getattr(a, "test_head_sha", None)
        if (
            test_head_sha is not None
            and current_branch_sha is None
            and not branch_sha_attempted
            and gh_ops is not None
            and repo_github
            and entry_branch
        ):
            current_branch_sha, branch_sha_probe_failed, branch_sha_probe_error = (
                _gh_get_branch_sha(gh_ops, repo_github, entry_branch)
            )
            branch_sha_attempted = True

        # #2704: same fail-closed treatment as the base-SHA probe above — the
        # live probe just above positively confirmed it could not read
        # GitHub, with a recorded verdict that names a specific branch head
        # to compare against. Left unchecked this would silently skip the
        # branch-content check below and reach SMOKE_OK.
        if test_head_sha is not None and branch_sha_probe_failed:
            if unknown is None:
                unknown = SmokeVerdictStatus(
                    ok=False,
                    kind=SMOKE_UNKNOWN,
                    assignment_id=getattr(a, "assignment_id", None),
                    anchor="branch",
                    recorded_sha=test_head_sha,
                    probe_error=branch_sha_probe_error,
                )
            continue

        if (
            test_head_sha is not None
            and current_branch_sha is not None
            and test_head_sha != current_branch_sha
        ):
            test_patch_id = getattr(a, "test_patch_id", None)
            if (
                test_patch_id is not None
                and current_patch_id is None
                and not patch_id_attempted
                and gh_ops is not None
            ):
                current_patch_id = _backfill_branch_patch_id(entry, gh_ops)
                patch_id_attempted = True
            if not (
                test_patch_id is not None
                and current_patch_id is not None
                and test_patch_id == current_patch_id
            ):
                # stale: branch content changed since the test ran
                if stale is None:
                    stale = SmokeVerdictStatus(
                        ok=False,
                        kind=SMOKE_STALE,
                        assignment_id=getattr(a, "assignment_id", None),
                        anchor="branch",
                        recorded_sha=test_head_sha,
                        current_sha=current_branch_sha,
                    )
                continue

        return SmokeVerdictStatus(
            ok=True,
            kind=SMOKE_OK,
            assignment_id=getattr(a, "assignment_id", None),
            spared_reason=base_move_spare_reason,
        )

    # #2704: "cannot confirm" outranks "confirmed stale" — both mean the
    # verdict cannot be trusted as-is, but only `stale` names an actual
    # discrepancy this call observed; `unknown` means no comparison was
    # possible at all, which is the more honest thing to report first.
    if unknown is not None:
        return unknown
    if stale is not None:
        return stale

    # #1819: no terminal verdict — but is it MISSING, or abandoned? A row
    # pinned at the transient `running` marker whose Test worker is gone has a
    # verdict that will never arrive, which is a staleness problem with a
    # bounded automatic fix (re-run and re-record), not the #1640 "was a write
    # lost?" question `--revalidate` deliberately refuses to paper over.
    abandoned = _abandoned_running_marker(branch_work, board)
    if abandoned is not None:
        return SmokeVerdictStatus(
            ok=False,
            kind=SMOKE_STALE,
            assignment_id=getattr(abandoned, "assignment_id", None),
            anchor="run",
        )

    return SmokeVerdictStatus(
        ok=False,
        kind=SMOKE_MISSING,
        assignment_id=getattr(branch_work[0], "assignment_id", None),
    )


def stale_smoke_conflict_reason(entry, smoke, gh_ops) -> str | None:
    """#2231: the conflict hiding behind a stale-verdict smoke block, if any.

    Gate evaluation is ordered (review → smoke → CI → …) and every gate
    short-circuits, so an entry whose branch does not merge AT ALL never gets
    that far: it reports "test verdict stale", which invites exactly the one
    remedy that cannot work. Worse, the two mechanisms that could fix it are
    both downstream of a step that never runs — #1738's auto-repair answers a
    stale verdict (there wasn't one) and #241's conflict-fix is dispatched off
    a failed *merge attempt* (the smoke gate returned before one happened).
    quadraui #306/#309 spent 11h there apiece.

    So: when the smoke gate is about to block on a **stale** verdict
    specifically, ask GitHub whether the PR merges at all. A confirmed
    ``mergeable: false`` means the stale verdict is not the blocker, and this
    returns a reason naming the conflict — the caller turns that into a
    ``conflict`` event, which is what arms :func:`classify_conflict` and #241.

    Returns ``None`` — leaving today's stale-verdict block exactly as it was —
    whenever the answer isn't a definite yes:

    * the verdict is MISSING rather than stale (the #1640 lost-write shape,
      deliberately out of scope here as in :func:`revalidation_candidates`);
    * there is no PR yet, so there is nothing to ask about;
    * ``gh_ops`` has no ``check_pr_mergeable`` — the same duck-typed probe the
      #1877 CI-absent branch uses, and for the same reason: this function also
      runs against a :class:`~coord.gate_snapshot.GateSnapshot` on the
      ``/board`` read path (#1336 Invariant 1, no third-party I/O), which
      doesn't implement it;
    * the probe returns ``None`` (GitHub still computing mergeability) or
      raises. An inconclusive read must never be upgraded to "conflict": that
      would replace a recoverable staleness block with one that dispatches a
      rebase worker at a branch which merges fine.

    The wording is load-bearing in both directions — see
    :func:`coord.revalidate.compose_conflict_error`, whose docstring explains
    the same constraint for the local-compose variant of this fact.
    """
    if smoke is None or smoke.ok or smoke.kind != SMOKE_STALE:
        return None
    pr_number = getattr(entry, "pr_number", None)
    if not pr_number:
        return None
    probe = getattr(gh_ops, "check_pr_mergeable", None)
    if probe is None:
        return None
    try:
        conflicted = probe(entry.repo_github, pr_number) is False
    except Exception:  # noqa: BLE001 — inconclusive, never a block upgrade
        return None
    if not conflicted:
        return None
    return (
        f"merge conflict: PR #{pr_number} does not merge into "
        f"{entry.target_branch} — GitHub reports it as not mergeable. Its "
        "test verdict is out of date against the current base too, but that "
        "is downstream: no re-test can clear a branch that will not merge "
        "(#2231)"
    )


@dataclass(frozen=True)
class RevalidationCandidate:
    """One queue entry that ``coord merge --revalidate`` may re-test (#1769).

    Built only for entries blocked **solely** on a stale-but-``passed`` smoke
    verdict — see :func:`revalidation_candidates`, which is the whole of the
    eligibility policy. ``work_assignment_id`` is the row whose verdict has to
    be re-recorded for the entry to clear its gate (the one
    :func:`evaluate_smoke_verdict` named as carrying the stale verdict).
    """

    entry: "QueuedMerge"
    work_assignment_id: str | None
    smoke: SmokeVerdictStatus


def revalidation_candidates(
    items: Iterable["QueuedMerge"],
    board,
    config,
    gh_ops: "GhOps | None" = None,
    *,
    skip_review: bool = False,
) -> list[RevalidationCandidate]:
    """The subset of *items* ``--revalidate`` is allowed to re-test (#1769).

    An entry qualifies **only** when every one of these holds:

    * it is ``PENDING`` (a ``CONFLICT``/``HUMAN_REQUIRED``/``MERGED`` entry is
      never re-tested — a conflict is not a staleness problem);
    * the smoke gate applies to it (:func:`requires_smoke`) and
      :func:`evaluate_smoke_verdict` reports :data:`SMOKE_STALE` — i.e. a
      terminal ``passed`` verdict exists but was recorded against a branch/base
      combination that no longer exists. :data:`SMOKE_MISSING` is deliberately
      excluded: a re-test cannot safely paper over the #1640 "was a verdict
      ever written?" disagreement, and #1769's acceptance criteria name a
      genuinely-missing verdict as out of scope;
    * **no other gate is failing** — evaluated AFTER *skip_review* is applied
      (#3107). Concretely, the smoke failure is the only entry left in
      :func:`merge_gate_failures` once a review failure is discarded for a
      ``--skip-review`` run, so an entry that also needs a review is left
      alone UNLESS that same invocation already waived it. Composing
      ``--skip-review`` with ``--revalidate`` is the one combination an
      operator needs — a branch with an unfixable review finding but a merely
      stale test verdict — and evaluating this predicate against the raw,
      unwaived gate set (as before #3107) made that combination inexpressible:
      the waiver would print, then ``--revalidate`` would refuse anyway because
      it still saw the review block the same run had just bypassed. CI is not
      evaluated here (it needs a PR number and a live ``gh`` round trip);
      :func:`process` still enforces it afterwards, so a red-CI entry that was
      revalidated simply stays blocked on CI — it is never merged.

    This is the *eligibility* half of ``--revalidate``. The re-test itself and
    the verdict write live in :mod:`coord.revalidate`; nothing here mutates
    anything, so it is safe to call from ``--dry-run``.
    """
    out: list[RevalidationCandidate] = []
    for entry in items:
        if getattr(entry, "state", None) != PENDING:
            continue
        if config is None or board is None:
            continue
        if not requires_smoke(entry, config):
            continue
        failures = merge_gate_failures(entry, config, board, gh_ops)
        if skip_review:
            failures = [f for f in failures if f.gate != "review"]
        # Blocked *solely* on smoke — a review/other block means a human (or
        # another stage) still owes this entry something a re-test can't give.
        if len(failures) != 1 or failures[0].gate != "smoke":
            continue
        smoke = evaluate_smoke_verdict(entry, board, gh_ops)
        if smoke.ok or smoke.kind != SMOKE_STALE:
            continue
        out.append(RevalidationCandidate(
            entry=entry,
            work_assignment_id=smoke.assignment_id,
            smoke=smoke,
        ))
    return out


def ci_revalidation_candidates(
    items: Iterable["QueuedMerge"],
    board,
    config,
    ci_store: "CiStore | None",
    gh_ops: "GhOps | None" = None,
) -> list["QueuedMerge"]:
    """The subset of *items* ``--revalidate`` may trigger a CI re-run for
    (#1851) — the CI analogue of :func:`revalidation_candidates`.

    An entry qualifies only when it is ``PENDING`` and
    :func:`_entry_gate_status` blocks it for CI staleness specifically (a
    reason starting with :data:`CI_STALE_PREFIX`) — which only happens after
    every gate ahead of CI in that function's evaluation order (review,
    smoke) has already passed, and the CI checks themselves are neither
    failed nor still running, only stale. This mirrors
    :func:`revalidation_candidates`'s "blocked *solely* on..." policy: an
    entry that also needs a review or a fresh local Test verdict is left
    alone here exactly as it already is there — a re-run is never offered as
    a distraction from a block it can't resolve.

    Returns the raw :class:`QueuedMerge` entries (unlike
    :class:`RevalidationCandidate`, there is no local verdict to re-record —
    the remedy is :meth:`coord.ci_store.CiStore.rerun_for_pr`, keyed off
    ``entry.repo_github``/``entry.pr_number`` alone).
    """
    if ci_store is None or not ci_store.is_available:
        return []
    out: list["QueuedMerge"] = []
    for entry in items:
        if getattr(entry, "state", None) != PENDING:
            continue
        if not getattr(entry, "pr_number", None):
            continue
        status, reason = _entry_gate_status(entry, board, config, ci_store, gh_ops)
        if status == PLAN_BLOCKED and (reason or "").startswith(CI_STALE_PREFIX):
            out.append(entry)
    return out


# Stored error strings that only reflect the gate state *at the moment a
# merge attempt ran* (`process()`) — nothing clears them when the approval or
# verdict they're waiting on lands outside of a merge attempt (a normal
# interactive review, no `coord merge`/auto-loop tick in between). See #420.
_STALE_GATE_ERRORS = frozenset({
    "review required but not approved",
    "review required but board unavailable to confirm approval",
    "smoke test required but no verdict recorded",
    "smoke test required but board unavailable to confirm verdict",
})

# #1640: the stale-verdict wording carries live SHAs, so it can't be matched
# by equality against a fixed set. It goes stale for exactly the same reason
# the strings above do (recording a fresh verdict clears the condition
# without any merge attempt running), so it gets the same recomputation.
_STALE_GATE_ERROR_PREFIXES = ("smoke test verdict is stale:",)

# #2687: the UAT gate's messages embed a preview URL/assignment id (see
# evaluate_uat_verdict), so — like the smoke-stale wording above — they
# can't be matched by equality; both variants (missing/failed) share this
# prefix and go stale the same #420 way: `coord uat --passed` outside a
# merge attempt doesn't touch the stored `entry.error` string.
_UAT_GATE_ERROR_PREFIX = "uat verdict"

# #2085: the honest third answer for the review gate on a read-only surface —
# neither "not approved" (unconfirmed failure) nor cleared (unconfirmed
# success). See `display_error`.
REVIEW_UNCONFIRMED_ERROR = (
    "review approved but not yet confirmed against the branch head"
)


def _is_recomputable_gate_error(err: str | None) -> bool:
    """True when *err* is a gate refusal :func:`display_error` may recompute."""
    if not err:
        return False
    return (
        err in _STALE_GATE_ERRORS
        or err.startswith(_STALE_GATE_ERROR_PREFIXES)
        or err.startswith(_UAT_GATE_ERROR_PREFIX)
    )


def display_error(entry: "QueuedMerge", board, config) -> str | None:
    """Return the error to show for *entry* in a read-only display (``coord
    status``, dashboards) — recomputing the review/smoke gates live instead
    of trusting the stored ``entry.error`` string verbatim.

    #420: ``entry.error`` is only refreshed by :func:`process` (a real merge
    attempt) or ``refresh_entry_assignment``. When a review approves — or a
    smoke verdict is recorded — through the normal path (no ``coord merge``
    run, no auto-loop tick in between), nothing clears the stored string, so
    a mergeable entry can keep showing e.g. "review required but not
    approved" indefinitely. Left unchecked this invites operators to bounce
    already-approved work back for another round (the #410 real-world case).

    Only the gate messages known to go stale this way (review, smoke, and
    — #2687 — uat) are recomputed here, and recomputation is pure
    board/config lookups — no I/O. Every
    other stored error (merge conflicts, CI check results) reflects the
    outcome of the *last actual attempt* and is left untouched; re-checking
    CI on every ``coord status`` would mean a live ``gh`` call per queue
    entry just to render a status line.
    """
    if not _is_recomputable_gate_error(entry.error):
        return entry.error
    if board is None or config is None:
        # Can't recompute without both — fall back to the stored string.
        return entry.error
    if entry.error.startswith("review"):
        if not requires_review(entry, config):
            return None
        scan = scan_approved_reviews(entry, board)
        if scan.approved:
            return None
        if scan.unknown_head:
            # #2085 review follow-up: an approval exists and carries a
            # `review_head_sha`, but this entry's `branch_head_sha` is still
            # None — it's only populated once a live `process()`/`plan()`
            # tick has touched the row, so a freshly-approved entry hasn't
            # got one yet. This recompute is deliberately I/O-free (see
            # above), so it cannot resolve the freshness question; the
            # stored "review required but not approved" would be an
            # unconfirmed *failure* verdict, and clearing it outright would
            # be an unconfirmed *success* one (the #1640 trap in the smoke
            # branch below). Say what is actually known instead — this
            # self-heals to a definite answer on the next live tick.
            return REVIEW_UNCONFIRMED_ERROR
        return entry.error
    if entry.error.startswith("smoke"):
        if not requires_smoke(entry, config):
            return None
        smoke = evaluate_smoke_verdict(entry, board)
        if not smoke.ok:
            # Prefer the freshly-computed wording — it names stale-vs-missing
            # (#1640) even when the stored string predates that distinction.
            return smoke.message
        # #1640: this recomputation is deliberately I/O-free (no gh_ops), so
        # the #1479 freshness anchors are only populated on an entry that a
        # live `process()` pass already backfilled. Without them "ok" means
        # "found a terminal verdict", NOT "found a fresh one" — so a stored
        # staleness refusal must not be cleared on that evidence, or this
        # read-only surface starts showing green for exactly the entry
        # `coord merge` refuses. The plain "no verdict recorded" string keeps
        # its original #420 clear-on-recompute behaviour.
        if entry.error.startswith(_STALE_GATE_ERROR_PREFIXES):
            return entry.error
        return None
    if entry.error.startswith(_UAT_GATE_ERROR_PREFIX):
        # #2687: no staleness nuance to preserve here (unlike the smoke
        # branch above) — a UAT verdict carries no SHA anchor that can go
        # stale, so a fresh recompute is always the right answer: cleared
        # when a "passed" verdict landed since the stored error was set,
        # otherwise the freshly-worded (still-accurate) message.
        #
        # #2948: deliberately called with no `gh_ops` — this function is
        # I/O-free by contract (see its own docstring above). That means the
        # live GitHub-Deployment preview lookup never runs here, so a repo
        # relying solely on `uat_live_preview` shows the "could not be
        # resolved" wording on this read-only surface even when a live
        # `coord merge` tick would have found a real URL. Harmless: this
        # recompute only ever *clears* or re-words an existing block, it
        # never blocks on its own, and the next live gate evaluation
        # resolves the URL properly.
        if not requires_uat(entry, config):
            return None
        uat_ok, uat_message = evaluate_uat_verdict(entry, board, config)
        return None if uat_ok else uat_message
    return entry.error  # pragma: no cover — unreachable, kept for safety


@dataclass
class QueuedMerge:
    assignment_id: str
    repo_name: str
    repo_github: str
    branch: str
    target_branch: str
    issue_number: int
    issue_title: str
    state: str = PENDING
    pr_number: int | None = None
    pr_url: str | None = None
    size: int | None = None
    last_attempt: float | None = None
    error: str | None = None
    enqueued_at: float | None = None
    # #821: current branch HEAD SHA, populated at process() time from GitHub.
    # When set, `has_approved_review` checks it against the review assignment's
    # `review_head_sha` to detect stale approvals (commits pushed after review).
    # None means SHA tracking is not available for this entry.
    branch_head_sha: str | None = None
    # #1475: current content-addressed patch-id for the branch's diff against
    # `target_branch`, populated at process() time alongside branch_head_sha.
    # `has_approved_review` falls back to comparing this against the review's
    # `review_patch_id` when the SHAs differ (e.g. a conflict-fix rebase) —
    # identical patch-id means the rebase changed no content, so the approval
    # still covers it. None means patch-id tracking is not available (fails
    # closed to the pre-#1475 SHA-only staleness check).
    branch_patch_id: str | None = None
    # #1479: current HEAD SHA of `target_branch` itself, populated at
    # process() time alongside branch_head_sha/branch_patch_id.
    # `has_smoke_verdict` compares this against the test verdict's recorded
    # `test_base_sha` to detect a merge base that moved since the test ran —
    # a condition `branch_patch_id` (the branch's own content fingerprint)
    # cannot see, since a rebase replays the identical diff onto a new base
    # without changing it. None means base-SHA tracking is not available for
    # this entry (transient, like branch_head_sha/branch_patch_id — never
    # persisted to the queue DB).
    target_branch_head_sha: str | None = None
    # #2809: the `GhTransientError` (often the `GhRateLimitError` subclass)
    # that made `branch_head_sha` unknown, when the failure was CONFIRMED
    # transient — i.e. `_gh_get_branch_sha` raised rather than merely
    # returning a falsy SHA. `None` covers both "never probed" and "probed
    # fine" as well as "probed and gh_ops doesn't support raise_on_transient
    # at all" — every one of those degrades to the pre-#2809 generic
    # `UNKNOWN_BRANCH_HEAD_REASON` string, never a fabricated cause.
    # Transient like branch_head_sha/branch_patch_id/target_branch_head_sha
    # above — recomputed every tick, never persisted to the queue DB.
    branch_head_probe_error: "GhTransientError | None" = None
    # #1077: the originating assignment's `type` (e.g. "work", "mock-author"),
    # captured at enqueue time. Drives both the PR-body "Closes #N" vs
    # "Refs #N" keyword (`_briefing_body`) and whether `process()` closes
    # `issue_number` deterministically after merge — see
    # `coord.models.CLOSES_ISSUE_TYPES`. Defaults to "work" for entries
    # created before this field existed (preserves prior close-on-merge
    # behavior for old rows).
    assignment_type: str = "work"
    # #1213: snapshot of the originating assignment's resolved
    # required_gates (from config.pipeline.labels via a matching GitHub
    # issue label, or [] for "no override"), captured at enqueue() time.
    # requires_review/requires_smoke read this — falling back to
    # config.pipeline.default_gates when empty — instead of re-resolving
    # from the live board at merge time, so the effective gate policy for
    # an entry is commit-bound to when it was enqueued. [] (the default)
    # means "no override" for both fresh entries and rows predating this
    # column (NULL decodes to []) — both fall back identically.
    required_gates: list[str] = field(default_factory=list)
    # #1892: count of automatic `CiStore.rerun_for_pr` calls `process()` has
    # issued for this entry's CURRENT run of verdictless CI failures —
    # capped at `MAX_CI_INFRA_RERUNS`. Persists across ticks (unlike the
    # transient `branch_head_sha`/`branch_patch_id` fields above) because the
    # whole point is a durable ceiling: a workflow broken at "Set up job"
    # must stop auto-rerunning and park for a human, not retry every tick
    # forever. 0 for every entry that has never hit a verdictless failure,
    # and for rows predating this column.
    ci_infra_reruns: int = 0
    # #2197: count of automatic `CiStore.rerun_for_pr` calls `process()` has
    # issued for this entry's CURRENT run of CI staleness (#1851) — a
    # PASSING check recorded against a base that has since moved. Kept
    # separate from `ci_infra_reruns` above on purpose (see
    # `MAX_CI_STALE_RERUNS`'s comment): the two triggers must be
    # independently capped and independently legible in the audit trail.
    # Capped at `MAX_CI_STALE_RERUNS`. 0 for every entry that has never gone
    # CI-stale, and for rows predating this column.
    ci_stale_reruns: int = 0
    # #2252: count of automatic `CiStore.rerun_failed_for_pr` calls
    # `process()` has issued for this entry's CURRENT streak of genuinely-
    # verdicted (non-infra) CI failures — capped at `MAX_CI_FLAKY_RERUNS`.
    # Kept separate from `ci_infra_reruns`/`ci_stale_reruns` above for the
    # same reason those two are kept apart from each other: independently
    # capped, independently legible in the audit trail. 0 for every entry
    # that has never hit a genuine-verdict failure, and for rows predating
    # this column.
    ci_flaky_reruns: int = 0
    # #2252: JSON blob of the failing check names/conclusions and the
    # branch SHA they failed against, captured at the moment `process()`
    # triggered this entry's one flake-checking re-run. `branch_head_sha`
    # itself is transient (recomputed from GitHub every tick, never
    # persisted) — this is the only durable record of WHAT failed once the
    # re-run's answer arrives on a later tick. `""` (falsy) means no re-run
    # is currently pending an answer; set only while `ci_flaky_reruns`
    # reflects an in-flight re-run, cleared the moment its answer (pass ->
    # flake recorded via `record_audit`, fail -> confirmed real, no flake)
    # is known.
    ci_flaky_pending: str = ""
    # #2347: count of consecutive LIVE `process()` attempts that have
    # observed a bare check-list FETCH failure (GitHub unreachable) for this
    # entry's CURRENT streak — capped at `MAX_CI_UNREADABLE_RERUNS`. Kept
    # separate from `ci_infra_reruns`/`ci_stale_reruns`/`ci_flaky_reruns` for
    # the same reason those are kept apart from each other: independently
    # capped, independently legible in the audit trail — a fetch failure
    # answers a different question ("could GitHub be reached at all?") than
    # any of the other three ("what did CI say?"). 0 for every entry that
    # has never hit a fetch failure, and for rows predating this column.
    ci_unreadable_reruns: int = 0
    # #2510: count of `type="ci-fix"`-shaped fix-worker dispatches the
    # coordinator has issued for this entry's confirmed (non-infra,
    # non-first-flake) `checks_failed` streak — capped at
    # `coord.ci_fix.MAX_CI_FIX_DISPATCHES`. A CONFIRMED failure (see
    # `process()`'s `msg = f"checks failed: {summary}"` block, reached only
    # once the infra/flaky retry budgets above are exhausted or
    # inapplicable) used to just leave the entry `PENDING` forever with no
    # path back to a fix — this is the durable ceiling that lets
    # `coord.commands.merge._dispatch_ci_fixes` dispatch a bounded number of
    # fix attempts before giving up and promoting the entry to
    # `HUMAN_REQUIRED`, mirroring how `ci_infra_reruns`/`ci_flaky_reruns`
    # bound their own auto-remedies. 0 for every entry that has never hit a
    # confirmed CI failure, and for rows predating this column. Unlike the
    # rerun counters above (which reset once CI resolves cleanly — see the
    # "genuinely resolved" reset block later in `process()`), this one is
    # intentionally NOT auto-reset there: a fix dispatch's payoff is a FUTURE
    # green run on a NEW commit, which as a side effect also resets
    # ci_infra_reruns/ci_flaky_reruns/ci_unreadable_reruns — so by the time
    # this entry could reach that reset block again, the fix either worked
    # (merge proceeds, entry leaves the queue) or the retry budget is what
    # stopped the loop; resetting it on an unrelated green tick would just
    # reopen a budget a still-broken PR could re-exhaust forever.
    ci_fix_dispatches: int = 0
    # #3011: `branch_head_sha` (see the field above) captured at the moment
    # `coord.ci_fix.dispatch_ci_fix` last dispatched a fix worker for this
    # entry's confirmed-failure streak. Durable — unlike `branch_head_sha`
    # itself, which is transient/recomputed every tick — so a later tick
    # can compare "what was the branch at when we dispatched" against "what
    # is the branch NOW" and tell a genuine attempt apart from a worker
    # that pushed no commit. '' means no ci-fix dispatch is currently
    # unaccounted-for: either none has ever been dispatched for this
    # streak, or the last one was already resolved (a real attempt spent,
    # or a no-op refunded — see `coord.ci_fix.dispatch_was_noop`/
    # `refund_noop_ci_fix`). 0/'' for every row predating this column.
    ci_fix_head_sha: str = ""
    # #3011: count of CONSECUTIVE ci-fix legs that completed with the
    # branch HEAD unchanged from `ci_fix_head_sha` — i.e. a fresh worker
    # looked at this confirmed failure and correctly concluded it wasn't
    # theirs to fix, pushing no commit. Kept separate from
    # `ci_fix_dispatches`: a no-op leg is refunded there (does NOT count
    # toward `coord.ci_fix.MAX_CI_FIX_DISPATCHES`) precisely so two correct
    # declines don't masquerade as two failed genuine attempts. This
    # counter is what actually bounds the no-op case — capped at
    # `coord.ci_fix.MAX_CI_FIX_NOOP_STREAK`, at which point
    # `coord.commands.merge._dispatch_ci_fixes` escalates to
    # `HUMAN_REQUIRED` with a "not attributable to this branch" reason
    # instead of the generic retry-cap one. Reset to 0 the moment a
    # dispatch's OWN outcome shows the branch actually moved (a real
    # attempt, not a no-op) — see `dispatch_ci_fix`. 0 for every row
    # predating this column.
    ci_fix_noop_streak: int = 0
    # #3114 review fix: the `branch_head_sha` (see the field above) that
    # `coord.ci_github.build_ci_failure_detail` was last invoked for on
    # this entry, together with its JSON-serialized result in
    # `ci_fix_detail_json` (`coord.ci_store.ci_failure_detail_to_json`/
    # `_from_json`). Exists because a dispatch declined for a reason
    # UNRELATED to the CI detail itself (no capable machine, agent
    # unreachable, the #2538 DB-lock-contention case) leaves the entry
    # `PENDING` "for the next tick to retry" — without this cache, every
    # subsequent `coord merge`/`coord merge --only` pass over the SAME
    # still-failing SHA would re-issue the `gh api .../actions/jobs/{id}/
    # logs` fetch behind `build_ci_failure_detail`, exactly the per-tick
    # GitHub probing #2989/#2988 warn against. '' means "no fetch cached
    # for any SHA yet" — matching `ci_fix_head_sha`'s own empty-string
    # sentinel convention above — for every entry that has never had a
    # detail fetch attempted, and for rows predating this column.
    ci_fix_detail_sha: str = ""
    # #3114 review fix: paired with `ci_fix_detail_sha` above. `None` means
    # "no fetch cached for the SHA in `ci_fix_detail_sha`" (including every
    # row predating this column); once a fetch has been attempted for a
    # given SHA this holds `coord.ci_store.ci_failure_detail_to_json`'s
    # output — the JSON literal `"null"` when the fetch ran but genuinely
    # found no detail, or the serialized `CIFailureDetail` otherwise. Only
    # ever trusted by a reader when `ci_fix_detail_sha` matches the
    # CURRENT `branch_head_sha` — a stale cache entry from a since-moved
    # SHA is simply refetched, never served.
    ci_fix_detail_json: str | None = None


class GhOps(Protocol):
    """Minimal interface the queue needs from github_ops. Tests pass a stub."""

    def create_pr(
        self, repo: str, *, base: str, head: str, title: str, body: str
    ) -> dict: ...

    def get_pr_size(self, repo: str, number: int) -> int: ...

    def merge_pr(self, repo: str, number: int, method: str = "rebase") -> tuple[bool, str]: ...

    def close_issue(self, repo: str, issue_number: int) -> None: ...

    def get_pr_body(self, repo: str, number: int) -> str:
        """Return PR *number*'s current body text (#1196, PR-body lint)."""
        ...

    def edit_pr_body(self, repo: str, number: int, body: str) -> None:
        """Overwrite PR *number*'s body text (#1196, PR-body lint)."""
        ...

    def has_open_children(self, repo: str, issue_number: int) -> bool:
        """True when *issue_number* has an open child (#1196)."""
        ...

    def is_epic_issue(self, repo: str, issue_number: int) -> bool:
        """True when *issue_number* carries the tracking/epic label (#1318)."""
        ...

    def get_pr_commit_messages(self, repo: str, number: int) -> list[str]:
        """Return every commit message on PR *number* (#1318, epic guard)."""
        ...

    def get_branch_sha(self, repo: str, branch: str) -> str | None:
        """Return the current HEAD SHA for *branch*, or None on failure.

        Used to populate ``QueuedMerge.branch_head_sha`` at process() time so
        ``has_approved_review`` can reject stale approvals (#821).  Returning
        ``None`` (on any network/auth failure) is safe — the staleness check
        is skipped for rows without a SHA, preserving backward compatibility.
        """
        ...

    def get_branch_patch_id(self, repo: str, base: str, branch: str) -> str | None:
        """Return the content-addressed patch-id for *branch*'s diff against
        *base*, or None on failure.

        Used to populate ``QueuedMerge.branch_patch_id`` at process() time so
        ``has_approved_review`` can carry an approval forward across a pure
        rebase (#1475) even though the branch's HEAD SHA changed. Returning
        ``None`` is safe — the gate falls back to the pre-#1475 SHA-only
        staleness check.
        """
        ...

    def get_compare_files(self, repo: str, base: str, head: str) -> list[str] | None:
        """Return the file paths changed in the three-dot *base*...*head*
        compare, or None on failure.

        #1738: used by :func:`_base_move_is_inert` to tell a content-
        irrelevant base move (docs/scripts/issue-template only) from one that
        could actually affect a test result, before staling an otherwise-
        fresh smoke verdict just because the merge base's SHA moved.

        #1778: also used by :func:`_branch_is_inert`, the mirror check — is
        the *branch's* own diff (not the base move) entirely inert, so a
        base move (however substantive) doesn't need to re-verify it either.

        #1847: both file lists (fetched once each, via
        :func:`_fetch_compare_files`) also feed
        :func:`_base_move_disjoint_from_branch` — the third #1479 escape
        hatch, sparing a verdict when the two diffs simply touch no files in
        common, independent of either being inert on its own.
        """
        ...

    def check_pr_mergeable(self, repo: str, number: int) -> bool | None:
        """Return GitHub's current mergeability verdict for PR *number*.

        ``True`` when cleanly mergeable, ``False`` when conflicting, ``None``
        when unknown (still computing, or the check itself failed). Used by
        :func:`reconcile_conflict_entries` (#1477) to re-test a parked
        ``CONFLICT`` entry rather than trusting the cached verdict from
        whenever the queue last attempted it.
        """
        ...

    def get_pr_deployment_url(self, repo: str, branch: str) -> str | None:
        """Return the live preview-deployment URL for *branch*'s GitHub
        Deployment, or ``None`` when one can't be confirmed (#2948).

        Used by :func:`evaluate_uat_verdict` (via ``_resolve_uat_preview_url``)
        for a repo opted into ``Repo.uat_live_preview`` — the primary UAT-gate
        preview-URL resolution path, replacing the #2687 ``{pr_branch_slug}``
        template placeholder that was confirmed live to never resolve for a
        real Cloudflare Pages project. See
        :func:`coord.github_ops.get_pr_deployment_url` for the actual ``gh
        api`` calls and the environment-name matching rule.
        """
        ...

    def branch_has_merge_commit(self, repo: str, number: int) -> bool | None:
        """True when any commit on PR *number* has more than one parent.

        ``True``/``False`` when determined, ``None`` when it can't be (any
        ``gh`` error, or a malformed response) — an inconclusive read, same
        fail-closed contract as :meth:`check_pr_mergeable`. Used by
        :func:`process` (#1467) to fall back from ``--rebase`` to
        ``--squash`` before attempting a merge GitHub would otherwise refuse
        with "This branch can't be rebased", and by
        :func:`reconcile_conflict_entries` to avoid unparking an entry whose
        rebase-refusal will deterministically recur.

        Optional on stub ``GhOps`` implementations: callers detect support
        via ``getattr(gh_ops, "branch_has_merge_commit", None)`` and treat a
        missing method the same as an inconclusive (``None``) result, so
        existing test stubs that predate #1467 keep working unmodified.
        """
        ...

    def find_pr_for_branch(self, repo: str, branch: str) -> dict | None:
        """Return the open PR whose head ref is *branch*, or ``None``.

        Used by :func:`process` (#1624) to resolve an entry's real PR in the
        ``dry_run`` path — mirroring what ``create_pr`` already does
        internally on the real path — so a branch with an already-open PR is
        reported as ``PR #N (existed)`` instead of ``would open PR``, and so
        the CI gate below has a real PR number to evaluate against instead of
        silently skipping.

        Optional on stub ``GhOps`` implementations, same contract as
        :meth:`branch_has_merge_commit`: callers detect support via
        ``getattr(gh_ops, "find_pr_for_branch", None)`` and treat a missing
        method (or a lookup failure) the same as "no PR found" — fail closed,
        never assume a PR exists that couldn't be confirmed.
        """
        ...

    def pr_is_merged(self, repo: str, branch: str) -> bool:
        """True when *branch*'s current tip is a commit that already merged.

        Used by :func:`process` (#2143) as the last check before opening a
        PR for an entry with no ``pr_number`` yet — right before the
        mutating ``create_pr`` call, not at snapshot time, so a branch
        another merge driver (the drive-queue timer, a concurrent
        ``coord merge``) merged out from under a long-running ``--revalidate``
        wait is never handed a second, purposeless PR.  Unlike
        :meth:`find_pr_for_branch` (open PRs only — ``gh pr list --state
        open``), this resolves regardless of PR state, so "no open PR" is
        never misread as "no PR at all".

        Optional on stub ``GhOps`` implementations, same contract as
        :meth:`branch_has_merge_commit`: callers detect support via
        ``getattr(gh_ops, "pr_is_merged", None)`` and treat a missing method
        (or a lookup failure) as "not merged" — fail *open* here, since the
        cost of a false negative is the pre-#2143 status quo (an extra PR
        that a human or the next pass closes) while a false positive would
        silently strand real, unmerged work in a MERGED state.
        """
        ...

    def get_default_branch_head(self, repo: str, branch: str) -> str:
        """Return the full commit SHA at the tip of *branch*.

        #2164: used by ``coord.acceptance.clear_expected_red_via_pr`` (the
        post-merge ``expected_red`` clearing sweep, fired from
        :func:`process` right after a `work` entry merges) to anchor the
        throwaway clearing branch at the default branch's current tip.
        Optional on stub ``GhOps`` implementations — same contract as
        :meth:`branch_has_merge_commit`.
        """
        ...

    def create_remote_branch(self, repo: str, branch: str, sha: str) -> bool:
        """Create ``refs/heads/{branch}`` pointing at *sha*. Returns True on
        success, False on failure (including "already exists").

        #2164, same clearing sweep as :meth:`get_default_branch_head`.
        Optional on stub ``GhOps`` implementations.
        """
        ...

    def get_repo_file_with_sha(self, repo: str, path: str, branch: str = "develop") -> tuple[str, str]:
        """Return (*content*, *blob_sha*) for *path* on *branch*.

        #2164, same clearing sweep — also used to enumerate/read
        ``tests/acceptance/ms-*/manifest.*`` without a local checkout, since
        :func:`process` never assumes one exists (this is a pure ``gh``-API
        wire layer, see the module docstring). Optional on stub ``GhOps``
        implementations.
        """
        ...

    def update_repo_file(
        self, repo: str, path: str, branch: str, content: str, message: str, *, sha: str,
    ) -> str:
        """Commit *content* to *path* on *branch* via the Contents API.
        Returns the new commit sha.

        #2164, same clearing sweep. Optional on stub ``GhOps``
        implementations.
        """
        ...

    def list_repo_subdirs(self, repo: str, path: str, branch: str = "develop") -> list[str]:
        """Directory names directly under *path* on *branch*.

        #2164, same clearing sweep — enumerates ``tests/acceptance/ms-*/``
        to find the manifest that maps a given issue. Optional on stub
        ``GhOps`` implementations.
        """
        ...


def live_gate_entry(
    a: Assignment,
    repo_github: str,
    target_branch: str,
    gh_ops: "GhOps | None",
) -> QueuedMerge:
    """Build a synthetic, never-persisted :class:`QueuedMerge` from a raw
    work :class:`~coord.models.Assignment` *a*, with the #821/#1475/#1479
    freshness anchors (``branch_head_sha``, ``branch_patch_id``,
    ``target_branch_head_sha``) populated LIVE via *gh_ops* when supplied.

    #2085: :func:`has_approved_review` / :func:`evaluate_smoke_verdict` read
    those anchors straight off whatever *entry* they're handed — a real
    ``QueuedMerge`` only carries them once :func:`process` has run at least
    once. Any caller that needs to gate-check a raw work ``Assignment``
    *before* it has gone through ``process()`` — the daemon's passive-tick
    :func:`enqueue_approved_work`, ``coord.notify``'s stalled-dispatch
    recovery, ``coord.diagnose``'s stage-work recovery, and ``coord.commands.
    merge``'s auto-enqueue scan — used to hand ``has_approved_review`` the
    bare ``Assignment`` directly. It has no ``branch_head_sha`` attribute at
    all, so ``getattr(entry, "branch_head_sha", None)`` always read ``None``;
    since #2085 made an unconfirmed SHA fail CLOSED (previously it fell
    open), that made a review carrying a real ``review_head_sha`` —
    virtually every modern approval — permanently unconfirmable from any of
    those call sites, not just the superseded-approval case #2085 was filed
    about. Routing through this helper first — mirroring the construction
    :func:`coord.gates.build_gate_report` already used inline for the same
    reason — gives those callers the same live SHA a real ``coord merge``
    run would see, so a genuinely fresh approval can still confirm.

    This is now the ONE place that construction happens — ``build_gate_report``
    was refactored to call this too (#2096: two surfaces answering "is this
    entry's approval still fresh" must call one function, not reimplement it
    twice and risk drifting apart).

    *gh_ops* ``None`` skips every live lookup (fails open on the freshness
    anchors themselves, exactly as ``build_gate_report`` does with no live
    client) — the resulting entry still lets a review with no
    ``review_head_sha`` at all take the legacy no-SHA-to-compare path, but
    correctly fails closed for one that has a SHA and can't be confirmed.
    """
    entry = QueuedMerge(
        assignment_id=a.assignment_id or "",
        repo_name=a.repo_name,
        repo_github=repo_github,
        branch=a.branch or "",
        target_branch=target_branch,
        issue_number=getattr(a, "issue_number", 0) or 0,
        issue_title=getattr(a, "issue_title", None) or "",
        assignment_type=getattr(a, "type", None) or "work",
        required_gates=list(getattr(a, "required_gates", None) or []),
    )
    live_anchor_entry(entry, gh_ops)
    return entry


def live_anchor_entry(entry: "QueuedMerge", gh_ops: "GhOps | None") -> None:
    """Refresh *entry*'s #821/#1475/#1479 freshness anchors (``branch_head_sha``,
    ``branch_patch_id``, ``target_branch_head_sha``, ``branch_head_probe_error``)
    LIVE via *gh_ops*, in place.

    Factored out of :func:`live_gate_entry` (#2809 review) so a caller that
    already has a real, persisted :class:`QueuedMerge` — not a raw work
    :class:`~coord.models.Assignment` — can re-anchor it against the current
    GitHub state without rebuilding it from scratch. ``coord merge --only``
    is exactly this: it resolves ``only_entry`` straight off the queue DB
    (:func:`resolve_entry_key`), which was last live-anchored whenever
    :func:`process` (or this function) previously ran on it — potentially
    stale by the time the operator's ``--only`` invocation reports gate
    status. Without a fresh call here, ``only_entry.branch_head_probe_error``
    stays at whatever it was (often the dataclass default ``None``), so
    :func:`merge_gate_failures`' review-gate line can't tell "GitHub
    confirmed this is unknown" apart from "we never checked" — exactly the
    gap #2809's incident reproduction (``coord merge --only``) hit.

    *gh_ops* ``None`` or *entry* having no ``branch`` is a no-op — same
    fail-open behaviour as :func:`live_gate_entry` with no live client.
    """
    if gh_ops is None or not entry.branch:
        return
    # #2085: NOT "fail-open, unknown SHA isn't blocking" — an unknown
    # branch_head_sha now fails has_approved_review CLOSED (not open)
    # for any review that carries a review_head_sha to compare against.
    # A transient gh error here degrades to the same conservative
    # refusal as gh_ops=None, never a silent pass.
    #
    # #2809: routed through `_gh_get_branch_sha` (not a bare
    # `gh_ops.get_branch_sha(...)` try/except) so a CONFIRMED transient
    # failure — this is THE call the issue's incident traced the swallow
    # to — is captured on `branch_head_probe_error` instead of collapsing
    # to the same bare `None` a genuinely-deleted branch produces. The
    # branch's own probe wins when both fail (its detail is what the
    # review/smoke gate reasons actually name); the target's is used only
    # when the branch fetch itself succeeded.
    entry.branch_head_sha, _, branch_probe_error = _gh_get_branch_sha(
        gh_ops, entry.repo_github, entry.branch
    )
    entry.target_branch_head_sha, _, target_probe_error = _gh_get_branch_sha(
        gh_ops, entry.repo_github, entry.target_branch
    )
    entry.branch_head_probe_error = branch_probe_error or target_probe_error
    try:
        entry.branch_patch_id = gh_ops.get_branch_patch_id(
            entry.repo_github, entry.target_branch, entry.branch
        )
    except Exception:  # noqa: BLE001
        entry.branch_patch_id = None


# ── Persistence ──────────────────────────────────────────────────────────

def load_queue() -> list[QueuedMerge]:
    """Load all merge queue entries from the database."""
    conn = get_connection()
    rows = sql.execute(conn, "SELECT * FROM merge_queue ORDER BY id").fetchall()
    return [
        QueuedMerge(
            assignment_id=row["assignment_id"],
            repo_name=row["repo_name"],
            repo_github=row["repo_github"],
            branch=row["branch"],
            target_branch=row["target_branch"],
            issue_number=row["issue_number"],
            issue_title=row["issue_title"],
            state=row["state"],
            pr_number=row["pr_number"],
            pr_url=row["pr_url"],
            size=row["size"],
            last_attempt=row["last_attempt"],
            error=row["error"],
            enqueued_at=row["enqueued_at"],
            # #1077: column added via migration; rows written before it
            # existed read back as NULL, so fall back to "work" (the
            # pre-existing close-on-merge behavior for those entries).
            assignment_type=row["assignment_type"] or "work",
            # #1213: column added via migration; NULL (pre-migration rows)
            # and '[]' (explicit "no override") both decode to [] — the
            # gate falls back to config.pipeline.default_gates for either.
            required_gates=json.loads(row["required_gates"]) if row["required_gates"] else [],
            # #1892: column added via migration; NULL (pre-migration rows)
            # decodes to 0 — no auto-reruns spent yet, same as a fresh entry.
            ci_infra_reruns=row["ci_infra_reruns"] or 0,
            # #2197: same NULL-to-0 decoding as ci_infra_reruns above, for
            # rows predating this column.
            ci_stale_reruns=row["ci_stale_reruns"] or 0,
            # #2252: same NULL-to-0/'' decoding as the other rerun counters
            # above, for rows predating these columns.
            ci_flaky_reruns=row["ci_flaky_reruns"] or 0,
            ci_flaky_pending=row["ci_flaky_pending"] or "",
            # #2347: same NULL-to-0 decoding as ci_infra_reruns above, for
            # rows predating this column.
            ci_unreadable_reruns=row["ci_unreadable_reruns"] or 0,
            # #2510: column added via migration; NULL (pre-migration rows)
            # decodes to 0 — no CI-fix dispatches spent yet, same as a fresh
            # entry.
            ci_fix_dispatches=row["ci_fix_dispatches"] or 0,
            # #3011: same NULL-to-''/0 decoding as the columns above, for
            # rows predating these migrations.
            ci_fix_head_sha=row["ci_fix_head_sha"] or "",
            ci_fix_noop_streak=row["ci_fix_noop_streak"] or 0,
            # #3114 review fix: column added via migration; NULL/'' (rows
            # predating this migration, or an entry that has never had a
            # detail fetch attempted) decodes to the same "no cache" shape
            # as a fresh entry's own defaults.
            ci_fix_detail_sha=row["ci_fix_detail_sha"] or "",
            ci_fix_detail_json=row["ci_fix_detail_json"],
        )
        for row in rows
    ]


def save_queue(items: list[QueuedMerge]) -> None:
    """Replace the entire merge queue in the database."""
    conn = get_connection()

    def _write() -> None:
        with conn:
            sql.execute(conn, "DELETE FROM merge_queue")
            for item in items:
                sql.execute(
                    conn,
                    """INSERT INTO merge_queue (
                        assignment_id, repo_name, repo_github, branch,
                        target_branch, issue_number, issue_title, state,
                        pr_number, pr_url, size, last_attempt, error, enqueued_at,
                        assignment_type, required_gates, ci_infra_reruns,
                        ci_stale_reruns, ci_flaky_reruns, ci_flaky_pending,
                        ci_unreadable_reruns, ci_fix_dispatches,
                        ci_fix_head_sha, ci_fix_noop_streak,
                        ci_fix_detail_sha, ci_fix_detail_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        item.assignment_id, item.repo_name, item.repo_github,
                        item.branch, item.target_branch, item.issue_number,
                        item.issue_title, item.state, item.pr_number, item.pr_url,
                        item.size, item.last_attempt, item.error, item.enqueued_at,
                        item.assignment_type, json.dumps(list(item.required_gates or [])),
                        item.ci_infra_reruns, item.ci_stale_reruns,
                        item.ci_flaky_reruns, item.ci_flaky_pending,
                        item.ci_unreadable_reruns, item.ci_fix_dispatches,
                        item.ci_fix_head_sha, item.ci_fix_noop_streak,
                        item.ci_fix_detail_sha, item.ci_fix_detail_json,
                    ),
                )

    # #2802: ride out transient `database is locked` contention the same way
    # every other write in the module does — a bare write here left a
    # review-dispatch drain aborting mid-flight on lock contention with no
    # retry, stranding downstream queue rows behind it.
    retry_on_locked(_write)


# ── Merged-history lookups (#1107 Part 3) ───────────────────────────────────
#
# `housekeeping.sweep()` archives MERGED merge_queue rows older than the
# archive retention window into `merge_queue_archive` (same move-not-delete
# pattern as `assignments`/`notifications`) so the live table stays bounded
# instead of growing forever. `enqueue_approved_work()` and `staging_items()`
# both dedup on "(repo, issue) already merged" to avoid re-surfacing an issue
# whose prior work attempt already shipped — once the winning entry ages out
# of the live table, that check must still see it, so it consults the
# archive too.

def _archived_merged_issue_keys(conn: sqlite3.Connection) -> set[tuple[str, int]]:
    """(repo_name, issue_number) pairs recorded MERGED in the archive.

    `merge_queue_archive` only exists once a housekeeping sweep has archived
    at least one row (or archiving is disabled entirely), so this is
    defensive against the table being absent.
    """
    try:
        rows = sql.execute(
            conn,
            "SELECT repo_name, issue_number FROM merge_queue_archive WHERE state = ?",
            (MERGED,),
        ).fetchall()
    except sql.driver_errors() as exc:  # #2784: not just sqlite3.OperationalError
        # "no such table" surfaces as a *different* driver-named exception
        # per backend (sqlite3.OperationalError vs psycopg.errors.
        # UndefinedTable), so this must go through the dialect seam or the
        # not-yet-archived case becomes an uncaught crash on Postgres.
        #
        # #2983: and catching it is only half the job. `conn` here is the
        # process-lived `get_connection()` singleton, which the caller
        # (`merged_issue_keys`, and in turn `enqueue_approved_work` /
        # `staging_items`) keeps writing and reading through long after this
        # returns — on Postgres the swallowed UndefinedTable aborts that
        # connection's transaction, so the "archive doesn't exist yet"
        # degrade turned every subsequent statement into
        # InFailedSqlTransaction. Nothing uncommitted is at risk: this
        # function only reads, and on Postgres the abort had already
        # discarded anything pending regardless.
        rollback_after_driver_error(conn, exc)
        return set()
    return {(r["repo_name"], r["issue_number"]) for r in rows}


def merged_issue_keys() -> set[tuple[str, int]]:
    """All ``(repo_name, issue_number)`` pairs ever recorded MERGED.

    Unions the live ``merge_queue`` table with ``merge_queue_archive`` so
    callers get a correct answer regardless of whether the winning entry has
    since been archived by ``housekeeping.sweep()``.
    """
    conn = get_connection()
    live = {
        (r["repo_name"], r["issue_number"])
        for r in sql.execute(
            conn,
            "SELECT repo_name, issue_number FROM merge_queue WHERE state = ?",
            (MERGED,),
        ).fetchall()
    }
    return live | _archived_merged_issue_keys(conn)


# ── Enqueue ──────────────────────────────────────────────────────────────

def enqueue(
    assignment: Assignment,
    repo_github: str,
    target_branch: str,
    config=None,
    board=None,
    gh_ops: "GhOps | None" = None,
) -> QueuedMerge | None:
    """Add a completed assignment to the queue if it isn't already there.

    Returns the new entry, or None if it was already queued, has no branch,
    or (#946) *config* was supplied and ``passes_merge_gates`` rejects it —
    i.e. review/smoke are required but not yet satisfied.  ``config`` (and
    ``board``) are optional and default to ``None`` for backward
    compatibility with existing callers (notably tests that seed the queue
    directly); passing ``None`` skips the gate check entirely rather than
    failing closed, since without a config there's no way to know which
    gates apply.

    #2085: when *config* IS supplied, the gate check runs against
    :func:`live_gate_entry` — never against the raw *assignment*. An
    ``Assignment`` has no ``branch_head_sha``/``branch_patch_id`` attribute
    at all, so handing it straight to :func:`passes_merge_gates` made
    :func:`has_approved_review`'s #821 freshness check permanently
    *unconfirmable*: every review carrying a real ``review_head_sha`` (i.e.
    virtually every modern approval) failed closed, turning this gate into
    one that can never pass. ``repo_github`` and ``target_branch`` are
    already parameters here, so the confirmation data is built rather than
    demanded — a caller cannot reintroduce that regression by forgetting to
    thread *gh_ops* through, which is exactly how the dashboard's enqueue
    path was missed when the other four raw-``Assignment`` gate call sites
    were fixed.

    *gh_ops* defaults to the live :mod:`coord.github_ops` module (only
    consulted when *config* is set, so a gate-less enqueue stays as
    I/O-light as before, and only alongside the ``get_branch_diff_size``
    call this function already makes). Pass an explicit stub to inject a
    fake; a lookup failure degrades to the conservative "unconfirmed"
    refusal, never a silent pass.

    Dedup is by ``(repo_github, branch)`` — the queue's natural key is the
    branch we'd merge, not the assignment_id.  Multiple work assignments
    routinely target the same branch (original + fix-1 in the auto-loop,
    original + PR-creator from ``coord pr``); they should not produce
    duplicate rows. (#274)
    """
    if not assignment.branch:
        return None
    if config is not None:
        if gh_ops is None:
            from coord import github_ops as _live_gh  # noqa: PLC0415

            gh_ops = _live_gh
        gate_entry = live_gate_entry(assignment, repo_github, target_branch, gh_ops)
        if not passes_merge_gates(gate_entry, config, board, gh_ops=gh_ops):
            return None
    items = load_queue()
    if any(
        x.assignment_id == assignment.assignment_id
        or (x.repo_github == repo_github and x.branch == assignment.branch)
        for x in items
    ):
        return None
    # #776: populate size eagerly at enqueue time via the compare API so the
    # displayed order matches the merge order without waiting for a PR to be
    # opened.  Fail-open: size=None keeps the entry at the back of the queue.
    from coord import github_ops as _gho  # noqa: PLC0415
    try:
        diff_size: int | None = _gho.get_branch_diff_size(
            repo_github, target_branch, assignment.branch
        ) or None
    except Exception:  # noqa: BLE001
        diff_size = None

    entry = QueuedMerge(
        assignment_id=assignment.assignment_id or "",
        repo_name=assignment.repo_name,
        repo_github=repo_github,
        branch=assignment.branch,
        target_branch=target_branch,
        issue_number=assignment.issue_number,
        issue_title=assignment.issue_title,
        size=diff_size,
        enqueued_at=time.time(),
        assignment_type=assignment.type,
        # #1213: snapshot the resolved gate list at enqueue time (commit-
        # bound) rather than leaving requires_review/requires_smoke to
        # re-resolve it from the live board later.
        required_gates=list(assignment.required_gates or []),
    )
    items.append(entry)
    save_queue(items)
    return entry


def enqueue_approved_work(config, board=None) -> list[str]:
    """Enqueue / re-key merge-queue entries for all approved + tested done work.

    Scans ``board.completed`` for done assignments whose ``type`` is in
    :data:`coord.models.WORK_LIKE_TYPES` (``"work"`` or ``"mock-author"``,
    #930) and, for each that satisfies ALL three conditions:

    1. Review gate OK — ``requires_review(a, config)`` is False, **or** an
       approved review exists on the board (``has_approved_review``) that
       still covers the branch's LIVE current head (#2085: confirmed via
       :func:`live_gate_entry`, not the raw ``Assignment``, which has no SHA
       to compare against).
    2. Smoke gate OK — ``requires_smoke(a, config)`` is False, **or** the
       work assignment carries a ``test_state in ('passed', 'skipped')``
       verdict (``has_smoke_verdict``).
    3. Not terminal on GitHub — ``work_is_terminal`` returns False (issue still
       open, or the PR that merged is not this branch's *current* commit —
       #1150: a historical merge on a reused branch, e.g. from ``--fix-of``
       continuing on the same branch, must not block enqueue of new commits
       pushed on top of it). This is checked directly against GitHub per
       assignment rather than via a queue-derived "already merged" shortcut,
       since a MERGED entry for the same ``(repo, issue)`` pair may belong to
       an entirely different branch/commit than the one being considered here.

    …calls :func:`refresh_entry_assignment` so the entry is **created** (when
    the work was never enqueued) or **re-keyed** to the latest fix assignment
    (the #292 bounce fix).  :func:`enqueue` is *not* used because it cannot
    update an existing entry's ``assignment_id``; ``refresh_entry_assignment``
    handles both cases.

    Idempotent: a second call with the same board produces no further changes
    (``refresh_entry_assignment`` is a no-op when the entry already exists and
    is keyed correctly).

    #1490: a fix/bounce cycle piles up more than one ``WORK_LIKE_TYPES`` row
    on the *same* branch (the original dispatch plus every retry), and each
    stays in ``board.completed`` forever. Processing every such row
    independently — the pre-#1490 behaviour — re-keyed the branch's one
    queue entry once per row, every single tick, because the review/smoke
    gates are resolved over the whole branch chain (so even a *failed*-test
    row passes them) and there was nothing to stop each row's turn from
    winning the re-key. :func:`group_branch_candidates` now resolves every
    branch to a single winner up front (the most-recently-dispatched row
    with a passed/skipped verdict — falling back to the most recent row
    overall when none has passed yet); every other row on that branch is
    logged (:func:`_log_superseded`) and never touches the queue.

    Returns a list of assignment IDs for which an entry was created or updated.
    Call sites use this list for diagnostic logging; callers that don't need it
    can discard the return value.

    Called from the daemon passive tick (:func:`coord.serve_app._passive_tick`)
    on every interval so approved work enters the queue without requiring a
    manual ``coord merge`` run (#736 / #217 invisible limbo).
    """
    from coord import github_ops as _gho  # noqa: PLC0415

    if board is None:
        from coord.state import build_board as _build_board  # noqa: PLC0415
        board = _build_board()

    changed: list[str] = []
    terminal_cache: dict = {}
    milestone_cache: dict = {}

    completed = list(getattr(board, "completed", []) or [])
    existing_queue = load_queue()

    for a, superseded in group_branch_candidates(completed):
        for row in superseded:
            _log_superseded(row)

        branch = a.branch
        aid = a.assignment_id
        repo_name = a.repo_name
        repo_cfg = config.repo(repo_name)
        if repo_cfg is None:
            continue

        # Skip if the assignment is already in the queue under its own ID.
        # refresh_entry_assignment would create a second entry when no entry
        # exists with a matching branch, even if one exists with the same
        # assignment_id (e.g. seeded with a different branch in the queue).
        # This guard prevents double-entries; re-keying is still handled
        # because for fix-work the new aid is NOT yet in the queue.
        if any(x.assignment_id == aid for x in existing_queue):
            continue

        # #934: target the milestone's `feature/ms-NN` branch, not
        # `default_branch`, when this issue belongs to a milestone and the
        # repo opted into the develop + feature-branch-per-milestone git
        # model. The milestone lookup itself is skipped entirely (no `gh`
        # call) when the repo hasn't opted in — fails open to
        # `default_branch`, today's behavior, unchanged.
        # #2085: resolved BEFORE the gate check now (it used to run after) —
        # `live_gate_entry` below needs a target_branch to populate the
        # #821/#1479 freshness anchors live.
        from coord.branch_model import resolve_base_branch_for_issue_number  # noqa: PLC0415

        target_branch = resolve_base_branch_for_issue_number(
            repo_cfg,
            repo_cfg.github,
            getattr(a, "issue_number", 0),
            cache=milestone_cache,
        )

        # Gates 1+2: review + smoke, via the shared predicate (#946) so this
        # path stays in lockstep with the `coord merge` auto-enqueue loop and
        # the raw `enqueue()` helper.  Only blocks when a gate is configured
        # AND not satisfied — passes_merge_gates itself no-ops a disabled gate.
        #
        # #2085: `a` is a raw work Assignment — it has no `branch_head_sha`/
        # `branch_patch_id`/`repo_github`/`target_branch` attribute at all,
        # so handing it straight to `passes_merge_gates` made
        # `has_approved_review`'s #821 SHA-freshness check permanently
        # unconfirmable (fails closed on every review that carries a real
        # `review_head_sha` — i.e. virtually every modern approval, not just
        # the superseded-approval case this gate exists to catch).
        # `live_gate_entry` builds the same live-anchored synthetic entry
        # `coord.gates.build_gate_report` uses, so a genuinely fresh approval
        # can still be confirmed via `_gho` (already available in this
        # function for the terminal-state check below).
        gate_entry = live_gate_entry(a, repo_cfg.github, target_branch, _gho)
        if not passes_merge_gates(gate_entry, config, board, gh_ops=_gho):
            continue

        # Gate 3: not already terminal on GitHub (merged / closed).  Fail OPEN
        # on transient gh errors so a network blip never blocks a real enqueue.
        #
        # #2639: this function processes WORK_LIKE_TYPES rows (not just
        # type='work' — see module docstring), so a test-author/mock-author
        # `a` must not trust its (tracking-issue) `issue_number`'s closed
        # state, or a closed epic silently blocks its enqueue forever.
        if _gho.work_is_terminal(
            repo_cfg.github,
            getattr(a, "issue_number", 0),
            branch,
            cache=terminal_cache,
            trust_issue_closed=trust_issue_closed_for(getattr(a, "type", None)),
        ):
            continue

        if refresh_entry_assignment(
            a,
            repo_github=repo_cfg.github,
            target_branch=target_branch,
        ):
            changed.append(aid)

    return changed


def refresh_entry_assignment(
    assignment: Assignment,
    repo_github: str,
    target_branch: str,
) -> bool:
    """Ensure a PENDING queue entry exists for *assignment*'s branch and
    is keyed to *assignment*.

    #292 (Defect 2): after a review bounce the entry was created during an
    earlier ``coord merge`` run and is keyed to the *original* work
    assignment.  When the fix work gets approved, the entry's
    ``assignment_id`` must be updated so ``has_approved_review`` (and the
    matching TUI check) can find the approval.

    - If no entry exists for the branch, one is created (same as
      ``enqueue``).
    - If an entry already exists keyed to a different assignment on the
      same branch and its state is ``PENDING``, its ``assignment_id`` is
      updated and any stale ``"review required"`` error is cleared.
    - If the entry is in a terminal state (MERGED, CONFLICT, etc.) it is
      left untouched.

    Returns ``True`` when a change was made (entry created or updated).
    """
    from coord import github_ops as _gho  # noqa: PLC0415

    if not assignment.branch or not assignment.assignment_id:
        return False
    items = load_queue()
    # Match by (repo_github, branch) first; also accept a match by
    # assignment_id alone so that a queue entry with a different branch but
    # the same assignment_id (e.g. a test-seeded entry or a manually-created
    # entry) is treated as "already present" rather than spawning a second row.
    existing = next(
        (
            x for x in items
            if (x.repo_github == repo_github and x.branch == assignment.branch)
            or x.assignment_id == assignment.assignment_id
        ),
        None,
    )
    if existing is None:
        # #776: populate size eagerly (same as enqueue()) and record enqueued_at.
        try:
            diff_size: int | None = _gho.get_branch_diff_size(
                repo_github, target_branch, assignment.branch
            ) or None
        except Exception:  # noqa: BLE001
            diff_size = None

        entry = QueuedMerge(
            assignment_id=assignment.assignment_id,
            repo_name=assignment.repo_name,
            repo_github=repo_github,
            branch=assignment.branch,
            target_branch=target_branch,
            issue_number=assignment.issue_number,
            issue_title=assignment.issue_title,
            size=diff_size,
            enqueued_at=time.time(),
            assignment_type=assignment.type,
            # #1213: snapshot the resolved gate list, same as enqueue().
            required_gates=list(assignment.required_gates or []),
        )
        items.append(entry)
        save_queue(items)
        return True
    if existing.assignment_id == assignment.assignment_id:
        return False  # already correct
    if existing.state != PENDING:
        return False  # don't touch terminal entries (MERGED, CONFLICT, etc.)
    existing.assignment_id = assignment.assignment_id
    # #1077 (review round 1): do NOT overwrite existing.assignment_type here.
    # assignment_type is a structural property of the branch/issue pairing,
    # fixed once at enqueue() time -- not something to refresh from whatever
    # assignment last touched the branch. A review-bounce fix worker is
    # unconditionally dispatched with type="work" (auto_loop.py's
    # _dispatch_fix_for_review), regardless of the original assignment's
    # type, so re-keying assignment_type here would clobber a "mock-author"
    # entry back to "work" on every ordinary request-changes round trip --
    # silently re-enabling the close-on-merge behavior this issue fixed.
    # assignment_id legitimately needs to track the latest fix (for
    # approval-lookup purposes via has_approved_review), but assignment_type
    # does not -- a bounce/fix iteration is conceptually still "fixing the
    # same PR", so the type set at enqueue() stays authoritative.
    # Clear a stale "review required" error now that a fresh approval arrived.
    if existing.error == "review required but not approved":
        existing.error = None
    save_queue(items)
    return True


# ── Stale-conflict reconciliation (#1477) ───────────────────────────────────

def reconcile_conflict_entries(gh_ops: "GhOps") -> list["MergeEvent"]:
    """Re-test every ``CONFLICT`` entry's mergeability and clear stale verdicts.

    A ``CONFLICT`` entry caches the ``gh pr merge`` failure message from
    whenever the queue last attempted it, and ``process()`` never looks at it
    again — it only ever iterates ``PENDING`` entries. When a conflict-fix
    worker (#241) lands a rebase, or a human pushes a fix by hand, the branch
    becomes clean but the entry sits parked on the old verdict forever,
    requiring the three-step manual incantation described in #1477
    (``--drop`` → a bare re-enqueue → ``--only``) to notice.

    This re-tests GitHub's own mergeability computation for every
    ``CONFLICT`` entry that has an open PR and, when it now reports clean,
    returns the entry to ``PENDING`` and clears the stored error so it
    re-enters the ordinary merge flow on this tick — no manual archaeology.

    Fail-closed by design: an entry with no PR yet, or whose mergeability
    can't be determined (``gh`` error, or GitHub still computing it — both
    surface as ``None`` from :meth:`GhOps.check_pr_mergeable`), is left
    untouched. Only an explicit ``True`` unparks it — never speculative.

    #1467: a ``MERGEABLE`` verdict only reflects *content* conflicts — it
    says nothing about whether a ``--rebase`` merge specifically will
    succeed, because GitHub reports a branch carrying a merge commit as
    ``MERGEABLE`` even though it flatly refuses to rebase-merge it. An
    entry parked on that particular refusal (:func:`is_rebase_refusal`)
    would otherwise unpark here, hit the exact same wall in :func:`process`,
    and re-park — an infinite loop once auto-drain is on (#1491). For those
    entries specifically, this also confirms via
    :meth:`GhOps.branch_has_merge_commit` that the branch has actually gone
    linear before unparking; an inconclusive read (``None``, or a ``gh_ops``
    that doesn't support the probe) leaves the entry parked rather than
    guessing — the same fail-closed posture as the mergeability check above.
    A plain content conflict (no rebase-refusal wording) is unaffected and
    keeps the original mergeable-only behaviour.

    Loads and saves the queue directly (same shape as
    :func:`enqueue_approved_work`), so this is safe to call unconditionally,
    even under ``--dry-run``: it corrects previously-cached state rather than
    taking a merge action, mirroring the auto-enqueue scan that already runs
    regardless of ``--dry-run`` in ``coord merge``.

    Returns the list of :class:`MergeEvent` for entries that were cleared, so
    callers can echo them the same way they echo ``process()`` events.
    """
    items = load_queue()
    events: list[MergeEvent] = []
    changed = False
    for entry in items:
        if entry.state != CONFLICT or not entry.pr_number:
            continue
        try:
            mergeable = gh_ops.check_pr_mergeable(entry.repo_github, entry.pr_number)
        except Exception:  # noqa: BLE001 — never let a gh hiccup wedge the tick
            mergeable = None
        if mergeable is not True:
            continue
        if is_rebase_refusal(entry.error):
            probe = getattr(gh_ops, "branch_has_merge_commit", None)
            if probe is None:
                continue  # can't confirm linearity — stay parked (#1467)
            try:
                has_merge_commit = probe(entry.repo_github, entry.pr_number)
            except Exception:  # noqa: BLE001
                has_merge_commit = None
            if has_merge_commit is not False:
                # Still has a merge commit, or the probe was inconclusive —
                # unparking now would just reproduce the same refusal.
                continue
        entry.state = PENDING
        entry.error = None
        changed = True
        events.append(MergeEvent(
            entry, "reopened",
            f"conflict cleared — PR #{entry.pr_number} ({entry.branch}) is "
            "mergeable again, returned to pending",
        ))
    if changed:
        save_queue(items)
    return events


# ── Post-merge sibling conflict sweep (#2246) ───────────────────────────────

#: How many times the sweep re-asks GitHub for a sibling's mergeability while
#: it still reads ``UNKNOWN``. GitHub computes ``mergeable`` asynchronously and
#: a merge that JUST landed is precisely the moment it is least likely to have
#: settled — treating the first ``None`` as "clean" is the bug (#2246), so the
#: probe is retried a bounded number of times before giving up.
SIBLING_SWEEP_ATTEMPTS = 3

#: Seconds between sweep probe rounds. Total added wall-clock is at most
#: ``(SIBLING_SWEEP_ATTEMPTS - 1) * SIBLING_SWEEP_INTERVAL`` regardless of how
#: many siblings are in the queue — see :func:`sweep_sibling_conflicts`, which
#: probes in ROUNDS (everyone, then only the still-unresolved) rather than
#: exhausting the retry budget per entry.
SIBLING_SWEEP_INTERVAL = 2.0


def merged_bases(events: Iterable["MergeEvent"]) -> set[tuple[str, str]]:
    """The ``(repo_github, target_branch)`` pairs a batch of events just moved.

    Scoped deliberately to ``kind == "merged"``: a base branch only invalidates
    siblings when something actually landed on it. Every other event kind
    (``opened``, ``sized``, ``conflict``, gate blocks) leaves the base exactly
    where it was and must not trigger a sweep.
    """
    return {
        (ev.entry.repo_github, ev.entry.target_branch)
        for ev in events
        if ev.kind == "merged" and ev.entry.repo_github and ev.entry.target_branch
    }


def sibling_sweep_candidates(
    events: Iterable["MergeEvent"], items: Iterable["QueuedMerge"],
) -> list["QueuedMerge"]:
    """The queue entries a just-landed merge could plausibly have broken.

    An entry qualifies when **all** of the following hold:

    * Its ``(repo_github, target_branch)`` is one the batch actually moved
      (:func:`merged_bases`) — #2246's "scope to the merged repo, and only to
      PRs whose base is the branch that just moved".
    * It is not itself one of the entries that merged.
    * It has an open PR (``pr_number``) — GitHub can only compute
      mergeability for a PR, and an entry with no PR has nothing to mark.
    * It is still ``PENDING``. This is the **transition** filter #2246 asks
      for, expressed in the state the queue already keeps: ``CONFLICT`` is the
      durable record of "we already know this one is conflicting", so an entry
      parked there was somebody else's problem before this merge and is left
      alone (it is #1477's :func:`reconcile_conflict_entries` that unparks it).
      ``HUMAN_REQUIRED``/``MERGED``/``SKIPPED`` are equally not ours to touch.
    * Its cached ``error`` does not already read as a conflict. Belt-and-braces
      for the same rule: an entry can be ``PENDING`` while carrying a conflict
      error from a prior attempt (a re-enqueue, a #1477 unpark that raced),
      and re-dispatching a conflict-fix for it on every merge is exactly the
      loop #2246 says not to build.

    Note what is deliberately *not* required: that the sibling be blocked on
    some particular gate. The whole point is that the gate it is blocked on
    (stale smoke verdict, "checks_failed (unknown)") is the WRONG reason — the
    real blocker is the conflict, and it is only visible if we look regardless.
    """
    moved = merged_bases(events)
    if not moved:
        return []
    merged_ids = {
        ev.entry.assignment_id for ev in events if ev.kind == "merged"
    }
    out: list[QueuedMerge] = []
    for entry in items:
        if entry.assignment_id in merged_ids:
            continue
        if (entry.repo_github, entry.target_branch) not in moved:
            continue
        if entry.state != PENDING or not entry.pr_number:
            continue
        if classify_conflict(entry.error) == "rebaseable":
            continue
        out.append(entry)
    return out


def sibling_conflict_error(
    entry: "QueuedMerge", events: Iterable["MergeEvent"],
) -> str:
    """The ``entry.error`` text a swept sibling is parked with.

    Two hard requirements, both load-bearing:

    1. It must contain wording :func:`classify_conflict` reads as
       ``"rebaseable"`` (here: ``merge conflict``), because the whole value of
       the sweep is that ``coord merge``'s existing ``_dispatch_conflict_fixes``
       step then routes it to the #241 worker with every guard intact — the
       retry cap, the in-flight check, the HUMAN_REQUIRED escalation.
    2. It must NOT contain any ``_HUMAN_SIGNALS`` wording ("review required",
       "permission", "protected branch", …), which would classify it as a
       branch-protection problem and escalate straight to a human.

    Beyond that it names the merge that caused it, because "which PR broke me"
    was the fact a human had to reconstruct by hand in both 2026-08-14
    collisions.
    """
    culprits = sorted({
        f"{ev.entry.repo_name}#{ev.entry.issue_number}"
        + (f" (PR #{ev.entry.pr_number})" if ev.entry.pr_number else "")
        for ev in events
        if ev.kind == "merged"
        and ev.entry.repo_github == entry.repo_github
        and ev.entry.target_branch == entry.target_branch
    })
    blame = ", ".join(culprits) if culprits else "a sibling merge"
    return (
        f"merge conflict: GitHub reports PR #{entry.pr_number} ({entry.branch}) "
        f"as CONFLICTING against {entry.target_branch} immediately after "
        f"{blame} landed on it. The blocker is a content conflict — not a "
        "stale test verdict and not a CI failure (a conflicting PR has no "
        f"{entry.target_branch} merge ref, so GitHub queues no pull_request "
        "check-suites for it at all). Rebase the branch onto "
        f"{entry.target_branch} and resolve (#2246)."
    )


def sweep_sibling_conflicts(
    events: list["MergeEvent"],
    items: list["QueuedMerge"],
    gh_ops: "GhOps",
    *,
    attempts: int = SIBLING_SWEEP_ATTEMPTS,
    interval: float = SIBLING_SWEEP_INTERVAL,
    sleep=None,
    persist: bool = True,
) -> list["MergeEvent"]:
    """Ask GitHub which siblings *events*' merges just broke (#2246).

    When a PR merges it can silently invalidate every other open PR against
    the same base. GitHub computes that for us exactly and for free, and
    before this nothing asked at the one moment it matters — ``mergeable`` was
    only consulted at merge time, i.e. after the next drive attempt was
    already spent. On 2026-08-14 that cost four terminal ``blocked`` entries
    across two repos, presented as "smoke gate — test verdict stale" and
    "checks_failed … (unknown)", neither of which said *conflict*.

    Called immediately after :func:`process` with the events it returned and
    the item list the caller is about to persist. For every sibling that now
    reads ``CONFLICTING`` (see :func:`sibling_sweep_candidates` for who
    qualifies), the entry is moved to ``CONFLICT`` with a
    :func:`sibling_conflict_error` explaining what happened, and a
    ``conflict`` :class:`MergeEvent` is returned. The caller echoes those
    events and hands them to its usual conflict-fix dispatch step — the sweep
    itself never dispatches, so #241's retry cap and in-flight guards keep
    living in exactly one place.

    **UNKNOWN is not clean.** GitHub recomputes mergeability asynchronously,
    and the instant after a merge is when it is most likely to still be
    computing — the first read came back ``UNKNOWN`` repeatedly in the
    2026-08-14 session. Probing is therefore retried up to *attempts* times,
    in ROUNDS: every unresolved sibling is probed, then only the ones still
    unresolved are probed again after *interval* seconds. Total added
    wall-clock is bounded by ``(attempts - 1) * interval`` no matter how many
    siblings there are, rather than growing per entry.

    **Fails open, always.** A ``gh`` error, a ``gh_ops`` with no
    ``check_pr_mergeable`` (the duck-typed stubs in older tests), an
    unreadable queue — every one of them yields "no sweep", never an
    exception. The merge that triggered this already succeeded; a read failure
    afterwards must not undo or obscure it.

    Cost is one API call per candidate sibling per merge — single digits on
    this fleet — and zero when nothing merged, which is the overwhelmingly
    common tick.

    Persistence: mutations are written straight back to the queue (``persist``
    is only turned off by tests). Entries the caller already holds in *items*
    are mutated **in place**, so the caller's own subsequent
    ``save_queue(merge_over_disk)`` step re-writes the same values rather than
    clobbering them — the same convention
    ``coord.commands.merge._dispatch_conflict_fixes`` relies on. Siblings the
    caller does *not* hold (the ``--only`` path, where ``items`` is a single
    entry) are persisted by this function's own write, which reloads the
    queue fresh and overrides **only** the entries this call itself moved to
    ``CONFLICT`` (one per event in the return value) — never the full *rows*
    snapshot this function built before its retry-sleep loop, which can be
    stale by the time we get here and would otherwise clobber any concurrent
    writer's changes to unrelated rows.
    """
    if not any(ev.kind == "merged" for ev in events):
        return []
    probe = getattr(gh_ops, "check_pr_mergeable", None)
    if probe is None:
        return []

    try:
        disk = load_queue()
    except Exception:  # noqa: BLE001 — a queue read error must not undo a merge
        _log.warning("sibling sweep: could not read the merge queue", exc_info=True)
        return []

    # Prefer the caller's live objects for any row it already holds: mutating
    # a second copy loaded here would be silently reverted by the caller's own
    # save-over-disk step a few lines later.
    pool = {x.assignment_id: x for x in items}
    rows = [pool.get(x.assignment_id, x) for x in disk]
    known = {x.assignment_id for x in rows}
    rows.extend(x for x in items if x.assignment_id not in known)

    candidates = sibling_sweep_candidates(events, rows)
    if not candidates:
        return []

    _sleep = sleep if sleep is not None else time.sleep
    verdicts: dict[str, bool] = {}
    unresolved = list(candidates)
    for attempt in range(max(1, attempts)):
        if not unresolved:
            break
        if attempt:
            # Only ever paid when GitHub is genuinely still computing.
            _sleep(interval)
        still: list[QueuedMerge] = []
        for entry in unresolved:
            try:
                verdict = probe(entry.repo_github, entry.pr_number)
            except Exception:  # noqa: BLE001 — fail open, per entry
                verdict = None
            if verdict is None:
                still.append(entry)
            else:
                verdicts[entry.assignment_id] = bool(verdict)
        unresolved = still

    for entry in unresolved:
        # Never resolved inside the budget. Deliberately NOT marked: an
        # inconclusive read is not evidence of a conflict, and #1477's
        # reconcile pass plus the next ordinary merge attempt will both look
        # again. Logged so a systematically-slow repo is attributable.
        _log.info(
            "sibling sweep: %s#%s (PR #%s) still UNKNOWN after %d attempt(s)"
            " — left PENDING",
            entry.repo_name, entry.issue_number, entry.pr_number, max(1, attempts),
        )

    out: list[MergeEvent] = []
    for entry in candidates:
        if verdicts.get(entry.assignment_id) is not False:
            continue
        entry.error = sibling_conflict_error(entry, events)
        entry.state = CONFLICT
        out.append(MergeEvent(
            entry, "conflict",
            f"became CONFLICTING when a sibling merged into "
            f"{entry.target_branch} — parked as a conflict, not a gate "
            "failure (#2246)",
        ))

    if out and persist:
        try:
            fresh = load_queue()
            # Scope the override to the entries THIS call actually mutated
            # (one per `out` event) — never the full `rows` snapshot. `rows`
            # was built before the retry-sleep loop above (up to
            # ``(attempts - 1) * interval``, longer under real GitHub
            # latency), so by the time we get here it can be stale for every
            # entry in the queue, not just the swept candidates. Building
            # `by_id` from it and replacing the whole table would silently
            # revert any concurrent writer's changes to unrelated rows made
            # during that wait — exactly the convention this function's
            # docstring promises to follow (mutate in place, override only
            # what changed, default everything else to the fresh read).
            by_id = {ev.entry.assignment_id: ev.entry for ev in out}
            save_queue([by_id.get(x.assignment_id, x) for x in fresh])
        except Exception:  # noqa: BLE001 — fail open
            _log.warning(
                "sibling sweep: could not persist conflict markers", exc_info=True,
            )
    return out


def resolve_entry_key(items: list["QueuedMerge"], key: str) -> "QueuedMerge | None":
    """Resolve *key* to a queue entry by whatever identifier the read path
    printed — ``assignment_id``, the durable ``repo#issue`` form, a bare
    issue number, or the branch name (#1477, #1490).

    ``assignment_id`` is volatile across a drop + re-enqueue cycle: a fresh
    row mints whatever assignment id the board currently shows for that
    issue, which is not guaranteed to match the id an operator last saw in
    ``coord status`` (#1477). #1490 sharpens this further: even *without* a
    drop, a queue entry can legitimately be re-keyed between the moment the
    board is read and the moment ``--only`` is invoked (a concurrent
    auto-enqueue tick re-keying the branch's one entry to a newer fix
    assignment) — so an id that was 100% correct when printed can already
    be stale by the time it's passed here. Every fallback below resolves by
    something that does *not* change out from under the operator for the
    life of the entry.

    Resolution order (first match wins):

    1. Exact ``assignment_id`` — unchanged, most specific.
    2. ``repo#issue`` (or ``repo_github#issue``) — only tried when *key*
       contains ``#`` (plain ids/branches never do, so this can never
       accidentally shadow one). A parse failure after ``#`` is a hard
       miss — no fallthrough to the forms below.
    3. A bare issue number — *key* parses as an ``int`` with no ``#``.
       Matches ``entry.issue_number`` across every repo in *items*;
       ambiguous only when the same issue number is queued for more than
       one repo, in which case (like form 2) the most recently added match
       wins.
    4. The entry's own ``branch`` name (#1490) — the most stable identifier
       there is: it's set once at enqueue time and never changes for the
       life of the entry, unlike ``assignment_id`` which re-keys on every
       fix/bounce round. This is the fallback the issue calls out
       explicitly: "if an ID is genuinely re-keyed between passes, resolve
       by branch".

    When more than one entry matches forms 2-4, :func:`_pick_ambiguous_match`
    breaks the tie: an entry still in play (not ``MERGED``/``SKIPPED``) is
    preferred over one already at rest, and only among equally-actionable
    (or equally-terminal) candidates does "most recently added" apply
    (``load_queue()`` returns rows in insertion order) — the #1477 tie-break.
    #2080: two sealed slices of the same milestone share one *tracking*
    issue number, so a bare-issue or ``repo#issue`` key is routinely
    ambiguous between them even though each is a distinct, independently
    mergeable entry. Plain "most recent wins" used to resolve that ambiguity
    to whichever slice merged *first* — because merging is what makes an
    entry's ``load_queue()`` position look "most recent" to a re-read of the
    file — permanently orphaning the other slice's ``--only <issue>`` runs
    behind an already-``MERGED`` sibling. Preferring the still-pending entry
    fixes that without changing behaviour for the (still ambiguous, still a
    caller error to rely on) case where two matches are equally actionable —
    use the branch name there.

    Returns ``None`` when nothing matches any form — callers must treat
    that as an explicit error, never a silent no-op (#1477).
    """
    for entry in items:
        if entry.assignment_id == key:
            return entry
    if "#" in key:
        repo_part, _, issue_part = key.rpartition("#")
        try:
            issue_number = int(issue_part)
        except ValueError:
            return None
        matches = [
            e for e in items
            if e.issue_number == issue_number and repo_part in (e.repo_name, e.repo_github)
        ]
        if matches:
            return _pick_ambiguous_match(matches)
        return None
    try:
        bare_issue_number = int(key)
    except ValueError:
        bare_issue_number = None
    if bare_issue_number is not None:
        matches = [e for e in items if e.issue_number == bare_issue_number]
        if matches:
            return _pick_ambiguous_match(matches)
    branch_matches = [e for e in items if e.branch == key]
    if branch_matches:
        return branch_matches[-1]
    return None


# States a queue entry never leaves once reached — resolving a shared key
# (durable repo#issue, or bare issue number) against a mix of these and
# still-actionable states should never silently prefer the entry that's
# already done (#2080).
_RESOLVE_TERMINAL_STATES = (MERGED, SKIPPED)


def _pick_ambiguous_match(matches: list["QueuedMerge"]) -> "QueuedMerge":
    """Break a tie between several :func:`resolve_entry_key` *matches* that
    share one durable key (#2080).

    Two sealed slices of the same milestone share the milestone's tracking
    issue number, so ``repo#issue`` and bare-issue-number resolution
    routinely finds more than one queue entry. Prefer an entry that is still
    actionable (state not in :data:`_RESOLVE_TERMINAL_STATES`) over one
    already at rest — a merged sibling is never the row an operator meant by
    the shared key once anything else with that key is still pending. Among
    equally-actionable (or equally-terminal) candidates, fall back to
    "most recently added" (*matches* is in ``load_queue()`` insertion
    order) — the pre-existing #1477 tie-break, applied only where it can no
    longer pick a done entry over a live one.
    """
    actionable = [e for e in matches if e.state not in _RESOLVE_TERMINAL_STATES]
    pool = actionable if actionable else matches
    return pool[-1]


def resolve_board_work_key(board, key: str) -> "list":
    """Every done work-like board row that *key* addresses (#1695).

    The board-side twin of :func:`resolve_entry_key`, matching the **same
    four key forms** (``assignment_id``, ``repo#issue``, bare issue number,
    branch name) against ``board.completed`` instead of the persisted queue.

    Exists purely so ``coord merge --only`` can tell the two failure modes
    apart when the queue lookup misses:

    * *no board row either* → the identifier genuinely did not resolve, which
      is what the pre-#1695 message ("tried assignment_id, repo#issue, issue
      number, and branch name") always claimed and was usually wrong about;
    * *a board row exists* → the identifier is fine, and the reason there is
      no entry is a gate — which the caller can then name via
      :func:`merge_gate_failures`.

    Returns **all** matches (not just the most recent, unlike
    :func:`resolve_entry_key`'s tie-break) because the caller is producing a
    diagnostic, not choosing a row to act on: it is more useful to report
    every candidate and its gate state than to silently pick one. Ordered as
    they appear in ``board.completed``; ``[]`` when nothing matches or
    *board* is None.
    """
    rows = [
        a for a in (getattr(board, "completed", None) or [])
        if getattr(a, "type", None) in WORK_LIKE_TYPES
    ]
    exact = [a for a in rows if a.assignment_id == key]
    if exact:
        return exact
    if "#" in key:
        repo_part, _, issue_part = key.rpartition("#")
        try:
            issue_number = int(issue_part)
        except ValueError:
            return []
        return [
            a for a in rows
            if a.issue_number == issue_number and a.repo_name == repo_part
        ]
    try:
        bare_issue_number: int | None = int(key)
    except ValueError:
        bare_issue_number = None
    if bare_issue_number is not None:
        matches = [a for a in rows if a.issue_number == bare_issue_number]
        if matches:
            return matches
    return [a for a in rows if a.branch == key]


# ── Plan-status constants (#776) ─────────────────────────────────────────────

# Computed status values for PlannedMerge.status — not stored in the DB.
PLAN_READY = "READY"
PLAN_BLOCKED = "BLOCKED"
PLAN_MERGING = "MERGING"
PLAN_MERGED = "MERGED"
PLAN_NEEDS_ATTENTION = "NEEDS_ATTENTION"


# ── Gate evaluation (#776) ──────────────────────────────────────────────────

def _entry_gate_status(
    entry: "QueuedMerge",
    board,
    config,
    ci_store: "CiStore | None" = None,
    gh_ops: "GhOps | None" = None,
) -> tuple[str, str | None]:
    """Return *(status, reason)* for a single PENDING merge-queue entry.

    Evaluates gates in the same order as :func:`process` — review → smoke →
    CI → epic-closing-keyword-in-commit — so the plan shown to the operator
    is byte-for-byte what merge would do. Both :func:`plan` and :func:`process`
    delegate to this helper so they can never diverge.

    Returns ``(PLAN_READY, None)`` when all gates pass.
    Returns ``(PLAN_BLOCKED, reason)`` when any gate blocks.

    The *ci_store* gate is only evaluated when both *ci_store* is provided
    **and** the entry has a ``pr_number`` (CI is checked per-PR, not per-branch).
    This mirrors the live-merge behaviour: a ``PENDING`` entry with no PR yet
    opened is not blocked on CI — the PR hasn't been created yet.

    The *gh_ops* epic-closing-keyword-in-commit gate (#1318) is likewise only
    evaluated when both *gh_ops* is provided **and** the entry has a
    ``pr_number`` — mirroring the CI gate's guard, since commit messages can
    only be read once a PR exists. This gate is never bypassable via
    ``force_merge`` here (unlike :func:`process`'s live override) — the plan
    view has no such flag; an operator who wants to see the override outcome
    reads the ``coord merge --force-merge`` output itself.

    #1851: a CI gate that reads all-green additionally checks *staleness* —
    whether every passing check predates the target branch's current HEAD
    commit, which GitHub's own re-run-on-``synchronize``-only behaviour never
    catches. Reported with the :data:`CI_STALE_PREFIX` wording, distinct from
    "CI failed"/"CI running" so an operator (and :func:`ci_revalidation_candidates`,
    which keys off the same prefix) can tell the three apart.
    """
    smoke: "SmokeVerdictStatus | None" = None
    if config is not None and board is not None:
        # #1506: pass gh_ops through so a null branch_patch_id (e.g. an entry
        # whose approved review predates #1475) is computed on demand rather
        # than displaying a stale "review not approved" the plan can't fix.
        if requires_review(entry, config):
            review_scan = scan_approved_reviews(entry, board, gh_ops)
            if not review_scan.approved:
                # #2704: don't render "not approved" for a branch head this
                # scan couldn't even read — see `ApprovalScan.unknown_head`.
                return PLAN_BLOCKED, (
                    UNKNOWN_BRANCH_HEAD_REASON
                    if review_scan.unknown_head
                    else "review not approved"
                )
        if requires_smoke(entry, config):
            # #1640: render the specific failure. A stale verdict used to be
            # reported with the same "test verdict missing" wording as one
            # that was never recorded, which sent operators hunting a lost
            # write instead of re-verifying against the moved base.
            smoke = evaluate_smoke_verdict(entry, board, gh_ops)
            if not smoke.ok:
                # #2231: name the conflict when there is one — the plan view
                # is where an operator (and `coord drive`, via the board's
                # `merge_reason`) reads WHY an entry is stuck, and "test
                # verdict stale" on a branch that doesn't compose sends both
                # of them at a remedy that cannot work. Costs one `gh` call,
                # only for an entry already blocked on a STALE verdict, and
                # only where a live probe exists (never on the `/board` read
                # path — see `stale_smoke_conflict_reason`).
                conflict_reason = stale_smoke_conflict_reason(
                    entry, smoke, gh_ops,
                )
                if conflict_reason is not None:
                    return PLAN_BLOCKED, conflict_reason
                return PLAN_BLOCKED, smoke.short_reason
        # #2687: UAT gate, ordered between review/smoke and CI/merge —
        # matching the issue's "ordered between review and merge". No
        # staleness recompute needed (unlike the smoke branch above): a UAT
        # verdict carries no SHA anchor to go stale against, so the message
        # `evaluate_uat_verdict` returns here is already exactly what a
        # live merge attempt would report.
        if requires_uat(entry, config):
            uat_ok, uat_message = evaluate_uat_verdict(entry, board, config, gh_ops)
            if not uat_ok:
                return PLAN_BLOCKED, uat_message
    if ci_store is not None and ci_store.is_available and entry.pr_number:
        checks = ci_store.list_checks_for_pr(entry.repo_github, entry.pr_number)
        # #1904: an empty check list satisfies every gate below vacuously —
        # must be handled explicitly, before those gates, or a PR whose CI
        # never ran at all reads as clear to merge.
        if not checks and _ci_expects_checks(
            ci_store, entry.repo_github, entry.pr_number
        ):
            # #1877: an empty check list is ALSO what GitHub reports for a
            # PR that conflicts with its base — it can never build a merge
            # ref, so no `pull_request`-triggered workflow ever runs. That
            # is a different fact from "CI never ran on a mergeable PR" and
            # needs the opposite response: don't block here, mirror what
            # `process()` does — fall through so `coord merge` attempts the
            # merge, discovers the real conflict, and routes to the #241
            # conflict-fix path via the `conflict` event, instead of
            # pre-empting it with a "CI never ran" block only a human can
            # clear. `check_pr_mergeable` is duck-typed/optional here
            # (unlike in `process()`, where `gh_ops` is always a live
            # client): this function also runs against a `GateSnapshot`
            # (the `/board` read path, #1336 Invariant 1 — no third-party
            # I/O), which doesn't implement it — a missing probe reads as
            # inconclusive, same as a `None`/confirmed-mergeable verdict,
            # and today's block is left untouched.
            _mergeable_probe = getattr(gh_ops, "check_pr_mergeable", None)
            conflicted = False
            if _mergeable_probe is not None:
                try:
                    conflicted = (
                        _mergeable_probe(entry.repo_github, entry.pr_number)
                        is False
                    )
                except Exception:  # noqa: BLE001 — inconclusive, not a block override
                    conflicted = False
            if not conflicted:
                return (
                    PLAN_BLOCKED,
                    f"{CI_ABSENT_PREFIX} no checks reported for PR #{entry.pr_number} "
                    "though this repo declares CI — merging would run untested code",
                )
        failed = failed_checks(checks)
        if failed:
            # #2347: classify a bare check-list FETCH failure (GitHub
            # unreachable) before the generic "CI failed" wording below —
            # see :func:`_ci_unreadable_reason`. No extra I/O needed (unlike
            # the #1892 infra classifier, which is why THAT one is confined
            # to the live merge path only), so the board/plan read path can
            # apply it directly and stay byte-for-byte in sync with
            # `process()`'s own live reading.
            unreadable_reason = _ci_unreadable_reason(failed)
            if unreadable_reason is not None:
                # #2380: a fetch failure this uniform (EVERY failing check is
                # the synthetic unreadable stand-in) is ALSO exactly what a
                # DIRTY/CONFLICTING PR produces — see
                # :func:`_pr_reports_conflicting`'s docstring. Same "don't
                # block, mirror `process()`, let the real conflict surface"
                # response as the #1877 checks-absent case immediately above
                # — deliberately NOT falling into the `pending`/staleness
                # checks below, which assume (per `_ci_checks_are_stale`'s
                # own docstring) a *non-empty, no-failed-entries* check list;
                # this one is neither.
                if not _pr_reports_conflicting(
                    gh_ops, entry.repo_github, entry.pr_number
                ):
                    return PLAN_BLOCKED, unreadable_reason
            else:
                summary = ", ".join(f"{c.name} ({c.conclusion})" for c in failed)
                return PLAN_BLOCKED, f"CI failed: {summary}"
        else:
            pending = in_flight_checks(checks)
            if pending:
                summary = ", ".join(c.name for c in pending)
                return PLAN_BLOCKED, f"{CI_PENDING_PREFIX} {summary}"
            if checks and _ci_checks_are_stale(
                checks, gh_ops, entry.repo_github, entry.target_branch, smoke
            ):
                # #1826: name the anchors, #1479-style. Costs one extra
                # timestamp read, only for an entry already blocked.
                return (
                    PLAN_BLOCKED,
                    ci_stale_reason(
                        checks, gh_ops, entry.repo_github, entry.target_branch
                    ),
                )
    if gh_ops is not None and entry.pr_number:
        try:
            commit_messages = gh_ops.get_pr_commit_messages(
                entry.repo_github, entry.pr_number
            )
        except Exception:  # noqa: BLE001
            commit_messages = []
        commit_referenced: set[int] = set()
        for message in commit_messages:
            commit_referenced.update(find_closing_references(message))
        commit_epic_hits: list[int] = []
        for n in sorted(commit_referenced):
            try:
                if gh_ops.is_epic_issue(entry.repo_github, n):
                    commit_epic_hits.append(n)
            except Exception:  # noqa: BLE001
                pass
        if commit_epic_hits:
            numbers_str = ", ".join(f"#{n}" for n in commit_epic_hits)
            return (
                PLAN_BLOCKED,
                f"commit message contains closing keyword for epic {numbers_str} (#1318)",
            )
    return PLAN_READY, None


def entry_gate_status(
    entry: "QueuedMerge",
    board,
    config,
    ci_store: "CiStore | None" = None,
    gh_ops: "GhOps | None" = None,
) -> tuple[str, str | None]:
    """Public seam onto :func:`_entry_gate_status`, for callers outside this
    module that need a fresh, SINGLE-entry re-derivation of the gate — not
    the whole-queue :func:`plan`.

    #2182: ``coord.drive_queue``'s park/resume machinery (#1891/#1892) used
    to release a `parked` entry only two ways — the cached board's own
    `merge_plan` reason re-deriving itself (only true off a live daemon
    `/board`, never on the daemon-host tick itself, which reads the local DB
    directly and never computes `merge_plan` at all — see
    ``coord.commands.drive_queue._local_merge_queue_rows``), or
    :data:`coord.drive_queue.PARK_STALE_SECONDS` ageing the frozen reading
    out after 45 minutes. Both leave a genuinely-clear gate undetected for
    up to the ceiling, even though ``coord merge --plan`` can answer correctly on
    demand at any moment — the same disagreement claude-coordinator#2159
    surfaced (the queue read `parked` while `--plan` read READY, at the SAME
    instant, off the SAME board).

    ``coord.commands.drive_queue._fetch_live_ci_gate`` calls this directly,
    once per currently-`parked` entry, with a LIVE ``ci_store``/``gh_ops`` —
    the same live backend ``coord merge --plan``'s local branch builds — so
    the two readings can no longer disagree. Deliberately NOT routed through
    :func:`plan`: that function evaluates *every* PENDING queue entry with a
    PR, so handing it a live backend would pay for a `gh` call per entry in
    the whole merge queue on every 3-minute tick, not just the bounded
    handful currently parked — reintroducing the unbounded-`gh`-polling
    shape #1344 removed from the read path. A single-entry call, made only
    for entries already sitting in `parked`, keeps the cost exactly where
    the issue says it should be: small and predictable.
    """
    return _entry_gate_status(entry, board, config, ci_store, gh_ops)


# ── Merge plan (#776) ────────────────────────────────────────────────────────

@dataclass
class PlannedMerge:
    """One entry in the server-side merge plan.

    The plan is the single source of truth for ordering and gate-status — it
    is what the TUI panel, the CLI ``--plan`` flag, and auto-drain all consume.
    Unlike ``QueuedMerge``, which is the raw DB row, ``PlannedMerge`` carries
    computed fields (``rank``, ``status``, ``reason``, ``milestone``) that are
    always fresh and never stale.
    """

    assignment_id: str
    repo_name: str
    repo_github: str
    branch: str
    target_branch: str
    issue_number: int
    issue_title: str
    rank: int                    # 1-based, ordered by true merge sequence
    size: int | None             # diff lines (populated at enqueue; None = unknown)
    status: str                  # READY | BLOCKED | MERGING | MERGED | NEEDS_ATTENTION
    reason: str | None           # why it is blocked (None when READY / terminal)
    enqueued_at: float | None    # unix timestamp when the entry was created
    last_attempt: float | None   # unix timestamp of the last merge attempt
    milestone: str | None        # issue milestone title, or None
    # #1344: structured CI rollup so the TUI can render "2✓ 1✗" badges straight
    # from `/board` instead of shelling out to `gh pr checks` itself. `None`
    # when no PR is open yet, or `ci_store` has no checks for this PR.
    pr_number: int | None = None
    ci_summary: "CiCheckSummary | None" = None
    # #2446: the unfiltered (required + advisory) counterpart to
    # `ci_summary` above, which is narrowed to what GitHub's branch
    # protection actually requires — the same scope the merge gate itself
    # evaluates (see `_entry_gate_status`/`coord.ci_github.GitHubCi.
    # list_checks_for_pr`'s docstrings). A merely-advisory check regressing
    # (a flaky/hung job the branch's own protection rules don't wait on
    # either) must never gate a merge attempt, but it's still worth an
    # operator seeing — this is what `coord merge --plan` renders that from.
    # `None` under the same conditions as `ci_summary` (no PR yet, or no
    # `ci_store`); a `CiStore` that predates `list_all_checks_for_pr` yields
    # the same value as `ci_summary` rather than `None`, so a plan built
    # against an older store still shows *something* instead of silently
    # losing the badge.
    ci_summary_all: "CiCheckSummary | None" = None
    # #2397: mirrors `config.merge.auto_drain` (default False) at plan-build
    # time — read-only exposure, no gate-logic change. The TUI's per-issue
    # Merge stage box needs this to tell "nothing retries until a human runs
    # `coord merge`" (auto_drain off) apart from "an automatic retry tick is
    # already handling this" (auto_drain on); today it has no way to know
    # and the box renders identically either way (issue #2397's incident:
    # `#2284`'s acceptance-authoring slice sat "pending — waiting 2:22" with
    # no signal that `merge.auto_drain: false` meant nothing was coming on
    # its own). Same value on every entry in one `plan()` call — it's a
    # single process-wide config flag, not per-entry state.
    auto_drain: bool = False


def _load_milestones_for_queue(
    items: "list[QueuedMerge]",
) -> "dict[tuple[str, int], str | None]":
    """Load milestone titles for each (repo_name, issue_number) in *items*.

    Queries the ``issues`` table in bulk and returns a dict keyed by
    ``(repo_name, issue_number)``.  Missing rows (issue not yet synced) map
    to ``None``.  Any DB error returns an empty dict so the plan degrades
    gracefully.
    """
    if not items:
        return {}
    try:
        conn = get_connection()
        rows = sql.execute(
            conn, "SELECT repo_name, number, milestone_title FROM issues"
        ).fetchall()
        return {
            (r["repo_name"], r["number"]): r["milestone_title"]
            for r in rows
        }
    except Exception:  # noqa: BLE001
        return {}


def _state_to_plan_status(state: str) -> str:
    """Map a ``QueuedMerge.state`` to a ``PlannedMerge.status`` constant."""
    if state == PENDING:
        return PLAN_READY      # will be overridden by gate check if blocked
    if state == MERGING:
        return PLAN_MERGING
    if state == MERGED:
        return PLAN_MERGED
    # CONFLICT, HUMAN_REQUIRED, SKIPPED → surface for operator attention.
    return PLAN_NEEDS_ATTENTION


def plan(
    board,
    config,
    ci_store: "CiStore | None" = None,
    gh_ops: "GhOps | None" = None,
) -> "list[PlannedMerge]":
    """Return the **ordered merge plan** — one :class:`PlannedMerge` per queue entry.

    This is the single source of truth for ordering and gate-status consumed by
    the TUI panel (#B), the CLI ``--plan`` flag (#D), and auto-drain (#E).

    Algorithm
    ---------
    1. Load the queue from the DB.
    2. Group entries by ``(repo_github, target_branch)``.
    3. Within each group, order PENDING entries by ``sequence()`` (size-ascending
       with unknown-size last), then append non-PENDING entries in original DB
       order.
    4. Assign a 1-based ``rank`` globally across all groups (i.e. the first
       PENDING entry across all repos is rank=1 regardless of repo).
    5. For each entry:
       - Derive ``status`` from the raw ``state`` value.
       - For PENDING entries, override with :func:`_entry_gate_status` which
         evaluates review / smoke / CI / epic-closing-keyword-in-commit gates
         live against *board* + *config* + *ci_store* + *gh_ops*.
       - Look up the issue's milestone from the ``issues`` table.

    The function is intentionally **read-only** — no side effects, no DB writes.
    Pass ``board=None`` and/or ``config=None`` to skip the review/smoke gates,
    ``ci_store=None`` to skip the CI gate, and ``gh_ops=None`` to skip the
    epic-closing-keyword-in-commit gate (useful in test scenarios that only
    care about ordering).
    """
    items = load_queue()
    milestones = _load_milestones_for_queue(items)
    # #2397: read once, stamped on every entry below — see `PlannedMerge
    # .auto_drain`'s doc comment for why the TUI needs this alongside
    # `reason`.
    auto_drain = bool(getattr(getattr(config, "merge", None), "auto_drain", False))

    # ── Group by (repo_github, target_branch) ──────────────────────────────
    group_order: list[tuple[str, str]] = []
    groups: dict[tuple[str, str], list[QueuedMerge]] = {}
    for entry in items:
        key = (entry.repo_github, entry.target_branch)
        if key not in groups:
            group_order.append(key)
            groups[key] = []
        groups[key].append(entry)

    # ── Build the ranked plan ───────────────────────────────────────────────
    result: list[PlannedMerge] = []
    rank = 0

    for key in group_order:
        group = groups[key]
        # PENDING entries sorted by sequence(); all others in DB insertion order.
        pending = [e for e in group if e.state == PENDING]
        non_pending = [e for e in group if e.state != PENDING]
        ordered = sequence(pending) + non_pending

        for entry in ordered:
            rank += 1
            base_status = _state_to_plan_status(entry.state)
            reason: str | None = None

            ci_summary = None
            ci_summary_all = None
            if entry.state == PENDING:
                base_status, reason = _entry_gate_status(
                    entry, board, config, ci_store, gh_ops
                )

                # #1344: structured CI rollup for the TUI's badges. Deliberately
                # scoped to PENDING entries only — the same scope as the gate
                # check above — because `ci_store` is not always the cheap,
                # tick-refreshed `GateSnapshot` (a dict lookup). Two other
                # callers pass a freshly-built *live* `CiStore`
                # (`ci_github.GitHubCi`) instead: `_auto_drain_tick`
                # (serve_app.py, every ~30s when `merge.auto_drain` is on) and
                # `coord merge --plan` (commands/merge.py). `merge_queue`
                # never prunes MERGED entries (see
                # `prune_stale_queue_entries`), so on a long-lived project the
                # queue table accumulates unbounded merged history — widening
                # this to "any entry with a pr_number" would fire one live
                # `gh pr checks` subprocess per historical merged PR on every
                # auto-drain tick, reintroducing the exact unbounded-`gh`-
                # polling failure class #1344 removed from the TUI, just
                # relocated to the daemon. It also wouldn't gain anything on
                # the safe /board path: `GateSnapshotRefresher.refresh` itself
                # only ever populates checks for entries that are PENDING at
                # refresh time, so a MERGING/MERGED row never had real
                # snapshot data to render in the first place.
                #
                # #2446: `ci_summary` MUST stay derived from the same
                # (already required-narrowed) `list_checks_for_pr` view
                # `_entry_gate_status` just consulted above — do not widen it
                # to the unfiltered check list. :func:`ci_rollup_all_clear`
                # (used by both `coord.drive_queue`'s #2158 park-recovery and
                # `coord.drive_state`'s #2808 recovery) reads this exact
                # field as positive evidence that a frozen "CI running:"
                # raw-row string has gone stale, and that evidence is only
                # valid when it is counting the SAME checks the live gate
                # decision is based on (see that function's docstring);
                # widening it here would make a still-pending ADVISORY check
                # block the #2158 park-recovery self-heal even though the
                # gate itself (and GitHub's own merge button) is already
                # clear. Advisory visibility is a genuinely separate concern
                # — see
                # `ci_summary_all` below.
                if ci_store is not None and ci_store.is_available and entry.pr_number:
                    checks = ci_store.list_checks_for_pr(entry.repo_github, entry.pr_number)
                    if checks:
                        ci_summary = summarize_counts(checks)

                    # #2446: the unfiltered (required + advisory) rollup —
                    # pure visibility, never consulted by any gate decision
                    # or self-heal evidence. Lets `coord merge --plan`/the
                    # TUI still show a regressed advisory check (e.g.
                    # `Acceptance (web)`) even though it can no longer block
                    # `_entry_gate_status` above or the #2158 recovery read
                    # above. Falls back to `ci_summary` for a `CiStore`
                    # stand-in that predates `list_all_checks_for_pr`.
                    list_all = getattr(ci_store, "list_all_checks_for_pr", None)
                    all_checks = (
                        list_all(entry.repo_github, entry.pr_number)
                        if list_all is not None
                        else checks
                    )
                    if all_checks:
                        ci_summary_all = summarize_counts(all_checks)

            result.append(PlannedMerge(
                assignment_id=entry.assignment_id,
                repo_name=entry.repo_name,
                repo_github=entry.repo_github,
                branch=entry.branch,
                target_branch=entry.target_branch,
                issue_number=entry.issue_number,
                issue_title=entry.issue_title,
                rank=rank,
                size=entry.size,
                status=base_status,
                reason=reason,
                pr_number=entry.pr_number,
                ci_summary=ci_summary,
                ci_summary_all=ci_summary_all,
                enqueued_at=entry.enqueued_at,
                last_attempt=entry.last_attempt,
                milestone=milestones.get((entry.repo_name, entry.issue_number)),
                auto_drain=auto_drain,
            ))

    return result


# ── Sequencing ───────────────────────────────────────────────────────────

def sequence(items: Iterable[QueuedMerge]) -> list[QueuedMerge]:
    """Order pending entries. Smaller diffs first; unknown sizes go last."""
    pending = [x for x in items if x.state == PENDING]
    return sorted(
        pending,
        key=lambda x: (x.size if x.size is not None else 10**9, x.assignment_id),
    )


def reorder(items: list[QueuedMerge], order: list[str]) -> list[QueuedMerge]:
    """Return `items` reordered so that assignment_ids in `order` come first
    in the given sequence. Unknown IDs are dropped from the override."""
    by_id = {x.assignment_id: x for x in items}
    head = [by_id[aid] for aid in order if aid in by_id]
    tail = [x for x in items if x.assignment_id not in set(order)]
    return head + tail


# ── Sibling overlap warnings (#920) ─────────────────────────────────────────
#
# The 2026-07-02 mess (docs referenced in #915) was triggered by late
# merging of overlapping sibling branches: #769/#645/#770 (+#768) were a
# milestone chain all editing the same new files, approved but left sitting
# while main moved, so every rebase collided with its siblings' additions.
# Nothing warned that these would conflict if merged out of order or late.
#
# `find_sibling_overlaps` is a pure, read-only heuristic over the merge
# queue: it groups PENDING (i.e. approved — see `enqueue_approved_work`'s
# review+smoke gate) entries by `(repo_github, target_branch)`, clusters
# same-group entries whose originating assignment's `files_allowed`
# (the brain's inferred "files likely touched" — the same signal
# `compute_do_not_touch` uses pre-dispatch, see `coord.dispatch`) overlap,
# and reports a warning once the oldest member of a ≥2-entry cluster has
# been sitting in the queue at least `config.merge.sibling_overlap_aging_hours`.


@dataclass(frozen=True)
class SiblingOverlapWarning:
    """≥2 approved, aging queue entries whose branches touch the same files.

    `issue_numbers` is already in the suggested merge order — oldest
    ``enqueued_at`` first, since that entry has drifted furthest from a
    moving main and merging it first shrinks the others' eventual rebase.
    """

    repo_name: str
    target_branch: str
    issue_numbers: tuple[int, ...] = field(default_factory=tuple)
    overlapping_files: tuple[str, ...] = field(default_factory=tuple)
    oldest_age_hours: float = 0.0


def find_sibling_overlaps(
    board,
    config,
    *,
    now: float | None = None,
) -> list[SiblingOverlapWarning]:
    """Detect approved, aging, file-overlapping sibling branches in the queue.

    Pure/read-only: loads the queue via :func:`load_queue`, reads
    ``files_allowed`` off the matching assignments on *board*
    (``board.completed`` + ``board.active``), does no GitHub/subprocess
    calls. ``config.merge.sibling_overlap_aging_hours`` (default 24h) gates
    how long the oldest entry in an overlapping cluster must have waited
    before it's worth surfacing — a value of ``0`` (or a missing
    ``merge`` config) disables the warning entirely.

    Only ``PENDING`` entries are considered: by the time an assignment has a
    queue entry, :func:`enqueue`/:func:`enqueue_approved_work` have already
    applied the review+smoke gate, so a PENDING entry is "approved" in the
    sense #920 means. Entries without a recorded ``enqueued_at`` (pre-#274
    rows) are skipped — there's no age to measure.
    """
    aging_hours = getattr(getattr(config, "merge", None), "sibling_overlap_aging_hours", 24.0)
    if not aging_hours or aging_hours <= 0:
        return []
    if now is None:
        now = time.time()

    entries = [e for e in load_queue() if e.state == PENDING and e.enqueued_at is not None]
    if len(entries) < 2:
        return []

    pool = (
        list(getattr(board, "completed", []) or [])
        + list(getattr(board, "active", []) or [])
    )
    files_by_aid: dict[str, set[str]] = {}
    for a in pool:
        aid = getattr(a, "assignment_id", None)
        if aid:
            files_by_aid[aid] = set(getattr(a, "files_allowed", None) or [])

    groups: dict[tuple[str, str], list[QueuedMerge]] = {}
    for e in entries:
        groups.setdefault((e.repo_github, e.target_branch), []).append(e)

    warnings: list[SiblingOverlapWarning] = []
    for (repo_github, target_branch), group in groups.items():
        if len(group) < 2:
            continue

        # Union-find: cluster entries transitively sharing >=1 file.
        parent = {e.assignment_id: e.assignment_id for e in group}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for i in range(len(group)):
            files_i = files_by_aid.get(group[i].assignment_id, set())
            if not files_i:
                continue
            for j in range(i + 1, len(group)):
                files_j = files_by_aid.get(group[j].assignment_id, set())
                if files_i & files_j:
                    union(group[i].assignment_id, group[j].assignment_id)

        clusters: dict[str, list[QueuedMerge]] = {}
        for e in group:
            clusters.setdefault(find(e.assignment_id), []).append(e)

        for members in clusters.values():
            if len(members) < 2:
                continue
            oldest_enqueued = min(m.enqueued_at for m in members)
            age_hours = (now - oldest_enqueued) / 3600.0
            if age_hours < aging_hours:
                continue

            ordered = sorted(members, key=lambda m: (m.enqueued_at, m.assignment_id))
            overlap_files: set[str] = set()
            for i in range(len(ordered)):
                files_i = files_by_aid.get(ordered[i].assignment_id, set())
                for j in range(i + 1, len(ordered)):
                    files_j = files_by_aid.get(ordered[j].assignment_id, set())
                    overlap_files |= files_i & files_j

            warnings.append(SiblingOverlapWarning(
                repo_name=members[0].repo_name,
                target_branch=target_branch,
                issue_numbers=tuple(m.issue_number for m in ordered),
                overlapping_files=tuple(sorted(overlap_files)),
                oldest_age_hours=round(age_hours, 1),
            ))

    warnings.sort(key=lambda w: (-w.oldest_age_hours, w.repo_name, w.target_branch))
    return warnings


# ── Staging section (#778) ────────────────────────────────────────────────────

# Status values for StagingItem.status — never stored in the DB.
STAGING_READY = "ready"      # all gates pass; will be enqueued on the next tick
STAGING_BLOCKED = "blocked"  # at least one non-review gate is failing


@dataclass
class StagingItem:
    """One entry in the 'approved but not yet queued' staging section.

    Populated by :func:`staging_items` which scans the board for completed
    work assignments that have an approved review (or don't need one) but
    have not yet been admitted to the merge queue.  Exposed on ``/board`` so
    thin clients (TUI, phone webapp) can answer "did my PR make it in?" without
    a manual ``coord merge --dry-run``.
    """

    assignment_id: str
    repo_name: str
    issue_number: int
    issue_title: str
    branch: str
    status: str          # STAGING_READY | STAGING_BLOCKED
    reason: str | None   # None when ready; human-readable gate failure when blocked


# #2085: `_work_has_approved_review_a` (a hand-rolled "any approve on any
# connected work id" scan, no SHA/patch-id binding at all) used to live here
# as a second implementation of the same question `has_approved_review`
# answers — "two surfaces that happen to agree" is exactly the shape #2096
# warns about, and this one agreed by being *more* permissive than the real
# gate rather than less: a staging item could read READY off a review that
# `coord merge` would refuse as stale. `has_approved_review` is already
# duck-typed on `.assignment_id`/`.branch` (both present on a raw
# `Assignment`) via `_chain_work_ids`, and getattr-defaults every SHA/
# patch-id field a raw Assignment doesn't carry to `None` — which, since
# #2085, means it fails CLOSED on any review whose approval can't be
# confirmed fresh rather than skipping the check. staging_items now calls
# `has_approved_review(a, board, gh_ops)` directly below.


def _staging_smoke_entry(a, config):
    """Build the minimal duck-typed entry :func:`evaluate_smoke_verdict` needs
    for a staging candidate (#1640).

    A staging item is by definition *not* in the merge queue, so there is no
    :class:`QueuedMerge` row carrying ``branch_head_sha`` /
    ``target_branch_head_sha`` / ``branch_patch_id``. The shim supplies the
    identity fields (``assignment_id``/``branch``/``repo_github``/
    ``target_branch``) the evaluator needs to *look those up* through a
    ``gh_ops``, and leaves the SHA fields ``None`` so a ``gh_ops=None`` call
    stays I/O-free.

    ``target_branch`` is the repo's ``default_branch``, not the #934
    milestone feature branch: resolving the latter costs a ``gh`` call, and
    this function is on the ``/board`` read path. A staging candidate on a
    milestone repo therefore compares against the wrong base and simply fails
    the freshness check open — the same "anchor missing → skip that half"
    convention the evaluator already uses, never a false *block*.
    """
    repo_cfg = None
    if config is not None:
        try:
            repo_cfg = config.repo(getattr(a, "repo_name", None) or "")
        except Exception:  # noqa: BLE001 — unknown repo: no live lookup possible
            repo_cfg = None
    return QueuedMerge(
        assignment_id=getattr(a, "assignment_id", None) or "",
        repo_name=getattr(a, "repo_name", None) or "",
        repo_github=getattr(repo_cfg, "github", None) or "",
        branch=getattr(a, "branch", None) or "",
        target_branch=getattr(repo_cfg, "default_branch", None) or "",
        issue_number=int(getattr(a, "issue_number", 0) or 0),
        issue_title=getattr(a, "issue_title", None) or "",
    )


def staging_items(board, config, gh_ops: "GhOps | None" = None) -> list[StagingItem]:
    """Return work assignments that are done+approved but not yet in the queue.

    Scans ``board.completed`` for ``status=done`` assignments whose ``type``
    is in :data:`coord.models.WORK_LIKE_TYPES` (``"work"`` or
    ``"mock-author"``, #930) and returns one :class:`StagingItem` per
    candidate that has an approved review
    (or doesn't need one) but hasn't yet been admitted to the merge queue.
    Each item is classified:

    * ``STAGING_READY``   — all gates pass; will be enqueued on the next daemon
      tick (typically within 30 s of approval).
    * ``STAGING_BLOCKED`` — the smoke / test gate is failing; the item cannot
      enter the queue until the operator records a verdict (``coord test
      --passed`` / ``--skipped``).

    Items that have NOT received an approved review are silently excluded so
    that the staging section only shows work the pipeline has already green-lit.

    The function is intentionally **read-only**: no DB writes.  Pass
    ``board=None`` or ``config=None`` to skip gate evaluation (useful in
    tests that only care about filtering logic).

    #1640: the smoke gate here used to read the raw ``test_state`` column
    with no freshness check at all, so it printed READY (and ``/board``'s
    staging section showed green) for an assignment whose verdict
    :func:`has_smoke_verdict` — the reader ``coord merge`` actually uses —
    rejects as stale under the #1479 binding. It now routes through the same
    :func:`evaluate_smoke_verdict` helper as every other smoke reader, so the
    two can no longer disagree, and reports *which* failure it is.

    *gh_ops* is optional and, when ``None`` (the default), no GitHub call is
    made: the freshness anchors simply aren't available, and the evaluation
    degrades to the terminal-verdict check it has always performed. Callers
    that can supply a client — or the daemon's tick-refreshed
    :class:`coord.gate_snapshot.GateSnapshot`, which serves the same two
    lookups without live I/O — get the full freshness binding.
    """
    existing_queue = load_queue()

    # Fast-lookup: assignment IDs already in the queue (any state).
    queued_aids: set[str] = {x.assignment_id for x in existing_queue}

    # Fast-lookup: branches already in the queue (any state).  A fix worker
    # dispatched after the original work was enqueued will have a different
    # assignment_id but share the same branch — so dedup by branch too.
    queued_branches: set[str] = {x.branch for x in existing_queue if x.branch}

    # Fast-lookup: (repo_name, issue_number) pairs already MERGED so we skip
    # issues whose prior attempt was already shipped. Consults the archive
    # too (#1107 Part 3) since the winning entry may have aged out of the
    # live table by the time a later duplicate work item shows up here.
    already_merged = merged_issue_keys()

    result: list[StagingItem] = []
    completed = list(getattr(board, "completed", []) or [])

    for a in completed:
        if getattr(a, "type", None) not in WORK_LIKE_TYPES:
            continue
        if getattr(a, "status", None) != "done":
            continue

        aid = getattr(a, "assignment_id", None)
        branch = getattr(a, "branch", None)
        if not aid or not branch:
            continue

        repo_name = getattr(a, "repo_name", None) or ""
        issue_number = int(getattr(a, "issue_number", 0) or 0)
        issue_title = getattr(a, "issue_title", None) or ""

        # Skip items already tracked in the queue (by assignment_id or branch).
        # Branch-level dedup catches fix workers that share a branch with an
        # already-queued original work assignment (#778).
        if aid in queued_aids or branch in queued_branches:
            continue

        # Skip if the issue has already been merged via a prior work attempt.
        if (repo_name, issue_number) in already_merged:
            continue

        # Gate: review.  Skip entirely when review is required but NOT yet
        # approved — the item isn't "approved" yet and should not appear in the
        # staging section (it belongs to the pipeline, not the merge staging).
        if config is not None and board is not None:
            if requires_review(a, config) and not has_approved_review(a, board, gh_ops):
                continue

        # Gate: smoke.  When the test gate is enabled and no *fresh* verdict
        # exists, the item appears as BLOCKED rather than being silently
        # excluded.  #1640: shared reader — see the docstring.
        status = STAGING_READY
        reason: str | None = None
        if config is not None and board is not None and requires_smoke(a, config):
            smoke = evaluate_smoke_verdict(
                _staging_smoke_entry(a, config), board, gh_ops
            )
            if not smoke.ok:
                status = STAGING_BLOCKED
                reason = smoke.short_reason

        result.append(StagingItem(
            assignment_id=aid,
            repo_name=repo_name,
            issue_number=issue_number,
            issue_title=issue_title,
            branch=branch,
            status=status,
            reason=reason,
        ))

    return result


# ── Processing ───────────────────────────────────────────────────────────

@dataclass
class MergeEvent:
    entry: QueuedMerge
    kind: str  # "opened" | "sized" | "merged" | "conflict" | "skipped" | "error" | "reopened"
    message: str = ""


def _briefing_body(entry: QueuedMerge) -> str:
    # `Closes #N` makes GitHub auto-close the linked issue when the PR
    # merges — without it the issue stays stranded open and the TUI's
    # lifecycle ledger shows the row as In-flight forever (the brain
    # keeps re-synching it as state=open).  Quadraui #239/#240/#242 hit
    # this in 2026-05; closing the issues was a manual cleanup.
    #
    # #1077: only emit the closing keyword when this entry's issue_number is
    # actually resolved by the PR (`CLOSES_ISSUE_TYPES`). A "mock-author"
    # entry's issue_number is the milestone's tracking issue — closing it on
    # merge is wrong (the epic reads "done" while its sub-issues are still
    # open), so it gets the non-closing `Refs #N` instead.
    keyword = "Closes" if entry.assignment_type in CLOSES_ISSUE_TYPES else "Refs"
    return (
        f"{keyword} #{entry.issue_number}\n\n"
        f"Automated merge from the coordinator for assignment "
        f"{entry.assignment_id} on issue #{entry.issue_number}.\n\n"
        f"Worker branch: `{entry.branch}` → `{entry.target_branch}`."
    )


def _work_assignment_for_entry(entry: QueuedMerge, board) -> Assignment | None:
    """The ``type="work"`` :class:`Assignment` behind *entry*, or ``None``.

    Thin lookup used by :func:`_maybe_clear_expected_red` to read the
    acceptance verdict (``acceptance_state``/``acceptance_sha``) recorded
    by ``coord acceptance record`` for this merge's originating work
    assignment. Scans both ``board.active`` and ``board.completed`` — same
    rationale as :func:`scan_approved_reviews`: by the time ``process()``
    reaches the merge-success path the row may still be on ``active`` for a
    tick before reconcile moves it.
    """
    if board is None:
        return None
    pool = list(getattr(board, "completed", []) or []) + list(getattr(board, "active", []) or [])
    for a in pool:
        if getattr(a, "assignment_id", None) == entry.assignment_id and getattr(a, "type", None) == "work":
            return a
    return None


def _test_author_effective_issue_number(entry: QueuedMerge, board) -> int | None:
    """#2191: the child issue a ``type="test-author"`` merge entry's slice
    is actually FOR — resolved via ``coord.models.effective_issue_number``
    from the originating assignment's ``for_issue_number``, NOT
    ``entry.issue_number``. For a test-author row, ``entry.issue_number``
    (== ``Assignment.issue_number``) is always the milestone's TRACKING
    issue: every JIT slice for one milestone shares a single branch/PR, so
    ``issue_number`` alone can't tell "this is #1039's slice" from "this is
    #1042's slice" apart (see ``Assignment.for_issue_number``'s docstring).
    Mirrors :func:`_work_assignment_for_entry`'s board scan, just keyed on
    ``type == "test-author"``.

    Returns ``None`` when no originating assignment is found on the board
    (best-effort — the PR-open path this feeds never blocks on it) or when
    it's milestone-mode (Gate A) authoring, where ``for_issue_number`` is
    unset and ``effective_issue_number`` falls back to the tracking issue
    itself — deliberately inert downstream, since no manifest ever maps a
    test id to the tracking issue.
    """
    if board is None:
        return None
    pool = list(getattr(board, "completed", []) or []) + list(getattr(board, "active", []) or [])
    for a in pool:
        if (
            getattr(a, "assignment_id", None) == entry.assignment_id
            and getattr(a, "type", None) == "test-author"
        ):
            from coord.models import effective_issue_number  # noqa: PLC0415

            return effective_issue_number(a)
    return None


def _expected_red_ids_for_entry(
    entry: QueuedMerge, gh_ops: GhOps, config=None,
) -> frozenset[str]:
    """#2199 review (blocking finding 2) / #2266 review (non-blocking
    finding): the ``expected_red`` test ids, if any, recorded against
    *entry*'s issue in some ``ms-*/manifest.yml`` on ``entry.target_branch``.

    Two call sites need this:

    * :func:`_issue_has_expected_red_entries` — the scope check that keeps
      :func:`_maybe_clear_expected_red`'s loud "no passing trust-gate
      verdict" diagnostic from firing on every ordinary
      ``CLOSES_ISSUE_TYPES`` merge fleet-wide (no driver configured, no
      oracle-opted-in milestone, an ``oracle:exempt`` issue — none of which
      ever populate ``expected_red`` for their issue, #2191). Those merges
      have nothing here for ``coord acceptance record`` to ever have
      cleared and never will; before this check, the diagnostic branch
      below could not tell them apart from an issue that genuinely IS in
      scope and genuinely IS stuck red, so it printed the same
      actionable-looking (and, for a driverless repo, un-followable)
      advice on every single one.
    * :func:`_maybe_clear_expected_red`'s clear branch, to put the actual
      test ids (not just a pr/branch pair) into the durable audit row —
      matching ``coord acceptance expected-red --clear``'s
      ``details["test_ids"]`` so a merge-queue-triggered clear is just as
      correlatable back to specific stuck tests as a CLI-triggered one.

    Reuses the exact API-only lookup ``coord.acceptance.
    clear_expected_red_via_pr`` already performs on the success path —
    best-effort/read-only like the rest of this sweep: any lookup failure
    (unreachable API, older ``gh_ops`` stub missing the list/get methods)
    reads as "nothing recorded", matching :func:`coord.acceptance.
    find_ms_manifest_for_issue_via_api`'s own fail-soft posture.

    #2896 review: *config*, when given, supplies the acceptance SEARCH
    ROOTS for the lookup (``coord.acceptance.search_roots_for_repo``). It
    is not optional in spirit — without it the sweep only sees the shared
    repo-root ``tests/acceptance/`` tree, so a relocated
    (entrypoint-linked) milestone's entries read as "nothing recorded" and
    :func:`_maybe_clear_expected_red` silently takes its "not in scope for
    the oracle loop at all" branch: no clear, no ``MergeEvent``, no audit
    row — exactly the #2199 regression its own docstring says was fixed.
    It defaults to ``None`` only so an older/partial caller degrades to the
    legacy single-root behaviour instead of raising.
    """
    from coord.acceptance import (  # noqa: PLC0415
        find_ms_manifest_for_issue_via_api,
        search_roots_for_repo,
    )

    try:
        found = find_ms_manifest_for_issue_via_api(
            entry.repo_github, entry.target_branch, entry.issue_number,
            gh_ops=gh_ops,
            search_roots=search_roots_for_repo(config, entry.repo_name),
        )
    except Exception:  # noqa: BLE001 — best-effort, same posture as callers
        return frozenset()
    if found is None:
        return frozenset()
    _path, _text, _blob_sha, data = found
    return frozenset(data.expected_red.get(entry.issue_number, frozenset()))


def _issue_has_expected_red_entries(
    entry: QueuedMerge, gh_ops: GhOps, config=None,
) -> bool:
    """Whether *entry*'s issue has at least one ``expected_red`` entry
    recorded against it. See :func:`_expected_red_ids_for_entry` for why
    this scope check exists — and why *config* matters."""
    return bool(_expected_red_ids_for_entry(entry, gh_ops, config))


def _record_expected_red_audit(
    entry: QueuedMerge,
    event_type: str,
    message: str,
    *,
    test_ids: frozenset[str] | None = None,
) -> None:
    """#2266: durable half of an `expected_red` clear/skip/failure — a
    `MergeEvent` alone only reaches the operator as a `coord merge` output
    line that scrolls past (issue #2266's framing: "nothing durable,
    nothing re-checked"). ``record_audit`` gives the same outcomes
    (cleared, skipped for one of two distinct reasons, pending retry, or
    attempted-and-failed) a queryable row (`coord audit` / `query_audit_log`)
    so a repo with a stuck registry is discoverable without re-reading
    merge output. Best-effort like every other audit call site in this
    module — ``record_audit`` itself never raises.

    *test_ids*, when given, lands in ``details["test_ids"]`` — matching the
    CLI path's (`coord.commands.acceptance._clear_stuck_expected_red`)
    audit payload, per #2266 review non-blocking finding: without it,
    merge-queue-triggered audit rows were harder to correlate back to the
    specific stuck test ids than CLI-triggered ones.
    """
    details: dict[str, object] = {
        "pr_number": entry.pr_number, "target_branch": entry.target_branch,
    }
    if test_ids:
        details["test_ids"] = sorted(test_ids)
    record_audit(
        tier="business",
        category="acceptance",
        event_type=event_type,
        actor="system",
        summary=f"{entry.repo_name}#{entry.issue_number}: {message}",
        repo=entry.repo_name,
        issue=entry.issue_number,
        assignment_id=entry.assignment_id,
        details=details,
    )


def _acceptance_patch_id_matches(
    entry: "QueuedMerge", acceptance_sha: str, gh_ops: "GhOps",
) -> bool:
    """#2298: True when *acceptance_sha* — the commit the trust-gate verdict
    (``coord acceptance record``) was recorded against — introduced the
    *same content* as the commit that just merged, even though the two
    SHAs differ.

    The merge queue's own ``checks_stale``/``smoke_required`` gates force a
    PR sitting behind a moved base to rebase before it can merge at all (a
    re-run CI needs a fresh SHA to attach to); rebasing rewrites
    ``branch_head_sha`` by construction. :func:`_maybe_clear_expected_red`
    treating that SHA change alone as "the content changed" made the two
    gates mutually unsatisfiable on any PR that had to rebase — every such
    merge skipped its ``expected_red`` clear and manually needed one, #2298.

    This mirrors the exact rebase-survives-content fingerprint
    ``has_approved_review`` already uses for the review gate (#1475): a
    content-addressed patch-id (``git patch-id --stable`` over the unified
    diff, via ``gh_ops.get_branch_patch_id``) is insensitive to which
    commit the diff is replayed onto, so a pure rebase (no conflict, no new
    commits) produces the *same* patch-id from a *different* SHA, while a
    genuine content change (a conflict resolved differently, an extra
    commit) does not.

    Compares ``entry.branch_patch_id`` — the diff the commit that just
    merged actually introduced, backfilled on demand via
    :func:`_backfill_branch_patch_id` when `process()` never needed it
    (e.g. neither review nor smoke was required for this entry, the only
    two gates that consult it pre-merge) — against a patch-id computed
    fresh for *acceptance_sha* against ``entry.target_branch``.
    *acceptance_sha* need not be any branch's current tip: per
    :func:`coord.github_ops.get_compare_diff`'s docstring, GitHub's
    three-dot compare API accepts a raw commit SHA for *head* exactly like
    a branch name, the same trick :func:`has_approved_review`'s #1476
    scoped-re-review path already relies on for an old, superseded review
    SHA.

    Returns ``False`` — fail closed — whenever either side is unavailable
    (no patch-id support, a `gh_ops` failure, or *acceptance_sha* no longer
    resolving, e.g. long since garbage-collected): "cannot confirm
    identical content" must never read as "confirmed identical".
    """
    branch_patch_id = getattr(entry, "branch_patch_id", None)
    if branch_patch_id is None:
        branch_patch_id = _backfill_branch_patch_id(entry, gh_ops)
    if branch_patch_id is None:
        return False
    try:
        acceptance_patch_id = gh_ops.get_branch_patch_id(
            entry.repo_github, entry.target_branch, acceptance_sha,
        )
    except Exception:  # noqa: BLE001 — fail-safe, matches _backfill_branch_patch_id
        acceptance_patch_id = None
    return acceptance_patch_id is not None and acceptance_patch_id == branch_patch_id


def _maybe_clear_expected_red(
    entry: QueuedMerge, board, gh_ops: GhOps, config=None,
) -> MergeEvent | None:
    """#2164: right after *entry*'s PR has actually merged into
    ``entry.target_branch``, clear any of its issue's ``expected_red``
    entries that the trust gate (``coord acceptance record``) already
    observed green — this is the ordering event the first cut of #2164 got
    wrong (clearing at record-time, before the merge that's the whole
    point had actually happened; see ``coord.acceptance.
    clear_expected_red_via_pr``'s docstring). Returns ``None`` when there
    is nothing to do (not a `work` entry, or this issue was never in scope
    for the oracle trust gate at all) — every other skip path names itself
    with its own ``MergeEvent`` (see below), so ``coord merge`` output
    never implies a clear that didn't happen. Never raises — every failure
    mode inside ``clear_expected_red_via_pr`` degrades to a message, and
    this wrapper's own lookups are read-only/best-effort.

    #2199 review: before the trust gate had a call site at all
    (``coord acceptance record`` was never invoked by anything —
    ``getattr(work, "acceptance_state", None) != "passed"`` was the
    UNIVERSAL case), this branch returned bare ``None`` — silently. An
    ``expected_red`` entry could then never clear, indistinguishable from
    #1965's genuine vacuous-assertion alarm (quadraui#542). Now it names
    which condition failed instead — but ONLY for an issue actually in
    scope for the oracle loop (:func:`_issue_has_expected_red_entries`,
    #2199 review finding 2): the first cut of this fix regressed to
    printing that same loud, actionable-looking text on every ordinary
    merge fleet-wide, contradicting the acceptance criterion that exempt
    issues and driverless repos stay unaffected.

    #2298: the SHA-exact guard below is no longer the only path to a
    clear. ``checks_stale``/``smoke_required`` refuse a PR whose CI
    predates the current base, so a PR that sits long enough for the base
    to move must rebase to merge at all — which rewrites
    ``entry.branch_head_sha`` even when nothing about the PR's own diff
    changed. Comparing SHAs alone made that guard and those gates mutually
    unsatisfiable on exactly the PRs this whole trust gate exists to
    protect (every rebased oracle-loop merge). See
    :func:`_acceptance_patch_id_matches` for the content-addressed
    fallback that tells a pure rebase apart from a genuine content change.

    #2896 review: *config* is threaded through so every ``expected_red``
    lookup below searches the repo's real acceptance roots, not just the
    shared repo-root tree. A relocated (entrypoint-linked) milestone's
    manifest lives beside its driver's entrypoint (``tui/tests/
    acceptance/ms-NN/``); without *config* the lookup finds nothing there,
    the ``not ids`` branch below reads that as "never in scope for the
    oracle loop", and the clear silently never happens — the exact #2199
    failure this function's diagnostics exist to make impossible.
    """
    if entry.assignment_type not in CLOSES_ISSUE_TYPES:
        return None
    work = _work_assignment_for_entry(entry, board)
    if work is None:
        return MergeEvent(
            entry, "expected_red_clear_skipped_no_work",
            "no `work` assignment found for this merge entry — cannot read "
            "an acceptance verdict; skipping expected_red clear",
        )
    acceptance_state = getattr(work, "acceptance_state", None)
    if acceptance_state != "passed":
        ids = _expected_red_ids_for_entry(entry, gh_ops, config)
        if not ids:
            # Not in scope for the oracle loop at all — the pre-#2199
            # silent no-op is still correct here, not a regression (see
            # the docstring above and #2199 review finding 2).
            return None
        msg = (
            f"no passing trust-gate verdict recorded on {work.assignment_id} "
            f"(acceptance_state={acceptance_state!r}) — the external `coord "
            "acceptance record` re-run either never happened or did not "
            "pass; skipping expected_red clear. Run `coord acceptance "
            f"record --repo {entry.repo_name} --issue {entry.issue_number} "
            "--sha <merged sha>` by hand, or re-drive the issue, to clear "
            "any listed entries."
        )
        # #2266: distinct from the SHA-mismatch guard below — "acceptance
        # never recorded" and "recorded SHA is stale" are different
        # problems with different fixes, so they get distinct event types
        # rather than reaching the operator as the same silence.
        _record_expected_red_audit(
            entry, "expected_red_clear_skipped_no_acceptance", msg, test_ids=ids,
        )
        return MergeEvent(entry, "expected_red_clear_skipped_no_acceptance", msg)
    acceptance_sha = getattr(work, "acceptance_sha", None)
    sha_matches = acceptance_sha is not None and acceptance_sha == entry.branch_head_sha
    # #2298: a bare SHA mismatch no longer voids the verdict outright.
    # `checks_stale`/`smoke_required` force a rebase before a PR sitting
    # behind a moved base can merge at all — which rewrites
    # `branch_head_sha` on every such PR by construction, making this
    # guard and those gates mutually unsatisfiable pre-#2298 (see the
    # issue). Mirror `has_approved_review`'s #1475 patch-id fallback: a
    # SHA mismatch whose content-addressed patch-id still matches is a
    # pure rebase, not a stale observation.
    patch_id_verified = (
        acceptance_sha is not None
        and not sha_matches
        and _acceptance_patch_id_matches(entry, acceptance_sha, gh_ops)
    )
    if not sha_matches and not patch_id_verified:
        # The recorded trust-gate verdict isn't for the exact commit that
        # just merged, and its diff doesn't match the merged commit's diff
        # either (acceptance never ran, a genuine content change landed
        # after the last `record`, or the patch-id comparison itself
        # couldn't be confirmed) — skip rather than clear on a stale
        # observation. #2298 (also worth fixing here): the issue this
        # entry closed is already closed by the time this runs, so
        # `coord acceptance record --sha` (which targets an *open* issue's
        # work assignment) has nothing left to attach to; point at the
        # remedy that actually works from here instead.
        msg = (
            "acceptance_sha does not match the merged commit, and its "
            "content-addressed patch-id does not confirm a pure rebase "
            "either (#2298) — skipping expected_red clear. If this was "
            "genuinely just a rebase onto a moved base, clear by hand: "
            f"`coord acceptance expected-red {entry.repo_name} --clear "
            f"--issue {entry.issue_number}`. If the content actually "
            "changed, a fresh `coord acceptance record` against the "
            "merged SHA (while the issue was still open) was the correct "
            "fix and did not happen."
        )
        _record_expected_red_audit(
            entry, "expected_red_clear_skipped_sha_mismatch", msg,
            test_ids=_expected_red_ids_for_entry(entry, gh_ops, config),
        )
        return MergeEvent(entry, "expected_red_clear_skipped", msg)

    from coord.acceptance import (  # noqa: PLC0415
        classify_expected_red_clear_result,
        clear_expected_red_via_pr,
        search_roots_for_repo,
    )

    # Captured *before* the clear attempt: a successful clear edits the
    # manifest, so looking this up afterwards would just find the ids the
    # clear itself just removed (#2266 review non-blocking finding).
    ids = _expected_red_ids_for_entry(entry, gh_ops, config)
    if patch_id_verified:
        # #2298: name the arm taken — a durable row independent of whether
        # there ends up being anything to clear (the "no_op" branch below
        # never gets one of those), so this rebase-not-content-change call
        # is queryable on its own, the same way every other skip/clear
        # branch here names itself rather than staying silent (#2199).
        _record_expected_red_audit(
            entry, "expected_red_sha_mismatch_patch_id_verified",
            "acceptance_sha does not match the merged commit, but its "
            "content-addressed patch-id does — a pure rebase, not a "
            "content change (#2298); proceeding with expected_red clear",
            test_ids=ids,
        )
    msg = clear_expected_red_via_pr(
        entry.repo_github, entry.repo_name, entry.target_branch, entry.issue_number,
        gh_ops=gh_ops,
        search_roots=search_roots_for_repo(config, entry.repo_name),
    )
    # #2266 review (blocking finding 1): a binary "did the message start
    # with 'cleared expected_red'" conflated a genuine failure with "there
    # was nothing to clear in the first place" — the *common* case for an
    # ordinary oracle-loop merge whose issue was never part of a
    # deliberately-red slice. `classify_expected_red_clear_result` (shared
    # with the CLI `--clear` path, review blocking finding 2) tells those
    # apart; the "no_op" case is reported back via `MergeEvent` for `coord
    # merge` output but never gets a durable audit row — recording one for
    # every ordinary passing merge would drown the genuinely actionable
    # `expected_red_clear_failed` rows this audit trail exists to surface.
    status = classify_expected_red_clear_result(msg)
    if status == "no_op":
        return MergeEvent(entry, "expected_red_clear_noop", msg)
    event_type = {
        "cleared": "expected_red_clear",
        "pending_retry": "expected_red_clear_pending",
        "failed": "expected_red_clear_failed",
    }[status]
    if patch_id_verified:
        # #2298: keep the arm visible on the terminal `coord merge` line
        # too, not just in the audit row above.
        msg = (
            f"{msg} (acceptance_sha rebased since verdict — content "
            "verified identical via patch-id, #2298)"
        )
    # #2266 review (non-blocking finding): `clear_expected_red_via_pr`
    # never raises — every genuine failure degrades to a "warning: ..."
    # string. Make that durable too, not just a merge-output line: a
    # repeated `expected_red_clear_failed` for the same issue is exactly
    # the signal `coord acceptance expected-red --clear` (the re-fire
    # path) exists for.
    _record_expected_red_audit(entry, event_type, msg, test_ids=ids)
    return MergeEvent(entry, event_type, msg)


def _maybe_push_design_round(
    entry: QueuedMerge, config, gh_ops: GhOps
) -> "MergeEvent | None":
    """PDR-3 (#2508): right after a `type="mock-author"` (Gate A) PR has
    actually merged into ``entry.target_branch``, push its rendered mock
    bundle + contract to the portal as a design round — the epic-#2506
    bridge's third leg, wiring PDR-1's link (``coord portal link``,
    #2507) and PDR-2's upload route (coord-portal#120) into the one signal
    ``coord/merge_queue.py`` already fires for any assignment type: a real
    merge.

    Returns ``None`` for every "nothing to do" reason — not a `mock-author`
    entry, no ``portal:`` block configured, the tracking issue isn't under
    a milestone, or (the common case pre-#2508) the milestone has no portal
    link on file. That last one is the fail-open posture
    ``coord.portal_bridge``'s module docstring already states for the rest
    of this bridge: "a portal outage must never block a merge or a
    dispatch" applies just as much to "there's simply no portal submission
    for this milestone yet" — until an operator runs ``coord portal link``,
    this is silently, correctly, a no-op. Every other failure (can't fetch
    the tracking issue, can't collect the bundle, upload/enqueue rejected)
    degrades to a ``design_round_push_failed`` event rather than raising —
    called from ``process()`` inside its own try/except, same discipline as
    :func:`_maybe_clear_expected_red` right above it, but belt-and-braces
    here too since this reaches out to two different externals (GitHub's
    Contents API and the portal itself).

    #3068: a resolvable driver whose mock glob isn't browser-viewable (or no
    resolvable driver at all) is a VISIBLE ``design_round_push_skipped``
    event, not a silent no-op and never a success — a design round that
    reaches a customer with no viewable mock is exactly the failure this
    issue named.
    """
    if entry.assignment_type != "mock-author":
        return None
    portal_cfg = getattr(config, "portal", None)
    if portal_cfg is None or not portal_cfg.enabled:
        return None

    # #1467-style optional probe: `get_issue` isn't part of the GhOps
    # Protocol proper (most stubs in tests never need it), so a stub that
    # doesn't implement it degrades to "nothing to do" rather than an
    # AttributeError — mirrors `branch_has_merge_commit`'s optional-probe
    # pattern used for the rebase→squash fallback above.
    get_issue = getattr(gh_ops, "get_issue", None)
    if get_issue is None:
        return None
    try:
        issue_data = get_issue(entry.repo_github, entry.issue_number)
    except Exception as e:  # noqa: BLE001 — best-effort, see docstring
        return MergeEvent(
            entry, "design_round_push_failed",
            f"could not fetch tracking issue #{entry.issue_number}: {e}",
        )
    milestone = (issue_data or {}).get("milestone") or {}
    milestone_number = milestone.get("number")
    if milestone_number is None:
        # A "mock-author" entry not scoped to a milestone at all (shouldn't
        # happen via `coord acceptance mock`, which always resolves one —
        # but this hook must survive a hand-dispatched entry that skips it).
        return None

    from coord import portal_store  # noqa: PLC0415

    link = portal_store.get_milestone_link(
        repo_name=entry.repo_name, milestone_number=milestone_number
    )
    if link is None:
        # PDR-1: no `coord portal link` recorded for this milestone yet —
        # the whole point of the fail-open posture (see docstring above).
        return None

    from coord.mock_author import (  # noqa: PLC0415
        collect_mock_bundle_files,
        resolve_viewable_mock_glob,
    )
    from coord.portal_bridge import PortalBridgeError, client_from_config  # noqa: PLC0415
    from coord.portal_sync import PortalSyncError, push_design_round_bundle  # noqa: PLC0415

    client = client_from_config(portal_cfg)
    if client is None:
        return None

    # #3068: consult the repo's OWN acceptance-driver mock glob rather than
    # assuming `*.html`. `resolve_viewable_mock_glob` is the single shared
    # answer to "is this repo's mock browser-viewable, and which glob
    # collects it" — `coord portal publish-mocks` asks the same helper, so
    # the on-demand and merge-triggered paths cannot disagree. A repo whose
    # mocks aren't viewable surfaces as a VISIBLE skip, never as a success:
    # a design round with no viewable mock is not a design round a customer
    # should ever see.
    mock_glob, skip_reason = resolve_viewable_mock_glob(
        getattr(config, "acceptance", None), entry.repo_name
    )
    if mock_glob is None:
        return MergeEvent(
            entry, "design_round_push_skipped",
            f"{skip_reason} — a design round needs a viewable mock, so "
            f"nothing was pushed for ms-{milestone_number}",
        )

    try:
        files = collect_mock_bundle_files(
            entry.repo_github, milestone_number, entry.target_branch, mock_glob
        )
    except Exception as e:  # noqa: BLE001 — best-effort, see docstring
        return MergeEvent(
            entry, "design_round_push_failed",
            f"could not collect mock bundle: {e}",
        )
    if not files:
        return MergeEvent(
            entry, "design_round_push_skipped",
            f"no rendered mock bundle found on {entry.target_branch} for "
            f"ms-{milestone_number} — nothing to push",
        )

    try:
        bundle_key, row = push_design_round_bundle(
            client,
            link.submission_id,
            files,
            milestone_title=milestone.get("title") or f"ms-{milestone_number}",
            tracking_issue_title=issue_data.get("title") or "",
            tracking_issue_body=issue_data.get("body") or "",
            # #2903: the same Config this hook already holds, so the draft
            # gate's policy read does not re-resolve coordinator.yml from
            # disk and cannot disagree with the config this process was
            # started with (e.g. a `--config` override).
            config=config,
        )
    except PortalBridgeError as e:
        return MergeEvent(entry, "design_round_push_failed", f"bundle upload failed: {e}")
    except PortalSyncError as e:
        return MergeEvent(entry, "design_round_push_failed", f"enqueue failed: {e}")

    if row.state == portal_store.STATE_DRAFT:
        # #2903: it is NOT queued — it is waiting for an operator, and an
        # event that says "queued" would have somebody waiting for an email
        # that is sitting behind a gate only they can open.
        return MergeEvent(
            entry, "design_round_drafted",
            f"design round for portal submission {link.submission_id} is "
            f"awaiting operator approval (seq={row.seq}, "
            f"bundle_key={bundle_key}) — `coord portal drafts`",
        )
    return MergeEvent(
        entry, "design_round_queued",
        f"queued design round for portal submission {link.submission_id} "
        f"(seq={row.seq}, bundle_key={bundle_key})",
    )


def _maybe_push_status(
    entry: QueuedMerge, config, gh_ops: GhOps, board,
) -> "MergeEvent | None":
    """#2588: right after a `type="work"` PR closes its issue, fold every
    issue under that issue's milestone into one customer status and push it
    if it changed.

    The pattern this issue names explicitly: `_maybe_push_design_round`
    right above (PDR-3, #2508) is the merge-queue's one existing automatic
    portal caller, and #2588 asks for the same shape applied to status. Same
    optional-probe use of `gh_ops.get_issue` (not part of the `GhOps`
    Protocol proper — most stubs never need it), same fail-open posture: no
    portal config, no milestone on the merged issue, or no `coord portal
    link` on file for it are all silently correct no-ops — an operator
    hasn't linked this milestone yet, which is the common case and stays
    common for a while (:func:`coord.portal_sync.fold_status_for_milestone`'s
    own docstring). Only a genuine read/enqueue failure becomes a
    `status_push_failed` event, and even that never undoes the merge that
    already happened.

    This is the immediate half of the fold — responsive the moment the last
    issue in a submission closes. The daemon's periodic portal-sync tick
    (`coord.portal_sync.sync_submission_statuses`, run from
    `coord.serve_app._portal_sync_tick`) is the self-healing half: it also
    catches "work started" (no merge involved to hook here) and anything
    this hook missed (the daemon was down, the portal was unreachable).

    #3096 — **the two halves must fold the same link universe.** This hook
    used to resolve only the merged issue's MILESTONE, so a milestone-less
    issue with a `coord portal link --issue` on it (#2665) was visible to the
    tick and invisible here; the two callers saw different sets of links and
    could reach different answers for the same submission. It now falls back
    to the issue-scoped fold when the merged issue carries no milestone,
    which is the same either/or `sync_submission_statuses` branches on. The
    "which link wins when several name one submission" half of that
    reconciliation lives one level down, in
    `coord.portal_sync.authoritative_link`, so both callers inherit it rather
    than each implementing it.
    """
    if entry.assignment_type not in CLOSES_ISSUE_TYPES:
        return None
    portal_cfg = getattr(config, "portal", None)
    if portal_cfg is None or not portal_cfg.enabled:
        return None

    get_issue = getattr(gh_ops, "get_issue", None)
    if get_issue is None:
        return None
    try:
        issue_data = get_issue(entry.repo_github, entry.issue_number)
    except Exception as e:  # noqa: BLE001 — best-effort, see docstring
        return MergeEvent(
            entry, "status_push_failed",
            f"could not fetch issue #{entry.issue_number}: {e}",
        )
    milestone = (issue_data or {}).get("milestone") or {}
    milestone_number = milestone.get("number")

    from coord.portal_sync import (  # noqa: PLC0415
        fold_status_for_issue,
        fold_status_for_milestone,
    )

    if milestone_number is not None:
        result = fold_status_for_milestone(
            config, entry.repo_name, milestone_number, board=board,
        )
    else:
        result = fold_status_for_issue(
            config, entry.repo_name, entry.issue_number, board=board,
        )
    if result.row is not None:
        return MergeEvent(
            entry, "status_queued",
            f"queued status {result.status!r} for portal submission "
            f"{result.submission_id} (seq={result.row.seq})",
        )
    if result.failed:
        return MergeEvent(entry, "status_push_failed", result.reason)
    return None


def process(
    items: list[QueuedMerge],
    gh_ops: GhOps,
    *,
    method: str = "rebase",
    dry_run: bool = False,
    presorted: bool = False,
    ci_store: CiStore | None = None,
    force_merge: bool = False,
    config=None,
    board=None,
    skip_review: bool = False,
    skip_smoke: bool = False,
    skip_uat: bool = False,
) -> list[MergeEvent]:
    """Open PRs, size them, then merge each pending item.

    Items are grouped by (repo_github, target_branch); a **merge conflict**
    parks the conflicting entry (``CONFLICT`` state; the caller in
    ``cli.py`` promotes it to ``HUMAN_REQUIRED``) and **continues** with
    the remaining siblings in that group — each entry's ``gh pr merge`` is
    independent, so a failed merge does not dirty the target branch for
    siblings (#735).  Within a group, items are merged in input order —
    call `sequence(group)` first if you want size-based ordering.
    Set `presorted=True` to make that explicit at call sites.

    When ``ci_store`` is provided and available, each PR is checked against
    its CI status before merge.  A failed check produces a ``checks_failed``
    event; a still-running check produces ``checks_pending``; a PR whose CI
    was expected to run but reported zero checks at all — a distinct case
    from either, see #1904 — produces ``checks_absent``.  In all three cases
    the entry is **skipped** (``continue``) rather than halting the group, so
    a ready sibling can still merge.  ``force_merge=True`` skips this gate.

    #253/#821: When *config* says review is required (``reviews.enabled`` and
    ``"review"`` in ``pipeline.default_gates``) the gate **fails closed**: if
    *board* is ``None`` the approval cannot be confirmed so the entry is
    blocked (``review_required`` event, skip — never merge).  When *config*
    is ``None`` the gate is not applicable (no review policy → no block).
    ``skip_review=True`` bypasses the gate for explicit local-only overrides.
    The daemon ``/merge`` endpoint always passes ``skip_review=False`` and
    ignores any ``skip_review`` flag from the client (#821).

    #465/#821: Same fail-closed semantics for the smoke gate: when *config*
    says ``"test"`` is in ``pipeline.default_gates`` but *board* is ``None``,
    the verdict cannot be confirmed → block (``smoke_required`` event).
    ``skip_smoke=True`` bypasses the gate.

    Dry-run applies the review and smoke gates, and — #1624 — resolves each
    entry's real PR via ``find_pr_for_branch`` (the same lookup ``create_pr``
    does internally) and applies the CI gate against it too, so output
    reflects what a real run would do. CI genuinely cannot be checked for an
    entry with no PR yet (nothing exists to query); that case is reported as
    an explicit ``gate: unknown (no PR yet)`` note rather than silently
    treated as passing.

    #1318: before each merge, both the PR body (#1196) and every commit
    message on the branch are scanned for a GitHub closing keyword
    (``Closes``/``Fixes``/``Resolves #N``) targeting an epic-labelled issue
    — free-text prose in a commit message (even a quote explaining the bug)
    is enough for GitHub's own scanner to auto-close it once the commit
    lands on the base branch, and no PR-body edit can undo that. A body hit
    is downgraded to ``Refs #N`` in place, same as #1196. A commit-message
    hit can't be rewritten here (no local git checkout in this ``gh``-only
    wire layer) so it **blocks** the merge (``epic_closing_keyword_in_commit``
    event) unless ``force_merge=True``, in which case the merge proceeds but
    an ``epic_closing_keyword_in_commit_forced`` warning event is still
    emitted — the override is never silent.

    Mutates `items` in place; the caller saves the queue after.
    """
    events: list[MergeEvent] = []
    ci: CiStore = ci_store if ci_store is not None else NoOpCi()

    groups: dict[tuple[str, str], list[QueuedMerge]] = {}
    for entry in items:
        if entry.state != PENDING:
            continue
        groups.setdefault((entry.repo_github, entry.target_branch), []).append(entry)

    _unset = object()

    for group in groups.values():
        # #1479-review: every entry in a group shares the same target_branch
        # (that's the grouping key), so target_branch_head_sha is the same
        # value for all of them — fetch it once per group instead of once
        # per entry to avoid N redundant `gh api` calls for an N-entry group.
        _group_target_branch_head_sha: str | None | object = _unset

        if dry_run:
            # #1624: resolve each entry's real PR the same way the non-dry
            # path does (`create_pr` internally calls `find_pr_for_branch`
            # before ever calling `gh pr create`) instead of unconditionally
            # announcing "would open PR". A branch can already have an open
            # PR — from an earlier real attempt that opened one and then
            # stalled on a gate, or created out-of-band — and the CI gate
            # below needs a real PR number to evaluate against; without this,
            # the gate was silently skipped and the entry reported mergeable
            # even with failing checks (#1624). `find_pr_for_branch` is
            # optional on GhOps (older test stubs predate #1624): a missing
            # probe or a lookup failure leaves the PR unresolved, same
            # fail-closed contract as `branch_has_merge_commit` (#1467).
            _find_pr = getattr(gh_ops, "find_pr_for_branch", None)
            for entry in group:
                if entry.pr_number is not None:
                    events.append(MergeEvent(
                        entry, "opened",
                        f"PR #{entry.pr_number} (existed) for {entry.branch}",
                    ))
                    continue
                existing = None
                if _find_pr is not None:
                    try:
                        existing = _find_pr(entry.repo_github, entry.branch)
                    except Exception:  # noqa: BLE001
                        existing = None
                if existing is not None:
                    entry.pr_number = existing.get("number")
                    entry.pr_url = existing.get("url")
                    events.append(MergeEvent(
                        entry, "opened",
                        f"PR #{entry.pr_number} (existed) for {entry.branch}",
                    ))
                    continue
                # #2143: mirror the real path's already-merged check so a
                # dry-run preview never claims "would open PR" for a branch
                # another driver already merged — `find_pr_for_branch` above
                # only ever looks at OPEN PRs, so a merged-and-closed PR is
                # otherwise indistinguishable here from "never opened".
                _pr_is_merged = getattr(gh_ops, "pr_is_merged", None)
                already_merged = False
                if _pr_is_merged is not None:
                    try:
                        already_merged = _pr_is_merged(
                            entry.repo_github, entry.branch
                        )
                    except Exception:  # noqa: BLE001 — fail open, same
                        # contract as the real path's check.
                        already_merged = False
                if already_merged:
                    events.append(MergeEvent(
                        entry, "already_merged",
                        f"(dry run) {entry.branch} was already merged by "
                        "another merge driver — would skip PR creation "
                        "(#2143)",
                    ))
                else:
                    events.append(MergeEvent(
                        entry, "opened",
                        f"(dry run) would open PR for {entry.branch}",
                    ))
            ordered = group if presorted else sequence(group)
            for entry in ordered:
                # #821: populate branch_head_sha for the commit-bound approval
                # staleness check in has_approved_review.  Only when the board
                # is live (board=None blocks unconditionally; no SHA needed).
                if board is not None and entry.branch_head_sha is None:
                    entry.branch_head_sha = gh_ops.get_branch_sha(
                        entry.repo_github, entry.branch
                    )
                # #1475/#1479: populate branch_patch_id alongside
                # branch_head_sha so has_approved_review / has_smoke_verdict
                # can carry a verdict forward across a content-identical
                # rebase instead of re-blocking on SHA alone. Only fetch it
                # when review or smoke is actually required for this entry —
                # neither gate consults branch_patch_id otherwise, so
                # skipping here saves a `gh api compare` round trip per entry
                # per process() tick for the common gate-disabled case.
                if (
                    board is not None
                    and entry.branch_patch_id is None
                    and config is not None
                    and (
                        (not skip_review and requires_review(entry, config))
                        or (not skip_smoke and requires_smoke(entry, config))
                    )
                ):
                    entry.branch_patch_id = gh_ops.get_branch_patch_id(
                        entry.repo_github, entry.target_branch, entry.branch
                    )
                # #1479: populate target_branch_head_sha so has_smoke_verdict
                # can detect a merge base that moved since the test verdict
                # was recorded — a condition branch_patch_id can't see, since
                # a rebase replays the identical diff onto a new base without
                # changing it. Only fetched when smoke is actually required,
                # same cost-avoidance as branch_patch_id above.
                if (
                    board is not None
                    and entry.target_branch_head_sha is None
                    and not skip_smoke
                    and config is not None
                    and requires_smoke(entry, config)
                ):
                    if _group_target_branch_head_sha is _unset:
                        _group_target_branch_head_sha = gh_ops.get_branch_sha(
                            entry.repo_github, entry.target_branch
                        )
                    entry.target_branch_head_sha = _group_target_branch_head_sha
                # #292 (Defect 4): apply the review gate in dry-run so output
                # reflects real behaviour.  CI cannot be checked in dry-run
                # (no PR exists yet), so review and smoke gates are evaluated.
                # #821: fail closed — if review is required but board is None
                # (approval cannot be confirmed) block the entry.
                if (
                    not skip_review
                    and config is not None
                    and requires_review(entry, config)
                ):
                    # #2704: a single scan, reused for both the boolean check
                    # and the `unknown_head` reason below — two separate
                    # calls each walk the full board a second time per
                    # blocked entry per tick, adding gh load precisely during
                    # the rate-limited/unreachable condition this reason
                    # exists to report.
                    _review_scan = (
                        None if board is None else scan_approved_reviews(entry, board, gh_ops)
                    )
                    if board is None or not _review_scan.approved:
                        # #2704: don't report a confirmed refusal ("not
                        # approved") for a branch head this scan couldn't even
                        # read — see `ApprovalScan.unknown_head`.
                        _why = (
                            "board unavailable to confirm review approval"
                            if board is None
                            else (
                                UNKNOWN_BRANCH_HEAD_REASON
                                if _review_scan.unknown_head
                                else "review required but not approved"
                            )
                        )
                        events.append(MergeEvent(
                            entry, "review_required",
                            f"(dry run) would be blocked: {_why} for {entry.branch}",
                        ))
                        continue
                # #465/#821: smoke gate in dry-run — same fail-closed logic.
                # #1640: when a verdict exists but failed the #1479 freshness
                # binding, say so (and against which SHA) rather than
                # reporting it as never recorded.
                _smoke = None  # read below for the "merged" preview note (#1847)
                if (
                    not skip_smoke
                    and config is not None
                    and requires_smoke(entry, config)
                ):
                    _smoke = (
                        None
                        if board is None
                        else evaluate_smoke_verdict(entry, board, gh_ops)
                    )
                    if _smoke is None or not _smoke.ok:
                        # #2231: mirror the live path (line ~4689) — before
                        # previewing a block on a stale verdict, check whether
                        # the branch is actually conflicted. A dry run that
                        # skips this reports the misleading "test verdict
                        # stale" headline for an entry a real run (or
                        # `--plan`) would already report/dispatch as
                        # `conflict`, which is precisely the split-brain this
                        # issue exists to fix.
                        _conflict_msg = stale_smoke_conflict_reason(
                            entry, _smoke, gh_ops,
                        )
                        if _conflict_msg is not None:
                            events.append(MergeEvent(
                                entry, "conflict",
                                f"(dry run) {_conflict_msg}",
                            ))
                            continue
                        _why = (
                            "board unavailable to confirm smoke verdict"
                            if _smoke is None
                            else _smoke.message
                        )
                        events.append(MergeEvent(
                            entry, "smoke_required",
                            f"(dry run) would be blocked: {_why} for {entry.branch}",
                        ))
                        continue
                # #2687: UAT gate preview — same check the live path below
                # runs, ordered between review/smoke and CI/merge.
                if (
                    not skip_uat
                    and config is not None
                    and requires_uat(entry, config)
                ):
                    _uat_ok, _uat_message = (
                        (False, "board unavailable to confirm UAT verdict")
                        if board is None
                        else evaluate_uat_verdict(entry, board, config, gh_ops)
                    )
                    if not _uat_ok:
                        events.append(MergeEvent(
                            entry, "uat_required",
                            f"(dry run) would be blocked: {_uat_message} for {entry.branch}",
                        ))
                        continue
                # CI gate (#240) preview, added by #1624: same check the real
                # path runs, evaluated here so a dry run can't claim
                # "would merge" for a PR whose checks are already failing.
                # Only evaluable when a real PR number is known — either
                # persisted from an earlier attempt or just resolved above
                # via `find_pr_for_branch` — since CI is checked per-PR, not
                # per-branch. A brand-new entry with no PR yet genuinely
                # cannot be checked; say so explicitly in the "merged"
                # preview below rather than silently treating "not
                # evaluated" as "would merge" (#1624). `force_merge` skips
                # the gate here exactly as it does in the real path.
                _ci_note = ""
                if not force_merge and ci.is_available:
                    if entry.pr_number is not None:
                        checks = ci.list_checks_for_pr(entry.repo_github, entry.pr_number)
                        # #1904: same explicit "empty checks" handling the
                        # real path applies below — every gate that follows
                        # is a filter over `checks` and passes vacuously on
                        # `[]`, so a PR whose CI never ran must be caught
                        # here, not silently reported as "would merge".
                        if not checks and _ci_expects_checks(
                            ci, entry.repo_github, entry.pr_number
                        ):
                            # #1877: mirror the real path's fall-through —
                            # an empty check list also means "PR conflicts
                            # with its base, GitHub never built a merge ref
                            # to test" and a real run routes that to the
                            # #241 conflict-fix path rather than blocking
                            # on CI. Preview that accurately instead of
                            # either the misleading "CI never ran" (nothing
                            # here is a CI problem) or letting it fall
                            # through to the "would merge" message below
                            # (the merge attempt itself would fail).
                            conflicted = gh_ops.check_pr_mergeable(
                                entry.repo_github, entry.pr_number
                            ) is False
                            if conflicted:
                                events.append(MergeEvent(
                                    entry, "conflict",
                                    f"(dry run) {entry.branch} conflicts "
                                    f"with {entry.target_branch} and "
                                    "reports no checks — a real run would "
                                    "route to the #241 conflict-fix path "
                                    "rather than block on CI (#1877)",
                                ))
                                continue
                            events.append(MergeEvent(
                                entry, "checks_absent",
                                f"(dry run) would be blocked: {CI_ABSENT_PREFIX} "
                                f"no checks reported for {entry.branch} though "
                                "this repo declares CI",
                            ))
                            continue
                        failed = failed_checks(checks)
                        if failed:
                            # #1892/#2347: preview-only — never mutates,
                            # never reruns; just shows the same
                            # classification a live attempt would compute so
                            # `--dry-run` doesn't undersell what's actually
                            # blocking. Unreadable (bare fetch failure) is
                            # checked first, mirroring the live path's
                            # ordering — see `_ci_unreadable_reason`.
                            unreadable_reason = _ci_unreadable_reason(failed)
                            # #2380: mirror the real path's straight-to-
                            # conflict routing — see the identical check a
                            # few lines above (#1877) and the live path's own
                            # `_pr_reports_conflicting` branch. Preview-only:
                            # never mutates `entry.state`, same as every
                            # other branch in this dry-run block.
                            if unreadable_reason is not None and _pr_reports_conflicting(
                                gh_ops, entry.repo_github, entry.pr_number
                            ):
                                events.append(MergeEvent(
                                    entry, "conflict",
                                    f"(dry run) {entry.branch} conflicts "
                                    f"with {entry.target_branch} and its CI "
                                    "check-list fetch fails for the same "
                                    "reason — a real run would route to the "
                                    "#241 conflict-fix path rather than park "
                                    "on an unreadable CI retry (#2380)",
                                ))
                                continue
                            infra_reason = None if unreadable_reason else _ci_infra_reason(
                                ci, entry.repo_github, entry.pr_number, failed
                            )
                            summary = ", ".join(
                                f"{c.name} ({c.conclusion})" for c in failed
                            )
                            msg = (
                                unreadable_reason
                                or infra_reason
                                or f"checks failed: {summary}"
                            )
                            events.append(MergeEvent(
                                entry, "checks_failed",
                                f"(dry run) would be blocked: {msg}",
                            ))
                            continue
                        pending = in_flight_checks(checks)
                        if pending:
                            summary = ", ".join(c.name for c in pending)
                            events.append(MergeEvent(
                                entry, "checks_pending",
                                f"(dry run) would be blocked: checks still running: {summary}",
                            ))
                            continue
                        # #1851: a green CI result can itself be stale —
                        # GitHub re-runs `pull_request` checks on head
                        # `synchronize`, never on base movement, so a check
                        # that started before the base's newest commit
                        # landed never saw it. Named distinctly from
                        # checks_failed/checks_pending above.
                        if checks and _ci_checks_are_stale(
                            checks, gh_ops, entry.repo_github,
                            entry.target_branch, _smoke,
                        ):
                            # #1826: same renderer the live path uses, so a
                            # preview and the merge it previews can never
                            # describe this condition differently.
                            events.append(MergeEvent(
                                entry, "checks_stale",
                                "(dry run) would be blocked: "
                                + ci_stale_reason(
                                    checks, gh_ops, entry.repo_github,
                                    entry.target_branch,
                                )
                                + f" ({entry.branch})",
                            ))
                            continue
                    else:
                        _ci_note = " [gate: unknown (no PR yet) — CI cannot be evaluated]"
                elif force_merge and ci.is_available:
                    # #1826: preview the waiver too — a `--dry-run
                    # --force-merge` that just says "would merge" hides the
                    # single most consequential fact about the merge it is
                    # previewing. Mirrors the live path's
                    # `checks_stale_forced` event; folded into `_ci_note` so
                    # the preview stays one line per entry.
                    _stale_waiver = ci_stale_waiver_message(
                        entry, ci, gh_ops, _smoke
                    )
                    if _stale_waiver:
                        _ci_note = f" [{_stale_waiver}]"
                # #1467-review: preview the rebase→squash fallback in
                # dry-run too. Only reachable when this entry already has a
                # pr_number — from an earlier (non-dry-run) attempt, or just
                # resolved above via `find_pr_for_branch` (#1624) — since the
                # probe needs one to query. A first-time dry-run preview of a
                # brand-new entry still can't foresee the fallback. Same
                # fail-closed contract as the real merge path: an
                # inconclusive probe leaves the previewed method unchanged.
                _preview_method = method
                if method == "rebase" and entry.pr_number is not None:
                    _probe = getattr(gh_ops, "branch_has_merge_commit", None)
                    if _probe is not None:
                        try:
                            _has_merge_commit = _probe(
                                entry.repo_github, entry.pr_number
                            )
                        except Exception:  # noqa: BLE001
                            _has_merge_commit = None
                        if _has_merge_commit is True:
                            _preview_method = "squash"
                            events.append(MergeEvent(
                                entry, "method_fallback",
                                f"(dry run) PR #{entry.pr_number} ({entry.branch}) "
                                "contains a merge commit and cannot be "
                                "rebase-merged — would fall back to --squash "
                                "(#1467)",
                            ))
                # #1847: name *why* a base move didn't stale the smoke
                # verdict, distinctly for each of the three #1479 escape
                # hatches, so `--dry-run`/the TUI don't just say "fresh" —
                # they say fresh *because the base move was inert*, *because
                # the branch was inert*, or *because the two diffs are
                # disjoint*. Absent whenever the base never moved at all
                # (the common, unremarkable case stays quiet).
                _smoke_note = (
                    f" [test verdict fresh: {_smoke.spared_reason}]"
                    if _smoke is not None and _smoke.ok and _smoke.spared_reason
                    else ""
                )
                events.append(MergeEvent(
                    entry, "merged",
                    f"(dry run) would merge {entry.branch} → {entry.target_branch} "
                    f"via --{_preview_method}"
                    f"{_bypass_note(entry, config)}"
                    f"{_ci_note}"
                    f"{_smoke_note}",
                ))
            continue

        # Open PRs first so every entry has a pr_number when we sort & merge.
        for entry in group:
            if entry.pr_number is None:
                # #2143: re-check right here, immediately before the
                # mutating create_pr call — not against whatever snapshot
                # this entry was resolved from — whether another merge
                # driver already merged this exact branch while this run
                # was doing something else (a `--revalidate` CI-settle
                # wait, a composite suite run, simply queueing behind a
                # concurrent merge). `create_pr` internally only ever looks
                # for an OPEN PR (`find_pr_for_branch`), so a branch that
                # was merged (and its PR closed) in the meantime reads as
                # "no PR" and would otherwise get a second, purposeless PR
                # opened against it — the 2026-08-12 incident this closes.
                _pr_is_merged = getattr(gh_ops, "pr_is_merged", None)
                if _pr_is_merged is not None:
                    try:
                        already_merged = _pr_is_merged(
                            entry.repo_github, entry.branch
                        )
                    except Exception:  # noqa: BLE001 — fail open: never
                        # block a legitimate merge on a transient lookup
                        # failure; worst case is the pre-#2143 status quo.
                        already_merged = False
                    if already_merged:
                        entry.state = MERGED
                        entry.error = None
                        events.append(MergeEvent(
                            entry, "already_merged",
                            f"{entry.branch} was already merged by another "
                            "merge driver while this run was in progress — "
                            "skipping duplicate PR creation (#2143)",
                        ))
                        continue
                try:
                    pr = gh_ops.create_pr(
                        entry.repo_github,
                        base=entry.target_branch,
                        head=entry.branch,
                        title=f"#{entry.issue_number}: {entry.issue_title}",
                        body=_briefing_body(entry),
                    )
                except Exception as e:  # noqa: BLE001 — surface gh failure as event
                    events.append(MergeEvent(entry, "error", f"create_pr failed: {e}"))
                    continue
                entry.pr_number = pr.get("number")
                entry.pr_url = pr.get("url")
                events.append(MergeEvent(
                    entry, "opened",
                    f"PR #{entry.pr_number} ({'existed' if pr.get('existed') else 'created'}) for {entry.branch}",
                ))
                # #2191: at the exact moment a test-author slice's PR opens,
                # check whether its writer (`TEST_AUTHOR_SYSTEM_PROMPT`
                # step 4b) actually recorded `expected_red` — the gate half
                # of #2191, since a prompt instruction is not a guarantee.
                # Advisory only (a MergeEvent, never a `continue`/skip): the
                # slice PR still opens and can still be reviewed/merged, it
                # just carries a visible warning an operator (or a future
                # hard gate) can act on.
                if entry.assignment_type == "test-author":
                    for_issue = _test_author_effective_issue_number(entry, board)
                    if for_issue is not None and for_issue != entry.issue_number:
                        from coord.acceptance import (  # noqa: PLC0415
                            missing_expected_red_warning,
                            search_roots_for_repo,
                        )

                        warning = missing_expected_red_warning(
                            entry.repo_github, entry.branch, for_issue, gh_ops=gh_ops,
                            search_roots=search_roots_for_repo(
                                config, entry.repo_name,
                            ),
                        )
                        if warning:
                            events.append(MergeEvent(
                                entry, "expected_red_missing_warning", warning,
                            ))
            if entry.pr_number and entry.size is None:
                entry.size = gh_ops.get_pr_size(entry.repo_github, entry.pr_number)
                events.append(MergeEvent(entry, "sized", f"size={entry.size}"))

        ordered = group if presorted else sequence(group)
        for entry in ordered:
            if entry.pr_number is None:
                continue
            # #821: populate branch_head_sha for the commit-bound approval
            # staleness check in has_approved_review.  Only when the board
            # is live (board=None blocks unconditionally; no SHA needed).
            if board is not None and entry.branch_head_sha is None:
                entry.branch_head_sha = gh_ops.get_branch_sha(
                    entry.repo_github, entry.branch
                )
            # #1475/#1479: populate branch_patch_id alongside branch_head_sha
            # so has_approved_review / has_smoke_verdict can carry a verdict
            # forward across a content-identical rebase instead of
            # re-blocking on SHA alone. Only fetch it when review or smoke is
            # actually required for this entry — neither gate consults
            # branch_patch_id otherwise, so skipping here saves a `gh api
            # compare` round trip per entry per process() tick for the
            # common gate-disabled case.
            if (
                board is not None
                and entry.branch_patch_id is None
                and config is not None
                and (
                    (not skip_review and requires_review(entry, config))
                    or (not skip_smoke and requires_smoke(entry, config))
                )
            ):
                entry.branch_patch_id = gh_ops.get_branch_patch_id(
                    entry.repo_github, entry.target_branch, entry.branch
                )
            # #1479: populate target_branch_head_sha so has_smoke_verdict can
            # detect a merge base that moved since the test verdict was
            # recorded — a condition branch_patch_id can't see, since a
            # rebase replays the identical diff onto a new base without
            # changing it. Only fetched when smoke is actually required,
            # same cost-avoidance as branch_patch_id above.
            if (
                board is not None
                and entry.target_branch_head_sha is None
                and not skip_smoke
                and config is not None
                and requires_smoke(entry, config)
            ):
                if _group_target_branch_head_sha is _unset:
                    _group_target_branch_head_sha = gh_ops.get_branch_sha(
                        entry.repo_github, entry.target_branch
                    )
                entry.target_branch_head_sha = _group_target_branch_head_sha
            # Review gate (#253/#821): refuse to merge when a review is required
            # by the pipeline policy but no approved review is on the board.
            # --skip-review bypasses for trivial/docs-only merges where the
            # user has consciously decided review isn't needed.
            # #292 (Defect 3): skip this entry and try the next one in the
            # group rather than halting the whole group.  An un-reviewed entry
            # should not prevent a fully-approved sibling from merging.
            # #821: fail closed — when review is required but board is None
            # the approval cannot be confirmed; block rather than silently merge.
            if (
                not skip_review
                and config is not None
                and requires_review(entry, config)
            ):
                # #2704: a single scan, reused for both the boolean check and
                # the `unknown_head` reason below — see the matching dry-run
                # site above for why a second call here is worth avoiding.
                _review_scan = (
                    None if board is None else scan_approved_reviews(entry, board, gh_ops)
                )
                if board is None or not _review_scan.approved:
                    # #2704: don't report a confirmed refusal ("not
                    # approved") for a branch head this scan couldn't even
                    # read — see `ApprovalScan.unknown_head`.
                    msg = (
                        "review required but board unavailable to confirm approval"
                        if board is None
                        else (
                            UNKNOWN_BRANCH_HEAD_REASON
                            if _review_scan.unknown_head
                            else "review required but not approved"
                        )
                    )
                    entry.error = msg
                    events.append(MergeEvent(entry, "review_required", msg))
                    continue  # #292: skip this entry; try the next in the group
            # Smoke gate (#465/#821): refuse to merge when the interactive smoke
            # is required by the pipeline policy but no passing/skipped verdict
            # is recorded on the work assignment.  Same skip-not-halt semantics
            # as the review gate above.
            # #821: fail closed — when smoke is required but board is None
            # the verdict cannot be confirmed; block rather than silently merge.
            # #1640: distinguish "never recorded" from "recorded but stale
            # against the current base" — both used to print the former.
            # #1851: also read below by the CI-staleness check, which reuses
            # `smoke.spared_reason` when the smoke gate evaluated this same
            # base move — `None` when smoke wasn't required for this entry.
            smoke: "SmokeVerdictStatus | None" = None
            if (
                not skip_smoke
                and config is not None
                and requires_smoke(entry, config)
            ):
                smoke = (
                    None if board is None else evaluate_smoke_verdict(entry, board, gh_ops)
                )
                if smoke is None or not smoke.ok:
                    # #2231: before parking this entry behind a gate whose
                    # stated cause invites a re-test, check whether the branch
                    # merges at all. A confirmed conflict is the real blocker
                    # and a re-test can never clear it — emit the `conflict`
                    # event so `_dispatch_conflict_fixes` arms #241's rebase
                    # worker, exactly as a failed merge attempt would have.
                    conflict_msg = stale_smoke_conflict_reason(
                        entry, smoke, gh_ops,
                    )
                    if conflict_msg is not None:
                        entry.state = CONFLICT
                        entry.error = conflict_msg
                        events.append(
                            MergeEvent(entry, "conflict", conflict_msg)
                        )
                        continue
                    msg = (
                        "smoke test required but board unavailable to confirm verdict"
                        if smoke is None
                        else smoke.message
                    )
                    entry.error = msg
                    events.append(MergeEvent(entry, "smoke_required", msg))
                    continue  # skip this entry; try the next in the group
            # UAT gate (#2687): refuse to merge a customer-facing change until
            # an operator has clicked through the PR's deployed preview and
            # recorded a verdict. Ordered between review/smoke and CI/merge,
            # same skip-not-halt semantics as the gates above. Two-part
            # opt-in (see requires_uat) means this is a no-op for every repo
            # that hasn't set `uat_preview` or `uat_live_preview` (#2948) —
            # the default posture everywhere.
            if (
                not skip_uat
                and config is not None
                and requires_uat(entry, config)
            ):
                uat_ok, uat_msg = (
                    (False, "uat verdict required but board unavailable to confirm")
                    if board is None
                    else evaluate_uat_verdict(entry, board, config, gh_ops)
                )
                if not uat_ok:
                    entry.error = uat_msg
                    events.append(MergeEvent(entry, "uat_required", uat_msg))
                    continue  # skip this entry; try the next in the group
            # CI gate (#240): refuse to merge when checks are failed or
            # still running.  --force-merge overrides for the case where the
            # user has seen the failures and wants to merge anyway.
            # #292 (Defect 3): skip-and-proceed for CI gates too, same logic
            # as the review gate — a pending/failing CI entry should not
            # block an approved sibling in the same (repo, target) group.
            if not force_merge and ci.is_available:
                checks = ci.list_checks_for_pr(entry.repo_github, entry.pr_number)
                # #1904: `failed_checks`/`in_flight_checks`/`_ci_checks_are_stale`
                # below are all filters over `checks` — an empty list
                # satisfies every one of them vacuously, which is exactly
                # the mechanism that let a PR whose CI never ran (a
                # throttled webhook, a wedged run, a path-filtered-out
                # workflow) merge as if it were green. Handled explicitly,
                # ahead of those gates, and only when `expects_checks` says
                # this repo actually declares CI — a repo with none
                # configured (`NoOpCi`, or `GitHubCi` against a repo with no
                # workflows) must not deadlock on this.
                if not checks and _ci_expects_checks(
                    ci, entry.repo_github, entry.pr_number
                ):
                    # #1877: an empty check list is ALSO what GitHub reports
                    # for a PR that conflicts with its base — GitHub can
                    # never build a merge ref for a conflicted PR, so no
                    # `pull_request`-triggered workflow ever runs. That is a
                    # different fact from "CI never ran on a mergeable PR"
                    # and needs the opposite response: fall through to the
                    # merge attempt below, which discovers the real conflict
                    # and (via the `conflict` event / `_dispatch_conflict_
                    # fixes`) dispatches #241's conflict-fix rebase, instead
                    # of pre-empting it here with a "CI never ran" block
                    # that only a human can clear. A confirmed-mergeable or
                    # inconclusive (`None` — still computing, or the `gh`
                    # call itself failed) read leaves today's block
                    # untouched; this is not a license to skip the CI gate
                    # for a merely-slow or unreadable mergeability check.
                    conflicted = gh_ops.check_pr_mergeable(
                        entry.repo_github, entry.pr_number
                    ) is False
                    if not conflicted:
                        msg = (
                            f"{CI_ABSENT_PREFIX} no checks reported for PR "
                            f"#{entry.pr_number} though this repo declares CI "
                            "— merging would run untested code"
                        )
                        entry.error = msg
                        events.append(MergeEvent(entry, "checks_absent", msg))
                        continue  # #292: skip, don't halt the group
                failed = failed_checks(checks)
                if failed:
                    summary = ", ".join(
                        f"{c.name} ({c.conclusion})" for c in failed
                    )
                    # #2347: classify a bare check-list FETCH failure
                    # BEFORE #1892's infra classifier gets a chance — a
                    # synthetic unreadable check always carries `run_id ==
                    # ""`, which `_ci_infra_reason` (via `is_verdictless_job`'s
                    # documented false-negative bias for "no job data") would
                    # read as "carries a verdict about the code", so without
                    # this it falls all the way through to the plain "checks
                    # failed: coord: could not read CI status ..." wording —
                    # exactly the collapse #2347 exists to stop. No
                    # `CiStore` call needed (unlike #1892), so this is safe
                    # to evaluate unconditionally, every time.
                    unreadable_reason = _ci_unreadable_reason(failed)
                    if unreadable_reason is not None:
                        # #2380: a check-list fetch failure this uniform
                        # (EVERY failing check is the synthetic unreadable
                        # stand-in) is ALSO exactly what GitHub reports for a
                        # DIRTY/CONFLICTING PR — it can never build a merge
                        # ref, so `gh pr checks` has nothing to read either.
                        # `_pr_reports_conflicting` tells the two apart the
                        # same way the #1877 checks-absent branch above does:
                        # GitHub's own `mergeable` field, definitive and
                        # always computable, conflict or not. When it reads
                        # CONFLICTING, retrying the CI read can never
                        # succeed — route STRAIGHT to the same `conflict`
                        # event / `_dispatch_conflict_fixes` path a readable
                        # CONFLICT merge-queue status already triggers
                        # (#1474/#241), instead of parking behind a read that
                        # will never resolve.
                        if _pr_reports_conflicting(
                            gh_ops, entry.repo_github, entry.pr_number
                        ):
                            msg = (
                                f"merge conflict: GitHub reports PR "
                                f"#{entry.pr_number} ({entry.branch}) as "
                                f"CONFLICTING against {entry.target_branch} "
                                "— no CI ever ran because GitHub cannot "
                                "build a merge ref for a conflicting PR, so "
                                "the check-list fetch failure was never a "
                                "transient GitHub outage (#2380). Rebase "
                                f"the branch onto {entry.target_branch} and "
                                "resolve."
                            )
                            entry.state = CONFLICT
                            entry.error = msg
                            events.append(MergeEvent(entry, "conflict", msg))
                            continue  # #292: skip, don't halt the group
                        if entry.ci_unreadable_reruns < MAX_CI_UNREADABLE_RERUNS:
                            entry.ci_unreadable_reruns += 1
                            _log.info(
                                "#2347 GitHub unreachable %d/%d for %s#%d "
                                "(PR #%s): %s",
                                entry.ci_unreadable_reruns,
                                MAX_CI_UNREADABLE_RERUNS,
                                entry.repo_name, entry.issue_number,
                                entry.pr_number, summary,
                            )
                            msg = (
                                f"{unreadable_reason} — retrying "
                                f"automatically "
                                f"({entry.ci_unreadable_reruns}/"
                                f"{MAX_CI_UNREADABLE_RERUNS}), no attempt "
                                "spent (#2347)"
                            )
                        else:
                            # #2347: budget exhausted — unlike #1892's own
                            # exhaustion, this deliberately does NOT fall
                            # back to the generic "checks failed" wording:
                            # a bare fetch failure never becomes a real CI
                            # verdict no matter how many times it repeats,
                            # so collapsing the two here would recreate the
                            # exact confusion this issue is about. Still a
                            # bare, no-attempt-spent wait either side of the
                            # cap — there is nothing else to do but wait for
                            # GitHub to answer again — only the wording
                            # escalates, so a human glancing at the queue
                            # can tell "still retrying" from "this has been
                            # going on a while".
                            msg = (
                                f"{unreadable_reason} — GitHub has failed "
                                f"to answer {MAX_CI_UNREADABLE_RERUNS}+ "
                                "consecutive checks in a row; may be a "
                                "standing problem (auth, `gh` config, a "
                                "GitHub outage), not a blip — still "
                                "waiting, no attempt spent, but worth a "
                                "human glance (#2347)"
                            )
                        entry.error = msg
                        events.append(MergeEvent(entry, "checks_unreadable", msg))
                        continue  # #292: skip, don't halt the group
                    # #1892: classify BEFORE deciding the message/event — a
                    # verdictless failure (every failing check says nothing
                    # about the code: never assigned a runner, or died at
                    # "Set up job") gets auto-rerun instead of the plain
                    # "checks failed" block, up to MAX_CI_INFRA_RERUNS times
                    # per entry. This is the one extra `gh api .../jobs` call
                    # per distinct failing run — never issued above on the
                    # absent/pending paths, only here once something has
                    # actually failed.
                    infra_reason = _ci_infra_reason(
                        ci, entry.repo_github, entry.pr_number, failed
                    )
                    if (
                        infra_reason is not None
                        and entry.ci_infra_reruns < MAX_CI_INFRA_RERUNS
                    ):
                        entry.ci_infra_reruns += 1
                        reran = ci.rerun_for_pr(entry.repo_github, entry.pr_number)
                        _log.info(
                            "#1892 auto-rerun %d/%d for %s#%d (PR #%s): %s "
                            "(rerun_for_pr %s)",
                            entry.ci_infra_reruns, MAX_CI_INFRA_RERUNS,
                            entry.repo_name, entry.issue_number,
                            entry.pr_number, summary,
                            "triggered" if reran else "FAILED",
                        )
                        msg = (
                            f"{infra_reason} — auto-rerun "
                            f"{entry.ci_infra_reruns}/{MAX_CI_INFRA_RERUNS} "
                            f"{'triggered' if reran else 'failed to trigger'}"
                        )
                        entry.error = msg
                        events.append(MergeEvent(entry, "ci_infra_rerun", msg))
                        continue  # #292: skip, don't halt the group
                    if infra_reason is not None:
                        # #1892: budget exhausted — a workflow broken at
                        # "Set up job" itself (a bad `uses:` ref, a deleted
                        # action) must stop auto-rerunning and surface to a
                        # human instead of looping forever. Deliberately
                        # WITHOUT the CI_INFRA_PREFIX from here on: this is
                        # no longer something more real time alone resolves,
                        # so it falls back to being treated exactly like a
                        # genuine `checks_failed` block (drive attempts are
                        # spent on it again, same as today).
                        _log.info(
                            "#1892 auto-rerun budget exhausted for %s#%d "
                            "(PR #%s) after %d/%d tries: %s",
                            entry.repo_name, entry.issue_number,
                            entry.pr_number, entry.ci_infra_reruns,
                            MAX_CI_INFRA_RERUNS, summary,
                        )
                        msg = (
                            f"checks failed: {summary} — auto-rerun budget "
                            f"exhausted ({entry.ci_infra_reruns}/"
                            f"{MAX_CI_INFRA_RERUNS}); needs a human"
                        )
                        entry.error = msg
                        events.append(MergeEvent(entry, "checks_failed", msg))
                        continue  # #292: skip, don't halt the group
                    # #2252: reached only when `infra_reason is None` —
                    # every failing check carries a REAL verdict about the
                    # code (#1892's verdictless case is handled/exhausted
                    # above). A real verdict is still not enough to tell
                    # "broken" from "flaky": a 1-in-N random-tempdir
                    # collision reports exactly the same completed/failure
                    # conclusion a genuine regression does. Re-run the
                    # failed job(s) once — scoped via `rerun_failed_for_pr`
                    # so the green evidence from checks that already passed
                    # is never touched — and re-read before spending the
                    # drive attempt this failure would otherwise cost.
                    if entry.ci_flaky_reruns < MAX_CI_FLAKY_RERUNS:
                        rerun_failed_for_pr = getattr(
                            ci, "rerun_failed_for_pr", None
                        )
                        reran = (
                            rerun_failed_for_pr(entry.repo_github, entry.pr_number)
                            if rerun_failed_for_pr is not None
                            else False
                        )
                        if reran:
                            entry.ci_flaky_reruns += 1
                            entry.ci_flaky_pending = json.dumps({
                                "checks": [
                                    {"name": c.name, "conclusion": c.conclusion}
                                    for c in failed
                                ],
                                "sha": entry.branch_head_sha,
                            })
                            msg = (
                                f"{CI_FLAKY_PREFIX} {summary} — re-running "
                                "once before treating as broken "
                                f"({entry.ci_flaky_reruns}/"
                                f"{MAX_CI_FLAKY_RERUNS}, #2252)"
                            )
                            entry.error = msg
                            _log.info(
                                "#2252 flake re-check %d/%d for %s#%d "
                                "(PR #%s): %s (rerun_failed_for_pr triggered)",
                                entry.ci_flaky_reruns, MAX_CI_FLAKY_RERUNS,
                                entry.repo_name, entry.issue_number,
                                entry.pr_number, summary,
                            )
                            events.append(MergeEvent(entry, "ci_flaky_rerun", msg))
                            continue  # #292: skip, don't halt the group
                        # #2252 fail-safe: the re-run could not be
                        # triggered (no `rerun_failed_for_pr` capability, or
                        # the `gh` call itself failed) — fall straight
                        # through to the plain `checks_failed` block below,
                        # spending the attempt exactly as if this feature
                        # did not exist. Never treat "could not re-run" as
                        # "passed", and never increment `ci_flaky_reruns`
                        # for a re-run that never actually happened.
                    elif entry.ci_flaky_pending:
                        # Budget already spent for this failure streak and
                        # it is STILL red on the second read — confirmed
                        # real, not a flake (#2252's other acceptance
                        # criterion: "a check that fails twice: attempt
                        # consumed, entry blocks — identical to today").
                        # Nothing to record; clear the pending marker so a
                        # later, unrelated resolution doesn't misattribute
                        # this already-confirmed streak as a flake.
                        entry.ci_flaky_pending = ""
                    msg = f"checks failed: {summary}"
                    entry.error = msg
                    events.append(MergeEvent(entry, "checks_failed", msg))
                    continue  # #292: skip, don't halt the group
                pending = in_flight_checks(checks)
                if pending:
                    summary = ", ".join(c.name for c in pending)
                    # #1891: same `CI_PENDING_PREFIX` wording `_entry_gate_status`
                    # returns for the board render — this is what lets
                    # `IssueState.merge_reason`'s raw-row fallback (see that
                    # constant's docstring) carry the SAME, recognisable marker
                    # even when it falls back to this persisted `entry.error`
                    # instead of a fresh live re-evaluation.
                    msg = f"{CI_PENDING_PREFIX} {summary}"
                    entry.error = msg
                    events.append(MergeEvent(entry, "checks_pending", msg))
                    continue  # #292: skip, don't halt the group
                # #1892: this line is only reached once BOTH `if failed:`
                # and `if pending:` above did not fire — a genuine
                # resolution (checks non-empty*, nothing failed, nothing
                # pending), not merely "not currently failed". Resetting on
                # "not failed" alone was a bug: the tick right after this
                # same code triggers an auto-rerun almost always observes
                # the rerun as still pending (a real Actions run takes real
                # wall-clock minutes), which would zero the budget before
                # the rerun itself ever resolves — so a workflow genuinely
                # broken at "Set up job" would fail, rerun, get reset to 0
                # while pending, fail again, rerun again... forever, never
                # reaching MAX_CI_INFRA_RERUNS and never parking for a
                # human. Whatever verdictless run the budget was tracking
                # has now actually resolved one way or another, so a LATER
                # failure (a fresh push, a flaky green-then-red) starts its
                # own budget from zero rather than inheriting an unrelated
                # exhausted count.
                # * `checks` can still be `[]` here for a repo that doesn't
                # declare CI at all (`_ci_expects_checks` false, so the
                # `checks_absent` branch above didn't fire) — resetting in
                # that case is harmless since such an entry can never have
                # accrued a nonzero `ci_infra_reruns` to begin with.
                entry.ci_infra_reruns = 0
                # #2252: this same "genuinely resolved" point is also where
                # a suspected-flaky failure gets its answer: nothing failed
                # and nothing is pending, so the one re-run this entry spent
                # (see the `ci_flaky_reruns`/`CI_FLAKY_PREFIX` branch above)
                # came back clean. Record it — the audit trail is what keeps
                # this from quietly becoming a "just retry until green"
                # button: a check that is flaky 30% of the time should show
                # up as thirty recorded flakes, not thirty invisible free
                # passes.
                if entry.ci_flaky_pending:
                    _record_ci_flake_audit(entry, entry.ci_flaky_pending)
                    entry.ci_flaky_pending = ""
                entry.ci_flaky_reruns = 0
                # #2347: same "genuinely resolved" reset, for the identical
                # reason ci_infra_reruns is reset above — GitHub answered
                # this time (whatever it said), so whatever run of fetch
                # failures the budget was tracking is over. A LATER,
                # unrelated fetch failure starts its own count from zero.
                entry.ci_unreadable_reruns = 0
                # #1851: a green CI result can itself be stale relative to the
                # base — see `_ci_checks_are_stale`'s docstring. Named
                # distinctly (`checks_stale`) from checks_failed/
                # checks_pending above so an operator (and `coord merge
                # --revalidate`, the remedy) can tell the three apart.
                #
                # #2197: this used to always block here, escalating to a
                # human (or, via `coord drive`, spending a merge attempt)
                # for a condition a re-run resolves on its own — the exact
                # #2170 regression (a docs-only base move stales a
                # perfectly good green PR). Mirror #1892's shape exactly:
                # auto-rerun via the SAME `CiStore.rerun_for_pr` this
                # module already calls unattended for verdictless
                # failures, up to `MAX_CI_STALE_RERUNS` — but track it
                # with its OWN counter (`ci_stale_reruns`), never
                # `ci_infra_reruns`, so a failed-then-stale (or
                # stale-then-failed) PR does not have one trigger silently
                # spend the other's budget, and so the audit trail can
                # always tell which condition an auto-rerun was answering.
                if checks and _ci_checks_are_stale(
                    checks, gh_ops, entry.repo_github, entry.target_branch, smoke,
                ):
                    if entry.ci_stale_reruns < MAX_CI_STALE_RERUNS:
                        entry.ci_stale_reruns += 1
                        reran = ci.rerun_for_pr(entry.repo_github, entry.pr_number)
                        _log.info(
                            "#2197 auto-rerun %d/%d for stale CI on %s#%d "
                            "(PR #%s) (rerun_for_pr %s)",
                            entry.ci_stale_reruns, MAX_CI_STALE_RERUNS,
                            entry.repo_name, entry.issue_number,
                            entry.pr_number,
                            "triggered" if reran else "FAILED",
                        )
                        # #1891: same `CI_PENDING_PREFIX` wording the
                        # genuinely-still-running case uses above — this is
                        # what lets `coord drive`'s `is_ci_pending_reason`
                        # check (coord/drive.py) treat a re-run THIS auto-
                        # trigger just kicked off exactly like any other
                        # in-flight CI: a bare wait, never a spent merge
                        # attempt. The queue resumes it automatically once
                        # the re-run reports, no operator needed.
                        msg = (
                            f"{CI_PENDING_PREFIX} re-run triggered for CI "
                            "checks that predate the current base (#2197 "
                            f"auto-rerun {entry.ci_stale_reruns}/"
                            f"{MAX_CI_STALE_RERUNS} "
                            f"{'triggered' if reran else 'failed to trigger'})"
                        )
                        entry.error = msg
                        events.append(MergeEvent(entry, "checks_stale_rerun", msg))
                        continue  # #292: skip, don't halt the group
                    # #1826: same renderer the plan path uses, so the two
                    # surfaces can never describe this condition differently.
                    msg = ci_stale_reason(
                        checks, gh_ops, entry.repo_github, entry.target_branch,
                        suffix=(
                            f"; auto-rerun budget exhausted "
                            f"({entry.ci_stale_reruns}/{MAX_CI_STALE_RERUNS})"
                        ),
                    )
                    entry.error = msg
                    events.append(MergeEvent(entry, "checks_stale", msg))
                    continue  # #292: skip, don't halt the group
                # #2197: reached only once the checks are genuinely fresh
                # (or the smoke-side #1738/#1778/#1847 base-move exemption
                # spared them) — mirrors the `ci_infra_reruns = 0` reset
                # above and for the identical reason: whatever staleness
                # streak the budget was tracking has now actually resolved,
                # so a LATER base move starts its own budget from zero
                # rather than inheriting an unrelated exhausted count.
                entry.ci_stale_reruns = 0
            elif force_merge and ci.is_available:
                # #1826: the override still overrides — but it says so. A
                # forced merge over STALE CI is the 2026-08-04 incident's
                # exact shape performed deliberately, and "merged with no CI
                # event at all" reads in the audit trail identically to
                # "merged because CI was green". Advisory only: never blocks,
                # never mutates the entry, and stays silent unless the checks
                # are POSITIVELY known stale (see `ci_stale_waiver_message`).
                # Same "an override must never be silent" posture as the
                # `epic_closing_keyword_in_commit_forced` event below.
                _stale_waiver = ci_stale_waiver_message(entry, ci, gh_ops, smoke)
                if _stale_waiver:
                    events.append(
                        MergeEvent(entry, "checks_stale_forced", _stale_waiver)
                    )
            # #1318: cache is_epic_issue lookups for this entry — the same
            # referenced number can show up in both the PR body and one or
            # more commit messages below, and each lookup is a `gh` round
            # trip. Best-effort like every check in this block: a lookup
            # failure just means "not known to be an epic", never a block.
            _epic_cache: dict[int, bool] = {}

            def _is_epic(n: int) -> bool:
                if n not in _epic_cache:
                    try:
                        _epic_cache[n] = gh_ops.is_epic_issue(entry.repo_github, n)
                    except Exception:  # noqa: BLE001
                        _epic_cache[n] = False
                return _epic_cache[n]

            # #1196 hole 2 / #1318: GitHub's own closing-keyword magic reads
            # the PR body directly at merge time and never calls
            # `github_ops.close_issue` — that chokepoint's open-children
            # guard can't stop it. Scan the body for `Closes #N`/`Fixes
            # #N`/`Resolves #N` and downgrade to `Refs #N` for any N that
            # either currently has open children (#1196) or carries the
            # epic/tracking label (#1318 — an epic can have zero open
            # children today and still be the wrong thing to auto-close),
            # before the merge lands. Best effort throughout: a lint
            # failure must never block a merge.
            try:
                pr_body = gh_ops.get_pr_body(entry.repo_github, entry.pr_number)
            except Exception:  # noqa: BLE001
                pr_body = ""
            if pr_body:
                referenced = find_closing_references(pr_body)
                blocking: set[int] = set()
                for n in referenced:
                    try:
                        if gh_ops.has_open_children(entry.repo_github, n):
                            blocking.add(n)
                    except Exception:  # noqa: BLE001
                        pass
                    if _is_epic(n):
                        blocking.add(n)
                if blocking:
                    new_body, downgraded = downgrade_closing_keywords(pr_body, blocking)
                    if downgraded:
                        try:
                            gh_ops.edit_pr_body(entry.repo_github, entry.pr_number, new_body)
                            events.append(MergeEvent(
                                entry, "pr_body_downgraded",
                                "downgraded closing keyword to Refs for "
                                + ", ".join(f"#{n}" for n in downgraded)
                                + " (open children / epic — #1196/#1318)",
                            ))
                        except Exception as e:  # noqa: BLE001
                            events.append(MergeEvent(
                                entry, "pr_body_downgrade_failed",
                                f"could not downgrade PR #{entry.pr_number} body "
                                f"for {', '.join(f'#{n}' for n in downgraded)}: {e}",
                            ))

            # #1318: the PR-body scan above can't help with commit messages
            # — GitHub's closing-keyword scanner reads those too once they
            # land on the base branch (every original commit, unchanged, for
            # `--rebase`/`--merge`; and depending on repo settings, squash's
            # default commit body can pull the same text). There's no local
            # git checkout in this `gh`-only wire layer to amend and
            # force-push a rewritten message, so a hit here **blocks** the
            # merge rather than silently rewriting history. `force_merge`
            # overrides (same flag `--force-merge` already uses to skip the
            # CI gate) but the override is never silent — a warning event
            # still fires so it shows up in `coord merge` output and the
            # audit trail.
            try:
                commit_messages = gh_ops.get_pr_commit_messages(
                    entry.repo_github, entry.pr_number
                )
            except Exception:  # noqa: BLE001
                commit_messages = []
            commit_referenced: set[int] = set()
            for message in commit_messages:
                commit_referenced.update(find_closing_references(message))
            commit_epic_hits = sorted(n for n in commit_referenced if _is_epic(n))
            if commit_epic_hits:
                numbers_str = ", ".join(f"#{n}" for n in commit_epic_hits)
                msg = (
                    f"a commit message on this branch contains a closing keyword "
                    f"(Closes/Fixes/Resolves) for {numbers_str}, which carries the "
                    f"'epic' label — GitHub auto-closes it on merge regardless of "
                    f"the PR body (#1318). Reword the commit message(s) to "
                    f"'refs #N' / 'epic #N' and push, or pass --force-merge to "
                    f"merge anyway (the epic WILL still auto-close)."
                )
                if force_merge:
                    events.append(MergeEvent(
                        entry, "epic_closing_keyword_in_commit_forced", msg,
                    ))
                else:
                    entry.error = msg
                    events.append(MergeEvent(
                        entry, "epic_closing_keyword_in_commit", msg,
                    ))
                    continue  # #1318: refuse — never merge a branch that will
                    # auto-close an epic via a commit message we can't rewrite.

            # #1467: pre-flight linearity check. GitHub refuses to
            # rebase-merge any branch containing a merge commit ("This
            # branch can't be rebased") — a distinct failure from a content
            # conflict, and one GitHub's own `mergeable` field can't predict
            # (a branch with a merge commit still reads MERGEABLE). Detect
            # it via the PR's commit list — no local checkout is guaranteed
            # on the daemon host, so `git rev-list --merges` is the wrong
            # instrument here — and fall back to squash, which is always
            # valid and keeps the target branch linear.
            #
            # Fail-closed: `branch_has_merge_commit` is optional on `gh_ops`
            # (older stubs in tests predate #1467) and returns `None` on any
            # `gh` error or ambiguous response; either case leaves `method`
            # unchanged rather than guessing.
            merge_method = method
            if method == "rebase":
                _probe = getattr(gh_ops, "branch_has_merge_commit", None)
                if _probe is not None:
                    try:
                        _has_merge_commit = _probe(entry.repo_github, entry.pr_number)
                    except Exception:  # noqa: BLE001
                        _has_merge_commit = None
                    if _has_merge_commit is True:
                        merge_method = "squash"
                        events.append(MergeEvent(
                            entry, "method_fallback",
                            f"PR #{entry.pr_number} ({entry.branch}) contains a "
                            "merge commit and cannot be rebase-merged — "
                            "falling back to --squash (#1467)",
                        ))

            entry.last_attempt = time.time()
            entry.state = MERGING
            ok, msg = gh_ops.merge_pr(entry.repo_github, entry.pr_number, method=merge_method)
            if ok:
                entry.state = MERGED
                entry.error = None
                # #1767: the condition that produced a drive escalation for
                # this issue (if any) just resolved through the normal
                # pipeline — clear it so `coord escalate list` doesn't
                # accumulate phantoms for merged work. Idempotent and a
                # no-op when there's nothing on file; routes through
                # `coord.state` so it works (via the daemon) from a thin
                # client rather than writing the local DB directly.
                try:
                    dismiss_drive_escalation(entry.repo_name, entry.issue_number)
                except Exception:  # noqa: BLE001 — never fail a merge on this
                    pass
                # #1213: audit any gate bypassed by a per-issue label override
                # BEFORE announcing the merge, so the "merged" event message
                # already carries the bypass note — a bypass is never silent.
                # Only fires on a real merge (never dry-run, handled above via
                # the side-effect-free _bypass_note) so previews can't write
                # phantom audit rows.
                _record_gate_bypass_audit(entry, config)
                bypass_note = _bypass_note(entry, config)
                # Deterministically close the linked issue.  GitHub's `Closes #N`
                # auto-close only fires when the PR *body* carries the keyword
                # AND it merges into the default branch; the worker-created-PR
                # path only asks the LLM for it and `fix(#N):` subjects aren't
                # closing keywords, so issues got stranded open (#806).
                # Best-effort — a close failure must not undo a successful merge.
                # Closing on GitHub keeps the daemon the sole DB writer: the next
                # reconcile/sync flips the cached row to closed (state.py).
                #
                # #1077: only for entries whose issue_number is actually
                # resolved by this PR (CLOSES_ISSUE_TYPES). A "mock-author"
                # entry's issue_number is the milestone's tracking issue —
                # closing it here would be the exact #1077 bug regardless of
                # what the PR body says.
                if entry.assignment_type in CLOSES_ISSUE_TYPES:
                    try:
                        gh_ops.close_issue(entry.repo_github, entry.issue_number)
                        events.append(MergeEvent(
                            entry, "merged",
                            f"merged PR #{entry.pr_number}; closed issue #{entry.issue_number}"
                            f"{bypass_note}",
                        ))
                    except Exception as e:  # noqa: BLE001 — never fail a merge on close
                        events.append(MergeEvent(
                            entry, "merged",
                            f"merged PR #{entry.pr_number} (warning: could not "
                            f"close issue #{entry.issue_number}: {e}){bypass_note}",
                        ))
                else:
                    events.append(MergeEvent(
                        entry, "merged",
                        f"merged PR #{entry.pr_number}; issue #{entry.issue_number} "
                        f"left open (assignment type {entry.assignment_type!r} "
                        f"does not close its tracking issue, #1077){bypass_note}",
                    ))
                # #2164: the fix just landed on the default branch — the
                # ordering event `expected_red` clearing must wait for
                # (never `coord acceptance record` time, which can run long
                # before Test/Review/this merge actually happen). Best
                # effort, never blocks or fails the merge itself.
                try:
                    clear_event = _maybe_clear_expected_red(entry, board, gh_ops, config)
                except Exception as e:  # noqa: BLE001 — bookkeeping, never undoes a real merge
                    clear_event = MergeEvent(entry, "expected_red_clear_failed", str(e))
                if clear_event is not None:
                    events.append(clear_event)
                # PDR-3 (#2508): a merged Gate-A (`type="mock-author"`)
                # branch auto-pushes a design round to the portal, if (and
                # only if) its milestone has a portal link on file. Best
                # effort, same as the expected_red clear above — a portal
                # outage, or simply no `coord portal link` recorded, must
                # never undo a real merge.
                try:
                    design_round_event = _maybe_push_design_round(entry, config, gh_ops)
                except Exception as e:  # noqa: BLE001 — bookkeeping, never undoes a real merge
                    design_round_event = MergeEvent(
                        entry, "design_round_push_failed", str(e)
                    )
                if design_round_event is not None:
                    events.append(design_round_event)
                # #2588: the same auto-push pattern as the design round just
                # above, applied to status — a merged `type="work"` PR that
                # closed its issue may have just finished (or started) its
                # submission; fold every linked issue and push if changed.
                # Best effort, same posture: never undoes a real merge.
                try:
                    status_event = _maybe_push_status(entry, config, gh_ops, board)
                except Exception as e:  # noqa: BLE001 — bookkeeping, never undoes a real merge
                    status_event = MergeEvent(entry, "status_push_failed", str(e))
                if status_event is not None:
                    events.append(status_event)
                continue
            entry.state = CONFLICT
            entry.error = msg
            events.append(MergeEvent(entry, "conflict", msg))
            continue  # #735: park this entry; siblings in same group still merge

    # #1896 Phase 0: persist merge-gate CI refusal counts by reason — the
    # third of the three seams the forge-availability program asks for.
    # Live attempts only: `dry_run` is a single flag for this whole call (see
    # the one `if dry_run:` branch above), so every event in `events` is a
    # preview when it's set, and a preview isn't a real refusal to count.
    if not dry_run:
        _record_forge_gate_refusals(events)

    return events


def _record_forge_gate_refusals(events: list["MergeEvent"]) -> None:
    """Best-effort: persist one forge-availability row per live merge-gate CI
    refusal in *events* (#1896 Phase 0). Never raises — `record_merge_gate_
    refusal` is itself best-effort, and this loop is a thin filter over
    `events` on top of it, so there is nothing here that needs its own
    try/except beyond what that function already guarantees."""
    for event in events:
        if event.kind not in MERGE_GATE_REFUSAL_KINDS:
            continue
        record_merge_gate_refusal(
            repo=event.entry.repo_name,
            issue=event.entry.issue_number,
            reason=event.kind,
            message=event.message,
        )


# ── Drop / prune (#732) ──────────────────────────────────────────────────

def drop_entry(assignment_id: str) -> bool:
    """Remove exactly the merge_queue row keyed to *assignment_id*.

    Returns ``True`` when a row was deleted, ``False`` when no matching row
    was found.  This is the surgical mutation that ``coord merge --drop`` and
    the TUI "drop" action use; it never touches other rows.

    Because the queue lives on the daemon host, callers on thin clients must
    route through the daemon (``/merge`` endpoint with ``"drop": aid`` in the
    body) rather than calling this directly — the daemon guard pattern is the
    same as ``coord merge`` (#584).

    #1477: *assignment_id* is resolved via :func:`resolve_entry_key`, so the
    durable ``repo#issue`` form works here too — not just a raw assignment
    id, which can go stale across a drop + re-enqueue cycle.
    """
    conn = get_connection()
    entry = resolve_entry_key(load_queue(), assignment_id)
    if entry is None:
        return False
    with conn:
        cursor = sql.execute(
            conn, "DELETE FROM merge_queue WHERE assignment_id = ?", (entry.assignment_id,)
        )
    return cursor.rowcount > 0


def prune_stale_queue_entries(dry_run: bool = False) -> list["QueuedMerge"]:
    """Remove merge_queue entries whose issue is closed or PR is already merged.

    Returns the list of pruned entries so callers can surface them in output.

    Only non-``MERGED`` entries are inspected — entries already recorded as
    ``MERGED`` are correct history and are left untouched.

    Uses :func:`coord.github_ops.issue_is_closed` and
    :func:`coord.github_ops.pr_is_merged`, both of which **fail-open**
    (return ``False`` on any ``gh`` error) so a transient GitHub/CLI failure
    never silently prunes a live entry.

    #3063: gated by :func:`coord.models.trust_issue_closed_for` on the
    entry's own ``assignment_type`` (populated at enqueue time, #1077) —
    the same carve-out `enqueue_approved_work` already applies on the way
    in (#2639). A test-author/mock-author entry's `issue_number` is the
    milestone's *tracking* issue, not this row's own deliverable, so a
    closed tracking epic is not evidence THIS row's branch landed. Without
    this gate, this sweep deleted the queue row the instant the epic
    closed — undoing enqueue's carve-out and feeding an infinite
    enqueue -> open PR -> close PR (reconcile.close_stale_prs) -> prune
    loop.
    """
    from coord import github_ops  # noqa: PLC0415

    entries = load_queue()
    stale: list[QueuedMerge] = []
    surviving: list[QueuedMerge] = []

    for entry in entries:
        if entry.state == MERGED:
            surviving.append(entry)
            continue

        is_stale = False
        if trust_issue_closed_for(entry.assignment_type) and github_ops.issue_is_closed(
            entry.repo_github, entry.issue_number
        ):
            is_stale = True
        elif entry.branch and github_ops.pr_is_merged(entry.repo_github, entry.branch):
            is_stale = True

        if is_stale:
            stale.append(entry)
        else:
            surviving.append(entry)

    if not dry_run and stale:
        save_queue(surviving)

    return stale


# ── Convenience ──────────────────────────────────────────────────────────

def pending_summary(items: list[QueuedMerge]) -> dict[str, list[QueuedMerge]]:
    """Group items for display in `coord status`. Returns {repo_name: [entries]}."""
    out: dict[str, list[QueuedMerge]] = {}
    for entry in items:
        if entry.state in (MERGED, SKIPPED):
            continue
        out.setdefault(entry.repo_name, []).append(entry)
    return out
