"""#930 (docs/ORACLE_LOOP.md, Gate A): dispatch a `type="mock-author"` agent.

Gate A is the milestone's pre-work architecture gate: before any of a
milestone's issues may dispatch, an independent agent — zero shared context
with the eventual workers, mirroring the adversarial code reviewer — renders
a viewable mock of the milestone's user-facing surface and pins the exact
black-box contract (CLI names, key screen text, API field shapes) that both
the workers and the independent test-author (#931) implement/test to.

Used by `coord acceptance mock <repo> <tracking_issue>` (the CLI command in
``coord/commands/acceptance.py``). Deliberately keyed by the milestone's
tracking-issue number, not a `--milestone NN` flag — every sibling milestone
command (`coord milestone order/dispatch/chat/gate-c`) takes
`<repo> <tracking_issue>`, and the milestone number itself is resolved from
the tracking issue, same as `coord/milestone_chat.py` already does. The
on-disk artifact directory is still `tests/acceptance/ms-<milestone_number>/`
per ORACLE_LOOP.md's settled layout.

The mock-author gets a real git worktree + branch (unlike the read-only
`milestone-chat` type) since its whole job is committing files under
`tests/acceptance/ms-NN/` — see `type="mock-author"` handling in
``coord/agent.py`` (``MOCK_AUTHOR_SYSTEM_PROMPT`` / ``WRITE_CAPABLE_SPEC_
TYPES``) and the matching exemption from the acceptance-dir auto-forbid in
``coord/dispatch.py``. It dispatches through the same
Work → Test → Review → Merge pipeline as any other branch (`required_gates`
is the repo's normal `default_gates`) — Gate A produces a normal reviewed
commit, not a special-cased one.

**PDR-3 (#2508):** :func:`collect_mock_bundle_files` and
:func:`build_design_round` are the other end of Gate A's output — read back
off a merged mock-author branch and reshaped into a portal design round by
``coord.merge_queue``'s post-merge hook, so a Gate-A merge auto-pushes a
design round to any milestone with a portal link (`coord portal link`,
PDR-1/#2507) instead of that link sitting unread.
"""
from __future__ import annotations

import fnmatch
import uuid
from typing import Any

import httpx

from coord import github_ops
from coord.acceptance import ms_dirname
from coord.claim import claim_message, claim_remedy_hint, find_work_claim
from coord.config import Config
from coord.milestone_chat import _fetch_milestone_issues
from coord.milestone_dispatch import pick_machine
from coord.models import Machine, Proposal


def _wants_mock_index(driver_mock_glob: str) -> bool:
    """#2512: the navigation-index post-render step only makes sense for
    HTML mocks — an `index.html` full of `<a href>`s to `.screen` text-grid
    dumps (tui-tuidriver) or whatever cli-pytest renders isn't navigable in
    a browser anyway. Gate on the mock glob rather than the driver name so
    this stays correct if a future driver also renders `.html`."""
    return driver_mock_glob.strip().lower().endswith(".html")


def mock_matches_glob(name: str, driver_mock_glob: str) -> bool:
    """Does mock file *name* belong to a bundle collected under
    *driver_mock_glob*? (#3068)

    The single filename test both bundle collectors use — the GitHub-side
    :func:`collect_mock_bundle_files` and the local-checkout
    ``coord.commands.portal._collect_local_mock_bundle_files`` — so an
    on-demand `coord portal publish-mocks` and the merge-triggered auto-push
    always publish the SAME set of files for a given repo.

    Case-INSENSITIVE (``SCREEN.HTML`` matches ``*.html``) on every platform,
    not just the case-folding ones `fnmatch.fnmatch` happens to fold on:
    that keeps this aligned with the TUI's `gate_a_mocks_dir_exists_for`
    enablement gate (#2513 review follow-up) — a file that lights the menu
    item up must be a file the command actually publishes, or the operator
    gets an enabled button whose dispatch dies with "nothing to publish".
    """
    return fnmatch.fnmatch(name.lower(), driver_mock_glob.strip().lower())


