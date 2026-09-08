"""Plan-mode follow-up commands: `pr`, `fix`, `review`, `approve-plan`,
`reject-plan`, `resume-stuck`, `split`. Extracted from coord/cli.py (#747)."""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path

import click
import httpx

from coord.config import Config
from coord.dispatch import DispatchRefused

from coord.commands._common import AGENT_PORT, _CONFIG_OPTION, _load_config


@click.command(help="Create sub-issues from a split proposal (e.g. coord split S1).")
@click.argument("ids")
@_CONFIG_OPTION
@click.option("--dry-run", is_flag=True, help="Show what would be created.")
def split(ids: str, config_path: Path, dry_run: bool) -> None:
    from coord import github_ops
    from coord.state import load_split_proposals, clear_split_proposals

    cfg = _load_config(config_path)
    splits = load_split_proposals()
    if not splits:
        click.echo("No pending split proposals. Run `coord plan` first.", err=True)
        sys.exit(1)

    try:
        selected_ids = [int(x.strip().lstrip("Ss")) for x in ids.split(",")]
    except ValueError:
        click.echo("error: IDs must be comma-separated (e.g. S1,S2 or 1,2)", err=True)
        sys.exit(2)

    selected = [s for s in splits if s.id in selected_ids]
    missing = set(selected_ids) - {s.id for s in selected}
    if missing:
        click.echo(f"error: unknown split proposal IDs: {missing}", err=True)
        sys.exit(2)

    for s in selected:
        repo = cfg.repo(s.repo_name)
        if repo is None:
            click.echo(f"error: unknown repo {s.repo_name!r}", err=True)
            continue

        click.echo(f"\nSplitting #{s.issue_number}: {s.issue_title} into {len(s.chunks)} sub-issues:")

        child_numbers: list[int] = []
        for j, chunk in enumerate(s.chunks, 1):
            title = f"{chunk.title} (sub-task {j}/{len(s.chunks)} of #{s.issue_number})"
            body = (
                f"## Sub-task of #{s.issue_number} — {s.issue_title}\n\n"
                f"### Scope (chunk {j} of {len(s.chunks)}): {chunk.title}\n\n"
                f"{chunk.scope}\n\n"
                f"### Files likely touched\n\n"
                + "\n".join(f"- `{f}`" for f in chunk.files_likely)
                + f"\n\n### Context\n\n- Parent issue: #{s.issue_number}\n"
            )

            if dry_run:
                click.echo(f"  [{j}] would create: {title}")
                continue

            try:
                result = github_ops.create_issue(
                    repo.github, title, body, labels=["sub-task"],
                )
                child_numbers.append(result["number"])
                click.echo(f"  [{j}] created #{result['number']}: {chunk.title}")
            except RuntimeError as e:
                click.echo(f"  [{j}] failed to create: {e}", err=True)

        if dry_run or not child_numbers:
            continue

        task_list = "\n".join(
            f"- [ ] #{n}" for n in child_numbers
        )
        try:
            github_ops.update_issue_body(
                repo.github, s.issue_number,
                f"Split into sub-tasks:\n\n{task_list}\n",
            )
            click.echo(f"  Parent #{s.issue_number} updated with task list")
        except RuntimeError as e:
            click.echo(f"  Failed to update parent: {e}", err=True)

    if not dry_run:
        clear_split_proposals()
        click.echo("\nSplit proposals cleared. Run `coord plan` to assign the new sub-issues.")


def _dispatch_followup(
    cfg: Config,
    original: Assignment,
    briefing: str,
    *,
    issue_suffix: str = "",
    model: str | None = None,
    type: str = "work",
    files_likely: list[str] | None = None,
    inherit_branch: bool = True,
    machine_name: str | None = None,
) -> str:
    """Dispatch a follow-up assignment for an existing assignment. Returns assignment ID.

    *model* overrides the model tier for the follow-up. When None, the
    dispatcher falls back to ``cfg.models.default``.

    *type* sets the assignment type (``"work"`` or ``"plan"``).  Defaults to
    ``"work"`` so existing callers are unaffected.

    *files_likely* is the list of files the worker is expected to touch.
    When None, an empty list is used (no file constraints).

    *inherit_branch* controls whether the follow-up checks out the parent's
    branch (``target_branch=original.branch``).  True for follow-ups that
    *continue* existing work on the same branch (``coord pr``, smoke-test
    fix-up, continuation).  Must be False when the parent is a read-only
    PLAN assignment: a plan never pushes, its recorded branch is a
    throwaway worktree name (sometimes a stale/wrong capture), and the
    work it spawns must start a FRESH branch derived from the issue.

    *machine_name* (#3208) overrides which machine the follow-up is
    dispatched to — defaults to ``original.machine_name``, same as before
    this parameter existed. A same-branch fix caller that has already
    resolved a live, capable machine via
    :func:`coord.dispatch.select_fix_machine` (because the original worker's
    machine turned out to be unreachable) passes that resolved name here
    instead of unconditionally re-aiming at the original.
    """
    from coord.board_service import read_board, write_board
    from coord.dispatch import dispatch, post_briefing, compute_do_not_touch
    from coord.state import record_dispatched
    from coord.models import Proposal

    repo = cfg.repo(original.repo_name)
    if repo is None:
        raise ValueError(f"Unknown repo: {original.repo_name!r}")

    proposal = Proposal(
        id=0,
        machine_name=machine_name or original.machine_name,
        repo_name=original.repo_name,
        issue_number=original.issue_number,
        issue_title=original.issue_title,
        rationale=f"follow-up for assignment {original.assignment_id}",
        briefing=briefing,
        model=model if model else cfg.models.default,
        type=type,
        files_likely=files_likely if files_likely is not None else [],
        # Pin the follow-up to the parent's branch when one exists AND the
        # caller wants continuation.  Without this, prefixed issue titles
        # like `[fix-1] …` / `[conflict-fix] …` carried into
        # _dispatch_followup (e.g. `coord pr` on a fix-up assignment)
        # cause the agent to slugify the prefixed title and push to an
        # orphan branch instead of the original PR's branch.  But for a
        # plan→work hand-off the parent is read-only and its branch is a
        # throwaway (sometimes wrong) capture, so the work must branch
        # fresh — callers pass inherit_branch=False there.
        target_branch=(original.branch or None) if inherit_branch else None,
    )

    response = dispatch(proposal, cfg)
    assignment_id = response.get("id", "pending")
    record_dispatched(
        assignment_id=assignment_id,
        proposal=proposal,
        repo_github=repo.github,
        provider_name=response.get("_provider_name"),
    )

    # #906: use read_board() to build the peer-conflict in-flight list instead
    # of load_dispatched() — the latter reads the local DB which is empty on a
    # thin client.  read_board() routes to the daemon's /board when configured,
    # so we get the canonical active assignment list for do-not-touch detection.
    # This also consolidates the board read we needed anyway for write_board().
    board = read_board()
    in_flight = [
        {
            "machine_name": a.machine_name,
            "repo_name": a.repo_name,
            "files_likely": a.files_allowed,
        }
        for a in board.active
        if a.assignment_id != assignment_id  # exclude just-dispatched
    ]
    do_not_touch = compute_do_not_touch(proposal, peers=[], in_flight=in_flight)
    post_briefing(proposal, cfg, assignment_id=assignment_id, do_not_touch=do_not_touch)

    write_board(board)

    return assignment_id


