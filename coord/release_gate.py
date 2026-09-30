"""#3488: the opt-in cross-platform release gate.

vimcode shipped Win-GUI with no visible menu bar and no window controls — a
regression produced by two INDIVIDUALLY correct fixes (quadraui#1199 plus
#1200) — and nothing at release time looked. This module is the "something
looked": before an app repo's release is cut, every configured Tier-2 smoke
lane must show a PASSING result at the release SHA, and (when required) the
most recent ``coord bugbash`` run reachable from that SHA must have ended
with zero new findings.

This is the pure decision core, in the same shape as ``coord/gates.py``
(read-only, dependency-injected, no network/subprocess of its own) — the I/O
shell (dispatching to fetch real Tier-2 results, reading a bugbash journal,
resolving SHA ancestry via git) lives in ``coord.commands.release``.

**#2096 "a gate must be able to fail" is the design constraint throughout:**

- A lane with NO recorded result is a FAILING step, never a skip — there is
  nothing to default to "pass" from (see :func:`evaluate_release_gate`'s
  ``lane:<name>`` step for a missing lane).
- A lane result recorded at some OTHER commit is a FAILING step too ("too
  stale to certify this release"), not silently accepted — a snapshot from
  before the release SHA existed cannot contradict anything that changed
  since.
- A ``coord bugbash`` run that terminated via its own ``"lane_failure"``
  reason (:mod:`coord.bugbash`'s own #2096 guard: every explored lane failed
  to dispatch/poll/log) is NOT treated as a clean pass here either — see
  :attr:`BugbashRunRecord.verified`.
- An operator override never erases the underlying failing steps — see
  :meth:`ReleaseGateVerdict.failing_steps` vs. :meth:`ReleaseGateVerdict.
  effective_passed`. The override is audited evidence layered ON TOP of the
  failure, not a rewrite of what was actually observed.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass, replace
from typing import Callable, Sequence

# ── observed inputs ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LaneResult:
    """One Tier-2 smoke-spec observation for one lane (acceptance-driver
    kind — ``tui-pty``/``win-native``/``mac-native``/``gtk-native``, see
    :data:`coord.acceptance_drivers.SUPPORTED_KINDS`) at one commit.

    ``sha`` is the full commit the observation was taken AT — never
    assumed or defaulted; :func:`evaluate_release_gate` only ever counts a
    result whose ``sha`` equals the release SHA exactly (lane results are
    not given an ancestry oracle the way bugbash runs are — a smoke leg is
    cheap enough to re-run per release, so there is no reason to accept a
    stale one).
    """

    lane: str
    sha: str
    passed: bool
    detail: str = ""
    #: Epoch seconds the observation was taken. Used only to pick the most
    #: recent among several results for the same ``(lane, sha)`` pair; a
    #: missing/zero value sorts first, never last, so an un-timestamped
    #: fixture never silently wins over a real one.
    checked_at: float | None = None


@dataclass(frozen=True)
class BugbashRunRecord:
    """One ``coord bugbash`` run (:class:`coord.bugbash.BugbashReport`),
    reduced to what the release gate needs to grade it.

    ``verified`` mirrors :meth:`coord.bugbash.BugbashReport.rounds`'s own
    #2096 distinction: ``False`` means the terminating round's
    ``termination_reason`` was ``"lane_failure"`` — every lane explored that
    round failed to dispatch/poll/fetch its log, so "zero new findings" was
    never actually OBSERVED, it is just the absence of a dispatch that could
    have found one. A run like that must never read as a clean pass.
    """

    sha: str
    new_findings: int
    verified: bool = True
    ran_at: float = 0.0
    detail: str = ""

    @property
    def clean(self) -> bool:
        """A genuinely verified, zero-new-findings run — the only shape
        that may satisfy the gate's bugbash step."""
        return self.verified and self.new_findings == 0


# ── the comparator a caller supplies for "at or after" ─────────────────────