def resolve_viewable_mock_glob(acceptance_cfg: Any, repo_name: str) -> tuple[str | None, str]:
    """#3068: the ONE answer to "can this repo's Gate-A mocks be shown to a
    customer in a browser, and if so which glob collects them?"

    Returns ``(glob, "")`` when *repo_name*'s configured acceptance driver
    renders browser-viewable mocks, and ``(None, reason)`` otherwise —
    where *reason* is operator-facing prose naming why nothing should be
    published. Every caller that puts a mock bundle in front of a paying
    customer (the merge-triggered auto-push in
    :func:`coord.merge_queue._maybe_push_design_round`, the on-demand
    ``coord portal publish-mocks``) resolves through here, so the two paths
    cannot drift apart the way they did before this issue: pre-#3068 both
    hardcoded ``*.html``, and a partial fix left one right and one wrong.

    Viewability itself is still :func:`_wants_mock_index`'s call (#2512's
    "gate on the glob, not the driver name" rule) — this only adds the
    resolution around it. A repo with no acceptance driver on file yields
    ``None``: guessing ``*.html`` is what shipped a customer a design round
    containing engineering prose and no screen.

    Routed repos (#1125, ``acceptance.drivers.<repo>.routes``) have no
    single driver without a *path*, and this deliberately has none to give —
    a milestone is not one file. So it resolves across the routes instead:
    every route agreeing on one viewable glob is an unambiguous answer, not
    a guess, and is used. Routes that disagree (or any non-viewable route)
    is genuinely unknowable here and skips with a reason, since publishing
    the wrong route's mocks is worse than publishing none.
    """
    entry = (
        acceptance_cfg.drivers.get(repo_name) if acceptance_cfg is not None else None
    )
    if entry is None:
        return None, f"repo {repo_name!r} has no acceptance driver configured"

    candidates = entry.routes or [entry]
    globs = {c.mock.strip() for c in candidates}
    if len(globs) > 1:
        return None, (
            f"repo {repo_name!r}'s acceptance routes declare different mock "
            f"globs ({', '.join(sorted(repr(g) for g in globs))}) and a "
            f"milestone-wide bundle can't pick one"
        )

    glob = globs.pop()
    if not _wants_mock_index(glob):
        return None, (
            f"repo {repo_name!r}'s acceptance driver mock glob is not "
            f"browser-viewable ({glob!r})"
        )
    return glob, ""


def _mock_index_instruction(ms_dir: str) -> str:
    """#2512: the instruction text both briefing builders below hand the
    mock-author worker, verbatim — a *provided script* the worker runs as
    its last step before committing, not something it free-hands per
    milestone (see `scripts/gen_mock_index.py`'s docstring for why: every
    Gate-A mock set should look the same, and nothing should drift by
    taste)."""
    return (
        f"As your LAST step before committing, run `python "
        f"scripts/gen_mock_index.py {ms_dir}/mocks` to (re)generate "
        f"`{ms_dir}/mocks/index.html` — a plain navigation page linking "
        "every mock by its own `<title>` tag (#2512). Do not hand-write "
        "this file yourself; the script is the single source of truth so "
        "every milestone's mock set gets the same glue page."
    )


def build_mock_author_briefing(
    *,
    repo_slug: str,
    milestone_title: str,
    milestone_number: int,
    tracking_issue_number: int,
    tracking_issue_body: str,
    issues: list[dict],
    driver_kind: str,
    driver_mock_glob: str,
) -> str:
    """Compose the seed briefing the mock-author sees as its first user
    message: milestone context + exactly where its output must land."""
    ms_dir = f"tests/acceptance/{ms_dirname(milestone_number)}"

    parts: list[str] = []
    parts.append(
        f"=== Gate A mock-author context for {repo_slug} "
        f"— milestone {milestone_title!r} ===\n"
    )

    parts.append(f"TRACKING ISSUE: #{tracking_issue_number}")
    parts.append("MILESTONE TRACKING ISSUE BODY:")
    parts.append(tracking_issue_body.strip() if tracking_issue_body.strip() else "(empty)")
    parts.append("")

    parts.append(f"OPEN ISSUES UNDER THIS MILESTONE ({len(issues)}):")
    if issues:
        for issue in issues:
            parts.append(f"--- #{issue['number']}: {issue['title']} ---")
            parts.append(issue["body"] if issue["body"] else "(no body)")
            parts.append("")
    else:
        parts.append("  (none fetched)")

    parts.append("---")
    parts.append(
        f"This repo's acceptance driver is `{driver_kind}` — render the "
        f"mock(s) in that medium as `{driver_mock_glob}` fixtures under "
        f"`{ms_dir}/mocks/`, then write the black-box contract to "
        f"`{ms_dir}/contract.md` (docs/ORACLE_LOOP.md \"Layout\"). If "
        f"`{ms_dir}/contract.md` already exists, you are AMENDING Gate A, "
        "not authoring it from scratch — read it first and edit in place. "
        "Commit and push both to this branch when done."
    )
    if _wants_mock_index(driver_mock_glob):
        parts.append(_mock_index_instruction(ms_dir))
    return "\n".join(parts)


