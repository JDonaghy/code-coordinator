"""Nightly real-platform smoke runner — the pure decision core (#3652).

vimcode's v0.15.0 pre-release manual smoke found five bugs in about 30
minutes that CI, the Test stage, and an LLM bug-bash had all passed. Those
checks (``tests/smoke-spec/*.yaml`` in each app repo) only ran because a
human remembered to run them by hand. This module is the decision core for
making that happen on its own, every night:

1. **Which artifact, from where** (:func:`resolve_artifact_plan`) — build
   fresh from the configured integration branch, or fall back to
   downloading the most recent release's published artifact. Pure: the
   actual git-build/HTTP-download I/O is a caller's job.
2. **Which host runs it** (:func:`pick_nightly_host`) — reuses
   :func:`coord.smoke.pick_smoke_machine` rather than growing a second
   capability-matching implementation (#2096 "one question, one answer":
   "which machine can run this" must have exactly one answerer, shared
   with the Test-stage's own smoke dispatch).
3. **What a result MEANS** (:func:`classify_step`) — the one place that
   turns a real, timestamped observation into one of four verdicts: a
   clean green needs nobody's attention (no LLM dispatch, no filed issue —
   #3652 "no LLM on a green run"); a plain red needs a bug filed or an
   existing one updated; a red step already parked as ``known_bug:
   <repo>#<N>`` is expected and stays silent; and — the bidirectional half
   of that same gate — a step PARKED as a known bug that starts passing
   alerts so the parked issue can close. :func:`classify_step` refuses to
   classify an observation that was never actually taken (``checked_at is
   None``) — #2096 "unconfirmed success is a defect": a verdict must come
   from something OBSERVED, never a default.
4. **What to do about it** (:func:`process_nightly_step`) — files or
   updates exactly one issue per ``(spec, step)`` by routing through the
   SAME dedupe/file machinery every other coord-filed bug uses
   (:func:`coord.bugbash.dedupe_finding`/:func:`coord.bugbash.
   file_finding`), rather than a second, independent dedupe
   implementation that could silently disagree with bugbash's (#2096).

What this module deliberately does NOT do (tracked as follow-up, out of
this first slice's scope): drive an actual cron/scheduler trigger, run a
real build or a real GitHub release download, or persist a production
nightly-results store (:class:`coord.release_gate.NightlyArtifactResult`,
the #3652 release-gate step wired in ``coord/release_gate.py`` +
``coord/commands/release.py``, is the typed seam a real store's reader
fills in — the same ``--from-json`` seam ``coord release gate`` already
uses for Tier-2 lanes and bugbash runs).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Sequence

from coord.bugbash import (
    BugbashLane,
    CoordRunner,
    DedupeVerdict,
    Finding,
    dedupe_finding,
    file_finding,
)
from coord.smoke import SmokeMachineChoice, pick_smoke_machine

if TYPE_CHECKING:  # pragma: no cover - import-cycle-avoidance only
    from coord.config import Config
    from coord.models import Board


# ── artifact source: build fresh, or fall back to the last release ────────


class ArtifactSource(str, Enum):
    """How :func:`resolve_artifact_plan` decided to obtain the artifact."""

    BUILD = "build"
    DOWNLOAD = "download"


@dataclass(frozen=True)
class ArtifactPlan:
    """What :func:`resolve_artifact_plan` decided for one ``(repo,
    artifact)`` pair. Pure data — the actual build/download is a caller's
    I/O shell, not this module's job."""

    repo: str
    artifact: str
    source: ArtifactSource
    ref: str
    detail: str = ""


def resolve_artifact_plan(
    *,
    repo: str,
    artifact: str,
    integration_branch: str | None,
    latest_release_tag: str | None,
) -> ArtifactPlan:
    """Decide how the nightly runner obtains *artifact* for *repo* (#3652
    Wanted #1): build fresh from *integration_branch* when one is
    configured, else fall back to *latest_release_tag*.

    #2096 "a gate must be able to fail": a repo with NEITHER an
    integration branch NOR a known release tag has nothing to build or
    download, and this raises rather than returning a plan with an empty
    ``ref`` that a caller could mistake for "nothing to do here, treat it
    as green".
    """
    if integration_branch:
        return ArtifactPlan(
            repo=repo, artifact=artifact, source=ArtifactSource.BUILD,
            ref=integration_branch,
            detail=f"build {artifact!r} from integration branch {integration_branch!r}",
        )
    if latest_release_tag:
        return ArtifactPlan(
            repo=repo, artifact=artifact, source=ArtifactSource.DOWNLOAD,
            ref=latest_release_tag,
            detail=f"download {artifact!r} from release {latest_release_tag!r}",
        )
    raise ValueError(
        f"no integration branch and no release tag configured for "
        f"{repo!r}/{artifact!r} — nothing to build or download"
    )