#: ``(candidate_sha, release_sha) -> bool`` — "did *candidate_sha* happen at
#: a commit that already contains *release_sha*?" Injected rather than
#: computed here: answering it for real needs a git checkout
#: (``git merge-base --is-ancestor``, wired by ``coord.commands.release``),
#: and this module stays dependency-free and fixture-testable.
ShaComparator = Callable[[str, str], bool]


def _default_sha_at_or_after(candidate_sha: str, release_sha: str) -> bool:
    """The only answer provable with NO ancestry oracle at all: exact
    equality. #2096 — never assume a descendant relationship that cannot be
    proven; a caller with real git access passes a real ancestry check
    instead (see ``coord.commands.release._git_is_ancestor``)."""
    return candidate_sha == release_sha


# ── the verdict ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GateStepResult:
    """One named check the gate evaluated — one lane, or the bugbash step."""

    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class GateOverride:
    """An audited operator override (#3488, the ``--override-human-required``
    pattern from ``coord merge``, #1251): a non-empty *reason* is mandatory
    — see :func:`validate_override_reason`, the one place that decides
    whether a reason is acceptable, shared by the CLI's up-front validation
    and :func:`apply_override`'s own defense-in-depth check."""

    reason: str
    by: str | None = None
    at: float | None = None


@dataclass(frozen=True)
class ReleaseGateVerdict:
    """The result of one :func:`evaluate_release_gate` call.

    Two different questions, and a caller must pick the right one:

    - :attr:`gate_passed` — did every configured step actually observe a
      pass? Ignores any override. This is what a parity-matrix report or an
      audit trail should read to describe what was OBSERVED.
    - :attr:`effective_passed` — may a release actually be cut? The raw
      gate, OR a recorded override. This is the one a CLI exit code / a
      merge-gate-style caller should branch on.
    """

    repo: str
    release_sha: str
    steps: tuple[GateStepResult, ...] = ()
    override: GateOverride | None = None

    @property
    def gate_passed(self) -> bool:
        return all(s.passed for s in self.steps)

    @property
    def effective_passed(self) -> bool:
        return self.gate_passed or self.override is not None

    @property
    def failing_steps(self) -> tuple[GateStepResult, ...]:
        return tuple(s for s in self.steps if not s.passed)


def validate_override_reason(reason: str | None) -> str:
    """The ONE place that decides whether an override reason is acceptable
    (#2096 "one question, one answer"). Both the CLI's up-front validation
    (so a bad override fails fast, before anything else runs — mirrors
    ``coord merge --override-human-required``'s own early exit) and
    :func:`apply_override`'s defense-in-depth check call this, rather than
    each re-implementing the same ``.strip()`` test and risking them
    disagreeing about an edge case like ``"   "``.

    Raises :class:`ValueError` naming what's wrong; never silently accepts
    a blank/whitespace-only reason — see the #1251 review note about
    ``--override-human-required ""`` almost shipping exactly that gap.
    """
    if reason is None or not reason.strip():
        raise ValueError(
            "a release-gate override requires a non-empty reason string"
        )
    return reason.strip()


def apply_override(
    verdict: ReleaseGateVerdict,
    *,
    reason: str,
    by: str | None = None,
    now: float | None = None,
) -> ReleaseGateVerdict:
    """Layer an audited override onto *verdict*.

    Never mutates ``verdict.steps`` — the underlying failing steps stay
    exactly as observed (:attr:`ReleaseGateVerdict.failing_steps` is
    unaffected), so an override is auditable evidence of WHAT was bypassed,
    never a rewrite of what was actually seen (#2096: a gate must be able to
    fail, and "it failed but was overridden" must stay visible after the
    override is applied).
    """
    validated_reason = validate_override_reason(reason)
    at = _time.time() if now is None else now
    return replace(
        verdict, override=GateOverride(reason=validated_reason, by=by, at=at)
    )


