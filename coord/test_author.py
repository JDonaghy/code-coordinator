"""Dispatch a `type="test-author"` session (#931, docs/ORACLE_LOOP.md).

An independent, feature-level black-box acceptance-suite author — zero
shared context with the worker under test, the same independence principle
as the adversarial reviewer (`coord/review.py`). Authored **at the Gate-A
arch gate, before any work**, then extended **just-in-time** as each issue
firms up its slice of the surface.

Like `coord/smoke.py` / `coord/review.py` / `coord/conflict_fix.py`, this
bypasses `coord.dispatch.dispatch()` (the `Proposal`/brain-oriented path)
and POSTs directly to the agent's `/assign` endpoint. That matters here
specifically: `coord.dispatch.dispatch()` auto-forbids `tests/acceptance/`
for every proposal when the repo has an acceptance driver configured (the
seal that keeps a `type="work"` worker from editing the oracle it's
graded against) — but a test-author session's entire job IS writing there.
Bypassing `dispatch()` means that seal never applies to this type, by
construction, instead of needing a type-gated exception in a shared
hot path.

The dispatcher does NOT embed `contract.md`'s text into the worker's
briefing — no local checkout is required to dispatch, and the worker is
told, in its briefing, to read the contract from its own checkout. This
mirrors `coord.acceptance.oracle_loop_contract_block` (#945), which points
the *worker's* briefing at the same path rather than embedding the contract
text. (The coordinator itself now does fetch `contract.md`'s content
server-side, via `gate_a_signoff_status`'s #2063 sign-off check below — the
invariant this docstring is actually protecting is "never in the worker's
prompt", not "never read at all".)
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

import click
import httpx

from coord import github_ops
from coord.acceptance import ACCEPTANCE_DIRNAME
from coord.config import Config, entrypoint_sibling_acceptance_dir
from coord.dispatch import AGENT_PORT, DispatchRefused
from coord.machine_pause import paused_set
from coord.milestone_dispatch import (
    MilestoneDispatchError,
    fetch_milestone_context,
    gate_a_signoff_status,
)
from coord.models import Assignment, Machine

# The test-author never needs `gh` — every fact it needs (tracking issue,
# milestone membership, the JIT issue's title/body) is fetched by the
# coordinator and embedded in the briefing before dispatch. Mirrors
# `conflict_fix.CONFLICT_FIX_DENY_COMMANDS`'s "the coordinator owns GitHub
# interactions" rationale.
TEST_AUTHOR_DENY_COMMANDS: list[str] = [
    "Bash(gh *)",
]

# #1172 follow-up: how many versioned forks (`-v2`, `-v3`, ...) to probe
# before giving up on finding a JIT slice's next re-authoring branch. Kept
# small and finite — a real loop here means something is genuinely wrong
# (not a legitimate string of re-authoring dispatches), and #1172's own
# guard should surface that as a loud, readable error rather than spin.
MAX_REAUTHOR_BRANCH_ATTEMPTS = 20

TEST_AUTHOR_SYSTEM_PROMPT = """\
You are an INDEPENDENT acceptance-test author dispatched by the coordinator.

You have ZERO shared context with whoever implements this milestone's issues \
— the same independence principle as an adversarial code reviewer. You write \
to the CONTRACT, never to an implementation. Do not read, diff, or reason \
about any work branch, PR, or commit for the issue(s) in your briefing — if \
one exists, ignore it. Your tests must be derivable from the contract and \
the issue description alone. If you find yourself wanting to peek at code to \
"see what it actually does," stop — that would make you the worker grading \
its own homework, which is exactly what this assignment type exists to \
prevent.

Your job:
1. Read the contract at the path given in your briefing \
(`tests/acceptance/ms-NN/contract.md`) from YOUR OWN checkout. If it does \
not exist, STOP and output:
     STUCK: contract.md missing at <path> — Gate-A hasn't produced it yet
1b. Also open the rendered mock(s) named `MOCKS:` in your briefing (under \
`tests/acceptance/ms-NN/mocks/`), if any exist. For a `tui-tuidriver` \
driver the `.screen` grid IS the assertion fixture (compare against it \
directly). For a `web-playwright` driver the `.html` file(s) are \
hand-authored wireframes, NOT auto-consumed fixtures — read their markup \
yourself and write your Playwright assertions (roles, visible text, \
`data-testid` locators, structure) against exactly what the mock renders. \
The mock is part of the contract; contract.md alone may not spell out \
every DOM detail.
   Do not invent a contract yourself.
