"""Refinement-chat commands: `refine-chat`, `test-chat`, `new-issue-chat`,
`refine-board`, `ready`, `refine`. Extracted from coord/cli.py (#747)."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from coord.commands._common import _apply_label_change, _CONFIG_OPTION, _load_config


@click.command(
    "refine-chat",
    help=(
        "#264: dispatch a refinement-chat session for an issue.\n\n"
        "Seeds a `type=\"refinement\"` `claude -p` worker with the issue "
        "body + recent comments + the repo's CLAUDE.md + a bounded file-tree "
        "snapshot, then prints the new assignment id to stdout.  The TUI "
        "captures the id and opens a ChatController overlay bound to it; "
        "developer-typed turns flow via `POST /inject/{id}` and assistant "
        "replies stream back via the existing SSE watch.\n\n"
        "Read-only — refinement workers have only the `Read` tool; they "
        "cannot mutate the repo or talk to GitHub.  The Done button in the "
        "TUI calls `coord ready` to flip `status:refining` → `status:ready` "
        "on session end.\n\n"
        "REPO is the local repo name from coordinator.yml; ISSUE is the GH "
        "issue number."
    ),
)


@click.argument("repo")
@click.argument("issue", type=int)
@click.option(
    "--machine",
    default=None,
    help="Override machine selection (default: first reachable machine that lists the repo).",
)


@_CONFIG_OPTION
def refine_chat(repo: str, issue: int, machine: str | None, config_path: Path) -> None:
    cfg = _load_config(config_path)
    repo_cfg = cfg.repo(repo)
    if repo_cfg is None:
        click.echo(
            f"error: repo {repo!r} not in coordinator.yml "
            f"(have: {[r.name for r in cfg.repos]})",
            err=True,
        )
        sys.exit(2)

    from coord.refine_chat import dispatch_refinement
    try:
        assignment_id, _picked_machine = dispatch_refinement(
            cfg=cfg,
            repo_cfg=repo_cfg,
            repo=repo,
            issue_number=issue,
            machine_override=machine,
        )
    except RuntimeError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)

    # Also flip status:backlog → status:refining so the lifecycle view
    # shows the issue is being actively refined.  Best-effort; the chat
    # session itself is the actual refinement work.
    _apply_label_change(
        repo, issue, config_path,
        add={"status:refining"},
        remove_if_present={"status:ready", "status:backlog"},
        success_message="",  # no echo — keep stdout clean for the TUI
    )

    # Print the assignment_id as the LAST line on stdout so callers (the
    # TUI) can capture it with a simple "last non-empty line" parse.  Any
    # warnings or progress lines must be written to stderr.
    click.echo(assignment_id)


@click.command(
    "test-chat",
    help=(
        "#314 Phase B: dispatch a test-chat session for a completed work assignment.\n\n"
        "Seeds a `type=\"test-chat\"` `claude -p` worker with the PR diff, "
        "most recent build log, the worker's SMOKE_TESTS block, the repo's "
        "run command, and the repo's CLAUDE.md.  Prints the new assignment id "
        "to stdout.  The TUI captures the id and opens a ChatController overlay "
        "bound to it; developer-typed turns flow via `POST /inject/{id}`.\n\n"
        "Read-plus-Bash — test-chat workers have `Read` and `Bash` tools but "
        "write-side Bash commands (gh, git push, etc.) are blocked by the deny "
        "list in the system prompt.\n\n"
        "WORK_ASSIGNMENT_ID is the id of the work assignment to test (visible "
        "in `coord status` or the TUI Pipeline > Stages tab)."
    ),
)


@click.argument("work_assignment_id")
@click.option(
    "--machine",
    default=None,
    help="Override machine selection (default: first reachable machine that lists the repo).",
)


@_CONFIG_OPTION
def test_chat(work_assignment_id: str, machine: str | None, config_path: Path) -> None:
    """Dispatch a test-chat session for a completed work assignment."""
    from coord import sql  # noqa: PLC0415
    from coord.db import get_connection  # noqa: PLC0415

    cfg = _load_config(config_path)

    # Look up the work assignment to resolve the repo name.
    conn = get_connection()
    row = sql.execute(
        conn,
        "SELECT repo_name FROM assignments WHERE assignment_id=?",
        (work_assignment_id,),
    ).fetchone()
    if row is None:
        click.echo(
            f"error: assignment {work_assignment_id!r} not found in DB",
            err=True,
        )
        sys.exit(1)

    repo = row["repo_name"]
    repo_cfg = cfg.repo(repo)
    if repo_cfg is None:
        click.echo(
            f"error: repo {repo!r} not in coordinator.yml "
            f"(have: {[r.name for r in cfg.repos]})",
            err=True,
        )
        sys.exit(2)

    from coord.test_chat import dispatch_test_chat  # noqa: PLC0415

    try:
        assignment_id, _picked_machine = dispatch_test_chat(
            cfg=cfg,
            repo_cfg=repo_cfg,
            repo=repo,
            work_assignment_id=work_assignment_id,
            machine_override=machine,
        )
    except RuntimeError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)

    # Print the assignment_id as the LAST line on stdout so callers (the TUI)
    # can capture it with a simple "last non-empty line" parse.
    click.echo(assignment_id)


@click.command(
    "new-issue-chat",
    help=(
        "#316: dispatch a new-issue-chat session for drafting a GitHub issue.\n\n"
        "Seeds a `type=\"new-issue-chat\"` `claude -p` worker with the repo's "
        "CLAUDE.md, the per-repo issue guidance from coordinator.yml, and a "
        "list of recently open issues for near-duplicate detection.  Prints "
        "the new assignment id to stdout — the TUI shells this out and binds "
        "a ChatController overlay to the returned id.\n\n"
        "The worker helps the developer draft a well-structured issue body in "
        "the TITLE: / --- / body format.  It does NOT call `gh issue create`; "
        "submission is handled by the TUI.\n\n"
        "REPO is the local repo name from coordinator.yml."
    ),
)


@click.argument("repo")
@click.option(
    "--machine",
    default=None,
    help="Override machine selection (default: first unpaused machine that lists the repo).",
)


@_CONFIG_OPTION
def new_issue_chat(repo: str, machine: str | None, config_path: Path) -> None:
    cfg = _load_config(config_path)
    repo_cfg = cfg.repo(repo)
    if repo_cfg is None:
        click.echo(
            f"error: repo {repo!r} not in coordinator.yml "
            f"(have: {[r.name for r in cfg.repos]})",
            err=True,
        )
        sys.exit(2)

    from coord.new_issue_chat import dispatch_new_issue_chat

    try:
        assignment_id, _picked_machine = dispatch_new_issue_chat(
            repo,
            cfg,
            machine_override=machine,
        )
    except RuntimeError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)

    # Print the assignment id as the LAST stdout line so the TUI can capture
    # it with a simple "last non-empty line" parse.
    click.echo(assignment_id)


@click.command(
    "refine-board",
    help=(
        "#316 Phase C: dispatch a board-level refinement chat for a repo.\n\n"
        "Unlike `refine-chat` (which targets a specific issue), this starts an "
        "open-ended `type=\"refinement\"` session for brainstorming new work, "
        "exploring the codebase, or discussing ideas without being tied to any "
        "particular issue.\n\n"
        "Uses ``issue_number=0`` as the sentinel so the TUI routes the chat to "
        "the Board Chat tab rather than a pipeline issue's Refinement tab.  "
        "Prints the new assignment id to stdout — the TUI shells this out and "
        "binds a ChatController overlay to the returned id.\n\n"
        "REPO is the local repo name from coordinator.yml."
    ),
)


@click.argument("repo")
@click.option(
    "--machine",
    default=None,
    help="Override machine selection (default: first unpaused machine that lists the repo).",
)


@_CONFIG_OPTION
def refine_board(repo: str, machine: str | None, config_path: Path) -> None:
    cfg = _load_config(config_path)
    repo_cfg = cfg.repo(repo)
    if repo_cfg is None:
        click.echo(
            f"error: repo {repo!r} not in coordinator.yml "
            f"(have: {[r.name for r in cfg.repos]})",
            err=True,
        )
        sys.exit(2)

    from coord.refine_chat import dispatch_board_refinement

    try:
        assignment_id, _picked_machine = dispatch_board_refinement(
            cfg=cfg,
            repo=repo,
            machine_override=machine,
        )
    except RuntimeError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)

    # Print the assignment id as the LAST stdout line so the TUI can capture
    # it with a simple "last non-empty line" parse.
    click.echo(assignment_id)


@click.command(
    help=(
        "Mark a refined issue as ready for dispatch.\n\n"
        "Sets the GitHub `status:ready` and `coord` labels and removes "
        "`status:refining` / `status:backlog` if present. After this the "
        "issue appears in the Pipeline panel as Pending with a [Go] button.\n\n"
        "REPO is the local repo name from coordinator.yml; ISSUE is the GH "
        "issue number."
    )
)


@click.argument("repo")
@click.argument("issue", type=int)
@_CONFIG_OPTION
def ready(repo: str, issue: int, config_path: Path) -> None:
    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    slug = repo_entry.github if repo_entry else repo
    _apply_label_change(
        repo, issue, config_path,
        add={"status:ready", "coord"},
        remove_if_present={"status:refining", "status:backlog"},
        success_message=f"#{issue} ({slug}) marked ready for dispatch",
    )


@click.command(
    help=(
        "Mark an issue as in-refinement on GitHub.\n\n"
        "Sets the `status:refining` label and removes `status:ready` if "
        "present so the issue moves out of Refined and back into the "
        "scoping flow.  Symmetric with `coord ready`.\n\n"
        "REPO is the local repo name from coordinator.yml; ISSUE is the "
        "GH issue number."
    )
)


@click.argument("repo")
@click.argument("issue", type=int)
@_CONFIG_OPTION
def refine(repo: str, issue: int, config_path: Path) -> None:
    """#260: TUI right-click 'Refine' fires this command to move a
    Backlog row into the Refining lifecycle section."""
    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    slug = repo_entry.github if repo_entry else repo
    _apply_label_change(
        repo, issue, config_path,
        add={"status:refining"},
        remove_if_present={"status:ready"},
        success_message=f"#{issue} ({slug}) marked status:refining",
    )