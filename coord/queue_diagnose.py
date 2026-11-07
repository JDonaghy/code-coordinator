"""Phase 1 of #2235's queue-rescue plan (#2276) — the read-only diagnostician.

**What this is.**  Phase 0 (:mod:`coord.block_log`) records what the queue
*said* when it stopped.  #2235's load-bearing finding is that the queue was
wrong five times out of seven: *"stale test verdict"* where the truth was a
four-branch conflict, *"CI red, 2/2 attempts"* for a build that went green 23
minutes later.  Phase 0 cannot fix that on its own — it writes
``true_cause: ""`` on entry (nobody knows it yet) and, on exit, derives a
cause from *the release the queue itself performed*, which is still the queue
talking about itself.

So this module re-derives the blocker from **live state** — the PR's
lifecycle, ``gh pr view --json mergeable``, ``gh pr checks``, the live gate
decision ``coord gates`` renders, and the agent's ``/health`` — and writes the
answer back as the episode's ``true_cause``.  That column is what #2270's
report reads and what Phase 2 (#2268, still gated) would be scoped from.

**It mutates nothing.**  No board write, no queue write, no ``gh`` verb that
is not a read, no dispatch, no merge.  Its only output is a ``diagnosis``
record appended to the Phase-0 log — its own file, which no decision path
reads.  ``tests/test_block_log.py`` proves that byte-for-byte rather than
asserting it.

**Four rules it is built around.**

1. *The stated reason is an input to be contradicted, never a starting
   hypothesis.*  :func:`diagnose` derives a cause from the live readings
   alone; the stated reason is consulted only at the end, to ask whether the
   evidence still SUPPORTS it (:func:`_stated_still_supported`).  A
   diagnostician that anchors on the stated reason reproduces the
   five-of-seven failure it exists to correct.
2. *It does not own a clock.*  The trigger is #1632's stall detector, whose
   events :func:`stalled_keys` reads.  Two competing definitions of "stalled"
   is the defect class #1440 names, so there is deliberately no threshold, no
   age comparison and no ``now - x >`` anywhere in this module.
3. *``unknown`` is a first-class verdict.*  Thin evidence emits
   :data:`CAUSE_UNKNOWN` with :data:`CONFIDENCE_NONE`, and nothing penalises
   it.  A diagnostician that is confidently wrong is strictly worse than one
   that abstains, because Phase 2 would inherit the confidence.
4. *It has its own budget.*  :data:`MAX_DIAGNOSES_PER_EPISODE` bounds how
   often one stall may be re-diagnosed, so a diagnosis that cannot conclude
   cannot loop — the shape #2272 is about.

**Where it runs, and why not as a worker.**  It needs ``gh``, and ``gh`` is
denied to workers (#1483) — so it is emphatically *not* a ``coord assign``
worker.  It runs in-process on the coordinator host: hung off the notifier
tick inside ``coord serve`` (see :func:`coord.notifier.service.diagnose_pass`),
and on demand from ``coord drive-queue diagnose``.  Both are places that
already hold the operator's ``gh`` credentials and already read the board, so
Phase 1 adds no new trust boundary and no new credential.  A worker leg would
have needed one, for a task whose entire output is a log line.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from coord.drive_queue import QueueEntry, is_unreachable_wait_shape

_log = logging.getLogger(__name__)

__all__ = [
    "CAUSE_INTEGRATION_RED",
    "CAUSE_NEVER_DISPATCHED",
    "CAUSE_PROSE",
    "CAUSE_UNKNOWN",
    "CONFIDENCE_HIGH",
    "CONFIDENCE_LOW",
    "CONFIDENCE_MEDIUM",
    "CONFIDENCE_NONE",
    "MAX_DIAGNOSES_PER_EPISODE",
    "MAX_DIAGNOSES_PER_PASS",
    "CheckReading",
    "Diagnosis",
    "GhLiveProbe",
    "LiveProbe",
    "LiveState",
    "diagnose",
    "needs_diagnosis",
    "run_pass",
    "stalled_keys",
    "stated_family",
    "trigger_conditions",
]


# ── verdict vocabulary ───────────────────────────────────────────────────────
#
# Short slugs, because `coord.block_log.summarize` buckets on the text before
# the first " — ".  The prose after it is what an operator reads; the slug is
# what Phase 2's scoping would count.  Keep them stable: a renamed slug
# silently splits a bucket in the corpus.

CAUSE_PR_MERGED = "pr-merged"
CAUSE_PR_CLOSED = "pr-closed"
CAUSE_MERGE_CONFLICT = "merge-conflict"
CAUSE_DEAD_LEG = "dead-leg"
CAUSE_NO_PR = "no-pr"
CAUSE_CI_RED = "ci-red"
#: A `ci-red` row whose live checks are failing while the LATEST branch
#: Test-gate DECISION (`coord.gates`'s own "test" `GateDecision` — its
#: required/ok answer, not the raw `test_state` column, which can disagree
#: with it on a #1479-stale verdict) already reads green (`passed`) on this
#: branch alone. CI builds the PR's merge ref (branch + current base); the
#: Test stage builds the branch. When the two disagree the failure is a
#: property of the COMBINATION, not of this branch's own diff — a sibling
#: PR landing on the base while this one is in flight is the shape that
#: produces it, and a fix leg that never builds the merged tree stays green
#: on the branch forever. Never confused with plain `ci-red`: that cause
#: covers everything else, including a branch whose own Test verdict is
#: ALSO red (the regression is real and local, not an integration gap), or
#: merely `skipped` (a structural claim about the diff's shape, never a
#: claim the code behaved correctly at this SHA — see `_rule_integration_red`).
CAUSE_INTEGRATION_RED = "integration-red"
CAUSE_CI_PENDING = "ci-pending"
CAUSE_GATE_BLOCKED = "gate-blocked"
CAUSE_NOTHING_BLOCKING = "nothing-blocking"
CAUSE_CI_GREEN = "ci-green"
CAUSE_AGENT_UNREACHABLE = "agent-unreachable"
#: A `blocked`/`parked` row that has never been dispatched at all — no
#: branch, no PR, no merge-queue row — and so has structurally nothing for a
#: live probe to ever read, no matter how many times this diagnoses it. See
#: :func:`coord.drive_queue.is_unreachable_wait_shape`, the one predicate
#: this reuses rather than re-deriving (#2096: one question, one answer) —
#: the SAME shape `coord.drive_queue._reconcile_blocked_unreachable` resets
#: to `waiting` from the tick's own reconcile loop, surfaced here so a row
#: whose tick-side reset is itself bounded (`MAX_BLOCKED_RESUMES`) does not
#: also fall off `coord drive-queue sweep`'s own radar.
#:
#: Two coverage gaps this does NOT close, by design rather than oversight:
#: `is_unreachable_wait_shape` unconditionally excludes a row whose
#: `last_reason` still names an unsatisfiable `after=` verdict — that shape
#: is `_reconcile_blocked_after`'s alone to resolve (#2978), so if that
#: function ever declines for an unrelated reason (a stale board cache, a
#: missing `live_prereq_terminal`), the row is blocked with no surface here
#: either. And an episode that already spent its `MAX_DIAGNOSES_PER_EPISODE`
#: budget recorded as `unknown` stays `unknown` — `needs_diagnosis` refuses
#: to re-probe it — so only new or unspent episodes reach this cause.
CAUSE_NEVER_DISPATCHED = "never-dispatched"
CAUSE_UNKNOWN = "unknown"

CAUSE_PROSE: dict[str, str] = {
    CAUSE_PR_MERGED: "the PR merged while the entry sat stalled, so whatever the queue said had outlived its subject",
    CAUSE_PR_CLOSED: "the PR was closed while the entry sat stalled, so the queue is waiting on work that no longer exists",
    CAUSE_MERGE_CONFLICT: "GitHub reports the PR as conflicting; the branch needs a rebase before anything else can be true",
    CAUSE_DEAD_LEG: "the entry names a session the machine that launched it no longer has — the leg is dead, not slow",
    CAUSE_NO_PR: "no PR exists for this entry, so no CI-shaped or merge-shaped reason can be the real blocker",
    CAUSE_CI_RED: "a required check is failing right now",
    CAUSE_INTEGRATION_RED: (
        "GitHub Actions CI is failing on the PR's merge ref, but this "
        "branch's own latest Test-gate verdict read green — the failure "
        "lives in the combination with the current base, not in this "
        "branch's own diff; a fix leg must build the merged tree, not "
        "just the branch, to reproduce it"
    ),
    CAUSE_CI_PENDING: "checks are still running; nothing has actually failed yet",
    CAUSE_GATE_BLOCKED: "a merge gate is genuinely refusing right now",
    CAUSE_NOTHING_BLOCKING: "no live blocker exists — the gates read clear, checks are green and the PR is mergeable; the queue is stalled on a reading that has since gone stale",
    CAUSE_CI_GREEN: "every check has reported success; the CI-shaped reason no longer describes anything live",
    CAUSE_AGENT_UNREACHABLE: "the machine that owns this entry is not answering /health",
    CAUSE_NEVER_DISPATCHED: "this row has no branch, no PR and no merge-queue row behind it — it was never dispatched, so no amount of re-probing a merge gate can ever learn anything new; resetting it to waiting re-runs nothing that already ran",
    CAUSE_UNKNOWN: "evidence was too thin to name a cause, and a guess here would poison the corpus Phase 2 is scoped from",
}

#: Lower is more confident.  Rendered verbatim into the record.
CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"
#: Reserved for :data:`CAUSE_UNKNOWN`.  Not "very low" — an abstention carries
#: no confidence at all, and giving it a number invites averaging it with one.
CONFIDENCE_NONE = "none"

#: How many times one stall episode may be diagnosed before Phase 1 gives up
#: on it.  #2272's shape: a diagnosis that cannot conclude must not become a
#: loop that re-shells `gh` every notifier tick for the life of the stall.
#: Three is one first look plus two retries — enough for a check that was
#: pending to report, not enough to matter if it never does.
MAX_DIAGNOSES_PER_EPISODE = 3

#: How many entries one pass may diagnose.  The notifier tick is on `coord
#: serve`'s 30 s hot path and each diagnosis is several `gh` round trips, so
#: the pass is bounded per tick as well as per episode.  Nothing is dropped —
#: the entries beyond the cap are simply diagnosed on the next tick, and
#: :func:`run_pass` reports the deferral rather than truncating silently.
MAX_DIAGNOSES_PER_PASS = 4


# ── live readings ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CheckReading:
    """One row of ``gh pr checks``, reduced to what a verdict needs."""

    name: str
    #: ``success`` | ``failure`` | ``pending`` | ``unknown``.  ``unknown`` is
    #: NOT folded into ``failure``: #1525's fail-closed rule is about whether
    #: to *merge*, and this module never merges.  Calling an unreadable check
    #: a failure here would manufacture a `ci-red` verdict out of a `gh`
    #: hiccup, which is exactly the confidently-wrong diagnosis Phase 2 must
    #: not inherit.
    conclusion: str = "unknown"


@dataclass(frozen=True)
class LiveState:
    """Everything Phase 1 is allowed to look at, all of it read-only.

    Every field is optional and every ``None`` means *"we could not tell"* —
    never *"no"*.  That distinction is the whole reason :data:`CAUSE_UNKNOWN`
    exists: a probe that failed and a probe that answered in the negative lead
    to opposite verdicts, and collapsing them is how a diagnostician starts
    inventing causes out of network errors.
    """

    pr_number: int | None = None
    #: ``OPEN`` / ``MERGED`` / ``CLOSED``, or ``""`` when unread.
    pr_state: str = ""
    #: GitHub's own ``mergeable`` verdict.  ``None`` = still computing, or the
    #: read failed.
    mergeable: bool | None = None
    #: ``None`` (not read) is distinct from ``()`` (read, and the PR has no
    #: checks) — the second is evidence, the first is not.
    checks: tuple[CheckReading, ...] | None = None
    #: The live merge-gate decision, as ``coord gates`` renders it.
    gate_ready: bool | None = None
    gate_blockers: tuple[str, ...] = ()
    #: Did the owning machine answer ``/health``?
    agent_reachable: bool | None = None
    #: Does that machine still have the tmux session this entry names?
    agent_has_session: bool | None = None
    #: Machine-level CRITs, verbatim from ``/health``.
    machine_crits: tuple[str, ...] = ()
    #: Probes that raised.  Non-empty means the picture is partial, and every
    #: verdict below high confidence is downgraded accordingly.
    probe_errors: tuple[str, ...] = ()
    #: The LATEST Test-gate DECISION for this issue's own branch — read off
    #: ``coord gates``'s own "test" :class:`~coord.gates.GateDecision`
    #: (:func:`coord.gates.build_gate_report`), never off the raw
    #: ``test_state`` column directly. ``"passed"`` | ``"skipped"`` only
    #: when the decision itself reads ``ok`` (genuinely satisfied, or not
    #: required at all); ``"failed"`` whenever the decision is ``required``
    #: and NOT ``ok`` — including a #1479-STALE refusal, where the raw
    #: ``test_state`` column can still read ``"passed"`` even though
    #: ``coord merge`` is actively refusing this exact branch right now.
    #: Trusting the raw column there would misreport a refusing gate as
    #: green. ``None`` covers every case that is NOT a confirmed decision
    #: either way: the read failed, no Test gate applies to this issue at
    #: all, or no smoke verdict has ever been recorded — never collapsed
    #: into either verdict, same posture as every other optional field on
    #: this class.
    branch_test_state: str | None = None
    #: ``True`` only when ``coord gates``'s own board read
    #: (:func:`coord.gates.build_gate_report`) completed WITHOUT raising and
    #: came back having found zero assignments at all for this issue — a
    #: positive, board-derived observation, not an absence of one.  ``None``
    #: means that read did not happen or raised (so this says nothing);
    #: ``False`` means it ran and found at least one assignment, even if
    #: that assignment has no branch recorded yet (dispatched, just not
    #: pushed). Only a confirmed ``True`` may ever upgrade a verdict.
    no_assignment_ever: bool | None = None

    @property
    def read_anything(self) -> bool:
        """True when at least one probe came back with something usable."""
        return (
            bool(self.pr_state)
            or self.mergeable is not None
            or self.checks is not None
            or self.gate_ready is not None
            or self.agent_reachable is not None
        )

    def failing_checks(self) -> tuple[CheckReading, ...]:
        return tuple(c for c in (self.checks or ()) if c.conclusion == "failure")

    def pending_checks(self) -> tuple[CheckReading, ...]:
        return tuple(c for c in (self.checks or ()) if c.conclusion == "pending")

    def all_checks_green(self) -> bool:
        return bool(self.checks) and all(
            c.conclusion == "success" for c in (self.checks or ())
        )


# ── evidence rendering (pure) ────────────────────────────────────────────────


def _checks_line(live: LiveState) -> str:
    if live.checks is None:
        return "gh pr checks: not read"
    if not live.checks:
        return "gh pr checks: no checks on this PR"
    buckets: dict[str, list[str]] = {}
    for check in live.checks:
        buckets.setdefault(check.conclusion, []).append(check.name)
    parts = [
        f"{len(names)} {bucket} ({', '.join(sorted(names)[:3])})"
        for bucket, names in sorted(buckets.items())
    ]
    return f"gh pr checks: {len(live.checks)} check(s) — " + "; ".join(parts)


def _pr_line(live: LiveState) -> str:
    if live.pr_number is None and not live.pr_state:
        return "gh pr view: no PR resolved for this entry"
    mergeable = (
        "unknown" if live.mergeable is None else ("yes" if live.mergeable else "NO")
    )
    number = "?" if live.pr_number is None else f"#{live.pr_number}"
    return (
        f"gh pr view {number}: state={live.pr_state or 'unknown'} "
        f"mergeable={mergeable}"
    )


def _gate_line(live: LiveState) -> str:
    if live.gate_ready is None:
        return "coord gates: not read"
    if live.gate_ready:
        return "coord gates: every required gate reads clear"
    blockers = ", ".join(live.gate_blockers) or "reason not reported"
    return f"coord gates: blocked — {blockers}"


def _branch_test_line(live: LiveState) -> str:
    """Rendered unconditionally (never only inside `_rule_integration_red`'s
    own decisive string) so an operator reading a `ci-red` diagnosis can see
    what the branch's own Test-gate decision read — the one piece of
    evidence that answers "why didn't this fire as `integration-red`"."""
    if live.branch_test_state is None:
        return "coord gates (test decision): not read, or no Test gate on this issue"
    return f"coord gates (test decision): branch Test verdict reads {live.branch_test_state!r}"


def _health_line(entry: QueueEntry, live: LiveState) -> str:
    machine = entry.machine or entry.launch_host or "unassigned"
    if live.agent_reachable is None:
        return f"/health {machine}: not read"
    if not live.agent_reachable:
        return f"/health {machine}: NOT ANSWERING"
    bits = ["reachable"]
    if live.agent_has_session is not None:
        bits.append(
            f"session {entry.session_name or '(none named)'}: "
            + ("present" if live.agent_has_session else "ABSENT")
        )
    if live.machine_crits:
        bits.append("CRIT: " + ", ".join(live.machine_crits))
    return f"/health {machine}: " + "; ".join(bits)


def _evidence(entry: QueueEntry, live: LiveState) -> list[str]:
    """Every reading, in a fixed order, whether or not it was decisive.

    Fixed order and unconditional inclusion on purpose: the record is meant
    to be diffable across two weeks of episodes, and a list that omits the
    probes that came back empty makes "we did not look" indistinguishable
    from "we looked and there was nothing".
    """
    lines = [
        _pr_line(live),
        _checks_line(live),
        _gate_line(live),
        _branch_test_line(live),
        _health_line(entry, live),
    ]
    lines.extend(f"probe failed: {err}" for err in live.probe_errors)
    return lines


# ── the stated reason, as something to be contradicted ───────────────────────
#
# Coarse families only.  The point is not to parse the queue's prose — it is
# to ask ONE question of each family: does the live state still support it?
# Anything finer would be a second taxonomy to keep in sync with
# `_RESUME_CAUSES`, and anything that fed the family back into cause
# selection would be the anchoring bug this module exists to avoid.

FAMILY_CI = "ci"
FAMILY_CONFLICT = "conflict"
FAMILY_GATE = "gate"
FAMILY_DISPATCH = "dispatch"
FAMILY_DEPENDENCY = "dependency"

#: Ordered: the first family whose marker appears wins, so the more specific
#: conflict/dispatch wording is tested before the broad CI vocabulary.
#:
#: Matched on WORD BOUNDARIES, not as substrings.  Bare `in` misfires on
#: exactly the wording this fleet uses — "review requi**red** but not
#: approved" contains "red" and would classify a review-gate refusal as a CI
#: claim, which then reads as a contradiction when the live gate agrees with
#: it.  A misclassified family produces a false contradiction, and a false
#: contradiction is the confidently-wrong verdict #2276 exists to avoid.
_FAMILY_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (FAMILY_CONFLICT, ("conflict", "conflicting", "not mergeable", "rebase", "diverged")),
    (FAMILY_DISPATCH, ("dispatch", "launch", "tmux", "no session", "session died")),
    (FAMILY_DEPENDENCY, ("after", "waiting on", "depends on", "pre-req", "prereq")),
    (
        FAMILY_CI,
        ("ci", "check", "checks", "checks_failed", "test verdict", "smoke", "red",
         "failing", "flake"),
    ),
    (FAMILY_GATE, ("gate", "review", "approval", "human_required", "verdict")),
)

_FAMILY_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = tuple(
    (family, re.compile(r"\b(?:" + "|".join(re.escape(m) for m in markers) + r")\b"))
    for family, markers in _FAMILY_MARKERS
)


def stated_family(reason: str) -> str:
    """Which coarse claim the queue's stated reason makes, or ``""``.

    ``""`` means the reason is prose this module cannot classify — in which
    case it never claims a contradiction.  Asserting that live state
    contradicts a sentence you did not understand is a confident-wrong
    verdict wearing a different hat.
    """
    lowered = " ".join(reason.lower().split())
    if not lowered:
        return ""
    for family, pattern in _FAMILY_PATTERNS:
        if pattern.search(lowered):
            return family
    return ""


def _stated_still_supported(family: str, live: LiveState) -> bool | None:
    """Does the live state still back the stated reason?  ``None`` = can't tell.

    Deliberately independent of which cause :func:`diagnose` picked.  A
    contradiction is a statement about the STATED reason and the evidence,
    not about the ranking of our own rules — deriving it from the chosen
    cause would make the two mutually reinforcing, which is how a
    diagnostician talks itself into confidence it has not earned.
    """
    if live.pr_state in ("MERGED", "CLOSED"):
        # Nothing about a finished PR can still be blocking the queue.
        return False if family else None
    if family == FAMILY_CI:
        if live.pr_state == "none":
            return False  # no PR, therefore no checks, therefore not this
        if live.checks is None:
            return None
        return bool(live.failing_checks() or live.pending_checks())
    if family == FAMILY_CONFLICT:
        if live.pr_state == "none":
            return False  # nothing to conflict with
        return None if live.mergeable is None else not live.mergeable
    if family == FAMILY_GATE:
        return None if live.gate_ready is None else not live.gate_ready
    if family == FAMILY_DISPATCH:
        if live.agent_reachable is None:
            return None
        if not live.agent_reachable:
            return True
        return None if live.agent_has_session is None else not live.agent_has_session
    # FAMILY_DEPENDENCY and "" — nothing here reads the queue's own graph, so
    # no honest claim is available either way.
    return None


# ── the rules ────────────────────────────────────────────────────────────────
#
# Ordered strongest-evidence-first, and every rule returns the reading that
# DECIDED it so the record can put that line at the top of the evidence.
# `coord.notifier.models.CONDITION_ORDER` is the same convention: rank the
# probes, never average them.


def _rule_pr_merged(entry: QueueEntry, live: LiveState) -> tuple[str, str, str] | None:
    if live.pr_state == "MERGED":
        return CAUSE_PR_MERGED, CONFIDENCE_HIGH, _pr_line(live)
    return None


def _rule_pr_closed(entry: QueueEntry, live: LiveState) -> tuple[str, str, str] | None:
    if live.pr_state == "CLOSED":
        return CAUSE_PR_CLOSED, CONFIDENCE_HIGH, _pr_line(live)
    return None


def _rule_conflict(entry: QueueEntry, live: LiveState) -> tuple[str, str, str] | None:
    if live.mergeable is False:
        return CAUSE_MERGE_CONFLICT, CONFIDENCE_HIGH, _pr_line(live)
    return None


def _probe_confirms_never_dispatched(live: LiveState) -> bool:
    """Did the probe positively confirm, via a successful board read, that
    this issue has no assignment at all — as opposed to a probe that merely
    failed to find a branch for an unrelated reason (an exception, or an
    assignment that exists but has no branch recorded yet)?

    Reads `live.no_assignment_ever` rather than scanning `probe_errors` for
    text: `GhLiveProbe._gates` catches EVERY exception `build_gate_report`
    raises and still appends a distinct `coord gates: ...` error, after
    which `GhLiveProbe._github` — handed the now-empty branch — appends its
    own `gh: no branch known for ...` line too. Both strings land in
    `probe_errors` on a plain timeout/rate-limit, so a string match alone
    cannot tell that apart from a genuine "never dispatched" reading.
    """
    return live.no_assignment_ever is True


def _rule_dead_leg(entry: QueueEntry, live: LiveState) -> tuple[str, str, str] | None:
    # Diagnosing this shape is in scope; FIXING it is not (#2276 explicitly
    # leaves the deterministic liveness check to its own issue — mechanism
    # before agent).  So this rule names it and stops.
    if entry.session_name and live.agent_reachable and live.agent_has_session is False:
        return CAUSE_DEAD_LEG, CONFIDENCE_HIGH, _health_line(entry, live)
    return None


def _rule_no_pr(entry: QueueEntry, live: LiveState) -> tuple[str, str, str] | None:
    # Only when the PR probe actually ran: `pr_number is None` with no state
    # and no successful lookup is "we did not look", which is `unknown`.
    if live.pr_number is None and live.pr_state == "none":
        return CAUSE_NO_PR, CONFIDENCE_MEDIUM, _pr_line(live)
    return None


def _rule_integration_red(entry: QueueEntry, live: LiveState) -> tuple[str, str, str] | None:
    """A live-failing check whose branch Test verdict is already green is a
    property of the MERGE, not the branch — see
    :data:`CAUSE_INTEGRATION_RED`'s docstring. Checked strictly BEFORE
    :func:`_rule_ci_red` in :data:`_RULES` so the more specific cause wins
    whenever both would otherwise match; a branch whose own Test verdict
    also read red falls through to plain ``ci-red`` unchanged.

    Restricted to ``"passed"`` alone — deliberately excludes ``"skipped"``.
    A skipped Test is a structural claim about the SHAPE of the diff
    ("contract/fixture-only, nothing to smoke-test"), never a claim that
    code behaved correctly at this SHA (see
    ``coord.merge_queue.evaluate_smoke_verdict``'s own docstring), so there
    is no confirmed green half to split the CI failure away from. Worse, a
    ``baseline_red``-confirmed skip means the BASE is what's broken —
    relabeling that ``integration-red`` would steer it away from
    ``coord.board_doctor.CAUSE_BASELINE_RED``, the cause that actually
    names it.
    """
    failing = live.failing_checks()
    if not failing or live.branch_test_state != "passed":
        return None
    names = ", ".join(sorted(c.name for c in failing)[:3])
    return (
        CAUSE_INTEGRATION_RED,
        CONFIDENCE_HIGH,
        f"gh pr checks: failing — {names} — but this branch's own latest "
        f"Test-gate verdict is {live.branch_test_state!r}",
    )


def _rule_ci_red(entry: QueueEntry, live: LiveState) -> tuple[str, str, str] | None:
    failing = live.failing_checks()
    if failing:
        names = ", ".join(sorted(c.name for c in failing)[:3])
        return CAUSE_CI_RED, CONFIDENCE_HIGH, f"gh pr checks: failing — {names}"
    return None


def _rule_ci_pending(entry: QueueEntry, live: LiveState) -> tuple[str, str, str] | None:
    pending = live.pending_checks()
    if pending:
        names = ", ".join(sorted(c.name for c in pending)[:3])
        return CAUSE_CI_PENDING, CONFIDENCE_MEDIUM, f"gh pr checks: still running — {names}"
    return None


def _rule_gate_blocked(entry: QueueEntry, live: LiveState) -> tuple[str, str, str] | None:
    if live.gate_ready is False:
        return CAUSE_GATE_BLOCKED, CONFIDENCE_MEDIUM, _gate_line(live)
    return None


def _rule_nothing_blocking(
    entry: QueueEntry, live: LiveState
) -> tuple[str, str, str] | None:
    # #2230's shape, stated positively: every gate this fleet has reads clear,
    # and the entry is stalled anyway.  It needs ALL THREE readings — a
    # "nothing is wrong" verdict off one probe is the least defensible claim
    # in the vocabulary.
    if live.gate_ready and live.all_checks_green() and live.mergeable is True:
        return (
            CAUSE_NOTHING_BLOCKING,
            CONFIDENCE_MEDIUM,
            f"{_gate_line(live)}; {_checks_line(live)}; {_pr_line(live)}",
        )
    return None


def _rule_ci_green(entry: QueueEntry, live: LiveState) -> tuple[str, str, str] | None:
    if live.all_checks_green():
        return CAUSE_CI_GREEN, CONFIDENCE_MEDIUM, _checks_line(live)
    return None


def _rule_agent_unreachable(
    entry: QueueEntry, live: LiveState
) -> tuple[str, str, str] | None:
    if live.agent_reachable is False:
        return CAUSE_AGENT_UNREACHABLE, CONFIDENCE_MEDIUM, _health_line(entry, live)
    return None


_RULES = (
    _rule_pr_merged,
    _rule_pr_closed,
    _rule_conflict,
    _rule_dead_leg,
    _rule_no_pr,
    # #3882: integration-red must be tried before the generic ci-red rule
    # below, or a branch whose own Test verdict is clean would always be
    # misclassified as a plain (local) regression.
    _rule_integration_red,
    _rule_ci_red,
    _rule_ci_pending,
    _rule_gate_blocked,
    # #2276 review: this sits ABOVE `nothing-blocking` deliberately.  Every
    # rule above it names a positive blocker (a conflict, a red check, a
    # refused gate) and is the more actionable answer even on a machine that
    # is down.  `nothing-blocking` is the opposite: it means "we looked and
    # found no reason", and "the machine that owns this entry is not
    # answering" IS a reason — reporting "nothing blocking" over a known-down
    # agent is precisely the confidently-wrong verdict Phase 1 exists to
    # avoid.  (`agent_reachable is False` is a positive reading; `None` —
    # never polled, or stale — falls straight through.)
    _rule_agent_unreachable,
    _rule_nothing_blocking,
    _rule_ci_green,
)


# ── the diagnosis ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Diagnosis:
    """One entry's derived cause, and everything it rests on."""

    key: str
    state: str
    stated_reason: str
    cause: str
    confidence: str
    evidence: tuple[str, ...] = ()
    #: True only when the evidence positively RULES OUT the stated reason —
    #: #2235's whole point, and the number to watch.
    contradicts_stated: bool = False
    #: Which notifier condition triggered this look (#1632's vocabulary,
    #: reused verbatim so the corpus can be joined back to the detector).
    trigger: str = ""

    @property
    def true_cause(self) -> str:
        """``slug — prose``, the shape :func:`coord.block_log.summarize` buckets."""
        return f"{self.cause} — {CAUSE_PROSE.get(self.cause, '')}".rstrip(" —")

    @property
    def abstained(self) -> bool:
        return self.cause == CAUSE_UNKNOWN


def diagnose(
    entry: QueueEntry,
    live: LiveState,
    *,
    trigger: str = "",
) -> Diagnosis:
    """Derive *entry*'s real blocker from *live*.  **Pure — no I/O, no clock.**

    The stated reason is read exactly once, at the end, and only to ask
    whether the evidence still supports it.  It never selects, orders or
    breaks ties between causes.
    """
    verdict: tuple[str, str, str] | None = None
    for rule in _RULES:
        verdict = rule(entry, live)
        if verdict is not None:
            break

    evidence = _evidence(entry, live)
    if verdict is None:
        cause, confidence = CAUSE_UNKNOWN, CONFIDENCE_NONE
    else:
        cause, confidence, decisive = verdict
        # Decisive reading first; the rest still ride along, deduped.
        evidence = [decisive] + [line for line in evidence if line != decisive]
        if live.probe_errors and confidence == CONFIDENCE_MEDIUM:
            # A partial picture cannot support a medium claim.  High-confidence
            # rules are exempt: `state=MERGED` and `mergeable=false` are single
            # authoritative fields, and a second probe failing does not make
            # them less true.
            confidence = CONFIDENCE_LOW

    if not live.read_anything:
        # Nothing came back at all.  Even a rule that fired on a default would
        # be reading its own assumptions back out; abstain instead.
        cause, confidence = CAUSE_UNKNOWN, CONFIDENCE_NONE

    # A cause still sitting at CAUSE_UNKNOWN here — whether from the
    # abstention just above, or because no `_RULES` entry matched a readable
    # picture — gets one more chance to be upgraded to a NAMED,
    # high-confidence cause, never downgraded from one. Deliberately not
    # narrowed to the abstention branch alone: "readable, but nothing in
    # `_RULES` matched" is exactly what a never-dispatched row looks like —
    # there is no PR state, no mergeable verdict and no checks for any rule
    # to key off, yet `agent_reachable` can still be read (that probe does
    # not depend on this issue having a branch at all), which would make
    # `read_anything` true despite nothing being dispatched.
    #
    # `_probe_confirms_never_dispatched` is gated on `live.no_assignment_ever`
    # — a POSITIVE board-derived observation (coord gates' own read ran
    # without raising and found zero assignments), never an absence of one.
    # It cannot fire on a `coord gates` exception, a timeout, or any other
    # reason the picture could be thin, which is exactly what tells this
    # apart from a probe that merely failed.
    #
    # Paired with `is_unreachable_wait_shape` (the SAME predicate
    # `coord.drive_queue._reconcile_blocked_unreachable` already trusts for
    # "nothing here will ever answer a live probe" — #2096, one question,
    # one answer) rather than `entry.attempts == 0` alone, because attempts
    # alone is not sufficient: a successful dispatch never increments it
    # either, so a row with a real, in-flight branch could misreport.  A row
    # already excluded there — a permanent block, or `_resolve_prereqs`'s
    # own unsatisfiable `after=` verdict, which `_reconcile_blocked_after`
    # alone resolves — is excluded here too.
    if (
        cause == CAUSE_UNKNOWN
        and is_unreachable_wait_shape(entry)
        and _probe_confirms_never_dispatched(live)
    ):
        cause, confidence = CAUSE_NEVER_DISPATCHED, CONFIDENCE_HIGH

    family = stated_family(entry.last_reason)
    supported = _stated_still_supported(family, live)
    return Diagnosis(
        key=entry.key,
        state=entry.state,
        stated_reason=entry.last_reason,
        cause=cause,
        confidence=confidence,
        evidence=tuple(evidence),
        contradicts_stated=supported is False,
        trigger=trigger,
    )


# ── probing (the only impure part) ───────────────────────────────────────────


def _row_reachable(row: Mapping[str, Any]) -> bool | None:
    """Is the machine this ``machine_health`` row describes answering?

    #2276 review: there is no ``reachable`` key on these rows and never was.
    Both producers — :func:`coord.health.aggregate.local_fleet_health_block`
    (the CLI path) and ``FleetHealthRefresher.snapshot().to_dict()`` (the
    notifier tick) — build rows via
    :func:`coord.health.fleet_snapshot._machine_health_rows`, whose
    reachability signal is ``state`` (``coord.network``'s ``online`` /
    ``offline`` / ``timeout`` / ``dns_error`` / ``http_error`` /
    ``rate_limited`` / ``unknown``).  Reading a key that does not exist made
    every configured machine read "reachable", which is worse than useless
    here: it left ``agent-unreachable`` structurally unreachable AND let
    ``_rule_dead_leg`` fire at high confidence on a machine that is merely
    down, which is the "confidently wrong" failure mode Phase 1 exists to
    avoid.

    Three answers, and the abstention is load-bearing:

    * ``None`` — no usable reading.  ``unknown`` (never polled, or an
      unclassifiable error), a missing/blank ``state``, or a ``stale`` row
      (nothing has polled inside ``STALE_AFTER_SECONDS``, i.e. no tick loop
      is running on this host).  Stale is deliberately ``None`` for a
      *non-online* state too: an "offline" reading from an hour ago is not
      evidence about now, in either direction.
    * ``True`` — a fresh ``online``.
    * ``False`` — a fresh, recognised, non-online state.
    """
    from coord import network  # noqa: PLC0415

    state = str(row.get("state") or "").strip().lower()
    if not state or state == network.UNKNOWN:
        return None
    if row.get("stale"):
        return None
    return state == network.ONLINE


class LiveProbe(Protocol):
    """Gathers :class:`LiveState` for one entry.  Read-only, by contract."""

    def probe(self, entry: QueueEntry) -> LiveState:  # pragma: no cover - protocol
        ...


@dataclass
class GhLiveProbe:
    """The real probe: ``gh`` reads, the live gate report, and ``/health``.

    Every call it makes is a read.  There is no ``gh pr merge``, no
    ``gh pr comment``, no board write and no queue write anywhere below this
    line, and ``tests/test_block_log.py`` asserts that against a seeded board
    rather than trusting this paragraph.

    Each probe is individually wrapped: one failing ``gh`` call costs its own
    field (which then reads ``None`` — *"could not tell"*, never *"no"*) and
    is recorded in ``probe_errors``, so a partial picture degrades the
    confidence of the verdict instead of silently faking a complete one.
    """

    config: Any = None
    board: Any = None
    #: The notifier tick's own ``/health`` fold, passed straight through — see
    #: `coord.notifier.collect.fleet_crits` for the shape.  Phase 1 never
    #: issues its own health round trip: the tick that triggered it just did.
    fleet_health: Mapping[str, Any] | None = None
    #: Session names the local host can see, when the caller knows them.
    #: ``None`` disables the dead-leg rule rather than guessing.
    live_sessions: frozenset[str] | None = None
    #: Which host ``live_sessions`` was read on (short hostname, lowercased).
    #: ``live_sessions`` is always a LOCAL ``tmux`` read, so an entry whose
    #: ``launch_host`` names a DIFFERENT machine is invisible to it and its
    #: absence there is not evidence — #1870, the same rule
    #: ``coord.drive_queue._reconcile_running`` already enforces.  ``''``
    #: means the caller did not say, which keeps the pre-#1870 behaviour.
    local_host: str = ""

    def probe(self, entry: QueueEntry) -> LiveState:
        errors: list[str] = []
        branch, gate_ready, gate_blockers, no_assignment_ever, branch_test_state = (
            self._gates(entry, errors)
        )
        pr_number, pr_state, mergeable, checks = self._github(entry, branch, errors)
        reachable, has_session, crits = self._health(entry)
        return LiveState(
            pr_number=pr_number,
            pr_state=pr_state,
            mergeable=mergeable,
            checks=checks,
            gate_ready=gate_ready,
            gate_blockers=tuple(gate_blockers),
            agent_reachable=reachable,
            agent_has_session=has_session,
            machine_crits=tuple(crits),
            probe_errors=tuple(errors),
            no_assignment_ever=no_assignment_ever,
            branch_test_state=branch_test_state,
        )

    # -- `coord gates`, verbatim ------------------------------------------
    def _gates(
        self, entry: QueueEntry, errors: list[str]
    ) -> tuple[str, bool | None, list[str], bool | None, str | None]:
        if self.board is None or self.config is None:
            return "", None, [], None, None
        try:
            from coord import github_ops  # noqa: PLC0415
            from coord.gates import build_gate_report  # noqa: PLC0415

            report = build_gate_report(
                self.board, self.config, entry.repo, entry.issue, gh_ops=github_ops
            )
        except Exception as exc:  # noqa: BLE001 — a probe, never a decision
            errors.append(f"coord gates: {type(exc).__name__}: {exc}")
            return "", None, [], None, None
        # `build_gate_report` returns early, with `report.rows` empty, ONLY
        # when `assignments_for_issue` found nothing on the board for this
        # issue at all (coord/gates.py) — never for an assignment that
        # exists but merely lacks a branch yet (that leaves `rows`
        # populated and `report.branch` empty instead). This is why
        # `no_assignment_ever` is read off `report.rows`, not off
        # `report.branch` or an error string: it is true only on the one
        # code path that is a genuine, successful "nothing was ever
        # dispatched" reading.
        no_assignment_ever = not report.rows
        # The branch's own Test-gate DECISION — read off `coord gates`'s
        # UNFILTERED decisions (not the `required`-only list just below) so
        # a repo with no Test gate configured at all leaves this `None`
        # rather than reading as either "passed" or "failed". Keyed off
        # `GateDecision.assignment_id` -> the matching `AssignmentGateRow`
        # (coord/gates.py: `test_decision.assignment_id` is set on BOTH the
        # pass and the refusal path), never re-derived from a second board
        # read — one question, one answer (#2096).
        #
        # The decision's own `required`/`ok` is read FIRST and is
        # authoritative: `coord/gates.py` can refuse a verdict as
        # #1479-STALE (the recorded SHA moved) while the raw `test_state`
        # column still reads "passed" — exactly the shape a branch lands in
        # right after its own push. Trusting the raw column in that case
        # would read a refusing gate as green. The raw column is consulted
        # only once the decision itself already says "ok", to tell
        # "passed" apart from "skipped".
        branch_test_state: str | None = None
        test_decision = next(
            (d for d in report.decisions if d.gate == "test"), None
        )
        if test_decision is not None and test_decision.assignment_id:
            if test_decision.required and not test_decision.ok:
                # Actively refusing right now (stale, missing, or a
                # genuinely failing verdict) — never read as green no
                # matter what the raw column says.
                branch_test_state = "failed"
            else:
                row = next(
                    (
                        r
                        for r in report.rows
                        if r.assignment_id == test_decision.assignment_id
                    ),
                    None,
                )
                if row is not None:
                    branch_test_state = row.test_state
        decisions = [d for d in report.decisions if getattr(d, "required", False)]
        if not decisions:
            return str(report.branch or ""), None, [], no_assignment_ever, branch_test_state
        blockers = [
            f"{d.gate}: {d.reason or 'refused'}" for d in decisions if not d.ok
        ]
        return (
            str(report.branch or ""), not blockers, blockers, no_assignment_ever,
            branch_test_state,
        )

    # -- `gh pr view` / `gh pr checks` ------------------------------------
    def _github(
        self, entry: QueueEntry, branch: str, errors: list[str]
    ) -> tuple[int | None, str, bool | None, tuple[CheckReading, ...] | None]:
        slug = self._github_slug(entry.repo)
        if not slug or not branch:
            if not slug:
                errors.append(f"gh: no github slug configured for repo {entry.repo!r}")
            else:
                errors.append(f"gh: no branch known for {entry.key}")
            return None, "", None, None

        from coord import github_ops  # noqa: PLC0415

        pr_state = ""
        try:
            pr_state = github_ops.get_pr_state_for_branch(slug, branch) or "none"
        except Exception as exc:  # noqa: BLE001
            errors.append(f"gh pr view: {type(exc).__name__}: {exc}")

        pr_number: int | None = None
        mergeable: bool | None = None
        if pr_state == "OPEN":
            try:
                found = github_ops.find_pr_for_branch(slug, branch) or {}
                pr_number = int(found.get("number")) if found.get("number") else None
            except Exception as exc:  # noqa: BLE001
                errors.append(f"gh pr list: {type(exc).__name__}: {exc}")
            if pr_number is not None:
                try:
                    mergeable = github_ops.check_pr_mergeable(slug, pr_number)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"gh pr view --json mergeable: {type(exc).__name__}: {exc}")

        checks: tuple[CheckReading, ...] | None = None
        if pr_number is not None:
            checks = self._checks(slug, pr_number, errors)
        return pr_number, pr_state, mergeable, checks

    @staticmethod
    def _checks(
        slug: str, number: int, errors: list[str]
    ) -> tuple[CheckReading, ...] | None:
        try:
            from coord.ci_github import GitHubCi  # noqa: PLC0415

            runs = GitHubCi().list_checks_for_pr(slug, number)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"gh pr checks: {type(exc).__name__}: {exc}")
            return None
        out: list[CheckReading] = []
        for run in runs:
            conclusion = getattr(run, "conclusion", None)
            status = str(getattr(run, "status", "") or "")
            if conclusion is None:
                bucket = "pending" if status != "completed" else "unknown"
            elif conclusion == "success":
                bucket = "success"
            elif conclusion in ("failure", "timed_out", "cancelled", "action_required"):
                bucket = "failure"
            else:
                bucket = "unknown"
            out.append(CheckReading(name=str(getattr(run, "name", "") or "?"), conclusion=bucket))
        return tuple(out)

    def _github_slug(self, repo: str) -> str:
        try:
            found = self.config.repo(repo) if self.config is not None else None
        except Exception:  # noqa: BLE001 — pragma: no cover
            return ""
        return str(getattr(found, "github", "") or "")

    # -- agent `/health` ---------------------------------------------------
    def _foreign(self, entry: QueueEntry) -> bool:
        """Was this entry launched by a host whose tmux we cannot read?

        #1870: ``live_sessions`` is a LOCAL ``tmux list-sessions``.  For an
        entry another machine launched, "not in that set" means *"not my
        session to see"*, not *"dead"* — and feeding that to
        ``_rule_dead_leg`` would manufacture a high-confidence ``dead-leg``
        for a perfectly healthy remote session.  Leave ``agent_has_session``
        at ``None`` instead, which disables the rule.
        """
        if not self.local_host or not entry.launch_host:
            return False
        # Same normalisation `_local_host_id()` documents: short hostname,
        # lowercased, domain suffix dropped — so a machine addressed as
        # `dellserver` in the queue row and `dellserver.local` by DNS still
        # compares equal to itself and does NOT read as foreign.
        def short(name: str) -> str:
            return name.split(".")[0].strip().lower()

        return short(entry.launch_host) != short(self.local_host)

    def _health(
        self, entry: QueueEntry
    ) -> tuple[bool | None, bool | None, list[str]]:
        machine = entry.launch_host or entry.machine
        has_session: bool | None = None
        if self.live_sessions is not None and entry.session_name and not self._foreign(
            entry
        ):
            has_session = entry.session_name in self.live_sessions
        if not self.fleet_health or not machine:
            return None, has_session, []
        rows = self.fleet_health.get("machine_health")
        if not isinstance(rows, list):
            return None, has_session, []
        for row in rows:
            if not isinstance(row, dict) or str(row.get("machine") or "") != machine:
                continue
            crits = [
                str(r.get("check_id") or "")
                for r in (row.get("results") or [])
                if isinstance(r, dict) and str(r.get("severity") or "") == "crit"
            ]
            return _row_reachable(row), has_session, crits
        # The machine has no row at all.  Every producer of `machine_health`
        # emits one row per *configured* machine, so this means the entry
        # names a machine that is not in `config.machines` (decommissioned,
        # renamed, or hand-written) — we have no reading for it, which is
        # `None` ("could not tell"), never `False` ("it is down").
        return None, has_session, []