def _load_plan_for_assignment(assignment, assignment_id: str) -> dict | None:
    """Retrieve the plan dict for a plan-type assignment.

    Tries (in order):
    1. The plan field cached on the assignment object.
    2. The plans table in the DB (populated by `coord notify`).
    3. Parsing the local log file directly (works when agent is local).

    Returns the plan dict or None if not found.
    """
    from coord.state import COORD_DIR, load_plans

    plan_dict = getattr(assignment, "plan", None)
    if plan_dict is None:
        plans = load_plans()
        plan_dict = plans.get(assignment_id)
    if plan_dict is None:
        local_log = COORD_DIR / "logs" / f"{assignment_id}.log"
        try:
            from coord.plan_parser import parse_plan_from_log  # noqa: PLC0415
            worker_plan = parse_plan_from_log(local_log)
        except Exception:  # noqa: BLE001
            worker_plan = None
        if worker_plan is not None:
            plan_dict = worker_plan.to_dict()
    return plan_dict


def _plan_dict_to_text(plan_dict: dict) -> str:
    """Format a WorkerPlan dict into a human-readable text block for briefings."""
    from coord.plan_parser import WorkerPlan  # noqa: PLC0415

    plan = WorkerPlan.from_dict(plan_dict)
    parts: list[str] = []
    if plan.plan:
        parts.append(f"Summary:\n{plan.plan}")
    if plan.files_modify:
        parts.append("Files to modify:\n" + "\n".join(f"  - {f}" for f in plan.files_modify))
    if plan.approach:
        parts.append(f"Approach:\n{plan.approach}")
    if plan.risks:
        parts.append(f"Risks:\n{plan.risks}")
    if plan.estimate:
        parts.append(f"Estimate:\n{plan.estimate}")
    # Smoke tests authored at planning time — the work worker re-emits
    # these (refining if needed) in its own SMOKE_TESTS block before
    # exit.  Surfacing them in the briefing lets the worker copy them
    # verbatim when the change matches the plan.
    if plan.smoke_tests:
        bullets = "\n".join(f"  - {b}" for b in plan.smoke_tests)
        parts.append(f"Smoke tests (from plan — re-emit in your SMOKE_TESTS block):\n{bullets}")
    elif plan.smoke_tests == []:
        parts.append(
            "Smoke tests (from plan): (none — change is internal). "
            "Emit `SMOKE_TESTS: (none — change is internal)` in your block."
        )
    # Fall back to raw_text when no structured sections were found.
    if not parts:
        return plan.raw_text or "(no plan text)"
    return "\n\n".join(parts)


@click.command(help="Dispatch a worker to create a PR for a completed assignment.")
@click.argument("assignment_id")
@_CONFIG_OPTION
@click.option(
    "--no-review",
    is_flag=True,
    default=False,
    help="Skip auto-dispatching an adversarial review after the PR worker.",
)


def pr(assignment_id: str, config_path: Path, no_review: bool) -> None:
    from coord.board_service import read_board, write_board

    cfg = _load_config(config_path)
    board = read_board()

    assignment = board.find_by_id(assignment_id)
    if assignment is None:
        click.echo(f"error: assignment {assignment_id!r} not found in board", err=True)
        sys.exit(1)

    if assignment.status != "done":
        click.echo(
            f"error: assignment {assignment_id} is {assignment.status!r}, "
            "can only create a PR for done assignments",
            err=True,
        )
        sys.exit(1)

    if not assignment.branch:
        click.echo(
            f"error: assignment {assignment_id} has no branch recorded. "
            "The worker may not have pushed yet.",
            err=True,
        )
        sys.exit(1)

    repo = cfg.repo(assignment.repo_name)
    if repo is None:
        click.echo(f"error: unknown repo {assignment.repo_name!r}", err=True)
        sys.exit(1)

    default_branch = repo.default_branch
    # #1077: "mock-author" (Gate A) assignments' issue_number is the
    # milestone's tracking issue, not something this PR resolves — closing
    # it on merge would wrongly flip the epic to "done" while its real
    # sub-issues are untouched. Only "work"-type PRs get the closing
    # keyword; everything else gets a non-closing reference.
    from coord.models import CLOSES_ISSUE_TYPES, PR_HELPER_TYPE  # noqa: PLC0415

    closes_issue = assignment.type in CLOSES_ISSUE_TYPES

    # #1314: the #1077 guard above assumed only mock-author/test-author are
    # ever dispatched directly against a tracking issue's own number — but a
    # type="work" assignment can land there too (e.g. a Gate-A contract
    # correction with no properly-typed tool yet, see #1314). Closing an
    # epic on merge because a single corrective PR touched its number would
    # wrongly flip it to "done" while its real children sit untouched, so
    # check the issue's own labels regardless of assignment type. Fail-open
    # (network hiccup, unknown repo, ...) — keep the type-only verdict
    # rather than block PR creation on a GitHub read.
    if closes_issue:
        try:
            from coord import github_ops  # noqa: PLC0415
            from coord.milestone_order import TRACKING_ISSUE_LABEL  # noqa: PLC0415

            issue_data = github_ops.get_issue(repo.github, assignment.issue_number)
            issue_labels = {
                lbl.get("name", "") for lbl in (issue_data.get("labels") or [])
            }
            if TRACKING_ISSUE_LABEL in issue_labels:
                closes_issue = False
        except Exception:  # noqa: BLE001 — fail open, see docstring above
            pass

    ref_keyword = (
        f"Closes #{assignment.issue_number}"
        if closes_issue
        else f"Refs #{assignment.issue_number}"
    )
    briefing = (
        f"You are on branch {assignment.branch}. The code is complete and tests pass.\n"
        f"Create a PR from {assignment.branch} to {default_branch} for issue #{assignment.issue_number}.\n"
        f"Title: {assignment.issue_title}\n\n"
        f"Use gh pr create. Read the diff (git fetch origin && git diff origin/{default_branch}...HEAD) and write a clear\n"
        f"summary of what changed. Reference the issue with \"{ref_keyword}\".\n"
        f"Do NOT modify any code — only create the PR."
    )

    # #1142: only give the PR-opening helper `type="work"` when the original
    # assignment's own type actually resolves `issue_number` (mirrors the
    # Closes/Refs split above). Otherwise (test-author/mock-author/etc, whose
    # issue_number is a milestone tracking issue) the helper gets a distinct
    # `PR_HELPER_TYPE` so it can never be mistaken for that tracking issue's
    # own merged work by `coord.stage_projection.merge_stage_status_for` (or
    # any other heuristic keyed on `type == "work"` / `CLOSES_ISSUE_TYPES`).
    followup_type = "work" if closes_issue else PR_HELPER_TYPE

    try:
        new_id = _dispatch_followup(cfg, assignment, briefing, type=followup_type)
    except httpx.HTTPError as e:
        click.echo(f"error: dispatch failed: {e}", err=True)
        sys.exit(1)
    except ValueError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    click.echo(f"PR worker dispatched (assignment {new_id})")
    click.echo(f"  branch: {assignment.branch} → {default_branch}")
    click.echo(f"  issue: #{assignment.issue_number}: {assignment.issue_title}")

    if not no_review and cfg.reviews.enabled:
        from coord.review import dispatch_review

        fresh_board = read_board()
        review = dispatch_review(assignment, fresh_board, cfg)
        if review is not None:
            write_board(fresh_board)
            click.echo(f"Review dispatched (assignment {review.assignment_id})")
            click.echo(f"  reviewer: {review.machine_name}")
        else:
            # #1627: report the specific guard dispatch_review hit rather
            # than a generic guess.
            reason = assignment.review_dispatch_reason or "reason not recorded"
            click.echo(f"  review not dispatched: {reason}")