def build_mock_author_amend_briefing(
    *,
    repo_slug: str,
    milestone_title: str,
    milestone_number: int,
    tracking_issue_number: int,
    amend_text: str,
    driver_mock_glob: str,
) -> str:
    """#1315: seed briefing for a *targeted* Gate-A contract amendment.

    Unlike :func:`build_mock_author_briefing` (a full fresh render from the
    milestone's open-issues digest), this skips re-fetching milestone
    context entirely and hands the mock-author exactly the operator's own
    description of what to correct in the already-merged
    ``tests/acceptance/ms-NN/contract.md`` (and/or its ``mocks/``). This is
    the "properly-typed contract-amend dispatch" #1315 closes the gap for:
    before this, the only way to land a small, targeted correction to a
    merged Gate-A contract was a plain ``coord assign`` (defaulting to
    ``type="work"``) — the root cause of #1314's three-way breakage. Still
    dispatches as ``type="mock-author"`` (independently-typed, exempt from
    the ``tests/acceptance/**`` sealing guard — see ``coord/dispatch.py``
    and #1315's ``_sealed_write_guard_tools`` in ``coord/agent.py``), so a
    contract correction never needs to fall back to ``type="work"`` again.

    *driver_mock_glob* (#2512) is the repo's resolved acceptance-driver mock
    glob (e.g. ``"*.html"``, ``"*.screen"``) — already resolved by the
    caller (:func:`dispatch_acceptance_mock` always resolves ``driver_cfg``
    before either briefing builder runs). Threaded through purely to gate
    whether the mock-navigation-index instruction below is worth including;
    an amendment that only touches ``contract.md`` still gets the
    instruction, phrased conditionally, since this function has no way to
    know in advance whether the correction will touch ``mocks/``.
    """
    ms_dir = f"tests/acceptance/{ms_dirname(milestone_number)}"

    parts: list[str] = []
    parts.append(
        f"=== Gate A CONTRACT AMENDMENT for {repo_slug} — milestone "
        f"{milestone_title!r} (tracking #{tracking_issue_number}) ===\n"
    )
    parts.append(
        f"This is a targeted correction to the ALREADY-MERGED Gate-A "
        f"contract at `{ms_dir}/contract.md` (and/or its mocks under "
        f"`{ms_dir}/mocks/`) — NOT a from-scratch render. Read the existing "
        f"`{ms_dir}/contract.md` first, then apply exactly the correction "
        "described below. Do not regenerate the document from scratch, and "
        f"do not touch any file outside `{ms_dir}/`. Commit and push both "
        "to this branch when done."
    )
    if _wants_mock_index(driver_mock_glob):
        parts.append(
            f"If this correction touches any file under `{ms_dir}/mocks/`, "
            f"regenerate `{ms_dir}/mocks/index.html` before committing by "
            f"running `python scripts/gen_mock_index.py {ms_dir}/mocks` "
            "(#2512) — a plain navigation page linking every mock by its "
            "own `<title>` tag. Do not hand-write this file yourself; the "
            "script is the single source of truth so every milestone's "
            "mock set gets the same glue page."
        )
    parts.append("")
    parts.append("--- Requested correction ---")
    parts.append(amend_text.strip())
    return "\n".join(parts)


