"""Issue claim detection — prevent two agents picking up the same issue.

Without this, two coordinator runs (e.g. two operators, or a manual dispatch
racing an auto-dispatch) can both kick off work on the same issue because
neither side notices the other. The fix is a simple pre-dispatch check:

1. Is there an active board assignment for `(issue_number, repo_name)`?
2. Does the remote already have a branch matching `issue-{N}-*` for this
   repo? (Workers create branches in that shape — its existence is treated
   as a claim signal even if our board doesn't know about it yet.)

If either is true, the dispatch site refuses with a clear message. Reviews
and smoke tests run *after* a worker has pushed a branch, so they don't
participate in this check — they have their own dedupe (no two reviews of
the same completed assignment).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from coord.models import Board


# A branch_lookup takes (repo_github, issue_number) and returns the matching
# branch names. Injected so tests don't hit GitHub.
BranchLookup = Callable[[str, int], list[str]]


@dataclass
class Claim:
    """Evidence that an issue is already being worked on."""

    issue_number: int
    repo_name: str
    source: str  # "board" or "remote_branch"
    machine_name: str | None = None
    branch: str | None = None
    assignment_id: str | None = None


def find_work_claim(
    issue_number: int,
    repo_name: str,
    repo_github: str,
    board: Board,
    *,
    branch_lookup: BranchLookup | None = None,
) -> Claim | None:
    """Return a `Claim` if dispatching new work for this issue would conflict.

    Checks the board first (cheap, local) then the remote (one `gh` call).
    Returns the first hit — callers only need to know *that* it's claimed,
    not enumerate every claim source.
    """
    for a in board.active:
        if a.issue_number == issue_number and a.repo_name == repo_name:
            if a.status == "failed":
                continue
            # #1059 fix: "chat"/"troubleshoot" are human-attended, read-only
            # diagnostic sessions (#628/#676, coord/issue_store.py's
            # `("chat", "troubleshoot")` advisory-only special case) — they
            # never commit code and are always finalized as "advisory"
            # regardless of how they end. A stale one (session died without
            # the TUI ever finalizing it, e.g. a "Chat about issue" left
            # open on an epic) must not permanently block a real dispatch
            # for the same issue, the way a genuine work/review/smoke claim
            # should. Without this, a leftover chat row on a tracking issue
            # wedged every future "Dispatch Gate A mock" attempt with
            # "already in flight" against a claim with no backing worker and
            # no way to clear it short of `coord diagnose`.
            if a.type in ("plan", "review", "smoke", "chat", "troubleshoot"):
                continue
            return Claim(
                issue_number=issue_number,
                repo_name=repo_name,
                source="board",
                machine_name=a.machine_name,
                branch=a.branch,
                assignment_id=a.assignment_id,
            )

    lookup = branch_lookup or _default_branch_lookup
    branches = lookup(repo_github, issue_number)
    if branches:
        return Claim(
            issue_number=issue_number,
            repo_name=repo_name,
            source="remote_branch",
            branch=branches[0],
        )
    return None


def claim_message(claim: Claim) -> str:
    """Human-readable error for a refused dispatch."""
    if claim.source == "board":
        parts = [
            f"issue #{claim.issue_number} ({claim.repo_name}) already claimed",
        ]
        if claim.machine_name:
            parts.append(f"by {claim.machine_name}")
        if claim.assignment_id:
            parts.append(f"(assignment {claim.assignment_id})")
        if claim.branch:
            parts.append(f"on branch {claim.branch}")
        return " ".join(parts)
    return (
        f"issue #{claim.issue_number} ({claim.repo_name}) already claimed: "
        f"remote branch {claim.branch} exists"
    )


def claim_remedy_hint(claim: Claim, repo_name: str, issue_number: int) -> str:
    """The escape hatch to name in a refusal message, matched to *claim.source*.

    #3103: naming the wrong remedy is worse than naming none — an operator who
    follows it burns a full round trip before learning it was never going to
    help. The two claim sources need genuinely different remedies because
    they're different kinds of state:

    - ``"board"``: a board row (dead session, wedged assignment). This is
      exactly what `coord diagnose <repo> <issue>` inspects and can clear.
    - ``"remote_branch"``: a leftover branch with no board row at all — most
      often the source branch of a **squash-merged** PR that GitHub never
      deleted (the repo's content landed on the default branch as a new
      commit; the branch itself is just residue). `coord diagnose` inspects
      board *stages*; it has no branch-deletion path, so it cannot clear this
      even in principle — it will report a clean bill of health and change
      nothing (the exact false lead #3103 reported). The real fix is to check
      whether the branch's PR merged and, if so, delete the stale branch.
    """
    if claim.source == "board":
        return (
            f"if that session is dead, clear it with `coord diagnose "
            f"{repo_name} {issue_number}`, then dispatch again"
        )
    branch = claim.branch or "<branch>"
    return (
        f"if its PR already merged, delete the stale branch with `git push "
        f"origin --delete {branch}`, then dispatch again; if the PR is still "
        f"open, wait for it to land (or close it) first"
    )


# ── Dedupe for downstream auto-dispatch (review / smoke) ────────────────────


def has_active_followup(
    board: Board,
    *,
    of_assignment_id: str | None,
    assignment_type: str,
) -> bool:
    """True if `board.active` already has a review/smoke of the given work.

    Used by `dispatch_review`/`dispatch_smoke` to skip when one is already in
    flight. The check is by `review_of_assignment_id` rather than `(issue,
    repo)` so that re-dispatching after a worker re-runs the same issue
    isn't accidentally blocked.
    """
    if of_assignment_id is None:
        return False
    for a in board.active:
        if a.type != assignment_type:
            continue
        if a.review_of_assignment_id == of_assignment_id:
            return True
    return False


def has_active_branch_followup(
    board: Board,
    *,
    repo_name: str,
    branch: str | None,
    assignment_type: str,
) -> bool:
    """True if `board.active` already has a follow-up for this ``(repo, branch)``.

    #1819: the branch-scoped peer of :func:`has_active_followup`. The unit a
    Test-stage run actually measures is the **branch**, not the work row that
    happened to push it — and after a fix round (``coord fix`` / ``--fix-of``
    reuses the branch **by design**) one branch carries *two* ``work`` rows.
    The ``of_assignment_id``-keyed dedupe asks "does *this row* already have a
    smoke in flight?" and answers "no" for the sibling row, so both rows
    dispatched their own Test worker: two machines running the identical suite
    on the identical branch, racing to write a verdict (observed live on
    #1797, 2026-08-04).

    Deliberately NOT applied to reviews. A review is a judgement *of a work
    row's contribution* — the fix round genuinely needs its own review even
    though it shares a branch with the round it fixes — whereas a smoke run
    on branch B is the same measurement no matter which row asked for it.
    """
    if not branch:
        return False
    for a in board.active:
        if a.type != assignment_type:
            continue
        if a.repo_name == repo_name and a.branch == branch:
            return True
    return False


def superseding_work_row(board: Board, assignment) -> "Assignment | None":
    """The later work-like row on the same ``(repo, branch)``, if any (#1819).

    A ``work`` row that another work-like row was dispatched *after*, on the
    same branch, is **superseded**: the branch's current content is the later
    row's output, so the earlier row is not a meaningful dispatch target for
    the Test stage. Dispatching against it burns a machine re-testing a branch
    it did not produce and lands the verdict on a row nothing gates on.

    Ordering is by ``dispatched_at``, with the assignment id as a deterministic
    tie-break so two rows stamped in the same second still order stably. A
    ``failed``, ``advisory``, or ``refused_policy`` later row does not
    supersede — all three are terminal no-op outcomes (``_ZERO_COMMIT_TYPES``
    in ``coord/agent.py``: zero commits pushed, e.g. "already fixed", a
    graceful usage-limit exit, or a #2234 policy refusal) that leave the
    branch exactly as the earlier row left it, so the earlier row is still
    the branch's author. Without excluding these here, a `--fix-of` round
    that lands zero commits would falsely mark the row it was fixing as
    superseded — and since none of ``advisory``/``refused_policy`` is itself
    a valid dispatch target (``dispatch_smoke`` requires ``status ==
    "done"``), the branch would then never get *any* Test dispatch. (#2234
    added ``refused_policy`` to this exclusion — it's drawn from the same
    ``_ZERO_COMMIT_TYPES`` gate as ``advisory`` in ``coord/agent.py``'s
    ``_reap`` and reproduces the identical failure mode if left out.)

    Returns the (newest) superseding row so callers can name it in a log line,
    or ``None`` when *assignment* is the branch's current work row. Related but
    distinct from #1277, which is the *display* side of the same idea.
    """
    from coord.models import WORK_LIKE_TYPES  # noqa: PLC0415

    branch = getattr(assignment, "branch", None)
    if not branch:
        return None
    aid = getattr(assignment, "assignment_id", None)
    key = (getattr(assignment, "dispatched_at", None) or 0.0, aid or "")

    best = None
    best_key: tuple[float, str] | None = None
    for a in list(board.active) + list(board.completed):
        if a is assignment:
            continue
        if a.type not in WORK_LIKE_TYPES:
            continue
        if a.status in ("failed", "advisory", "refused_policy"):
            continue
        if a.repo_name != assignment.repo_name or a.branch != branch:
            continue
        other_aid = getattr(a, "assignment_id", None)
        if other_aid is not None and other_aid == aid:
            continue
        other_key = (getattr(a, "dispatched_at", None) or 0.0, other_aid or "")
        if other_key <= key:
            continue
        if best_key is None or other_key > best_key:
            best, best_key = a, other_key
    return best


def has_active_work_followup(
    board: Board,
    *,
    repo_name: str,
    issue_number: int,
) -> bool:
    """True if a work or conflict-fix assignment is actively running for (repo, issue).

    Used before dispatching a review to skip when a coord-bounce fix is
    actively rewriting the branch — dispatching a review against stale code
    produces a verdict on code that's about to change and causes unnecessary
    churn.  The existing ``has_active_followup`` covers duplicate-review
    dedupe; this covers the orthogonal case where a *work* re-run (not a
    review) is live for the same issue.

    Called from both the reconcile review-dispatch loop and ``dispatch_review``
    for defence in depth.

    #1553: keyed on the *effective* issue
    (:func:`coord.models.effective_issue_number`), not the raw
    ``issue_number``. Every oracle-loop acceptance slice under one milestone
    shares the tracking issue's number, so keying on the raw field made an
    in-flight fix for child A block review dispatch for child B — and for the
    epic itself — even though they are unrelated pieces of work. With the
    effective issue, a slice's follow-up only guards its own child.
    """
    from coord.models import effective_issue_number  # noqa: PLC0415

    _WORK_TYPES = frozenset({"work", "conflict-fix"})
    for a in board.active:
        if a.type not in _WORK_TYPES:
            continue
        if a.status == "failed":
            continue
        if a.repo_name == repo_name and effective_issue_number(a) == issue_number:
            return True
    return False


# ── Default branch lookup (uses gh) ─────────────────────────────────────────


def _default_branch_lookup(repo_github: str, issue_number: int) -> list[str]:
    """Return remote branches whose name starts with `issue-{N}-`.

    Uses `gh api repos/.../git/matching-refs/heads/issue-{N}-`. Empty result
    on any lookup failure — we'd rather wave through a dispatch than block
    on a transient GH error.
    """
    from coord import github_ops

    try:
        raw = github_ops._gh(
            "api",
            f"repos/{repo_github}/git/matching-refs/heads/issue-{issue_number}-",
        )
    except RuntimeError:
        return []
    try:
        refs = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(refs, list):
        return []
    branches: list[str] = []
    for r in refs:
        if not isinstance(r, dict):
            continue
        ref = r.get("ref", "")
        if isinstance(ref, str) and ref.startswith("refs/heads/"):
            branches.append(ref[len("refs/heads/"):])
    # Drop branches already fully merged into the default branch — a merged
    # branch is finished work, not an active claim. A stale merged branch (e.g. a
    # PR head that wasn't auto-deleted on merge) must not block new work on the
    # issue forever (the chat→work block on a long-merged issue-N-* branch).
    return _drop_merged_branches(repo_github, branches)


def _repo_default_branch(repo_github: str) -> str | None:
    """The repo's default branch via the GH API, or None on any error."""
    from coord import github_ops

    try:
        data = json.loads(github_ops._gh("api", f"repos/{repo_github}"))
    except (RuntimeError, ValueError):
        return None
    val = data.get("default_branch") if isinstance(data, dict) else None
    return val if isinstance(val, str) and val else None


def _drop_merged_branches(repo_github: str, branches: list[str]) -> list[str]:
    """Filter out branches that are finished work, not an active claim.

    Two independent merge signals; either is sufficient to drop a branch:

    1. **`github_ops.pr_is_merged`** (#1150) — asks GitHub directly whether the
       branch's PR merged, keyed on the branch's *current* tip commit. This is
       the signal that matters when the repo **squash-merges** (#3103): a
       squash merge lands the PR's content as a brand-new commit on the
       default branch and never puts the branch's own commits on it, so
       ancestry (below) reads "not merged" *forever* even though the work
       landed hours or days ago — the exact bug that let a squash-merged
       Gate-A branch permanently block every later amendment on its epic.
    2. **`ahead_by == 0`** on `compare/{default}...{branch}` — the pre-#3103
       ancestry check. Still needed for a branch that merged without ever
       going through a PR (a direct fast-forward/merge push): `pr_is_merged`
       finds no PR at all in that case and fails open.

    Conservative on every uncertainty — unknown default branch, compare-API
    error, no merged PR found — keeps the branch as a claim (fail toward
    blocking duplicate work, never toward allowing it).
    """
    if not branches:
        return branches
    from coord import github_ops

    default_branch = _repo_default_branch(repo_github)
    kept: list[str] = []
    for b in branches:
        if default_branch and b == default_branch:
            continue
        if github_ops.pr_is_merged(repo_github, b):
            continue  # PR confirmed merged (survives squash merges, #3103)
        if default_branch:
            try:
                cmp = json.loads(
                    github_ops._gh(
                        "api", f"repos/{repo_github}/compare/{default_branch}...{b}"
                    )
                )
                ahead = cmp.get("ahead_by") if isinstance(cmp, dict) else None
            except (RuntimeError, ValueError):
                ahead = None
            if ahead == 0:
                continue  # fully merged by ancestry → not an active claim
        kept.append(b)  # still ahead, or merged-ness undetermined → keep
    return kept