1c. For a `web-playwright` driver ONLY: seed your slice with a fixture \
file, not `page.route()`. Write `tests/acceptance/ms-NN/fixtures/<name>.json` \
(schema: `coord/dashboard/fixture.py`'s module docstring — same shape as \
`tests/fixtures/board-pipeline-basic.json`, which is a worked example) and \
`playwright.acceptance.config.ts` will automatically boot a real \
`coord web --fixture` process seeded from it for your slice (#1818) — no \
`page.route()` interception needed anywhere in your spec, and no dev server \
guesswork about which endpoints exist. Write AT MOST ONE `*.json` file under \
that `fixtures/` directory; the config raises if it finds more than one. \
(The `page.route()`-based slice that used to be this repo's one exception, \
`tests/acceptance/ms-51/`, left with the webapp for the `coord-web` repo in \
#2009 — there is no in-repo counter-example to copy from any more.)
2. Author (or extend) the acceptance suite in `tests/acceptance/ms-NN/`, \
using the repo's declared driver framework (kind + run command are in your \
briefing) — the tests must be runnable by that exact command.
2b. WIRE THE SLICE IN. If your briefing names an `ENTRY POINT:` path, that \
file is the driver's crate root and is part of your sealed surface: your \
slice does NOT execute until you register it there (for a Rust/cargo \
driver, an `include!("../../tests/acceptance/ms-NN/<slice>.rs");` line). \
ADD that line. Do not rewrite, reorder, or delete anything already in the \
file, and register nothing beyond your own new slice files. If the briefing \
says the driver has no entry point, it discovers tests by directory and \
there is nothing to wire.
3. Write `tests/acceptance/ms-NN/manifest.d/<issue-number>.yml` — YOUR issue's \
OWN manifest fragment file (#2543), mapping every test id you added/kept to \
that issue number, in either accepted shape:
     tests: {<test-id>: <issue-number>, ...}
   or
     issues: {<issue-number>: [<test-id>, ...], ...}
   This file belongs to your issue and NO OTHER slice ever writes to it — \
create it fresh, or overwrite it wholesale if it already exists from a prior \
dispatch on this same branch, per the RESUMING guidance below if present. Do \
NOT write to the shared `manifest.yml` in this directory (if one exists, it \
carries only milestone-level `gate_a:`/`exempt:` declarations, not test/issue \
data) and do NOT touch any OTHER issue's `manifest.d/<other-issue>.yml` — \
each issue's fragment is its own file precisely so two slices writing at the \
same time, or against stale bases, can never collide on one shared file.
4. Your tests MUST be RED right now (the implementation doesn't exist yet). \
Run the driver's run command yourself and confirm the new/changed tests \
fail (not error out from a missing framework hookup) — a red suite that \
doesn't even execute is not useful to the worker who inherits it. If the \
run reports ZERO tests for your ids, your slice is not wired in (see 2b) — \
that is a failure, not a pass.
4b. Record `expected_red` in YOUR issue's own manifest fragment \
(`manifest.d/<issue-number>.yml`, step 3) from what you JUST OBSERVED in \
step 4 — not from what you intended to write:
     expected_red:
       <issue-number>:
         - <test-id that FAILED in your step-4 run>
         - ...
   The rule is OBSERVED, not INTENDED: list exactly the ids you watched \
FAIL in step 4, and no others — not "the ids my slice adds," not "the ids \
that should be red." A control clause that stayed green (it must keep \
passing to prove a regression would be caught) and a ratchet clause that \
stayed green (existing behavior the slice must not break) both exclude \
themselves automatically, because they never failed — do not add them \
because they're "part of the slice." If you skip this step, every id you \
added lands with no `expected_red` entry, is red to CI with nothing telling \
it that's expected, and the operator has to hand-edit the manifest or pass \
`--force-merge` to merge your slice at all.
   If step 4's run reported NOTHING failing for this issue's ids — every \
test you added or touched already passes — your slice is vacuous (it \
asserts nothing the current code doesn't already satisfy, #1965) and \
`expected_red` would be empty. Do not file an empty entry. STOP and output:
     STUCK: step 4 run reported zero failures for #<issue-number> — the \
slice doesn't test anything new against the current (unimplemented) state.
5. Do NOT touch anything outside `tests/acceptance/ms-NN/**`, with exactly \
ONE exception: the `ENTRY POINT:` file named in your briefing, and only to \
ADD the registration lines for your own slice (step 2b). You are not \
implementing, refactoring, or fixing anything else in the repo. NEVER drop \
the registration line to make your diff look narrower — an unwired slice is \
dead code that reports zero tests and fails the gate late and expensively \
(#1552).
6. Commit and push your branch. Do not open a PR — the coordinator handles \
that.

If the contract is ambiguous or silent on a case you think matters, don't \
guess: write the test with a `// TODO(test-author): contract doesn't specify
X` comment (or the language's equivalent) rather than inventing behavior, \
and call it out in your final summary.
"""

# #1173: the human-attended (`coord acceptance author --interactive`) variant
# of the system prompt above. The independence contract is UNCHANGED — same
# rules, same STOP-on-missing-contract, same "don't peek at the work branch"
# — this only tells the model an operator is attached, since that's a
# material fact about the session it would otherwise have no way to know.
TEST_AUTHOR_INTERACTIVE_SYSTEM_PROMPT = (
    TEST_AUTHOR_SYSTEM_PROMPT
    + "\n\nThis session is HUMAN-ATTENDED: an operator is at the keyboard "
    "with you, watching your output and able to redirect you. That does "
    "NOT relax anything above — you still author from the contract alone, "
    "with zero shared context with the implementation. The operator is "
    "here to catch a bad contract/mock read, a stuck session, or an "
    "ambiguous case worth discussing out loud — not to hand you "
    "implementation details or steer you toward a specific test shape "
    "beyond what the contract says.\n"
)


def test_author_deny_commands(config: Config, repo_name: str) -> list[str]:
    """Merge the repo's configured deny-list with :data:`TEST_AUTHOR_DENY_COMMANDS`.

    Shared by :func:`dispatch_test_author` (the original dispatch) and
    ``coord.auto_loop._dispatch_fix`` (a bounced test-author fix, #1176) so
    the two call sites can't drift — a fix session POSTs `type="test-author"`
    directly to `/assign` the same way the original dispatch does, and needs
    the identical guardrails.
    """
    repo_cfg = config.repo(repo_name)
    repo_deny = (
        repo_cfg.worker_permissions.deny
        if repo_cfg and repo_cfg.worker_permissions
        else []
    )
    return list(dict.fromkeys(list(repo_deny) + TEST_AUTHOR_DENY_COMMANDS))


def pick_test_author_machine(
    config: Config, repo_name: str, required_capability: str = ""
) -> Machine | None:
    """Pick a machine to run the test-author session on.

    Any qualified, unpaused machine that has the repo cloned and (when the
    repo's driver declares one) the required capability — mirrors
    `refine_chat.pick_refinement_machine`'s "any qualified machine works"
    simplicity; test-author is a one-off CLI-triggered dispatch (Gate A /
    JIT), not a high-frequency auto-dispatch, so no idle/busy weighting is
    needed. Unlike smoke, there's no "different from the worker" axis here
    — there is no single worker machine to avoid, by design.
    """
    paused = paused_set(config.machines)
    for m in config.machines:
        if not m.can_work_on(repo_name):
            continue
        if m.repo_path(repo_name) is None:
            continue
        if m.name in paused:
            continue
        if required_capability and required_capability not in m.capabilities:
            continue
        return m
    return None


def build_test_author_briefing(
    *,
    repo_name: str,
    repo_github: str,
    ms_dir: str,
    tracking_issue: int,
    milestone_number: int,
    milestone_issue_numbers: list[int],
    driver_kind: str,
    driver_run: str,
    issue_number: int | None,
    issue_title: str | None,
    issue_body: str | None,
    driver_entrypoint: str = "",
    driver_mock: str = "",
    default_branch: str = "main",
) -> str:
    """Compose the test-author's briefing (its first/only user message).

    Two modes, matching docs/ORACLE_LOOP.md's "authored red ... before the
    work, then extended just-in-time": *milestone mode* (`issue_number` is
    None) authors the full initial suite from the contract; *JIT mode*
    (`issue_number` set) extends just that issue's slice.

    *default_branch* (#2539) is the repo's merge target (``repo_cfg.
    default_branch or "main"`` at both call sites, the same value already
    sent as the dispatch payload's ``branch`` field). JIT mode uses it to
    tell the author to check this branch's mergeability before finishing —
    a re-dispatch onto an already-authored slice branch (see the #2552
    RESUMING note appended by `dispatch_test_author`) previously had no
    instruction to look at `default_branch` at all, so a slice that had
    gone stale behind a sibling slice's merge would report done with an
    unresolved conflict still sitting on the PR (coord-portal#132).

    *driver_entrypoint* (#1552) is the driver's declared ``entrypoint:`` —
    the crate root a slice must be registered in before the run command can
    see it, for a framework that links tests through an entry point rather
    than discovering them by directory. Stated explicitly in BOTH directions:
    naming it authorises the one out-of-tree write the author needs, and
    saying "none" stops an author on a directory-discovered route (pytest)
    from inventing one.

    *driver_mock* (#1542) is the driver's declared ``mock:`` glob (e.g.
    ``"*.screen"``, ``"*.html"``) — named explicitly so the author knows
    which files under ``mocks/`` to open. For a mock-format whose kind
    treats the mock as source (``tui-tuidriver``'s ``.screen`` grid IS the
    assertion fixture), this is just where to find it; for ``web-playwright``
    the ``.html`` mock is a hand-authored wireframe the author must read and
    transcribe into DOM assertions (see :data:`TEST_AUTHOR_SYSTEM_PROMPT`
    step 1b) — contract.md alone is not guaranteed to spell out every
    role/text/test-id.

    For ``driver_kind == "web-playwright"`` a ``FIXTURES:`` line is also
    emitted (#1818): ``playwright.acceptance.config.ts`` boots a real
    ``coord web --fixture`` process seeded from
    ``tests/acceptance/ms-NN/fixtures/*.json`` when present, so the author
    should seed the slice that way instead of hand-rolling a
    ``page.route()`` interception (see :data:`TEST_AUTHOR_SYSTEM_PROMPT`
    step 1c).
    """
    # #2896: an entrypoint-linked driver's slices (and their contract/mocks/
    # manifest) live under that entrypoint's OWN sibling `acceptance/` dir,
    # not the shared repo-root tree — relocated out of the repo root so the
    # crate is self-contained (the same rule
    # `coord.config.AcceptanceConfig.sealed_paths` already seals by). A
    # directory-discovered driver (no entrypoint, e.g. cli-pytest) is
    # unaffected and keeps using the shared repo-root tree exactly as
    # before.
    dirname = (
        entrypoint_sibling_acceptance_dir(driver_entrypoint).rstrip("/")
        if driver_entrypoint
        else ACCEPTANCE_DIRNAME
    )
    contract_path = f"{dirname}/{ms_dir}/contract.md"
    # #2543: per-issue manifest fragments — each issue writes ONLY its own
    # `manifest.d/<issue>.yml`, never the shared `manifest.yml` (which now
    # carries just milestone-level `gate_a:`/`exempt:` declarations, if any).
    # In JIT mode the issue number is known, so name the exact file; in
    # milestone mode point at the pattern, since one fragment per work-order
    # issue is expected.
    manifest_glob = (
        f"{dirname}/{ms_dir}/manifest.d/{issue_number}.(yml|json)"
        if issue_number is not None
        else f"{dirname}/{ms_dir}/manifest.d/<issue-number>.(yml|json)"
        " (one fragment file PER issue in the work-order list below)"
    )
    mocks_glob = f"{dirname}/{ms_dir}/mocks/{driver_mock}" if driver_mock else (
        f"{dirname}/{ms_dir}/mocks/ (glob not declared)"
    )

    parts: list[str] = []
    parts.append(
        f"=== Independent test-author session for {repo_github} — "
        f"milestone #{milestone_number} (tracking issue #{tracking_issue}) ===\n"
    )
    parts.append(f"CONTRACT: {contract_path}")
    parts.append(f"MOCKS: {mocks_glob}")
    if driver_kind == "web-playwright":
        parts.append(
            f"FIXTURES: {dirname}/{ms_dir}/fixtures/<name>.json "
            "(at most one file — schema: coord/dashboard/fixture.py, worked "
            "example: tests/fixtures/board-pipeline-basic.json). Seed your "
            "slice with this instead of page.route() — #1818."
        )
    parts.append(f"MANIFEST: {manifest_glob}")
    parts.append(f"DRIVER: kind={driver_kind!r}  run={driver_run!r}")
    if driver_entrypoint:
        parts.append(f"ENTRY POINT: {driver_entrypoint}")
        parts.append(
            f"  This driver links its slices through `{driver_entrypoint}` — "
            "your slice files are INVISIBLE to the run command above until "
            "they are registered there (for cargo: an "
            f"`include!(\"acceptance/{ms_dir}/<slice>.rs\");` line, relative "
            f"to `{driver_entrypoint}`'s own directory — #2896 relocated the "
            "sealed slices to live beside the entrypoint, not across the "
            "repo root). Adding those registration lines is part of your "
            "job and is explicitly allowed even though the file sits "
            f"outside `{dirname}/{ms_dir}/` — it is part of the sealed "
            "oracle, declared as this driver's `entrypoint:` (#1552). ADD "
            "only; do not rewrite, reorder, or delete what is already there, "
            "and never remove a registration line to narrow your diff."
        )
    else:
        parts.append(
            "ENTRY POINT: (none — this driver discovers tests by directory, "
            "so there is nothing to wire up and nothing to touch outside "
            f"`{dirname}/{ms_dir}/`)"
        )
    parts.append(
        f"MILESTONE WORK-ORDER ISSUES: {milestone_issue_numbers or '(none recorded yet)'}"
    )
    parts.append("")

    if issue_number is None:
        parts.append(
            "MODE: full milestone authoring (Gate A). Author the initial red "
            f"acceptance suite under `{dirname}/{ms_dir}/` covering "
            "the whole black-box surface in the contract, with at least one "
            "test per issue in the work-order list above, and write ONE "
            "manifest fragment PER issue "
            f"(`{dirname}/{ms_dir}/manifest.d/<issue-number>.yml`, "
            "#2543) mapping that issue's own test ids — never a single "
            "manifest.yml covering all of them."
        )
    else:
        parts.append(
            f"MODE: just-in-time slice extension for issue #{issue_number}. "
            "Extend the existing suite with tests covering ONLY this issue's "
            "slice of the black-box surface — leave other issues' tests, and "
            "their manifest fragments, alone. Your manifest data goes ONLY in "
            f"YOUR OWN `{dirname}/{ms_dir}/manifest.d/"
            f"{issue_number}.yml` (#2543)."
        )
        parts.append("")
        parts.append(f"ISSUE #{issue_number}: {issue_title or '(no title)'}")
        parts.append(issue_body.strip() if issue_body and issue_body.strip() else "(no body)")
        parts.append("")
        parts.append(
            f"MERGEABILITY (#2539): before you finish — after step 4b's "
            f"manifest update, before step 6's commit/push — fetch "
            f"`{default_branch}` (`git fetch origin {default_branch}`) and "
            f"check whether this branch still merges onto it cleanly (e.g. "
            f"`git merge --no-commit --no-ff origin/{default_branch}`, then "
            "`git merge --abort` once you've seen the result — never leave "
            "the worktree mid-merge). This branch may already carry commits "
            "from a prior dispatch (see any note above about resuming "
            "unfinished work), and a sibling slice's PR may have merged "
            f"into `{default_branch}` since this one was authored. Since "
            "#2543, a sibling slice's manifest entries live in ITS OWN "
            f"`manifest.d/<other-issue>.yml` — a different file from yours — "
            "so that specific collision (two slices' manifest edits landing "
            "in the same file) can no longer happen at all. If it merges "
            "clean, proceed to commit/push as normal. If it conflicts:\n"
            f"  - Confined to `{dirname}/{ms_dir}/**` (the sealed "
            "suite YOU already have authoring rights over) AND a clean "
            "additive collision (e.g. an entry-point registration line "
            "alongside a sibling's own) — resolve it yourself: rebase onto "
            f"`origin/{default_branch}` and keep BOTH sides' additions, "
            "never dropping the other slice's entries to make yours "
            "resolve. Re-run step 4's red-check afterward — a rebase can "
            "shift what the tests see underneath them.\n"
            "  - Anything else — a conflict that reaches outside "
            f"`{dirname}/{ms_dir}/**`, or isn't a clean additive "
            "collision — do NOT guess a resolution. Abort the merge/rebase, "
            "leave the branch as it was, and STOP with:\n"
            f"      STUCK: merge conflict against {default_branch} in "
            "<file(s)> — outside sealed-authoring scope or not a clean "
            "additive merge\n"
            "(same posture a `conflict-fix` session takes for a "
            "non-rebaseable conflict — you are simply the only dispatch "
            f"type structurally allowed to touch `{dirname}/` "
            "at all, so a conflict confined there is yours to resolve, not "
            "conflict-fix's)."
        )

    parts.append("")
    parts.append("---")
    parts.append(
        "Follow the steps in your system prompt (read contract → author/"
        "extend → "
        + ("wire into the entry point → " if driver_entrypoint else "")
        + "update manifest → verify red → commit + push, no PR)."
    )
    return "\n".join(parts)


def dispatch_test_author(
    repo_name: str,
    tracking_issue: int,
    config: Config,
    *,
    issue_number: int | None = None,
    machine_override: str | None = None,
    path: str | None = None,
    http_client: httpx.Client | None = None,
) -> tuple[str, str]:
    """End-to-end: resolve the milestone, pick a machine, seed the
    briefing, dispatch a `type="test-author"` assignment.

    *path* (#1125, repo-root-relative, e.g. ``"coord/foo.py"``) resolves
    which driver to use when the repo's acceptance config is routed
    (``acceptance.drivers.<repo>.routes``) — pass the milestone/issue's
    representative subtree (see `AcceptanceConfig.driver_for` for the
    single-path-per-call resolution rule). Unused (and unneeded) when the
    repo has a flat, unrouted driver.

    Returns `(assignment_id, machine_name)`. Raises `RuntimeError` on any
    resolution failure (unknown repo, no acceptance driver configured (or,
    for a routed repo, no driver resolves for *path*), bad tracking issue,
    `issue_number` not a member of the milestone's work order, no qualified
    machine, or the agent rejecting the dispatch) — EXCEPT the #2063 Gate-A
    sign-off refusal (contract exists but carries no recorded human
    verdict), which raises `coord.dispatch.DispatchRefused` (a `ValueError`
    subclass) instead, so callers can tell a deterministic, operator-fixable
    refusal apart from every other, non-recoverable failure mode here.

    Branch: milestone mode (`issue_number=None`) shares one branch/PR across
    repeated calls, keyed on *tracking_issue*. JIT mode (`issue_number` set)
    gets its own per-slice branch keyed on `(tracking_issue, issue_number)`
    — required so each member issue's slice can merge independently without
    stranding the next slice on an already-closed PR (#1171).
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
            f"no acceptance driver configured for repo {repo_name!r} "
            "(add it under acceptance.drivers in coordinator.yml)"
        )

    try:
        ctx = fetch_milestone_context(repo_cfg, tracking_issue)
    except MilestoneDispatchError as e:
        raise RuntimeError(str(e)) from e

    if issue_number is not None and ctx.work_order.node(issue_number) is None:
        raise RuntimeError(
            f"issue #{issue_number} is not a member of milestone "
            f"#{ctx.milestone_number}'s work order (tracking issue #{tracking_issue})"
        )

    # #2063: refuse to author a sealed slice against a contract no human has
    # signed off on. This is the path that actually burned money on
    # coord-portal ms-2 — an independent test-author authored ~$2.70 of
    # sealed suite against an unapproved surface, and it happened to be a
    # good contract by luck, not process. The strings a contract pins
    # (button text, data-testid hooks, status vocabulary) become assertions
    # the worker may never edit, so a wrong one is not cosmetic: it is a
    # suite that enforces the wrong product.
    #
    # Raised as `DispatchRefused` (a `ValueError` subclass), NOT a plain
    # `RuntimeError` like every other failure mode in this function: this
    # refusal is deterministic and operator-fixable (`coord gate-a
    # --approved`), not a crash, and `coord acceptance author`'s CLI handler
    # maps THIS exception — and only this one — to `EXIT_DISPATCH_REFUSED`
    # so `coord drive-queue`'s tick can park the entry (#1891/#1892) instead
    # of burning attempts toward terminal `blocked` (#2040) on an unapproved
    # contract. Same distinguishing pattern `enforce_oracle_readiness` (the
    # Work-dispatch guard this mirrors) already uses in `coord/dispatch.py`.
    signoff_refusal = gate_a_signoff_status(repo_cfg, config, ctx.milestone_number)
    if signoff_refusal is not None:
        raise DispatchRefused(signoff_refusal)

    if machine_override:
        machine = next(
            (m for m in config.machines if m.name == machine_override), None
        )
        if machine is None:
            raise RuntimeError(f"machine {machine_override!r} not in coordinator.yml")
        if not machine.can_work_on(repo_name):
            raise RuntimeError(
                f"machine {machine_override!r} does not list repo {repo_name!r}"
            )
    else:
        machine = pick_test_author_machine(config, repo_name, driver_cfg.capability)
        if machine is None:
            cap_note = (
                f" with capability {driver_cfg.capability!r}"
                if driver_cfg.capability else ""
            )
            raise RuntimeError(
                f"no machine claims repo {repo_name!r}{cap_note} — "
                "test-author needs a machine with the repo cloned"
                + (" and the driver's required capability" if cap_note else "")
            )

    repo_path = machine.repo_path(repo_name)
    if repo_path is None:
        raise RuntimeError(
            f"machine {machine.name!r} has no repo_path for {repo_name!r}"
        )

    issue_title: str | None = None
    issue_body: str | None = None
    if issue_number is not None:
        try:
            issue_data = github_ops.get_issue(repo_cfg.github, issue_number)
        except RuntimeError as e:
            raise RuntimeError(f"could not fetch #{issue_number}: {e}") from e
        issue_title = issue_data.get("title") or ""
        issue_body = issue_data.get("body") or ""

    ms_dir = f"ms-{ctx.milestone_number}"
    briefing = build_test_author_briefing(
        repo_name=repo_name,
        repo_github=repo_cfg.github,
        ms_dir=ms_dir,
        tracking_issue=tracking_issue,
        milestone_number=ctx.milestone_number,
        milestone_issue_numbers=list(ctx.work_order.issue_numbers),
        driver_kind=driver_cfg.kind,
        driver_run=driver_cfg.run,
        driver_entrypoint=driver_cfg.entrypoint,
        driver_mock=driver_cfg.mock,
        issue_number=issue_number,
        issue_title=issue_title,
        issue_body=issue_body,
        default_branch=repo_cfg.default_branch or "main",
    )

    # #1171: milestone-mode dispatches (issue_number is None) keep a single
    # FIXED assignment title so repeated calls derive the SAME shared branch
    # (issue-{tracking_issue}-{slug(title)}, see AgentServer._setup_worktree)
    # — that's Gate A's "extend the same in-flight suite" case, and the
    # branch's PR does not merge until the whole milestone's suite is ready.
    #
    # JIT slices (issue_number set) must NOT reuse that shared branch: each
    # member issue's slice needs its own PR because #1138's oracle gate
    # merges it to unblock that issue's own Work — so the shared branch's PR
    # is guaranteed closed before the next slice is authored, silently
    # stranding it (#1171). Key the branch on (milestone, member issue)
    # instead via an explicit `target_branch`, deliberately OUTSIDE the
    # `issue-{N}-*` namespace: if this PR is squash-merged (ancestry breaks)
    # and deleteBranchOnMerge=false lets the branch survive, an
    # `issue-{issue_number}-*` name would false-positive `coord.claim`'s
    # remote-branch check for that same member issue's own Work dispatch —
    # re-wedging the exact stall this fix removes. A retry/continuation for
    # the SAME (tracking_issue, issue_number) pair still resolves to the
    # same branch name, so extending an already-authored slice keeps
    # pushing to its own still-open PR rather than forking a new one.
    if issue_number is None:
        assignment_title = f"[test-author] ms-{ctx.milestone_number} acceptance suite"
        target_branch: str | None = None
    else:
        assignment_title = (
            f"[test-author] ms-{ctx.milestone_number} slice #{issue_number}"
        )
        target_branch = f"test-author-ms-{ctx.milestone_number}-slice-{issue_number}"

    # #1172: resolve the branch this dispatch would push onto UP FRONT (same
    # formula `AgentServer._setup_worktree` / the post-dispatch recording
    # below use) and fail loudly if its PR has already merged, instead of
    # silently dispatching a worker whose commits would land on a dead
    # branch with no open PR for review/merge to ever pick up (#947/#1115 —
    # a day-long invisible strand). This is defence-in-depth on top of
    # #1171's branch-per-slice fix: it also catches a *retry* of the SAME
    # (tracking_issue, issue_number) pair after that slice's own PR already
    # merged (e.g. via #1138's oracle gate), and a stale milestone-mode
    # dispatch after Gate A's shared-suite PR merged out from under it.
    from coord.agent import _slugify  # noqa: PLC0415

    branch = target_branch or f"issue-{tracking_issue}-{_slugify(assignment_title)}"

    if github_ops.pr_is_merged(repo_cfg.github, branch):
        if issue_number is not None:
            # #1172's own stated fix scope was "refuse (or fork a fresh
            # branch)" — only the refuse half shipped. This is the other
            # half: a genuine re-authoring dispatch (e.g. a Gate-A contract
            # `--amend`, docs/ORACLE_LOOP.md's "amend the contract -> the
            # test-author updates the affected slice") has nowhere else to
            # push once the slice's own PR has merged, so fork onto the next
            # unmerged versioned branch instead of failing outright.
            base_branch = branch
            for suffix in range(2, MAX_REAUTHOR_BRANCH_ATTEMPTS + 1):
                candidate = f"{base_branch}-v{suffix}"
                if not github_ops.pr_is_merged(repo_cfg.github, candidate):
                    branch = candidate
                    target_branch = candidate
                    break
            else:
                raise RuntimeError(
                    f"branch {base_branch!r} and its next "
                    f"{MAX_REAUTHOR_BRANCH_ATTEMPTS - 1} versioned forks "
                    "(-v2..-v"
                    f"{MAX_REAUTHOR_BRANCH_ATTEMPTS}) all already have "
                    "merged PRs (#1172) — that many re-authoring rounds on "
                    "one slice is not a normal amend/re-sync cycle; open a "
                    "fresh branch by hand and investigate."
                )
        else:
            raise RuntimeError(
                f"branch {branch!r} already has a merged PR — dispatching "
                "would push new commits onto a dead branch with nothing "
                "left to open a PR against them (#1172). The milestone's "
                "Gate-A suite PR already merged; if the suite needs more "
                "work, open a fresh branch by hand — do not retry as-is."
            )

    # #2552: a retried JIT dispatch re-derives this SAME branch for the same
    # (tracking_issue, issue_number) pair, and `_setup_worktree` already
    # resumes from `origin/<branch>` at the git level whenever it exists —
    # the #389/#460 continuation-branch logic checks out the remote tip
    # instead of branching fresh. So a retried worker's worktree is NOT
    # empty when the branch already carries real commits (e.g. a #1394
    # WIP-rescue commit left behind by a killed prior attempt) — but the
    # briefing built above reads identically whether the branch is fresh or
    # already has content, so nothing told the worker to check what's
    # already there before writing more, and a worker that doesn't think to
    # `git log` first can end up re-authoring over its own prior output.
    # `branch_commits_ahead` asks GitHub directly (no local checkout
    # required — the coordinator dispatching this may not have one); `None`
    # (lookup failed) is left silent rather than asserted either way, same
    # fail-quiet posture as every other best-effort GitHub read in this
    # function.
    _resume_ahead = github_ops.branch_commits_ahead(
        repo_cfg.github, repo_cfg.default_branch or "main", branch
    )
    if _resume_ahead:
        briefing += (
            "\n\n---\nRESUMING: this branch already has "
            f"{_resume_ahead} commit(s) on it ahead of "
            f"{repo_cfg.default_branch or 'main'} — likely unfinished or "
            "unverified work from an interrupted prior session (possibly a "
            "coordinator WIP-rescue commit — see #1394). Run `git log` and "
            "read what's already there FIRST. Continue/complete/verify it "
            "rather than re-authoring from scratch (#2552)."
        )

    deny_commands = test_author_deny_commands(config, repo_name)

    # #2549: this was the only dispatcher on the fleet that never sent
    # "model" — with no model on the wire, the agent built `claude -p` with
    # no `--model` flag and the CLI fell back to ITS OWN default (Opus),
    # silently overriding `models.default` (sonnet) for every headless
    # test-author slice. Mirrors `dispatch_test_author_interactive`'s
    # `resolved_model = config.models.default` (this feature's already-
    # established answer, see that function below) and `gate_b.py`'s plain
    # `config.models.default` shape — deliberately NOT
    # `resolve_dispatch_model_alias`'s label-routing (`coord/dispatch.py`):
    # test-author dispatches off a milestone/tracking issue, not a single
    # labelled work issue, so there's no per-issue label to route on.
    resolved_model = config.models.default

    payload = {
        "repo_name": repo_name,
        "repo_path": repo_path,
        "issue_number": tracking_issue,
        "issue_title": assignment_title,
        "briefing": briefing,
        "files_allowed": [],
        "files_forbidden": [],
        "pull_repos": [],
        "type": "test-author",
        "system_prompt": TEST_AUTHOR_SYSTEM_PROMPT,
        "deny_commands": deny_commands,
        "branch": repo_cfg.default_branch or "main",
        "model": resolved_model,
    }
    if target_branch:
        payload["target_branch"] = target_branch

    url = f"http://{machine.host}:{AGENT_PORT}/assign"
    client = http_client or httpx
    resp = client.post(url, json=payload, timeout=15)
    resp.raise_for_status()
    agent_response = resp.json()

    assignment_id = agent_response.get("id") or uuid.uuid4().hex[:12]

    # #1171: record the deterministic branch name up front (mirrors #706's
    # `state._record_dispatched_local`) instead of leaving it NULL for
    # `reconcile.py`'s `issue-{tracking_issue}-*` backfill sweep (#1083) to
    # guess later — that sweep's prefix search can never find a JIT slice's
    # `target_branch` (deliberately outside the `issue-{N}-*` namespace, see
    # above), so the branch must be known at dispatch time here. (`branch`
    # itself was already resolved above, ahead of the POST, for the #1172
    # merged-PR guard.)

    asg = Assignment(
        machine_name=machine.name,
        repo_name=repo_name,
        issue_number=tracking_issue,
        issue_title=assignment_title,
        files_allowed=[],
        files_forbidden=[],
        briefing=briefing,
        assignment_id=assignment_id,
        status="running",
        dispatched_at=time.time(),
        branch=branch,
        type="test-author",
        model=resolved_model,
        # #1084: correlate this JIT dispatch back to the specific member
        # issue it's extending, so the TUI's per-issue Acceptance-Authoring
        # mini-pipeline can tell "issue #1039's slice" apart from a sibling
        # issue's slice sharing the same tracking-issue-keyed assignment
        # row. None in milestone mode (issue_number is None) — Gate A's own
        # mock-author track doesn't need this field.
        for_issue_number=issue_number,
    )

    from coord.state import record_dispatched_assignment  # noqa: PLC0415

    record_dispatched_assignment(assignment=asg, repo_github=repo_cfg.github)

    return assignment_id, machine.name


def dispatch_test_author_interactive(
    repo_name: str,
    tracking_issue: int,
    config: Config,
    *,
    issue_number: int | None = None,
    machine_override: str | None = None,
    path: str | None = None,
    dry_run: bool = False,
) -> int:
    """Human-attended counterpart to :func:`dispatch_test_author` (#1173,
    ``coord acceptance author --interactive``).

    Reuses the SAME interactive-launch machinery every other attended stage
    uses, instead of rebuilding any of it:

    * :func:`coord.commands.dispatch._build_interactive_launch_setup` for the
      shared ``ClaudePtyProvider`` / local-vs-remote detection / per-issue
      context digest (#603) that every ``coord assign --interactive`` flavour
      shares — including its #2086 ``_require_interactive_tty`` gate, which
      refuses to claim/record this assignment when stdin is not a TTY (and
      this isn't a dry run), the same protection ``coord assign
      --interactive`` gets.
    * :func:`coord.agent.setup_interactive_worktree` (local) / a raw
      ``git worktree add`` shell command over ssh+tmux (remote) — the same
      primitives :func:`~coord.commands.dispatch_workers._dispatch_rework_of`
      uses to land on a NAMED branch rather than a fresh one.
    * :func:`coord.interactive.launch_human_attended_interactive` /
      :func:`~coord.interactive.finalize_interactive_exit` /
      :func:`~coord.interactive.finalize_remote_interactive_exit` for the
      actual PTY/tmux attach and the #466 git-floor completion backstop.

    The session lands on the EXACT same derived branch a headless dispatch
    of the same milestone/JIT slice would use — ``issue-{tracking_issue}-
    {slug(assignment_title)}``, the same derivation
    :meth:`coord.agent.AgentServer._setup_worktree` uses for
    ``type="test-author"`` — so headless and interactive test-author
    dispatches continue the SAME branch/PR rather than forking one each.

    Keeps the independence seal unchanged: the briefing content is built by
    the SAME :func:`build_test_author_briefing` the headless path uses, and
    the system prompt is :data:`TEST_AUTHOR_INTERACTIVE_SYSTEM_PROMPT` — the
    headless prompt plus a one-paragraph "an operator is watching" note.
    ``--interactive`` changes who supervises the authoring, never who writes
    the tests or what they may read.

    Records a ``type="test-author"`` :class:`~coord.models.Assignment` with
    ``provider_name="claude-pty"`` BEFORE launching (mirrors every other
    interactive flavour) so the board always reflects the in-flight session.
    That field is also what keeps this row out of automatic headless review
    dispatch (#555's generic ``provider_name != "claude-pty"`` guard in
    :func:`coord.review.dispatch_pending_reviews` — ``test-author`` is
    already in :data:`coord.models.WORK_LIKE_TYPES`, so no type-specific
    exclusion was needed). The explicit human-attended handoffs
    (``coord review``, ``coord assign --interactive --review-of/--merge-of``)
    pick this row up exactly like an interactive ``work`` completion — same
    board membership, same ``.branch`` — so there's no new stall to plumb
    around, only the same "human drives Test→Review→Merge" path #1173 asks
    for.

    Returns the child process's exit code (``0`` on a clean exit, or on a
    dry run). Raises :class:`RuntimeError` on any resolution failure — the
    same failure modes as :func:`dispatch_test_author` (unknown repo/driver/
    milestone/issue-membership/machine), plus a worktree/remote-launch setup
    failure (mirrors ``--rework-of``'s #618 failure-reason backstop: the
    reason is recorded on the assignment row before the error is raised, so
    the TUI can explain a red box with no log file) — EXCEPT the #2063
    Gate-A sign-off refusal, which (as in :func:`dispatch_test_author`)
    raises :class:`coord.dispatch.DispatchRefused` instead, so `coord
    acceptance author --interactive`'s CLI handler can map it to
    ``EXIT_DISPATCH_REFUSED``.
    """
    from coord.agent import (  # noqa: PLC0415
        AssignmentSpec,
        _GitError,
        _slugify,
        setup_interactive_worktree,
    )
    from coord.interactive import (  # noqa: PLC0415
        TmuxHost,
        _launch_via_tmux,
        finalize_interactive_exit,
        finalize_remote_interactive_exit,
        launch_human_attended_interactive,
        tmux_available,
        tmux_session_alive,
        tmux_session_name,
    )
    from coord.state import (  # noqa: PLC0415
        build_board,
        record_dispatched_assignment,
        save_board,
        set_assignment_failure_reason,
    )

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
            f"no acceptance driver configured for repo {repo_name!r} "
            "(add it under acceptance.drivers in coordinator.yml)"
        )

    try:
        ctx = fetch_milestone_context(repo_cfg, tracking_issue)
    except MilestoneDispatchError as e:
        raise RuntimeError(str(e)) from e

    if issue_number is not None and ctx.work_order.node(issue_number) is None:
        raise RuntimeError(
            f"issue #{issue_number} is not a member of milestone "
            f"#{ctx.milestone_number}'s work order (tracking issue #{tracking_issue})"
        )

    # #2063: refuse to author a sealed slice against a contract no human has
    # signed off on. This is the path that actually burned money on
    # coord-portal ms-2 — an independent test-author authored ~$2.70 of
    # sealed suite against an unapproved surface, and it happened to be a
    # good contract by luck, not process. The strings a contract pins
    # (button text, data-testid hooks, status vocabulary) become assertions
    # the worker may never edit, so a wrong one is not cosmetic: it is a
    # suite that enforces the wrong product.
    #
    # Raised as `DispatchRefused` (a `ValueError` subclass), NOT a plain
    # `RuntimeError` like every other failure mode in this function: this
    # refusal is deterministic and operator-fixable (`coord gate-a
    # --approved`), not a crash, and `coord acceptance author`'s CLI handler
    # maps THIS exception — and only this one — to `EXIT_DISPATCH_REFUSED`
    # so `coord drive-queue`'s tick can park the entry (#1891/#1892) instead
    # of burning attempts toward terminal `blocked` (#2040) on an unapproved
    # contract. Same distinguishing pattern `enforce_oracle_readiness` (the
    # Work-dispatch guard this mirrors) already uses in `coord/dispatch.py`.
    signoff_refusal = gate_a_signoff_status(repo_cfg, config, ctx.milestone_number)
    if signoff_refusal is not None:
        raise DispatchRefused(signoff_refusal)

    if machine_override:
        machine = next(
            (m for m in config.machines if m.name == machine_override), None
        )
        if machine is None:
            raise RuntimeError(f"machine {machine_override!r} not in coordinator.yml")
        if not machine.can_work_on(repo_name):
            raise RuntimeError(
                f"machine {machine_override!r} does not list repo {repo_name!r}"
            )
    else:
        machine = pick_test_author_machine(config, repo_name, driver_cfg.capability)
        if machine is None:
            cap_note = (
                f" with capability {driver_cfg.capability!r}"
                if driver_cfg.capability else ""
            )
            raise RuntimeError(
                f"no machine claims repo {repo_name!r}{cap_note} — "
                "test-author needs a machine with the repo cloned"
                + (" and the driver's required capability" if cap_note else "")
            )

    repo_path_cfg = machine.repo_path(repo_name)
    if repo_path_cfg is None:
        raise RuntimeError(
            f"machine {machine.name!r} has no repo_path for {repo_name!r}"
        )

    issue_title: str | None = None
    issue_body: str | None = None
    if issue_number is not None:
        try:
            issue_data = github_ops.get_issue(repo_cfg.github, issue_number)
        except RuntimeError as e:
            raise RuntimeError(f"could not fetch #{issue_number}: {e}") from e
        issue_title = issue_data.get("title") or ""
        issue_body = issue_data.get("body") or ""

    ms_dir = f"ms-{ctx.milestone_number}"
    briefing = build_test_author_briefing(
        repo_name=repo_name,
        repo_github=repo_cfg.github,
        ms_dir=ms_dir,
        tracking_issue=tracking_issue,
        milestone_number=ctx.milestone_number,
        milestone_issue_numbers=list(ctx.work_order.issue_numbers),
        driver_kind=driver_cfg.kind,
        driver_run=driver_cfg.run,
        driver_entrypoint=driver_cfg.entrypoint,
        driver_mock=driver_cfg.mock,
        issue_number=issue_number,
        issue_title=issue_title,
        issue_body=issue_body,
        default_branch=repo_cfg.default_branch or "main",
    )

    # Same FIXED title as the headless dispatch (see dispatch_test_author's
    # comment above `assignment_title`) so both modes derive the SAME
    # branch name and continue the same branch/PR across repeated dispatches.
    assignment_title = f"[test-author] ms-{ctx.milestone_number} acceptance suite"
    repo_deny = repo_cfg.worker_permissions.deny if repo_cfg.worker_permissions else []
    deny_commands = list(dict.fromkeys(list(repo_deny) + TEST_AUTHOR_DENY_COMMANDS))
    default_branch = repo_cfg.default_branch or "main"
    branch_name = f"issue-{tracking_issue}-{_slugify(assignment_title)}"

    from coord.commands.dispatch import _build_interactive_launch_setup  # noqa: PLC0415

    setup = _build_interactive_launch_setup(
        machine=machine.name, repo=repo_name, issue=tracking_issue, machine_obj=machine,
        dry_run=dry_run,
    )
    provider = setup.provider
    is_local = setup.is_local
    issue_ctx = setup.issue_ctx
    svc = setup.svc

    if is_local:
        ta_repo_path = str(Path(repo_path_cfg).expanduser())
    else:
        ta_repo_path = repo_path_cfg

    resolved_model = config.models.default
    assignment_id = uuid.uuid4().hex[:12]

    scope = f"issue #{issue_number} slice" if issue_number is not None else "full milestone"
    report_reminder = (
        f"[Coordinator test-author assignment {assignment_id}] HUMAN-ATTENDED "
        f"interactive test-authoring ({scope}) for {repo_cfg.github} milestone "
        f"#{ctx.milestone_number} (tracking issue #{tracking_issue}). Before "
        f"you exit, run `coord report-result --assignment {assignment_id} "
        "--status <done|blocked> --summary <text>` so the coordinator "
        "records the result.\n\n"
    )
    effective_briefing = issue_ctx + report_reminder + briefing

    spec = AssignmentSpec(
        repo_name=repo_name,
        repo_path=ta_repo_path,
        issue_number=tracking_issue,
        issue_title=assignment_title,
        briefing=effective_briefing,
        model=resolved_model,
        type="test-author",
        provider="claude-pty",
        system_prompt=TEST_AUTHOR_INTERACTIVE_SYSTEM_PROMPT,
        deny_commands=deny_commands,
    )
    argv = provider.build_command(spec, resolved_model=resolved_model)
    # Remote: a bare "claude" is not on the SSH login PATH (#424/#425).
    if not is_local:
        argv = ["~/.local/bin/claude"] + list(argv)[1:]

    location = "local TTY" if is_local else f"{machine.host} (remote tmux)"
    click.echo(
        f"{machine.name} ({location}) → TEST-AUTHOR for {repo_cfg.github} "
        f"milestone #{ctx.milestone_number} (tracking issue #{tracking_issue}, "
        f"{scope})"
    )
    click.echo("  mode: HUMAN-ATTENDED interactive test-authoring (#1173)")
    click.echo(f"  assignment id: {assignment_id}  (branch: {branch_name})")

    if dry_run:
        click.echo("  (dry run — not launched)")
        click.echo(f"  would exec: {argv}")
        return 0

    ta_assignment = Assignment(
        machine_name=machine.name,
        repo_name=repo_name,
        issue_number=tracking_issue,
        issue_title=assignment_title,
        briefing=effective_briefing,
        assignment_id=assignment_id,
        status="running",
        branch=branch_name,
        dispatched_at=time.time(),
        type="test-author",
        for_issue_number=issue_number,
        model=resolved_model,
        provider_name="claude-pty",
    )
    record_dispatched_assignment(assignment=ta_assignment, repo_github=repo_cfg.github)
    if svc is None:
        save_board(build_board())
    os.environ["COORD_ASSIGNMENT_ID"] = assignment_id

    if is_local:
        try:
            wt_path, _ = setup_interactive_worktree(
                Path(ta_repo_path),
                issue_number=tracking_issue,
                issue_title=assignment_title,
                assignment_id=assignment_id,
                default_branch=default_branch,
                existing_branch=branch_name,
            )
            worktree_path = str(wt_path)
        except (_GitError, OSError) as wt_err:
            reason = f"worktree-add failed for branch {branch_name}: {wt_err}"
            click.echo(f"  error: {reason}", err=True)
            set_assignment_failure_reason(assignment_id, reason)
            raise RuntimeError(reason) from wt_err
        click.echo(f"  worktree: {worktree_path} (branch: {branch_name})")

        started_at = time.time()
        exit_code = launch_human_attended_interactive(
            argv, effective_briefing, assignment_id=assignment_id, cwd=worktree_path,
        )
        if exit_code != 0:
            click.echo(f"  claude exited with status {exit_code}", err=True)

        sname = tmux_session_name(assignment_id) if tmux_available() else None
        if sname and tmux_session_alive(sname):
            click.echo(
                f"  session still running in tmux: {sname}\n"
                f"  reattach with:  coord reattach {assignment_id}"
            )
            return 0

        try:
            finalize_result = finalize_interactive_exit(
                assignment_id=assignment_id,
                repo_name=repo_name,
                repo_github=repo_cfg.github,
                issue_number=tracking_issue,
                machine_name=machine.name,
                worktree_path=worktree_path,
                base_branch=default_branch,
                exit_code=exit_code,
                started_at=started_at,
                log_path=None,
                repo_path=ta_repo_path,
                branch=branch_name,
            )
            if finalize_result.already_recorded:
                click.echo(
                    "  result recorded via `coord report-result`; backstop "
                    "did not overwrite"
                )
            else:
                click.echo(
                    f"  backstop: status={finalize_result.terminal_status} "
                    f"commits_ahead={finalize_result.commits_ahead}"
                )
                if not finalize_result.push_ok:
                    click.echo(
                        f"  warning: git push failed: {finalize_result.push_error}",
                        err=True,
                    )
        except Exception as exc:  # noqa: BLE001 — best-effort backstop
            click.echo(
                f"  warning: backstop failed to record test-author exit: {exc}",
                err=True,
            )
        return exit_code

    # ── REMOTE (#1173) ──────────────────────────────────────────────────
    # Mirrors _dispatch_rework_of's remote shape (named-branch continuation
    # + finalize_remote_interactive_exit) but WITHOUT its holder-detection
    # retry maze (#759/#814) — test-author dispatch is a low-frequency Gate-A
    # / JIT call, not the hot auto-loop path, so a branch/worktree collision
    # is far less likely than for --rework-of's "resume a specific in-flight
    # session" case. Lift that block from dispatch_workers._dispatch_rework_of
    # if this turns out to need it.
    import shlex

    remote_wt = "$HOME/.coord/worktrees/" + assignment_id
    rp_sh = (
        "$HOME/" + ta_repo_path[2:]
        if ta_repo_path.startswith("~/")
        else ("$HOME" if ta_repo_path == "~" else ta_repo_path)
    )
    claude_args = shlex.join(list(argv)[1:])
    br_q = shlex.quote(branch_name)
    orig_ref = shlex.quote(f"origin/{branch_name}")
    remote_cmd = (
        f"mkdir -p $HOME/.coord/worktrees"
        f" && cd {rp_sh}"
        f" && git fetch origin --prune 2>/dev/null || true"
        f" && git worktree prune 2>/dev/null || true"
        f" && (git worktree add -B {br_q} {remote_wt} {orig_ref} 2>/dev/null"
        f" || git worktree add -b {br_q} {remote_wt} origin/{default_branch})"
        f" && cd {remote_wt}"
        f" && COORD_ASSIGNMENT_ID={assignment_id} {argv[0]} {claude_args}"
    )
    tmux_host = TmuxHost(ssh_target=machine.host)
    sname = tmux_session_name(assignment_id)
    click.echo(
        f"  remote worktree: $HOME/.coord/worktrees/{assignment_id} on "
        f"{machine.host} (branch: {branch_name})"
    )

    if effective_briefing.strip():
        hdr = (
            "--- seeded briefing -- review below; "
            "submit the pre-filled input in Claude to send ---"
        )
        ftr = "-" * len(hdr)
        preview = f"\n{hdr}\n{effective_briefing.rstrip()}\n{ftr}\n\n"
        try:
            os.write(sys.stdout.fileno(), preview.encode("utf-8"))
        except OSError:
            pass

    started_at = time.time()
    rc = _launch_via_tmux(
        argv, effective_briefing, sname, cwd=None, host=tmux_host,
        raw_shell_cmd=remote_cmd,
    )
    if rc is None:
        reason = f"could not create remote tmux session on {machine.host}"
        click.echo(f"  error: {reason}", err=True)
        set_assignment_failure_reason(assignment_id, reason)
        raise RuntimeError(reason)
    exit_code = rc

    if tmux_session_alive(sname, host=tmux_host):
        click.echo(
            f"  session still running in remote tmux: {sname}\n"
            f"  reattach with:  ssh -t {machine.host} tmux attach-session -t {sname}"
        )
        return 0

    try:
        remote_result = finalize_remote_interactive_exit(
            assignment_id=assignment_id,
            repo_name=repo_name,
            repo_github=repo_cfg.github,
            issue_number=tracking_issue,
            machine_name=machine.name,
            ssh_target=machine.host,
            remote_worktree_sh=remote_wt,
            remote_repo_sh=rp_sh,
            branch=branch_name,
            base_branch=default_branch,
            exit_code=exit_code,
            started_at=started_at,
        )
        if remote_result.already_recorded:
            click.echo(
                "  result recorded via `coord report-result`; remote "
                "backstop did not overwrite"
            )
        else:
            click.echo(
                f"  remote backstop: status={remote_result.terminal_status} "
                f"commits_ahead={remote_result.commits_ahead} "
                f"pushed={remote_result.push_ok}"
            )
            if not remote_result.push_ok:
                click.echo(
                    f"  warning: remote push failed: {remote_result.push_error}",
                    err=True,
                )
    except Exception as exc:  # noqa: BLE001 — best-effort backstop
        click.echo(
            f"  warning: remote backstop failed to record test-author exit: {exc}",
            err=True,
        )
    return exit_code