# ── the gate itself ─────────────────────────────────────────────────────


def _lane_step(
    lane: str, release_sha: str, lane_results: Sequence[LaneResult],
) -> GateStepResult:
    at_release_sha = [
        lr for lr in lane_results if lr.lane == lane and lr.sha == release_sha
    ]
    if not at_release_sha:
        any_result = [lr for lr in lane_results if lr.lane == lane]
        if any_result:
            stale = max(any_result, key=lambda lr: lr.checked_at or 0.0)
            return GateStepResult(
                name=f"lane:{lane}",
                passed=False,
                detail=(
                    f"most recent Tier-2 result for lane {lane!r} is at "
                    f"{stale.sha!r}, not the release SHA {release_sha!r} — "
                    "too stale to certify this release"
                ),
            )
        return GateStepResult(
            name=f"lane:{lane}",
            passed=False,
            detail=f"no Tier-2 smoke result recorded for lane {lane!r}",
        )

    chosen = max(at_release_sha, key=lambda lr: lr.checked_at or 0.0)
    if chosen.passed:
        return GateStepResult(
            name=f"lane:{lane}", passed=True, detail=chosen.detail or "passed",
        )
    return GateStepResult(
        name=f"lane:{lane}",
        passed=False,
        detail=chosen.detail or f"lane {lane!r} failed at {release_sha!r}",
    )


def _bugbash_step(
    release_sha: str,
    bugbash_runs: Sequence[BugbashRunRecord],
    sha_is_at_or_after: ShaComparator,
) -> GateStepResult:
    eligible = [b for b in bugbash_runs if sha_is_at_or_after(b.sha, release_sha)]
    if not eligible:
        return GateStepResult(
            name="bugbash",
            passed=False,
            detail=(
                f"no `coord bugbash` run found at or after release SHA "
                f"{release_sha!r}"
            ),
        )

    latest = max(eligible, key=lambda b: b.ran_at)
    if not latest.verified:
        return GateStepResult(
            name="bugbash",
            passed=False,
            detail=(
                f"most recent eligible bugbash run ({latest.sha}) was not a "
                f"verified clean pass — {latest.detail or 'lane failure mid-run'}"
            ),
        )
    if latest.new_findings:
        return GateStepResult(
            name="bugbash",
            passed=False,
            detail=(
                f"most recent eligible bugbash run ({latest.sha}) filed "
                f"{latest.new_findings} new finding(s)"
            ),
        )
    return GateStepResult(
        name="bugbash", passed=True, detail=f"zero new findings at {latest.sha}",
    )


def evaluate_release_gate(
    *,
    repo: str,
    release_sha: str,
    required_lanes: Sequence[str],
    lane_results: Sequence[LaneResult] = (),
    bugbash_required: bool = False,
    bugbash_runs: Sequence[BugbashRunRecord] = (),
    sha_is_at_or_after: ShaComparator = _default_sha_at_or_after,
) -> ReleaseGateVerdict:
    """Evaluate the #3488 release gate for *repo* at *release_sha*.

    One :class:`GateStepResult` per required lane (named ``lane:<lane>``),
    plus one more named ``"bugbash"`` when *bugbash_required*. Every step is
    graded from an OBSERVATION passed in — this function never assumes a
    step passed because no data was given for it (see the module docstring
    for the #2096 rationale behind each failure mode).

    Callers: :func:`coord.commands.release._release_gate_evaluate` wires
    this to real Tier-2 lane fetchers and a real bugbash journal/git
    ancestry check; ``tests/test_release_gate.py`` calls it directly against
    fixtures.
    """
    steps = [_lane_step(lane, release_sha, lane_results) for lane in required_lanes]
    if bugbash_required:
        steps.append(_bugbash_step(release_sha, bugbash_runs, sha_is_at_or_after))
    return ReleaseGateVerdict(repo=repo, release_sha=release_sha, steps=tuple(steps))
