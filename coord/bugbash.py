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

**#3510: an unavailable lane is not a bug finding.** When a win-native/
mac-native/gtk-native lane's own driver reports a locked or absent GUI
session (or no usable display), an explorer that sets
:attr:`ExploreOutcome.unavailable` causes :func:`run_bugbash` to skip that
lane for the round (:attr:`RoundReport.unavailable_lanes`) rather than
running the exploration checklist against it or filing any finding from it
— a locked desktop is an environment condition for the operator to fix,
not evidence of an app bug. This engine-level behavior is unit-tested
against a fake explorer in ``tests/test_bugbash.py``; the production
explorer (:func:`coord.commands.bugbash._dispatch_and_await_lane`) does
not itself set ``unavailable`` yet — see its own module docstring's
"KNOWN GAP" note — so a live ``coord bugbash`` run does not currently
benefit from this skip until that wiring lands.
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
    #: #3517: ``True`` when the lane worker's JSON entry for this finding was
    #: missing one or more required fields and a value below had to be
    #: defaulted/derived rather than actually reported. A protocol slip
    #: (the worker naming a field differently, or skipping it) must NEVER
    #: silently collapse a real finding into "no finding" — see
    #: :data:`_REQUIRED_FINDING_FIELDS` and :func:`_finding_from_entry`. Kept
    #: ``False`` for a well-formed entry.
    incomplete: bool = False
    #: Which required field names (see :data:`_REQUIRED_FINDING_FIELDS`) were
    #: missing/blank in the raw JSON entry this finding was built from —
    #: empty whenever :attr:`incomplete` is ``False``.
    missing_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class FindingsParseResult:
    """What :func:`parse_findings_block` extracted from one lane worker's
    final message (#3517).

    ``findings`` is never silently emptied by a per-entry defect — a JSON
    entry missing a required field is still turned into a :class:`Finding`
    (see :attr:`Finding.incomplete`). ``protocol_error`` is the DISTINCT
    failure mode this type exists to make unmistakable: a non-empty string
    means the lane's report itself could not be trusted at all — the
    ```` ```bugbash-findings ```` fence held invalid JSON or something other
    than a list, or there was no fence AND no explicit statement that the
    round found nothing. ``findings`` is always ``()`` when
    ``protocol_error`` is set. Callers (:func:`coord.bugbash.run_bugbash` via
    :data:`ExploreOutcome.protocol_error`) must never read an empty
    ``findings`` tuple alone as "zero findings" without also checking this
    field — that conflation is exactly bug #3517.
    """

    findings: tuple[Finding, ...] = ()
    protocol_error: str = ""


#: The findings-entry fields a lane worker's briefing (see
#: :func:`build_exploration_briefing`) asks for by name. ``evidence`` is
#: handled specially by :func:`_finding_from_entry` (derived from ``captures``
#: / ``actual`` rather than merely defaulted to a placeholder) since it is
#: the one field #3517's real-world miss actually dropped.
_REQUIRED_FINDING_FIELDS: tuple[str, ...] = ("title", "expected", "actual", "repro", "evidence")

#: Placeholder text used for a missing field with no better derivation —
#: visibly a placeholder (never mistaken for a real report) rather than an
#: empty string, which would render as a blank issue-body section.
_MISSING_FIELD_PLACEHOLDER = "(not reported by lane worker)"

#: Phrases that count as an explicit "this round found nothing" statement
#: when a lane worker's final message carries no ```` ```bugbash-findings ````
#: fence at all (#3517). Deliberately narrow and literal (no fuzzy/NLP
#: matching) — a missing fence defaults to a PROTOCOL ERROR, never silently
#: to a clean pass, so the bar for accepting "no fence" as clean is an
#: unambiguous statement, not a guess.
_CLEAN_PASS_PHRASES: tuple[str, ...] = (
    "no findings", "zero findings", "0 findings", "found nothing",
    "nothing to report", "no bugs found", "no issues found",
    "found no issues", "found no bugs", "clean round", "clean pass",
)


def _is_explicit_clean_statement(text: str) -> bool:
    """``True`` when *text* contains one of :data:`_CLEAN_PASS_PHRASES`,
    case-insensitively — the ONLY thing that lets a fence-less message in
    :func:`parse_findings_block` read as a genuine clean pass rather than a
    protocol error (#3517)."""
    lowered = text.lower()
    return any(phrase in lowered for phrase in _CLEAN_PASS_PHRASES)