@click.command(
    help=(
        "Deliberately dispatch a headless review for a done work assignment "
        "(the #555 escape hatch — thin wrapper over dispatch_review)."
    )
)
@click.argument("assignment_id")
@_CONFIG_OPTION
def review(assignment_id: str, config_path: Path) -> None:
    """``coord review <work_assignment_id>``

    The #555 guard in ``dispatch_pending_reviews`` deliberately never
    auto-dispatches a headless ``claude -p`` review for an *interactive*
    (``provider_name="claude-pty"``) work completion — that path already had
    a human attending it. Its comment promises an escape hatch for a human
    to deliberately request a headless review anyway; this command is it.

    Pure dispatch — no PR worker is spawned. :func:`coord.review.dispatch_review`
    already opens (or reuses) the PR itself, so unlike ``coord pr`` there is no
    ``_dispatch_followup`` work session in between.
    """
    from coord.board_service import read_board, write_board
    from coord.claim import has_active_followup
    from coord.review import dispatch_review

    cfg = _load_config(config_path)
    board = read_board()

    assignment = board.find_by_id(assignment_id)
    if assignment is None:
        click.echo(f"error: assignment {assignment_id!r} not found in board", err=True)
        sys.exit(1)

    if assignment.status != "done":
        click.echo(
            f"error: assignment {assignment_id} is {assignment.status!r}, not "
            "'done' — nothing to review yet",
            err=True,
        )
        sys.exit(1)

    if not assignment.branch:
        click.echo(
            f"error: assignment {assignment_id} has no branch recorded. "
            "The worker may not have pushed yet.",
            err=True,
        )
        sys.exit(1)

    if has_active_followup(
        board, of_assignment_id=assignment_id, assignment_type="review"
    ):
        click.echo(
            f"error: a review is already in flight for {assignment_id}",
            err=True,
        )
        sys.exit(1)

    if not cfg.reviews.enabled:
        click.echo(
            "error: reviews are disabled (reviews.enabled: false in config)",
            err=True,
        )
        sys.exit(1)

    review_assignment = dispatch_review(assignment, board, cfg)
    if review_assignment is None:
        # #1627: dispatch_review() records *why* it declined on the
        # assignment itself (review_dispatch_reason) — print that verbatim
        # instead of guessing at a cause. (It used to be a bare `return
        # None` from any of 11 guards, most of which never logged anything,
        # so this message used to send operators chasing a log entry that
        # didn't exist — see #1627.)
        reason = assignment.review_dispatch_reason or (
            "no reason recorded — this is itself a bug in dispatch_review; "
            "please report it"
        )
        click.echo(f"error: no review dispatched for {assignment_id} — {reason}", err=True)
        sys.exit(1)

    # Persist only on success: dispatch_review mutates the board in place
    # (review_state, pr_url, the new review row).
    write_board(board)
    click.echo(
        f"review dispatched: {review_assignment.assignment_id} on "
        f"{review_assignment.machine_name}"
    )
    if assignment.pr_url:
        click.echo(f"  pr: {assignment.pr_url}")


@dataclass(frozen=True)
class CiRead:
    """#2091: the outcome of the live-CI read, with "did not read" separated
    from "read it, it was green".

    Before #2091 this path collapsed both into a bare ``None``, and the
    refusal message told the operator only that no red CI was found.  In
    coord-portal #14 the PR *was* red (``gh pr checks 42`` said so) — the
    read had simply never happened, and the operator was left to guess which
    of six silent short-circuits fired.  ``unread_reason`` names it.

    Invariants:

    * ``story`` set                          → CI was read and is RED.
    * ``story is None`` and no ``unread_reason``  → CI was read, nothing red.
    * ``unread_reason`` set                  → **no read happened**; this is
      not evidence of green, and callers must say so out loud.
    """

    story: str | None = None
    unread_reason: str | None = None
    pr_number: int | None = None

    @property
    def is_red(self) -> bool:
        return self.story is not None

    @property
    def was_read(self) -> bool:
        return self.unread_reason is None


def _resolve_pr_number(repo_github: str, assignment) -> int | None:
    """PR number for *assignment*, from its ``pr_url`` or (#2091) its branch.

    The stored ``pr_url`` is missing on any row whose PR was opened out of
    band, or that predates PR creation — and that alone used to silently
    disable the whole live-CI fallback.  Fall back to asking GitHub which PR
    has this branch as its head, which is the same question ``gh pr checks``
    answers from a checkout.
    """
    from coord.drive import _extract_pr_number

    pr_number = _extract_pr_number(getattr(assignment, "pr_url", None) or "")
    if pr_number is not None:
        return pr_number

    branch = getattr(assignment, "branch", None)
    if not branch:
        return None
    try:
        from coord import github_ops  # noqa: PLC0415

        pr = github_ops.find_pr_for_branch(repo_github, branch)
    except Exception:  # noqa: BLE001 — advisory lookup, never fatal
        return None
    if not isinstance(pr, dict):
        return None
    number = pr.get("number")
    return number if isinstance(number, int) else None


def _read_ci(cfg: Config, assignment) -> CiRead:
    """#1622 (part 3): a rendered summary of *assignment*'s FAILED CI checks.

    The third legitimate fix trigger, alongside a request-changes review and a
    failed local test gate.  CI reports failures the local Test stage cannot
    see — vimcode #613 was a sibling path-dep that only breaks in a clean
    checkout — and the fix has to land on the *existing* branch, because the
    thing being fixed is what gates the merge.

    Read-only and fail-quiet, but no longer fail-*silent* (#2091): every path
    that declines to read returns a :class:`CiRead` carrying the reason, so
    the caller can distinguish "CI is green" from "nobody looked".
    """
    from coord.ci_store import build_ci_store, failed_checks, summarize

    repo = cfg.repo(assignment.repo_name)
    if repo is None:
        return CiRead(unread_reason=f"repo {assignment.repo_name!r} is not in coordinator.yml")
    if not repo.github:
        return CiRead(
            unread_reason=f"repo {repo.name!r} has no `github:` slug configured"
        )

    # Availability first (#2091): `_resolve_pr_number`'s branch fallback can
    # shell out to `gh`, and there is no point paying for that when the store
    # will refuse to read anything anyway.
    store = build_ci_store(
        cfg.ci_store.type, host=cfg.ci_store.host, token_env=cfg.ci_store.token_env
    )
    if not store.is_available:
        return CiRead(
            unread_reason=(
                f"ci_store.type is {cfg.ci_store.type!r}, so live CI is never "
                "read — set it to 'github' in coordinator.yml"
            ),
        )

    pr_number = _resolve_pr_number(repo.github, assignment)
    if pr_number is None:
        return CiRead(
            unread_reason=(
                "no PR could be resolved for this row (its `pr_url` is empty "
                f"and no open PR has head branch {getattr(assignment, 'branch', None)!r})"
            )
        )

    try:
        checks = store.list_checks_for_pr(repo.github, pr_number)
    except Exception as exc:  # noqa: BLE001 — CI read is advisory, never fatal
        click.echo(f"warning: could not read CI checks for PR #{pr_number}: {exc}", err=True)
        return CiRead(
            pr_number=pr_number,
            unread_reason=f"the CI read for PR #{pr_number} raised: {exc}",
        )

    if not checks:
        return CiRead(
            pr_number=pr_number,
            unread_reason=f"PR #{pr_number} reported no checks at all",
        )

    failed = failed_checks(checks)
    if not failed:
        # A genuine read that found nothing red. Note this still includes the
        # case where every check is *pending* — `failed_checks` only considers
        # completed ones — so say which, rather than implying a settled green.
        if not any(c.status == "completed" for c in checks):
            return CiRead(
                pr_number=pr_number,
                unread_reason=(
                    f"every check on PR #{pr_number} is still running "
                    f"({summarize(checks)}) — no completed verdict yet"
                ),
            )
        return CiRead(pr_number=pr_number)

    lines = [
        f"CI on PR #{pr_number} is RED ({summarize(checks)}).",
        "",
        "Failed checks:",
    ]
    for c in failed:
        lines.append(f"- {c.name} — conclusion={c.conclusion!r}{(' — ' + c.url) if c.url else ''}")
    lines += [
        "",
        "These failures were NOT visible to the local Test stage. Read the run "
        "logs (`gh run view <id> --log-failed`, or open the URLs above), "
        "reproduce locally where you can, and fix the root cause on THIS "
        "branch — a new branch would not carry the change CI is gating on.",
    ]
    return CiRead(story="\n".join(lines), pr_number=pr_number)


