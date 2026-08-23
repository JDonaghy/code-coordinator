"""`coord milestone` — Phase 0 (`order`) + Phase 1 (`dispatch`) of #767
(milestone-driven workflow).

Thin CLI glue around ``coord/milestone_order.py`` (the pure DAG/frontier
parser) and ``coord/milestone_dispatch.py`` (Phase 1's machine-picking +
dispatch, and #2335's work-order -> drive-queue translation): fetches the
tracking issue + milestone membership + issue terminal state from GitHub via
``coord.milestone_dispatch.fetch_milestone_context`` (shared by both
subcommands so they see identical inputs), then hands that data to the pure
functions and prints the result.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click
import httpx

from coord.commands._common import _CONFIG_OPTION, _load_config
from coord.milestone_dispatch import (
    DispatchOutcome,
    MilestoneDispatchError,
    dispatch_entry,
    fetch_milestone_context,
    gate_a_status,
    is_milestone_complete,
    pick_machine,
    plan_dispatch,
    plan_queue,
)
from coord.milestone_order import (
    TRACKING_ISSUE_LABEL,
    WorkOrder,
    WorkOrderError,
    WorkOrderNode,
    compute_progress,
    parse_progress,
    parse_sub_issues,
    parse_work_order,
    remove_sub_issues_section,
    render_progress,
    render_sub_issues,
    render_work_order,
    replace_progress_section,
    replace_sub_issues_section,
    replace_work_order_section,
    validate_milestone_membership,
    validate_no_shared_oracle_group,
)


@click.group("milestone")
def milestone_group() -> None:
    """Milestone work-order operations (#767 Phase 0) + the milestone write
    seam (#645)."""


@milestone_group.command(
    "create",
    help=(
        "Create a GitHub milestone through the backend-agnostic tracker seam. "
        "REPO is the local repo name from coordinator.yml. Prints the new "
        "milestone number on success. Routes through the daemon seam so "
        "agents (notably the milestone-chat session) never call `gh` "
        "directly."
    ),
)
@click.argument("repo")
@click.option("--title", required=True, help="Milestone title.")
@click.option("--description", default=None, help="Milestone description (markdown).")
@click.option(
    "--due-on",
    default=None,
    help="Due date, ISO 8601 (e.g. 2026-08-01T00:00:00Z).",
)
@_CONFIG_OPTION
def milestone_create_cmd(
    repo: str,
    title: str,
    description: str | None,
    due_on: str | None,
    config_path: Path,
) -> None:
    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    if repo_entry is None:
        click.echo(f"error: unknown repo {repo!r}", err=True)
        sys.exit(2)

    from coord.state import write_milestone

    try:
        result = write_milestone(
            repo,
            title=title,
            description=description,
            due_on=due_on,
            repo_github=repo_entry.github,
        )
    except Exception as e:  # noqa: BLE001
        click.echo(f"error: milestone create failed: {e}", err=True)
        sys.exit(1)
    click.echo(
        f"milestone #{result.get('number')} created ({repo_entry.github}): "
        f"{result.get('title')}"
    )


@milestone_group.command(
    "edit",
    help=(
        "Edit a GitHub milestone's title/description/due date. REPO is the "
        "local repo name from coordinator.yml; NUMBER is the GH milestone "
        "number. Provide at least one of --title/--description/--due-on. "
        "Routes through the backend-agnostic tracker seam."
    ),
)
@click.argument("repo")
@click.argument("number", type=int)
@click.option("--title", default=None, help="New milestone title.")
@click.option("--description", default=None, help="New milestone description (markdown).")
@click.option(
    "--due-on",
    default=None,
    help="New due date, ISO 8601 (e.g. 2026-08-01T00:00:00Z).",
)
@_CONFIG_OPTION
def milestone_edit_cmd(
    repo: str,
    number: int,
    title: str | None,
    description: str | None,
    due_on: str | None,
    config_path: Path,
) -> None:
    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    if repo_entry is None:
        click.echo(f"error: unknown repo {repo!r}", err=True)
        sys.exit(2)
    if title is None and description is None and due_on is None:
        click.echo(
            "error: provide --title and/or --description and/or --due-on",
            err=True,
        )
        sys.exit(2)

    from coord.state import write_milestone

    try:
        write_milestone(
            repo,
            number=number,
            title=title,
            description=description,
            due_on=due_on,
            repo_github=repo_entry.github,
        )
    except Exception as e:  # noqa: BLE001
        click.echo(f"error: milestone edit failed: {e}", err=True)
        sys.exit(1)
    click.echo(f"milestone #{number} updated ({repo_entry.github})")


@milestone_group.command(
    "assign",
    help=(
        "Assign an existing issue to a milestone. REPO is the local repo name "
        "from coordinator.yml; ISSUE is the GH issue number; MILESTONE is the "
        "milestone number (e.g. `5`) or title (e.g. `v1.0`). Titles are "
        "resolved to their number by fetching open milestones from GitHub.\n\n"
        "Routes through the daemon seam (``POST /issue-milestone``) so the "
        "write is always traceable and the local issues cache "
        "``milestone_number``/``milestone_title`` columns are updated "
        "immediately (no need to wait for ``coord sync``)."
    ),
)
@click.argument("repo")
@click.argument("issue", type=int)
@click.argument("milestone")
@_CONFIG_OPTION
def milestone_assign_cmd(
    repo: str,
    issue: int,
    milestone: str,
    config_path: Path,
) -> None:
    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    if repo_entry is None:
        click.echo(f"error: unknown repo {repo!r}", err=True)
        sys.exit(2)

    from coord import github_ops  # noqa: PLC0415
    from coord.state import assign_issue_milestone  # noqa: PLC0415

    # Resolve milestone argument: numeric → number, string → title lookup.
    milestone_number: int
    milestone_title: str | None

    try:
        milestone_number = int(milestone)
        # Resolve the title from the number so the cache can store it.
        try:
            ms_data = github_ops.get_milestone(repo_entry.github, milestone_number)
            milestone_title = ms_data.get("title")
        except RuntimeError as e:
            click.echo(
                f"error: could not fetch milestone #{milestone_number}: {e}", err=True
            )
            sys.exit(1)
    except ValueError:
        # Treat the argument as a title and resolve to a number.
        try:
            all_ms = github_ops.get_repo_milestones(repo_entry.github)
        except RuntimeError as e:
            click.echo(f"error: could not list milestones: {e}", err=True)
            sys.exit(1)
        matches = [m for m in all_ms if m.get("title") == milestone]
        if not matches:
            click.echo(
                f"error: no open milestone with title {milestone!r} in {repo_entry.github}",
                err=True,
            )
            sys.exit(1)
        if len(matches) > 1:
            click.echo(
                f"error: multiple open milestones match {milestone!r} — "
                "use the milestone number instead",
                err=True,
            )
            sys.exit(1)
        milestone_number = matches[0]["number"]
        milestone_title = matches[0].get("title")

    try:
        assign_issue_milestone(
            repo,
            issue,
            milestone_number,
            milestone_title=milestone_title,
            repo_github=repo_entry.github,
        )
    except Exception as e:  # noqa: BLE001
        click.echo(f"error: milestone assign failed: {e}", err=True)
        sys.exit(1)

    ms_label = (
        f"{milestone_title!r} (#{milestone_number})"
        if milestone_title
        else f"#{milestone_number}"
    )
    click.echo(f"#{issue} ({repo_entry.github}) assigned to milestone {ms_label}")


@milestone_group.command(
    "remove",
    help=(
        "Unassign an issue from its milestone (#1003) — the counterpart to "
        "`coord milestone assign`. REPO is the local repo name from "
        "coordinator.yml; ISSUE is the GH issue number. Idempotent — "
        "clearing an issue that has no milestone is a no-op. Routes through "
        "the daemon seam (``POST /issue-milestone-remove``) so the local "
        "issues cache ``milestone_number``/``milestone_title`` columns are "
        "cleared immediately (no need to wait for ``coord sync``)."
    ),
)
@click.argument("repo")
@click.argument("issue", type=int)
@_CONFIG_OPTION
def milestone_remove_cmd(
    repo: str,
    issue: int,
    config_path: Path,
) -> None:
    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    if repo_entry is None:
        click.echo(f"error: unknown repo {repo!r}", err=True)
        sys.exit(2)

    from coord.state import unassign_issue_milestone  # noqa: PLC0415

    try:
        unassign_issue_milestone(repo, issue, repo_github=repo_entry.github)
    except Exception as e:  # noqa: BLE001
        click.echo(f"error: milestone remove failed: {e}", err=True)
        sys.exit(1)

    click.echo(f"#{issue} ({repo_entry.github}) removed from its milestone")


@milestone_group.command(
    "add-child",
    help=(
        "Idempotent append/remove on an epic tracking issue's `## "
        "Sub-issues` checklist (#1008) — same splice-not-duplicate spirit "
        "as `coord milestone write-order` for `## Work order`, keyed on a "
        "different heading. REPO is the local repo name from "
        "coordinator.yml; EPIC is the GH issue number of the epic tracking "
        "issue; ISSUE is the child issue number to add (or remove, with "
        "--remove).\n\n"
        "Adding an ISSUE already present with identical --group/--after "
        "is a no-op; adding it again with different annotations updates "
        "that line in place (preserving its `[x]` checked state). "
        "--remove drops ISSUE from the checklist (no-op if absent) and "
        "cannot be combined with --group/--after. The resulting checklist "
        "is re-parsed and validated (no duplicate/undeclared-`after`/cycle) "
        "before writing via `github_ops.update_issue_body` — the epic's "
        "`## Work order` section (if any) is left untouched."
    ),
)
@click.argument("repo")
@click.argument("epic", type=int)
@click.argument("issue", type=int)
@click.option(
    "--group", default=None, help="Optional `{group: G}` annotation for ISSUE."
)
@click.option(
    "--after", "after_raw", default=None,
    help="Optional `{after: N,...}` annotation — comma-separated issue numbers.",
)
@click.option(
    "--remove", is_flag=True,
    help="Remove ISSUE from the epic's `## Sub-issues` checklist instead of adding it.",
)
@_CONFIG_OPTION
def milestone_add_child_cmd(
    repo: str,
    epic: int,
    issue: int,
    group: str | None,
    after_raw: str | None,
    remove: bool,
    config_path: Path,
) -> None:
    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    if repo_entry is None:
        click.echo(f"error: unknown repo {repo!r}", err=True)
        sys.exit(2)

    if remove and (group is not None or after_raw is not None):
        click.echo("error: --remove cannot be combined with --group/--after", err=True)
        sys.exit(2)

    after: tuple[int, ...] = ()
    if after_raw:
        try:
            after = tuple(
                int(chunk.strip().lstrip("#"))
                for chunk in after_raw.split(",")
                if chunk.strip()
            )
        except ValueError:
            click.echo(
                f"error: --after must be a comma-separated list of issue "
                f"numbers, got {after_raw!r}",
                err=True,
            )
            sys.exit(2)

    from coord import github_ops  # noqa: PLC0415

    try:
        epic_data = github_ops.get_issue(repo_entry.github, epic)
    except RuntimeError as e:
        click.echo(f"error: could not fetch epic #{epic}: {e}", err=True)
        sys.exit(1)

    if not remove:
        # Catch a typo'd child issue number before it's baked into the
        # epic's body — mirrors write-order's pre-write validation rigor.
        try:
            github_ops.get_issue(repo_entry.github, issue)
        except RuntimeError as e:
            click.echo(f"error: could not fetch issue #{issue}: {e}", err=True)
            sys.exit(1)

    old_body = epic_data.get("body") or ""
    try:
        current = parse_sub_issues(old_body)
    except WorkOrderError as e:
        click.echo(f"error: existing `## Sub-issues` block is invalid: {e}", err=True)
        sys.exit(1)

    nodes = list(current.nodes)
    existing_idx = next(
        (i for i, n in enumerate(nodes) if n.issue_number == issue), None
    )

    if remove:
        if existing_idx is None:
            click.echo(
                f"#{epic} ({repo_entry.github}): #{issue} is not in the "
                "`## Sub-issues` checklist (no-op)"
            )
            return
        nodes.pop(existing_idx)
    else:
        prior_checked = (
            nodes[existing_idx].checked if existing_idx is not None else False
        )
        candidate_node = WorkOrderNode(
            issue_number=issue, group=group, after=after, checked=prior_checked
        )
        if existing_idx is not None:
            if nodes[existing_idx] == candidate_node:
                click.echo(
                    f"#{epic} ({repo_entry.github}): #{issue} already in the "
                    "`## Sub-issues` checklist (no-op)"
                )
                return
            nodes[existing_idx] = candidate_node
        else:
            nodes.append(candidate_node)

    new_block = render_sub_issues(WorkOrder(nodes=tuple(nodes)))
    candidate_body = replace_sub_issues_section(old_body, new_block)

    try:
        parse_sub_issues(candidate_body)
    except WorkOrderError as e:
        click.echo(
            f"error: resulting `## Sub-issues` block would be invalid: {e}", err=True
        )
        sys.exit(1)

    if candidate_body == old_body:
        click.echo(
            f"#{epic} ({repo_entry.github}): `## Sub-issues` unchanged "
            "(idempotent no-op)"
        )
        return

    github_ops.update_issue_body(repo_entry.github, epic, candidate_body)
    action = "removed from" if remove else "added to"
    click.echo(
        f"#{issue} {action} #{epic}'s ({repo_entry.github}) `## Sub-issues` checklist"
    )


@milestone_group.command(
    "sync",
    help=(
        "#1061 (EP-2): backfill the live GitHub sub-issues API from an "
        "epic's `## Work order` and/or `## Sub-issues` blocks, and retire "
        "the old checkbox-based grammar on that body. REPO is the local "
        "repo name from coordinator.yml; EPIC is the GH issue number of "
        "the epic tracking issue.\n\n"
        "`## Work order` and `## Sub-issues` are independent, "
        "not-necessarily-overlapping sources for the same parent->children "
        "relationship (a standalone `add-child` epic may have only a `## "
        "Sub-issues` checklist) — sync unions the two before acting, so a "
        "child listed in either section is never silently dropped. For "
        "every issue in that union, links it as a live sub-issue of EPIC "
        "(skipping any already linked — idempotent, safe to re-run). Then "
        "rewrites `## Work order` to the checkbox-free grammar (`- #N "
        "{group: ..., after: ...}`, no `[ ]`/`[x]` — the box was parsed and "
        "rendered but never read for readiness), folding in any `## "
        "Sub-issues`-only children, and removes the `## Sub-issues` block "
        "entirely — membership is now owned by the live API plus `## Work "
        "order`, so the separate checklist (#1008) is pure duplication. A "
        "body already in the target shape with nothing left to link is "
        "reported unchanged, not rewritten."
    ),
)
@click.argument("repo")
@click.argument("epic", type=int)
@click.option(
    "--dry-run", is_flag=True,
    help="Show what would be linked/rewritten without calling GitHub.",
)
@_CONFIG_OPTION
def milestone_sync_cmd(repo: str, epic: int, dry_run: bool, config_path: Path) -> None:
    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    if repo_entry is None:
        click.echo(f"error: unknown repo {repo!r}", err=True)
        sys.exit(2)

    from coord import github_ops
    from coord.parentage_github import GitHubParentage

    try:
        epic_data = github_ops.get_issue(repo_entry.github, epic)
    except RuntimeError as e:
        click.echo(f"error: could not fetch epic #{epic}: {e}", err=True)
        sys.exit(1)

    old_body = epic_data.get("body") or ""
    try:
        work_order = parse_work_order(old_body)
    except WorkOrderError as e:
        click.echo(f"error: #{epic}'s `## Work order` block is invalid: {e}", err=True)
        sys.exit(1)
    try:
        sub_issues = parse_sub_issues(old_body)
    except WorkOrderError as e:
        click.echo(f"error: #{epic}'s `## Sub-issues` block is invalid: {e}", err=True)
        sys.exit(1)

    if not work_order.nodes and not sub_issues.nodes:
        click.echo(
            f"#{epic} ({repo_entry.github}): no `## Work order` or `## "
            "Sub-issues` block found — nothing to sync"
        )
        return

    # #1061 fix-iteration 1: `## Work order` and `## Sub-issues` are
    # independent, not-necessarily-overlapping sources for the same
    # parent->children relationship (mirrors the `fallback_to_work_order`
    # precedent added to `coord/parentage.py` by #1197) — a standalone
    # `add-child` epic can have only a `## Sub-issues` checklist, and either
    # section can list a child the other doesn't. Union them before deciding
    # what to link/render so neither case silently drops a child; a `##
    # Sub-issues`-only node is folded into the rewritten `## Work order`
    # (preferred per the issue's resolution: `## Work order` + the live API
    # own membership going forward) rather than merely linked and discarded.
    extra_from_sub_issues = [
        n for n in sub_issues.nodes
        if n.issue_number not in work_order.issue_numbers
    ]
    if extra_from_sub_issues:
        merged_block = render_work_order(
            WorkOrder(nodes=tuple(work_order.nodes) + tuple(extra_from_sub_issues)),
            checkbox=False,
        )
        try:
            work_order = parse_work_order(f"## Work order\n{merged_block}")
        except WorkOrderError as e:
            click.echo(
                f"error: #{epic}'s `## Work order` and `## Sub-issues` "
                f"blocks conflict when merged: {e}",
                err=True,
            )
            sys.exit(1)

    parentage = GitHubParentage()
    try:
        already_linked = {c.number for c in parentage.children(repo_entry.github, epic)}
    except RuntimeError as e:
        click.echo(
            f"error: could not fetch #{epic}'s live sub-issues: {e}", err=True
        )
        sys.exit(1)

    to_link = [n.issue_number for n in work_order.nodes if n.issue_number not in already_linked]

    new_block = render_work_order(work_order, checkbox=False)
    candidate_body = replace_work_order_section(old_body, new_block)
    stripped_body = remove_sub_issues_section(candidate_body)
    had_sub_issues = stripped_body != candidate_body
    candidate_body = stripped_body
    body_changed = candidate_body != old_body

    node_source = (
        "`## Work order` + `## Sub-issues`" if extra_from_sub_issues else "`## Work order`"
    )
    click.echo(
        f"#{epic} ({repo_entry.github}): {len(work_order.nodes)} node(s) in "
        f"{node_source}"
    )
    if to_link:
        verb = "would link" if dry_run else "linking"
        click.echo(f"  {verb} as live sub-issues: " + ", ".join(f"#{n}" for n in to_link))
    else:
        click.echo("  live sub-issues API already up to date (no-op)")

    if body_changed:
        bits = ["checkbox-free `## Work order`"]
        if extra_from_sub_issues:
            bits.append(
                "folded "
                + ", ".join(f"#{n.issue_number}" for n in extra_from_sub_issues)
                + " in from `## Sub-issues`"
            )
        if had_sub_issues:
            bits.append("removed `## Sub-issues`")
        verb = "would rewrite" if dry_run else "rewriting"
        click.echo(f"  {verb} body ({', '.join(bits)})")
    else:
        click.echo("  body already in the target shape (no-op)")

    if dry_run:
        click.echo("(dry run — nothing changed)")
        return

    failed: list[tuple[int, str]] = []
    for issue_number in to_link:
        try:
            parentage.add_child(repo_entry.github, epic, issue_number)
        except RuntimeError as e:
            failed.append((issue_number, str(e)))

    for issue_number, err in failed:
        click.echo(
            f"  error: could not link #{issue_number} as a sub-issue of #{epic}: {err}",
            err=True,
        )

    if body_changed:
        github_ops.update_issue_body(repo_entry.github, epic, candidate_body)

    if failed:
        sys.exit(1)


@milestone_group.command(
    "sync-progress",
    help=(
        "#1412 deliverable 2: render live per-item status into a separate "
        "`## Progress` section of an epic's tracking-issue body — `## Work "
        "order` (and, transitionally, `## Sub-issues`) are never touched. "
        "REPO is the local repo name from coordinator.yml; TRACKING_ISSUE "
        "is the GH issue number of the tracking issue.\n\n"
        "Derives each node's status from exactly the same live inputs "
        "`coord milestone order` already prints (terminal state + the "
        "current ready frontier) — never a second notion of readiness. "
        "Idempotent: a fresh run whose computed statuses match what's "
        "already in `## Progress` is a no-op (not even the timestamp is "
        "touched), so this is safe to run on a timer — the daemon tick "
        "does exactly that for every milestone registered via a "
        "non-dry-run `coord milestone dispatch`, regardless of whether "
        "`milestone.auto_dispatch` is enabled (this command dispatches "
        "nothing, so there's no reason to gate it on that flag)."
    ),
)
@click.argument("repo")
@click.argument("tracking_issue", type=int)
@click.option(
    "--dry-run", is_flag=True,
    help="Show the computed status without writing anything.",
)
@_CONFIG_OPTION
def milestone_sync_progress_cmd(
    repo: str, tracking_issue: int, dry_run: bool, config_path: Path
) -> None:
    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    if repo_entry is None:
        click.echo(f"error: unknown repo {repo!r}", err=True)
        sys.exit(2)

    from coord import board_service, github_ops
    from coord.milestone_order import ready_frontier

    try:
        ctx = fetch_milestone_context(repo_entry, tracking_issue)
    except MilestoneDispatchError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    if not ctx.work_order.nodes:
        click.echo(
            f"#{tracking_issue}: no `## Work order` block found — nothing to "
            "report progress on"
        )
        return

    board = board_service.read_board()
    frontier = ready_frontier(
        ctx.work_order,
        board,
        repo_name=repo_entry.name,
        repo_github=repo_entry.github,
        terminal_issues=set(ctx.terminal_issues),
    )
    statuses = compute_progress(ctx.work_order, frontier, ctx.terminal_issues)

    try:
        issue_data = github_ops.get_issue(repo_entry.github, tracking_issue)
    except RuntimeError as e:
        click.echo(f"error: could not fetch #{tracking_issue}: {e}", err=True)
        sys.exit(1)
    old_body = issue_data.get("body") or ""

    click.echo(
        f"Progress for #{tracking_issue} (milestone #{ctx.milestone_number}):"
    )
    for s in statuses:
        bits = [f"[{s.status}]", f"#{s.issue_number}"]
        if s.group:
            bits.append(f"(group {s.group})")
        line = "  " + " ".join(bits)
        if s.detail:
            line += f" — {s.detail}"
        click.echo(line)

    if parse_progress(old_body) == statuses:
        click.echo("`## Progress` already up to date (no-op)")
        return

    if dry_run:
        click.echo("(dry run — would update `## Progress`)")
        return

    from datetime import datetime, timezone

    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    new_block = render_progress(statuses, generated_at=generated_at)
    candidate_body = replace_progress_section(old_body, new_block)
    github_ops.update_issue_body(repo_entry.github, tracking_issue, candidate_body)
    click.echo(f"#{tracking_issue}: updated `## Progress` ({len(statuses)} item(s))")


_DEFAULT_CAPTURE_BODY = (
    "Captured via coord-tui fast plan capture — no work order yet. "
    "Promote to a full epic with `coord milestone chat`."
)


@milestone_group.command(
    "capture",
    help=(
        "Fast-capture a lightweight plan stub (#977): creates a new milestone "
        "titled TEXT, a plain issue (no `epic` label) under it, and assigns "
        "the issue to the milestone — all through the existing write seams "
        "(`coord milestone create` + `coord issue create` + `coord milestone "
        "assign` composed in one step). REPO is the local repo name from "
        "coordinator.yml. Appears immediately in the `coord plans` / TUI "
        "Plans-panel roster flagged `no_work_order`, ready to be promoted to "
        "a full epic later via `coord milestone chat`."
    ),
)
@click.argument("repo")
@click.option(
    "--title",
    required=True,
    help="Plan title (used for both the milestone and the issue).",
)
@click.option(
    "--body",
    default=None,
    help="Issue body (markdown). Defaults to a short note pointing at `coord milestone chat`.",
)
@_CONFIG_OPTION
def milestone_capture_cmd(
    repo: str,
    title: str,
    body: str | None,
    config_path: Path,
) -> None:
    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    if repo_entry is None:
        click.echo(f"error: unknown repo {repo!r}", err=True)
        sys.exit(2)
    slug = repo_entry.github

    from coord.state import assign_issue_milestone, create_issue, write_milestone  # noqa: PLC0415

    try:
        ms = write_milestone(repo, title=title, repo_github=slug)
    except Exception as e:  # noqa: BLE001
        click.echo(f"error: plan capture failed creating milestone: {e}", err=True)
        sys.exit(1)

    try:
        issue = create_issue(repo, title, body or _DEFAULT_CAPTURE_BODY, repo_github=slug)
    except Exception as e:  # noqa: BLE001
        click.echo(
            f"error: plan capture failed creating issue (milestone #{ms.get('number')} "
            f"was already created): {e}",
            err=True,
        )
        sys.exit(1)

    try:
        assign_issue_milestone(
            repo,
            issue["number"],
            ms["number"],
            milestone_title=title,
            repo_github=slug,
        )
    except Exception as e:  # noqa: BLE001
        click.echo(
            f"error: plan capture failed assigning issue #{issue.get('number')} to "
            f"milestone #{ms.get('number')}: {e}",
            err=True,
        )
        sys.exit(1)

    click.echo(
        f"plan captured: milestone #{ms['number']} ({slug}) — issue #{issue['number']} "
        "— no work order yet. Promote via `coord milestone chat`."
    )


@milestone_group.command(
    "order",
    help=(
        "Parse the `## Work order` block from a milestone tracking issue and "
        "print the DAG + current ready frontier. REPO is the local repo name "
        "from coordinator.yml; TRACKING_ISSUE is the GH issue number of the "
        "tracking issue (its body holds the `## Work order` block)."
    ),
)
@click.argument("repo")
@click.argument("tracking_issue", type=int)
@_CONFIG_OPTION
def milestone_order_cmd(repo: str, tracking_issue: int, config_path: Path) -> None:
    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    if repo_entry is None:
        click.echo(f"error: unknown repo {repo!r}", err=True)
        sys.exit(2)

    from coord import board_service

    try:
        ctx = fetch_milestone_context(repo_entry, tracking_issue)
    except MilestoneDispatchError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    if not ctx.work_order.nodes:
        click.echo(f"#{tracking_issue}: no `## Work order` block found")
        return

    board = board_service.read_board()
    from coord.milestone_order import ready_frontier

    frontier = ready_frontier(
        ctx.work_order,
        board,
        repo_name=repo_entry.name,
        repo_github=repo_entry.github,
        terminal_issues=set(ctx.terminal_issues),
    )

    click.echo(
        f"Work order for #{tracking_issue} (milestone #{ctx.milestone_number}):"
    )
    for node in ctx.work_order.nodes:
        state = "done" if node.issue_number in ctx.terminal_issues else "open"
        bits = [f"[{state}]"]
        if node.group:
            bits.append(f"group:{node.group}")
        if node.after:
            bits.append("after:" + ",".join(f"#{d}" for d in node.after))
        click.echo(f"  #{node.issue_number}  {' '.join(bits)}")

    click.echo()
    click.echo("Ready frontier:")
    if frontier.ready:
        for entry in frontier.ready:
            suffix = f"  (group {entry.group})" if entry.group else ""
            click.echo(f"  #{entry.issue_number}{suffix}")
    else:
        click.echo("  (none)")

    if frontier.blocked:
        click.echo()
        click.echo("Blocked:")
        for b in frontier.blocked:
            click.echo(f"  #{b.issue_number}: {b.reason}")


def _echo_outcome(outcome: DispatchOutcome) -> None:
    if outcome.ok:
        click.echo(
            f"  #{outcome.issue_number} -> {outcome.machine_name} "
            f"(dispatched, assignment {outcome.assignment_id})"
        )
        if outcome.model_reason:
            click.echo(f"     model: {outcome.model_reason}")
        if outcome.provider_reason:
            click.echo(f"     provider: {outcome.provider_reason}")
    else:
        click.echo(
            f"  #{outcome.issue_number}: dispatch failed: {outcome.error}", err=True
        )


@milestone_group.command(
    "dispatch",
    help=(
        "Promote a milestone into the pipeline: derive the dependency order "
        "from the `## Work order` DAG and queue every still-open issue into "
        "the drive-queue (`coord drive-queue add --after ...`) in that order "
        "(#2335) — the DQ-4 tick then launches each issue as its pre-reqs "
        "land, with #2247 file-overlap prediction, durable board-backed "
        "state, and `coord drive-queue list/status` observability. `--next` "
        "keeps the lighter direct single-dispatch path. REPO is the local "
        "repo name from coordinator.yml; TRACKING_ISSUE is the GH issue "
        "number of the tracking issue (its body holds the `## Work order` "
        "block). This is the single explicit approval for the whole declared "
        "work order — it does not expand scope beyond what `## Work order` "
        "lists."
    ),
)
@click.argument("repo")
@click.argument("tracking_issue", type=int)
@click.option(
    "--dry-run", is_flag=True,
    help="Show what would dispatch now vs. what's waiting (and on what), without dispatching.",
)
@click.option(
    "--next", "next_", is_flag=True,
    help=(
        "Single-pick mode: show up to 3 ready-frontier items and dispatch "
        "only the one you choose — directly, not via the drive-queue. "
        "Lighter-weight than bulk mode's whole-DAG drive-queue enqueue "
        "(#2335)."
    ),
)
@click.option(
    "--pick", "pick_issue", type=int, default=None,
    help=(
        "#1003: non-interactive companion to --next — dispatch this "
        "specific ready-frontier issue number without the interactive "
        "prompt (the coord-tui Plans-panel/MilestoneDag \"Dispatch next…\" "
        "action's backend; a bare TTY-less `--next` would otherwise hang "
        "on `click.prompt`). Errors if the issue isn't currently in the "
        "ready-to-dispatch frontier."
    ),
)
@_CONFIG_OPTION
def milestone_dispatch_cmd(
    repo: str,
    tracking_issue: int,
    dry_run: bool,
    next_: bool,
    pick_issue: int | None,
    config_path: Path,
) -> None:
    if pick_issue is not None and not next_:
        click.echo("error: --pick requires --next", err=True)
        sys.exit(2)
    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    if repo_entry is None:
        click.echo(f"error: unknown repo {repo!r}", err=True)
        sys.exit(2)

    from coord import board_service

    # #1930 (epic #1440, S-2): a milestone under gate control (`coord
    # milestone drive`) is drained EXCLUSIVELY by the daemon's gate tick, as
    # its `work` state — see `coord.milestone_gate`'s module docstring. This
    # manual command is the second dispatch path #1440 exists to close: an
    # operator who runs it against a gate-controlled milestone would launch
    # the same frontier the daemon is about to (or just did) launch, racing
    # it rather than being refused or delegating to it. Checked before the
    # GitHub fetch below (and before Gate A, `--dry-run`, `--next`) so the
    # refusal costs nothing and can never be silently bypassed by a flag.
    from coord import state as state_mod  # noqa: PLC0415

    try:
        existing_gate = state_mod.get_milestone_gate(
            repo_name=repo_entry.name, tracking_issue=tracking_issue
        )
    except Exception as e:  # noqa: BLE001 — see coord.client.fetch_milestone_gate:
        # a thin client that can't reach the daemon has no way to know
        # whether this milestone is gate-controlled, so refuse rather than
        # risk racing a gate tick it can't see (#1930, epic #1440) — the
        # same "refuse, never race" posture as the guard below, just for
        # "couldn't determine" instead of "confirmed gated".
        click.echo(
            f"error: could not check #{tracking_issue}'s gate status ({e}) "
            "— refusing to dispatch rather than risk racing a gate tick "
            "this client can't see. Retry once the daemon is reachable, or "
            "run this on the daemon host directly.",
            err=True,
        )
        sys.exit(1)
    if existing_gate is not None:
        from coord import milestone_gate as mg  # noqa: PLC0415

        gate_name = str(existing_gate.get("gate") or "")
        gate_label = (
            mg.GATE_LABELS.get(gate_name, gate_name) if gate_name else "an unknown gate"
        )
        click.echo(
            f"error: #{tracking_issue} is under gate control, currently at "
            f"{gate_label} — the daemon's gate tick is the sole driver of "
            "this milestone's frontier (epic #1440). Running `coord "
            "milestone dispatch` here would race it, not replace it. Use "
            "`coord milestone drive --dry-run` to see the planned sequence, "
            "or wait for the daemon to reach `work`.",
            err=True,
        )
        sys.exit(1)

    try:
        ctx = fetch_milestone_context(repo_entry, tracking_issue)
    except MilestoneDispatchError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    # Gate A (#930, docs/ORACLE_LOOP.md): refuse to dispatch any of this
    # milestone's issues until its black-box contract exists. Checked before
    # everything else — including `--dry-run`/`--next` — so the gate is
    # never silently bypassable.
    block_reason = gate_a_status(repo_entry, cfg, ctx.milestone_number)
    if block_reason:
        click.echo(f"error: {block_reason}", err=True)
        sys.exit(1)

    if not ctx.work_order.nodes:
        click.echo(f"#{tracking_issue}: no `## Work order` block found")
        return

    click.echo(
        f"Work order for #{tracking_issue} (milestone #{ctx.milestone_number}):"
    )

    if next_:
        board = board_service.read_board()
        plan = plan_dispatch(
            ctx.work_order, board, cfg, repo_entry, ctx.terminal_issues
        )
        if not plan.to_dispatch:
            click.echo("No ready frontier item can dispatch right now.")
            if plan.skipped:
                click.echo("Ready but no idle machine:")
                for s in plan.skipped:
                    click.echo(f"  #{s.entry.issue_number}: {s.reason}")
            if plan.waiting:
                click.echo("Waiting:")
                for b in plan.waiting:
                    click.echo(f"  #{b.issue_number}: {b.reason}")
            return

        # #1003: --pick short-circuits the interactive prompt entirely — the
        # operator (or, via coord-tui, a client that already knows the
        # issue number it wants) names the issue directly instead of
        # picking an index off a numbered list. Search the FULL
        # to-dispatch set, not just the top-3 `choices` shown to a human —
        # a non-interactive caller has no reason to be limited to the
        # truncated display.
        if pick_issue is not None:
            chosen = next(
                (p for p in plan.to_dispatch if p.entry.issue_number == pick_issue),
                None,
            )
            if chosen is None:
                click.echo(
                    f"error: #{pick_issue} is not in the ready-to-dispatch "
                    "frontier right now",
                    err=True,
                )
                sys.exit(1)
            if dry_run:
                click.echo(
                    f"(dry run — would dispatch #{pick_issue} -> {chosen.machine.name})"
                )
                return
            outcome = dispatch_entry(
                chosen, repo_entry, cfg, board, tracking_issue=tracking_issue
            )
            _echo_outcome(outcome)
            if not outcome.ok:
                sys.exit(1)
            return

        choices = plan.to_dispatch[:3]
        click.echo("Ready frontier — pick one to dispatch:")
        for i, pick in enumerate(choices, 1):
            suffix = f"  (group {pick.entry.group})" if pick.entry.group else ""
            click.echo(f"  {i}. #{pick.entry.issue_number} -> {pick.machine.name}{suffix}")

        if dry_run:
            click.echo("(dry run — not dispatched)")
            return

        idx = click.prompt(
            "Pick one", type=click.IntRange(1, len(choices)), default=1
        )
        chosen = choices[idx - 1]
        outcome = dispatch_entry(chosen, repo_entry, cfg, board, tracking_issue=tracking_issue)
        _echo_outcome(outcome)
        if not outcome.ok:
            sys.exit(1)
        return

    # Bulk mode (#2335): queue the whole declared work order into the
    # drive-queue rather than dispatching the ready frontier directly. Before
    # #2335 this path called `dispatch_entry` per frontier pick and registered
    # the milestone for daemon auto-drain — bypassing the drive-queue
    # entirely: no #2247 overlap prediction, no durable queue entry surviving
    # a coordinator restart, nothing in `coord drive-queue list/status`. The
    # queue's `after=` edges now carry the DAG, so the DQ-4 tick launches each
    # issue as its pre-reqs land; that also makes `register_milestone_drain`
    # redundant here (the queue IS the durable drain, and registering would
    # have the daemon's direct-dispatch drain racing it).
    # #2542: under oracle-loop control (Gate A already checked above), chain
    # the whole milestone's drive-queue entries so the tick can never launch
    # two of them at once — see plan_queue's own docstring for why.
    queue_plan = plan_queue(
        ctx.work_order,
        ctx.terminal_issues,
        repo_entry.name,
        oracle_loop=cfg.acceptance.has_driver(repo_entry.name),
    )

    if not queue_plan:
        click.echo("Nothing to queue — every work-order node is already closed.")
        return

    click.echo("Will queue (drive-queue), in dependency order:")
    for qe in queue_plan:
        bits = []
        if qe.group:
            bits.append(f"group {qe.group}")
        if qe.after:
            bits.append("after " + ", ".join(qe.after))
        suffix = f"  ({'; '.join(bits)})" if bits else ""
        click.echo(f"  #{qe.issue_number}{suffix}")

    if dry_run:
        click.echo()
        click.echo("(dry run — nothing queued)")
        return

    # Reuse `coord drive-queue add`'s own helpers so a milestone enqueue is
    # byte-identical to N manual `add` calls: same pre-write validation, same
    # #2247 overlap prediction (auto-`after` edges recorded to the audit log),
    # same upsert semantics for an issue already queued.
    from coord.commands.drive_queue import (  # noqa: PLC0415
        _applicable_auto_after,
        _predict_overlap,
        _record_overlap_prediction,
    )
    from coord.overlap_predict import fanout_warnings  # noqa: PLC0415
    from coord.drive_queue import (  # noqa: PLC0415
        QueueError,
        entries_from_rows,
        entry_key,
        validate_enqueue,
    )
    from coord.state import enqueue_drive_queue, list_drive_queue  # noqa: PLC0415

    click.echo()
    failures = 0
    queued = 0
    for qe in queue_plan:
        # Re-read per entry so each add validates (and overlap-predicts)
        # against the queue as its just-added siblings left it.
        existing = entries_from_rows(list_drive_queue())
        after = list(qe.after)
        key = entry_key(repo_entry.name, qe.issue_number)
        try:
            validate_enqueue(existing, repo_entry.name, qe.issue_number, after)
        except QueueError as exc:
            click.echo(f"  {key}: not queued: {exc}", err=True)
            failures += 1
            continue
        prediction, staleness_note = _predict_overlap(
            config_path, repo_entry.name, qe.issue_number, existing
        )
        auto_after = _applicable_auto_after(
            existing, repo_entry.name, qe.issue_number, after, prediction
        )
        after = [*after, *auto_after]
        enqueue_drive_queue(repo_entry.name, qe.issue_number, after=after)
        if auto_after:
            _record_overlap_prediction(
                repo_entry.name, qe.issue_number, prediction, auto_after
            )
        suffix = f" after {', '.join(after)}" if after else ""
        # #2601: same notes `drive-queue add` surfaces — the applied reason,
        # any high-fanout directory-token warning, and a staleness note when
        # this entry's own body could only be read from cache.
        notes = []
        if auto_after:
            notes.append(prediction.reason)
        notes.extend(fanout_warnings(prediction))
        if staleness_note:
            notes.append(staleness_note)
        overlap_note = ("\n     " + "\n     ".join(notes)) if notes else ""
        click.echo(f"  queued {key}{suffix}{overlap_note}")
        queued += 1

    if queued:
        click.echo()
        click.echo(
            f"{queued} issue(s) queued — `coord drive-queue tick` (the DQ-4 "
            "timer, or run it manually) launches them in dependency order; "
            "`coord drive-queue list` shows the queue. Entries are durable "
            "across coordinator restarts."
        )

    if failures:
        sys.exit(1)


@milestone_group.command(
    "drive",
    help=(
        "Put a milestone under gate control (#1929, epic #1440): the daemon "
        "walks it through Gate A (contract) -> work -> Gate B (architecture) "
        "-> Gate C (acceptance) -> Gate D (ship), one step per tick, from a "
        "durable board-backed gate record. The `work` state IS the frontier "
        "drain, so a gate-driven milestone is NOT also drained by the "
        "independently-gated `milestone.auto_dispatch` path. Every gate edge "
        "other than the A->work and work->B ones is currently an explicit, "
        "logged HOLD (the individual edge behaviours are epic #1440's other "
        "children) — never a silent fall-through. Use --dry-run to print the "
        "full planned sequence without dispatching or writing anything."
    ),
)
@click.argument("repo")
@click.argument("tracking_issue", type=int)
@click.option(
    "--dry-run", is_flag=True,
    help=(
        "Print the full planned gate sequence + the work order's ready "
        "frontier and exit. Dispatches nothing and writes no gate record."
    ),
)
@_CONFIG_OPTION
def milestone_drive_cmd(
    repo: str, tracking_issue: int, dry_run: bool, config_path: Path
) -> None:
    import dataclasses
    import time as _time

    from coord import board_service, state
    from coord import milestone_gate as mg

    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    if repo_entry is None:
        click.echo(f"error: unknown repo {repo!r}", err=True)
        sys.exit(2)

    try:
        ctx = fetch_milestone_context(repo_entry, tracking_issue)
    except MilestoneDispatchError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    # Resume, don't restart: an existing record is the source of truth for
    # which gate this milestone is in. `drive` on an already-driven milestone
    # re-reports its position rather than rewinding the walk to Gate A.
    try:
        existing = state.get_milestone_gate(
            repo_name=repo_entry.name, tracking_issue=tracking_issue
        )
    except Exception as e:  # noqa: BLE001 — see coord.client.fetch_milestone_gate:
        # a thin client that can't reach the daemon can't tell "never
        # driven" from "already driven, record unreadable right now" —
        # restarting at Gate A below would silently clobber real progress
        # once `save_milestone_gate` lands its wholesale upsert (#1930,
        # epic #1440's non-blocking follow-up: same root cause as the
        # dispatch guard above).
        click.echo(
            f"error: could not read #{tracking_issue}'s existing gate record "
            f"({e}) — refusing to drive rather than risk restarting an "
            "in-progress walk this client can't see. Retry once the daemon "
            "is reachable, or run this on the daemon host directly.",
            err=True,
        )
        sys.exit(1)
    record = mg.GateRecord.from_dict(existing) if existing else None
    if record is None:
        if existing is not None:
            # A record was found but couldn't be parsed (unknown schema, or
            # a `gate` value this build doesn't recognize — e.g. written by
            # a different-schema client via the permissive `/milestone-gate`
            # endpoint). Rewinding silently to Gate A here would re-run
            # gates this milestone may have already cleared — the exact
            # thing GateRecord exists to prevent (see its `from_dict`
            # docstring). Loudly say so instead of guessing, mirroring the
            # warning `_milestone_gate_tick` logs for the same situation.
            click.echo(
                f"warning: existing gate record for {repo_entry.name}"
                f"#{tracking_issue} could not be parsed (raw={existing!r}) — "
                "starting a fresh walk at Gate A. If this milestone had "
                "already cleared gates, that history is now lost; inspect "
                "or delete the malformed record out of band before "
                "re-driving if that matters.",
                err=True,
            )
        now = _time.time()
        record = mg.GateRecord(
            repo_name=repo_entry.name,
            tracking_issue=tracking_issue,
            gate=mg.GATE_A,
            entered_at=now,
            updated_at=now,
            milestone_number=ctx.milestone_number,
        )
    else:
        record = dataclasses.replace(record, milestone_number=ctx.milestone_number)

    board = board_service.read_board()
    probes = mg.probe_milestone(ctx, board, cfg, repo_entry)
    steps = mg.plan_sequence(record, probes)

    # The frontier half of the dry-run report. Computed for both modes so the
    # non-dry-run confirmation shows what the first `work` tick will do.
    # #2542: pass the same `oracle_loop` the gate tick itself will use, so
    # this preview never shows more than the one entry the actual `work`
    # gate would dispatch for an oracle-loop milestone.
    if ctx.work_order.nodes:
        plan = plan_dispatch(
            ctx.work_order, board, cfg, repo_entry, ctx.terminal_issues,
            oracle_loop=cfg.acceptance.has_driver(repo_entry.name),
        )
        to_dispatch, skipped, waiting, deferred = (
            plan.to_dispatch, plan.skipped, plan.waiting, plan.deferred,
        )
    else:
        to_dispatch, skipped, waiting, deferred = [], [], [], []

    if dry_run:
        for line in mg.format_plan(
            record, steps,
            to_dispatch=to_dispatch, skipped=skipped, waiting=waiting,
            deferred=deferred,
        ):
            click.echo(line)
        return

    if not ctx.work_order.nodes:
        click.echo(
            f"error: #{tracking_issue} has no `## Work order` block — nothing "
            "to drive (write one with `coord milestone write-order`)",
            err=True,
        )
        sys.exit(1)

    # #1930: this call is the ENTIRE side effect of `drive` — it persists the
    # record (unchanged, if one already existed) but never calls
    # `mg.apply_step` and never dispatches. Only the daemon's
    # `_milestone_gate_tick` advances the walk or drains `work`, so running
    # `drive` again (same operator, a second operator, a second machine) is
    # inert: same key, same record, nothing to race. See
    # `coord.milestone_gate`'s "Exactly one overseer" module docstring.
    state.save_milestone_gate(record.to_dict())
    for line in mg.format_plan(
        record, steps, to_dispatch=to_dispatch, skipped=skipped, waiting=waiting,
        deferred=deferred, footer=False,
    ):
        click.echo(line)
    click.echo()
    click.echo(
        f"Milestone #{tracking_issue} is now under gate control at "
        f"{mg.GATE_LABELS.get(record.gate, record.gate)}. The daemon advances "
        "it one gate per tick; `coord milestone drive --dry-run` re-prints the "
        "plan at any time."
    )


def _resolve_milestone_membership(
    repo_entry, milestone_number: int, work_order: WorkOrder
) -> tuple[set[int], set[int]]:
    """Resolve which of *work_order*'s issue numbers belong to the milestone
    and which have already reached a closed/terminal state.

    Open issues under the milestone come free from one ``get_open_issues``
    call; any work-order node not in that set gets an individual
    ``get_issue`` lookup (closed, or foreign) to classify it. Used by
    ``coord milestone write-order`` to validate proposed work-order content
    before writing it (``coord milestone order``/``dispatch`` resolve
    membership via ``milestone_dispatch.fetch_milestone_context`` instead).
    """
    from coord import github_ops

    open_issues = github_ops.get_open_issues(repo_entry.github)
    milestone_issue_numbers = {
        i["number"]
        for i in open_issues
        if (i.get("milestone") or {}).get("number") == milestone_number
    }
    terminal_issues: set[int] = set()
    for node in work_order.nodes:
        if node.issue_number in milestone_issue_numbers:
            continue
        node_data = github_ops.get_issue(repo_entry.github, node.issue_number)
        node_milestone_number = (node_data.get("milestone") or {}).get("number")
        if node_milestone_number == milestone_number:
            milestone_issue_numbers.add(node.issue_number)
        if node_data.get("state", "").upper() == "CLOSED":
            terminal_issues.add(node.issue_number)
    return milestone_issue_numbers, terminal_issues


@milestone_group.command(
    "write-order",
    help=(
        "#770 (Phase 2 of #767): validate and write a `## Work order` block "
        "into a milestone tracking issue.\n\n"
        "Reads the new checklist lines (e.g. `- #762  {group: A}`, no "
        "heading) from --file or stdin, splices them into the tracking "
        "issue's current body (replacing any existing `## Work order` "
        "section — idempotent, never duplicated), re-parses and validates "
        "the result (cycles, unknown `after` targets, milestone membership) "
        "BEFORE writing, then calls `github_ops.update_issue_body` — the "
        "`coord`, never-raw-`gh` write path #645/#770 require. Also ensures "
        "the tracking issue carries the `epic` label (#1057) — added if "
        "missing, even when the checklist body itself is unchanged — since "
        "`coord plans`/the TUI Plans panel find a milestone's tracking epic "
        "by that label, not by the presence of a `## Work order` block.\n\n"
        "REPO is the local repo name from coordinator.yml; TRACKING_ISSUE is "
        "the GH issue number of the tracking issue (must carry a milestone)."
    ),
)
@click.argument("repo")
@click.argument("tracking_issue", type=int)
@click.option(
    "--file",
    "block_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to a file with the new checklist lines. Reads stdin when omitted.",
)
@_CONFIG_OPTION
def milestone_write_order_cmd(
    repo: str, tracking_issue: int, block_file: Path | None, config_path: Path
) -> None:
    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    if repo_entry is None:
        click.echo(f"error: unknown repo {repo!r}", err=True)
        sys.exit(2)

    new_block = (
        block_file.read_text() if block_file is not None else sys.stdin.read()
    )
    if not new_block.strip():
        click.echo("error: no work-order content given (empty --file/stdin)", err=True)
        sys.exit(2)

    from coord import github_ops

    try:
        issue_data = github_ops.get_issue(repo_entry.github, tracking_issue)
    except RuntimeError as e:
        click.echo(f"error: could not fetch #{tracking_issue}: {e}", err=True)
        sys.exit(1)

    milestone = issue_data.get("milestone") or {}
    milestone_number = milestone.get("number")
    if milestone_number is None:
        click.echo(f"error: #{tracking_issue} has no milestone", err=True)
        sys.exit(1)

    old_body = issue_data.get("body") or ""
    candidate_body = replace_work_order_section(old_body, new_block)

    try:
        work_order = parse_work_order(candidate_body)
    except WorkOrderError as e:
        click.echo(f"error: proposed work order is invalid: {e}", err=True)
        sys.exit(1)

    if not work_order.nodes:
        click.echo(
            "error: proposed work order is empty — refusing to write an "
            "empty `## Work order` block",
            err=True,
        )
        sys.exit(1)

    try:
        milestone_issue_numbers, _terminal = _resolve_milestone_membership(
            repo_entry, milestone_number, work_order
        )
    except RuntimeError as e:
        click.echo(f"error: could not resolve milestone membership: {e}", err=True)
        sys.exit(1)

    try:
        validate_milestone_membership(work_order, milestone_issue_numbers)
    except WorkOrderError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    # #2542: refuse a proposed work order that puts two issues in the same
    # `{group: ...}` cohort when this milestone is under oracle-loop control
    # — every group member's JIT acceptance slice would additively append to
    # the SAME tests/acceptance/ms-N/manifest.yml, a manifest race that
    # exists regardless of whether the issues' actual feature work overlaps
    # (coord-portal#122). Cheap/early check for the explicit-group case only
    # — see validate_no_shared_oracle_group's docstring for what it can't
    # catch.
    try:
        validate_no_shared_oracle_group(
            work_order, oracle_loop=cfg.acceptance.has_driver(repo_entry.name)
        )
    except WorkOrderError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    # #1057: a valid `## Work order` block alone doesn't make this issue
    # findable as the milestone's tracking epic — `coord plans`/the TUI
    # Plans panel key off the `epic` label (TRACKING_ISSUE_LABEL). Ensure it
    # here so promotion is self-consistent, and do it even when the
    # checklist body below turns out to be an idempotent no-op (the #1051
    # repro: a valid work order already present, label missing).
    current_labels = {lbl.get("name") for lbl in issue_data.get("labels") or []}
    label_added = False
    if TRACKING_ISSUE_LABEL not in current_labels:
        github_ops.add_issue_labels(
            repo_entry.github, tracking_issue, [TRACKING_ISSUE_LABEL]
        )
        label_added = True
        click.echo(
            f"#{tracking_issue}: added missing '{TRACKING_ISSUE_LABEL}' label "
            "(tracking issues must carry it to be found as an epic)"
        )

    if candidate_body == old_body:
        click.echo(f"#{tracking_issue}: work order unchanged (idempotent no-op)")
        return

    github_ops.update_issue_body(repo_entry.github, tracking_issue, candidate_body)
    click.echo(
        f"#{tracking_issue}: wrote `## Work order` block "
        f"({len(work_order.nodes)} node(s))"
    )


@milestone_group.command(
    "chat",
    help=(
        "#770 (Phase 2 of #767) + #1009: dispatch a milestone-steward chat "
        "session.\n\n"
        "Seeds a `type=\"milestone-chat\"` `claude -p` worker with the "
        "milestone tracking issue's current body and the open issues filed "
        "under the milestone, then prints the new assignment id to stdout. "
        "The steward discusses the milestone, proposes a `## Work order` "
        "block inferring parallel cohorts (`group`) vs. hard dependencies "
        "(`after`) from the issue bodies, and can also propose creating/"
        "editing the milestone, assigning an issue to it, or splicing a "
        "sub-issue onto its epic — each written only once the operator "
        "confirms in the conversation, via `coord milestone write-order`/"
        "`create`/`edit`/`assign`/`add-child` (never raw `gh`).\n\n"
        "REPO is the local repo name from coordinator.yml; TRACKING_ISSUE is "
        "the GH issue number of the tracking issue (must carry a milestone). "
        "Pass `--new` instead of TRACKING_ISSUE to start a chat for a "
        "brand-new milestone that doesn't have a tracking issue yet. Pass "
        "`--add-child ISSUE` alongside TRACKING_ISSUE (#1017) to seed an "
        "\"add sub-issue\" chat about splicing ISSUE onto the epic's `## "
        "Sub-issues` checklist via `coord milestone add-child`."
    ),
)
@click.argument("repo")
@click.argument("tracking_issue", type=int, required=False, default=None)
@click.option(
    "--new",
    "is_new",
    is_flag=True,
    help="Start a chat to create a brand-new milestone (no tracking issue yet) instead of discussing an existing one.",
)
@click.option(
    "--title",
    "seed_title",
    default=None,
    help="Optional seed title for --new (the operator can still change it in conversation).",
)
@click.option(
    "--seed",
    "seed_prompt",
    default=None,
    help="Optional seed prompt for --new describing the milestone's goal/scope.",
)
@click.option(
    "--add-child",
    "add_child_issue",
    type=int,
    default=None,
    help="Seed an \"add sub-issue\" chat (#1017) about splicing this candidate issue onto TRACKING_ISSUE's epic via `coord milestone add-child`. Requires TRACKING_ISSUE; invalid with --new.",
)
@click.option(
    "--machine",
    default=None,
    help="Override machine selection (default: first unpaused machine that lists the repo).",
)
@_CONFIG_OPTION
def milestone_chat_cmd(
    repo: str,
    tracking_issue: int | None,
    is_new: bool,
    seed_title: str | None,
    seed_prompt: str | None,
    add_child_issue: int | None,
    machine: str | None,
    config_path: Path,
) -> None:
    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    if repo_entry is None:
        click.echo(f"error: unknown repo {repo!r}", err=True)
        sys.exit(2)

    if is_new:
        if tracking_issue is not None:
            click.echo("error: pass either TRACKING_ISSUE or --new, not both", err=True)
            sys.exit(2)
        if add_child_issue is not None:
            click.echo("error: --add-child requires TRACKING_ISSUE, not --new", err=True)
            sys.exit(2)

        from coord.milestone_chat import dispatch_new_milestone_chat

        try:
            assignment_id, _picked_machine = dispatch_new_milestone_chat(
                repo,
                cfg,
                seed_title=seed_title,
                seed_prompt=seed_prompt,
                machine_override=machine,
            )
        except RuntimeError as exc:
            click.echo(f"error: {exc}", err=True)
            sys.exit(1)
        except httpx.HTTPError as exc:
            # #1017 review: dispatch_with_retry raises httpx.HTTPError (not
            # RuntimeError) after exhausting retries on a transient network
            # failure — previously this escaped as an unhandled traceback,
            # which looked to the TUI operator like a silent no-op (the
            # subprocess still exited non-zero, but no clean one-line
            # `error:` reason was there for `first_meaningful_stderr_line`
            # to surface as a toast).
            from coord.network import classify_error

            state, reason = classify_error(exc)
            click.echo(f"error: dispatch failed — {state}: {reason}", err=True)
            sys.exit(1)
        except ValueError as exc:
            # dispatch() raises ValueError for an unknown machine or a
            # missing repo_path — likewise not a RuntimeError, likewise
            # deserves a clean one-line message instead of a traceback.
            click.echo(f"error: dispatch failed — {exc}", err=True)
            sys.exit(1)
    else:
        if tracking_issue is None:
            click.echo("error: TRACKING_ISSUE is required unless --new is given", err=True)
            sys.exit(2)
        if seed_title is not None or seed_prompt is not None:
            click.echo("error: --title/--seed only apply with --new", err=True)
            sys.exit(2)

        from coord.milestone_chat import dispatch_milestone_chat

        try:
            assignment_id, _picked_machine = dispatch_milestone_chat(
                repo,
                tracking_issue,
                cfg,
                machine_override=machine,
                add_child_issue=add_child_issue,
            )
        except RuntimeError as exc:
            click.echo(f"error: {exc}", err=True)
            sys.exit(1)
        except httpx.HTTPError as exc:
            # See the --new branch above (#1017 review) for why this and the
            # ValueError case are caught separately from RuntimeError.
            from coord.network import classify_error

            state, reason = classify_error(exc)
            click.echo(f"error: dispatch failed — {state}: {reason}", err=True)
            sys.exit(1)
        except ValueError as exc:
            click.echo(f"error: dispatch failed — {exc}", err=True)
            sys.exit(1)

    # Print the assignment id as the LAST stdout line so callers (the TUI,
    # eventually — #771/#645) can capture it with a "last non-empty line"
    # parse, matching refine-chat / new-issue-chat.
    click.echo(assignment_id)


@milestone_group.command(
    "gate-c",
    help=(
        "Gate C (docs/ORACLE_LOOP.md, #932): the milestone's FULL "
        "accumulated acceptance suite must be green before it ships — "
        "catches the integration gaps *between* issues that per-issue "
        "acceptance runs miss. Runs the repo's acceptance driver once "
        "against the current checkout (`--path`, default: the repo's "
        "configured local checkout) and reports pass/fail with a "
        "non-zero exit on red, alongside a per-issue rollup of the "
        "milestone's own Acceptance box state (e.g. \"3/7 acceptance "
        "green\") for visibility. This is a manual, read-only check the "
        "operator runs before shipping — it mutates no git state and "
        "blocks nothing automatically. The ship step itself is `coord "
        "milestone ship` (Gate D, #934), which merges `feature/ms-NN → "
        "develop`."
    ),
)
@click.argument("repo")
@click.argument("tracking_issue", type=int)
@click.option(
    "--path", "path_opt", type=click.Path(file_okay=False), default=None,
    help="Repo checkout to run the driver in (default: repo_paths in coordinator.yml).",
)
@click.option(
    "--for-path", "route_path", default=None,
    help=(
        "Repo-relative path (e.g. 'coord/foo.py') used to resolve a "
        "routed acceptance driver (acceptance.drivers.<repo>.routes) — "
        "required when the repo's driver is routed; unused/ignored for a "
        "flat (unrouted) driver. NOT the same as --path (the checkout dir)."
    ),
)
@_CONFIG_OPTION
def milestone_gate_c_cmd(
    repo: str,
    tracking_issue: int,
    path_opt: str | None,
    route_path: str | None,
    config_path: Path,
) -> None:
    from coord.acceptance import build_verdict, ms_dirname
    from coord.acceptance_drivers import DriverError, run_driver

    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    if repo_entry is None:
        click.echo(f"error: unknown repo {repo!r}", err=True)
        sys.exit(2)

    driver_cfg = cfg.acceptance.driver_for(repo, route_path)
    if driver_cfg is None:
        if cfg.acceptance.has_driver(repo):
            click.echo(
                f"error: repo {repo!r} has a routed acceptance driver "
                "(acceptance.drivers routes) but no route matched — pass "
                "--for-path to select the subtree (e.g. 'coord/**')",
                err=True,
            )
        else:
            click.echo(
                f"error: no acceptance driver configured for repo {repo!r} "
                "(add it under acceptance.drivers in coordinator.yml)",
                err=True,
            )
        sys.exit(1)

    if path_opt is not None:
        repo_dir = Path(path_opt).expanduser()
    else:
        from coord.test_orchestrator import find_local_repo_path

        found = find_local_repo_path(repo, cfg)
        if found is None:
            click.echo(
                f"error: no local repo checkout found for {repo!r} "
                "(repo_paths in coordinator.yml) — pass --path",
                err=True,
            )
            sys.exit(1)
        repo_dir = found

    try:
        ctx = fetch_milestone_context(repo_entry, tracking_issue)
    except MilestoneDispatchError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    click.echo(
        f"Gate C for #{tracking_issue} (milestone #{ctx.milestone_number}) "
        f"— running the full accumulated acceptance suite in {repo_dir}..."
    )
    # #1125 review finding 2: gate-c already knows the milestone number
    # directly (no manifest lookup needed, unlike the per-issue `run`/
    # `record` commands) — resolve `{ms}` from it so a routed
    # `run: "pytest tests/acceptance/{ms}"` driver actually runs the right
    # suite dir for Gate C's "full accumulated suite" semantics.
    ms = ms_dirname(ctx.milestone_number)
    try:
        result = run_driver(driver_cfg.kind, driver_cfg.run, cwd=str(repo_dir), ms=ms)
    except DriverError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    if not result.tests and result.exit_code != 0:
        click.echo(result.raw_output, err=True)

    verdict = build_verdict(result.tests, scope="all")
    click.echo(json.dumps(verdict, indent=2))

    # Per-issue rollup: how many of the milestone's own issues already have
    # a passed Acceptance box, for visibility alongside the full-suite
    # verdict (docs/ORACLE_LOOP.md's "3/7 acceptance green" example is a
    # per-milestone-member reading, not this full-suite one).
    if ctx.work_order.nodes:
        from coord import board_service
        from coord.diagnose import stage_assignments

        board = board_service.read_board()
        passed = 0
        with_signal = 0
        for node in ctx.work_order.nodes:
            work = stage_assignments(board, repo, node.issue_number, "work")
            with_state = [a for a in work if (a.acceptance_state or "") != ""]
            if not with_state:
                continue
            with_signal += 1
            if with_state[0].acceptance_state == "passed":
                passed += 1
        click.echo(
            f"\nMilestone Acceptance boxes: {passed}/{with_signal} passed "
            f"({len(ctx.work_order.nodes)} issue(s) total, "
            f"{len(ctx.work_order.nodes) - with_signal} with no verdict yet)"
        )

    if verdict["total"] == 0 or not verdict["green"]:
        click.echo(f"\nGate C RED for #{tracking_issue} — milestone is not ready to ship.")
        sys.exit(1)
    click.echo(f"\nGate C GREEN for #{tracking_issue}.")


@milestone_group.command(
    "gate-b",
    help=(
        "Gate B (docs/PIPELINE_V2.md, #933): after every issue in the "
        "milestone has landed, dispatch an independent architecture review "
        "that checks the ASSEMBLED result against the Gate-A contract — "
        "was it implemented to spec, did the pieces integrate. Routes "
        "through the review pipeline (type=review, gh access via the "
        "coordinator posting on the reviewer's behalf) rather than "
        "`coord assign`, whose workers have `gh` denied. The verdict is "
        "posted as a comment on TRACKING_ISSUE when the reviewer finishes; "
        "request-changes means bounce, not ship — this command itself is a "
        "manual, non-automated gate, same posture as `coord milestone "
        "gate-c`. The ship step itself is `coord milestone ship` (Gate D, "
        "#934), which merges `feature/ms-NN -> develop`."
    ),
)
@click.argument("repo")
@click.argument("tracking_issue", type=int)
@click.option(
    "--machine", "machine_name", default=None,
    help="Dispatch to this machine by name instead of the first idle/capable one.",
)
@click.option(
    "--dry-run", is_flag=True,
    help="Show the target machine + issue list without dispatching.",
)
@_CONFIG_OPTION
def milestone_gate_b_cmd(
    repo: str,
    tracking_issue: int,
    machine_name: str | None,
    dry_run: bool,
    config_path: Path,
) -> None:
    from coord import board_service
    from coord.gate_b import GateBError, dispatch_gate_b_review

    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    if repo_entry is None:
        click.echo(f"error: unknown repo {repo!r}", err=True)
        sys.exit(2)

    try:
        ctx = fetch_milestone_context(repo_entry, tracking_issue)
    except MilestoneDispatchError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    # Gate A must already be satisfied — Gate B's rubric IS the Gate-A
    # contract, so there is nothing to review against otherwise.
    block_reason = gate_a_status(repo_entry, cfg, ctx.milestone_number)
    if block_reason:
        click.echo(f"error: {block_reason}", err=True)
        sys.exit(1)

    if not ctx.work_order.nodes:
        click.echo(
            f"#{tracking_issue}: no `## Work order` block found — nothing for "
            "Gate B to review",
            err=True,
        )
        sys.exit(1)

    if not is_milestone_complete(ctx):
        incomplete = [
            n.issue_number
            for n in ctx.work_order.nodes
            if n.issue_number not in ctx.terminal_issues
        ]
        click.echo(
            "error: Gate B runs only after every issue in the milestone has "
            "landed — still open: "
            + ", ".join(f"#{n}" for n in incomplete),
            err=True,
        )
        sys.exit(1)

    board = board_service.read_board()

    if machine_name is not None:
        machine = next((m for m in cfg.machines if m.name == machine_name), None)
        if machine is None:
            click.echo(f"error: unknown machine {machine_name!r}", err=True)
            sys.exit(2)
    else:
        machine = pick_machine(repo, board, cfg)
        if machine is None:
            click.echo(
                f"error: no idle, capable, unpaused machine available for {repo!r}",
                err=True,
            )
            sys.exit(1)

    click.echo(
        f"Gate B for #{tracking_issue} (milestone #{ctx.milestone_number}), "
        f"{len(ctx.work_order.nodes)} issue(s) -> {machine.name}"
    )

    if dry_run:
        click.echo("(dry run — not dispatched)")
        return

    try:
        assignment = dispatch_gate_b_review(
            repo_cfg=repo_entry,
            config=cfg,
            machine=machine,
            tracking_issue=tracking_issue,
            milestone_number=ctx.milestone_number,
            work_order=ctx.work_order,
            board=board,
        )
    except GateBError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    click.echo(f"dispatched: assignment {assignment.assignment_id} on {machine.name}")


@milestone_group.command(
    "ship",
    help=(
        "Gate D (docs/PIPELINE_V2.md, #934): ship a milestone's "
        "`feature/ms-NN` branch to `develop` — the last step of the "
        "develop + feature-branch-per-milestone git model. Refuses unless "
        "the repo has opted in (`develop_branch` set in coordinator.yml) "
        "AND both Gate B (an `approve` verdict — `coord milestone gate-b`) "
        "AND Gate C (the full acceptance suite, re-run live here, the same "
        "check `coord milestone gate-c` performs) are green. On success, "
        "opens (or reuses) and merges a PR from `feature/ms-NN` into "
        "`develop_branch`."
    ),
)
@click.argument("repo")
@click.argument("tracking_issue", type=int)
@click.option(
    "--path", "path_opt", type=click.Path(file_okay=False), default=None,
    help="Repo checkout to run Gate C's driver in (default: repo_paths in coordinator.yml).",
)
@click.option(
    "--for-path", "route_path", default=None,
    help="Repo-relative path used to resolve a routed acceptance driver (mirrors `gate-c`).",
)
@click.option(
    "--method", "merge_method", default="merge",
    type=click.Choice(["merge", "squash", "rebase"]),
    help=(
        "PR merge method for feature/ms-NN -> develop (default: 'merge' — "
        "a real merge commit, so the milestone's own commit history is "
        "preserved as a unit rather than flattened/replayed onto develop)."
    ),
)
@click.option(
    "--dry-run", is_flag=True,
    help="Check both gates and report the ship plan without opening/merging a PR.",
)
@_CONFIG_OPTION
def milestone_ship_cmd(
    repo: str,
    tracking_issue: int,
    path_opt: str | None,
    route_path: str | None,
    merge_method: str,
    dry_run: bool,
    config_path: Path,
) -> None:
    from coord import board_service, github_ops
    from coord.acceptance import build_verdict, ms_dirname
    from coord.acceptance_drivers import DriverError, run_driver
    from coord.branch_model import feature_branch_name
    from coord.gate_b import latest_gate_b_verdict

    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    if repo_entry is None:
        click.echo(f"error: unknown repo {repo!r}", err=True)
        sys.exit(2)

    if not repo_entry.develop_branch:
        click.echo(
            f"error: repo {repo!r} has not opted into the develop + "
            "feature-branch-per-milestone git model (#934) — set "
            "develop_branch in coordinator.yml to ship",
            err=True,
        )
        sys.exit(1)

    try:
        ctx = fetch_milestone_context(repo_entry, tracking_issue)
    except MilestoneDispatchError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    if not is_milestone_complete(ctx):
        incomplete = [
            n.issue_number
            for n in ctx.work_order.nodes
            if n.issue_number not in ctx.terminal_issues
        ]
        click.echo(
            "error: cannot ship — still open: "
            + ", ".join(f"#{n}" for n in incomplete),
            err=True,
        )
        sys.exit(1)

    feature_branch = feature_branch_name(ctx.milestone_number)

    # ── Gate B ────────────────────────────────────────────────────────────
    board = board_service.read_board()
    gate_b_verdict = latest_gate_b_verdict(
        board, repo_entry.name, tracking_issue, ctx.milestone_number
    )
    if gate_b_verdict != "approve":
        seen = gate_b_verdict or "no Gate B review found"
        click.echo(
            f"error: Gate B is not green ({seen}) — run `coord milestone "
            f"gate-b {repo} {tracking_issue}` first",
            err=True,
        )
        sys.exit(1)
    click.echo(f"Gate B: approve (milestone #{ctx.milestone_number})")

    # ── Gate C — re-run live, same as `coord milestone gate-c` ─────────────
    driver_cfg = cfg.acceptance.driver_for(repo, route_path)
    if driver_cfg is None:
        if cfg.acceptance.has_driver(repo):
            click.echo(
                f"error: repo {repo!r} has a routed acceptance driver "
                "(acceptance.drivers routes) but no route matched — pass "
                "--for-path to select the subtree (e.g. 'coord/**')",
                err=True,
            )
        else:
            click.echo(
                f"error: no acceptance driver configured for repo {repo!r} "
                "(add it under acceptance.drivers in coordinator.yml)",
                err=True,
            )
        sys.exit(1)

    if path_opt is not None:
        repo_dir = Path(path_opt).expanduser()
    else:
        from coord.test_orchestrator import find_local_repo_path

        found = find_local_repo_path(repo, cfg)
        if found is None:
            click.echo(
                f"error: no local repo checkout found for {repo!r} "
                "(repo_paths in coordinator.yml) — pass --path",
                err=True,
            )
            sys.exit(1)
        repo_dir = found

    click.echo(f"Gate C: running the full accumulated acceptance suite in {repo_dir}...")
    ms = ms_dirname(ctx.milestone_number)
    try:
        result = run_driver(driver_cfg.kind, driver_cfg.run, cwd=str(repo_dir), ms=ms)
    except DriverError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    if not result.tests and result.exit_code != 0:
        click.echo(result.raw_output, err=True)

    verdict = build_verdict(result.tests, scope="all")
    if verdict["total"] == 0 or not verdict["green"]:
        click.echo(
            f"error: Gate C is RED for #{tracking_issue} — milestone is not ready to ship",
            err=True,
        )
        sys.exit(1)
    click.echo(f"Gate C: GREEN ({verdict['passed']}/{verdict['total']})")

    # ── Ship: feature/ms-NN -> develop ──────────────────────────────────────
    if dry_run:
        click.echo(
            f"(dry run) would open/merge a PR: {feature_branch} -> "
            f"{repo_entry.develop_branch} (method={merge_method}), then close "
            f"tracking issue #{tracking_issue} on success"
        )
        return

    if not github_ops.branch_exists_on_remote(repo_entry.github, feature_branch):
        click.echo(
            f"error: {feature_branch} does not exist on {repo_entry.github} — "
            "nothing to ship (no issue in this milestone ever dispatched?)",
            err=True,
        )
        sys.exit(1)

    pr = github_ops.create_pr(
        repo_entry.github,
        base=repo_entry.develop_branch,
        head=feature_branch,
        title=f"Ship milestone #{ctx.milestone_number}: {feature_branch} -> {repo_entry.develop_branch}",
        body=(
            f"Gate B + Gate C both green for milestone #{ctx.milestone_number} "
            f"(tracking issue #{tracking_issue}). Opened by `coord milestone ship`."
        ),
    )
    ok, message = github_ops.merge_pr(repo_entry.github, pr["number"], method=merge_method)
    if not ok:
        click.echo(
            f"error: PR #{pr['number']} ({pr['url']}) opened but merge failed: {message}",
            err=True,
        )
        sys.exit(1)

    click.echo(
        f"shipped: {feature_branch} -> {repo_entry.develop_branch} "
        f"(PR #{pr['number']}, {pr['url']})"
    )

    # ── Close the tracking issue ─────────────────────────────────────────
    # This is the completion signal Gate D (`coord milestone drive`,
    # `coord.milestone_gate.probe_milestone`'s `shipped` probe) watches for
    # — nothing else in the pipeline closes it. `force=True`: this command
    # already proved every work-order node terminal (`is_milestone_complete`
    # above) by the milestone's own definition of "done", which is not
    # necessarily the same set of issues GitHub's sub-issues API would list
    # as this issue's children, so the #1196 open-children guard would be
    # the wrong gate to also apply here. A close failure must not undo the
    # merge that already happened — report it and tell the operator how to
    # finish by hand rather than exiting non-zero.
    try:
        github_ops.close_issue(
            repo_entry.github,
            tracking_issue,
            comment=(
                f"Closed by `coord milestone ship`: {feature_branch} merged "
                f"into {repo_entry.develop_branch} (PR #{pr['number']})."
            ),
            force=True,
        )
        click.echo(
            f"closed tracking issue #{tracking_issue} "
            "(coord milestone drive's Gate D will observe this on its next tick)"
        )
    except RuntimeError as e:
        click.echo(
            f"warning: shipped but could not close tracking issue "
            f"#{tracking_issue}: {e} — close it manually (`gh issue close "
            f"{tracking_issue} --repo {repo_entry.github}`), otherwise Gate D "
            "will hold forever waiting for it",
            err=True,
        )
