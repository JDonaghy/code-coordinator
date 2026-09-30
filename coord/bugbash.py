"""`coord bugbash` (#3487): per-platform find -> dedupe -> file -> queue loop.

On 2026-09-29 the operator and a coordinator session found more than a dozen
real cross-platform bugs in about an hour by driving vimcode on Windows and
macOS by hand, comparing screenshots, and filing what differed. This module
is that loop, automated: for each platform lane declared by a repo's
acceptance drivers (``tui-pty`` / ``win-native`` / ``mac-native`` /
``gtk-native``, :data:`LANE_DRIVER_KINDS`), dispatch a headless worker to the
capable machine, have it run the Tier-2 smoke spec plus an exploration
checklist (:data:`EXPLORATION_CHECKLIST`) against the real app, and compare
what it sees against a declared reference backend. Findings come back as
structured data (:class:`Finding`), get deduped against open *and recently
closed* issues (:func:`dedupe_finding` — a closed issue whose symptom
reappears is a regression, not a new bug: that is how vimcode#1583 would
have been caught), and survivors are filed through ``coord issue create``
then queued through ``coord drive-queue add --machine <lane host>``
(:func:`file_finding`). The whole thing repeats round over round
(:func:`run_bugbash`) until a round finds nothing new, or a round/cost cap
fires.

Two seams keep the engine (this module) testable without a live fleet:

- **Explorer** (``Callable[[BugbashLane, int], ExploreOutcome]``) — "go run
  this lane's exploration and hand back findings." The production
  implementation (wired by ``coord/commands/bugbash.py``) dispatches a
  headless worker and parses its final ```` ```bugbash-findings ```` fenced
  JSON block (:func:`parse_findings_block`) out of its transcript; tests
  inject a fake that returns canned :class:`ExploreOutcome` values.
- **CoordRunner** (``Callable[[Sequence[str]], str]``) — "run this `coord`
  subcommand and hand back its stdout," used ONLY for the two mutating
  calls this module ever makes: ``coord issue create`` and ``coord
  drive-queue add`` (:func:`file_finding`). The production implementation
  (:func:`subprocess_coord_runner`) shells out to the real ``coord``
  binary; tests inject a fake that records calls instead of touching
  GitHub or the drive queue. ``--dry-run`` (:func:`file_finding` with
  ``dry_run=True``) never calls the runner at all — the strongest
  guarantee that a dry run files nothing is that the seam that could file
  something is never invoked.

Dedupe, filing, and the round loop are pure/seam-driven and unit-tested in
``tests/test_bugbash.py``. Lane *discovery* (:func:`discover_lanes`) reads
``coordinator.yml`` (:class:`coord.config.AcceptanceConfig` /
:class:`coord.config.Config`) the same way :mod:`coord.smoke`'s capability
routing does — a lane only exists when some configured machine actually
carries the driver's declared ``capability``, so a repo with no capable
machine for a platform silently gets no lane rather than a lane that can
never run.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Sequence

from coord.bug_intake import format_bug_report

# ── constants ────────────────────────────────────────────────────────────

#: Acceptance-driver ``kind`` values that represent a real, on-device
#: platform lane bugbash can explore — i.e. the drivers that put real bytes
#: in front of a real OS/terminal, as opposed to the in-process
#: ``tui-tuidriver`` Tier-1 oracle. Kept in sync with
#: :data:`coord.acceptance_drivers.SUPPORTED_KINDS`'s native-tier subset by
#: hand — a kind not yet implemented there simply never gets a capable
#: machine in :func:`discover_lanes`, so this list can stay ahead of the
#: adapters landing without producing a lane that can't actually run.
LANE_DRIVER_KINDS: tuple[str, ...] = (
    "tui-pty", "win-native", "mac-native", "gtk-native",
)

#: The exploration checklist every lane walks on top of the repo's Tier-2
#: smoke spec (issue #3487's "panels, menus, extension install flow,
#: terminal, splits, themes, idle stability"). Ordered so a worker that runs
#: out of budget mid-checklist still covers the highest-signal areas first.
EXPLORATION_CHECKLIST: tuple[str, ...] = (
    "panels", "menus", "extension install flow", "terminal", "splits",
    "themes", "idle stability",
)

#: Fenced-code-block language tag a lane worker's final message must use to
#: report its findings — mirrors how :mod:`coord.acceptance_drivers` forces
#: each framework's own structured report format rather than parsing prose.
FINDINGS_FENCE = "bugbash-findings"

#: Mandatory acceptance line stamped onto every filed finding's issue body
#: (#3487 requirement 5): a bugbash finding must not be closeable by a fix
#: alone — it must also leave behind permanent automated coverage so the
#: same symptom reappearing later is an automatic regression, not another
#: hour of hand-driving the app.
ACCEPTANCE_LINE = (
    "The fix must add a Tier-1 shared conformance scenario or a Tier-2 "
    "smoke-spec step that fails first, covering this exact behaviour, "
    "before it is considered fixed."
)

#: Default title-similarity threshold for :func:`dedupe_finding` — see its
#: docstring for what the score measures.
DEFAULT_DEDUPE_THRESHOLD = 0.6

_ISSUE_NUMBER_RE = re.compile(r"#(\d+)")
_WORD_RE = re.compile(r"[a-z0-9]+")


# ── findings ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Finding:
    """One structured bugbash finding, as reported by a lane worker.

    ``platform`` is the lane name (e.g. ``"win-native"``); ``repo`` is the
    app repo the lane ran against; ``suspected_repo`` is the coordinator's
    best guess at which repo the fix belongs in (the app itself, or its UI
    framework, e.g. vimcode vs quadraui — #3487 requirement 2). The four
    bug-lane intake fields (``expected``/``actual``/``repro``/``evidence``)
    match :mod:`coord.bug_intake` exactly so a filed finding renders through
    the same addressable-section contract every other bug-lane issue does.
    """

    title: str
    platform: str
    repo: str
    suspected_repo: str
    expected: str
    actual: str
    repro: str
    evidence: str
    #: Paths/descriptions of captures (screenshots, probe dumps) backing
    #: this finding — folded into the issue's Evidence section, not a
    #: separate upload step (#3487 doesn't specify an artifact store).
    captures: tuple[str, ...] = ()


def parse_findings_block(text: str, *, platform: str, repo: str) -> list[Finding]:
    """Extract the ```` ```bugbash-findings ```` fenced JSON block from a lane
    worker's final message and turn it into :class:`Finding` objects.

    Tolerant of no block at all (a clean round — returns ``[]``) and of a
    block that fails to parse as a JSON list of objects (also ``[]``: a
    malformed report is not a reportable finding, mirroring
    :func:`coord.bug_intake.parse_bug_report`'s "partial isn't actionable"
    stance). Each object must carry ``title``/``expected``/``actual``/
    ``repro``/``evidence``; ``suspected_repo`` defaults to *repo* and
    ``captures`` defaults to ``[]`` when omitted.
    """
    pattern = re.compile(
        rf"```{re.escape(FINDINGS_FENCE)}\s*\n(.*?)```", re.DOTALL,
    )
    match = pattern.search(text)
    if match is None:
        return []
    try:
        raw = json.loads(match.group(1))
    except (ValueError, TypeError):
        return []
    if not isinstance(raw, list):
        return []

    findings: list[Finding] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        required = ("title", "expected", "actual", "repro", "evidence")
        if any(not str(entry.get(k, "")).strip() for k in required):
            continue
        captures = entry.get("captures") or []
        if not isinstance(captures, list):
            captures = []
        findings.append(
            Finding(
                title=str(entry["title"]).strip(),
                platform=platform,
                repo=repo,
                suspected_repo=str(entry.get("suspected_repo", repo)).strip() or repo,
                expected=str(entry["expected"]).strip(),
                actual=str(entry["actual"]).strip(),
                repro=str(entry["repro"]).strip(),
                evidence=str(entry["evidence"]).strip(),
                captures=tuple(str(c) for c in captures),
            )
        )
    return findings


# ── dedupe ───────────────────────────────────────────────────────────────


class DedupeVerdict(str, Enum):
    """What :func:`dedupe_finding` decided about one :class:`Finding`."""

    #: No sufficiently similar open OR closed issue — files as a new bug.
    NEW = "new"
    #: Matches an OPEN issue — already tracked, never re-filed.
    DUPLICATE = "duplicate"
    #: Matches a CLOSED issue — the symptom reappeared after being fixed.
    #: Filed as a regression report (referencing the closed issue), not
    #: silently dropped and not treated as brand new (#3487 requirement 3,
    #: "that is how vimcode#1583 would have been caught").
    REGRESSION = "regression"


@dataclass(frozen=True)
class DedupeResult:
    verdict: DedupeVerdict
    matched_number: int | None = None
    matched_title: str | None = None
    score: float = 0.0


def _title_tokens(title: str) -> set[str]:
    return set(_WORD_RE.findall(title.lower()))


def _title_similarity(a: str, b: str) -> float:
    """Jaccard similarity of *a* and *b*'s lowercased word-tokens.

    Cheap and dependency-free (no fuzzy-match library needed for a single
    title-vs-title comparison over a bounded issue list) — a pure word-set
    overlap is enough to catch "Extension install flow crashes on Windows"
    vs "Extension install crashes (Windows)" without a false-positive on
    two unrelated one-word-in-common titles, which a substring match would
    not distinguish.
    """
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _best_match(
    finding: Finding, issues: list[dict], *, threshold: float,
) -> tuple[dict, float] | None:
    """The highest-scoring issue in *issues* whose title clears *threshold*
    AND whose recorded platform (when present) matches *finding*'s — or
    ``None`` when nothing clears the bar.

    Platform-gating a title match matters here specifically: "Extension
    install flow crashes" on ``win-native`` and the identically-titled
    finding on ``mac-native`` are two different bugs, not one — an issue
    only gates the platform comparison when it actually names one (a
    ``[bugbash:<platform>]`` title prefix, :func:`compose_finding_issue_title`),
    so a hand-filed issue with no platform tag is still eligible to match
    on title alone.
    """
    best: tuple[dict, float] | None = None
    for issue in issues:
        title = str(issue.get("title", ""))
        issue_platform = _platform_from_title(title)
        if issue_platform is not None and issue_platform != finding.platform:
            continue
        score = _title_similarity(finding.title, title)
        if score >= threshold and (best is None or score > best[1]):
            best = (issue, score)
    return best


def _platform_from_title(title: str) -> str | None:
    m = re.match(r"^\[bugbash:([^\]]+)\]", title)
    return m.group(1) if m else None


def dedupe_finding(
    finding: Finding,
    open_issues: list[dict],
    closed_issues: list[dict],
    *,
    threshold: float = DEFAULT_DEDUPE_THRESHOLD,
) -> DedupeResult:
    """Decide whether *finding* is new, a duplicate of an open issue, or a
    regression of a closed one (#3487 requirement 3).

    Open issues are checked first — an OPEN match always wins even when a
    closed issue also scores higher, since "already tracked, still open" is
    the actionable answer regardless of some unrelated older closure. Both
    lists take plain ``{"number": int, "title": str, ...}`` dicts (exactly
    the shape ``coord.github_ops.get_open_issues`` / a closed-issue list
    fetch already returns) — this function never calls GitHub itself.
    """
    open_match = _best_match(finding, open_issues, threshold=threshold)
    if open_match is not None:
        issue, score = open_match
        return DedupeResult(
            verdict=DedupeVerdict.DUPLICATE,
            matched_number=issue.get("number"),
            matched_title=issue.get("title"),
            score=score,
        )
    closed_match = _best_match(finding, closed_issues, threshold=threshold)
    if closed_match is not None:
        issue, score = closed_match
        return DedupeResult(
            verdict=DedupeVerdict.REGRESSION,
            matched_number=issue.get("number"),
            matched_title=issue.get("title"),
            score=score,
        )
    return DedupeResult(verdict=DedupeVerdict.NEW)


# ── lanes ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BugbashLane:
    """One platform lane: a native/PTY acceptance driver paired with a
    specific capable machine to run it on."""

    platform: str
    driver_kind: str
    machine: str
    capability: str
    reference: bool = False


def discover_lanes(config: Any, repo_name: str, *, reference_backend: str = "") -> list[BugbashLane]:
    """Derive *repo_name*'s bugbash lanes from its acceptance drivers,
    routed to a capable machine the same way
    :mod:`coord.smoke`'s ``capability_rules`` routes smoke legs.

    Walks the repo's top-level driver plus every ``routes:`` entry (mirrors
    :meth:`coord.config.AcceptanceConfig.entrypoints`'s walk), keeps only
    :data:`LANE_DRIVER_KINDS` entries, and drops any that has no configured
    machine listing both *repo_name* and the driver's ``capability`` — a
    lane with no capable machine is omitted rather than returned with
    ``machine=""``, so a caller never has to separately check "is this lane
    actually runnable." *reference_backend*, when it names one of the
    surviving lanes' ``platform``, marks that lane's ``reference=True``.
    """
    entry = config.acceptance.drivers.get(repo_name)
    if entry is None:
        return []
    candidates = list(entry.routes) if entry.routes else [entry]

    lanes: list[BugbashLane] = []
    for cfg in candidates:
        if cfg.kind not in LANE_DRIVER_KINDS:
            continue
        machine = _pick_lane_machine(config, repo_name, cfg.capability)
        if machine is None:
            continue
        lanes.append(
            BugbashLane(
                platform=cfg.kind,
                driver_kind=cfg.kind,
                machine=machine,
                capability=cfg.capability,
                reference=(cfg.kind == reference_backend),
            )
        )
    return lanes


def _pick_lane_machine(config: Any, repo_name: str, capability: str) -> str | None:
    for m in config.machines:
        if repo_name in m.repos and (not capability or capability in m.capabilities):
            return m.name
    return None


def build_exploration_briefing(
    lane: BugbashLane,
    *,
    reference_backend: str,
    checklist: Sequence[str] = EXPLORATION_CHECKLIST,
) -> str:
    """Compose the seed briefing for a lane's headless exploration worker.

    Tells the worker to run the repo's Tier-2 smoke spec first, then walk
    *checklist* against the real app, comparing behaviour to
    *reference_backend* and capturing evidence through the native driver's
    own probes. Ends with the exact contract :func:`parse_findings_block`
    parses back out, so the briefing and the parser can never silently
    drift apart (one constant, :data:`FINDINGS_FENCE`, used by both).
    """
    lines = [
        f"=== coord bugbash: {lane.platform} lane ===",
        "",
        f"Reference backend for comparison: {reference_backend or '(none configured)'}",
        "",
        "1. Run this repo's Tier-2 smoke spec for this driver to completion.",
        "2. Then walk the exploration checklist below on the real app, using "
        "the native driver's own probes/captures as evidence:",
    ]
    for item in checklist:
        lines.append(f"   - {item}")
    lines += [
        "",
        "For anything that behaves differently from the reference backend, "
        "or crashes, hangs, or renders wrong, report it as a finding.",
        "",
        "When done, end your final message with a fenced "
        f"```{FINDINGS_FENCE}``` block containing a JSON array of finding "
        "objects, each with: title, expected, actual, repro, evidence, "
        "suspected_repo (the app or its UI framework), captures (list of "
        "capture paths/descriptions, may be empty). An empty array means "
        "zero findings this round.",
    ]
    return "\n".join(lines)


# ── explorer / runner seams ──────────────────────────────────────────────


@dataclass(frozen=True)
class ExploreOutcome:
    """What one lane's exploration round produced: its findings, plus the
    cost spent producing them (whatever unit :class:`BugbashConfig`'s cost
    caps are denominated in — turns, dollars, minutes; this module is
    agnostic, it just accumulates and compares)."""

    findings: tuple[Finding, ...] = ()
    cost: float = 0.0
    notes: str = ""


#: ``(lane, round_num) -> ExploreOutcome`` — "go run this lane's
#: exploration round and hand back what it found." The production
#: implementation lives in ``coord/commands/bugbash.py`` (dispatch +
#: transcript parsing); tests inject a fake.
Explorer = Callable[[BugbashLane, int], ExploreOutcome]

#: ``(argv) -> stdout`` — "run this `coord` subcommand." Used only for
#: ``coord issue create`` / ``coord drive-queue add`` (:func:`file_finding`).
CoordRunner = Callable[[Sequence[str]], str]


def subprocess_coord_runner(args: Sequence[str]) -> str:
    """Production :data:`CoordRunner`: shells out to the real ``coord``
    binary and returns its stdout. Raises ``RuntimeError`` (with stderr
    folded in) on a non-zero exit — a failed file/queue call must not be
    silently swallowed, since that would report a finding as handled when
    it never actually made it onto GitHub or the drive queue."""
    proc = subprocess.run(
        ["coord", *args], capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"coord {' '.join(args)} failed (exit {proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout


# ── filing ───────────────────────────────────────────────────────────────


def compose_finding_issue_title(finding: Finding) -> str:
    """``[bugbash:<platform>] <title>`` — the platform tag is what lets a
    later :func:`dedupe_finding` pass restrict its title match to the same
    platform (:func:`_platform_from_title`)."""
    return f"[bugbash:{finding.platform}] {finding.title}"


def _evidence_with_acceptance(finding: Finding, dedupe: DedupeResult) -> str:
    parts = [finding.evidence.strip()] if finding.evidence.strip() else []
    if finding.captures:
        parts.append("Captures: " + ", ".join(finding.captures))
    if dedupe.verdict is DedupeVerdict.REGRESSION and dedupe.matched_number is not None:
        parts.append(
            f"Regression: reopens the symptom from closed issue "
            f"#{dedupe.matched_number} ({dedupe.matched_title or ''}).".strip()
        )
    parts.append(f"Acceptance: {ACCEPTANCE_LINE}")
    return "\n\n".join(parts)


@dataclass(frozen=True)
class FilingResult:
    finding: Finding
    verdict: DedupeVerdict
    filed: bool = False
    queued: bool = False
    issue_number: int | None = None
    #: Set when ``dry_run=True`` (or a duplicate — nothing to preview
    #: beyond the existing match) — the title/body that WOULD have been
    #: filed, so a dry run's report can show exactly what a real run would
    #: do without ever calling the runner.
    preview_title: str | None = None
    preview_body: str | None = None


def file_finding(
    finding: Finding,
    dedupe: DedupeResult,
    lane: BugbashLane | None,
    runner: CoordRunner,
    *,
    dry_run: bool,
) -> FilingResult:
    """File and queue one non-duplicate finding (#3487 requirement 4).

    A :data:`DedupeVerdict.DUPLICATE` finding is never filed — the runner
    is not called at all, and the result just carries the existing issue
    number it matches. A :data:`DedupeVerdict.NEW` or
    :data:`DedupeVerdict.REGRESSION` finding is filed through
    ``coord issue create`` (body assembled via
    :func:`coord.bug_intake.format_bug_report`, with the regression note
    and the mandatory acceptance line folded into Evidence) and then, on
    success, queued through ``coord drive-queue add --machine
    <lane.machine>``.

    ``dry_run=True`` skips BOTH calls entirely and returns a preview —
    this is the sole mechanism backing "a dry run files nothing": there is
    no code path from ``dry_run=True`` to the runner being invoked.
    """
    title = compose_finding_issue_title(finding)
    body = format_bug_report(
        expected=finding.expected,
        actual=finding.actual,
        repro=finding.repro,
        evidence=_evidence_with_acceptance(finding, dedupe),
    )

    if dedupe.verdict is DedupeVerdict.DUPLICATE:
        return FilingResult(
            finding=finding, verdict=dedupe.verdict,
            filed=False, queued=False, issue_number=dedupe.matched_number,
        )

    if dry_run:
        return FilingResult(
            finding=finding, verdict=dedupe.verdict,
            filed=False, queued=False,
            preview_title=title, preview_body=body,
        )

    if lane is None:
        raise RuntimeError(
            f"no lane resolved for finding {finding.title!r} (platform "
            f"{finding.platform!r}) — cannot queue it without a --machine"
        )

    create_out = runner(
        [
            "issue", "create", finding.repo,
            "--title", title,
            "--expected", finding.expected,
            "--actual", finding.actual,
            "--repro", finding.repro,
            "--evidence", _evidence_with_acceptance(finding, dedupe),
        ]
    )
    match = _ISSUE_NUMBER_RE.search(create_out)
    if match is None:
        raise RuntimeError(
            f"coord issue create did not report an issue number: {create_out!r}"
        )
    issue_number = int(match.group(1))

    runner(
        ["drive-queue", "add", finding.repo, str(issue_number), "--machine", lane.machine]
    )

    return FilingResult(
        finding=finding, verdict=dedupe.verdict,
        filed=True, queued=True, issue_number=issue_number,
    )


# ── the round loop ───────────────────────────────────────────────────────


@dataclass
class BugbashConfig:
    repo: str
    lanes: list[BugbashLane]
    reference_backend: str = ""
    max_rounds: int = 5
    #: Cumulative cost a single lane may spend across ALL rounds before
    #: being skipped in subsequent rounds — a hard per-lane cap (#3487).
    cost_cap_per_lane: float = float("inf")
    #: Cumulative cost across every lane, every round, before the whole run
    #: stops — a hard per-run cap (#3487).
    cost_cap_total: float = float("inf")
    dry_run: bool = False
    #: How many of the FIRST rounds require an explicit operator
    #: confirmation (via the *confirm* callback passed to
    #: :func:`run_bugbash`) before filing anything — #3487's "operator
    #: confirmation step before filing on the first rounds." Irrelevant
    #: when ``dry_run=True`` (nothing files regardless).
    confirm_rounds: int = 1


@dataclass
class RoundReport:
    round_num: int
    findings: list[Finding] = field(default_factory=list)
    filings: list[FilingResult] = field(default_factory=list)
    lane_cost: dict[str, float] = field(default_factory=dict)
    skipped_lanes: list[str] = field(default_factory=list)
    #: Findings this round that were NOT duplicates of an already-tracked
    #: open issue — i.e. what actually moved the round-cap/zero-findings
    #: termination decision. Computed from the SAME dedupe verdicts stored
    #: in ``filings``, never re-derived independently (one question, one
    #: answer): a finding declined at the confirm gate still counts here,
    #: since it genuinely was new/regression, just not filed this round.
    new_count: int = 0
    declined: bool = False


@dataclass
class BugbashReport:
    repo: str
    rounds: list[RoundReport] = field(default_factory=list)
    termination_reason: str = ""
    total_cost: float = 0.0

    @property
    def total_filed(self) -> int:
        return sum(1 for r in self.rounds for f in r.filings if f.filed)

    @property
    def would_file(self) -> list[FilingResult]:
        """Every preview-only filing across all rounds — what a
        ``--dry-run`` invocation would have filed, for the "attach to the
        PR" listing (#3487 acceptance)."""
        return [
            f for r in self.rounds for f in r.filings
            if f.preview_title is not None
        ]


def run_bugbash(
    config: BugbashConfig,
    *,
    explorer: Explorer,
    runner: CoordRunner,
    open_issues_fetcher: Callable[[str], list[dict]],
    closed_issues_fetcher: Callable[[str], list[dict]],
    confirm: Callable[[int, list[Finding]], bool] | None = None,
) -> BugbashReport:
    """Run the find -> dedupe -> file -> queue loop for one repo until a
    round yields zero new findings, or a round/cost cap fires (#3487).

    Each round: every lane is explored (unless it has already exceeded
    ``cost_cap_per_lane``, in which case it is skipped and recorded in
    ``skipped_lanes`` — never silently dropped), findings are deduped
    against a FRESH fetch of open/closed issues (so a finding filed earlier
    in the SAME run is already visible and won't be re-filed next round),
    and non-duplicates are filed via :func:`file_finding` — gated by
    *confirm* for the first ``config.confirm_rounds`` rounds when not a dry
    run. Termination is checked AFTER filing, from the round's own observed
    ``new_count``/cost, never inferred from "no exception was raised":

    - ``"zero_findings"`` — this round's non-duplicate finding count is 0.
    - ``"cost_cap"`` — cumulative cost has reached ``cost_cap_total``.
    - ``"round_cap"`` — ``config.max_rounds`` rounds ran without either of
      the above firing.
    """
    lane_cost: dict[str, float] = {lane.platform: 0.0 for lane in config.lanes}
    lanes_by_platform: dict[str, BugbashLane] = {lane.platform: lane for lane in config.lanes}
    total_cost = 0.0
    rounds: list[RoundReport] = []
    reason = "round_cap"

    for round_num in range(1, config.max_rounds + 1):
        report = RoundReport(round_num=round_num)

        for lane in config.lanes:
            if lane_cost[lane.platform] >= config.cost_cap_per_lane:
                report.skipped_lanes.append(lane.platform)
                continue
            outcome = explorer(lane, round_num)
            lane_cost[lane.platform] += outcome.cost
            report.lane_cost[lane.platform] = lane_cost[lane.platform]
            total_cost += outcome.cost
            report.findings.extend(outcome.findings)

        open_issues = open_issues_fetcher(config.repo)
        closed_issues = closed_issues_fetcher(config.repo)

        # Dedupe every finding exactly once (one question, one answer) —
        # everything below (the confirm gate's candidate list, new_count,
        # and the actual filing decision) reads off this SAME verdict per
        # finding rather than re-asking dedupe_finding with a chance to
        # disagree with itself.
        dedupes = [dedupe_finding(f, open_issues, closed_issues) for f in report.findings]

        require_confirm = (not config.dry_run) and round_num <= config.confirm_rounds
        candidates = [
            f for f, d in zip(report.findings, dedupes) if d.verdict != DedupeVerdict.DUPLICATE
        ]
        declined = require_confirm and candidates and not (confirm and confirm(round_num, candidates))
        report.declined = bool(declined)

        for finding, dedupe in zip(report.findings, dedupes):
            lane = lanes_by_platform.get(finding.platform)
            if dedupe.verdict != DedupeVerdict.DUPLICATE:
                report.new_count += 1
            if dedupe.verdict != DedupeVerdict.DUPLICATE and declined:
                # Operator declined this round's filings — record the
                # would-be preview (same shape a dry run produces) without
                # ever invoking the runner.
                title = compose_finding_issue_title(finding)
                body = format_bug_report(
                    expected=finding.expected, actual=finding.actual,
                    repro=finding.repro,
                    evidence=_evidence_with_acceptance(finding, dedupe),
                )
                report.filings.append(
                    FilingResult(
                        finding=finding, verdict=dedupe.verdict,
                        filed=False, queued=False,
                        preview_title=title, preview_body=body,
                    )
                )
                continue
            # An unresolvable lane (finding.platform not in this run's
            # config.lanes) is only a problem when it would actually be
            # queued — file_finding raises in that case, never silently
            # drops the machine target (#2096: a gate must be able to fail).
            result = file_finding(finding, dedupe, lane, runner, dry_run=config.dry_run)
            report.filings.append(result)

        rounds.append(report)

        if report.new_count == 0:
            reason = "zero_findings"
            break
        if total_cost >= config.cost_cap_total:
            reason = "cost_cap"
            break
    else:
        reason = "round_cap"

    return BugbashReport(
        repo=config.repo, rounds=rounds, termination_reason=reason, total_cost=total_cost,
    )