def _finding_from_entry(entry: dict, *, platform: str, repo: str) -> Finding:
    """Turn one raw JSON object from a ```` ```bugbash-findings ```` block
    into a :class:`Finding` — NEVER dropping it for a missing required field
    (#3517). Each missing/blank field in :data:`_REQUIRED_FINDING_FIELDS` is
    replaced with the best available value and recorded in
    :attr:`Finding.missing_fields`:

    - ``evidence`` is derived from ``captures`` (what the worker DID attach)
      when present, else from ``actual`` (the behaviour it already
      described), else a visible placeholder — this is exactly the field the
      live #3517 finding omitted, so it gets the most effort to recover.
    - every other field falls back to :data:`_MISSING_FIELD_PLACEHOLDER`,
      which is distinguishable from a real report at a glance.
    """
    missing: list[str] = []

    def _field(key: str) -> str:
        value = str(entry.get(key, "")).strip()
        if not value:
            missing.append(key)
            return _MISSING_FIELD_PLACEHOLDER
        return value

    title = _field("title")
    expected = _field("expected")
    actual_raw = str(entry.get("actual", "")).strip()
    actual = actual_raw or _MISSING_FIELD_PLACEHOLDER
    if not actual_raw:
        missing.append("actual")
    repro = _field("repro")

    captures_raw = entry.get("captures") or []
    captures = tuple(str(c) for c in captures_raw) if isinstance(captures_raw, list) else ()

    evidence = str(entry.get("evidence", "")).strip()
    if not evidence:
        missing.append("evidence")
        if captures:
            evidence = "Derived from captures (evidence field was missing): " + ", ".join(captures)
        elif actual_raw:
            evidence = "Derived from `actual` (evidence field was missing): " + actual_raw
        else:
            evidence = _MISSING_FIELD_PLACEHOLDER

    return Finding(
        title=title,
        platform=platform,
        repo=repo,
        suspected_repo=str(entry.get("suspected_repo", repo)).strip() or repo,
        expected=expected,
        actual=actual,
        repro=repro,
        evidence=evidence,
        captures=captures,
        incomplete=bool(missing),
        missing_fields=tuple(missing),
    )


def parse_findings_block(text: str, *, platform: str, repo: str) -> FindingsParseResult:
    """Extract the ```` ```bugbash-findings ```` fenced JSON block from a lane
    worker's final message and turn it into :class:`FindingsParseResult`
    (#3517).

    Three distinct outcomes, none of which collapse into each other:

    - **No fence, and an explicit clean statement** (one of
      :data:`_CLEAN_PASS_PHRASES`) — a genuine clean round: returns
      ``FindingsParseResult()`` (empty findings, no protocol error).
    - **No fence, and no explicit clean statement** — the worker's protocol
      slip, not a clean pass: returns a non-empty ``protocol_error``. A
      lane worker that simply forgot to fence its findings must never read
      identically to one that affirmatively found nothing.
    - **A fence present, but its contents fail to parse as a JSON list**
      (invalid JSON, or valid JSON that isn't a list) — also a
      ``protocol_error``, never silently ``[]`` (that was bug #3517: a
      malformed block and a clean pass rendered identically).

    Otherwise, every object in the parsed list becomes a :class:`Finding`
    via :func:`_finding_from_entry` — missing required fields are defaulted/
    derived and flagged :attr:`Finding.incomplete`, never dropped. An item in
    the list that isn't a JSON object at all is skipped (the fence itself
    still parsed as a valid JSON list, so this is not elevated to a protocol
    error).
    """
    pattern = re.compile(
        rf"```{re.escape(FINDINGS_FENCE)}\s*\n(.*?)```", re.DOTALL,
    )
    match = pattern.search(text)
    if match is None:
        if _is_explicit_clean_statement(text):
            return FindingsParseResult()
        return FindingsParseResult(
            protocol_error=(
                f"no ```{FINDINGS_FENCE}``` fence found in the lane worker's "
                "final message, and it did not explicitly state a clean "
                "(zero-findings) pass"
            ),
        )
    try:
        raw = json.loads(match.group(1))
    except (ValueError, TypeError) as e:
        return FindingsParseResult(
            protocol_error=f"```{FINDINGS_FENCE}``` fence did not contain valid JSON: {e}",
        )
    if not isinstance(raw, list):
        return FindingsParseResult(
            protocol_error=(
                f"```{FINDINGS_FENCE}``` fence must contain a JSON list, "
                f"got {type(raw).__name__}"
            ),
        )

    findings = tuple(
        _finding_from_entry(entry, platform=platform, repo=repo)
        for entry in raw
        if isinstance(entry, dict)
    )
    return FindingsParseResult(findings=findings)


#: #3566 ask #4/#5: the briefing's hard-rule reporting contract for a lane
#: worker that hits a missing permission (Accessibility/Screen Recording
#: trust, a locked/absent GUI session, ...) instead of improvising a
#: workaround. Mirrors :data:`FINDINGS_FENCE`'s "one constant, used by both
#: the briefing and the parser" discipline so the two can never drift apart.
UNAVAILABLE_FENCE = "bugbash-unavailable"

_UNAVAILABLE_FENCE_RE = re.compile(
    rf"```{re.escape(UNAVAILABLE_FENCE)}\s*\n(.*?)```", re.DOTALL,
)