# ── routing: reuse the Test-stage's own capability matcher ────────────────


def pick_nightly_host(
    required_caps: list[str], repo_name: str, board: "Board", config: "Config",
) -> SmokeMachineChoice | None:
    """Route a nightly real-platform run to a capable host (#3652 Wanted
    #1 "route it to a capable host").

    Deliberately a thin wrapper over :func:`coord.smoke.pick_smoke_machine`
    rather than a second capability-matching implementation — #2096 "one
    question, one answer": whether a given machine can run a given
    capability set must have exactly one answerer in this codebase, shared
    with the Test-stage's own smoke dispatch (:func:`coord.smoke.
    dispatch_smoke`) and Work-leg routing (:func:`coord.dispatch.
    route_work_by_capability`). A nightly run has no originating worker
    leg to prefer, so an empty worker-machine sentinel is passed with
    ``prefer_worker=False`` — every capable machine is ranked purely on
    idle/busy status, nothing is artificially favored.
    """
    return pick_smoke_machine(
        required_caps, repo_name, "", board, config, prefer_worker=False,
    )


# ── known-bug references ("<repo>#<N>") ───────────────────────────────────

_KNOWN_BUG_RE = re.compile(r"^\s*([^\s#]+)#(\d+)\s*$")


def parse_known_bug_ref(ref: str) -> tuple[str, int]:
    """Parse a ``known_bug: <repo>#<N>`` smoke-spec step reference.

    Raises :class:`ValueError` on anything else — a malformed reference
    must never silently resolve to "no known bug" (which would turn an
    expected-red step into one that pages someone every night) nor to some
    guessed repo/number (#2096: a gate must be able to fail loudly on bad
    input, not paper over it).
    """
    match = _KNOWN_BUG_RE.match(ref)
    if match is None:
        raise ValueError(
            f"known_bug ref {ref!r} is not '<repo>#<number>' "
            "(e.g. 'vimcode#1583')"
        )
    return match.group(1), int(match.group(2))


# ── one step's observation, and what it means ──────────────────────────


@dataclass(frozen=True)
class NightlyStepObservation:
    """One step of one spec's real-platform nightly run, as actually
    observed. ``evidence`` is free-form capture descriptions (screenshot
    path, file listing, timing numbers — #3652 Wanted #2) attached to
    whatever issue this step's result causes to be filed/updated.

    ``checked_at`` is mandatory in spirit even though the type allows
    ``None`` for construction convenience in tests — :func:`classify_step`
    refuses to classify an observation with no timestamp at all (#2096:
    "unconfirmed success is a defect" applies just as much to an
    unconfirmed-when-observed result as to an unconfirmed-whether-observed
    one).
    """

    repo: str
    spec: str
    step: str
    sha: str
    passed: bool
    detail: str = ""
    evidence: tuple[str, ...] = ()
    checked_at: float | None = None


class StepVerdictKind(str, Enum):
    """What :func:`classify_step` decided about one observed step."""

    #: Passed, no known_bug configured for this step — #3652 "no LLM on a
    #: green run": nothing more happens than recording the plain pass.
    GREEN_CLEAN = "green_clean"
    #: Passed, but this step IS configured as a known bug — the
    #: bidirectional half of the gate (#3652 Wanted #3): alert so the
    #: parked issue can close.
    GREEN_KNOWN_BUG_FIXED = "green_known_bug_fixed"
    #: Failed, and this step IS configured as a known bug — expected-red,
    #: stays silent (#3652 Wanted #3).
    RED_EXPECTED_KNOWN_BUG = "red_expected_known_bug"
    #: Failed, no known_bug configured — needs a bug filed or an existing
    #: one updated (#3652 Wanted #2).
    RED_NEEDS_FILING = "red_needs_filing"