def dispatch_acceptance_mock(
    repo_name: str,
    tracking_issue_number: int,
    config: Config,
    *,
    machine_override: str | None = None,
    path: str | None = None,
    amend_briefing: str | None = None,
) -> tuple[str, str]:
    """End-to-end: resolve the milestone, pick a machine, seed the
    briefing, dispatch a ``type="mock-author"`` assignment.

    *path* (#1125, repo-root-relative, e.g. ``"coord/foo.py"``) resolves
    which driver to use when the repo's acceptance config is routed
    (``acceptance.drivers.<repo>.routes``) — pass the milestone's
    representative subtree (see `AcceptanceConfig.driver_for` for the
    single-path-per-call resolution rule). Unused when the repo has a flat,
    unrouted driver.

    *amend_briefing* (#1315): when given, skip the full fresh-render
    briefing (:func:`build_mock_author_briefing`, which re-fetches every
    open issue under the milestone) in favor of a narrow, targeted
    amendment briefing (:func:`build_mock_author_amend_briefing`) built
    from this exact text — the "small, targeted fix to an already-merged
    contract" case #1314 hit with no properly-typed tool for it. Also
    leaves ``target_branch`` unset (unlike the fresh-render path's
    ``ms-{N}-gate-a``) so the worker gets a normal auto-named branch off the
    milestone's current base — the original gate-a branch was already
    merged (and likely deleted) by the time an amendment is needed.

    Returns ``(assignment_id, machine_name)``. Raises ``RuntimeError`` when
    the repo is unknown, has no acceptance driver configured (or, for a
    routed repo, no driver resolves for *path*), the tracking issue can't be
    fetched or has no milestone, the milestone's Gate A is already claimed,
    no machine claims the repo, or the agent rejects the dispatch.
    """
    repo_cfg = config.repo(repo_name)
    if repo_cfg is None:
        raise RuntimeError(f"repo {repo_name!r} not in coordinator.yml")

    driver_cfg = config.acceptance.driver_for(repo_name, path)
    if driver_cfg is None:
        if config.acceptance.has_driver(repo_name):
            raise RuntimeError(
                f"repo {repo_name!r} has a routed acceptance driver "
                "(acceptance.drivers routes) but no route matched — pass "
                "--for-path to select the milestone's subtree (e.g. "
                "'coord/**')"
            )
        raise RuntimeError(
            f"repo {repo_name!r} has no acceptance driver configured — add "
            "it under acceptance.drivers in coordinator.yml before running "
            "Gate A (docs/ORACLE_LOOP.md)"
        )

    try:
        issue_data = github_ops.get_issue(repo_cfg.github, tracking_issue_number)
    except RuntimeError as e:
        raise RuntimeError(f"could not fetch #{tracking_issue_number}: {e}") from e

    milestone = issue_data.get("milestone") or {}
    milestone_number = milestone.get("number")
    if milestone_number is None:
        raise RuntimeError(f"#{tracking_issue_number} has no milestone")
    milestone_title = milestone.get("title") or f"#{milestone_number}"

    from coord import board_service  # noqa: PLC0415

    board = board_service.read_board()

    claim = find_work_claim(tracking_issue_number, repo_name, repo_cfg.github, board)
    if claim is not None:
        # #1059: make the refusal actionable. The operator's "PERMANENTLY
        # STUCK" report (#1041) was a stale claim they "found no way to clear
        # through normal coord commands" — so name the escape hatch. #3103:
        # the right escape hatch depends on *what kind* of claim this is — a
        # dead board session is cleared by `coord diagnose`, but a leftover
        # `remote_branch` claim (e.g. a squash-merged PR's un-deleted source
        # branch) is invisible to `coord diagnose` entirely, so naming it
        # there cost an operator a full diagnostic round trip that could
        # never have helped. `claim_remedy_hint` picks the remedy that
        # actually matches the claim's source.
        raise RuntimeError(
            f"Gate A already in flight: {claim_message(claim)} — "
            f"{claim_remedy_hint(claim, repo_name, tracking_issue_number)}"
        )

    # Pick the machine.
    if machine_override:
        machine = next(
            (m for m in config.machines if m.name == machine_override),
            None,
        )
        if machine is None:
            raise RuntimeError(f"machine {machine_override!r} not in coordinator.yml")
        if not machine.can_work_on(repo_name):
            raise RuntimeError(
                f"machine {machine_override!r} does not list repo {repo_name!r}"
            )
    else:
        picked: Machine | None = pick_machine(repo_name, board, config)
        if picked is None:
            raise RuntimeError(
                f"no idle machine claims repo {repo_name!r} — mock-author "
                "needs a machine that has the repo cloned"
            )
        machine = picked

    tracking_title = issue_data.get("title") or f"Milestone #{tracking_issue_number}"
    resolved_model = config.models.default

    if amend_briefing is not None:
        # #1315: targeted amend — skip the full open-issues fetch, the
        # fresh-render briefing, and reusing the (likely already-merged and
        # deleted) original gate-a branch name.
        briefing = build_mock_author_amend_briefing(
            repo_slug=repo_cfg.github,
            milestone_title=milestone_title,
            milestone_number=milestone_number,
            tracking_issue_number=tracking_issue_number,
            amend_text=amend_briefing,
            driver_mock_glob=driver_cfg.mock,
        )
        proposal = Proposal(
            id=0,
            machine_name=machine.name,
            repo_name=repo_name,
            issue_number=tracking_issue_number,
            issue_title=f"[gate-a-amend] {tracking_title} — contract correction",
            rationale="Gate A contract amendment (coord acceptance mock --amend, #1315)",
            briefing=briefing,
            model=resolved_model,
            type="mock-author",
            required_gates=list(config.pipeline.default_gates),
        )
    else:
        # #2969: Gate A's contract IS the spec — anything truncated here
        # becomes silent guesswork in `contract.md`. Unlike milestone-chat's
        # own cohort/dependency-inference use of this fetcher, hand the
        # mock-author every body in full (no `max_body_chars` cap).
        issues = _fetch_milestone_issues(
            repo_cfg.github, milestone_number, max_body_chars=None
        )

        briefing = build_mock_author_briefing(
            repo_slug=repo_cfg.github,
            milestone_title=milestone_title,
            milestone_number=milestone_number,
            tracking_issue_number=tracking_issue_number,
            tracking_issue_body=issue_data.get("body") or "",
            issues=issues,
            driver_kind=driver_cfg.kind,
            driver_mock_glob=driver_cfg.mock,
        )

        proposal = Proposal(
            id=0,
            machine_name=machine.name,
            repo_name=repo_name,
            issue_number=tracking_issue_number,
            issue_title=f"[gate-a] {tracking_title} — mock + contract",
            rationale="Gate A mock-author dispatch (coord acceptance mock, #930)",
            briefing=briefing,
            model=resolved_model,
            type="mock-author",
            required_gates=list(config.pipeline.default_gates),
            target_branch=f"ms-{milestone_number}-gate-a",
        )

    from coord.dispatch import dispatch_with_retry, post_briefing  # noqa: PLC0415
    from coord.state import record_dispatched  # noqa: PLC0415

    # #1059 review: dispatch_with_retry can raise ValueError (bad machine/repo
    # config) or httpx.HTTPError (agent unreachable/rejected the payload) —
    # neither was previously caught here, so it propagated past this
    # function's documented "raises RuntimeError" contract as a raw
    # traceback. `acceptance_mock_cmd` only catches RuntimeError, so an
    # uncaught one would dump a multi-line Python traceback to stderr instead
    # of a clean `error: ...` line — exactly the "inconsistent/unreadable
    # error" the operator hit, just via a different trigger than the one
    # reproduced. Translate to RuntimeError so every failure path here is a
    # single clean line, no partial dispatch is recorded (record_dispatched
    # below never runs when this raises), and no claim is left dangling.
    try:
        response = dispatch_with_retry(
            proposal,
            config,
            max_retries=config.concurrency.max_retries,
            backoff_base=config.concurrency.backoff_base,
        )
    except (ValueError, httpx.HTTPError) as e:
        raise RuntimeError(f"could not dispatch mock-author to {machine.name!r}: {e}") from e

    assignment_id = response.get("id") or uuid.uuid4().hex[:12]

    record_dispatched(
        assignment_id=assignment_id,
        proposal=proposal,
        repo_github=repo_cfg.github,
        provider_name=response.get("_provider_name"),
    )

    try:
        post_briefing(proposal, config, assignment_id=assignment_id)
    except Exception:  # noqa: BLE001 — best-effort, mirrors dispatch_entry
        pass

    return assignment_id, machine.name


