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

**#3580: a repo-supplied behaviour catalogue replaces the hardcoded
checklist when one exists.** :data:`EXPLORATION_CHECKLIST` is a cross-
backend differential tester with no product knowledge — it can never find
a bug every backend shares, and it can't judge editing behaviour at all.
When the app repo has a ``tests/smoke-spec/catalogue.yaml`` (schema in the
issue body; parsed by :func:`parse_catalogue` into :class:`Journey`
objects), :func:`build_exploration_briefing` walks that lane's journeys
instead — filtered by :attr:`BugbashLane.driver_kind`, ordered by
``priority`` (:func:`journeys_for_lane`), each with its own declared
``expected``/``reference``/``reference_detail`` given to the worker
verbatim. A ``reference: nvim`` journey's expected outcome must be
established by actually running the same keystrokes through
``nvim --headless`` (never reasoned from memory); a ``mode: vscode``
journey must be run after switching the app into that mode. A missing or
invalid catalogue falls back to :data:`EXPLORATION_CHECKLIST` with a
visible ``NOTE:`` in the briefing — never silently, never crashing the
run. Findings may carry an optional :attr:`Finding.journey_id`, and every
lane worker also reports per-journey coverage
(:data:`COVERAGE_FENCE`/:func:`parse_coverage_block`/
:class:`CoverageSummary`) so a clean round reads as "N journeys passed,"
not "nothing was reported."

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
against a fake explorer in ``tests/test_bugbash.py``; as of #3566 the
production explorer (:func:`coord.commands.bugbash._dispatch_and_await_lane`)
also sets ``unavailable`` — it detects a lane worker's own "unavailable"
report (:func:`parse_unavailable_report`) and the real ``mac-native`` driver
now emits a genuine ``status="unavailable"`` verdict for a denied
Accessibility-trust grant (see :mod:`coord.mac_native_driver`'s own module
docstring), so this is reachable in production, not just in a test fixture.
A real two-lane live dry run against the fleet is still outstanding — see
``coord.commands.bugbash``'s own "KNOWN GAP" note.

**#3566: lane routing cross-references live ``/health`` probes, not just the
static ``coordinator.yml`` claim.** :func:`_pick_lane_machine` mirrors
:mod:`coord.smoke`'s ``dispatch_smoke`` cross-check
(:func:`coord.smoke._capability_probe_reasons`): a machine whose declared
capability is contradicted by its own live probe (e.g. ``macos`` declared
but Accessibility trust denied, or Screen Recording denied) is skipped
rather than picked, so ``coord bugbash`` refuses to route to it the same way
``dispatch_smoke`` already does — not just in `/health`'s own JSON, but in
the machine selection that actually dispatches a worker.

**#3581: a route can ask to run once per OS, not once on any capable
machine.** Before this, lane discovery picked exactly ONE machine per
driver ``kind`` — fine for ``win-native``/``mac-native``/``gtk-native``
(genuinely one OS each), but wrong for ``tui-pty``: its ``UnixPtyChild``
covers BOTH Linux and macOS, declared via a single ``capability: rust``, so
discovery always landed on whichever Rust box sorted first (in practice
always the Linux one) and macOS never got a lane at all. A route now
declares :attr:`coord.config.AcceptanceDriverConfig.platforms` (e.g.
``[linux, macos]``) to get one :class:`BugbashLane` per listed platform,
labelled ``f"{kind}:{os_name}"`` (e.g. ``"tui-pty:macos"``) so dedupe/
titles never conflate a macOS-only finding with a Linux one. A listed
platform with no capable machine is reported as an :class:`UnavailableLane`
rather than silently omitted (see :func:`discover_lanes`). A route that
doesn't set ``platforms`` is completely unaffected.

**#3615: the briefing/dry-run name the matching route's real setup and
launch command, not a ``'<app launch command>'`` placeholder.** Before
this, every lane's ``coord app-drive open``/``run-spec`` usage line in both
:func:`build_exploration_briefing` and ``coord bugbash --dry-run``
(:mod:`coord.commands.bugbash`) carried the literal placeholder text
regardless of what ``coordinator.yml`` actually declared for that route —
a lane worker had to guess the build/launch recipe from repo docs, which
once led a worker to trust a stale doc claiming a backend had been removed
and give up rather than build it. :func:`discover_lanes` now copies each
resolved route's own ``setup``/``run`` strings onto the
:class:`BugbashLane` it returns (:attr:`BugbashLane.setup`/
:attr:`BugbashLane.launch_command`), and :func:`driver_command_for_lane` /
:func:`build_exploration_briefing` substitute them into the ``--launch``
value and an explicit "build/provision this lane once" step. A route's own
``label`` (:attr:`coord.config.AcceptanceDriverConfig.label`) also now
disambiguates two sibling routes that share one ``kind`` (e.g. vimcode's
``win-gui``/``win-terminal`` routes, both ``kind: win-native``) into
distinct lane labels (``"win-native:gui"`` / ``"win-native:terminal"``)
the same way #3581 disambiguates ``platforms`` — so ``--lane`` can select
one without the other.

**#3620: one worker per lane per round covered about 20% of a catalogue**
(vimcode's ~167-journey ``tui-pty`` lane: 32 passed + 2 found out of 167
attempted in round 1, and round 2 re-walked from the top instead of
picking up where round 1 left off — two rounds together barely covered
more than one). :func:`plan_lane_chunks`/:func:`chunk_journeys` split a
lane's (optionally ``--max-priority``-filtered, see
:func:`journeys_for_lane`) journey list into chunks of
``--journeys-per-worker`` (:data:`DEFAULT_JOURNEYS_PER_WORKER`, ~25),
dispatching one worker per chunk — serially for a GUI lane (one desktop,
one focus: :func:`max_concurrent_chunks_for_lane` always returns ``1`` for
:data:`GUI_LANE_DRIVER_KINDS`), or concurrently up to the host's own
``max_workers`` for ``tui-pty`` (each worker gets its own pty, nothing to
contend over). :class:`JourneyScheduler` is the coverage-aware part: one
instance per lane, kept for the whole run, that hands back each round's
chunk list ordered skipped-or-not-yet-reached FIRST, then already-passed
journeys — so round N+1 picks up where round N left off instead of
re-walking from the top — and never auto-requeues a journey that already
produced a finding. :func:`explore_lane_sharded` is the ONE function that
ties chunking, the scheduler, and a run-wide chunk-level cost accumulator
(:class:`_ShardCostState` — #3620 requirement 4: a cap must stop a NEW
chunk from starting mid-round, not just between rounds) together into a
single aggregate :class:`ExploreOutcome` per lane per round — a drop-in
:data:`Explorer`, so :func:`run_bugbash`'s own round loop needed zero
changes to support sharding at all.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Sequence

import yaml

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

#: #3590: `coord app-drive`'s CLI puts the `kind` argument INSIDE each
#: subcommand (`_KIND_ARG` in `coord/commands/app_drive.py` is applied to
#: `open` and `run-spec` individually), so the real invocation order is
#: ``coord app-drive open KIND ...`` / ``coord app-drive run-spec KIND
#: SPEC ...`` — never ``coord app-drive KIND open ...``. These three
#: helpers build that order directly from `driver_kind`, computed, not
#: hand-maintained per kind, so a new :data:`LANE_DRIVER_KINDS` entry is
#: automatically runnable with no parallel table to keep in sync (closing
#: the exact drift #3590 reports: a briefing that reversed the argument
#: order, or named a DIFFERENT lane's driver). `build_exploration_briefing`'s
#: HARD RULE/usage example AND `coord bugbash REPO --dry-run`'s per-lane
#: preview (:func:`driver_command_for_lane`, :mod:`coord.commands.bugbash`)
#: both call these, so the two surfaces can never name two different
#: commands for the same lane (#2096 "one question, one answer").
#:
#: **#3615: `--launch` is the matching route's own `run:` command, not a
#: literal placeholder.** *launch_command*, when given, is the exact
#: ``run:`` shell command declared for this lane's route in
#: ``coordinator.yml`` (threaded through via :attr:`BugbashLane.
#: launch_command`, set by :func:`discover_lanes`) — the same string
#: :func:`coord.acceptance_drivers.run_driver` passes as ``launch_command``/
#: ``run_command`` when `coord acceptance run` drives this kind, so a
#: bugbash worker and the acceptance driver are told to launch the
#: identical command. Left empty (no lane context, or an older caller),
#: this falls back to the ``'<app launch command>'`` placeholder it always
#: used — never a hard requirement, since a worker could in principle be
#: pointed at a lane this module doesn't know the route config for.
#: ``--cwd`` stays a placeholder either way: it is the worker's own
#: checkout path, which only the dispatched worker (not this module) knows.
def app_drive_open_usage(driver_kind: str, launch_command: str = "") -> str:
    """The exact, runnable ``coord app-drive open KIND ...`` invocation for
    *driver_kind*, with ``--launch`` filled in from *launch_command* when
    given (#3615) rather than left as the ``'<app launch command>'``
    placeholder."""
    launch = launch_command or "<app launch command>"
    return f"coord app-drive open {driver_kind} --launch '{launch}' --cwd '<repo checkout dir>'"


def app_drive_run_spec_usage(driver_kind: str, launch_command: str = "") -> str:
    """The exact, runnable ``coord app-drive run-spec KIND SPEC ...``
    invocation for *driver_kind*, with ``--launch`` filled in from
    *launch_command* when given (#3615)."""
    launch = launch_command or "<app launch command>"
    return (
        f"coord app-drive run-spec {driver_kind} <spec-file> "
        f"--launch '{launch}' --cwd '<repo checkout dir>'"
    )


#: The second usage line (sending one input event to an already-open
#: session). `send`/`screen`/`probe`/`close`/`wait-idle` are all SIBLING
#: subcommands of `coord app-drive` (none of them take `kind` — the open
#: session file already carries it), so this line is identical for every
#: lane kind; it is not a per-kind table. `--screen`/`--probe`/`--close`
#: are NOT options of `send` (they are the separate subcommands named in
#: the trailing comment) — advertising them as `send` flags was the other
#: half of #3590's unrunnable-command bug.
_APP_DRIVE_SEND_USAGE_LINE = (
    "coord app-drive send --session <id> --key Enter   # or --text/--click; "
    "see also: screen/probe/close --session <id>"
)