# ── trigger + budget + the pass ──────────────────────────────────────────────


def stalled_keys(events: Iterable[Any]) -> list[str]:
    """The ``repo#issue`` keys #1632's detector currently considers stalled.

    This module owns **no** stall definition.  It reads the notifier's own
    :class:`~coord.notifier.models.NotifyEvent` list — whatever
    :func:`coord.notifier.predicate.evaluate` raised — and maps the ones that
    name a repo and an issue onto queue keys.  There is no threshold here, no
    ``now``, and no age comparison: adding one would be the second clock
    #1440 and the propagate-quiescence design both warn about.
    """
    from coord.drive_queue import entry_key  # noqa: PLC0415

    out: list[str] = []
    seen: set[str] = set()
    for event in events:
        repo = getattr(event, "repo", None)
        issue = getattr(event, "issue", None)
        if not repo or issue is None:
            continue
        key = entry_key(str(repo), int(issue))
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def trigger_conditions(events: Iterable[Any]) -> dict[str, str]:
    """``key -> the strongest notifier condition that named it``."""
    from coord.drive_queue import entry_key  # noqa: PLC0415

    out: dict[str, str] = {}
    for event in events:
        repo = getattr(event, "repo", None)
        issue = getattr(event, "issue", None)
        if not repo or issue is None:
            continue
        out.setdefault(
            entry_key(str(repo), int(issue)), str(getattr(event, "condition", "") or "")
        )
    return out


