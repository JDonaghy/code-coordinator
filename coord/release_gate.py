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
  :attr:`BugbashRunRecord.verified`. Neither is one that terminated
  ``"protocol_error"`` (#3517: a lane completed, but its final report was a
  malformed or missing findings block — never a parseable, trustworthy
  "zero findings" answer) — see :func:`bugbash_run_record_from_report`, the
  one place that maps both termination reasons onto ``verified=False``.
- An operator override never erases the underlying failing steps — see
  :meth:`ReleaseGateVerdict.failing_steps` vs. :meth:`ReleaseGateVerdict.
  effective_passed`. The override is audited evidence layered ON TOP of the
  failure, not a rewrite of what was actually observed.

**#3510: a locked/absent GUI session is BLOCKING, but not "failed".** A
Tier-2 lane observation can itself be ``unavailable`` (the driver's own
session precheck — a locked desktop, no GUI session, or no display —
reported by :mod:`coord.win_native_driver`/:mod:`coord.mac_native_driver`/
:mod:`coord.gtk_native_driver`, never an ordinary ``"fail"``). This still
blocks the gate (:attr:`LaneResult.unavailable` makes :attr:`GateStepResult.
passed` ``False`` the same as any other failing step — #2096, a gate must
be able to fail), but :attr:`GateStepResult.unavailable` keeps that
distinct from an app bug, so an operator reads "unlock the host" instead of
"go debug the app."
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass, replace
from typing import Any, Callable, Sequence

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
    #: ``True`` when this observation is the driver's own ``unavailable``
    #: verdict (#3510) — a locked/absent GUI session or missing display,
    #: never an app bug. ``passed`` must be ``False`` whenever this is
    #: ``True`` (nothing ran, so nothing can have passed); callers
    #: constructing a `LaneResult` by hand should never set both
    #: ``passed=True`` and ``unavailable=True`` — :func:`_lane_step` treats
    #: ``unavailable`` as authoritative over ``passed`` regardless.
    unavailable: bool = False


@dataclass(frozen=True)
class NightlyArtifactResult:
    """One #3652 real-platform nightly-smoke observation for one SHIPPED
    artifact at one commit — the nightly-runner's analogue of
    :class:`LaneResult`.

    ``artifact`` names the shipped build the nightly runner exercised
    (e.g. ``"macos-dmg"``, ``"windows-installer"``,
    ``"linux-gtk-appimage"``) — the per-repo set a release must cover is
    declared in ``coordinator.yml`` as
    ``coord.config.ReleaseGateRepoConfig.nightly_artifacts``. This is
    deliberately the SAME shape (and the SAME #2096 staleness/missing-data
    discipline, INCLUDING #3510's ``unavailable`` distinction) as
    :class:`LaneResult` — a real-platform run and a synthetic Tier-2 lane
    run answer the identical question ("did this pass AT the release SHA,
    on real hardware/a real build"), so they are graded by the identical
    pattern (:func:`_nightly_artifact_step` mirrors :func:`_lane_step`
    exactly) rather than two gates quietly drifting apart (#2096 "one
    question, one answer"). A real-platform nightly run is, if anything,
    the case that hits a locked/absent GUI session MOST often — dropping
    ``unavailable`` here would make `_nightly_artifact_step` file an
    app-repo bug for a locked host it should instead report as "unlock the
    host and re-run".
    """

    artifact: str
    sha: str
    passed: bool
    detail: str = ""
    #: Epoch seconds the observation was taken — same role as
    #: :attr:`LaneResult.checked_at`.
    checked_at: float | None = None
    #: ``True`` when this observation is the driver's own ``unavailable``
    #: verdict (#3510) — same role as :attr:`LaneResult.unavailable`:
    #: a locked/absent GUI session or missing display, never an app bug.
    #: ``passed`` must be ``False`` whenever this is ``True``.
    unavailable: bool = False