def driver_command_for_lane(lane: "BugbashLane") -> str:
    """The exact, runnable `coord app-drive` command this lane's worker
    opens a session with — e.g. ``"coord app-drive open tui-pty --launch "
    "'<app launch command>' --cwd '<repo checkout dir>'"`` for a
    ``driver_kind="tui-pty"`` lane (#3590), with ``--launch`` filled in from
    :attr:`BugbashLane.launch_command` (#3615) when the lane carries one.
    The ONE function both :func:`build_exploration_briefing`'s HARD RULE and
    ``coord bugbash REPO --dry-run``'s per-lane preview (:mod:`coord.
    commands.bugbash`) call, so the two surfaces can never drift apart
    (#2096 "one question, one answer") — fixing the bug this issue reports:
    a briefing that named a DIFFERENT lane's driver module, reversed
    `open`/`kind` argument order, named no runnable command at all, or (#3615)
    handed the worker an unresolved ``'<app launch command>'`` placeholder
    to guess at.
    """
    return app_drive_open_usage(lane.driver_kind, lane.launch_command)


def _app_drive_usage_lines(driver_kind: str, launch_command: str = "") -> tuple[str, str]:
    """The 2-line usage example for *driver_kind*: open the lane's own
    kind, then send one input event to the resulting session. Built
    directly from `driver_kind` (see the helpers above) — never a by-hand
    per-kind table, so there is nothing to fall out of sync. *launch_command*
    (#3615) threads through to :func:`app_drive_open_usage`."""
    return app_drive_open_usage(driver_kind, launch_command), _APP_DRIVE_SEND_USAGE_LINE


#: The exploration checklist every lane walks on top of the repo's Tier-2
#: smoke spec (issue #3487's "panels, menus, extension install flow,
#: terminal, splits, themes, idle stability"). Ordered so a worker that runs
#: out of budget mid-checklist still covers the highest-signal areas first.
EXPLORATION_CHECKLIST: tuple[str, ...] = (
    "panels", "menus", "extension install flow", "terminal", "splits",
    "themes", "idle stability",
)

#: Repo-root-relative path of the optional behaviour catalogue this module
#: walks instead of :data:`EXPLORATION_CHECKLIST` when the app repo supplies
#: one (#3580) — the shared contract between claude-coordinator and the app
#: repo, documented in this module's own docstring and the issue body.
CATALOGUE_PATH = "tests/smoke-spec/catalogue.yaml"

#: The only catalogue schema version this module understands (#3580). A
#: catalogue naming any other value is treated as invalid (falls back to
#: :data:`EXPLORATION_CHECKLIST`, never guessed at) rather than parsed
#: best-effort against a schema it might not actually match.
CATALOGUE_VERSION = 1

#: ``reference`` values whose expected outcome requires establishing it by
#: running the SAME keystrokes through a live oracle rather than reasoning
#: from memory — currently just ``"nvim"`` (#3580 requirement 3). Kept as
#: its own constant (rather than a literal string check) so a future
#: oracle-backed reference doesn't require hunting down every place
#: ``"nvim"`` is compared.
ORACLE_BACKED_REFERENCES: tuple[str, ...] = ("nvim",)

#: Fenced-code-block language tag a lane worker's final message must use to
#: report its PER-JOURNEY coverage (#3580 requirement 5) — attempted/passed/
#: found/skipped, so a bugbash run summary can report "N journeys passed"
#: instead of inferring coverage from the findings list alone (a lane that
#: silently skipped half its journeys would otherwise look identical to one
#: that ran everything and found nothing wrong). Reported for every item the
#: worker walked, whether it came from a repo catalogue journey or the
#: fallback :data:`EXPLORATION_CHECKLIST` (using the checklist item's own
#: text as its id) — see :func:`build_exploration_briefing`.
COVERAGE_FENCE = "bugbash-coverage"

#: Fenced-code-block language tag a lane worker's final message must use to
#: report its findings — mirrors how :mod:`coord.acceptance_drivers` forces
#: each framework's own structured report format rather than parsing prose.
FINDINGS_FENCE = "bugbash-findings"

#: Shared with :func:`parse_unavailable_report` (#3628): the ONE regex both
#: :func:`parse_findings_block` and the unavailable-signature guard use to
#: detect a well-formed ```` ```bugbash-findings ```` fence — so "does this
#: text carry a findings fence" is answered identically everywhere (#2096
#: "one question, one answer"), rather than two independently-drifting
#: `re.compile` calls.
_FINDINGS_FENCE_RE = re.compile(
    rf"```{re.escape(FINDINGS_FENCE)}\s*\n(.*?)```", re.DOTALL,
)

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
    #: #3580 requirement 2: the catalogue :class:`Journey` id this finding
    #: relates to, when the lane worker was walking a repo-supplied
    #: catalogue rather than the generic checklist — ``""`` when there was
    #: no catalogue, the journey didn't come from one (fallback checklist
    #: item), or the worker simply didn't name one. Optional — never
    #: required, never defaulted to a placeholder (unlike
    #: :data:`_REQUIRED_FINDING_FIELDS`), since plenty of real findings
    #: genuinely have no journey to cite. Round-trips verbatim into the
    #: filed issue body (:func:`_evidence_with_acceptance`) so the fixer
    #: knows which journey — and therefore which reference oracle — the
    #: expected behaviour came from.
    journey_id: str = ""


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
        journey_id=str(entry.get("journey_id", "")).strip(),
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
    match = _FINDINGS_FENCE_RE.search(text)
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
#: caught. Kept narrow and literal: `"no unlocked GUI session is available"`/
#: `"the screen is locked"`/`"is not on the console"`/
#: `"AXIsProcessTrusted() is False"` are the exact strings
#: `coord.mac_native_driver`'s own `session_available`/`ax_trust_available`
#: precheck now actually emits in production (#3566) — reachable, not just
#: hand-written in a test. `"AXIsProcessTrusted() returned False"` and
#: `"CGPreflightScreenCaptureAccess"` are defensive-only: no in-tree driver
#: emits that exact phrasing today, but `coord.prereqs`'s own `/health` probe
#: text and a future Screen-Recording driver precheck are plausible sources,
#: and keeping the signature narrow and literal costs nothing. None of these
#: are a vague substring that could false-positive on an unrelated mention of
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