def _fix_from_review(
    cfg: Config,
    board,
    review,
    *,
    guidance: str,
    force: bool,
    machine_override: str | None = None,
) -> None:
    """#1622: dispatch a HEADLESS fix round for a request-changes review.

    This is a CLI **door** onto the review→fix auto-loop, not a second
    implementation of same-branch dispatch.  It hands the review row straight
    to :func:`coord.auto_loop.process_review_completion`, the same function the
    ``coord notify`` transition path calls, so the fix worker is produced by the
    one and only :func:`coord.auto_loop._dispatch_fix` — which pins
    ``target_branch`` to the reviewed work's branch and bumps
    ``review_iteration``.  Every guard that path applies applies here unchanged:
    ``pipeline.auto_loop``, the #476/#1456 approve-with-nits gate, the #522
    terminal-work guard, and the ``max_review_iterations`` cap.

    Two things are added on top, both *before* the shared call:

    - the #555 interactive exclusion, checked against the reviewed WORK row so
      an interactive completion is never followed by a headless fix; and
    - resolution of the review's log path / machine host, which the transition
      path gets handed by ``coord notify`` and a CLI invocation does not.

    Never returns — always exits via :func:`sys.exit`.
    """
    from coord import auto_loop
    from coord.board_service import write_board
    from coord.state import COORD_DIR, add_issue_context_entry

    work = None
    if review.review_of_assignment_id:
        work = board.find_by_id(review.review_of_assignment_id)
    if work is None:
        click.echo(
            f"error: review {review.assignment_id} has no linked work assignment "
            f"on the board (review_of_assignment_id="
            f"{review.review_of_assignment_id!r}) — nothing to fix",
            err=True,
        )
        sys.exit(1)

    # #2092: --guidance used to be silently discarded here — a warning was
    # printed immediately above a success line the operator often doesn't
    # read past the tail of, so a maintainer decision could be lost with no
    # visible trace. Route it through the #603 pinned per-issue context store
    # instead of dropping it OR bolting a second implementation of fix-
    # briefing assembly onto this CLI door: `_dispatch_fix_for_review`
    # already prepends `issue_context_block(...)` to the TOP of every fix
    # briefing it builds, ABOVE the reviewer's findings, so a pinned entry
    # written here lands exactly where the operator's guidance belongs — and
    # (unlike a one-shot parameter) it also survives into every later
    # briefing for this issue, not just this one dispatch.
    if guidance:
        add_issue_context_entry(
            work.repo_name, work.issue_number, guidance,
            pinned=True, source="coord fix --guidance",
        )
        click.echo(
            f"guidance pinned to issue #{work.issue_number} context (#603) — "
            "it will be prepended above the reviewer's findings in the fix "
            "briefing.",
        )

    # #555: an INTERACTIVE work completion must never be silently followed by a
    # headless fix.  The human at the tmux pane owns that branch and may still
    # be mid-edit; a `claude -p` worker pushing over them is the incident that
    # exclusion exists to prevent.  The auto-loop enforces this on the
    # re-review side (`run_for_fix_transition`); a CLI door onto the *dispatch*
    # side needs its own check, because `process_review_completion` never sees
    # the provider.  `--force` is the deliberate override for the case the
    # exclusion cannot detect: the session is genuinely gone.
    if (work.provider_name or "") == "claude-pty" and not force:
        click.echo(
            f"error: work {work.assignment_id} ran INTERACTIVELY "
            "(provider=claude-pty); refusing to follow it with a headless fix "
            "(#555). Continue in that session, or run `coord assign ... "
            f"--fix-of {review.assignment_id} --interactive`, or pass --force "
            "if the interactive session is really gone.",
            err=True,
        )
        sys.exit(1)

    # The transition path is handed these by `coord notify`; reconstruct them
    # so the findings loader keeps all four of its sources (DB cache → local
    # log → agent HTTP → GitHub message bus) rather than only the first and last.
    _log = COORD_DIR / "logs" / f"{review.assignment_id}.log"
    log_path = str(_log) if _log.exists() else None
    machine_host = None
    _machine = next(
        (m for m in cfg.machines if m.name == review.machine_name), None
    )
    if _machine is not None and _machine.host:
        machine_host = _machine.host

    before = {a.assignment_id for a in board.active}
    actions = auto_loop.process_review_completion(
        review, board, cfg, log_path=log_path, machine_host=machine_host,
        override_machine=machine_override,
    )

    # `process_review_completion` mutates the board and leaves persistence to
    # its caller — same contract, same kind list, as the transition path.
    if any(a.kind in auto_loop.PERSIST_ACTION_KINDS for a in actions):
        write_board(board)

    kinds = {a.kind for a in actions}
    detail = "; ".join(a.detail for a in actions if a.detail)

    if "fix_dispatched" in kinds:
        max_iter = cfg.pipeline.max_review_iterations
        click.echo(f"Fix worker dispatched ({detail})")
        for row in board.active:
            if row.assignment_id in before:
                continue
            click.echo(f"  assignment: {row.assignment_id}")
            click.echo(f"  branch: {row.branch}")
            click.echo(f"  review iteration: {row.review_iteration}/{max_iter}")
        click.echo(f"  issue: #{work.issue_number}: {work.issue_title}")
        if work.pr_url:
            click.echo(f"  pr: {work.pr_url}")
        return

    # Everything else is a refusal.  Each one is a guard doing its job, so say
    # which guard and exit non-zero — a headless drive keys off the exit code.
    _why = {
        "disabled": (
            "pipeline.auto_loop is disabled in coordinator.yml — the review→fix "
            "path this command opens is switched off"
        ),
        "no_findings": (
            "no structured review findings could be resolved (DB cache, local "
            "log, agent HTTP and the GitHub findings comment were all empty)"
        ),
        "approved": "review verdict is approve — nothing to fix",
        "approved_with_nits": (
            "review raised no blocking findings, only advisory ones — the #476 "
            "gate advanced the pipeline instead of dispatching a fix"
        ),
        "max_iterations": (
            "pipeline.max_review_iterations reached — raise it in "
            "coordinator.yml or take the branch over by hand"
        ),
        "terminal_skip": (
            "the reviewed work is already merged/closed on GitHub (#522)"
        ),
        "no_work_found": "fix dispatch failed",
    }
    reason = next(
        (_why[a.kind] for a in actions if a.kind in _why),
        f"auto-loop returned {sorted(kinds) or 'nothing'}",
    )
    click.echo(f"error: no fix dispatched for review {review.assignment_id}: {reason}", err=True)
    if detail:
        click.echo(f"  {detail}", err=True)
    sys.exit(1)