#: Fallback signatures (#3566 ask #5, "a driver session/permission
#: failure") — a worker that forgot to fence its unavailable report, or
#: whose own driver call surfaced the condition directly in a tool result,
#: still has one of these strings somewhere in its raw transcript. Matched
#: against the FULL log text (not just the final assistant message), so a
#: worker that reported the condition mid-session and then crashed is still
#: caught. Kept narrow and literal — these are the exact strings
#: `coord.mac_native_driver`/`coord.win_native_driver`/
#: `coord.gtk_native_driver`'s own session/trust probes emit, never a vague
#: substring that could false-positive on an unrelated mention of
#: "unavailable" in a finding's prose.
_UNAVAILABLE_SIGNATURES: tuple[str, ...] = (
    '"status": "unavailable"',
    '"status":"unavailable"',
    "no unlocked GUI session is available",
    "the screen is locked",
    "is not on the console",
    "AXIsProcessTrusted() is False",
    "AXIsProcessTrusted() returned False",
    "CGPreflightScreenCaptureAccess",
)


def parse_unavailable_report(text: str) -> str:
    """The lane-unavailable reason from *text* (a lane worker's full
    transcript), or ``""`` if none is present (#3566).

    First checks for the authoritative fenced
    ```` ```bugbash-unavailable ```` block the briefing instructs a worker
    to write when it hits a missing permission or absent session rather
    than improvising a workaround (ask #4's hard rule). Falls back to
    :data:`_UNAVAILABLE_SIGNATURES` — a driver-level session/permission
    failure surfacing directly in a tool result even without the worker's
    own cooperation.

    Never raises. The caller
    (:func:`coord.commands.bugbash._dispatch_and_await_lane`) treats a
    non-empty return as :attr:`ExploreOutcome.unavailable`, never
    ``ok=False``/a protocol error — #2096's "one question, one answer":
    this is the ONE place that question is answered.
    """
    match = _UNAVAILABLE_FENCE_RE.search(text)
    if match:
        reason = match.group(1).strip()
        if reason:
            return reason
    for signature in _UNAVAILABLE_SIGNATURES:
        if signature in text:
            return f"driver/session signal found in transcript: {signature!r}"
    return ""


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
        # #3546: score against the TAG-STRIPPED title, not the raw one — the
        # platform gate above already accounts for the ``[bugbash:<platform>]``
        # prefix, so scoring the untouched title made its own extra tokens
        # ("bugbash", the platform name) dilute the Jaccard score against a
        # short real-world title. "Caption buttons unclickable" (3 words) vs.
        # "[bugbash:win-native] Caption buttons unclickable" scored only 0.5
        # — BELOW :data:`DEFAULT_DEDUPE_THRESHOLD` — purely because of the
        # tag's own 3 extra words, which is exactly the "titles differing
        # enough to defeat matching" root cause behind vimcode#1656/#1660
        # being filed as two separate issues for one bug.
        score = _title_similarity(finding.title, _strip_platform_tag(title))
        if score >= threshold and (best is None or score > best[1]):
            best = (issue, score)
    return best


def _platform_from_title(title: str) -> str | None:
    m = re.match(r"^\[bugbash:([^\]]+)\]", title)
    return m.group(1) if m else None


def _strip_platform_tag(title: str) -> str:
    """Remove a leading ``[bugbash:<platform>]`` tag (if present) before
    scoring title similarity — see :func:`_best_match`'s comment for why
    leaving it in place made the score sensitive to the platform name
    itself rather than just the bug description."""
    return re.sub(r"^\[bugbash:[^\]]+\]\s*", "", title)