def parse_unavailable_report(text: str, *, final_message: str = "") -> str:
    """The lane-unavailable reason from *text* (a lane worker's full
    transcript), or ``""`` if none is present (#3566).

    First checks for the authoritative fenced
    ```` ```bugbash-unavailable ```` block the briefing instructs a worker
    to write when it hits a missing permission or absent session rather
    than improvising a workaround (ask #4's hard rule), searched over the
    full *text* so it is still caught even if reported mid-session before a
    crash. Falls back to :data:`_UNAVAILABLE_SIGNATURES` — a driver-level
    session/permission failure surfacing directly in a tool result even
    without the worker's own cooperation.

    *final_message* — the lane worker's own LAST assistant message, as
    opposed to the full transcript in *text* — decides whether that
    signature fallback is even reachable (#3628). A driver call that
    returned ``"status": "unavailable"`` mid-session and was then worked
    around still leaves that literal string somewhere in *text*; if the
    worker's final message goes on to carry a well-formed
    ```` ```bugbash-findings ```` fence, that fence is the worker's own
    authoritative report and must decide the outcome, so the stale
    mid-session signature is never allowed to override it. Only when
    *final_message* carries NEITHER a findings fence NOR an unavailable
    fence does the transcript-wide signature scan run. Passing
    ``final_message=""`` (the default) disables this guard — callers that
    cannot distinguish the final message from the full transcript keep the
    pre-#3628 behavior, which only ever makes the fallback MORE eager, never
    less.

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
    if final_message and (
        _FINDINGS_FENCE_RE.search(final_message)
        or _UNAVAILABLE_FENCE_RE.search(final_message)
    ):
        return ""
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


# ── behaviour catalogue (#3580) ─────────────────────────────────────────


@dataclass(frozen=True)
class Journey:
    """One journey declared in a repo's :data:`CATALOGUE_PATH` (#3580's
    catalogue schema v1). Every field here mirrors the YAML schema in the
    issue body exactly — this is the shared contract between
    claude-coordinator and the app repo, so field names must never drift
    from what a repo's ``catalogue.yaml`` actually writes.
    """

    id: str
    area: str = ""
    #: ``"vim"`` | ``"vscode"`` | ``"any"``.
    mode: str = "any"
    lanes: tuple[str, ...] = ()
    #: ``"nvim"`` | ``"vscode"`` | ``"platform"`` | ``"spec"`` — where
    #: ``expected`` comes from. See :data:`ORACLE_BACKED_REFERENCES`.
    reference: str = ""
    reference_detail: str = ""
    steps: str = ""
    expected: str = ""
    #: 1 = must work for the release, 3 = nice to have. Lower sorts first
    #: (:func:`journeys_for_lane`) so a worker that runs out of budget
    #: mid-walk still covered the highest-priority journeys first.
    priority: int = 3


@dataclass(frozen=True)
class CatalogueResult:
    """What :func:`parse_catalogue` extracted from a repo's
    ``catalogue.yaml`` text (#3580).

    ``warning`` is non-empty for EVERY problem short of a perfectly clean
    catalogue — missing/blank text, unparseable YAML, the wrong top-level
    shape, an unsupported ``version``, a catalogue with no valid journeys
    at all, or (non-fatally) one or more individual journey entries that
    had to be dropped for missing a required field. ``journeys`` is never
    partially trusted silently: either the catalogue produced at least one
    valid journey (``journeys`` non-empty, ``warning`` possibly still
    non-empty if SOME entries were dropped) or it produced none at all
    (``journeys == ()``, ``warning`` always non-empty) — a caller never has
    to guess which case it's in, since checking ``bool(journeys)`` alone is
    always the right test for "do I have anything to walk."
    """

    journeys: tuple[Journey, ...] = ()
    warning: str = ""
    #: :data:`CATALOGUE_PATH` when at least one journey parsed successfully
    #: (even if the catalogue carries other problems); ``""`` otherwise —
    #: lets a caller distinguish "used the catalogue" from "fell back"
    #: without re-deriving that from ``journeys``/``warning`` itself.
    source: str = ""


#: Per-journey-entry fields with no reasonable default — an entry missing
#: any of these is dropped (not fatal to the rest of the catalogue) by
#: :func:`parse_catalogue`.
_REQUIRED_JOURNEY_FIELDS: tuple[str, ...] = ("id", "lanes", "reference", "expected")


def parse_catalogue(yaml_text: str | None) -> CatalogueResult:
    """Parse and validate a repo's :data:`CATALOGUE_PATH` text (#3580).

    NEVER raises — every failure mode (missing/blank text, invalid YAML,
    the wrong top-level shape, an unsupported ``version``, individual
    journey entries missing a required field, a catalogue with zero valid
    journeys) becomes a non-empty :attr:`CatalogueResult.warning` with
    ``journeys=()`` (or, for a per-entry drop, journeys minus the dropped
    entries) rather than a crash or a silent empty catalogue indistinguishable
    from "repo declared zero journeys on purpose." :func:`build_exploration_briefing`
    is the sole caller that turns a non-empty ``warning``/empty ``journeys``
    into the :data:`EXPLORATION_CHECKLIST` fallback — this function only
    decides what's valid, never what to do about it.
    """
    if not yaml_text or not yaml_text.strip():
        return CatalogueResult(
            warning=f"no catalogue found at {CATALOGUE_PATH}",
        )
    try:
        raw = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        return CatalogueResult(warning=f"{CATALOGUE_PATH} failed to parse as YAML: {e}")
    if not isinstance(raw, dict):
        return CatalogueResult(
            warning=f"{CATALOGUE_PATH} must be a YAML mapping at the top level, "
            f"got {type(raw).__name__}",
        )
    version = raw.get("version")
    if version != CATALOGUE_VERSION:
        return CatalogueResult(
            warning=f"{CATALOGUE_PATH} version {version!r} is not supported "
            f"(expected {CATALOGUE_VERSION})",
        )
    raw_journeys = raw.get("journeys")
    if not isinstance(raw_journeys, list) or not raw_journeys:
        return CatalogueResult(warning=f"{CATALOGUE_PATH} has no `journeys` list")

    journeys: list[Journey] = []
    dropped: list[str] = []
    seen_ids: set[str] = set()
    for entry in raw_journeys:
        if not isinstance(entry, dict):
            dropped.append("<non-mapping journey entry>")
            continue
        jid = str(entry.get("id", "")).strip()
        lanes_raw = entry.get("lanes")
        missing = [
            f for f in _REQUIRED_JOURNEY_FIELDS
            if not (
                str(entry.get(f, "")).strip()
                if f != "lanes" else isinstance(lanes_raw, list) and lanes_raw
            )
        ]
        if missing:
            dropped.append(f"{jid or '<missing id>'} (missing: {', '.join(missing)})")
            continue
        if jid in seen_ids:
            dropped.append(f"{jid} (duplicate id)")
            continue
        seen_ids.add(jid)
        try:
            priority = int(entry.get("priority", 3))
        except (TypeError, ValueError):
            priority = 3
        journeys.append(
            Journey(
                id=jid,
                area=str(entry.get("area", "")).strip(),
                mode=str(entry.get("mode", "any")).strip() or "any",
                lanes=tuple(str(l) for l in lanes_raw),
                reference=str(entry.get("reference", "")).strip(),
                reference_detail=str(entry.get("reference_detail", "")).strip(),
                steps=str(entry.get("steps", "")).strip(),
                expected=str(entry.get("expected", "")).strip(),
                priority=priority,
            )
        )

    if not journeys:
        return CatalogueResult(
            warning=f"{CATALOGUE_PATH} had no valid journeys (all "
            f"{len(dropped)} entr{'y' if len(dropped) == 1 else 'ies'} invalid/dropped)",
        )
    warning = ""
    if dropped:
        warning = (
            f"{CATALOGUE_PATH}: dropped {len(dropped)} invalid journey "
            f"entr{'y' if len(dropped) == 1 else 'ies'}: {'; '.join(dropped)}"
        )
    return CatalogueResult(journeys=tuple(journeys), warning=warning, source=CATALOGUE_PATH)


def journeys_for_lane(
    journeys: Sequence[Journey], driver_kind: str, *, max_priority: int | None = None,
) -> list[Journey]:
    """Journeys from *journeys* applicable to *driver_kind*, in priority
    order (1 first), ties broken by ``id`` for a deterministic walk order
    (#3580 requirement 1: "a worker that runs out of budget has covered
    priority 1 first").

    *max_priority* (#3620 requirement 2) drops any journey whose own
    ``priority`` is numerically GREATER than it — e.g. ``max_priority=1``
    keeps only priority-1 journeys — applied BEFORE the priority/id sort,
    so a lower cap never changes the relative order of what's left.
    ``None`` (the default, and every pre-#3620 caller) applies no filter
    at all, so this stays fully backward compatible."""
    matching = [j for j in journeys if driver_kind in j.lanes]
    if max_priority is not None:
        matching = [j for j in matching if j.priority <= max_priority]
    return sorted(matching, key=lambda j: (j.priority, j.id))


#: #3620 requirement 1: the default chunk size `coord bugbash run
#: --journeys-per-worker` splits a lane's (post `--max-priority`) journey
#: list into — one worker dispatched per chunk, instead of the whole lane's
#: catalogue slice handed to a single time-boxed session (which measured
#: ~20% coverage per round on vimcode's ~60/80/18 priority tiers, #3620).
DEFAULT_JOURNEYS_PER_WORKER = 25

#: The GUI driver kinds among :data:`LANE_DRIVER_KINDS` — derived, never a
#: hand-maintained parallel list, so a future `LANE_DRIVER_KINDS` entry is
#: automatically classified as GUI (one desktop, one focus, chunks must
#: serialize) unless it's `tui-pty` (own pty per worker, chunks may
#: overlap) — see :func:`max_concurrent_chunks_for_lane`.
GUI_LANE_DRIVER_KINDS: frozenset[str] = frozenset(LANE_DRIVER_KINDS) - {"tui-pty"}


def chunk_journeys(journeys: Sequence[Journey], size: int) -> list[tuple[Journey, ...]]:
    """Split *journeys* (already ordered by the caller — see
    :func:`journeys_for_lane`/:class:`JourneyScheduler`) into chunks of at
    most *size* each, in order (#3620 requirement 1) — e.g. 60 journeys at
    ``size=25`` yields 3 chunks (25/25/10). ``size <= 0`` is treated
    defensively as "everything in one chunk" (the CLI's own
    ``--journeys-per-worker`` is validated to be a positive int, but this
    function must never divide by zero or infinite-loop for a caller that
    skips that check). Empty *journeys* yields ``[]`` — zero chunks, the
    signal a lane has nothing (left) to shard, handled by
    :func:`explore_lane_sharded`'s checklist fallback."""
    if not journeys:
        return []
    if size <= 0:
        size = len(journeys)
    return [tuple(journeys[i:i + size]) for i in range(0, len(journeys), size)]


@dataclass(frozen=True)
class LaneChunkPlan:
    """What ``--dry-run`` and the live sharded dispatcher both compute for
    ONE lane's chunking (#3620 requirements 1/2): the journeys left after
    ``--max-priority`` filtering, and the resulting ordered chunk list.
    :func:`plan_lane_chunks` is the ONE function both
    :mod:`coord.commands.bugbash`'s ``--dry-run`` preview and (via
    :class:`JourneyScheduler`, which is seeded from this same journeys
    list) the live dispatcher consult, so the two can never report a
    different chunk count for the same lane (#2096 "one question, one
    answer")."""

    platform: str
    journeys: tuple[Journey, ...]
    chunks: tuple[tuple[Journey, ...], ...]

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)


def plan_lane_chunks(
    lane: "BugbashLane",
    catalogue_journeys: Sequence[Journey],
    *,
    journeys_per_worker: int = DEFAULT_JOURNEYS_PER_WORKER,
    max_priority: int | None = None,
) -> LaneChunkPlan:
    """*lane*'s journeys (from the repo's full *catalogue_journeys* list,
    filtered to this lane's ``driver_kind`` and *max_priority* via
    :func:`journeys_for_lane`), split into chunks of *journeys_per_worker*
    (#3620 requirements 1/2)."""
    lane_journeys = journeys_for_lane(catalogue_journeys, lane.driver_kind, max_priority=max_priority)
    chunks = chunk_journeys(lane_journeys, journeys_per_worker)
    return LaneChunkPlan(platform=lane.platform, journeys=tuple(lane_journeys), chunks=tuple(chunks))


def max_concurrent_chunks_for_lane(lane: "BugbashLane", host_max_workers: int) -> int:
    """How many of *lane*'s chunks may be dispatched to its host at once
    this round (#3620 requirement 1).

    GUI lanes (:data:`GUI_LANE_DRIVER_KINDS` — ``win-native``/
    ``mac-native``/``gtk-native``) always return ``1``: they share one
    desktop and one input focus, so two chunks running concurrently would
    fight over it exactly the way two concurrent GUI *lanes* already would
    (#3602's own per-host lane serialization) — *host_max_workers* is
    irrelevant for these. A ``tui-pty`` lane returns
    ``max(1, host_max_workers)``: each worker gets its own pty, so there is
    nothing to contend over, and the host's own configured capacity
    (``machines[].max_workers`` / ``concurrency.max_workers``) is the only
    real ceiling."""
    if lane.driver_kind in GUI_LANE_DRIVER_KINDS:
        return 1
    return max(1, host_max_workers)


#: Per-journey cumulative status :class:`JourneyScheduler` tracks across
#: every round/chunk dispatched so far this run (#3620 requirement 3) —
#: distinct from :class:`JourneyOutcome.status`, which is what ONE chunk's
#: worker reported for ONE round; this is the latest-known value, carried
#: forward whennever a journey isn't re-walked in a later round.
_JOURNEY_NOT_RUN = "not_run"


@dataclass
class JourneyScheduler:
    """Coverage-aware per-lane journey scheduler (#3620 requirement 3):
    decides which of *journeys* the lane's NEXT round should walk, and
    tracks the cumulative, latest-known status of every one of them across
    every round/chunk dispatched so far this run — the data
    :func:`explore_lane_sharded` consults every time it's asked for this
    lane's next round, and the source a run's final cumulative-coverage
    summary is built from (never re-derived by summing each round's own
    :class:`CoverageSummary`, which would double-count a journey re-walked
    in a later round).

    :meth:`chunks_for_round` orders *journeys* skipped-or-not-yet-run
    FIRST (in their original priority order), then already-passed ones —
    so a lane with chunk budget left after clearing its backlog
    re-verifies past journeys rather than sitting idle. A journey that
    produced a FINDING is never automatically re-queued by this scheduler
    (#3620: "re-run only to confirm a fix, if at all" — there is no
    automatic "a fix landed" signal this engine can observe; a deliberate
    re-walk of a `found` journey, if it ever happens, is a separate,
    explicit invocation, not something this scheduler does on its own).

    One instance is created per lane for the whole run (not per round) and
    threaded into every round's :func:`explore_lane_sharded` call for that
    lane — it is the ONLY place "what has this lane covered so far"
    lives, across however many chunks/rounds that took.
    """

    journeys: tuple[Journey, ...]
    journeys_per_worker: int = DEFAULT_JOURNEYS_PER_WORKER
    _status: dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for j in self.journeys:
            self._status.setdefault(j.id, _JOURNEY_NOT_RUN)

    def _ordered_for_round(self) -> list[Journey]:
        rank = {_JOURNEY_NOT_RUN: 0, "skipped": 0, "passed": 1}
        pending = [
            j for j in self.journeys if self._status.get(j.id, _JOURNEY_NOT_RUN) != "found"
        ]
        return sorted(
            pending,
            key=lambda j: (rank.get(self._status.get(j.id, _JOURNEY_NOT_RUN), 0), j.priority, j.id),
        )

    def chunks_for_round(self) -> list[tuple[Journey, ...]]:
        """This round's ordered chunk list (#3620 requirements 1 + 3) —
        empty when every journey already produced a finding (nothing left
        this scheduler will auto re-queue) or *journeys* is empty (no
        catalogue journeys for this lane at all)."""
        return chunk_journeys(self._ordered_for_round(), self.journeys_per_worker)

    def record(self, outcomes: Sequence["JourneyOutcome"]) -> None:
        """Update the cumulative status for every journey *outcomes*
        reports on — called once per CHUNK's coverage report (never once
        per round, since #3620 lets a round dispatch several chunks). An
        outcome naming a journey id this scheduler doesn't track (e.g. a
        checklist item's own text, when this lane fell back to the
        checklist) is silently ignored — only catalogue journeys are
        scheduled/tracked here."""
        for outcome in outcomes:
            if outcome.journey_id in self._status:
                self._status[outcome.journey_id] = outcome.status

    def cumulative_summary(self) -> "CoverageSummary":
        """"N journeys passed" across EVERY round/chunk dispatched so far
        this run (#3620 requirement 3) — not just the terminating round's
        own report, which is all :attr:`RoundReport.lane_coverage` ever
        showed before this. ``attempted`` counts only journeys that have
        actually been given to a chunk worker at least once (i.e. not
        :data:`_JOURNEY_NOT_RUN`) — a journey never yet reached is not
        "attempted" any more than it would be mid-round; ``len(journeys) -
        attempted`` is how many are still not yet reached."""
        statuses = list(self._status.values())
        return CoverageSummary(
            attempted=sum(1 for s in statuses if s != _JOURNEY_NOT_RUN),
            passed=sum(1 for s in statuses if s == "passed"),
            found=sum(1 for s in statuses if s == "found"),
            skipped=sum(1 for s in statuses if s == "skipped"),
        )


@dataclass(frozen=True)
class JourneyOutcome:
    """One line of a lane worker's per-journey coverage report
    (:data:`COVERAGE_FENCE`, #3580 requirement 5)."""

    journey_id: str
    #: ``"passed"`` | ``"found"`` | ``"skipped"``.
    status: str
    #: Required (by convention of the briefing, not enforced here) when
    #: ``status == "skipped"`` — e.g. ``"no nvim"`` (#3580 requirement 3).
    reason: str = ""


_VALID_JOURNEY_STATUSES: tuple[str, ...] = ("passed", "found", "skipped")

_COVERAGE_FENCE_RE = re.compile(
    rf"```{re.escape(COVERAGE_FENCE)}\s*\n(.*?)```", re.DOTALL,
)


def parse_coverage_block(text: str) -> tuple[JourneyOutcome, ...]:
    """Extract the ```` ```bugbash-coverage ```` fenced JSON array from a
    lane worker's final message (#3580 requirement 5).

    Deliberately lenient — unlike :func:`parse_findings_block`, a missing or
    malformed coverage block is NOT a protocol error: the findings fence
    remains the authoritative "did this lane produce a trustworthy report"
    signal, and coverage is purely an informational summary layered on top.
    A missing fence, invalid JSON, a non-list payload, or an individual
    entry missing ``journey_id``/a recognised ``status`` simply contributes
    nothing to the summary rather than failing the round. Never raises.
    """
    match = _COVERAGE_FENCE_RE.search(text)
    if match is None:
        return ()
    try:
        raw = json.loads(match.group(1))
    except (ValueError, TypeError):
        return ()
    if not isinstance(raw, list):
        return ()
    outcomes: list[JourneyOutcome] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        journey_id = str(entry.get("journey_id", "")).strip()
        status = str(entry.get("status", "")).strip().lower()
        if not journey_id or status not in _VALID_JOURNEY_STATUSES:
            continue
        outcomes.append(
            JourneyOutcome(
                journey_id=journey_id,
                status=status,
                reason=str(entry.get("reason", "")).strip(),
            )
        )
    return tuple(outcomes)


@dataclass(frozen=True)
class CoverageSummary:
    """Per-lane journey coverage counts (#3580 requirement 5) — "N journeys
    passed" instead of inferring coverage from the findings list alone."""

    attempted: int = 0
    passed: int = 0
    found: int = 0
    skipped: int = 0
    #: One entry per skipped journey, in report order — e.g. ``("no nvim",)``
    #: — so an operator can see WHY without cross-referencing the raw
    #: transcript.
    skip_reasons: tuple[str, ...] = ()

    @classmethod
    def from_outcomes(cls, outcomes: Sequence[JourneyOutcome]) -> "CoverageSummary":
        skipped_outcomes = [o for o in outcomes if o.status == "skipped"]
        return cls(
            attempted=len(outcomes),
            passed=sum(1 for o in outcomes if o.status == "passed"),
            found=sum(1 for o in outcomes if o.status == "found"),
            skipped=len(skipped_outcomes),
            skip_reasons=tuple(o.reason or "no reason recorded" for o in skipped_outcomes),
        )


# ── lanes ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BugbashLane:
    """One platform lane: a native/PTY acceptance driver paired with a
    specific capable machine to run it on.

    ``platform`` is the lane's display/dedupe label — ``driver_kind`` for a
    single-platform driver (today's behaviour, unchanged),
    ``f"{driver_kind}:{os_name}"`` (e.g. ``"tui-pty:macos"``) for one lane
    of a :attr:`coord.config.AcceptanceDriverConfig.platforms`-bearing
    route (#3581) — so a macOS-only finding from a route that also runs on
    Linux is never deduped/titled as if it were the same lane — or
    ``f"{driver_kind}:{label}"`` (e.g. ``"win-native:gui"``) for a route
    that sets :attr:`coord.config.AcceptanceDriverConfig.label` (#3615)
    because a sibling route shares the same ``kind`` (e.g. vimcode's
    ``win-gui``/``win-terminal`` routes, both ``kind: win-native``) — see
    :func:`discover_lanes`.

    ``setup``/``launch_command`` (#3615) carry the matching route's own
    ``setup:``/``run:`` strings from ``coordinator.yml`` verbatim — the
    SAME strings :func:`coord.acceptance_drivers.run_driver` runs/launches
    for ``coord acceptance run`` — so :func:`driver_command_for_lane` and
    :func:`build_exploration_briefing` can hand a lane worker the real
    build-and-launch recipe instead of a ``'<app launch command>'``
    placeholder it would otherwise have to guess at (the bug this issue
    reports). Both default to ``""`` — unchanged behaviour (the placeholder
    fallback) for a lane built without route context.
    """

    platform: str
    driver_kind: str
    machine: str
    capability: str
    reference: bool = False
    setup: str = ""
    launch_command: str = ""


@dataclass(frozen=True)
class UnavailableLane:
    """A :attr:`coord.config.AcceptanceDriverConfig.platforms` (#3581) entry
    with no configured machine that both claims *capability* AND that OS —
    discovery-time absence, reported through :func:`discover_lanes`'s
    *unavailable_out* rather than silently dropped the way a plain
    (non-``platforms``) route with no capable machine always has been
    (there, "no lane" already fully describes the situation; here, the
    sibling platform DOES have a lane, so silently omitting this one would
    read as "every platform was checked" when one never was)."""

    driver_kind: str
    os_name: str
    capability: str

    @property
    def platform(self) -> str:
        """Same ``driver_kind:os_name`` label a resolved lane for this same
        route/platform would have carried, had a machine been found."""
        return f"{self.driver_kind}:{self.os_name}"


def discover_lanes(
    config: Any,
    repo_name: str,
    *,
    reference_backend: str = "",
    http_client: Any = None,
    unavailable_out: "list[UnavailableLane] | None" = None,
) -> list[BugbashLane]:
    """Derive *repo_name*'s bugbash lanes from its acceptance drivers,
    routed to a capable machine the same way
    :mod:`coord.smoke`'s ``capability_rules`` routes smoke legs.

    Walks the repo's top-level driver plus every ``routes:`` entry (mirrors
    :meth:`coord.config.AcceptanceConfig.entrypoints`'s walk), keeps only
    :data:`LANE_DRIVER_KINDS` entries, and drops any that has no configured
    machine listing both *repo_name* and the driver's ``capability`` AND
    whose live ``/health`` probe doesn't contradict that claim (#3566, see
    :func:`_pick_lane_machine`) — a lane with no capable machine is omitted
    rather than returned with ``machine=""``, so a caller never has to
    separately check "is this lane actually runnable." *reference_backend*,
    when it names one of the surviving lanes' ``platform``, marks that
    lane's ``reference=True``. *http_client*, when given, is forwarded to
    the ``/health`` cross-check (tests inject a fake; production leaves it
    ``None`` and gets a real ``httpx`` call).

    **#3581: a route declaring ``platforms`` (e.g. ``[linux, macos]``)
    yields one lane PER listed platform**, each independently resolved to a
    machine claiming BOTH the route's ``capability`` and that platform name
    (the same free-string capability vocabulary every other capability
    already uses — see :class:`coord.config.SmokeRule.platforms`'s
    docstring for why this doesn't invent a second "what OS is this
    machine" mechanism). A platform with no such machine is NOT silently
    dropped — unlike every other gap in this function, it is reported via
    *unavailable_out* (when the caller passes a list; appended to, never
    replaced) as an :class:`UnavailableLane`, since its sibling platform(s)
    DO get a lane and a caller needs to be able to tell "every platform ran"
    from "one platform silently never got picked." A route with an empty
    (the default) ``platforms`` behaves exactly as before #3581 — one lane,
    ``platform == driver_kind``.

    **#3615: a route's own ``label`` (:attr:`coord.config.
    AcceptanceDriverConfig.label`) disambiguates sibling routes that share
    one ``kind``** — e.g. vimcode's ``win-gui``/``win-terminal`` routes are
    BOTH ``kind: win-native``, so without this every such route produced a
    lane whose ``platform`` was just ``"win-native"`` for both, making
    ``--lane win-native`` (and dedupe/titling) unable to tell them apart.
    When a route sets ``label``, its lane's ``platform`` becomes
    ``f"{kind}:{label}"`` (e.g. ``"win-native:gui"``) the same way #3581's
    ``platforms`` already appends ``:{os_name}`` — a route with no
    ``label`` (the default) is completely unaffected. Every resolved lane
    also carries the route's own ``setup``/``run`` strings verbatim (see
    :attr:`BugbashLane.setup`/:attr:`BugbashLane.launch_command`) so a
    briefing/dry-run can hand the worker the real build-and-launch command
    instead of a placeholder.
    """
    entry = config.acceptance.drivers.get(repo_name)
    if entry is None:
        return []
    candidates = list(entry.routes) if entry.routes else [entry]

    lanes: list[BugbashLane] = []
    for cfg in candidates:
        if cfg.kind not in LANE_DRIVER_KINDS:
            continue
        route_label = getattr(cfg, "label", "") or ""
        base_platform = f"{cfg.kind}:{route_label}" if route_label else cfg.kind
        platforms = getattr(cfg, "platforms", None) or ()
        if not platforms:
            machine = _pick_lane_machine(
                config, repo_name, cfg.capability, http_client=http_client,
            )
            if machine is None:
                continue
            lanes.append(
                BugbashLane(
                    platform=base_platform,
                    driver_kind=cfg.kind,
                    machine=machine,
                    capability=cfg.capability,
                    reference=(base_platform == reference_backend),
                    setup=cfg.setup,
                    launch_command=cfg.run,
                )
            )
            continue
        for os_name in platforms:
            machine = _pick_lane_machine(
                config, repo_name, cfg.capability,
                os_name=os_name, http_client=http_client,
            )
            label = f"{base_platform}:{os_name}"
            if machine is None:
                if unavailable_out is not None:
                    unavailable_out.append(
                        UnavailableLane(
                            driver_kind=cfg.kind, os_name=os_name, capability=cfg.capability,
                        )
                    )
                continue
            lanes.append(
                BugbashLane(
                    platform=label,
                    driver_kind=cfg.kind,
                    machine=machine,
                    capability=cfg.capability,
                    reference=(label == reference_backend),
                    setup=cfg.setup,
                    launch_command=cfg.run,
                )
            )
    return lanes


def _pick_lane_machine(
    config: Any, repo_name: str, capability: str, *, os_name: str = "", http_client: Any = None,
) -> str | None:
    """The first configured machine that both claims *capability* (and, when
    given, *os_name* — #3581's one-lane-per-OS field, just another entry in
    the same capability vocabulary) in ``coordinator.yml`` AND
    repo-membership for *repo_name* — cross-checked against that machine's
    own live ``/health`` tool probes (#3566) before it's picked, not just
    the static claim.

    Before this fix, this function (and therefore every ``coord bugbash``
    dispatch) only ever asked ``capability in m.capabilities`` — a
    hand-written ``coordinator.yml`` claim nothing verified — while
    :mod:`coord.smoke`'s own ``dispatch_smoke`` already cross-referenced
    ``/health``'s ``tool_versions`` (:func:`coord.smoke
    ._capability_probe_reasons`) before routing smoke work. A ``macos``
    machine whose Accessibility/Screen-Recording trust (:mod:`coord.prereqs`'s
    ``macos-accessibility-trust``/``macos-screen-recording`` probes) was
    revoked therefore still looked dispatchable to bugbash even though
    ``/health`` itself would have said otherwise — the exact gap the first
    live ``mac-native`` attempt hit. Reusing
    :func:`coord.smoke._capability_probe_reasons` here (rather than a second,
    independently-drifting copy of the same dict) means ``coord bugbash``
    now refuses to route to a machine with a known-unmet required-capability
    prereq the same way ``dispatch_smoke`` already does, falling through to
    the next capable-on-paper machine (or returning ``None`` if none remain)
    instead of dispatching a worker that can never actually run the lane.
    """
    from coord.smoke import _capability_probe_reasons  # noqa: PLC0415 — avoid an import cycle

    required = [c for c in (capability, os_name) if c]
    for m in config.machines:
        if repo_name not in m.repos:
            continue
        if required and not all(c in m.capabilities for c in required):
            continue
        if required and _capability_probe_reasons(m, required, http_client=http_client):
            continue  # declared but the machine's own /health probe denies it
        return m.name
    return None


def build_exploration_briefing(
    lane: BugbashLane,
    *,
    reference_backend: str,
    checklist: Sequence[str] = EXPLORATION_CHECKLIST,
    catalogue_text: str | None = None,
    journeys_override: Sequence[Journey] | None = None,
) -> str:
    """Compose the seed briefing for a lane's headless exploration worker.

    Tells the worker to run the repo's Tier-2 smoke spec first, then walk
    either this lane's slice of a repo-supplied behaviour catalogue
    (#3580) or, absent/invalid one, the generic *checklist* — comparing
    behaviour to *reference_backend* (for checklist items) or to each
    journey's own declared ``reference``/``reference_detail`` (for
    catalogue journeys) and capturing evidence through the native driver's
    own probes. Ends with the exact contracts :func:`parse_findings_block`/
    :func:`parse_coverage_block` parse back out, so the briefing and the
    parsers can never silently drift apart (:data:`FINDINGS_FENCE`/
    :data:`COVERAGE_FENCE`, each used by both).

    *catalogue_text* is the raw text of the repo's :data:`CATALOGUE_PATH`
    (``None`` when the repo has none, or the fetch failed — see
    :func:`coord.commands.bugbash._fetch_catalogue_text`). Parsed via
    :func:`parse_catalogue` and filtered/ordered for this lane via
    :func:`journeys_for_lane`. Whenever that yields zero journeys — no
    *catalogue_text* at all, invalid YAML, a valid catalogue with no
    journey declaring this lane's :attr:`BugbashLane.driver_kind` in its
    ``lanes`` — this falls back to *checklist* (#3580's "A missing or
    invalid catalogue falls back to the current checklist with a visible
    warning. It must never fail silently or crash the run."): a ``NOTE:``
    line naming the reason is always included in that case, never a quiet
    substitution.

    *journeys_override* (#3620), when given (not ``None``), is used
    VERBATIM as this worker's journey list instead of re-deriving one from
    *catalogue_text* — the sharded dispatcher
    (:func:`explore_lane_sharded`) has already resolved exactly which
    journeys this CHUNK owns via :class:`JourneyScheduler`, and re-parsing
    *catalogue_text* here could in principle disagree with that (#2096
    "one question, one answer"). An empty tuple is a valid override
    (falls through to the *checklist* branch below, exactly like "no
    catalogue journeys for this lane" always has) — only ``None`` means
    "no override, derive it from *catalogue_text* the old way."
    """
    catalogue_warning = ""
    lane_journeys: list[Journey] = []
    if journeys_override is not None:
        lane_journeys = list(journeys_override)
    elif catalogue_text is not None:
        catalogue = parse_catalogue(catalogue_text)
        catalogue_warning = catalogue.warning
        if catalogue.journeys:
            lane_journeys = journeys_for_lane(catalogue.journeys, lane.driver_kind)
            if not lane_journeys:
                no_lane_note = (
                    f"{CATALOGUE_PATH} has no journey declaring lane "
                    f"{lane.driver_kind!r} — falling back to the generic checklist"
                )
                catalogue_warning = (
                    f"{catalogue_warning}; {no_lane_note}" if catalogue_warning
                    else no_lane_note
                )

    usage_line_1, usage_line_2 = _app_drive_usage_lines(lane.driver_kind, lane.launch_command)
    run_spec_usage = app_drive_run_spec_usage(lane.driver_kind, lane.launch_command)
    lines = [
        f"=== coord bugbash: {lane.platform} lane ===",
        "",
        f"Reference backend for comparison: {reference_backend or '(none configured)'}",
        "",
        f"HARD RULE — drive the app ONLY through `coord app-drive` for kind "
        f"`{lane.driver_kind}` (this lane's own sanctioned entry point — see "
        "`coord app-drive --help` for the full verb list: "
        "open/send/wait-idle/screen/probe/close/run-spec). Never use "
        "osascript, System Events, a Terminal/iTerm `do script`, "
        "xdotool/AppleScript/win32 calls run directly, a home-made "
        "input-injection helper, or System Settings. Two-line usage "
        f"example:\n  {usage_line_1}\n  {usage_line_2}\nIf a required "
        "permission (Accessibility, Screen Recording, a locked/absent GUI "
        "session, ...) is missing, STOP IMMEDIATELY and report the lane "
        "unavailable (see below) — do NOT improvise a workaround, and do "
        "NOT send any key or click to recover (#3566).",
        "",
    ]
    if lane.setup:
        lines += [
            f"0. Build/provision this lane ONCE before opening any session "
            f"(run from the repo checkout root — this is the route's own "
            f"`setup:`, not something to infer from repo docs): "
            f"`{lane.setup}`",
            "",
        ]
    lines += [
        f"1. Run this repo's Tier-2 smoke spec for this driver to completion "
        f"(`{run_spec_usage}` runs it end to end in one command).",
    ]
    if catalogue_warning:
        lines += ["", f"NOTE: {catalogue_warning}"]

    if lane_journeys:
        lines += [
            "",
            f"2. Then walk the {len(lane_journeys)} journey(s) below from "
            f"{CATALOGUE_PATH}, in this exact (priority) order, using the "
            "native driver's own probes/captures as evidence:",
        ]
        for j in lane_journeys:
            lines.append(f"   - [{j.id}] (priority {j.priority}, area: {j.area or 'unspecified'})")
            lines.append(f"     steps: {j.steps}")
            lines.append(f"     expected: {j.expected}")
            ref_line = f"     reference ({j.reference})"
            if j.reference_detail:
                ref_line += f": {j.reference_detail}"
            lines.append(ref_line)
            if j.reference in ORACLE_BACKED_REFERENCES:
                lines.append(
                    "     ORACLE: establish the expected outcome by running these "
                    "exact keystrokes through `nvim --headless` on the same buffer "
                    "and comparing buffer text and cursor — do NOT reason from "
                    "memory about what real Neovim does. If `nvim` is not "
                    "available on this host, do NOT guess: report this journey's "
                    'coverage as `"skipped"` with reason `"no nvim"` (#3580).'
                )
            if j.mode == "vscode":
                lines.append(
                    "     MODE: run this journey only after switching the app "
                    "into VS Code mode (Alt-M, or the `editor_mode` setting in "
                    "this lane's isolated settings.json)."
                )
            elif j.mode == "vim":
                lines.append(
                    "     MODE: run this journey in the app's default Vim mode."
                )
    else:
        lines += [
            "",
            "2. Then walk the exploration checklist below on the real app, using "
            "the native driver's own probes/captures as evidence:",
        ]
        for item in checklist:
            lines.append(f"   - {item}")

    lines += [
        "",
        "For anything that behaves differently from the reference backend "
        "(checklist items) or from a journey's own declared `expected` "
        "outcome, or crashes, hangs, or renders wrong, report it as a "
        "finding.",
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
        "capture paths/descriptions, may be empty), journey_id (the id of "
        "the catalogue journey above this finding relates to, if any — "
        "omit/blank if there was no catalogue or this finding doesn't come "
        "from one of its journeys). An empty array means zero findings "
        "this round.",
        "",
        f"ALSO end your final message with a fenced ```{COVERAGE_FENCE}``` "
        "block: a JSON array covering EVERY item you walked above (every "
        "journey, or every checklist item), each as "
        '{"journey_id": <the id above, or the checklist item\'s own text>, '
        '"status": "passed" | "found" | "skipped", "reason": <non-empty '
        'when status is "skipped", e.g. "no nvim">}. Use "found" for an '
        "item you filed a finding for above, \"passed\" for one that "
        "behaved as expected, \"skipped\" for one you could not actually "
        "run (missing oracle, unreachable mode, etc. — never guess at "
        "what would have happened).",
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
    #: #3580 requirement 5: this lane's per-journey coverage report
    #: (:func:`parse_coverage_block`'s output) — informational, never part
    #: of the ``ok``/``unavailable``/``protocol_error`` trust decision.
    #: Empty when the worker's message carried no (or an unparseable)
    #: ```` ```bugbash-coverage ```` block.
    journey_outcomes: tuple["JourneyOutcome", ...] = ()


#: ``(lane, round_num) -> ExploreOutcome`` — "go run this lane's
#: exploration round and hand back what it found." The production
#: implementation lives in ``coord/commands/bugbash.py`` (dispatch +
#: transcript parsing); tests inject a fake.
Explorer = Callable[[BugbashLane, int], ExploreOutcome]

#: ``(argv) -> stdout`` — "run this `coord` subcommand." Used only for
#: ``coord issue create`` / ``coord drive-queue add`` (:func:`file_finding`).
CoordRunner = Callable[[Sequence[str]], str]


# ── journey sharding: one worker per chunk (#3620) ──────────────────────────

#: ``(lane, round_num, journeys) -> ExploreOutcome`` — "go run ONE chunk of
#: a lane's sharded exploration and hand back what it found." *journeys* is
#: the exact (possibly empty — meaning "fall back to the checklist")
#: ordered slice this chunk owns, as computed by
#: :meth:`JourneyScheduler.chunks_for_round`. The production implementation
#: (:mod:`coord.commands.bugbash`) wraps :func:`coord.commands.bugbash
#: ._dispatch_and_await_lane` with ``journeys_override=journeys``; tests
#: inject a fake.
ChunkExplorer = Callable[[BugbashLane, int, "tuple[Journey, ...]"], ExploreOutcome]


@dataclass
class _ShardCostState:
    """Run-wide (never reset per round) cost accumulator a sharded lane
    dispatch consults BEFORE starting each new chunk (#3620 requirement 4:
    "no new chunk dispatched after cap trips"). ONE instance is shared
    across every lane's sharded dispatch for the whole ``coord bugbash
    run`` invocation — mirrors :class:`_RoundExploreState`'s own
    lock-guarded accumulator, but at CHUNK granularity: a single lane-round
    can now spend several chunks' worth of cost before
    :func:`run_bugbash`'s own per-round cap check (which only sees the
    AGGREGATE cost :func:`explore_lane_sharded` hands back once the whole
    round's chunks are done) ever gets a look — this is the layer that
    actually stops a NEW chunk from starting mid-round once a cap trips,
    not just between rounds."""

    lane_cost: dict[str, float] = field(default_factory=dict)
    total_cost: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def try_reserve(self, platform: str, cap_per_lane: float, cap_total: float) -> str:
        """``""`` when a new chunk for *platform* may start; otherwise the
        human-readable reason it may not (a cap already tripped) — checked
        under the lock, with the chunk's own dispatch (and its eventual
        :meth:`record` call) left to the caller to run OUTSIDE the lock, the
        same check-then-run-outside-the-lock split
        :func:`_explore_round_lanes` already uses for lane-level caps."""
        with self.lock:
            spent = self.lane_cost.get(platform, 0.0)
            if spent >= cap_per_lane:
                return f"cumulative cost {spent:.2f} already >= per-lane cap {cap_per_lane:.2f}"
            if self.total_cost >= cap_total:
                return f"total cost {self.total_cost:.2f} already >= total cap {cap_total:.2f}"
            return ""

    def record(self, platform: str, cost: float) -> None:
        with self.lock:
            self.lane_cost[platform] = self.lane_cost.get(platform, 0.0) + cost
            self.total_cost += cost


def explore_lane_sharded(
    lane: BugbashLane,
    round_num: int,
    *,
    chunk_explorer: ChunkExplorer,
    scheduler: JourneyScheduler,
    cost_state: _ShardCostState,
    cost_cap_per_lane: float,
    cost_cap_total: float,
    max_concurrent_chunks: int = 1,
) -> ExploreOutcome:
    """The sharded :data:`Explorer` (#3620): walks *lane*'s next round of
    journeys (via *scheduler*) split into chunks of at most
    ``scheduler.journeys_per_worker``, dispatching one *chunk_explorer*
    call per chunk — serially when ``max_concurrent_chunks <= 1`` (every
    GUI lane: one desktop, one focus — see
    :func:`max_concurrent_chunks_for_lane`), or up to
    ``max_concurrent_chunks`` concurrently (a ``tui-pty`` lane, bounded by
    its host's own ``max_workers`` — each worker gets its own pty, nothing
    to contend over). Returns ONE aggregate :class:`ExploreOutcome` —
    :func:`run_bugbash`'s own round loop is completely unaware chunking
    happened at all, so its cap/termination logic (#2096, #3510, #3517)
    needs no changes: this is a drop-in :data:`Explorer` exactly like the
    unsharded production one.

    Stops starting new chunks the moment *cost_state* reports either cap
    already tripped (#3620 requirement 4, checked per chunk, not just once
    per round) — a chunk already in flight always finishes and has its
    cost recorded, but no further chunk for ANY lane sharing *cost_state*
    starts once a cap trips.

    The first chunk reporting ``unavailable=True`` stops every remaining
    chunk this round from starting (the lane's own session/permission
    problem applies identically to every chunk, so there is nothing to
    gain by burning further chunks against it) — the aggregate outcome is
    ``unavailable=True`` with that chunk's notes.

    When *scheduler* has nothing to shard this round (no catalogue
    journeys for this lane at all, or every journey already produced a
    finding), this dispatches exactly ONE chunk with an empty journeys
    tuple — the production chunk explorer asks
    :func:`build_exploration_briefing` to fall back to the checklist
    exactly like it always has, so a repo with no catalogue sees no
    behaviour change from this feature existing.
    """
    chunks = scheduler.chunks_for_round()
    if not chunks:
        chunks = [()]

    outcomes: list[ExploreOutcome | None] = [None] * len(chunks)
    lock = threading.Lock()
    stopped = {"flag": False}

    def attempt(index: int) -> None:
        with lock:
            if stopped["flag"]:
                return
            reason = cost_state.try_reserve(lane.platform, cost_cap_per_lane, cost_cap_total)
            if reason:
                return
        outcome = chunk_explorer(lane, round_num, chunks[index])
        cost_state.record(lane.platform, outcome.cost)
        outcomes[index] = outcome
        if outcome.unavailable:
            with lock:
                stopped["flag"] = True

    if max_concurrent_chunks <= 1:
        for i in range(len(chunks)):
            attempt(i)
    else:
        with ThreadPoolExecutor(max_workers=max_concurrent_chunks) as pool:
            futures = [pool.submit(attempt, i) for i in range(len(chunks))]
            for f in futures:
                f.result()  # re-raise any exception from a chunk's thread

    findings: list[Finding] = []
    journey_outcomes: list[JourneyOutcome] = []
    total_cost = 0.0
    ok = True
    unavailable = False
    unavailable_notes = ""
    protocol_errors: list[str] = []
    notes_parts: list[str] = []
    skipped_count = 0

    for outcome in outcomes:
        if outcome is None:
            skipped_count += 1
            continue
        total_cost += outcome.cost
        if outcome.unavailable:
            unavailable = True
            unavailable_notes = unavailable_notes or outcome.notes
            continue
        findings.extend(outcome.findings)
        journey_outcomes.extend(outcome.journey_outcomes)
        scheduler.record(outcome.journey_outcomes)
        if not outcome.ok:
            ok = False
            notes_parts.append(outcome.notes or "chunk explorer reported failure")
        elif outcome.protocol_error:
            protocol_errors.append(outcome.protocol_error)

    if skipped_count:
        notes_parts.append(
            f"{skipped_count}/{len(chunks)} chunk(s) not dispatched this round "
            "(cost cap already tripped, or a sibling chunk reported the lane unavailable)"
        )

    if unavailable:
        return ExploreOutcome(unavailable=True, cost=total_cost, notes=unavailable_notes)

    return ExploreOutcome(
        findings=tuple(findings),
        cost=total_cost,
        ok=ok,
        protocol_error="; ".join(protocol_errors),
        journey_outcomes=tuple(journey_outcomes),
        notes="; ".join(notes_parts) or "status=completed",
    )


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
    if finding.journey_id:
        # #3580 requirement 2: name the catalogue journey this finding
        # relates to, so a fixer can go find its `reference`/`reference_detail`
        # in tests/smoke-spec/catalogue.yaml rather than guessing where the
        # expected behaviour came from.
        parts.append(f"Journey: {finding.journey_id} (see {CATALOGUE_PATH})")
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
    #: ``{platform: CoverageSummary}`` for every lane explored this round
    #: whose :attr:`ExploreOutcome.journey_outcomes` was non-empty (#3580
    #: requirement 5) — "N journeys passed" instead of inferring coverage
    #: from the findings list alone. Absent for a lane whose worker didn't
    #: report a (parseable) coverage block at all — never defaulted to a
    #: zeroed :class:`CoverageSummary`, which would misrepresent "no
    #: coverage report" as "zero journeys attempted."
    lane_coverage: dict[str, "CoverageSummary"] = field(default_factory=dict)

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


def _apply_outcome_to_round(report: RoundReport, lane: BugbashLane, outcome: ExploreOutcome) -> None:
    """Classify one lane's :class:`ExploreOutcome` into *report*'s
    findings/unavailable/lane-failure/protocol-error buckets — the SAME
    bucketing both :func:`run_bugbash`'s inline round loop and
    :func:`harvest_outcome` (#3569) use, so a lane explorer that finishes
    AFTER its ``--lane-timeout`` already elapsed and is picked up through
    ``coord bugbash harvest`` is judged by identically the same rules as
    one observed inline within its deadline — never a second, independently
    -drifting copy of this decision (#2096 "one question, one answer").

    Does NOT touch :attr:`RoundReport.lane_cost` — that's cumulative-across-
    rounds bookkeeping only :func:`run_bugbash`'s own loop needs (a harvest
    has no prior rounds to accumulate against), so callers set it
    themselves.
    """
    if outcome.unavailable:
        # #3510: a locked/absent GUI session (or missing display) is a host
        # condition, not a finding — recorded separately from both a clean
        # pass and a dispatch/poll failure, and NEVER contributes findings
        # this round, even defensively if the explorer happened to also
        # hand some back.
        report.unavailable_lanes[lane.platform] = (
            outcome.notes or "lane unavailable — no usable GUI session/display"
        )
        return
    report.findings.extend(outcome.findings)
    if not outcome.ok:
        # #2096: this lane's "findings" (almost certainly empty) are NOT a
        # verified observation — record why, so a round whose every lane
        # failed this way can never render identically to a round that
        # actually looked and found nothing.
        report.lane_failures[lane.platform] = outcome.notes or "explorer reported failure"
    elif outcome.protocol_error:
        # #3517: the lane DID complete, but its final message could not be
        # trusted as a findings report at all — a malformed/missing block,
        # never silently read as "zero findings observed" (that conflation
        # is exactly what let a real finding disappear and pass the #3488
        # release gate).
        report.protocol_error_lanes[lane.platform] = outcome.protocol_error
    if outcome.journey_outcomes:
        # #3580 requirement 5: purely informational — never gates the
        # round's termination reason, just the coverage summary.
        report.lane_coverage[lane.platform] = CoverageSummary.from_outcomes(
            outcome.journey_outcomes
        )


def _dedupe_and_file_round(
    findings: Sequence[Finding],
    *,
    repo: str,
    dry_run: bool,
    require_confirm: bool,
    confirm: Callable[[int, list[Finding]], bool] | None,
    round_num: int,
    runner: CoordRunner,
    open_issues: list[dict],
    closed_issues: list[dict],
    lanes_by_platform: dict[str, BugbashLane],
    run_filed_issues: list[dict],
    run_filed_by_title: dict[str, int],
) -> tuple[list[FilingResult], int, bool]:
    """Dedupe *findings* against *open_issues*/*closed_issues* (the caller
    has already merged in this run's own already-filed issues, #3546) and
    file/queue every non-duplicate.

    This is the SAME dedupe/confirm/file/queue path both :func:`run_bugbash`
    's round loop and :func:`harvest_outcome` (#3569) drive — a finding
    recovered through ``coord bugbash harvest`` after its lane's own
    ``--lane-timeout`` elapsed goes through identically the same rules
    (platform-gated title dedupe, the operator-confirm gate, the mandatory
    acceptance line, ``suspected_repo`` routing) as one filed inline, never
    a second, looser copy of this decision (#2096 "one question, one
    answer").

    Returns ``(filings, new_count, declined)``: *new_count* is every
    finding whose verdict was NOT :data:`DedupeVerdict.DUPLICATE` (what
    moves round-cap/zero-findings termination upstream); *declined* is
    whether an operator declined to file this round's candidates via
    *confirm* (always ``False`` when *require_confirm* is ``False`` — e.g.
    a harvest's standalone recovery never re-gates behind a second
    confirmation prompt).

    Mutates *run_filed_issues*/*run_filed_by_title* in place exactly as the
    original inline loop did, so a caller running multiple rounds
    (:func:`run_bugbash`) keeps seeing this run's own earlier filings
    across calls.
    """
    dedupes = _dedupe_round_findings(findings, open_issues, closed_issues)

    candidates = [
        f for f, d in zip(findings, dedupes) if d.verdict != DedupeVerdict.DUPLICATE
    ]
    declined = require_confirm and bool(candidates) and not (confirm and confirm(round_num, candidates))

    filings: list[FilingResult] = []
    new_count = 0
    for finding, dedupe in zip(findings, dedupes):
        lane = lanes_by_platform.get(finding.platform)
        if dedupe.verdict != DedupeVerdict.DUPLICATE:
            new_count += 1
        if dedupe.verdict != DedupeVerdict.DUPLICATE and declined:
            # Operator declined this round's filings — record the would-be
            # preview (same shape a dry run produces) without ever invoking
            # the runner.
            title = compose_finding_issue_title(finding)
            body = format_bug_report(
                expected=finding.expected, actual=finding.actual,
                repro=finding.repro,
                evidence=_evidence_with_acceptance(finding, dedupe),
            )
            filings.append(
                FilingResult(
                    finding=finding, verdict=dedupe.verdict,
                    filed=False, queued=False,
                    preview_title=title, preview_body=body,
                )
            )
            continue
        if dedupe.verdict is DedupeVerdict.DUPLICATE and dedupe.matched_number is None:
            # #3546: a within-round duplicate from `_dedupe_round_findings`
            # — its sibling finding may have already been filed earlier in
            # THIS loop (in which case its real issue number is now in
            # `run_filed_by_title`). Resolve it so the report/CLI shows the
            # real number instead of a permanent "duplicate of #None".
            resolved = run_filed_by_title.get(dedupe.matched_title or "")
            if resolved is not None:
                dedupe = DedupeResult(
                    verdict=dedupe.verdict, matched_number=resolved,
                    matched_title=dedupe.matched_title, score=dedupe.score,
                )
        # An unresolvable lane (finding.platform not in lanes_by_platform)
        # is only a problem when it would actually be queued — file_finding
        # raises in that case, never silently drops the machine target
        # (#2096: a gate must be able to fail).
        result = file_finding(finding, dedupe, lane, runner, dry_run=dry_run)
        filings.append(result)
        if result.filed and result.issue_number is not None:
            # Only track filings that landed in *repo*'s own namespace — a
            # finding routed elsewhere via `finding_target_repo` (#3546
            # requirement 2) dedupes against THAT repo's issues, not this
            # one's.
            if finding_target_repo(finding) == repo:
                filed_title = compose_finding_issue_title(finding)
                run_filed_issues.append({"number": result.issue_number, "title": filed_title})
                run_filed_by_title[filed_title] = result.issue_number

    return filings, new_count, declined


def harvest_outcome(
    outcome: ExploreOutcome,
    lane: BugbashLane,
    *,
    repo: str,
    runner: CoordRunner,
    open_issues_fetcher: Callable[[str], list[dict]],
    closed_issues_fetcher: Callable[[str], list[dict]],
    dry_run: bool = False,
) -> RoundReport:
    """File (or preview) the findings in a single lane's late-arriving
    :class:`ExploreOutcome` — the #3569 recovery path for an explorer that
    finished AFTER its lane's ``--lane-timeout`` had already elapsed and
    the controller had stopped waiting on it (``coord bugbash harvest``,
    wired in :mod:`coord.commands.bugbash`).

    Reuses the identical bucketing (:func:`_apply_outcome_to_round`) and
    dedupe/file path (:func:`_dedupe_and_file_round`) :func:`run_bugbash`'s
    own round loop uses — a harvested result is subject to the exact same
    rules (unavailable/protocol-error/incomplete handling, platform-gated
    dedupe, the mandatory acceptance line) as one observed inline, never a
    parallel, looser path (#2096 "one question, one answer").

    Always treated as a standalone round 1 with no prior in-run filings to
    cross-reference — a harvest recovers ONE lane's result after the fact,
    it is not itself a multi-round run — and never gated behind an operator
    confirmation: :func:`run_bugbash`'s ``confirm_rounds`` gate exists to
    let an operator preview the FIRST rounds of a live, automatically-
    filing run before it starts; a harvest is already a single, deliberate,
    after-the-fact operator action (``coord bugbash harvest``), so gating
    it behind a second prompt would just be an extra step for no added
    safety. ``dry_run=True`` still skips filing/queuing exactly like a live
    run's ``--dry-run`` does — the runner is never invoked on that path.
    """
    report = RoundReport(round_num=1)
    report.lane_cost[lane.platform] = outcome.cost
    _apply_outcome_to_round(report, lane, outcome)

    open_issues = list(open_issues_fetcher(repo))
    closed_issues = closed_issues_fetcher(repo)
    filings, new_count, _declined = _dedupe_and_file_round(
        report.findings,
        repo=repo,
        dry_run=dry_run,
        require_confirm=False,
        confirm=None,
        round_num=1,
        runner=runner,
        open_issues=open_issues,
        closed_issues=closed_issues,
        lanes_by_platform={lane.platform: lane},
        run_filed_issues=[],
        run_filed_by_title={},
    )
    report.filings = filings
    report.new_count = new_count
    return report


def _group_lanes_by_host(lanes: Sequence[BugbashLane]) -> dict[str, list[BugbashLane]]:
    """Groups *lanes* by :attr:`BugbashLane.machine`, preserving each
    lane's original relative order within its own host's list — the exact
    order :func:`_explore_round_lanes` explores that host's lanes in,
    unchanged from the old strictly-sequential loop. Different hosts' lists
    are explored concurrently (#3602); lanes within the SAME list never
    are — dict iteration order is insertion order (first lane seen for a
    new ``machine``), so this is also deterministic given *lanes*' order."""
    groups: dict[str, list[BugbashLane]] = {}
    for lane in lanes:
        groups.setdefault(lane.machine, []).append(lane)
    return groups


@dataclass
class _RoundExploreState:
    """Mutable cross-host bookkeeping for one round's
    :func:`_explore_round_lanes` call — bundles what used to be three
    separate parameters (``lane_cost``, a ``total_cost`` mutable cell, and
    the lock guarding both) behind one name (#3602 review round 1 nit).
    ``lane_cost`` is keyed by :attr:`BugbashLane.platform` — pre-existing
    from before #3602, and still true under concurrency: two lanes sharing
    a platform on DIFFERENT hosts (the real fleet's two ``win-native``
    lanes) share one bucket and one per-lane cap. See
    :func:`_explore_round_lanes`'s docstring for exactly what that costs
    under concurrency that it didn't cost sequentially."""

    lane_cost: dict[str, float]
    total_cost: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)


def _explore_round_lanes(
    lanes: Sequence[BugbashLane],
    lanes_by_host: dict[str, list[BugbashLane]],
    round_num: int,
    explorer: Explorer,
    report: RoundReport,
    state: _RoundExploreState,
    cost_cap_per_lane: float,
    cost_cap_total: float,
) -> None:
    """Explores every lane configured for this round, one worker thread per
    HOST (#3602) — a host's own lanes run strictly in the order given (the
    thread blocks on each lane's :data:`Explorer` call before starting the
    next lane on that SAME host, since two GUI lanes on one desktop would
    fight over focus and a host like ``macmini`` has ``max_workers: 1``
    anyway), but different hosts' threads run concurrently — so a round's
    wall-clock cost is the slowest HOST's own lane chain, never the sum
    over every lane.

    Both cost caps are read and updated only under *state*'s lock, with the
    :data:`Explorer` call itself made OUTSIDE the lock (a real lane can take
    many minutes) — so the "check cap, then mark started" step is atomic
    across hosts. The only overshoot the TOTAL cap can ever see is from
    lane(s) ALREADY in flight (their :data:`Explorer` call already started)
    at the moment a sibling's completion pushes the running total to/over
    the cap — a lane that hasn't started yet always sees the tripped cap
    under the same lock its sibling just wrote through, and is skipped
    (recorded in ``skip_reasons``, same as a per-lane cap skip) rather than
    started.

    The PER-LANE cap is weaker under concurrency than it was sequentially
    for two lanes sharing a platform on different hosts (``lane_cost`` is
    keyed by platform, see :class:`_RoundExploreState`): sequentially, the
    second such lane always saw the first one's already-applied cost.
    Concurrently, both can read the same pre-update value under the lock
    before either has run its :data:`Explorer` call, so both start — the
    per-lane cap can be overshot by a whole sibling lane's cost on top of
    the triggering lane's own. Not fixed here (it would mean keying every
    per-platform ``RoundReport`` bucket — ``lane_cost``, ``lane_failures``,
    ``unavailable_lanes``, ``protocol_error_lanes``, ``lane_coverage`` — by
    lane identity instead, a much larger, pre-existing-schema change);
    operators relying on a tight per-lane budget for a platform run on
    multiple hosts should account for this extra headroom.

    *lanes* is *lanes_by_host*'s own input, flattened back into its
    ORIGINAL configured order — the order every outcome is replayed onto
    *report* in once every host's thread has joined, below. A host thread
    only ever decides WHETHER a lane ran (the cap check has to happen live,
    interleaved with every other host, under the lock); it never writes an
    outcome straight onto the shared *report* itself, because thread-
    completion order is not deterministic and both
    :func:`_dedupe_round_findings` (which of two lanes' duplicate findings
    this round wins and gets filed, into which repo) and an operator
    reading ``skipped_lanes``/``skip_reasons`` depend on seeing *report* in
    *lanes*' own configured order — unchanged from before #3602 (#3602
    review round 1).

    A ``KeyboardInterrupt`` raised while this is running still has to wait
    for every lane ALREADY in flight on every host before it can
    propagate — there is no mid-lane cancellation, and with
    ``max_workers == len(lanes_by_host)`` every host's thread starts
    immediately on submission, so there is nothing queued at the pool level
    left to drop either. An operator who needs to abort a bugbash run
    promptly still has to wait out the slowest lane's own timeout, same as
    before this changed lanes to run one thread per host instead of one
    thread total.
    """
    # Buffered per-lane results, replayed onto *report* in *lanes*' own
    # order after the pool joins (see docstring above). Keyed by `id(lane)`
    # rather than `lane.platform` — two lanes CAN share a platform on
    # different hosts (the real fleet's two `win-native` lanes), and each
    # one's own outcome must still reach `report` even though they'd
    # collide on a platform-keyed dict.
    results: dict[int, tuple[str, str] | tuple[str, ExploreOutcome]] = {}

    def run_host(host_lanes: list[BugbashLane]) -> None:
        for lane in host_lanes:
            with state.lock:
                if state.lane_cost[lane.platform] >= cost_cap_per_lane:
                    results[id(lane)] = (
                        "skip",
                        f"cumulative cost {state.lane_cost[lane.platform]:.2f} already "
                        f">= per-lane cap {cost_cap_per_lane:.2f}",
                    )
                    continue
                if state.total_cost >= cost_cap_total:
                    results[id(lane)] = (
                        "skip",
                        f"total cost {state.total_cost:.2f} already >= total cap "
                        f"{cost_cap_total:.2f} (cap already tripped by another lane "
                        "this round)",
                    )
                    continue
            outcome = explorer(lane, round_num)
            with state.lock:
                state.lane_cost[lane.platform] += outcome.cost
                state.total_cost += outcome.cost
                results[id(lane)] = ("explored", outcome)

    if not lanes_by_host:
        return
    with ThreadPoolExecutor(max_workers=len(lanes_by_host)) as pool:
        futures = [pool.submit(run_host, host_lanes) for host_lanes in lanes_by_host.values()]
        for future in futures:
            future.result()  # re-raise any exception from a host's thread

    for lane in lanes:
        result = results.get(id(lane))
        if result is None:
            # A host thread raised before reaching this lane — already
            # re-raised by `future.result()` above, so this round never
            # gets this far for that lane. Defensive only.
            continue
        kind, payload = result
        if kind == "skip":
            report.skipped_lanes.append(lane.platform)
            report.skip_reasons[lane.platform] = payload  # type: ignore[assignment]
        else:
            report.lane_cost[lane.platform] = state.lane_cost[lane.platform]
            # #3569: the SAME bucketing `coord bugbash harvest`'s
            # `harvest_outcome` uses for a late-arriving explorer — one
            # question ("how does this ExploreOutcome classify"), one
            # answer, whether it's observed inline here or recovered
            # after the fact.
            _apply_outcome_to_round(report, lane, payload)  # type: ignore[arg-type]


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

    Each round: every lane is explored CONCURRENTLY ACROSS HOSTS (#3602,
    see :func:`_explore_round_lanes`) — lanes sharing a host (e.g. two
    routes both landing on ``macmini``) are still run strictly one after
    another, but lanes on different hosts overlap, so a round's wall-clock
    time is the slowest HOST's own lane chain, not the sum over every lane.
    A lane is skipped (never silently dropped — recorded in
    ``skipped_lanes``/``skip_reasons``) when it has already exceeded
    ``cost_cap_per_lane``, OR when ``cost_cap_total`` was already tripped by
    a sibling lane earlier in THIS round (mid-round, not just at the round
    boundary — see :func:`_explore_round_lanes`'s docstring for exactly how
    much overshoot that still allows). Once every host's lanes for the
    round have reported, findings
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
    lanes_by_host = _group_lanes_by_host(config.lanes)
    state = _RoundExploreState(lane_cost=lane_cost)
    rounds: list[RoundReport] = []
    reason = "round_cap"
    # Bound even when `config.max_rounds <= 0` skips the loop below entirely
    # (reachable from the CLI: `--max-rounds` has no lower bound) — the
    # round loop's own `total_cost = state.total_cost` re-binds this every
    # iteration, but the final `return` needs a value regardless of
    # whether any round ever ran (#3602 review round 1: this used to be an
    # `UnboundLocalError` for `--max-rounds 0`).
    total_cost = 0.0
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

        _explore_round_lanes(
            config.lanes,
            lanes_by_host,
            round_num,
            explorer,
            report,
            state,
            config.cost_cap_per_lane,
            config.cost_cap_total,
        )
        total_cost = state.total_cost

        # #3546: merge the fresh fetch with every issue THIS RUN has already
        # filed — a finding matching one of this run's own earlier filings
        # must be recognised by number even if the fresh fetch hasn't (yet)
        # caught up with this process's own recent write.
        open_issues = list(open_issues_fetcher(config.repo)) + run_filed_issues
        closed_issues = closed_issues_fetcher(config.repo)

        require_confirm = (not config.dry_run) and round_num <= config.confirm_rounds
        filings, new_count, declined = _dedupe_and_file_round(
            report.findings,
            repo=config.repo,
            dry_run=config.dry_run,
            require_confirm=require_confirm,
            confirm=confirm,
            round_num=round_num,
            runner=runner,
            open_issues=open_issues,
            closed_issues=closed_issues,
            lanes_by_platform=lanes_by_platform,
            run_filed_issues=run_filed_issues,
            run_filed_by_title=run_filed_by_title,
        )
        report.filings = filings
        report.new_count = new_count
        report.declined = declined

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
