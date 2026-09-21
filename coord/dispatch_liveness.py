"""#3376 deliverable A: ONE dispatch liveness precondition.

Four separate bugs (#3367, #3368, #3369, #3375), filed the same day, share
one root: coord committed a worker, a retry budget, or a verdict without
first checking whether the thing it was acting on was still real. This
module is the shared precondition that closes the dispatch half of that
pattern: it is called from the ONE place a work/test/review/smoke/fix
assignment is actually created — :func:`coord.dispatch.dispatch`, which
every dispatching caller funnels through regardless of stage — and refuses
before anything is spent when:

1. the issue is already **closed**, or
2. the branch has already **merged** into the target branch, or
3. the target machine **fails its credential/health probe** (the live
   probe itself is #3371 Part B — ``coord.network.claude_credential_
   reachable`` — this module only consumes its verdict).

Deliberately ONE function with three predicates, not three scattered
guards (#3376's own framing) — so the next instance of this pattern (a
fourth "did anyone check reality first" bug) has exactly one obvious place
to add a fourth predicate, instead of a fourth grep for call sites.

WHY A DISPATCH THAT NEVER HAPPENED COSTS NOTHING. `check_dispatch_liveness`
is consulted, and `coord.dispatch.DispatchRefused` raised, BEFORE
`coord.dispatch.dispatch()` does any worktree/HTTP work and before any
assignment row is created. There is therefore no assignment id, no
`num_turns`, no `cost_usd` for a retry-budget counter (`coord.drive.
DriveCounters`) to charge against. `DispatchRefused` is deliberately the
SAME exception #1844 already built for `enforce_oracle_readiness`/
`enforce_epic_dispatch_guard` — a `ValueError` subclass `coord drive`'s
subprocess boundary already maps to `coord.drive.EXIT_DISPATCH_REFUSED`
("deterministic, not worth retrying, doesn't consume the issue's ordinary
retry budget") rather than inventing a second, competing "this refusal
doesn't count" concept (#2096: "one question, one answer" — see the
2026-08-04/05 incident `EXIT_DISPATCH_REFUSED`'s own docstring cites,
which is the #1844 analogue of what #3376's charging rule is asking for).
So #3376's charging rule ("a dispatch refused by this precondition costs
the issue nothing") is a structural consequence of reusing that existing
exception and call path, not a separate accounting fix. The sibling half
of that rule — a dispatch that fails before turn 2 also costs nothing —
was already delivered by #3367's `coord.machine_fault` module for the
post-hoc case (a dispatch that WAS made and then failed instantly); this
module is the pre-hoc case, refusing before the dispatch is even made.

RECORDING, NOT SILENCE. "the resulting waste is invisible" is half of
#3376's title — a suppressed dispatch is information, and must be visible
the same way every other board mutation is: through `coord.audit.
record_audit` (the existing durable, best-effort, queryable audit trail —
#1036), not a new bespoke log file. `record_dispatch_refusal` is the one
place that happens, so a refusal always leaves exactly one row, in the one
place operators already know to look (`coord audit`).

OPT-IN, NO-OP BY DEFAULT — same shape as #3371's own `credential_fetcher`
and #3353's `status_fetcher`: every input defaults to `None` ("not probed"),
which refuses nothing. A caller that hasn't wired a fact source in yet is
byte-for-byte unaffected, exactly like every pre-#3371 caller of
`coord.dispatch.dispatch()` still is today.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from coord.audit import record_audit

if TYPE_CHECKING:
    from coord.config import Config

__all__ = [
    "LivenessRefusal",
    "check_dispatch_liveness",
    "record_dispatch_refusal",
    "github_issue_liveness_fetcher",
    "PREDICATE_ISSUE_CLOSED",
    "PREDICATE_BRANCH_MERGED",
    "PREDICATE_MACHINE_UNHEALTHY",
]

PREDICATE_ISSUE_CLOSED = "issue_closed"
PREDICATE_BRANCH_MERGED = "branch_merged"
PREDICATE_MACHINE_UNHEALTHY = "machine_unhealthy"

EVENT_DISPATCH_REFUSED = "dispatch_refused_liveness"


@dataclass(frozen=True)
class LivenessRefusal:
    """Why `check_dispatch_liveness` refused — one of the three predicates."""

    predicate: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"predicate": self.predicate, "reason": self.reason}


def check_dispatch_liveness(
    *,
    repo_name: str,
    issue_number: int,
    machine_name: str,
    issue_closed: bool | None = None,
    branch_merged: bool | None = None,
    machine_healthy: bool | None = None,
) -> LivenessRefusal | None:
    """The one function, three predicates. Returns a refusal, or `None`
    when dispatch may proceed.

    Every boolean defaults to `None` ("not probed / unknown") rather than
    `False` — a caller that never wired a fact source in should refuse
    nothing, the same "no evidence supplied at all is not a refusal"
    posture `coord.machine_fault.classify_machine_fault` documents for
    itself. Checked in the order #3376 lists them: closed, then merged,
    then machine health — the first true predicate wins and is reported;
    this function does not try to report more than one cause at a time,
    matching every other structural gate in `coord.dispatch.dispatch()`.
    """
    if issue_closed:
        return LivenessRefusal(
            predicate=PREDICATE_ISSUE_CLOSED,
            reason=(
                f"{repo_name}#{issue_number} is already closed — "
                "dispatching now cannot matter (#3376)"
            ),
        )
    if branch_merged:
        return LivenessRefusal(
            predicate=PREDICATE_BRANCH_MERGED,
            reason=(
                f"{repo_name}#{issue_number}'s branch has already merged "
                "into the target branch — dispatching now cannot matter "
                "(#3376)"
            ),
        )
    if machine_healthy is False:
        return LivenessRefusal(
            predicate=PREDICATE_MACHINE_UNHEALTHY,
            reason=(
                f"machine {machine_name!r} failed a live health/credential "
                "probe — not routable (#3371/#3376): re-authenticate "
                "(`claude` or `claude setup-token`) on that host, or "
                "approve/assign this to a different machine"
            ),
        )
    return None


def github_issue_liveness_fetcher(
    config: "Config",
) -> Callable[[str, int, str | None], tuple[bool, bool]]:
    """Build a REAL `(repo_name, issue_number, branch) -> (issue_closed,
    branch_merged)` fetcher, backed by live GitHub calls — the piece #3376
    review round 1 found missing: `check_dispatch_liveness`'s two new
    predicates existed and were unit-tested, but every actual dispatch
    chokepoint (`coord approve`, `coord assign`, `coord drive`'s WORK
    stage, the daemon auto-loop, the dashboard approve route, and the
    milestone/refine/new-issue/decomposition chat dispatchers /
    mock-author) passed `credential_fetcher` but never `issue_liveness_
    fetcher` — exactly the "mechanism that exists but nothing actually
    calls" gap #3371's own review round already found once for
    `credential_fetcher` (see `coord/commands/dispatch.py`'s and
    `coord/commands/dispatch_workers.py`'s comments at their own
    `credential_fetcher` wiring).

    Resolves `repo_name` (coordinator.yml's internal name) to `owner/repo`
    via *config* — every fetcher call inside `coord.dispatch.dispatch()`
    only ever hands this `(proposal.repo_name, proposal.issue_number,
    proposal.target_branch)`, so the GitHub-repo mapping has to happen
    inside the closure, not at the call site.

    issue_closed: `coord.github_ops.issue_is_closed` — one `gh` call.
    branch_merged: `coord.claim.any_matching_branch_merged` — #3436: when
    *branch* is given (the dispatch's actual target — e.g. `Assignment.
    branch`/`Proposal.target_branch`), asks whether THAT branch has merged
    and ignores every other `issue-{N}-*` sibling, so a zero-commit
    review-leg branch cut from the default-branch tip can no longer
    permanently refuse dispatch for the issue's real, unmerged work
    branch. `branch=None` (a caller with nothing dispatched yet) falls
    back to the original issue-scoped "any matching branch merged" check.
    Both fail open (`False`) on any GitHub/network hiccup — same "never
    refuse on evidence we don't have" posture `claude_credential_reachable`
    documents for the third predicate, and matching `issue_is_closed`'s/
    `pr_is_merged`'s own documented fail-open contracts.
    """

    def fetcher(
        repo_name: str, issue_number: int, branch: str | None = None
    ) -> tuple[bool, bool]:
        from coord import github_ops  # noqa: PLC0415
        from coord.claim import any_matching_branch_merged  # noqa: PLC0415

        repo_cfg = config.repo(repo_name)
        repo_github = repo_cfg.github if repo_cfg is not None else repo_name
        issue_closed = github_ops.issue_is_closed(repo_github, issue_number)
        branch_merged = any_matching_branch_merged(
            repo_github, issue_number, branch=branch
        )
        return issue_closed, branch_merged

    return fetcher


def record_dispatch_refusal(
    refusal: LivenessRefusal,
    *,
    repo_name: str,
    issue_number: int,
    machine_name: str,
    assignment_type: str,
) -> None:
    """Make a refusal visible. Best-effort (`record_audit` swallows its own
    failures) — a suppressed dispatch is information, but losing that one
    audit row must never be the reason the refusal itself failed to take
    effect.
    """
    record_audit(
        tier="business",
        category="dispatch",
        event_type=EVENT_DISPATCH_REFUSED,
        actor="coord.dispatch_liveness",
        summary=refusal.reason,
        repo=repo_name,
        issue=issue_number,
        machine=machine_name,
        details={
            "predicate": refusal.predicate,
            "assignment_type": assignment_type,
        },
    )