@click.command(
    help=(
        "Dispatch a headless same-branch fix worker.\n\n"
        "ASSIGNMENT_ID is either a WORK assignment whose test gate FAILED "
        "(or whose PR has red CI, or whose oracle-loop acceptance trust "
        "gate FAILED — #2344), or a REVIEW assignment whose verdict was "
        "request-changes. Either way the fix lands on the ORIGINAL branch and "
        "updates the ORIGINAL PR — no new issue-N-* branch, no orphan PR."
    )
)
@click.argument("assignment_id")
@_CONFIG_OPTION
@click.option("--guidance", default="", help="Additional guidance for the fix-up worker.")
@click.option(
    "--force",
    is_flag=True,
    help=(
        "Override the #555 interactive-work exclusion when fixing a review of "
        "work that ran under claude-pty, or (#2051) dispatch a WORK-row fix "
        "when neither the test verdict, a live CI read, nor the acceptance "
        "trust gate shows a failure — for when the caller knows the PR is red "
        "but the check missed it (ci_store not configured, a transient read "
        "error). Since #2091 the refusal names which of those it was, so "
        "check the `note:` line before reaching for this. Does NOT override "
        "max_review_iterations or the #522 terminal-work guard."
    ),
)
@click.option(
    "--machine",
    "machine_override",
    default=None,
    help=(
        "Redirect this same-branch fix to a specific machine instead of the "
        "original worker's (#3208) — e.g. when `coord status` shows the "
        "original asleep/unreachable. The branch is fetched from the remote, "
        "so any machine configured for this repo works. Without this flag, "
        "an unreachable original now falls back to another capable, reachable "
        "machine automatically; this flag only matters when you want a "
        "SPECIFIC one, or every automatic fallback was also unreachable."
    ),
)
def fix(
    assignment_id: str, config_path: Path, guidance: str, force: bool,
    machine_override: str | None,
) -> None:
    from coord.board_service import read_board
    from coord.state import COORD_DIR

    cfg = _load_config(config_path)
    board = read_board()

    assignment = board.find_by_id(assignment_id)
    if assignment is None:
        click.echo(f"error: assignment {assignment_id!r} not found in board", err=True)
        sys.exit(1)

    # #1622: a REVIEW id is the request-changes door.  It routes through
    # `coord.auto_loop`, which owns same-branch fix dispatch — this command
    # deliberately does not grow a second implementation of it.
    if assignment.type == "review":
        _fix_from_review(
            cfg, board, assignment, guidance=guidance, force=force,
            machine_override=machine_override,
        )
        return

    # #1384: gate on the canonical `test_state` with the legacy `smoke_test`
    # mirror as fallback, so the two fields can never drift apart again.  The
    # writer (`state._record_test_verdict_local`) now derives the mirror, but
    # rows recorded before that fix — a headless smoke failure via #1021's
    # `coord/notify.py` propagation — carry `test_state='failed'` with
    # `smoke_test=NULL` and must still be fixable.
    test_failed = assignment.test_state == "failed" or assignment.smoke_test == "fail"

    # #2344: the oracle-loop TRUST GATE (docs/ORACLE_LOOP.md, #2199) is a
    # fourth door onto this command. `_decide_acceptance_gate`
    # (coord/drive.py) dispatches `coord fix <work_aid>` the instant the
    # gate's coordinator-run re-run of the sealed suite comes back red — the
    # same shape as `_decide_test` dispatching on a failed Test verdict.
    # Before this, nothing here recognized that door: a milestone whose
    # trust gate failed in isolation (Test not yet run, or already passed;
    # CI green) hit the #2051 "expected a failed test verdict" refusal on
    # every attempt — structurally unwinnable, not a transient miss — until
    # the drive-queue entry parked after burning its retry budget (ms-65 /
    # #2282, observed live 2026-08-17).
    acceptance_failed = assignment.acceptance_state == "failed"

    # #3208: a failed human/customer UAT verdict (`coord uat --failed` or a
    # customer portal `preview.changes_requested`, #2687/#3188) is a FIFTH
    # door — distinct from the four above, so it does NOT by itself skip the
    # `--force` gate below (nothing here has independently confirmed the PR
    # is red the way a test/CI/acceptance failure does). What it DOES supply
    # is evidence to brief the fix worker with: `uat_reason` already holds
    # the full write-up of what's wrong, so a caller forcing a fix for a UAT
    # failure should not have to retype it via `--guidance`.
    uat_failed = assignment.uat_state == "failed"

    # #1622 (part 3): red CI is the third trigger.  Only consulted when the
    # local test gate has NOT already failed, so the cheap in-DB path stays
    # zero-I/O and the existing failure story keeps priority. #2344: same
    # treatment for a failed trust gate — it is definitive evidence on its
    # own, so skip the live CI read entirely rather than let a green CI read
    # (which says nothing about the trust gate's sealed-suite re-run) shadow
    # it below.
    ci_read = CiRead() if (test_failed or acceptance_failed) else _read_ci(cfg, assignment)
    ci_story = ci_read.story

    # #2091: the stored Test verdict says PASSED while the branch's live CI
    # says RED — the coord-portal #14 shape.  Both are "the tests", and they
    # disagree; that is a fact about the Test gate, not a detail of this
    # dispatch, so name it rather than quietly preferring one.
    #
    # #2244: this used to ASSERT the cause ("the Test stage ran a narrower
    # suite than CI — set ci_command") without ever having measured it. On
    # #2230 that diagnosis was simply wrong: the Test stage ran the FULL
    # suite, CI ran the same tests, the same five failed in both — and the
    # green verdict came from the headless smoke's unwired verdict channel
    # (a `claude -p` session exits 0 whatever the suite did, so `SMOKE: fail`
    # was recorded as `passed`). Acting on the old message would have changed
    # nothing and buried the real defect under config. So: state the conflict,
    # list the candidate causes, and point at the evidence that distinguishes
    # them, rather than naming one.
    if ci_read.is_red and (
        assignment.test_state == "passed" or assignment.smoke_test == "pass"
    ):
        stored_verdict = assignment.test_state or assignment.smoke_test
        click.echo(
            f"conflict (#2091): assignment {assignment_id} has a stored Test "
            f"verdict of {stored_verdict!r} "
            f"but CI on PR #{ci_read.pr_number} is RED on the same branch. "
            "Trusting CI. Which of the two is wrong is not determined here — "
            "check, in this order: (1) what the Test stage actually ran and "
            f"reported (`coord log {assignment_id}` → its `SMOKE:` verdict "
            "line and suite summary); (2) whether that suite is narrower than "
            f"CI's (if so, set repos[{assignment.repo_name}].ci_command to "
            "what CI runs); (3) whether the failures are environmental on the "
            "Test machine but not in CI.",
            err=True,
        )

    # #2051 (extended by #2344): none of the four doors (failed test
    # verdict, red CI, a failed acceptance trust gate, or the review-id door
    # handled above) is open.  `_read_ci` is read-only and
    # fail-quiet (see :class:`CiRead`) — a missing story means "no CI
    # evidence of a failure", never "CI is green" — so a caller who KNOWS
    # the PR is red (ci_store not configured for this repo, a transient
    # GitHub API error, a check that hasn't reported yet) would otherwise be
    # stranded with no headless door onto the ORIGINAL branch.  `--force` is
    # that caller's release valve; it is NOT a way to skip the #555 / #522 /
    # max_review_iterations guards, which sit elsewhere.
    forced_without_evidence = False
    if not test_failed and not acceptance_failed and ci_story is None:
        if not force:
            click.echo(
                f"error: assignment {assignment_id} test_state is "
                f"{assignment.test_state!r} / smoke_test is "
                f"{assignment.smoke_test!r} / acceptance_state is "
                f"{assignment.acceptance_state!r}, expected a failed test "
                "verdict (or red CI on its PR, a failed acceptance trust "
                "gate, or a request-changes review id). If the PR is "
                "actually red and this check missed it, re-run with --force.",
                err=True,
            )
            # #2091: say WHY there is no CI evidence.  Before this, six
            # distinct short-circuits (unconfigured ci_store, a row with no
            # pr_url, an unknown repo, ...) were all indistinguishable from
            # "CI is green", so an operator staring at a red `gh pr checks`
            # had no way to tell that the documented fallback never ran.
            if not ci_read.was_read:
                click.echo(
                    "  note: live CI was NOT read for this row, so this is "
                    f"not a green-CI finding — {ci_read.unread_reason}.",
                    err=True,
                )
            elif ci_read.pr_number is not None:
                click.echo(
                    f"  note: live CI on PR #{ci_read.pr_number} was read and "
                    "reported no failing completed check.",
                    err=True,
                )
            sys.exit(1)
        if not guidance and uat_failed and assignment.uat_reason:
            # #3208: the board already holds a full write-up of what's
            # broken (`uat_reason` — the Gate-A/preview divergence detail a
            # customer or operator recorded via `coord uat --failed`) — don't
            # make the caller retype it.
            guidance = assignment.uat_reason
            click.echo(
                f"  guidance: defaulted to uat_reason (#3208, {len(guidance)} "
                "chars) — pass --guidance explicitly to override",
            )
        if not guidance:
            # There's no failed verdict, CI read, uat_reason, or test-output
            # file to brief the fix worker with — `--force` alone would
            # dispatch it blind. Require the caller to say what's actually
            # broken.
            click.echo(
                f"error: --force on assignment {assignment_id} also needs "
                "--guidance — there's no failed test verdict, CI read, or "
                "UAT reason to brief the fix worker with, so say what's "
                "actually broken.",
                err=True,
            )
            sys.exit(1)
        forced_without_evidence = True

    repo = cfg.repo(assignment.repo_name)
    if repo is None:
        click.echo(f"error: unknown repo {assignment.repo_name!r}", err=True)
        sys.exit(1)

    default_branch = repo.default_branch

    # Load stored test output if available
    test_output = ""
    test_output_file = COORD_DIR / "test_output" / f"{assignment_id}.txt"
    if ci_story is not None:
        test_output = ci_story
    elif acceptance_failed and not test_failed:
        # #2344: the trust gate's own recorded reason is the evidence for
        # this door — `coord acceptance record` writes it to
        # `acceptance_reason` at the same time it flips `acceptance_state`
        # to "failed" (coord/state.py). Neither the CI-story nor the
        # smoke/test-reason fallbacks below apply here: this door fires
        # precisely when Test/CI are NOT what's red.
        test_output = assignment.acceptance_reason or ""
    elif uat_failed and not test_failed and assignment.uat_reason:
        # #3208: same shape as the acceptance-trust-gate branch above — a
        # UAT failure's own recorded reason IS the evidence for this door.
        test_output = assignment.uat_reason
    elif test_output_file.exists():
        test_output = test_output_file.read_text()
    elif assignment.smoke_test_reason or assignment.test_reason:
        # #1337: the board wire carries a bounded PREVIEW of the reason text;
        # the briefing quotes it verbatim — prefer the full text via the
        # detail loader (falls back to the board-carried value).
        # #1384: fall back to `test_reason` too — a headless-smoke failure
        # recorded before the mirror-derivation fix has no smoke_test_reason.
        from coord.state import load_assignment_test_reason as _load_tr  # noqa: PLC0415

        test_output = (
            _load_tr(assignment_id)
            or assignment.smoke_test_reason
            or assignment.test_reason
            or ""
        )

    guidance_text = guidance or "Fix the failing tests and push."
    if forced_without_evidence and uat_failed and assignment.uat_reason:
        # #3208: a real, board-recorded UAT failure — not the caller merely
        # attesting the PR is red — so give it its own heading rather than
        # the generic "unverified" one below.
        _what = f"a failed UAT verdict ({assignment.uat_actor or 'operator'}, #2687/#3208)"
        _failure_heading = "UAT failure"
    elif forced_without_evidence:
        _what = (
            "a reported failure (--force: neither a failed test verdict nor "
            "an automated CI read found one — the caller attests the PR is "
            "red)"
        )
        _failure_heading = "Reported failure (--force, unverified)"
    elif acceptance_failed and not test_failed and ci_story is None:
        _what = "a failed oracle-loop acceptance trust gate (#2199)"
        _failure_heading = "Acceptance trust-gate failure"
    else:
        _what = "red CI" if ci_story is not None else "a failed smoke test"
        _failure_heading = "CI failure" if ci_story is not None else "Test failure"

    briefing = (
        f"You are fixing {_what} for issue #{assignment.issue_number}: {assignment.issue_title}\n\n"
        f"The previous worker created branch {assignment.branch}. You are already on that branch.\n"
        f"Do NOT start over — work from the existing code.\n\n"
        f"## What was done\n"
        f"The previous worker's changes are already committed on this branch.\n"
        f"Run `git fetch origin && git log --oneline origin/{default_branch}..HEAD` to see what was done.\n"
        f"Run `git diff origin/{default_branch}...HEAD` to see the full diff.\n\n"
        f"## {_failure_heading}\n"
        f"{test_output}\n\n"
        f"## Guidance\n"
        f"{guidance_text}\n\n"
        f"## Rules\n"
        f"- Do NOT start over or rewrite from scratch\n"
        f"- Fix the specific test failures\n"
        f"- Commit your fixes and push with git push origin HEAD"
    )

    # #3208: pick a machine to run the fix on BEFORE ever POSTing — same
    # selector the review-verdict door uses (`coord.dispatch.
    # select_fix_machine`), so the two doors onto `coord fix` can never
    # disagree about "which machine" (#2096). Prefers the original worker's
    # machine, but a live reachability probe (not just config capability)
    # skips it — and falls through to another capable, reachable machine —
    # the moment it's asleep/offline, instead of the previous unconditional
    # re-aim that surfaced only as a bare `dispatch failed: timed out` with
    # no named cause (format-converter#6 / #3208).
    from coord.dispatch import describe_fix_machine_failure, select_fix_machine

    selection = select_fix_machine(
        original_machine_name=assignment.machine_name,
        repo_name=assignment.repo_name,
        machines=cfg.machines,
        override_machine_name=machine_override,
    )
    if selection.machine is None:
        click.echo(
            f"error: cannot dispatch fix for assignment {assignment_id}: "
            + describe_fix_machine_failure(
                assignment.machine_name, selection,
                override_machine_name=machine_override,
            ),
            err=True,
        )
        sys.exit(1)
    if selection.machine.name != assignment.machine_name:
        # #586 (mirrored from `coord.auto_loop._dispatch_fix`): routing to a
        # DIFFERENT machine than the original worker only works if the
        # branch actually exists on the remote for the new machine to fetch
        # — otherwise the fix worker starts from an empty checkout with no
        # commits and no clear error. A WORK assignment reaching this arm of
        # `coord fix` has normally already cleared Test/Review (both of
        # which require a pushed branch), but check rather than assume.
        from coord import github_ops  # noqa: PLC0415

        repo_for_check = cfg.repo(assignment.repo_name)
        if (
            repo_for_check is not None
            and assignment.branch
            and not github_ops.branch_exists_on_remote(
                repo_for_check.github, assignment.branch
            )
        ):
            click.echo(
                f"error: cannot redirect fix for {assignment_id} to "
                f"{selection.machine.name!r} — branch {assignment.branch!r} "
                f"is not on the remote, so a different machine cannot fetch "
                f"it. The original machine {assignment.machine_name!r} must "
                "push it first, or dispatch from that machine once it's "
                "reachable again.",
                err=True,
            )
            sys.exit(1)
        click.echo(
            f"  redirecting: original machine {assignment.machine_name!r} "
            f"unusable — dispatching to {selection.machine.name!r} instead "
            "(#3208; the branch is fetched from the remote)",
        )

    # Determine escalated model for the fix-up.
    original_model = assignment.model or cfg.models.default
    escalated = cfg.models.next_model(original_model)
    if escalated != original_model:
        click.echo(f"  escalating model: {original_model} → {escalated}")

    try:
        new_id = _dispatch_followup(
            cfg, assignment, briefing, model=escalated,
            machine_name=selection.machine.name,
        )
    except httpx.HTTPError as e:
        click.echo(f"error: dispatch failed: {e}", err=True)
        sys.exit(1)
    except DispatchRefused as e:
        # #1844: this arm of `coord fix` (a failed test / red CI, reached
        # from a WORK assignment id — see `coord/drive.py`'s
        # `command=("fix", state.work_aid)`) is a RUN action `coord drive`
        # dispatches directly, and `_dispatch_followup` → `dispatch()` raises
        # THIS (a `ValueError` subclass) specifically for a deterministic
        # pre-dispatch guard refusal — not worth a retry. (The OTHER `coord
        # fix` arm — a request-changes review id — routes through
        # `_fix_from_review` → `coord.auto_loop._dispatch_fix`, which POSTs
        # directly and never runs these guards at all; this branch does not
        # cover it.) Same distinguishable exit code as `_dispatch_headless`'s
        # equivalent branch (coord/commands/dispatch_workers.py), so `coord
        # drive`'s subprocess call can tell this apart from a crash.
        from coord.drive import EXIT_DISPATCH_REFUSED  # noqa: PLC0415

        click.echo(f"error: {e}", err=True)
        sys.exit(EXIT_DISPATCH_REFUSED)
    except ValueError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    # #3210: a same-branch fix is about to rewrite the code on this branch —
    # any UAT verdict already recorded on `assignment` (passed OR failed)
    # describes code that is about to be superseded. Clear it back to NULL
    # (the reset `state._record_uat_verdict_local` already documents, just
    # never wired to this seam) so `evaluate_uat_verdict` asks for a fresh
    # look at what actually lands, instead of either replaying a stale FAILED
    # against a fix that already addressed it (format-converter#6) or —
    # the dangerous direction — silently clearing the merge gate off a stale
    # PASSED for code no human has looked at.
    if assignment.uat_state is not None:
        from coord.state import record_uat_verdict  # noqa: PLC0415

        prior_uat_state = assignment.uat_state
        record_uat_verdict(assignment_id=assignment.assignment_id, uat_state=None)
        click.echo(
            f"  uat verdict cleared (#3210): {assignment.assignment_id} carried "
            f"uat_state={prior_uat_state!r} for the pre-fix code — a fresh "
            "`coord uat` verdict is required before this can merge."
        )

    click.echo(f"Fix-up worker dispatched (assignment {new_id})")
    click.echo(f"  branch: {assignment.branch}")
    click.echo(f"  issue: #{assignment.issue_number}: {assignment.issue_title}")
    if test_output:
        _label = "CI failure summary" if ci_story is not None else "test output"
        click.echo(f"  {_label} included in briefing ({len(test_output)} chars)")