def needs_diagnosis(episode: Mapping[str, Any]) -> bool:
    """Is this open episode still worth (and still allowed) a look?

    Three conditions, all necessary:

    * it is still stalled — a resolved episode's cause is already known;
    * it has not exhausted :data:`MAX_DIAGNOSES_PER_EPISODE` — the #2272
      budget, which is what stops a stall nobody can explain from re-shelling
      ``gh`` on every notifier tick for the rest of the night; and
    * its last look did not already conclude.  A concluded diagnosis is not
      re-derived: re-running it would spend the budget re-confirming
      something, and would let a later probe failure downgrade an answer that
      was already earned.  Only :data:`CAUSE_UNKNOWN` is retried.
    """
    if episode.get("resolved"):
        return False
    if int(episode.get("diagnoses") or 0) >= MAX_DIAGNOSES_PER_EPISODE:
        return False
    cause = str(episode.get("diagnosed_cause") or "")
    return cause in ("", CAUSE_UNKNOWN)


def run_pass(
    entries: Sequence[QueueEntry],
    open_episodes: Sequence[Mapping[str, Any]],
    *,
    probe: LiveProbe,
    keys: Sequence[str] | None = None,
    triggers: Mapping[str, str] | None = None,
    limit: int | None = MAX_DIAGNOSES_PER_PASS,
) -> list[Diagnosis]:
    """Diagnose every stalled entry the detector named.  **Writes nothing.**

    Returns the diagnoses; appending them to the log is the caller's job (see
    :func:`coord.block_log.diagnosis_event`).  Splitting derivation from
    persistence is what lets the zero-mutation test run a full pass and then
    assert the board, the queue and every ``gh`` verb are unchanged.

    *keys* narrows the pass to the detector's output.  ``None`` means "every
    open episode", which is what ``coord drive-queue diagnose`` uses when an
    operator asks directly — the budget still applies.
    """
    by_key = {e.key: e for e in entries}
    wanted = None if keys is None else set(keys)
    triggers = triggers or {}

    candidates: list[tuple[str, QueueEntry]] = []
    for episode in open_episodes:
        key = str(episode.get("key") or "")
        if not key or (wanted is not None and key not in wanted):
            continue
        if not needs_diagnosis(episode):
            continue
        entry = by_key.get(key)
        if entry is None:
            # The log has an open episode for a row that has left the queue.
            # Not our business to reconcile — Phase 0's `episodes()` already
            # reports it, and inventing a QueueEntry to diagnose would be a
            # write in all but name.
            continue
        candidates.append((key, entry))

    if limit is not None and len(candidates) > limit:
        # Never a silent truncation: the operator (and #2270's report) must be
        # able to tell "nothing else needed diagnosing" from "we ran out of
        # tick".  The deferred entries are diagnosed on the next pass.
        _log.info(
            "queue-diagnose: %d entr(ies) deferred to the next pass (cap %d)",
            len(candidates) - limit,
            limit,
        )
        candidates = candidates[:limit]

    out: list[Diagnosis] = []
    for key, entry in candidates:
        try:
            live = probe.probe(entry)
        except Exception as exc:  # noqa: BLE001 — a diagnosis must never raise
            _log.debug("queue-diagnose: probe failed for %s", key, exc_info=True)
            live = LiveState(probe_errors=(f"probe raised {type(exc).__name__}: {exc}",))
        out.append(diagnose(entry, live, trigger=triggers.get(key, "")))
    return out
