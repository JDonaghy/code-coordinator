"""Milestone dispatch — Phase 1 of #767 (milestone-driven workflow, #769).

Turns Phase 0's pure DAG/frontier (:mod:`coord.milestone_order`) into actual
dispatches: fetch a milestone's tracking-issue context from GitHub, compute
the ready frontier, pick an idle/capable machine for each ready issue, and
dispatch it through the same primitives ``coord assign`` uses
(:func:`coord.dispatch.dispatch` + ``record_dispatched`` + ``post_briefing``)
— no new dispatch mechanism.

Deliberately mechanical / ~zero-Claude-per-decision, matching Phase 0's
design note: machine selection is a plain deterministic filter (idle,
``Machine.can_work_on(repo)``, not routing-paused — the same candidate filter
``coord.reconcile._reassign`` and ``coord.review.pick_reviewer_machine`` use),
not an LLM judgment call like ``coord.brain.propose``.

Three call sites share this module:

- ``coord milestone dispatch`` (``coord/commands/milestone.py``) — the
  ``--next`` single-pick still dispatches directly through
  :func:`plan_dispatch`/:func:`dispatch_entry`; bulk mode instead derives
  drive-queue enqueues from the work order via :func:`plan_queue` (#2335),
  so the DQ-4 tick owns the actual launches.
- The daemon's auto-drain tick (``coord.serve_app._milestone_drain_tick``,
  opt-in via ``coordinator.yml`` ``milestone.auto_dispatch``) — re-runs the
  same fetch → plan → dispatch sequence for milestones registered via a
  non-dry-run ``coord milestone dispatch`` call, so newly-unblocked frontier
  entries dispatch automatically as dependencies complete.
- The daemon's gate tick (``coord.serve_app._milestone_gate_tick``, #1929),
  whose ``work`` gate state IS the drain for a gate-driven milestone (``coord
  milestone drive``, docs/ORACLE_LOOP.md) — same ``plan_dispatch`` +
  ``dispatch_entry`` primitives, so gate policy and dispatch policy can't
  drift apart.
- Tests exercise the pure ``plan_dispatch``/``pick_machine`` functions
  directly with a seeded :class:`~coord.models.Board`, no GitHub or HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Iterable

import httpx

from coord.milestone_order import (
    BlockedNode,
    FrontierEntry,
    WorkOrder,
    WorkOrderError,
    WorkOrderNode,
    parse_work_order,
    ready_frontier,
    validate_milestone_membership,
)
from coord.models import Assignment, Board, Machine, Proposal, Repo

if TYPE_CHECKING:
    from coord.config import AcceptanceDriverConfig, Config

__all__ = [
    "MilestoneDispatchError",
    "MilestoneContext",
    "fetch_milestone_context",
    "GateAFileExists",
    "gate_a_status",
    "ManifestFetch",
    "GateAApprovalFetch",
    "gate_a_signoff",
    "gate_a_signoff_status",
    "OracleReadiness",
    "issue_oracle_ready",
    "pick_machine",
    "MachinePick",
    "NoMachineAvailable",
    "DeferredByOracleLoop",
    "MilestonePlan",
    "plan_dispatch",
    "QueuePlanEntry",
    "plan_queue",
    "DispatchOutcome",
    "dispatch_entry",
    "is_milestone_complete",
]


class MilestoneDispatchError(Exception):
    """A milestone's tracking-issue context could not be fetched or is invalid.

    Covers the same failure modes as ``coord milestone order``'s inline
    error handling (GitHub fetch failure, no milestone on the tracking
    issue, a malformed ``## Work order`` block, or a node that isn't a
    member of the milestone) but as a plain exception rather than
    ``click.echo`` + ``sys.exit`` — so both the CLI and the daemon tick can
    catch it and decide how to report it themselves.
    """


@dataclass(frozen=True)
class MilestoneContext:
    """The fetched + validated inputs to :func:`plan_dispatch`."""

    tracking_issue: int
    milestone_number: int
    work_order: WorkOrder
    terminal_issues: frozenset[int] = field(default_factory=frozenset)
    #: The tracking issue's own GitHub state (``"OPEN"``/``"CLOSED"``).
    #: #1929: the milestone gate machine's Gate D observes "has this shipped"
    #: from here — closing the epic is the observable end of the walk. Comes
    #: free from the ``get_issue`` call this function already makes; defaulted
    #: so every existing construction site (tests included) is unaffected.
    tracking_issue_state: str = "OPEN"


def fetch_milestone_context(repo_cfg: Repo, tracking_issue: int) -> MilestoneContext:
    """Fetch the tracking issue, parse its work order, and resolve terminal state.

    Shared by ``coord milestone order`` and ``coord milestone dispatch`` (and
    the daemon's auto-drain tick) so all three compute the frontier from
    identical inputs. Raises :class:`MilestoneDispatchError` on any fetch,
    parse, or membership-validation failure.
    """
    from coord import github_ops  # noqa: PLC0415

    try:
        issue_data = github_ops.get_issue(repo_cfg.github, tracking_issue)
    except RuntimeError as e:
        raise MilestoneDispatchError(f"could not fetch #{tracking_issue}: {e}") from e

    milestone = issue_data.get("milestone") or {}
    milestone_number = milestone.get("number")
    if milestone_number is None:
        raise MilestoneDispatchError(f"#{tracking_issue} has no milestone")

    body = issue_data.get("body") or ""
    try:
        work_order = parse_work_order(body)
    except WorkOrderError as e:
        raise MilestoneDispatchError(str(e)) from e

    tracking_issue_state = str(issue_data.get("state") or "OPEN").upper()

    if not work_order.nodes:
        return MilestoneContext(
            tracking_issue=tracking_issue,
            milestone_number=milestone_number,
            work_order=work_order,
            terminal_issues=frozenset(),
            tracking_issue_state=tracking_issue_state,
        )

    # Membership + terminal state — mirrors coord/commands/milestone.py's
    # original inline logic (Phase 0): issues currently open under the
    # milestone come free from one `get_open_issues` call; anything a node
    # references that isn't in that set gets an individual lookup (closed,
    # or foreign).
    open_issues = github_ops.get_open_issues(repo_cfg.github)
    milestone_issue_numbers = {
        i["number"]
        for i in open_issues
        if (i.get("milestone") or {}).get("number") == milestone_number
    }
    terminal_issues: set[int] = set()
    for node in work_order.nodes:
        if node.issue_number in milestone_issue_numbers:
            continue
        try:
            node_data = github_ops.get_issue(repo_cfg.github, node.issue_number)
        except RuntimeError as e:
            raise MilestoneDispatchError(
                f"could not fetch #{node.issue_number}: {e}"
            ) from e
        node_milestone_number = (node_data.get("milestone") or {}).get("number")
        if node_milestone_number == milestone_number:
            milestone_issue_numbers.add(node.issue_number)
        if node_data.get("state", "").upper() == "CLOSED":
            terminal_issues.add(node.issue_number)

    try:
        validate_milestone_membership(work_order, milestone_issue_numbers)
    except WorkOrderError as e:
        raise MilestoneDispatchError(str(e)) from e

    return MilestoneContext(
        tracking_issue=tracking_issue,
        milestone_number=milestone_number,
        work_order=work_order,
        terminal_issues=frozenset(terminal_issues),
        tracking_issue_state=tracking_issue_state,
    )


def is_milestone_complete(ctx: MilestoneContext) -> bool:
    """Whether every node in the work order has reached a terminal state."""
    return all(
        n.issue_number in ctx.terminal_issues for n in ctx.work_order.nodes
    )


# (repo_github, path, branch) -> True if the file exists at that ref.
# Injected so tests never hit `gh` — mirrors ``coord.claim``'s BranchLookup.
GateAFileExists = Callable[[str, str, str], bool]


def _default_gate_a_file_exists(repo_github: str, path: str, branch: str) -> bool:
    from coord import github_ops  # noqa: PLC0415

    try:
        github_ops.get_repo_file(repo_github, path, branch=branch)
        return True
    except RuntimeError:
        return False


def gate_a_status(
    repo_cfg: Repo,
    config: "Config",
    milestone_number: int,
    *,
    file_exists: GateAFileExists | None = None,
) -> str | None:
    """Gate A (docs/ORACLE_LOOP.md, #930): a milestone's issues may not
    dispatch until its black-box contract exists.

    Returns ``None`` when dispatch may proceed — either the repo has no
    ``acceptance.drivers`` entry configured (Gate A is an oracle-loop
    concept; repos outside that model dispatch exactly as before #930), or
    the contract file already exists on the repo's default branch. Returns a
    human-readable block reason otherwise, naming the missing path and the
    command that produces it.

    #2896: a bare *milestone_number* doesn't say which acceptance search
    root its contract lives under (the shared repo-root tree, or an
    entrypoint-linked driver's own relocated sibling dir) — this tries
    every candidate :func:`coord.acceptance.gate_a_contract_candidates`
    returns for this repo and passes as soon as any one exists, rather than
    checking only the legacy repo-root path and reporting a false "Gate A
    not satisfied" for a milestone whose contract already exists, just
    somewhere else.
    """
    if not config.acceptance.has_driver(repo_cfg.name):
        return None

    from coord.acceptance import gate_a_contract_candidates  # noqa: PLC0415

    candidates = gate_a_contract_candidates(config, repo_cfg.name, milestone_number)
    check = file_exists or _default_gate_a_file_exists
    for path in candidates:
        if check(repo_cfg.github, path, repo_cfg.default_branch):
            return None
    named = " or ".join(repr(p) for p in candidates)
    return (
        f"Gate A not satisfied: {named} does not exist yet on "
        f"{repo_cfg.default_branch!r}. Run `coord acceptance mock {repo_cfg.name} "
        "<tracking_issue>` (docs/ORACLE_LOOP.md) to render the mock + write "
        "the contract before dispatching this milestone's issues."
    )


# (repo_github, path, branch) -> file content, or None if it doesn't exist.
# Injected so tests never hit `gh` — mirrors GateAFileExists above.
ManifestFetch = Callable[[str, str, str], "str | None"]


def _default_fetch_repo_file(repo_github: str, path: str, branch: str) -> str | None:
    from coord import github_ops  # noqa: PLC0415

    try:
        return github_ops.get_repo_file(repo_github, path, branch=branch)
    except RuntimeError:
        return None


def _fetch_manifest_data(
    repo_github: str,
    milestone_number: int,
    branch: str,
    fetch: ManifestFetch,
    *,
    issue_number: int | None = None,
    roots: Iterable[str] | None = None,
):
    """*milestone_number*'s manifest data via *fetch* (a raw-content GitHub
    fetch, no local checkout, no directory listing) — the legacy single-file
    ``manifest.(yml|yaml|json)`` (milestone-level ``gate_a:``/``exempt:``
    blocks live here by choice, #2543), merged with *issue_number*'s own
    ``manifest.d/<issue_number>.(yml|yaml|json)`` fragment
    (:data:`coord.acceptance.MANIFEST_FRAGMENTS_DIRNAME`) when *issue_number*
    is given.

    #2543 review: before this, only the legacy file was ever fetched here,
    so an issue whose JIT slice was authored under the new per-issue-
    fragment convention (``coord.test_author``'s briefing writes ONLY to
    the fragment, never the shared file) would read back
    ``test_ids_for_issue(...) == set()`` forever — the #1138 hard gate
    (:func:`issue_oracle_ready`) would refuse Work dispatch with "no
    acceptance slice yet" even after the slice landed, and
    :func:`coord.drive.AcceptanceGateChecker.has_authored_slice` (which
    reads the same ``OracleReadiness.has_slice``) would keep believing no
    slice was ever authored, re-dispatching a redundant ``test-author``
    session — precisely the concurrent-JIT-authoring collision #2543 exists
    to prevent, triggered by the fix itself. Fetching + merging the
    fragment closes that gap. Callers that only need the milestone-level
    blocks (:func:`gate_a_signoff_status`, which has no single issue in
    scope) pass no *issue_number* and get exactly the legacy-only reads
    this always did.

    *roots* (#2896) is every candidate acceptance-tree root to try, in
    order — normally ``config.acceptance.acceptance_search_roots(repo_name)``
    from the caller, since a bare *milestone_number* doesn't say whether
    it's a directory-discovered driver's slice (shared repo-root tree) or an
    entrypoint-linked driver's relocated one (that driver's own sibling
    dir). Defaults to the legacy single repo-root candidate when omitted,
    unchanged from before #2896. Tries each root until one actually has a
    legacy manifest and/or fragment file; a milestone's data lives under
    exactly one, so the first root that produces anything wins.
    """
    from coord.acceptance import (  # noqa: PLC0415
        ACCEPTANCE_DIRNAME,
        MANIFEST_FRAGMENTS_DIRNAME,
        ManifestData,
        ManifestError,
        merge_manifest_data,
        ms_dirname,
        parse_manifest_text,
    )

    def _fetch_one(dir_path: str, filename_stem: str) -> "tuple[ManifestData, bool]":
        for ext in (".yml", ".yaml", ".json"):
            path = f"{dir_path}/{filename_stem}{ext}"
            content = fetch(repo_github, path, branch)
            if content is None:
                continue
            try:
                return parse_manifest_text(content, source=path), True
            except ManifestError:
                # Malformed manifest degrades to "no slice authored" rather
                # than crashing dispatch — same fail-soft posture as
                # oracle_loop_contract_block (#945).
                return ManifestData(), True
        return ManifestData(), False

    ms_dir = ms_dirname(milestone_number)
    search_roots = [r.rstrip("/") for r in roots] if roots else [ACCEPTANCE_DIRNAME]

    legacy = ManifestData()
    fragment = ManifestData()
    for root_dir in search_roots:
        legacy, legacy_found = _fetch_one(f"{root_dir}/{ms_dir}", "manifest")
        if issue_number is None:
            if legacy_found:
                return legacy
            continue
        fragment, fragment_found = _fetch_one(
            f"{root_dir}/{ms_dir}/{MANIFEST_FRAGMENTS_DIRNAME}",
            str(issue_number),
        )
        if legacy_found or fragment_found:
            return merge_manifest_data(legacy, fragment)

    # No root produced anything — degrade to empty data, same as the old
    # single-root "not found" behaviour.
    return legacy if issue_number is None else merge_manifest_data(legacy, fragment)


def _unsupported_driver_kinds(entry: "AcceptanceDriverConfig") -> tuple[str, ...]:
    """Every driver ``kind`` *entry* declares (its own flat ``kind``, or —
    for a routed entry (#1125) — every ``routes[].kind``) that isn't in
    :data:`coord.acceptance_drivers.SUPPORTED_KINDS` yet.

    This is the "live check against the currently-installed coord package"
    #1138 asks for: a repo can declare a driver kind in ``coordinator.yml``
    ahead of the code that implements it (exactly what happened with
    ``cli-pytest``/#1125 landing 48 minutes after v0.4.68 shipped) and
    nothing previously noticed before dispatching an issue into it.
    """
    from coord.acceptance_drivers import SUPPORTED_KINDS  # noqa: PLC0415

    kinds = {route.kind for route in entry.routes} if entry.routes else {entry.kind}
    return tuple(sorted(k for k in kinds if k and k not in SUPPORTED_KINDS))


# (repo_name, milestone_number) -> the stored Gate-A verdict dict, or None.
# Injected so tests never touch the board DB — mirrors ManifestFetch above.
GateAApprovalFetch = Callable[[str, int], "dict | None"]


def _default_fetch_gate_a_approval(repo_name: str, milestone_number: int):
    from coord import state  # noqa: PLC0415

    try:
        return state.get_gate_a_approval(
            repo_name=repo_name, milestone_number=milestone_number
        )
    except Exception:  # noqa: BLE001
        # Fail CLOSED: an unreadable board collapses to "no verdict
        # recorded", which refuses with an operator-readable remedy rather
        # than letting work dispatch against an unreviewed surface (#2063).
        return None


def gate_a_signoff(
    repo_cfg: Repo,
    config: "Config",
    milestone_number: int,
    manifest,
    *,
    fetch: ManifestFetch,
    approval_fetch: GateAApprovalFetch,
):
    """The #2063 human sign-off half of Gate A, as a
    :class:`coord.gate_a.GateADecision`.

    Gate A has always had two halves that #2063 finally separates: *does the
    contract exist* (:func:`gate_a_status`, #930) and *has a human read it*.
    The second was a convention — "merging the Gate-A PR is sign-off" — which
    anything able to merge a PR satisfied, silently, on CI green. This
    fetches the contract's **current content**, hashes it, and asks whether a
    recorded verdict covers exactly that content.

    Deliberately consumed here rather than at the merge: the Gate-A PR is
    merged with ``gh pr merge``, outside coord entirely, so no coord-side
    check ever sees it. Refusing where the contract is *consumed* means an
    unapproved contract merging is harmless — nothing is authored and no
    work dispatches until a human records a verdict.

    *config* (#2896) resolves which of this repo's acceptance search roots
    (:func:`coord.acceptance.gate_a_contract_candidates`) the contract
    actually lives under — same reason :func:`gate_a_status` needs it: a
    bare *milestone_number* doesn't say whether it's a directory-discovered
    driver's slice (shared repo-root tree) or an entrypoint-linked driver's
    relocated one. Tries each candidate and hashes whichever one actually
    has content; when none do, ``contract_text`` is ``None`` (the first
    candidate's path stands in for messaging, matching pre-#2896 behaviour
    for the common single-candidate case).
    """
    from coord import gate_a as gate_a_mod  # noqa: PLC0415
    from coord.acceptance import gate_a_contract_candidates  # noqa: PLC0415

    contract_text: str | None = None
    for path in gate_a_contract_candidates(config, repo_cfg.name, milestone_number):
        contract_text = fetch(repo_cfg.github, path, repo_cfg.default_branch)
        if contract_text is not None:
            break
    return gate_a_mod.evaluate(
        repo_name=repo_cfg.name,
        milestone_number=milestone_number,
        contract_text=contract_text,
        approval=approval_fetch(repo_cfg.name, milestone_number),
        exempt=bool(getattr(manifest, "gate_a_exempt", False)),
        exempt_reason=str(getattr(manifest, "gate_a_exempt_reason", "") or ""),
    )


def gate_a_signoff_status(
    repo_cfg: Repo,
    config: "Config",
    milestone_number: int,
    *,
    fetch_manifest: ManifestFetch | None = None,
    fetch_gate_a_approval: GateAApprovalFetch | None = None,
) -> str | None:
    """``None`` when a human has signed off on this milestone's contract,
    a refusal reason otherwise (#2063).

    The milestone-level convenience wrapper around :func:`gate_a_signoff`,
    shaped like :func:`gate_a_status` so callers that gate on a *milestone*
    rather than an issue — ``coord acceptance author``, which dispatches the
    independent ``test-author`` — read the same one-line refusal. That path
    is the one that actually burned money on coord-portal ms-2: a sealed
    slice authored against a contract nobody had approved. It happened to be
    a good contract; that was luck, not process.

    A no-op (``None``) for repos with no ``acceptance.drivers`` entry, and
    for a milestone whose contract does not exist yet — the latter is
    :func:`gate_a_status`'s own, already-surfaced refusal.
    """
    if not config.acceptance.has_driver(repo_cfg.name):
        return None

    # One memoised fetch seam for both the existence probe and the content
    # read — `gate_a_status` only wants "is it there", `gate_a_signoff`
    # wants the bytes, and without this the contract would be pulled twice
    # over `gh` on every call.
    base = fetch_manifest or _default_fetch_repo_file
    seen: dict[tuple[str, str, str], "str | None"] = {}

    def fetch(repo_github: str, path: str, branch: str) -> "str | None":
        key = (repo_github, path, branch)
        if key not in seen:
            seen[key] = base(repo_github, path, branch)
        return seen[key]

    if gate_a_status(
        repo_cfg,
        config,
        milestone_number,
        file_exists=lambda g, p, b: fetch(g, p, b) is not None,
    ) is not None:
        return None
    manifest = _fetch_manifest_data(
        repo_cfg.github, milestone_number, repo_cfg.default_branch, fetch,
        roots=config.acceptance.acceptance_search_roots(repo_cfg.name),
    )
    decision = gate_a_signoff(
        repo_cfg,
        config,
        milestone_number,
        manifest,
        fetch=fetch,
        approval_fetch=fetch_gate_a_approval or _default_fetch_gate_a_approval,
    )
    return None if decision.ok else decision.reason


@dataclass(frozen=True)
class OracleReadiness:
    """Issue-level oracle-loop dispatch readiness (#1138), layered on top of
    the milestone-level :func:`gate_a_status`.

    ``applies`` is ``False`` — a no-op, dispatch proceeds exactly as before
    #1138 — for every issue outside this gate's scope: no milestone, no
    ``acceptance.drivers`` entry configured for the repo, or Gate A itself
    not yet satisfied for this milestone (that's a distinct, already-
    surfaced refusal — see :func:`gate_a_status` — firing a confusing
    "no slice yet" message before the contract even exists would be worse,
    not better). When ``applies`` is ``True``, ``reason`` is ``None`` iff
    dispatch may proceed: either the issue is ``exempt``, or it has an
    authored slice (``has_slice``) AND every driver kind its repo declares
    is implemented by this install (``unsupported_kinds`` empty).
    """

    applies: bool = False
    exempt: bool = False
    has_slice: bool = False
    unsupported_kinds: tuple[str, ...] = ()
    reason: str | None = None
    #: #2063: the Gate-A human sign-off state for this issue's milestone —
    #: one of :mod:`coord.gate_a`'s ``STATE_*`` values, or ``""`` when this
    #: gate doesn't apply. ``"approved"``/``"exempt"`` are the only values
    #: that let ``reason`` be ``None``.
    gate_a_state: str = ""


def issue_oracle_ready(
    repo_cfg: Repo,
    config: "Config",
    milestone_number: int | None,
    issue_number: int,
    issue_labels: Iterable[str] = (),
    *,
    file_exists: GateAFileExists | None = None,
    fetch_manifest: ManifestFetch | None = None,
    fetch_gate_a_approval: GateAApprovalFetch | None = None,
) -> OracleReadiness:
    """The #1138 hard gate: refuse Work dispatch for an issue that belongs
    to an oracle-opted-in milestone (Gate A satisfied — ``contract.md``
    exists) but has no JIT-authored acceptance slice yet, or whose repo
    declares an acceptance driver ``kind`` this ``coord`` install doesn't
    implement — the exact gap that let #1118 dispatch and merge through the
    ordinary Work→Test→Review→Merge pipeline despite ms-37's Gate A already
    being satisfied (2026-07-13 incident, see #1138).

    Scenarios (b) non-opted-in milestones/epics and (c) plain issues with no
    milestone are unaffected: this only activates when
    ``tests/acceptance/ms-N/contract.md`` exists for *issue_number*'s
    milestone — the same signal :func:`gate_a_status` checks, no new config
    surface. An issue may opt out of the sealed suite (e.g. it builds the
    driver rather than consuming it, like #1125) via an explicit
    ``exempt:`` list in the milestone's manifest or an ``oracle:exempt``
    label — a declared, reviewable decision rather than tribal knowledge.

    #2063 adds a second refusal on the same seam: the contract exists but
    carries **no recorded human sign-off** for its current content (see
    :func:`gate_a_signoff`). That refusal's prose carries
    :func:`coord.gate_a.park_marker` so ``coord drive-queue``'s tick parks
    the entry (re-checked every tick) instead of landing it in terminal
    ``blocked``, which nothing re-evaluates (#2040) — this is an explicitly
    operator-fixable condition with a one-command remedy.
    """
    if milestone_number is None or not config.acceptance.has_driver(repo_cfg.name):
        return OracleReadiness()

    # One memoised fetch seam shared by `gate_a_status`'s existence probe and
    # the manifest/contract content reads below — same fix
    # `gate_a_signoff_status` already applies for its own pair of calls.
    # Without this, every dispatch-readiness check against an oracle-opted
    # milestone pulls `tests/acceptance/ms-NN/contract.md` over `gh` TWICE:
    # once here (existence only, content discarded) and again inside
    # `gate_a_signoff` below (content, to hash it). Only takes effect when
    # *file_exists* isn't explicitly overridden — callers that inject their
    # own (tests) keep exactly their prior behaviour.
    base_fetch = fetch_manifest or _default_fetch_repo_file
    _fetch_seen: dict[tuple[str, str, str], "str | None"] = {}

    def fetch(repo_github: str, path: str, branch: str) -> "str | None":
        key = (repo_github, path, branch)
        if key not in _fetch_seen:
            _fetch_seen[key] = base_fetch(repo_github, path, branch)
        return _fetch_seen[key]

    effective_file_exists = file_exists or (
        lambda g, p, b: fetch(g, p, b) is not None
    )

    if gate_a_status(
        repo_cfg, config, milestone_number, file_exists=effective_file_exists
    ) is not None:
        return OracleReadiness()

    from coord.acceptance import test_ids_for_issue  # noqa: PLC0415
    from coord.acceptance_drivers import SUPPORTED_KINDS  # noqa: PLC0415

    manifest = _fetch_manifest_data(
        repo_cfg.github, milestone_number, repo_cfg.default_branch, fetch,
        issue_number=issue_number,
        roots=config.acceptance.acceptance_search_roots(repo_cfg.name),
    )
    has_slice = bool(test_ids_for_issue(manifest.tests, issue_number))
    exempt = issue_number in manifest.exempt or "oracle:exempt" in set(issue_labels)

    entry = config.acceptance.drivers.get(repo_cfg.name)
    unsupported = _unsupported_driver_kinds(entry) if entry is not None else ()

    # #2063: the human sign-off half of Gate A is checked FIRST, and is not
    # bypassed by the issue-level `exempt` list. `exempt` says "this ISSUE
    # doesn't consume the sealed suite" (e.g. #1125, which builds the
    # driver); it says nothing about whether a human has read the milestone's
    # contract — the surface every sibling issue is about to be built
    # against. The one legitimate opt-out is milestone-level and declared in
    # the manifest (`gate_a: {exempt: true}`), which `gate_a_signoff` honours.
    signoff = gate_a_signoff(
        repo_cfg,
        config,
        milestone_number,
        manifest,
        fetch=fetch,
        approval_fetch=fetch_gate_a_approval or _default_fetch_gate_a_approval,
    )
    if not signoff.ok:
        return OracleReadiness(
            applies=True, exempt=exempt, has_slice=has_slice,
            unsupported_kinds=unsupported, reason=signoff.reason,
            gate_a_state=signoff.state,
        )

    if exempt:
        return OracleReadiness(
            applies=True, exempt=True, has_slice=has_slice,
            unsupported_kinds=unsupported, gate_a_state=signoff.state,
        )

    reason: str | None = None
    if not has_slice:
        reason = (
            f"Issue #{issue_number} is part of oracle-opted-in milestone "
            f"ms-{milestone_number} (Gate A satisfied) but has no acceptance "
            "slice yet — run `coord acceptance author "
            f"{repo_cfg.name} <tracking_issue> --issue {issue_number}` first. "
            "Already covered by its own unit tests instead (e.g. it builds "
            f"the driver rather than consuming it)? Add {issue_number} to "
            f"tests/acceptance/ms-{milestone_number}/manifest.yml's `exempt:` "
            "list, or label the issue `oracle:exempt`."
        )
    elif unsupported:
        reason = (
            f"Issue #{issue_number}'s repo {repo_cfg.name!r} declares "
            f"acceptance driver kind(s) {', '.join(unsupported)} that this "
            f"coord install doesn't implement yet (supported: "
            f"{', '.join(SUPPORTED_KINDS)}) — update coord "
            "(`coord agent update`) before dispatching."
        )
    return OracleReadiness(
        applies=True, exempt=False, has_slice=has_slice,
        unsupported_kinds=unsupported, reason=reason,
        gate_a_state=signoff.state,
    )


def pick_machine(
    repo_name: str,
    board: Board,
    config: "Config",
    *,
    exclude: frozenset[str] = frozenset(),
) -> Machine | None:
    """Deterministically pick an idle, capable, unpaused machine for *repo_name*.

    Mirrors the candidate filter ``coord.reconcile._reassign`` and
    ``coord.review.pick_reviewer_machine`` already use: idle (no running
    assignment on the board), lists *repo_name* in its ``repos:`` (this is
    what keeps coord-self work off a machine like dellserver whose
    ``coordinator.yml`` entry omits ``claude-coordinator`` — #688), has a
    configured ``repo_paths`` entry, and isn't routing-paused
    (``coord pause``). First match wins in ``config.machines`` order — no
    scoring, no LLM.

    Deliberately computes "busy" from ``board.active`` directly (like
    ``_reassign``/``pick_reviewer_machine`` do) rather than via
    ``Board.idle_machines()``, which filters ``board.machines`` — a separate
    DB-synced snapshot that isn't guaranteed to be populated on every board
    read path. ``config.machines`` is the authoritative machine list here.
    """
    from coord.machine_pause import paused_set  # noqa: PLC0415

    busy = {a.machine_name for a in board.active if a.status == "running"}
    paused = paused_set(config.machines)
    for m in config.machines:
        if m.name in exclude:
            continue
        if m.name in busy:
            continue
        if m.name in paused:
            continue
        if not m.can_work_on(repo_name):
            continue
        if m.repo_path(repo_name) is None:
            continue
        return m
    return None


@dataclass(frozen=True)
class MachinePick:
    """A ready-frontier entry paired with the machine it would dispatch to."""

    entry: FrontierEntry
    machine: Machine


@dataclass(frozen=True)
class NoMachineAvailable:
    """A ready-frontier entry with nowhere to dispatch it *right now*.

    Distinct from :class:`~coord.milestone_order.BlockedNode` — the frontier
    itself considers this entry ready (dependencies satisfied, unclaimed,
    unconflicted); it just has no idle capable machine this tick. It will be
    reconsidered on the next ``coord milestone dispatch`` / daemon tick.
    """

    entry: FrontierEntry
    reason: str = "no idle machine available for this repo"


@dataclass(frozen=True)
class DeferredByOracleLoop:
    """A ready-frontier entry withheld *this tick* because the milestone is
    under oracle-loop control and another entry already claimed the tick's
    one dispatch slot (#2542).

    Distinct from both :class:`~coord.milestone_order.BlockedNode` (the
    frontier itself considers this entry ready) and
    :class:`NoMachineAvailable` (there may well be an idle machine for it) —
    it is held back purely so two same-milestone entries never enter their
    JIT-slice-authoring phase concurrently and race on the shared
    ``tests/acceptance/ms-N/manifest.yml``. It will be reconsidered on the
    next tick, by which point the dispatched entry is claimed (an active
    board assignment) and drops out of the ready frontier on its own.
    """

    entry: FrontierEntry
    reason: str = "oracle-loop milestone: only one entry dispatches per tick"


@dataclass(frozen=True)
class MilestonePlan:
    """The result of :func:`plan_dispatch`: what to dispatch now, what's
    idle-machine-starved, what's withheld for oracle-loop serialization, and
    what Phase 0's frontier says is still blocked.
    """

    to_dispatch: tuple[MachinePick, ...] = ()
    skipped: tuple[NoMachineAvailable, ...] = ()
    waiting: tuple[BlockedNode, ...] = ()
    deferred: tuple[DeferredByOracleLoop, ...] = ()


def plan_dispatch(
    work_order: WorkOrder,
    board: Board,
    config: "Config",
    repo_cfg: Repo,
    terminal_issues: frozenset[int] | set[int],
    *,
    oracle_loop: bool = False,
) -> MilestonePlan:
    """Compute the ready frontier and pick a machine for each ready entry.

    Pure — no GitHub/HTTP calls, no dispatch side effects. Greedily assigns
    each :class:`~coord.milestone_order.FrontierEntry` in frontier order to
    the first idle+capable machine not already claimed by an earlier entry
    in *this* call (so a cohort of N ready issues fans out across up to N
    distinct idle machines instead of piling onto one).

    ``oracle_loop`` (#2542) — the caller's already-resolved
    ``config.acceptance.has_driver(repo_cfg.name)`` — caps ``to_dispatch`` at
    **one** entry per call. Without this, two ready-frontier entries with no
    claim/dependency edge between them (the normal, desired fan-out above)
    would both dispatch in the very same tick under an oracle-loop
    milestone, and each independently authors its own JIT acceptance slice
    into the SAME ``tests/acceptance/ms-N/manifest.yml``
    (``coord.test_author``'s "later slices MERGE into this file"
    convention) — a race regardless of what the declared work order says is
    safe to parallelize. This is the same hazard :func:`plan_queue`'s
    ``oracle_loop`` chaining closes for the drive-queue path; this is the
    equivalent for the daemon's direct-dispatch paths
    (``coord.serve_app._milestone_gate_tick``'s ``work`` state and the
    legacy ``_milestone_drain_tick``), which call this function directly
    rather than going through the drive-queue. Entries that would otherwise
    have dispatched this tick are returned in :attr:`MilestonePlan.deferred`
    instead of silently dropped; they re-enter ``ready`` on a later tick
    once the dispatched entry is claimed (an active board assignment) and
    falls out of the frontier on its own — see
    :func:`~coord.milestone_order.ready_frontier`.

    Deliberately coarse, mirroring ``plan_queue``: it withholds the ENTIRE
    entry (not just its slice-authoring phase), a real throughput cost for
    an oracle-loop milestone dispatched via the daemon in bulk. #2543
    (per-issue manifest fragments) is what would let real concurrency come
    back safely; this is defense-in-depth until it lands.
    """
    frontier = ready_frontier(
        work_order,
        board,
        repo_name=repo_cfg.name,
        repo_github=repo_cfg.github,
        terminal_issues=set(terminal_issues),
    )
    picks: list[MachinePick] = []
    skipped: list[NoMachineAvailable] = []
    deferred: list[DeferredByOracleLoop] = []
    used: set[str] = set()
    for entry in frontier.ready:
        if oracle_loop and picks:
            deferred.append(DeferredByOracleLoop(entry))
            continue
        machine = pick_machine(repo_cfg.name, board, config, exclude=frozenset(used))
        if machine is None:
            skipped.append(NoMachineAvailable(entry))
            continue
        used.add(machine.name)
        picks.append(MachinePick(entry, machine))
    return MilestonePlan(
        to_dispatch=tuple(picks),
        skipped=tuple(skipped),
        waiting=frontier.blocked,
        deferred=tuple(deferred),
    )


@dataclass(frozen=True)
class QueuePlanEntry:
    """One drive-queue enqueue derived from a work-order node (#2335)."""

    issue_number: int
    #: Fully-qualified pre-req keys (``"repo#N"``) for the drive-queue's
    #: ``after=`` edge list — the node's declared ``{after: #N}`` targets,
    #: minus any that are already terminal (closed issues never enter the
    #: queue, so an edge at one would read as unsatisfiable to the tick's
    #: ``_resolve_prereqs`` rather than as "already done").
    after: tuple[str, ...] = ()
    group: str | None = None


def plan_queue(
    work_order: WorkOrder,
    terminal_issues: frozenset[int] | set[int],
    repo_name: str,
    *,
    oracle_loop: bool = False,
) -> tuple[QueuePlanEntry, ...]:
    """Translate the work order's DAG into drive-queue enqueues (#2335).

    Pure — no GitHub, no board, no side effects. One entry per non-terminal
    node, in dependency order: a stable topological sort seeded by declared
    order, so independent nodes keep the operator's declared sequence while a
    node declared *before* its own pre-req still queues after it (queue
    position is the tick's tie-break among eligible entries; the ``after``
    edges are what actually gate launches).

    Each entry's ``after`` carries the node's declared ``{after: #N}`` edges
    as fully-qualified ``repo#N`` keys — the same format ``coord drive-queue
    add --after`` parses — filtered to pre-reqs that are still open.
    :func:`~coord.milestone_order.parse_work_order` already refused cycles,
    self-edges, and edges to undeclared nodes, so the sort always terminates;
    the defensive tail below only fires on inputs constructed outside it.

    ``oracle_loop`` (#2542) — the caller's already-resolved
    ``config.acceptance.has_driver(repo_name)`` — additionally chains every
    entry's ``after`` to the issue immediately before it in this function's
    own topological order, so the drive-queue admits the WHOLE milestone
    strictly one entry at a time. Without this, two entries with no declared
    edge between them (different ``group``s, or neither declaring one) are
    both ``waiting`` the moment their own pre-reqs land, and the
    drive-queue's per-repo concurrency cap (``coord.drive_queue.plan_tick``)
    launches both at once — each independently authoring its own JIT
    acceptance slice into the SAME ``tests/acceptance/ms-N/manifest.yml``
    (``coord.test_author``'s "later slices MERGE into this file"
    convention), regardless of what the declared work order says is safe to
    parallelize. This is exactly coord-portal#122's SECOND collision (#130
    vs. #132, filed as #2542's correction) — ``validate_no_shared_oracle_
    group`` only catches the explicit-group case, and this milestone's
    entries shared no group at all.

    Deliberately coarse: it serializes the entry's WHOLE dispatch (Work
    through Merge), not just its slice-authoring phase, because the
    drive-queue has no notion of "authoring" vs. "working" phases to hang a
    narrower edge on. That is a real throughput cost for an oracle-loop
    milestone dispatched in bulk — #2543 (giving each issue its own manifest
    fragment instead of one shared appended file) is what would let real
    concurrency come back safely; this is defense-in-depth until it lands.
    """
    from coord.drive_queue import entry_key  # noqa: PLC0415

    terminal = set(terminal_issues)
    remaining: dict[int, WorkOrderNode] = {
        n.issue_number: n for n in work_order.nodes if n.issue_number not in terminal
    }
    open_set = set(remaining)
    placed: set[int] = set()
    ordered: list[WorkOrderNode] = []
    while remaining:
        progressed = False
        for number, node in list(remaining.items()):
            if any(d in open_set and d not in placed for d in node.after):
                continue
            ordered.append(node)
            placed.add(number)
            del remaining[number]
            progressed = True
        if not progressed:  # cycle — unreachable via parse_work_order; see above
            ordered.extend(remaining.values())
            break

    entries: list[QueuePlanEntry] = []
    previous_issue_number: int | None = None
    for n in ordered:
        after_numbers = list(n.after)
        if oracle_loop and (
            previous_issue_number is not None
            and previous_issue_number not in after_numbers
        ):
            after_numbers.append(previous_issue_number)
        entries.append(
            QueuePlanEntry(
                issue_number=n.issue_number,
                after=tuple(
                    entry_key(repo_name, d) for d in after_numbers if d not in terminal
                ),
                group=n.group,
            )
        )
        previous_issue_number = n.issue_number
    return tuple(entries)


@dataclass(frozen=True)
class DispatchOutcome:
    """The result of one :func:`dispatch_entry` call."""

    issue_number: int
    machine_name: str
    ok: bool
    assignment_id: str | None = None
    error: str | None = None
    # #1454: the resolved model + a human-readable reason (e.g. "via label
    # 'tier:large'" / "default; no label match") — see
    # `coord.config.describe_model_choice`. `None` on a failed outcome (no
    # model was ever resolved) or for a non-"work" proposal_type.
    model: str | None = None
    model_reason: str | None = None
    # #1889: mirrors `model_reason` above, for the effective PROVIDER
    # instead — see `coord.providers.describe_provider_choice`. `None` on a
    # failed outcome (no provider was ever resolved).
    provider_reason: str | None = None


def dispatch_entry(
    pick: MachinePick,
    repo_cfg: Repo,
    config: "Config",
    board: Board,
    *,
    tracking_issue: int | None = None,
) -> DispatchOutcome:
    """Dispatch one ready-frontier entry to its picked machine.

    Mirrors ``coord.commands.dispatch_workers._dispatch_headless``'s logic
    (build a :class:`~coord.models.Proposal` → defensive claim recheck →
    :func:`coord.dispatch.dispatch` → ``record_dispatched`` →
    ``post_briefing``) without its ``click.echo``/``sys.exit`` coupling, so
    it's usable from both ``coord milestone dispatch`` and the daemon's
    auto-drain tick.

    On success, appends a lightweight ``running`` :class:`~coord.models.
    Assignment` stub to *board*'s ``active`` list in place — so a caller
    dispatching several entries (or several milestones) in the same batch
    sees the machine as busy for the *next* :func:`plan_dispatch` /
    :func:`pick_machine` call without re-reading the board over the network.
    This does not itself persist the board; ``record_dispatched`` already
    wrote the real assignment row.

    Re-checks :func:`coord.claim.find_work_claim` immediately before
    dispatching (defense-in-depth against the frontier snapshot going stale
    between planning and dispatch — e.g. a race with a manual `coord
    assign`), matching the same check ``_dispatch_headless`` performs.
    """
    from coord import github_ops  # noqa: PLC0415
    from coord.claim import claim_message, find_work_claim  # noqa: PLC0415
    from coord.dispatch import (  # noqa: PLC0415
        dispatch,
        post_briefing,
        resolve_dispatch_model_alias,
    )
    from coord.providers import resolve_provider_name  # noqa: PLC0415
    from coord.state import record_dispatched  # noqa: PLC0415

    issue_number = pick.entry.issue_number
    machine = pick.machine

    claim = find_work_claim(issue_number, repo_cfg.name, repo_cfg.github, board)
    if claim is not None:
        return DispatchOutcome(
            issue_number=issue_number,
            machine_name=machine.name,
            ok=False,
            error=claim_message(claim),
        )

    try:
        issue_data = github_ops.get_issue(repo_cfg.github, issue_number)
    except RuntimeError as e:
        return DispatchOutcome(
            issue_number=issue_number,
            machine_name=machine.name,
            ok=False,
            error=f"could not fetch #{issue_number}: {e}",
        )
    issue_title = issue_data.get("title", f"Issue #{issue_number}")
    issue_body = issue_data.get("body") or ""
    briefing = f"Issue #{issue_number}: {issue_title}\n\n{issue_body}"
    if tracking_issue is not None:
        group_note = f" (group {pick.entry.group})" if pick.entry.group else ""
        briefing += (
            "\n\n---\nDispatched by `coord milestone dispatch` as part of the "
            f"declared work order in #{tracking_issue}{group_note}."
        )

    issue_labels = [lbl.get("name", "") for lbl in (issue_data.get("labels") or [])]
    required_gates = list(config.pipeline.default_gates)
    for lbl in issue_labels:
        if lbl in config.pipeline.labels:
            required_gates = list(config.pipeline.labels[lbl])
            break

    # #934: this issue's GitHub Milestone number, when it has one —
    # `issue_data` already carries it (`coord.github_ops.get_issue`'s
    # `milestone` field), so no extra fetch is needed. Threaded onto the
    # Proposal so `coord.dispatch.dispatch()` can resolve the worker's base
    # branch via `coord.branch_model.resolve_base_branch` (`feature/ms-NN`
    # for a repo that opted into the git model, `default_branch` otherwise).
    issue_milestone = issue_data.get("milestone") or {}
    milestone_number = issue_milestone.get("number") if isinstance(issue_milestone, dict) else None

    if milestone_number is not None and getattr(repo_cfg, "develop_branch", None):
        from coord.branch_model import ensure_feature_branch_exists  # noqa: PLC0415

        try:
            ensure_feature_branch_exists(repo_cfg, milestone_number)
        except (ValueError, RuntimeError) as e:
            return DispatchOutcome(
                issue_number=issue_number,
                machine_name=machine.name,
                ok=False,
                error=f"could not ensure feature/ms-{milestone_number} exists: {e}",
            )

    # #1430: models.labels routes work dispatches by the issue's tier/type
    # label; plan workers (require_plan=true) deliberately stay on
    # `default` — read-only/cheap, must not inherit a tier:large -> opus
    # routing meant for the eventual work dispatch.
    proposal_type = "plan" if config.dispatch.require_plan else "work"
    label_model, matched_label, shadowed_labels = (
        config.models.model_for_labels_with_reason(issue_labels)
        if proposal_type == "work"
        else (None, None, [])
    )
    # #1889: providers.labels gets the identical type="work"-only gating —
    # this is also the daemon's auto-drain tick, a headless path with no
    # human to type `--provider`, exactly what #1889 exists for.
    provider_issue_labels = issue_labels if proposal_type == "work" else None
    # #1706 review fix: milestone dispatch has no per-call `--provider`
    # override, so the effective provider is spec(None) → label → repo →
    # default — same chain `coord.dispatch.dispatch()` uses. Route model
    # resolution through `resolve_dispatch_model_alias` so a non-claude/
    # claude-pty provider's own pinned `model` isn't shadowed by
    # `models.default`; see that function's docstring for the full
    # rationale. Without this, `resolved_model` was always truthy by the
    # time it reached `Proposal.model`, so `dispatch()`'s own
    # provider-aware fallback could never fire for `coord milestone
    # dispatch`.
    effective_provider_name = resolve_provider_name(
        None, repo_cfg.provider, config.providers,
        issue_labels=provider_issue_labels,
    )
    resolved_model = resolve_dispatch_model_alias(
        explicit_model=None,
        label_model=label_model,
        config=config,
        effective_provider_name=effective_provider_name,
    )
    # #1454: surfaced on the outcome so `coord milestone dispatch`'s CLI
    # output states *why* this model was picked, same as `coord assign` /
    # `coord approve`. `issue_labels` (above, line ~572) came from
    # `issue_data` fetched fresh just moments ago in this same call — so
    # this is never stale, unlike a proposal snapshot carried over from an
    # earlier `coord plan`/`coord milestone plan` run.
    from coord.config import describe_model_choice  # noqa: PLC0415

    if resolved_model:
        model_reason = describe_model_choice(
            resolved_model=resolved_model,
            matched_label=matched_label,
            shadowed_labels=shadowed_labels,
        )
    else:
        # #1706/#1798: resolved_model is None when the effective provider's
        # own `providers.definitions.<name>.model` is pinned — state that
        # explicitly rather than feeding `None` to describe_model_choice
        # (which expects a str). #1798: after the precedence fix, this can
        # now happen *with* a matched label too (the pin winning over it,
        # not just "no label matched") — surface which label lost, same as
        # `_dispatch_headless`'s equivalent branch in dispatch_workers.py,
        # so `coord milestone dispatch`'s output doesn't silently omit it.
        _pinned = config.providers.definitions.get(effective_provider_name)
        if _pinned is not None and _pinned.model:
            _via = f"providers.definitions[{effective_provider_name!r}].model"
            model_reason = (
                f"{_pinned.model} (via {_via}, overriding label {matched_label!r})"
                if matched_label
                else f"{_pinned.model} (via {_via})"
            )
        else:
            model_reason = "none (provider default; no --model support at this call site)"

    # #1889: mirrors the model_reason block above — state which link of the
    # spec(None) → providers.labels → repo → default chain
    # (coord.providers.resolve_provider_name) won, so a providers.labels
    # match (e.g. `harness:opencode`) is legible in `coord milestone
    # dispatch`'s output, not just discoverable via coordinator.yml.
    from coord.providers import describe_provider_choice  # noqa: PLC0415

    provider_reason = describe_provider_choice(
        None, repo_cfg.provider, config.providers,
        issue_labels=provider_issue_labels,
    )

    proposal = Proposal(
        id=0,
        machine_name=machine.name,
        repo_name=repo_cfg.name,
        issue_number=issue_number,
        issue_title=issue_title,
        rationale="milestone work-order dispatch (coord milestone dispatch)",
        briefing=briefing,
        model=resolved_model,
        type=proposal_type,
        required_gates=required_gates,
        milestone_number=milestone_number,
        issue_labels=issue_labels,
    )

    try:
        response = dispatch(proposal, config)
    except (httpx.HTTPError, ValueError) as e:
        return DispatchOutcome(
            issue_number=issue_number, machine_name=machine.name, ok=False, error=str(e)
        )

    assignment_id = response.get("id", "pending")
    record_dispatched(
        assignment_id=assignment_id,
        proposal=proposal,
        repo_github=repo_cfg.github,
        provider_name=response.get("_provider_name"),
    )

    try:
        post_briefing(proposal, config, assignment_id=assignment_id, do_not_touch=())
    except Exception:  # noqa: BLE001 — best-effort, mirrors _dispatch_headless
        pass

    board.active.append(
        Assignment(
            machine_name=machine.name,
            repo_name=repo_cfg.name,
            issue_number=issue_number,
            issue_title=issue_title,
            assignment_id=str(assignment_id),
            status="running",
            type=proposal.type,
        )
    )

    return DispatchOutcome(
        issue_number=issue_number,
        machine_name=machine.name,
        ok=True,
        assignment_id=str(assignment_id),
        model=resolved_model,
        model_reason=model_reason,
        provider_reason=provider_reason,
    )
