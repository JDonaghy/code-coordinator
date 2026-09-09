"""Core ``coord acceptance`` orchestration (#944, docs/ORACLE_LOOP.md).

Pure/testable logic shared by the ``coord acceptance run`` / ``record`` CLI
commands in ``coord/commands/acceptance.py``: manifest loading (test-id ->
issue slice mapping) and building the structured verdict payload from a
driver's parsed test results.  Kept separate from the CLI so it can be unit
tested without Click's invocation machinery, mirroring the
``test_orchestrator.py`` / ``commands/test_gate.py`` split.

Layout this module expects (docs/ORACLE_LOOP.md "Layout"):

    tests/acceptance/ms-NN/
        contract.md          # black-box surface (not read by this module)
        mocks/                # viewable mocks == assertion fixtures
        <suite files>         # SEALED to the worker
        manifest.(yml|json)   # test-id -> issue-slice mapping
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Sequence

import yaml

from coord.config import Config, entrypoint_sibling_acceptance_dir
from coord.models import Machine, Repo

ACCEPTANCE_DIRNAME = "tests/acceptance"


def acceptance_root_for_driver(base: Path, entrypoint: str) -> Path:
    """Where a resolved driver's manifests/contracts actually live under
    *base* (a repo checkout root) — #2896.

    A directory-discovered driver (``cli-pytest``, no ``entrypoint:``, e.g.
    this repo's own ``ms-37``) still uses the shared repo-root
    :data:`ACCEPTANCE_DIRNAME`. An entrypoint-linked driver
    (``tui-tuidriver``) wires its JIT-authored slices in from the
    entrypoint's own sibling ``acceptance/`` directory instead — relocated
    there (from the shared root) so the crate ``include!``s across nothing
    but its own tree; see :func:`coord.config.entrypoint_sibling_acceptance_dir`,
    the same derivation :meth:`coord.config.AcceptanceConfig.sealed_paths`
    uses to seal it.

    Callers that already resolved a ``driver_cfg`` (``coord acceptance run``
    / ``record``, both routed through the same ``--for-path``-selected
    driver) pass its ``.entrypoint`` here instead of hardcoding
    ``base / ACCEPTANCE_DIRNAME`` — the bug the hardcoded form had: a routed
    repo's OTHER driver's slices (relocated out of the shared root) would
    silently resolve to an empty/wrong directory and report "no acceptance
    slice" for an issue that has one, just not there any more.
    """
    if not entrypoint:
        return base / ACCEPTANCE_DIRNAME
    return base / entrypoint_sibling_acceptance_dir(entrypoint)


def search_roots_for_repo(config: Config | None, repo_name: str | None) -> list[str]:
    """Every repo-relative, trailing-slash directory *repo_name*'s
    milestone-scoped acceptance slices could live under (#2896) — a thin,
    fail-open wrapper over
    :meth:`coord.config.AcceptanceConfig.acceptance_search_roots`.

    Single source of truth for the "…or fall back to the legacy repo-root
    tree" rule that every multi-root caller needs, so the fallback is
    spelled once instead of re-derived at each call site: returns
    ``[ACCEPTANCE_DIRNAME + "/"]`` when there is no *config*/*repo_name* in
    hand (an API-only caller invoked without one, e.g. an older
    ``coord.merge_queue`` entry point) or when the repo declares no
    acceptance driver at all. Callers therefore always get at least one
    candidate root and never have to special-case an empty list.
    """
    if config is None or not repo_name:
        return [ACCEPTANCE_DIRNAME + "/"]
    return config.acceptance.acceptance_search_roots(repo_name) or [
        ACCEPTANCE_DIRNAME + "/"
    ]


def ms_dirname(milestone_number: int) -> str:
    """The ``ms-NN`` directory name for *milestone_number* (docs/ORACLE_LOOP.md
    "Layout"). Single source of truth for the naming convention so Gate A
    (#930, ``coord acceptance mock``) and the manifest reader agree."""
    return f"ms-{milestone_number}"


def gate_a_contract_path(
    milestone_number: int, acceptance_dir: str = ACCEPTANCE_DIRNAME
) -> str:
    """Repo-relative path to *milestone_number*'s Gate A contract
    (docs/ORACLE_LOOP.md "Layout": ``tests/acceptance/ms-NN/contract.md``).

    Used both by ``coord acceptance mock`` (#930, what it writes) and
    ``coord.milestone_dispatch.gate_a_status`` (what it checks for before
    letting the milestone's issues dispatch).

    *acceptance_dir* (#2896) is the acceptance-tree root to build the path
    under — defaults to the shared repo-root :data:`ACCEPTANCE_DIRNAME`
    (unchanged behaviour for a directory-discovered driver, e.g.
    ``cli-pytest``'s ms-37) but a caller that already knows a specific
    entrypoint-linked driver's own sibling ``acceptance/`` dir (e.g.
    ``tui/tests/acceptance``, via
    :func:`coord.config.entrypoint_sibling_acceptance_dir`) passes that
    instead. A trailing slash, if present, is stripped. Most callers that
    don't know which root governs a given milestone ahead of time should go
    through :func:`gate_a_contract_candidates` instead of guessing one.
    """
    dirname = acceptance_dir.rstrip("/") if acceptance_dir else ACCEPTANCE_DIRNAME
    return f"{dirname}/{ms_dirname(milestone_number)}/contract.md"


def gate_a_contract_candidates(
    config: Config, repo_name: str, milestone_number: int
) -> list[str]:
    """Every repo-relative path *milestone_number*'s Gate-A contract could
    live at (#2896) — one candidate per
    :meth:`coord.config.AcceptanceConfig.acceptance_search_roots` this repo
    declares: the shared repo-root tree first (where a directory-discovered
    driver's slices, e.g. ms-37's ``cli-pytest`` suite, still live), then
    each entrypoint-linked driver's own sibling dir (where a relocated
    slice, e.g. ms-65's ``tui-tuidriver`` suite, now lives instead).

    A milestone's contract lives under exactly one candidate; callers try
    each in turn since which one governs a bare milestone number isn't
    knowable ahead of a single path in hand — the same problem
    :meth:`coord.config.AcceptanceConfig.driver_for` punts on for a routed
    repo with no *path* (see its docstring). Falls back to the single
    legacy repo-root candidate when the repo has no acceptance driver
    configured (or no entrypoint-linked one) at all, so a caller gets a
    sane single candidate rather than an empty list either way.
    """
    roots = search_roots_for_repo(config, repo_name)
    return [gate_a_contract_path(milestone_number, root) for root in roots]


def _mocks_dir(milestone_number: int, acceptance_dir: str = ACCEPTANCE_DIRNAME) -> str:
    dirname = acceptance_dir.rstrip("/") if acceptance_dir else ACCEPTANCE_DIRNAME
    return f"{dirname}/{ms_dirname(milestone_number)}/mocks"


def issue_dirname(issue_number: int) -> str:
    """The ``issue-NN`` directory name for a single-issue bug-lane contract
    (docs/TEST_FIRST_BUG_LANE.md "The intake contract", #1964) — the bug
    lane's counterpart to :func:`ms_dirname`, with no milestone in the name
    because a bug has none.

    This is purely a naming convention, not new plumbing: the manifest
    scanner below (:func:`_manifest_paths`) globs ``*/manifest.*`` under
    ``tests/acceptance/`` regardless of what the directory is called, so an
    ``issue-NN/`` slice is discovered, run (``coord acceptance run --issue
    N``), recorded (``coord acceptance record``), and injected into the
    worker's briefing (:func:`oracle_loop_contract_block`) by the exact same
    code path as an ``ms-NN/`` one — see ``TestOracleLoopContractBlock`` in
    ``tests/test_acceptance.py``, which already proves the block is built
    from whatever the owning directory happens to be named. Pinning the name
    here just keeps every bug-lane contract in the same shape.
    """
    return f"issue-{issue_number}"


def bug_contract_path(issue_number: int) -> str:
    """Repo-relative path to *issue_number*'s single-issue bug-lane contract
    (docs/TEST_FIRST_BUG_LANE.md "The intake contract").

    Unlike :func:`gate_a_contract_path`, nothing gates dispatch on this
    existing — a bug issue has no milestone, so there is no Gate A to block
    on it. It is hand-authored (or agent-assisted) directly from the four
    intake fields (:mod:`coord.bug_intake`); once it — and a
    ``manifest.yml`` alongside it — exist, the issue behaves exactly like an
    authored ``ms-NN`` slice to every downstream command.
    """
    return f"{ACCEPTANCE_DIRNAME}/{issue_dirname(issue_number)}/contract.md"


# Mock-fixture file extension -> the driver ``kind`` it implies (the SAME
# rule each ``AcceptanceDriverConfig.mock`` glob already encodes in
# coordinator.yml / docs/ORACLE_LOOP.md: ``"*.screen"`` for ``tui-tuidriver``,
# ``"*.out"`` for ``cli-pytest``, ``"*.html"`` for ``web-playwright`` (#1542
# — hand-authored, self-contained wireframes; see
# ``coord.agent.MOCK_AUTHOR_SYSTEM_PROMPT`` for the authoring rules and
# ``tests/acceptance/ms-example/mocks/`` for a worked example). Single
# source of truth for the mock-kind -> ``--for-path`` derivation (#1453
# review) — do not re-derive this mapping a second time anywhere else.
MOCK_EXT_TO_DRIVER_KIND: dict[str, str] = {
    ".screen": "tui-tuidriver",
    ".out": "cli-pytest",
    ".html": "web-playwright",
}


class ForPathResolutionError(Exception):
    """A routed repo's ``--for-path`` could not be resolved unambiguously
    from a milestone's Gate-A mocks. Message is operator-facing."""


# (repo_github, dir_path, branch) -> filenames (not full paths) directly
# under that directory, or () when it doesn't exist. Injected so tests never
# hit `gh` — mirrors ``coord.milestone_dispatch.GateAFileExists``.
MockLister = Callable[[str, str, str], "tuple[str, ...]"]


def _default_list_mock_dir(repo_github: str, path: str, branch: str) -> "tuple[str, ...]":
    from coord import github_ops  # noqa: PLC0415

    try:
        return tuple(github_ops.list_repo_dir(repo_github, path, branch=branch))
    except RuntimeError:
        return ()


def resolve_for_path(
    config: Config,
    repo_cfg: Repo,
    milestone_number: int,
    *,
    list_mock_dir: MockLister | None = None,
) -> str | None:
    """Derive the ``--for-path`` glob a ROUTED repo's JIT acceptance-author
    dispatch needs, from *milestone_number*'s Gate-A mock file kind.

    SHARED helper (#1453 review finding 1, tracked for #1460's TUI-menu
    equivalent too — do not duplicate this rule): the mock fixtures a
    milestone's Gate-A contract ships under ``tests/acceptance/ms-NN/mocks/``
    (already merged to the default branch by the time this is ever called —
    :func:`coord.milestone_dispatch.gate_a_status` gates on exactly that)
    have a file extension that implies exactly one driver ``kind``
    (:data:`MOCK_EXT_TO_DRIVER_KIND`). Crossing that against
    ``acceptance.drivers.<repo>.routes[].kind`` picks the one route whose
    ``match`` glob is this milestone's ``--for-path``.

    Returns:
    - ``None`` when *repo_cfg* has no acceptance driver at all, or a FLAT
      (unrouted) one — :meth:`coord.config.AcceptanceConfig.driver_for`
      already resolves those with no path, so no ``--for-path`` is needed.
    - the single matching route's ``match`` glob when resolution is
      unambiguous.

    Raises :class:`ForPathResolutionError` (operator-facing, mirrors
    ``coord.test_author.dispatch_test_author``'s "no route matched" message)
    when the repo IS routed but resolution is ambiguous — no mocks found,
    more than one mock kind present, or zero/more-than-one route declares
    the implied kind. Callers should surface this rather than guess.

    #2896: which directory a routed repo's mocks actually live under is
    exactly the thing this function is trying to determine (an
    entrypoint-linked driver's mocks moved to its own sibling ``acceptance/``
    dir, alongside the rest of its relocated slice) — a bootstrap this
    can't resolve by asking :func:`acceptance_root_for_driver` first, since
    that needs the very driver this function exists to pick. So it searches
    every :meth:`coord.config.AcceptanceConfig.acceptance_search_roots` this
    repo declares (the shared repo-root tree, then each entrypoint's own
    sibling dir) and unions whatever mock files each turns up — in practice
    exactly one root ever has files for a given milestone, so this behaves
    like "find the one that has them" without needing to guess first.
    """
    entry = config.acceptance.drivers.get(repo_cfg.name)
    if entry is None or not entry.routes:
        return None

    lister = list_mock_dir or _default_list_mock_dir
    search_roots = search_roots_for_repo(config, repo_cfg.name)
    mocks_dirs = [_mocks_dir(milestone_number, root) for root in search_roots]
    names: list[str] = []
    for mocks_dir in mocks_dirs:
        names.extend(lister(repo_cfg.github, mocks_dir, repo_cfg.default_branch))

    kinds = {
        MOCK_EXT_TO_DRIVER_KIND[Path(name).suffix]
        for name in names
        if Path(name).suffix in MOCK_EXT_TO_DRIVER_KIND
    }

    def _refuse(reason: str) -> "ForPathResolutionError":
        routes = ", ".join(f"{r.match!r} ({r.kind})" for r in entry.routes)
        return ForPathResolutionError(
            f"repo {repo_cfg.name!r} has a routed acceptance driver ({routes}) "
            f"but --for-path could not be derived from {mocks_dirs!r}'s mock "
            f"kind: {reason}. Pass --no-acceptance to skip JIT authoring, or "
            f"dispatch by hand: coord acceptance author {repo_cfg.name} "
            "<tracking_issue> --issue <N> --for-path <glob>"
        )

    if not kinds:
        raise _refuse(f"no recognized mock files found ({names!r})")
    if len(kinds) > 1:
        raise _refuse(f"mixed mock kinds found ({sorted(kinds)!r})")
    kind = next(iter(kinds))

    matches = [route.match for route in entry.routes if route.kind == kind]
    if len(matches) != 1:
        raise _refuse(
            f"mock kind {kind!r} matches {len(matches)} routes, need exactly 1"
        )
    return matches[0]


class ManifestError(Exception):
    """Raised when a manifest file exists but is malformed."""


# #2543: per-issue manifest fragments. Before this, every JIT test-author
# slice appended its own `issues:`/`expected_red:` block to ONE shared
# `ms-NN/manifest.yml` (`coord.test_author`'s "Later slices MERGE into this
# file" convention) — two slices authored concurrently, or one authored
# against a base that predates a sibling's already-merged slice, collide on
# that single file even though their actual content never overlaps (each
# writes a distinct, differently-keyed block). See coord-portal#122/#128/#132.
#
# The fix: each issue's `issues:`/`expected_red:` entries live in their OWN
# file, `ms-NN/manifest.d/<issue>.(yml|yaml|json)` — two slices writing two
# different files can never textually conflict, no matter how they're
# scheduled. `manifest.yml` itself keeps carrying the MILESTONE-level blocks
# (`gate_a:`, `exempt:`) that are rare, hand-edited, and not part of the
# per-slice JIT-authoring hot path, so it stays a single shared file by
# choice, not oversight.
#
# Backward compatible by construction: every reader below still accepts a
# legacy single-file `ms-NN/manifest.(yml|json)` that ALSO carries
# `issues:`/`tests:`/`expected_red:` (this repo's own already-merged
# `tests/acceptance/ms-33/manifest.yml` and friends) — fragments and a
# legacy file under the same `ms-NN/` dir are simply merged together, same
# "later manifest wins on a bare collision" rule as across milestones. No
# migration script is needed: existing manifests keep working as-is, and new
# per-issue entries land in `manifest.d/` going forward.
MANIFEST_FRAGMENTS_DIRNAME = "manifest.d"


def _manifest_fragment_paths(ms_dir: Path) -> list[Path]:
    """``<ms_dir>/manifest.d/*.(yml|yaml|json)`` fragment files, sorted for
    deterministic scan order. ``[]`` when the fragments dir doesn't exist."""
    frag_dir = ms_dir / MANIFEST_FRAGMENTS_DIRNAME
    if not frag_dir.is_dir():
        return []
    return sorted(
        p for p in frag_dir.iterdir()
        if p.is_file() and p.suffix in (".yml", ".yaml", ".json")
    )


def _manifest_paths(acceptance_root: Path) -> list[Path]:
    """Every manifest SOURCE path under *acceptance_root*, sorted for
    deterministic scan order: per ``ms-NN``/``issue-NN`` directory, the
    legacy single-file ``manifest.(yml|yaml|json)`` (if present) followed by
    each ``manifest.d/<issue>.(yml|yaml|json)`` fragment (#2543). ``[]`` when
    *acceptance_root* doesn't exist.

    Callers that need to know which owning directory a given path came from
    (a fragment's is its grandparent, not its parent) should go through
    :func:`_ms_dir_for_manifest_path` rather than assuming ``path.parent``.
    """
    if not acceptance_root.exists():
        return []
    paths: list[Path] = []
    for ms_dir in sorted(p for p in acceptance_root.iterdir() if p.is_dir()):
        paths.extend(
            sorted(
                p for p in ms_dir.glob("manifest.*")
                if p.suffix in (".yml", ".yaml", ".json")
            )
        )
        paths.extend(_manifest_fragment_paths(ms_dir))
    return paths


def _ms_dir_for_manifest_path(path: Path) -> Path:
    """The owning ``ms-NN``/``issue-NN`` directory for a manifest source
    *path* returned by :func:`_manifest_paths` — itself for a legacy
    single-file manifest, or its grandparent for a
    ``manifest.d/<issue>.(yml|json)`` fragment (#2543)."""
    if path.parent.name == MANIFEST_FRAGMENTS_DIRNAME:
        return path.parent.parent
    return path.parent


@dataclass(frozen=True)
class ManifestData:
    """One manifest file's parsed contents: the test-id -> issue-number
    mapping, plus the #1138 issue-level ``exempt:`` list (issues in an
    oracle-opted-in milestone that are deliberately validated by their own
    unit tests instead of the sealed suite — e.g. the driver-building issue
    itself, #1125 — declared here instead of living only as tribal knowledge
    in an issue body).

    #2063 adds the milestone-level ``gate_a:`` block: the declared,
    reviewable opt-out from the Gate-A human sign-off gate, same posture as
    ``exempt:`` above — a milestone whose surface genuinely needs no human
    eye says so in the repo, in writing, rather than the gate quietly not
    existing for everyone.

    #2164 adds ``expected_red``: ``{issue_number: {test_id, ...}}``, the
    registry of test-ids a sealed slice is *known* to fail before its fix
    exists. A test-id listed there that fails is not a CI failure
    (:func:`apply_expected_red`); one that PASSES is a hard, loud failure —
    the vacuous-assertion case #1965 cares about. Cleared only by ``coord
    acceptance record`` observing green externally
    (:func:`clear_expected_red_entries`), never by a worker edit."""

    tests: dict[str, int] = field(default_factory=dict)
    exempt: frozenset[int] = field(default_factory=frozenset)
    #: ``gate_a: {exempt: true, reason: "..."}`` — this milestone's contract
    #: may be consumed without a recorded human verdict (#2063).
    gate_a_exempt: bool = False
    gate_a_exempt_reason: str = ""
    #: #2164 — see the class docstring's ``expected_red`` paragraph.
    expected_red: "dict[int, frozenset[str]]" = field(default_factory=dict)
    #: #3212 — ``{issue_number: ExemptDependency}`` for every ``exempt:``
    #: entry whose justification names another issue as covering it, either
    #: declared structurally (``{issue: N, covered_by: M, artifact: "..."}``)
    #: or inferred from a plain entry's inline ``# ... #M`` comment. See
    #: :class:`ExemptDependency`'s docstring for why this exists at all: an
    #: exemption that defers coverage to another issue is a promise nobody
    #: was checking.
    exempt_deps: "dict[int, ExemptDependency]" = field(default_factory=dict)


@dataclass(frozen=True)
class ExemptDependency:
    """(#3212) One ``exempt:`` entry's promise that *covered_by* delivers the
    coverage *issue*'s exemption defers to — e.g. format-converter ms-1's

        exempt:
          - 6  # One-page UI — covered by the harness #2 stands up

    Nothing previously checked that #2 actually landed, let alone that it
    produced anything. This makes that promise machine-readable so
    :func:`verify_exempt_dependency` can check it instead of trusting it
    forever.

    *source* is ``"declared"`` for a structured entry
    (``{issue: N, covered_by: M, artifact: "glob"}``) and ``"comment"`` for
    one inferred from a plain integer entry's trailing ``#M`` reference in
    its comment — best-effort, since a comment is prose, not a contract;
    surfaced so a caller can say "this promise was never actually declared,
    only implied" rather than reporting it with the same confidence as an
    explicit one. A comment-derived dependency never carries an *artifact*
    (a free-text comment doesn't name a glob), so it can only be checked
    against "did the named issue land", never "did it produce the thing".
    """

    issue: int
    covered_by: int
    #: Repo-relative glob the covering issue was supposed to produce (e.g.
    #: ``"tests/**/*.spec.ts"``), or ``None`` when the exemption's promise
    #: only names an issue, not an artifact.
    artifact: "str | None" = None
    source: Literal["declared", "comment"] = "declared"


def parse_manifest_text(text: str, *, source: str = "<manifest>") -> ManifestData:
    """Pure parse of one manifest file's YAML/JSON text into
    :class:`ManifestData`. Shared by the local-disk loader
    (:func:`_parse_manifest_file`, used by ``coord acceptance run``/
    ``record`` on a worker's own checkout) and the dispatch-time GitHub-fetch
    reader (:func:`coord.milestone_dispatch.issue_oracle_ready`, #1138) so
    both agree on the manifest schema.

    Three on-disk shapes are accepted:

    - ``tests: {<test-id>: <issue-number>, ...}`` — flat, one issue per test.
    - ``issues: {<issue-number>: [<test-id>, ...], ...}`` — grouped by issue.
    - ``exempt: [<issue-number>, ...]`` — issues exempted from the #1138
      issue-level oracle gate (no slice required before Work dispatch). An
      entry may instead be a mapping, ``{issue: N, covered_by: M, artifact:
      "glob"}`` (#3212) — same exemption, plus a machine-readable promise
      that issue *M* covers it, checked by :func:`verify_exempt_dependency`
      rather than trusted forever. A plain integer entry followed by a
      trailing comment naming another issue (``- 6  # covered by #2``) has
      that promise inferred best-effort (``ExemptDependency.source ==
      "comment"``) — see :func:`_extract_exempt_comment_deps`.

    Plus two milestone-level blocks:

    - ``gate_a: {exempt: true, reason: "..."}`` — this milestone's contract
      may be consumed without a recorded human sign-off (#2063). ``gate_a:
      true`` is accepted as shorthand. Anything else (including a missing
      key) leaves the gate on.
    - ``expected_red: {<issue_number>: [<test-id>, ...], ...}`` (#2164) — the
      test-ids a sealed slice is authored to fail *right now*, before its
      fix exists. Non-dict/non-list entries are ignored rather than raising
      — a malformed ``expected_red`` block degrades to "nothing is
      expected-red" (fails toward the stricter, ordinary-CI behavior)
      instead of blowing up the parse.
    """
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ManifestError(f"failed to parse manifest {source}: {e}") from e
    if raw is None:
        return ManifestData()
    if not isinstance(raw, dict):
        raise ManifestError(f"manifest {source} must be a mapping")

    mapping: dict[str, int] = {}
    tests_raw = raw.get("tests")
    if isinstance(tests_raw, dict):
        for test_id, issue in tests_raw.items():
            mapping[str(test_id)] = int(issue)

    issues_raw = raw.get("issues")
    if isinstance(issues_raw, dict):
        for issue, test_ids in issues_raw.items():
            if not isinstance(test_ids, list):
                continue
            for test_id in test_ids:
                mapping[str(test_id)] = int(issue)

    exempt_nums: set[int] = set()
    exempt_deps: dict[int, ExemptDependency] = {}
    exempt_raw = raw.get("exempt")
    if isinstance(exempt_raw, list):
        for entry in exempt_raw:
            if isinstance(entry, bool):
                continue
            if isinstance(entry, int):
                exempt_nums.add(entry)
                continue
            if isinstance(entry, dict):
                try:
                    issue_num = int(entry["issue"])
                except (KeyError, TypeError, ValueError):
                    continue
                exempt_nums.add(issue_num)
                covered_by_raw = entry.get("covered_by")
                if covered_by_raw is None:
                    continue
                try:
                    covered_by = int(covered_by_raw)
                except (TypeError, ValueError):
                    continue
                artifact_raw = entry.get("artifact")
                exempt_deps[issue_num] = ExemptDependency(
                    issue=issue_num,
                    covered_by=covered_by,
                    artifact=str(artifact_raw) if artifact_raw else None,
                    source="declared",
                )
    exempt = frozenset(exempt_nums)
    # #3212: fill in comment-inferred dependencies for entries that didn't
    # already get a structured one above — a declared `covered_by` is never
    # overridden by a best-effort comment guess.
    for issue_num, dep in _extract_exempt_comment_deps(text).items():
        if issue_num in exempt_nums and issue_num not in exempt_deps:
            exempt_deps[issue_num] = dep

    gate_a_exempt = False
    gate_a_reason = ""
    gate_a_raw = raw.get("gate_a")
    if isinstance(gate_a_raw, dict):
        gate_a_exempt = bool(gate_a_raw.get("exempt"))
        gate_a_reason = str(gate_a_raw.get("reason") or "")
    elif isinstance(gate_a_raw, bool):
        gate_a_exempt = gate_a_raw

    expected_red: dict[int, frozenset[str]] = {}
    expected_red_raw = raw.get("expected_red")
    if isinstance(expected_red_raw, dict):
        for issue, test_ids in expected_red_raw.items():
            if not isinstance(test_ids, list):
                continue
            try:
                issue_num = int(issue)
            except (TypeError, ValueError):
                continue
            expected_red[issue_num] = frozenset(str(t) for t in test_ids)

    return ManifestData(
        tests=mapping,
        exempt=exempt,
        gate_a_exempt=gate_a_exempt,
        gate_a_exempt_reason=gate_a_reason,
        expected_red=expected_red,
        exempt_deps=exempt_deps,
    )


# #3212: a plain `- 6  # ... covered by ... #2 ...` entry's dependency is only
# visible in the raw text -- `yaml.safe_load` above discards every comment,
# so a structured `{issue: 6, covered_by: 2}` mapping is the only shape the
# parsed `raw` dict could ever carry it in. This scans the `exempt:` block's
# source lines directly (best-effort: prose in a comment, not a contract) so
# the many manifests that already write the promise as a comment -- exactly
# the format-converter ms-1 incident this issue describes -- get *some*
# machine-readable signal without a hand-edit, matching the issue's "cheaper
# interim" suggestion #2 (option #1, a mandatory structured field, is left to
# whoever authors a new `exempt:` entry going forward; this only recovers
# what's already on disk).
_EXEMPT_BLOCK_START_RE = re.compile(r"^exempt:\s*(#.*)?$")
_EXEMPT_PLAIN_ITEM_RE = re.compile(r"^\s*-\s*(\d+)\s*(#(?P<comment>.*))?$")
_ISSUE_REF_RE = re.compile(r"#(\d+)")


def _extract_exempt_comment_deps(text: str) -> "dict[int, ExemptDependency]":
    """Best-effort ``{issue_number: ExemptDependency}`` for every plain
    ``exempt:`` list entry whose trailing comment names a *different* issue
    number (``- 6  # ... covered by ... #2 ...`` -> ``{6: ExemptDependency(
    issue=6, covered_by=2, source="comment")}``). The first ``#N`` reference
    in the comment wins when more than one appears. An entry with no comment,
    or whose comment names only itself, is left out of the result — nothing
    to infer.

    Scans line-by-line rather than parsing YAML: once ``yaml.safe_load`` has
    run, the comment is already gone, so this is the only place in the parse
    pipeline that can still see it. The ``exempt:`` block is considered ended
    only once a non-blank line dedents all the way back to column 0 (a
    sibling top-level key) — a structured ``- issue: 6`` entry's own indented
    ``covered_by:``/``artifact:`` sub-lines don't match the plain-item regex
    below, but must NOT end the block early (they're just not plain-item
    lines this function extracts anything from; the structured path above
    already handles them), or a later plain entry in the SAME list would be
    missed entirely.
    """
    deps: dict[int, ExemptDependency] = {}
    in_block = False
    for line in text.splitlines():
        if _EXEMPT_BLOCK_START_RE.match(line):
            in_block = True
            continue
        if not in_block:
            continue
        if not line.strip():
            continue
        if not line[:1].isspace():
            in_block = False
            continue
        m = _EXEMPT_PLAIN_ITEM_RE.match(line)
        if m is None:
            continue
        issue_num = int(m.group(1))
        comment = m.group("comment") or ""
        refs = [int(r) for r in _ISSUE_REF_RE.findall(comment) if int(r) != issue_num]
        if refs:
            deps[issue_num] = ExemptDependency(
                issue=issue_num, covered_by=refs[0], artifact=None, source="comment",
            )
    return deps


def merge_manifest_data(*datas: ManifestData) -> ManifestData:
    """Merge multiple :class:`ManifestData` instances into one, as if their
    source files had been scanned together (#2543) — same "later source
    wins on a bare collision" rule :func:`load_manifest`/:func:`_manifest_paths`
    already apply across files, just operating on already-parsed data
    instead of paths. Shared by both the local-checkout readers (which
    merge ``_manifest_paths``' legacy-file-then-fragments per ``ms-NN``) and
    the API-only dispatch-time reader
    (:func:`coord.milestone_dispatch._fetch_manifest_data`, which fetches
    the legacy file and one issue's fragment separately over ``gh`` and
    needs to combine them the same way).

    ``tests``/``exempt``/``expected_red`` union across every *datas* entry
    (a later entry's ``tests`` mapping overwrites an earlier one's on an
    exact test-id collision; ``exempt``/``expected_red`` union rather than
    overwrite since those are naturally per-issue-keyed sets, not a flat
    mapping that can collide). ``gate_a_exempt``/``gate_a_exempt_reason``
    are milestone-level, not per-issue, so they come from whichever entry
    sets ``gate_a_exempt=True`` last — in practice always the legacy shared
    file, since only that carries the ``gate_a:`` block (#2543 keeps it
    there by choice; per-issue fragments never set it). ``exempt_deps``
    (#3212) is per-issue-keyed like ``expected_red``: a later entry's
    dependency for the same issue number overwrites an earlier one's rather
    than being dropped or unioned incoherently.
    """
    tests: dict[str, int] = {}
    exempt: set[int] = set()
    expected_red: dict[int, frozenset[str]] = {}
    exempt_deps: dict[int, ExemptDependency] = {}
    gate_a_exempt = False
    gate_a_exempt_reason = ""
    for data in datas:
        tests.update(data.tests)
        exempt |= set(data.exempt)
        for issue_number, test_ids in data.expected_red.items():
            expected_red[issue_number] = expected_red.get(issue_number, frozenset()) | test_ids
        exempt_deps.update(data.exempt_deps)
        if data.gate_a_exempt:
            gate_a_exempt = True
            gate_a_exempt_reason = data.gate_a_exempt_reason or gate_a_exempt_reason
    return ManifestData(
        tests=tests,
        exempt=frozenset(exempt),
        gate_a_exempt=gate_a_exempt,
        gate_a_exempt_reason=gate_a_exempt_reason,
        expected_red=expected_red,
        exempt_deps=exempt_deps,
    )


def _parse_manifest_file(path: Path) -> dict[str, int]:
    """Parse one manifest file into ``{test_id: issue_number}`` (the
    ``exempt:`` list, if any, is dropped — callers that need it use
    :func:`parse_manifest_text` directly). Two on-disk shapes are accepted:

    - ``tests: {<test-id>: <issue-number>, ...}`` — flat, one issue per test.
    - ``issues: {<issue-number>: [<test-id>, ...], ...}`` — grouped by issue.
    """
    try:
        text = path.read_text()
    except OSError as e:
        raise ManifestError(f"failed to parse manifest {path}: {e}") from e
    return parse_manifest_text(text, source=str(path)).tests


def load_manifest(acceptance_root: Path) -> dict[str, int]:
    """Merge every ``ms-NN/manifest.(yml|json)`` under *acceptance_root* into
    one ``{test_id: issue_number}`` mapping.

    Returns ``{}`` when *acceptance_root* doesn't exist or has no manifest
    files yet (the suite hasn't been authored — sibling issue #931). Later
    manifests win on a test-id collision (last one scanned, sorted by path
    for determinism) rather than raising, since two milestones legitimately
    sharing a test id is an authoring bug, not something this reader should
    crash the whole run over.
    """
    mapping: dict[str, int] = {}
    for path in _manifest_paths(acceptance_root):
        mapping.update(_parse_manifest_file(path))
    return mapping


def _driver_kind_for_manifest_dir(ms_dir: Path) -> str | None:
    """Best-effort LOCAL driver-kind lookup for one ``ms-NN``/``issue-NN``
    acceptance directory, via its ``mocks/`` file extensions
    (:data:`MOCK_EXT_TO_DRIVER_KIND`) — the filesystem counterpart of
    :func:`resolve_for_path`'s GitHub-backed lookup, for callers that
    already have a local checkout and don't want a network round trip.

    Returns ``None`` (unknown) rather than guessing when ``mocks/`` is
    absent, empty, or its files imply more than one kind — callers MUST
    treat ``None`` as "don't exclude", never as "safe to skip": this feeds
    a hard-failure check (:func:`load_expected_red`), and silently
    dropping a manifest because its kind couldn't be determined would hide
    the exact class of bug that check exists to catch.
    """
    mocks_dir = ms_dir / "mocks"
    if not mocks_dir.is_dir():
        return None
    kinds = {
        MOCK_EXT_TO_DRIVER_KIND[p.suffix]
        for p in mocks_dir.iterdir()
        if p.suffix in MOCK_EXT_TO_DRIVER_KIND
    }
    return next(iter(kinds)) if len(kinds) == 1 else None


def load_expected_red(
    acceptance_root: Path, *, driver_kind: str | None = None
) -> dict[str, int]:
    """Merge every ``ms-NN/manifest.(yml|json)``'s ``expected_red:`` block
    under *acceptance_root* into one ``{test_id: issue_number}`` mapping
    (#2164) — the flat shape :func:`apply_expected_red` and the ``coord
    acceptance run --all --ci`` CI wrapper consume.

    Mirrors :func:`load_manifest`'s merge/empty-dict/last-writer-wins
    conventions exactly, just reading ``expected_red`` instead of ``tests``.

    *driver_kind* (#2339): when given, a manifest whose own directory
    resolves (via :func:`_driver_kind_for_manifest_dir`) to a DIFFERENT
    driver kind is skipped entirely. A repo routes different milestones to
    different drivers (``coord/**`` -> cli-pytest, ``tui/**`` ->
    tui-tuidriver, ...), but a single ``coord acceptance run --for-path X
    --all`` invocation only ever executes ONE of them — before this
    parameter existed, the merged registry always spanned every driver, so
    every milestone's ``expected_red`` ids that belonged to a DIFFERENT
    driver than the one actually running were reported as
    ``missing_expected_red_ids`` (a hard, un-waivable CI failure) on every
    single run, for every repo with more than one routed driver kind, the
    moment two milestones used different drivers. A directory whose kind
    can't be determined (see above) is always included, matching the
    unfiltered (``driver_kind=None``) behaviour this defaults to.
    """
    mapping: dict[str, int] = {}
    for path in _manifest_paths(acceptance_root):
        if driver_kind is not None:
            resolved = _driver_kind_for_manifest_dir(_ms_dir_for_manifest_path(path))
            if resolved is not None and resolved != driver_kind:
                continue
        try:
            data = parse_manifest_text(path.read_text(), source=str(path))
        except OSError as e:
            raise ManifestError(f"failed to parse manifest {path}: {e}") from e
        for issue_number, test_ids in data.expected_red.items():
            for test_id in test_ids:
                mapping[test_id] = issue_number
    return mapping


def ms_dir_for_issue(acceptance_root: Path, issue_number: int) -> str | None:
    """The ``ms-NN`` directory name (under *acceptance_root*) whose manifest
    covers *issue_number*, or ``None`` if no manifest maps any test to it yet
    (the issue's slice hasn't been authored — #945 uses this to decide
    whether there's a contract to point the worker at).

    Unlike :func:`load_manifest`, this checks manifests **per file** rather
    than merging first, since the whole point is recovering *which* ``ms-NN``
    dir a given issue's tests live under.
    """
    for path in _manifest_paths(acceptance_root):
        mapping = _parse_manifest_file(path)
        if test_ids_for_issue(mapping, issue_number):
            return _ms_dir_for_manifest_path(path).name
    return None


def ms_dir_for_exempt_issue(acceptance_root: Path, issue_number: int) -> str | None:
    """(#3212) Sibling to :func:`ms_dir_for_issue`: the ``ms-NN`` directory
    name (under *acceptance_root*) whose manifest's ``exempt:`` list names
    *issue_number*, or ``None`` if no manifest exempts it.

    Exists so :func:`oracle_loop_contract_block` can still point an exempted
    issue's worker at the milestone's Gate-A contract/mocks even though the
    issue itself has no test mapping (:func:`ms_dir_for_issue` returns
    ``None`` for it) — an exemption waives the *automated gate*, not the
    *design contract* the milestone's mocks define (issue #3212's "more
    damaging half": before this, an exempted issue's worker briefing carried
    zero pointer to either).

    Same per-file scan discipline as :func:`ms_dir_for_issue` (never merges
    manifests first) — recovering *which* directory did the exempting is the
    whole point, same as recovering which directory owns a test mapping.
    """
    for path in _manifest_paths(acceptance_root):
        try:
            data = parse_manifest_text(path.read_text(), source=str(path))
        except Exception:  # noqa: BLE001 — a malformed manifest is skipped, not raised
            continue
        if issue_number in data.exempt:
            return _ms_dir_for_manifest_path(path).name
    return None


def oracle_loop_contract_block(
    acceptance_root: Path,
    repo_name: str,
    issue_number: int,
    *,
    acceptance_dirname: str = ACCEPTANCE_DIRNAME,
) -> str:
    """The worker briefing contract (#945, docs/ORACLE_LOOP.md "The worker
    briefing contract") prepended to the TOP of a Work briefing when
    *issue_number* has a sealed acceptance slice authored for it under
    *acceptance_root* — or (#3212) is exempted from one, in which case a
    variant of the same block still points at the milestone's Gate-A
    contract/mocks, just worded for "no automated gate checks you against
    this, but the design contract still applies" rather than "run `coord
    acceptance run` against your own slice".

    Returns ``""`` when the issue has neither an authored slice nor an
    exemption naming it (nothing to point the worker at — Gate A/#931 hasn't
    run for it) or on any read error. Fully fail-soft — mirrors
    ``coord.state.issue_context_block`` (#603): this runs on the dispatch hot
    path, so a manifest hiccup must degrade to "no block" rather than break
    dispatch.

    *acceptance_dirname* (#2896) is the repo-relative dirname the returned
    text should NAME (``contract.md``/``mocks/`` paths, the "may not edit"
    line) — defaults to the shared repo-root :data:`ACCEPTANCE_DIRNAME`,
    unchanged for a directory-discovered driver. *acceptance_root* itself
    is always the caller-resolved absolute path actually scanned for a
    manifest; callers driving a repo with an entrypoint-linked driver (#2896
    relocated its slices to that entrypoint's own sibling ``acceptance/``
    dir) must pass BOTH together — the local root to scan and the matching
    repo-relative name to print — or the printed path won't match where the
    scan actually found the slice.
    """
    exempt = False
    try:
        ms_dir = ms_dir_for_issue(acceptance_root, issue_number)
        if ms_dir is None:
            ms_dir = ms_dir_for_exempt_issue(acceptance_root, issue_number)
            exempt = ms_dir is not None
    except Exception:  # noqa: BLE001 — never let a manifest read break dispatch
        return ""
    if ms_dir is None:
        return ""

    dirname = acceptance_dirname.rstrip("/") if acceptance_dirname else ACCEPTANCE_DIRNAME
    contract_path = f"{dirname}/{ms_dir}/contract.md"
    mocks_dir = f"{dirname}/{ms_dir}/mocks"

    if exempt:
        # #3212: the issue's OWN slice is waived, but the milestone's Gate-A
        # contract/mocks are still the design truth — an exemption from
        # verification is not an exemption from the spec. Deliberately drops
        # the "run `coord acceptance run --issue N`" instruction below (there
        # is no slice of this issue's own to run) but keeps the "don't touch
        # the sealed tree" and "write your own tests" bullets, since both
        # still apply verbatim to an exempted issue.
        return (
            "## 🔒 Oracle-loop acceptance contract — READ THIS FIRST "
            "(exempted issue, #3212)\n\n"
            f"This issue is **exempted** from its own acceptance-slice gate "
            f"(`{dirname}/{ms_dir}/manifest.*`'s `exempt:` list) — but "
            "exemption waives the automated *verification*, not the "
            f"*design*. Treat `{contract_path}` (the black-box surface) — "
            f"and, if present, the rendered mock(s) under `{mocks_dir}/` — "
            "as the spec for what you build, exactly as if your own slice "
            "were being checked against them. No automated suite verifies "
            "this issue at all (that is what the exemption means), so a "
            "human at UAT is the only thing left between a mismatch and the "
            "customer — match the mocks precisely.\n\n"
            f"- You **may not** edit `{dirname}/**` (contract, mocks, or any "
            "sealed suite), even though your own slice is exempt from it.\n"
            "- Write your own unit / internal tests — that is still your "
            "job, and the only automated coverage this issue will get.\n\n"
            "---\n\n"
        )

    return (
        "## 🔒 Oracle-loop acceptance contract — READ THIS FIRST\n\n"
        "This issue has a sealed acceptance slice authored for it. Treat "
        f"`{contract_path}` (the black-box surface) — and, if present, "
        f"the rendered mock(s) under `{mocks_dir}/` — as the spec — not "
        "guesswork. For a web slice the mocks ARE part of the contract "
        "(hand-authored HTML wireframes, one per screen state): the app "
        "must satisfy the sealed assertions written against them, not the "
        "other way around.\n\n"
        f"- You **may not** edit `{dirname}/**` (contract, "
        "mocks, or the sealed suite). It is the sealed oracle, authored "
        "independently of your work — touching it fails the gate.\n"
        f"- Run `coord acceptance run --repo {repo_name} --issue "
        f"{issue_number}` to check yourself; iterate in this warm session "
        "until your slice is green, then release.\n"
        "- Write your own unit / internal tests too — that is still your "
        "job.\n"
        "- If your slice won't converge — the failing set churns rather "
        "than shrinks across 2 rounds — **stop grinding**: run "
        f"`coord acceptance stall --repo {repo_name} --issue {issue_number} "
        '--tried "..." --stuck "..."` (#846) so the coordinator sees it '
        "immediately, in addition to a `STUCK:` line for the interactive "
        "log.\n\n"
        "---\n\n"
    )


def test_ids_for_issue(manifest: dict[str, int], issue_number: int) -> set[str]:
    """The set of test ids mapped to *issue_number* in *manifest*."""
    return {test_id for test_id, issue in manifest.items() if issue == issue_number}


def build_verdict(
    tests: list[dict],
    *,
    scope: str,
    issue_number: int | None = None,
) -> dict[str, Any]:
    """Assemble the structured pass/fail payload ``coord acceptance run``
    prints and ``record`` persists a summary of.

    *tests* is the (already filtered, when scoped to one issue) list of
    ``{"id", "status", "message"}`` dicts from a driver. Sealed: this only
    ever carries verdicts (id/status/message), never test source.
    """
    passed = sum(1 for t in tests if t.get("status") == "pass")
    failed = sum(1 for t in tests if t.get("status") == "fail")
    skipped = sum(1 for t in tests if t.get("status") == "skip")
    payload: dict[str, Any] = {
        "scope": scope,
        "total": len(tests),
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "green": failed == 0 and len(tests) > 0,
        "tests": tests,
    }
    if issue_number is not None:
        payload["issue"] = issue_number
    return payload


def failure_summary(verdict: dict[str, Any], *, limit: int = 5) -> str:
    """One-line-per-failure summary text for a verdict payload (used as the
    Acceptance-gate reason string and the #603 durable-context note)."""
    failing = [t for t in verdict.get("tests", []) if t.get("status") == "fail"]
    if not failing:
        return ""
    lines = [f"{t['id']}: {t.get('message') or 'failed'}" for t in failing[:limit]]
    if len(failing) > limit:
        lines.append(f"... and {len(failing) - limit} more")
    return "\n".join(lines)


def apply_expected_red(verdict: dict[str, Any], expected_red_ids: "set[str]") -> dict[str, Any]:
    """Mutate + return *verdict* (from :func:`build_verdict`) with the
    #2164 expected-red accounting the CI wrapper (``coord acceptance run
    --all --ci``) needs, on top of the raw ``green`` field callers already
    relied on before this existed.

    Adds:

    - ``expected_red_still_red``: ids in *expected_red_ids* that failed —
      the ordinary, designed-for case. Excluded from ``ci_green``'s failure
      count.
    - ``unexpected_green``: ids in *expected_red_ids* that PASSED — the
      loud, distinguishable hard failure this registry exists to catch
      (#1965's "an assertion that never exercised the bug"). A single one
      of these is enough to fail ``ci_green`` even if every other test is
      green.
    - ``missing_expected_red_ids``: ids in *expected_red_ids* that appeared
      in neither the pass nor the fail set — i.e. the driver never emitted
      a verdict for them at all (a broken entry point, an accidentally
      deleted test — the same "wiring failure" :func:`build_verdict`'s
      scoped-verdict sibling already detects via its own ``missing_ids``,
      #1125 review finding 2; the ``--all``/``--ci`` path had no equivalent
      until now). Also enough to fail ``ci_green`` — a vanished
      expected-red test is invisible either way (neither
      ``expected_red_still_red`` nor ``unexpected_green``) unless this is
      checked explicitly.
    - ``ci_green``: true iff there are zero real (non-expected-red)
      failures AND zero unexpected-green ids AND zero missing-expected-red
      ids AND at least one test ran. This — not the raw ``green`` — is what
      a CI gate should key off of; ``green`` is left untouched so existing
      (non-CI) callers of :func:`build_verdict` see no behavior change.

    A no-op (``ci_green == green``, all three new lists empty) when
    *expected_red_ids* is empty — the overwhelmingly common case (most
    slices have nothing expected-red).
    """
    tests = verdict.get("tests", [])
    if not expected_red_ids:
        verdict["expected_red_still_red"] = []
        verdict["unexpected_green"] = []
        verdict["missing_expected_red_ids"] = []
        verdict["ci_green"] = verdict["green"]
        return verdict

    seen_ids = {t["id"] for t in tests}
    unexpected_green = sorted(
        t["id"] for t in tests if t.get("status") == "pass" and t["id"] in expected_red_ids
    )
    expected_red_still_red = sorted(
        t["id"] for t in tests if t.get("status") == "fail" and t["id"] in expected_red_ids
    )
    missing_expected_red_ids = sorted(expected_red_ids - seen_ids)
    real_failures = sum(
        1 for t in tests if t.get("status") == "fail" and t["id"] not in expected_red_ids
    )
    verdict["unexpected_green"] = unexpected_green
    verdict["expected_red_still_red"] = expected_red_still_red
    verdict["missing_expected_red_ids"] = missing_expected_red_ids
    verdict["ci_green"] = (
        len(tests) > 0
        and real_failures == 0
        and not unexpected_green
        and not missing_expected_red_ids
    )
    return verdict


def expected_red_failure_summary(verdict: dict[str, Any]) -> str:
    """Loud, distinguishable-from-an-ordinary-failure message for a verdict
    whose ``unexpected_green`` (from :func:`apply_expected_red`) is
    non-empty — a test-id the manifest says is ``expected_red`` but which
    just PASSED. Returns ``""`` when there's nothing to report.

    This is deliberately worded differently from :func:`failure_summary`'s
    per-test failure lines: the point (#1965) is that a human/CI reader
    can't mistake this for "a test failed" — it is the opposite signal, and
    the fix is editorial (clear the manifest entry, or realize the
    assertion never exercised the bug), not code.

    #2199: the guidance below used to send the operator to `coord
    acceptance record`, on the claim that a green trust-gate run clears the
    entry automatically. PR #2173 deliberately moved clearing OUT of
    `record` and into the post-merge hook (`coord.merge_queue.
    _maybe_clear_expected_red` / `coord.acceptance.
    clear_expected_red_via_pr`, right after the fix's own PR actually
    merges — see that function's docstring for the ordering bug this
    fixed). Re-running `record` today clears nothing; an operator who
    followed the old wording would watch it not work with no clue why.
    """
    ids = verdict.get("unexpected_green") or []
    if not ids:
        return ""
    listed = "\n".join(f"  - {i}" for i in ids)
    return (
        f"HARD FAILURE: {len(ids)} test(s) listed in `expected_red` now PASS:\n"
        f"{listed}\n"
        "An expected-red test that passes means either the fix already "
        "landed silently — it clears automatically once ITS OWN PR actually "
        "merges (the post-merge hook, not `coord acceptance record`; see "
        "docs/ORACLE_LOOP.md). Still listed after that merge? Check `coord "
        "merge`'s own output for an `expected_red_clear_skipped*` event "
        "naming why (no work assignment found, no passing trust-gate "
        "verdict recorded, or a stale acceptance SHA), or run `coord "
        "acceptance expected-red <repo>` to see current state — or the "
        "assertion never exercised the bug in the first place (#1965). "
        "This is NOT an ordinary test failure — it is the opposite signal."
    )


def dump_manifest_error_hint(acceptance_root: Path) -> str:
    """Human-facing hint for "no manifest found" — points at the authoring
    step (#931) rather than leaving the operator guessing."""
    return (
        f"no acceptance manifest found under {acceptance_root} — the sealed "
        "suite has not been authored yet for this repo (see docs/ORACLE_LOOP.md "
        "/ #931)."
    )


# ── #3202: Gate-A contract exempt-exposure warning ──────────────────────────
#
# A milestone can carry a Gate-A contract (the customer-facing behaviour
# spec a human signed off on, docs/ORACLE_LOOP.md) while ALSO exempting some
# or all of its issues from the acceptance-slice gate via manifest.yml's
# `exempt:` list (the #1138 issue-level opt-out — see `ManifestData`'s
# docstring above). Each is individually reasonable — the decomposition may
# judge the work not oracle-shaped, and the contract may exist only because
# a customer needed a design round — but the combination means NOTHING
# checks the contract's behaviours except a human at the UAT gate, one step
# before merge. The format-converter ms-1 incident (#3202) shipped a UI with
# no source pane at all, contradicting the approved mock's primary screen,
# through Review, CI and unit tests, caught only by a human looking.
#
# Neither side refuses the combination — a refusal here would just get
# worked around, and it is sometimes the right call. This only makes the
# trade VISIBLE, with the SAME wording, at every seam that reads this state:
#
# - the pre-dispatch guard that suggests the exemption in the first place
#   (`coord.milestone_dispatch.issue_oracle_ready`'s "add it to `exempt:`"
#   refusal reason, via `_gate_a_exempt_trade_off_note`) — also where "say
#   it in the manifest too" is delivered: that note hands the operator
#   `manifest_exempt_generated_comment`'s ready-to-paste text alongside the
#   suggestion, since nothing in this codebase edits `exempt:` on a human's
#   behalf (it is "rare, hand-edited" by design — see
#   `MANIFEST_FRAGMENTS_DIRNAME`'s comment below);
# - `coord gates` for a milestone already in that state
#   (`coord.gates._gate_a_exempt_note_for_winner`, via
#   :func:`fetch_gate_a_exempt_warning` below — the shared fetch-and-detect
#   seam this module exposes so neither caller re-derives the manifest/
#   contract fetch loop independently); and
# - the reviewer's own briefing (`coord.review.build_review_briefing`, via
#   the same :func:`fetch_gate_a_exempt_warning`).
#
# `gate_a_exempt_warning` below is the single canonical text every one of
# those seams renders — never re-derive the wording independently, or a
# future edit updates one copy and silently leaves the others stale (the
# same "one question, one answer" discipline as #3180's mechanical-verdict
# helpers in `coord.review`). `coord.diagnose.gate_a_exempt_exposure_lines`
# renders the same detection + wording for a caller that already has a
# manifest/contract in hand (no fetch of its own) — not yet called from
# `coord doctor`, which has no existing per-milestone iteration to hang it
# off without new fleet-wide scanning plumbing that is out of scope here.

_BEHAVIOUR_LABEL_RE = re.compile(r"\*\*(§\d+[a-zA-Z]?|[A-Za-z]{1,3}\d{1,3})\*\*")


def count_declared_behaviours(contract_text: str) -> int:
    """Best-effort count of individually-labelled behaviours in a Gate-A
    ``contract.md``'s prose (#3202).

    Every contract this codebase has authored anchors one assertable
    behaviour with a short bold label — ``**§4a**`` (this repo's own
    ``tests/acceptance/ms-51/contract.md``) or ``**B4**`` (the
    format-converter ms-1 incident #3202 describes) — so a distinct-label
    count is a reasonable proxy for "how much of this contract exists"
    without this module needing to parse, or agree on, one fixed markdown
    dialect across repos. Labels are deduplicated (a label referenced
    twice — e.g. once in prose and again in a footnote — counts once).

    Returns ``0`` for text with no such labels — a contract written in some
    other style, an empty string, or no contract at all — rather than
    falling back to a heading count or another guess: ``0`` is a visible
    "this heuristic found nothing", not a silently wrong number.
    """
    if not contract_text:
        return 0
    return len({m.group(1) for m in _BEHAVIOUR_LABEL_RE.finditer(contract_text)})


@dataclass(frozen=True)
class GateAExemptExposure:
    """One milestone's #3202 exposure: it exempts one or more issues from
    the acceptance-slice gate, so a Gate-A contract's behaviours go
    unverified by anything but UAT for that exempted work.

    Built by :func:`gate_a_exempt_exposure`; a caller that wants the
    canonical detection rule applied should go through that function rather
    than constructing this directly — it is the one place "does this
    milestone have this exposure" is answered (see the module note above).
    """

    milestone_number: int
    exempt_issues: "tuple[int, ...]"
    #: :func:`count_declared_behaviours` applied to the milestone's
    #: contract.md, or ``0`` when the contract couldn't be fetched/parsed —
    #: see that function's docstring for why ``0`` is left visible rather
    #: than hidden behind a fallback guess.
    behaviour_count: int


def gate_a_exempt_exposure(
    milestone_number: int,
    manifest: ManifestData,
    contract_text: str | None,
) -> "GateAExemptExposure | None":
    """Detect #3202's exposure for one milestone: does *manifest* exempt any
    issue from the acceptance-slice gate at all?

    Returns ``None`` when ``manifest.exempt`` is empty — the ordinary case,
    nothing to warn about. *contract_text* is optional: pass ``None`` when
    the contract couldn't be fetched (every caller here is fail-open) and
    the returned exposure just carries ``behaviour_count=0`` rather than
    blocking detection on a fetch that failed — UNLESS *manifest* itself
    says there is no contract to fetch in the first place (see below), in
    which case ``None`` (no exposure at all) is returned instead.

    This function does not itself CONFIRM a Gate-A contract exists for
    *milestone_number* — a caller with a genuinely-fetched *contract_text*
    already knows one does (the same "only call this once contract.md
    exists" precondition every other Gate-A-gated check in this module
    already applies, e.g. :func:`gate_a_contract_candidates`'s callers) and
    gets an exposure back regardless of ``manifest.gate_a_exempt``.

    #3202 review: a caller that could NOT fetch a contract (*contract_text*
    is ``None``) is in a genuinely ambiguous spot — a transient fetch
    failure and "no contract.md was ever authored" look identical from out
    here. *manifest* itself resolves that ambiguity in exactly the one case
    it can: ``manifest.gate_a_exempt`` (``gate_a: {exempt: true, ...}`` —
    docs/ORACLE_LOOP.md) is the milestone's own declared, reviewable opt-out
    from Gate-A, recorded in the same file this function already reads. When
    it's set, "couldn't fetch a contract" is read as "there isn't one to
    fetch" rather than "unknown" — so this returns ``None`` (no exposure,
    nothing to warn about) instead of asserting "has a Gate-A contract" with
    a fabricated "count unavailable". A fetch failure with
    ``gate_a_exempt`` unset keeps the prior fail-loud behaviour (warn with
    "count unavailable") — the plain network-hiccup case, where hiding the
    warning would be the wrong direction to fail in.
    """
    if not manifest.exempt:
        return None
    if contract_text is None and manifest.gate_a_exempt:
        return None
    return GateAExemptExposure(
        milestone_number=milestone_number,
        exempt_issues=tuple(sorted(manifest.exempt)),
        behaviour_count=count_declared_behaviours(contract_text or ""),
    )


def gate_a_exempt_warning(exposure: GateAExemptExposure) -> str:
    """The single canonical #3202 warning text for *exposure* — rendered
    verbatim (never re-derived) by every surface that reads this state: the
    pre-dispatch guard's exemption suggestion, ``coord doctor``/``coord
    gates``, the reviewer's briefing (``coord.review.build_review_briefing``),
    and :func:`manifest_exempt_generated_comment` below.
    """
    n = len(exposure.exempt_issues)
    issues_str = ", ".join(f"#{i}" for i in exposure.exempt_issues)
    behaviours_str = (
        f"{exposure.behaviour_count} declared behaviour"
        f"{'s' if exposure.behaviour_count != 1 else ''}"
        if exposure.behaviour_count
        else "declared behaviours (count unavailable)"
    )
    return (
        f"⚠️ ms-{exposure.milestone_number} has a Gate-A contract "
        f"({behaviours_str}) but exempts {n} issue{'s' if n != 1 else ''} "
        f"({issues_str}) from acceptance slices — nothing but a human at the "
        "UAT gate verifies the contract's behaviours for that work (#3202). "
        "This can be the right call (the work may not be oracle-shaped), but "
        "make it deliberately, not as a silent side effect of the exempt: "
        "list."
    )


def manifest_exempt_generated_comment(exposure: GateAExemptExposure) -> str:
    """Generated ``#`` comment block (#3202) recording, in the manifest
    itself, exactly what an ``exempt:`` list trades away — so the next
    person reading ``manifest.yml`` sees the consequence stated in writing,
    rather than an unstated side effect of a bare list of issue numbers.

    Pure text generation; this module never auto-edits a hand-maintained
    manifest (``exempt:`` is "rare, hand-edited" — see the
    :data:`MANIFEST_FRAGMENTS_DIRNAME` comment above) — a caller (a human
    author, or `coord acceptance author` tooling) is responsible for
    actually inserting the returned text next to the ``exempt:`` block.
    """
    issues_str = ", ".join(f"#{i}" for i in exposure.exempt_issues)
    behaviours = (
        f"{exposure.behaviour_count}" if exposure.behaviour_count
        else "an unknown number of"
    )
    return (
        "# ── #3202: this exempts the contract's behaviours from verification ──\n"
        f"# ms-{exposure.milestone_number}'s Gate-A contract declares "
        f"{behaviours} behaviour(s).\n"
        f"# This exempt: list opts {issues_str} out of authoring an "
        "acceptance slice, so none\n"
        "# of those behaviours are checked by anything but a human at the "
        "UAT gate for\n"
        "# that work. Confirmed intentional, not an oversight.\n"
    )


# (repo_github: str, path: str, branch: str) -> file content. Raises on
# not-found (mirrors ``coord.github_ops.get_repo_file``) — every caller here
# treats any exception as "this candidate doesn't exist" and tries the next
# one, so a fetcher that instead returned ``None``/``""`` on a miss would be
# indistinguishable from a genuinely empty file.
GateAExemptFileFetcher = Callable[[str, str, str], str]


def fetch_gate_a_exempt_warning(
    config: Config,
    repo: Repo,
    milestone_number: int | None,
    *,
    file_fetcher: GateAExemptFileFetcher | None = None,
) -> str | None:
    """(#3202) Fetch *milestone_number*'s manifest + Gate-A contract off
    *repo*'s default branch and, if the manifest exempts one or more issues
    from needing an acceptance slice, return the canonical
    :func:`gate_a_exempt_warning` text — or ``None`` when there's nothing to
    warn about, or nothing to check at all.

    THE single fetch-and-detect seam for this state (#3202 review finding:
    the reviewer's briefing, ``coord gates``, and any future ``coord
    doctor`` surfacing must all call this rather than re-deriving the
    manifest/contract fetch loop each independently — a second copy is
    exactly how the reviewer-briefing-only version of this fix drifted from
    the "every seam" claim in this module's own docstring).

    Fail-open like every other best-effort fetch this module makes ahead of
    a briefing/report: a missing manifest/contract, a repo with no
    acceptance driver configured, no milestone in hand, or a transient
    network hiccup all return ``None`` rather than raise — this is an
    advisory surfacing, never a gate, so a fetch failure must never affect
    whether or how a review/report proceeds.

    *file_fetcher* defaults to :func:`coord.github_ops.get_repo_file` (a
    real ``gh`` call); inject a stub in tests so this advisory lookup never
    shells out live.

    ``exempt:`` is milestone-level and lives only in the legacy single
    ``manifest.(yml|yaml|json)`` file, never a per-issue ``manifest.d/``
    fragment (see :data:`MANIFEST_FRAGMENTS_DIRNAME`'s comment: "rare,
    hand-edited... stays a single shared file by choice"), so only that file
    needs checking here — unlike a full manifest load, no fragment merge is
    needed.

    #3202 review (non-blocking): tries ``.yml``/``.yaml``/``.json`` under
    every :func:`search_roots_for_repo` root, breaking out of the extension
    loop the moment any fetch succeeds — even if the fetched text then fails
    to parse (``manifest_data`` resets to ``None`` and the loop moves to the
    next root, never trying a sibling extension under the SAME root that
    might have parsed). Accepted: a root carrying two same-named manifests
    at different extensions, one malformed, is not a shape this codebase
    produces — not incidental, a deliberate simplicity/rare-edge-case trade.
    """
    if milestone_number is None or not config.acceptance.has_driver(repo.name):
        return None

    from coord import github_ops  # noqa: PLC0415

    fetch = file_fetcher or github_ops.get_repo_file

    manifest_data = _fetch_milestone_manifest_data(config, repo, milestone_number, fetch)
    if manifest_data is None or not manifest_data.exempt:
        return None

    contract_text: str | None = None
    for path in gate_a_contract_candidates(config, repo.name, milestone_number):
        try:
            contract_text = fetch(repo.github, path, repo.default_branch)
            break
        except Exception:  # noqa: BLE001 — try the next candidate root
            continue

    exposure = gate_a_exempt_exposure(milestone_number, manifest_data, contract_text)
    if exposure is None:
        return None
    return gate_a_exempt_warning(exposure)


def _fetch_milestone_manifest_data(
    config: Config,
    repo: Repo,
    milestone_number: int,
    fetch: GateAExemptFileFetcher,
) -> "ManifestData | None":
    """Fetch + parse *milestone_number*'s legacy single-file manifest off
    *repo*'s default branch, trying every :func:`search_roots_for_repo` root
    and ``.yml``/``.yaml``/``.json`` extension in turn — the exact loop
    :func:`fetch_gate_a_exempt_warning` used to run inline, extracted so
    :func:`fetch_exempt_dependency_warnings` (#3212) can reuse it rather than
    re-deriving a second copy (the same "one fetch-and-detect seam" discipline
    the #3202 module note above already asks for).

    ``None`` when nothing fetchable parses — the caller's existing fail-open
    convention, unchanged from the inline version. Only the legacy shared
    file is read, never a per-issue ``manifest.d/`` fragment — see
    :func:`fetch_gate_a_exempt_warning`'s docstring for why that's the
    deliberate scope, not an oversight.
    """
    manifest_data: ManifestData | None = None
    for root in search_roots_for_repo(config, repo.name):
        ms_dir = f"{root.rstrip('/')}/{ms_dirname(milestone_number)}"
        for ext in (".yml", ".yaml", ".json"):
            try:
                text = fetch(repo.github, f"{ms_dir}/manifest{ext}", repo.default_branch)
            except Exception:  # noqa: BLE001 — this extension/root doesn't exist
                continue
            try:
                manifest_data = parse_manifest_text(
                    text, source=f"{ms_dir}/manifest{ext}"
                )
            except Exception:  # noqa: BLE001 — malformed manifest: fail open
                manifest_data = None
            break
        if manifest_data is not None:
            break
    return manifest_data


# ── #3212: exempt-dependency verification ───────────────────────────────────
#
# `exempt:`'s promise can be bare ("this issue needs no acceptance slice") or
# conditional ("... because #M covers it" — format-converter ms-1's own
# `exempt: [6]  # ... covered by the harness #2 stands up`). Nothing
# previously re-checked the conditional case: #2 merged, produced no spec
# files, and #6's slice stayed silently waived forever — through Review, CI
# and Test, caught only by a human at UAT. This section makes that specific
# promise (:class:`ExemptDependency`, parsed above) checkable: did the named
# issue actually land, and — when declared — did it produce the artifact it
# promised.
#
# Same posture as the #3202 block above: this does not REFUSE anything (an
# exempt issue's slice stays skipped either way — #3212's suggested shape
# explicitly keeps this a report, not a new gate, since retroactively
# un-exempting a shipped issue has nowhere left to go). It only makes the
# unmet promise VISIBLE, with one canonical wording, wherever this state is
# read — mirroring the #3202 "make it deliberately, not silently" posture
# one hop further down the same promise chain.


@dataclass(frozen=True)
class ExemptDependencyStatus:
    """(#3212) One :class:`ExemptDependency`, checked against live state.

    *unmet* is the single bit every caller acts on: ``True`` means the
    exemption's promise is unverified and should be reported loudly (the
    issue's "at minimum" bar) — the named issue hasn't landed, or (when an
    artifact was declared) it landed without producing it.
    """

    dep: ExemptDependency
    #: Whether ``dep.covered_by`` is closed on GitHub (this repo's
    #: convention: an issue closes when its work merges — see
    #: ``coord.hooks._close_merged_issues``).
    covered_by_closed: bool
    #: ``None`` when ``dep.artifact`` is unset (nothing declared to check);
    #: otherwise whether the glob matched anything under the checked root.
    artifact_found: "bool | None" = None

    @property
    def unmet(self) -> bool:
        if not self.covered_by_closed:
            return True
        return self.artifact_found is False


def verify_exempt_dependency(
    dep: ExemptDependency,
    repo_github: str,
    *,
    issue_is_closed: "Callable[[str, int], bool] | None" = None,
    artifact_root: "Path | None" = None,
) -> ExemptDependencyStatus:
    """(#3212) Check whether *dep*'s promise actually held.

    *issue_is_closed* defaults to :func:`coord.github_ops.issue_is_closed` —
    the same "did this land" answer this codebase already asks elsewhere
    (this repo's convention: an issue closes when its work merges, see
    ``coord.hooks._close_merged_issues``); never re-derived independently
    here. Inject a stub in tests so this never shells out live.

    *artifact_root* is a local checkout to glob ``dep.artifact`` against —
    best-effort and optional, since not every caller has one in hand (a
    dispatch-time or review-briefing fetch only has GitHub API reads, not a
    clone). ``artifact_found`` stays ``None`` ("not checked") rather than
    ``False`` ("checked and missing") when no root is given, so a caller can
    tell the two apart instead of reading an un-checked artifact as absent.

    Unlike the #3202 warnings above (advisory, fail-open on a fetch error),
    this fails CLOSED on the dependency check itself:
    :func:`~coord.github_ops.issue_is_closed` already fails open toward
    ``False`` on any transient error (per its own docstring), so an
    unreachable GitHub reads here as "hasn't landed" rather than being caught
    and silently trusted — the entire point of #3212 is that this promise
    was never being checked at all; a network hiccup must not reintroduce
    that same silence under a new name.
    """
    from coord import github_ops  # noqa: PLC0415

    is_closed = issue_is_closed or github_ops.issue_is_closed
    covered_by_closed = bool(is_closed(repo_github, dep.covered_by))

    artifact_found: "bool | None" = None
    if dep.artifact:
        artifact_found = False
        if artifact_root is not None:
            try:
                artifact_found = artifact_root.exists() and any(
                    artifact_root.glob(dep.artifact)
                )
            except (OSError, ValueError):
                artifact_found = False

    return ExemptDependencyStatus(
        dep=dep, covered_by_closed=covered_by_closed, artifact_found=artifact_found,
    )


def exempt_dependency_warning(status: ExemptDependencyStatus) -> str:
    """The canonical #3212 warning text for one unmet
    :class:`ExemptDependencyStatus` — rendered verbatim by every surface
    that reports it (``coord gates``, the reviewer's briefing), same
    "one canonical wording" discipline as :func:`gate_a_exempt_warning`."""
    dep = status.dep
    reasons: list[str] = []
    if not status.covered_by_closed:
        reasons.append(f"#{dep.covered_by} has not landed (still open)")
    if status.artifact_found is False:
        reasons.append(f"no file matching {dep.artifact!r} was found")
    reason_str = "; ".join(reasons) if reasons else "its promise is unverified"
    origin = (
        "" if dep.source == "declared"
        else " (inferred from the exempt: entry's own comment, not a declared dependency)"
    )
    return (
        f"⚠️ #{dep.issue}'s acceptance-slice exemption defers coverage to "
        f"#{dep.covered_by}{origin}, but that promise is unmet: {reason_str} "
        "(#3212). The exemption is still in effect and the gate stays "
        f"disarmed — this is reported, not enforced — so treat #{dep.issue}'s "
        "acceptance slice as unverified until a human checks it by hand."
    )


def unmet_exempt_dependency_warnings(
    statuses: "Sequence[ExemptDependencyStatus]",
) -> list[str]:
    """:func:`exempt_dependency_warning` for every *statuses* entry whose
    promise is unmet, in the caller's given order — the shared filter+render
    step every surface above should call instead of re-checking ``.unmet``
    and re-rendering the text independently."""
    return [exempt_dependency_warning(s) for s in statuses if s.unmet]


def fetch_exempt_dependency_warnings(
    config: Config,
    repo: Repo,
    milestone_number: int | None,
    *,
    file_fetcher: GateAExemptFileFetcher | None = None,
    issue_is_closed: "Callable[[str, int], bool] | None" = None,
    artifact_root: "Path | None" = None,
) -> list[str]:
    """(#3212) Fetch *milestone_number*'s manifest off *repo*'s default
    branch and report every ``exempt:`` entry whose named dependency
    (:class:`ExemptDependency`) is unmet — the "at minimum" bar the issue
    asks for: an exemption naming an issue that hasn't delivered is reported
    loudly rather than silently disarming the gate forever.

    Fail-open like :func:`fetch_gate_a_exempt_warning`: a missing manifest, a
    repo with no acceptance driver, no milestone in hand, or a fetch/parse
    hiccup all return ``[]`` — this is a reporting surface, not a gate, so a
    lookup failure must never itself become a new failure mode. Once a
    dependency IS in hand, though, :func:`verify_exempt_dependency` checks it
    fail-closed, per its own docstring.

    *artifact_root* is normally left ``None`` here (this is a GitHub-API-only
    fetch seam, same as :func:`fetch_gate_a_exempt_warning`) — a caller
    running against a local checkout (``coord acceptance run``/``record``)
    should call :func:`verify_exempt_dependency` directly per-dependency
    instead, passing its own checkout root.
    """
    if milestone_number is None or not config.acceptance.has_driver(repo.name):
        return []

    from coord import github_ops  # noqa: PLC0415

    fetch = file_fetcher or github_ops.get_repo_file
    manifest_data = _fetch_milestone_manifest_data(config, repo, milestone_number, fetch)
    if manifest_data is None or not manifest_data.exempt_deps:
        return []

    statuses = [
        verify_exempt_dependency(
            dep, repo.github, issue_is_closed=issue_is_closed, artifact_root=artifact_root,
        )
        for dep in manifest_data.exempt_deps.values()
    ]
    return unmet_exempt_dependency_warnings(statuses)


# ── #3212 "Related mitigation" — the reviewer never sees the mocks ─────────
#
# docs/ORACLE_LOOP.md's worker briefing contract (oracle_loop_contract_block
# above) points the WORKER at a milestone's Gate-A contract/mocks. The
# reviewer never got the same pointer — issue #3212's own words: "Today the
# reviewer gets the diff, the repo's CLAUDE.md, the generic checklist and the
# issue — but not mocks/index.html, the one artifact that defines what
# 'correct' means for that screen." A reviewer asked "does this match the
# approved screen?" would likely catch a mismatch a reviewer asked "is this
# good code?" has no reason to. This section is the reviewer-facing rendering
# of that same pointer, fetched via the GitHub API (the reviewer briefing has
# no local checkout in hand — see fetch_gate_a_exempt_warning's identical
# GateAExemptFileFetcher shape immediately above).


def oracle_loop_contract_reviewer_note(
    *, contract_path: str, mocks_dir: str, exempt: bool,
) -> str:
    """(#3212) The reviewer-facing rendering of the same pointer
    :func:`oracle_loop_contract_block` gives the worker. Same canonical-
    wording discipline as :func:`gate_a_exempt_warning` /
    :func:`exempt_dependency_warning` — one function renders this text,
    every caller (currently just :func:`fetch_oracle_loop_contract_note`)
    uses it verbatim rather than re-wording it independently.
    """
    lines = [
        "This issue's milestone carries a signed Gate-A design contract. "
        f"Before judging correctness, read `{contract_path}` (the black-box "
        f"surface) and, if present, the rendered mock(s) under `{mocks_dir}/` "
        "— not just the diff, CLAUDE.md and the issue. A diff can pass every "
        "generic check and still contradict the approved screen; only the "
        "contract/mocks say what \"correct\" means here."
    ]
    if exempt:
        lines.append(
            "This issue's OWN acceptance slice is exempted from the "
            "automated gate (see the milestone manifest's `exempt:` list) — "
            "which makes this review the only automated check left before a "
            "human sees it at UAT. Read the contract/mocks with that in "
            "mind."
        )
    return "\n\n".join(lines)


def fetch_oracle_loop_contract_note(
    config: Config,
    repo: Repo,
    milestone_number: int | None,
    issue_number: int,
    *,
    file_fetcher: GateAExemptFileFetcher | None = None,
) -> str | None:
    """(#3212) Fetch *milestone_number*'s manifest off *repo*'s default
    branch and, if *issue_number* has an authored acceptance slice OR is
    named in an ``exempt:`` list, return
    :func:`oracle_loop_contract_reviewer_note` for it — the reviewer's copy
    of the same pointer :func:`oracle_loop_contract_block` gives the worker.

    ``None`` when there's nothing to point at (no milestone in hand, no
    acceptance driver configured, or the issue is neither sliced nor
    exempted in any root's manifest) or on any fetch/parse hiccup —
    fail-open, same as :func:`fetch_gate_a_exempt_warning` /
    :func:`fetch_exempt_dependency_warnings`: this is advisory, never a gate,
    so a lookup failure must never affect whether a review proceeds.

    Tries every :func:`search_roots_for_repo` root in turn — like
    :func:`gate_a_contract_candidates`, which root actually governs a bare
    milestone number isn't knowable ahead of time. Only the legacy
    single-file ``manifest.(yml|yaml|json)`` is checked per root (same scope
    as :func:`_fetch_milestone_manifest_data`, which this deliberately does
    NOT reuse: that helper only tells the caller whether the milestone's
    manifest parsed, not which root it parsed from, and the contract/mocks
    paths below must come from the SAME root the match was found under) —
    an issue whose test mapping lives *only* in a per-issue
    ``manifest.d/<issue>.(yml|json)`` fragment (#2543) and carries no
    ``exempt:`` entry is not detected here. Accepted, matching the identical
    documented scope of the #3202 machinery this sits alongside.

    *file_fetcher* defaults to :func:`coord.github_ops.get_repo_file`;
    inject a stub in tests so this never shells out to a live ``gh``.
    """
    if milestone_number is None or not config.acceptance.has_driver(repo.name):
        return None

    from coord import github_ops  # noqa: PLC0415

    fetch = file_fetcher or github_ops.get_repo_file
    for root in search_roots_for_repo(config, repo.name):
        dirname = root.rstrip("/") or ACCEPTANCE_DIRNAME
        ms_dir = ms_dirname(milestone_number)
        manifest_data: ManifestData | None = None
        for ext in (".yml", ".yaml", ".json"):
            try:
                text = fetch(
                    repo.github, f"{dirname}/{ms_dir}/manifest{ext}", repo.default_branch,
                )
            except Exception:  # noqa: BLE001 — this extension/root doesn't exist
                continue
            try:
                manifest_data = parse_manifest_text(
                    text, source=f"{dirname}/{ms_dir}/manifest{ext}"
                )
            except Exception:  # noqa: BLE001 — malformed manifest: try next root
                manifest_data = None
            break
        if manifest_data is None:
            continue

        exempt = issue_number in manifest_data.exempt
        has_slice = bool(test_ids_for_issue(manifest_data.tests, issue_number))
        if not exempt and not has_slice:
            continue

        return oracle_loop_contract_reviewer_note(
            contract_path=f"{dirname}/{ms_dir}/contract.md",
            mocks_dir=f"{dirname}/{ms_dir}/mocks",
            exempt=exempt,
        )
    return None


def acceptance_capability_gap(
    capability: str, repo_name: str, config: Config,
) -> Machine | None:
    """Detect a capability-matched-routing gap for an acceptance driver run
    (#966, deferred from #932/#944).

    ``coord acceptance run --all`` (Gate C) and ``coord acceptance record``
    always execute the driver's ``run`` command on whatever host invoked
    them — there is no remote-exec plumbing to actually route the run
    elsewhere yet (that's the "new plumbing, not a copy-paste" part #966
    defers until a driver with a real capability mismatch exists). What this
    function *can* do cheaply — mirroring ``coord.smoke.pick_smoke_machine``'s
    candidate filter, minus the async/busy-machine bits that don't apply to
    a synchronous command — is detect when it's about to run on the *wrong*
    hardware, so the caller can fail loudly instead of silently.

    Returns the first other configured machine that has *repo_name* and
    *capability*, when:
    - *capability* is set (drivers without one, e.g. today's only real
      driver's implicit local-only assumption, are never gapped), AND
    - this host is a recognized machine in ``coordinator.yml`` that does
      NOT have *capability* (an unrecognized host is given the benefit of
      the doubt — it might be a dev machine outside the fleet that happens
      to have everything installed), AND
    - some other configured machine actually has it (nothing to route to
      otherwise, so failing wouldn't be actionable).

    Returns ``None`` in every other case — i.e. "no known gap, proceed."
    """
    if not capability:
        return None

    from coord.test_orchestrator import local_machine  # noqa: PLC0415 — avoid import cycle

    here = local_machine(config)
    if here is None or capability in here.capabilities:
        return None

    candidates = [
        m for m in config.machines
        if m.can_work_on(repo_name) and capability in m.capabilities
    ]
    if not candidates:
        return None
    return candidates[0]


def _is_content_line(line: str) -> bool:
    """True when *line*, with any ``#`` comment stripped, still has
    non-whitespace content — i.e. it's real YAML, not blank/comment-only."""
    return bool(line.split("#", 1)[0].strip())


_EXPECTED_RED_KEY_RE = re.compile(r"^expected_red\s*:\s*(#.*)?$")


def _issue_header_re(issue_number: int) -> re.Pattern[str]:
    """Matches *issue_number*'s block-style ``554:`` (or ``"554":``) header
    line, on a line of its own, with its test-id list on the lines that
    follow — the documented/example ``expected_red:`` shape everywhere in
    this codebase (see ``tests/acceptance/ms-33/manifest.yml``). A
    flow-style single-line entry (``554: [a, b]``) does NOT match this
    regex; :func:`clear_expected_red_entries` silently no-ops for it (its
    own "nothing changed" contract) rather than clearing anything — low
    risk since nothing else in this module authors or expects flow style,
    but worth knowing if a manifest is ever hand-edited into that shape.
    """
    return re.compile(rf"^(\s*)['\"]?{issue_number}['\"]?\s*:\s*(#.*)?$")


def _parse_list_item_scalar(line: str) -> str | None:
    """Resolve one YAML block-sequence item line (``- ...``) to its parsed
    scalar value — the same value :func:`parse_manifest_text` would produce
    for it — rather than the raw text as written. #2296: a Playwright test id
    always starts with ``[chromium]``, so its manifest entry is *always*
    quoted (an unquoted leading ``[`` is a YAML flow sequence, not a string);
    comparing the raw scalar against the parsed id then never matches. Going
    through :mod:`yaml` here instead handles double- and single-quoted
    scalars (incl. the ``''`` embedded-quote escape) and, since YAML's own
    comment rule applies, a ``#`` inside a quoted scalar is correctly treated
    as data rather than truncated as a comment.

    Returns ``None`` for anything that isn't a well-formed single-item
    sequence line (blank/comment-only, flow-style, malformed YAML, or a
    non-string value) so the caller leaves such lines untouched rather than
    risk misclassifying them.
    """
    stripped = line.strip()
    if not stripped.startswith("-"):
        return None
    try:
        parsed = yaml.safe_load(stripped)
    except yaml.YAMLError:
        return None
    if not isinstance(parsed, list) or len(parsed) != 1:
        return None
    value = parsed[0]
    return value if isinstance(value, str) else None


def _strip_cleared_ids_from_issue_block(
    body: "list[str]", issue_number: int, cleared_ids: "set[str]",
) -> "tuple[list[str], bool]":
    """Within one ``expected_red:`` block's lines (*body*), drop any list
    item under *issue_number* whose id is in *cleared_ids*; drop the whole
    ``<issue_number>:`` sub-block (header + remaining lines, including any
    now-orphaned comments) if nothing but comments/blanks are left under it.

    Returns ``(new_body, changed)``.
    """
    header_re = _issue_header_re(issue_number)
    out: list[str] = []
    changed = False
    i, n = 0, len(body)
    while i < n:
        line = body[i]
        m = header_re.match(line)
        if not m:
            out.append(line)
            i += 1
            continue
        header_indent = len(m.group(1))
        i += 1
        sub: list[str] = []
        while i < n:
            sub_line = body[i]
            sub_indent = len(sub_line) - len(sub_line.lstrip(" "))
            if sub_line.strip() and sub_indent <= header_indent:
                break
            sub.append(sub_line)
            i += 1
        new_sub = []
        for sub_line in sub:
            item = _parse_list_item_scalar(sub_line)
            if item is not None and item in cleared_ids:
                changed = True
                continue
            new_sub.append(sub_line)
        if any(_is_content_line(sub_line) for sub_line in new_sub):
            out.append(line)
            out.extend(new_sub)
        else:
            # Nothing but comments/blanks left under this issue — drop the
            # header too rather than leave a dangling `NNN:` with no items.
            changed = True
    return out, changed


def clear_expected_red_entries(
    text: str, issue_number: int, cleared_test_ids: "set[str]",
) -> str | None:
    """Pure text-surgery (#2164): remove *cleared_test_ids* from
    *issue_number*'s list under the ``expected_red:`` block of a
    ``manifest.yml``'s raw *text*, preserving every other line — including
    comments — byte-for-byte.

    Used by :func:`clear_expected_red_via_pr` — the coordinator's post-merge
    clearing sweep (never a worker, and never before the fix that made these
    ids green has actually landed on the default branch — see that
    function's docstring for why record-time was the wrong moment) — the
    manifest carries hand-written commentary (see
    ``tests/acceptance/ms-33/manifest.yml``) that a parse-and-
    ``yaml.safe_dump`` round-trip would destroy, so this edits the text
    directly instead of going through :mod:`yaml`.

    Returns the updated text, or ``None`` when nothing changed (no matching
    id was found under *issue_number* — the caller should skip committing a
    no-op). If clearing empties an issue's whole list, that issue's
    sub-block (header + any orphaned comments) is dropped; if that empties
    the whole ``expected_red:`` block, the key itself is dropped too.
    """
    if not cleared_test_ids:
        return None

    lines = text.splitlines(keepends=True)
    out: list[str] = []
    i, n = 0, len(lines)
    changed = False
    while i < n:
        line = lines[i]
        if _EXPECTED_RED_KEY_RE.match(line.strip()):
            i += 1
            body: list[str] = []
            while i < n:
                body_line = lines[i]
                indent = len(body_line) - len(body_line.lstrip(" "))
                if body_line.strip() and indent == 0:
                    break
                body.append(body_line)
                i += 1
            new_body, body_changed = _strip_cleared_ids_from_issue_block(
                body, issue_number, cleared_test_ids,
            )
            if body_changed:
                changed = True
            if any(_is_content_line(bl) for bl in new_body):
                out.append(line)
                out.extend(new_body)
            else:
                # Whole registry is now empty — drop the key too.
                changed = True
        else:
            out.append(line)
            i += 1

    if not changed:
        return None
    return "".join(out)


# ── #2164: post-merge, API-only clearing sweep ───────────────────────────
#
# The first cut of this feature cleared `expected_red` from inside `coord
# acceptance record` via a raw `git push origin HEAD:{default_branch}`. A
# review caught two problems with that: (1) `record` runs at the trust-gate
# step, which can be steps (Test/Review/the actual merge) before the fix
# has landed on the default branch at all — clearing that early reopens the
# exact "red default branch" failure mode #2164 exists to prevent, just
# relocated in time; (2) a raw push straight to the default branch is
# rejected outright by any repo with branch protection (this one included
# — see CLAUDE.md's "a plain `git push origin main` is rejected, even for
# admins").
#
# The fix for both: never touch git directly, and never fire until the fix
# has actually merged. `coord.merge_queue.process` calls
# `clear_expected_red_via_pr` right after `gh_ops.merge_pr` succeeds for a
# `type="work"` entry whose acceptance was recorded "passed" against the
# exact SHA that just merged — i.e. after the ordering event the first cut
# skipped. The mutation itself goes through a real PR
# (`github_ops.create_pr` + `github_ops.merge_pr`), the same protected path
# every other change to the default branch takes — and, since the merge
# queue has no local checkout at all (see `coord.merge_queue.process`'s
# docstring), everything here is pure GitHub-API calls, no `git` subprocess.


def _default_github_ops():
    from coord import github_ops  # noqa: PLC0415

    return github_ops


def _fetch_manifest_source_via_api(
    repo_github: str,
    branch: str,
    dir_path: str,
    filename_stem: str,
    get_file: Callable[..., "tuple[str, str]"],
) -> "tuple[str, str, str, ManifestData] | None":
    """One manifest source file — ``<dir_path>/<filename_stem>.(yml|yaml|
    json)`` — via *get_file* (a ``get_repo_file_with_sha``-shaped callable),
    trying each extension in turn like :func:`_manifest_paths` does on local
    disk. Returns ``(path, text, blob_sha, data)`` for the first extension
    that exists and parses, or ``None`` if none does / the one that exists is
    malformed.

    Shared primitive (#2543) behind both the legacy single-file fetch
    (:func:`_fetch_ms_manifest_via_api`, ``filename_stem="manifest"``) and a
    per-issue fragment fetch (:func:`_fetch_ms_manifest_fragment_via_api`,
    ``filename_stem=str(issue_number)`` under ``<ms_dir>/manifest.d/``).
    """
    for ext in (".yml", ".yaml", ".json"):
        path = f"{dir_path}/{filename_stem}{ext}"
        try:
            text, blob_sha = get_file(repo_github, path, branch)
        except Exception:  # noqa: BLE001 — this extension doesn't exist, or a transient gh hiccup
            continue
        try:
            data = parse_manifest_text(text, source=path)
        except ManifestError:
            return None
        return path, text, blob_sha, data
    return None


def _fetch_ms_manifest_via_api(
    repo_github: str,
    branch: str,
    ms_dir: str,
    get_file: Callable[..., "tuple[str, str]"],
    acceptance_dir: str = ACCEPTANCE_DIRNAME,
) -> "tuple[str, str, str, ManifestData] | None":
    """One ms-dir's LEGACY single-file ``manifest.(yml|yaml|json)`` via
    *get_file* — see :func:`_fetch_manifest_source_via_api`. Since #2543,
    per-issue data more commonly lives in a ``manifest.d/<issue>.(yml|json)``
    fragment instead (:func:`_fetch_ms_manifest_fragment_via_api`); this
    still covers the milestone-level ``gate_a:``/``exempt:`` blocks (which
    stay in the single shared file by choice, #2543) and any not-yet-migrated
    milestone whose per-issue data is still all in one file.

    #2896: *acceptance_dir* is the search root *ms_dir* sits under —
    defaults to the shared repo-root :data:`ACCEPTANCE_DIRNAME`, but a
    relocated (entrypoint-linked) slice lives under that driver's own
    sibling ``acceptance/`` dir instead, so multi-root callers pass each
    :func:`search_roots_for_repo` candidate in turn."""
    dirname = acceptance_dir.rstrip("/") if acceptance_dir else ACCEPTANCE_DIRNAME
    return _fetch_manifest_source_via_api(
        repo_github, branch, f"{dirname}/{ms_dir}", "manifest", get_file,
    )


def _fetch_ms_manifest_fragment_via_api(
    repo_github: str,
    branch: str,
    ms_dir: str,
    issue_number: int,
    get_file: Callable[..., "tuple[str, str]"],
    acceptance_dir: str = ACCEPTANCE_DIRNAME,
) -> "tuple[str, str, str, ManifestData] | None":
    """*issue_number*'s own ``ms-NN/manifest.d/<issue_number>.(yml|yaml|
    json)`` fragment via *get_file* (#2543) — see
    :func:`_fetch_manifest_source_via_api`. A targeted fetch by exact,
    predictable filename, no directory listing required, since the caller
    already knows the issue number it's looking for.

    *acceptance_dir*: see :func:`_fetch_ms_manifest_via_api` (#2896)."""
    dirname = acceptance_dir.rstrip("/") if acceptance_dir else ACCEPTANCE_DIRNAME
    return _fetch_manifest_source_via_api(
        repo_github,
        branch,
        f"{dirname}/{ms_dir}/{MANIFEST_FRAGMENTS_DIRNAME}",
        str(issue_number),
        get_file,
    )


def find_ms_manifest_for_issue_via_api(
    repo_github: str,
    branch: str,
    issue_number: int,
    *,
    gh_ops: Any = None,
    search_roots: "Sequence[str] | None" = None,
) -> "tuple[str, str, str, ManifestData] | None":
    """API-only (no local checkout) equivalent of :func:`ms_dir_for_issue`:
    search every ``<root>/ms-*/`` on *branch* via the GitHub Contents API
    for the one whose ``tests``/``issues``/``expected_red`` mapping covers
    *issue_number* — checking, per ms-dir, *issue_number*'s own
    ``manifest.d/<issue_number>.(yml|json)`` fragment (#2543) FIRST (a
    targeted, exact-filename fetch), then the legacy single-file
    ``manifest.(yml|yaml|json)``.

    Returns ``(path, text, blob_sha, data)`` for the first match (roots in
    the given order, ms-dirs within a root scanned in sorted-name order for
    determinism), or ``None`` when nothing maps *issue_number* at all.
    *gh_ops* is any object exposing ``list_repo_subdirs``/
    ``get_repo_file_with_sha`` (defaults to :mod:`coord.github_ops`; tests
    inject a stub) — mirrors ``coord.merge_queue.GhOps``'s
    optional-attribute convention: a *gh_ops* that lacks either method (an
    older stub) is treated as "nothing found" rather than raising, since
    this whole sweep is best-effort.

    #2896 review: *search_roots* is every repo-relative directory a
    milestone's slices could live under — pass
    :func:`search_roots_for_repo`'s output when a ``Config``/repo name is
    in hand. It defaults to the legacy repo-root :data:`ACCEPTANCE_DIRNAME`
    alone, which is *wrong for a relocated milestone*: this function is a
    GitHub-API sweep taking only a ``owner/repo`` string, so it cannot look
    the roots up itself, and a caller that omits them silently misses every
    entrypoint-linked driver's slices (ms-65/ms-67 here). A listing failure
    on one root skips to the next rather than abandoning the sweep, keeping
    the single-root fail-soft behaviour identical.
    """
    ops = gh_ops or _default_github_ops()
    list_subdirs = getattr(ops, "list_repo_subdirs", None)
    get_file = getattr(ops, "get_repo_file_with_sha", None)
    if list_subdirs is None or get_file is None:
        return None

    roots = list(search_roots) if search_roots else search_roots_for_repo(None, None)
    for root in roots:
        dirname = root.rstrip("/") or ACCEPTANCE_DIRNAME
        try:
            subdirs = list_subdirs(repo_github, dirname, branch)
        except Exception:  # noqa: BLE001 — best-effort sweep, never raises
            continue

        for name in sorted(subdirs):
            frag = _fetch_ms_manifest_fragment_via_api(
                repo_github, branch, name, issue_number, get_file, dirname,
            )
            if frag is not None:
                _path, _text, _blob_sha, data = frag
                if issue_number in data.expected_red or test_ids_for_issue(
                    data.tests, issue_number
                ):
                    return frag
            found = _fetch_ms_manifest_via_api(
                repo_github, branch, name, get_file, dirname,
            )
            if found is None:
                continue
            _path, _text, _blob_sha, data = found
            if issue_number in data.expected_red or test_ids_for_issue(
                data.tests, issue_number
            ):
                return found
    return None


def missing_expected_red_warning(
    repo_github: str,
    branch: str,
    issue_number: int,
    *,
    gh_ops: Any = None,
    search_roots: "Sequence[str] | None" = None,
) -> str | None:
    """#2191 — the writer/gate seam: is *issue_number* the signature of an
    unwritten ``expected_red`` registry on *branch*'s manifest? That
    signature is checkable without ever running the suite: the manifest
    maps at least one test id to *issue_number* (a slice was authored for
    it) but records ZERO ``expected_red`` entries for it, and the issue is
    still open. #2164 shipped a reader (:func:`apply_expected_red`), a
    clearer (:func:`clear_expected_red_via_pr`) and a lister
    (:func:`list_expected_red_via_api`) for this registry but no writer —
    :data:`coord.test_author.TEST_AUTHOR_SYSTEM_PROMPT` step 4b is the
    writer (the test-author records what it observed FAIL in its step-4
    run). This function is the gate half: it does not trust the prompt was
    followed, so it re-derives "was anything recorded" from the manifest
    itself at slice-PR-open time (:func:`coord.merge_queue.process`'s
    "Open PRs first" loop, gated on ``assignment_type == "test-author"``)
    and flags exactly the case a skipped step 4b produces.

    Returns ``None`` — nothing to flag — when: *branch*'s manifest doesn't
    reference *issue_number* at all (no slice authored yet, out of scope);
    ``expected_red`` already has a non-empty entry for it (the writer did
    its job, or the slice is genuinely all-green and needs none); the issue
    is closed (its slice already served its purpose — flagging it now is
    stale noise, not #2191's live deadlock); or any lookup fails. The last
    case is deliberate fail-open, matching every other best-effort API
    sweep in this module (:func:`find_ms_manifest_for_issue_via_api`,
    :func:`coord.merge_queue._issue_has_expected_red_entries`) — this check
    is advisory (see the "refused or warned" phrasing in #2191's acceptance
    criteria; callers choose which), so an unreachable API must degrade to
    "say nothing," never to blocking a PR the check itself couldn't
    evaluate.

    Otherwise returns a human-readable warning naming the missing ids.

    *search_roots* (#2896 review): forwarded to
    :func:`find_ms_manifest_for_issue_via_api` — without it a relocated
    (entrypoint-linked) milestone's manifest is never found, so this check
    reads as "no slice authored, out of scope" and stays silent on exactly
    the milestones it should be watching.
    """
    ops = gh_ops or _default_github_ops()
    found = find_ms_manifest_for_issue_via_api(
        repo_github, branch, issue_number, gh_ops=ops, search_roots=search_roots,
    )
    if found is None:
        return None
    path, _text, _blob_sha, data = found
    if data.expected_red.get(issue_number):
        return None
    test_ids = test_ids_for_issue(data.tests, issue_number)
    if not test_ids:
        return None
    live_state = getattr(ops, "get_issues_live_state", None)
    if live_state is None:
        return None
    try:
        states = live_state(repo_github, [issue_number])
    except Exception:  # noqa: BLE001 — best-effort, fail open (see docstring)
        return None
    if states.get(issue_number) != "open":
        return None
    return (
        f"#{issue_number}: {path} maps {len(test_ids)} test-id(s) to this "
        f"issue ({', '.join(sorted(test_ids))}) but records no "
        "`expected_red` entries for it — the #2191 signature of an "
        "unwritten registry (the test-author's step 4b never ran, or was "
        "skipped). If this slice is genuinely all-green already, ignore; "
        "otherwise every red id here fails CI with nothing telling "
        "`coord acceptance run --all --ci` that's expected, and `coord "
        "merge` will need `--force-merge` or a hand-edited manifest to "
        "land it."
    )


def list_expected_red_via_api(
    repo_github: str,
    branch: str,
    *,
    gh_ops: Any = None,
    search_roots: "Sequence[str] | None" = None,
) -> "dict[str, dict[int, frozenset[str]]]":
    """Every ``expected_red:`` entry across every ``ms-NN`` manifest on
    *branch*, via the API alone — the read half of the #2164 visibility
    story (acceptance criterion 4: "expected_red entries are visible
    wherever gate state is read, so a long-lived one is not invisible
    debt"). Backs ``coord acceptance expected-red``.

    Returns ``{ms_dir: {issue_number: {test_id, ...}}}`` — only ms-dirs
    with at least one expected_red entry are included. ``{}`` on any
    listing failure or when nothing is expected-red anywhere (best-effort,
    matches the rest of this module's read paths).

    #2543: merges BOTH the legacy single-file ``manifest.(yml|json)`` and
    every ``manifest.d/<issue>.(yml|json)`` fragment for each ms-dir — a
    milestone whose per-issue data has moved to fragments must not go dark
    here just because ``list_repo_dir`` (needed to enumerate the fragments
    dir) is unavailable on *gh_ops*: that case degrades to "legacy file
    only", same fail-open posture as everywhere else in this module, not to
    "nothing found."

    #2896 review: *search_roots* is every repo-relative directory a
    milestone's slices could live under (:func:`search_roots_for_repo`);
    every root is swept and the results unioned per ms-dir NAME, since a
    given milestone lives under exactly one root in practice. Defaulting to
    the legacy repo-root :data:`ACCEPTANCE_DIRNAME` alone is what made this
    listing silently omit every relocated (entrypoint-linked) milestone's
    entries — precisely the "long-lived expected_red entry is invisible
    debt" failure #2164 built this command to prevent.
    """
    ops = gh_ops or _default_github_ops()
    list_subdirs = getattr(ops, "list_repo_subdirs", None)
    get_file = getattr(ops, "get_repo_file_with_sha", None)
    list_dir = getattr(ops, "list_repo_dir", None)
    if list_subdirs is None or get_file is None:
        return {}

    roots = list(search_roots) if search_roots else search_roots_for_repo(None, None)
    out: dict[str, dict[int, frozenset[str]]] = {}
    for root in roots:
        dirname = root.rstrip("/") or ACCEPTANCE_DIRNAME
        try:
            subdirs = list_subdirs(repo_github, dirname, branch)
        except Exception:  # noqa: BLE001
            continue

        for name in sorted(subdirs):
            merged: dict[int, frozenset[str]] = dict(out.get(name, {}))

            found = _fetch_ms_manifest_via_api(
                repo_github, branch, name, get_file, dirname,
            )
            if found is not None:
                _path, _text, _blob_sha, data = found
                for issue, ids in data.expected_red.items():
                    merged[issue] = merged.get(issue, frozenset()) | ids

            if list_dir is not None:
                frag_dir = f"{dirname}/{name}/{MANIFEST_FRAGMENTS_DIRNAME}"
                try:
                    filenames = list_dir(repo_github, frag_dir, branch)
                except Exception:  # noqa: BLE001 — no fragments dir, or a transient hiccup
                    filenames = []
                for filename in filenames:
                    if Path(filename).suffix not in (".yml", ".yaml", ".json"):
                        continue
                    frag_path = f"{frag_dir}/{filename}"
                    try:
                        text, _sha = get_file(repo_github, frag_path, branch)
                    except Exception:  # noqa: BLE001
                        continue
                    try:
                        frag_data = parse_manifest_text(text, source=frag_path)
                    except ManifestError:
                        continue
                    for issue, ids in frag_data.expected_red.items():
                        merged[issue] = merged.get(issue, frozenset()) | ids

            if merged:
                out[name] = merged
    return out


def clear_expected_red_via_pr(
    repo_github: str,
    repo_name: str,
    default_branch: str,
    issue_number: int,
    *,
    gh_ops: Any = None,
    search_roots: "Sequence[str] | None" = None,
) -> str:
    """#2164 trust-gate clearing, corrected: call this ONLY after the fix's
    own PR has actually merged into *default_branch*
    (``coord.merge_queue.process``, right after ``gh_ops.merge_pr``
    succeeds) — never at ``coord acceptance record`` time. See this
    module's "post-merge, API-only clearing sweep" section comment above
    for the failure this replaces.

    Applies the edit through a real PR + ``gh pr merge``
    (``github_ops.create_pr`` / ``github_ops.merge_pr``), so a protected
    default branch accepts it exactly like any other change, instead of a
    raw push such a repo would reject outright.

    Returns a short, human-readable status line — never raises. Every step
    is best-effort/non-fatal by design (bookkeeping layered on top of an
    already-successfully-recorded, already-merged fix): a failure anywhere
    degrades to a ``"warning: ..."`` string the caller can log, not an
    exception that could take down merge-queue processing.

    *search_roots* (#2896 review): forwarded to
    :func:`find_ms_manifest_for_issue_via_api`. Omitting it for a relocated
    (entrypoint-linked) milestone makes this report ``"no expected_red
    entries found for this issue"`` for an issue that has them, so the
    clearing PR is never opened and the entry stays red forever.
    """
    ops = gh_ops or _default_github_ops()
    found = find_ms_manifest_for_issue_via_api(
        repo_github, default_branch, issue_number, gh_ops=ops,
        search_roots=search_roots,
    )
    if found is None:
        return "no expected_red entries found for this issue"
    path, text, blob_sha, data = found
    ids = data.expected_red.get(issue_number, frozenset())
    if not ids:
        return "no expected_red entries for this issue"

    if path.endswith(".json"):
        # #2164 review (non-blocking finding): the text-surgery clearer
        # below only understands block-style YAML. A JSON manifest parses
        # and CI-gates correctly but would silently never get entries
        # cleared this way — say so instead of quietly no-oping.
        return (
            f"warning: {path} is a JSON manifest — automatic expected_red "
            "clearing only supports YAML manifests today; clear "
            f"{', '.join(sorted(ids))} by hand"
        )

    new_text = clear_expected_red_entries(text, issue_number, ids)
    if new_text is None:
        # #2296: ids were passed in and none matched — that's a defect in
        # the text surgery (or a manifest that no longer has this issue's
        # block), not a benign no-op. Prefix `warning:` so it's greppable
        # and so `coord acceptance expected-red --clear` doesn't report a
        # clean sweep over an operation that actually did nothing.
        return "warning: expected_red text unchanged (nothing matched)"

    get_head = getattr(ops, "get_default_branch_head", None)
    create_branch = getattr(ops, "create_remote_branch", None)
    update_file = getattr(ops, "update_repo_file", None)
    create_pr = getattr(ops, "create_pr", None)
    merge_pr = getattr(ops, "merge_pr", None)
    if not all((get_head, create_branch, update_file, create_pr, merge_pr)):
        return "warning: gh_ops does not support the expected_red clearing PR path"

    try:
        base_sha = get_head(repo_github, default_branch)
    except Exception as exc:  # noqa: BLE001
        return f"warning: could not resolve {default_branch} tip: {exc}"

    # #2543: for a manifest.d/<issue>.yml fragment, path.parent is the
    # `manifest.d` directory itself, not the ms-NN dir — go through
    # _ms_dir_for_manifest_path so a fragment-clear derives the SAME
    # branch-name component a legacy-file clear would.
    ms_dir = _ms_dir_for_manifest_path(Path(path)).name if "/" in path else "ms"
    branch_name = f"coord/clear-expected-red-{issue_number}-{ms_dir}"
    # #944-style idempotency: a prior attempt may have already created this
    # branch (e.g. a partial failure on a previous merge-queue tick) — a
    # `False` return just means "already exists", not an error; the write
    # below still targets it either way.
    create_branch(repo_github, branch_name, base_sha)

    message = (
        f"coord acceptance: clear expected_red for {repo_name} #{issue_number} "
        f"({', '.join(sorted(ids))})"
    )
    try:
        update_file(repo_github, path, branch_name, new_text, message, sha=blob_sha)
    except Exception as exc:  # noqa: BLE001
        return f"warning: could not commit expected_red clear: {exc}"

    try:
        pr = create_pr(
            repo_github, base=default_branch, head=branch_name,
            title=f"coord acceptance: clear expected_red for #{issue_number}",
            body=(
                f"Automated (#2164 trust gate): {repo_name} #{issue_number}'s "
                "fix merged and the sealed slice observed green — clearing "
                f"{', '.join(sorted(ids))} from `{path}`'s `expected_red:`.\n\n"
                "No worker edited the sealed suite — this is the "
                "coordinator's own observation, applied through the normal "
                "protected-branch PR path."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return f"warning: could not open expected_red clear PR: {exc}"

    try:
        ok, msg = merge_pr(repo_github, pr["number"], method="squash")
    except Exception as exc:  # noqa: BLE001
        return (
            f"warning: expected_red clear PR #{pr.get('number')} opened but "
            f"could not merge: {exc}"
        )
    if not ok:
        return (
            f"expected_red clear PR #{pr['number']} opened but did not "
            "merge (branch protection / required checks pending?) — will "
            f"retry on the next merge ({msg})"
        )
    return (
        f"cleared expected_red for #{issue_number}: {', '.join(sorted(ids))} "
        f"(PR #{pr['number']})"
    )


ExpectedRedClearStatus = Literal["cleared", "no_op", "pending_retry", "failed"]


def classify_expected_red_clear_result(msg: str) -> ExpectedRedClearStatus:
    """#2266 review (blocking finding 2): the single source of truth for
    interpreting :func:`clear_expected_red_via_pr`'s return string.

    Before this, ``coord.merge_queue._maybe_clear_expected_red`` and
    ``coord.commands.acceptance._clear_stuck_expected_red`` each carried
    their own ``msg.startswith("cleared expected_red")`` check — two
    independent implementations of "did the clear succeed?" that agreed
    today but could silently desync the moment either
    :func:`clear_expected_red_via_pr`'s wording changed (the exact "One
    question, one answer" split-brain the epic #2096 review checklist
    warns about). Both call sites now classify through this function.

    Returns one of four outcomes, not a bare bool — a binary
    cleared/not-cleared collapsed two very different "not cleared" cases
    into one (#2266 review, blocking finding 1):

    * ``"cleared"`` — the PR opened *and* merged; entries are gone.
    * ``"no_op"`` — there was nothing to clear in the first place (no
      manifest found for this issue, or the issue has no ``expected_red``
      entries at all). This is the *common* case for an ordinary
      oracle-loop merge whose issue was never part of a deliberately-red
      slice — it must never read as a failure.
    * ``"pending_retry"`` — the clearing PR opened but did not merge yet
      (branch protection / required checks pending); the caller already
      knows this will retry on the next merge, not a hard failure.
    * ``"failed"`` — a genuine failure: everything ``clear_expected_red_via_pr``'s
      docstring says degrades to a ``"warning: ..."``-prefixed string
      (unresolvable branch tip, a commit/PR-open/PR-merge exception, an
      unsupported ``gh_ops``, a JSON manifest, or unchanged text).
    """
    if msg.startswith("cleared expected_red"):
        return "cleared"
    if msg.startswith("no expected_red entries"):
        return "no_op"
    if "did not merge" in msg and "will retry on the next merge" in msg:
        return "pending_retry"
    return "failed"