@click.command(
    "approve-plan",
    help=(
        "Approve a completed plan assignment and dispatch a work assignment "
        "to implement it."
    ),
)


@click.argument("assignment_id")
@_CONFIG_OPTION
def approve_plan(assignment_id: str, config_path: Path) -> None:
    from coord.board_service import read_board

    cfg = _load_config(config_path)
    board = read_board()

    assignment = board.find_by_id(assignment_id)
    if assignment is None:
        click.echo(f"error: assignment {assignment_id!r} not found in board", err=True)
        sys.exit(1)

    if assignment.type != "plan":
        click.echo(
            f"error: assignment {assignment_id} is type {assignment.type!r}, not 'plan'. "
            "Only plan assignments can be approved with approve-plan.",
            err=True,
        )
        sys.exit(1)

    if assignment.status != "done":
        click.echo(
            f"error: assignment {assignment_id} is {assignment.status!r}, not 'done'. "
            "The plan worker must finish before you can approve it.",
            err=True,
        )
        sys.exit(1)

    plan_dict = _load_plan_for_assignment(assignment, assignment_id)
    if plan_dict is None:
        click.echo(
            f"error: no plan data found for assignment {assignment_id}.\n"
            "Possible reasons: the log is on a remote machine, or the worker "
            "did not output plan sections.\n"
            "Run 'coord notify' after the worker finishes to parse and cache the plan.",
            err=True,
        )
        sys.exit(1)

    plan_text = _plan_dict_to_text(plan_dict)

    # Build the enhanced briefing for the work assignment.
    original_briefing = (assignment.briefing or "").strip()
    separator = "\n\n" if original_briefing else ""
    enhanced_briefing = (
        original_briefing
        + separator
        + "Your plan was reviewed and approved. Implement exactly as described:\n\n"
        + plan_text
    ).strip()

    # Use files_modify from the plan as the allowed-files hint for the worker.
    from coord.plan_parser import WorkerPlan  # noqa: PLC0415
    plan_obj = WorkerPlan.from_dict(plan_dict)
    files_likely = plan_obj.files_modify or assignment.files_allowed or []

    # #1430: the plan's ESTIMATE — informed by actually reading the code —
    # is a better signal than the label chosen at issue-creation time, so it
    # overrides the label-derived model for the work assignment. Falls back
    # to the label when there's no usable ESTIMATE, and to models.default
    # when neither resolves (mirrors dispatch()'s own precedence).
    label_model: str | None = None
    repo = cfg.repo(assignment.repo_name)
    if repo is not None:
        try:
            from coord import github_ops  # noqa: PLC0415
            issue_data = github_ops.get_issue(repo.github, assignment.issue_number)
            issue_labels = [
                lbl.get("name", "") for lbl in (issue_data.get("labels") or [])
            ]
            label_model = cfg.models.model_for_labels(issue_labels)
        except RuntimeError:
            pass  # best-effort — fall through to estimate/default below
    estimate_model = cfg.models.model_for_estimate(plan_obj.estimate)
    if estimate_model:
        work_model = estimate_model
        click.echo(
            f"  model: {work_model} (from plan ESTIMATE={plan_obj.estimate!r}"
            + (f", overriding label-derived {label_model!r}" if label_model else "")
            + ")"
        )
    elif label_model:
        work_model = label_model
        click.echo(f"  model: {work_model} (from issue label)")
    else:
        work_model = None
        click.echo(f"  model: {cfg.models.default} (default)")

    click.echo(
        f"Approving plan {assignment_id}: "
        f"{assignment.repo_name} #{assignment.issue_number} — {assignment.issue_title}"
    )
    click.echo(f"  Dispatching work assignment to {assignment.machine_name}...")

    try:
        new_id = _dispatch_followup(
            cfg,
            assignment,
            enhanced_briefing,
            model=work_model,
            type="work",
            files_likely=files_likely,
            # The plan is read-only; its recorded branch is a throwaway
            # worktree name (and can be a stale/wrong capture).  Work must
            # branch fresh from the issue, not inherit the plan's branch.
            inherit_branch=False,
        )
    except httpx.HTTPError as e:
        click.echo(f"error: dispatch failed: {e}", err=True)
        sys.exit(1)
    except DispatchRefused as e:
        # #1844: same reasoning as `_dispatch_headless`'s equivalent branch
        # (coord/commands/dispatch_workers.py) — `dispatch()` raises THIS (a
        # `ValueError` subclass) specifically for a deterministic
        # pre-dispatch guard refusal. `coord approve-plan` is the other RUN
        # action `coord drive`'s work-stage dispatch can hit this on (the
        # #1453 plan → work hand-off), so it needs the same distinguishable
        # exit code.
        from coord.drive import EXIT_DISPATCH_REFUSED  # noqa: PLC0415

        click.echo(f"error: {e}", err=True)
        sys.exit(EXIT_DISPATCH_REFUSED)
    except ValueError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    # Persist plan-stage SMOKE_TESTS onto the new work assignment so the
    # TUI surfaces them immediately — and so they survive even if the
    # work worker exits without re-emitting its own block.  The work
    # worker's later SMOKE_TESTS (captured by notify._capture_smoke_tests)
    # overrides this when present.
    if plan_obj.smoke_tests is not None:
        from coord.state import update_assignment_smoke_tests  # noqa: PLC0415
        update_assignment_smoke_tests(new_id, plan_obj.smoke_tests)

    click.echo(f"  Work assignment dispatched (assignment {new_id})")
    click.echo(f"  repo: {assignment.repo_name}  issue: #{assignment.issue_number}")
    click.echo(f"  Run: coord log {new_id} to follow progress")