def _dedupe_round_findings(
    findings: Sequence[Finding],
    open_issues: list[dict],
    closed_issues: list[dict],
    *,
    threshold: float = DEFAULT_DEDUPE_THRESHOLD,
) -> list[DedupeResult]:
    """Dedupe every finding from ONE round, in order, against *open_issues*/
    *closed_issues* — AND against every finding already decided NEW or
    REGRESSION earlier in this SAME call (#3546).

    The bugbash run that filed vimcode#1656/#1660/#1669/#1675 as four
    separate issues for one bug did so partly because two lanes reporting
    the identical symptom in the SAME round were each deduped only against
    issues that existed BEFORE the round started — neither lane's finding
    could see the other's. This function fixes that: a finding is matched
    not just against *open_issues* but against a running list seeded with
    every PRIOR finding in *findings* that this same walk already decided
    was new/a regression, so the second lane's identical report comes back
    :attr:`DedupeVerdict.DUPLICATE` instead of also being judged new.

    A within-round match's :attr:`DedupeResult.matched_number` is ``None``
    here — the sibling finding it matches has not actually been filed
    through ``coord issue create`` yet at dedupe time, so there is no real
    issue number to report. :func:`run_bugbash`'s filing loop resolves this
    to the sibling's real number once that sibling is actually filed,
    rather than leaving every within-round duplicate permanently numberless.
    """
    results: list[DedupeResult] = []
    running_open = list(open_issues)
    for finding in findings:
        result = dedupe_finding(finding, running_open, closed_issues, threshold=threshold)
        results.append(result)
        if result.verdict is not DedupeVerdict.DUPLICATE:
            running_open.append(
                {"number": None, "title": compose_finding_issue_title(finding)}
            )
    return results


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
        "HARD RULE — drive the app ONLY through this lane's own driver "
        "(coord.mac_native_driver / coord.win_native_driver / "
        "coord.gtk_native_driver, whichever this lane is). Never use "
        "osascript, System Events, a Terminal/iTerm `do script`, a "
        "home-made input-injection helper, or System Settings. If a "
        "required permission (Accessibility, Screen Recording, a locked/"
        "absent GUI session, ...) is missing, STOP IMMEDIATELY and report "
        "the lane unavailable (see below) — do NOT improvise a workaround, "
        "and do NOT send any key or click to recover (#3566).",
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
        "If the HARD RULE above fires (a required permission/session is "
        "missing), skip the findings fence entirely and end your final "
        "message with a fenced "
        f"```{UNAVAILABLE_FENCE}``` block containing one line: the reason "
        "the lane is unavailable.",
        "",
        "Otherwise, when done, end your final message with a fenced "
        f"```{FINDINGS_FENCE}``` block containing a JSON array of finding "
        "objects, each with: title, expected, actual, repro, evidence, "
        "suspected_repo (the repo the FIX belongs in — the app, its UI "
        "framework, e.g. quadraui, OR the coordinator tooling itself, e.g. "
        "claude-coordinator, if the bug is actually in this bugbash driver "
        "or its WSL/native bridge rather than in the app under test), "
        "captures (list of "
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
    agnostic, it just accumulates and compares).

    ``ok`` is the explicit, machine-checkable signal for "this outcome is a
    verified observation of the lane," separate from ``notes`` (#2096: an
    unconfirmed success is a defect). A production explorer sets
    ``ok=False`` on every path that did NOT actually observe the lane run to
    completion — dispatch failed, the poll never reached a terminal state,
    the assignment vanished, it finished with a non-zero exit code, or its
    log couldn't be fetched — with ``notes`` explaining why. It defaults to
    ``True`` because a fake explorer in a test, and a real explorer that
    genuinely completed, both hand back a trustworthy ``findings`` tuple
    without having to opt in. ``findings=(), ok=False`` and ``findings=(),
    ok=True`` are NOT the same thing: the first means "we don't know if
    there were findings," the second means "we looked, and there weren't
    any" — :func:`run_bugbash` keeps them distinguishable in
    :attr:`RoundReport.lane_failures` and its termination reason, rather
    than collapsing both into "zero findings".

    ``unavailable`` is a THIRD, separate outcome (#3510): the lane's own
    native driver observed a locked/absent GUI session (win-native/
    mac-native) or no usable display (gtk-native) and ran no exploration at
    all — a host/environment condition, never a bug finding and never a
    dispatch/poll failure. An explorer that sets ``unavailable=True`` (as
    the fake explorers in ``tests/test_bugbash.py`` do, modeling a
    dispatched worker whose Tier-2 lane result came back
    ``status="unavailable"`` — see ``coord.win_native_driver``'s/
    ``coord.mac_native_driver``'s/``coord.gtk_native_driver``'s own
    precheck) causes :func:`run_bugbash` to skip that lane for the round —
    recording it in :attr:`RoundReport.unavailable_lanes` instead of
    :attr:`RoundReport.lane_failures` — and never file findings from it
    this round, even defensively, regardless of what ``findings`` carries.
    The production explorer,
    :func:`coord.commands.bugbash._dispatch_and_await_lane`, sets this flag
    too (#3566): it detects a worker's own "lane unavailable" report (or a
    driver session/permission failure surfacing directly in the transcript)
    via :func:`parse_unavailable_report`.

    ``protocol_error`` is a FOURTH, separate outcome (#3517): the lane
    worker DID run to completion (``ok=True``, unlike a dispatch/poll/log
    failure) and its own driver observed no locked/absent session (unlike
    ``unavailable``) — but its final message, run through
    :func:`parse_findings_block`, came back as a :class:`FindingsParseResult`
    with a non-empty ``protocol_error``: no parseable
    ```` ```bugbash-findings ```` block, and
    no explicit statement that the round found nothing. This must never be
    mistaken for ``findings=(), ok=True`` ("ran the checklist, found
    nothing") — that conflation is exactly how bug #3517's real finding (a
    valid block, but one entry missing ``evidence``) got dropped to "zero
    findings" and let a real bug pass the #3488 release gate.
    :func:`run_bugbash` records a non-empty ``protocol_error`` in
    :attr:`RoundReport.protocol_error_lanes` and that round can never
    terminate ``"zero_findings"`` while any lane set it (see
    :attr:`BugbashReport.any_protocol_errors`)."""

    findings: tuple[Finding, ...] = ()
    cost: float = 0.0
    notes: str = ""
    ok: bool = True
    unavailable: bool = False
    protocol_error: str = ""


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
    if finding.incomplete:
        # #3517: a finding filed from a lane report missing one or more
        # required fields must say so on the issue itself, not just in the
        # CLI's round output — a human triaging it needs to know some of
        # what's below was defaulted/derived, not actually reported.
        parts.append(
            "INCOMPLETE REPORT: the lane worker's findings entry was missing "
            f"required field(s): {', '.join(finding.missing_fields)}. Values "
            "above were defaulted/derived rather than reported."
        )
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


def finding_target_repo(finding: Finding) -> str:
    """Which repo a finding's issue should be filed into (#3546 requirement
    2): :attr:`Finding.suspected_repo` when the worker reported one, else
    *finding.repo* (the app the lane actually ran against) as the fallback.

    Three coord/win-native-driver/WSL-bridge bugs from the first real
    vimcode bugbash run were filed (and queued) in vimcode anyway, where no
    worker could ever fix them, because filing unconditionally targeted
    *finding.repo*. ``suspected_repo`` already carries the worker's best
    guess at the actually-at-fault repo (the app itself, its UI framework,
    or coord's own driver/bridge — see :func:`build_exploration_briefing`)
    and defaults to *repo* in :func:`_finding_from_entry` when a lane
    worker doesn't name one explicitly, so this is a one-line fallback, not
    a guess of its own.
    """
    return finding.suspected_repo or finding.repo


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

    Both calls target :func:`finding_target_repo` (#3546) — NOT always
    *finding.repo* — so a finding whose ``suspected_repo`` names coord's
    own driver/bridge or the app's UI framework lands where a worker can
    actually fix it, rather than in the app repo where nobody can.

    ``dry_run=True`` skips BOTH calls entirely and returns a preview —
    this is the sole mechanism backing "a dry run files nothing": there is
    no code path from ``dry_run=True`` to the runner being invoked.
    """
    if dedupe.verdict is DedupeVerdict.DUPLICATE:
        # Nothing to preview or file — the title/body below are discarded
        # on this path, so don't bother building them.
        return FilingResult(
            finding=finding, verdict=dedupe.verdict,
            filed=False, queued=False, issue_number=dedupe.matched_number,
        )

    target_repo = finding_target_repo(finding)
    title = compose_finding_issue_title(finding)
    body = format_bug_report(
        expected=finding.expected,
        actual=finding.actual,
        repro=finding.repro,
        evidence=_evidence_with_acceptance(finding, dedupe),
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
            "issue", "create", target_repo,
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
        ["drive-queue", "add", target_repo, str(issue_number), "--machine", lane.machine]
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
    #: ``{platform: reason}`` for every entry in :attr:`skipped_lanes`
    #: (#3546) — a skip must be as explainable as a failure or an
    #: unavailability, never a bare platform name with no reason attached.
    #: Currently populated with the per-lane cost-cap detail (the only
    #: engine-level skip reason today); kept as its own dict (rather than
    #: folded into ``skipped_lanes`` itself) so ``skipped_lanes``'s existing
    #: ``list[str]`` shape — already read by callers — never has to change.
    skip_reasons: dict[str, str] = field(default_factory=dict)
    #: Findings this round that were NOT duplicates of an already-tracked
    #: open issue — i.e. what actually moved the round-cap/zero-findings
    #: termination decision. Computed from the SAME dedupe verdicts stored
    #: in ``filings``, never re-derived independently (one question, one
    #: answer): a finding declined at the confirm gate still counts here,
    #: since it genuinely was new/regression, just not filed this round.
    new_count: int = 0
    declined: bool = False
    #: ``{platform: notes}`` for every lane explored this round whose
    #: :attr:`ExploreOutcome.ok` was ``False`` — a dispatch failure, a poll
    #: that never reached a terminal state, a vanished assignment, a
    #: non-zero exit code, or a log-fetch failure (#2096: an unverified
    #: round must never render identically to a clean one). Never populated
    #: from a *skipped* lane (:attr:`skipped_lanes` already covers those —
    #: a lane skipped for having blown its per-lane cost cap was never
    #: asked a question this round, so it can't have failed to answer one).
    lane_failures: dict[str, str] = field(default_factory=dict)
    #: ``{platform: notes}`` for every lane explored this round whose
    #: :attr:`ExploreOutcome.unavailable` was ``True`` (#3510) — a locked or
    #: absent GUI session (win-native/mac-native) or no usable display
    #: (gtk-native). Distinct from :attr:`lane_failures`: this is a verified
    #: observation (the native driver's own session precheck ran and
    #: reported it), not a dispatch/poll/log failure — but it is ALSO not a
    #: verified "ran the checklist and found nothing" either, so it is
    #: tracked separately rather than folded into either bucket. No
    #: findings are ever filed from a lane recorded here this round.
    unavailable_lanes: dict[str, str] = field(default_factory=dict)
    #: ``{platform: detail}`` for every lane explored this round whose
    #: :attr:`ExploreOutcome.protocol_error` was non-empty (#3517) — the lane
    #: worker completed (``ok=True``), its driver reported no locked/absent
    #: session, but its final message could not be trusted as a findings
    #: report at all: no parseable ```` ```bugbash-findings ```` block, and no
    #: explicit "found nothing" statement either. Distinct from BOTH
    #: ``lane_failures`` (we never even got an answer) and
    #: ``unavailable_lanes`` (the driver's own precheck blocked the run) —
    #: here the worker ran and answered, but the answer violates the
    #: reporting contract, so it must never be read as "zero findings
    #: observed" (that silent collapse is exactly bug #3517).
    protocol_error_lanes: dict[str, str] = field(default_factory=dict)

    @property
    def explored_lanes(self) -> set[str]:
        """Platforms actually explored this round (i.e. not skipped) —
        every explored lane adds an entry to ``lane_cost`` even when its
        outcome cost ``0.0``, so this is exactly ``lane_cost``'s key set."""
        return set(self.lane_cost)

    @property
    def all_explored_lanes_failed(self) -> bool:
        """``True`` when every lane explored this round (there must be at
        least one) came back ``ok=False`` — the "the whole fleet is down,
        not a clean pass" signal (#2096). A round with nothing explored at
        all (e.g. every lane skipped on its cost cap) is NOT reported as
        "all failed" — there is nothing to distrust, just nothing that
        ran. Does NOT count an ``unavailable`` lane as failed — see
        :attr:`all_explored_lanes_unavailable_or_failed` for the combined
        check."""
        explored = self.explored_lanes
        return bool(explored) and explored <= set(self.lane_failures)

    @property
    def all_explored_lanes_unavailable_or_failed(self) -> bool:
        """``True`` when every lane explored this round either failed to
        dispatch/poll/log OR reported its GUI session/display unavailable
        (#3510) — i.e. NONE of them actually ran the exploration checklist.
        Used by :func:`run_bugbash` to pick the ``"lanes_unavailable"``
        termination reason over a false ``"zero_findings"`` clean-pass read
        when every lane this round was simply locked/absent rather than
        genuinely explored (#2096: zero findings is only a clean pass when
        something was actually observed to run)."""
        explored = self.explored_lanes
        return bool(explored) and explored <= (
            set(self.lane_failures) | set(self.unavailable_lanes)
        )

    @property
    def all_lanes_skipped(self) -> bool:
        """``True`` when at least one lane was skipped this round (its
        per-lane cost cap was already blown) AND NOT A SINGLE lane was
        actually explored (#3546) — i.e. the round asked nothing, got no
        answer from anyone, yet :attr:`new_count` still reads ``0`` the same
        way a genuine clean pass does.

        The first real vimcode bugbash run's round 4 skipped every
        configured lane this way and the CLI still printed
        ``terminated='zero_findings'`` — a release gate reading that output
        would conclude "last bugbash clean" about a round that tested
        nothing at all. Distinct from :attr:`all_explored_lanes_failed` /
        :attr:`all_explored_lanes_unavailable_or_failed`, both of which
        require ``explored_lanes`` to be non-empty (a lane that was asked
        and answered badly) — this property is specifically the "nobody was
        even asked" case."""
        return bool(self.skipped_lanes) and not self.explored_lanes


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

    @property
    def any_lane_failures(self) -> bool:
        """``True`` if ANY round recorded a lane failure — surfaced
        regardless of whether it happened to be the terminating round, so
        an operator glancing at a "round_cap"/"cost_cap" run still sees that
        part of what it observed along the way was unverified (#2096)."""
        return any(r.lane_failures for r in self.rounds)

    @property
    def any_lane_unavailable(self) -> bool:
        """``True`` if ANY round recorded a lane as unavailable (#3510) —
        surfaced regardless of whether it happened to be the terminating
        round, so an operator glancing at a run that otherwise filed/found
        plenty still sees that one platform was locked/absent the whole
        time and needs attention, not a bug report."""
        return any(r.unavailable_lanes for r in self.rounds)

    @property
    def any_protocol_errors(self) -> bool:
        """``True`` if ANY round recorded a lane protocol error (#3517) —
        surfaced regardless of whether it happened to be the terminating
        round, so an operator glancing at an otherwise-clean run still sees
        that one lane's report could not be trusted and needs a human to
        look at its transcript, not a silent "zero findings" credit."""
        return any(r.protocol_error_lanes for r in self.rounds)


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
    ``skipped_lanes``/``skip_reasons`` — never silently dropped), findings
    are deduped via :func:`_dedupe_round_findings` against a FRESH fetch of
    open/closed issues MERGED with every issue THIS RUN has already filed
    (#3546: a finding filed earlier in the same run — this round or an
    earlier one — must be recognised by its real issue number, not just by
    whatever a fresh ``gh issue list`` happens to already reflect, and two
    lanes reporting the same bug in the SAME round must collapse to one
    filing too), and non-duplicates are filed via :func:`file_finding` —
    gated by *confirm* for the first ``config.confirm_rounds`` rounds when
    not a dry run. Termination is checked AFTER filing, from the round's own
    observed ``new_count``/cost, never inferred from "no exception was
    raised":

    - ``"zero_findings"`` — this round's non-duplicate finding count is 0,
      AND at least one lane was actually explored this round (not every
      lane was skipped on its cost cap — ``RoundReport.all_lanes_skipped``
      is ``False``), AND that explored lane actually completed its
      checklist (neither ``RoundReport.all_explored_lanes_failed`` nor
      ``RoundReport.all_explored_lanes_unavailable_or_failed`` is ``True``),
      AND no lane reported a protocol error (``RoundReport
      .protocol_error_lanes`` is empty) — a genuine observed clean pass.
    - ``"lane_failure"`` — this round's non-duplicate finding count is ALSO
      0, but every lane explored this round came back ``ok=False``
      (dispatch/poll/log failure) — #2096: a fleet-wide dispatch outage
      must never be reported identically to a clean bugbash pass. Check
      ``BugbashReport.rounds[-1].lane_failures`` for what actually broke.
    - ``"protocol_error"`` — this round's non-duplicate finding count is ALSO
      0, not every lane failed to dispatch/poll/log, but at least one lane
      that DID complete reported a :attr:`ExploreOutcome.protocol_error`
      (#3517: a malformed or missing ```` ```bugbash-findings ```` block, or a
      fence-less message with no explicit "found nothing" statement). This
      takes priority over ``"lanes_unavailable"`` below — a lane that
      answered badly is a stronger "do not trust this round" signal than one
      that was simply locked out. Check ``BugbashReport.rounds[-1]
      .protocol_error_lanes`` for which lane and why.
    - ``"lanes_unavailable"`` — this round's non-duplicate finding count is
      ALSO 0, no lane came back a dispatch/poll/log failure or a protocol
      error, and either (a) every lane explored this round reported its GUI
      session/display unavailable (#3510: locked or absent, e.g. a locked
      dell64), or (b) EVERY configured lane was skipped on its cost cap and
      none was explored at all this round (#3546: a round that asked
      nothing must never read as a clean pass either) — a different reason
      from ``"lane_failure"`` (the explorer DID run and DID get a verified
      answer, or nothing was even asked), and still not a clean pass either,
      since nothing was actually exercised against the app. Check
      ``BugbashReport.rounds[-1].unavailable_lanes``/``skip_reasons`` for
      which host needs unlocking or budget needs raising.
    - ``"cost_cap"`` — cumulative cost has reached ``cost_cap_total``.
    - ``"round_cap"`` — ``config.max_rounds`` rounds ran without either of
      the above firing.
    """
    lane_cost: dict[str, float] = {lane.platform: 0.0 for lane in config.lanes}
    lanes_by_platform: dict[str, BugbashLane] = {lane.platform: lane for lane in config.lanes}
    total_cost = 0.0
    rounds: list[RoundReport] = []
    reason = "round_cap"
    # #3546: every issue THIS RUN has actually filed into config.repo,
    # across every round so far — consulted alongside each round's FRESH
    # open-issues fetch so a finding matching an issue this run itself
    # already created reads as a duplicate by NUMBER, never depending on a
    # `gh issue list` snapshot having caught up with this process's own
    # recent write. `run_filed_by_title` is the same data keyed for O(1)
    # resolution when a within-round duplicate (see
    # `_dedupe_round_findings`) needs its placeholder `matched_number=None`
    # upgraded to the sibling's real number once that sibling is filed.
    run_filed_issues: list[dict] = []
    run_filed_by_title: dict[str, int] = {}

    for round_num in range(1, config.max_rounds + 1):
        report = RoundReport(round_num=round_num)

        for lane in config.lanes:
            if lane_cost[lane.platform] >= config.cost_cap_per_lane:
                report.skipped_lanes.append(lane.platform)
                report.skip_reasons[lane.platform] = (
                    f"cumulative cost {lane_cost[lane.platform]:.2f} already "
                    f">= per-lane cap {config.cost_cap_per_lane:.2f}"
                )
                continue
            outcome = explorer(lane, round_num)
            lane_cost[lane.platform] += outcome.cost
            report.lane_cost[lane.platform] = lane_cost[lane.platform]
            total_cost += outcome.cost
            if outcome.unavailable:
                # #3510: a locked/absent GUI session (or missing display)
                # is a host condition, not a finding — recorded separately
                # from both a clean pass and a dispatch/poll failure, and
                # NEVER contributes findings this round, even defensively
                # if the explorer happened to also hand some back.
                report.unavailable_lanes[lane.platform] = (
                    outcome.notes or "lane unavailable — no usable GUI session/display"
                )
                continue
            report.findings.extend(outcome.findings)
            if not outcome.ok:
                # #2096: this lane's "findings" (almost certainly empty) are
                # NOT a verified observation — record why, so a round whose
                # every lane failed this way can never render identically to
                # a round that actually looked and found nothing.
                report.lane_failures[lane.platform] = outcome.notes or "explorer reported failure"
            elif outcome.protocol_error:
                # #3517: the lane DID complete, but its final message could
                # not be trusted as a findings report at all — a malformed/
                # missing block, never silently read as "zero findings
                # observed" (that conflation is exactly what let a real
                # finding disappear and pass the #3488 release gate).
                report.protocol_error_lanes[lane.platform] = outcome.protocol_error

        # #3546: merge the fresh fetch with every issue THIS RUN has already
        # filed — a finding matching one of this run's own earlier filings
        # must be recognised by number even if the fresh fetch hasn't (yet)
        # caught up with this process's own recent write.
        open_issues = list(open_issues_fetcher(config.repo)) + run_filed_issues
        closed_issues = closed_issues_fetcher(config.repo)

        # Dedupe every finding exactly once (one question, one answer) —
        # everything below (the confirm gate's candidate list, new_count,
        # and the actual filing decision) reads off this SAME verdict per
        # finding rather than re-asking dedupe with a chance to disagree
        # with itself. `_dedupe_round_findings` also catches two lanes
        # reporting the same bug in THIS round against each other, not just
        # against issues that existed before the round started.
        dedupes = _dedupe_round_findings(report.findings, open_issues, closed_issues)

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
            if dedupe.verdict is DedupeVerdict.DUPLICATE and dedupe.matched_number is None:
                # #3546: a within-round duplicate from `_dedupe_round_findings`
                # — its sibling finding may have already been filed earlier
                # in THIS loop (in which case its real issue number is now in
                # `run_filed_by_title`). Resolve it so the report/CLI shows
                # the real number instead of a permanent "duplicate of
                # #None".
                resolved = run_filed_by_title.get(dedupe.matched_title or "")
                if resolved is not None:
                    dedupe = DedupeResult(
                        verdict=dedupe.verdict, matched_number=resolved,
                        matched_title=dedupe.matched_title, score=dedupe.score,
                    )
            # An unresolvable lane (finding.platform not in this run's
            # config.lanes) is only a problem when it would actually be
            # queued — file_finding raises in that case, never silently
            # drops the machine target (#2096: a gate must be able to fail).
            result = file_finding(finding, dedupe, lane, runner, dry_run=config.dry_run)
            report.filings.append(result)
            if result.filed and result.issue_number is not None:
                # Only track filings that landed in config.repo's own
                # namespace — a finding routed elsewhere via
                # `finding_target_repo` (#3546 requirement 2) dedupes
                # against THAT repo's issues, not this one's.
                if finding_target_repo(finding) == config.repo:
                    filed_title = compose_finding_issue_title(finding)
                    run_filed_issues.append({"number": result.issue_number, "title": filed_title})
                    run_filed_by_title[filed_title] = result.issue_number

        rounds.append(report)

        if report.new_count == 0:
            # #2096/#3517/#3546: "zero findings" is only a genuine clean-pass
            # verdict when at least one lane was actually verified to have
            # RUN THE CHECKLIST this round AND every lane that did complete
            # produced a trustworthy report. A round where every explored
            # lane failed to dispatch/poll/fetch its log gets "lane_failure";
            # a round where some lane completed but its report couldn't be
            # trusted at all (a malformed/missing findings block — #3517)
            # gets "protocol_error" instead, ahead of "lanes_unavailable"
            # below since a bad answer is a stronger "don't trust this round"
            # signal than a lane simply being locked out; a round where every
            # explored lane instead reported its session/display unavailable
            # (#3510 — locked or absent, never a dispatch failure), OR every
            # configured lane was skipped on its cost cap with NONE explored
            # at all (#3546), gets the same "lanes_unavailable" reason —
            # neither is a verified clean pass.
            if report.all_explored_lanes_failed:
                reason = "lane_failure"
            elif report.protocol_error_lanes:
                reason = "protocol_error"
            elif report.all_explored_lanes_unavailable_or_failed or report.all_lanes_skipped:
                reason = "lanes_unavailable"
            else:
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