# ── PDR-3 (#2508): reading a merged Gate-A branch back into a design round ──


def collect_mock_bundle_files(
    repo_github: str, milestone_number: int, branch: str, driver_mock_glob: str
) -> dict[str, str]:
    """Read a rendered Gate-A bundle off *branch* (post-merge) via the
    GitHub Contents API — no local checkout, the same "gh-only wire layer"
    posture the merge queue's other post-merge reads already use (see e.g.
    ``coord.acceptance.clear_expected_red_via_pr``'s docstring).

    Returns a ``{relative_path: content}`` mapping — ``"contract.md"`` plus
    every mock fixture under ``tests/acceptance/ms-<milestone_number>/mocks/``
    matching *driver_mock_glob* (#3068) — ready to hand straight to
    :meth:`coord.portal_bridge.PortalBridgeClient.upload_bundle`. Empty when
    the directory doesn't exist on *branch* at all (Gate A hasn't merged
    anything there yet) — callers treat that as "nothing to push", not an
    error.

    *driver_mock_glob* is the repo's resolved acceptance-driver mock glob
    (``acceptance.drivers.<repo>.mock`` — e.g. ``"*.html"`` for
    ``web-playwright``, ``"*.screen"`` for ``tui-tuidriver``), the same value
    :func:`build_mock_author_briefing` already threads through as
    ``driver_mock_glob``. This function does NOT hardcode ``*.html`` — it
    collects whatever the repo's own driver actually renders, via the shared
    :func:`mock_matches_glob`. It also does NOT judge whether that glob is
    browser-viewable — that is :func:`resolve_viewable_mock_glob`'s job
    (#2512/#3068), and callers that push the result somewhere that must be
    viewable in a browser (a portal design round) must consult it themselves
    before treating a non-empty return as pushable: a non-``*.html`` glob
    still returns real, non-empty mock content here, just content nobody can
    open in a browser.
    """
    ms_dir = f"tests/acceptance/{ms_dirname(milestone_number)}"
    files: dict[str, str] = {}
    if github_ops.repo_file_exists(repo_github, f"{ms_dir}/contract.md", branch):
        files["contract.md"] = github_ops.get_repo_file(
            repo_github, f"{ms_dir}/contract.md", branch
        )
    try:
        mock_names = github_ops.list_repo_dir(repo_github, f"{ms_dir}/mocks", branch)
    except RuntimeError:
        # No `mocks/` dir on this branch at all — same "nothing to push"
        # case list_repo_dir already returns [] for a missing path; the
        # explicit catch is only for a `gh` error shaped differently.
        mock_names = []
    for name in mock_names:
        if mock_matches_glob(name, driver_mock_glob):
            files[f"mocks/{name}"] = github_ops.get_repo_file(
                repo_github, f"{ms_dir}/mocks/{name}", branch
            )
    return files