@click.command(
    "reject-plan",
    help=(
        "Reject a completed plan assignment and re-dispatch for revision "
        "with additional guidance."
    ),
)


@click.argument("assignment_id")
@_CONFIG_OPTION
@click.option(
    "--guidance",
    required=True,
    help="Guidance text explaining what to revise in the plan.",
)


def reject_plan(assignment_id: str, config_path: Path, guidance: str) -> None:
    from coord.board_service import read_board

    cfg = _load_config(config_path)
    board = read_board()

    assignment = board.find_by_id(assignment_id)
    if assignment is None:
        click.echo(f"error: assignment {assignment_id!r} not found in board", err=True)
        sys.exit(1)

    if assignment.type != "plan":
        click.echo(
            f"error: assignment {assignment_id} is type {assignment.type!r}, not 'plan'. "
            "Only plan assignments can be rejected with reject-plan.",
            err=True,
        )
        sys.exit(1)

    if assignment.status != "done":
        click.echo(
            f"error: assignment {assignment_id} is {assignment.status!r}, not 'done'. "
            "The plan worker must finish before you can reject it.",
            err=True,
        )
        sys.exit(1)

    plan_dict = _load_plan_for_assignment(assignment, assignment_id)
    if plan_dict is None:
        click.echo(
            f"error: no plan data found for assignment {assignment_id}.\n"
            "Possible reasons: the log is on a remote machine, or the worker "
            "did not output plan sections.\n"
            "Run 'coord notify' after the worker finishes to parse and cache the plan.",
            err=True,
        )
        sys.exit(1)

    plan_text = _plan_dict_to_text(plan_dict)

    # Build the enhanced briefing for the revised plan assignment.
    original_briefing = (assignment.briefing or "").strip()
    separator = "\n\n" if original_briefing else ""
    enhanced_briefing = (
        original_briefing
        + separator
        + "Previous plan (rejected):\n\n"
        + plan_text
        + "\n\nGuidance:\n\n"
        + guidance.strip()
    ).strip()

    click.echo(
        f"Rejecting plan {assignment_id}: "
        f"{assignment.repo_name} #{assignment.issue_number} — {assignment.issue_title}"
    )
    click.echo(f"  Re-dispatching revised plan to {assignment.machine_name}...")

    try:
        new_id = _dispatch_followup(
            cfg,
            assignment,
            enhanced_briefing,
            type="plan",
            files_likely=list(assignment.files_allowed),
            # Revised plan is read-only too — don't inherit the prior
            # plan's throwaway branch.
            inherit_branch=False,
        )
    except httpx.HTTPError as e:
        click.echo(f"error: dispatch failed: {e}", err=True)
        sys.exit(1)
    except ValueError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    click.echo(f"  Revised plan assignment dispatched (assignment {new_id})")
    click.echo(f"  repo: {assignment.repo_name}  issue: #{assignment.issue_number}")
    click.echo(f"  Run: coord log {new_id} to follow progress")


