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
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from coord.drive_queue import QueueEntry, is_unreachable_wait_shape

_log = logging.getLogger(__name__)

__all__ = [
    "CAUSE_INTEGRATION_RED",
    "CAUSE_NEVER_DISPATCHED",
    "CAUSE_PROSE",
    "CAUSE_REVIEW_STALE",
    "CAUSE_TEST_STALE",
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
    "is_foreign_launch",
    "needs_diagnosis",
    "run_pass",
    "stalled_keys",
    "stated_family",
    "trigger_conditions",
    # #3883: escalation-diagnosis enrichment.
    "ENV_DIAGNOSIS_BASE_RED",
    "ENV_DIAGNOSIS_CREDENTIAL",
    "ENV_DIAGNOSIS_INCONCLUSIVE",
    "ENV_DIAGNOSIS_MISSING_TOOL",
    "DiagnosisOption",
    "EnvDiagnosis",
    "EnvProbeResult",
    "LostWorkCandidate",
    "classify_env_probe_output",
    "diagnose_baseline_red_env",
    "extract_failing_file_paths",
    "find_lost_work_touching",
    "is_sealed_confined_failure",
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
#: #3929 (#3918 gap 3): a live :class:`~coord.entry_truth.EntryTruth`
#: (#3936) confirms an approve verdict exists for this branch's work chain
#: but no longer covers it, via one of two routes, never `coord gates`'s
#: own cached decision: (1) :attr:`~coord.entry_truth.EntryTruth.
#: review_stale` — the gate itself still reads APPROVED, but the #3948
#: patch-id check confirms the base moved under an unchanged branch head
#: (quadraui#2173: `coord gates` read "every required gate reads clear"
#: while the approving review's own `review_patch_id` no longer matched the
#: branch — a cause that reached :data:`CAUSE_UNKNOWN` and dispatched a
#: blind fix-it work leg instead of a fresh review); or (2) the gate reads
#: NOT approved and :func:`coord.merge_queue.find_scoped_review_candidate`
#: — built on the same :func:`~coord.merge_queue.scan_approved_reviews`
#: ``has_approved_review`` relies on — finds a genuinely voided PRIOR
#: approval, with none of the rebase-equivalence escape hatches (#1475
#: content-identical patch-id, #3707 ancillary-only, #3749 context-only)
#: applying. The fix either way is a review, never a worker leg — see
#: :mod:`coord.board_doctor_policy`'s ``restage`` wiring for this cause.
CAUSE_REVIEW_STALE = "review-stale"
#: #3929 (#3918 gap 3), the Test-gate twin of :data:`CAUSE_REVIEW_STALE`:
#: :func:`coord.merge_queue.evaluate_smoke_verdict`'s own ``kind`` reads
#: :data:`coord.merge_queue.SMOKE_STALE` — a ``passed``/``skipped`` verdict
#: recorded against a branch/base SHA pair that has since moved. Never
#: ``SMOKE_MISSING`` (nothing was ever recorded — that is a plain gate
#: refusal, not staleness) or ``SMOKE_UNKNOWN`` (the live SHA needed to
#: judge staleness could not even be read — abstain, don't guess). The fix
#: is a fresh test, never a worker leg.
CAUSE_TEST_STALE = "test-stale"
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
    CAUSE_REVIEW_STALE: "the merge gate's own freshness check confirms an approval exists but no longer covers this branch's current head/patch-id — needs a fresh review, not a fix-it leg",
    CAUSE_TEST_STALE: "the merge gate's own freshness check confirms a recorded test verdict is stale against the current branch/base — needs a fresh test, not a fix-it leg",
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
    #: #3929: ``True`` only when the merge gate's OWN freshness check
    #: (:func:`coord.merge_queue.find_scoped_review_candidate`) confirms a
    #: prior approve verdict exists for this branch but no longer covers
    #: its current head/patch-id. ``False`` when review isn't required at
    #: all, or the requirement reads fresh (approved-and-current, or never
    #: approved in the first place — there is nothing stale to report
    #: either way). ``None`` — "could not tell" — when the check was never
    #: attempted (no board/config, no work-like assignment to anchor it
    #: to) or raised. Never derived from ``coord gates``'s own cached
    #: decision (see :data:`CAUSE_REVIEW_STALE`): a direct call, every
    #: time, is the whole point.
    review_stale: bool | None = None
    #: #3929, the Test-gate twin of `review_stale`: ``True`` only when
    #: :func:`coord.merge_queue.evaluate_smoke_verdict`'s own ``kind``
    #: reads :data:`coord.merge_queue.SMOKE_STALE`. ``False`` when Test
    #: isn't required, or the verdict reads fresh/missing/unknown — a
    #: verdict that was simply never recorded is a plain gate refusal, not
    #: staleness, and an unreadable live SHA is "could not tell", not
    #: "confirmed not stale". ``None`` when the check was never attempted.
    test_stale: bool | None = None

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


def _staleness_line(live: LiveState) -> str:
    """Rendered unconditionally, same posture as :func:`_branch_test_line` —
    the one place an operator reading ANY cause can see what the merge
    gate's own direct freshness re-check found, independent of what
    ``coord gates``'s cached decision said (#3929)."""

    def _bit(label: str, value: bool | None, stale_word: str) -> str:
        if value is None:
            return f"{label}: not read"
        if value:
            return f"{label}: {stale_word}"
        # `False` deliberately collapses three materially different
        # readings the live recheck does not distinguish further: fresh
        # (approved/passed and current), not required at all, or never
        # approved/recorded in the first place. None of the three is
        # actionable the way a confirmed-stale reading is, so this module
        # does not split them — but say so plainly rather than implying
        # "fresh" is the confirmed answer.
        return f"{label}: not confirmed stale (fresh, not required, or never recorded)"

    return (
        "merge-gate freshness recheck — "
        f"{_bit('review', live.review_stale, 'APPROVED BUT STALE')}; "
        f"{_bit('test', live.test_stale, 'RECORDED BUT STALE')}"
    )


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
        _staleness_line(live),
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


def _rule_review_stale(entry: QueueEntry, live: LiveState) -> tuple[str, str, str] | None:
    """#3929: fires straight off the merge gate's own direct freshness
    re-check (:attr:`LiveState.review_stale`) — never off ``coord
    gates``'s cached decision, which can read "every required gate reads
    clear" for the exact row this is meant to catch (quadraui#2173, see
    :data:`CAUSE_REVIEW_STALE`). Checked ahead of :func:`_rule_gate_
    blocked`: a confirmed-stale approval is a sharper, more actionable
    answer than the generic "a merge gate is genuinely refusing right
    now" read, and must win the race against :data:`CAUSE_UNKNOWN` long
    before this module gives up. Checked AFTER :func:`_rule_integration_
    red`/:func:`_rule_ci_red`/:func:`_rule_ci_pending` on purpose — the
    issue this closes is staleness outranking a CLEAR gate reading, not a
    genuinely red or pending required check, which is the row's more
    actionable, more local problem regardless of what the prior approval
    says.
    """
    if live.review_stale is True:
        return CAUSE_REVIEW_STALE, CONFIDENCE_HIGH, _staleness_line(live)
    return None


def _rule_test_stale(entry: QueueEntry, live: LiveState) -> tuple[str, str, str] | None:
    """#3929, the Test-gate twin of :func:`_rule_review_stale` — see that
    rule's docstring."""
    if live.test_stale is True:
        return CAUSE_TEST_STALE, CONFIDENCE_HIGH, _staleness_line(live)
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
    # #3929: a confirmed-stale review/test, straight off a direct re-check
    # of the merge gate's own freshness functions, outranks the generic
    # `gate-blocked` read that a STALE `coord gates` cache can otherwise
    # hide behind (quadraui#2173) — a named staleness is a sharper, more
    # actionable answer than "a merge gate is genuinely refusing right
    # now". Deliberately tried AFTER the CI rules above rather than ahead
    # of them: the issue this closes is staleness outranking a CLEAR gate
    # reading, not a genuinely red required check — a row that is both
    # failing CI right now AND carries a superseded prior approval is a CI
    # problem first; the stale approval cannot matter again until the CI
    # failure is fixed and a fresh review is needed anyway.
    _rule_review_stale,
    _rule_test_stale,
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


def is_foreign_launch(entry: QueueEntry, local_host: str | None) -> bool:
    """Was *entry* launched by a host whose tmux *local_host* cannot read?

    #1870: a ``tmux list-sessions`` read is always LOCAL.  For an entry
    launched on a DIFFERENT machine, "not in that set" means *"not mine to
    see"*, not *"dead"* — treating it as dead would manufacture a
    confidently-wrong verdict for a perfectly healthy remote session.
    ``False`` (never foreign) whenever either side of the comparison is
    unknown: *local_host* unset (the check disabled, same posture as
    ``now=None`` disabling a grace window), or *entry* predates the
    ``launch_host`` column / was hand-edited (degrades to the pre-#1870
    behaviour exactly).

    One answer, shared by every caller that needs this guard
    (:meth:`GhLiveProbe._foreign`, :func:`coord.board_doctor._no_progress_
    shape`) rather than each re-deriving it (#2096) — the #3888 review
    finding that the board-doctor no-progress pass had grown a THIRD,
    guardless copy of this exact question.
    """
    if not local_host or not entry.launch_host:
        return False

    # Short hostname, lowercased, domain suffix dropped — so a machine
    # addressed as `dellserver` in the queue row and `dellserver.local` by
    # DNS still compares equal to itself and does NOT read as foreign.
    def short(name: str) -> str:
        return name.split(".")[0].strip().lower()

    return short(entry.launch_host) != short(local_host)


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
        (
            branch, gate_ready, gate_blockers, no_assignment_ever, branch_test_state,
            review_stale, test_stale,
        ) = self._gates(entry, errors)
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
            review_stale=review_stale,
            test_stale=test_stale,
        )

    # -- `coord gates`, verbatim ------------------------------------------
    def _gates(
        self, entry: QueueEntry, errors: list[str]
    ) -> tuple[str, bool | None, list[str], bool | None, str | None, bool | None, bool | None]:
        # #3929 review: run BEFORE `build_gate_report`, and independently of
        # whether it raises — the whole point of this recheck is that it
        # does not trust `coord gates`'s cached decision, so it must not be
        # coupled to that decision having succeeded in the first place. A
        # raising `build_gate_report` still leaves `review_stale`/
        # `test_stale` as the one reading this pass got to confirm.
        review_stale, test_stale = self._staleness(entry, errors)
        if self.board is None or self.config is None:
            return "", None, [], None, None, review_stale, test_stale
        try:
            from coord import github_ops  # noqa: PLC0415
            from coord.gates import build_gate_report  # noqa: PLC0415

            report = build_gate_report(
                self.board, self.config, entry.repo, entry.issue, gh_ops=github_ops
            )
        except Exception as exc:  # noqa: BLE001 — a probe, never a decision
            errors.append(f"coord gates: {type(exc).__name__}: {exc}")
            return "", None, [], None, None, review_stale, test_stale
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
            return (
                str(report.branch or ""), None, [], no_assignment_ever, branch_test_state,
                review_stale, test_stale,
            )
        blockers = [
            f"{d.gate}: {d.reason or 'refused'}" for d in decisions if not d.ok
        ]
        return (
            str(report.branch or ""), not blockers, blockers, no_assignment_ever,
            branch_test_state, review_stale, test_stale,
        )

    # -- #3936: review/test staleness decided from a live EntryTruth -------
    def _staleness(
        self, entry: QueueEntry, errors: list[str]
    ) -> tuple[bool | None, bool | None]:
        """Review/test staleness for *entry*, read off a live
        :class:`~coord.entry_truth.EntryTruth` (:func:`coord.entry_truth.
        reconcile_entry`) rather than this module re-deriving the freshness
        math a second time (#3929's original sin, #2096).

        **Test side** is fully answered by :attr:`EntryTruth.test` — the
        SAME ``coord.merge_queue.evaluate_smoke_verdict`` call
        :func:`coord.gates.build_gate_report` already makes to build it.
        Its ``anchor`` is populated ONLY for a STALE (never a MISSING)
        verdict (see :class:`~coord.gates.GateDecision`'s own field
        docstring) — exactly the stale-vs-missing split this needs, with no
        second ``evaluate_smoke_verdict`` call anywhere in this method.

        **Review side**: once the gate itself reports APPROVED
        (``truth.review.ok``), :attr:`EntryTruth.review_stale` is the
        #3948 patch-id-drift check this module used to have no way to see
        at all — the OLD code read ``scan.approved`` alone as unconditionally
        fresh, which is quadraui#2173's exact shape: ``coord gates`` read
        "every required gate reads clear" while the approving review's own
        ``review_patch_id`` no longer matched the branch. When the gate
        reports NOT approved (and the branch head was confirmable),
        :class:`EntryTruth` has nothing further to say — distinguishing
        "genuinely never reviewed" from "a prior approval voided by a real
        content change" is additive work it does not yet do (see its own
        module docstring, rule 2) — so :meth:`_review_stale_fallback` asks
        :func:`coord.merge_queue.find_scoped_review_candidate` directly,
        gated behind the gate's own "not approved" reading exactly as
        #3929's blocking review required (that function disagrees with
        ``scan_approved_reviews`` on its own, on a branch carrying two
        approvals at different SHAs — see its own docstring).

        ``(None, None)`` whenever there is nothing to anchor the check to —
        :func:`~coord.entry_truth.reconcile_entry`'s own ``None``/"unknown"
        fail-closed posture (#2704): a live probe that was never asked, or
        one that was asked and failed, both read "could not tell", never
        "confirmed fresh".
        """
        from coord import github_ops  # noqa: PLC0415
        from coord.entry_truth import reconcile_entry  # noqa: PLC0415
        from coord.merge_queue import UNKNOWN_BRANCH_HEAD_REASON  # noqa: PLC0415

        try:
            truth = reconcile_entry(
                self.board, self.config, entry.repo, entry.issue, gh_ops=github_ops,
            )
        except Exception as exc:  # noqa: BLE001 — a probe, never a decision
            errors.append(f"merge-gate freshness recheck: {type(exc).__name__}: {exc}")
            return None, None

        review_stale: bool | None
        if truth.review is None:
            review_stale = None
        elif not truth.review.required:
            review_stale = False
        elif truth.review.ok:
            review_stale = truth.review_stale
        elif (truth.review.reason or "").startswith(UNKNOWN_BRANCH_HEAD_REASON):
            # Refused purely because the live branch head could not be
            # confirmed right now — "could not tell", not "confirmed fresh"
            # and not "confirmed stale" either.
            review_stale = None
        else:
            review_stale = self._review_stale_fallback(entry, truth, errors)

        test_stale: bool | None
        if truth.test is None:
            test_stale = None
        elif not truth.test.required:
            test_stale = False
        elif truth.test.ok:
            test_stale = False
        elif (truth.test.reason or "").startswith(UNKNOWN_BRANCH_HEAD_REASON):
            # The live SHA needed to judge staleness could not even be
            # read — "could not tell", never "confirmed fresh".
            test_stale = None
        else:
            test_stale = truth.test.anchor is not None

        return review_stale, test_stale

    def _review_stale_fallback(
        self, entry: QueueEntry, truth: Any, errors: list[str],
    ) -> bool | None:
        """#3929's remaining gap :class:`~coord.entry_truth.EntryTruth`
        does not cover: the review gate reads NOT approved (not merely
        "branch head unconfirmed"), and the open question is whether that
        is a genuinely voided PRIOR approval (needs a fresh review,
        :data:`CAUSE_REVIEW_STALE`) or a branch that was simply never
        reviewed at all (an ordinary pending review).

        :attr:`EntryTruth.review_stale` is scoped to the OPPOSITE case — an
        approval that still reads ``ok`` (see its own docstring) — so this
        asking :func:`coord.merge_queue.find_scoped_review_candidate` is
        additive, never a second implementation of what `EntryTruth`
        already decides for the approved case.

        ``None`` ("could not tell") when there is nothing to anchor the
        check to (no work-like assignment, no branch, no repo config) or a
        live call raises — never "confirmed not stale".
        """
        try:
            from coord import github_ops  # noqa: PLC0415
            from coord import merge_queue as mq  # noqa: PLC0415
            from coord.gates import _select_winning_work_assignment, assignments_for_issue  # noqa: PLC0415
            from coord.models import WORK_LIKE_TYPES  # noqa: PLC0415

            if self.board is None or self.config is None or not truth.branch:
                return None
            repo_cfg = self.config.repo(entry.repo)
            if repo_cfg is None:
                return None
            work_like = [
                a
                for a in assignments_for_issue(self.board, entry.repo, entry.issue)
                if a.type in WORK_LIKE_TYPES
            ]
            if not work_like:
                return None
            winner = _select_winning_work_assignment(work_like)
            if not winner.branch:
                return None
            # The SAME head/patch-id `reconcile_entry` already probed LIVE
            # a moment ago — carried straight through rather than paying
            # for (and risking a second, independently-timed disagreement
            # with) another `gh_ops` round trip for the identical branch.
            mq_entry = mq.QueuedMerge(
                assignment_id=winner.assignment_id or "",
                repo_name=winner.repo_name,
                repo_github=repo_cfg.github,
                branch=winner.branch,
                target_branch=truth.target_branch or repo_cfg.default_branch,
                issue_number=getattr(winner, "issue_number", 0) or 0,
                issue_title=getattr(winner, "issue_title", None) or "",
                assignment_type=getattr(winner, "type", None) or "work",
                required_gates=list(getattr(winner, "required_gates", None) or []),
                branch_head_sha=truth.head_sha,
                branch_patch_id=truth.patch_id,
            )
            candidate = mq.find_scoped_review_candidate(
                mq_entry, self.board, github_ops, config=self.config,
            )
            return candidate is not None
        except Exception as exc:  # noqa: BLE001 — a probe, never a decision
            errors.append(f"merge-gate freshness recheck: {type(exc).__name__}: {exc}")
            return None

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

        Delegates to :func:`is_foreign_launch`, the one place this question
        is answered (#2096/#3888).
        """
        return is_foreign_launch(entry, self.local_host)

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


# ── #3883: escalation-diagnosis enrichment ──────────────────────────────────
#
# `coord.board_doctor_policy.escalation_alert_dispatcher` is the one place a
# `StuckRow` turns into a human-facing page — these three pieces each answer
# one of #3883's three re-investigation questions that cost an operator a
# whole session apiece on 2026-11-05/06 (coord#3870, quadraui#2028,
# coord#3793): is the live failure confined to a sealed oracle path (never
# fixable by a retry), is the repo's red base an environment problem rather
# than a real regression, and does a branch already sitting unmerged on the
# board happen to be the fix. Every function here is pure or takes its I/O
# as an injected callable — `coord.board_doctor_policy` supplies the live
# board/config/subprocess the production path needs; the tests below drive
# the same functions with fakes, exactly like `LiveProbe` does for
# :func:`diagnose` above.


@dataclass(frozen=True)
class DiagnosisOption:
    """One concrete, ready-to-run next step offered to the operator.

    *command* is a literal, copy-pasteable shell command whenever the fleet
    has one (a `coord` verb, a `git`/`gh` invocation) — the whole point of
    #3883 is that the operator should not have to re-derive it. A handful of
    causes (renewing an out-of-band credential, choosing a design direction)
    have no such command; for those *command* is the concrete instruction
    itself, worded as an imperative action rather than left blank, since
    "there is no command" must never collapse into "there is no option".
    """

    label: str
    command: str
    recommended: bool = False


def is_sealed_confined_failure(
    failing_paths: Sequence[str], sealed_paths: Sequence[str]
) -> bool:
    """True only when *failing_paths* is non-empty, *sealed_paths* is
    non-empty, and EVERY entry in *failing_paths* names a path falling
    under one of *sealed_paths*.

    *failing_paths* is the set of file paths a failing CI job's own log
    excerpt named as failing (`extract_failing_file_paths`), never a bare
    CI check/job NAME — a job name is an operator-chosen label ("Test
    (ubuntu-latest)") that essentially never mentions a path, so matching
    on it alone could never fire. An empty *failing_paths* (no log excerpt
    available, or none of its lines named a file) is the conservative
    direction: this must never manufacture a design-decision page out of
    an ordinary CI failure just because the repo happens to have a sealed
    suite. It fires only for the shape quadraui#2028 actually hit — a
    merge-ref-only failure whose only failing file(s) are themselves
    inside the sealed tree.
    """
    if not failing_paths or not sealed_paths:
        return False
    for path in failing_paths:
        if not any(_path_under_sealed(path, prefix) for prefix in sealed_paths):
            return False
    return True


def _path_under_sealed(name: str, sealed_path: str) -> bool:
    """True when *name* (a failing check name, or a failing file path
    pulled from a CI log excerpt) falls under *sealed_path*.

    A trailing ``/`` means *sealed_path* is a directory: any name
    containing that prefix anywhere counts. No trailing slash means
    *sealed_path* is itself one exact file (an `entrypoint:` route like
    ``tui/tests/acceptance.rs``) — that must match *name* exactly, or as
    a path suffix (*name* ending in ``/<sealed_path>``), never a bare
    substring check, which would also match an unrelated file that
    merely shares a substring with the sealed file's own name.
    """
    sealed_path = sealed_path.strip()
    if not sealed_path:
        return False
    if sealed_path.endswith("/"):
        return sealed_path in name
    return name == sealed_path or name.endswith("/" + sealed_path)


#: :func:`diagnose_baseline_red_env`'s verdict vocabulary — mirrors the
#: `CAUSE_*`/`CONFIDENCE_*` convention above: short, stable slugs a report
#: can bucket on, not prose.
ENV_DIAGNOSIS_CREDENTIAL = "credential"
ENV_DIAGNOSIS_MISSING_TOOL = "missing_tool"
ENV_DIAGNOSIS_BASE_RED = "base_red"
#: The probe could not be run at all, or ran and passed — neither answers
#: "why is the base red", so neither may claim one of the three categories
#: above. Never rendered as a finding; callers fall back to the pre-#3883
#: generic wording when they see this.
ENV_DIAGNOSIS_INCONCLUSIVE = "inconclusive"

#: Substrings of a failing re-run's combined stdout/stderr that mean "the
#: credential this ran with is no good", lowercase. Deliberately broad
#: (not anchored to a provider's exact wording) since the whole point
#: (coord#3870) is catching a NEW provider's auth failure without having
#: seen its exact error text before — a false positive here only produces
#: an extra, harmless "renew credential" option; a false negative silently
#: drops back to the generic "base is red" wording, which is the pre-#3883
#: status quo, never a worse outcome than today.
#:
#: A bare ``"401"``/``"403"`` is deliberately NOT one of these: pytest's own
#: failure output routinely contains both as line numbers
#: (``tests/test_foo.py:403: AssertionError``) and as pass/fail counts
#: (``5 failed, 1401 passed``), so an ordinary red base would misclassify
#: as a credential failure far more often than not. The status-code
#: entries below require the code to appear WITH its usual HTTP wording —
#: enough context that a line number or a count can never match, while
#: still catching a provider's error text this list has never seen before.
_CREDENTIAL_ERROR_PATTERNS: tuple[str, ...] = (
    "unauthorized", "forbidden", "invalid api key",
    "invalid_api_key", "authentication failed", "authentication error",
    "api key expired", "token expired", "invalid_request_error: incorrect api key",
    "permission denied: invalid credential",
    "http 401", "401 unauthorized", "unauthorized (401", "status 401",
    "status: 401", "error 401",
    "http 403", "403 forbidden", "forbidden (403", "status 403",
    "status: 403", "error 403",
)

#: Provider/credential names worth naming in the option text when they turn
#: up near a credential-shaped error — cosmetic only (`EnvDiagnosis.
#: credential_name` is never required to be non-``None``), so this list may
#: safely miss a provider without breaking the classification itself.
#: Every entry is matched as a plain substring of the whole lowercased
#: output, so each must be long and distinctive enough that it cannot turn
#: up inside an unrelated English word — a short abbreviation like `gh`
#: would match "through"/"highlighted" and name the wrong credential, so
#: the forge is listed only under its full name.
_KNOWN_CREDENTIAL_NAMES: tuple[str, ...] = (
    "opencode", "anthropic", "claude", "openai", "github",
)

#: Matches the shell's own "command not found" / "no such file" wording —
#: covers bash, zsh and PowerShell's `CommandNotFoundException` message
#: closely enough to extract the missing tool's name as the capture group.
_MISSING_TOOL_RE = re.compile(
    r"(?:^|[:\s])([\w.\-]+): (?:command not found|not found)"
    r"|no such file or directory: ['\"]?([\w.\-/]+)",
    re.IGNORECASE,
)


def classify_env_probe_output(output: str) -> tuple[str, str | None]:
    """``(category, name)`` for one failing re-run's combined output.

    *category* is one of :data:`ENV_DIAGNOSIS_CREDENTIAL`,
    :data:`ENV_DIAGNOSIS_MISSING_TOOL` or :data:`ENV_DIAGNOSIS_BASE_RED` —
    never :data:`ENV_DIAGNOSIS_INCONCLUSIVE`, which is
    :func:`diagnose_baseline_red_env`'s own verdict for "never got output to
    classify at all", not something this text classifier can conclude.
    Credential is checked before missing-tool: an expired-token message
    sometimes also echoes a "command not found" from the *wrapper* script
    the credential failure aborted, and the credential reading is the more
    actionable one of the two.
    """
    lowered = output.lower()
    for pattern in _CREDENTIAL_ERROR_PATTERNS:
        if pattern in lowered:
            name = next((n for n in _KNOWN_CREDENTIAL_NAMES if n in lowered), None)
            return ENV_DIAGNOSIS_CREDENTIAL, name
    match = _MISSING_TOOL_RE.search(output)
    if match:
        tool = match.group(1) or match.group(2)
        return ENV_DIAGNOSIS_MISSING_TOOL, tool
    return ENV_DIAGNOSIS_BASE_RED, None


#: pytest's own short-summary line ("FAILED tests/test_foo.py::test_bar -
#: AssertionError ..."), which `diagnose_baseline_red_env`'s re-run output
#: carries whenever the failure is a genuine local test and not a
#: credential/missing-tool error aborting before any test ran at all.
_FAILING_FILE_RE = re.compile(r"^FAILED\s+([^\s:]+)", re.MULTILINE)


def extract_failing_file_paths(output: str) -> tuple[str, ...]:
    """The source file(s) named by a failing re-run's own output (#3883
    point 3) — read off the SAME output :func:`diagnose_baseline_red_env`
    already classified, never a second probe, so the lost-work cross-check
    costs no extra re-run. Dedupes, preserving first-seen order; ``()``
    when the output names no file at all (a credential/missing-tool error
    aborted before any test ran, or the output is empty).
    """
    return tuple(dict.fromkeys(_FAILING_FILE_RE.findall(output)))


@dataclass(frozen=True)
class EnvProbeResult:
    """One re-run of a repo's test command, on one host.

    *error* is set only when the probe itself could not be carried out at
    all (no remote-exec path to *host* from here, the checkout is missing,
    the runner raised) — a case :func:`diagnose_baseline_red_env` must read
    as "cannot tell", never as "the tests failed", since *exit_code*/*output*
    are meaningless when *error* is set.
    """

    exit_code: int = 0
    output: str = ""
    error: str | None = None


#: Re-runs *command* on *host*, returning what actually happened. The
#: production default (`coord.board_doctor_policy._default_env_probe_runner`)
#: is a real subprocess re-run under this process's own environment when
#: *host* is the machine this process is itself running on — the fleet has
#: no agent-HTTP exec primitive yet to reach a DIFFERENT host's env from
#: here (tracked as a gap, not silently pretended away: see that function's
#: own docstring). Tests inject a fake.
EnvProbeRunner = Callable[[str, str], EnvProbeResult]


@dataclass(frozen=True)
class EnvDiagnosis:
    """#3883 point 2: what re-running the base under *host*'s own
    environment actually showed.

    *raw_output* is the probe's own combined stdout/stderr, carried through
    verbatim (never re-derived) so a caller doing the point-3 lost-work
    cross-check (:func:`extract_failing_file_paths`) reads the SAME output
    this already classified, rather than re-running the probe a second
    time. Empty whenever the probe itself could not run at all.
    """

    category: str
    detail: str
    host: str
    credential_name: str | None = None
    tool_name: str | None = None
    raw_output: str = ""


def diagnose_baseline_red_env(
    repo_name: str,
    command: str,
    *,
    host: str,
    run: EnvProbeRunner,
) -> EnvDiagnosis:
    """Re-run *command* for *repo_name* on *host* and classify the result
    (#3883 point 2).

    Never raises: *run* itself raising is caught and reported the same as
    an explicit :attr:`EnvProbeResult.error` — a diagnosis probe failing is
    evidence of nothing and must degrade to
    :data:`ENV_DIAGNOSIS_INCONCLUSIVE`, not take the sweep down. A re-run
    that PASSES is just as inconclusive as one that could not be run at
    all: it means the failure did not reproduce right now, which is real
    information (the board-doctor's own alert text says so) but answers a
    different question than "why is it red", so it is never reported as one
    of the three positive categories.
    """
    try:
        result = run(host, command)
    except Exception as exc:  # noqa: BLE001 — a probe failing is not a verdict
        return EnvDiagnosis(
            ENV_DIAGNOSIS_INCONCLUSIVE,
            f"could not re-run on {host}: {type(exc).__name__}: {exc}",
            host,
        )
    if result.error:
        return EnvDiagnosis(
            ENV_DIAGNOSIS_INCONCLUSIVE, f"could not re-run on {host}: {result.error}", host,
        )
    if result.exit_code == 0:
        return EnvDiagnosis(
            ENV_DIAGNOSIS_INCONCLUSIVE,
            f"re-ran clean on {host} just now — the failure did not reproduce",
            host,
        )
    category, name = classify_env_probe_output(result.output)
    if category == ENV_DIAGNOSIS_CREDENTIAL:
        named = f" ({name})" if name else ""
        return EnvDiagnosis(
            category, f"renew credential{named} on {host}", host,
            credential_name=name, raw_output=result.output,
        )
    if category == ENV_DIAGNOSIS_MISSING_TOOL:
        return EnvDiagnosis(
            category, f"install {name or 'the missing tool'} on {host}", host,
            tool_name=name, raw_output=result.output,
        )
    return EnvDiagnosis(
        category, f"base is red on {host}: {command}", host, raw_output=result.output,
    )


@dataclass(frozen=True)
class LostWorkCandidate:
    """One unmerged branch the board still knows about — a merged-status
    row GitHub never actually shows a matching merged PR for, or a
    closed-issue branch with commits that never reached the base — together
    with what it actually changed (#3883 point 3).

    *changed_files* is the branch's diff against the base, already resolved
    by the caller (`coord.github_ops.get_compare_files`, the same primitive
    `coord.reconcile`'s own false-merge audit uses) — this dataclass and
    :func:`find_lost_work_touching` do not fetch anything themselves, so the
    search stays unit-testable without a live repo.
    """

    assignment_id: str
    repo_name: str
    issue_number: int
    branch: str
    changed_files: tuple[str, ...] = ()


def find_lost_work_touching(
    failing_files: Sequence[str],
    candidates: Sequence[LostWorkCandidate],
) -> list[LostWorkCandidate]:
    """Which *candidates* touched any of *failing_files* (#3883 point 3).

    Pure set-intersection — the I/O (which branches exist, what they
    changed) is entirely the caller's job
    (`coord.board_doctor_policy._lost_work_candidates`), so this is the one
    piece that can be driven with plain fixtures. Returns ``[]`` when
    *failing_files* is empty: an unknown failing-file set must never match
    every candidate by vacuous intersection.
    """
    wanted = set(failing_files)
    if not wanted:
        return []
    return [c for c in candidates if wanted.intersection(c.changed_files)]