@dataclass(frozen=True)
class BugbashRunRecord:
    """One ``coord bugbash`` run (:class:`coord.bugbash.BugbashReport`),
    reduced to what the release gate needs to grade it.

    ``verified`` mirrors :meth:`coord.bugbash.BugbashReport.rounds`'s own
    #2096/#3517 distinction: ``False`` means the terminating round's
    ``termination_reason`` was EITHER ``"lane_failure"`` (every lane explored
    that round failed to dispatch/poll/fetch its log) OR ``"protocol_error"``
    (#3517: a lane completed, but its final report was a malformed or
    missing ```` ```bugbash-findings ```` block — never a parseable, trustworthy
    "zero findings" answer). In both cases "zero new findings" was never
    actually OBSERVED — in the first case nothing answered at all, in the
    second something answered but the answer can't be trusted — so a run
    terminated either way must never read as a clean pass here. ``detail``
    is expected to say which of the two happened (surfaced verbatim in the
    gate step's own ``detail``, see :func:`_bugbash_step`), but this type
    intentionally does NOT split them into two booleans: the release gate
    only ever needs the single yes/no "was this genuinely observed clean"
    answer, and a caller building this from a real
    :class:`coord.bugbash.BugbashReport` sets ``verified=False`` for either
    ``termination_reason`` the exact same way (one question, one answer).
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


#: `coord.bugbash.BugbashReport` termination reasons that mean "this round's
#: zero-new-findings count was never actually OBSERVED" (#2096/#3517) — see
#: :func:`bugbash_run_record_from_report`, the ONE place that maps a real
#: bugbash run onto :attr:`BugbashRunRecord.verified`.
_UNVERIFIED_TERMINATION_REASONS = frozenset({"lane_failure", "protocol_error"})


def bugbash_run_record_from_report(report: Any, *, sha: str, ran_at: float = 0.0) -> BugbashRunRecord:
    """The ONE place that turns a real ``coord bugbash`` run
    (:class:`coord.bugbash.BugbashReport`) into this module's own
    :class:`BugbashRunRecord` (#2096/#3517 "one question, one answer") — so
    a caller wiring the production bugbash-journal store (the
    ``--from-json`` KNOWN GAP noted in ``coord.commands.release``) reads the
    SAME verdict this module's own tests exercise here, rather than
    reimplementing — and risking silently disagreeing with — what counts as
    a verified clean pass.

    Duck-typed on *report* (``termination_reason`` plus each round's
    ``new_count``) rather than importing :class:`coord.bugbash.BugbashReport`
    directly — this module stays dependency-free per its own module
    docstring, and ``coord.bugbash`` has no reason to import
    ``coord.release_gate`` back.

    ``new_findings`` is the sum of every round's ``new_count`` — the
    non-duplicate (new/regression) findings actually OBSERVED, independent
    of whether each one went on to be filed. Using *filed* count instead
    would let a run where the operator declined the confirm-gate prompt
    (#3487) read as "zero new findings" even though something genuinely new
    was found — a gate that can be satisfied just by declining to file is a
    gate that can't fail (#2096).

    ``verified`` is ``False`` exactly when ``report.termination_reason`` is
    in :data:`_UNVERIFIED_TERMINATION_REASONS` (``"lane_failure"`` or
    ``"protocol_error"``, #3517) — every other termination reason
    (``"zero_findings"``, ``"cost_cap"``, ``"round_cap"``) reflects a round
    that was genuinely observed, even one that hit a cost/round cap while
    still finding new things.
    """
    new_findings = sum(getattr(r, "new_count", 0) for r in getattr(report, "rounds", ()))
    reason = getattr(report, "termination_reason", "")
    verified = reason not in _UNVERIFIED_TERMINATION_REASONS
    detail = "" if verified else f"terminated {reason!r} — not a verified clean pass"
    return BugbashRunRecord(
        sha=sha, new_findings=new_findings, verified=verified, ran_at=ran_at, detail=detail,
    )


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
    #: ``True`` when this step is blocking NOT because the app failed, but
    #: because the lane's own session precheck reported the host's GUI
    #: session/display locked or absent (#3510 — see
    #: :attr:`LaneResult.unavailable`). Always implies ``passed=False``: a
    #: step can never be both passing and unavailable. Lets a caller render
    #: "UNAVAILABLE — unlock the host" rather than "FAILED — debug the app"
    #: without losing the fact that the gate is still blocked either way.
    unavailable: bool = False


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

    @property
    def unavailable_steps(self) -> tuple[GateStepResult, ...]:
        """The subset of :attr:`failing_steps` blocking because a lane's GUI
        session/display was unavailable (#3510), not because the app
        actually failed — so a caller can label those distinctly from a
        genuine app-bug failure in its rendered output."""
        return tuple(s for s in self.steps if s.unavailable)


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
    if chosen.unavailable:
        # #3510: blocking, but distinctly labeled — a locked/absent GUI
        # session or missing display is an environment condition, not an
        # app bug, so this is reported `unavailable=True` rather than an
        # ordinary failed step, even though it still fails the gate
        # (#2096: a gate must be able to fail).
        return GateStepResult(
            name=f"lane:{lane}",
            passed=False,
            unavailable=True,
            detail=chosen.detail or (
                f"lane {lane!r} was unavailable at {release_sha!r} — no "
                "usable interactive/GUI session (locked or absent); unlock "
                "the host and re-run rather than debugging the app"
            ),
        )
    if chosen.passed:
        return GateStepResult(
            name=f"lane:{lane}", passed=True, detail=chosen.detail or "passed",
        )
    return GateStepResult(
        name=f"lane:{lane}",
        passed=False,
        detail=chosen.detail or f"lane {lane!r} failed at {release_sha!r}",
    )


def _nightly_artifact_step(
    artifact: str,
    release_sha: str,
    nightly_results: Sequence[NightlyArtifactResult],
) -> GateStepResult:
    """Grade one required nightly artifact (#3652) — deliberately the exact
    same logic as :func:`_lane_step`: no result at the release SHA is a
    FAILING step (never a default pass, #2096), a result at some OTHER SHA
    is "too stale to certify this release" rather than silently accepted,
    and among several results at the release SHA the most recently checked
    one wins."""
    at_release_sha = [
        r for r in nightly_results if r.artifact == artifact and r.sha == release_sha
    ]
    if not at_release_sha:
        any_result = [r for r in nightly_results if r.artifact == artifact]
        if any_result:
            stale = max(any_result, key=lambda r: r.checked_at or 0.0)
            return GateStepResult(
                name=f"nightly:{artifact}",
                passed=False,
                detail=(
                    f"most recent nightly real-platform smoke result for "
                    f"artifact {artifact!r} is at {stale.sha!r}, not the "
                    f"release SHA {release_sha!r} — too stale to certify "
                    "this release"
                ),
            )
        return GateStepResult(
            name=f"nightly:{artifact}",
            passed=False,
            detail=(
                f"no nightly real-platform smoke result recorded for "
                f"artifact {artifact!r}"
            ),
        )

    chosen = max(at_release_sha, key=lambda r: r.checked_at or 0.0)
    if chosen.unavailable:
        # #3510, mirrored from `_lane_step`: blocking, but distinctly
        # labeled — a locked/absent GUI session or missing display is an
        # environment condition, not an app bug, even though it still
        # fails the gate (#2096: a gate must be able to fail).
        return GateStepResult(
            name=f"nightly:{artifact}",
            passed=False,
            unavailable=True,
            detail=chosen.detail or (
                f"nightly artifact {artifact!r} was unavailable at "
                f"{release_sha!r} — no usable interactive/GUI session "
                "(locked or absent); unlock the host and re-run rather "
                "than debugging the app"
            ),
        )
    if chosen.passed:
        return GateStepResult(
            name=f"nightly:{artifact}", passed=True, detail=chosen.detail or "passed",
        )
    return GateStepResult(
        name=f"nightly:{artifact}",
        passed=False,
        detail=chosen.detail or (
            f"nightly real-platform smoke failed for artifact {artifact!r} "
            f"at {release_sha!r}"
        ),
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
    nightly_required: bool = False,
    required_nightly_artifacts: Sequence[str] = (),
    nightly_results: Sequence[NightlyArtifactResult] = (),
    sha_is_at_or_after: ShaComparator = _default_sha_at_or_after,
) -> ReleaseGateVerdict:
    """Evaluate the #3488 release gate for *repo* at *release_sha*.

    One :class:`GateStepResult` per required lane (named ``lane:<lane>``),
    plus one more named ``"bugbash"`` when *bugbash_required*, plus one more
    per *required_nightly_artifacts* entry (named ``nightly:<artifact>``,
    #3652 — the real-platform nightly smoke runner's own per-repo gate:
    "the latest nightly for this SHA is green across all shipped
    artifacts"). Every step is graded from an OBSERVATION passed in — this
    function never assumes a step passed because no data was given for it
    (see the module docstring for the #2096 rationale behind each failure
    mode; :func:`_nightly_artifact_step` applies the identical discipline
    :func:`_lane_step` already does, deliberately, so the two gates can
    never silently disagree about what "green" means — #2096 "one
    question, one answer").

    *nightly_required* is a second, independent guard on top of
    *required_nightly_artifacts* being non-empty — #3652 review: a
    :class:`~coord.config.ReleaseGateRepoConfig` not built by the YAML
    parser's own cross-check (:func:`coord.config._parse_release_gate`,
    which already refuses ``nightly: required`` with an empty
    ``nightly_artifacts``) could otherwise set ``nightly_required=True``
    with ``required_nightly_artifacts=()`` and get a silent VACUOUS PASS —
    zero nightly steps are added, so the gate reports green having
    evaluated nothing. Raises :class:`ValueError` on that combination
    instead (#2096 "a gate must be able to fail"). ``False`` (the default)
    never raises regardless of *required_nightly_artifacts*, so every
    pre-#3652 and bugbash-only/lane-only caller is unaffected.

    Callers: :func:`coord.commands.release._release_gate_evaluate` wires
    this to real Tier-2 lane fetchers, a real bugbash journal/git ancestry
    check, and (#3652) the nightly smoke runner's own results store;
    ``tests/test_release_gate.py`` calls it directly against fixtures.
    """
    if nightly_required and not required_nightly_artifacts:
        raise ValueError(
            f"evaluate_release_gate({repo!r}): nightly_required=True but "
            "required_nightly_artifacts is empty — refusing a vacuous "
            "pass (#2096: a gate must be able to fail)"
        )
    steps = [_lane_step(lane, release_sha, lane_results) for lane in required_lanes]
    if bugbash_required:
        steps.append(_bugbash_step(release_sha, bugbash_runs, sha_is_at_or_after))
    steps.extend(
        _nightly_artifact_step(artifact, release_sha, nightly_results)
        for artifact in required_nightly_artifacts
    )
    return ReleaseGateVerdict(repo=repo, release_sha=release_sha, steps=tuple(steps))