def build_design_round(
    *,
    milestone_title: str,
    tracking_issue_title: str,
    tracking_issue_body: str,
    bundle_key: str,
    round_number: int = 1,
) -> dict[str, Any]:
    """Build the D1 metadata half of a design round from the same inputs
    `coord milestone chat`'s steward already reads: the tracking issue's
    title/body (source of the plain-language ``outcome_definition``) and its
    ``## Work order`` block (source of the ``decomposition``).

    *bundle_key* is the R2 object key
    :meth:`coord.portal_bridge.PortalBridgeClient.upload_bundle` returned
    for this round's rendered mock bundle + contract — see that method's
    docstring and :func:`coord.portal_sync.enqueue_design_round`'s for why
    the bundle itself is never inlined into this payload.

    A missing/malformed ``## Work order`` block degrades to an empty
    ``decomposition`` rather than raising — a milestone that hasn't written
    one yet (or has a stale one that fails validation) should still get a
    design round pushed with whatever plain-language description its
    tracking issue carries; the portal shows a design round with no listed
    decomposition just fine.
    """
    from coord.milestone_order import WorkOrderError, parse_work_order  # noqa: PLC0415

    try:
        work_order = parse_work_order(tracking_issue_body)
    except WorkOrderError:
        work_order = None
    decomposition = [
        {
            "issue_number": node.issue_number,
            "group": node.group,
            "after": list(node.after),
        }
        for node in (work_order.nodes if work_order is not None else ())
    ]
    outcome_definition = (
        tracking_issue_body.strip()
        or tracking_issue_title.strip()
        or milestone_title
    )
    return {
        "round": round_number,
        "outcome_definition": outcome_definition,
        "decomposition": decomposition,
        "bundle_key": bundle_key,
    }