@dataclass(frozen=True)
class StepVerdict:
    """The result of classifying one :class:`NightlyStepObservation`
    against its configured ``known_bug`` (or lack of one)."""

    observation: NightlyStepObservation
    known_bug: str | None
    kind: StepVerdictKind

    @property
    def alerts(self) -> bool:
        """Whether this step needs ANY action beyond the plain board/
        status-bar record (#3652 Wanted #2 "no LLM on a green run"). Only
        :attr:`StepVerdictKind.RED_NEEDS_FILING` (a genuinely new/ongoing
        failure) and :attr:`StepVerdictKind.GREEN_KNOWN_BUG_FIXED` (the
        parked bug can close) ever need :func:`process_nightly_step` to do
        anything — a clean green or an expected, still-red known bug
        never reach a runner call at all.
        """
        return self.kind in (
            StepVerdictKind.GREEN_KNOWN_BUG_FIXED,
            StepVerdictKind.RED_NEEDS_FILING,
        )


def classify_step(
    observation: NightlyStepObservation, known_bug: str | None,
) -> StepVerdict:
    """The ONE place that decides what a nightly step's result means
    (#2096 "one question, one answer") — every caller (CLI, scheduler,
    tests) classifies through here rather than re-deriving the same
    pass/known_bug logic ad hoc.

    Raises :class:`ValueError` when *observation* carries no
    ``checked_at`` — #2096 "unconfirmed success is a defect": a verdict
    may only be built from something actually observed.
    """
    if observation.checked_at is None:
        raise ValueError(
            f"nightly step observation for {observation.repo}/"
            f"{observation.spec}::{observation.step} has no checked_at — "
            "refusing to classify an unobserved result "
            "(#2096: unconfirmed success is a defect)"
        )
    if observation.passed:
        kind = (
            StepVerdictKind.GREEN_KNOWN_BUG_FIXED
            if known_bug
            else StepVerdictKind.GREEN_CLEAN
        )
    else:
        kind = (
            StepVerdictKind.RED_EXPECTED_KNOWN_BUG
            if known_bug
            else StepVerdictKind.RED_NEEDS_FILING
        )
    return StepVerdict(observation=observation, known_bug=known_bug, kind=kind)


def classify_nightly_run(
    observations: Sequence[NightlyStepObservation],
    known_bugs: dict[tuple[str, str], str],
) -> list[StepVerdict]:
    """Classify every step of one nightly run (#3652).

    *known_bugs* maps ``(spec, step)`` to a ``"<repo>#<N>"`` reference —
    the smoke-spec's own ``known_bug:`` declarations, already resolved to
    a dict by the caller's spec parser. A step with no entry is treated as
    "not a known bug" (``known_bugs.get(..., None)``), never as "skip it" —
    every observation passed in gets exactly one verdict back, in order.
    """
    return [
        classify_step(obs, known_bugs.get((obs.spec, obs.step)))
        for obs in observations
    ]


# ── acting on a verdict: file, update, or close — never a second dedupe ──


def finding_from_step(verdict: StepVerdict, *, platform: str = "nightly-smoke") -> Finding:
    """Build a :class:`coord.bugbash.Finding` for a
    :attr:`StepVerdictKind.RED_NEEDS_FILING` step, so it is dedup'd and
    filed through the EXACT SAME path every other coord-filed bug uses
    (:func:`coord.bugbash.dedupe_finding`/:func:`coord.bugbash.
    file_finding`) — #2096 "one question, one answer": the nightly runner
    must never grow a second, independently-drifting title-similarity
    dedupe or issue-filing implementation.
    """
    if verdict.kind is not StepVerdictKind.RED_NEEDS_FILING:
        raise ValueError(
            f"finding_from_step called on a {verdict.kind.value} step — "
            "only RED_NEEDS_FILING steps are findings"
        )
    obs = verdict.observation
    title = f"nightly smoke: {obs.spec}::{obs.step} failing on {platform}"
    evidence = "\n".join(obs.evidence) if obs.evidence else "no evidence captured"
    return Finding(
        title=title,
        platform=platform,
        repo=obs.repo,
        suspected_repo=obs.repo,
        expected=f"spec {obs.spec!r} step {obs.step!r} passes at {obs.sha}",
        actual=obs.detail or "step failed",
        repro=(
            f"run the real-platform nightly spec {obs.spec!r} against "
            f"{obs.repo}@{obs.sha} on a capable host"
        ),
        evidence=evidence,
        captures=obs.evidence,
    )


