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
   :func:`coord.smoke.rank_smoke_machines` rather than growing a second
   capability-matching implementation (#2096 "one question, one answer":
   "which machine can run this" must have exactly one answerer, shared
   with the Test-stage's own smoke dispatch), and walks the full ranked
   list cross-referenced against each candidate's live ``/health`` probe
   (:func:`coord.smoke._capability_probe_reasons`) rather than trusting a
   single head pick (#1678).
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
   updates exactly one issue per ``(spec, step)`` by filing through the
   SAME path every other coord-filed bug uses (:func:`coord.bugbash.
   file_finding`), but with an EXACT ``(spec, step)``-keyed dedupe
   decision of its own (:func:`_dedupe_nightly_finding`) rather than
   :func:`coord.bugbash.dedupe_finding`'s fuzzy title-similarity scoring —
   that scorer is right for human-prose bugbash titles but wrong for these
   machine-generated ones, which already carry an unambiguous key.

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

import httpx

from coord.bugbash import (
    BugbashLane,
    CoordRunner,
    DedupeResult,
    DedupeVerdict,
    Finding,
    file_finding,
    finding_target_repo,
)
from coord.smoke import (
    SmokeMachineChoice,
    _capability_probe_reasons,
    rank_smoke_machines,
)

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
    required_caps: list[str],
    repo_name: str,
    board: "Board",
    config: "Config",
    *,
    http_client: "httpx.Client | None" = None,
) -> SmokeMachineChoice | None:
    """Route a nightly real-platform run to a capable host (#3652 Wanted
    #1 "route it to a capable host").

    Reuses :func:`coord.smoke.rank_smoke_machines` for capability matching
    rather than a second capability-matching implementation — #2096 "one
    question, one answer": whether a given machine can run a given
    capability set must have exactly one answerer in this codebase, shared
    with the Test-stage's own smoke dispatch (:func:`coord.smoke.
    dispatch_smoke`) and Work-leg routing (:func:`coord.dispatch.
    route_work_by_capability`). A nightly run has no originating worker
    leg to prefer, so an empty worker-machine sentinel is passed with
    ``prefer_worker=False`` — every capable machine is ranked purely on
    idle/busy status, nothing is artificially favored.

    Unlike a bare head-pick, this walks the FULL ranked candidate list and
    cross-references each one's live ``/health`` probe (:func:`coord.smoke.
    _capability_probe_reasons`) before accepting it — exactly what
    :func:`coord.smoke.dispatch_smoke` does for every other smoke dispatch,
    and for the same reason (#1678): on 2026-08-01 the router picked the
    same unhealthy machine every 30s forever while two other machines
    declared the identical capability and were never tried. A nightly
    real-platform runner is the single most probe-sensitive caller there
    is — CLAUDE.md records a `browser` capability that has read UNMET
    since that incident — so it must never settle for a single unverified
    pick. A candidate whose probe disagrees with its declared capabilities
    is skipped, not fatal: the next capable candidate is tried, same as
    the Test stage's own routing.
    """
    candidates = rank_smoke_machines(
        required_caps, repo_name, "", board, config, prefer_worker=False,
    )
    for choice in candidates:
        if required_caps:
            unmet = _capability_probe_reasons(
                choice.machine, required_caps, http_client=http_client,
            )
            if unmet:
                continue
        return choice
    return None


# ── known-bug references ("<repo>#<N>") ───────────────────────────────────

# #3652 review: the repo group deliberately excludes `/` as well as
# whitespace/`#` — `coord issue comment|close REPO ...` resolves REPO
# against the LOCAL name under `repos:` in coordinator.yml
# (`coord/commands/issues.py`), never a GitHub `owner/repo` slug. Allowing
# `/` here let `owner/repo#12` parse "successfully" into a repo name that
# then fails at `coord issue` call time with a confusing runtime error,
# even though the parser itself looked happy. Rejecting it here, at parse
# time, with the example in the message, is cheaper than that trip.
_KNOWN_BUG_RE = re.compile(r"^\s*([^\s#/]+)#(\d+)\s*$")


def parse_known_bug_ref(ref: str) -> tuple[str, int]:
    """Parse a ``known_bug: <repo>#<N>`` smoke-spec step reference, where
    ``<repo>`` is the LOCAL name under ``repos:`` in coordinator.yml (never
    a GitHub ``owner/repo`` slug — see :data:`_KNOWN_BUG_RE`'s comment).

    Raises :class:`ValueError` on anything else — a malformed reference
    must never silently resolve to "no known bug" (which would turn an
    expected-red step into one that pages someone every night) nor to some
    guessed repo/number (#2096: a gate must be able to fail loudly on bad
    input, not paper over it).
    """
    match = _KNOWN_BUG_RE.match(ref)
    if match is None:
        raise ValueError(
            f"known_bug ref {ref!r} is not '<repo>#<number>' using the "
            "LOCAL repo name from coordinator.yml's 'repos:' (e.g. "
            "'vimcode#1583' — not a GitHub 'owner/repo#1583' slug)"
        )
    return match.group(1), int(match.group(2))


# ── one step's observation, and what it means ──────────────────────────


@dataclass(frozen=True)
class NightlyStepObservation:
    """One step of one spec's real-platform nightly run, as actually
    observed. ``evidence`` is free-form capture descriptions (screenshot
    path, file listing, timing numbers — #3652 Wanted #2) attached to
    whatever issue this step's result causes to be filed/updated.

    ``checked_at`` is mandatory in spirit — :func:`classify_step` refuses
    to classify an observation with no timestamp at all (#2096:
    "unconfirmed success is a defect" applies just as much to an
    unconfirmed-when-observed result as to an unconfirmed-whether-observed
    one) — and is enforced at construction time, not merely by that later
    runtime check: it has no default, so a caller that forgets it gets a
    ``TypeError`` immediately rather than a plausible-looking observation
    that only fails much later, inside :func:`classify_step`. The type
    stays ``float | None`` (an explicit ``checked_at=None`` is how a test
    deliberately builds the unobserved case :func:`classify_step` must
    reject) — only the convenience DEFAULT is gone. Tests get the same
    ergonomics back via a factory (``_obs`` in
    ``tests/test_nightly_smoke.py``) rather than a type-level escape hatch
    every production caller also inherits.
    """

    repo: str
    spec: str
    step: str
    sha: str
    passed: bool
    checked_at: float | None
    detail: str = ""
    evidence: tuple[str, ...] = ()


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


def _nightly_key(spec: str, step: str) -> str:
    """The exact, unambiguous key embedded in every nightly finding's title
    (#3652 review) so "exactly one issue per (spec, step)" is a literal
    guarantee rather than a hope.

    :func:`coord.bugbash.dedupe_finding`'s ``_best_match`` scores Jaccard
    word-set similarity of the tag-stripped title — right for human-prose
    bugbash titles, but wrong here: splitting ``install.yaml::launch`` into
    bare words makes it score 0.80 against ``install.yaml::uninstall``,
    comfortably above ``DEFAULT_DEDUPE_THRESHOLD``, so two genuinely
    different steps of the same spec collapse into one issue. This key is
    matched as a literal substring (:func:`_find_by_nightly_key`), never
    scored for overlap, so one (spec, step) matches only itself.
    """
    return f"[nightly:{spec}::{step}]"


def _find_by_nightly_key(key: str, issues: Sequence[dict]) -> dict | None:
    """The first issue in *issues* whose title carries *key* verbatim, or
    ``None``. Order-preserving, first match wins — callers pass in
    open/closed issue lists already ordered however their fetch returned
    them; this makes no further assumption about freshness."""
    for issue in issues:
        if key in str(issue.get("title", "")):
            return issue
    return None


def _dedupe_nightly_finding(
    finding: Finding,
    key: str,
    open_issues: Sequence[dict],
    closed_issues: Sequence[dict],
) -> DedupeResult:
    """The nightly runner's OWN "is this the same finding?" decision —
    exact-key lookup, not :func:`coord.bugbash.dedupe_finding`'s fuzzy
    title-similarity scoring (see :func:`_nightly_key`'s docstring for why
    that scorer is the wrong tool for a machine-generated, already-unique
    key). Open issues are checked first, same precedence
    :func:`coord.bugbash.dedupe_finding` uses: an OPEN match is the
    actionable answer regardless of some unrelated closed issue also
    carrying the key.
    """
    open_match = _find_by_nightly_key(key, open_issues)
    if open_match is not None:
        return DedupeResult(
            verdict=DedupeVerdict.DUPLICATE,
            matched_number=open_match.get("number"),
            matched_title=open_match.get("title"),
            score=1.0,
        )
    closed_match = _find_by_nightly_key(key, closed_issues)
    if closed_match is not None:
        return DedupeResult(
            verdict=DedupeVerdict.REGRESSION,
            matched_number=closed_match.get("number"),
            matched_title=closed_match.get("title"),
            score=1.0,
        )
    return DedupeResult(verdict=DedupeVerdict.NEW)


def finding_from_step(verdict: StepVerdict, *, platform: str = "nightly-smoke") -> Finding:
    """Build a :class:`coord.bugbash.Finding` for a
    :attr:`StepVerdictKind.RED_NEEDS_FILING` step, so it is filed through
    the EXACT SAME path every other coord-filed bug uses
    (:func:`coord.bugbash.file_finding`) — #2096 "one question, one
    answer": the nightly runner must never grow a second, independently
    drifting issue-filing implementation. The title carries
    :func:`_nightly_key`'s exact ``(spec, step)`` key — reused for the
    runner's OWN dedupe decision (:func:`_dedupe_nightly_finding`), not
    :func:`coord.bugbash.dedupe_finding`'s fuzzy scoring. The resulting
    issue title still reads like a bugbash finding
    (``compose_finding_issue_title`` prefixes ``[bugbash:<platform>]``)
    because filing genuinely does reuse bugbash's own path — that's a
    deliberate trade of a slightly odd-looking title for not maintaining a
    second filer.
    """
    if verdict.kind is not StepVerdictKind.RED_NEEDS_FILING:
        raise ValueError(
            f"finding_from_step called on a {verdict.kind.value} step — "
            "only RED_NEEDS_FILING steps are findings"
        )
    obs = verdict.observation
    key = _nightly_key(obs.spec, obs.step)
    title = f"{key} nightly smoke: {obs.spec}::{obs.step} failing on {platform}"
    # #3652 review: `captures` already carries `obs.evidence` verbatim, and
    # `_evidence_with_acceptance` renders it again as a "Captures: ..." line
    # — setting `evidence` to the SAME joined tuple would print it a third
    # time. `evidence` is left to describe "was anything captured at all";
    # the captures themselves are `captures`'s job alone.
    evidence = "" if obs.evidence else "no evidence captured"
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
    open_issues: Sequence[dict],
    closed_issues: Sequence[dict],
    lane: BugbashLane | None = None,
    runner: CoordRunner | None = None,
    dry_run: bool = True,
    platform: str | None = None,
) -> NightlyStepOutcome:
    """Act on one classified step (#3652 Wanted #2/#3).

    - A non-:attr:`~StepVerdict.alerts` verdict (clean green, or an
      expected-red known bug) never calls *runner* at all — ``action=
      "none"``. This is the literal mechanism behind "no LLM on a green
      run": there is no code path from a clean pass to any dispatch.
    - :attr:`StepVerdictKind.RED_NEEDS_FILING` is deduped by EXACT
      ``(spec, step)`` key (:func:`_dedupe_nightly_finding`), against
      *open_issues*/*closed_issues* — both required, never defaulted, so a
      caller can never silently feed an empty corpus and have every
      finding read as brand new. A brand-new or regression finding is
      filed through :func:`coord.bugbash.file_finding` (the SAME path
      every bugbash finding takes). A match against an already-OPEN issue
      is never re-filed — instead a fresh-evidence comment is posted to
      that SAME issue, so "repeated failures update the same issue rather
      than filing new ones" (#3652 Wanted #2) is literal behaviour, not
      merely "doesn't duplicate" — and a DIFFERENT step of the same spec
      is never folded into it, because the key is exact, not scored.
    - :attr:`StepVerdictKind.GREEN_KNOWN_BUG_FIXED` posts a comment
      recording the clean pass and closes the parked issue — the
      bidirectional half of the known-bug gate (#3652 Wanted #3).

    *platform* defaults to *lane*'s own ``platform`` (its documented
    "display/dedupe label") when a lane is given, else the literal string
    ``"nightly-smoke"`` — never an independent default that could disagree
    with the lane actually doing the run. Passing *platform* explicitly
    always wins. This matters beyond labelling: with a careless constant
    default, the identical step failing on macOS and on GTK Linux would
    dedupe into ONE issue, directly contradicting `_best_match`'s own
    documented rationale that an identically-titled finding on two
    different platforms is two different bugs, not one.

    ``dry_run=True`` (the default) never calls *runner* — same "a dry run
    files/comments/closes nothing" guarantee :func:`coord.bugbash.
    file_finding` already gives, extended to the comment/close actions
    this module adds.
    """
    if not verdict.alerts:
        return NightlyStepOutcome(verdict=verdict, action="none")

    obs = verdict.observation
    effective_platform = platform if platform is not None else (
        lane.platform if lane is not None else "nightly-smoke"
    )

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
    finding = finding_from_step(verdict, platform=effective_platform)
    key = _nightly_key(obs.spec, obs.step)
    dedupe = _dedupe_nightly_finding(finding, key, open_issues, closed_issues)
    target_repo = finding_target_repo(finding)

    if dedupe.verdict is DedupeVerdict.DUPLICATE and dedupe.matched_number is not None:
        if dry_run:
            return NightlyStepOutcome(
                verdict=verdict, action="would-comment",
                issue_number=dedupe.matched_number,
            )
        if runner is None:
            raise ValueError("process_nightly_step needs a runner to comment on an issue")
        runner([
            "issue", "comment", target_repo, str(dedupe.matched_number),
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
    # #3652 review: derive the outcome from what `file_finding` actually
    # OBSERVED, never from "no exception was raised" — `file_finding`
    # short-circuits with `filed=False` for a DUPLICATE verdict (reachable
    # here whenever `dedupe.matched_number` was `None`, e.g. an issue dict
    # with no `"number"` key), and reporting `action="filed"` with a real
    # `issue_number=None` in that case would be a fabricated success.
    action = "filed" if result.filed and result.issue_number is not None else "none"
    return NightlyStepOutcome(verdict=verdict, action=action, issue_number=result.issue_number)