@click.command(
    "resume-stuck",
    help="Stop a stuck worker and dispatch a continuation with guidance.",
)


@click.argument("assignment_id")
@_CONFIG_OPTION
@click.option("--guidance", required=True, help="Guidance for the continuation worker.")
def resume_stuck(assignment_id: str, config_path: Path, guidance: str) -> None:
    from coord.board_service import read_board

    cfg = _load_config(config_path)
    board = read_board()

    assignment = board.find_by_id(assignment_id)
    if assignment is None:
        click.echo(f"error: assignment {assignment_id!r} not found in board", err=True)
        sys.exit(1)

    if assignment.status != "running":
        click.echo(
            f"error: assignment {assignment_id} is {assignment.status!r}, "
            "can only resume-stuck a running assignment",
            err=True,
        )
        sys.exit(1)

    # Find the machine this assignment is running on
    machine = next(
        (m for m in cfg.machines if m.name == assignment.machine_name), None
    )
    if machine is None:
        click.echo(
            f"error: machine {assignment.machine_name!r} not in config", err=True
        )
        sys.exit(1)

    # Stop the current worker. #1567 changed `coord stop`'s default to NOT
    # push a worker's uncommitted WIP anywhere — but resume-stuck is not a
    # `coord stop`: it immediately dispatches a continuation onto this same
    # branch (below) and that continuation's fresh worktree is built from
    # `origin/<branch>` (see AgentServer._setup_worktree's continuation
    # path), so any uncommitted work MUST reach the branch on the remote or
    # the continuation silently starts over from before it. push_mode=branch
    # keeps the pre-#1567 behaviour here on purpose.
    try:
        resp = httpx.post(
            f"http://{machine.host}:{AGENT_PORT}/cancel/{assignment_id}",
            params={"push_mode": "branch"},
            timeout=10,
        )
        resp.raise_for_status()
        click.echo(f"Cancelled stuck worker on {machine.name}")
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        click.echo(
            f"warning: could not cancel worker on {machine.name}: {e} "
            "(may have already stopped)",
            err=True,
        )

    # Brief pause for cancellation to take effect
    time.sleep(2)

    # Retrieve the stuck message from the agent's progress data
    stuck_message = ""
    try:
        status_resp = httpx.get(
            f"http://{machine.host}:{AGENT_PORT}/status", timeout=5
        )
        if status_resp.status_code == 200:
            status_data = status_resp.json()
            # Check active and completed for progress info
            for entry in status_data.get("active", []) + status_data.get("completed", []):
                if entry.get("id") == assignment_id:
                    progress = entry.get("progress", {})
                    if progress and progress.get("stuck"):
                        stuck_message = progress["stuck"]
                    break
    except Exception:  # noqa: BLE001
        pass

    repo = cfg.repo(assignment.repo_name)
    if repo is None:
        click.echo(f"error: unknown repo {assignment.repo_name!r}", err=True)
        sys.exit(1)

    default_branch = repo.default_branch

    stuck_section = stuck_message if stuck_message else "(no stuck message captured)"

    briefing = (
        f"You are continuing work on issue #{assignment.issue_number}: {assignment.issue_title}\n\n"
        f"The previous worker got stuck on branch {assignment.branch or 'unknown'}. "
        f"You are already on that branch.\n"
        f"Do NOT start over — continue from where they left off.\n\n"
        f"## What was done\n"
        f"Run `git fetch origin && git log --oneline origin/{default_branch}..HEAD` to see previous work.\n"
        f"Run `git diff origin/{default_branch}...HEAD` to see the full diff.\n\n"
        f"## What the previous worker was stuck on\n"
        f"{stuck_section}\n\n"
        f"## Guidance\n"
        f"{guidance}\n\n"
        f"## Rules\n"
        f"- Continue from the existing branch, do not start over\n"
        f"- Commit your work and push with git push origin HEAD"
    )

    # Determine escalated model for the continuation worker.
    original_model = assignment.model or cfg.models.default
    escalated = cfg.models.next_model(original_model)
    if escalated != original_model:
        click.echo(f"  escalating model: {original_model} → {escalated}")

    try:
        new_id = _dispatch_followup(cfg, assignment, briefing, model=escalated)
    except httpx.HTTPError as e:
        click.echo(f"error: dispatch failed: {e}", err=True)
        sys.exit(1)
    except ValueError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    click.echo(f"Continuation dispatched (assignment {new_id})")
    click.echo(f"  branch: {assignment.branch or 'unknown'}")
    click.echo(f"  issue: #{assignment.issue_number}: {assignment.issue_title}")
    click.echo(f"  guidance: {guidance}")