@dataclass(frozen=True)
class NightlyStepOutcome:
    """What :func:`process_nightly_step` actually did for one verdict."""

    verdict: StepVerdict
    action: str = "none"  # "none" | "filed" | "commented" | "closed" |
    #                       "would-file" | "would-comment" | "would-close"
    issue_number: int | None = None


def process_nightly_step(
    verdict: StepVerdict,
    *,
    open_issues: Sequence[dict] = (),
    closed_issues: Sequence[dict] = (),
    lane: BugbashLane | None = None,
    runner: CoordRunner | None = None,
    dry_run: bool = True,
    platform: str = "nightly-smoke",
) -> NightlyStepOutcome:
    """Act on one classified step (#3652 Wanted #2/#3).

    - A non-:attr:`~StepVerdict.alerts` verdict (clean green, or an
      expected-red known bug) never calls *runner* at all — ``action=
      "none"``. This is the literal mechanism behind "no LLM on a green
      run": there is no code path from a clean pass to any dispatch.
    - :attr:`StepVerdictKind.RED_NEEDS_FILING` is deduped
      (:func:`coord.bugbash.dedupe_finding`) against *open_issues*/
      *closed_issues*. A brand-new or regression finding is filed through
      :func:`coord.bugbash.file_finding` (the SAME path every bugbash
      finding takes). A match against an already-OPEN issue is never
      re-filed — instead a fresh-evidence comment is posted to that SAME
      issue, so "repeated failures update the same issue rather than
      filing new ones" (#3652 Wanted #2) is literal behaviour, not merely
      "doesn't duplicate".
    - :attr:`StepVerdictKind.GREEN_KNOWN_BUG_FIXED` posts a comment
      recording the clean pass and closes the parked issue — the
      bidirectional half of the known-bug gate (#3652 Wanted #3).

    ``dry_run=True`` (the default) never calls *runner* — same "a dry run
    files/comments/closes nothing" guarantee :func:`coord.bugbash.
    file_finding` already gives, extended to the comment/close actions
    this module adds.
    """
    if not verdict.alerts:
        return NightlyStepOutcome(verdict=verdict, action="none")

    obs = verdict.observation

    if verdict.kind is StepVerdictKind.GREEN_KNOWN_BUG_FIXED:
        if not verdict.known_bug:
            raise ValueError(
                "a GREEN_KNOWN_BUG_FIXED verdict must carry a known_bug ref"
            )
        repo_name, number = parse_known_bug_ref(verdict.known_bug)
        if dry_run:
            return NightlyStepOutcome(
                verdict=verdict, action="would-close", issue_number=number,
            )
        if runner is None:
            raise ValueError("process_nightly_step needs a runner to close an issue")
        runner([
            "issue", "comment", repo_name, str(number),
            "--body",
            (
                f"Nightly real-platform smoke ({obs.spec}::{obs.step}) passed "
                f"at {obs.sha} — this known bug appears fixed. Closing."
            ),
        ])
        runner(["issue", "close", repo_name, str(number)])
        return NightlyStepOutcome(verdict=verdict, action="closed", issue_number=number)

    # StepVerdictKind.RED_NEEDS_FILING
    finding = finding_from_step(verdict, platform=platform)
    dedupe = dedupe_finding(finding, list(open_issues), list(closed_issues))

    if dedupe.verdict is DedupeVerdict.DUPLICATE and dedupe.matched_number is not None:
        if dry_run:
            return NightlyStepOutcome(
                verdict=verdict, action="would-comment",
                issue_number=dedupe.matched_number,
            )
        if runner is None:
            raise ValueError("process_nightly_step needs a runner to comment on an issue")
        runner([
            "issue", "comment", finding.repo, str(dedupe.matched_number),
            "--body",
            (
                f"Nightly real-platform smoke ({obs.spec}::{obs.step}) failed "
                f"again at {obs.sha} — {obs.detail or 'see evidence below'}.\n\n"
                f"Evidence:\n{finding.evidence}"
            ),
        ])
        return NightlyStepOutcome(
            verdict=verdict, action="commented", issue_number=dedupe.matched_number,
        )

    if dry_run:
        return NightlyStepOutcome(verdict=verdict, action="would-file")
    if runner is None:
        raise ValueError("process_nightly_step needs a runner to file an issue")
    result = file_finding(finding, dedupe, lane, runner, dry_run=False)
    return NightlyStepOutcome(verdict=verdict, action="filed", issue_number=result.issue_number)
